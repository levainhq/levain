"""The Desk view's three 1114 rows (levain#22 layout, #23 voice, #24 clear), driven on the real cockpit page behind the desk stub of
tests/test_desk_pane_page.py."""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_desk_pane_page import (
    _OPEN, _TAP_DESK, _TOKENED, CLICK_SEND, DeskStub, accepted, desk_page, put, typed, until, wired,
)

# a phone with its keyboard up: the page is a short box, so the stage above the composer shrinks to (almost) nothing
KEYBOARD_UP = {"emulate": {"w": 430, "h": 430, "dpr": 2, "mobile": True}}
LONG = ("question: do you think me sending a message to you via the desk connector here in the cockpit uses more tokens than "
        "speaking directly in the terminal?")

# Every line the conversation box holds, against the dock: the point of the line that lies inside the dock's box must belong to the
# dock (a clipped line is not hit there), never to the line drawn behind it.
_HIDDEN_BEHIND_DOCK = """(() => { const z = document.querySelector('.dv-zone'), dock = z.querySelector('.rv-dock'), d = dock.getBoundingClientRect(),
  stage = z.querySelector('.rv-stage'), s = stage.getBoundingClientRect(), body = z.querySelector('.rv-body');
  const lines = [...z.querySelectorAll('.rv-stream .rv-note, .rv-stream .rv-line')];
  let over = [];
  lines.forEach(n => { const r = n.getBoundingClientRect(); if (!r.height) return;
    const top = Math.max(r.top, d.top + 1), bot = Math.min(r.bottom, d.bottom - 1); if (bot <= top) return;
    const hit = document.elementFromPoint(r.left + r.width / 2, (top + bot) / 2);
    if (hit && !dock.contains(hit)) over.push(n.className + ' hit ' + hit.className); });
  return JSON.stringify({ over, stageOverflow: getComputedStyle(stage).overflow, stageH: Math.round(s.height), bodyH: Math.round(body.getBoundingClientRect().height),
    lines: lines.length }); })()"""


def _look(out) -> dict:
    return json.loads([v for v in out["values"] if isinstance(v, str) and '"stageOverflow"' in v][-1])


@needs_browser
def test_the_empty_state_and_a_sent_line_never_draw_behind_the_composer_with_the_keyboard_up(tmp_path):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [KEYBOARD_UP, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN,
                                           until("document.querySelector('.dv-lease-held')"),
                                           typed(LONG), {"eval": _HIDDEN_BEHIND_DOCK},          # zero messages: the empty-state line
                                           CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
                                           typed(LONG), {"eval": _HIDDEN_BEHIND_DOCK}])         # after the first send: the filed line
    empty, sent = [json.loads(v) for v in out["values"] if isinstance(v, str) and '"stageOverflow"' in v]
    assert empty["lines"] == 1 and sent["lines"] >= 1, (empty, sent)
    for g in (empty, sent):
        # the stage is shorter than the box inside it (the case the page failed on), and what overflows is clipped, not drawn over the dock
        assert g["bodyH"] > g["stageH"], g
        assert g["stageOverflow"] == "hidden", g
        assert g["over"] == [], g


# --- levain#23: the Desk's voice is the Room's, from one implementation ------------------------------------------------------------

WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"

# a recognizer that stays on the device, a speaker with a local voice, a media element that plays nothing: the test says what was heard
# and reads what was spoken
VOICE_INIT = """(() => {
  window.__spoken = []; window.__started = 0; window.__rec = null; window.__stopped = 0;
  window.SpeechRecognition = function () { window.__rec = this; this.processLocally = false; };
  window.SpeechRecognition.prototype = { processLocally: false, start() { window.__started++; }, stop() { window.__stopped++; } };
  HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
  HTMLMediaElement.prototype.pause = function () {};
  window.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; this.voice = null; this.rate = 1; } };
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
    speak(u) { if (u.text) { window.__spoken.push(u.text); window.__utt = u; } }, cancel() {},
    getVoices() { return [{ name: 'local-en', lang: 'en-US', localService: true }]; } } });
})();"""
VOICE_ON = {"eval": "LevainVoice.saveSettings({tier: 'device', autoplay: true}) && 0"}
DICTATE = "document.querySelector('.dv-zone .rv-mic:not(.rv-mic-stream)')"


def test_the_desk_and_the_room_hold_no_copy_of_the_voice_code():
    desk, room = (WEB / "desk_tab_view.js").read_text(), (WEB / "room_view.js").read_text()
    mic, voice = (WEB / "mic.js").read_text(), (WEB / "voice.js").read_text()
    for name, js in (("desk_tab_view.js", desk), ("room_view.js", room)):
        # both views call the one dock and the one tracker; neither names a recognizer, installs a pack or arms the player itself
        assert "LevainMic.dock(" in js and "LevainVoice.tracker()" in js, name
        for held in ("SpeechRecognition", "processLocally", "Speech.install(", "LM.start(", "V.arm(", "V.replyFinished(", "V.button(", "LevainVoiceIcons"):
            assert held not in js, (name, held)
    assert mic.count("function dock(") == 1 and mic.count("Speech.install(") == 1 and mic.count("LM.start()") == 1
    assert voice.count("function tracker(") == 1 and voice.count("arm(base)") == 1
    # levain#80: a desk reply is read by the one player on the one setting; the server re-reads the desk's event, there is no device-only path
    assert "desk: {" in desk and "reply.desk" in voice and "deviceOnly" not in desk + voice


def test_the_desk_mount_never_asks_the_recognizer_whether_it_is_available():
    """SpeechRecognition.available() froze the headless renderer (RM-G2, measured 2026-11-04); the Room's mount is pinned the same way.
    Red with: `probe: false` removed from the Desk's mount call in dashboard_boot.js."""
    import re
    boot = (WEB / "dashboard_boot.js").read_text()
    assert re.search(r"LevainDeskView\.mount\([^\n]*probe: false\s*\}\)", boot)


@needs_browser
def test_both_views_call_the_same_dock_and_the_same_tracker(tmp_path):
    wrap = """(() => { window.__calls = []; const d = LevainMic.dock, t = LevainVoice.tracker;
      LevainMic.dock = function (cfg) { window.__calls.push('dock ' + cfg.draftKey); return d.apply(this, arguments); };
      LevainVoice.tracker = function () { window.__calls.push('tracker'); return t.apply(this, arguments); }; return 1; })()"""
    click = lambda which: {"eval": f"document.querySelector('[data-phone-jump={which}]').click(); new Promise(r => setTimeout(r, 500)).then(() => 1)"}  # noqa: E731
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"init": VOICE_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), {"eval": wrap},
                                           click("desk"), click("room"), click("desk"),
                                           {"eval": "JSON.stringify({calls: window.__calls, rooms: document.querySelectorAll('.rv-zone .rv-mic').length})"}])
    got = json.loads(out["values"][-1])
    docks = [c for c in got["calls"] if c.startswith("dock ")]
    assert docks == ["dock levain.desk.draft", "dock levain.room.draft", "dock levain.desk.draft"], got
    assert got["calls"].count("tracker") >= 2, got                      # the Desk's mount and the Room's mount each took a tracker from voice.js


@needs_browser
def test_the_desk_dictates_into_its_draft_and_reads_the_desks_reply_aloud(tmp_path):
    state = ("""JSON.stringify({ret: 'state', ta: document.querySelector('.dv-zone .rv-input').value, started: window.__started, stopped: window.__stopped,
      icon: (%s || {getAttribute: () => null}).getAttribute('data-voice-icon'), draft: sessionStorage.getItem('levain.desk.draft'),
      sendOff: document.querySelector('.dv-zone .rv-send').getAttribute('aria-disabled'), spoken: window.__spoken,
      btns: [...document.querySelectorAll('.dv-zone .rv-room .voice-btn')].length, mic: !!%s && %s.style.display !== 'none'})""" % (DICTATE, DICTATE, DICTATE))
    reply = {"by": "desk", "kind": "reply", "text": "on it, back by noon", "at": "09:03:00"}
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"init": VOICE_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, VOICE_ON,
                                           until("document.querySelector('.dv-lease-held')"),
                                           {"eval": state},                                              # 0: Dictate is drawn in the Desk's composer
                                           {"eval": f"{DICTATE}.click(); 1"}, {"eval": state},             # 1: started
                                           {"eval": "window.__rec.onresult({results: [[{transcript: 'say hello to the desk'}]]}); 1"}, {"eval": state},   # 2: the words are in the draft
                                           {"eval": f"{DICTATE}.click(); 1"}, {"eval": state},             # 3: stopped
                                           CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
                                           put(get={"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[reply])}),
                                           until("document.querySelectorAll('.dv-zone .rv-room .voice-btn').length === 1"),
                                           until("window.__spoken.length >= 1"), {"eval": state},          # 4: a reply that arrived after the first read is spoken
                                           {"eval": "window.__utt.onend(); window.__spoken.length = 0; 1"}, until("!document.querySelector('.dv-zone .rv-room .voice-btn').getAttribute('data-voice-icon').endsWith('stop')"),
                                           {"eval": "document.querySelector('.dv-zone .rv-room .voice-btn').click(); 1"},
                                           until("window.__spoken.length === 1"), {"eval": state}])        # 5: its speaker control reads it again
    s = [json.loads(v) for v in out["values"] if isinstance(v, str) and '"ret":"state"' in v]
    drawn, started, heard, stopped, spoke, replayed = s
    assert drawn["mic"] is True and drawn["started"] == 0, drawn
    assert started["started"] == 1 and started["icon"].endswith("stop"), started
    assert heard["ta"] == "say hello to the desk" and heard["draft"] == "say hello to the desk" and heard["sendOff"] is None, heard
    assert stopped["stopped"] >= 1 and not stopped["icon"].endswith("stop"), stopped
    assert spoke["spoken"] == ["on it, back by noon"] and spoke["btns"] == 1, spoke
    assert replayed["spoken"] == ["on it, back by noon"], replayed


# --- levain#24: Clear empties what is SETTLED and touches no stored row ------------------------------------------------------------------

class RecordingDeskStub(DeskStub):
    """The desk stub plus a STORE the host holds (one row per outbox id a send filed) and the log of EVERY request the page made (all
    but the stub's own /__stub/ calls). Only the desk read and the send are allowed: any other request (a DELETE, a PUT, a purge POST,
    a request to any other route) while a window is open empties the store and is logged. A test opens a window with
    POST /__stub/mark?name=<n>: the requests and the host's rows are snapshotted at each mark, so what happened between two marks is
    an exact set, and the rows a Clear leaves can be compared with the rows before it."""

    ALLOWED = {("GET", "/cockpit/desk.json"), ("POST", "/cockpit/desk/send")}

    def __init__(self, upstream: str) -> None:
        super().__init__(upstream)
        self.purged = False
        self.requests: list[tuple[str, str]] = []
        self.marks: dict[str, dict] = {}

    def rows(self) -> dict:
        """What the host holds now: the filed rows (outbox id to words) and the thread fixtures it answers reads from (gone once purged)."""
        with self.lock:
            if self.purged:
                return {"sends": {}, "threads": {}}
            return {"sends": {x["outbox_id"]: x["words"] for x in self.sends},
                    "threads": json.loads(json.dumps(self.by_thread, sort_keys=True))}

    @property
    def store(self) -> dict[str, str]:
        """The host's rows: one per outbox id a send filed (gone once anything purged them)."""
        return self.rows()["sends"]

    def between(self, a: str, b: str) -> list[tuple[str, str]]:
        """The exact requests made after mark `a` and before mark `b`."""
        return self.requests[self.marks[a]["n"]:self.marks[b]["n"]]

    def handler(self):
        base, stub = super().handler(), self

        class Recording(base):
            def _note(self, method: str) -> None:
                path = self.path.split("?", 1)[0]
                if path.startswith("/__stub/"):
                    return
                with stub.lock:
                    stub.requests.append((method, path))
                    if (method, path) not in stub.ALLOWED and (method not in ("GET", "POST") or path.startswith("/cockpit/desk")):
                        stub.purged = True                 # a purge: the host's rows are gone

            def _mark(self) -> bool:
                if self.path.split("?", 1)[0] != "/__stub/mark":
                    return False
                name = urllib.parse.parse_qs(self.path.split("?", 1)[1])["name"][0]
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                snap = stub.rows()
                with stub.lock:
                    stub.marks[name] = {"n": len(stub.requests), "rows": snap}
                self._json(200, {"ok": 1})
                return True

            def do_GET(self):
                self._note("GET")
                super().do_GET()

            def do_POST(self):
                if self._mark():
                    return
                self._note("POST")
                super().do_POST()

            def _other(self, method: str) -> None:
                self._note(method)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_DELETE = lambda self: self._other("DELETE")      # noqa: E731
            do_PUT = lambda self: self._other("PUT")            # noqa: E731
            do_PATCH = lambda self: self._other("PATCH")        # noqa: E731

        return Recording


from contextlib import contextmanager  # noqa: E402


@contextmanager
def recording_page(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from tests.test_cockpit_k2a import TOKEN, _install, _serve
    root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (upstream, _h):
        stub = RecordingDeskStub(upstream)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
        httpd.daemon_threads = True
        stub.httpd = httpd
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield stub, f"http://127.0.0.1:{httpd.server_address[1]}/"
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)


SHOWN = """JSON.stringify({ret: 'shown', lines: [...document.querySelectorAll('.dv-zone .rv-stream .rv-line')].map(n => n.innerText),
  marks: document.querySelectorAll('.dv-zone .dv-mark').length, note: document.querySelector('.dv-zone .rv-sendnote').innerText,
  sent: sessionStorage.getItem('levain.desk.sent'), threads: sessionStorage.getItem('levain.desk.threads'),
  clearOff: document.querySelector('.dv-zone .dv-clear').disabled, clearText: document.querySelector('.dv-zone .dv-clear').innerText,
  cancels: window.__cancels, draft: document.querySelector('.dv-zone .rv-input').value})"""
CLEAR = {"eval": "document.querySelector('.dv-zone .dv-clear').click(); 1"}


def MARK(name: str) -> dict:     # noqa: N802 - a step, written like the other step constants
    return {"eval": f"fetch('/__stub/mark?name={name}', {{method: 'POST', body: '{{}}'}}).then(() => 1)"}
COUNT_CANCELS = "(() => { window.__cancels = 0; const ss = window.speechSynthesis, c = ss.cancel; ss.cancel = function () { window.__cancels++; return c.apply(this, arguments); }; return 1; })()"
AMBIGUOUS = {"status": 200, "json": {"outcome": "ambiguous", "ref": None, "thread_id": None, "reason": None, "replayed": False}}


def _shown(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"ret":"shown"')]


RELOAD = [{"eval": "window.__pre = 1; location.reload(); 1"},
          {"until": "!window.__pre && document.querySelector('.dv-zone') && document.querySelector('[data-phone-jump=desk]')"}, _TAP_DESK, _OPEN]


@needs_browser
def test_clear_empties_the_settled_view_across_a_reload_and_touches_no_stored_row(tmp_path):
    """Red with: Clear making any request beyond the reads and the sends (the log shows it), purging the host's rows (the store's ids
    change), or a cleared view that comes back after a reload."""
    reply = {"by": "desk", "kind": "reply", "text": "on it, back by noon", "at": "09:03:00"}
    with recording_page(tmp_path) as (stub, url):
        out = _drive(tmp_path, url, [
            {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
            {"eval": SHOWN},                                                                    # 0: nothing yet, Clear is off
            typed("one line for the desk"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
            put(by_thread={"t1": {"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[reply])}}),
            until("document.querySelectorAll('.dv-zone .rv-line').length === 2"), {"eval": SHOWN},   # 1: the line and the reply
            MARK("before_clear"), CLEAR, until("document.querySelectorAll('.dv-zone .rv-line').length === 0"), MARK("after_clear"), {"eval": SHOWN},   # 2: cleared
            *RELOAD, until("document.querySelector('.dv-lease-held')"), {"eval": SHOWN},        # 3: after a reload it is still cleared
            put(get={"status": 200, "json": wired()}, script=[accepted(ref="inbox-2", thread="t2")]),
            typed("a line after the clear"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
            {"eval": SHOWN}])                                                                   # 4: only what came after
        rows_after = dict(stub.store)
        requests = list(stub.requests)
        sends = [x["words"] for x in stub.sends]
        marks = dict(stub.marks)
        during_clear = stub.between("before_clear", "after_clear")
    empty, full, cleared, reloaded, later = _shown(out)
    assert empty["clearOff"] is True and full["clearOff"] is False and len(full["lines"]) == 2, (empty, full)
    assert cleared["lines"] == [] and cleared["clearOff"] is True and json.loads(cleared["sent"]) == [] and json.loads(cleared["threads"]) == [], cleared
    assert reloaded["lines"] == [] and reloaded["clearText"] == "Clear", reloaded                      # a cleared view stays cleared (this tab's memory)
    assert len(later["lines"]) == 1 and "a line after the clear" in later["lines"][0] and "one line for the desk" not in later["lines"][0], later
    # the host's rows: both lines are still there, under the ids they were filed under, after the Clear and the reload
    assert sends == ["one line for the desk", "a line after the clear"], sends
    assert sorted(rows_after.values()) == sorted(sends) and len(rows_after) == 2, rows_after
    # the Clear itself: no request but desk reads (the poll may tick), and the host's rows and thread fixtures are exactly as they were
    assert desk_touches(during_clear) == [], during_clear
    assert marks["after_clear"]["rows"] == marks["before_clear"]["rows"] and len(marks["before_clear"]["rows"]["sends"]) == 1, marks
    assert stub_ok(requests), requests
    assert [r for r in requests if r[0] == "POST" and r not in AMBIENT] == [("POST", "/cockpit/desk/send")] * 2, requests


@needs_browser
def test_clear_never_removes_an_unconfirmed_line_or_one_still_waiting_for_its_answer_and_stops_a_reply_being_read(tmp_path):
    """Red with: Clear removing every line (the unconfirmed line and the only handle on the unanswered thread are gone: "did not arrive"
    can no longer give the words back, and the reply is never fetched), or Clear leaving the reply it is removing playing."""
    r1 = {"by": "desk", "kind": "reply", "text": "first answer", "at": "09:03:00"}
    r2 = {"by": "desk", "kind": "reply", "text": "answer to the waiting line", "at": "09:09:00"}
    with recording_page(tmp_path) as (stub, url):
        out = _drive(tmp_path, url, [
            {"init": VOICE_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, VOICE_ON, until("document.querySelector('.dv-lease-held')"),
            {"eval": COUNT_CANCELS},
            put(script=[accepted(ref="inbox-1", thread="t1"), accepted(ref="inbox-2", thread="t2"), AMBIGUOUS]),
            typed("answered line"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
            typed("waiting line"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 2"),
            typed("maybe line"), CLICK_SEND, until("document.querySelector('.dv-ambiguous')"),
            put(by_thread={"t1": {"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[r1])}}),
            until("window.__spoken.indexOf('first answer') >= 0"),                              # the answer is being read: its utterance has not ended
            {"eval": "window.__cancels = 0; 1"}, {"eval": SHOWN},                               # 0: three lines, one answered
            MARK("b1"), CLEAR, until("document.querySelectorAll('.dv-zone .rv-stream .rv-line').length === 2"), MARK("a1"), {"eval": SHOWN},   # 1: the answered line went; the other two stay
            put(by_thread={"t1": {"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[r1])},
                           "t2": {"status": 200, "json": wired(thread_id="t2", cursor="c3", events=[r2])}}),
            until("document.body.innerText.indexOf('answer to the waiting line') >= 0"),         # the waiting thread was still read: its reply is fetched
            {"eval": SHOWN},                                                                    # 2
            CLEAR, until("document.querySelectorAll('.dv-zone .rv-stream .rv-line').length === 1"), {"eval": SHOWN},   # 3: only the unconfirmed line is left: Clear says so
            *RELOAD, until("document.querySelector('.dv-lease-held')"), {"eval": SHOWN},        # 4: it is still there, with its marks
            {"eval": "document.querySelector('.dv-zone .rv-mark-no').click(); 1"}, until("document.querySelector('.dv-notarrived')"),
            {"eval": SHOWN},                                                                    # 5: its words are back in the box, and now it can be cleared
            CLEAR, until("document.querySelectorAll('.dv-zone .rv-stream .rv-line').length === 0"), {"eval": SHOWN}])   # 6
        requests = list(stub.requests)
        marks = dict(stub.marks)
        during_clear = stub.between("b1", "a1")
    s = _shown(out)
    three, after1, waiting, after2, reloaded, back, last = s
    assert len(three["lines"]) == 3 + 1 and three["marks"] == 2, three                              # (the answered line's reply is a fourth row)
    assert len(after1["lines"]) == 2 and "answered line" not in "".join(after1["lines"]).replace("waiting", "") and after1["marks"] == 2, after1
    assert any("waiting line" in x for x in after1["lines"]) and any("maybe line" in x for x in after1["lines"]), after1
    assert after1["cancels"] >= 1, after1                                                           # Clear stopped the reply being read
    assert after1["note"].startswith("Cleared.") and "stays" in after1["note"], after1
    assert any("answer to the waiting line" in x for x in waiting["lines"]), waiting
    assert len(after2["lines"]) == 1 and "maybe line" in after2["lines"][0] and after2["marks"] == 2, after2
    assert after2["clearOff"] is True and after2["clearText"] == "Nothing to clear", after2
    assert len(reloaded["lines"]) == 1 and reloaded["marks"] == 2 and reloaded["clearOff"] is True, reloaded
    assert back["draft"] == "maybe line" and back["clearOff"] is False, back
    assert last["lines"] == [] and last["clearText"] == "Clear", last
    assert {r for r in requests if r[1].startswith("/cockpit/desk")} == {("GET", "/cockpit/desk.json"), ("POST", "/cockpit/desk/send")}, requests
    assert stub_ok(requests), requests


# The cockpit page makes requests of its own whatever the Desk does (its assets and panel reads, the service worker's landing record):
# they are not the Desk's. What a Clear must never do is write, or touch the desk's record beyond reading it.
AMBIENT = {("POST", "/webpush/landed")}


def desk_touches(requests) -> list[tuple[str, str]]:
    """The requests that are neither a plain GET (a read or a page asset) nor the page's own ambient write, plus every request under
    /cockpit/desk other than the read of it. A Clear leaves none."""
    return [(m, p) for m, p in requests
            if (m, p) not in AMBIENT and (m != "GET" or (p.startswith("/cockpit/desk") and p != "/cockpit/desk.json"))]


def stub_ok(requests) -> bool:
    """Every request that is not a read or a send is a page asset the cockpit serves (a GET), never a write to the desk's record."""
    return all(m == "GET" or (m, p) in AMBIENT or (m, p) == ("POST", "/cockpit/desk/send") for m, p in requests)


# --- the dock's words under the box survive the Desk's repaints (levain#23 L1 fix W1) -----------------------------------------------

@needs_browser
def test_the_docks_words_under_the_box_survive_a_keystroke_and_a_poll(tmp_path):
    """Red with: the Desk's paintDock writing `sendNote.textContent` on every call: the dock's dictation reason is blank after the next
    keystroke or poll (the Room never writes the line on a repaint)."""
    note = "(() => document.querySelector('.dv-zone .rv-sendnote').textContent)()"
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"init": VOICE_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN,
                                           until("document.querySelector('.dv-lease-held')"),
                                           {"eval": f"{DICTATE}.click(); window.__rec.onerror({{error: 'not-allowed'}}); 1"},
                                           {"eval": note}, typed("a word"), {"eval": note},               # a keystroke repaints the dock
                                           put(get={"status": 200, "json": wired(thread_id="t9", events=[{"by": "desk", "kind": "took", "at": "09:00:00"}])}),
                                           {"eval": "new Promise(r => setTimeout(r, 4500)).then(() => 1)"}, {"eval": note}])   # a poll draws again
    said = [v for v in out["values"] if isinstance(v, str) and v.startswith("Dictation needs")]
    assert len(said) == 3, out["values"]


# --- a Desk mounted again reads its new reply (levain#23 L1 fix W2) ------------------------------------------------------------------

@needs_browser
def test_a_reply_after_clear_and_leaving_the_desk_is_read_not_taken_for_one_already_read(tmp_path):
    """voice.js keeps what it decided for the life of the page; the Desk's numbers used to restart at 1 on every mount. Red with:
    the numbering held per mount again (the second reply is numbered 1, is already seen, and nothing is spoken)."""
    r1 = {"by": "desk", "kind": "reply", "text": "first answer", "at": "09:03:00"}
    r2 = {"by": "desk", "kind": "reply", "text": "second answer", "at": "09:09:00"}
    jump = lambda which: {"eval": f"document.querySelector('[data-phone-jump={which}]').click(); new Promise(r => setTimeout(r, 600)).then(() => 1)"}  # noqa: E731
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"init": VOICE_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, VOICE_ON,
                                           until("document.querySelector('.dv-lease-held')"),
                                           typed("one"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
                                           put(by_thread={"t1": {"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[r1])}}),
                                           until("window.__spoken.length >= 1"),
                                           {"eval": "window.__utt.onend(); window.__spoken.length = 0; 1"},
                                           {"eval": "document.querySelector('.dv-zone .dv-clear').click(); 1"},
                                           jump("room"), jump("desk"), until("document.querySelector('.dv-lease-held')"),
                                           put(script=[accepted(ref="inbox-2", thread="t2")],
                                               by_thread={"t2": {"status": 200, "json": wired(thread_id="t2", cursor="c9", events=[r2])}}),
                                           typed("two"), CLICK_SEND,
                                           until("window.__spoken.length >= 1"), {"eval": "JSON.stringify({ret: 'spoke', spoken: window.__spoken})"}])
    spoke = json.loads([v for v in out["values"] if isinstance(v, str) and '"ret":"spoke"' in v][-1])
    assert spoke["spoken"] == ["second answer"], spoke


# --- a failed read changes nothing for the voice (levain#23 L1 fix W3) -------------------------------------------------------------

@needs_browser
def test_a_failed_poll_neither_stops_the_reply_being_read_nor_turns_the_next_reply_into_backlog(tmp_path):
    """Red with: voiceSync passing thread null for a read that failed (the playing reply is cut mid-sentence and the reply that landed
    during the blip is backlog: never read)."""
    r1 = {"by": "desk", "kind": "reply", "text": "first answer", "at": "09:03:00"}
    r2 = {"by": "desk", "kind": "reply", "text": "second answer", "at": "09:09:00"}
    ok1 = {"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[r1])}
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"init": VOICE_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, VOICE_ON,
                                           until("document.querySelector('.dv-lease-held')"),
                                           put(script=[accepted(ref="inbox-1", thread="t1"), accepted(ref="inbox-2", thread="t2")]),
                                           # both lines are sent BEFORE any answer: sending is a tap, and a tap stops what is being read. A thread the
                                           # desk has answered is not read again, so t2 is the only thread under read once t1's answer is in: its read is the one that fails.
                                           typed("one"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
                                           typed("two"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 2"),
                                           put(by_thread={"t1": ok1}),
                                           until("window.__spoken.length >= 1"),                      # first answer is being read (its utterance is not ended)
                                           put(by_thread={"t1": ok1, "t2": {"status": 500, "json": {}}}),
                                           until("document.querySelector('.dv-state-error')"),
                                           {"eval": "JSON.stringify({ret: 'blip', playing: document.querySelector('.dv-zone .voice-btn').getAttribute('data-voice-icon'), spoken: window.__spoken})"},
                                           put(by_thread={"t1": ok1, "t2": {"status": 200, "json": wired(thread_id="t2", cursor="c3", events=[r2])}}),
                                           until("document.querySelectorAll('.dv-zone .rv-room .voice-btn').length === 2"),
                                           {"eval": "window.__utt.onend(); 1"}, until("window.__spoken.length >= 2"),
                                           {"eval": "JSON.stringify({ret: 'after', spoken: window.__spoken})"}])
    blip = json.loads([v for v in out["values"] if isinstance(v, str) and '"ret":"blip"' in v][-1])
    after = json.loads([v for v in out["values"] if isinstance(v, str) and '"ret":"after"' in v][-1])
    assert blip["playing"].endswith("stop") and blip["spoken"] == ["first answer"], blip           # not cut by the failed read
    assert after["spoken"] == ["first answer", "second answer"], after                              # not made backlog by it


@needs_browser
def test_clear_keeps_every_line_not_positively_settled(tmp_path):
    """Red with: keepLine defaulting to CLEAR (an "It arrived" line with no thread, a failed send, a malformed 200, a filed line with no
    thread_id, and a line whose thread's reply has not been polled yet are all dropped by Clear: the words or the answer are lost)."""
    malformed = {"status": 200, "json": {"outcome": "something_new"}}
    no_thread = {"status": 200, "json": {"outcome": "accepted", "ref": "inbox-9", "thread_id": None, "reason": None, "replayed": False}}
    lines = ["arrived line", "dropped line", "malformed line", "no thread line", "unpolled line"]
    one_more = lambda n: until(f"document.querySelectorAll('.dv-zone .rv-stream .rv-line').length === {n} && !document.querySelector('.dv-zone .rv-send').disabled")  # noqa: E731
    with recording_page(tmp_path) as (stub, url):
        out = _drive(tmp_path, url, [
            *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, until("document.querySelector('.dv-lease-held')"),
            put(script=[AMBIGUOUS]), typed(lines[0]), CLICK_SEND, until("document.querySelector('.dv-ambiguous')"),
            {"eval": "document.querySelector('.dv-zone .rv-mark-yes').click(); 1"}, one_more(1),         # "It arrived": settled by hand, no thread
            put(script=[{"drop": True}]), typed(lines[1]), CLICK_SEND, one_more(2),                       # network failure
            put(script=[malformed]), typed(lines[2]), CLICK_SEND, one_more(3),                            # a 200 the page does not understand
            put(script=[no_thread]), typed(lines[3]), CLICK_SEND, one_more(4),                            # accepted, no thread to read
            put(script=[accepted(ref="inbox-5", thread="t5")]), typed(lines[4]), CLICK_SEND, one_more(5), # a thread whose reply has not landed
            {"eval": SHOWN}, MARK("b"), CLEAR, MARK("a"), {"eval": SHOWN}])
        marks = dict(stub.marks)
        during_clear = stub.between("b", "a")
    before, after = _shown(out)
    assert len(before["lines"]) == 5 and before["clearOff"] is True, before                            # nothing is settled: Clear is off
    assert len(after["lines"]) == 5 and all(w in "".join(after["lines"]) for w in lines), after
    assert json.loads(after["sent"]) and len(json.loads(after["sent"])) == 5, after
    assert desk_touches(during_clear) == [] and marks["a"]["rows"] == marks["b"]["rows"], (during_clear, marks)
