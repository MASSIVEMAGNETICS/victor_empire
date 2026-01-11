from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


class RunwayLedger:
    def __init__(self, path: Path | str = "runway_log.jsonl") -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        mode: str,
        work_order_id: str,
        artifact: str,
        minutes_worked: Optional[int] = None,
        friction_events: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "timestamp": int(time.time()),
            "mode": mode,
            "work_order_id": work_order_id,
            "artifact": artifact,
            "minutes_worked": minutes_worked,
            "friction_events": friction_events,
        }
        if extra:
            entry.update(extra)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
