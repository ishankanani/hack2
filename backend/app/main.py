"""
DACHSER Live Transit Planner — API.
Real network + holidays + historical analytics; dynamic restriction engine; ETA,
cost, fuel, risk, savings engines; shipment lifecycle; live providers (honest
status); manager hub delays; audit. See README for the section-by-section map.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import analytics as an
from . import data as dataloader
from .cost import journey_cost, cost_per_kg
from .eta import compute_journey, _haversine_km
from .providers import Providers
from .restrictions import drive_with_restrictions
from .risk import assess
from .shipments import Shipments, plan as plan_options, savings_vs_baseline

DATA_DIR = os.environ.get("DATA_DIR", "")
S = {}


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _audit(kind, detail, source="system"):
    S["audit"].insert(0, {"at": _now_iso(), "type": kind, "detail": detail, "source": source})
    S["audit"] = S["audit"][:300]


def _direct_estimate(origin, dest, depart):
    net = S["network"]; na, nb = net["nodes"][origin], net["nodes"][dest]
    km = round(_haversine_km((na["lat"], na["lon"]), (nb["lat"], nb["lon"])) * 1.25)
    drive = round(km / 65 * 60)
    cur = depart; steps = []; h = 120
    steps.append({"type": "handling", "location": origin, "location_name": na["name"],
                  "start": cur.isoformat(), "end": (cur + timedelta(minutes=h)).isoformat(),
                  "minutes": h, "detail": "Loading & handling (proxy)", "source": "proxy"})
    cur += timedelta(minutes=h)
    leg = drive_with_restrictions(cur, drive, S["holidays"])
    t = cur; driven = 0
    for p in leg["pauses"]:
        before = int((p["start"] - t).total_seconds() // 60)
        if before > 0:
            steps.append({"type": "drive", "location": origin, "location_name": na["name"], "start": t.isoformat(),
                          "end": p["start"].isoformat(), "minutes": before, "detail": f"Direct road {na['name']} → {nb['name']} ({km} km, estimated)", "source": "estimated"})
            driven += before
        steps.append({"type": "legal_wait", "location": origin, "location_name": na["name"], "start": p["start"].isoformat(),
                      "end": p["end"].isoformat(), "minutes": p["minutes"], "detail": p["reason"], "source": "REGULATORY DATA"})
        t = p["end"]
    rest = drive - driven
    if rest > 0:
        steps.append({"type": "drive", "location": origin, "location_name": na["name"], "start": t.isoformat(),
                      "end": leg["arrival"].isoformat(), "minutes": rest, "detail": f"Direct road {na['name']} → {nb['name']} ({km} km, estimated)", "source": "estimated"})
    cur = leg["arrival"]
    return {"origin": origin, "destination": dest, "path": [origin, dest], "depart_at": depart.isoformat(),
            "eta": cur.isoformat(), "total_minutes": int((cur - depart).total_seconds() // 60),
            "components": {"transport": drive, "transfer": h, "hub_delay": 0, "traffic": 0, "legal_wait": leg["waiting_minutes"]},
            "steps": steps, "weather_notes": [], "data_sources": {"transport": "estimated (haversine × 1.25)", "legal_wait": "REGULATORY DATA"}}


def _geometry(path):
    """Real road geometry via OSRM (keyless-live); None if unavailable -> UI falls back."""
    coords = []
    for i in range(len(path) - 1):
        a, b = S["network"]["nodes"][path[i]], S["network"]["nodes"][path[i + 1]]
        leg = S["providers"].routing.leg((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if leg.get("geometry"):
            coords.extend(leg["geometry"])
        else:
            return None
    return coords


def _ctx():
    return {"network": S["network"], "holidays": S["holidays"], "transfer": S["transfer"],
            "hub_delays": S["hub_delays"], "providers": S["providers"], "history": S["history"],
            "direct_fn": _direct_estimate}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DATA_DIR or not os.path.isdir(DATA_DIR):
        raise RuntimeError("Set DATA_DIR to the folder containing the DACHSER CSVs.")
    S["network"] = dataloader.load_network(DATA_DIR)
    S["holidays"] = dataloader.load_holidays(DATA_DIR)
    S["transfer"] = dataloader.transfer_stats(DATA_DIR)
    # relationen dict for cost/analytics
    rel = {}
    for e in S["network"]["edges"].values():
        if e["from"] == S["network"]["hub_id"]:
            rel[e["relation"]] = {"km": e["km"], "cost_line": e["cost_eur"], "cost_special": e["cost_special_eur"]}
    S["history"] = an.relation_history(DATA_DIR, rel)
    S["disruptions"] = an.disruptions(DATA_DIR)
    S["providers"] = Providers(S["holidays"])
    S["hub_delays"] = {}
    S["audit"] = []
    S["ships"] = Shipments(_ctx())
    S["ships"].seed_if_empty()
    _audit("startup", f"{len(S['network']['nodes'])} hubs, {len(S['holidays'])} holidays, "
                      f"{len(S['history'])} lane histories, {len(S['ships'].list())} shipments")
    yield


app = FastAPI(title="DACHSER Live Transit Planner", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class RouteReq(BaseModel):
    origin: str
    destination: str
    depart_at: Optional[str] = None
    weight_kg: Optional[float] = 8000
    required_delivery: Optional[str] = None
    value_eur: Optional[float] = 0


class DelayReq(BaseModel):
    minutes: int
    reason: str
    note: Optional[str] = ""


class ShipmentReq(BaseModel):
    origin: str
    destination: str
    weight_kg: float = 8000
    value_eur: float = 0
    planned_departure: str
    required_delivery: Optional[str] = None
    container: Optional[str] = None
    customer_segment: Optional[str] = None


def _parse(dt):
    return datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---- network / reference ----
@app.get("/api/health")
def health():
    return {"status": "ok", "hubs": len(S["network"]["nodes"]), "time": _now_iso()}


@app.get("/api/network")
def network():
    n = S["network"]
    return {"hub_id": n["hub_id"], "nodes": list(n["nodes"].values()), "edges": list(n["edges"].values())}


@app.get("/api/hubs")
def hubs():
    out = []
    for nid, node in S["network"]["nodes"].items():
        rel = node.get("relation")
        st = S["transfer"].get(rel, {}) if rel else {}
        hist = S["history"].get(rel, {}) if rel else {}
        delay = S["hub_delays"].get(nid)
        out.append({**node, "transfer": st, "history": hist, "operational_delay": delay,
                    "expected_transfer_minutes": (st.get("avg_transfer_minutes", 120) if node["type"] == "branch" else 120) + (delay["minutes"] if delay else 0)})
    return {"hubs": out}


@app.get("/api/providers")
def providers():
    return {"providers": S["providers"].all(), "generated_at": _now_iso()}


@app.get("/api/holidays")
def holidays():
    today = datetime.now(timezone.utc).date().isoformat()
    up = sorted([{"date": d, "name": n, "region": "Baden-Württemberg", "country": "DE",
                  "transport_impact": "High (Sunday/holiday driving ban 00:00–22:00)"}
                 for d, n in S["holidays"].items() if d >= today], key=lambda x: x["date"])[:12]
    return {"source": "kalender.csv (Baden-Württemberg)", "upcoming": up, "count": len(S["holidays"])}


@app.get("/api/analytics/relations")
def analytics_relations():
    net = S["network"]
    rows = []
    for e in net["edges"].values():
        if e["from"] == net["hub_id"]:
            h = S["history"].get(e["relation"], {})
            rows.append({"relation": e["relation"], "destination": net["nodes"][e["to"]]["name"],
                         "km": e["km"], "cost_line_eur": e["cost_eur"], "cost_special_eur": e["cost_special_eur"], **h})
    return {"relations": sorted(rows, key=lambda r: r.get("spillover_rate", 0), reverse=True)}


@app.get("/api/disruptions")
def disruptions():
    active = [{"hub": nid, "name": S["network"]["nodes"][nid]["name"], **d, "source": "MANAGER INPUT"}
              for nid, d in S["hub_delays"].items()]
    return {"active_operational": active, "historical": S["disruptions"]}


@app.get("/api/weather")
def weather(node: str):
    n = S["network"]["nodes"].get(node)
    if not n:
        raise HTTPException(404, "unknown node")
    return {"node": node, **S["providers"].weather.fetch(n["lat"], n["lon"])}


# ---- planning ----
@app.post("/api/route")
def route(req: RouteReq):
    net = S["network"]
    if req.origin not in net["nodes"] or req.destination not in net["nodes"]:
        raise HTTPException(422, "unknown origin or destination")
    if req.origin == req.destination:
        raise HTTPException(422, "origin equals destination")
    depart = _parse(req.depart_at) if req.depart_at else datetime.now(timezone.utc)
    options = plan_options(_ctx(), req.origin, req.destination, depart,
                           req.weight_kg or 0, req.required_delivery, req.value_eur or 0)
    for o in options:
        o["geometry"] = _geometry(o["path"])
    savings = savings_vs_baseline(options[0], options[1]) if len(options) > 1 else None
    wx = []
    for nid in options[0]["path"]:
        nn = net["nodes"][nid]
        wx.append({"node": nid, "name": nn["name"], **S["providers"].weather.fetch(nn["lat"], nn["lon"])})
    _audit("route_calculated", f"{req.origin} → {req.destination}: recommended ETA {options[0]['eta']}")
    return {"options": options, "recommended": 0, "savings": savings,
            "weather": wx, "providers": S["providers"].all(), "evaluated_at": _now_iso()}


# ---- shipments ----
@app.get("/api/shipments")
def list_shipments(status: Optional[str] = None, origin: Optional[str] = None,
                   destination: Optional[str] = None, at_risk: Optional[bool] = None,
                   high_value: Optional[bool] = None, delayed: Optional[bool] = None):
    items = S["ships"].list()
    def keep(s):
        if status and s["status"] != status: return False
        if origin and s["origin"] != origin: return False
        if destination and s["destination"] != destination: return False
        if at_risk and s["risk_level"] not in ("MEDIUM", "HIGH"): return False
        if high_value and (s.get("value_eur", 0) < 100000): return False
        if delayed and not s.get("delay_minutes"): return False
        return True
    return {"shipments": [{k: v for k, v in s.items() if k != "options"} for s in items if keep(s)]}


@app.get("/api/shipments/{sid}")
def get_shipment(sid: str):
    s = S["ships"].get(sid)
    if not s:
        raise HTTPException(404, "unknown shipment")
    for o in s["options"]:
        if "geometry" not in o:
            o["geometry"] = _geometry(o["path"])
    return s


@app.post("/api/shipments")
def create_shipment(req: ShipmentReq):
    net = S["network"]
    if req.origin not in net["nodes"] or req.destination not in net["nodes"] or req.origin == req.destination:
        raise HTTPException(422, "invalid origin/destination")
    s = S["ships"].create(req.model_dump())
    _audit("shipment_created", f"{s['id']}: {req.origin} → {req.destination}, {req.weight_kg:.0f} kg, risk {s['risk_level']}")
    return s


@app.post("/api/shipments/{sid}/schedule")
def schedule_shipment(sid: str, option: int = 0, force: bool = False):
    s, warn = S["ships"].schedule(sid, option)
    if s is None:
        raise HTTPException(404, "unknown shipment")
    if warn and not force:
        return {"scheduled": False, "warning": warn, "shipment": {k: v for k, v in s.items() if k != "options"}}
    if warn and force:
        s.update({"status": "SCHEDULED", "scheduled_departure": s["planned_departure"]})
    _audit("shipment_scheduled", f"{sid}: scheduled via option {option}{' (forced past warning)' if force else ''}")
    return {"scheduled": True, "shipment": {k: v for k, v in s.items() if k != "options"}}


@app.post("/api/shipments/{sid}/status")
def set_status(sid: str, status: str):
    s = S["ships"].set_status(sid, status)
    if not s:
        raise HTTPException(404, "unknown shipment")
    _audit("shipment_status", f"{sid}: → {status}")
    return {"ok": True, "status": status}


# ---- dashboards ----
@app.get("/api/savings")
def savings():
    total = {"money_eur": 0, "fuel_l": 0, "time_min": 0, "optimized": 0}
    per = []
    for s in S["ships"].list():
        opts = s.get("options", [])
        if len(opts) < 2:
            continue
        sv = savings_vs_baseline(opts[0], opts[1])
        if sv["money_eur"] > 0 or sv["fuel_l"] > 0 or sv["time_min"] > 0:
            total["money_eur"] += max(0, sv["money_eur"]); total["fuel_l"] += max(0, sv["fuel_l"])
            total["time_min"] += max(0, sv["time_min"]); total["optimized"] += 1
            per.append({"id": s["id"], "container": s.get("container"), **sv})
    n = total["optimized"] or 1
    return {"total": total, "avg_per_shipment_eur": round(total["money_eur"] / n, 2),
            "per_shipment": per, "note": "Estimated savings vs the alternative plan — not guaranteed.",
            "source": "relationen.csv tariffs + plan"}


@app.get("/api/high-value")
def high_value():
    out = []
    for s in S["ships"].list():
        if s.get("value_eur", 0) >= 100000:
            opt = s.get("options", [{}])[0]
            out.append({"id": s["id"], "value_eur": s["value_eur"], "origin": s["origin"],
                        "destination": s["destination"], "current_location": s["current_location"],
                        "route": s["route"], "required_delivery": s.get("required_delivery"),
                        "current_eta": s["current_eta"], "risk_level": s["risk_level"],
                        "exposure_eur": opt.get("risk", {}).get("exposure_eur", 0),
                        "reasons": opt.get("risk", {}).get("reasons", []),
                        "recommended_action": "Review alternatives" if s["risk_level"] != "LOW" else "On track"})
    return {"shipments": sorted(out, key=lambda x: x["exposure_eur"], reverse=True)}


@app.get("/api/dashboard")
def dashboard():
    ships = S["ships"].list()
    by_status = {}
    for s in ships:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    at_risk = [s["id"] for s in ships if s["risk_level"] in ("MEDIUM", "HIGH")]
    high_val = [s["id"] for s in ships if s.get("value_eur", 0) >= 100000]
    sv = savings()
    hol = holidays()["upcoming"]
    return {"counts": by_status, "total": len(ships), "at_risk": len(at_risk),
            "high_value": len(high_val), "active_hub_delays": len(S["hub_delays"]),
            "next_holiday": hol[0] if hol else None,
            "estimated_savings_eur": sv["total"]["money_eur"],
            "providers": S["providers"].all(), "generated_at": _now_iso()}


# ---- manager ----
@app.post("/api/hubs/{node_id}/delay")
def set_delay(node_id: str, req: DelayReq):
    if node_id not in S["network"]["nodes"]:
        raise HTTPException(404, "unknown hub")
    S["hub_delays"][node_id] = {"minutes": req.minutes, "reason": req.reason, "note": req.note,
                                "status": "ACTIVE", "at": _now_iso()}
    _audit("hub_delay_set", f"{S['network']['nodes'][node_id]['name']}: +{req.minutes}m — {req.reason}", "MANAGER INPUT")
    return {"ok": True, "hub": node_id, "delay": S["hub_delays"][node_id]}


@app.delete("/api/hubs/{node_id}/delay")
def clear_delay(node_id: str):
    if S["hub_delays"].pop(node_id, None):
        _audit("hub_delay_resolved", f"{S['network']['nodes'][node_id]['name']}: operational delay resolved", "MANAGER INPUT")
    return {"ok": True}


@app.get("/api/audit")
def audit():
    return {"events": S["audit"]}
