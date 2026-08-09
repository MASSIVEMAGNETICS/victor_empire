from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from victor_runtime import VictorKernel
from victor_runtime.kernel import PolicyError


class ClosedLoopTests(unittest.TestCase):
    def make_kernel(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        kernel = VictorKernel(data_dir=root / ".victor", workspace=root / "artifacts")
        return temp, kernel

    def test_closed_loop_completes_and_verifies_chain(self):
        temp, kernel = self.make_kernel()
        self.addCleanup(temp.cleanup)
        result = kernel.run_closed_loop("Ship one verified artifact")
        self.assertTrue(result["verification"]["ok"])
        self.assertTrue(result["event_chain"]["ok"])
        self.assertTrue(Path(result["artifact"]).exists())
        status = kernel.status()
        self.assertIsNone(status["active_work_order"])
        self.assertEqual(status["recent_outcomes"][0]["status"], "verified")

    def test_hosted_model_provider_fails_closed(self):
        temp, kernel = self.make_kernel()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(PolicyError):
            kernel.govern("artifact.write", provider="openai")

    def test_unknown_capability_is_denied(self):
        temp, kernel = self.make_kernel()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(PolicyError):
            kernel.govern("shell.unbounded")

    def test_single_active_work_order(self):
        temp, kernel = self.make_kernel()
        self.addCleanup(temp.cleanup)
        kernel.create_work_order("first")
        with self.assertRaises(PolicyError):
            kernel.create_work_order("second")


if __name__ == "__main__":
    unittest.main()
