from __future__ import annotations

from typing import Protocol

from .models import CapabilityLease, ExecutionResult, WorkOrder


class VictorOrgan(Protocol):
    organ_id: str

    def execute(self, work_order: WorkOrder, lease: CapabilityLease) -> ExecutionResult:
        """Perform leased work and return a content-addressed artifact claim.

        The organ never marks a WorkOrder DONE. Only the kernel's independent
        verification gate can do that.
        """
        ...
