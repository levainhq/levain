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
(function () {
  "use strict";
  var TOKEN_KEY = "levain_write_token", TOKEN_HEADER = "X-Levain-Write-Token";
  var BASE = "/cockpit/voice/stream/";
  var RATE = 16000, CHUNK = 4000;            // 4000 samples at 16 kHz = 250 ms = 8000 bytes
  var MAX_MS = 120000;                       // the server's session ceiling; the page ends a session at it
  var WORDS = {
    openFailed: "Streaming dictation could not start; the Dictate control and the keyboard dictation key still work.",
    broke: "Streaming dictation stopped; the words so far are in the box.",
  };
  var listeners = { partial: [], final: [], note: [], end: [] };
  var streamOk = false, provider = "", probing = null;
  var S = null;                              // the live session, or null
  var deadlines = { open: 10000, chunk: 10000, close: 5000 };   // ms; a request past its deadline is cancelled, never waited on

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
      if (j && j.stream === true && Array.isArray(j.providers)) {
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
  function postJson(s, path, body, ms) {
    return new Promise(function (resolve, reject) {
      var opts = { method: "POST", headers: headers() }, ctl = null, done = false, tm = 0, h = null;
      if (body !== undefined) { opts.body = body; opts.headers["Content-Type"] = "application/octet-stream"; }
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
        if (!r.ok) throw new Error("status");
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
    if (s.ended) return;
    if (S === s) S = null;
    s.ended = true;
    releaseCapture(s);
    cancelAll(s);
    emit("end");
    s.resFin(s.text);
  }
  function stale(s) { return S !== s || s.stopping || s.released || s.dead || s.ended; }
  function enqueue(s, bytes) {
    if (s.dead || s.ended) return;
    var seq = s.seq++;
    s.q = s.q.then(function () {
      if (s.dead || s.ended) return;
      return postJson(s, BASE + "chunk?sid=" + encodeURIComponent(s.sid) + "&seq=" + seq, bytes, deadlines.chunk).then(function (j) {
        if (s.dead || s.ended) return;
        var fin = j && typeof j.final === "string" ? j.final : "", par = j && typeof j.partial === "string" ? j.partial : "";
        var text = (fin && par ? fin + " " + par : fin + par);
        if (text !== s.last) { s.last = text; emit("partial", text); }
      });
    }).catch(function () {
      if (s.dead || s.ended) return;
      s.dead = true;
      if (s.stopping) closeFailed = true;
      emit("note", WORDS.broke);
      finish(s);                    // the session is over now; the close below is best effort and nothing waits on it
      closeSession(s, false);
    });
  }
  var closeFailed = false;       // true when the last stop could not confirm the final text with the server
  function closeSession(s, wantFinal) {
    if (!s.sid) { finish(s); return Promise.resolve(); }
    return postJson(s, BASE + "close?sid=" + encodeURIComponent(s.sid), undefined, deadlines.close).then(function (j) {
      if (wantFinal && j && typeof j.text === "string") { s.text = j.text; emit("final", j.text); }
    }, function () { closeFailed = true; /* the server ends an idle session itself */ }).then(function () { finish(s); });
  }
  // resolves with the final text ("" when there is none); safe to call twice
  function stopSession(s) {
    if (s.stopping || s.ended) return s.finP;
    s.stopping = true;
    closeFailed = false;
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
    var s = S = { sid: "", seq: 0, q: null, openP: null, dead: false, stopping: false, released: false, ended: false, last: "", text: "",
      phase: "gum", reqs: [], node: null, src: null, stream: null, ctx: null, timer: 0, chunker: null };
    s.finP = new Promise(function (res) { s.resFin = res; });
    var AC = window.AudioContext || window.webkitAudioContext;
    var fail = function () {
      if (s.ended) { releaseCapture(s); return; }
      var announce = !s.stopping;
      s.dead = true; releaseCapture(s);
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

  window.LevainMic = {
    available: available, probe: probe, start: start, stop: stop, active: function () { return !!S; }, closeFailed: function () { return closeFailed; }, deadlines: deadlines,
    onPartial: function (cb) { return on("partial", cb); }, onFinal: function (cb) { return on("final", cb); },
    onNote: function (cb) { return on("note", cb); }, onEnd: function (cb) { return on("end", cb); },
  };
})();
