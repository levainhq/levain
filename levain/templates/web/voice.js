// levain voice.js — reading the room aloud: the settings, the player, the per-reply control and the autoplay rule
// (voice V1-5; design project_memory/1104-voice/V1_DESIGN.md section 4).
//
// The player asks ONE route for audio: POST /cockpit/voice/speak {room_thread_id, reply_gseq, chunk, provider, voice,
// speed}. The page never sends words to speak; the server rebuilds the reply from the room record. The on-device tier
// (speechSynthesis) calls no route at all. This file calls no other route: the settings block (devices_panel.js) is the
// one reader of GET /cockpit/voice.json.
//
// Integration surface (the room view, a later slice; this file is loadable on its own and hooks nothing by itself):
//   LevainVoice.arm(maxFinishedGseq)       once at mount: replies finished at or before it are backlog, never autoplayed
//   LevainVoice.unlock()                   inside the operator's own tap (Send, a mic tap, a Play tap)
//   LevainVoice.replyFinished(reply)       when a reply's vendor_turn_ended arrives on a poll
//   LevainVoice.button(reply)              the speaker control for a finished reply (draw it at the reply's foot)
//   LevainVoice.stop()                     on Send, a mic tap, a new conversation, unmount (a hidden page stops itself)
//   reply = { thread, gseq, text }  thread = the room_thread_id, gseq = the gseq of the reply's vendor_turn_ended,
//   text = the reply as drawn (read only by the on-device voice; the premium route never receives it)
// Everything is drawn with textContent. Browser storage is wrapped in try/catch: the page works without it.
(function () {
  "use strict";
  const SETTINGS_KEY = "levain_voice_settings";
  const TOKEN_KEY = "levain_write_token";
  const TOKEN_HEADER = "X-Levain-Write-Token";
    const SPEAK_PATH = "/cockpit/voice/speak";
  const SAMPLE = "This is how your room will sound.";
  const WORDS = {
    play: "Play", stop: "Stop", tap: "Tap to hear",
    busy: "Voice is busy; try again in a moment.",
    failed: "Premium voice failed; reading uses the on-device voice.",
    midway: "Premium voice stopped partway; the rest is not read aloud.",
    cantPlay: "This browser could not play the audio.",
    noSpeech: "This browser has no on-device voice.",
  };
  const DEFAULTS = { tier: "device", provider: "", voice: "", speed: 1, autoplay: false, notify: false };

  // ---- per-device settings -------------------------------------------------------------------------------------------
  function readSettings() {
    let raw = null;
    try { raw = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || "null"); } catch (_) { raw = null; }
    const s = Object.assign({}, DEFAULTS);
    if (!raw || typeof raw !== "object") return s;
    if (raw.tier === "device" || raw.tier === "premium") s.tier = raw.tier;
    if (typeof raw.provider === "string") s.provider = raw.provider.slice(0, 40);
    if (typeof raw.voice === "string") s.voice = raw.voice.slice(0, 80);
    if (typeof raw.speed === "number" && isFinite(raw.speed) && raw.speed > 0 && raw.speed < 10) s.speed = raw.speed;
    s.autoplay = raw.autoplay === true;
    s.notify = raw.notify === true;
    return s;
  }
  function writeSettings(patch) {
    const s = Object.assign(readSettings(), patch || {});
    try { window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (_) { /* works without storage */ }
    return s;
  }

  function headers(json) {
    const h = {};
    let t = "";
    try { t = window.localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { t = ""; }
    if (t) h[TOKEN_HEADER] = t;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  // ---- the player ----------------------------------------------------------------------------------------------------
  // A silent half-second of 8 kHz mono WAV, as a blob, for the unlock: a media element must play something inside a tap.
  function silentWav() {
    const n = 4000, buf = new ArrayBuffer(44 + n), v = new DataView(buf);
    const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + n, true); str(8, "WAVE"); str(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 8000, true);
    v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true); str(36, "data"); v.setUint32(40, n, true);
    new Uint8Array(buf, 44).fill(128);
    return new Blob([buf], { type: "audio/wav" });
  }

  let runId = 0;            // bumped by every play and every stop: a step that finds it changed is stale and does nothing
  let playing = null;       // {thread, gseq} of the reply being read, or null
  let ctl = null;           // AbortController of the chunk fetches in flight
  let audioEl = null, urls = [], unlocked = false;
  let baseline = null;      // the gseq at or below which a finished reply is backlog; null = not armed, nothing autoplays
  const seen = new Set();   // gseqs replyFinished has already decided on (a poll repeats them)
  const tapNeeded = new Set(); // gseqs whose autoplay was held for a tap
  const messages = new Map(); // gseq -> the last fixed sentence about that reply
  const listeners = new Set();
  const key = (r) => String(r.thread) + ":" + String(r.gseq);

  function changed() { listeners.forEach((f) => { try { f(); } catch (_) { /* a view's fault never stops the player */ } }); }
  function dropUrls() { urls.forEach((u) => { try { URL.revokeObjectURL(u); } catch (_) { /* gone */ } }); urls = []; }

  function unlock() {
    if (unlocked) return;
    try {
      if (window.speechSynthesis && typeof SpeechSynthesisUtterance === "function") {
        const u = new SpeechSynthesisUtterance(""); u.volume = 0; window.speechSynthesis.speak(u);
      }
    } catch (_) { /* no on-device voice to unlock */ }
    try {
      if (!audioEl) audioEl = new Audio();
      const url = URL.createObjectURL(silentWav());
      urls.push(url);
      audioEl.muted = true; audioEl.src = url;
      const p = audioEl.play();
      const ok = () => { try { audioEl.pause(); } catch (_) { /* ok */ } audioEl.muted = false; unlocked = true; changed(); };
      if (p && typeof p.then === "function") p.then(ok, () => { audioEl.muted = false; });
      else ok();
    } catch (_) { /* a browser without media elements stays locked */ }
  }

  function stop() {
    runId++;
    if (ctl) { try { ctl.abort(); } catch (_) { /* done */ } ctl = null; }
    try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (_) { /* none */ }
    if (audioEl) { try { audioEl.pause(); } catch (_) { /* ok */ } audioEl.onended = null; audioEl.onerror = null; }
    dropUrls();
    if (playing) { playing = null; changed(); }
  }

  function say(reply, text) { messages.set(key(reply), text || ""); changed(); }

  function speakDevice(reply, text, me, speed) {
    return new Promise((resolve) => {
      if (!window.speechSynthesis || typeof SpeechSynthesisUtterance !== "function" || !text) { say(reply, WORDS.noSpeech); resolve(); return; }
      const u = new SpeechSynthesisUtterance(text);
      u.rate = speed;
      const voices = (window.speechSynthesis.getVoices && window.speechSynthesis.getVoices()) || [];
      const local = voices.filter((v) => v.localService);   // on-device voices only: the free tier sends nothing anywhere
      const pick = local.find((v) => /^en/i.test(v.lang || "")) || local[0];
      if (pick) u.voice = pick;
      u.onend = u.onerror = () => resolve();
      window.speechSynthesis.speak(u);
    }).then(() => { if (me === runId) { playing = null; changed(); } });
  }

  // One chunk from the one route. Resolves {blob, total} or {refused: {message, fallback}} or {aborted: true}.
  async function fetchChunk(reply, chunk, s, signal) {
    let res;
    try {
      res = await fetch(SPEAK_PATH, {
        method: "POST", headers: headers(true), signal,
        body: JSON.stringify({ room_thread_id: reply.thread, reply_gseq: reply.gseq, chunk,
                               provider: s.provider, voice: s.voice, speed: s.speed }),
      });
    } catch (e) { return signal.aborted ? { aborted: true } : { refused: { message: WORDS.failed, fallback: "on_device" } }; }
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (_) { body = null; }
      return { refused: { message: (body && typeof body.message === "string" && body.message) || WORDS.failed,
                          fallback: body && body.fallback === "on_device" ? "on_device" : "" } };
    }
    try {
      return { blob: await res.blob(), total: Math.max(1, Number(res.headers.get("X-Voice-Chunks")) || 1) };
    } catch (_) { return signal.aborted ? { aborted: true } : { refused: { message: WORDS.failed, fallback: "on_device" } }; }
  }

  function playBlob(blob, me) {
    return new Promise((resolve) => {
      if (me !== runId) { resolve(true); return; }
      if (!audioEl) audioEl = new Audio();
      const url = URL.createObjectURL(blob);
      urls.push(url);
      audioEl.muted = false;
      audioEl.onended = () => resolve(true);
      audioEl.onerror = () => resolve(false);
      audioEl.src = url;
      const p = audioEl.play();
      if (p && typeof p.catch === "function") p.catch(() => resolve(false));
    });
  }

  async function playPremium(reply, me, s) {
    ctl = new AbortController();
    const signal = ctl.signal;
    let next = fetchChunk(reply, 0, s, signal), total = 1;
    for (let i = 0; i < total; i++) {
      const got = await next;
      if (me !== runId || got.aborted) return;
      if (got.refused) {
        if (i === 0 && got.refused.fallback === "on_device") {
          say(reply, got.refused.message);
          await speakDevice(reply, reply.text || "", me, s.speed);
        } else { say(reply, i === 0 ? got.refused.message : WORDS.midway); }
        break;
      }
      total = got.total;
      next = i + 1 < total ? fetchChunk(reply, i + 1, s, signal) : null;   // the next is fetched while this one plays
      const heard = await playBlob(got.blob, me);
      if (me !== runId) return;
      if (!heard) { say(reply, i === 0 ? WORDS.cantPlay : WORDS.midway); break; }
    }
    if (me === runId) { playing = null; changed(); }
  }

  // Read one finished reply now: from the tap on its control, or from autoplay. A second call replaces the first.
  async function play(reply) {
    if (!reply || reply.thread === undefined || reply.gseq === undefined) return;
    stop();
    const me = runId;
    const s = readSettings();
    playing = { thread: reply.thread, gseq: reply.gseq };
    messages.delete(key(reply)); tapNeeded.delete(key(reply));
    changed();
    if (s.tier === "premium" && s.provider) await playPremium(reply, me, s);
    else await speakDevice(reply, reply.text || "", me, s.speed);
  }

  // The autoplay rule, in one place. Returns the decision as a word so the caller and the tests can see it.
  function replyFinished(reply) {
    if (!reply || reply.thread === undefined || reply.gseq === undefined) return "ignored";
    const k = key(reply);
    if (seen.has(k)) return "seen";
    seen.add(k);
    if (!readSettings().autoplay) return "off";
    if (baseline === null || Number(reply.gseq) <= baseline) return "backlog";
    if (document.hidden) return "hidden";
    if (!unlocked) { tapNeeded.add(k); changed(); return "tap"; }
    play(reply);
    return "play";
  }

  function arm(maxFinishedGseq) { baseline = maxFinishedGseq === null || maxFinishedGseq === undefined ? null : (Number(maxFinishedGseq) || 0); }

  // The per-reply control: one button, Play / Stop / Tap to hear, and a line for a fixed sentence about that reply.
  function button(reply) {
    const wrap = document.createElement("span");
    wrap.className = "voice-ctl";
    const b = document.createElement("button");
    b.type = "button"; b.className = "voice-btn";
    const note = document.createElement("span");
    note.className = "voice-say"; note.setAttribute("role", "status"); note.hidden = true;
    wrap.appendChild(b); wrap.appendChild(note);
    const k = key(reply);
    const paint = () => {
      const mine = playing && key(playing) === k;
      b.textContent = mine ? WORDS.stop : (tapNeeded.has(k) ? WORDS.tap : WORDS.play);
      b.setAttribute("aria-pressed", mine ? "true" : "false");
      const m = messages.get(k) || "";
      note.textContent = m; note.hidden = !m;
    };
    b.addEventListener("click", () => {
      unlock();
      if (playing && key(playing) === k) stop(); else play(reply);
    });
    listeners.add(paint);
    paint();
    wrap._unbind = () => listeners.delete(paint);
    return wrap;
  }

  // A hidden page reads nothing: the room view unmounts then, and a playing reply would otherwise carry on unseen.
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });

  window.LevainVoice = {
    SAMPLE, WORDS, DEFAULTS,
    settings: readSettings, saveSettings: writeSettings,
    unlock, stop, play, arm, replyFinished, button,
    isUnlocked: () => unlocked,
    onChange: (f) => { listeners.add(f); return () => listeners.delete(f); },
    sampleDevice: function () {
      unlock();
      stop();
      const me = runId, s = readSettings();
      return speakDevice({ thread: "sample", gseq: 0 }, SAMPLE, me, s.speed);
    },
  };
})();
