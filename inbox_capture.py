from __future__ import annotations

from pathlib import Path
from typing import Optional


class InboxCapture:
    def __init__(self, path: Path | str = "INBOX.txt") -> None:
        self.path = Path(path)

    def capture(
        self,
        message: str,
        *,
        tag: str = "idea",
        schedule: str = "later",
        work_order_id: Optional[str] = None,
    ) -> None:
        line = f"[{tag}] ({schedule}) {message}"
        if work_order_id:
            line = f"{line} #wo={work_order_id}"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            raise RuntimeError(f"Failed to write to inbox {self.path}: {exc}") from exc
