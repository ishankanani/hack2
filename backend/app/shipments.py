"""
Shipment lifecycle + persistence. The provided data is aggregate daily operations,
not per-shipment records, so managers CREATE shipments here; each is planned,
costed, risk-scored and scheduled with a real feasibility check. Persisted to a
JSON store (STORE_DIR) so it survives restarts.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from .cost import journey_cost, cost_per_kg
from .eta import compute_journey
from .risk import assess

STORE_DIR = os.environ.get("STORE_DIR", os.path.join(os.getcwd(), "store"))
STORE = os.path.join(STORE_DIR, "shipments.json")
KG_PER_LDM = 1750.0


def _load():
    try:
        with open(STORE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(d):
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(STORE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, default=str)


def _ldm(weight_kg):
    return max(0.5, round((weight_kg or 0) / KG_PER_LDM, 1))


def plan(ctx, origin, destination, depart_utc, weight_kg, required_delivery, value_eur):
    """Compute route options (via-hub + direct estimate) with ETA, cost, fuel, risk."""
    net = ctx["network"]
    ldm = _ldm(weight_kg)
    options = []
    j = compute_journey(net, ctx["holidays"], ctx["transfer"], ctx["hub_delays"],
                        ctx["providers"], origin, destination, depart_utc)
    for jr, label, kind in [(j, "Via Heilbronn hub", "scheduled"),
                            (ctx["direct_fn"](origin, destination, depart_utc), "Direct estimate (no scheduled lane)", "estimate")]:
        c = journey_cost(net, jr["path"], ldm)
        lane_hist = [ctx["history"][net["nodes"][p].get("relation")]
                     for p in jr["path"] if net["nodes"][p].get("relation") in ctx["history"]]
        rk = assess(jr, required_delivery, value_eur, lane_hist)
        options.append({**jr, "label": label, "kind": kind,
                        "cost": {"transport_eur": c["transport_eur"], "fuel_l": c["fuel_l"],
                                 "fuel_eur": c["fuel_eur"], "distance_km": c["km"],
                                 "cost_per_kg": cost_per_kg(c["transport_eur"], weight_kg)},
                        "risk": rk, "ldm": ldm,
                        "historical": _hist_summary(lane_hist)})
    options.sort(key=lambda o: o["eta"])
    return options


def _hist_summary(lane_hist):
    if not lane_hist:
        return None
    n = len(lane_hist)
    return {
        "avg_cost_eur": round(sum(h["avg_daily_cost_eur"] for h in lane_hist) / n),
        "avg_fuel_l": round(sum(h["avg_daily_fuel_l"] for h in lane_hist) / n),
        "reliability_pct": round(sum(h["reliability_pct"] for h in lane_hist) / n, 1),
        "samples": sum(h["samples"] for h in lane_hist),
    }


def savings_vs_baseline(recommended, baseline):
    """Estimated savings of recommended vs a baseline option (not guaranteed)."""
    return {
        "money_eur": baseline["cost"]["transport_eur"] - recommended["cost"]["transport_eur"],
        "fuel_l": baseline["cost"]["fuel_l"] - recommended["cost"]["fuel_l"],
        "time_min": baseline["total_minutes"] - recommended["total_minutes"],
        "note": "Estimated, not guaranteed — based on tariffs (relationen.csv) and current plan.",
    }


class Shipments:
    def __init__(self, ctx):
        self.ctx = ctx
        self.data = _load()

    def seed_if_empty(self):
        if self.data:
            return
        net = self.ctx["network"]
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        samples = [
            ("R01", "R11", 8200, 42000, base + timedelta(hours=2), base + timedelta(days=2, hours=9), "Top"),
            ("R11", "R24", 15400, 180000, base + timedelta(hours=6), base + timedelta(days=2), "Top"),
            ("R08", "R03", 3100, 12000, base + timedelta(days=1), base + timedelta(days=3), "Standard"),
            ("R16", "R21", 21000, 810000, _next_friday(base, 16), _next_monday(base, 9), "Top"),
        ]
        for o, d, w, v, dep, req, seg in samples:
            self.create({"origin": o, "destination": d, "weight_kg": w, "value_eur": v,
                         "planned_departure": dep.isoformat(), "required_delivery": req.isoformat(),
                         "customer_segment": seg, "container": f"C{uuid.uuid4().hex[:5].upper()}"})

    def create(self, p):
        sid = "SHP-" + uuid.uuid4().hex[:6].upper()
        dep = datetime.fromisoformat(p["planned_departure"].replace("Z", "+00:00")).astimezone(timezone.utc)
        opts = plan(self.ctx, p["origin"], p["destination"], dep, p.get("weight_kg", 0),
                    p.get("required_delivery"), p.get("value_eur", 0))
        rec = opts[0]
        ship = {
            "id": sid, "origin": p["origin"], "destination": p["destination"],
            "current_location": p["origin"], "route": rec["path"],
            "next_hub": rec["path"][1] if len(rec["path"]) > 1 else None,
            "status": "PLANNED",
            "planned_departure": p["planned_departure"], "scheduled_departure": None,
            "current_eta": rec["eta"], "required_delivery": p.get("required_delivery"),
            "distance_km": rec["cost"]["distance_km"], "weight_kg": p.get("weight_kg", 0),
            "container": p.get("container"), "value_eur": p.get("value_eur", 0),
            "est_cost_eur": rec["cost"]["transport_eur"], "est_fuel_l": rec["cost"]["fuel_l"],
            "cost_per_kg": rec["cost"]["cost_per_kg"], "risk_level": rec["risk"]["level"],
            "customer_segment": p.get("customer_segment"),
            "alert": None if rec["risk"]["deadline_ok"] else "Delivery deadline at risk",
            "options": opts, "created_at": datetime.now(timezone.utc).isoformat(),
        }
        ship["delay_minutes"] = _delay(rec["eta"], p.get("required_delivery"))
        self.data[sid] = ship
        _save(self.data)
        return ship

    def schedule(self, sid, option_index=0):
        s = self.data.get(sid)
        if not s:
            return None, "unknown shipment"
        opt = s["options"][option_index]
        if not opt["risk"]["deadline_ok"]:
            # feasibility warning BEFORE committing
            return s, f"WARNING: selected route misses delivery window (ETA {opt['eta']}). Schedule anyway or pick another route."
        s.update({"status": "SCHEDULED", "scheduled_departure": s["planned_departure"],
                  "route": opt["path"], "current_eta": opt["eta"],
                  "est_cost_eur": opt["cost"]["transport_eur"], "est_fuel_l": opt["cost"]["fuel_l"],
                  "risk_level": opt["risk"]["level"]})
        _save(self.data)
        return s, None

    def set_status(self, sid, status):
        s = self.data.get(sid)
        if s:
            s["status"] = status; _save(self.data)
        return s

    def list(self):
        return sorted(self.data.values(), key=lambda x: x["created_at"], reverse=True)

    def get(self, sid):
        return self.data.get(sid)


def _delay(eta, required):
    if not required:
        return 0
    e = datetime.fromisoformat(eta); r = datetime.fromisoformat(required.replace("Z", "+00:00"))
    return max(0, int((e - r).total_seconds() // 60))


def _next_friday(base, hour):
    d = base
    while d.isoweekday() != 5:
        d += timedelta(days=1)
    return d.replace(hour=hour)


def _next_monday(base, hour):
    d = base + timedelta(days=1)
    while d.isoweekday() != 1:
        d += timedelta(days=1)
    return d.replace(hour=hour)
