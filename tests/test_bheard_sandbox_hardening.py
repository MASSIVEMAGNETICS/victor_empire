from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_deep_webhook_json_is_rejected_before_persistence(self):
        root, victor, eak, organ = self.env()
        intake = self.intake(organ)
        event = {
            "id": "evt-deep",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {"object": {"metadata": {"intake_id": intake}}},
        }
        cursor = event
        for _ in range(eak.MAX_TASK_PAYLOAD_DEPTH):
            child = {}
            cursor["extra"] = child
            cursor = child
        payload = json.dumps(event, separators=(",", ":")).encode()
        ts = int(time.time())
        with self.assertRaisesRegex(PaymentError, "structure exceeds limits"):
            organ.accept_payment(payload, organ.sign(payload, "secret", ts), now=ts)
        with victor.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM bheard_payments").fetchone()["n"], 0)

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

    def test_intake_event_failure_rolls_back_state(self):
        root, victor, eak, organ = self.env()
        original_append = organ.events.append_in_transaction

        def fail_intake_event(conn, **kwargs):
            if kwargs.get("action") == "INTAKE_COMPLETED":
                raise RuntimeError("injected intake event failure")
            return original_append(conn, **kwargs)

        organ.events.append_in_transaction = fail_intake_event
        with self.assertRaisesRegex(RuntimeError, "injected intake event failure"):
            self.intake(organ)
        with victor.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM bheard_intakes").fetchone()["n"], 0)
            self.assertEqual(
                c.execute("SELECT COUNT(*) n FROM events WHERE action='INTAKE_COMPLETED'").fetchone()["n"],
                0,
            )

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

    def test_draft_creation_rejects_final_entry_symlink_swap(self):
        root, victor, eak, organ = self.env()
        intake = self.intake(organ)
        payload, signature, ts = self.paid_fixture(organ, intake, event_id="evt_symlink")
        outside = root / "outside.md"
        outside.write_text("SAFE", encoding="utf-8")
        draft_name = f"{intake}.md"
        original_open = os.open
        swapped = False

        def race_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == draft_name and flags & os.O_WRONLY and not swapped:
                swapped = True
                (root / "bheard" / draft_name).symlink_to(outside)
            return original_open(path, flags, *args, **kwargs)

        with mock.patch("empire_eak.economic.os.open", side_effect=race_open):
            outcome = organ.accept_payment(payload, signature, now=ts)
        self.assertTrue(swapped)
        self.assertEqual(outcome["state"], "QUARANTINED")
        self.assertEqual(outside.read_text(encoding="utf-8"), "SAFE")

    def test_legacy_continuation_lookup_is_exact_and_does_not_parse_history(self):
        root, victor, eak, organ = self.env()
        intake = self.intake(organ)
        payment_id = "pay-legacy"
        now = "2026-09-14T00:00:00+00:00"
        task_payload = {"intake_id": intake, "payment_receipt_id": payment_id}
        expected_task = "eak-legacy"
        with victor.db.connect() as c:
            c.execute(
                "INSERT INTO bheard_payments VALUES(?,?,?,?,?,?,?)",
                (payment_id, intake, "evt-legacy", 1900, "usd", "a" * 64, now),
            )
            c.execute(
                "INSERT INTO eak_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (expected_task, organ.CAPABILITY, "payment_webhook",
                 json.dumps(task_payload, sort_keys=True), "TRIGGERED",
                 None, None, None, now, now),
            )
            for index in range(100):
                c.execute(
                    "INSERT INTO eak_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"eak-noise-{index}", organ.CAPABILITY, "payment_webhook",
                     "not-json", "TRIGGERED", None, None, None, now, now),
                )
        with mock.patch("empire_eak.economic.json.loads", side_effect=AssertionError("parsed history")):
            with victor.db.connect() as c:
                self.assertEqual(
                    organ._find_existing_task_in_transaction(c, payment_id), expected_task
                )


if __name__ == "__main__":
    unittest.main()
