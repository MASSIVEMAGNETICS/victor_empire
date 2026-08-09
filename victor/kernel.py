"""Transactional Victor control plane.

An acknowledged transition always commits its Informatron, Chronos receipt,
projection update, and dispatch intent in one SQLite transaction.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Callable, Mapping

from .chronos import build_informatron, build_receipt, event_hash_payload, sha256_json
from .persistence import VictorStore
from .protocol import Artifact, CapabilityLease, VerificationReceipt, WorkOrder, WorkOrderStatus, new_id, utc_now
from .state_machine import require_transition


class VictorKernel:
    def __init__(self, database_path: str, fault_hook: Callable[[str], None] | None = None):
        self.store = VictorStore(database_path)
        self._fault_hook = fault_hook

    def _checkpoint(self, name: str) -> None:
        if self._fault_hook:
            self._fault_hook(name)

    @staticmethod
    def _chain_head(connection: sqlite3.Connection) -> tuple[int, str | None, str | None]:
        row = connection.execute(
            """SELECT e.sequence, e.event_hash, c.chain_hash
               FROM events e JOIN chronos_receipts c USING(sequence)
               ORDER BY e.sequence DESC LIMIT 1"""
        ).fetchone()
        return (0, None, None) if row is None else (int(row[0]), str(row[1]), str(row[2]))

    def _append(
        self,
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        action: str,
        entity_id: str,
        payload: Mapping[str, Any],
        evidence: Mapping[str, Any] | None = None,
        authority: str,
        idempotency_key: str,
        correlation_id: str | None,
        causation_id: str | None = None,
    ) -> str:
        last_sequence, previous_event_hash, previous_chain_hash = self._chain_head(connection)
        event = build_informatron(
            actor_id=actor_id,
            action=action,
            entity_id=entity_id,
            payload=payload,
            provenance={"component": "victor.kernel"},
            evidence=evidence or {},
            authority=authority,
            previous_event_hash=previous_event_hash,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        receipt = build_receipt(event, last_sequence + 1, previous_chain_hash)
        self.store.append_event(connection, event, receipt)
        return event.event_id

    def create_work_order(self, work_order: WorkOrder, *, idempotency_key: str) -> WorkOrder:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM work_orders WHERE create_idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return self._row_to_work_order(existing)
            event_id = self._append(
                connection,
                actor_id=work_order.requested_by,
                action="work_order.captured",
                entity_id=work_order.work_order_id,
                payload=work_order.to_dict(),
                authority="human_request",
                idempotency_key=idempotency_key,
                correlation_id=work_order.correlation_id,
            )
            self._checkpoint("create.after_event")
            self.store.insert_work_order(connection, work_order, idempotency_key)
            connection.execute(
                "INSERT INTO work_order_transitions(work_order_id, from_status, to_status, event_id, reason, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                (work_order.work_order_id, work_order.status.value, event_id, "captured", utc_now()),
            )
        return work_order

    def transition(
        self,
        work_order_id: str,
        target: WorkOrderStatus,
        *,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        authority: str = "kernel_policy",
    ) -> WorkOrder:
        with self.store.transaction() as connection:
            prior = connection.execute("SELECT event_id FROM events WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if prior:
                return self._get_work_order(connection, work_order_id)
            current = self._get_work_order(connection, work_order_id)
            require_transition(current.status, target)
            specialized = {
                WorkOrderStatus.LEASED: "issue_lease",
                WorkOrderStatus.RUNNING: "start_execution",
                WorkOrderStatus.VERIFYING: "record_artifact",
                WorkOrderStatus.DONE: "record_verification",
            }
            if target in specialized:
                raise ValueError(f"{target.value} may only be entered through {specialized[target]}")
            event_id = self._append(
                connection,
                actor_id=actor_id,
                action="work_order.transitioned",
                entity_id=work_order_id,
                payload={"from": current.status.value, "to": target.value, "reason": reason},
                authority=authority,
                idempotency_key=idempotency_key,
                correlation_id=current.correlation_id,
            )
            self._checkpoint("transition.after_event")
            self._set_status(connection, current, target, event_id, reason)
            return replace(current, status=target)

    def issue_lease(
        self,
        work_order_id: str,
        *,
        actor_id: str,
        organ_id: str,
        capability: str,
        scope: Mapping[str, Any],
        issued_by: str,
        duration_seconds: int,
        idempotency_key: str,
    ) -> CapabilityLease:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        now = datetime.now(timezone.utc)
        with self.store.transaction() as connection:
            existing_event = connection.execute(
                "SELECT entity_id, action FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_event:
                if existing_event["action"] != "capability_lease.issued":
                    raise ValueError("idempotency_key was already used for a different command")
                row = connection.execute(
                    "SELECT lease_json FROM capability_leases WHERE lease_id = ?", (existing_event["entity_id"],)
                ).fetchone()
                if row is None:
                    raise RuntimeError("lease event exists without its atomic lease projection")
                return CapabilityLease(**json.loads(row["lease_json"]))
            current = self._get_work_order(connection, work_order_id)
            require_transition(current.status, WorkOrderStatus.LEASED)
            lease = CapabilityLease(
                work_order_id=work_order_id,
                actor_id=actor_id,
                organ_id=organ_id,
                capability=capability,
                scope=dict(scope),
                issued_by=issued_by,
                issued_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=duration_seconds)).isoformat(),
            )
            event_id = self._append(
                connection,
                actor_id=issued_by,
                action="capability_lease.issued",
                entity_id=lease.lease_id,
                payload=lease.to_dict(),
                authority="lease_issuer",
                idempotency_key=idempotency_key,
                correlation_id=current.correlation_id,
            )
            self.store.insert_lease(connection, lease)
            self._set_status(connection, current, WorkOrderStatus.LEASED, event_id, "bounded capability issued")
            connection.execute(
                "INSERT INTO outbox(event_id, destination, message_json) VALUES (?, ?, ?)",
                (event_id, organ_id, json.dumps({"work_order_id": work_order_id, "lease_id": lease.lease_id}, sort_keys=True)),
            )
            self._checkpoint("lease.before_commit")
            return lease

    def start_execution(
        self,
        work_order_id: str,
        lease_id: str,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkOrder:
        """Consume one authorized lease use and enter RUNNING atomically."""
        with self.store.transaction() as connection:
            existing_event = connection.execute(
                "SELECT action, entity_id FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_event:
                if existing_event["action"] != "execution.started" or existing_event["entity_id"] != work_order_id:
                    raise ValueError("idempotency_key was already used for a different command")
                return self._get_work_order(connection, work_order_id)

            current = self._get_work_order(connection, work_order_id)
            require_transition(current.status, WorkOrderStatus.RUNNING)
            row = connection.execute(
                "SELECT lease_json, uses, status FROM capability_leases WHERE lease_id = ? AND work_order_id = ?",
                (lease_id, work_order_id),
            ).fetchone()
            if row is None:
                raise ValueError("lease does not exist for this WorkOrder")
            lease = CapabilityLease(**json.loads(row["lease_json"]))
            if lease.actor_id != actor_id:
                raise PermissionError("lease actor does not match execution actor")
            if row["status"] != "ACTIVE" or int(row["uses"]) >= lease.max_uses or not lease.active_at(utc_now()):
                raise PermissionError("lease is expired, exhausted, or inactive")

            event_id = self._append(
                connection,
                actor_id=actor_id,
                action="execution.started",
                entity_id=work_order_id,
                payload={"work_order_id": work_order_id, "lease_id": lease_id, "organ_id": lease.organ_id},
                evidence={"lease_id": lease_id, "capability": lease.capability},
                authority="capability_lease",
                idempotency_key=idempotency_key,
                correlation_id=current.correlation_id,
            )
            new_uses = int(row["uses"]) + 1
            new_status = "EXHAUSTED" if new_uses >= lease.max_uses else "ACTIVE"
            connection.execute(
                "UPDATE capability_leases SET uses = ?, status = ? WHERE lease_id = ?",
                (new_uses, new_status, lease_id),
            )
            self._set_status(connection, current, WorkOrderStatus.RUNNING, event_id, "valid capability lease consumed")
            return replace(current, status=WorkOrderStatus.RUNNING)

    def record_artifact(
        self,
        artifact: Artifact,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkOrder:
        with self.store.transaction() as connection:
            existing_event = connection.execute(
                "SELECT action, entity_id FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_event:
                if existing_event["action"] != "artifact.produced" or existing_event["entity_id"] != artifact.artifact_id:
                    raise ValueError("idempotency_key was already used for a different command")
                return self._get_work_order(connection, artifact.work_order_id)
            current = self._get_work_order(connection, artifact.work_order_id)
            require_transition(current.status, WorkOrderStatus.VERIFYING)
            event_id = self._append(
                connection,
                actor_id=actor_id,
                action="artifact.produced",
                entity_id=artifact.artifact_id,
                payload=artifact.to_dict(),
                evidence={"sha256": artifact.sha256},
                authority="organ_observation",
                idempotency_key=idempotency_key,
                correlation_id=current.correlation_id,
            )
            self.store.insert_artifact(connection, artifact)
            self._set_status(connection, current, WorkOrderStatus.VERIFYING, event_id, "artifact awaiting independent verification")
            return replace(current, status=WorkOrderStatus.VERIFYING)

    def record_verification(self, receipt: VerificationReceipt, *, idempotency_key: str) -> WorkOrder:
        with self.store.transaction() as connection:
            existing_event = connection.execute(
                "SELECT action, entity_id FROM events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_event:
                if existing_event["action"] != "verification.completed" or existing_event["entity_id"] != receipt.receipt_id:
                    raise ValueError("idempotency_key was already used for a different command")
                return self._get_work_order(connection, receipt.work_order_id)
            current = self._get_work_order(connection, receipt.work_order_id)
            if current.status is not WorkOrderStatus.VERIFYING:
                raise ValueError("verification is only valid while WorkOrder is VERIFYING")
            artifact = connection.execute(
                "SELECT sha256 FROM artifacts WHERE artifact_id = ? AND work_order_id = ?",
                (receipt.artifact_id, receipt.work_order_id),
            ).fetchone()
            if artifact is None or artifact[0] != receipt.artifact_hash:
                raise ValueError("verification receipt does not match the persisted artifact")
            target = WorkOrderStatus.DONE if receipt.passed else WorkOrderStatus.REWORK
            require_transition(current.status, target)
            event_id = self._append(
                connection,
                actor_id=receipt.verifier_id,
                action="verification.completed",
                entity_id=receipt.receipt_id,
                payload={"work_order_id": receipt.work_order_id, "artifact_id": receipt.artifact_id, "passed": receipt.passed},
                evidence=receipt.to_dict(),
                authority="independent_verifier",
                idempotency_key=idempotency_key,
                correlation_id=current.correlation_id,
            )
            self.store.insert_verification(connection, receipt)
            self._checkpoint("verification.after_receipt")
            self._set_status(connection, current, target, event_id, "verification passed" if receipt.passed else "verification failed")
            return replace(current, status=target)

    def get_work_order(self, work_order_id: str) -> WorkOrder:
        with self.store.connect() as connection:
            return self._get_work_order(connection, work_order_id)

    def verify_chronos(self) -> bool:
        from .chronos import build_receipt
        from .protocol import Informatron

        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT e.*, c.previous_chain_hash, c.chain_hash FROM events e JOIN chronos_receipts c USING(sequence) ORDER BY sequence"
            ).fetchall()
        previous_event_hash = None
        previous_chain_hash = None
        for row in rows:
            if row["previous_event_hash"] != previous_event_hash or row["previous_chain_hash"] != previous_chain_hash:
                return False
            event = Informatron(
                event_id=row["event_id"], event_hash=row["event_hash"], schema_version=row["schema_version"],
                occurred_at=row["occurred_at"], recorded_at=row["recorded_at"], actor_id=row["actor_id"],
                action=row["action"], entity_id=row["entity_id"], payload=json.loads(row["payload_json"]),
                provenance=json.loads(row["provenance_json"]), evidence=json.loads(row["evidence_json"]),
                authority=row["authority"], causation_id=row["causation_id"], correlation_id=row["correlation_id"],
                idempotency_key=row["idempotency_key"], previous_event_hash=row["previous_event_hash"], signature=row["signature"],
            )
            if sha256_json(event_hash_payload(event)) != event.event_hash:
                return False
            expected = build_receipt(event, int(row["sequence"]), previous_chain_hash)
            if expected.event_hash != row["event_hash"] or expected.chain_hash != row["chain_hash"]:
                return False
            previous_event_hash = event.event_hash
            previous_chain_hash = expected.chain_hash
        return True

    @staticmethod
    def _set_status(connection: sqlite3.Connection, current: WorkOrder, target: WorkOrderStatus, event_id: str, reason: str) -> None:
        connection.execute("UPDATE work_orders SET status = ? WHERE work_order_id = ?", (target.value, current.work_order_id))
        connection.execute(
            "INSERT INTO work_order_transitions(work_order_id, from_status, to_status, event_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (current.work_order_id, current.status.value, target.value, event_id, reason, utc_now()),
        )

    @classmethod
    def _get_work_order(cls, connection: sqlite3.Connection, work_order_id: str) -> WorkOrder:
        row = connection.execute("SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown WorkOrder: {work_order_id}")
        return cls._row_to_work_order(row)

    @staticmethod
    def _row_to_work_order(row: sqlite3.Row) -> WorkOrder:
        return WorkOrder(
            work_order_id=row["work_order_id"], schema_version=row["schema_version"], goal=row["goal"],
            definition_of_done=row["definition_of_done"], status=WorkOrderStatus(row["status"]),
            requested_by=row["requested_by"], assigned_organ=row["assigned_organ"], priority=int(row["priority"]),
            created_at=row["created_at"], deadline=row["deadline"], inputs=tuple(json.loads(row["inputs_json"])),
            required_capabilities=tuple(json.loads(row["required_capabilities_json"])),
            verification_requirements=tuple(json.loads(row["verification_requirements_json"])),
            parent_work_order_id=row["parent_work_order_id"], correlation_id=row["correlation_id"],
        )
