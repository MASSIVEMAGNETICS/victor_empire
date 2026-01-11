from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Optional

from work_order import WorkOrder


class SingleThreadEnforcer:
    def __init__(self, state_path: Path | str = ".active_work_order.json") -> None:
        self.state_path = Path(state_path)

    def get_active(self) -> Optional[WorkOrder]:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, JSONDecodeError):
            return None
        return WorkOrder.from_dict(data)

    def set_active(self, work_order: WorkOrder) -> None:
        self.state_path.write_text(json.dumps(work_order.to_dict(), indent=2))

    def clear_active(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    def ensure_single_active(self, work_order: WorkOrder) -> None:
        active = self.get_active()
        if active and active.id != work_order.id:
            raise RuntimeError(
                f"Active work order {active.id} must be completed or cleared before starting {work_order.id}"
            )
        self.set_active(work_order)
