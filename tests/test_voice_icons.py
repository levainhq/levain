"""The voice controls are icons (Phill 11-09: the Play / Mic / Dictate words wrapped into ragged lines on his phone).

Real cockpit page in headless Chrome, phone-sized, the fake microphone of ``tests/test_mic_page.py`` with BOTH Dictate and Mic on:
every voice button on the card and the Room is an inline SVG with an accessible name and no words, and a reply box's controls sit
on ONE row with the box or its send button at the iPhone 15 Pro Max width (430 CSS px) and at 390. The icon state of each button
is one helper (``voice_icons.js``), served and loaded by both pages that draw voice controls. What a person sees on a real
iPhone is residue (screenshots are kept by the lane, not asserted here).

Each test names the line whose revert turns it red."""
from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_mic_page import _fake
from tests.test_voice_room_hook import A, SETTINGS, _room, _run, _set, _to_room

# every voice control the page drew: name it, check it is an icon (an svg, no words) with an accessible name
AUDIT = """(() => {
  const all = [...document.querySelectorAll('%s')];
  return JSON.stringify(all.filter((b) => !b.hidden).map((b) => ({
    cls: b.className, label: b.getAttribute('aria-label'), title: b.getAttribute('title'), svg: b.querySelectorAll('svg').length,
    words: b.textContent.trim() })));
})()"""
SELECTOR = ".card-mic, .voice-btn, .rv-mic"
WIDTHS = (430, 390)


def _phone(w: int) -> dict:
    return {"emulate": {"w": w, "h": 932, "dpr": 2, "mobile": True}}


def test_the_icon_module_is_served_and_both_voice_pages_load_it_before_the_modules_that_use_it() -> None:
    """Red with: the "/voice_icons.js" row of web_server._ASSETS removed (404, no icons), or its tag out of dashboard.html or
    sign.html (the buttons fall back to words)."""
    from levain import web_server
    assert "/voice_icons.js" in web_server._ASSETS
    web = Path(web_server.__file__).parent / "templates" / "web"
    for page, first in (("dashboard.html", "/voice.js"), ("sign.html", "/mic.js")):
        html = (web / page).read_text()
        assert '<script src="/voice_icons.js">' in html and html.index("/voice_icons.js") < html.index(f'<script src="{first}">'), page


@needs_browser
def test_the_module_is_served_over_http(tmp_path) -> None:
    """Red with: the asset row removed from web_server._ASSETS."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (base, _h):
        c = http.client.HTTPConnection(base.split("//")[1], timeout=20)
        c.request("GET", "/voice_icons.js")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200 and r.getheader("Content-Type").startswith("text/javascript") and "LevainVoiceIcons" in body


def _card(tmp_path: Path, monkeypatch, open_ended: bool) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    if open_ended:
        store.file(agent="codex", agent_session_id="s2", what="flow: Due 11-09 15:30: Call with Jim (spore-1663)",
                   blocks="nothing acts on it until you do", cwd="/w", open_ended=True)
    else:
        store.file(agent="claude_code", agent_session_id="s1", what="Should a rule be a 30-day lease?",
                   options=[("a", "a 30-day lease, like a graduation policy: the rule lapses unless you re-sign it"),
                            ("b", "no expiry: a rule stands until you revoke it, and doctor lists each rule's age")],
                   rec="a", blocks="the build waits on it", cwd="/w")


ROW = """(() => {
  const r = (n) => { const b = n.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right)}; };
  const mics = [...document.querySelectorAll('#levain-modal .card-mic')].filter((b) => !b.hidden);
  const send = document.querySelector('#levain-modal .question-send'), box = document.querySelector('#levain-modal .question-words');
  return JSON.stringify({mics: mics.map(r), send: r(send), box: r(box), vw: document.documentElement.clientWidth,
    sw: document.documentElement.scrollWidth});
})()"""


@needs_browser
@pytest.mark.parametrize("open_ended", [False, True], ids=["options", "agenda"])
@pytest.mark.parametrize("width", WIDTHS)
def test_a_card_reply_boxs_voice_controls_are_icons_on_one_row_with_send(tmp_path, monkeypatch, width, open_ended) -> None:
    """Red with: the `sendRow` wrapper in dashboard_core.js removed (Mic drops under the box on its own line: the tops differ),
    or a face() call in mic.js left drawing words (words != '' / no svg)."""
    _card(tmp_path, monkeypatch, open_ended)
    _root, src, _ids = _install(tmp_path)
    mic = "document.querySelector('#levain-modal .card-mic-stream')"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            _phone(width), {"init": _fake()},
            {"until": "[...document.querySelectorAll('#ck-needs .needs-question')].length === 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#ck-needs .needs-question')][0].click(); 1"},
            {"until": f"{mic} && !{mic}.hidden && document.querySelector('#levain-modal .voice-btn') && 1", "ms": 30000},
            {"eval": AUDIT % "#levain-modal .card-mic, #levain-modal .voice-btn"},
            {"eval": ROW},
        ])
    audit = json.loads(out["values"][-2])
    names = {b["label"] for b in audit}
    assert {"Play", "Dictate", "Mic"} <= names, audit
    for b in audit:
        assert b["svg"] == 1 and b["words"] == "" and b["label"] and b["title"], b      # an icon, named, no words
    row = json.loads(out["values"][-1])
    assert len(row["mics"]) == 2, row
    for m in row["mics"]:                                    # one row with the send button: same band, mic left of send
        assert abs(m["t"] - row["send"]["t"]) <= 12 and m["r"] <= row["send"]["l"], row
    assert row["sw"] <= row["vw"], row                       # nothing pushes the page wider than the phone


@needs_browser
@pytest.mark.parametrize("width", WIDTHS)
def test_the_rooms_voice_controls_are_icons_and_stay_in_the_composer(tmp_path, qdir, width) -> None:
    """Red with: the `face(...)` calls in room_view.js (the buttons are drawn as words and wrap out of the composer)."""
    v = _run(tmp_path, qdir, [
        _phone(width), _set(_room("t1", *A)), _to_room(),
        {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 1"},
        SETTINGS,
        {"until": "document.querySelector('.rv-mic-stream') && !document.querySelector('.rv-mic-stream').hidden", "ms": 20000},
        {"eval": AUDIT % ".rv-room .voice-btn, .rv-mic"},
        {"eval": "(() => { const c = document.querySelector('.rv-compose').getBoundingClientRect();"
                 " return JSON.stringify([...document.querySelectorAll('.rv-mic')].map((b) => { const r = b.getBoundingClientRect();"
                 " return r.top >= c.top - 1 && r.bottom <= c.bottom + 1 && r.right <= c.right; })); })()"},
    ], init_extra=_fake())
    audit = json.loads(v[-2])
    assert {"Play", "Dictate", "Mic"} <= {b["label"] for b in audit}, audit
    for b in audit:
        assert b["svg"] == 1 and b["words"] == "" and b["label"] and b["title"], b
    assert json.loads(v[-1]) == [True, True], v[-1]


@needs_browser
def test_a_state_change_swaps_the_icon_and_the_accessible_name_not_words(tmp_path, qdir) -> None:
    """Red with: a toggle in mic.js or room_view.js writing textContent again (words come back, the label goes stale)."""
    v = _run(tmp_path, qdir, [
        _phone(430), _set(_room("t1", *A)), _to_room(),
        {"until": "document.querySelector('.rv-mic-stream') && !document.querySelector('.rv-mic-stream').hidden", "ms": 20000},
        {"eval": "const b = document.querySelector('.rv-mic-stream'); b.click(); 1"},
        {"until": "document.querySelector('.rv-mic-stream').getAttribute('aria-label') === 'Stop mic'", "ms": 20000},
        {"eval": "(() => { const b = document.querySelector('.rv-mic-stream'); return JSON.stringify([b.getAttribute('aria-label'), b.getAttribute('aria-pressed'), b.getAttribute('data-voice-icon'), b.textContent, b.querySelectorAll('svg').length]); })()"},
        {"eval": "document.querySelector('.rv-mic-stream').click(); 1"},
        {"until": "document.querySelector('.rv-mic-stream').getAttribute('aria-label') === 'Mic'", "ms": 20000},
        {"eval": "(() => { const b = document.querySelector('.rv-mic-stream'); return JSON.stringify([b.getAttribute('aria-label'), b.getAttribute('aria-pressed'), b.getAttribute('data-voice-icon'), b.textContent, b.querySelectorAll('svg').length]); })()"},
    ], init_extra=_fake())
    assert json.loads(v[-4]) == ["Stop mic", "true", "mic-stop", "", 1], v[-4]
    assert json.loads(v[-1]) == ["Mic", "false", "mic", "", 1], v[-1]


SIGN_ROW = """(() => {
  const r = (n) => { const b = n.getBoundingClientRect(); return {t: Math.round(b.top), b: Math.round(b.bottom), l: Math.round(b.left), r: Math.round(b.right)}; };
  const q = (s) => document.querySelector('#levain-modal .sign-actions ' + s);
  const acts = document.querySelector('#levain-modal .sign-actions');
  return JSON.stringify({acts: r(acts), box: r(q('.sign-write-in')), mics: [...acts.querySelectorAll('.card-mic')].filter((b) => !b.hidden).map(r),
    words: r(q('.sign-words')), sheet: r(q('.sign-sheet-open')), sw: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth});
})()"""


@needs_browser
@pytest.mark.parametrize("width", WIDTHS)
def test_a_signed_cards_write_in_row_spans_the_card_and_sign_and_sheet_share_the_next_row(tmp_path, monkeypatch, width) -> None:
    """Red with: the `.voice-in-row` grid-column rule in dashboard.css removed (the box is squeezed into one half-column cell),
    or the `sign-words` class dropped from the sign button in dashboard_core.js (the sheet button fills a row alone)."""
    from tests.test_cockpit_sign_card import DESKTOP, STRIP_FACET, TOK, _Rig  # noqa: F401
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _h):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            _phone(width), rig.authenticator(), {"init": _fake()},
            {"until": STRIP_FACET + " && document.querySelector('#levain-modal .sign-actions .card-mic') && 1", "ms": 30000},
            {"eval": SIGN_ROW},
        ])
    row = json.loads(out["values"][-1])
    acts, box, words, sheet = row["acts"], row["box"], row["words"], row["sheet"]
    assert len(row["mics"]) == 2, row
    assert box["r"] - box["l"] >= (acts["r"] - acts["l"]) * 0.45, row                   # the box keeps real width
    for m in row["mics"]:                                                              # the mic icons share the box's row
        assert abs(m["t"] - box["t"]) <= 12 and m["l"] >= box["r"] - 1, row
    assert abs(words["t"] - sheet["t"]) <= 2 and words["r"] <= sheet["l"], row         # sign my words | open the sheet, one row
    assert words["t"] >= box["b"] - 1, row                                             # under the box row, not beside it
    assert row["sw"] <= row["vw"], row
