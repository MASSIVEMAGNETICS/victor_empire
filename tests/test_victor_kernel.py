from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from victor_kernel import ExecutionResult, OrganDispatcher, VictorKernel, WorkOrderStatus


TEST_KEY = b"victor-kernel-test-key-32-bytes!!"


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


class FakeDevVilleOrgan:
    organ_id = "dev-ville"

    def execute(self, work_order, lease):
        payload = (work_order.goal + "|" + lease.lease_id).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return ExecutionResult(
            organ_id=self.organ_id,
            work_order_id=work_order.work_order_id,
            artifact_sha256=digest,
            artifact_uri=f"memory://{digest}",
            metadata={"executor": "fake-dev-ville"},
        )


class VictorKernelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "kernel.db"
        self.clock = MutableClock()
        self.kernel = VictorKernel(
            self.db,
            signing_key=TEST_KEY,
            clock=self.clock,
        )

    def tearDown(self):
        self.kernel.close()
        self.tmp.cleanup()

    def _captured(self, *, idem="create-1"):
        return self.kernel.create_work_order(
            goal="Build verified artifact",
            definition_of_done="Artifact passes deterministic verification",
            requested_by="bando",
            assigned_organ="dev-ville",
            required_capabilities=["execute"],
            verification_requirements=["compile", "tests"],
            inputs={"directive": "build a tiny service"},
            idempotency_key=idem,
        )

    def _leased(self):
        wo = self._captured()
        self.kernel.authorize_work_order(
            wo.work_order_id,
            authorized_by="ethica",
            policy_evidence={"policy": "allow-test"},
            idempotency_key="auth-1",
        )
        lease = self.kernel.issue_lease(
            wo.work_order_id,
            actor_id="devville.worker",
            organ_id="dev-ville",
            capabilities=["execute"],
            scope={"filesystem": {"allow": ["/workspace/**"]}},
            ttl_seconds=60,
            max_uses=1,
            idempotency_key="lease-1",
        )
        return wo.work_order_id, lease

    def test_full_verified_happy_path(self):
        work_order_id, lease = self._leased()
        running = self.kernel.start_execution(
            work_order_id,
            lease_id=lease.lease_id,
            actor_id="devville.worker",
            capability="execute",
            idempotency_key="run-1",
        )
        self.assertEqual(running.status, WorkOrderStatus.RUNNING)
        digest = hashlib.sha256(b"artifact-v1").hexdigest()
        verifying = self.kernel.submit_artifact(
            work_order_id,
            actor_id="devville.worker",
            artifact_sha256=digest,
            artifact_uri="file:///workspace/artifact-v1",
            idempotency_key="artifact-1",
        )
        self.assertEqual(verifying.status, WorkOrderStatus.VERIFYING)
        receipt = self.kernel.record_verification(
            work_order_id,
            verifier_id="victor.verifier",
            artifact_sha256=digest,
            passed=True,
            checks=[{"name": "compile", "passed": True}, {"name": "tests", "passed": True}],
            evidence_refs=["sha256:" + digest],
            environment_fingerprint={"python": "3.13"},
            idempotency_key="verify-1",
        )
        self.assertTrue(receipt.passed)
        self.assertTrue(self.kernel.verify_receipt_signature(receipt))
        self.assertEqual(self.kernel.get_work_order(work_order_id).status, WorkOrderStatus.DONE)
        self.assertTrue(self.kernel.store.verify_integrity()["ok"])

    def test_done_cannot_be_set_without_verification(self):
        work_order_id, lease = self._leased()
        with self.kernel.store.transaction() as conn:
            with self.assertRaises(ValueError):
                self.kernel._transition(
                    conn,
                    work_order_id=work_order_id,
                    to_status=WorkOrderStatus.DONE,
                    actor_id="attacker",
                    action="force_done",
                    payload={},
                    evidence={},
                    authority="none",
                    idempotency_key=None,
                )
        self.assertNotEqual(self.kernel.get_work_order(work_order_id).status, WorkOrderStatus.DONE)

    def test_failed_verification_routes_to_rework(self):
        work_order_id, lease = self._leased()
        self.kernel.start_execution(
            work_order_id,
            lease_id=lease.lease_id,
            actor_id="devville.worker",
            idempotency_key="run-2",
        )
        digest = hashlib.sha256(b"broken").hexdigest()
        self.kernel.submit_artifact(
            work_order_id,
            actor_id="devville.worker",
            artifact_sha256=digest,
            artifact_uri="file:///workspace/broken",
            idempotency_key="artifact-2",
        )
        receipt = self.kernel.record_verification(
            work_order_id,
            verifier_id="victor.verifier",
            artifact_sha256=digest,
            passed=False,
            checks=[{"name": "tests", "passed": False}],
            evidence_refs=["test:failed"],
            environment_fingerprint={},
            idempotency_key="verify-2",
        )
        self.assertFalse(receipt.passed)
        self.assertEqual(self.kernel.get_work_order(work_order_id).status, WorkOrderStatus.REWORK)
        self.assertTrue(self.kernel.store.verify_integrity()["ok"])

    def test_idempotent_command_retries_do_not_duplicate_state(self):
        first = self._captured(idem="same-create")
        second = self._captured(idem="same-create")
        self.assertEqual(first.work_order_id, second.work_order_id)
        count = self.kernel.store.conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
        events = self.kernel.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(events, 1)

    def test_expired_lease_is_rejected(self):
        work_order_id, lease = self._leased()
        self.clock.advance(61)
        with self.assertRaises(PermissionError):
            self.kernel.start_execution(
                work_order_id,
                lease_id=lease.lease_id,
                actor_id="devville.worker",
                idempotency_key="expired-run",
            )
        recovered = self.kernel.recover()
        self.assertTrue(recovered["integrity"]["ok"])
        self.assertEqual(self.kernel.get_lease(lease.lease_id).status.value, "EXPIRED")
        self.assertEqual(self.kernel.get_work_order(work_order_id).status, WorkOrderStatus.LEASED)

    def test_wrong_actor_or_capability_is_rejected(self):
        work_order_id, lease = self._leased()
        with self.assertRaises(PermissionError):
            self.kernel.start_execution(
                work_order_id,
                lease_id=lease.lease_id,
                actor_id="wrong.actor",
                idempotency_key="wrong-actor",
            )
        with self.assertRaises(PermissionError):
            self.kernel.start_execution(
                work_order_id,
                lease_id=lease.lease_id,
                actor_id="devville.worker",
                capability="shell.root",
                idempotency_key="wrong-cap",
            )

    def test_crash_restart_preserves_causal_state_and_outbox(self):
        work_order_id, lease = self._leased()
        self.kernel.start_execution(
            work_order_id,
            lease_id=lease.lease_id,
            actor_id="devville.worker",
            idempotency_key="run-restart",
        )
        pending_before = self.kernel.store.pending_outbox()
        self.assertEqual(len(pending_before), 1)
        chain_before = self.kernel.store.conn.execute(
            "SELECT chain_hash FROM chronos_receipts ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        self.kernel.close()

        self.kernel = VictorKernel(self.db, signing_key=TEST_KEY, clock=self.clock)
        recovered = self.kernel.recover()
        chain_after = self.kernel.store.conn.execute(
            "SELECT chain_hash FROM chronos_receipts ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(chain_before, chain_after)
        self.assertEqual(self.kernel.get_work_order(work_order_id).status, WorkOrderStatus.RUNNING)
        self.assertEqual(len(recovered["pending_outbox"]), 1)
        self.assertTrue(recovered["integrity"]["ok"])

    def test_tampering_breaks_chronos_verification(self):
        wo = self._captured()
        self.assertTrue(self.kernel.store.verify_chronos())
        self.kernel.store.conn.execute("UPDATE events SET payload_json='{}' WHERE sequence=1")
        self.assertFalse(self.kernel.store.verify_chronos())

    def test_outbox_ack_is_durable(self):
        work_order_id, lease = self._leased()
        self.kernel.start_execution(
            work_order_id,
            lease_id=lease.lease_id,
            actor_id="devville.worker",
            idempotency_key="run-outbox",
        )
        item = self.kernel.store.pending_outbox()[0]
        self.kernel.store.acknowledge_outbox(item["outbox_id"])
        self.assertEqual(self.kernel.store.pending_outbox(), [])

    def test_lease_signature_survives_usage_counter_changes(self):
        work_order_id, lease = self._leased()
        self.kernel.start_execution(
            work_order_id,
            lease_id=lease.lease_id,
            actor_id="devville.worker",
            idempotency_key="run-signed-lease",
        )
        consumed = self.kernel.get_lease(lease.lease_id)
        from victor_kernel.crypto import verify_json_signature
        self.assertTrue(
            verify_json_signature(
                TEST_KEY, consumed.signing_payload(), consumed.signature
            )
        )

    def test_dispatcher_connects_outbox_to_organ_artifact_claim(self):
        work_order_id, lease = self._leased()
        self.kernel.start_execution(
            work_order_id,
            lease_id=lease.lease_id,
            actor_id="devville.worker",
            idempotency_key="run-dispatch",
        )
        dispatcher = OrganDispatcher(self.kernel, {"dev-ville": FakeDevVilleOrgan()})
        result = dispatcher.run_once()
        self.assertIsNotNone(result)
        self.assertEqual(self.kernel.get_work_order(work_order_id).status, WorkOrderStatus.VERIFYING)
        pending = self.kernel.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["topic"], "verifier.verify")
        self.assertTrue(self.kernel.store.verify_integrity()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
