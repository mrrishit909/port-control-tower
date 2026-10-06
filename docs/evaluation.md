# Evaluation

Everything here is measured on synthetic terminals generated with seeds the demo does not use (`(101, 102, 103)`), 6 days each, with the clock
at 10:00 and, on most days, one vessel two to five hours late. Reproduce with `python -m pc.evaluate` (under a minute). The numbers say
how well the methods do against the day as the simulator plays it. They are not a claim about a real terminal: real arrivals have
pilots, tides and bunkering in them, crane rates vary by hour, and a yard is never empty when the day begins.

## 1. Arrival estimates

Trained on 320 past calls, tested on 80: minutes to the pilot station from AIS distance and speed, the gap to the schedule and the queue.
The baseline is distance over speed.

| terminal (seed) | test calls | error, model (min) | error, distance / speed (min) |
|---|---|---|---|
| 101 | 80 | 8.2 | 23.2 |
| 102 | 80 | 8.6 | 25.2 |
| 103 | 80 | 7.7 | 22.2 |

## 2. Berth and crane plans

Plans made at 10:00 on what was known then, scored on the real arrivals. "Perfect" plans with the real arrival times, which nothing had.

|  | waiting (min) | priority-weighted waiting | mean turn time (min) | late beyond window (min) | crane utilisation |
|---|---|---|---|---|---|
| plan | 958 | 1824 | 947 | 48 | 81% |
| first come | 1217 | 2623 | 999 | 324 | 69% |
| perfect | 897 | 1721 | 927 | 27 | 83% |

Means over 18 days. The planner is told to minimise priority-weighted waiting and, three times as heavily, minutes a vessel finishes
after its window. Against first come, first served it cuts weighted waiting by 799 minutes a day and
lateness by 276, mostly by giving big ships four cranes and small ones two instead of three each. Against perfect knowledge of
arrivals it loses 103 weighted minutes a day: the price of estimates.

## 3. Yard stacking

|  | mean over days |
|---|---|
| import boxes a day | 2863 |
| rehandles, planned stacking | 0 |
| rehandles, stacking in discharge order | 2074 |

Zero rehandles is a property of the generator: pickup times are known exactly, so boxes leaving together can be stacked latest-first.
Real pickup times are appointments that slip; the gain would be smaller, and the count is of planned rehandles, not of what happens.

## 4. Truck gate

|  | mean wait (min) | longest queue (trucks) |
|---|---|---|
| without appointments | 93.5 | 290 |
| appointments at 90% of capacity | 0.4 | 8 |

Appointments do not make the gate faster; they move trucks to hours with room. The fluid queue has no randomness in it, so the
"after" figures are the best case for a gate that runs to its appointments.
