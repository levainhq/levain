"""The answer a decision was given stays on the cockpit card: one composed line (the option key and label, or his words; signed or
unsigned when known; when; via which channel) on a Decisions-panel row without a click, and on a Needs-you card for a short
window after the answer, where it counts for nothing. The line's helpers are run in node from the page source; the strip and the row
run in headless Chrome when it is installed."""
from __future__ import annotations

import json
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
           _slice("const NEEDS_ANSWERED_MINUTES", "  const needsKey"))
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
    got = _node('["0s ago", "29m ago", "30m ago", "1h ago", "2d ago", "", null].map(decidedRecently)')
    assert got == [True, True, False, False, False, False, False]


def test_the_strip_keeps_answered_cards_apart_from_everything_that_counts() -> None:
    assert "items: (lines) => needsWaiting(needsItems(lines))" in CORE      # the glance tile and the push badge read waiting items only
    assert "needsWaiting(needsItems(((currentView" in CORE                  # the focus view still closes when its item is answered
    assert "answered.push({ kind: \"question\", q: ln.question, answered: true })" in CORE


OPTIONS = [("ship", "Ship it tonight"), ("hold", "Hold for the morning")]


@needs_browser
def test_an_answered_card_shows_its_answer_and_moves_no_count(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    done = store.file(agent="claude_code", agent_session_id="s1", what="Ship the sweep fix tonight?", options=OPTIONS, rec="hold",
                      blocks="the sweep", cwd="/w")
    waiting = store.file(agent="codex", agent_session_id="s2", what="Which branch do we cut from?", options=OPTIONS, rec="ship",
                         blocks="the cut", cwd="/w")
    store.answer(done.n, option="ship", channel="cli")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            {"until": "document.querySelector('#ck-needs .needs-settled .consent-note') && 1", "ms": 30000},
            {"eval": "JSON.stringify({rows: document.querySelectorAll('#ck-needs .needs-row').length,"
                     " count: document.querySelector('#ck-needs .needs-headline .needs-count').textContent,"
                     " settled: document.querySelectorAll('#ck-needs .needs-settled').length,"
                     " line: document.querySelector('#ck-needs .needs-settled .consent-note').textContent,"
                     " buttons: document.querySelectorAll('#ck-needs .needs-settled .question-option, #ck-needs .needs-settled .question-send').length,"
                     " open: [...document.querySelectorAll('#ck-needs .needs-row')].map(r => r.dataset.needsId)})"},
        ])["values"]
    seen = [json.loads(v) for v in out if isinstance(v, str) and v.startswith("{") and '"settled"' in v][0]
    assert seen["rows"] == 1 and seen["count"] == "1" and seen["settled"] == 1 and seen["buttons"] == 0, seen
    assert seen["line"].startswith("Answered: ship · Ship it tonight") and seen["line"].endswith("via cli"), seen
    assert waiting.n and len(seen["open"]) == 1
