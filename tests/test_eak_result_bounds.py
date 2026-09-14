from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from empire_eak import Authority, Capability, EAKError, EmpireAutonomyKernel
from victor_runtime import VictorKernel


class EAKResultBoundsRegressionTests(unittest.TestCase):
    def test_oversized_executor_result_is_rejected_before_receipt_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victor = VictorKernel(data_dir=root / ".victor", workspace=root / "artifacts")
            eak = EmpireAutonomyKernel(victor)

            eak.register(
                Capability(
                    "test.result.bounds",
                    "software",
                    Authority.A2_REVERSIBLE,
                    ("manual",),
                    "test",
                    True,
                    True,
                ),
                executor=lambda payload: {
                    "blob": "x" * (eak.MAX_TASK_PAYLOAD_BYTES + 1),
                    "rollback": {"ok": True},
                },
                verifier=lambda payload, result: True,
            )

            task_id = eak.trigger("test.result.bounds", "manual", {"value": 1})
            eak.score(
                task_id,
                expected_value=1,
                information_gain=1,
                strategic_alignment=1,
                cost=0,
                risk=0,
                uncertainty=0,
                authority_friction=0,
            )

            with self.assertRaisesRegex(EAKError, "result.*limit|result.*bound|serialized byte limit"):
                eak.run(task_id)

            with victor.db.connect() as conn:
                task = conn.execute(
                    "SELECT state, result_json FROM eak_tasks WHERE id=?", (task_id,)
                ).fetchone()
                receipt_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM eak_receipts WHERE task_id=?", (task_id,)
                ).fetchone()["n"]

            self.assertEqual(task["state"], "QUARANTINED")
            self.assertIsNone(task["result_json"])
            self.assertEqual(receipt_count, 0)


if __name__ == "__main__":
    unittest.main()
