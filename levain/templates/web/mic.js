// levain mic.js — streaming dictation capture (cockpit mic slice S4; design project_memory/1107-15-voice/COCKPIT_MIC_DESIGN.md).
//
// The page captures the microphone as PCM16 mono 16 kHz in ~250 ms chunks and POSTs them to three routes, in order:
//   POST /cockpit/voice/stream/open?provider=<name>            -> {sid}
//   POST /cockpit/voice/stream/chunk?sid=<sid>&seq=<n>         body = raw PCM16 little-endian, 8000 bytes; n = 0,1,2,...  -> {seq, partial, final}
//   POST /cockpit/voice/stream/close?sid=<sid>                 -> {text}
// partial = the in-progress tail (string), final = the text committed so far (string). The page sends bytes, a session id and a
// sequence number and nothing else: no words from the page ever reach the server. Every transcript is drawn by the caller with
// textContent only. Nothing here runs until the operator's tap calls start(); a hidden page stops the mic.
//
//   LevainMic.available()      true only once the cockpit's GET /cockpit/voice.json said {"stream": true} with a provider that lists
//                              "stt", AND this browser has getUserMedia and an audio context. Default false.
//   LevainMic.probe()          Promise<boolean>: reads voice.json once (the write token header) and settles available()
//   LevainMic.start()          inside the operator's tap
//   LevainMic.stop()           flush, close the session, release every track; returns a Promise of the final text ("" if none),
//                              which also arrives through onFinal
//   LevainMic.active()         true from start() until the session has ended
//   LevainMic.onPartial(cb)    cb(text) the live text (final + partial); returns an unsubscribe function
//   LevainMic.onFinal(cb)      cb(text) the text of the closed session
//   LevainMic.onNote(cb)       cb(sentence) one of the fixed sentences below
//   LevainMic.onEnd(cb)        cb() whenever a session ends, for any reason
//   LevainMic.attach(box, o)   the Mic and Dictate controls for ONE text box (card replies): {buttons, busy(), off(), stop(), lock(on)}; busy() is true until the final text is in
//
// Stop-time WAV fallback (cockpit mic S5b): every PCM chunk the page captures is also kept in memory (bounded: the oldest are dropped
// past limits.wavMax, which is the transcribe route's 4 MiB upload cap less the 44-byte header). When the stream breaks (a chunk
// fails or times out, the open is refused after audio was captured, or the close cannot confirm the final text), the kept PCM is
// wrapped as a 16 kHz mono PCM16 WAV and POSTed ONCE to the existing POST /cockpit/voice/transcribe?provider=<name>
// (Content-Type audio/wav); its text arrives through onFinal with one fixed note. Nothing is kept or sent once the session ends.
(function () {
  "use strict";
  var TOKEN_KEY = "levain_write_token", TOKEN_HEADER = "X-Levain-Write-Token";
  var BASE = "/cockpit/voice/stream/", TRANSCRIBE = "/cockpit/voice/transcribe?provider=";
  var MIN_WAV = 8000;                        // under 250 ms of audio there is nothing worth transcribing
  var RATE = 16000, CHUNK = 4000;            // 4000 samples at 16 kHz = 250 ms = 8000 bytes
  var MAX_MS = 120000;                       // the server's session ceiling; the page ends a session at it
  var WORDS = {
    openFailed: "Streaming dictation could not start; the Dictate control and the keyboard dictation key still work.",
    broke: "Streaming dictation stopped; the words so far are in the box.",
    revoked: "Premium voice was turned off, so the dictation stopped; the words so far are in the box.",
    wavFilled: "Streaming dictation stopped; these words come from the recording instead. Check them before you send.",
    wavTrimmed: "Streaming dictation stopped; these words come from the recording instead, and the start of a long recording was left out. Check them before you send.",
  };
  var listeners = { partial: [], final: [], note: [], end: [], committed: [] };   // committed: attach()'s own (the server's settled words)
  var streamOk = false, provider = "", probing = null;
  var S = null;                              // the live session, or null
  var deadlines = { open: 10000, chunk: 10000, close: 5000, wav: 30000 };
  var limits = { wavMax: 4 * 1024 * 1024 - 44 };   // bytes of PCM kept for the fallback: the server's UPLOAD_MAX less the WAV header

  function headers() {
    var h = {}, t = "";
    try { t = window.localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { t = ""; }
    if (t) h[TOKEN_HEADER] = t;
    return h;
  }
  function caps() {
    var md = window.navigator && window.navigator.mediaDevices;
    return !!(md && typeof md.getUserMedia === "function" && (window.AudioContext || window.webkitAudioContext));
  }
  function emit(kind, arg) { listeners[kind].slice().forEach(function (cb) { try { cb(arg); } catch (e) { /* a listener's fault is its own */ } }); }
  function on(kind, cb) {
    if (typeof cb !== "function") return function () {};
    listeners[kind].push(cb);
    return function () { var i = listeners[kind].indexOf(cb); if (i >= 0) listeners[kind].splice(i, 1); };
  }

  // Cached only when the server gave a definitive answer; a network failure or a non-2xx clears the cache so a later mount retries.
  function probe() {
    if (probing) return probing;
    var p = probing = window.fetch("/cockpit/voice.json", { headers: headers() }).then(function (r) {
      if (!r.ok) { if (probing === p) probing = null; return null; }
      return r.json();
    }).then(function (j) {
      streamOk = false; provider = "";
      if (j && j.stream === true && typeof j.stream_provider === "string" && j.stream_provider) {
        provider = j.stream_provider; streamOk = true;
      } else if (j && j.stream === true && Array.isArray(j.providers)) {
        var q = j.providers.filter(function (x) { return x && typeof x.name === "string" && x.key === "set" && Array.isArray(x.uses) && x.uses.indexOf("stt") >= 0; })[0];
        if (q) { provider = q.name; streamOk = true; }
      }
      return available();
    }).then(null, function () { if (probing === p) probing = null; return false; });
    return p;
  }
  function available() { return streamOk && caps(); }

  // ---- resample + chunk: linear interpolation from the context's real rate to 16 kHz, carried across blocks ----------------
  function makeChunker(rate, emitChunk) {
    var ratio = rate / RATE, pend = new Float32Array(0), pos = 0, out = new Int16Array(CHUNK), n = 0;
    function put(v) {
      v = v < -1 ? -1 : v > 1 ? 1 : v;
      out[n++] = v < 0 ? Math.round(v * 32768) : Math.round(v * 32767);
      if (n === CHUNK) { emitChunk(out.buffer.slice(0)); n = 0; }
    }
    return {
      push: function (block) {
        var all = new Float32Array(pend.length + block.length);
        all.set(pend, 0); all.set(block, pend.length);
        while (pos + 1 < all.length) {
          var i = Math.floor(pos), f = pos - i;
          put(all[i] * (1 - f) + all[i + 1] * f);
          pos += ratio;
        }
        var drop = Math.min(Math.floor(pos), all.length);
        pend = all.slice(drop); pos -= drop;
      },
      flush: function () { if (n > 0) { emitChunk(out.buffer.slice(0, n * 2)); n = 0; } },
    };
  }

  // ---- the session -----------------------------------------------------------------------------------------------------
  // Every request has a deadline and a handle the session can cancel: a hung request must never wedge the session.
  function postJson(s, path, body, ms, ctype) {
    return new Promise(function (resolve, reject) {
      var opts = { method: "POST", headers: headers() }, ctl = null, done = false, tm = 0, h = null;
      if (body !== undefined) { opts.body = body; opts.headers["Content-Type"] = ctype || "application/octet-stream"; }
      if (typeof window.AbortController === "function") { ctl = new window.AbortController(); opts.signal = ctl.signal; }
      function settle(fn, v) {
        if (done) return;
        done = true; clearTimeout(tm);
        var i = s.reqs.indexOf(h); if (i >= 0) s.reqs.splice(i, 1);
        if (ctl && fn === reject) { try { ctl.abort(); } catch (e) { /* settled */ } }
        fn(v);
      }
      h = { cancel: function () { settle(reject, new Error("cancelled")); } };
      s.reqs.push(h);
      tm = setTimeout(function () { settle(reject, new Error("timeout")); }, ms);
      window.fetch(path, opts).then(function (r) {
        if (!r.ok) {
          // a refusal the server marked "revoked" (the key or its grant was revoked mid-dictation) is a FINAL stop, not a break
          return r.json().then(function (j) { var e = new Error("status"); e.revoked = !!(j && j.revoked === true); throw e; },
                               function () { throw new Error("status"); });
        }
        return r.json();
      }).then(function (j) { settle(resolve, j); }, function (e) { settle(reject, e); });
    });
  }
  function cancelAll(s) { s.reqs.slice().forEach(function (h) { h.cancel(); }); }
  function stopStream(st) {
    try { if (st) st.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* ended */ } }); } catch (e) { /* none */ }
  }
  // idempotent PER RESOURCE: each piece is taken off the session before it is released, so a second call (or a late one) finds nothing
  function releaseCapture(s) {
    s.released = true;
    var node = s.node, src = s.src, st = s.stream, ctx = s.ctx;
    s.node = null; s.src = null; s.stream = null; s.ctx = null;
    try { if (node) { if (node.port) node.port.onmessage = null; node.onaudioprocess = null; node.disconnect(); } } catch (e) { /* gone */ }
    try { if (src) src.disconnect(); } catch (e) { /* gone */ }
    stopStream(st);
    try { if (ctx && ctx.close) { var c = ctx.close(); if (c && c.catch) c.catch(function () {}); } } catch (e) { /* closed */ }
    clearTimeout(s.timer); s.timer = 0;
    document.removeEventListener("visibilitychange", s.onVis);
    window.removeEventListener("pagehide", s.onHide);
  }
  function finish(s) {
    if (s.ended || s.wavBusy) return;     // a recording being transcribed is the session's last act: nothing ends it early
    if (S === s) S = null;
    s.ended = true;
    releaseCapture(s);
    cancelAll(s);
    s.pcm = null;
    emit("end");
    s.resFin(s.text);
  }
  function stale(s) { return S !== s || s.stopping || s.released || s.dead || s.ended; }
  // ---- the stop-time WAV fallback --------------------------------------------------------------------------------------
  function keepPcm(s, bytes) {
    if (!s.pcm) return;
    s.pcm.push(bytes); s.pcmN += bytes.byteLength;
    while (s.pcmN > limits.wavMax && s.pcm.length > 1) { s.pcmN -= s.pcm.shift().byteLength; s.pcmDropped = true; }
  }
  function canWav(s) { return !!(provider && s.pcm && s.pcmN >= MIN_WAV && !s.wavTried); }
  function wavOf(s) {
    var n = s.pcmN, buf = new ArrayBuffer(44 + n), v = new DataView(buf), u = new Uint8Array(buf), off = 44, i;
    function tag(at, t) { for (var k = 0; k < 4; k++) v.setUint8(at + k, t.charCodeAt(k)); }
    tag(0, "RIFF"); v.setUint32(4, 36 + n, true); tag(8, "WAVE"); tag(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); tag(36, "data"); v.setUint32(40, n, true);
    for (i = 0; i < s.pcm.length; i++) { u.set(new Uint8Array(s.pcm[i]), off); off += s.pcm[i].byteLength; }
    return buf;
  }
  // Resolves true when the kept recording was transcribed and its text delivered (one note, then onFinal), false otherwise.
  // While it runs the session cannot be ended by anything but its own result (see finish).
  function recover(s) {
    if (!canWav(s)) return Promise.resolve(false);
    s.wavTried = true; s.wavBusy = true;
    return postJson(s, TRANSCRIBE + encodeURIComponent(provider), wavOf(s), deadlines.wav, "audio/wav").then(function (j) {
      if (!j || typeof j.text !== "string" || !j.text.trim()) throw new Error("empty");
      s.text = j.text.trim();
      emit("note", s.pcmDropped ? WORDS.wavTrimmed : WORDS.wavFilled);
      emit("final", s.text);
      return true;
    }).then(null, function () { return false; }).then(function (ok) { s.wavBusy = false; return ok; });
  }
  function enqueue(s, bytes) {
    if (s.ended) return;
    keepPcm(s, bytes);
    if (s.dead) return;
    var seq = s.seq++;
    s.q = s.q.then(function () {
      if (s.dead || s.ended) return;
      return postJson(s, BASE + "chunk?sid=" + encodeURIComponent(s.sid) + "&seq=" + seq, bytes, deadlines.chunk).then(function (j) {
        if (s.dead || s.ended) return;
        var fin = j && typeof j.final === "string" ? j.final : "", par = j && typeof j.partial === "string" ? j.partial : "";
        var text = (fin && par ? fin + " " + par : fin + par);
        if (fin !== s.fin) { s.fin = fin; emit("committed", fin); }     // the text the server calls committed, before the live text
        if (text !== s.last) { s.last = text; emit("partial", text); }
      });
    }).catch(function (err) {
      if (s.dead || s.ended) return;
      s.dead = true;
      if (err && err.revoked === true) {   // revoked: no WAV fallback, no audio kept or sent anywhere; the fixed words and an end
        releaseCapture(s);
        s.pcm = null;
        s.revoked = true;            // a Send that stopped this session must not transmit the words cut short by the revoke
        emit("note", WORDS.revoked);
        finish(s);
        closeSession(s, false);
        return;
      }
      if (s.stopping) s.closeFailed = true;
      if (canWav(s)) {              // the audio is in memory: transcribe it from there, the mic is let go meanwhile
        releaseCapture(s);
        recover(s).then(function (ok) {
          if (ok) s.closeFailed = false; else emit("note", WORDS.broke);
          finish(s);
          closeSession(s, false);
        });
        return;
      }
      emit("note", WORDS.broke);
      finish(s);                    // the session is over now; the close below is best effort and nothing waits on it
      closeSession(s, false);
    });
  }
  // The close outcome lives on its own session (s.closeFailed / s.revoked); lastStopped is the session the page last asked to
  // stop, the only one closeFailed() and revoked() answer for, so a stale session's late cleanup cannot block another's Send.
  var lastStopped = null;
  function closeSession(s, wantFinal) {
    if (!s.sid) { finish(s); return Promise.resolve(); }
    return postJson(s, BASE + "close?sid=" + encodeURIComponent(s.sid), undefined, deadlines.close).then(function (j) {
      if (wantFinal && j && typeof j.text === "string") { s.text = j.text; emit("final", j.text); }
    }, function () {
      s.closeFailed = true;         // the server ends an idle session itself
      if (!wantFinal) return null;
      return recover(s).then(function (ok) { if (ok) s.closeFailed = false; });
    }).then(function () { finish(s); });
  }
  // resolves with the final text ("" when there is none); safe to call twice
  function stopSession(s) {
    if (s.stopping || s.ended) return s.finP;
    s.stopping = true;
    s.closeFailed = false; s.revoked = false; lastStopped = s;
    releaseCapture(s);
    // still waiting for the permission or the worklet: there is no server session to close; the init chain finds the session
    // stale when it resumes and stops whatever it then holds
    if (s.phase === "gum" || s.phase === "wire") { finish(s); return s.finP; }
    if (s.chunker && !s.dead) s.chunker.flush();
    s.openP.then(function () {
      if (s.dead || s.ended) return;
      return s.q.then(function () { return s.dead ? null : closeSession(s, true); });
    }).then(null, function () { /* finish below */ }).then(function () { finish(s); });
    return s.finP;
  }
  function stop() { return S ? stopSession(S) : Promise.resolve(""); }
  function start() {
    if (S || !available()) return;
    var s = S = { sid: "", seq: 0, q: null, openP: null, dead: false, stopping: false, released: false, ended: false, last: "", fin: "", text: "",
      phase: "gum", reqs: [], pcm: [], pcmN: 0, pcmDropped: false, wavTried: false, wavBusy: false, node: null, src: null, stream: null, ctx: null, timer: 0, chunker: null };
    s.finP = new Promise(function (res) { s.resFin = res; });
    var AC = window.AudioContext || window.webkitAudioContext;
    var fail = function () {
      if (s.ended) { releaseCapture(s); return; }
      var announce = !s.stopping;
      s.dead = true; releaseCapture(s);
      if (announce && canWav(s)) {  // audio was captured while the open was pending: the recording can still be transcribed
        recover(s).then(function (ok) { if (!ok) emit("note", WORDS.openFailed); finish(s); });
        return;
      }
      if (announce) emit("note", WORDS.openFailed);
      finish(s);
    };
    s.onVis = function () { if (document.visibilityState === "hidden") stopSession(s); };
    s.onHide = function () { stopSession(s); };
    // the context and the permission are both asked for here, inside the tap
    try {
      s.ctx = new AC();
      var rp = s.ctx.resume && s.ctx.resume();
      if (rp && typeof rp.catch === "function") rp.catch(function () { fail(); });
    } catch (e) { fail(); return; }
    var gum;
    try { gum = window.navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } }); }
    catch (e) { gum = Promise.reject(e); }
    document.addEventListener("visibilitychange", s.onVis);
    window.addEventListener("pagehide", s.onHide);
    s.openP = new Promise(function (resolve) {
      gum.then(function (stream) {
        // every step below re-asks whether this session is still the live one; a late stream is stopped right here
        if (stale(s)) { stopStream(stream); releaseCapture(s); finish(s); return null; }
        s.stream = stream; s.phase = "wire";
        return withDeadline(wire(s), deadlines.open).then(function () {
          if (stale(s)) { releaseCapture(s); finish(s); return null; }
          s.timer = setTimeout(function () { stopSession(s); }, MAX_MS);
          s.phase = "open";
          return postJson(s, BASE + "open?provider=" + encodeURIComponent(provider), undefined, deadlines.open).then(function (j) {
            if (!j || typeof j.sid !== "string" || !j.sid || j.sid.length > 128) throw new Error("sid");
            s.sid = j.sid; s.phase = "live";
          });
        });
      }).then(resolve, function () { fail(); resolve(); });
    });
    s.q = s.openP;
  }
  // a worklet module that never loads must not hold the microphone open: the wait is bounded like the open request
  function withDeadline(p, ms) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { reject(new Error("deadline")); }, ms);
      p.then(function (v) { clearTimeout(t); resolve(v); }, function (e) { clearTimeout(t); reject(e); });
    });
  }
  // build the capture graph: an AudioWorklet where there is one, else the deprecated ScriptProcessor
  function wire(s) {
    var ctx = s.ctx;
    s.src = ctx.createMediaStreamSource(s.stream);
    s.chunker = makeChunker(ctx.sampleRate, function (bytes) { enqueue(s, bytes); });
    if (ctx.audioWorklet && typeof window.AudioWorkletNode === "function") {
      return ctx.audioWorklet.addModule("/mic_worklet.js").then(function () {
        if (stale(s)) return;
        s.node = new window.AudioWorkletNode(ctx, "levain-mic-tap");
        s.node.port.onmessage = function (ev) { if (!s.released && ev.data) s.chunker.push(ev.data); };
        s.src.connect(s.node); s.node.connect(ctx.destination);
      });
    }
    s.node = ctx.createScriptProcessor(4096, 1, 1);
    s.node.onaudioprocess = function (ev) { if (!s.released) s.chunker.push(new Float32Array(ev.inputBuffer.getChannelData(0))); };
    s.src.connect(s.node); s.node.connect(ctx.destination);
    return Promise.resolve();
  }

  // The on-device test every dictation control shares (the Room composer and the card boxes): a recognizer that cannot be told to
  // keep the audio on this device, one with no processLocally on its prototype (it would send the audio to the browser vendor's
  // cloud), is no recognizer here. Returns the constructor or null.
  function localSpeech(Speech) {
    return Speech && Speech.prototype && "processLocally" in Speech.prototype ? Speech : null;
  }

  // A control's state: the icon and its accessible name come from voice_icons.js; on a page without that file the words are the button.
  var FACE_WORDS = { dictate: "Dictate", "dictate-stop": "Stop dictating", mic: "Mic", "mic-stop": "Stop mic" };
  function face(b, kind) {
    if (window.LevainVoiceIcons) { window.LevainVoiceIcons.set(b, kind); return; }
    b.textContent = FACE_WORDS[kind]; b.setAttribute("aria-label", FACE_WORDS[kind]); b.setAttribute("title", FACE_WORDS[kind]);
  }

  var insts = [];                            // every attach() still in use: a capture started in one box ends the others'
  var TAIL_MS = 1500;                        // how long Stop dictating waits for the engine's last phrase
  var ORPHAN_MS = 5000;                      // a box never put on the page by then is let go (a draw that landed after its card closed)

  // A card reply box's mic (board row 140): the SAME streaming mic and on-device Dictate the Room composer has, for any one
  // text box. attach(box, {cls, note}) returns {buttons, busy(), off(), stop(), lock(on)}: the caller places `buttons` (hidden until
  // the server offers streaming / the browser offers on-device dictation). busy() is true from a tap until the session has ended and
  // the box holds its words, so a caller that SIGNS the box refuses while it is true. Only FINAL words stay: while a capture runs the
  // box shows the typed text, the final words and the live (interim) ones; when it ends for any reason (its end, an error, the tail
  // timeout, a broken or revoked stream, off()) the box is put back to the typed text and the final words alone before busy()
  // turns false, so an interim is never signed or sent. The box is read-only while a capture runs (the typed text is the box as it
  // stood at the tap). stop() ends any capture (its last words still land); off() ends it at once, drops any tail, hides the buttons
  // and lets go of every listener (a caller that replaces the box or starts a ceremony under a live mic calls it); lock(on) disables
  // the buttons with the caller's other controls but never a live capture's Stop. A box that leaves the page is let go on a timer
  // whether or not it ever captured. Words reach the box through .value writes only.
  function attach(box, o) {
    o = o || {};
    var doc = box.ownerDocument, cls = o.cls || "", say = typeof o.note === "function" ? o.note : function () {};
    var buttons = [], mine = false, typed = "", base = "", fin = "", rec = null, ending = false, dtimer = 0, gone = false, offs = [];
    var locked = false, watch = 0, seen = box.isConnected, born = Date.now(), sb = null, db = null;
    function detached() { if (box.isConnected) seen = true; return gone || (seen && !box.isConnected); }
    function sync() {
      buttons.forEach(function (b) { b.disabled = locked && !((b === sb && mine) || (b === db && !!rec && !ending)); });
    }
    function show(v) {
      if (box.maxLength > 0 && v.length > box.maxLength) v = v.slice(0, box.maxLength);
      box.value = v;
      try { box.dispatchEvent(new window.Event("input", { bubbles: true })); } catch (e) { /* no events */ }
    }
    function put(text) { show(base + text); }
    // the capture is over: the typed text and the final words, never the interim
    function restore() { show(fin ? base + fin : typed); }
    function begin() { typed = box.value; base = typed ? typed.replace(/\s*$/, " ") : ""; fin = ""; box.readOnly = true; }
    function mk(kind, extra) {
      var b = doc.createElement("button");
      b.type = "button"; b.className = (cls ? cls + " " : "") + "card-mic " + extra; b.hidden = true;
      face(b, kind);
      buttons.push(b); return b;
    }
    // Dictate: finishDict drops the engine's handlers, puts the box back to its typed and final words and frees it; endDict first
    // lets the engine deliver its last phrase (bounded by TAIL_MS); abortDict drops it at once
    function finishDict() {
      var r = rec; if (dtimer) { clearTimeout(dtimer); dtimer = 0; }
      if (r) { r.onresult = null; r.onend = null; r.onerror = null; restore(); box.readOnly = false; }
      rec = null; ending = false; if (db) face(db, "dictate");
      sync();
    }
    function abortDict() { var r = rec; finishDict(); if (r) { try { r.stop(); } catch (e) { /* stopped */ } } }
    function endDict() {
      if (!rec || ending) return;
      ending = true; if (db) face(db, "dictate");
      try { rec.stop(); } catch (e) { finishDict(); return; }
      dtimer = setTimeout(finishDict, TAIL_MS); sync();
    }
    function stopCapture() { if (mine) { if (sb) face(sb, "mic"); stop(); } endDict(); }
    function haltOthers() { insts.forEach(function (i) { if (i !== api) i.stop(); }); }
    function off() {
      if (gone) return;
      if (mine) { mine = false; restore(); stop(); }
      abortDict(); box.readOnly = false;
      gone = true; if (watch) { clearInterval(watch); watch = 0; }
      offs.forEach(function (f) { try { f(); } catch (e) { /* gone */ } }); offs = [];
      var at = insts.indexOf(api); if (at >= 0) insts.splice(at, 1);
      buttons.forEach(function (b) { b.hidden = true; });
    }
    // a box that leaves the page (or never reaches it) is let go on a timer, idle or not: a live capture ends without waiting for
    // the next partial (a silent room sends none), and an idle one gives up its listeners
    function watchOn() {
      setTimeout(detached, 0);      // a caller places the box right after attach(): from then on its leaving is seen at the next tick
      watch = setInterval(function () { if (detached() || (!seen && Date.now() - born > ORPHAN_MS)) off(); }, 400);
    }
    var api = { buttons: buttons, off: off, stop: stopCapture, busy: function () { return mine || !!rec; }, lock: function (on) { locked = !!on; sync(); } };
    insts.push(api);
    var SR = localSpeech(window.SpeechRecognition || window.webkitSpeechRecognition);
    if (SR) {                                           // on-device dictation, the room's rule: processLocally or off
      db = mk("dictate", "card-mic-dictate");
      var lang = doc.documentElement.lang || (window.navigator && window.navigator.language) || "en-US";
      // SpeechRecognition.available() is never asked: MEASURED 2026-11-04 (RM-G2) it froze the renderer of headless Chrome, so
      // the control shows on the prototype test alone and the click-time processLocally readback and onerror stay the guard.
      db.hidden = false;
      db.addEventListener("click", function () {
        if (mine || gone) return;
        if (rec) { endDict(); return; }
        haltOthers();
        if (S) { say("The microphone is in use: stop it first, then tap Dictate."); return; }
        var r = new SR();
        r.interimResults = true; r.continuous = false; r.processLocally = true; r.lang = lang;
        if (r.processLocally !== true) { say("This browser will not keep dictation on this device, so it is off; the keyboard dictation key works."); return; }
        r.onresult = function (ev) {
          if (rec !== r) return;
          var all = "", done = "";
          for (var i = 0; i < ev.results.length; i++) {
            var res = ev.results[i], t = res && res[0] && typeof res[0].transcript === "string" ? res[0].transcript : "";
            all += t; if (res && res.isFinal === true) done += t;
          }
          fin = done; put(all);
        };
        r.onend = function () { if (rec === r) finishDict(); };
        r.onerror = function (ev) { if (rec === r) { finishDict(); say(ev && ev.error === "not-allowed" ? "Dictation needs permission to use the microphone." : "Dictation stopped."); } };
        begin(); rec = r; ending = false; face(db, "dictate-stop");
        try { r.start(); } catch (e) { finishDict(); }
      });
      var leave = function () { if (detached()) { off(); return; } if (doc.visibilityState === "hidden") abortDict(); };
      doc.addEventListener("visibilitychange", leave);
      offs.push(function () { doc.removeEventListener("visibilitychange", leave); });
    }
    if (typeof probe === "function") {
      sb = mk("mic", "card-mic-stream");
      var live = function () { return mine && !detached(); };
      offs.push(on("partial", function (t) { if (detached()) { off(); return; } if (mine) put(t); }));
      offs.push(on("committed", function (t) { if (mine) fin = t; }));
      offs.push(on("final", function (t) { if (detached()) { off(); return; } if (mine) { fin = t; put(t); } }));
      offs.push(on("note", function (s) { if (live()) say(s); }));
      // the session is over (closed, broken, revoked, timed out): the box keeps the typed text and the final words alone
      offs.push(on("end", function () { if (!mine) return; restore(); mine = false; box.readOnly = false; face(sb, "mic"); sync(); }));
      Promise.resolve().then(probe).then(function (ok) { if (!gone && ok === true && available()) sb.hidden = false; }, function () {});
      sb.addEventListener("click", function () {
        if (gone) return;
        if (mine) { face(sb, "mic"); stop(); return; }        // the session closes; busy() holds until its end
        haltOthers();
        if (S) { say("The microphone is still closing: tap Mic again in a moment."); return; }
        if (rec) abortDict();
        begin(); mine = true; face(sb, "mic-stop");     // read-only while streaming: a partial redraws the whole box
        start();
        if (!S) { mine = false; box.readOnly = false; face(sb, "mic"); }
      });
    }
    watchOn();
    sync();
    return api;
  }

  window.LevainMic = {
    attach: attach, localSpeech: localSpeech, available: available, probe: probe, start: start, stop: stop, active: function () { return !!S; }, closeFailed: function () { return !!(lastStopped && lastStopped.closeFailed); }, revoked: function () { return !!(lastStopped && lastStopped.revoked); }, deadlines: deadlines, limits: limits,
    onPartial: function (cb) { return on("partial", cb); }, onFinal: function (cb) { return on("final", cb); },
    onNote: function (cb) { return on("note", cb); }, onEnd: function (cb) { return on("end", cb); },
  };
})();
