from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import tempfile
import textwrap
import unittest
from pathlib import Path

from victor_runtime import VictorKernel
from victor_runtime.kernel import PolicyError
from victor_runtime.organs import (
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    DevVilleOrganRunner,
    OrganError,
)


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if result == 0xFFFFFFFF:
                raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
            return result == 0x00000102  # WAIT_TIMEOUT means still running
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
        except FileNotFoundError:
            return False
        return state != "Z"
    return True


FAKE_ADAPTER = r'''
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="command", required=True)
sub.add_parser("probe")
r = sub.add_parser("run")
r.add_argument("--job", required=True)
r.add_argument("--output-root", required=True)
a = p.parse_args()

if a.command == "probe":
    print(json.dumps({
        "organ": "dev-ville",
        "status": "ready",
        "job_schema": "victor.organ.job.v1",
        "receipt_schema": "victor.organ.receipt.v1",
        "capabilities": ["devville.project.build"],
        "network_required": False,
        "external_packages_required": False,
    }))
    raise SystemExit(0)

job = json.loads(Path(a.job).read_text(encoding="utf-8"))
root = Path(a.output_root).resolve()
run_dir = root / job["work_order_id"] / job["job_id"]
artifacts_dir = run_dir / "artifacts"
artifacts_dir.mkdir(parents=True, exist_ok=False)
data = ("verified fake dev-ville artifact for " + job["work_order_id"] + "\n").encode("utf-8")
artifact = artifacts_dir / "generated.txt"
artifact.write_bytes(data)
receipt = {
    "schema": "victor.organ.receipt.v1",
    "organ": "dev-ville",
    "capability": "devville.project.build",
    "job_id": job["job_id"],
    "work_order_id": job["work_order_id"],
    "lease_id": job["lease_id"],
    "started_at": "2026-01-01T00:00:00+00:00",
    "completed_at": "2026-01-01T00:00:01+00:00",
    "status": "completed",
    "cycles": 1,
    "limits": job["limits"],
    "project": {
        "name": "Fake-Project",
        "description": job["directive"],
        "status": "completed",
        "progress": 100.0,
        "task_count": 1,
        "completed_tasks": 1,
        "ticket_count": 1,
    },
    "artifacts": [{
        "path": "artifacts/generated.txt",
        "sha256": digest(data),
        "bytes": len(data),
        "description": "fixture",
    }],
    "runtime_policy": {"network": "fixture", "subprocess": "fixture", "write_scope": str(root)},
}
receipt["receipt_hash"] = digest(canonical(receipt))
receipt_path = run_dir / "ORGAN_RECEIPT.json"
receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
response = dict(receipt)
response["receipt_path"] = str(receipt_path)
print(json.dumps(response))
'''


class ClosedLoopTests(unittest.TestCase):
    def make_kernel(self, *, devville_root: Path | None = None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        kernel = VictorKernel(
            data_dir=root / ".victor",
            workspace=root / "artifacts",
            devville_root=devville_root,
        )
        return temp, kernel

    def make_fake_devville(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "victor_adapter.py").write_text(textwrap.dedent(FAKE_ADAPTER), encoding="utf-8")
        return temp, root

    def make_runner_with_script(self, script: str, *, timeout_seconds: int = 5):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "victor_adapter.py").write_text(textwrap.dedent(script), encoding="utf-8")
        runner = DevVilleOrganRunner(
            data_dir=root / "data",
            workspace=root / "workspace",
            devville_root=root,
            timeout_seconds=timeout_seconds,
        )
        return temp, runner

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

    def test_devville_closed_loop_verifies_external_receipt(self):
        organ_temp, organ_root = self.make_fake_devville()
        self.addCleanup(organ_temp.cleanup)
        temp, kernel = self.make_kernel(devville_root=organ_root)
        self.addCleanup(temp.cleanup)

        result = kernel.run_goal("Build a bounded software artifact", organ="dev-ville")
        self.assertEqual(result["organ"], "dev-ville")
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(result["verification"]["artifact_count"], 1)
        self.assertTrue(result["event_chain"]["ok"])
        self.assertTrue(Path(result["receipt"]).is_file())

        status = kernel.status()
        self.assertIsNone(status["active_work_order"])
        self.assertEqual(status["recent_outcomes"][0]["status"], "verified")
        self.assertEqual(status["recent_organ_runs"][0]["status"], "committed")
        devville = next(item for item in status["organs"] if item["name"] == "dev-ville")
        self.assertEqual(devville["status"], "ready")

    def test_devville_stdout_overflow_is_rejected(self):
        script = f"""
            import os
            import time

            remaining = {MAX_STDOUT_BYTES + 1}
            chunk = b"x" * 16384
            while remaining:
                try:
                    written = os.write(1, chunk[:remaining])
                    remaining -= written
                except BlockingIOError:
                    time.sleep(0.001)
        """
        temp, runner = self.make_runner_with_script(script)
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(OrganError, "stdout exceeded"):
            runner.probe()

    def test_devville_stderr_overflow_is_rejected(self):
        script = f"""
            import os
            import time

            remaining = {MAX_STDERR_BYTES + 1}
            chunk = b"x" * 16384
            while remaining:
                try:
                    written = os.write(2, chunk[:remaining])
                    remaining -= written
                except BlockingIOError:
                    time.sleep(0.001)
        """
        temp, runner = self.make_runner_with_script(script)
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(OrganError, "stderr exceeded"):
            runner.probe()

    def test_devville_drains_stdout_and_stderr_concurrently(self):
        # stdout stays below its cap but exceeds a normal pipe buffer before
        # stderr crosses its smaller cap. Both readers must be active or the
        # child blocks and the runner times out instead of detecting overflow.
        script = f"""
            import os

            def write_all(fd, total):
                remaining = total
                chunk = b"x" * 16384
                while remaining:
                    try:
                        written = os.write(fd, chunk[:remaining])
                    except OSError:
                        return
                    remaining -= written

            write_all(1, {MAX_STDOUT_BYTES // 2})
            write_all(2, {MAX_STDERR_BYTES + 1})
        """
        temp, runner = self.make_runner_with_script(script, timeout_seconds=3)
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(OrganError, "stderr exceeded"):
            runner.probe()

    def test_devville_timeout_remains_bounded(self):
        temp, runner = self.make_runner_with_script("import time\ntime.sleep(2)\n", timeout_seconds=1)
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(OrganError, "timed out after 1s"):
            runner.probe()

    def test_devville_descendant_is_terminated_when_adapter_exits(self):
        temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(temp_root.cleanup)
        root = Path(temp_root.name)
        pid_path = root / "descendant.pid"
        script = f"""
            import subprocess
            import sys
            from pathlib import Path

            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            Path({str(pid_path)!r}).write_text(str(child.pid), encoding="utf-8")
            print("adapter exiting")
        """
        runner_temp, runner = self.make_runner_with_script(script, timeout_seconds=5)
        self.addCleanup(runner_temp.cleanup)

        started = time.monotonic()
        with self.assertRaisesRegex(OrganError, "descendant processes"):
            runner.probe()
        elapsed = time.monotonic() - started

        child_pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_is_running(child_pid), "adapter descendant remained alive")
        self.assertLess(elapsed, 5, "process-tree cleanup exceeded the adapter deadline")

    def test_devville_missing_adapter_fails_and_releases_active_work(self):
        missing_temp = tempfile.TemporaryDirectory()
        self.addCleanup(missing_temp.cleanup)
        missing_root = Path(missing_temp.name) / "missing-dev-ville"
        temp, kernel = self.make_kernel(devville_root=missing_root)
        self.addCleanup(temp.cleanup)

        with self.assertRaises(OrganError):
            kernel.run_goal("Build something", organ="dev-ville")

        status = kernel.status()
        self.assertIsNone(status["active_work_order"])
        devville = next(item for item in status["organs"] if item["name"] == "dev-ville")
        self.assertEqual(devville["status"], "degraded")

    def test_hosted_model_provider_fails_closed(self):
        temp, kernel = self.make_kernel()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(PolicyError):
            kernel.govern("artifact.write", provider="openai")
        with self.assertRaises(PolicyError):
            kernel.govern("devville.project.build", provider="anthropic")

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
