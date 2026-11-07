"""Voice V1-5 round 2: the player and the Voice block, driven as they really behave.

Round 1's tests stubbed the media element, ``fetch`` and ``speechSynthesis``, so they could not see real browser behaviour
(an unlock cancelled by the stop() after it, a premium Play the server answers 400, a promise left waiting). These tests run
the REAL page (``levain serve`` over the room rig of ``tests/test_cockpit_voice_routes.py``: the real ``/cockpit/voice.json`` and
the real ``/cockpit/voice/speak`` validation, a fake provider transport that returns a real silent WAV) in headless Chrome
(``tests/chrome_flags.json`` plus ``--mute-audio``), with a REAL ``<audio>`` element whose volume is forced to 0: it is
never audible. Only the audio OUTPUT DEVICE is replaced, and only where the browser has none: ``speechSynthesis`` (headless
Chrome lists no voices, and a ``SpeechSynthesisVoice`` cannot be built, so the utterance class is plain too). The page's own
fetch, ``Audio``, blob URLs and trusted taps (``tap`` steps) are the browser's. ``fetch`` is wrapped only to RECORD calls, and,
in three named tests, to make the server fail the way a network does.

Each test names the revert that turns it red."""
from __future__ import annotations

import json
import struct
import subprocess
from typing import Any

import pytest

from levain.cockpit import voice as cockpit_voice
from tests.test_cockpit_k2a import TOKEN
from tests.test_cockpit_voice_routes import (  # noqa: F401  (fixtures: rig, qdir, the pinned clock)
    SOURCE, _clock_before_the_openai_cutoff, qdir, rig, served)
from tests.test_pair_card_w1b import _drive, needs_browser

pytestmark = pytest.mark.skipif(subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0,
                                reason="needs ssh-keygen")

FLAGS = ["--mute-audio", "--autoplay-policy=document-user-activation-required"]   # a page must be tapped before it plays
FREE = ["--mute-audio", "--autoplay-policy=no-user-gesture-required"]               # tests that are not about the tap


def wav(seconds: float) -> bytes:
    """A valid 8 kHz mono 8-bit silent WAV of the given length: what the fake provider 'speaks'."""
    n = int(8000 * seconds)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
            + b"data" + struct.pack("<I", n) + b"\x80" * n)


# Observation wrappers (the browser's own behaviour is kept) and the output-device stand-in for speechSynthesis.
INIT = """
(() => {
  %s
  window.__plays = []; window.__auds = []; window.__made = []; window.__revoked = []; window.__spoken = []; window.__posts = [];
  window.__voices = []; window.__events = [];
  const cu = URL.createObjectURL.bind(URL), ru = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (b) => { const u = cu(b); window.__made.push(u); return u; };
  URL.revokeObjectURL = (u) => { window.__revoked.push(u); return ru(u); };
  const NA = window.Audio;
  window.Audio = function () { const e = new NA(); e.volume = 0; window.__auds.push(e);
    ['ended', 'error', 'pause', 'playing'].forEach((n) => e.addEventListener(n, () => window.__events.push([n, Math.round(performance.now())])));
    return e; };
  const op = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    this.volume = 0; const silent = this.muted; window.__plays.push({ muted: this.muted });
    const p = op.call(this);
    // device latency on the unlock's silent play only: its promise settles late (a slow phone)
    if (window.__lagSilent && silent) return p.then((v) => new Promise((r) => setTimeout(() => r(v), window.__lagSilent)));
    return p;
  };
  const rf = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const path = new URL(String(u), location.href).pathname;
    if (path === '/cockpit/voice/speak') {
      let b = null; try { b = JSON.parse(o.body); } catch (_) {}
      window.__posts.push({ body: b, t: Math.round(performance.now()), ended: window.__events.filter((e) => e[0] === 'ended').length });
      if (window.__speakMode === 'html502') return Promise.resolve(new Response('<html>bad gateway</html>', { status: 502, headers: { 'Content-Type': 'text/html' } }));
      if (window.__speakMode === 'netfail') return Promise.reject(new TypeError('Failed to fetch'));
    }
    if (path === '/cockpit/voice.json' && window.__voiceJson) { const s = window.__voiceJson.shift(); if (s) return Promise.resolve(new Response('{}', { status: s })); }
    return rf(u, o);
  };
  class Utt { constructor(t) { this.text = t; this.voice = null; this.rate = 1; this.volume = 1; } }
  window.SpeechSynthesisUtterance = Utt;
  const ss = new EventTarget();
  ss.speak = (u) => {
    if (window.__speakThrow && u.text) { window.__speakThrow = false; throw new Error('speak refused'); }
    if (u.text) window.__spoken.push({ text: u.text, voice: u.voice ? u.voice.name : null });
    if (window.__speakHang) return;                      // an engine that never says "end" (and cancel() ends nothing)
    setTimeout(() => u.onend && u.onend(), 20); };
  ss.cancel = () => {};
  ss.getVoices = () => window.__voices;
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: window.__noSpeech ? undefined : ss });
})();
"""
URL = "http://127.0.0.1:%d/#write_token=" + TOKEN
READY = "(() => { const p = document.querySelector('.vc-panel'); return !!p && !p.hidden && !!window.LevainVoice; })()"
LOCAL = "{ name: 'local-en', lang: 'en-US', localService: true }"
NETWORK = "{ name: 'net-en', lang: 'en-US', localService: false }"
UNLOCK_BTN = ("(() => { const b = document.createElement('button'); b.id = 'tt'; b.textContent = 'tt';"
              " b.style.cssText = 'position:fixed;top:0;left:0;width:90px;height:50px;z-index:99999';"
              " b.onclick = () => { %s }; document.body.appendChild(b); return 1; })()")


def _run(tmp_path, monkeypatch, rig, steps, *, pre: str = "", seconds: float = 1.2, key: bool = True, free: bool = False) -> tuple[list[Any], Any]:
    if key:
        rig.add_key(provider="inworld", uses=("tts",), source=SOURCE + "-inworld")
    rig.transport.body = wav(seconds)
    with served(rig) as (port, _httpd):
        out = _drive(tmp_path, monkeypatch, URL % port, [{"init": INIT % pre}, *steps], flags=FREE if free else FLAGS)
    return out["values"], out


def _reply(rig, text: str = "Hello there.") -> dict[str, Any]:
    return {"thread": rig.w.thread, "gseq": rig.reply(text), "text": text}


def _timed_out(v: Any) -> bool:
    return isinstance(v, dict) and "timeout" in v


PREMIUM = "LevainVoice.saveSettings({tier: 'premium', provider: 'inworld', voice: 'Dennis', speed: 1}) && 0"


@needs_browser
def test_a_tap_that_unlocks_and_then_stops_still_unlocks(rig, tmp_path, monkeypatch) -> None:
    """The tap handlers of the room (Send, a mic tap, the sample button) call unlock() and then stop() in the same tap.
    Revert: stop() pausing the element that carries the unlock (round 1: the pause rejected the play(), `unlocked` stayed false)."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock(); LevainVoice.stop();"},
        {"eval": "LevainVoice.isUnlocked()"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000, "may_timeout": True},
    ])
    assert v[2] is False, v
    assert v[4] is True, ("a tap that unlocks and stops must leave the page unlocked", v[4])


@needs_browser
def test_a_play_tap_unlocks_and_the_unlock_never_pauses_the_premium_chunk_that_follows(rig, tmp_path, monkeypatch) -> None:
    """The unlock's silent play() settles late (a slow phone, forced here to 600 ms), after the premium chunk already owns the
    element. Revert: the unlock's `ok()` pausing the shared element unconditionally -> the chunk is paused, never ends, Play waits forever."""
    r = _reply(rig)
    v, out = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": PREMIUM},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"eval": "new Promise((res) => setTimeout(() => { window.__done = false; LevainVoice.play(%s).then(() => { window.__done = true; }); res(1); }, 150))" % json.dumps(r)},
        {"until": "window.__done === true", "ms": 15000, "may_timeout": True},
        {"eval": "JSON.stringify({unlocked: LevainVoice.isUnlocked(), ended: window.__events.filter((e) => e[0] === 'ended').length,"
                 " note: LevainVoice.noteFor(%s)})" % json.dumps(r)},
    ], seconds=1.2, pre="window.__lagSilent = 600;")
    assert not _timed_out(v[5]), ("the premium chunk must play to its end", v[5], v[6])
    got = json.loads(v[6])
    assert got["ended"] >= 1 and got["unlocked"] is True and got["note"] == "", got


@needs_browser
def test_selecting_premium_stores_the_shown_voice_and_the_real_route_accepts_the_first_play(rig, tmp_path, monkeypatch) -> None:
    """Fresh settings hold voice "". The block shows the provider default; Play must send it. The REAL /cockpit/voice/speak validation
    answers (a bad voice is a 400 before any provider call). Revert: the block persisting only tier+provider -> 400 bad_voice, no call."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { const t = document.querySelector('#vc-tier'); t.value = 'premium'; t.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "JSON.stringify(LevainVoice.settings())"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify({posts: window.__posts.map((p) => p.body), note: LevainVoice.noteFor(%s), shown: document.querySelector('#vc-voice').value})" % json.dumps(r)},
    ], free=True)
    stored = json.loads(v[2])
    assert stored["tier"] == "premium" and stored["provider"] == "inworld" and stored["voice"] == "Dennis", stored
    got = json.loads(v[4])
    assert got["shown"] == "Dennis" and got["posts"][0]["voice"] == "Dennis" and got["note"] == "", got
    assert len(rig.transport.seen) >= 1, "the real route must have reached the provider (a 400 never does)"


@needs_browser
def test_a_speed_out_of_range_is_clamped_to_what_the_block_shows_and_stores(rig, tmp_path, monkeypatch) -> None:
    """Stored: inworld, no voice, speed 1.9. The block shows Dennis / 1.5 and stores exactly that, so the player's POST is the one
    the real route accepts. Revert: paintVoice not persisting / the player not reading effective()."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "JSON.stringify({s: LevainVoice.settings(), shown: [document.querySelector('#vc-provider').value, document.querySelector('#vc-voice').value, document.querySelector('#vc-speed').value]})"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify(window.__posts.map((p) => p.body))"},
    ], pre="localStorage.setItem('levain_voice_settings', JSON.stringify({tier: 'premium', provider: 'inworld', voice: '', speed: 1.9}));", free=True)
    got = json.loads(v[1])
    assert got["shown"] == ["inworld", "Dennis", "1.5"], got
    assert (got["s"]["provider"], got["s"]["voice"], got["s"]["speed"]) == ("inworld", "Dennis", 1.5), got
    (post,) = json.loads(v[3])
    assert (post["provider"], post["voice"], post["speed"]) == ("inworld", "Dennis", 1.5), post
    assert len(rig.transport.seen) == 1


@needs_browser
def test_a_revoked_provider_is_never_swapped_for_another_vendor(rig, tmp_path, monkeypatch) -> None:
    """Stored: premium / openai (its key is revoked: only inworld holds a key). Play must send NOTHING to inworld, the reply says
    which key is missing, the block says the same and stores nothing new; choosing a provider in the block is what un-blocks it.
    Revert: effective() falling back to offered[0] (the words went to a vendor he never chose, and the swap was stored)."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify({posts: window.__posts.length, note: LevainVoice.noteFor(%s), s: LevainVoice.settings(),"
                 " block: document.querySelector('.vc-blocked') && document.querySelector('.vc-blocked').textContent,"
                 " tier: document.querySelector('#vc-tier').value, prov: document.querySelector('#vc-provider').value})" % json.dumps(r)},
        {"eval": "(() => { const p = document.querySelector('#vc-provider'); p.value = 'inworld'; p.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify({posts: window.__posts.map((p) => p.body.provider), block: !!document.querySelector('.vc-blocked')})"},
    ], pre="localStorage.setItem('levain_voice_settings', JSON.stringify({tier: 'premium', provider: 'openai', voice: 'nova', speed: 1}));", free=True)
    got = json.loads(v[2])
    assert got["posts"] == 0 and len(rig.transport.seen) == 1, got      # the one provider call is the explicit choice below
    assert "openai" in got["note"] and "Settings" in got["note"], got
    assert got["block"] == got["note"] and got["s"]["provider"] == "openai" and got["tier"] == "premium" and got["prov"] == "", got
    after = json.loads(v[5])
    assert after == {"posts": ["inworld"], "block": False}, after


@needs_browser
def test_a_throw_inside_a_reading_clears_playing_and_the_next_reply_still_autoplays(rig, tmp_path, monkeypatch) -> None:
    """speechSynthesis.speak() throws for the first reply. Revert: the `try/finally` in play() -> `playing` stays set, the next
    reply is `queued` behind a reading that never ends, and nothing is ever read again."""
    a, b = {"thread": "t", "gseq": 5, "text": "first"}, {"thread": "t", "gseq": 6, "text": "second"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(async () => { window.__voices = [%s]; window.__speakThrow = true; LevainVoice.saveSettings({tier: 'device', autoplay: true}); LevainVoice.arm(0);"
                 " const first = LevainVoice.replyFinished(%s); await new Promise((x) => setTimeout(x, 200));"
                 " const second = LevainVoice.replyFinished(%s); await new Promise((x) => setTimeout(x, 300));"
                 " return JSON.stringify({first, second, spoken: window.__spoken.map((x) => x.text), note: LevainVoice.noteFor(%s)}); })()"
                 % (LOCAL, json.dumps(a), json.dumps(b), json.dumps(a))},
    ], key=False)
    got = json.loads(v[4])
    assert got["first"] == "play" and got["second"] == "play" and got["spoken"] == ["second"], got
    assert got["note"], "the reply that threw says so"


@needs_browser
def test_a_queued_replys_waiting_label_clears_on_stop_on_autoplay_off_and_when_it_starts(rig, tmp_path, monkeypatch) -> None:
    """A is read, B waits ("Waiting"); stop() drops B and B's label clears (a second pair, C playing and D waiting, stopped). Revert: stop() setting `queued = null` without deleting its message."""
    a, b = {"thread": "t", "gseq": 5, "text": "first"}, {"thread": "t", "gseq": 6, "text": "second"}
    c, d = {"thread": "t", "gseq": 7, "text": "third"}, {"thread": "t", "gseq": 8, "text": "fourth"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(async () => { window.__voices = [%s]; window.__speakHang = true; LevainVoice.saveSettings({tier: 'device', autoplay: true}); LevainVoice.arm(0);"
                 " LevainVoice.replyFinished(%s); const q = LevainVoice.replyFinished(%s); const waiting = LevainVoice.noteFor(%s);"
                 " LevainVoice.stop(); const afterStop = LevainVoice.noteFor(%s);"
                 " LevainVoice.replyFinished(%s); LevainVoice.replyFinished(%s); LevainVoice.saveSettings({autoplay: false});"
                 " window.__speakHang = false; const w = LevainVoice.noteFor(%s); LevainVoice.stop();"
                 " return JSON.stringify({q, waiting, afterStop, w, afterOff: LevainVoice.noteFor(%s), spoken: window.__spoken.map((x) => x.text)}); })()"
                 % (LOCAL, json.dumps(a), json.dumps(b), json.dumps(b), json.dumps(b), json.dumps(c), json.dumps(d), json.dumps(d), json.dumps(d))},
    ], key=False)
    got = json.loads(v[4])
    assert got["q"] == "queued" and "Waiting" in got["waiting"] and got["afterStop"] == "", got
    assert "Waiting" in got["w"] and got["afterOff"] == "", got


@needs_browser
def test_a_queued_reply_is_not_started_when_autoplay_went_off_while_it_waited(rig, tmp_path, monkeypatch) -> None:
    """Revert: the drain checking only hidden/unlocked (B still started after the operator turned autoplay off)."""
    a, b = {"thread": "t", "gseq": 5, "text": "first"}, {"thread": "t", "gseq": 6, "text": "second"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(async () => { window.__voices = [%s]; LevainVoice.saveSettings({tier: 'device', autoplay: true}); LevainVoice.arm(0);"
                 " LevainVoice.replyFinished(%s); LevainVoice.replyFinished(%s); LevainVoice.saveSettings({autoplay: false});"
                 " await new Promise((x) => setTimeout(x, 400));"
                 " return JSON.stringify({spoken: window.__spoken.map((x) => x.text), note: LevainVoice.noteFor(%s)}); })()"
                 % (LOCAL, json.dumps(a), json.dumps(b), json.dumps(b))},
    ], key=False)
    got = json.loads(v[4])
    assert got["spoken"] == ["first"] and got["note"] == "", got


@needs_browser
def test_stop_settles_an_on_device_reading_and_the_utterance_is_held_until_it_ends(rig, tmp_path, monkeypatch) -> None:
    """The engine never says "end" and cancel() ends nothing (the WebKit habit). a) play() resolves after stop(); b) the page holds the
    utterance while it speaks and lets go at stop. Revert: speakDevice waiting on onend alone (play() never resolves) / `liveUtt` unset."""
    r = {"thread": "t", "gseq": 1, "text": "read me"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { window.__voices = [%s]; window.__speakHang = true; window.__done = false;"
                 " LevainVoice.play(%s).then(() => { window.__done = true; }); return 1; })()" % (LOCAL, json.dumps(r))},
        {"until": "window.__spoken.length === 1", "ms": 5000},
        {"eval": "LevainVoice.held()"},
        {"eval": "LevainVoice.stop() || 1"},
        {"until": "window.__done === true", "ms": 3000, "may_timeout": True},
        {"eval": "LevainVoice.held()"},
    ], key=False)
    assert v[3] is True and not _timed_out(v[5]) and v[6] is False, v


@needs_browser
def test_an_on_device_reading_whose_engine_never_ends_is_released_by_the_watchdog(rig, tmp_path, monkeypatch) -> None:
    """Revert: no watchdog in speakDevice -> play() never resolves and the queue behind it never moves."""
    r = {"thread": "t", "gseq": 1, "text": "hi"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "LevainVoice.speechBound('hi', 1)"},
        {"eval": "(() => { window.__voices = [%s]; window.__speakHang = true; window.__done = false;"
                 " LevainVoice.play(%s).then(() => { window.__done = true; }); return 1; })()" % (LOCAL, json.dumps(r))},
        {"until": "window.__done === true", "ms": 9000, "may_timeout": True},
        {"eval": "LevainVoice.held()"},
    ], key=False)
    assert v[1] <= 10000 and not _timed_out(v[3]) and v[4] is False, v


@needs_browser
def test_replies_past_the_cap_are_never_re_offered_by_a_repeated_poll(rig, tmp_path, monkeypatch) -> None:
    """250 distinct replies arrive after arm(); a later poll repeats the first 50 (a page polls the same window again). Revert:
    FIFO-evicting `seen` with no high-water mark -> the evicted 50 are decided again ("tap"/"play"), so they are read again and,
    on the premium tier, billed again. A reply above every mark is still new."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { LevainVoice.saveSettings({tier: 'device', autoplay: true}); LevainVoice.arm(0);"
                 " const first = []; for (let g = 1; g <= 250; g++) first.push(LevainVoice.replyFinished({thread: 't', gseq: g, text: 'x'}));"
                 " const again = []; for (let g = 1; g <= 50; g++) again.push(LevainVoice.replyFinished({thread: 't', gseq: g, text: 'x'}));"
                 " const fresh = LevainVoice.replyFinished({thread: 't', gseq: 251, text: 'x'}), other = LevainVoice.replyFinished({thread: 'u', gseq: 1, text: 'x'});"
                 " return JSON.stringify({first: first.filter((d) => d !== 'tap').length, again: again.filter((d) => d !== 'seen').length, fresh, other, sizes: LevainVoice.sizes()}); })()"},
    ], key=False)
    got = json.loads(v[1])
    assert got["first"] == 0 and got["again"] == 0 and got["fresh"] == "tap" and got["other"] == "tap", got
    assert got["sizes"]["seen"] <= 200 and got["sizes"]["floor"] <= 200, got


@needs_browser
def test_a_reply_that_finishes_while_the_page_is_hidden_asks_for_a_tap_when_the_poll_offers_it_again(rig, tmp_path, monkeypatch) -> None:
    """Revert: remembering the reply as decided before the hidden check -> it is never offered a tap."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { LevainVoice.saveSettings({autoplay: true}); LevainVoice.arm(0); const r = {thread: 't', gseq: 9, text: 'x'};"
                 " Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); const a = LevainVoice.replyFinished(r);"
                 " Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); const b = LevainVoice.replyFinished(r);"
                 " return JSON.stringify([a, b, LevainVoice.replyFinished(r)]); })()"},
    ], key=False)
    assert json.loads(v[1]) == ["hidden", "tap", "seen"], v[1]


@needs_browser
def test_the_on_device_tier_speaks_only_with_a_local_voice_and_waits_for_the_list(rig, tmp_path, monkeypatch) -> None:
    """Revert: speak() called with no chosen voice / `localService` not required (the browser default may be network-backed).
    a) only a network voice listed: nothing is spoken and the reply says so. b) no voices yet, then a local one arrives with
    voiceschanged inside the wait: that voice speaks. c) both listed: the local one."""
    r = {"thread": "t1", "gseq": 1, "text": "read me"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(async () => { window.__voices = [%s]; const w = LevainVoice.play(%s); await w;"
                 " return JSON.stringify({spoken: window.__spoken, note: LevainVoice.noteFor(%s)}); })()" % (NETWORK, json.dumps(r), json.dumps(r))},
        {"eval": "(async () => { window.__voices = []; window.__spoken.length = 0; const w = LevainVoice.play(%s);"
                 " await new Promise((x) => setTimeout(x, 300)); window.__voices = [%s]; speechSynthesis.dispatchEvent(new Event('voiceschanged')); await w;"
                 " return JSON.stringify(window.__spoken); })()" % (json.dumps(r), LOCAL)},
        {"eval": "(async () => { window.__voices = [%s, %s]; window.__spoken.length = 0; await LevainVoice.play(%s); return JSON.stringify(window.__spoken); })()" % (NETWORK, LOCAL, json.dumps(r))},
    ], key=False)
    a = json.loads(v[1])
    assert a["spoken"] == [] and "no on-device voice" in a["note"].lower(), a
    assert json.loads(v[2]) == [{"text": "read me", "voice": "local-en"}], v[2]
    assert json.loads(v[3]) == [{"text": "read me", "voice": "local-en"}], v[3]


@needs_browser
def test_stop_during_premium_playback_settles_the_reading_and_revokes_every_blob_url(rig, tmp_path, monkeypatch) -> None:
    """Revert: stop() clearing onended without settling the chunk -> play() never resolves; and the URL left to a later stop()."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": PREMIUM},
        {"eval": "(() => { window.__pr = false; LevainVoice.play(%s).then(() => { window.__pr = true; }); return 1; })()" % json.dumps(r)},
        {"until": "window.__events.some((e) => e[0] === 'playing')", "ms": 15000},
        {"eval": "LevainVoice.stop() || 1"},
        {"until": "window.__pr === true", "ms": 3000, "may_timeout": True},
        {"eval": "JSON.stringify({unrevoked: window.__made.filter((u) => window.__revoked.indexOf(u) < 0).length})"},
    ], seconds=4, free=True)
    assert not _timed_out(v[5]), "play() must resolve once stop() ran"
    assert json.loads(v[6]) == {"unrevoked": 0}, v[6]


@needs_browser
def test_a_chunk_that_ends_naturally_releases_its_blob_url(rig, tmp_path, monkeypatch) -> None:
    """Revert: revoking only in stop() -> every played chunk's URL stays until the next stop."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": PREMIUM},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify({made: window.__made.length, unrevoked: window.__made.filter((u) => window.__revoked.indexOf(u) < 0).length})"},
    ], seconds=0.6, free=True)
    got = json.loads(v[3])
    assert got["made"] >= 1 and got["unrevoked"] == 0, got


@needs_browser
def test_the_remembered_maps_stay_bounded(rig, tmp_path, monkeypatch) -> None:
    """Revert: the cap in remember() removed -> seen, tapNeeded and messages grow with every reply of a long session."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(async () => { LevainVoice.saveSettings({tier: 'device', autoplay: true}); LevainVoice.arm(0);"
                 " for (let g = 1; g <= 500; g++) LevainVoice.replyFinished({thread: 't', gseq: g, text: 'x'});"
                 " for (let g = 1; g <= 400; g++) await LevainVoice.play({thread: 't', gseq: g, text: 'x'});"
                 " return JSON.stringify(LevainVoice.sizes()); })()"},
    ], key=False, pre="window.__noSpeech = true;")
    sizes = json.loads(v[1])
    assert all(n <= 200 for n in sizes.values()) and sizes["seen"] == 200 and sizes["messages"] == 200, sizes


@needs_browser
def test_a_failed_premium_call_never_claims_a_voice_that_did_not_read(rig, tmp_path, monkeypatch) -> None:
    """A gateway page that is not JSON carries no fallback marker: nothing is read on-device, so the words must not say it is.
    A network failure DOES fall back, and then the words may say so and the device voice reads. Revert: the one WORDS.failed sentence
    ("reading uses the on-device voice") on both paths."""
    r = {"thread": rig.w.thread, "gseq": 5, "text": "read me"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": PREMIUM},
        {"eval": "(async () => { window.__voices = [%s]; window.__speakMode = 'html502'; await LevainVoice.play(%s);"
                 " return JSON.stringify({note: LevainVoice.noteFor(%s), spoken: window.__spoken.length}); })()" % (LOCAL, json.dumps(r), json.dumps(r))},
        {"eval": "(async () => { window.__speakMode = 'netfail'; window.__spoken.length = 0; await LevainVoice.play(%s);"
                 " return JSON.stringify({note: LevainVoice.noteFor(%s), spoken: window.__spoken.length}); })()" % (json.dumps(r), json.dumps(r))},
    ], free=True)
    html, net = json.loads(v[2]), json.loads(v[3])
    assert html["spoken"] == 0 and "on-device" not in html["note"] and html["note"], html
    assert net["spoken"] == 1 and "on-device" in net["note"], net


@needs_browser
def test_the_voice_block_asks_again_after_a_403_before_the_token(rig, tmp_path, monkeypatch) -> None:
    """Revert: voiceProbe() running once. The first voice.json answer is a 403 (the page had not stored its token): the block
    appears on the later look."""
    v, _ = _run(tmp_path, monkeypatch, rig, [{"until": READY, "ms": 20000, "may_timeout": True}], pre="window.__voiceJson = [403];")
    assert v[0] is True, ("the block must appear after a 403 first answer", v)


@needs_browser
def test_the_voice_block_stops_asking_at_a_404(rig, tmp_path, monkeypatch) -> None:
    """Revert: the 404 treated as retryable (every focus asks again). A cockpit with no voice is asked once."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"eval": "new Promise((res) => setTimeout(() => { window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('focus')); setTimeout(res, 3500); }, 3000))"},
        {"eval": "JSON.stringify({left: window.__voiceJson.length, hidden: document.querySelector('.vc-panel').hidden})"},
    ], key=False, pre="window.__voiceJson = [404, 404, 404, 404, 404];")
    got = json.loads(v[1])
    assert got["left"] == 4 and got["hidden"] is True, got


@needs_browser
def test_a_hidden_page_locks_so_the_first_reply_after_returning_asks_for_a_tap(rig, tmp_path, monkeypatch) -> None:
    """Revert: lock() not clearing `unlocked` on pagehide / a hidden visibilitychange."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(() => { window.dispatchEvent(new Event('pagehide')); return LevainVoice.isUnlocked(); })()"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); document.dispatchEvent(new Event('visibilitychange'));"
                 " Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); return LevainVoice.isUnlocked(); })()"},
        {"eval": "(() => { LevainVoice.saveSettings({autoplay: true}); LevainVoice.arm(0); return LevainVoice.replyFinished({thread: 't', gseq: 9, text: 'x'}); })()"},
    ], key=False)
    assert v[4] is False and v[7] is False and v[8] == "tap", v


@needs_browser
def test_two_replies_finished_in_one_poll_are_read_one_after_the_other(rig, tmp_path, monkeypatch) -> None:
    """Revert: the `playing` -> queue branch of replyFinished (the second play() stops the first mid-way: it never ends)."""
    a, b = _reply(rig, "First reply."), _reply(rig, "Second reply.")
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": PREMIUM},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(() => { LevainVoice.saveSettings({autoplay: true}); LevainVoice.arm(0);"
                 " return JSON.stringify([LevainVoice.replyFinished(%s), LevainVoice.replyFinished(%s), LevainVoice.noteFor(%s)]); })()" % (json.dumps(a), json.dumps(b), json.dumps(b))},
        {"until": "window.__posts.length >= 2 && window.__events.filter((e) => e[0] === 'ended').length >= 2", "ms": 20000, "may_timeout": True},
        {"eval": "JSON.stringify(window.__posts.map((p) => [p.body.reply_gseq, p.ended]))"},
    ], seconds=1.0)
    decisions = json.loads(v[5])
    assert decisions[:2] == ["play", "queued"] and "Waiting" in decisions[2], decisions
    assert not _timed_out(v[6]), ("both replies must play to their end", v[6])
    posts = json.loads(v[7])
    assert [p[0] for p in posts[:2]] == [a["gseq"], b["gseq"]], posts
    assert posts[1][1] >= 1, ("the second reply must be asked for only after the first one ended", posts)


@needs_browser
def test_a_sample_started_over_a_reply_settles_the_reply_and_neither_strands_the_other(rig, tmp_path, monkeypatch) -> None:
    """L3 r3 (complement): reply A is read on-device by an engine that never says "end"; the operator taps "Hear the on-device voice".
    A must settle at once (its play() resolves, `playing` clears, the queue is free), the sample must hold the one utterance slot,
    and a Stop must then settle the sample too. The sample goes through the same stop() path a reply does.
    Revert: sampleDevice() without its stop() -> the sample overwrites A's settler, A waits for its watchdog (up to 10 minutes)."""
    r = {"thread": "t", "gseq": 1, "text": "read me a long one"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { window.__voices = [%s]; window.__speakHang = true; window.__done = false; window.__sdone = false;"
                 " LevainVoice.play(%s).then(() => { window.__done = true; }); return 1; })()" % (LOCAL, json.dumps(r))},
        {"until": "window.__spoken.length === 1", "ms": 5000},
        {"eval": "(() => { LevainVoice.sampleDevice().then(() => { window.__sdone = true; }); return 1; })()"},
        {"until": "window.__done === true", "ms": 3000, "may_timeout": True},
        {"until": "window.__spoken.length === 2", "ms": 3000},
        {"eval": "JSON.stringify({held: LevainVoice.held(), sdone: window.__sdone})"},
        {"eval": "LevainVoice.stop() || 1"},
        {"until": "window.__sdone === true", "ms": 3000, "may_timeout": True},
        {"eval": "LevainVoice.held()"},
    ], key=False)
    assert not _timed_out(v[4]) and not _timed_out(v[5]), ("the reply must settle when the sample starts", v)
    assert json.loads(v[6]) == {"held": True, "sdone": False}, v
    assert not _timed_out(v[8]) and v[9] is False, v


@needs_browser
def test_a_sample_whose_engine_throws_says_so_and_does_not_reject(rig, tmp_path, monkeypatch) -> None:
    """L3 r3 (gemini): speak() throwing inside the sample must not leave an unhandled rejection or a silent button.
    Revert: sampleDevice() with no try/catch around speakDevice()."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(async () => { window.__voices = [%s]; window.__speakThrow = true;"
                 " try { await LevainVoice.sampleDevice(); } catch (e) { return 'rejected ' + e.message; }"
                 " return LevainVoice.noteFor({thread: 'sample', gseq: 0}); })()" % LOCAL},
    ], key=False)
    assert v[1] == "This browser could not play the audio.", v
    assert not str(v[1]).startswith("rejected"), v


@needs_browser
def test_premium_with_two_vendors_picks_none_for_him(rig, tmp_path, monkeypatch) -> None:
    """L3 r3 (complement): two providers hold a key. One tap on "Premium voice" must not choose a vendor: nothing is stored, nothing is
    sent, the reply and the block both say to choose, and the explicit choice then reads. With exactly one key the tap still
    pre-selects it (the test above). Revert: the tier handler storing `ready[0]` / effective() falling to offered[0]."""
    r = _reply(rig)
    rig.add_key(provider="openai", uses=("tts",), source=SOURCE + "-openai")
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { const t = document.querySelector('#vc-tier'); t.value = 'premium'; t.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify({s: LevainVoice.settings(), posts: window.__posts.length, note: LevainVoice.noteFor(%s),"
                 " tier: document.querySelector('#vc-tier').value, prov: document.querySelector('#vc-provider').value,"
                 " opts: Array.from(document.querySelector('#vc-provider').options).map((o) => o.value)})" % json.dumps(r)},
        {"eval": "(() => { const p = document.querySelector('#vc-provider'); p.value = 'inworld'; p.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify(window.__posts.map((p) => p.body.provider))"},
    ], free=True)
    got = json.loads(v[3])
    assert got["s"]["tier"] == "premium" and not got["s"]["provider"] and got["posts"] == 0, got
    assert "provider" in got["note"].lower() and got["tier"] == "premium" and got["prov"] == "" and got["opts"][0] == "", got
    assert json.loads(v[6]) == ["inworld"], v


@needs_browser
def test_a_watermark_for_a_thread_survives_four_hundred_other_threads(rig, tmp_path, monkeypatch) -> None:
    """L3 r3 (codex MED): `floor` was itself capped at 200 threads; after 401 threads' decisions the first thread's mark was gone and
    a poll that repeated its old reply read (and billed) it again. Revert: floor capped at CAP."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "(() => { LevainVoice.saveSettings({tier: 'device', autoplay: true}); LevainVoice.arm(0);"
                 " for (let i = 0; i <= 400; i++) LevainVoice.replyFinished({thread: 'th' + i, gseq: 1, text: 'x'});"
                 " return JSON.stringify({again: LevainVoice.replyFinished({thread: 'th0', gseq: 1, text: 'x'}), sizes: LevainVoice.sizes()}); })()"},
    ], key=False)
    got = json.loads(v[1])
    assert got["again"] == "seen" and got["sizes"]["seen"] <= 200, got


@needs_browser
def test_a_reply_waiting_in_the_queue_when_the_page_hides_asks_for_a_tap(rig, tmp_path, monkeypatch) -> None:
    """L3 r3 (complement, gemini, deepseek): A is read, B waits behind it, the page hides. B was decided ("seen") so the poll never
    offers it again: it must carry a "Tap to hear" cue. Revert: lock() calling stop() with the queue not moved to tapNeeded."""
    a = {"thread": "t", "gseq": 1, "text": "first"}
    b = {"thread": "t", "gseq": 2, "text": "second"}
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": UNLOCK_BTN % "LevainVoice.unlock();"},
        {"tap": "document.querySelector('#tt')", "ms": 5000},
        {"until": "LevainVoice.isUnlocked()", "ms": 5000},
        {"eval": "(() => { window.__voices = [%s]; window.__speakHang = true; LevainVoice.saveSettings({tier: 'device', autoplay: true});"
                 " LevainVoice.arm(0); return JSON.stringify([LevainVoice.replyFinished(%s), LevainVoice.replyFinished(%s)]); })()" % (LOCAL, json.dumps(a), json.dumps(b))},
        {"eval": "(() => { window.dispatchEvent(new Event('pagehide')); const c = LevainVoice.button(%s);"
                 " return JSON.stringify({text: c.querySelector('button').textContent, again: LevainVoice.replyFinished(%s)}); })()" % (json.dumps(b), json.dumps(b))},
    ], key=False)
    assert json.loads(v[4]) == ["play", "queued"], v
    got = json.loads(v[5])
    assert got["text"] == "Tap to hear" and got["again"] == "seen", got


STORED_GONE = "localStorage.setItem('levain_voice_settings', JSON.stringify({tier: '%s', provider: 'openai', voice: 'nova', speed: 1}));"
TIER_FLIP = "(() => { const t = document.querySelector('#vc-tier'); t.value = '%s'; t.dispatchEvent(new Event('change')); return 1; })()"
SNAP = ("JSON.stringify({s: LevainVoice.settings(), tier: document.querySelector('#vc-tier').value,"
        " block: (document.querySelector('.vc-blocked') || {}).textContent || '',"
        " opts: document.querySelector('#vc-provider') ? Array.from(document.querySelector('#vc-provider').options).map((o) => o.value) : null,"
        " prov: document.querySelector('#vc-provider') ? document.querySelector('#vc-provider').value : null,"
        " voiceRow: !!document.querySelector('#vc-voice'), posts: window.__posts.length})")


@needs_browser
def test_a_tier_flip_never_swaps_a_revoked_stored_provider_for_the_one_remaining_vendor(rig, tmp_path, monkeypatch) -> None:
    """L3 r4 (codex MED): stored openai (key revoked), inworld the only live key. Premium -> On-device -> Premium must leave the
    stored provider openai, show 'Your openai voice key is not available; choose a voice in Settings' with an EMPTY chooser,
    send nothing to inworld, and only his explicit choice un-blocks it.
    Revert: the tier handler's `ready.length === 1 ? ready[0].name` / paintVoice's `ready.length === 1 ? ready[0]` ignoring a stored provider."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": TIER_FLIP % "device"},
        {"eval": SNAP},
        {"eval": TIER_FLIP % "premium"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": SNAP},
        {"eval": "(() => { const p = document.querySelector('#vc-provider'); p.value = 'inworld'; p.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify(window.__posts.map((p) => p.body.provider))"},
    ], pre=STORED_GONE % "premium", free=True)
    dev, back = json.loads(v[2]), json.loads(v[5])
    for got in (dev, back):
        assert got["s"]["provider"] == "openai" and "openai" in got["block"] and "Settings" in got["block"], got
        assert got["opts"] and got["opts"][0] == "" and got["prov"] == "" and not got["voiceRow"] and got["posts"] == 0, got
    assert dev["tier"] == "device" and back["tier"] == "premium" and back["s"]["tier"] == "premium", (dev, back)
    assert json.loads(v[8]) == ["inworld"], v


@needs_browser
def test_a_device_tier_block_with_a_revoked_stored_provider_shows_no_vendor_picked_for_him(rig, tmp_path, monkeypatch) -> None:
    """Stored tier device + provider openai (revoked), inworld the only live key: the block says the key is gone with an empty
    chooser and no inworld provider/voice rows; nothing is stored. Revert: paintVoice's `prov` falling to the sole ready vendor."""
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": SNAP},
    ], pre=STORED_GONE % "device", free=True)
    got = json.loads(v[1])
    assert got["s"]["provider"] == "openai" and got["s"]["tier"] == "device" and "openai" in got["block"], got
    assert got["opts"] and got["opts"][0] == "" and got["prov"] == "" and not got["voiceRow"], got


@needs_browser
def test_with_nothing_stored_the_sole_live_vendor_is_still_pre_selected_and_a_device_read_sends_nothing(rig, tmp_path, monkeypatch) -> None:
    """The other half of the rule: nothing stored and exactly one live key -> Premium pre-selects it (stored). The sample button and an
    on-device read never POST to the speak route, whatever is stored."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": TIER_FLIP % "premium"},
        {"eval": "JSON.stringify(LevainVoice.settings())"},
        {"eval": "(() => { const t = document.querySelector('#vc-tier'); t.value = 'device'; t.dispatchEvent(new Event('change')); return 1; })()"},
        {"eval": "new Promise((res) => { document.querySelector('.vc-hear').click(); setTimeout(() => res(1), 300); })"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "window.__posts.length"},
    ], free=True)
    assert json.loads(v[2])["provider"] == "inworld", v[2]
    assert v[6] == 0, v


@needs_browser
def test_the_sample_and_an_on_device_read_send_nothing_when_the_stored_provider_is_revoked(rig, tmp_path, monkeypatch) -> None:
    """The sample button and the player's device path never POST to the speak route, whatever provider is stored."""
    r = _reply(rig)
    v2, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "new Promise((res) => { document.querySelector('.vc-hear').click(); setTimeout(() => res(1), 300); })"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "window.__posts.length"},
    ], pre=STORED_GONE % "device", free=True)
    assert v2[3] == 0, v2
