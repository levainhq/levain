"""Voice V1-5 room hook: a finished reply in the Room is spoken through voice.js (room_view.js + the dashboard.html script tag).

Everything runs on the REAL cockpit page served by ``levain serve`` over the room rig (``tests/test_cockpit_room_join.py``).
Test A also runs the real room: a fake claude over real pipes answers the line, the real pump records the turn, the page polls
the real ``/cockpit/room.json``. The other tests replace ONLY the page's ``GET /cockpit/room.json`` answer with a body built
here (the one way to make two replies finish inside one poll); room_view.js, voice.js and the page are the shipped files.

The output device is stubbed (``speechSynthesis`` records what it is asked to say and lets the test end an utterance;
``HTMLMediaElement.play`` resolves). What a person hears, the unlock on a real iPhone and the silent switch are residue these
tests cannot judge.

Each test names the line whose revert turns it red."""
from __future__ import annotations

import json

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_room_join import _store
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_coordinator_room_serve import fake_serve

INIT = """
(() => {
  window.__spoken = []; window.__utts = []; window.__cancels = 0; window.__room = null; window.__roomCalls = 0;
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const path = new URL(String(u), location.href).pathname;
    if (path === '/cockpit/room.json' && window.__room) {
      window.__roomCalls++;
      return Promise.resolve(new Response(JSON.stringify(window.__room), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    return real(u, o);
  };
  Object.defineProperty(HTMLMediaElement.prototype, 'src', { get() { return this._s || ''; }, set(v) { this._s = v; }, configurable: true });
  HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
  HTMLMediaElement.prototype.pause = function () {};
  window.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; this.voice = null; this.rate = 1; } };
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
    speak(u) { if (u.text) { window.__spoken.push(u.text); window.__utts.push(u); } },   // the unlock speaks an empty one
    cancel() { window.__cancels++; },
    getVoices() { return [{ name: 'local-en', lang: 'en-US', localService: true }]; } } });
  window.__endUtt = () => { const u = window.__utts[window.__utts.length - 1]; if (u && u.onend) u.onend(); };
  %s
})();
"""
NO_MODULE = "Object.defineProperty(window, 'LevainVoice', { configurable: true, get() { return undefined; }, set() {} });"
HEAD = [
    {"until": "document.querySelector('[data-mode-tab=board]')"},
    {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
    {"until": "!window.__pre && document.querySelector('.rv-zone') && document.querySelector('[data-phone-jump=room]')"},
]
SETTINGS = {"eval": "LevainVoice.saveSettings({tier: 'device', autoplay: true}) && 0"}
BTNS = "[...document.querySelectorAll('.rv-room .voice-btn')].map((b) => b.getAttribute('aria-label'))"


def _item(g, kind, body="", source=None):
    it = {"gseq": g, "kind": kind, "body": body, "launch_id": "L1", "at": "2026-11-07T10:00:00Z"}
    if source:
        it["source"] = source
    return it


def _op(g, t):
    return _item(g, "message", t, "operator")


def _room(thread, *items):
    return {"room_thread": thread, "launch_id": "L1", "model": "m", "gap": False, "proposals": [], "composer": {"state": "writable"},
            "stream": list(items)}


def _set(body):
    return {"eval": f"window.__room = {json.dumps(body)}; 1"}


def _to_room():
    return {"do": "document.querySelector('[data-phone-jump=room]').click(); 1", "until": "document.querySelector('.rv .rv-input')"}


def _run(tmp_path, qdir, steps, init_extra=""):
    (tmp_path / "fk").mkdir()
    _store_, rs, _fake = fake_serve(tmp_path / "fk", store=_store(qdir))
    _root, src, _ids = _install(tmp_path / "inst")
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [{"init": INIT % init_extra}, *HEAD, *steps])
    return out["values"]


A = [_op(1, "first"), _item(2, "vendor_agent_text", "A old reply"), _item(3, "vendor_turn_ended")]


@needs_browser
def test_a_real_round_trip_speaks_the_reply_after_send_and_the_backlog_after_a_remount_does_not(tmp_path, qdir) -> None:
    """The real room: Send (a tap) unlocks, the fake claude's reply is recorded by the real pump, the real poll shows it and
    voice.js reads it. After an app switch the new mount's first body is backlog: the reply shows Play and nothing is spoken.
    Red with: `voiceTap()` removed from the Send handler (the reply asks "Tap to hear" and nothing is spoken); `V.arm(...)`
    removed (an unarmed module reads nothing); voiceSync not called after renderRoom (no read)."""
    hide_show = ("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
                 " document.dispatchEvent(new Event('visibilitychange'));"
                 " Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                 " document.dispatchEvent(new Event('visibilitychange')); 1")
    v = _run(tmp_path, qdir, [
        _to_room(),
        {"until": "document.querySelector('.rv-open') && !document.querySelector('.rv-open').hidden"},
        {"eval": "document.querySelector('.rv-open').click(); 1"},
        {"until": "!document.querySelector('.rv-input').disabled && document.querySelector('.rv-open').hidden"},
        SETTINGS,
        {"eval": "const ta = document.querySelector('.rv-input'); ta.value = 'hello page'; ta.dispatchEvent(new Event('input')); document.querySelector('.rv-send').click(); 1"},
        {"until": "window.__spoken.length >= 1 && document.querySelectorAll('.rv-room .voice-btn').length === 1", "ms": 30000},
        {"eval": "JSON.stringify([window.__spoken, %s])" % BTNS},
        {"eval": hide_show},
        {"until": "document.querySelectorAll('.rv-input').length === 1 && document.querySelectorAll('.rv-room .voice-btn').length === 1"},
        {"eval": "new Promise((r) => setTimeout(r, 3500)).then(() => JSON.stringify([window.__spoken, %s]))" % BTNS},
    ])
    spoken, btns = json.loads(v[-4])
    assert len(spoken) == 1 and "echo: hello page" in spoken[0], spoken
    assert btns == ["Stop"] or btns == ["Play"], btns       # the reading is on (or already over): never "Tap to hear"
    spoken2, btns2 = json.loads(v[-1])
    assert spoken2 == spoken and btns2 == ["Play"], (spoken2, btns2)


@needs_browser
def test_two_replies_finishing_in_one_poll_queue_one_behind_the_playing_reply_and_the_later_one_wins(tmp_path, qdir) -> None:
    """A is playing (tapped). B and C finish inside one poll: ONE waits behind A, and it is C (B is replaced, never read).
    Red with: the `r.gseq > voiceBase` filter removed (the backlog A is offered again: harmless) is NOT this test; red with:
    voiceSync not handing replies to V.replyFinished (nothing queued, C never read); handing only the first (B read)."""
    b = _room("t1", *A, _op(4, "again"), _item(5, "vendor_agent_text", "B reply"), _item(6, "vendor_turn_ended"),
              _op(7, "and again"), _item(8, "vendor_agent_text", "C reply"), _item(9, "vendor_turn_ended"))
    v = _run(tmp_path, qdir, [
        _set(_room("t1", *A)),
        _to_room(),
        {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 1"},
        SETTINGS,
        {"eval": "document.querySelector('.rv-room .voice-btn').click(); 1"},
        {"until": "window.__spoken.length === 1"},
        _set(b),
        {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 3", "ms": 15000},
        {"eval": "JSON.stringify([window.__spoken, %s, [...document.querySelectorAll('.rv-room .voice-say')].map((n) => n.textContent)])" % BTNS},
        {"eval": "window.__endUtt(); 1"},
        {"until": "window.__spoken.length === 2"},
        {"eval": "new Promise((r) => setTimeout(r, 400)).then(() => JSON.stringify(window.__spoken))"},
    ])
    spoken, btns, says = json.loads(v[-4])
    assert spoken == ["A old reply"], spoken                        # nothing talks over A
    assert btns == ["Stop", "Play", "Play"], btns
    assert says[0] == "" and says[1] != "" and says[2] != "" and says[1] != says[2], says     # B says it was skipped, C that it waits
    assert json.loads(v[-1]) == ["A old reply", "C reply"], v[-1]   # C after A; B never read


@needs_browser
def test_send_stops_the_reading_and_a_new_conversation_stops_it_and_reads_nothing_of_the_old_backlog(tmp_path, qdir) -> None:
    """Red with: `voiceTap()` removed from the Send handler (A keeps its Stop button); the `V.stop()` on a changed thread
    removed (A keeps playing into the new conversation); `V.arm` not re-called on a new thread (the new room's old reply is read)."""
    new = _room("t2", _op(1, "other"), _item(2, "vendor_agent_text", "T2 old"), _item(3, "vendor_turn_ended"))
    v = _run(tmp_path, qdir, [
        _set(_room("t1", *A)),
        _to_room(),
        {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 1"},
        SETTINGS,
        {"eval": "document.querySelector('.rv-room .voice-btn').click(); 1"},
        {"until": "window.__spoken.length === 1 && document.querySelector('.rv-room .voice-btn').getAttribute('aria-label') === 'Stop'"},
        {"eval": "const ta = document.querySelector('.rv-input'); ta.value = 'interrupt'; ta.dispatchEvent(new Event('input')); window.__t0 = performance.now(); document.querySelector('.rv-send').click(); 1"},
        {"until": "document.querySelector('.rv-room .voice-btn').getAttribute('aria-label') === 'Play'"},
        {"eval": "window.__t0 = performance.now() - window.__t0; 1"},
        {"eval": "document.querySelector('.rv-room .voice-btn').click(); 1"},          # play A again, then the conversation is replaced
        {"until": "window.__spoken.length === 2 && document.querySelector('.rv-room .voice-btn').getAttribute('aria-label') === 'Stop'"},
        {"eval": "window.__before = window.__cancels; 1"},
        _set(new),
        {"until": "[...document.querySelectorAll('.rv-room .rv-text')].some((n) => n.textContent.includes('T2 old'))", "ms": 15000},
        {"eval": "new Promise((r) => setTimeout(r, 600)).then(() => JSON.stringify([window.__spoken, window.__cancels > window.__before, %s, window.__t0]))" % BTNS},
    ])
    spoken, cancelled, btns, took = json.loads(v[-1])
    assert took < 1500, took        # Send stopped the reading itself; the engine's own watchdog is several seconds
    assert spoken == ["A old reply", "A old reply"], spoken        # the new room's old reply is backlog: never read
    assert cancelled is True and btns == ["Play"], (cancelled, btns)


@needs_browser
def test_a_page_without_the_voice_module_draws_the_room_and_no_control(tmp_path, qdir) -> None:
    """Red with: the `voice()` null guard removed (the mount throws on the first body and the room is not drawn)."""
    v = _run(tmp_path, qdir, [
        _set(_room("t1", *A)),
        _to_room(),
        {"until": "[...document.querySelectorAll('.rv-room .rv-text')].some((n) => n.textContent.includes('A old reply'))"},
        {"eval": "JSON.stringify([document.querySelectorAll('.voice-btn').length, typeof window.LevainVoice])"},
    ], init_extra=NO_MODULE)
    assert json.loads(v[-1]) == [0, "undefined"], v[-1]


def test_the_dashboard_loads_voice_js_and_the_hook_is_wired() -> None:
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"
    html = (web / "dashboard.html").read_text()
    assert html.count('<script src="/voice.js"></script>') == 1
    js = (web / "room_view.js").read_text()
    for needle in ("V.arm(voiceBase)", "V.replyFinished(r)", "V.unlock(); V.stop();", "V.button(r)", "w._unbind()", "if (V) V.stop();"):
        assert needle in js, needle


# --- L3 round 1 fixes ---------------------------------------------------------------------------------------------------
POLLS = "new Promise((r) => { const n = window.__roomCalls; const t = setInterval(() => { if (window.__roomCalls >= n + 3) { clearInterval(t); r(1); } }, 100); })"
PLAYING_A = [
    _set(_room("t1", *A)),
    _to_room(),
    {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 1"},
    SETTINGS,
    {"eval": "document.querySelector('.rv-room .voice-btn').click(); 1"},
    {"until": "window.__spoken.length === 1"},
]


@needs_browser
def test_a_second_turn_with_no_operator_line_between_speaks_only_its_own_words(tmp_path, qdir) -> None:
    """The server's reply_text starts a reply after the operator's last line OR the previous vendor_turn_ended; the page must
    match it. Red with: `buf = [];` removed from the vendor_turn_ended branch of repliesOf (the second turn is spoken as
    "A old reply\\n\\nsecond")."""
    b = _room("t1", *A, _item(4, "vendor_agent_text", "second"), _item(5, "vendor_turn_ended"))
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        _set(b),
        {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 2", "ms": 15000},
        {"eval": "window.__endUtt(); 1"},
        {"until": "window.__spoken.length === 2"},
        {"eval": "JSON.stringify(window.__spoken)"},
    ])
    assert json.loads(v[-1]) == ["A old reply", "second"], v[-1]


@needs_browser
def test_a_whitespace_only_agent_text_is_not_a_reply(tmp_path, qdir) -> None:
    """The server drops a body that is only whitespace (``body.strip()``), so premium would say nothing_to_read. Red with:
    `.trim()` removed from the vendor_agent_text test of repliesOf (a control appears for the blank turn and it is queued)."""
    b = _room("t1", *A, _item(4, "vendor_agent_text", "  \n "), _item(5, "vendor_turn_ended"))
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        _set(b),
        {"eval": POLLS},
        {"eval": "window.__endUtt(); 1"},
        {"eval": "new Promise((r) => setTimeout(r, 500)).then(() => JSON.stringify([window.__spoken, %s]))" % BTNS},
    ])
    spoken, btns = json.loads(v[-1])
    assert spoken == ["A old reply"] and len(btns) == 1, (spoken, btns)


@needs_browser
def test_a_room_that_goes_away_stops_the_reading_and_a_later_room_is_backlog_again(tmp_path, qdir) -> None:
    """Red with: the `m.thread === null` branch of voiceSync (V.stop(), voiceThread = null) removed: the audio keeps playing
    and, when t1 comes back with a newer reply, it is read as if it were new."""
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        {"eval": "window.__before = window.__cancels; 1"},
        _set(_room(None)),
        {"until": "window.__cancels > window.__before && document.querySelectorAll('.rv-room .voice-btn').length === 0", "ms": 15000},
        _set(_room("t1", *A, _op(4, "x"), _item(5, "vendor_agent_text", "T1 later"), _item(6, "vendor_turn_ended"))),
        {"until": "document.querySelectorAll('.rv-room .voice-btn').length === 2", "ms": 15000},
        {"eval": "new Promise((r) => setTimeout(r, 600)).then(() => JSON.stringify(window.__spoken))"},
    ])
    assert json.loads(v[-1]) == ["A old reply"], v[-1]


DEFER = """
  const f1 = window.fetch;
  window.__defer = false; window.__holdRot = false; window.__rel = null; window.__relRot = null;
  window.fetch = (u, o) => {
    const path = new URL(String(u), location.href).pathname;
    if (path === '/cockpit/room.json' && window.__defer) {
      const p = f1(u, o);                                   // the body as it is NOW; the page gets it when released
      window.__defer = false;
      return new Promise((res) => { window.__rel = () => res(p); });
    }
    if (path.endsWith('/rotate') && window.__holdRot) return new Promise((res) => {
      window.__relRot = () => res(f1(u, o));
      window.__failRot = () => res(new Response(JSON.stringify({message: 'no'}), { status: 500, headers: { 'Content-Type': 'application/json' } }));
    });
    return f1(u, o);
  };
"""
B_T1 = _room("t1", *A, _op(4, "again"), _item(5, "vendor_agent_text", "B reply"), _item(6, "vendor_turn_ended"))


@needs_browser
def test_a_poll_sent_before_the_new_click_cannot_read_the_old_reply_after_it(tmp_path, qdir) -> None:
    """Red with: `gen++` removed from the New click (the pre-click poll lands, renders B and voiceSync speaks it)."""
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        {"eval": "window.__room = %s; window.__defer = true; 1" % json.dumps(B_T1)},
        {"until": "window.__rel", "ms": 15000},
        _set(_room("t2", _op(1, "other"), _item(2, "vendor_agent_text", "T2 old"), _item(3, "vendor_turn_ended"))),
        {"eval": "document.querySelector('.rv-rotate').click(); window.__rel(); 1"},
        {"eval": "new Promise((r) => setTimeout(r, 1500)).then(() => JSON.stringify(window.__spoken))"},
    ], init_extra=DEFER)
    assert json.loads(v[-1]) == ["A old reply"], v[-1]


@needs_browser
def test_a_reply_that_finishes_while_the_new_conversation_is_pending_is_not_read(tmp_path, qdir) -> None:
    """Red with: the `voiceHold` check removed from voiceSync (a poll sent after the click, before the rotate answers, still
    names the old room and reads B)."""
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        {"eval": "window.__holdRot = true; document.querySelector('.rv-rotate').click(); 1"},
        _set(B_T1),
        {"eval": POLLS},
        {"eval": "new Promise((r) => setTimeout(r, 300)).then(() => JSON.stringify(window.__spoken))"},
    ], init_extra=DEFER)
    assert json.loads(v[-1]) == ["A old reply"], v[-1]


@needs_browser
def test_a_falsy_voice_button_leaving_the_room_does_not_break_the_poll(tmp_path, qdir) -> None:
    """Red only with BOTH `if (b)` removed from voiceBtn (the null is cached) and `if (!w) return;` removed from voiceSync (the
    reply leaving the room throws and the room reads "could not be read"). Either line alone leaves it green: they guard the
    same null, and voiceBtn is the only writer, so `if (!w) return;` cannot fire while `if (b)` holds. Only the crash is
    asserted: the drawn rows are not rebuilt, so a late control for A cannot be observed."""
    b = _room("t1", *A, _op(4, "again"), _item(5, "vendor_agent_text", "B reply"), _item(6, "vendor_turn_ended"))
    v = _run(tmp_path, qdir, [
        {"eval": "window.__b = LevainVoice.button; LevainVoice.button = (r) => (r.gseq === 3 ? null : window.__b(r)); 1"},
        _set(_room("t1", *A)),
        _to_room(),
        {"until": "[...document.querySelectorAll('.rv-room .rv-text')].some((n) => n.textContent.includes('A old reply'))"},
        {"eval": "LevainVoice.button = window.__b; 1"},
        _set(b),
        {"until": "[...document.querySelectorAll('.rv-room .rv-text')].some((n) => n.textContent.includes('B reply'))", "ms": 15000},
        {"eval": "window.__err = false; new MutationObserver(() => { if ([...document.querySelectorAll('.rv-note')].some((n) => n.textContent.includes('could not be read'))) window.__err = true; }).observe(document.body, {subtree: true, childList: true, characterData: true}); 1"},
        _set(_room(None)),
        {"eval": POLLS},
        {"eval": "JSON.stringify(window.__err)"},
    ])
    assert json.loads(v[-1]) is False, v[-1]


@needs_browser
def test_a_failed_new_click_does_not_read_the_reply_that_finished_during_it(tmp_path, qdir) -> None:
    """The rotate is held, reply B finishes on the OLD thread meanwhile, then the rotate fails (500). The pull after it still
    names t1 with B: the hold is gone, so without a re-baseline B is read as new.
    Red with: `voiceThread = null` removed from the rotate settle in the New click."""
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        {"eval": "window.__holdRot = true; document.querySelector('.rv-rotate').click(); 1"},
        _set(B_T1),
        {"eval": POLLS},
        {"eval": "window.__failRot(); 1"},
        {"eval": POLLS},
        {"eval": "new Promise((r) => setTimeout(r, 500)).then(() => JSON.stringify(window.__spoken))"},
    ], init_extra=DEFER)
    assert json.loads(v[-1]) == ["A old reply"], v[-1]


@needs_browser
def test_a_new_click_answered_before_the_poll_names_the_new_room_does_not_read_the_old_rooms_reply(tmp_path, qdir) -> None:
    """The rotate answers 200 but the next poll still names t1 (the server catching up) and carries B, which finished during
    the click. Red with: `voiceThread = null` removed from the rotate settle in the New click."""
    v = _run(tmp_path, qdir, [
        *PLAYING_A,
        {"eval": "window.__holdRot = true; document.querySelector('.rv-rotate').click(); 1"},
        _set(B_T1),
        {"eval": POLLS},
        {"eval": "window.__relRot(); 1"},
        {"eval": POLLS},
        {"eval": "new Promise((r) => setTimeout(r, 500)).then(() => JSON.stringify(window.__spoken))"},
    ], init_extra=DEFER)
    assert json.loads(v[-1]) == ["A old reply"], v[-1]
