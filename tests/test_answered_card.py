"""The answer a decision was given stays on the cockpit card: one composed line (the option key and label, or his words; signed or
unsigned when known; when; via which channel) on a Decisions-panel row without a click, and on a Needs-you card for a short
window after the answer, where it counts for nothing. The line's helpers are run in node from the page source; the strip and the row
run in headless Chrome when it is installed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.consent.questions import Answer, QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
CORE = (WEB / "dashboard_core.js").read_text()
NODE = shutil.which("node")


def _slice(start: str, stop: str) -> str:
    return CORE[CORE.index(start):CORE.index(stop)]


def _node(expr: str) -> object:
    src = (_slice("function answerLine(", "  // `card` (the Needs-you strip's card)") +
           _slice("const NEEDS_ANSWERED_MINUTES", "  // the name a glance tile gives the top item"))
    out = subprocess.run([NODE, "-e", src + "\nconsole.log(JSON.stringify(" + expr + "));"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


OPTS = [{"key": "ship", "label": "Ship it tonight"}, {"key": "hold", "label": "Hold"}]
AT = "2026-11-01T14:05:00Z"


def test_answered_dataclass_keeps_five_positional_args_and_defaults_unknown() -> None:
    a = Answer("q1", "option", "ship", "cli", AT)
    assert a.signed is None and a.signer is None
    assert Answer("q1", "option", "ship", "phone", AT, True, "SHA256:x").signed is True


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_composed_line_for_an_option_words_and_each_signed_state() -> None:
    opt = _node('answerLine({kind: "option", value: "ship", label: "Ship it tonight", channel: "phone", signed: true, answered_at: "%s"}, [])' % AT)
    assert opt.startswith("Answered: ship · Ship it tonight · signed · ") and opt.endswith(" · via phone")
    fallback = _node('answerLine({kind: "option", value: "hold", channel: "cli", signed: false, answered_at: "%s"}, %s)' % (AT, json.dumps(OPTS)))
    assert fallback.startswith("Answered: hold · Hold · unsigned · ") and fallback.endswith("via cli")
    words = _node('answerLine({kind: "text", value: "not tonight, page me", channel: "cockpit", signed: null, answered_at: "%s"}, [])' % AT)
    assert words.startswith("Answered: not tonight, page me · ") and "signed" not in words and words.endswith("via cockpit")
    bare = _node('answerLine({kind: "text", value: "x", channel: "cli", answered_at: "garbage"}, [])')
    assert bare == "Answered: x · via cli"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_only_a_recent_decision_stays_on_the_strip() -> None:
    got = _node('["0s ago", "29m ago", "30m ago", "1h ago", "2d ago", "", null, "120s ago", "59s ago"].map((l) => decidedRecently(l))')
    assert got == [True, True, False, False, False, False, False, False, True]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_window_is_decided_from_the_stamp_against_the_live_clock_not_the_label() -> None:
    """A label computed at map time reads "29m ago" forever; the stamp against the clock does not."""
    got = _node('(() => { const at = new Date(Date.now() - 29 * 60000).toISOString(); const stale = new Date(Date.now() - 31 * 60000).toISOString();'
                ' return [decidedRecently("29m ago", at), decidedRecently("29m ago", stale), decidedRecently("", stale),'
                ' decidedRecently("", at), decidedRecently("0s ago", "garbage"), decidedLeftMs("garbage"), decidedLeftMs(at) > 0]; })()')
    assert got == [True, False, False, True, True, None, True]
    clocked = _node('(() => { globalThis.window = {LevainCockpitKinds: {serverNow: () => Date.parse("2026-11-01T15:00:00Z")}};'
                    ' return [decidedRecently("", "2026-11-01T14:40:00Z"), decidedRecently("", "2026-11-01T14:20:00Z")]; })()')
    assert clocked == [True, False]          # the server-adjusted clock, not the browser's, bounds the window


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_question_signature_changes_when_the_write_token_arrives() -> None:
    src = _slice("function hasWriteToken", "  // the name a glance tile gives the top item")
    out = subprocess.run([NODE, "-e", src + "\nconst it = {kind: 'question', q: {n: 4, state: 'answered'}};"
                          "globalThis.window = {LevainCockpitTransport: {headers: () => ({})}}; const a = needsKey(it);"
                          "globalThis.window = {LevainCockpitTransport: {headers: () => ({'X-Levain-Write-Token': 'T'})}}; const b = needsKey(it);"
                          "console.log(JSON.stringify([a !== b, a, b]));"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)[0] is True


def test_the_server_body_normalises_signed_and_reads_options_once() -> None:
    src = (WEB.parent.parent / "web_server.py").read_text()
    body = src[src.index("def _serve_question_body"):]
    body = body[:body.index("finally:")]
    assert body.count("q.options") == 1 and "opts = list(q.options)" in body
    assert '"signed": None if signed is None else bool(signed)' in body


def test_a_strip_of_only_answered_cards_takes_no_space_in_build_and_memory() -> None:
    css = (WEB / "dashboard.css").read_text()
    for mode in ("build", "memory"):
        assert f'body.ke[data-mode="{mode}"] #ck-needs:not([data-waiting])' in css


OPTIONS = [("ship", "Ship it tonight"), ("hold", "Hold for the morning")]
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}


def _file(store: QuestionStore, what: str, agent: str = "claude_code"):
    return store.file(agent=agent, agent_session_id="s-" + what[:4], what=what, options=OPTIONS, rec="hold", blocks="the sweep", cwd="/w")


def _values(out: dict, marker: str) -> dict:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and marker in v][0]


def _home(tmp_path: Path, monkeypatch) -> QuestionStore:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    return QuestionStore(home / "consent")


# the selector the page's "Needs you" tab jumps to, read from the page source so the test follows it
JUMP_SELECTOR = re.search(r'needs: "([^"]+)"', CORE[CORE.index("const PHONE_JUMP_TARGET"):]).group(1)

# Both strip states read the page in build mode, where only the bar draws the strip: an answered card is not drawn there
STRIP = ("JSON.stringify({rows: document.querySelectorAll('#ck-needs .needs-row').length,"
         " headline: !!document.querySelector('#ck-needs .needs-headline'), pill: !!document.querySelector('#ck-needs .needs-count'),"
         " count: (document.querySelector('#ck-needs .needs-headline .needs-count') || {}).textContent || null,"
         " history: !!document.querySelector('#ck-needs .needs-history'),"
         " waiting: !!document.querySelector('#ck-needs[data-waiting]'),"
         " jump: !!document.querySelector(%s),"
         " cards: document.querySelectorAll('#ck-needs .needs-answered .needs-card').length,"
         " label: (document.querySelector('#ck-needs .needs-answered .needs-sec-label') || {}).textContent || null,"
         " ticking: document.querySelectorAll('#ck-needs .needs-settled-tag [data-age-at]').length,"
         " lines: [...document.querySelectorAll('#ck-needs .needs-answered .consent-note')].map(n => n.textContent),"
         " strip: 1})") % json.dumps(JUMP_SELECTOR)
BUILD_HIDES = ("(window.LevainCockpitKinds.applyMode('build', true), JSON.stringify({hidden: [...document.querySelectorAll('#ck-needs .needs-answered')]"
               ".every(n => getComputedStyle(n).display === 'none'), n: document.querySelectorAll('#ck-needs .needs-answered').length, build: 1}))")


@needs_browser
def test_an_answered_card_shows_its_answer_and_moves_no_count(tmp_path: Path, monkeypatch) -> None:
    store = _store = _home(tmp_path, monkeypatch)
    done = _file(store, "Ship the sweep fix tonight?")
    _file(store, "Which branch do we cut from?", agent="codex")
    store.answer(done.n, option="ship", channel="cli")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": "document.querySelector('#ck-needs .needs-answered .consent-note') && 1", "ms": 30000},
            {"eval": STRIP}, {"eval": BUILD_HIDES}])
    seen, build = _values(out, '"strip"'), _values(out, '"build"')
    assert seen["rows"] == 1 and seen["count"] == "1" and seen["cards"] == 1 and seen["waiting"] and seen["jump"], seen
    assert seen["label"] == "Just answered" and seen["ticking"] == 1, seen     # the age in the tag is the page's ticking one
    assert seen["lines"][0].startswith("Answered: ship \u00b7 Ship it tonight") and seen["lines"][0].endswith("via cli"), seen
    assert build == {"hidden": True, "n": 1, "build": 1}, build                 # build and memory draw the bar alone


@needs_browser
def test_a_strip_of_only_answered_cards_has_no_headline_pill_or_jump_and_keeps_three(tmp_path: Path, monkeypatch) -> None:
    store = _home(tmp_path, monkeypatch)
    for k in range(5):
        store.answer(_file(store, f"Question number {k} to settle?").n, option="hold", channel="cockpit")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": "document.querySelector('#ck-needs .needs-answered .consent-note') && 1", "ms": 30000},
            {"eval": STRIP}, {"eval": BUILD_HIDES}])
    seen, build = _values(out, '"strip"'), _values(out, '"build"')
    assert seen["headline"] is False and seen["pill"] is False and seen["history"] is False and seen["rows"] == 0, seen
    assert seen["waiting"] is False and seen["jump"] is False, seen              # the "Needs you" tab has nothing to jump to
    assert seen["cards"] == 3 and seen["label"] == "Just answered", seen         # five answered, the newest three kept
    assert build["hidden"] is True and build["n"] == 1, build


ROWS = "[...document.querySelectorAll('#levain-modal [data-question-row]')]"
ROWS_STATE = ("JSON.stringify({rows: %s.length, drawn: %s.filter(r => r.querySelector('.consent-body')).length,"
              " drawnNoButton: %s.filter(r => r.querySelector('.consent-body') && !r.querySelector('.consent-details')).length,"
              " buttons: %s.filter(r => r.querySelector('.consent-details')).length,"
              " blank: %s.filter(r => { const b = r.querySelector('.consent-body'); return b && b.textContent.trim() === ''; }).length, rowsstate: 1})"
              % ((ROWS,) * 5))


def _history_steps(settle: str) -> list[dict]:
    return [DESKTOP, {"until": "window.LevainNeeds && window.LevainNeeds.history && 1", "ms": 30000},
            {"eval": "window.LevainNeeds.history(null) ? 1 : 0"}, {"until": f"{ROWS}.length === 7 && 1", "ms": 30000},
            {"until": settle, "ms": 15000}, {"eval": ROWS_STATE}]


@needs_browser
def test_only_the_five_newest_answered_rows_draw_their_answer_unasked(tmp_path: Path, monkeypatch) -> None:
    store = _home(tmp_path, monkeypatch)
    for k in range(7):
        store.answer(_file(store, f"Question number {k} to settle?").n, option="ship", channel="cli")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        got = _values(_drive(tmp_path, f"{url}/#write_token=T", _history_steps(f"{ROWS}.filter(r => r.querySelector('.consent-body')).length >= 5")),
                      '"rowsstate"')
    assert got["rows"] == 7 and got["drawn"] == 5 and got["drawnNoButton"] == 5 and got["buttons"] == 2 and got["blank"] == 0, got


@needs_browser
def test_a_row_whose_read_fails_draws_no_blank_slot(tmp_path: Path, monkeypatch) -> None:
    """No page token: every body read is refused. The rows keep their details button and carry no empty body."""
    store = _home(tmp_path, monkeypatch)
    for k in range(7):
        store.answer(_file(store, f"Question number {k} to settle?").n, option="ship", channel="cli")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        got = _values(_drive(tmp_path, f"{url}/", _history_steps("true")), '"rowsstate"')
    assert got["rows"] == 7 and got["drawn"] == 0 and got["buttons"] == 7 and got["blank"] == 0, got


@needs_browser
def test_an_answered_card_leaves_an_idle_page_when_its_window_ends(tmp_path: Path, monkeypatch) -> None:
    """Answered 29m45s ago: the card is drawn, then the page's own timer drops it with no refetch and no input."""
    from datetime import datetime, timedelta, timezone
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent", clock=lambda: datetime.now(timezone.utc) - timedelta(minutes=29, seconds=45))
    store.answer(_file(store, "Ship the sweep fix tonight?").n, option="ship", channel="cli")
    _root, src, _ids = _install(tmp_path)
    cards = "document.querySelectorAll('#ck-needs .needs-answered .needs-card').length"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{cards} === 1 && 1", "ms": 10000},
            {"until": f"{cards} === 0 && 1", "ms": 40000},
            {"eval": "JSON.stringify({gone: " + cards + ", host: document.querySelector('#ck-needs').hidden, done: 1})"}])
    seen = _values(out, '"done"')
    assert seen["gone"] == 0 and seen["host"] is True, seen


@needs_browser
def test_a_details_click_during_the_eager_read_draws_one_body(tmp_path: Path, monkeypatch) -> None:
    store = _home(tmp_path, monkeypatch)
    for k in range(7):
        store.answer(_file(store, f"Question number {k} to settle?").n, option="ship", channel="cli")
    _root, src, _ids = _install(tmp_path)
    click = ("(() => { const r = " + ROWS + ".find(r => r.querySelector('.consent-details')); if (!r) return 0;"
             " r.querySelector('.consent-details').click(); return 1; })()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        steps = [DESKTOP, {"until": "window.LevainNeeds && window.LevainNeeds.history && 1", "ms": 30000},
                 {"eval": "window.LevainNeeds.history(null) ? 1 : 0"}, {"until": f"{ROWS}.length === 7 && 1", "ms": 30000},
                 {"eval": click},                                                     # right after the rows drew, eager reads in flight
                 {"until": f"{ROWS}.filter(r => r.querySelector('.consent-body')).length >= 5", "ms": 15000},
                 {"eval": "JSON.stringify({multi: " + ROWS + ".filter(r => r.querySelectorAll('.consent-body').length > 1).length, rowsstate: 1})"}]
        got = _values(_drive(tmp_path, f"{url}/#write_token=T", steps), '"rowsstate"')
    assert got["multi"] == 0, got
