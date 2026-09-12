from __future__ import annotations
import json
import tempfile
import time
import unittest
from pathlib import Path

from victor_runtime import VictorKernel
from empire_eak import (
    Authority, Capability, EAKError, EmpireAutonomyKernel,
    BHeardPaidIntakeSandbox, PaymentError,
)


class EAKTests(unittest.TestCase):
    def env(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        victor=VictorKernel(data_dir=root/".victor",workspace=root/"artifacts")
        return root,victor,EmpireAutonomyKernel(victor)

    def safe(self,eak,*,verify=True,rollback=True):
        eak.register(
            Capability("test.safe","software",Authority.A2_REVERSIBLE,("manual",),"test",True,True),
            executor=lambda p: {"value":p.get("value"),"rollback":{"ok":True}} if rollback else {"value":p.get("value")},
            verifier=lambda p,r: verify and r.get("value")==p.get("value"),
        )

    def score(self,eak,task,good=True):
        return eak.score(
            task, expected_value=1 if good else 0, information_gain=1 if good else 0,
            strategic_alignment=1 if good else 0, cost=0 if good else 1,
            risk=0 if good else 1, uncertainty=0 if good else 1, authority_friction=0,
        )

    def test_a2_happy_path_and_receipt(self):
        root,v,eak=self.env(); self.safe(eak)
        task=eak.trigger("test.safe","manual",{"value":7}); self.score(eak,task)
        out=eak.run(task); self.assertEqual(out["state"],"CLOSED")
        with v.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM eak_receipts").fetchone()["n"],1)

    def test_a5_active_registration_denied(self):
        root,v,eak=self.env()
        with self.assertRaises(EAKError):
            eak.register(Capability("x","gov",Authority.A5_SOVEREIGN,("manual",),"human"))

    def test_a3_execution_denied(self):
        root,v,eak=self.env()
        eak.register(
            Capability("a3","external",Authority.A3_CONSEQUENTIAL,("manual",),"human",True,True),
            verifier=lambda p,r: True,
        )
        task=eak.trigger("a3","manual"); self.score(eak,task)
        with self.assertRaises(EAKError): eak.run(task)

    def test_low_score_does_not_execute(self):
        root,v,eak=self.env(); calls=[]
        eak.register(
            Capability("low","software",Authority.A2_REVERSIBLE,("manual",),"t"),
            executor=lambda p: calls.append(1) or {"rollback":{"ok":True}},
            verifier=lambda p,r: True,
        )
        task=eak.trigger("low","manual"); self.score(eak,task,False)
        with self.assertRaises(EAKError): eak.run(task)
        self.assertEqual(calls,[])

    def test_missing_rollback_quarantines(self):
        root,v,eak=self.env(); self.safe(eak,rollback=False)
        task=eak.trigger("test.safe","manual",{"value":1}); self.score(eak,task)
        with self.assertRaises(EAKError): eak.run(task)
        self.assertEqual(eak.status()["recent_tasks"][0]["state"],"QUARANTINED")

    def test_verifier_failure_quarantines(self):
        root,v,eak=self.env(); self.safe(eak,verify=False)
        task=eak.trigger("test.safe","manual",{"value":1}); self.score(eak,task)
        with self.assertRaises(EAKError): eak.run(task)

    def test_human_stop_aborts_and_blocks(self):
        root,v,eak=self.env(); self.safe(eak)
        task=eak.trigger("test.safe","manual",{"value":1}); self.score(eak,task)
        eak.set_human_stop(True,actor="BANDO",reason="test")
        with self.assertRaises(EAKError): eak.run(task)
        self.assertTrue(eak.human_stop())

    def test_human_stop_during_executor_cannot_be_overwritten_closed(self):
        root,v,eak=self.env()
        def executor(payload):
            eak.set_human_stop(True,actor="BANDO",reason="during-executor")
            return {"value":payload.get("value"),"rollback":{"ok":True}}
        eak.register(
            Capability("stop.race","software",Authority.A2_REVERSIBLE,("manual",),"test",True,True),
            executor=executor,
            verifier=lambda p,r: True,
        )
        task=eak.trigger("stop.race","manual",{"value":1}); self.score(eak,task)
        with self.assertRaises(EAKError): eak.run(task)
        with v.db.connect() as c:
            state=c.execute("SELECT state FROM eak_tasks WHERE id=?",(task,)).fetchone()["state"]
            receipts=c.execute("SELECT COUNT(*) n FROM eak_receipts WHERE task_id=?",(task,)).fetchone()["n"]
        self.assertEqual(state,"ABORTED"); self.assertEqual(receipts,0)

    def test_human_stop_during_verifier_cannot_commit_receipt(self):
        root,v,eak=self.env()
        def verifier(payload,result):
            eak.set_human_stop(True,actor="TORI",reason="during-verifier")
            return True
        eak.register(
            Capability("stop.verify","software",Authority.A2_REVERSIBLE,("manual",),"test",True,True),
            executor=lambda p: {"rollback":{"ok":True}},
            verifier=verifier,
        )
        task=eak.trigger("stop.verify","manual"); self.score(eak,task)
        with self.assertRaises(EAKError): eak.run(task)
        with v.db.connect() as c:
            state=c.execute("SELECT state FROM eak_tasks WHERE id=?",(task,)).fetchone()["state"]
            receipts=c.execute("SELECT COUNT(*) n FROM eak_receipts WHERE task_id=?",(task,)).fetchone()["n"]
        self.assertEqual(state,"ABORTED"); self.assertEqual(receipts,0)

    def test_deactivated_capability_blocks_already_scored_task(self):
        root,v,eak=self.env(); calls=[]
        cap=Capability("revoked","software",Authority.A2_REVERSIBLE,("manual",),"test",True,True)
        eak.register(cap,executor=lambda p: calls.append(1) or {"rollback":{"ok":True}},verifier=lambda p,r: True)
        task=eak.trigger("revoked","manual"); self.score(eak,task)
        eak.register(Capability("revoked","software",Authority.A2_REVERSIBLE,("manual",),"test",True,False))
        with self.assertRaises(EAKError): eak.run(task)
        self.assertEqual(calls,[])
        self.assertEqual(eak.status()["recent_tasks"][0]["state"],"ABORTED")

    def test_non_sovereign_cannot_toggle_stop(self):
        root,v,eak=self.env()
        with self.assertRaises(EAKError): eak.set_human_stop(True,actor="worker")

    def paid_fixture(self,organ,intake,event_id="evt_1",amount=1900,live=False):
        ts=int(time.time())
        event={
          "id":event_id,"type":"checkout.session.completed","livemode":live,
          "data":{"object":{"payment_status":"paid","amount_total":amount,"currency":"usd",
          "metadata":{"intake_id":intake}}}}
        payload=json.dumps(event,separators=(",",":")).encode()
        return payload,organ.sign(payload,"secret",ts),ts

    def test_payment_to_draft_is_closed_loop(self):
        root,v,eak=self.env()
        organ=BHeardPaidIntakeSandbox(eak,workspace=root/"bheard",webhook_secret="secret")
        intake=organ.submit(name="A",email="a@example.com",problem="workflow",desired_outcome="fix",consent=True)
        payload,sig,ts=self.paid_fixture(organ,intake)
        out=organ.accept_payment(payload,sig,now=ts)
        self.assertEqual(out["state"],"CLOSED")
        self.assertTrue((root/"bheard"/f"{intake}.md").exists())
        with v.db.connect() as c:
            state=c.execute("SELECT state FROM bheard_fulfillments WHERE intake_id=?",(intake,)).fetchone()["state"]
        self.assertEqual(state,"HUMAN_APPROVAL_PENDING")

    def test_bad_signature_rejected(self):
        root,v,eak=self.env()
        organ=BHeardPaidIntakeSandbox(eak,workspace=root/"bheard",webhook_secret="secret")
        intake=organ.submit(name="A",email="a@example.com",problem="x",desired_outcome="y",consent=True)
        payload,sig,ts=self.paid_fixture(organ,intake)
        with self.assertRaises(PaymentError): organ.accept_payment(payload,"t=1,v1=bad",now=ts)

    def test_live_or_underpaid_event_rejected(self):
        root,v,eak=self.env()
        organ=BHeardPaidIntakeSandbox(eak,workspace=root/"bheard",webhook_secret="secret")
        intake=organ.submit(name="A",email="a@example.com",problem="x",desired_outcome="y",consent=True)
        for amount,live in ((1900,True),(100,False)):
            payload,sig,ts=self.paid_fixture(organ,intake,event_id=f"e{amount}{live}",amount=amount,live=live)
            with self.assertRaises(PaymentError): organ.accept_payment(payload,sig,now=ts)

    def test_replay_changed_payload_rejected(self):
        root,v,eak=self.env()
        organ=BHeardPaidIntakeSandbox(eak,workspace=root/"bheard",webhook_secret="secret")
        intake=organ.submit(name="A",email="a@example.com",problem="x",desired_outcome="y",consent=True)
        payload,sig,ts=self.paid_fixture(organ,intake,event_id="replay")
        organ.accept_payment(payload,sig,now=ts)
        event=json.loads(payload); event["data"]["object"]["extra"]="changed"
        changed=json.dumps(event,separators=(",",":")).encode()
        with self.assertRaises(PaymentError):
            organ.accept_payment(changed,organ.sign(changed,"secret",ts),now=ts)

    def test_delivery_requires_human_approval(self):
        root,v,eak=self.env()
        organ=BHeardPaidIntakeSandbox(eak,workspace=root/"bheard",webhook_secret="secret")
        intake=organ.submit(name="A",email="a@example.com",problem="x",desired_outcome="y",consent=True)
        payload,sig,ts=self.paid_fixture(organ,intake); organ.accept_payment(payload,sig,now=ts)
        with self.assertRaises(EAKError): organ.deliver(intake)
        organ.approve(intake,actor="TORI"); organ.deliver(intake); organ.record_outcome(intake,"replied")

    def test_payment_receipt_immutable(self):
        root,v,eak=self.env()
        organ=BHeardPaidIntakeSandbox(eak,workspace=root/"bheard",webhook_secret="secret")
        intake=organ.submit(name="A",email="a@example.com",problem="x",desired_outcome="y",consent=True)
        payload,sig,ts=self.paid_fixture(organ,intake); organ.accept_payment(payload,sig,now=ts)
        with self.assertRaises(Exception):
            with v.db.connect() as c: c.execute("UPDATE bheard_payments SET amount_cents=1")


if __name__=="__main__":
    unittest.main()
