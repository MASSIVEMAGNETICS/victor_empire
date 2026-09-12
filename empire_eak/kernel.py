from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from victor_runtime import VictorKernel
from victor_runtime.events import utc_now


class EAKError(RuntimeError):
    pass


class Authority(IntEnum):
    A0_OBSERVE = 0
    A1_THINK = 1
    A2_REVERSIBLE = 2
    A3_CONSEQUENTIAL = 3
    A4_ECONOMIC = 4
    A5_SOVEREIGN = 5


@dataclass(frozen=True)
class Capability:
    id: str
    organ: str
    authority: Authority
    triggers: tuple[str, ...]
    verifier: str
    rollback_required: bool = True
    active: bool = True


class EmpireAutonomyKernel:
    """Bounded coordinator over victor_empire's existing canonical DB/event chain."""

    def __init__(self, victor: VictorKernel) -> None:
        self.victor = victor
        self.db = victor.db
        self.events = victor.events
        self.executors: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.verifiers: dict[str, Callable[[dict[str, Any], dict[str, Any]], bool]] = {}
        self._schema()

    def _schema(self) -> None:
        with self.db.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS eak_capabilities(
              id TEXT PRIMARY KEY, organ TEXT NOT NULL, authority INTEGER NOT NULL,
              triggers_json TEXT NOT NULL, verifier TEXT NOT NULL,
              rollback_required INTEGER NOT NULL, active INTEGER NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS eak_tasks(
              id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, trigger TEXT NOT NULL,
              payload_json TEXT NOT NULL, state TEXT NOT NULL, score REAL,
              result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(capability_id) REFERENCES eak_capabilities(id));
            CREATE TABLE IF NOT EXISTS eak_receipts(
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES eak_tasks(id));
            CREATE TABLE IF NOT EXISTS eak_state(
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS eak_receipts_no_update BEFORE UPDATE ON eak_receipts
              BEGIN SELECT RAISE(ABORT,'eak receipts immutable'); END;
            CREATE TRIGGER IF NOT EXISTS eak_receipts_no_delete BEFORE DELETE ON eak_receipts
              BEGIN SELECT RAISE(ABORT,'eak receipts immutable'); END;
            """)
            c.execute(
                "INSERT OR IGNORE INTO eak_state VALUES('human_stop','0',?)", (utc_now(),)
            )

    def human_stop(self) -> bool:
        with self.db.connect() as c:
            row = c.execute("SELECT value FROM eak_state WHERE key='human_stop'").fetchone()
        return bool(row and row["value"] == "1")

    def set_human_stop(self, active: bool, *, actor: str, reason: str = "") -> None:
        actor = actor.strip().upper()
        if actor not in {"BANDO", "TORI"}:
            raise EAKError("Human STOP requires BANDO or TORI")
        with self.db.connect() as c:
            c.execute(
                "UPDATE eak_state SET value=?,updated_at=? WHERE key='human_stop'",
                ("1" if active else "0", utc_now()),
            )
            if active:
                c.execute(
                    "UPDATE eak_tasks SET state='ABORTED',error=?,updated_at=? "
                    "WHERE state NOT IN ('CLOSED','ABORTED','QUARANTINED')",
                    (f"human_stop:{reason}"[:4000], utc_now()),
                )
        self.events.append(
            actor=actor.lower(),
            action="EAK_HUMAN_STOP_ON" if active else "EAK_HUMAN_STOP_OFF",
            entity_id="eak",
            payload={"reason": reason[:1000]},
        )

    def register(
        self,
        cap: Capability,
        *,
        executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        verifier: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None,
    ) -> None:
        if not cap.id or not cap.triggers:
            raise ValueError("capability id and triggers required")
        if cap.authority == Authority.A5_SOVEREIGN and cap.active:
            raise EAKError("A5 can never be autonomous")
        if cap.active and cap.authority <= Authority.A2_REVERSIBLE and executor is None:
            raise EAKError("active A0-A2 capability needs executor")
        if cap.active and verifier is None:
            raise EAKError("active capability needs verifier")
        now = utc_now()
        with self.db.connect() as c:
            old = c.execute("SELECT * FROM eak_capabilities WHERE id=?", (cap.id,)).fetchone()
            contract = (
                cap.organ, int(cap.authority), json.dumps(cap.triggers),
                cap.verifier, int(cap.rollback_required)
            )
            if old:
                prior = (
                    old["organ"], old["authority"], old["triggers_json"],
                    old["verifier"], old["rollback_required"]
                )
                if prior != contract:
                    raise EAKError("capability contract mutation denied")
                c.execute("UPDATE eak_capabilities SET active=? WHERE id=?",
                          (int(cap.active), cap.id))
            else:
                c.execute(
                    "INSERT INTO eak_capabilities VALUES(?,?,?,?,?,?,?,?)",
                    (cap.id, *contract, int(cap.active), now),
                )
        if executor:
            self.executors[cap.id] = executor
        if verifier:
            self.verifiers[cap.id] = verifier
        self.events.append(
            actor="eak", action="CAPABILITY_REGISTERED", entity_id=cap.id,
            payload={"authority": cap.authority.name, "active": cap.active},
        )

    def trigger(self, capability_id: str, trigger: str, payload: dict[str, Any] | None = None) -> str:
        with self.db.connect() as c:
            cap = c.execute("SELECT * FROM eak_capabilities WHERE id=?", (capability_id,)).fetchone()
        if not cap or not cap["active"]:
            raise EAKError("unknown or inactive capability")
        if trigger not in json.loads(cap["triggers_json"]):
            raise EAKError("unregistered trigger")
        task_id = f"eak-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO eak_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, capability_id, trigger, json.dumps(payload or {}, sort_keys=True),
                 "TRIGGERED", None, None, None, now, now),
            )
        self.events.append(
            actor="eak", action="TASK_TRIGGERED", entity_id=task_id,
            payload={"capability": capability_id, "trigger": trigger},
        )
        return task_id

    def score(
        self, task_id: str, *, expected_value: float, information_gain: float,
        strategic_alignment: float, cost: float, risk: float, uncertainty: float,
        authority_friction: float
    ) -> float:
        vals = (expected_value, information_gain, strategic_alignment, cost,
                risk, uncertainty, authority_friction)
        if any(not 0 <= float(v) <= 1 for v in vals):
            raise ValueError("score inputs must be in [0,1]")
        score = max(0.0, min(1.0,
            .30*expected_value + .15*information_gain + .25*strategic_alignment
            - .10*cost - .10*risk - .10*uncertainty - .05*authority_friction))
        with self.db.connect() as c:
            if not c.execute("SELECT 1 FROM eak_tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError(task_id)
            c.execute("UPDATE eak_tasks SET score=?,state='SCORED',updated_at=? WHERE id=?",
                      (score, utc_now(), task_id))
        return score

    def _task(self, task_id: str) -> dict[str, Any]:
        with self.db.connect() as c:
            row = c.execute(
                "SELECT t.*,c.authority,c.rollback_required,c.active "
                "FROM eak_tasks t JOIN eak_capabilities c ON c.id=t.capability_id WHERE t.id=?",
                (task_id,),
            ).fetchone()
        if not row:
            raise KeyError(task_id)
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json"))
        return out

    def _finish(self, task_id: str, state: str, error: str | None = None) -> None:
        with self.db.connect() as c:
            c.execute("UPDATE eak_tasks SET state=?,error=?,updated_at=? WHERE id=?",
                      (state, error, utc_now(), task_id))
        self.events.append(
            actor="eak", action=f"TASK_{state}", entity_id=task_id,
            payload={"error": error},
        )

    def run(self, task_id: str, *, minimum_score: float = .62) -> dict[str, Any]:
        if self.human_stop():
            self._finish(task_id, "ABORTED", "human_stop")
            raise EAKError("Human STOP active")
        task = self._task(task_id)
        authority = Authority(task["authority"])
        if authority >= Authority.A3_CONSEQUENTIAL:
            self._finish(task_id, "ABORTED", "A3-A5 disabled in EAK v0.1")
            raise EAKError("A3-A5 execution disabled")
        if task["score"] is None or task["score"] < minimum_score:
            self._finish(task_id, "ABORTED", "score below threshold")
            raise EAKError("score below threshold")
        executor = self.executors.get(task["capability_id"])
        verifier = self.verifiers.get(task["capability_id"])
        if not executor or not verifier:
            self._finish(task_id, "QUARANTINED", "executor/verifier unavailable")
            raise EAKError("executor/verifier unavailable")
        self._finish(task_id, "EXECUTING")
        try:
            payload = dict(task["payload"])
            payload["_task_id"] = task_id
            result = executor(payload)
            if not isinstance(result, dict):
                raise EAKError("executor result must be dict")
            if authority >= Authority.A2_REVERSIBLE and task["rollback_required"] and not result.get("rollback"):
                raise EAKError("rollback evidence required")
            self._finish(task_id, "VERIFYING")
            if not verifier(payload, result):
                raise EAKError("verification failed")
            receipt_id = f"eakr-{uuid.uuid4().hex}"
            with self.db.connect() as c:
                c.execute(
                    "INSERT INTO eak_receipts VALUES(?,?,?,?,?)",
                    (receipt_id, task_id, "verification",
                     json.dumps(result, sort_keys=True), utc_now()),
                )
                c.execute(
                    "UPDATE eak_tasks SET result_json=?,state='CLOSED',updated_at=? WHERE id=?",
                    (json.dumps(result, sort_keys=True), utc_now(), task_id),
                )
            self.events.append(
                actor="eak", action="RECEIPT_COMMITTED", entity_id=receipt_id,
                payload={"task_id": task_id, "kind": "verification"},
            )
            return {"task_id": task_id, "state": "CLOSED", "receipt_id": receipt_id, "result": result}
        except Exception as exc:
            self._finish(task_id, "QUARANTINED", f"{type(exc).__name__}: {exc}"[:4000])
            raise

    def status(self) -> dict[str, Any]:
        with self.db.connect() as c:
            caps = [dict(x) for x in c.execute(
                "SELECT id,organ,authority,verifier,active FROM eak_capabilities ORDER BY id")]
            tasks = [dict(x) for x in c.execute(
                "SELECT id,capability_id,state,score,error FROM eak_tasks ORDER BY created_at DESC LIMIT 20")]
        for cap in caps:
            cap["authority_name"] = Authority(cap["authority"]).name
            cap["active"] = bool(cap["active"])
        return {"human_stop": self.human_stop(), "capabilities": caps, "recent_tasks": tasks}
