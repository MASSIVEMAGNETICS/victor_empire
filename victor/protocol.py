"""Versioned Victor ABI objects.

These objects describe authority and evidence. They deliberately contain no
model/provider assumptions and persist no hidden chain-of-thought.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4


SCHEMA_VERSION = "victor.abi.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class WorkOrderStatus(StrEnum):
    CAPTURED = "CAPTURED"
    PLANNED = "PLANNED"
    AUTHORIZED = "AUTHORIZED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REWORK = "REWORK"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class WorkOrder:
    goal: str
    definition_of_done: str
    requested_by: str
    work_order_id: str = field(default_factory=lambda: new_id("wo"))
    schema_version: str = SCHEMA_VERSION
    status: WorkOrderStatus = WorkOrderStatus.CAPTURED
    assigned_organ: str | None = None
    priority: int = 50
    created_at: str = field(default_factory=utc_now)
    deadline: str | None = None
    inputs: tuple[Mapping[str, Any], ...] = ()
    required_capabilities: tuple[str, ...] = ()
    verification_requirements: tuple[str, ...] = ()
    parent_work_order_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.goal.strip() or not self.definition_of_done.strip():
            raise ValueError("goal and definition_of_done must be non-empty")
        if not self.requested_by.strip():
            raise ValueError("requested_by must be non-empty")
        if not 0 <= self.priority <= 100:
            raise ValueError("priority must be between 0 and 100")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True, slots=True)
class CapabilityLease:
    work_order_id: str
    actor_id: str
    organ_id: str
    capability: str
    scope: Mapping[str, Any]
    issued_by: str
    issued_at: str
    expires_at: str
    lease_id: str = field(default_factory=lambda: new_id("lease"))
    schema_version: str = SCHEMA_VERSION
    resource_constraints: Mapping[str, Any] = field(default_factory=dict)
    network_policy: Mapping[str, Any] = field(default_factory=lambda: {"default": "deny"})
    filesystem_policy: Mapping[str, Any] = field(default_factory=lambda: {"default": "deny"})
    process_policy: Mapping[str, Any] = field(default_factory=lambda: {"default": "deny"})
    max_uses: int = 1
    status: str = "ACTIVE"
    signature: str | None = None

    def __post_init__(self) -> None:
        if self.max_uses < 1:
            raise ValueError("max_uses must be positive")
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.issued_at):
            raise ValueError("expires_at must be later than issued_at")

    def active_at(self, instant: str) -> bool:
        point = datetime.fromisoformat(instant)
        return self.status == "ACTIVE" and datetime.fromisoformat(self.issued_at) <= point < datetime.fromisoformat(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Artifact:
    work_order_id: str
    produced_by: str
    media_type: str
    sha256: str
    uri: str
    artifact_id: str = field(default_factory=lambda: new_id("artifact"))
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    work_order_id: str
    artifact_id: str
    verifier_id: str
    artifact_hash: str
    verification_policy: str
    checks: tuple[Mapping[str, Any], ...]
    passed: bool
    environment_fingerprint: Mapping[str, Any]
    receipt_id: str = field(default_factory=lambda: new_id("verify"))
    schema_version: str = SCHEMA_VERSION
    evidence_refs: tuple[str, ...] = ()
    executed_at: str = field(default_factory=utc_now)
    signature: str | None = None

    def __post_init__(self) -> None:
        if not self.checks:
            raise ValueError("verification requires at least one check")
        required_failures = [c for c in self.checks if c.get("required", True) and not c.get("passed", False)]
        if self.passed and required_failures:
            raise ValueError("passed receipt contains a failed required check")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Informatron:
    occurred_at: str
    recorded_at: str
    actor_id: str
    action: str
    entity_id: str
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    evidence: Mapping[str, Any]
    authority: str
    event_id: str
    event_hash: str
    schema_version: str = SCHEMA_VERSION
    causation_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    previous_event_hash: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OrganManifest:
    organ_id: str
    name: str
    version: str
    capabilities: tuple[str, ...]
    accepted_schema_versions: tuple[str, ...] = (SCHEMA_VERSION,)
    local_only: bool = True


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    experiment_id: str
    proposal_hash: str
    code_commit: str
    dataset_hash: str
    environment_hash: str
    seed: int
    configuration: Mapping[str, Any]
    started_at: str
    finished_at: str
    baseline_metrics: Mapping[str, float]
    result_metrics: Mapping[str, float]
    artifact_hashes: tuple[str, ...]
    stdout_hash: str
    stderr_hash: str
    reproducible: bool
    verification_receipt_id: str | None = None
    schema_version: str = SCHEMA_VERSION

