from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import VictorDB
from .events import EventLedger, utc_now


ALLOWED_CAPABILITIES = {"artifact.write"}
HOSTED_MODEL_PROVIDERS = {"openai", "anthropic", "google", "gemini", "meta", "mistral"}

DEFAULT_ORGANS = [
    ("dev-ville", "MASSIVEMAGNETICS/dev-ville", "verification/evidence organ"),
    ("victor-prime-agent", "MASSIVEMAGNETICS/victor-prime-agent", "bounded execution organ"),
    ("autoresearch-win-rtx-victor", "MASSIVEMAGNETICS/autoresearch-win-rtx-victor", "research organ"),
    ("AGI", "MASSIVEMAGNETICS/AGI", "experimental intelligence organ"),
    ("victor-core", "MASSIVEMAGNETICS/victor-core", "legacy core/reference organ"),
    ("skills", "MASSIVEMAGNETICS/skills", "capability catalog organ"),
]


class PolicyError(RuntimeError):
    pass


class VictorKernel:
    """Canonical control plane.

    External repositories can supply capabilities, but they never own canonical
    state. SQLite plus the event ledger are authoritative.
    """

    def __init__(
        self,
        *,
        data_dir: str | Path = ".victor",
        workspace: str | Path = "artifacts",
    ) -> None:
        self.data_dir = Path(data_dir)
        self.workspace = Path(workspace)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = VictorDB(self.data_dir / "victor.db")
        self.events = EventLedger(self.db)
        self._register_default_organs()

    def _register_default_organs(self) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            for name, repo, role in DEFAULT_ORGANS:
                conn.execute(
                    """
                    INSERT INTO organs(name, repo, role, status, authority, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(name) DO UPDATE SET
                      repo=excluded.repo,
                      role=excluded.role,
                      updated_at=excluded.updated_at
                    """,
                    (name, repo, role, "adapter_pending", "capability-only", now),
                )

    def capture(self, text: str) -> str:
        clean = text.strip()
        if not clean:
            raise ValueError("capture text cannot be empty")
        item_id = f"in-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO inbox(id, text, status, created_at) VALUES(?,?,?,?)",
                (item_id, clean, "captured", now),
            )
        self.events.append(
            actor="bando",
            action="INPUT_CAPTURED",
            entity_id=item_id,
            payload={"text": clean},
        )
        return item_id

    def _active_work_order(self) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_orders WHERE status IN ('active','running') ORDER BY created_at LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def create_work_order(self, goal: str, definition_of_done: str | None = None) -> str:
        clean = goal.strip()
        if not clean:
            raise ValueError("goal cannot be empty")
        active = self._active_work_order()
        if active:
            raise PolicyError(f"active work order exists: {active['id']}")
        work_order_id = f"wo-{uuid.uuid4().hex}"
        now = utc_now()
        dod = definition_of_done or "Produce one non-empty verified local artifact."
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO work_orders(id, goal, definition_of_done, status, created_at, updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (work_order_id, clean, dod, "active", now, now),
            )
        self.events.append(
            actor="victor-kernel",
            action="WORK_ORDER_CREATED",
            entity_id=work_order_id,
            payload={"goal": clean, "definition_of_done": dod},
        )
        return work_order_id

    def govern(self, capability: str, *, provider: str | None = None) -> None:
        provider_normalized = (provider or "").strip().lower()
        if provider_normalized in HOSTED_MODEL_PROVIDERS:
            self.events.append(
                actor="ethica-governor",
                action="POLICY_DENIED",
                entity_id=None,
                payload={"capability": capability, "provider": provider_normalized, "reason": "VICTOR_MODEL_SOVEREIGNTY"},
            )
            raise PolicyError("hosted model inference is disabled by VICTOR_MODEL_SOVEREIGNTY")
        if capability not in ALLOWED_CAPABILITIES:
            self.events.append(
                actor="ethica-governor",
                action="POLICY_DENIED",
                entity_id=None,
                payload={"capability": capability, "reason": "capability not allowlisted"},
            )
            raise PolicyError(f"capability not allowlisted: {capability}")

    def issue_lease(
        self,
        *,
        work_order_id: str,
        capability: str,
        issued_to: str = "local-worker",
        ttl_seconds: int = 300,
    ) -> str:
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("ttl_seconds must be between 1 and 3600")
        self.govern(capability)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        lease_id = f"lease-{uuid.uuid4().hex}"
        with self.db.connect() as conn:
            wo = conn.execute("SELECT status FROM work_orders WHERE id=?", (work_order_id,)).fetchone()
            if not wo:
                raise KeyError(f"unknown work order: {work_order_id}")
            if wo["status"] not in {"active", "running"}:
                raise PolicyError(f"work order is not executable: {wo['status']}")
            conn.execute(
                """
                INSERT INTO leases(id, work_order_id, capability, issued_to, expires_at, status, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (lease_id, work_order_id, capability, issued_to, expires, "active", now),
            )
        self.events.append(
            actor="ethica-governor",
            action="LEASE_ISSUED",
            entity_id=lease_id,
            payload={
                "work_order_id": work_order_id,
                "capability": capability,
                "issued_to": issued_to,
                "expires_at": expires,
            },
        )
        return lease_id

    def _lease(self, lease_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown lease: {lease_id}")
        lease = dict(row)
        if lease["status"] != "active":
            raise PolicyError(f"lease is not active: {lease['status']}")
        if datetime.fromisoformat(lease["expires_at"]) <= datetime.now(timezone.utc):
            with self.db.connect() as conn:
                conn.execute("UPDATE leases SET status='expired' WHERE id=?", (lease_id,))
            raise PolicyError("lease expired")
        return lease

    def execute_artifact_write(self, *, work_order_id: str, lease_id: str) -> Path:
        lease = self._lease(lease_id)
        if lease["work_order_id"] != work_order_id:
            raise PolicyError("lease/work-order mismatch")
        if lease["capability"] != "artifact.write":
            raise PolicyError("lease does not authorize artifact.write")

        with self.db.connect() as conn:
            wo = conn.execute("SELECT * FROM work_orders WHERE id=?", (work_order_id,)).fetchone()
            if not wo:
                raise KeyError(f"unknown work order: {work_order_id}")
            conn.execute(
                "UPDATE work_orders SET status='running', updated_at=? WHERE id=?",
                (utc_now(), work_order_id),
            )

        target = (self.workspace / f"{work_order_id}.md").resolve()
        root = self.workspace.resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise PolicyError("artifact path escaped workspace")

        content = (
            f"# Victor Work Artifact\n\n"
            f"- Work order: `{work_order_id}`\n"
            f"- Goal: {wo['goal']}\n"
            f"- Definition of done: {wo['definition_of_done']}\n"
            f"- Executor: local-worker\n\n"
            "## Execution receipt\n\n"
            "This artifact was produced by the bounded `artifact.write` capability. "
            "It is intentionally local-only and contains no hosted-model output.\n"
        )
        target.write_text(content, encoding="utf-8")
        self.events.append(
            actor="local-worker",
            action="EXECUTION_FINISHED",
            entity_id=work_order_id,
            payload={"capability": "artifact.write", "artifact_path": str(target)},
        )
        return target

    def verify_artifact(self, *, work_order_id: str, artifact: str | Path) -> dict[str, Any]:
        path = Path(artifact).resolve()
        root = self.workspace.resolve()
        in_workspace = os.path.commonpath([str(root), str(path)]) == str(root)
        exists = path.exists() and path.is_file()
        data = path.read_bytes() if exists else b""
        non_empty = bool(data.strip())
        contains_work_order = work_order_id.encode("utf-8") in data
        digest = hashlib.sha256(data).hexdigest() if exists else None
        ok = in_workspace and exists and non_empty and contains_work_order
        receipt = {
            "ok": ok,
            "in_workspace": in_workspace,
            "exists": exists,
            "non_empty": non_empty,
            "contains_work_order": contains_work_order,
            "sha256": digest,
            "bytes": len(data),
        }
        self.events.append(
            actor="verifier",
            action="VERIFICATION_PASSED" if ok else "VERIFICATION_FAILED",
            entity_id=work_order_id,
            payload=receipt,
        )
        return receipt

    def commit_outcome(
        self,
        *,
        work_order_id: str,
        artifact: str | Path,
        verification: dict[str, Any],
    ) -> str:
        outcome_id = f"out-{uuid.uuid4().hex}"
        status = "verified" if verification.get("ok") else "failed"
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO outcomes(id, work_order_id, status, artifact_path, artifact_sha256, verification_json, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    outcome_id,
                    work_order_id,
                    status,
                    str(Path(artifact)),
                    verification.get("sha256"),
                    json.dumps(verification, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                "UPDATE work_orders SET status=?, updated_at=? WHERE id=?",
                ("completed" if status == "verified" else "failed", now, work_order_id),
            )
            conn.execute(
                "UPDATE leases SET status='closed' WHERE work_order_id=? AND status='active'",
                (work_order_id,),
            )
        self.events.append(
            actor="victor-kernel",
            action="OUTCOME_COMMITTED",
            entity_id=outcome_id,
            payload={"work_order_id": work_order_id, "status": status, "verification": verification},
        )
        return outcome_id

    def run_closed_loop(self, goal: str) -> dict[str, Any]:
        inbox_id = self.capture(goal)
        work_order_id = self.create_work_order(goal)
        lease_id = self.issue_lease(work_order_id=work_order_id, capability="artifact.write")
        artifact = self.execute_artifact_write(work_order_id=work_order_id, lease_id=lease_id)
        verification = self.verify_artifact(work_order_id=work_order_id, artifact=artifact)
        outcome_id = self.commit_outcome(
            work_order_id=work_order_id,
            artifact=artifact,
            verification=verification,
        )
        chain_ok, event_count, chain_error = self.events.verify()
        return {
            "inbox_id": inbox_id,
            "work_order_id": work_order_id,
            "lease_id": lease_id,
            "artifact": str(artifact),
            "verification": verification,
            "outcome_id": outcome_id,
            "event_chain": {"ok": chain_ok, "events": event_count, "error": chain_error},
        }

    def status(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            active = conn.execute(
                "SELECT * FROM work_orders WHERE status IN ('active','running') ORDER BY created_at LIMIT 1"
            ).fetchone()
            recent = conn.execute(
                "SELECT * FROM outcomes ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            organs = conn.execute("SELECT * FROM organs ORDER BY name").fetchall()
            event_count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        chain_ok, _, chain_error = self.events.verify()
        return {
            "active_work_order": dict(active) if active else None,
            "recent_outcomes": [dict(row) for row in recent],
            "organs": [dict(row) for row in organs],
            "events": event_count,
            "event_chain_ok": chain_ok,
            "event_chain_error": chain_error,
            "model_sovereignty": "fail-closed",
            "allowed_capabilities": sorted(ALLOWED_CAPABILITIES),
        }
