"""How the cockpit's work-thread view draws a Read's result and when its Stop can be pressed (1105-49, the thread-view twin of
the room's two defects). The items are the ones the page is really given: each is built by ``threads._item`` from a timeline
row. STRUCTURAL: the page is mounted in headless Chrome over a stubbed read; how it looks on his iPhone is his glass."""
from __future__ import annotations

import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

from levain.cockpit import threads
from levain.desk.chat import TimelineRow
from tests.test_cockpit_flip_parity import NODE, _drive, needs_browser

WEB = Path(threads.__file__).resolve().parents[1] / "templates" / "web"
_PAGE = '<!doctype html><meta charset="utf-8"><div id="host"></div><script src="/thread_view.js"></script><script src="/tvc.js"></script>'


def _row(gseq, kind, source, ref, launch, body, status=None):
    return TimelineRow(gseq, kind, source, ref, launch, status, body, "2026-11-05T21:48:38+00:00", None)


def _results(*bodies, tool_name=None):
    """The items the page is given for results of ``tool_name`` (None: a row stored before the name was kept, no fact)."""
    facts = {} if tool_name is None else {("L1", "tool_result:toolu_x%d" % i): (None, 0, 0, tool_name) for i in range(len(bodies))}
    return [threads._item(_row(i + 1, "vendor_tool_result", "vendor", "tool_result:toolu_x%d" % i, "L1", b), facts, set())
            for i, b in enumerate(bodies)]


def _turn(gseq, kind, launch):
    return threads._item(_row(gseq, kind, "vendor", "%s:%d" % (kind, gseq), launch,
                              "success completed" if kind == "vendor_turn_ended" else None), {}, set())


def _said(gseq):
    return threads._item(_row(gseq, "message", "operator", "cm-%d" % gseq, None, "hi", status="accepted"), {}, set())


def _boot(stream, stoppable=True, stop_outcome="confirmed", launch="L1", gap=False):
    return ('window.__body = {thread_id: "t1", launch_id: %s, stoppable: %s, gap: %s, model: null, authority: null, before: [], stream: %s};\n'
            'window.__posts = [];\n'
            'const stub = (u, o) => { const post = o && o.method === "POST"; if (post) window.__posts.push(u);\n'
            '  const out = post ? {outcome: "%s"} : window.__body;\n'
            '  return Promise.resolve({status: 200, ok: true, text: () => Promise.resolve(JSON.stringify(out))}); };\n'
            'window.__tv = LevainThreadView.mount(document.getElementById("host"), {pollMs: 0, fetch: stub, headers: () => ({}), stop: true});\n'
            'window.__tv.open("t1");'
            % (json.dumps(launch), "true" if stoppable else "false", json.dumps(gap), json.dumps(stream), stop_outcome))


class _Page:
    """A page that serves the real thread_view.js and a boot script; nothing else."""
    def __init__(self, boot):
        files = {"/": ("text/html; charset=utf-8", _PAGE.encode()),
                 "/thread_view.js": ("text/javascript; charset=utf-8", (WEB / "thread_view.js").read_bytes()),
                 "/tvc.js": ("text/javascript; charset=utf-8", boot.encode())}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                ctype, body = files.get(self.path.split("?")[0], ("text/plain", b""))
                self.send_response(200 if self.path.split("?")[0] in files else 404)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def __enter__(self):
        return self.base

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


_DUMP = """(() => JSON.stringify([...document.querySelectorAll('.tv-line')].map(n => ({
  kind: n.querySelector('.tv-kind').textContent, text: (n.querySelector('.tv-body') || {}).textContent || null,
  peek: (n.querySelector('.tv-peek') || {}).textContent || null}))))()"""


def _lines(tmp_path, stream):
    with _Page(_boot(stream)) as base:
        out = _drive(tmp_path, base + "/", [{"until": "document.querySelectorAll('.tv-line').length >= %d" % len(stream)}, {"eval": _DUMP}])
    return json.loads(out["values"][1])


# the bodies the room's own test uses (tests/test_room_rows_1105.py): the strip is one rule drawn in two places
BODIES = ["     1\t# CLAUDE.md — flow\n     2\t\n     3\tbody line",
          "1\thello from the room scratch dir\n2\t",
          "     1\t2\tx",
          "42 apples\n43 pears",
          "12:a grep hit\n13:another",
          "     1\ta\n     5\tb",
          "<tool_use_error>3\tnot a file</tool_use_error>",
          "7\tstarts at seven\n8\tnext"]
STRIPPED = ["# CLAUDE.md — flow\n\nbody line", "hello from the room scratch dir\n", "2\tx", "42 apples\n43 pears", "12:a grep hit\n13:another",
            "a\n     5\tb", "<tool_use_error>3\tnot a file</tool_use_error>", "starts at seven\nnext"]


@needs_browser
def test_a_reads_line_numbers_are_not_drawn_and_no_other_leading_number_is_touched(tmp_path):
    """Reproduced: the body of a Read's result drew "     1\\t# CLAUDE.md ..." and its peek "1\\t# CLAUDE.md". The vendor prefixes every
    line with a right-aligned number and a tab (``cat -n``); a work thread's result carries no tool name either, so the test is
    the shape: the leading run of consecutive "<number><tab>" lines. Only a tool result is read this way."""
    agent = threads._item(_row(20, "vendor_agent_text", "vendor", "agent_text:20", "L1", "1\tan agent line\n2\tmore"), {}, set())
    for name in ("Read", None):                                              # a Read, and a row stored before the name was kept
        g = _lines(tmp_path, _results(*BODIES, tool_name=name) + [agent])
        assert [x["text"] for x in g[:len(BODIES)]] == STRIPPED, (name, g)
        assert g[0]["peek"] == "# CLAUDE.md — flow", g[0]                    # the one-line summary of the closed row, too
        assert g[-1]["text"] == "1\tan agent line\n2\tmore", g[-1]            # an agent's own words are never read as a file


@needs_browser
def test_a_result_of_another_tool_keeps_its_numbers(tmp_path):
    """Reproduced (1106-1 striptool): a Bash result ``1\\talpha\\n2\\tbeta`` (``nl``, a table, a cut) drew without its numbers, because the
    strip went by shape alone. The row now carries the vendor's tool name (``tool_name``) and only "Read" is stripped."""
    body = "1\talpha\n2\tbeta\n3\tgamma"
    for name in ("Bash", "Grep", "read", "Read "):                            # an exact match only: no case folding, no trimming
        g = _lines(tmp_path, _results(body, tool_name=name))
        assert g[0]["text"] == body, (name, g)
    assert _lines(tmp_path, _results(body, tool_name="Read"))[0]["text"] == "alpha\nbeta\ngamma"
    assert _lines(tmp_path, _results(body))[0]["text"] == "alpha\nbeta\ngamma"      # no name stored: today's shape rule

def _stop_state(tmp_path, stream, stoppable=True, launch="L1", gap=False):
    """None when the thread draws no Stop, else whether the Stop button is disabled."""
    with _Page(_boot(stream, stoppable, launch=launch, gap=gap)) as base:
        out = _drive(tmp_path, base + "/", [{"until": "document.querySelector('.tv-title')"},
                                            {"eval": "JSON.stringify((() => { const b = document.querySelector('.tv-stop'); return b ? b.disabled : null; })())"}])
    return json.loads(out["values"][1])


@needs_browser
def test_stop_is_available_only_while_a_turn_is_running(tmp_path):
    """Reproduced: a work thread whose session was held (``stoppable``) drew an enabled Stop whether or not a turn ran. A turn
    is running when the newest launch's latest turn row is a start with no end after it (as in the room)."""
    ended = [_said(1), _turn(2, "vendor_turn_started", "L1"), _turn(3, "vendor_turn_ended", "L1")]
    running = ended[:2]
    assert _stop_state(tmp_path, [], True) is True                                   # nothing said yet: nothing to stop
    assert _stop_state(tmp_path, ended, True) is True                                # idle: the turn ended
    assert _stop_state(tmp_path, running, True) is False                             # a turn is running
    assert _stop_state(tmp_path, ended + [_said(4), _turn(5, "vendor_turn_started", "L1")], True) is False
    # a turn left open by an earlier launch that died is not the new launch's turn
    assert _stop_state(tmp_path, running + [_turn(4, "vendor_turn_started", "L2"), _turn(5, "vendor_turn_ended", "L2")], launch="L2") is True
    assert _stop_state(tmp_path, running + [_turn(4, "vendor_agent_text", "L2")], launch="L2") is True
    # the server's word still decides whether a Stop exists at all
    assert _stop_state(tmp_path, running, False) is None


# The fixtures of the landing 12 L3 finding, shared by the thread's own Stop test and the room-versus-thread agreement test:
# (name, stream, the body's launch_id, the body's gap, whether a turn is running). The server names the current launch and says gap.
_C_START, _C_END = _turn(2, "vendor_turn_started", "L2"), _turn(3, "vendor_turn_ended", "L2")
_OLD_START, _OLD_END = _turn(9, "vendor_turn_started", "L1"), _turn(9, "vendor_turn_ended", "L1")
_C_TEXT = threads._item(_row(4, "vendor_agent_text", "vendor", "agent_text:4", "L2", "x"), {}, set())
RUNNING_CASES = [
    ("gapped, only a non-turn row of the current launch", [_said(1), _C_TEXT], "L2", True, True),      # the start scrolled out: unknown is running
    ("gapped, nothing of the launch", [_said(1)], "L2", True, True),
    ("no gap, no turn row", [], "L2", False, False),
    ("no gap, only a said line", [_said(1)], "L2", False, False),
    ("gapped but the end is seen", [_C_START, _C_END], "L2", True, False),                              # a gap does not outvote a seen end
    ("gapped, a start seen", [_C_START], "L2", True, True),
    ("a late old-launch end beside a running turn", [_C_START, _OLD_END], "L2", False, True),
    ("a late old-launch start after the turn ended", [_C_START, _C_END, _OLD_START], "L2", False, False),
    ("an old start before the current start", [_OLD_START, _C_START], "L2", False, True),
    ("only an old launch's open turn", [_OLD_START], "L2", False, False),
    ("no launch named", [_C_START], None, True, False),
]


@needs_browser
def test_a_gapped_stream_and_a_late_row_of_another_launch_read_in_the_work_thread_as_in_the_room(tmp_path):
    """Reproduced on the first fix (landing 12 L3): a gapped stream whose start row was not in it left Stop disabled on a live
    turn, and a late turn row of an older or worker launch reset the newest-launch scan. Only the current launch's turn rows count
    (``launch_id`` is the body's), and a gap with none of them is running."""
    for name, stream, launch, gap, running in RUNNING_CASES:
        if launch is None:
            continue
        assert _stop_state(tmp_path, stream, True, launch=launch, gap=gap) is (not running), name


@needs_browser
def test_stop_follows_the_turn_as_the_thread_is_read_again_and_a_raced_stop_keeps_its_words(tmp_path):
    ended = [_said(1), _turn(2, "vendor_turn_started", "L1"), _turn(3, "vendor_turn_ended", "L1")]
    stop = "(() => { const b = document.querySelector('.tv-stop'); return b ? b.disabled : null; })()"
    with _Page(_boot(ended[:2], stop_outcome="no_such_request")) as base:
        out = _drive(tmp_path, base + "/", [
            {"until": "document.querySelector('.tv-stop') && document.querySelector('.tv-stop').disabled === false"},
            # the turn ended on the server a moment before the tap: the stop is sent, the server says nothing was running
            {"eval": "window.__body.stream = %s; document.querySelector('.tv-stop').click(); 1" % json.dumps(ended)},
            {"until": "document.querySelector('.tv-sendnote').textContent === 'Nothing was running to stop.' && window.__posts.length === 1"},
            {"eval": "JSON.stringify(%s)" % stop},
            # a new turn starts and the next read enables Stop again
            {"eval": "window.__body.stream = %s; window.__tv.open('t1'); 1" % json.dumps(ended[:2] + [_turn(4, "vendor_turn_ended", "L1"), _turn(5, "vendor_turn_started", "L1")])},
            {"until": "document.querySelector('.tv-stop').disabled === false"},
            {"eval": "JSON.stringify(%s)" % stop}])
    assert json.loads(out["values"][3]) is True, out["values"]        # the re-read after the stop shows the turn over
    assert json.loads(out["values"][6]) is False, out["values"]


# --- the two copies of the strip agree ----------------------------------------------------------------------------------

_AGREE = """const fs = require("fs");
const rv = require(process.argv[2] + "/room_view.js"), tv = require(process.argv[2] + "/thread_view.js");
const bodies = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const item = (b, name) => { const it = {gseq: 1, kind: "vendor_tool_result", source: "vendor", ref: "tool_result:x", launch_id: "L1", body: b, at: ""};
  if (name !== "absent") it.tool_name = name; return it; };
const out = {};
for (const name of ["Read", "Bash", null, "absent"]) {
  out[String(name)] = bodies.map(b => ({room: rv.lineModel(item(b, name)).text,
    thread: tv.threadModel({thread_id: "t1", launch_id: "L1", stream: [item(b, name)]}).stream[0].text}));
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_the_room_and_the_thread_strip_a_reads_numbers_alike(tmp_path):
    """room_view.js and thread_view.js are separate served files with no shared loader, so each holds its own copy of the strip.
    Two pieces of code computing one thing: on every fixture the two views give the same text (error bodies and refusals are the
    room's own business and are left out of this comparison)."""
    script, fixtures = tmp_path / "agree.js", tmp_path / "bodies.json"
    script.write_text(_AGREE)
    bodies = BODIES[:6] + BODIES[7:] + ["     9\tonly one\n", "no number here", "", "1\ta\n2\tb\n3\tc\n5\td", " 10\tx\n 11\ty"]
    fixtures.write_text(json.dumps(bodies))
    res = subprocess.run([NODE, str(script), str(WEB), str(fixtures)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    by_name = json.loads(res.stdout)
    for name, got in by_name.items():                    # under every name (a missing key and a null alike) the two views agree
        assert [g["room"] for g in got] == [g["thread"] for g in got], (name, got)
    assert by_name["Read"][0]["thread"] == "# CLAUDE.md — flow\n\nbody line"
    assert by_name["null"] == by_name["absent"] == by_name["Read"]          # no name: today's shape rule, the Read's answer
    assert [g["thread"] for g in by_name["Bash"]] == bodies         # any other tool: drawn exactly as recorded


_AGREE_RUNNING = """const fs = require("fs");
const rv = require(process.argv[2] + "/room_view.js"), tv = require(process.argv[2] + "/thread_view.js");
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(cases.map(c => ({
  room: rv.roomModel({room_thread: "t1", launch_id: c.launch, gap: c.gap, composer: {state: "writable"}, stream: c.stream}).running,
  thread: tv.threadModel({thread_id: "t1", launch_id: c.launch, gap: c.gap, stream: c.stream}).running}))));
"""


@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_the_room_and_the_thread_agree_on_whether_a_turn_is_running_on_every_fixture(tmp_path):
    """turnRunning is copied by design into both served files: on every fixture (the gapped window, the late old-launch rows, the
    idle cases) the two models give the same answer, and it is the expected one."""
    script, fixtures = tmp_path / "agree_running.js", tmp_path / "cases.json"
    script.write_text(_AGREE_RUNNING)
    fixtures.write_text(json.dumps([{"stream": st, "launch": ln, "gap": gap} for _n, st, ln, gap, _r in RUNNING_CASES]))
    res = subprocess.run([NODE, str(script), str(WEB), str(fixtures)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert [(g["room"], g["thread"]) for g in got] == [(r, r) for *_x, r in RUNNING_CASES], list(zip([c[0] for c in RUNNING_CASES], got))
