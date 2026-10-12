// levain cockpit_view.js — the manifest as the dashboard's data source (cockpit design §7, R1).
//
// The dashboard core (dashboard_core.js) draws a SubstrateView. This file builds that same shape from
// the kernel's manifest and panel payloads, so the page keeps its look, layout and components and only
// the DATA SOURCE changes. It is a mapper and nothing else: panels, rows and groups come out in the
// order the kernel sent; nothing is sorted, regrouped, counted or given a gesture here. A panel that
// is `error` (or whose fetch failed) never reaches a native component, where an empty array would read
// "nothing waiting": it becomes an explicit unavailable panel carrying the reason and last-good time.
// Anything the native components do not know (the `now` view, a downstream's own panels, extra header
// panels) is drawn by the core's generic external panel. The mapper also carries what a write needs
// (each row's panel, full id, version and offered actions; the panels' own actions; the verb heads and
// the transport flags) and decides nothing about a tier or a gesture: whoever writes asks the kernel.
(function (root) {
  "use strict";

  // The kernel's own built-in panel ids and the native component each feeds. A panel with any other
  // id (or kind) is a downstream's and takes the generic path. A Map: ids come from the wire, and an
  // id like "constructor" or "__proto__" must never reach an object's prototype.
  const NATIVE = new Map([["tray", "tray"], ["loops", "spores"], ["keep", "keep"], ["episodes", "episodes"],
    ["edits", "edits"], ["health", "health"], ["graph", "graph"], ["crystals", "crystals"], ["wraps", "wraps"]]);
  // The /1 kind names the kernel normalises to the /2 ones; a payload from an older kernel may still carry them.
  const ALIAS = { "triage-list": "list", metric: "status-dots", visual: "ambient", prose: "brief" };
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

  // case- and accent-folded text: the one comparison the filter, the jump box and the kernel's rows scope share
  function fold(x) { return String(x === null || x === undefined ? "" : x).normalize("NFD").replace(/\p{M}/gu, "").toLowerCase(); }
  function termsOf(q) { return fold(q).split(/\s+/).filter(Boolean); }
  function hits(hay, terms) { return terms.every((t) => hay.indexOf(t) >= 0); }

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

  // a row's age as its kernel badge words it ("12d old"), else empty
  function ageOf(row) {
    for (const b of row.badges || []) if (b && b.kind === "age_days") return badgeText(b);
    return "";
  }

  // the write target a row carries: the panel it was read on, its full id (the source-scoped one), its
  // version and the actions the kernel offers on it. Nothing here is chosen by the page.
  function targetOf(pid, row) {
    return { panel_id: pid, row_id: row.id, row_version: row.version, actions: Array.isArray(row.actions) ? row.actions : [] };
  }

  // the kind options a resolve action carries (the kernel's taxonomy for this row's type); none when the
  // row does not offer the action
  function kindsOf(actions, verb) {
    for (const a of actions) {
      if (a && a.verb === verb && isObj(a.options) && Array.isArray(a.options.spore_kind)) {
        return a.options.spore_kind.filter((k) => typeof k === "string");
      }
    }
    return [];
  }

  const HIDDEN_TEXT = "contains hidden characters; edit with the CLI";
  function sporeOf(pid, row, groupTitles, hidden) {
    const f = row.facets || {};
    const t = targetOf(pid, row);
    return Object.assign({
      id: bare(row.id), age: ageOf(row), type: f.spore_type || "task", tier: f.tier, salience: f.salience, domain: f.domain,
      disposition: f.disposition, text: row.body || row.title, next: f.due || null,
      descend_kinds: kindsOf(t.actions, "spore_descend"), ascend_kinds: kindsOf(t.actions, "spore_ascend"),
      group_title: row.group ? (groupTitles.get(row.group) || row.group) : null, group: row.group || null,
    }, t, hidden && hidden.has(row.id) ? { text_locked: HIDDEN_TEXT } : {});
  }

  // a stale or partial panel keeps rendering its rows, under a banner saying so
  function bannerOf(head, panel, now) {
    const out = [];
    if (head.status === "stale") out.push({ tone: "stale", text: "STALE: last read " + (ageLabel(head.as_of, now, "") || "unknown") });
    if (head.status === "partial") for (const s of head.skipped || []) out.push({ tone: "stale", text: "PARTIAL: " + s.count + " unreadable — " + s.reason });
    for (const f of head.filtered || []) out.push({ tone: "dim", text: f.count + " held back — " + f.reason });
    if (head.note) out.push({ tone: "dim", text: head.note });
    // a Metric's alerts (e.g. a wrap in progress) are part of its reading, never dropped
    for (const a of ((panel.value || {}).alerts || [])) out.push({ tone: "stale", alert: true, text: "! " + a.message });
    return out.length ? out : null;
  }

  // The text a search matches for one row: only the panel's declared search fields, folded once here, with
  // the kernel's own rules (levain/cockpit/engine.py `_field_text` for a row panel, a plain field read for a
  // roster or log row): title, body and id are the row's own, any other name is a facet whose list is joined.
  const ROW_FIELDS = ["title", "body", "id"];
  const textOf = (v) => v === null || v === undefined ? "" : Array.isArray(v) ? v.map(textOf).join(" ")
    : typeof v === "string" || isNum(v) || typeof v === "boolean" ? String(v) : "";
  const rowText = (r, f) => f === "title" || f === "body" || f === "id" ? (typeof r[f] === "string" ? r[f] : "") : textOf((r.facets || {})[f]);
  const valueText = (r, f) => r[f] ? textOf(r[f]) : "";
  const hayOfRow = (r, fields) => fold(fields.map((f) => rowText(r, f)).join(" "));
  const hayOfValue = (r, fields) => fold(fields.map((f) => valueText(r, f)).join(" "));
  const fieldsOf = (sr, dflt) => sr.fields.length ? sr.fields : dflt;

  function searchOf(panel) {
    const s = isObj(panel.search) ? panel.search : {};
    return {
      on: panel.searchable === true, fields: Array.isArray(s.fields) ? s.fields.filter((f) => typeof f === "string") : [],
      scope: s.scope === "store" ? "store" : "rows", hook: s.hook === true,
      window: isNum(panel.window) ? panel.window : null,
      visible: Number.isInteger(s.default_visible) && s.default_visible > 0 ? s.default_visible : 10,
      matchLabel: typeof panel.match_label === "string" ? panel.match_label : "",
      error: typeof panel.search_error === "string" ? panel.search_error : "",
      matched: isNum(panel.matched) ? panel.matched : null,
    };
  }

  // a list panel's compose card as the manifest declares it: {verb, fields} of the form kind's own field types.
  // A field type the page does not know refuses the panel visibly (the caller's need() throws), never drops it.
  const COMPOSE_TYPES = ["text", "recipient", "date"];
  function composeOf(c) {
    if (c === undefined || c === null) return null;
    need(isObj(c) && typeof c.verb === "string" && c.verb && Array.isArray(c.fields) && c.fields.length, "compose");
    const names = new Set();
    const fields = c.fields.map((f) => {
      need(isObj(f) && typeof f.name === "string" && f.name && !names.has(f.name), "compose field name");
      names.add(f.name);
      need(COMPOSE_TYPES.indexOf(f.type) >= 0, "compose field " + f.name + " has a type this page cannot draw (" + String(f.type) + ")");
      if (f.type === "recipient") {
        // the send trims a chosen id: a blank one would send no reader (everyone), and two ids equal after the
        // trim are one reader drawn twice
        need(Array.isArray(f.enum) && f.enum.length && f.enum.every((x) => typeof x === "string" && x.trim()), "compose recipient list (a blank id)");
        need(new Set(f.enum.map((x) => x.trim())).size === f.enum.length, "compose recipient list (an id repeated)");
      }
      return f;
    });
    // one address line, one chip row
    need(fields.filter((f) => f.type === "recipient").length <= 1, "compose: one recipient field at most");
    return { verb: c.verb, fields: fields };
  }

  function externalOf(head, panel, groupTitles, nowMs) {
    const v = panel.value || {};
    const kind = ALIAS[head.kind] || head.kind;
    const out = { note: head.note || "", empty: head.empty || "nothing here", kind: kind, pid: head.id, typed: false,
                  search: searchOf(panel), face: panel.face === "count" ? "count" : "" };
    if (kind === "list" || kind === "count-queue") {
      const compose = composeOf(panel.compose);
      // an addressed list draws every row as `source -> for`. Its address is the compose's recipient field when
      // the panel declares a compose; a read-only serve declares none (no send verb), but the rows still carry the
      // `for` facet, so the facet alone addresses them: the address line never depends on the write side.
      const rfield = compose ? compose.fields.find((f) => f.type === "recipient") : null;
      const forName = rfield ? rfield.name : "for";
      const addressed = !!rfield || (panel.rows || []).some((r) => isObj(r.facets) && Object.prototype.hasOwnProperty.call(r.facets, "for"));
      if (compose) out.compose = compose;
      out.lines = (panel.rows || []).map((r) => {
        const grp = r.group ? (groupTitles.get(r.group) || r.group) : "";
        const badges = (r.badges || []).map(badgeText).join(" · ");
        const prov = r.provenance ? r.provenance.label : "";
        const legacy = (r.facets || {}).legacy_meta;   // an adapted extra panel's own meta line (design §6.2)
        const meta = [typeof legacy === "string" ? legacy : "", r.panel_id ? r.panel_id : "", grp, badges, prov]
          .filter(Boolean).join(" · ");
        const line = { meta: meta, text: r.body || r.title, accent: r.emphasis === "accent", dim: r.emphasis === "dim" };
        if (addressed) {
          const f = r.facets || {};
          // the kernel refuses an addressed compose whose recipient facet is not declared, so a row with no
          // value for it names no reader: everyone
          const fv = f[forName];
          const to = Array.isArray(fv) ? fv : typeof fv === "string" ? fv.split(",") : [];
          line.addr = { source: typeof f.source === "string" && f.source.trim() ? f.source.trim() : "unknown",
                        to: to.filter((x) => typeof x === "string" && x.trim()).map((x) => x.trim()),
                        age: typeof f.at === "string" ? ageLabel(f.at, nowMs || Date.now(), "") : "" };
          // the meta (badges, provenance, group) stays: the renderer draws it after the address line
        }
        // a pending (or its outcome) names its state; a WAITING one can be cancelled from here
        if (typeof r.id === "string" && r.id.indexOf("pend:") === 0) {
          const f = r.facets || {};
          const stateWord = typeof f.state === "string" && f.state ? f.state : (r.group ? (groupTitles.get(r.group) || r.group) : "");
          if (stateWord && line.meta.indexOf(stateWord) < 0) line.meta = [stateWord, line.meta].filter(Boolean).join(" · ");
          const c = (Array.isArray(r.actions) ? r.actions : []).find((a) => a && a.verb === "pending_cancel");
          if (c && isObj(c.target) && typeof c.target.pending_id === "string") {
            line.cancel = { verb: "pending_cancel", tier: c.tier, gesture: c.gesture, pending_id: c.target.pending_id,
                            route: typeof c.route === "string" ? c.route : "" };
          }
        }
        return line;
      });
      const fields = fieldsOf(out.search, ROW_FIELDS);
      out.hays = (panel.rows || []).map((r) => hayOfRow(r, fields));
      out.typed = true;
      out.count = isNum(panel.count) ? panel.count : null;
      if (panel.degraded && panel.degraded.length) {
        const why = "not complete: " + (panel.error || panel.degraded.join(", "));
        out.note = out.note ? out.note + " · " + why : why;   // added to the source's note, never in place of it
      }
    } else if (head.kind === "line") {
      out.lines = labelled(v.lines, "lines").map((l) => ({ meta: l.label, text: l.text }));
    } else if (kind === "status-dots") {
      const ms = v.metrics === undefined ? [] : v.metrics;
      // a dot's value is optional: a status with no figure is a legitimate reading
      need(Array.isArray(ms) && ms.every((m) => isObj(m) && typeof m.label === "string"
        && (m.value == null || typeof m.value === "string" || isNum(m.value)) && (m.unit == null || typeof m.unit === "string")
        && (m.read == null || typeof m.read === "string")), "metrics");
      out.metrics = ms;
      out.typed = true;
      out.lines = ms.map((m) => ({
        meta: m.label,
        text: [m.value == null ? "" : m.value + (m.unit ? " " + m.unit : ""), m.read || ""].filter(Boolean).join(" — ") || (m.status || "unknown"),
        accent: m.status === "warn" || m.status === "bad", dim: m.status === "unknown" }));
    } else if (kind === "ambient") {
      if (Array.isArray(v.events)) {
        need(v.events.every((e) => isObj(e) && typeof e.kind === "string" && typeof e.at === "string"), "events");
        need(typeof v.legend === "string", "legend");
        out.sky = { events: v.events, legend: v.legend };
        out.typed = true;
      } else out.lines = labelled(v.text, "text").map((l) => ({ meta: l.label, text: l.text }));
    } else if (kind === "brief") {
      const lines = labelled(v.lines, "lines");
      const head1 = typeof v.headline === "string" && v.headline ? v.headline : "";
      if (typeof v.markdown === "string" && v.markdown) out.markdown = (head1 ? "**" + head1 + "**\n\n" : "") + v.markdown;
      else { out.typed = true; out.brief = { headline: head1, lines: lines }; }
    } else if (kind === "reading") {
      need(isNum(v.value) || (typeof v.value === "string" && v.value), "reading value");
      need(v.sub === undefined || (Array.isArray(v.sub) && v.sub.every((s) => isObj(s) && typeof s.label === "string")), "reading sub");
      out.reading = v;
      out.typed = true;
    } else if (kind === "metric-grid") {
      need(Array.isArray(v.numbers) && v.numbers.every((n) => isObj(n) && typeof n.label === "string" && isNum(n.value)), "numbers");
      out.numbers = v.numbers;
      out.typed = true;
    } else if (kind === "roster") {
      need(Array.isArray(v.rows) && v.rows.every((r) => isObj(r) && typeof r.name === "string"), "roster rows");
      const rows = v.rows.filter((r) => r.lifecycle !== "retired");
      out.roster = rows;
      const fields = fieldsOf(out.search, ["name", "role", "group"]);
      out.hays = rows.map((r) => hayOfValue(r, fields));
      out.retiredHidden = isNum(v.retired_hidden) ? v.retired_hidden : 0;
      out.typed = true;
    } else if (kind === "log") {
      need(Array.isArray(v.rows) && v.rows.every((r) => isObj(r) && typeof r.verb === "string"), "log rows");
      out.logRows = v.rows;
      const fields = fieldsOf(out.search, ["verb", "target", "actor"]);
      out.hays = v.rows.map((r) => hayOfValue(r, fields));
      out.typed = true;
    } else if (kind === "form") {
      need(Array.isArray(v.fields) && v.fields.length && v.fields.every((f) => isObj(f) && typeof f.name === "string"), "form fields");
      out.form = { fields: v.fields, verb: typeof v.verb === "string" ? v.verb : "" };
      out.typed = true;
    }
    return out;
  }

  // PARSE, DON'T VALIDATE. Each panel kind has ONE shape check at this boundary. A payload that fails it
  // throws inside its own mapping, and placeOnce() turns that into an unavailable panel while every other
  // panel still renders. Nothing here repairs a bad field into a value that reads as data: a default such
  // as 0 or "" is a reading the renderer would print as true.
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  // an action a browser can perform: refused and laptop-only gestures have nothing to post
  const isPostable = (a) => isObj(a) && typeof a.verb === "string" && a.gesture !== "refused" && a.gesture !== "laptop-only";
  const isNum = (x) => typeof x === "number" && Number.isFinite(x);
  function need(ok, what) { if (!ok) throw new Error("malformed " + what); }
  // a line list (line and visual kinds): absent is empty; each line is an object whose text is a string
  function labelled(x, what) {
    if (x === undefined || x === null) return [];
    need(Array.isArray(x) && x.every((l) => isObj(l) && typeof l.text === "string" && (l.label == null || typeof l.label === "string")), what);
    return x;
  }
  // a tag list: the legacy form is ONE comma-joined string, which is split; an array's elements are whole tags
  function tagsOf(t) {
    if (t === undefined || t === null) return [];
    if (typeof t === "string") return t.split(",").map((s) => s.trim()).filter(Boolean);
    need(Array.isArray(t) && t.every((s) => typeof s === "string"), "tags");
    return t;
  }

  // Which of the three modes show a panel. Morning is the intake read (what is waiting, the state, the
  // sky); Build drops the readings that are only for orientation; Board is everything. The hero sky is in all.
  const MORNING_KINDS = new Set(["brief", "reading", "count-queue", "ambient", "status-dots", "section"]);
  const BUILD_DROPS = new Set(["brief", "reading", "section"]);
  function modesOf(e) {
    const k = e.pkind || e.kind;
    if (k === "ambient") return "morning board build";
    const out = [];
    if (MORNING_KINDS.has(k) || e.kind === "tray" || e.kind === "section") out.push("morning");
    out.push("board");
    if (!BUILD_DROPS.has(k) && e.kind !== "section") out.push("build");
    return out.join(" ");
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
    // every figure is required: the kernel always sends each one, and an absent figure would print as 0
    for (const k of HEALTH_FIGURES) need(m.has(k) && isNum(m.get(k)), "metric " + k);
    need(m.get("write path") === "live" || m.get("write path") === "dark", "metric write path");
    const n = (k) => m.get(k);
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
    // the state line is the operator's own words: a malformed panel must never read as "no state set"
    // The kernel drops an expired line and sends the stamp as toISOString would, so a stamp that does not
    // round-trip through Date is malformed (outside that format Date.parse is engine-specific). Time is not
    // judged here beyond that: a stamp ahead of this clock (a browser clock behind the server's) shows its
    // words with the age unknown, never hidden.
    if (panel.value === null) return { text: null };    // the kernel's empty or compact read
    need(isObj(panel.value), "state value");
    const l = labelled(panel.value.lines, "state lines")[0];
    if (!l) return { text: null };
    need(typeof l.at === "string" && (l.source == null || typeof l.source === "string"), "state line");
    const t = Date.parse(l.at);
    need(!isNaN(t) && new Date(t).toISOString() === l.at, "state line stamp");
    const known = t <= now + 60000;   // a minute of slack before an age is called unknown rather than "just now"
    return { text: l.text, set_at: l.at, source: l.source, age_label: known ? ageLabel(l.at, now, "set ") : "" };
  }

  // a compose field from a name alone: free text for a message-like name, a comma list for `for`
  const LONG_TEXT = new Set(["message", "body", "text", "content", "note"]);
  function composeField(name) {
    if (name === "for") return { name: name, label: "Readers (comma list; none = broadcast)", kind: "csv", required: false };
    const long = LONG_TEXT.has(name);
    return { name: name, label: name, kind: long ? "textarea" : "text", required: long };
  }

  // one form field of the kind contract as the compose box's widget: a recipient is a fixed-set chooser
  function formField(f) {
    const label = typeof f.label === "string" && f.label ? f.label : f.name;
    if (f.type === "recipient") {
      return { name: f.name, label: label, kind: "multiselect", chips: true, required: false,
               options: Array.isArray(f.enum) ? f.enum.filter((x) => typeof x === "string") : [] };
    }
    const long = f.type === "text" && LONG_TEXT.has(f.name);
    return { name: f.name, label: f.type === "date" ? label + " (YYYY-MM-DD)" : label, kind: long ? "textarea" : "text", required: long, placeholder: long ? label : undefined };
  }

  // The last manifest the page was drawn from and when it was fetched: a single panel's refetch maps its fresh
  // payload through the same mapper, against this manifest, so it draws exactly what a full load would.
  let lastManifest = null;
  let manifestAt = 0;

  // snap = {manifest, panels}; returns the SubstrateView-shaped object dashboard_core.js renders.
  // `snap.panels` maps a panel id to its payload, or to {error} when its fetch failed.
  function fromManifest(raw, opts) {
    lastManifest = raw.manifest;
    manifestAt = Date.now() - Math.max(0, (opts && opts.elapsedMs) || 0);
    return mapSnapshot(raw, opts);
  }
  function mapSnapshot(raw, opts) {
    const elapsed = Math.max(0, (opts && opts.elapsedMs) || 0);   // time since the manifest was fetched, measured by the caller
    // The manifest and each panel are cleaned apart: a hostile key inside ONE panel's payload (two keys that
    // clean to one) rejects that panel alone; a collision in the manifest or between two panel ids is
    // refused whole, since the references between them cannot then be told apart.
    const snap = { manifest: clean(raw.manifest), panels: Object.create(null) };
    // the rows whose text the sanitiser would change: known here, while the raw string is still in hand
    const hiddenRows = new Set();
    for (const key of Object.keys(raw.panels || {})) {
      const rp = raw.panels[key];
      for (const r of (isObj(rp) && Array.isArray(rp.rows) ? rp.rows : [])) {
        if (!isObj(r) || typeof r.id !== "string") continue;
        if ([r.body, r.title].some((t) => typeof t === "string" && visible(t) !== t)) hiddenRows.add(visible(r.id));
      }
    }
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
      // the write surface: the transport's own flags, the verb heads, and every target a write names. A
      // page without `transport` (an older server) is read-only, never writable by default.
      verb_heads: isObj(m.verbs) ? m.verbs : Object.create(null), action_verbs: Object.create(null),
      targets: { rows: Object.create(null), panels: Object.create(null) },
    };
    if (isObj(m.transport)) {
      view.writable = m.transport.writable === true;
      view.write_token_required = m.transport.write_token_required === true;
    }
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
    // retired data, not a fault: it is skipped. A manifest that declares hero / brief / widgets is KE-shaped:
    // those regions are drawn from their own lists and `header` (their derived union) is not read.
    const ke = Array.isArray(m.regions.hero) && Array.isArray(m.regions.brief) && Array.isArray(m.regions.widgets);
    const headerExtra = [];
    const mapState = (pid, head) => {
      const bad = failure(pid);
      if (bad) { view.errors[pid] = bad; return; }
      try {
        view.state = lineOf(P.get(pid), P.get(pid), now);
        // the version the line was rendered with, which a write to it names (the kernel refuses a changed line)
        const sp = P.get(pid);
        const acts = Array.isArray(sp.actions) ? sp.actions.filter((a) => isObj(a) && typeof a.verb === "string") : [];
        const sv = isObj(sp.value) && typeof sp.value.value_version === "string" ? sp.value.value_version : null;
        if (acts.length) view.targets.panels[pid] = { actions: acts, panel_version: sv };
        if (view.state && view.writable) {
          view.state.version = sv;
          view.state.can_edit = acts.some((a) => a.verb === "operator_state");
        }
      } catch (e) { view.state = null; view.errors[pid] = String(e && e.message || e); }
    };
    for (const pid of (ke ? [] : m.regions.header || [])) {
      const head = heads.get(pid);
      if (!head) { view.errors[pid] = "listed in the manifest header but has no head"; continue; }
      if (pid === "focus") continue;
      if (pid === "state") mapState(pid, head); else headerExtra.push(pid);
    }

    let sectionRef = 0;
    // `region` is set for a hero / brief / widgets panel: those are drawn bare by the page, always through the
    // typed external path, never as one of the kernel's own built-in panels (a widget named `health` is not the Health panel)
    const place = (pid, zoneId, region) => {
      const nat = region ? "" : pid;
      const head = heads.get(pid);
      if (!head) { view.errors[pid] = "listed in the manifest but has no head"; return; }
      const bad = failure(pid);
      if (bad) { unavailable(pid, zoneId, bad); return; }
      const panel = P.get(pid);
      const groupTitles = new Map();
      for (const g of panel.groups || head.groups || []) groupTitles.set(g.id, titleOf(g.title, String(g.id)));
      const entry = { zone: zoneId, edit_class: "", title: titleOf(panel.title, titleOf(head.title, pid)), id: pid,
                      pkind: ALIAS[panel.kind] || panel.kind };
      if (region) entry.region = region;
      const banner = bannerOf(panel, panel, now);
      if (banner) entry.banner = banner;
      if (panel.as_of) entry.fresh = ageLabel(panel.as_of, now, "read ");
      const rows = panel.rows || [];
      const kind = NATIVE.get(nat);
      // what this panel lets the operator do: its own actions (a create, a value write, a downstream verb)
      // and the actions its rows carry. The page offers a verb only where the kernel offered one.
      const pActions = Array.isArray(panel.actions) ? panel.actions.filter((a) => isObj(a) && typeof a.verb === "string") : [];
      const rowsAct = rows.some((r) => r && Array.isArray(r.actions) && r.actions.length);
      if (pActions.length) {
        const pv = isObj(panel.value) && typeof panel.value.value_version === "string" ? panel.value.value_version : null;
        view.targets.panels[pid] = { actions: pActions, panel_version: pv };
      }
      for (const r of rows) if (r && typeof r.id === "string" && Array.isArray(r.actions) && r.actions.length) {
        view.targets.rows[r.id] = targetOf(pid, r);
      }
      // every capture is the Tray's create, so the Keep's capture box shows whenever the Tray offers one
      const tp = P.get("tray");
      const traySeeds = isObj(tp) && Array.isArray(tp.actions) && tp.actions.some((a) => isObj(a) && a.verb === "spore_seed" && isPostable(a));
      if ((rowsAct || pActions.length || (nat === "keep" && traySeeds)) && (nat === "tray" || nat === "loops" || nat === "keep" || nat === "episodes")) entry.edit_class = "B";
      const lists = { tray: "tray", loops: "open_spores", keep: "keep" };
      if ((nat === "tray" || nat === "loops" || nat === "keep") && (panel.kind === "triage-list" || panel.kind === "list")) {
        view[lists[pid]] = rows.map((r) => sporeOf(pid, r, groupTitles, hiddenRows));
        if (nat === "tray" && Array.isArray(head.views) && isObj(panel.view_rows)) {
          // the kernel's named orderings and the row ids in each; the renderer picks one, never sorts
          const named = head.views.filter((v) => isObj(v) && typeof v.id === "string" && Array.isArray(panel.view_rows[v.id]));
          if (named.length) {
            view.tray_views = named.map((v) => ({ id: v.id, title: titleOf(v.title, v.id) }));
            view.tray_view_rows = Object.create(null);
            for (const v of named) view.tray_view_rows[v.id] = panel.view_rows[v.id].map(bare);
          }
        }
        view.layout.push(Object.assign(entry, { kind: kind }));
      } else if (nat === "episodes" && (panel.kind === "triage-list" || panel.kind === "list")) {
        view.episodes = rows.map((r) => ({ panel_id: pid, row_id: r.id, row_version: r.version, id: bare(r.id), timestamp: (r.facets || {}).at, type: (r.facets || {}).episode_type,
          source: (r.facets || {}).source, agent: titleOf((r.facets || {}).agent, ""),
          actions: Array.isArray(r.actions) ? r.actions : undefined, tags: tagsOf((r.facets || {}).tags), content: r.body || r.title,
          emphasis: r.emphasis }));
        view.layout.push(Object.assign(entry, { kind: "episodes" }));
      } else if (nat === "edits" && (panel.kind === "triage-list" || panel.kind === "list")) {
        view.recent_edits = rows.map((r) => ({ panel_id: pid, row_id: r.id, row_version: r.version,
          actions: Array.isArray(r.actions) ? r.actions : [], id: bare(r.id), ts: (r.facets || {}).at, action: (r.facets || {}).edit_kind,
          source: r.title, undoable: (r.facets || {}).undoable }));
        view.layout.push(Object.assign(entry, { kind: "edits" }));
      } else if (nat === "crystals" && (panel.kind === "triage-list" || panel.kind === "list")) {
        view.crystal_index = rows.map((r) => {
          const f = r.facets || {};
          const tags = tagsOf(f.tags);
          return { name: bare(r.id), level: f.crystal_level, one_clause: r.body || r.title, permanence: f.permanence,
                   last_activated_on: f.last_activated_on, tags: tags };
        });
        view.layout.push(Object.assign(entry, { kind: "crystals" }));
      } else if (nat === "health" && (panel.kind === "metric" || panel.kind === "status-dots")) {
        view.health = healthOf((panel.value || {}).metrics, wrapsData);
        view.layout.push(Object.assign(entry, { kind: "health" }));
      } else if (nat === "graph" && (panel.kind === "visual" || panel.kind === "ambient")) {
        view.graph = (panel.value || {}).data || null;
        view.layout.push(Object.assign(entry, { kind: "graph" }));
      } else if (nat === "wraps" && (panel.kind === "visual" || panel.kind === "ambient")) {
        if (wrapsShape) throw wrapsShape;
        view.wraps = Array.isArray(wrapsData) ? wrapsData : [];
        view.layout.push(Object.assign(entry, { kind: "wraps" }));
      } else if ((panel.kind === "prose" || panel.kind === "brief") && nat.indexOf("section:") === 0 && (panel.value || {}).markdown != null) {
        view.sections.push({ heading: titleOf(panel.title, titleOf(head.title, pid)), body: panel.value.markdown });
        // a section the kernel offers section_edit on is the operator's to edit; its panel id stands as the
        // write address, and the save posts the action the panel offered
        if (view.writable && pActions.some((a) => a.verb === "section_edit" && isPostable(a))) { entry.edit_class = "A"; entry.source = pid; }
        view.layout.push(Object.assign(entry, { kind: "section", ref: sectionRef++, heading: titleOf(panel.title, titleOf(head.title, pid)) }));
      } else {
        view.extra_panels[pid] = externalOf(panel, panel, groupTitles, now);
        view.extra_panels[pid].pid = pid;
        // a health widget is drawn as a widget, never as the Health panel, so the page's liveness reads its own write-path metric
        const wp = (view.extra_panels[pid].metrics || []).find((x) => x.label === "write path");
        if (region && wp && (wp.value === "live" || wp.value === "dark")) view.write_path_live = wp.value === "live";
        // a downstream's own verb on this panel becomes the compose box. The manifest names the verb's
        // fields and not their widgets, so a widget is chosen from the field's name (a known edge).
        // a form panel names its verb: the action is the one offered under that name, and no other
        const formVerb = view.extra_panels[pid].form ? view.extra_panels[pid].form.verb : (view.extra_panels[pid].compose ? view.extra_panels[pid].compose.verb : null);
        const own = pActions.find((a) => (!isObj(a.target) || typeof a.target.row_id !== "string") && (formVerb === null || a.verb === formVerb));
        const vh = own && isObj(view.verb_heads[own.verb]) ? view.verb_heads[own.verb] : null;
        const cmpDecl = view.extra_panels[pid].compose;
        if (cmpDecl && view.writable && !NATIVE.has(pid)) {
          // a writable surface draws the card or says why not, never a silently missing card (a read-only one
          // draws no verbs at all, so it stays silent)
          if (!vh) throw new Error("its compose verb " + cmpDecl.verb + " is not offered by this panel");
          // a job's result arrives by polling, which the card does not do
          if (vh.job === true) throw new Error("its compose verb " + cmpDecl.verb + " is a job verb, which a compose card cannot send");
        }
        if (vh && !NATIVE.has(pid)) {
          // the action names its own panel: the send posts to THIS panel's offer, never another panel's
          view.extra_panels[pid].action = { verb: own.verb, panel: pid, fields: (Array.isArray(vh.fields) ? vh.fields : []).filter((n) => typeof n === "string").map(composeField) };
          view.action_verbs[own.verb] = { label: vh.label, confirm_required: vh.confirm === true, idempotent: vh.idempotent === true, job: false };
          // a form panel names its own fields: they win over the widgets guessed from the verb head's names
          const form = view.extra_panels[pid].form;
          if (form && form.verb === own.verb) view.extra_panels[pid].action.fields = form.fields.map(formField);
          // a list's own compose card: its typed fields win, and the card is drawn on the list itself
          const cmp = view.extra_panels[pid].compose;
          if (cmp && cmp.verb === own.verb) { view.extra_panels[pid].action.fields = cmp.fields.map(formField); view.extra_panels[pid].action.card = true; }
        }
        // the external renderer draws the note itself, so the banner must not draw it a second time
        if (entry.banner && panel.note) {
          entry.banner = entry.banner.filter((b) => !(b.tone === "dim" && b.text === panel.note));
          if (!entry.banner.length) delete entry.banner;
        }
        view.layout.push(Object.assign(entry, { kind: "external" }));
      }
    };

    const placed = new Set();
    const placeOnce = (pid, zoneId, region) => {
      if (placed.has(pid)) return;
      placed.add(pid);
      const mark = view.layout.length, nSections = view.sections.length, refMark = sectionRef;
      try { place(pid, zoneId, region); } catch (e) {
        // one malformed panel must not blank the cockpit: it becomes an unavailable panel with the reason
        view.layout.length = mark;
        view.sections.length = nSections;
        sectionRef = refMark;
        delete view.extra_panels[pid];
        unavailable(pid, zoneId, "could not be mapped (" + (e && e.message ? e.message : e) + ")");
      }
    };
    for (const pid of headerExtra) placeOnce(pid, "operate");
    if (ke) {
      // the page's own regions, each panel kept in the kernel's order: hero (reading, ambient), brief, widgets, then
      // the zones as cards. `state` is not a card: it is the operator's line, drawn at the top of the brief.
      view.ke = { hero: [], brief: [], widgets: [], zones: [] };
      for (const region of ["hero", "brief", "widgets"]) {
        for (const pid of m.regions[region]) {
          if (view.ke[region].indexOf(pid) >= 0 || pid === "focus") continue;   // an older kernel's `focus` is retired data, never drawn
          if (pid === "state" && region === "brief") { if (heads.has(pid)) mapState(pid, heads.get(pid)); else view.errors[pid] = "listed in the manifest brief but has no head"; continue; }
          view.ke[region].push(pid);
          placeOnce(pid, region, region);
        }
      }
    }
    for (const zone of (m.regions.zones || [])) {
      for (const pid of (zone.panels || [])) placeOnce(pid, zone.id);
      if (ke) {
        const cards = Array.isArray(zone.cards) ? zone.cards.filter(isObj) : [];
        view.ke.zones.push({ id: zone.id, title: titleOf(zone.title, zone.id), cards: cards.map((c) => ({
          panels: (Array.isArray(c.panels) ? c.panels : []).filter((x) => typeof x === "string"),
          size: ["tall", "medium", "short"].indexOf(c.size) >= 0 ? c.size : "medium",
          span: c.span === 2 || c.span === 3 ? c.span : 1, tabs: typeof c.tabs === "string" && c.tabs ? c.tabs : "" })) });
      }
    }
    for (const e of view.layout) e.modes = modesOf(e);
    // the verbs the kernel offered somewhere with a gesture a browser can perform: an affordance whose
    // verb is not here has nothing to post to
    view.offered_verbs = Object.create(null);
    const postable = isPostable;
    for (const t of Object.values(view.targets.panels)) for (const a of t.actions) if (postable(a)) view.offered_verbs[a.verb] = true;
    for (const t of Object.values(view.targets.rows)) for (const a of t.actions) if (postable(a)) view.offered_verbs[a.verb] = true;
    return view;
  }

  // ───────────────────────────── the typed panel bodies (cockpit contract v2) ─────────────────────────────
  // One drawing function per kind, a filter box shared by every searchable panel, the mode keys and the jump
  // box. Everything is built with textContent: a wire string never reaches an HTML sink.
  const K = {};
  const states = new Set();             // every live typed panel body (a panel and its expanded copy are two)
  const FILTER_KEY = "levain_cockpit_filter:";
  const MODE_KEY = "levain_cockpit_mode";
  const MODES = ["morning", "board", "build"];
  const SEARCH_DEBOUNCE_MS = 250;
  const STORE_LIMIT = 50;
  let hoverPanel = null;                // the panel the pointer last entered: `/` filters the panel you are looking at
  let focusKeep = null;                 // {pid, start, end}: the filter being typed in, restored after a re-render

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function store() { try { return root.sessionStorage; } catch (_) { return null; } }
  function saved(key) { const s = store(); try { return s ? s.getItem(key) : null; } catch (_) { return null; } }
  function save(key, val) {
    const s = store();
    try { if (s) { if (val) s.setItem(key, val); else s.removeItem(key); } } catch (_) { /* no storage: the query just does not survive */ }
  }
  // the query as the kernel accepts it: plain text, no control characters, bounded
  function sendable(q) { return q.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 200); }
  function ago(iso) { const a = ageLabel(iso, Date.now(), ""); return a || ""; }

  // ---- the filter box: ONE component, every searchable panel ----
  function buildFilter(s) {
    const box = el("div", "xfilter");
    const input = el("input", "xfilter-input");
    input.type = "search";
    input.maxLength = 200;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = s.data && s.data.kind === "roster" ? "filter agents\u2026" : "Filter";
    input.setAttribute("aria-label", "Filter " + s.title);
    input.dataset.pid = s.pid;
    const count = el("span", "xfilter-count");
    count.setAttribute("aria-live", "polite");
    const key = el("kbd", "xfilter-key", "/");
    key.setAttribute("aria-hidden", "true");
    box.append(input, count, key);
    input.value = s.query;
    input.addEventListener("input", () => { s.setQuery(input.value, false); keepFocus(input, s.pid); });
    input.addEventListener("focus", () => keepFocus(input, s.pid));
    input.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      if (input.value) { input.value = ""; s.setQuery("", true); } else { focusKeep = null; input.blur(); }
    });
    s.filterInput = input;
    s.countEl = count;
    return box;
  }
  function keepFocus(input, pid) { focusKeep = { pid: pid, start: input.selectionStart, end: input.selectionEnd }; }

  // ---- row painting by kind ----
  function dotOf(status) { const d = el("span", "dot " + (status || "unknown")); d.setAttribute("aria-hidden", "true"); return d; }

  // The Fleet card (KE4): resident / on call / roundtable, drawn from the kernel's own fleet_group and health.
  // Accent is the kernel's tone "alarm" alone, which only a resident member whose silence is a signal can carry;
  // an on-call member's last call, failed or not, is a neutral fact.
  const FLEET_GROUPS = [["resident", "Resident \u00b7 judged by their writing"], ["on_call", "On call \u00b7 judged by their last answer"],
                        ["roundtable", "Roundtable"]];
  const fleetGroupOf = (r) => r.fleet_group || (r.group === "roundtable" ? "roundtable" : r.cadence === "resident" ? "resident" : "on_call");
  function fleetRow(r) {
    const h = isObj(r.health) ? r.health : { tone: "unknown", text: "no data", at: null, memory: "" };
    const alarm = h.tone === "alarm";
    const row = el("div", "fl rrow");
    row.setAttribute("data-ck-row", "");
    row.setAttribute("tabindex", "0");
    const n = el("div", "n");
    // unknown is its own mark (a dashed ring) so it never reads as an on-call "not called yet" hollow dot, and never as accent
    const dot = el("i", alarm ? "bad" : (h.tone === "unknown" ? "unk" : (h.tone === "neutral" && !h.at ? "off" : "")));
    dot.setAttribute("aria-hidden", "true");
    n.append(dot, document.createTextNode(r.name));
    const when = h.at ? " " + (ago(h.at) || h.at) : "";
    const d = el("div", "d" + (alarm ? " bad" : "") + (h.tone === "unknown" ? " unk" : ""), (h.text || "") + when);
    row.append(n, d);
    // a resident's tags say only "memory" (mock-e); the store's size is an on-call fact. "stateless" stays as the kernel says it.
    const mem = fleetGroupOf(r) === "resident" && /^memory\b/.test(h.memory || "") ? "memory" : h.memory;
    const tags = [r.role, mem].filter(Boolean).join(" \u00b7 ");
    if (tags) row.appendChild(el("div", "tags", tags));
    return row;
  }
  function paintRoster(host, rows, d) {
    for (const [gid, title] of FLEET_GROUPS) {
      const rs = rows.filter((r) => fleetGroupOf(r) === gid);
      if (!rs.length) continue;
      const g = el("div", "fl-group");
      g.setAttribute("data-ck-fleet-group", gid);
      g.appendChild(el("h3", "grp-h", title));
      if (gid === "roundtable") {
        // a roundtable's members are on call (the kernel refuses any other): their seats fold into one line
        let last = NaN, unreadable = false, any = false;
        for (const r of rs) {
          const at = isObj(r.health) ? r.health.at : null;
          if (!at) continue;
          any = true;
          const t = parseIso(at);                     // compared as instants, never as strings
          if (isNaN(t)) unreadable = true; else if (isNaN(last) || t > last) last = t;
        }
        const known = !isNaN(last);
        const row = el("div", "fl rrow");
        row.setAttribute("tabindex", "0");
        const n = el("div", "n");
        const dot = el("i", known ? "" : (unreadable ? "unk" : "off"));
        dot.setAttribute("aria-hidden", "true");
        n.append(dot, document.createTextNode("roundtable"));
        const when = known ? (ago(new Date(last).toISOString()) || "unknown") : (unreadable || any ? "unknown" : "never");
        const stateless = rs.filter((r) => r.memory === "stateless").length;
        const tag = stateless === rs.length ? rs.length + " stateless seats" : rs.length + " seats, " + (rs.length - stateless) + " with memory";
        row.append(n, el("div", "d" + (!known && unreadable ? " unk" : ""), rs.map((r) => r.name).join(", ") + " \u00b7 last called " + when),
                   el("div", "tags", tag));
        g.appendChild(row);
      } else {
        for (const r of rs) g.appendChild(fleetRow(r));
      }
      host.appendChild(g);
    }
    if (d.retiredHidden) host.appendChild(el("p", "xnote", d.retiredHidden + " retired " + (d.retiredHidden === 1 ? "member" : "members") + " not shown"));
  }
  // the kernel's sentence, else the verb and the target when the row has one
  function logLine(r) { return typeof r.sentence === "string" && r.sentence ? r.sentence : typeof r.target === "string" && r.target ? r.verb + " · " + r.target : r.verb; }
  function paintLog(host, rows) {
    for (const r of rows) {
      const row = el("div", "lrow");
      row.appendChild(el("span", "lsent", logLine(r)));
      row.appendChild(el("span", "lactor", r.actor));
      row.appendChild(el("span", "lage", ago(r.at) || r.at));
      host.appendChild(row);
    }
  }

  function paintStatusDots(host, metrics) {
    for (const m of metrics) {
      const st = ["ok", "warn", "bad"].indexOf(m.status) >= 0 ? m.status : "unknown";
      const row = el("div", "op " + st);
      row.appendChild(dotOf(st));
      row.appendChild(el("span", "op-l", m.label));
      const fig = m.value == null ? "" : m.value + (m.unit ? " " + m.unit : "");
      // a state is never colour alone: with no figure and no reading, the word stands in
      row.appendChild(el("span", "v", [fig, m.read || ""].filter(Boolean).join(" · ") || st));
      host.appendChild(row);
    }
  }

  // The reading's icon: one inline SVG per token of the contract's READING_SYMBOLS, drawn from a few shared
  // parts (no external asset, no icon font). An unknown token draws the plain cloud rather than nothing.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ICON_INK = "#c9d2e3", ICON_SUN = "#ffd36b", ICON_RAIN = "#7fb2ff";
  const P = {
    cloud: ["path", { d: "M14 30h24a9 9 0 0 0 1-17.9A12 12 0 0 0 14.3 11 10 10 0 0 0 14 30z", fill: ICON_INK }],
    cloudHi: ["path", { d: "M12 30h22a9 9 0 0 0 .8-18A11.5 11.5 0 0 0 12.3 11 9.6 9.6 0 0 0 12 30z", fill: ICON_INK }],
    sun: ["circle", { cx: 26, cy: 20, r: 9, fill: ICON_SUN }],
    sunHi: ["circle", { cx: 34, cy: 12, r: 7, fill: ICON_SUN }],
    rays: ["path", { d: "M26 3v5M26 32v5M9 20h5M38 20h5M14 8l3.5 3.5M34.5 28.5L38 32M14 32l3.5-3.5M34.5 11.5L38 8", stroke: ICON_SUN, "stroke-width": 3, "stroke-linecap": "round", fill: "none" }],
    moon: ["path", { d: "M32 6a14 14 0 1 0 8 25A11 11 0 0 1 32 6z", fill: ICON_INK }],
    moonHi: ["path", { d: "M38 4a9 9 0 1 0 5 16A7 7 0 0 1 38 4z", fill: ICON_INK }],
    drops: ["path", { d: "M18 35l-2 6M27 35l-2 6M36 35l-2 6", stroke: ICON_RAIN, "stroke-width": 2.5, "stroke-linecap": "round", fill: "none" }],
    drizzle: ["path", { d: "M19 35l-1 3M28 35l-1 3M37 35l-1 3", stroke: ICON_RAIN, "stroke-width": 2, "stroke-linecap": "round", fill: "none" }],
    heavy: ["path", { d: "M15 34l-3 8M22 34l-3 8M29 34l-3 8M36 34l-3 8", stroke: ICON_RAIN, "stroke-width": 3, "stroke-linecap": "round", fill: "none" }],
    snow: ["path", { d: "M18 36v5M15.5 38.5h5M27 36v5M24.5 38.5h5M36 36v5M33.5 38.5h5", stroke: ICON_INK, "stroke-width": 2, "stroke-linecap": "round", fill: "none" }],
    bolt: ["path", { d: "M29 28l-7 9h5l-3 7 9-10h-5z", fill: ICON_SUN }],
    lines: ["path", { d: "M8 34h36M12 40h28M8 28h36", stroke: ICON_INK, "stroke-width": 3, "stroke-linecap": "round", fill: "none" }],
    wind: ["path", { d: "M6 18h28a5 5 0 1 0-5-5M6 26h36a5 5 0 1 1-5 5M6 34h20", stroke: ICON_INK, "stroke-width": 3, "stroke-linecap": "round", fill: "none" }],
    haze: ["path", { d: "M10 20h32M6 27h40M10 34h32", stroke: ICON_SUN, "stroke-width": 3, "stroke-linecap": "round", opacity: 0.7, fill: "none" }],
  };
  const ICONS = {
    "clear": ["sun", "rays"], "clear-night": ["moon"], "partly-cloudy": ["sunHi", "cloudHi"], "partly-cloudy-night": ["moonHi", "cloudHi"],
    "cloudy": ["cloud"], "fog": ["cloud", "lines"], "drizzle": ["cloud", "drizzle"], "rain": ["cloud", "drops"], "heavy-rain": ["cloud", "heavy"],
    "thunderstorm": ["cloud", "bolt"], "snow": ["cloud", "snow"], "sleet": ["cloud", "drops", "snow"], "wind": ["wind"], "haze": ["haze"],
  };
  function weatherIcon(symbol) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 52 44");
    svg.setAttribute("width", "52");
    svg.setAttribute("height", "44");
    svg.setAttribute("class", "wx-icon");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", typeof symbol === "string" && symbol ? symbol : "weather");
    for (const key of (Object.prototype.hasOwnProperty.call(ICONS, symbol) ? ICONS[symbol] : ICONS.cloudy)) {
      const [tag, attrs] = P[key];
      const n = document.createElementNS(SVG_NS, tag);
      for (const k of Object.keys(attrs)) n.setAttribute(k, String(attrs[k]));
      svg.appendChild(n);
    }
    return svg;
  }
  // the EPA band a sub-reading names, compared case-insensitively against the contract's lowercase tokens
  const BAND_WORDS = { "good": "Good", "moderate": "Moderate", "unhealthy-sensitive": "Unhealthy for sensitive groups", "unhealthy": "Unhealthy",
    "very-unhealthy": "Very unhealthy", "hazardous": "Hazardous" };
  function bandOf(raw) {
    const t = String(raw).trim().toLowerCase().replace(/\s+/g, "-");
    return Object.prototype.hasOwnProperty.call(BAND_WORDS, t) ? t : "";
  }
  function bars() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 14");
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    const g = document.createElementNS(SVG_NS, "path");
    g.setAttribute("d", "M2 3h9M2 7h12M2 11h7");
    g.setAttribute("stroke", "currentColor");
    g.setAttribute("stroke-width", "2");
    g.setAttribute("stroke-linecap", "round");
    g.setAttribute("fill", "none");
    svg.appendChild(g);
    return svg;
  }

  function paintReading(host, v) {
    const top = el("div", "rd wx-main");
    const icon = weatherIcon(v.symbol);
    icon.setAttribute("data-ck-weather", "icon");
    top.appendChild(icon);
    const main = el("div", "wx-big");
    const t = el("div", "wx-t");
    t.appendChild(el("span", "rd-n num", v.value));
    if (v.unit) t.appendChild(el("span", "rd-u", v.unit));
    main.appendChild(t);
    if (v.condition) main.appendChild(el("div", "rd-c wx-c", v.condition));
    top.appendChild(main);
    // what sits to the side of the figure: the bounds, and every sub-reading that carries no band
    const side = el("div", "wx-side");
    const subs = Array.isArray(v.sub) ? v.sub : [];
    for (const sub of subs) {
      if (typeof sub.band === "string" && sub.band) continue;
      const line = el("div", "rd-sub");
      line.appendChild(document.createTextNode(sub.label + " "));
      line.appendChild(el("b", "num", sub.value == null ? "" : sub.value));
      side.appendChild(line);
    }
    const bounds = [isNum(v.hi) ? ["H", v.hi] : null, isNum(v.lo) ? ["L", v.lo] : null].filter(Boolean);
    if (bounds.length) {
      const line = el("div", "rd-b");
      bounds.forEach(([k, n], i) => { if (i) line.appendChild(document.createTextNode(" · ")); line.appendChild(document.createTextNode(k + " ")); line.appendChild(el("b", "num", Math.round(n) + "°")); });
      side.appendChild(line);
    }
    if (side.childNodes.length) top.appendChild(side);
    host.appendChild(top);
    for (const sub of subs) {
      if (!(typeof sub.band === "string" && sub.band)) continue;
      const token = bandOf(sub.band);
      const row = el("div", "aqi band-" + (token || "other"));
      row.appendChild(bars());
      row.appendChild(el("b", "num", sub.label.charAt(0).toUpperCase() + sub.label.slice(1) + (sub.value == null ? "" : " " + sub.value)));
      // the pill carries the band in words; `good` is calm, any other named band is hot, an unknown one neutral
      const pill = el("span", "pill" + (token && token !== "good" ? " hot" : ""), token ? BAND_WORDS[token] : sub.band);
      pill.setAttribute("data-ck-weather", "aqi");
      row.appendChild(pill);
      host.appendChild(row);
      if (typeof sub.detail === "string" && sub.detail) host.appendChild(el("div", "wx-src", sub.detail));
    }
    const age = typeof v.as_of === "string" ? ago(v.as_of) : "";
    if (age) host.appendChild(el("div", "wx-src", "read " + age));
  }

  // ---- the widget faces: one answer each, never a row (cockpit contract, widgets region) ----
  const WIDGET_DOTS = 4, WIDGET_FIGURES = 4;
  const figure = (n) => Number.isInteger(n) ? n.toLocaleString("en-US") : isNum(n) ? String(Number(n.toPrecision(3))) : String(n);
  function paintWidget(s, host) {
    const d = s.data;
    if (d.face === "count") {
      const n = isNum(d.count) ? d.count : (d.lines || []).length;
      host.appendChild(el("div", "big num", n));
      // at zero the answer is the panel's empty sentence ("Nothing waiting on you"), as the mock draws it; the
      // provider's note (a caveat about what the count leaves out) is the line only when something is waiting, or
      // when the panel is alerted (degraded or failed) and the zero may be a failed read
      if (!n && !s.ctx.alerted) { const e = String(d.empty || ""); if (e) host.appendChild(el("div", "sub", e.charAt(0).toUpperCase() + e.slice(1))); }
      else if (d.note) host.appendChild(el("div", "sub", d.note));
      return;
    }
    if (d.kind === "status-dots") {
      // each metric is drawn at most once, up to WIDGET_DOTS dots and WIDGET_FIGURES figures (the rest counted in "+N more"): a status that is ok / warn / bad is a dot row; any other metric carrying a value is a
      // figure (an integer, a fractional or a string alike); one with neither is a dot row in the unknown state
      const ms = d.metrics || [];
      const isDot = (m) => m.status === "ok" || m.status === "warn" || m.status === "bad";
      const dots = ms.filter((m) => isDot(m) || m.value == null);
      // whole numbers first (they read as the headline figures), then the fractional and the written ones, each in the provider's order
      const rest = ms.filter((m) => !isDot(m) && m.value != null);
      const figs = rest.filter((m) => Number.isInteger(m.value)).concat(rest.filter((m) => !Number.isInteger(m.value)));
      if (dots.length) {
        const box = el("div", "dots");
        paintStatusDots(box, dots.slice(0, WIDGET_DOTS));
        host.appendChild(box);
      }
      if (figs.length) {
        const row = el("div", "row");
        for (const m of figs.slice(0, WIDGET_FIGURES)) { const c = el("span"); c.append(el("b", "num", figure(m.value) + (m.unit ? " " + m.unit : "")), document.createTextNode(m.label)); row.appendChild(c); }
        host.appendChild(row);
      }
      const hidden = Math.max(0, dots.length - WIDGET_DOTS) + Math.max(0, figs.length - WIDGET_FIGURES);
      if (hidden) host.appendChild(el("div", "xnote", "+" + hidden + " more"));
      if (!ms.length && !s.ctx.alerted) host.appendChild(el("div", "sub", d.empty || "nothing to show"));
      return;
    }
    if (d.kind === "metric-grid") {
      const ns = d.numbers || [];
      // the first number is the headline, its label beside it; the others follow as a row of figure and label
      if (ns[0]) {
        const head = el("div", "bigrow");
        head.append(el("div", "big num", figure(ns[0].value)), el("span", "bl", ns[0].label + (ns[0].unit ? " (" + ns[0].unit + ")" : "")));
        host.appendChild(head);
      }
      const row = el("div", "row");
      ns.slice(1).forEach((n) => { const c = el("span"); c.append(el("b", "num", figure(n.value)), document.createTextNode(n.label + (n.unit ? " (" + n.unit + ")" : ""))); row.appendChild(c); });
      if (ns.length > 1) host.appendChild(row);
      return;
    }
    if (d.kind === "reading") { paintReading(host, d.reading); return; }
    for (const l of d.lines || []) host.appendChild(el("div", "sub", l.text));
    if (!(d.lines || []).length && !s.ctx.alerted) host.appendChild(el("div", "sub", d.empty || "nothing to show"));
  }

  function paintGrid(host, numbers) {
    const g = el("div", "mgrid");
    for (const n of numbers) {
      const c = el("div", "mcell");
      c.appendChild(el("b", "num", n.value.toLocaleString("en-US")));
      c.appendChild(el("span", "", n.label + (n.unit ? " (" + n.unit + ")" : "")));
      g.appendChild(c);
    }
    host.appendChild(g);
  }

  // In the brief region a label that repeats is a list, not a paragraph: its lines sit in a side column, clamped, and
  // the rest read as the card's body. The first-seen order is kept; nothing is sorted.
  const BRIEF_SIDE_LINES = 6;
  function paintBrief(host, b, region) {
    if (b.headline) host.appendChild(el("div", "q", b.headline));
    const count = new Map();
    for (const l of b.lines) count.set(l.label || "", (count.get(l.label || "") || 0) + 1);
    const side = new Map();
    for (const l of b.lines) {
      if (region && l.label && count.get(l.label) > 1) { if (!side.has(l.label)) side.set(l.label, []); side.get(l.label).push(l); continue; }
      const row = el("div", "brief-line");
      if (l.label) row.appendChild(el("span", "k", l.label));
      row.appendChild(el("p", "", l.text));
      host.appendChild(row);
    }
    if (side.size) {
      host.classList.add("has-side");
      const col = el("div", "brief-side");
      for (const [label, ls] of side) {
        col.appendChild(el("span", "k", label));
        for (const l of ls.slice(0, BRIEF_SIDE_LINES)) col.appendChild(el("p", "side-line", l.text));
        if (ls.length > BRIEF_SIDE_LINES) col.appendChild(el("p", "xnote", "+" + (ls.length - BRIEF_SIDE_LINES) + " more"));
      }
      host.appendChild(col);
    }
  }

  // The ambient kind's slot. The picture itself is drawn by sky.js, a separate file that registers
  // LevainSky.mount; without it the slot says what it holds, in words.
  const clocks = new Set();
  let clockTimer = 0;
  function setClock(c) {
    const d = new Date();
    const h = d.getHours();
    c.node.replaceChildren(document.createTextNode((h % 12 || 12) + ":" + String(d.getMinutes()).padStart(2, "0")), el("small", "", h < 12 ? "am" : "pm"));
    c.date.textContent = d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  }
  // the page's own clock (the hero's column), ticking whether or not a sky is mounted
  function mountClock(node, date) {
    const clk = { node: node, date: date, seen: false, born: Date.now() };
    clocks.add(clk);
    setClock(clk);
    if (!clockTimer) clockTimer = root.setInterval(tickClocks, 30000);
    return clk;
  }
  function tickClocks() {
    for (const c of Array.from(clocks)) {
      if (c.node.isConnected) c.seen = true;
      else if (c.seen || Date.now() - c.born > 5000) { clocks.delete(c); continue; }   // a clock never attached yet is still being built
      setClock(c);
    }
    if (!clocks.size && clockTimer) { root.clearInterval(clockTimer); clockTimer = 0; }
  }
  // The sky's mount and the clock a panel owns end with the paint that made them.
  function dropSky(s) {
    if (s.skyHandle && typeof s.skyHandle.destroy === "function") { try { s.skyHandle.destroy(); } catch (_) { /* a mount that cannot unmount */ } }
    s.skyHandle = null;
    s.skyParts = null;
    if (s.clock) clocks.delete(s.clock);
    s.clock = null;
  }
  // A panel body that leaves the page for good: nothing it started keeps running.
  // The focus modal moves a typed panel's body out of its panel node (which is then discarded), so a modal
  // state is on the page while its body is; a board state while its panel is.
  function onPage(s) { return (s.modal ? s.body : s.p).isConnected; }
  function teardown(s) {
    root.clearTimeout(s.timer);
    s.seq++;
    dropSky(s);
    states.delete(s);
  }
  function skyCounts(sky) {
    const repos = new Set(sky.events.map((e) => e.repo).filter((r) => typeof r === "string" && r));
    const failed = sky.events.filter((e) => e.outcome === "failed").length;
    return { text: sky.events.length + " events from " + repos.size + " repos", failed: failed };
  }
  // the failed count is the one accent on the line: a failed review is where attention goes
  function paintCounts(node, sky) {
    const c = skyCounts(sky);
    node.replaceChildren(document.createTextNode(c.text));
    if (c.failed) { node.appendChild(document.createTextNode(" · ")); node.appendChild(el("span", "bad", c.failed + " failed")); }
  }
  // An ambient panel whose sky is already mounted takes new events through the mount's own update(), so the
  // animation keeps running instead of restarting. True when it did; false leaves the paint to remount.
  function updateSky(s, sky) {
    const k = s.skyParts;
    if (!k || !s.skyHandle || typeof s.skyHandle.update !== "function" || !k.plot.isConnected || !s.body.contains(k.plot)) return false;
    try { s.skyHandle.update({ events: sky.events, legend: sky.legend }); } catch (_) { return false; }
    paintCounts(k.win, sky);
    paintLegend(k.legend, sky);
    return true;
  }
  // In the hero the clock, the date and the events line are the page's own nodes (ctx.hero), drawn in the clock
  // column beside the sky; anywhere else the sky carries its own.
  function paintSky(s, host, sky) {
    dropSky(s);
    const hero = s.ctx && s.ctx.hero;
    const wrap = el("div", "sky" + (hero ? " sky-hero" : ""));
    let win;
    if (hero) win = hero.events;
    else {
      const when = el("div", "when");
      const t = el("div", "clock num");
      const date = el("div", "date");
      win = el("div", "win");
      when.append(t, date, win);
      const clk = { node: t, date: date, seen: false, born: Date.now() };
      s.clock = clk;
      clocks.add(clk);
      setClock(clk);
      if (!clockTimer) clockTimer = root.setInterval(tickClocks, 30000);
      wrap.appendChild(when);
    }
    paintCounts(win, sky);
    const plot = el("div", "plot");
    plot.dataset.skySlot = "1";
    wrap.appendChild(plot);
    host.appendChild(wrap);
    if (root.LevainSky && typeof root.LevainSky.mount === "function") {
      try { s.skyHandle = root.LevainSky.mount(plot, { events: sky.events, legend: sky.legend }, { legend: false }); } catch (e) { plot.appendChild(el("p", "err", "sky failed: " + (e && e.message ? e.message : e))); }
    } else plot.appendChild(el("p", "xnote", "the sky draws here once sky.js is loaded"));
    const legend = el("div", "legendbar" + (hero ? "" : " frame"));
    paintLegend(legend, sky);
    // the hero's key is a full-width band BELOW the hero (mock-e), not a row inside the sky column: it goes to the
    // page's own #ck-legend mount; a sky anywhere else, or a page without the mount, keeps it under its plot
    const band = hero && document.getElementById ? document.getElementById("ck-legend") : null;
    if (band) { band.replaceChildren(legend); band.hidden = false; } else host.appendChild(legend);
    s.skyParts = { plot: plot, win: win, legend: legend };
  }

  // The strip chart's key: the real glyphs, drawn like the chart's own marks (sky.js). Page chrome, not data:
  // the kernel's `legend` sentence stays as the tooltip. Orange is reserved for failed and now, so the diamond is neutral.
  // The key names only the kinds sky.js draws apart. A feed carrying any other kind gets the kernel's sentence as the
  // whole legend and no glyph key, because sky.js draws an unknown kind with the memory square.
  const DRAWN_KINDS = new Set(["commit", "review", "decision", "memory"]);
  function paintLegend(host, sky) {
    host.replaceChildren();
    host.title = sky.legend;
    const known = (sky.events || []).every((e) => e && DRAWN_KINDS.has(e.kind));
    if (!known) { host.appendChild(el("span", "lg-note", String(sky.legend || ""))); return; }
    const NS = "http://www.w3.org/2000/svg";
    const glyph = (size, build) => {
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("width", String(size)); svg.setAttribute("height", String(size));
      svg.setAttribute("viewBox", "0 0 12 12"); svg.setAttribute("aria-hidden", "true");
      svg.classList.add("lg-g");
      build((tag, attrs) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); svg.appendChild(n); });
      return svg;
    };
    const items = [
      ["commit", (a) => a("circle", { cx: 6, cy: 6, r: 2.5, class: "lg-fill" })],
      ["review", (a) => a("circle", { cx: 6, cy: 6, r: 3.5, class: "lg-ring" })],
      ["review that failed", (a) => a("circle", { cx: 6, cy: 6, r: 4, class: "lg-ring lg-bad" })],
      ["memory write", (a) => a("rect", { x: 5, y: 5, width: 2, height: 2, class: "lg-mem" })],
      ["decision made", (a) => a("path", { d: "M6 2l4 4-4 4-4-4z", class: "lg-fill" })]
    ];
    for (const [label, build] of items) {
      const it = el("span", "lg");
      it.append(glyph(12, build), document.createTextNode(label));
      host.appendChild(it);
    }
    host.appendChild(el("span", "lg-sp"));
    host.appendChild(el("span", "lg-note", "Each mark is one real event. Hover for its time, kind and repo."));
  }

  // ---- one typed panel ----
  // Everything a panel draws OUTSIDE its body: kind, whether it is typed, whether it has a filter box and where
  // that searches, its title and notes, the verbs it offers at panel level, and the hero treatment. Two payloads
  // with the same shape differ only in body data, which paint() redraws in place; a different shape needs the
  // chrome rebuilt, which only the one mount path (render) does.
  function shapeOf(entry, data) {
    const sr = data.search || {};
    return JSON.stringify([entry.title, entry.edit_class || "", entry.source || "", entry.modes || "", entry.banner || null,
      data.kind, !!data.typed, data.error || "", data.markdown != null, data.note || "",
      sr.on === true, sr.scope || "", sr.fields || [],
      data.action ? [data.action.verb, data.action.fields] : null, data.form ? data.form.verb : null,
      data.kind === "ambient" && !!data.sky]);
  }
  // state s: pid, title, data (the mapper's record), body (the node this paints into), query, results.
  function fill(p, data, ctx) {
    const s = { pid: data.pid, title: (ctx && ctx.entry && ctx.entry.title) || (p.querySelector("h2") || {}).textContent || data.pid, data: data, p: p, ctx: ctx || {},
                modal: !!(ctx && ctx.modal), query: saved(FILTER_KEY + data.pid) || "", results: null, timer: 0, seq: 0, born: Date.now() };
    // a rebuilt board leaves its old bodies behind: drop the ones that were never going to be seen again
    for (const o of Array.from(states)) if (!o.body.isConnected && Date.now() - o.born > 5000) teardown(o);
    p.classList.add("k-" + data.kind);
    if (data.face === "count") p.classList.add("k-count");
    if (data.kind === "ambient" && data.sky && !(ctx && ctx.hero)) p.classList.add("panel-hero");
    s.body = el("div", "xbody");
    const sr = data.search || {};
    const rowKind = data.kind === "list" || data.kind === "count-queue" || data.kind === "roster" || data.kind === "log";
    s.searchable = rowKind && sr.on === true && !(ctx && ctx.widget);
    if (!s.searchable) s.query = "";     // a filter saved for a panel that has no filter box must not filter it
    s.shape = shapeOf(ctx && ctx.entry ? ctx.entry : { title: s.title }, data);
    if (data.kind === "count-queue") {
      // the figure is data, not chrome: paint() keeps it current
      const big = el("div", "cq");
      s.cqN = el("span", "cq-n num", "");
      big.appendChild(s.cqN);
      big.appendChild(el("span", "cq-l", "waiting"));
      p.appendChild(big);
    }
    if (s.searchable) p.appendChild(buildFilter(s));
    p.appendChild(s.body);
    s.setQuery = (q, now) => setQuery(s, q, now);
    s.paint = () => paint(s);
    states.add(s);
    paint(s);
    if (s.searchable && sendable(s.query) && sr.scope === "store") runStoreSearch(s);
    if (focusKeep && focusKeep.pid === s.pid && s.filterInput) {
      const keep = focusKeep;
      // the panel is attached by the caller after this returns; a node that is not in the page cannot take focus
      root.setTimeout(() => {
        if (!s.filterInput.isConnected || (document.activeElement && document.activeElement !== document.body)) return;
        s.filterInput.focus();
        try { s.filterInput.setSelectionRange(keep.start, keep.end); } catch (_) { /* a search box that cannot place a caret */ }
      }, 0);
    }
    return s;
  }

  function itemsOf(d) {
    if (d.kind === "roster") return d.roster || [];
    if (d.kind === "log") return d.logRows || [];
    return d.lines || [];
  }

  function paint(s) {
    const d = s.data, sr = d.search || {};
    if (d.kind === "ambient" && !s.fault && updateSky(s, d.sky)) return;
    dropSky(s);
    s.body.replaceChildren();
    if (s.fault) { s.body.appendChild(el("p", "err", "unavailable — " + s.fault)); return; }
    if (s.cqN) s.cqN.textContent = String(isNum(d.count) ? d.count : (d.lines || []).length);
    if (s.ctx.widget) { paintWidget(s, s.body); return; }
    if (d.kind === "status-dots") { paintStatusDots(s.body, d.metrics || []); if (!(d.metrics || []).length && !s.ctx.alerted) s.body.appendChild(el("p", "empty", d.empty)); return; }
    if (d.kind === "reading") { paintReading(s.body, d.reading); return; }
    if (d.kind === "metric-grid") { paintGrid(s.body, d.numbers); return; }
    if (d.kind === "brief") { paintBrief(s.body, d.brief, s.ctx.region === "brief"); return; }
    if (d.kind === "ambient") { paintSky(s, s.body, d.sky); return; }
    if (d.kind === "form") {
      // the compose box (mounted by the core when the verb is offered) is the form; without it, say why
      if (!d.action) s.body.appendChild(el("p", "xnote", "this form cannot send from here" + (d.form.verb ? " (" + d.form.verb + " is not offered)" : "")));
      return;
    }
    // row-shaped: the window, or the store's answer to the query
    const nq = sendable(s.query);     // the query as the kernel would take it: a blank one is no query
    const terms = termsOf(s.query);
    const store = sr.scope === "store" && nq && s.searchable;
    let items = itemsOf(d), hays = d.hays || [], label = "", err = "";
    if (store) {
      if (s.results) { items = s.results.items; hays = s.results.hays; label = s.results.label; err = s.results.error; if (err) items = []; }
      else { items = []; label = "searching…"; }
    } else if (terms.length) {
      const keep = [];
      items.forEach((it, i) => { if (hits(hays[i] || "", terms)) keep.push(i); });
      label = keep.length + " of " + items.length;
      hays = keep.map((i) => hays[i]);
      items = keep.map((i) => items[i]);
    } else if (s.searchable) {
      label = items.length + " rows" + (sr.window ? " · newest " + sr.window : "");
    }
    if (s.countEl) { s.countEl.textContent = err ? "search failed" : label; s.countEl.classList.toggle("bad", !!err); }
    if (err) s.body.appendChild(el("p", "err", "search failed — " + err));
    else if (!items.length) s.body.appendChild(el("p", "empty", store && !s.results ? "searching…" : (terms.length ? "no match" : d.empty)));
    // the first screenful, unless the operator asked for all of it or is filtering
    const cap = s.searchable && !nq && !s.showAll && items.length > (sr.visible || 10) ? (sr.visible || 10) : items.length;
    const shown = items.slice(0, cap);
    if (d.kind === "roster") paintRoster(s.body, shown, d);
    else if (d.kind === "log") paintLog(s.body, shown);
    else if (s.ctx.rows) s.ctx.rows(s.body, shown);
    if (cap < items.length) {
      const more = el("button", "xmore", "show all " + items.length);
      more.type = "button";
      more.addEventListener("click", () => { s.showAll = true; paint(s); });
      s.body.appendChild(more);
    }
    if (store && s.results && s.results.label && !err) s.body.appendChild(el("p", "xnote", s.results.label));
  }

  function setQuery(s, q, now) {
    s.query = q;
    s.showAll = false;
    save(FILTER_KEY + s.pid, q);
    for (const o of states) if (o !== s && o.pid === s.pid && onPage(o)) { o.query = q; if (o.filterInput) o.filterInput.value = q; paintOrSearch(o, now); }
    if (s.filterInput && s.filterInput.value !== q) s.filterInput.value = q;
    paintOrSearch(s, now);
  }
  function paintOrSearch(s, now) {
    const sr = s.data.search || {};
    s.seq++;     // every change of query or data retires the search in flight, not only the one that gets sent
    if (sr.scope === "store" && s.searchable) {
      if (!sendable(s.query)) { s.results = null; root.clearTimeout(s.timer); paint(s); return; }
      s.results = null;
      paint(s);
      root.clearTimeout(s.timer);
      if (now) runStoreSearch(s); else s.timer = root.setTimeout(() => runStoreSearch(s), SEARCH_DEBOUNCE_MS);
    } else paint(s);
  }

  // The store scope: ask the kernel, over the page's own read transport (the same token handling as every
  // panel read; q rides the query string of a read and is never kept by the kernel).
  async function runStoreSearch(s) {
    const q = sendable(s.query);
    const seq = ++s.seq;
    if (!q) { s.results = null; paint(s); return; }
    let out;
    try {
      const raw = await readPanel(s.pid, "&q=" + encodeURIComponent(q) + "&limit=" + STORE_LIMIT);
      out = mapSearch(s, clean(raw));
    } catch (e) { out = { items: [], hays: [], label: "", error: e && e.message ? e.message : String(e) }; }
    if (seq !== s.seq || !onPage(s)) return;     // a newer query, or the panel is gone
    s.results = out;
    paint(s);
  }
  function mapSearch(s, payload) {
    if (typeof payload.search_error === "string" && payload.search_error) return { items: [], hays: [], label: "", error: payload.search_error };
    if (payload.status === "error") return { items: [], hays: [], label: "", error: payload.error || "unavailable" };
    const titles = new Map();
    for (const g of payload.groups || []) titles.set(g.id, titleOf(g.title, String(g.id)));
    const ext = externalOf(payload, payload, titles);
    return { items: itemsOf(ext), hays: ext.hays || [], label: ext.search.matchLabel, error: "" };
  }

  function readPanel(pid, extra) {
    const t = root.LevainCockpitTransport;
    if (!t || typeof t.readJson !== "function") return Promise.reject(new Error("no read transport on this page"));
    return t.readJson("/cockpit/panel/" + encodeURIComponent(pid) + ".json?profile=full" + (extra || ""));
  }

  // Refetch one panel by id. The streaming lane (CK4) calls this when an event says a panel moved, so it is
  // the hot path and must disturb nothing the operator is holding: the scroll position, an expanded row, the
  // filter's caret, a mounted sky. A panel is therefore kept, not rebuilt: when the fresh payload has the same
  // shape as the one drawn (the chrome is unchanged, only data moved) every live copy of the panel, board and
  // focus modal alike, repaints its body in place and re-measures its host, and no node is created, moved or
  // detached. When the shape changed, this declines and the caller answers with a full reload, so the board's
  // one render path stays the only place a panel is mounted. Resolves "applied" when the page was brought up to
  // date, "superseded" when a newer refetch of the same panel took over (this one changed nothing and says nothing), "fault" when the read or the mapping failed (the panel
  // says so, and the caller may retry), and false when the caller must reload.
  const refetchGen = new Map();
  const liveOf = (pid) => Array.from(states).filter((s) => s.pid === pid && onPage(s));
  async function refetchPanel(pid) {
    if (!liveOf(pid).length) return false;
    const gen = (refetchGen.get(pid) || 0) + 1;
    refetchGen.set(pid, gen);
    let raw, fault = "";
    try { raw = await readPanel(pid); } catch (e) { fault = e && e.message ? e.message : String(e); }
    if (refetchGen.get(pid) !== gen) return "superseded";   // a newer refetch of this panel owns the answer and the record
    const live = liveOf(pid);
    if (!live.length) return false;
    if (fault) { for (const s of live) { s.fault = fault; paint(s); } return "fault"; }
    const core = root.LevainDashboard;
    if (!lastManifest || !core) return false;
    let view, entry, data;
    try {
      view = mapSnapshot({ manifest: lastManifest, panels: { [pid]: raw } }, { elapsedMs: Date.now() - manifestAt });
      entry = view.layout.find((e) => e.id === pid);
      data = view.extra_panels[pid];
    } catch (e) { for (const s of live) { s.fault = "could not be mapped (" + (e && e.message ? e.message : e) + ")"; paint(s); } return "fault"; }
    if (!entry || !data) return false;      // the panel is no longer an external one: a full reload draws it
    const shape = shapeOf(entry, data);
    if (live.some((s) => s.shape !== shape)) return false;
    for (const s of live) {
      s.data = data;
      s.fault = "";
      // the read-age chip is the one piece of chrome that moves with every read: it follows the payload
      const src = s.p && s.p.querySelector ? s.p.querySelector(".phead .src.fresh") : null;
      if (src) src.textContent = entry.fresh || "";
      paint(s);
      const sr = data.search || {};
      if (s.searchable && sr.scope === "store" && sendable(s.query)) runStoreSearch(s);
      // the host's finalisation, on the node already there: the clause toggles and the overflow cue follow the new rows
      const host = s.body.closest ? s.body.closest(".pbody, .modal-body") : null;
      if (host) {
        if (core.measureClauses) core.measureClauses(host);
        if (core.measureOverflow && host.classList.contains("clamped")) core.measureOverflow(host);
      }
    }
    // the page now shows what this read returned, so the held etag is this read's own (never a stream event's)
    const heldMap = root.LevainCockpitHeld;
    if (heldMap && raw && typeof raw.etag === "string") heldMap[pid] = Object.assign({}, heldMap[pid], { etag: raw.etag });
    return "applied";
  }

  // ---- the modes and the keys ----
  function applyMode(m) {
    if (MODES.indexOf(m) < 0) m = "board";
    document.body.dataset.mode = m;
    save(MODE_KEY, m);
    for (const b of document.querySelectorAll("[data-mode-tab]")) b.setAttribute("aria-selected", b.dataset.modeTab === m ? "true" : "false");
  }
  function typing(t) { return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable); }
  function visibleFilters() {
    return Array.from(document.querySelectorAll(".xfilter-input")).filter((i) => i.offsetParent !== null);
  }

  // ---- the jump box: one search path with the filters. A panel title, a loaded row, or "search the store" ----
  let palette = null;
  function jumpItems(q) {
    const terms = termsOf(q);
    const items = [];
    for (const h of document.querySelectorAll("#board .panel > .phead h2")) {
      const panel = h.closest(".panel");
      if (!terms.length || hits(fold(h.textContent), terms)) items.push({ kind: "panel", label: h.textContent, node: panel });
    }
    if (terms.length) {
      for (const s of states) {
        if (!onPage(s) || !s.searchable) continue;
        const d = s.data, hays = d.hays || [], its = itemsOf(d);
        let n = 0;
        its.forEach((it, i) => {
          if (n < 5 && hits(hays[i] || "", terms)) {
            n++;
            const text = d.kind === "roster" ? it.name : d.kind === "log" ? logLine(it) : it.text;
            items.push({ kind: "row", label: String(text), where: s.title, state: s, q: q });
          }
        });
        if ((d.search || {}).scope === "store") items.push({ kind: "store", label: "Search " + s.title + " for “" + q + "”", state: s, q: q });
      }
    }
    return items.slice(0, 14);
  }
  function openJump() {
    if (palette) return;
    const dlg = el("div", "palette");
    dlg.setAttribute("role", "dialog");
    dlg.setAttribute("aria-label", "Jump");
    const box = el("div", "palette-box");
    const input = el("input", "palette-input");
    input.placeholder = "Jump to a panel or a row";
    input.setAttribute("aria-label", "Jump to");
    const list = el("ul", "palette-list");
    list.setAttribute("role", "listbox");
    box.append(input, list);
    dlg.appendChild(box);
    document.body.appendChild(dlg);
    palette = { dlg: dlg, input: input };
    let items = [], sel = 0;
    const close = () => { dlg.remove(); palette = null; };
    const choose = (it) => {
      close();
      if (it.kind === "panel") {
        // a panel the current mode hides is reached through Board, which shows every panel
        if (document.body.dataset.mode && (it.node.dataset.modes || "").split(" ").indexOf(document.body.dataset.mode) < 0) applyMode("board");
        if (it.node.ckShowTab) it.node.ckShowTab();
        it.node.scrollIntoView({ block: "start" }); it.node.setAttribute("tabindex", "-1"); it.node.focus({ preventScroll: true }); return; }
      applyMode("board");
      it.state.setQuery(it.q, true);
      it.state.p.scrollIntoView({ block: "start" });
      if (it.state.filterInput) it.state.filterInput.focus();
    };
    const draw = () => {
      list.replaceChildren();
      items.forEach((it, i) => {
        const li = el("li");
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", i === sel ? "true" : "false");
        li.append(el("span", "pk", it.kind === "panel" ? "panel" : it.kind === "store" ? "store" : "row"), el("span", "pl", it.label));
        if (it.where) li.appendChild(el("span", "pw", it.where));
        li.addEventListener("click", () => choose(it));
        list.appendChild(li);
      });
      if (!items.length) list.appendChild(el("li", "pl-none", "nothing matches"));
    };
    const refresh = () => { items = jumpItems(input.value); sel = 0; draw(); };
    input.addEventListener("input", refresh);
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); close(); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); sel = Math.min(items.length - 1, sel + 1); draw(); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
      else if (ev.key === "Enter" && items[sel]) { ev.preventDefault(); choose(items[sel]); }
    });
    dlg.addEventListener("mousedown", (ev) => { if (ev.target === dlg) close(); });
    refresh();
    input.focus();
  }

  function install() {
    applyMode(saved(MODE_KEY) || "board");
    document.addEventListener("click", (ev) => {
      const t = ev.target && ev.target.closest ? ev.target.closest("[data-mode-tab],#jumpbtn") : null;
      if (!t) return;
      if (t.id === "jumpbtn") openJump(); else applyMode(t.dataset.modeTab);
    });
    // a filter keeps focus through a re-render until the operator goes somewhere else
    document.addEventListener("mousedown", (ev) => { if (!(ev.target && ev.target.closest && ev.target.closest(".xfilter"))) focusKeep = null; });
    document.addEventListener("mouseover", (ev) => { const p = ev.target && ev.target.closest ? ev.target.closest(".panel") : null; if (p) hoverPanel = p; });
    document.addEventListener("keydown", (ev) => {
      if (ev.defaultPrevented) return;
      if ((ev.metaKey || ev.ctrlKey) && ev.key && ev.key.toLowerCase() === "k") { ev.preventDefault(); if (palette) palette.dlg.remove(), palette = null; else openJump(); return; }
      if (ev.metaKey || ev.ctrlKey || ev.altKey || palette || typing(ev.target)) return;
      if (document.querySelector(".modal-overlay")) return;
      if (ev.key === "/") {
        const all = visibleFilters();
        if (!all.length) return;
        const near = (hoverPanel && hoverPanel.isConnected && all.find((i) => hoverPanel.contains(i))) || all[0];
        ev.preventDefault();
        near.focus();
        near.select();
      } else if (ev.key.length === 1 && "123".indexOf(ev.key) >= 0) applyMode(MODES["123".indexOf(ev.key)]);
    });
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install); else install();
  }

  const pruneModal = () => { for (const o of Array.from(states)) if (o.modal && !o.body.isConnected) teardown(o); };
  Object.assign(K, { fill, mountClock, refetchPanel, pruneModal, reset: () => { for (const o of Array.from(states)) if (!(o.modal && o.body.isConnected)) teardown(o); }, fold, termsOf, hits, jumpItems, applyMode });

  const api = { fromManifest, ageLabel, badgeText, visible, clean, refetchPanel, fold };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else { root.LevainCockpitView = api; root.LevainCockpitKinds = K; }
})(typeof globalThis !== "undefined" ? globalThis : this);
