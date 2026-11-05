"""A flow band card's title is a short human handle, never the long card number. The wire row carries no words (they are
fetched with the page's token), so the dialog title and the glance tile both come from ONE helper (`needsName`): the first
words of the question once read, `<agent> · question` before that (and when the page holds no token). The number stays the
answer key (`q.n`): the focus view's `levain question show <n>` line and the answer POST still carry it."""
from __future__ import annotations

import json
import time
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k4a_needs import DESKTOP

SHORT = "Ship the sweep fix?"
LONG = "Should the cockpit title branch merge before Friday's release window opens, given the review?"
# a card number of the size flow's desk_cards makes: 1 + int(hex of the np id, 16)
BIG_N = 3_040_104_907

TITLE = "(document.getElementById('levain-modal-title') || {}).textContent"


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
            # the tile's name for the top item goes through the SAME helper: asked of it directly, with a card number of flow's size
            {"eval": "JSON.stringify({tile: window.LevainNeeds.name({kind: 'question', q: {n: 1, agent: 'claude_code'}}),"
                     f" big: window.LevainNeeds.name({{kind: 'question', q: {{n: {BIG_N}, agent: 'claude_code'}}}}),"
                     " consent: window.LevainNeeds.name({kind: 'consent', c: {tool: 'Bash', agent: 'codex'}})})"}])["values"]
    first, second, tile = json.loads(out[-7]), json.loads(out[-3]), json.loads(out[-1])
    assert first["dlg1"] == "Ship the sweep fix? · claude_code", first
    assert "#" not in first["dlg1"] and first["aria"].startswith("Ship the sweep fix?"), first
    assert first["show"] == "levain question show 1", first            # the number is still the answer key, shown where it is a command
    assert second["dlg2"] == "Should the cockpit title branch merge… · codex", second   # six words on a word boundary, then an ellipsis
    assert tile["tile"] == "Ship the sweep fix? · claude_code", tile
    assert tile["big"] == "claude_code · question", tile            # a question whose words were never read: number-free, agent first
    assert tile["consent"] == "Bash · codex", tile                  # a consent keeps its tool · agent title


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
            {"eval": f"JSON.stringify({{title: {TITLE}, tile: window.LevainNeeds.name({{kind: 'question', q: {{n: 1, agent: 'claude_code'}}}})}})"}])["values"]
    got = json.loads(out[-1])
    assert got == {"title": "claude_code · question", "tile": "claude_code · question"}, got


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
