from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional

CLARITY_THRESHOLD_MINUTES = 180
DAYTIME_START_HOUR = 6
DAYTIME_END_HOUR = 18
MAX_LOW_PRESSURE = 6


def route_mode(
    *,
    now: Optional[_dt.datetime] = None,
    last_output_minutes_ago: Optional[int] = None,
    self_reported_state: Optional[str] = None,
    tab_switch_pressure: Optional[int] = None,
) -> str:
    """
    Select a mode based on lightweight signals.

    - CLARITY: when overwhelmed, long since last output, or late night
    - DRIVE: daytime hours, recent momentum, and low friction
    - RECOVERY: high tab-switch pressure or self-reported fatigue
    """
    ts = now or _dt.datetime.now()
    hour = ts.hour

    if self_reported_state:
        normalized = self_reported_state.strip().lower()
        if normalized in {"tired", "fried", "fatigued", "burnt"}:
            return "RECOVERY"
        if normalized in {"focused", "ready", "fresh"}:
            return "DRIVE"

    if tab_switch_pressure is not None:
        if not 1 <= tab_switch_pressure <= 10:
            raise ValueError("tab_switch_pressure must be between 1 and 10")
        if tab_switch_pressure > MAX_LOW_PRESSURE:
            return "RECOVERY"

    if last_output_minutes_ago is not None and last_output_minutes_ago > CLARITY_THRESHOLD_MINUTES:
        return "CLARITY"

    if DAYTIME_START_HOUR <= hour <= DAYTIME_END_HOUR and (tab_switch_pressure or 0) <= MAX_LOW_PRESSURE:
        return "DRIVE"

    return "CLARITY"


def mode_payload(signals: Dict[str, Any]) -> str:
    return route_mode(
        now=signals.get("now"),
        last_output_minutes_ago=signals.get("last_output_minutes_ago"),
        self_reported_state=signals.get("self_reported_state"),
        tab_switch_pressure=signals.get("tab_switch_pressure"),
    )
