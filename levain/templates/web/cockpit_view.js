// levain cockpit_view.js — the manifest as the dashboard's data source (cockpit design §7, R1).
//
// The dashboard core (dashboard_core.js) draws a SubstrateView. This file builds that same shape from
// the kernel's manifest and panel payloads, so the page keeps its look, layout and components and only
// the DATA SOURCE changes. It is a mapper and nothing else: panels, rows and groups come out in the
// order the kernel sent; nothing is sorted, regrouped, counted or given a gesture here. A panel that
// is `error` (or whose fetch failed) never reaches a native component, where an empty array would read
// "nothing waiting": it becomes an explicit unavailable panel carrying the reason and last-good time.
// Anything the native components do not know (the `now` view, a downstream's own panels, extra header
// panels) is drawn by the core's generic external panel. READ HALF: no verbs, tiers or write state.
(function (root) {
  "use strict";

  // The kernel's own built-in panel ids and the native component each feeds. A panel with any other
  // id (or kind) is a downstream's and takes the generic path. A Map: ids come from the wire, and an
  // id like "constructor" or "__proto__" must never reach an object's prototype.
  const NATIVE = new Map([["tray", "tray"], ["loops", "spores"], ["keep", "keep"], ["episodes", "episodes"],
    ["edits", "edits"], ["health", "health"], ["graph", "graph"], ["crystals", "crystals"], ["wraps", "wraps"]]);
  const BADGE_TEXT = {
    overdue_days: (v) => v + "d overdue", age_days: (v) => v + "d old", due: (v) => "due " + v,
  };

  // Control, format, surrogate and separator characters shown as <U+XXXX>, as the terminal renderer
  // does, so a kernel-sent title cannot reorder or hide what the reader sees. A newline is structure,
  // not content, and is kept (markdown bodies need it).
  function visible(v) {
    return String(v === null || v === undefined ? "" : v).replace(/[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/gu,
      (ch) => (ch === "\n" ? ch : "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">"));
  }

  function parseIso(iso) {
    if (!iso) return NaN;
    return Date.parse(iso.indexOf("T") < 0 || /(Z|[+-]\d\d:?\d\d)$/i.test(iso) ? iso : iso + "Z");
  }

  function ageLabel(iso, now, prefix) {
    const t = parseIso(iso);
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.floor((now - t) / 1000));
    const u = s >= 86400 ? Math.floor(s / 86400) + "d" : s >= 3600 ? Math.floor(s / 3600) + "h"
      : s >= 60 ? Math.floor(s / 60) + "m" : s + "s";
    return (prefix || "") + u + " ago";
  }

  function badgeText(b) {
    const f = Object.prototype.hasOwnProperty.call(BADGE_TEXT, b.kind) ? BADGE_TEXT[b.kind] : null;
    return f ? f(b.value) : b.kind + " " + b.value;
  }

  // "spore:spore-12" -> "spore-12": the source-scoped row id back to the id the components show
  function bare(id) { const i = String(id).indexOf(":"); return i < 0 ? String(id) : String(id).slice(i + 1); }

  function sporeOf(row, groupTitles) {
    const f = row.facets || {};
    return {
      id: bare(row.id), type: f.spore_type || "task", tier: f.tier, salience: f.salience, domain: f.domain,
      disposition: f.disposition, text: visible(row.body || row.title), next: f.due || null,
      descend_kinds: [], ascend_kinds: [],
      group_title: row.group ? (groupTitles.get(row.group) || visible(row.group)) : null, group: row.group || null,
    };
  }

  // a stale or partial panel keeps rendering its rows, under a banner saying so
  function bannerOf(head, panel, now) {
    const out = [];
    if (head.status === "stale") out.push({ tone: "stale", text: "STALE: last read " + (ageLabel(head.as_of, now, "") || "unknown") });
    if (head.status === "partial") for (const s of head.skipped || []) out.push({ tone: "stale", text: "PARTIAL: " + s.count + " unreadable — " + visible(s.reason) });
    for (const f of head.filtered || []) out.push({ tone: "dim", text: f.count + " held back — " + visible(f.reason) });
    if (head.note) out.push({ tone: "dim", text: visible(head.note) });
    // a Metric's alerts (e.g. a wrap in progress) are part of its reading, never dropped
    for (const a of ((panel.value || {}).alerts || [])) out.push({ tone: "stale", text: "! " + visible(a.message) });
    return out.length ? out : null;
  }

  function externalOf(head, panel, groupTitles) {
    const v = panel.value || {};
    const out = { note: visible(head.note || ""), empty: visible(head.empty || "nothing here") };
    if (head.kind === "triage-list") {
      out.lines = (panel.rows || []).map((r) => {
        const grp = r.group ? (groupTitles.get(r.group) || visible(r.group)) : "";
        const badges = (r.badges || []).map(badgeText).join(" · ");
        const prov = r.provenance ? visible(r.provenance.label) : "";
        const meta = [r.panel_id ? visible(r.panel_id) : "", grp, badges, prov].filter(Boolean).join(" · ");
        return { meta: meta, text: visible(r.body || r.title), accent: r.emphasis === "accent", dim: r.emphasis === "dim" };
      });
      if (panel.degraded && panel.degraded.length) out.note = "not complete: " + visible(panel.error || panel.degraded.join(", "));
    } else if (head.kind === "line") {
      out.lines = (v.lines || []).map((l) => ({ meta: visible(l.label), text: visible(l.text) }));
    } else if (head.kind === "metric") {
      out.lines = (v.metrics || []).map((m) => ({
        meta: visible(m.label), text: visible(m.value + (m.unit ? " " + m.unit : "") + (m.read ? " — " + m.read : "")),
        accent: m.status === "warn" || m.status === "bad", dim: m.status === "unknown" }));
      for (const a of v.alerts || []) out.lines.push({ meta: "!", text: visible(a.message), accent: true });
    } else if (head.kind === "visual") {
      out.lines = (v.text || []).map((l) => ({ meta: visible(l.label), text: visible(l.text) }));
    } else if (head.kind === "prose") {
      out.markdown = visible(v.markdown || "");
    }
    return out;
  }

  // wraps = null means the wraps panel was unreadable: last-wrap figures are then ABSENT, not "never"
  function healthOf(metrics, wraps) {
    const m = new Map();
    for (const x of metrics) m.set(x.label, x.value);
    const last = wraps && wraps[0] ? wraps[0] : null;
    return {
      write_path_live: m.get("write path") === "live", total_links: m.get("links"), avg_strength: m.get("avg strength"),
      density: m.get("density"), total_episodes: m.get("episodes"), episodes_since_wrap: m.get("episodes since wrap"),
      tombstones: m.get("tombstones"), total_wraps: m.get("wraps"),
      graduations_validated_total: m.get("graduations validated"), graduations_demoted_total: m.get("graduations demoted"),
      // not in the manifest (a K1 gap, routed): max_strength, local_density. The core omits what is absent.
      last_wrap_at: last ? last.wrapped_at : null, continuity_chars: last ? last.continuity_chars : null,
    };
  }

  function lineOf(panel, head, now, withSet) {
    if (head.status === "error") return null;
    const l = ((panel.value || {}).lines || [])[0];
    if (!l) return { text: null };
    const t = parseIso(l.at);
    const known = !isNaN(t) && t <= now + 60000;   // an unparseable or future stamp is "age unknown", never "fresh"
    const out = { text: visible(l.text), set_at: l.at, source: l.source, age_label: known ? ageLabel(l.at, now, "set ") : "" };
    if (withSet) { out.stale = head.status === "stale"; out.freshness = known ? "fresh" : "unknown"; }
    return out;
  }

  // snap = {manifest, panels}; returns the SubstrateView-shaped object dashboard_core.js renders.
  // `snap.panels` maps a panel id to its payload, or to {error} when its fetch failed.
  function fromManifest(snap) {
    const m = snap.manifest;
    const gen = parseIso(m.generated_at);
    const now = isNaN(gen) ? Date.now() : gen;   // the server's clock, so a skewed browser cannot misreport ages
    const P = new Map(Object.entries(snap.panels || {}));
    const heads = new Map(Object.entries(m.panels || {}));
    const ent = m.entity || {};
    const view = {
      paths: { omitted: true }, scope: ent.governance, entity_name: visible(ent.name),
      brand_wordmark: visible((ent.brand || {}).wordmark), brand_model: visible((ent.brand || {}).model),
      health: null, graph: null, crystal_index: [], open_spores: [], tray: [], keep: [], episodes: [],
      sections: [], config_docs: [], wraps: [], recent_edits: [], focus: null, state: null, jar: null,
      layout: [], errors: Object.create(null), extra_panels: Object.create(null), writable: false, write_token_required: false,
    };
    for (const e of m.errors || []) view.errors[e.source || "manifest"] = visible(e.message);

    // a panel is unreadable when its payload is missing, a fetch failure, or the kernel said `error`
    const failure = (pid) => {
      const p = P.get(pid), h = heads.get(pid);
      if (p && p.error !== undefined && p.status === undefined) return visible(p.error);      // our own fetch failure record
      if (!p) return "no payload";
      if (p.status === "error") return visible(p.error || "unavailable");
      return null;
    };
    const unavailable = (pid, zone, why) => {
      const h = heads.get(pid) || {};
      view.layout.push({ kind: "external", zone: zone, edit_class: "", title: visible(h.title || pid), id: pid });
      view.extra_panels[pid] = { error: why + " — last good " + (h.as_of ? ageLabel(h.as_of, now, "") : "never") };
    };

    const wrapsFail = heads.has("wraps") ? failure("wraps") : null;
    const wrapsData = heads.has("wraps") && !wrapsFail ? (P.get("wraps").value || {}).data || [] : null;
    if (wrapsFail) view.errors.wraps = wrapsFail;

    // header region: focus and state go under the masthead; any other header panel (a downstream's
    // weather, say) is drawn as a panel first in Operate, never silently dropped
    const headerExtra = [];
    for (const pid of (m.regions.header || [])) {
      const head = heads.get(pid);
      if (!head) { view.errors[pid] = "listed in the manifest header but has no head"; continue; }
      const bad = failure(pid);
      if (pid === "focus" || pid === "state") {
        if (bad) { view.errors[pid] = bad; continue; }
        const line = lineOf(P.get(pid), P.get(pid), now, pid === "focus");
        if (pid === "focus") view.focus = line; else view.state = line;
      } else headerExtra.push(pid);
    }

    let sectionRef = 0;
    const place = (pid, zoneId) => {
      const head = heads.get(pid);
      if (!head) { view.errors[pid] = "listed in the manifest but has no head"; return; }
      const bad = failure(pid);
      if (bad) { unavailable(pid, zoneId, bad); return; }
      const panel = P.get(pid);
      const groupTitles = new Map();
      for (const g of panel.groups || head.groups || []) groupTitles.set(g.id, visible(g.title));
      const entry = { zone: zoneId, edit_class: "", title: visible(panel.title || head.title), id: pid };
      const banner = bannerOf(panel, panel, now);
      if (banner) entry.banner = banner;
      if (panel.as_of) entry.fresh = ageLabel(panel.as_of, now, "read ");
      const rows = panel.rows || [];
      const kind = NATIVE.get(pid);
      const lists = { tray: "tray", loops: "open_spores", keep: "keep" };
      if ((pid === "tray" || pid === "loops" || pid === "keep") && panel.kind === "triage-list") {
        view[lists[pid]] = rows.map((r) => sporeOf(r, groupTitles));
        view.layout.push(Object.assign(entry, { kind: kind }));
      } else if (pid === "episodes" && panel.kind === "triage-list") {
        view.episodes = rows.map((r) => ({ id: bare(r.id), timestamp: (r.facets || {}).at, type: visible((r.facets || {}).episode_type),
          source: (r.facets || {}).source, tags: (r.facets || {}).tags || [], content: visible(r.body || r.title) }));
        view.layout.push(Object.assign(entry, { kind: "episodes" }));
      } else if (pid === "edits" && panel.kind === "triage-list") {
        view.recent_edits = rows.map((r) => ({ id: bare(r.id), ts: (r.facets || {}).at, action: (r.facets || {}).edit_kind,
          source: visible(r.title), undoable: (r.facets || {}).undoable }));
        view.layout.push(Object.assign(entry, { kind: "edits" }));
      } else if (pid === "crystals" && panel.kind === "triage-list") {
        view.crystal_index = rows.map((r) => {
          const f = r.facets || {};
          // the kernel sends a tag list as one comma-joined string in a one-element list (a K1 quirk, routed)
          const tags = [].concat(f.tags || []).join(",").split(",").map((s) => s.trim()).filter(Boolean).map(visible);
          return { name: visible(bare(r.id)), level: f.crystal_level, one_clause: visible(r.body || r.title), permanence: f.permanence,
                   last_activated_on: f.last_activated_on, tags: tags };
        });
        view.layout.push(Object.assign(entry, { kind: "crystals" }));
      } else if (pid === "health" && panel.kind === "metric") {
        view.health = healthOf((panel.value || {}).metrics || [], wrapsData);
        view.layout.push(Object.assign(entry, { kind: "health" }));
      } else if (pid === "graph" && panel.kind === "visual") {
        view.graph = (panel.value || {}).data || null;
        view.layout.push(Object.assign(entry, { kind: "graph" }));
      } else if (pid === "wraps" && panel.kind === "visual") {
        view.wraps = wrapsData || [];
        view.layout.push(Object.assign(entry, { kind: "wraps" }));
      } else if (panel.kind === "prose" && pid.indexOf("section:") === 0 && (panel.value || {}).markdown != null) {
        view.sections.push({ heading: visible(panel.title || head.title), body: visible(panel.value.markdown) });
        view.layout.push(Object.assign(entry, { kind: "section", ref: sectionRef++, heading: visible(panel.title || head.title) }));
      } else {
        view.extra_panels[pid] = externalOf(panel, panel, groupTitles);
        view.layout.push(Object.assign(entry, { kind: "external" }));
      }
    };

    for (const pid of headerExtra) place(pid, "operate");
    for (const zone of (m.regions.zones || [])) for (const pid of (zone.panels || [])) place(pid, zone.id);
    return view;
  }

  const api = { fromManifest, ageLabel, badgeText, visible };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainCockpitView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
