"""levain.consent.listing — a READ-ONLY view of the consent queue, for the cockpit page.

The page shows what agents are waiting on; it never decides anything. This reader opens
``<queue dir>/queue.db`` with ``mode=ro`` and goes around :class:`~levain.consent.queue.ConsentQueue` on
purpose: the queue's constructor path creates the directory and the schema, and a sweep writes. Here a
missing directory or file is "no consent queue here" (an empty listing), nothing is created, nothing is
swept, nothing is written.

The rows are an index, never authority (``queue.py``'s own docstring): a request whose seal does not match
is skipped and counted, and a pending request past its expiry that nobody has swept reads as ``expired``
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

__all__ = ["ConsentEntry", "ConsentListing", "RECENT", "RECENT_CAP", "UNSHOWN", "audit_line", "consent_targets",
           "filed_input", "read_listing"]

RECENT = timedelta(hours=24)        # how long a terminal request stays in the listing
RECENT_CAP = 50
UNSHOWN = object()

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
    ``expired``); ``decided_at`` is the decision's time, or the expiry for one that expired unswept."""
    request: ExternalGateRequest
    state: str
    via: str | None
    signer_fp: str | None
    grant_id: str | None
    decided_at: str | None


@dataclass(frozen=True)
class ConsentListing:
    """``pending`` oldest first; ``recent`` newest first, at most :data:`RECENT_CAP`; ``skipped`` the rows whose
    seal did not match."""
    pending: tuple[ConsentEntry, ...] = ()
    recent: tuple[ConsentEntry, ...] = ()
    skipped: int = 0


def audit_line(entry: ConsentEntry) -> str:
    """What happened to a terminal request, in a sentence."""
    if entry.state == "allowed":
        if entry.via == "grant":
            return f"allowed by standing grant {entry.grant_id or '(unnamed)'}"
        return f"allowed by signed answer, key {entry.signer_fp or '(unknown)'}"
    if entry.state == "denied":
        return f"denied by signed answer, key {entry.signer_fp or '(unknown)'}"
    if entry.state == "expired":
        return "expired: no answer, the agent's own dialog stood"
    if entry.state == "withdrawn":
        return "withdrawn"
    return entry.state


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


def read_listing(directory: Path | str | None = None, *, now: datetime) -> ConsentListing:
    """The queue in ``directory`` (default :func:`~levain.consent.queue.default_queue_dir`) as of ``now``.
    No directory or no file is an empty listing and nothing is created. A store that cannot be trusted (another
    format, a directory others can reach) raises :class:`~levain.consent.queue.QueueStoreError`; a database
    that cannot be read raises ``sqlite3.Error``: the caller's note, never a dark panel."""
    from levain.consent.queue import DB_NAME, STORE_FORMAT, QueueStoreError, default_queue_dir

    base = Path(directory) if directory is not None else default_queue_dir()
    path = base / DB_NAME
    try:
        _check_directory(base)
    except FileNotFoundError:
        return ConsentListing()
    if not path.is_file():
        return ConsentListing()
    stamp = utc_stamp(now)
    horizon = utc_stamp(now - RECENT)
    cols = ", ".join(_REQUEST_FIELDS)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    try:
        conn.execute("BEGIN")
        fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        if fmt is None or fmt[0] != STORE_FORMAT:
            raise QueueStoreError(f"store format {None if fmt is None else fmt[0]!r} is not {STORE_FORMAT!r}")
        rows = conn.execute(
            f"SELECT {cols}, state, decided_at, answer, signer_fp, via FROM requests "
            "WHERE state = 'pending' OR decided_at >= ? ORDER BY received_at, rowid", (horizon,)).fetchall()
        conn.execute("COMMIT")
    finally:
        conn.close()
    pending: list[ConsentEntry] = []
    recent: list[ConsentEntry] = []
    skipped = 0
    for row in rows:
        fields = dict(zip(_REQUEST_FIELDS, row[:len(_REQUEST_FIELDS)]))
        state, decided_at, answer, signer_fp, via = row[len(_REQUEST_FIELDS):]
        try:
            request = ExternalGateRequest(**fields)
            sealed = request.seal_matches()
        except Exception:  # noqa: BLE001 - a row that does not even build is a row that does not match
            sealed = False
        if not sealed:
            skipped += 1
            continue
        if state == "pending":
            if request.expires_at > stamp:
                pending.append(ConsentEntry(request, "pending", None, None, None, None))
            elif request.expires_at >= horizon:    # nobody swept it: it reads as expired, at its expiry
                recent.append(ConsentEntry(request, "expired", None, None, None, request.expires_at))
        elif decided_at is not None and decided_at >= horizon:
            gid = _grant_id_of(answer) if via == "grant" else None
            recent.append(ConsentEntry(request, state, via, signer_fp, gid, decided_at))
    recent.sort(key=lambda e: (e.decided_at or "", e.request.id), reverse=True)
    return ConsentListing(tuple(pending), tuple(recent[:RECENT_CAP]), skipped)
