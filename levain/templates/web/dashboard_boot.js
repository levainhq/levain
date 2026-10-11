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

  // The write token (spore-129, then K2a). A writable server REQUIRES the X-Levain-Write-Token
  // header on every write, on loopback too (K2a); off-box it also gates the reads (spore-220).
  // The server signals it via `view.write_token_required` (set in load()); we hold the token in
  // localStorage and attach it. The server prints the token at start, with a link carrying it
  // in the fragment, which the page takes on load. (This localStorage entry is the
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
      if (/^[\x21-\x7e]+$/.test(tok)) window.localStorage.setItem(WRITE_TOKEN_KEY, tok);
      else console.warn("levain: the write token in the link is not a header-safe string; not stored");
    } catch (_) { /* no history or storage: the prompt still works */ }
  })();

  // The device-held off-box token from localStorage ("" if none / private mode).
  function storedToken() {
    try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; }
  }
  // Prompt for + persist the write token; returns the entered token ("" if cancelled). The
  // desktop-browser STOPGAP entry point (flowConnect's native shell injects the same header from
  // the keychain). One token gates BOTH the reads (spore-220) and the writes (spore-129).
  function promptForToken() {
    const tok = (window.prompt("Write token (printed when this server started: on its startup line, or in the daemon's log):") || "").trim();
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

  // DATA SOURCE (cockpit design §7). `/?source=manifest` draws the page from the kernel's cockpit
  // manifest + panel routes (cockpit_view.js maps them onto the same components); the default stays
  // /substrate.json until the flip is ruled. The manifest source writes through /cockpit/verb (postVerb
  // below). The flip is this one constant.
  const useManifest = new URLSearchParams(window.location.search).get("source") === "manifest";

  // ---- MANIFEST WRITES (R1): every affordance on the manifest source posts /cockpit/verb ----
  //
  // The page decides no tier. A gesture that may escalate asks the kernel first (a dry run), shows the
  // tier it would take, and fires only on the operator's second click, with exactly the request it showed.
  // `live` is the write surface of the view last RENDERED: a write names the row version the operator saw,
  // so a re-read never moves it under an open form (the kernel's `stale` answer is the backstop).
  let live = null;
  let frozen = null;        // the previewed request: {fp0, tier, gesture, intent}
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

  const NETWORK_MESSAGE = "no answer: the request may or may not have landed. Click again to send the same request (it carries the same key, so it lands once).";
  // answers after which the page's picture of the row or of the install is stale or unavailable
  const REREAD = new Set(["needs_intent", "stale", "tier_moved", "gesture_moved", "needs_broker", "standby", "not_built"]);

  function fault(r, what) {
    const d = r.data || {};
    const code = d.error || "HTTP " + r.status;
    const text = d.message || code;
    if (REREAD.has(code) || r.status === 501) {
      frozen = null;
      notice = { key: what.row_id || null, text: what.label + ": " + text + " (re-read)", cls: "err" };
      load({ passive: true });   // a re-read that never discards an open form: it defers while one is dirty
      return { ok: false, error: code, message: text + " Re-read the page; review and click again." };
    }
    return { ok: false, error: code, message: text };
  }

  function previewText(head, action, tier, gesture, base) {
    const how = gesture === "sign" ? "waits for a signature" : "fires now, recorded as reported (claims cockpit token)";
    const from = action.tier && action.tier !== tier ? " (rises from " + action.tier + ")" : "";
    const cut = (v) => { const t = typeof v === "string" ? v : JSON.stringify(v); return JSON.stringify(t.length > 60 ? t.slice(0, 57) + "..." : t); };
    const vals = Object.keys(base.params).map((k) => k + " = " + cut(base.params[k])).join(", ") || "no values";
    return (head.label || base.verb) + " is " + tier + from + ": " + how + ". " + (head.effect ? head.effect + ". " : "")
      + "Sending " + vals + ". Click again to fire.";
  }

  async function postVerb(action, params, opts) {
    opts = opts || {};
    const head = (live && live.heads && live.heads[action.verb]) || {};
    const label = head.label || action.verb;
    if (action.gesture === "refused" || action.gesture === "laptop-only") {
      return { ok: false, error: "gesture", message: label + " is " + action.gesture + " here: it cannot be done from this page." };
    }
    const t = action.target || {};
    const base = { verb: action.verb, panel_id: t.panel_id, params: params || {} };
    if (typeof t.row_id === "string") { base.row_id = t.row_id; base.row_version = t.row_version; }
    const pv = opts.panelVersion !== undefined && opts.panelVersion !== null ? opts.panelVersion : t.panel_version;
    if (typeof pv === "string") base.panel_version = pv;
    if (head.confirm === true && opts.confirm === true) base.confirm = true;
    const what = { row_id: base.row_id, label: label };
    const fp0 = canon(base);

    let tier = "C1", gesture = action.gesture, intent = null;
    if (frozen && frozen.fp0 === fp0) {
      tier = frozen.tier; gesture = frozen.gesture; intent = frozen.intent;
    } else {
      frozen = null;
      const mayEscalate = !(action.gesture === "direct" && (!Array.isArray(action.escalates) || action.escalates.length === 0));
      if (mayEscalate) {
        const d = await send("/cockpit/verb", Object.assign({}, base, { dry_run: true }));
        if (d.net) return { ok: false, error: "network", message: NETWORK_MESSAGE };
        if (!d.ok) return fault(d, what);
        tier = d.data.tier; gesture = d.data.gesture;
        if (gesture === "refused" || gesture === "laptop-only") {
          return { ok: false, error: "gesture", message: label + " is " + tier + " with these values, and its gesture is " + gesture + ": it cannot be done from this page." };
        }
        if (tier !== "C1") {
          intent = gesture === "sign" ? "sign" : "reported";
          frozen = { fp0: fp0, tier: tier, gesture: gesture, intent: intent };
          return { ok: false, preview: true, error: "preview", message: previewText(head, action, tier, gesture, base) };
        }
      }
    }

    let env = Object.assign({}, base);
    if (intent) { env.intent = intent; env.tier_seen = tier; env.gesture_seen = gesture; }
    // A T2+ fire (or an idempotent verb's) carries one key, minted for this frozen request and kept with
    // the whole request until the kernel answers, so a lost answer is replayed, never re-decided.
    let fpKey = null;
    if (tier !== "C1" || head.idempotent === true) {
      fpKey = canon([base.verb, base.panel_id, base.row_id || null, base.row_version || base.panel_version || null,
                     base.params, intent, base.confirm === true]);
      const keys = readKeys();
      if (keys[fpKey] && keys[fpKey].envelope) env = keys[fpKey].envelope;
      else { env.idempotency_key = newKey(); keys[fpKey] = { envelope: env }; writeKeys(keys); }
    }
    const r = await send("/cockpit/verb", env);
    if (r.net) return { ok: false, error: "network", message: NETWORK_MESSAGE };   // key and preview stay
    frozen = null;
    if (fpKey) { const keys = readKeys(); delete keys[fpKey]; writeKeys(keys); }
    if (!r.ok) return fault(r, what);
    const d = r.data;
    const state = typeof d.state === "string" && d.state ? d.state : "done";
    notice = { key: base.row_id || null, text: label + ": " + state + (d.label ? " · " + d.label : ""), cls: state === "COMMITTED" || state === "done" ? "note" : "preview" };
    await afterWrite();
    return { ok: true, data: d };
  }

  // reload after a landed write, deferring while an async job polls (as postWrite does)
  async function afterWrite() {
    if (window.LevainDashboard && typeof window.LevainDashboard.jobsActive === "function" && window.LevainDashboard.jobsActive()) {
      deferredReload = true;
    } else {
      await load();
    }
  }

  // The legacy request kinds the render core still speaks, translated onto cockpit verbs. Only the verb
  // name, the params and the target are read from the request; everything else the kernel decides.
  const ROW_KEY = { spore_id: "spore:", episode_id: "episode:", edit_id: "edit:" };
  const NOT_PARAMS = new Set(["kind", "spore_id", "episode_id", "edit_id", "expect_disposition", "confirm",
    "panel_version", "source", "heading", "expected_body"]);
  const VERB_OF_KIND = { state: "section_edit" };

  function panelActionFor(verb, req) {
    // a capture lands in the Keep when it is a note, else in the Tray
    const want = verb === "spore_seed" ? (req && req.disposition === "note" ? "keep" : "tray") : null;
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

  // a downstream verb's compose box: found by verb name on the panels that offer it
  async function manifestAction(verb, params, confirm) {
    if (!live) return { ok: false, error: "not_ready", message: "the page has not finished reading" };
    const action = panelActionFor(verb, null);
    if (!action) return { ok: false, error: "not_offered", message: "the cockpit did not offer " + verb + " here" };
    return postVerb(action, params, { confirm: confirm === true });
  }

  // cancel a waiting pending: the gesture's intent, no preview, and a key of its own
  async function cancelPending(c) {
    const body = { idempotency_key: newKey() };
    if (c.tier && c.tier !== "C1") body.intent = c.gesture === "sign" ? "sign" : "reported";
    const route = c.route || "/cockpit/pending/" + encodeURIComponent(c.pending_id) + "/cancel";
    const r = await send(route, body);
    if (r.net) return { ok: false, error: "network", message: NETWORK_MESSAGE };
    if (!r.ok) return fault(r, { row_id: null, label: "cancel" });
    const d = r.data;
    notice = { key: null, text: "cancel: " + (d.state || "done") + (d.label ? " · " + d.label : ""), cls: "note" };
    await afterWrite();
    return { ok: true, data: d };
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

  // Any edit to a form discards the preview: the operator fires only what they were shown.
  function discardPreview(ev) {
    if (!frozen) return;
    frozen = null;
    const t = ev && ev.target && ev.target.closest ? ev.target.closest(".verb-form, .tray-dump, .edit-controls, .editor, .state-editor") : null;
    const m = t ? t.querySelector(".edit-msg.preview") : null;
    if (m) { m.className = "edit-msg"; m.textContent = ""; }
  }
  if (useManifest) {
    document.addEventListener("input", discardPreview, true);
    document.addEventListener("change", discardPreview, true);
  }

  async function loadManifestView(passive) {
    const manifest = await authedReadJson("/cockpit/manifest.json");   // settles the token: panels do not re-prompt
    const fetchedAt = Date.now();
    const ids = Object.keys(manifest.panels);
    const panels = Object.create(null);
    // One shared re-auth for the batch: if the token rotated after the manifest read, every panel 403s
    // together, so the first one prompts (or drops the dead token) and the rest wait on that same answer.
    let reauth = null;
    const fetchPanel = (url) => fetch(url, { cache: "no-store", headers: readHeaders() });
    await Promise.all(ids.map(async (id) => {
      const url = "/cockpit/panel/" + encodeURIComponent(id) + ".json?profile=full";
      try {
        let res = await fetchPanel(url);
        if (res.status === 403 && await isTokenReject(res.clone())) {
          // a passive refresh never steals focus with a prompt over an open editor (as the legacy path does)
          reauth = reauth || Promise.resolve().then(() => {
            if (passive && editInProgress()) return false;
            if (promptForToken()) return true;
            dropToken();
            return false;
          });
          if (await reauth) {
            const usedToken = storedToken();
            res = await fetchPanel(url);
            // the replacement token was rejected too: drop it so the next refresh asks again
            if (res.status === 403 && await isTokenReject(res.clone()) && storedToken() === usedToken) dropToken();   // not a newer token another tab stored
          }
        }
        if (!res.ok) throw new Error("HTTP " + res.status);
        panels[id] = await res.json();
      } catch (e) {
        // keep the reason: the mapper draws this panel as unavailable with it, never as an empty one
        panels[id] = { error: e && e.message ? e.message : String(e) };
      }
    }));
    return window.LevainCockpitView.fromManifest({ manifest: manifest, panels: panels }, { elapsedMs: Date.now() - fetchedAt });
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
      if (useManifest) {
        const mview = await loadManifestView(passive);
        if (passive && editInProgress()) { notice = null; status("editing — refresh deferred"); return; }
        writeTokenRequired = mview.write_token_required === true;
        frozen = null;   // a rebuilt board shows no preview, so none may be fired
        window.LevainDashboard.render(mview, mview.writable
          ? { commit: manifestCommit, commitAction: manifestAction, cancelPending, onJobsIdle, recall } : { recall });
        live = { rows: mview.targets.rows, panels: mview.targets.panels, heads: mview.verb_heads };
        status("read " + new Date().toLocaleTimeString());
        showNotice();
        return;
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
