from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from victor_runtime import VictorKernel
from victor_runtime.events import utc_now


class EAKError(RuntimeError):
    pass


TRUSTED_MINIMUM_SCORE = 0.62
TERMINAL_TASK_STATES = frozenset({"CLOSED", "ABORTED", "QUARANTINED"})


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
            CREATE UNIQUE INDEX IF NOT EXISTS eak_one_verification_receipt_per_task
              ON eak_receipts(task_id) WHERE kind='verification';
            CREATE TRIGGER IF NOT EXISTS eak_terminal_tasks_no_update
              BEFORE UPDATE ON eak_tasks
              WHEN OLD.state IN ('CLOSED','ABORTED','QUARANTINED')
              BEGIN SELECT RAISE(ABORT,'eak terminal tasks immutable'); END;
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
            c.execute("BEGIN IMMEDIATE")
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
            self.events.append_in_transaction(
                c,
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
        if executor and cap.id in self.executors and self.executors[cap.id] is not executor:
            raise EAKError("capability executor rebind denied; use a new capability version")
        if verifier and cap.id in self.verifiers and self.verifiers[cap.id] is not verifier:
            raise EAKError("capability verifier rebind denied; use a new capability version")
        now = utc_now()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
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
                if not cap.active:
                    c.execute(
                        "UPDATE eak_tasks SET state='ABORTED',error='capability inactive',updated_at=? "
                        "WHERE capability_id=? AND state NOT IN ('CLOSED','ABORTED','QUARANTINED')",
                        (now, cap.id),
                    )
            else:
                c.execute(
                    "INSERT INTO eak_capabilities VALUES(?,?,?,?,?,?,?,?)",
                    (cap.id, *contract, int(cap.active), now),
                )
            self.events.append_in_transaction(
                c,
                actor="eak",
                action="CAPABILITY_REGISTERED",
                entity_id=cap.id,
                payload={"authority": cap.authority.name, "active": cap.active},
            )
        if executor:
            self.executors[cap.id] = executor
        if verifier:
            self.verifiers[cap.id] = verifier

    def trigger(self, capability_id: str, trigger: str, payload: dict[str, Any] | None = None) -> str:
        task_id = f"eak-{uuid.uuid4().hex}"
        now = utc_now()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cap = c.execute(
                "SELECT * FROM eak_capabilities WHERE id=?", (capability_id,)
            ).fetchone()
            if not cap or not cap["active"]:
                raise EAKError("unknown or inactive capability")
            if trigger not in json.loads(cap["triggers_json"]):
                raise EAKError("unregistered trigger")
            c.execute(
                "INSERT INTO eak_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, capability_id, trigger, json.dumps(payload or {}, sort_keys=True),
                 "TRIGGERED", None, None, None, now, now),
            )
            self.events.append_in_transaction(
                c,
                actor="eak",
                action="TASK_TRIGGERED",
                entity_id=task_id,
                payload={"capability": capability_id, "trigger": trigger},
            )
        return task_id

    def score(
        self, task_id: str, *, expected_value: float, information_gain: float,
        strategic_alignment: float, cost: float, risk: float, uncertainty: float,
        authority_friction: float
    ) -> float:
        vals = tuple(float(v) for v in (
            expected_value, information_gain, strategic_alignment, cost,
            risk, uncertainty, authority_friction
        ))
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in vals):
            raise ValueError("score inputs must be in [0,1]")
        (expected_value, information_gain, strategic_alignment, cost,
         risk, uncertainty, authority_friction) = vals
        score = max(0.0, min(1.0,
            .30*expected_value + .15*information_gain + .25*strategic_alignment
            - .10*cost - .10*risk - .10*uncertainty - .05*authority_friction))
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            task = c.execute("SELECT state FROM eak_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["state"] != "TRIGGERED":
                raise EAKError(f"task cannot be scored from state {task['state']}")
            changed = c.execute(
                "UPDATE eak_tasks SET score=?,state='SCORED',updated_at=? "
                "WHERE id=? AND state='TRIGGERED'",
                (score, utc_now(), task_id),
            )
            if changed.rowcount != 1:
                raise EAKError("task score lost eligibility")
            self.events.append_in_transaction(
                c,
                actor="eak",
                action="TASK_SCORED",
                entity_id=task_id,
                payload={"score": score},
            )
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

    def _admit(
        self,
        task_id: str,
        threshold: float,
    ) -> tuple[
        dict[str, Any],
        Callable[[dict[str, Any]], dict[str, Any]],
        Callable[[dict[str, Any], dict[str, Any]], bool],
    ]:
        failure: tuple[str, str] | None = None
        task: dict[str, Any] | None = None
        executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        verifier: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT t.*,c.authority,c.rollback_required,c.active,s.value AS human_stop "
                "FROM eak_tasks t JOIN eak_capabilities c ON c.id=t.capability_id "
                "JOIN eak_state s ON s.key='human_stop' WHERE t.id=?",
                (task_id,),
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["state"] != "SCORED":
                raise EAKError(f"task cannot execute from state {row['state']}")

            authority = Authority(row["authority"])
            executor = self.executors.get(row["capability_id"])
            verifier = self.verifiers.get(row["capability_id"])
            if row["human_stop"] == "1":
                failure = ("ABORTED", "human_stop")
            elif not row["active"]:
                failure = ("ABORTED", "capability inactive")
            elif authority >= Authority.A3_CONSEQUENTIAL:
                failure = ("ABORTED", "A3-A5 execution disabled")
            elif row["score"] is None or row["score"] < threshold:
                failure = ("ABORTED", "score below trusted threshold")
            elif not executor or not verifier:
                failure = ("QUARANTINED", "executor/verifier unavailable")

            if failure:
                state, error = failure
                changed = c.execute(
                    "UPDATE eak_tasks SET state=?,error=?,updated_at=? "
                    "WHERE id=? AND state='SCORED'",
                    (state, error, utc_now(), task_id),
                )
                if changed.rowcount != 1:
                    raise EAKError("task admission rejection lost eligibility")
                self.events.append_in_transaction(
                    c,
                    actor="eak",
                    action=f"TASK_{state}",
                    entity_id=task_id,
                    payload={"error": error},
                )
            else:
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError as exc:
                    raise EAKError("task payload is not valid JSON") from exc
                if not isinstance(payload, dict):
                    raise EAKError("task payload must be a JSON object")
                changed = c.execute(
                    "UPDATE eak_tasks SET state='EXECUTING',error=NULL,updated_at=? "
                    "WHERE id=? AND state='SCORED'",
                    (utc_now(), task_id),
                )
                if changed.rowcount != 1:
                    raise EAKError("task admission lost eligibility")
                self.events.append_in_transaction(
                    c,
                    actor="eak",
                    action="TASK_EXECUTING",
                    entity_id=task_id,
                    payload={"threshold": threshold},
                )
                task = dict(row)
                task["payload"] = payload

        if failure:
            raise EAKError(failure[1])
        if task is None or executor is None or verifier is None:
            raise EAKError("task admission failed closed")
        return task, executor, verifier

    def _begin_verification(self, task_id: str) -> None:
        failure: str | None = None
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            gate = c.execute(
                "SELECT t.state,c.active,s.value AS human_stop "
                "FROM eak_tasks t JOIN eak_capabilities c ON c.id=t.capability_id "
                "JOIN eak_state s ON s.key='human_stop' WHERE t.id=?",
                (task_id,),
            ).fetchone()
            if not gate:
                raise KeyError(task_id)
            if gate["state"] != "EXECUTING":
                raise EAKError(f"task cannot verify from state {gate['state']}")
            if gate["human_stop"] == "1":
                failure = "human_stop"
            elif not gate["active"]:
                failure = "capability inactive"

            state = "ABORTED" if failure else "VERIFYING"
            changed = c.execute(
                "UPDATE eak_tasks SET state=?,error=?,updated_at=? "
                "WHERE id=? AND state='EXECUTING'",
                (state, failure, utc_now(), task_id),
            )
            if changed.rowcount != 1:
                raise EAKError("task verification admission lost eligibility")
            self.events.append_in_transaction(
                c,
                actor="eak",
                action=f"TASK_{state}",
                entity_id=task_id,
                payload={"error": failure},
            )
        if failure:
            raise EAKError(failure)

    def _settle_failure(self, task_id: str, exc: Exception) -> None:
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT t.state,c.active,s.value AS human_stop "
                "FROM eak_tasks t JOIN eak_capabilities c ON c.id=t.capability_id "
                "JOIN eak_state s ON s.key='human_stop' WHERE t.id=?",
                (task_id,),
            ).fetchone()
            if not row or row["state"] in TERMINAL_TASK_STATES:
                return
            state = (
                "ABORTED"
                if row["human_stop"] == "1" or not row["active"]
                else "QUARANTINED"
            )
            error = (
                "human_stop"
                if row["human_stop"] == "1"
                else str(exc)[:4000]
                if state == "ABORTED"
                else f"{type(exc).__name__}: {exc}"[:4000]
            )
            changed = c.execute(
                "UPDATE eak_tasks SET state=?,error=?,updated_at=? "
                "WHERE id=? AND state NOT IN ('CLOSED','ABORTED','QUARANTINED')",
                (state, error, utc_now(), task_id),
            )
            if changed.rowcount != 1:
                return
            self.events.append_in_transaction(
                c,
                actor="eak",
                action=f"TASK_{state}",
                entity_id=task_id,
                payload={"error": error},
            )

    def run(
        self,
        task_id: str,
        *,
        minimum_score: float = TRUSTED_MINIMUM_SCORE,
    ) -> dict[str, Any]:
        try:
            threshold = float(minimum_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("minimum_score must be numeric") from exc
        if (
            not math.isfinite(threshold)
            or threshold < TRUSTED_MINIMUM_SCORE
            or threshold > 1.0
        ):
            raise ValueError(
                f"minimum_score must be between trusted floor {TRUSTED_MINIMUM_SCORE} and 1"
            )

        task, executor, verifier = self._admit(task_id, threshold)
        try:
            payload = dict(task["payload"])
            payload["_task_id"] = task_id
            result = executor(payload)
            if not isinstance(result, dict):
                raise EAKError("executor result must be dict")
            authority = Authority(task["authority"])
            if (
                authority >= Authority.A2_REVERSIBLE
                and task["rollback_required"]
                and not result.get("rollback")
            ):
                raise EAKError("rollback evidence required")

            self._begin_verification(task_id)
            if not verifier(payload, result):
                raise EAKError("verification failed")

            receipt_id = f"eakr-{uuid.uuid4().hex}"
            result_json = json.dumps(result, sort_keys=True)
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                gate = c.execute(
                    "SELECT t.state,c.active,s.value AS human_stop "
                    "FROM eak_tasks t JOIN eak_capabilities c ON c.id=t.capability_id "
                    "JOIN eak_state s ON s.key='human_stop' WHERE t.id=?",
                    (task_id,),
                ).fetchone()
                if not gate or gate["state"] != "VERIFYING":
                    raise EAKError("task no longer eligible to close")
                if not gate["active"]:
                    raise EAKError("capability inactive")
                if gate["human_stop"] == "1":
                    raise EAKError("Human STOP active")
                c.execute(
                    "INSERT INTO eak_receipts VALUES(?,?,?,?,?)",
                    (receipt_id, task_id, "verification", result_json, utc_now()),
                )
                changed = c.execute(
                    "UPDATE eak_tasks SET result_json=?,state='CLOSED',updated_at=? "
                    "WHERE id=? AND state='VERIFYING'",
                    (result_json, utc_now(), task_id),
                )
                if changed.rowcount != 1:
                    raise EAKError("task close lost eligibility")
                self.events.append_in_transaction(
                    c,
                    actor="eak",
                    action="RECEIPT_COMMITTED",
                    entity_id=receipt_id,
                    payload={"task_id": task_id, "kind": "verification"},
                )
            return {
                "task_id": task_id,
                "state": "CLOSED",
                "receipt_id": receipt_id,
                "result": result,
            }
        except Exception as exc:
            self._settle_failure(task_id, exc)
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
