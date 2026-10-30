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
  // entry, so no per-field guard can miss a field. This is a RENDER ESCAPE, not the judge of what hides: the
  // server's levain.unshowable is, and a row's text_hidden flag (computed there) is what locks its editing.
  // HOSTILE is a context-free approximation of unshowable.is_unshowable: it shows as <U+XXXX> every control,
  // format, surrogate, separator, private-use, unassigned or default-ignorable character, every space but U+0020,
  // and the characters that draw nothing. Kept as they are: a tab and newlines (structure); the zero-width joiner
  // and non-joiner (emoji sequences and Persian or Indic text need them); U+FE0E and U+FE0F, whose rule depends on
  // the character before them (marking every one here would break "⚠️"). So a kept one may still hide: the
  // server's flag, not this escape, decides whether that row's text can be edited here.
  const HOSTILE = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}\p{Co}\p{Cn}\p{Zs}\p{Default_Ignorable_Code_Point}\u2800\u{16fe4}\u{1d159}]/gu;
  const KEPT = new Set(["\n", "\t", " ", "\u200c", "\u200d", "\ufe0e", "\ufe0f"]);
  function visible(v) {
    return String(v === null || v === undefined ? "" : v).replace(/\r\n/g, "\n").replace(HOSTILE,
      (ch) => (KEPT.has(ch) ? ch : "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">"));
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

  // The kernel sends a save's STAMP, never its age (an age moves every second and would change the panel's etag on an
  // idle store). The page draws the age from the stamp against the server's clock and redraws it on its own timer:
  // AGE_TICK_MS is that timer's one interval, so a display tier can slow or stop every age on the page by changing it.
  const AGE_TICK_MS = 30000;
  const CALM_AGE_TICK_MS = 120000;       // the same timer under Calm: slower, never stopped, so every age stays true
  const SAVE_STAMP = "last save at";
  let serverOffsetMs = 0;       // the server's clock minus this browser's, as of the last snapshot
  const serverNow = () => Date.now() + serverOffsetMs;

  function ageLabel(iso, now, prefix) {
    const t = parseIso(iso);
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.floor((now - t) / 1000));
    const u = s >= 86400 ? Math.floor(s / 86400) + "d" : s >= 3600 ? Math.floor(s / 3600) + "h"
      : s >= 60 ? Math.floor(s / 60) + "m" : s + "s";
    return (prefix || "") + u + " ago";
  }

  const savedAge = (iso, nowMs) => ageLabel(iso, nowMs, "") || "unknown";
  // the Health panel's save stamp as a metric a reader can read: an age that tickAges keeps current; no readable stamp reads "unknown"
  function saveMetric(m, nowMs) {
    const iso = typeof m.value === "string" && !isNaN(parseIso(m.value)) ? m.value : null;
    return Object.assign({}, m, { label: "last save", value: iso ? savedAge(iso, nowMs || serverNow()) : "unknown", age_at: iso });
  }
  let ageTimer = 0;
  // Calm (one page control): the page reads as if the OS asked for reduced motion. The class on <html> is the one predicate every
  // reader (dashboard.css, topology_view.js, sky.js, the age timer) tests; the choice is kept per browser, absent storage = full.
  const isCalm = () => typeof document !== "undefined" && !!document.documentElement && !!document.documentElement.classList && document.documentElement.classList.contains("calm");
  // Once armed, the one timer runs for the page's life: a tick that finds no age on the page costs one empty query, and the
  // next age drawn needs no re-arm. Detached nodes are skipped (querySelectorAll only returns attached ones, so they are never seen).
  function tickAges() {
    for (const n of (typeof document !== "undefined" && document.querySelectorAll ? Array.from(document.querySelectorAll("[data-age-at]")) : [])) {
      if (!n.isConnected) continue;
      const text = savedAge(n.dataset.ageAt, serverNow());
      if (n.textContent !== text) n.textContent = text;
      // only the age part of the row's tooltip moves; its reading and detail stay
      if (n.dataset.ageTitle && n.parentNode && n.parentNode.title !== undefined)
        n.parentNode.title = n.dataset.ageTitle + ": " + [text, n.dataset.ageRead || "", n.dataset.ageDetail || ""].filter(Boolean).join(" · ");
    }
  }
  // a node that shows a save's age: tagged with its stamp, and the one timer started if it is not running
  function ageNode(node, m, titled) {
    if (!m.age_at) return node;
    node.dataset.ageAt = m.age_at;
    if (titled) {                                       // the row's tooltip carries the age too, beside the reading and detail
      node.dataset.ageTitle = m.label;
      node.dataset.ageRead = m.read || "";
      node.dataset.ageDetail = typeof m.detail === "string" ? m.detail : "";
    }
    if (!ageTimer) ageTimer = root.setInterval(tickAges, isCalm() ? CALM_AGE_TICK_MS : AGE_TICK_MS);
    return node;
  }

  // a span that shows the age of `iso` and ticks like every other age (the native Health panel draws its "last save" with it)
  function ageSpan(iso) { return ageNode(el("span", "", savedAge(iso, serverNow())), { age_at: iso }); }

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
  function sporeOf(pid, row, groupTitles, hidden, today) {
    const f = row.facets || {};
    const t = targetOf(pid, row);
    return Object.assign({
      id: bare(row.id), age: ageOf(row), age_days: Number.isInteger(f.age_days) ? f.age_days : null, type: f.spore_type || "task", tier: f.tier, salience: f.salience, domain: f.domain,
      disposition: f.disposition, text: row.body || row.title, next: f.due || null, held: f.held === true, today: typeof today === "string" ? today : null,
      descend_kinds: kindsOf(t.actions, "spore_descend"), ascend_kinds: kindsOf(t.actions, "spore_ascend"),
      group_title: row.group ? (groupTitles.get(row.group) || row.group) : null, group: row.group || null,
    }, t, hidden && hidden.has(row.id) ? { text_locked: HIDDEN_TEXT } : {});
  }

  // the reason the Tray gives for a spore it holds until its surface date
  const HELD_REASON = "surface date not reached";

  // a stale or partial panel keeps rendering its rows, under a banner saying so
  function bannerOf(head, panel, now) {
    const out = [];
    if (head.status === "stale") out.push({ tone: "stale", text: "STALE: last read " + (ageLabel(head.as_of, now, "") || "unknown") });
    if (head.status === "partial") for (const s of head.skipped || []) out.push({ tone: "stale", text: "PARTIAL: " + s.count + " unreadable — " + s.reason });
    // a panel that lists its held rows draws that line itself, as the control that shows them
    const held = Array.isArray(panel.held) && panel.held.length > 0;
    for (const f of head.filtered || []) {
      if (held && f.reason === HELD_REASON) continue;
      out.push({ tone: "dim", text: f.count + " held back — " + f.reason });
    }
    // an optional source never seen says so once, in the panel's empty sentence: its "not configured: ..." note adds nothing over no rows
    if (head.note && !(/^not configured: /.test(head.note) && !(panel.rows || []).length)) out.push({ tone: "dim", text: head.note });
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

  // Now's rows in date order with the heading each sits under. Overdue and undated (waiting on you) rows lead as TODAY / OVERDUE,
  // each later day within a week is "TUE 10-20", anything further is LATER.
  // `today` is the server's calendar day ("YYYY-MM-DD", the panel's own `today`); the browser's clock and zone decide nothing here
  function nowByDate(rows, today) {
    const day = (y, m, d) => Date.UTC(y, m, d);
    const tm = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(today || ""));
    const d0 = new Date();
    const today0 = tm ? day(+tm[1], +tm[2] - 1, +tm[3]) : day(d0.getFullYear(), d0.getMonth(), d0.getDate());   // an old server sends none: the page's own day
    const iso = (r) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String((r.facets || {}).due || "")); return m ? day(+m[1], +m[2] - 1, +m[3]) : null; };
    const keyed = rows.map((r, i) => ({ r: r, i: i, t: iso(r) }));
    const rank = (k) => (k.t === null ? today0 : k.t);     // undated: due now
    keyed.sort((a, b) => rank(a) - rank(b) || a.i - b.i);
    const label = new Map();
    const names = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
    for (const k of keyed) {
      const t = rank(k);
      let text;
      if (t < today0) text = "OVERDUE";
      else if (t === today0) text = "TODAY";
      else if (t - today0 <= 7 * 86400000) { const d = new Date(t); text = names[d.getUTCDay()] + " " + String(d.getUTCMonth() + 1).padStart(2, "0") + "-" + String(d.getUTCDate()).padStart(2, "0"); }
      else text = "LATER";
      label.set(k.r.id, text);
    }
    return { rows: keyed.map((k) => k.r), label: label };
  }

  function externalOf(head, panel, groupTitles, nowMs) {
    const v = panel.value || {};
    const kind = ALIAS[head.kind] || head.kind;
    const out = { note: head.note || "", empty: head.empty || "nothing here", kind: kind, pid: head.id, typed: false,
                  search: searchOf(panel), face: panel.face === "count" ? "count" : "",
                  // whether the read behind this panel is clean and fresh (a widget's definitive zero needs it)
                  clean: (head.status === undefined || head.status === "ok" || head.status === "empty") && !(head.skipped || []).length && !(head.filtered || []).length };
    if (kind === "list" || kind === "count-queue") {
      // the sessions panel's note is what its readings do not see (sandbox is configured, not confirmed; no session end
      // is announced): it closes the focus modal and stays off the tile, where a long sentence would crowd the figure
      if (head.id === "sessions" && typeof head.note === "string") { out.footer = head.note; out.note = ""; }
      const compose = composeOf(panel.compose);
      // an addressed list draws every row as `source -> for`. Its address is the compose's recipient field when
      // the panel declares a compose; a read-only serve declares none (no send verb), but the panel still DECLARES
      // the `for` facet (head.facets), so the declaration alone addresses the rows: never the write side, and never
      // the values the current rows happen to carry (a broadcast-only inbox is still addressed; a list whose rows
      // carry a `for` key it does not declare is not).
      const rfield = compose ? compose.fields.find((f) => f.type === "recipient") : null;
      const forName = rfield ? rfield.name : "for";
      const declared = Array.isArray(head.facets) ? head.facets : Array.isArray(panel.facets) ? panel.facets : [];
      const addressed = !!rfield || declared.indexOf("for") >= 0;
      if (compose) out.compose = compose;
      // Now reads as a dated list, as the mock draws it: what is late and what is due today first, then each day of the next seven by
      // date ("TUE 10-20"), one line per item. A row with no date is due now (a decision waiting on you).
      const nowRows = head.id === "now" ? nowByDate(panel.rows || [], panel.today) : null;
      const srcRows = nowRows ? nowRows.rows : (panel.rows || []);
      if (nowRows) out.note = "next 7 days";
      out.lines = srcRows.map((r) => {
        const grp = r.group ? (groupTitles.get(r.group) || r.group) : "";
        const badges = (r.badges || []).map(badgeText).join(" · ");
        const prov = r.provenance ? r.provenance.label : "";
        const legacy = (r.facets || {}).legacy_meta;   // an adapted extra panel's own meta line (design §6.2)
        // a list drawn as a count opens into the focus modal, where its groups are headings (Waiting on you, Waiting on a
        // signature, ...) in the kernel's own order, so a group is the heading and no longer repeated on each row
        const grouped = panel.face === "count" && grp;
        const meta = [typeof legacy === "string" ? legacy : "", r.panel_id ? r.panel_id : "", grouped ? "" : grp, badges, prov]
          .filter(Boolean).join(" · ");
        const line = { meta: meta, text: r.body || r.title, accent: r.emphasis === "accent", dim: r.emphasis === "dim" };
        if (head.id === "sessions") {
          line.wrapMeta = true;
          // the Build tab's session row (paintBuildSessions below): the provider's own measures, never a full cwd
          const sv = (r.facets || {}).session_view;
          if (isObj(sv)) line.sess = Object.assign({ agent: typeof (r.facets || {}).agent === "string" ? r.facets.agent : "", active: (r.facets || {}).session_activity === "active" }, sv);
          // the transcript button's key: the row id is "session:<stem>" (the stem is a hash, not the session id)
          if (typeof r.id === "string" && /^session:[0-9a-f]{24}$/.test(r.id)) line.session = r.id.slice("session:".length);
        }
        if (typeof r.id === "string" && r.id) line.key = r.id;
        if (grouped) line.grp = grp;
        if (nowRows) { line.grp = nowRows.label.get(r.id) || ""; line.meta = ""; }
        if (head.id === "jobs") {
          // Build's Results: a finished job is a result: ok or failed, what ran, and when (never the raw result body)
          const jf = r.facets || {};
          const st = jf.job_status;
          line.dot = st === "done" ? "ok" : st === "failed" ? "bad" : "off";
          line.text = (typeof jf.verb === "string" && jf.verb ? jf.verb : r.title) + " " + (st === "done" ? "finished" : st === "failed" ? "failed" : st || "");
          // `at` is the job's finish for a finished job; a job still pending or running is not a result yet and says so
          line.meta = st === "pending" || st === "running" ? st : (typeof jf.at === "string" ? ago(jf.at) : "");
        }
        const rf = (r.facets || {}).repo;   // the structured repo: when a row carries one, its tag comes from this, not its words
        if (typeof rf === "string" && visible(rf).trim()) line.repo = visible(rf).trim();
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
        // an agent's consent request (levain.consent.listing), in the kernel's decisions panel only: the row carries
        // who asks, for what tool, its state and times; the request itself is fetched with the page's token
        // (dashboard_core.js consentRow). Everything is drawn as text; the countdown is the page's, from expires_at
        if (head.id === "decisions" && typeof r.id === "string" && r.id.indexOf("consent:") === 0) {
          const f = r.facets || {};
          const sv = (x) => (typeof x === "string" ? x : "");
          line.consent = { id: r.id.slice("consent:".length), agent: sv(f.agent), tool: sv(f.tool), state: sv(f.consent_state),
                           tier: sv(f.consent_tier), egressState: sv(f.egress_state), expiresAt: sv(f.expires_at),
                           age: typeof f.at === "string" ? ageLabel(f.at, nowMs || Date.now(), "") : "",
                           decidedAge: typeof f.decided_at === "string" ? ageLabel(f.decided_at, nowMs || Date.now(), "") : "" };
        }
        // an agent's question (Q-2b): the row carries who asked, when, how many options and whether it is answered; the
        // question itself is fetched with the page's token (dashboard_core.js questionRow). `offered` is the kernel's
        // own answer that this credential may answer it from here (the verb's gesture), never the page's guess
        if (head.id === "decisions" && typeof r.id === "string" && r.id.indexOf("question:") === 0) {
          const f = r.facets || {};
          const sv = (x) => (typeof x === "string" ? x : "");
          line.question = { id: r.id, n: isNum(f.question_n) ? f.question_n : 0, agent: sv(f.agent), state: sv(f.question_state),
                            options: isNum(f.question_options) ? f.question_options : 0,
                            age: typeof f.at === "string" ? ageLabel(f.at, nowMs || Date.now(), "") : "",
                            decidedAge: typeof f.decided_at === "string" ? ageLabel(f.decided_at, nowMs || Date.now(), "") : "",
                            offered: (Array.isArray(r.actions) ? r.actions : []).some((a) => a && a.verb === "question_answer" && a.gesture === "direct"),
                            sign: signOf(f) };
        }
        // a coordinator review item: its state, whether it is "yours to clear", and the one verb the kernel offers on it
        // here (review_clear on a plain open item, review_reopen on a cleared one), drawn as a button on the row
        if (head.id === "review-queue" && typeof r.id === "string" && r.id.indexOf("review:") === 0) {
          const f = r.facets || {};
          const offer = (Array.isArray(r.actions) ? r.actions : []).find((a) => a && (a.verb === "review_clear" || a.verb === "review_reopen" || a.verb === "review_clear_signed") && a.gesture === "direct");
          line.review = { id: r.id, item: r.id.slice("review:".length), state: f.review_state === "cleared" ? "cleared" : "open",
                          mine: f.review_authority === true, verb: offer ? offer.verb : "" };
        }
        // an agent's note: who filed it, when, and whether it was dismissed; its words are fetched with the page's token
        // (dashboard_core.js noteBody). It carries no answer and no authority: `offered` is only the kernel's dismiss
        if (head.id === "decisions" && typeof r.id === "string" && r.id.indexOf("note:") === 0) {
          const f = r.facets || {};
          const sv = (x) => (typeof x === "string" ? x : "");
          line.note = { id: r.id, n: isNum(f.note_n) ? f.note_n : 0, agent: sv(f.agent), state: sv(f.note_state),
                        total: isNum(f.note_open_total) ? f.note_open_total : 0,
                        age: typeof f.at === "string" ? ageLabel(f.at, nowMs || Date.now(), "") : "",
                        decidedAge: typeof f.decided_at === "string" ? ageLabel(f.decided_at, nowMs || Date.now(), "") : "",
                        offered: (Array.isArray(r.actions) ? r.actions : []).some((a) => a && a.verb === "note_dismiss" && a.gesture === "direct"),
                        sign: signOf(f) };
        }
        return line;
      });
      const fields = fieldsOf(out.search, ROW_FIELDS);
      out.hays = srcRows.map((r) => hayOfRow(r, fields));
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
        && (m.read == null || typeof m.read === "string") && (m.detail == null || typeof m.detail === "string")), "metrics");
      out.metrics = ms.map((m) => (m.label === SAVE_STAMP ? saveMetric(m, nowMs) : m));
      out.typed = true;
      out.lines = out.metrics.map((m) => ({
        meta: m.label,
        text: [m.value == null ? "" : m.value + (m.unit ? " " + m.unit : ""), m.read || "", m.detail || ""].filter(Boolean).join(" — ") || (m.status || "unknown"),   // the detail rides along: the focus filter, the jump box and a touch reader have no hover
        accent: m.status === "warn" || m.status === "bad", dim: m.status === "unknown" }));
    } else if (kind === "ambient") {
      if (Array.isArray(v.events)) {
        need(v.events.every((e) => isObj(e) && typeof e.kind === "string" && typeof e.at === "string"), "events");
        need(typeof v.legend === "string", "legend");
        out.sky = { events: v.events, legend: v.legend, dialect: v.dialect };
        out.typed = true;
      } else out.lines = labelled(v.text, "text").map((l) => ({ meta: l.label, text: l.text }));
    } else if (kind === "brief") {
      const lines = labelled(v.lines, "lines");
      const head1 = typeof v.headline === "string" && v.headline ? v.headline : "";
      if (typeof v.markdown === "string" && v.markdown) out.markdown = v.markdown;   // the card's own head carries the title: a bold copy of it above the text read as a second title
      else { out.typed = true; out.brief = { headline: head1, lines: lines, title: typeof v.title === "string" ? v.title : "", source: typeof v.source === "string" ? v.source : "",
        threads: Array.isArray(v.threads) ? v.threads : null, overnight: isObj(v.overnight) ? v.overnight : null }; }
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
    } else if (kind === "topology") {
      // the counts as lines (the jump box, the filter and a phone read them), and the full value for topology_view.js; the
      // compact profile carries numbers where the full one carries arrays, and has nothing to draw
      const n = (x, c) => (Array.isArray(x) ? x.length : isNum(c) ? c : 0);
      const tasks = Array.isArray(v.tasks) ? v.tasks : [];
      const waiting = Array.isArray(v.nodes) && root.LevainTopologyView && root.LevainTopologyView.waitingOnYou ? root.LevainTopologyView.waitingOnYou(v) : isNum(v.waiting) ? v.waiting : 0;   // the one function behind every "waiting on you" figure; the compact profile carries the count itself
      out.lines = [{ meta: "topology", text: n(v.nodes, v.nodes) + " sessions \u00b7 " + n(v.tasks, v.tasks) + " tasks \u00b7 " + waiting + " waiting on you" }];
      if (Array.isArray(v.nodes) && Array.isArray(v.tasks) && Array.isArray(v.groups) && isObj(v.hub)) { out.topology = v; out.typed = true; out.replay = head.replay === true || panel.replay === true; }   // typed: the page keeps a live state for it (the header lens and Build's Lost-if-closed read it)
    } else if (kind === "board") {
      // levain's board.json value (board_view.js draws it): the whole value rides to the painter, which turns it into the model
      // (LevainBoardView.boardModel); the counts ride as one line for the jump box and a touch reader. Read-only: no verb, no write box.
      need(Array.isArray(v.columns) && v.columns.every((c) => isObj(c) && typeof c.category === "string" && Array.isArray(c.cards)
        && c.cards.every((k) => isObj(k) && typeof k.id === "string")), "board columns");
      const total = v.columns.reduce((n, c) => n + c.cards.length, 0);
      out.board = v;
      out.typed = true;
      out.lines = [{ meta: "board", text: total + " cards · " + v.columns.map((c) => c.category + " " + c.cards.length).join(" · ") }];
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
  // a question or note row's sign facet (DESIGN_SIGN D5): the id of the sign request its answer is signed through, the
  // id only (the page reads the request with its token); "" when the row names none
  const signOf = (f) => (isObj(f.sign) && typeof f.sign.request === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(f.sign.request) ? f.sign.request : "");
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

  // Which of the four tabs show a panel (F3). Morning is the intake (Today, the glance, the Tray), Board the work (Tray, loops, Now,
  // Keep, episodes, Team), Build the live agents (the sessions, hot loops, edits, results), Memory the mind (the six sections,
  // crystals, cognition, identity). The hero is the page's header and is in all four. A panel the table does not name takes its
  // region's tabs: a glance tile on Morning and Board, the brief on Morning, a work card on Board, a mind or identity card on Memory.
  const ALL_TABS = "morning board build memory";
  // Null-prototype tables: a panel id comes from the wire, and "constructor" or "__proto__" must read as not-in-the-table
  // rather than an inherited function.
  const tableOf = (o) => Object.assign(Object.create(null), o);
  const TABS_OF_ID = tableOf({
    tray: "morning board", loops: "board build", now: "board", keep: "board", episodes: "board", "team-status": "board",
    edits: "build", jobs: "build", sessions: "morning board", "coord-morning": "morning board", "review-queue": "morning board", topology: "build", health: "memory",
    graph: "memory", wraps: "memory", crystals: "memory"
  });
  const TABS_OF_REGION = tableOf({ brief: "morning", widgets: "morning board", hero: ALL_TABS, operate: "board", mind: "memory", identity: "memory", team: "board" });
  function modesOf(e) {
    return TABS_OF_ID[e.id] || TABS_OF_REGION[e.region || e.zone] || "board";
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

  // The figures health reads: each is a finite number, except the write-path status.
  const HEALTH_FIGURES = ["episodes", "episodes since wrap", "tombstones", "wraps", "graduated", "demoted"];
  // the write path's words, and the status each stands for (Health.status). "dark" is the older kernel's word for ZERO LINKS,
  // never for unreadable: it stands for whatever the wrap count says (live once a wrap committed, else no wraps yet)
  const WRITE_PATH_STATUS = { "live": "live", "no wraps yet": "no-wraps", "unreadable": "unreadable", "dark": null };
  const WRITE_PATHS = Object.keys(WRITE_PATH_STATUS);
  const writePathStatus = (word, wraps) => WRITE_PATH_STATUS[word] || (isNum(wraps) && wraps > 0 ? "live" : "no-wraps");
  function healthOf(metrics, wraps, nowMs) {
    need(Array.isArray(metrics), "metrics");
    const m = new Map();
    for (const x of metrics) { need(isObj(x) && typeof x.label === "string", "metric"); m.set(x.label, x.value); }
    // every figure is required: the kernel always sends each one, and an absent figure would print as 0
    for (const k of HEALTH_FIGURES) need(m.has(k) && isNum(m.get(k)), "metric " + k);
    need(WRITE_PATHS.indexOf(m.get("write path")) >= 0, "metric write path");
    const n = (k) => m.get(k);
    // the substrate oracles are optional (the kernel omits one it could not read): carried only when well-formed
    // the kernel sends the save's stamp, never its age: an age moves every second and would change the panel's etag
    const savedMs = m.has("last save at") ? parseIso(m.get("last save at")) : NaN;
    const age = isNaN(savedMs) ? null : Math.max(0, (nowMs - savedMs) / 3600000);
    const proj = m.get("projection");
    const projOk = proj === "converged" ? true : (proj === "diverged" || proj === "file missing" ? false : null);
    // a present-but-unrecognised value is surfaced, never silently null; "unknown" carries the kernel's note
    const projKnown = proj === undefined || proj === null || ["converged", "diverged", "file missing", "unknown"].indexOf(proj) >= 0;
    const projNote0 = m.get("projection note");
    const projNote = proj === "file missing" ? "continuity file missing"
      : (!projKnown ? "unknown (unrecognised value)" : (typeof projNote0 === "string" && projNote0 ? projNote0 : null));
    const audit = m.has("audit write failures") && isNum(m.get("audit write failures")) ? m.get("audit write failures") : null;
    const last = Array.isArray(wraps) && wraps[0] ? wraps[0] : null;
    return {
      write_path_live: writePathStatus(m.get("write path"), n("wraps")) === "live", status: writePathStatus(m.get("write path"), n("wraps")),
      total_episodes: n("episodes"), episodes_since_wrap: n("episodes since wrap"),
      tombstones: n("tombstones"), total_wraps: n("wraps"),
      graduations_validated_total: n("graduated"), graduations_demoted_total: n("demoted"),
      last_wrap_at: last ? last.wrapped_at : null, continuity_chars: last ? last.continuity_chars : null,
      wrap_history_unavailable: wraps === "unavailable",
      save_age_hours: age, save_at: isNaN(savedMs) ? null : m.get("last save at"), projection_converged: projOk,
      projection_note: projNote,
      audit_backlog: audit,
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
      // `default`, `transient_prefix` and `gone_match` are the provider's own declarations on its recipient field (the ids pressed at the
      // start, the ids that can leave its roster, the words of its refusal when one has): carried through, given no meaning here
      const str = (x) => (typeof x === "string" && x ? x : undefined);
      return { name: f.name, label: label, kind: "multiselect", chips: true, required: false,
               options: Array.isArray(f.enum) ? f.enum.filter((x) => typeof x === "string") : [],
               default: Array.isArray(f.default) ? f.default.filter((x) => typeof x === "string") : [],
               transient_prefix: str(f.transient_prefix), gone_match: str(f.gone_match) };
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
    // the rows whose text hides a character: the server's text_hidden flag (levain.unshowable over the raw
    // title and body) is the one judge; the page never decides this itself
    const hiddenRows = new Set();
    for (const key of Object.keys(raw.panels || {})) {
      const rp = raw.panels[key];
      for (const r of (isObj(rp) ? (Array.isArray(rp.rows) ? rp.rows : []).concat(Array.isArray(rp.held) ? rp.held : []) : [])) {
        if (!isObj(r) || typeof r.id !== "string") continue;
        if (r.text_hidden === true) hiddenRows.add(visible(r.id));
      }
    }
    for (const key of Object.keys(raw.panels || {})) {
      const k = visible(key);
      if (Object.prototype.hasOwnProperty.call(snap.panels, k)) throw new Error("two panel ids collide once cleaned: " + k);
      let v;
      try { v = clean(raw.panels[key]); } catch (e) { v = { error: "payload rejected: " + (e && e.message ? e.message : e) }; }
      Object.defineProperty(snap.panels, k, { value: v, enumerable: true, writable: true, configurable: true });
    }
    noteRepos(snap.panels);
    const m = snap.manifest;
    const gen = parseIso(m.generated_at);
    serverOffsetMs = isNaN(gen) ? 0 : gen - Date.now();
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
      const entry = { zone: zoneId, edit_class: "", title: pid === "jobs" ? "Results" : titleOf(panel.title, titleOf(head.title, pid)), id: pid,
                      pkind: ALIAS[panel.kind] || panel.kind };
      if (region) entry.region = region;
      // how many rows the panel holds, from the kernel's own count: the page's count pill reads this, never the title's words
      const held = isNum(panel.count) ? panel.count : (isNum(head.count) ? head.count : null);
      if (held !== null) entry.count = held;
      const banner = bannerOf(panel, panel, now);
      if (banner) entry.banner = banner;
      if (panel.as_of) entry.fresh = ageLabel(panel.as_of, now, "read ");
      const rows = panel.rows || [];
      const kind = NATIVE.get(nat);
      // what this panel lets the operator do: its own actions (a create, a value write, a downstream verb)
      // and the actions its rows carry. The page offers a verb only where the kernel offered one.
      const pActions = Array.isArray(panel.actions) ? panel.actions.filter((a) => isObj(a) && typeof a.verb === "string") : [];
      const heldRows = nat === "tray" && Array.isArray(panel.held) ? panel.held : [];
      const rowsAct = rows.concat(heldRows).some((r) => r && Array.isArray(r.actions) && r.actions.length);
      if (pActions.length) {
        const pv = isObj(panel.value) && typeof panel.value.value_version === "string" ? panel.value.value_version : null;
        view.targets.panels[pid] = { actions: pActions, panel_version: pv };
      }
      for (const r of rows.concat(heldRows)) if (r && typeof r.id === "string" && Array.isArray(r.actions) && r.actions.length) {
        view.targets.rows[r.id] = targetOf(pid, r);
      }
      // every capture is the Tray's create, so the Keep's capture box shows whenever the Tray offers one
      const tp = P.get("tray");
      const traySeeds = isObj(tp) && Array.isArray(tp.actions) && tp.actions.some((a) => isObj(a) && a.verb === "spore_seed" && isPostable(a));
      if ((rowsAct || pActions.length || (nat === "keep" && traySeeds)) && (nat === "tray" || nat === "loops" || nat === "keep" || nat === "episodes")) entry.edit_class = "B";
      const lists = { tray: "tray", loops: "open_spores", keep: "keep" };
      if ((nat === "tray" || nat === "loops" || nat === "keep") && (panel.kind === "triage-list" || panel.kind === "list")) {
        view[lists[pid]] = rows.map((r) => sporeOf(pid, r, groupTitles, hiddenRows, panel.today));
        if (heldRows.length) {
          view.tray_held = heldRows.map((r) => sporeOf(pid, r, groupTitles, hiddenRows, panel.today));
          const hf = (head.filtered || []).find((f) => f && f.reason === HELD_REASON);
          view.tray_held_line = { count: hf && isNum(hf.count) ? hf.count : heldRows.length, reason: HELD_REASON };
        }
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
                   activation_mode: f.activation_mode, last_activated_on: f.last_activated_on, tags: tags };
        });
        view.layout.push(Object.assign(entry, { kind: "crystals" }));
      } else if (nat === "health" && (panel.kind === "metric" || panel.kind === "status-dots")) {
        view.health = healthOf((panel.value || {}).metrics, wrapsData, now);
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
        if (region && wp && WRITE_PATHS.indexOf(wp.value) >= 0) {
          const wn = (view.extra_panels[pid].metrics || []).find((x) => x.label === "wraps");
          view.write_path_status = writePathStatus(wp.value, wn ? wn.value : null); view.write_path_live = view.write_path_status === "live";
        }
        // a downstream's own verb on this panel becomes the compose box. The manifest names the verb's
        // fields and not their widgets, so a widget is chosen from the field's name (a known edge).
        // a form panel names its verb: the action is the one offered under that name, and no other
        const formVerb = view.extra_panels[pid].form ? view.extra_panels[pid].form.verb : (view.extra_panels[pid].compose ? view.extra_panels[pid].compose.verb : null);
        const ownOf = (a) => (!isObj(a.target) || typeof a.target.row_id !== "string") && (formVerb === null || a.verb === formVerb);
        // an action a browser cannot perform (laptop-only, refused) draws nothing: the panel's rows already name the laptop command.
        // A text panel keeps its read-first editor whatever the gesture (its own design, held by the editor tests):
        // only the editor verb, the one whose head has exactly one field, new_body (dashboard_core.js mounts that alone as an editor)
        const textPanel = view.extra_panels[pid].markdown != null;
        const isEditor = (a) => {
          const h = view.verb_heads[a.verb];
          return textPanel && isObj(h) && Array.isArray(h.fields) && h.fields.length === 1 && h.fields[0] === "new_body";
        };
        const own = pActions.find((a) => ownOf(a) && (isPostable(a) || isEditor(a)));
        const ownLaptop = !own && pActions.some((a) => ownOf(a) && !isPostable(a));
        const vh = own && isObj(view.verb_heads[own.verb]) ? view.verb_heads[own.verb] : null;
        const cmpDecl = view.extra_panels[pid].compose;
        if (cmpDecl && view.writable && !NATIVE.has(pid) && !ownLaptop) {
          // a writable surface draws the card or says why not, never a silently missing card (a read-only one
          // draws no verbs at all, so it stays silent)
          if (!vh) throw new Error("its compose verb " + cmpDecl.verb + " is not offered by this panel");
          // a job's result arrives by polling, which the card does not do
          if (vh.job === true) throw new Error("its compose verb " + cmpDecl.verb + " is a job verb, which a compose card cannot send");
        }
        if (vh && !NATIVE.has(pid)) {
          // the action names its own panel: the send posts to THIS panel's offer, never another panel's
          view.extra_panels[pid].action = { verb: own.verb, panel: pid, fields: (Array.isArray(vh.fields) ? vh.fields : []).filter((n) => typeof n === "string").map(composeField) };
          view.action_verbs[own.verb] = { label: vh.label, confirm_required: vh.confirm === true, idempotent: vh.idempotent === true, job: false, sign: vh.sign === true };
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
    // the hero is the page's header and reads the same in every mode: its weather is not one of the orientation
    // readings Build drops
    const inHero = new Set(view.ke ? view.ke.hero : []);
    for (const e of view.layout) e.modes = inHero.has(e.id) ? ALL_TABS : modesOf(e);
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
  const MODES = ["morning", "board", "build", "memory"];
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
      row.appendChild(ageNode(el("span", "v", (m.age_at ? fig : [fig, m.read || ""].filter(Boolean).join(" · ")) || st), m, true));      // an aged row's cell is the age alone: its reading rides in the tooltip
      // the row stays one line and a tile cuts its reading: the tooltip holds the whole reading, then the detail
      const whole = [[fig, m.read || ""].filter(Boolean).join(" · "), typeof m.detail === "string" ? m.detail : ""].filter(Boolean).join(" · ");
      if (whole) row.title = m.label + ": " + whole;
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

  // opts (a glance tile only): rows = how many band sub-readings to draw, age = whether the "read ... ago" line is drawn
  function paintReading(host, v, opts) {
    const cap = opts && opts.rows != null ? opts.rows : Infinity;
    const sideCap = opts && opts.side != null ? opts.side : Infinity;
    let drawn = 0, sideDrawn = 0;
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
      if (sideDrawn++ >= sideCap) continue;
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
    // the banded sub-readings (air, pollen, any other) are one small table beside the figure: a band glyph, the name, the figure on a
    // tabular column, the band in words; each source line sits under its own row. An absent reading draws nothing, never a placeholder.
    const air = el("div", "wx-air");
    for (const sub of subs) {
      if (!(typeof sub.band === "string" && sub.band)) continue;
      if (drawn++ >= cap) continue;
      const token = bandOf(sub.band);
      const row = el("div", "aqi band-" + (token || "other"));
      row.appendChild(bars());
      row.appendChild(el("span", "aqi-l", sub.label.charAt(0).toUpperCase() + sub.label.slice(1)));
      row.appendChild(el("b", "num aqi-v", sub.value == null ? "" : String(sub.value)));
      // the pill carries the band in words; `good` is calm, any other named band is hot, an unknown one neutral
      const pill = el("span", "pill" + (token && token !== "good" ? " hot" : ""), token ? BAND_WORDS[token] : sub.band);
      pill.setAttribute("data-ck-weather", "aqi");
      row.appendChild(pill);
      air.appendChild(row);
      if (typeof sub.detail === "string" && sub.detail) { row.title = sub.detail; air.appendChild(el("div", "wx-src aqi-d", sub.detail)); }
    }
    if (air.childNodes.length) host.appendChild(air);
    const age = typeof v.as_of === "string" ? ago(v.as_of) : "";
    if (age && !(opts && opts.age === false)) host.appendChild(el("div", "wx-src wx-age", "read " + age));
  }

  // ---- the widget faces: one answer each, never a row (cockpit contract, widgets region) ----
  const WIDGET_DOTS = 4, WIDGET_FIGURES = 4;
  const figure = (n) => Number.isInteger(n) ? n.toLocaleString("en-US") : isNum(n) ? String(Number(n.toPrecision(3))) : String(n);
  // A glance tile draws what fits and says how much more is behind it. `s.budget` is how many items the tile may draw (null = all
  // the static caps allow); `s.units` is how many it could draw, `s.hidden` how many it does not (a "+N more" line), `s.clamp` whether
  // a run of text had to be cut to two lines. fitWidget() finds the budget that fits the tile's real height.
  const moreNote = (host, n) => { if (n > 0) host.appendChild(el("div", "xnote", "+" + n + " more")); };
  // F5 (a): a count tile that holds rows names what is in front, not only how many. Decisions names the top item the strip draws
  // first (levain-needs, dashboard_core.js, orders both); Sessions says live and quiet and names the busiest.
  function glanceFace(d, n, host) {
    const N = root.LevainNeeds;
    if (d.pid === "decisions" && n && N && Array.isArray(d.lines)) {
      const items = N.items(d.lines);
      if (items.length) {
        host.appendChild(el("div", "sub glance-top", N.name(items[0])));
        host.appendChild(el("div", "sub glance-sum", N.summary(items)));
      }
      return;
    }
    if (d.pid === "sessions" && Array.isArray(d.lines)) {
      const sess = d.lines.map((l) => l && l.sess).filter(isObj);
      if (!sess.length) return;
      const live = sess.filter((x) => x.active), quiet = sess.length - live.length;
      const pool = live.length ? live : sess;
      let top = pool[0];
      for (const x of pool) if ((x.tokens || 0) > (top.tokens || 0)) top = x;
      const line = d.lines.find((l) => l && l.sess === top);
      const meter = isNum(top.tokens) && top.tokens > 0 ? (top.window ? " \u00b7 " + Math.round(100 * top.tokens / top.window) + "% of context" : " \u00b7 " + k7K(top.tokens) + " tokens") : "";
      // one line: a strip tile draws only its first sub line, and the counts are what the reader wants first. The busiest seat is the tile's
      // title (hover) and a second sub line where the tile has the room for it, never a tail that gets cut off the counts.
      const busiest = ((line && line.text) || top.agent || "session").replace(/\s+/g, " ").trim() + meter;
      const counts = el("div", "sub glance-top", live.length + " live \u00b7 " + quiet + " quiet");
      counts.title = "busiest: " + busiest;
      host.appendChild(counts);
      host.appendChild(el("div", "sub glance-busiest", "busiest: " + busiest));
    }
  }
  function paintWidget(s, host) {
    const d = s.data;
    const cap = s.budget == null ? Infinity : s.budget;
    s.units = 0; s.hidden = 0;
    if (d.face === "count") {
      const n = isNum(d.count) ? d.count : (d.lines || []).length;
      const big = el("div", "big num", n);
      big.dataset.count = String(n);   // the glow below reads this, not the figure's drawn text
      host.appendChild(big);
      // at zero the answer is the panel's empty sentence ("Nothing waiting on you"), as the mock draws it, but only for
      // a clean, fresh read: on a degraded one (partial, stale, cached, alerted, something held back) the zero may be a
      // failed read, so the provider's note stays the line. The sentence is the panel's own: only a plain lowercase
      // English word at its head is capitalised (an identifier or a path is left as written).
      const sentence = (e) => /^[a-z]+ /.test(e) ? e.charAt(0).toUpperCase() + e.slice(1) : e;
      // a tile whose glance face draws it (Decisions names the top item, Sessions says live and quiet) is drawn by that face, after
      // the provider's note when there is one: the generic first line + count would be a second, lesser answer ahead of it, and a
      // strip tile shows only its first sub line
      const glance = el("div");
      glanceFace(d, n, glance);
      const glanced = glance.childNodes.length > 0;
      const note = typeof d.note === "string" ? d.note.trim() : "";
      // the face names the top item and counts the rest ("backfill +3 more"), so the tile reads as an answer, not a bare number
      const topItem = () => {
        if (glanced || !n || s.ctx.alerted || !Array.isArray(d.lines) || !d.lines[0] || typeof d.lines[0].text !== "string") return;
        host.appendChild(el("div", "sub", d.lines[0].text + (n > 1 ? " +" + (n - 1) + " more" : "")));
      };
      if (!n && !s.ctx.alerted && d.clean !== false) { const e = String(d.empty || ""); if (e) host.appendChild(el("div", "sub", sentence(e))); }
      else if (note) {
        // with something waiting (or a degraded zero), the provider's note is its word on what the read covers ("no cockpit
        // broker: decisions waiting on a signature are not shown"), so it is the first line: a strip tile draws only its first
        // sub line, and a count with its caveat dropped reads as the whole answer. The top item follows it.
        host.appendChild(el("div", "sub", note));
        topItem();
      }
      else topItem();
      while (glance.firstChild) host.appendChild(glance.firstChild);
      return;
    }
    if (d.kind === "status-dots" && d.pid === "doctor" && (d.metrics || []).some((m) => m.status === "ok" || m.status === "warn" || m.status === "bad")) {
      // the face is one answer: how many checks are ok, and the worst one named ("hooks · 1 warning"); the rows are the focus view's
      const ms = d.metrics || [];
      const graded = ms.filter((m) => m.status === "ok" || m.status === "warn" || m.status === "bad");
      const okN = graded.filter((m) => m.status === "ok").length;
      const worst = graded.find((m) => m.status === "bad") || graded.find((m) => m.status === "warn");
      s.units = 1; s.hidden = ms.length;     // the checks behind the face: the tile opens them in the focus view
      const head = el("div", "bigrow");
      head.append(el("div", "big num", okN + "/" + graded.length), el("span", "bl", "ok"));
      host.appendChild(head);
      host.appendChild(el("div", "sub", worst ? worst.label + " · " + (worst.value == null ? worst.status : worst.value) : "nothing needs attention"));
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
      const dotsAll = dots.slice(0, WIDGET_DOTS), figsAll = figs.slice(0, WIDGET_FIGURES);
      s.units = dotsAll.length + figsAll.length;
      // the dots keep the room first; the figures give way first
      const nd = Math.min(dotsAll.length, cap), nf = Math.min(figsAll.length, Math.max(0, cap - nd));
      if (nd) {
        const box = el("div", "dots");
        paintStatusDots(box, dotsAll.slice(0, nd));
        host.appendChild(box);
      }
      if (nf) {
        const row = el("div", "row");
        for (const m of figsAll.slice(0, nf)) { const c = el("span"); c.append(ageNode(el("b", "num", figure(m.value) + (m.unit ? " " + m.unit : "")), m), document.createTextNode(m.label)); row.appendChild(c); }
        host.appendChild(row);
      }
      s.hidden = ms.length - nd - nf;
      moreNote(host, s.hidden);
      if (!ms.length && !s.ctx.alerted) host.appendChild(el("div", "sub", d.empty || "nothing to show"));
      return;
    }
    if (d.kind === "metric-grid") {
      const ns = d.numbers || [];
      s.units = ns.length;
      const shown = ns.slice(0, cap);
      // the first number is the headline, its label beside it; the others follow as a row of figure and label
      if (shown[0]) {
        const head = el("div", "bigrow");
        head.append(el("div", "big num", figure(shown[0].value)), el("span", "bl", shown[0].label + (shown[0].unit ? " (" + shown[0].unit + ")" : "")));
        host.appendChild(head);
      }
      const row = el("div", "row");
      shown.slice(1).forEach((n) => { const c = el("span"); c.append(el("b", "num", figure(n.value)), document.createTextNode(n.label + (n.unit ? " (" + n.unit + ")" : ""))); row.appendChild(c); });
      if (shown.length > 1) host.appendChild(row);
      s.hidden = ns.length - shown.length;
      moreNote(host, s.hidden);
      return;
    }
    if (d.kind === "reading") {
      const r = d.reading || {};
      // every component the tile draws is budgeted: the side lines (sub-readings with no band) first, then the band rows, and the
      // "read ... ago" line goes before any of them; whatever is not drawn is counted, so the tile opens
      const subs = Array.isArray(r.sub) ? r.sub : [];
      const bands = subs.filter((x) => typeof x.band === "string" && x.band).length;
      const side = subs.length - bands;
      const hasAge = typeof r.as_of === "string" && !!ago(r.as_of);
      s.units = side + bands;
      const sideShown = Math.min(side, cap), bandShown = Math.min(bands, Math.max(0, cap - side));
      paintReading(host, r, { rows: bandShown, side: sideShown, age: !s.noAge });
      s.hidden = side - sideShown + bands - bandShown + (s.noAge && hasAge ? 1 : 0);
      moreNote(host, s.hidden);
      return;
    }
    const ls = d.lines || [];
    s.units = ls.length;
    for (const l of ls.slice(0, cap)) host.appendChild(el("div", "sub", l.text));
    s.hidden = Math.max(0, ls.length - cap);
    moreNote(host, s.hidden);
    if (!ls.length && !s.ctx.alerted) host.appendChild(el("div", "sub", d.empty || "nothing to show"));
  }

  // Draw a glance tile at the largest budget that fits the room it really has (its height is fixed on a wide page and grows with
  // its content on a phone), so nothing is cut by the tile's bottom edge. What does not fit is counted, and the tile says so to
  // whoever opens it: a "levain-hidden" event on the tile's body, with the number hidden (0 = nothing behind it).
  function fitWidget(s) {
    const box = s.p;
    const fits = () => box.scrollHeight <= box.clientHeight + 1;
    s.fitting = true;
    try {
      s.budget = null; s.noAge = false; s.clamp = false;
      s.body.classList.remove("clamped");
      paintBody(s);
      if (!fits()) {
        s.noAge = true; paintBody(s);
        let b = s.units;
        while (!fits() && b > 0) { b--; s.budget = b; paintBody(s); }
        if (!fits()) { s.clamp = true; s.body.classList.add("clamped"); }
      }
    } finally { s.fitting = false; }
    publishOpenable(s);
  }
  // THE rule for whether a glance tile is a button, in one place: it is openable iff it is not faulted AND (it is a count face, whose
  // list is always behind it, OR it holds items back: a "+N more" or a run of text cut to two lines). Every path that paints a widget
  // ends here (a good fit, a fault), and dashboard_core.js listens to the "levain-hidden" event this publishes for every typed tile.
  function publishOpenable(s) {
    const open = !s.fault && (s.data.face === "count" || s.hidden > 0 || s.clamp);
    publishHidden(s, open ? Math.max(1, s.hidden || 0) : 0);
  }
  function publishHidden(s, n) {
    const box = s.p;
    if (!box || (box.dataset.ckHidden || "0") === String(n)) return;
    box.dataset.ckHidden = String(n);
    if (typeof root.CustomEvent === "function" && typeof box.dispatchEvent === "function") box.dispatchEvent(new root.CustomEvent("levain-hidden", { bubbles: true, detail: n }));
  }
  // the room a tile has is only known once it is laid out and while it keeps its size: the first fit runs on attach, and a
  // ResizeObserver on the tile's body fits it again whenever its width (a figure row wraps differently) or its room changes
  function fitWidgets(region) {
    for (const s of Array.from(states)) {
      if (!(s.ctx.widget && s.p && s.body.isConnected && region.contains(s.p))) continue;
      fitWidget(s);
      if (!s.ro && typeof root.ResizeObserver === "function") {
        s.ro = new root.ResizeObserver(() => { if (s.body.isConnected && !s.fitting) fitWidget(s); });
        s.ro.observe(s.p);
      }
    }
  }

  // The focus view of a metric list (status-dots, metric-grid): every metric, and a filter box once the list is long enough to want one.
  const FILTER_MIN = 8;
  function paintFiltered(s) {
    const d = s.data, dots = d.kind === "status-dots";
    const all = (dots ? d.metrics : d.numbers) || [];
    const terms = termsOf(s.query);
    const hay = (m) => fold([m.label, m.value, m.unit, m.read, m.detail, m.status].filter((x) => x != null).join(" "));
    const ms = terms.length ? all.filter((m) => hits(hay(m), terms)) : all;
    if (s.countEl) s.countEl.textContent = terms.length ? ms.length + " of " + all.length : all.length + " metrics";
    if (!ms.length) { s.body.appendChild(el("p", "empty", terms.length ? "no match" : d.empty)); return; }
    if (dots) paintStatusDots(s.body, ms); else paintGrid(s.body, ms);
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
  // Where to begin, as the mock draws it: a head (title, what it reads from), the lead in large type with what comes after it,
  // the overnight line, and a Threads column: the open loops grouped by the repo each one names (the row tags' own reading), a
  // loop naming none under its domain, else "other". Loudest first: most loops, then the hottest tier.
  const THREADS_SHOWN = 5;
  const TIER_RANK = { hot: 0, warm: 1, cold: 2, parked: 3 };
  function groupThreads(threads) {
    const sky = root.LevainSky;
    const matchers = matchersOf(sky);
    const groups = new Map();
    for (const t of threads) {
      if (!isObj(t) || typeof t.text !== "string") continue;
      const text = visible(t.text);
      const dom = typeof t.domain === "string" ? visible(t.domain).trim() : "";
      // the repo a row's tag shows: its domain when that is a repo the sky knows (Build's Open loops chip), else the repo its words name
      const hit = (dom && matchers.find(([n]) => n.toLowerCase() === dom.toLowerCase())) || matchers.find(([, re]) => re.test(text));
      const key = hit ? "r:" + hit[0] : "d:" + (dom || "other");
      let g = groups.get(key);
      if (!g) { g = { repo: hit ? hit[0] : "", label: hit ? "" : (dom || "other"), top: text, n: 0, rank: 9, order: groups.size }; groups.set(key, g); }
      g.n++;
      const r = t.tier in TIER_RANK ? TIER_RANK[t.tier] : 9;
      if (r < g.rank) { g.rank = r; g.top = text; }   // the group is named by its hottest loop
    }
    return Array.from(groups.values()).sort((a, b) => b.n - a.n || a.rank - b.rank || a.order - b.order);
  }
  function paintBriefCard(host, b) {
    const head = el("div", "brief-head");
    // levain's own card is named for what it holds: the Tray's next items (the data's "Where to begin" would read twice beside flow's plan card)
    head.appendChild(el("h3", "", b.title === "Where to begin" ? "Next from the Tray" : b.title));
    head.appendChild(el("span", "sp"));
    const hasThreads = Array.isArray(b.threads) && groupThreads(b.threads).length > 0;
    // where the card has a lower heading (the loops column) the "from ..." caption sits under it, not floating in the head
    if (b.source && !hasThreads) head.appendChild(el("span", "stamp", b.source));
    host.appendChild(head);
    const body = el("div", "brief-cols");
    const lead = el("div", "brief-lead");
    const lines = b.lines || [];
    if (b.headline) {
      // nothing set: one line, what is missing and the way to fill it
      lead.appendChild(el("p", "lead-first", [b.headline, ...lines.map((l) => l.text)].join(". ").replace(/\.\.$/, ".")));
    } else {
      if (lines[0]) lead.appendChild(el("p", "lead-first", lines[0].text));
      if (lines[1]) { const p = el("p", "lead-then"); p.appendChild(el("span", "k", lines[1].label || "then")); p.appendChild(document.createTextNode(" " + lines[1].text)); lead.appendChild(p); }
      if (lines.length > 2) {
        const more = el("div", "lead-more");
        for (const l of lines.slice(2)) { const p = el("p", "lead-item"); p.appendChild(el("span", "k", l.label || "")); p.appendChild(document.createTextNode(" " + l.text)); more.appendChild(p); }
        lead.appendChild(more);
      }
    }
    const ov = b.overnight;
    if (isObj(ov) && Number.isInteger(ov.episodes) && typeof ov.since === "string") {
      const at = new Date(ov.since);
      if (!Number.isNaN(at.getTime())) {
        const when = at.toLocaleDateString(undefined, { weekday: "short" }) + " " + at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
        const row = el("div", "brief-overnight");
        row.appendChild(el("span", "k", "Overnight"));
        row.appendChild(el("span", "mono", ov.episodes + (ov.episodes === 1 ? " episode" : " episodes") + " since the last wrap, " + when));
        lead.appendChild(row);
      }
    }
    body.appendChild(lead);
    const groups = Array.isArray(b.threads) ? groupThreads(b.threads) : [];
    if (groups.length) {
      const col = el("div", "brief-threads");
      col.appendChild(el("div", "k", "Loops by thread"));
      if (b.source) col.appendChild(el("div", "brief-src", b.source));
      for (const g of groups.slice(0, THREADS_SHOWN)) {
        const row = el("div", "thread");
        row.appendChild(el("span", "t", g.repo ? g.top : g.label));
        row.appendChild(el("span", "pill", g.n + (g.n === 1 ? " loop" : " loops")));
        if (g.repo) {
          const sky = root.LevainSky;
          const tag = el("span", "rp-tag", g.repo);
          const key = sky && typeof sky.hueKey === "function" ? sky.hueKey(g.repo) : "";
          if (key) tag.dataset.repo = key;
          row.appendChild(tag);
        }
        col.appendChild(row);
      }
      if (groups.length > THREADS_SHOWN) col.appendChild(el("p", "xnote", "+" + (groups.length - THREADS_SHOWN) + " more"));
      body.appendChild(col);
    }
    body.classList.toggle("has-threads", groups.length > 0);
    host.appendChild(body);
  }
  function paintBrief(host, b, region) {
    if (region && b.title && b.source) { host.classList.add("brief-card"); paintBriefCard(host, b); return; }
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
    // the hero's legend band goes with the sky it keyed: a dropped, failed or repainting sky leaves no stale key
    if (s.ctx && s.ctx.hero && typeof s.ctx.hero.hide === "function") s.ctx.hero.hide();
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
    if (s.ro) { s.ro.disconnect(); s.ro = null; }
    states.delete(s);
  }
  function isLevFeed(sky) {
    const S = root.LevainSky;
    try { return !!(S && typeof S.isLevainFeed === "function" && S.isLevainFeed({ events: sky.events, dialect: sky.dialect })); } catch (_) { return false; }
  }
  // the Build tab draws the sky as the compact strip the mock shows (merged lanes, its own header row), the others the full chart
  function skySize() { return document.body && document.body.dataset && document.body.dataset.mode === "build" ? "strip" : "compact"; }
  function skyNow(s) {
    const k = s.skyParts;
    return !!k && k.size === skySize();
  }
  function skyCounts(sky) {
    const evs = Array.isArray(sky.events) ? sky.events : [];
    const repos = new Set(evs.map((e) => e && e.repo).filter((r) => typeof r === "string" && r));
    const failed = evs.filter((e) => e && e.outcome === "failed").length;
    const n = evs.length + (evs.length === 1 ? " event" : " events");
    return { text: repos.size ? n + " from " + repos.size + (repos.size === 1 ? " repo" : " repos") : n, failed: failed };
  }
  // the failed count is the one accent on the line: a failed review is where attention goes
  function paintCounts(node, sky) {
    const c = skyCounts(sky);
    node.replaceChildren(document.createTextNode(c.text));
    // a Levain feed in the hero: the band draws only the wrap line (the mock), the sky's own summary line says the counts
    if (node.dataset && node.dataset.ckHero === "events") { const lev = isLevFeed(sky); node.hidden = lev; if (lev) { node.replaceChildren(); return; } }
    if (c.failed) { node.appendChild(document.createTextNode(" · ")); node.appendChild(el("span", "bad", c.failed + " failed")); }
  }
  // An ambient panel whose sky is already mounted takes new events through the mount's own update(), so the
  // animation keeps running instead of restarting. True when it did; false leaves the paint to remount.
  function updateSky(s, sky) {
    const k = s.skyParts;
    if (!k || k.size !== skySize() || !s.skyHandle || typeof s.skyHandle.update !== "function" || !k.plot.isConnected || !s.body.contains(k.plot)) return false;
    try { s.skyHandle.update({ events: sky.events, legend: sky.legend, dialect: sky.dialect }); } catch (_) { return false; }
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
      try { s.skyHandle = root.LevainSky.mount(plot, { events: sky.events, legend: sky.legend, dialect: sky.dialect }, hero ? { legend: false, size: skySize(), head: true, toggleHost: document.querySelector("header.masthead .indicators"), toggleBefore: document.getElementById("jumpbtn"), onHead: paintEventFigures } : { legend: false, size: skySize(), head: true, onHead: paintEventFigures }); } catch (e) { plot.appendChild(el("p", "err", "sky failed: " + (e && e.message ? e.message : e))); }
    } else plot.appendChild(el("p", "xnote", "the sky draws here once sky.js is loaded"));
    const legend = el("div", "legendbar" + (hero ? "" : " frame"));
    paintLegend(legend, sky);
    // the hero's key is a full-width band BELOW the hero (mock-e), not a row inside the sky column. The band is the
    // page's mount, which renderHero owns: this hands the legend to it (place) and never writes the live mount
    // itself. A sky anywhere else, or a page without the mount (place answers false), keeps it under its plot.
    if (!(hero && typeof hero.place === "function" && hero.place(legend))) host.appendChild(legend);
    s.skyParts = { plot: plot, win: win, legend: legend, size: skySize() };
  }

  // The strip chart's key: the real glyphs, drawn like the chart's own marks (sky.js). Page chrome, not data:
  // the kernel's `legend` sentence stays as the tooltip. Orange is reserved for failed and now, so the diamond is neutral.
  // The key names only the kinds sky.js draws apart. A feed carrying any other kind gets the kernel's sentence as the
  // whole legend and no glyph key, because sky.js draws an unknown kind with the memory square.
  const DRAWN_KINDS = new Set(["commit", "review", "decision", "memory"]);
  function paintLegend(host, sky) {
    host.replaceChildren();
    host.title = sky.legend || "";
    // a Levain feed has its own key (sky.js), and the mock's hint beside it
    if (isLevFeed(sky) && root.LevainSky && typeof root.LevainSky.legendNode === "function") {
      host.appendChild(root.LevainSky.legendNode());
      host.appendChild(el("span", "lg-sp"));
      host.appendChild(el("span", "lg-note", "Hover a mark for its time, kind and repo."));
      return;
    }
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
      data.kind, !!data.typed, data.error || "", data.markdown != null, data.note || "", data.footer || "",
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
    s.carry = root.LevainDashboard && root.LevainDashboard.buildKept ? root.LevainDashboard.buildKept() : null;
    s.shape = shapeOf(ctx && ctx.entry ? ctx.entry : { title: s.title }, data);
    if (data.kind === "count-queue") {
      // the figure is data, not chrome: paint() keeps it current
      const big = el("div", "cq");
      s.cqN = el("span", "cq-n num", "");
      big.appendChild(s.cqN);
      big.appendChild(el("span", "cq-l", "waiting"));
      p.appendChild(big);
    }
    // the focus view of a long metric list carries the same filter box a long list does (and starts unfiltered: a saved query would hide metrics)
    const metricsOf = data.kind === "status-dots" ? data.metrics : data.kind === "metric-grid" ? data.numbers : null;
    // a board (the board kind) always carries the box, on the panel face and in its focus view: it is as long as its queue is
    s.filterable = (!!(ctx && ctx.modal) && !(ctx && ctx.widget) && Array.isArray(metricsOf) && metricsOf.length >= FILTER_MIN)
      || (data.kind === "board" && !(ctx && ctx.widget));
    // a fresh open starts unfiltered; the SAME open modal rebuilt by a refresh keeps the query being typed in it
    // (a count-face panel's modal is the same: a saved query would hide a newly waiting row behind "1 of 3"); a board's panel face
    // keeps its own the same way
    if (s.filterable || (s.searchable && s.modal && data.face === "count")) {
      const prev = Array.from(states).find((o) => !!o.modal === !!s.modal && o.pid === data.pid && o.body.isConnected && (o.filterable || o.searchable));
      s.query = prev ? prev.query : "";
    }
    if (s.searchable || s.filterable) p.appendChild(buildFilter(s));
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
        s.filterInput.focus({ preventScroll: true });   // the box was where the reader left it; focusing must not move the page
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

  // every repaint (a poll, a search, a held refetch, a first fill) can empty or fill a card: the marking follows each one, once per tick
  let emptyQueued = false;
  function paint(s) {
    paintBody(s);
    if (emptyQueued) return;
    emptyQueued = true;
    root.setTimeout(() => { emptyQueued = false; markEmptyCards(); }, 0);
  }
  // The board kind (board_view.js): grouped under column headings on the page, the six columns in the focus modal (the same two
  // renderers the Board tab and its modal use). A page without the view draws one plain line, never a blank panel.
  function paintBoard(s) {
    const V = root.LevainBoardView;
    if (!V || typeof V.boardModel !== "function" || typeof V.renderGrouped !== "function" || typeof V.renderBoard !== "function"
        || typeof V.filterModel !== "function" || typeof V.countLabel !== "function") {
      s.body.appendChild(el("p", "xnote", "The board view did not load, so this board is not drawn."));
      return;
    }
    try {
      // the box narrows the cards, never the columns; its count is the whole board's size, or "k of n" while a query is typed
      const f = V.filterModel(V.boardModel(s.data.board), s.query);
      if (s.countEl) s.countEl.textContent = V.countLabel(f);
      if (s.modal) V.renderBoard(document, s.body, f.model); else V.renderGrouped(document, s.body, f.model);
    } catch (e) {
      s.body.replaceChildren(el("p", "err", "unavailable — the board could not be drawn"));
    }
  }
  function paintBody(s) {
    const d = s.data, sr = d.search || {};
    if (d.kind === "ambient" && !s.fault && updateSky(s, d.sky)) return;
    dropSky(s);
    s.body.replaceChildren();
    if (s.fault) {
      s.body.appendChild(el("p", "err", "unavailable — " + s.fault));
      // a faulted tile has nothing behind it: it is no button until a good repaint says otherwise
      if (s.ctx.widget) { s.units = 0; s.hidden = 0; s.clamp = false; s.body.classList.remove("clamped"); publishOpenable(s); }
      return;
    }
    if (s.cqN) s.cqN.textContent = String(isNum(d.count) ? d.count : (d.lines || []).length);
    if (s.ctx.widget) {
      if (!s.fitting && s.p && s.body.isConnected) fitWidget(s); else paintWidget(s, s.body);
      return;
    }
    if (s.filterable && d.kind !== "board") { paintFiltered(s); return; }
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
    if (d.kind === "board") { paintBoard(s); return; }
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
    else if (!items.length) s.body.appendChild(el("p", "empty" + (store && !s.results ? " loading" : ""), store && !s.results ? "searching…" : (terms.length ? "no match" : d.empty)));
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
    if (s.modal && d.footer) s.body.appendChild(el("p", "xnote panel-foot", d.footer));
  }

  function setQuery(s, q, now) {
    s.query = q;
    s.carry = null;   // what a draw held belongs to the query it was drawn under
    for (const o of states) if (o.pid === s.pid) o.carry = null;
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
  // every live copy of a panel (the board and its focus modal) asking the same query shares one read, so two copies
  // never spend both of the kernel's search slots on one question
  const searchShared = new Map();
  function sharedSearch(pid, q) {
    const k = pid + "\u0000" + q;
    let p = searchShared.get(k);
    if (!p) {
      p = readPanel(pid, "&q=" + encodeURIComponent(q) + "&limit=" + STORE_LIMIT);
      searchShared.set(k, p);
      const drop = () => { if (searchShared.get(k) === p) searchShared.delete(k); };
      p.then(drop, drop);
    }
    return p;
  }
  async function runStoreSearch(s) {
    const q = sendable(s.query);
    const seq = ++s.seq;
    if (!q) { s.results = null; repaintKeeping(s); return; }
    let out;
    try {
      const raw = await sharedSearch(s.pid, q);
      out = mapSearch(s, clean(raw));
    } catch (e) { out = { items: [], hays: [], label: "", error: e && e.message ? e.message : String(e) }; }
    if (seq !== s.seq || !onPage(s)) return;     // a newer query, or the panel is gone
    s.results = out;
    repaintKeeping(s);
  }
  // A repaint of a body already on the page, handing back what the reader holds in it: the rows they opened and the
  // scroll, settled in the order the board uses. A panel filled during a board or modal draw also owes what that draw
  // captured, since its rows arrive after the draw's own restore. Only a body still holding no rows takes it (rows on
  // the page mean the reader's own state is the live one), and it is spent once a repaint puts rows there.
  function repaintKeeping(s) {
    const core = root.LevainDashboard;
    const host = (s.body.closest ? s.body.closest(".pbody, .modal-body") : null) || s.body;
    const kept = core && core.captureKept ? core.captureKept(host) : null;
    const hadRows = !!(s.body.querySelector && s.body.querySelector(".row"));
    if (hadRows || (s.carry && Date.now() - s.born >= 15000)) s.carry = null;
    if (kept && s.carry) {
      for (const k of s.carry.open) kept.open.add(k);
      for (const [k, v] of s.carry.scroll) if (!kept.scroll.has(k)) kept.scroll.set(k, v);
    }
    paint(s);
    if (s.body.querySelector && s.body.querySelector(".row")) s.carry = null;
    if (core && core.settleKept) core.settleKept(host, kept);
    else if (core && core.measureClauses) core.measureClauses(host);   // a core without the keeper still re-measures its rows
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
  // bumped by every board render (K.reset): a panel read begun before one cannot repaint what that render drew
  let boardEpoch = 0;
  const liveOf = (pid) => Array.from(states).filter((s) => s.pid === pid && onPage(s));
  async function refetchPanel(pid) {
    if (!liveOf(pid).length) return false;
    // a scrubbed past (topology_view.js Replay) pauses the topology's live refetch: answered without a read; Live reads it again
    if (pid === "topology" && root.LevainTopologyView && root.LevainTopologyView.paused && root.LevainTopologyView.paused()) return "applied";
    const gen = (refetchGen.get(pid) || 0) + 1;
    refetchGen.set(pid, gen);
    const epoch = boardEpoch;
    let raw, fault = "";
    try { raw = await readPanel(pid); } catch (e) { fault = e && e.message ? e.message : String(e); }
    if (refetchGen.get(pid) !== gen) return "superseded";   // a newer refetch of this panel owns the answer and the record
    // a full render since drew this panel from a read that may be older or newer than this one: the panel is not
    // painted from this read, and `false` asks the caller to draw the board again (the stream reloads)
    if (boardEpoch !== epoch) return false;
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
      repaintKeeping(s);   // a body repainted in place keeps the rows the reader expanded and its scroll position
      const sr = data.search || {};
      if (s.searchable && sr.scope === "store" && sendable(s.query)) runStoreSearch(s);
    }
    if (core.adoptPanel) core.adoptPanel(pid, view);
    // the page now shows what this read returned, so the held etag is this read's own (never a stream event's)
    const heldMap = root.LevainCockpitHeld;
    if (heldMap && raw && typeof raw.etag === "string") heldMap[pid] = Object.assign({}, heldMap[pid], { etag: raw.etag });
    return "applied";
  }

  // ---- the theme (F6): one token set, two themes, three choices. System (no choice: dashboard.css follows prefers-color-scheme),
  // dark, light. A choice is kept per browser, never anywhere an agent reads; `?theme=light|dark` is a one-visit override that is not
  // kept. A browser that refuses storage still switches for the page's life.
  const THEME_KEY = "levain_cockpit_theme";
  const isTheme = (t) => t === "light" || t === "dark";
  function storedTheme() { try { const t = root.localStorage.getItem(THEME_KEY); return isTheme(t) ? t : ""; } catch (_) { return ""; } }
  function queryTheme() {
    try { const t = new URLSearchParams(root.location.search).get("theme"); return isTheme(t) ? t : ""; } catch (_) { return ""; }
  }
  const pageRoot = () => (typeof document !== "undefined" && document.documentElement) || null;   // a DOM without an <html> (the test fake) has no theme to set
  // the choice the page is under right now: "light", "dark", or "system" when neither is set
  function chosenTheme() {
    const t = pageRoot() && pageRoot().dataset ? pageRoot().dataset.theme : "";
    return isTheme(t) ? t : "system";
  }
  function paintThemeSwitch() {
    const now = chosenTheme();
    for (const b of document.querySelectorAll("[data-theme-set]")) b.setAttribute("aria-pressed", b.dataset.themeSet === now ? "true" : "false");
  }
  function setTheme(t, keep) {
    const html = pageRoot();
    if (html && html.dataset) { if (isTheme(t)) html.dataset.theme = t; else delete html.dataset.theme; }
    if (keep) { try { if (isTheme(t)) root.localStorage.setItem(THEME_KEY, t); else root.localStorage.removeItem(THEME_KEY); } catch (_) { /* the page still switched */ } }
    paintThemeSwitch();
  }
  // ---- Calm: a stored choice sets html.calm before the body paints; the page's controls and the three motion readers follow it
  const CALM_KEY = "levain_cockpit_calm";
  function storedCalm() { try { return root.localStorage.getItem(CALM_KEY) === "1"; } catch (_) { return false; } }
  function paintCalmSwitch() {
    const on = isCalm();
    for (const b of document.querySelectorAll("[data-calm-toggle]")) {
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.textContent = on ? "Calm on" : "Calm off";
      b.setAttribute("aria-label", b.textContent);     // the phone shows an icon only: its name is this
      b.title = b.textContent;
    }
  }
  function setCalm(on, keep) {
    const html = pageRoot();
    if (html && html.classList) { if (on) html.classList.add("calm"); else html.classList.remove("calm"); }
    if (keep) { try { if (on) root.localStorage.setItem(CALM_KEY, "1"); else root.localStorage.removeItem(CALM_KEY); } catch (_) { /* the page still switched */ } }
    if (ageTimer) { root.clearInterval(ageTimer); ageTimer = root.setInterval(tickAges, on ? CALM_AGE_TICK_MS : AGE_TICK_MS); }
    if (typeof root.dispatchEvent === "function" && typeof root.Event === "function") root.dispatchEvent(new root.Event("levain-calm"));
    paintCalmSwitch();
  }
  // run as this file loads (it is in the <head>): the theme is set before the body paints, so a stored light theme never flashes dark
  (function earlyTheme() {
    const html = pageRoot();
    const t = queryTheme() || storedTheme();
    if (html && html.dataset && t) html.dataset.theme = t;
    if (html && html.classList && storedCalm()) html.classList.add("calm");
  })();
  // ---- the layout (K5): the order of a zone's cards and which panels are folded, kept per browser under one key, the way the theme is.
  // {"v":1,"zones":{"<zone id>":{"order":["<card key>",...]}},"collapsed":["<card key>",...]}; a card's key is its first panel id, and a folded card folds all its panels.
  // Anything that is not that shape is ignored part by part, and a browser that refuses storage draws the kernel order.
  const LAYOUT_KEY = "levain_cockpit_layout_v1";
  const strings = (a) => (Array.isArray(a) ? a.filter((s) => typeof s === "string" && s) : []);
  // The page's own copy of the last layout written: when the store refuses a write (Safari private, quota) it is the truth for the page's life.
  let memLayout = null, memStale = false;
  const copyLayout = (l) => { const zones = Object.create(null); for (const z of Object.keys(l.zones)) zones[z] = l.zones[z].slice(); return { zones, collapsed: l.collapsed.slice() }; };
  function readLayout() {
    if (memStale && memLayout) return copyLayout(memLayout);
    const out = { zones: Object.create(null), collapsed: [] };
    let v = null;
    try { v = JSON.parse(root.localStorage.getItem(LAYOUT_KEY)); } catch (_) { return out; }
    if (!v || typeof v !== "object" || v.v !== 1) return out;
    if (v.zones && typeof v.zones === "object") {
      for (const z of Object.keys(v.zones)) {
        const o = v.zones[z];
        if (o && typeof o === "object" && Array.isArray(o.order)) out.zones[z] = strings(o.order);
      }
    }
    out.collapsed = strings(v.collapsed);
    return out;
  }
  function writeLayout(l) {
    const zones = {};
    for (const z of Object.keys(l.zones)) zones[z] = { order: l.zones[z] };
    memLayout = copyLayout(l);
    try { root.localStorage.setItem(LAYOUT_KEY, JSON.stringify({ v: 1, zones, collapsed: l.collapsed })); memStale = false; } catch (_) { memStale = true; }
  }
  // A store that refuses the removal still holds the old copy: the page then keeps an EMPTY layout in memory (the stored one is never read
  // back) and drops it only after a removal that worked.
  function resetLayout() {
    try { root.localStorage.removeItem(LAYOUT_KEY); memLayout = null; memStale = false; }
    catch (_) { memLayout = { zones: Object.create(null), collapsed: [] }; memStale = true; }
  }
  // the stored names first, in their stored order; every key the page has and the store does not name follows in the order it came
  function layoutOrder(zoneId, keys) {
    const stored = readLayout().zones[zoneId] || [];
    const have = new Set(keys), seen = new Set(), head = [];
    for (const k of stored) if (have.has(k) && !seen.has(k)) { seen.add(k); head.push(k); }
    return head.concat(keys.filter((k) => !seen.has(k)));
  }
  const layoutCollapsed = () => new Set(readLayout().collapsed);
  // true when anything is saved (a stored order or a collapsed panel): the one condition under which a Reset layout control is drawn
  function hasLayout() { const l = readLayout(); return l.collapsed.length > 0 || Object.keys(l.zones).length > 0; }
  function setLayoutOrder(zoneId, order) { const l = readLayout(); l.zones[zoneId] = order; writeLayout(l); }
  function setLayoutCollapsed(panelId, on) {
    const l = readLayout(), s = new Set(l.collapsed);
    if (on) s.add(panelId); else s.delete(panelId);
    l.collapsed = Array.from(s);
    writeLayout(l);
  }
  // ---- the modes and the keys ----
  // A tab named in the address: `#morning|#board|#build|#memory`, alone or beside other fragment parts (`#write_token=...&board`).
  // A bare name may arrive spelled `name=`: dashboard_boot.js strips the write token through URLSearchParams, which writes every
  // part it keeps as `key=value`. The name is read with that suffix removed and, when the address is rewritten, written bare.
  const hashParts = () => String((root.location && root.location.hash) || "").replace(/^#/, "").split("&").filter(Boolean).map((p) => (MODES.indexOf(p.replace(/=$/, "")) >= 0 ? p.replace(/=$/, "") : p));
  function hashMode() { return hashParts().find((p) => MODES.indexOf(p) >= 0) || ""; }
  // the address follows the tab the reader chose; any other fragment part stays
  function writeHash(m) {
    try { root.history.replaceState(null, "", root.location.pathname + root.location.search + "#" + hashParts().filter((p) => MODES.indexOf(p) < 0).concat(m).join("&")); }
    catch (_) { /* an address that cannot be rewritten is still a working page */ }
  }
  // `fromReader`: the tab was chosen by the reader (a click, a key, the jump box), so the address follows; the first paint does not write it
  // the sky is mounted at the size its tab wants; a tab switch remounts the ones drawn at the other size
  function resizeSkies() {
    for (const s of Array.from(states)) {
      if (!s.skyParts || !s.skyHandle || !s.data || !s.data.sky || !s.body.isConnected || skyNow(s)) continue;
      try { dropSky(s); s.body.replaceChildren(); paintSky(s, s.body, s.data.sky); } catch (_) { /* the next paint remounts */ }
    }
  }
  function applyMode(m, fromReader) {
    if (MODES.indexOf(m) < 0) m = "board";
    document.body.dataset.mode = m;
    save(MODE_KEY, m);
    if (fromReader) writeHash(m);
    for (const b of document.querySelectorAll("[data-mode-tab]")) b.setAttribute("aria-selected", b.dataset.modeTab === m ? "true" : "false");
    resizeSkies();
    fillGrids();
    // the header lens is on every tab but Memory (going to Memory ends its replay and stops its stream loop), the Sessions card on Build only
    try { paintHeaderLens(); paintBuildSessions(); } catch (_) { /* the card repaints on the next poll */ }
    // the layout controls read what the tab shows: any tab change, by click, key, address or reveal, asks them to refresh
    if (typeof document.dispatchEvent === "function") document.dispatchEvent(new Event("ck-layout-refresh"));
  }
  // the first tab that shows a panel: the one it is on if it shows it, else Board if it does, else the first it names. A node with no
  // tab list (page chrome) is on every tab.
  function revealTab(node) {
    const tabs = ((node && node.dataset && node.dataset.modes) || "").split(" ").filter(Boolean);
    if (document.body.dataset.mode && tabs.length && tabs.indexOf(document.body.dataset.mode) < 0) applyMode(tabs.indexOf("board") >= 0 ? "board" : tabs[0], true);
  }
  // The grids are laid out from what a mode shows: the glance is a strip sharing its row (tiles from four), and a zone grid is six
  // tracks under three, four under two, one under one.
  function shown(n) { return getComputedStyle(n).display !== "none"; }
  // A card with nothing to list is its header and the one empty-state line, not a populated card's height with a gap in it.
  // Decided at render (this runs after each one): a card the person is filtering keeps its height, since the filter is the
  // reason the list is short and a collapsing card would jump under the cursor.
  function markEmptyCards() {
    for (const p of document.querySelectorAll(".ck-zgrid .panel")) {
      const none = !!p.querySelector(".pbody .empty") && !p.querySelector(".xfilter, .pbody .row, .pbody .rrow, .pbody .lrow, .pbody .section-display, .pbody .ep-search-input");
      p.classList.toggle("ck-is-empty", none);
      // an empty card folds into a slim bar at the end of its zone, unless it has a compose box or an editor behind an Edit link on it
      // (Tray, Keep, Inbox, an empty identity card) or tabs (Cognition trace): they are its content, so it stays a card
      p.classList.toggle("ck-fold", none && !p.querySelector(".tray-dump, .ck-tabs, .edit-trigger-row"));
    }
  }
  // ---- project colour: repo tags on rows that name a repo, and the Decisions signal ----
  // The repos are the install's own: every `repo` its data carries (a row's `repo` facet, an ambient event's `repo`),
  // collected as panels are read into the sky's one registry (sky.js `noteRepo`), which also assigns their hues. The package
  // names none, so an install whose data names no repo shows no tags.
  function noteRepos(panels) {
    const sky = root.LevainSky;
    if (!sky || !sky.noteRepo) return;
    const note = (n) => { if (typeof n === "string") sky.noteRepo(visible(n)); };
    for (const p of Object.values(panels || {})) {
      if (!isObj(p)) continue;
      for (const r of Array.isArray(p.rows) ? p.rows : []) if (isObj(r) && isObj(r.facets)) note(r.facets.repo);
      const ev = isObj(p.value) && Array.isArray(p.value.events) ? p.value.events : [];
      for (const e of ev) if (isObj(e)) note(e.repo);
    }
  }
  // A row names a repo when its own words do: the whole name, bounded by anything but a letter, digit, `_` or `-` (so
  // `my-app` is not inside `my-apps`, while a path such as `x/my-app/y` still names it). The longest name is tried first,
  // so `anneal-memory` wins over `anneal`. The registry only grows, so its size says when the list must be rebuilt.
  let repoMatchers = [], matchersFor = -1;
  function matchersOf(sky) {
    const names = sky && sky.repos ? sky.repos() : [];
    if (names.length !== matchersFor) {
      const esc = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      repoMatchers = names.slice().sort((a, b) => b.length - a.length)
        .map((n) => [n, new RegExp("(?:^|[^\\w-])" + esc(n) + "(?![\\w-])", "i")]);   // no lookbehind: Safari before 16.4 throws on one
      matchersFor = names.length;
    }
    return repoMatchers;
  }
  // The work lists' rows (Tray, Open loops, Keep, Now) carry the repo tag in the meta line, after the id, the date and the age, as the
  // mock draws it; every other row keeps it at the head of its text. `tagHome` is where that tag lives for a row.
  const META_TAG_PANELS = ["tray", "loops", "keep", "now"];
  function tagHome(row, clause) {
    const panel = row.closest("[data-ck-panel]");
    const inMeta = !!panel && META_TAG_PANELS.indexOf(panel.dataset.ckPanel) >= 0 && row.classList.contains("row");
    return { inMeta: inMeta, host: inMeta ? row : clause };
  }
  function tintRepos() {
    const sky = root.LevainSky;
    const matchers = matchersOf(sky);
    for (const row of document.querySelectorAll(".ck-zgrid .row")) {
      const clause = row.querySelector(":scope > .clause");
      if (!clause) continue;
      const home = tagHome(row, clause);
      if (home.inMeta && row.querySelector(":scope > .k7-repo")) continue;   // a chip already names the repo (Build's loops)
      // decided again on every pass, on the row's words without its own tag: a repo noted later can be the longer name
      // the row means, or move a hue slot
      const had = home.host.querySelector(":scope > .rp-tag:not(.k7-repo)");
      // a row with a structured `repo` facet the sky's registry knows is tagged from it alone, in the registry's
      // spelling (so its length and count are the registry's bounds); any other row is read by its words
      const facet = (row.dataset.repoFacet || "").toLowerCase();
      const known = facet ? matchers.find(([n]) => n.toLowerCase() === facet) : undefined;
      let hit;
      if (known) hit = [known[0]];
      else {
        // the pieces of the row are joined by a space: a badge ("handoff") and the text that follows it are not one word
        // and without the tag it already wears, wherever it sits (a direct child of the row, or inside the clause): its own words
        // are not evidence of the repo, and a tag glued to the clause's first word would hide a match
        const bare = row.cloneNode(true);
        for (const t of bare.querySelectorAll(".rp-tag:not(.k7-repo)")) t.remove();
        let text = Array.from(bare.children).map((c) => c.textContent).join(" ");
        hit = matchers.find(([, re]) => re.test(text));
      }
      const key = hit && sky && typeof sky.hueKey === "function" ? sky.hueKey(hit[0]) : "";
      if (had && key && had.textContent === hit[0]) { if (had.dataset.repo !== key) had.dataset.repo = key; continue; }
      if (!had && !key) continue;
      if (had) had.remove();
      if (key) {
        const tag = el("span", "rp-tag", hit[0]);
        tag.dataset.repo = key;
        if (home.inMeta) row.insertBefore(tag, row.querySelector(":scope > .verb-actions"));   // null: appended
        else clause.insertBefore(tag, clause.firstChild);
      }
      // the tag changes the clause's length after the clause toggles were measured: measure this row again
      if (clause.clientHeight && clause.classList.contains("clampable") && !row.classList.contains("row-expanded")) row.classList.toggle("has-more", clause.scrollHeight > clause.clientHeight);
    }
    // Build's repo chips (session rows, Open loops rows) take the sky's hue for a repo its registry knows, and stay neutral otherwise
    for (const c of document.querySelectorAll(".k7-repo[data-repo-facet]")) {
      const facet = (c.dataset.repoFacet || "").toLowerCase();
      const known = facet ? matchers.find(([n]) => n.toLowerCase() === facet) : undefined;
      const key = known && sky && typeof sky.hueKey === "function" ? sky.hueKey(known[0]) : "";
      if (key) { c.classList.add("rp-tag"); if (c.dataset.repo !== key) c.dataset.repo = key; }
    }
    // the Decisions tile glows only while something waits on the operator
    for (const w of document.querySelectorAll('.ck-card.w[data-ck-panel="decisions"]')) {
      const big = w.querySelector(".big[data-count]");
      const n = big ? Number(big.dataset.count) : 0;
      if (n > 0) w.dataset.signal = "on"; else delete w.dataset.signal;
    }
  }
  // a panel that refetches on its own repaints its rows after fillGrids has run, so the tags are laid again on a slow tick
  if (typeof document !== "undefined" && document.documentElement) root.setInterval(() => { if (!document.hidden) { tintRepos(); paintBuild(); } }, 1500);
  // ---- K7: the Build tab ----
  // Sessions is a count tile on Morning and Board; on Build it is a large card of its own, drawn here from the same panel
  // state (so one read feeds both) into #k7-sessions, with the headings the Build tab carries. Nothing here reads a store.
  const K7_NONE_SESSIONS = "No agent session yet: start Claude Code or Codex in a workspace.";
  const K7_NONE_RESULTS = "No results yet: finished jobs and checks appear here.";
  let k7Sig = "";
  function k7Time(iso) {
    const t = parseIso(iso);
    return isNaN(t) ? "" : new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  function k7Ago(iso, now) {
    const t = parseIso(iso);
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.floor((now - t) / 1000));
    return s < 60 ? s + " s ago" : s < 3600 ? Math.floor(s / 60) + " m ago" : s < 86400 ? Math.floor(s / 3600) + " h ago" : Math.floor(s / 86400) + " d ago";
  }
  const k7K = (n) => (n >= 1000 ? Math.round(n / 1000) + "K" : String(n));
  // the context meter: how much of the model's window (Codex states it) or of the operator's warn line (Claude Code does not)
  // the session holds. A session at or past the warn line (either agent, not compacted) says OVER in words and in red: that
  // reads on a screenshot and on touch, and is not the orange of a session waiting on a decision. Never a number the
  // transcript did not give.
  function k7Meter(v, waiting) {
    const m = el("div", "k7-meter");
    const head = el("div", "k7-meter-h");
    head.appendChild(el("span", "k7-meter-l", "CONTEXT"));
    const bar = el("div", "k7-bar");
    const fill = el("i");
    bar.appendChild(fill);
    let label = "not read", frac = 0, over = false;
    if (isNum(v.tokens) && v.compacted) { label = "compacted"; }
    else if (isNum(v.tokens) && v.tokens > 0) {
      const hasWin = isNum(v.window) && v.window > 0;
      const wall = hasWin ? v.window : (isNum(v.warn) && v.warn > 0 ? v.warn : 0);
      frac = wall ? Math.min(1, v.tokens / wall) : 0;
      // Codex states its window: a percentage of it. Claude Code does not: the bar is measured against the warn line, and says so
      label = hasWin ? Math.round((v.tokens / v.window) * 100) + "%" : (wall ? k7K(v.tokens) + " of " + k7K(wall) + " warn" : k7K(v.tokens));
      over = isNum(v.warn) && v.warn > 0 && v.tokens >= v.warn;
    }
    fill.style.width = Math.round(frac * 100) + "%";
    if (over) head.appendChild(el("span", "k7-over", "OVER"));
    head.appendChild(el("span", "k7-meter-v", label));
    m.append(head, bar);
    if (typeof v.context === "string" && v.context) m.title = v.context;
    if (waiting) m.classList.add("waiting");
    if (over) m.classList.add("over");
    if (label === "not read") m.classList.add("unread");
    return m;
  }
  function k7Limits(v) {
    const l = v.limits;
    if (!isObj(l)) return null;
    // the blocking fact first (a halted session), then that nobody is counting; a session nobody counts is never drawn as healthy
    const parts = [];
    let tone = "ok";
    if (typeof l.halted === "string" && l.halted) { parts.push("halted: " + l.halted); tone = "over"; }
    if (l.counting !== true) { parts.push("limits: not counting"); tone = "over"; }
    if (!parts.length && typeof l.metric === "string" && l.metric) { parts.push(l.metric); tone = l.status === "over" || l.status === "amber" ? l.status : "ok"; }
    return parts.length ? el("div", "k7-limits " + tone, parts.join(" \u00b7 ")) : null;
  }
  function k7Row(ln, now) {
    const v = ln.sess, live = v.active === true;
    const waiting = typeof v.waiting === "string" && v.waiting !== "";
    const row = el("div", "k7-srow" + (live ? " live" : " quiet") + (waiting ? " waiting" : ""));
    const dot = el("i", "k7-sdot " + (live ? "live" : "quiet"));
    dot.setAttribute("aria-hidden", "true");
    const who = el("div", "k7-who");
    const top = el("div", "k7-who-t");
    top.appendChild(el("b", "k7-agent", v.agent || "agent"));
    if (ln.repo) { const chip = el("span", "k7-repo", ln.repo); chip.dataset.repoFacet = ln.repo; top.appendChild(chip); }
    who.append(top, el("div", "k7-cwd", v.cwd || ""));
    const nowc = el("div", "k7-now");
    let said;
    let tick = null;      // [prefix, iso]: an "N s ago" the page keeps current in place, without drawing the card again
    if (waiting) said = "waiting on a decision (" + v.waiting + (isNum(v.waiting_n) && v.waiting_n > 1 ? " +" + (v.waiting_n - 1) : "") + ")";
    else if (live) { tick = v.tool ? [v.tool + " \u00b7 ", v.tool_at] : ["last hook ", v.last_seen]; said = tick[0] + k7Ago(tick[1], now); }
    else said = v.tool ? "last tool " + k7Time(v.tool_at) : "last hook " + k7Time(v.last_seen);
    const toolEl = el("div", "k7-tool", said);
    if (tick) { toolEl.dataset.k7Pre = tick[0]; toolEl.dataset.k7At = tick[1]; }
    nowc.appendChild(toolEl);
    const lim = k7Limits(v);
    if (lim) nowc.appendChild(lim);
    // "ended" would claim what no hook announces: a quiet session says when it last fired one
    row.append(dot, who, nowc, k7Meter(v, waiting), el("div", "k7-when", live ? "since " + k7Time(v.first_seen) : "last active " + k7Time(v.last_seen)));
    return row;
  }
  function k7Host() {
    let sec = document.getElementById("k7-sessions");
    if (sec) return sec;
    const after = document.getElementById("ck-widgets");
    if (!after || !after.parentNode) return null;
    sec = el("section", "k7-sess frame");
    sec.id = "k7-sessions";
    sec.hidden = true;
    after.parentNode.insertBefore(sec, after.nextSibling);
    return sec;
  }
  function k7Heading(title, gloss) {
    const h = el("div", "k7-head");
    h.append(el("h3", null, title), el("span", null, gloss));
    return h;
  }
  // A waiting task or node links to the Needs-you item it waits on: the strip's card with that id is scrolled to and lit (on Build, the bar's
  // chip is). An item with no card (decided, no longer waiting) opens the Decisions focus view, where its row is marked and scrolled to by
  // the card id the row carries (data-card). With no Decisions panel on the page, the Board tab is shown instead.
  function openNeedsYouCard(card) {
    const N = root.LevainNeeds;
    if (N && N.go(card)) return;   // the strip's card (or, on Build, its chip) is lit; the strip is on the page only while the item waits
    const mark = (tries) => {
      const row = Array.from(document.querySelectorAll("[data-card]")).find((r) => r.dataset.card === card && !r.classList.contains("tv-open"));
      if (row) { row.classList.add("tv-lit"); if (row.scrollIntoView) row.scrollIntoView({ block: "center" }); return; }
      if (tries > 0) root.setTimeout(() => mark(tries - 1), 150);
    };
    if (N && N.history()) mark(12);
    else applyMode("board", true);
  }
  // The topology panel's state, read once for the header lens and the Sessions card alike. The panel is hidden on the page (the header draws it), so a
  // fault is the header's to say, three ways: a failed fetch (ts.fault), an error record the kernel sent (data.error), or the page's own
  // "unavailable" card (an external panel, no state of its own).
  function topoInfo() {
    const TV = root.LevainTopologyView;
    if (!TV) return { TV: null };
    const ts = Array.from(states).find((o) => o.pid === "topology" && o.p && o.p.isConnected) || null;
    const ep = !ts ? document.querySelector('[data-ck-panel="topology"][data-ck-external]') : null;
    const epErr = ep ? ep.querySelector(".err") : null;
    const tfault = ts ? (ts.fault || (ts.data && typeof ts.data.error === "string" ? ts.data.error : "")) : (epErr ? epErr.textContent : "");
    const tvLive = ts && !tfault && ts.data && ts.data.topology && TV.hasWork(ts.data.topology) ? ts.data.topology : null;
    // the lens in force: the stored choice, but an install with no topology to draw has only its Event graph
    const lens = tvLive || tfault ? TV.headerLens() : "events";
    // the header is on every tab but Memory; replay lives only while a topology lens is on screen with a replay path (the head's flag and the page's transport)
    const shown = !!(tvLive || tfault) && lens !== "events" && !!document.body && document.body.dataset.mode !== "memory";
    const canReplay = !!(shown && tvLive && ts && ts.data && ts.data.replay === true && root.LevainCockpitTransport);
    if (TV.replaying && TV.replaying() && !canReplay) TV.leaveReplay(true);   // on any other exit it ends BEFORE a value is read, so nothing is drawn from the past without its badge
    // a scrubbed past is drawn in place of the live value (the read behind it may hold no work: an empty past is still the past)
    const tv = tvLive && TV.replaying && TV.replaying() ? (TV.replayValue() || tvLive) : tvLive;
    return { TV, ts, ep, tfault, tvLive, tv, lens, shown, canReplay };
  }
  // ---- the header lens: the Activity slot's three views [Agents graph | Event graph | Work] ----
  // The Event graph is the page's Activity band (#ck-sky, sky.js), unchanged; the other two are topology_view.js's renderer drawn into #ck-hlens
  // above it. The choice is a view preference (stored per browser by topology_view.js): it never changes what is read.
  let hSig = "";
  // The Event graph's figures (sky.js hands them over each time it rebuilds its head row): drawn big in the lens bar, the head row itself is hidden there.
  let evFig = null;
  function paintEventFigures(parts, hours) {
    if (parts) evFig = { parts, hours };
    const box = document.querySelector("#ck-hlens .tv-evfig");
    if (!box || !evFig) return;
    const out = evFig.parts.map(([n, label]) => {
      const m = el("div", "tv-m");
      m.append(el("b", null, String(n)), el("span", "tv-ml", label));
      return m;
    });
    out.push(el("span", "tv-evwin", "last " + evFig.hours + " h"));
    box.replaceChildren(...out);
  }
  function paintHeaderLens() {
    const host = document.getElementById("ck-hlens"), card = document.getElementById("ck-skycard");
    if (!host || !card) return;
    const I = topoInfo();
    const TV = I.TV;
    if (!TV) return;
    const lens = I.lens;
    card.dataset.hlens = lens;
    const fp = I.ts && I.ts.p ? I.ts.p : I.ep;
    if (fp) fp.classList.toggle("ck-faulted", !!I.tfault);       // a faulted topology panel is the one error panel: the page's hide rule leaves it alone
    const has = !!(I.tvLive || I.tfault);
    if (!has) { if (!host.hidden || host.firstChild) { TV.release(); host.replaceChildren(); host.hidden = true; } hSig = ""; return; }
    if (document.body.dataset.mode === "memory") { TV.release(); hSig = ""; return; }      // the header is not on Memory: nothing runs behind it
    const sig = JSON.stringify([lens, I.tfault, I.tv ? TV.signature(I.tv, lens) : "", I.canReplay]);
    if (sig === hSig && !host.hidden) return;
    hSig = sig;
    host.hidden = false;
    TV.release();                 // the old body's resize observer and stream go with it
    const repaint = () => { hSig = ""; k7Sig = ""; paintHeaderLens(); paintBuildSessions(); };
    const toggle = TV.headerToggle(lens, () => { if (TV.replaying && TV.replaying()) TV.returnToLive(); repaint(); });
    if (lens === "events") {
      // the Event graph keeps the bar the other two lenses draw: the switch, a hairline, then the band's own figures at the same size
      const bar = el("div", "tv-bar hl-bar");
      bar.append(toggle, el("div", "tv-metrics tv-evfig"));
      host.replaceChildren(bar);
      paintEventFigures();
      return;
    }
    let body = null, fail = false;
    if (I.tv) {
      // the Replay strip rides the page's own read path (LevainCockpitTransport.readJson: its auth headers and token handling); resume re-reads the live topology
      const replay = I.canReplay ? { readJson: root.LevainCockpitTransport.readJson, resume: () => refetchPanel("topology") } : undefined;
      try { body = TV.render(I.tv, { lens, lensSeg: toggle, repaint, openCard: openNeedsYouCard, replay }); } catch (e) { fail = true; }
    }
    if (body) { host.replaceChildren(body); return; }
    // a fault, or a throw while drawing: said in words with the switch beside it, so the Event graph is one click away
    const bar = el("div", "tv-bar hl-bar");
    bar.appendChild(toggle);
    host.replaceChildren(bar, el("p", "k7-none err", I.tfault ? "Topology unavailable: " + I.tfault : "The topology could not be drawn."));
    if (fail) hSig = "";
  }
  function paintBuildSessions() {
    const sec = k7Host();
    if (!sec) return;
    const s = Array.from(states).find((o) => o.pid === "sessions" && o.ctx && o.ctx.widget && o.p && o.p.isConnected);
    if (!s || document.body.dataset.mode !== "build") {
      k7Sig = "";            // coming back to Build draws the card again
      if (!s) sec.hidden = true;
      return;
    }
    const lines = (s.data.lines || []).filter((l) => l && isObj(l.sess));
    const now = Date.now();
    const I = topoInfo();
    const TV = I.TV, ts = I.ts, tfault = I.tfault || "", tv = I.tv;
    // the signature is content only: the clock does not repaint the card (a focused button would lose focus), it moves the "ago" words
    const sig = JSON.stringify([s.fault || "", lines.map((l) => [l.sess, l.repo]), s.data.empty || "", I.lens, I.lens === "events" ? tfault : "",
                                tv ? TV.signature(tv, "agents") : (TV && ts && ts.data && ts.data.topology ? TV.signature({ e: ts.data.topology.errors, u: ts.data.topology.unreadable }, "") : "")]);
    if (sig === k7Sig && !sec.hidden) {
      for (const t of sec.querySelectorAll(".k7-tool[data-k7-at]")) {
        const w = t.dataset.k7Pre + k7Ago(t.dataset.k7At, now);
        if (t.textContent !== w) t.textContent = w;
      }
      return;
    }
    k7Sig = sig;
    sec.hidden = false;
    const card = el("div", "panel k7-card");
    const head = el("div", "k7-card-h");
    const live = lines.filter((l) => l.sess.active === true).length;
    const ttl = el("h2", null, "Sessions");
    head.appendChild(ttl);
    if (lines.length) {
      const pill = el("span", "ck-count", String(lines.length));
      head.append(pill, el("span", "k7-sum", live + " live · " + (lines.length - live) + " quiet"));
    }
    head.appendChild(el("span", "k7-sp"));
    if (lines.length) {
      const day = new Date(now).toDateString();
      const today = lines.filter((l) => { const t = parseIso(l.sess.first_seen); return !isNaN(t) && new Date(t).toDateString() === day; }).length;
      head.appendChild(el("span", "k7-stats", "started today " + today));
      const open = el("button", "k7-open", "all details");
      open.type = "button";
      open.title = "open the full session list with each session's transcript";
      open.addEventListener("click", () => s.p.click());
      head.appendChild(open);
    }
    card.appendChild(head);
    // the topology is the header's: this card is the session list, with the topology fault said here only while the Event graph is the header's view
    if (tfault && I.lens === "events") card.appendChild(el("p", "k7-none err", "Topology unavailable: " + tfault));
    if (s.fault) card.appendChild(el("p", "k7-none err", "Sessions unavailable: " + s.fault));
    else if (!lines.length) {
      card.appendChild(el("p", "k7-none", K7_NONE_SESSIONS));
      // an empty graph that could not read a source says so beside the empty line, never in place of it
      const q = TV && ts && ts.data && ts.data.topology ? TV.quietLine(ts.data.topology) : null;
      if (q) card.appendChild(q);
      card.classList.add("k7-lone");
    } else { const body = el("div", "k7-srows"); for (const ln of lines) body.appendChild(k7Row(ln, now)); card.appendChild(body); }
    // Lost if closed (F12c): its own panel, right of the card from 1440 px, below it under that; drawn whenever the topology feed is
    let lost = null;
    try { if (TV && TV.renderLost) lost = tfault ? TV.renderLostFault(tfault) : tv ? TV.renderLost(tv) : null; } catch (e) { lost = null; }
    const wrap = el("div", "k7-sess-wrap" + (lost ? " k7-has-lost" : ""));
    wrap.appendChild(card);
    if (lost) wrap.appendChild(lost);
    sec.replaceChildren(k7Heading("Sessions", "Who is working right now, and how much room they have left."), wrap);
    tintRepos();
  }
  // "Results & edits": the Work zone's own heading, shown on Build only, and the one line a Results card says when no job has
  // ever been recorded (the jobs panel is optional: an install with none has no card to say so)
  function paintBuildZone() {
    const zone = document.querySelector('.ck-zone[data-zone="operate"]');
    if (!zone) return;
    const head = zone.querySelector(".zone-head");
    if (head && !head.querySelector(".k7-zt")) {
      const t = el("div", "zone-title k7-zt", "Results & edits");
      const g = el("span", "zone-gloss k7-zg", "What the sessions produced.");
      head.append(t, g);
    }
    const grid = zone.querySelector(".ck-zgrid");
    if (grid && grid.querySelector('[data-ck-panel="jobs"]')) for (const old of grid.querySelectorAll(".k7-results-none")) old.remove();   // a real Results card replaces the placeholder
    if (grid && !grid.querySelector('[data-ck-panel="jobs"]') && !grid.querySelector(".k7-results-none")) {
      const p = el("div", "panel ck-dash k7-results-none");
      p.dataset.ckPanel = "jobs-none";
      p.dataset.modes = "build";
      const h = el("div", "phead");
      h.appendChild(el("h2", null, "Results"));
      p.append(h, el("div", "pbody", K7_NONE_RESULTS));
      grid.appendChild(p);
    }
  }
  function paintBuild() {
    try { paintHeaderLens(); paintBuildSessions(); paintBuildZone(); } catch (e) { /* a Build tab that cannot paint leaves the page as the tiles drew it */ }
  }

  // The glance is a compact strip (one bordered row of equal tiles) whenever fewer than four tiles show; four or more are full
  // tiles. A zone's grid is told which of its columns have a card on this tab, and a card that would sit alone in a row of a wider grid
  // is stretched to the row: a lone card never leaves a hole beside it. Measured after layout, so it is redone on every tab change.
  function layoutGrid(g) {
    for (const p of g.querySelectorAll(".panel[data-stretch]")) delete p.dataset.stretch;
    const cols = Array.from(g.querySelectorAll(":scope > .ck-col"));
    let live = 0;
    for (const c of cols) {
      const on = Array.from(c.querySelectorAll(".panel")).some(shown);
      c.classList.toggle("ck-col-off", !on);
      if (on) live++;
    }
    if (cols.length) g.dataset.cols = String(live);
    const flat = (p) => p.parentElement === g || (p.parentElement.classList.contains("ck-col") && getComputedStyle(p.parentElement).display === "contents");
    const rows = new Map();
    for (const p of g.querySelectorAll(".panel")) {
      if (!shown(p) || !flat(p)) continue;
      const r = p.getBoundingClientRect();
      const k = Math.round(r.top);
      if (!rows.has(k)) rows.set(k, []);
      rows.get(k).push([p, r.width]);
    }
    const full = g.clientWidth;
    for (const row of rows.values()) if (row.length === 1 && row[0][1] < full * 0.9) row[0][0].dataset.stretch = "1";
  }
  const MIN_TILE_PX = 200;
  function fillGrids() {
    if (typeof getComputedStyle !== "function") return;   // a harness without layout
    markEmptyCards();
    tintRepos();
    paintBuild();
    for (const w of document.querySelectorAll(".ck-widgets")) {
      const n = Array.from(w.children).filter(shown).length;
      // more than four tiles wrap into rows of at most four (6 -> 3 x 2), whatever the tab: a tile never shares a row so narrow it clips its text
      let cols = n > 4 ? (n % 3 === 0 ? 3 : 4) : Math.max(4, n);
      // the Board draws the same separate cards as Morning (Phill 10-22: the Board-only strip, K2 r2 3d5c8660, clipped every sub-row)
      const shape = n < 4 ? "strip" : "tiles";
      // ...and never so many that a tile is narrower than MIN_TILE_PX: the 720px rule covers the phone, this covers the half-screen laptop in between
      const room = w.clientWidth;
      w.dataset.shape = shape;   // before the gap is read: only the tiles shape is a grid with a gap
      // the grid's column gap is room no tile gets: c columns need c * MIN_TILE_PX + (c - 1) * gap
      const gap = parseFloat(getComputedStyle(w).columnGap) || 0;
      if (shape === "tiles" && room > 0) {
        const fit = Math.max(2, Math.floor((room + gap) / (MIN_TILE_PX + gap)));
        cols = Math.max(2, Math.min(cols, fit));
        // P1: never leave a lone tile in the last row (n % cols === 1): take an exact divisor of n in 5..3 that fits, else the
        // largest column count in 5..2 that fits and leaves no lone tile (4 tiles -> one row of 4, or 2 x 2 where only three fit;
        // 5 -> one row of 5, 6 -> 3 x 2, 7 -> 4 + 3)
        if (n >= 4) {
          let pick = 0;
          for (let c = Math.min(5, fit); c >= 3 && !pick; c--) if (n % c === 0) pick = c;
          for (let c = Math.min(5, fit); c >= 2 && !pick; c--) if (n % c !== 1) pick = c;
          if (pick) cols = pick;
        }
      }
      w.style.setProperty("--ck-n", String(cols));
      w.dataset.shape = shape;
      w.dataset.n = String(n);
      const tiles = Array.from(w.children).filter(shown);
      for (const t of tiles) delete t.dataset.last;
      if (tiles.length % 2 === 1) tiles[tiles.length - 1].dataset.last = "1";   // the narrow page is two across: an odd one out takes the row
    }
    for (const g of document.querySelectorAll(".ck-zgrid")) layoutGrid(g);
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
        // a panel the current tab hides is reached through the first tab that shows it
        revealTab(it.node);
        if (it.node.ckShowTab) it.node.ckShowTab();
        it.node.scrollIntoView({ block: "start" }); it.node.setAttribute("tabindex", "-1"); it.node.focus({ preventScroll: true }); return; }
      // a row in a panel on another tab (crystals, a Memory section, an episode list) is reached through that panel's tab, the same way
      revealTab(it.state.p);
      if (it.state.p.ckShowTab) it.state.p.ckShowTab();
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
    // the address wins over the tab kept for this browser tab; an unknown fragment falls back to the kept one, then Board
    applyMode(hashMode() || saved(MODE_KEY) || "board");
    setTheme(queryTheme() || storedTheme(), false);
    if (root.matchMedia) { const mq = root.matchMedia("(prefers-color-scheme: light)"); if (mq.addEventListener) mq.addEventListener("change", paintThemeSwitch); }
    if (typeof root.addEventListener === "function") root.addEventListener("hashchange", () => { const m = hashMode(); if (m) applyMode(m); });
    document.addEventListener("click", (ev) => {
      const b = ev.target && ev.target.closest ? ev.target.closest("[data-theme-set]") : null;
      if (b) setTheme(b.dataset.themeSet, true);
    });
    paintCalmSwitch();
    document.addEventListener("click", (ev) => {
      if (ev.target && ev.target.closest && ev.target.closest("[data-calm-toggle]")) setCalm(!isCalm(), true);
    });
    document.addEventListener("click", (ev) => {
      if (!(ev.target && ev.target.closest && ev.target.closest("[data-layout-reset]"))) return;
      const D = root.LevainDashboard;
      if (D && D.hasUnsavedEdit && D.hasUnsavedEdit()) return;   // the gate comes before the write: a draft or a running job keeps the layout as it is
      resetLayout();
      if (D && D.redraw) root.LevainDashboard.redraw();
    });
    let fillTimer = 0;
    if (typeof root.addEventListener === "function") root.addEventListener("resize", () => { root.clearTimeout(fillTimer); fillTimer = root.setTimeout(fillGrids, 120); });
    document.addEventListener("click", (ev) => {
      const t = ev.target && ev.target.closest ? ev.target.closest("[data-mode-tab],#jumpbtn") : null;
      if (!t) return;
      if (t.id === "jumpbtn") openJump(); else applyMode(t.dataset.modeTab, true);
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
        const hovered = hoverPanel && hoverPanel.isConnected && all.find((i) => hoverPanel.contains(i));
        const near = hovered || all[0];
        ev.preventDefault();
        // a box already on screen takes focus where it is; one off screen (the page's first box, or a hovered panel the
        // keyboard scrolled away from) is scrolled to, so the reader sees what they type
        const r = near.getBoundingClientRect();
        const onScreen = r.bottom > 0 && r.top < (root.innerHeight || document.documentElement.clientHeight);
        near.focus(onScreen ? { preventScroll: true } : undefined);
        near.select();
      } else if (ev.key.length === 1 && "1234".indexOf(ev.key) >= 0) applyMode(MODES["1234".indexOf(ev.key)], true);
    });
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install); else install();
  }

  const pruneModal = () => { for (const o of Array.from(states)) if (o.modal && !o.body.isConnected) teardown(o); };
  Object.assign(K, { paintHeaderLens, paintBuildSessions, paintBuildZone, paintLegend, skyCounts, skySize, fill, fitWidgets, mountClock, refetchPanel, pruneModal, tickAges, ageSpan, AGE_TICK_MS, CALM_AGE_TICK_MS, isCalm, setCalm, ageTimerArmed: () => ageTimer !== 0, reset: () => { boardEpoch++; for (const o of Array.from(states)) if (!(o.modal && o.body.isConnected)) teardown(o); }, fold, termsOf, hits, jumpItems, applyMode, fillGrids, modesOf, TABS_OF_ID, TABS_OF_REGION, setTheme, chosenTheme, layoutOrder, layoutCollapsed, hasLayout, setLayoutOrder, setLayoutCollapsed, resetLayout, hashMode, revealTab });

  const api = { fromManifest, ageLabel, badgeText, visible, clean, refetchPanel, fold };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else { root.LevainCockpitView = api; root.LevainCockpitKinds = K; }
})(typeof globalThis !== "undefined" ? globalThis : this);
