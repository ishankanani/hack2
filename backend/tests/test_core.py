import os, shutil
os.environ.setdefault("DATA_DIR", "/mnt/user-data/uploads")
os.environ["STORE_DIR"] = "/tmp/dachser_test_store"
shutil.rmtree("/tmp/dachser_test_store", ignore_errors=True)

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from app.main import app
from app.restrictions import drive_with_restrictions

BERLIN = ZoneInfo("Europe/Berlin")


# --- restriction engine (dynamic legal waiting) ---
def test_restriction_sunday_waiting_is_dynamic():
    dep = datetime(2026, 9, 19, 20, 0, tzinfo=BERLIN).astimezone(timezone.utc)
    r = drive_with_restrictions(dep, 360, {})
    assert r["waiting_minutes"] == 22 * 60
    assert r["arrival"].astimezone(BERLIN).strftime("%a %H:%M") == "Mon 00:00"


def test_restriction_holiday_and_weekday():
    r = drive_with_restrictions(datetime(2026, 10, 5, 6, 0, tzinfo=BERLIN).astimezone(timezone.utc), 180, {"2026-10-05": "H"})
    assert r["waiting_minutes"] == 16 * 60
    r2 = drive_with_restrictions(datetime(2026, 9, 22, 8, 0, tzinfo=BERLIN).astimezone(timezone.utc), 300, {})
    assert r2["waiting_minutes"] == 0


# --- real network + analytics ---
def test_network_real_and_complete():
    with TestClient(app) as c:
        net = c.get("/api/network").json()
        assert net["hub_id"] == "HN" and len(net["nodes"]) == 31
        assert {"Hamburg", "München", "Münster", "Heilbronn"} <= {n["name"] for n in net["nodes"]}


def test_analytics_from_history():
    with TestClient(app) as c:
        rows = c.get("/api/analytics/relations").json()["relations"]
        assert len(rows) == 30
        top = rows[0]
        assert top["samples"] > 100 and 0 <= top["spillover_rate"] <= 1 and top["avg_daily_cost_eur"] > 0


def test_disruptions_are_historical():
    with TestClient(app) as c:
        d = c.get("/api/disruptions").json()
        assert len(d["historical"]) == 7
        assert all(x["source"].endswith("(historical)") for x in d["historical"])


# --- providers honesty ---
def test_providers_honest():
    with TestClient(app) as c:
        ps = {p["key"]: p["status"] for p in c.get("/api/providers").json()["providers"]}
        assert ps["traffic"] == "NOT_CONFIGURED" and ps["holidays"] == "LIVE"


# --- shipment lifecycle ---
def test_shipments_seed_and_high_value_risk():
    with TestClient(app) as c:
        ships = c.get("/api/shipments").json()["shipments"]
        assert len(ships) >= 4
        hv = [s for s in ships if s["value_eur"] >= 800000]
        assert hv and hv[0]["risk_level"] == "HIGH" and hv[0]["alert"]


def test_schedule_feasibility_warning():
    with TestClient(app) as c:
        risky = next(s for s in c.get("/api/shipments").json()["shipments"] if s["value_eur"] >= 800000)
        res = c.post(f"/api/shipments/{risky['id']}/schedule?option=0").json()
        assert res["scheduled"] is False and "misses delivery window" in res["warning"]
        forced = c.post(f"/api/shipments/{risky['id']}/schedule?option=0&force=true").json()
        assert forced["scheduled"] is True


def test_create_costs_and_cpk():
    with TestClient(app) as c:
        s = c.post("/api/shipments", json={"origin": "R08", "destination": "R14", "weight_kg": 5000,
                                           "value_eur": 30000, "planned_departure": "2026-09-22T06:00:00Z",
                                           "required_delivery": "2026-09-24T12:00:00Z"}).json()
        assert s["est_cost_eur"] > 0 and s["est_fuel_l"] > 0 and s["cost_per_kg"] > 0


def test_dashboard_and_savings():
    with TestClient(app) as c:
        dash = c.get("/api/dashboard").json()
        assert dash["total"] >= 4 and dash["high_value"] >= 1 and dash["next_holiday"]
        sv = c.get("/api/savings").json()
        assert "not guaranteed" in sv["note"]
