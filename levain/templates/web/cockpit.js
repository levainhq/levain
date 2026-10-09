// levain cockpit.js — the manifest renderer (cockpit design §7, R1 read half).
//
// Draws what the kernel serves, and nothing else: panels in the manifest's region order, rows in the
// server's order under the server's groups, the kernel's counts, badges and provenance label. It never
// sorts, regroups, computes a count, formats a facet or decides a gesture. It MAY filter: a top-N cut
// per group, saying how many rows its own cut hid (the kernel's group count minus the rows drawn).
// DOM construction is textContent/createElement only (no HTML sink), so the page keeps its strict CSP
// and the token in localStorage stays outside any injection path.
(function (root) {
  "use strict";
  const TOP_N = 12;

  // Control and bidi characters become visible <U+XXXX> text, so a kernel-sent field can never move or
  // reorder what the reader sees (design §7).
  function visible(v) {
    // every Unicode Cc/Cf (controls, format and bidi characters) plus the line/paragraph separators,
    // matching the terminal renderer's category test
    return String(v === null || v === undefined ? "" : v).replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu,
      (ch) => "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">");
  }

  function age(iso, now) {
    if (!iso) return "never";
    // an offset-less timestamp is UTC, as in the terminal renderer
    const t = Date.parse(/(Z|[+-]\d\d:?\d\d)$/i.test(iso) ? iso : iso + "Z");
    if (isNaN(t)) return visible(iso);
    const s = Math.max(0, Math.floor((now - t) / 1000));
    if (s >= 86400) return Math.floor(s / 86400) + "d ago";
    if (s >= 3600) return Math.floor(s / 3600) + "h ago";
    if (s >= 60) return Math.floor(s / 60) + "m ago";
    return s + "s ago";
  }

  function make(doc, tag, cls, text) {
    const e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function rowEl(doc, r) {
    const li = make(doc, "li", "ck-row" + (r.emphasis === "accent" ? " ck-accent" : r.emphasis === "dim" ? " ck-dimmed" : ""));
    li.setAttribute("data-row", r.id);
    li.appendChild(make(doc, "span", "ck-title", visible(r.title)));
    for (const b of r.badges || []) li.appendChild(make(doc, "span", "ck-badge", visible(b.kind) + " " + visible(b.value)));
    if (r.provenance) li.appendChild(make(doc, "span", "ck-prov", visible(r.provenance.label)));
    if (r.body) li.appendChild(make(doc, "div", "ck-body", visible(r.body)));
    return li;
  }

  function listEl(doc, head, panel, topN) {
    const wrap = make(doc, "div", "ck-list");
    const rows = panel.rows || [];
    const counts = new Map();   // Map, not {}: a group id such as "constructor" must not hit a prototype
    for (const g of head.groups || []) counts.set(g.id, g);
    const drawn = new Map();
    let current = {};   // sentinel: never equal to a real group id
    let ul = null;
    for (const r of rows) {
      const g = r.group === undefined ? null : r.group;
      if (g !== current || ul === null) {
        current = g;
        if (g !== null) {
          const gh = counts.get(g);
          wrap.appendChild(make(doc, "div", "ck-group", visible(gh ? gh.title : g) + (gh ? " (" + gh.count + ")" : "")));
        }
        ul = make(doc, "ul", "ck-rows");
        ul.setAttribute("data-group", g === null ? "" : g);
        wrap.appendChild(ul);
      }
      drawn.set(g, (drawn.get(g) || 0) + 1);
      if (drawn.get(g) > topN) continue;
      ul.appendChild(rowEl(doc, r));
    }
    for (const [g, n] of drawn) {
      const total = counts.has(g) ? counts.get(g).count : n;
      const shown = Math.min(n, topN);
      // the renderer's own cut says what it hid: the kernel's group count minus the rows drawn
      if (total > shown) wrap.appendChild(make(doc, "div", "ck-more", "+" + (total - shown) + " more (cut by this view)"));
    }
    return wrap;
  }

  function valueEl(doc, head, panel, now) {
    const v = panel.value || {};
    const ul = make(doc, "ul", head.kind === "metric" ? "ck-metrics" : "ck-lines");
    if (head.kind === "line") {
      for (const ln of v.lines || []) {
        const li = make(doc, "li", "ck-line");
        li.appendChild(make(doc, "span", "ck-label", visible(ln.label) + ":"));
        li.appendChild(make(doc, "span", "ck-text", visible(ln.text) + (ln.at ? " (" + age(ln.at, now) + ")" : "")));
        ul.appendChild(li);
      }
    } else if (head.kind === "metric") {
      for (const m of v.metrics || []) {
        const li = make(doc, "li", "ck-metric ck-metric-" + (m.status || "unknown"));
        li.appendChild(make(doc, "span", "ck-label", visible(m.label) + ":"));
        li.appendChild(make(doc, "span", "ck-text", visible(m.value) + (m.unit ? " " + visible(m.unit) : "") + (m.read ? " — " + visible(m.read) : "")));
        ul.appendChild(li);
      }
      for (const a of v.alerts || []) ul.appendChild(make(doc, "li", "ck-msg ck-msg-" + (a.severity === "bad" ? "error" : "stale"), "! " + visible(a.message)));
    } else if (head.kind === "visual") {
      // the text projection: every surface without the native drawing renders this
      for (const ln of v.text || []) {
        const li = make(doc, "li", "ck-line");
        li.appendChild(make(doc, "span", "ck-label", visible(ln.label) + ":"));
        li.appendChild(make(doc, "span", "ck-text", visible(ln.text)));
        ul.appendChild(li);
      }
    } else if (head.kind === "prose") {
      ul.appendChild(make(doc, "li", "ck-line", visible(v.headline || "")));
      if (v.markdown) ul.appendChild(make(doc, "li", "ck-prose", visible(v.markdown)));   // text, never HTML
    }
    return ul;
  }

  function openButton(doc, head, panel) {
    const more = make(doc, "button", "ck-open", (head.count === null || head.count === undefined ? "" : head.count + " rows · ") + "open");
    more.setAttribute("type", "button");
    more.setAttribute("data-next", panel.next || "");
    return more;
  }

  function panelEl(doc, manifestHead, panel, opts) {
    const now = opts.now;
    // The panel payload is its own PanelHead plus rows/value, read in the same call, so its status and
    // error describe the content under it. The manifest head was read earlier and may disagree (a source
    // that failed in between): it is the fallback only.
    const head = panel || manifestHead;
    const sec = make(doc, "section", "ck-panel ck-kind-" + head.kind + " ck-prio-" + head.priority);
    sec.setAttribute("data-panel", head.id);
    sec.setAttribute("data-status", head.status);
    const h = make(doc, "h3");
    h.appendChild(make(doc, "span", "ck-name", visible(head.title)));
    h.appendChild(make(doc, "span", "ck-status ck-status-" + head.status, head.status));
    sec.appendChild(h);
    if (head.status === "error") {
      sec.appendChild(make(doc, "div", "ck-msg ck-msg-error",
        "ERROR: " + visible(head.error || "unknown") + " — last good " + age(head.as_of, now)));
      return sec;   // an error never carries rows or a value
    }
    if (!panel) {
      sec.appendChild(make(doc, "div", "ck-msg ck-msg-error", "ERROR: the kernel returned no payload for this panel"));
      return sec;
    }
    if (head.status === "stale") sec.appendChild(make(doc, "div", "ck-msg ck-msg-stale", "STALE: last read " + age(head.as_of, now)));
    if (head.status === "partial") {
      for (const s of head.skipped || []) sec.appendChild(make(doc, "div", "ck-msg ck-msg-partial", "PARTIAL: " + s.count + " unreadable — " + visible(s.reason)));
    }
    for (const f of head.filtered || []) sec.appendChild(make(doc, "div", "ck-msg ck-msg-dim", f.count + " held back — " + visible(f.reason)));
    if (head.note) sec.appendChild(make(doc, "div", "ck-note", visible(head.note)));
    if (head.status === "empty") {
      sec.appendChild(make(doc, "div", "ck-empty", visible(head.empty || "Nothing here.")));
      return sec;
    }
    if (panel.next && (panel.rows === null || panel.rows === undefined) && (panel.value === null || panel.value === undefined)) {
      // compact profile, feed priority: the kernel sent the count and a pointer, content on open
      sec.appendChild(openButton(doc, head, panel));
    } else if (head.kind === "triage-list") {
      {
        sec.appendChild(listEl(doc, head, panel, opts.topN || TOP_N));
      }
    } else {
      sec.appendChild(valueEl(doc, head, panel, now));
      if (panel.next) sec.appendChild(openButton(doc, head, panel));   // compact visual/prose: the full form is on open
    }
    return sec;
  }

  // snap = {manifest, panels: {id: Panel}}; replaces `rootEl`'s children with the cockpit.
  function render(doc, rootEl, snap, opts) {
    opts = Object.assign({ now: Date.now(), topN: TOP_N }, opts || {});
    const m = snap.manifest;
    const nodes = [];
    const errs = m.errors || [];
    for (const e of errs) nodes.push(make(doc, "div", "ck-msg ck-msg-error", "ERROR " + visible(e.source) + ": " + visible(e.message)));
    const draw = (pid) => {
      const head = m.panels[pid];
      return head ? panelEl(doc, head, snap.panels[pid] || null, opts) : make(doc, "div", "ck-msg ck-msg-error", pid + ": not in the manifest");
    };
    const header = make(doc, "div", "ck-header");
    for (const pid of m.regions.header || []) header.appendChild(draw(pid));
    nodes.push(header);
    for (const z of m.regions.zones || []) {
      const zs = make(doc, "div", "ck-zone");
      zs.setAttribute("data-zone", z.id);
      zs.appendChild(make(doc, "h2", "", visible(z.title)));
      for (const pid of z.panels || []) zs.appendChild(draw(pid));
      nodes.push(zs);
    }
    rootEl.replaceChildren(...nodes);
  }

  const api = { render, visible, age, TOP_N };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainCockpit = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
