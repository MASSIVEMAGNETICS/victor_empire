from __future__ import annotations

import argparse
import json
from pathlib import Path

from victor_runtime import VictorKernel
from victor_runtime.events import utc_now


def migrate(root: Path, kernel: VictorKernel) -> dict:
    counts = {"inbox": 0, "runway": 0, "active_work_order": 0}

    inbox_path = root / "INBOX.txt"
    if inbox_path.exists():
        for line in inbox_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                kernel.capture(f"[legacy] {line.strip()}")
                counts["inbox"] += 1

    runway_path = root / "runway_log.jsonl"
    if runway_path.exists():
        for line in runway_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = {"raw": line, "parse_error": True}
            kernel.events.append(
                actor="legacy-migrator",
                action="LEGACY_RUNWAY_IMPORTED",
                entity_id=payload.get("work_order_id"),
                payload=payload,
            )
            counts["runway"] += 1

    active_path = root / ".active_work_order.json"
    if active_path.exists():
        try:
            payload = json.loads(active_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"raw": active_path.read_text(encoding="utf-8"), "parse_error": True}
        kernel.events.append(
            actor="legacy-migrator",
            action="LEGACY_ACTIVE_WORK_ORDER_SNAPSHOT",
            entity_id=payload.get("id"),
            payload=payload,
        )
        counts["active_work_order"] += 1

    kernel.events.append(
        actor="legacy-migrator",
        action="LEGACY_MIGRATION_FINISHED",
        entity_id=None,
        payload={"counts": counts, "root": str(root.resolve()), "at": utc_now()},
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy victor_empire flat files as evidence.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir", default=".victor")
    parser.add_argument("--workspace", default="artifacts")
    args = parser.parse_args()
    kernel = VictorKernel(data_dir=args.data_dir, workspace=args.workspace)
    print(json.dumps(migrate(Path(args.root), kernel), indent=2))


if __name__ == "__main__":
    main()
