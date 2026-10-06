"""ETA model, berth and crane planning (CP-SAT), yard slotting with rehandle counting, and the truck-gate queue."""
import functools
import math

import numpy as np
from ortools.sat.python import cp_model
from sklearn.ensemble import HistGradientBoostingRegressor

from . import world

VERSION = "eta-gbm-1"
STEP = 10                     # planning granularity in minutes
WAIT_WEIGHT, LATE_WEIGHT = 1, 3


@functools.lru_cache(maxsize=4)
def eta_model(seed):
    """Remaining minutes to the pilot station from AIS distance and speed, the gap to the schedule and the queue; trained on past calls."""
    rows = world.history(world.build(seed), seed)
    x = np.array([[r["schedule_gap_min"], r["distance_nm"], r["speed_kn"], r["vessels_waiting"], r["service"]] for r in rows])
    y = np.array([r["remaining_min"] for r in rows])
    cut = int(0.8 * len(rows))
    m = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.05, max_depth=3, loss="absolute_error", random_state=0).fit(x[:cut], y[:cut])
    pred, naive = m.predict(x[cut:]), x[cut:, 1] / x[cut:, 2] * 60
    return {"model": m, "metrics": {"trained_on": cut, "tested_on": len(rows) - cut, "mae_min": round(float(np.abs(pred - y[cut:]).mean()), 1), "mae_distance_over_speed_min": round(float(np.abs(naive - y[cut:]).mean()), 1)}}


def predict_eta(m, point, schedule_eta, waiting, service):
    """From the latest AIS point. -> (predicted arrival minute, naive distance-over-speed arrival)"""
    x = np.array([[schedule_eta - point["t"], point["distance_nm"], point["speed_kn"], waiting, service]])
    return int(point["t"] + m["model"].predict(x)[0]), int(point["t"] + point["distance_nm"] / point["speed_kn"] * 60)


def duration_min(moves, cranes):
    return int(math.ceil(moves / (cranes * world.CRANE_MOVES_PER_HOUR) * 60 / STEP) * STEP)


def fits(v, b):
    return v["loa_m"] + 20 <= b["length_m"] and v["draft_m"] + 1.0 <= b["depth_m"]


def plan_berths(vessels, arrivals, now, locks=None, seconds=10.0):
    """CP-SAT: a berth, a start and a crane count for every vessel. One vessel per berth at a time, at most 9 cranes in use at
    any moment, 2 to 4 cranes per vessel, the berth long and deep enough. Minimise priority-weighted waiting plus a heavier
    penalty on finishing after the vessel's window. vessels: dicts with moves, loa, draft, eta, window_h, priority;
    arrivals: minute each is expected. -> (plan list, info)"""
    locks = locks or {}
    m = cp_model.CpModel()
    per_berth, crane_iv, crane_dem, cost, choice = {b["index"]: [] for b in world.BERTHS}, [], [], [], {}
    H = (now + world.HOURS * 60) // STEP
    for v in vessels:
        a = -(-max(now, arrivals[v["index"]]) // STEP)                      # the first whole step after the arrival
        s = m.NewIntVar(a, H, f"s{v['index']}")
        e = m.NewIntVar(a, H + 200, f"e{v['index']}")
        alts = []
        for b in world.BERTHS:
            if not fits(v, b) or (v["index"] in locks and locks[v["index"]] != b["index"]):
                continue
            for c in range(world.MIN_CRANES, world.MAX_CRANES + 1):
                x = choice[v["index"], b["index"], c] = m.NewBoolVar(f"x{v['index']}_{b['index']}_{c}")
                d = duration_min(v["discharge"] + v["load"], c) // STEP
                iv = m.NewOptionalIntervalVar(s, d, e, x, f"i{v['index']}_{b['index']}_{c}")
                per_berth[b["index"]].append(iv)
                crane_iv.append(iv)
                crane_dem.append(c)
                alts.append(x)
        if not alts:
            return None, {"status": "INFEASIBLE", "why": f"{v['name']} fits no berth"}
        m.AddExactlyOne(alts)
        late = m.NewIntVar(0, H + 200, f"l{v['index']}")
        m.Add(late >= e - (v["eta"] + v["window_h"] * 60) // STEP)
        cost += [WAIT_WEIGHT * v["priority"] * (s - a), LATE_WEIGHT * v["priority"] * late]
    for iv in per_berth.values():
        m.AddNoOverlap(iv)
    m.AddCumulative(crane_iv, crane_dem, world.CRANES)
    m.Minimize(sum(cost))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds, solver.parameters.num_search_workers = seconds, 8
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, {"status": solver.StatusName(status)}
    plan = []
    for v in vessels:
        for (vi, bi, c), x in choice.items():
            if vi == v["index"] and solver.Value(x):
                st = [iv for iv in per_berth[bi] if iv.Name() == f"i{vi}_{bi}_{c}"][0]
                start = solver.Value(st.StartExpr()) * STEP
                plan.append({"vessel": vi, "berth": bi, "cranes": c, "start": start, "end": start + duration_min(v["discharge"] + v["load"], c), "arrival": arrivals[vi]})
    return plan, {"status": solver.StatusName(status), "objective": solver.ObjectiveValue(), "seconds": round(solver.WallTime(), 2)}


def first_come(vessels, arrivals, now, cranes=3):
    """The obvious plan: in order of expected arrival, the first compatible berth that is free, three cranes each (if free)."""
    free, busy_cranes, plan = {b["index"]: now for b in world.BERTHS}, [], []
    for v in sorted(vessels, key=lambda v: arrivals[v["index"]]):
        a = max(now, arrivals[v["index"]])
        ok = [b for b in world.BERTHS if fits(v, b)]
        b = min(ok, key=lambda b: max(a, free[b["index"]]))
        start = max(a, free[b["index"]])
        c = cranes
        while True:                                            # take three cranes if the quay has them at that time, else fewer, else wait
            in_use = sum(cc for s_, e_, cc in busy_cranes if s_ <= start < e_)
            if in_use + c <= world.CRANES:
                break
            if c > world.MIN_CRANES:
                c -= 1
            else:
                start += STEP
        end = start + duration_min(v["discharge"] + v["load"], c)
        busy_cranes.append((start, end, c))
        free[b["index"]] = end
        plan.append({"vessel": v["index"], "berth": b["index"], "cranes": c, "start": start, "end": end, "arrival": arrivals[v["index"]]})
    return sorted(plan, key=lambda p: p["vessel"])


def score(plan, vessels, actual, now):
    """What a plan gives when vessels arrive at `actual`: a vessel waits for its slot or its berth, whichever is later."""
    by_v = {v["index"]: v for v in vessels}
    rows, free = [], {}
    for p in sorted(plan, key=lambda p: p["start"]):
        v = by_v[p["vessel"]]
        start = max(p["start"], actual[p["vessel"]], free.get(p["berth"], 0))
        end = start + (p["end"] - p["start"])
        free[p["berth"]] = end
        rows.append({"vessel": p["vessel"], "name": v["name"], "berth": world.BERTHS[p["berth"]]["code"], "cranes": p["cranes"], "arrival": actual[p["vessel"]], "start": start, "end": end, "wait_min": start - actual[p["vessel"]], "turn_min": end - actual[p["vessel"]],
                     "late_min": max(0, end - (v["eta"] + v["window_h"] * 60)), "priority": v["priority"]})
    crane_h = sum(r["cranes"] * (r["end"] - r["start"]) for r in rows) / 60
    span = max(r["end"] for r in rows) - now
    return {"rows": sorted(rows, key=lambda r: r["vessel"]), "wait_min": sum(r["wait_min"] for r in rows), "weighted_wait_min": sum(r["wait_min"] * r["priority"] for r in rows), "mean_turn_min": round(float(np.mean([r["turn_min"] for r in rows]))),
            "late_min": sum(r["late_min"] for r in rows), "crane_utilisation": round(crane_h / (world.CRANES * span / 60), 3), "makespan_min": span}


def conflicts(plan, arrivals):
    """Pairs on one berth whose slots would overlap if vessels arrive at `arrivals` and hold their planned durations."""
    out = []
    for a in plan:
        for b in plan:
            if a["vessel"] < b["vessel"] and a["berth"] == b["berth"]:
                sa, sb = max(a["start"], arrivals[a["vessel"]]), max(b["start"], arrivals[b["vessel"]])
                ea, eb = sa + a["end"] - a["start"], sb + b["end"] - b["start"]
                if min(ea, eb) - max(sa, sb) >= STEP:
                    out.append({"berth": world.BERTHS[a["berth"]]["code"], "vessels": [a["vessel"], b["vessel"]], "overlap_min": int(min(ea, eb) - max(sa, sb))})
    return out


def slot(containers, holds=(), planned=True):
    """Stack import boxes in the yard. Planned: boxes going out together (same mode, same pickup window) share a stack, the
    latest pickup at the bottom; held boxes go to block H. Unplanned: stacks filled in discharge order. A rehandle is a box
    stacked on top of one that leaves earlier. -> (assignment list, rehandles)"""
    held = set(holds)
    group = lambda c: ("H", -1) if c["index"] in held else (c["mode"], int(c["pickup_after_h"] // 6), c["reefer"], c["hazard"])        # noqa: E731
    order = sorted(containers, key=lambda c: (group(c), -c["pickup_after_h"])) if planned else list(containers)
    blocks = [b for b in world.BLOCKS if b != "H"]
    out, stacks, cursor, rehandles = [], {}, {"H": 0, "Y": 0}, 0
    for c in order:
        pool = "H" if c["index"] in held else "Y"
        k = group(c) if planned else (pool, cursor[pool] - 1)
        stack = stacks.get(k)
        if stack is None or len(stack["boxes"]) == world.TIERS:
            n = cursor[pool]
            cursor[pool] += 1
            k = group(c) if planned else (pool, n)
            stack = stacks[k] = {"block": "H" if pool == "H" else blocks[(n // (world.BAYS * world.ROWS)) % len(blocks)], "bay": (n // world.ROWS) % world.BAYS + 1, "row": n % world.ROWS + 1, "boxes": []}
        rehandles += sum(1 for below in stack["boxes"] if below["pickup_after_h"] < c["pickup_after_h"] and below["index"] not in held)
        stack["boxes"].append(c)
        out.append({"container": c["index"], "block": stack["block"], "bay": stack["bay"], "row": stack["row"], "tier": len(stack["boxes"]), "held": c["index"] in held})
    return out, rehandles


def hold_impact(assignment, holds):
    """Customs holds arrive after stacking. A held box left where it is must be lifted every time a box beneath it is picked;
    moving it to block H costs one move now. -> dict"""
    held = set(holds)
    by_stack = {}
    for a in assignment:
        by_stack.setdefault((a["block"], a["bay"], a["row"]), []).append(a)
    rehandles = sum(sum(1 for b in by_stack[(a["block"], a["bay"], a["row"])] if b["tier"] < a["tier"]) for a in assignment if a["container"] in held)
    on_top = sum(1 for a in assignment if a["container"] in held and max(b["tier"] for b in by_stack[(a["block"], a["bay"], a["row"])]) == a["tier"])
    return {"held": len(held), "rehandles_if_left_in_place": rehandles, "moves_to_reslot": len(held), "held_boxes_already_on_top": on_top, "block": "H"}


def gate(arrivals_per_hour, appointments=None):
    """A fluid queue at the gate hour by hour: trucks arriving (or holding appointments) against the lanes' capacity.
    -> per-hour rows, mean wait, max queue"""
    cap = 60 / world.GATE_MIN_PER_TRUCK * world.GATE_LANES
    q, rows, waits = 0.0, [], []
    for h, n in enumerate(appointments if appointments is not None else arrivals_per_hour):
        served = min(cap, q + n)
        q = q + n - served
        wait = q / cap * 60 if served else 0
        waits += [wait] * int(n)
        rows.append({"hour": h, "trucks": int(n), "served": int(served), "queue_at_end": int(q), "wait_min": round(wait)})
    return {"rows": rows, "mean_wait_min": round(float(np.mean(waits)), 1) if waits else 0.0, "max_queue": max(r["queue_at_end"] for r in rows), "capacity_per_hour": int(cap)}


def smooth(arrivals_per_hour, share=0.9):
    """Appointments: cap each hour at a share of capacity and move the overflow to the next hours with room."""
    cap = int(60 / world.GATE_MIN_PER_TRUCK * world.GATE_LANES * share)
    out, carry = [], 0
    for n in arrivals_per_hour:
        total = int(n) + carry
        take = min(cap, total)
        out.append(take)
        carry = total - take
    if carry:
        out[-1] += carry
    return out
