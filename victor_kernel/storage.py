from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .crypto import canonical_json, sha256_json
from .models import ChronosReceipt, Informatron


SCHEMA_VERSION = "victor.kernel.v1"


class KernelStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._bootstrap()

    def _configure(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=10000")

    def _bootstrap(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                schema_version TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                authority TEXT NOT NULL,
                causation_id TEXT,
                correlation_id TEXT,
                idempotency_key TEXT,
                parent_event_hash TEXT,
                signature TEXT,
                event_hash TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS chronos_receipts (
                sequence INTEGER PRIMARY KEY REFERENCES events(sequence) ON DELETE RESTRICT,
                event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT,
                event_hash TEXT NOT NULL UNIQUE,
                previous_chain_hash TEXT,
                chain_hash TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS work_orders (
                work_order_id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                definition_of_done TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                assigned_organ TEXT NOT NULL,
                priority INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                parent_work_order_id TEXT REFERENCES work_orders(work_order_id),
                inputs_json TEXT NOT NULL,
                required_capabilities_json TEXT NOT NULL,
                verification_requirements_json TEXT NOT NULL,
                last_artifact_sha256 TEXT
            );

            CREATE TABLE IF NOT EXISTS work_order_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id) ON DELETE RESTRICT,
                from_status TEXT,
                to_status TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
                transitioned_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS capability_leases (
                lease_id TEXT PRIMARY KEY,
                work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id) ON DELETE RESTRICT,
                actor_id TEXT NOT NULL,
                organ_id TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_uses INTEGER NOT NULL CHECK(max_uses > 0),
                uses INTEGER NOT NULL DEFAULT 0 CHECK(uses >= 0),
                status TEXT NOT NULL,
                issuer TEXT NOT NULL,
                signature TEXT NOT NULL UNIQUE,
                issued_event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS verification_receipts (
                receipt_id TEXT PRIMARY KEY,
                work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id) ON DELETE RESTRICT,
                artifact_sha256 TEXT NOT NULL,
                verifier_id TEXT NOT NULL,
                passed INTEGER NOT NULL CHECK(passed IN (0,1)),
                checks_json TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                executed_at TEXT NOT NULL,
                environment_fingerprint_json TEXT NOT NULL,
                signature TEXT NOT NULL UNIQUE,
                event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS command_results (
                idempotency_key TEXT PRIMARY KEY,
                command_type TEXT NOT NULL,
                work_order_id TEXT,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
                topic TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_work_orders_status ON work_orders(status);
            CREATE INDEX IF NOT EXISTS idx_leases_work_order ON capability_leases(work_order_id);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, outbox_id);
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def schema_version(self) -> str:
        row = self.conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        return str(row["value"]) if row else ""

    @staticmethod
    def json_load(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _next_sequence(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS n FROM events").fetchone()
        return int(row["n"])

    @staticmethod
    def _chain_head(conn: sqlite3.Connection) -> tuple[Optional[str], Optional[str]]:
        row = conn.execute(
            """
            SELECT e.event_hash, c.chain_hash
            FROM events e
            JOIN chronos_receipts c ON c.sequence=e.sequence
            ORDER BY e.sequence DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return None, None
        return str(row["event_hash"]), str(row["chain_hash"])

    def append_event(
        self,
        conn: sqlite3.Connection,
        *,
        occurred_at: str,
        recorded_at: str,
        actor_id: str,
        action: str,
        entity_id: str,
        payload: Dict[str, Any],
        provenance: Dict[str, Any],
        evidence: Dict[str, Any],
        authority: str,
        causation_id: Optional[str],
        correlation_id: Optional[str],
        idempotency_key: Optional[str],
        signature: Optional[str] = None,
    ) -> tuple[Informatron, ChronosReceipt]:
        sequence = self._next_sequence(conn)
        parent_event_hash, previous_chain_hash = self._chain_head(conn)
        core = {
            "sequence": sequence,
            "schema_version": "informatron.kernel.v1",
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "actor_id": actor_id,
            "action": action,
            "entity_id": entity_id,
            "payload": payload,
            "provenance": provenance,
            "evidence": evidence,
            "authority": authority,
            "causation_id": causation_id,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "parent_event_hash": parent_event_hash,
            "signature": signature,
        }
        event_id = sha256_json(core)
        event = Informatron(event_id=event_id, **core)
        event_hash = sha256_json(event.to_dict())
        receipt_core = {
            "sequence": sequence,
            "event_id": event_id,
            "event_hash": event_hash,
            "previous_chain_hash": previous_chain_hash,
        }
        receipt = ChronosReceipt(chain_hash=sha256_json(receipt_core), **receipt_core)
        conn.execute(
            """
            INSERT INTO events(
                sequence,event_id,schema_version,occurred_at,recorded_at,actor_id,action,
                entity_id,payload_json,provenance_json,evidence_json,authority,causation_id,
                correlation_id,idempotency_key,parent_event_hash,signature,event_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sequence,
                event_id,
                event.schema_version,
                occurred_at,
                recorded_at,
                actor_id,
                action,
                entity_id,
                canonical_json(payload),
                canonical_json(provenance),
                canonical_json(evidence),
                authority,
                causation_id,
                correlation_id,
                idempotency_key,
                parent_event_hash,
                signature,
                event_hash,
            ),
        )
        conn.execute(
            """
            INSERT INTO chronos_receipts(sequence,event_id,event_hash,previous_chain_hash,chain_hash)
            VALUES(?,?,?,?,?)
            """,
            (
                receipt.sequence,
                receipt.event_id,
                receipt.event_hash,
                receipt.previous_chain_hash,
                receipt.chain_hash,
            ),
        )
        return event, receipt

    def enqueue_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        topic: str,
        payload: Dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO outbox(event_id,topic,payload_json,created_at) VALUES(?,?,?,?)",
            (event_id, topic, canonical_json(payload), created_at),
        )

    def command_result(self, conn: sqlite3.Connection, idempotency_key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not idempotency_key:
            return None
        row = conn.execute(
            "SELECT result_json FROM command_results WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def save_command_result(
        self,
        conn: sqlite3.Connection,
        *,
        idempotency_key: Optional[str],
        command_type: str,
        work_order_id: Optional[str],
        result: Dict[str, Any],
        created_at: str,
    ) -> None:
        if not idempotency_key:
            return
        conn.execute(
            """
            INSERT INTO command_results(idempotency_key,command_type,work_order_id,result_json,created_at)
            VALUES(?,?,?,?,?)
            """,
            (idempotency_key, command_type, work_order_id, canonical_json(result), created_at),
        )

    def pending_outbox(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE status='PENDING' ORDER BY outbox_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "outbox_id": int(r["outbox_id"]),
                "event_id": r["event_id"],
                "topic": r["topic"],
                "payload": json.loads(r["payload_json"]),
                "attempts": int(r["attempts"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def acknowledge_outbox(self, outbox_id: int) -> None:
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE outbox SET status='DELIVERED' WHERE outbox_id=? AND status='PENDING'",
                (outbox_id,),
            ).rowcount
            if changed != 1:
                raise ValueError(f"outbox item {outbox_id} is not pending")

    def verify_chronos(self) -> bool:
        rows = self.conn.execute(
            """
            SELECT e.*, c.previous_chain_hash, c.chain_hash
            FROM events e JOIN chronos_receipts c ON c.sequence=e.sequence
            ORDER BY e.sequence
            """
        ).fetchall()
        previous_event_hash = None
        previous_chain_hash = None
        for expected_sequence, row in enumerate(rows, start=1):
            if int(row["sequence"]) != expected_sequence:
                return False
            payload = {
                "sequence": int(row["sequence"]),
                "schema_version": row["schema_version"],
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "entity_id": row["entity_id"],
                "payload": json.loads(row["payload_json"]),
                "provenance": json.loads(row["provenance_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "authority": row["authority"],
                "causation_id": row["causation_id"],
                "correlation_id": row["correlation_id"],
                "idempotency_key": row["idempotency_key"],
                "parent_event_hash": row["parent_event_hash"],
                "signature": row["signature"],
            }
            event_id = sha256_json(payload)
            if event_id != row["event_id"]:
                return False
            event_dict = dict(payload)
            event_dict["event_id"] = event_id
            event_hash = sha256_json(event_dict)
            if event_hash != row["event_hash"]:
                return False
            if row["parent_event_hash"] != previous_event_hash:
                return False
            if row["previous_chain_hash"] != previous_chain_hash:
                return False
            receipt_core = {
                "sequence": expected_sequence,
                "event_id": event_id,
                "event_hash": event_hash,
                "previous_chain_hash": previous_chain_hash,
            }
            if sha256_json(receipt_core) != row["chain_hash"]:
                return False
            previous_event_hash = event_hash
            previous_chain_hash = row["chain_hash"]
        return True

    def verify_integrity(self) -> Dict[str, Any]:
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_rows = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        chronos_ok = self.verify_chronos()
        invalid_done = self.conn.execute(
            """
            SELECT w.work_order_id
            FROM work_orders w
            WHERE w.status='DONE'
              AND NOT EXISTS (
                SELECT 1 FROM verification_receipts v
                WHERE v.work_order_id=w.work_order_id AND v.passed=1
              )
            """
        ).fetchall()
        status_projection_mismatch = self.conn.execute(
            """
            SELECT w.work_order_id
            FROM work_orders w
            JOIN (
                SELECT t.work_order_id, t.to_status
                FROM work_order_transitions t
                JOIN (
                    SELECT work_order_id, MAX(transition_id) AS max_id
                    FROM work_order_transitions GROUP BY work_order_id
                ) x ON x.max_id=t.transition_id
            ) last ON last.work_order_id=w.work_order_id
            WHERE last.to_status <> w.status
            """
        ).fetchall()
        return {
            "sqlite_integrity": integrity == "ok",
            "foreign_keys": len(fk_rows) == 0,
            "chronos": chronos_ok,
            "done_requires_passed_receipt": len(invalid_done) == 0,
            "work_order_projection": len(status_projection_mismatch) == 0,
            "schema_version": self.schema_version(),
            "ok": (
                integrity == "ok"
                and len(fk_rows) == 0
                and chronos_ok
                and len(invalid_done) == 0
                and len(status_projection_mismatch) == 0
            ),
        }
