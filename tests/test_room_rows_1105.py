"""How the cockpit room draws a tool's rows and the room's own replies (roomfix, 1105-24). The items are the ones the
page is really given: each is built by ``threads._item`` from a timeline row, the bodies are the words seen on the live page.
STRUCTURAL: the page is mounted in headless Chrome over a stubbed read; how it reads on his iPhone is his glass."""
from __future__ import annotations

import json
import re
from pathlib import Path

from levain.cockpit import room as cockpit_room
from levain.cockpit import threads
from levain.coordinator.chat import TimelineRow
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_room import _serve_room, w  # noqa: F401  (fixture)
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

WEB = Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web"
_PAGE = ('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/dashboard.css"><div id="host"></div>'
         '<script src="/room_view.js"></script><script src="/rvc.js"></script>')

DENIED = "<tool_use_error>File is in a directory that is denied by your permission settings.</tool_use_error>"
MISSING = "<tool_use_error>File does not exist.</tool_use_error>"
FILE = "1\thello from the room scratch dir\n2\t"
REPLY = "I will not read **`.env.x` decoy:** that is a `bash` call, x ** y and z** w stay put, and this **never closes and a `tick never closes\nsee **q and r ** s here"
FENCED = "**intro**\n```py\n**x** and `y`\n```\ntail `t` and <b>raw</b>"
YOURS = "hello **not bold** and `not code`"


def _row(gseq, kind, source, ref, launch, body, status=None):
    return TimelineRow(gseq, kind, source, ref, launch, status, body, "2026-11-05T21:48:38+00:00", None)


def _stream():
    """A tool that ran and is done, one still running, three results, the room's replies, and one line of his own."""
    facts = {("L1", "tool_call:1"): ("toolu_1", 0, 0), ("L1", "tool_call:2"): ("toolu_2", 0, 0)}
    done = {("L1", "toolu_1")}
    rows = [
        _row(1, "vendor_tool_call", "vendor", "tool_call:1", "L1", None),
        _row(2, "vendor_tool_call", "vendor", "tool_call:2", "L1", None),
        _row(3, "vendor_tool_result", "vendor", "tool_result:toolu_1", "L1", DENIED),
        _row(4, "vendor_tool_result", "vendor", "tool_result:toolu_3", "L1", FILE),
        _row(5, "vendor_tool_result", "vendor", "tool_result:toolu_4", "L1", MISSING),
        _row(6, "vendor_agent_text", "vendor", "agent_text:6", "L1", REPLY),
        _row(7, "message", "operator", "cm-7", None, YOURS, status="accepted"),
        _row(8, "vendor_agent_text", "vendor", "agent_text:8", "L1", FENCED),
    ]
    return [threads._item(r, facts, done) for r in rows]


def _boot():
    return ('window.__body = {room_thread: "t1", composer: {state: "writable"}, model: "m", stream: %s, proposals: [], room_error: null};\n'
            'const stub = (u, o) => Promise.resolve({status: 200, ok: true, text: () => Promise.resolve(JSON.stringify(window.__body))});\n'
            'window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: null, fetch: stub, headers: () => ({})});'
            % json.dumps(_stream()))


def _assets():
    return {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", _boot().encode())}


_LOOK = """(() => {
  const lines = [...document.querySelectorAll('.rv-line')];
  const heads = lines.map(n => n.querySelector('.rv-who').textContent);
  const room = [...document.querySelectorAll('.rv-room .rv-text')];
  const you = document.querySelector('.rv-you .rv-text');
  const texts = lines.map(n => (n.querySelector('.rv-text') || {}).textContent || null);
  return JSON.stringify({
    n: lines.length, heads,
    hiddenTexts: [...document.querySelectorAll('.rv-text')].filter(n => n.hidden).length,
    strong: [...room[0].querySelectorAll('strong')].map(s => s.textContent),
    strongCode: [...room[0].querySelectorAll('strong code')].map(c => c.textContent),
    codes0: [...room[0].querySelectorAll('code')].map(c => c.textContent),
    text0: room[0].textContent,
    fencedPre: [...room[1].querySelectorAll('pre code')].map(c => c.textContent),
    fencedStrong: [...room[1].querySelectorAll('strong')].map(c => c.textContent),
    fencedCodesOutside: [...room[1].querySelectorAll(':scope > code')].map(c => c.textContent),
    fencedStrongInPre: room[1].querySelectorAll('pre strong, pre code code').length,
    rawB: document.querySelectorAll('.rv-text b').length,
    yourMarkup: you.querySelectorAll('strong, code').length, yourText: you.textContent,
    anyMarkupInTools: document.querySelectorAll('.rv-line:not(.rv-room):not(.rv-you) .rv-text *').length,
    angle: texts.filter(t => t && t.indexOf('<tool_use_error>') >= 0).length,
  });
})()"""


@needs_browser
def test_a_tool_that_finished_draws_no_empty_chip_and_one_still_running_says_so(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelectorAll('.rv-line').length >= 7"}, {"eval": _LOOK}])
    g = json.loads(out["values"][1])
    assert g["heads"].count("A tool ran") == 0 and g["heads"].count("A tool is running") == 1, g["heads"]
    assert g["n"] == 7, g["heads"]                    # eight rows, minus the one finished call with nothing to say


@needs_browser
def test_a_tool_result_reads_as_a_sentence_without_its_tag_and_opens_on_a_tap(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelectorAll('.rv-line').length >= 7"}, {"eval": _LOOK},
            {"eval": "const b = [...document.querySelectorAll('.rv-who')].find(n => n.textContent.indexOf('A tool was refused') === 0);"
                     " const t = b.nextSibling; const before = t.hidden; b.click();"
                     " JSON.stringify({before, after: t.hidden, text: t.textContent, expanded: b.getAttribute('aria-expanded'), head: b.textContent})"}])
    g = json.loads(out["values"][1])
    heads = g["heads"]
    assert "A tool was refused — File is in a directory that is denied by your permission settings." in heads, heads
    assert "A tool failed — File does not exist." in heads, heads
    assert "A tool result — 1\thello from the room scratch dir" in heads, heads
    assert not [h for h in heads if "vendor" in h or "<" in h], heads
    assert g["angle"] == 0 and g["anyMarkupInTools"] == 0
    o = json.loads(out["values"][2])
    assert o["before"] is True and o["after"] is False and o["expanded"] == "true"
    assert o["text"] == "File is in a directory that is denied by your permission settings."
    assert o["head"] == "A tool was refused"


@needs_browser
def test_the_rooms_replies_show_bold_and_code_as_elements_and_nothing_else_is_parsed(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelectorAll('.rv-line').length >= 7"}, {"eval": _LOOK}])
    g = json.loads(out["values"][1])
    assert g["strong"] == [".env.x decoy:"] and g["strongCode"] == [".env.x"], g
    assert g["codes0"] == [".env.x", "bash"], g                        # the unclosed backtick stays literal
    assert "x ** y and z** w stay put" in g["text0"], g["text0"]       # stars with a space after the opening pair are not bold
    assert "this **never closes and a `tick never closes\nsee **q and r ** s here" in g["text0"], g["text0"]
    assert g["text0"].startswith("I will not read .env.x decoy: that is a bash call"), g["text0"]
    assert g["fencedPre"] == ["**x** and `y`"] and g["fencedStrongInPre"] == 0, g       # a fenced block is code, as before
    assert g["fencedStrong"] == ["intro"] and g["fencedCodesOutside"] == ["t"], g
    assert g["rawB"] == 0                                              # <b> is text, never an element
    assert g["yourMarkup"] == 0 and g["yourText"] == YOURS             # his own line is plain


def test_the_page_script_has_no_markup_sink():
    js = (WEB / "room_view.js").read_text()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|createContextualFragment", js)
