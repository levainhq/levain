"""The Desk view's page (levain 33, lane C): levain/templates/web/desk_tab_view.js mounted by dashboard_boot.js over the real cockpit
page, driven in headless Chrome at a phone's size (390x844) first.

A tiny stub server stands in front of the real cockpit: it answers the two desk routes (GET /cockpit/desk.json, POST
/cockpit/desk/send) from the fixture the test sets (behind the same write-token check the real gate makes) and forwards every other
request to the real server. What this proves is the PAGE against `desk_wire.md`, with answers the real routes cannot be made to
give (a dropped connection, a held answer, a hostile string). The page against the REAL routes and port is
`tests/test_desk_join_1113.py`; neither file proves a real flow script or a real phone."""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from levain import web_server
from tests.test_cockpit_flip_parity import NODE, _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "levain" / "templates" / "web"
JS = WEB / "desk_tab_view.js"
ASSET = "/desk_tab_view.js"
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
HOLD_AMBIGUOUS = "It may not have arrived. It is not sent again; say whether it did."

WIRED = {"wired": True, "caps": {"adapter": "host-command", "wake": "poll", "receipts": True, "lease": True, "replies": True, "words_max": 2000},
         "lease": {"state": "held", "holder": "desk 25", "age_s": 190}, "thread_id": None, "events": [], "cursor": None, "readable": True}


def wired(**kw):
    out = json.loads(json.dumps(WIRED))
    out.update(kw)
    return out


def accepted(ref="inbox-1", thread="t1", **kw):
    return {"status": 200, "json": {"outcome": "accepted", "ref": ref, "thread_id": thread, "reason": None, "replayed": False}, **kw}


# --- the stub server ----------------------------------------------------------------------------------------------------------

class DeskStub:
    """The two desk routes from a fixture, behind the write token; everything else goes to the real cockpit (streamed, so a held
    response is not buffered). `sends` and `gets` are what the page asked, in order."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.lock = threading.Lock()
        self.get = {"status": 200, "json": {"wired": False}}
        self.by_thread: dict[str, dict] = {}     # a read that names a thread gets that thread's fixture (else `get`); either may carry a `delay`
        self.script: list[dict] = []
        self.sends: list[dict] = []
        self.gets: list[dict] = []
        self.httpd: ThreadingHTTPServer | None = None

    def set(self, state: dict) -> None:
        with self.lock:
            if "get" in state:
                self.get = state["get"]
            if "by_thread" in state:
                self.by_thread = dict(state["by_thread"])
            if "script" in state:
                self.script = list(state["script"])

    def handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _json(self, status: int, obj) -> None:
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                # a fresh connection for every answer: Chrome re-sends a POST that died on a REUSED socket, which would hide a drop
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

            def _gate(self) -> bool:
                if self.headers.get("X-Levain-Write-Token") != TOKEN:
                    self._json(403, {"error": "token"})
                    return False
                return True

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/cockpit/desk.json":
                    if not self._gate():
                        return
                    q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                    asked = (q.get("thread") or [None])[0]
                    with stub.lock:
                        stub.gets.append({"t": time.monotonic(), "path": self.path, "thread": asked, "after": (q.get("after") or [None])[0]})
                        # "<thread>@<cursor>" answers a read that names that cursor; "<thread>" any other; no thread: `get`
                        fixture = (stub.by_thread.get(f"{asked}@{(q.get('after') or [''])[0]}") or stub.by_thread.get(asked) or stub.get) if asked else stub.get
                    if fixture.get("delay"):
                        time.sleep(fixture["delay"])
                    self._json(fixture["status"], fixture["json"])
                    return
                self._proxy(b"")

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                body = self._body()
                if path == "/__stub/state":
                    stub.set(json.loads(body))
                    self._json(200, {"ok": 1})
                    return
                if path == "/cockpit/desk/send":
                    if not self._gate():
                        return
                    payload = json.loads(body)
                    with stub.lock:
                        stub.sends.append({"t": time.monotonic(), **payload})
                        step = stub.script.pop(0) if stub.script else accepted(ref=f"inbox-{len(stub.sends)}")
                    if step.get("delay"):
                        time.sleep(step["delay"])
                    if step.get("drop"):
                        self.close_connection = True
                        try:
                            self.request.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                    self._json(step.get("status", 200), step.get("json"))
                    return
                self._proxy(body)

            def _proxy(self, body: bytes) -> None:
                skip = {"host", "origin", "referer", "connection", "content-length", "accept-encoding"}
                headers = {k: v for k, v in self.headers.items() if k.lower() not in skip}
                req = urllib.request.Request(stub.upstream + self.path, data=body or None, headers=headers, method=self.command)
                try:
                    resp = urllib.request.urlopen(req, timeout=30)  # noqa: S310 - loopback
                except urllib.error.HTTPError as e:
                    resp = e
                self.send_response(resp.status if hasattr(resp, "status") else resp.code)
                for k, v in resp.headers.items():
                    if k.lower() not in {"transfer-encoding", "connection", "content-length"}:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, ValueError):
                    pass

        return H


@contextmanager
def desk_page(tmp_path: Path):
    """The real cockpit page behind the stub. The stub's base is the page's origin."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (upstream, _h):
        stub = DeskStub(upstream)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
        httpd.daemon_threads = True
        stub.httpd = httpd
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield stub, f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)


# --- the drive's pieces -------------------------------------------------------------------------------------------------------

_TOKENED = [
    {"until": "document.querySelector('[data-mode-tab=board]')"},
    {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
    {"until": "!window.__pre && document.querySelector('.dv-zone') && document.querySelector('[data-phone-jump=desk]')"},
]
_TOKENLESS = [
    {"until": "document.querySelector('[data-mode-tab=board]') && document.querySelector('.dv-zone') && document.querySelector('[data-phone-jump=desk]')"},
]
_TAP_DESK = {"tap": "document.querySelector('[data-phone-jump=desk]')"}
_OPEN = {"until": "document.body.classList.contains('dv-on') && document.querySelector('.dv-zone .rv-input')"}
# a page re-shown reads at once: the way a test makes the view read without waiting its interval
_NUDGE = "document.dispatchEvent(new Event('visibilitychange')); 1"


def put(**state) -> dict:
    return {"eval": f"fetch('/__stub/state', {{method: 'POST', body: {json.dumps(json.dumps(state))}}}).then(() => 1)"}


def until(cond: str, ms: int = 15000) -> dict:
    return {"do": _NUDGE, "until": cond, "ms": ms, "every": 300}


def typed(text: str) -> dict:
    return {"eval": f"(() => {{ const t = document.querySelector('.dv-zone .rv-input'); t.value = {json.dumps(text)}; t.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()"}


_SNAP = """(() => { const z = document.querySelector('.dv-zone'), q = s => z.querySelector(s), all = s => [...z.querySelectorAll(s)];
  const bar = q('.dv-state'), send = q('.rv-send'), ta = q('.rv-input');
  return JSON.stringify({ text: z.innerText, bar: bar ? bar.innerText : null, barCls: bar ? bar.className : null, drop: !!q('.dv-droplink'),
    lines: all('.rv-line').map(l => ({cls: l.className, text: l.innerText})), sendDisabled: send.disabled, taDisabled: ta.disabled, taValue: ta.value,
    sendNote: q('.rv-sendnote').innerText, imgs: all('img,script,b,i,a').length, pwn: window.__pwn === undefined ? null : window.__pwn,
    on: document.body.classList.contains('dv-on'), room: document.body.classList.contains('rv-on')}); })()"""
SNAP = {"eval": _SNAP}
_BAN = r"\b(fenced|read|acted on)\b"


def snaps(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"sendDisabled"' in v]


def _no_ban_words(s: dict) -> None:
    import re
    assert not re.search(_BAN, s["text"], re.I), s["text"]


# --- static pins (no browser) -------------------------------------------------------------------------------------------------

def test_the_pure_functions_hold():
    if NODE is None:
        pytest.skip("node is needed")
    out = subprocess.run([NODE, str(HERE / "desk_pane_check.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "ALL PASS" in out.stdout, out.stdout + out.stderr


def test_the_view_never_makes_markup_from_a_word_it_was_given():
    src = JS.read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment", "DOMParser", "eval("):
        assert banned not in src, banned


def test_the_display_marks_are_the_cockpits_own_expression():
    ours = JS.read_text()
    theirs = (WEB / "cockpit_view.js").read_text()
    # cockpit_view.js writes the braille blank as the character itself, this file as its escape: the same code point
    pick = lambda s, name: s[s.index(name):].split("\n", 1)[0].split("= ", 1)[1].rstrip(";").replace("⠀", "\\u2800")   # noqa: E731
    assert pick(ours, "var HOSTILE") == pick(theirs, "const HOSTILE")


def test_the_draft_box_is_prose_with_a_dash_free_name():
    src = JS.read_text()
    block = src[src.index("function prose("):][:400]
    for k, v in {"autocapitalize": "sentences", "autocorrect": "on", "spellcheck": "true", "inputmode": "text", "autocomplete": "off"}.items():
        assert f'["{k}", "{v}"]' in block, k
    assert 'prose(ta, "deskdraft")' in src


def test_the_page_carries_the_desk_tab_and_its_script_after_the_rooms():
    html = (WEB / "dashboard.html").read_text()
    assert html.count('data-phone-jump="desk"') == 1 and html.count('<script src="/desk_tab_view.js">') == 1
    assert html.index("/room_view.js") < html.index("/desk_tab_view.js") < html.index("/board_view.js")
    boot = (WEB / "dashboard_boot.js").read_text()
    assert 'el("section", "dv-zone")' in boot and "dv-on" in boot


def test_the_server_serves_the_view_script():
    """The page loads /desk_tab_view.js, and only web_server.py's asset table can serve it (the shell's digest check requires
    every script the page names to be in that table)."""
    assert web_server._ASSETS[ASSET][0] == "desk_tab_view.js"


# --- the states ---------------------------------------------------------------------------------------------------------------

@needs_browser
def test_every_state_of_the_wire_is_drawn_from_the_read_alone(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        # an older page remembered ONE thread: it becomes a one-entry list (the events below arrive for it)
        steps = [PHONE, *_TOKENED, {"eval": "sessionStorage.setItem('levain.desk.thread', JSON.stringify('t1')); 1"},
                 put(get={"status": 200, "json": {"wired": False}}), _TAP_DESK, _OPEN,
                 until("document.querySelector('.dv-state-unwired')"), SNAP,                                     # 1 no desk wired
                 put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held')"), SNAP,   # 2 lease held
                 put(get={"status": 200, "json": wired(lease={"state": "none"})}), until("document.querySelector('.dv-lease-none')"), SNAP,   # 3 none
                 put(get={"status": 200, "json": wired(lease={"state": "unknown"})}), until("document.querySelector('.dv-lease-unknown')"), SNAP,  # 4 unknown
                 put(get={"status": 200, "json": wired(thread_id="t1", cursor="c3", events=[
                     {"by": "script", "kind": "took", "text": "", "at": "09:01:02"},
                     {"by": "script", "kind": "held", "text": "", "at": "09:02:00"},
                     {"by": "desk", "kind": "reply", "text": "on it, back by noon", "at": "09:03:00"}])}),
                 until("document.querySelectorAll('.dv-zone .rv-line').length >= 3"), SNAP,                      # 5 took, held, reply
                 put(get={"status": 200, "json": wired(thread_id="t1", cursor="c3", events=[], readable=False)}),
                 until("document.querySelector('.dv-unreadable')"), SNAP,                                          # 6 could not be fetched, held events kept
                 put(get={"status": 500, "json": {"error": "boom"}}), until("document.querySelector('.dv-state-error')"), SNAP,   # 7 server error
                 {"eval": "JSON.stringify({kept: sessionStorage.getItem('levain.desk.threads'), legacy: sessionStorage.getItem('levain.desk.thread')})"}]
        out = _drive(tmp_path, base + "/", steps)
    kept = json.loads(out["values"][-1])
    assert kept["legacy"] is None, kept                                           # the single remembered thread is gone
    assert json.loads(kept["kept"])[0]["id"] == "t1" and json.loads(kept["kept"])[0]["cursor"] == "c3", kept
    unwired, held, none, unknown, replies, unreadable, error = snaps(out)
    for s in (unwired, held, none, unknown, replies, unreadable, error):
        _no_ban_words(s)
        assert s["on"] and not s["room"], s
    assert unwired["bar"] == "This cockpit has no desk wired." and unwired["sendDisabled"] and unwired["taDisabled"], unwired
    assert held["bar"] == "desk 25, renewed 3 min ago" and not held["drop"], held
    assert held["sendDisabled"] is False and not held["taDisabled"] and held["barCls"].endswith("dv-lease-held"), held
    assert none["drop"] and "held for the next desk" in none["bar"] and "never starts a desk" in none["bar"] and not none["taDisabled"], none
    assert "will queue" in unknown["bar"] and not unknown["drop"] and not unknown["taDisabled"], unknown
    texts = [l["text"] for l in replies["lines"]]
    assert texts[0] == "Desk took it at 09:01:02 (script)" and texts[1] == "No desk holds the lease; held (09:02:00)", texts
    assert replies["lines"][2]["text"].startswith("Desk · 09:03:00") and "on it, back by noon" in replies["lines"][2]["text"], texts
    assert "rv-room" in replies["lines"][2]["cls"], replies["lines"]
    assert "could not be fetched" in unreadable["text"] and "on it, back by noon" in unreadable["text"], unreadable["text"]   # never "no events yet"
    assert "could not be reached" in error["bar"] and "500" in error["bar"], error
    # the read names its thread and cursor once it has them, and never before
    reads = [g["path"] for g in stub.gets]
    assert reads[0] == "/cockpit/desk.json?thread=t1", reads                       # the migrated thread, no cursor yet
    assert "/cockpit/desk.json?thread=t1&after=c3" in reads, reads                 # then the cursor the answer gave


@needs_browser
def test_the_lease_none_state_points_at_the_signed_drop_and_leaves_the_desk_view(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, put(get={"status": 200, "json": wired(lease={"state": "none"})}), _TAP_DESK, _OPEN,
                                           until("document.querySelector('.dv-droplink')"),
                                           {"tap": "document.querySelector('.dv-droplink')"},
                                           {"until": "!document.body.classList.contains('dv-on')"},
                                           {"eval": "JSON.stringify({dv: document.body.classList.contains('dv-on'), rv: document.body.classList.contains('rv-on'), zoneHidden: document.querySelector('.dv-zone').hidden, dropShown: !!document.querySelector('#ck-drop') && document.querySelector('#ck-drop').getClientRects().length > 0})"}])
    got = json.loads(out["values"][-1])
    assert got["dv"] is False and got["rv"] is False and got["zoneHidden"] is True, got
    assert stub.sends == []            # reaching for the Drop sent nothing


# --- the send -----------------------------------------------------------------------------------------------------------------

@needs_browser
def test_one_send_per_tap_and_one_outbox_id_per_message_kept_on_a_retry(tmp_path):
    """A double tap files one line; the line in flight greys and says Filing...; a retry after an unanswered try reuses the id; a
    new message mints a new one; the answers (filed, ambiguous, refused, over the cap) are the server's words."""
    click2 = "(() => { const b = document.querySelector('.dv-zone .rv-send'); b.click(); b.click(); return 1; })()"
    click1 = "document.querySelector('.dv-zone .rv-send').click(); 1"
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 # A: two clicks in one tick while the server holds the answer
                 put(script=[accepted("inbox-A", delay=0.7)]), typed("first line"),
                 {"eval": click2}, SNAP,
                 until("document.querySelector('.dv-filed')"), SNAP,
                 # B: two TRUSTED taps at the one point
                 put(script=[accepted("inbox-B", delay=0.7)]), typed("second line"),
                 {"tap": "document.querySelector('.dv-zone .rv-send')", "times": 2},
                 until("document.querySelectorAll('.dv-filed').length === 2"),
                 # C: a 502 then a dropped connection, retried under the same id; then it files
                 put(script=[{"status": 502, "json": None}, {"drop": True}, accepted("inbox-C")]), typed("third line"),
                 {"eval": click1}, until("document.querySelector('.dv-failed')"), SNAP,
                 {"eval": click1}, until("[...document.querySelectorAll('.dv-failed .dv-status')].some(n => n.innerText.indexOf('could not be reached') >= 0)"),
                 {"eval": click1}, until("document.querySelectorAll('.dv-filed').length === 3"), SNAP,
                 # D: ambiguous: the Room's mark-flow words, the line is not sent again, the draft goes (the line is in the log)
                 put(script=[{"status": 200, "json": {"outcome": "ambiguous", "ref": None, "thread_id": None, "reason": None, "replayed": False}}]), typed("fourth line"),
                 {"eval": click1}, until("document.querySelector('.dv-ambiguous')"), SNAP,
                 # D2: the same words again are the same message: the same id (the server answers the line it holds), one line in the log
                 put(script=[{"status": 200, "json": {"outcome": "ambiguous", "ref": None, "thread_id": None, "reason": None, "replayed": True}}]), typed("fourth line"),
                 {"eval": click1}, {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"}, SNAP,
                 # E: refused by the host: its one line, the draft stays, the id is spent
                 put(script=[{"status": 200, "json": {"outcome": "not_delivered", "ref": None, "thread_id": None, "reason": "the desk inbox is closed tonight", "replayed": False}}, accepted("inbox-E")]), typed("fifth line"),
                 {"eval": click1}, until("document.querySelector('.dv-refused')"), SNAP,
                 {"eval": click1}, until("document.querySelectorAll('.dv-filed').length === 4"),
                 # F: over the cap: refused, never cut, the draft stays
                 put(script=[{"status": 422, "json": {"error": "words_over_cap", "words_max": 2000}}]), typed("sixth line"),
                 {"eval": click1}, until("document.querySelectorAll('.dv-refused').length === 2"), SNAP]
        out = _drive(tmp_path, base + "/", steps)
    A_flight, A_filed, C_failed, C_filed, D, D2, E, F = (snaps(out)[i] for i in range(8))
    sends = stub.sends
    words = [s["words"] for s in sends]
    # A and B: ONE post each, however the button was hit
    assert words.count("first line") == 1 and words.count("second line") == 1, words
    last = A_flight["lines"][-1]
    assert "dv-sending" in last["cls"] and "Filing..." in last["text"], last
    assert A_flight["sendDisabled"] is True, A_flight
    assert "Filed: inbox-A" in A_filed["lines"][-1]["text"] and A_filed["taValue"] == "", A_filed
    # C: three tries, ONE id, the words unchanged; then a new message gets a new id
    c = [s for s in sends if s["words"] == "third line"]
    assert len(c) == 3 and len({s["outbox_id"] for s in c}) == 1, c
    assert "will not duplicate" in C_failed["lines"][-1]["text"] and C_failed["taValue"] == "third line", C_failed
    ids = {w: [s["outbox_id"] for s in sends if s["words"] == w] for w in set(words)}
    assert len({v[0] for v in ids.values()}) == len(ids), ids          # every message its own id
    assert all(8 <= len(v[0]) <= 128 for v in ids.values()), ids        # the wire's bound on an outbox id
    # D: ambiguous, exactly once, with the Room's wording
    assert HOLD_AMBIGUOUS in D["lines"][-1]["text"] and D["taValue"] == "", D
    d = [s for s in sends if s["words"] == "fourth line"]
    assert len(d) == 2 and d[0]["outbox_id"] == d[1]["outbox_id"], d
    assert [l["text"] for l in D2["lines"]].count(D["lines"][-1]["text"]) == 1 and D2["taValue"] == "", D2
    # E: refused: the host's line, the draft stays, a resend after a refusal is a NEW id
    e = [s for s in sends if s["words"] == "fifth line"]
    assert "the desk inbox is closed tonight" in E["lines"][-1]["text"] and E["taValue"] == "fifth line", E
    assert len(e) == 2 and e[0]["outbox_id"] != e[1]["outbox_id"], e
    # F: over the cap: the server's number, nothing cut
    assert words.count("sixth line") == 1 and "2000 characters" in F["lines"][-1]["text"] and "nothing was cut" in F["lines"][-1]["text"].lower() and F["taValue"] == "sixth line", F
    for s in snaps(out):
        _no_ban_words(s)


@needs_browser
def test_a_word_from_the_host_is_text_and_a_hidden_character_is_drawn_as_a_mark_while_typed_words_go_as_typed(tmp_path):
    hostile = "<img src=x onerror=\"window.__pwn=1\"><b id=pwned>x</b><script>window.__pwn=2</script>"
    mine = "a‮b <i>c</i>​d"
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, {"eval": "sessionStorage.setItem('levain.desk.thread', JSON.stringify('t9')); 1"},
                 put(get={"status": 200, "json": wired(lease={"state": "held", "holder": "desk " + hostile, "age_s": 5}, thread_id="t9", cursor="c1", events=[
                     {"by": "desk", "kind": "reply", "text": hostile + "‮gnp", "at": "12:00" + "​"}])},
                 script=[accepted("ref" + hostile + "​", thread="t9")]),
                 _TAP_DESK, _OPEN, until("document.querySelectorAll('.dv-zone .rv-line').length >= 1"), SNAP,
                 typed(mine), {"eval": "document.querySelector('.dv-zone .rv-send').click(); 1"}, until("document.querySelector('.dv-filed')"), SNAP]
        out = _drive(tmp_path, base + "/", steps)
    before, after = snaps(out)
    for s in (before, after):
        assert s["imgs"] == 0 and s["pwn"] is None, s                # no element was made from a word the host gave
        assert "<img src=x" in s["text"] and "<script>" in s["text"], s["text"]   # the words are there as text
        assert "<U+202E>" in s["text"] and "<U+200B>" in s["text"], s["text"]     # hidden characters are marked, display only
    assert "desk <img" in before["bar"], before
    sent = [x for x in stub.sends if x["words"] == mine]
    assert len(sent) == 1, stub.sends                                  # the words went exactly as typed (no mark in them)
    mine_line = [l for l in after["lines"] if "dv-you" in l["cls"]][0]["text"]
    assert "a<U+202E>b <i>c</i><U+200B>d" in mine_line, mine_line      # and are shown marked, as text


# --- the view mechanism -------------------------------------------------------------------------------------------------------

@needs_browser
def test_the_desk_and_the_room_replace_each_other_and_the_desk_needs_a_token(tmp_path):
    state = ("JSON.stringify({dv: document.body.classList.contains('dv-on'), rv: document.body.classList.contains('rv-on'), "
             "deskCur: document.querySelector('[data-phone-jump=desk]').getAttribute('aria-current'), roomCur: document.querySelector('[data-phone-jump=room]').getAttribute('aria-current'), "
             "deskShown: document.querySelector('.dv-zone').getClientRects().length > 0, roomShown: document.querySelector('.rv-zone').getClientRects().length > 0, "
             "mounted: !!document.querySelector('.dv-zone .rv-input')})")
    click = lambda which: {"eval": f"document.querySelector('[data-phone-jump={which}]').click(); new Promise(r => setTimeout(r, 400)).then(() => 1)"}  # noqa: E731
    with desk_page(tmp_path) as (stub, base):
        no_token = _drive(tmp_path, base + "/", [PHONE, *_TOKENLESS, click("desk"), {"eval": state}])
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}), click("desk"), {"eval": state}, click("room"), {"eval": state},
                 click("desk"), {"eval": state}, {"eval": "document.querySelector('[data-mode-tab=board]').click(); new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": state}]
        out = _drive(tmp_path, base + "/", steps)
    nt = json.loads(no_token["values"][-1])
    assert nt["dv"] is False and nt["mounted"] is False and nt["deskShown"] is False, nt     # no token: the view never mounts
    vals = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"deskCur"' in v]
    d1, r1, d2, back = vals
    assert d1["dv"] and not d1["rv"] and d1["deskCur"] == "page" and d1["roomCur"] is None and d1["deskShown"] and d1["mounted"], d1
    assert r1["rv"] and not r1["dv"] and r1["roomCur"] == "page" and r1["deskCur"] is None and not r1["deskShown"] and r1["roomShown"], r1
    assert d2["dv"] and not d2["rv"] and d2["deskShown"] and not d2["roomShown"], d2
    assert not back["dv"] and not back["rv"] and not back["deskShown"], back                # a mode tab leaves it


@needs_browser
def test_the_view_reads_every_four_seconds_open_and_every_twelve_hidden(tmp_path):
    install = ("(() => { const o = window.setTimeout; window.__delays = []; window.setTimeout = (f, ms, ...a) => { window.__delays.push(ms); return o(f, ms, ...a); }; return 1; })()")
    hide = ("(() => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'}); document.dispatchEvent(new Event('visibilitychange')); return 1; })()")
    show = ("(() => { delete document.visibilityState; document.dispatchEvent(new Event('visibilitychange')); return new Promise(r => setTimeout(r, 300)).then(() => JSON.stringify(window.__delays)); })()")
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                                           {"eval": install}, {"eval": hide}, {"eval": show}, {"eval": "new Promise(r => setTimeout(r, 9500)).then(() => 1)"}])
        gets = [g["t"] for g in stub.gets]
    delays = json.loads([v for v in out["values"] if isinstance(v, str) and v.startswith("[")][0])
    assert 12000 in delays and 4000 in delays, delays                 # hidden: 12 s; shown again: 4 s
    steady = [b - a for a, b in zip(gets, gets[1:]) if 3.0 < b - a < 6.5]
    assert len(steady) >= 2 and all(3.5 <= d <= 5.5 for d in steady), (steady, [round(g - gets[0], 1) for g in gets])


# --- the phone ----------------------------------------------------------------------------------------------------------------

@needs_browser
def test_the_phone_shows_the_desk_tab_whole_and_the_composer_pinned_with_no_sideways_scroll(tmp_path):
    measure = """(() => { const q = s => document.querySelector(s), r = n => n.getBoundingClientRect(), tabs = [...q('#ck-phonetabs').querySelectorAll('button')];
      const desk = q('[data-phone-jump=desk]');
      return JSON.stringify({vw: innerWidth, vh: innerHeight, scrollW: document.documentElement.scrollWidth, scrollH: document.documentElement.scrollHeight,
        tabs: tabs.map(b => b.dataset.phoneJump), deskWhole: desk.scrollWidth <= desk.clientWidth, deskH: Math.round(r(desk).height),
        dockBottom: Math.round(r(q('.dv-zone .rv-dock')).bottom), sendW: Math.round(r(q('.dv-zone .rv-send')).width), sendH: Math.round(r(q('.dv-zone .rv-send')).height),
        font: parseFloat(getComputedStyle(q('.dv-zone .rv-input')).fontSize), stateW: Math.round(r(q('.dv-state')).width), stateScroll: q('.dv-state').scrollWidth,
        mainShown: q('main.frame').getClientRects().length > 0, bodyOverflow: getComputedStyle(document.body).overflow}); })()"""
    long_lease = "a" * 90
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, put(get={"status": 200, "json": wired(lease={"state": "held", "holder": long_lease, "age_s": 99999})}), _TAP_DESK, _OPEN,
                                           until("document.querySelector('.dv-lease-held')"), {"eval": measure}])
    g = json.loads(out["values"][-1])
    assert g["scrollW"] <= g["vw"] and g["scrollH"] <= g["vh"] + 1 and g["bodyOverflow"] == "hidden" and not g["mainShown"], g
    assert g["tabs"][-2:] == ["room", "desk"] and g["deskWhole"] and g["deskH"] >= 44, g
    assert abs(g["dockBottom"] - g["vh"]) <= 1 and g["sendW"] >= 44 and g["sendH"] >= 44 and g["font"] >= 16, g
    assert g["stateScroll"] <= g["stateW"] + 1, g                      # a long holder wraps, it does not push the page


# --- the thread model (fix round 1): the host files EVERY typed line under a NEW thread ------------------------------------------

CLICK_SEND = {"eval": "document.querySelector('.dv-zone .rv-send').click(); 1"}


def _ev(kind, text="", by="desk", at="09:00:00"):
    return {"by": by, "kind": kind, "text": text, "at": at}


def _fixture(tid, cursor, *events, **kw):
    return {"status": 200, "json": wired(thread_id=tid, cursor=cursor, events=list(events)), **kw}


def _heads(s):
    return [l["text"].split("\n")[0] for l in s["lines"]]


def _send(text, n):
    """One line typed, sent, and waited for until `n` lines are filed."""
    return [typed(text), CLICK_SEND, until(f"document.querySelectorAll('.dv-zone .dv-filed').length === {n}")]


@needs_browser
def test_every_line_keeps_the_answers_of_its_own_thread_and_the_newest_three_are_read_and_six_are_kept(tmp_path):
    """DESK-E M1 and codex HIGH 1: line one's took and reply stay beside it after line two (a new thread), a late reply to line
    one is still fetched, a tick reads the newest three threads (one request each), and past six the oldest is dropped."""
    took = lambda n: _ev("took", by="script", at=f"09:00:0{n}")   # noqa: E731
    by_thread = {f"t{n}": _fixture(f"t{n}", "c1", took(n), _ev("reply", f"reply {n}")) for n in range(1, 8)}
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}, by_thread=by_thread,
                                       script=[accepted(f"inbox-{n}", thread=f"t{n}") for n in range(1, 8)]),
                 _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 *_send("line 1", 1), until("document.body.innerText.indexOf('reply 1') >= 0"),
                 *_send("line 2", 2), until("document.body.innerText.indexOf('reply 2') >= 0"), SNAP,       # 0: two lines, two threads
                 # the desk answers line one AGAIN (after the cursor c1): it must still be fetched and drawn beside line one
                 put(by_thread={**by_thread, "t1@c1": _fixture("t1", "c2", _ev("reply", "late 1")), "t1@c2": _fixture("t1", "c2")}),   # nothing after c2
                 until("document.body.innerText.indexOf('late 1') >= 0"), SNAP,                               # 1
                 *[s for n in range(3, 8) for s in _send(f"line {n}", n)],
                 until("document.body.innerText.indexOf('reply 7') >= 0"), SNAP,                              # 2: seven lines sent
                 {"eval": "JSON.stringify({threads: sessionStorage.getItem('levain.desk.threads')})"}]
        out = _drive(tmp_path, base + "/", steps)
        gets = list(stub.gets)
    two, late, seven = snaps(out)[:3]
    assert _heads(two) == ["line 1", "Desk took it at 09:00:01 (script)", "Desk · 09:00:00", "line 2", "Desk took it at 09:00:02 (script)", "Desk · 09:00:00"], two["lines"]
    assert [l["text"].split("\n")[1] for l in two["lines"] if "rv-room" in l["cls"]] == ["reply 1", "reply 2"], two["lines"]
    heads = _heads(late)
    assert heads[:4] == ["line 1", "Desk took it at 09:00:01 (script)", "Desk · 09:00:00", "Desk · 09:00:00"] and heads[4] == "line 2", late["lines"]
    assert late["lines"][3]["text"].endswith("late 1"), late["lines"]
    # seven lines: every line stays drawn, only the last six threads keep (and draw) their answers
    texts = [l["text"] for l in seven["lines"]]
    assert [h for h in _heads(seven) if h.startswith("line ")] == [f"line {n}" for n in range(1, 8)], seven["lines"]
    assert not any("reply 1" in t for t in texts) and all(any(f"reply {n}" in t for t in texts) for n in range(2, 8)), texts
    stored = json.loads(json.loads(out["values"][-1])["threads"])
    assert [t["id"] for t in stored] == [f"t{n}" for n in range(2, 8)], stored
    # a tick reads the newest three, one request each: after the first read of t7, t4 and older are never asked again
    asked = [g["thread"] for g in gets if g["thread"]]
    after7 = asked[asked.index("t7"):]
    assert set(after7) == {"t7", "t6", "t5"}, after7
    assert "t1" in asked and any(g["path"] == "/cockpit/desk.json?thread=t1&after=c1" for g in gets), asked


@needs_browser
def test_a_slow_read_of_one_thread_never_switches_or_loses_another(tmp_path):
    """codex HIGH 1 and 2, complement MED 1: a read of thread one is in flight when line two is answered with thread two. Its answer
    lands on thread one alone: thread two is still read and its reply is drawn."""
    by_thread = {"t1": _fixture("t1", "c1", _ev("took", by="script"), _ev("reply", "reply one"), delay=2.5),
                 "t2": _fixture("t2", "c1", _ev("took", by="script"), _ev("reply", "reply two"))}
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}, by_thread=by_thread,
                                       script=[accepted("inbox-1", thread="t1"), accepted("inbox-2", thread="t2")]),
                 _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 *_send("line one", 1),                           # the follow-up read of t1 is now held for 2.5 s
                 *_send("line two", 2),                           # ... and line two is answered meanwhile
                 until("document.body.innerText.indexOf('reply two') >= 0 && document.body.innerText.indexOf('reply one') >= 0", 25000), SNAP]
        out = _drive(tmp_path, base + "/", steps)
        asked = [g["thread"] for g in stub.gets if g["thread"]]
    (s,) = snaps(out)
    assert _heads(s) == ["line one", "Desk took it at 09:00:00 (script)", "Desk · 09:00:00", "line two", "Desk took it at 09:00:00 (script)", "Desk · 09:00:00"], s["lines"]
    assert "t2" in asked, asked


@needs_browser
def test_a_send_answered_while_a_read_is_in_flight_is_read_at_once_not_a_tick_later(tmp_path):
    """complement LOW 4: the send's follow-up read is queued behind the one in flight, not dropped until the next 4 s tick."""
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}, by_thread={"t1": _fixture("t1", "c1", _ev("reply", "reply one"))},
                                       script=[accepted("inbox-1", thread="t1")]),
                 _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 put(get={"status": 200, "json": wired(), "delay": 2.0}),
                 {"eval": _NUDGE},                                # a read of the desk, held for 2 s
                 typed("line one"), CLICK_SEND,                   # answered while it is in flight
                 {"until": "document.body.innerText.indexOf('reply one') >= 0", "ms": 15000, "every": 200}]
        _drive(tmp_path, base + "/", steps)
        sent_at = stub.sends[0]["t"]
        first = next(g["t"] for g in stub.gets if g["thread"] == "t1")
    assert first - sent_at < 3.0, first - sent_at                      # about 2 s (the read in flight); a lost follow-up is 4 s past that


@needs_browser
def test_a_headers_function_that_throws_clears_the_flags_and_the_send_and_the_poll_go_on(tmp_path):
    """deepseek HIGH 2: a synchronous throw from the transport's headers() was a throw before any promise: the send flag and the
    reading flag stayed set for good."""
    boom = "window.__h = window.LevainCockpitTransport.headers; window.LevainCockpitTransport.headers = () => { throw new Error('no storage'); };"
    unpatch = "window.LevainCockpitTransport.headers = window.__h; 1"
    send_now = ("const t = document.querySelector('.dv-zone .rv-input'); t.value = 'line one'; t.dispatchEvent(new Event('input', {bubbles: true})); "
                "document.querySelector('.dv-zone .rv-send').click(); 1")
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 # a read whose headers() throws before any request exists: it must not leave `reading` set
                 {"eval": boom + " document.dispatchEvent(new Event('visibilitychange')); 1"}, {"eval": unpatch},
                 put(get={"status": 200, "json": wired(lease={"state": "held", "holder": "desk 99", "age_s": 5})}),
                 until("document.querySelector('.dv-state').innerText.indexOf('desk 99') >= 0"),           # the poll went on
                 # a send whose headers() throws: the line is marked unreached and the button comes back
                 {"eval": boom + " " + send_now}, {"until": "document.querySelector('.dv-zone .dv-failed')", "ms": 8000, "every": 200}, {"eval": unpatch}, SNAP,
                 CLICK_SEND, until("document.querySelector('.dv-zone .dv-filed')"), SNAP]
        out = _drive(tmp_path, base + "/", steps)
    failed, filed = snaps(out)
    assert "could not be reached" in failed["lines"][0]["text"] and failed["sendDisabled"] is False, failed
    assert len(stub.sends) == 1 and stub.sends[0]["words"] == "line one", stub.sends     # the failed try never reached the server
    assert "dv-filed" in filed["lines"][0]["cls"], filed


@needs_browser
def test_a_draw_that_throws_after_the_servers_200_never_marks_the_line_unreached(tmp_path):
    """complement LOW 3: the failure path belongs to the POST. The server filed the line; a draw that throws afterwards is not
    "The server could not be reached"."""
    install = ("(() => { const of = window.fetch; window.fetch = (u, o) => of(u, o).then(r => { if (String(u).indexOf('/desk/send') >= 0) window.__arm = 1; return r; });"
               " const oc = document.createElement.bind(document);"
               " document.createElement = (...a) => { if (window.__arm) { window.__arm = 0; window.__threw = 1; throw new Error('draw failed'); } return oc(...a); }; return 1; })()")
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, {"eval": install}, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 typed("line one"), CLICK_SEND, until("window.__threw"), until("document.querySelector('.dv-zone .dv-filed')"), SNAP]
        out = _drive(tmp_path, base + "/", steps)
    (s,) = snaps(out)
    assert len(stub.sends) == 1, stub.sends
    assert "dv-filed" in s["lines"][0]["cls"] and "could not be reached" not in s["text"] and "dv-failed" not in s["lines"][0]["cls"], s


@needs_browser
def test_the_bubble_draws_the_hosts_ref_once_as_text(tmp_path):
    """DESK-E M3: the host's first stdout line is the ref and often says Filed itself."""
    refs = ["Filed for the desk. (thread th-1)", "Filed; no desk is open, so it waits. <b>x</b>", "inbox-9"]
    with desk_page(tmp_path) as (stub, base):
        steps = [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}, script=[accepted(r, thread=f"t{i}") for i, r in enumerate(refs)]),
                 _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
                 *_send("a", 1), *_send("b", 2), *_send("c", 3), SNAP]
        out = _drive(tmp_path, base + "/", steps)
    (s,) = snaps(out)
    notes = [l["text"].split("\n")[1] for l in s["lines"] if "dv-you" in l["cls"]]
    assert notes == ["Filed for the desk. (thread th-1)", "Filed; no desk is open, so it waits. <b>x</b>", "Filed: inbox-9"], notes
    assert s["imgs"] == 0, s


@needs_browser
def test_a_desk_to_room_switch_keeps_the_rooms_keyboard_fit(tmp_path):
    """All three lineages: the Desk's stop() cleared --rv-vvh/--rv-vvt that the Room had JUST set (sync mounted the Room before it
    stopped the Desk). Every stop now runs before any mount."""
    read = ("new Promise(r => setTimeout(r, 400)).then(() => JSON.stringify({vvh: document.documentElement.style.getPropertyValue('--rv-vvh'), "
            "vvt: document.documentElement.style.getPropertyValue('--rv-vvt'), rv: document.body.classList.contains('rv-on'), dv: document.body.classList.contains('dv-on'), "
            "roomInput: !!document.querySelector('.rv-zone .rv-input'), deskInput: !!document.querySelector('.dv-zone .rv-input')}))")
    click = lambda which: {"eval": f"document.querySelector('[data-phone-jump={which}]').click(); {read}"}   # noqa: E731
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [PHONE, *_TOKENED, put(get={"status": 200, "json": wired()}), click("desk"), until("document.querySelector('.dv-lease-held')"),
                                           click("room"), click("desk")])
    desk1, room, desk2 = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"vvh"' in v]
    assert desk1["dv"] and desk1["deskInput"] and desk1["vvh"].endswith("px") and desk1["vvt"].endswith("px"), desk1
    assert room["rv"] and not room["dv"] and room["roomInput"], room
    assert room["vvh"].endswith("px") and room["vvt"].endswith("px"), room             # the Room's own, set by the Room, not cleared by the Desk
    assert desk2["dv"] and desk2["vvh"].endswith("px") and desk2["vvt"].endswith("px"), desk2


def test_the_desk_lets_go_of_what_it_listened_to_on_stop():
    src = JS.read_text()
    stop = src[src.index("stop: function ()"):][:900]
    for listener in ('removeEventListener("visibilitychange", onVisible)', 'body.removeEventListener("scroll", onBodyScroll)', 'vv.removeEventListener("resize", fitViewport)'):
        assert listener in stop, listener
