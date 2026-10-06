"""Integration, end-to-end and security tests against a real Postgres."""
import uuid

import pytest

from core import db, jobs, scenario
from pc import world

from .conftest import bearer

MANAGER, PLANNER, VIEWER, OTHER = bearer("terminal", "manager"), bearer("terminal", "planner"), bearer("terminal", "viewer"), bearer("other", "manager")
IMO0 = world.build()["vessels"][0]["imo"]


def test_nothing_works_before_a_terminal_is_loaded(client):
    assert client.get("/v1/control-tower/status", headers=VIEWER).json()["code"] == "no_terminal" and client.post("/v1/planning/berths", headers=PLANNER, json={}).json()["code"] == "no_terminal"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=(), tenant=None):
    with db.tx(tenant) as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    load = demo["load"]["result"]
    assert len(load["vessels"]) == 5 and load["containers_to_discharge"] == one("SELECT count(*) AS n FROM containers")["n"] and load["eta_model"]["mae_min"] < load["eta_model"]["mae_distance_over_speed_min"]
    p1 = demo["plan1"]
    assert p1["solver"]["status"] == "OPTIMAL" and len(p1["allocations"]) == 5 and p1["conflicts"] == [] and one("SELECT count(*) AS n FROM berth_allocations")["n"] == 5
    a = demo["ais"]
    assert a["stored"] == 1 and a["duplicates_ignored"] == 1 and a["late_min"] > 300 and len(a["berth_conflicts_under_current_plan"]) >= 1 and "MSC Riverbend" in a["berth_conflicts_under_current_plan"][0]["vessels"]
    s1 = demo["status1"]
    assert s1["conflicts"] and s1["vessels"][0]["expected"] == a["predicted_eta"] and s1["vessels"][1]["status"] == "arrived"
    w = demo["whatif"]
    assert w["applied"] is False and [r["name"][:4] for r in w["rows"]] == ["Keep", "Re-p", "Firs"] and w["rows"][1]["late_min"] <= w["rows"][0]["late_min"]
    p2 = demo["plan2"]
    assert p2["conflicts"] == [] and p2["expected"]["optimised"]["late_min"] <= p2["expected"]["first_come"]["late_min"] and one("SELECT count(*) AS n FROM plans WHERE kind = 'berths'")["n"] == 2
    c = demo["cranes"]
    assert c["never_over_capacity"] and c["peak_in_use"] <= 9 and len(c["per_vessel"]) == 5
    y1, y2 = demo["yard1"], demo["yard2"]
    assert y1["rehandles_planned"] == 0 < y1["rehandles_discharge_order"] and y1["holds"] is None and y2["holds"]["held"] == 40 and y2["by_block"]["H"] == 40 and one("SELECT count(*) AS n FROM containers WHERE customs_status = 'hold'")["n"] == 40
    assert one("SELECT count(*) AS n FROM container_moves WHERE move_type = 'reslot'")["n"] == 40 and one("SELECT count(*) AS n FROM yard_slots")["n"] == load["containers_to_discharge"]
    g = demo["gate"]
    assert g["trucks"] == 1610 and g["after"]["mean_wait_min"] < g["before"]["mean_wait_min"] and sum(h["appointments"] for h in g["hours"]) == g["trucks"]
    s2 = demo["status2"]
    t = s2["simulation_truth"]
    assert s2["containers_on_hold"] == 40 and s2["gate"]["after"]["mean_wait_min"] == g["after"]["mean_wait_min"] and t["current_plan"]["late_min"] <= demo["status1"]["simulation_truth"]["current_plan"]["late_min"]
    assert demo["audit"]["chain_valid"] and {"terminal.loaded", "berths.planned", "ais.received", "yard.planned", "gate.appointments"} <= {e["action"] for e in demo["audit"]["events"]}


def test_ais_validation(client, demo):
    post = lambda **kw: client.post("/v1/ais/positions", headers=PLANNER, json={"imo": world.build()["vessels"][3]["imo"], "positions": [{"t": "2026-10-05T09:50:00Z", "distance_nm": 40, "speed_kn": 13, **kw}]})     # noqa: E731
    assert post().json()["stored"] == 1 and post().json()["duplicates_ignored"] == 1 and post(t="2026-10-05T09:50:00").json()["code"] == "timezone_required" and post(t="2026-10-05T23:00:00Z").json()["code"] == "out_of_window"
    assert client.post("/v1/ais/positions", headers=PLANNER, json={"imo": "1111111", "positions": [{"t": "2026-10-05T09:50:00Z", "distance_nm": 40, "speed_kn": 13}]}).json()["code"] == "vessel_not_found"
    assert client.post("/v1/ais/positions", headers=PLANNER, json={"imo": world.build()["vessels"][1]["imo"], "positions": [{"t": "2026-10-05T09:50:00Z", "distance_nm": 1, "speed_kn": 3}]}).json()["code"] == "already_arrived"
    assert post(speed_kn=99).status_code == 422


def test_planning_validation(client, demo):
    assert client.post("/v1/planning/berths", headers=PLANNER, json={"locks": [{"imo": world.build()["vessels"][1]["imo"], "berth": "B1"}]}).json()["code"] == "vessel_does_not_fit"
    assert client.post("/v1/planning/berths", headers=PLANNER, json={"locks": [{"imo": "1111111", "berth": "B1"}]}).json()["code"] == "vessel_not_found"
    locked = client.post("/v1/planning/berths", headers=PLANNER, json={"locks": [{"imo": world.build()["vessels"][2]["imo"], "berth": "B1"}]}).json()
    assert next(a for a in locked["allocations"] if a["name"] == "Maersk Prairie")["berth"] == "B1" and next(a for a in locked["allocations"] if a["name"] == "Maersk Prairie")["locked"]
    assert client.post("/v1/planning/yard", headers=PLANNER, json={"holds": ["NOPE0000000"]}).json()["code"] == "container_not_found"
    assert client.post("/v1/scenarios/disruption", headers=PLANNER, json={"imo": "1111111", "delay_h": 2}).status_code == 404 and client.post("/v1/gate/appointments", headers=PLANNER, json={"share_of_capacity": 0.1}).status_code == 422


def test_roles(client, demo):
    assert client.get("/v1/control-tower/status").status_code == 401 and client.get("/v1/control-tower/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    for path, payload in (("/v1/ais/positions", {}), ("/v1/planning/berths", {}), ("/v1/planning/yard", {}), ("/v1/gate/appointments", {}), ("/v1/scenarios/disruption", {})):
        assert client.post(path, headers=VIEWER, json=payload).status_code == 403, path
    assert client.post("/v1/terminal:load", headers=PLANNER, json={}).status_code == 403 and client.get("/v1/audit", headers=PLANNER).status_code == 403 and client.get("/v1/audit", headers=MANAGER).status_code == 200


def test_another_terminal_sees_nothing(client, demo, seeded):
    assert client.get("/v1/control-tower/status", headers=OTHER).json()["code"] == "no_terminal"
    for table in ("terminals", "vessels", "port_calls", "ais_positions", "berths", "berth_allocations", "containers", "yard_slots", "container_moves", "gate_hours", "plans"):
        assert one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["other"])["n"] == 0 < one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["terminal"])["n"], table
    with pytest.raises(Exception, match="row-level security"), db.tx(seeded["other"]) as c:
        c.execute("INSERT INTO terminals (id, tenant_id, name, model_seed, day, now_min) VALUES (%s,%s,'x',1,'2026-10-05',0)", [uuid.uuid4(), seeded["terminal"]])


def test_idempotent_writes(client, demo):
    again = client.post("/v1/gate/appointments", headers={**PLANNER, "Idempotency-Key": "demo-1:gate"}, json={"share_of_capacity": 0.9})
    assert again.status_code == 201 and again.headers["Idempotent-Replay"] == "true" and again.json()["plan_id"] == demo["gate"]["plan_id"]
    assert client.post("/v1/gate/appointments", headers={**PLANNER, "Idempotency-Key": "demo-1:gate"}, json={"share_of_capacity": 0.8}).json()["code"] == "idempotency_key_reused"
    assert client.post("/v1/terminal:load", headers=MANAGER, json={"seed": 17}).json()["code"] == "terminal_exists"
