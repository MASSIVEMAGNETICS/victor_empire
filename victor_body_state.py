"""Bounded organism-level body-state projection for Victor.

This module deliberately exposes state *about* Victor's organs without exposing
or executing their internal implementation.  It is an interoception boundary,
not a new authority path: callers register known organs, submit validated
signals, and receive a deterministic summary suitable for higher-level
cognition, GEV display, or verification receipts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Iterable


class BoundaryClass(str, Enum):
    """Relationship of a component to Victor's bounded self."""

    SELF = "self"
    ORGAN = "organ"
    CAPABILITY = "capability"
    ENVIRONMENT = "environment"


class AuthorityState(str, Enum):
    """Whether an organ may currently perform consequential work."""

    ACTIVE = "active"
    RESTRICTED = "restricted"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ContinuityState(str, Enum):
    """Trust state of an organ's continuity evidence."""

    VERIFIED = "verified"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class OrganDescriptor:
    organ_id: str
    boundary: BoundaryClass = BoundaryClass.ORGAN

    def __post_init__(self) -> None:
        object.__setattr__(self, "organ_id", _clean_id(self.organ_id))
        if self.boundary not in {BoundaryClass.SELF, BoundaryClass.ORGAN}:
            raise ValueError("registered body members must be self or organ")


@dataclass(frozen=True, slots=True)
class OrganSignal:
    """Small, implementation-opaque state vector emitted by one known organ."""

    organ_id: str
    health: float
    confidence: float
    load: float
    salience: float
    authority: AuthorityState
    continuity: ContinuityState
    detail_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "organ_id", _clean_id(self.organ_id))
        object.__setattr__(self, "detail_code", self.detail_code.strip())
        for name in ("health", "confidence", "load", "salience"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a finite number")
            value = float(value)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0.0, 1.0]")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class BodyState:
    """Deterministic organism-level projection of the latest organ signals."""

    organ_count: int
    reporting_count: int
    health: float
    confidence: float
    load: float
    salience: float
    blocked_organs: tuple[str, ...]
    degraded_organs: tuple[str, ...]
    recovering_organs: tuple[str, ...]
    unknown_organs: tuple[str, ...]
    digest_sha256: str

    @property
    def stable(self) -> bool:
        return not self.blocked_organs and not self.degraded_organs and not self.unknown_organs


class BodyStateAggregator:
    """Maintains the bounded self perimeter and projects body-level state.

    Registration is explicit.  A signal from an unregistered organ is rejected
    rather than silently enlarging Victor's sense of self.
    """

    def __init__(self, descriptors: Iterable[OrganDescriptor]) -> None:
        registry: dict[str, OrganDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.organ_id in registry:
                raise ValueError(f"duplicate organ_id: {descriptor.organ_id}")
            registry[descriptor.organ_id] = descriptor
        if not registry:
            raise ValueError("at least one body member must be registered")
        self._registry = registry
        self._signals: dict[str, OrganSignal] = {}

    @property
    def registered_organs(self) -> tuple[str, ...]:
        return tuple(sorted(self._registry))

    def ingest(self, signal: OrganSignal) -> None:
        if signal.organ_id not in self._registry:
            raise ValueError(f"signal outside bounded self perimeter: {signal.organ_id}")
        self._signals[signal.organ_id] = signal

    def snapshot(self) -> BodyState:
        signals = [self._signals[key] for key in sorted(self._signals)]
        unknown = tuple(sorted(set(self._registry) - set(self._signals)))

        if signals:
            health = _mean(signal.health for signal in signals)
            confidence = _mean(signal.confidence for signal in signals)
            load = _mean(signal.load for signal in signals)
            salience = max(signal.salience for signal in signals)
        else:
            health = confidence = load = salience = 0.0

        blocked = tuple(
            signal.organ_id for signal in signals if signal.authority is AuthorityState.BLOCKED
        )
        degraded = tuple(
            signal.organ_id for signal in signals if signal.continuity is ContinuityState.DEGRADED
        )
        recovering = tuple(
            signal.organ_id for signal in signals if signal.continuity is ContinuityState.RECOVERING
        )

        payload = {
            "organ_count": len(self._registry),
            "reporting_count": len(signals),
            "health": health,
            "confidence": confidence,
            "load": load,
            "salience": salience,
            "blocked_organs": blocked,
            "degraded_organs": degraded,
            "recovering_organs": recovering,
            "unknown_organs": unknown,
            "signals": [asdict(signal) for signal in signals],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        return BodyState(
            organ_count=len(self._registry),
            reporting_count=len(signals),
            health=health,
            confidence=confidence,
            load=load,
            salience=salience,
            blocked_organs=blocked,
            degraded_organs=degraded,
            recovering_organs=recovering,
            unknown_organs=unknown,
            digest_sha256=digest,
        )


def _clean_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("organ_id must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("organ_id must not be blank")
    if len(cleaned) > 128:
        raise ValueError("organ_id exceeds 128 characters")
    return cleaned


def _mean(values: Iterable[float]) -> float:
    items = tuple(values)
    return sum(items) / len(items)


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")
