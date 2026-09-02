"""Read-only interoception projection for the Victor Command Center.

This module is deliberately outside the execution kernel. It may observe a
bounded organ probe and render body state, but it cannot issue leases, dispatch
jobs, mutate canonical organ status, or infer execution authority from health.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from devville_interoception import DevVilleProbeRunner, observe_devville_probe
from organ_interoception_adapter import build_organ_signal
from victor_body_state import AuthorityState, BodyStateAggregator, OrganDescriptor
from victor_runtime.organs import DEVVILLE_ORGAN


def project_devville_body_state(
    runner: DevVilleProbeRunner,
    *,
    authority: AuthorityState = AuthorityState.UNKNOWN,
) -> dict[str, Any]:
    """Return a display-safe Dev-Ville body-state projection.

    The default authority is UNKNOWN because this observer has no authority
    resolver. Callers may supply an independently resolved AuthorityState, but
    the probe itself is never used to infer permission.
    """

    if not isinstance(authority, AuthorityState):
        raise TypeError("authority must be an AuthorityState")

    observation = observe_devville_probe(runner)
    signal = build_organ_signal(observation, authority=authority)
    body = BodyStateAggregator([OrganDescriptor(DEVVILLE_ORGAN)])
    body.ingest(signal)
    state = body.snapshot()

    return {
        "mode": "read_only",
        "organ": DEVVILLE_ORGAN,
        "authority": authority.value,
        "authority_resolution": (
            "unresolved" if authority is AuthorityState.UNKNOWN else "supplied_by_control_plane"
        ),
        "observation_evidence_sha256": observation.evidence_digest_sha256,
        "observation_detail": observation.detail_code,
        "body": {**asdict(state), "stable": state.stable},
    }
