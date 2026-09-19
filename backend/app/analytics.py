"""
Historical analytics from the REAL operational history (disposition.csv, 19,980
daily records) and disruption log (stoerungen.csv). Everything here is measured
history — used for historical route performance, reliability and savings baselines.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict

FUEL_L_PER_100KM = 30.0  # articulated-truck estimate (labelled)
DIESEL_EUR_PER_L = 1.65  # fuel price estimate (labelled)


def relation_history(data_dir: str, relationen: dict) -> dict:
    """Per-relation historical KPIs from disposition.csv.
    relationen: { 'R01': {km, cost_line, cost_special}, ... } from relationen.csv.
    """
    path = os.path.join(data_dir, "disposition.csv")
    agg = defaultdict(lambda: {"days": 0, "spill_days": 0, "util": 0.0, "linien": 0,
                               "sonder": 0, "ldm": 0.0})
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                b = agg[r["relation"]]
                b["days"] += 1
                b["util"] += float(r.get("auslastung_pct", 0) or 0)
                b["linien"] += int(float(r.get("trailer_linie_gefahren", 0) or 0))
                b["sonder"] += int(float(r.get("sonderfahrten", 0) or 0))
                b["ldm"] += float(r.get("lademeter_aufkommen", 0) or 0)
                if float(r.get("lademeter_verschoben_auf_folgetag", 0) or 0) > 0:
                    b["spill_days"] += 1
    out = {}
    for rel, b in agg.items():
        days = b["days"] or 1
        rl = relationen.get(rel, {})
        line_cost, spec_cost, km = rl.get("cost_line", 0), rl.get("cost_special", 0), rl.get("km", 0)
        # real historical daily cost = line trailers * line cost + special trips * special cost
        avg_line = b["linien"] / days
        avg_sonder = b["sonder"] / days
        avg_cost = avg_line * line_cost + avg_sonder * spec_cost
        avg_trailers = avg_line + avg_sonder
        avg_fuel = km * avg_trailers * FUEL_L_PER_100KM / 100.0
        out[rel] = {
            "samples": b["days"],
            "avg_utilisation": round(b["util"] / days, 1),
            "spillover_rate": round(b["spill_days"] / days, 3),
            "special_trip_rate": round(avg_sonder, 3),
            "avg_line_trailers": round(avg_line, 2),
            "avg_daily_cost_eur": round(avg_cost),
            "avg_daily_fuel_l": round(avg_fuel),
            "avg_volume_ldm": round(b["ldm"] / days, 1),
            "reliability_pct": round(100 * (1 - b["spill_days"] / days), 1),
            "source": "disposition.csv (measured history)",
        }
    return out


def disruptions(data_dir: str) -> list:
    """Real historical disruption events (stoerungen.csv)."""
    path = os.path.join(data_dir, "stoerungen.csv")
    out = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                out.append({
                    "from": r["von"], "to": r["bis"], "type": r["typ"],
                    "description": r["beschreibung"],
                    "volume_effect": float(r.get("effekt_volumen", 0) or 0),
                    "recovery_next_day": float(r.get("nachholeffekt_folgetag", 0) or 0),
                    "source": "stoerungen.csv (historical)",
                })
    return sorted(out, key=lambda d: d["from"], reverse=True)
