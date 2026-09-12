from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
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
            CREATE TABLE IF NOT EXISTS bheard_fulfillments(
              id TEXT PRIMARY KEY,intake_id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL,
              draft_path TEXT NOT NULL,draft_sha256 TEXT NOT NULL,state TEXT NOT NULL,
              approved_by TEXT,delivered_at TEXT,outcome TEXT,updated_at TEXT NOT NULL,
              FOREIGN KEY(intake_id) REFERENCES bheard_intakes(id));
            CREATE TRIGGER IF NOT EXISTS bheard_payments_no_update BEFORE UPDATE ON bheard_payments
              BEGIN SELECT RAISE(ABORT,'payment receipts immutable'); END;
            CREATE TRIGGER IF NOT EXISTS bheard_payments_no_delete BEFORE DELETE ON bheard_payments
              BEGIN SELECT RAISE(ABORT,'payment receipts immutable'); END;
            """)

    def submit(self, *, name: str, email: str, problem: str, desired_outcome: str,
               consent: bool) -> str:
        name, email, problem, desired_outcome = (
            name.strip(), email.strip(), problem.strip(), desired_outcome.strip()
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

    def accept_payment(self, payload: bytes, signature: str, *, now: int | None = None) -> dict[str,Any]:
        now = int(time.time()) if now is None else int(now)
        self._check_signature(payload, signature, now)
        try:
            event = json.loads(payload.decode())
        except (UnicodeDecodeError,json.JSONDecodeError) as exc:
            raise PaymentError("invalid JSON") from exc
        if event.get("type") != "checkout.session.completed" or event.get("livemode") is not False:
            raise PaymentError("sandbox accepts test checkout.session.completed only")
        obj = ((event.get("data") or {}).get("object") or {})
        intake_id = str((obj.get("metadata") or {}).get("intake_id") or "")
        event_id = str(event.get("id") or "")
        if not intake_id or not event_id or obj.get("payment_status") != "paid":
            raise PaymentError("incomplete paid session")
        if obj.get("amount_total") != self.amount or str(obj.get("currency") or "").lower() != self.currency:
            raise PaymentError("payment does not match pinned offer")
        digest = hashlib.sha256(payload).hexdigest()
        with self.db.connect() as c:
            if not c.execute("SELECT 1 FROM bheard_intakes WHERE id=?", (intake_id,)).fetchone():
                raise PaymentError("unknown intake")
            old = c.execute("SELECT * FROM bheard_payments WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if old["payload_sha256"] != digest:
                    raise PaymentError("event replay payload changed")
                return {"payment_receipt_id": old["id"], "task_id": None, "state": "IDEMPOTENT"}
            pay_id = f"pay-{uuid.uuid4().hex}"
            c.execute(
                "INSERT INTO bheard_payments VALUES(?,?,?,?,?,?,?)",
                (pay_id,intake_id,event_id,self.amount,self.currency,digest,utc_now()),
            )
            c.execute(
                "UPDATE bheard_intakes SET state='PAYMENT_VERIFIED',updated_at=? WHERE id=?",
                (utc_now(),intake_id),
            )
        self.events.append(
            actor="bheard-sandbox", action="PAYMENT_VERIFIED_SANDBOX", entity_id=pay_id,
            payload={"intake_id": intake_id,"amount_cents":self.amount,"currency":self.currency},
        )
        task = self.eak.trigger(
            self.CAPABILITY,"payment_webhook",
            {"intake_id":intake_id,"payment_receipt_id":pay_id},
        )
        self.eak.score(
            task, expected_value=1, information_gain=.8, strategic_alignment=1,
            cost=.05, risk=.05, uncertainty=.05, authority_friction=0,
        )
        result = self.eak.run(task)
        return {"payment_receipt_id":pay_id,"task_id":task,"state":result["state"]}

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
        target = (self.root/f"{intake_id}.md").resolve()
        if target.parent != self.root:
            raise EAKError("draft path escaped workspace")
        target.write_text(
            "# B Heard Signal Intake - SANDBOX DRAFT\n\n"
            f"Intake: {intake_id}\nProblem: {intake['problem']}\n"
            f"Desired outcome: {intake['outcome']}\n\n"
            "No external delivery occurred. Human approval is required.\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
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
        p = Path(result.get("draft_path","")).resolve()
        return (
            p.parent == self.root and p.is_file() and result.get("sandbox") is True
            and result.get("human_approval_required") is True
            and result.get("intake_id") == payload.get("intake_id")
            and hashlib.sha256(p.read_bytes()).hexdigest() == result.get("draft_sha256")
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
