"""levain.coordinator.lease — the coordinator lease and the launch rows (coordinator C3a; C0 §6, §3.4).

The coordinator is a required role, one per operator, held by one session at a time or by you. Who holds it is the
LEASE: an append-only run of ``lease_events`` (claim, beat, release, lapse, takeover). The lease is live while the
latest ``claim`` has no later release, lapse or takeover and the store's clock is before its ``until``; a ``beat``
moves ``until`` on and never changes the TOKEN, which is the claim's ``seq``. Every write the coordinator asks for
carries that token, and the store checks it INSIDE the write's own transaction (:func:`fence`), so a holder past its
``until`` is refused even when nobody has claimed since, and a holder that lost the lease to a takeover is refused at
once. (Fencing tokens are standard; C0 §10 cites the sources.)

The lease is not authority. Before S1 places a control principal any process of your uid can write this file (C0 §2),
so the token keeps two coordinator sessions from both counting; it grants nothing.

A LAUNCH is a session Levain starts (C0 §3.4): ``launches`` holds what was asked for (harness, role, threads, model,
cwd, the token it was asked under, the slot ``session_key`` allocated before any process starts), ``launch_events``
its life (``pending`` -> ``active``, or ``failed`` / ``expired``; ``active`` -> ``ended``). A launch becomes
``active`` only if its latest event is that ``pending`` row, the clock is before its ``until`` and its token is still
the live lease's, all in one transaction (:func:`activate`); a takeover voids every pending launch.

WAKES (:func:`wake`, run inside every new drop's transaction): with no live lease and no pending coordinator launch,
the drop's transaction writes a ``pending`` coordinator launch and a ``bootstrap`` claim held by its ``session_key``,
whose token the launch is then activated against; otherwise the drop only queues. A coordinator launch that dies
before ``active`` (failed, expired) or ends at 0 turns is a DEATH: the next wake waits out :data:`BACKOFF`, and after
:data:`MAX_DEATHS` deaths in a row nothing launches until you look (``levain coord take`` or ``release``).

[seat] choices C0 leaves open (C3a, 1020+35): the TTLs and backoff below; a beat comes from the holder (its token) or
the service; an operator ``take`` holds the lease for ``LEASE_TTL`` unless told otherwise; your take or release is what
"until you look" reads. Stdlib only.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta

from levain import unshowable
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator.store import CoordError, _append_only, next_gseq

__all__ = [
    "BACKOFF", "HARNESSES", "LAUNCH_KINDS", "LAUNCH_TTL", "LEASE_KINDS", "LEASE_SCHEMA", "LEASE_TTL", "MAX_DEATHS",
    "ROLES", "Launch", "Lease", "LeaseRefused", "activate", "beat", "claim", "end_launch", "fence", "launch", "lease",
    "launches", "release", "request_launch", "status", "take", "wake", "wake_state",
]

LEASE_KINDS = ("claim", "beat", "release", "lapse", "takeover")
LAUNCH_KINDS = ("pending", "active", "failed", "expired", "ended")
HARNESSES = ("claude", "codex")
ROLES = ("coordinator", "worker")
_LEASE_BY = ("operator", "coordinator", "service")

# [seat, judged by 1020+35 at C3a against no measured turn lengths] C0 §6 put 10 min and asked for it to be
# reconsidered at C3 against real turns; one long turn should not lose the lease. C3b's run measures it
LEASE_TTL = timedelta(minutes=30)
# [seat] how long a launch may sit pending: a spawn and the id read back take seconds
LAUNCH_TTL = timedelta(minutes=5)
BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
# [seat] how long a coordinator still running without the lease is waited for (to claim again) before a drop ends
# its launch and asks for a fresh one: a crashed session leaves its launch active, and waiting on it unbounded would
# read as health
IDLE_GRACE = LEASE_TTL
# causes the store writes itself; a caller's end_launch never takes one, so no caller can relabel a death
_SERVICE_CAUSES = ("takeover", "operator release", "lease lost before active", "pending past until",
                   "lost the lease")
MAX_DEATHS = 3

_LAUNCH_ID = re.compile(r"ln-[0-9a-f]{32}")
_SESSION_KEY = re.compile(r"s-[0-9a-f]{16}")
_LINE_MAX = 4096


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


LEASE_SCHEMA: tuple[str, ...] = (
    # token: the claim seq a beat, release, lapse or takeover applies to (NULL on a claim, whose token is its seq,
    # and on a takeover when nothing was held)
    "CREATE TABLE IF NOT EXISTS lease_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, "
    f"kind TEXT NOT NULL CHECK ({_in('kind', LEASE_KINDS)}), token INTEGER, holder TEXT NOT NULL, until TEXT, "
    f"cause TEXT NOT NULL, by TEXT NOT NULL CHECK ({_in('by', _LEASE_BY)}), at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS launches (launch_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE, "
    f"session_key TEXT NOT NULL UNIQUE, harness TEXT NOT NULL CHECK ({_in('harness', HARNESSES)}), "
    f"role TEXT NOT NULL CHECK ({_in('role', ROLES)}), threads TEXT NOT NULL, model TEXT, cwd TEXT, "
    "token INTEGER NOT NULL, placement TEXT NOT NULL, at TEXT NOT NULL)",
    # harness_session_id and argv_sha256 are known only once the process is spawned: they ride the active row
    "CREATE TABLE IF NOT EXISTS launch_events (launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    f"seq INTEGER NOT NULL, gseq INTEGER NOT NULL UNIQUE, kind TEXT NOT NULL CHECK ({_in('kind', LAUNCH_KINDS)}), "
    "until TEXT, harness_session_id TEXT, argv_sha256 TEXT, pinned_envelope_id TEXT, cause TEXT NOT NULL, "
    "at TEXT NOT NULL, PRIMARY KEY (launch_id, seq))",
    *_append_only("lease_events", "a lease event"),
    *_append_only("launches", "a launch"),
    *_append_only("launch_events", "a launch event"),
    "CREATE TRIGGER IF NOT EXISTS lease_events_no_replace BEFORE INSERT ON lease_events WHEN EXISTS (SELECT 1 FROM "
    "lease_events WHERE seq = NEW.seq OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a lease event is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS launches_no_replace BEFORE INSERT ON launches WHEN EXISTS (SELECT 1 FROM launches "
    "WHERE launch_id = NEW.launch_id OR session_key = NEW.session_key OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a launch is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS launch_events_next BEFORE INSERT ON launch_events "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM launch_events WHERE launch_id = NEW.launch_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM launch_events WHERE rowid = NEW.rowid OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a launch event is the next seq of its launch'); END",
    # a terminal launch takes no further event; an active one is never active twice
    "CREATE TRIGGER IF NOT EXISTS launch_events_edges BEFORE INSERT ON launch_events WHEN "
    "(SELECT kind FROM launch_events WHERE launch_id = NEW.launch_id ORDER BY seq DESC LIMIT 1) IN "
    "('failed', 'expired', 'ended') "
    "OR (NEW.kind = 'pending' AND EXISTS (SELECT 1 FROM launch_events WHERE launch_id = NEW.launch_id)) "
    "OR (NEW.kind != 'pending' AND NOT EXISTS (SELECT 1 FROM launch_events WHERE launch_id = NEW.launch_id)) "
    "OR (NEW.kind IN ('failed', 'expired') AND (SELECT kind FROM launch_events WHERE launch_id = NEW.launch_id "
    "ORDER BY seq DESC LIMIT 1) IS NOT 'pending') "
    "OR (NEW.kind = 'active' AND (SELECT kind FROM launch_events WHERE launch_id = NEW.launch_id "
    "ORDER BY seq DESC LIMIT 1) IS NOT 'pending') "
    "OR (NEW.kind = 'ended' AND (SELECT kind FROM launch_events WHERE launch_id = NEW.launch_id "
    "ORDER BY seq DESC LIMIT 1) IS NOT 'active') "
    "BEGIN SELECT RAISE(ABORT, 'a launch moves pending -> active -> ended, or ends failed or expired'); END",
)


class LeaseRefused(CoordError):
    """A coordinator write whose token is not the live lease's, or a lease or launch step the rules do not allow."""


def _refused(message: str) -> LeaseRefused:
    return LeaseRefused(message)


@dataclass(frozen=True)
class Lease:
    token: int           # the claim's seq
    holder: str          # a launch's session_key, or "operator"
    until: str
    cause: str
    live: bool           # False: past until with nothing written since (a lapse not yet recorded)


@dataclass(frozen=True)
class Launch:
    launch_id: str
    session_key: str
    harness: str
    role: str
    threads: tuple[str, ...]
    model: str | None
    cwd: str | None
    token: int
    placement: str
    state: str           # the latest event's kind
    until: str | None    # the pending row's until
    harness_session_id: str | None
    cause: str
    at: str


def _plus(now: str, delta: timedelta) -> str:
    return utc_stamp(parse_stamp(now) + delta)


def _event(conn: sqlite3.Connection, now: str, kind: str, holder: str, *, token: int | None, until: str | None,
           cause: str, by: str) -> int:
    return conn.execute(
        "INSERT INTO lease_events (gseq, kind, token, holder, until, cause, by, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (next_gseq(conn, now), kind, token, holder, until, cause, by, now)).lastrowid


# --- reading the lease ---------------------------------------------------------------------------------------------
def lease(conn: sqlite3.Connection, now: str) -> Lease | None:
    """The latest claim that nothing has ended, live or past its ``until``; None when no claim stands."""
    row = conn.execute("SELECT seq, holder, until, cause FROM lease_events WHERE kind = 'claim' "
                       "ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        return None
    seq, holder, until, cause = row
    if conn.execute("SELECT 1 FROM lease_events WHERE seq > ? AND kind IN ('release', 'lapse', 'takeover')",
                    (seq,)).fetchone() is not None:
        return None
    # the furthest until any beat gave, never the latest beat's: a clock stepped back never shortens a lease
    beat = conn.execute("SELECT MAX(until) FROM lease_events WHERE kind = 'beat' AND token = ?", (seq,)).fetchone()
    if beat[0] is not None and beat[0] > until:     # fixed-width UTC stamps order as text
        until = beat[0]
    return Lease(seq, holder, until, cause, now < until)


def _lapse_if_past(conn: sqlite3.Connection, now: str) -> Lease | None:
    """The live lease; a claim past its ``until`` is lapsed here, in the caller's (committing) transaction."""
    held = lease(conn, now)
    if held is not None and not held.live:
        _event(conn, now, "lapse", held.holder, token=held.token, until=None, cause="past until", by="service")
        _expire_pending(conn, now)
        return None
    return held


def fence(conn: sqlite3.Connection, now: str, token: object) -> Lease:
    """Refuse unless ``token`` is the live lease's and its holder may write: you, or a launch that is ``active``.
    Runs inside the caller's write transaction, so the check and the write it allows are one."""
    if not isinstance(token, int) or isinstance(token, bool):
        raise _refused("a coordinator write carries the lease token (levain coord status shows it to its holder)")
    held = lease(conn, now)
    if held is None or held.token != token:
        current = "no coordinator holds the lease" if held is None else f"the lease token is {held.token}"
        raise _refused(f"lease token {token} is not the live lease's ({current}); nothing was written")
    if not held.live:
        raise _refused(f"lease token {token} is past its until ({held.until}); the holder claims again")
    if held.holder != "operator":
        holder = _launch_by_key(conn, held.holder)
        if holder is None or holder.state != "active":
            state = "unknown" if holder is None else holder.state
            raise _refused(f"the lease holder {held.holder} is not an active launch (it is {state})")
    return held


# --- lease steps -------------------------------------------------------------------------------------------------
def claim(conn: sqlite3.Connection, now: str, launch_id: str, session_key: str) -> int:
    """A coordinator session claims the lease (writer 1 of C0 §6): its launch is ``active`` with role coordinator and
    ``session_key`` is that launch's. A live lease held by anyone else refuses it; one it already holds is beaten."""
    ln = _must_launch(conn, launch_id)
    if ln.session_key != session_key:
        raise _refused(f"launch {launch_id} is not session {str(session_key)[:40]}")
    if ln.role != "coordinator" or ln.state != "active":
        raise _refused(f"launch {launch_id} is a {ln.state} {ln.role}; only an active coordinator claims the lease")
    held = _lapse_if_past(conn, now)
    if held is not None and held.holder == session_key:
        beat(conn, now, held.token, by="coordinator")
        return held.token
    holder = None if held is None or held.holder == "operator" else _launch_by_key(conn, held.holder)
    if holder is not None and holder.state != "active":
        # a holder whose launch is not running holds nothing a claimant must wait out (wake reads it the same way)
        _event(conn, now, "release", held.holder, token=held.token, until=None, cause=f"holder {holder.state}",
               by="service")
        held = None
    if held is not None:
        raise _refused(f"the lease is held by {held.holder} until {held.until}; nothing was written")
    return _event(conn, now, "claim", session_key, token=None, until=_plus(now, LEASE_TTL), cause="claim",
                  by="coordinator")


def beat(conn: sqlite3.Connection, now: str, token: object, *, by: str = "coordinator") -> Lease:
    """Move the live lease's ``until`` to ``now + LEASE_TTL``: from its holder (the token) or the service."""
    held = fence(conn, now, token)
    _event(conn, now, "beat", held.holder, token=held.token, until=_plus(now, LEASE_TTL), cause="beat", by=by)
    return lease(conn, now)


def release(conn: sqlite3.Connection, now: str, token: int | None, *, by: str) -> Lease | None:
    """End the lease. The holder releases with its token (handing off: its launch keeps running); you release whatever
    is held (``token`` None), which also voids pending launches, ends every running coordinator launch, and is you
    looking after the deaths that stopped the wakes. Returns the lease that was ended, if one was."""
    if by == "operator":
        if token is not None:
            raise _refused("your release ends whatever is held; it takes no token")
        held = _lapse_if_past(conn, now)
        _event(conn, now, "release", "operator" if held is None else held.holder,
               token=None if held is None else held.token, until=None, cause="operator release", by="operator")
        _void_pending(conn, now, "operator release")
        _end_coordinators(conn, now, "operator release")
        return held
    held = fence(conn, now, token)
    _event(conn, now, "release", held.holder, token=held.token, until=None, cause="holder release", by=by)
    return held


def take(conn: sqlite3.Connection, now: str, *, hold: timedelta = LEASE_TTL) -> int:
    """You take the coordinator role (writer 2): a ``takeover`` ends whatever was held, every pending launch is
    voided, every running coordinator launch is ended (so none claims again when your hold runs out), and you hold
    the lease for ``hold``. Returns your token."""
    if not isinstance(hold, timedelta) or not timedelta(minutes=1) <= hold <= timedelta(days=7):
        raise _refused("a take holds the lease for 1 minute to 7 days")
    held = lease(conn, now)
    _event(conn, now, "takeover", "operator", token=None if held is None else held.token, until=None,
           cause="operator take", by="operator")
    _void_pending(conn, now, "takeover")
    _end_coordinators(conn, now, "takeover")
    return _event(conn, now, "claim", "operator", token=None, until=_plus(now, hold), cause="takeover",
                  by="operator")


# --- launches ------------------------------------------------------------------------------------------------------
def _launch_row(conn: sqlite3.Connection, where: str, arg: object) -> Launch | None:
    r = conn.execute("SELECT launch_id, session_key, harness, role, threads, model, cwd, token, placement, at "
                     f"FROM launches WHERE {where} = ?", (arg,)).fetchone()
    if r is None:
        return None
    events = conn.execute("SELECT kind, until, harness_session_id, cause FROM launch_events WHERE launch_id = ? "
                          "ORDER BY seq", (r[0],)).fetchall()
    until = next((e[1] for e in events if e[0] == "pending"), None)
    hsid = next((e[2] for e in events if e[0] == "active"), None)
    return Launch(r[0], r[1], r[2], r[3], tuple(json.loads(r[4])), r[5], r[6], r[7], r[8], events[-1][0], until,
                  hsid, events[-1][3], r[9])


def _launch_by_key(conn: sqlite3.Connection, session_key: str) -> Launch | None:
    return _launch_row(conn, "session_key", session_key)


def launch(conn: sqlite3.Connection, launch_id: object) -> Launch | None:
    if not isinstance(launch_id, str) or _LAUNCH_ID.fullmatch(launch_id) is None:
        return None
    return _launch_row(conn, "launch_id", launch_id)


def _must_launch(conn: sqlite3.Connection, launch_id: object) -> Launch:
    ln = launch(conn, launch_id)
    if ln is None:
        raise _refused(f"no launch {str(launch_id)[:40]!r}")
    return ln


def launches(conn: sqlite3.Connection, *, limit: int = 20) -> list[Launch]:
    ids = [r[0] for r in conn.execute("SELECT launch_id FROM launches ORDER BY gseq DESC LIMIT ?", (limit,))]
    return [_launch_row(conn, "launch_id", i) for i in ids]


def _launch_event(conn: sqlite3.Connection, now: str, launch_id: str, kind: str, cause: str, *,
                  until: str | None = None, harness_session_id: str | None = None,
                  argv_sha256: str | None = None, pinned_envelope_id: str | None = None) -> None:
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM launch_events WHERE launch_id = ?",
                       (launch_id,)).fetchone()[0]
    conn.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, until, harness_session_id, argv_sha256, "
                 "pinned_envelope_id, cause, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (launch_id, seq, next_gseq(conn, now), kind, until, harness_session_id, argv_sha256,
                  pinned_envelope_id, cause, now))


def _line(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _LINE_MAX \
            or unshowable.first_hidden(value, allow="", selectors=False) is not None:
        raise _refused(f"a launch's {name} is one line of plain text")
    return value


def _pending(conn: sqlite3.Connection, now: str, *, token: int, harness: str, role: str, threads: Sequence[str],
             model: str | None, cwd: str | None, cause: str, session_key: str | None = None) -> Launch:
    if harness not in HARNESSES:
        raise _refused(f"a launch's harness is one of {', '.join(HARNESSES)}")
    if role not in ROLES:
        raise _refused(f"a launch's role is one of {', '.join(ROLES)}")
    threads = list(threads)
    if role == "coordinator" and threads:
        raise _refused("the coordinator's own launch holds no threads (C0 §3.4)")
    if role == "worker" and not threads:
        raise _refused("a worker launch holds at least one thread")
    if any(not isinstance(t, str) or re.fullmatch(r"th-[0-9a-f]{32}", t) is None for t in threads) \
            or len(set(threads)) != len(threads):
        raise _refused("a launch's threads are distinct thread ids")
    for t in threads:
        if conn.execute("SELECT 1 FROM threads WHERE thread_id = ?", (t,)).fetchone() is None:
            raise _refused(f"no thread {t}")
    launch_id = "ln-" + secrets.token_hex(16)
    session_key = session_key or "s-" + secrets.token_hex(8)
    # before S1 places principals a session runs as your uid and the launch grants nothing (C0 §2, §7 L5)
    conn.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, model, cwd, token, "
                 "placement, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'operator-uid', ?)",
                 (launch_id, next_gseq(conn, now), session_key, harness, role, canonical(sorted(threads)),
                  _line("model", model), _line("cwd", cwd), token, now))
    _launch_event(conn, now, launch_id, "pending", cause, until=_plus(now, LAUNCH_TTL))
    return _launch_row(conn, "launch_id", launch_id)


def request_launch(conn: sqlite3.Connection, now: str, token: object, *, harness: str, role: str = "worker",
                   threads: Sequence[str] = (), model: str | None = None, cwd: str | None = None) -> Launch:
    """The lease holder asks for a launch: written ``pending`` under its token, activated only while that token is
    still the live lease's. A coordinator launch asked for while a lease is held is a rotation."""
    fence(conn, now, token)
    return _pending(conn, now, token=token, harness=harness, role=role, threads=threads, model=model, cwd=cwd,
                    cause="requested")


def activate(conn: sqlite3.Connection, now: str, launch_id: str, *, harness_session_id: str,
             argv_sha256: str | None = None, pinned_envelope_id: str | None = None) -> tuple[Launch, str | None]:
    """``pending`` -> ``active`` if the latest event is that pending row, ``now`` is before its ``until`` and its
    token is still the live lease's. Otherwise the launch is written ``expired`` or ``failed`` (in this transaction,
    which commits) and the reason returned: ``(launch, None)`` on activation, ``(launch, reason)`` when refused."""
    ln = _must_launch(conn, launch_id)
    hsid = _line("harness session id", harness_session_id)
    if hsid is None:
        raise _refused("an active launch records the harness's own session id")
    if ln.state != "pending":
        return ln, f"launch {launch_id} is {ln.state}, not pending"
    if now >= ln.until:
        _launch_event(conn, now, launch_id, "expired", "pending past until")
        return _launch_row(conn, "launch_id", launch_id), f"launch {launch_id} was pending past {ln.until}"
    held = _lapse_if_past(conn, now)
    if held is None or held.token != ln.token:
        _launch_event(conn, now, launch_id, "failed", "lease lost before active")
        return _launch_row(conn, "launch_id", launch_id), (f"launch {launch_id} was asked for under lease token "
                                                           f"{ln.token}, which is no longer live")
    _launch_event(conn, now, launch_id, "active", "spawned", harness_session_id=hsid,
                  argv_sha256=_line("argv sha256", argv_sha256), pinned_envelope_id=pinned_envelope_id)
    if ln.role == "coordinator" and held.holder == ln.session_key:
        # the bootstrap claim was written with the pending launch's until: the active session now holds a full TTL
        _event(conn, now, "beat", held.holder, token=held.token, until=_plus(now, LEASE_TTL), cause="active",
               by="service")
    return _launch_row(conn, "launch_id", launch_id), None


def end_launch(conn: sqlite3.Connection, now: str, launch_id: str, kind: str, cause: str) -> Launch:
    """Record a launch's end: ``failed`` or ``expired`` from pending, ``ended`` from active (cause ``0 turns`` for a
    session that died before doing anything). A coordinator that held the lease releases it with its end."""
    if kind not in ("failed", "expired", "ended"):
        raise _refused("a launch ends failed, expired or ended")
    if cause in _SERVICE_CAUSES:
        raise _refused(f"cause {cause!r} is one the store writes itself")
    ln = _must_launch(conn, launch_id)
    if kind == "ended" and ln.state != "active" or kind != "ended" and ln.state != "pending":
        raise _refused(f"launch {launch_id} is {ln.state}; it cannot be {kind}")
    _launch_event(conn, now, launch_id, kind, _line("cause", cause) or kind)
    held = lease(conn, now)
    if held is not None and held.holder == ln.session_key:
        _event(conn, now, "release", held.holder, token=held.token, until=None, cause=f"launch {kind}",
               by="service")
    return _launch_row(conn, "launch_id", launch_id)


def _in_state(conn: sqlite3.Connection, state: str, role: str | None = None) -> list[Launch]:
    """The launches whose latest event is ``state`` (and of ``role``), oldest first: read by state, never by a scan
    of every launch ever written."""
    rows = conn.execute(
        "SELECT l.launch_id FROM launches l WHERE (SELECT e.kind FROM launch_events e WHERE e.launch_id = l.launch_id "
        "ORDER BY e.seq DESC LIMIT 1) = ? AND (? IS NULL OR l.role = ?) ORDER BY l.gseq", (state, role, role))
    return [_launch_row(conn, "launch_id", r[0]) for r in rows.fetchall()]


def _end_coordinators(conn: sqlite3.Connection, now: str, cause: str) -> None:
    for ln in _in_state(conn, "active", "coordinator"):
        _launch_event(conn, now, ln.launch_id, "ended", cause)


def _void_pending(conn: sqlite3.Connection, now: str, cause: str) -> None:
    for ln in _in_state(conn, "pending"):
        _launch_event(conn, now, ln.launch_id, "failed", cause)


def _expire_pending(conn: sqlite3.Connection, now: str) -> None:
    for ln in _in_state(conn, "pending"):
        if now >= ln.until:
            _launch_event(conn, now, ln.launch_id, "expired", "pending past until")


def _idle_until(conn: sqlite3.Connection, ln: Launch) -> str:
    """When a running coordinator without the lease stops being waited for: IDLE_GRACE after the later of its
    activation and the furthest ``until`` its claims and beats reached. Never a row's ``at``: a lapse is written
    whenever someone looks, and a late look must not restart the wait."""
    last = conn.execute("SELECT MAX(t) FROM (SELECT at AS t FROM launch_events WHERE launch_id = ? AND kind = 'active' "
                        "UNION ALL SELECT until AS t FROM lease_events WHERE holder = ? AND kind IN ('claim', 'beat'))",
                        (ln.launch_id, ln.session_key)).fetchone()[0]
    return _plus(last, IDLE_GRACE)


def _idle(conn: sqlite3.Connection, now: str) -> tuple[Launch | None, list[Launch]]:
    """``(the running coordinator still waited for, the ones past IDLE_GRACE)``, with no live lease standing."""
    waited, stale = None, []
    for ln in _in_state(conn, "active", "coordinator"):
        if now < _idle_until(conn, ln):
            waited = ln
        else:
            stale.append(ln)
    return waited, stale


# --- wakes ---------------------------------------------------------------------------------------------------------
def _looked(conn: sqlite3.Connection) -> int:
    """The gseq of your latest take or release: what "until you look" reads."""
    return conn.execute("SELECT MAX(gseq) FROM lease_events WHERE by = 'operator' AND kind IN "
                        "('takeover', 'release')").fetchone()[0] or 0


def _deaths(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Coordinator deaths in a row since you last looked (a take or release of yours) or a coordinator last ran:
    ``(count, at of the latest)``."""
    looked = _looked(conn)
    count, latest = 0, None
    # in the order the launches ENDED (their latest event), not the order they were asked for
    for (launch_id,) in conn.execute(
            "SELECT l.launch_id FROM launches l WHERE l.role = 'coordinator' AND l.gseq > ? ORDER BY "
            "(SELECT MAX(e.gseq) FROM launch_events e WHERE e.launch_id = l.launch_id) DESC", (looked,)).fetchall():
        ln = _launch_row(conn, "launch_id", launch_id)
        # a launch voided by your take or release is older than your look; one asked for under a lease its asker
        # lost is not the launch's fault; one that lost the lease without ever writing as coordinator died idle
        wrote = conn.execute("SELECT 1 FROM lease_events WHERE holder = ? AND by = 'coordinator'",
                             (ln.session_key,)).fetchone() is not None
        died = ln.state in ("failed", "expired") and ln.cause != "lease lost before active" \
            or ln.state == "ended" and (ln.cause == "0 turns" or ln.cause == "lost the lease" and not wrote)
        if not died:
            break
        count += 1
        if latest is None:
            # the death's own time: an expiry happened at its until, whenever it was noticed
            latest = ln.until if ln.state == "expired" else _idle_until(conn, ln) if ln.cause == "lost the lease" \
                else conn.execute(
                "SELECT at FROM launch_events WHERE launch_id = ? ORDER BY seq DESC LIMIT 1", (launch_id,)).fetchone()[0]
    return count, latest


def wake_state(conn: sqlite3.Connection, now: str) -> tuple[str, str]:
    """What a drop arriving now would do, without writing: ``(state, why)``, state one of ``held`` (a live lease),
    ``launching`` (a pending coordinator launch), ``idle`` (a coordinator still running without the lease),
    ``backoff``, ``stopped`` (you look), or ``wake``."""
    held = lease(conn, now)
    if held is not None and held.live:
        holder = None if held.holder == "operator" else _launch_by_key(conn, held.holder)
        if holder is not None and holder.state == "pending":
            # a bootstrap claim: its launch has not started yet
            return "launching", f"coordinator launch {holder.launch_id} is pending until {holder.until}"
        if holder is None or holder.state == "active":
            return "held", f"{held.holder} holds the lease until {held.until} (token {held.token})"
        # a holder whose launch already ended holds nothing: its writes are refused (fence), so drops must not wait
    waited, _stale = _idle(conn, now)
    if waited is not None:
        # its lease lapsed (a long idle, a sleeping laptop) while it kept running: a second coordinator beside it
        # would fight it at claim time, so drops queue for it, until IDLE_GRACE ends the wait
        return "idle", (f"coordinator launch {waited.launch_id} is running without the lease; it claims again "
                        f"(levain coord claim) by {_idle_until(conn, waited)}, or the next drop after that ends it "
                        "and asks for a fresh coordinator")
    for ln in _in_state(conn, "pending", "coordinator"):
        if now < ln.until:
            return "launching", f"coordinator launch {ln.launch_id} is pending until {ln.until}"
    count, latest = _deaths(conn)
    if count >= MAX_DEATHS:
        return "stopped", (f"{count} coordinator launches in a row died; nothing launches until you look "
                           "(levain coord take, or levain coord release)")
    if count and latest is not None:
        ready = _plus(latest, BACKOFF[count - 1])
        if now < ready:
            return "backoff", f"{count} coordinator launch(es) died; the next waits until {ready}"
    return "wake", "no coordinator holds the lease; the next drop asks for one"


def wake(conn: sqlite3.Connection, now: str) -> Launch | None:
    """Inside a new drop's transaction: with no live lease, no pending coordinator launch and no backoff, write a
    ``pending`` coordinator launch and its ``bootstrap`` claim (writer 3) under the CAS "no live lease" this one
    ``BEGIN IMMEDIATE`` holds. Returns the launch written, or None when the drop only queues."""
    _lapse_if_past(conn, now)
    _expire_pending(conn, now)
    if lease(conn, now) is None:
        for ln in _idle(conn, now)[1]:
            _launch_event(conn, now, ln.launch_id, "ended", "lost the lease")
    if wake_state(conn, now)[0] != "wake":
        return None
    # the slot session_key is allocated first, so the claim can name its holder and the launch can carry the
    # claim's seq as the token it is activated against
    session_key = "s-" + secrets.token_hex(8)
    token = _event(conn, now, "claim", session_key, token=None, until=_plus(now, LAUNCH_TTL), cause="bootstrap",
                   by="service")
    return _pending(conn, now, token=token, harness="claude", role="coordinator", threads=(), model=None, cwd=None,
                    cause="wake", session_key=session_key)


def status(conn: sqlite3.Connection, now: str) -> dict[str, object]:
    held = lease(conn, now)
    state, why = wake_state(conn, now)
    return {"lease": None if held is None else {"token": held.token, "holder": held.holder, "until": held.until,
                                                "cause": held.cause, "live": held.live},
            "wake": state, "why": why, "launches": [asdict(ln) for ln in launches(conn, limit=10)]}
