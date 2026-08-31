from __future__ import annotations

import unittest

from devville_interoception import observe_devville_probe
from organ_interoception_adapter import build_organ_signal
from victor_body_state import AuthorityState, ContinuityState
from victor_runtime.organs import DEVVILLE_CAPABILITY, DEVVILLE_ORGAN


class FakeRunner:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    def probe(self):
        if self.error is not None:
            raise self.error
        return self.payload


class DevVilleInteroceptionTests(unittest.TestCase):
    def test_successful_probe_is_liveness_only_and_has_no_authority(self) -> None:
        runner = FakeRunner(
            {
                "organ": DEVVILLE_ORGAN,
                "capabilities": [DEVVILLE_CAPABILITY],
                "version": "test",
            }
        )

        observation = observe_devville_probe(runner)

        self.assertEqual(observation.organ_id, DEVVILLE_ORGAN)
        self.assertEqual(observation.health, 1.0)
        self.assertEqual(observation.continuity, ContinuityState.UNVERIFIED)
        self.assertEqual(observation.detail_code, "probe_reachable_only")
        self.assertFalse(hasattr(observation, "authority"))
        self.assertEqual(len(observation.evidence_digest_sha256), 64)

    def test_successful_probe_digest_is_deterministic_for_equivalent_payloads(self) -> None:
        first = FakeRunner(
            {
                "organ": DEVVILLE_ORGAN,
                "capabilities": [DEVVILLE_CAPABILITY],
                "version": "test",
            }
        )
        second = FakeRunner(
            {
                "version": "test",
                "capabilities": [DEVVILLE_CAPABILITY],
                "organ": DEVVILLE_ORGAN,
            }
        )

        one = observe_devville_probe(first)
        two = observe_devville_probe(second)

        self.assertEqual(one.evidence_digest_sha256, two.evidence_digest_sha256)

    def test_probe_failure_becomes_degraded_observation(self) -> None:
        observation = observe_devville_probe(
            FakeRunner(error=RuntimeError("adapter unavailable")),
            salience=0.75,
        )

        self.assertEqual(observation.health, 0.0)
        self.assertEqual(observation.confidence, 1.0)
        self.assertEqual(observation.salience, 0.75)
        self.assertEqual(observation.continuity, ContinuityState.DEGRADED)
        self.assertEqual(observation.detail_code, "probe_failed:RuntimeError")

    def test_malformed_probe_fails_closed(self) -> None:
        observation = observe_devville_probe(
            FakeRunner({"organ": DEVVILLE_ORGAN, "capabilities": []})
        )

        self.assertEqual(observation.health, 0.0)
        self.assertEqual(observation.continuity, ContinuityState.DEGRADED)
        self.assertEqual(observation.detail_code, "probe_failed:ValueError")

    def test_healthy_probe_cannot_override_control_plane_authority(self) -> None:
        observation = observe_devville_probe(
            FakeRunner(
                {
                    "organ": DEVVILLE_ORGAN,
                    "capabilities": [DEVVILLE_CAPABILITY],
                }
            )
        )

        signal = build_organ_signal(observation, authority=AuthorityState.BLOCKED)

        self.assertEqual(signal.health, 1.0)
        self.assertEqual(signal.authority, AuthorityState.BLOCKED)
        self.assertEqual(signal.continuity, ContinuityState.UNVERIFIED)


if __name__ == "__main__":
    unittest.main()
