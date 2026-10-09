// levain cockpit_boot.js — the cockpit page's fetch lifecycle (design §7: poll the manifest with
// If-None-Match, fetch a panel only when its etag moved). The conditional request is the browser's own
// (`cache: "no-cache"` revalidates with the ETag the kernel sent and serves the cached body on a 304).
// READ HALF ONLY: no write token entry, no intent, no pending state (K2a/K2b, R1's write path).
(function () {
  "use strict";
  const POLL_MS = 3000;
  const COMPACT_MAX_PX = 600;
  const rootEl = document.getElementById("ck-root");
  const stampEl = document.getElementById("ck-stamp");
  const entityEl = document.getElementById("ck-entity");
  const panels = {};   // id -> {etag, profile, payload}
  const opened = new Set();   // feed panels the reader opened: fetched full from then on
  let inflight = false;

  function headers() {
    const h = {};
    try { const t = window.localStorage.getItem("levain_write_token"); if (t) h["X-Levain-Write-Token"] = t; } catch (_) { /* ignore */ }
    return h;
  }
  function profile() {
    const q = new URLSearchParams(window.location.search).get("profile");
    if (q === "full" || q === "compact") return q;
    return window.matchMedia && window.matchMedia("(max-width: " + COMPACT_MAX_PX + "px)").matches ? "compact" : "full";
  }
  async function getJson(url) {
    const r = await fetch(url, { headers: headers(), cache: "no-cache" });
    if (!r.ok) throw new Error(url + " → HTTP " + r.status);
    return r.json();
  }

  async function load() {
    if (inflight) return;
    inflight = true;
    try {
      const prof = profile();
      document.body.classList.toggle("ck-compact", prof === "compact");
      const manifest = await getJson("/cockpit/manifest.json");
      const ids = Object.keys(manifest.panels);
      await Promise.all(ids.map(async (id) => {
        const have = panels[id];
        const etag = manifest.panels[id].etag;
        const want = opened.has(id) ? "full" : prof;
        if (have && have.etag === etag && have.profile === want) return;
        try {
          const payload = await getJson("/cockpit/panel/" + encodeURIComponent(id) + ".json?profile=" + want);
          panels[id] = { etag: etag, profile: want, payload: payload };
        } catch (_) { delete panels[id]; }   // rendered as an error panel, never as a stale-ok one
      }));
      const snap = { manifest: manifest, panels: {} };
      for (const id of ids) if (panels[id]) snap.panels[id] = panels[id].payload;
      entityEl.textContent = LevainCockpit.visible((manifest.entity && manifest.entity.name) || "Levain");
      LevainCockpit.render(document, rootEl, snap, { now: Date.now() });
      stampEl.textContent = "as of " + new Date().toLocaleTimeString();
    } catch (e) {
      stampEl.textContent = "cannot reach the cockpit: " + LevainCockpit.visible(e.message);
    } finally { inflight = false; }
  }

  rootEl.addEventListener("click", async (ev) => {
    const b = ev.target.closest && ev.target.closest("button.ck-open");
    if (!b) return;
    const next = b.getAttribute("data-next");
    if (!next || next.indexOf("/cockpit/panel/") !== 0) return;
    const sec = b.closest("[data-panel]");
    opened.add(sec.getAttribute("data-panel"));
    load();
  });

  load();
  setInterval(load, POLL_MS);
})();
