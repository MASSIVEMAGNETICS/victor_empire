from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


JOB_SCHEMA = "victor.organ.job.v1"
RECEIPT_SCHEMA = "victor.organ.receipt.v1"
DEVVILLE_ORGAN = "dev-ville"
DEVVILLE_CAPABILITY = "devville.project.build"


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
            return subprocess.run(
                command,
                cwd=str(self.devville_root),
                env=self._minimal_env(),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OrganError(f"dev-ville organ timed out after {self.timeout_seconds}s") from exc
        except OSError as exc:
            raise OrganError(f"unable to launch dev-ville organ: {exc}") from exc

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
