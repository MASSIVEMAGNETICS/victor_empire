from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from organ_authority_resolver import resolve_organ_authority
from victor_body_state import AuthorityState
from victor_runtime.db import VictorDB


NOW = datetime(2026, 9, 2, 23, 0, tzinfo=timezone.utc)


class OrganAuthorityResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = VictorDB(Path(self.tmp.name) / "victor.db")
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO organs(name, repo, role, status, authority, updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    "dev-ville",
                    "MASSIVEMAGNETICS/dev-ville",
                    "software build organ",
                    "ready",
                    "capability-only",
                    NOW.isoformat(),
                ),
            )

    def _work_order(self, work_order_id: str, status: str = "active") -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO work_orders(
                    id, goal, definition_of_done, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    work_order_id,
                    "test",
                    "verified",
                    status,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )

    def _lease(
        self,
        lease_id: str,
        work_order_id: str,
        *,
        expires_at: datetime | str,
        status: str = "active",
        capability: str = "devville.project.build",
        issued_to: str = "dev-ville",
    ) -> None:
        value = expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO leases(
                    id, work_order_id, capability, issued_to,
                    expires_at, status, created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    lease_id,
                    work_order_id,
                    capability,
                    issued_to,
                    value,
                    status,
                    NOW.isoformat(),
                ),
            )

    def test_no_lease_is_blocked(self) -> None:
        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )
        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "no_live_capability_lease")
        self.assertEqual(result.active_lease_ids, ())

    def test_one_live_matching_lease_is_active(self) -> None:
        self._work_order("wo-1")
        self._lease("lease-1", "wo-1", expires_at=NOW + timedelta(minutes=5))

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.ACTIVE)
        self.assertEqual(result.reason, "one_live_capability_lease")
        self.assertEqual(result.active_lease_ids, ("lease-1",))

    def test_expired_active_row_does_not_grant_authority_or_mutate_status(self) -> None:
        self._work_order("wo-1")
        self._lease("lease-1", "wo-1", expires_at=NOW - timedelta(seconds=1))

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "active_lease_expired")
        with self.db.connect() as conn:
            status = conn.execute(
                "SELECT status FROM leases WHERE id='lease-1'"
            ).fetchone()["status"]
        self.assertEqual(status, "active")

    def test_nonexecuting_work_order_blocks_stale_lease(self) -> None:
        self._work_order("wo-1", status="completed")
        self._lease("lease-1", "wo-1", expires_at=NOW + timedelta(minutes=5))

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "active_lease_on_nonexecuting_work_order")

    def test_degraded_organ_is_blocked_even_with_live_lease(self) -> None:
        self._work_order("wo-1")
        self._lease("lease-1", "wo-1", expires_at=NOW + timedelta(minutes=5))
        with self.db.connect() as conn:
            conn.execute("UPDATE organs SET status='degraded' WHERE name='dev-ville'")

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "organ_degraded")

    def test_unregistered_organ_is_blocked(self) -> None:
        result = resolve_organ_authority(
            self.db,
            organ="unknown-organ",
            capability="devville.project.build",
            now=NOW,
        )
        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "organ_unregistered")

    def test_malformed_expiry_fails_closed(self) -> None:
        self._work_order("wo-1")
        self._lease("lease-1", "wo-1", expires_at="not-a-date")

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "malformed_active_lease_expiry")

    def test_multiple_live_matching_leases_are_restricted(self) -> None:
        self._work_order("wo-1")
        self._lease("lease-1", "wo-1", expires_at=NOW + timedelta(minutes=5))
        self._lease("lease-2", "wo-1", expires_at=NOW + timedelta(minutes=5))

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.RESTRICTED)
        self.assertEqual(result.reason, "ambiguous_multiple_live_leases")
        self.assertEqual(result.active_lease_ids, ("lease-1", "lease-2"))

    def test_wrong_capability_and_wrong_recipient_do_not_grant_authority(self) -> None:
        self._work_order("wo-1")
        self._lease(
            "lease-1",
            "wo-1",
            expires_at=NOW + timedelta(minutes=5),
            capability="artifact.write",
            issued_to="local-worker",
        )

        result = resolve_organ_authority(
            self.db,
            organ="dev-ville",
            capability="devville.project.build",
            now=NOW,
        )

        self.assertIs(result.state, AuthorityState.BLOCKED)
        self.assertEqual(result.reason, "no_live_capability_lease")

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            resolve_organ_authority(
                self.db,
                organ="dev-ville",
                capability="devville.project.build",
                now=datetime(2026, 9, 2, 23, 0),
            )


if __name__ == "__main__":
    unittest.main()
