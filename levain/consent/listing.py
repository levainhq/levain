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
from typing import Any, Callable

from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.consent.record import ExternalGateRequest

__all__ = ["ConsentEntry", "ConsentListing", "HISTORY", "KeyEvidence", "PENDING_CAP", "RECENT", "RECENT_CAP",
           "UNSHOWN", "audit_line", "consent_targets", "entry_body", "filed_input", "find_entry", "read_evidence",
           "read_grant_evidence", "read_listing"]

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
        named = _named_by_tool(request.tool_name, tool_input)
        return named if named is not None else "(none named: the working directory)"
    # a path named by two readers (a file tool's file_path is also a path field) is shown once
    return ", ".join(dict.fromkeys("(unreadable)" if t is None else str(t) for t in got))


def _field(call: dict, key: str) -> str:
    """One filed field as text; a missing, empty or non-scalar one reads ``(absent)``."""
    v = call.get(key)
    return str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v) else "(absent)"


def _named_by_tool(tool_name: str, tool_input: object) -> str | None:
    """What a levain-filed publish or limit request names, read from its filed input (they name no path, so the
    targets reader finds none). ``None`` for any other tool or a non-mapping input. A display only."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "levain_publish":       # publish_gate.PUBLISH_TOOL
        return (f"publish {_field(tool_input, 'package')} {_field(tool_input, 'version')} to "
                f"{_field(tool_input, 'index')} ({_field(tool_input, 'target')})")
    if tool_name == "levain_limit":         # limits.LIMIT_TOOL
        return (f"{_field(tool_input, 'agent')} session {_field(tool_input, 'session')} limit "
                f"{_field(tool_input, 'limit')} count {_field(tool_input, 'count')} "
                f"extend_to {_field(tool_input, 'extend_to')}")
    return None


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
        if entry.via == "contained_shell":
            return f"allowed by contained-shell grant {entry.grant_id or '(unnamed)'}"
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


def home_relative(cwd: str) -> str:
    """``cwd`` as ``~/...`` when it is the server user's home or under it, else unchanged: the home is the server's
    (``Path.home()``), never the browser's."""
    try:
        home = str(Path.home()).rstrip("/\\")
    except (RuntimeError, OSError):
        return cwd
    if not home or home == "/":
        return cwd
    if cwd == home:
        return "~"
    return "~" + cwd[len(home):] if cwd.startswith(home + os.sep) else cwd


def entry_body(entry: ConsentEntry, settled_lookup: Callable[[str, str], tuple[Any, bool]] | None = None
               ) -> dict[str, Any]:
    """What the page shows of one request beyond its panel row: the request as filed, where, what it names, how
    to answer it from a terminal, and, once decided, how it was. Served only to a caller holding a surface token
    (``web_server``'s consent route), never in the token-free panel. A request whose display is cut at its limit
    offers no commands: ``levain consent answer`` refuses to sign what it cannot show.

    ``settled_lookup(launch, request)`` is a READ-ONLY reading of a chat launch's egress rows
    (``levain.chat_egress.records.EgressReader.reading``). Given one, a ``levain_egress`` card's body also says what
    the request became (``egress_state``, ``chat_egress.cards.card_state``), or, when the rows cannot be read, says so
    (``egress_note``). Without one (the default) the body is as it was for every caller."""
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
            "display": visible(r.display), "cwd": visible(r.cwd), "cwd_short": visible(home_relative(r.cwd)),
            "targets": visible(consent_targets(r)),
            "answer_allow": f"levain consent answer {r.id} allow --signing-key <key>" if answerable else None,
            "answer_deny": f"levain consent answer {r.id} deny --signing-key <key>" if answerable else None,
            "note": TOO_LONG if pending and not answerable else "",
            # Q12: the two values the terminal prints before signing; Q16: hints, which inform and never authorise
            "tool_input_digest": r.tool_input_digest, "digest": r.digest,
            "hints": hints,
            "audit": "" if pending else visible(audit_line(entry)),
            **(_egress(entry, shown, settled_lookup) if settled_lookup is not None else {})}


UNREADABLE = "unreadable"   # the listing's egress_state when a configured lookup could not answer (not a CardState)


def _egress(entry: ConsentEntry, shown: object, lookup: Callable[[str, str], tuple[Any, bool]]) -> dict[str, str]:
    """``egress_state`` for a ``levain_egress`` card (nothing for another tool); ``egress_note`` when its egress rows
    cannot be read, so the page never shows a queue-only word as if the rows had been read. The decisions listing's
    rows carry the same word (:func:`egress_state_of`, this function's own computation)."""
    from levain.chat_egress.cards import EGRESS_TOOL, card_state

    if entry.request.tool_name != EGRESS_TOOL:
        return {}
    if not (isinstance(shown, dict) and isinstance(shown.get("launch"), str) and isinstance(shown.get("request"), str)):
        return {"egress_note": "this card names no launch and request to read its egress record by"}
    try:
        settled, in_flight = lookup(shown["launch"], shown["request"])
        return {"egress_state": card_state(entry.state, settled, in_flight=in_flight).value}
    except Exception as exc:  # noqa: BLE001 - an unreadable record is said on the card, never a dead consent body
        return {"egress_note": f"the egress record could not be read ({type(exc).__name__})"}


def egress_state_of(entry: ConsentEntry, lookup: Callable[[str, str], tuple[Any, bool]] | None) -> str | None:
    """The card's state word for a ``levain_egress`` entry, the one :func:`entry_body` serves as ``egress_state``;
    None for another tool's entry (its body is never read) or with no lookup configured. A lookup that was configured
    and could not answer (the body unreadable, the egress rows unreadable) is :data:`UNREADABLE`, never a queue-only
    word the page would draw as if the rows had been read."""
    from levain.chat_egress.cards import EGRESS_TOOL

    if lookup is None or entry.request.tool_name != EGRESS_TOOL:
        return None
    try:
        return _egress(entry, filed_input(entry.request), lookup).get("egress_state", UNREADABLE)
    except Exception:  # noqa: BLE001 - one card that cannot be read is said so, never a dropped row
        return UNREADABLE


def egress_reading(reader: object, busy_timeout: float | None = None) -> Callable[[str, str], tuple[Any, bool]] | None:
    """A read-only reading of the chat egress rows (``EgressReader.reading``) over the dispatch store beside the queue
    ``reader`` lists (``dispatch.db`` lives in the consent directory), or None for a reader naming no queue: never the
    default queue's then, as the proposals route. The reading is meant for ONE listing build (or one body read): the
    first failure is kept and raised again for every later request, so a locked or corrupt store costs one failed read
    per build, never one per card. ``busy_timeout`` (seconds) is how long a read waits on a locked store; None keeps
    ``EgressReader``'s default."""
    from levain.chat_egress.records import EgressReader
    from levain.consent.queue import default_queue_dir

    if not hasattr(reader, "directory"):
        return None
    egress = EgressReader(reader.directory if reader.directory is not None else default_queue_dir(),
                          **({} if busy_timeout is None else {"busy_timeout": busy_timeout}))
    failed: list[Exception] = []

    def reading(launch_id: str, request_id: str) -> tuple[Any, bool]:
        if failed:
            raise failed[0]
        try:
            return egress.reading(launch_id, request_id)
        except Exception as exc:  # noqa: BLE001 - kept for the rest of this build, raised to each caller
            failed.append(exc)
            raise
    return reading


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


def read_grant_evidence(directory: Path | str | None, grants: "list[tuple[str, int, str]]", *, recent: int = 5
                        ) -> dict[str, dict[str, Any]]:
    """What each standing grant answered: ``{grant id: {"count", "last_at", "recent", "refused_count"}}`` for
    ``grants``, each ``(id, seq, hash)`` of the link that created or last changed it. An answer counts for a grant only when its
    stored answer names all three (``grant_id``, ``grant_seq``, ``grant_hash``), so a grant revoked and made again
    under the same id starts with nothing. Read from the requests the queue allowed by a grant (``via = 'grant'``),
    over the queue's whole history. ``recent`` holds the newest ``recent`` of them, each ``{"request_id", "at",
    "tool", "cwd", "outcome"}``; ``outcome`` is the authority ledger's word for whether that call then ran
    (``accepted``, ``refused``, ``unknown`` when the ledger holds no row for it). ``refused_count`` is how many of
    ALL its calls (not only the newest ``recent``) the ledger reads as refused, so a refusal older than those still
    counts; a ledger that cannot be read now counts none (as ``recent`` then reads ``unknown``). A grant that
    answered nothing is absent. Same read-only rules as :func:`read_listing`, the ledger's too
    (:func:`levain.consent.ledger.request_outcomes`); no queue (or no ledger) creates none. ``tool`` and ``cwd`` are
    the agent's text, as stored: the caller shows them through ``visible``."""
    out: dict[str, dict[str, Any]] = {}
    wanted_grants = sorted(set(grants))
    conn = _open_ro(directory) if wanted_grants else None
    if conn is None:
        return out
    from levain.consent.ledger import DB_NAME as LEDGER_DB

    base = Path(directory) if directory is not None else _default_dir()
    ledger_path = base / LEDGER_DB
    ledger_attached = False
    try:
        if ledger_path.is_file():
            # ATTACH is refused inside a transaction: it comes first, read-only, so the queue and the ledger are then
            # read in the ONE transaction below (one snapshot: a count and a row can never disagree)
            try:
                conn.execute("ATTACH DATABASE ? AS l", (ledger_path.resolve().as_uri() + "?mode=ro",))
                ledger_attached = True
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
        conn.execute("BEGIN")
        _check_format(conn)
        match = ("via = 'grant' AND state = 'allowed' AND json_extract(answer, '$.grant_id') = ? "
                 "AND json_extract(answer, '$.grant_seq') = ? AND json_extract(answer, '$.grant_hash') = ?")
        links: dict[str, tuple[int, str]] = {}
        for gid, seq, link_hash in wanted_grants:
            n, last = conn.execute(f"SELECT COUNT(*), MAX(decided_at) FROM requests WHERE {match}",
                                   (gid, seq, link_hash)).fetchone()
            if not n:
                continue
            calls = conn.execute(
                f"SELECT id, decided_at, tool_name, cwd FROM requests WHERE {match} "
                "ORDER BY decided_at DESC, rowid DESC LIMIT ?", (gid, seq, link_hash, max(recent, 0))).fetchall()
            out[gid] = {"count": int(n), "last_at": last or "",
                        "recent": [{"request_id": rid, "at": at, "tool": tool, "cwd": cwd, "outcome": "unknown"}
                                   for rid, at, tool, cwd in calls], "refused_count": 0}
            links[gid] = (seq, link_hash)        # the same iteration as ``out``: count and refusals name ONE instance
        if out and ledger_attached:
            try:
                _read_ledger_into(conn, out, links)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                for g in out.values():           # a ledger still locked after the short wait: no outcome, none refused
                    g["refused_count"] = 0
                    for c in g["recent"]:
                        c["outcome"] = "unknown"
        conn.execute("COMMIT")
    finally:
        conn.close()
    return out


def _default_dir() -> Path:
    from levain.consent.queue import default_queue_dir

    return default_queue_dir()


def _read_ledger_into(conn: sqlite3.Connection, out: "dict[str, dict[str, Any]]",
                      links: "dict[str, tuple[int, str]]") -> None:
    """Fill the outcomes of the shown rows and every grant's ``refused_count`` from the ledger attached as ``l`` to
    ``conn``, inside the caller's one read transaction (the queue rows were read in it). A ledger of another format
    raises :class:`~levain.consent.ledger.LedgerStoreError` (the same check as
    :func:`levain.consent.ledger.request_outcomes`). ``calls`` below is unqualified on purpose: the per-call view
    text names it bare and only the attached ledger has that table.
    The refusal count is ONE grouped query per chunk of listed grants: the listed grants' requests joined to the set
    of request ids with a refused ``pre`` row. That set is collected from the WHOLE ledger once per chunk, so this
    read's cost grows with the ledger's refused rows, not with the listed grants alone (known edge, L3 r4 LOW, closed
    under the reachability line: a correlated per-request form measured ~10x slower at 20,006 requests). The queue
    and ledger snapshots each start at their file's first read, so they are two points in time; ``refused_count``
    cannot exceed ``count`` because the join is driven from the queue rows. A request counts refused if ANY of its
    ``pre`` rows reads refused: a deliberate choice [judged by m17 fix3 lane, 2026-10-22, against the old
    last-row-wins dict, which was arbitrary here], and it counts once however many rows name it."""
    from levain.consent.ledger import _PER_CALL, STORE_FORMAT as LEDGER_FORMAT, LedgerStoreError

    tables = {r[0] for r in conn.execute("SELECT name FROM l.sqlite_master WHERE type = 'table'")}
    fmt = conn.execute("SELECT value FROM l.meta WHERE key = 'format'").fetchone() if "meta" in tables else None
    if fmt is None or fmt[0] != LEDGER_FORMAT or "calls" not in tables or "outcome" not in {
            r[1] for r in conn.execute("PRAGMA l.table_info(calls)")}:
        raise LedgerStoreError(f"store format {None if fmt is None else fmt[0]!r} is not {LEDGER_FORMAT!r}")
    shown = sorted({c["request_id"] for g in out.values() for c in g["recent"]})
    outcomes: dict[str, str] = {}
    for i in range(0, len(shown), 500):          # bounded by ``recent`` per grant
        chunk = shown[i:i + 500]
        outcomes.update(conn.execute(
            f"SELECT request_id, outcome FROM {_PER_CALL} WHERE phase = 'pre' AND request_id IN "
            f"({', '.join('?' for _ in chunk)})", chunk).fetchall())
    for g in out.values():
        for c in g["recent"]:
            c["outcome"] = outcomes.get(c["request_id"], "unknown")
    instances = sorted({(gid, seq, h) for gid, (seq, h) in links.items()})
    for i in range(0, len(instances), 200):
        chunk = instances[i:i + 200]
        params = [v for inst in chunk for v in inst]
        rows = conn.execute(
            "SELECT json_extract(r.answer, '$.grant_id'), json_extract(r.answer, '$.grant_seq'), "
            "json_extract(r.answer, '$.grant_hash'), COUNT(*) FROM requests r "
            f"JOIN (SELECT DISTINCT request_id FROM {_PER_CALL} WHERE phase = 'pre' AND outcome = 'refused') f "
            "ON f.request_id = r.id "
            "WHERE r.via = 'grant' AND r.state = 'allowed' AND (json_extract(r.answer, '$.grant_id'), "
            "json_extract(r.answer, '$.grant_seq'), json_extract(r.answer, '$.grant_hash')) IN (VALUES "
            + ", ".join("(?, ?, ?)" for _ in chunk) + ") GROUP BY 1, 2, 3", params).fetchall()
        got = {(gid, seq, h): int(n) for gid, seq, h, n in rows}
        for gid, g in out.items():
            seq, h = links[gid]
            g["refused_count"] = got.get((gid, seq, h), g["refused_count"])


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
    from levain.consent.queue import MIGRATES_FROM, STORE_FORMAT, QueueStoreError

    fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    # a store of an earlier format holds the same request columns; a queue migrates it on its next open
    if fmt is None or (fmt[0] != STORE_FORMAT and fmt[0] not in MIGRATES_FROM):
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
            gid = _grant_id_of(answer) if via in ("grant", "contained_shell") else None
            recent.append(ConsentEntry(request, state, via, signer_fp, gid, decided_at, outcomes.get(request.id),
                                       gaps.get(request.id)))
    pending.sort(key=lambda e: (e.request.received_at, e.request.id))
    undecided.sort(key=lambda e: (e.decided_at or "", e.request.id))
    recent.sort(key=lambda e: (e.decided_at or "", e.request.id), reverse=True)
    return ConsentListing(tuple(pending), tuple(recent[:cap]), skipped, overflow, tuple(undecided))
