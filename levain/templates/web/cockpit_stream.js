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
 * While the stream is down the page polls the manifest with a validator and refetches what moved.
 *
 * The stream does not paint anything. It keeps the facts the masthead's freshness line needs (its state, the last bytes, the last good
 * manifest read, the heads it carried) and says `levain:fresh` when its own state changes; fresh_line.js reads them through
 * window.LevainStreamFacts and writes the line (#freshline). A heartbeat updates `lastChunk` and tells no one. The outcome of a PANEL read
 * (applied, failed) is published by refetchPanel, the one place that knows it; the stream only schedules the retries. */
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
  var HELLO_MS = 8000;               // a connection that has not said hello by now is dead: it is dropped and the retry runs
  var DRAW_WAIT_MS = 30000;          // how long the stream waits for a first draw before it gives up (state "off")

  var T = null, V = null;
  var abort = null, watchdog = 0, retryTimer = 0, pollTimer = 0, reloadTimer = 0;
  var attempt = 0, staleSince = null, epoch = null, started = false, paused = false, gaveUp = false, everLive = false;
  var lastId = null, pollTag = null, lastConnect = 0, lastChunk = 0;
  var pending = false, pendingAttempt = 0, lastReload = 0, reloadGen = 0, reloading = false;
  var withPosition = false;           // this connection named a last event, so its hello may be a replay
  var faults = Object.create(null);   // panel id -> { timer }: the pending retry of a panel whose read failed (what failed is the held record's `err`)
  var tries = Object.create(null);    // panel id -> retries taken since its last cause; a whole draw cancels timers but never this, or a panel that stays unavailable would reload the board forever
  var bytesAt = 0;                    // the last bytes of any connection (lastChunk restarts with each connection; this does not)
  var pollOkAt = 0, lastHeads = null, lastHeadsAt = 0;   // the last good manifest read (a 304 counts), the heads it carried and when that read STARTED
  var headSeq = 0, headApplied = 0;                      // manifest reads are numbered by their start: the newer start holds the heads, whichever answers last

  // the stream's state as the line names it: off (gave up waiting for a first draw), paused (hidden tab), down, live, connecting; null when
  // this page never runs a stream (the line then has no stream words to say)
  function streamState() {
    if (!started) return null;
    if (gaveUp) return "off";
    if (paused) return "paused";
    if (staleSince) return "down";
    return epoch ? "live" : "connecting";
  }
  // a panel the page has read since the manifest read that carried its head is described by that read (its status and read time), not by the head
  function headsNow() {
    if (!lastHeads) return null;
    var h = held(), out = {};
    Object.keys(lastHeads).forEach(function (id) {
      var rec = h[id], head = lastHeads[id], nx = {};
      if (rec && !rec.err && rec.at >= lastHeadsAt) {
        if (typeof rec.status === "string") nx.status = rec.status;
        if (typeof rec.as_of === "string") nx.as_of = rec.as_of;
      }
      out[id] = Object.assign({}, head, nx);
    });
    return out;
  }
  function facts() {
    var h = held();
    return { state: streamState(), everLive: everLive, since: staleSince ? staleSince.getTime() : null, lastChunk: bytesAt, pollOkAt: pollOkAt,
      faults: Object.keys(h).filter(function (id) { return !!(h[id] && h[id].err); }), heads: headsNow(), headsAt: lastHeadsAt };
  }
  root.LevainStreamFacts = facts;
  // tell the line that a fact moved (a state change, a read that settled): never a heartbeat
  function tell() {
    try { if (typeof document.dispatchEvent === "function" && typeof root.CustomEvent === "function") document.dispatchEvent(new root.CustomEvent("levain:fresh")); } catch (_) { /* no line to tell */ }
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
    var n = tries[id] || 0;
    clearFault(id);
    if (n >= FAULT_RETRY_MS.length) return;
    // a retry started while hidden would run unseen and spend the budget: it is owed to the visible edge instead
    if (paused) { if (owed.indexOf(id) < 0) owed.push(id); return; }
    var f = { timer: 0 };
    // a step is spent when its timer fires: a timer cancelled before that (a whole draw, a hide) spends nothing
    f.timer = root.setTimeout(function () { if (faults[id] === f) { f.timer = 0; tries[id] = (tries[id] || 0) + 1; refetch(id); } }, FAULT_RETRY_MS[n]);
    faults[id] = f;
  }

  // ---- a wanted reload is pending until the board has been drawn again ----
  function wantReload() {
    pending = true;
    reloadGen++;
    if (reloadTimer || reloading) return;
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
    var startGen = reloadGen, p;
    reloading = true;
    try { p = T.reload(); } catch (_) { p = false; }
    Promise.resolve(p).then(function (ok) { return ok === true; }, function () { return false; }).then(function (ok) {
      reloading = false;
      if (ok && startGen === reloadGen) { pending = false; pendingAttempt = 0; return; }
      // a request made while this render was under way is not covered by it
      pendingAttempt = ok ? 0 : pendingAttempt + 1;
      wantReload();
    });
  }

  // the settled answer of one refetch: "applied", "superseded" (a newer refetch owns the panel: nothing to clear), "fault" or false.
  // refetchPanel has recorded the read and told the line; what is decided here is only the retry
  function refetch(id) {
    var done;
    try { done = V.refetchPanel(id); } catch (_) { done = Promise.resolve("fault"); }
    return Promise.resolve(done).then(function (r) {
      if (r === "superseded") return r;
      if (r === "applied" || r === true) { clearFault(id); delete tries[id]; }     // refetchPanel recorded the etag of its own panel read; the announced one is another credential's
      else if (r === "fault") scheduleFault(id);
      else wantReload();
      return r;
    }, function () { scheduleFault(id); return "fault"; });
  }
  // a stream event frame for a panel is a new cause: its retry budget starts over
  function onEvent(id) { delete tries[id]; return reread(id); }
  // a read the page chose to make (a manifest diff, the visible edge) is not a cause: it spends from the budget it finds
  function reread(id) { clearFault(id); return refetch(id); }

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
      everLive = true;
      staleSince = null;
      attempt = 0;
      stopPoll();
      tell();
      // anything but a ring replay is compared with the server's manifest
      if (body.resync || !withPosition) reconcile(false);
    } else if (event === "panel-changed" && body && body.id) {
      onEvent(body.id);
    } else if (event === "manifest-changed") {
      wantReload();
    }
  }

  function arm(ms) {
    root.clearTimeout(watchdog);
    watchdog = root.setTimeout(function () { if (abort) abort.abort(); }, ms || HEARTBEAT_MS * 2 + 2000);
  }

  function drop() {
    root.clearTimeout(watchdog);
    abort = null;
    if (paused) return;
    if (!staleSince) staleSince = new Date();
    tell();
    startPoll();
    var wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt++;
    root.clearTimeout(retryTimer);
    retryTimer = root.setTimeout(connect, wait);
  }

  function connect() {
    if (paused) return;
    lastConnect = Date.now();
    lastChunk = 0;
    var ctl = new AbortController();
    abort = ctl;
    arm(HELLO_MS);
    var headers = readHeaders();
    withPosition = !!lastId;
    if (lastId) headers["Last-Event-ID"] = lastId;
    fetch(EVENTS, { cache: "no-store", headers: headers, signal: ctl.signal }).then(function (res) {
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) throw new Error("stream ended");
          arm();
          lastChunk = bytesAt = Date.now();
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

  // ---- the manifest against what the page holds: the poll while the stream is down, and every hello that is not a replay ----
  function diffManifest(m, startedAt) {
    var heads = (m && m.panels) || {}, h = held();
    lastHeads = heads; lastHeadsAt = startedAt;
    var ids = Object.keys(heads), have = Object.keys(h);
    if (ids.length !== have.length || ids.some(function (id) { return !(id in h); })) { wantReload(); return; }
    ids.forEach(function (id) {
      if (h[id] && h[id].etag === heads[id].etag) return;
      // a panel never read (held `err`, no etag) differs from every head, so this read is a retry taken early (it replaces the pending timer
      // and spends the step); once the budget is spent it waits for an event or a recovery
      if (h[id] && h[id].err && !h[id].etag) {
        var n = tries[id] || 0;
        if (n >= FAULT_RETRY_MS.length) return;
        tries[id] = n + 1;
      }
      reread(id);
    });
  }
  function reconcile(conditional) {
    if (paused) return;
    var headers = readHeaders(), seq = ++headSeq, startedAt = Date.now();
    if (conditional && pollTag) headers["If-None-Match"] = pollTag;
    fetch(MANIFEST, { cache: "no-store", headers: headers }).then(function (res) {
      if (res.status === 304) { pollOkAt = Date.now(); tell(); return null; }
      if (!res.ok) throw new Error("HTTP " + res.status);
      var tag = res.headers.get("ETag");
      return res.json().then(function (m) {
        pollOkAt = Date.now();     // a good read whichever it answers
        // an older request answering after a newer one has been applied describes an older manifest: it replaces nothing
        if (seq > headApplied) { headApplied = seq; if (conditional) pollTag = tag; diffManifest(m, startedAt); }
        tell();
      });
    }).catch(function () { /* the next hello or poll tries again */ });
  }
  function pollOnce() { reconcile(true); }
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
    tell();
  }
  function resume() {
    if (!paused) return;
    paused = false;
    attempt = 0;
    staleSince = null;
    tell();
    var o = owed; owed = [];
    o.forEach(function (id) { reread(id); });
    if (pending) wantReload();
    if (gaveUp) { if (root.LevainCockpitHeld) { gaveUp = false; connect(); } else whenDrawn(Date.now() + DRAW_WAIT_MS); return; }
    connect();
  }

  // the app came back to the front (pwa_resume.js) without the page having been hidden first (a frozen page, a restored one):
  // a stream that is not live is dead or stuck, so it is replaced now rather than at its watchdog
  document.addEventListener("levain:resume", function () {
    // gaveUp: still waiting for the board's first draw; the stream starts from that draw (onDrawn), never from here
    if (!started || paused || gaveUp || document.hidden) return;
    tell();
    // the visible edge just opened a stream (resume() above): one is enough
    if (Date.now() - lastConnect < 1500) return;
    // a stream that has spoken within a heartbeat is live: leave it (no reconnect, no hello replay)
    if (abort && lastChunk && Date.now() - lastChunk <= HEARTBEAT_MS) return;
    // otherwise whatever is open crossed a freeze nobody announced, and cannot be trusted as live
    root.clearTimeout(retryTimer);
    if (abort) { var a = abort; abort = null; a.abort(); }
    attempt = 0;
    connect();
  });

  // the first hello is compared with what the board was drawn from, so it waits for the first draw
  function whenDrawn(deadline) {
    if (root.LevainCockpitHeld) { gaveUp = false; if (document.hidden) { paused = true; tell(); } else connect(); return; }
    if (Date.now() > deadline) { gaveUp = true; paused = true; tell(); return; }
    root.setTimeout(function () { whenDrawn(deadline); }, 200);
  }

  // the board's first successful draw after the wait gave up (or the view arrived late) starts the stream
  function onDrawn() {
    cancelFaults();     // the board was drawn whole from fresh reads: the retries it supersedes are cancelled
    if (!started) start();
    else if (gaveUp) { paused = false; whenDrawn(Date.now() + DRAW_WAIT_MS); }
    // a panel that whole read could not read (drawn as unavailable) enters the same bounded retry as a refetch that failed
    if (started) {
      sweepTries();
      var h = held();
      Object.keys(h).forEach(function (id) { if (h[id] && h[id].err) scheduleFault(id); });
    }
  }
  // a panel whose held record has no `err` has recovered, by whatever path read it: its budget starts over
  function sweepTries() {
    var h = held();
    Object.keys(tries).forEach(function (id) { if (!(h[id] && h[id].err)) delete tries[id]; });
  }
  document.addEventListener("levain:cockpit-held", onDrawn);
  // refetchPanel says this after every settled panel read, a direct caller's (a write's refetch, the topology replay) included; it only deletes
  document.addEventListener("levain:fresh", sweepTries);

  function start() {
    if (started) return;
    T = root.LevainCockpitTransport;
    V = root.LevainCockpitView;
    if (!T || !V || typeof V.refetchPanel !== "function" || typeof root.fetch !== "function" || typeof root.AbortController !== "function") return;
    // the legacy substrate page draws no cockpit panels
    if (new URLSearchParams(root.location.search).get("source") === "substrate") return;
    started = true;
    tell();
    document.addEventListener("visibilitychange", function () { if (document.hidden) pause(); else resume(); });
    whenDrawn(Date.now() + DRAW_WAIT_MS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})(window);
