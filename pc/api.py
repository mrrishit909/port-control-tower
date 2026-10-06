"""Public API (modular monolith). Blueprint services map to: ais-ingestion, vessel-eta-service, berth-planner and crane-scheduler
(one CP-SAT model), yard-optimizer, gate-appointment-service, customs-hold-service (holds through the yard plan) and the
control-tower API. Not built: rail-planner, an event stream, real AIS or TOS adapters.

    uvicorn pc.api:app          python -m core.jobs pc.api      # the worker
"""
import datetime
import uuid

import numpy as np
from fastapi import Depends, Header
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"terminal:read", "jobs:read"}
PLAN = READ | {"ais:write", "planning:run", "gate:write", "scenarios:run"}
PERMISSIONS = {"viewer": READ, "planner": PLAN, "manager": PLAN | {"terminal:load", "audit:read"}}
app = create_app("port-control-tower", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
DAY = datetime.datetime(2026, 10, 5, tzinfo=datetime.UTC)
DAY_SEED = 1
TRUTH_DELAYS = {world.DELAY_SHIP: world.DELAY_H}         # what really happens to the day's arrivals


def ts(minute):
    return DAY + datetime.timedelta(minutes=int(minute))


def clock(m):
    return f"{'D2 ' if m >= 1440 else ''}{(int(m) % 1440) // 60:02d}:{int(m) % 60:02d}"


def vid(t, k):
    return uuid.uuid5(t, f"vessel-{k}")


def bid(t, k):
    return uuid.uuid5(t, f"berth-{k}")


def cid(t, k):
    return uuid.uuid5(t, f"container-{k}")


def worker_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def terminal(c):
    tm = c.execute("SELECT * FROM terminals").fetchone()
    if not tm:
        raise Problem(409, "no_terminal", "load a terminal first: POST /v1/terminal:load")
    return tm, world.build(tm["model_seed"])


def calls(c):
    """Vessels and their calls in the engine's shape, with the arrival estimate to plan on (prediction, else schedule)."""
    rows = c.execute("SELECT v.ordinal, v.name, v.imo, v.loa_m, v.draft_m, p.* FROM port_calls p JOIN vessels v ON v.id = p.vessel_id ORDER BY v.ordinal").fetchall()
    vessels = [{"index": r["ordinal"], "name": r["name"], "imo": r["imo"], "loa_m": float(r["loa_m"]), "draft_m": float(r["draft_m"]), "eta": r["eta_min"], "window_h": int((r["etd"] - r["eta"]).total_seconds() // 3600), "discharge": r["discharge"], "load": r["load"], "priority": r["priority"], "id": r["vessel_id"], "status": r["status"]} for r in rows]
    est = {r["ordinal"]: r["predicted_min"] if r["predicted_min"] is not None else r["eta_min"] for r in rows}
    return vessels, est


def current_plan(c):
    return [{"vessel": r["ordinal"], "berth": r["b"], "cranes": r["cranes"], "start": r["start_min"], "end": r["end_min"], "locked": r["locked"]} for r in
            c.execute("SELECT v.ordinal, b.ordinal AS b, a.cranes, a.start_min, a.end_min, a.locked FROM berth_allocations a JOIN port_calls p ON p.id = a.port_call_id JOIN vessels v ON v.id = p.vessel_id JOIN berths b ON b.id = a.berth_id ORDER BY v.ordinal")]


def save_plan(c, ctx, plan, source):
    c.execute("DELETE FROM berth_allocations")
    for p in plan:
        c.execute("INSERT INTO berth_allocations (id, tenant_id, port_call_id, berth_id, start_ts, end_ts, locked, cranes, start_min, end_min, source) SELECT %s,%s,p.id,%s,%s,%s,%s,%s,%s,%s,%s FROM port_calls p WHERE p.vessel_id = %s",
                  [uuid.uuid7(), ctx.tenant_id, bid(ctx.tenant_id, p["berth"]), ts(p["start"]), ts(p["end"]), p.get("locked", False), p["cranes"], p["start"], p["end"], source, vid(ctx.tenant_id, p["vessel"])])


def describe(plan, vessels, est):
    by = {v["index"]: v for v in vessels}
    return [{"vessel": p["vessel"], "name": by[p["vessel"]]["name"], "berth": world.BERTHS[p["berth"]]["code"], "cranes": p["cranes"], "expected_arrival": clock(est[p["vessel"]]), "start": clock(p["start"]), "end": clock(p["end"]), "start_min": p["start"], "end_min": p["end"],
             "wait_min": p["start"] - est[p["vessel"]], "window_end": clock(by[p["vessel"]]["eta"] + by[p["vessel"]]["window_h"] * 60), "late_min": max(0, p["end"] - by[p["vessel"]]["eta"] - by[p["vessel"]]["window_h"] * 60), "locked": p.get("locked", False)} for p in sorted(plan, key=lambda p: p["start"])]


# ---------------------------------------------------------------- terminal

class LoadIn(BaseModel):
    seed: int = Field(17, ge=0, le=10 ** 6)


@app.post("/v1/terminal:load", status_code=202, tags=["terminal"], summary="(+) Load a synthetic terminal at 10:00: berths, five vessel calls, their containers, AIS so far, the gate forecast")
def load_terminal(body: LoadIn, ctx: Ctx = Depends(auth("terminal:load")), idem: str | None = IdemKey):
    def work(c):
        if c.execute("SELECT 1 FROM terminals LIMIT 1").fetchone():
            raise Problem(409, "terminal_exists")
        return 202, jobs.enqueue(c, ctx, "terminal.load", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("terminal.load")
def load_job(c, job):
    t, w, now = job["tenant_id"], world.build(job["payload"]["seed"]), world.NOW
    tid = uuid.uuid5(t, "terminal")
    c.execute("INSERT INTO terminals (id, tenant_id, name, model_seed, day, now_min) VALUES (%s,%s,'Riverbend Container Terminal',%s,%s,%s)", [tid, t, w["seed"], DAY.date(), now])
    db.load(c, "berths", ["id", "tenant_id", "terminal_id", "ordinal", "code", "length_m", "depth_m", "cranes_available"], [(bid(t, b["index"]), t, tid, b["index"], b["code"], b["length_m"], b["depth_m"], 3) for b in world.BERTHS])
    real = world.real_arrivals(w, DAY_SEED, TRUTH_DELAYS)
    db.load(c, "vessels", ["id", "tenant_id", "terminal_id", "ordinal", "imo", "name", "loa_m", "draft_m"], [(vid(t, v["index"]), t, tid, v["index"], v["imo"], v["name"], v["loa_m"], v["draft_m"]) for v in w["vessels"]])
    db.load(c, "port_calls", ["id", "tenant_id", "vessel_id", "eta", "etd", "actual_arrival", "status", "service", "eta_min", "discharge", "load", "priority"],
            [(uuid.uuid7(), t, vid(t, v["index"]), ts(v["eta"]), ts(v["eta"] + v["window_h"] * 60), ts(real[v["index"]]) if real[v["index"]] <= now else None, "arrived" if real[v["index"]] <= now else "expected", v["service"], v["eta"], v["discharge"], v["load"], v["priority"]) for v in w["vessels"]])
    pts = [(t, vid(t, v["index"]), p["t"], p["distance_nm"], p["speed_kn"]) for v in w["vessels"] for p in world.ais(w, v, real[v["index"]], DAY_SEED, now - 10)]
    db.load(c, "ais_positions", ["tenant_id", "vessel_id", "t_min", "distance_nm", "speed_kn"], pts)
    boxes = world.containers(w, DAY_SEED)
    db.load(c, "containers", ["id", "tenant_id", "terminal_id", "ordinal", "iso_no", "size_type", "weight_kg", "hazard_code", "destination_mode", "vessel_id", "reefer", "pickup_after_h"],
            [(cid(t, b["index"]), t, tid, b["index"], b["iso_no"], b["size"] + "GP", b["weight_kg"], "UN1993" if b["hazard"] else None, b["mode"], vid(t, b["vessel"]), b["reefer"], round(b["pickup_after_h"], 2)) for b in boxes])
    trucks = world.truck_arrivals(w, DAY_SEED)
    db.load(c, "gate_hours", ["tenant_id", "terminal_id", "hour", "trucks_expected"], [(t, tid, h, int(n)) for h, n in enumerate(trucks)])
    metrics = engine.eta_model(w["seed"])["metrics"]
    out = {"terminal_id": tid, "clock": clock(now), "berths": [{"code": b["code"], "length_m": b["length_m"], "depth_m": b["depth_m"]} for b in world.BERTHS], "cranes": world.CRANES,
           "vessels": [{"vessel_id": vid(t, v["index"]), "imo": v["imo"], "name": v["name"], "loa_m": v["loa_m"], "draft_m": v["draft_m"], "schedule_eta": clock(v["eta"]), "moves": v["discharge"] + v["load"], "priority": v["priority"], "status": "arrived" if real[v["index"]] <= now else "expected"} for v in w["vessels"]],
           "containers_to_discharge": len(boxes), "ais_points": len(pts), "trucks_expected_today": int(trucks.sum()), "eta_model": metrics}
    audit.record(c, worker_ctx(job), "terminal.loaded", "terminal", tid, {"vessels": len(w["vessels"]), "containers": len(boxes)})
    return out


# ---------------------------------------------------------------- ais-ingestion and vessel-eta-service

class Position(BaseModel):
    t: datetime.datetime
    distance_nm: float = Field(ge=0, le=500)
    speed_kn: float = Field(ge=0, le=30)


class AisIn(BaseModel):
    imo: str = Field(pattern=r"^\d{7}$")
    positions: list[Position] = Field(min_length=1, max_length=500)


@app.post("/v1/ais/positions", tags=["ais"], summary="AIS positions for one vessel (a resend stores nothing), then a new arrival estimate from the latest point and a check of the berth plan")
def ais_positions(body: AisIn, ctx: Ctx = Depends(auth("ais:write")), idem: str | None = IdemKey):
    def work(c):
        tm, w = terminal(c)
        v = c.execute("SELECT v.*, p.status, p.eta_min FROM vessels v JOIN port_calls p ON p.vessel_id = v.id WHERE v.imo = %s", [body.imo]).fetchone()
        if not v:
            raise Problem(404, "vessel_not_found")
        if v["status"] == "arrived":
            raise Problem(409, "already_arrived")
        if any(p.t.tzinfo is None for p in body.positions):
            raise Problem(422, "timezone_required")
        mins = [int((p.t - DAY).total_seconds() // 60) for p in body.positions]
        if any(not 0 <= m_ <= tm["now_min"] for m_ in mins):
            raise Problem(422, "out_of_window", "positions must be timestamped within the day, up to the terminal clock")
        stored = sum(bool(c.execute("INSERT INTO ais_positions (tenant_id, vessel_id, t_min, distance_nm, speed_kn) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING t_min", [ctx.tenant_id, v["id"], m_, p.distance_nm, p.speed_kn]).fetchone()) for m_, p in zip(mins, body.positions))
        latest = c.execute("SELECT t_min, distance_nm, speed_kn FROM ais_positions WHERE vessel_id = %s ORDER BY t_min DESC LIMIT 1", [v["id"]]).fetchone()
        waiting = c.execute("SELECT count(*) AS n FROM port_calls WHERE status = 'arrived' AND vessel_id NOT IN (SELECT p.vessel_id FROM berth_allocations a JOIN port_calls p ON p.id = a.port_call_id WHERE a.start_min <= %s)", [tm["now_min"]]).fetchone()["n"]
        pred, naive = engine.predict_eta(engine.eta_model(w["seed"]), {"t": latest["t_min"], "distance_nm": float(latest["distance_nm"]), "speed_kn": float(latest["speed_kn"])}, v["eta_min"], waiting, 0)
        c.execute("UPDATE port_calls SET predicted_eta = %s, predicted_min = %s WHERE vessel_id = %s", [ts(pred), pred, v["id"]])
        vessels, est = calls(c)
        plan = current_plan(c)
        conf = engine.conflicts(plan, est) if plan else []
        audit.record(c, ctx, "ais.received", "vessel", v["id"], {"imo": body.imo, "predicted_eta": clock(pred), "schedule_eta": clock(v["eta_min"])})
        return 200, {"vessel": v["name"], "imo": body.imo, "received": len(mins), "stored": stored, "duplicates_ignored": len(mins) - stored, "latest": {"t": clock(latest["t_min"]), "distance_nm": float(latest["distance_nm"]), "speed_kn": float(latest["speed_kn"])},
                     "schedule_eta": clock(v["eta_min"]), "predicted_eta": clock(pred), "late_min": pred - v["eta_min"], "distance_over_speed_eta": clock(naive), "berth_conflicts_under_current_plan": [{**x, "vessels": [vessels[i]["name"] for i in x["vessels"]]} for x in conf], "model": engine.VERSION}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- berth-planner and crane-scheduler

class Lock(BaseModel):
    imo: str = Field(pattern=r"^\d{7}$")
    berth: str = Field(pattern=r"^B[123]$")


class BerthPlanIn(BaseModel):
    locks: list[Lock] = Field([], max_length=5)


@app.post("/v1/planning/berths", status_code=201, tags=["planning"], summary="Berth, start and crane count for every vessel (CP-SAT), against the first-come plan; the result becomes the current plan")
def plan_berths(body: BerthPlanIn, ctx: Ctx = Depends(auth("planning:run")), idem: str | None = IdemKey):
    def work(c):
        tm, w = terminal(c)
        vessels, est = calls(c)
        locks = {}
        for lock in body.locks:
            v = next((v for v in vessels if v["imo"] == lock.imo), None)
            if not v:
                raise Problem(404, "vessel_not_found", lock.imo)
            b = next(b for b in world.BERTHS if b["code"] == lock.berth)
            if not engine.fits(v, b):
                raise Problem(422, "vessel_does_not_fit", f"{v['name']} ({v['loa_m']} m, {v['draft_m']} m) does not fit {lock.berth}")
            locks[v["index"]] = b["index"]
        plan, info = engine.plan_berths(vessels, est, tm["now_min"], locks)
        if plan is None:
            raise Problem(409, "no_plan", info.get("why", info["status"]))
        for p in plan:
            p["locked"] = p["vessel"] in locks
        simple = engine.first_come(vessels, est, tm["now_min"])
        save_plan(c, ctx, plan, "cp-sat")
        mine, theirs = engine.score(plan, vessels, est, tm["now_min"]), engine.score(simple, vessels, est, tm["now_min"])
        pid = uuid.uuid7()
        summary = {"solver": info, "expected": {"optimised": {k: v for k, v in mine.items() if k != "rows"}, "first_come": {k: v for k, v in theirs.items() if k != "rows"}}, "locks": body.model_dump()["locks"]}
        c.execute("INSERT INTO plans (id, tenant_id, terminal_id, kind, summary, created_by) VALUES (%s,%s,%s,'berths',%s,%s)", [pid, ctx.tenant_id, tm["id"], Jsonb(summary), ctx.actor_id])
        audit.record(c, ctx, "berths.planned", "plan", pid, {"vessels": len(plan), "weighted_wait_min": mine["weighted_wait_min"], "late_min": mine["late_min"]})
        return 201, {"plan_id": pid, **summary, "allocations": describe(plan, vessels, est), "first_come_allocations": describe(simple, vessels, est), "conflicts": engine.conflicts(plan, est),
                     "how": "one optional interval per (vessel, berth, crane count); no overlap per berth; a cumulative constraint keeps cranes in use at or below nine; the objective is priority-weighted waiting plus three times the minutes a vessel finishes after its window"}
    return run(ctx, idem, body, work)


@app.post("/v1/planning/cranes", tags=["planning"], summary="The crane schedule the current berth plan implies: cranes per vessel and in use through the day, with utilisation")
def plan_cranes(ctx: Ctx = Depends(auth("planning:run"))):
    with db.tx(ctx.tenant_id) as c:
        tm, w = terminal(c)
        vessels, est = calls(c)
        plan = current_plan(c)
    if not plan:
        raise Problem(409, "no_plan", "plan the berths first: POST /v1/planning/berths")
    end = max(p["end"] for p in plan)
    steps = list(range(tm["now_min"], end + 1, 30))
    in_use = [sum(p["cranes"] for p in plan if p["start"] <= s < p["end"]) for s in steps]
    by = {v["index"]: v for v in vessels}
    return jsonable_encoder({"cranes": world.CRANES, "moves_per_crane_hour": world.CRANE_MOVES_PER_HOUR, "per_vessel": [{"name": by[p["vessel"]]["name"], "berth": world.BERTHS[p["berth"]]["code"], "cranes": p["cranes"], "moves": by[p["vessel"]]["discharge"] + by[p["vessel"]]["load"], "hours": round((p["end"] - p["start"]) / 60, 1), "from": clock(p["start"]), "to": clock(p["end"])} for p in sorted(plan, key=lambda p: p["start"])],
                             "timeline": [{"t": clock(s), "t_min": s, "in_use": n} for s, n in zip(steps, in_use)], "peak_in_use": max(in_use), "utilisation": round(float(np.mean(in_use)) / world.CRANES, 3), "never_over_capacity": max(in_use) <= world.CRANES})


# ---------------------------------------------------------------- yard-optimizer and customs-hold-service

class YardIn(BaseModel):
    holds: list[str] = Field([], max_length=200, description="ISO numbers of containers placed on customs hold")


@app.post("/v1/planning/yard", status_code=201, tags=["planning"], summary="Stack the import boxes so that boxes leaving together share a stack with the latest at the bottom; count rehandles against stacking in discharge order; move held boxes to the hold block")
def plan_yard(body: YardIn, ctx: Ctx = Depends(auth("planning:run")), idem: str | None = IdemKey):
    def work(c):
        tm, w = terminal(c)
        rows = c.execute("SELECT id, ordinal, iso_no, destination_mode, reefer, hazard_code, pickup_after_h FROM containers ORDER BY ordinal").fetchall()
        boxes = [{"index": r["ordinal"], "id": r["id"], "iso_no": r["iso_no"], "mode": r["destination_mode"], "reefer": r["reefer"], "hazard": r["hazard_code"] is not None, "pickup_after_h": float(r["pickup_after_h"])} for r in rows]
        by_iso = {b["iso_no"]: b for b in boxes}
        unknown = [h for h in body.holds if h not in by_iso]
        if unknown:
            raise Problem(404, "container_not_found", unknown[0])
        held = [by_iso[h]["index"] for h in body.holds]
        before, _ = engine.slot(boxes)                                       # the plan as it stood before the holds
        impact = engine.hold_impact(before, held) if held else None
        planned, rehandles = engine.slot(boxes, holds=held)
        _, naive = engine.slot(boxes, planned=False)
        c.execute("DELETE FROM yard_slots")
        c.execute("DELETE FROM container_moves")
        db.load(c, "yard_slots", ["id", "tenant_id", "terminal_id", "block", "bay", "row_no", "tier", "reefer", "hazard_allowed", "container_id"], [(uuid.uuid7(), ctx.tenant_id, tm["id"], a["block"], a["bay"], a["row"], a["tier"], boxes[a["container"]]["reefer"], True, cid(ctx.tenant_id, a["container"])) for a in planned])
        if held:
            c.execute("UPDATE containers SET customs_status = 'hold' WHERE iso_no = ANY(%s)", [body.holds])
            was = {a["container"]: a for a in before}
            db.load(c, "container_moves", ["id", "tenant_id", "container_id", "move_type", "planned_ts", "from_loc", "to_loc"], [(uuid.uuid7(), ctx.tenant_id, cid(ctx.tenant_id, a["container"]), "reslot", ts(tm["now_min"] + 60), f"{was[a['container']]['block']}-{was[a['container']]['bay']}-{was[a['container']]['row']}-{was[a['container']]['tier']}", f"H-{a['bay']}-{a['row']}-{a['tier']}") for a in planned if a["held"]])
        blocks = {}
        for a in planned:
            blocks[a["block"]] = blocks.get(a["block"], 0) + 1
        pid = uuid.uuid7()
        summary = {"containers": len(boxes), "stacks": len({(a["block"], a["bay"], a["row"]) for a in planned}), "rehandles_planned": rehandles, "rehandles_discharge_order": naive, "by_block": blocks, "holds": impact}
        c.execute("INSERT INTO plans (id, tenant_id, terminal_id, kind, summary, created_by) VALUES (%s,%s,%s,'yard',%s,%s)", [pid, ctx.tenant_id, tm["id"], Jsonb(summary), ctx.actor_id])
        audit.record(c, ctx, "yard.planned", "plan", pid, {"containers": len(boxes), "holds": len(held), "rehandles": rehandles})
        return 201, {"plan_id": pid, **summary, "capacity_slots": len(world.BLOCKS) * world.BAYS * world.ROWS * world.TIERS, "how": "boxes with the same mode and six-hour pickup window share a stack, latest pickup at the bottom; held boxes go to block H by the customs office; a rehandle is a box stacked on one that leaves earlier"}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- gate-appointment-service

class GateIn(BaseModel):
    share_of_capacity: float = Field(0.9, ge=0.5, le=1.0)


@app.post("/v1/gate/appointments", status_code=201, tags=["gate"], summary="Spread the day's trucks into appointment slots at a share of gate capacity; the queue before and after")
def gate_appointments(body: GateIn, ctx: Ctx = Depends(auth("gate:write")), idem: str | None = IdemKey):
    def work(c):
        tm, w = terminal(c)
        hours = c.execute("SELECT hour, trucks_expected FROM gate_hours ORDER BY hour").fetchall()
        arrivals = [h["trucks_expected"] for h in hours]
        appts = engine.smooth(arrivals, body.share_of_capacity)
        before, after = engine.gate(arrivals), engine.gate(arrivals, appts)
        db.many(c, "UPDATE gate_hours SET appointments = %s WHERE hour = %s", [(a, h) for h, a in enumerate(appts)])
        moved = sum(max(0, n - a) for n, a in zip(arrivals, appts))
        pid = uuid.uuid7()
        summary = {"trucks": int(sum(arrivals)), "capacity_per_hour": before["capacity_per_hour"], "share_of_capacity": body.share_of_capacity, "trucks_moved_to_a_later_hour": moved, "before": {k: v for k, v in before.items() if k != "rows"}, "after": {k: v for k, v in after.items() if k != "rows"}}
        c.execute("INSERT INTO plans (id, tenant_id, terminal_id, kind, summary, created_by) VALUES (%s,%s,%s,'gate',%s,%s)", [pid, ctx.tenant_id, tm["id"], Jsonb(summary), ctx.actor_id])
        audit.record(c, ctx, "gate.appointments", "plan", pid, {"trucks": summary["trucks"], "mean_wait_before": before["mean_wait_min"], "mean_wait_after": after["mean_wait_min"]})
        return 201, {"plan_id": pid, **summary, "hours": [{"hour": h, "trucks": n, "appointments": a, "queue_before": qb["queue_at_end"], "queue_after": qa["queue_at_end"], "wait_before_min": qb["wait_min"], "wait_after_min": qa["wait_min"]} for h, (n, a, qb, qa) in enumerate(zip(arrivals, appts, before["rows"], after["rows"]))],
                     "how": "a fluid queue hour by hour against four lanes at 2.5 minutes a truck; appointments cap each hour and carry the overflow to the next hours with room"}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- control-tower-api

@app.get("/v1/control-tower/status", tags=["control tower"], summary="The terminal now: vessels and their estimates, the berth plan and its conflicts, cranes, yard, gate")
def status(ctx: Ctx = Depends(auth("terminal:read"))):
    with db.tx(ctx.tenant_id) as c:
        tm, w = terminal(c)
        vessels, est = calls(c)
        plan = current_plan(c)
        yard = c.execute("SELECT summary FROM plans WHERE kind = 'yard' ORDER BY created_at DESC LIMIT 1").fetchone()
        gate = c.execute("SELECT summary FROM plans WHERE kind = 'gate' ORDER BY created_at DESC LIMIT 1").fetchone()
        holds = c.execute("SELECT count(*) AS n FROM containers WHERE customs_status = 'hold'").fetchone()["n"]
        ais = {r["vessel_id"]: r for r in c.execute("SELECT DISTINCT ON (vessel_id) vessel_id, t_min, distance_nm, speed_kn FROM ais_positions ORDER BY vessel_id, t_min DESC")}
    sc = engine.score(plan, vessels, est, tm["now_min"]) if plan else None
    real = world.real_arrivals(w, DAY_SEED, TRUTH_DELAYS)
    truth = {"what": "the day's real arrivals, which the simulator knows and the planner does not", "arrivals": {v["name"]: clock(real[v["index"]]) for v in vessels},
             "current_plan": {k: v_ for k, v_ in engine.score(plan, vessels, real, tm["now_min"]).items() if k != "rows"} if plan else None,
             "first_come_on_schedule": {k: v_ for k, v_ in engine.score(engine.first_come(vessels, {v["index"]: v["eta"] for v in vessels}, tm["now_min"]), vessels, real, tm["now_min"]).items() if k != "rows"}}
    return jsonable_encoder({"clock": clock(tm["now_min"]), "simulation_truth": truth, "now_min": tm["now_min"], "berths": [{"code": b["code"], "length_m": b["length_m"], "depth_m": b["depth_m"]} for b in world.BERTHS], "cranes": world.CRANES,
                             "vessels": [{"name": v["name"], "imo": v["imo"], "status": v["status"], "loa_m": v["loa_m"], "draft_m": v["draft_m"], "priority": v["priority"], "moves": v["discharge"] + v["load"], "schedule_eta": clock(v["eta"]), "expected": clock(est[v["index"]]), "expected_min": est[v["index"]], "late_min": est[v["index"]] - v["eta"],
                                          "ais": {"t": clock(ais[v["id"]]["t_min"]), "distance_nm": float(ais[v["id"]]["distance_nm"]), "speed_kn": float(ais[v["id"]]["speed_kn"])} if v["id"] in ais else None} for v in vessels],
                             "plan": describe(plan, vessels, est) if plan else [], "conflicts": [{**x, "vessels": [vessels[i]["name"] for i in x["vessels"]]} for x in engine.conflicts(plan, est)] if plan else [],
                             "expected": {k: v for k, v in sc.items() if k != "rows"} if sc else None, "yard": yard and yard["summary"], "gate": gate and gate["summary"], "containers_on_hold": holds})


# ---------------------------------------------------------------- scenarios

class DisruptionIn(BaseModel):
    imo: str = Field(pattern=r"^\d{7}$")
    delay_h: float = Field(ge=0.5, le=24)


@app.post("/v1/scenarios/disruption", tags=["scenarios"], summary="What if a vessel were this many hours later: conflicts under the current plan, the re-planned result, and the cost of each")
def disruption(body: DisruptionIn, ctx: Ctx = Depends(auth("scenarios:run"))):
    with db.tx(ctx.tenant_id) as c:
        tm, w = terminal(c)
        vessels, est = calls(c)
        plan = current_plan(c)
    v = next((v for v in vessels if v["imo"] == body.imo), None)
    if not v:
        raise Problem(404, "vessel_not_found")
    if not plan:
        raise Problem(409, "no_plan", "plan the berths first")
    shifted = dict(est)
    shifted[v["index"]] = max(tm["now_min"] + 10, shifted[v["index"]] + int(body.delay_h * 60))
    new, info = engine.plan_berths(vessels, shifted, tm["now_min"])
    keep, re, fc = engine.score(plan, vessels, shifted, tm["now_min"]), engine.score(new, vessels, shifted, tm["now_min"]), engine.score(engine.first_come(vessels, shifted, tm["now_min"]), vessels, shifted, tm["now_min"])
    return jsonable_encoder({"vessel": v["name"], "delay_h": body.delay_h, "expected_arrival_before": clock(est[v["index"]]), "expected_arrival_after": clock(shifted[v["index"]]), "conflicts_under_current_plan": [{**x, "vessels": [vessels[i]["name"] for i in x["vessels"]]} for x in engine.conflicts(plan, shifted)],
                             "rows": [{"name": "Keep the current plan (vessels wait for their berth)", **{k: v_ for k, v_ in keep.items() if k != "rows"}}, {"name": "Re-plan", **{k: v_ for k, v_ in re.items() if k != "rows"}}, {"name": "First come, first served", **{k: v_ for k, v_ in fc.items() if k != "rows"}}],
                             "replanned_allocations": describe(new, vessels, shifted), "solver": info, "applied": False, "note": "a what-if; POST /v1/planning/berths after the AIS shows the delay to make it the plan"})
