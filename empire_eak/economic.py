from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
import uuid
import weakref
from pathlib import Path
from typing import Any

from victor_runtime.events import utc_now
from .kernel import Authority, Capability, EAKError, EmpireAutonomyKernel


class PaymentError(RuntimeError):
    pass


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BHeardPaidIntakeSandbox:
    """Receipt-only economic sandbox. Never charges, sends, refunds, publishes, or spends."""

    CAPABILITY = "bheard.paid_intake.sandbox"
    MAX_WEBHOOK_BYTES = 64 * 1024
    MAX_SIGNATURE_CHARS = 4096
    MAX_NAME_CHARS = 200
    MAX_EMAIL_CHARS = 320
    MAX_PROBLEM_CHARS = 8000
    MAX_OUTCOME_CHARS = 4000
    MAX_ID_CHARS = 255
    MAX_INTERNAL_TASK_BYTES = 4096
    MAX_DRAFT_BYTES = 16 * 1024

    def __init__(self, eak: EmpireAutonomyKernel, *, workspace: str | Path,
                 webhook_secret: str, amount_cents: int = 1900, currency: str = "usd",
                 tolerance_seconds: int = 300) -> None:
        if not webhook_secret or amount_cents <= 0 or tolerance_seconds <= 0:
            raise ValueError("valid sandbox secret/amount/tolerance required")
        self.eak = eak
        self.db = eak.db
        self.events = eak.events
        self.root = Path(workspace).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        required = (os.open, os.stat, os.unlink)
        if not all(operation in os.supports_dir_fd for operation in required):
            raise EAKError("descriptor-bound sandbox workspace is unavailable")
        workspace_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        workspace_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        self._root_fd = os.open(self.root, workspace_flags)
        self._close_root = weakref.finalize(self, os.close, self._root_fd)
        self.secret = webhook_secret.encode()
        self.amount = amount_cents
        self.currency = currency.lower()
        self.tolerance = tolerance_seconds
        self._schema()
        eak.register(
            Capability(
                self.CAPABILITY, "economic", Authority.A2_REVERSIBLE,
                ("payment_webhook",), "sandbox_receipt_chain", True, True
            ),
            executor=self._draft,
            verifier=self._verify_draft,
        )

    def _schema(self) -> None:
        with self.db.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS bheard_intakes(
              id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL,
              problem TEXT NOT NULL,outcome TEXT NOT NULL,consent INTEGER NOT NULL,
              state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bheard_payments(
              id TEXT PRIMARY KEY,intake_id TEXT NOT NULL,event_id TEXT NOT NULL UNIQUE,
              amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
              verified_at TEXT NOT NULL,FOREIGN KEY(intake_id) REFERENCES bheard_intakes(id));
            CREATE TABLE IF NOT EXISTS bheard_payment_continuations(
              event_id TEXT PRIMARY KEY,payment_id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              FOREIGN KEY(payment_id) REFERENCES bheard_payments(id),
              FOREIGN KEY(task_id) REFERENCES eak_tasks(id));
            CREATE TABLE IF NOT EXISTS bheard_fulfillments(
              id TEXT PRIMARY KEY,intake_id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL,
              draft_path TEXT NOT NULL,draft_sha256 TEXT NOT NULL,state TEXT NOT NULL,
              approved_by TEXT,delivered_at TEXT,outcome TEXT,updated_at TEXT NOT NULL,
              FOREIGN KEY(intake_id) REFERENCES bheard_intakes(id));
            CREATE TRIGGER IF NOT EXISTS bheard_payments_no_update BEFORE UPDATE ON bheard_payments
              BEGIN SELECT RAISE(ABORT,'payment receipts immutable'); END;
            CREATE TRIGGER IF NOT EXISTS bheard_payments_no_delete BEFORE DELETE ON bheard_payments
              BEGIN SELECT RAISE(ABORT,'payment receipts immutable'); END;
            CREATE INDEX IF NOT EXISTS bheard_task_payload_lookup
              ON eak_tasks(capability_id,trigger,payload_json)
              WHERE capability_id='bheard.paid_intake.sandbox'
                AND trigger='payment_webhook';
            """)

    @staticmethod
    def _require_text(name: str, value: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        if len(value) > maximum:
            raise ValueError(f"{name} exceeds {maximum} characters")
        return value.strip()

    def submit(self, *, name: str, email: str, problem: str, desired_outcome: str,
               consent: bool) -> str:
        name = self._require_text("name", name, self.MAX_NAME_CHARS)
        email = self._require_text("email", email, self.MAX_EMAIL_CHARS)
        problem = self._require_text("problem", problem, self.MAX_PROBLEM_CHARS)
        desired_outcome = self._require_text(
            "desired_outcome", desired_outcome, self.MAX_OUTCOME_CHARS
        )
        if not all((name, email, problem, desired_outcome)) or not _EMAIL.match(email):
            raise ValueError("valid intake fields required")
        if not consent:
            raise ValueError("consent required")
        intake_id = f"intake-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO bheard_intakes VALUES(?,?,?,?,?,?,?,?,?)",
                (intake_id,name,email,problem,desired_outcome,1,"INTAKE_COMPLETED",now,now),
            )
        self.events.append(
            actor="bheard-sandbox", action="INTAKE_COMPLETED", entity_id=intake_id,
            payload={"problem": problem},
        )
        return intake_id

    @staticmethod
    def sign(payload: bytes, secret: str, timestamp: int) -> str:
        signed = str(timestamp).encode() + b"." + payload
        return f"t={timestamp},v1={hmac.new(secret.encode(),signed,hashlib.sha256).hexdigest()}"

    def _check_signature(self, payload: bytes, header: str, now: int) -> None:
        if not isinstance(header, str) or len(header) > self.MAX_SIGNATURE_CHARS:
            raise PaymentError("invalid signature header")
        fields: dict[str,list[str]] = {}
        for part in header.split(","):
            if "=" in part:
                k,v = part.split("=",1)
                fields.setdefault(k.strip(),[]).append(v.strip())
        try:
            ts = int(fields["t"][0])
        except (KeyError,ValueError,IndexError) as exc:
            raise PaymentError("invalid signature header") from exc
        if abs(now-ts) > self.tolerance:
            raise PaymentError("stale webhook")
        expected = hmac.new(
            self.secret, str(ts).encode()+b"."+payload, hashlib.sha256
        ).hexdigest()
        if not any(hmac.compare_digest(expected,x) for x in fields.get("v1",[])):
            raise PaymentError("bad webhook signature")

    def _validate_webhook_bytes(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise PaymentError("webhook payload must be bytes")
        if len(payload) > self.MAX_WEBHOOK_BYTES:
            raise PaymentError("webhook payload too large")

    def _find_existing_task_in_transaction(self, c: Any, payment_id: str) -> str | None:
        fulfillment = c.execute(
            "SELECT task_id FROM bheard_fulfillments WHERE intake_id=(SELECT intake_id FROM bheard_payments WHERE id=?)",
            (payment_id,),
        ).fetchone()
        if fulfillment:
            return str(fulfillment["task_id"])
        task_payload = {"intake_id": str(c.execute(
            "SELECT intake_id FROM bheard_payments WHERE id=?", (payment_id,)
        ).fetchone()["intake_id"]), "payment_receipt_id": payment_id}
        canonical = json.dumps(task_payload, sort_keys=True, separators=(",", ":"))
        legacy = json.dumps(task_payload, sort_keys=True)
        row = c.execute(
            "SELECT id FROM eak_tasks WHERE capability_id=? AND trigger='payment_webhook' "
            "AND payload_json IN (?,?) ORDER BY created_at,id LIMIT 1",
            (self.CAPABILITY, canonical, legacy),
        ).fetchone()
        return str(row["id"]) if row else None

    def _create_task_in_transaction(self, c: Any, *, intake_id: str, payment_id: str) -> str:
        cap = c.execute(
            "SELECT active,triggers_json FROM eak_capabilities WHERE id=?", (self.CAPABILITY,)
        ).fetchone()
        if not cap or not cap["active"]:
            raise PaymentError("sandbox capability unavailable")
        try:
            triggers = json.loads(cap["triggers_json"])
        except json.JSONDecodeError as exc:
            raise PaymentError("sandbox capability trigger contract invalid") from exc
        if "payment_webhook" not in triggers:
            raise PaymentError("sandbox payment trigger unavailable")

        task_id = f"eak-{uuid.uuid4().hex}"
        task_payload = {"intake_id": intake_id, "payment_receipt_id": payment_id}
        payload_json = json.dumps(task_payload, sort_keys=True, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > self.MAX_INTERNAL_TASK_BYTES:
            raise PaymentError("internal sandbox task payload too large")
        now = utc_now()
        c.execute(
            "INSERT INTO eak_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, self.CAPABILITY, "payment_webhook", payload_json,
             "TRIGGERED", None, None, None, now, now),
        )
        self.events.append_in_transaction(
            c,
            actor="eak",
            action="TASK_TRIGGERED",
            entity_id=task_id,
            payload={"capability": self.CAPABILITY, "trigger": "payment_webhook"},
        )
        return task_id

    def _ensure_continuation_in_transaction(
        self, c: Any, *, event_id: str, intake_id: str, payment_id: str
    ) -> str:
        existing = c.execute(
            "SELECT task_id FROM bheard_payment_continuations WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing:
            return str(existing["task_id"])
        task_id = self._find_existing_task_in_transaction(c, payment_id)
        if task_id is None:
            task_id = self._create_task_in_transaction(
                c, intake_id=intake_id, payment_id=payment_id
            )
        c.execute(
            "INSERT INTO bheard_payment_continuations VALUES(?,?,?,?)",
            (event_id, payment_id, task_id, utc_now()),
        )
        return task_id

    def _resume_payment_task(self, task_id: str) -> dict[str, Any]:
        for _ in range(4):
            task = self.eak._task(task_id)
            state = str(task["state"])
            if state == "TRIGGERED":
                try:
                    self.eak.score(
                        task_id, expected_value=1, information_gain=.8, strategic_alignment=1,
                        cost=.05, risk=.05, uncertainty=.05, authority_friction=0,
                    )
                except EAKError:
                    continue
                continue
            if state == "SCORED":
                try:
                    result = self.eak.run(task_id)
                    return {"task_id": task_id, "state": result["state"]}
                except EAKError:
                    refreshed = self.eak._task(task_id)
                    if refreshed["state"] in {"CLOSED", "ABORTED", "QUARANTINED"}:
                        return {"task_id": task_id, "state": refreshed["state"]}
                    raise
            if state in {"CLOSED", "ABORTED", "QUARANTINED"}:
                return {"task_id": task_id, "state": state}
            if state in {"EXECUTING", "VERIFYING"}:
                return {
                    "task_id": task_id,
                    "state": "RECOVERY_REQUIRED",
                    "task_state": state,
                }
            raise PaymentError(f"unknown continuation task state: {state}")
        raise PaymentError("sandbox continuation did not converge")

    def accept_payment(self, payload: bytes, signature: str, *, now: int | None = None) -> dict[str,Any]:
        self._validate_webhook_bytes(payload)
        now = int(time.time()) if now is None else int(now)
        self._check_signature(payload, signature, now)
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError,json.JSONDecodeError) as exc:
            raise PaymentError("invalid JSON") from exc
        if not isinstance(event, dict):
            raise PaymentError("webhook event must be a JSON object")
        if event.get("type") != "checkout.session.completed" or event.get("livemode") is not False:
            raise PaymentError("sandbox accepts test checkout.session.completed only")
        obj = ((event.get("data") or {}).get("object") or {})
        if not isinstance(obj, dict):
            raise PaymentError("invalid checkout session object")
        metadata = obj.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise PaymentError("invalid checkout metadata")
        intake_id = str(metadata.get("intake_id") or "")
        event_id = str(event.get("id") or "")
        if len(intake_id) > self.MAX_ID_CHARS or len(event_id) > self.MAX_ID_CHARS:
            raise PaymentError("webhook identifiers too large")
        if not intake_id or not event_id or obj.get("payment_status") != "paid":
            raise PaymentError("incomplete paid session")
        if obj.get("amount_total") != self.amount or str(obj.get("currency") or "").lower() != self.currency:
            raise PaymentError("payment does not match pinned offer")
        digest = hashlib.sha256(payload).hexdigest()

        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM bheard_intakes WHERE id=?", (intake_id,)).fetchone():
                raise PaymentError("unknown intake")
            old = c.execute("SELECT * FROM bheard_payments WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if old["payload_sha256"] != digest:
                    raise PaymentError("event replay payload changed")
                payment_id = str(old["id"])
            else:
                payment_id = f"pay-{uuid.uuid4().hex}"
                c.execute(
                    "INSERT INTO bheard_payments VALUES(?,?,?,?,?,?,?)",
                    (payment_id,intake_id,event_id,self.amount,self.currency,digest,utc_now()),
                )
                c.execute(
                    "UPDATE bheard_intakes SET state='PAYMENT_VERIFIED',updated_at=? WHERE id=?",
                    (utc_now(),intake_id),
                )

            if not c.execute(
                "SELECT 1 FROM events WHERE action='PAYMENT_VERIFIED_SANDBOX' AND entity_id=? LIMIT 1",
                (payment_id,),
            ).fetchone():
                self.events.append_in_transaction(
                    c,
                    actor="bheard-sandbox",
                    action="PAYMENT_VERIFIED_SANDBOX",
                    entity_id=payment_id,
                    payload={"intake_id": intake_id,"amount_cents":self.amount,"currency":self.currency},
                )

            task_id = self._ensure_continuation_in_transaction(
                c, event_id=event_id, intake_id=intake_id, payment_id=payment_id
            )

        continuation = self._resume_payment_task(task_id)
        return {"payment_receipt_id": payment_id, **continuation}

    def _draft(self, payload: dict[str,Any]) -> dict[str,Any]:
        intake_id = payload["intake_id"]
        with self.db.connect() as c:
            intake = c.execute("SELECT * FROM bheard_intakes WHERE id=?", (intake_id,)).fetchone()
            payment = c.execute(
                "SELECT 1 FROM bheard_payments WHERE id=? AND intake_id=?",
                (payload["payment_receipt_id"],intake_id),
            ).fetchone()
        if not intake or not payment or intake["state"] != "PAYMENT_VERIFIED":
            raise EAKError("verified payment required before fulfillment")
        if not re.fullmatch(r"intake-[0-9a-f]{32}", intake_id):
            raise EAKError("invalid sandbox intake identifier")
        draft_name = f"{intake_id}.md"
        target = self.root / draft_name
        content = (
            "# B Heard Signal Intake - SANDBOX DRAFT\n\n"
            f"Intake: {intake_id}\nProblem: {intake['problem']}\n"
            f"Desired outcome: {intake['outcome']}\n\n"
            "No external delivery occurred. Human approval is required.\n"
        ).encode("utf-8")
        if len(content) > self.MAX_DRAFT_BYTES:
            raise EAKError("sandbox draft exceeds byte limit")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(draft_name, flags, 0o600, dir_fd=self._root_fd)
            try:
                view = memoryview(content)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short sandbox draft write")
                    view = view[written:]
                draft_stat = os.fstat(fd)
                if not stat.S_ISREG(draft_stat.st_mode) or draft_stat.st_size != len(content):
                    raise EAKError("sandbox draft is not a bounded regular file")
            finally:
                os.close(fd)
        except OSError as exc:
            raise EAKError("sandbox draft creation failed closed") from exc
        digest = hashlib.sha256(content).hexdigest()
        now = utc_now()
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO bheard_fulfillments VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"ful-{uuid.uuid4().hex}",intake_id,payload["_task_id"],str(target),
                 digest,"HUMAN_APPROVAL_PENDING",None,None,None,now),
            )
            c.execute(
                "UPDATE bheard_intakes SET state='FULFILLMENT_DRAFTED',updated_at=? WHERE id=?",
                (now,intake_id),
            )
        return {
            "draft_path":str(target),"draft_sha256":digest,"intake_id":intake_id,
            "human_approval_required":True,"sandbox":True,
            "rollback":{"method":"delete_sandbox_draft","path":str(target)},
        }

    def _verify_draft(self, payload: dict[str,Any], result: dict[str,Any]) -> bool:
        intake_id = payload.get("intake_id")
        if not isinstance(intake_id, str) or not re.fullmatch(r"intake-[0-9a-f]{32}", intake_id):
            return False
        draft_name = f"{intake_id}.md"
        if result.get("draft_path") != str(self.root / draft_name):
            return False
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(draft_name, flags, dir_fd=self._root_fd)
            try:
                draft_stat = os.fstat(fd)
                if not stat.S_ISREG(draft_stat.st_mode) or draft_stat.st_size > self.MAX_DRAFT_BYTES:
                    return False
                chunks: list[bytes] = []
                remaining = self.MAX_DRAFT_BYTES + 1
                while remaining:
                    chunk = os.read(fd, min(8192, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                if len(content) > self.MAX_DRAFT_BYTES:
                    return False
            finally:
                os.close(fd)
        except OSError:
            return False
        return (
            result.get("sandbox") is True
            and result.get("human_approval_required") is True
            and result.get("intake_id") == intake_id
            and hashlib.sha256(content).hexdigest() == result.get("draft_sha256")
        )

    def approve(self, intake_id: str, *, actor: str) -> None:
        actor = actor.strip().upper()
        if actor not in {"BANDO","TORI"}:
            raise EAKError("approval requires BANDO or TORI")
        with self.db.connect() as c:
            row = c.execute("SELECT state FROM bheard_fulfillments WHERE intake_id=?", (intake_id,)).fetchone()
            if not row or row["state"] != "HUMAN_APPROVAL_PENDING":
                raise EAKError("not awaiting approval")
            c.execute(
                "UPDATE bheard_fulfillments SET state='HUMAN_APPROVED_SANDBOX',approved_by=?,updated_at=? WHERE intake_id=?",
                (actor,utc_now(),intake_id),
            )
        self.events.append(
            actor=actor.lower(),action="BHEARD_SANDBOX_APPROVED",entity_id=intake_id,payload={}
        )

    def deliver(self, intake_id: str) -> None:
        with self.db.connect() as c:
            row = c.execute("SELECT state FROM bheard_fulfillments WHERE intake_id=?", (intake_id,)).fetchone()
            if not row or row["state"] != "HUMAN_APPROVED_SANDBOX":
                raise EAKError("delivery receipt requires approval")
            c.execute(
                "UPDATE bheard_fulfillments SET state='DELIVERED_SANDBOX',delivered_at=?,updated_at=? WHERE intake_id=?",
                (utc_now(),utc_now(),intake_id),
            )
        self.events.append(
            actor="bheard-sandbox",action="DELIVERY_RECORDED_SANDBOX",entity_id=intake_id,
            payload={"external_send_performed":False},
        )

    def record_outcome(self, intake_id: str, outcome: str) -> None:
        if outcome not in {"opened","replied","requested_refund","upgraded","no_response"}:
            raise ValueError("invalid outcome")
        with self.db.connect() as c:
            row = c.execute("SELECT state FROM bheard_fulfillments WHERE intake_id=?", (intake_id,)).fetchone()
            if not row or row["state"] != "DELIVERED_SANDBOX":
                raise EAKError("outcome requires delivery receipt")
            c.execute(
                "UPDATE bheard_fulfillments SET outcome=?,updated_at=? WHERE intake_id=?",
                (outcome,utc_now(),intake_id),
            )
        self.events.append(
            actor="bheard-sandbox",action="OUTCOME_RECORDED_SANDBOX",entity_id=intake_id,
            payload={"outcome":outcome},
        )
