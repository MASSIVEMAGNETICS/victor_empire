from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from victor_runtime import VictorKernel
from empire_eak import BHeardPaidIntakeSandbox, EmpireAutonomyKernel, PaymentError


class BHeardSandboxHardeningTests(unittest.TestCase):
    def env(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        victor = VictorKernel(data_dir=root / ".victor", workspace=root / "artifacts")
        eak = EmpireAutonomyKernel(victor)
        organ = BHeardPaidIntakeSandbox(
            eak, workspace=root / "bheard", webhook_secret="secret"
        )
        return root, victor, eak, organ

    def intake(self, organ: BHeardPaidIntakeSandbox) -> str:
        return organ.submit(
            name="A",
            email="a@example.com",
            problem="workflow",
            desired_outcome="fix",
            consent=True,
        )

    def paid_fixture(self, organ, intake, event_id="evt_harden"):
        ts = int(time.time())
        event = {
            "id": event_id,
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {
                "object": {
                    "payment_status": "paid",
                    "amount_total": 1900,
                    "currency": "usd",
                    "metadata": {"intake_id": intake},
                }
            },
        }
        payload = json.dumps(event, separators=(",", ":")).encode()
        return payload, organ.sign(payload, "secret", ts), ts

    def test_oversized_webhook_rejected_before_signature_parse(self):
        root, victor, eak, organ = self.env()
        payload = b"x" * (organ.MAX_WEBHOOK_BYTES + 1)
        with self.assertRaisesRegex(PaymentError, "payload too large"):
            organ.accept_payment(payload, "not-a-valid-signature", now=int(time.time()))

    def test_oversized_intake_field_rejected_before_persistence(self):
        root, victor, eak, organ = self.env()
        with self.assertRaisesRegex(ValueError, "problem exceeds"):
            organ.submit(
                name="A",
                email="a@example.com",
                problem="x" * (organ.MAX_PROBLEM_CHARS + 1),
                desired_outcome="fix",
                consent=True,
            )
        with victor.db.connect() as c:
            count = c.execute("SELECT COUNT(*) n FROM bheard_intakes").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_retry_resumes_same_task_after_post_commit_interruption(self):
        root, victor, eak, organ = self.env()
        intake = self.intake(organ)
        payload, signature, ts = self.paid_fixture(organ, intake, event_id="evt_resume")

        original_resume = organ._resume_payment_task
        seen = []

        def interrupt(task_id):
            seen.append(task_id)
            raise RuntimeError("simulated process loss after durable admission")

        organ._resume_payment_task = interrupt
        with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
            organ.accept_payment(payload, signature, now=ts)

        with victor.db.connect() as c:
            payment_count = c.execute("SELECT COUNT(*) n FROM bheard_payments").fetchone()["n"]
            continuation = c.execute(
                "SELECT task_id FROM bheard_payment_continuations WHERE event_id='evt_resume'"
            ).fetchone()
            task_count = c.execute(
                "SELECT COUNT(*) n FROM eak_tasks WHERE capability_id=?",
                (organ.CAPABILITY,),
            ).fetchone()["n"]
        self.assertEqual(payment_count, 1)
        self.assertEqual(task_count, 1)
        self.assertIsNotNone(continuation)
        self.assertEqual(continuation["task_id"], seen[0])

        organ._resume_payment_task = original_resume
        out = organ.accept_payment(payload, signature, now=ts)
        self.assertEqual(out["task_id"], seen[0])
        self.assertEqual(out["state"], "CLOSED")

        with victor.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM bheard_payments").fetchone()["n"], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM bheard_payment_continuations").fetchone()["n"], 1)
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) n FROM eak_receipts WHERE task_id=? AND kind='verification'",
                    (seen[0],),
                ).fetchone()["n"],
                1,
            )

    def test_identical_retry_reuses_closed_continuation(self):
        root, victor, eak, organ = self.env()
        intake = self.intake(organ)
        payload, signature, ts = self.paid_fixture(organ, intake, event_id="evt_repeat")
        first = organ.accept_payment(payload, signature, now=ts)
        second = organ.accept_payment(payload, signature, now=ts)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["payment_receipt_id"], second["payment_receipt_id"])
        self.assertEqual(second["state"], "CLOSED")
        with victor.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM bheard_payments").fetchone()["n"], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM bheard_fulfillments").fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
