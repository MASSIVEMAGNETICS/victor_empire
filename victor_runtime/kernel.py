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
from .organs import DEVVILLE_CAPABILITY, DEVVILLE_ORGAN, DevVilleOrganRunner


ALLOWED_CAPABILITIES = {"artifact.write", DEVVILLE_CAPABILITY}
HOSTED_MODEL_PROVIDERS = {"openai", "anthropic", "google", "gemini", "meta", "mistral"}

DEFAULT_ORGANS = [
    ("dev-ville", "MASSIVEMAGNETICS/dev-ville", "software build + verification/evidence organ"),
    ("victor-prime-agent", "MASSIVEMAGNETICS/victor-prime-agent", "bounded execution organ"),
    ("autoresearch-win-rtx-victor", "MASSIVEMAGNETICS/autoresearch-win-rtx-victor", "research organ"),
    ("AGI", "MASSIVEMAGNETICS/AGI", "experimental intelligence organ"),
    ("victor-core", "MASSIVEMAGNETICS/victor-core", "legacy core/reference organ"),
    ("skills", "MASSIVEMAGNETICS/skills", "capability catalog organ"),
]


class PolicyError(RuntimeError):
    pass


class VictorKernel:
    """Canonical Victor control plane.

    External repositories can supply bounded capabilities, but they never own
    canonical state. SQLite plus the hash-chained event ledger are authoritative.
    """

    def __init__(
        self,
        *,
        data_dir: str | Path = ".victor",
        workspace: str | Path = "artifacts",
        devville_root: str | Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.workspace = Path(workspace)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = VictorDB(self.data_dir / "victor.db")
        self.events = EventLedger(self.db)
        self._register_default_organs()
        self.devville = DevVilleOrganRunner(
            data_dir=self.data_dir,
            workspace=self.workspace,
            devville_root=devville_root,
        )

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

    def _set_organ_status(self, name: str, status: str) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT status FROM organs WHERE name=?", (name,)).fetchone()
            if not row:
                raise KeyError(f"unknown organ: {name}")
            previous = row["status"]
            conn.execute(
                "UPDATE organs SET status=?, updated_at=? WHERE name=?",
                (status, utc_now(), name),
            )
        if previous != status:
            self.events.append(
                actor="victor-kernel",
                action="ORGAN_STATUS_CHANGED",
                entity_id=name,
                payload={"previous": previous, "status": status},
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
                payload={
                    "capability": capability,
                    "provider": provider_normalized,
                    "reason": "VICTOR_MODEL_SOVEREIGNTY",
                },
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

    def _fail_work_order(
        self,
        *,
        work_order_id: str,
        lease_id: str | None,
        reason: str,
    ) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE work_orders SET status='failed', updated_at=? WHERE id=?",
                (now, work_order_id),
            )
            if lease_id:
                conn.execute(
                    "UPDATE leases SET status='closed' WHERE id=? AND status='active'",
                    (lease_id,),
                )
        self.events.append(
            actor="victor-kernel",
            action="WORK_ORDER_FAILED",
            entity_id=work_order_id,
            payload={"lease_id": lease_id, "reason": reason[:4000]},
        )

    def _record_organ_run(
        self,
        *,
        job_id: str,
        work_order_id: str,
        lease_id: str,
        receipt_path: str,
        verification: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO organ_runs(
                    job_id, organ, work_order_id, lease_id, capability, status,
                    receipt_path, verification_json, error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    DEVVILLE_ORGAN,
                    work_order_id,
                    lease_id,
                    DEVVILLE_CAPABILITY,
                    "committed",
                    receipt_path,
                    json.dumps(verification, sort_keys=True),
                    None,
                    now,
                    now,
                ),
            )

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
            "organ": "local-worker",
            "inbox_id": inbox_id,
            "work_order_id": work_order_id,
            "lease_id": lease_id,
            "artifact": str(artifact),
            "verification": verification,
            "outcome_id": outcome_id,
            "event_chain": {"ok": chain_ok, "events": event_count, "error": chain_error},
        }

    def run_devville_closed_loop(self, goal: str) -> dict[str, Any]:
        inbox_id = self.capture(goal)
        work_order_id = self.create_work_order(
            goal,
            "Dev-Ville completes the project and Victor independently verifies the receipt and every artifact digest.",
        )
        lease_id: str | None = None
        try:
            lease_id = self.issue_lease(
                work_order_id=work_order_id,
                capability=DEVVILLE_CAPABILITY,
                issued_to=DEVVILLE_ORGAN,
                ttl_seconds=900,
            )
            lease = self._lease(lease_id)
            self.events.append(
                actor="victor-kernel",
                action="ORGAN_DISPATCH_REQUESTED",
                entity_id=work_order_id,
                payload={
                    "organ": DEVVILLE_ORGAN,
                    "capability": DEVVILLE_CAPABILITY,
                    "lease_id": lease_id,
                },
            )
            dispatch = self.devville.dispatch(
                work_order_id=work_order_id,
                lease=lease,
                directive=goal,
            )
            verification = dict(dispatch["verification"])
            verification["sha256"] = verification["receipt_sha256"]
            self.events.append(
                actor="verifier",
                action="ORGAN_RECEIPT_VERIFIED",
                entity_id=dispatch["job_id"],
                payload=verification,
            )
            outcome_id = self.commit_outcome(
                work_order_id=work_order_id,
                artifact=dispatch["receipt_path"],
                verification=verification,
            )
            self._record_organ_run(
                job_id=dispatch["job_id"],
                work_order_id=work_order_id,
                lease_id=lease_id,
                receipt_path=dispatch["receipt_path"],
                verification=verification,
            )
            self._set_organ_status(DEVVILLE_ORGAN, "ready")
            chain_ok, event_count, chain_error = self.events.verify()
            return {
                "organ": DEVVILLE_ORGAN,
                "inbox_id": inbox_id,
                "work_order_id": work_order_id,
                "lease_id": lease_id,
                "job_id": dispatch["job_id"],
                "receipt": dispatch["receipt_path"],
                "verification": verification,
                "outcome_id": outcome_id,
                "event_chain": {"ok": chain_ok, "events": event_count, "error": chain_error},
            }
        except Exception as exc:
            self._fail_work_order(
                work_order_id=work_order_id,
                lease_id=lease_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
            self._set_organ_status(DEVVILLE_ORGAN, "degraded")
            self.events.append(
                actor="victor-kernel",
                action="ORGAN_EXECUTION_FAILED",
                entity_id=work_order_id,
                payload={
                    "organ": DEVVILLE_ORGAN,
                    "capability": DEVVILLE_CAPABILITY,
                    "error": type(exc).__name__,
                    "message": str(exc)[:4000],
                },
            )
            raise

    def run_goal(self, goal: str, *, organ: str = "local") -> dict[str, Any]:
        normalized = organ.strip().lower()
        if normalized in {"local", "local-worker", "receipt"}:
            return self.run_closed_loop(goal)
        if normalized in {"dev-ville", "devville", "dev"}:
            return self.run_devville_closed_loop(goal)
        raise PolicyError(f"unknown execution organ: {organ}")

    def status(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            active = conn.execute(
                "SELECT * FROM work_orders WHERE status IN ('active','running') ORDER BY created_at LIMIT 1"
            ).fetchone()
            recent = conn.execute(
                "SELECT * FROM outcomes ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            organs = conn.execute("SELECT * FROM organs ORDER BY name").fetchall()
            organ_runs = conn.execute(
                "SELECT * FROM organ_runs ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            event_count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        chain_ok, _, chain_error = self.events.verify()
        return {
            "active_work_order": dict(active) if active else None,
            "recent_outcomes": [dict(row) for row in recent],
            "recent_organ_runs": [dict(row) for row in organ_runs],
            "organs": [dict(row) for row in organs],
            "events": event_count,
            "event_chain_ok": chain_ok,
            "event_chain_error": chain_error,
            "model_sovereignty": "fail-closed",
            "allowed_capabilities": sorted(ALLOWED_CAPABILITIES),
            "devville_adapter": {
                "available": self.devville.available(),
                "root": str(self.devville.devville_root),
                "capability": DEVVILLE_CAPABILITY,
            },
        }
