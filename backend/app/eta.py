"""
ETA engine. Composes the COMPLETE realistic journey and explains every component:
  transport time + hub transfer (historical proxy) + current hub delays
  + traffic impact (when live) + legal waiting time (dynamic) .

Route model on the provided star network: a branch->branch shipment routes via the
Heilbronn hub (origin -> HN -> destination); origin/destination == HN is a single leg.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .restrictions import drive_with_restrictions

HUB_HANDLING_MIN = 120  # central-hub transfer baseline (labelled)


def _haversine_km(a, b):
    R = 6371.0
    dlat = math.radians(b[0] - a[0]); dlon = math.radians(b[1] - a[1])
    x = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _path(network, origin, dest):
    hub = network["hub_id"]
    if origin == hub or dest == hub:
        return [origin, dest]
    return [origin, hub, dest]


def _handling_for(network, transfer, node_id):
    """Transfer minutes + its source for a node."""
    node = network["nodes"][node_id]
    if node["type"] == "hub":
        return HUB_HANDLING_MIN, "hub baseline (labelled)"
    rel = node.get("relation")
    st = transfer.get(rel)
    if st:
        return st["avg_transfer_minutes"], st["source"]
    return 120, "default"


def compute_journey(network, holidays, transfer, hub_delays, providers,
                    origin, dest, depart_utc: datetime):
    path = _path(network, origin, dest)
    rows = []
    cur = depart_utc
    comp = {"transport": 0, "transfer": 0, "hub_delay": 0, "traffic": 0, "legal_wait": 0}
    weather_notes = []

    def add(kind, loc, start, end, minutes, detail, source):
        rows.append({"type": kind, "location": loc, "location_name": network["nodes"][loc]["name"] if loc in network["nodes"] else loc,
                     "start": start.isoformat(), "end": end.isoformat(),
                     "minutes": int(minutes), "detail": detail, "source": source})

    # origin handling (load/prep)
    h, hsrc = _handling_for(network, transfer, origin)
    add("handling", origin, cur, cur + timedelta(minutes=h), h,
        "Loading & handling (historical proxy)", hsrc)
    comp["transfer"] += h
    cur += timedelta(minutes=h)
    hd = hub_delays.get(origin)
    if hd and hd.get("minutes"):
        add("hub_delay", origin, cur, cur + timedelta(minutes=hd["minutes"]), hd["minutes"],
            f"Operational delay: {hd.get('reason','')}", "MANAGER INPUT")
        comp["hub_delay"] += hd["minutes"]; cur += timedelta(minutes=hd["minutes"])

    # legs
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        edge = network["edges"].get(f"{a}-{b}")
        if not edge:  # symmetric fallback
            na, nb = network["nodes"][a], network["nodes"][b]
            km = round(_haversine_km((na["lat"], na["lon"]), (nb["lat"], nb["lon"])) * 1.25)
            drive = round(km / 65 * 60)
            src = "estimated (no scheduled lane)"
        else:
            km, drive, src = edge["km"], edge["base_drive_minutes"], edge["source"]

        # live traffic (key-gated)
        tr = providers.traffic.leg((network["nodes"][a]["lat"], network["nodes"][a]["lon"]),
                                   (network["nodes"][b]["lat"], network["nodes"][b]["lon"]))
        traffic_delay = tr.get("delay_minutes", 0)
        comp["traffic"] += traffic_delay
        drive_total = drive + traffic_delay

        # legal restrictions (dynamic) applied across the drive
        leg = drive_with_restrictions(cur, drive_total, holidays)
        # interleave drive chunks and legal-wait pauses into timeline rows
        t = cur; driven = 0
        for p in leg["pauses"]:
            before = int((p["start"] - t).total_seconds() // 60)
            if before > 0:
                add("drive", a, t, p["start"], before, f"Road {network['nodes'][a]['name']} → {network['nodes'][b]['name']} ({km} km){' · +'+str(traffic_delay)+'m traffic' if traffic_delay else ''}", src)
                driven += before
            add("legal_wait", a, p["start"], p["end"], p["minutes"], p["reason"], "REGULATORY DATA")
            t = p["end"]
        rest = drive_total - driven
        if rest > 0:
            add("drive", a, t, leg["arrival"], rest, f"Road {network['nodes'][a]['name']} → {network['nodes'][b]['name']} ({km} km){' · +'+str(traffic_delay)+'m traffic' if traffic_delay else ''}", src)
        comp["transport"] += drive
        comp["legal_wait"] += leg["waiting_minutes"]
        cur = leg["arrival"]

        # handling at intermediate hub (not at final destination)
        if b != dest:
            hb, hbsrc = _handling_for(network, transfer, b)
            add("handling", b, cur, cur + timedelta(minutes=hb), hb, "Hub transfer (historical proxy)", hbsrc)
            comp["transfer"] += hb; cur += timedelta(minutes=hb)
            hdb = hub_delays.get(b)
            if hdb and hdb.get("minutes"):
                add("hub_delay", b, cur, cur + timedelta(minutes=hdb["minutes"]), hdb["minutes"],
                    f"Operational delay: {hdb.get('reason','')}", "MANAGER INPUT")
                comp["hub_delay"] += hdb["minutes"]; cur += timedelta(minutes=hdb["minutes"])

    total = int((cur - depart_utc).total_seconds() // 60)
    return {
        "origin": origin, "destination": dest, "path": path,
        "depart_at": depart_utc.isoformat(), "eta": cur.isoformat(),
        "total_minutes": total, "components": comp, "steps": rows,
        "weather_notes": weather_notes,
        "data_sources": {
            "transport": "relationen.csv (km) + road average",
            "transfer": "HISTORICAL (derived proxy from disposition.csv)",
            "hub_delay": "MANAGER INPUT",
            "traffic": providers.traffic.status(),
            "legal_wait": "REGULATORY DATA (kalender.csv + German rules)",
        },
    }
