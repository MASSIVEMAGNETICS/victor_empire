from pathlib import Path
import sqlite3
import tempfile
import unittest

from victor.kernel import VictorKernel
from victor.protocol import Artifact, VerificationReceipt, WorkOrder, WorkOrderStatus


class SimulatedCrash(RuntimeError):
    pass


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "victor.db")
        self.kernel = VictorKernel(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def create_authorized(self) -> WorkOrder:
        work = WorkOrder(
            goal="Build verified artifact", definition_of_done="Artifact tests pass",
            requested_by="human:bando", correlation_id="corr_test",
            required_capabilities=("filesystem.write",), verification_requirements=("unit_tests",),
        )
        self.kernel.create_work_order(work, idempotency_key="create:1")
        self.kernel.transition(work.work_order_id, WorkOrderStatus.PLANNED, actor_id="kernel", reason="plan accepted", idempotency_key="plan:1")
        return self.kernel.transition(work.work_order_id, WorkOrderStatus.AUTHORIZED, actor_id="ethica", reason="policy passed", idempotency_key="auth:1")

    def test_wal_and_foreign_keys(self):
        self.assertEqual(self.kernel.store.journal_mode(), "wal")
        with self.kernel.store.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_full_done_path_requires_independent_receipt(self):
        work = self.create_authorized()
        lease = self.kernel.issue_lease(
            work.work_order_id, actor_id="worker:dev", organ_id="dev-ville",
            capability="filesystem.write", scope={"allow": ["C:/Victor/work/**"]},
            issued_by="ethica", duration_seconds=300, idempotency_key="lease:1",
        )
        self.assertEqual(self.kernel.get_work_order(work.work_order_id).status, WorkOrderStatus.LEASED)
        self.kernel.start_execution(work.work_order_id, lease.lease_id, actor_id=lease.actor_id, idempotency_key="run:1")
        artifact = Artifact(
            work_order_id=work.work_order_id, produced_by="dev-ville", media_type="text/x-python",
            sha256="a" * 64, uri="file:///C:/Victor/work/result.py",
        )
        self.kernel.record_artifact(artifact, actor_id="dev-ville", idempotency_key="artifact:1")
        receipt = VerificationReceipt(
            work_order_id=work.work_order_id, artifact_id=artifact.artifact_id,
            verifier_id="verifier:independent", artifact_hash=artifact.sha256,
            verification_policy="victor.python.v1",
            checks=({"name": "unit_tests", "required": True, "passed": True},),
            passed=True, environment_fingerprint={"python": "3.11", "os": "windows"},
        )
        done = self.kernel.record_verification(receipt, idempotency_key="verify:1")
        self.assertEqual(done.status, WorkOrderStatus.DONE)
        self.assertTrue(self.kernel.verify_chronos())

    def test_chronos_detects_event_content_tampering(self):
        work = WorkOrder("preserve history", "tampering detected", "human:bando")
        self.kernel.create_work_order(work, idempotency_key="tamper:create")
        with self.kernel.store.connect() as connection:
            connection.execute("UPDATE events SET payload_json = ? WHERE sequence = 1", ('{"goal":"rewritten"}',))
        self.assertFalse(self.kernel.verify_chronos())

    def test_direct_done_is_forbidden(self):
        work = self.create_authorized()
        with self.assertRaises(ValueError):
            self.kernel.transition(work.work_order_id, WorkOrderStatus.DONE, actor_id="organ", reason="trust me", idempotency_key="bad:done")

    def test_failed_verification_routes_to_rework(self):
        work = self.create_authorized()
        lease = self.kernel.issue_lease(
            work.work_order_id, actor_id="worker:dev", organ_id="dev-ville", capability="filesystem.write",
            scope={"allow": ["C:/Victor/work/**"]}, issued_by="ethica", duration_seconds=300, idempotency_key="lease:2",
        )
        self.kernel.start_execution(work.work_order_id, lease.lease_id, actor_id="worker:dev", idempotency_key="run:2")
        artifact = Artifact(work_order_id=work.work_order_id, produced_by="dev-ville", media_type="text/plain", sha256="b" * 64, uri="file:///C:/Victor/work/fail.txt")
        self.kernel.record_artifact(artifact, actor_id="dev-ville", idempotency_key="artifact:2")
        receipt = VerificationReceipt(
            work_order_id=work.work_order_id, artifact_id=artifact.artifact_id, verifier_id="verifier:1",
            artifact_hash=artifact.sha256, verification_policy="v1",
            checks=({"name": "acceptance", "required": True, "passed": False},), passed=False,
            environment_fingerprint={"python": "3.11"},
        )
        self.assertEqual(self.kernel.record_verification(receipt, idempotency_key="verify:2").status, WorkOrderStatus.REWORK)

    def test_simulated_crash_rolls_back_event_and_projection(self):
        work = WorkOrder("atomic goal", "atomic done", "human:bando")

        def crash(checkpoint: str) -> None:
            if checkpoint == "create.after_event":
                raise SimulatedCrash(checkpoint)

        crashing = VictorKernel(self.db, fault_hook=crash)
        with self.assertRaises(SimulatedCrash):
            crashing.create_work_order(work, idempotency_key="crash:create")

        recovered = VictorKernel(self.db)
        with recovered.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0], 0)
        recovered.create_work_order(work, idempotency_key="crash:create")
        self.assertEqual(recovered.get_work_order(work.work_order_id).status, WorkOrderStatus.CAPTURED)
        self.assertTrue(recovered.verify_chronos())

    def test_create_idempotency_returns_same_work_order(self):
        first = WorkOrder("one", "done", "human:bando")
        second = WorkOrder("different", "done", "human:bando")
        self.kernel.create_work_order(first, idempotency_key="same-request")
        observed = self.kernel.create_work_order(second, idempotency_key="same-request")
        self.assertEqual(observed.work_order_id, first.work_order_id)
        with self.kernel.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_lease_transaction_is_atomic_across_crash(self):
        work = self.create_authorized()

        def crash(checkpoint: str) -> None:
            if checkpoint == "lease.before_commit":
                raise SimulatedCrash(checkpoint)

        crashing = VictorKernel(self.db, fault_hook=crash)
        with self.assertRaises(SimulatedCrash):
            crashing.issue_lease(
                work.work_order_id, actor_id="worker:dev", organ_id="dev-ville",
                capability="filesystem.write", scope={"allow": ["C:/Victor/work/**"]},
                issued_by="ethica", duration_seconds=300, idempotency_key="lease:crash",
            )

        recovered = VictorKernel(self.db)
        self.assertEqual(recovered.get_work_order(work.work_order_id).status, WorkOrderStatus.AUTHORIZED)
        with recovered.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM capability_leases").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertTrue(recovered.verify_chronos())

    def test_execution_rejects_wrong_actor_and_consumes_lease(self):
        work = self.create_authorized()
        lease = self.kernel.issue_lease(
            work.work_order_id, actor_id="worker:authorized", organ_id="dev-ville",
            capability="filesystem.write", scope={"allow": ["C:/Victor/work/**"]},
            issued_by="ethica", duration_seconds=300, idempotency_key="lease:actor-test",
        )
        with self.assertRaises(PermissionError):
            self.kernel.start_execution(
                work.work_order_id, lease.lease_id, actor_id="worker:intruder", idempotency_key="run:intruder"
            )
        self.kernel.start_execution(
            work.work_order_id, lease.lease_id, actor_id="worker:authorized", idempotency_key="run:authorized"
        )
        with self.kernel.store.connect() as connection:
            row = connection.execute(
                "SELECT uses, status FROM capability_leases WHERE lease_id = ?", (lease.lease_id,)
            ).fetchone()
            self.assertEqual(row["uses"], 1)
            self.assertEqual(row["status"], "EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
