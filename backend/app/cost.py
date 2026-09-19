"""
Cost + fuel engine. Transport cost comes from relationen.csv (real line-trailer /
special-trip tariffs). Fuel is a physical ESTIMATE (litres from km) — labelled.
"""
from __future__ import annotations

from .analytics import FUEL_L_PER_100KM, DIESEL_EUR_PER_L

CAPACITY_LDM = 13.6


def leg_cost(edge, ldm: float, force_special: bool = False):
    """Cost for moving `ldm` loading-metres over one lane."""
    cap = edge.get("capacity_ldm", CAPACITY_LDM)
    share = min(1.0, max(0.15, ldm / cap))  # min 15% allocation
    if force_special:
        base = edge.get("cost_special_eur", 0)
        kind = "special"
    else:
        base = edge.get("cost_eur", 0)
        kind = "line"
    transport = round(base * share)
    fuel_l = round(edge.get("km", 0) * FUEL_L_PER_100KM / 100.0)
    return {"transport_eur": transport, "fuel_l": fuel_l,
            "fuel_eur": round(fuel_l * DIESEL_EUR_PER_L), "km": edge.get("km", 0),
            "kind": kind, "ldm_share": round(share, 2)}


def journey_cost(network, path, ldm: float):
    """Sum lane costs across a path. Returns totals + per-leg detail."""
    total = {"transport_eur": 0, "fuel_l": 0, "fuel_eur": 0, "km": 0}
    legs = []
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        edge = network["edges"].get(f"{a}-{b}")
        if not edge:  # direct estimate — synthesise a lane cost from distance
            from .eta import _haversine_km
            na, nb = network["nodes"][a], network["nodes"][b]
            km = round(_haversine_km((na["lat"], na["lon"]), (nb["lat"], nb["lon"])) * 1.25)
            edge = {"km": km, "cost_eur": round(km * 2.6), "cost_special_eur": round(km * 4.4), "capacity_ldm": CAPACITY_LDM}
        lc = leg_cost(edge, ldm)
        legs.append({"from": a, "to": b, **lc})
        for k in total:
            total[k] += lc.get(k, 0)
    total["cost_per_leg"] = legs
    return total


def cost_per_kg(total_cost_eur: float, weight_kg: float):
    if not weight_kg:
        return None
    return round(total_cost_eur / weight_kg, 4)
