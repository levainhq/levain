"""levain.agents.bars — the agent revoke bar and its recovery table (A0 section 2.2, rows R1 to R5; BUILD_PLAN P5c-2a).

A bar is the revoke's intent, a row of ``agent_bars`` in the autonomic store. :func:`open_bar` writes it and raises the
epoch of every live occurrence of the agent in one transaction, so nothing of the agent is admitted or launched and the
next journal effect of a running fire is refused. A bar only narrows and needs no signature.

:func:`recover` is the recovery table. Each row has one function (``_r1`` to ``_r5``) and the table runs in order, so a
bar takes the first row that fits it. Every row starts from a verified, anchored replay of the authority log
(:meth:`levain.agent_authority.AgentAuthority.replay`, which raises); a replay that fails, or an anchor that refuses,
leaves every bar as it is, because "nothing could be read" is never taken for "nothing is live".

Every write to a bar is ``... WHERE bar_id = ? AND closed_at IS NULL``. A closed bar is frozen by a trigger, so an
unguarded write would abort after the revoke it reports had already landed; a guarded write that touches no row means
the bar was closed meanwhile (an abandon), and the caller reports the true outcome instead of raising.

This module reaches the store by plain SQL on a connection the caller opened and owns (autocommit, no transaction
open); the autonomic leaf cannot import from here.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from levain import authority as _auth
from levain.agent_authority import AgentAuthority
from levain.cockpit.consent import parse_stamp, utc_stamp

__all__ = ["BAR_BUSY_SECONDS", "SIGNATURE_MAX_AGE", "Bar", "Outcome", "open_bar", "open_bar_of", "read_bar",
           "record_prepared", "record_signature", "close_bar", "recover"]

_log = logging.getLogger("levain.agents.bars")

# Step 1 retries a busy store this long: durable_sqlite sets no busy timeout of its own.
BAR_BUSY_SECONDS = 2.0
# R2: a signature recorded longer ago than this goes to R4, and the stored signature is not used.
SIGNATURE_MAX_AGE = timedelta(hours=24)
# the autonomic store id's form, as the authority log validates an agent link's store_id
_STORE_ID = re.compile(r"store-[0-9a-f]{32}")

_COLUMNS = ("bar_id", "agent_id", "barred_at", "head_seq_at_bar", "revoke_seq", "closed_at", "reason",
            "pending_record", "pending_prev", "pending_seq", "pending_signer_fp", "pending_signature",
            "pending_signed_at", "attempts", "last_error")
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM agent_bars"


@dataclass(frozen=True)
class Bar:
    bar_id: str
    agent_id: str
    barred_at: str
    head_seq_at_bar: int
    revoke_seq: int | None
    closed_at: str | None
    reason: str | None
    pending_record: str | None
    pending_prev: str | None
    pending_seq: int | None
    pending_signer_fp: str | None
    pending_signature: str | None
    pending_signed_at: str | None
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class Outcome:
    """What recovery did to one bar (or, for R5, wrote): ``row`` is ``R1`` to ``R5``, ``text`` says it plainly."""
    row: str
    agent_id: str
    bar_id: str
    text: str


def _bar(row: tuple) -> Bar:
    return Bar(*row)


def read_bar(conn: sqlite3.Connection, bar_id: str) -> Bar | None:
    row = conn.execute(f"{_SELECT} WHERE bar_id = ?", (bar_id,)).fetchone()
    return None if row is None else _bar(row)


def open_bar_of(conn: sqlite3.Connection, agent_id: str) -> Bar | None:
    """The agent's open bar, if any (the partial unique index allows at most one)."""
    row = conn.execute(f"{_SELECT} WHERE agent_id = ? AND closed_at IS NULL", (agent_id,)).fetchone()
    return None if row is None else _bar(row)


def _begin_immediate(conn: sqlite3.Connection) -> None:
    # the connection's own busy timeout (sqlite3's default is 5 s) would wait inside each attempt past the deadline,
    # so every attempt runs with the time left and the caller's setting comes back after
    deadline = time.monotonic() + BAR_BUSY_SECONDS
    saved = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    try:
        while True:
            left_ms = max(0, int((deadline - time.monotonic()) * 1000))
            conn.execute(f"PRAGMA busy_timeout = {left_ms}")
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                # a bounded retry on a busy store, the one wait this step is specified to make; the pause never
                # runs past the deadline
                time.sleep(min(0.02, remaining))
    finally:
        conn.execute(f"PRAGMA busy_timeout = {saved}")


def open_bar(conn: sqlite3.Connection, agent_id: str, *, head_seq: int, now: datetime) -> Bar:
    """Step 1. One ``BEGIN IMMEDIATE``: insert the open bar (a second revoke reuses the open one) and raise the epoch
    of every admitted or launched occurrence of the agent. Returns the open bar."""
    if conn.in_transaction:
        raise ValueError("open_bar owns its transaction; the connection has one open")
    _begin_immediate(conn)
    try:
        existing = open_bar_of(conn, agent_id)
        if existing is None:
            conn.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES (?, ?, ?, ?)",
                         (f"bar-{secrets.token_hex(8)}", agent_id, utc_stamp(now), head_seq))
        conn.execute("UPDATE agent_occurrences SET epoch = epoch + 1 WHERE agent_id = ?"
                     " AND state IN ('admitted', 'launched')", (agent_id,))
        bar = open_bar_of(conn, agent_id)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    if bar is None:
        raise RuntimeError(f"open_bar committed and then read no open bar of {agent_id}")
    return bar


def _guarded(conn: sqlite3.Connection, set_sql: str, params: tuple, bar_id: str) -> bool:
    """One bar write, guarded by ``closed_at IS NULL``; False when it touched no row (the bar is closed)."""
    return conn.execute(f"UPDATE agent_bars SET {set_sql} WHERE bar_id = ? AND closed_at IS NULL",
                        (*params, bar_id)).rowcount == 1


def record_prepared(conn: sqlite3.Connection, bar_id: str, link: _auth.UnsignedLink) -> bool:
    """Step 2: the four ``pending_*`` fields of an unsigned link, with any stored signature and its time nulled
    together (a stale signature never pairs with a new record) and ``attempts`` up by one. False: the bar is closed."""
    return _guarded(conn, "pending_record = ?, pending_prev = ?, pending_seq = ?, pending_signer_fp = ?,"
                          " pending_signature = NULL, pending_signed_at = NULL, attempts = attempts + 1,"
                          " last_error = NULL",
                    (link.record, link.prev_hash, link.seq, link.signer_fp), bar_id)


def record_signature(conn: sqlite3.Connection, bar_id: str, signature: str, *, now: datetime) -> bool:
    """Step 3: the signature and the time it was recorded, in one UPDATE. False: the bar is closed."""
    return _guarded(conn, "pending_signature = ?, pending_signed_at = ?", (signature, utc_stamp(now)), bar_id)


def close_bar(conn: sqlite3.Connection, bar_id: str, *, revoke_seq: int | None, reason: str, now: datetime) -> bool:
    """Close an open bar. False: it was closed first (an abandon), and nothing was changed."""
    return _guarded(conn, "revoke_seq = ?, closed_at = ?, reason = ?", (revoke_seq, utc_stamp(now), reason), bar_id)


# --- the recovery table -------------------------------------------------------------------------------------------

def _verified(state, seq: int, prev: object, record: object) -> bool:
    """Whether ``(prev, record)`` is the replay's verified link at ``seq``: the replay holds each link's hash, so a
    row read from the table after the replay is trusted only when it hashes to that (a row rewritten since is not)."""
    return type(prev) is str and type(record) is str and state.hash_at(seq) == _auth._link_hash(prev, record)


def _pending(bar: Bar) -> tuple[str, str, int, str] | None:
    """The bar's four pending fields when all are well formed, else None (a bar with a malformed field is never
    matched against the log and falls to R4)."""
    rec, prev, seq, fp = bar.pending_record, bar.pending_prev, bar.pending_seq, bar.pending_signer_fp
    if type(rec) is not str or type(prev) is not str or type(seq) is not int or seq < 1 or type(fp) is not str:
        return None
    return rec, prev, seq, fp


def _r1(conn, state, bar: Bar, *, now) -> Outcome | None:
    """R1: the log already holds the bar's pending link: its prev and record hash to the replay's link at
    ``pending_seq``, so the match is judged on the verified replay alone and no table row is read."""
    pending = _pending(bar)
    if pending is None or pending[2] > state.seq:
        return None
    rec, prev, seq, _fp = pending
    if not _verified(state, seq, prev, rec):
        return None
    if close_bar(conn, bar.bar_id, revoke_seq=seq, reason="signed", now=now):
        return Outcome("R1", bar.agent_id, bar.bar_id, f"the revoke is in the log at seq {seq}; bar closed (signed)")
    return Outcome("R1", bar.agent_id, bar.bar_id, f"the revoke is in the log at seq {seq}; the bar was already closed")


def _signature_fresh(bar: Bar, now: datetime) -> bool:
    try:
        signed = parse_stamp(bar.pending_signed_at)     # type: ignore[arg-type]
        age = now - signed      # a naive now against the aware stamp raises here, and is a stale signature
        return isinstance(age, timedelta) and timedelta(0) <= age < SIGNATURE_MAX_AGE
    except Exception:  # noqa: BLE001 - an unreadable time is a stale signature
        return False


def _r2(conn, cockpit, state, bar: Bar, *, now, anchor, ssh_keygen) -> Outcome | None:
    """R2: a fresh stored signature and the head still where the link was prepared: append it, no new gesture."""
    pending = _pending(bar)
    if pending is None or bar.pending_signature is None or not _signature_fresh(bar, now):
        return None
    rec, prev, seq, fp = pending
    if (state.seq, state.head_hash) != (seq - 1, prev):
        return None
    link = _auth.UnsignedLink(seq, prev, rec, fp)
    kw = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        _auth.append(cockpit, link, bar.pending_signature, now=now, anchor=anchor, **kw)
    except _auth.AuthorityError as exc:
        # StaleHead or a refused link: the bar stays open and falls to R4; the reason is recorded on the bar
        _guarded(conn, "last_error = ?", (("stale_head" if isinstance(exc, _auth.StaleHead)
                                           else f"append_refused:{type(exc).__name__}"),), bar.bar_id)
        return None
    if close_bar(conn, bar.bar_id, revoke_seq=seq, reason="signed", now=now):
        return Outcome("R2", bar.agent_id, bar.bar_id, f"appended the stored signature at seq {seq}; bar closed (signed)")
    return Outcome("R2", bar.agent_id, bar.bar_id,
                   f"revoke landed at seq {seq}; the bar had been abandoned; it is recorded from_log at the next tick")


def _r3(conn, cockpit, state, bar: Bar, *, now, store_id) -> Outcome | None:
    """R3: the replay holds no agent-link of the agent at all (presence, not liveness: a lapsed link or one whose
    signer was revoked is still present, and a revoke_agent would remove it)."""
    if _auth.agent_links(state, bar.agent_id, store_id):
        return None
    if close_bar(conn, bar.bar_id, revoke_seq=state.seq, reason="nothing_present", now=now):
        return Outcome("R3", bar.agent_id, bar.bar_id,
                       f"no agent-link of the agent is in the log; bar closed at head seq {state.seq} (nothing_present)")
    return Outcome("R3", bar.agent_id, bar.bar_id, "no agent-link in the log; the bar was already closed")


def _r4(conn, bar: Bar, attention: Callable[[str, str], None] | None) -> Outcome:
    """R4: any other open bar stays a bar. One attention row per bar: the key is the bar id, so a caller that upserts
    on it writes one row however many ticks pass. It never clears on its own, so a bar closed since the tick read it
    (an abandon) gets none."""
    current = read_bar(conn, bar.bar_id)
    if current is None or current.closed_at is not None:
        return Outcome("R4", bar.agent_id, bar.bar_id, "the bar was closed meanwhile; no attention row")
    text = "revoke in force by bar; sign to make it permanent"
    if attention is not None:
        attention(f"agent-bar:{bar.bar_id}", text)
    return Outcome("R4", bar.agent_id, bar.bar_id, text)


def _r5(conn, cockpit, state, *, store_id, now) -> list[Outcome]:
    """R5: a ``revoke_agent`` in the verified log with no bar of that agent whose ``revoke_seq`` is its seq gets a
    closed ``from_log`` bar (the agent is part of the match: R3 closes at the head, which may be another agent's
    revoke). A row is used only when it hashes to the replay's link at its seq. Correctness never depends on it: the
    replay already holds the links dead."""
    out: list[Outcome] = []
    rows = cockpit.execute("SELECT seq, prev_hash, record FROM authority_log WHERE seq <= ?"
                           " AND record LIKE '%revoke_agent%' ORDER BY seq", (state.seq,)).fetchall()
    for seq, prev, record in rows:
        if type(seq) is not int or not _verified(state, seq, prev, record):
            _log.warning("agent bars: authority_log row %r is not the verified replay's link; R5 skips it", seq)
            out.append(Outcome("R0", "", "", f"log row {seq!r} differs from the verified replay; nothing recorded"))
            continue
        try:
            rec = json.loads(record)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("op") != "revoke_agent" or rec.get("agent_store_id") != store_id:
            continue
        agent_id = rec.get("agent_id")
        if type(agent_id) is not str:
            continue
        bar_id = f"bar-log-{seq}"
        stamp = utc_stamp(now)
        wrote = conn.execute(
            "INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar, revoke_seq, closed_at, reason)"
            " SELECT ?, ?, ?, ?, ?, ?, 'from_log'"
            " WHERE NOT EXISTS (SELECT 1 FROM agent_bars WHERE agent_id = ? AND revoke_seq = ?)",
            (bar_id, agent_id, stamp, seq, seq, stamp, agent_id, seq)).rowcount
        if wrote:
            out.append(Outcome("R5", agent_id, bar_id, f"revoke_agent at seq {seq} had no bar; wrote a closed from_log bar"))
    return out


def _store_id(conn: sqlite3.Connection, given: str | None) -> tuple[str | None, str]:
    """The autonomic store's id agent links name, or ``(None, why)``. The default is the store's own ``meta`` row
    (the authority log's store id is the cockpit's, never this one); a given id must agree with that row."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        own = None if row is None else row[0]
    except sqlite3.OperationalError as exc:
        if given is None or "no such table" not in str(exc):
            return None, f"the store's id cannot be read: {type(exc).__name__}"
        own = None      # a bare store with no meta table: the caller's id is the only one there is
    sid = own if given is None else given
    if type(sid) is not str or not _STORE_ID.fullmatch(sid):
        return None, "no well-formed autonomic store id"
    if own is not None and own != sid:
        return None, "the given store id is not this store's"
    return sid, ""


def recover(conn: sqlite3.Connection, authority: AgentAuthority, cockpit: sqlite3.Connection, *, now: datetime,
            anchor: Path | None, store_id: str | None = None, ssh_keygen: Path | str | None = None,
            attention: Callable[[str, str], None] | None = None) -> list[Outcome]:
    """Run the recovery table over every open bar, then R5, and return what each row did.

    ``conn`` is the autonomic store, ``cockpit`` the cockpit journal's writable connection (R2 appends through it),
    ``anchor`` the authority anchor path (``None``: an unanchored store, as :func:`levain.authority.append` states),
    ``store_id`` the autonomic store's id, the one agent links name (default: this store's ``meta`` row; a given one
    must agree with it). ``attention(key, text)`` is called once per tick for each R4 bar and must upsert on ``key``.
    A replay or anchor failure, a store id that cannot be established, or a cockpit that cannot be read, raises
    nothing and changes nothing: the one outcome is an ``R0`` row saying why. A bar whose rows raise is left open
    with an ``R0`` row of its own, and the table goes on to the next bar."""
    try:
        state = authority.replay()
    except Exception as exc:  # noqa: BLE001 - whatever stops a verified replay leaves every bar as it is
        _log.warning("agent bars: recovery refused (%s): %s", type(exc).__name__, exc)
        return [Outcome("R0", "", "", f"recovery refused, every bar left as it is: {type(exc).__name__}")]
    try:
        sid, why = _store_id(conn, store_id)
    except sqlite3.Error as exc:
        sid, why = None, f"the store's id cannot be read: {type(exc).__name__}"
    if sid is None:
        _log.warning("agent bars: recovery refused: %s", why)
        return [Outcome("R0", "", "", f"recovery refused, every bar left as it is: {why}")]
    outcomes: list[Outcome] = []
    try:
        open_bars = [_bar(r) for r in conn.execute(f"{_SELECT} WHERE closed_at IS NULL ORDER BY barred_at, bar_id")]
        for bar in open_bars:
            try:
                outcome = (_r1(conn, state, bar, now=now)
                           or _r2(conn, cockpit, state, bar, now=now, anchor=anchor, ssh_keygen=ssh_keygen)
                           or _r3(conn, cockpit, state, bar, now=now, store_id=sid)
                           or _r4(conn, bar, attention))
            except Exception as exc:  # noqa: BLE001 - one bar that cannot be resolved never stops the others
                _log.warning("agent bars: bar %s left open (%s): %s", bar.bar_id, type(exc).__name__, exc)
                outcome = Outcome("R0", bar.agent_id, bar.bar_id, f"the bar is left open: {type(exc).__name__}")
            outcomes.append(outcome)
        outcomes.extend(_r5(conn, cockpit, state, store_id=sid, now=now))
    except Exception as exc:  # noqa: BLE001 - a store or cockpit that cannot be read leaves what is left as it is
        _log.warning("agent bars: recovery stopped (%s): %s", type(exc).__name__, exc)
        outcomes.append(Outcome("R0", "", "", f"recovery stopped: {type(exc).__name__}"))
    return outcomes
