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

  // The write transport for Slice-2a governed edits. POSTs the edit to /edit with a
  // same-origin fetch (browser sends Sec-Fetch-Site: same-origin + we set
  // application/json — exactly what the server's write/auth boundary requires). On
  // success it reloads so the dashboard reflects the saved state; on refusal it
  // returns {ok:false, error, message} so the render core can surface it inline.
  async function commit(request) {
    try {
      const res = await fetch("/edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      let data = {};
      try { data = await res.json(); } catch (_) { /* tolerate a non-JSON body */ }
      if (res.ok) { await load(); return { ok: true }; }
      return {
        ok: false,
        error: data.error || "HTTP " + res.status,
        message: data.message || "HTTP " + res.status,
      };
    } catch (e) {
      return { ok: false, error: "network", message: e && e.message ? e.message : String(e) };
    }
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
      const res = await fetch("/substrate.json", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const view = await res.json();
      // Post-fetch race re-check (codex L3 HIGH): a passive load that STARTED clean, then
      // the operator opened/typed an editor WHILE this fetch was in flight — rebuilding
      // now would still discard it. The save path (passive:false) always applies.
      if (passive && editInProgress()) { status("editing — refresh deferred"); return; }
      // Gate the write transport on the substrate being writable (NO THEATER): a
      // read-only source (no install root → POST /edit 422s) renders with no edit
      // affordances at all, matching the server. An older payload without
      // `writable` defaults to writable, so existing installs are unchanged.
      window.LevainDashboard.render(view, view.writable === false ? {} : { commit });
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
