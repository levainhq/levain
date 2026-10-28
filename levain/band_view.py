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
human's prompt against the viewer's last view, records a full parse through ``QuestionStore.answer`` (channel ``cli``,
``answer_context.via = 'seat'``, a note kept verbatim) and echoes ``recorded: ... refused: ...`` plus the viewer's delta
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
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from levain.band_shorthand import ItemInfo, Parse, View, parse
from levain.cockpit.consent import utc_stamp
from levain.consent.questions import HOOK_BUSY_MS, Question, QuestionStore, _now_utc, _select

__all__ = ["BAND_SCHEMA", "COMPACT_AFTER", "apply_plan", "format_echo", "hook_contract", "hook_prompt", "is_autonomous",
           "last_view", "mark_closed_shown", "plan_for", "record_view", "render", "render_full", "render_with_closed",
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
    # S2 (D7): each render's view: what a viewer was shown, so an answer binds to it
    "CREATE TABLE IF NOT EXISTS band_views (seq INTEGER PRIMARY KEY AUTOINCREMENT, viewer TEXT NOT NULL, "
    "seen_through INTEGER NOT NULL, open_list TEXT NOT NULL, store_id TEXT NOT NULL, at TEXT NOT NULL)",
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


class Rendered(NamedTuple):
    """One render: the text, the ``closed`` band numbers it carries, the band numbers of the open items shown with a
    number, and the header's ``band@`` high-water mark (0 when nothing was numbered)."""
    text: str
    closed: list[int]
    shown: list[int]
    hwm: int


def render_with_closed(store: QuestionStore | None = None) -> tuple[str, list[int]]:
    """The band as text (the whole of it, every time; ``""`` when nothing is open and nothing closed is due) and the
    band numbers of the ``closed`` notices it carries, consumed by nobody: the caller that DELIVERED the text hands
    them to :func:`mark_closed_shown`. Showing an item numbers it (first show, one bounded write). Raises on a store
    that cannot be trusted: the hook fails open on it."""
    r = render_full(store)
    return r.text, r.closed


def render_full(store: QuestionStore | None = None) -> Rendered:
    """:func:`render_with_closed` plus what the view row needs (:class:`Rendered`)."""
    store = store if store is not None else QuestionStore()
    deadline = time.monotonic() + HOOK_BUSY_MS / 1000
    items, numbers, closed = _read(store)
    refs = [i for i, _ in items if i not in numbers]
    if refs:
        numbers = {**numbers, **_number(store, refs, deadline)}
    if not items and not closed:
        return Rendered("", [], [], 0)
    shown = [numbers[i] for i, _ in items if i in numbers]
    hwm = max([*shown, *(n for n, _ in closed)], default=0)
    head = [f"NEEDS YOU · {len(items)} open"]
    if shown or closed:
        head.append(f"band@{hwm}")
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
    return Rendered("\n".join(lines), [n for n, _ in closed], shown, hwm)


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
                deadline: float | None = None) -> None:
    """Store what ``viewer`` was just shown (D7), in one bounded write (by ``deadline``, ``time.monotonic()``; the hook
    passes its own start + the bound, so a busy store costs the hook one bound in all). A busy lock records nothing
    (the viewer's next answer is then refused as bound to an older view or to none: safe, never wrong)."""
    sid = _store_id_of(store)
    if sid is None:
        return
    try:
        with store._write_by(deadline if deadline is not None else time.monotonic() + HOOK_BUSY_MS / 1000) as conn:
            for stmt in BAND_SCHEMA:
                conn.execute(stmt)
            conn.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at) VALUES (?, ?, ?, ?, ?)",
                         (viewer, int(seen_through), json.dumps(sorted(open_list)), sid,
                          utc_stamp(_now_utc(store._clock))))
    except sqlite3.OperationalError as exc:
        if not _busy(exc):
            raise


def last_view(store: QuestionStore, viewer: str) -> View | None:
    """``viewer``'s most recent view, or ``None`` (never shown a band, or no views table yet)."""
    with store._peek() as conn:
        if conn is None or "band_views" not in _tables(conn):
            return None
        row = conn.execute("SELECT seen_through, open_list, store_id FROM band_views WHERE viewer = ? "
                           "ORDER BY seq DESC LIMIT 1", (viewer,)).fetchone()
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


def plan_for(store: QuestionStore, viewer: str, text: str) -> tuple[Parse, View | None, dict[int, Question]]:
    """The parser's plan for ``text`` bound to ``viewer``'s last view, with the questions it names."""
    view = last_view(store, viewer)
    questions = _items_of(store, view) if view is not None else {}
    plan = parse(text, view, {n: _info_of(q) for n, q in questions.items()}, _store_id_of(store))
    return plan, view, questions


def apply_plan(store: QuestionStore, plan: Parse, questions: dict[int, Question], *, viewer: str, via: str,
               seen_through: int) -> tuple[list[str], list[str]]:
    """Record ``plan.actions`` through the question store's own ``answer`` (channel ``cli``, the human channel a
    terminal or a seat's typing is) and write each one's ``answer_context`` sidecar (via, viewer, the view's
    ``seen_through``, the note verbatim). Returns ``(recorded labels, refusal texts)`` (the parser's refusals first).
    An item closed since the view, or an error on one item, is refused for that item alone."""
    recorded: list[str] = []
    refused = [r.reason for r in plan.refused]
    for act in plan.actions:
        q = questions.get(act.n)
        try:
            if q is None:
                raise ValueError(f"{act.n} was not open in the band you saw; nothing recorded for it")
            now = store.get(q.id)
            if now is not None and now.answer is not None:
                a = now.answer
                what = _letter_of(now, a.value) if a.kind == "option" else "text"
                refused.append(f"{act.n} was already answered ({what}, via {a.channel}, {a.answered_at})")
                continue
            if act.text is not None:
                store.answer(q.id, text=act.text, channel="cli")
            else:
                key = q.options["abcdef".index(act.pick or "")][0]
                store.answer(q.id, option=key, channel="cli")
        except Exception as exc:                              # one item's failure never costs the others
            refused.append(f"{act.n}: {exc}")
            continue
        recorded.append(act.label)
        try:
            with store._write() as conn:
                for stmt in BAND_SCHEMA:
                    conn.execute(stmt)
                conn.execute("INSERT INTO answer_context (question_id, via, viewer, seen_through, note) "
                             "VALUES (?, ?, ?, ?, ?)", (q.id, via, viewer, seen_through, act.note))
        except Exception as exc:                              # the answer stands; only its context is lost
            refused.append(f"{act.n}: recorded, but its context could not be saved ({type(exc).__name__})")
    return recorded, refused


def format_echo(recorded: list[str], refused: list[str], via: str, hint: str | None = None) -> str:
    """The echo: exactly what was recorded and refused (``recorded: 1a (you, seat); refused: 16 ...``)."""
    if hint and not recorded and not refused:
        return f"[Levain band answer] {hint}."
    got = ", ".join(f"{label} (you, {via})" for label in recorded) or "nothing"
    line = f"[Levain band answer] recorded: {got}"
    if refused:
        line += "; refused: " + "; ".join(refused)
    if hint:
        line += f"; {hint}"
    return line


def _is_relay(prompt: str, environ: Mapping[str, str]) -> bool:
    """A turn Levain itself wrote into the seat: never parsed as an answer. ``LEVAIN_TURN=relay`` on the process,
    or the prompt is one of Levain's own lines (a band resume delivers ``levain: an answer was recorded ...``)."""
    return environ.get("LEVAIN_TURN") == "relay" or prompt.lstrip().startswith("levain:")


def _seat_answer(store: QuestionStore, viewer: str, prompt: Any, environ: Mapping[str, str]) -> str:
    """Parse the human's prompt text; on a full parse record it (via ``seat``) and return the echo. A prompt that
    is no answer line, a relay turn or an empty one: ``""``. Raises on a store that cannot be read (nothing is then
    recorded)."""
    if not isinstance(prompt, str) or not prompt.strip() or _is_relay(prompt, environ):
        return ""
    plan, view, questions = plan_for(store, viewer, prompt)
    if not plan.matched:
        return format_echo([], [], "seat", plan.hint) if plan.hint else ""
    recorded, refused = apply_plan(store, plan, questions, viewer=viewer, via="seat",
                                   seen_through=view.seen_through if view else 0)
    return format_echo(recorded, refused, "seat", plan.hint)


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
    echo = ""
    try:
        echo = _seat_answer(store, viewer, payload.get("prompt"), env)
    except Exception:                         # fail open: the prompt passes untouched
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
            record_view(store, viewer, r.hwm, r.shown, deadline=t0 + HOOK_BUSY_MS / 1000)
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
        record_view(store, "cli", r.hwm, r.shown)
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
        plan, view, questions = plan_for(store, "cli", args.line)
        if not plan.matched or plan.rest.strip():
            why = plan.hint or "the whole line must be band answers, e.g. `1a, 2: rec`"
            print(f"levain band answer: refused: {why}; nothing was recorded", file=sys.stderr)
            return EXIT_REFUSED
        recorded, refused = apply_plan(store, plan, questions, viewer="cli", via="cli",
                                       seen_through=view.seen_through if view else 0)
    except Exception as exc:
        print(f"levain band answer: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(format_echo(recorded, refused, "cli").replace("[Levain band answer] ", "levain band answer: "))
    return 0 if recorded else EXIT_REFUSED


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
