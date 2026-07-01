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
  const btn = document.getElementById("refresh");

  // Report transient status WITHOUT clobbering the store-path subtitle (#store):
  // a momentary fetch blip shouldn't erase the identity of what we're viewing.
  function status(msg) {
    if (stampEl) stampEl.textContent = msg;
  }

  let inflight = false;
  // A non-passive (save / boot) reload is AUTHORITATIVE — it must eventually render. If one
  // lands while a load is in flight, the shared `inflight` guard would drop it; latch it
  // here and re-fire when the current load settles, so a save's reload can't be swallowed by
  // an overlapping passive read (which would leave the editor wedged open). [codex L3 round-2]
  let pendingReload = false;
  // A board reload a write DEFERRED because an async job was polling (a reload rebuilds the board +
  // would detach the live consult result box; L3 codex MED). Flushed by onJobsIdle() when the last
  // job finishes — so the sibling write's saved state still lands, just after the job.
  let deferredReload = false;

  // OFF-BOX write token (spore-129). When the substrate is served off-loopback (a Tailscale
  // mesh write surface) the server REQUIRES the X-Levain-Write-Token header on /edit — the
  // factor that replaces loopback-is-auth once the surface leaves the machine. The server
  // signals it via `view.write_token_required` (set in load()); we hold the device's token in
  // localStorage and attach it. On a loopback (default) surface this stays false and the
  // localhost-sovereign token-free path is unchanged. (This localStorage entry is the
  // desktop-browser STOPGAP; flowConnect's native shell later holds the token in the keychain
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
  let writeTokenRequired = false;

  // The device-held off-box token from localStorage ("" if none / private mode).
  function storedToken() {
    try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; }
  }
  // Prompt for + persist the off-box token; returns the entered token ("" if cancelled). The
  // desktop-browser STOPGAP entry point (flowConnect's native shell injects the same header from
  // the keychain). One token gates BOTH the reads (spore-220) and the writes (spore-129).
  function promptForToken() {
    const tok = (window.prompt("Off-box token (shown on the bridge --write startup line):") || "").trim();
    if (tok) { try { window.localStorage.setItem(WRITE_TOKEN_KEY, tok); } catch (_) { /* ignore */ } }
    return tok;
  }
  // Drop a stored token so the next attempt re-prompts (a mistyped / rotated token).
  function dropToken() {
    try { window.localStorage.removeItem(WRITE_TOKEN_KEY); } catch (_) { /* ignore */ }
  }
  // Headers for a READ (GET): attach the stored off-box token OPPORTUNISTICALLY if we hold one.
  // Harmless on a loopback / read-only surface (the server skips the read gate there); REQUIRED on
  // an off-box writable surface (spore-220 gates the substrate reads). No Content-Type — GET has no body.
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
  // message ("missing or invalid write token") and is DUPLICATED in flow's fleetview_web.py — tighten
  // the regex in one place and you must mirror it in the other repo, or the prompt/drop silently drifts.
  async function isTokenReject(res) {
    try { const d = await res.json(); return /token/i.test((d && d.message) || ""); }
    catch (_) { return false; }
  }

  // The write transport for Slice-2a governed edits. POSTs the edit to /edit with a
  // same-origin fetch (browser sends Sec-Fetch-Site: same-origin + we set
  // application/json — exactly what the server's write/auth boundary requires; off-box it
  // also carries the write token via writeHeaders). On success it reloads so the dashboard
  // reflects the saved state; on refusal it returns {ok:false, error, message} so the render
  // core can surface it inline.
  async function postWrite(route, body) {
    try {
      const res = await fetch(route, {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await res.json(); } catch (_) { /* tolerate a non-JSON body */ }
      if (res.ok) {
        // Defer the reload while an async job is polling — rebuilding the board would detach the
        // live consult result box (L3 codex MED). onJobsIdle() flushes it when the job ends.
        if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function"
            && window.LevainDashboard.jobsActive()) {
          deferredReload = true;
        } else {
          await load();
        }
        return { ok: true, data: data };
      }
      // A token rejection (off-box only): drop the stored token so the next attempt re-prompts
      // — covers a mistyped/rotated token without wedging every future write.
      if (res.status === 403 && writeTokenRequired && /token/i.test(data.message || "")) dropToken();
      return {
        ok: false,
        error: data.error || "HTTP " + res.status,
        message: data.message || "HTTP " + res.status,
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
  async function authedReadJson(url) {
    let res = await fetch(url, { cache: "no-store", headers: readHeaders() });
    if (res.status === 403 && await isTokenReject(res)) {
      if (promptForToken()) res = await fetch(url, { cache: "no-store", headers: readHeaders() });
      if (res.status === 403) { dropToken(); throw new Error("off-box token missing or invalid"); }
    }
    if (!res.ok) {
      let m = "HTTP " + res.status;
      try { const d = await res.json(); m = d.message || d.error || m; } catch (_) { /* ignore */ }
      throw new Error(m);
    }
    return res.json();
  }

  // Poll an async job's status → { status, result?, error? }. Rides authedReadJson (spore-220 gates
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
  function onJobsIdle() {
    if (deferredReload) { deferredReload = false; load({ passive: true }); }
  }

  // An involuntary re-read rebuilds the whole board (+ modal) via replaceChildren and
  // would discard an in-progress edit/capture textarea (spore-108). The PASSIVE triggers
  // (refresh-button, visibilitychange) defer while the operator is mid-edit — never the
  // save's own commit→load reload (passive:false), which MUST rebuild to close the editor
  // and show saved state. Deferring loses nothing: the substrate only changes on a wrap,
  // so the next visibility flip / the save's reload re-reads.
  //
  // Fails CLOSED (block) when the render core or its predicate is absent — uniform with
  // core.js's missing-data-orig→dirty bias (complement+kimi L3). Only the passive triggers
  // consult this, so a false "editing" merely defers a harmless re-read; it never blocks
  // save or the initial boot (both passive:false).
  function editInProgress() {
    if (!window.LevainDashboard || typeof window.LevainDashboard.hasUnsavedEdit !== "function") return true;
    return window.LevainDashboard.hasUnsavedEdit();
  }

  async function load(opts) {
    const passive = !!(opts && opts.passive);
    if (inflight) {
      // A passive read is droppable (next trigger re-reads); a non-passive save/boot
      // reload must NOT be — latch it to re-run when the in-flight load settles.
      if (!passive) pendingReload = true;
      return;
    }
    // If the render core didn't load (reorder / serve failure), say so plainly
    // rather than throwing an opaque TypeError after a successful fetch.
    if (!window.LevainDashboard || typeof window.LevainDashboard.render !== "function") {
      status("render core failed to load");
      return;
    }
    // Pre-fetch fast path: don't even fetch if we already know an edit is open.
    if (passive && editInProgress()) { status("editing — refresh deferred"); return; }
    inflight = true;
    if (btn) btn.disabled = true;
    try {
      let res = await fetch("/substrate.json", { cache: "no-store", headers: readHeaders() });
      // OFF-BOX BOOTSTRAP (spore-220): an off-box writable surface gates the substrate READS. On the
      // first load we hold no token yet (and don't yet know one is required — that flag rides INSIDE
      // substrate.json, the chicken-and-egg). A token-class 403 means "off-box surface, token needed":
      // prompt once, store, retry. A read-only mesh surface never 403s here (its reads are token-free),
      // so it never prompts — iPad VIEWING stays open. The token, once entered, also unlocks writes.
      if (res.status === 403 && await isTokenReject(res)) {
        // Don't pop a BLOCKING prompt over an edit the operator opened DURING this fetch on a PASSIVE
        // re-read (visibilitychange / refresh): re-check here, mirroring the post-fetch render guard
        // below, so a background token-403 can't steal focus from in-progress text (complement L3
        // FIND-1 / the spore-108 "passive never disrupts an edit" invariant). The next explicit
        // boot/save load (passive:false) re-prompts.
        if (passive && editInProgress()) { status("editing — refresh deferred"); return; }
        if (promptForToken()) res = await fetch("/substrate.json", { cache: "no-store", headers: readHeaders() });
        if (res.status === 403) {
          // Still refused after the retry (wrong/rotated token, or the operator cancelled): drop the
          // stored token so the next explicit refresh re-prompts, and surface it plainly. Scoped to
          // the token-reject branch: a non-token 403 (Host/CSRF — plain-text body) never reaches here,
          // so it falls through to the generic read-failed path below with the stored token intact.
          dropToken();
          status("read refused — off-box token missing or invalid");
          return;
        }
      }
      if (!res.ok) throw new Error("HTTP " + res.status);
      const view = await res.json();
      // Post-fetch race re-check (codex L3 HIGH): a passive load that STARTED clean, then
      // the operator opened/typed an editor WHILE this fetch was in flight — rebuilding
      // now would still discard it. The save path (passive:false) always applies.
      if (passive && editInProgress()) { status("editing — refresh deferred"); return; }
      // Track whether THIS surface demands the off-box write token (spore-129) so commit()'s
      // writeHeaders attaches it. False on a loopback/read-only surface (the default).
      writeTokenRequired = view.write_token_required === true;
      // Gate the write transport on the substrate being writable (NO THEATER): a
      // read-only source (no install root → POST /edit 422s) renders with no edit
      // affordances at all, matching the server. An older payload without
      // `writable` defaults to writable, so existing installs are unchanged.
      // `recall` (episode search) is a READ — injected in BOTH branches so search works on a
      // read-only surface too; the write transports stay gated on `writable` (NO THEATER).
      window.LevainDashboard.render(view, view.writable === false ? { recall } : { commit, commitAction, commitJob, pollJob, onJobsIdle, recall });
      status("read " + new Date().toLocaleTimeString());
    } catch (e) {
      status("read failed: " + (e && e.message ? e.message : e));
    } finally {
      inflight = false;
      if (btn) btn.disabled = false;
      // Run a reload that arrived (authoritative, non-passive) while we were in flight.
      // Always non-passive → never deferred, so a saved edit's state always lands.
      if (pendingReload) { pendingReload = false; load(); }
    }
  }

  // The refresh button + visibilitychange are PASSIVE re-reads → load({passive:true}),
  // which defers (and reports) while an edit is open. The substrate only changes on a
  // wrap, so we don't poll; visibilitychange catches "something moved while you were
  // away" — but never at the cost of the operator's in-progress text.
  if (btn) btn.addEventListener("click", function () { load({ passive: true }); });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load({ passive: true });
  });
  load();
})();
