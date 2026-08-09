"""SQLite WAL transactional substrate for Victor state and causal history."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .chronos import ChronosReceipt, canonical_json
from .protocol import Artifact, CapabilityLease, Informatron, VerificationReceipt, WorkOrder


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_hash TEXT NOT NULL UNIQUE,
    previous_event_hash TEXT,
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
    idempotency_key TEXT UNIQUE,
    signature TEXT
);

CREATE TABLE IF NOT EXISTS chronos_receipts (
    sequence INTEGER PRIMARY KEY REFERENCES events(sequence),
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    event_hash TEXT NOT NULL,
    previous_chain_hash TEXT,
    chain_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    goal TEXT NOT NULL,
    definition_of_done TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    assigned_organ TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    deadline TEXT,
    inputs_json TEXT NOT NULL,
    required_capabilities_json TEXT NOT NULL,
    verification_requirements_json TEXT NOT NULL,
    parent_work_order_id TEXT,
    correlation_id TEXT,
    create_idempotency_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS work_order_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_leases (
    lease_id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    actor_id TEXT NOT NULL,
    organ_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    lease_json TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    sha256 TEXT NOT NULL,
    artifact_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_receipts (
    receipt_id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    receipt_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organ_registry (
    organ_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    work_order_id TEXT REFERENCES work_orders(work_order_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    command_json TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS command_results (
    command_id TEXT PRIMARY KEY REFERENCES commands(command_id),
    result_json TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    work_order_id TEXT REFERENCES work_orders(work_order_id),
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    destination TEXT NOT NULL,
    message_json TEXT NOT NULL,
    dispatched_at TEXT
);
"""


class VictorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(SCHEMA_SQL)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def append_event(connection: sqlite3.Connection, event: Informatron, receipt: ChronosReceipt) -> int:
        cursor = connection.execute(
            """INSERT INTO events (
                event_id, event_hash, previous_event_hash, schema_version,
                occurred_at, recorded_at, actor_id, action, entity_id,
                payload_json, provenance_json, evidence_json, authority,
                causation_id, correlation_id, idempotency_key, signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id, event.event_hash, event.previous_event_hash, event.schema_version,
                event.occurred_at, event.recorded_at, event.actor_id, event.action, event.entity_id,
                canonical_json(event.payload), canonical_json(event.provenance), canonical_json(event.evidence),
                event.authority, event.causation_id, event.correlation_id, event.idempotency_key, event.signature,
            ),
        )
        sequence = int(cursor.lastrowid)
        if sequence != receipt.sequence:
            raise RuntimeError("Chronos sequence diverged from event sequence")
        connection.execute(
            "INSERT INTO chronos_receipts VALUES (?, ?, ?, ?, ?)",
            (sequence, receipt.event_id, receipt.event_hash, receipt.previous_chain_hash, receipt.chain_hash),
        )
        return sequence

    @staticmethod
    def insert_work_order(connection: sqlite3.Connection, work_order: WorkOrder, idempotency_key: str) -> None:
        connection.execute(
            """INSERT INTO work_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                work_order.work_order_id, work_order.schema_version, work_order.goal,
                work_order.definition_of_done, work_order.status.value, work_order.requested_by,
                work_order.assigned_organ, work_order.priority, work_order.created_at, work_order.deadline,
                canonical_json(work_order.inputs), canonical_json(work_order.required_capabilities),
                canonical_json(work_order.verification_requirements), work_order.parent_work_order_id,
                work_order.correlation_id, idempotency_key,
            ),
        )

    @staticmethod
    def insert_lease(connection: sqlite3.Connection, lease: CapabilityLease) -> None:
        connection.execute(
            "INSERT INTO capability_leases VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (lease.lease_id, lease.work_order_id, lease.actor_id, lease.organ_id, lease.capability, canonical_json(lease.to_dict()), lease.status),
        )

    @staticmethod
    def insert_artifact(connection: sqlite3.Connection, artifact: Artifact) -> None:
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?)",
            (artifact.artifact_id, artifact.work_order_id, artifact.sha256, canonical_json(artifact.to_dict())),
        )

    @staticmethod
    def insert_verification(connection: sqlite3.Connection, receipt: VerificationReceipt) -> None:
        connection.execute(
            "INSERT INTO verification_receipts VALUES (?, ?, ?, ?, ?)",
            (receipt.receipt_id, receipt.work_order_id, receipt.artifact_id, int(receipt.passed), canonical_json(receipt.to_dict())),
        )

    def journal_mode(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
