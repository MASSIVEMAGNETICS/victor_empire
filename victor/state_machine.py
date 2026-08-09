"""Legal WorkOrder transitions for the Victor kernel."""

from __future__ import annotations

from .protocol import WorkOrderStatus


ALLOWED_TRANSITIONS: dict[WorkOrderStatus, frozenset[WorkOrderStatus]] = {
    WorkOrderStatus.CAPTURED: frozenset({WorkOrderStatus.PLANNED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.PLANNED: frozenset({WorkOrderStatus.AUTHORIZED, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.AUTHORIZED: frozenset({WorkOrderStatus.LEASED, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.LEASED: frozenset({WorkOrderStatus.RUNNING, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.RUNNING: frozenset({WorkOrderStatus.VERIFYING, WorkOrderStatus.FAILED, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.VERIFYING: frozenset({WorkOrderStatus.DONE, WorkOrderStatus.REWORK, WorkOrderStatus.FAILED, WorkOrderStatus.BLOCKED}),
    WorkOrderStatus.REWORK: frozenset({WorkOrderStatus.PLANNED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.DONE: frozenset(),
    WorkOrderStatus.FAILED: frozenset(),
    WorkOrderStatus.BLOCKED: frozenset({WorkOrderStatus.PLANNED, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.CANCELLED: frozenset(),
}


def require_transition(current: WorkOrderStatus, target: WorkOrderStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"illegal WorkOrder transition: {current.value} -> {target.value}")

