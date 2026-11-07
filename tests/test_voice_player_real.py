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
  ss.speak = (u) => { if (u.text) window.__spoken.push({ text: u.text, voice: u.voice ? u.voice.name : null }); setTimeout(() => u.onend && u.onend(), 20); };
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
def test_a_stored_provider_with_no_key_and_a_speed_out_of_range_are_replaced_by_what_the_block_shows(rig, tmp_path, monkeypatch) -> None:
    """Stored: openai (no key set here), voice nova, speed 1.9. The block shows inworld / Dennis / 1.5 and stores exactly that, so the
    player's POST is the one the real route accepts. Revert: paintVoice not persisting / the player not reading effective()."""
    r = _reply(rig)
    v, _ = _run(tmp_path, monkeypatch, rig, [
        {"until": READY, "ms": 30000},
        {"eval": "JSON.stringify({s: LevainVoice.settings(), shown: [document.querySelector('#vc-provider').value, document.querySelector('#vc-voice').value, document.querySelector('#vc-speed').value]})"},
        {"eval": "new Promise((res) => { LevainVoice.play(%s).then(() => res(1)); })" % json.dumps(r)},
        {"eval": "JSON.stringify(window.__posts.map((p) => p.body))"},
    ], pre="localStorage.setItem('levain_voice_settings', JSON.stringify({tier: 'premium', provider: 'openai', voice: 'nova', speed: 1.9}));", free=True)
    got = json.loads(v[1])
    assert got["shown"] == ["inworld", "Dennis", "1.5"], got
    assert (got["s"]["provider"], got["s"]["voice"], got["s"]["speed"]) == ("inworld", "Dennis", 1.5), got
    (post,) = json.loads(v[3])
    assert (post["provider"], post["voice"], post["speed"]) == ("inworld", "Dennis", 1.5), post
    assert len(rig.transport.seen) == 1


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
