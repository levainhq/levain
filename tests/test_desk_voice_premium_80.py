"""levain#80: with Premium TTS on, a desk reply in the Desk view plays the premium voice, through the Room's shared voice player.

Before this the Desk marked its replies ``deviceOnly`` and voice.js read those with the on-device voice whatever the setting said.
Now the Desk's reply names ``{desk: {thread, at, after}}``, the shared player (voice.js) asks POST /cockpit/voice/speak for it, and
the server re-reads the desk's own reply event through the Desk port in ONE read from the cursor the page read it after (the
words are never the page's).

Layer 1 (no browser): the route's parse and the desk chunks, over a stub port AND over the real HostCommandDeskPort running a
fake host command that pages by size as flow's desk_events.py does (and over the real 4,000-character output cap). Layer 2 (the
real ``make_server``): the speak route on a Desk-only server (no room host). Layer 3 (headless Chrome, the real cockpit page
behind the desk stub): the Desk view's play path POSTs the speak route with the stored provider and voice and the cursor, and a
reply with no time reads on the device. Each test names the change that turns it red."""
from __future__ import annotations

import json
import sys
import threading
from datetime import date
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from levain.cockpit import voice as cockpit_voice  # noqa: E402
from levain.desk.desk_port import DeskCommands, HostCommandDeskPort, ScriptedDeskPort  # noqa: E402
from tests.test_cockpit_desk_tab import _call, _serve  # noqa: E402
from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_cockpit_threads_mount import qdir  # noqa: E402,F401  (fixture: the consent directory the server reads)
from tests.test_desk_pane_page import _OPEN, _TAP_DESK, _TOKENED, CLICK_SEND, desk_page, put, typed, until, wired  # noqa: E402
from tests.test_desk_view_1114 import VOICE_INIT  # noqa: E402


@pytest.fixture(autouse=True)
def _clock_before_the_openai_cutoff(monkeypatch):
    """BODY names OpenAI, which ``offered()`` drops from 2027-01-06: the day is pinned, so these tests do not start failing
    when the real calendar passes the cutoff (test_cockpit_voice_routes pins it the same way)."""
    monkeypatch.setattr(cockpit_voice, "_today", lambda: date(2026, 11, 16))


def _ev(by, kind, text, at):
    return SimpleNamespace(by=by, kind=kind, text=text, at=at)


class Port:
    def __init__(self, events, readable=True):
        self.batch = SimpleNamespace(events=events, readable=readable, cursor="c")
        self.asked = []

    def events(self, thread, after):
        self.asked.append((thread, after))
        return self.batch


def _gate():
    return threading.BoundedSemaphore(2)


BODY = {"desk_thread_id": "t1", "desk_at": "09:03:00", "desk_after": None, "chunk": 0, "provider": "openai", "voice": "alloy",
        "speed": 1.0}


def _error(answer):
    return answer.status, json.loads(answer.body)["error"], json.loads(answer.body).get("fallback")


def test_a_desk_body_is_a_known_shape_and_its_words_are_the_desks_own_event() -> None:
    """Red with: DESK_KEYS removed from _parse_speak (bad_keys), _desk_chunks reading any text but the desk's reply event, or
    reading from the thread's start instead of the page's cursor."""
    port = Port([_ev("script", "reply", "not the desk's", "09:03:00"), _ev("desk", "reply", "on it, back by noon", "09:03:00"),
                 _ev("desk", "reply", "later", "09:04:00")])
    chunks, refused = cockpit_voice._desk_chunks(port, _gate(), {**BODY, "desk_after": "c7"})
    assert refused is None and chunks == ["on it, back by noon"] and port.asked == [("t1", "c7")]      # ONE read, from the cursor
    assert _error(cockpit_voice._desk_chunks(port, _gate(), {**BODY, "desk_at": "10:00:00"})[1]) == (404, "desk_moved", "on_device")
    assert _error(cockpit_voice._desk_chunks(Port([], readable=False), _gate(), BODY)[1]) == (503, "desk_unreadable", "on_device")
    assert _error(cockpit_voice._desk_chunks(None, _gate(), BODY)[1]) == (404, "not_found", None)


def test_two_replies_at_one_time_same_words_are_read_and_different_words_are_refused_by_name() -> None:
    """``at`` is to the second, so it is not unique. Red with: a list (not a set) of matches, or the first match picked."""
    same = Port([_ev("desk", "reply", "ok", "t"), _ev("desk", "reply", "ok", "t")])
    assert cockpit_voice._desk_chunks(same, _gate(), {**BODY, "desk_at": "t"}) == (["ok"], None)
    two = Port([_ev("desk", "reply", "a", "t"), _ev("desk", "reply", "b", "t")])
    assert _error(cockpit_voice._desk_chunks(two, _gate(), {**BODY, "desk_at": "t"})[1]) == (409, "desk_ambiguous", "on_device")


def test_the_desk_read_takes_the_desk_views_own_gate_and_never_waits_for_it() -> None:
    """Red with: the gate not taken (the port is asked with every slot held), taken blocking (the call hangs), or never released."""
    port = Port([_ev("desk", "reply", "ok", "09:03:00")])
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(blocking=False)
    assert _error(cockpit_voice._desk_chunks(port, gate, BODY)[1]) == (503, "desk_busy", None) and port.asked == []
    gate.release()
    assert cockpit_voice._desk_chunks(port, gate, BODY)[0] == ["ok"]
    assert cockpit_voice._desk_chunks(port, gate, BODY)[0] == ["ok"]        # released after each read
    assert _error(cockpit_voice._desk_chunks(port, None, BODY)[1]) == (503, "desk_busy", None)    # no gate: no read


def test_a_body_carrying_words_is_still_refused() -> None:
    """The route never speaks a sentence the page chose. Red with: a `text` key accepted beside the desk keys."""
    ok = cockpit_voice._parse_speak(json.dumps(BODY).encode())
    assert ok[1] is None and ok[0]["desk_thread_id"] == "t1"
    bad = cockpit_voice._parse_speak(json.dumps({**BODY, "text": "say this"}).encode())
    assert bad[0] is None and bad[1].status == 400
    for k, v in (("desk_at", ""), ("desk_at", None), ("desk_thread_id", ""), ("desk_after", ""), ("desk_after", 3), ("desk_at", "x" * 65)):
        assert cockpit_voice._parse_speak(json.dumps({**BODY, k: v}).encode())[1].status == 400, (k, v)
    no_after = dict(BODY)
    del no_after["desk_after"]
    assert json.loads(cockpit_voice._parse_speak(json.dumps(no_after).encode())[1].body)["error"] == "bad_request"


# --- over the real port: a fake host that pages by size, as flow's desk_events.py does --------------------------------------
PAGED = """\
import json, sys
src, page = sys.argv[1], int(sys.argv[2])
args = sys.argv[3:]
rows = json.load(open(src))
start = int(args[args.index("--after") + 1]) if "--after" in args else 0
if page <= 0:
    out = rows[start:]
else:
    out = []
    for r in rows[start:]:
        if out and len(json.dumps({"events": out + [r], "cursor": "0" * 32})) > page:
            break
        out.append(r)
print(json.dumps({"events": out, "cursor": str(start + len(out)) if out else (args[args.index("--after") + 1] if "--after" in args else None)}))
"""
THREAD = "th-0abcdef-a1b2c3"


def _host_port(tmp_path, rows, page):
    script, src = tmp_path / "paged_host.py", tmp_path / "rows.json"
    script.write_text(PAGED)
    src.write_text(json.dumps(rows))
    return HostCommandDeskPort(DeskCommands(events=(sys.executable, str(script), str(src), str(page))), tmp_path / "claims.db")


def _row(i, text=None, by="desk", at=None):
    return {"by": by, "kind": "reply" if by == "desk" else "took", "text": text or f"reply number {i} " + "w" * 120,
            "at": at or f"2026-11-16T09:{i:02d}:00-05:00"}


def test_over_the_real_port_a_reply_past_the_first_page_is_found_from_its_own_cursor(tmp_path) -> None:
    """The host pages by size, oldest first: a read from the thread's start never reaches a later page. Red with: _desk_chunks
    reading ``events(thread, None)`` (the late reply answers desk_moved)."""
    rows = [_row(i) for i in range(12)]
    port = _host_port(tmp_path, rows, page=700)
    first = port.events(THREAD, None)
    assert first.readable and 0 < len(first.events) < 12, first             # the host did page
    later = port.events(THREAD, first.cursor)
    target = later.events[0]
    req = {**BODY, "desk_thread_id": THREAD, "desk_at": target.at, "desk_after": first.cursor}
    assert cockpit_voice._desk_chunks(port, _gate(), req) == ([target.text], None)
    # the same reply named from the thread's start is not where the page read it: refused, with the on-device fallback
    assert _error(cockpit_voice._desk_chunks(port, _gate(), {**req, "desk_after": None})[1]) == (404, "desk_moved", "on_device")


def test_over_the_real_port_an_answer_past_the_output_cap_is_unreadable_never_a_guess(tmp_path) -> None:
    """A host that does not page and passes the 4,000-character output cap is cut, so the batch is unreadable. Red with: an
    unreadable batch read as an empty one (desk_moved) or as a 500."""
    port = _host_port(tmp_path, [_row(i) for i in range(40)], page=0)
    assert port.events(THREAD, None).readable is False
    got = cockpit_voice._desk_chunks(port, _gate(), {**BODY, "desk_thread_id": THREAD, "desk_at": _row(3)["at"]})
    assert _error(got[1]) == (503, "desk_unreadable", "on_device")


def test_over_the_real_port_two_same_time_replies(tmp_path) -> None:
    """Red with: the first of two same-``at`` replies with different words read."""
    at = "2026-11-16T09:00:00-05:00"
    port = _host_port(tmp_path, [_row(0, "same", at=at), _row(1, "same", at=at)], page=3000)
    assert cockpit_voice._desk_chunks(port, _gate(), {**BODY, "desk_thread_id": THREAD, "desk_at": at}) == (["same"], None)
    port = _host_port(tmp_path, [_row(0, "one", at=at), _row(1, "two", at=at)], page=3000)
    got = cockpit_voice._desk_chunks(port, _gate(), {**BODY, "desk_thread_id": THREAD, "desk_at": at})
    assert _error(got[1]) == (409, "desk_ambiguous", "on_device")


# --- the real server ----------------------------------------------------------------------------------------------------------
def test_a_desk_only_server_answers_the_speak_route_not_found_and_reads_no_desk(tmp_path, qdir) -> None:  # noqa: F811
    """A Desk view with no room host: the voice routes are absent there (V1-4, test_t_gate_the_routes_are_absent_without_a_room_host),
    and a desk body does not open them, nor run a host read. Red with: the room-host condition dropped from _voice_answer's gate."""
    port = ScriptedDeskPort()
    port.script_event(THREAD, "desk", "reply", "hello", "09:03:00")
    asked = []
    real = port.events
    port.events = lambda *a: asked.append(a) or real(*a)
    with _serve(tmp_path, port) as base:
        status, got = _call(base, cockpit_voice.SPEAK_PATH, {**BODY, "desk_thread_id": THREAD})
        assert (status, got["error"]) == (404, "not_found"), got
        assert _call(base, cockpit_voice.VOICE_JSON, method="GET")[0] == 404
    assert asked == []


# --- the page -------------------------------------------------------------------------------------------------------------------
PREMIUM_INIT = VOICE_INIT + """
(() => {
  window.__speak = [];
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    if (new URL(String(u), location.href).pathname === '/cockpit/voice/speak') {
      window.__speak.push(JSON.parse(o.body));
      return Promise.resolve(new Response(new Blob(['x'], { type: 'audio/mpeg' }), { status: 200, headers: { 'X-Voice-Chunks': '1' } }));
    }
    return real(u, o);
  };
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: () => 'blob:x' });
})();"""
PREMIUM_ON = {"eval": "LevainVoice.saveSettings({tier: 'premium', provider: 'openai', voice: 'alloy', speed: 1, autoplay: true}) && 0"}


def _play_first(tmp_path, events):
    with desk_page(tmp_path) as (_stub, base):
        out = _drive(tmp_path, base + "/", [{"init": PREMIUM_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, PREMIUM_ON,
                                           until("document.querySelector('.dv-lease-held')"),
                                           typed("hello desk"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
                                           put(get={"status": 200, "json": wired(thread_id="t1", cursor="c2", events=events)}),
                                           until("document.querySelectorAll('.dv-zone .rv-room .voice-btn').length >= 1"),
                                           {"eval": "document.querySelector('.dv-zone .rv-room .voice-btn').click(); 1"},   # its speaker control: the shared player's tap
                                           until("window.__speak.length >= 1 || window.__spoken.length >= 1"),
                                           {"eval": "JSON.stringify({ret: 'r', speak: window.__speak, spoken: window.__spoken, "
                                                    "buttons: document.querySelectorAll('.dv-zone .voice-btn').length})"}])
    return json.loads([v for v in out["values"] if isinstance(v, str) and '"ret":"r"' in v][-1])


@needs_browser
def test_the_desk_views_play_path_goes_through_the_shared_player_with_the_premium_setting(tmp_path):
    """Red with: `desk:` put back to `deviceOnly: true` in desk_tab_view.js's voiceReply (and voice.js reading it on-device): the
    speak route is never asked and the browser's voice speaks the words; or the cursor the batch was read after not carried."""
    got = _play_first(tmp_path, [{"by": "desk", "kind": "reply", "text": "on it, back by noon", "at": "09:03:00"}])
    assert got["spoken"] == [], got
    assert got["speak"][0] == {"desk_thread_id": "t1", "desk_at": "09:03:00", "desk_after": None, "chunk": 0, "provider": "openai",
                               "voice": "alloy", "speed": 1}, got


@needs_browser
def test_a_desk_reply_with_no_time_reads_on_the_device_and_only_the_desks_replies_have_a_control(tmp_path):
    """A reply the route could never find (no ``at``) is read by this device, never sent as a request the route refuses; a
    non-desk reply draws no control (the route reads only by == "desk"). Red with: routeKey's desk check dropped (a request with
    an empty or undefined desk_at), or the control drawn for every kind == "reply"."""
    got = _play_first(tmp_path, [{"by": "script", "kind": "reply", "text": "not the desk", "at": "09:02:00"},
                                 {"by": "desk", "kind": "reply", "text": "no time on me", "at": ""}])
    assert got["buttons"] == 1, got
    assert got["speak"] == [] and got["spoken"] == ["no time on me"], got
