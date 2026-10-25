"""The egress key's "renew due" trigger (19a, Phill np-72642220: the egress key keeps its 30-day expiry, plus a renew
ping a few days before, answered with one Face ID tap on the phone; CH-D2 S8).

At launch, :func:`renew_due` reads each keyed route's expiry from the AUTHORITY record (`expires_at` of the setup
record in the replay the launch built its routes from: the expiry's one home; the route snapshot carries none) and
names every record whose expiry falls within `RENEW_DUE` of now. `launch.start_egress` computes it before it
allocates anything (a failure here refuses the launch) and hands a non-empty list to its `on_renew_due` callback once
per launch, off the start path. The phone half (the push, the one-tap re-sign of a renewed record) is not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = ["RENEW_DUE", "RenewDue", "renew_due"]

#: DECISION [judged by lane-chd2-s8b, 10-25, against 19a's "a few days before"]: the head's / Phill's to confirm
RENEW_DUE = timedelta(days=3)


@dataclass(frozen=True)
class RenewDue:
    record_id: str
    host: str
    expires_at: datetime


def renew_due(authority: Any, routes: Any, *, now: datetime, window: timedelta = RENEW_DUE) -> list[RenewDue]:
    """The launch's keyed records expiring within `window` of `now`, soonest first. A route whose record the
    authority no longer holds, or whose expiry does not parse, is not named (it attaches nothing either way). A naive
    `now` is a ValueError; an authority or route set of another shape raises as itself."""
    from levain.cockpit.consent import parse_stamp

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"renew_due needs an aware now, got {now!r}")
    grants = authority.policy.grants
    out = []
    for r in routes.routes.values():
        g = grants.get(r.record_id)
        try:
            exp = parse_stamp(g["expires_at"])          # aware UTC, so the subtraction below cannot raise
        except Exception:  # noqa: BLE001 - an absent or malformed record is not due, it is not live
            continue
        if exp - now <= window:
            out.append(RenewDue(record_id=r.record_id, host=r.canonical_host, expires_at=exp))
    return sorted(out, key=lambda d: (d.expires_at, d.record_id))
