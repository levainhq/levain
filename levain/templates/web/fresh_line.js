/* fresh_line.js: the masthead's freshness line (levain#159). It replaced the Sync button: instead of a key that re-reads, the page says how
 * current it is. It paints one element, #freshline (the .livechip in dashboard.html), from ONE function, freshFacts(inputs):
 *
 *   { confirmedAt, lastReadAt, serverOffsetMs, read: {state, why, inflightAt}, stream: {state, everLive},
 *     panels: [{id, title, kind, asOfMs, onServerClock, stale, pending}], worker: "live" | "stale" | "down" | null }
 *
 * v1 fills it from what the boot shim and the change stream already know and from the manifest heads' `status` / `as_of`; `worker` is
 * null. When the server publishes snapshot freshness only freshFacts() changes (it reads the heads' `stale`, `pending`, `valid_until`
 * and the manifest's `freshness`); decide() and the painter below read the facts and nothing else.
 *
 * Reading cost. The line is repainted from events (the shim's and the stream's `levain:fresh`, the tab coming back) and never from a
 * heartbeat. A clock runs only while a trouble state shows (ONE interval, TICK_FAST_MS steps until FAST_UNTIL_MS, then TICK_SLOW_MS) and
 * is cleared when the state clears or the tab hides. In the fine state it writes nothing (a write happens only when the text differs)
 * and the only timer is the one an in-flight read arms for its "updating" cue (UPDATING_MS); a worse state also arms its hold.
 * On a phone-width screen the age is written in its short form (STRINGS.ageShort) so its number survives beside the state words.
 *
 * Speech. The state words sit alone in the role="status" span; the age and the "updating" cue sit in a sibling span outside it, so a
 * ticking age is never announced. The words are written only when the state changes. A worse state, and any entry into "down" (whatever
 * state it comes from), is held HOLD_MS before it is written (a stream reconnect flap does not speak); a hold pending when the tab hides
 * is dropped and judged afresh when it is seen. A recovery to live is spoken only if the worse state was; the first connecting-to-live
 * is silent. */
(function (root) {
  "use strict";

  // every word the line can show, in one place; {name}, {n} and {age} are filled by decide()
  var STRINGS = {
    connecting: "connecting",
    live: "live",
    refused: "not signed in",
    failed: "can't reach",
    down: "stream down",
    staleOne: "{name} stale",
    staleMore: "{name} stale +{n}",
    errorOne: "{name} unavailable",
    errorMore: "{name} unavailable +{n}",
    ageUpdated: "updated {age} ago",
    agePanel: "read {age} ago",
    justNow: "just now",
    ageUpdatedNow: "updated just now",
    agePanelNow: "read just now",
    agePanelNever: "never read",
    ageShort: "{age}",
    justNowShort: "<5s",
    updating: "updating",
    titleLive: "Live: the page is told when a panel changes.",
    titleConnecting: "Connecting: the board is being read.",
    titleRefused: "This device is not signed in to the cockpit; it is read again when the app comes back to the front.",
    titleFailed: "The last read failed ({why}); it is tried again.",
    titleDown: "The change stream is down; the page polls for changes until it is back.",
    titleStale: "A panel's data is old: {names}.",
    titleError: "A panel could not be read: {names}.",
    titleRead: "Last read {clock}.",
    titleUpdated: "Updated {age} ({clock}).",
    sep: " "
  };

  var HOLD_MS = 3000;          // a worse state waits this long before it is written
  var HOLD_FIRST_MS = 9000;    // the stream's first connection, never yet live, waits longer: a slow link's hello is not "stream down"
  var PHONE_QUERY = "(max-width: 720px)";   // where the age is written in its short form
  var UPDATING_MS = 1000;      // a read shorter than this shows no cue
  var TICK_FAST_MS = 5000, TICK_SLOW_MS = 30000, FAST_UNTIL_MS = 60000;
  // worse = higher; a state moves to a lower rank at once and to a higher one after its hold (and "down" is always entered after one)
  var RANK = { live: 0, connecting: 1, stale: 2, down: 3, failed: 4, refused: 5 };
  var AGED = { stale: true, down: true, failed: true };   // the states that show an age and so need the clock

  // ---- timestamps: an ISO string (the manifest head today) or epoch seconds or ms (a number), read as ms; null when it is neither ----
  function toMs(x) {
    if (typeof x === "number") return isFinite(x) && x > 0 ? (x < 1e11 ? Math.round(x * 1000) : x) : null;
    if (typeof x === "string" && x) {
      if (/^\d+(\.\d+)?$/.test(x)) return toMs(Number(x));
      var t = Date.parse(x);
      return isNaN(t) ? null : t;
    }
    return null;
  }

  // "just now" under 5 s, then 5 s steps to a minute, whole minutes to an hour, then hours
  function fmtAge(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    if (s < 5) return STRINGS.justNow;
    if (s < 60) return (Math.floor(s / 5) * 5) + "s";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h";
  }
  // an age as it reads inside a sentence: "40s ago", "just now"
  function ageWords(ms) { var a = fmtAge(ms); return a === STRINGS.justNow ? a : a + " ago"; }
  function fill(tpl, vars) { return String(tpl).replace(/\{(\w+)\}/g, function (_, k) { return vars[k] === undefined ? "" : String(vars[k]); }); }
  function clockOf(ms) {
    try { return new Date(ms).toLocaleTimeString(); } catch (_) { return ""; }
  }

  // ---- the facts: pure over its inputs ----
  // inputs: { now, boot: {wholeDrawn, lastWholeAt, inflight, inflightAt, refused, failed}, stream: {state, since, lastChunk, pollOkAt,
  //           faults: [ids], everLive}, held: {id: {etag, at}}, heads: {id: {as_of, status}}, serverOffsetMs, titleOf: fn(id) }
  function freshFacts(inputs) {
    var inp = inputs || {};
    var boot = inp.boot || {}, stream = inp.stream || {}, held = inp.held || {}, heads = inp.heads || {};
    var faulted = Object.create(null);
    (stream.faults || []).forEach(function (id) { faulted[id] = true; });
    var confirmed = [boot.lastWholeAt, stream.pollOkAt, stream.lastChunk].filter(function (v) { return typeof v === "number" && v > 0; });
    var lastRead = [boot.lastWholeAt, stream.pollOkAt].filter(function (v) { return typeof v === "number" && v > 0; });
    Object.keys(held).forEach(function (id) {
      var at = held[id] && held[id].at;
      if (typeof at === "number" && at > 0) { confirmed.push(at); lastRead.push(at); }
    });
    var max = function (a) { return a.length ? Math.max.apply(null, a) : null; };
    var ids = Object.keys(heads);
    Object.keys(faulted).forEach(function (id) { if (ids.indexOf(id) < 0) ids.push(id); });
    var titleOf = typeof inp.titleOf === "function" ? inp.titleOf : function (id) { return id; };
    var panels = ids.map(function (id) {
      var h = heads[id] || {};
      // a panel whose very first read failed has no body to call old: it is unavailable, as an error head is
      var neverRead = faulted[id] === true && !!held[id] && held[id].etag === null;
      var kind = h.status === "error" || neverRead ? "error" : (h.status === "stale" || faulted[id] === true ? "stale" : "");
      var stale = kind !== "";
      var asOf = toMs(h.as_of), onServer = true;
      // a panel the page could not read has aged from the page's own last SUCCESSFUL read of it, on the page's clock, not from the server's;
      // one never read has no age (the line says so), never the failed attempt's
      if (faulted[id]) { asOf = (held[id] && held[id].at) || null; onServer = false; }
      return { id: id, title: stale ? (titleOf(id) || id) : id, kind: kind, asOfMs: asOf || null, onServerClock: onServer, stale: stale, pending: false };
    });
    var read;
    // the read state is taken as the facts give it: whether a failure still stands is not decided here
    if (boot.refused) read = { state: "refused", why: "" };
    else if (boot.failed) read = { state: "failed", why: String(boot.failed) };
    else if (!boot.wholeDrawn) read = { state: "none", why: "" };
    else read = { state: "ok", why: "" };
    read.inflightAt = boot.inflight && boot.inflightAt ? boot.inflightAt : 0;
    return {
      confirmedAt: max(confirmed),
      lastReadAt: max(lastRead),
      serverOffsetMs: typeof inp.serverOffsetMs === "number" && isFinite(inp.serverOffsetMs) ? inp.serverOffsetMs : 0,
      read: read,
      stream: { state: stream.state || null, everLive: stream.everLive === true },
      panels: panels,
      worker: null
    };
  }

  // ---- the words: pure over the facts and the clock. First match wins; a read that has not drawn the board yet yields to a refusal or a failure ----
  // opts.short: the phone's forms (the age without its words)
  function decide(facts, now, opts) {
    var f = facts, key, words = "", age = "", ageFrom = null, title = "", short = !!(opts && opts.short), holdMs = HOLD_MS, ageNow = now;
    // oldest read first; a panel with no read time sorts last, and those keep the order they came in (a total order: no NaN)
    var when = function (p) { return p.asOfMs || Infinity; };
    var stale = f.panels.filter(function (p) { return p.stale; }).sort(function (a, b) { var x = when(a), y = when(b); return x === y ? 0 : (x < y ? -1 : 1); });
    var st = f.stream.state;
    // a paused stream (the tab has just come back) is not live either: it has not spoken yet
    var streamBad = st === "down" || st === "connecting" || st === "off" || st === "paused";
    if (f.read.state === "refused") key = "refused";
    else if (f.read.state === "failed") key = "failed";
    else if (f.read.state === "none") key = "connecting";
    else if (streamBad) { key = "down"; if ((st === "connecting" || st === "paused") && !f.stream.everLive) holdMs = HOLD_FIRST_MS; }
    else if (stale.length) key = "stale";
    else key = "live";

    if (key === "connecting") { words = STRINGS.connecting; title = STRINGS.titleConnecting; }
    else if (key === "refused") { words = STRINGS.refused; title = STRINGS.titleRefused; }
    // the age of a failed read counts from the last READ, never from heartbeats: a live stream says the line is up, not that the board is current
    else if (key === "failed") { words = STRINGS.failed; title = fill(STRINGS.titleFailed, { why: f.read.why }); ageFrom = f.lastReadAt; }
    else if (key === "down") { words = STRINGS.down; title = STRINGS.titleDown; ageFrom = f.confirmedAt; }
    else if (key === "stale") {
      var first = stale[0], more = stale.length - 1, bad = first.kind === "error";
      words = fill(more ? (bad ? STRINGS.errorMore : STRINGS.staleMore) : (bad ? STRINGS.errorOne : STRINGS.staleOne), { name: first.title, n: more });
      var names = function (kind) { return stale.filter(function (p) { return p.kind === kind; }).map(function (p) { return p.title; }).join(", "); };
      title = [names("stale") && fill(STRINGS.titleStale, { names: names("stale") }), names("error") && fill(STRINGS.titleError, { names: names("error") })].filter(Boolean).join(STRINGS.sep);
      ageFrom = first.asOfMs;
      // a head's read time is the server's clock: its age is measured against the server's now, as every other age on the page is
      if (first.onServerClock) ageNow = now + f.serverOffsetMs;
    } else { words = STRINGS.live; title = STRINGS.titleLive; }

    var ageText = "";
    if (AGED[key] && ageFrom) {
      var a = fmtAge(ageNow - ageFrom), fresh = a === STRINGS.justNow;
      if (short) ageText = fresh ? STRINGS.justNowShort : fill(STRINGS.ageShort, { age: a });
      else ageText = key === "stale" ? (fresh ? STRINGS.agePanelNow : fill(STRINGS.agePanel, { age: a })) : (fresh ? STRINGS.ageUpdatedNow : fill(STRINGS.ageUpdated, { age: a }));
    }
    else if (key === "stale" && stale[0].kind === "error") ageText = STRINGS.agePanelNever;   // an unavailable panel with no read time was never read
    // the first read says "connecting" already; the cue is for a re-read of a board that is up
    var updating = key !== "connecting" && !!(f.read.inflightAt && now - f.read.inflightAt >= UPDATING_MS);
    if (updating) age = STRINGS.updating; else age = ageText;
    // the absolute time of the last read rides in the title (it moves only with a read, never with a heartbeat)
    if (key === "live" && f.lastReadAt) title += STRINGS.sep + fill(STRINGS.titleRead, { clock: clockOf(f.lastReadAt) });
    else if (ageFrom && AGED[key]) title += STRINGS.sep + fill(STRINGS.titleUpdated, { age: ageWords(ageNow - ageFrom), clock: clockOf(ageFrom) });
    return { key: key, words: words, age: age, title: title, ageMs: ageFrom ? ageNow - ageFrom : null, ageFrom: ageFrom, holdMs: holdMs };
  }

  // ---- the page: gathers the inputs from what the shim, the stream and the view export ----
  function titleOfPanel(id) {
    try {
      var esc = root.CSS && root.CSS.escape ? root.CSS.escape(id) : String(id).replace(/"/g, "");
      var h = document.querySelector('[data-panel-id="' + esc + '"] .phead h2');
      // the heading adds only "(unavailable)" after the name (a count is a sibling pill, never in the h2 text): a parenthesis that is part of the title, a year included, stays
      var t = h && h.textContent ? h.textContent.replace(/\s*\(unavailable\)\s*$/, "").trim() : "";
      return t || id;
    } catch (_) { return id; }
  }
  function collectInputs(now) {
    var B = typeof root.LevainBootFacts === "function" ? root.LevainBootFacts() : {};
    var S = typeof root.LevainStreamFacts === "function" ? root.LevainStreamFacts() : {};
    var K = root.LevainCockpitKinds, heads = null, headsAt = 0;
    try { var fh = K && typeof K.freshHeads === "function" ? K.freshHeads() : null; if (fh) { heads = fh.heads; headsAt = fh.at; } } catch (_) { /* no view yet */ }
    // the stream's own manifest reads (a hello, a poll) are newer than the drawn board's whenever it has made one
    if (S && S.heads && (S.headsAt || 0) >= headsAt) heads = S.heads;
    var off = 0;
    try { if (K && typeof K.serverNow === "function") off = K.serverNow() - Date.now(); } catch (_) { /* no view yet */ }
    return { now: now, boot: B || {}, stream: S || {}, held: root.LevainCockpitHeld || {}, heads: heads || {}, serverOffsetMs: off, titleOf: titleOfPanel };
  }

  var el = null, stateEl = null, ageEl = null;
  var clock = function () { return Date.now(); };
  var inputsFn = null;                    // the test seam: facts supplied instead of read from the page
  // the timers, looked up when used so a test can hand the painter its own scheduler (the seam's setTimers)
  var T = {
    setInterval: function (f, ms) { return root.setInterval(f, ms); }, clearInterval: function (t) { root.clearInterval(t); },
    setTimeout: function (f, ms) { return root.setTimeout(f, ms); }, clearTimeout: function (t) { root.clearTimeout(t); }
  };
  var current = null;                     // what is written now: { key, words, age, title }
  var holdT = 0, holdKey = null, updT = 0, tickT = 0, tickMs = 0, spoke = false;
  var phone = null;                       // the phone-width media query, once the page has one
  var writes = [];                        // every write of the state words: { key, announce } (the test seam reads it)

  function worse(a, b) { return RANK[a] > RANK[b]; }
  function hiddenNow() { try { return !!document.hidden; } catch (_) { return false; } }

  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
  function setAttr(node, name, value) { if (node && node.getAttribute(name) !== value) node.setAttribute(name, value); }

  function paintWords(d, announce) {
    if (stateEl.textContent === d.words) return;
    if (announce) stateEl.removeAttribute("aria-live"); else stateEl.setAttribute("aria-live", "off");
    stateEl.textContent = d.words;
    writes.push({ key: d.key, announce: announce });
  }
  function paint(d, announce) {
    setAttr(el, "data-state", d.key);
    paintWords(d, announce);
    setText(ageEl, d.age);
    if (ageEl) { if (d.age) ageEl.removeAttribute("hidden"); else setAttr(ageEl, "hidden", ""); }
    setAttr(el, "title", d.title);
    current = d;
  }
  // the age half only (the state is unchanged): the clock's tick, an "updating" cue, a new read's time in the title
  function paintAge(d) {
    setText(ageEl, d.age);
    if (ageEl) { if (d.age) ageEl.removeAttribute("hidden"); else setAttr(ageEl, "hidden", ""); }
    setAttr(el, "title", d.title);
    current = d;
  }

  function stopTick() { if (tickT) { T.clearInterval(tickT); tickT = 0; tickMs = 0; } }
  function wantTick(d) {
    if (!AGED[d.key] || !d.ageFrom || hiddenNow()) { stopTick(); return; }
    var want = d.ageMs < FAST_UNTIL_MS ? TICK_FAST_MS : TICK_SLOW_MS;
    if (tickT && tickMs === want) return;
    stopTick();
    tickMs = want;
    tickT = T.setInterval(recompute, want);
  }
  // an in-flight read earns its cue after UPDATING_MS: one timeout, armed by the load's start event, never a clock
  function wantUpdating(facts, now) {
    if (updT) { T.clearTimeout(updT); updT = 0; }
    var at = facts.read.inflightAt;
    if (!at || now - at >= UPDATING_MS) return;
    updT = T.setTimeout(function () { updT = 0; recompute(); }, UPDATING_MS - (now - at));
  }
  function cancelHold() { if (holdT) { T.clearTimeout(holdT); holdT = 0; } holdKey = null; }

  function desired() {
    var now = clock();
    var facts = freshFacts(inputsFn ? inputsFn(now) : collectInputs(now));
    return { facts: facts, d: decide(facts, now, { short: !!(phone && phone.matches) }), now: now };
  }

  function recompute() {
    if (!el || !stateEl) return;
    if (hiddenNow()) { stopTick(); return; }     // a hidden page repaints when it is seen again
    var r = desired(), d = r.d;
    wantUpdating(r.facts, r.now);
    if (!current) { paint(d, false); wantTick(d); return; }
    if (d.key === current.key) {
      cancelHold();
      // the same state naming other words (a different panel is the stale one) is a change of what is said, so it is written and spoken
      if (d.words !== current.words) { paint(d, d.key !== "live"); wantTick(d); return; }
      paintAge(d);
      wantTick(d);
      return;
    }
    if (worse(d.key, current.key) || d.key === "down") {
      // the state we are in stays on screen until the worse one has lasted its hold; "down" always waits (a stream's flap is the same
      // flap whether the page was fine, failed or refused before it)
      if (holdKey !== d.key) {
        cancelHold();
        holdKey = d.key;
        holdT = T.setTimeout(function () {
          holdT = 0; holdKey = null;
          if (hiddenNow()) return;      // a hidden page is not repainted: it is read again when seen
          commit();
        }, d.holdMs);
      }
      wantTick(current);
      return;
    }
    cancelHold();
    commit(d);
  }
  // a state changes: a worse one is spoken; a recovery to live is spoken only if a worse state was
  function commit(d0) {
    var d = d0 || desired().d;
    var announce = d.key === "live" ? spoke : d.key !== "connecting";
    if (d.key === "live") spoke = false;
    else if (d.key !== "connecting") spoke = true;
    paint(d, announce);
    wantTick(d);
  }

  function onSeen() {
    if (!el) return;
    if (hiddenNow()) { stopTick(); return; }
    recompute();
  }

  function adopt() {
    el = document.getElementById("freshline");
    stateEl = el && el.querySelector(".fl-state");
    ageEl = el && el.querySelector(".fl-age");
    if (!el || !stateEl) { el = null; return false; }
    return true;
  }
  function start() {
    if (!adopt()) return;
    document.addEventListener("levain:fresh", onSeen);
    document.addEventListener("levain:resume", onSeen);
    try {
      phone = typeof root.matchMedia === "function" ? root.matchMedia(PHONE_QUERY) : null;
      if (phone && typeof phone.addEventListener === "function") phone.addEventListener("change", onSeen);
    } catch (_) { phone = null; }
    // a hold pending when the tab hides is dropped: the connection it judged is gone, and the tab seen again judges the new one
    document.addEventListener("visibilitychange", function () { if (!document.hidden) onSeen(); else { stopTick(); cancelHold(); } });
    recompute();
  }

  var api = {
    freshFacts: freshFacts, decide: decide, toMs: toMs, fmtAge: fmtAge, STRINGS: STRINGS, RANK: RANK, HOLD_MS: HOLD_MS, HOLD_FIRST_MS: HOLD_FIRST_MS,
    // the test seam: a faked clock and faked inputs, and the writes the painter made
    seam: {
      setClock: function (fn) { clock = fn || function () { return Date.now(); }; },
      setInputs: function (fn) { inputsFn = fn || null; },
      setTimers: function (t) { T = t; },
      // back to the first paint: no state written, no timer armed, the static markup's words and the log emptied
      reset: function () {
        stopTick(); cancelHold();
        if (updT) { T.clearTimeout(updT); updT = 0; }
        current = null; spoke = false; writes = [];
        if (el) { el.setAttribute("data-state", "connecting"); stateEl.removeAttribute("aria-live"); stateEl.textContent = STRINGS.connecting; ageEl.textContent = ""; ageEl.setAttribute("hidden", ""); el.setAttribute("title", STRINGS.titleConnecting); }
      },
      recompute: recompute,
      writes: function () { return writes.slice(); },
      armed: function () { return { tick: tickMs, hold: !!holdT, upd: !!updT }; }
    }
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.LevainFreshLine = api;
  if (typeof document !== "undefined" && document.addEventListener) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  }
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
