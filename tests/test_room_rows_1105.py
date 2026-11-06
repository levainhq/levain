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


def _boot(stream=None):
    return ('window.__body = {room_thread: "t1", composer: {state: "writable"}, model: "m", stream: %s, proposals: [], room_error: null};\n'
            'const stub = (u, o) => Promise.resolve({status: 200, ok: true, text: () => Promise.resolve(JSON.stringify(window.__body))});\n'
            'window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: null, fetch: stub, headers: () => ({})});'
            % json.dumps(_stream() if stream is None else stream))


def _assets(stream=None):
    return {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", _boot(stream).encode())}


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
            {"eval": "const b = [...document.querySelectorAll('.rv-who')].find(n => n.textContent.indexOf('A read was refused') === 0);"
                     " const t = b.nextSibling; const before = t.hidden; b.click();"
                     " JSON.stringify({before, after: t.hidden, text: t.textContent, expanded: b.getAttribute('aria-expanded'), head: b.textContent})"}])
    g = json.loads(out["values"][1])
    heads = g["heads"]
    assert "A read was refused — That is outside what the room can read, so nothing was read." in heads, heads
    assert not [h for h in heads if "permission settings" in h or "blockReads" in h], heads    # never the vendor's wording
    assert "A tool failed — File does not exist." in heads, heads
    assert "A tool result — hello from the room scratch dir" in heads, heads     # the Read's own line number is not drawn
    assert not [h for h in heads if "vendor" in h or "<" in h], heads
    assert g["angle"] == 0 and g["anyMarkupInTools"] == 0
    o = json.loads(out["values"][2])
    assert o["before"] is True and o["after"] is False and o["expanded"] == "true"
    assert o["text"] == "That is outside what the room can read, so nothing was read."
    assert o["head"] == "A read was refused"


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


_DUMP = """(() => JSON.stringify([...document.querySelectorAll('.rv-line')].map(n => ({
  head: n.querySelector('.rv-who').textContent, text: (n.querySelector('.rv-text') || {}).textContent || null}))))()"""


def _results(*bodies):
    return [threads._item(_row(i + 1, "vendor_tool_result", "vendor", "tool_result:toolu_x%d" % i, "L1", b), {}, set())
            for i, b in enumerate(bodies)]


def _lines(tmp_path, w, stream, want):
    with _serve_room(tmp_path, w, extra_assets=_assets(stream)) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [{"until": "document.querySelectorAll('.rv-line').length >= %d" % want}, {"eval": _DUMP}])
    return json.loads(out["values"][1])


HTML = "<html><body>Hello</body></html>"
RESP = "<response>v</response>"
OUTSIDE = ("/etc/hosts is outside /tmp/real1/cwd; the permissions.blockReadsOutsideWorkingDirectories setting blocks reads outside "
           "the working directories. Ask the user to add the directory with /add-dir, or to remove that setting.")


@needs_browser
def test_only_the_vendors_error_tag_is_removed_and_any_other_body_is_drawn_as_recorded(tmp_path, w):
    g = _lines(tmp_path, w, _results(HTML, RESP, "<tool_use_error>X</tool_use_error>\nextra"), 3)
    assert g[0]["text"] == HTML and g[1]["text"] == RESP, g           # a file that is one tag pair is not stripped
    assert g[0]["head"].startswith("A tool result") and g[1]["head"].startswith("A tool result"), g
    assert g[2]["text"] == "X\nextra" and g[2]["head"].startswith("A tool failed"), g    # the tag goes, the words after it stay


@needs_browser
def test_a_tool_is_called_refused_only_by_the_vendors_own_refusal_sentences(tmp_path, w):
    wrap = lambda t: "<tool_use_error>%s</tool_use_error>" % t
    bodies = [wrap("File is in a directory that is denied by your permission settings."), wrap("Permission to use Bash has been denied."),
              wrap(OUTSIDE), wrap("File not found: /home/permissions/x"), wrap("This was refused by the shell"), OUTSIDE,
              wrap("README.md is outside this section; its guide blocks reads outside examples"),    # a look-alike of the third phrase (L3 r2 codex)
              # a tool that RAN and printed the first two sentences, with no error wrapper, is a result and not a refusal (L3 room r1 codex MED)
              "Permission to use Bash has been denied. (a log line a search printed)",
              "File is in a directory that is denied by your permission settings. (a log line a search printed)",
              # two long paths: the vendor's sentence is still replaced whole, never drawn with the path and the setting (L3 room r1 codex MED)
              "/" + "a" * 420 + "/x is outside /" + "b" * 420 + "; the permissions.blockReadsOutsideWorkingDirectories setting blocks "
              "reads outside the working directories."]
    g = _lines(tmp_path, w, _results(*bodies), 10)
    assert [x["head"].split(" — ")[0] for x in g] == ["A read was refused", "A tool was refused", "A read was refused",
                                                      "A tool failed", "A tool failed", "A read was refused", "A tool failed",
                                                      "A tool result", "A tool result", "A read was refused"], g
    assert "aaaa" not in g[9]["text"] and "permissions." not in g[9]["text"], g[9]
    # the untagged outside-the-cwd denial (the vendor sends it without the wrapper) is read from its words and drawn as the
    # same refusal (roomcanary: it drew "A tool result — /etc/hosts is outside /priv..." with the vendor's path and setting)
    assert not [x for x in g[:7] + g[9:] if "permissions." in (x["text"] or "") or "/tmp/real1" in (x["text"] or "") or "/etc/hosts" in (x["text"] or "")
                or "denied by your" in (x["text"] or "") or "Bash" in (x["text"] or "")], g
    assert [x["text"] for x in g if x["head"].startswith("A tool was refused")] == [
        "That is outside what the room can use, so the tool was not used."], g


@needs_browser
def test_an_empty_call_is_hidden_only_when_the_tool_is_known_to_have_finished(tmp_path, w):
    facts = {("L1", "tool_call:11"): ("toolu_11", 0, 0), ("L1", "tool_call:12"): ("toolu_12", 0, 0)}
    rows = [_row(10, "vendor_tool_call", "vendor", "tool_call:10", "L1", None),     # no facts: no tool object
            _row(11, "vendor_tool_call", "vendor", "tool_call:11", "L1", None),     # finished
            _row(12, "vendor_tool_call", "vendor", "tool_call:12", "L1", None)]     # running
    stream = [threads._item(r, facts, {("L1", "toolu_11")}) for r in rows]
    assert "tool" not in stream[0] and stream[1]["tool"]["open"] is False and stream[2]["tool"]["open"] is True
    g = _lines(tmp_path, w, stream, 2)
    assert [x["head"] for x in g] == ["vendor tool call", "A tool is running"], g


_BIG = """(async () => {
  const body = %s;
  const mk = (b) => ({gseq: 100, kind: "vendor_agent_text", source: "vendor", ref: "agent_text:100", launch_id: "L1", status: null, body: b, at: "2026-11-05T21:48:38+00:00"});
  window.__body.stream = [mk(body)];
  const t = performance.now(); await window.__rv.refresh(); const ms = performance.now() - t;
  const n = document.querySelector('.rv-room .rv-text');
  return JSON.stringify({ms, strong: n.querySelectorAll('strong').length, first: (n.querySelector('strong') || {}).textContent || null, same: n.textContent === body});
})()"""


def _timed(tmp_path, w, *bodies):
    """Each JS expression in bodies is drawn as the room's one reply on the same mounted page; the in-page draw time and what came out."""
    steps = [{"until": "document.querySelectorAll('.rv-line').length >= 7"}] + [{"eval": _BIG % b} for b in bodies]
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", steps)
    return [json.loads(v) for v in out["values"][1:]]


@needs_browser
def test_a_huge_line_of_unpaired_bold_openers_renders_fast_and_a_line_past_20000_is_drawn_plain(tmp_path, w):
    huge, over, under, worst = _timed(tmp_path, w, '"**a ".repeat(25000)',      # 100k characters on one line: ~10 s before the bound
                                      '"**a**".repeat(5000)',                   # 25k characters, every pair would be bold: drawn plain
                                      '"**a**".repeat(3000)',                   # 15k characters: the pass still runs
                                      '"**a ".repeat(4999)')                    # 19996, just under the cap, every opener unpaired
    assert huge["ms"] < 2000 and huge["strong"] == 0 and huge["same"] is True, huge
    assert over["strong"] == 0 and over["same"] is True and over["ms"] < 2000, over
    assert under["strong"] == 3000 and under["same"] is False and under["ms"] < 2000, under
    assert worst["strong"] == 0 and worst["same"] is True and worst["ms"] < 2000, worst


@needs_browser
def test_an_empty_backtick_pair_is_not_a_code_span_to_the_bold_scan_either(tmp_path, w):
    # the bold scan and the inner pass read the empty pair alike: the pair is two literal ticks, the second opens a span to the
    # next tick (hiding the "b**" as a closer), so the one bold runs to the final "**"
    (g,) = _timed(tmp_path, w, '"**a `` b** c ` d**"')
    assert g["strong"] == 1 and g["first"] == "a ` b** c  d", g        # one literal tick, then " b** c " as code, then " d"


def test_the_page_script_has_no_markup_sink():
    js = (WEB / "room_view.js").read_text()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|createContextualFragment", js)


# --- a Read's line numbers, and the Stop button (1105-49, desk 10's look at the live room) --------------------------------

@needs_browser
def test_a_reads_line_numbers_are_not_drawn_and_no_other_leading_number_is_touched(tmp_path, w):
    """Reproduced on the live room: the row read "A tool result — 1 # CLAUDE.md ...". The vendor's Read prefixes every line with
    a right-aligned number and a tab (``cat -n``). The record carries no tool name for a result, and the room has no shell, so a
    result whose lines open with consecutive numbers and a tab is a Read's."""
    read = "     1\t# CLAUDE.md — flow\n     2\t\n     3\tbody line"
    bodies = [read,
              "1\thello from the room scratch dir\n2\t",         # the shape the vendor's stream recorded (no padding)
              "     1\t2\tx",                                     # the file's own text starts with a number and a tab: one prefix goes
              "42 apples\n43 pears",                              # a number that is the result's own words
              "12:a grep hit\n13:another",                        # a grep's numbers (a colon, no tab)
              "     1\ta\n     5\tb",                             # numbers that are not a run stop the strip at the break
              "<tool_use_error>3\tnot a file</tool_use_error>"]   # an error body is never read as a file
    g = _lines(tmp_path, w, _results(*bodies), 7)
    assert [x["text"] for x in g] == ["# CLAUDE.md — flow\n\nbody line", "hello from the room scratch dir\n", "2\tx",
                                      "42 apples\n43 pears", "12:a grep hit\n13:another", "a\n     5\tb", "3\tnot a file"], g
    assert g[0]["head"] == "A tool result — # CLAUDE.md — flow", g[0]


def _stop_boot(stream, state="writable", stop_outcome="confirmed", launch="L1", gap=False):
    return ('window.__body = {room_thread: "t1", launch_id: %s, gap: %s, composer: {state: "%s"}, model: "m", stream: %s, proposals: [], room_error: null};\n'
            'window.__posts = [];\n'
            'const stub = (u, o) => { const post = o && o.method === "POST"; if (post) window.__posts.push(u);\n'
            '  const out = post ? {outcome: "%s"} : window.__body;\n'
            '  return Promise.resolve({status: 200, ok: true, text: () => Promise.resolve(JSON.stringify(out))}); };\n'
            'window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: null, fetch: stub, headers: () => ({})});'
            % (json.dumps(launch), json.dumps(gap), state, json.dumps(stream), stop_outcome))


def _turn(gseq, kind, launch):
    return threads._item(_row(gseq, kind, "vendor", "%s:%d" % (kind, gseq), launch, "success completed" if kind == "vendor_turn_ended" else None),
                         {}, set())


def _said(gseq):
    return threads._item(_row(gseq, "message", "operator", "cm-%d" % gseq, None, "hi", status="accepted"), {}, set())


_STOP = "document.querySelector('.rv-stop').disabled"
_READY = {"until": "window.__rv && window.__rv.model() && document.querySelector('.rv-input')"}


_SERVES = iter(range(1000))


def _stop_disabled(tmp_path, w, stream, state="writable", launch="L1", gap=False):
    boot = _stop_boot(stream, state, launch=launch, gap=gap)
    with _serve_room(tmp_path, w, "serve%d" % next(_SERVES), extra_assets={"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()),
                                                "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [_READY, {"eval": "JSON.stringify(%s)" % _STOP}])
    return json.loads(out["values"][1])


@needs_browser
def test_stop_is_available_only_while_a_turn_is_running(tmp_path, w):
    """Reproduced: an idle room (composer "writable", the last turn ended) and a running one (the last turn started, not ended)
    gave the same model, and Stop was enabled on both. A turn is running when the newest launch's latest turn row is a start."""
    ended = [_said(1), _turn(2, "vendor_turn_started", "L1"), _turn(3, "vendor_turn_ended", "L1")]
    running = ended[:2]
    assert _stop_disabled(tmp_path, w, [], "writable") is True                      # a room with nothing said yet
    assert _stop_disabled(tmp_path, w, ended, "writable") is True                   # idle: the turn ended
    assert _stop_disabled(tmp_path, w, running, "writable") is False                # a turn is running
    assert _stop_disabled(tmp_path, w, running, "not_live") is False                # a stuck turn can still be stopped
    assert _stop_disabled(tmp_path, w, running, "ended") is True                    # no launch to stop
    assert _stop_disabled(tmp_path, w, ended + [_said(4), _turn(5, "vendor_turn_started", "L1")], "writable") is False
    # a turn left open by an earlier launch that died is not the new launch's turn
    assert _stop_disabled(tmp_path, w, running + [_turn(4, "vendor_turn_started", "L2"), _turn(5, "vendor_turn_ended", "L2")], launch="L2") is True
    assert _stop_disabled(tmp_path, w, running + [_turn(4, "vendor_agent_text", "L2")], launch="L2") is True


@needs_browser
def test_a_gapped_stream_with_no_turn_row_of_the_current_launch_is_a_running_turn(tmp_path, w):
    """Reproduced on the first fix (landing 12 L3, finding a): with the stream gapped and a long turn's start row not in it, no turn
    row meant idle and Stop was disabled on a live turn. The server says ``gap`` and names the current launch (``launch_id``):
    a gap with none of that launch's turn rows is running; a seen end still outvotes a gap."""
    cur_start, cur_end = _turn(2, "vendor_turn_started", "L2"), _turn(3, "vendor_turn_ended", "L2")
    assert _stop_disabled(tmp_path, w, [_said(1), threads._item(_row(4, "vendor_agent_text", "vendor", "agent_text:4", "L2", "x"), {}, set())],
                          launch="L2", gap=True) is False
    assert _stop_disabled(tmp_path, w, [_said(1)], launch="L2", gap=True) is False
    assert _stop_disabled(tmp_path, w, [], launch="L2", gap=False) is True                    # no gap, no turn rows: idle, as before
    assert _stop_disabled(tmp_path, w, [_said(1)], launch="L2", gap=False) is True
    assert _stop_disabled(tmp_path, w, [cur_start, cur_end], launch="L2", gap=True) is True    # a gap does not outvote a seen end
    assert _stop_disabled(tmp_path, w, [cur_start], launch="L2", gap=True) is False


@needs_browser
def test_a_late_turn_row_of_another_launch_decides_nothing_about_the_current_one(tmp_path, w):
    """The (b) half of the landing 12 L3 finding, see the test above."""
    cur_start, cur_end = _turn(2, "vendor_turn_started", "L2"), _turn(3, "vendor_turn_ended", "L2")
    late_old_start, late_old_end = _turn(9, "vendor_turn_started", "L1"), _turn(9, "vendor_turn_ended", "L1")
    assert _stop_disabled(tmp_path, w, [cur_start, late_old_end], launch="L2") is False       # the turn is still running
    assert _stop_disabled(tmp_path, w, [cur_start, cur_end, late_old_start], launch="L2") is True   # the turn is over
    assert _stop_disabled(tmp_path, w, [late_old_start, cur_start], launch="L2") is False
    assert _stop_disabled(tmp_path, w, [late_old_start], launch="L2") is True                 # an old launch's open turn is nobody's to stop


@needs_browser
def test_stop_follows_the_turn_as_the_room_is_read_again_and_a_raced_stop_keeps_its_words(tmp_path, w):
    ended = [_said(1), _turn(2, "vendor_turn_started", "L1"), _turn(3, "vendor_turn_ended", "L1")]
    boot = _stop_boot(ended[:2], stop_outcome="no_such_request")
    assets = {"/rvc.html": ("text/html; charset=utf-8", _PAGE.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}
    with _serve_room(tmp_path, w, extra_assets=assets) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            _READY, {"eval": "JSON.stringify(%s)" % _STOP},
            # the turn ended on the server a moment before the tap: the stop is sent, the server says nothing was running
            {"eval": "window.__body.stream = %s; document.querySelector('.rv-stop').click(); 1" % json.dumps(ended)},
            {"until": "document.querySelector('.rv-sendnote').textContent === 'Nothing was running to stop.' && window.__posts.length === 1"},
            {"eval": "JSON.stringify(%s)" % _STOP}])
    assert json.loads(out["values"][1]) is False
    assert json.loads(out["values"][4]) is True, out["values"]       # the re-read after the stop shows the turn over
