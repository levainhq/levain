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
         '<link rel="stylesheet" href="/dashboard.css"><body class="ke"><div id="host"></div>'
         '<script src="/room_view.js"></script><script src="/rvc.js"></script>')
_BOOT = """
window.__body = {room_thread: "t1", composer: {state: "writable"}, model: "m", stream: [], proposals: [], room_error: window.__err || null};
for (let i = 1; i <= 30; i++) window.__body.stream.push({gseq: i, kind: i % 2 ? "message" : "vendor_agent_text", source: i % 2 ? "operator" : "vendor",
  body: i === 30 ? "newest\\n\\n```py\\nx = 1 < 2\\n```\\n```unclosed\\nstill text" : "line " + i + " " + "word ".repeat(40)});
document.getElementById("host").style.height = "100dvh";      // the room fills its host: the page gives it a height (inline style is CSP-refused, CSSOM is not)
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
            {"eval": "window.__body = Object.assign({}, window.__body, {room_thread: 't2'}); window.__rv.refresh().then(() => document.querySelectorAll('.rv-banner').length)"},
        ])
    n = json.loads(out["values"][1])
    assert n["lead"].startswith("The room ran into a problem") and n["detail"].startswith("The room reported: pump: CoordError") and n["wrap"] == "anywhere" and n["fits"], n
    assert out["values"][2] == 0      # the old thread still on the wire: the old error stays hushed for that poll
    assert out["values"][3] == 1      # the same error persisting on the new conversation shows the banner


def test_the_room_has_a_jump_entry_in_the_views_strip_that_targets_its_zone():
    html = (WEB / "dashboard.html").read_text()
    core = (WEB / "dashboard_core.js").read_text()
    assert html.count('data-phone-jump="room"') == 1
    assert 'room: "[data-zone=\\"room\\"]:not([hidden])"' in core


def test_the_room_rules_keep_the_dock_in_flow_and_the_conversation_the_one_scroller():
    css = (WEB / "dashboard.css").read_text()
    i = css.index(".rv .rv-stage")
    block = css[i:css.index("/* C12:", i)]
    assert ".rv .rv-body { flex: 1 1 auto; min-height: 0; overflow-y: auto;" in block and "max-height: 62" not in block
    assert "overflow-wrap: anywhere" in block and "position: sticky" in block      # the notice, not the dock
    assert "body.rv-on .deck { position: fixed;" in block


def _assets_from(boot):
    return {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}


_ERR = 'room_error: "pump: CoordError: no driver session is attached to launch ln-1da8"'


@needs_browser
def test_stop_removes_the_resize_listener_and_a_resize_after_stop_paints_nothing(tmp_path, w):
    # round 1 (roomfix L3): the resize listener outlived stop() and kept painting a detached jump button
    spy = ('window.__rs = new Set(); const ae = window.addEventListener.bind(window), re = window.removeEventListener.bind(window);'
           'window.addEventListener = (t, h, o) => { if (t === "resize") window.__rs.add(h); return ae(t, h, o); };'
           'window.removeEventListener = (t, h, o) => { if (t === "resize") window.__rs.delete(h); return re(t, h, o); };')
    with _serve_room(tmp_path, w, extra_assets=_assets_from(spy + _BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelectorAll('.rv-line').length >= 30"},
            {"eval": "window.__rs.size"},
            {"eval": "window.__rv.stop(); const j = document.querySelector('.rv-jump'); j.hidden = true; window.dispatchEvent(new Event('resize'));"
                     " new Promise(r => setTimeout(() => r(JSON.stringify({left: window.__rs.size, jumpHidden: j.hidden})), 100))"},
        ])
    assert out["values"][1] == 1, out["values"]
    assert json.loads(out["values"][2]) == {"left": 0, "jumpHidden": True}


@needs_browser
def test_the_same_error_on_the_new_conversation_shows_the_banner_again(tmp_path, w):
    # round 1 (roomfix L3): a repeated identical error stayed hushed forever; a rotate now moves the room to a new thread
    boot = _BOOT.replace('room_error: window.__err || null', _ERR).replace(
        'window.__posts.push(u);', 'window.__posts.push(u); if (/rotate/.test(u)) window.__body = Object.assign({}, window.__body, {room_thread: "t2"});')
    with _serve_room(tmp_path, w, extra_assets=_assets_from(boot)) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-banner')"},
            {"eval": "document.querySelector('.rv-rotate').click(); new Promise(r => setTimeout(() => r(document.querySelectorAll('.rv-banner').length), 600))"},
        ])
    assert out["values"][1] == 1


@needs_browser
def test_a_rotate_the_server_has_not_yet_shown_hides_the_old_error_for_one_poll_only(tmp_path, w):
    boot = _BOOT.replace('room_error: window.__err || null', _ERR)       # the stub keeps answering with the OLD thread
    with _serve_room(tmp_path, w, extra_assets=_assets_from(boot)) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-banner')"},
            {"eval": "document.querySelector('.rv-rotate').click(); new Promise(r => setTimeout(() => r(document.querySelectorAll('.rv-banner').length), 600))"},
            {"eval": "window.__body = Object.assign({}, window.__body, {room_thread: 't2'}); window.__rv.refresh()"
                     ".then(() => document.querySelectorAll('.rv-banner').length)"},
        ])
    assert out["values"][1] == 0 and out["values"][2] == 1, out["values"]


@needs_browser
def test_a_fenced_block_in_a_crlf_body_is_pre_code(tmp_path, w):
    boot = _BOOT.replace('"newest\\n\\n```py\\nx = 1 < 2\\n```\\n```unclosed\\nstill text"', '"newest\\r\\n\\r\\n```py\\r\\nx = 1 < 2\\r\\n```\\r\\n```unclosed\\r\\nstill text"')
    assert "\\r\\n```py" in boot, "the CRLF substitution did not apply"
    with _serve_room(tmp_path, w, extra_assets=_assets_from(boot)) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelectorAll('.rv-line').length >= 30"}, {"eval": _GEO}])
    g = json.loads(out["values"][1])
    assert g["pre"] == 1 and g["code"] == "x = 1 < 2" and not g["raw"], g
