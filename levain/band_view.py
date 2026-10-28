"""levain.band_view — the NEEDS YOU band: the whole of what waits on the operator, rendered into a seat.

S1 of Q17-ALL (project_memory/1028-12/Q17ALL_DESIGN.md): the band over OPEN QUESTIONS only. Each open question is
shown with a BAND NUMBER, a number space of its own (the question's ``#n`` stays as the source tag), allocated in the
append-only ``band_items`` table of ``questions.db``: ``n INTEGER PRIMARY KEY AUTOINCREMENT`` and ``UNIQUE (source,
ref)``, so a number is never reused, not after a restart and not after the band empties (D2). A number is allocated the
first time an item is SHOWN, inside one bounded write (D3); a busy lock leaves the item as ``new (number pending)``,
never with a provisional number. The whole band is printed every time, never reordered, never cut: past
:data:`COMPACT_AFTER` items the rest take one line each (D15), and an item that cannot be read is listed as UNREADABLE
(D13). Closed is derived (the question has an answer) and shown as ``closed N`` in the next hook render, then it leaves; its
number stays retired. The notice is recorded as shown only AFTER the hook's ``emit`` returned True (at-least-once).

Three seams use it: the UserPromptSubmit hook (:func:`hook_prompt`: the render, nothing parsed), the SessionStart
contract line (:func:`hook_contract`) and ``levain band`` (:func:`run_band`). The hooks FAIL OPEN: any error prints
nothing and never blocks the prompt. A seat that no human turn can reach (autonomous, D4) gets nothing.

Known edges, none built against (later slices): no shorthand parse and no per-viewer delta (``closed N`` is shown once
for the store, to the first hook that renders it); no source but questions; a question the coordinator holds as first
reader is shown as any other. ``levain band`` numbers what it shows (a first show) but consumes no ``closed``.
A seat with no non-empty string ``session_id`` (``levain verify-hooks``) gets no band and consumes nothing.
Also known: (1) Codex hooks are not installed by S1; Codex parity is S4's. (2) Two hooks rendering at once may both show
the same ``closed N`` (at-least-once; no claim protocol is built). (3) A dispatch worker's first prompt can fire before
its ``active`` launch event carries ``harness_session_id``: it is then classed non-autonomous and may be shown the band
and a closed notice (at most a duplicate, since the notice is marked only after delivery). (4) The read snapshot waits
up to HOOK_BUSY_MS on its own, before the write's deadline starts; a worst case is about twice the bound, and a
store not in WAL mode is the operator's to fix (the reader then blocks on any writer). (5) An item that cannot render
(an unreadable row, more options than letters) is listed UNREADABLE, never dropped.
Stdlib plus the question store.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.consent.questions import HOOK_BUSY_MS, Question, QuestionStore, _now_utc, _select

__all__ = ["BAND_SCHEMA", "COMPACT_AFTER", "hook_contract", "hook_prompt", "is_autonomous", "mark_closed_shown", "render",
           "render_with_closed", "run_band"]

COMPACT_AFTER = 12
_COMPACT_WHAT = 100
_LETTERS = "abcdef"                       # an option's display letter is its position; the stored key never shows
# a number shown must be final: this is the one place numbers are made, and nothing rewrites or removes one
BAND_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS band_items (n INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL CHECK (source "
    "IN ('question','consent','sign','extend')), ref TEXT NOT NULL, numbered_at TEXT NOT NULL, UNIQUE (source, ref))",
    # an item's closing, shown once to a hook: written once (a viewer's own delta is S2)
    "CREATE TABLE IF NOT EXISTS band_closed_shown (n INTEGER PRIMARY KEY REFERENCES band_items(n), at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS band_items_frozen BEFORE UPDATE ON band_items "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_items_kept BEFORE DELETE ON band_items "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    # INSERT OR REPLACE drops a row without a delete trigger: refuse a row that reuses a key or an explicit number
    "CREATE TRIGGER IF NOT EXISTS band_items_no_replace BEFORE INSERT ON band_items "
    "WHEN EXISTS (SELECT 1 FROM band_items WHERE (source = NEW.source AND ref = NEW.ref) OR n = NEW.n) "
    "BEGIN SELECT RAISE(ABORT, 'a band number is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS band_closed_shown_frozen BEFORE UPDATE ON band_closed_shown "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_closed_shown_kept BEFORE DELETE ON band_closed_shown "
    "BEGIN SELECT RAISE(ABORT, 'band numbers are append-only'); END",
)

CONTRACT = ("[Levain NEEDS YOU band] When a NEEDS YOU block is in your context, end every reply with it exactly as "
            "Levain last gave it: do not renumber, reorder, drop or answer its items, and say what each one blocks.")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _tag(q: Question) -> str:
    return f"{os.path.basename(q.cwd.rstrip('/')) or q.cwd} · {q.agent}"


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _item_lines(label: str, q: Question, compact: bool) -> list[str]:
    """One item: ``label`` is its number (``7.``) or ``new (number pending):``."""
    rec = next((_LETTERS[i] for i, (k, _) in enumerate(q.options) if k == q.rec), q.rec)
    if compact:
        return [f"{label} [{_tag(q)}] {_short(q.what, _COMPACT_WHAT)} · rec: {rec}"]
    opts = "   ".join(f"{_LETTERS[i]}) {lab}" for i, (_, lab) in enumerate(q.options))
    return [f"{label} [{_tag(q)}] {q.what}", f"    {opts}", f"    rec: {rec} · blocks: {q.blocks} · asked by question #{q.n}"]


def _read(store: QuestionStore) -> tuple[list[tuple[str, Question | None]], dict[str, int], list[tuple[int, str]]]:
    """(the open questions oldest first as ``(id, question)`` with ``None`` for one that cannot be read, the numbers
    allocated so far by ref, the numbered items whose question has since been answered and not yet shown closed), from
    one read-only snapshot. No store: nothing."""
    with store._peek() as conn:
        if conn is None:
            return [], {}, []
        rows = conn.execute(f"{_select(conn)} WHERE a.question_id IS NULL ORDER BY q.n").fetchall()
        items: list[tuple[str, Question | None]] = []
        for row in rows:
            try:
                items.append((row[1], store._question_of(row)))
            except Exception:                 # a row whose options are not JSON: listed, never dropped (D13)
                items.append((row[1], None))
        have = _tables(conn)
        numbers: dict[str, int] = {}
        closed: list[tuple[int, str]] = []
        if "band_items" in have:
            numbers = {r[1]: r[0] for r in conn.execute("SELECT n, ref FROM band_items WHERE source = 'question'")}
            if "band_closed_shown" in have:
                closed = [(r[0], r[1]) for r in conn.execute(
                    "SELECT b.n, b.ref FROM band_items b JOIN answers a ON a.question_id = b.ref WHERE b.source = "
                    "'question' AND NOT EXISTS (SELECT 1 FROM band_closed_shown s WHERE s.n = b.n) ORDER BY b.n")]
    return items, numbers, closed


def _busy(exc: sqlite3.OperationalError) -> bool:
    """Whether ``exc`` is the lock still held (SQLITE_BUSY / SQLITE_LOCKED); any other failure is the store's."""
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in (5, 6)


def _number(store: QuestionStore, refs: list[str], deadline: float) -> dict[str, int]:
    """Number every ``refs`` that has no number yet (in the order given), in ONE write transaction waited for until
    ``deadline``. The numbers come back read under the lock, so two writers never number a ref twice. The lock still
    busy at the deadline: nothing written, nothing returned (``new (number pending)``); any other error is raised."""
    try:
        with store._write_by(deadline) as conn:
            for stmt in BAND_SCHEMA:
                conn.execute(stmt)
            stamp = utc_stamp(_now_utc(store._clock))
            got: dict[str, int] = {}
            for ref in refs:
                row = conn.execute("SELECT n FROM band_items WHERE source = 'question' AND ref = ?", (ref,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES ('question', ?, ?)",
                                 (ref, stamp))
                    row = conn.execute("SELECT n FROM band_items WHERE source = 'question' AND ref = ?",
                                       (ref,)).fetchone()
                got[ref] = row[0]
            return got
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            return {}
        raise


def mark_closed_shown(store: QuestionStore, closed: list[int]) -> None:
    """Record ``closed`` (band numbers) as shown, once each, in one bounded write: call it only AFTER the render was
    delivered. A lock still busy at the deadline records nothing (the notice comes once more: at-least-once)."""
    if not closed:
        return
    try:
        with store._write_by(time.monotonic() + HOOK_BUSY_MS / 1000) as conn:
            for stmt in BAND_SCHEMA:
                conn.execute(stmt)
            stamp = utc_stamp(_now_utc(store._clock))
            for n in closed:
                if conn.execute("SELECT 1 FROM band_closed_shown WHERE n = ?", (n,)).fetchone() is None:
                    conn.execute("INSERT INTO band_closed_shown (n, at) VALUES (?, ?)", (n, stamp))
    except sqlite3.OperationalError as exc:
        if not _busy(exc):
            raise


def render_with_closed(store: QuestionStore | None = None) -> tuple[str, list[int]]:
    """The band as text (the whole of it, every time; ``""`` when nothing is open and nothing closed is due) and the
    band numbers of the ``closed`` notices it carries, consumed by nobody: the caller that DELIVERED the text hands
    them to :func:`mark_closed_shown`. Showing an item numbers it (first show, one bounded write). Raises on a store
    that cannot be trusted: the hook fails open on it."""
    store = store if store is not None else QuestionStore()
    deadline = time.monotonic() + HOOK_BUSY_MS / 1000
    items, numbers, closed = _read(store)
    refs = [i for i, _ in items if i not in numbers]
    if refs:
        numbers = {**numbers, **_number(store, refs, deadline)}
    if not items and not closed:
        return "", []
    shown = [numbers[i] for i, _ in items if i in numbers]
    head = [f"NEEDS YOU · {len(items)} open"]
    if shown or closed:
        head.append(f"band@{max([*shown, *(n for n, _ in closed)])}")
    if closed:
        head.append("closed " + ", ".join(str(n) for n, _ in closed))
    lines = [" · ".join(head)]
    for pos, (ref, q) in enumerate(items):
        n = numbers.get(ref)
        label = f"{n}." if n is not None else "new (number pending):"
        try:
            if q is None:
                raise ValueError("unreadable")
            lines.extend(_item_lines(label, q, compact=pos >= COMPACT_AFTER))
        except Exception:                     # one item that will not render never drops the band (D13)
            lines.append(f"{label} UNREADABLE: question {ref} could not be read (listed, never dropped)")
    return "\n".join(lines), [n for n, _ in closed]


def render(store: QuestionStore | None = None) -> str:
    """:func:`render_with_closed`'s text alone: a look (``levain band``) consumes no ``closed`` notice."""
    return render_with_closed(store)[0]


def is_autonomous(session_id: str, queue_dir: Path | str | None = None) -> bool:
    """Whether no human turn can reach this seat (D4), as far as S1 reads it: the Levain launch row of the harness
    session, a coordinator being attended and a worker not (the only worker Levain launches today is the dispatch
    executor); no launch row, a subagent aside, is a seat the user opened: NOT autonomous (fail-safe: a band shown
    where nobody reads it costs tokens, a band missing from a human seat drops questions)."""
    if "#agent:" in session_id:
        return True                           # a subagent never renders: the band belongs to the session's main thread
    try:
        from levain.coordinator import lease
        from levain.coordinator.store import CoordStore
        conn = CoordStore(queue_dir).open_readonly()
        try:
            ln = lease.by_harness_session(conn, session_id)
        finally:
            conn.close()
    except Exception:                         # no coordinator store, or not one to read: no launch fact
        return False
    return ln is not None and ln.role == "worker"


def _seat_gets_band(payload: Any, environ: Mapping[str, str]) -> bool:
    """A seat with no non-empty string ``session_id`` (``levain verify-hooks``, a malformed payload) is nobody's: no
    band, nothing consumed."""
    from levain.consent.queue import queue_dir_for

    session = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session, str) or not session:
        return False
    return not is_autonomous(session, queue_dir_for(environ))


def _no_commit() -> None:
    return None


def hook_prompt(payload: Any, environ: Mapping[str, str] | None = None) -> tuple[str, Callable[[], None]]:
    """The UserPromptSubmit hook's ``(band text, commit)`` (render only, no parse): call ``commit()`` only once the
    text was DELIVERED, to record its ``closed`` notices as shown. ``("", no-op)`` for an autonomous or id-less seat,
    an empty band or ANY error: it never raises (``commit`` neither) and takes about
    :data:`~levain.consent.questions.HOOK_BUSY_MS` at most per store wait."""
    try:
        from levain.consent.queue import queue_dir_for

        env = os.environ if environ is None else environ
        if not _seat_gets_band(payload, env):
            return "", _no_commit
        store = QuestionStore(queue_dir_for(env))
        text, closed = render_with_closed(store)
    except Exception:
        return "", _no_commit

    def commit() -> None:
        try:
            mark_closed_shown(store, closed)
        except Exception:
            pass

    return text, commit


def hook_contract(payload: Any, environ: Mapping[str, str] | None = None) -> str:
    """The SessionStart contract line (D5 layer 1); ``""`` for an autonomous or id-less seat or on any error."""
    try:
        return CONTRACT if _seat_gets_band(payload, os.environ if environ is None else environ) else ""
    except Exception:
        return ""


def run_band(args: argparse.Namespace) -> int:
    """``levain band``: print the band as a seat would see it, consuming no ``closed``."""
    try:
        store = QuestionStore(args.queue_dir)
        text = render(store)
    except Exception as exc:                  # a store that cannot be trusted: said, not hidden
        print(f"levain band: {exc}", file=sys.stderr)
        return 1
    print(text or "NEEDS YOU · nothing open")
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("band", help="Print the NEEDS YOU band: what waits on you, numbered.")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent directory holding questions.db (default: the levain home's).")
    p.set_defaults(func=run_band)
