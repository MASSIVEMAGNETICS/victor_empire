import unittest

from victor_body_state import (
    AuthorityState,
    BodyStateAggregator,
    BoundaryClass,
    ContinuityState,
    OrganDescriptor,
    OrganSignal,
)


class BodyStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = BodyStateAggregator(
            [
                OrganDescriptor("victor", BoundaryClass.SELF),
                OrganDescriptor("memory"),
                OrganDescriptor("ethica"),
            ]
        )

    def test_unknown_signal_cannot_expand_self_perimeter(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside bounded self perimeter"):
            self.body.ingest(
                OrganSignal(
                    "foreign-tool",
                    health=1.0,
                    confidence=1.0,
                    load=0.0,
                    salience=1.0,
                    authority=AuthorityState.ACTIVE,
                    continuity=ContinuityState.VERIFIED,
                )
            )

    def test_non_body_boundary_cannot_be_registered_as_organ(self) -> None:
        with self.assertRaisesRegex(ValueError, "self or organ"):
            OrganDescriptor("stripe", BoundaryClass.ENVIRONMENT)

    def test_snapshot_reports_unknown_body_members_without_inventing_health(self) -> None:
        self.body.ingest(
            OrganSignal(
                "victor",
                health=0.8,
                confidence=0.7,
                load=0.25,
                salience=0.6,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            )
        )
        state = self.body.snapshot()
        self.assertEqual(state.organ_count, 3)
        self.assertEqual(state.reporting_count, 1)
        self.assertEqual(state.unknown_organs, ("ethica", "memory"))
        self.assertAlmostEqual(state.health, 0.8)
        self.assertFalse(state.stable)

    def test_blocked_and_degraded_organs_surface_to_body_state(self) -> None:
        self.body.ingest(
            OrganSignal(
                "victor",
                health=1.0,
                confidence=1.0,
                load=0.1,
                salience=0.2,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            )
        )
        self.body.ingest(
            OrganSignal(
                "memory",
                health=0.4,
                confidence=0.5,
                load=0.9,
                salience=0.95,
                authority=AuthorityState.BLOCKED,
                continuity=ContinuityState.DEGRADED,
                detail_code="hash-chain-mismatch",
            )
        )
        self.body.ingest(
            OrganSignal(
                "ethica",
                health=0.9,
                confidence=0.95,
                load=0.2,
                salience=0.8,
                authority=AuthorityState.RESTRICTED,
                continuity=ContinuityState.RECOVERING,
            )
        )
        state = self.body.snapshot()
        self.assertEqual(state.blocked_organs, ("memory",))
        self.assertEqual(state.unknown_authority_organs, ())
        self.assertEqual(state.degraded_organs, ("memory",))
        self.assertEqual(state.recovering_organs, ("ethica",))
        self.assertEqual(state.unverified_organs, ())
        self.assertEqual(state.unknown_organs, ())
        self.assertAlmostEqual(state.salience, 0.95)
        self.assertFalse(state.stable)

    def test_unknown_authority_is_not_stable(self) -> None:
        body = BodyStateAggregator([OrganDescriptor("dev-ville")])
        body.ingest(
            OrganSignal(
                "dev-ville",
                health=1.0,
                confidence=1.0,
                load=0.0,
                salience=0.25,
                authority=AuthorityState.UNKNOWN,
                continuity=ContinuityState.VERIFIED,
            )
        )
        state = body.snapshot()
        self.assertEqual(state.unknown_authority_organs, ("dev-ville",))
        self.assertFalse(state.stable)

    def test_unverified_continuity_is_not_stable(self) -> None:
        body = BodyStateAggregator([OrganDescriptor("dev-ville")])
        body.ingest(
            OrganSignal(
                "dev-ville",
                health=1.0,
                confidence=1.0,
                load=0.0,
                salience=0.25,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.UNVERIFIED,
            )
        )
        state = body.snapshot()
        self.assertEqual(state.unverified_organs, ("dev-ville",))
        self.assertFalse(state.stable)

    def test_recovering_continuity_is_not_stable(self) -> None:
        body = BodyStateAggregator([OrganDescriptor("dev-ville")])
        body.ingest(
            OrganSignal(
                "dev-ville",
                health=1.0,
                confidence=1.0,
                load=0.0,
                salience=0.25,
                authority=AuthorityState.RESTRICTED,
                continuity=ContinuityState.RECOVERING,
            )
        )
        state = body.snapshot()
        self.assertEqual(state.recovering_organs, ("dev-ville",))
        self.assertFalse(state.stable)

    def test_signal_validation_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "within"):
            OrganSignal(
                "memory",
                health=float("nan"),
                confidence=0.5,
                load=0.5,
                salience=0.5,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            )
        with self.assertRaisesRegex(TypeError, "finite number"):
            OrganSignal(
                "memory",
                health=True,
                confidence=0.5,
                load=0.5,
                salience=0.5,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            )
        with self.assertRaisesRegex(TypeError, "AuthorityState"):
            OrganSignal(
                "memory",
                health=1.0,
                confidence=1.0,
                load=0.0,
                salience=0.5,
                authority="active",  # type: ignore[arg-type]
                continuity=ContinuityState.VERIFIED,
            )
        with self.assertRaisesRegex(TypeError, "ContinuityState"):
            OrganSignal(
                "memory",
                health=1.0,
                confidence=1.0,
                load=0.0,
                salience=0.5,
                authority=AuthorityState.ACTIVE,
                continuity="verified",  # type: ignore[arg-type]
            )

    def test_snapshot_digest_is_deterministic_and_order_independent(self) -> None:
        signals = [
            OrganSignal(
                "memory",
                health=0.7,
                confidence=0.8,
                load=0.3,
                salience=0.4,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            ),
            OrganSignal(
                "victor",
                health=0.9,
                confidence=0.85,
                load=0.2,
                salience=0.6,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            ),
            OrganSignal(
                "ethica",
                health=1.0,
                confidence=0.95,
                load=0.1,
                salience=0.5,
                authority=AuthorityState.ACTIVE,
                continuity=ContinuityState.VERIFIED,
            ),
        ]
        first = BodyStateAggregator(
            [OrganDescriptor("victor", BoundaryClass.SELF), OrganDescriptor("memory"), OrganDescriptor("ethica")]
        )
        second = BodyStateAggregator(
            [OrganDescriptor("ethica"), OrganDescriptor("victor", BoundaryClass.SELF), OrganDescriptor("memory")]
        )
        for signal in signals:
            first.ingest(signal)
        for signal in reversed(signals):
            second.ingest(signal)
        self.assertEqual(first.snapshot().digest_sha256, second.snapshot().digest_sha256)
        self.assertTrue(first.snapshot().stable)


if __name__ == "__main__":
    unittest.main()
