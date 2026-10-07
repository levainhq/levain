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
  let note = null;

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

  // The server says when its list may be incomplete (a cap, a time limit or a read error); say so, once, instead of
  // letting a short list pass as complete.
  function render(views, truncated) {
    const ok = views.map((v) => ({ v: v, u: safeUrl(v.url) })).filter((x) => x.u);
    if (control) { control.remove(); control = null; }
    if (note) { note.remove(); note = null; }
    if (truncated) {
      note = document.createElement("span");
      note.className = "tab-team-note";
      note.textContent = "team list may be incomplete";
      bar.appendChild(note);
    }
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

  // The server answers /team_views.json only to a loopback peer on a loopback-bound cockpit (anything else is a 404).
  // The request also carries the cockpit's own auth headers when dashboard_boot.js provides window.levainAuthHeaders,
  // so it keeps working when the cockpit's data routes require them; without that function it sends none.
  // Loads can overlap (page load, then tab focus). A success is applied unless a NEWER request has already been
  // applied, so an older success is not thrown away just because a newer request failed (a failure applies nothing).
  let latest = 0;
  let applied = 0;
  function load() {
    const mine = ++latest;
    // Only a successful response changes the UI: a non-ok answer or a failed fetch keeps what is shown now (an
    // empty list from a healthy server still clears it).
    const auth = window.levainAuthHeaders ? window.levainAuthHeaders() : {};
    fetch("/team_views.json", { headers: { ...auth, Accept: "application/json" } })
      .then((r) => { if (!r.ok) throw new Error("status " + r.status); return r.json(); })
      .then((j) => { if (mine > applied && Array.isArray(j.views)) { applied = mine; render(j.views, j.truncated === true); } })
      .catch(() => {});
  }
  load();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  // The first load can run before the page has traded its one-time link code for the cockpit's token, and be
  // refused; load again once it has (window.LevainToken exists only where the cockpit uses a token).
  if (window.LevainToken) window.LevainToken.onUnlock(load);
})();
