"""A flow band card's title is a short human handle, never the long card number. The wire row carries no words (they are
fetched with the page's token), so the dialog title and the glance tile both come from ONE helper (`needsName`): the first
words of the question once read, `<agent> · question` before that (and when the page holds no token). The number stays the
answer key (`q.n`): the focus view's `levain question show <n>` line and the answer POST still carry it. The strip row, the
bar chip and the tile never name a question by that number; the words are the question's own, so they are shown only to a page
that holds the token they were read with, and a name learned after a tile was drawn is painted into that tile."""
from __future__ import annotations

import json
import time
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_band_pages import BAND_ROWS, HISTORY
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

SHORT = "Ship the sweep fix?"
LONG = "Should the cockpit title branch merge before Friday's release window opens, given the review?"
# a card number of the size flow's desk_cards makes: 1 + int(hex of the np id, 16)
BIG_N = 3_040_104_907

TITLE = "(document.getElementById('levain-modal-title') || {}).textContent"
# the glance tile's name line, as drawn on the page; the strip rows' title lines
TILE_TOP = "([...document.querySelectorAll('.ck-widgets .glance-top')].map(t => t.textContent)[0] || '')"
ROW_TITLES = "[...document.querySelectorAll('#ck-needs .needs-row-title')].map(t => t.textContent)"
CHIP_TEXTS = "[...document.querySelectorAll('#ck-needs .needs-chip-btn')].map(t => t.textContent)"
TOKEN_KEY = "levain_write_token"
# the page drops its token the way the app does (storage, then the announce event) and the same turn reads what it drew
DROP_TOKEN = f"window.localStorage.removeItem('{TOKEN_KEY}'); window.dispatchEvent(new Event('levain:write-token-change'));"


def _two_questions(tmp_path: Path, monkeypatch) -> tuple[Path, QuestionStore]:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    store.file(agent="claude_code", agent_session_id="s1", what=SHORT, options=[("ship", "Ship it"), ("hold", "Hold it")],
               rec="ship", blocks="the sweep stays red", cwd="/w")
    store.file(agent="codex", agent_session_id="s2", what=LONG, options=[("go", "Go"), ("wait", "Wait")], rec="wait",
               blocks="the release", cwd="/w")
    return home, store


def _open(n: int) -> str:
    return f"document.querySelector('#ck-needs .needs-row[data-needs-id=\"question:{n}\"]').click(); 1"


@needs_browser
def test_the_dialog_title_is_the_questions_first_words_and_the_tile_uses_the_same_helper(tmp_path: Path, monkeypatch) -> None:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP,
            {"until": "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1", "ms": 30000},
            {"until": "[...document.querySelectorAll('#ck-needs .needs-row-title')].every(t => !/^question #/.test(t.textContent) && t.textContent.length > 12) && 1", "ms": 30000},
            {"eval": _open(1)},
            {"until": f"/^Ship the sweep fix\\? \\u00b7 claude_code$/.test({TITLE}) && 1", "ms": 30000},
            {"eval": f"JSON.stringify({{dlg1: {TITLE}, aria: document.querySelector('#levain-modal .modal-body').getAttribute('aria-label'),"
                     " show: (document.querySelector('#levain-modal .needs-terminal .consent-code') || {}).textContent})"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
            {"eval": _open(2)},
            {"until": f"/\\u00b7 codex$/.test({TITLE}) && !/^codex/.test({TITLE}) && 1", "ms": 30000},
            {"eval": f"JSON.stringify({{dlg2: {TITLE}}})"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
            # the tile that is ON THE PAGE learns the name when the words land (it was drawn before them), not only the helper asked directly
            {"until": f"/^Ship the sweep fix\\? \\u00b7 claude_code$/.test({TILE_TOP}) && 1", "ms": 30000},
            {"eval": f"JSON.stringify({{page: {TILE_TOP}, rows: {ROW_TITLES}}})"},
            # and the same helper, asked directly, with a card number of flow's size and with a consent
            {"eval": f"JSON.stringify({{big: window.LevainNeeds.name({{kind: 'question', q: {{id: 'question:{BIG_N}', n: {BIG_N}, agent: 'claude_code'}}}}),"
                     " consent: window.LevainNeeds.name({kind: 'consent', c: {tool: 'Bash', agent: 'codex'}})})"}])["values"]
    first, second, page, direct = json.loads(out[-9]), json.loads(out[-5]), json.loads(out[-2]), json.loads(out[-1])
    assert first["dlg1"] == "Ship the sweep fix? · claude_code", first
    assert "#" not in first["dlg1"] and first["aria"].startswith("Ship the sweep fix?"), first
    assert first["show"] == "levain question show 1", first            # the number is still the answer key, shown where it is a command
    assert second["dlg2"] == "Should the cockpit title branch merge… · codex", second   # six words on a word boundary, then an ellipsis
    assert page["page"] == "Ship the sweep fix? · claude_code", page   # the tile on the page, read after the words landed
    assert not any(t.startswith("question #") for t in page["rows"]), page
    assert direct["big"] == "claude_code · question", direct        # a question whose words were never read: number-free, agent first
    assert direct["consent"] == "Bash · codex", direct              # a consent keeps its tool · agent title


@needs_browser
def test_without_a_token_the_words_are_not_read_and_the_title_has_no_number(tmp_path: Path, monkeypatch) -> None:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [DESKTOP,
            {"until": "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1", "ms": 30000},
            {"eval": _open(1)},
            {"until": f"{TITLE} && 1", "ms": 30000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 800))"},
            {"eval": f"JSON.stringify({{title: {TITLE}, tile: {TILE_TOP}, rows: {ROW_TITLES}, chips: {CHIP_TEXTS}}})"}])["values"]
    got = json.loads(out[-1])
    assert got["title"] == "claude_code · question" and got["tile"] == "claude_code · question", got
    # the strip's rows (and the bar's chips, where drawn) never fall back to the card number either: nothing is read, so each is the agent-only name
    assert got["rows"] == ["claude_code · question", "codex · question"], got
    assert all(not c.startswith("question #") for c in got["chips"]), got


@needs_browser
def test_the_answer_path_still_sends_the_numbered_question(tmp_path: Path, monkeypatch) -> None:
    _home, store = _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP,
            {"until": "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1", "ms": 30000},
            {"eval": _open(1)},
            {"until": "document.querySelector('#levain-modal .question-option') && 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#levain-modal .question-option')].find(b => /^hold/.test(b.textContent)).click(); 1"},
            {"until": "!document.getElementById('levain-modal') && 1", "ms": 30000}])
        deadline = time.monotonic() + 20
        got = store.get(1)
        while (got is None or got.answer is None) and time.monotonic() < deadline:
            time.sleep(0.1)
            got = store.get(1)
    assert got is not None and got.answer is not None and got.answer.value == "hold", got
    assert store.get(2).answer is None


# every read of a question's words waits 1.5 s, so a focus view opened at once is opened before they have landed
SLOW_WORDS = ("const f = window.fetch.bind(window); window.fetch = (u, o) => { const s = String(u && u.url || u);"
              " return /\\/cockpit\\/question\\//.test(s) ? new Promise((r) => setTimeout(r, 1500)).then(() => f(u, o)) : f(u, o); };")


@needs_browser
def test_a_dialog_opened_before_the_words_land_is_retitled_when_they_do(tmp_path: Path, monkeypatch) -> None:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"init": SLOW_WORDS},
            {"until": "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1", "ms": 30000},
            {"eval": _open(1)},
            {"until": f"{TITLE} && 1", "ms": 30000},
            {"eval": f"JSON.stringify({{before: {TITLE}}})"},
            {"until": f"/^Ship the sweep fix\\? \\u00b7 claude_code$/.test({TITLE}) && 1", "ms": 30000},
            {"eval": f"JSON.stringify({{after: {TITLE}, aria: document.querySelector('#levain-modal .modal-body').getAttribute('aria-label')}})"}])["values"]
    before, after = json.loads(out[-3]), json.loads(out[-1])
    assert before == {"before": "claude_code · question"}, before
    assert after["after"] == "Ship the sweep fix? · claude_code" and after["aria"].startswith("Ship the sweep fix?"), after


def _name(n: int, agent: str = "claude_code") -> str:
    """The shared helper asked for question n as the wire carries it (id and number)."""
    return f"window.LevainNeeds.name({{kind: 'question', q: {{id: 'question:{n}', n: {n}, agent: '{agent}'}}}})"


FIRST_WORDS = f"/^Ship the sweep fix\\? \\u00b7 claude_code$/.test({TILE_TOP}) && 1"


@needs_browser
def test_the_words_name_the_question_only_while_the_page_holds_the_token_they_were_read_with(tmp_path: Path, monkeypatch) -> None:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP,
            {"until": FIRST_WORDS, "ms": 30000},
            {"eval": f"JSON.stringify({{name: {_name(1)}, tile: {TILE_TOP}, asString: {_name(1).replace('n: 1', 'n: ' + chr(39) + '1' + chr(39))}}})"},
            # the token goes (storage, then the announce event) and the same turn reads what the helper and the tile now say
            {"eval": f"(() => {{ {DROP_TOKEN} return JSON.stringify({{name: {_name(1)}, tile: {TILE_TOP}}}); }})()"}])["values"]
    held, dropped = json.loads(out[-2]), json.loads(out[-1])
    # one string key whether the number arrives as a number or as a string
    assert held == {"name": "Ship the sweep fix? · claude_code", "tile": "Ship the sweep fix? · claude_code",
                    "asString": "Ship the sweep fix? · claude_code"}, held
    assert dropped == {"name": "claude_code · question", "tile": "claude_code · question"}, dropped


@needs_browser
def test_a_read_that_lands_after_the_token_went_keeps_no_name_and_the_next_read_learns_it(tmp_path: Path, monkeypatch) -> None:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"init": SLOW_WORDS},
            {"until": "document.querySelectorAll('#ck-needs .needs-row').length === 2 && 1", "ms": 30000},
            # both reads are in flight with the token they were started under; the token goes before they land
            {"eval": f"{DROP_TOKEN} 1"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 4500))"},
            {"eval": f"window.localStorage.setItem('{TOKEN_KEY}', 'T'); window.dispatchEvent(new Event('levain:write-token-change')); JSON.stringify({{name: {_name(1)}, tile: {TILE_TOP}}})"},
            {"eval": _open(1)},
            {"until": f"/^Ship the sweep fix\\? \\u00b7 claude_code$/.test({TITLE}) && 1", "ms": 30000}])["values"]
    back = json.loads(out[-3])
    # the late read stored no name (the token it was read under had gone), and was not cached as read: opening the item reads again
    assert back == {"name": "claude_code · question", "tile": "claude_code · question"}, back


@needs_browser
def test_a_reached_band_number_is_in_the_name_of_a_question_whose_words_are_not_read(tmp_path: Path, monkeypatch) -> None:
    _two_questions(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    hang = ("const f = window.fetch.bind(window); window.fetch = (u, o) => /\\/cockpit\\/question\\//.test(String(u && u.url || u))"
            " ? new Promise(() => {}) : f(u, o);")
    with _serve(src, write_token=TOKEN) as (base, _httpd):
        out = _drive(tmp_path, f"{base}/#write_token={TOKEN}", [DESKTOP, {"init": hang},
            {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000},
            {"eval": HISTORY},
            {"until": f"{BAND_ROWS} === 2 && 1", "ms": 30000},
            {"until": "[...document.querySelectorAll('[data-question-row] .band-num')].every(b => !b.hidden && b.textContent) && 1", "ms": 30000},
            {"eval": f"JSON.stringify([{_name(1)}, {_name(2, 'codex')}])"}])["values"]
    # bandNumOf is keyed by the band read's integer question number, the key the name looks up with
    assert json.loads(out[-1]) == ["claude_code · question · band 1", "codex · question · band 2"], out[-1]


@needs_browser
def test_words_that_are_only_marks_make_no_handle(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    marks = "(" * 70
    QuestionStore(home / "consent").file(agent="claude_code", agent_session_id="s1", what=marks, options=[("a", "A"), ("b", "B")],
                                         rec="a", blocks="x", cwd="/w")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP,
            {"until": f"[...document.querySelectorAll('#ck-needs .needs-row-title')].some(t => t.textContent === '{marks}') && 1", "ms": 30000},
            {"eval": _open(1)},
            {"until": f"{TITLE} && 1", "ms": 30000},
            {"eval": f"JSON.stringify({{title: {TITLE}, name: {_name(1)}}})"}])["values"]
    assert json.loads(out[-1]) == {"title": "claude_code · question", "name": "claude_code · question"}, out[-1]
