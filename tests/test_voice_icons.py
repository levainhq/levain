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
    cls: b.className, label: b.getAttribute('aria-label'), title: b.getAttribute('title'), svg: b.querySelectorAll('svg').length, pressed: b.getAttribute('aria-pressed'),
    shape: (b.querySelector('svg') || document.body).getAttribute('data-voice-shape'),
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
        assert b["pressed"] is None, b                       # the name swaps with the state, so no pressed state beside it
    shapes = {b["label"]: b["shape"] for b in audit}
    assert shapes["Dictate"] == "microphone" and shapes["Mic"] == "waveform" and shapes["Play"] == "play", shapes
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
        assert b["svg"] == 1 and b["words"] == "" and b["label"] and b["title"] and b["pressed"] is None, b
    assert {b["label"]: b["shape"] for b in audit}["Dictate"] == "microphone", audit
    assert json.loads(v[-1]) == [True, True], v[-1]


@needs_browser
def test_a_state_change_swaps_the_icon_and_the_accessible_name_not_words(tmp_path, qdir) -> None:
    """Red with: a toggle in mic.js or room_view.js writing textContent again (words come back, the label goes stale), or the
    `aria-pressed` line back in voice_icons.js set() (the swapped name is announced twice), or the `announce` call out of set()
    (the polite status node says nothing on start or stop)."""
    v = _run(tmp_path, qdir, [
        _phone(430), _set(_room("t1", *A)), _to_room(),
        {"until": "document.querySelector('.rv-mic-stream') && !document.querySelector('.rv-mic-stream').hidden", "ms": 20000},
        {"eval": "const b = document.querySelector('.rv-mic-stream'); b.click(); 1"},
        {"until": "document.querySelector('.rv-mic-stream').getAttribute('aria-label') === 'Stop mic'", "ms": 20000},
        {"eval": "(() => { const b = document.querySelector('.rv-mic-stream'); return JSON.stringify([b.getAttribute('aria-label'), b.getAttribute('aria-pressed'), b.getAttribute('data-voice-icon'), b.textContent, b.querySelectorAll('svg').length, (document.getElementById('levain-voice-status') || {textContent: null}).textContent]); })()"},
        {"eval": "document.querySelector('.rv-mic-stream').click(); 1"},
        {"until": "document.querySelector('.rv-mic-stream').getAttribute('aria-label') === 'Mic'", "ms": 20000},
        {"eval": "(() => { const b = document.querySelector('.rv-mic-stream'); return JSON.stringify([b.getAttribute('aria-label'), b.getAttribute('aria-pressed'), b.getAttribute('data-voice-icon'), b.textContent, b.querySelectorAll('svg').length, (document.getElementById('levain-voice-status') || {textContent: null}).textContent]); })()"},
    ], init_extra=_fake())
    assert json.loads(v[-4]) == ["Stop mic", None, "mic-stop", "", 1, "Mic on"], v[-4]
    assert json.loads(v[-1]) == ["Mic", None, "mic", "", 1, "Stopped"], v[-1]


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


@needs_browser
def test_every_kind_announces_its_state_in_one_polite_status_node_and_the_first_draw_is_silent(tmp_path, qdir) -> None:
    """Red with: the `if (before && before !== kind) announce(...)` line of voice_icons.js removed (no node), or its `before`
    test dropped (a first draw says "Stopped" out of nowhere), or a second node made per call (two ids)."""
    v = _run(tmp_path, qdir, [
        _phone(430), _set(_room("t1", *A)), _to_room(),
        {"until": "window.LevainVoiceIcons && 1"},
        {"eval": "(() => { window.__b = document.createElement('button'); document.body.appendChild(window.__b); const V = window.LevainVoiceIcons;"
                 " const say = () => { const n = document.getElementById('levain-voice-status'); return n ? n.textContent : null; };"
                 " const out = [];"
                 " V.set(window.__b, 'dictate'); out.push(say());"                      # first draw: nothing said (the node is already there, empty)
                 " V.set(window.__b, 'dictate-stop'); out.push(say());"
                 " V.set(window.__b, 'dictate'); out.push(say());"
                 " V.set(window.__b, 'mic'); V.set(window.__b, 'mic-stop'); out.push(say());"
                 " V.set(window.__b, 'mic'); out.push(say());"
                 " V.set(window.__b, 'play'); V.set(window.__b, 'stop'); out.push(say());"
                 " V.set(window.__b, 'stop'); out.push(say());"                         # a redraw in the same state: unchanged
                 " V.set(window.__b, 'play'); out.push(say());"
                 " const n = document.querySelectorAll('#levain-voice-status');"
                 " out.push([n.length, n[0].getAttribute('role'), n[0].getAttribute('aria-live')]);"
                 " return JSON.stringify(out); })()"},
    ], init_extra=_fake())
    got = json.loads(v[-1])
    assert got[:8] == ["", "Dictating", "Stopped", "Mic on", "Stopped", "Playing", "Playing", "Stopped"], got
    assert got[8] == [1, "status", "polite"], got


@needs_browser
@pytest.mark.parametrize("width", WIDTHS)
def test_without_the_icon_module_the_voice_words_are_named_and_sit_in_a_padded_pill(tmp_path, monkeypatch, width) -> None:
    """The fallback with /voice_icons.js answering 404. Red with: the `voice-words` class dropped from the fallback `face()` in
    mic.js (the words land in a padding:0 pill and touch its edges), or the `.voice-words` padding rule in dashboard.css removed."""
    from levain import web_server
    monkeypatch.delitem(web_server._ASSETS, "/voice_icons.js")
    _card(tmp_path, monkeypatch, False)
    _root, src, _ids = _install(tmp_path)
    mic = "document.querySelector('#levain-modal .card-mic-stream')"
    pad = ("JSON.stringify([...document.querySelectorAll('#levain-modal .card-mic, #levain-modal .voice-btn')].filter((b) => !b.hidden)"
           ".map((b) => { const c = getComputedStyle(b); return [b.className, c.paddingLeft, c.paddingRight, b.getBoundingClientRect().height]; }))")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            _phone(width), {"init": _fake()},
            {"until": "[...document.querySelectorAll('#ck-needs .needs-question')].length === 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#ck-needs .needs-question')][0].click(); 1"},
            {"until": f"{mic} && !{mic}.hidden && document.querySelector('#levain-modal .voice-btn') && 1", "ms": 30000},
            {"eval": "String(window.LevainVoiceIcons)"},
            {"eval": AUDIT % "#levain-modal .card-mic, #levain-modal .voice-btn"},
            {"eval": pad},
        ])
    assert out["values"][-3] == "undefined", out["values"][-3]          # the module really was absent
    audit = json.loads(out["values"][-2])
    assert {"Play", "Dictate", "Mic"} <= {b["label"] for b in audit}, audit
    for b in audit:
        assert b["label"] and b["words"] and b["svg"] == 0, b           # named, and the words are the button
    for cls, left, right, height in json.loads(out["values"][-1]):
        assert left != "0px" and right != "0px", (cls, left, right)     # the words do not touch the pill's edges
        assert height <= 60, (cls, height)                              # a single line, not a wrapped tall pill


@needs_browser
def test_the_status_node_exists_empty_before_any_change_and_a_dialogs_control_is_announced_inside_the_dialog(tmp_path, qdir) -> None:
    """Red with: the `ready()` call of voice_icons.js removed (no node until the first change, which a screen reader drops), or
    `announce` writing to the body node for a control inside an aria-modal dialog (outside the dialog's tree)."""
    v = _run(tmp_path, qdir, [
        _phone(430), _set(_room("t1", *A)), _to_room(),
        {"until": "window.LevainVoiceIcons && 1"},
        {"eval": "(() => { const n = document.getElementById('levain-voice-status');"
                 " return JSON.stringify([!!n, n ? n.textContent : null, n ? n.parentNode === document.body : null]); })()"},
        {"eval": "(() => { const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.setAttribute('aria-modal', 'true'); d.id = '__dlg';"
                 " const b = document.createElement('button'); d.appendChild(b); document.body.appendChild(d);"
                 " window.LevainVoiceIcons.set(b, 'mic'); window.LevainVoiceIcons.set(b, 'mic-stop'); window.__dlgbtn = b; return 1; })()"},
        {"until": "document.querySelector('#__dlg #levain-voice-status-dialog') && document.querySelector('#__dlg #levain-voice-status-dialog').textContent === 'Mic on'", "ms": 5000},
        {"eval": "(() => { const b = window.__dlgbtn; window.LevainVoiceIcons.set(b, 'mic'); const n = document.querySelector('#__dlg #levain-voice-status-dialog');"
                 " return JSON.stringify([n.textContent, document.getElementById('levain-voice-status').textContent]); })()"},
    ], init_extra=_fake())
    assert json.loads(v[-4]) == [True, "", True], v[-4]
    assert json.loads(v[-1]) == ["Stopped", ""], v[-1]       # the dialog's control spoke in the dialog; the page's node stayed quiet


@needs_browser
def test_two_changes_inside_a_new_dialogs_first_moment_leave_the_later_one_said(tmp_path, qdir) -> None:
    """Red with: the dialog node's delayed first text captured at creation, so a change inside that moment is said and then
    overwritten by the older text."""
    v = _run(tmp_path, qdir, [
        _phone(430), _set(_room("t1", *A)), _to_room(),
        {"until": "window.LevainVoiceIcons && 1"},
        {"eval": "(() => { const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.setAttribute('aria-modal', 'true'); d.id = '__dlg2';"
                 " const b = document.createElement('button'); d.appendChild(b); document.body.appendChild(d);"
                 " window.LevainVoiceIcons.set(b, 'mic'); window.LevainVoiceIcons.set(b, 'mic-stop'); window.LevainVoiceIcons.set(b, 'mic'); return 1; })()"},
        {"eval": "new Promise((r) => setTimeout(() => r(document.querySelector('#__dlg2 #levain-voice-status-dialog').textContent), 300))"},
    ], init_extra=_fake())
    assert v[-1] == "Stopped", v[-1]


@needs_browser
def test_a_repaint_in_the_same_state_keeps_the_drawn_icon_and_the_button(tmp_path, qdir) -> None:
    """Red with: the `same` test of voice_icons.js set() removed (every repaint builds a new svg: the node differs)."""
    v = _run(tmp_path, qdir, [
        _phone(430), _set(_room("t1", *A)), _to_room(),
        {"until": "window.LevainVoiceIcons && 1"},
        {"eval": "(() => { const V = window.LevainVoiceIcons, b = document.createElement('button'); document.body.appendChild(b);"
                 " V.set(b, 'play'); const first = b.querySelector('svg'); V.set(b, 'play'); const kept = b.querySelector('svg') === first;"
                 " V.set(b, 'play', 'Tap to hear this card'); const relabel = b.querySelector('svg') !== first && b.getAttribute('aria-label') === 'Tap to hear this card';"
                 " const second = b.querySelector('svg'); V.set(b, 'stop'); const moved = b.querySelector('svg') !== second && b.querySelectorAll('svg').length === 1;"
                 " return JSON.stringify([kept, relabel, moved]); })()"},
    ], init_extra=_fake())
    assert json.loads(v[-1]) == [True, True, True], v[-1]
