"""levain#80: with Premium TTS on, a desk reply in the Desk view plays the premium voice, through the Room's shared voice player.

Before this the Desk marked its replies ``deviceOnly`` and voice.js read those with the on-device voice whatever the setting said.
Now the Desk's reply names ``{desk: {thread, at}}``, the shared player (voice.js) asks POST /cockpit/voice/speak for it, and the
server re-reads the desk's own event for that pair through the Desk port (the words are never the page's).

Layer 1 (no browser): the route's parse and the desk chunks. Layer 2 (headless Chrome, the real cockpit page behind the desk stub):
the Desk view's play path POSTs the speak route with the stored provider and voice, and the browser's own voice says nothing.
Each test names the change that turns it red."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from levain.cockpit import voice as cockpit_voice  # noqa: E402
from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_desk_pane_page import _OPEN, _TAP_DESK, _TOKENED, CLICK_SEND, desk_page, put, typed, until, wired  # noqa: E402
from tests.test_desk_view_1114 import VOICE_INIT  # noqa: E402


def _ev(by, kind, text, at):
    return SimpleNamespace(by=by, kind=kind, text=text, at=at)


class Port:
    def __init__(self, events, readable=True):
        self.batch = SimpleNamespace(events=events, readable=readable, cursor="c")
        self.asked = []

    def events(self, thread, after):
        self.asked.append((thread, after))
        return self.batch


BODY = {"desk_thread_id": "t1", "desk_at": "09:03:00", "chunk": 0, "provider": "openai", "voice": "alloy", "speed": 1.0}


def test_a_desk_body_is_a_known_shape_and_its_words_are_the_desks_own_event() -> None:
    """Red with: DESK_KEYS removed from _parse_speak (bad_keys), or _desk_chunks reading any text but the desk's reply event."""
    port = Port([_ev("you", "line", "not mine", "09:03:00"), _ev("desk", "reply", "on it, back by noon", "09:03:00"),
                 _ev("desk", "reply", "later", "09:04:00")])
    chunks, refused = cockpit_voice._desk_chunks(port, BODY)
    assert refused is None and chunks == ["on it, back by noon"] and port.asked == [("t1", None)]
    assert cockpit_voice._desk_chunks(port, {**BODY, "desk_at": "10:00:00"})[1].status == 404     # no such event
    assert cockpit_voice._desk_chunks(Port([], readable=False), BODY)[1].status == 404
    assert cockpit_voice._desk_chunks(None, BODY)[1].status == 404
    assert cockpit_voice._desk_chunks(Port([_ev("desk", "reply", "a", "t"), _ev("desk", "reply", "b", "t")]), {**BODY, "desk_at": "t"})[1].status == 404


def test_a_body_carrying_words_is_still_refused() -> None:
    """The route never speaks a sentence the page chose. Red with: a `text` key accepted beside the desk keys."""
    ok = cockpit_voice._parse_speak(json.dumps(BODY).encode())
    assert ok[1] is None and ok[0]["desk_thread_id"] == "t1"
    bad = cockpit_voice._parse_speak(json.dumps({**BODY, "text": "say this"}).encode())
    assert bad[0] is None and bad[1].status == 400
    assert cockpit_voice._parse_speak(json.dumps({**BODY, "desk_at": ""}).encode())[1].status == 400


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


@needs_browser
def test_the_desk_views_play_path_goes_through_the_shared_player_with_the_premium_setting(tmp_path):
    """Red with: `desk:` put back to `deviceOnly: true` in desk_tab_view.js's voiceReply (and voice.js reading it on-device): the
    speak route is never asked and the browser's voice speaks the words."""
    reply = {"by": "desk", "kind": "reply", "text": "on it, back by noon", "at": "09:03:00"}
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [{"init": PREMIUM_INIT}, *_TOKENED, put(get={"status": 200, "json": wired()}), _TAP_DESK, _OPEN, PREMIUM_ON,
                                           until("document.querySelector('.dv-lease-held')"),
                                           typed("hello desk"), CLICK_SEND, until("document.querySelectorAll('.dv-zone .dv-filed').length === 1"),
                                           put(get={"status": 200, "json": wired(thread_id="t1", cursor="c2", events=[reply])}),
                                           until("document.querySelectorAll('.dv-zone .rv-room .voice-btn').length === 1"),
                                           {"eval": "document.querySelector('.dv-zone .rv-room .voice-btn').click(); 1"},   # its speaker control: the shared player's tap
                                           until("window.__speak.length >= 1"),
                                           {"eval": "JSON.stringify({ret: 'r', speak: window.__speak, spoken: window.__spoken})"}])
    got = json.loads([v for v in out["values"] if isinstance(v, str) and '"ret":"r"' in v][-1])
    assert got["spoken"] == [], got
    assert got["speak"][0] == {"desk_thread_id": "t1", "desk_at": "09:03:00", "chunk": 0, "provider": "openai", "voice": "alloy", "speed": 1}, got
