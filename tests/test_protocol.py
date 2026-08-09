from datetime import datetime, timedelta, timezone
import unittest

from victor.protocol import CapabilityLease, SCHEMA_VERSION, VerificationReceipt, WorkOrder


class ProtocolTests(unittest.TestCase):
    def test_schema_version_is_frozen_for_v1(self):
        self.assertEqual(SCHEMA_VERSION, "victor.abi.v1")
        self.assertEqual(WorkOrder("goal", "done", "human:bando").schema_version, SCHEMA_VERSION)

    def test_work_order_rejects_invalid_priority(self):
        with self.assertRaises(ValueError):
            WorkOrder("goal", "done", "human:bando", priority=101)

    def test_lease_expiration_and_default_deny(self):
        now = datetime.now(timezone.utc)
        lease = CapabilityLease(
            work_order_id="wo_1", actor_id="worker:1", organ_id="dev-ville",
            capability="filesystem.write", scope={"allow": ["C:/Victor/work/**"]},
            issued_by="ethica", issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        self.assertTrue(lease.active_at((now + timedelta(minutes=1)).isoformat()))
        self.assertFalse(lease.active_at((now + timedelta(minutes=6)).isoformat()))
        self.assertEqual(lease.network_policy["default"], "deny")

    def test_receipt_cannot_claim_pass_with_required_failure(self):
        with self.assertRaises(ValueError):
            VerificationReceipt(
                work_order_id="wo_1", artifact_id="a_1", verifier_id="verify:1",
                artifact_hash="abc", verification_policy="v1",
                checks=({"name": "tests", "required": True, "passed": False},),
                passed=True, environment_fingerprint={"python": "3.11"},
            )


if __name__ == "__main__":
    unittest.main()

