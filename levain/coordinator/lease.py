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
whose token the launch is then activated against; otherwise the drop only queues. The launch is the one the COORDINATOR
SETTING names (``coord_settings``, yours only: the agent, its model, the directory it runs in, set by ``levain coord
agent``); with no setting, or agent ``none``, a drop only queues (``unset``): no launch is asked for and none can die. A
coordinator launch that dies before ``active`` (failed, expired) after a process claimed its spawn, or ends at 0 turns,
is a DEATH (one that expired or failed with no spawn claimed, nobody having tried to start it, is none): the next wake
waits out :data:`BACKOFF`, and after :data:`MAX_DEATHS` deaths in a row nothing launches until you look (``levain coord
take`` or ``release``).

``launch_spawns`` is the one-spawner rule (C3b): a process that means to start a launch's session first inserts the
launch's row there, in the transaction that checks the launch is still pending, in time and under the live lease's
token (:func:`claim_spawn`); the primary key is the compare-and-set, so a second process spawns nothing.

[seat] choices C0 leaves open (C3a, 1020+35): the TTLs and backoff below; a beat comes from the holder (its token) or
the service; an operator ``take`` holds the lease for ``LEASE_TTL`` unless told otherwise; your take or release is what
"until you look" reads. (C3b, 1020+50): ``unset`` is checked after a live lease, a running coordinator and a pending
launch (those still say what a drop would do), before the death count; a setting is a whole row, never a field
update. (C3c, 1021+18): :func:`set_agent` takes codex now that a codex launch is built. (C3b fix 1): a thread
or drop write the fence lets through is recorded as a ``beat`` row by coordinator that moves nothing
(:func:`record_write`), so "it wrote as coordinator" is one question over ``lease_events``. Stdlib only.
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
    "ROLES", "SERVICE_BEAT_WINDOW", "SETTING_AGENTS", "Launch", "Lease", "LeaseRefused", "Setting", "activate", "beat",
    "check_model", "claim", "by_harness_session", "claim_spawn", "end_launch", "end_unlisted", "fence", "launch",
    "lease", "launches", "LaunchThread", "mark_swept", "over_unswept", "record_thread", "record_write", "release",
    "request_launch", "service_beat", "set_agent", "setting", "status", "take", "thread_of", "unspawned", "wake",
    "wake_state",
]

LEASE_KINDS = ("claim", "beat", "release", "lapse", "takeover")
LAUNCH_KINDS = ("pending", "active", "failed", "expired", "ended")
HARNESSES = ("claude", "codex")
# a worker refused at its spawn claim or activation because its thread's budget gauge is crossed (C6): a stop, never
# a death
BUDGET_CROSSED = "budget crossed"
# a worker holding several threads, refused when asked for and, if one is already pending, at its spawn claim or
# activation (C6 L3 r1 B1): C0 §3.4 gives such a launch no envelope authority, so no budget can meter it, and nothing
# launches past a crossed budget. A refusal, never a death
SEVERAL_THREADS = "a worker holds one thread"
# a done coordinator the watcher (a drop it never saw waits) or your `levain coord wake` ended so a fresh one starts:
# a stop, never a death, so the wake in the same transaction is never put into backoff by it (1030-42 L3 r1)
REPLACED = "replaced while done"
ROLES = ("coordinator", "worker")
SETTING_AGENTS = (*HARNESSES, "none")
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
# ruled (C3b fix 1, 1020+50): the watcher's service beat goes only to a holder that wrote as coordinator (a lease row
# of its own, or a thread or drop write under its token) this recently; a holder silent longer lapses
SERVICE_BEAT_WINDOW = 2 * LEASE_TTL
# a model name as the harness takes it: never a leading dash (argv would read it as a flag)
_MODEL = re.compile(r"[A-Za-z0-9._\[\]][A-Za-z0-9._\[\]-]*")
# causes the store writes itself; a caller's end_launch never takes one, so no caller can relabel a death
_SERVICE_CAUSES = ("takeover", "operator release", "lease lost before active", "pending past until",
                   "lost the lease", BUDGET_CROSSED, SEVERAL_THREADS, REPLACED)
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
    # the coordinator setting: yours only, a whole row each time (the latest row is the setting); agent none holds no
    # model and no directory, a harness always a directory
    "CREATE TABLE IF NOT EXISTS coord_settings (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, "
    f"agent TEXT NOT NULL CHECK ({_in('agent', SETTING_AGENTS)}), model TEXT, cwd TEXT, "
    "by TEXT NOT NULL CHECK (by = 'operator'), at TEXT NOT NULL, "
    "CHECK ((agent = 'none' AND model IS NULL AND cwd IS NULL) OR (agent != 'none' AND cwd IS NOT NULL)))",
    # pid: the levain process that won the spawn (the session's own process is the harness's, known only after)
    "CREATE TABLE IF NOT EXISTS launch_spawns (launch_id TEXT PRIMARY KEY REFERENCES launches(launch_id), "
    "gseq INTEGER NOT NULL UNIQUE, pid INTEGER NOT NULL, at TEXT NOT NULL)",
    # a Codex start's thread, written by the process that won its spawn BEFORE the start counts as accepted (before
    # its turn starts, and again with the turn's id once it has), with the daemon it runs on: a starter that dies
    # before activation leaves a row that still names what to interrupt, and every later read or interrupt goes to
    # the daemon recorded here, never to whatever CODEX_HOME a later process has
    "CREATE TABLE IF NOT EXISTS launch_threads (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "launch_id TEXT NOT NULL REFERENCES launches(launch_id), gseq INTEGER NOT NULL UNIQUE, thread_id TEXT NOT NULL, "
    "turn_id TEXT, codex_home TEXT NOT NULL, socket TEXT NOT NULL, at TEXT NOT NULL)",
    # a launch that is over whose session the watcher saw not running: it leaves the watcher's over list for good
    "CREATE TABLE IF NOT EXISTS launch_sweeps (launch_id TEXT PRIMARY KEY REFERENCES launches(launch_id), "
    "gseq INTEGER NOT NULL UNIQUE, cause TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("launch_threads", "a launch thread"),
    *_append_only("launch_sweeps", "a launch sweep"),
    "CREATE TRIGGER IF NOT EXISTS launch_threads_no_replace BEFORE INSERT ON launch_threads WHEN EXISTS (SELECT 1 "
    "FROM launch_threads WHERE seq = NEW.seq OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a launch thread is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS launch_sweeps_no_replace BEFORE INSERT ON launch_sweeps WHEN EXISTS (SELECT 1 "
    "FROM launch_sweeps WHERE launch_id = NEW.launch_id OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a launch is swept once'); END",
    *_append_only("coord_settings", "a coordinator setting"),
    *_append_only("launch_spawns", "a launch spawn"),
    "CREATE TRIGGER IF NOT EXISTS coord_settings_no_replace BEFORE INSERT ON coord_settings WHEN EXISTS (SELECT 1 "
    "FROM coord_settings WHERE seq = NEW.seq OR gseq = NEW.gseq) "
    "BEGIN SELECT RAISE(ABORT, 'a coordinator setting is never replaced'); END",
    # INSERT OR REPLACE would let a second spawner take the row: the CAS is a plain INSERT, and a replace is refused
    "CREATE TRIGGER IF NOT EXISTS launch_spawns_no_replace BEFORE INSERT ON launch_spawns WHEN EXISTS (SELECT 1 "
    "FROM launch_spawns WHERE launch_id = NEW.launch_id OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a launch is spawned once'); END",
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
    pinned_envelope_id: str | None = None   # the envelope a one-thread worker was activated under (§3.4)


@dataclass(frozen=True)
class LaunchThread:
    """A Codex launch's thread as its starter recorded it (:func:`record_thread`)."""
    thread_id: str
    turn_id: str | None
    codex_home: str      # the daemon's own codexHome, as it answered initialize
    socket: str          # its control socket


@dataclass(frozen=True)
class Setting:
    agent: str           # claude, codex or none
    model: str | None
    cwd: str | None      # resolved: the directory a coordinator session runs in
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


def record_write(conn: sqlite3.Connection, now: str, held: Lease) -> None:
    """A thread or drop write the fence let through, recorded as the holder's own ``beat`` row (cause ``write``,
    by coordinator) that moves ``until`` nowhere: what "it wrote as coordinator" reads (the service beat, ``0
    turns``, a death) is then one query over ``lease_events``, whatever the write was."""
    if held.holder != "operator":
        _event(conn, now, "beat", held.holder, token=held.token, until=held.until, cause="write", by="coordinator")


def _wrote_since(conn: sqlite3.Connection, session_key: str, *, gseq: int = 0, at: str = "") -> bool:
    """Did ``session_key`` write as coordinator (a claim, beat or release of its own, or a fenced write) after
    ``gseq`` and at or after ``at``?"""
    return conn.execute("SELECT 1 FROM lease_events WHERE holder = ? AND by = 'coordinator' AND gseq > ? AND at >= ?",
                        (session_key, gseq, at)).fetchone() is not None


def service_beat(conn: sqlite3.Connection, now: str, token: object) -> Lease | None:
    """The watcher's beat for a holder whose session is listed and not done: given only while that holder wrote as
    coordinator within :data:`SERVICE_BEAT_WINDOW` (checked here, in the beat's own transaction). None, and nothing
    written, when it did not: a session that is listed but silent is no evidence it is working, so its lease lapses
    at its until and C3a's IDLE_GRACE path ends its launch."""
    held = fence(conn, now, token)
    if held.holder == "operator" or not _wrote_since(conn, held.holder, at=_plus(now, -SERVICE_BEAT_WINDOW)):
        return None
    _event(conn, now, "beat", held.holder, token=held.token, until=_plus(now, LEASE_TTL), cause="beat", by="service")
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
    events = conn.execute("SELECT kind, until, harness_session_id, cause, pinned_envelope_id FROM launch_events "
                          "WHERE launch_id = ? ORDER BY seq", (r[0],)).fetchall()
    until = next((e[1] for e in events if e[0] == "pending"), None)
    hsid, pin = next(((e[2], e[4]) for e in events if e[0] == "active"), (None, None))
    return Launch(r[0], r[1], r[2], r[3], tuple(json.loads(r[4])), r[5], r[6], r[7], r[8], events[-1][0], until,
                  hsid, events[-1][3], r[9], pin)


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
    still the live lease's. A coordinator launch asked for while a lease is held is a rotation. A worker holds
    exactly one thread (:data:`SEVERAL_THREADS`): refused here with nothing written."""
    fence(conn, now, token)
    threads = list(threads)
    if role == "worker" and len(threads) > 1:
        raise _refused(_several(len(threads)))
    return _pending(conn, now, token=token, harness=harness, role=role, threads=threads, model=model, cwd=cwd,
                    cause="requested")


def _several(n: int) -> str:
    return (f"a worker holds one thread, and this launch names {n}: C0 §3.4 gives a launch holding several threads no "
            "envelope authority, so no thread budget could meter it; ask for one worker per thread")


def refuse_several(conn: sqlite3.Connection, now: str, ln: Launch) -> tuple[Launch, str] | None:
    """A pending worker holding several threads (one asked for before :func:`request_launch` refused them): written
    ``failed`` (cause :data:`SEVERAL_THREADS`) in the caller's transaction and ``(launch, why)`` returned; None for
    any other launch. The spawn claim and the activation both call it, so such a launch never starts."""
    if ln.state != "pending" or ln.role != "worker" or len(ln.threads) <= 1:
        return None
    _launch_event(conn, now, ln.launch_id, "failed", SEVERAL_THREADS)
    return _launch_row(conn, "launch_id", ln.launch_id), f"launch {ln.launch_id}: {_several(len(ln.threads))}"


def _head_envelope(conn: sqlite3.Connection, thread_id: str) -> str | None:
    """Thread ``thread_id``'s head envelope (the one most recently bound to it, as
    :meth:`levain.coordinator.envelopes.EnvelopeStore.head` reads it), or None: none bound, or a store with no
    envelope tables."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelope_binds'").fetchone() is None:
        return None
    row = conn.execute("SELECT envelope_id FROM envelope_binds WHERE thread_id = ? ORDER BY gseq DESC LIMIT 1",
                       (thread_id,)).fetchone()
    return None if row is None else row[0]


def activate(conn: sqlite3.Connection, now: str, launch_id: str, *, harness_session_id: str,
             argv_sha256: str | None = None) -> tuple[Launch, str | None]:
    """``pending`` -> ``active`` if the latest event is that pending row, ``now`` is before its ``until`` and its
    token is still the live lease's. Otherwise the launch is written ``expired`` or ``failed`` (in this transaction,
    which commits) and the reason returned: ``(launch, None)`` on activation, ``(launch, reason)`` when refused.

    THE PIN (C0 §3.4): a worker launch whose threads are exactly ``[T]`` records T's head envelope at this moment on
    its ``active`` row, in this transaction; it and every job it files run under that envelope, which later
    successors and attenuations can only narrow. A coordinator launch and a thread with no envelope pin nothing (no
    envelope authority); a pending worker holding several threads is refused here (:func:`refuse_several`). The pin is the head as bound, live or not: whether
    it is still in force is the predicate's question at each use (§4.5), never this one's."""
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
    several = refuse_several(conn, now, ln)
    if several is not None:
        return several
    pin = _head_envelope(conn, ln.threads[0]) if ln.role == "worker" else None
    _launch_event(conn, now, launch_id, "active", "spawned", harness_session_id=hsid,
                  argv_sha256=_line("argv sha256", argv_sha256), pinned_envelope_id=pin)
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


def end_unlisted(conn: sqlite3.Connection, now: str, launch_id: str) -> Launch | None:
    """The watcher saw ``launch_id``'s session missing from its harness's list: an ``active`` launch is written
    ``ended``, cause ``0 turns`` for a coordinator that wrote no lease row as coordinator since its ``active`` (a
    death), else ``gone``. The check and the write are one transaction. None when the launch is no longer active."""
    ln = _must_launch(conn, launch_id)
    if ln.state != "active":
        return None
    cause = "gone"
    if ln.role == "coordinator":
        active_gseq = conn.execute("SELECT gseq FROM launch_events WHERE launch_id = ? AND kind = 'active'",
                                   (launch_id,)).fetchone()[0]
        # a fenced thread or drop write is a lease row too (record_write): a coordinator that split but never beat
        cause = "gone" if _wrote_since(conn, ln.session_key, gseq=active_gseq) else "0 turns"
    return end_launch(conn, now, launch_id, "ended", cause)


def unseen_drop(conn: sqlite3.Connection, ln: Launch) -> str | None:
    """The oldest drop ``ln``'s session never saw, or None: a drop with no ``split`` or ``noted`` receipt (yours or a
    coordinator's) delivered AFTER the launch's spawn claim, in the store-wide order (gseq, never a clock). Its brief
    is written after that claim and quotes every unsplit drop delivered before it, so only a later drop is one it may
    never have read. A fresh launch's claim is newer than the drop that replaced its predecessor, so the same drop
    never replaces it: one replacement per drop, by construction. Uncapped, in the caller's transaction."""
    start = conn.execute(
        "SELECT COALESCE((SELECT gseq FROM launch_spawns WHERE launch_id = ?), "
        "(SELECT gseq FROM launch_events WHERE launch_id = ? AND kind = 'active'))",
        (ln.launch_id, ln.launch_id)).fetchone()[0]
    if start is None:
        return None
    row = conn.execute(
        "SELECT r.ref_id FROM replies r WHERE r.ref_store = 'drops' AND r.kind = 'drop_receipt' "
        "AND json_extract(r.body, '$.stage') = 'delivered' AND r.gseq > ? AND NOT EXISTS (SELECT 1 FROM replies s "
        "WHERE s.ref_store = 'drops' AND s.ref_id = r.ref_id AND s.kind = 'drop_receipt' "
        "AND json_extract(s.body, '$.stage') IN ('split', 'noted')) ORDER BY r.gseq LIMIT 1", (start,)).fetchone()
    return None if row is None else row[0]


def replace_done(conn: sqlite3.Connection, now: str, launch_id: str, *, asked: bool = False) -> Launch | None:
    """A coordinator whose session is ``done`` (a finished turn, listed, not working; the caller read the listing)
    is written ``ended``, cause :data:`REPLACED` (no death), its lease released if it still held one, so the caller's
    :func:`wake` in this transaction asks for a fresh coordinator. The watcher replaces it only while
    :func:`unseen_drop` names a drop; ``asked`` (your ``levain coord wake``) needs none. None, nothing written, when
    the launch is no longer an active coordinator, or no unseen drop waits and you did not ask."""
    ln = launch(conn, launch_id)
    if ln is None or ln.state != "active" or ln.role != "coordinator" or not asked and unseen_drop(conn, ln) is None:
        return None
    _launch_event(conn, now, launch_id, "ended", REPLACED)
    held = lease(conn, now)
    if held is not None and held.holder == ln.session_key:
        _event(conn, now, "release", held.holder, token=held.token, until=None, cause="launch ended", by="service")
    return _launch_row(conn, "launch_id", launch_id)


def unspawned(conn: sqlite3.Connection) -> list[Launch]:
    """Pending launches no process has claimed the spawn of, oldest first."""
    return [ln for ln in _in_state(conn, "pending") if conn.execute(
        "SELECT 1 FROM launch_spawns WHERE launch_id = ?", (ln.launch_id,)).fetchone() is None]


def launch_dirs(conn: sqlite3.Connection) -> list[str]:
    """The distinct directories launches were asked to run in, in the order first asked for."""
    return [r[0] for r in conn.execute(
        "SELECT cwd FROM launches WHERE cwd IS NOT NULL GROUP BY cwd ORDER BY MIN(gseq)").fetchall()]


def by_harness_session(conn: sqlite3.Connection, harness_session_id: str) -> Launch | None:
    """The launch whose ``active`` row recorded ``harness_session_id`` (a session Levain started), or None."""
    row = conn.execute("SELECT launch_id FROM launch_events WHERE kind = 'active' AND harness_session_id = ? "
                       "ORDER BY gseq DESC LIMIT 1", (harness_session_id,)).fetchone()
    return None if row is None else _launch_row(conn, "launch_id", row[0])


def _in_state(conn: sqlite3.Connection, state: str, role: str | None = None) -> list[Launch]:
    """The launches whose latest event is ``state`` (and of ``role``), oldest first: read by state, never by a scan
    of every launch ever written."""
    rows = conn.execute(
        "SELECT l.launch_id FROM launches l WHERE (SELECT e.kind FROM launch_events e WHERE e.launch_id = l.launch_id "
        "ORDER BY e.seq DESC LIMIT 1) = ? AND (? IS NULL OR l.role = ?) ORDER BY l.gseq", (state, role, role))
    return [_launch_row(conn, "launch_id", r[0]) for r in rows.fetchall()]


def record_thread(conn: sqlite3.Connection, now: str, launch_id: str, pid: int, *, thread_id: str,
                  turn_id: str | None, codex_home: str, socket: str) -> str | None:
    """Record the thread (and, once started, the turn) the process ``pid`` started for ``launch_id``, inside the
    caller's ``BEGIN IMMEDIATE``. Fenced as the spawn is: written only while the launch is ``pending``, before its
    ``until``, under the live lease's token, with its spawn claimed by ``pid``, and only for the thread an earlier row
    of this launch named. ``None`` once written, else why (and nothing written: the caller starts no turn)."""
    ln = _must_launch(conn, launch_id)
    for name, value in (("thread id", thread_id), ("codex home", codex_home), ("socket", socket)):
        if _line(name, value) is None:
            raise _refused(f"a launch thread records its {name}")
    _line("turn id", turn_id)
    if ln.state != "pending":
        return f"launch {launch_id} is {ln.state}, not pending"
    row = conn.execute("SELECT pid FROM launch_spawns WHERE launch_id = ?", (launch_id,)).fetchone()
    if row is None or row[0] != pid:
        return f"launch {launch_id}'s spawn is not this process's"
    if now >= ln.until:
        return f"launch {launch_id} was pending past {ln.until}"
    held = _lapse_if_past(conn, now)
    if held is None or held.token != ln.token:
        return f"launch {launch_id} was asked for under lease token {ln.token}, which is no longer live"
    had = thread_of(conn, launch_id)
    if had is not None and (had.thread_id, had.codex_home, had.socket) != (thread_id, codex_home, socket):
        return f"launch {launch_id} already recorded thread {had.thread_id}"
    conn.execute("INSERT INTO launch_threads (launch_id, gseq, thread_id, turn_id, codex_home, socket, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (launch_id, next_gseq(conn, now), thread_id, turn_id, codex_home, socket, now))
    return None


def thread_of(conn: sqlite3.Connection, launch_id: str) -> LaunchThread | None:
    """The latest :func:`record_thread` row of ``launch_id``, or None."""
    row = conn.execute("SELECT thread_id, turn_id, codex_home, socket FROM launch_threads WHERE launch_id = ? "
                       "ORDER BY seq DESC LIMIT 1", (launch_id,)).fetchone()
    return None if row is None else LaunchThread(*row)


def over_unswept(conn: sqlite3.Connection, harness: str, *, limit: int = 20
                 ) -> list[tuple[Launch, LaunchThread | None]]:
    """Up to ``limit`` launches of ``harness`` that are over (ended, failed, expired), recorded a session (an
    ``active`` row, or a thread its starter recorded before activation) and were not yet seen finished
    (:func:`mark_swept`), oldest first, each with its recorded thread: what the watcher checks for a turn still
    running. A launch leaves this list once marked, so the bound never hides an older one behind finished ones."""
    rows = conn.execute(
        "SELECT l.launch_id FROM launches l WHERE l.harness = ? AND (EXISTS (SELECT 1 FROM launch_events a WHERE "
        "a.launch_id = l.launch_id AND a.kind = 'active') OR EXISTS (SELECT 1 FROM launch_threads t WHERE "
        "t.launch_id = l.launch_id)) AND NOT EXISTS (SELECT 1 FROM launch_sweeps w WHERE w.launch_id = l.launch_id) "
        "AND (SELECT e.kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY e.seq DESC LIMIT 1) "
        "IN ('ended', 'failed', 'expired') ORDER BY l.gseq LIMIT ?", (harness, limit))
    return [(_launch_row(conn, "launch_id", r[0]), thread_of(conn, r[0])) for r in rows.fetchall()]


def mark_swept(conn: sqlite3.Connection, now: str, launch_id: str, cause: str) -> bool:
    """Record that ``launch_id``, over, has no session left running (``cause`` says what was seen); False when it is
    not over or was marked before."""
    ln = _must_launch(conn, launch_id)
    if ln.state not in ("ended", "failed", "expired"):
        return False
    if conn.execute("SELECT 1 FROM launch_sweeps WHERE launch_id = ?", (launch_id,)).fetchone() is not None:
        return False
    conn.execute("INSERT INTO launch_sweeps (launch_id, gseq, cause, at) VALUES (?, ?, ?, ?)",
                 (launch_id, next_gseq(conn, now), _line("cause", cause) or "swept", now))
    return True


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


# --- the coordinator setting and the spawn ----------------------------------------------------------------------
def setting(conn: sqlite3.Connection) -> Setting | None:
    """The latest coordinator setting, or None when you never set one."""
    r = conn.execute("SELECT agent, model, cwd, at FROM coord_settings ORDER BY seq DESC LIMIT 1").fetchone()
    return None if r is None else Setting(*r)


def check_model(model: object) -> str | None:
    """``model`` when it is a model name the spawn's argv can carry (letters, digits, ``. _ [ ] -``, never a leading
    dash), None for none; refused otherwise."""
    if model is None:
        return None
    if not isinstance(model, str) or len(model) > 200 or _MODEL.fullmatch(model) is None:
        raise _refused("a model is letters, digits and . _ [ ] - (never starting with -)")
    return model


def set_agent(conn: sqlite3.Connection, now: str, agent: str, *, model: str | None = None,
              cwd: str | None = None) -> Setting:
    """Record your coordinator setting (writer: you only). ``cwd`` is stored as given, which the caller has resolved
    (Claude Code keys a directory's trust on its real path); agent ``none`` takes neither model nor directory."""
    if agent not in SETTING_AGENTS:
        raise _refused(f"the coordinator agent is one of {', '.join(SETTING_AGENTS)}")
    if agent == "none":
        if model is not None or cwd is not None:
            raise _refused("agent none takes no --model and no --cwd")
    elif not isinstance(cwd, str) or not cwd.startswith("/"):
        raise _refused("a coordinator agent runs in a directory: an absolute path")
    conn.execute("INSERT INTO coord_settings (gseq, agent, model, cwd, by, at) VALUES (?, ?, ?, ?, 'operator', ?)",
                 (next_gseq(conn, now), agent, check_model(model), _line("cwd", cwd), now))
    return setting(conn)


def claim_spawn(conn: sqlite3.Connection, now: str, launch_id: str, pid: int) -> tuple[Launch, str | None]:
    """The one-spawner compare-and-set, inside the caller's ``BEGIN IMMEDIATE``: ``(launch, None)`` when this process
    may start the launch's session (its latest event is ``pending``, ``now`` is before its ``until``, its token is
    still the live lease's, and nobody claimed the spawn before), else ``(launch, why)`` and nothing to spawn. A launch
    past its until is written ``expired`` and one whose lease is gone ``failed``, as :func:`activate` would."""
    ln = _must_launch(conn, launch_id)
    if ln.state != "pending":
        return ln, f"launch {launch_id} is {ln.state}, not pending"
    row = conn.execute("SELECT pid, at FROM launch_spawns WHERE launch_id = ?", (launch_id,)).fetchone()
    if row is not None:
        return ln, f"launch {launch_id} is already being started (by pid {row[0]} at {row[1]})"
    if now >= ln.until:
        _launch_event(conn, now, launch_id, "expired", "pending past until")
        return _launch_row(conn, "launch_id", launch_id), f"launch {launch_id} was pending past {ln.until}"
    held = _lapse_if_past(conn, now)
    if held is None or held.token != ln.token:
        _launch_event(conn, now, launch_id, "failed", "lease lost before active")
        return _launch_row(conn, "launch_id", launch_id), (f"launch {launch_id} was asked for under lease token "
                                                           f"{ln.token}, which is no longer live")
    several = refuse_several(conn, now, ln)
    if several is not None:
        return several
    conn.execute("INSERT INTO launch_spawns (launch_id, gseq, pid, at) VALUES (?, ?, ?, ?)",
                 (launch_id, next_gseq(conn, now), pid, now))
    return ln, None


# --- wakes ---------------------------------------------------------------------------------------------------------
def _looked(conn: sqlite3.Connection) -> int:
    """The gseq of your latest take or release: what "until you look" reads."""
    return conn.execute("SELECT MAX(gseq) FROM lease_events WHERE by = 'operator' AND kind IN "
                        "('takeover', 'release')").fetchone()[0] or 0


def launch_death(conn: sqlite3.Connection, ln: Launch) -> bool | None:
    """Did this launch die? ``True`` it died; ``None`` nobody tried to start it, so it says nothing either way (a watcher
    down, a refusal before the spawn claim; ruled L3 r1 G6); ``False`` it did not. The one rule: the wake backoff
    (:func:`_deaths`) and the review queue's dead-seat feed both call it."""
    # a launch asked for under a lease its asker lost is not the launch's fault; one that lost the lease without ever
    # writing as coordinator died idle
    wrote = _wrote_since(conn, ln.session_key)
    died = ln.state in ("failed", "expired") and ln.cause not in ("lease lost before active", BUDGET_CROSSED,
                                                                       SEVERAL_THREADS, REPLACED) \
        or ln.state == "ended" and (ln.cause == "0 turns" or ln.cause == "lost the lease" and not wrote)
    # REPLACED (ended): its session finished a turn and was ended to make room, never a death, written or not
    if not died:
        return False
    if ln.state in ("failed", "expired") and conn.execute(
            "SELECT 1 FROM launch_spawns WHERE launch_id = ?", (ln.launch_id,)).fetchone() is None:
        return None
    return True


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
        death = launch_death(conn, ln)
        if death is False:
            break
        if death is None:
            continue
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
    ``unset`` (no coordinator setting, or agent none: a drop only queues), ``backoff``, ``stopped`` (you look), or
    ``wake``."""
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
                        "and asks for a fresh coordinator; once its session is done, `levain coord wake` replaces "
                        "it, and `levain coord watch` does at its next pass when a drop it never saw waits")
    for ln in _in_state(conn, "pending", "coordinator"):
        if now < ln.until:
            return "launching", f"coordinator launch {ln.launch_id} is pending until {ln.until}"
    chosen = setting(conn)
    if chosen is None or chosen.agent == "none":
        how = "no coordinator agent is set" if chosen is None else "the coordinator agent is none"
        return "unset", f"{how}; drops queue for you (levain coord agent claude sets one)"
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
    chosen = setting(conn)          # wake_state answered wake: a harness is set
    return _pending(conn, now, token=token, harness=chosen.agent, role="coordinator", threads=(), model=chosen.model,
                    cwd=chosen.cwd, cause="wake", session_key=session_key)


def status(conn: sqlite3.Connection, now: str) -> dict[str, object]:
    held = lease(conn, now)
    state, why = wake_state(conn, now)
    return {"lease": None if held is None else {"token": held.token, "holder": held.holder, "until": held.until,
                                                "cause": held.cause, "live": held.live},
            "wake": state, "why": why, "setting": None if (s := setting(conn)) is None else asdict(s),
            "launches": [asdict(ln) for ln in launches(conn, limit=10)]}
