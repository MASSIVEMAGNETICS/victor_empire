from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from empire_eak import Authority, Capability, EAKError, EmpireAutonomyKernel
from victor_runtime import VictorKernel


class EAKResultBoundsRegressionTests(unittest.TestCase):
    def _assert_rejected_without_persistence(
        self,
        result: object,
        *,
        verifier: object | None = None,
        pattern: str = "result",
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victor = VictorKernel(data_dir=root / ".victor", workspace=root / "artifacts")
            eak = EmpireAutonomyKernel(victor)
            verifier_fn = verifier or (lambda payload, candidate: True)

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
                executor=lambda payload: result,
                verifier=verifier_fn,
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

            with self.assertRaisesRegex(EAKError, pattern):
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

    def test_oversized_executor_result_is_rejected_before_receipt_commit(self) -> None:
        self._assert_rejected_without_persistence(
            {
                "blob": "x" * (EmpireAutonomyKernel.MAX_TASK_PAYLOAD_BYTES + 1),
                "rollback": {"ok": True},
            },
            pattern="executor result.*byte limit",
        )

    def test_executor_result_depth_and_node_limits_fail_closed(self) -> None:
        deep: dict[str, object] = {"rollback": {"ok": True}}
        cursor = deep
        for _ in range(EmpireAutonomyKernel.MAX_TASK_PAYLOAD_DEPTH):
            child: dict[str, object] = {}
            cursor["next"] = child
            cursor = child
        self._assert_rejected_without_persistence(
            deep,
            pattern="executor result exceeds nesting depth limit",
        )

        many_nodes = {
            "values": [None] * EmpireAutonomyKernel.MAX_TASK_PAYLOAD_NODES,
            "rollback": {"ok": True},
        }
        self._assert_rejected_without_persistence(
            many_nodes,
            pattern="executor result exceeds structural node limit",
        )

    def test_executor_result_rejects_cycles_and_non_json_types(self) -> None:
        cyclic: dict[str, object] = {"rollback": {"ok": True}}
        cyclic["self"] = cyclic
        self._assert_rejected_without_persistence(
            cyclic,
            pattern="executor result cannot contain shared or cyclic containers",
        )
        self._assert_rejected_without_persistence(
            {"value": (1, 2), "rollback": {"ok": True}},
            pattern="executor result contains unsupported type tuple",
        )

    def test_executor_result_rejects_non_finite_and_oversized_numbers(self) -> None:
        self._assert_rejected_without_persistence(
            {"value": float("nan"), "rollback": {"ok": True}},
            pattern="executor result numbers must be finite",
        )
        self._assert_rejected_without_persistence(
            {"value": 1 << 4096, "rollback": {"ok": True}},
            pattern="executor result integer exceeds size limit",
        )

    def test_executor_result_serialized_overhead_is_bounded(self) -> None:
        near_limit_key = "k" * 65520
        self._assert_rejected_without_persistence(
            {near_limit_key: "", "rollback": {"ok": True}},
            pattern="executor result exceeds serialized byte limit",
        )

    def test_verifier_result_is_bounded_and_must_be_boolean(self) -> None:
        valid_result = {"value": 1, "rollback": {"ok": True}}
        self._assert_rejected_without_persistence(
            valid_result,
            verifier=lambda payload, result: {
                "blob": "x" * (EmpireAutonomyKernel.MAX_TASK_PAYLOAD_BYTES + 1)
            },
            pattern="verifier result.*byte limit",
        )
        self._assert_rejected_without_persistence(
            valid_result,
            verifier=lambda payload, result: {"verified": True},
            pattern="verifier result must be bool",
        )

    def test_verifier_cannot_mutate_result_after_admission(self) -> None:
        result = {"value": 1, "rollback": {"ok": True}}

        def mutating_verifier(payload: dict[str, object], candidate: dict[str, object]) -> bool:
            candidate["value"] = 2
            return True

        self._assert_rejected_without_persistence(
            result,
            verifier=mutating_verifier,
            pattern="verifier mutated executor result",
        )


if __name__ == "__main__":
    unittest.main()
