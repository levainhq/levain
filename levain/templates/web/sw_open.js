// The page half of the push tap (web push, ONESURFACE OS-4): the worker cannot reliably steer a window that already
// exists (WindowClient.navigate rejects on an uncontrolled one, and on iOS WebKit can resolve without moving the page),
// so on a tap it asks the page to go to the card itself. Message {type: 'levain-open', url} with a reply port; this
// page acks {ok: true} on that port BEFORE it navigates (the navigation unloads the page), then assigns the location.
// A url that is not on this origin is ignored without an ack (the worker checks too; this page does not trust it).
// Loaded by every page this origin serves a window can be showing: the cockpit (/), the sign sheet and /pair.
// The worker also writes PENDING TARGETS to Cache Storage, one entry per open under its own key '/__levain_open/<nonce>',
// value {url, nonce, at, for}. Whoever deletes that exact key (cache.delete resolves true for one caller only) owns the
// open, and nobody deletes a key it did not name or read. for 'asked' (written before the worker asks a window): only the
// message path claims it, by the nonce the worker named. for 'new' (written just before openWindow): only this document's
// first script run claims it (one scan per document, never repeated on pageshow or resume), because iOS can land a tapped
// window on the root url; and only a page at '/' or at the entry's own target does, so an unrelated first load never takes
// it. An already loaded page that merely resumes claims nothing, so a window the worker gave up on never moves late.

(() => {
  "use strict";
  if (!("serviceWorker" in navigator)) return;
  const CACHE = "levain-open", PREFIX = "/__levain_open/";
  // a target older than this is a tap nobody followed up (the card it points at is stale, and a later plain visit must not jump).
  // 'new': the window between openWindow and the opened page's first script run. 'asked': the worker's ack wait plus slack.
  // 'shown': the launch fallback for a tap whose notificationclick never ran (see below); SHOWN_TTL_MS is also its expiry here.
  const SHOWN_TTL_MS = 180000;
  const TTL_MS = { asked: 30000, new: 30000, shown: SHOWN_TTL_MS };
  const haveCache = typeof caches !== "undefined";

  // The TRACE (the worker writes the same ring, see sw.js): one cache entry per step under '/__levain_trace/<time>-<seq>',
  // value {t, src: 'page', step, ...detail}, capped. Best effort and never awaited by the tap; urls lose their fragment
  // (it can carry a sign token), and no token or ping body is ever written.
  const TRACE_CACHE = "levain-trace", TRACE_PREFIX = "/__levain_trace/", TRACE_CAP = 60;
  let traceChain = Promise.resolve(), traceSeq = 0;
  // a url reduced to its path, an allowlisted `?view=<value>` (clipped) and markers: other query parameters can carry a
  // credential (written `?...`), a fragment a sign token (written `#...`)
  const traceUrl = (u) => {
    try {
      const x = new URL(String(u), location.href);
      const v = x.searchParams.get("view");
      const others = Array.from(x.searchParams.keys()).some((k) => k !== "view");
      const q = v !== null ? "?view=" + encodeURIComponent(v.slice(0, 40)) : (others || x.search ? "?..." : "");
      return x.pathname + q + (x.hash ? "#..." : "");
    } catch (_) { return null; }
  };
  // PRESENCE of a notification-center item id (`n=`, the shape dashboard_core.js NC_ID accepts) in a url, as `{n: true}` to spread
  // into a step's detail, else `{}`: a tap that carried a deep-linked item is told apart from one that did not, and the id
  // itself (a numeric ping id) is never written.
  const NC_ID = /^[pn][1-9][0-9]{0,17}$/;
  const nFlag = (u) => {
    try { const n = new URL(String(u), location.href).searchParams.get("n"); return n !== null && NC_ID.test(n) ? { n: true } : {}; } catch (_) { return {}; }
  };
  // total: nothing in here (the clock, the key, the JSON, the promise chain) can throw into the tap
  const trace = (step, detail) => {
    try {
      const t = Date.now(), seq = "p" + String(traceSeq++).padStart(3, "0");
      traceChain = traceChain.then(async () => {
        if (!haveCache) return;
        const c = await caches.open(TRACE_CACHE);
        await c.put(TRACE_PREFIX + String(t).padStart(13, "0") + "-" + seq, new Response(JSON.stringify({ t, src: "page", step, ...detail })));
        const keys = (await c.keys()).map((r) => new URL(r.url).pathname).filter((k) => k.indexOf(TRACE_PREFIX) === 0).sort();
        for (const k of keys.slice(0, Math.max(0, keys.length - TRACE_CAP))) await c.delete(k);
      }).catch(() => { /* best effort: a failed trace write never reaches the tap */ });
    } catch (_) { /* best effort */ }
  };
  // the assign comes first: the trace of it may often be lost to the navigation, which is fine
  const assignTo = (href) => { location.assign(href); trace("assign", { target: traceUrl(href), ...nFlag(href) }); };
  try {
    trace("page-load", {
      href: traceUrl(location.href), ...nFlag(location.href),
      standalone: !!(typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches),
      navStandalone: navigator.standalone === undefined ? null : !!navigator.standalone,
      referrer: document.referrer ? traceUrl(document.referrer) : "",
      visibility: document.visibilityState,
      controller: !!navigator.serviceWorker.controller,
    });
  } catch (_) { /* best effort */ }
  // same origin, path and query; only a fragment may differ
  const sameTarget = (a, b) => a.origin === b.origin && a.pathname === b.pathname && a.search === b.search;

  navigator.serviceWorker.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || d.type !== "levain-open" || typeof d.url !== "string") return;
    let u;
    try { u = new URL(d.url, location.href); } catch (_) { return; }
    if (u.origin !== location.origin) return;
    const port = ev.ports && ev.ports[0];
    const go = () => {
      if (port) { try { port.postMessage({ ok: true }); } catch (_) { /* the worker falls back to its own path */ } }
      assignTo(u.href);
    };
    // no cache, or the worker could not write a target: message-only, as before
    const msg = { type: d.type, url: traceUrl(u.href), nonce: typeof d.nonce === "string", ...nFlag(u.href) };
    if (!haveCache || typeof d.nonce !== "string") { trace("message", { ...msg, claim: "message-only" }); go(); return; }
    // only the key this message names, and only a nonce shaped like the worker's: a lost claim means another path (or the
    // worker) already owns this open, so no ack and no navigation
    if (!/^[a-z0-9]+$/i.test(d.nonce)) { trace("message", { ...msg, claim: "bad-nonce" }); return; }
    caches.open(CACHE).then((c) => c.delete(PREFIX + d.nonce)).then((won) => { trace("message", { ...msg, claim: won ? "won" : "lost" }); if (won) go(); })
      .catch((err) => { trace("message", { ...msg, claim: "error", error: err && err.name || "error" }); /* cache unreadable: the worker's path runs */ });
  });

  // the first-load check: a page that opens right after the tap (iOS can open the root url) forwards itself
  let checking = false;
  async function checkPending() {
    if (!haveCache || checking) return;
    checking = true;
    try {
      const cache = await caches.open(CACHE);
      const found = [], seen = [], shownEntries = [];
      for (const req of await cache.keys()) {
        if (new URL(req.url).pathname.indexOf(PREFIX) !== 0) continue;
        let e = null;
        try { const r = await cache.match(req); e = r && await r.json(); } catch (_) { e = null; }
        // unreadable or expired: deleted (the exact key read); the worker deletes its own on the paths it owns
        if (!e || typeof e.url !== "string" || typeof e.at !== "number" || Date.now() - e.at > (TTL_MS[e.for] || TTL_MS.asked)) {
          seen.push({ for: e && e.for || null, ageMs: e && typeof e.at === "number" ? Date.now() - e.at : null, url: e && typeof e.url === "string" ? traceUrl(e.url) : null, dropped: "expired" });
          await cache.delete(req); continue;
        }
        seen.push({ for: e.for || null, ageMs: Date.now() - e.at, url: traceUrl(e.url) });
        // the first version's fixed singleton key (no ping suffix) is orphaned now: swept, never judged
        if (new URL(req.url).pathname === PREFIX + "shown") { await cache.delete(req); continue; }
        if (e.for === "shown") { shownEntries.push({ req, e }); continue; }   // never claimable by the 'new' path (below)
        if (e.for !== "new") continue;                       // 'asked' belongs to the message path
        let u;
        try { u = new URL(e.url, location.href); } catch (_) { continue; }
        if (u.origin !== location.origin) continue;
        found.push({ req, e, u });
      }
      // only a page at '/' (the iOS root landing) or at the entry's own target may take a 'new' entry
      const here = new URL(location.href);
      for (let i = found.length - 1; i >= 0; i--) if (here.pathname !== "/" && !sameTarget(found[i].u, here)) found.splice(i, 1);
      // the entry for the page already showing, else the newest; a lost delete means another page took it: try the next
      found.sort((x, y) => (sameTarget(y.u, here) - sameTarget(x.u, here)) || (y.e.at - x.e.at));
      for (const f of found) {
        if (!(await cache.delete(f.req))) continue;
        trace("checkPending", { seen, claimed: traceUrl(f.u.href), assigned: !sameTarget(f.u, here) });
        if (!sameTarget(f.u, here)) assignTo(f.u.href);   // already here: consumed, the page stays
        return;
      }
      trace("checkPending", { seen, claimed: null, assigned: false });
      await takeShown(cache, shownEntries, here);
    } catch (_) { /* no cache: nothing pending */ } finally { checking = false; }
  }
  // The LAUNCH FALLBACK. On the iPhone Home Screen app a tapped push can open the app at the bare '/' without ever running the
  // worker's notificationclick (11-06: pings 607-609 shown, no click, no `opens` row), so the target is lost. The worker writes
  // ONE immutable 'shown' entry per ping (sw.js startShownPending). This document, at its first run, takes one ONLY when
  // everything says "launched by that tap": the page is exactly '/' (no query, no fragment) and was not reloaded, a standalone
  // launch, the entry at most SHOWN_TTL_MS old and same-origin, and the notification it names is no longer in the tray (iOS
  // dismisses a tapped one; a manual launch with the notification still showing must not redirect). Every live entry is
  // judged; when SEVERAL are gone from the tray (several pings were tapped away or dismissed) the NEWEST wins: it is the one on
  // top of what he would have tapped. Anything unknown (no registration, no getNotifications, a throw) fails toward the plain
  // launch. Deletes name exactly the requests this page read, so a push arriving meanwhile (its own key) is never touched.
  // KNOWN, ACCEPTED EDGE: tray absence is not proof of a tap. A ping he swiped away, then an icon launch within SHOWN_TTL_MS,
  // redirects to that ping's card. It lands inside his own cockpit and costs one tap to leave, and the evidence it replaces is
  // that today the real tap lands nowhere; so the tray check stays and is not tightened further.
  async function takeShown(cache, entries, here) {
    if (!entries.length) return;
    const bare = here.pathname === "/" && !here.search && !here.hash;
    let standalone = false;
    try { standalone = !!((typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches) || navigator.standalone); } catch (_) { standalone = false; }
    if (!bare || !standalone) { trace("shown-skip", { why: !bare ? "not-bare" : "not-standalone" }); return; }
    // a reload at '/' is never a launch (when the Navigation Timing API says so)
    let navType = null;
    try { const n = typeof performance !== "undefined" && performance.getEntriesByType ? performance.getEntriesByType("navigation")[0] : null; navType = n && n.type || null; } catch (_) { navType = null; }
    if (navType === "reload") { trace("shown-skip", { why: "reload" }); return; }
    const live = [];
    for (const f of entries) {
      let u = null;
      try { u = new URL(f.e.url, location.href); } catch (_) { u = null; }
      if (!u || u.origin !== location.origin) { trace("shown-skip", { why: "cross-origin" }); continue; }
      if (Date.now() - f.e.at > SHOWN_TTL_MS) { trace("shown-skip", { why: "expired" }); continue; }
      const ping = Number(f.e.ping);
      if (!Number.isInteger(ping)) { trace("shown-skip", { why: "no-ping" }); continue; }
      live.push({ ...f, u, ping, tag: typeof f.e.tag === "string" ? f.e.tag : "" });
    }
    if (!live.length) return;
    const gone = [];
    try {
      const sw = navigator.serviceWorker;
      let timer;
      const reg = await Promise.race([sw.getRegistration ? sw.getRegistration() : sw.ready, new Promise((r) => { timer = setTimeout(() => r(null), 3000); })]);
      clearTimeout(timer);
      if (!reg || typeof reg.getNotifications !== "function") { trace("shown-skip", { why: "no-api" }); return; }
      // one query per TAG. A re-ping reuses the tag (renotify): the newer notification REPLACES the older in the tray, so an
      // entry is in the tray (or superseded by what is) when a notification under its tag carries its ping or a NEWER one
      const byTag = new Map();
      for (const f of live) {
        if (!byTag.has(f.tag)) byTag.set(f.tag, Array.from(await reg.getNotifications({ tag: f.tag })));
        const list = byTag.get(f.tag);
        if (list.some((n) => n && n.data && Number(n.data.ping) >= f.ping)) trace("shown-skip", { why: "in-tray" });
        else gone.push(f);
      }
    } catch (_) { trace("shown-skip", { why: "error" }); return; }
    if (!gone.length) return;
    gone.sort((x, y) => y.e.at - x.e.at);
    const f = gone[0];
    if (!(await cache.delete(f.req))) return;   // a lost delete: another page took it
    // the launch handled the others that were gone too: a later bare launch must not pop one of them
    for (const o of gone.slice(1)) { try { await cache.delete(o.req); } catch (_) { /* swept by its TTL */ } }
    trace("shown-launch", { url: traceUrl(f.u.href), ping: f.ping, ageMs: Date.now() - f.e.at, candidates: gone.length });
    assignTo(f.u.href);
  }
  // once, at this document's first script run: a pageshow call would let a page opened for one tap consume a later tap's entry
  checkPending();

  // The trace REPORT: the ring posted to /webpush/trace with the key this paired page holds in localStorage (the same key
  // pair_card.js posts opens with), so a tap test needs only the tap and the operator reads the report at the laptop
  // (levain.webpush.tap_report). Every page that loads this script reports, so a tap landing on any of them is read. Step
  // names, counts and path-only urls: no token, no ping body. Best effort and silent. One post per ring CONTENT: what is
  // remembered is the whole list of cache keys joined, compared for equality only, so no clock order (a device clock that
  // moved back) and no count can hide a new step. An answer that is not a settle (a refused key, 429, 5xx, no network) tries
  // again, at most RETRIES more times in a row, 20 s and then 40 s after (a settle resets the count).
  const SENT_KEY = "levain_trace_sent", BODY_MAX = 7600, RETRIES = 2;   // bytes: the push family's body cap is 8192
  let reporting = false, retried = 0;
  async function reportTrace(isRetry) {
    let key = "";
    try { key = window.localStorage.getItem("levain_write_token") || ""; } catch (_) { /* no storage: no key */ }
    if (reporting || !key || !haveCache) return;
    reporting = true;
    let settled = true;
    try {
      const c = await caches.open(TRACE_CACHE);
      const got = [];
      for (const req of await c.keys()) {
        const k = new URL(req.url).pathname;
        if (k.indexOf(TRACE_PREFIX) !== 0) continue;
        try { const e = await (await c.match(req)).json(); if (e && typeof e === "object" && Number.isFinite(e.t)) got.push({ k, e }); } catch (_) { /* skipped */ }
      }
      if (!got.length) return;
      got.sort((a, b) => (a.k < b.k ? -1 : 1));
      const sig = got.map((x) => x.k).join("|");
      let sent = "";
      try { sent = window.localStorage.getItem(SENT_KEY) || ""; } catch (_) { /* none */ }
      if (sig === sent) return;
      if (isRetry !== true) retried = 0;   // a call that is not a scheduled retry starts its own allowance
      let keep = got.map((x) => x.e);
      const bodyOf = () => JSON.stringify({ page: location.pathname.slice(0, 100), entries: keep });
      while (keep.length > 1 && new TextEncoder().encode(bodyOf()).length > BODY_MAX) keep = keep.slice(1);   // drop the oldest
      settled = false;
      const ctl = typeof AbortController === "function" ? new AbortController() : null;
      const stop = ctl ? setTimeout(() => ctl.abort(), 10000) : 0;   // a stalled link must not hold `reporting` forever
      const res = await fetch("/webpush/trace", { method: "POST", cache: "no-store", signal: ctl ? ctl.signal : undefined,
        headers: { "Content-Type": "application/json", "X-Levain-Write-Token": key }, body: bodyOf() }).finally(() => clearTimeout(stop));
      // settled by a 2xx, or a 400 or 413 (this body will never be accepted)
      settled = (res.status >= 200 && res.status < 300) || res.status === 400 || res.status === 413;
      if (settled) { retried = 0; try { window.localStorage.setItem(SENT_KEY, sig); } catch (_) { /* resent next time */ } }
    } catch (_) { /* best effort: a report never reaches the page */ } finally {
      reporting = false;
      if (!settled && retried < RETRIES) { retried += 1; setTimeout(() => reportTrace(true), 20000 * retried); }
    }
  }
  window.levainTraceReport = reportTrace;
  // a moment after this page's own first steps are written, and again when the app comes forward
  const reportSoon = () => { setTimeout(reportTrace, 2500); };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") reportSoon(); });
  reportSoon();

  // an uncontrolled page (opened before the worker took over) queues worker messages until this is called
  try { navigator.serviceWorker.startMessages(); } catch (_) { /* older engines deliver without it */ }
})();
