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

  async function load() {
    if (inflight) return; // a focus event mid-refresh must not stack a 2nd fetch
    // If the render core didn't load (reorder / serve failure), say so plainly
    // rather than throwing an opaque TypeError after a successful fetch.
    if (!window.LevainDashboard || typeof window.LevainDashboard.render !== "function") {
      status("render core failed to load");
      return;
    }
    inflight = true;
    if (btn) btn.disabled = true;
    try {
      const res = await fetch("/substrate.json", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const view = await res.json();
      window.LevainDashboard.render(view, { commit });
      status("read " + new Date().toLocaleTimeString());
    } catch (e) {
      status("read failed: " + (e && e.message ? e.message : e));
    } finally {
      inflight = false;
      if (btn) btn.disabled = false;
    }
  }

  if (btn) btn.addEventListener("click", load);
  // The substrate only changes on a wrap, so we don't poll — but re-read when the
  // tab becomes visible again (cheap; catches a wrap that landed while the
  // operator was away — the "something moved while you were gone" the v2 spine is
  // ultimately about, here in its passive read-only form). visibilitychange is
  // the reliable "tab became active" signal; the inflight guard prevents stacking.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") load();
  });
  load();
})();
