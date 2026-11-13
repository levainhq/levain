"""THE LIST's page half of the Keep (row 182) and the band row to agenda (row 183), in real headless Chrome against a served cockpit
with a real broker, intention store, question store and anneal store in the scratch home (the conftest isolates HOME: nothing here
touches an operator's live apps; the band row is filed with no session, so its answer reaches no agent, and the delivery is
recorded, never run).

Pinned: an intention row offers ``keep`` where the cockpit offers ``intention_keep``, asks once in words, and posts the verb with
``confirm: true``; the episode lands and the intention composts; the receipt line is the server's. The List's search reaches
memory: a query with kept hits draws a "Kept in memory" group, expanded, read-only, each row naming its receipt line; a query
with none draws no group. A plain band row offers "Make an agenda", a day picker with no time, the server's refusal for a day
inside the snooze window printed in its own words, and on a day past it plants the agenda and answers the row.
``LEVAIN_LIST_SHOTS=<dir>`` keeps the desktop 1280 and phone 390 screenshots there."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import IntentionStore

from levain import band_agenda
from levain.consent import band
from levain.consent.queue import default_queue_dir
from levain.consent.questions import QuestionStore
from levain.writes import KEEP_EPISODE_SOURCE, apply_edit
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_list_page_browser import _FOCUS, _FOCUS_ROWS, TRAY, _open_focus, _posts, _reload, _seed_more

SEED = "a tray seed"
BAND_WORDS = "levain: pick the release date for the fellowship essay draft?"
ROWS = f"[...{TRAY}.querySelectorAll('.sp-results .row')]"
KEPT = f"{TRAY}.querySelector('[data-list-kept]')"
SEARCH = f"{TRAY}.querySelector('.ep-search-input')"


class _Delivered(list):
    """The recorder that stands in for the answer's delivery to a stopped agent. The server starts that delivery on a daemon thread
    after it has answered the page, so a test that reads the list right after the page's last step can read it before the thread
    has run: ``arrived`` is set once a delivery is recorded, and a test waits on it (bounded) before it counts."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = threading.Event()

    def record(self, *a: Any, **_k: Any) -> None:
        self.append(a)
        self.arrived.set()


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any, dict[str, str], Any]:
    """An install with the List's three stores, one plain band row (filed with no session: its answer reaches no agent) and
    one earlier Keep in memory. The delivery to a stopped agent is replaced by a recorder."""
    root, src, ids = _install(tmp_path)
    _seed_more(root)
    delivered = _Delivered()
    monkeypatch.setattr(band, "deliver_answer", delivered.record)
    qs = QuestionStore(default_queue_dir())
    q = qs.file(agent="claude_code", agent_session_id=band.NO_SESSION, what=BAND_WORDS,
                options=[("ship", "Ship it"), ("hold", "Hold")], rec="hold", blocks="the essay waits on it", cwd="/w/levain")
    apply_edit(src.write_scope, {"kind": "intention_keep", "intention_id": ids["note"]})      # "a keep note" is in memory already
    return root, src, ids, (qs, q, delivered)


def _keep_episodes(src: Any) -> list[tuple[str, str]]:
    db = sqlite3.connect(str(src.anneal.episodic_db))
    try:
        return db.execute("select id, content from episodes where source = ? order by timestamp", (KEEP_EPISODE_SOURCE,)).fetchall()
    finally:
        db.close()


def _row_of(words: str) -> str:
    """The List row whose clause starts with ``words``."""
    return (f"{ROWS}.find(r => (r.querySelector('.clause') || {{}}).textContent && "
            f"r.querySelector('.clause').textContent.startsWith({json.dumps(words)}))")


def _set_query(words: str) -> list[dict[str, Any]]:
    return [{"eval": f"(() => {{ const i = {SEARCH}; i.focus(); i.value = {json.dumps(words)}; "
                     "i.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"}]


def _shots() -> Path | None:
    """Where ``LEVAIN_LIST_SHOTS`` keeps the screenshots, or None (a run without it takes none)."""
    raw = os.environ.get("LEVAIN_LIST_SHOTS")
    if not raw:
        return None
    d = Path(raw)
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- the Keep ------------------------------------------------------------------------------------------------------


@needs_browser
def test_keep_asks_once_in_words_then_posts_confirm_true_and_the_row_leaves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, src, ids, _band = _world(tmp_path, monkeypatch)
    before = len(_keep_episodes(src))
    row = _row_of(SEED)
    keep_btn = f"{row}.querySelector('[data-verb=intention_keep]')"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {row} && 1"},
            # a person opens the row's overflow menu and picks keep: a form asks, in words, and nothing is sent yet
            {"eval": f"{row}.querySelector('.verb-more').click(); {keep_btn}.click(); 1"},
            {"until": f"{row}.querySelector('.verb-form .verb-confirm') && 1"},
            {"eval": f"JSON.stringify({{ask: {row}.querySelector('.verb-form').textContent, "
                     f"go: {row}.querySelector('.verb-form .verb-confirm').textContent}})"},
            {"eval": f"{row}.querySelector('.verb-form .verb-cancel').click(); {row}.querySelector('.verb-form') === null"},
            # cancelled: nothing was sent and the row is still here; asked again and confirmed, it goes
            {"eval": f"{row}.querySelector('.verb-more').click(); {keep_btn}.click(); 1"},
            {"until": f"{row}.querySelector('.verb-form .verb-confirm') && 1"},
            {"eval": f"{row}.querySelector('.verb-form .verb-confirm').click(); 1"},
            {"until": f"!{row} && 1"},
            {"until": f"(() => {{ const r = {TRAY}.querySelector('[data-list-receipt]'); return !!r && /kept as memory/.test(r.textContent) && r.textContent; }})()"},
            {"eval": f"{TRAY}.querySelector('[data-list-receipt]').textContent"},
        ])
    ask = json.loads(next(x for x in out["values"] if isinstance(x, str) and x.startswith('{"ask"')))
    assert "a tray seed" in ask["ask"] and "memory" in ask["ask"] and ask["go"] == "Keep", ask
    assert out["values"][-1].startswith("kept as memory: episode "), out["values"]
    fired = [p for p in _posts(out) if p.get("verb") == "intention_keep"]
    assert len(fired) == 1 and fired[0]["confirm"] is True and fired[0]["row_id"] == f"intention:{ids['seed']}", fired
    eps = _keep_episodes(src)
    assert len(eps) == before + 1 and eps[-1][1].startswith("a tray seed\n")
    open_ids = {r["id"] for r in IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").list_open()}
    assert ids["seed"] not in open_ids                                    # composted behind the episode


@needs_browser
def test_a_row_the_cockpit_does_not_offer_keep_on_has_no_keep_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            {"eval": f"JSON.stringify({{keep: {ROWS}.filter(r => r.querySelector('[data-verb=intention_keep]')).map(r => r.dataset.surface), "
                     f"foreign: {ROWS}.filter(r => ['board'].includes(r.dataset.surface) && r.querySelector('.verb-btn')).length, "
                     f"all: {ROWS}.map(r => r.dataset.surface)}})"},
        ])
    got = json.loads(out["values"][-1])
    assert got["keep"] and set(got["keep"]) <= {"intention", "talk", "drop"}, got      # an intention-backed row only
    assert "band" not in got["keep"] and got["foreign"] == 0, got


# --- the List's search reaches memory ---------------------------------------------------------------------------------


@needs_browser
def test_a_query_with_kept_hits_draws_the_kept_group_expanded_and_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    shots = _shots()
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            # no query: no kept group, and no kept row anywhere in the List
            {"eval": f"JSON.stringify({{group: !!({KEPT} && !{KEPT}.hidden && {KEPT}.children.length), kept: {ROWS}.filter(r => r.dataset.surface === 'kept').length}})"},
            *_set_query("noticed Z"),
            {"until": f"{KEPT} && !{KEPT}.hidden && {KEPT}.querySelectorAll('.row').length && 1"},
            {"eval": f"JSON.stringify({{open: {KEPT}.querySelector('details').open, head: {KEPT}.querySelector('summary').textContent, "
                     f"rows: [...{KEPT}.querySelectorAll('.row')].map(r => ({{s: r.dataset.surface, t: r.querySelector('.clause').textContent, "
                     f"line: (r.querySelector('.list-kept-line') || {{}}).textContent, verbs: r.querySelectorAll('.verb-btn').length}}))}})"},
            # a query only the List's own rows match: the kept group is gone
            *_set_query("tray seed"),
            {"until": f"{KEPT}.hidden || !{KEPT}.children.length"},
            {"eval": f"JSON.stringify({{hits: {ROWS}.length}})"},
            # a query that a keep's own episode matches: it names itself as kept
            *_set_query("keep note"),
            {"until": f"{KEPT} && !{KEPT}.hidden && {KEPT}.querySelectorAll('.row').length && 1"},
            {"eval": f"{KEPT}.querySelector('.list-kept-line').textContent"},
            {"eval": f"{TRAY}.scrollIntoView(); 1"},
            *([{"shot": str(shots / "kept_1280.png")}] if shots else []),
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{TRAY}.scrollIntoView(); JSON.stringify({{fits: document.documentElement.scrollWidth <= window.innerWidth + 1}})"},
            *([{"shot": str(shots / "kept_390.png")}] if shots else []),
            # clearing the box takes the memory part away with the query
            *_set_query(""),
            {"until": f"{KEPT}.hidden && {KEPT}.children.length === 0"},
        ])
    v = out["values"]
    quiet = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"group"')))
    assert quiet == {"group": False, "kept": 0}, quiet
    hit = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"open"')))
    assert hit["open"] is True and "Kept in memory" in hit["head"] and "1" in hit["head"], hit
    assert len(hit["rows"]) == 1 and hit["rows"][0]["s"] == "kept" and hit["rows"][0]["t"] == "noticed Z", hit
    assert hit["rows"][0]["line"].startswith("memory: observation episode "), hit      # not a Keep of ours: it says what it is
    assert hit["rows"][0]["verbs"] == 0, hit                                          # read-only
    assert [x for x in v if isinstance(x, str) and x.startswith("kept as memory: episode ")], v   # a Keep names itself
    assert '{"fits":true}' in v                                                       # the phone does not scroll sideways


# --- the band row to agenda --------------------------------------------------------------------------------------------


@needs_browser
def test_a_plain_band_row_makes_an_agenda_through_a_day_form_and_the_server_judges_the_day(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, src, _ids, (qs, q, delivered) = _world(tmp_path, monkeypatch)
    shots = _shots()
    inside = (date.today() + timedelta(days=2)).isoformat()            # inside the snooze window: the server refuses it in words
    past = (date.today() + timedelta(days=9)).isoformat()
    row = _row_of("levain: pick the release date")
    btn = f"{row}.querySelector('[data-verb=band_agenda]')"
    day = f"{row}.querySelector('.verb-form .verb-date')"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {row} && {btn} && 1"},
            {"eval": f"JSON.stringify({{label: {btn}.textContent, time: !!{row}.querySelector('.verb-time')}})"},
            {"eval": f"{btn}.click(); 1"},
            {"until": f"{day} && 1"},
            # the form is the date alone (a timed value is never the day the verb takes) and says what will happen
            {"eval": f"JSON.stringify({{time: !!{row}.querySelector('.verb-form .verb-time'), "
                     f"go: {row}.querySelector('.verb-form .verb-confirm').textContent}})"},
            {"eval": f"(() => {{ const d = {day}; d.value = {json.dumps(inside)}; d.dispatchEvent(new Event('input', {{bubbles: true}})); "
                     f"{row}.querySelector('.verb-form .verb-confirm').click(); return 1; }})()"},
            {"until": f"(() => {{ const m = {row}.querySelector('.verb-form .edit-msg'); return !!m && m.classList.contains('err') && m.textContent; }})()"},
            {"eval": f"{row}.querySelector('.verb-form .edit-msg').textContent"},
            {"eval": f"{row}.querySelector('.verb-form').scrollIntoView({{block: 'center'}}); 1"},
            *([{"shot": str(shots / "agenda_form_1280.png")}] if shots else []),
            # the same form at a phone's width: every line of it (the day, the words under it, the refusal) stays inside the card
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{row}.querySelector('.verb-form').scrollIntoView({{block: 'center'}}); 1"},
            {"eval": f"(() => {{ const card = {TRAY}.getBoundingClientRect(); const over = [{row}, ...{row}.querySelectorAll('.verb-form, .verb-form *')]"
                     ".filter(n => { const r = n.getBoundingClientRect(); return r.width > 0 && (r.right > card.right + 0.5 || r.left < card.left - 0.5); })"
                     ".map(n => n.className || n.tagName); return JSON.stringify({formOver: over}); })()"},
            *([{"shot": str(shots / "agenda_form_390.png")}] if shots else []),
            # past the window: it goes, and the row leaves with its receipt
            {"eval": f"(() => {{ const d = {day}; d.value = {json.dumps(past)}; d.dispatchEvent(new Event('input', {{bubbles: true}})); "
                     f"{row}.querySelector('.verb-form .verb-confirm').click(); return 1; }})()"},
            {"until": f"!{row} && 1"},
            {"until": f"(() => {{ const r = {TRAY}.querySelector('[data-list-receipt]'); return !!r && /Later/.test(r.textContent) && r.textContent; }})()"},
            {"eval": f"{TRAY}.querySelector('[data-list-receipt]').textContent"},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{TRAY}.scrollIntoView(); JSON.stringify({{fits: document.documentElement.scrollWidth <= window.innerWidth + 1}})"},
            *([{"shot": str(shots / "agenda_receipt_390.png")}] if shots else []),
        ])
    v = out["values"]
    first = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"label"')))
    assert first == {"label": "Make an agenda", "time": False}, first
    form = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"time"')))
    assert form["time"] is False and form["go"], form
    refusal = next((x for x in v if isinstance(x, str) and "snooze window" in x), None)
    assert refusal and refusal.startswith(inside) and "an agenda is for a day after" in refusal, v    # the server's own words, not a fixed line
    assert "snooze the row instead" not in refusal, refusal            # no pointer to a Snooze the List does not offer on this row
    fit = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"formOver"')))
    assert fit == {"formOver": []}, fit                               # at 390 nothing in the form passes the card's edge
    fired = [p for p in _posts(out) if p.get("verb") == "band_agenda"]
    assert [p["params"] for p in fired] == [{"day": inside}, {"day": past}], fired
    assert all(p["confirm"] is True and p["row_id"] == f"band:{q.id}" for p in fired), fired
    receipt = next(x for x in v if isinstance(x, str) and x.startswith("-> Later"))
    assert receipt.startswith("-> Later, wakes ") and past in receipt, receipt
    agendas = [s for s in IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").list_open()
               if str(s.get("origin_key", "")).startswith(band_agenda.KEY_PREFIX)]
    assert len(agendas) == 1 and agendas[0]["text"] == BAND_WORDS and str(agendas[0]["next"]).startswith(past), agendas
    answered = qs.get(q.id)
    assert answered.answer is not None and answered.answer.kind == "text" and answered.answer.channel == "cockpit", answered.answer
    assert answered.answer.value.startswith("deferred: comes back " + past), answered.answer
    assert delivered.arrived.wait(5), "the delivery thread never ran"        # it is started on a thread after the answer: wait for it
    assert len(delivered) == 1                                         # the stopped agent's delivery was started once
    assert '{"fits":true}' in v


@needs_browser
def test_only_the_plain_band_row_offers_an_agenda_button(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            {"eval": f"JSON.stringify({{agenda: {ROWS}.filter(r => r.querySelector('[data-verb=band_agenda]')).map(r => r.dataset.surface)}})"},
        ])
    assert json.loads(out["values"][-1]) == {"agenda": ["band"]}, out["values"]      # the one plain band row, and nothing else


# --- the L1 fixes (levain 24, key 1112-44): what the page says when the server refuses --------------------------------------
#
# Each test below was run against the build as it stood (13ccd657d) first and failed there for the reason its comment names.

def _day_words_step(*days: date) -> dict[str, Any]:
    """Asks the page how it words each day (``dayYearWords``, the function the line is composed with): the browser's locale
    decides the order and the punctuation, so the test reads the words from the page and does not rebuild them."""
    return {"eval": "JSON.stringify({words: " + json.dumps([d.isoformat() for d in days])
                    + ".map(d => window.LevainDashboard.dayYearWords(d))})"}


def _day_words(out: dict[str, Any], *days: date) -> list[str]:
    """The words the page gave for ``days``, each pinned by literal facts that hold in any locale: the year and the day of the
    month are in it, and it is not the raw ISO day (a function that returned its argument would pass a derived-only test)."""
    got = next(json.loads(x)["words"] for x in out["values"] if isinstance(x, str) and x.startswith('{"words"'))
    assert len(got) == len(days), got
    for day, words in zip(days, got):
        assert isinstance(words, str) and words != day.isoformat() and day.isoformat() not in words, (day, words)
        assert str(day.year) in words and str(day.day) in words, (day, words)
    return got


def _planted_line(words: str) -> str:
    """The agenda_planted_unanswered line when the server names the day the agenda stands on (``words``: the page's wording of it)."""
    return f"The agenda for {words} is planted, but the row may still be open. Reload the List and check; trying again is safe."


GENERIC_STORE_LINE = "The store behind that is unavailable right now. Nothing was added."
FAULT_LINES = {
    "needs_signature": "This row is settled by signing: sign it, or answer it on its own sheet in Decisions.",
    "rings_a_reminder": "This row rings a reminder, so it needs no agenda. Change the reminder itself.",
    "agenda_resolved": ("The agenda made for this row is no longer open, so it cannot bring the row back. "
                        "Answer the row in words instead."),
    "answer_refused": "The row was not answered. Reload the List and check it before trying again.",
    "keep_key_retired": ("This item was kept before and that memory was deleted, so it cannot be kept again. "
                         "Compost the item by hand."),
}
KEPT_FAULT_LINE = "memory could not be searched"
A_PATH = "/Users/someone/.levain/consent/questions.db"


def _day_input(row: str) -> str:
    return f"{row}.querySelector('.verb-form .verb-date')"


def _pick(row: str, day: str) -> dict[str, Any]:
    """What a person does: put a day in the form's date box (an input event, as picking makes)."""
    return {"eval": f"(() => {{ const d = {_day_input(row)}; d.value = {json.dumps(day)}; "
                    "d.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"}


def _confirm(row: str) -> dict[str, Any]:
    return {"eval": f"{row}.querySelector('.verb-form .verb-confirm').click(); 1"}


def _failed(row: str) -> dict[str, Any]:
    """Waits for the form's message to read a refusal, and answers its text (a click sets the busy mark first, so a LATER
    refusal is waited for, not the one already on screen)."""
    return {"until": f"(() => {{ const m = {row}.querySelector('.verb-form .edit-msg'); "
                     "return !!m && m.classList.contains('err') && m.textContent; })()"}


def _refuse_in_order(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, refusals: list[tuple[Any, ...]]) -> None:
    """Replaces ``module.name`` with a function raising the real :class:`EditError` the server raises, one per call, in order.
    The cockpit's verb route, the JSON it sends, the page's transport and its `fault` are all the real ones. A refusal may carry a
    fourth member: the ``extra`` fields the server sends beside ``error`` and ``message``."""
    from levain.writes import EditError

    calls = iter(refusals)

    def raiser(*_a: Any, **_k: Any) -> Any:
        code, status, words, *extra = next(calls)
        raise EditError(code, status, words, extra[0] if extra else None)

    monkeypatch.setattr(module, name, raiser)


@needs_browser
def test_a_store_fault_after_the_agenda_is_planted_does_not_say_nothing_was_added(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 13ccd657d: the form read "The store behind that is unavailable right now. Nothing was added." while the
    agenda stood in the store (the server raises this 503 after act 1)."""
    root, src, _ids, (qs, q, delivered) = _world(tmp_path, monkeypatch)
    past_day = date.today() + timedelta(days=9)
    past = past_day.isoformat()

    def locked(*_a: Any, **_k: Any) -> Any:           # the real shape: the question store is a sqlite file the hooks write too
        raise sqlite3.OperationalError(f"database is locked: {A_PATH}")

    monkeypatch.setattr(band_agenda, "_answer_deferred", locked)
    row = _row_of("levain: pick the release date")
    btn = f"{row}.querySelector('[data-verb=band_agenda]')"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {row} && {btn} && 1"},
            _day_words_step(past_day),
            {"eval": f"{btn}.click(); 1"},
            {"until": f"{_day_input(row)} && 1"},
            _pick(row, past),
            _confirm(row),
            _failed(row),
            {"eval": f"JSON.stringify({{again: !{row}.querySelector('.verb-form .verb-confirm').disabled}})"},
        ])
    v = out["values"]
    shown = v[-2]
    assert shown == _planted_line(_day_words(out, past_day)[0]), v                           # the server's code names the day the agenda stands on
    assert "Nothing was added" not in shown and A_PATH not in shown and "locked" not in shown
    assert '{"again":true}' in v                                         # the form is live again: a retry is safe
    agendas = [s for s in IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").list_open()
               if str(s.get("origin_key", "")).startswith(band_agenda.KEY_PREFIX)]
    assert len(agendas) == 1, agendas                                    # the agenda IS planted: "Nothing was added" was false
    assert qs.get(q.id).answer is None                                   # and the row is still open


@needs_browser
def test_the_refusal_codes_with_no_line_of_their_own_each_get_a_fixed_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 13ccd657d: each read "That did not go through (<code>)." and hid what to do. The server's own words (which can
    carry a raw exception) are never shown; each code reads its own fixed line."""
    from levain import writes

    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    past = (date.today() + timedelta(days=9)).isoformat()
    raw = "RAW-SERVER-TEXT " + A_PATH
    _refuse_in_order(monkeypatch, band_agenda, "defer", [
        ("needs_signature", 409, "this row is settled by signing; " + raw),
        ("rings_a_reminder", 409, "this row rings a reminder; " + raw),
        ("agenda_resolved", 409, "this row's agenda is composted; " + raw),
        ("answer_refused", 422, "the row was not answered (" + raw + ")")])
    _refuse_in_order(monkeypatch, writes, "_apply_intention_keep", [
        ("keep_key_retired", 409, "this item was kept before; " + raw)])
    row = _row_of("levain: pick the release date")
    btn = f"{row}.querySelector('[data-verb=band_agenda]')"
    seed = _row_of(SEED)
    keep_btn = f"{seed}.querySelector('[data-verb=intention_keep]')"
    steps: list[dict[str, Any]] = [
        {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
        *_reload(),
        {"until": f"{TRAY} && {row} && {btn} && {seed} && 1"},
        {"eval": f"{btn}.click(); 1"},
        {"until": f"{_day_input(row)} && 1"},
        _pick(row, past)]
    for _ in range(4):
        steps += [_confirm(row), _failed(row)]
    steps += [
        {"eval": f"{seed}.querySelector('.verb-more').click(); {keep_btn}.click(); 1"},
        {"until": f"{seed}.querySelector('.verb-form .verb-confirm') && 1"},
        {"eval": f"{seed}.querySelector('.verb-form .verb-confirm').click(); 1"},
        {"until": f"(() => {{ const m = {seed}.querySelector('.verb-form .edit-msg'); return !!m && m.classList.contains('err') && m.textContent; }})()"}]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", steps)
    lines = [x for x in out["values"] if isinstance(x, str)][-5:]          # the five refusals, in the order they were raised
    assert lines == [FAULT_LINES[c] for c in ("needs_signature", "rings_a_reminder", "agenda_resolved", "answer_refused",
                                              "keep_key_retired")], out["values"]
    assert not any("RAW-SERVER-TEXT" in x or A_PATH in x for x in lines)
    assert len(set(lines)) == 5                                          # each code its own line


@needs_browser
def test_the_agenda_forms_preview_and_done_line_name_the_year(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 13ccd657d: a day typed as 2036 read "comes back Sun Nov 17 as an agenda", the same as the intended day. The year is
    always named (not only when it differs): the preview and the done line both end the day in `, <year>`. The write itself is
    stubbed at the page's transport (a landed write redraws the row away, taking the done line with it)."""
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    near = date.today() + timedelta(days=9)
    far = "2036-11-17"
    row = _row_of("levain: pick the release date")
    btn = f"{row}.querySelector('[data-verb=band_agenda]')"
    back = f"{row}.querySelector('.verb-form .intention-back').textContent"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {row} && {btn} && 1"},
            {"eval": f"{btn}.click(); 1"},
            {"until": f"{_day_input(row)} && 1"},
            {"eval": "window.LevainCockpitTransport.rowVerb = async () => ({ok: true, data: {}}); 1"},
            _pick(row, near.isoformat()),
            {"eval": back},
            _pick(row, far),
            {"eval": back},
            _confirm(row),
            {"until": f"(() => {{ const m = {row}.querySelector('.verb-form .edit-msg'); return !!m && /^agenda for/.test(m.textContent) && m.textContent; }})()"},
        ])
    v = out["values"][-5:]                                                     # back(near), pick, back(far), confirm, done
    assert str(near.year) in v[0] and near.strftime("%b") in v[0], v          # a same-year day names its year too
    assert "2036" in v[2], v                                                   # the fat-fingered year can be seen
    assert "2036" in v[4] and v[4].startswith("agenda for "), v                # and so can the done line


@needs_browser
def test_the_kept_group_shows_a_fixed_line_never_the_servers_or_transports_text(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 13ccd657d: a memory read fault printed the kernel's `kept memory could not be read (episodes: <path>)`, and a
    transport failure printed `memory could not be searched — <e.message>`."""
    from levain.cockpit import list_panel, providers

    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    real = list_panel._view_fault

    def episodes_fail(view: Any, *keys: str) -> Any:
        if "episodes" in keys:
            return providers.Fault(f"episodes: disk I/O error at {A_PATH}")
        return real(view, *keys)

    monkeypatch.setattr(list_panel, "_view_fault", episodes_fail)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            *_set_query("tray seed"),
            {"until": f"{KEPT} && !{KEPT}.hidden && {KEPT}.textContent"},
            # the box is cleared (the group goes), then a transport that fails with a path in its message (the page's own catch
            # branch) answers the next query: the group read at the end is the NEW answer, never the first query's left on screen
            *_set_query(""),
            {"until": f"{KEPT}.hidden && {KEPT}.children.length === 0"},
            {"eval": f"window.LevainCockpitTransport.readJson = () => Promise.reject(new Error('EIO {A_PATH}')); 1"},
            *_set_query("another query"),
            {"until": f"{KEPT} && !{KEPT}.hidden && {KEPT}.textContent"},
        ])
    server_fault, transport_fault = out["values"][-6], out["values"][-1]
    for text in (server_fault, transport_fault):
        assert A_PATH not in text and "disk I/O" not in text and "EIO" not in text, text
        assert text.strip() == KEPT_FAULT_LINE, text


@needs_browser
def test_the_keep_form_with_a_long_line_stays_inside_the_card_at_phone_width(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L1 finding 8, measured: the Keep form's quoted line (up to 120 characters of the row's words) at 390 px, as words and as
    one unbroken token. Numbers go to ``measure_keep_390.json`` in ``LEVAIN_LIST_SHOTS`` when it is set."""
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    store = IntentionStore(_root / ".levain" / "memory.spores.json", id_prefix="intention")
    words = "keep this long line of plain words that a person might type into the list box " * 3
    store.add(type="task", text=words.strip(), disposition="seed", attention="talk")
    store.add(type="task", text="U" * 160, disposition="seed", attention="talk")
    shots = _shots()
    first = _row_of("keep this long line")
    second = _row_of("U" * 20)
    probe = ("JSON.stringify({sw: document.documentElement.scrollWidth, iw: window.innerWidth, "
             "ask: %s.querySelector('.keep-ask').textContent.length})")
    steps: list[dict[str, Any]] = [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, *_reload(),
                                   {"until": f"{TRAY} && {first} && {second} && 1"}]
    for n, row in enumerate((first, second)):
        steps += [
            {"eval": f"{row}.querySelector('.verb-more').click(); {row}.querySelector('[data-verb=intention_keep]').click(); 1"},
            {"until": f"{row}.querySelector('.verb-form .keep-ask') && 1"},
            {"eval": f"{row}.querySelector('.verb-form').scrollIntoView({{block: 'center'}}); " + probe % row},
            *([{"shot": str(shots / f"keep_form_390_{n}.png")}] if shots else []),
            {"eval": f"{row}.querySelector('.verb-form .verb-cancel').click(); 1"}]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", steps)
    got = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"sw"')]
    if shots:
        (shots / "measure_keep_390.json").write_text(json.dumps(got))
    assert len(got) == 2 and all(g["ask"] > 100 for g in got), got          # the long quoted line really was drawn
    assert all(g["sw"] <= g["iw"] for g in got), got


@needs_browser
def test_a_snooze_and_a_when_form_still_lay_out_at_both_widths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L1 finding 7: the `.when-form .intention-back` rule is global, so the existing snooze form (and the `when?` form, which
    draws no back line) must still fit the card at 390 and 1280. Screenshots go to ``LEVAIN_LIST_SHOTS``."""
    from levain.consent.questions import ANSWER_CHANNELS

    root, src, _ids, (qs, _q, _delivered) = _world(tmp_path, monkeypatch)
    ring, _ = qs.file_once(agent="claude_code", key="levain-germinate:k1", what="Due today: renew the thing (intention)",
                           options=[("done", "Done")], rec="done", blocks="the agenda came due", cwd=str(root),
                           held_by=ANSWER_CHANNELS)
    sp = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    sid = sp.add(type="task", text="renew the thing", disposition="agenda", next=date.today().isoformat())["id"]
    sp.update(sid, germinated={"on": date.today().isoformat(), "target": "band", "ref": ring.id})
    shots = _shots()
    later = (date.today() + timedelta(days=3)).isoformat()
    # the Focus dialog holds every row (a phone's card clamps to four with a "more" control), so both forms are drawn there
    ringing = f"{_FOCUS_ROWS}.find(r => r.querySelector('[data-verb=intention_snooze]'))"
    water = (f"{_FOCUS_ROWS}.find(r => (r.querySelector('.clause') || {{}}).textContent && "
             "r.querySelector('.clause').textContent.startsWith('talk about the water'))")
    over = (lambda row: f"(() => {{ const card = {_FOCUS}.getBoundingClientRect(); const bad = [{row}, ...{row}.querySelectorAll('.verb-form, .verb-form *')]"
                        ".filter(n => { const r = n.getBoundingClientRect(); return r.width > 0 && (r.right > card.right + 0.5 || r.left < card.left - 0.5); })"
                        ".map(n => n.className || n.tagName); return JSON.stringify({over: bad, sw: document.documentElement.scrollWidth <= window.innerWidth + 1}); })()")
    steps: list[dict[str, Any]] = [{"emulate": {"w": 1280, "h": 1100, "dpr": 1}}, *_reload(),
                                   {"until": f"{TRAY} && {TRAY}.querySelector('.pexpand') && 1"}, *_open_focus(),
                                   {"until": f"{ringing} && {water} && 1"}]
    for label, size in (("1280", {"w": 1280, "h": 1100, "dpr": 1}), ("390", {"w": 390, "h": 844, "dpr": 2, "mobile": True})):
        steps += [
            {"emulate": size},
            {"eval": "new Promise(r => setTimeout(() => r(1), 400))"},
            {"eval": f"{ringing}.querySelector('[data-verb=intention_snooze]').click(); 1"},
            {"until": f"{ringing}.querySelector('.verb-form .verb-date') && 1"},
            _pick(ringing, later),
            {"eval": f"{ringing}.querySelector('.verb-form').scrollIntoView({{block: 'center'}}); " + over(ringing)},
            *([{"shot": str(shots / f"snooze_form_{label}.png")}] if shots else []),
            {"eval": f"{ringing}.querySelector('.verb-form .verb-cancel').click(); 1"},
            {"eval": f"{water}.querySelector('[data-verb=intention_surface_at]').click(); 1"},
            {"until": f"{water}.querySelector('.verb-form .verb-date') && 1"},
            {"eval": f"{water}.querySelector('.verb-form').scrollIntoView({{block: 'center'}}); " + over(water)},
            *([{"shot": str(shots / f"when_form_{label}.png")}] if shots else []),
            {"eval": f"{water}.querySelector('.verb-form .verb-cancel').click(); 1"}]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", steps)
    fits = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"over"')]
    assert len(fits) == 4, out["values"]
    assert all(f == {"over": [], "sw": True} for f in fits), fits


# --- AGENDA_DAY_REFUSAL against the host's real words --------------------------------------------------------------------


def test_agenda_day_refusal_matches_every_branch_of_the_hosts_real_day_refusal() -> None:
    """The page's pattern is a prose contract, so each branch of ``band_agenda.day_refusal`` is asserted against the pattern
    with the host's REAL output (never retyped): dropping a branch from the pattern fails here. A message that is not one of the
    host's openings, or that carries a path, must not match."""
    import re
    import subprocess

    from tests.test_cockpit_flip_parity import NODE

    if NODE is None:
        pytest.skip("node is needed")
    boot = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "dashboard_boot.js").read_text()
    literal = re.search(r"const AGENDA_DAY_REFUSAL = (/.*/);", boot)
    assert literal, "AGENDA_DAY_REFUSAL moved"
    today = date(2026, 11, 12)
    real = {"not_after_today": band_agenda.day_refusal("2026-11-12", today),
            "within_the_window": band_agenda.day_refusal("2026-11-14", today),
            "not_a_date": band_agenda.day_refusal("20261-11-17", today)}
    assert all(real.values()), real
    cases = {**real, "unrelated": "the question store cannot read /Users/x/q.db", "a_path_after_the_opening":
             real["within_the_window"] + " /Users/x/q.db", "empty": ""}
    script = f"const re = {literal.group(1)}; const c = JSON.parse(process.argv[1]); " \
             "console.log(JSON.stringify(Object.fromEntries(Object.entries(c).map(([k, v]) => [k, re.test(v)]))));"
    res = subprocess.run([NODE, "-e", script, json.dumps(cases)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"not_after_today": True, "within_the_window": True, "not_a_date": True,
                                      "unrelated": False, "a_path_after_the_opening": False, "empty": False}, res.stdout


# --- the L3 round 1 fixes (levain 24, key 1112-44, lane LISTPAGEFIX2) --------------------------------------------------------
#
# Each test below was run against the build as it stood (52fc9ea4f) first and failed there for the reason its comment names.

WRITE_UNSURE_LINE = "Could not tell whether that went through. Reload the List and check; trying again is safe."
_LONE_SURROGATE = r"/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/"
# the page's tray.json reads that carry a search query (the memory half of the List's search), counted and shaped by a stub of fetch
_COUNT_READS = ("window.__reads = []; const realFetch = window.fetch.bind(window); "
                "window.fetch = (u, o) => { if (/tray\\.json/.test(String(u)) && /[?&]q=/.test(String(u))) window.__reads.push(String(u)); "
                "return realFetch(u, o); }; 1")
_LIST_WORDS = (f"JSON.stringify({{rows: {ROWS}.map(r => (r.querySelector('.clause') || {{}}).textContent), "
               f"status: ({TRAY}.querySelector('.ep-search-status') || {{}}).textContent}})")


def _seed_texts(root: Path, *texts: str) -> None:
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    for t in texts:
        store.add(type="task", text=t, disposition="seed", attention="talk")


def _list_words(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"rows"')]


@needs_browser
def test_a_list_with_no_live_rows_still_searches_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (codex MED): with no live row but one kept memory, the panel returned at its empty-state line before it
    made the search box, so memory could not be searched at all."""
    root, src, ids = _install(tmp_path)
    apply_edit(src.write_scope, {"kind": "intention_keep", "intention_id": ids["note"]})         # "a keep note" is in memory now
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    for s in store.list_open():
        store.descend(s["id"], kind="composted")                                                 # and nothing is left on the List
    assert store.list_open() == []
    # the Done group keeps a row a week: age every resolution, or the List still holds the rows just composted (it then has rows)
    doc_path = root / ".levain" / "memory.spores.json"
    doc = json.loads(doc_path.read_text("utf-8"))
    assert doc["resolved"], doc.keys()
    for r in doc["resolved"]:
        r["resolution"]["at"] = "2020-01-07T09:00:00+00:00"
    doc_path.write_text(json.dumps(doc), "utf-8")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && 1"},
            {"eval": f"JSON.stringify({{rows: {ROWS}.length, search: !!{SEARCH}, empty: !!{TRAY}.querySelector('.empty'), host: !!{KEPT}}})"},
            *_set_query("keep note"),
            {"until": f"{KEPT} && !{KEPT}.hidden && {KEPT}.querySelectorAll('.row').length && 1"},
            {"eval": f"JSON.stringify({{head: {KEPT}.querySelector('summary').textContent, line: {KEPT}.querySelector('.clause').textContent}})"},
            *_set_query(""),
            {"until": f"{KEPT}.hidden && {KEPT}.children.length === 0"},
        ])
    v = out["values"]
    before = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"rows"')))
    assert before == {"rows": 0, "search": True, "empty": True, "host": True}, before     # nothing live, and the search is there
    hit = json.loads(next(x for x in v if isinstance(x, str) and x.startswith('{"head"')))
    assert "Kept in memory" in hit["head"] and hit["line"].startswith("a keep note"), hit


@needs_browser
def test_the_lists_live_rows_match_a_query_the_way_the_servers_memory_search_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (codex MED): live rows used one contiguous, case-folded substring, memory used accent-folded space-separated
    AND terms, so `alpha beta` hid `alpha one beta two` while showing the same words from memory. The page now mirrors the
    server's `_fold` and `_terms_match` (levain/cockpit/engine.py) for the List's rows."""
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_texts(root, "alpha one beta two", "Café Crème brûlée", "SHOUTING words")
    asks = ["alpha beta", "BETA   alpha", "alpha gamma", "cafe creme", "CAFÉ", "brulee", "shouting WORDS", "intention-"]
    steps: list[dict[str, Any]] = [{"emulate": {"w": 1280, "h": 1100, "dpr": 1}}, *_reload(),
                                   {"until": f"{TRAY} && {ROWS}.length > 2 && 1"}]
    for q in asks:
        steps += [*_set_query(q), {"eval": _LIST_WORDS}]
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", steps)
    got = {q: g["rows"] for q, g in zip(asks, _list_words(out))}
    assert got["alpha beta"] == ["alpha one beta two"], got                    # terms, not one contiguous run
    assert got["BETA   alpha"] == ["alpha one beta two"], got                  # any order, any run of spaces, any case
    assert got["alpha gamma"] == [], got                                       # every term is required
    assert got["cafe creme"] == ["Café Crème brûlée"], got                    # accents fold, both ways
    assert got["CAFÉ"] == ["Café Crème brûlée"] and got["brulee"] == ["Café Crème brûlée"], got
    assert got["shouting WORDS"] == ["SHOUTING words"], got
    assert len(got["intention-"]) >= 4, got                                    # an intention id still matches
    assert _list_words(out)[0]["status"].startswith("1 match"), _list_words(out)[0]


@needs_browser
def test_the_list_search_takes_at_most_200_characters_and_both_halves_read_the_same_query(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (codex LOW): rows filtered by the whole box while the memory read cut it to 200, so a long paste showed
    memory hits for a query the rows beside it did not match. The box now holds 200 at most, and a value set past that
    (a script, a restored query) is cut the same way for the rows."""
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_texts(root, "needle haystack")
    long_query = "needle" + " " * 210 + "zzz"                                   # past 200, the tail is the only thing that does not match
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            {"eval": f"JSON.stringify({{max: {SEARCH}.maxLength}})"},
            *_set_query(long_query),
            {"eval": _LIST_WORDS},
        ])
    assert json.loads(next(x for x in out["values"] if isinstance(x, str) and x.startswith('{"max"'))) == {"max": 200}
    assert _list_words(out)[0]["rows"] == ["needle haystack"], _list_words(out)


@needs_browser
def test_a_send_that_throws_leaves_the_keep_and_agenda_forms_with_a_fixed_line_and_live_buttons(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (complement LOW): a rejected send (a network drop) threw inside the click handler and left the form
    disabled on "..." with no way out but a reload; both are irreversible writes, so the person must be told it is unknown."""
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    past = (date.today() + timedelta(days=9)).isoformat()
    seed = _row_of(SEED)
    band_row = _row_of("levain: pick the release date")
    state = lambda row: ("JSON.stringify({msg: " + row + ".querySelector('.verb-form .edit-msg').textContent, "      # noqa: E731
                         "cls: " + row + ".querySelector('.verb-form .edit-msg').className, "
                         "go: !" + row + ".querySelector('.verb-form .verb-confirm').disabled, "
                         "cancel: !" + row + ".querySelector('.verb-form .verb-cancel').disabled})")
    wait = {"eval": "new Promise(r => setTimeout(() => r(1), 600))"}
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {seed} && {band_row} && 1"},
            {"eval": "window.LevainCockpitTransport.rowVerb = () => Promise.reject(new TypeError('Failed to fetch')); 1"},
            # the Keep
            {"eval": f"{seed}.querySelector('.verb-more').click(); {seed}.querySelector('[data-verb=intention_keep]').click(); 1"},
            {"until": f"{seed}.querySelector('.verb-form .verb-confirm') && 1"},
            _confirm(seed),
            wait,
            {"eval": state(seed)},
            # the agenda
            {"eval": f"{band_row}.querySelector('[data-verb=band_agenda]').click(); 1"},
            {"until": f"{_day_input(band_row)} && 1"},
            _pick(band_row, past),
            _confirm(band_row),
            wait,
            {"eval": state(band_row)},
        ])
    got = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"msg"')]
    assert len(got) == 2, out["values"]
    for g in got:
        assert g["msg"] == WRITE_UNSURE_LINE and "err" in g["cls"], g
        assert g["go"] is True and g["cancel"] is True, g                          # the buttons are live again: a retry is possible


@needs_browser
def test_a_panel_redraw_leaves_no_read_from_its_old_search_box_and_a_newer_key_aborts_the_older_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (complement LOW 2, deepseek MED): a debounce armed in a panel that was then redrawn still fired its
    read, and a superseded read was dropped but never aborted. The host check (`isConnected`) stops the first; the AbortController
    the second."""
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    hang = ("window.__sigs = []; window.__res = []; const real2 = window.fetch.bind(window); "
            "window.fetch = (u, o) => { if (/tray\\.json/.test(String(u)) && /[?&]q=/.test(String(u))) "
            "return new Promise((res, rej) => { const s = o && o.signal ? o.signal : null; window.__sigs.push(s); window.__res.push(res); "
            "if (s) s.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); }); return real2(u, o); }; 1")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            {"eval": _COUNT_READS},
            # a control: a query is read once its debounce has run
            *_set_query("keep note"),
            {"until": "window.__reads.length === 1 && 1"},
            {"until": f"{KEPT} && !{KEPT}.hidden && 1"},
            # the panel is redrawn (its memory host replaced) while the next debounce is still pending: no read follows
            *_set_query("tray seed"),
            {"eval": f"window.__host = {KEPT}; window.__host.remove(); 1"},
            {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({reads: window.__reads.length})), 800))"},
            # the host is put back (the same box goes on): a hung read, then a newer key, and the older read's signal is aborted
            {"eval": f"{TRAY}.querySelector('.sp-results').after(window.__host); 1"},
            {"eval": hang},
            *_set_query("keep"),
            {"until": "window.__sigs.length === 1 && 1"},
            *_set_query("keep n"),
            {"eval": "JSON.stringify({aborted: window.__sigs[0] ? window.__sigs[0].aborted : null})"},
            # the panel is redrawn while the newest read is in flight; the answer that comes after is drawn into nothing
            {"until": "window.__sigs.length === 2 && 1"},
            {"eval": "window.__host.remove(); 1"},
            {"eval": "window.__res[1](new Response(JSON.stringify({status: 'ok', rows: [{id: 'k1', title: 'late', body: 'late words', "
                     "facets: {surface: 'kept', kept_line: 'kept from the List: x'}}]}), {status: 200, headers: {'Content-Type': 'application/json'}})); 1"},
            {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({late: window.__host.textContent.includes('late words')})), 400))"},
        ])
    v = out["values"]
    assert '{"reads":1}' in v, v                                                   # the control's one read, and none from the orphan
    assert '{"aborted":true}' in v, v
    assert v[-1] == '{"late":false}', v                                                # an answer for a replaced panel draws nothing


@needs_browser
def test_a_refused_memory_read_prompts_no_token_and_drops_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (complement LOW 3): a keystroke-driven background read that the server answered with the token 403 ran
    the transport's token bootstrap (a window.prompt, then a dropped token). It is now a quiet kept fault."""
    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    refuse = ("window.__prompts = 0; window.prompt = () => { window.__prompts++; return ''; }; "
              "const real3 = window.fetch.bind(window); "
              "window.fetch = (u, o) => (/tray\\.json/.test(String(u)) && /[?&]q=/.test(String(u)) "
              "? Promise.resolve(new Response(JSON.stringify({message: 'missing or invalid write token'}), "
              "{status: 403, headers: {'Content-Type': 'application/json'}})) : real3(u, o)); 1")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {ROWS}.length > 2 && 1"},
            {"eval": refuse},
            *_set_query("tray seed"),
            {"until": f"{KEPT} && !{KEPT}.hidden && {KEPT}.textContent"},
            {"eval": "JSON.stringify({prompts: window.__prompts, token: localStorage.getItem('levain_write_token')})"},
        ])
    shown = out["values"][-2]
    assert shown.strip() == KEPT_FAULT_LINE, shown
    assert json.loads(out["values"][-1]) == {"prompts": 0, "token": TOKEN}, out["values"]


@needs_browser
def test_the_keep_form_cuts_its_quoted_line_on_a_whole_character(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 52fc9ea4f (complement LOW 4): `slice(0, 120)` counts UTF-16 units, so an astral character at the cut left half of
    its pair in the quoted line."""
    root, src, _ids, _band = _world(tmp_path, monkeypatch)
    rocket = "\U0001F680"
    _seed_texts(root, "q" * 119 + rocket + " and the rest of the words that follow it")
    row = _row_of("q" * 20)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1280, "h": 1100, "dpr": 1}},
            *_reload(),
            {"until": f"{TRAY} && {row} && 1"},
            {"eval": f"{row}.querySelector('.verb-more').click(); {row}.querySelector('[data-verb=intention_keep]').click(); 1"},
            {"until": f"{row}.querySelector('.verb-form .keep-ask') && 1"},
            {"eval": f"(() => {{ const t = {row}.querySelector('.keep-ask').textContent; "
                     f"return JSON.stringify({{lone: {_LONE_SURROGATE}.test(t), rocket: t.includes({json.dumps(rocket)}), "
                     "cut: t.includes('\\u2026')}); })()"},
        ])
    assert json.loads(out["values"][-1]) == {"lone": False, "rocket": True, "cut": True}, out["values"]


# --- the three result codes the server added (levain 26, key 1113-3): what the page says for each --------------------------------
#
# Each test below was run first against the page as merged from the list-page branch (the boot code keys the day refusal on
# bad_request/400 and the unsure-agenda line on source_unavailable) with the new server under it, and failed there as its comment says.

BAD_DAY_LINE = "That day cannot be used for an agenda. Pick a later day."
PLANTED_NO_DAY_LINE = "The agenda is planted, but the row may still be open. Reload the List and check; trying again is safe."
KEPT_503_TAIL = ", but the item could not be closed here and is still on the List. Keeping it again is safe: it writes the memory once."
KEPT_409_TAIL = ", but the item changed before it could be closed. Reload the List to see where it stands."
KEPT_OTHER_TAIL = ", and its current state could not be confirmed."
WIDTHS = [pytest.param(1280, 1100, 1, False, id="1280"), pytest.param(390, 844, 2, True, id="390")]


def _form_overflow(row: str) -> dict[str, Any]:
    """What passes the card's edge in the row's open form (the day, the words under it, the refusal line)."""
    return {"eval": f"(() => {{ {row}.querySelector('.verb-form').scrollIntoView({{block: 'center'}}); const card = {TRAY}.getBoundingClientRect(); "
                    f"const over = [{row}, ...{row}.querySelectorAll('.verb-form, .verb-form *')]"
                    ".filter(n => { const r = n.getBoundingClientRect(); return r.width > 0 && (r.right > card.right + 0.5 || r.left < card.left - 0.5); })"
                    ".map(n => n.className || n.tagName); return JSON.stringify({formOver: over}); })()"}


def _open_keep(seed: str) -> list[dict[str, Any]]:
    return [{"eval": f"{seed}.querySelector('.verb-more').click(); {seed}.querySelector('[data-verb=intention_keep]').click(); 1"},
            {"until": f"{seed}.querySelector('.verb-form .verb-confirm') && 1"}]


def _texts(out: dict[str, Any]) -> list[str]:
    """The line reads of a run in order: the strings that are not an overflow measurement."""
    return [x for x in out["values"] if isinstance(x, str) and not x.startswith(('{"formOver"', '{"gets"', '{"words"'))]


def _no_overflow(out: dict[str, Any]) -> list[dict[str, Any]]:
    got = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"formOver"')]
    assert got and all(g == {"formOver": []} for g in got), got
    return got


@needs_browser
@pytest.mark.parametrize("w,h,dpr,mobile", WIDTHS)
def test_the_real_servers_new_codes_each_read_their_own_line_with_the_extras_it_sends(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, w: int, h: int, dpr: int, mobile: bool) -> None:
    """RED on the page as merged: the day refusal (bad_agenda_day, 422) read "That did not go through (bad_agenda_day).", the
    planted-unanswered 503 the same with its own code, and the Keep's kept_not_closed 503 the same: three raw code strings, and no
    word of the day the agenda stands on or the episode that was kept. Real server, real faults (the answer act locked at the seam
    after the plant; the compost down after the episode landed)."""
    from levain import writes

    root, src, _ids, (qs, q, delivered) = _world(tmp_path, monkeypatch)
    inside = (date.today() + timedelta(days=2)).isoformat()
    past_day = date.today() + timedelta(days=9)

    def locked(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError(f"database is locked: {A_PATH}")

    def compost_down(*_a: Any, **_k: Any) -> Any:
        raise OSError(f"disk I/O error at {A_PATH}")

    monkeypatch.setattr(band_agenda, "_answer_deferred", locked)
    monkeypatch.setattr(writes, "_keep_compost", compost_down)
    seed = _row_of(SEED)
    row = _row_of("levain: pick the release date")
    btn = f"{row}.querySelector('[data-verb=band_agenda]')"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": w, "h": h, "dpr": dpr, **({"mobile": True} if mobile else {})}},
            *_reload(),
            {"until": f"{TRAY} && {seed} && {row} && {btn} && 1"},
            _day_words_step(past_day),
            {"eval": f"{btn}.click(); 1"},
            {"until": f"{_day_input(row)} && 1"},
            _pick(row, inside), _confirm(row), _failed(row), _form_overflow(row),             # a day in the snooze window: bad_agenda_day
            _pick(row, past_day.isoformat()), _confirm(row), _failed(row), _form_overflow(row),   # past it, the answer act locked
            *_open_keep(seed), _confirm(seed), _failed(seed), _form_overflow(seed),           # the Keep, the compost down
        ])
    day_refusal, planted, kept = _texts(out)[-3:]
    assert day_refusal.startswith(inside) and "snooze window" in day_refusal, day_refusal      # the server's day sentence, by its code
    assert planted == _planted_line(_day_words(out, past_day)[0]), planted
    kept_id = next(i for i, c in _keep_episodes(src) if c.startswith(SEED + "\n"))
    assert kept == f"Kept as a memory (episode {kept_id}){KEPT_503_TAIL}", kept                # the real episode, in the receipt's shape
    for text in (day_refusal, planted, kept):
        assert A_PATH not in text and "locked" not in text and "disk I/O" not in text and "bad_agenda_day" not in text, text
        assert "kept_not_closed" not in text and "agenda_planted_unanswered" not in text, text
    _no_overflow(out)
    agendas = [s for s in IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").list_open()
               if str(s.get("origin_key", "")).startswith(band_agenda.KEY_PREFIX)]
    assert len(agendas) == 1 and qs.get(q.id).answer is None                      # planted once, the row still open (what the line says)


@needs_browser
@pytest.mark.parametrize("w,h,dpr,mobile", WIDTHS)
def test_each_shape_the_new_codes_can_arrive_in_reads_one_fixed_line_and_a_moved_keep_reads_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, w: int, h: int, dpr: int, mobile: bool) -> None:
    """RED on the page as merged: every one read "That did not go through (<code>)." except source_unavailable, which read the
    unsure-agenda line although nothing was planted. The server's own words (they can carry a raw exception) never show; an
    extra that is not in the shape the page expects (a day, an episode id) is left out of the line, never printed."""
    from levain import writes

    _root, src, _ids, _band = _world(tmp_path, monkeypatch)
    far = date(2036, 2, 29)
    raw = "RAW-SERVER-TEXT " + A_PATH
    own_sentence = "an agenda's day is written YYYY-MM-DD: 'x' is not a date"
    _refuse_in_order(monkeypatch, band_agenda, "defer", [
        ("bad_agenda_day", 422, raw),                                                          # not the host's own sentence: the fixed line
        ("bad_agenda_day", 422, own_sentence),                                                 # the host's own sentence: shown as sent
        ("bad_agenda_day", 400, own_sentence),                                                 # the same sentence under another status: the fixed line
        ("agenda_planted_unanswered", 503, "agenda intention-009 is planted. " + raw, {"agenda": "intention-009", "day": far.isoformat()}),
        ("agenda_planted_unanswered", 503, raw, {"agenda": "intention-009", "day": "not a day " + A_PATH}),
        ("agenda_planted_unanswered", 503, raw),
        ("source_unavailable", 503, "the row's store cannot be read, nothing was planted " + raw)])
    _refuse_in_order(monkeypatch, writes, "_apply_intention_keep", [
        ("kept_not_closed", 503, raw, {"episode_id": "ep-503"}),
        ("kept_not_closed", 503, raw, {"episode_id": "bad id " + A_PATH}),
        ("kept_not_closed", 503, raw),
        ("kept_not_closed", 500, raw, {"episode_id": "ep-500"}),                               # neither status the server sends: no outcome promised
        ("kept_not_closed", 409, raw, {"episode_id": "ep-409"})])
    seed = _row_of(SEED)
    row = _row_of("levain: pick the release date")
    btn = f"{row}.querySelector('[data-verb=band_agenda]')"
    steps: list[dict[str, Any]] = [
        {"emulate": {"w": w, "h": h, "dpr": dpr, **({"mobile": True} if mobile else {})}},
        *_reload(),
        {"until": f"{TRAY} && {seed} && {row} && {btn} && 1"},
        _day_words_step(far),
        {"eval": f"{btn}.click(); 1"},
        {"until": f"{_day_input(row)} && 1"},
        _pick(row, (date.today() + timedelta(days=9)).isoformat())]
    for _ in range(7):
        steps += [_confirm(row), _failed(row), _form_overflow(row)]
    steps += [                                    # the agenda form is closed first: an open form with a day in it holds every re-read
        {"eval": f"{row}.querySelector('.verb-form .verb-cancel').click(); 1"},
        {"until": f"!{row}.querySelector('.verb-form') && 1"},
        # every manifest read is counted when it STARTS and, while `__hold` is a promise, waits for it: the 409 starts a re-read at once
        # (the Keep's form holds no text, so nothing holds it off) and the redraw replaces the form, so the old form's lines can only be
        # read deterministically if the read is held until they have been
        {"eval": "window.__gets = []; window.__hold = null; const f = window.fetch.bind(window); window.fetch = (u, o) => { "
                 "if (/\\/cockpit\\/manifest\\.json/.test(String(u))) { window.__gets.push(String(u)); "
                 "if (window.__hold) return window.__hold.then(() => f(u, o)); } return f(u, o); }; 1"},
        *_open_keep(seed)]
    for _ in range(4):                                                                      # the 503s and the 500: the form stays, nothing re-reads
        steps += [_confirm(seed), _failed(seed), _form_overflow(seed)]
    cancel = {"eval": f"{seed}.querySelector('.verb-form .verb-cancel').click(); 1"}
    steps += [cancel, {"until": f"!{seed}.querySelector('.verb-form') && 1"},
              {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({gets: window.__gets.length, urls: window.__gets})), 900))"},
              {"eval": "window.__hold = new Promise(r => { window.__release = r; }); 1"},
              *_open_keep(seed), _confirm(seed), _failed(seed), _form_overflow(seed),       # the 409: the item moved; its re-read is held
              {"until": "window.__gets.length > 0"},
              {"eval": f"JSON.stringify({{gets: window.__gets.length, urls: window.__gets, formStillOpen: !!{seed}.querySelector('.verb-form')}})"},
              {"eval": "window.__release(); window.__hold = null; 1"},
              {"until": f"!{seed}.querySelector('.verb-form') && 1"},                       # the redraw replaced the form ...
              {"eval": f"JSON.stringify({{gets: window.__gets.length, urls: window.__gets, "
                       f"noticeOnRow: !!{seed} && {seed}.textContent.includes('Kept as a memory (episode ep-409)')}})"}]   # ... the notice stays
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", steps)
    seen = [json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith('{"gets"')]
    gets = [g["gets"] for g in seen]
    lines = _texts(out)[-12:]
    assert lines == [
        BAD_DAY_LINE, own_sentence, BAD_DAY_LINE, _planted_line(_day_words(out, far)[0]), PLANTED_NO_DAY_LINE, PLANTED_NO_DAY_LINE,
        GENERIC_STORE_LINE,
        "Kept as a memory (episode ep-503)" + KEPT_503_TAIL, "Kept as a memory" + KEPT_503_TAIL, "Kept as a memory" + KEPT_503_TAIL,
        "Kept as a memory (episode ep-500)" + KEPT_OTHER_TAIL,
        "Kept as a memory (episode ep-409)" + KEPT_409_TAIL], lines
    assert not any("RAW-SERVER-TEXT" in x or A_PATH in x for x in lines)
    assert "Nothing was added" in lines[6] and "planted" not in lines[6]                    # nothing was planted: the plain line is the true one
    assert "safe" not in lines[10] and "still on the List" not in lines[10] and "Reload" not in lines[10]   # no outcome promised
    assert len(seen) == 3 and gets[0] == 0, seen                                           # a 503 or 500 Keep (the item stays) did not re-read
    assert gets[1] >= 1 and seen[1]["formStillOpen"], seen                                 # the 409's re-read started while the old form was read
    assert gets[2] >= 1 and seen[2]["noticeOnRow"] is True, seen                           # the redraw came, and the notice is on the row
    _no_overflow(out)
