// levain dashboard_boot.js — the local web-app's transport shim.
//
// The thin lifecycle layer that distinguishes the sovereign web surface from the
// parked in-host MCP-App: where that port receives its snapshot via the MCP-Apps
// `app.ontoolresult` callback, this fetches it from the localhost JSON endpoint
// `levain serve` exposes. Same SubstrateView, same `LevainDashboard.render` core
// — only the delivery differs. Kept in its own served file (not inline) so the
// page's CSP can be `script-src 'self'` with no inline-script exception.

(function () {
  "use strict";
  const storeEl = document.getElementById("store");
  const stampEl = document.getElementById("stamp");

  // Report transient status WITHOUT clobbering the store-path subtitle (#store):
  // a momentary fetch blip shouldn't erase the identity of what we're viewing.
  // `stampFailure`: the stamp holds a read-failure text, which a good read of any kind ends (see the levain:fresh listener below)
  let stampFailure = false;
  function status(msg) {
    if (stampEl) stampEl.textContent = msg;
    stampFailure = false;
  }

  // What this shim knows about how current the page is, for the masthead's freshness line (fresh_line.js). The line is told when
  // a read starts, settles or fails (`levain:fresh`) and asks for these facts then; nothing here is a timer.
  // `failure` is the last read that failed ({why, at, refused}); whether it STILL stands is never stored, only derived (failureStands).
  let lastWholeAt = 0, failure = null;
  function fresh() {
    try { if (typeof document.dispatchEvent === "function" && typeof CustomEvent === "function") document.dispatchEvent(new CustomEvent("levain:fresh")); } catch (_) { /* a page without events has no line to tell */ }
  }
  window.LevainBootFacts = function () {
    const f = failureStands() ? failure : null;
    return { wholeDrawn: wholeDrawn, lastWholeAt: lastWholeAt, inflight: inflight, inflightAt: inflightAt, refused: !!(f && f.refused), failed: f ? f.why : "" };
  };
  // the stamp's failure text ends where the line's failure does: the same derivation
  document.addEventListener("levain:fresh", function () { if (stampFailure && !failureStands()) status(""); });

  let inflight = false;
  // A foreground (pwa_resume.js) after a long freeze: iOS kills the sockets of a frozen page, so a read that was in flight may
  // never settle, and `inflight` would then drop every later read. A read older than STUCK_MS is superseded: its generation is
  // dead, its late result is discarded, and the next read starts.
  let loadGen = 0, inflightAt = 0, inflightPassive = true;
  const STUCK_MS = 8000;
  // A FAILED read (a radio still waking on foreground, a proxy hiccup) must not leave the page with nothing to try again until the next
  // foreground: Phill 11-06 saw both phones sit on the cold "connecting…" line with an empty board, and with no re-read key a failure after
  // the first draw would sit the same way. A failure says so (the cold line before a board is drawn, the freshness line after) and re-reads
  // on a short backoff (and when the network returns); a refusal (token) is not retried.
  // `wholeDrawn` is a WHOLE board (a part does not count): before it, a read that fails after a part was drawn leaves the reader a part,
  // so it is retried like a first read that drew nothing.
  // What bounds the chain: one timer at a time (retryT), the backoff below (RETRY_MS, its last step repeated for as long as the server keeps
  // failing), and it ends at the next good read of ANY kind (a whole draw, a panel applied in place, a poll) or a refusal; a persistently
  // failing server is read once per last step, never in a tight loop.
  let wholeDrawn = false, retryN = 0, retryT = 0, retryMs = 0;
  let holdT = 0, holdSpent = false;     // the watchdog on an inert part (boardMark): its timer, and whether it has already released the page
  // a refusal travels as a typed property, never as message text (a connection-refused network error must still retry)
  function refusal(msg) { const e = new Error(msg); e.refused = true; return e; }
  const RETRY_MS = [2000, 4000, 8000, 15000, 30000];
  // A draw ends the cold line's fault text and a queued retry; only a WHOLE draw resets the backoff. A part (the shells, or what a read has
  // landed so far) is drawn on every read of a board not yet whole, so if it reset the backoff a read that kept failing after its part (the
  // whole draw throwing, a later step rejecting) would be retried every 2 s for good.
  function drawnOnce(whole) { if (wholeDrawn === false && storeEl && /^(can't reach the cockpit|the cockpit refused)/.test(storeEl.textContent || "")) storeEl.textContent = ""; if (whole === true) { wholeDrawn = true; retryN = 0; } clearTimeout(retryT); retryT = 0; }
  // WHOLE OR PART. The first board draws in parts (S4: the Needs strip and the core panels as they land), and the whole draw then REPLACES
  // every node, so anything a reader or a test did on a part would be lost under them. Two things keep that from mattering:
  // - `html[data-board-whole]` is the positive "nothing more is coming that would replace this board" signal. It is set by the code that has
  //   just drawn the whole board, or that knows no whole draw is coming soon (a refusal, a failure with no part drawn, or the watchdog below
  //   at its bound: the page is then left as it is, live, and the retry below still runs); a failure after a part with a retry queued
  //   leaves it unset and the part inert until that retry draws or the watchdog fires; a partial draw clears it, and it is never set on
  //   a partial draw. Absent is the default, so the
  //   page is not ready from the first byte. A deep link's jump (dashboard_core.js) and the test driver wait for it.
  // - ONE bound, a clock: the first inert part arms a watchdog (BOARD_HOLD_MS, below) that releases the page as it is when it fires, whatever
  //   the reads are doing (a hung read, a dead server, retries re-queued behind a read in flight). Any boardMark(true) clears it; a further
  //   partial draw does not re-arm it, but a panel read that settles does (boardProgress): the bound is a stretch with no panel answering, so a
  //   slow board that is still answering is not released half-loaded, and a dead server or a hung read still is. After it has fired the same
  //   cold read draws no more partial boards. When it releases, every panel still loading is drawn as unavailable (releaseShells), never
  //   left pulsing; a later whole draw replaces the board like any redraw. No count of failures releases the part.
  // - while only a part is drawn the regions the draw replaces are `inert`, so nothing in them can be focused, clicked or opened; the whole
  //   draw replaces their children and drops `inert` in the same synchronous turn (dashboard.css dims them). The masthead and the nav (the
  //   tabs, the theme switch, a deep link's jump targets) are not in the list and stay live. The list is the page's own: #board holds
  //   the zones, but the hero, the Needs strip, the notes and the widgets (the tiles) are its siblings, and those are where a
  //   reader first acts (dashboard.html; renderKe in dashboard_core.js fills them). The drop (#ck-drop) is NOT in the list: renderDropHero
  //   builds its card once and refreshes it in place, so the whole draw never replaces it, and an inert drop would make a tap on the Drop
  //   tab scroll without focusing the box (phoneJump focuses inside a frame callback, which is a no-op on an inert node).
  const BOARD_REGIONS = ["ck-hero", "ck-needs", "ck-notes", "ck-widgets", "state", "board"];
  function boardMark(whole) {
    try {
      for (const id of BOARD_REGIONS) {
        const n = document.getElementById(id);
        if (n && typeof n.setAttribute === "function") { if (whole) n.removeAttribute("inert"); else n.setAttribute("inert", ""); }
      }
      const h = document.documentElement;
      if (h && typeof h.setAttribute === "function") { if (whole) h.setAttribute("data-board-whole", ""); else h.removeAttribute("data-board-whole"); }
    } catch (_) { /* a page without these nodes (the node harnesses) has no board to mark */ }
    if (whole) { clearTimeout(holdT); holdT = 0; }
    else if (!holdT && !holdSpent) armHold();
  }
  function armHold() {
    // the panels whose bodies are in hand (the draw is only waiting, on an edit) keep their shells: only a read that has not landed is released
    holdT = setTimeout(function () { holdT = 0; holdSpent = true; boardMark(true); releaseShells("no answer in " + BOARD_HOLD_MS / 1000 + " s", Object.keys(bootLanded)); }, BOARD_HOLD_MS);
  }
  // a panel read of the CURRENT read settled: the board is still answering, so the watchdog's stretch starts again (only a watchdog already armed)
  function boardProgress() { if (holdT) { clearTimeout(holdT); armHold(); } }
  // every panel shell still loading, but those named in `keep`, is drawn as unavailable with the reason (the render core's own), so nothing
  // pulses for a read that is not coming
  function releaseShells(why, keep) {
    try { const d = window.LevainDashboard; if (d && typeof d.releaseLoading === "function") d.releaseLoading(why, keep); } catch (_) { /* nothing drawn to release */ }
  }
  // a read of any kind (a whole draw, a panel applied in place by the stream, a manifest poll) landed after `t`: the page is current again
  function readSince(t) {
    if (lastWholeAt > t) return true;
    try {
      const S = typeof window.LevainStreamFacts === "function" ? window.LevainStreamFacts() : null;
      if (S && S.pollOkAt > t) return true;
      const H = window.LevainCockpitHeld || {};
      return Object.keys(H).some(function (id) { return !!(H[id] && H[id].at > t); });
    } catch (_) { return false; }
  }
  // THE derivation of "the read failed": a failure stands only while no read of any kind is newer than it. The line's facts, the stamp, the retry
  // chain and the `online` handler all ask here, so none of them can call the page failed or current on its own.
  function failureStands() { return !!failure && !readSince(failure.at); }
  function refusalStands() { return failureStands() && failure.refused; }
  function retryUntilDrawn(why, refused) {
    // a refusal (the token the page holds is missing or wrong) is not a network blip: a retry would only ask again, and each ask can
    // put up the token prompt. Say it and stop; the next foreground reads again. Before a board is drawn the cold line says it; after,
    // the freshness line does (the failure's `refused`), and the cold line is left alone (nothing would clear it).
    if (refused) {
      if (!wholeDrawn) {
        if (storeEl) { storeEl.hidden = false; storeEl.textContent = "the cockpit refused this device (" + why + "); pair it again or reload"; }
        releaseShells("the cockpit refused this device");     // shells drawn before the refusal would otherwise load for good
      }
      return;
    }
    if (!wholeDrawn && storeEl) { storeEl.hidden = false; storeEl.textContent = "can't reach the cockpit yet (" + why + "), trying again…"; }
    if (retryT) return;
    // only a FAILED read advances the backoff
    retryMs = RETRY_MS[Math.min(retryN++, RETRY_MS.length - 1)];
    armRetry(why);
  }
  function armRetry(why) {
    retryT = setTimeout(function () {
      retryT = 0;
      if (refusalStands()) return;
      // a drawn board is retried only while its failure stands: a read of any kind since ends the chain
      if (wholeDrawn && !failureStands()) { retryN = 0; return; }
      // a read already in flight (a foreground started one) would drop this retry and end the chain: wait and ask again instead, on the
      // SAME delay (no read failed, so the backoff does not advance)
      if (inflight) { armRetry(why); return; }
      load({ passive: true, auto: true });
    }, retryMs);
  }
  // A failed read after a part, with the watchdog still armed (boardMark), keeps the part inert and the signal unset: the retry's whole draw
  // is coming and replaces the part. The wait is bounded by that watchdog's clock alone, so a dead server or a hung read cannot leave the page
  // inert for good. A refusal queues no retry, so it releases at once (boardMark(true) also clears the watchdog), as does a failure with no
  // part drawn (no watchdog armed).
  function holdingPart(refused) { return !refused && holdT !== 0; }
  // (the node harnesses that load this file hand it a window with no event API)
  if (typeof window.addEventListener === "function") window.addEventListener("online", function () { if ((!wholeDrawn || failureStands()) && !refusalStands() && !inflight) { clearTimeout(retryT); retryT = 0; load({ passive: true, auto: true }); } });
  // A non-passive (save / boot) reload is AUTHORITATIVE — it must eventually render. If one
  // lands while a load is in flight, the shared `inflight` guard would drop it; latch it
  // here and re-fire when the current load settles, so a save's reload can't be swallowed by
  // an overlapping passive read (which would leave the editor wedged open). [codex L3 round-2]
  let pendingReload = false;
  // A board reload a write DEFERRED because an async job was polling (a reload rebuilds the board +
  // would detach the live consult result box; L3 codex MED). Flushed by onJobsIdle() when the last
  // job finishes — so the sibling write's saved state still lands, just after the job.
  let deferredReload = false;

  // The write token (intention-129, then K2a). A writable server REQUIRES the X-Levain-Write-Token
  // header on every write, on loopback too (K2a); off-box it also gates the reads (intention-220).
  // The server signals it via `view.write_token_required` (set in load()); we hold the token in
  // localStorage and attach it. The server prints the token at start, with a link carrying it
  // in the fragment, which the page takes on load. (This localStorage entry is the
  // desktop-browser STOPGAP; a native app shell later holds the token in the keychain
  // and injects the same header — the server gate underneath is identical.)
  //
  // ⚠ SECURITY (TCB, L2 review): storing the token here puts it in the page's localStorage, so
  // the render core (dashboard_core.js) is now part of this token's trust boundary — its
  // confidentiality depends on the core NEVER introducing an HTML/script-injection sink (it
  // renders via textContent/replaceChildren only, and the substrate is the operator's OWN store,
  // so the surface is low). The strict CSP (script-src 'self', no unsafe-inline) is the backstop.
  // Conscious tradeoff: localStorage (persists → enter once) over sessionStorage (per-session)
  // for daily-driver convenience on the operator's personal device; the native keychain is the
  // real fix at Lane 1.
  const WRITE_TOKEN_KEY = "levain_write_token";
  // `storage` never fires in the document that changed the token, so every same-page set or remove announces it here
  // for the zones that follow the token (the Room and Desk views); other tabs still arrive through `storage`.
  const TOKEN_CHANGE_EVENT = "levain:write-token-change";
  function announceTokenChange() { forgetLanded(); try { window.dispatchEvent(new Event(TOKEN_CHANGE_EVENT)); } catch (_) { /* no events */ } }
  // The panels that landed for a cold board no whole draw has yet replaced: a later read of it (a foreground that superseded a stuck read, a
  // retry, a read deferred for an edit) paints them again at once, and reads every panel again as it always did. They are bodies read as
  // ONE credential: `bootLandedAs` names it (the manifest's credential class and the stored token), and a token set or dropped on this page,
  // or a read that finds another credential (another tab changed the token), forgets them, so a page never draws a body read as a
  // credential it no longer holds.
  let bootLanded = Object.create(null), bootLandedAs = null;
  function forgetLanded() { bootLanded = Object.create(null); bootLandedAs = null; }
  // Set by pair.js when this browser was paired by a link: its key is a DEVICE key, so when it is refused the way back is
  // pairing again from a paired device, never typing the owner's token (P2 L3 r1 F6). MUST equal pair.js PAIRED_MARK_KEY.
  const PAIRED_MARK_KEY = "levain_paired_device";
  // The page's own cap on concurrent panel reads (see the batch in the board read below).
  const PANEL_READ_CONCURRENCY = 4;
  // BEGIN conditional panel read
  // A panel read is conditional: the page keeps each panel's last body with its ETag (module-level, so it outlives a
  // load) and sends If-None-Match; a 304 answers with the held body, so an unchanged panel costs headers, not bytes.
  // The ETag is per credential class on the server, so a changed credential reads a 200, never another's body.
  // The server's panel ETag leaves out `as_of` (a refresher over unchanged rows still answers 304), so a held body
  // takes its `as_of` from the manifest head read in the same load, which is what the page shows as "read ...".
  const panelBodies = new Map();   // panel url -> { etag, text, type } of its last 200
  const panelUrl = (id) => "/cockpit/panel/" + encodeURIComponent(id) + ".json?profile=full";
  function pruneHeldPanels(ids) {
    const live = new Set(ids.map(panelUrl));
    for (const u of Array.from(panelBodies.keys())) if (!live.has(u)) panelBodies.delete(u);
  }
  async function fetchPanelConditional(url, head, signal) {
    const hit = panelBodies.get(url);
    const headers = readHeaders();
    if (hit) headers["If-None-Match"] = hit.etag;
    const res = await fetch(url, signal ? { cache: "no-store", headers: headers, signal: signal } : { cache: "no-store", headers: headers });
    if (res.status === 304 && hit) {
      let text = hit.text;
      if (head && typeof head.as_of === "string") {
        try { const p = JSON.parse(text); p.as_of = head.as_of; text = JSON.stringify(p); } catch (_) { /* keep the held body */ }
      }
      return new Response(text, { status: 200, headers: { "Content-Type": hit.type, "ETag": hit.etag } });
    }
    const etag = res.ok ? res.headers.get("ETag") : null;
    if (!etag) { panelBodies.delete(url); return res; }
    const text = await res.text();
    const type = res.headers.get("Content-Type") || "application/json";
    panelBodies.set(url, { etag: etag, text: text, type: type });
    return new Response(text, { status: 200, headers: { "Content-Type": type, "ETag": etag } });
  }
  // END conditional panel read
  // BEGIN progressive draw
  // The first board is drawn as its panels land, never held for the whole set: the manifest, then at once every panel's shell
  // (each one loading, a third state beside drawn and unavailable), then each panel as its read answers, the Needs strip's panel
  // first and the page's top (the hero, and the state line the now band shows) next; the brief region's other panels, which the page no longer draws, queue with the rest. These are the pure parts: which ids are read first, the board in hand
  // (a landed panel's body, a loading marker for each one still out), and the pacing of the draws.
  const PARTIAL_GAP_MS = 250;        // two partial draws are at least this far apart, and at least PARTIAL_IDLE x the last draw's own time (a slow phone renders slowly)
  const PARTIAL_IDLE = 4;
  const PANEL_TIMEOUT_MS = 30000;    // a panel that has not answered by then is drawn as its own failure, the rest of the board stays usable
  // The longest a part may stay inert, from the first inert part: one full panel attempt (PANEL_TIMEOUT_MS, the longest any read of the
  // rest of the board can take) plus a margin of about the retry backoff's first four waits (2 + 4 + 8 + 15 s), so a board that is only
  // slow lands its whole draw first and the release is for a read that is hung or a server that is dead.
  const BOARD_HOLD_MARGIN_MS = 30000;
  const BOARD_HOLD_MS = PANEL_TIMEOUT_MS + BOARD_HOLD_MARGIN_MS;
  // The panel the NEEDS YOU strip and its band are drawn from: read before any other.
  const NEEDS_PANELS = ["decisions"];
  function drawOrder(manifest, ids) {
    const r = (manifest && manifest.regions) || {};
    const first = [];
    for (const id of [].concat(NEEDS_PANELS, r.hero || [], (r.brief || []).filter((b) => b === "state"))) if (ids.indexOf(id) >= 0 && first.indexOf(id) < 0) first.push(id);
    return first.concat(ids.filter((id) => first.indexOf(id) < 0));
  }
  // The panels the board is drawn from: each landed panel's own record, and a loading marker for every one still out.
  // (a cold board that is not yet whole keeps what an earlier read of it landed: `bootLanded`, below, is the seed it is drawn from again)
  function boardPanels(ids, landed) {
    const out = Object.create(null);
    for (const id of ids) out[id] = Object.prototype.hasOwnProperty.call(landed, id) ? landed[id] : { loading: true };
    return out;
  }
  // draw(settledCount) is called by first() at once (the shells), then after each panel that lands at most once per PARTIAL_GAP_MS,
  // and never for the last panel (the full draw is the caller's). `stop` cancels a pending one.
  function partialPacer(total, draw, clock) {
    let n = 0, last = -Infinity, gap = PARTIAL_GAP_MS, timer = null, done = false;
    const fire = () => {
      timer = null; if (done) return;
      const t0 = clock.now();
      // a partial draw is best-effort: a throw here must never reach the panel read that landed it
      try { draw(n); } catch (e) { if (typeof console !== "undefined") console.warn("levain: a partial board draw failed:", e); }
      finally {
        last = clock.now();
        gap = Math.max(PARTIAL_GAP_MS, PARTIAL_IDLE * (last - t0));
      }
    };
    return {
      first: fire,
      settled: function () {
        n++;
        if (done || n >= total) return;
        if (timer !== null) return;
        const wait = last + gap - clock.now();
        if (wait <= 0) fire(); else timer = clock.setTimeout(fire, wait);
      },
      stop: function () { done = true; if (timer !== null) { clock.clearTimeout(timer); timer = null; } },
    };
  }
  // One ACTUAL attempt (the fetch AND the read of its whole body) gets its own controller and its own PANEL_TIMEOUT_MS: the timer
  // stays armed until the body is in, so a server that sends headers and stalls is timed out too; it is never running while the
  // page waits on a token prompt, and a retry after re-auth starts a fresh one. The answer is buffered, so what follows reads no socket.
  async function timedAttempt(fetchPanel, url, clock) {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl ? clock.setTimeout(() => ctl.abort(), PANEL_TIMEOUT_MS) : 0;
    try {
      const res = await fetchPanel(url, ctl ? ctl.signal : undefined);
      const text = await res.text();
      return { status: res.status, ok: res.ok, text: function () { return Promise.resolve(text); }, clone: function () { return this; }, json: function () { return Promise.resolve().then(() => JSON.parse(text)); } };
    }
    catch (e) { if (ctl && ctl.signal.aborted) throw new Error("no answer in " + PANEL_TIMEOUT_MS / 1000 + " s"); throw e; }
    finally { if (timer) clock.clearTimeout(timer); }
  }
  // io: attempt(url) -> Response, isReject(res) -> bool, reauth() -> Promise<bool>, token(), drop()
  async function readPanelBody(url, io) {
    let res = await io.attempt(url);
    if (res.status === 403 && await io.isReject(res.clone())) {
      if (await io.reauth()) {
        const usedToken = io.token();
        res = await io.attempt(url);
        // the replacement token was rejected too: drop it so the next refresh asks again (not a newer token another tab stored)
        if (res.status === 403 && await io.isReject(res.clone()) && io.token() === usedToken) io.drop();
      }
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }
  // A partial board is drawn only if its load is still the current one and nothing is being edited, and its mapping (which keeps
  // state in the view mapper) runs only then; a throw anywhere in it leaves the board as it was.
  function runPartial(gate, build, show) {
    if (!gate()) return false;
    try { show(build()); return true; }
    catch (e) { if (typeof console !== "undefined") console.warn("levain: a partial board draw failed:", e); return false; }
  }
  // the whole board's draw: a load that drew a partial board (held inert) is held to the same edit rule as a passive one; the whole-board
  // signal stays unset until the deferred draw lands
  function mayDrawWhole(passive, partialDrawn, editing) { return !((passive || partialDrawn) && editing); }
  // END progressive draw
  let writeTokenRequired = false;
  // What a missing token is answered with, from the server: "pair" (this request cannot use the owner key: pair the device by a
  // short code) or "owner" (a loopback page: the owner's own token is typed, as before). "" until a 403 body or the manifest says.
  let tokenAuth = "";

  // K2a: `levain serve` opens the page with the write token in the URL FRAGMENT (never sent to the server).
  // Take it, keep it, and strip it from the address bar, leaving any chat_token for the chat panel.
  // A token this browser already holds is replaced only once the server has accepted the new one: a link that arrives by a
  // redirect from another origin carries whatever it likes, and storing that first would force the owner to pair again.
  // With nothing held there is nothing to lose, so a first pairing stores at once. The first board read waits (bounded) for
  // the answer, so it never runs on the old token while the new one is being checked.
  const FRAGMENT_TOKEN_CHECK_MS = 8000;
  let fragmentTokenPending = null;
  // Whether the server takes `tok` as a credential: its manifest names the class of the credential it read (a refused or
  // unknown key answers 200 with class "none", pair.js M1). Anything but a clear yes is a no. The bound is a timer raced
  // against the read, so it holds in a browser without AbortController (the abort, where there is one, only frees the request).
  async function serverTakesToken(tok) {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    let timer = 0;
    const answer = (async () => {
      const res = await fetch("/cockpit/manifest.json", { method: "GET", headers: { "X-Levain-Write-Token": tok }, cache: "no-store",
        referrerPolicy: "no-referrer", signal: ctl ? ctl.signal : undefined });
      if (res.status !== 200) return false;
      const m = await res.json();
      return !!(m && m.credential && m.credential.class === "token");
    })().catch(() => false);
    const bound = new Promise((resolve) => { timer = setTimeout(() => { if (ctl) { try { ctl.abort(); } catch (_) { /* ignore */ } } resolve(false); }, FRAGMENT_TOKEN_CHECK_MS); });
    try { return await Promise.race([answer, bound]); }
    finally { clearTimeout(timer); }
  }
  (function takeFragmentWriteToken() {
    try {
      const params = new URLSearchParams((location.hash || "").replace(/^#/, ""));
      const tok = params.get("write_token");
      if (tok === null) return;
      params.delete("write_token");
      const rest = params.toString();
      history.replaceState(null, "", location.pathname + location.search + (rest ? "#" + rest : ""));
      // a token is printable ASCII without spaces (it travels in a request header)
      if (/^[\x21-\x7e]+$/.test(tok)) {
        const take = () => {
          window.localStorage.setItem(WRITE_TOKEN_KEY, tok);
          window.localStorage.removeItem(PAIRED_MARK_KEY);   // the owner's own link: this browser holds the owner's key now
          announceTokenChange();
        };
        const held = window.localStorage.getItem(WRITE_TOKEN_KEY) || "";
        const marked = !!window.localStorage.getItem(PAIRED_MARK_KEY);
        // nothing held AND no paired mark: a first pairing, nothing to lose. A mark with no token is a paired browser that dropped
        // its key; its link token is checked like any other, so a bad one cannot clear the mark and the recovery path.
        if ((!held && !marked) || held === tok) take();
        else {
          fragmentTokenPending = serverTakesToken(tok).then((ok) => {
            if (!ok) { console.warn("levain: the write token in the link was not accepted by the server; the token this browser holds is kept"); return; }
            // the check took time: another tab may have stored its own token meanwhile, which is newer than this link's and stays
            const now = window.localStorage.getItem(WRITE_TOKEN_KEY) || "";
            if (now && now !== held) { console.warn("levain: this browser's token changed while the link's was checked; the newer one is kept"); return; }
            take();
          }).catch(() => { /* no storage: the prompt still works */ }).finally(() => { fragmentTokenPending = null; });
        }
      }
      else console.warn("levain: the write token in the link is not a header-safe string; not stored");
    } catch (_) { /* no history or storage: the prompt still works */ }
  })();

  // The device-held off-box token from localStorage ("" if none / private mode).
  function storedToken() {
    try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; }
  }
  // Prompt for + persist the write token; returns the entered token ("" if cancelled). The
  // desktop-browser STOPGAP entry point (a native app shell injects the same header from
  // the keychain). One token gates BOTH the reads (intention-220) and the writes (intention-129).
  let tokenDeclined = false;   // this page load: the operator declined the prompt; passive refreshes stay quiet
  function pairedDevice() {
    try { return window.localStorage.getItem(PAIRED_MARK_KEY) === "1"; } catch (_) { return false; }
  }
  // A paired device whose key is refused (revoked): a plain card over the page in place of the prompt.
  function showUnpaired() {
    if (document.getElementById("ck-unpaired")) return;
    const veil = document.createElement("div");
    veil.id = "ck-unpaired";
    veil.className = "pair-veil";
    veil.setAttribute("role", "alertdialog");
    veil.setAttribute("aria-labelledby", "ck-unpaired-t");
    const card = document.createElement("div");
    card.className = "pair-card";
    const brand = document.createElement("div");
    brand.className = "pair-brand";
    const dot = document.createElement("span");
    dot.className = "pair-dot";
    brand.append(dot, "Levain");
    const h = document.createElement("h1");
    h.id = "ck-unpaired-t";
    h.textContent = "This device was unpaired";
    const p = document.createElement("p");
    p.textContent = "Pair it again from a paired device: open the cockpit there, then Devices, Show a code and type it here, or Make a link and open it here.";
    card.append(brand, h, p);
    veil.appendChild(card);
    document.body.appendChild(veil);
  }
  // W1b: the pair card, for a page whose server will not take the owner key (off the box). It asks for the short code a paired
  // device or the desk shows and hands it to pair_card.js, which claims it; the card never holds a token itself.
  // A pair card already up is left as it is (a later 403 in the same load, or a passive re-read, would wipe the typed code);
  // the unpaired card, which has no form, is replaced.
  function showPairCard() {
    const old = document.getElementById("ck-unpaired");
    if (old && old.querySelector(".pair-form")) return;
    if (old) old.remove();
    const veil = document.createElement("div");
    veil.id = "ck-unpaired";
    veil.className = "pair-veil";
    veil.setAttribute("role", "dialog");
    veil.setAttribute("aria-labelledby", "ck-pair-t");
    const card = document.createElement("div");
    card.className = "pair-card";
    const brand = document.createElement("div");
    brand.className = "pair-brand";
    const dot = document.createElement("span");
    dot.className = "pair-dot";
    brand.append(dot, "Levain");
    const h = document.createElement("h1");
    h.id = "ck-pair-t";
    h.textContent = "Pair this device";
    const p = document.createElement("p");
    p.textContent = "Type the code shown on a paired device (Devices, Show a code) or by the desk.";
    const form = document.createElement("form");
    form.className = "pair-form";
    const input = document.createElement("input");
    input.className = "dev-label pair-code";
    input.type = "text";
    input.maxLength = 14;
    input.placeholder = "XXXX-XXXX";
    input.autocomplete = "one-time-code";
    input.autocapitalize = "characters";
    input.spellcheck = false;
    input.setAttribute("autocorrect", "off");
    input.setAttribute("aria-label", "Pairing code");
    const msg = document.createElement("p");
    msg.className = "pair-msg";
    msg.setAttribute("role", "status");
    msg.hidden = true;
    const actions = document.createElement("div");
    actions.className = "pair-actions";
    const go = document.createElement("button");
    go.type = "submit";
    go.className = "dev-btn dev-primary";
    go.textContent = "Pair";
    actions.appendChild(go);
    form.append(input, msg, actions);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const claimer = window.levainPairCard;
      if (!claimer) { msg.textContent = "The pairing script did not load. Reload the page."; msg.hidden = false; return; }
      go.disabled = true; msg.hidden = true;
      try { await claimer.claim(input.value, { card: card, form: form, message: msg }); }
      finally { if (go.isConnected) go.disabled = false; }
    });
    card.append(brand, h, p);
    // an installed app keeps its own sign-in: one extra line then, nothing otherwise (read now, when the card is drawn)
    const pc = window.levainPairCard;
    const installed = pc && typeof pc.installedNote === "function" ? pc.installedNote() : "";
    if (installed) {
      const n = document.createElement("p");
      n.className = "pair-installed";
      n.textContent = installed;
      card.appendChild(n);
    }
    // a phone tab where an install is possible (pwa_install.js): the coach in place of the code form; pairing is the installed app's
    const pwa = window.levainPwa;
    if (pwa && typeof pwa.gated === "function" && pwa.gated()) card.appendChild(pwa.note()); else card.appendChild(form);
    veil.appendChild(card);
    document.body.appendChild(veil);
    try { input.focus(); } catch (_) { /* no focus */ }
  }
  function promptForToken() {
    if (pairedDevice()) {
      // never the owner-token prompt on a paired device: drop the dead key so a new pair link claims without asking
      tokenDeclined = true;
      dropToken();
      if (tokenAuth === "pair") showPairCard(); else showUnpaired();
      return "";
    }
    if (tokenAuth === "pair") {
      // off the box the owner key opens nothing: never window.prompt for it, draw the pair card
      tokenDeclined = true;
      showPairCard();
      return "";
    }
    const tok = (window.prompt("Write token (printed when this server started: on its startup line, or in the daemon's log):") || "").trim();
    if (tok) tokenDeclined = false;
    if (tok) { try { window.localStorage.setItem(WRITE_TOKEN_KEY, tok); announceTokenChange(); } catch (_) { /* ignore */ } }
    return tok;
  }
  // Drop a stored token so the next attempt re-prompts (a mistyped / rotated token).
  function dropToken() {
    try { window.localStorage.removeItem(WRITE_TOKEN_KEY); announceTokenChange(); } catch (_) { /* ignore */ }
  }
  // Headers for a READ (GET): attach the stored off-box token OPPORTUNISTICALLY if we hold one.
  // Harmless on a loopback / read-only surface (the server skips the read gate there); REQUIRED on
  // an off-box writable surface (intention-220 gates the substrate reads). No Content-Type — GET has no body.
  function readHeaders() {
    const h = {};
    const tok = storedToken();
    if (tok) h["X-Levain-Write-Token"] = tok;
    return h;
  }
  // Headers for a WRITE (POST): Content-Type + the token when the surface requires it, PROMPTING if
  // we don't hold one yet (the write path always knows writeTokenRequired by commit time).
  function writeHeaders() {
    const h = { "Content-Type": "application/json" };
    if (!writeTokenRequired) return h;
    let tok = storedToken();
    if (!tok) tok = promptForToken();
    if (tok) h["X-Levain-Write-Token"] = tok;
    return h;
  }
  // True iff a 403 is the off-box token gate (JSON body w/ a token message) vs a Host / cross-site
  // refusal (plain text). Consumes the body — the caller re-fetches on retry, so that's fine.
  // NB (complement L3 FIND-4): this ``/token/i`` matcher is coupled to the kernel's literal token-403
  // message ("missing or invalid write token") and is DUPLICATED in an embedder's own frontend — tighten
  // the regex in one place and you must mirror it in the other repo, or the prompt/drop silently drifts.
  async function isTokenReject(res) {
    try {
      const d = await res.json();
      const rejected = /token/i.test((d && d.message) || "");
      if (rejected && d && (d.auth === "pair" || d.auth === "owner")) tokenAuth = d.auth;   // W1b: what the server would accept here
      return rejected;
    }
    catch (_) { return false; }
  }

  // The write transport for Slice-2a governed edits. POSTs the edit to /edit with a
  // same-origin fetch (browser sends Sec-Fetch-Site: same-origin + we set
  // application/json — exactly what the server's write/auth boundary requires; off-box it
  // also carries the write token via writeHeaders). On success it reloads so the dashboard
  // reflects the saved state; on refusal it returns {ok:false, error, message} so the render
  // core can surface it inline.
  let staleReread = null;   // the one pending re-read after a stale refusal
  async function postWrite(route, body) {
    try {
      const res = await fetch(route, {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await res.json(); } catch (_) { /* tolerate a non-JSON body */ }
      // Defer the reload while an async job is polling — rebuilding the board would detach the
      // live consult result box (L3 codex MED). onJobsIdle() flushes it when the job ends.
      const reread = async () => {
        if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function"
            && window.LevainDashboard.jobsActive()) {
          deferredReload = true;
        } else {
          await load();
        }
      };
      if (res.ok) {
        await reread();
        return { ok: true, data: data };
      }
      // A stale refusal means this page's picture of the row is old (intention-174): re-read it, after the row has shown
      // the refusal for a moment, so the next click acts on the current row instead of re-firing the old one. The
      // re-read is PASSIVE, so an editor the operator opened meanwhile is never rebuilt away; one timer at a time.
      if (res.status === 409 && data.error === "stale" && !staleReread) {
        staleReread = setTimeout(() => { staleReread = null; rereadLatched(); }, 2500);
      }
      // A token rejection (off-box only): drop the stored token so the next attempt re-prompts
      // — covers a mistyped/rotated token without wedging every future write.
      if (res.status === 403 && writeTokenRequired && /token/i.test(data.message || "")) dropToken();
      return {
        ok: false,
        error: data.error || "HTTP " + res.status,
        message: data.message || "HTTP " + res.status,
        status: res.status,   // an answer from the server: a 4xx is a definite refusal (a signed send is filed again)
      };
    } catch (e) {
      return { ok: false, error: "network", message: e && e.message ? e.message : String(e) };
    }
  }

  // Class-A/B substrate edits → POST /edit (the request carries its own `kind`).
  async function commit(request) { return postWrite("/edit", request); }

  // Governed operator ACTION verbs → POST /action (the external-panel-ACTION seam). `verb` is
  // the registered extra_verb name, `params` the compose fields; confirm:true is sent only AFTER
  // the operator confirms a confirm_required verb (the kernel 409s without it — the fat-finger
  // gate). `idempotencyKey` (a string) is included for an IDEMPOTENT verb — the at-most-once
  // retry token the kernel dedupes on, so a tailnet/proxy/browser retry of this same POST (or an
  // operator re-click after a network error) returns the original response WITHOUT re-firing.
  // Mirror of commit: same auth/token/reload contract via postWrite.
  async function commitAction(verb, params, confirm, idempotencyKey) {
    const body = { verb: verb, params: params || {}, confirm: confirm === true };
    if (idempotencyKey) body.idempotency_key = idempotencyKey;
    return postWrite("/action", body);
  }

  // A JOB (I/O-bound) verb PROPOSES via POST /action but does NOT reload the board: the result
  // lands in the compose box via polling, and a board reload would nuke the polling box mid-job.
  // Returns the job HANDLE { ok, job_id, status } (or { ok:false, error, message }). Same
  // auth/token contract as postWrite, minus the reload.
  async function commitJob(verb, params, confirm, idempotencyKey) {
    const body = { verb: verb, params: params || {}, confirm: confirm === true };
    if (idempotencyKey) body.idempotency_key = idempotencyKey;
    try {
      const res = await fetch("/action", { method: "POST", headers: writeHeaders(), body: JSON.stringify(body) });
      let data = {};
      try { data = await res.json(); } catch (_) { /* tolerate a non-JSON body */ }
      if (res.ok) { return { ok: true, job_id: data.job_id, status: data.status }; }
      if (res.status === 403 && writeTokenRequired && /token/i.test(data.message || "")) dropToken();
      return { ok: false, error: data.error || "HTTP " + res.status, message: data.message || "HTTP " + res.status };
    } catch (e) {
      return { ok: false, error: "network", message: e && e.message ? e.message : String(e) };
    }
  }

  // Fetch JSON from a READ route with the off-box token attached, running the SAME token-403
  // bootstrap as load() — prompt once, retry, drop-on-fail — so a rotated/absent token doesn't WEDGE
  // a read (job-poll / search) on repeated 403s until a full substrate reload happens to re-bootstrap
  // (codex L3 LOW). Preserves the server's error message on a hard non-OK (e.g. the fail-closed 500 a
  // corrupt job store returns); the caller surfaces it. The token-class 403 is handled here.
  // `signal` (optional): an AbortSignal, so a caller that supersedes its own read (the topology Replay scrub) can cancel the older one.
  // `opts.quiet` (optional): a read the person did not ask for by name (the List's search as they type) never opens the token prompt
  // and never drops the stored token on a token 403; it throws the refusal (`refused`) and the caller shows its own fault.
  async function authedReadJson(url, signal, opts) {
    const usedToken = storedToken();
    let res = await fetch(url, { cache: "no-store", headers: readHeaders(), signal: signal });
    if (res.status === 403 && await isTokenReject(res)) {
      if (opts && opts.quiet === true) throw refusal("off-box token missing or invalid");
      // a token stored while this read was in flight (another tab, a paste, a pair claim) is newer than the one refused:
      // read again with it, never prompt over it and never drop it (a slow first 403 used to erase the token a reload had just been given)
      const newer = storedToken() !== usedToken && storedToken();
      let usedNow = usedToken;
      if (newer) usedNow = newer;
      if (newer || promptForToken()) { usedNow = storedToken(); res = await fetch(url, { cache: "no-store", headers: readHeaders(), signal: signal }); }
      if (res.status === 403) { if (storedToken() === usedNow) dropToken(); throw refusal("off-box token missing or invalid"); }
    }
    if (!res.ok) {
      let m = "HTTP " + res.status;
      try { const d = await res.json(); m = d.message || d.error || m; } catch (_) { /* ignore */ }
      const re = new Error(m); re.refused = res.status === 401 || res.status === 403; throw re;
    }
    return res.json();
  }

  // The read transport for code outside this file (the filter box's store search, a one-panel refetch):
  // the same function, so the same token handling.
  // `headers` is the SAME read-header function the reads above use, so the change stream (a fetch read that
  // cannot go through res.json()) carries the same credential; `reload` is the board's one passive re-read and
  // resolves true only once a board was drawn (false: deferred by an edit or a load in flight, or it failed).
  // notice: a verb's own words for the status line, after its write has redrawn the page (the intention controls' "said")
  window.LevainCockpitTransport = { readJson: authedReadJson, headers: readHeaders, reload: () => load({ passive: true }), rowActions, rowVerb,
    notice: (text) => status(String(text).slice(0, 200)) };

  // Poll an async job's status → { status, result?, error? }. Rides authedReadJson (intention-220 gates
  // /job.json off-box; token-free on loopback/read-only). Throws on a non-OK (incl. the fail-closed
  // 500 a corrupt job store returns); the in-box poll loop retries a few times, then surfaces it.
  async function pollJob(jobId) {
    return authedReadJson("/job.json?id=" + encodeURIComponent(jobId));
  }

  // Episode keyword search → GET /recall.json (the read-peer of pollJob, routed through the render
  // transport seam so the core stays token-agnostic). Rides authedReadJson (off-box token + the
  // token-403 bootstrap). Returns the parsed {episodes,count,errors} body; the core's search()
  // surfaces a throw.
  async function recall(keyword) {
    return authedReadJson("/recall.json?keyword=" + encodeURIComponent(keyword));
  }

  // The render core calls this when the LAST active job poll terminates → flush a board reload a
  // sibling write deferred while the job was polling (L3 codex MED). A passive reload (the editor
  // baseline still applies); if an edit is now open it defers again via the normal load() guard.
  function onJobsIdle() { flushDeferred(); }

  // A re-read this page OWES (a stale refusal, a write that landed while a job polled) is latched in
  // `deferredReload` until a load actually renders: a passive load that cannot run now (an editor open, a
  // load in flight, a job polling, a failed read) leaves the latch set, and every moment the block can lift
  // (a job ends, an editor closes, an in-flight load lands) flushes it. Without it a refused stale row
  // stayed on screen and could be sent again.
  function flushDeferred() {
    if (!deferredReload) return;
    deferredReload = false;
    rereadLatched();
  }
  function rereadLatched() {
    return load({ passive: true }).then(function (ok) { if (ok === false) deferredReload = true; return ok; });
  }
  // An editor closing lifts the block: the render core announces a retired stale form, and any other editor
  // closes by a click (cancel, save) or Escape; check after that handler has run.
  document.addEventListener("levain:editor-closed", function () { setTimeout(flushIfIdle, 0); });
  // capture phase: an inline editor's own handlers may stop propagation, and its close must still flush (the
  // timeout runs the check after that handler)
  document.addEventListener("click", function () { setTimeout(flushIfIdle, 0); }, true);
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") setTimeout(flushIfIdle, 0); }, true);
  // leaving a field lifts the focus block on a deferred read (the timeout lets focus settle on its next target first)
  // (a pointer still down means the blur is the start of a click on something: that click's own flush runs after it, so the target
  // is never torn out between the press and the release)
  let pointerDown = false;
  document.addEventListener("pointerdown", function () { pointerDown = true; }, true);
  document.addEventListener("pointerup", function () { pointerDown = false; }, true);
  document.addEventListener("pointercancel", function () { pointerDown = false; }, true);
  document.addEventListener("focusout", function () { if (!pointerDown) setTimeout(flushIfIdle, 0); }, true);
  function flushIfIdle() { if (deferredReload && !inflight && !editInProgress()) flushDeferred(); }

  // An involuntary re-read rebuilds the whole board (+ modal) via replaceChildren and
  // would discard an in-progress edit/capture textarea (intention-108). The PASSIVE triggers
  // (visibilitychange, the foreground) defer while the operator is mid-edit — never the
  // save's own commit→load reload (passive:false), which MUST rebuild to close the editor
  // and show saved state. Deferring loses nothing: the substrate only changes on a wrap,
  // so the next visibility flip / the save's reload re-reads.
  //
  // Fails CLOSED (block) when the render core or its predicate is absent — uniform with
  // core.js's missing-data-orig→dirty bias (complement+kimi L3). Only the passive triggers
  // consult this, so a false "editing" merely defers a harmless re-read; it never blocks
  // save or the initial boot (both passive:false).
  function editInProgress() {
    const d = window.LevainDashboard;
    if (d && typeof d.editHoldsRefresh === "function") return d.editHoldsRefresh();
    if (!d || typeof d.hasUnsavedEdit !== "function") return true;
    return d.hasUnsavedEdit();
  }
  // A passive read refused for an edit or a field in use stays OWED: the latch is set here, so every caller (the transport's reload,
  // the tab coming back, a stale re-read) is flushed by the same release, and a timer checks again once a focus-only hold has aged out.
  let idleT = 0;
  const IDLE_RECHECK_MS = 10250;
  function refuseForEdit(msg) {
    status(msg || "editing — the read waits until you finish");
    deferredReload = true;
    refuseArm();
    return false;
  }
  function refuseArm() { if (!idleT) idleT = setTimeout(function () { idleT = 0; flushIfIdle(); if (deferredReload) refuseArm(); }, IDLE_RECHECK_MS); }

  // DATA SOURCE (cockpit design §7). The page draws from the kernel's cockpit manifest + panel routes
  // (cockpit_view.js maps them onto the same components) and writes through /cockpit/verb (postVerb
  // below). `/?source=substrate` draws the legacy /substrate.json page, whose writes post /edit and
  // /action (a T2 write there is refused: it fires only through the broker).
  const useManifest = new URLSearchParams(window.location.search).get("source") !== "substrate";

  // ---- MANIFEST WRITES (R1): every affordance on the manifest source posts /cockpit/verb ----
  //
  // The page decides no tier. A gesture that may escalate asks the kernel first (a dry run) and fires on
  // the same click, naming the tier and gesture that dry run answered (`tier_seen`, `gesture_seen`): the
  // kernel refuses the fire if either moved in between. The authority is the token or the signature, never
  // a click, so there is no second click: the tier and "reported" are in the audit record, not in a prompt.
  // `live` is the write surface of the view last RENDERED: a write names the row version the operator saw,
  // so a re-read never moves it under an open form (the kernel's `stale` answer is the backstop).
  let live = null;
  let notice = null;        // what the last write said, drawn on its row once the board is rebuilt

  const KEYS_STORE = "levain_cockpit_keys";
  function readKeys() {
    try { const o = JSON.parse(window.sessionStorage.getItem(KEYS_STORE) || "{}"); return o && typeof o === "object" ? o : {}; }
    catch (_) { return {}; }
  }
  function writeKeys(o) { try { window.sessionStorage.setItem(KEYS_STORE, JSON.stringify(o)); } catch (_) { /* no storage: no replay */ } }

  // a stable string for a JSON value: keys sorted, so one request always has one fingerprint
  function canon(v) {
    if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
    if (v && typeof v === "object") return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
    return JSON.stringify(v === undefined ? null : v);
  }
  function newKey() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
      const a = new Uint8Array(16);
      window.crypto.getRandomValues(a);
      return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
    } catch (_) { return "idem-" + Date.now() + "-" + Math.random().toString(16).slice(2); }
  }

  // POST a body; {net:true} when the outcome is unknown (no answer, or a proxy's non-JSON 5xx), else
  // {ok, status, data}. An answer from the server is authoritative, whatever it says.
  async function send(route, body) {
    let res;
    try { res = await fetch(route, { method: "POST", headers: writeHeaders(), body: JSON.stringify(body) }); }
    catch (e) { return { net: true, message: e && e.message ? e.message : String(e) }; }
    let data = {};
    let parsed = true;
    try { data = await res.json(); } catch (_) { parsed = false; }
    if (!parsed && res.status >= 500) return { net: true, message: "HTTP " + res.status };
    if (res.status === 403 && writeTokenRequired && /token/i.test((data && data.message) || "")) dropToken();
    return { ok: res.ok, status: res.status, data: data || {} };
  }

  const NETWORK_MESSAGE = "Levain did not answer, so this change may not have been saved. Reload the page to see where it stands.";
  // answers after which the page's picture of the row or of the install is stale or unavailable
  const REREAD = new Set(["needs_intent", "stale", "tier_moved", "gesture_moved", "needs_broker", "standby", "not_built"]);

  // a landed write paints nothing, so the row's own "saving…" or an earlier refusal is cleared here rather than left for the
  // re-read, which can defer behind a polling job
  function clearBusy(key) {
    if (!key) return;
    for (const row of document.querySelectorAll("[data-row-key]")) {
      if (row.dataset.rowKey !== key) continue;
      for (const m of row.querySelectorAll(".edit-msg")) { m.className = "edit-msg"; m.textContent = ""; }   // busy, and an earlier refusal
    }
  }
  // The kernel's answer, drawn on its row the moment it arrives when it is one a person needs (a landed write says nothing:
  // the full re-read can take seconds). The row is not locked meanwhile: an edit sent
  // against the old row version is refused "stale" by the kernel.
  function paintNow(n) {
    if (!n || !n.key) return;
    for (const row of document.querySelectorAll("[data-row-key]")) {
      if (row.dataset.rowKey !== n.key) continue;
      const host = row.querySelector(".verb-actions") || row;
      let m = host.querySelector(".edit-msg");
      if (!m) { m = document.createElement("span"); host.appendChild(m); }
      m.className = "edit-msg " + n.cls;
      m.textContent = n.text;
      return;
    }
  }

  // What a person reads for an answer: a plain sentence, never a state code or an authority label. A landed write
  // says nothing (the row shows the new content); only a write that did not land, or is still waiting, speaks.
  const RESULT_WORDS = {
    COMMITTED_INCOMPLETE: "Only part of that change was saved. Check the row.",
    STALE: "That changed somewhere else first. Reload to see the new version.",
    REFUSED: "Levain refused that change.",
    EXPIRED: "That request ran out of time. Try again.",
    CANCELLED: "Cancelled.",
    WAITING: "Waiting for your signature.",
    applying: "Still saving."
  };
  // The page never shows the server's own text for a refusal: it can carry a path or an OS error (the exceptions are
  // SNOOZE_REFUSALS and AGENDA_DAY_REFUSAL, below). A code not listed here says only its code.
  const FAULT_WORDS = {
    stale: "That changed somewhere else first. Reload to see the new version.",
    needs_intent: "This needs a fresh confirmation. Try again.",
    tier_moved: "This needs a fresh confirmation. Try again.",
    gesture_moved: "This needs a fresh confirmation. Try again.",
    needs_broker: "That cannot be done from this page right now.",
    standby: "That cannot be done from this page right now.",
    not_built: "That cannot be done from this page yet.",
    not_initialised: "This install is not set up to do that yet.",
    install_disabled: "Writes are turned off for this install.",
    pending_cap: "Too many changes are waiting. Decide one first.",
    idempotency_conflict: "That is already being saved. Try again in a moment.",
    refused: "That was refused.",
    team_entry: "A teammate's entry cannot be removed from this page. Remove it with anneal's command line.",
    invalid_body: "That text cannot be saved: a line may not start with \"## \". Change that line and save again.",
    too_large: "That is too long to save. Shorten it and try again.",
    wrap_in_progress: "A memory update is running. Save again once it finishes.",
    not_editable: "That section is written by the memory update, not by hand.",
    not_found: "That is no longer there. Reload to see the current page.",
    bad_target: "That row could not be matched to what was shown. Reload and try again.",
    not_offered: "That is not available here any more. Reload and try again.",
    unknown_verb: "That is not available here any more. Reload and try again.",
    read_only: "This page is read-only: nothing here can be changed.",
    field_not_allowed: "This page and the server disagree about that request. Reload and try again.",
    route_broker: "This page and the server disagree about that request. Reload and try again.",
    bad_kind: "This page and the server disagree about that request. Reload and try again.",
    confirm_required: "That needs a confirmation. Try again and confirm it.",
    bad_request: "That request was not understood. Reload and try again.",
    intake_refused: "That line was not understood. Nothing was added.",
    source_unavailable: "The store behind that is unavailable right now. Nothing was added.",
    drop_refused: "The desk's store refused that line. Nothing was added.",
    bad_signature: "That signature was not accepted. Sign again.",
    sign_required: "That is sent only after your passkey signs it; nothing was sent. Send it again.",
    credential_required: "Saving needs the write token for this page. Open the page from the link that carries it.",
    // the refusals of the List's two irreversible writes (the Keep, the band row to agenda): one fixed line each, true to the cause
    // and to every place the server raises it; the server's own sentence is never shown (it can carry a raw exception)
    needs_signature: "This row is settled by signing: sign it, or answer it on its own sheet in Decisions.",
    rings_a_reminder: "This row rings a reminder, so it needs no agenda. Change the reminder itself.",
    agenda_resolved: "The agenda made for this row is no longer open, so it cannot bring the row back. Answer the row in words instead.",
    answer_refused: "The row was not answered. Reload the List and check it before trying again.",
    keep_key_retired: "This item was kept before and that memory was deleted, so it cannot be kept again. Compost the item by hand.",
    // the band row to agenda refuses a day with its own code (422). Only a day sentence of the shape below is shown as the host
    // sent it (see fault); any other message under this code reads this line
    bad_agenda_day: "That day cannot be used for an agenda. Pick a later day.",
    // the code the server answers AFTER the plant (see fault: the line names the day when the server sends one)
    agenda_planted_unanswered: "The agenda is planted, but the row may still be open. Reload the List and check; trying again is safe."
  };
  // The band row to agenda plants the agenda first and answers the row second. A failure BEFORE the plant is source_unavailable
  // (503) when the question store or the band listing cannot be read: the plain "Nothing was added." line is true for it. (The
  // plant's own store fault is store_unavailable (503), which has no line of its own here and reads the generic one.) A failure
  // after the plant is agenda_planted_unanswered (503), which
  // carries the day the agenda stands on (an earlier plant's day stands, so it can differ from the day asked); the row may or may
  // not be answered (the host can record the answer and then fail its read-back), so the line promises no more than reload, check,
  // and that a retry is safe (true for both of the server's messages: a retry finishes an open row, and on an answered row it is
  // refused "stale", which the page reloads on).
  function plantedLine(day) {
    if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return FAULT_WORDS.agenda_planted_unanswered;
    const D = window.LevainDashboard;
    const words = D && typeof D.dayYearWords === "function" ? D.dayYearWords(day) : day;
    return "The agenda for " + words + " is planted, but the row may still be open. Reload the List and check; trying again is safe.";
  }
  // The Keep writes the memory first and closes the item second. The code is raised only after the memory landed, so it is kept
  // either way: 409 = the item moved before it could be closed (look at the List), 503 = the store could not close it (the item
  // stays, and keeping it again writes the memory once). Any other status promises neither outcome. The episode id is shown only in
  // the shape the receipt line shows it.
  function keptNotClosedLine(status, episodeId) {
    const named = typeof episodeId === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(episodeId);
    const lead = named ? "Kept as a memory (episode " + episodeId + ")" : "Kept as a memory";
    if (status === 409) return lead + ", but the item changed before it could be closed. Reload the List to see where it stands.";
    if (status === 503) return lead + ", but the item could not be closed here and is still on the List. Keeping it again is safe: it writes the memory once.";
    return lead + ", and its current state could not be confirmed.";
  }
  const SNOOZE_REFUSALS = new Set(["pick a time after now", "pick a later day", "pick a day: that date could not be read"]);
  // The List's band row to agenda refuses a day inside the snooze window in words that give the rule ("<day> is within the
  // snooze window (...): an agenda is for a day after <day>"). They are composed from the day and the window
  // alone, so they are shown as sent, but only a message that opens the way the host's day refusals open and carries no path.
  const AGENDA_DAY_REFUSAL = /^(?:\d{4}-\d{2}-\d{2} is (?:not after today|within the snooze window)|an agenda's day is written YYYY-MM-DD)[^/\\]{0,280}$/;
  // Only a landed state is silent; a state this page does not know speaks too (never a quiet failure). Own-property lookups:
  // a state or code named "constructor" must not index into Object.prototype.
  function resultWords(state) {
    if (state === "COMMITTED" || state === "done") return null;
    return Object.prototype.hasOwnProperty.call(RESULT_WORDS, state) ? RESULT_WORDS[state] : "That did not finish. Check the row.";
  }

  function fault(r, what) {
    const d = r.data || {};
    const code = d.error || "HTTP " + r.status;
    let text = (Object.prototype.hasOwnProperty.call(FAULT_WORDS, code) && FAULT_WORDS[code]) || "That did not go through (" + code + ").";
    // a snooze's own refusal of the day or minute the operator typed: the host's three plain sentences, shown as sent so the
    // form says what to change. Only these exact sentences, only on that verb: any other bad_request keeps the fixed line.
    if (code === "bad_request" && r.status === 400 && what && what.verb === "intention_snooze" && typeof d.message === "string"
        && SNOOZE_REFUSALS.has(d.message)) text = d.message;
    if (code === "bad_agenda_day" && r.status === 422 && what && what.verb === "band_agenda" && typeof d.message === "string"
        && AGENDA_DAY_REFUSAL.test(d.message)) text = d.message;
    if (code === "agenda_planted_unanswered") text = plantedLine(d.day);
    if (code === "kept_not_closed") text = keptNotClosedLine(r.status, d.episode_id);
    // a Keep whose item moved (409) leaves the page holding a row that is gone or changed, so it reads again. The Keep's form holds
    // no text, so nothing in it holds that read off: it starts at once, and the redraw replaces the form while the notice stays on the row.
    if (REREAD.has(code) || r.status === 501 || (code === "kept_not_closed" && r.status === 409)) {
      notice = { key: (what && what.row_id) || null, text: text, cls: "err" };
      paintNow(notice);
      rereadLatched();   // a re-read that never discards an open form: it defers while one is dirty, and stays owed
      return { ok: false, error: code, message: text, status: r.status };
    }
    // `detail` is the server's own words for why, only for the semantic refusal of a band answer (it names the item it
    // refused); any other fault (a 503, a 5xx) shows the fixed line, never the server's raw text
    return { ok: false, error: code, message: text, status: r.status,
             // an intake refusal's reason is the parser's own sentence ("that time has passed"): the box shows it, since
             // the grammar lives on the server only
             detail: (code === "answer_refused" || code === "intake_refused") && r.status === 422 && typeof d.message === "string" ? d.message : "" };
  }

  // One write per fingerprint at a time: a second identical submit while the first is still asking, firing or
  // re-reading (a repeated Cmd+Enter, a double click) gets the first one's answer instead of minting a new key.
  const writing = new Map();

  async function postVerb(action, params, opts) {
    opts = opts || {};
    const head = (live && live.heads && live.heads[action.verb]) || {};
    const label = head.label || action.verb;
    // an undefined value is no value: it is not sent and not fingerprinted
    const given = {};
    for (const k of Object.keys(params || {})) if (params[k] !== undefined) given[k] = params[k];
    params = given;
    if (action.gesture === "refused" || action.gesture === "laptop-only") {
      return { ok: false, error: "gesture", message: "That cannot be done from this page." };
    }
    const t = action.target || {};
    const base = { verb: action.verb, panel_id: t.panel_id, params: params || {} };
    if (typeof t.row_id === "string") { base.row_id = t.row_id; base.row_version = t.row_version; }
    const pv = opts.panelVersion !== undefined && opts.panelVersion !== null ? opts.panelVersion : t.panel_version;
    if (typeof pv === "string") base.panel_version = pv;
    if (head.confirm === true && opts.confirm === true) base.confirm = true;
    // the request as the operator asked it, before any tier is known: a stored envelope is found by it. A signed send
    // (opts.signTxn, the draft's one identity) is found by that identity and its words, never by the sign request it
    // names: a reload that signs the same draft again replays the envelope (and the key) its first send kept
    const txn = typeof opts.signTxn === "string" && opts.signTxn ? opts.signTxn : null;
    const fpParams = Object.assign({}, base.params);
    if (txn) delete fpParams.sign_request;
    const fpKey = canon([base.verb, base.panel_id, base.row_id || null, base.row_version || base.panel_version || null,
                         fpParams, base.confirm === true, txn]);
    if (writing.has(fpKey)) return writing.get(fpKey);
    const run = fire(base, head, label, action, fpKey, txn);
    writing.set(fpKey, run);
    try { return await run; } finally { writing.delete(fpKey); }
  }

  async function fire(base, head, label, action, fpKey, txn) {
    const what = { row_id: base.row_id, label: label, verb: base.verb };
    // A T2+ fire (or an idempotent or signed verb's) carries one key, minted for this request and kept with the whole
    // envelope until the kernel answers. A kept envelope is sent again BEFORE any new dry run: the write it
    // carries may have landed and moved the row, and only a replay under its own key gets that answer back.
    let env = (readKeys()[fpKey] || {}).envelope || null;
    if (env === null) {
      let tier = "C1", gesture = action.gesture, intent = null;
      const mayEscalate = !(action.gesture === "direct" && (!Array.isArray(action.escalates) || action.escalates.length === 0));
      if (mayEscalate) {
        // a signed send's preview names its draft's key too: the kernel admits a signed request only under that key
        const d = await send("/cockpit/verb", Object.assign({}, base, { dry_run: true }, txn ? { idempotency_key: "sign-" + txn } : {}));
        if (d.net) return { ok: false, error: "network", message: NETWORK_MESSAGE };
        if (!d.ok) return fault(d, what);
        tier = d.data.tier; gesture = d.data.gesture;
        if (gesture === "refused" || gesture === "laptop-only") {
          return { ok: false, error: "gesture", message: "That cannot be done from this page." };
        }
        if (tier !== "C1") intent = gesture === "sign" ? "sign" : "reported";
      }
      env = Object.assign({}, base);
      if (intent) { env.intent = intent; env.tier_seen = tier; env.gesture_seen = gesture; }
      if (txn || tier !== "C1" || head.idempotent === true) {
        // every signed send carries its draft's identity as its key, whatever its tier (the kernel dedupes a signed
        // verb's key at any tier), so a retry, another tab or a reload of the same draft applies it once
        env.idempotency_key = txn ? "sign-" + txn : newKey();
        const keys = readKeys(); keys[fpKey] = { envelope: env }; writeKeys(keys);
      }
    }
    const r = await send("/cockpit/verb", env);
    if (r.net) return { ok: false, error: "network", message: NETWORK_MESSAGE };   // the envelope stays, so a retry replays it
    // a 5xx is indeterminate (the write may have landed before it): the envelope stays for the retry
    if (env.idempotency_key && r.status < 500) { const keys = readKeys(); delete keys[fpKey]; writeKeys(keys); }
    if (!r.ok) return fault(r, what);
    const d = r.data;
    const state = typeof d.state === "string" && d.state ? d.state : "done";
    const words = resultWords(state);
    notice = words ? { key: base.row_id || null, text: words, cls: state === "WAITING" || state === "applying" ? "preview" : "err" } : null;
    if (notice) paintNow(notice); else clearBusy(base.row_id || null);
    const touched = touchedOf(d);
    if (touched && !words) paintRow(base.row_id || null, d.row, base.verb);
    const redrew = await afterWrite(touched);
    if (!words && redrew === false && !pendingReload) {   // landed, but the page could not redraw: say so, the silence was for a page that updates
      notice = { key: base.row_id || null, text: "Saved. The page could not read the new state yet; it will read again.", cls: "note" };
      paintNow(notice);
    }
    return { ok: true, data: d };
  }

  // The refetch each write is waiting on, by panel id: { done } of the last one a write asked for, held only while it is in
  // flight (its own settling removes it, whatever it answered).
  const writeRefetches = new Map();
  let dedupInstalled = false;
  let viewRefetch = null;             // the view's own refetch, kept once the de-dup stands in front of it

  // The panels a landed write says it changed: a non-empty list of ids, else null (an older server, which says nothing:
  // the whole-board path below runs).
  function touchedOf(d) {
    if (!d || !Array.isArray(d.touched)) return null;
    const ids = Array.from(new Set(d.touched.filter((x) => typeof x === "string" && x)));
    return ids.length ? ids : null;
  }

  // Whether a snooze's new `next` ("YYYY-MM-DD" or "YYYY-MM-DDTHH:MM") is after now on this page's own clock: a day-only date
  // is in the future from the day after today, a timed one when its minute is ahead. Anything else (a clear, a past date, a
  // string that is no date) is not.
  function isFutureNext(next) {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/.exec(typeof next === "string" ? next : "");
    if (!m) return false;
    const now = new Date();
    if (m[4] !== undefined) return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]).getTime() > now.getTime();
    return Date.UTC(+m[1], +m[2] - 1, +m[3]) > Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  }

  // The row as the write left it, drawn at once from the write's own answer (the panel read after it can take seconds): the
  // offered actions take the new row version (the next write on this row must not be refused stale meanwhile), and the person
  // SEES the change. A snooze to a future date takes the row out of the List's now-view: it is collapsed and hidden from
  // assistive tech, never removed, so the panel read that follows settles it. Otherwise the new words go into the row's clause
  // where the row has one, and any other changed field dims the row with a title that says it was updated. Inline styles and
  // attributes only. A `row` that names another row than the one written is not painted.
  function paintRow(key, row, verb) {
    if (!key) return;
    const have = row && typeof row === "object" ? row : null;
    if (have && typeof have.id === "string" && have.id !== key) return;
    const ver = have && typeof have.row_version === "string" ? have.row_version : null;
    const target = live && live.rows ? live.rows[key] : null;
    if (target && ver) {
      target.row_version = ver;
      if (Array.isArray(target.actions)) for (const a of target.actions) if (a && a.target && typeof a.target === "object") a.target.row_version = ver;
    }
    const leaves = verb === "intention_surface_at" && !!have && isFutureNext(have.next);
    const words = have && typeof have.text === "string" ? have.text : null;
    const others = have ? Object.keys(have).filter((k) => k !== "id" && k !== "row_version" && k !== "text") : [];
    for (const el of document.querySelectorAll("[data-row-key]")) {
      if (el.dataset.rowKey !== key) continue;
      if (ver) el.dataset.rowVersion = ver;
      if (have && typeof have.next === "string") el.dataset.rowNext = have.next;
      el.dataset.rowUpdated = "1";
      if (el.classList && typeof el.classList.add === "function") el.classList.add("row-updated");
      if (!have || !el.style) continue;
      if (leaves) {
        el.style.display = "none";
        if (typeof el.setAttribute === "function") el.setAttribute("aria-hidden", "true");
        continue;
      }
      let placed = false;
      if (words !== null && typeof el.querySelector === "function") {
        const clause = el.querySelector(".clause");
        if (clause) { clause.textContent = words; placed = true; }
      }
      // a text the row has no clause for, or any other changed field (the row has no obvious place for it): dim the row
      if (others.length || (words !== null && !placed)) {
        el.style.opacity = "0.55";
        el.title = "Updated; the list is refreshing";
      }
    }
  }

  // A call through the view for a panel the write refetched, while the write's read R is still in flight, is never handed R: R
  // began with the write's response, and the announcement behind the call may be about a change R predates (another writer, a
  // refresher tick, an agent), so R's payload could be the older one and nothing would announce again. The call waits for R to
  // settle (answered, faulted or superseded) and then makes ONE read of its own, shared by every call that arrived meanwhile.
  // The wait is bounded by PANEL_TIMEOUT_MS, since R has no deadline of its own; when it runs out the entry is dropped, so a hung
  // R holds no later call at all. The call carries the panel id only, so there is no etag to compare against R's: a call that
  // lands in flight always costs the one extra read. Once R has settled its entry is gone and a call reads for itself at once.
  function installRefetchDedup() {
    const V = window.LevainCockpitView;
    if (dedupInstalled || !V || typeof V.refetchPanel !== "function") return;
    dedupInstalled = true;
    viewRefetch = V.refetchPanel;
    V.refetchPanel = function (pid) {
      const w = writeRefetches.get(pid);
      if (!w) return viewRefetch.apply(V, arguments);
      if (!w.next) {
        w.next = new Promise(function (resolve, reject) {
          let went = false, timer = 0;
          const go = function () {
            if (went) return;
            went = true;
            clearTimeout(timer);
            if (writeRefetches.get(pid) === w) writeRefetches.delete(pid);
            try { resolve(viewRefetch.call(V, pid)); } catch (e) { reject(e); }
          };
          timer = setTimeout(go, PANEL_TIMEOUT_MS);
          w.done.then(go, go);
        });
      }
      return w.next;
    };
  }

  // Refetch ONLY the panels a write touched, in the place they are drawn, in the background: nothing here waits on a read and
  // no whole-board load is waited on. A panel the page cannot repaint in place (answered false) asks for the board read, below.
  function refetchTouched(ids) {
    installRefetchDedup();
    const V = window.LevainCockpitView;
    const read = viewRefetch || (V && V.refetchPanel);
    if (typeof read !== "function") return false;
    let boardAsked = false;
    for (const pid of ids) {
      let done;
      try { done = Promise.resolve(read.call(V, pid)); } catch (_) { done = Promise.resolve("fault"); }
      done = done.catch(() => "fault");
      const mine = { done };
      writeRefetches.set(pid, mine);
      done.then(() => { if (writeRefetches.get(pid) === mine) writeRefetches.delete(pid); });
      // A panel the view cannot repaint in place (false), or whose read faulted or threw ("fault"; the repaint the write drew
      // would otherwise stand with nothing to settle it), asks for the board read, NOT passive and not awaited here: the rebuild
      // it makes is what closes the form or the state editor the write came from (a passive read is refused for an open edit
      // and would leave that form on "saving..."), and a non-passive read that finds one in flight is latched to run after it.
      // One write asks for it once, whatever number of its panels answered so, and not at all when a re-read is already owed.
      // A board read that fails says so through the page's own failed-read line (load() sets it).
      // While a job polls that rebuild would detach the job's live result box, the case the job deferral exists for: the
      // fallback asks no read now and latches `deferredReload`, which onJobsIdle() flushes as one board read.
      done.then((r) => {
        if ((r !== false && r !== "fault") || boardAsked || pendingReload) return;
        if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function" && window.LevainDashboard.jobsActive()) {
          deferredReload = true;
          return;
        }
        boardAsked = true;
        load().catch(() => {});
      });
    }
    return true;
  }

  // after a landed write: with the panels it touched, an in-place background refetch of those (resolves at once, true when
  // started); without (an older server), the reload of the whole board, deferring while an async job polls (as postWrite does).
  // A board read already out when the write lands began before it and may draw the pre-write board over the rows the refetches
  // paint, so one re-read is latched after it (the latch a non-passive load sets when it finds one in flight). While an async
  // job polls, the refetch still runs (the write's own change is real now) but the data a job verb changes moves when the job
  // ends, so the same deferral the whole-board path sets is set too: the board is read once more when the last job ends.
  async function afterWrite(touched) {
    if (touched) {
      if (inflight) pendingReload = true;
      if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function" && window.LevainDashboard.jobsActive()) deferredReload = true;
      return refetchTouched(touched);
    }
    if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function" && window.LevainDashboard.jobsActive()) {
      deferredReload = true;
      return undefined;
    }
    return load();   // false when the read or the render failed
  }

  // The legacy request kinds the render core still speaks, translated onto cockpit verbs. Only the verb
  // name, the params and the target are read from the request; everything else the kernel decides.
  const ROW_KEY = { intention_id: "intention:", episode_id: "episode:", edit_id: "edit:" };
  const NOT_PARAMS = new Set(["kind", "intention_id", "episode_id", "edit_id", "expect_disposition", "confirm",
    "panel_version", "source", "heading", "expected_body"]);
  const VERB_OF_KIND = { state: "section_edit" };

  function panelActionFor(verb, req) {
    // every capture, a note included, is the Tray's create; the disposition rides in params
    // the List's panel keeps its wire id `tray`; its create (intention_seed) and its intake box (list_intake) are both offered there
    const want = verb === "intention_seed" || verb === "list_intake" ? "tray" : null;
    for (const pid of Object.keys(live.panels)) {
      if (want && pid !== want) continue;
      const a = live.panels[pid].actions.find((x) => x && x.verb === verb);
      if (a) return a;
    }
    return null;
  }

  async function manifestCommit(req) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const verb = Object.prototype.hasOwnProperty.call(VERB_OF_KIND, req.kind) ? VERB_OF_KIND[req.kind] : req.kind;
    const params = {};
    for (const k of Object.keys(req)) if (!NOT_PARAMS.has(k)) params[k] = req[k];
    let action = null;
    const rk = Object.keys(ROW_KEY).find((k) => Object.prototype.hasOwnProperty.call(req, k));
    if (rk) {
      const row = live.rows[ROW_KEY[rk] + req[rk]];
      action = row ? row.actions.find((a) => a && a.verb === verb) : null;
    } else {
      action = panelActionFor(verb, req);
    }
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit did not offer " + verb + " here" };
    return postVerb(action, params, { confirm: req.confirm === true, panelVersion: req.panel_version });
  }

  // a downstream verb's compose box. A box that names its panel posts THAT panel's offer, and refuses when the
  // panel no longer offers the verb: never another panel's offer of the same verb (KE3 L3 cx2). The idempotency
  // key argument is the legacy route's; the cockpit route keys its own envelope (a signed send's by `signTxn`, its
  // draft's identity).
  async function manifestAction(verb, params, confirm, _idempotencyKey, panelId, signTxn) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    let action = null;
    if (typeof panelId === "string") {
      const p = Object.prototype.hasOwnProperty.call(live.panels, panelId) ? live.panels[panelId] : null;
      action = p ? p.actions.find((x) => x && x.verb === verb && !(x.target && typeof x.target.row_id === "string")) || null : null;
      if (!action) return { ok: false, error: "not_offered", message: "the panel " + panelId + " no longer offers " + verb + "; nothing was sent" };
    } else {
      action = panelActionFor(verb, null);
    }
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit did not offer " + verb + " here" };
    return postVerb(action, params, { confirm: confirm === true, signTxn: signTxn });
  }

  // cancel a waiting pending: the gesture's intent, no preview, and a key of its own. The whole envelope
  // is kept by pending id until the kernel answers, so a click after a lost answer replays the same key.
  async function cancelPending(c) {
    const slot = "cancel:" + c.pending_id;
    const keys = readKeys();
    let body = keys[slot] && keys[slot].envelope;
    if (!body) {
      body = { idempotency_key: newKey() };
      if (c.tier && c.tier !== "C1") body.intent = c.gesture === "sign" ? "sign" : "reported";
      keys[slot] = { envelope: body };
      writeKeys(keys);
    }
    const route = c.route || "/cockpit/pending/" + encodeURIComponent(c.pending_id) + "/cancel";
    const r = await send(route, body);
    if (r.net) return { ok: false, error: "network", message: NETWORK_MESSAGE };   // the envelope stays
    if (r.status < 500) { const after = readKeys(); delete after[slot]; writeKeys(after); }   // a 5xx is indeterminate: keep it
    if (!r.ok) return fault(r, { row_id: null, label: "cancel" });
    const d = r.data;
    const cancelWords = resultWords(d.state || "done");
    notice = cancelWords ? { key: null, text: cancelWords, cls: "note" } : null;
    await afterWrite();
    return { ok: true, data: d };
  }

  // answer an agent's question (Q-2b): the row's own offered question_answer, posted like any row verb (C1, one tap
  // = one answer, so no confirm); the kernel's 409 on a row that changed is the answer when it was answered elsewhere
  async function answerQuestion(rowId, params) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const row = live.rows[rowId];
    const action = row && Array.isArray(row.actions) ? row.actions.find((a) => a && a.verb === "question_answer") : null;
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit no longer offers an answer here; nothing was sent" };
    return postVerb(action, params, {});
  }

  // The NEEDS YOU band as this credential is shown it (Q17-ALL S5b): reading it numbers what is shown and records the view
  // the answer below binds to. Rejects when the cockpit has no band to show (no question store) or cannot read it.
  function bandRead() { return authedReadJson("/cockpit/band.json"); }

  // answer the band by its shorthand ("15b", "1a 2b"): the decisions panel's offered band_answer (C1, unsigned, a human
  // channel), bound to the view the page was shown ({store_id, view_id}). A 409 means another view was shown since.
  async function answerBand(text, storeId, viewId) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const action = panelActionFor("band_answer", null);
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit no longer offers an answer here; nothing was sent" };
    return postVerb(action, { text: text, store_id: storeId, view_id: viewId }, {});
  }

  // dismiss an agent's note: the row's own offered note_dismiss (C1, no confirm, no params). It tells the agent nothing;
  // the kernel's 409 on a row that changed is the answer when it was dismissed elsewhere
  async function dismissNote(rowId) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const row = live.rows[rowId];
    const action = row && Array.isArray(row.actions) ? row.actions.find((a) => a && a.verb === "note_dismiss") : null;
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit no longer offers a dismiss here; nothing was sent" };
    return postVerb(action, {}, {});
  }

  // clear or reopen a desk review item: the row's own offered review_clear / review_reopen (C1, no confirm, no
  // params). The kernel refuses it on a shared surface token (a paired device only) and 409s a row that changed. The
  // signed clear (review_clear_signed) carries {item, sign_request} and its draft's `signTxn`, which keys the send
  async function reviewItem(rowId, verb, params, signTxn) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const row = live.rows[rowId];
    const action = row && Array.isArray(row.actions) ? row.actions.find((a) => a && a.verb === verb) : null;
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit no longer offers that here; nothing was sent" };
    return postVerb(action, params || {}, signTxn ? { signTxn: signTxn } : {});
  }

  // The row actions the cockpit offered on a row now, and one of them posted by name (the agents card's detail view): the row's own
  // offered action, so a verb the kernel did not offer on it is never sent. `confirm` is the second tap of a verb that asks one.
  function rowActions(rowId) {
    const row = live && live.rows ? live.rows[rowId] : null;
    return row && Array.isArray(row.actions) ? row.actions : [];
  }
  async function rowVerb(rowId, verb, params, confirm) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const action = rowActions(rowId).find((a) => a && a.verb === verb);
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit no longer offers that here; nothing was sent" };
    return postVerb(action, params || {}, { confirm: confirm === true });
  }

  // Draw what the last write said on its row (or in the status line when the row is gone). The status
  // line carries it either way.
  function showNotice() {
    if (!notice) return;
    const n = notice;
    notice = null;
    status(n.text);
    if (!n.key) return;
    const rows = document.querySelectorAll("[data-row-key]");
    for (const row of rows) {
      if (row.dataset.rowKey !== n.key) continue;
      const host = row.querySelector(".verb-actions") || row;
      let m = host.querySelector(".edit-msg");
      if (!m) { m = document.createElement("span"); host.appendChild(m); }
      m.className = "edit-msg " + n.cls;
      m.textContent = n.text;
      return;
    }
  }


  let heldNext = null;
  // `isCurrent()`: this read is still the page's current one (a superseded read's late answers write nothing the board keeps)
  async function loadManifestView(passive, onPartial, isCurrent) {
    const usedToken = storedToken();      // the credential this read goes out with: a decline drops that one, never a newer one
    let manifest;
    manifest = await authedReadJson("/cockpit/manifest.json");   // settles the token: panels do not re-prompt
    // A writable install that wants a token and read the manifest without one (a loopback read needs none)
    // would draw every action refused. Ask for the token now, then read again as that credential.
    const cred = manifest && manifest.credential;
    if (manifest && manifest.transport && typeof manifest.transport.owner_key === "boolean") tokenAuth = manifest.transport.owner_key ? "owner" : "pair";
    const wantsToken = !!(manifest && manifest.transport && manifest.transport.write_token_required === true
      && cred && cred.class === "none");
    if (wantsToken && !passive && !tokenDeclined) {
      // a newer token stored meanwhile (another tab, an earlier page of this tab) is read with, never prompted over: the prompt
      // itself drops a paired device's token first, so the comparison has to come before it
      if (storedToken() !== usedToken) manifest = await authedReadJson("/cockpit/manifest.json");
      else if (promptForToken()) manifest = await authedReadJson("/cockpit/manifest.json");
      else { tokenDeclined = true; if (storedToken() === usedToken) dropToken(); }   // a decline drops the token it read with, not a newer one
    }
    const fetchedAt = Date.now();
    const ids = Object.keys(manifest.panels);
    const panelHeads = Object.create(null);   // panel url -> the manifest's head for it, the source of the fields an ETag leaves out
    for (const id of ids) panelHeads[panelUrl(id)] = manifest.panels[id];
    const panels = Object.create(null);
    // the credential this board is read as: the bodies kept from an earlier read are drawn again only if they were read as it, and a body
    // this read lands is kept only while the page still holds it (a token dropped mid-read forgets the kept ones, see forgetLanded)
    const credOf = () => { const c = manifest.credential; return (c && typeof c.class === "string" ? c.class : "") + "\n" + storedToken(); };
    const readAs = credOf();
    const sameAs = () => credOf() === readAs;
    if (onPartial && isCurrent() && bootLandedAs !== readAs) { bootLanded = Object.create(null); bootLandedAs = readAs; }
    const seed = onPartial && bootLandedAs === readAs ? Object.assign(Object.create(null), bootLanded) : null;
    // One shared re-auth for the batch: if the token rotated after the manifest read, every panel 403s
    // together, so the first one prompts (or drops the dead token) and the rest wait on that same answer.
    let reauth = null;
    // each panel's last attempt went out as this credential (a re-auth's second attempt goes out as the new one): what its body was read as
    const sentAs = Object.create(null);
    const fetchPanel = (url, signal) => { sentAs[url] = credOf(); return fetchPanelConditional(url, panelHeads[url], signal); };
    // THE credential check, the one every draw of this read takes its bodies through (each part, and the whole): a body is drawn only
    // while the page holds the credential it was read as, the kept seed (read as `readAs`) and each body this read landed alike; a failed
    // read carries no body and is always drawn. A page never draws a body read under a credential it no longer holds.
    const drawable = () => {
      const now = credOf(), out = Object.create(null);
      if (seed && now === readAs) Object.assign(out, seed);
      for (const id of Object.keys(panels)) {
        const p = panels[id];
        if ((p && p.error !== undefined) || sentAs[panelUrl(id)] === now) out[id] = p;
      }
      return out;
    };
    pruneHeldPanels(ids);
    // At most PANEL_READ_CONCURRENCY panel reads are in flight, the rest queue in manifest order: one page
    // bursting ~28 reads at once, or two open pages, would otherwise meet the server's concurrency cap.
    // Every read settles (the worker catches its own failure) and so frees its slot.
    const queue = drawOrder(manifest, ids);
    // a cold first board draws every panel's shell at once and then each panel as it lands (onPartial); a refresh keeps showing the
    // last board until the new one is whole
    const pacer = onPartial ? partialPacer(ids.length, (n) => {
      const build = () => window.LevainCockpitView.fromManifest({ manifest: manifest, panels: boardPanels(ids, drawable()) }, { elapsedMs: Date.now() - fetchedAt });
      onPartial(build, n, ids.length, () => ids.filter((id) => Object.prototype.hasOwnProperty.call(drawable(), id)).length);
    }, { now: Date.now, setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) }) : null;
    const clock = { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) };
    const io = {
      attempt: (url) => timedAttempt(fetchPanel, url, clock),
      isReject: isTokenReject,
      reauth: () => {
        // a passive refresh never steals focus with a prompt over an open editor (as the legacy path does)
        reauth = reauth || Promise.resolve().then(() => {
          if (passive && editInProgress()) return false;
          if (passive && tokenDeclined) return false;
          if (promptForToken()) return true;
          tokenDeclined = true;
          dropToken();
          return false;
        });
        return reauth;
      },
      token: storedToken, drop: dropToken,
    };
    const readOne = async (id) => {
      try {
        panels[id] = await readPanelBody(panelUrl(id), io);
        // kept for a later read of this cold board only from the current read, and only while the page holds the credential it was read as
        if (onPartial && !wholeDrawn && isCurrent() && sameAs() && bootLandedAs === readAs) bootLanded[id] = panels[id];
      } catch (e) {
        // keep the reason: the mapper draws this panel as unavailable with it, never as an empty one
        panels[id] = { error: e && e.message ? e.message : String(e) };
      }
      if (isCurrent()) boardProgress();     // a superseded read's late answer is no sign the board is answering
      if (pacer) pacer.settled();
    };
    const worker = async () => { while (queue.length) await readOne(queue.shift()); };
    if (pacer) pacer.first();     // every panel's shell, loading, before any panel read has been issued
    try { await Promise.all(Array.from({ length: Math.min(PANEL_READ_CONCURRENCY, ids.length) }, worker)); }
    finally { if (pacer) pacer.stop(); }
    // the whole draw takes its bodies through the same check: a body read as a credential the page no longer holds (a token dropped or
    // changed while this read was out) ends the read undrawn, and load() reads the board again as the credential the page holds now
    const held = drawable();
    if (ids.some((id) => !Object.prototype.hasOwnProperty.call(held, id))) {
      const e = new Error("the write token changed while the board was read");
      e.credentialChanged = true;
      throw e;
    }
    // the panels this board was drawn from, each with the head etag it was drawn under: the change stream
    // resyncs over these ids, and the poll fallback compares the etags
    // `err` marks a panel this read could not read (drawn as unavailable): a failure the stream reads again on its bounded schedule
    heldNext = Object.fromEntries(ids.map((id) => {
      const p = panels[id], unread = !!p && p.error !== undefined && p.status === undefined;
      return [id, { etag: unread ? null : (manifest.panels[id] || {}).etag || null, err: unread ? String(p.error || "unreadable") : "" }];
    }));
    return window.LevainCockpitView.fromManifest({ manifest: manifest, panels: held }, { elapsedMs: Date.now() - fetchedAt });
  }

  async function load(opts) {
    const passive = !!(opts && opts.passive);
    // a refusal is not unsaid by the ASKING for another read (a foreground that is deferred for an edit made no request, one in flight has not
    // answered): it ends at a read's answer (a good one, or a failure that is not a refusal)
    if (inflight) {
      // A passive read is droppable (next trigger re-reads); a non-passive save/boot
      // reload must NOT be — latch it to re-run when the in-flight load settles.
      if (!passive) pendingReload = true;
      return false;
    }
    // If the render core didn't load (reorder / serve failure), say so plainly
    // rather than throwing an opaque TypeError after a successful fetch.
    if (!window.LevainDashboard || typeof window.LevainDashboard.render !== "function") {
      status("render core failed to load");
      boardMark(true);     // no whole draw is coming (the core is missing): a driver or a deep link waiting on the signal would only stall
      return false;
    }
    // Pre-fetch fast path: don't even fetch if we already know an edit is open.
    if (passive && editInProgress()) return refuseForEdit();
    inflight = true;
    inflightAt = Date.now();
    inflightPassive = passive;
    const myGen = ++loadGen;
    let landed = false, reread = false;
    fresh();
    try {
      if (fragmentTokenPending) await fragmentTokenPending;   // a link's token is being checked: read with the token that settles
      if (useManifest) {
        // the first board draws as its panels land: the cold "connecting..." line goes with the first useful content
        const drawView = (mview) => {
          writeTokenRequired = mview.write_token_required === true;
          const liveBefore = live;
          live = { rows: mview.targets.rows, panels: mview.targets.panels, heads: mview.verb_heads };   // published before the draw: an open detail rebuilds inside render and must read THIS read's actions
          try {
            window.LevainDashboard.render(mview, mview.writable
              ? { commit: manifestCommit, commitAction: manifestAction, cancelPending, answerQuestion, bandRead, answerBand, dismissNote, reviewItem, onJobsIdle, recall } : { recall });
          } catch (e) { live = liveBefore; throw e; }
        };
        let partialDrawn = false;
        // a board not yet whole draws as it lands, on every read of it: a second read of a cold board (the first superseded, or failed after a part)
        // keeps the shells and what had landed, never waits for its whole set
        const onPartial = wholeDrawn ? null : (build, n, total, landedCount) => {
          runPartial(() => myGen === loadGen && !editInProgress() && !holdSpent, build, (pview) => {     // holdSpent: the watchdog released the page, it is not made inert again
            boardMark(false);     // a part: inert, and not whole (before the draw, so a draw that throws midway still leaves nothing to act on)
            drawView(pview);
            if (landedCount() > 0) partialDrawn = true;     // the shells alone are not a part, so they never make the whole draw refuse for an edit
            if (storeEl) storeEl.removeAttribute("data-cold");
            drawnOnce();
            status("reading " + n + " of " + total + " panels...");
          });
        };
        const mview = await loadManifestView(passive, onPartial, () => myGen === loadGen);
        if (myGen !== loadGen) return false;     // superseded while it was stuck: the newer read owns the board
        if (!mayDrawWhole(passive, partialDrawn, editInProgress())) { notice = null; return refuseForEdit(); }
        drawView(mview);
        bootLanded = Object.create(null);
        boardMark(true);     // the whole board, drawn: live, and the signal, in this same turn
        if (heldNext) {    // published once drawn: only then can a panel be refetched in place
          window.LevainCockpitHeld = heldNext;
          document.dispatchEvent(new CustomEvent("levain:cockpit-held"));   // the change stream starts, or restarts, on this
        }
        lastWholeAt = Date.now();
        status("");     // the transient line is cleared; the freshness line says how current the page is
        showNotice();
        landed = true; drawnOnce(true);
        return true;
      }
      let res = await fetch("/substrate.json", { cache: "no-store", headers: readHeaders() });
      // OFF-BOX BOOTSTRAP (intention-220): an off-box writable surface gates the substrate READS. On the
      // first load we hold no token yet (and don't yet know one is required — that flag rides INSIDE
      // substrate.json, the chicken-and-egg). A token-class 403 means "off-box surface, token needed":
      // prompt once, store, retry. A read-only mesh surface never 403s here (its reads are token-free),
      // so it never prompts — iPad VIEWING stays open. The token, once entered, also unlocks writes.
      if (res.status === 403 && await isTokenReject(res)) {
        // Don't pop a BLOCKING prompt over an edit the operator opened DURING this fetch on a PASSIVE
        // re-read (visibilitychange): re-check here, mirroring the post-fetch render guard
        // below, so a background token-403 can't steal focus from in-progress text (complement L3
        // FIND-1 / the intention-108 "passive never disrupts an edit" invariant). The next explicit
        // boot/save load (passive:false) re-prompts.
        if (passive && editInProgress()) return refuseForEdit();
        if (promptForToken()) res = await fetch("/substrate.json", { cache: "no-store", headers: readHeaders() });
        if (res.status === 403) {
          // Still refused after the retry (wrong/rotated token, or the operator cancelled): drop the
          // stored token so the next explicit load re-prompts, and surface it plainly. Scoped to
          // the token-reject branch: a non-token 403 (Host/CSRF — plain-text body) never reaches here,
          // so it falls through to the generic read-failed path below with the stored token intact.
          dropToken();
          failure = { why: "off-box token missing or invalid", at: Date.now(), refused: true };
          status("read refused — off-box token missing or invalid");
          stampFailure = true;
          boardMark(true);     // refused: no whole draw is coming for this read
          return false;
        }
      }
      if (!res.ok) { const he = new Error("HTTP " + res.status); he.refused = res.status === 401 || res.status === 403; throw he; }
      const view = await res.json();
      if (myGen !== loadGen) return false;     // superseded while it was stuck: the newer read owns the board
      // Post-fetch race re-check (codex L3 HIGH): a passive load that STARTED clean, then
      // the operator opened/typed an editor WHILE this fetch was in flight — rebuilding
      // now would still discard it. The save path (passive:false) always applies.
      if (passive && editInProgress()) return refuseForEdit();
      // Track whether THIS surface demands the write token so commit()'s writeHeaders attaches it.
      // True on any writable surface (K2a), false on a read-only one.
      writeTokenRequired = view.write_token_required === true;
      // Gate the write transport on the substrate being writable (NO THEATER): a
      // read-only source (no install root → POST /edit 422s) renders with no edit
      // affordances at all, matching the server. An older payload without
      // `writable` defaults to writable, so existing installs are unchanged.
      // `recall` (episode search) is a READ — injected in BOTH branches so search works on a
      // read-only surface too; the write transports stay gated on `writable` (NO THEATER).
      window.LevainDashboard.render(view, view.writable === false ? { recall } : { commit, commitAction, commitJob, pollJob, onJobsIdle, recall });
      boardMark(true);
      lastWholeAt = Date.now();
      status("");
      landed = true; drawnOnce(true);
      return true;
    } catch (e) {
      if (myGen !== loadGen) return false;     // a superseded read's failure says nothing about the page now
      // the token changed while the board was read (nothing of it was drawn as the credential the page no longer holds): no failure and no
      // backoff, the board is read again at once as the page's credential now, as a passive read (an open edit defers it, see refuseForEdit)
      if (e && e.credentialChanged) { reread = true; return false; }
      const refused = !!(e && e.refused);
      // the reason, and for a refusal the remedy, are written where a touch screen reads them (the stamp), not only in the line's title
      status(refused ? "not signed in: pair this device again" : "read failed: " + (e && e.message ? e.message : e));
      stampFailure = true;
      failure = { why: String(e && e.message ? e.message : e || "read failed"), at: Date.now(), refused: refused };
      // a part with the watchdog armed stays inert and unsigned (the retry's whole draw replaces it and sets the signal, or the watchdog
      // releases it); anything else (no part drawn, a refusal, or the watchdog already fired) releases the page as it is, and the retry
      // below still reads again
      if (!holdingPart(refused)) boardMark(true);
      retryUntilDrawn(e && e.message ? e.message : String(e || "read failed"), refused);
      return false;
    } finally {
     if (myGen === loadGen) {
      inflight = false;
      fresh();
      // Run a reload that arrived (authoritative, non-passive) while we were in flight.
      // Always non-passive → never deferred, so a saved edit's state always lands.
      if (pendingReload) { pendingReload = false; load(); }
      // a read that ended because the token changed under it (see the catch above) is made again now
      else if (reread) load({ passive: true });
      // a passive read dropped because THIS load was in flight is still owed: flush once this one has rendered
      // (never after a failed read, which would loop on a dead server)
      else if (landed) flushIfIdle();
     }
    }
  }

  // The app came back to the front (pwa_resume.js): repaint from a fresh read now. The page keeps showing its last state meanwhile.
  document.addEventListener("levain:resume", function (ev) {
    // a read that began before the page went to the background crossed the freeze: its socket is not to be trusted
    const away = ev && ev.detail && ev.detail.hiddenAt;
    let passive = true;
    if (inflight && ((away && inflightAt <= away) || Date.now() - inflightAt > STUCK_MS)) {
      passive = inflightPassive;     // an authoritative (save) reload that crossed the freeze is replaced by an authoritative one
      loadGen++; inflight = false;     // the superseded read's own finally is skipped, so the flag is dropped here
    }
    load({ passive: passive }).then(function (ok) { if (ok && storeEl && !inflight) storeEl.removeAttribute("data-cold"); });   // only a read that landed: a failed one leaves the placeholder
  });

  // visibilitychange is a PASSIVE re-read → load({passive:true}),
  // which defers (and reports) while an edit is open. The substrate only changes on a
  // wrap, so we don't poll; visibilitychange catches "something moved while you were
  // away" — but never at the cost of the operator's in-progress text.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load({ passive: true });
  });
  // the first read settles (drawn, refused or failed): the cold-start placeholder under #store (data-cold, dashboard.css) goes
  load().finally(function () { if (storeEl) storeEl.removeAttribute("data-cold"); });
})();

// The Room and Desk full-tab views (room_view.js, desk_tab_view.js): each owns the whole tab while its body class is on, mounts only with
// a write token, and leaves on any other places tab. (The Build tab's Threads zone that once shared this boot went with the Modes bar.)
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";
  let roomSection = null, roomHost = null, roomView = null;     // RM-G2: the cockpit's one Claude room
  let deskSection = null, deskHost = null, deskView = null;     // the Desk view (levain 33), a second full tab beside the Room

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function token() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }

  function build() {
    const main = document.querySelector("main.frame");
    buildRoom(main);
    buildDesk(main);
  }

  // The Room tab (1106-51; Phill, signed row 273: "full-tab Room"): the room owns the whole tab. body.rv-on (dashboard.css) hides
  // the rest of the page but the app's own header and views strip; the Room mounts (room_view.js over GET /cockpit/room.json and POST
  // /cockpit/room/*) only while that is on, on a visible page, with a write token. Any other views tab or mode tab leaves the Room.
  // A server with no room answers its read an empty room and the page says so; a server that mounts no room routes answers 404 and
  // the view says it could not read.
  function buildRoom(main) {
    if (!window.LevainRoomView) return;
    roomSection = el("section", "rv-zone");
    roomSection.dataset.zone = "room";
    roomSection.hidden = true;
    roomHost = el("div", "rv-body-host");
    roomSection.appendChild(roomHost);
    if (main && main.parentNode) main.parentNode.insertBefore(roomSection, main.nextSibling); else document.body.appendChild(roomSection);
  }
  // The Desk view (levain 33): a second full tab beside the Room, the same mechanism. body.dv-on (dashboard.css) hides the rest of
  // the page but the app's own header and views strip; desk_tab_view.js mounts over GET /cockpit/desk.json and POST
  // /cockpit/desk/send only while that is on and with a write token. It is NOT stopped when the page is hidden: it reads slower
  // then (the view's own 12 s), so a reply that arrives while the phone is away is waiting. Entering one view leaves the other.
  function buildDesk(main) {
    if (!window.LevainDeskView) return;
    deskSection = el("section", "dv-zone");
    deskSection.dataset.zone = "desk";
    deskSection.hidden = true;
    deskHost = el("div", "dv-body-host");
    deskSection.appendChild(deskHost);
    if (main && main.parentNode) main.parentNode.insertBefore(deskSection, main.nextSibling); else document.body.appendChild(deskSection);
  }
  function roomOn() { return document.body.classList.contains("rv-on"); }
  function deskOn() { return document.body.classList.contains("dv-on"); }
  function paintViewTabs() {
    [["room", roomOn()], ["desk", deskOn()]].forEach((p) => {
      document.querySelectorAll('[data-phone-jump="' + p[0] + '"]').forEach((b) => { if (p[1]) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    });
  }
  function setRoom(on) {
    if (on === roomOn()) return;
    document.body.classList.toggle("rv-on", on);
    if (on) document.body.classList.remove("dv-on");
    paintViewTabs();
    sync();
  }
  function setDesk(on) {
    if (on === deskOn()) return;
    document.body.classList.toggle("dv-on", on);
    if (on) document.body.classList.remove("rv-on");
    paintViewTabs();
    sync();
  }

  function sync() {
    if (roomSection && roomOn() && !token()) { setRoom(false); return; }   // the token went: the protected conversation leaves the page with it
    if (deskSection && deskOn() && !token()) { setDesk(false); return; }   // the token went: the protected view leaves the page with it
    const wantRoom = !!roomSection && !!token() && roomOn() && document.visibilityState !== "hidden";
    const wantDesk = !!deskSection && !!token() && deskOn();
    if (roomSection) roomSection.hidden = !roomOn();
    if (deskSection) deskSection.hidden = !deskOn();
    // Every stop runs before any mount: the Room and the Desk both write the viewport variables (--rv-vvh, --rv-vvt) the page's
    // keyboard fit reads, and a view stopped AFTER the other mounted would clear the ones the other had just set.
    if (roomSection && !wantRoom && roomView) {
      roomView.stop();
      roomView = null;
    }
    if (deskSection && !wantDesk && deskView) {
      deskView.stop();
      deskView = null;
    }
    if (roomSection && wantRoom && !roomView) {
      roomHost.replaceChildren();
      const T = window.LevainCockpitTransport;
      roomView = window.LevainRoomView.mount(roomHost, { headers: () => (T ? T.headers() : {}), probe: false });
    }
    if (deskSection && wantDesk && !deskView) {
      deskHost.replaceChildren();
      const T = window.LevainCockpitTransport;
      deskView = window.LevainDeskView.mount(deskHost, { headers: () => (T ? T.headers() : {}), probe: false });
    }
  }

  function start() {
    if (!document.querySelector("main.frame")) return;
    build();
    let lastMode = document.body.dataset.mode;
    new MutationObserver(() => { if (document.body.dataset.mode !== lastMode) { lastMode = document.body.dataset.mode; setRoom(false); setDesk(false); } sync(); }).observe(document.body, { attributes: true, attributeFilter: ["data-mode"] });
    // the Room tab enters the Room (captured, so the page's own jump never also scrolls to it); every other views tab and every mode tab leaves it
    document.addEventListener("click", (ev) => {
      const t = ev.target && ev.target.closest ? ev.target : null;
      const jump = t && t.closest("[data-phone-jump]");
      if (jump && jump.dataset.phoneJump === "room") {
        if (!roomSection || !token()) return;
        ev.stopPropagation();
        setRoom(true);
      } else if (jump && jump.dataset.phoneJump === "desk") {
        if (!deskSection || !token()) return;
        ev.stopPropagation();
        setDesk(true);
      } else if (jump && (jump.dataset.phoneJump === "grants" || jump.dataset.phoneJump === "notifications")) {
        // these two only open a dialog over the page, so the view the person is in stays as it is
      } else if (jump || (t && t.closest("[data-mode-tab]"))) { setRoom(false); setDesk(false); }
    }, true);
    // a mode shortcut (1-4) that re-selects the mode already showing changes no attribute, so it is caught here: it leaves the Room too.
    // A dialog's keys are its own: with one open the view stays.
    document.addEventListener("keydown", (ev) => {
      if (!(roomOn() || deskOn()) || ev.metaKey || ev.ctrlKey || ev.altKey || !/^[1-4]$/.test(ev.key)) return;
      const sheet = document.getElementById("pr-sheet");
      if (document.querySelector(".modal-overlay") || (sheet && sheet.getClientRects().length)) return;
      const a = document.activeElement, tag = a && a.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (a && a.isContentEditable)) return;
      setRoom(false);
      setDesk(false);
    }, true);
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("storage", (e) => { if (e.key === WRITE_TOKEN_KEY) sync(); });
    window.addEventListener("levain:write-token-change", sync);   // the same page's own set / remove (dashboard_boot's token handling)
    sync();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
