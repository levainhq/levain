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

  // ONE sanitising boundary. Every string in the manifest and the panels goes through clean() once, on
  // entry, so no per-field guard can miss a field. It shows as <U+XXXX> the characters that can move or
  // hide neighbouring text: control characters (a tab and newlines are structure and stay), the bidi
  // controls and marks, the invisible format characters, a byte-order mark, lone surrogates and the line
  // and paragraph separators. The zero-width joiner and non-joiner stay: emoji sequences and Persian or
  // Indic text need them.
  const HOSTILE = /[\p{Cc}\p{Cs}\p{Zl}\p{Zp}\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb\u{e0000}-\u{e007f}]/gu;
  function visible(v) {
    return String(v === null || v === undefined ? "" : v).replace(/\r\n/g, "\n").replace(HOSTILE,
      (ch) => (ch === "\n" || ch === "\t" ? ch : "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">"));
  }
  function clean(x) {
    if (typeof x === "string") return visible(x);
    if (Array.isArray(x)) return x.map(clean);
    if (x && typeof x === "object") {
      // null-prototype with own data properties: a wire key such as "__proto__" is data, never the prototype.
      // Keys are cleaned like values, so an id and the references to it still agree after cleaning.
      // Two raw keys that clean to one cannot both be kept and no choice between them is stable (the
      // payload map's order is fetch-completion order), so the snapshot is refused whole.
      const o = Object.create(null);
      for (const k of Object.keys(x)) {
        let key = visible(k);
        if (Object.prototype.hasOwnProperty.call(o, key)) throw new Error("two wire keys collide once cleaned: " + key);
        Object.defineProperty(o, key, { value: clean(x[k]), enumerable: true, writable: true, configurable: true });
      }
      return o;
    }
    return x;
  }

  // a title is text: an object or number where one belongs falls back rather than reaching a renderer
  function titleOf(v, fallback) { return typeof v === "string" && v ? v : fallback; }

  function parseIso(iso) {
    if (typeof iso !== "string" || !iso) return NaN;
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
      disposition: f.disposition, text: row.body || row.title, next: f.due || null,
      descend_kinds: [], ascend_kinds: [],
      group_title: row.group ? (groupTitles.get(row.group) || row.group) : null, group: row.group || null,
    };
  }

  // a stale or partial panel keeps rendering its rows, under a banner saying so
  function bannerOf(head, panel, now) {
    const out = [];
    if (head.status === "stale") out.push({ tone: "stale", text: "STALE: last read " + (ageLabel(head.as_of, now, "") || "unknown") });
    if (head.status === "partial") for (const s of head.skipped || []) out.push({ tone: "stale", text: "PARTIAL: " + s.count + " unreadable — " + s.reason });
    for (const f of head.filtered || []) out.push({ tone: "dim", text: f.count + " held back — " + f.reason });
    if (head.note) out.push({ tone: "dim", text: head.note });
    // a Metric's alerts (e.g. a wrap in progress) are part of its reading, never dropped
    for (const a of ((panel.value || {}).alerts || [])) out.push({ tone: "stale", text: "! " + a.message });
    return out.length ? out : null;
  }

  function externalOf(head, panel, groupTitles) {
    const v = panel.value || {};
    const out = { note: head.note || "", empty: head.empty || "nothing here" };
    if (head.kind === "triage-list") {
      out.lines = (panel.rows || []).map((r) => {
        const grp = r.group ? (groupTitles.get(r.group) || r.group) : "";
        const badges = (r.badges || []).map(badgeText).join(" · ");
        const prov = r.provenance ? r.provenance.label : "";
        const meta = [r.panel_id ? r.panel_id : "", grp, badges, prov].filter(Boolean).join(" · ");
        return { meta: meta, text: r.body || r.title, accent: r.emphasis === "accent", dim: r.emphasis === "dim" };
      });
      if (panel.degraded && panel.degraded.length) out.note = "not complete: " + (panel.error || panel.degraded.join(", "));
    } else if (head.kind === "line") {
      out.lines = (v.lines || []).map((l) => ({ meta: l.label, text: l.text }));
    } else if (head.kind === "metric") {
      need(Array.isArray(v.metrics || []) && (v.metrics || []).every(isObj), "metrics");
      out.lines = (v.metrics || []).map((m) => ({
        meta: m.label, text: m.value + (m.unit ? " " + m.unit : "") + (m.read ? " — " + m.read : ""),
        accent: m.status === "warn" || m.status === "bad", dim: m.status === "unknown" }));
    } else if (head.kind === "visual") {
      out.lines = (v.text || []).map((l) => ({ meta: l.label, text: l.text }));
    } else if (head.kind === "prose") {
      out.markdown = v.markdown || "";
    }
    return out;
  }

  // PARSE, DON'T VALIDATE. Each panel kind has ONE shape check at this boundary. A payload that fails it
  // throws inside its own mapping, and placeOnce() turns that into an unavailable panel while every other
  // panel still renders. Nothing here repairs a bad field into a value that reads as data: a default such
  // as 0 or "" is a reading the renderer would print as true.
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  const isNum = (x) => typeof x === "number" && Number.isFinite(x);
  function need(ok, what) { if (!ok) throw new Error("malformed " + what); }
  // text where text belongs; absent (null/undefined) is allowed and stays absent
  function textOrAbsent(x, what) { need(x === undefined || x === null || typeof x === "string", what); return x === null ? undefined : x; }
  // a tag list: the legacy form is ONE comma-joined string, which is split; an array's elements are whole tags
  function tagsOf(t) {
    if (t === undefined || t === null) return [];
    if (typeof t === "string") return t.split(",").map((s) => s.trim()).filter(Boolean);
    need(Array.isArray(t) && t.every((s) => typeof s === "string"), "tags");
    return t;
  }

  // The entity record: a bad masthead string is omitted and named in view.errors.entity; the jar is all-or-null.
  function entityOf(ent, errors) {
    const bad = [];
    const text = (x, what) => { if (x === undefined || x === null) return undefined;
      if (typeof x === "string") return x; bad.push(what); return undefined; };
    if (!isObj(ent)) { if (ent !== undefined && ent !== null) bad.push("entity"); ent = {}; }
    const brand = isObj(ent.brand) ? ent.brand : (ent.brand === undefined || ent.brand === null ? {} : (bad.push("brand"), {}));
    const out = { name: text(ent.name, "name"), governance: text(ent.governance, "governance"),
                  wordmark: text(brand.wordmark, "brand.wordmark"), model: text(brand.model, "brand.model"),
                  store: text(ent.store_label, "store_label"), jar: null };
    const j = ent.jar;
    if (j !== undefined && j !== null) {
      const ok = isObj(j) && typeof j.status === "string" && typeof j.label === "string" && isNum(j.level)
        && j.level >= 0 && j.level <= 1 && (j.today === undefined || j.today === null || Number.isInteger(j.today))
        && (j.day === undefined || j.day === null || typeof j.day === "string");
      if (ok) out.jar = { status: j.status, label: j.label, level: j.level, today: j.today == null ? null : j.today, day: j.day == null ? null : j.day };
      else bad.push("jar");
    }
    if (bad.length) errors.entity = "malformed entity field(s) omitted: " + bad.join(", ");
    return out;
  }

  // The figures health reads: each is a finite number, except the write-path flag.
  const HEALTH_FIGURES = ["links", "avg strength", "max strength", "density", "local density", "episodes",
    "episodes since wrap", "tombstones", "wraps", "graduations validated", "graduations demoted"];
  function healthOf(metrics, wraps) {
    need(Array.isArray(metrics), "metrics");
    const m = new Map();
    for (const x of metrics) { need(isObj(x) && typeof x.label === "string", "metric"); m.set(x.label, x.value); }
    for (const k of HEALTH_FIGURES) if (m.has(k)) need(isNum(m.get(k)), "metric " + k);
    if (m.has("write path")) need(m.get("write path") === "live" || m.get("write path") === "dark", "metric write path");
    const n = (k) => (m.has(k) ? m.get(k) : undefined);
    const last = Array.isArray(wraps) && wraps[0] ? wraps[0] : null;
    return {
      write_path_live: m.get("write path") === "live", total_links: n("links"), avg_strength: n("avg strength"),
      max_strength: n("max strength"), density: n("density"), local_density: n("local density"),
      total_episodes: n("episodes"), episodes_since_wrap: n("episodes since wrap"),
      tombstones: n("tombstones"), total_wraps: n("wraps"),
      graduations_validated_total: n("graduations validated"), graduations_demoted_total: n("graduations demoted"),
      last_wrap_at: last ? last.wrapped_at : null, continuity_chars: last ? last.continuity_chars : null,
      wrap_history_unavailable: wraps === "unavailable",
    };
  }

  // wraps = "unavailable" means the wraps panel was unreadable: last-wrap figures are then ABSENT, not "never"
  function checkWraps(data) {
    need(Array.isArray(data), "wraps data");
    for (const w of data) need(isObj(w) && typeof w.wrapped_at === "string" && (w.continuity_chars === null || isNum(w.continuity_chars)), "wraps row");
  }

  function lineOf(panel, head, now) {
    if (head.status === "error") return null;
    const l = ((panel.value || {}).lines || [])[0];
    if (!l) return { text: null };
    const t = parseIso(l.at);
    const known = !isNaN(t) && t <= now + 60000;   // an unparseable or future stamp is "age unknown", never "fresh"
    const out = { text: l.text, set_at: l.at, source: l.source, age_label: known ? ageLabel(l.at, now, "set ") : "" };
    return out;
  }

  // snap = {manifest, panels}; returns the SubstrateView-shaped object dashboard_core.js renders.
  // `snap.panels` maps a panel id to its payload, or to {error} when its fetch failed.
  function fromManifest(raw, opts) {
    const elapsed = Math.max(0, (opts && opts.elapsedMs) || 0);   // time since the manifest was fetched, measured by the caller
    // The manifest and each panel are cleaned apart: a hostile key inside ONE panel's payload (two keys that
    // clean to one) rejects that panel alone; a collision in the manifest or between two panel ids is
    // refused whole, since the references between them cannot then be told apart.
    const snap = { manifest: clean(raw.manifest), panels: Object.create(null) };
    for (const key of Object.keys(raw.panels || {})) {
      const k = visible(key);
      if (Object.prototype.hasOwnProperty.call(snap.panels, k)) throw new Error("two panel ids collide once cleaned: " + k);
      let v;
      try { v = clean(raw.panels[key]); } catch (e) { v = { error: "payload rejected: " + (e && e.message ? e.message : e) }; }
      Object.defineProperty(snap.panels, k, { value: v, enumerable: true, writable: true, configurable: true });
    }
    const m = snap.manifest;
    const gen = parseIso(m.generated_at);
    let now = isNaN(gen) ? Date.now() : gen;   // the server's clock, so a skewed browser cannot misreport ages
    const genAt = now;
    for (const p of Object.values(snap.panels || {})) {
      // panels are fetched after the manifest, so the measured fetch time (plus a minute of skew) may have
      // passed; a stamp further ahead than that is a bad stamp, and it must not move every other panel's clock
      const t = parseIso(p && p.as_of);
      if (!isNaN(t) && t > now && t <= genAt + elapsed + 60000) now = t;
    }
    const P = new Map(Object.entries(snap.panels || {}));
    const heads = new Map(Object.entries(m.panels || {}));
    const view_errors = Object.create(null);
    const ent = entityOf(m.entity, view_errors);
    const view = {
      // the store line is the kernel's home-relative label, never an absolute path; no label, no line
      paths: ent.store ? { episodic_db: ent.store } : { omitted: true }, scope: ent.governance, entity_name: ent.name,
      brand_wordmark: ent.wordmark, brand_model: ent.model,
      health: null, graph: null, crystal_index: [], open_spores: [], tray: [], keep: [], episodes: [],
      sections: [], config_docs: [], wraps: [], recent_edits: [], state: null, jar: ent.jar,
      layout: [], errors: view_errors, extra_panels: Object.create(null), writable: false, write_token_required: false,
    };
    for (const e of m.errors || []) view.errors[e.source || "manifest"] = e.message;

    // a panel is unreadable when its payload is missing, a fetch failure, or the kernel said `error`
    const failure = (pid) => {
      const p = P.get(pid), h = heads.get(pid);
      if (p && p.error !== undefined && p.status === undefined) return p.error;      // our own fetch failure record
      if (!p || typeof p !== "object") return "no payload";
      if (p.status === "error") return p.error || "unavailable";
      return null;
    };
    const unavailable = (pid, zone, why) => {
      const pl = P.get(pid);
      const h = pl && pl.status === "error" ? pl : (heads.get(pid) || {});   // a real error payload's own last-good time wins
      view.layout.push({ kind: "external", zone: zone, edit_class: "", title: titleOf(h.title, pid), id: pid });
      view.extra_panels[pid] = { error: why + " — last good " + (h.as_of ? ageLabel(h.as_of, now, "") : "never") };
    };

    const wrapsFail = heads.has("wraps") ? failure("wraps") : null;
    let wrapsData = !heads.has("wraps") ? [] : wrapsFail ? "unavailable" : (P.get("wraps").value || {}).data;
    let wrapsShape = null;
    if (!wrapsFail && heads.has("wraps")) {
      try { checkWraps(wrapsData); } catch (e) { wrapsShape = e; wrapsData = "unavailable"; view.errors.wraps = e.message; }
    }
    if (wrapsFail) view.errors.wraps = wrapsFail;

    // header region: state goes under the masthead; any other header panel (a downstream's weather, say)
    // is drawn as a panel first in Operate, never silently dropped. An older kernel's `focus` panel is
    // retired data, not a fault: it is skipped.
    const headerExtra = [];
    for (const pid of (m.regions.header || [])) {
      const head = heads.get(pid);
      if (!head) { view.errors[pid] = "listed in the manifest header but has no head"; continue; }
      const bad = failure(pid);
      if (pid === "focus") continue;
      if (pid === "state") {
        if (bad) { view.errors[pid] = bad; continue; }
        view.state = lineOf(P.get(pid), P.get(pid), now);
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
      for (const g of panel.groups || head.groups || []) groupTitles.set(g.id, titleOf(g.title, String(g.id)));
      const entry = { zone: zoneId, edit_class: "", title: titleOf(panel.title, titleOf(head.title, pid)), id: pid };
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
        view.episodes = rows.map((r) => ({ id: bare(r.id), timestamp: (r.facets || {}).at, type: (r.facets || {}).episode_type,
          source: (r.facets || {}).source, tags: tagsOf((r.facets || {}).tags), content: r.body || r.title }));
        view.layout.push(Object.assign(entry, { kind: "episodes" }));
      } else if (pid === "edits" && panel.kind === "triage-list") {
        view.recent_edits = rows.map((r) => ({ id: bare(r.id), ts: (r.facets || {}).at, action: (r.facets || {}).edit_kind,
          source: r.title, undoable: (r.facets || {}).undoable }));
        view.layout.push(Object.assign(entry, { kind: "edits" }));
      } else if (pid === "crystals" && panel.kind === "triage-list") {
        view.crystal_index = rows.map((r) => {
          const f = r.facets || {};
          const tags = tagsOf(f.tags);
          return { name: bare(r.id), level: f.crystal_level, one_clause: r.body || r.title, permanence: f.permanence,
                   last_activated_on: f.last_activated_on, tags: tags };
        });
        view.layout.push(Object.assign(entry, { kind: "crystals" }));
      } else if (pid === "health" && panel.kind === "metric") {
        view.health = healthOf((panel.value || {}).metrics, wrapsData);
        view.layout.push(Object.assign(entry, { kind: "health" }));
      } else if (pid === "graph" && panel.kind === "visual") {
        view.graph = (panel.value || {}).data || null;
        view.layout.push(Object.assign(entry, { kind: "graph" }));
      } else if (pid === "wraps" && panel.kind === "visual") {
        if (wrapsShape) throw wrapsShape;
        view.wraps = Array.isArray(wrapsData) ? wrapsData : [];
        view.layout.push(Object.assign(entry, { kind: "wraps" }));
      } else if (panel.kind === "prose" && pid.indexOf("section:") === 0 && (panel.value || {}).markdown != null) {
        view.sections.push({ heading: titleOf(panel.title, titleOf(head.title, pid)), body: panel.value.markdown });
        view.layout.push(Object.assign(entry, { kind: "section", ref: sectionRef++, heading: titleOf(panel.title, titleOf(head.title, pid)) }));
      } else {
        view.extra_panels[pid] = externalOf(panel, panel, groupTitles);
        view.layout.push(Object.assign(entry, { kind: "external" }));
      }
    };

    const placed = new Set();
    const placeOnce = (pid, zoneId) => {
      if (placed.has(pid)) return;
      placed.add(pid);
      const mark = view.layout.length, nSections = view.sections.length, refMark = sectionRef;
      try { place(pid, zoneId); } catch (e) {
        // one malformed panel must not blank the cockpit: it becomes an unavailable panel with the reason
        view.layout.length = mark;
        view.sections.length = nSections;
        sectionRef = refMark;
        delete view.extra_panels[pid];
        unavailable(pid, zoneId, "could not be mapped (" + (e && e.message ? e.message : e) + ")");
      }
    };
    for (const pid of headerExtra) placeOnce(pid, "operate");
    for (const zone of (m.regions.zones || [])) for (const pid of (zone.panels || [])) placeOnce(pid, zone.id);
    return view;
  }

  const api = { fromManifest, ageLabel, badgeText, visible, clean };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainCockpitView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
