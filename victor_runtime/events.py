from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import VictorDB


GENESIS_HASH = "0" * 64


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_digest(event: dict[str, Any]) -> str:
    material = canonical_json(event).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class EventLedger:
    def __init__(self, db: VictorDB) -> None:
        self.db = db

    def append(
        self,
        *,
        actor: str,
        action: str,
        entity_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = row["hash"] if row else GENESIS_HASH
            event = {
                "event_id": f"evt-{uuid.uuid4().hex}",
                "ts": utc_now(),
                "actor": actor,
                "action": action,
                "entity_id": entity_id,
                "payload": payload,
                "prev_hash": prev_hash,
            }
            digest = event_digest(event)
            conn.execute(
                """
                INSERT INTO events(event_id, ts, actor, action, entity_id, payload_json, prev_hash, hash)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    event["event_id"],
                    event["ts"],
                    actor,
                    action,
                    entity_id,
                    canonical_json(payload),
                    prev_hash,
                    digest,
                ),
            )
            event["hash"] = digest
            return event

    def verify(self) -> tuple[bool, int, str | None]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()

        prev_hash = GENESIS_HASH
        for index, row in enumerate(rows, start=1):
            payload = json.loads(row["payload_json"])
            event = {
                "event_id": row["event_id"],
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "entity_id": row["entity_id"],
                "payload": payload,
                "prev_hash": row["prev_hash"],
            }
            if row["prev_hash"] != prev_hash:
                return False, index, "prev_hash mismatch"
            if event_digest(event) != row["hash"]:
                return False, index, "hash mismatch"
            prev_hash = row["hash"]
        return True, len(rows), None
