from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sign_json(key: bytes, value: Any) -> str:
    if not key:
        raise ValueError("signing key must not be empty")
    return hmac.new(key, canonical_json(value).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_json_signature(key: bytes, value: Any, signature: str) -> bool:
    expected = sign_json(key, value)
    return hmac.compare_digest(expected, signature)
