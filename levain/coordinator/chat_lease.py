"""levain.coordinator.chat_lease — the turn lease: one fenced owner among Levain clients per worker launch (CH-D1 §5).

A worker launch's turn is held by at most one EPOCH at a time, recorded as an append-only run of ``turn_lease_events``
in ``dispatch.db``. The epoch is the ``seq`` of the row that OPENED the holding: ``claim`` (the turn was free),
``handoff`` (the holder passed it on), ``take`` (your explicit "take the turn", from any of your surfaces, at any time)
or ``outside`` (the service saw a line or an answer from a client Levain cannot fence: the terminal, the vendor's app;
rev 3 §5.4: any ``user_item`` not PROVEN to be a Levain message, by a vendor correlation id or the send's own accepted
item id, and never decided by a digest match; that trigger is CH-D1b's, this module only records the epoch).
``beat``, ``outside_beat``, ``release`` and ``lapse`` carry ``token`` = the epoch they affect, and the edge trigger
refuses one whose token is not the launch's LATEST opening, so a delayed release for epoch N can never end N+1.

An epoch is live while it is the launch's latest opening, no ``release`` or ``lapse`` carries its token, the store's
clock is before the furthest ``until`` among it and its beats, and (for a Levain client's epoch) a send passed its
fence within :data:`HOLD_CAP` of now (or of the opening). A read that finds an epoch no longer live writes its
``lapse`` (or, for a launch that is over, its ``release``) in the reading transaction: :func:`settle`.

The fence (:func:`turn_fence`) names an AUTHENTICATED owner, ``"<surface>:<client_id>"``, which the service derives
from the caller's session and never from a request body: an epoch is a public sequence number, not a bearer secret, so
the right epoch from the wrong owner is refused. The coordinator never holds a worker's turn (Q2): no owner it could
name is a client owner.

Not authority (C0 §2): before S1 any process of your uid can write this file. The lease keeps two of your own clients
from both typing into one turn; it grants nothing, and narrowing (stop, kill, a native deny) is never fenced (Q1).

[seat] numbers the design marks ``[seat]`` (CH-D1 rev 2 §5.3, §5.4): :data:`TURN_TTL`, :data:`HOLD_CAP`,
:data:`OUTSIDE_TTL`. ``owner_kind`` ``none`` (in rev 2's column list) is not built: every row names its epoch's owner,
so no row could carry it [judged by chd1a, 10-23, against §5.1's rule that every non-opening row names its epoch].
Stdlib only.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.coordinator.store import CoordError, _append_only, _in, next_gseq

__all__ = [
    "CLIENT_SURFACES", "HOLD_CAP", "OPENINGS", "OUTSIDE_KINDS", "OUTSIDE_TTL", "TURN_KINDS", "TURN_LEASE_SCHEMA",
    "TURN_TTL", "TurnLease", "TurnRefused", "TurnView", "beat", "check_owner", "claim", "handoff", "history", "outside",
    "release", "settle", "take", "turn_fence", "turn_lease", "view",
]

OPENINGS = ("claim", "handoff", "take", "outside")
TURN_KINDS = (*OPENINGS, "beat", "outside_beat", "release", "lapse")
CLIENT_SURFACES = ("cockpit", "phone", "cli")
OUTSIDE_KINDS = ("terminal", "vendor_app")
_OWNER_KINDS = ("client", *OUTSIDE_KINDS)
_CLIENT_OPENINGS = ("claim", "handoff", "take")

# [seat] a Levain client's epoch lives this long past its opening or its last beat; beats come only on real composer
# activity (a keystroke, a focus, a send), so a locked phone stops beating (complement F7)
TURN_TTL = timedelta(seconds=120)
# [seat] a Levain client's epoch with no send for this long is lapsed by the next read, whatever its beats
HOLD_CAP = timedelta(minutes=10)
# [seat] an outside epoch lives this long past the last outside event the service saw
OUTSIDE_TTL = timedelta(seconds=120)

_CLIENT_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_CLIENT_OWNER = re.compile(rf"({'|'.join(CLIENT_SURFACES)}):{_CLIENT_ID}")
_LAUNCH_ID = re.compile(r"ln-[0-9a-f]{32}")
_CAUSE_MAX = 200
_OPENINGS_SQL = ", ".join(repr(k) for k in OPENINGS)
# GLOB, not LIKE: LIKE folds ASCII case, so 'COCKPIT:x' would pass it
_CLIENT_GLOB = " OR ".join(f"owner GLOB '{s}:?*'" for s in CLIENT_SURFACES)

# the epoch a row would land on: the launch's latest opening
_LATEST = (f"(SELECT seq FROM turn_lease_events WHERE launch_id = NEW.launch_id AND kind IN ({_OPENINGS_SQL}) "
           "ORDER BY seq DESC LIMIT 1)")
_ENDED = f"EXISTS (SELECT 1 FROM turn_lease_events WHERE token = {_LATEST} AND kind IN ('release', 'lapse'))"
# the furthest until among the latest opening and its beats (fixed-width UTC stamps order as text)
_UNTIL = (f"(SELECT MAX(until) FROM turn_lease_events WHERE seq = {_LATEST} OR (token = {_LATEST} "
          "AND kind IN ('beat', 'outside_beat')))")
_LIVE_AT_NEW = f"({_LATEST} IS NOT NULL AND NOT {_ENDED} AND NEW.at < {_UNTIL})"
_RUNNING_WORKER = ("EXISTS (SELECT 1 FROM launches l WHERE l.launch_id = NEW.launch_id AND l.role = 'worker' AND "
                   "(SELECT kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY seq DESC LIMIT 1) "
                   "= 'active')")

TURN_LEASE_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS turn_lease_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "gseq INTEGER NOT NULL UNIQUE, launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    f"kind TEXT NOT NULL CHECK ({_in('kind', TURN_KINDS)}), token INTEGER, owner TEXT NOT NULL, "
    f"owner_kind TEXT NOT NULL CHECK ({_in('owner_kind', _OWNER_KINDS)}), until TEXT, cause TEXT NOT NULL, "
    "by TEXT NOT NULL CHECK (by IN ('operator', 'service')), at TEXT NOT NULL, "
    # an opening's seq IS its epoch; every other row names the epoch it affects
    f"CHECK ((kind IN ({_OPENINGS_SQL})) = (token IS NULL)), "
    # an opening or a beat moves until on; a release or a lapse ends the epoch and carries none
    "CHECK ((kind IN ('release', 'lapse')) = (until IS NULL)), "
    # a Levain client is <surface>:<client_id>; an outside owner is its kind
    f"CHECK ((owner_kind = 'client' AND ({_CLIENT_GLOB})) OR (owner_kind != 'client' AND owner = owner_kind)), "
    # your clients open and beat their own epochs; the service records outside holdings and lapses; either releases
    "CHECK ((kind IN ('claim', 'handoff', 'take', 'beat') AND by = 'operator' AND owner_kind = 'client') "
    "OR (kind IN ('outside', 'outside_beat') AND by = 'service' AND owner_kind != 'client') "
    "OR (kind = 'lapse' AND by = 'service') OR kind = 'release'))",
    *_append_only("turn_lease_events", "a turn lease event"),
    "CREATE TRIGGER IF NOT EXISTS turn_lease_events_no_replace BEFORE INSERT ON turn_lease_events WHEN EXISTS "
    "(SELECT 1 FROM turn_lease_events WHERE seq = NEW.seq OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a turn lease event is never replaced'); END",
    # the edges (§5.1, §5.3): what the rows themselves can hold, whoever writes them
    "CREATE TRIGGER IF NOT EXISTS turn_lease_events_edges BEFORE INSERT ON turn_lease_events WHEN "
    # a launch that is not a running worker takes no opening and no beat
    f"(NEW.kind IN ({_OPENINGS_SQL}, 'beat', 'outside_beat') AND NOT {_RUNNING_WORKER}) "
    # a claim only while no epoch is live; a handoff only from a live Levain client's epoch
    f"OR (NEW.kind = 'claim' AND {_LIVE_AT_NEW}) "
    f"OR (NEW.kind = 'handoff' AND (NOT {_LIVE_AT_NEW} OR (SELECT owner_kind FROM turn_lease_events "
    f"WHERE seq = {_LATEST}) != 'client')) "
    # every other row names the launch's latest opening, not yet ended, and that epoch's owner
    f"OR (NEW.kind NOT IN ({_OPENINGS_SQL}) AND (NEW.token IS NOT {_LATEST} OR {_ENDED} "
    f"OR (SELECT owner FROM turn_lease_events WHERE seq = NEW.token) IS NOT NEW.owner "
    f"OR (SELECT owner_kind FROM turn_lease_events WHERE seq = NEW.token) IS NOT NEW.owner_kind)) "
    # a beat moves a live epoch on, never revives one: a client's beat a client's epoch, an outside beat an outside one
    f"OR (NEW.kind IN ('beat', 'outside_beat') AND NOT {_LIVE_AT_NEW}) "
    "OR (NEW.kind = 'beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) = 'outside') "
    "OR (NEW.kind = 'outside_beat' AND (SELECT kind FROM turn_lease_events WHERE seq = NEW.token) != 'outside') "
    "BEGIN SELECT RAISE(ABORT, 'a turn lease event takes an edge §5 does not allow'); END",
)


class TurnRefused(CoordError):
    """A turn step the lease does not allow: the right epoch from the wrong owner, an epoch no longer live, a claim
    while another holds the turn. Nothing was written."""


@dataclass(frozen=True)
class TurnLease:
    launch_id: str
    epoch: int            # the opening row's seq
    opened_by: str        # claim, handoff, take or outside
    owner: str            # "<surface>:<client_id>", or terminal / vendor_app
    owner_kind: str       # client, terminal or vendor_app
    until: str            # the furthest until among the opening and its beats
    opened_at: str
    live: bool
    why_not: str | None   # past until, hold cap, launch over; None while live


@dataclass(frozen=True)
class TurnView:
    """What a composer for ``owner`` shows: ``yours`` (it holds the live epoch), ``free`` (no epoch is live; it may
    claim) or ``taken`` (another holder: the composer is read-only and ``holder`` says who)."""
    state: str
    epoch: int | None
    holder: str | None


def check_owner(owner: object) -> str:
    """``owner`` if it names a Levain client (``"<surface>:<client_id>"``, surface one of :data:`CLIENT_SURFACES`)."""
    if not isinstance(owner, str) or _CLIENT_OWNER.fullmatch(owner) is None:
        raise TurnRefused(f"a turn owner is <{'|'.join(CLIENT_SURFACES)}>:<client id> (the authenticated client)")
    return owner


def _cause(cause: object) -> str:
    if not isinstance(cause, str) or not cause or len(cause) > _CAUSE_MAX or "\n" in cause:
        raise TurnRefused(f"a cause is one line of at most {_CAUSE_MAX} characters")
    return cause


def _plus(now: str, delta: timedelta) -> str:
    return utc_stamp(parse_stamp(now) + delta)


def _launch(conn: sqlite3.Connection, launch_id: object) -> tuple[str, str]:
    """``(role, state)`` of a launch, read here (this module imports no launch writer)."""
    if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None:
        raise TurnRefused(f"no launch {str(launch_id)[:40]!r}")
    row = conn.execute("SELECT l.role, (SELECT kind FROM launch_events e WHERE e.launch_id = l.launch_id "
                       "ORDER BY seq DESC LIMIT 1) FROM launches l WHERE l.launch_id = ?", (launch_id,)).fetchone()
    if row is None:
        raise TurnRefused(f"no launch {launch_id}")
    return row[0], row[1]


def _running_worker(conn: sqlite3.Connection, launch_id: str) -> None:
    role, state = _launch(conn, launch_id)
    if role != "worker":
        raise TurnRefused(f"launch {launch_id} is the coordinator's; a turn lease is a worker launch's")
    if state != "active":
        raise TurnRefused(f"launch {launch_id} is {state}; only a running worker's turn can be held")


def _event(conn: sqlite3.Connection, now: str, launch_id: str, kind: str, *, token: int | None, owner: str,
           owner_kind: str, until: str | None, cause: str, by: str) -> int:
    return conn.execute(
        "INSERT INTO turn_lease_events (gseq, launch_id, kind, token, owner, owner_kind, until, cause, by, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (next_gseq(conn, now), launch_id, kind, token, owner, owner_kind, until, cause, by, now)).lastrowid


def _last_send(conn: sqlite3.Connection, launch_id: str, epoch: int) -> str | None:
    """When a send under ``epoch`` last passed its fence: the latest ``chat_messages`` row of the launch carrying the
    epoch whose outbox reached ``pending`` (a refused send passed no fence and keeps no holding alive)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chat_outbox_events'").fetchone() \
            is None:
        return None
    row = conn.execute("SELECT MAX(m.at) FROM chat_messages m WHERE m.launch_id = ? AND m.owner_epoch = ? AND EXISTS "
                       "(SELECT 1 FROM chat_outbox_events o WHERE o.message_id = m.message_id AND o.state = 'pending')",
                       (launch_id, epoch)).fetchone()
    return row[0]


def turn_lease(conn: sqlite3.Connection, now: str, launch_id: str) -> TurnLease | None:
    """The launch's latest epoch that no release or lapse has ended, live or not (``why_not`` says why not); None when
    no epoch stands. Writes nothing."""
    _launch(conn, launch_id)
    row = conn.execute(f"SELECT seq, kind, owner, owner_kind, until, at FROM turn_lease_events WHERE launch_id = ? "
                       f"AND kind IN ({_OPENINGS_SQL}) ORDER BY seq DESC LIMIT 1", (launch_id,)).fetchone()
    if row is None:
        return None
    epoch, kind, owner, owner_kind, until, opened_at = row
    if conn.execute("SELECT 1 FROM turn_lease_events WHERE token = ? AND kind IN ('release', 'lapse')",
                    (epoch,)).fetchone() is not None:
        return None
    beat = conn.execute("SELECT MAX(until) FROM turn_lease_events WHERE token = ? AND kind IN ('beat', 'outside_beat')",
                        (epoch,)).fetchone()[0]
    if beat is not None and beat > until:
        until = beat
    why_not = None
    _role, state = _launch(conn, launch_id)
    if state != "active":
        why_not = "launch over"
    elif now >= until:
        why_not = "past until"
    elif owner_kind == "client":
        last = _last_send(conn, launch_id, epoch)
        since = opened_at if last is None or last < opened_at else last
        if now >= _plus(since, HOLD_CAP):
            why_not = "hold cap"
    return TurnLease(launch_id, epoch, kind, owner, owner_kind, until, opened_at, why_not is None, why_not)


def settle(conn: sqlite3.Connection, now: str, launch_id: str) -> TurnLease | None:
    """The live epoch, or None. An epoch no longer live is ended HERE, in the caller's (committing) transaction: a
    ``release`` (cause ``launch over``) for a launch that is not running, else a ``lapse`` naming why."""
    held = turn_lease(conn, now, launch_id)
    if held is None or held.live:
        return held
    kind = "release" if held.why_not == "launch over" else "lapse"
    _event(conn, now, launch_id, kind, token=held.epoch, owner=held.owner, owner_kind=held.owner_kind, until=None,
           cause=held.why_not, by="service")
    return None


def _holder_text(held: TurnLease, owner: str | None) -> str:
    if held.owner_kind == "terminal":
        return "the terminal has this turn"
    if held.owner_kind == "vendor_app":
        return "the vendor's app has this turn"
    surface = held.owner.split(":", 1)[0]
    if owner is not None and owner.split(":", 1)[0] == surface:
        return f"your other {surface} client has this turn"
    return f"your {surface} has this turn"


def turn_fence(conn: sqlite3.Connection, now: str, launch_id: str, expected_epoch: object,
               authenticated_owner: object) -> TurnLease:
    """Refuse unless ``expected_epoch`` is the launch's live epoch AND its owner is ``authenticated_owner``. Runs inside
    the caller's write transaction, so the check and the row it allows commit together. Writes nothing."""
    owner = check_owner(authenticated_owner)
    if not isinstance(expected_epoch, int) or isinstance(expected_epoch, bool):
        raise TurnRefused("a turn step names the epoch it holds")
    held = turn_lease(conn, now, launch_id)
    if held is None or held.epoch != expected_epoch:
        if held is not None and held.live:
            raise TurnRefused(f"epoch {expected_epoch} is not this turn's ({_holder_text(held, owner)}); nothing was "
                              "written")
        raise TurnRefused(f"epoch {expected_epoch} is not live (the turn is free); claim it again")
    if not held.live:
        raise TurnRefused(f"epoch {expected_epoch} is over ({held.why_not}); claim it again")
    if held.owner != owner:
        raise TurnRefused(f"epoch {expected_epoch} is not yours ({_holder_text(held, owner)}); nothing was written")
    return held


def claim(conn: sqlite3.Connection, now: str, launch_id: str, owner: str, *, cause: str = "claim") -> int:
    """A Levain client claims a free turn; its epoch. Refused while any epoch is live (take it instead)."""
    owner, cause = check_owner(owner), _cause(cause)
    _running_worker(conn, launch_id)
    held = settle(conn, now, launch_id)
    if held is not None:
        raise TurnRefused(f"the turn is held ({_holder_text(held, owner)}); take it to end that holding")
    return _event(conn, now, launch_id, "claim", token=None, owner=owner, owner_kind="client",
                  until=_plus(now, TURN_TTL), cause=cause, by="operator")


def handoff(conn: sqlite3.Connection, now: str, launch_id: str, expected_epoch: object, owner: str, to_owner: str,
            *, cause: str = "handoff") -> int:
    """The holder passes the turn to another of your clients, in one transaction; the new epoch."""
    to_owner, cause = check_owner(to_owner), _cause(cause)
    _running_worker(conn, launch_id)
    held = turn_fence(conn, now, launch_id, expected_epoch, owner)
    if to_owner == held.owner:
        raise TurnRefused("a handoff names another client; this one already holds the turn")
    return _event(conn, now, launch_id, "handoff", token=None, owner=to_owner, owner_kind="client",
                  until=_plus(now, TURN_TTL), cause=cause, by="operator")


def take(conn: sqlite3.Connection, now: str, launch_id: str, owner: str, *, cause: str = "take") -> int:
    """Your explicit "take the turn", from any authenticated surface, at any time: whatever epoch is live (another
    client's, an outside one) ends by this opening. A take never interrupts the running turn. A live epoch ``owner``
    already holds is beaten, not reopened."""
    owner, cause = check_owner(owner), _cause(cause)
    _running_worker(conn, launch_id)
    held = settle(conn, now, launch_id)
    if held is not None and held.owner == owner:
        beat(conn, now, launch_id, held.epoch, owner, cause=cause)
        return held.epoch
    return _event(conn, now, launch_id, "take", token=None, owner=owner, owner_kind="client",
                  until=_plus(now, TURN_TTL), cause=cause, by="operator")


def beat(conn: sqlite3.Connection, now: str, launch_id: str, expected_epoch: object, owner: str, *,
         cause: str = "activity") -> TurnLease:
    """Move the holder's ``until`` to ``now + TURN_TTL``: only on real composer activity, which the caller asserts
    (never on "composer open"). The hold cap still lapses an epoch with no send."""
    cause = _cause(cause)
    _running_worker(conn, launch_id)
    held = turn_fence(conn, now, launch_id, expected_epoch, owner)
    _event(conn, now, launch_id, "beat", token=held.epoch, owner=held.owner, owner_kind="client",
           until=_plus(now, TURN_TTL), cause=cause, by="operator")
    return turn_lease(conn, now, launch_id)


def release(conn: sqlite3.Connection, now: str, launch_id: str, expected_epoch: object, owner: str, *,
            cause: str = "release") -> None:
    """The holder lets the turn go; the turn is free."""
    cause = _cause(cause)
    held = turn_fence(conn, now, launch_id, expected_epoch, owner)
    _event(conn, now, launch_id, "release", token=held.epoch, owner=held.owner, owner_kind=held.owner_kind,
           until=None, cause=cause, by="operator")


def outside(conn: sqlite3.Connection, now: str, launch_id: str, owner_kind: str, *, cause: str) -> int:
    """The service saw a line or an answer from outside Levain (§5.4): an ``outside`` opening owned by ``owner_kind``
    (terminal or vendor_app) until ``now + OUTSIDE_TTL``, which ends any Levain client's epoch; a further outside event
    of the same kind while that epoch is live writes an ``outside_beat``. The epoch."""
    if owner_kind not in OUTSIDE_KINDS:
        raise TurnRefused(f"an outside holder is one of {', '.join(OUTSIDE_KINDS)}")
    cause = _cause(cause)
    _running_worker(conn, launch_id)
    held = settle(conn, now, launch_id)
    if held is not None and held.opened_by == "outside" and held.owner_kind == owner_kind:
        _event(conn, now, launch_id, "outside_beat", token=held.epoch, owner=owner_kind, owner_kind=owner_kind,
               until=_plus(now, OUTSIDE_TTL), cause=cause, by="service")
        return held.epoch
    return _event(conn, now, launch_id, "outside", token=None, owner=owner_kind, owner_kind=owner_kind,
                  until=_plus(now, OUTSIDE_TTL), cause=cause, by="service")


def view(conn: sqlite3.Connection, now: str, launch_id: str, owner: str) -> TurnView:
    """What ``owner``'s composer shows for the launch (§5.4: every Levain composer is read-only while another holds
    the turn, an outside client included). Writes nothing; a caller that commits calls :func:`settle` first."""
    owner = check_owner(owner)
    held = turn_lease(conn, now, launch_id)
    if held is None or not held.live:
        return TurnView("free", None, None)
    if held.owner == owner:
        return TurnView("yours", held.epoch, None)
    return TurnView("taken", held.epoch, _holder_text(held, owner))


def history(conn: sqlite3.Connection, launch_id: str) -> Sequence[tuple]:
    """Every turn lease row of the launch, oldest first: ``(seq, kind, token, owner, owner_kind, until, cause, by,
    at)``."""
    return conn.execute("SELECT seq, kind, token, owner, owner_kind, until, cause, by, at FROM turn_lease_events "
                        "WHERE launch_id = ? ORDER BY seq", (launch_id,)).fetchall()
