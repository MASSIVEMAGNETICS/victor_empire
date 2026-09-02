"""Fail-closed adapter from verified organ observations to Victor body-state signals.

External organs may report health evidence, but they may not self-grant execution
authority.  This module keeps those channels separate: organ observations carry
implementation-opaque condition data, while the Victor control plane supplies
the independently resolved AuthorityState used in the final OrganSignal.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from victor_body_state import AuthorityState, ContinuityState, OrganSignal


@dataclass(frozen=True, slots=True)
class VerifiedOrganObservation:
    """Bounded condition evidence accepted from a known organ adapter.

    The structure intentionally contains no authority field.  Authority is a
    control-plane decision and must be supplied separately when constructing an
    OrganSignal.
    """

    organ_id: str
    health: float
    confidence: float
    load: float
    salience: float
    continuity: ContinuityState
    evidence_digest_sha256: str
    detail_code: str = ""

    def __post_init__(self) -> None:
        organ_id = _clean_text(self.organ_id, "organ_id", maximum=128)
        detail_code = self.detail_code.strip()
        digest = self.evidence_digest_sha256.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("evidence_digest_sha256 must be 64 lowercase hexadecimal characters")

        for name in ("health", "confidence", "load", "salience"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a finite number")
            numeric = float(value)
            if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                raise ValueError(f"{name} must be within [0.0, 1.0]")
            object.__setattr__(self, name, numeric)

        if not isinstance(self.continuity, ContinuityState):
            raise TypeError("continuity must be a ContinuityState")

        object.__setattr__(self, "organ_id", organ_id)
        object.__setattr__(self, "detail_code", detail_code)
        object.__setattr__(self, "evidence_digest_sha256", digest)


def build_organ_signal(
    observation: VerifiedOrganObservation,
    *,
    authority: AuthorityState,
) -> OrganSignal:
    """Create a body-state signal using control-plane-resolved authority.

    This function does not inspect or infer authority from the organ's evidence.
    A caller must provide an AuthorityState that was independently resolved by
    Victor's governance/capability layer.
    """

    if not isinstance(observation, VerifiedOrganObservation):
        raise TypeError("observation must be a VerifiedOrganObservation")
    if not isinstance(authority, AuthorityState):
        raise TypeError("authority must be an AuthorityState")

    return OrganSignal(
        organ_id=observation.organ_id,
        health=observation.health,
        confidence=observation.confidence,
        load=observation.load,
        salience=observation.salience,
        authority=authority,
        continuity=observation.continuity,
        detail_code=observation.detail_code,
    )


def _clean_text(value: str, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return cleaned
