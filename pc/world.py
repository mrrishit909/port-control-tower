"""An invented container terminal: three berths, nine quay cranes, a yard of six blocks, a truck gate, and a day with five vessel
calls. Each vessel has a schedule ETA and a hidden real arrival; its AIS track approaches the port. Containers to discharge have
a destination mode and a pickup time; some get a customs hold. Everything is a pure function of a seed. Time in minutes from 00:00."""
import functools

import numpy as np

HOURS = 36
NOW = 10 * 60
BERTHS = [{"index": 0, "code": "B1", "length_m": 300, "depth_m": 13.5}, {"index": 1, "code": "B2", "length_m": 360, "depth_m": 15.0}, {"index": 2, "code": "B3", "length_m": 400, "depth_m": 16.5}]
CRANES = 9
CRANE_MOVES_PER_HOUR = 28
MIN_CRANES, MAX_CRANES = 2, 4
BLOCKS = ["A", "B", "C", "D", "E", "H"]           # H is the hold block, by the customs office
BAYS, ROWS, TIERS = 40, 6, 4
GATE_LANES, GATE_MIN_PER_TRUCK = 4, 2.5           # four lanes, 2.5 minutes a truck: 96 trucks an hour
PORT_NM = 60                                      # the AIS record starts when a vessel is this far out
DELAY_SHIP, DELAY_H = 0, 4                        # the demo's disruption


@functools.lru_cache(maxsize=8)
def build(seed=17):
    r = np.random.default_rng(seed)
    vessels = []
    for k in range(5):
        loa = float(r.choice([230, 260, 294, 334, 366]))
        vessels.append({"index": k, "name": ["MSC Riverbend", "Ever Summit", "Maersk Prairie", "CMA Delta", "ONE Granite"][k], "imo": f"{9400000 + 137 * k}", "loa_m": loa, "draft_m": round(float(min(15.4, 10.5 + (loa - 230) / 136 * 5.0 + r.uniform(-0.5, 0.5))), 1),
                        "eta": int(420 + k * 170 + r.uniform(-30, 30)), "window_h": 20, "discharge": int(loa * r.uniform(1.6, 2.4)), "load": int(loa * r.uniform(1.2, 2.0)), "service": ["Asia-Europe", "Transpacific", "Feeder", "Asia-Europe", "Transatlantic"][k], "priority": [3, 2, 1, 3, 2][k]})
    return {"seed": seed, "vessels": vessels}


def real_arrivals(w, seed, delays=None):
    """When each vessel really reaches the pilot station: schedule plus weather and congestion, plus any demo delay."""
    r = np.random.default_rng([w["seed"], seed])
    out = []
    for v in w["vessels"]:
        late = float(r.normal(0, 25) + (r.exponential(60) if r.random() < 0.3 else 0))
        out.append(int(v["eta"] + late + (delays or {}).get(v["index"], 0) * 60))
    return out


def ais(w, v, arrival, seed, upto):
    """The vessel's AIS track to `upto`: distance to port (nm) and speed (knots) every 10 minutes, consistent with arriving at `arrival`."""
    r = np.random.default_rng([w["seed"], v["index"], seed])
    start = arrival - int(PORT_NM / 14 * 60)                        # about 4.3 hours out at 14 knots
    ts = list(range(start, min(upto, arrival) + 1, 10))
    speed = 14 + r.normal(0, 0.8, len(ts)).cumsum() * 0.2
    speed = np.clip(speed, 8, 19)
    dist = PORT_NM - np.cumsum(np.r_[0, speed[:-1]] * 10 / 60)
    return [{"t": t, "distance_nm": round(float(max(0, d)), 2), "speed_kn": round(float(s), 1)} for t, d, s in zip(ts, dist, speed)]


def history(w, seed, n=400):
    """Past calls: the schedule ETA, what AIS showed with some hours to go, and the real arrival, for the ETA model."""
    r = np.random.default_rng([w["seed"], seed, 3])
    rows = []
    for k in range(n):
        eta = 1000.0
        late = float(r.normal(0, 25) + (r.exponential(60) if r.random() < 0.3 else 0))
        arrival = eta + late
        lead = float(r.uniform(60, 250))                                 # minutes before the real arrival the position was taken
        dist = lead / 60 * float(r.uniform(12, 16))
        speed = float(np.clip(dist / (lead / 60) + r.normal(0, 0.7), 8, 19))
        queue = int(r.integers(0, 3))
        rows.append({"schedule_gap_min": eta - (arrival - lead), "distance_nm": dist, "speed_kn": speed, "vessels_waiting": queue, "service": int(r.integers(0, 4)), "remaining_min": lead + queue * 20 + float(r.normal(0, 6))})
    return rows


def containers(w, seed):
    """The import boxes each vessel discharges: a mode (truck or rail), a pickup time after discharge, and a few hazards and reefers."""
    r = np.random.default_rng([w["seed"], seed, 5])
    out = []
    for v in w["vessels"]:
        for i in range(v["discharge"]):
            mode = "rail" if r.random() < 0.3 else "truck"
            out.append({"index": len(out), "vessel": v["index"], "iso_no": f"RBCU{len(out) + 1000000:07d}", "size": "40" if r.random() < 0.6 else "20", "weight_kg": int(r.uniform(4000, 30000)), "mode": mode,
                        "reefer": r.random() < 0.08, "hazard": r.random() < 0.04, "pickup_after_h": float(r.uniform(6, 60)) if mode == "truck" else float(r.choice([12, 24, 36]))})
    return out


def truck_arrivals(w, seed):
    """Trucks per hour at the gate over the day without appointments: a morning and an afternoon peak."""
    r = np.random.default_rng([w["seed"], seed, 9])
    base = np.array([10, 8, 6, 6, 8, 20, 60, 110, 140, 150, 130, 100, 90, 120, 145, 150, 120, 80, 50, 30, 20, 15, 12, 10], dtype=float)
    return np.maximum(0, base + r.normal(0, 8, 24)).round().astype(int)
