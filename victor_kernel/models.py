from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class WorkOrderStatus(str, Enum):
    CAPTURED = "CAPTURED"
    AUTHORIZED = "AUTHORIZED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    REWORK = "REWORK"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALLOWED_TRANSITIONS = {
    WorkOrderStatus.CAPTURED: {WorkOrderStatus.AUTHORIZED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.AUTHORIZED: {WorkOrderStatus.LEASED, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.LEASED: {WorkOrderStatus.RUNNING, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.RUNNING: {WorkOrderStatus.VERIFYING, WorkOrderStatus.FAILED, WorkOrderStatus.BLOCKED},
    WorkOrderStatus.VERIFYING: {WorkOrderStatus.DONE, WorkOrderStatus.REWORK, WorkOrderStatus.FAILED},
    WorkOrderStatus.REWORK: {WorkOrderStatus.AUTHORIZED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.BLOCKED: {WorkOrderStatus.AUTHORIZED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.FAILED: {WorkOrderStatus.AUTHORIZED, WorkOrderStatus.CANCELLED},
    WorkOrderStatus.DONE: set(),
    WorkOrderStatus.CANCELLED: set(),
}


class LeaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class WorkOrder:
    work_order_id: str
    goal: str
    definition_of_done: str
    status: WorkOrderStatus
    requested_by: str
    assigned_organ: str
    priority: int
    created_at: str
    updated_at: str
    correlation_id: str
    parent_work_order_id: Optional[str] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    required_capabilities: List[str] = field(default_factory=list)
    verification_requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class CapabilityLease:
    lease_id: str
    work_order_id: str
    actor_id: str
    organ_id: str
    capabilities: List[str]
    scope: Dict[str, Any]
    issued_at: str
    expires_at: str
    max_uses: int
    uses: int
    status: LeaseStatus
    issuer: str
    signature: str

    def signing_payload(self) -> Dict[str, Any]:
        # Sign only the immutable grant. Usage/status are kernel-maintained
        # runtime state and must not invalidate the original authorization.
        data = self.to_dict()
        data.pop("signature", None)
        data.pop("uses", None)
        data.pop("status", None)
        return data

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class VerificationReceipt:
    receipt_id: str
    work_order_id: str
    artifact_sha256: str
    verifier_id: str
    passed: bool
    checks: List[Dict[str, Any]]
    evidence_refs: List[str]
    executed_at: str
    environment_fingerprint: Dict[str, Any]
    signature: str

    def signing_payload(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("signature", None)
        return data

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Informatron:
    sequence: int
    event_id: str
    schema_version: str
    occurred_at: str
    recorded_at: str
    actor_id: str
    action: str
    entity_id: str
    payload: Dict[str, Any]
    provenance: Dict[str, Any]
    evidence: Dict[str, Any]
    authority: str
    causation_id: Optional[str]
    correlation_id: Optional[str]
    idempotency_key: Optional[str]
    parent_event_hash: Optional[str]
    signature: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChronosReceipt:
    sequence: int
    event_id: str
    event_hash: str
    previous_chain_hash: Optional[str]
    chain_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionResult:
    organ_id: str
    work_order_id: str
    artifact_sha256: str
    artifact_uri: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
