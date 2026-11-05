"""The room's phone layout, its notice and its code block (lane 1104-29). The page is mounted over a stubbed read, so the
room's words are the test's own. STRUCTURAL: headless Chrome proves the boxes do not overlap and the controls show; how it
feels under a thumb, the keyboard and the safe area is his iPhone's."""
from __future__ import annotations

import json
from pathlib import Path

from levain.cockpit import room as cockpit_room
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_room import _serve_room, w  # noqa: F401  (fixture)
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

WEB = Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web"
_PAGE = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
         '<link rel="stylesheet" href="/dashboard.css"><body class="ke"><section class="rv-zone"><div class="ck-zgrid th-grid">'
         '<div class="panel"><div class="pbody"><div id="host"></div></div></div></div></section>'
         '<script src="/room_view.js"></script><script src="/rvc.js"></script>')
_BOOT = """
window.__body = {room_thread: "t1", composer: {state: "writable"}, model: "m", stream: [], proposals: [], room_error: window.__err || null};
for (let i = 1; i <= 30; i++) window.__body.stream.push({gseq: i, kind: i % 2 ? "message" : "vendor_agent_text", source: i % 2 ? "operator" : "vendor",
  body: i === 30 ? "newest\\n\\n```py\\nx = 1 < 2\\n```\\n```unclosed\\nstill text" : "line " + i + " " + "word ".repeat(40)});
window.__posts = [];
const stub = (u, o) => { if (o && o.method === "POST") { window.__posts.push(u); return Promise.resolve({status: 200, ok: true, text: () => Promise.resolve('{"ok":1}')}); }
  return Promise.resolve({status: 200, ok: true, text: () => Promise.resolve(JSON.stringify(window.__body))}); };
window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: null, fetch: stub, headers: () => ({})});
"""
_GEO = """(() => { const r = n => n.getBoundingClientRect(), b = document.querySelector('.rv-body'), d = r(document.querySelector('.rv-dock')),
 last = [...document.querySelectorAll('.rv-line')].pop();
return JSON.stringify({endsAbove: r(b).bottom <= d.top + 0.5, scrolls: b.scrollHeight > b.clientHeight, atEnd: b.scrollHeight - b.scrollTop - b.clientHeight < 49,
  newestShown: r(last).bottom <= r(b).bottom + 1, jumpHidden: document.querySelector('.rv-jump').hidden, dockPos: getComputedStyle(document.querySelector('.rv-dock')).position,
  st: [b.scrollTop, b.scrollHeight, b.clientHeight], pre: document.querySelectorAll('.rv-code code').length, code: (document.querySelector('.rv-code code') || {}).textContent,
  raw: document.body.textContent.indexOf('```py') >= 0, unclosed: document.body.textContent.indexOf('```unclosed') >= 0}); })()"""


def _assets():
    return {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", _BOOT.encode())}


@needs_browser
def test_a_phone_conversation_ends_above_the_dock_shows_the_newest_line_and_offers_a_way_back(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"emulate": {"w": 430, "h": 932, "dpr": 2, "mobile": True}},
            {"until": "document.querySelectorAll('.rv-line').length >= 30"},
            {"eval": _GEO},
            {"eval": "document.querySelector('.rv-body').scrollTop = 0; new Promise(r => setTimeout(() => r(document.querySelector('.rv-jump').hidden), 300))"},
            {"eval": "document.querySelector('.rv-jump').click(); new Promise(r => setTimeout(() => r(document.querySelector('.rv-jump').hidden), 300))"},
        ])
    g = json.loads(out["values"][1])
    assert g["endsAbove"] and g["scrolls"] and g["atEnd"] and g["newestShown"] and g["jumpHidden"], json.dumps(g)
    assert g["dockPos"] != "sticky", g
    assert out["values"][2] is False and out["values"][3] is True      # scrolled up: the control shows; tapped: it is gone and the box is at its end


@needs_browser
def test_a_fenced_block_is_pre_code_from_text_and_an_unclosed_fence_stays_text(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelectorAll('.rv-line').length >= 30"}, {"eval": _GEO}])
    g = json.loads(out["values"][1])
    assert g["pre"] == 1 and g["code"] == "x = 1 < 2" and not g["raw"] and g["unclosed"], g


@needs_browser
def test_the_ended_notice_leads_with_a_sentence_wraps_its_ids_and_clears_on_a_new_conversation(tmp_path, w):
    boot = _BOOT.replace('room_error: window.__err || null', 'room_error: "pump: CoordError: no driver session is attached to launch ln-" + "1da8".repeat(20)')
    assets = {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}
    with _serve_room(tmp_path, w, extra_assets=assets) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"emulate": {"w": 430, "h": 932, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('.rv-banner')"},
            {"eval": "const n = document.querySelector('.rv-banner'); JSON.stringify({lead: n.querySelector('.rv-banner-lead').textContent, detail: n.querySelector('.rv-detail').textContent.slice(0, 40),"
                     " wrap: getComputedStyle(n.querySelector('.rv-detail')).overflowWrap, fits: n.scrollWidth <= n.clientWidth + 1})"},
            {"eval": "document.querySelector('.rv-rotate').click(); new Promise(r => setTimeout(() => r(document.querySelectorAll('.rv-banner').length), 600))"},
        ])
    n = json.loads(out["values"][1])
    assert n["lead"].startswith("The room ran into a problem") and n["detail"].startswith("The room reported: pump: CoordError") and n["wrap"] == "anywhere" and n["fits"], n
    assert out["values"][2] == 0


def test_the_room_has_a_jump_entry_in_the_views_strip_that_targets_its_zone():
    html = (WEB / "dashboard.html").read_text()
    core = (WEB / "dashboard_core.js").read_text()
    assert html.count('data-phone-jump="room"') == 1
    assert 'room: "[data-zone=\\"room\\"]:not([hidden])"' in core


def test_the_phone_rules_free_the_card_clamp_and_keep_the_dock_in_flow():
    css = (WEB / "dashboard.css").read_text()
    i = css.index(".rv .rv-stage")
    block = css[i:css.index("/* C12:", i)]
    assert "body.ke .rv-zone .ck-zgrid .panel { height: auto; max-height: none; }" in block and ".rv .rv-body { max-height: 62vh" in block
    assert "overflow-wrap: anywhere" in block and "position: sticky" in block      # the notice, not the dock
    assert "rv-dock" not in block
