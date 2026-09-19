"""Pre-shipment + in-transit risk scoring. Explainable, from real signals."""
from __future__ import annotations

from datetime import datetime, timezone


def assess(journey, required_delivery, value_eur, lane_histories):
    """-> {level, score, reasons[], exposure_eur, deadline_ok}."""
    reasons = []
    score = 0
    eta = datetime.fromisoformat(journey["eta"])
    deadline_ok = True
    exposure = 0

    if required_delivery:
        req = datetime.fromisoformat(required_delivery.replace("Z", "+00:00"))
        buffer_min = (req - eta).total_seconds() / 60
        if buffer_min < 0:
            score += 60; deadline_ok = False
            reasons.append(f"ETA misses the required delivery window by {abs(int(buffer_min//60))}h")
            exposure = value_eur or 0
        elif buffer_min < 180:
            score += 30
            reasons.append(f"Only {int(buffer_min//60)}h {int(buffer_min%60)}m of slack before the deadline")

    lw = journey["components"].get("legal_wait", 0)
    if lw > 0:
        score += 20
        reasons.append(f"Route crosses a driving ban → {lw//60}h legal waiting")

    if lane_histories:
        avg_spill = sum(h.get("spillover_rate", 0) for h in lane_histories) / len(lane_histories)
        if avg_spill > 0.15:
            score += 15
            reasons.append(f"Lanes historically spill volume on {round(avg_spill*100)}% of days")

    if value_eur and value_eur >= 100000:
        score += 10
        reasons.append(f"High-value shipment (€{value_eur:,.0f}) — elevated exposure")
        if not deadline_ok:
            exposure = value_eur

    level = "HIGH" if score >= 55 else "MEDIUM" if score >= 25 else "LOW"
    if not reasons:
        reasons.append("No significant risk factors detected")
    return {"level": level, "score": score, "reasons": reasons,
            "exposure_eur": exposure, "deadline_ok": deadline_ok}
