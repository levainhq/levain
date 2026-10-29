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
from levain.band_sources import Extra, Extras, read_extras, sheet_text
from levain.cockpit.consent import utc_stamp
from levain.consent.questions import HOOK_BUSY_MS, Question, QuestionStore, _now_utc, _select

__all__ = ["BAND_SCHEMA", "COMPACT_AFTER", "Applied", "apply_plan", "format_echo", "hook_contract", "hook_prompt",
           "is_autonomous", "last_view", "mark_closed_shown", "plan_for", "plan_for_full", "record_view", "render", "render_full", "render_with_closed",
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


def _number(store: QuestionStore, refs: list[Any], deadline: float) -> dict[Any, int]:
    """Number every ``refs`` that has no number yet (in the order given), in ONE write transaction waited for until
    ``deadline``. A ref is a question id, or ``(source, ref)`` for another source; the result is keyed as given. The numbers come back read under the lock, so two writers never number a ref twice. The lock still
    busy at the deadline: nothing written, nothing returned (``new (number pending)``); any other error is raised."""
    try:
        if not store._ready:                  # no question was ever filed here: the store is set up for the first number
            _ = store.store_id
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
    start, ``time.time_ns()``: what orders two views of one viewer, whatever order they are committed in)."""
    text: str
    closed: list[int]
    shown: list[int]
    hwm: int
    gen: int


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
    still = {(e.source, e.ref) for e in extras.items}
    closed = [(n, ref) for n, src, ref in rows
              if src in extras.complete and (src, ref) not in still and n not in shown_closed]
    return numbers, closed


def _extra_lines(label: str, e: Extra, compact: bool) -> list[str]:
    if e.unreadable:
        return [f"{label} [{e.tag}] {e.what} (listed, never dropped)"]
    if compact:
        return [f"{label} [{e.tag}] {_short(e.what, _COMPACT_WHAT)} · rec: {e.rec}"]
    opts = "   ".join(f"{_LETTERS[i]}) {lab}" for i, lab in enumerate(e.options[:len(_LETTERS)]))
    return [f"{label} [{e.tag}] {e.what}   <- needs your signature",
            f"    {opts}",
            f"    rec: {e.rec} · blocks: {e.blocks} · {e.origin}"]


def render_full(store: QuestionStore | None = None) -> Rendered:
    """:func:`render_with_closed` plus what the view row needs (:class:`Rendered`)."""
    store = store if store is not None else QuestionStore()
    gen = time.time_ns()                      # before the snapshot: a later render has a greater gen
    deadline = time.monotonic() + HOOK_BUSY_MS / 1000
    items, numbers, closed = _read(store)
    try:
        extras = read_extras(store.directory, store._clock)
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
        entries.append(((e.source, e.ref), e))
        filed[(e.source, e.ref)] = e.filed
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
    return Rendered("\n".join(lines), [n for n, _ in closed_all], shown, hwm, gen)


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


def record_view(store: QuestionStore, viewer: str, seen_through: int, open_list: list[int],
                deadline: float | None = None, gen: int | None = None) -> None:
    """Store what ``viewer`` was just shown (D7), in one bounded write (by ``deadline``, ``time.monotonic()``; the hook
    passes its own start + the bound, so a busy store costs the hook one bound in all). ``gen``: the render's
    generation (:class:`Rendered`; default now); the viewer's last view is the greatest gen, so a render committed
    late never replaces a newer one. A view with nothing open is not written when the viewer's last view already had
    nothing open (no row per empty prompt). A busy lock records nothing (the viewer's next answer is then refused
    as bound to an older view or to none: safe, never wrong)."""
    sid = _store_id_of(store)
    if sid is None:
        return
    if not open_list:
        prev = last_view(store, viewer)
        if prev is not None and prev.store_id == sid and not prev.open_list:
            return
    try:
        with store._write_by(deadline if deadline is not None else time.monotonic() + HOOK_BUSY_MS / 1000) as conn:
            for stmt in BAND_SCHEMA:
                conn.execute(stmt)
            conn.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at, gen) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (viewer, int(seen_through), json.dumps(sorted(open_list)), sid,
                          utc_stamp(_now_utc(store._clock)), int(gen if gen is not None else time.time_ns())))
    except sqlite3.OperationalError as exc:
        if not _busy(exc):
            raise


def last_view(store: QuestionStore, viewer: str) -> View | None:
    """``viewer``'s newest view (the greatest render generation), or ``None`` (never shown a band, or no views table
    yet)."""
    with store._peek() as conn:
        if conn is None or "band_views" not in _tables(conn):
            return None
        row = conn.execute("SELECT seen_through, open_list, store_id FROM band_views WHERE viewer = ? "
                           "ORDER BY gen DESC, seq DESC LIMIT 1", (viewer,)).fetchone()
    if row is None:
        return None
    return View(int(row[0]), tuple(int(n) for n in json.loads(row[1])), row[2])


def _letter_of(q: Question, key: str) -> str:
    return next((_LETTERS[i] for i, (k, _) in enumerate(q.options) if k == key), key)


def _items_of(store: QuestionStore, view: View) -> dict[int, Question]:
    """The questions behind the view's numbers (answered or not: the apply step tells a closed one from an open
    one). A number that is no question, or one that cannot be read, is absent: the parser refuses it."""
    out: dict[int, Question] = {}
    with store._peek() as conn:
        if conn is None or "band_items" not in _tables(conn):
            return out
        for n in view.open_list:
            row = conn.execute("SELECT ref FROM band_items WHERE source = 'question' AND n = ?", (n,)).fetchone()
            if row is None:
                continue
            qrow = conn.execute(f"{_select(conn)} WHERE q.id = ?", (row[0],)).fetchone()
            if qrow is None:
                continue
            try:
                out[n] = store._question_of(qrow)
            except Exception:
                continue
    return out


def _info_of(q: Question) -> ItemInfo:
    # a question with a Link is an authority item (design §5): shorthand records nothing on it (S3 routes it)
    rec = next((_LETTERS[i] for i, (k, _) in enumerate(q.options) if k == q.rec and i < len(_LETTERS)), None)
    return ItemInfo("authority" if q.link is not None else "information", min(len(q.options), len(_LETTERS)), rec)


def _extras_of(store: QuestionStore, view: View) -> tuple[dict[int, Extra], set[int]]:
    """The authority items behind the view's numbers that are open NOW, read fresh: one that has closed since (or whose
    source cannot be read) is absent, so shorthand on it opens nothing. Second: the view's authority numbers whose
    source could not be read in full just now (absent, but not known to be closed)."""
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
    open_now = {(e.source, e.ref): e for e in now.items}
    got = {n: open_now[k] for n, k in refs.items() if k in open_now and open_now[k].unreadable is None}
    return got, {n for n, (src, _ref) in refs.items() if n not in got and src not in now.complete}


def _info_of_extra(e: Extra) -> ItemInfo:
    return ItemInfo("authority", min(len(e.options), len(_LETTERS)), e.rec_letter)


def plan_for_full(store: QuestionStore, viewer: str, text: str
                  ) -> tuple[Parse, View | None, dict[int, Question], dict[int, Extra]]:
    """:func:`plan_for` plus the authority items (open now) the view's numbers name."""
    view = last_view(store, viewer)
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


def apply_plan(store: QuestionStore, plan: Parse, questions: dict[int, Question], *, viewer: str, via: str,
               seen_through: int, deadline: float | None = None, extras: dict[int, Extra] | None = None) -> Applied:
    """Record ``plan.actions`` with their ``answer_context`` sidecars (via, viewer, the view's ``seen_through``, the
    note verbatim) through ONE :meth:`QuestionStore.answer_with_context` write (channel ``cli``, the human channel a
    terminal or a seat's typing is): an answer and its context are stored together or not at all, and the lock is
    waited for until ``deadline`` (``time.monotonic()``; the hook passes its own, ``None`` the ordinary wait). An
    item closed since the view, or refused by the store, is refused for that item alone; the lock busy at the
    deadline (or any store failure) records nothing and refuses each item. It never raises once it is called:
    the echo it returns always says what was recorded.

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
                    fresh = {(x.source, x.ref): x for x in now.items if x.unreadable is None}
                    fresh_complete = now.complete
                now_e = fresh.get((e.source, e.ref))
                if now_e is not None:
                    sheets.append(f"{sh.label}: {sheet_text(now_e, sh.pick, store.directory)}")
                elif e.source in fresh_complete:
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
    if todo:
        try:
            results: list[Any] = store.answer_with_context([e for _, e in todo], channel="cli", schema=BAND_SCHEMA,
                                                           deadline=deadline)
        except Exception as exc:                              # rolled back whole: nothing of it was stored
            why = ("the question store stayed busy" if isinstance(exc, sqlite3.OperationalError) and _busy(exc)
                   else f"the question store failed ({type(exc).__name__})")
            results = [ValueError(f"{why}; nothing recorded for it")] * len(todo)
        for (act, _), res in zip(todo, results):
            if isinstance(res, Exception):
                refused.append(f"{act.n}: {res}")
            else:
                recorded.append(act.label)
    try:
        echo = format_echo(recorded, refused, via, plan.hint, sheets)
    except Exception:                                         # what was recorded is said, whatever the formatting
        echo = "[Levain band answer] recorded: " + (", ".join(recorded) or "nothing")
    return Applied(recorded, refused, echo, tuple(sheets))


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
    """The UserPromptSubmit hook's ``(text, commit)``: first the parse of the human's prompt (S2, D6: a full parse is
    recorded as the operator's via ``seat``; a relay turn is never parsed), then the render, with the echo of what was
    recorded and refused and the viewer's delta. Call ``commit()`` only once the text was DELIVERED: it records the
    ``closed`` notices as shown and the viewer's view row (D7). ``("", no-op)`` for an autonomous or id-less seat, an
    empty band or ANY error before anything was recorded; it never raises (``commit`` neither). A render that fails
    AFTER an answer was recorded still returns the echo: what was recorded is said."""
    t0 = time.monotonic()
    try:
        from levain.consent.queue import queue_dir_for

        env = os.environ if environ is None else environ
        if not _seat_gets_band(payload, env):
            return "", _no_commit
        store = QuestionStore(queue_dir_for(env))
        viewer = f"claude_code:{payload['session_id']}"
    except Exception:
        return "", _no_commit
    deadline = t0 + HOOK_BUSY_MS / 1000       # ONE bound for the hook's writes: the answers, then the view row
    echo = ""
    try:
        echo = _seat_answer(store, viewer, payload.get("prompt"), env, deadline)
    except Exception:                         # fail open before anything is recorded: the prompt passes untouched
        echo = ""
    try:
        prev = last_view(store, viewer)
        r = render_full(store)
        text = r.text
        if text:
            delta = _delta_line(store, prev, r.shown)
            if delta:
                head, _, body = text.partition("\n")
                text = head + "\n" + delta + ("\n" + body if body else "")
    except Exception:
        return echo, _no_commit
    if echo:
        text = echo + ("\n" + text if text else "")

    def commit() -> None:
        try:
            mark_closed_shown(store, r.closed)
        except Exception:
            pass
        try:
            record_view(store, viewer, r.hwm, r.shown, deadline=deadline, gen=r.gen)
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
