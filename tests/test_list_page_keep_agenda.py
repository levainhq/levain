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
from tests.test_list_page_browser import TRAY, _posts, _reload, _seed_more

SEED = "a tray seed"
BAND_WORDS = "levain: pick the release date for the fellowship essay draft?"
ROWS = f"[...{TRAY}.querySelectorAll('.sp-results .row')]"
KEPT = f"{TRAY}.querySelector('[data-list-kept]')"
SEARCH = f"{TRAY}.querySelector('.ep-search-input')"


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any, dict[str, str], Any]:
    """An install with the List's three stores, one plain band row (filed with no session: its answer reaches no agent) and
    one earlier Keep in memory. The delivery to a stopped agent is replaced by a recorder."""
    root, src, ids = _install(tmp_path)
    _seed_more(root)
    delivered: list[Any] = []
    monkeypatch.setattr(band, "deliver_answer", lambda *a, **k: delivered.append(a))
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
    assert refusal and refusal.startswith(inside) and "snooze the row instead" in refusal, v    # the server's own words, not a fixed line
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
