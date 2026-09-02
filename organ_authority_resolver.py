"""Read-only authority resolution for Victor organ interoception.

This module projects *existing* canonical control-plane lease state into an
``AuthorityState`` suitable for display/interoception. It never grants, closes,
expires, consumes, or otherwise mutates a capability lease.

The resolver is intentionally conservative:
- an unregistered or degraded organ is BLOCKED;
- no matching live lease is BLOCKED;
- one valid matching live lease is ACTIVE;
- multiple simultaneously valid matching leases are RESTRICTED as ambiguous;
- stale/expired/mismatched lease rows never become authority.

Condition evidence (health/liveness) is deliberately absent from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from victor_body_state import AuthorityState
from victor_runtime.db import VictorDB


_EXECUTABLE_WORK_ORDER_STATES = {"active", "running"}


@dataclass(frozen=True, slots=True)
class AuthorityResolution:
    organ: str
    capability: str
    state: AuthorityState
    reason: str
    active_lease_ids: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def resolve_organ_authority(
    db: VictorDB,
    *,
    organ: str,
    capability: str,
    now: datetime | None = None,
) -> AuthorityResolution:
    """Resolve current execution authority from persisted control-plane state.

    This is a pure read projection over ``organs``, ``leases`` and
    ``work_orders``. It does not call the governor and cannot create authority.
    """

    clean_organ = organ.strip()
    clean_capability = capability.strip()
    if not clean_organ:
        raise ValueError("organ cannot be empty")
    if not clean_capability:
        raise ValueError("capability cannot be empty")

    resolved_now = now or _utc_now()
    if resolved_now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    resolved_now = resolved_now.astimezone(timezone.utc)

    with db.connect() as conn:
        organ_row = conn.execute(
            "SELECT name, status, authority FROM organs WHERE name=?",
            (clean_organ,),
        ).fetchone()
        if organ_row is None:
            return AuthorityResolution(
                organ=clean_organ,
                capability=clean_capability,
                state=AuthorityState.BLOCKED,
                reason="organ_unregistered",
            )

        if organ_row["status"] == "degraded":
            return AuthorityResolution(
                organ=clean_organ,
                capability=clean_capability,
                state=AuthorityState.BLOCKED,
                reason="organ_degraded",
            )

        rows = conn.execute(
            """
            SELECT
                leases.id AS lease_id,
                leases.expires_at AS expires_at,
                leases.status AS lease_status,
                work_orders.status AS work_order_status
            FROM leases
            JOIN work_orders ON work_orders.id = leases.work_order_id
            WHERE leases.issued_to=? AND leases.capability=?
            ORDER BY leases.created_at, leases.id
            """,
            (clean_organ, clean_capability),
        ).fetchall()

    valid_ids: list[str] = []
    saw_active_expired = False
    saw_active_malformed = False
    saw_active_stale_work = False

    for row in rows:
        if row["lease_status"] != "active":
            continue
        expiry = _parse_expiry(row["expires_at"])
        if expiry is None:
            saw_active_malformed = True
            continue
        if expiry <= resolved_now:
            saw_active_expired = True
            continue
        if row["work_order_status"] not in _EXECUTABLE_WORK_ORDER_STATES:
            saw_active_stale_work = True
            continue
        valid_ids.append(row["lease_id"])

    if len(valid_ids) == 1:
        return AuthorityResolution(
            organ=clean_organ,
            capability=clean_capability,
            state=AuthorityState.ACTIVE,
            reason="one_live_capability_lease",
            active_lease_ids=tuple(valid_ids),
        )

    if len(valid_ids) > 1:
        return AuthorityResolution(
            organ=clean_organ,
            capability=clean_capability,
            state=AuthorityState.RESTRICTED,
            reason="ambiguous_multiple_live_leases",
            active_lease_ids=tuple(valid_ids),
        )

    if saw_active_malformed:
        reason = "malformed_active_lease_expiry"
    elif saw_active_stale_work:
        reason = "active_lease_on_nonexecuting_work_order"
    elif saw_active_expired:
        reason = "active_lease_expired"
    else:
        reason = "no_live_capability_lease"

    return AuthorityResolution(
        organ=clean_organ,
        capability=clean_capability,
        state=AuthorityState.BLOCKED,
        reason=reason,
    )
