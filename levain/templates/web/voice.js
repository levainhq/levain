// levain voice.js — reading the room aloud: the settings, the player, the per-reply control and the autoplay rule
// (voice V1-5; design project_memory/1104-voice/V1_DESIGN.md section 4).
//
// The player asks ONE route for audio: POST /cockpit/voice/speak {room_thread_id, reply_gseq, chunk, provider, voice,
// speed}. The page never sends words to speak; the server rebuilds the reply from the room record. The on-device tier
// (speechSynthesis) calls no route at all and speaks only with a voice the browser marks localService === true. This file
// calls no other route: the settings block (devices_panel.js) is the one reader of GET /cockpit/voice.json, and hands the
// providers it read to LevainVoice.offer() so the player and the block use the SAME effective settings.
//
// Integration surface (the room view, a later slice; this file is loadable on its own and hooks nothing by itself):
//   LevainVoice.arm(maxFinishedGseq)       once at mount: replies finished at or before it are backlog, never autoplayed
//   LevainVoice.unlock()                   inside the operator's own tap (Send, a mic tap, a Play tap); a stop() after it is fine
//   LevainVoice.replyFinished(reply)       when a reply's vendor_turn_ended arrives on a poll
//   LevainVoice.button(reply)              the speaker control for a finished reply (draw it at the reply's foot)
//   LevainVoice.stop()                     on Send, a mic tap, a new conversation, unmount (a hidden page stops itself and locks)
//   reply = { thread, gseq, text }  thread = the room_thread_id, gseq = the gseq of the reply's vendor_turn_ended,
//   text = the reply as drawn (read only by the on-device voice; the premium route never receives it)
// Two replies that finish in one poll: the first is read to its end, ONE more waits behind it (a third replaces the waiting
// one, and the replaced reply says so). A tap on a control, Send or a stop clears the wait.
// Everything is drawn with textContent. Browser storage is wrapped in try/catch: the page works without it.
(function () {
  "use strict";
  const SETTINGS_KEY = "levain_voice_settings";
  const TOKEN_KEY = "levain_write_token";
  const TOKEN_HEADER = "X-Levain-Write-Token";
  const SPEAK_PATH = "/cockpit/voice/speak";
  const SAMPLE = "This is how your room will sound.";
  const VOICE_WAIT_MS = 1500;   // how long the on-device tier waits for the browser to list its voices
  const CAP = 200;              // the most gseqs each remembered map keeps
  const WORDS = {
    play: "Play", stop: "Stop", tap: "Tap to hear",
    failed: "Premium voice failed.",
    failedDevice: "Premium voice failed; reading uses the on-device voice.",
    midway: "Premium voice stopped partway; the rest is not read aloud.",
    cantPlay: "This browser could not play the audio.",
    noSpeech: "This browser has no on-device voice.",
    noLocal: "No on-device voice is ready yet; tap again in a moment.",
    nothing: "There is nothing to read aloud.",
    queued: "Waiting: another reply is being read first.",
    skipped: "Not read: a newer reply was waiting.",
  };
  const DEFAULTS = { tier: "device", provider: "", voice: "", speed: 1, autoplay: false, notify: false };

  // ---- per-device settings -------------------------------------------------------------------------------------------
  let mem = null;          // the last settings written: used when this browser will not keep them
  let storageOk = true;
  function readSettings() {
    let raw = null;
    if (storageOk) { try { raw = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || "null"); } catch (_) { raw = null; } }
    else raw = mem;
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
    mem = s;
    try { window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (_) { storageOk = false; }
    return s;
  }

  // The providers the cockpit says hold a live key for speech (devices_panel.js hands them over after GET /cockpit/voice.json).
  // Until then the player trusts the stored choice, and a premium choice with no stored voice reads on-device.
  let offered = null;
  function offer(list) {
    offered = Array.isArray(list) ? list.filter((p) => p && typeof p.name === "string").map((p) => ({
      name: p.name, voices: Array.isArray(p.voices) ? p.voices.slice() : [], default_voice: p.default_voice || "",
      speed: Array.isArray(p.speed) && p.speed.length === 2 ? [Number(p.speed[0]), Number(p.speed[1])] : [0.5, 2],
    })) : null;
  }
  // What the player WILL use, and what the block shows: a voice the stored provider does not offer is replaced by its default,
  // a speed outside its range by the nearest end. A stored provider that is NOT offered (its key was revoked) is never swapped
  // for another vendor (that would move money and the words to a vendor he did not choose): `blocked` names it, the player
  // reads nothing and sends nothing, and the block shows the same sentence. No stored provider and none live means on-device.
  function blockedWords(name) { return "Your " + name + " voice key is not available; choose a voice in Settings."; }
  function effective() {
    const st = readSettings(), out = Object.assign({}, st);
    if (st.tier !== "premium") return out;
    if (offered === null) { if (!st.provider || !st.voice) out.tier = "device"; return out; }
    if (st.provider && !offered.some((x) => x.name === st.provider)) { out.blocked = blockedWords(st.provider); return out; }
    const p = offered.find((x) => x.name === st.provider) || offered[0] || null;
    if (!p) { out.tier = "device"; return out; }
    out.provider = p.name;
    out.voice = p.voices.indexOf(st.voice) >= 0 ? st.voice : p.default_voice;
    out.speed = Math.min(p.speed[1], Math.max(p.speed[0], st.speed));
    return out;
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
  let audioEl = null;       // the one media element: unlocked inside a tap, then reused for every chunk
  let settleSpeech = null;  // ends the on-device utterance now (stop calls it), or null when none is speaking
  let liveUtt = null;       // the utterance being spoken, held here until it ends: a browser may collect one only the engine holds
  let settleChunk = null;   // ends the chunk now on the element (stop calls it), or null when no chunk is on it
  let unlocked = false, lockEpoch = 0, unlocking = false;
  let queued = null;        // the one reply waiting behind the one being read
  let baseline = null;      // the gseq at or below which a finished reply is backlog; null = not armed, nothing autoplays
  const seen = new Set();   // replies replyFinished has already decided on (a poll repeats them)
  const floor = new Map();  // thread -> the highest gseq evicted from `seen`: a reply at or below it that is not in `seen` was decided
  const tapNeeded = new Set(); // gseqs whose autoplay was held for a tap
  const messages = new Map(); // gseq -> the last fixed sentence about that reply
  const listeners = new Set();
  const key = (r) => String(r.thread) + ":" + String(r.gseq);

  // The three maps keep the newest CAP entries: a cockpit left open for a day does not grow with its replies.
  function remember(coll, k, v) {
    if (coll instanceof Map) { coll.delete(k); coll.set(k, v); } else { coll.delete(k); coll.add(k); }
    while (coll.size > CAP) coll.delete(coll.keys().next().value);
  }
  // `seen` keeps the newest CAP decisions; what it evicts is not forgotten, it moves into `floor` (a high-water mark per thread),
  // so a poll that repeats an old reply never re-offers it, however many replies came since.
  function wasSeen(reply, k) {
    if (seen.has(k)) return true;
    const f = floor.get(String(reply.thread));
    return f !== undefined && Number(reply.gseq) <= f;
  }
  function markSeen(k) {
    seen.delete(k); seen.add(k);
    while (seen.size > CAP) {
      const old = seen.keys().next().value;
      seen.delete(old);
      const i = old.lastIndexOf(":"), thread = old.slice(0, i), g = Number(old.slice(i + 1));
      if (isFinite(g)) { const f = floor.get(thread); if (f === undefined || g > f) { floor.delete(thread); floor.set(thread, g); } }
    }
    while (floor.size > CAP) floor.delete(floor.keys().next().value);
  }
  function changed() { listeners.forEach((f) => { try { f(); } catch (_) { /* a view's fault never stops the player */ } }); }

  function localVoice() {
    const ss = window.speechSynthesis;
    let all = [];
    try { all = (ss && ss.getVoices && ss.getVoices()) || []; } catch (_) { all = []; }
    const local = Array.prototype.filter.call(all, (v) => v && v.localService === true);   // on-device only: nothing is sent anywhere
    return local.find((v) => /^en/i.test(v.lang || "")) || local[0] || null;
  }

  // The unlock: a media element must be played once inside the operator's own tap. A play() started inside the tap has taken
  // the gesture whether or not it finishes: the stop() or the first real chunk that follows in the same tap ends it with an
  // AbortError, which is no refusal. Only a refusal (NotAllowedError) leaves the page locked.
  function unlock() {
    if (unlocked || unlocking) return;
    if (settleChunk) { unlocked = true; changed(); return; }       // a chunk is already playing, so the element is allowed
    try {
      const v = localVoice(), ss = window.speechSynthesis;
      if (v && ss && typeof SpeechSynthesisUtterance === "function") {
        const u = new SpeechSynthesisUtterance(""); u.voice = v; u.volume = 0; ss.speak(u);
      }
    } catch (_) { /* no on-device voice to unlock */ }
    try {
      if (!audioEl) audioEl = new Audio();
      const el = audioEl, epoch = lockEpoch, url = URL.createObjectURL(silentWav());
      unlocking = true;
      el.muted = true; el.src = url;
      let p;
      try { p = el.play(); } catch (e) { p = Promise.reject(e); }
      const end = (ok) => {
        if (epoch === lockEpoch) unlocking = false;
        try { URL.revokeObjectURL(url); } catch (_) { /* gone */ }
        if (el.src === url) { try { el.pause(); } catch (_) { /* ok */ } el.muted = false; }   // only while the silence is still on the element
        if (epoch === lockEpoch) { unlocked = ok; changed(); }
      };
      if (p && typeof p.then === "function") p.then(() => end(true), (e) => end(!!e && e.name === "AbortError"));
      else end(true);
    } catch (_) { unlocking = false; /* a browser without media elements stays locked */ }
  }

  // The page hid, or is going: nothing plays and the next reply asks for a tap.
  function lock() { stop(); lockEpoch++; unlocked = false; unlocking = false; changed(); }

  function dropQueued() { if (queued) { messages.delete(key(queued)); queued = null; } }

  function stop() {
    runId++;
    dropQueued();
    if (ctl) { try { ctl.abort(); } catch (_) { /* done */ } ctl = null; }
    try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (_) { /* none */ }
    const s = settleChunk;
    if (s) s(false);
    const sp = settleSpeech;
    if (sp) sp();
    if (playing) { playing = null; changed(); }
  }

  function say(reply, text) { remember(messages, key(reply), text || ""); changed(); }
  function sayIfNone(reply, text) { if (!messages.get(key(reply))) say(reply, text); }

  // Resolves a local voice, or null. The browser lists its voices late (Chrome fires voiceschanged), so it waits, bounded.
  function waitForLocalVoice() {
    const have = localVoice();
    const ss = window.speechSynthesis;
    if (have || !ss || typeof ss.addEventListener !== "function") return Promise.resolve(have);
    return new Promise((resolve) => {
      let t = 0;
      const done = () => { clearTimeout(t); ss.removeEventListener("voiceschanged", on); resolve(localVoice()); };
      const on = () => { if (localVoice()) done(); };
      ss.addEventListener("voiceschanged", on);
      t = setTimeout(done, VOICE_WAIT_MS);
    });
  }

  async function speakDevice(reply, text, me, speed) {
    const ss = window.speechSynthesis;
    if (!ss || typeof SpeechSynthesisUtterance !== "function") { sayIfNone(reply, WORDS.noSpeech); return; }
    if (!text) { sayIfNone(reply, WORDS.nothing); return; }
    const voice = await waitForLocalVoice();
    if (me !== runId) return;
    if (!voice) { sayIfNone(reply, WORDS.noLocal); return; }   // never speak(): a default voice may be network-backed
    await new Promise((resolve, reject) => {
      const u = new SpeechSynthesisUtterance(text);
      u.voice = voice;
      u.rate = speed;
      let over = false, wd = 0;
      const finish = (cancel) => {
        if (over) return;
        over = true;
        clearTimeout(wd);
        if (settleSpeech === finish) settleSpeech = null;
        if (liveUtt === u) liveUtt = null;
        u.onend = u.onerror = null;
        if (cancel === true) { try { ss.cancel(); } catch (_) { /* none */ } }
        resolve();
      };
      u.onend = u.onerror = () => finish();
      liveUtt = u; settleSpeech = finish;
      wd = setTimeout(() => finish(true), speechBound(text, speed));   // an engine that never says "end" does not hold the queue
      try { ss.speak(u); } catch (e) { over = true; clearTimeout(wd); if (settleSpeech === finish) settleSpeech = null; if (liveUtt === u) liveUtt = null; reject(e); }
    });
  }
  // The longest an utterance may take: a few seconds plus the slow end of speech (about 7 characters a second at rate 1).
  function speechBound(text, rate) {
    const r = Math.max(0.25, Number(rate) || 1);
    return Math.min(600000, 5000 + Math.ceil(String(text).length * 150 / r));
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
    } catch (e) { return signal.aborted ? { aborted: true } : { refused: { message: WORDS.failedDevice, fallback: "on_device" } }; }
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (_) { body = null; }
      const onDevice = !!body && body.fallback === "on_device";
      return { refused: { message: (body && typeof body.message === "string" && body.message) || WORDS.failed,
                          fallback: onDevice ? "on_device" : "" } };
    }
    try {
      return { blob: await res.blob(), total: Math.max(1, Number(res.headers.get("X-Voice-Chunks")) || 1) };
    } catch (_) { return signal.aborted ? { aborted: true } : { refused: { message: WORDS.failedDevice, fallback: "on_device" } }; }
  }

  // One chunk on the element. Resolves true when it played to its end, false when it could not play or was stopped. Its blob
  // URL is revoked the moment the chunk is over, however it ended, and stop() settles it, so no step is left waiting.
  function playBlob(blob, me) {
    return new Promise((resolve) => {
      if (me !== runId) { resolve(true); return; }
      if (!audioEl) audioEl = new Audio();
      const el = audioEl, url = URL.createObjectURL(blob);
      let over = false;
      const finish = (heard) => {
        if (over) return;
        over = true;
        if (settleChunk === finish) settleChunk = null;
        el.onended = null; el.onerror = null;
        try { el.pause(); } catch (_) { /* ok */ }
        try { URL.revokeObjectURL(url); } catch (_) { /* gone */ }
        if (el.src === url) { try { el.removeAttribute("src"); el.load(); } catch (_) { /* ok */ } }
        resolve(heard);
      };
      settleChunk = finish;
      el.muted = false;
      el.onended = () => finish(true);
      el.onerror = () => finish(false);
      el.src = url;
      let p;
      try { p = el.play(); } catch (_) { finish(false); return; }
      if (p && typeof p.catch === "function") p.catch((e) => { if (!(e && e.name === "AbortError" && over)) finish(false); });
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
    if (me === runId && ctl) { try { ctl.abort(); } catch (_) { /* done */ } ctl = null; }   // a break leaves no chunk billed unheard
  }

  // Read one finished reply now: from the tap on its control, or from autoplay. A second call replaces the first.
  async function play(reply) {
    if (!reply || reply.thread === undefined || reply.gseq === undefined) return;
    stop();
    const me = runId;
    const s = effective();
    playing = { thread: reply.thread, gseq: reply.gseq };
    messages.delete(key(reply)); tapNeeded.delete(key(reply));
    changed();
    try {
      if (s.blocked) say(reply, s.blocked);                      // the stored provider has no key: nothing is read, nothing is sent
      else if (s.tier === "premium") await playPremium(reply, me, s);
      else await speakDevice(reply, reply.text || "", me, s.speed);
    } catch (_) {
      if (me === runId) {
        if (ctl) { try { ctl.abort(); } catch (_e) { /* done */ } ctl = null; }
        sayIfNone(reply, WORDS.cantPlay);
      }
    } finally {
      if (me === runId) {      // whatever happened, this reading is over: `playing` clears and the one waiting reply gets its turn
        playing = null; changed();
        const q = queued;
        queued = null;
        if (q) {
          if (!document.hidden && unlocked && readSettings().autoplay) play(q);
          else { messages.delete(key(q)); if (readSettings().autoplay) remember(tapNeeded, key(q)); changed(); }
        }
      }
    }
  }

  // The autoplay rule, in one place. Returns the decision as a word so the caller and the tests can see it.
  function replyFinished(reply) {
    if (!reply || reply.thread === undefined || reply.gseq === undefined) return "ignored";
    const k = key(reply);
    if (wasSeen(reply, k)) return "seen";
    if (!readSettings().autoplay) { markSeen(k); return "off"; }
    if (baseline === null || Number(reply.gseq) <= baseline) { markSeen(k); return "backlog"; }
    if (document.hidden) return "hidden";           // not decided: when the page is back, the poll offers it again, as a tap
    markSeen(k);
    if (!unlocked) { remember(tapNeeded, k); changed(); return "tap"; }
    if (playing) {
      if (queued) say(queued, WORDS.skipped);
      queued = reply;
      say(reply, WORDS.queued);
      return "queued";
    }
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
    wrap._unbind = () => { listeners.delete(paint); messages.delete(k); tapNeeded.delete(k); };
    return wrap;
  }

  // A hidden page reads nothing and locks: the room view unmounts then, and a playing reply would otherwise carry on unseen.
  // Coming back, the first reply asks for a tap.
  document.addEventListener("visibilitychange", () => { if (document.hidden) lock(); });
  window.addEventListener("pagehide", lock);

  window.LevainVoice = {
    SAMPLE, WORDS, DEFAULTS,
    settings: readSettings, saveSettings: writeSettings, effective, offer,
    unlock, stop, play, arm, replyFinished, button,
    isUnlocked: () => unlocked,
    noteFor: (reply) => messages.get(key(reply)) || "",
    held: () => liveUtt !== null,
    speechBound,
    sizes: () => ({ seen: seen.size, floor: floor.size, tapNeeded: tapNeeded.size, messages: messages.size }),
    onChange: (f) => { listeners.add(f); return () => listeners.delete(f); },
    sampleDevice: async function () {
      const reply = { thread: "sample", gseq: 0 };
      unlock();
      stop();
      messages.delete(key(reply));
      const me = runId, s = readSettings();
      await speakDevice(reply, SAMPLE, me, s.speed);
      changed();
    },
  };
})();
