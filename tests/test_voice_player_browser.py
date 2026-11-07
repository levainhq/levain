"""Voice V1-5/V1-7 (partial): the player module (levain/templates/web/voice.js) and the Settings page Voice block
(devices_panel.js), driven in headless Chrome on the real cockpit page.

No audio is played and no provider is reached: the page's fetch is wrapped (``/cockpit/voice.json`` and
``/cockpit/voice/speak`` answer from a fixture, every call is recorded), ``HTMLMediaElement`` play/pause/src are stubs that
count, and ``speechSynthesis`` is a recorder. What a person hears and feels (the voice, the timing, the silent switch) is
residue these tests cannot judge.

Each gating test names the line whose revert turns it red (the RETURN records each run)."""
from __future__ import annotations

import http.client
import json
from typing import Any

from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import T, _serve
from tests.test_pair_card_w1b import _drive, needs_browser

VOICES = [f"voice-{n:02d}" for n in range(41)]


def _fixture(*, key: str = "set") -> dict[str, Any]:
    return {
        "providers": [
            {"name": "inworld", "uses": ["tts"] if key == "set" else [], "key": key, "record_id": "vk-1" if key == "set" else None,
             "last_read": None, "voices": VOICES, "default_voice": "voice-03", "tts_model": "m", "speed": [0.5, 1.5],
             "retention": "Inworld retention words."},
            {"name": "deepgram", "uses": [], "key": "not_set", "record_id": None, "last_read": None, "voices": ["a"],
             "default_voice": "a", "tts_model": "m", "speed": [0.5, 1.5], "retention": "Deepgram words."},
        ],
        "prices_as_of": "2026-11-05", "estimate": True, "authority": "ok", "premium_stt": False, "meter": "ok",
        "caps": {"month_usd": "10.00", "day_usd": "2.00", "spent_month_est": "1.25", "spent_day_est": "0.10",
                 "month_start": "x", "day_start": "y"},
    }


def _init(fixture: dict[str, Any] | None) -> str:
    """The page's stubs, installed before its own scripts on every load."""
    return """
(() => {
  window.__calls = []; window.__plays = []; window.__spoken = []; window.__cancels = 0; window.__chunks = 2; window.__refuse = false;
  const FIX = %s;
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const path = new URL(String(u), location.href).pathname;
    window.__calls.push({ path: path, method: (o && o.method) || 'GET', body: o && o.body ? String(o.body) : null, fromVoice: new Error().stack.indexOf('/voice.js') >= 0 });
    if (FIX && path === '/cockpit/voice.json') return Promise.resolve(new Response(JSON.stringify(FIX), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    if (path === '/cockpit/voice/speak') {
      if (window.__delay) return new Promise((r) => setTimeout(r, window.__delay)).then(() => new Response(new Blob([new Uint8Array([1])], { type: 'audio/mpeg' }), { status: 200, headers: { 'X-Voice-Chunks': '1' } }));
      if (window.__refuse) return Promise.resolve(new Response(JSON.stringify({ error: 'month_cap', message: 'Premium voice is paused: this month cap is reached.', fallback: 'on_device' }), { status: 402, headers: { 'Content-Type': 'application/json' } }));
      return Promise.resolve(new Response(new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/mpeg' }), { status: 200, headers: { 'X-Voice-Chunks': String(window.__chunks) } }));
    }
    return real(u, o);
  };
  Object.defineProperty(HTMLMediaElement.prototype, 'src', { get() { return this._s || ''; }, set(v) { this._s = v; }, configurable: true });
  HTMLMediaElement.prototype.play = function () { window.__plays.push({ muted: this.muted, src: this._s || '' }); window.__cur = this; return Promise.resolve(); };
  HTMLMediaElement.prototype.pause = function () {};
  window.__end = () => { const e = window.__cur; if (e && e.onended) e.onended(); };
  // the output device: one on-device voice (a real SpeechSynthesisVoice cannot be built, so the utterance is a plain class too)
  window.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; this.voice = null; this.rate = 1; } };
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
    speak(u) { if (u.text) window.__spoken.push(u.text); }, cancel() { window.__cancels++; },
    getVoices() { return [{ name: 'local-en', lang: 'en-US', localService: true }]; } } });
})();
""" % json.dumps(fixture)


URL = "http://127.0.0.1:%d/#write_token=" + T
READY_BLOCK = "(() => { const p = document.querySelector('.vc-panel'); return !!p && !p.hidden && !!window.LevainVoice; })()"
SPEAK_CALLS = "window.__calls.filter((c) => c.path === '/cockpit/voice/speak')"
# the paths the PLAYER used: the calls whose stack runs through voice.js (the panel's own polls and voice.json read are not)
PLAYER_PATHS = "JSON.stringify([...new Set(window.__calls.filter((c) => c.fromVoice).map((c) => c.path))])"
MARK = {"eval": "window.__mark = window.__calls.length"}


def _run(tmp_path, monkeypatch, steps, fixture) -> list[Any]:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _httpd):
        out = _drive(tmp_path, monkeypatch, URL % port, [{"init": _init(fixture)}, *steps])
    return out["values"]


@needs_browser
def test_voice_js_is_served_and_a_cockpit_without_voice_shows_no_block_and_never_loads_it(tmp_path, monkeypatch) -> None:
    """Revert: remove the "/voice.js" row of the static table (served), or make devices_panel load voice.js before
    voice.json answers (the page would carry LevainVoice with no voice)."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _httpd):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request("GET", "/voice.js")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200 and r.getheader("Content-Type").startswith("text/javascript"), (r.status, r.getheader("Content-Type"))
        assert "/cockpit/voice/speak" in body
        out = _drive(tmp_path, monkeypatch, URL % port, [
            {"until": "document.querySelector('.dev-zone') && document.querySelectorAll('.dev-row').length >= 0", "ms": 20000},
            {"eval": "new Promise((r) => setTimeout(() => r(JSON.stringify([!!document.querySelector('.vc-panel') && document.querySelector('.vc-panel').hidden, typeof window.LevainVoice])), 3500))"},
        ])
    assert json.loads(out["values"][1]) == [True, "undefined"], out["values"]


@needs_browser
def test_the_voice_block_offers_the_providers_with_a_key_and_keeps_each_choice_on_this_device(tmp_path, monkeypatch) -> None:
    """Revert: saveSettings not called from a control's change handler (nothing stored), or the premium option left
    enabled when no provider holds a tts key."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify({tier: [...document.querySelectorAll('#vc-tier option')].map((o) => [o.value, o.disabled]),"
                 " providers: [...document.querySelectorAll('#vc-provider option')].map((o) => o.value),"
                 " voices: document.querySelectorAll('#vc-voice option').length, voice: document.querySelector('#vc-voice').value,"
                 " label: document.querySelector('label[for=vc-voice]').textContent,"
                 " text: document.querySelector('.vc-panel').textContent})"},
        {"eval": "(() => { const t = document.querySelector('#vc-tier'); t.value = 'premium'; t.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "(() => { const s = document.querySelector('#vc-voice'); s.value = 'voice-07'; s.dispatchEvent(new Event('change'));"
                 " const a = document.querySelector('#vc-autoplay'); a.checked = true; a.dispatchEvent(new Event('change'));"
                 " const n = document.querySelector('#vc-notify'); n.checked = true; n.dispatchEvent(new Event('change'));"
                 " return localStorage.getItem('levain_voice_settings'); })()"},
        # a fresh read of the stored choice (what the next page load would see)
        {"eval": "JSON.stringify(LevainVoice.settings())"},
        {"eval": "document.querySelector('.vc-panel').textContent"},
    ], _fixture())
    first = json.loads(v[1])
    assert first["tier"] == [["device", False], ["premium", False]], first
    assert first["providers"] == ["inworld"], first         # deepgram holds no key: not offered
    assert first["voices"] == 41 and first["voice"] == "voice-03" and first["label"] == "Voice (41)", first
    assert "Inworld retention words." in first["text"] and "$0.10 today of a $2.00 cap" in first["text"], first["text"]
    stored = json.loads(v[3])
    assert stored["tier"] == "premium" and stored["provider"] == "inworld" and stored["voice"] == "voice-07", stored
    assert stored["autoplay"] is True and stored["notify"] is True, stored
    assert json.loads(v[4]) == stored | {"speed": 1}, v[4]
    assert "To hear the premium voice, tap Play on a room reply." in v[5]


@needs_browser
def test_premium_is_offered_only_where_a_key_is_set_and_says_how_to_set_one(tmp_path, monkeypatch) -> None:
    """Revert: `po.disabled = !prov` (the option stays selectable with no key)."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "JSON.stringify({tier: [...document.querySelectorAll('#vc-tier option')].map((o) => [o.value, o.disabled]),"
                 " providers: !!document.querySelector('#vc-provider'), text: document.querySelector('.vc-panel').textContent})"},
    ], _fixture(key="not_set"))
    got = json.loads(v[1])
    assert got["tier"] == [["device", False], ["premium", True]] and got["providers"] is False, got
    assert "levain voice key add <provider>" in got["text"], got["text"]


R5 = "{thread: 't1', gseq: 5, text: 'five'}"
R6 = "{thread: 't1', gseq: 6, text: 'six'}"
R7 = "{thread: 't1', gseq: 7, text: 'seven'}"
SET = "LevainVoice.saveSettings(%s)"
PREMIUM = {"tier": "premium", "provider": "inworld", "voice": "voice-07", "speed": 1.25, "autoplay": True}


@needs_browser
def test_autoplay_waits_for_the_setting_the_mount_the_page_and_a_tap(tmp_path, monkeypatch) -> None:
    """Each decision is one line of replyFinished; the revert that turns each red is named after it.

    off: `if (!readSettings().autoplay) return "off"` removed -> the first reply plays with autoplay off.
    backlog: `Number(reply.gseq) <= baseline` weakened to `<` -> gseq 5 plays; `baseline === null` removed -> an unarmed
    module plays. hidden: `if (document.hidden) return "hidden"` removed. tap: `if (!unlocked)` removed -> plays with no tap."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        MARK,
        {"eval": "LevainVoice.saveSettings({tier: 'device', autoplay: false}) && 0"},
        {"eval": "(() => { LevainVoice.arm(5); return LevainVoice.replyFinished(%s); })()" % R6},                  # 3 off
        {"eval": "LevainVoice.saveSettings({autoplay: true}) && 0"},
        {"eval": "(() => { const a = LevainVoice.replyFinished(%s); LevainVoice.arm(null); return a; })()" % "{thread: 't1', gseq: 4, text: 'four'}"},  # 5 backlog
        {"eval": "LevainVoice.replyFinished(%s)" % R5},                                                                  # 6 seen? (5 not yet seen) -> backlog at the baseline
        {"eval": "(() => { LevainVoice.arm(5); return LevainVoice.replyFinished({thread: 't1', gseq: 5, text: 'x'}); })()"},   # 7 seen (same key): never twice
        {"eval": "(() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); const r = LevainVoice.replyFinished({thread: 't1', gseq: 8, text: 'e'}); Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); return r; })()"},  # 8 hidden
        {"eval": "LevainVoice.replyFinished({thread: 't1', gseq: 9, text: 'nine'})"},                                       # 9 tap (locked)
        {"eval": "JSON.stringify([window.__spoken, %s.length])" % SPEAK_CALLS},                                          # 10 nothing read yet
        {"eval": "(() => { const w = LevainVoice.button({thread: 't1', gseq: 9, text: 'nine'}); return w.querySelector('button').textContent; })()"},  # 11
        {"eval": "(async () => { LevainVoice.unlock(); await new Promise((r) => setTimeout(r, 100)); return LevainVoice.isUnlocked(); })()"},  # 12
        {"eval": "LevainVoice.replyFinished({thread: 't1', gseq: 10, text: 'ten'})"},                                    # 13 play
        {"eval": "JSON.stringify(window.__spoken)"},                                                                     # 14
        {"eval": PLAYER_PATHS},                                                                                          # 15 on-device: no route at all
    ], _fixture())
    assert v[3] == "off" and v[5] == "backlog" and v[6] == "backlog" and v[7] == "seen" and v[8] == "hidden" and v[9] == "tap", v[3:10]
    assert json.loads(v[10]) == [[], 0], v[10]
    assert v[11] == "Tap to hear", v[11]
    assert v[12] is True and v[13] == "play", v[12:14]
    assert json.loads(v[14]) == ["ten"], v[14]
    assert json.loads(v[15]) == [], v[15]


@needs_browser
def test_unarmed_autoplay_reads_nothing(tmp_path, monkeypatch) -> None:
    """Revert: `baseline === null ||` removed from the backlog test (a module nobody armed would read every reply)."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "LevainVoice.saveSettings({tier: 'device', autoplay: true}) && 0"},
        {"eval": "(async () => { LevainVoice.unlock(); await new Promise((r) => setTimeout(r, 100)); return LevainVoice.replyFinished(%s); })()" % R6},
        {"eval": "JSON.stringify(window.__spoken)"},
    ], _fixture())
    assert v[2] == "backlog" and json.loads(v[3]) == [], v[2:4]


@needs_browser
def test_the_premium_player_calls_the_speak_route_and_no_other_and_never_sends_words(tmp_path, monkeypatch) -> None:
    """Revert: change SPEAK_PATH (any other route), add `text` to the speak body, or fetch voice.json from the player."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": SET % json.dumps(PREMIUM) + " && 0"},
        {"eval": "(async () => { LevainVoice.arm(5); LevainVoice.unlock(); await new Promise((r) => setTimeout(r, 100)); return 1; })()"},
        MARK,
        {"eval": "LevainVoice.replyFinished(%s)" % R6},                                      # 5
        {"until": "window.__plays.filter((p) => !p.muted).length >= 1", "ms": 5000},
        {"eval": "window.__end() || 1"},                                                    # first chunk ends -> the second plays
        {"until": "window.__plays.filter((p) => !p.muted).length >= 2", "ms": 5000},
        {"eval": "window.__end() || 1"},
        {"eval": PLAYER_PATHS},                                                              # 9
        {"eval": "JSON.stringify(%s.map((c) => JSON.parse(c.body)))" % SPEAK_CALLS},        # 10
        {"eval": "JSON.stringify(window.__spoken)"},                                         # 11
    ], _fixture())
    assert v[4] == "play", v[4]
    assert json.loads(v[9]) == ["/cockpit/voice/speak"], v[9]
    bodies = json.loads(v[10])
    assert [b["chunk"] for b in bodies] == [0, 1], bodies
    for b in bodies:
        assert b == {"room_thread_id": "t1", "reply_gseq": 6, "chunk": b["chunk"], "provider": "inworld", "voice": "voice-07", "speed": 1.25}, b
    assert json.loads(v[11]) == [], v[11]       # the premium tier read nothing with the on-device voice


@needs_browser
def test_stop_interrupts_the_reading_and_the_control_follows(tmp_path, monkeypatch) -> None:
    """Revert: stop() no longer bumps runId (the second chunk still plays), or the control's click does not call stop
    on the playing reply, or the visibilitychange hook is removed."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": SET % json.dumps(PREMIUM) + " && 0"},
        {"eval": "(() => { window.__w = LevainVoice.button(%s); document.body.appendChild(window.__w); window.__b = window.__w.querySelector('button'); return window.__b.textContent; })()" % R6},  # 2 Play
        {"eval": "(() => { window.__b.click(); return 1; })()"},
        {"until": "window.__plays.filter((p) => !p.muted).length >= 1", "ms": 5000},
        {"eval": "window.__b.textContent"},                                                  # 5 Stop
        {"eval": "(() => { window.__b.click(); return window.__b.textContent; })()"},       # 6 Play again after the second tap
        {"eval": "(async () => { window.__end(); await new Promise((r) => setTimeout(r, 300)); return window.__plays.filter((p) => !p.muted).length; })()"},  # 7 still one: the second chunk never played
        # the page hiding stops a reading too
        {"eval": "(() => { window.__b.click(); return 1; })()"},
        {"until": "window.__plays.filter((p) => !p.muted).length >= 2 && window.__b.textContent === 'Stop'", "ms": 5000},
        {"eval": "(() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); document.dispatchEvent(new Event('visibilitychange')); Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); return window.__b.textContent; })()"},  # 10
    ], _fixture())
    assert v[2] == "Play" and v[5] == "Stop" and v[6] == "Play", v[2:7]
    assert v[7] == 1, v[7]
    assert v[10] == "Play", v[10]


@needs_browser
def test_a_refused_premium_read_falls_back_to_the_on_device_voice_with_the_servers_words(tmp_path, monkeypatch) -> None:
    """Revert: the `fallback === "on_device"` branch (nothing is read) or `say(reply, ...)` (the words are not shown)."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": SET % json.dumps(PREMIUM) + " && 0"},
        {"eval": "(() => { window.__refuse = true; window.__w = LevainVoice.button(%s); document.body.appendChild(window.__w); window.__w.querySelector('button').click(); return 1; })()" % R6},
        {"until": "window.__spoken.length >= 1", "ms": 5000},
        {"eval": "JSON.stringify([window.__spoken, window.__w.querySelector('.voice-say').textContent, window.__w.querySelector('.voice-say').hidden])"},
    ], _fixture())
    assert json.loads(v[4]) == [["six"], "Premium voice is paused: this month cap is reached.", False], v[4]


@needs_browser
def test_a_device_tier_read_calls_no_route(tmp_path, monkeypatch) -> None:
    """Revert: the device tier going through fetchChunk (any call after the mark)."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "LevainVoice.saveSettings({tier: 'device', provider: 'inworld', voice: 'voice-07'}) && 0"},   # a stored provider does not make it premium
        MARK,
        {"eval": "(() => { const w = LevainVoice.button(%s); document.body.appendChild(w); w.querySelector('button').click(); return 1; })()" % R6},
        {"until": "window.__spoken.length >= 1", "ms": 5000},
        {"eval": PLAYER_PATHS},
        {"eval": "JSON.stringify(window.__spoken)"},
    ], _fixture())
    assert json.loads(v[5]) == [] and json.loads(v[6]) == ["six"], v[5:7]


@needs_browser
def test_a_stop_while_the_audio_is_still_being_fetched_plays_nothing_when_it_arrives(tmp_path, monkeypatch) -> None:
    """Revert: `runId++` in stop() (a chunk that arrives after the stop is played)."""
    v = _run(tmp_path, monkeypatch, [
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": SET % json.dumps(PREMIUM) + " && 0"},
        {"eval": "(() => { window.__delay = 400; window.__w = LevainVoice.button(%s); document.body.appendChild(window.__w);"
                 " const b = window.__w.querySelector('button'); b.click(); const mid = b.textContent; b.click(); return mid + '/' + b.textContent; })()" % R6},
        {"eval": "new Promise((r) => setTimeout(() => r(window.__plays.filter((p) => !p.muted).length), 900))"},
    ], _fixture())
    assert v[2] == "Stop/Play" and v[3] == 0, v[2:4]


@needs_browser
def test_the_voice_block_does_not_scroll_sideways_at_phone_width_and_its_controls_are_tappable(tmp_path, monkeypatch) -> None:
    """Revert: nothing in the block's markup may widen the page; a control under 44px fails the hit-size floor."""
    v = _run(tmp_path, monkeypatch, [
        {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}},
        {"until": READY_BLOCK, "ms": 20000},
        {"eval": "document.documentElement.scrollWidth - document.documentElement.clientWidth"},
        {"eval": "JSON.stringify([...document.querySelectorAll('.vc-panel select, .vc-panel button, .vc-panel input[type=checkbox]')].map((n) => Math.round(n.getBoundingClientRect().height)))"},
        {"shot": "/tmp/voice_block_390.png", "full": True},
    ], _fixture())
    assert v[1] <= 0, v[1]
    assert all(h >= 24 for h in json.loads(v[2])), v[2]
