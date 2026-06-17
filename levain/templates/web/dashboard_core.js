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

  // The write transport, injected by the surface shim each render (Slice 2a). A
  // surface that provides none (the parked in-host MCP-App port) renders strictly
  // read-only — every edit affordance below is gated on `commit` being a function,
  // so the governance model stays in the schema and the read-only port needs zero
  // change. `commit(request) → Promise<{ok} | {ok:false, error, message}>`.
  let commit = null;

  // Friendly zone labels, in IA order — used for the "All" view separators.
  const ZONE_LABELS = [
    ["identity", "Identity"],
    ["operate", "Operate"],
    ["mind", "Mind"],
  ];

  // ---------------------------------------------------------------- render ----
  function render(view, opts) {
    // The surface injects its write transport here; a port that provides none (the
    // parked MCP-App) renders read-only — no edit affordances, zero other change.
    commit = opts && typeof opts.commit === "function" ? opts.commit : null;

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
      wireEntityName(entityEl, view);  // Class-A rename affordance (Slice 2a; commit-gated)
    }
    // Masthead branding override (the surface's identity) — applied ONLY when the payload
    // carries it, so a bare Levain install keeps the HTML-default wordmark/model chrome.
    // The bridge sets these to flow-brand its cockpit.
    if (view.brand_wordmark) {
      const wm = document.querySelector(".brand .wordmark");
      if (wm) wm.textContent = view.brand_wordmark;
    }
    if (view.brand_model) {
      const md = document.querySelector(".brand .model");
      if (md) md.textContent = view.brand_model;
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
      // editable Class-A config panels get their edit affordance on its own right-
      // aligned row at the panel BOTTOM (after the pbody + any expander).
      if (p.dataset.editable === "1" && p._levainEdit) {
        p.appendChild(buildEditRow(p, p._levainEdit.entry, p._levainEdit.doc));
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
      case "edits": return renderEdits(entry, view);
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
      // Math.max floors the divisor at 1 — forward-defense for a future 1-sample
      // timebase (today span ≥ 12, so wraps.length ≥ 2 and this is a no-op). [L1/codex MED]
      const X = (i) => PAD + (i * (W - 2 * PAD)) / Math.max(1, wraps.length - 1);
      const chips = [];
      for (const ch of CHANNELS) {
        // counters are non-negative; clamp a malformed negative so it can't plot
        // off-grid (num() lets negatives through). gain floors at 1 to avoid /0, but
        // the legend reports the TRUE max so the calibration stays honest. [codex MED]
        const vals = wraps.map((w) => Math.max(0, ch.val(w)));
        const actualMax = Math.max(0, ...vals);
        const gain = Math.max(1, actualMax);
        const pts = vals
          .map((v, i) => `${X(i).toFixed(1)},${(H - PAD - (v / gain) * (H - 2 * PAD)).toFixed(1)}`)
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
          `${ch.label} ${fmt(vals[vals.length - 1])} · max ${fmt(actualMax)}`);
        chip.type = "button";
        chip.title = "solo this channel (click again for all)";
        chip.setAttribute("aria-pressed", solo === ch.cls ? "true" : "false");
        chip.addEventListener("click", () => {
          solo = solo === ch.cls ? null : ch.cls;
          wrap.dataset.solo = solo || "";
          for (const c of chips) c.setAttribute("aria-pressed", solo != null && c.classList.contains(solo) ? "true" : "false");
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
    const verbs = isVerbPanel(entry);
    for (const s of list) {
      const row = el("div", "row");
      // the spore id is the common reference handle (how flow tracks + you name it,
      // e.g. "compost spore-049") — show it leading so the row is addressable; the TUI
      // shows the same id trailing (tui.render_panel_lines).
      if (s.id) row.appendChild(el("span", "sid", s.id));
      if (s.tier) row.appendChild(el("span", "tier", `[${s.tier}]`));
      row.appendChild(el("span", "clause", s.text));
      if (s.next) row.appendChild(el("span", "muted", "→ " + s.next));
      if (verbs && s.id) row.appendChild(buildSporeVerbs(s));
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
    const verbs = isVerbPanel(entry);
    for (const e of list) {
      const row = el("div", "row");
      if (e.type) row.appendChild(el("span", "etype", e.type));
      row.appendChild(el("span", "clause", e.content));
      if (Array.isArray(e.tags) && e.tags.length) {
        row.appendChild(el("span", "tags", "#" + e.tags.slice(0, 5).join(" #")));
      }
      row.appendChild(el("span", "muted", datePart(e.timestamp)));
      if (verbs && e.id) row.appendChild(buildEpisodeVerbs(e));
      p.appendChild(row);
    }
    return p;
  }

  // Projection history (Slice 2b-ii / spore-093 v0). Each wrap is a RE-PROJECTION of
  // the continuity from the moving substrate — a regeneration, not a saved version.
  // The teaching frame + the per-wrap size delta make the projection visibly REGENERATE;
  // the deliberate ABSENCE of a restore button is the lesson (you can't roll a
  // projection back over a substrate that has moved on — govern the inputs instead).
  // aesthetic_encodes_the_relationship_model. (Full content/lineage `as-of` view = v1.)
  function renderWraps(entry, view) {
    const list = view.wraps || [];
    const p = panel(entry);
    const err = tierErr(view, "wraps");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (list.length === 0) { p.appendChild(el("p", "empty", "no wraps yet")); return p; }
    p.appendChild(el("p", "note",
      "each wrap re-projects this continuity from the substrate — regenerations, not " +
      "saved versions. no restore: to steer cognition, govern the inputs (Operate)."));
    list.forEach((w, i) => {
      const row = el("div", "row");
      row.appendChild(el("span", "tier", datePart(w.wrapped_at)));
      row.appendChild(el("span", "clause",
        `${fmt(num(w.graduations_validated))}↑ / ${fmt(num(w.graduations_demoted))}↓ grad · ` +
        `+${fmt(num(w.associations_formed))} links · ${fmt(num(w.continuity_chars))} chars`));
      // digest-delta: how much the projection grew/shrank vs the previous (older)
      // regeneration — list is newest-first, so the older wrap is the next index.
      const older = list[i + 1];
      if (older) {
        const d = num(w.continuity_chars) - num(older.continuity_chars);
        const sign = d > 0 ? "+" : d < 0 ? "−" : "±";
        row.appendChild(el("span", "delta" + (d > 0 ? " up" : d < 0 ? " down" : ""),
          `Δ ${sign}${fmt(Math.abs(d))}`));
      } else {
        row.appendChild(el("span", "muted", "first projection"));
      }
      p.appendChild(row);
    });
    return p;
  }

  function renderSection(entry, sec) {
    const p = panel(entry);
    if (!sec) { p.appendChild(el("p", "empty", "—")); return p; }
    p.appendChild(el("pre", "section", sec.body || ""));
    if (isEditablePanel(entry)) {
      // State (Class-A neocortex section) is editable in Slice 2b via the SAME
      // affordance config panels use — mark + stash; finalizePanels appends the bar.
      p.dataset.editable = "1";
      p._levainEdit = { entry, doc: sec };
    }
    return p;
  }

  function renderConfig(entry, doc) {
    const p = panel(entry);
    if (!doc) { p.appendChild(el("p", "empty", "—")); return p; }
    p.appendChild(el("pre", "section", doc.body || ""));
    if (isEditablePanel(entry)) {
      // Mark + stash; the edit bar is appended at the panel BOTTOM in finalizePanels
      // (a consistent position, after the pbody/expander — never wraps in the header).
      p.dataset.editable = "1";
      p._levainEdit = { entry, doc };
    }
    return p;
  }

  // The writable surfaces: a Class-A CONFIG panel (world.md section / posture /
  // recency — Slice 2a) OR a Class-A SECTION panel (the neocortex State section —
  // Slice 2b). Both need `commit` (read-only port → no affordance), edit_class "A",
  // and a `source` write-address. origin.md + constitution + the five felt-layer
  // sections are Class C → no match → stay glass.
  function isEditablePanel(entry) {
    return !!commit && entry.edit_class === "A" && !!entry.source &&
      (entry.kind === "config" || entry.kind === "section");
  }

  // A Class-B panel (spores / episodes — the operator's INPUT layer) gets
  // verb affordances when a write transport is present (read-only port → none).
  // The verbs are anneal-validated lifecycle ops, never raw edits; a DESTRUCTIVE
  // one (resolving a spore / tombstoning an episode) sends confirm:true — and the
  // server enforces that too, so the UI confirm is the affordance, not the gate.
  function isVerbPanel(entry) {
    return !!commit && entry.edit_class === "B";
  }

  // The edit affordance is a small METAL button on its own row under the header;
  // clicking flips the panel body from its glass <pre> display into a recessed editor.
  function buildEditRow(p, entry, doc) {
    const row = el("div", "edit-trigger-row");
    const btn = el("button", "edit-btn", "edit");
    btn.type = "button";
    btn.addEventListener("click", () => enterEditMode(p, entry, doc, row));
    row.appendChild(btn);
    return row;
  }

  function enterEditMode(p, entry, doc, row) {
    const body = p.querySelector(".pbody");  // created by finalizePanels (post-render)
    if (!body || body.dataset.editing === "1") return;
    body.dataset.editing = "1";
    const pre = body.querySelector("pre.section");
    const expander = p.querySelector(".expander");
    if (pre) pre.style.display = "none";
    if (expander) expander.style.display = "none";
    row.style.display = "none";  // the row is the trigger; hide it while editing
    body.classList.remove("clamped");  // show full text while editing

    const editor = el("div", "editor");
    const ta = el("textarea", "edit-ta");
    ta.value = doc.body || "";  // .value (not innerHTML) — store text never becomes markup
    const controls = el("div", "edit-controls");
    const save = el("button", "edit-save", "save");
    const cancel = el("button", "edit-cancel", "cancel");
    save.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    controls.append(save, cancel, msg);
    editor.append(ta, controls);
    body.appendChild(editor);
    ta.focus();

    const restore = () => {
      editor.remove();
      if (pre) pre.style.display = "";
      if (expander) expander.style.display = "";
      row.style.display = "";
      body.dataset.editing = "";
    };
    cancel.addEventListener("click", restore);
    save.addEventListener("click", async () => {
      save.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "saving…";
      // expected_body is exactly the body the operator saw → the server's per-section
      // stale-check rejects (409) if the file changed underneath; new_body is verbatim.
      // A section panel writes the neocortex State section (kind "state"); a config
      // panel writes a seed/config file (kind "config"). Each kind self-confines to
      // its own target set server-side.
      const res = await commit({
        kind: entry.kind === "section" ? "state" : "config",
        source: entry.source,
        heading: entry.heading != null ? entry.heading : null,
        expected_body: doc.body || "",
        new_body: ta.value,
      });
      if (res && res.ok) return;  // shim re-fetched + re-rendered → this DOM is gone
      msg.className = "edit-msg err";
      msg.textContent = (res && (res.message || res.error)) || "save failed";
      save.disabled = false; cancel.disabled = false;
    });
  }

  // ---- Class-B verb affordances (Slice 2b-ii) ----
  // Spores + episodes are the operator's INPUTS, mutated through anneal's validated
  // verbs (never raw writes). A metal button per row; destructive verbs open a small
  // confirm/kind form that sends confirm:true (the server requires it regardless).

  function verbBtn(label, title, onClick) {
    const b = el("button", "verb-btn", label);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", onClick);
    return b;
  }

  function verbErr(wrap, res) {
    let m = wrap.querySelector(".edit-msg");
    if (!m) { m = el("span", "edit-msg", ""); wrap.appendChild(m); }
    m.className = "edit-msg err";
    m.textContent = (res && (res.message || res.error)) || "failed";
  }

  function buildSporeVerbs(s) {
    const wrap = el("span", "verb-actions");
    // touch — non-destructive (engage: seen=today, clears an elapsed alarm)
    wrap.appendChild(verbBtn("touch", "mark seen — reset its clock", async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      const res = await commit({ kind: "spore_touch", spore_id: s.id });
      if (res && res.ok) return;  // shim re-fetched + re-rendered → this DOM is gone
      b.disabled = false;
      verbErr(wrap, res);
    }));
    // compost (descend) / promote (ascend) — destructive; kind comes from the
    // spore's own type taxonomy (server-emitted, so the UI can't offer an invalid kind).
    if (Array.isArray(s.descend_kinds) && s.descend_kinds.length) {
      wrap.appendChild(verbBtn("compost", "resolve this loop downward", () =>
        openResolveForm(wrap, "spore_descend", "compost", s, s.descend_kinds, false)));
    }
    if (Array.isArray(s.ascend_kinds) && s.ascend_kinds.length) {
      wrap.appendChild(verbBtn("promote", "transmute into memory / project", () =>
        openResolveForm(wrap, "spore_ascend", "promote", s, s.ascend_kinds, true)));
    }
    return wrap;
  }

  // The destructive resolve form: a kind <select> (+ a ref <input> for ascend) and a
  // confirm/cancel pair. The trigger buttons hide while it's open; confirm sends
  // confirm:true. One form per row at a time.
  function openResolveForm(host, kind, label, s, kinds, needsRef) {
    if (host.querySelector(".verb-form")) return;
    const stale = host.querySelector(".edit-msg"); if (stale) stale.remove();  // a prior touch error
    const triggers = Array.prototype.slice.call(host.querySelectorAll(".verb-btn"));
    triggers.forEach((b) => (b.style.display = "none"));
    const form = el("span", "verb-form");
    const sel = el("select", "verb-kind");
    for (const k of kinds) { const o = el("option", null, k); o.value = k; sel.appendChild(o); }
    form.appendChild(sel);
    let refInput = null;
    if (needsRef) {
      refInput = el("input", "verb-ref");
      refInput.type = "text"; refInput.placeholder = "what it became (ref)"; refInput.maxLength = 200;
      form.appendChild(refInput);
    }
    const go = el("button", "verb-confirm", label);
    const cancel = el("button", "verb-cancel", "cancel");
    go.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    form.append(go, cancel, msg);
    host.appendChild(form);
    (needsRef ? refInput : sel).focus();

    const close = () => { form.remove(); triggers.forEach((b) => (b.style.display = "")); };
    cancel.addEventListener("click", close);
    go.addEventListener("click", async () => {
      const req = { kind: kind, spore_id: s.id, spore_kind: sel.value, confirm: true };
      if (needsRef) {
        const ref = refInput.value.trim();
        if (!ref) { msg.className = "edit-msg err"; msg.textContent = "a ref is required"; return; }
        req.ref = ref;
      }
      go.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit(req);
      if (res && res.ok) return;  // re-rendered
      msg.className = "edit-msg err"; msg.textContent = (res && (res.message || res.error)) || "failed";
      go.disabled = false; cancel.disabled = false;
    });
  }

  function buildEpisodeVerbs(e) {
    const wrap = el("span", "verb-actions");
    wrap.appendChild(verbBtn("tombstone", "delete this input — the consolidate re-derives without it", () =>
      openTombstoneConfirm(wrap, e)));
    return wrap;
  }

  function openTombstoneConfirm(host, e) {
    if (host.querySelector(".verb-form")) return;
    const stale = host.querySelector(".edit-msg"); if (stale) stale.remove();  // a prior touch error
    const triggers = Array.prototype.slice.call(host.querySelectorAll(".verb-btn"));
    triggers.forEach((b) => (b.style.display = "none"));
    const form = el("span", "verb-form");
    const go = el("button", "verb-confirm danger", "confirm tombstone");
    const cancel = el("button", "verb-cancel", "cancel");
    go.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    form.append(go, cancel, msg);
    host.appendChild(form);
    const close = () => { form.remove(); triggers.forEach((b) => (b.style.display = "")); };
    cancel.addEventListener("click", close);
    go.addEventListener("click", async () => {
      go.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit({ kind: "episode_tombstone", episode_id: e.id, confirm: true });
      if (res && res.ok) return;  // re-rendered
      msg.className = "edit-msg err"; msg.textContent = (res && (res.message || res.error)) || "failed";
      go.disabled = false; cancel.disabled = false;
    });
  }

  // The edit log + undo surface (Operate zone). Offers undo only on the most-recent
  // non-undo, file-undoable edit PER SOURCE (a safe stack-pop — undoing an older edit
  // would discard newer ones; that's a Slice-2b time-travel concern). Verb-mediated
  // records (undoable === false) show in the log but get no undo (anneal owns their
  // reversibility). Undo restores that edit's backup via the same commit transport.
  function renderEdits(entry, view) {
    const list = view.recent_edits || [];
    const p = panel(entry);
    if (list.length === 0) { p.appendChild(el("p", "empty", "no edits yet")); return p; }
    const claimed = new Set();
    for (const e of list) {
      const row = el("div", "edit-row");
      row.appendChild(el("span", "tier", datePart(e.ts)));
      const isUndo = e.action === "undo";
      row.appendChild(el("span", "edit-act" + (isUndo ? " undo" : ""), isUndo ? "undo" : (e.action || "edit")));
      const label = String(e.source || "") + (e.heading ? " · " + e.heading : "");
      row.appendChild(el("span", "clause", label));
      if (commit && !isUndo && e.id && e.undoable !== false && !claimed.has(e.source)) {
        const u = el("button", "undo-btn", "undo");
        u.type = "button";
        u.addEventListener("click", async () => {
          u.disabled = true;
          const res = await commit({ kind: "undo", edit_id: e.id });
          if (res && res.ok) return;  // reloaded
          u.disabled = false;
          row.appendChild(el("span", "edit-msg err",
            (res && (res.message || res.error)) || "undo failed"));
        });
        row.appendChild(u);
      }
      claimed.add(e.source);  // any record claims its source's latest-action slot
      p.appendChild(row);
    }
    return p;
  }

  // The masthead "Unit" name is a Class-A input too — a commit-gated rename.
  function wireEntityName(entityEl, view) {
    if (!commit) return;
    const unit = entityEl.parentElement;
    if (!unit || unit.querySelector(".name-edit") || unit.querySelector(".name-editor")) return;
    const btn = el("button", "name-edit", "rename");
    btn.type = "button";
    btn.title = "rename this entity";
    btn.setAttribute("aria-label", "rename entity");
    btn.addEventListener("click", () => enterNameEdit(unit, entityEl, view, btn));
    unit.appendChild(btn);
  }

  function enterNameEdit(unit, entityEl, view, btn) {
    const current = view.entity_name || "";
    const editor = el("div", "name-editor");
    const input = el("input", "name-input");
    input.type = "text"; input.value = current; input.maxLength = 120;
    const save = el("button", "edit-save", "save");
    const cancel = el("button", "edit-cancel", "cancel");
    save.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    editor.append(input, save, cancel, msg);
    entityEl.style.display = "none";
    btn.style.display = "none";
    unit.appendChild(editor);
    input.focus(); input.select();

    const restore = () => {
      editor.remove();
      entityEl.style.display = "";
      btn.style.display = "";
    };
    const doSave = async () => {
      save.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "saving…";
      // no `expected` sent: the displayed name may come from the origin.md H1 fallback
      // (≠ the config field the server stale-checks), so an optimistic lock here would
      // false-409 the first rename. The name is a single trivial field — last-writer-wins.
      const res = await commit({ kind: "entity_name", value: input.value });
      if (res && res.ok) return;  // reloaded
      msg.className = "edit-msg err";
      msg.textContent = (res && (res.message || res.error)) || "save failed";
      save.disabled = false; cancel.disabled = false;
    };
    cancel.addEventListener("click", restore);
    save.addEventListener("click", doSave);
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); doSave(); }
      else if (ev.key === "Escape") { ev.preventDefault(); restore(); }
    });
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
