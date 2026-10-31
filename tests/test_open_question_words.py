"""The words an OPEN question (`levain ask --open`: no options, no rec) draws on the cockpit and the phone sheet, beside an
ordinary two-option question drawn through the same code. Words only: the open question says it is open and asks for words,
and never draws a dangling "recommends ", "0 options" or "or" with no buttons before it; the ordinary question is what it was
(the REC line, "recommends X", "N options", "or your own words"). Real runs in headless Chrome; the string-level tests pin the
same sources when no browser is installed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_phone_sign_preselect import TOK, _Port

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "levain" / "templates" / "web"
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
OPEN_WHAT = "What should the release note say about the sweep?"
ORD_WHAT = "Ship the sweep fix tonight or hold it?"


def _file_both(consent: Path) -> tuple[int, int]:
    """The ordinary question first (n=1), the open one second (n=2)."""
    store = QuestionStore(consent)
    ordinary = store.file(agent="claude_code", agent_session_id="s1", what=ORD_WHAT,
                          options=[("ship", "Ship it tonight"), ("hold", "Hold for the morning")], rec="hold",
                          blocks="the nightly sweep stays red", cwd="/w")
    opened = store.file(agent="codex", agent_session_id="s2", what=OPEN_WHAT, blocks="the release note waits", cwd="/w",
                        open_ended=True)
    return ordinary.n, opened.n


# --- the cockpit: the focus view of each question, and the Decisions panel's row head -----------------------


def _open_modal(what: str) -> list[dict]:
    """Steps: open the strip row whose title is this question's text, then wait for its answer box."""
    return [{"eval": "[...document.querySelectorAll('#ck-needs .needs-question')].find(r => r.innerText.includes(" + json.dumps(what) + ")).click(); 1"},
            {"until": "document.querySelector('#levain-modal .needs-question .question-words')"},
            {"eval": "(() => { const q = document.querySelector('#levain-modal .needs-question'); return JSON.stringify({"
                     " text: q.innerText, placeholder: q.querySelector('.question-words').placeholder,"
                     " options: [...q.querySelectorAll('.question-option')].map(b => b.textContent),"
                     " rec: !!q.querySelector('.needs-rec-tag'), send: !!q.querySelector('.question-send') }); })()"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"}]


@needs_browser
def test_the_cockpit_focus_view_says_open_question_and_the_ordinary_one_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    ordinary_n, open_n = _file_both(home / "consent")
    _root, src, _ids = _install(tmp_path)
    rows = "[...document.querySelectorAll('#ck-needs .needs-question')]"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"until": f"{rows}.length === 2 && {rows}.every(r => r.querySelector('.needs-row-sub').textContent !== '')"},
            *_open_modal(OPEN_WHAT), {"until": "!document.querySelector('#levain-modal .needs-question')"},
            *_open_modal(ORD_WHAT)])["values"]
    seen = [json.loads(v) for v in out if isinstance(v, str) and v.startswith("{") and '"placeholder"' in v]
    assert len(seen) == 2, out
    opened, ordinary = seen
    assert opened["placeholder"] == "your answer, in your own words", opened
    assert opened["options"] == [] and opened["rec"] is False and opened["send"] is True, opened
    assert OPEN_WHAT in opened["text"] and "recommends" not in opened["text"], opened
    assert ordinary["placeholder"] == "or your own words", ordinary
    assert ordinary["rec"] is True and len(ordinary["options"]) == 2 and ordinary["send"] is True, ordinary
    assert ORD_WHAT in ordinary["text"] and re.search(r"REC\s+hold\.", ordinary["text"]), ordinary


HISTORY = ("(document.querySelector('#levain-modal .modal-close') && document.querySelector('#levain-modal .modal-close').click(), "
           "document.querySelector('#ck-needs .needs-history').click(), 1)")
ROWS_ALL = "[...document.querySelectorAll('[data-question-row]')]"


@needs_browser
def test_the_decisions_row_says_open_question_for_none_and_keeps_n_options_and_recommends_for_the_rest(tmp_path: Path, monkeypatch) -> None:
    """The Decisions panel's row head, and the row's opened body (the non-card path: "recommends X" vs the open words)."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    ordinary_n, open_n = _file_both(home / "consent")
    _root, src, _ids = _install(tmp_path)
    one = "document.querySelector('[data-question-row][data-question-n=\"%d\"]')"
    with _serve(src, write_token="T") as (url, _httpd):
        steps = [DESKTOP, {"until": "document.querySelector('#ck-needs .needs-history') && 1", "ms": 30000}, {"eval": HISTORY},
                 {"until": f"{ROWS_ALL}.length === 2 && 1", "ms": 30000}]
        for n in (open_n, ordinary_n):
            row = one % n
            steps += [{"eval": f"({row}.querySelector('.consent-details').click(), 1)"},
                      {"until": f"{row} && {row}.querySelector('.consent-facts') && {row}.querySelector('.question-words') && 1", "ms": 30000},
                      {"eval": f"JSON.stringify({{n: {n}, head: {row}.querySelector('.consent-head').innerText,"
                               f" facts: {row}.querySelector('.consent-facts').innerText, placeholder: {row}.querySelector('.question-words').placeholder,"
                               f" options: {row}.querySelectorAll('.question-option').length}})"}]
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)["values"]
    got = {d["n"]: d for d in (json.loads(v) for v in out if isinstance(v, str) and v.startswith('{"n"'))}
    o, a = got[open_n], got[ordinary_n]
    assert "open question" in o["head"] and "options" not in o["head"], o
    assert "open question: answer in your own words" in o["facts"] and "recommends" not in o["facts"], o
    assert o["placeholder"] == "your answer, in your own words" and o["options"] == 0, o
    assert "2 options" in a["head"] and "open question" not in a["head"], a
    assert "recommends hold" in a["facts"] and a["placeholder"] == "or answer in your own words" and a["options"] == 2, a


# --- the phone: the question card and the band item -------------------------------------------------------


@pytest.fixture
def port(tmp_path: Path):
    (tmp_path / "q").mkdir(mode=0o700)
    p = _Port(tmp_path / "q")
    try:
        yield p
    finally:
        p.srv.shutdown()
        p.srv.server_close()


@needs_browser
def test_the_phone_question_card_says_open_question_and_keeps_its_words_box(port: _Port) -> None:
    ordinary_n, open_n = _file_both(port.qdir)
    cards = "[...document.querySelectorAll('article.req')].filter(c => /question #/.test(c.innerText))"
    out = _drive(port.qdir.parent, port.url + f"#phone_token={TOK}", [
        PHONE, {"until": f"{cards}.length === 2"},
        {"eval": f"JSON.stringify({cards}.map(c => ({{ n: (/question #(\\d+)/.exec(c.innerText) || [])[1], text: c.innerText,"
                 " placeholder: c.querySelector('textarea.words').placeholder, buttons: [...c.querySelectorAll('button')].map(b => b.textContent),"
                 " emptyActions: [...c.querySelectorAll('.actions')].filter(a => a.children.length === 0).length })))"}])["values"]
    got = {c["n"]: c for c in json.loads(next(v for v in out if isinstance(v, str) and v.startswith("[{")))}
    o, a = got[str(open_n)], got[str(ordinary_n)]
    assert "open question: your answer, in words" in o["text"], o
    assert o["buttons"] == ["Send words"] and o["emptyActions"] == 0, o
    assert o["placeholder"] == "your answer, in words", o
    assert "open question" not in a["text"] and a["placeholder"] == "or your own words", a
    assert any(b.startswith("Ship it tonight") for b in a["buttons"]) and "Send words" in a["buttons"], a


@needs_browser
def test_the_phone_band_item_for_an_open_question_says_so_and_draws_no_empty_actions_row(port: _Port) -> None:
    ordinary_n, open_n = _file_both(port.qdir)
    items = "[...document.querySelectorAll('.band-item')]"
    out = _drive(port.qdir.parent, port.url + f"#phone_token={TOK}", [
        PHONE, {"until": f"{items}.length === 2"},
        {"eval": f"JSON.stringify({items}.map(i => ({{ text: i.innerText, actions: i.querySelectorAll('.actions').length,"
                 " buttons: i.querySelectorAll('.actions button').length })))"}])["values"]
    rows = json.loads(next(v for v in out if isinstance(v, str) and v.startswith("[{")))
    open_row = next(r for r in rows if OPEN_WHAT in r["text"])
    ordinary = next(r for r in rows if ORD_WHAT in r["text"])
    assert "open question: type " in open_row["text"] and ": and your words in the box below" in open_row["text"], open_row
    assert open_row["actions"] == 0, open_row
    assert ordinary["actions"] == 1 and ordinary["buttons"] == 2 and "open question" not in ordinary["text"], ordinary


# --- the sources, string level (run with no browser) ------------------------------------------------------


def test_the_cockpit_source_carries_the_four_open_question_words_and_the_old_ones() -> None:
    core = (WEB / "dashboard_core.js").read_text()
    assert '"open question: answer in your own words" : "recommends " + sv(b.rec)' in core
    assert 'q.options === 0 ? "open question" : q.options + " options"' in core
    assert ('words.placeholder = b.open_ended ? "your answer, in your own words" : card ? "or your own words" '
            ': "or answer in your own words";') in core


def test_the_phone_source_carries_the_open_question_words_and_keeps_the_ordinary_placeholder() -> None:
    phone = (WEB / "phone.js").read_text()
    assert '"open question: type " + String(it.n) + ": and your words in the box below"' in phone and "if (!it.open_ended) row.appendChild(acts);" in phone
    assert '"open question: your answer, in words"' in phone and 'words.placeholder = "or your own words";' in phone


def test_the_changed_files_parse() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    for name in ("dashboard_core.js", "phone.js"):
        r = subprocess.run([node, "--check", str(WEB / name)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
