"""Unit tests for the simulator, ETA model, berth planner, yard slotting and gate queue: no database."""
import numpy as np

from pc import engine, world

W = world.build()
NOW = world.NOW
REAL = world.real_arrivals(W, 1)


def test_world_is_deterministic_and_plausible():
    assert world.real_arrivals(W, 1) == REAL and all(abs(r - v["eta"]) < 400 for r, v in zip(REAL, W["vessels"]))
    assert world.real_arrivals(W, 1, {0: 4})[0] == REAL[0] + 240
    for v in W["vessels"]:
        assert any(engine.fits(v, b) for b in world.BERTHS)
    assert not engine.fits(W["vessels"][1], world.BERTHS[0])                                         # the biggest ship does not fit the smallest berth
    track = world.ais(W, W["vessels"][2], REAL[2], 1, REAL[2])
    assert track[0]["distance_nm"] == world.PORT_NM and track[-1]["distance_nm"] < 3 and all(8 <= p["speed_kn"] <= 19 for p in track)


def test_eta_model_beats_distance_over_speed():
    m = engine.eta_model(17)
    assert m["metrics"]["mae_min"] < m["metrics"]["mae_distance_over_speed_min"]
    pred, naive = engine.predict_eta(m, {"t": 600, "distance_nm": 60.0, "speed_kn": 14.0}, 423, 0, 0)
    assert 600 + 180 < pred < 600 + 330 and abs(naive - (600 + 60 / 14 * 60)) <= 1


def test_plan_is_feasible_and_no_worse_than_first_come():
    est = {v["index"]: v["eta"] for v in W["vessels"]}
    plan, info = engine.plan_berths(W["vessels"], est, NOW)
    assert info["status"] in ("OPTIMAL", "FEASIBLE") and len(plan) == 5
    for a in plan:
        v = W["vessels"][a["vessel"]]
        assert engine.fits(v, world.BERTHS[a["berth"]]) and world.MIN_CRANES <= a["cranes"] <= world.MAX_CRANES and a["start"] >= max(NOW, est[a["vessel"]])
        assert a["end"] - a["start"] == engine.duration_min(v["discharge"] + v["load"], a["cranes"])
    for b in range(3):
        mine = sorted((a for a in plan if a["berth"] == b), key=lambda a: a["start"])
        assert all(y["start"] >= x["end"] for x, y in zip(mine, mine[1:]))
    for t in range(NOW, max(a["end"] for a in plan), 10):
        assert sum(a["cranes"] for a in plan if a["start"] <= t < a["end"]) <= world.CRANES
    simple = engine.first_come(W["vessels"], est, NOW)
    mine, theirs = engine.score(plan, W["vessels"], est, NOW), engine.score(simple, W["vessels"], est, NOW)
    assert engine.WAIT_WEIGHT * mine["weighted_wait_min"] + engine.LATE_WEIGHT * sum(r["late_min"] * r["priority"] for r in mine["rows"]) <= engine.WAIT_WEIGHT * theirs["weighted_wait_min"] + engine.LATE_WEIGHT * sum(r["late_min"] * r["priority"] for r in theirs["rows"])
    locked, _ = engine.plan_berths(W["vessels"], est, NOW, locks={2: 0})
    assert next(a for a in locked if a["vessel"] == 2)["berth"] == 0


def test_conflicts_appear_when_a_ship_is_late():
    est = {v["index"]: v["eta"] for v in W["vessels"]}
    plan, _ = engine.plan_berths(W["vessels"], est, NOW)
    assert engine.conflicts(plan, est) == []
    late = dict(est)
    late[0] += 400
    assert any(0 in c["vessels"] for c in engine.conflicts(plan, late)) or engine.score(plan, W["vessels"], late, NOW)["wait_min"] > engine.score(plan, W["vessels"], est, NOW)["wait_min"]


def test_yard_slotting_counts_rehandles_and_respects_tiers():
    boxes = world.containers(W, 1)
    planned, rh = engine.slot(boxes)
    naive, rh_naive = engine.slot(boxes, planned=False)
    assert rh == 0 < rh_naive and len(planned) == len(boxes) and max(a["tier"] for a in planned) <= world.TIERS
    for out in (planned, naive):
        stacks = {}
        for a in out:
            stacks.setdefault((a["block"], a["bay"], a["row"]), []).append(a["tier"])
        assert all(sorted(t) == list(range(1, len(t) + 1)) for t in stacks.values())              # every stack is filled from the ground up, no gaps
    holds = [b["index"] for b in boxes[:30]]
    held, _ = engine.slot(boxes, holds=holds)
    assert all(a["block"] == "H" for a in held if a["held"]) and sum(a["held"] for a in held) == 30
    impact = engine.hold_impact(planned, holds)
    assert impact["moves_to_reslot"] == 30 and impact["rehandles_if_left_in_place"] >= 0


def test_gate_queue_and_smoothing():
    trucks = world.truck_arrivals(W, 1)
    before = engine.gate(trucks)
    appts = engine.smooth(trucks)
    after = engine.gate(trucks, appts)
    assert sum(appts) == trucks.sum() and max(appts[:-1]) <= before["capacity_per_hour"] and after["mean_wait_min"] < before["mean_wait_min"] and after["max_queue"] < before["max_queue"]
    assert engine.gate(np.zeros(24, int))["mean_wait_min"] == 0
