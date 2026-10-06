"""Measure the ETA model, the berth planner and the yard and gate rules on terminals and days the demo never uses. Writes docs/evaluation.md.

    python -m pc.evaluate
"""
import pathlib

import numpy as np

from . import engine, world

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "evaluation.md"
SEEDS = (101, 102, 103)
DAYS = 6


def table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def main():
    eta_rows, plan_rows, yard_rows, gate_rows = [], [], [], []
    for seed in SEEDS:
        w, m = world.build(seed), engine.eta_model(seed)
        eta_rows.append([seed, m["metrics"]["tested_on"], m["metrics"]["mae_min"], m["metrics"]["mae_distance_over_speed_min"]])
        for d in range(DAYS):
            r = np.random.default_rng([seed, d])
            delays = {int(r.integers(0, 5)): float(r.uniform(2, 5))} if r.random() < 0.7 else {}
            real = world.real_arrivals(w, 50 + d, delays)
            now = world.NOW
            # what the planner knows at 10:00: AIS where there is any, else the schedule
            est = {}
            for v in w["vessels"]:
                track = [p for p in world.ais(w, v, real[v["index"]], 50 + d, now)]
                est[v["index"]] = engine.predict_eta(m, track[-1], v["eta"], 0, 0)[0] if track else (real[v["index"]] if real[v["index"]] <= now else v["eta"])
            plan, _ = engine.plan_berths(w["vessels"], est, now)
            fc = engine.first_come(w["vessels"], est, now)
            perfect, _ = engine.plan_berths(w["vessels"], {v["index"]: real[v["index"]] for v in w["vessels"]}, now)
            rows = {name: engine.score(p, w["vessels"], real, now) for name, p in (("plan", plan), ("first come", fc), ("perfect", perfect))}
            plan_rows.append((seed, d, rows))
            boxes = world.containers(w, 50 + d)
            _, rh = engine.slot(boxes)
            _, rh0 = engine.slot(boxes, planned=False)
            yard_rows.append((len(boxes), rh, rh0))
            trucks = world.truck_arrivals(w, 50 + d)
            gate_rows.append((engine.gate(trucks)["mean_wait_min"], engine.gate(trucks, engine.smooth(trucks))["mean_wait_min"], engine.gate(trucks)["max_queue"], engine.gate(trucks, engine.smooth(trucks))["max_queue"]))
    agg = lambda name, k: int(round(np.mean([rows[name][k] for *_, rows in plan_rows])))          # noqa: E731
    doc = f"""# Evaluation

Everything here is measured on synthetic terminals generated with seeds the demo does not use (`{SEEDS}`), {DAYS} days each, with the clock
at 10:00 and, on most days, one vessel two to five hours late. Reproduce with `python -m pc.evaluate` (under a minute). The numbers say
how well the methods do against the day as the simulator plays it. They are not a claim about a real terminal: real arrivals have
pilots, tides and bunkering in them, crane rates vary by hour, and a yard is never empty when the day begins.

## 1. Arrival estimates

Trained on 320 past calls, tested on 80: minutes to the pilot station from AIS distance and speed, the gap to the schedule and the queue.
The baseline is distance over speed.

{table(["terminal (seed)", "test calls", "error, model (min)", "error, distance / speed (min)"], eta_rows)}

## 2. Berth and crane plans

Plans made at 10:00 on what was known then, scored on the real arrivals. "Perfect" plans with the real arrival times, which nothing had.

{table(["", "waiting (min)", "priority-weighted waiting", "mean turn time (min)", "late beyond window (min)", "crane utilisation"],
       [[n, agg(n, "wait_min"), agg(n, "weighted_wait_min"), agg(n, "mean_turn_min"), agg(n, "late_min"), f"{np.mean([rows[n]['crane_utilisation'] for *_, rows in plan_rows]):.0%}"] for n in ("plan", "first come", "perfect")])}

Means over {len(plan_rows)} days. The planner is told to minimise priority-weighted waiting and, three times as heavily, minutes a vessel finishes
after its window. Against first come, first served it {"cuts" if agg("plan", "weighted_wait_min") < agg("first come", "weighted_wait_min") else "adds"} weighted waiting by {abs(agg("plan", "weighted_wait_min") - agg("first come", "weighted_wait_min"))} minutes a day and
lateness by {abs(agg("plan", "late_min") - agg("first come", "late_min"))}, mostly by giving big ships four cranes and small ones two instead of three each. Against perfect knowledge of
arrivals it loses {agg("plan", "weighted_wait_min") - agg("perfect", "weighted_wait_min")} weighted minutes a day: the price of estimates.

## 3. Yard stacking

{table(["", "mean over days"], [["import boxes a day", int(np.mean([y[0] for y in yard_rows]))], ["rehandles, planned stacking", int(np.mean([y[1] for y in yard_rows]))], ["rehandles, stacking in discharge order", int(np.mean([y[2] for y in yard_rows]))]])}

Zero rehandles is a property of the generator: pickup times are known exactly, so boxes leaving together can be stacked latest-first.
Real pickup times are appointments that slip; the gain would be smaller, and the count is of planned rehandles, not of what happens.

## 4. Truck gate

{table(["", "mean wait (min)", "longest queue (trucks)"], [["without appointments", f"{np.mean([g[0] for g in gate_rows]):.1f}", int(np.mean([g[2] for g in gate_rows]))], ["appointments at 90% of capacity", f"{np.mean([g[1] for g in gate_rows]):.1f}", int(np.mean([g[3] for g in gate_rows]))]])}

Appointments do not make the gate faster; they move trucks to hours with room. The fluid queue has no randomness in it, so the
"after" figures are the best case for a gate that runs to its appointments.
"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(doc)
    print(doc)


if __name__ == "__main__":
    main()
