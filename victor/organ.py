"""Typed boundary implemented by replaceable Victor organs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .protocol import Artifact, CapabilityLease, OrganManifest, WorkOrder


@dataclass(frozen=True, slots=True)
class AcceptanceReceipt:
    accepted: bool
    reason: str
    organ_id: str
    work_order_id: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    succeeded: bool
    artifacts: tuple[Artifact, ...]
    observations: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    healthy: bool
    details: Mapping[str, Any]


class VictorOrgan(Protocol):
    def describe(self) -> OrganManifest: ...
    def accept(self, work_order: WorkOrder, lease: CapabilityLease) -> AcceptanceReceipt: ...
    def execute(self, work_order: WorkOrder, lease: CapabilityLease) -> ExecutionResult: ...
    def health(self) -> HealthReport: ...

