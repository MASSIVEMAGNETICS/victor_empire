from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import List, Optional


@dataclass
class WorkOrder:
    goal: str
    definition_of_done: str
    next_actions: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    timebox_minutes: Optional[int] = None
    id: str = field(default_factory=lambda: f"wo-{uuid.uuid4().hex}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "definition_of_done": self.definition_of_done,
            "next_actions": self.next_actions,
            "blockers": self.blockers,
            "timebox_minutes": self.timebox_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkOrder":
        missing = [field for field in ("goal", "definition_of_done") if field not in data]
        if missing:
            raise ValueError(f"WorkOrder missing required fields: {', '.join(missing)}")
        return cls(
            id=data.get("id", f"wo-{uuid.uuid4().hex}"),
            goal=data["goal"],
            definition_of_done=data["definition_of_done"],
            next_actions=data.get("next_actions", []),
            blockers=data.get("blockers", []),
            timebox_minutes=data.get("timebox_minutes"),
        )

    def save(self, path: Path) -> None:
        try:
            path.write_text(json.dumps(self.to_dict(), indent=2))
        except OSError as exc:
            raise RuntimeError(f"Failed to save work order to {path}: {exc}") from exc

    @classmethod
    def load(cls, path: Path) -> "WorkOrder":
        try:
            contents = path.read_text()
            data = json.loads(contents)
        except OSError as exc:
            raise RuntimeError(f"Failed to read work order from {path}: {exc}") from exc
        except JSONDecodeError as exc:
            raise RuntimeError(f"Invalid work order format in {path}: {exc}") from exc
        return cls.from_dict(data)
