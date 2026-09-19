"""
Dynamic legal-restriction engine.

Germany: heavy trucks are generally prohibited from driving on Sundays and public
holidays 00:00-22:00 (StVO §30 Abs. 3; exemptions exist). This engine does NOT add
a flat "+24h". It walks the journey forward from the departure instant, drives
during legal windows and pauses during restricted windows, and returns the ACTUAL
waiting time plus every pause with its reason and source.

All wall-clock logic is in Europe/Berlin; inputs/outputs are timezone-aware UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
RESTRICTION_END_HOUR = 22  # ban lifts at 22:00 local on Sundays / holidays


def _restricted_day(d, holidays: dict):
    """(is_restricted_day, reason) for a local date."""
    iso = d.isoformat()
    if iso in holidays:
        return True, f"Public holiday: {holidays[iso]}"
    if d.isoweekday() == 7:
        return True, "Sunday driving ban"
    return False, None


def restriction_at(instant_utc: datetime, holidays: dict):
    """Is driving restricted at this instant? -> (bool, reason, window_end_utc)."""
    local = instant_utc.astimezone(BERLIN)
    is_day, reason = _restricted_day(local.date(), holidays)
    if is_day and local.hour < RESTRICTION_END_HOUR:
        end_local = local.replace(hour=RESTRICTION_END_HOUR, minute=0, second=0, microsecond=0)
        return True, reason, end_local.astimezone(timezone.utc)
    return False, None, None


def _next_restriction_start(instant_utc: datetime, holidays: dict, horizon_days=14):
    """Next UTC instant at which a restriction window begins (local 00:00 of a
    restricted day), at or after `instant_utc`."""
    local = instant_utc.astimezone(BERLIN)
    for i in range(0, horizon_days + 1):
        d = (local + timedelta(days=i)).date()
        is_day, _ = _restricted_day(d, holidays)
        if is_day:
            start_local = datetime(d.year, d.month, d.day, 0, 0, tzinfo=BERLIN)
            start_utc = start_local.astimezone(timezone.utc)
            if start_utc > instant_utc:
                return start_utc
    return None


def drive_with_restrictions(depart_utc: datetime, drive_minutes: int, holidays: dict):
    """Consume `drive_minutes` of driving from `depart_utc`, pausing inside
    restricted windows. Returns {arrival, driving_minutes, waiting_minutes, pauses}.
    """
    cur = depart_utc
    remaining = int(drive_minutes)
    pauses = []
    guard = 0
    while remaining > 0 and guard < 60:
        guard += 1
        restricted, reason, window_end = restriction_at(cur, holidays)
        if restricted:
            pauses.append({"start": cur, "end": window_end, "minutes": int((window_end - cur).total_seconds() // 60), "reason": reason})
            cur = window_end
            continue
        nxt = _next_restriction_start(cur, holidays)
        if nxt is None:
            cur = cur + timedelta(minutes=remaining)
            remaining = 0
        else:
            avail = int((nxt - cur).total_seconds() // 60)
            take = min(remaining, avail)
            cur = cur + timedelta(minutes=take)
            remaining -= take
    return {
        "arrival": cur,
        "driving_minutes": int(drive_minutes),
        "waiting_minutes": sum(p["minutes"] for p in pauses),
        "pauses": pauses,
    }
