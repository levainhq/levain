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

S2 (:mod:`levain.band_shorthand`): each delivered render stores a view row (``band_views``); the seat hook parses the
human's prompt against the viewer's last view (its greatest render generation), records a full parse through ONE
bounded ``QuestionStore.answer_with_context`` write (channel ``cli``, ``answer_context.via = 'seat'`` in the same
transaction, a note kept verbatim) and echoes ``recorded: ... refused: ...`` plus the viewer's delta
at the head of the next band; ``levain band answer`` is the same path (refused inside any agent session, Q1 (a)). A
question with a Link is the one AUTHORITY kind S2 can see: shorthand records nothing on it (S3 routes it to its sheet).

Known edges, none built against (later slices): ``closed N`` is shown once for the store, to the first hook that
renders it (the viewer's own delta is separate); no source but questions; a question the coordinator holds as first
reader is shown as any other. ``levain band`` numbers what it shows (a first show) but consumes no ``closed``.
A seat with no non-empty string ``session_id`` (``levain verify-hooks``) gets no band and consumes nothing.
Also known: (1) Codex hooks (SessionStart, UserPromptSubmit, and the Stop verifier) are installed from S4. (2) Two hooks rendering at once may both show
the same ``closed N`` (at-least-once; no claim protocol is built). (3) A dispatch worker's first prompt can fire before
its ``active`` launch event carries ``harness_session_id``: it is then classed non-autonomous and may be shown the band
and a closed notice (at most a duplicate, since the notice is marked only after delivery). (4) S3 fix round 2: the
hook runs on ONE deadline (start + HOOK_BUSY_MS) covering the launch-row read, every snapshot, the answers' write, a
first-time store setup and the numbering; a store held past it is said ("! not read") or left "number pending".
The view row and the closed mark are written after delivery (``commit``): their write tries once more past the
deadline. A store not in WAL mode is the operator's to fix (the reader then blocks on any writer). (5) An item that cannot render
(an unreadable row, more options than letters) is listed UNREADABLE, never dropped.
Stdlib plus the question store.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from levain.band_shorthand import ItemInfo, Parse, Refusal, View, parse
from levain.band_sources import Extra, Extras, NotOffered, read_extras, sheet_text
from levain.cockpit.consent import utc_stamp
from levain.consent.questions import HOOK_BUSY_MS, Question, QuestionStore, _now_utc, _select, read_deadline

__all__ = ["BAND_SCHEMA", "COMPACT_AFTER", "Applied", "apply_plan", "format_echo", "hook_contract", "hook_prompt",
           "band_hooks_state", "gate_notice", "hook_stop", "is_autonomous", "last_view", "mark_closed_shown", "plan_for", "plan_for_full", "record_view", "render", "render_full", "render_with_closed",
           "run_band", "run_band_answer"]

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
    # S2 (D7): each render's view: what a viewer was shown, so an answer binds to it. `gen` is the render's own
    # generation (its wall-clock start, ns): the viewer's last view is its greatest gen, never its last commit
    "CREATE TABLE IF NOT EXISTS band_views (seq INTEGER PRIMARY KEY AUTOINCREMENT, viewer TEXT NOT NULL, "
    "seen_through INTEGER NOT NULL, open_list TEXT NOT NULL, store_id TEXT NOT NULL, at TEXT NOT NULL, "
    "gen INTEGER NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS band_views_frozen BEFORE UPDATE ON band_views "
    "BEGIN SELECT RAISE(ABORT, 'band views are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS band_views_kept BEFORE DELETE ON band_views "
    "BEGIN SELECT RAISE(ABORT, 'band views are append-only'); END",
    # the rendered band block of a view (what the Stop verifier compares a reply's reprint with), keyed as the view is
    "CREATE TABLE IF NOT EXISTS band_view_blocks (viewer TEXT NOT NULL, gen INTEGER NOT NULL, block TEXT NOT NULL, "
    "PRIMARY KEY (viewer, gen))",
    # S2 (D14): the sidecar of a band answer; the `answers` table and its channel CHECK are untouched
    "CREATE TABLE IF NOT EXISTS answer_context (question_id TEXT PRIMARY KEY REFERENCES answers(question_id), "
    "via TEXT NOT NULL CHECK (via IN ('seat','cli','cockpit','phone')), viewer TEXT, seen_through INTEGER, note TEXT)",
    "CREATE TRIGGER IF NOT EXISTS answer_context_frozen BEFORE UPDATE ON answer_context "
    "BEGIN SELECT RAISE(ABORT, 'answer context is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS answer_context_kept BEFORE DELETE ON answer_context "
    "BEGIN SELECT RAISE(ABORT, 'answer context is append-only'); END",
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


FOREIGN_STORE_ID = "foreign"
FOREIGN_VIEW_ID = 0


def _foreign(store: Any) -> bool:
    """A store with no ``_peek`` is not a levain :class:`QuestionStore` but a QuestionStore-SHAPED reader a host injects
    (flow's ``NeedsQuestions``: ``open_questions`` / ``recent`` / ``get`` / ``answer`` over its own log, no sqlite).
    The band reads it through those public methods only, numbers by the store's own ``q.n``, and keeps no band
    view, number or sidecar in it (levain cannot write into it)."""
    return not hasattr(store, "_peek")


def _foreign_open(store: Any) -> list[Question]:
    """The foreign store's open questions, oldest number first. A number two open questions share names neither (the
    same rule as the store's own ``get``): both stay listed, but neither can be answered by shorthand."""
    return sorted(store.open_questions(), key=lambda q: (q.n, q.id))


def _foreign_by_n(store: Any) -> dict[int, Question]:
    qs = _foreign_open(store)
    twice = {q.n for q in qs if sum(1 for o in qs if o.n == q.n) > 1}
    return {q.n: q for q in qs if q.n not in twice}


def _read(store: QuestionStore) -> tuple[list[tuple[str, Question | None]], dict[str, int], list[tuple[int, str]]]:
    """(the open questions oldest first as ``(id, question)`` with ``None`` for one that cannot be read, the numbers
    allocated so far by ref, the numbered items whose question has since been answered and not yet shown closed), from
    one read-only snapshot. No store: nothing. A foreign store: its public ``open_questions``, numbered by its own ``q.n``."""
    if _foreign(store):
        qs = _foreign_open(store)
        return [(q.id, q) for q in qs], {q.id: q.n for q in qs}, []
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


def _number(store: QuestionStore, refs: list[Any], deadline: float) -> dict[Any, int]:
    """Number every ``refs`` that has no number yet (in the order given), in ONE write transaction waited for until
    ``deadline``. A ref is a question id, or ``(source, ref)`` for another source; the result is keyed as given. The numbers come back read under the lock, so two writers never number a ref twice. The lock still
    busy at the deadline: nothing written, nothing returned (``new (number pending)``); any other error is raised."""
    try:
        # no question was ever filed here: the store is set up for the first number, inside the same deadline
        store.ready_by(deadline)
        with store._write_by(deadline) as conn:
            for stmt in BAND_SCHEMA:
                conn.execute(stmt)
            stamp = utc_stamp(_now_utc(store._clock))
            got: dict[Any, int] = {}
            for key in refs:
                source, ref = key if isinstance(key, tuple) else ("question", key)
                row = conn.execute("SELECT n FROM band_items WHERE source = ? AND ref = ?", (source, ref)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO band_items (source, ref, numbered_at) VALUES (?, ?, ?)",
                                 (source, ref, stamp))
                    row = conn.execute("SELECT n FROM band_items WHERE source = ? AND ref = ?",
                                       (source, ref)).fetchone()
                got[key] = row[0]
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


class Rendered(NamedTuple):
    """One render: the text, the ``closed`` band numbers it carries, the band numbers of the open items shown with a
    number, the header's ``band@`` high-water mark (0 when nothing was numbered), and the render's generation (its
    start, ``time.time_ns()``: what orders two views of one viewer, whatever order they are committed in), and whether
    an authority source was not read (``failed``: the text then carries its ``! not read`` line)."""
    text: str
    closed: list[int]
    shown: list[int]
    hwm: int
    gen: int
    failed: bool = False


def render_with_closed(store: QuestionStore | None = None) -> tuple[str, list[int]]:
    """The band as text (the whole of it, every time; ``""`` when nothing is open and nothing closed is due) and the
    band numbers of the ``closed`` notices it carries, consumed by nobody: the caller that DELIVERED the text hands
    them to :func:`mark_closed_shown`. Showing an item numbers it (first show, one bounded write). Raises on a store
    that cannot be trusted: the hook fails open on it."""
    r = render_full(store)
    return r.text, r.closed


def _extra_state(store: QuestionStore, extras: Extras) -> tuple[dict[tuple[str, str], int], list[tuple[int, str]]]:
    """The numbers the authority items already have, and the numbered ones of a fully read source that are no longer
    open and not yet shown closed. Closing is read only from a source that was read in full."""
    with store._peek() as conn:
        if conn is None or "band_items" not in _tables(conn):
            return {}, []
        have = _tables(conn)
        rows = conn.execute("SELECT n, source, ref FROM band_items WHERE source != 'question' ORDER BY n").fetchall()
        shown_closed = ({r[0] for r in conn.execute("SELECT n FROM band_closed_shown")}
                        if "band_closed_shown" in have else set())
    numbers = {(src, ref): n for n, src, ref in rows}
    still = {e.band_key for e in extras.items}
    closed = [(n, ref) for n, src, ref in rows
              if src in extras.complete and (src, ref) not in still and n not in shown_closed]
    return numbers, closed


def _extra_lines(label: str, e: Extra, compact: bool) -> list[str]:
    if e.unreadable:
        return [f"{label} [{e.tag}] {e.what} (listed, never dropped)"]
    if compact:
        return [f"{label} [{e.tag}] {_short(e.what, _COMPACT_WHAT)} · rec: {e.rec}"]
    opts = "   ".join(f"{_LETTERS[i]}) {lab}" for i, lab in enumerate(e.options[:len(_LETTERS)]))
    if not opts:                              # nothing to pick from the band: said, so no letter is guessed at
        opts = f"no letters: {e.data.get('no_letters') or 'settle it on its own sheet'}"
    return [f"{label} [{e.tag}] {e.what}   <- needs your signature",
            f"    {opts}",
            f"    rec: {e.rec} · blocks: {e.blocks} · {e.origin}"]


def render_full(store: QuestionStore | None = None, *, deadline: float | None = None,
                coord_conn: sqlite3.Connection | None = None) -> Rendered:
    """:func:`render_with_closed` plus what the view row needs (:class:`Rendered`). ``deadline``
    (``time.monotonic()``; default now + HOOK_BUSY_MS) bounds EVERY wait of the render: the question snapshots, the
    authority sources' stores and a first-time setup of the question store; a store held past it is said ("! not
    read") or leaves numbers pending, never a stalled prompt. ``coord_conn``: a coordinator snapshot the caller holds
    (the morning card's), read instead of a second one."""
    store = store if store is not None else QuestionStore()
    gen = time.time_ns()                      # before the snapshot: a later render has a greater gen
    deadline = deadline if deadline is not None else time.monotonic() + HOOK_BUSY_MS / 1000
    with read_deadline(deadline):
        return _render_full(store, gen, deadline, coord_conn)


def _render_full(store: QuestionStore, gen: int, deadline: float, coord_conn: sqlite3.Connection | None) -> Rendered:
    items, numbers, closed = _read(store)
    foreign = _foreign(store)
    try:
        # a foreign store has no directory of levain's own: no authority sources are read from it, none are claimed
        extras = Extras() if foreign else read_extras(store.directory, store._clock, deadline=deadline,
                                                      coord_conn=coord_conn)
    except Exception as exc:  # noqa: BLE001 - the questions still render; the band says the sources were not read
        extras = Extras(failed=[f"authority sources: {type(exc).__name__}"])
    extra_numbers, extra_closed = _extra_state(store, extras) if extras.items or extras.complete else ({}, [])
    filed: dict[tuple[str, str], str] = {}
    last = ""
    for i, q in items:                        # a row that cannot be read takes its neighbour's time: its place holds
        last = q.filed_at if q is not None else last
        filed[("question", i)] = last
    entries: list[tuple[tuple[str, str], Question | Extra | None]] = [(("question", i), q) for i, q in items]
    for e in extras.items:
        if e.band_key in filed:
            continue                          # one item per band key: its underlying request is listed once
        entries.append((e.band_key, e))
        filed[e.band_key] = e.filed
    new = [k for k, _ in entries if (numbers.get(k[1]) if k[0] == "question" else extra_numbers.get(k)) is None]
    new.sort(key=lambda k: filed.get(k, ""))              # each source's own filed time; a stable sort keeps ties
    if new:
        got = _number(store, [k[1] if k[0] == "question" else k for k in new], deadline)
        numbers = {**numbers, **{k: v for k, v in got.items() if isinstance(k, str)}}
        extra_numbers = {**extra_numbers, **{k: v for k, v in got.items() if isinstance(k, tuple)}}

    def number_of(k: tuple[str, str]) -> int | None:
        return numbers.get(k[1]) if k[0] == "question" else extra_numbers.get(k)

    closed_all = sorted([*closed, *extra_closed])
    if not entries and not closed_all and not extras.failed:
        return Rendered("", [], [], 0, gen)
    order = sorted(range(len(entries)), key=lambda p: (number_of(entries[p][0]) is None,
                                                       number_of(entries[p][0]) or 0, p))
    shown = [n for p in order if (n := number_of(entries[p][0])) is not None]
    hwm = max([*shown, *(n for n, _ in closed_all)], default=0)
    head = [f"NEEDS YOU · {len(entries)} open"]
    if shown or closed_all:
        head.append(f"band@{hwm}")
    if closed_all:
        head.append("closed " + ", ".join(str(n) for n, _ in closed_all))
    lines = [" · ".join(head)]
    for pos, p in enumerate(order):
        key, obj = entries[p]
        n = number_of(key)
        label = f"{n}." if n is not None else "new (number pending):"
        try:
            if obj is None:
                raise ValueError("unreadable")
            if isinstance(obj, Extra):
                lines.extend(_extra_lines(label, obj, compact=pos >= COMPACT_AFTER))
            else:
                lines.extend(_item_lines(label, obj, compact=pos >= COMPACT_AFTER))
                if obj.id in extras.escalations:
                    text, rec = extras.escalations[obj.id]
                    lines.append(f"    escalated by the coordinator: {_short(text, 200)} · rec on record: {rec}")
        except Exception:                     # one item that will not render never drops the band (D13)
            lines.append(f"{label} UNREADABLE: {key[0]} {key[1]} could not be read (listed, never dropped)")
    lines.extend(f"! not read: {why} (items from it are missing, not closed)" for why in extras.failed)
    return Rendered("\n".join(lines), [n for n, _ in closed_all], shown, hwm, gen, bool(extras.failed))


def render(store: QuestionStore | None = None) -> str:
    """:func:`render_with_closed`'s text alone: a look (``levain band``) consumes no ``closed`` notice."""
    return render_with_closed(store)[0]


# --- S2: views, the shorthand's plan applied, the echo ---------------------------------------------------------------
def _store_id_of(store: QuestionStore) -> str | None:
    """The store's id from a read-only snapshot (``QuestionStore.store_id`` takes the write lock); ``None``: no store."""
    with store._peek() as conn:
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        return row[0] if row else None


def _store_absent(store: QuestionStore) -> bool:
    """True only when the question store is PHYSICALLY absent (no file, or an empty one: :meth:`QuestionStore._peek`
    yields no connection). A store that exists but is locked or unreadable, or lacks its ``store_id`` row, is not
    absent: any sqlite or OS error here answers ``False``, so it keeps its note and never reads as a fresh install."""
    try:
        with store._peek() as conn:
            return conn is None
    except (sqlite3.Error, OSError):                      # a lock, a corrupt file, a PermissionError on the db file
        return False


def record_view(store: QuestionStore, viewer: str, seen_through: int, open_list: list[int],
                deadline: float | None = None, gen: int | None = None, block: str | None = None,
                dedupe: bool = False) -> None:
    """Store what ``viewer`` was just shown (D7), in one bounded write (by ``deadline``, ``time.monotonic()``; the hook
    passes its own start + the bound, so a busy store costs the hook one bound in all). ``gen``: the render's
    generation (:class:`Rendered`; default now); the viewer's last view is the greatest gen, so a render committed
    late never replaces a newer one. A view with nothing open is not written when the viewer's last view already had
    nothing open (no row per empty prompt). A busy lock records nothing (the viewer's next answer is then refused
    as bound to an older view or to none: safe, never wrong). ``block``: the rendered band text of the view, kept
    (same transaction) for the Stop verifier. ``dedupe`` (a polling surface, :func:`show`; S5a decision A (c)): the
    view is written only when the viewer's last one differs in open list or high-water mark, so a poll loop adds no
    row, and an unchanged view takes no write lock at all (decided on a read-only snapshot; two pollers racing may
    each write one equal row, which is harmless)."""
    sid = _store_id_of(store)
    if sid is None:
        return
    if dedupe:
        last = last_view(store, viewer)
        if (last is not None and last.store_id == sid and list(last.open_list) == sorted(open_list)
                and last.seen_through == int(seen_through)):
            return
    gen = int(gen if gen is not None else time.time_ns())
    try:
        with store._write_by(deadline if deadline is not None else time.monotonic() + HOOK_BUSY_MS / 1000) as conn:
            for stmt in BAND_SCHEMA:
                conn.execute(stmt)
            if not open_list:
                # The empty-view dedupe is decided INSIDE the write transaction, against the newest view by gen as
                # the transaction sees it: a render that committed a nonempty view first is never skipped past.
                row = conn.execute("SELECT open_list, store_id FROM band_views WHERE viewer = ? "
                                   "ORDER BY gen DESC, seq DESC LIMIT 1", (viewer,)).fetchone()
                if row is not None and row[1] == sid and not json.loads(row[0]):
                    return
            conn.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (viewer, int(seen_through), json.dumps(sorted(open_list)), sid,
                          utc_stamp(_now_utc(store._clock)), gen))
            if block and open_list:
                conn.execute("INSERT OR IGNORE INTO band_view_blocks (viewer, gen, block) VALUES (?, ?, ?)",
                             (viewer, gen, block))
    except sqlite3.OperationalError as exc:
        if not _busy(exc):
            raise


_NEWEST_VIEW = "SELECT seq, seen_through, open_list, store_id FROM band_views WHERE viewer = ? ORDER BY gen DESC, seq DESC LIMIT 1"


def _newest_view(store: QuestionStore, viewer: str) -> tuple[View, int] | None:
    """``viewer``'s newest view (the greatest render generation) with its row id (``band_views.seq``, the VIEW ID a
    surface's answer names: append-only and never reused, so a different view is always a different id), or
    ``None`` (never shown a band, or no views table yet)."""
    with store._peek() as conn:
        if conn is None or "band_views" not in _tables(conn):
            return None
        row = conn.execute(_NEWEST_VIEW, (viewer,)).fetchone()
    if row is None:
        return None
    return View(int(row[1]), tuple(int(n) for n in json.loads(row[2])), row[3]), int(row[0])


def last_view(store: QuestionStore, viewer: str) -> View | None:
    """``viewer``'s newest view (the greatest render generation), or ``None`` (never shown a band, or no views table
    yet)."""
    got = _newest_view(store, viewer)
    return got[0] if got is not None else None


def _view_by_id(store: QuestionStore, viewer: str, view_id: int) -> View | None:
    """The view ``view_id`` names, if it is one of ``viewer``'s; ``None`` otherwise. Whether it is still the viewer's
    newest is not decided here: :func:`answer_text` decides that inside its write (:func:`_still_newest`)."""
    with store._peek() as conn:
        if conn is None or "band_views" not in _tables(conn):
            return None
        row = conn.execute("SELECT seen_through, open_list, store_id FROM band_views WHERE viewer = ? AND seq = ?",
                           (viewer, view_id)).fetchone()
    if row is None:
        return None
    return View(int(row[0]), tuple(int(n) for n in json.loads(row[1])), row[2])


def _letter_of(q: Question, key: str) -> str:
    return next((_LETTERS[i] for i, (k, _) in enumerate(q.options) if k == key), key)


def _items_of(store: QuestionStore, view: View) -> dict[int, Question]:
    """The questions behind the view's numbers (answered or not: the apply step tells a closed one from an open
    one). A number that is no question, or one that cannot be read, is absent: the parser refuses it."""
    return _items_read(store, view)[0]


def _items_read(store: QuestionStore, view: View) -> tuple[dict[int, Question], bool]:
    """:func:`_items_of`, and whether a question row could not be decoded (a corrupt row): that item is absent, but not
    known to be closed or gone, so a surface listing the band says it may be missing items."""
    out: dict[int, Question] = {}
    dropped = False
    with store._peek() as conn:
        if conn is None or "band_items" not in _tables(conn):
            return out, dropped
        for n in view.open_list:
            row = conn.execute("SELECT ref FROM band_items WHERE source = 'question' AND n = ?", (n,)).fetchone()
            if row is None:
                continue
            qrow = conn.execute(f"{_select(conn)} WHERE q.id = ?", (row[0],)).fetchone()
            if qrow is None:
                continue
            try:
                out[n] = store._question_of(qrow)
            except Exception:  # noqa: BLE001 - a corrupt row: absent for the parser, said by the band listing
                dropped = True
    return out, dropped


def _info_of(q: Question) -> ItemInfo:
    # a question with a Link is an authority item (design §5): shorthand records nothing on it (S3 routes it)
    rec = next((_LETTERS[i] for i, (k, _) in enumerate(q.options) if k == q.rec and i < len(_LETTERS)), None)
    return ItemInfo("authority" if q.link is not None else "information", min(len(q.options), len(_LETTERS)), rec)


def _extras_of(store: QuestionStore, view: View) -> tuple[dict[int, Extra], set[int]]:
    """The authority items behind the view's numbers that are open NOW, read fresh: one that has closed since (or whose
    source cannot be read) is absent, so shorthand on it opens nothing. Second: the view's authority numbers whose
    source could not be read in full just now, or whose record is still open but cannot be decoded (absent, but not known
    to be closed)."""
    with store._peek() as conn:
        if conn is None or "band_items" not in _tables(conn):
            return {}, set()
        refs = {n: (src, ref) for n in view.open_list for src, ref in [r for r in conn.execute(
            "SELECT source, ref FROM band_items WHERE n = ? AND source != 'question'", (n,))]}
    if not refs:
        return {}, set()
    try:
        now = read_extras(store.directory, store._clock)
    except Exception:  # noqa: BLE001 - every authority number of the view is then unknown, never "closed"
        return {}, set(refs)
    open_now = {e.band_key: e for e in now.items}
    got = {n: open_now[k] for n, k in refs.items() if k in open_now and open_now[k].unreadable is None}
    # unknown: its source was not read in full, OR it is still open but its record cannot be decoded (listed UNREADABLE,
    # absent from ``got``): not known closed, so the band says it may be missing an item. One that is simply gone (absent
    # from ``open_now`` under a fully-read source) has closed, and says nothing.
    return got, {n for n, k in refs.items() if n not in got and (k[0] not in now.complete or
                                                                  (k in open_now and open_now[k].unreadable is not None))}


def _info_of_extra(e: Extra) -> ItemInfo:
    return ItemInfo("authority", min(len(e.options), len(_LETTERS)), e.rec_letter)


def plan_for_full(store: QuestionStore, viewer: str, text: str
                  ) -> tuple[Parse, View | None, dict[int, Question], dict[int, Extra]]:
    """:func:`plan_for` plus the authority items (open now) the view's numbers name."""
    return _plan_on(store, last_view(store, viewer), text)


def _plan_on(store: QuestionStore, view: View | None, text: str
             ) -> tuple[Parse, View | None, dict[int, Question], dict[int, Extra]]:
    """The parser's plan for ``text`` bound to ``view``, with the questions and authority items its numbers name."""
    questions = _items_of(store, view) if view is not None else {}
    try:
        extras, unknown = _extras_of(store, view) if view is not None else ({}, set())
    except Exception:                        # unreadable sources: their numbers are simply not open for shorthand
        extras, unknown = {}, set()
    infos = {n: _info_of(q) for n, q in questions.items()}
    infos.update({n: _info_of_extra(e) for n, e in extras.items()})
    plan = parse(text, view, infos, _store_id_of(store))
    if unknown and any(r.n in unknown for r in plan.refused):
        plan = dataclasses.replace(plan, refused=tuple(
            Refusal(r.n, f"{r.n}: its source could not be read just now, so Levain cannot tell it is still open; "
                         "nothing opened (try again)") if r.n in unknown and "not open in the band" in r.reason
            else r for r in plan.refused))
    return plan, view, questions, extras


def plan_for(store: QuestionStore, viewer: str, text: str) -> tuple[Parse, View | None, dict[int, Question]]:
    """The parser's plan for ``text`` bound to ``viewer``'s last view, with the questions it names."""
    plan, view, questions, _extras = plan_for_full(store, viewer, text)
    return plan, view, questions


def _question_sheet_text(q: Question, letter: str) -> str:
    """The ceremony of a question with a Link: the phone's ``question_rule`` sheet. The tap on the linked option signs
    the ruling first; the answer is recorded only once it verified."""
    link = q.link
    label = dict(q.options).get(link.option, link.option) if link is not None else ""
    pick = q.options[_LETTERS.index(letter)][1] if _LETTERS.index(letter) < len(q.options) else letter
    only = "" if link is None or q.options[_LETTERS.index(letter)][0] == link.option else (
        f"; only {label!r} carries the signed ruling")
    return (f"open question #{q.n} on your phone and tap {pick!r}: the tap signs the ruling first and the answer is "
            f"recorded only once it verified{only}")


class Applied(NamedTuple):
    """What :func:`apply_plan` did: the labels recorded, the refusal texts, and the echo that says both. ``sheets``:
    the signing ceremonies opened for authority items (one line each); they record nothing."""
    recorded: list[str]
    refused: list[str]
    echo: str
    sheets: tuple[str, ...] = ()
    answered: tuple[str, ...] = ()      # the ids of the questions recorded (what a delivery is asked for)


def apply_plan(store: QuestionStore, plan: Parse, questions: dict[int, Question], *, viewer: str, via: str,
               seen_through: int, deadline: float | None = None, extras: dict[int, Extra] | None = None,
               channel: str = "cli", require: Callable[[sqlite3.Connection], None] | None = None) -> Applied:
    """Record ``plan.actions`` with their ``answer_context`` sidecars (via, viewer, the view's ``seen_through``, the
    note verbatim) through ONE :meth:`QuestionStore.answer_with_context` write (``channel``: ``cli`` by default, the
    human channel a terminal or a seat's typing is; the cockpit and the phone pass their own, S5a): an answer and its
    context are stored together or not at all, and the lock is
    waited for until ``deadline`` (``time.monotonic()``; the hook passes its own, ``None`` the ordinary wait). An
    item closed since the view, or refused by the store, is refused for that item alone; the lock busy at the
    deadline (or any store failure) records nothing and refuses each item. It never raises once it is called:
    the echo it returns always says what was recorded. EXCEPT a surface's answer (``require``, :func:`answer_text`):
    ``require`` runs inside the write transaction before any answer (the write is made even with nothing to record,
    so the check always runs), and whatever it raises, or any failure of the write itself (the lock busy, disk,
    corruption), is RAISED with nothing recorded, so the surface answers 409 or 503, never a per-item refusal.

    An authority item (``plan.sheets``, D8 SIG-ONLY) records NOTHING here: its existing signing ceremony is named with
    the pick preselected (``Applied.sheets``), and only if the item is still open now: each is read again here, at
    apply time, so an item closed since the plan was made opens nothing. No ruling, signature or answer is written
    for it by this function."""
    recorded: list[str] = []
    refused = [r.reason for r in plan.refused]
    sheets: list[str] = []
    fresh: dict[tuple[str, str], Extra] | None = None
    fresh_complete: set[str] = set()
    for sh in plan.sheets:
        try:
            q = questions.get(sh.n)
            e = (extras or {}).get(sh.n)
            if q is not None:
                q = store.get(q.id) or q                      # read again now: answered since the plan, or not
                if q.answer is not None:
                    refused.append(f"{sh.n} was already answered ({q.answer.channel}, {q.answer.answered_at}); "
                                   "nothing opened")
                elif q.link is None:
                    refused.append(f"{sh.n} is no longer a signing item; nothing opened")
                else:
                    sheets.append(f"{sh.label}: {_question_sheet_text(q, sh.pick)}")
            elif e is not None:
                if fresh is None:                             # once per apply, only when an authority item is named
                    try:
                        now = read_extras(store.directory, store._clock)
                    except Exception:                         # noqa: BLE001 - said below, per item
                        now = Extras(failed=["authority sources"])
                    fresh = {x.band_key: x for x in now.items if x.unreadable is None}
                    fresh_complete = now.complete
                now_e = fresh.get(e.band_key)
                if now_e is not None and (now_e.source, now_e.options, now_e.data.get("menu"),
                                          now_e.data.get("version")) != (e.source, e.options, e.data.get("menu"),
                                                                         e.data.get("version")):
                    refused.append(f"{sh.n} changed since the band you saw (its options are not the ones you picked "
                                   "from); nothing opened")
                elif now_e is not None:
                    try:
                        sheets.append(f"{sh.label}: {sheet_text(now_e, sh.pick, store.directory)}")
                    except NotOffered as no:
                        refused.append(f"{sh.n}: {no.why}; nothing opened")
                elif e.band_key[0] in fresh_complete:
                    refused.append(f"{sh.n} closed since the band you saw; nothing opened")
                else:
                    refused.append(f"{sh.n}: its source could not be read just now to check it is still open; "
                                   "nothing opened (try again)")
            else:
                refused.append(f"{sh.n} is no longer open; nothing opened")
        except Exception as exc:                              # one sheet's failure never costs the others
            refused.append(f"{sh.n}: the signing sheet could not be built ({type(exc).__name__}); nothing recorded")
    todo: list[tuple[Any, dict[str, Any]]] = []
    for act in plan.actions:
        try:
            q = questions.get(act.n)
            if q is None:
                raise ValueError(f"{act.n} was not open in the band you saw; nothing recorded for it")
            if q.answer is not None:                          # closed in the snapshot the plan was made from
                a = q.answer
                what = _letter_of(q, a.value) if a.kind == "option" else "text"
                refused.append(f"{act.n} was already answered ({what}, via {a.channel}, {a.answered_at})")
                continue
            entry: dict[str, Any] = {"ref": q.id, "via": via, "viewer": viewer, "seen_through": seen_through,
                                     "note": act.note}
            if act.text is not None:
                entry["text"] = act.text
            else:
                entry["option"] = q.options[_LETTERS.index(act.pick or "")][0]
            todo.append((act, entry))
        except Exception as exc:                              # one item's failure never costs the others
            refused.append(f"{act.n}: {exc}")
    ids: list[str] = []
    if todo or require is not None:
        try:
            results: list[Any] = store.answer_with_context([e for _, e in todo], channel=channel, schema=BAND_SCHEMA,
                                                           deadline=deadline, require=require)
        except Exception as exc:                              # rolled back whole: nothing of it was stored
            if require is not None:                           # a surface: 409 / 503, never a per-item refusal
                raise
            why = ("the question store stayed busy" if isinstance(exc, sqlite3.OperationalError) and _busy(exc)
                   else f"the question store failed ({type(exc).__name__})")
            results = [ValueError(f"{why}; nothing recorded for it")] * len(todo)
        for (act, entry), res in zip(todo, results):
            if isinstance(res, Exception):
                refused.append(f"{act.n}: {res}")
            else:
                recorded.append(act.label)
                ids.append(entry["ref"])
    try:
        echo = format_echo(recorded, refused, via, plan.hint, sheets)
    except Exception:                                         # what was recorded is said, whatever the formatting
        echo = "[Levain band answer] recorded: " + (", ".join(recorded) or "nothing")
    return Applied(recorded, refused, echo, tuple(sheets), tuple(ids))


def format_echo(recorded: list[str], refused: list[str], via: str, hint: str | None = None,
                sheets: list[str] | None = None) -> str:
    """The echo: exactly what was recorded and refused (``recorded: 1a (you, seat); refused: 16 ...``), and the
    signing sheets opened for authority items (``signature needed, nothing recorded: 16a: ...``)."""
    if hint and not recorded and not refused and not sheets:
        return f"[Levain band answer] {hint}."
    got = ", ".join(f"{label} (you, {via})" for label in recorded) or "nothing"
    line = f"[Levain band answer] recorded: {got}"
    if refused:
        line += "; refused: " + "; ".join(refused)
    if hint:
        line += f"; {hint}"
    if sheets:
        line += "; signature needed, nothing recorded (the item closes only when your signature verifies): " + "; ".join(sheets)
    return line


# --- S5a: the human surfaces (the cockpit and the phone) ---------------------------------------------------------------
def show(store: QuestionStore, viewer: str, deadline: float | None = None) -> Rendered:
    """A surface shows ``viewer`` the band (Q17-ALL S5a, decision A (c)): every open item is numbered on show (D3, the
    bounded write ``render_full`` already does), and the viewer's view is recorded only when its open list or
    high-water mark differs from its last one (:func:`record_view` ``dedupe``), so a poll loop grows nothing. An answer
    from this viewer binds to this view (D7). The Board is a status look, not an answer surface: it never calls this."""
    r = render_full(store, deadline=deadline)
    if _foreign(store):
        return r                              # nothing to record a view in: the foreign payload binds to the open list itself
    record_view(store, viewer, r.hwm, r.shown, deadline=deadline, gen=r.gen, dedupe=True)
    return r


_UNREAD_NOTE = "a decision source could not be read just now: the band may be missing items"


def band_payload(store: QuestionStore, viewer: str, deadline: float | None = None) -> dict[str, Any]:
    """What a surface lists of the band for ``viewer``: the binding an answer must carry back (``store_id`` and
    ``view_id``, the recorded view this listing is; ``seen_through`` is that view's ``band@`` mark, for display) and
    the numbered open items (``kind`` is ``information`` or ``authority``: shorthand records only the first; an
    authority item opens its existing signing sheet). Shows the band to ``viewer`` first (:func:`show`)."""
    shown = show(store, viewer, deadline)
    if _foreign(store):
        return _foreign_payload(store, shown)
    got = _newest_view(store, viewer)
    if got is None:                               # no view to bind to: which of two facts is it?
        if not shown.failed and _store_absent(store):   # no store file (or an empty one) AND every source read: nothing
            #                                       was ever filed, so nothing needed a view; a read-only look, the poll set nothing up
            return {"seen_through": None, "store_id": None, "view_id": None, "items": [], "store": "absent"}
        # a store that exists whose view could not be stored (a lock held, a write failing), or an authority source
        # that was not read (its "! not read" line is in the text): said, never drawn as health
        return {"seen_through": None, "store_id": None, "view_id": None, "items": [],
                "note": "the band could not be recorded just now"}
    view, view_id = got
    questions, dropped = _items_read(store, view)
    extras_failed = False
    try:
        extras, unknown = _extras_of(store, view)
        extras_failed = bool(unknown)         # an authority number whose source the second read could not read: omitted, not closed
    except Exception:  # noqa: BLE001 - the questions still list; the authority items are not, and the note says so
        extras, extras_failed = {}, True
    items: list[dict[str, Any]] = []
    for n in view.open_list:
        q, e = questions.get(n), extras.get(n)
        if q is not None and q.answer is None:
            info = _info_of(q)
            items.append({"n": n, "kind": info.kind, "tag": _tag(q), "what": q.what, "blocks": q.blocks,
                          "options": [{"letter": _LETTERS[i], "label": lab} for i, (_, lab) in enumerate(q.options)
                                      if i < len(_LETTERS)], "rec": info.rec_letter, "question": q.n})
        elif e is not None:
            items.append({"n": n, "kind": "authority", "tag": e.tag, "what": e.what, "blocks": e.blocks,
                          "options": [{"letter": _LETTERS[i], "label": lab} for i, lab in enumerate(e.options)
                                      if i < len(_LETTERS)], "rec": e.rec_letter, "question": None})
    # an item that was not read (a transient lock on queue.db: the first read or the second; a question row that cannot be
    # decoded): the items listed are the ones that were read, and the band says so, as the text render's "! not read" line
    # does: never a healthy band that is missing items
    return {"seen_through": view.seen_through, "store_id": view.store_id, "view_id": view_id, "items": items,
            "note": _UNREAD_NOTE if (shown.failed or extras_failed or dropped) else ""}


def _foreign_payload(store: Any, shown: Rendered) -> dict[str, Any]:
    """:func:`band_payload` for a foreign store: its open questions numbered by their own ``q.n``, bound to the fixed
    ``FOREIGN_STORE_ID`` / ``FOREIGN_VIEW_ID`` (no view is recorded in a store levain cannot write to; the answer
    re-reads the open set, so a question answered since is refused for itself)."""
    items = [{"n": q.n, "kind": "information", "tag": _tag(q), "what": q.what, "blocks": q.blocks,
              "options": [{"letter": _LETTERS[i], "label": lab} for i, (_, lab) in enumerate(q.options)
                          if i < len(_LETTERS)], "rec": _info_of(q).rec_letter, "question": q.n}
             for q in _foreign_open(store)]
    return {"seen_through": max((i["n"] for i in items), default=None), "store_id": FOREIGN_STORE_ID,
            "view_id": FOREIGN_VIEW_ID, "items": items, "note": _UNREAD_NOTE if shown.failed else ""}


def _foreign_answer(store: Any, text: str, *, channel: str) -> Applied:
    """:func:`answer_text` for a foreign store: the shorthand is parsed against the store's open questions now and each
    recorded through the store's OWN ``answer(ref, option=|text=, channel=)``, as the cockpit's ``question_answer`` verb
    does; no sidecar, no view. An item the store refuses is refused for itself."""
    questions = _foreign_by_n(store)
    view = View(max(questions, default=0), tuple(sorted(questions)), FOREIGN_STORE_ID)
    plan = parse(text, view, {n: _info_of(q) for n, q in questions.items()}, FOREIGN_STORE_ID)
    if not plan.matched or plan.rest.strip():
        raise ValueError(plan.hint or "the whole text must be band answers, e.g. `1a, 2: rec`; nothing was recorded")
    recorded: list[str] = []
    refused = [r.reason for r in plan.refused] + [f"{s.n}: this store has no signing sheet; nothing opened"
                                                  for s in plan.sheets]
    ids: list[str] = []
    for act in plan.actions:
        q = questions.get(act.n)
        try:
            if q is None:
                raise ValueError(f"{act.n} was not open in the band you saw; nothing recorded for it")
            if act.text is not None:
                store.answer(q.id, text=act.text, channel=channel)
            else:
                store.answer(q.id, option=q.options[_LETTERS.index(act.pick or "")][0], channel=channel)
        except Exception as exc:                  # one item's refusal never costs the others
            refused.append(f"{act.n}: {exc}")
        else:
            recorded.append(act.label)
            ids.append(q.id)
    try:
        echo = format_echo(recorded, refused, channel, plan.hint, [])
    except Exception:
        echo = "[Levain band answer] recorded: " + (", ".join(recorded) or "nothing")
    return Applied(recorded, refused, echo, (), tuple(ids))


class StaleBand(ValueError):
    """The band an answer names (``store_id``, ``view_id``) is not the one this viewer was last shown."""


_STALE = "the band you answered from is not the one last shown to you; reload it and answer again (nothing was recorded)"


def _still_newest(viewer: str, store_id: str, view_id: int) -> Callable[[sqlite3.Connection], None]:
    """The answer's view check, run INSIDE its write transaction (``BEGIN IMMEDIATE``): the store is ``store_id`` and
    ``view_id`` is still ``viewer``'s newest view. A view recorded after the answer was planned (a concurrent poll,
    another tab) makes the answer stale; it can never bind to a view the viewer was not answering from."""
    def check(conn: sqlite3.Connection) -> None:
        sid = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        top = conn.execute(_NEWEST_VIEW, (viewer,)).fetchone()
        if sid is None or sid[0] != store_id or top is None or int(top[0]) != view_id:
            raise StaleBand(_STALE)
    return check


def answer_text(store: QuestionStore, text: str, *, viewer: str, channel: str, store_id: str, view_id: int,
                deadline: float | None = None) -> Applied:
    """Record the shorthand ``text`` as an answer from a human surface (``channel`` is ``cockpit`` or ``phone``, the
    viewer ``cockpit:<credential>`` / ``phone:<device>``). Q1 RULED (a): "Information answers stay unsigned and are
    accepted only from human channels; agent-typed or relayed answers are refused and the item stays open." The
    surfaces call this only behind their own credential (the cockpit write token, the phone token or a paired
    device's), which is the human gate; this adds no second check, and an authority item records nothing (D8: it
    opens its sheet).

    The answer binds to the view it names (D7): ``store_id`` and ``view_id`` (:func:`band_payload`). The plan is made
    from THAT view, and the write records it only if, inside the same transaction, it is still this viewer's newest
    view in this store (:func:`_still_newest`); else :class:`StaleBand`, nothing recorded (a view never stored,
    because the lock was held when it was shown, names no id and is refused the same way). The whole text must parse
    as band answers, else ``ValueError``, nothing recorded. A store failure in the write (busy past the bound, disk,
    corruption) is raised as itself, nothing recorded: the surfaces answer 503. Items refused for themselves (closed
    since, not open in the view) come back in ``Applied.refused``."""
    if channel not in ("cockpit", "phone"):
        raise ValueError(f"a surface answers on the cockpit or the phone, not {channel!r}")
    if _foreign(store):
        if store_id != FOREIGN_STORE_ID or view_id != FOREIGN_VIEW_ID:
            raise StaleBand(_STALE)
        return _foreign_answer(store, text, channel=channel)
    view = _view_by_id(store, viewer, view_id)
    if view is None:                                  # no view of this viewer's to plan from; the store is checked below
        raise StaleBand(_STALE)
    plan, view, questions, extras = _plan_on(store, view, text)
    if not plan.matched or plan.rest.strip():
        raise ValueError(plan.hint or "the whole text must be band answers, e.g. `1a, 2: rec`; nothing was recorded")
    return apply_plan(store, plan, questions, viewer=viewer, via=channel, seen_through=view.seen_through,
                      deadline=deadline, extras=extras, channel=channel,
                      require=_still_newest(viewer, store_id, view_id))


def deliver_recorded(store: QuestionStore, done: Applied) -> None:
    """Deliver each answer ``done`` recorded to its agent's session, one after another (a daemon thread's body: a
    resume settles in seconds). A delivery error never undoes the answer: ``levain question deliver`` retries it."""
    from levain.consent import band

    for qid in done.answered:
        try:
            band.deliver_answer(store, qid)
        except Exception:  # noqa: BLE001 - the answer stands
            pass


def number_for_ping(store: QuestionStore, question_id: str) -> int | None:
    """The band number of question ``question_id``, allocated now if it has none (D3, decision B (a): the ping is
    the first time the operator sees the item, so it is a show), in one write bounded by HOOK_BUSY_MS. ``None`` when
    the lock stays busy or the write fails: the ping then carries the old ``#n`` text, and the number is taken at
    the next show. Never raises."""
    try:
        return _number(store, [question_id], time.monotonic() + HOOK_BUSY_MS / 1000).get(question_id)
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def _is_relay(prompt: str, environ: Mapping[str, str]) -> bool:
    """A turn Levain itself wrote into the seat: never parsed as an answer. ``LEVAIN_TURN=relay`` on the process,
    or the prompt is one of Levain's own lines (a band resume delivers ``levain: an answer was recorded ...``)."""
    return environ.get("LEVAIN_TURN") == "relay" or prompt.lstrip().startswith("levain:")


def _seat_answer(store: QuestionStore, viewer: str, prompt: Any, environ: Mapping[str, str],
                 deadline: float | None = None) -> str:
    """Parse the human's prompt text; on a full parse record it (via ``seat``, the lock waited for until
    ``deadline``) and return the echo. A prompt that is no answer line, a relay turn or an empty one: ``""``. Raises
    only before anything is recorded (a store that cannot be read); :func:`apply_plan` never raises."""
    if not isinstance(prompt, str) or not prompt.strip() or _is_relay(prompt, environ):
        return ""
    plan, view, questions, extras = plan_for_full(store, viewer, prompt)
    if not plan.matched:
        return format_echo([], [], "seat", plan.hint) if plan.hint else ""
    return apply_plan(store, plan, questions, viewer=viewer, via="seat",
                      seen_through=view.seen_through if view else 0, deadline=deadline, extras=extras).echo


def _delta_line(store: QuestionStore, prev: View | None, shown: list[int]) -> str:
    """The viewer's delta since its last view: new numbers, closed ones with how (D4 §4)."""
    if prev is None:
        return "first band on this seat"
    new = [n for n in shown if n > prev.seen_through or n not in prev.open_list]
    gone = [n for n in prev.open_list if n not in shown]
    if not new and not gone:
        return ""
    how: dict[int, str] = {}
    if gone:
        with store._peek() as conn:
            if conn is not None:
                for n in gone:
                    row = conn.execute("SELECT a.channel FROM band_items b JOIN answers a ON a.question_id = b.ref "
                                       "WHERE b.source = 'question' AND b.n = ?", (n,)).fetchone()
                    how[n] = f"answered via {row[0]}" if row else "gone"
    parts = []
    if new:
        parts.append("new " + ", ".join(str(n) for n in new))
    if gone:
        parts.append("closed " + ", ".join(f"{n} ({how.get(n, 'gone')})" for n in gone))
    return "since your last message: " + " · ".join(parts)


def is_autonomous(session_id: str, queue_dir: Path | str | None = None,
                  environ: Mapping[str, str] | None = None, busy_s: float | None = None) -> bool:
    """Whether no human turn can reach this seat (D4). The first fact found decides: (1) the Levain launch row of the
    harness session (a coordinator is attended; a worker is not: the only worker Levain launches is the dispatch
    executor, and the row carries no worker kind, so a ``session`` worker will need its kind read here when its
    brief lands); (2) the ``LEVAIN_SEAT=autonomous|attended`` marker Levain sets on the processes it starts (or the
    user on their own script); (3) neither: NOT autonomous (fail-safe: a band shown where nobody reads it costs tokens,
    a band missing from a human seat drops questions). Grants and posture never decide it. ``busy_s`` (default
    HOOK_BUSY_MS) bounds the wait on a held coordinator store; past it there is no launch fact."""
    if "#agent:" in session_id:
        return True                           # a subagent never renders: the band belongs to the session's main thread
    try:
        from levain.coordinator import lease
        from levain.coordinator.store import CoordStore
        conn = CoordStore(queue_dir).open_readonly(busy_s=HOOK_BUSY_MS / 1000 if busy_s is None else busy_s)
        try:
            ln = lease.by_harness_session(conn, session_id)
        finally:
            conn.close()
    except Exception:                         # no coordinator store, or not one to read: no launch fact
        ln = None
    if ln is not None and ln.role == "coordinator":
        return False
    if ln is not None and ln.role == "worker":
        return True
    env = os.environ if environ is None else environ
    return env.get("LEVAIN_SEAT") == "autonomous"      # "attended", anything else or unset: a seat a human reaches


def _seat_gets_band(payload: Any, environ: Mapping[str, str], deadline: float | None = None) -> bool:
    """A seat with no non-empty string ``session_id`` (``levain verify-hooks``, a malformed payload) is nobody's: no
    band, nothing consumed. ``deadline``: the hook's bound, which the launch-row read waits within."""
    from levain.consent.queue import queue_dir_for

    session = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session, str) or not session:
        return False
    busy = None if deadline is None else max(0.0, deadline - time.monotonic())
    return not is_autonomous(session, queue_dir_for(environ), environ, busy)


def _no_commit() -> None:
    return None


def hook_prompt(payload: Any, environ: Mapping[str, str] | None = None, agent: str = "claude_code") -> tuple[str, Callable[[], None]]:
    """The UserPromptSubmit hook's ``(text, commit)``: first the parse of the human's prompt (S2, D6: a full parse is
    recorded as the operator's via ``seat``; a relay turn is never parsed), then the render, with the echo of what was
    recorded and refused and the viewer's delta. Call ``commit()`` only once the text was DELIVERED: it records the
    ``closed`` notices as shown and the viewer's view row (D7). ``("", no-op)`` for an autonomous or id-less seat, an
    empty band or ANY error before anything was recorded; it never raises (``commit`` neither). A render that fails
    AFTER an answer was recorded still returns the echo: what was recorded is said."""
    t0 = time.monotonic()
    # ONE bound for the whole hook (S3 fix round 2, codex 4/5): the launch-row read, every snapshot (questions and
    # the authority sources), the answers' write, a first-time setup, the render's numbering, then the view row.
    # A store held past it is said ("! not read") or left pending; the prompt is never stalled behind it.
    deadline = t0 + HOOK_BUSY_MS / 1000
    with read_deadline(deadline):
        return _hook_prompt(payload, environ, deadline, agent)


def _hook_prompt(payload: Any, environ: Mapping[str, str] | None, deadline: float, agent: str = "claude_code"
                 ) -> tuple[str, Callable[[], None]]:
    try:
        from levain.consent.queue import queue_dir_for

        env = os.environ if environ is None else environ
        if not _seat_gets_band(payload, env, deadline):
            return "", _no_commit
        store = QuestionStore(queue_dir_for(env))
        viewer = f"{agent}:{payload['session_id']}"
    except Exception:
        return "", _no_commit
    echo = ""
    try:
        echo = _seat_answer(store, viewer, payload.get("prompt"), env, deadline)
    except Exception:                         # fail open before anything is recorded: the prompt passes untouched
        echo = ""
    try:
        prev = last_view(store, viewer)
        r = render_full(store, deadline=deadline)
        text = r.text
        if text:
            delta = _delta_line(store, prev, r.shown)
            if delta:
                head, _, body = text.partition("\n")
                text = head + "\n" + delta + ("\n" + body if body else "")
        if not text and prev is not None and prev.open_list:
            # Everything this viewer last saw closed (perhaps its notice consumed by another seat): say so and let the
            # empty view be committed, or the old view stays active and the Stop verifier demands obsolete items.
            delta = _delta_line(store, prev, r.shown)
            text = "NEEDS YOU · 0 open" + ("\n" + delta if delta else "")
    except Exception:
        return echo, _no_commit
    block = text                             # the band as the seat reads it (the delta line in): what Stop compares
    if echo:
        text = echo + ("\n" + text if text else "")

    def commit() -> None:
        try:
            mark_closed_shown(store, r.closed)
        except Exception:
            pass
        try:
            record_view(store, viewer, r.hwm, r.shown, deadline=deadline, gen=r.gen, block=block)
        except Exception:
            pass

    return text, commit


def hook_contract(payload: Any, environ: Mapping[str, str] | None = None, agent: str = "claude_code") -> str:
    """The SessionStart contract line (D5 layer 1); ``""`` for an autonomous or id-less seat or on any error."""
    try:
        return CONTRACT if _seat_gets_band(payload, os.environ if environ is None else environ) else ""
    except Exception:
        return ""


def _view_row(store: QuestionStore, viewer: str) -> tuple[View, int, str | None] | None:
    """``viewer``'s newest view with its generation and rendered block (``None``: an older view, kept without one), all
    from ONE read snapshot: the list checked and the generation claimed are the same view's."""
    with store._peek() as conn:
        if conn is None:
            return None
        have = _tables(conn)
        if "band_views" not in have:
            return None
        row = conn.execute("SELECT seen_through, open_list, store_id, gen FROM band_views WHERE viewer = ? "
                           "ORDER BY gen DESC, seq DESC LIMIT 1", (viewer,)).fetchone()
        if row is None:
            return None
        block = None
        if "band_view_blocks" in have:
            got = conn.execute("SELECT block FROM band_view_blocks WHERE viewer = ? AND gen = ?",
                               (viewer, row[3])).fetchone()
            block = got[0] if got else None
    return View(int(row[0]), tuple(int(n) for n in json.loads(row[1])), row[2]), int(row[3]), block


def _claim_file(directory: Path | str, viewer: str) -> Path:
    import hashlib

    return Path(directory) / "band_stop" / hashlib.sha256(viewer.encode()).hexdigest()[:24]


def _claim_path(directory: Path | str, viewer: str, gen: int) -> Path:
    f = _claim_file(directory, viewer)
    return f.with_name(f"{f.name}.{int(gen)}")


def _claimed(directory: Path | str, viewer: str, gen: int) -> bool:
    """Read-only: whether ``viewer``'s claim for ``gen`` is already taken (False when it cannot be read)."""
    try:
        return _claim_path(directory, viewer, gen).exists()
    except Exception:
        return False


def _claim_retry(directory: Path | str, viewer: str, gen: int) -> bool:
    """The verifier's second bound beside ``stop_hook_active``: one block per view (render generation). True when
    this call took the claim (nothing was blocked for this view yet); False when one was, or on any error (fail open:
    an unwritable claim is no licence to block). The claim is a file keyed by (viewer, gen) made with exclusive
    creation: of any number of concurrent claimants exactly one succeeds, and a claim of another generation is a
    different file, so an old-generation claimant can never overwrite a newer one."""
    try:
        f = _claim_path(directory, viewer, gen)
        f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.close(os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return False
        return True
    except Exception:
        return False


def _norm(line: str) -> str:
    """A reply's band line as compared: markdown quote/list lead and spacing ignored, nothing else."""
    import re

    return re.sub(r"\s+", " ", re.sub(r"^[ \t>*-]+", "", line)).strip()


def _reprint_check(view: View, block: str | None, message: str) -> tuple[list[int], bool]:
    """``(item numbers the reply lacks, whether the band as a whole is not reprinted)`` against the view. With the
    rendered ``block``: the header line must appear as a line, and every following block line, in the recorded order,
    contiguous (blank lines and markdown lead aside). Without one (a view kept before blocks were): the ``band@N`` header (digit-bounded) and each shown
    number as an item line."""
    import re

    lines = [_norm(ln) for ln in message.splitlines()]
    lines = [ln for ln in lines if ln]
    if block:
        want = [_norm(ln) for ln in block.splitlines() if ln.strip()]
        have = set(lines)
        missing = [n for n in view.open_list if not any(w.startswith(f"{n}. ") and w in have for w in want)]
        for i, ln in enumerate(lines):
            if ln != want[0]:
                continue
            if lines[i + 1:i + len(want)] == want[1:]:
                return [], False
        return missing, True
    missing = [n for n in view.open_list if not any(re.match(rf"{n}\.(\s|$)", ln) for ln in lines)]
    head = not re.search(rf"band@{view.seen_through}(?!\d)", message)
    return missing, head


def hook_stop(payload: Any, environ: Mapping[str, str] | None = None, agent: str = "claude_code") -> str | None:
    """The Stop hook's verifier (D5 layer 3, Claude Code and Codex alike): ``None`` to let the turn end, or the reason
    to block it once because the reply did not carry the band shown to this seat. The check is against this seat's last
    view (what the UserPromptSubmit hook delivered): the reply must reprint the rendered band, header line and every
    item line in order (:func:`_reprint_check`). BOUNDED: never when the harness says the stop is already a
    continuation (``stop_hook_active``), at most one block per view (:func:`_claim_retry`), and FAIL OPEN on anything
    unreadable (no view, a non-text reply, an autonomous seat, a busy or missing store). Never raises, never writes the
    operator's data."""
    try:
        from levain.consent.queue import queue_dir_for

        if not isinstance(payload, dict) or payload.get("stop_hook_active"):
            return None
        message = payload.get("last_assistant_message")
        if not isinstance(message, str):
            return None
        env = os.environ if environ is None else environ
        if not _seat_gets_band(payload, env):
            return None
        directory = queue_dir_for(env)
        store = QuestionStore(directory)
        viewer = f"{agent}:{payload['session_id']}"
        got = _view_row(store, viewer)
        if got is None:
            return None
        view, gen, block = got
        if not view.open_list:
            return None
        missing, altered = _reprint_check(view, block, message)
        if not missing and not altered:
            return None
        if not _claim_retry(directory, viewer, gen):
            return None
        what = ("the items " + ", ".join(str(n) for n in missing)) if missing else "the band as given (header line or items altered)"
        return (f"[Levain NEEDS YOU band] Your reply dropped {what} of the NEEDS YOU band you were given. Reprint the "
                "whole band once, exactly as Levain gave it (the band@ line and every numbered item, unchanged), at "
                "the end of your reply. Do not answer or renumber any item.")
    except Exception:
        return None


BAND_MARK = "levain.band_view"


def band_hooks_state(hooks_dir: Path | str, agent: str = "claude_code") -> tuple[str, list[str]]:
    """``(state, scripts lacking the band)`` of an install's ``activation/hooks``: ``wired`` (every required script
    calls :mod:`levain.band_view`), ``stale`` (some do: an older copy beside a newer one; ``levain update`` fixes it)
    or ``absent`` (none, or no directory). Required, for Claude Code and Codex alike: SessionStart, UserPromptSubmit and
    the Stop verifier (a Stop hook of either can block once with ``{"decision": "block", "reason": ...}``)."""
    names = ["session_start.py", "user_prompt_submit.py", "stop.py"]
    lacking = []
    for n in names:
        try:
            has = BAND_MARK in (Path(hooks_dir) / n).read_text(encoding="utf-8", errors="replace")
        except OSError:
            has = False
        if not has:
            lacking.append(n)
    return ("wired" if not lacking else "absent" if len(lacking) == len(names) else "stale"), lacking


def _codex_hooks_dir(environ: Mapping[str, str], cwd: str | None) -> Path | None:
    """The ``activation/hooks`` directory Codex's own ``hooks.json`` names for UserPromptSubmit: ``$CODEX_HOME`` (or
    ``~/.codex``), then a ``.codex/hooks.json`` of the working directory or a parent. ``None``: none names one."""
    import shlex

    home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    files = [home / "hooks.json"]
    if cwd:
        p = Path(cwd)
        files.extend(c / ".codex" / "hooks.json" for c in (p, *p.parents))
    for f in files:
        try:
            tree = json.loads(f.read_text(encoding="utf-8"))
            for entry in (tree.get("hooks") or {}).get("UserPromptSubmit") or []:
                for h in entry.get("hooks") or []:
                    for tok in shlex.split(str(h.get("command", ""))):
                        if tok.endswith("user_prompt_submit.py"):
                            return Path(tok).parent
        except Exception:
            continue
    return None


def _find_hooks_dir(agent: str, cwd: str | None, environ: Mapping[str, str]) -> Path | None:
    """The ``activation/hooks`` directory a seat's hooks run from, found as the install is: Claude Code's
    ``CLAUDE_PROJECT_DIR`` or the working directory and its parents; Codex's own hooks.json (never a neighbouring
    Claude install: those scripts do not run in Codex)."""
    if agent == "codex":
        return _codex_hooks_dir(environ, cwd)
    cands: list[Path] = []
    if environ.get("CLAUDE_PROJECT_DIR"):
        cands.append(Path(environ["CLAUDE_PROJECT_DIR"]))
    if cwd:
        p = Path(cwd)
        cands.extend([p, *p.parents])
    for c in cands:
        if (c / "activation" / "hooks" / "user_prompt_submit.py").is_file():
            return c / "activation" / "hooks"
    return None


def gate_notice(directory: Path | str, agent: str, session: str | None, cwd: str | None,
                environ: Mapping[str, str] | None = None, deliverable: bool = True) -> str | None:
    """The gate's fallback line (D5): a seat with the consent gate but no band hook gets, ONCE per session, one line in
    the gate's ``additionalContext`` saying N items need the operator (questions and authority items) and the band is
    not installed here. ``None`` when the hooks are wired, nothing is open, the seat is autonomous, the line was
    already given, ``deliverable`` is False (the gate ends the turn: nothing is claimed for a line that may never be
    read), or on any error (the gate never fails for it). The claim is checked first (a cheap read) and taken last."""
    try:
        env = os.environ if environ is None else environ
        if not deliverable or not isinstance(session, str) or not session or "#agent:" in session:
            return None
        viewer = f"gate:{agent}:{session}"
        if _claimed(directory, viewer, 1):
            return None
        if is_autonomous(session, directory, env):
            return None
        hooks = _find_hooks_dir(agent, cwd, env)
        state = band_hooks_state(hooks, agent)[0] if hooks is not None else "absent"
        if state == "wired":
            return None
        store = QuestionStore(directory)
        items, _, _ = _read(store)
        n = len(items)
        partial = False
        try:
            extras = read_extras(store.directory, store._clock)
            n += len(extras.items)
            partial = bool(extras.failed)
        except Exception:                     # an unreadable authority source: the questions still count, said so
            partial = True
        if n == 0:
            return None
        if not _claim_retry(directory, viewer, 1):
            return None
        count = f"at least {n}" if partial else f"{n}"
        plural = n != 1 or partial
        return (f"[Levain] {count} item{'s' if plural else ''} need{'' if plural else 's'} the operator; the NEEDS YOU "
                f"band is not installed on this seat (hooks {state}: `levain update`). Tell the operator so in your "
                "reply and point to `levain band`.")
    except Exception:
        return None


def run_band(args: argparse.Namespace) -> int:
    """``levain band``: print the band as a seat would see it, consuming no ``closed``. It records the viewer ``cli``'s
    view (what ``levain band answer`` binds to)."""
    try:
        store = QuestionStore(args.queue_dir)
        r = render_full(store)
    except Exception as exc:                  # a store that cannot be trusted: said, not hidden
        print(f"levain band: {exc}", file=sys.stderr)
        return 1
    print(r.text or "NEEDS YOU · nothing open")
    try:
        record_view(store, "cli", r.hwm, r.shown, gen=r.gen)
    except Exception:
        pass
    return 0


def run_band_answer(args: argparse.Namespace) -> int:
    """``levain band answer "<line>"``: the same parser and path as the seat hook, channel ``cli`` and via ``cli``,
    bound to the view ``levain band`` last printed. Refused inside any agent session (Q1 (a)); the whole line must
    parse; a line that does not records nothing."""
    from levain.consent.band import AGENT_ANSWER_REFUSAL, EXIT_REFUSED, agent_session_caller

    if agent_session_caller():
        print(f"levain band answer: refused: {AGENT_ANSWER_REFUSAL}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        store = QuestionStore(args.queue_dir)
        plan, view, questions, extras = plan_for_full(store, "cli", args.line)
        if not plan.matched or plan.rest.strip():
            why = plan.hint or "the whole line must be band answers, e.g. `1a, 2: rec`"
            print(f"levain band answer: refused: {why}; nothing was recorded", file=sys.stderr)
            return EXIT_REFUSED
    except Exception as exc:
        print(f"levain band answer: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    done = apply_plan(store, plan, questions, viewer="cli", via="cli", seen_through=view.seen_through if view else 0,
                      extras=extras)
    print(done.echo.replace("[Levain band answer] ", "levain band answer: "))
    return 0 if done.recorded or done.sheets else EXIT_REFUSED


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("band", help="Print the NEEDS YOU band: what waits on you, numbered.")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent directory holding questions.db (default: the levain home's).")
    p.set_defaults(func=run_band)
    sub = p.add_subparsers(dest="band_cmd", required=False)
    a = sub.add_parser("answer", help="Answer band items in shorthand (\"1a, 2: rec\"), from a terminal outside any "
                                      "agent session.")
    a.add_argument("line", help="The shorthand line, e.g. \"1a, 2: rec, all recs\".")
    a.add_argument("--queue-dir", dest="queue_dir", type=Path, default=argparse.SUPPRESS,
                   help="The consent directory holding questions.db.")
    a.set_defaults(func=run_band_answer)
