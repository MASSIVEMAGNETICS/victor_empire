"""Observation-only Dev-Ville interoception adapter.

A successful Dev-Ville probe proves only bounded adapter liveness/identity at the
instant of observation.  It does not prove project execution health, grant a
capability, or establish execution authority.  Accordingly, successful probes
remain ``ContinuityState.UNVERIFIED`` until stronger continuity evidence is
independently supplied by the Victor control plane.
"""

from __future__ import annotations

from typing import Any, Protocol

from organ_interoception_adapter import VerifiedOrganObservation
from victor_body_state import ContinuityState
from victor_runtime.organs import (
    DEVVILLE_CAPABILITY,
    DEVVILLE_ORGAN,
    canonical_json,
    sha256_bytes,
)


class DevVilleProbeRunner(Protocol):
    """Minimal observation surface required from the live Dev-Ville organ."""

    def probe(self) -> dict[str, Any]: ...


def observe_devville_probe(
    runner: DevVilleProbeRunner,
    *,
    load: float = 0.0,
    salience: float = 0.25,
) -> VerifiedOrganObservation:
    """Convert one bounded Dev-Ville probe into interoceptive evidence.

    ``health`` in this adapter means *probe liveness only*.  A value of ``1.0``
    means that the adapter returned the expected organ identity and capability;
    it must not be interpreted as proof that arbitrary Dev-Ville execution will
    succeed.  The returned observation intentionally has no authority field.
    """

    try:
        payload = runner.probe()
        _validate_probe_payload(payload)
        evidence = {
            "schema": "victor.interoception.devville.probe.v1",
            "organ": DEVVILLE_ORGAN,
            "result": "reachable",
            "probe": payload,
        }
        digest = sha256_bytes(canonical_json(evidence))
        return VerifiedOrganObservation(
            organ_id=DEVVILLE_ORGAN,
            health=1.0,
            confidence=1.0,
            load=load,
            salience=salience,
            continuity=ContinuityState.UNVERIFIED,
            evidence_digest_sha256=digest,
            detail_code="probe_reachable_only",
        )
    except Exception as exc:  # observation must fail closed, not escape into authority logic
        return _failed_probe_observation(
            exc,
            load=load,
            salience=salience,
        )


def _validate_probe_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise TypeError("dev-ville probe payload must be an object")
    if payload.get("organ") != DEVVILLE_ORGAN:
        raise ValueError("dev-ville probe identity mismatch")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or DEVVILLE_CAPABILITY not in capabilities:
        raise ValueError("dev-ville capability missing from probe")
    # Ensure the exact evidence we bind into the digest is canonicalizable before
    # the observation can be accepted.
    canonical_json(payload)


def _failed_probe_observation(
    exc: Exception,
    *,
    load: float,
    salience: float,
) -> VerifiedOrganObservation:
    error_type = type(exc).__name__
    error_text = str(exc).strip()[:512]
    evidence = {
        "schema": "victor.interoception.devville.probe.v1",
        "organ": DEVVILLE_ORGAN,
        "result": "failed",
        "error_type": error_type,
        "error": error_text,
    }
    digest = sha256_bytes(canonical_json(evidence))
    return VerifiedOrganObservation(
        organ_id=DEVVILLE_ORGAN,
        health=0.0,
        confidence=1.0,
        load=load,
        salience=salience,
        continuity=ContinuityState.DEGRADED,
        evidence_digest_sha256=digest,
        detail_code=f"probe_failed:{error_type}",
    )
