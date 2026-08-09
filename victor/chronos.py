"""Canonical hashing and Chronos receipt construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from .protocol import Informatron, SCHEMA_VERSION, utc_now


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def event_hash_payload(event: Informatron) -> dict[str, Any]:
    """Return the exact canonical payload protected by ``event_hash``."""
    payload = event.to_dict()
    payload.pop("event_hash")
    return payload


@dataclass(frozen=True, slots=True)
class ChronosReceipt:
    sequence: int
    event_id: str
    event_hash: str
    previous_chain_hash: str | None
    chain_hash: str


def build_informatron(
    *,
    actor_id: str,
    action: str,
    entity_id: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
    evidence: Mapping[str, Any],
    authority: str,
    previous_event_hash: str | None,
    occurred_at: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    signature: str | None = None,
) -> Informatron:
    recorded_at = utc_now()
    identity = {
        "schema_version": SCHEMA_VERSION,
        "occurred_at": occurred_at or recorded_at,
        "recorded_at": recorded_at,
        "actor_id": actor_id,
        "action": action,
        "entity_id": entity_id,
        "payload": payload,
        "provenance": provenance,
        "evidence": evidence,
        "authority": authority,
        "causation_id": causation_id,
        "correlation_id": correlation_id,
        "idempotency_key": idempotency_key,
        "previous_event_hash": previous_event_hash,
        "signature": signature,
    }
    event_id = f"evt_{uuid4().hex}"
    event_hash = sha256_json({"event_id": event_id, **identity})
    return Informatron(event_id=event_id, event_hash=event_hash, **identity)


def build_receipt(event: Informatron, sequence: int, previous_chain_hash: str | None) -> ChronosReceipt:
    core = {
        "sequence": sequence,
        "event_id": event.event_id,
        "event_hash": event.event_hash,
        "previous_chain_hash": previous_chain_hash,
    }
    return ChronosReceipt(chain_hash=sha256_json(core), **core)
