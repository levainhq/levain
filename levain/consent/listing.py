"""levain.consent.listing — a READ-ONLY view of the consent queue, for the cockpit page.

The page shows what agents are waiting on; it never decides anything. This reader opens
``<queue dir>/queue.db`` with ``mode=ro`` and goes around :class:`~levain.consent.queue.ConsentQueue` on
purpose: the queue's constructor path creates the directory and the schema, and a sweep writes. Here a
missing directory or file is "no consent queue here" (an empty listing), nothing is created, nothing is
swept, nothing is written.

The rows are an index, never authority (``queue.py``'s own docstring): a request whose seal does not match,
or whose timestamps do not parse, is skipped and counted, and a pending request past its expiry that nobody has swept reads as ``expired``
here, for display only. What a request means (``display``) is already ``redact_display``'s output.

This module also holds the two readers the terminal and the page share, so both show the same thing:
:func:`filed_input` and :func:`consent_targets`, moved here from the CLI.

Stdlib only (plus the record and the stamp helpers).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.consent.record import ExternalGateRequest

__all__ = ["ConsentEntry", "ConsentListing", "HISTORY", "KeyEvidence", "PENDING_CAP", "RECENT", "RECENT_CAP",
           "UNSHOWN", "audit_line", "consent_targets", "entry_body", "filed_input", "find_entry", "read_evidence",
           "read_listing"]

RECENT = timedelta(hours=24)        # how long a terminal request stays in the listing
RECENT_CAP = 50
HISTORY = timedelta(days=14)        # how far back the standing-rule proposals read decided requests
PENDING_CAP = 200                   # pending rows read per listing; past it, flagged, never drawn
UNSHOWN = object()
TOO_LONG = "too long to answer from the terminal: answer in the agent's own dialog"

_REQUEST_FIELDS = ("id", "agent", "agent_session_id", "event_id", "tool_name", "tool_input_digest", "display",
                   "cwd", "tier", "received_at", "expires_at")
_ACCESS_BY_OTHERS = 0o077


def filed_input(request: object) -> object:
    """The tool input as filed, parsed back from the request's sealed display (``<tool_name> <json>``, secret-
    named keys masked); :data:`UNSHOWN` when the display is not that whole: a display cut at its limit never
    parses (the cut leaves the JSON unterminated, or trailed by the ellipsis)."""
    prefix = f"{request.tool_name} "
    if not request.display.startswith(prefix):
        return UNSHOWN
    try:
        return json.loads(request.display[len(prefix):])
    except (ValueError, RecursionError):            # a display nested too deep to read back is not shown either
        return UNSHOWN


def consent_targets(request: object) -> str:
    """The paths a grant's criteria would read from the request, read back from its sealed display. A display
    only: nothing is decided from it."""
    import levain.consent.adapters.codex  # noqa: F401 - registers Codex's apply_patch target reader
    from levain.consent.policy import _request_targets

    tool_input = filed_input(request)
    if tool_input is UNSHOWN:
        return "(too long to answer here: the input as filed is cut at its display limit)"
    got = _request_targets(request.agent, request.tool_name, tool_input)
    if not got:
        return "(none named: the working directory)"
    # a path named by two readers (a file tool's file_path is also a path field) is shown once
    return ", ".join(dict.fromkeys("(unreadable)" if t is None else str(t) for t in got))


@dataclass(frozen=True)
class ConsentEntry:
    """One request as the page shows it. ``state`` is the DISPLAY state (a pending past its expiry reads
    ``expired``); ``decided_at`` is the decision's time, or the expiry for one that expired unswept. ``dialog``
    is the UNSIGNED outcome recorded for a request levain left to the agent's own dialog (``ran``,
    ``ran_unasked``, ``did_not_run``; :data:`levain.consent.queue.DIALOG_OUTCOMES`), ``None`` for none yet.
    ``gap`` is the gap number of a request that went undecided (undecided, or ruled late), else ``None``; for an
    undecided one ``decided_at`` is when it went undecided."""
    request: ExternalGateRequest
    state: str
    via: str | None
    signer_fp: str | None
    grant_id: str | None
    decided_at: str | None
    dialog: str | None = None
    gap: int | None = None


@dataclass(frozen=True)
class ConsentListing:
    """``pending`` oldest first; ``undecided`` every undecided request, however old (it waits on a ruling, so the
    decided-row horizon never drops it), oldest first; ``recent`` newest first, at most :data:`RECENT_CAP`;
    ``skipped`` the rows that do not check out (a seal that does not match, a stamp that does not parse);
    ``overflow`` whether more than :data:`PENDING_CAP` requests are pending, or more than that undecided (the rest
    are not read, and not counted)."""
    pending: tuple[ConsentEntry, ...] = ()
    recent: tuple[ConsentEntry, ...] = ()
    skipped: int = 0
    overflow: bool = False
    undecided: tuple[ConsentEntry, ...] = ()


def audit_line(entry: ConsentEntry) -> str:
    """What happened to a terminal (or undecided) request, in a sentence."""
    gap = f"#{entry.gap}" if entry.gap is not None else "(unnumbered)"
    if entry.state in ("allowed", "denied") and entry.via == "ruling":
        return f"{entry.state} by a late ruling on gap {gap}, key {entry.signer_fp or '(unknown)'}"
    if entry.state == "allowed":
        if entry.via == "grant":
            return f"allowed by standing grant {entry.grant_id or '(unnamed)'}"
        return f"allowed by signed answer, key {entry.signer_fp or '(unknown)'}"
    if entry.state == "denied":
        return f"denied by signed answer, key {entry.signer_fp or '(unknown)'}"
    if entry.state == "undecided":
        return f"undecided: the hold ended with no answer and the call did not run; gap {gap} waits on a ruling"
    from levain.consent.dialog import LABELS

    if entry.state == "expired":
        return f"expired: no answer, the agent's own dialog stood; {LABELS.get(entry.dialog, LABELS[None])}"
    if entry.state == "withdrawn":
        return f"withdrawn to the agent's own dialog; {LABELS.get(entry.dialog, LABELS[None])}"
    return entry.state


def find_entry(listing: ConsentListing, request_id: str) -> ConsentEntry | None:
    """The listed request with this id, or None (unknown, past the listing's day, or skipped)."""
    for e in (*listing.pending, *listing.undecided, *listing.recent):
        if e.request.id == request_id:
            return e
    return None


def entry_body(entry: ConsentEntry) -> dict[str, Any]:
    """What the page shows of one request beyond its panel row: the request as filed, where, what it names, how
    to answer it from a terminal, and, once decided, how it was. Served only to a caller holding a surface token
    (``web_server``'s consent route), never in the token-free panel. A request whose display is cut at its limit
    offers no commands: ``levain consent answer`` refuses to sign what it cannot show."""
    r = entry.request
    pending = entry.state == "pending"
    shown = filed_input(r)
    answerable = pending and shown is not UNSHOWN
    from levain.cockpit.hintview import hints_for, visible     # rendering only: what a shell command looks like (Q16)

    try:
        hints = [visible(h) for h in hints_for(r, shown, shown is UNSHOWN)]
    except Exception:  # noqa: BLE001 - a hint is a reading aid: it never takes the decision surface down
        hints = []
    return {"id": r.id, "agent": visible(r.agent), "tool": visible(r.tool_name), "tier": r.tier, "state": entry.state,
            # agent-controlled text: invisible format characters show as markers (a bidi override must not reorder it)
            "display": visible(r.display), "cwd": visible(r.cwd), "targets": visible(consent_targets(r)),
            "answer_allow": f"levain consent answer {r.id} allow --signing-key <key>" if answerable else None,
            "answer_deny": f"levain consent answer {r.id} deny --signing-key <key>" if answerable else None,
            "note": TOO_LONG if pending and not answerable else "",
            # Q12: the two values the terminal prints before signing; Q16: hints, which inform and never authorise
            "tool_input_digest": r.tool_input_digest, "digest": r.digest,
            "hints": hints,
            "audit": "" if pending else visible(audit_line(entry))}


def _check_directory(directory: Path) -> None:
    from levain.consent.queue import QueueStoreError

    st = os.lstat(directory)
    if os.path.islink(directory) or not os.path.isdir(directory):
        raise QueueStoreError(f"consent queue directory {directory} is not a directory")
    if st.st_uid != os.getuid():
        raise QueueStoreError(f"consent queue directory {directory} is not owned by uid {os.getuid()}")
    if st.st_mode & _ACCESS_BY_OTHERS:
        raise QueueStoreError(f"consent queue directory {directory} is reachable by group or others "
                              f"(mode {st.st_mode & 0o777:o}); it must be 0700")


def _grant_id_of(answer: Any) -> str | None:
    try:
        gid = json.loads(answer)["grant_id"]
    except (TypeError, ValueError, KeyError):
        return None
    return gid if isinstance(gid, str) else None


@dataclass(frozen=True)
class KeyEvidence:
    """What the decided requests of one (agent, tool, working directory) say over the last :data:`HISTORY`: how many
    were allowed by hand (a signed answer, below the agent-config tier), when the latest of those was, whether any
    was denied, and whether the latest decision of all was a standing grant's."""
    agent: str
    tool: str
    cwd: str
    hand_allows: int
    last_hand_at: str
    denied: bool
    latest_via_grant: bool


def read_evidence(directory: Path | str | None = None, *, now: datetime, exclude_tier: str,
                  min_hand: int = 1) -> list[KeyEvidence]:
    """The standing-rule proposals' evidence, computed in the database per key (``GROUP BY agent, tool_name, cwd``)
    over the allowed and denied requests of the last :data:`HISTORY` (a hand-allow is a SIGNED ANSWER only,
    ``via = 'signature'``: a late ruling is a judgement on one call after its hold ended, not evidence that the
    operator would allow it in the moment, so it never counts toward a standing-rule proposal): no row cap is
    involved, so no number of newer
    rows of other keys can crowd out a key's allows or its denial. Only keys with at least ``min_hand`` hand-allows
    come back, most first then most recent, every one of them (:func:`levain.cockpit.proposals.propose` caps the cards); the
    per-key latest-decision lookup runs only for keys past ``min_hand`` signed allows with no denial.
    Same read-only rules as :func:`read_listing`; a request's seal is not re-checked (the count is an aggregate)."""
    conn = _open_ro(directory)
    if conn is None:
        return []
    horizon = utc_stamp(now - HISTORY)
    try:
        conn.execute("BEGIN")
        _check_format(conn)
        rows = conn.execute(
            "SELECT agent, tool_name, cwd, "
            "SUM(CASE WHEN state = 'allowed' AND via = 'signature' AND tier != ?1 THEN 1 ELSE 0 END) AS hand, "
            "MAX(CASE WHEN state = 'allowed' AND via = 'signature' AND tier != ?1 THEN decided_at END), "
            "MAX(CASE WHEN state = 'denied' THEN 1 ELSE 0 END) AS denied, "
            "(SELECT r2.via FROM requests r2 WHERE r2.agent = r.agent AND r2.tool_name = r.tool_name AND r2.cwd = r.cwd "
            "AND r2.state IN ('allowed', 'denied') AND r2.decided_at >= ?2 ORDER BY r2.decided_at DESC, r2.id DESC LIMIT 1) AS latest_via "
            "FROM requests r WHERE state IN ('allowed', 'denied') AND decided_at >= ?2 "
            "GROUP BY agent, tool_name, cwd HAVING hand >= ?3 AND denied = 0 AND latest_via IS NOT 'grant' "
            "ORDER BY hand DESC, MAX(decided_at) DESC", (exclude_tier, horizon, min_hand)).fetchall()
        conn.execute("COMMIT")
    finally:
        conn.close()
    return [KeyEvidence(a, t, c, int(n), last or "", bool(d), v == "grant") for a, t, c, n, last, d, v in rows]


def _open_ro(directory: Path | str | None) -> sqlite3.Connection | None:
    """A read-only connection to the queue in ``directory``, or None when there is no queue (nothing is created)."""
    from levain.consent.queue import DB_NAME, default_queue_dir

    base = Path(directory) if directory is not None else default_queue_dir()
    path = base / DB_NAME
    try:
        _check_directory(base)
    except FileNotFoundError:
        return None
    if not path.is_file():
        return None
    # a short busy wait: the page's read worker never stalls on a writer holding the database
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=0.25)


def _check_format(conn: sqlite3.Connection) -> None:
    from levain.consent.queue import PRIOR_FORMAT, STORE_FORMAT, QueueStoreError

    fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    # a store of the previous format holds the same request columns; a queue migrates it on its next open
    if fmt is None or fmt[0] not in (STORE_FORMAT, PRIOR_FORMAT):
        raise QueueStoreError(f"store format {None if fmt is None else fmt[0]!r} is not {STORE_FORMAT!r}")


def read_listing(directory: Path | str | None = None, *, now: datetime, span: timedelta = RECENT,
                 cap: int = RECENT_CAP) -> ConsentListing:
    """The queue in ``directory`` (default :func:`~levain.consent.queue.default_queue_dir`) as of ``now``.
    ``span`` and ``cap`` bound the terminal requests read (the last day and :data:`RECENT_CAP` by default).
    No directory or no file is an empty listing and nothing is created. A store that cannot be trusted (another
    format, a directory others can reach) raises :class:`~levain.consent.queue.QueueStoreError`; a database
    that cannot be read raises ``sqlite3.Error``: the caller's note, never a dark panel."""
    conn = _open_ro(directory)
    if conn is None:
        return ConsentListing()
    stamp = utc_stamp(now)
    horizon = utc_stamp(now - span)
    cols = ", ".join(_REQUEST_FIELDS) + ", state, decided_at, answer, signer_fp, via"
    try:
        conn.execute("BEGIN")
        _check_format(conn)
        # every read is bounded and indexed (requests_pending, requests_decided): the queue's whole history is
        # never scanned. The live pending come soonest-expiring first, one past the cap, so overflow is known
        # without counting every row; the unswept expired ones (which would crowd the live ones out of that
        # order) are read apart, newest expiry first, within the recent cap
        rows = conn.execute(
            f"SELECT {cols} FROM requests WHERE state = 'pending' AND expires_at > ? "
            "ORDER BY expires_at ASC, rowid ASC LIMIT ?", (stamp, PENDING_CAP + 1)).fetchall()
        overflow = len(rows) > PENDING_CAP
        rows = rows[:PENDING_CAP]
        rows += conn.execute(
            f"SELECT {cols} FROM requests WHERE state = 'pending' AND expires_at <= ? AND expires_at >= ? "
            "ORDER BY expires_at DESC LIMIT ?", (stamp, horizon, cap)).fetchall()
        rows += conn.execute(
            f"SELECT {cols} FROM requests WHERE state NOT IN ('pending', 'undecided') AND decided_at >= ? "
            "ORDER BY decided_at DESC LIMIT ?", (horizon, cap)).fetchall()
        # every undecided request, whatever its age: it waits on a ruling (a /3 store has none)
        undecided_rows = conn.execute(
            f"SELECT {cols} FROM requests WHERE state = 'undecided' ORDER BY decided_at ASC, rowid ASC LIMIT ?",
            (PENDING_CAP + 1,)).fetchall()
        overflow = overflow or len(undecided_rows) > PENDING_CAP
        rows += undecided_rows[:PENDING_CAP]
        gaps: dict[str, int] = {}
        # a store opened only by a levain before dialog outcomes has no such table: no outcomes
        outcomes: dict[str, str] = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dialog_outcomes'").fetchone():
            ids = [r[0] for r in rows]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                outcomes.update(conn.execute(
                    f"SELECT request_id, outcome FROM dialog_outcomes WHERE request_id IN "
                    f"({', '.join('?' for _ in chunk)})", chunk).fetchall())
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'gaps'").fetchone():
            ids = [r[0] for r in rows if r[len(_REQUEST_FIELDS)] in ("undecided", "allowed", "denied")]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                gaps.update((rid, g) for rid, g in conn.execute(
                    f"SELECT request_id, gap FROM gaps WHERE request_id IN "
                    f"({', '.join('?' for _ in chunk)})", chunk).fetchall() if type(g) is int)
        conn.execute("COMMIT")
    finally:
        conn.close()
    pending: list[ConsentEntry] = []
    undecided: list[ConsentEntry] = []
    recent: list[ConsentEntry] = []
    skipped = 0
    for row in rows:
        fields = dict(zip(_REQUEST_FIELDS, row[:len(_REQUEST_FIELDS)]))
        state, decided_at, answer, signer_fp, via = row[len(_REQUEST_FIELDS):]
        try:
            request = ExternalGateRequest(**fields)
            sealed = request.seal_matches()
            # the seal says the fields are as filed, not that they were filed through create(): a self-sealed
            # row with a stamp that does not parse is skipped here, never compared or dated downstream
            parse_stamp(request.received_at)
            parse_stamp(request.expires_at)
            if decided_at is not None:
                parse_stamp(decided_at)
        except Exception:  # noqa: BLE001 - a row that does not even build, or does not date, is skipped
            sealed = False
        if not sealed:
            skipped += 1
            continue
        if state == "pending":
            if request.expires_at > stamp:
                pending.append(ConsentEntry(request, "pending", None, None, None, None))
            elif request.expires_at >= horizon:    # nobody swept it: it reads as expired, at its expiry
                recent.append(ConsentEntry(request, "expired", None, None, None, request.expires_at,
                                           outcomes.get(request.id)))
        elif state == "undecided":
            undecided.append(ConsentEntry(request, state, None, None, None, decided_at, None, gaps.get(request.id)))
        elif decided_at is not None and decided_at >= horizon:
            gid = _grant_id_of(answer) if via == "grant" else None
            recent.append(ConsentEntry(request, state, via, signer_fp, gid, decided_at, outcomes.get(request.id),
                                       gaps.get(request.id)))
    pending.sort(key=lambda e: (e.request.received_at, e.request.id))
    undecided.sort(key=lambda e: (e.decided_at or "", e.request.id))
    recent.sort(key=lambda e: (e.decided_at or "", e.request.id), reverse=True)
    return ConsentListing(tuple(pending), tuple(recent[:cap]), skipped, overflow, tuple(undecided))
