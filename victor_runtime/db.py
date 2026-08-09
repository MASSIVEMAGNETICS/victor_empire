from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_id TEXT,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    definition_of_done TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_work_order
ON work_orders(status)
WHERE status IN ('active', 'running');

CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    issued_to TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(work_order_id) REFERENCES work_orders(id)
);

CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    artifact_path TEXT,
    artifact_sha256 TEXT,
    verification_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(work_order_id) REFERENCES work_orders(id)
);

CREATE TABLE IF NOT EXISTS organs (
    name TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    authority TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organ_runs (
    job_id TEXT PRIMARY KEY,
    organ TEXT NOT NULL,
    work_order_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    status TEXT NOT NULL,
    receipt_path TEXT,
    verification_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(work_order_id) REFERENCES work_orders(id),
    FOREIGN KEY(lease_id) REFERENCES leases(id),
    FOREIGN KEY(organ) REFERENCES organs(name)
);

CREATE INDEX IF NOT EXISTS organ_runs_by_work_order
ON organ_runs(work_order_id, created_at);
"""


class VictorDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
