from __future__ import annotations

from typing import Dict, Optional

from .kernel import VictorKernel
from .models import ExecutionResult
from .organs import VictorOrgan


class OrganDispatcher:
    """Deliver transactional outbox commands to registered Victor organs.

    Delivery is at-least-once. Artifact submission is idempotent by outbox ID,
    so a crash after organ execution but before acknowledgement does not create
    duplicate authoritative state transitions when the command is retried.
    """

    def __init__(self, kernel: VictorKernel, organs: Dict[str, VictorOrgan]):
        self.kernel = kernel
        self.organs = dict(organs)

    def run_once(self) -> Optional[ExecutionResult]:
        pending = self.kernel.store.pending_outbox(limit=1)
        if not pending:
            return None
        item = pending[0]
        topic = item["topic"]
        if not topic.startswith("organ.") or not topic.endswith(".execute"):
            return None
        organ_id = topic[len("organ.") : -len(".execute")]
        organ = self.organs.get(organ_id)
        if organ is None:
            raise KeyError(f"no registered organ for {organ_id}")

        payload = item["payload"]
        work_order = self.kernel.get_work_order(payload["work_order_id"])
        lease = self.kernel.get_lease(payload["lease_id"])

        result = organ.execute(work_order, lease)
        if result.organ_id != organ_id:
            raise ValueError("organ result identity mismatch")
        if result.work_order_id != work_order.work_order_id:
            raise ValueError("organ result work_order_id mismatch")

        self.kernel.submit_artifact(
            work_order.work_order_id,
            actor_id=lease.actor_id,
            artifact_sha256=result.artifact_sha256,
            artifact_uri=result.artifact_uri,
            metadata=result.metadata,
            idempotency_key=f"outbox:{item['outbox_id']}:artifact",
        )
        self.kernel.store.acknowledge_outbox(item["outbox_id"])
        return result
