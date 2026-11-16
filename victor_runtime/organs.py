from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


JOB_SCHEMA = "victor.organ.job.v1"
RECEIPT_SCHEMA = "victor.organ.receipt.v1"
DEVVILLE_ORGAN = "dev-ville"
DEVVILLE_CAPABILITY = "devville.project.build"
MAX_STDOUT_BYTES = 1_048_576
MAX_STDERR_BYTES = 262_144
_OUTPUT_READ_CHUNK_BYTES = 64 * 1024


class OrganError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _drain_bounded(
    pipe: Any,
    limit: int,
    captured: bytearray,
    overflow: threading.Event,
    read_error: threading.Event,
) -> None:
    """Drain a child stream while retaining no more than its configured limit."""
    try:
        while True:
            chunk = pipe.read(_OUTPUT_READ_CHUNK_BYTES)
            if not chunk:
                break
            remaining = limit - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                overflow.set()
    except (OSError, ValueError):
        read_error.set()
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            read_error.set()


class _PosixProcessGroup:
    """Owns the adapter's POSIX process group, including ordinary descendants."""

    def __init__(self, process_id: int) -> None:
        self.process_id = process_id

    def has_descendants(self) -> bool:
        try:
            os.killpg(self.process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate(self) -> None:
        try:
            os.killpg(self.process_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise OrganError(f"unable to terminate adapter process group: {exc}") from exc

    def close(self) -> None:
        return


class _WindowsJob:
    """Places the suspended adapter in a kill-on-close Windows Job Object."""

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004
    _INVALID_HANDLE_VALUE = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            self._raise_last_error("CreateJobObjectW")

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._extended_limit_type = ExtendedLimitInformation
        self._accounting_type = BasicAccountingInformation
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            self._raise_last_error("SetInformationJobObject")

    def _configure_api(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.ResumeThread.restype = wintypes.DWORD

    def _raise_last_error(self, action: str) -> None:
        error = self._ctypes.get_last_error()
        raise OrganError(f"{action} failed: {self._ctypes.WinError(error)}")

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32

        if not kernel32.AssignProcessToJobObject(self._handle, int(process._handle)):
            self._raise_last_error("AssignProcessToJobObject")

        class ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(self._TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == self._INVALID_HANDLE_VALUE:
            self._raise_last_error("CreateToolhelp32Snapshot")

        try:
            entry = ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel32.OpenThread(
                        self._THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if not thread:
                        self._raise_last_error("OpenThread")
                    try:
                        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            self._raise_last_error("ResumeThread")
                    finally:
                        kernel32.CloseHandle(thread)
                    return
                found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            raise OrganError("unable to locate the suspended adapter's primary thread")
        finally:
            kernel32.CloseHandle(snapshot)

    def has_descendants(self) -> bool:
        info = self._accounting_type()
        returned = self._wintypes.DWORD()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            self._ctypes.byref(returned),
        ):
            self._raise_last_error("QueryInformationJobObject")
        return info.ActiveProcesses > 0

    def terminate(self) -> None:
        if self._handle and self.has_descendants():
            if not self._kernel32.TerminateJobObject(self._handle, 1):
                self._raise_last_error("TerminateJobObject")

    def close(self) -> None:
        if self._handle:
            handle = self._handle
            self._handle = None
            if not self._kernel32.CloseHandle(handle):
                self._raise_last_error("CloseHandle(Job Object)")


def _spawn_contained(
    command: list[str], *, cwd: str, env: dict[str, str]
) -> tuple[subprocess.Popen[bytes], Any]:
    common = {
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": False,
        "shell": False,
    }
    if os.name != "nt":
        process = subprocess.Popen(command, start_new_session=True, **common)
        return process, _PosixProcessGroup(process.pid)

    job = _WindowsJob()
    process = None
    try:
        process = subprocess.Popen(
            command,
            creationflags=_WindowsJob._CREATE_SUSPENDED,
            **common,
        )
        job.assign_and_resume(process)
        return process, job
    except Exception as exc:
        try:
            job.terminate()
        except Exception:
            pass
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
        try:
            job.close()
        except Exception:
            pass
        if isinstance(exc, OrganError):
            raise
        raise OrganError("unable to establish Windows Job Object containment") from exc


class DevVilleOrganRunner:
    """Executes the Dev-Ville adapter as a tightly scoped child process.

    The runner never uses a shell. It passes a versioned job envelope, strips
    most environment variables, bounds execution time, and independently
    verifies the returned receipt and every artifact digest before Victor may
    commit the outcome.
    """

    def __init__(
        self,
        *,
        data_dir: str | Path,
        workspace: str | Path,
        devville_root: str | Path | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.workspace = Path(workspace).resolve()
        self.timeout_seconds = int(timeout_seconds)
        if self.timeout_seconds < 1 or self.timeout_seconds > 900:
            raise ValueError("timeout_seconds must be between 1 and 900")
        self.devville_root = self._resolve_root(devville_root)

    def _resolve_root(self, explicit: str | Path | None) -> Path:
        if explicit:
            return Path(explicit).expanduser().resolve()
        configured = os.environ.get("VICTOR_DEVVILLE_ROOT")
        if configured:
            return Path(configured).expanduser().resolve()
        repo_root = Path(__file__).resolve().parents[1]
        return (repo_root.parent / "dev-ville").resolve()

    @property
    def adapter_path(self) -> Path:
        return (self.devville_root / "victor_adapter.py").resolve()

    @property
    def output_root(self) -> Path:
        return (self.workspace / "organs" / DEVVILLE_ORGAN).resolve()

    def available(self) -> bool:
        adapter = self.adapter_path
        return (
            self.devville_root.is_dir()
            and adapter.is_file()
            and adapter.parent == self.devville_root
        )

    def _require_adapter(self) -> Path:
        if not self.available():
            raise OrganError(
                "dev-ville Victor adapter not found; clone MASSIVEMAGNETICS/dev-ville "
                "as a sibling repo or set VICTOR_DEVVILLE_ROOT"
            )
        return self.adapter_path

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        allowed = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["VICTOR_ORGAN_MODE"] = "1"
        return env

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        adapter = self._require_adapter()
        command = [sys.executable, str(adapter), *args]
        try:
            process, process_tree = _spawn_contained(
                command,
                cwd=str(self.devville_root),
                env=self._minimal_env(),
            )
        except (OSError, OrganError) as exc:
            raise OrganError(f"unable to launch contained dev-ville organ: {exc}") from exc

        stdout = bytearray()
        stderr = bytearray()
        stdout_overflow = threading.Event()
        stderr_overflow = threading.Event()
        stdout_read_error = threading.Event()
        stderr_read_error = threading.Event()
        readers = [
            threading.Thread(
                target=_drain_bounded,
                args=(process.stdout, MAX_STDOUT_BYTES, stdout, stdout_overflow, stdout_read_error),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_bounded,
                args=(process.stderr, MAX_STDERR_BYTES, stderr, stderr_overflow, stderr_read_error),
                daemon=True,
            ),
        ]
        cleanup_error = None
        failure = None
        readers_stuck = False

        def terminate_tree() -> None:
            nonlocal cleanup_error
            try:
                process_tree.terminate()
            except Exception as exc:
                cleanup_error = exc
                try:
                    process.kill()
                except OSError:
                    pass

        try:
            for reader in readers:
                reader.start()
        except RuntimeError as exc:
            terminate_tree()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=2)
            process_tree.close()
            raise OrganError("unable to start dev-ville output readers") from exc

        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                if stdout_overflow.is_set():
                    failure = f"dev-ville organ stdout exceeded {MAX_STDOUT_BYTES} bytes; process tree terminated"
                    terminate_tree()
                    break
                if stderr_overflow.is_set():
                    failure = f"dev-ville organ stderr exceeded {MAX_STDERR_BYTES} bytes; process tree terminated"
                    terminate_tree()
                    break

                parent_running = process.poll() is None
                readers_running = any(reader.is_alive() for reader in readers)
                if not parent_running:
                    if process_tree.has_descendants():
                        failure = "dev-ville organ left descendant processes running; process tree terminated"
                        terminate_tree()
                        break
                    if not readers_running:
                        break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if parent_running:
                        failure = f"dev-ville organ timed out after {self.timeout_seconds}s; process tree terminated"
                    else:
                        failure = "dev-ville organ output streams remained open after adapter exit; process-tree cleanup attempted"
                    terminate_tree()
                    break
                time.sleep(min(0.01, remaining))

            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_tree()
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
        finally:
            if process.poll() is None:
                terminate_tree()
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
            for reader in readers:
                reader.join(timeout=2)
            if any(reader.is_alive() for reader in readers):
                terminate_tree()
                for reader in readers:
                    reader.join(timeout=2)
            readers_stuck = any(reader.is_alive() for reader in readers)
            try:
                process_tree.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc

        if cleanup_error is not None:
            raise OrganError(f"dev-ville process-tree cleanup failed: {cleanup_error}") from cleanup_error
        if failure:
            raise OrganError(failure)
        if readers_stuck:
            raise OrganError("dev-ville output readers did not terminate after process-tree cleanup")
        if stdout_overflow.is_set():
            raise OrganError(
                f"dev-ville organ stdout exceeded {MAX_STDOUT_BYTES} bytes; process tree terminated"
            )
        if stderr_overflow.is_set():
            raise OrganError(
                f"dev-ville organ stderr exceeded {MAX_STDERR_BYTES} bytes; process tree terminated"
            )
        if stdout_read_error.is_set() or stderr_read_error.is_set():
            raise OrganError("dev-ville organ output could not be drained safely")

        return subprocess.CompletedProcess(
            command,
            process.returncode if process.returncode is not None else 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def probe(self) -> dict[str, Any]:
        proc = self._invoke(["probe"])
        if proc.returncode != 0:
            raise OrganError(f"dev-ville probe failed: {proc.stderr.strip()[:2000]}")
        try:
            payload = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            raise OrganError("dev-ville probe returned invalid JSON") from exc
        if payload.get("organ") != DEVVILLE_ORGAN:
            raise OrganError("dev-ville probe identity mismatch")
        if DEVVILLE_CAPABILITY not in payload.get("capabilities", []):
            raise OrganError("dev-ville capability missing from probe")
        return payload

    def _write_job(
        self,
        *,
        work_order_id: str,
        lease: dict[str, Any],
        directive: str,
    ) -> tuple[str, Path, dict[str, Any]]:
        job_id = f"job-{uuid.uuid4().hex}"
        job = {
            "schema": JOB_SCHEMA,
            "organ": DEVVILLE_ORGAN,
            "job_id": job_id,
            "work_order_id": work_order_id,
            "lease_id": lease["id"],
            "capability": DEVVILLE_CAPABILITY,
            "expires_at": lease["expires_at"],
            "directive": directive,
            "limits": {
                "max_cycles": 500,
                "time_delta": 10.0,
                "max_files": 128,
                "max_total_bytes": 2_000_000,
            },
        }
        jobs_dir = self.data_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        path = jobs_dir / f"{job_id}.json"
        path.write_text(json.dumps(job, indent=2, sort_keys=True), encoding="utf-8")
        return job_id, path, job

    def dispatch(
        self,
        *,
        work_order_id: str,
        lease: dict[str, Any],
        directive: str,
    ) -> dict[str, Any]:
        self.probe()
        job_id, job_path, job = self._write_job(
            work_order_id=work_order_id,
            lease=lease,
            directive=directive,
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        proc = self._invoke(
            ["run", "--job", str(job_path), "--output-root", str(self.output_root)]
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise OrganError(f"dev-ville job {job_id} failed: {detail[:4000]}")
        try:
            response = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            raise OrganError(f"dev-ville job {job_id} returned invalid JSON") from exc

        receipt_path_raw = response.get("receipt_path")
        if not receipt_path_raw:
            raise OrganError(f"dev-ville job {job_id} did not return a receipt path")
        receipt_path = Path(receipt_path_raw).resolve()
        expected_run_dir = (self.output_root / work_order_id / job_id).resolve()
        expected_receipt = expected_run_dir / "ORGAN_RECEIPT.json"
        if receipt_path != expected_receipt or not _inside(receipt_path, self.output_root):
            raise OrganError("dev-ville receipt path escaped the expected organ run directory")
        if not receipt_path.is_file():
            raise OrganError("dev-ville receipt file does not exist")

        verification = self.verify_receipt(
            receipt_path=receipt_path,
            expected_job=job,
        )
        return {
            "job_id": job_id,
            "job_path": str(job_path),
            "receipt_path": str(receipt_path),
            "verification": verification,
        }

    def verify_receipt(
        self,
        *,
        receipt_path: str | Path,
        expected_job: dict[str, Any],
    ) -> dict[str, Any]:
        path = Path(receipt_path).resolve()
        if not _inside(path, self.output_root):
            raise OrganError("receipt is outside dev-ville output root")
        try:
            raw = path.read_bytes()
            receipt = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OrganError(f"unable to read dev-ville receipt: {exc}") from exc
        if not isinstance(receipt, dict):
            raise OrganError("dev-ville receipt must be a JSON object")

        identity_fields = {
            "schema": RECEIPT_SCHEMA,
            "organ": DEVVILLE_ORGAN,
            "capability": DEVVILLE_CAPABILITY,
            "job_id": expected_job["job_id"],
            "work_order_id": expected_job["work_order_id"],
            "lease_id": expected_job["lease_id"],
        }
        mismatches = [key for key, value in identity_fields.items() if receipt.get(key) != value]
        if mismatches:
            raise OrganError(f"dev-ville receipt identity mismatch: {', '.join(mismatches)}")
        if receipt.get("status") != "completed":
            raise OrganError(f"dev-ville receipt status is {receipt.get('status')!r}")

        project = receipt.get("project") or {}
        if project.get("status") != "completed" or float(project.get("progress", 0)) < 100.0:
            raise OrganError("dev-ville project did not reach verified completion")
        if int(project.get("completed_tasks", -1)) != int(project.get("task_count", -2)):
            raise OrganError("dev-ville receipt reports incomplete project tasks")

        declared_hash = receipt.get("receipt_hash")
        hash_payload = dict(receipt)
        hash_payload.pop("receipt_hash", None)
        computed_hash = sha256_bytes(canonical_json(hash_payload))
        if not declared_hash or declared_hash != computed_hash:
            raise OrganError("dev-ville receipt hash mismatch")

        run_dir = path.parent
        verified_artifacts: list[dict[str, Any]] = []
        total_bytes = 0
        artifacts = receipt.get("artifacts") or []
        if not isinstance(artifacts, list) or not artifacts:
            raise OrganError("dev-ville receipt contains no artifacts")
        if len(artifacts) > int(expected_job["limits"]["max_files"]):
            raise OrganError("dev-ville receipt exceeds artifact count limit")

        seen: set[str] = set()
        for item in artifacts:
            rel_text = str(item.get("path") or "")
            if not rel_text or rel_text in seen:
                raise OrganError("dev-ville receipt contains invalid or duplicate artifact paths")
            seen.add(rel_text)
            artifact_path = (run_dir / rel_text).resolve()
            if not _inside(artifact_path, run_dir) or not artifact_path.is_file():
                raise OrganError(f"dev-ville artifact is missing or escaped run directory: {rel_text}")
            data = artifact_path.read_bytes()
            digest = sha256_bytes(data)
            if digest != item.get("sha256") or len(data) != int(item.get("bytes", -1)):
                raise OrganError(f"dev-ville artifact digest mismatch: {rel_text}")
            total_bytes += len(data)
            verified_artifacts.append(
                {"path": str(artifact_path), "sha256": digest, "bytes": len(data)}
            )

        if total_bytes > int(expected_job["limits"]["max_total_bytes"]):
            raise OrganError("dev-ville output exceeds total byte limit")

        return {
            "ok": True,
            "organ": DEVVILLE_ORGAN,
            "capability": DEVVILLE_CAPABILITY,
            "job_id": expected_job["job_id"],
            "receipt_path": str(path),
            "receipt_sha256": sha256_bytes(raw),
            "receipt_hash": computed_hash,
            "artifact_count": len(verified_artifacts),
            "total_artifact_bytes": total_bytes,
            "artifacts": verified_artifacts,
            "project": project,
        }
