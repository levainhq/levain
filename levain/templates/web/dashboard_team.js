// levain dashboard_team.js — the cockpit's Team tab: the back-link to `levain team view`.
//
// Reads GET /team_views.json (the running team views registered on this machine, each already confirmed
// alive by the server) and adds ONE control to the existing tab bar: a tab button for a single view, a select
// for several. It adds nothing when no team view is running (no empty tab). It is deliberately a separate
// file that adds its control AFTER dashboard_core's wireTabs() has run, so the zone-filter wiring never sees
// it, and it is not a zone: it only navigates. Read-only; a view's URL is opened only if it is http on a
// loopback host (the server checks too), and the labels are set with textContent (never parsed as HTML).
(function () {
  "use strict";
  const bar = document.querySelector("nav.tabs");
  if (!bar) return;
  let control = null;

  function safeUrl(raw) {
    try {
      const u = new URL(raw);
      const host = u.hostname;
      const loop = host === "localhost" || /^127(\.\d{1,3}){3}$/.test(host);
      return u.protocol === "http:" && loop && u.port ? u.href : null;
    } catch (_) { return null; }
  }
  function go(raw) { const u = safeUrl(raw); if (u) window.location.href = u; }
  function label(v) { return "▣ Team · " + v.project; }

  function render(views) {
    const ok = views.map((v) => ({ v: v, u: safeUrl(v.url) })).filter((x) => x.u);
    if (control) { control.remove(); control = null; }
    if (!ok.length) return;
    if (ok.length === 1) {
      control = document.createElement("button");
      control.type = "button";
      control.className = "tab tab-team";
      control.textContent = "▣ Team";
      control.title = "Open the team view for " + ok[0].v.project;
      control.addEventListener("click", () => go(ok[0].u));
    } else {
      control = document.createElement("select");
      control.className = "tab tab-team";
      control.setAttribute("aria-label", "open a team view");
      control.style.maxWidth = "11em";   // CSSOM, not an inline attribute: allowed under the page CSP
      const first = document.createElement("option");
      first.value = ""; first.textContent = "▣ Team ▾";
      control.appendChild(first);
      ok.forEach((x) => {
        const o = document.createElement("option");
        o.value = x.u;
        o.textContent = label(x.v) + " (" + (x.v.repo.split("/").filter(Boolean).pop() || x.v.repo) + ")";
        control.appendChild(o);
      });
      control.addEventListener("change", () => { const u = control.value; control.value = ""; go(u); });
    }
    bar.appendChild(control);
  }

  function load() {
    fetch("/team_views.json", { headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : { views: [] }))
      .then((j) => render(Array.isArray(j.views) ? j.views : []))
      .catch(() => render([]));
  }
  load();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
})();
