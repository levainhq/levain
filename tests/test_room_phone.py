"""RM-H (CH-U2): the room on a phone and its foreground voice. STRUCTURAL ONLY: headless Chrome clamps the window width
(spore-1066), so a 390 emulation proves the rules apply and nothing overflows, never how it looks. Residue (his iPhone): the layout in
the Home Screen app, and whether any dictation control appears there at all."""
from __future__ import annotations

import json
import re
from pathlib import Path

from levain.cockpit import room as cockpit_room
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_room import _serve_room, w  # noqa: F401  (fixture)
from tests.test_cockpit_k2a import TOKEN
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

WEB = Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web"
PROSE = {"autocapitalize": "sentences", "autocorrect": "on", "spellcheck": "true", "inputmode": "text", "autocomplete": "off"}

_STYLED = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<link rel="stylesheet" href="/dashboard.css"><body class="ke"><div id="host"></div>'
           '<script src="/room_view.js"></script><script src="/rvc.js"></script>')


def _assets(boot):
    return {"/rvc.html": ("text/html; charset=utf-8", _STYLED.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}


def _boot(speech):
    return ('%s window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: %s,'
            ' headers: () => ({"X-Levain-Write-Token": "%s"})});' % (_FAKE, speech, TOKEN))


_FAKE = """
window.__started = [];
class Local { start() { window.__started.push({local: this.processLocally}); } stop() {} }
Local.prototype.processLocally = false;
class Cloud { start() { window.__started.push({cloud: true}); } stop() {} }
"""


def test_the_room_boxes_are_prose_with_dash_free_names():
    """The same pin as tests/test_drop_box_is_prose.py, for the room's two text boxes (the draft and a card's words)."""
    js = (WEB / "room_view.js").read_text()
    assert 'prose(ta, "roomdraft")' in js and 'prose(ta, "roomcardwords")' in js
    body = js[js.index("function prose("):][:400]
    for k, v in PROSE.items():
        assert f'["{k}", "{v}"]' in body, k
    assert '["name", name]' in body
    assert "one-time-code" not in js and "inputmode=\"none\"" not in js and '"none"' not in re.findall(r'"inputmode"[^)]*', js)[0]


def test_the_phone_rules_exist_and_use_tokens_only():
    css = (WEB / "dashboard.css").read_text()
    i = css.index("RM-H, the phone")
    block = css[i:css.index("/* C12:", i)]
    assert "font-size: 16px" in block and "env(safe-area-inset-bottom" in block and "position: sticky" in block
    assert "grid-template-columns: repeat(2" in block and ".rv-send { grid-column: 1 / -1" in block
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)


def test_voice_never_names_a_cloud_recognizer_or_a_transcribe_route():
    js = (WEB / "room_view.js").read_text()
    for word in ("getUserMedia", "MediaRecorder", "audio/transcriptions", "api.openai.com"):
        assert word not in js, word
    assert 'Speech.prototype && "processLocally" in Speech.prototype' in js and "r.processLocally = true" in js


@needs_browser
def test_a_recognizer_that_cannot_stay_on_the_device_gets_no_control(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Cloud"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"},
            {"eval": "document.querySelectorAll('.rv-mic').length"},
        ])
    assert out["values"][1] == 0


@needs_browser
def test_an_on_device_recognizer_gets_a_control_that_asks_for_local_processing(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Local"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-mic')"},
            {"eval": "document.querySelector('.rv-mic').click(); JSON.stringify(window.__started)"},
        ])
    assert json.loads(out["values"][1]) == [{"local": True}]


_GEOM = """(() => { const r = n => n.getBoundingClientRect();
const acts = [...document.querySelectorAll('.rv-actions > button')], dock = r(document.querySelector('.rv-dock'));
return JSON.stringify({noSideScroll: document.documentElement.scrollWidth <= window.innerWidth,
  inside: acts.every(b => r(b).left >= dock.left - 0.5 && r(b).right <= dock.right + 0.5),
  tall: acts.every(b => r(b).height >= 43.5), unclipped: acts.every(b => b.scrollWidth <= b.clientWidth + 1),
  sendWide: r(document.querySelector('.rv-send')).width > r(document.querySelector('.rv-drop')).width,
  rows: new Set(acts.map(b => Math.round(r(b).top))).size, n: acts.length,
  input16: parseFloat(getComputedStyle(document.querySelector('.rv-input')).fontSize),
  sticky: getComputedStyle(document.querySelector('.rv-dock')).position}); })()"""


@needs_browser
def test_the_dock_fits_a_390_phone_with_every_control_whole(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Local"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"emulate": {"w": 390, "h": 800, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('.rv-mic') && document.querySelector('.rv-rotate')"},
            {"eval": _GEOM},
        ])
    g = json.loads(out["values"][1])
    assert g["noSideScroll"] and g["inside"] and g["tall"] and g["unclipped"] and g["sendWide"], g
    assert g["n"] == 5 and g["rows"] <= 3 and g["input16"] >= 16 and g["sticky"] == "sticky", g
