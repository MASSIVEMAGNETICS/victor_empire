from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional


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

    if tab_switch_pressure is not None and tab_switch_pressure > 6:
        return "RECOVERY"

    if last_output_minutes_ago is not None and last_output_minutes_ago > 180:
        return "CLARITY"

    if 6 <= hour <= 18 and (tab_switch_pressure or 0) <= 6:
        return "DRIVE"

    return "CLARITY"


def mode_payload(signals: Dict[str, Any]) -> str:
    return route_mode(
        now=signals.get("now"),
        last_output_minutes_ago=signals.get("last_output_minutes_ago"),
        self_reported_state=signals.get("self_reported_state"),
        tab_switch_pressure=signals.get("tab_switch_pressure"),
    )
