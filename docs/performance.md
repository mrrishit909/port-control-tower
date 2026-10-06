# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 5 calls, 2,830 containers, 2,830 yard slots.
Reproduce with `make demo`, then `make load-test`.

## Reads: the control-tower status (twice) and the job list, in rotation, closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 2921 | 292.1 req/s | 3.5 ms | 4.6 ms | 5.2 ms | 0.00% |
| 8 | 4566 | 456.1 req/s | 17.7 ms | 23.1 ms | 27.1 ms | 0.00% |
| 32 | 3404 | 338.7 req/s | 71.6 ms | 84.1 ms | 100.3 ms | 0.00% |

Target from the blueprint: p95 under 500 ms for reads: met. The status view re-scores the current plan on every call.

## Compute (in-process, one core)

| Work | Time |
|---|---|
| Train the arrival model on 320 past calls | 0.2 s |
| Berth and crane plan, five vessels, CP-SAT to optimality | under 0.1 s |
| Stack 2,830 boxes and count rehandles | 0.05 s |
| Gate queue and appointments, 24 hours | under 1 ms |
| Evaluation: three terminals, 18 days | about 2 s |

Not measured: a stream of AIS and TOS events. The blueprint's scale (dozens of calls a day, a yard of tens of thousands of boxes
with live moves) is beyond this slice.
