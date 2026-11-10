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
  // A foreground (pwa_resume.js) after a long freeze: iOS kills the sockets of a frozen page, so a read that was in flight may
  // never settle, and `inflight` would then drop every later read. A read older than STUCK_MS is superseded: its generation is
  // dead, its late result is discarded, and the next read starts.
  let loadGen = 0, inflightAt = 0, inflightPassive = true;
  const STUCK_MS = 8000;
  // A FAILED first read (a radio still waking on foreground, a proxy hiccup) used to leave the cold "connecting…" line and an empty
  // board with nothing to try again until the next foreground: Phill 11-06 saw both phones sit there. Until a board has been drawn
  // once, a failure says so in the line and re-reads on a short backoff (and when the network returns); a refusal (token) is not retried.
  let everDrawn = false, retryN = 0, retryT = 0, refusedStop = false;
  // a refusal travels as a typed property, never as message text (a connection-refused network error must still retry)
  function refusal(msg) { const e = new Error(msg); e.refused = true; return e; }
  const RETRY_MS = [2000, 4000, 8000, 15000, 30000];
  function drawnOnce() { if (everDrawn === false && storeEl && /^(can't reach the cockpit|the cockpit refused)/.test(storeEl.textContent || "")) storeEl.textContent = ""; everDrawn = true; refusedStop = false; retryN = 0; clearTimeout(retryT); retryT = 0; }
  function retryUntilDrawn(why, refused) {
    if (everDrawn) return;
    // a refusal (the token the page holds is missing or wrong) is not a network blip: a retry would only ask again, and each ask can
    // put up the token prompt. Say it and stop; the next foreground or the Refresh button reads again.
    if (refused) {
      refusedStop = true;
      if (storeEl) { storeEl.hidden = false; storeEl.textContent = "the cockpit refused this device (" + why + "); pair it again or reload"; }
      return;
    }
    if (storeEl) { storeEl.hidden = false; storeEl.textContent = "can't reach the cockpit yet (" + why + "), trying again…"; }
    if (retryT) return;
    // a read already in flight (a foreground started one) would drop this retry and end the chain: wait and ask again instead
    retryT = setTimeout(function () {
      retryT = 0;
      if (everDrawn || refusedStop) return;
      if (inflight) { retryUntilDrawn(why); return; }
      load({ passive: true, auto: true });
    }, RETRY_MS[Math.min(retryN++, RETRY_MS.length - 1)]);
  }
  // (the node harnesses that load this file hand it a window with no event API)
  if (typeof window.addEventListener === "function") window.addEventListener("online", function () { if (!everDrawn && !refusedStop && !inflight) { clearTimeout(retryT); retryT = 0; load({ passive: true, auto: true }); } });
  // A non-passive (save / boot) reload is AUTHORITATIVE — it must eventually render. If one
  // lands while a load is in flight, the shared `inflight` guard would drop it; latch it
  // here and re-fire when the current load settles, so a save's reload can't be swallowed by
  // an overlapping passive read (which would leave the editor wedged open). [codex L3 round-2]
  let pendingReload = false;
  // A board reload a write DEFERRED because an async job was polling (a reload rebuilds the board +
  // would detach the live consult result box; L3 codex MED). Flushed by onJobsIdle() when the last
  // job finishes — so the sibling write's saved state still lands, just after the job.
  let deferredReload = false;

  // The write token (spore-129, then K2a). A writable server REQUIRES the X-Levain-Write-Token
  // header on every write, on loopback too (K2a); off-box it also gates the reads (spore-220).
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
  // for the zones that follow the token (the Threads zone); other tabs still arrive through `storage`.
  const TOKEN_CHANGE_EVENT = "levain:write-token-change";
  function announceTokenChange() { try { window.dispatchEvent(new Event(TOKEN_CHANGE_EVENT)); } catch (_) { /* no events */ } }
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
  // The first board is drawn as its panels land, never held for the whole set: the manifest, then the panels the page's
  // top (the hero, the brief, the Needs strip's `decisions`) is made of, then the rest as each one answers. These are the
  // pure parts: which ids are read first, the manifest narrowed to the panels in hand, and the pacing of the partial draws.
  const PARTIAL_GAP_MS = 250;        // two partial draws are at least this far apart, and at least PARTIAL_IDLE x the last draw's own time (a slow phone renders slowly)
  const PARTIAL_IDLE = 4;
  const PANEL_TIMEOUT_MS = 30000;    // a panel that has not answered by then is drawn as its own failure, the rest of the board stays usable
  function drawOrder(manifest, ids) {
    const r = (manifest && manifest.regions) || {};
    const core = [];
    for (const id of [].concat(r.hero || [], r.brief || [], ["decisions"])) if (ids.indexOf(id) >= 0 && core.indexOf(id) < 0) core.push(id);
    return { core: core, order: core.concat(ids.filter((id) => core.indexOf(id) < 0)) };
  }
  function manifestSubset(manifest, have) {
    const keep = (a) => Array.isArray(a) ? a.filter((id) => have.has(id)) : a;
    const m = Object.assign({}, manifest);
    m.panels = {};
    for (const id of Object.keys(manifest.panels || {})) if (have.has(id)) m.panels[id] = manifest.panels[id];
    if (manifest.regions && typeof manifest.regions === "object") {
      const r = Object.assign({}, manifest.regions);
      for (const k of ["hero", "brief", "widgets", "header"]) if (Array.isArray(r[k])) r[k] = keep(r[k]);
      if (Array.isArray(r.zones)) {
        r.zones = r.zones.map((z) => {
          const zz = Object.assign({}, z);
          zz.panels = keep(z.panels);
          if (Array.isArray(z.cards)) zz.cards = z.cards.map((c) => Object.assign({}, c, { panels: keep(c.panels) })).filter((c) => c.panels.length);
          return zz;
        }).filter((z) => z.panels.length);
      }
      m.regions = r;
    }
    return m;
  }
  // draw(settledCount) is called when the core panels are all in, then after each later panel at most once per PARTIAL_GAP_MS,
  // and never for the last panel (the full draw is the caller's). `stop` cancels a pending one.
  function partialPacer(core, total, draw, clock) {
    const waiting = new Set(core);
    let n = 0, last = -Infinity, gap = PARTIAL_GAP_MS, timer = null, done = false;
    const fire = () => {
      timer = null; if (done) return;
      const t0 = clock.now();
      draw(n);
      last = clock.now();
      gap = Math.max(PARTIAL_GAP_MS, PARTIAL_IDLE * (last - t0));
    };
    return {
      settled: function (id) {
        n++; waiting.delete(id);
        if (done || waiting.size || n >= total) return;
        if (timer !== null) return;
        const wait = last + gap - clock.now();
        if (wait <= 0) fire(); else timer = clock.setTimeout(fire, wait);
      },
      stop: function () { done = true; if (timer !== null) { clock.clearTimeout(timer); timer = null; } },
    };
  }
  // END progressive draw
  let writeTokenRequired = false;
  // What a missing token is answered with, from the server: "pair" (this request cannot use the owner key: pair the device by a
  // short code) or "owner" (a loopback page: the owner's own token is typed, as before). "" until a 403 body or the manifest says.
  let tokenAuth = "";

  // K2a: `levain serve` opens the page with the write token in the URL FRAGMENT (never sent to the server).
  // Take it, keep it, and strip it from the address bar, leaving any chat_token for the chat panel.
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
        window.localStorage.setItem(WRITE_TOKEN_KEY, tok);
        window.localStorage.removeItem(PAIRED_MARK_KEY);   // the owner's own link: this browser holds the owner's key now
        announceTokenChange();
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
  // the keychain). One token gates BOTH the reads (spore-220) and the writes (spore-129).
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
      // A stale refusal means this page's picture of the row is old (spore-174): re-read it, after the row has shown
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
  async function authedReadJson(url, signal) {
    const usedToken = storedToken();
    let res = await fetch(url, { cache: "no-store", headers: readHeaders(), signal: signal });
    if (res.status === 403 && await isTokenReject(res)) {
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
    const d = window.LevainDashboard;
    if (d && typeof d.editHoldsRefresh === "function") return d.editHoldsRefresh();
    if (!d || typeof d.hasUnsavedEdit !== "function") return true;
    return d.hasUnsavedEdit();
  }
  // A passive read refused for an edit or a field in use stays OWED: the latch is set here, so every caller (the refresh button,
  // the tab coming back, a stale re-read) is flushed by the same release, and a timer checks again once a focus-only hold has aged out.
  let idleT = 0;
  const IDLE_RECHECK_MS = 10250;
  function refuseForEdit(msg) {
    status(msg || "editing — refresh deferred");
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
  // The page never shows the server's own text for a refusal: it can carry a path or an OS error (the one exception is
  // SNOOZE_REFUSALS, below). A code not listed here says only its code.
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
    credential_required: "Saving needs the write token for this page. Open the page from the link that carries it."
  };
  const SNOOZE_REFUSALS = new Set(["pick a time after now", "pick a later day", "pick a day: that date could not be read"]);
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
    if (REREAD.has(code) || r.status === 501) {
      notice = { key: what.row_id || null, text: text, cls: "err" };
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
    const redrew = await afterWrite();
    if (!words && redrew === false && !pendingReload) {   // landed, but the page could not redraw: say so, the silence was for a page that updates
      notice = { key: base.row_id || null, text: "Saved. The page could not refresh; reload to see it.", cls: "note" };
      paintNow(notice);
    }
    return { ok: true, data: d };
  }

  // reload after a landed write, deferring while an async job polls (as postWrite does)
  async function afterWrite() {
    if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function" && window.LevainDashboard.jobsActive()) {
      deferredReload = true;
      return undefined;
    }
    return load();   // false when the read or the render failed
  }

  // The legacy request kinds the render core still speaks, translated onto cockpit verbs. Only the verb
  // name, the params and the target are read from the request; everything else the kernel decides.
  const ROW_KEY = { spore_id: "spore:", episode_id: "episode:", edit_id: "edit:" };
  const NOT_PARAMS = new Set(["kind", "spore_id", "episode_id", "edit_id", "expect_disposition", "confirm",
    "panel_version", "source", "heading", "expected_body"]);
  const VERB_OF_KIND = { state: "section_edit" };

  function panelActionFor(verb, req) {
    // every capture, a note included, is the Tray's create; the disposition rides in params
    // the List's panel keeps its wire id `tray`; its create (spore_seed) and its intake box (list_intake) are both offered there
    const want = verb === "spore_seed" || verb === "list_intake" ? "tray" : null;
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
  // the server offers no Threads zone (make_server(threads=False)): the zone's boot reads this. Both data sources call it.
  function markThreadsOff() {
    if (document.body.dataset.threads === "off") return;
    document.body.dataset.threads = "off";
    window.dispatchEvent(new Event("levain:threads-off"));
  }

  // the zone stays hidden until a read says threads are on (no flash before the manifest lands): both data sources call it
  function markThreadsRead() {
    if (document.body.dataset.threadsRead === "1") return;
    document.body.dataset.threadsRead = "1";
    window.dispatchEvent(new Event("levain:threads-off"));   // the zone's sync listens on this one event
  }

  async function loadManifestView(passive, onPartial) {
    const usedToken = storedToken();      // the credential this read goes out with: a decline drops that one, never a newer one
    let manifest;
    try { manifest = await authedReadJson("/cockpit/manifest.json"); }   // settles the token: panels do not re-prompt
    catch (e) { markThreadsRead(); throw e; }     // a failed read does not say threads are off: only an explicit threads:false hides the zone
    // A writable install that wants a token and read the manifest without one (a loopback read needs none)
    // would draw every action refused. Ask for the token now, then read again as that credential.
    const cred = manifest && manifest.credential;
    if (manifest && manifest.transport && manifest.transport.threads === false) markThreadsOff();
    else markThreadsRead();     // a transport-less manifest is no "off" either (the server 404s every thread write when threads are off)
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
    // One shared re-auth for the batch: if the token rotated after the manifest read, every panel 403s
    // together, so the first one prompts (or drops the dead token) and the rest wait on that same answer.
    let reauth = null;
    const fetchPanel = (url, signal) => fetchPanelConditional(url, panelHeads[url], signal);
    pruneHeldPanels(ids);
    // At most PANEL_READ_CONCURRENCY panel reads are in flight, the rest queue in manifest order: one page
    // bursting ~28 reads at once, or two open pages, would otherwise meet the server's concurrency cap.
    // Every read settles (the worker catches its own failure) and so frees its slot.
    const plan = drawOrder(manifest, ids);
    const queue = plan.order.slice();
    // a cold first board draws as its panels land (onPartial); a refresh keeps showing the last board until the new one is whole
    const pacer = onPartial ? partialPacer(plan.core, ids.length, (n) => {
      const have = new Set(Object.keys(panels));
      onPartial(window.LevainCockpitView.fromManifest({ manifest: manifestSubset(manifest, have), panels: Object.assign(Object.create(null), panels) }, { elapsedMs: Date.now() - fetchedAt }), n, ids.length);
    }, { now: Date.now, setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) }) : null;
    const readOne = async (id) => {
      const url = panelUrl(id);
      const ctl = typeof AbortController === "function" ? new AbortController() : null;
      const timer = ctl ? setTimeout(() => ctl.abort(), PANEL_TIMEOUT_MS) : 0;
      try {
        let res = await fetchPanel(url, ctl ? ctl.signal : undefined);
        if (res.status === 403 && await isTokenReject(res.clone())) {
          // a passive refresh never steals focus with a prompt over an open editor (as the legacy path does)
          reauth = reauth || Promise.resolve().then(() => {
            if (passive && editInProgress()) return false;
            if (passive && tokenDeclined) return false;
            if (promptForToken()) return true;
            tokenDeclined = true;
            dropToken();
            return false;
          });
          if (await reauth) {
            const usedToken = storedToken();
            res = await fetchPanel(url, ctl ? ctl.signal : undefined);
            // the replacement token was rejected too: drop it so the next refresh asks again
            if (res.status === 403 && await isTokenReject(res.clone()) && storedToken() === usedToken) dropToken();   // not a newer token another tab stored
          }
        }
        if (!res.ok) throw new Error("HTTP " + res.status);
        panels[id] = await res.json();
      } catch (e) {
        // keep the reason: the mapper draws this panel as unavailable with it, never as an empty one
        panels[id] = { error: ctl && ctl.signal.aborted ? "no answer in " + PANEL_TIMEOUT_MS / 1000 + " s" : (e && e.message ? e.message : String(e)) };
      } finally { if (timer) clearTimeout(timer); }
      if (pacer) pacer.settled(id);
    };
    const worker = async () => { while (queue.length) await readOne(queue.shift()); };
    try { await Promise.all(Array.from({ length: Math.min(PANEL_READ_CONCURRENCY, ids.length) }, worker)); }
    finally { if (pacer) pacer.stop(); }
    // the panels this board was drawn from, each with the head etag it was drawn under: the change stream
    // resyncs over these ids, and the poll fallback compares the etags
    heldNext = Object.fromEntries(ids.map((id) => [id, { etag: (manifest.panels[id] || {}).etag || null }]));
    return window.LevainCockpitView.fromManifest({ manifest: manifest, panels: panels }, { elapsedMs: Date.now() - fetchedAt });
  }

  async function load(opts) {
    const passive = !!(opts && opts.passive);
    // a read the operator or the page's own foreground asks for re-opens the retry chain; only the chain's own timer / online
    // re-reads (auto) stay shut after a refusal
    if (!(opts && opts.auto)) refusedStop = false;
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
      return false;
    }
    // Pre-fetch fast path: don't even fetch if we already know an edit is open.
    if (passive && editInProgress()) return refuseForEdit();
    inflight = true;
    inflightAt = Date.now();
    inflightPassive = passive;
    const myGen = ++loadGen;
    let landed = false;
    if (btn) btn.disabled = true;
    try {
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
        const onPartial = everDrawn ? null : (pview, n, total) => {
          if (myGen !== loadGen || editInProgress()) return;
          try { drawView(pview); } catch (e) { return; }    // a partial board that cannot draw leaves the line as it was; the whole board's draw reports its own failure
          if (storeEl) storeEl.removeAttribute("data-cold");
          drawnOnce();
          status("reading " + n + " of " + total + " panels...");
        };
        const mview = await loadManifestView(passive, onPartial);
        if (myGen !== loadGen) return false;     // superseded while it was stuck: the newer read owns the board
        if (passive && editInProgress()) { notice = null; return refuseForEdit(); }
        drawView(mview);
        if (heldNext) {    // published once drawn: only then can a panel be refetched in place
          window.LevainCockpitHeld = heldNext;
          document.dispatchEvent(new CustomEvent("levain:cockpit-held"));   // the change stream starts, or restarts, on this
        }
        status("read " + new Date().toLocaleTimeString());
        showNotice();
        landed = true; drawnOnce();
        return true;
      }
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
        if (passive && editInProgress()) return refuseForEdit();
        if (promptForToken()) res = await fetch("/substrate.json", { cache: "no-store", headers: readHeaders() });
        if (res.status === 403) {
          // Still refused after the retry (wrong/rotated token, or the operator cancelled): drop the
          // stored token so the next explicit refresh re-prompts, and surface it plainly. Scoped to
          // the token-reject branch: a non-token 403 (Host/CSRF — plain-text body) never reaches here,
          // so it falls through to the generic read-failed path below with the stored token intact.
          dropToken();
          refusedStop = true;
          status("read refused — off-box token missing or invalid");
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
      if (view.threads === false) markThreadsOff(); else markThreadsRead();
      // Gate the write transport on the substrate being writable (NO THEATER): a
      // read-only source (no install root → POST /edit 422s) renders with no edit
      // affordances at all, matching the server. An older payload without
      // `writable` defaults to writable, so existing installs are unchanged.
      // `recall` (episode search) is a READ — injected in BOTH branches so search works on a
      // read-only surface too; the write transports stay gated on `writable` (NO THEATER).
      window.LevainDashboard.render(view, view.writable === false ? { recall } : { commit, commitAction, commitJob, pollJob, onJobsIdle, recall });
      status("read " + new Date().toLocaleTimeString());
      landed = true; drawnOnce();
      return true;
    } catch (e) {
      if (myGen !== loadGen) return false;     // a superseded read's failure says nothing about the page now
      status("read failed: " + (e && e.message ? e.message : e));
      retryUntilDrawn(e && e.message ? e.message : String(e || "read failed"), !!(e && e.refused));
      return false;
    } finally {
     if (myGen === loadGen) {
      inflight = false;
      if (btn) btn.disabled = false;
      // Run a reload that arrived (authoritative, non-passive) while we were in flight.
      // Always non-passive → never deferred, so a saved edit's state always lands.
      if (pendingReload) { pendingReload = false; load(); }
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
      loadGen++; inflight = false;
      if (btn) btn.disabled = false;   // the superseded read's own finally is skipped: the button is freed here, whatever the new read does
    }
    load({ passive: passive }).then(function (ok) { if (ok && storeEl && !inflight) storeEl.removeAttribute("data-cold"); });   // only a read that landed: a failed one leaves the placeholder
  });

  // The refresh button + visibilitychange are PASSIVE re-reads → load({passive:true}),
  // which defers (and reports) while an edit is open. The substrate only changes on a
  // wrap, so we don't poll; visibilitychange catches "something moved while you were
  // away" — but never at the cost of the operator's in-progress text.
  if (btn) btn.addEventListener("click", function () { load({ passive: true }); });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load({ passive: true });
  });
  // the first read settles (drawn, refused or failed): the cold-start placeholder under #store (data-cold, dashboard.css) goes
  load().finally(function () { if (storeEl) storeEl.removeAttribute("data-cold"); });
})();

// CH-U1a: the Build tab's Threads zone. The desk's threads and one thread's stream, read-only, drawn by
// thread_view.js (LevainThreadView) over GET /cockpit/threads.json and /cockpit/thread/<id>.json. Those routes need the
// page's write token on every bind, so the zone stays hidden on a page holding none. It polls only while the Build tab
// is shown and the page is visible; leaving stops the poll, and coming back starts again at the thread list.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";
  let section = null, host = null, view = null;
  let roomSection = null, roomHost = null, roomView = null;     // RM-G2: the cockpit's one Claude room, beside the threads

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function token() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }

  function build() {
    section = el("section", "ck-zone th-zone");
    section.dataset.zone = "threads";
    section.dataset.modes = "build";
    section.hidden = true;
    const head = el("div", "zone-head");
    head.appendChild(el("div", "zone-title", "Threads"));
    head.appendChild(el("span", "zone-gloss", "what each desk thread said, in the order it happened"));
    section.appendChild(head);
    const grid = el("div", "ck-zgrid th-grid");
    const panel = el("div", "panel k-list th-panel");
    panel.dataset.modes = "build";   // dashboard.css hides a zone whose panel names no tab for the current mode
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Desk threads"));
    panel.appendChild(ph);
    host = el("div", "pbody th-body");
    panel.appendChild(host);
    grid.appendChild(panel);
    section.appendChild(grid);
    const main = document.querySelector("main.frame");
    if (main) main.appendChild(section); else document.body.appendChild(section);
    buildRoom(main);
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
  function roomOn() { return document.body.classList.contains("rv-on"); }
  function setRoom(on) {
    if (on === roomOn()) return;
    document.body.classList.toggle("rv-on", on);
    document.querySelectorAll('[data-phone-jump="room"]').forEach((b) => { if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    sync();
  }

  function sync() {
    const off = document.body.dataset.threads === "off";
    const read = document.body.dataset.threadsRead === "1";
    const want = !off && read && !!token() && document.body.dataset.mode === "build" && document.visibilityState !== "hidden";
    section.hidden = off || !read || !token();
    if (want && !view) {
      host.replaceChildren();
      const T = window.LevainCockpitTransport;
      view = window.LevainThreadView.mount(host, { headers: () => (T ? T.headers() : {}), stop: true });
    } else if (!want && view) {
      view.stop();
      view = null;
    }
    if (roomSection) {
      if (roomOn() && !token()) { setRoom(false); return; }   // the token went: the protected conversation leaves the page with it
      const wantRoom = !!token() && roomOn() && document.visibilityState !== "hidden";
      roomSection.hidden = !roomOn();
      if (wantRoom && !roomView) {
        roomHost.replaceChildren();
        const T = window.LevainCockpitTransport;
        roomView = window.LevainRoomView.mount(roomHost, { headers: () => (T ? T.headers() : {}), probe: false });
      } else if (!wantRoom && roomView) {
        roomView.stop();
        roomView = null;
      }
    }
  }

  function start() {
    if (!window.LevainThreadView || !document.querySelector("main.frame")) return;
    build();
    let lastMode = document.body.dataset.mode;
    new MutationObserver(() => { if (document.body.dataset.mode !== lastMode) { lastMode = document.body.dataset.mode; setRoom(false); } sync(); }).observe(document.body, { attributes: true, attributeFilter: ["data-mode"] });
    // the Room tab enters the Room (captured, so the page's own jump never also scrolls to it); every other views tab and every mode tab leaves it
    document.addEventListener("click", (ev) => {
      const t = ev.target && ev.target.closest ? ev.target : null;
      const jump = t && t.closest("[data-phone-jump]");
      if (jump && jump.dataset.phoneJump === "room") {
        if (!roomSection || !token()) return;
        ev.stopPropagation();
        setRoom(true);
      } else if (jump || (t && t.closest("[data-mode-tab]"))) setRoom(false);
    }, true);
    // a mode shortcut (1-4) that re-selects the mode already showing changes no attribute, so it is caught here: it leaves the Room too
    document.addEventListener("keydown", (ev) => {
      if (!roomOn() || ev.metaKey || ev.ctrlKey || ev.altKey || !/^[1-4]$/.test(ev.key)) return;
      const a = document.activeElement, tag = a && a.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (a && a.isContentEditable)) return;
      setRoom(false);
    }, true);
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("storage", (e) => { if (e.key === WRITE_TOKEN_KEY) sync(); });
    window.addEventListener("levain:threads-off", sync);
    window.addEventListener("levain:write-token-change", sync);   // the same page's own set / remove (dashboard_boot's token handling)
    sync();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
