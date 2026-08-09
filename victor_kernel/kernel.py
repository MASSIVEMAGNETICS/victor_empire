from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .crypto import sign_json, verify_json_signature
from .models import (
    ALLOWED_TRANSITIONS,
    CapabilityLease,
    LeaseStatus,
    VerificationReceipt,
    WorkOrder,
    WorkOrderStatus,
)
from .storage import KernelStore


Clock = Callable[[], datetime]


class VictorKernel:
    """Canonical Victor control-plane kernel.

    Invariants:
    - state mutation and causal event append occur in the same SQLite transaction;
    - DONE is reachable only through a passing VerificationReceipt;
    - capability leases are signed, bounded, expiring and usage-limited;
    - idempotency keys make retried commands safe;
    - organs execute outside the kernel and receive work only through the outbox.
    """

    def __init__(
        self,
        db_path: str | Path = "victor_kernel.db",
        *,
        signing_key: bytes | None = None,
        kernel_id: str = "victor.kernel",
        clock: Clock | None = None,
    ):
        self.store = KernelStore(db_path)
        env_key = os.environ.get("VICTOR_KERNEL_SIGNING_KEY")
        self.signing_key = signing_key or (env_key.encode("utf-8") if env_key else None)
        if not self.signing_key:
            raise ValueError(
                "VictorKernel requires a signing key. Pass signing_key=... or set "
                "VICTOR_KERNEL_SIGNING_KEY."
            )
        if len(self.signing_key) < 16:
            raise ValueError("signing key must be at least 16 bytes")
        self.kernel_id = kernel_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self.store.close()

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _row_to_work_order(row: Any) -> WorkOrder:
        return WorkOrder(
            work_order_id=row["work_order_id"],
            goal=row["goal"],
            definition_of_done=row["definition_of_done"],
            status=WorkOrderStatus(row["status"]),
            requested_by=row["requested_by"],
            assigned_organ=row["assigned_organ"],
            priority=int(row["priority"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            correlation_id=row["correlation_id"],
            parent_work_order_id=row["parent_work_order_id"],
            inputs=json.loads(row["inputs_json"]),
            required_capabilities=json.loads(row["required_capabilities_json"]),
            verification_requirements=json.loads(row["verification_requirements_json"]),
        )

    def get_work_order(self, work_order_id: str) -> WorkOrder:
        row = self.store.conn.execute(
            "SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"unknown work order {work_order_id}")
        return self._row_to_work_order(row)

    def _transition(
        self,
        conn,
        *,
        work_order_id: str,
        to_status: WorkOrderStatus,
        actor_id: str,
        action: str,
        payload: Dict[str, Any],
        evidence: Dict[str, Any],
        authority: str,
        idempotency_key: Optional[str],
        outbox_topic: Optional[str] = None,
        outbox_payload: Optional[Dict[str, Any]] = None,
    ) -> tuple[WorkOrder, str]:
        row = conn.execute("SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown work order {work_order_id}")
        current = WorkOrderStatus(row["status"])
        if to_status not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"illegal work order transition {current.value} -> {to_status.value}")
        if to_status == WorkOrderStatus.DONE:
            raise ValueError("DONE may only be entered through record_verification()")
        now = self._now()
        event, _ = self.store.append_event(
            conn,
            occurred_at=now,
            recorded_at=now,
            actor_id=actor_id,
            action=action,
            entity_id=f"work_order:{work_order_id}",
            payload={"from_status": current.value, "to_status": to_status.value, **payload},
            provenance={"source": "victor_kernel"},
            evidence=evidence,
            authority=authority,
            causation_id=None,
            correlation_id=row["correlation_id"],
            idempotency_key=idempotency_key,
        )
        conn.execute(
            "UPDATE work_orders SET status=?, updated_at=? WHERE work_order_id=?",
            (to_status.value, now, work_order_id),
        )
        conn.execute(
            """
            INSERT INTO work_order_transitions(work_order_id,from_status,to_status,event_id,transitioned_at)
            VALUES(?,?,?,?,?)
            """,
            (work_order_id, current.value, to_status.value, event.event_id, now),
        )
        if outbox_topic:
            self.store.enqueue_outbox(
                conn,
                event_id=event.event_id,
                topic=outbox_topic,
                payload=outbox_payload or {},
                created_at=now,
            )
        updated = replace(self._row_to_work_order(row), status=to_status, updated_at=now)
        return updated, event.event_id

    def create_work_order(
        self,
        *,
        goal: str,
        definition_of_done: str,
        requested_by: str,
        assigned_organ: str,
        required_capabilities: List[str],
        verification_requirements: List[str],
        priority: int = 50,
        inputs: Optional[Dict[str, Any]] = None,
        parent_work_order_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> WorkOrder:
        if not goal.strip() or not definition_of_done.strip():
            raise ValueError("goal and definition_of_done are required")
        if not assigned_organ.strip():
            raise ValueError("assigned_organ is required")
        if not 0 <= priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        now = self._now()
        with self.store.transaction() as conn:
            existing = self.store.command_result(conn, idempotency_key)
            if existing:
                return self.get_work_order(existing["work_order_id"])
            work_order_id = self._new_id("wo")
            correlation_id = correlation_id or self._new_id("corr")
            event, _ = self.store.append_event(
                conn,
                occurred_at=now,
                recorded_at=now,
                actor_id=requested_by,
                action="work_order_created",
                entity_id=f"work_order:{work_order_id}",
                payload={
                    "goal": goal,
                    "definition_of_done": definition_of_done,
                    "assigned_organ": assigned_organ,
                    "priority": priority,
                    "required_capabilities": list(required_capabilities),
                    "verification_requirements": list(verification_requirements),
                },
                provenance={"source": "victor_kernel"},
                evidence={},
                authority="request_recorded",
                causation_id=None,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
            conn.execute(
                """
                INSERT INTO work_orders(
                    work_order_id,goal,definition_of_done,status,requested_by,assigned_organ,
                    priority,created_at,updated_at,correlation_id,parent_work_order_id,inputs_json,
                    required_capabilities_json,verification_requirements_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    work_order_id,
                    goal,
                    definition_of_done,
                    WorkOrderStatus.CAPTURED.value,
                    requested_by,
                    assigned_organ,
                    priority,
                    now,
                    now,
                    correlation_id,
                    parent_work_order_id,
                    json.dumps(inputs or {}, sort_keys=True),
                    json.dumps(list(required_capabilities), sort_keys=True),
                    json.dumps(list(verification_requirements), sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO work_order_transitions(work_order_id,from_status,to_status,event_id,transitioned_at)
                VALUES(?,?,?,?,?)
                """,
                (work_order_id, None, WorkOrderStatus.CAPTURED.value, event.event_id, now),
            )
            result = {"work_order_id": work_order_id}
            self.store.save_command_result(
                conn,
                idempotency_key=idempotency_key,
                command_type="create_work_order",
                work_order_id=work_order_id,
                result=result,
                created_at=now,
            )
        return self.get_work_order(work_order_id)

    def authorize_work_order(
        self,
        work_order_id: str,
        *,
        authorized_by: str,
        policy_evidence: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> WorkOrder:
        now = self._now()
        with self.store.transaction() as conn:
            existing = self.store.command_result(conn, idempotency_key)
            if existing:
                return self.get_work_order(existing["work_order_id"])
            updated, _ = self._transition(
                conn,
                work_order_id=work_order_id,
                to_status=WorkOrderStatus.AUTHORIZED,
                actor_id=authorized_by,
                action="work_order_authorized",
                payload={},
                evidence=policy_evidence,
                authority="policy_authorization",
                idempotency_key=idempotency_key,
            )
            self.store.save_command_result(
                conn,
                idempotency_key=idempotency_key,
                command_type="authorize_work_order",
                work_order_id=work_order_id,
                result={"work_order_id": work_order_id},
                created_at=now,
            )
            return updated

    def issue_lease(
        self,
        work_order_id: str,
        *,
        actor_id: str,
        organ_id: str,
        capabilities: List[str],
        scope: Dict[str, Any],
        ttl_seconds: int = 900,
        max_uses: int = 1,
        issuer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> CapabilityLease:
        if ttl_seconds <= 0 or max_uses <= 0:
            raise ValueError("ttl_seconds and max_uses must be positive")
        now_dt = self.clock().astimezone(timezone.utc)
        now = now_dt.isoformat()
        with self.store.transaction() as conn:
            existing = self.store.command_result(conn, idempotency_key)
            if existing:
                return self.get_lease(existing["lease_id"])
            row = conn.execute("SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown work order {work_order_id}")
            if WorkOrderStatus(row["status"]) != WorkOrderStatus.AUTHORIZED:
                raise ValueError("lease can only be issued for an AUTHORIZED work order")
            required = set(json.loads(row["required_capabilities_json"]))
            granted = set(capabilities)
            if not required.issubset(granted):
                missing = sorted(required - granted)
                raise ValueError(f"lease missing required capabilities: {missing}")
            if organ_id != row["assigned_organ"]:
                raise ValueError("lease organ does not match assigned organ")
            lease_id = self._new_id("lease")
            unsigned = {
                "lease_id": lease_id,
                "work_order_id": work_order_id,
                "actor_id": actor_id,
                "organ_id": organ_id,
                "capabilities": sorted(set(capabilities)),
                "scope": scope,
                "issued_at": now,
                "expires_at": (now_dt + timedelta(seconds=ttl_seconds)).isoformat(),
                "max_uses": max_uses,
                "uses": 0,
                "status": LeaseStatus.ACTIVE.value,
                "issuer": issuer or self.kernel_id,
            }
            lease_for_signing = CapabilityLease(
                signature="",
                **{**unsigned, "status": LeaseStatus.ACTIVE},
            )
            signature = sign_json(self.signing_key, lease_for_signing.signing_payload())
            lease = replace(lease_for_signing, signature=signature)
            updated, event_id = self._transition(
                conn,
                work_order_id=work_order_id,
                to_status=WorkOrderStatus.LEASED,
                actor_id=issuer or self.kernel_id,
                action="capability_lease_issued",
                payload={
                    "lease_id": lease_id,
                    "actor_id": actor_id,
                    "organ_id": organ_id,
                    "capabilities": unsigned["capabilities"],
                    "scope": scope,
                    "expires_at": unsigned["expires_at"],
                    "max_uses": max_uses,
                    "lease_signature": signature,
                },
                evidence={},
                authority="capability_authorization",
                idempotency_key=idempotency_key,
            )
            conn.execute(
                """
                INSERT INTO capability_leases(
                    lease_id,work_order_id,actor_id,organ_id,capabilities_json,scope_json,
                    issued_at,expires_at,max_uses,uses,status,issuer,signature,issued_event_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lease_id,
                    work_order_id,
                    actor_id,
                    organ_id,
                    json.dumps(unsigned["capabilities"], sort_keys=True),
                    json.dumps(scope, sort_keys=True),
                    unsigned["issued_at"],
                    unsigned["expires_at"],
                    max_uses,
                    0,
                    LeaseStatus.ACTIVE.value,
                    unsigned["issuer"],
                    signature,
                    event_id,
                ),
            )
            self.store.save_command_result(
                conn,
                idempotency_key=idempotency_key,
                command_type="issue_lease",
                work_order_id=work_order_id,
                result={"lease_id": lease_id},
                created_at=now,
            )
        return self.get_lease(lease_id)

    def get_lease(self, lease_id: str) -> CapabilityLease:
        row = self.store.conn.execute(
            "SELECT * FROM capability_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"unknown lease {lease_id}")
        return CapabilityLease(
            lease_id=row["lease_id"],
            work_order_id=row["work_order_id"],
            actor_id=row["actor_id"],
            organ_id=row["organ_id"],
            capabilities=json.loads(row["capabilities_json"]),
            scope=json.loads(row["scope_json"]),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            max_uses=int(row["max_uses"]),
            uses=int(row["uses"]),
            status=LeaseStatus(row["status"]),
            issuer=row["issuer"],
            signature=row["signature"],
        )

    def _validate_and_consume_lease(
        self,
        conn,
        *,
        lease_id: str,
        work_order_id: str,
        actor_id: str,
        capability: str,
    ) -> CapabilityLease:
        row = conn.execute("SELECT * FROM capability_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown lease {lease_id}")
        lease = CapabilityLease(
            lease_id=row["lease_id"],
            work_order_id=row["work_order_id"],
            actor_id=row["actor_id"],
            organ_id=row["organ_id"],
            capabilities=json.loads(row["capabilities_json"]),
            scope=json.loads(row["scope_json"]),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            max_uses=int(row["max_uses"]),
            uses=int(row["uses"]),
            status=LeaseStatus(row["status"]),
            issuer=row["issuer"],
            signature=row["signature"],
        )
        if not verify_json_signature(self.signing_key, lease.signing_payload(), lease.signature):
            raise PermissionError("lease signature is invalid")
        if lease.work_order_id != work_order_id or lease.actor_id != actor_id:
            raise PermissionError("lease subject does not match execution request")
        if capability not in lease.capabilities:
            raise PermissionError(f"capability {capability!r} is not leased")
        now = self.clock().astimezone(timezone.utc)
        if now >= datetime.fromisoformat(lease.expires_at):
            conn.execute(
                "UPDATE capability_leases SET status=? WHERE lease_id=?",
                (LeaseStatus.EXPIRED.value, lease_id),
            )
            raise PermissionError("lease expired")
        if lease.status != LeaseStatus.ACTIVE:
            raise PermissionError(f"lease is {lease.status.value}")
        if lease.uses >= lease.max_uses:
            conn.execute(
                "UPDATE capability_leases SET status=? WHERE lease_id=?",
                (LeaseStatus.EXHAUSTED.value, lease_id),
            )
            raise PermissionError("lease exhausted")
        new_uses = lease.uses + 1
        new_status = LeaseStatus.EXHAUSTED if new_uses >= lease.max_uses else LeaseStatus.ACTIVE
        conn.execute(
            "UPDATE capability_leases SET uses=?, status=? WHERE lease_id=?",
            (new_uses, new_status.value, lease_id),
        )
        return replace(lease, uses=new_uses, status=new_status)

    def start_execution(
        self,
        work_order_id: str,
        *,
        lease_id: str,
        actor_id: str,
        capability: str = "execute",
        idempotency_key: Optional[str] = None,
    ) -> WorkOrder:
        now = self._now()
        with self.store.transaction() as conn:
            existing = self.store.command_result(conn, idempotency_key)
            if existing:
                return self.get_work_order(existing["work_order_id"])
            lease = self._validate_and_consume_lease(
                conn,
                lease_id=lease_id,
                work_order_id=work_order_id,
                actor_id=actor_id,
                capability=capability,
            )
            updated, _ = self._transition(
                conn,
                work_order_id=work_order_id,
                to_status=WorkOrderStatus.RUNNING,
                actor_id=actor_id,
                action="execution_started",
                payload={"lease_id": lease_id, "capability": capability},
                evidence={"lease_signature": lease.signature},
                authority="leased_execution",
                idempotency_key=idempotency_key,
                outbox_topic=f"organ.{lease.organ_id}.execute",
                outbox_payload={
                    "work_order_id": work_order_id,
                    "lease_id": lease_id,
                    "actor_id": actor_id,
                    "capability": capability,
                },
            )
            self.store.save_command_result(
                conn,
                idempotency_key=idempotency_key,
                command_type="start_execution",
                work_order_id=work_order_id,
                result={"work_order_id": work_order_id},
                created_at=now,
            )
            return updated

    def submit_artifact(
        self,
        work_order_id: str,
        *,
        actor_id: str,
        artifact_sha256: str,
        artifact_uri: str,
        metadata: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> WorkOrder:
        if len(artifact_sha256) != 64 or any(c not in "0123456789abcdef" for c in artifact_sha256.lower()):
            raise ValueError("artifact_sha256 must be a 64-character hex digest")
        now = self._now()
        with self.store.transaction() as conn:
            existing = self.store.command_result(conn, idempotency_key)
            if existing:
                return self.get_work_order(existing["work_order_id"])
            conn.execute(
                "UPDATE work_orders SET last_artifact_sha256=? WHERE work_order_id=?",
                (artifact_sha256.lower(), work_order_id),
            )
            updated, _ = self._transition(
                conn,
                work_order_id=work_order_id,
                to_status=WorkOrderStatus.VERIFYING,
                actor_id=actor_id,
                action="artifact_submitted",
                payload={
                    "artifact_sha256": artifact_sha256.lower(),
                    "artifact_uri": artifact_uri,
                    "metadata": metadata or {},
                },
                evidence={"content_addressed": True},
                authority="execution_claim",
                idempotency_key=idempotency_key,
                outbox_topic="verifier.verify",
                outbox_payload={
                    "work_order_id": work_order_id,
                    "artifact_sha256": artifact_sha256.lower(),
                    "artifact_uri": artifact_uri,
                },
            )
            self.store.save_command_result(
                conn,
                idempotency_key=idempotency_key,
                command_type="submit_artifact",
                work_order_id=work_order_id,
                result={"work_order_id": work_order_id},
                created_at=now,
            )
            return updated

    def record_verification(
        self,
        work_order_id: str,
        *,
        verifier_id: str,
        artifact_sha256: str,
        passed: bool,
        checks: List[Dict[str, Any]],
        evidence_refs: List[str],
        environment_fingerprint: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> VerificationReceipt:
        now = self._now()
        with self.store.transaction() as conn:
            existing = self.store.command_result(conn, idempotency_key)
            if existing:
                return self.get_verification_receipt(existing["receipt_id"])
            row = conn.execute("SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown work order {work_order_id}")
            if WorkOrderStatus(row["status"]) != WorkOrderStatus.VERIFYING:
                raise ValueError("verification is only valid from VERIFYING")
            expected_artifact = row["last_artifact_sha256"]
            if expected_artifact != artifact_sha256.lower():
                raise ValueError("verification receipt artifact hash does not match submitted artifact")
            receipt_id = self._new_id("vr")
            unsigned = {
                "receipt_id": receipt_id,
                "work_order_id": work_order_id,
                "artifact_sha256": artifact_sha256.lower(),
                "verifier_id": verifier_id,
                "passed": bool(passed),
                "checks": checks,
                "evidence_refs": evidence_refs,
                "executed_at": now,
                "environment_fingerprint": environment_fingerprint,
            }
            signature = sign_json(self.signing_key, unsigned)
            receipt = VerificationReceipt(signature=signature, **unsigned)
            current = WorkOrderStatus(row["status"])
            target = WorkOrderStatus.DONE if passed else WorkOrderStatus.REWORK
            event, _ = self.store.append_event(
                conn,
                occurred_at=now,
                recorded_at=now,
                actor_id=verifier_id,
                action="verification_passed" if passed else "verification_failed",
                entity_id=f"work_order:{work_order_id}",
                payload={
                    "from_status": current.value,
                    "to_status": target.value,
                    "receipt_id": receipt_id,
                    "artifact_sha256": artifact_sha256.lower(),
                    "passed": bool(passed),
                },
                provenance={"source": "victor_kernel.verifier"},
                evidence={"checks": checks, "evidence_refs": evidence_refs},
                authority="verification_gate",
                causation_id=None,
                correlation_id=row["correlation_id"],
                idempotency_key=idempotency_key,
                signature=signature,
            )
            conn.execute(
                """
                INSERT INTO verification_receipts(
                    receipt_id,work_order_id,artifact_sha256,verifier_id,passed,checks_json,
                    evidence_refs_json,executed_at,environment_fingerprint_json,signature,event_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    receipt_id,
                    work_order_id,
                    artifact_sha256.lower(),
                    verifier_id,
                    1 if passed else 0,
                    json.dumps(checks, sort_keys=True),
                    json.dumps(evidence_refs, sort_keys=True),
                    now,
                    json.dumps(environment_fingerprint, sort_keys=True),
                    signature,
                    event.event_id,
                ),
            )
            conn.execute(
                "UPDATE work_orders SET status=?, updated_at=? WHERE work_order_id=?",
                (target.value, now, work_order_id),
            )
            conn.execute(
                """
                INSERT INTO work_order_transitions(work_order_id,from_status,to_status,event_id,transitioned_at)
                VALUES(?,?,?,?,?)
                """,
                (work_order_id, current.value, target.value, event.event_id, now),
            )
            self.store.save_command_result(
                conn,
                idempotency_key=idempotency_key,
                command_type="record_verification",
                work_order_id=work_order_id,
                result={"receipt_id": receipt_id},
                created_at=now,
            )
            return receipt

    def get_verification_receipt(self, receipt_id: str) -> VerificationReceipt:
        row = self.store.conn.execute(
            "SELECT * FROM verification_receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"unknown verification receipt {receipt_id}")
        return VerificationReceipt(
            receipt_id=row["receipt_id"],
            work_order_id=row["work_order_id"],
            artifact_sha256=row["artifact_sha256"],
            verifier_id=row["verifier_id"],
            passed=bool(row["passed"]),
            checks=json.loads(row["checks_json"]),
            evidence_refs=json.loads(row["evidence_refs_json"]),
            executed_at=row["executed_at"],
            environment_fingerprint=json.loads(row["environment_fingerprint_json"]),
            signature=row["signature"],
        )

    def verify_receipt_signature(self, receipt: VerificationReceipt) -> bool:
        return verify_json_signature(self.signing_key, receipt.signing_payload(), receipt.signature)

    def recover(self) -> Dict[str, Any]:
        integrity = self.store.verify_integrity()
        if not integrity["ok"]:
            raise RuntimeError(f"kernel integrity verification failed: {integrity}")
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE capability_leases
                SET status=?
                WHERE status=? AND expires_at <= ?
                """,
                (LeaseStatus.EXPIRED.value, LeaseStatus.ACTIVE.value, now),
            )
        pending = self.store.conn.execute(
            """
            SELECT work_order_id,status,assigned_organ,updated_at
            FROM work_orders
            WHERE status NOT IN ('DONE','CANCELLED')
            ORDER BY priority DESC, created_at
            """
        ).fetchall()
        return {
            "integrity": self.store.verify_integrity(),
            "pending_work_orders": [dict(row) for row in pending],
            "pending_outbox": self.store.pending_outbox(),
        }
