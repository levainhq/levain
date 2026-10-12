/* cockpit_stream.js: keeps the cockpit page current without a reload.
 *
 * One stream per visible page: GET /cockpit/events, read with fetch() + a ReadableStream (an EventSource
 * cannot send the credential header), carrying the credential the page's own reads carry. The server names
 * which panels moved (`panel-changed {id, etag}`) or that the set of panels did (`manifest-changed`); this
 * file refetches a moved panel through LevainCockpitView.refetchPanel and reloads the board when it must.
 *
 * refetchPanel answers "applied" (the panel took the new data), "fault" (the read failed: retried on a
 * short bounded schedule, then it waits for the next event) or false (the panel cannot be redrawn in place:
 * the board reloads). A wanted reload is PENDING: it survives an in-flight load or an open edit and clears
 * only once the board has been drawn again.
 *
 * Connections are scarce (a browser gives an origin only a handful), so a hidden tab closes its stream and a
 * visible one reconnects, naming the last event it saw so the server can replay or ask for a resync.
 * While the stream is down the page polls the manifest with a validator and refetches what moved. */
(function (root) {
  "use strict";
  var EVENTS = "/cockpit/events";
  var MANIFEST = "/cockpit/manifest.json";
  var HEARTBEAT_MS = 15000;          // silence past two beats means the stream is dead
  var POLL_MS = 30000;
  var BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
  var FAULT_RETRY_MS = [5000, 15000, 60000];
  var PENDING_RETRY_MS = [5000, 15000, 60000];   // the last step repeats while a reload is still pending
  var RELOAD_GAP_MS = 10000;
  var DRAW_WAIT_MS = 30000;          // how long the stream waits for a first draw before saying it is not live

  var T = null, V = null;
  var chip = null;
  var abort = null, watchdog = 0, retryTimer = 0, pollTimer = 0, reloadTimer = 0;
  var attempt = 0, staleSince = null, epoch = null, started = false, paused = false, gaveUp = false;
  var lastId = null, pollTag = null;
  var pending = false, pendingAttempt = 0, lastReload = 0;
  var faults = Object.create(null);   // panel id -> { n, timer }

  function hhmm(d) { return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  function paintChip() {
    if (!chip) return;
    if (gaveUp) { chip.dataset.state = "off"; chip.textContent = "not live"; }
    else if (paused) { chip.dataset.state = "off"; chip.textContent = "paused"; }
    else if (staleSince) { chip.dataset.state = "stale"; chip.textContent = "stale since " + hhmm(staleSince); }
    else if (epoch) { chip.dataset.state = "live"; chip.textContent = "live"; }
    else { chip.dataset.state = "connecting"; chip.textContent = "connecting"; }
  }
  function ensureChip() {
    if (chip && chip.isConnected) return;
    var host = document.querySelector(".indicators");
    if (!host) return;
    chip = document.createElement("span");
    chip.className = "livechip";
    chip.setAttribute("role", "status");
    chip.title = "whether the page is told when a panel changes";
    host.insertBefore(chip, host.firstChild);
    paintChip();
  }

  function held() { return root.LevainCockpitHeld || {}; }
  function readHeaders() {
    var h = {};
    try { var src = T.headers ? T.headers() : {}; Object.keys(src || {}).forEach(function (k) { h[k] = src[k]; }); } catch (_) { /* no credential to carry */ }
    return h;
  }

  // ---- a failed panel read: retried a few times, then it waits for the next event ----
  function clearFault(id) {
    var f = faults[id];
    if (f) { root.clearTimeout(f.timer); delete faults[id]; }
  }
  function cancelFaults() { Object.keys(faults).forEach(clearFault); }
  function scheduleFault(id) {
    var n = faults[id] ? faults[id].n : 0;
    clearFault(id);
    if (n >= FAULT_RETRY_MS.length) return;
    var f = { n: n + 1, timer: 0 };
    f.timer = root.setTimeout(function () { if (faults[id] === f) { f.timer = 0; refetch(id, f.n); } }, FAULT_RETRY_MS[n]);
    faults[id] = f;
  }

  // ---- a wanted reload is pending until the board has been drawn again ----
  function wantReload() {
    pending = true;
    if (reloadTimer) return;
    var wait = pendingAttempt === 0
      ? Math.max(500, RELOAD_GAP_MS - (Date.now() - lastReload))
      : PENDING_RETRY_MS[Math.min(pendingAttempt - 1, PENDING_RETRY_MS.length - 1)];
    reloadTimer = root.setTimeout(flushReload, wait);
  }
  function flushReload() {
    reloadTimer = 0;
    if (!pending) return;
    if (document.hidden) return;        // the visible edge flushes it
    lastReload = Date.now();
    var p;
    try { p = T.reload(); } catch (_) { p = false; }
    Promise.resolve(p).then(function (ok) { return ok === true; }, function () { return false; }).then(function (ok) {
      if (ok) { pending = false; pendingAttempt = 0; cancelFaults(); return; }
      pendingAttempt++;
      wantReload();
    });
  }

  // the settled answer of one refetch: "applied", "fault" or false
  function refetch(id) {
    var done;
    try { done = V.refetchPanel(id); } catch (_) { done = Promise.resolve("fault"); }
    return Promise.resolve(done).then(function (r) {
      if (r === "applied" || r === true) clearFault(id);
      else if (r === "fault") scheduleFault(id);
      else wantReload();
      return r;
    }, function () { scheduleFault(id); return "fault"; });
  }
  function onEvent(id) { clearFault(id); return refetch(id); }

  function resyncAll() { Object.keys(held()).forEach(function (id) { onEvent(id); }); }

  // ---- the frame parser: events are blank-line separated; `:` lines are comments (the heartbeat) ----
  function onFrame(text) {
    var event = "message", data = [], id = null;
    text.split("\n").forEach(function (line) {
      if (!line || line.charAt(0) === ":") return;
      var i = line.indexOf(":");
      var field = i < 0 ? line : line.slice(0, i);
      var val = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = val;
      else if (field === "data") data.push(val);
      else if (field === "id") id = val;
    });
    if (!data.length) return;
    var body;
    try { body = JSON.parse(data.join("\n")); } catch (_) { return; }
    if (id) lastId = id;
    if (event === "hello") {
      epoch = body.epoch || "?";
      staleSince = null;
      attempt = 0;
      stopPoll();
      paintChip();
      if (body.resync) resyncAll();
    } else if (event === "panel-changed" && body && body.id) {
      onEvent(body.id);
    } else if (event === "manifest-changed") {
      wantReload();
    }
  }

  function arm() {
    root.clearTimeout(watchdog);
    watchdog = root.setTimeout(function () { if (abort) abort.abort(); }, HEARTBEAT_MS * 2 + 2000);
  }

  function drop() {
    root.clearTimeout(watchdog);
    abort = null;
    if (paused) return;
    if (!staleSince) staleSince = new Date();
    paintChip();
    startPoll();
    var wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt++;
    root.clearTimeout(retryTimer);
    retryTimer = root.setTimeout(connect, wait);
  }

  function connect() {
    if (paused) return;
    ensureChip();
    var ctl = new AbortController();
    abort = ctl;
    arm();
    var headers = readHeaders();
    if (lastId) headers["Last-Event-ID"] = lastId;
    fetch(EVENTS, { cache: "no-store", headers: headers, signal: ctl.signal }).then(function (res) {
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) throw new Error("stream ended");
          arm();
          buf += dec.decode(r.value, { stream: true });
          var cut;
          while ((cut = buf.indexOf("\n\n")) >= 0) { onFrame(buf.slice(0, cut)); buf = buf.slice(cut + 2); }
          return pump();
        });
      }
      return pump();
    }).catch(function () { /* any end of the stream is the same event */ }).then(function () {
      if (abort === ctl) drop();
    });
  }

  // ---- while the stream is down: the manifest, with a validator; refetch what moved ----
  function diffManifest(m) {
    var heads = (m && m.panels) || {}, h = held();
    var ids = Object.keys(heads), have = Object.keys(h);
    if (ids.length !== have.length || ids.some(function (id) { return !(id in h); })) { wantReload(); return; }
    ids.forEach(function (id) {
      if (h[id] && h[id].etag === heads[id].etag) return;
      onEvent(id).then(function (r) { if (r === "applied" && h[id]) h[id].etag = heads[id].etag; });
    });
  }
  function pollOnce() {
    if (paused) return;
    var headers = readHeaders();
    if (pollTag) headers["If-None-Match"] = pollTag;
    fetch(MANIFEST, { cache: "no-store", headers: headers }).then(function (res) {
      if (res.status === 304) return null;
      if (!res.ok) throw new Error("HTTP " + res.status);
      var tag = res.headers.get("ETag");
      return res.json().then(function (m) { pollTag = tag; diffManifest(m); });
    }).catch(function () { /* still down: the chip already says so */ });
  }
  function startPoll() { if (!pollTimer) pollTimer = root.setInterval(pollOnce, POLL_MS); }
  function stopPoll() { if (pollTimer) { root.clearInterval(pollTimer); pollTimer = 0; } pollTag = null; }

  // ---- a hidden tab gives its connection back; a visible one takes it again ----
  var owed = [];
  function pause() {
    if (paused) return;
    paused = true;
    owed = Object.keys(faults);
    cancelFaults();
    root.clearTimeout(retryTimer);
    root.clearTimeout(watchdog);
    stopPoll();
    if (abort) { var a = abort; abort = null; a.abort(); }
    epoch = null;
    paintChip();
  }
  function resume() {
    if (!paused) return;
    paused = false;
    attempt = 0;
    staleSince = null;
    paintChip();
    var o = owed; owed = [];
    o.forEach(onEvent);
    if (pending) wantReload();
    if (gaveUp) { if (root.LevainCockpitHeld) { gaveUp = false; connect(); } else whenDrawn(Date.now() + DRAW_WAIT_MS); return; }
    connect();
  }

  // the first hello is compared with what the board was drawn from, so it waits for the first draw
  function whenDrawn(deadline) {
    if (root.LevainCockpitHeld) { gaveUp = false; if (document.hidden) { paused = true; paintChip(); } else connect(); return; }
    if (Date.now() > deadline) { gaveUp = true; paused = true; paintChip(); return; }
    root.setTimeout(function () { whenDrawn(deadline); }, 200);
  }

  function start() {
    if (started) return;
    T = root.LevainCockpitTransport;
    V = root.LevainCockpitView;
    if (!T || !V || typeof V.refetchPanel !== "function" || typeof root.fetch !== "function" || typeof root.AbortController !== "function") return;
    // the legacy substrate page draws no cockpit panels
    if (new URLSearchParams(root.location.search).get("source") === "substrate") return;
    started = true;
    ensureChip();
    paintChip();
    document.addEventListener("visibilitychange", function () { if (document.hidden) pause(); else resume(); });
    whenDrawn(Date.now() + DRAW_WAIT_MS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})(window);
