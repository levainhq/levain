// levain dashboard_core.js — the transport-agnostic render core.
//
// This is the CANONICAL "how to draw a SubstrateView" logic, shared by every
// Levain v2 control surface that renders the substrate in a browser. It is pure:
// it takes a `SubstrateView.to_dict()` object and paints it into the page. It
// knows nothing about HOW the view arrived — `fetch('/substrate.json')` (the
// local sovereign web-app, `levain serve`) or an MCP-Apps `app.ontoolresult`
// (the parked in-host port) — that lifecycle is each surface's own thin shim.
//
// Slice 1.5 — SHOW EVERYTHING. The render program is DECLARED by the backend:
// `view.layout` is an ordered list of panels, each carrying its `zone`
// (identity/operate/mind — the IA) and `edit_class` (A/B/C — the governance
// model). This core renders FROM that manifest, so the IA and the edit-class
// taxonomy live in the substrate schema (Python), not hardcoded here — the app
// cannot drift from what it edits. The `[All | Identity | Operate | Mind]` tabs
// filter the (already zone-tagged) panels; the edit-class chip on each panel
// makes the governance visible (read-only in 1.5; Slice 2 turns the A/B classes
// into edit affordances with no change to this dispatch).
//
// No dependencies, no CDN, no framework. A sovereign local surface must not rent
// its client library either, so this is vanilla JS that runs offline. Store data
// is rendered with `textContent` ONLY, never `innerHTML` — a malformed or hostile
// store value paints as inert text, never as markup. The DOM contract: the host
// page provides `#board` (the panel grid), `#store` (store-path subtitle),
// `#entity` (the entity-name headline), and a `.tabs` bar of `.tab[data-zone]`.

(function () {
  "use strict";

  // ---- tiny DOM helpers (textContent only — never innerHTML on store data) ----
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  };
  // A panel carries its zone + edit-class as data attributes (the tab filter
  // reads data-zone) and renders a header with the title, an edit-class chip, and
  // an optional source path.
  const panel = (entry, span2) => {
    const p = el("div", "panel" + (span2 ? " span2" : ""));
    p.dataset.zone = entry.zone || "";
    p.dataset.editClass = entry.edit_class || "";
    const head = el("div", "phead");
    head.appendChild(el("h2", null, entry.title));
    if (entry.edit_class) {
      head.appendChild(el("span", "chip chip-" + entry.edit_class, entry.edit_class));
    }
    if (entry.source) head.appendChild(el("span", "src", entry.source));
    p.appendChild(head);
    return p;
  };
  const kv = (parent, k, v) => {
    const row = el("div", "kv");
    row.appendChild(el("span", "k", k));
    row.appendChild(el("span", "v", v));
    parent.appendChild(row);
  };
  const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : n);
  // coerce a render value to a finite number (a malformed/injected structuredContent
  // must degrade to 0, never paint the literal "NaN" across the health panel)
  const num = (x) => { const n = +x; return Number.isFinite(n) ? n : 0; };
  const datePart = (ts) => (ts ? String(ts).split("T")[0] : "");

  // The active zone tab. Module-level so it survives a re-render (refresh /
  // visibility re-read) — the operator's chosen tab must not reset on every poll.
  let activeZone = "all";

  // Friendly zone labels, in IA order — used for the "All" view separators.
  const ZONE_LABELS = [
    ["identity", "Identity"],
    ["operate", "Operate"],
    ["mind", "Mind"],
  ];

  // ---------------------------------------------------------------- render ----
  function render(view) {
    const board = document.getElementById("board");
    if (!board) return;
    board.replaceChildren();

    const entityEl = document.getElementById("entity");
    const storeEl = document.getElementById("store");
    if (!view || typeof view !== "object") {
      board.appendChild(el("p", "empty", "No substrate data delivered."));
      return;
    }
    const paths = view.paths || {};
    if (storeEl) storeEl.textContent = paths.episodic_db || "(store path unknown)";
    if (entityEl) {
      const stem = (paths.episodic_db ? String(paths.episodic_db).split("/").pop() : "") || "substrate";
      entityEl.textContent = view.entity_name || stem.replace(/\.db$/, "");
    }
    // Drive the living-rings vital-signs from substrate health: write-path LIVE →
    // steady phosphor heartbeat; DARK → slow, dim-red. The background IS the pulse.
    // (view.scope stays a data-only seam — the UI surfaces it when team scope is
    // real, not as a PM-style profile selector that does nothing yet.)
    const live = !!(view.health && view.health.write_path_live);
    if (document.body) document.body.dataset.vital = live ? "live" : "dark";

    const layout = Array.isArray(view.layout) ? view.layout : [];
    let lastZone = null;
    for (const entry of layout) {
      // In the "All" view, drop a zone separator before each zone's first panel.
      if (entry.zone !== lastZone) {
        const lbl = (ZONE_LABELS.find((z) => z[0] === entry.zone) || [entry.zone, entry.zone])[1];
        // zone divider is ALWAYS full-width — no span2 class (which at ≥1180px
        // would collapse it to 2 columns, freeing the 3rd for a panel to orphan
        // above its own divider).
        const sep = el("div", "zone-head", lbl);
        sep.dataset.zone = entry.zone || "";
        sep.dataset.zoneHead = "1";
        board.appendChild(sep);
        lastZone = entry.zone;
      }
      const node = renderPanel(entry, view);
      if (node) {
        if (CLAMP_KINDS.has(entry.kind)) node.setAttribute("data-clamp", "");
        board.appendChild(node);
      }
    }

    if (view.errors && Object.keys(view.errors).length) {
      board.appendChild(renderErrors(view));
    }
    finalizePanels(board);  // wrap content + clamp the long modules (no inner scroll)
    applyFilter();
  }

  // Long modules clamp to a fixed height with click-to-expand — so the page is a
  // single scroll context (no inner-scroll wheel capture) AND heights stay uniform
  // so the grid tiles cleanly instead of stair-stepping.
  const CLAMP_KINDS = new Set(["config", "section", "episodes", "wraps", "crystals", "spores"]);
  const CLAMP_PX = 232;

  function finalizePanels(board) {
    for (const p of board.querySelectorAll(".panel")) {
      const phead = p.querySelector(".phead");
      if (!phead) continue;
      const body = el("div", "pbody");
      while (phead.nextSibling) body.appendChild(phead.nextSibling);
      p.appendChild(body);
      // measure full height BEFORE clamping; only add the expander if it overflows
      if (p.hasAttribute("data-clamp") && body.scrollHeight > CLAMP_PX + 28) {
        body.classList.add("clamped");
        const btn = el("button", "expander", "▼ expand");
        btn.type = "button";
        btn.addEventListener("click", () => {
          const open = body.classList.toggle("expanded");
          btn.textContent = open ? "▲ collapse" : "▼ expand";
        });
        p.appendChild(btn);
      }
    }
  }

  // Dispatch a single manifest entry to its renderer. Singleton kinds read their
  // data from the matching view field; indexed kinds (config/section) use `ref`.
  function renderPanel(entry, view) {
    switch (entry.kind) {
      case "health": return renderHealth(entry, view);
      case "graph": return renderGraph(entry, view);
      case "crystals": return renderCrystals(entry, view);
      case "spores": return renderSpores(entry, view);
      case "episodes": return renderEpisodes(entry, view);
      case "wraps": return renderWraps(entry, view);
      case "section": return renderSection(entry, (view.sections || [])[entry.ref]);
      case "config": return renderConfig(entry, (view.config_docs || [])[entry.ref]);
      default: return null;
    }
  }

  function renderHealth(entry, view) {
    const p = panel(entry);
    const h = view.health;
    if (!h) {
      const err = view.errors && (view.errors.store || view.errors.health);
      p.appendChild(el("p", "empty", err ? ("unavailable — " + err) : "no health data"));
      return p;
    }
    const head = el("div", "kv");
    head.appendChild(el("span", "k", "write-path"));
    const b = el("span", "badge " + (h.write_path_live ? "live" : "dark"),
      h.write_path_live ? "LIVE" : "DARK · 0 links");
    const vWrap = el("span", "v"); vWrap.appendChild(b);
    head.appendChild(vWrap);
    p.appendChild(head);

    kv(p, "Hebbian links", `${fmt(h.total_links)} (avg ${num(h.avg_strength).toFixed(2)}, max ${num(h.max_strength).toFixed(2)})`);
    kv(p, "density", `${num(h.density).toFixed(3)} · local ${num(h.local_density).toFixed(3)}`);
    kv(p, "graduations", `${fmt(h.graduations_validated_total)} validated / ${fmt(h.graduations_demoted_total)} demoted`);
    kv(p, "episodes", `${fmt(h.total_episodes)} (${fmt(h.episodes_since_wrap)} since wrap)`);
    const last = h.last_wrap_at ? datePart(h.last_wrap_at) : "never";
    kv(p, "continuity", `${h.continuity_chars != null ? fmt(h.continuity_chars) + " chars" : "not yet created"} · ${fmt(h.total_wraps)} wraps · last ${last}`);
    if (h.tombstones) kv(p, "tombstones", fmt(h.tombstones));
    if (h.wrap_in_progress) p.appendChild(el("p", "note", "⚠ wrap in progress — snapshot may be momentarily inconsistent"));
    return p;
  }

  // The cognition trace — a phosphor oscilloscope over REAL per-wrap vitals
  // (no-theater rule: every sample is an actual consolidation; the draw-in
  // animation fires only when this renders, and a render only happens on a
  // real fetch — boot or the refresh button). Channels: episodes compressed
  // (mind), graduations validated (identity), links formed+strengthened
  // (operate). Per-channel auto-gain, like a real scope; the legend shows
  // each channel's latest value + its max calibration so the gain is honest.
  function renderGraph(entry, view) {
    const p = panel(entry, true);
    const series = view.wraps || [];
    if (series.length < 2) {
      const err = tierErr(view, "wraps");
      p.appendChild(el("p", "empty",
        err ? ("trace unavailable — " + err)
            : "trace needs two consolidations — each wrap draws a sample"));
      return p;
    }
    const NS = "http://www.w3.org/2000/svg";
    const W = 640, H = 210, PAD = 12;
    const CHANNELS = [
      { label: "CH1 EPISODES", cls: "ch1", val: (w) => num(w.episodes_compressed) },
      { label: "CH2 GRADUATIONS", cls: "ch2", val: (w) => num(w.graduations_validated) },
      { label: "CH3 LINKS", cls: "ch3",
        val: (w) => num(w.associations_formed) + num(w.associations_strengthened) },
    ];

    // the instrument's read-side controls — the first real metal on the deck
    // (no-theater: both verbs act on the instrument NOW; write-side knobs land
    // with the Slice-2 governed verb seam). solo = CSS-only; timebase = redraw,
    // and the re-draw-in is honest (you changed the timebase, the scope re-sweeps).
    let solo = null;
    let span = 48;
    const wrap = el("div", "scope-wrap");
    wrap.dataset.solo = "";
    const bar = el("div", "scope-bar");
    bar.appendChild(el("span", "lbl", "timebase"));
    const scopeBox = el("div", "scope-box");
    const legend = el("div", "scope-legend");
    const note = el("p", "note", "");
    for (const n of [12, 24, 48]) {
      const b = el("button", "scope-tb" + (n === span ? " active" : ""), String(n));
      b.type = "button";
      b.addEventListener("click", () => {
        span = n;
        for (const x of bar.querySelectorAll(".scope-tb")) x.classList.toggle("active", x === b);
        draw();
      });
      bar.appendChild(b);
    }

    function draw() {
      const wraps = series.slice(0, span).reverse(); // chronological L→R
      legend.replaceChildren(); // chips rebuild each redraw — never accumulate
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "scope");
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      for (let i = 0; i <= 8; i++) svg.appendChild(gratLine(NS, (i * W) / 8, 0, (i * W) / 8, H));
      for (let i = 0; i <= 4; i++) svg.appendChild(gratLine(NS, 0, (i * H) / 4, W, (i * H) / 4));
      const X = (i) => PAD + (i * (W - 2 * PAD)) / (wraps.length - 1);
      const chips = [];
      for (const ch of CHANNELS) {
        const vals = wraps.map(ch.val);
        const max = Math.max(1, ...vals);
        const pts = vals
          .map((v, i) => `${X(i).toFixed(1)},${(H - PAD - (v / max) * (H - 2 * PAD)).toFixed(1)}`)
          .join(" ");
        for (const layer of ["trace-glow", "trace-core"]) {
          const pl = document.createElementNS(NS, "polyline");
          pl.setAttribute("points", pts);
          pl.setAttribute("class", `${layer} ${ch.cls}`);
          pl.setAttribute("fill", "none");
          pl.setAttribute("pathLength", "1"); // normalizes the draw-in dash math
          svg.appendChild(pl);
        }
        const chip = el("button", `scope-ch ${ch.cls}`,
          `${ch.label} ${fmt(vals[vals.length - 1])} · max ${fmt(max)}`);
        chip.type = "button";
        chip.title = "solo this channel (click again for all)";
        chip.setAttribute("aria-pressed", solo === ch.cls ? "true" : "false");
        chip.addEventListener("click", () => {
          solo = solo === ch.cls ? null : ch.cls;
          wrap.dataset.solo = solo || "";
          for (const c of chips) c.setAttribute("aria-pressed", c.classList.contains(solo) ? "true" : "false");
        });
        chips.push(chip);
        legend.appendChild(chip);
      }
      scopeBox.replaceChildren(svg);
      const g = view.graph;
      const assoc = g && g.nodes && g.nodes.length
        ? ` · association graph ${g.nodes.length} nodes / ${(g.edges || []).length} edges`
        : "";
      note.textContent = `${wraps.length} consolidations, oldest → newest${assoc}`;
    }

    p.appendChild(wrap);
    wrap.appendChild(bar);
    wrap.appendChild(scopeBox);
    wrap.appendChild(legend);
    wrap.appendChild(note);
    draw();
    return p;
  }

  function gratLine(NS, x1, y1, x2, y2) {
    const ln = document.createElementNS(NS, "line");
    ln.setAttribute("x1", x1); ln.setAttribute("y1", y1);
    ln.setAttribute("x2", x2); ln.setAttribute("y2", y2);
    ln.setAttribute("class", "grat");
    return ln;
  }

  // a tier that faulted shows its error in-context, not a misleading empty-state.
  const tierErr = (view, key) => view.errors && view.errors[key];

  function renderCrystals(entry, view) {
    const list = view.crystal_index || [];
    const p = panel(entry);
    const err = tierErr(view, "crystal_index");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (list.length === 0) { p.appendChild(el("p", "empty", "none crystallized yet")); return p; }
    for (const c of list) {
      const row = el("div", "row");
      row.appendChild(el("span", "lvl", `${c.level}x`));
      row.appendChild(el("span", "name", c.name));
      if (c.one_clause) row.appendChild(el("span", "clause", "— " + c.one_clause));
      // fuller crystal detail — permanence / activation mode / tags
      const meta = [c.permanence, c.activation_mode].filter(Boolean).join(" · ");
      if (meta) row.appendChild(el("span", "muted", meta));
      if (Array.isArray(c.tags) && c.tags.length) {
        row.appendChild(el("span", "tags", "#" + c.tags.slice(0, 6).join(" #")));
      }
      p.appendChild(row);
    }
    return p;
  }

  function renderSpores(entry, view) {
    const list = view.open_spores || [];
    const p = panel(entry);
    const err = tierErr(view, "open_spores");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (list.length === 0) { p.appendChild(el("p", "empty", "no open prospective loops")); return p; }
    for (const s of list) {
      const row = el("div", "row");
      if (s.tier) row.appendChild(el("span", "tier", `[${s.tier}]`));
      row.appendChild(el("span", "clause", s.text));
      if (s.next) row.appendChild(el("span", "muted", "→ " + s.next));
      p.appendChild(row);
    }
    return p;
  }

  function renderEpisodes(entry, view) {
    const list = view.episodes || [];
    const p = panel(entry);
    const err = tierErr(view, "episodes");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (list.length === 0) { p.appendChild(el("p", "empty", "no episodes yet")); return p; }
    const scroll = el("div", "scroll");
    for (const e of list) {
      const row = el("div", "row");
      if (e.type) row.appendChild(el("span", "etype", e.type));
      row.appendChild(el("span", "clause", e.content));
      if (Array.isArray(e.tags) && e.tags.length) {
        row.appendChild(el("span", "tags", "#" + e.tags.slice(0, 5).join(" #")));
      }
      row.appendChild(el("span", "muted", datePart(e.timestamp)));
      scroll.appendChild(row);
    }
    p.appendChild(scroll);
    return p;
  }

  function renderWraps(entry, view) {
    const list = view.wraps || [];
    const p = panel(entry);
    const err = tierErr(view, "wraps");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (list.length === 0) { p.appendChild(el("p", "empty", "no wraps yet")); return p; }
    const scroll = el("div", "scroll");
    for (const w of list) {
      const row = el("div", "row");
      row.appendChild(el("span", "tier", datePart(w.wrapped_at)));
      row.appendChild(el("span", "clause",
        `${fmt(w.graduations_validated)}↑ / ${fmt(w.graduations_demoted)}↓ grad · ` +
        `+${fmt(w.associations_formed)} links · ${fmt(w.continuity_chars)} chars`));
      scroll.appendChild(row);
    }
    p.appendChild(scroll);
    return p;
  }

  function renderSection(entry, sec) {
    const p = panel(entry);
    if (!sec) { p.appendChild(el("p", "empty", "—")); return p; }
    p.appendChild(el("pre", "section", sec.body || ""));
    return p;
  }

  function renderConfig(entry, doc) {
    const p = panel(entry);
    if (!doc) { p.appendChild(el("p", "empty", "—")); return p; }
    p.appendChild(el("pre", "section", doc.body || ""));
    return p;
  }

  function renderErrors(view) {
    const p = panel({ title: "Degraded tiers", zone: "mind", edit_class: "" }, true);
    // ALWAYS visible — a tier can fault in any zone, so the degradation summary must
    // not hide behind the Identity/Operate tab filter (degrade VISIBLY, every view).
    p.dataset.always = "1";
    const errs = view.errors || {};
    for (const k of Object.keys(errs)) p.appendChild(el("p", "err", `${k}: ${errs[k]}`));
    return p;
  }

  // ----------------------------------------------------------- zone tabs ----
  function applyFilter() {
    const board = document.getElementById("board");
    if (!board) return;
    for (const node of board.children) {
      // always-visible nodes (the degraded-tiers summary) ignore the zone filter
      if (node.dataset.always === "1") { node.style.display = ""; continue; }
      const z = node.dataset.zone || "";
      const isHead = node.dataset.zoneHead === "1";
      // "All": show everything incl. zone separators. A specific zone: show only
      // that zone's panels and hide the (now redundant) separators.
      let show;
      if (activeZone === "all") show = true;
      else show = z === activeZone && !isHead;
      node.style.display = show ? "" : "none";
    }
  }

  function wireTabs() {
    const tabs = document.querySelectorAll(".tab");
    tabs.forEach((t) => {
      t.addEventListener("click", () => {
        activeZone = t.dataset.zone || "all";
        tabs.forEach((x) => x.classList.toggle("active", x === t));
        applyFilter();
      });
    });
  }

  wireTabs();

  // Export the one entry point onto a namespace the surface shim calls.
  window.LevainDashboard = { render };
})();
