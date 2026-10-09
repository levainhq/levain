// levain cockpit_view.js — the manifest as the dashboard's data source (cockpit design §7, R1).
//
// The dashboard core (dashboard_core.js) draws a SubstrateView. This file builds that same shape from
// the kernel's manifest and panel payloads, so the page keeps its look, layout and components and only
// the DATA SOURCE changes. It is a mapper and nothing else: panels, rows and groups come out in the
// order the kernel sent; nothing is sorted, regrouped, counted or given a gesture here. A panel that
// is `error` never reaches a native component (an empty array there would read "nothing waiting"): it
// becomes an explicit unavailable panel carrying the kernel's message and last-good time. Anything the
// native components do not know (the `now` view, a downstream's own panels) is drawn by the core's
// generic external panel. READ HALF: no verbs, tiers or write state.
(function (root) {
  "use strict";

  // The kernel's own built-in panel ids and the native component each feeds. A panel with any other
  // id (or kind) is a downstream's and takes the generic path.
  const NATIVE = { tray: "tray", loops: "spores", keep: "keep", episodes: "episodes", edits: "edits",
                   health: "health", graph: "graph", crystals: "crystals", wraps: "wraps" };
  const BADGE_TEXT = {
    overdue_days: (v) => v + "d overdue", age_days: (v) => v + "d old", due: (v) => "due " + v,
  };

  function ageLabel(iso, now, prefix) {
    const t = Date.parse(iso && (iso.indexOf("T") < 0 || /(Z|[+-]\d\d:?\d\d)$/i.test(iso)) ? iso : iso + "Z");
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.floor((now - t) / 1000));
    const u = s >= 86400 ? Math.floor(s / 86400) + "d" : s >= 3600 ? Math.floor(s / 3600) + "h"
      : s >= 60 ? Math.floor(s / 60) + "m" : s + "s";
    return (prefix || "") + u + " ago";
  }

  function badgeText(b) {
    const f = BADGE_TEXT[b.kind];
    return f ? f(b.value) : b.kind + " " + b.value;
  }

  // "spore:spore-12" -> "spore-12": the source-scoped row id back to the id the components show
  function bare(id) { const i = String(id).indexOf(":"); return i < 0 ? String(id) : String(id).slice(i + 1); }

  function sporeOf(row, groupTitles) {
    const f = row.facets || {};
    return {
      id: bare(row.id), type: f.spore_type || "task", tier: f.tier, salience: f.salience, domain: f.domain,
      disposition: f.disposition, text: row.body || row.title, next: f.due || null,
      descend_kinds: [], ascend_kinds: [],
      group_title: row.group ? (groupTitles[row.group] || row.group) : null, group: row.group || null,
    };
  }

  // a stale or partial panel keeps rendering its rows, under a banner saying so
  function bannerOf(head, now) {
    const out = [];
    if (head.status === "stale") out.push({ tone: "stale", text: "STALE: last read " + (ageLabel(head.as_of, now, "") || "unknown") });
    if (head.status === "partial") for (const s of head.skipped || []) out.push({ tone: "stale", text: "PARTIAL: " + s.count + " unreadable — " + s.reason });
    for (const f of head.filtered || []) out.push({ tone: "dim", text: f.count + " held back — " + f.reason });
    if (head.note) out.push({ tone: "dim", text: head.note });
    return out.length ? out : null;
  }

  function externalOf(head, panel) {
    const v = panel.value || {};
    const out = { note: head.note || "", empty: head.empty || "nothing here" };
    if (head.kind === "triage-list") {
      out.lines = (panel.rows || []).map((r) => {
        const meta = [r.panel_id, r.group].filter(Boolean).join(" · ");
        const badges = (r.badges || []).map(badgeText).join(" · ");
        return { meta: [meta, badges].filter(Boolean).join(" · "), text: r.title,
                 accent: r.emphasis === "accent", dim: r.emphasis === "dim" };
      });
      if (panel.degraded && panel.degraded.length) out.note = "not complete: " + (panel.error || panel.degraded.join(", "));
    } else if (head.kind === "line") {
      out.lines = (v.lines || []).map((l) => ({ meta: l.label, text: l.text }));
    } else if (head.kind === "metric") {
      out.lines = (v.metrics || []).map((m) => ({
        meta: m.label, text: m.value + (m.unit ? " " + m.unit : "") + (m.read ? " — " + m.read : ""),
        accent: m.status === "warn" || m.status === "bad", dim: m.status === "unknown" }));
      for (const a of v.alerts || []) out.lines.push({ meta: "!", text: a.message, accent: true });
    } else if (head.kind === "visual") {
      out.lines = (v.text || []).map((l) => ({ meta: l.label, text: l.text }));
    } else if (head.kind === "prose") {
      out.markdown = v.markdown || "";
    }
    return out;
  }

  function healthOf(metrics, wraps) {
    const m = {};
    for (const x of metrics) m[x.label] = x.value;
    const last = (wraps && wraps[0]) || null;
    return {
      write_path_live: m["write path"] === "live", total_links: m["links"], avg_strength: m["avg strength"],
      density: m["density"], total_episodes: m["episodes"], episodes_since_wrap: m["episodes since wrap"],
      tombstones: m["tombstones"], total_wraps: m["wraps"],
      graduations_validated_total: m["graduations validated"], graduations_demoted_total: m["graduations demoted"],
      // not in the manifest (a K1 gap, routed): max_strength, local_density. The core omits what is absent.
      last_wrap_at: last ? last.wrapped_at : null, continuity_chars: last ? last.continuity_chars : null,
    };
  }

  function lineOf(panel, head, now, withSet) {
    if (head.status === "error") return null;
    const l = ((panel.value || {}).lines || [])[0];
    if (!l) return { text: null };
    const out = { text: l.text, set_at: l.at, source: l.source, age_label: l.at ? ageLabel(l.at, now, "set ") : "" };
    if (withSet) { out.stale = head.status === "stale"; out.freshness = l.at ? "fresh" : "unknown"; }
    return out;
  }

  // snap = {manifest, panels}; returns the SubstrateView-shaped object dashboard_core.js renders.
  function fromManifest(snap, now) {
    now = now || Date.now();
    const m = snap.manifest, P = snap.panels;
    const ent = m.entity || {};
    const view = {
      paths: { omitted: true }, scope: ent.governance, entity_name: ent.name,
      brand_wordmark: (ent.brand || {}).wordmark, brand_model: (ent.brand || {}).model,
      health: null, graph: null, crystal_index: [], open_spores: [], tray: [], keep: [], episodes: [],
      sections: [], config_docs: [], wraps: [], recent_edits: [], focus: null, state: null, jar: null,
      layout: [], errors: {}, extra_panels: {}, writable: false, write_token_required: false,
    };
    for (const e of m.errors || []) view.errors[e.source || "manifest"] = e.message;

    const unavailable = (head, zone, pid) => {
      view.layout.push({ kind: "external", zone: zone, edit_class: "", title: head.title, id: pid });
      view.extra_panels[pid] = { error: (head.error || "unavailable") + " — last good " + (head.as_of ? ageLabel(head.as_of, now, "") : "never") };
    };

    // header region: the focus and state lines under the masthead
    for (const pid of (m.regions.header || [])) {
      const head = (P[pid] || m.panels[pid]);
      if (pid === "focus") view.focus = P[pid] ? lineOf(P[pid], head, now, true) : null;
      else if (pid === "state") view.state = P[pid] ? lineOf(P[pid], head, now, false) : null;
      if (head.status === "error") view.errors[pid] = head.error || "unavailable";
    }

    const wrapsData = P.wraps && P.wraps.status !== "error" ? (P.wraps.value || {}).data || [] : [];
    let sectionRef = 0;
    for (const zone of (m.regions.zones || [])) {
      for (const pid of (zone.panels || [])) {
        const panel = P[pid];
        const head = panel || m.panels[pid];
        if (!head) continue;
        if (head.status === "error" || !panel) { unavailable(head, zone.id, pid); continue; }
        const groupTitles = {};
        for (const g of head.groups || []) groupTitles[g.id] = g.title;
        const banner = bannerOf(head, now);
        const entry = { zone: zone.id, edit_class: "", title: head.title, id: pid };
        if (banner) entry.banner = banner;
        if (head.as_of) entry.fresh = ageLabel(head.as_of, now, "read ");
        const rows = panel.rows || [];
        const kind = NATIVE[pid];
        if (kind && head.kind === "triage-list" && (pid === "tray" || pid === "loops" || pid === "keep")) {
          const list = rows.map((r) => sporeOf(r, groupTitles));
          if (pid === "tray") view.tray = list; else if (pid === "keep") view.keep = list; else view.open_spores = list;
          view.layout.push(Object.assign(entry, { kind: kind }));
        } else if (kind === "episodes" && head.kind === "triage-list") {
          view.episodes = rows.map((r) => ({ id: bare(r.id), timestamp: (r.facets || {}).at, type: (r.facets || {}).episode_type,
            source: (r.facets || {}).source, tags: (r.facets || {}).tags || [], content: r.body || r.title }));
          view.layout.push(Object.assign(entry, { kind: "episodes" }));
        } else if (kind === "edits" && head.kind === "triage-list") {
          view.recent_edits = rows.map((r) => ({ id: bare(r.id), ts: (r.facets || {}).at, action: (r.facets || {}).edit_kind,
            source: r.title, undoable: (r.facets || {}).undoable }));
          view.layout.push(Object.assign(entry, { kind: "edits" }));
        } else if (kind === "crystals" && head.kind === "triage-list") {
          view.crystal_index = rows.map((r) => {
            const f = r.facets || {};
            // the kernel sends a tag list as one comma-joined string in a one-element list (a K1 quirk, routed)
            const tags = [].concat(f.tags || []).join(",").split(",").map((s) => s.trim()).filter(Boolean);
            return { name: bare(r.id), level: f.crystal_level, one_clause: r.body || r.title, permanence: f.permanence,
                     last_activated_on: f.last_activated_on, tags: tags };
          });
          view.layout.push(Object.assign(entry, { kind: "crystals" }));
        } else if (kind === "health" && head.kind === "metric") {
          view.health = healthOf((panel.value || {}).metrics || [], wrapsData);
          view.layout.push(Object.assign(entry, { kind: "health" }));
        } else if (kind === "graph" && head.kind === "visual") {
          view.graph = (panel.value || {}).data || null;
          view.layout.push(Object.assign(entry, { kind: "graph" }));
        } else if (kind === "wraps" && head.kind === "visual") {
          view.wraps = wrapsData;
          view.layout.push(Object.assign(entry, { kind: "wraps" }));
        } else if (head.kind === "prose" && pid.indexOf("section:") === 0 && (panel.value || {}).markdown != null) {
          view.sections.push({ heading: head.title, body: panel.value.markdown });
          view.layout.push(Object.assign(entry, { kind: "section", ref: sectionRef++, heading: head.title }));
        } else {
          view.extra_panels[pid] = externalOf(head, panel);
          view.layout.push(Object.assign(entry, { kind: "external" }));
        }
      }
    }
    return view;
  }

  const api = { fromManifest, ageLabel, badgeText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainCockpitView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
