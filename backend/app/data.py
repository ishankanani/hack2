"""
Real DACHSER operational data loader.

Builds the live route network from the PROVIDED dataset:
  - relationen.csv   -> hub-and-spoke network (Heilbronn central hub + 30 branches)
  - kalender.csv     -> real German (Baden-Württemberg) public-holiday calendar
  - disposition.csv  -> historical operational stats -> hub transfer-time proxy

Nothing here is invented: km/cost/capacity come straight from relationen.csv,
holidays from kalender.csv. Transfer times are a DERIVED PROXY (the files carry
no measured handling timestamps) and are labelled as such everywhere.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date

# Central hub for the provided network (the data is a star from Heilbronn:
# short km to Karlsruhe/Mannheim 90, Würzburg 110, Ulm 120, Frankfurt 150).
HUB_ID = "HN"
HUB_NAME = "Heilbronn"

# Approximate depot coordinates (lat, lon) for map placement.
COORDS = {
    "Heilbronn": (49.142, 9.211), "Hamburg": (53.551, 9.993), "Bremen": (53.079, 8.802),
    "Berlin": (52.520, 13.405), "Hannover": (52.375, 9.732), "Dortmund": (51.514, 7.466),
    "Köln": (50.938, 6.960), "Düsseldorf": (51.228, 6.773), "Frankfurt": (50.110, 8.682),
    "Kassel": (51.312, 9.480), "Nürnberg": (49.452, 11.077), "München": (48.135, 11.582),
    "Augsburg": (48.370, 10.898), "Regensburg": (49.013, 12.101), "Ulm": (48.401, 9.987),
    "Freiburg": (47.999, 7.842), "Karlsruhe": (49.007, 8.404), "Mannheim": (49.487, 8.466),
    "Saarbrücken": (49.240, 6.997), "Koblenz": (50.356, 7.594), "Leipzig": (51.340, 12.375),
    "Dresden": (51.051, 13.738), "Erfurt": (50.978, 11.029), "Magdeburg": (52.121, 11.628),
    "Rostock": (54.092, 12.140), "Kiel": (54.323, 10.135), "Münster": (51.962, 7.626),
    "Bielefeld": (52.021, 8.535), "Osnabrück": (52.279, 8.047), "Würzburg": (49.794, 9.932),
    "Chemnitz": (50.828, 12.921),
}
AVG_SPEED_KMH = 65.0  # line-haul road average; live routing overrides when available


def _slug(name: str) -> str:
    return (name.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae")
            .replace("ß", "ss").upper()[:3])


def load_network(data_dir: str) -> dict:
    """Hub-and-spoke graph from relationen.csv. Heilbronn <-> each branch."""
    path = os.path.join(data_dir, "relationen.csv")
    nodes = {HUB_ID: {"id": HUB_ID, "name": HUB_NAME, "type": "hub",
                      "lat": COORDS[HUB_NAME][0], "lon": COORDS[HUB_NAME][1],
                      "source": "relationen.csv"}}
    edges = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = row["ziel_niederlassung"]
            nid = row["relation"]  # R01..R30 — unique, tied to the dataset
            lat, lon = COORDS.get(name, (49.0, 9.0))
            nodes[nid] = {"id": nid, "name": name, "type": "branch", "lat": lat, "lon": lon,
                          "relation": row["relation"], "source": "relationen.csv"}
            km = int(row["km"])
            base_min = round(km / AVG_SPEED_KMH * 60)
            for a, b in ((HUB_ID, nid), (nid, HUB_ID)):
                edges[f"{a}-{b}"] = {
                    "id": f"{a}-{b}", "from": a, "to": b, "km": km,
                    "base_drive_minutes": base_min, "mode": "road",
                    "relation": row["relation"],
                    "cost_eur": int(row["kosten_linientrailer_eur"]),
                    "cost_special_eur": int(row["kosten_sonderfahrt_eur"]),
                    "capacity_ldm": float(row["kapazitaet_ldm"]),
                    "source": "relationen.csv",
                }
    return {"hub_id": HUB_ID, "nodes": nodes, "edges": edges}


def load_holidays(data_dir: str) -> dict:
    """{ 'YYYY-MM-DD': name } from kalender.csv feiertag_bw (real provided data)."""
    path = os.path.join(data_dir, "kalender.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("feiertag_bw") or "").strip()
            if name:
                out[row["datum"]] = name
    return out


def transfer_stats(data_dir: str) -> dict:
    """Per-relation hub transfer-time PROXY derived from disposition.csv.

    The file has no measured handling timestamps, so we derive a proxy from the
    real spillover/utilisation signal: a base handling plus a congestion term
    from the share of days volume was postponed to the next day. Clearly labelled
    as derived, with sample counts and a plausible median/range.
    """
    path = os.path.join(data_dir, "disposition.csv")
    agg = defaultdict(lambda: {"n": 0, "spill": 0, "util": 0.0})
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                b = agg[row["relation"]]
                b["n"] += 1
                if float(row.get("lademeter_verschoben_auf_folgetag", 0) or 0) > 0:
                    b["spill"] += 1
                b["util"] += float(row.get("auslastung_pct", 0) or 0)
    out = {}
    for rel, b in agg.items():
        n = b["n"] or 1
        spill = b["spill"] / n
        avg = 120 + round(spill * 180)  # base 120 min + congestion term
        out[rel] = {
            "samples": b["n"], "spillover_rate": round(spill, 3),
            "avg_utilisation": round(b["util"] / n, 1),
            "avg_transfer_minutes": avg,
            "median_transfer_minutes": max(90, avg - 20),
            "range_minutes": [max(60, avg - 45), avg + 60],
            "source": "derived from disposition.csv (proxy; file has no measured transfer times)",
        }
    return out


def parse_date(s: str) -> date:
    return date.fromisoformat(s)
