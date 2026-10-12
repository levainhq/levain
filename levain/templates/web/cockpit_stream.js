/* cockpit_stream.js: keeps the cockpit page current without a reload.
 *
 * One stream per page: GET /cockpit/events, read with fetch() + a ReadableStream (an EventSource cannot
 * send the credential header), carrying the credential the page's own reads carry. The stream names which
 * panels moved; this file refetches them through LevainCockpitView.refetchPanel. Every (re)connect opens
 * with `hello` (the whole freshness map), so a dropped stream, a restarted server and a first connect are
 * all healed the same way: refetch whatever differs from what the page holds.
 *
 * The chip is honest: "live" only while the stream is up and heard from; "stale since HH:MM" from the
 * moment it is not. While down the page polls freshness.json and reconnects with backoff. */
(function (root) {
  "use strict";
  var EVENTS = "/cockpit/events";
  var FRESHNESS = "/cockpit/freshness.json";
  var HEARTBEAT_MS = 15000;          // silence past two beats means the stream is dead
  var POLL_MS = 30000;
  var BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
  var RELOAD_GAP_MS = 10000;

  var T = null, V = null;
  var chip = null;
  var abort = null, watchdog = 0, retryTimer = 0, pollTimer = 0, reloadTimer = 0;
  var attempt = 0, staleSince = null, epoch = null, lastReload = 0, started = false;

  function hhmm(d) { return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  function paintChip() {
    if (!chip) return;
    if (staleSince) { chip.dataset.state = "stale"; chip.textContent = "stale since " + hhmm(staleSince); }
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

  function same(a, b) { return !!a && !!b && a.as_of === b.as_of && a.status === b.status; }
  function held() { return root.LevainCockpitHeld || {}; }

  // one debounced full reload: a refetch that declined (the panel's shape changed, or it is not drawn here)
  function wantReload() {
    if (reloadTimer) return;
    var wait = Math.max(500, RELOAD_GAP_MS - (Date.now() - lastReload));
    reloadTimer = root.setTimeout(function () {
      reloadTimer = 0;
      lastReload = Date.now();
      try { T.reload(); } catch (_) { /* the next change asks again */ }
    }, wait);
  }

  function refetch(id, entry) {
    var done;
    try { done = V.refetchPanel(id); } catch (_) { done = Promise.resolve(false); }
    return Promise.resolve(done).then(function (ok) {
      if (ok === false) { wantReload(); return; }
      held()[id] = { as_of: entry.as_of || null, status: entry.status || "error" };
    }, function () { wantReload(); });
  }

  function reconcile(map) {
    var h = held();
    Object.keys(map || {}).forEach(function (id) { if (!same(h[id], map[id])) refetch(id, map[id]); });
  }

  // ---- the frame parser: events are blank-line separated; `:` lines are comments (the heartbeat) ----
  function onFrame(text) {
    var event = "message", data = [];
    text.split("\n").forEach(function (line) {
      if (!line || line.charAt(0) === ":") return;
      var i = line.indexOf(":");
      var field = i < 0 ? line : line.slice(0, i);
      var val = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = val;
      else if (field === "data") data.push(val);
    });
    if (!data.length) return;
    var body;
    try { body = JSON.parse(data.join("\n")); } catch (_) { return; }
    if (event === "hello") {
      epoch = body.epoch || "?";
      staleSince = null;
      attempt = 0;
      stopPoll();
      paintChip();
      reconcile(body.freshness);
    } else if (event === "panel-changed" && body && body.id) {
      if (!same(held()[body.id], body)) refetch(body.id, body);
    }
  }

  function arm() {
    root.clearTimeout(watchdog);
    watchdog = root.setTimeout(function () { if (abort) abort.abort(); }, HEARTBEAT_MS * 2 + 2000);
  }

  function drop() {
    root.clearTimeout(watchdog);
    abort = null;
    if (!staleSince) staleSince = new Date();
    paintChip();
    startPoll();
    var wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt++;
    root.clearTimeout(retryTimer);
    retryTimer = root.setTimeout(connect, wait);
  }

  function connect() {
    ensureChip();
    var ctl = new AbortController();
    abort = ctl;
    arm();
    var headers = {};
    try { headers = T.headers ? T.headers() : {}; } catch (_) { /* no credential to carry */ }
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

  // ---- while the stream is down: read freshness.json and refetch what moved ----
  function pollOnce() {
    T.readJson(FRESHNESS).then(reconcile, function () { /* still down: the chip already says so */ });
  }
  function startPoll() { if (!pollTimer) pollTimer = root.setInterval(pollOnce, POLL_MS); }
  function stopPoll() { if (pollTimer) { root.clearInterval(pollTimer); pollTimer = 0; } }

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
    // the first hello is compared with what the board was drawn from, so it waits for the first draw
    (function whenDrawn() { if (root.LevainCockpitHeld) connect(); else root.setTimeout(whenDrawn, 200); })();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})(window);
