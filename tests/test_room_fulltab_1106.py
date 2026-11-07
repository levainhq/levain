"""1106-51 (Phill, signed row 273: "full-tab Room, one conversation + a New button"): the Room owns the whole tab. Driven in
headless Chrome over the real cockpit page (the Room tab of the views strip), at a desktop (1440x900) and a phone (390x844).
Structural: the numbers below are measured from the built page; how it looks on glass is the desk's look, and nothing here
ran on a real iPhone (the keyboard is not simulated by headless Chrome, so the visual-viewport path is read from the code and
exercised with a stubbed visualViewport)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _serve
from tests.test_cockpit_room_join import _rig
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"

# the page with a token, the Room tab tapped, the room opened, and enough long lines in it that the conversation scrolls
_ENTER = [
    {"until": "document.querySelector('[data-mode-tab=board]')"},
    {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
    {"until": "!window.__pre && document.querySelector('.rv-zone') && document.querySelector('[data-phone-jump=room]')"},
    {"do": "document.querySelector('[data-phone-jump=room]').click(); 1",
     "until": "document.querySelector('.rv-zone .rv-open') && !document.querySelector('.rv-zone .rv-open').hidden"},
    {"eval": "document.querySelector('.rv-open').click(); 1"},
    {"until": "!document.querySelector('.rv-input').disabled && document.querySelector('.rv-open').hidden"},
]
_FILL = ("(async () => { const H = {'Content-Type': 'application/json', 'X-Levain-Write-Token': %s};"
         " const j = await (await fetch('/cockpit/room.json', {headers: H})).json();"
         " for (let i = 0; i < 14; i++) await fetch('/cockpit/room/send', {method: 'POST', headers: H,"
         " body: JSON.stringify({room_thread_id: j.room_thread, body: 'line ' + i + ' ' + 'word '.repeat(60), outbox_id: 'ob-fill-' + i})});"
         " return 1; })()" % json.dumps(TOKEN))
_MEASURE = """(() => { const q = s => document.querySelector(s), r = n => { const b = n.getBoundingClientRect(); return {l: Math.round(b.left * 10) / 10, t: Math.round(b.top * 10) / 10, w: Math.round(b.width * 10) / 10, h: Math.round(b.height * 10) / 10, b: Math.round(b.bottom * 10) / 10}; };
  const pad = n => { const c = getComputedStyle(n); return parseFloat(c.paddingLeft) + parseFloat(c.paddingRight); };
  const stream = q('.rv-stream'), visible = n => !!n && n.getClientRects().length > 0;
  return JSON.stringify({vw: innerWidth, vh: innerHeight, scrollH: document.documentElement.scrollHeight, scrollW: document.documentElement.scrollWidth,
    stage: r(q('.rv-stage')), body: r(q('.rv-body')), top: r(q('.rv-top')), dock: r(q('.rv-dock')), compose: r(q('.rv-compose')), input: r(q('.rv-input')),
    send: r(q('.rv-send')), rotate: r(q('.rv-rotate')), stream: r(stream), column: Math.round((stream.getBoundingClientRect().width - pad(stream)) * 10) / 10,
    scrolls: q('.rv-body').scrollHeight > q('.rv-body').clientHeight, rotateText: q('.rv-rotate').textContent,
    history: document.querySelectorAll('.rv-zone [class*=history], .rv-zone [class*=thread-list], .rv-zone .th-body').length,
    threadsShown: visible(q('.th-zone')), cardChrome: getComputedStyle(q('.rv-zone')).borderTopWidth + ' ' + getComputedStyle(q('.rv')).borderTopWidth + ' ' + getComputedStyle(q('.rv')).boxShadow,
    bodyOverflow: getComputedStyle(document.body).overflow, mastTop: r(q('.masthead')).t, strip: r(q('#ck-phonetabs')),
    inputFont: parseFloat(getComputedStyle(q('.rv-input')).fontSize), mainShown: visible(q('main.frame')), jump: q('.rv-jump').hidden}); })()"""


def _shot(name):
    d = os.environ.get("LEVAIN_ROOM_SHOTS")
    return [{"shot": str(Path(d) / name), "full": False}] if d else []


def _run(tmp_path, qdir, emulate, shots=()):
    rs, _launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [{"emulate": emulate}] + _ENTER + [{"eval": _FILL}, {"until": "document.querySelectorAll('.rv-line').length >= 14"},
                     {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}, {"eval": _MEASURE}] + [s for n in shots for s in _shot(n)])
    return json.loads(out["values"][_index(out)])


def _index(out):
    return max(i for i, v in enumerate(out["values"]) if isinstance(v, str) and v.startswith("{") and '"vw"' in v)


def _common(g, frac=0.6):
    # the room owns the tab: the page itself does not scroll, the conversation is the one scroller, nothing else of the page shows
    assert g["scrollH"] <= g["vh"] + 1 and g["scrollW"] <= g["vw"] + 1, g
    assert g["bodyOverflow"] == "hidden" and g["scrolls"], g
    assert not g["mainShown"] and not g["threadsShown"] and g["history"] == 0, g       # no history list, no threads, the board is gone
    assert g["cardChrome"].startswith("0px 0px"), g                                       # no card chrome around the room
    # the composer is pinned to the bottom of the viewport
    assert abs(g["dock"]["b"] - g["vh"]) <= 1, g
    assert g["stage"]["h"] >= frac * g["vh"] - 1, g
    assert g["rotateText"] == "New" and g["rotate"]["w"] >= 44 and g["rotate"]["h"] >= 44, g
    assert g["send"]["w"] >= 44 and g["send"]["h"] >= 44 and g["inputFont"] >= 16, g
    assert g["compose"]["h"] >= 48, g


@needs_browser
def test_desktop_room_is_the_whole_tab_with_a_centred_column_and_a_pinned_composer(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 1440, "h": 900, "dpr": 1}, shots=["room_1440x900.png"])
    _common(g)
    assert g["stage"]["h"] >= 0.6 * g["vh"], g
    assert g["column"] == 768 and g["stream"]["w"] <= 768 + 48 + 0.5, g                    # the text column at >= 1100: 48rem (ChatGPT, measured)
    assert g["compose"]["w"] == 768 and g["compose"]["h"] >= 52, g                        # the composer: 768 wide, 52+ tall
    assert abs((g["compose"]["l"] + g["compose"]["w"] / 2) - g["vw"] / 2) <= 1, g         # centred
    assert abs((g["stream"]["l"] + g["stream"]["w"] / 2) - g["vw"] / 2) <= 1, g
    assert g["top"]["h"] == 52, g


@needs_browser
def test_a_1024_desktop_gets_the_672_text_column(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 1024, "h": 768, "dpr": 1})
    _common(g, 0.55)       # a short desktop: the app's own header and strip take 104 of 768
    assert g["column"] == 672 and g["compose"]["w"] == 768, g


@needs_browser
def test_tablet_room_768x1024(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 768, "h": 1024, "dpr": 2, "mobile": True}, shots=["room_768x1024.png"])
    _common(g)
    assert g["column"] <= 672 and g["compose"]["w"] <= 768, g


@needs_browser
def test_phone_room_is_full_width_with_16px_gutters_and_a_composer_above_the_bottom(tmp_path, qdir):
    g = _run(tmp_path, qdir, {"w": 390, "h": 844, "dpr": 3, "mobile": True}, shots=["room_390x844.png"])
    _common(g)
    assert g["stage"]["h"] >= 0.6 * g["vh"] - 1, g
    assert g["stream"]["l"] == 0 and g["stream"]["w"] == 390 and g["column"] == 390 - 32, g    # full width, 16px gutters
    assert abs(g["compose"]["l"] - 16) <= 0.5 and abs(g["compose"]["w"] - (390 - 32)) <= 0.5, g
    assert g["compose"]["h"] >= 48 and g["top"]["h"] == 56, g


@needs_browser
def test_the_composer_follows_the_visual_viewport_above_a_keyboard(tmp_path, qdir):
    """headless Chrome shows no keyboard: a visualViewport that reports a shorter height stands in for one."""
    stub = ("(() => { const vv = window.visualViewport; const real = {h: vv.height};"
            " Object.defineProperty(vv, 'height', {configurable: true, get: () => window.__vvh || real.h});"
            " window.__vvh = 520; vv.dispatchEvent(new Event('resize')); return 1; })()")
    rs, _launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [{"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}] + _ENTER +
                     [{"eval": stub}, {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": _MEASURE}])
    g = json.loads(out["values"][_index(out)])
    # the deck is the visual viewport's 520, the composer's bottom is that box's bottom, and the app's header yields room
    assert abs(g["dock"]["b"] - 520) <= 1, g
    assert g["stage"]["h"] >= 0.4 * 520, g


def test_the_room_has_no_history_list_or_card_in_the_markup_and_keeps_its_one_ask():
    js = (WEB / "room_view.js").read_text()
    boot = (WEB / "dashboard_boot.js").read_text()
    css = (WEB / "dashboard.css").read_text()
    assert "history" not in js.lower() and 'el("section", "rv-zone")' in boot
    assert "ck-zgrid th-grid" not in boot[boot.index("function buildRoom"):boot.index("function roomOn")]
    assert "visualViewport" in js and "100dvh" in css
