// The tap trace page: lists the trace ring (Cache Storage cache `levain-trace`, written by the push worker and by
// sw_open.js) newest first, one plain line per step, with a Clear button. It holds no credential and makes no network call.
(() => {
  "use strict";
  const CACHE = "levain-trace", PREFIX = "/__levain_trace/";
  const head = document.getElementById("tt-head"), lines = document.getElementById("tt-lines");
  const clock = (t) => { const d = new Date(t); const p = (n, w = 2) => String(n).padStart(w, "0"); return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`; };
  const detail = (e) => Object.keys(e).filter((k) => !["t", "src", "step"].includes(k))
    .map((k) => `${k}=${typeof e[k] === "object" && e[k] !== null ? JSON.stringify(e[k]) : e[k]}`).join(" ");

  async function entries() {
    if (typeof caches === "undefined") return null;
    const c = await caches.open(CACHE);
    const out = [];
    for (const req of await c.keys()) {
      const key = new URL(req.url).pathname;
      if (key.indexOf(PREFIX) !== 0) continue;
      try { const r = await c.match(req); out.push({ key, e: await r.json() }); } catch (_) { /* an unreadable entry is skipped */ }
    }
    out.sort((a, b) => (a.key < b.key ? 1 : -1));   // newest first
    return out;
  }

  async function render() {
    let list;
    try { list = await entries(); } catch (err) { head.textContent = "The trace cache could not be read: " + (err && err.name || "error"); return; }
    if (list === null) { head.textContent = "No Cache Storage here (an insecure context)."; lines.replaceChildren(); return; }
    const ver = list.find((x) => x.e.step === "sw-version");
    let ctl = "none";
    try { const c = navigator.serviceWorker && navigator.serviceWorker.controller; if (c) ctl = new URL(c.scriptURL).pathname; } catch (_) { /* unreadable */ }
    head.textContent = `SW_VERSION ${ver ? ver.e.version + " (" + ver.e.on + ")" : "not in the ring"} · controller ${ctl} · ${list.length} steps`;
    // one wrapping paragraph per step, in the page's own message style (large, breaks long urls)
    const rows = list.map((x) => `${clock(x.e.t)} ${x.e.src} ${x.e.step} ${detail(x.e)}`.trim());
    lines.replaceChildren(...(rows.length ? rows : ["(empty)"]).map((r) => { const p = document.createElement("p"); p.className = "pair-msg"; p.textContent = r; return p; }));
  }

  document.getElementById("tt-refresh").addEventListener("click", render);
  document.getElementById("tt-clear").addEventListener("click", async () => {
    try { await caches.delete(CACHE); } catch (_) { /* nothing to clear */ }
    render();
  });
  render();
})();
