import unittest

from organ_interoception_adapter import VerifiedOrganObservation, build_organ_signal
from victor_body_state import AuthorityState, ContinuityState


DIGEST = "a" * 64


class OrganInteroceptionAdapterTests(unittest.TestCase):
    def test_observation_cannot_carry_or_self_grant_authority(self) -> None:
        observation = VerifiedOrganObservation(
            organ_id="dev-ville",
            health=0.8,
            confidence=0.9,
            load=0.3,
            salience=0.7,
            continuity=ContinuityState.VERIFIED,
            evidence_digest_sha256=DIGEST,
        )
        signal = build_organ_signal(observation, authority=AuthorityState.RESTRICTED)
        self.assertEqual(signal.authority, AuthorityState.RESTRICTED)
        self.assertFalse(hasattr(observation, "authority"))

    def test_blocked_control_plane_authority_survives_healthy_organ_report(self) -> None:
        observation = VerifiedOrganObservation(
            organ_id="dev-ville",
            health=1.0,
            confidence=1.0,
            load=0.0,
            salience=0.2,
            continuity=ContinuityState.VERIFIED,
            evidence_digest_sha256=DIGEST,
        )
        signal = build_organ_signal(observation, authority=AuthorityState.BLOCKED)
        self.assertEqual(signal.health, 1.0)
        self.assertEqual(signal.continuity, ContinuityState.VERIFIED)
        self.assertEqual(signal.authority, AuthorityState.BLOCKED)

    def test_invalid_evidence_digest_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "64 lowercase hexadecimal"):
            VerifiedOrganObservation(
                organ_id="dev-ville",
                health=0.5,
                confidence=0.5,
                load=0.5,
                salience=0.5,
                continuity=ContinuityState.VERIFIED,
                evidence_digest_sha256="not-a-digest",
            )

    def test_numeric_validation_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "within"):
            VerifiedOrganObservation(
                organ_id="dev-ville",
                health=1.01,
                confidence=0.5,
                load=0.5,
                salience=0.5,
                continuity=ContinuityState.VERIFIED,
                evidence_digest_sha256=DIGEST,
            )
        with self.assertRaisesRegex(TypeError, "finite number"):
            VerifiedOrganObservation(
                organ_id="dev-ville",
                health=True,
                confidence=0.5,
                load=0.5,
                salience=0.5,
                continuity=ContinuityState.VERIFIED,
                evidence_digest_sha256=DIGEST,
            )

    def test_wrong_continuity_and_authority_types_are_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "ContinuityState"):
            VerifiedOrganObservation(
                organ_id="dev-ville",
                health=0.8,
                confidence=0.8,
                load=0.2,
                salience=0.4,
                continuity="verified",  # type: ignore[arg-type]
                evidence_digest_sha256=DIGEST,
            )

        observation = VerifiedOrganObservation(
            organ_id="dev-ville",
            health=0.8,
            confidence=0.8,
            load=0.2,
            salience=0.4,
            continuity=ContinuityState.VERIFIED,
            evidence_digest_sha256=DIGEST,
        )
        with self.assertRaisesRegex(TypeError, "AuthorityState"):
            build_organ_signal(observation, authority="active")  # type: ignore[arg-type]

    def test_digest_normalization_is_deterministic(self) -> None:
        observation = VerifiedOrganObservation(
            organ_id="  dev-ville  ",
            health=0.7,
            confidence=0.8,
            load=0.3,
            salience=0.6,
            continuity=ContinuityState.RECOVERING,
            evidence_digest_sha256="A" * 64,
            detail_code="  recovery-check  ",
        )
        self.assertEqual(observation.organ_id, "dev-ville")
        self.assertEqual(observation.evidence_digest_sha256, DIGEST)
        self.assertEqual(observation.detail_code, "recovery-check")


if __name__ == "__main__":
    unittest.main()
