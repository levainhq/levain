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
    deadline = time.monotonic() + BAR_BUSY_SECONDS
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)    # a bounded retry on a busy store, the one wait this step is specified to make


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
    assert bar is not None
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

def _log_row(cockpit: sqlite3.Connection, seq: int) -> tuple[str, str, str] | None:
    row = cockpit.execute("SELECT prev_hash, record, hash FROM authority_log WHERE seq = ?", (seq,)).fetchone()
    return None if row is None else (row[0], row[1], row[2])


def _pending(bar: Bar) -> tuple[str, str, int, str] | None:
    """The bar's four pending fields when all are well formed, else None (a bar with a malformed field is never
    matched against the log and falls to R4)."""
    rec, prev, seq, fp = bar.pending_record, bar.pending_prev, bar.pending_seq, bar.pending_signer_fp
    if type(rec) is not str or type(prev) is not str or type(seq) is not int or seq < 1 or type(fp) is not str:
        return None
    return rec, prev, seq, fp


def _r1(conn, cockpit, state, bar: Bar, *, now) -> Outcome | None:
    """R1: the log already holds the bar's pending link (same prev, record and link hash at ``pending_seq``)."""
    pending = _pending(bar)
    if pending is None or pending[2] > state.seq:
        return None
    rec, prev, seq, _fp = pending
    row = _log_row(cockpit, seq)
    if row is None or row[:2] != (prev, rec) or row[2] != state.hash_at(seq):
        return None
    if close_bar(conn, bar.bar_id, revoke_seq=seq, reason="signed", now=now):
        return Outcome("R1", bar.agent_id, bar.bar_id, f"the revoke is in the log at seq {seq}; bar closed (signed)")
    return Outcome("R1", bar.agent_id, bar.bar_id, f"the revoke is in the log at seq {seq}; the bar was already closed")


def _signature_fresh(bar: Bar, now: datetime) -> bool:
    try:
        signed = parse_stamp(bar.pending_signed_at)     # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - an unreadable time is a stale signature
        return False
    age = now - signed
    return timedelta(0) <= age < SIGNATURE_MAX_AGE


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


def _r4(bar: Bar, attention: Callable[[str, str], None] | None) -> Outcome:
    """R4: any other open bar stays a bar. One attention row per bar: the key is the bar id, so a caller that upserts
    on it writes one row however many ticks pass. It never clears on its own."""
    text = "revoke in force by bar; sign to make it permanent"
    if attention is not None:
        attention(f"agent-bar:{bar.bar_id}", text)
    return Outcome("R4", bar.agent_id, bar.bar_id, text)


def _r5(conn, cockpit, state, *, store_id, now) -> list[Outcome]:
    """R5: a ``revoke_agent`` in the verified log with no bar whose ``revoke_seq`` is its seq gets a closed
    ``from_log`` bar. Correctness never depends on it: the replay already holds the links dead."""
    out: list[Outcome] = []
    rows = cockpit.execute("SELECT seq, record FROM authority_log WHERE seq <= ? AND record LIKE '%revoke_agent%'"
                           " ORDER BY seq", (state.seq,)).fetchall()
    for seq, record in rows:
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
            " SELECT ?, ?, ?, ?, ?, ?, 'from_log' WHERE NOT EXISTS (SELECT 1 FROM agent_bars WHERE revoke_seq = ?)",
            (bar_id, agent_id, stamp, seq, seq, stamp, seq)).rowcount
        if wrote:
            out.append(Outcome("R5", agent_id, bar_id, f"revoke_agent at seq {seq} had no bar; wrote a closed from_log bar"))
    return out


def recover(conn: sqlite3.Connection, authority: AgentAuthority, cockpit: sqlite3.Connection, *, now: datetime,
            anchor: Path | None, store_id: str | None = None, ssh_keygen: Path | str | None = None,
            attention: Callable[[str, str], None] | None = None) -> list[Outcome]:
    """Run the recovery table over every open bar, then R5, and return what each row did.

    ``conn`` is the autonomic store, ``cockpit`` the cockpit journal's writable connection (R2 appends through it),
    ``anchor`` the authority anchor path (``None``: an unanchored store, as :func:`levain.authority.append` states),
    ``store_id`` the authority log's store id (default: the replay's). ``attention(key, text)`` is called once per
    tick for each R4 bar and must upsert on ``key``. A replay or anchor failure, or a cockpit that cannot be read,
    raises nothing and changes nothing: the one outcome is an ``R0`` row saying why."""
    try:
        state = authority.replay()
    except Exception as exc:  # noqa: BLE001 - whatever stops a verified replay leaves every bar as it is
        _log.warning("agent bars: recovery refused (%s): %s", type(exc).__name__, exc)
        return [Outcome("R0", "", "", f"recovery refused, every bar left as it is: {type(exc).__name__}")]
    sid = state.store_id if store_id is None else store_id
    outcomes: list[Outcome] = []
    try:
        open_bars = [_bar(r) for r in conn.execute(f"{_SELECT} WHERE closed_at IS NULL ORDER BY barred_at, bar_id")]
        for bar in open_bars:
            outcome = (_r1(conn, cockpit, state, bar, now=now)
                       or _r2(conn, cockpit, state, bar, now=now, anchor=anchor, ssh_keygen=ssh_keygen)
                       or _r3(conn, cockpit, state, bar, now=now, store_id=sid)
                       or _r4(bar, attention))
            outcomes.append(outcome)
        outcomes.extend(_r5(conn, cockpit, state, store_id=sid, now=now))
    except sqlite3.Error as exc:
        _log.warning("agent bars: recovery stopped (%s): %s", type(exc).__name__, exc)
        outcomes.append(Outcome("R0", "", "", f"recovery stopped on a store error: {type(exc).__name__}"))
    return outcomes
