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
//   LevainMic.stop()           flush, close the session, release every track; the final text arrives through onFinal
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

  function probe() {
    if (probing) return probing;
    probing = window.fetch("/cockpit/voice.json", { headers: headers() }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) {
      streamOk = false; provider = "";
      if (j && j.stream === true && Array.isArray(j.providers)) {
        var p = j.providers.filter(function (x) { return x && typeof x.name === "string" && x.key === "set" && Array.isArray(x.uses) && x.uses.indexOf("stt") >= 0; })[0];
        if (p) { provider = p.name; streamOk = true; }
      }
      return available();
    }, function () { return false; });
    return probing;
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
  function postJson(path, body) {
    var opts = { method: "POST", headers: headers() };
    if (body !== undefined) { opts.body = body; opts.headers["Content-Type"] = "application/octet-stream"; }
    return window.fetch(path, opts).then(function (r) {
      if (!r.ok) throw new Error("status");
      return r.json();
    });
  }
  function releaseCapture(s) {
    if (s.released) return;
    s.released = true;
    try { if (s.node) { if (s.node.port) s.node.port.onmessage = null; s.node.onaudioprocess = null; s.node.disconnect(); } } catch (e) { /* gone */ }
    try { if (s.src) s.src.disconnect(); } catch (e) { /* gone */ }
    try { if (s.stream) s.stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* ended */ } }); } catch (e) { /* none */ }
    try { if (s.ctx && s.ctx.close) { var c = s.ctx.close(); if (c && c.catch) c.catch(function () {}); } } catch (e) { /* closed */ }
    clearTimeout(s.timer);
    document.removeEventListener("visibilitychange", s.onVis);
    window.removeEventListener("pagehide", s.onHide);
  }
  function finish(s) {
    if (s.ended) return;
    if (S === s) S = null;
    s.ended = true;
    emit("end");
  }
  function enqueue(s, bytes) {
    if (s.dead) return;
    var seq = s.seq++;
    s.q = s.q.then(function () {
      if (s.dead) return;
      return postJson(BASE + "chunk?sid=" + encodeURIComponent(s.sid) + "&seq=" + seq, bytes).then(function (j) {
        if (s.dead) return;
        var fin = j && typeof j.final === "string" ? j.final : "", par = j && typeof j.partial === "string" ? j.partial : "";
        var text = (fin && par ? fin + " " + par : fin + par);
        if (text !== s.last) { s.last = text; emit("partial", text); }
      });
    }).catch(function () {
      if (s.dead) return;
      s.dead = true; releaseCapture(s);
      emit("note", WORDS.broke);
      closeSession(s, false);    // best effort: the server settles the meter on close
    });
  }
  function closeSession(s, wantFinal) {
    if (!s.sid) { finish(s); return Promise.resolve(); }
    return postJson(BASE + "close?sid=" + encodeURIComponent(s.sid)).then(function (j) {
      if (wantFinal && j && typeof j.text === "string") emit("final", j.text);
    }, function () { /* the server ends an idle session itself */ }).then(function () { finish(s); });
  }
  function stop() {
    var s = S;
    if (!s || s.stopping) return;
    s.stopping = true;
    releaseCapture(s);
    if (s.chunker) s.chunker.flush();
    s.openP.then(function () {
      if (s.dead) { if (!s.ended) finish(s); return; }
      return s.q.then(function () { return s.dead ? finish(s) : closeSession(s, true); });
    });
  }
  function start() {
    if (S || !available()) return;
    var s = S = { sid: "", seq: 0, q: null, openP: null, dead: false, stopping: false, released: false, ended: false, last: "" };
    var AC = window.AudioContext || window.webkitAudioContext;
    // the context and the permission are both asked for here, inside the tap
    try { s.ctx = new AC(); if (s.ctx.resume) s.ctx.resume(); } catch (e) { S = null; emit("note", WORDS.openFailed); emit("end"); return; }
    var gum;
    try { gum = window.navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } }); }
    catch (e) { gum = Promise.reject(e); }
    var fail = function () { if (S === s) { s.dead = true; releaseCapture(s); emit("note", WORDS.openFailed); finish(s); } };
    s.onVis = function () { if (document.visibilityState === "hidden") stop(); };
    s.onHide = function () { stop(); };
    document.addEventListener("visibilitychange", s.onVis);
    window.addEventListener("pagehide", s.onHide);
    s.openP = new Promise(function (resolve) {
      gum.then(function (stream) {
        s.stream = stream;
        if (S !== s || s.stopping) { releaseCapture(s); resolve(); if (S === s) finish(s); return null; }
        return wire(s).then(function () {
          s.timer = setTimeout(stop, MAX_MS);
          return postJson(BASE + "open?provider=" + encodeURIComponent(provider)).then(function (j) {
            if (!j || typeof j.sid !== "string" || !j.sid || j.sid.length > 128) throw new Error("sid");
            s.sid = j.sid;
          });
        });
      }).then(resolve, function () { fail(); resolve(); });
    });
    s.q = s.openP;
  }
  // build the capture graph: an AudioWorklet where there is one, else the deprecated ScriptProcessor
  function wire(s) {
    var ctx = s.ctx;
    s.src = ctx.createMediaStreamSource(s.stream);
    s.chunker = makeChunker(ctx.sampleRate, function (bytes) { enqueue(s, bytes); });
    if (ctx.audioWorklet && typeof window.AudioWorkletNode === "function") {
      return ctx.audioWorklet.addModule("/mic_worklet.js").then(function () {
        if (s.released) return;
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
    available: available, probe: probe, start: start, stop: stop, active: function () { return !!S; },
    onPartial: function (cb) { return on("partial", cb); }, onFinal: function (cb) { return on("final", cb); },
    onNote: function (cb) { return on("note", cb); }, onEnd: function (cb) { return on("end", cb); },
  };
})();
