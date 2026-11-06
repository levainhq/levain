"""The Needs-you section at a phone's width (Phill 11-06): two tabs in that area (Needs you / Desk owes), and two layouts to judge on a
running page, `?needs=rows` (dense one-column rows with a fold that states its hidden count and oldest age) and `?needs=tiles`
(two-column fixed-height tiles, nothing folded). Either way a tap opens the item's detail as a sheet over the scroll position it was
tapped at, with focus inside, Escape closing it, and the page's scroll and focus handed back."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain import web_server
from levain.consent import questions as qmod
from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

PHONE = {"w": 390, "h": 844, "dpr": 2, "mobile": True}
YOU, DESK, OLD = 22, 4, 8      # open rows in the Needs tab, in the Desk tab; of the Needs rows, how many are over a day old
TITLES = ["Ship it?", "Which way should the long decision go? " + " ".join(f"Clause {i} carries more of the analysis." for i in range(10)),
          "Medium one: keep the plan or cut it back to the first slice?", "Approve the scratch seat from your phone, it needs you off the Mac?"]


def _seed(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    now = datetime.now(timezone.utc)
    at = {"t": now}
    store = QuestionStore(home / "consent", clock=lambda: at["t"])
    for i in range(YOU + DESK):
        desk = i >= YOU
        old = (not desk) and i < OLD
        at["t"] = now - (timedelta(days=2, minutes=i) if old else timedelta(minutes=5 + i))
        what = ("Desk owes: " if desk else "") + TITLES[i % len(TITLES)] + f" ({i})"
        store.file(agent="codex" if i % 3 == 1 else "claude_code", agent_session_id=("desk-new" if desk else "you") + f"-{i}", what=what,
                   options=[("a", "Yes"), ("b", "No"), ("c", "Later")][: 2 + i % 2], rec="a", blocks=f"blocks {i}", cwd="/w")
    orig = qmod.QuestionStore._question_of

    def with_section(row):
        q = orig(row)
        d = q.agent_session_id.startswith("desk")
        return dataclasses.replace(q, section="desk" if d else "you", carried=False)

    monkeypatch.setattr(qmod.QuestionStore, "_question_of", staticmethod(with_section))


def _flag(monkeypatch, on: bool) -> None:
    real = web_server._Handler._transport      # the manifest's transport is where a page learns the scratch launcher's flag
    monkeypatch.setattr(web_server._Handler, "_transport", lambda self: dict(real(self), **({"mockup_layout_switch": True} if on else {})))


STRIP = "document.getElementById('ck-needs')"
ROWS = "[...document.querySelectorAll('#ck-needs .needs-row')]"
READY = f"{ROWS}.length >= 6 && /Needs you \\({YOU}\\)/.test({STRIP}.innerText) && /Desk owes \\({DESK}\\)/.test({STRIP}.innerText)"
SETTLE = {"eval": "new Promise(r => setTimeout(() => r(1), 500))"}


def _run(tmp_path: Path, monkeypatch, query: str, steps: list, flag: bool = False, sub: str = "") -> list:
    base = tmp_path / sub if sub else tmp_path
    base.mkdir(exist_ok=True)
    _seed(monkeypatch, base / "levain_home")
    _flag(monkeypatch, flag)
    _root, src, _ids = _install(base)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(base, f"{url}/{query}#write_token=T", [{"emulate": PHONE}, {"until": READY, "ms": 30000}, SETTLE, *steps])["values"]
    return [json.loads(v) if isinstance(v, str) and v[:1] in "{[" else v for v in vals]


@needs_browser
def test_rows_show_six_then_a_fold_that_states_its_hidden_count_and_oldest_age(tmp_path: Path, monkeypatch) -> None:
    probe = (f"JSON.stringify({{rows: {ROWS}.length, more: (document.querySelector('#ck-needs .needs-more-row') || {{}}).textContent,"
             f" tabs: [...document.querySelectorAll('#ck-needs .needs-tab')].map(t => t.textContent),"
             f" groups: [...document.querySelectorAll('#ck-needs .needs-group')].map(t => t.textContent),"
             f" heights: {ROWS}.map(r => Math.round(r.getBoundingClientRect().height)),"
             " cols: getComputedStyle(document.querySelector('#ck-needs .needs-grid')).gridTemplateColumns,"
             " scrollW: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth,"
             " lines: [...document.querySelectorAll('#ck-needs .needs-row-title')].map(t => getComputedStyle(t).webkitLineClamp)})")
    steps = [{"eval": probe},
             {"do": "document.querySelector('#ck-needs .needs-more-row').click()", "until": f"{ROWS}.length === {YOU}"},
             {"eval": probe}]
    folded, opened = [v for v in _run(tmp_path, monkeypatch, "?needs=rows", steps) if isinstance(v, dict)]
    assert folded["tabs"] == [f"Needs you ({YOU})", f"Desk owes ({DESK})"], folded
    assert folded["rows"] == 6, folded
    # the fold's label carries the hidden count and the oldest hidden age, so a row never hides unseen
    assert folded["more"] == f"Show {YOU - 6} more, oldest 2 d", folded
    assert folded["groups"] == [f"Today ({YOU - OLD})", f"Older than a day ({OLD})"][: len(folded["groups"])], folded
    assert all(56 <= h <= 80 for h in folded["heights"]), folded           # dense rows about 64 pt
    assert folded["scrollW"] <= folded["vw"], folded
    assert set(folded["lines"]) == {"2"}, folded                          # titles clamp to two lines
    assert opened["rows"] == YOU and opened["more"] == "Show fewer", opened


@needs_browser
def test_tiles_are_two_columns_with_every_item_shown_and_clamped_titles(tmp_path: Path, monkeypatch) -> None:
    probe = (f"JSON.stringify({{rows: {ROWS}.length, more: !!document.querySelector('#ck-needs .needs-more-row'),"
             " cols: getComputedStyle(document.querySelector('#ck-needs .needs-grid')).gridTemplateColumns.split(' ').length,"
             f" heights: {ROWS}.map(r => Math.round(r.getBoundingClientRect().height)),"
             f" lefts: [...new Set({ROWS}.map(r => Math.round(r.getBoundingClientRect().left)))],"
             " scrollW: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth,"
             " lines: [...new Set([...document.querySelectorAll('#ck-needs .needs-row-title')].map(t => getComputedStyle(t).webkitLineClamp))],"
             f" overflow: {ROWS}.filter(r => r.scrollHeight > r.clientHeight + 1).length}})")
    got = [v for v in _run(tmp_path, monkeypatch, "?needs=tiles", [{"eval": probe}]) if isinstance(v, dict)][0]
    assert got["cols"] == 2 and len(got["lefts"]) == 2, got
    assert got["rows"] == YOU and not got["more"], got                     # nothing folded
    assert max(got["heights"]) - min(got["heights"]) <= 1 and min(got["heights"]) >= 44, got   # a regular grid, tap targets
    assert got["lines"] == ["2"] and got["scrollW"] <= got["vw"], got
    assert got["overflow"] == 0, got                                       # no tile clips its own text


@needs_browser
def test_the_tabs_swap_the_list_from_the_view_already_held_and_are_remembered(tmp_path: Path, monkeypatch) -> None:
    subs = "performance.getEntriesByType('resource').filter(r => /substrate\\.json/.test(r.name)).length"
    steps = [{"eval": subs},
             {"do": "document.querySelector('#ck-needs .needs-tab[data-needs-tab=desk]').click()",
              "until": f"{ROWS}.length === {DESK} && {ROWS}.every(r => /Desk owes/.test(r.title || r.textContent) || r.textContent.length > 0)"},
             {"eval": subs},
             {"eval": "JSON.stringify({sel: [...document.querySelectorAll('#ck-needs .needs-tab')].map(t => t.getAttribute('aria-selected')),"
                      f" more: !!document.querySelector('#ck-needs .needs-more-row'), kept: localStorage.getItem('levain.needs.tab'), n: {ROWS}.length}})"}]
    vals = _run(tmp_path, monkeypatch, "?needs=rows", steps)
    before, after = [v for v in vals if isinstance(v, int)][:2]
    got = [v for v in vals if isinstance(v, dict)][0]
    assert after == before, (before, after)                                # a tab change is a re-render, not a fetch of the view
    assert got["sel"] == ["false", "true"] and got["kept"] == "desk" and got["n"] == DESK and not got["more"], got


@needs_browser
def test_the_layout_switch_is_drawn_only_on_a_page_served_with_the_mockup_flag(tmp_path: Path, monkeypatch) -> None:
    has = "!!document.querySelector('#ck-needs .needs-layout-sw')"
    assert [v for v in _run(tmp_path, monkeypatch, "", [{"eval": has}], flag=False, sub="off") if isinstance(v, bool)][-1] is False
    steps = [{"eval": "JSON.stringify({sw: " + has + ", on: [...document.querySelectorAll('.needs-layout-btn.is-on')].map(b => b.textContent)})"},
             {"do": "document.querySelector('.needs-layout-btn[data-needs-layout=tiles]').click()",
              "until": "document.querySelector('#ck-needs .needs-grid').dataset.layout === 'tiles'"},
             {"eval": "JSON.stringify({kept: localStorage.getItem('levain.needs.layout'), url: location.search, rows: " + ROWS + ".length})"}]
    first, second = [v for v in _run(tmp_path, monkeypatch, "", steps, flag=True, sub="on") if isinstance(v, dict)]
    assert first == {"sw": True, "on": ["rows"]}, first                    # default rows, the switch drawn
    assert second == {"kept": "tiles", "url": "?needs=tiles", "rows": YOU}, second


DETAIL = f"""(() => {{
  const m = document.querySelector('#levain-modal .modal'); if (!m) return null; const r = m.getBoundingClientRect();
  return JSON.stringify({{top: r.top, bottom: r.bottom, left: r.left, right: r.right, vh: innerHeight, vw: innerWidth,
    focusIn: document.querySelector('#levain-modal').contains(document.activeElement), locked: document.body.style.position,
    options: document.querySelectorAll('#levain-modal .question-option').length}}); }})()"""


def _detail_steps(layout: str) -> list:
    fold = [{"do": "(document.querySelector('#ck-needs .needs-more-row') || {click(){}}).click()", "until": f"{ROWS}.length === {YOU}"}] if layout == "rows" else []
    last = f"{ROWS}[{ROWS}.length - 1]"
    return [*fold,
            # the last row lands at the foot of the viewport, far below the page's top; the scroll the tap itself arrives at is read in
            # a capture-phase click listener, the moment before the sheet opens
            {"eval": f"{last}.scrollIntoView({{block: 'end'}}); addEventListener('click', () => {{ window.__y = Math.round(scrollY); }}, true); 1"}, SETTLE,
            {"tap": last, "ms": 15000},
            {"until": "document.querySelector('#levain-modal .question-option')", "ms": 20000}, SETTLE,   # the sheet's rise is over
            {"eval": f"JSON.stringify({{y: window.__y, last: {last}.dataset.needsId}})"},
            {"eval": DETAIL},
            {"key": "Escape"},
            {"until": "!document.getElementById('levain-modal')", "ms": 10000}, SETTLE,
            {"eval": "JSON.stringify({y: Math.round(scrollY), active: (document.activeElement.dataset || {}).needsId || null, locked: document.body.style.position})"}]


def _check_detail(vals: list) -> None:
    dicts = [v for v in vals if isinstance(v, dict)]
    before = next(d for d in dicts if "last" in d)
    sheet = next(d for d in dicts if "top" in d)
    after = next(d for d in dicts if "active" in d)
    assert before["y"] > 300, before                                       # the page really was scrolled down
    assert sheet["top"] >= 0 and sheet["bottom"] <= sheet["vh"] + 1 and sheet["left"] >= 0 and sheet["right"] <= sheet["vw"] + 1, sheet
    assert sheet["focusIn"] and sheet["locked"] == "fixed" and sheet["options"] >= 2, sheet
    assert abs(after["y"] - before["y"]) <= 2, (before, after)             # the scroll position comes back
    assert after["active"] == before["last"] and after["locked"] == "", (before, after)   # and so does the focus, on the tapped row


@needs_browser
def test_a_tap_on_the_last_row_opens_the_rows_detail_in_view_and_closing_gives_the_place_back(tmp_path: Path, monkeypatch) -> None:
    _check_detail(_run(tmp_path, monkeypatch, "?needs=rows", _detail_steps("rows")))


@needs_browser
def test_a_tap_on_the_last_tile_opens_the_tiles_detail_in_view_and_closing_gives_the_place_back(tmp_path: Path, monkeypatch) -> None:
    _check_detail(_run(tmp_path, monkeypatch, "?needs=tiles", _detail_steps("tiles")))
