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
    // the panel's stable id: what a kept row or scroll position is filed under (the heading is not an identity)
    if (entry.id) p.dataset.panelId = String(entry.id);
    if (entry.modes) p.dataset.modes = entry.modes;   // which of the four tabs (Morning / Board / Build / Memory) show this panel
    const head = el("div", "phead");
    head.appendChild(el("h2", null, entry.title));
    if (entry.edit_class) {
      head.appendChild(el("span", "chip chip-" + entry.edit_class, entry.edit_class));
    }
    if (entry.source) head.appendChild(el("span", "src", entry.source));
    if (entry.fresh) head.appendChild(el("span", "src fresh", entry.fresh));   // manifest source: when the kernel last read it
    // dense kinds get a ⤢ control in the header → a focused, full-screen re-render
    // (Slice 2). Only the long modules (CLAMP_KINDS) — health/graph stay natural-height.
    if (entry.kind && CLAMP_KINDS.has(entry.kind)) head.appendChild(buildExpandBtn(entry));
    p.appendChild(head);
    // A manifest source's status lines (stale / partial / held back / the provider's note), drawn
    // under the header so a degraded panel never reads as a healthy one.
    appendBanner(p, entry);
    return p;
  };
  function appendBanner(host, entry) {
    for (const b of entry.banner || []) host.appendChild(richEl("div", "ext-note" + (b.tone === "stale" ? " stale" : ""), b.text));
  }
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
  // The governed ACTION transport (POST /action), injected alongside commit by the boot layer.
  // null on a read-only port → external-panel compose affordances render nothing (NO THEATER).
  let commitAction = null;
  // The async-JOB transports for an I/O-bound (`job:true`) verb: `commitJob` proposes (POST /action,
  // NO board reload — the result lands in the box, not a rebuild) and returns a job HANDLE;
  // `pollJob` reads GET /job.json for the terminal result. Both null on a read-only port.
  let commitJob = null;
  // Cancel a waiting pending (the manifest source only): `cancelPending(cancel) → Promise<{ok}|{ok:false,...}>`.
  let cancelPending = null;
  // Answer an agent's question (Q-2b, the manifest source only): `answerQuestion(rowId, {option}|{text}) → Promise<{ok}|{ok:false,...}>`.
  let answerQuestion = null;
  // Dismiss an agent's note (the manifest source only): `dismissNote(rowId) → Promise<{ok}|{ok:false,...}>`. It tells the agent nothing.
  let dismissNote = null;
  // Clear or reopen a coordinator review item (the manifest source only): `reviewItem(rowId, verb) → Promise<{ok}|{ok:false,...}>`.
  let reviewItem = null;
  let pollJob = null;
  // The episode-search READ transport (GET /recall.json), injected by the boot layer so the core
  // stays token-agnostic — the boot layer attaches the off-box token (spore-220 gates /recall.json
  // off-box, exactly like /substrate.json). null → search falls back to a direct token-free fetch:
  // the transport-agnostic-core SAFETY NET so the core never hard-requires the transport. The
  // shipped web boot injects `recall` on EVERY surface (both render branches), so for `levain serve`
  // that fallback is not a live path — off-box search always rides the token-carrying transport.
  let recall = null;
  // Called when the LAST active job poll terminates → the boot layer flushes a board reload it
  // DEFERRED while a job was polling (so a sibling write — inbox/relay/edit — doesn't rebuild the
  // board and detach the live consult box mid-job; L3 codex MED). null on a read-only port.
  let onJobsIdle = null;
  // Job ids with a LIVE in-box poll loop. hasUnsavedEdit() reports true while non-empty so a
  // passive board reload (visibilitychange / refresh) defers instead of nuking the polling box +
  // its eventual result mid-consult (a job runs 30s–3min). Cleared when a poll reaches a terminal
  // state (or its result box detaches). Module-level so it survives a render() rebuild.
  const _activeJobs = new Set();

  // Friendly zone labels, in IA order — used for the "All" view separators.
  const ZONE_LABELS = [
    ["identity", "Identity"],
    ["operate", "Work"],
    ["mind", "Mind"],
  ];

  // ---- expand-to-modal state (Slice 2) ----
  // `currentView` is the last view render() drew — the focus-modal re-projects from
  // it (so a governed verb fired inside the modal reflects on the next render()).
  // `modalKey` is the entryKey of the expanded panel (null = closed); `modalReturnFocus`
  // is the ⤢ trigger to restore focus to on close; `modalEpisodeQuery` persists the
  // modal's episode search across refresh-rebuilds (the supersede — see renderEpisodes).
  let currentView = null;
  let lastOpts = null;   // the options the last draw was made with: a layout change (K5) redraws the same view the same way
  let modalKey = null;
  let modalGlance = false;   // the open view came from a glance tile: it shows that tile's own typed panel, whatever the entry's kind
  let modalReturnFocus = null;
  // a focus view that is not a layout entry (a Needs-you card): { id, title, build() -> element }. One slot, so the dialog,
  // its Esc, its Tab trap and its focus return are the one implementation every focus view uses.
  let modalCustom = null;
  let modalEpisodeQuery = "";
  // Open-Loops filter query — CLIENT-side (the loops all ride the wire), persisted across
  // re-renders (a verb fires → the board rebuilds → you stay in your filter) and into the
  // focus modal. Empty = show all. Matches spore text OR spore-id.
  let openLoopsQuery = "";
  const sporeQueries = Object.create(null);  // Tray / Keep filter queries, keyed by panel kind
  // The Tray's chosen view ("" = the kernel's own order, else a view id) and the Episodes agent filter
  // ("" = all). Both are display choices that survive a refresh re-render in the same page.
  let trayView = "";
  // Whether the Tray lists the rows held until their surface date. Per viewer and per browser: it is a display
  // choice, so it lives in localStorage (which may be absent or throw) and never on the server. Default hidden.
  const HELD_KEY = "levain.tray.showHeld";
  let trayHeldShown = false;
  try { trayHeldShown = window.localStorage.getItem(HELD_KEY) === "1"; } catch (e) { trayHeldShown = false; }
  const heldMounts = new Set();   // each mounted Tray's toggle and its redraw
  const saveHeldShown = () => { try { window.localStorage.setItem(HELD_KEY, trayHeldShown ? "1" : "0"); } catch (e) { /* the choice then lasts this page only */ } };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // "2026-10-22" -> "Oct 22", from the digits alone (a Date would shift the day by the zone)
  const surfaceLabel = (due) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(due || ""));
    return m && MONTHS[+m[2] - 1] ? MONTHS[+m[2] - 1] + " " + (+m[3]) : String(due || "");
  };
  let episodeAgent = "";
  // The scroll position to re-apply after a modal refresh-rebuild. For the episode
  // panel the result list arrives async (a /recall.json fetch), so buildModal's
  // synchronous restore clamps to 0 — the episode renderer re-applies this once its
  // rows land. null = don't restore (cleared on a fresh user search → resets to top).
  // what the reader holds while the page is drawn again, for the draw in progress (read by a typed panel filled in it)
  let buildKept = null;
  // The focus modal's kept state, carried while its rows are still on the way (a keyword search answers later): every
  // rebuild until they land inherits it, never re-reading the emptied body. modalGen names the build a result belongs to.
  let modalPending = null;
  let modalAwaiting = false;
  let modalGen = 0;

  // The starter jar (masthead): rise level = today's episodes against the entity's own typical
  // day (view.jar, counts from its store); a typical day sits at the dashed mark. A bubble
  // rises for each episode that arrived since the LAST render of the same day, so a bubble is
  // a true new episode, never decoration. status != "ok" -> an empty jar with its reason.
  let jarLastToday = null, jarLastDay = null;
  function renderJar(jar) {
    const j = document.getElementById("jar");
    if (!j) return;
    j.replaceChildren();
    if (!jar) { j.hidden = true; jarLastToday = jarLastDay = null; return; }  // baseline resets while hidden
    j.hidden = false;
    j.classList.toggle("empty", jar.status !== "ok");
    const NS = "http://www.w3.org/2000/svg";
    const mk = (tag, attrs) => {
      const n = document.createElementNS(NS, tag);
      for (const k of Object.keys(attrs)) n.setAttribute(k, attrs[k]);
      return n;
    };
    const svg = mk("svg", { viewBox: "0 0 26 34", role: "img", "aria-label": "starter jar: " + jar.label });
    const clip = mk("clipPath", { id: "jar-clip" });
    clip.appendChild(mk("path", { d: "M5 8 H21 V28 Q21 32 17 32 H9 Q5 32 5 28 Z" }));
    svg.appendChild(clip);
    const top = 10, h = 22;  // the inside of the jar
    const fillH = Math.max(0, Math.min(1, jar.level || 0)) * h;
    svg.appendChild(mk("rect", { class: "jar-fill", "clip-path": "url(#jar-clip)", x: 5,
      y: top + h - fillH, width: 16, height: fillH }));
    svg.appendChild(mk("line", { class: "jar-mark", x1: 4, x2: 22, y1: top + h / 2, y2: top + h / 2 }));
    svg.appendChild(mk("path", { class: "jar-glass", d: "M8 4 H18 V8 H21 V28 Q21 32 17 32 H9 Q5 32 5 28 V8 H8 Z" }));
    // bubbles: one per newly arrived episode (cap 5), only on a same-day rise
    const grew = jar.today != null && jarLastToday != null && jar.day === jarLastDay && jar.today > jarLastToday;
    if (grew && jar.status === "ok") {
      const n = Math.min(5, jar.today - jarLastToday);
      for (let i = 0; i < n; i++) {
        const b = mk("circle", { class: "jar-bubble", cx: 9 + i * 2.5, cy: top + h - fillH + 2, r: 1.4 });
        b.style.animationDelay = (i * 0.25) + "s";
        b.addEventListener("animationend", () => b.remove());
        svg.appendChild(b);
      }
    }
    jarLastToday = jar.today == null ? null : jar.today;
    jarLastDay = jar.day || null;
    j.appendChild(svg);
    j.appendChild(el("span", "jar-label", jar.status === "ok" ? jar.label : jar.label + " (empty)"));
  }

  // The masthead state line: the optional freeform "what's going on" line.
  // `state` is view.state ({text:null} when unset OR expired: the server already dropped
  // an expired one, so nothing here re-derives age). Verbatim text via textContent, age
  // beside it, never interpreted. null/absent -> no live-context source -> hidden.
  function renderState(state) {
    const sEl = document.getElementById("state");
    if (!sEl) return;
    const openEditor = sEl.querySelector(".state-editor");
    if (openEditor && openEditor.dataset.saving !== "1") return;
    sEl.replaceChildren();
    sEl.classList.remove("unset");
    if (!state) { sEl.hidden = true; return; }
    sEl.hidden = false;
    if (!state.text) {
      sEl.classList.add("unset");
      sEl.appendChild(el("span", "ftext", "no state set"));
    } else {
      sEl.appendChild(el("span", "ftext", state.text));
      if (state.age_label) sEl.appendChild(el("span", "fage", state.age_label));
    }
    if (!commit || state.can_edit === false) return;
    const has = !!(state && state.text);
    const btn = el("button", "state-edit", has ? "edit" : "set");
    btn.type = "button";
    btn.title = has ? "edit your state" : "set your state";
    btn.setAttribute("aria-label", has ? "edit state" : "set state");
    btn.addEventListener("click", () => enterStateEdit(sEl, state, btn, {
      kind: "operator_state", placeholder: "what's going on, in your words; blank to clear",
    }));
    sEl.appendChild(btn);
  }

  // ---------------------------------------------------------------- render ----
  function render(view, opts) {
    // The surface injects its write transport here; a port that provides none (the
    // parked MCP-App) renders read-only — no edit affordances, zero other change.
    commit = opts && typeof opts.commit === "function" ? opts.commit : null;
    commitAction = opts && typeof opts.commitAction === "function" ? opts.commitAction : null;
    commitJob = opts && typeof opts.commitJob === "function" ? opts.commitJob : null;
    cancelPending = opts && typeof opts.cancelPending === "function" ? opts.cancelPending : null;
    answerQuestion = opts && typeof opts.answerQuestion === "function" ? opts.answerQuestion : null;
    dismissNote = opts && typeof opts.dismissNote === "function" ? opts.dismissNote : null;
    reviewItem = opts && typeof opts.reviewItem === "function" ? opts.reviewItem : null;
    pollJob = opts && typeof opts.pollJob === "function" ? opts.pollJob : null;
    onJobsIdle = opts && typeof opts.onJobsIdle === "function" ? opts.onJobsIdle : null;
    recall = opts && typeof opts.recall === "function" ? opts.recall : null;
    lastOpts = opts;

    const board = document.getElementById("board");
    if (!board) return;
    proposalRead = null;   // an authoritative reload: a denial or a grant answered since must withdraw its card, not wait out the 30 s
    grantRead = null;      // and a grant revoked since leaves the grants slot
    dispatchRead = null;   // and a dispatch job signed or cancelled since leaves the dispatch slot
    // the typed panels about to be dropped end their timers and mounts first
    if (window.LevainCockpitKinds && window.LevainCockpitKinds.reset) window.LevainCockpitKinds.reset();
    const kept = captureKept(board);   // the rows the reader holds open, restored below once the board is drawn again
    buildKept = kept;                  // a typed panel filled during this draw may owe the reader a later repaint of it
    board.replaceChildren();

    const entityEl = document.getElementById("entity");
    const storeEl = document.getElementById("store");
    if (!view || typeof view !== "object") {
      board.appendChild(el("p", "empty", "No substrate data delivered."));
      keShell(false);      // the KE regions and the state line (moved into the now band) must not outlive the data they showed
      renderState(null);
      closeModal();  // don't leave a focus-modal open over a view we can't render
      return;
    }
    const paths = view.paths || {};
    if (storeEl) {
      storeEl.textContent = paths.episodic_db || "(store path unknown)";
      storeEl.hidden = !!paths.omitted || !!view.ke;   // a manifest source carries no store path: no line, not a false "unknown"
    }
    if (entityEl) {
      const stem = (paths.episodic_db ? String(paths.episodic_db).split("/").pop() : "") || "substrate";
      // A rename editor open in the masthead (spore-280 — the pre-existing twin of the 247
      // state-line stranding). Unlike renderState (which replaceChildren-rebuilds #state and so
      // tears its editor down for free), the entity name toggles #entity's display + hangs a
      // sibling .name-editor on .unit — so the teardown must be EXPLICIT here or a successful
      // rename strands the editor frozen with #entity hidden behind it. Mirrors renderState's
      // data-saving exception: a passive render that slipped past the hasUnsavedEdit gate must
      // NOT tear a still-being-typed editor down (that drops the half-typed name) — skip the
      // rebuild and leave it intact. The authoritative SAVE render marks the editor
      // data-saving="1" (doSave) and DOES rebuild: drop the stranded editor + its hidden
      // trigger, un-hide #entity, and let wireEntityName re-add a fresh, visible rename button.
      const displayName = view.entity_name || stem.replace(/\.db$/, "");
      const unit = entityEl.parentElement;
      const openNameEditor = unit && unit.querySelector(".name-editor");
      if (openNameEditor && openNameEditor.dataset.saving !== "1") {
        // Editor open & NOT saving → a passive/other-write render. Don't tear it down (that drops
        // the half-typed name), BUT still refresh the HIDDEN #entity text so a later Cancel/Escape
        // reveals the latest name, not a stale one — #entity is display:none behind the editor, so
        // this can't clobber the input (codex L3 LOW: an external rename during an open edit).
        entityEl.textContent = displayName;
      } else {
        if (openNameEditor) {  // authoritative SAVE render (data-saving="1") — tear the editor down
          openNameEditor.remove();
          const staleBtn = unit.querySelector(".name-edit");  // hidden (display:none) by enterNameEdit
          if (staleBtn) staleBtn.remove();                    // drop it → wireEntityName re-adds a visible one
          entityEl.style.display = "";
        }
        entityEl.textContent = displayName;
        wireEntityName(entityEl, view);  // Class-A rename affordance (Slice 2a; commit-gated)
      }
    }
    // Masthead branding override (the surface's identity) — applied ONLY when the payload
    // carries it, so a bare Levain install keeps the HTML-default wordmark/model chrome.
    // The bridge sets these to flow-brand its cockpit.
    if (view.brand_wordmark) {
      const wm = document.querySelector(".brand .wordmark");
      if (wm) { wm.textContent = view.brand_wordmark; wm.title = view.brand_wordmark; }   // the title is the whole name where a narrow bar cuts it
      // The browser tab / <title> too — the HTML ships "Levain · substrate control"
      // hardcoded and JS is the only override seam (the static template can't know
      // the brand). Keep the " · substrate control" suffix; swap only the wordmark.
      document.title = view.brand_wordmark + " · substrate control";
    }
    if (view.brand_model) {
      const md = document.querySelector(".brand .model");
      if (md) md.textContent = view.brand_model;
    }
    // The operator's state line: their OWN words, reflected back verbatim (no machine
    // interpretation). Hidden when there is no live-context source; "no state set"
    // when present-but-empty; an expired line arrives already dropped by the server.
    renderState(view.state);
    renderJar(view.jar);
    // Drive the living-rings vital-signs from substrate health: write-path LIVE →
    // steady phosphor heartbeat; DARK → slow, dim-red. The background IS the pulse.
    // (view.scope stays a data-only seam — the UI surfaces it when team scope is
    // real, not as a PM-style profile selector that does nothing yet.)
    const live = !!(view.health ? view.health.write_path_live : view.write_path_live);
    if (document.body) document.body.dataset.vital = live ? "live" : "dark";

    const layout = Array.isArray(view.layout) ? view.layout : [];
    keShell(!!view.ke);
    if (view.ke) renderKe(view, board);
    let lastZone = null;
    for (const entry of view.ke ? [] : layout) {
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
      const node = renderPanelSafe(entry, view);
      if (node) {
        if (CLAMP_KINDS.has(entry.kind)) node.setAttribute("data-clamp", "");
        board.appendChild(node);
      }
    }

    if (view.errors && Object.keys(view.errors).length) {
      board.appendChild(renderErrors(view));
    }
    finalizePanels(board);  // wrap content + bound the long modules (scroll within, no reflow)
    // measure with every row closed, then hand the reader's state back (rows, scroll, overflow cue), then filter
    settleKept(board, kept);
    buildKept = null;
    applyFilter();
    // the cards are on the page and wrapped: the grids learn which columns hold a card on this tab, which card is empty, and which stands
    // alone in its row (measured, so it waits for the bodies)
    if (view.ke && window.LevainCockpitKinds && window.LevainCockpitKinds.fillGrids) window.LevainCockpitKinds.fillGrids();
    // Webfonts can shift line metrics after first paint — recompute the scents once they
    // settle so a panel that only overflows post-font-load still gets the cue + affordance.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => {
        for (const b of board.querySelectorAll(".pbody.clamped")) measureOverflow(b);
        measureClauses(board);  // line metrics shifted → re-measure per-item overflow too
      });
    }
    // The open focus-modal (if any) re-projects from this same fresh view, so a
    // governed verb fired INSIDE it reflects at once and it stays put (its place +
    // its episode search are kept — the inline reset is superseded).
    currentView = view;
    if (modalKey) refreshModal();
  }

  // Long modules get a BOUNDED body that scrolls within itself past a max-height
  // (overscroll-contained so a scroll doesn't chain to the page at the panel's bounds,
  // with a thin glass scrollbar) — the grid stays spatially stable (no expand-in-place
  // reflow), short panels render at natural height (no wasted space, no scrollbar).
  // Replaces the clamp+fade+click-to-expand model (Slice 1.5): bounded overview +
  // details-on-demand (the search box + the Slice-2 modal).
  const CLAMP_KINDS = new Set(["config", "section", "episodes", "wraps", "crystals", "spores", "tray", "keep", "external", "edits"]);

  // Measure whether a bounded body overflows its cap; set the overflow SCENT (.has-overflow)
  // AND a keyboard scroll affordance accordingly. RE-RUNNABLE — clears the state when content
  // no longer overflows (search shrinking results, font reflow) so the scent never goes stale.
  // Reads layout synchronously; valid because render() builds into the live #board, so the
  // .clamped max-height is actually applied (a detached board would read zero overflow).
  // The work lists draw whole rows and say how many they leave out: a list that does not fit its card folds the rows below the fold
  // (display none, never a half row) and ends in an "N more" link that opens the panel's focus view. Re-runnable: a redraw, a filter
  // or a resize unfolds first and measures again.
  const FOLD_PANELS = ["tray", "loops", "keep", "episodes", "now", "inbox", "roster", "stream", "flow-episodes", "flow-spores"];
  // what a folding list is made of: its rows (a work row, a roster row, a log line) and the headings that group them
  const FOLD_ROW = ".row, .rrow, .lrow", FOLD_HEAD = ".grp-head, .grp-h";
  function foldRows(body) {
    const panel = body.closest && body.closest(".panel[data-ck-panel]");
    if (!panel || FOLD_PANELS.indexOf(panel.dataset.ckPanel) < 0 || panel.closest(".modal")) return false;
    // the reading place survives every fold trigger (a row opening, a search, fonts settling): unfolding and folding again
    // changes the body's height, and the browser clamps or anchors the scroll under the reader
    const place = body.scrollTop;
    try { return foldRowsAt(body, panel); } finally { if (body.scrollTop !== place) body.scrollTop = place; }
  }
  function foldRowsAt(body, panel) {
    for (const n of body.querySelectorAll("[data-folded]")) { n.style.display = ""; delete n.dataset.folded; }
    const old = panel.querySelector(":scope > .more-foot:not([data-readall])"); if (old) old.remove();   // the "Read all" foot is memFoot's: removing it here made memFoot re-add it, a childList change that re-ran this, without end
    if (!(body.scrollHeight > body.clientHeight + 1)) return false;
    const top = body.getBoundingClientRect().top;
    const heldLine = body.querySelector(":scope > .held-line, .held-line");
    const limit = body.clientHeight - 34 - (heldLine ? heldLine.offsetHeight : 0);   // room kept for the link and the held-back line
    const isRow = (n) => n.matches(FOLD_ROW), isHead = (n) => n.matches(FOLD_HEAD);
    const items = Array.prototype.filter.call(body.querySelectorAll(FOLD_ROW + ", " + FOLD_HEAD), (n) => !n.parentElement.closest(FOLD_ROW));
    // where bands sit side by side (Morning's Tray: Overdue | Today) each column has its own fold, since a column that overflows says
    // nothing about the one beside it; a band laid in the same flow (`display: contents`) shares the one
    const colOf = (n) => { const c = n.closest(".grp-col"); return c && body.contains(c) && window.getComputedStyle(c).display !== "contents" ? c : null; };
    const buckets = new Map();
    for (const n of items) { const k = colOf(n); if (!buckets.has(k)) buckets.set(k, []); buckets.get(k).push(n); }
    let hidden = 0;
    for (const list of buckets.values()) {
      let cut = false;
      // a row the reader opened is the one thing they are reading: it and every row above it stay, however tall it
      // grew (the body then scrolls inside the card); only the rows after it fold. A fold that took the open row
      // away closed it under the reader on the click and again on every redraw.
      let pin = -1;
      list.forEach((n, i) => { if (n.classList.contains("row-expanded")) pin = i; });
      for (const [i, n] of list.entries()) {
        const r = n.getBoundingClientRect();
        // rects are measured against where the body is scrolled to: add the scroll back, or a scrolled body folds too few rows
        if (!cut && i > pin && r.bottom - top + body.scrollTop > limit) cut = true;
        if (cut) { n.style.display = "none"; n.dataset.folded = "1"; if (isRow(n)) hidden++; }
      }
      // a heading left with no row under it goes too
      for (let i = 0; i < list.length; i++) {
        if (!isHead(list[i]) || list[i].dataset.folded) continue;
        const next = list.slice(i + 1).find((n) => !n.dataset.folded && isRow(n));
        const nextHead = list.slice(i + 1).findIndex((n) => isHead(n) && !n.dataset.folded);
        const nextRow = next ? list.indexOf(next) : -1;
        if (nextRow < 0 || (nextHead >= 0 && i + 1 + nextHead < nextRow)) { list[i].style.display = "none"; list[i].dataset.folded = "1"; }
      }
    }
    if (!hidden) return false;
    // a list that has more rows than it first draws ends in its own "show all N" link; once the card folds, two links to the same rest would
    // disagree on the count, so the card keeps one: the foot, saying how many the focus view holds
    const xm = body.querySelector(".xmore"), total = xm ? parseInt((/\d+/.exec(xm.textContent) || [])[0], 10) : NaN;
    if (xm) { xm.style.display = "none"; xm.dataset.folded = "1"; }
    const more = el("button", "more-foot", Number.isFinite(total) && total > hidden ? "Read all " + total : hidden + " more");
    more.type = "button";
    more.setAttribute("aria-label", (Number.isFinite(total) && total > hidden ? "Read all " + total + " rows" : hidden + " more rows") + ": open the full list");
    more.addEventListener("click", () => { const x = panel.querySelector(".pexpand"); if (x) x.click(); });
    panel.appendChild(more);
    return true;
  }

  function measureOverflow(body) {
    const folded = foldRows(body);
    const atEnd = () => body.scrollTop + body.clientHeight >= body.scrollHeight - 1;
    const over = body.scrollHeight > body.clientHeight + 1;
    // a folded card still scrolls when the reader's open row is taller than it: it keeps the keyboard affordance below,
    // and only the "more below" fade is skipped (the "N more" link is its cue)
    body.classList.toggle("has-overflow", over && !folded);
    // the fade and the "Read all" foot key off this one class: set here for every bounded body, cleared when the content fits again
    body.classList.toggle("is-clipped", over && !folded);
    const memPanel = body.closest && !body.closest(".modal") ? body.closest(".ck-mem-nest, .ck-mem-row > .panel") : null;
    if (memPanel) memFoot(memPanel);
    // Keyboard access (codex L3): make the scroll region focusable + named so arrow-keys
    // scroll a text-only panel (no focusable child) — the affordance the expander gave.
    if (over) {
      body.tabIndex = 0;
      body.setAttribute("role", "region");
      const h = body.parentElement && body.parentElement.querySelector(".phead h2");
      body.setAttribute("aria-label", (h && h.textContent ? h.textContent + " — " : "") + "scrollable");
      // The "more below" cue is a fade mask on the last 24px (CSS, gated on .has-overflow:not(.at-end)),
      // never a glyph over the text; it goes when the bottom is reached. Wire the scroll toggles once;
      // finalizePanels builds a fresh .pbody each render, so no listener piles up across renders.
      if (!body.dataset.scrollWired) {
        body.dataset.scrollWired = "1";
        body.addEventListener("scroll", () => { body.classList.toggle("at-end", atEnd()); }, { passive: true });
      }
      body.classList.toggle("at-end", !folded && atEnd());  // recompute now (content size may have changed)
    } else {
      body.removeAttribute("tabindex");
      body.removeAttribute("role");
      body.removeAttribute("aria-label");
      body.classList.remove("at-end");
    }
  }

  // Per-ITEM overflow scent — the row-level twin of measureOverflow. A clause clamps to 3
  // lines (CSS); reveal its expand toggle ONLY when the FULL canonical text actually
  // overflows that clamp, so short rows stay clean. Reads layout synchronously (valid for
  // the same reason measureOverflow is — the rows are live in #board / the modal). Skips a
  // row the user already expanded (leave its toggle as "less"). RE-RUNNABLE.
  function measureClauses(root) {
    for (const clause of root.querySelectorAll(".clause.clampable")) {
      const row = clause.closest(".row");
      if (!row || row.classList.contains("row-expanded")) continue;
      if (row.dataset.folded) continue;   // display:none reads 0 high: leave a folded row's toggle as it was measured
      // No +1 tolerance: the harm is asymmetric — a false NEGATIVE hides the tail with no
      // toggle (the bug this slice fixes), a false positive is a harmless toggle on a 2-line
      // row. Line-clamp overflow is full-line-granular + heights are integers, so an exactly-
      // 2-line clause measures equal (no spurious toggle) [kimi L3]. .clampable only.
      row.classList.toggle("has-more", clause.scrollHeight > clause.clientHeight);
    }
  }

  function finalizePanels(board) {
    for (const p of board.querySelectorAll(".panel")) finalizePanel(p);
  }
  function finalizePanel(p) {
    {
      const phead = p.querySelector(".phead");
      if (!phead) return;
      const body = el("div", "pbody");
      while (phead.nextSibling) body.appendChild(phead.nextSibling);
      p.appendChild(body);
      // Bound the long modules — the CSS (max-height + overflow-y:auto) does the rest:
      // a short panel stays natural height, an overflowing one scrolls inside itself.
      if (p.hasAttribute("data-clamp")) {
        body.classList.add("clamped");
        // Overflow cue + keyboard affordance (see measureOverflow): a "more below" fade
        // mask + a focusable scroll region, shown only when the body actually exceeds the cap.
        measureOverflow(body);
      }
      // editable Class-A config panels get their edit affordance on its own right-
      // aligned row at the panel BOTTOM (after the pbody).
      if (p.dataset.editable === "1" && p._levainEdit) {
        p.appendChild(buildEditRow(p, p._levainEdit.entry, p._levainEdit.doc));
      }
      // K5: a zone card's collapse and move controls, in the same draw as its body (dashPass keeps them right after a repaint)
      const zgrid = p.closest(".ck-zgrid");
      if (zgrid && !emptyProvider(p)) layoutControls(zgrid, p);
    }
  }

  // Dispatch a single manifest entry to its renderer. Singleton kinds read their
  // data from the matching view field; indexed kinds (config/section) use `ref`.
  // `opts` (modal context) is forwarded to the kinds that vary in the focus modal.
  // One panel whose data a renderer cannot draw is an unavailable panel, never a blank board or a
  // stale modal: the board and the focus modal both draw through this.
  // ---------------------------------------------------------------- the KE page ----
  // A manifest that declares hero / brief / widgets is drawn as the page's own regions: the hero first (the now band: clock, date,
  // the state line, the wrap line; the sky as a card under it), the brief, one row of widgets, then each zone as a grid of cards.
  // The masthead above them is the page's bar and stays where the static page put it.
  const CK_SIZES = ["short", "medium", "tall"];   // ascending height; a grid row is as tall as its tallest card
  const CK_COLUMNS = 3;
  const tabChoice = new Map();                     // zone/title -> the tab the operator chose, kept across renders
  // the one structural node the KE page moves into a region: the state line, which lives in the now band while the KE page is up.
  // A region that fails restores it here BEFORE anything is cleared, so no catch can strand it inside a region it is about to wipe.
  // (The masthead and the brand no longer move: the bar is the same on every page.)
  function restoreState() {
    const state = document.getElementById("state"), store = document.getElementById("store");
    if (state && store && store.parentNode && state.parentNode !== store.parentNode) store.parentNode.insertBefore(state, store);
  }
  // a failed hero render: the CONTENT mounts are emptied (stale weather and sky beside the error line would read as current);
  // the structural mounts (#ck-hero, #ck-band, #ck-skycol) stay for the next render
  function restoreHero() {
    for (const id of ["ck-when", "ck-sky", "ck-legend", "ck-wrapline"]) { const m = document.getElementById(id); if (m) { m.replaceChildren(); if (id === "ck-legend") m.hidden = true; } }
  }
  function keShell(on) {
    const hero = document.getElementById("ck-hero"), brief = document.getElementById("ck-brief"), widgets = document.getElementById("ck-widgets");
    if (!hero || !brief || !widgets) return;
    hero.hidden = brief.hidden = widgets.hidden = !on;
    const needsHost = document.getElementById("ck-needs");
    if (needsHost && !on) clearNeeds(needsHost);   // the strip is drawn only by the KE page, and only while something waits (renderNeeds)
    const notesHost = document.getElementById("ck-notes");
    if (notesHost && !on) { clearNeeds(notesHost); noteBodies.clear(); }   // and the notes strip, likewise (renderNotes)
    // the hero's legend follows the hero: shown by the sky's own render, hidden with the region
    const band = document.getElementById("ck-legend");
    if (band) band.hidden = true;
    // the section headers are page chrome that follows its region: hidden here, set by the region's own render
    for (const id of ["ck-h-brief", "ck-h-widgets"]) { const h = document.getElementById(id); if (h) h.hidden = true; }
    document.body.classList.toggle("ke", on);
    const bd = document.getElementById("board");
    if (bd) bd.classList.toggle("ke", on);
    if (on) return;
    // back to the pre-KE page: the state line returns to where the static page put it
    restoreState();
    // what the regions drew is stale once the page is no longer the KE page
    for (const id of ["ck-when", "ck-sky", "ck-legend", "ck-wrapline", "ck-brief", "ck-widgets"]) { const h = document.getElementById(id); if (h) h.replaceChildren(); }
  }

  function ckAttrs(node, entry, kind) {
    node.dataset.ckPanel = entry.id;
    node.dataset.ckKind = kind || "unknown";
    if (entry.modes) node.dataset.modes = entry.modes;
    return node;
  }
  // a typed panel's kind as the page grades it: a count face is a `count`, never the row kind it came from
  function ckKindOf(entry, data) {
    if (data && data.face === "count") return "count";
    return entry.pkind || (data && data.kind) || entry.kind || "unknown";
  }

  // one bare node (no panel header): the hero's weather and sky, the brief, the widgets. It draws what a zone card draws
  // for the same panel: the banner (stale / partial / held back / an alert), the note, then the content in whichever
  // shape the contract allows (markdown, a typed face, or lines). An alert is content, so it suppresses the empty message.
  // An error sentence from a provider may carry a raw ISO timestamp ("failing since 2026-10-21T22:37:12.126051+00:00"): shown as the local clock
  // ("6:37 PM", with the day when it is not today), the exact stamp kept in the element's title. The text sits in ONE span so the dot beside it
  // stays a single flex sibling of a wrapping line.
  const ISO_AT = /\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)/g;
  function humanTimesIn(text) {
    const out = [];
    let at = 0, m;
    ISO_AT.lastIndex = 0;
    while ((m = ISO_AT.exec(text)) !== null) {
      const d = new Date(m[0]);
      if (Number.isNaN(d.getTime())) continue;
      if (m.index > at) out.push(text.slice(at, m.index));
      const same = d.toDateString() === new Date().toDateString();
      const clock = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      const t = el("time", null, same ? clock : d.toLocaleDateString([], { month: "short", day: "numeric" }) + ", " + clock);
      t.title = m[0];
      t.dateTime = m[0];
      out.push(t);
      at = m.index + m[0].length;
    }
    if (at < text.length) out.push(text.slice(at));
    return out;
  }
  function errorLine(text) {
    const p = el("p", "err"), span = el("span");
    span.append(...humanTimesIn(String(text)));
    p.appendChild(span);
    return p;
  }
  function bareNode(entry, view, cls, ctx) {
    const data = (view.extra_panels || {})[entry.id] || {};
    const node = el("div", cls);
    ckAttrs(node, entry, ckKindOf(entry, data));
    const K = window.LevainCockpitKinds;
    if (data.error) { node.appendChild(errorLine("unavailable \u2014 " + data.error)); return node; }
    appendBanner(node, entry);
    if (data.note && !(ctx && ctx.widget && data.face === "count")) node.appendChild(richEl("div", "ext-note", data.note));
    const alerted = (entry.banner || []).some((b) => b.alert);
    if (data.markdown != null) {
      const md = String(data.markdown);
      if (md.trim() === "") { if (!alerted) node.appendChild(richEl("p", "empty", data.empty || "nothing here")); }
      else node.appendChild(renderMarkdown(md));
      return node;
    }
    if (data.typed) {
      if (!K) { node.appendChild(el("p", "err", "unavailable — this panel cannot be drawn here")); return node; }
      K.fill(node, data, Object.assign({ rows: appendExternalLines, entry: entry, alerted: alerted }, ctx || {}));
      return node;
    }
    if (!Array.isArray(data.lines)) { node.appendChild(el("p", "err", "unavailable — this panel cannot be drawn here")); return node; }
    if (data.lines.length) appendExternalLines(node, data.lines);
    else if (!alerted) node.appendChild(richEl("p", "empty", data.empty || "nothing here"));
    return node;
  }

  // The band's right cell, never blank. With wrap history: "Last wrap Mon 06:02" over the wrap count (the health panel's own total when
  // the page holds it; else "N wraps held", or "N+ wraps" when the list is as long as a history list is sent: it is a floor then, not
  // a count). Without history: "No wraps yet" over a fact the install has (its name, and the day's episodes when the jar counts them).
  // When the wraps panel could not be read the line says nothing about wraps at all: unreadable is not "never".
  function wrapLineOf(view) {
    const WRAP_LIST_CAP = 50;   // the longest history list the page is sent
    const ws = Array.isArray(view.wraps) ? view.wraps : [];
    const unread = !!((view.errors && view.errors.wraps) || (view.health && view.health.wrap_history_unavailable));
    const last = ws.length && ws[0] && typeof ws[0].wrapped_at === "string" ? ws[0] : null;
    const at = last ? new Date(last.wrapped_at) : null;
    const box = el("div", "wrapline");
    const first = el("div"), second = el("div");
    if (at && !Number.isNaN(at.getTime())) {
      const when = at.toLocaleDateString(undefined, { weekday: "short" }) + " " + at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
      first.append(el("b", null, "Last wrap"), " " + when);
      const total = view.health && Number.isInteger(view.health.total_wraps) ? view.health.total_wraps : null;
      second.textContent = total !== null ? total + (total === 1 ? " wrap" : " wraps")
        : ws.length >= WRAP_LIST_CAP ? ws.length + "+ wraps" : ws.length + (ws.length === 1 ? " wrap held" : " wraps held");
    } else {
      const today = view.jar && Number.isInteger(view.jar.today) ? view.jar.today + " today" : "";
      const name = typeof view.entity_name === "string" ? view.entity_name : "";
      if (unread) {
        first.appendChild(el("b", null, name || "This install"));
        second.textContent = today;
      } else {
        const total = view.health && Number.isInteger(view.health.total_wraps) ? view.health.total_wraps : 0;
        if (total > 0) {   // the history list was not sent but the health panel counts wraps: a count, never "none yet"
          first.appendChild(el("b", null, total + (total === 1 ? " wrap" : " wraps")));
          second.textContent = [name, today].filter(Boolean).join(" · ");
        } else {
          first.appendChild(el("b", null, "No wraps yet"));
          second.textContent = [name, today].filter(Boolean).join(" · ") || "a fresh install";
        }
      }
    }
    box.append(first, second);
    return box;
  }

  let heroEpoch = 0;
  function renderHero(view, byId) {
    const hero = document.getElementById("ck-hero"), when = document.getElementById("ck-when"), wrapline = document.getElementById("ck-wrapline");
    const skyHost = document.getElementById("ck-sky");
    // The legend band (#ck-legend) is the hero's: cleared and hidden here, filled and placed with the other mounts at
    // the end, and only when a sky painted a key. Until then the sky hands its legend to `place`, which holds it off
    // the page; after, `place` writes the band (a hot repaint) and `hide` clears it (a dropped or failed sky).
    const band = document.getElementById("ck-legend");
    if (band) { band.replaceChildren(); band.hidden = true; }
    let live = false, pending = null;
    const epoch = ++heroEpoch;   // a sky from an earlier hero render (torn down late) must not touch this render's band
    const legendHook = {
      place: (node) => { if (!band || epoch !== heroEpoch) return false; if (live) { band.replaceChildren(node); band.hidden = false; } else pending = node; return true; },
      hide: () => { if (epoch !== heroEpoch) return; pending = null; if (live && band) { band.replaceChildren(); band.hidden = true; } }
    };
    const entries = view.ke.hero.map((id) => byId.get(id)).filter(Boolean);
    const kindOf = (e) => ((view.extra_panels || {})[e.id] || {}).kind;
    const wx = entries.find((e) => kindOf(e) === "reading"), sky = entries.find((e) => kindOf(e) === "ambient");
    // the now band: the clock and date at the left, the state line beside them (placed by renderBriefRegion), and at the right the
    // wrap line (always something real: the last wrap, or "No wraps yet" and a fact the install has), the sky's event count and the
    // weather when a provider supplies one. A bare install has no sky and no weather and the band is still whole.
    const clock = el("div", "clock num"), date = el("div", "date"), events = el("div", "events");
    clock.dataset.ckHero = "clock"; date.dataset.ckHero = "date"; events.dataset.ckHero = "events";
    // everything that can throw is built off the page; the structural mounts (when, wrap line, sky)
    // are touched only once all of it exists
    const whenKids = [], metaKids = [], skyKids = [];
    const stamp = el("div", "clockbox");
    stamp.append(clock, date);
    whenKids.push(stamp);
    const lastWrap = wrapLineOf(view);
    if (!wx) metaKids.push(lastWrap);   // with a weather card the wrap line rides the card's foot row instead (no dead band under it)
    metaKids.push(events);
    if (wx) {
      const node = bareNode(wx, view, "wx");
      node.dataset.ckHero = "weather";
      lastWrap.classList.add("wx-wrap");
      const V = window.LevainCockpitView;
      if (V && typeof V.refetchPanel === "function") {
        // re-reads this panel's source now; the source decides how fresh that is (the read age under the figure says)
        const again = el("button", "wx-refresh", "↻");
        again.type = "button";
        again.title = "Read the weather again";
        again.setAttribute("aria-label", "Read the weather again");
        // busy, not disabled: a disabled button drops the keyboard focus of the reader who pressed it
        let busy = false;
        again.addEventListener("click", () => {
          if (busy) return;
          busy = true;
          again.setAttribute("aria-busy", "true");
          Promise.resolve().then(() => V.refetchPanel(wx.id)).catch(() => "fault").then((r) => {
            // `false`: the panel cannot be re-read on its own (no longer external, its shape moved, or a board render
            // landed meanwhile): the board reloads, as the stream does with the same answer
            const T = window.LevainCockpitTransport;
            if (r === false && T && typeof T.reload === "function") return T.reload();
            return r;
          }).catch(() => "fault").finally(() => { busy = false; again.removeAttribute("aria-busy"); });
        });
        node.append(lastWrap, again);
      } else node.appendChild(lastWrap);
      metaKids.push(node);
    }
    if (sky) skyKids.push(bareNode(sky, view, "ck-sky", { hero: { events: events, place: legendHook.place, hide: legendHook.hide } }));
    for (const e of entries) if (e !== wx && e !== sky) skyKids.push(bareNode(e, view, "ck-card"));
    // the clock is mounted only now: nothing after this can throw, so no clock ticks on a node that never attached
    if (window.LevainCockpitKinds) window.LevainCockpitKinds.mountClock(clock, date);
    when.replaceChildren(...whenKids);
    if (wrapline) wrapline.replaceChildren(...metaKids);
    skyHost.replaceChildren(...skyKids);
    hero.hidden = false;
    live = true;
    if (band && sky && pending) { band.replaceChildren(pending); band.hidden = false; }
    pending = null;
  }

  // TODAY: where to begin, one card. The state line is not in it any more: it lives in the now band (#ck-state-slot), so a
  // bare install, which has no brief panel, draws no Today card and no heading at all.
  function renderBriefRegion(view, byId) {
    const region = document.getElementById("ck-brief");
    const state = document.getElementById("state");
    const slot = document.getElementById("ck-state-slot");
    const entries = view.ke.brief.map((id) => byId.get(id)).filter(Boolean);
    const bodies = entries.map((e) => bareNode(e, view, "ck-brief-body", { region: "brief" }));   // may throw: #state has not moved yet
    if (state && slot) { state.dataset.ckPanel = "state"; state.dataset.ckKind = "line"; slot.appendChild(state); }
    if (bodies.length) {
      const card = el("div", "ck-card ck-briefcard");
      for (const node of bodies) card.appendChild(node);
      region.replaceChildren(card);
    } else region.replaceChildren();
    region.hidden = !entries.length;
    const hb = document.getElementById("ck-h-brief"); if (hb) hb.hidden = region.hidden;   // page chrome: the section's header follows its region
  }

  function renderWidgets(view, byId) {
    const region = document.getElementById("ck-widgets");
    const entries = view.ke.widgets.map((id) => byId.get(id)).filter(Boolean);
    const cards = [];
    const fitLines = [];
    for (const e of entries) {
      const data = (view.extra_panels || {})[e.id] || {};
      const node = el("div", "ck-card w");
      ckAttrs(node, e, ckKindOf(e, data));
      node.appendChild(el("div", "k", String(e.title).replace(/\s*\(\d+\)\s*$/, "")));
      const body = bareNode(e, view, "wbody", { widget: true });
      // the card is the node the page grades; the typed mount inside it carries the kind class
      body.removeAttribute("data-ck-panel"); body.removeAttribute("data-ck-kind");
      node.appendChild(body);
      // a count tile is one answer with a list behind it: it opens that list in the focus modal (a button, so the keyboard reaches it).
      // Any other tile that does not draw everything it has (a "+N more", a clipped run of text) is the same kind of button: the
      // fit pass in cockpit_view.js says so with a "levain-hidden" event on the tile's body, and the focus view shows all of it.
      // Decisions has no modal while something waits: the strip under the band is where it is answered, so the tile takes the
      // reader there. With nothing waiting it opens the history (decided requests, outcomes, proposals).
      const open = () => {
        if (e.id === "decisions" && needsFocus()) return;
        openModal(findEntry(currentView, entryKey(e)) || e, node, true);
      };
      const onKey = (ev) => {
        if (ev.target !== node || (ev.key !== "Enter" && ev.key !== " ")) return;
        ev.preventDefault();
        open();
      };
      const setOpenable = (on) => {
        node.classList.toggle("w-open", on);
        if (on) {
          node.tabIndex = 0;
          node.setAttribute("role", "button");
          node.setAttribute("aria-haspopup", "dialog");
          // the entry as the page holds it now: a refetch since this tile was drawn may have changed its rows and its title
          node.setAttribute("aria-label", "Open " + String(e.title).replace(/\s*\(\d+\)\s*$/, "") + " in a focus view");
        } else { node.removeAttribute("tabindex"); node.removeAttribute("role"); node.removeAttribute("aria-haspopup"); node.removeAttribute("aria-label"); }
      };
      node.addEventListener("click", (ev) => {
        // a link, button or field inside the tile is its own control: the tile does not open under it
        const inner = ev.target && ev.target.closest ? ev.target.closest("a, button, input, select, textarea") : null;
        if (inner && inner !== node && node.contains(inner)) return;
        if (node.classList.contains("w-open")) open();
      });
      node.addEventListener("keydown", (ev) => { if (node.classList.contains("w-open")) onKey(ev); });
      if (data.typed && !data.error) node.addEventListener("levain-hidden", (ev) => { if (ev.target === body) setOpenable(ev.detail > 0); });
      else if (!data.typed && !data.error && Array.isArray(data.lines) && data.lines.length) fitLines.push(() => fitLineTile(body, setOpenable));
      cards.push(node);
    }
    region.replaceChildren(...cards);
    region.hidden = !entries.length;
    pruneLineTiles();     // the last render's tiles went with their cards: nothing may keep watching them
    for (const f of fitLines) f();
    if (window.LevainCockpitKinds && window.LevainCockpitKinds.fitWidgets) window.LevainCockpitKinds.fitWidgets(region);   // after attach: the room a tile has is only known on the page
    if (!region.dataset.ckWatched && typeof MutationObserver === "function") {   // the glance tiles alone can change: the strip's count and shape follow them
      region.dataset.ckWatched = "1";
      new MutationObserver(scheduleLayout).observe(region, { childList: true });
    }
    const hw = document.getElementById("ck-h-widgets"); if (hw) hw.hidden = region.hidden;
    // --ck-n (the tile grid's column count) has one writer: LevainCockpitKinds.fillGrids
  }

  // An untyped lines tile (rows the page draws itself) draws the rows that fit the tile and counts the rest in a "+N more" line, then
  // is a button to the whole list; a single row too tall for the tile is cut to two lines. It is fitted again when its room changes.
  // ONE observer watches every untyped line tile on the page, and lets go of a tile once its body has left the page, so a cockpit
  // that refreshes for days holds the current render's tiles and nothing older.
  const lineFits = new Map();
  let lineRO = null;
  function pruneLineTiles() {
    for (const b of Array.from(lineFits.keys())) if (!b.isConnected) { if (lineRO) lineRO.unobserve(b); lineFits.delete(b); }
  }
  function fitLineTile(body, setOpenable) {
    // the provider's notes are chrome (kept, and cut to two lines when the room is short); the rows are the items
    const all = Array.from(body.children);
    // only `.row` elements are items; a group heading is kept only with a row of its group that is shown
    const rows = all.filter((c) => c.classList.contains("row"));
    const fit = () => {
      if (!body.isConnected) return;
      body.classList.remove("clamped");
      const fits = () => body.scrollHeight <= body.clientHeight + 1;
      let shown = rows.length;
      const draw = () => {
        let ri = 0;
        const keep = [];
        for (const c of all) {
          if (c.classList.contains("row")) { if (ri < shown) keep.push(c); ri++; }
          else if (c.classList.contains("ext-grouphead")) { if (ri < shown) keep.push(c); }
          else keep.push(c);
        }
        body.replaceChildren(...keep);
        if (shown < rows.length) body.appendChild(el("div", "xnote", "+" + (rows.length - shown) + " more"));
      };
      draw();
      while (!fits() && shown > 1) { shown--; draw(); }
      if (!fits()) {
        body.classList.add("clamped");     // the notes and the row's text cut to two lines; then, if it is still too tall, the rows give way
        while (!fits() && shown > 0) { shown--; draw(); }
      }
      setOpenable(shown < rows.length || body.classList.contains("clamped"));
    };
    fit();
    if (typeof ResizeObserver === "function") {
      pruneLineTiles();
      if (!lineRO) lineRO = new ResizeObserver((recs) => { for (const r of recs) { const f = lineFits.get(r.target); if (f) f(); } });
      lineFits.set(body, fit);
      lineRO.observe(body);
    }
  }

  function ckPlaceholder(entry) {
    const node = el("div", "panel");
    node.dataset.zone = entry.zone || "";
    node.dataset.editClass = entry.edit_class || "";
    const head = el("div", "phead");
    head.appendChild(el("h2", null, entry.title || entry.id || "panel"));
    node.appendChild(head);
    node.appendChild(el("p", "err", "unavailable — this panel could not be drawn"));
    return node;
  }

  // a zone's cards, in the kernel's order, packed into rows of CK_COLUMNS; a row is as tall as its tallest card
  const ZONE_GLOSS = { operate: "what is waiting, what is moving" };
  function renderZone(zone, view, byId, board) {
    const cards = [];
    for (const c of zone.cards) {
      const entries = c.panels.map((id) => byId.get(id)).filter(Boolean);
      if (entries.length) cards.push({ entries: entries, size: c.size, span: c.span, tabs: c.tabs, key: entries[0].id });
    }
    if (!cards.length) return;
    const laid = ckArrange(zone.id, cards);   // K5: the saved order; null for a zone the layout does not reach (the Memory and Identity compositions)
    const sec = el("section", "ck-zone");
    sec.dataset.zone = zone.id;
    // the title is the manifest's; the one-line gloss beside it is page chrome (a zone the page has no gloss for gets none)
    const head = el("div", "zone-head");
    head.appendChild(el("div", "zone-title", zone.title));
    if (Object.hasOwn(ZONE_GLOSS, zone.id)) head.appendChild(el("span", "zone-gloss", ZONE_GLOSS[zone.id]));
    if (laid) ckZoneReset(head);   // K5: Reset layout lives in a laid-out zone's head, and only while a layout is saved
    sec.appendChild(head);
    const grid = el("div", "ck-zgrid");
    const entryOf = new Map();   // node -> its manifest entry (the Memory tab's composition reads it)
    grid.dataset.ckRegion = "zone";
    grid.dataset.ckZone = zone.id;
    if (laid) grid._ckOrder = laid;
    // rows: sequential fill, the way a 3-column grid auto-places spans
    const rows = [];
    let used = CK_COLUMNS + 1;
    for (const c of cards) {
      if (used + c.span > CK_COLUMNS) { rows.push([]); used = 0; }
      rows[rows.length - 1].push(c); used += c.span;
    }
    for (const row of rows) {
      const tallest = row.reduce((m, c) => Math.max(m, CK_SIZES.indexOf(c.size)), 0);
      for (const c of row) {
        const nodes = [];
        for (const e of c.entries) {
          // a panel that cannot be drawn keeps its place (and its tab) as a placeholder, so tabs and panels stay in step
          const node = renderPanelSafe(e, view) || ckPlaceholder(e);
          if (CLAMP_KINDS.has(e.kind)) node.setAttribute("data-clamp", "");
          ckAttrs(node, e, ckKindOf(e, (view.extra_panels || {})[e.id]));
          node.dataset.ckCard = c.key;
          // a kernel kind drawn by one of the page's own components is still a typed mount: it names its kind too
          if (!Array.from(node.classList).some((k) => /^k-/.test(k))) node.classList.add("k-" + (e.pkind || e.kind));
          node.classList.add("ck-h-" + CK_SIZES[tallest], "ck-span-" + c.span);
          if (e.kind === "external") node.dataset.ckExternal = "1";
          ckCountPill(node, e.count);
          entryOf.set(node, e);
          nodes.push(node);
          grid.appendChild(node);
        }
        // the Memory tab shows every neocortex section at once (composeMemory), so its section card has no tabs
        const sectionsOnly = zone.id === "mind" && c.entries.every((e) => e.kind === "section");
        if (c.tabs && nodes.length > 1 && !sectionsOnly) wireCardTabs(zone.id + "/" + c.tabs, c.tabs, c.entries, nodes);
      }
    }
    if (zone.id === "mind" || zone.id === "identity") composeMemory(zone.id, grid, view, entryOf);
    stackColumns(grid);
    sec.appendChild(grid);
    board.appendChild(sec);
    dashPass(grid); pairBoard(grid); memClips(grid); balanceStacks(grid);
    if (typeof MutationObserver === "function") new MutationObserver(() => { dashPass(grid); pairBoard(grid); memClips(grid); balanceStacks(grid); scheduleLayout(); }).observe(grid, { childList: true, subtree: true });
    // a card folds or empties by a class set after the draw (markEmptyCards), which no childList mutation reports: the pairs and the
    // "Read all" feet follow it. Only a change to the classes pairBoard reads wakes it: the classes these passes write are not among them.
    if (typeof MutationObserver === "function") {
      const watched = (v) => CK_PAIR_SKIP.filter((c) => (" " + (v || "") + " ").indexOf(" " + c + " ") >= 0).join(" ");
      new MutationObserver((recs) => {
        // measureOverflow and memFoot toggle is-clipped / has-overflow on cards inside the grid: reacting to those would feed this observer forever
        if (recs.some((r) => watched(r.oldValue) !== watched(r.target.getAttribute("class")))) { pairBoard(grid); memClips(grid); balanceStacks(grid); }
      }).observe(grid, { attributes: true, attributeFilter: ["class"], attributeOldValue: true, subtree: true });
    }
  }

  // A panel's count ("Tray (2)" in the kernel's title) is a pill beside the title, hidden at 0: the title says what the card is, the
  // pill how many it holds. The number is the panel's own count (`count`, from the manifest), never parsed out of the title's words:
  // a title that ends in a number of its own ("Annual plan (2026)") keeps it. Only the kernel's own " (count)" suffix is lifted off.
  // The pill is a sibling of the h2, so the heading's text (what the jump box lists) stays the name alone.
  function ckCountPill(node, count) {
    if (!Number.isInteger(count) || count < 0) return;
    const h2 = node.querySelector(":scope > .phead > h2");
    if (!h2) return;
    const tail = " (" + count + ")";
    const text = String(h2.textContent);
    if (text.endsWith(tail)) h2.textContent = text.slice(0, -tail.length).replace(/\s+$/, "");
    const pill = el("span", "ck-count", String(count));
    pill.hidden = count === 0;
    h2.after(pill);
  }

  // The Board is two independent stacks, not rows that pair: Tray and Now down the left, Open loops and Keep down the right, each as
  // tall as its own cards, so the shorter card of a pair never leaves a hole. Other tabs lay the same cards out as plain grid items
  // (the stacks are display: contents there). Only a zone that holds both sides is stacked; a card the table does not name stays a
  // direct, full-width child.
  const CK_STACK_SIDE = Object.assign(Object.create(null), { tray: 0, now: 0, loops: 1, keep: 1 });
  // Top to bottom inside a side when nothing is saved (the CSS used to draw this with `order`, which would override a saved order).
  const CK_STACK_RANK = Object.assign(Object.create(null), { tray: 0, now: 1, loops: 0, keep: 1 });
  // ---- K5: collapse and reorder the cards of a zone, saved per browser (cockpit_view.js holds the storage). Reorder is within a zone and,
  // on the Board's two stacks, within a side. The Memory and Identity zones are composed by composeMemory (their own rows and sections),
  // so the layout does not reach them; the now band, sky, widgets, brief and Needs-you strip are not zone cards and have no controls.
  const CK_NO_LAYOUT = new Set(["mind", "identity"]);
  const layoutKit = () => { const K = window.LevainCockpitKinds; return K && K.layoutOrder ? K : null; };
  // the cards in their drawn order (the saved order over the kernel's), their keys left on the zone's grid for the move buttons
  function ckArrange(zoneId, cards) {
    const K = layoutKit();
    if (!K || CK_NO_LAYOUT.has(zoneId)) return null;
    // a zone that holds both stack sides starts from the stacks' own top-to-bottom, not the kernel's
    const sided = cards.filter((c) => CK_STACK_SIDE[c.key] !== undefined);
    if (sided.some((c) => CK_STACK_SIDE[c.key] === 0) && sided.some((c) => CK_STACK_SIDE[c.key] === 1)) {
      const sorted = sided.slice().sort((a, b) => CK_STACK_RANK[a.key] - CK_STACK_RANK[b.key]);   // stable: equal ranks keep the kernel's order
      let n = 0;
      for (let i = 0; i < cards.length; i++) if (CK_STACK_SIDE[cards[i].key] !== undefined) cards[i] = sorted[n++];
    }
    const order = K.layoutOrder(zoneId, cards.map((c) => c.key));
    const at = new Map(order.map((k, i) => [k, i]));
    cards.sort((a, b) => at.get(a.key) - at.get(b.key));
    return order;
  }
  // The layout controls are off while a draft is open or a job is polling (a redraw would throw it away): every handler checks this
  // BEFORE it writes anything, and the controls say why they are off.
  const CK_GATE_WHY = "finish or cancel the edit first";
  const CK_EDIT_OPEN = ".text-editor, .edit-ta, .verb-edit-ta";
  // a zone's cards in the DOM, found by comparing dataset values (a panel id is never put into a selector)
  const ckPanels = (grid) => Array.from(grid.querySelectorAll(":scope > .panel, :scope > .ck-col > .panel"));
  const ckNodesOf = (grid, key) => ckPanels(grid).filter((n) => n.dataset.ckCard === key);
  function ckFind(zone, key) {
    for (const g of document.querySelectorAll(".ck-zgrid")) if (g.dataset.ckZone === zone) return ckNodesOf(g, key)[0] || null;
    return null;
  }
  // A card whose place CSS `order` pins (a fold line, a dashed line, Recent episodes on the Board, the Build jobs / edits / loops rows) takes
  // a move as a no-op: it gets no move buttons and is never a neighbour. One set of keys here, never a CSS selector list beside it.
  const CK_PIN_BOARD = new Set(["episodes"]);
  const CK_PIN_BUILD = new Set(["jobs", "jobs-none", "edits", "loops"]);
  function ckPinned(zone, key, nodes) {
    if (nodes.some((n) => n.classList.contains("ck-fold") || n.classList.contains("ck-dash"))) return true;
    const mode = document.body && document.body.dataset ? document.body.dataset.mode : "";
    return (mode === "board" && CK_PIN_BOARD.has(key)) || (mode === "build" && zone === "operate" && CK_PIN_BUILD.has(key));
  }
  // What a zone's controls need, read once per pass: the folded set, which cards are pinned, which are movable AND on screen (a card in
  // another tab is not a neighbour), and whether the gate is shut.
  function ckCtx(grid) {
    const K = layoutKit(), zone = grid.dataset.ckZone;
    const ctx = { folded: K ? K.layoutCollapsed() : new Set(), pinned: new Set(), ok: new Set(), gated: hasUnsavedEdit() };
    for (const key of grid._ckOrder || []) {
      const nodes = ckNodesOf(grid, key);
      if (ckPinned(zone, key, nodes)) { ctx.pinned.add(key); continue; }
      if (nodes.some((n) => n.getClientRects().length > 0)) ctx.ok.add(key);
    }
    return ctx;
  }
  // the nearest visible, movable card in `dir` (-1 earlier, +1 later) on the same stack side; -1 when there is none
  function ckNeighbor(grid, key, dir, ctx) {
    const order = grid._ckOrder || [], ok = (ctx || ckCtx(grid)).ok;
    const side = (k) => (grid.classList.contains("ck-twocol") && CK_STACK_SIDE[k] !== undefined ? CK_STACK_SIDE[k] : -1);
    const i = order.indexOf(key);
    if (i < 0) return -1;
    for (let j = i + dir; j >= 0 && j < order.length; j += dir) if (ok.has(order[j]) && side(order[j]) === side(key)) return j;
    return -1;
  }
  // the one Reset layout control: in a zone head (never the top bar, which has no room at any width), drawn only while a layout is saved
  function ckZoneReset(head) {
    const K = layoutKit();
    let have = head.querySelector(":scope > .ck-zreset");
    if (K && K.hasLayout && K.hasLayout()) {
      if (!have) {
        have = el("button", "layoutreset ck-zreset", "Reset layout");
        have.type = "button";
        have.setAttribute("data-layout-reset", "");
        head.appendChild(have);
      }
      const gated = hasUnsavedEdit();
      have.disabled = gated;
      have.title = gated ? CK_GATE_WHY : "Put the panels back in the default order and unfold them";
    } else if (have) have.remove();
  }
  function ckSyncResets() {
    for (const g of document.querySelectorAll(".ck-zgrid")) {
      const head = g._ckOrder && g.parentNode ? g.parentNode.querySelector(":scope > .zone-head") : null;
      if (head) ckZoneReset(head);
    }
  }
  function ckRedraw() {
    if (!currentView || hasUnsavedEdit()) return;   // a draft in an open editor is not thrown away for a reorder
    renderKeepingPlace(currentView, lastOpts);
  }
  // a button's panel, grid and card key are read when it is CLICKED: a panel that survives a redraw (adopted) keeps its buttons
  const ckAt = (ev) => {
    const p = ev.currentTarget.closest(".panel"), grid = p && p.closest(".ck-zgrid");
    return grid && grid._ckOrder && p.dataset.ckCard ? { p, grid, key: p.dataset.ckCard } : null;
  };
  function ckMove(ev, dir, focusCls) {
    const K = layoutKit(), at = ckAt(ev);
    if (!K || !at || hasUnsavedEdit()) return;   // the gate comes before any write
    const { grid, key } = at, zone = grid.dataset.ckZone, j = ckNeighbor(grid, key, dir);
    if (j < 0) return;
    const order = grid._ckOrder.slice(), to = order[j];
    order.splice(order.indexOf(key), 1);
    order.splice(order.indexOf(to) + (dir > 0 ? 1 : 0), 0, key);
    K.setLayoutOrder(zone, order);
    ckRedraw();
    // the redraw rebuilt the buttons: a keyboard user stays on the control they pressed (or its partner, when this card is now at the end)
    const node = ckFind(zone, key);
    const btn = node && (node.querySelector(".phead .ck-lay-" + focusCls + ":not(:disabled)") || node.querySelector(".phead .ck-lay-btn:not(:disabled)"));
    if (btn) btn.focus();
  }
  function ckFold(ev) {
    const K = layoutKit(), at = ckAt(ev);
    if (!K || !at || hasUnsavedEdit()) return;
    const { p, grid, key } = at, now = !p.classList.contains("ck-collapsed");
    if (now && ckNodesOf(grid, key).some((n) => n.querySelector(CK_EDIT_OPEN))) return;   // never fold a card whose edit row is open
    K.setLayoutCollapsed(key, now);
    const ctx = ckCtx(grid);
    for (const n of ckPanels(grid)) layoutControls(grid, n, ctx);
    ckSyncResets();
  }
  function ckButton(cls, glyph, label, onclick) {
    const b = el("button", "ck-lay-btn " + cls, glyph);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.addEventListener("click", onclick);
    return b;
  }
  // idempotent: dashPass calls it for every panel of a zone grid after every repaint. A dashed F7 line gets none.
  function layoutControls(grid, p, ctx) {
    const K = layoutKit(), head = p.querySelector(":scope > .phead"), body = p.querySelector(":scope > .pbody");
    let box = head ? head.querySelector(":scope > .ck-lay") : null;
    const on = !!(K && head && body && grid._ckOrder && p.dataset.ckCard && !p.classList.contains("ck-dash"));
    if (!on) {
      if (box) box.remove();
      p.classList.remove("ck-collapsed");
      return;
    }
    ctx = ctx || ckCtx(grid);
    const key = p.dataset.ckCard, id = p.dataset.ckPanel;
    const name = (head.querySelector("h2") || {}).textContent || id;
    const folded = ctx.folded.has(key);   // folded by CARD key: every panel of a tabbed card folds together
    p.classList.toggle("ck-collapsed", folded);
    if (!box) {
      box = el("span", "ck-lay");
      box.appendChild(ckButton("ck-lay-up", "↑", "", (ev) => ckMove(ev, -1, "up")));
      box.appendChild(ckButton("ck-lay-down", "↓", "", (ev) => ckMove(ev, 1, "down")));
      box.appendChild(ckButton("ck-lay-fold", "", "", ckFold));
      head.appendChild(box);
    }
    if (!body.id) body.id = "ck-pb-" + id.replace(/[^\w-]/g, "_");
    box.classList.toggle("ck-nomove", ctx.pinned.has(key));
    const say = (b, label, off) => { b.disabled = off; b.setAttribute("aria-label", label); b.title = off && ctx.gated ? CK_GATE_WHY : label; };
    say(box.querySelector(".ck-lay-up"), "Move " + name + " earlier", ctx.gated || ckNeighbor(grid, key, -1, ctx) < 0);
    say(box.querySelector(".ck-lay-down"), "Move " + name + " later", ctx.gated || ckNeighbor(grid, key, 1, ctx) < 0);
    const fold = box.querySelector(".ck-lay-fold");
    const editOpen = !folded && ckNodesOf(grid, key).some((n) => n.querySelector(CK_EDIT_OPEN));
    say(fold, (folded ? "Expand " : "Collapse ") + name, ctx.gated || editOpen);
    fold.setAttribute("aria-expanded", folded ? "false" : "true");
    fold.setAttribute("aria-controls", body.id);
  }
  // The controls' state reads the screen (which cards are visible, whether a draft is open), so a tab switch, a click or typing asks for a
  // fresh pass over every zone grid; one debounced path.
  let ckRefreshTimer = null;
  function ckRefreshAll() {
    for (const g of document.querySelectorAll(".ck-zgrid")) {
      balanceStacks(g); memClips(g);   // a tab switch shows a tab for the first time: its stacks and capped cards have heights only now
      if (!g._ckOrder) continue;
      const ctx = ckCtx(g);
      for (const p of ckPanels(g)) layoutControls(g, p, ctx);
    }
    ckSyncResets();
  }
  function ckScheduleRefresh() {
    if (ckRefreshTimer !== null) window.clearTimeout(ckRefreshTimer);
    ckRefreshTimer = window.setTimeout(() => { ckRefreshTimer = null; ckRefreshAll(); }, 60);
  }
  document.addEventListener("click", ckScheduleRefresh);
  document.addEventListener("input", ckScheduleRefresh);
  window.addEventListener("hashchange", ckScheduleRefresh);
  document.addEventListener("ck-layout-refresh", ckScheduleRefresh);   // a tab change (cockpit_view applyMode)
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") ckScheduleRefresh(); });   // Esc closes an editor
  function stackColumns(grid) {
    const kids = Array.from(grid.children);
    const side = (n) => CK_STACK_SIDE[n.dataset.ckPanel];
    if (!kids.some((k) => side(k) === 0) || !kids.some((k) => side(k) === 1)) return;
    const first = kids.find((k) => side(k) !== undefined);
    const cols = [el("div", "ck-col"), el("div", "ck-col")];
    cols[0].dataset.side = "left"; cols[1].dataset.side = "right";
    grid.insertBefore(cols[0], first);
    grid.insertBefore(cols[1], first);
    for (const k of kids) if (side(k) !== undefined) cols[side(k)].appendChild(k);
    grid.classList.add("ck-twocol");
  }

  // F4: below the two stacks the Board's full-width cards sit two to a row (Inbox | Fleet, Stream | Recent episodes). A card is marked
  // data-ck-pair = "l" | "r" in the order it is drawn (Recent episodes is pinned last by CSS, so it is last here), an odd one out is
  // "solo" and keeps its full row. A folded or empty card keeps the full row it already has and is no partner. Re-runnable: it is
  // asked again whenever a card folds, empties or appears, and it writes an attribute only when the value changed.
  const CK_PAIR_SKIP = ["ck-fold", "ck-dash", "ck-is-empty", "ck-tab-off"];
  function pairBoard(grid) {
    if (!grid.classList.contains("ck-twocol")) return;
    const direct = Array.from(grid.children).filter((n) => n.classList.contains("panel"));
    const set = (n, v) => { if (v === null) { if (n.hasAttribute("data-ck-pair")) n.removeAttribute("data-ck-pair"); } else if (n.getAttribute("data-ck-pair") !== v) n.setAttribute("data-ck-pair", v); };
    const ok = direct.filter((n) => !CK_PAIR_SKIP.some((c) => n.classList.contains(c)));
    for (const n of direct) if (!ok.includes(n)) set(n, null);
    const pinnedLast = (n) => (CK_PIN_BOARD.has(n.dataset.ckPanel) ? 1 : 0);
    const seq = ok.slice().sort((a, b) => pinnedLast(a) - pinnedLast(b));   // stable
    seq.forEach((n, i) => set(n, i === seq.length - 1 && seq.length % 2 === 1 ? "solo" : (i % 2 === 0 ? "l" : "r")));
  }

  // The two Board stacks end where their own cards end, so a long right stack beside a short left one leaves a blank band under the
  // left. When one stack is more than STACK_RATIO taller than the other, the taller stack's LAST card takes a cap that ends the two
  // stacks together (never below STACK_FLOOR, never above its own cap), and its overflow goes behind the card's own "more" and focus
  // view like every other capped card. The cap is a data attribute plus a custom property: neither is a class, so nothing watches it.
  // It reads natural heights (the cap cleared first), so repeating it settles on the same answer.
  const STACK_RATIO = 1.25, STACK_FLOOR = 300;
  function balanceStacks(grid) {
    if (!grid.classList.contains("ck-twocol")) return;
    const cols = Array.from(grid.children).filter((n) => n.classList.contains("ck-col") && n.offsetParent !== null);
    for (const n of grid.querySelectorAll(":scope > .ck-col > .panel[data-stack-cap]")) { n.removeAttribute("data-stack-cap"); n.style.removeProperty("--stack-cap"); }
    if (cols.length !== 2 || Math.abs(cols[0].getBoundingClientRect().top - cols[1].getBoundingClientRect().top) > 2) return;   // one column: nothing sits beside anything
    const hs = cols.map((c) => c.offsetHeight);
    const t = hs[0] >= hs[1] ? 0 : 1;
    if (hs[t] <= hs[1 - t] * STACK_RATIO) return;
    const cards = Array.from(cols[t].children).filter((n) => n.classList.contains("panel") && n.offsetParent !== null && !CK_PAIR_SKIP.some((c) => n.classList.contains(c)));
    if (cards.length < 2) return;
    const last = cards[cards.length - 1];
    const cap = Math.max(STACK_FLOOR, hs[1 - t] - (hs[t] - last.offsetHeight));
    if (cap >= last.offsetHeight) return;
    last.dataset.stackCap = "1";
    last.style.setProperty("--stack-cap", cap + "px");
  }

  // F6: a card the page caps (a neocortex section, a panel of the Crystallized | Cognition row) that clips its content ends in a
  // "Read all" foot that opens that card's focus view. The foot is built once and shown only while the card clips, so a card that
  // fits shows nothing. Which element clips (the card or its body) depends on the style, so both are measured, with the foot hidden
  // so it cannot be what makes the card clip.
  function memFoot(panel) {
    let foot = panel.querySelector(":scope > .more-foot[data-readall]");
    const body = panel.querySelector(":scope > .pbody");
    const cut = (n) => !!n && n.scrollHeight > n.clientHeight + 1;
    if (foot) foot.hidden = true;
    const clipped = panel.offsetParent !== null && (cut(panel) || cut(body));
    panel.classList.toggle("is-clipped", clipped);
    if (!clipped) { if (foot) foot.hidden = true; return; }
    if (!foot) {
      foot = el("button", "more-foot", "Read all");
      foot.type = "button";
      foot.dataset.readall = "1";
      foot.setAttribute("aria-label", "Read all of " + ((panel.querySelector(":scope > .phead h2") || {}).textContent || "this card") + ": open the focus view");
      foot.addEventListener("click", () => {
        const x = panel.querySelector(".pexpand");
        if (x) x.click(); else if (panel._ckEntry) openModal(panel._ckEntry, foot);
      });
      panel.appendChild(foot);
    }
    foot.hidden = false;
  }
  let memResizeWired = false;
  function wireMemResize() {
    if (memResizeWired || typeof window.addEventListener !== "function") return;
    memResizeWired = true;
    let t = null;
    window.addEventListener("resize", () => { window.clearTimeout(t); t = window.setTimeout(() => { for (const g of document.querySelectorAll(".ck-zgrid")) { memClips(g); balanceStacks(g); } }, 150); });
  }
  function memClips(grid) {
    wireMemResize();
    for (const p of grid.querySelectorAll(".ck-mem-nest, .ck-mem-row > .panel")) {
      const body = p.querySelector(":scope > .pbody");
      if (body) measureOverflow(body); else memFoot(p);
    }
  }

  // F7, the empty provider: an external panel that reports empty and says how to fix it collapses to ONE dashed line (its title, the
  // sentence, the fix), never a card with a body gap. An empty provider is an external panel with no rows, or one row that is the
  // provider's own report (a `no_` state key; its words are never read). Team without a ledger and an Inbox with no source
  // are the cases. A compose box stays reachable: the line carries its action ("Send"), which opens the box under the line.
  // A card of the page's own (Tray, Keep, Identity) keeps its card: its box is its content. An empty list card folds in
  // markEmptyCards (cockpit_view.js). Idempotent: it toggles classes and adds the action once, so the observer that calls it again
  // cannot loop.
  function emptyProvider(p) {
    if (!p.dataset.ckPanel || !p.dataset.ckExternal) return false;
    const rows = p.querySelectorAll(".pbody .row");
    if (rows.length === 0) return !!p.querySelector(".pbody .empty");
    if (rows.length > 1) return false;
    const key = rows[0].dataset.keepKey || "";
    return /^no_/.test(key);   // the provider's own sentinel row id; a row's words (a command in backticks) say nothing about it
  }
  function dashAction(p) {
    const head = p.querySelector(":scope > .phead");
    const compose = p.querySelector(".tray-dump");
    let act = head ? head.querySelector(".ck-dash-act") : null;
    if (!head || !compose) { if (act) act.remove(); p.classList.remove("ck-dash-open"); return; }
    if (act) return;
    const go = compose.querySelector(".tray-dump-btn");
    const label = (go && go.textContent.trim()) || "Write";
    act = el("button", "ck-dash-act", label.charAt(0).toUpperCase() + label.slice(1));
    act.type = "button";
    act.setAttribute("aria-expanded", "false");
    act.addEventListener("click", () => {
      const open = p.classList.toggle("ck-dash-open");
      act.setAttribute("aria-expanded", open ? "true" : "false");
      const ta = compose.querySelector("textarea, input");
      if (open && ta) ta.focus();
    });
    head.appendChild(act);
  }
  function dashPass(grid) {
    for (const p of grid.querySelectorAll(".panel")) {
      const dash = emptyProvider(p);
      p.classList.toggle("ck-dash", dash);
      if (dash) dashAction(p);
      else {
        // a card that stops being dashed gives its line's action and open state back
        const act = p.querySelector(":scope > .phead > .ck-dash-act");
        if (act) act.remove();
        p.classList.remove("ck-dash-open");
      }
    }
    // the controls come after every dashed line is settled, and read the grid once for the whole pass
    const ctx = grid._ckOrder ? ckCtx(grid) : null;
    for (const p of grid.querySelectorAll(".panel")) layoutControls(grid, p, ctx || undefined);
  }
  // The zone grids' layout (which columns are live, which card takes its row) is measured after layout. A repaint inside a grid (a card
  // folding, a dashed line appearing, a refetch) can change it, so the same observer that re-runs dashPass asks for it again here:
  // one debounced path, however many mutations a repaint makes.
  let layoutTimer = null;
  function scheduleLayout() {
    if (layoutTimer !== null) window.clearTimeout(layoutTimer);
    layoutTimer = window.setTimeout(() => {
      layoutTimer = null;
      const K = window.LevainCockpitKinds;
      if (K && typeof K.fillGrids === "function") K.fillGrids();
    }, 60);
  }

  // ---- the Memory tab (K6) --------------------------------------------------------------------------------------------------
  // The mind zone is ONE neocortex card with every section readable at once (State, threads, context and understanding left;
  // patterns and decisions right), then Crystallized patterns and Cognition side by side. The identity zone is ONE card of eight rows:
  // the identity cards themselves, restyled into rows (title | one-line summary | the card's own Edit), the editor opening in place.
  // Nothing is hidden and nothing is pressed for the reader: each panel the kernel drew keeps doing its own work.
  const MEM_WRAP_CAP = 50;   // the longest history list the page is sent
  // the authoritative wrap count when the view has one; the list is capped, so its length is only ever a floor
  const memWrapTotal = (view) => (view.health && Number.isInteger(view.health.total_wraps) ? view.health.total_wraps : null);
  function memWrapsLine(view) {
    const ws = Array.isArray(view.wraps) ? view.wraps : [];
    const total = memWrapTotal(view);
    // the newest stamp, wherever it sits in the list
    let last = null;
    for (const w of ws) {
      const d = w && typeof w.wrapped_at === "string" ? new Date(w.wrapped_at) : null;
      if (d && !Number.isNaN(d.getTime()) && (!last || d > last)) last = d;
    }
    if (total === null && !ws.length) return "";
    let out = total !== null ? total + (total === 1 ? " wrap" : " wraps")
      : ws.length >= MEM_WRAP_CAP ? ws.length + "+ wraps" : ws.length + (ws.length === 1 ? " wrap" : " wraps");
    if (total === 0) return "";
    if (last) {
      out += " · last wrap " + last.toLocaleDateString(undefined, { weekday: "short" }) + " " +
        last.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    }
    return out;
  }
  // the Cognition header's figures, the same on both of its tabs. The write path speaks only when it is unreadable.
  function memCogBits(view) {
    const g = view.graph, gn = g && Array.isArray(g.nodes) ? g.nodes.length : 0, ge = g && Array.isArray(g.edges) ? g.edges.length : 0;
    const listed = Array.isArray(view.wraps) ? view.wraps.length : 0, total = memWrapTotal(view);
    const ws = total !== null ? total : listed;
    const bits = [];
    if (ws) bits.push((total === null && ws >= MEM_WRAP_CAP ? ws + "+" : String(ws)) + (ws === 1 ? " consolidation" : " consolidations"));
    if (gn) bits.push(gn + " nodes");
    if (ge) bits.push(ge + " edges");
    // the write path speaks when it cannot be read; a store with no wraps yet is not a fault. The graph says when links stopped.
    const wst = (view.health && view.health.status) || view.write_path_status;
    if (wst === "unreadable") bits.push("write path unreadable");
    if (g && g.formation_off_at_wrap != null) bits.push("no new links since wrap " + g.formation_off_at_wrap);
    return bits;
  }
  function memCard(cls, title, meta, modes) {
    const card = el("div", "panel ck-mem-card " + cls);
    card.dataset.zone = cls.indexOf("ident") >= 0 ? "identity" : "mind";
    if (modes) card.dataset.modes = modes;
    const head = el("div", "phead");
    head.appendChild(el("h2", null, title));
    if (meta) head.appendChild(el("span", "ck-mem-meta", meta));
    card.appendChild(head);
    return card;
  }
  // a kernel card moved inside another: its own chrome (the expand control, the edit-class chip, the source path) stays out of the
  // way, and it is never clamped, so the section reads in full
  function memNest(node) {
    node.classList.add("ck-mem-nest");
    node.removeAttribute("data-clamp");
    node.classList.remove("ck-tab-off");
    return node;
  }
  function composeMemory(zoneId, grid, view, entryOf) {
    const nodes = Array.from(grid.children).filter((n) => n.classList && n.classList.contains("panel"));
    if (!nodes.length) return;
    const pid = (n) => n.dataset.ckPanel || "";
    const modes = nodes[0].dataset.modes || "memory";
    if (zoneId === "identity") {
      // the identity cards are config panels (the page's own) or typed text panels (the kernel's `config:*` panels): either is a card with
      // its own editor, and the card IS the row
      const rows = nodes.filter((n) => /^config(:|$)/.test(pid(n)) || (entryOf.get(n) || {}).kind === "config");
      if (!rows.length) return;
      const card = memCard("ck-mem-ident", "Identity", "", modes);
      card.querySelector(".phead h2").after(el("span", "ck-count", String(rows.length)));
      card.querySelector(".phead").appendChild(el("span", "ck-mem-meta", "read at every session start"));
      const list = el("div", "ck-mem-ilist");
      for (const n of rows) {
        memNest(n);
        n.classList.add("ck-mem-rowcard");
        list.appendChild(n);
      }
      card.appendChild(list);
      grid.insertBefore(card, grid.firstChild);
      return;
    }
    // the mind zone
    const secs = nodes.filter((n) => (entryOf.get(n) || {}).kind === "section");
    const crystals = nodes.find((n) => pid(n) === "crystals");
    const cog = nodes.filter((n) => pid(n) === "graph" || pid(n) === "wraps");
    const RIGHT = /^(patterns|decisions)$/i;
    const card = memCard("ck-mem-neo", "Neocortex", memWrapsLine(view), modes);
    // a section with no text is left out unless it can be edited: an editable one stays as a slim row that keeps its Edit
    const full = secs.filter((n) => !n.dataset.ckNoText || n.dataset.ckSlim);
    for (const n of secs) if (!full.includes(n)) n.remove();
    if (!full.length) card.appendChild(el("p", "empty", "No wrap yet: the first consolidation writes this."));
    else {
      const left = el("div", "ck-mem-col"), right = el("div", "ck-mem-col");
      for (const n of full) { n._ckEntry = entryOf.get(n); (RIGHT.test(String((entryOf.get(n) || {}).heading || "").trim()) ? right : left).appendChild(memNest(n)); }
      const cols = el("div", "ck-mem-cols");
      cols.appendChild(left);
      if (right.children.length) cols.appendChild(right); else cols.classList.add("ck-mem-one");
      if (!left.children.length) { cols.classList.add("ck-mem-one"); cols.replaceChildren(right); }
      card.appendChild(cols);
    }
    grid.insertBefore(card, grid.firstChild);
    // Crystallized patterns beside Cognition (about 60 / 40)
    const row = el("div", "ck-mem-row");
    row.dataset.modes = modes;
    if (crystals) {
      const lv = (Array.isArray(view.crystal_index) ? view.crystal_index : []).map((c) => num(c && c.level)).filter((x) => x > 0);
      const chead = crystals.querySelector(".phead");
      if (lv.length && chead) {
        const lo = Math.min(...lv), hi = Math.max(...lv);
        chead.appendChild(el("span", "ck-mem-meta", lo === hi ? "level " + lo : "levels " + lo + " to " + hi));
      }
      crystals.classList.add("ck-mem-crys");
      crystals._ckEntry = entryOf.get(crystals);
      crystals.classList.remove("ck-fold");   // the kernel's dashed one-liner spans its grid; here it keeps its side of the row
      row.appendChild(crystals);
    }
    if (cog.length) {
      const ws = Array.isArray(view.wraps) ? view.wraps.length : 0;
      const bits = memCogBits(view);
      const graph = cog.find((n) => pid(n) === "graph") || cog[0];
      if (!ws) {
        // no wrap yet: no history to tab to, so no tab row and no second panel; the card is the title and its one line
        for (const n of cog) { const t = n.querySelector(".ck-tabs"); if (t) t.remove(); if (n !== graph) n.remove(); }
        cog.splice(0, cog.length, graph);
      }
      // the figures ride the header of every Cognition tab, so whichever is showing carries them
      if (bits.length) for (const n of cog) { const gh = n.querySelector(".phead"); const t = gh && (gh.querySelector(".ck-card-title") || gh.querySelector("h2")); if (t) t.after(el("span", "ck-mem-meta", bits.join(" · "))); }
      graph.classList.add("ck-mem-cog");
      for (const n of cog) { n._ckEntry = entryOf.get(n); row.appendChild(n); }
    }
    if (row.children.length) grid.insertBefore(row, card.nextSibling);
  }

  // Tabs of one card: each panel of the group is a full card, only the chosen one shows, and every one carries
  // the same tab row in its header, so whichever is showing has the card's title and the way to the others.
  function wireCardTabs(key, title, entries, nodes) {
    let at = Math.min(tabChoice.has(key) ? tabChoice.get(key) : 0, nodes.length - 1);
    const buttons = [];
    const show = () => {
      nodes.forEach((n, i) => n.classList.toggle("ck-tab-off", i !== at));
      for (const group of buttons) group.forEach((b, i) => b.setAttribute("aria-selected", i === at ? "true" : "false"));
      // the cards the grid shows changed: re-measure its last row
      const K = window.LevainCockpitKinds;
      if (K && typeof K.fillGrids === "function") K.fillGrids();
    };
    nodes.forEach((node, ni) => {
      // the jump box reaches a panel inside a tab card by showing its tab first
      node.ckShowTab = () => { at = ni; tabChoice.set(key, ni); show(); };
      const head = node.querySelector(".phead");
      if (!head) return;
      const h2 = head.querySelector("h2");
      // the panel keeps its own title in its h2 (what it is, to a reader and to the jump box); the card's title
      // is the visible heading, and the tab row is how to reach the others
      if (h2) h2.classList.add("ck-tab-own");
      const ct = el("div", "ck-card-title", title);
      head.insertBefore(ct, h2 ? h2.nextSibling : head.firstChild);
      const nav = el("div", "ck-tabs");
      nav.setAttribute("role", "tablist");
      const group = [];
      entries.forEach((e, i) => {
        const b = el("button", "ck-tab", e.title);
        b.type = "button";
        b.setAttribute("role", "tab");
        b.addEventListener("click", () => { at = i; tabChoice.set(key, i); show(); });
        group.push(b); nav.appendChild(b);
      });
      buttons.push(group);
      head.insertBefore(nav, ct.nextSibling);
    });
    show();
  }

  // Each region is drawn on its own, under ONE rule: it builds its fallible content off the page and attaches it last
  // (structural nodes, the masthead and #state, move only then). So a throw leaves the page as it was, the region's
  // `restore` hands any structural node it owns back to its non-KE home, and the catch writes a line into the region's
  // dedicated error child. `fill` regions (the brief, the widgets) have no structural children and show only the error;
  // the hero keeps its mounts and gains the error line beside them. A zone passes its own `show`.
  function guardRegion(name, draw, show, restore) {
    try { draw(); } catch (e) {
      if (restore) restore();
      show(el("p", "err ck-region-err", "unavailable — " + name + " could not be drawn (" + (e && e.message ? e.message : "bad data") + ")"));
    }
  }
  const regionErr = (id, fill) => (node) => {
    const h = document.getElementById(id); if (!h) return;
    h.hidden = false;
    const head = document.getElementById("ck-h-" + id.slice(3)); if (head) head.hidden = false;
    if (fill) h.replaceChildren(node);   // only after the region's restore ran: nothing structural is left inside
    else { for (const old of h.querySelectorAll(":scope > .ck-region-err")) old.remove(); h.appendChild(node); }
  };
  function renderKe(view, board) {
    const byId = new Map();
    for (const e of view.layout) byId.set(e.id, e);
    for (const old of document.querySelectorAll("#ck-hero > .ck-region-err")) old.remove();   // a clean hero render clears its last error line
    guardRegion("the hero", () => renderHero(view, byId), regionErr("ck-hero", false), restoreHero);
    guardRegion("the brief", () => renderBriefRegion(view, byId), regionErr("ck-brief", true), restoreState);
    guardRegion("the needs strip", () => renderNeeds(view), regionErr("ck-needs", true));
    guardRegion("the notes strip", () => renderNotes(view), regionErr("ck-notes", true));
    guardRegion("the widgets", () => renderWidgets(view, byId), regionErr("ck-widgets", true));
    // a zone builds its section off to the side and attaches it last, so a throw leaves nothing half-drawn behind
    for (const zone of view.ke.zones) guardRegion(zone.title, () => renderZone(zone, view, byId, board), (node) => board.appendChild(node));
  }

  function renderPanelSafe(entry, view, opts) {
    try { return renderPanel(entry, view, opts); } catch (e) {
      const node = el("div", "panel");
      node.dataset.zone = entry.zone || "";
      node.dataset.editClass = entry.edit_class || "";
      const name = [entry.title, entry.id].find((v) => typeof v === "string" && v) || "panel";
      node.appendChild(el("div", "phead", name));
      node.appendChild(el("p", "err", "unavailable — could not be drawn (" + (e && e.message ? e.message : "bad data") + ")"));
      return node;
    }
  }

  function renderPanel(entry, view, opts) {
    // a glance tile opens its own typed panel in full, not the zone card of the same name
    if (opts && opts.glance && ((view.extra_panels || {})[entry.id] || {}).typed) return renderExternal(entry, view, opts);
    switch (entry.kind) {
      case "health": return renderHealth(entry, view);
      case "graph": return renderGraph(entry, view);
      case "crystals": return renderCrystals(entry, view);
      case "spores": return renderSpores(entry, view);
      case "tray": return renderTray(entry, view);
      case "keep": return renderKeep(entry, view);
      case "episodes": return renderEpisodes(entry, view, opts);
      case "wraps": return renderWraps(entry, view);
      case "section": return renderSection(entry, (view.sections || [])[entry.ref]);
      case "config": return renderConfig(entry, (view.config_docs || [])[entry.ref]);
      case "edits": return renderEdits(entry, view);
      case "external": return renderExternal(entry, view, opts);
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
    const st = h.status || (h.write_path_live ? "live" : "no-wraps");
    const b = el("span", "badge " + (st === "live" ? "live" : (st === "unreadable" ? "dark" : "neutral")),
      st === "live" ? "LIVE" : (st === "unreadable" ? "UNREADABLE" : "NO WRAPS YET"));
    const vWrap = el("span", "v"); vWrap.appendChild(b);
    head.appendChild(vWrap);
    p.appendChild(head);

    // the substrate oracles (facts, never an activity counter); absent on a manifest source or an older levain
    {
      const bits = [];
      const K = typeof window !== "undefined" ? window.LevainCockpitKinds : null;
      // the save's stamp (carried by the page's own model) draws through the shared age node so it ticks; an older model gets the text
      const stamped = !!(h.save_at && K && K.ageSpan);
      if (h.save_age_hours != null) bits.push(stamped ? null : "last save " + (h.save_age_hours >= 1 ? Math.round(h.save_age_hours) + "h ago" : "under 1h ago"));
      if (h.projection_converged != null) bits.push(h.projection_converged ? "projection converged" : "PROJECTION DIVERGED (" + (h.projection_note || "continuity file differs from the last wrap (edited since?)") + ")");
      else if (h.projection_note) bits.push("projection " + h.projection_note);
      if (h.audit_backlog != null) bits.push(!h.audit_backlog ? "no audit write failures recorded" : "AUDIT: " + h.audit_backlog + " audit writes failed");
      if (bits.length && !stamped) kv(p, "substrate", bits.join(" · "));
      else if (bits.length) {
        const row = el("div", "kv");
        row.appendChild(el("span", "k", "substrate"));
        const v = el("span", "v");
        bits.forEach((b, i) => {
          if (i) v.appendChild(document.createTextNode(" · "));
          if (b === null) { v.appendChild(document.createTextNode("last save ")); v.appendChild(K.ageSpan(h.save_at)); }
          else v.appendChild(document.createTextNode(b));
        });
        row.appendChild(v);
        p.appendChild(row);
      }
    }
    // a manifest source may not carry every figure: show only what was sent, never a made-up 0.00
    kv(p, "graduations", `${fmt(h.graduations_validated_total)} validated / ${fmt(h.graduations_demoted_total)} demoted`);
    kv(p, "episodes", `${fmt(h.total_episodes)} (${fmt(h.episodes_since_wrap)} since wrap)`);
    if (h.wrap_history_unavailable) {
      // the wraps panel could not be read: say so, never "never" (a manifest source)
      kv(p, "continuity", `${fmt(h.total_wraps)} wraps · history unavailable`);
    } else {
      const last = h.last_wrap_at ? datePart(h.last_wrap_at) : "never";
      kv(p, "continuity", `${h.continuity_chars != null ? fmt(h.continuity_chars) + " chars" : "not yet created"} · ${fmt(h.total_wraps)} wraps · last ${last}`);
    }
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
    const W = 480, H = 170, PAD = 10;
    // anneal stopped forming links after wrap `off`: the Links channel keeps its historical points and draws no new ones
    // (a post-cut zero is not a measurement), and a store that never formed any has no such channel at all. The wraps list
    // is newest-first and capped, so a row's wrap number is counted back from the authoritative total.
    const off = view.graph && Number.isInteger(view.graph.formation_off_at_wrap) ? view.graph.formation_off_at_wrap : null;
    const totalWraps = memWrapTotal(view);
    // with the total unknown (health failed) the capped history cannot be numbered, so the cut cannot be placed on it
    const wrapNo = new Map(series.map((w, k) => [w, (totalWraps !== null ? totalWraps : series.length) - k]));
    const linkVal = (w) => num(w.associations_formed) + num(w.associations_strengthened);
    const CHANNELS = [
      { label: "Episodes", cls: "ch1", val: (w) => num(w.episodes_compressed) },
      { label: "Graduations", cls: "ch2", val: (w) => num(w.graduations_validated) },
    ];
    let noteTail = "";
    if (off === null) CHANNELS.push({ label: "Links", cls: "ch3", val: linkVal });
    else if (totalWraps === null) noteTail = " · links not drawn — wrap numbers unknown";
    else if (series.some((w) => wrapNo.get(w) <= off && linkVal(w) > 0)) {
      CHANNELS.push({ label: "links (formation off since wrap " + off + ")", cls: "ch3", val: linkVal,
        drawn: (w) => wrapNo.get(w) <= off });
    }

    // the instrument's read-side controls — the first real metal on the deck
    // (no-theater: both verbs act on the instrument NOW; write-side knobs land
    // with the Slice-2 governed verb seam). solo = CSS-only; timebase = redraw,
    // and the re-draw-in is honest (you changed the timebase, the scope re-sweeps).
    let solo = null;
    let span = 48;
    const wrap = el("div", "scope-wrap");
    wrap.dataset.solo = "";
    const bar = el("div", "scope-bar");
    if (series.length > 12) bar.appendChild(el("span", "lbl", "timebase"));
    const scopeBox = el("div", "scope-box");
    const legend = el("div", "scope-legend");
    const note = el("p", "note", "");
    // the timebase is drawn only when the history is longer than its shortest span: below that every button would draw the same trace
    for (const n of (series.length > 12 ? [12, 24, 48] : [])) {
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
      svg.setAttribute("preserveAspectRatio", "none"); // the card sets the height; see .scope-box in dashboard.css
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
        const live = wraps.map((w) => !ch.drawn || ch.drawn(w));   // false: a wrap this channel no longer measures
        const shown = vals.filter((_v, i) => live[i]);
        const actualMax = Math.max(0, ...shown);
        const gain = Math.max(1, actualMax);
        const pts = vals
          .map((v, i) => (live[i] ? `${X(i).toFixed(1)},${(H - PAD - (v / gain) * (H - 2 * PAD)).toFixed(1)}` : null))
          .filter((x) => x !== null)
          .join(" ");
        if (shown.length === 1) {
          // a one-point polyline draws nothing: a lone sample is a visible point
          const m = pts.split(",");
          const dot = document.createElementNS(NS, "circle");
          dot.setAttribute("cx", m[0]); dot.setAttribute("cy", m[1]); dot.setAttribute("r", "3");
          dot.setAttribute("class", `trace-dot ${ch.cls}`);
          svg.appendChild(dot);
        }
        for (const layer of (pts && shown.length > 1 ? ["trace-glow", "trace-core"] : [])) {
          const pl = document.createElementNS(NS, "polyline");
          pl.setAttribute("points", pts);
          pl.setAttribute("class", `${layer} ${ch.cls}`);
          pl.setAttribute("fill", "none");
          pl.setAttribute("pathLength", "1"); // normalizes the draw-in dash math
          svg.appendChild(pl);
        }
        const chip = el("button", `scope-ch ${ch.cls}`);
        chip.appendChild(el("span", "scope-ch-l", ch.label));
        chip.appendChild(el("span", "scope-ch-v", shown.length ? `${fmt(shown[shown.length - 1])} · max ${fmt(actualMax)}` : "no samples"));
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
      note.textContent = "oldest → newest" + noteTail;  // the consolidation / node / edge counts are the card's header line (composeMemory)
    }

    p.appendChild(wrap);
    if (bar.children.length) wrap.appendChild(bar);
    const plot = el("div", "scope-plot");
    plot.append(scopeBox, legend);
    wrap.appendChild(plot);
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
    const list = Array.isArray(view.crystal_index) ? view.crystal_index : [];
    const p = panel(entry);
    const err = tierErr(view, "crystal_index");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (list.length === 0) { p.appendChild(el("p", "empty", "none crystallized yet")); return p; }
    const grid = el("div", "ck-crys");
    for (const c of list) {
      const row = el("div", "row");
      row.appendChild(el("span", "lvl", `${c.level}x`));
      const txt = el("span", "ck-crys-t");
      // a name breaks after each underscore, so a long snake_case name wraps at a word and never mid-word
      const nm = el("span", "name");
      String(c.name == null ? "" : c.name).split("_").forEach((part, i) => { if (i) nm.appendChild(document.createTextNode("_")), nm.appendChild(document.createElement("wbr")); nm.appendChild(document.createTextNode(part)); });
      txt.appendChild(nm);
      if (c.one_clause) txt.appendChild(el("span", "clause", c.one_clause));
      // permanence / activation mode / tags are a muted line under the clause: touch has no hover, so a tooltip is not a place to keep them
      const meta = [c.permanence, c.activation_mode].filter(Boolean).join(" · ");
      const tags = Array.isArray(c.tags) && c.tags.length ? "#" + c.tags.slice(0, 6).join(" #") : "";
      const detail = [meta, tags].filter(Boolean).join(" · ");
      if (detail) txt.appendChild(el("span", "ck-crys-meta", detail));
      row.appendChild(txt);
      grid.appendChild(row);
    }
    p.appendChild(grid);
    return p;
  }

  // Open or close a row's full text, and keep its toggle's state words in step with it.
  function setExpanded(row, open) {
    row.classList.toggle("row-expanded", open);
    const toggle = row.querySelector(":scope > .clause-toggle");
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      // flip the accessible name with the state — else a screen reader announces "expand
      // full text … expanded" (contradictory) [codex + kimi + complement L3 converged].
      toggle.setAttribute("aria-label", open ? "collapse full text" : "expand full text");
      toggle.textContent = open ? "⌃" : "⌄";
    }
    return open;
  }
  // What the reader holds open while a panel is drawn again (the board rebuilds on a stream event, a refresh or a
  // returning tab; a typed panel repaints its body in place): the rows they expanded and how far each panel's body is
  // scrolled. Rows are known by their id (data-row-key, or data-keep-key for a typed panel's row), never by position,
  // inside the panel that holds them (data-ck-panel); a row that is gone simply stays closed. RE-RUNNABLE.
  const keptScope = (n) => {
    const ck = n.closest ? n.closest("[data-ck-panel], [data-panel-id]") : null;
    if (ck) return ck.dataset.ckPanel || ck.dataset.panelId || "";
    // only a panel the manifest gave no id falls through to its heading
    const pn = n.closest ? n.closest(".panel") : null;
    const h = pn ? pn.querySelector("h2") : null;
    return h ? "t:" + h.textContent : "";
  };
  const keptKey = (row) => row.dataset.rowKey || row.dataset.keepKey || "";
  const scrollersOf = (root) => {
    const out = root.matches && root.matches(".pbody, .modal-body") ? [root] : [];
    return out.concat(Array.from(root.querySelectorAll(".pbody, .modal-body")));
  };
  function captureKept(root) {
    const kept = { open: new Set(), scroll: new Map() };
    if (!root || !root.querySelectorAll) return kept;
    for (const row of root.querySelectorAll(".row.row-expanded")) {
      const k = keptKey(row);
      if (k) kept.open.add(JSON.stringify([keptScope(row), k]));
    }
    for (const sc of scrollersOf(root)) if (sc.scrollTop > 0) kept.scroll.set(keptScope(sc), sc.scrollTop);
    return kept;
  }
  // Only a row the clause measure marked can be opened again: a row whose text no longer overflows has no control
  // to close it with, so it stays closed. Run measureClauses first, on closed rows.
  function restoreKeptRows(root, kept) {
    if (!kept || !root || !root.querySelectorAll || !kept.open.size) return;
    for (const row of root.querySelectorAll(".row.has-more")) {
      const k = keptKey(row);
      if (k && kept.open.has(JSON.stringify([keptScope(row), k]))) setExpanded(row, true);
    }
  }
  function restoreKeptScroll(root, kept) {
    if (!kept || !root || !kept.scroll.size) return;
    for (const sc of scrollersOf(root)) { const t = kept.scroll.get(keptScope(sc)); if (t) sc.scrollTop = t; }
  }

  // The one order a drawn panel settles in: measure the clauses with every row closed, open again the rows the reader
  // held (the measure decides which can be), put the scroll back, then measure each bounded body's overflow cue last,
  // since the rows and the scroll both change what it reads. `root` is a board, a modal, or a single panel body.
  function settleKept(root, kept) {
    if (!root || !root.querySelectorAll) return;
    measureClauses(root);
    restoreKeptRows(root, kept);
    restoreKeptScroll(root, kept);
    const bodies = Array.from(root.querySelectorAll(".pbody.clamped"));
    if (root.matches && root.matches(".pbody.clamped")) bodies.push(root);
    for (const b of bodies) measureOverflow(b);
    // the fold and the cue change how tall a body is, and a body shorter than the scroll it was given clamps it: put it back once more
    restoreKeptScroll(root, kept);
  }

  // Append a clause + its per-item expand toggle (shared by spore + episode rows). The
  // FULL canonical text goes into the DOM via textContent (the wire no longer truncates),
  // so expand is instant and any edit reads the whole thing — no truncation, no data loss.
  // CSS clamps the clause to 3 lines; measureClauses reveals the toggle only when it
  // overflows; the toggle flips .row-expanded to show all of it inline.
  // Text that carries `code` spans is drawn through the same inline renderer the neocortex panel uses (mdInline), so a command or a path
  // reads as code and not as raw backticks; text without a backtick stays a plain text node (no other markdown is opened up).
  function codeSpansInto(parent, text) {
    if (String(text).indexOf("`") === -1) { parent.appendChild(document.createTextNode(text)); return; }
    mdInline(String(text), parent, 0);
  }
  function richEl(tag, cls, text) {
    const n = el(tag, cls);
    codeSpansInto(n, String(text != null ? text : ""));
    return n;
  }
  function appendClause(row, text) {
    // .clampable is what the CSS clamps + measureClauses targets — bare .clause (crystals,
    // wrap/edit labels) stays full so it never clamps without this expand affordance.
    const clause = el("span", "clause clampable");
    // a date (2026-10-15) never breaks at its hyphens: each one is a nowrap span, the text around it plain text
    const parts = String(text != null ? text : "").split(/(\b\d{4}-\d{2}-\d{2}\b)/);   // nullish text draws an empty clause, as el() does
    parts.forEach((part, i) => {
      if (i % 2) clause.appendChild(el("span", "nowrap", part));
      else if (part) codeSpansInto(clause, part);
    });
    row.appendChild(clause);
    const toggle = el("button", "clause-toggle", "⌄");  // caret only; the action lives in aria-label
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "expand full text");
    toggle.addEventListener("click", (ev) => {
      ev.stopPropagation();  // don't trip a row/modal handler
      const body = row.closest(".pbody, .modal-body");
      const place = body ? body.scrollTop : 0;   // the browser's scroll anchoring shifts it when a row above the reader grows
      const open = setExpanded(row, !row.classList.contains("row-expanded"));
      // expanding changes the panel's content height → refresh its overflow scent
      if (body && body.classList.contains("pbody")) { measureOverflow(body); if (body.scrollTop !== place) body.scrollTop = place; }
    });
    row.appendChild(toggle);
    return clause;
  }

  // One spore row, shared by the three projections (Open Loops / Tray / Keep). The
  // leading chip is the panel's salient axis: Tray badges the `disposition` (the
  // operator-I/O class — seed/handoff/agenda), Open Loops + Keep badge the `tier`. `verbs`
  // rides only on a Class-B panel (Open Loops in 3a; Tray + Keep render read-only until 3b
  // turns on the governed dump/sort/park verbs — edit_class "" → isVerbPanel false → none).
  // The ONE place a verb-bearing row takes its verbs: every row kind (spore, episode, pending cancel) passes through
  // here. On the KE page the verbs are clipped until the row is hovered or has focus, so the row itself must be
  // focusable (tabIndex 0) or a keyboard user never reaches them.
  // On the KE page at most VERBS_VISIBLE verbs show; the rest sit behind a "···" menu (a popover: Enter/Space opens it,
  // Esc closes it and returns focus, arrows and Tab move). `prefer` names the labels that stay visible first (the Tray:
  // edit, schedule, dismiss); otherwise the first three the kernel offered, in its order. Every verb keeps its own
  // button, handlers and data attributes: the menu only moves the node.
  const VERBS_VISIBLE = 3;
  // Where the row is too narrow for the meta line (id, tag, date, age) beside the verbs, a container query on the row
  // (CSS, decided at render and on every resize) hides the inline verbs and shows a copy of each in the "···" menu.
  function overflowVerbs(group, prefer, shown) {
    const btns = Array.prototype.slice.call(group.querySelectorAll(":scope > .verb-btn"));
    const visible = shown || VERBS_VISIBLE;
    if (btns.length <= visible) return;
    let keep = [];
    for (const label of prefer || []) {
      const b = btns.find((x) => x.textContent === label);
      if (b && keep.length < visible) keep.push(b);
    }
    for (const b of btns) if (keep.length < visible && keep.indexOf(b) < 0) keep.push(b);
    const rest = btns.filter((b) => keep.indexOf(b) < 0);
    const more = el("button", "verb-btn verb-more", "\u00b7\u00b7\u00b7");
    more.type = "button";
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    more.setAttribute("aria-label", "more: " + rest.map((b) => b.textContent).join(", "));
    const menu = el("span", "verb-menu");
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    // a copy of each inline verb, shown only where the row's container is narrow (CSS): it presses the real button
    const dups = [];
    for (const b of keep) {
      const d = el("button", "verb-btn verb-dup", b.textContent);
      d.type = "button"; d.setAttribute("role", "menuitem");
      d.addEventListener("click", () => b.click());
      dups.push([b, d]);
      menu.appendChild(d);
    }
    // a copy states what its source states now (disabled, title, aria-*), re-read each time the menu opens
    const mirror = () => {
      for (const [src, d] of dups) {
        d.disabled = src.disabled;
        if (src.title) d.title = src.title; else d.removeAttribute("title");
        for (const a of Array.from(d.attributes)) if (a.name.indexOf("aria-") === 0) d.removeAttribute(a.name);
        for (const a of Array.from(src.attributes)) if (a.name.indexOf("aria-") === 0) d.setAttribute(a.name, a.value);
      }
    };
    for (const b of rest) { b.setAttribute("role", "menuitem"); menu.appendChild(b); }
    const items = () => Array.prototype.slice.call(menu.querySelectorAll(".verb-btn")).filter((b) => !b.disabled && b.offsetParent !== null);
    // an outside press closes the menu (a button takes no focus on a mouse press in Safari/Firefox, so a focusout on a
    // press carries a null relatedTarget, or the focusable ancestor the press landed on, and says nothing about where the press went)
    const outside = (ev) => { if (!group.contains(ev.target)) close(false); };
    const close = (refocus) => {
      menu.hidden = true; menu.classList.remove("up");
      more.setAttribute("aria-expanded", "false");
      document.removeEventListener("pointerdown", outside, true);
      if (refocus) more.focus();
    };
    const open = () => {
      mirror();
      menu.hidden = false;
      document.addEventListener("pointerdown", outside, true);
      more.setAttribute("aria-expanded", "true");
      const host = group.closest(".pbody, .modal-body, .xbody");
      if (host) {
        const room = host.getBoundingClientRect().bottom - more.getBoundingClientRect().bottom;
        menu.classList.toggle("up", room < menu.offsetHeight + 8);
      }
      const first = items()[0];
      if (first) first.focus(); else close(false);
    };
    more.addEventListener("click", (ev) => { ev.stopPropagation(); if (menu.hidden) open(); else close(true); });
    // keep focus on the item through a press: Safari would move it to the row, which strands the keyboard outside the group
    menu.addEventListener("mousedown", (ev) => ev.preventDefault());
    menu.addEventListener("click", () => {
      close(false);
      // the verb's handler ran first; if focus is still on the item that just hid (or on body), return it to the trigger,
      // never taking it from an element the handler focused elsewhere
      const a = document.activeElement;
      if (!a || a === document.body || menu.contains(a)) more.focus();
    });
    group.addEventListener("keydown", (ev) => {
      if (menu.hidden) return;
      mirror();
      if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(true); return; }
      const list = items(); const i = list.indexOf(document.activeElement);
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        const n = list.length; if (!n) return;
        list[(i + (ev.key === "ArrowDown" ? 1 : -1) + n) % n].focus();
      } else if (ev.key === "Tab") close(false);
    });
    group.addEventListener("focusout", (ev) => {
      // only a move to a real element outside the group, other than an ancestor of it, closes here; a press arrives with a
      // null relatedTarget or with the focusable ANCESTOR it landed on (Safari: the row, tabIndex 0), and the pointerdown
      // listener above decides those. The mousedown handler on the menu keeps the item focused, this is its backstop
      const rt = ev.relatedTarget;
      if (!menu.hidden && rt && !group.contains(rt) && !rt.contains(group)) close(false);
    });
    group.appendChild(more);
    group.appendChild(menu);
  }
  function appendRowVerbs(row, group, prefer, visible) {
    if (document.body.classList.contains("ke")) { row.tabIndex = 0; overflowVerbs(group, prefer, visible); }
    row.appendChild(group);
    return row;
  }
  function sporeRow(s, verbs, badgeField, panelKind) {
    const row = el("div", "row");
    if (s.row_id) row.dataset.rowKey = s.row_id;   // where a transport notice lands after the board is rebuilt
    // the spore id is the common reference handle (how flow tracks + you name it,
    // e.g. "compost spore-049") — show it leading so the row is addressable; the TUI
    // shows the same id trailing (tui.render_panel_lines).
    if (s.id) row.appendChild(el("span", "sid", s.id));
    // Tray badges the disposition; Open Loops the tier; Keep is mixed — a note badges
    // "note" (reference), a pinned-dormant loop badges its tier ("parked").
    let badge;
    if (badgeField === "disposition") badge = s.disposition;
    else if (badgeField === "keep") badge = s.disposition === "note" ? "note" : s.tier;
    else badge = s.tier;
    if (badge) row.appendChild(tagEl("tier", badge));
    appendClause(row, s.text);  // FULL text + per-item expand (no truncation, no edit data-loss)
    if (s.held) {
      row.dataset.held = "1";
      const sf = tagEl("muted", "surfaces " + surfaceLabel(s.next));
      sf.setAttribute("data-ck-tag-state", "held");
      row.appendChild(sf);
    } else if (s.next) {
      // a late row says how late ("overdue 4 d") and keeps its date ("due 10-17"); a row due today says so
      const late = daysLate(s.next, s.today);
      if (late > 0) {
        row.dataset.band = "overdue";
        const od = tagEl("muted", "overdue " + late + " d");
        od.setAttribute("data-ck-tag-state", "overdue");
        row.appendChild(od);
        row.appendChild(tagEl("muted", "due " + shortDate(s.next)));
      } else {
        row.appendChild(tagEl("muted", late === 0 ? "due today" : "due " + shortDate(s.next)));
      }
    }
    if (panelKind !== "tray" && panelKind !== "keep" && s.tier) row.dataset.heat = String(s.tier);   // Open loops: a stripe per heat
    if (s.age_shown && s.age) row.appendChild(el("span", "muted", s.age));
    // Build's Open loops draw "open N d" and the repo chip under the title (cockpit.css hides both elsewhere)
    // the integer the kernel sends (the spore's own created date), never the badge words parsed back
    if (Number.isInteger(s.age_days) && s.age_days >= 0) row.appendChild(el("span", "k7-opend", "open " + s.age_days + " d"));
    if (s.domain) { const rc = el("span", "k7-repo", String(s.domain)); rc.dataset.repoFacet = String(s.domain); row.appendChild(rc); }
    if (verbs && s.id) appendRowVerbs(row, buildSporeVerbs(s, panelKind), panelKind === "tray" ? ["Resolve", "Snooze"] : null, panelKind === "tray" ? 2 : 0);
    return row;
  }

  // The filter bar the spore panels share (Open Loops / Tray / Keep): CLIENT-side over the
  // loaded list, text (case-insensitive substring) OR spore-id. The query persists per panel
  // kind in `sporeQueries`, so a verb that rebuilds the board leaves you in your filter.
  // FILTER-EXTRACT-START (a node test lifts this block)
  // opts (optional): controls(bar, redraw) adds in-panel controls to the bar; pick(list) returns the
  // rows to filter (a view's own order), re-read on every redraw; extra() returns rows listed apart that are
  // filtered with the rest; empty is the message shown when nothing is listed and there is no query;
  // unlisted() returns rows that are not listed now (held back) but would be searched, so an empty result
  // can say how many of them match the query and offer to show them (hintShow(n, show) builds that offer).
  function mountSporeFilter(p, all, noun, kind, drawRows, opts) {
    const bar = el("div", "ep-search");
    const input = el("input", "ep-search-input");
    input.type = "search";
    input.placeholder = "filter " + noun + " by text or spore-id…";
    input.setAttribute("aria-label", "filter " + kind + " by text or spore id");
    const status = el("span", "ep-search-status", "");
    bar.append(input, status);
    p.appendChild(bar);
    const results = el("div", "sp-results");
    p.appendChild(results);
    const render = (raw) => {
      const q = raw.trim().toLowerCase();
      const picked = opts && opts.pick ? opts.pick(all) : all;
      const base = opts && opts.extra ? picked.concat(opts.extra()) : picked;   // rows listed apart, filtered with the rest
      const hit = (s) => String(s.text == null ? "" : s.text).toLowerCase().includes(q) ||
                         String(s.id || "").toLowerCase().includes(q);
      const rows = q ? base.filter(hit) : base;
      results.replaceChildren();
      if (rows.length === 0) {
        const note = el("p", "empty", q || !(opts && opts.empty) ? "no " + noun + " match “" + raw.trim() + "”" : opts.empty);
        const unlisted = q && opts && opts.unlisted ? opts.unlisted().filter(hit).length : 0;
        if (unlisted && opts.hintShow) opts.hintShow(note, unlisted);
        results.appendChild(note);
      } else drawRows(rows, results);
      status.textContent = q ? (rows.length + (rows.length === 1 ? " match" : " matches") + " · ⌫ to clear") : "";
      const b = results.closest(".pbody"); if (b) measureOverflow(b);
      measureClauses(results);
    };
    input.addEventListener("input", () => { sporeQueries[kind] = input.value; render(input.value); });
    if (opts && opts.controls) opts.controls(bar, () => render(input.value));
    if (sporeQueries[kind]) input.value = sporeQueries[kind];
    render(input.value);
  }

  // FILTER-EXTRACT-END

  // The three spore projections share one renderer — same read/verb contract, different
  // source list + badge axis + empty copy. All three read their fault from the single
  // `open_spores` error key (the dashboard does ONE spore read that fills all three).
  function renderSporeProjection(entry, view, list, emptyMsg, badgeField) {
    const p = panel(entry);
    // All three projections (Open Loops / Tray / Keep) come from ONE dashboard spore read,
    // whose fault is recorded under the single "open_spores" key — so every projection
    // reads its error from there BY DESIGN (not a copy-paste slip). If a future split gives
    // Tray/Keep their own source, give each its own error key here. [complement L3 LOW]
    const err = tierErr(view, "open_spores");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    // the Tray's rows held until their surface date: listed only when the viewer asks, under the due rows
    const held = entry.kind === "tray" && Array.isArray(view.tray_held) ? view.tray_held : [];
    if ((!list || list.length === 0) && held.length === 0) { p.appendChild(el("p", "empty", emptyMsg)); return p; }
    const verbs = isVerbPanel(entry);
    // the Tray's views: the kernel names each ordering and sends the row ids in it; this only picks one
    const views = entry.kind === "tray" && Array.isArray(view.tray_views) && view.tray_view_rows ? view.tray_views : [];
    const viewOf = () => (views.some((v) => v.id === trayView) ? trayView : "");
    const heldLine = held.length && view.tray_held_line && typeof view.tray_held_line === "object" ? view.tray_held_line : null;
    const opts = views.length || heldLine ? {
      empty: emptyMsg,
      extra: () => (heldLine && trayHeldShown ? held : []),
      unlisted: () => (heldLine && !trayHeldShown ? held : []),
      hintShow: (note, n) => {
        note.appendChild(document.createTextNode(" — " + n + (n === 1 ? " held row matches — " : " held rows match — ")));
        const go = el("button", "held-toggle");
        go.type = "button";
        go.dataset.heldShowMatches = "1";
        go.textContent = "show";
        go.addEventListener("click", () => { const t = p.querySelector("[data-held-toggle]"); if (t) t.click(); });
        note.appendChild(go);
      },
      controls: (bar, redraw) => {
        if (heldLine) {
          const line = el("div", "ext-note held-line");
          // the earliest surface date among the held rows: "1 held back until Dec 3"
          const nexts = held.map((h) => String(h.next || "")).filter((d) => /^\d{4}-\d{2}-\d{2}/.test(d)).sort();
          line.appendChild(el("span", null, nexts.length ? heldLine.count + " held back until " + surfaceLabel(nexts[0]) + " "
            : heldLine.count + " held back — " + heldLine.reason + " "));
          const tog = el("button", "held-toggle");
          tog.type = "button";
          tog.dataset.heldToggle = "1";
          const label = () => { tog.textContent = trayHeldShown ? "hide" : "show"; tog.setAttribute("aria-expanded", trayHeldShown ? "true" : "false"); };
          label();
          // one state, every mounted Tray (the board's and the focus modal's) redraws on a change
          const mine = { tog, sync: () => { label(); redraw(); } };
          heldMounts.add(mine);
          // drop the mounts a re-render replaced; after this task, so a Tray built but not yet attached stays
          setTimeout(() => { for (const m of [...heldMounts]) if (!m.tog.isConnected) heldMounts.delete(m); }, 0);
          tog.addEventListener("click", () => {
            trayHeldShown = !trayHeldShown; saveHeldShown();
            heldMounts.add(mine);   // a Tray attached after the prune ran still redraws itself
            for (const m of [...heldMounts]) { if (m.tog.isConnected) m.sync(); else heldMounts.delete(m); }
          });
          line.appendChild(tog);
          p.appendChild(line);   // at the foot of the list, as the mock draws it
        }
        if (!views.length) return;
        const sel = el("select", "tray-dump-type");
        sel.setAttribute("aria-label", "Tray order");
        sel.appendChild(Object.assign(el("option", null, "Tray order"), { value: "" }));
        for (const v of views) sel.appendChild(Object.assign(el("option", null, v.title), { value: v.id }));
        sel.value = viewOf();
        sel.addEventListener("change", () => { trayView = sel.value; redraw(); });
        bar.appendChild(sel);
      },
      pick: (all) => {
        const ids = views.length && viewOf() ? view.tray_view_rows[viewOf()] : null;
        if (!Array.isArray(ids)) return all;
        const byId = new Map(all.map((s) => [s.id, Object.assign({}, s, { age_shown: true })]));
        // a row the view did not name still shows, after the named ones
        const named = new Set(ids);
        return ids.filter((id) => byId.has(id)).map((id) => byId.get(id))
          .concat(all.filter((s) => !named.has(s.id)).map((s) => byId.get(s.id)));
      },
    } : undefined;
    mountSporeFilter(p, list || [], "items", entry.kind, (rows, results) => {
      let lastGroup = null;
      let heldHead = false;
      let box = results;
      for (const s of rows) {
        if (s.held && !heldHead) { heldHead = true; box = results; results.appendChild(el("div", "grp-head", "Held until their surface date")); }
        // a manifest source brings the kernel's own bands (Today / Overdue / Also pending), in its order;
        // a chosen view is ungrouped
        const g = s.group || null;
        if (s.group_title && g !== lastGroup && !viewOf() && !s.held) {
          // each band is a column of its own where the page is wide (Morning's Tray: Overdue | Today); `display: contents` elsewhere
          box = entry.kind === "tray" ? results.appendChild(el("div", "grp-col")) : results;
          box.appendChild(el("div", "grp-head", s.group_title));
        }
        lastGroup = g;
        box.appendChild(sporeRow(s, verbs, badgeField, entry.kind));
      }
    }, opts);
    return p;
  }

  function renderSpores(entry, view) {
    const p = panel(entry);
    const err = tierErr(view, "open_spores");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    const all = view.open_spores || [];
    const verbs = isVerbPanel(entry);
    // CLIENT-side filter over the loaded loops (they all ride the wire — no server round-trip,
    // unlike the episode keyword search). Matches the spore TEXT (case-insensitive substring)
    // OR the spore-id ("spore-103" / "103"), so you can jump straight to a loop by handle or
    // content [2026-06-19]. Reuses the .ep-search sticky bar for visual parity.
    const bar = el("div", "ep-search");
    const input = el("input", "ep-search-input");
    input.type = "search";
    input.placeholder = "filter loops by text or spore-id…";
    input.setAttribute("aria-label", "filter open loops by text or spore id");
    const status = el("span", "ep-search-status", "");
    bar.append(input, status);
    p.appendChild(bar);
    const results = el("div", "sp-results");
    p.appendChild(results);
    const render = (raw) => {
      const q = raw.trim().toLowerCase();
      const rows = q
        ? all.filter((s) => String(s.text == null ? "" : s.text).toLowerCase().includes(q) ||
                            String(s.id || "").toLowerCase().includes(q))
        : all;
      results.replaceChildren();
      if (rows.length === 0) {
        results.appendChild(el("p", "empty",
          q ? "no loops match “" + raw.trim() + "”" : "Nothing yet. What stays open after Sage sorts the Tray is listed here."));
      } else {
        for (const s of rows) results.appendChild(sporeRow(s, verbs, "tier", entry.kind));
      }
      status.textContent = q ? (rows.length + (rows.length === 1 ? " match" : " matches") + " · ⌫ to clear") : "";
      // result set changed → recompute the panel scent + the per-item expand toggles. .pbody
      // is null on the initial render (finalizePanels reparents later, then measures) — no-op
      // then, live on every input-driven re-render.
      const b = results.closest(".pbody"); if (b) measureOverflow(b);
      measureClauses(results);
    };
    input.addEventListener("input", () => { openLoopsQuery = input.value; render(input.value); });
    if (openLoopsQuery) { input.value = openLoopsQuery; render(openLoopsQuery); }
    else render("");
    return p;
  }

  // Tray — the operator session-I/O inbox (seed/handoff/agenda dumps awaiting AI triage);
  // badged by disposition. Slice 3b: a freeform DUMP input on top (the human dumps; the
  // AI sorts), then the pending list with per-row Tray verbs.
  // Mount a capture box right AFTER the panel header (.phead) — so the panel TITLE stays
  // on top like every other panel, AND so the expand-to-modal picks it up (modalContent
  // transplants phead.nextSibling onward; a box inserted BEFORE phead is dropped from the
  // modal). One placement fixes both the grid consistency and the modal presence.
  function mountCaptureBox(p, box) {
    const phead = p.querySelector(".phead");
    if (phead) p.insertBefore(box, phead.nextSibling);
    else p.insertBefore(box, p.firstChild);
  }

  function renderTray(entry, view) {
    const p = renderSporeProjection(entry, view, view.tray,
      "Nothing yet. Drop a thought in the box above and Sage sorts it.", "disposition");
    // The dump box is the primary capture affordance — mounted under the title when
    // writable. A read-only port (mesh) shows the Tray but offers no dump (NO-THEATER).
    if (isVerbPanel(entry)) mountCaptureBox(p, buildTrayDump(view.entity_name || "the assistant"));
    return p;
  }

  // Keep — durable reference: authored notes + pinned-dormant loops, lifted out of Open
  // Loops and exempt from the dormancy→compost prompt. Slice 3b: an add-note box on top
  // (when writable), then the list badged per-item (note → "note", parked loop → "parked").
  function renderKeep(entry, view) {
    const p = renderSporeProjection(entry, view, view.keep,
      "Nothing yet. Add a note above to keep something handy.", "keep");
    if (isVerbPanel(entry)) mountCaptureBox(p, buildKeepNote());
    return p;
  }

  // One episode row — the SINGLE markup for both the recent list and keyword-search
  // hits (spore-107), so they render identically. `verbs` carries the panel's verb
  // affordance (the tombstone), which works on search rows too: `commit` is the
  // module-level closure, available to dynamically-added rows.
  function episodeRow(e, verbs, showAgent) {
    const row = el("div", "row" + (e.emphasis === "dim" ? " ext-dim" : ""));
    if (e.row_id) row.dataset.rowKey = e.row_id;
    else if (e.id) row.dataset.keepKey = "episode:" + e.id;   // a keyword hit has no row id: the same key the recent list's row carries
    if (showAgent && e.agent) row.appendChild(tagEl("tier", e.agent));
    if (e.type) row.appendChild(tagEl("etype", e.type));
    appendClause(row, e.content);  // FULL content + per-item expand (no truncation)
    if (Array.isArray(e.tags) && e.tags.length) {
      row.appendChild(tagEl("tags", "#" + e.tags.slice(0, 5).join(" #")));
    }
    row.appendChild(el("span", "muted", datePart(e.timestamp)));
    // a row that lists its actions offers only those; a row without the field (the legacy path) keeps the panel's
    const offers = !Array.isArray(e.actions) || e.actions.some((a) => a && a.verb === "episode_tombstone");
    if (verbs && e.id && offers) appendRowVerbs(row, buildEpisodeVerbs(e));
    return row;
  }

  function renderEpisodes(entry, view, opts) {
    const list = view.episodes || [];
    const p = panel(entry);
    const err = tierErr(view, "episodes");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    const verbs = isVerbPanel(entry);
    const modal = !!(opts && opts.modal);
    const myGen = modalGen;

    // spore-107: inline keyword search over the episodic store, READ-ONLY. Enter runs
    // a /recall.json query and swaps the recent list for matches; clearing the box
    // restores the recent list. (Slice-2 evolution = search-into-modal, shape (c).)
    const bar = el("div", "ep-search");
    const input = el("input", "ep-search-input");
    input.type = "search";
    input.placeholder = "search episodes by keyword…";
    input.setAttribute("aria-label", "search episodes by keyword");
    const status = el("span", "ep-search-status", "");
    bar.append(input, status);
    // finalizePanels reparents this into .pbody (the scroll container) — the sticky pin
    // depends on that; if it ever lands outside .pbody, the sticky silently no-ops.
    p.appendChild(bar);

    const results = el("div", "ep-results");
    p.appendChild(results);

    // Recompute the panel's overflow scent + keyboard affordance after the result set
    // changes (search shrinks/grows it) — the body is .pbody once finalizePanels has run
    // (a no-op on the initial render, where finalizePanels measures instead).
    const reMeasure = () => {
      // rows that arrive after the board's own measure (a search answer) get their expand controls here
      measureClauses(results);
      const b = results.closest(".pbody"); if (b) measureOverflow(b);
      // In the modal, hand back what the reader held across a refresh-rebuild ONCE the (async) results have landed:
      // the rows they had open and the scroll, which buildModal's synchronous restore could not reach because the list
      // arrives after the /recall.json round-trip. One-shot, and only for the build this panel belongs to; the reader's
      // own search (capture) drops it, so a fresh search still starts at the top.
      if (modal && myGen === modalGen && modalPending && results.isConnected) {
        const sc = results.closest(".modal-body");
        if (sc) { const k = modalPending; modalPending = null; modalAwaiting = false; settleKept(sc, k); }
      }
    };
    const agents = [];
    for (const e of list) if (e.agent && !agents.includes(e.agent)) agents.push(e.agent);
    const multi = agents.length > 1;
    // the choice outlives a refresh in which that agent is absent: it shows as All, and applies again on return
    const agentNow = () => (agents.includes(episodeAgent) ? episodeAgent : "");
    let agentSel = null;
    const syncAgent = () => {
      if (!agentSel) return;
      const searching = !!input.value.trim();
      agentSel.disabled = searching;
      agentSel.title = searching ? "the agent filter applies to the recent list, not to keyword search results" : "";
    };
    const renderRows = (rows) => {
      results.replaceChildren();
      for (const e of rows) results.appendChild(episodeRow(e, verbs, multi));
    };
    const showRecent = () => {
      status.textContent = "";
      const shown = agentNow() ? list.filter((e) => e.agent === agentNow()) : list;
      if (list.length === 0) results.replaceChildren(el("p", "empty", "Nothing yet. Episodes appear here as sessions write them."));
      else renderRows(shown);
      reMeasure();
    };
    if (multi) {
      const sel = el("select", "tray-dump-type");
      sel.setAttribute("aria-label", "filter episodes by agent");
      sel.appendChild(Object.assign(el("option", null, "All agents"), { value: "" }));
      for (const a of agents) sel.appendChild(Object.assign(el("option", null, a), { value: a }));
      sel.value = agentNow();
      // a keyword search shows the store's own matches; the filter applies to the recent list
      sel.addEventListener("change", () => { episodeAgent = sel.value; if (!input.value.trim()) showRecent(); });
      agentSel = sel;
      bar.appendChild(sel);
    }

    // Latest-query-wins: a slow earlier response must not clobber a newer one.
    let seq = 0;
    const search = async (raw) => {
      const q = raw.trim();
      if (!q) { showRecent(); return; }
      const mine = ++seq;
      status.textContent = "searching…";
      try {
        // Route through the injected recall transport when present (it carries the off-box token —
        // spore-220 gates /recall.json off-box); fall back to a direct token-free fetch otherwise.
        let data;
        if (recall) {
          data = await recall(q);
        } else {
          const res = await fetch("/recall.json?keyword=" + encodeURIComponent(q), { cache: "no-store" });
          if (!res.ok) throw new Error("HTTP " + res.status);
          data = await res.json();
        }
        if (mine !== seq) return;  // superseded by a newer query
        const rerr = data.errors && (data.errors.episodes || data.errors.server);
        if (rerr) {
          results.replaceChildren(el("p", "err", "search unavailable — " + rerr));
          status.textContent = "";
        } else {
          const eps = data.episodes || [];
          status.textContent = eps.length + (eps.length === 1 ? " match" : " matches") + " · ⌫ to clear";
          if (eps.length === 0) results.replaceChildren(el("p", "empty", "no matches for “" + q + "”"));
          else renderRows(eps);
        }
      } catch (e2) {
        if (mine !== seq) return;
        status.textContent = "";
        results.replaceChildren(el("p", "err", "search failed — " + (e2 && e2.message ? e2.message : e2)));
      }
      reMeasure();  // result set changed → recompute the overflow scent + keyboard affordance
    };

    // In the focus modal, the query PERSISTS across a refresh-rebuild (a verb fired
    // inside the modal re-renders it): every keystroke is stashed in modalEpisodeQuery
    // and, on rebuild, the box is restored + the search re-run — so tombstoning a hit
    // keeps you IN your search (the grid panel's reset-to-recent is superseded). A
    // no-op outside the modal (the grid panel keeps its documented reset behavior).
    const capture = () => {
      if (!modal) return;
      modalEpisodeQuery = input.value.trim();
      modalPending = null; modalAwaiting = false;  // user is driving the search → don't fight their scroll
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); capture(); search(input.value); }
    });
    // Emptying the box (backspace or the native search-clear ×) restores recent at once.
    // Any edit invalidates an in-flight search (seq++), so a late response never renders
    // under a changed box (codex L3); emptying also restores the recent list.
    input.addEventListener("input", () => { seq++; capture(); syncAgent(); if (!input.value.trim()) showRecent(); });

    if (modal && modalEpisodeQuery) { input.value = modalEpisodeQuery; syncAgent(); modalAwaiting = true; search(modalEpisodeQuery); }
    else showRecent();
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
        (num(w.associations_formed) ? `+${fmt(num(w.associations_formed))} links · ` : "") +
        `${fmt(num(w.continuity_chars))} chars`));
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

  // ===================================================================== md ===
  // A safe, zero-dependency markdown → DOM renderer for the prose panels
  // (section / config). EVERY text value reaches the DOM via el()/textContent/
  // createTextNode, so store content can NEVER become markup — the same contract
  // as el(). The ONLY data-derived attribute is a link href, set only after a
  // scheme allowlist (mdSafeHref). Raw HTML in the source is NOT interpreted; it
  // paints as literal text. Markdown is DISPLAY-ONLY: the edit path still reads/
  // writes the raw text (display-renders / edit-raw). Within a paragraph each
  // source newline is a hard break (<br>) — the content is line-structured memory
  // prose, so the author's line breaks survive (matching the <pre> origin) with
  // markdown affordances layered on. Best-effort markdown: any syntax it doesn't
  // model degrades to visible literal text, never to an error or unsafe output.
  // [MD-EXTRACT-START — the node adversarial harness slices between these markers.]
  const MD_MAX_DEPTH = 6;

  // Scheme allowlist for link hrefs. Probes the scheme the way a browser does —
  // stripping embedded C0 controls/whitespace first, which browsers ignore when
  // resolving (so `java\nscript:` can't slip past). Returns a safe href or null.
  function mdSafeHref(raw) {
    const u = String(raw == null ? "" : raw).trim();
    if (!u) return null;
    const probe = u.replace(/[\u0000-\u0020\u00a0]+/g, "").toLowerCase();
    const m = /^([a-z][a-z0-9+.-]*):/.exec(probe);
    if (m) return (m[1] === "http" || m[1] === "https" || m[1] === "mailto") ? u : null;
    if (probe.indexOf("//") === 0) return null;  // protocol-relative → offsite → refuse
    return u;  // relative path / #anchor / query — no scheme, no offsite host
  }

  // [text](url): match a link starting at s[i] === "[". No nested brackets/parens
  // (first `]`, then a required `(`, then the first `)`). Returns {text,url,end} | null.
  function mdMatchLink(s, i) {
    const close = s.indexOf("]", i + 1);
    if (close < 0 || s[close + 1] !== "(") return null;
    const paren = s.indexOf(")", close + 2);
    if (paren < 0) return null;
    return { text: s.slice(i + 1, close), url: s.slice(close + 2, paren), end: paren + 1 };
  }

  // Inline tokenizer: appends inline nodes for `text` onto `parent`. Walks strictly
  // left-to-right with bounded lookahead (indexOf for closers) — no backtracking,
  // and i advances every iteration (no infinite loop). Unmatched delimiters degrade
  // to literal text. Recurses (bold/italic/link text) only to MD_MAX_DEPTH.
  // Find the next `_`/`__` that closes at a word boundary (the char after it is not
  // alphanumeric). With the intra-word OPENING guard below, this keeps snake_case
  // identifiers (cross_substrate_review_codex_nonreplaceable, master_plan.md) fully
  // literal — the embedder's content is dense with them, so a naive `_` italic shreds it.
  function mdCloseUnderscore(s, from, delim) {
    let k = s.indexOf(delim, from);
    while (k >= 0) {
      const after = s[k + delim.length];
      if (after === undefined || !/[A-Za-z0-9]/.test(after)) return k;
      k = s.indexOf(delim, k + 1);
    }
    return -1;
  }

  function mdInline(text, parent, depth) {
    depth = depth || 0;
    const s = String(text == null ? "" : text);
    const n = s.length;
    let run = "";
    const flush = () => { if (run) { parent.appendChild(document.createTextNode(run)); run = ""; } };
    // O(n) guard for the underscore closer scan: once NO valid closer exists from some
    // position, none exists from any LATER position (the candidate set only shrinks), so
    // cache the first dead `from` per delimiter. Turns a pathological ` _a _a _a…` (every
    // opener space-flanked, no closer) from O(n^2) into O(n). [codex L3 HIGH]
    const usDead = { "_": n + 1, "__": n + 1 };
    const usClose = (from, delim) => {
      if (from >= usDead[delim]) return -1;
      const ce = mdCloseUnderscore(s, from, delim);
      if (ce < 0) usDead[delim] = from;
      return ce;
    };
    let i = 0;
    while (i < n) {
      const c = s[i];
      // inline code `...` — literal inner, highest precedence
      if (c === "`") {
        const ce = s.indexOf("`", i + 1);
        if (ce > i) { flush(); parent.appendChild(el("code", "md-code-i", s.slice(i + 1, ce))); i = ce + 1; continue; }
      }
      // link [text](url)
      if (c === "[") {
        const link = mdMatchLink(s, i);
        if (link) {
          flush();
          const href = mdSafeHref(link.url);
          if (href != null && depth < MD_MAX_DEPTH) {
            const a = el("a", "md-link");
            a.setAttribute("href", href);
            a.setAttribute("rel", "noopener noreferrer nofollow");
            a.setAttribute("target", "_blank");
            mdInline(link.text, a, depth + 1);
            parent.appendChild(a);
          } else {
            mdInline(link.text, parent, depth + 1);  // unsafe/over-deep → keep text, drop link
          }
          i = link.end; continue;
        }
      }
      // Underscore emphasis is word-BOUNDARY flanked (CommonMark): a `_` whose LEFT
      // flank is alphanumeric does not OPEN emphasis, so snake_case stays literal.
      // Asterisks keep intra-word emphasis (and aren't a snake_case hazard).
      const usBlocked = c === "_" && i > 0 && /[A-Za-z0-9]/.test(s[i - 1]);
      // bold **...** / __...__  (checked before single-char emphasis)
      if ((c === "*" || c === "_") && s[i + 1] === c && !usBlocked) {
        const ce = c === "_" ? usClose(i + 2, "__") : s.indexOf("**", i + 2);
        // NON-EMPTY content: bold opener is 2 chars (content starts at i+2), so require
        // ce > i+2 — `****` inline must not emit <strong></strong> (parity w/ the italic
        // ce > i+1 rule). [complement L3 LOW-1]
        if (ce > i + 2 && depth < MD_MAX_DEPTH) {
          flush(); const b = el("strong", "md-b"); mdInline(s.slice(i + 2, ce), b, depth + 1);
          parent.appendChild(b); i = ce + 2; continue;
        }
      }
      // italic *...* / _..._  — require NON-EMPTY content (ce > i + 1): an unclosed `**`
      // must not let the second `*` close a zero-width italic against the first (`**a`
      // → literal, not <em></em>a). [L1 MED]
      if ((c === "*" || c === "_") && !usBlocked) {
        const ce = c === "_" ? usClose(i + 1, "_") : s.indexOf(c, i + 1);
        if (ce > i + 1 && depth < MD_MAX_DEPTH) {
          flush(); const em = el("em", "md-i"); mdInline(s.slice(i + 1, ce), em, depth + 1);
          parent.appendChild(em); i = ce + 1; continue;
        }
      }
      run += c; i++;
    }
    flush();
  }

  // A list item line → {indent, ordered, content} | null. Tabs count as 4 cols.
  function mdListItem(line) {
    const m = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/.exec(line);
    if (!m) return null;
    return { indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), content: m[3] };
  }

  // Does a line START a non-paragraph block? (Paragraph collection stops at these.)
  function mdIsBlockStart(line) {
    return /^[ \t]*(```+|~~~+)/.test(line) || /^#{1,6}[ \t]+/.test(line) ||
      /^[ \t]*>/.test(line) || /^[ \t]*([-*_])[ \t]*(\1[ \t]*){2,}$/.test(line) ||
      !!mdListItem(line);
  }

  // Render `lns` into `parent`, each source line a hard <br> break, inline-parsed.
  function mdLinesInto(parent, lns) {
    for (let k = 0; k < lns.length; k++) {
      if (k) parent.appendChild(el("br"));
      mdInline(lns[k], parent, 0);
    }
  }

  // Build a (possibly nested) list from lines[start..]; returns the index after it.
  // Nesting by indent via a stack of {indent, ordered, listEl, lastLi}; depth-capped so a
  // pathological indent ramp can't push past MD_MAX_DEPTH levels (it flattens).
  function mdBuildList(lines, start, frag) {
    let i = start;
    const N = lines.length;
    const stack = [];
    while (i < N) {
      const item = mdListItem(lines[i]);
      if (!item) break;
      // pop deeper-indent lists; ALSO pop a same-indent list of a DIFFERENT type (ul vs ol)
      // so a type switch at one indent opens a NEW list rather than mixing kinds. [codex L3 MED]
      while (stack.length && (item.indent < stack[stack.length - 1].indent ||
             (item.indent === stack[stack.length - 1].indent &&
              item.ordered !== stack[stack.length - 1].ordered))) {
        stack.pop();
      }
      if ((!stack.length || item.indent > stack[stack.length - 1].indent) && stack.length < MD_MAX_DEPTH) {
        const listEl = el(item.ordered ? "ol" : "ul", item.ordered ? "md-ol" : "md-ul");
        const parentLi = stack.length ? stack[stack.length - 1].lastLi : null;
        (parentLi || frag).appendChild(listEl);
        stack.push({ indent: item.indent, ordered: item.ordered, listEl, lastLi: null });
      }
      const top = stack[stack.length - 1];
      const li = el("li", "md-li");
      mdInline(item.content, li, 0);
      top.listEl.appendChild(li);
      top.lastLi = li;
      i++;
    }
    return i;
  }

  // Block parser: line-based, single forward pass. Returns a DocumentFragment.
  function renderMarkdown(text) {
    const frag = document.createDocumentFragment();
    const lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    const N = lines.length;
    let lastStart = -1;
    while (i < N) {
      // structural backstop (`structural_invariants_beat_discipline`): i MUST advance every
      // iteration. If a future predicate divergence ever leaves it unmoved (a marker line
      // mdIsBlockStart flags but no block branch consumes), fail OPEN — paint the stuck line
      // as inert text and advance — never spin + OOM the tab. Continue-proof (checked at top).
      if (i === lastStart) { frag.appendChild(el("p", "md-p", lines[i])); i++; continue; }
      lastStart = i;
      const line = lines[i];
      if (!line.trim()) { i++; continue; }  // blank line = block separator

      // fenced code (``` / ~~~): collect verbatim until the matching closing fence
      const fence = /^([ \t]*)(```+|~~~+)[ \t]*([^`~]*)$/.exec(line);
      if (fence) {
        const marker = fence[2][0], minLen = fence[2].length;
        i++;
        const buf = [];
        while (i < N) {
          const cl = /^[ \t]*(`{3,}|~{3,})[ \t]*$/.exec(lines[i]);
          if (cl && cl[1][0] === marker && cl[1].length >= minLen) { i++; break; }
          buf.push(lines[i]); i++;
        }
        const pre = el("pre", "md-pre");
        pre.appendChild(el("code", "md-code-b", buf.join("\n")));
        frag.appendChild(pre);
        continue;
      }

      // ATX heading — content headings start at h3 (the panel title is the h2). A closing
      // `#` run is stripped only when space-preceded (CommonMark), so `### C#` keeps "C#". [codex L3 LOW]
      const h = /^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/.exec(line);
      if (h) {
        const level = Math.min(3 + h[1].length - 1, 6);
        const hd = el("h" + level, "md-h md-h" + h[1].length);
        mdInline(h[2], hd, 0);
        frag.appendChild(hd); i++; continue;
      }

      // thematic break
      if (/^[ \t]*([-*_])[ \t]*(\1[ \t]*){2,}$/.test(line)) { frag.appendChild(el("hr", "md-hr")); i++; continue; }

      // blockquote: consecutive `>` lines (one level)
      if (/^[ \t]*>/.test(line)) {
        const q = el("blockquote", "md-quote");
        const qlines = [];
        while (i < N && /^[ \t]*>/.test(lines[i])) { qlines.push(lines[i].replace(/^[ \t]*>[ \t]?/, "")); i++; }
        mdLinesInto(q, qlines);
        frag.appendChild(q); continue;
      }

      // list
      if (mdListItem(line)) { i = mdBuildList(lines, i, frag); continue; }

      // paragraph: this line wasn't consumed by a block branch, so it OPENS a paragraph —
      // take it unconditionally (a marker-prefixed line no block branch accepted, e.g. a
      // fence whose info string holds a backtick/tilde, must still render as text AND
      // advance i — never spin), then extend until a blank line or a real block starter.
      const para = [lines[i]]; i++;
      while (i < N && lines[i].trim() && !mdIsBlockStart(lines[i])) { para.push(lines[i]); i++; }
      const p = el("p", "md-p");
      mdLinesInto(p, para);
      frag.appendChild(p);
    }
    return frag;
  }

  // The prose display for a section/config panel: markdown-rendered (display-
  // renders), with the raw text reachable via edit (edit-raw). Tagged
  // .section-display so enterEditMode can hide it while the textarea shows raw.
  function sectionDisplay(body) {
    const d = el("div", "section section-display");
    d.appendChild(renderMarkdown(body || ""));
    return d;
  }
  // [MD-EXTRACT-END]

  // The Patterns and Decisions sections are decorated on the Memory tab, never re-parsed: the section is drawn by its own markdown
  // renderer (sectionDisplay, the same path every other section takes), then the rendered DOM is dressed in place. So whatever the
  // markdown shows is still there (names with code spans, severity marks, bracket metadata, tails, lines that do not match), and an
  // item the decoration does not recognise is left exactly as rendered.
  // Patterns: in each list item the `| NNx` token (a direct text child, outside any code span) moves into a leading count chip; what
  // came before it is the name, what follows is muted after it. A `{group}` line is a group label.
  const MEM_COUNT = /\|\s*(\d+)x\b/;
  function memDecoratePatterns(root) {
    for (const p of root.querySelectorAll("p.md-p")) {
      if (/^\s*\{[^}]*\}\s*$/.test(p.textContent)) {
        p.classList.add("ck-mem-group");
        // the FlowScript braces are the file's notation, not the page's: the head reads "Today · 2026-10-21" (the stored text is untouched)
        if (!p.children.length) p.textContent = p.textContent.trim().slice(1, -1).trim().replace(/\s+\u2014\s+/, " \u00b7 ");
      }
    }
    for (const li of root.querySelectorAll("li.md-li")) {
      let at = null, m = null;
      for (const n of li.childNodes) {
        if (n.nodeType !== 3) continue;
        m = MEM_COUNT.exec(n.nodeValue);
        if (m) { at = n; break; }
      }
      if (!at) continue;
      const kids = Array.from(li.childNodes);
      const name = el("span", "ck-mem-name"), rest = el("span", "ck-mem-rest");
      const text = at.nodeValue, before = text.slice(0, m.index), after = text.slice(m.index + m[0].length);
      let seen = false;
      for (const n of kids) {
        if (n === at) { seen = true; if (before) name.appendChild(document.createTextNode(before)); if (after) rest.appendChild(document.createTextNode(after)); }
        else (seen ? rest : name).appendChild(n);
      }
      const t = el("span", "ck-mem-t");
      t.append(name, rest);
      li.replaceChildren(el("span", "ck-mem-n", m[1] + "x"), t);
      li.classList.add("ck-mem-li");
    }
  }
  // Decisions: an item (or paragraph) that begins with a `[decided|forwarded|carried ...]` bracket takes the bracket's date into a
  // leading date column; the whole bracket (who, qualifier) stays visible as muted text. A later line of the same paragraph that
  // opens with such a bracket gets its date inline. Anything else (a `[rejected ...]` line, prose) is left as rendered.
  const MEM_BRACKET = /^(\s*)(\[(decided|forwarded|carried)\b([^\]]*)\])/;
  function memWhen(m) {
    const d = /,\s*(?:\d{4}-)?(\d\d-\d\d)/.exec(m[4]);
    return m[3] === "decided" && d ? d[1] : m[3];
  }
  function memDecorateDecisions(root) {
    for (const host of root.querySelectorAll("p.md-p, li.md-li")) {
      // a paragraph's source lines are split at its <br>s into one block per line, so each decision line is its own row; a line that
      // opens with no bracket (a continuation) stays a block of its own under the row above, its words untouched
      const kids = Array.from(host.childNodes);
      const lines = [[]];
      for (const n of kids) { if (n.nodeName === "BR" && host.nodeName === "P") lines.push([]); else lines[lines.length - 1].push(n); }
      const hits = lines.map((ln) => { const n = ln[0]; return n && n.nodeType === 3 ? MEM_BRACKET.exec(n.nodeValue) : null; });
      if (!hits.some(Boolean)) continue;
      host.replaceChildren();
      lines.forEach((ln, i) => {
        const m = hits[i];
        const row = el("span", m ? "ck-mem-li" : "ck-mem-cont");
        if (m) {
          const first = ln[0];
          first.nodeValue = first.nodeValue.slice(m[0].length);
          const t = el("span", "ck-mem-text");
          t.appendChild(el("span", "ck-mem-cite", m[1] + m[2]));
          for (const n of ln) t.appendChild(n);
          row.append(el("span", "ck-mem-when", memWhen(m)), t);
        } else for (const n of ln) row.appendChild(n);
        host.appendChild(row);
      });
      host.classList.add("ck-mem-dec");
    }
  }

  function renderSection(entry, sec) {
    const p = panel(entry);
    if (!sec) { p.appendChild(el("p", "empty", "—")); return p; }
    const head = String(sec.heading || entry.heading || entry.title || "").trim().toLowerCase();
    const disp = sectionDisplay(sec.body);   // markdown-rendered (edit shows raw)
    // Patterns and Decisions are dressed in place on the Memory tab; a body the decoration finds nothing in stays the markdown it is
    if (head === "patterns") { memDecoratePatterns(disp); disp.classList.add("ck-mem-pats"); }
    else if (head === "decisions") { memDecorateDecisions(disp); disp.classList.add("ck-mem-decs"); }
    p.appendChild(disp);
    // a section with no text is left out of the Memory tab's neocortex card (a bare heading says nothing), unless it can be edited:
    // then it is a slim row that keeps its Edit
    const bare = !String(sec.body || "").trim();
    if (bare) p.dataset.ckNoText = "1";
    if (bare && isEditablePanel(entry)) { p.dataset.ckSlim = "1"; p.classList.add("ck-mem-slim"); p.appendChild(el("p", "empty", "empty")); }
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
    p.appendChild(sectionDisplay(doc.body));  // markdown-rendered (edit shows raw)
    if (isEditablePanel(entry)) {
      // Mark + stash; the edit bar is appended at the panel BOTTOM in finalizePanels
      // (a consistent position, after the pbody — never wraps in the header).
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
    const disp = body.querySelector(".section-display");  // the markdown-rendered display
    if (disp) disp.style.display = "none";
    row.style.display = "none";  // the row is the trigger; hide it while editing
    body.classList.remove("clamped");  // show full untruncated text while editing

    const editor = el("div", "editor");
    const ta = el("textarea", "edit-ta");
    ta.value = doc.body || "";  // .value (not innerHTML) — store text never becomes markup
    ta.dataset.orig = ta.value;  // dirty baseline for hasUnsavedEdit (spore-108): an involuntary
    // re-render (visibilitychange / refresh-button) is deferred while value diverges from this.
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
      if (disp) disp.style.display = "";
      // re-bound after edit; .has-overflow is intentionally NOT recomputed here — cancel
      // leaves content unchanged (the measure still holds) and save triggers a full re-render.
      if (p.hasAttribute("data-clamp")) body.classList.add("clamped");
      row.style.display = "";
      body.dataset.editing = "";
      const again = row.querySelector(".edit-btn");   // the way back in is where focus returns, not the page
      if (again) again.focus();
    };
    cancel.addEventListener("click", restore);
    // Escape cancels the edit (not the whole modal): stopPropagation keeps it from
    // bubbling to the focus-modal's overlay listener, which would otherwise nuke the
    // modal + the unsaved text. Mirrors the masthead rename's Escape→restore. While a
    // save is in-flight (save disabled) Escape is swallowed but does NOT restore — else
    // the pending commit would report into detached DOM (codex L3).
    ta.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!save.disabled) restore();
    });
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
      showFail(msg, res, "save failed");
      save.disabled = false; cancel.disabled = false;
    });
  }

  // ---- Class-B verb affordances (Slice 2b-ii) ----
  // Spores + episodes are the operator's INPUTS, mutated through anneal's validated
  // verbs (never raw writes). A metal button per row; destructive verbs open a small
  // confirm/kind form that sends confirm:true (the server requires it regardless).

  // The cockpit verb each spore button fires. On a manifest row (one that lists its `actions`) a button
  // is kept only when the kernel offered that verb on the row, and is disabled, with the gesture as its
  // title, when the gesture is one a browser cannot perform. A legacy row lists none and is untouched.
  const BUTTON_VERB = { touch: "spore_touch", edit: "spore_update", reclassify: "spore_update", park: "spore_update",
    reactivate: "spore_update", schedule: "spore_surface_at", Snooze: "spore_surface_at", Resolve: "spore_descend", metabolize: "spore_set_disposition",
    "→ tray": "spore_set_disposition", activate: "spore_set_disposition", "remind me": "spore_set_disposition",
    dismiss: "spore_descend", remove: "spore_descend", compost: "spore_descend", promote: "spore_ascend",
    tombstone: "episode_tombstone" };

  // a row tag (a disposition, a tier, a type, hashtags, a due date): plain meta text; the page's CSS decides how it
  // looks, and only a tag marked overdue may take the accent
  function tagEl(cls, text) {
    const t = el("span", cls, text);
    t.setAttribute("data-ck-tag", "");
    return t;
  }
  // whole days a due stamp is before today (negative: still ahead; 0: today); NaN when it is no date. `today` is the server's
  // calendar day ("YYYY-MM-DD", carried on the row): the browser's own zone and clock are used only when the server sent none
  function daysLate(due, today) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(due));
    if (!m) return NaN;
    const tm = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(today || ""));
    const now = new Date();
    const t0 = tm ? Date.UTC(+tm[1], +tm[2] - 1, +tm[3]) : Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((t0 - Date.UTC(+m[1], +m[2] - 1, +m[3])) / 86400000);
  }
  const shortDate = (due) => String(due).slice(5, 10);

  function verbBtn(label, title, onClick) {
    const b = el("button", "verb-btn", label);
    b.type = "button";
    b.setAttribute("data-ck-verb", "");
    if (title) b.title = title;
    if (Object.prototype.hasOwnProperty.call(BUTTON_VERB, label)) b.dataset.verb = BUTTON_VERB[label];
    b.addEventListener("click", onClick);
    return b;
  }

  function gateByActions(wrap, actions) {
    if (!Array.isArray(actions)) return wrap;
    for (const b of Array.prototype.slice.call(wrap.querySelectorAll(".verb-btn[data-verb]"))) {
      const a = actions.find((x) => x && x.verb === b.dataset.verb);
      if (!a) { b.remove(); continue; }
      if (a.gesture === "refused" || a.gesture === "laptop-only") { b.disabled = true; b.title = a.gesture; }
    }
    return wrap;
  }

  // A failed answer in a message span.
  function showFail(msg, res, fallback) {
    msg.className = "edit-msg err";
    msg.textContent = (res && (res.message || res.error)) || fallback;
  }

  function verbErr(wrap, res) {
    let m = wrap.querySelector(".edit-msg");
    if (!m) { m = el("span", "edit-msg", ""); wrap.appendChild(m); }
    showFail(m, res, "failed");
  }

  // A row whose text the page shows altered (hidden characters) offers no inline text edit: saving the
  // shown text would write the sanitiser's marker over the real characters.
  function editBtn(wrap, s) {
    const b = verbBtn("edit", "refine the text", () => openTextEdit(wrap, s));
    if (s.text_locked) { b.disabled = true; b.title = s.text_locked; }
    return b;
  }

  function buildSporeVerbs(s, panelKind) {
    const wrap = el("span", "verb-actions");

    // A TRAY item (the forming workbench): edit + reclassify (plastic WHILE forming) are
    // first-class — the item isn't ready for the lifecycle verbs yet. Then the route levers.
    if (panelKind === "tray") {
      wrap.appendChild(editBtn(wrap, s));
      wrap.appendChild(verbBtn("reclassify", "change the type — it's still forming", () =>
        openReclassify(wrap, s)));
      // metabolize — promote into cognition as a live loop (Tray->loop is never the
      // silent-loss direction, so no confirm); the type LOCKS at this boundary.
      wrap.appendChild(verbBtn("metabolize", "promote into cognition as a live loop", async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const res = await commit({ kind: "spore_set_disposition", spore_id: s.id, disposition: "loop",
          expect_disposition: s.disposition });
        if (res && res.ok) return;  // re-rendered
        b.disabled = false;
        verbErr(wrap, res);
      }));
      wrap.appendChild(verbBtn("Snooze", "resurface on a future date", () => openSurfaceForm(wrap, s)));
      if (Array.isArray(s.descend_kinds) && s.descend_kinds.length) {
        wrap.appendChild(verbBtn("Resolve", "resolve or compost this inbox item", () =>
          openResolveForm(wrap, "spore_descend", "dismiss", s, s.descend_kinds, false)));
      }
      return gateByActions(wrap, s.actions);
    }

    // KEEP — durable reference, NOT loop-lifecycle. Always editable. A NOTE gets the promote
    // lifecycle (→ tray / activate / remind me); a pinned-dormant LOOP reactivates (un-park).
    // Both end with remove.
    if (panelKind === "keep") {
      wrap.appendChild(editBtn(wrap, s));
      if (s.disposition === "note") {
        // The KEEP-NOTE PROMOTE lifecycle (2026-06-23): a deferred someday/maybe note re-enters
        // the flow without delete+recreate (lineage preserved). A pure reference note simply
        // never gets promoted — reference-vs-someday stays emergent (no taxonomy split). Each is
        // a GAIN into cognition / a lateral move into the Tray (never the silent-loss demote), so
        // no confirm. A note can't ascend directly — `activate` to a loop first, then the loop's
        // own `promote` (ascend) is what reaches memory: a deliberate two-step.
        // "→ tray" promotes to a generic `seed` (NOT handoff/agenda) by DESIGN — the operator
        // triages a promoted note to the inbox, then re-classifies it there. The server + CLI
        // allow note→{handoff,agenda} too; the GUI deliberately offers the one triage entry, not
        // every governed transition (L2 2026-06-23, intentional narrowing — "human dumps, AI sorts").
        wrap.appendChild(verbBtn("→ tray", "promote to the Tray inbox — surfaces for triage at next session", async (ev) => {
          const b = ev.currentTarget; b.disabled = true;
          const res = await commit({ kind: "spore_set_disposition", spore_id: s.id, disposition: "seed",
          expect_disposition: s.disposition });
          if (res && res.ok) return;  // re-rendered
          b.disabled = false;
          verbErr(wrap, res);
        }));
        wrap.appendChild(verbBtn("activate", "metabolize into a live cognition loop", async (ev) => {
          const b = ev.currentTarget; b.disabled = true;
          const res = await commit({ kind: "spore_set_disposition", spore_id: s.id, disposition: "loop",
          expect_disposition: s.disposition });
          if (res && res.ok) return;  // re-rendered
          b.disabled = false;
          verbErr(wrap, res);
        }));
        wrap.appendChild(verbBtn("remind me", "resurface in the Tray on a future date", () => openRemindForm(wrap, s)));
      } else {
        wrap.appendChild(verbBtn("reactivate", "un-park → back into active cognition", async (ev) => {
          const b = ev.currentTarget; b.disabled = true;
          // spore-173: the note-vs-parked-loop route was read off the rendered snapshot → CAS it,
          // so a concurrent re-route to a note fails closed instead of tier=warm-ing a note and
          // falsely reporting "reactivated". (Server normalizes 'loop'→key-absent for the compare.)
          const res = await commit({ kind: "spore_update", spore_id: s.id, tier: "warm",
                                     expect_disposition: s.disposition });
          if (res && res.ok) return;  // re-rendered
          b.disabled = false;
          verbErr(wrap, res);
        }));
      }
      if (Array.isArray(s.descend_kinds) && s.descend_kinds.length) {
        wrap.appendChild(verbBtn("remove", "delete this from Keep", () => openRemoveConfirm(wrap, s)));
      }
      return gateByActions(wrap, s.actions);
    }

    // OPEN LOOPS — the entity's live cognition: the lifecycle verbs (+ edit + schedule).
    wrap.appendChild(verbBtn("touch", "mark seen — reset its clock", async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      const res = await commit({ kind: "spore_touch", spore_id: s.id });
      if (res && res.ok) return;  // shim re-fetched + re-rendered → this DOM is gone
      b.disabled = false;
      verbErr(wrap, res);
    }));
    wrap.appendChild(editBtn(wrap, s));
    wrap.appendChild(verbBtn("schedule", "resurface on a future date", () => openSurfaceForm(wrap, s)));
    // park — pin to Keep: hide from active cognition but the loop stays OPEN (exempt from the
    // dormancy→compost prompt), resurfaceable. The exact inverse of Keep's "reactivate"; a tier
    // change (spore_update{tier:parked}) → bucket_of routes it to Keep. NON-destructive (the loop
    // isn't resolved — that's why this is NOT a compost/descend kind), so no confirm.
    wrap.appendChild(verbBtn("park", "pin to Keep — hide from cognition, stays open (un-park to restore)", async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      // spore-173: park is offered only on a loop (this Open-Loops branch) → CAS the snapshot so a
      // concurrent re-route before the write fails closed instead of tier-parking a now-note.
      const res = await commit({ kind: "spore_update", spore_id: s.id, tier: "parked",
                                 expect_disposition: s.disposition });
      if (res && res.ok) return;  // re-rendered
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
    return gateByActions(wrap, s.actions);
  }

  // The schedule form: a date input + confirm -> spore_surface_at (clear = explicit empty).
  // Mirrors openResolveForm's hide-triggers / one-form-at-a-time / Escape-cancel shape.
  function openSurfaceForm(host, s) {
    if (host.querySelector(".verb-form")) return;
    const stale = host.querySelector(".edit-msg"); if (stale) stale.remove();
    const triggers = Array.prototype.slice.call(host.querySelectorAll(".verb-btn"));
    triggers.forEach((b) => (b.style.display = "none"));
    const form = el("span", "verb-form");
    const input = el("input", "verb-date");
    input.type = "date";
    if (s.next) input.value = s.next;  // prefill an existing alarm
    const go = el("button", "verb-confirm", "schedule");
    const clearBtn = el("button", "verb-cancel", "clear");
    const cancel = el("button", "verb-cancel", "cancel");
    go.type = "button"; clearBtn.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    form.append(input, go, clearBtn, cancel, msg);
    host.appendChild(form);
    input.focus();
    const close = () => { form.remove(); triggers.forEach((b) => (b.style.display = "")); };
    cancel.addEventListener("click", close);
    form.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!go.disabled) close();
    });
    const submit = async (value) => {
      go.disabled = true; clearBtn.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit({ kind: "spore_surface_at", spore_id: s.id, surface_at: value });
      if (res && res.ok) return;  // re-rendered
      showFail(msg, res, "failed");
      go.disabled = false; clearBtn.disabled = false; cancel.disabled = false;
    };
    go.addEventListener("click", () => {
      if (!input.value) { msg.className = "edit-msg err"; msg.textContent = "pick a date (or clear)"; return; }
      submit(input.value);
    });
    clearBtn.addEventListener("click", () => submit(""));  // explicit clear: key present, value ""
  }

  // Shared inline-form scaffold for the per-row verbs (edit / reclassify / remove): hide the
  // trigger buttons, mount a form, wire cancel + Escape (stopPropagation so it doesn't nuke an
  // enclosing focus-modal), and run `onConfirm` which returns the commit request (or null to
  // validate-fail with `msg`). One form per row at a time.
  // A write refused as stale (spore-174) leaves the form that submitted it pointing at a row this page no
  // longer has right. Retire THAT form only: its inputs stop counting as unsaved text (so the re-read that
  // replaces the row is not held off by the refused request's own leftovers) and its buttons stay off so the old
  // request cannot be sent again. Other open editors are untouched. The boot layer re-reads on the event.
  function retireStaleForm(form) {
    for (const v of form.querySelectorAll(".verb-ref, .tray-dump-ta")) v.value = "";
    for (const v of form.querySelectorAll(".verb-edit-ta, .verb-domain")) v.value = v.dataset.orig || "";
    for (const b of form.querySelectorAll("button")) b.disabled = true;
    document.dispatchEvent(new CustomEvent("levain:editor-closed"));
  }

  function openRowForm(host, buildFields, onConfirm, confirmLabel, danger) {
    if (host.querySelector(".verb-form")) return;
    const stale = host.querySelector(".edit-msg"); if (stale) stale.remove();
    const triggers = Array.prototype.slice.call(host.querySelectorAll(".verb-btn"));
    triggers.forEach((b) => (b.style.display = "none"));
    const form = el("span", "verb-form");
    const go = el("button", "verb-confirm" + (danger ? " danger" : ""), confirmLabel);
    const cancel = el("button", "verb-cancel", "cancel");
    go.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    const fields = buildFields();  // {nodes:[...], focus:node}
    form.append(...fields.nodes, go, cancel, msg);
    host.appendChild(form);
    if (fields.focus) fields.focus.focus();
    const close = () => { form.remove(); triggers.forEach((b) => (b.style.display = "")); };
    cancel.addEventListener("click", close);
    form.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!go.disabled) close();
    });
    go.addEventListener("click", async () => {
      const req = onConfirm(fields, msg);
      if (!req) return;  // onConfirm set a validation message
      go.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit(req);
      if (res && res.ok) return;  // re-rendered
      showFail(msg, res, "failed");
      if (res && res.error === "stale") { retireStaleForm(form); return; }
      go.disabled = false; cancel.disabled = false;
    });
  }

  // Inline text edit — the forming-workbench refine (spore_update{text}). Any spore. When the kernel offers
  // `domain` on this row's edit, the form carries a domain input too (prefilled from the row): domain is
  // sent only when it differs from what was shown, and as "" only when the operator clears a domain on
  // purpose. A text-only edit keeps the same request.
  function offersDomain(s) {
    const a = Array.isArray(s.actions) ? s.actions.find((x) => x && x.verb === "spore_update") : null;
    return !!a && Array.isArray(a.escalates) && a.escalates.some((x) => x && x.param === "domain");
  }
  function openTextEdit(host, s) {
    const withDomain = offersDomain(s);
    openRowForm(host, () => {
      const ta = el("textarea", "verb-edit-ta");
      ta.value = s.text || "";  // FULL canonical text — the wire no longer truncates, so edit
      // reads/writes the WHOLE thing (edit-raw, no data loss). Size to content so a long spore
      // is visible without hunting in a 2-row box (it still scrolls past the cap; the full
      // value is always in .value regardless).
      const lines = ta.value.split("\n").length;
      ta.rows = Math.min(16, Math.max(3, lines, Math.ceil(ta.value.length / 56)));
      ta.dataset.orig = ta.value;  // dirty baseline for hasUnsavedEdit (spore-108)
      if (!withDomain) return { nodes: [ta], focus: ta, ta };
      const dom = el("input", "verb-domain");
      dom.type = "text"; dom.maxLength = 64; dom.placeholder = "domain";
      dom.setAttribute("aria-label", "domain");
      dom.value = s.domain || "";
      dom.dataset.orig = dom.value;
      return { nodes: [ta, dom], focus: ta, ta, dom };
    }, (f, msg) => {
      // Save the value VERBATIM — only trim for the empty-CHECK. trimming the saved text
      // would silently strip a spore's structural leading/trailing newlines on its first
      // edit (the small sibling of the truncation data-loss this slice fixes) [complement L3].
      const text = f.ta.value;
      if (!text.trim()) { msg.className = "edit-msg err"; msg.textContent = "text can't be empty"; return null; }
      if (!f.dom) return { kind: "spore_update", spore_id: s.id, text };
      const req = { kind: "spore_update", spore_id: s.id };
      const textChanged = text !== (s.text || "");
      const domChanged = f.dom.value.trim() !== (s.domain || "");
      if (!textChanged && !domChanged) { msg.className = "edit-msg err"; msg.textContent = "nothing changed"; return null; }
      if (textChanged) req.text = text;
      if (domChanged) req.domain = f.dom.value.trim();   // "" only when a non-empty domain was cleared
      return req;
    }, "save", false);
  }

  // Inline reclassify — change a forming Tray item's type (spore_update{type}; the server
  // gates it to forming items, locks at metabolize).
  function openReclassify(host, s) {
    openRowForm(host, () => {
      const sel = el("select", "verb-kind");
      for (const t of ["task", "question", "thought"]) { const o = el("option", null, t); o.value = t; sel.appendChild(o); }
      if (s.type) sel.value = s.type;
      return { nodes: [sel], focus: sel, sel };
    }, (f) => ({ kind: "spore_update", spore_id: s.id, type: f.sel.value }), "reclassify", false);
  }

  // Remove confirm — compost a Keep item (a note/reference or a parked loop). Prefer the
  // universal 'composted' kind; fall back to the spore's first descend kind. Remove is remove
  // (one confirm, no kind picker).
  function openRemoveConfirm(host, s) {
    const kinds = Array.isArray(s.descend_kinds) ? s.descend_kinds : [];
    const kind = kinds.indexOf("composted") >= 0 ? "composted" : kinds[0];
    openRowForm(host, () => ({ nodes: [], focus: null }),
      (_f, msg) => {
        if (!kind) { msg.className = "edit-msg err"; msg.textContent = "no remove path"; return null; }
        // spore-173: CAS the rendered snapshot — a concurrent promote of this Keep item to a live
        // loop during the confirm fails closed instead of composting the now-live loop.
        return { kind: "spore_descend", spore_id: s.id, spore_kind: kind, confirm: true,
                 expect_disposition: s.disposition };
      }, "confirm remove", true);
  }

  // "remind me" — promote a Keep note to a Tray seed AND schedule its resurface, ATOMICALLY:
  // one spore_set_disposition carrying surface_at. Two sequential commits is impossible — a
  // successful commit re-renders and the form's DOM is gone — so the schedule rides the promote
  // server-side (one transaction, one CAS). The note leaves Keep for the Tray, hidden until the
  // date, then surfaces for triage; lineage preserved (same spore).
  function openRemindForm(host, s) {
    openRowForm(host, () => {
      const input = el("input", "verb-date");
      input.type = "date";
      return { nodes: [input], focus: input, input };
    }, (f, msg) => {
      if (!f.input.value) { msg.className = "edit-msg err"; msg.textContent = "pick a date"; return null; }
      return { kind: "spore_set_disposition", spore_id: s.id, disposition: "seed", surface_at: f.input.value,
               expect_disposition: s.disposition };
    }, "remind me", false);
  }

  // A freeform capture box. `submit` builds the request from the typed text. Used by the
  // Tray dump (with a type dropdown) and the Keep add-note. No own label — it mounts right
  // under the panel's title (Tray (X) / Keep (X)), like the content of every other panel;
  // the placeholder carries the purpose hint.
  function buildCaptureBox(placeholder, btnLabel, emptyMsg, withType, buildReq, sortEntity) {
    const box = el("div", "tray-dump");
    const ta = el("textarea", "tray-dump-ta");
    ta.placeholder = placeholder; ta.title = "⌘/Ctrl+Enter sends"; ta.rows = 2;
    box.appendChild(ta);
    const controls = el("div", "tray-dump-controls");
    let sel = null;
    if (withType) {
      // "let <entity> sort" omits type → the server defaults (thought + AI routes it). The
      // operator classifies only when they want to; the AI/default is the fallback. The
      // entity name is install-specific (flow / Chip / …), so derive it — never hardcode.
      sel = el("select", "tray-dump-type");
      for (const [val, lbl] of [["", `let ${sortEntity} sort`], ["task", "task"],
                                ["question", "question"], ["thought", "thought"]]) {
        const o = el("option", null, lbl); o.value = val; sel.appendChild(o);
      }
      controls.appendChild(sel);
    }
    const go = el("button", "tray-dump-btn", btnLabel);
    go.type = "button";
    const msg = el("span", "edit-msg", "");
    const submit = async () => {
      const text = ta.value.trim();
      if (!text) { msg.className = "edit-msg err"; msg.textContent = emptyMsg; return; }
      go.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit(buildReq(text, sel && sel.value));
      if (res && res.ok) return;  // re-rendered → fresh panel carrying the new item
      go.disabled = false;
      showFail(msg, res, "failed");
    };
    go.addEventListener("click", submit);
    // Cmd/Ctrl+Enter submits; plain Enter newlines (a capture may be multi-line).
    ta.addEventListener("keydown", (ev) => {
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") { ev.preventDefault(); submit(); }
    });
    controls.append(go, msg);
    box.appendChild(controls);
    return box;
  }

  // The Tray DUMP — the operator's primary capture (a `seed` is born, held out of
  // cognition until triaged). The type dropdown lets them classify; default = let flow sort.
  function buildTrayDump(entity) {
    return buildCaptureBox(
      `Drop anything. ${entity} sorts it.`,
      "drop", "nothing to drop", true,
      (text, type) => {
        const req = { kind: "spore_seed", text };
        if (type) req.type = type;  // omit → server default (thought + AI routes)
        return req;
      }, entity);
  }

  // The Keep ADD-NOTE — authored durable reference (the Docker-commands case): a `note`,
  // cognition-excluded + resolve-exempt. No type (reference never resolves).
  function buildKeepNote() {
    return buildCaptureBox(
      "Add a note or a command",
      "keep", "nothing to keep", false,
      (text) => ({ kind: "spore_seed", text, disposition: "note" }));
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
    // Escape cancels the confirm-form, not the enclosing focus-modal (stopPropagation).
    // No-op while the confirm is in-flight (go disabled), so a pending verb can't be
    // closed out from under its commit / reopened for a double-submit (codex L3).
    form.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!go.disabled) close();
    });
    go.addEventListener("click", async () => {
      const req = { kind: kind, spore_id: s.id, spore_kind: sel.value, confirm: true };
      // spore-173/174: the face (compost/dismiss) and the promote were chosen off the rendered row, so
      // that row's disposition rides the request and the server compares it first; a re-route in the
      // confirm window answers stale instead of acting on a row the operator never saw.
      req.expect_disposition = s.disposition;
      if (needsRef) {
        const ref = refInput.value.trim();
        if (!ref) { msg.className = "edit-msg err"; msg.textContent = "a ref is required"; return; }
        req.ref = ref;
      }
      go.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit(req);
      if (res && res.ok) return;  // re-rendered
      showFail(msg, res, "failed");
      if (res && res.error === "stale") { retireStaleForm(form); return; }
      go.disabled = false; cancel.disabled = false;
    });
  }

  function buildEpisodeVerbs(e) {
    const wrap = el("span", "verb-actions");
    wrap.appendChild(verbBtn("tombstone", "delete this input — the consolidate re-derives without it", () =>
      openTombstoneConfirm(wrap, e)));
    return gateByActions(wrap, e.actions);
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
    // Escape cancels the confirm-form, not the enclosing focus-modal (stopPropagation).
    // No-op while the tombstone is in-flight (go disabled) — same reason as the resolve
    // form: don't close a pending commit out from under itself (codex L3).
    form.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!go.disabled) close();
    });
    go.addEventListener("click", async () => {
      go.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit({ kind: "episode_tombstone", episode_id: e.id, confirm: true });
      if (res && res.ok) return;  // re-rendered
      showFail(msg, res, "failed");
      go.disabled = false; cancel.disabled = false;
    });
  }

  // The edit log + undo surface (Operate zone). Offers undo only on the most-recent
  // non-undo, file-undoable edit PER SOURCE (a safe stack-pop — undoing an older edit
  // would discard newer ones; that's a Slice-2b time-travel concern). Verb-mediated
  // records (undoable === false) show in the log but get no undo (anneal owns their
  // reversibility). Undo restores that edit's backup via the same commit transport.
  // A generic READ-ONLY inline panel injected by a downstream through the extra-panel seam.
  // It is a FIRST-CLASS dashboard panel: "external" is in CLAMP_KINDS, so it gets the SAME
  // fixed-height + internal scroll + ⤢ expand-to-modal every other box has (no link off the
  // page — the modal IS the full view). Each line renders as a `.row` with an optional leading
  // `meta` label + the body as an `appendClause` (clamped to 4 lines with per-item expand —
  // the episodes/spores idiom), plus an optional `accent` (e.g. unread) / `dim` (e.g. read)
  // flag. The kernel knows nothing domain-specific — the flow Bridge renders its inbox here.
  // Every store-derived string is textContent (via el()/appendClause), never innerHTML.
  // The governed COMPOSE affordance for an external panel's action verb (the external-panel-
  // ACTION seam — the write-peer of how spore panels carry verbs). One control per declared
  // field (textarea | text | csv); a submit labeled from the verb REGISTRY; when the verb is
  // confirm_required, a confirm/cancel step before sending confirm:true (the kernel 409s without
  // it — the confirm is the fat-finger affordance, the server is the gate). On success the board
  // re-renders (the sent item appears, the box clears); errors surface inline. Every value is
  // read via .value and built via el()/createElement — never innerHTML.
  // [ACTION-EXTRACT-START — the node behavioral harness slices between these markers.]
  // Pure field→params coercion for the compose affordance (DOM-free so it's behaviorally
  // testable): csv → trimmed non-empty list, omitted when empty (so send_inbox's `for` keeps its
  // STRICT list contract — a real list or absent, never a coerced string); text/textarea →
  // trimmed string, omitted when empty + optional; a required field resolving empty → {error}.
  // It reads the values it is GIVEN — the caller snapshots them at submit and the confirm step
  // FREEZES the inputs, so the snapshot can't drift from what the operator sees (WYSIWYG).
  function collectActionParams(fields) {
    // null-proto params (codex L3): a field literally named "__proto__"/"constructor" can't mutate
    // the params prototype + silently drop from JSON — it lands as a normal own key. (The server
    // ALSO rejects such field names when folding the panel action; this is defense-in-depth.)
    const params = Object.create(null);
    const seen = new Set();
    for (const f of fields) {
      if (seen.has(f.name)) return { error: "duplicate field " + f.name };  // no silent overwrite
      seen.add(f.name);
      if (f.kind === "multiselect") {
        // a fixed-set peer of csv: value is the ARRAY of chosen option values (checkboxes), so it
        // never round-trips through a string. FAIL CLOSED on a non-array (a read() regression that
        // returns "cli" must error, not silently broadcast — codex L3); trim + drop blanks + dedupe.
        if (!Array.isArray(f.value)) return { error: (f.label || f.name) + " is invalid" };
        const ms = new Set();
        const list = [];
        for (const v of f.value) {
          if (typeof v !== "string") continue;
          const t = v.trim();
          if (t && !ms.has(t)) { ms.add(t); list.push(t); }
        }
        if (f.required && !list.length) return { error: (f.label || f.name) + " is required" };
        // something was chosen and none of it is a usable id: refuse, never omit the field (for a recipient
        // field an omitted value is a send to everyone)
        if (f.value.length && !list.length) return { error: (f.label || f.name) + " has no usable choice" };
        if (list.length) params[f.name] = list;
        continue;
      }
      const raw = f.value != null ? String(f.value) : "";
      if (f.kind === "csv") {
        const list = raw.split(",").map((s) => s.trim()).filter((s) => s.length);
        if (f.required && !list.length) return { error: (f.label || f.name) + " is required" };
        if (list.length) params[f.name] = list;
      } else {
        const val = raw.trim();
        if (f.required && !val) return { error: (f.label || f.name) + " is required" };
        if (val) params[f.name] = val;
      }
    }
    return { params: params };
  }
  // [ACTION-EXTRACT-END]

  // A client-supplied idempotency key for an irreversible verb (the at-most-once retry token).
  // crypto.randomUUID in a secure context (localhost / Tailscale-Serve HTTPS); a getRandomValues
  // hex fallback otherwise. Generated ONCE per compose-box instance (below) and reused across any
  // retry of THIS send — a successful send rebuilds the board (load()), so the next message gets
  // a fresh box → a fresh key. So every distinct send dedupes on its own key; every retry of the
  // same send (network fail → re-click, or a tailnet/proxy replay) reuses it.
  function newIdempotencyKey() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
      if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        const a = new Uint8Array(16);
        window.crypto.getRandomValues(a);
        return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
      }
    } catch (_) { /* fall through */ }
    return "idem-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  // An async job's result lives in MODULE state; the DOM is a PROJECTION of it (the canonical-object
  // discipline). So the result survives ANY re-render — a board rebuild AND the expand-to-modal,
  // which builds a FRESH consult box (the carry-over the visual gate flagged). Keyed by verb.
  const _jobState = {};  // verb → { kind: "running"|"done"|"err", payload?, label }
  function renderJobResultInto(wrap, verb) {
    const st = _jobState[verb];
    wrap.replaceChildren();
    if (!st) return;  // no run yet → empty (the box just shows the compose controls)
    const label = st.label || verb;
    if (st.kind === "running") {
      wrap.appendChild(el("div", "job-status running", label + " — running…"));
      return;
    }
    if (st.kind === "done") {
      const p = st.payload || {};
      wrap.appendChild(el("div", "job-status done", typeof p.summary === "string" ? p.summary : "done"));
      const scroll = el("div", "job-result");
      const md = el("div", "section-display");  // markdown styling container (the .md-* classes)
      const text = typeof p.text === "string" ? p.text : JSON.stringify(p, null, 2);
      md.appendChild(renderMarkdown(text));     // MARKDOWN → DOM, CSP-safe (the shipped renderer)
      scroll.appendChild(md);
      wrap.appendChild(scroll);
      return;
    }
    wrap.appendChild(el("div", "job-status err", typeof st.payload === "string" ? st.payload : "failed"));
  }
  // Re-render the cached job state into EVERY current result-wrap for this verb — the inline panel
  // AND an open modal can each hold one. DOM-queried fresh, so it also reaches a box built AFTER the
  // poll began (e.g. the modal opened mid-consult).
  function refreshJobBoxes(verb) {
    document.querySelectorAll('[data-job-verb="' + verb + '"] .job-result-wrap')
      .forEach((w) => renderJobResultInto(w, verb));
  }

  // The editor for a text panel: closed by default behind an "edit" button, prefilled with the current text, saved through
  // the panel's own verb (the same box a compose uses, so confirm, keys and failure text behave the same).
  function mountTextEditor(p, act, spec, current) {
    const field = Object.assign({}, act.fields[0], { kind: "textarea", label: "Edit this text" });
    const box = buildActionBox(Object.assign({}, act, { fields: [field] }), spec);
    box.classList.add("text-editor");
    const ta = box.querySelector("textarea");
    if (ta) { ta.value = current; ta.dataset.orig = ta.value; ta.rows = Math.min(14, Math.max(4, current.split("\n").length + 1)); }
    const save = box.querySelector(".tray-dump-btn");
    if (save) save.textContent = "save";
    const cancel = el("button", "verb-cancel", "cancel");
    cancel.type = "button";
    if (save && save.parentNode) save.parentNode.insertBefore(cancel, save.nextSibling);
    box.hidden = true;
    const trigger = el("div", "edit-trigger-row");
    const open = el("button", "edit-btn", "edit");
    open.type = "button";
    trigger.appendChild(open);
    const show = (on) => { box.hidden = !on; trigger.hidden = on; if (on && ta) ta.focus(); else open.focus(); };
    open.addEventListener("click", () => show(true));
    cancel.addEventListener("click", () => { if (ta) ta.value = current; show(false); });
    box.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !(save && save.disabled)) { ev.preventDefault(); ev.stopPropagation(); if (ta) ta.value = current; show(false); } });
    mountCaptureBox(p, box);
    box.before(trigger);
  }

  // a compose box is one line until something is typed, then as tall as its text
  // (the text editor of an identity or section card is built by the same builder, and keeps its own rows and its resize handle)
  function growToText(ta) {
    if (ta.closest(".text-editor")) return;
    ta.style.height = "auto";
    ta.style.height = (ta.scrollHeight + ta.offsetHeight - ta.clientHeight) + "px";
  }

  // The value of the picker's option for a choice of several; and the pick a compose card keeps across the rebuild of its box (by
  // panel), until a send lands. Which reader chips start pressed (`default`), which ids are seats (`transient_prefix`: messaged from
  // the seat's own page, never drawn as a chip on the card) and the words of the refusal that says one has left (`gone_match`) are
  // declared by the provider on its recipient field.
  const SEVERAL_VALUE = "*several";
  const CHOOSE_VALUE = "*choose";
  const composePick = Object.create(null);
  // which cards' kept pick is an "everyone" the person chose themselves: set only by their own pick (a chip, the picker), never
  // read off an empty kept pick, and kept and taken with that pick
  const composeEveryone = Object.create(null);

  // `stay` (optional) is a box addressed to ONE reader that outlives its own send (the agent page's message box, which no board reload
  // rebuilds): {pickKey, gone}. Its recipient field holds that one id; no chip or picker is drawn, so it can never fall back to everyone;
  // its kept pick is its own key; a landed send empties its text and says so; the departed-reader refusal calls `gone` (it holds the box).
  function buildActionBox(action, spec, stay) {
    const box = el("div", "tray-dump");
    // The at-most-once key for an idempotent verb is stable per SEND CONTENT: a retry of identical
    // content (a network-fail re-click, or a tailnet/proxy/browser replay of the same body) reuses
    // the key → dedupe; editing the content before resending mints a new key → a new logical send
    // (fires). null for a non-idempotent verb → omitted from the POST (the legacy replay-tolerant
    // path). A successful send rebuilds the board (load()) → a fresh box → this cache resets.
    let lastSend = null;  // { fp, key }
    const keyForSend = (params) => {
      if (!spec.idempotent) return null;
      const fp = JSON.stringify(params);
      if (!lastSend || lastSend.fp !== fp) lastSend = { fp: fp, key: newIdempotencyKey() };
      return lastSend.key;
    };
    const fields = Array.isArray(action.fields) ? action.fields : [];
    // Each control exposes read() → its current value (string for text/textarea/csv, ARRAY for
    // multiselect) and nodes[] → the inputs to freeze on confirm. A uniform shape so collect()
    // and setDisabled() don't branch on the field kind.
    const controls = [];
    for (const f of fields) {
      if (!f || typeof f !== "object" || !f.name) continue;
      if (f.kind === "multiselect" && f.chips) {
        // the KE compose card's recipient chips: toggle buttons. The provider's declared `default` ids (those in the enum) are pressed
        // when nothing else was chosen; un-pressing them leaves none pressed = the field is omitted = everyone. The enum is the one the panel head
        // carries NOW (this builds at every render and refetch). An id under the declared `transient_prefix` is a seat: the card draws no
        // chip for it (Phill 10-25, "61a": a seat is messaged from its node's page, the card is for the desk, the other readers or everyone).
        // At a phone width the same values are a <select> (a pick list holds a long roster; CSS shows one of the two), and every
        // change goes through one setter so chips, picker, address line and the kept pick agree.
        const group = el("div", "ck-to");
        group.appendChild(el("span", "ck-to-label", "To"));
        const chips = [];
        const seatPrefix = !stay && typeof f.transient_prefix === "string" ? f.transient_prefix : "";
        for (const opt of (Array.isArray(f.options) ? f.options : [])) {
          const value = typeof opt === "string" ? opt : (opt && opt.value);
          if (typeof value !== "string" || !value || (seatPrefix && value.startsWith(seatPrefix))) continue;
          const chip = el("button", "rc-chip", typeof opt === "string" ? opt : (opt.label || value));
          chip.type = "button";
          chip.dataset.value = value;
          chip.setAttribute("aria-pressed", "false");
          chip.setAttribute("data-ck-compose", "recipient");
          chip.addEventListener("click", () => {
            userPick(chips.filter((c) => (c === chip) !== (c.getAttribute("aria-pressed") === "true")).map((c) => c.dataset.value));
          });
          group.appendChild(chip);
          chips.push(chip);
        }
        const pick = el("select", "rc-pick");
        pick.setAttribute("aria-label", f.label ? String(f.label) : "To");
        pick.setAttribute("data-ck-compose", "recipient-pick");
        // the picker's options are told apart by IDENTITY, never by value: a roster may legally hold an id equal to the synthetic one's value.
        // While the card still needs a choice it shows "choose a reader", so picking "everyone" moves the picker and is a change like any other.
        const choose = Object.assign(el("option", null, "choose a reader"), { value: CHOOSE_VALUE, hidden: true, disabled: true });
        pick.appendChild(choose);
        const everyone = Object.assign(el("option", null, "everyone"), { value: "" });
        pick.appendChild(everyone);
        const optOf = new Map();
        for (const c of chips) { const o = Object.assign(el("option", null, c.textContent), { value: c.dataset.value }); pick.appendChild(o); optOf.set(c.dataset.value, o); }
        const several = Object.assign(el("option", null, "several readers"), { value: SEVERAL_VALUE, hidden: true, disabled: true });
        pick.appendChild(several);
        const optAt = (o) => Array.prototype.indexOf.call(pick.options, o);
        pick.addEventListener("change", () => {
          const o = pick.options[pick.selectedIndex];
          if (!o || o === choose) return;
          userPick(o === everyone || o === several ? [] : [o.value]);
        });
        group.appendChild(pick);
        group.appendChild(el("span", "ck-to-hint", "none selected = everyone"));
        if (!stay) box.insertBefore(group, box.firstChild);
        const pressedNow = () => chips.filter((c) => c.getAttribute("aria-pressed") === "true").map((c) => c.dataset.value);
        // `remember` keeps the pick across a rebuild; only the person's own pick (userPick) passes true
        function setTo(values, remember) {
          const want = new Set(values);
          for (const c of chips) c.setAttribute("aria-pressed", want.has(c.dataset.value) ? "true" : "false");
          const now = pressedNow();
          pick.selectedIndex = optAt(now.length === 1 ? optOf.get(now[0]) : (now.length ? several : (ctl.needsChoice ? choose : everyone)));
          if (remember === true) composePick[pickKey] = now;
          chipAddr();
        }
        // the person's pick (a chip click or a picker change, the only callers): the one place the card's need for a choice is answered
        function userPick(values) {
          ctl.needsChoice = false;
          setTo(values, true);
          if (composePick[pickKey].length) delete composeEveryone[pickKey]; else composeEveryone[pickKey] = true;
          if (ctl.onPick) ctl.onPick();
        }
        // a rebuild of this box (every refetch of the panel) keeps what the person chose, as far as it is still in the enum
        const pickKey = stay && stay.pickKey ? String(stay.pickKey) : String(action.panel || action.verb);
        const kept = composePick[pickKey];
        const inEnum = new Set(chips.map((c) => c.dataset.value));
        const keptNow = Array.isArray(kept) ? kept.filter((v) => inEnum.has(v)) : null;
        const dflt = Array.isArray(f.default) ? f.default.filter((v) => inEnum.has(v)) : [];
        const start = keptNow && (keptNow.length || !kept.length) ? keptNow : dflt;
        // a declared default that names a seat (which the card does not draw) cannot start pressed: an empty pick is "everyone", so
        // a send from that untouched state is refused rather than widened. A pick the person makes themselves (a chip, the picker,
        // "everyone" included, or their "everyone" kept from before this rebuild) is a send like any other: `needsChoice` goes at their pick.
        const seatDefault = !!seatPrefix && Array.isArray(f.default) && f.default.some((v) => typeof v === "string" && v.startsWith(seatPrefix));
        const ctl = { f: f, nodes: chips.concat([pick]), read: pressedNow, set: setTo, chips: chips, pickKey: pickKey, initial: start,
                      default: dflt, needsChoice: seatDefault && !start.length && composeEveryone[pickKey] !== true, onPick: null };
        controls.push(ctl);
        continue;
      }
      if (f.kind === "multiselect") {
        // a fixed-set chooser (checkboxes) — the operator picks from known options instead of
        // recalling names; nothing checked → the field is omitted (e.g. broadcast for `for`).
        const group = el("div", "action-multiselect");
        if (f.label) group.appendChild(el("div", "action-ms-label", String(f.label)));
        const opts = el("div", "action-ms-opts");
        const boxes = [];
        for (const opt of (Array.isArray(f.options) ? f.options : [])) {
          const value = typeof opt === "string" ? opt : (opt && opt.value);
          if (typeof value !== "string" || !value) continue;
          const lbl = el("label", "action-ms-opt");
          const cb = el("input"); cb.type = "checkbox"; cb.value = value;
          lbl.appendChild(cb);
          lbl.appendChild(el("span", null, typeof opt === "string" ? opt : (opt.label || value)));
          opts.appendChild(lbl);
          boxes.push(cb);
        }
        group.appendChild(opts);
        box.appendChild(group);
        controls.push({ f: f, nodes: boxes,
                        read: () => boxes.filter((b) => b.checked).map((b) => b.value) });
        continue;
      }
      const isTA = f.kind === "textarea";
      const node = el(isTA ? "textarea" : "input", "tray-dump-ta");
      if (isTA) { node.rows = 1; node.addEventListener("input", () => growToText(node)); } else node.type = "text";
      if (f.placeholder) node.placeholder = String(f.placeholder);
      if (f.label) node.setAttribute("aria-label", String(f.label));
      box.appendChild(node);
      controls.push({ f: f, nodes: [node], read: () => node.value });
    }
    // an action with no field to fill is a bare button: no field bar, no box
    if (!controls.length) box.classList.add("action-bare");
    const row = el("div", "tray-dump-controls");
    const go = el("button", "tray-dump-btn", spec.label || action.verb);
    go.type = "button";
    // the card's address line says in words who a send reaches: the chosen readers, or everyone
    const chipCtl = controls.find((c) => c.f.chips);
    const addr = chipCtl ? el("span", "ck-addr", "") : null;
    function chipAddr() {
      if (!addr) return;
      const to = chipCtl.read();
      addr.textContent = "to " + (to.length ? to.join(", ") : "everyone");
    }
    if (addr) { chipAddr(); row.appendChild(addr); }
    if (chipCtl) {
      chipCtl.set(chipCtl.initial, false);
    }
    const msg = el("span", "edit-msg", "");
    row.append(go, msg);
    box.appendChild(row);
    const NO_SEAT_DEFAULT = "this card's default reader is a seat, which is messaged from its page; choose a reader";
    const NO_SEAT_REFUSED = NO_SEAT_DEFAULT + ". Nothing was sent";
    if (chipCtl && chipCtl.needsChoice) { msg.className = "edit-msg"; msg.textContent = NO_SEAT_DEFAULT; }
    // the person's pick answers those words, so they go with it
    if (chipCtl) chipCtl.onPick = () => { if (msg.textContent === NO_SEAT_DEFAULT || msg.textContent === NO_SEAT_REFUSED) { msg.className = "edit-msg"; msg.textContent = ""; } };
    // a landed send's "sent" goes at the next keystroke, so it never sits beside an unsent draft
    box.addEventListener("input", () => { if (stay && msg.textContent === "sent") { msg.className = "edit-msg"; msg.textContent = ""; } });

    const setDisabled = (d) => {
      go.disabled = d;
      for (const c of controls) for (const n of c.nodes) n.disabled = d;
    };

    // Snapshot the live field values → the pure coercion (collectActionParams above). The confirm
    // step freezes the inputs after this snapshot, so what's reviewed == what's sent (WYSIWYG).
    const collect = () => collectActionParams(controls.map((c) =>
      ({ name: c.f.name, kind: c.f.kind, required: c.f.required, label: c.f.label, value: c.read() })));

    // The kept pick is taken BEFORE a send goes out: a landed send rebuilds the board (and this box) before its promise resolves, and the
    // fresh box must start from the provider's declared default, not from the pick it just used. The returned restore puts it back if the send fails.
    const takePick = () => {
      if (!chipCtl) return null;
      const k = chipCtl.pickKey;
      const had = k in composePick, snap = composePick[k], chose = composeEveryone[k];
      delete composePick[k]; delete composeEveryone[k];
      return () => {
        if (!had || k in composePick) return;
        composePick[k] = snap;
        if (chose === true) composeEveryone[k] = true;
      };
    };
    // Returns true when the box is HELD disabled after the departed-seat refusal (the caller must not re-enable it).
    const failed = (res, restore) => {
      if (restore) restore();
      showFail(msg, res, "failed");
      // a seat's box whose seat left the roster since its page was drawn: the serving app's refusal carries the words the provider
      // declared as `gone_match` (code or words, whichever transport carried it). A substring test, never a pattern built from them.
      const text = res ? [res.error, res.message].map((x) => String(x || "")).join(" ") : "";
      const gm = chipCtl ? chipCtl.f.gone_match : null;
      if (stay && typeof gm === "string" && gm && text.toLowerCase().includes(gm.toLowerCase())) {
        if (typeof stay.gone === "function") stay.gone(msg);
        return true;
      }
      setDisabled(false);
      return false;
    };
    // a send landed on a box that stays: its typed text goes, its reader stays, and the next identical text is a new send
    const landed = () => {
      lastSend = null;
      for (const c of controls) {
        if (c.f.chips || c.f.kind === "multiselect") continue;
        for (const n of c.nodes) { n.value = ""; if (n.tagName === "TEXTAREA") growToText(n); }
      }
      setDisabled(false);
      msg.className = "edit-msg"; msg.textContent = "sent";
    };
    // ── async-job (spec.job) result area + poll loop ──
    // A job verb PROPOSES (commitJob — NO board reload) then polls /job.json. The result lives in
    // MODULE state (_jobState[verb], rendered by refreshJobBoxes); THIS box just hosts a result-wrap
    // tagged with the verb so the projection finds it. Mount it + render the current cached state on
    // EVERY build, so the result carries into a fresh box (the modal / a rebuild). The cockpit stays
    // responsive while a 30s–3min consult runs out-of-band.
    if (spec.job) {
      box.dataset.jobVerb = action.verb;
      const wrap = el("div", "job-result-wrap");
      box.appendChild(wrap);
      renderJobResultInto(wrap, action.verb);  // re-show the last/live result in this fresh box
    }
    const setJob = (kind, payload) => {
      _jobState[action.verb] = { kind: kind, payload: payload, label: spec.label || action.verb };
      refreshJobBoxes(action.verb);
    };
    const startPoll = (jobId) => {
      _activeJobs.add(jobId);
      let netErrs = 0;
      const stop = () => {
        _activeJobs.delete(jobId);
        // last job done → let the boot layer flush any board reload it deferred while polling.
        if (_activeJobs.size === 0 && onJobsIdle) onJobsIdle();
      };
      const tick = async () => {
        let r;
        try { r = await pollJob(jobId); netErrs = 0; }
        catch (e) {
          if (++netErrs > 5) { setJob("err", "poll failed — the job may still be running"); stop(); return; }
          setTimeout(tick, 2000); return;
        }
        const st = r && r.status;
        if (st === "pending" || st === "running") { setTimeout(tick, 2000); return; }
        if (st === "done") { setJob("done", r.result); stop(); return; }
        if (st === "failed") { setJob("err", "failed: " + ((r && r.error) || "")); stop(); return; }
        if (st === "unknown") { setJob("err", "job not found (expired) — run again"); stop(); return; }
        setJob("err", "unexpected status: " + st); stop();
      };
      setTimeout(tick, 1000);  // first poll soon, then every 2s
    };
    // a fresh object each time: the refusal a send takes when a read-only render took its transport away
    const READ_ONLY_REFUSAL = () => ({ ok: false, error: "read-only", message: "this page can no longer send; nothing was sent" });
    const startJob = async (params, key, restore) => {
      msg.className = "edit-msg busy"; msg.textContent = "starting…";
      // the confirm step runs after the click guard: a read-only render in between took commitJob away
      const res = commitJob ? await commitJob(action.verb, params, true, key) : READ_ONLY_REFUSAL();
      if (!(res && res.ok)) return failed(res, restore);
      msg.className = "edit-msg"; msg.textContent = "";
      // a job start rebuilds nothing: put this live box's chips back to its start state so the shown pick is the kept one
      if (chipCtl && box.isConnected) chipCtl.set(chipCtl.default, false);
      setJob("running", null);
      startPoll(res.job_id);
    };

    // A signed verb (spec.sign, DESIGN_SIGN D5) is posted only after the operator's passkey signed exactly these params
    // (signedCompose), with `sign_request` added; anything short of a signed result posts nothing, and the draft stays until
    // a signed send lands (the board's rebuild after it is what clears the box). The draft's one identity (its `txn`) keys
    // the post, never the request it names: a lost answer retried, or retried after a reload, replays the first send. A
    // definite refusal (a 4xx) forgets the draft's request, so the next send files and signs again; an answer that never
    // came keeps it. Any other verb posts as it always has.
    // the transport this box sends through, read at the send (a later read-only render takes it away): a job verb's start, else the action post
    const sendWay = () => (spec.job ? commitJob : commitAction);
    const fire = async (params, confirm, key) => {
      if (!commitAction) return READ_ONLY_REFUSAL();  // fire posts through commitAction whatever sendWay() names
      if (!spec.sign) return commitAction(action.verb, params, confirm, key, action.panel);
      const say = (t, k) => { msg.className = "edit-msg" + (k === "bad" ? " err" : k === "ok" ? "" : " busy"); msg.textContent = t; };
      const s = await signedCompose(action.verb, action.panel, params, say, row);
      if (!s.ok) return { ok: false, error: "unsigned", message: s.message };
      const signed = Object.assign({}, params, { sign_request: s.request });
      // the ceremony awaited: a read-only render in that time took the transport away, so it is read again here
      if (!commitAction) return READ_ONLY_REFUSAL();
      const res = await commitAction(action.verb, signed, confirm, "sign-" + s.txn, action.panel, s.txn);
      if ((res && res.ok) || (res && typeof res.status === "number" && res.status >= 400 && res.status < 500)) {
        signDraftDrop(action.verb, action.panel, params);
      }
      return res;
    };
    go.addEventListener("click", () => {
      if (!sendWay()) { setDisabled(true); msg.className = "edit-msg err"; msg.textContent = "this page can no longer send; nothing was sent"; return; }
      const c = collect();
      if (chipCtl && chipCtl.needsChoice && !chipCtl.read().length) { msg.className = "edit-msg err"; msg.textContent = NO_SEAT_REFUSED; return; }
      if (stay && (!chipCtl || chipCtl.read().length !== 1)) { msg.className = "edit-msg err"; msg.textContent = "no reader to send to; nothing was sent"; return; }
      if (c.error) { msg.className = "edit-msg err"; msg.textContent = c.error; return; }
      const sendKey = keyForSend(c.params);  // stable across a retry of THIS content (above)
      if (!spec.confirm_required) {
        setDisabled(true); msg.className = "edit-msg busy"; msg.textContent = "…";
        const restore = takePick();
        if (spec.job) { startJob(c.params, sendKey, restore).then((held) => { if (!held) setDisabled(false); }); return; }
        fire(c.params, false, sendKey).then((res) => { if (!(res && res.ok)) failed(res, restore); else if (stay) landed(); });
        return;
      }
      // confirm step (the fat-finger gate): swap the send button for a confirm/cancel pair AND
      // FREEZE the fields — the params were snapshotted by collect() above, so disabling the
      // inputs makes what-you-see == what-you-send (a still-editable field would let the operator
      // change the text after the payload is frozen → a stale send; L1+L2 converged). Disabling
      // them also drops focus off the textarea, so Escape reliably lands on the confirm form (it
      // cancels the confirm — NOT the enclosing focus-modal, via stopPropagation). cancel/fail
      // re-enable. We focus `no` so a stray Enter/Space defaults to cancel, never send.
      go.style.display = "none";
      setDisabled(true);
      const form = el("span", "verb-form");
      const yes = el("button", "verb-confirm", "send");
      const no = el("button", "verb-cancel", "cancel");
      yes.type = "button"; no.type = "button";
      form.append(yes, no);
      row.insertBefore(form, msg);
      no.focus();
      msg.className = "edit-msg"; msg.textContent = "";
      const close = (keepFrozen) => { form.remove(); go.style.display = ""; if (!keepFrozen) setDisabled(false); };
      no.addEventListener("click", () => close());
      form.addEventListener("keydown", (ev) => {
        if (ev.key !== "Escape") return;
        ev.preventDefault(); ev.stopPropagation();
        if (!yes.disabled) close();
      });
      yes.addEventListener("click", async () => {
        yes.disabled = true; no.disabled = true; setDisabled(true);
        msg.className = "edit-msg busy"; msg.textContent = "…";
        const restore = takePick();
        if (spec.job) {
          // A job verb PROPOSES (no board reload), then polls in-box. Close the confirm form
          // (restores the compose box + re-enables it for a next run); the result renders below.
          const held = await startJob(c.params, sendKey, restore);
          close(held);
          return;
        }
        const res = await fire(c.params, true, sendKey);
        if (res && res.ok) { if (stay) { close(); landed(); } return; }  // re-rendered → this box is gone, replaced fresh (a box that stays is not)
        close(true); failed(res, restore);
      });
    });

    // Cmd/Ctrl+Enter on a textarea triggers the first step (matches the capture-box idiom), but
    // only while the send button is showing (not mid-confirm).
    for (const c of controls) {
      if (c.f.kind !== "textarea") continue;
      c.nodes[0].addEventListener("keydown", (ev) => {
        if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter" && go.style.display !== "none") {
          ev.preventDefault(); go.click();
        }
      });
    }
    return box;
  }

  // A waiting pending's cancel: posted with the gesture's intent and no preview. A gesture a browser
  // cannot perform is disabled with the gesture as its title.
  // A review queue row's one control: Clear on a plain open item, Reopen on a cleared one, posted as the row's own offered
  // verb. The server takes it only from a paired device's credential and says so on this shared-token page. A "yours to
  // clear" item is cleared only on your passkey's signature (review_clear_signed): its button files levain's own sign
  // request (signedCompose), runs the ceremony, and only then posts the verb with `sign_request`; the row also names the
  // laptop command, which clears it unsigned.
  function appendReviewVerb(row, rv) {
    if (rv.mine && rv.state === "open") {
      // drawn on the row itself, never in the hover-revealed verb group: it is words to read, not a control
      row.appendChild(el("div", "ext-meta review-mine", (rv.verb === "review_clear_signed" && reviewItem ? "or " : "not cleared here: ")
        + "on the laptop, levain coord review clear " + rv.item));
      if (rv.verb === "review_clear_signed" && reviewItem) appendReviewSigned(row, rv);
      return;
    }
    const wrap = el("span", "verb-actions");
    if (!rv.verb || !reviewItem) return;
    const clear = rv.verb === "review_clear";
    const b = verbBtn(clear ? "clear" : "reopen", clear ? "clear this review item as you" : "put it back in the queue", async () => {
      b.disabled = true;
      let res;
      try { res = await reviewItem(rv.id, rv.verb); } catch (e) { res = { ok: false, message: "the " + (clear ? "clear" : "reopen") + " could not be sent" }; }
      if (res && res.ok) return;   // the board re-reads and the row shows its new state
      b.disabled = false;
      verbErr(wrap, res);
    });
    wrap.appendChild(b);
    appendRowVerbs(row, wrap);
  }

  // The signed clear of a "yours to clear" item: the draft is {item} on the review panel (its one identity, signedCompose),
  // signed with the passkey, then sent as the row's review_clear_signed with `sign_request` under that draft's key. A send
  // that landed, or a definite refusal (a 4xx: a signature over an older version, say), forgets the draft, so the next
  // tap files and signs again; an answer that never came keeps it.
  function appendReviewSigned(row, rv) {
    const wrap = el("span", "verb-actions");
    const msg = el("span", "edit-msg", "");
    const say = (t, k) => { msg.className = "edit-msg" + (k === "bad" ? " err" : k === "ok" ? "" : " busy"); msg.textContent = t; };
    const params = { item: rv.item };
    const b = verbBtn("clear with passkey", "clear this item on your passkey's signature", async () => {
      b.disabled = true;
      let res;
      try {
        const s = await signedCompose("review_clear_signed", "review-queue", params, say, row);
        if (!s.ok) { b.disabled = false; return; }
        say("clearing…");
        res = await reviewItem(rv.id, "review_clear_signed", Object.assign({}, params, { sign_request: s.request }), s.txn);
      } catch (e) { res = { ok: false, message: "the clear could not be sent" }; }
      if ((res && res.ok) || (res && typeof res.status === "number" && res.status >= 400 && res.status < 500)) {
        signDraftDrop("review_clear_signed", "review-queue", params);
      }
      if (res && res.ok) return;   // the board re-reads and the row moves to Cleared
      b.disabled = false;
      verbErr(wrap, res);
    });
    wrap.append(b, msg);
    appendRowVerbs(row, wrap);
  }

  function buildPendingCancel(c) {
    const wrap = el("span", "verb-actions");
    const b = verbBtn("cancel", "cancel this waiting request", async () => {
      b.disabled = true;
      const res = await cancelPending(c);
      if (res && res.ok) return;  // re-rendered
      b.disabled = false;
      verbErr(wrap, res);
    });
    if (c.gesture === "refused" || c.gesture === "laptop-only") { b.disabled = true; b.title = c.gesture; }
    wrap.appendChild(b);
    return wrap;
  }

  function renderExternal(entry, view, opts) {
    const p = renderExternalBody(entry, view, opts);
    if (p.dataset && p.dataset.proposalSlot != null && !p.querySelector(".proposal-slot")) p.appendChild(standingSlots());
    return p;
  }
  function renderExternalBody(entry, view, opts) {
    const p = panel(entry);
    const data = (view.extra_panels || {})[entry.id] || {};
    if (data.error) {
      p.appendChild(el("p", "err", "unavailable — " + data.error));
      return p;
    }
    // Governed compose affordance: mount it when the surface is writable (commitAction injected)
    // AND the panel declares an `action` whose verb is in the server's registered action_verbs.
    // Mounted under the head (mountCaptureBox) so it sits on top + the expand-to-modal transplants
    // it — like the Tray dump / Keep add-note. Mounted EARLY so it shows even on an empty panel
    // (compose the first message). Double-gated + NO THEATER: a read-only serve registers no verbs
    // → action_verbs is absent → no box, even if the panel still declares an action.
    const act = data.action;
    if (act && commitAction && act.verb) {
      const spec = (view.action_verbs || {})[act.verb];
      // A text panel whose one verb replaces its text (new_body) is read first and edited on request: the text shows, an
      // "edit" button opens the editor holding that same text. Any other action keeps its compose box on top.
      const textEdit = spec && data.markdown != null && Array.isArray(act.fields) && act.fields.length === 1 && act.fields[0] && act.fields[0].name === "new_body";
      if (textEdit) mountTextEditor(p, act, spec, String(data.markdown));
      else if (spec) mountCaptureBox(p, buildActionBox(act, spec));
    }
    if (data.note) p.appendChild(richEl("div", "ext-note", data.note));
    // the Decisions panel carries proposal cards whatever rows it holds (three old hand-allows make a card on a panel with
    // nothing pending): decided by panel id, never by the presence of a consent row. The cards lead the rows when there are
    // any (appendExternalLines mounts them); a panel that drew none gets them after the body (renderExternal, below)
    if (entry.id === "decisions") p.dataset.proposalSlot = "";
    // Prose external panels (e.g. the morning-ritual reports) carry a `markdown` string instead of
    // `lines` — render it via the shipped CSP-safe markdown renderer (textContent-only, the same one
    // the section/config panels use). Falls through to the lines model when `markdown` is absent, so
    // the feed panels (inbox/relay/ops/stream) are unaffected.
    if (data.markdown != null) {
      const md = String(data.markdown);
      if (md.trim() === "") { p.appendChild(el("p", "empty", data.empty || "nothing here")); }
      else { p.appendChild(renderMarkdown(md)); }
      return p;
    }
    // A typed panel of the v2 kinds draws itself (cockpit_view.js); the row engine stays here, so a pending's
    // cancel and the per-item expand are the same code whichever component asks for rows.
    if (data.typed && window.LevainCockpitKinds) {
      window.LevainCockpitKinds.fill(p, data, { rows: appendExternalLines, modal: !!(opts && opts.modal), entry: entry });
      return p;
    }
    const lines = Array.isArray(data.lines) ? data.lines : [];
    if (lines.length === 0) {
      p.appendChild(el("p", "empty", data.empty || "nothing here"));
      return p;
    }
    appendExternalLines(p, lines);
    return p;
  }

  // An agent waiting on the operator (Q6a). Read-only: the page shows what is asked and how to answer it from a
  // terminal; nothing here answers. Every agent-sourced string is drawn with textContent, never as HTML.
  function consentLeft(expiresAt) {
    const ms = Date.parse(expiresAt) - Date.now();
    if (!Number.isFinite(ms) || ms <= 0) return "expired";
    // "11 h 31 m left", "52 m left", "40 s left": whole units the eye reads at a glance, one formatter for every place a clock shows
    const t = Math.floor(ms / 1000), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
    return (h ? h + " h " + String(m).padStart(2, "0") + " m" : m ? m + " m" : t + " s") + " left";
  }
  let consentTicker = null;
  function tickConsent() {
    const nodes = document.querySelectorAll("[data-consent-expires]");
    if (!nodes.length && consentTicker) { window.clearInterval(consentTicker); consentTicker = null; return; }
    for (const n of nodes) n.textContent = (n.dataset.consentPrefix || "") + consentLeft(n.dataset.consentExpires);
  }
  function copyText(text, codeEl, btn) {
    const done = (msg) => { btn.textContent = msg; setTimeout(() => { btn.textContent = btn.dataset.label; }, 1500); };
    const select = () => {   // the fallback: the command selected in place, ready for the system copy
      const sel = window.getSelection && window.getSelection();
      if (sel && document.createRange) { const r = document.createRange(); r.selectNodeContents(codeEl); sel.removeAllRanges(); sel.addRange(r); }
      done("selected");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => done("copied"), select);
    else select();
  }
  // The request itself (what is asked, where, the answer commands) is not in the token-free panel: a PENDING
  // request's body is fetched from /cockpit/consent/<id>.json with the token this page holds, on every bind (a
  // decided request shows its state word and times from the panel row and fetches nothing). A body never changes
  // while its request keeps one state, so one fetch per request serves every redraw. The server answers a read
  // it has no free worker for with 503: at most two fetches run at once, and a 503 is retried (~500 ms apart, three
  // tries in all) before the row says it could not be read.
  const consentBodies = new Map();
  const CONSENT_NO_TOKEN = "this page carries no token, so request details are not shown";
  const CONSENT_BAD_TOKEN = "this page's token was not accepted, so request details are not shown";
  const CONSENT_FETCH_MAX = 2, CONSENT_TRIES = 3, CONSENT_RETRY_MS = 500, CONSENT_FETCH_TIMEOUT_MS = 15000;
  let consentActive = 0;
  let consentGen = 0;   // closeModal bumps it: a job queued before a close whose row has left the page is not fetched
  const consentWaiting = [];
  // one read with a deadline: the abort stays armed until onRes (which reads the body) settles, so a server that
  // stalls before the headers or mid-body has the connection itself cancelled, not only the queue slot freed.
  // Without AbortController the read cannot be cancelled; consentQueued's race still frees the slot.
  function fetchBounded(url, opts, onRes, onErr) {
    if (typeof AbortController !== "function") return fetch(url, opts).then(onRes, onErr);
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), CONSENT_FETCH_TIMEOUT_MS);
    return fetch(url, Object.assign({}, opts, { signal: ac.signal })).then(onRes, onErr)
      .finally(() => clearTimeout(timer));
  }
  function consentPump() {
    while (consentActive < CONSENT_FETCH_MAX && consentWaiting.length) {
      const job = consentWaiting.shift();
      consentActive++;
      const release = () => { consentActive--; consentPump(); };
      job().then(release, release);
    }
  }
  function consentQueued(job, badError, row) {
    const bad = { error: badError || "the request could not be read" };
    const gen = consentGen;
    return new Promise((resolve) => {
      consentWaiting.push(() => {
        if (row && gen !== consentGen && !row.isConnected) {   // its view closed; a board row stays and is read
          resolve({ error: "the view closed before this was read" });
          return Promise.resolve();
        }
        let p;
        try { p = Promise.resolve(job()); } catch (e) { p = Promise.resolve(bad); }
        // the whole job (the response and its body) races the deadline: a server that stalls mid-body frees the slot
        const late = new Promise((r) => setTimeout(() => r({ error: (badError || "the request could not be read") + " (timed out)" }),
                                                   CONSENT_FETCH_TIMEOUT_MS + 1000));
        return Promise.race([p.catch(() => bad), late]).then(resolve);   // every job settles; the pump releases its slot
      });
      consentPump();
    });
  }
  function consentFetch(c, headers, tryNo) {
    return fetchBounded("/cockpit/consent/" + encodeURIComponent(c.id) + ".json", { cache: "no-store", headers: headers },
      (res) => {
        if (res.status === 403) return { error: CONSENT_BAD_TOKEN };
        if (res.status === 404) return { error: "this request is no longer listed" };
        if (res.status === 503 && tryNo < CONSENT_TRIES) {
          return new Promise((r) => setTimeout(r, CONSENT_RETRY_MS)).then(() => consentFetch(c, headers, tryNo + 1));
        }
        if (!res.ok) return { error: "the request could not be read (HTTP " + res.status + ")" };
        return res.json().then((b) => (b && typeof b === "object" ? b : { error: "the request could not be read" }),
                               () => ({ error: "the request could not be read" }));
      },
      () => ({ error: "the request could not be read (network)" }));
  }
  // An egress card's body is the exception: what it became (egress_state) moves on while its queue row keeps one
  // state (allowed, then in flight, then completed), so a body is kept only once its egress_state is final.
  function consentBody(c, row) {
    const key = c.id + "|" + c.state;
    if (consentBodies.has(key)) return consentBodies.get(key);
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ error: CONSENT_NO_TOKEN });
    const got = consentQueued(() => consentFetch(c, headers, 1), undefined, row)
      .then((b) => { if ((b.error || egressUnsettled(b)) && consentBodies.get(key) === got) consentBodies.delete(key); return b; });
    consentBodies.set(key, got);
    return got;
  }
  // Q12 / Q16: the hint box (what a shell command looks like; text only, and a hint informs, never authorises) and the two
  // digests the terminal prints before signing (short, the whole value one click away). Shared by the row and the strip's card.
  function consentHintBox(b) {
    const hints = Array.isArray(b.hints) ? b.hints.filter((h) => typeof h === "string" && h) : [];
    if (!hints.length) return null;
    const box = el("div", "consent-facts consent-hint");
    box.appendChild(el("span", "ext-meta", "hint (a few patterns only; informs, never authorises)"));
    for (const h of hints) box.appendChild(el("div", h === "no pattern matched: read the command" ? "consent-dgtext consent-none" : "consent-dgtext", "\u2022 " + h));
    return box;
  }
  function consentDigestLines(b) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const parts = [];
    for (const [label, value] of [["input digest", b.tool_input_digest], ["request digest", b.digest]]) {
      const full = sv(value);
      if (!full) {                                 // a missing digest is shown as missing, never dropped
        const gone = el("div", "consent-facts consent-dg");
        gone.append(el("span", "ext-meta", label + ": "), el("code", "consent-dgval", "(none)"));
        parts.push(gone);
        continue;
      }
      const at = full.indexOf(":") + 1;
      const short = full.length > at + 12 ? full.slice(0, at + 12) + "\u2026" : full;
      const line = el("div", "consent-facts consent-dg");
      const code = el("code", "consent-dgval", short);
      code.tabIndex = 0;
      code.setAttribute("role", "button");
      code.title = full;
      let open = false;
      const flip = () => { open = !open; code.textContent = open ? full : short; };
      code.addEventListener("click", flip);
      code.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
      line.append(el("span", "ext-meta", label), code);
      parts.push(line);
    }
    return parts;
  }
  // A chat launch's egress card (tool levain_egress, levain/chat_egress/cards.py). The server seals its fields in a
  // fixed order, the short ones that decide the answer first and the path last, every agent-controlled character
  // already shown as a marker: it is drawn as rows in that order (never one sentence, never a scrolled <pre> a long
  // path can push the removed names out of), and with what the request became. Text only, as every store string.
  const EGRESS_TOOL = "levain_egress";
  const EGRESS_LABEL = { method: "method", scheme: "scheme", host: "host", port: "port", reattached: "Levain adds your key for",
    stripped: "removed", why: "why it asks", hold: "held", body_len: "body bytes", body_sha256: "body", header_names: "header names",
    query_keys: "query names", path: "path", path_chars: "whole path length", path_sha256: "whole path", launch: "launch",
    request: "request" };
  // the words for chat_egress.cards.CardState. A body carrying `egress_state` (card_state over the request's egress
  // rows, which the server reads) is drawn as that; a body without one is drawn from the queue's state alone
  const EGRESS_WORDS = {
    pending: "waiting on your answer; nothing is sent unless you allow it",
    allowed: "you allowed it; no send is recorded yet",
    in_flight: "you allowed it; Levain is forwarding it and has no result yet",
    completed: "you allowed it and it was sent",
    denied: "you denied it; nothing was sent",
    expired: "the hold ran out with no answer; nothing was sent",
    denied_or_expired: "denied or expired; nothing was sent",
    ended: "ended before a result: the request's client left, or its launch or Levain closed; nothing was sent after that",
    ambiguous: "you allowed it, then Levain or its launch stopped before the result: it may or may not have been sent",
    failed: "Levain failed partway: its record of this request is incomplete",
    inconsistent: "the queue and the egress record disagree",
    unreadable: "the egress record could not be read: whether anything was sent is not known" };
  // the states a card stays in: a body showing one of these is kept (consentBody); the rest are read again
  const EGRESS_FINAL = new Set(["completed", "denied", "expired", "denied_or_expired", "ended", "ambiguous", "failed"]);
  // with no egress rows read, the queue's state alone (cards.card_state(queue_state, None), pinned by its test)
  const EGRESS_FROM_QUEUE = { pending: "pending", allowed: "allowed", denied: "denied", expired: "expired", withdrawn: "ended" };
  // levain.unshowable's own definition (its categories, Default_Ignorable_Code_Point, a space other than U+0020 and
  // the characters that draw nothing), in the browser's Unicode, parity-tested against it; its context rules (a mark
  // with no base, a selector) stay the server's, which marks every one before the card is filed
  const EGRESS_HIDDEN = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}\p{Co}\p{Cn}\p{Default_Ignorable_Code_Point}\u2800\u{16FE4}\u{1D159}]|(?! )\p{Zs}/u;
  const egressLabel = (s) => s.replace(/_/g, " ").toUpperCase();
  function egressUnsettled(b) {
    return !!b && b.tool === EGRESS_TOOL && !EGRESS_FINAL.has(typeof b.egress_state === "string" ? b.egress_state : "");
  }
  function egressParts(b) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const t = sv(b.display);
    if (sv(b.tool) !== EGRESS_TOOL || !t.startsWith(EGRESS_TOOL + " ")) return null;
    let o;
    try { o = JSON.parse(t.slice(EGRESS_TOOL.length + 1)); } catch (e) { return null; }
    if (!o || typeof o !== "object" || Array.isArray(o)) return null;
    const dl = el("dl", "consent-kv egress-kv");
    for (const [k, v] of Object.entries(o)) {
      const shown = v === null ? "none" : Array.isArray(v) ? (v.length ? v.map(String).join(", ") : "none") : String(v);
      if (EGRESS_HIDDEN.test(shown)) return null;  // the server marks these; a raw one means this is not a card it built
      dl.appendChild(el("dt", null, Object.prototype.hasOwnProperty.call(EGRESS_LABEL, k) ? EGRESS_LABEL[k] : k));
      dl.appendChild(el("dd", null, shown));
    }
    const s = Object.prototype.hasOwnProperty.call(EGRESS_WORDS, sv(b.egress_state)) ? sv(b.egress_state)
      : (Object.prototype.hasOwnProperty.call(EGRESS_FROM_QUEUE, sv(b.state)) ? EGRESS_FROM_QUEUE[sv(b.state)] : "inconsistent");
    const state = el("div", "consent-facts egress-state");
    state.dataset.egressState = s;
    state.append(el("b", null, egressLabel(s)), el("span", "ext-meta", EGRESS_WORDS[s]));
    if (sv(b.egress_note)) state.append(el("span", "ext-meta egress-note", sv(b.egress_note)));
    return { rows: dl, state: state, label: egressLabel(s) };   // the audit slot is one nowrap line: the label; the words are the state line
  }
  function drawConsentBody(row, slot, b, pending) {
    const sv = (x) => (typeof x === "string" ? x : "");
    if (b.error) { slot.replaceChildren(el("div", "consent-note ext-meta", b.error)); return; }
    const egress = egressParts(b);
    const parts = egress ? [egress.state, egress.rows] : [el("pre", "consent-display", sv(b.display))];
    const facts = el("div", "consent-facts");
    facts.appendChild(el("span", "ext-meta", "cwd " + sv(b.cwd)));
    facts.appendChild(el("span", "ext-meta", "targets " + sv(b.targets)));
    parts.push(facts);
    const hintBox = consentHintBox(b);
    if (hintBox) parts.push(hintBox);
    parts.push(...consentDigestLines(b));
    if (sv(b.note)) parts.push(el("div", "consent-note ext-meta", sv(b.note)));
    if (!pending && (egress || sv(b.audit))) {
      const audit = row.querySelector(".consent-audit");
      if (audit) audit.textContent = egress ? egress.label : sv(b.audit);
    }
    if (pending) {
      for (const [label, cmd] of [["allow", sv(b.answer_allow)], ["deny", sv(b.answer_deny)]]) {
        if (!cmd) continue;
        const line = el("div", "consent-cmd");
        const code = el("code", "consent-code", cmd);
        const btn = el("button", "undo-btn consent-copy", "copy " + label);
        btn.type = "button";
        btn.dataset.label = btn.textContent;
        btn.title = "copy the " + label + " command; run it in a terminal with your signing key";
        btn.addEventListener("click", () => copyText(cmd, code, btn));
        line.append(code, btn);
        parts.push(line);
      }
    }
    // OS-2: where this cockpit answers (pending) or rules (undecided) the request with a passkey, the sheet does it
    if (b.sheet === true && typeof b.id === "string" && b.id) {
      // no answer a passkey may sign here (none enrolled, or a ported page): the sheet says why, not "with your passkey"
      const can = Array.isArray(b.sheet_choices) && b.sheet_choices.length > 0;
      const btn = el("button", "undo-btn consent-sheet-open", !can ? "why no passkey answers this here"
        : pending ? "answer with your passkey" : "rule with your passkey");
      btn.type = "button";
      btn.dataset.consentSheet = pending ? "answer" : "rule";
      btn.title = "open the sign sheet for this request";
      btn.addEventListener("click", () => openConsentSheet(b.id, sv(b.sheet_v)));
      parts.push(btn);
    }
    slot.replaceChildren(...parts);
  }
  function consentRow(o) {
    const c = o.consent;
    const pending = c.state === "pending";
    // undecided: the hold ended with no answer and the call did not run; it waits on a ruling, so it is not drawn as answered
    const undecided = c.state === "undecided";
    const row = el("div", "row consent-row" + (pending || undecided ? " ext-accent" : " ext-dim"));
    row.setAttribute("data-consent-row", "");
    if (c.id) row.dataset.card = "consent:" + c.id;     // the id the topology's waiting tasks name (topology_view.js)
    const head = el("div", "consent-head");
    head.appendChild(el("span", "consent-agent", c.agent || "agent"));
    head.appendChild(el("b", "consent-tool", c.tool));
    if (c.tier) head.appendChild(el("span", "ext-meta", c.tier));
    if (pending) {
      const left = el("span", "consent-left", consentLeft(c.expiresAt));
      left.dataset.consentExpires = c.expiresAt;
      head.appendChild(left);
      if (!consentTicker && typeof window.setInterval === "function") consentTicker = window.setInterval(tickConsent, 1000);
    } else {
      // an egress card's header says what the card became: the listing's egress_state (the server's reading of the
      // rows), else, when the row carries none, the queue's state alone
      const word = c.tool === EGRESS_TOOL
        ? egressLabel(typeof c.egressState === "string" && Object.prototype.hasOwnProperty.call(EGRESS_WORDS, c.egressState) ? c.egressState
          : Object.prototype.hasOwnProperty.call(EGRESS_FROM_QUEUE, c.state) ? EGRESS_FROM_QUEUE[c.state] : "inconsistent")
        : c.state;
      head.appendChild(el("span", "consent-audit", word));
    }
    if (c.age) head.appendChild(el("span", "ext-meta", "filed " + c.age));
    if (!pending && c.decidedAge) {
      const when = c.state === "expired" ? "expired " : undecided ? "undecided " : "decided ";
      head.appendChild(el("span", "ext-meta", when + c.decidedAge + (undecided ? ", waits on a ruling" : "")));
    }
    row.appendChild(head);
    if (!pending) {
      // a decided request fetches nothing on draw; "details" fetches its body (the audit line) through the same queue
      if (!c.id) return row;
      const btn = el("button", "undo-btn consent-details", "details");
      btn.type = "button";
      btn.title = "show how this request was answered";
      let slot = null;  // one slot per row: a retry after a failed read reuses it rather than stacking another
      btn.addEventListener("click", () => {
        btn.disabled = true;
        if (!slot) { slot = el("div", "consent-body ext-meta"); row.appendChild(slot); }
        slot.replaceChildren(document.createTextNode("reading the request\u2026"));
        consentBody(c, row).then((b) => {
          drawConsentBody(row, slot, b, false);
          if (b.error) btn.disabled = false;
          else btn.remove();
        });
      });
      head.appendChild(btn);
      return row;
    }
    const slot = el("div", "consent-body ext-meta", "reading the request\u2026");
    row.appendChild(slot);
    if (c.id) consentBody(c, row).then((b) => drawConsentBody(row, slot, b, pending));
    else slot.textContent = "this request has no id";
    return row;
  }

  // An agent's question to the operator (Q-2b). The panel row carries only who asked, when, how many options and whether
  // it is answered: the question, the option labels and what it blocks are the agent's words, so they are fetched from
  // /cockpit/question/<n>.json with the token this page holds (the consent body's gate) when the row is opened. With the
  // write token and an offered verb the body shows one button per option and a words box; without them it shows the CLI
  // command to copy. Every string is drawn with textContent, never as HTML; an answer authorises nothing (ruling 26a).
  const questionBodies = new Map();
  // a refused send's words, by question id: a stale refusal empties the box so the owed re-read is not held off by it,
  // and a redraw that still shows the question open gives the words back
  const questionDrafts = new Map();
  function questionFetch(q, headers, tryNo) {
    return fetchBounded("/cockpit/question/" + encodeURIComponent(String(q.n)) + ".json", { cache: "no-store", headers: headers },
      (res) => {
        if (res.status === 403) return { error: CONSENT_BAD_TOKEN };
        if (res.status === 404) return { error: "this question is no longer listed" };
        if (res.status === 503 && tryNo < CONSENT_TRIES) {
          return new Promise((r) => setTimeout(r, CONSENT_RETRY_MS)).then(() => questionFetch(q, headers, tryNo + 1));
        }
        if (!res.ok) return { error: "the question could not be read (HTTP " + res.status + ")" };
        return res.json().then((b) => (b && typeof b === "object" ? b : { error: "the question could not be read" }),
                               () => ({ error: "the question could not be read" }));
      },
      () => ({ error: "the question could not be read (network)" }));
  }
  function questionBody(q, row) {
    const key = q.n + "|" + q.state;
    if (questionBodies.has(key)) return questionBodies.get(key);
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ error: CONSENT_NO_TOKEN, noToken: true });
    const got = consentQueued(() => questionFetch(q, headers, 1), "the question could not be read", row)
      .then((b) => { if (b.error && questionBodies.get(key) === got) questionBodies.delete(key); return b; });
    questionBodies.set(key, got);
    return got;
  }
  function copyLine(cmd, label) {
    const line = el("div", "consent-cmd");
    const code = el("code", "consent-code", cmd);
    const btn = el("button", "undo-btn consent-copy", "copy " + label);
    btn.type = "button";
    btn.dataset.label = btn.textContent;
    btn.addEventListener("click", () => copyText(cmd, code, btn));
    line.append(code, btn);
    return line;
  }
  // flow's provider puts the whole body into `blocks`, and the body starts with the title sentence: what `blocks` adds beyond
  // the title is the part after it (trimmed, a trailing "?" or "." on the title ignored). The provider also prefixes the title with
  // the asking repo ("flow: ..."), which the body does not carry, so the title is tried with that one-word prefix dropped too.
  // "" when nothing is left.
  function blocksBeyondTitle(title, blocks) {
    const t = String(blocks || "").trim();
    const full = String(title || "").trim();
    for (const cand of [full, full.replace(/^[^\s:]{1,40}:\s+/, "")]) {
      const w = cand.replace(/[?.\s]+$/, "");
      if (w && t.startsWith(w)) return t.slice(w.length).replace(/^[?.\s]+/, "");
    }
    return t;
  }
  // `card` (the Needs-you strip's card) draws the mock's anatomy: the question as a bold title, "blocks: ...", then "REC key. words";
  // the options, the words box and the send button go to the card's own bottom row, so cards in a row end their actions together.
  function drawQuestionBody(q, slot, b, card) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const open = q.state === "open";
    const cli = "levain question show " + q.n;   // the options are listed there; answer with: levain question answer N --option KEY
    if (b.error) {
      const parts = [el("div", "consent-note ext-meta", b.error)];
      if (open) parts.push(copyLine(cli, "show command"));   // no token: the terminal route still answers
      slot.replaceChildren(...parts);
      return;
    }
    const options = Array.isArray(b.options) ? b.options : [];
    const parts = [];
    if (card) {
      parts.push(el("div", "needs-q-title", sv(b.what)));
      const more = blocksBeyondTitle(sv(b.what), sv(b.blocks));
      if (more) parts.push(el("div", "ext-meta needs-q-blocks", "blocks: " + more));
      const recOpt = options.find((o) => o && sv(o.key) === sv(b.rec));
      if (sv(b.rec)) {
        const rl = el("div", "needs-rec");
        rl.append(el("span", "needs-rec-tag", "REC"), document.createTextNode(" " + sv(b.rec) + (recOpt && sv(recOpt.label) ? ". " + sv(recOpt.label) : "")));
        parts.push(rl);
      }
    } else {
      parts.push(el("pre", "consent-display", sv(b.what)));
      const facts = el("div", "consent-facts");
      facts.appendChild(el("span", "ext-meta", "recommends " + sv(b.rec)));
      facts.appendChild(el("span", "ext-meta", "blocks: " + sv(b.blocks)));
      parts.push(facts);
    }
    const a = b.answer && typeof b.answer === "object" ? b.answer : null;
    if (a) {
      const opt = options.find((o) => o && o.key === a.value);
      parts.push(el("div", "consent-note ext-meta", "answered via " + sv(a.channel) + (a.kind === "option"
        ? ": option " + sv(a.value) + (opt ? " (" + sv(opt.label) + ")" : "") : ", in words: " + sv(a.value))));
      slot.replaceChildren(...parts);
      return;
    }
    if (open && q.sign) {
      // answered by a signature (DESIGN_SIGN D5): the sign request's own options are the buttons, and no unsigned answer is
      // drawn beside them (an unsigned tap on a row its caller asked to be signed is refused there, never his word)
      const facet = signFacet(q.sign);
      slot.replaceChildren(...parts);
      if (card) { const bottom = el("div", "needs-actions needs-qa needs-sign"); bottom.appendChild(facet); card.appendChild(bottom); }
      else slot.appendChild(facet);
      return;
    }
    if (!(open && q.offered && answerQuestion)) {
      for (const o of options) parts.push(el("div", "ext-meta", sv(o && o.key) + " — " + sv(o && o.label)));
      if (open) parts.push(copyLine(cli, "show command"));
      slot.replaceChildren(...parts);
      return;
    }
    const wrap = el("div", "verb-actions question-answer");
    const controls = [];
    const msg = el("span", "edit-msg", "");
    const send = async (params) => {
      for (const c of controls) c.disabled = true;
      let res;
      try { res = await answerQuestion(q.id, params); } catch (e) { res = { ok: false, message: "the answer could not be sent" }; }
      if (res && res.ok) { words.value = ""; return; }   // the board re-reads and redraws this row as answered
      const kept = res && res.error === "stale" && words.value.trim() !== "";
      if (kept) { questionDrafts.set(q.id, words.value); words.value = ""; }
      for (const c of controls) c.disabled = false;
      msg.textContent = ((res && (res.message || res.error)) || "failed") +
        (kept ? " (your words are kept: they come back here while the question is still open)" : "");
      if (kept) document.dispatchEvent(new CustomEvent("levain:editor-closed"));   // the re-read the refusal owes may land now
    };
    for (const o of options) {
      const key = sv(o && o.key);
      const btn = el("button", "undo-btn question-option", key + " · " + sv(o && o.label) + (!card && key === sv(b.rec) ? " (recommended)" : ""));
      if (key === sv(b.rec)) btn.classList.add("question-rec");
      btn.type = "button";
      btn.title = "answer with option " + key;
      btn.addEventListener("click", () => send({ option: key }));
      controls.push(btn);
      wrap.appendChild(btn);
    }
    const words = el("textarea", "question-words");
    words.rows = 2;
    words.maxLength = 4000;
    words.placeholder = card ? "or your own words" : "or answer in your own words";
    if (questionDrafts.has(q.id)) { words.value = questionDrafts.get(q.id); questionDrafts.delete(q.id); }
    const sendWords = el("button", "undo-btn question-send", "send words");
    sendWords.type = "button";
    sendWords.addEventListener("click", () => { if (words.value.trim()) send({ text: words.value }); });
    controls.push(words, sendWords);
    if (card) {
      const bottom = el("div", "needs-actions needs-qa");
      bottom.append(wrap, words, sendWords, msg);
      slot.replaceChildren(...parts);
      card.appendChild(bottom);
      return;
    }
    parts.push(wrap, words, sendWords, msg);
    slot.replaceChildren(...parts);
  }
  function questionRow(o) {
    const q = o.question;
    const open = q.state === "open";
    const row = el("div", "row consent-row question-row" + (open ? " ext-accent" : " ext-dim"));
    row.setAttribute("data-question-row", "");
    if (q.id) row.dataset.card = String(q.id);
    const head = el("div", "consent-head");
    head.appendChild(el("span", "consent-agent", q.agent || "agent"));
    head.appendChild(el("b", "consent-tool", "question #" + q.n));
    head.appendChild(el("span", "ext-meta", q.options + " options"));
    head.appendChild(el("span", "consent-audit", q.state));
    if (q.age) head.appendChild(el("span", "ext-meta", "filed " + q.age));
    if (!open && q.decidedAge) head.appendChild(el("span", "ext-meta", "answered " + q.decidedAge));
    row.appendChild(head);
    const btn = el("button", "undo-btn consent-details", open ? "open" : "details");
    btn.type = "button";
    btn.title = open ? "read the question and answer it" : "show how this question was answered";
    let slot = null;   // one slot per row: a retry after a failed read reuses it
    btn.addEventListener("click", () => {
      btn.disabled = true;
      if (!slot) { slot = el("div", "consent-body ext-meta"); row.appendChild(slot); }
      slot.replaceChildren(document.createTextNode("reading the question…"));
      questionBody(q, row).then((b) => {
        drawQuestionBody(q, slot, b);
        if (b.error) btn.disabled = false; else btn.remove();
      });
    });
    head.appendChild(btn);
    return row;
  }

  // An agent's note: something it tells the operator that needs no answer (Phill 10-22). The row carries who filed it, when and
  // whether it was dismissed; the words are the agent's, fetched from /cockpit/note/<n>.json with the page's token (the question
  // body's gate). A note is drawn apart from everything that decides: no REC, no options, no words box, no "sign" line, no
  // accent; its one control is "dismiss", which tells the agent nothing. The exception is a note whose caller filed a sign
  // request for it (its `sign` facet): that request's own ceremony is drawn under its words (signFacet). Every string is
  // drawn with textContent, never as HTML.
  // keyed `n|state`; renderNotes keeps only the notes the current view lists, and the strip's clearing empties it
  const noteBodies = new Map();
  function pruneNoteBodies(lines) {
    const listed = new Set();
    for (const ln of lines) if (ln && ln.note) listed.add(ln.note.n + "|" + ln.note.state);
    for (const k of Array.from(noteBodies.keys())) if (!listed.has(k)) noteBodies.delete(k);
  }
  function noteFetch(n, headers, tryNo) {
    return fetchBounded("/cockpit/note/" + encodeURIComponent(String(n.n)) + ".json", { cache: "no-store", headers: headers },
      (res) => {
        if (res.status === 403) return { error: CONSENT_BAD_TOKEN };
        if (res.status === 404) return { error: "this note is no longer listed" };
        if (res.status === 503 && tryNo < CONSENT_TRIES) {
          return new Promise((r) => setTimeout(r, CONSENT_RETRY_MS)).then(() => noteFetch(n, headers, tryNo + 1));
        }
        if (!res.ok) return { error: "the note could not be read (HTTP " + res.status + ")" };
        return res.json().then((b) => (b && typeof b === "object" ? b : { error: "the note could not be read" }),
                               () => ({ error: "the note could not be read" }));
      },
      () => ({ error: "the note could not be read (network)" }));
  }
  function noteBody(n, node) {
    const key = n.n + "|" + n.state;
    if (noteBodies.has(key)) return noteBodies.get(key);
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ error: CONSENT_NO_TOKEN, noToken: true });
    const got = consentQueued(() => noteFetch(n, headers, 1), "the note could not be read", node)
      .then((b) => { if (b.error && noteBodies.get(key) === got) noteBodies.delete(key); return b; });
    noteBodies.set(key, got);
    return got;
  }
  function drawNoteBody(n, slot, b) {
    const sv = (x) => (typeof x === "string" ? x : "");
    if (b.error) { slot.replaceChildren(el("div", "consent-note ext-meta", b.error)); return; }
    const parts = [el("div", "note-what", sv(b.what))];
    if (sv(b.detail)) parts.push(el("div", "note-detail", sv(b.detail)));
    const d = b.dismissed && typeof b.dismissed === "object" ? b.dismissed : null;
    if (d) {
      parts.push(el("div", "consent-note ext-meta", "dismissed via " + sv(d.channel)));
    } else if (n.state === "open" && n.offered && dismissNote) {
      const bar = el("div", "note-actions");
      const btn = el("button", "note-dismiss", "dismiss");
      btn.type = "button";
      btn.title = "mark it read: the agent is told nothing";
      const msg = el("span", "edit-msg", "");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        let res;
        try { res = await dismissNote(n.id); } catch (e) { res = { ok: false, message: "the dismiss could not be sent" }; }
        if (res && res.ok) return;   // the board re-reads and the note leaves the strip
        btn.disabled = false;
        msg.textContent = (res && (res.message || res.error)) || "failed";
      });
      bar.append(btn, msg);
      parts.push(bar);
    }
    // a note its caller filed a sign request for (DESIGN_SIGN D5) carries that request's ceremony under its words
    if (!d && n.state === "open" && n.sign) parts.push(signFacet(n.sign));
    slot.replaceChildren(...parts);
  }
  function noteRow(o) {
    const n = o.note;
    const row = el("div", "row note-row" + (n.state === "open" ? "" : " ext-dim"));
    if (n.id) row.dataset.card = String(n.id);
    const head = el("div", "consent-head");
    head.appendChild(el("span", "note-chip", "note"));
    head.appendChild(el("span", "consent-agent", "from " + (n.agent || "agent")));
    if (n.age) head.appendChild(el("span", "ext-meta", "filed " + n.age));
    if (n.state !== "open") head.appendChild(el("span", "ext-meta", "dismissed" + (n.decidedAge ? " " + n.decidedAge : "")));
    row.appendChild(head);
    const btn = el("button", "undo-btn consent-details", n.state === "open" ? "read" : "details");
    btn.type = "button";
    let slot = null;
    btn.addEventListener("click", () => {
      btn.disabled = true;
      if (!slot) { slot = el("div", "consent-body ext-meta"); row.appendChild(slot); }
      slot.replaceChildren(document.createTextNode("reading the note…"));
      noteBody(n, row).then((b) => { drawNoteBody(n, slot, b); if (b.error) btn.disabled = false; else btn.remove(); });
    });
    head.appendChild(btn);
    return row;
  }
  function noteCard(n) {
    const card = el("article", "note-card");
    card.dataset.noteId = String(n.id || "") + "|" + n.offered;   // a card is kept only while what it offers is the same
    const head = el("div", "note-head");
    head.append(el("span", "note-chip", "note"), el("span", "note-from", "from " + (n.agent || "agent")));
    if (n.age) head.appendChild(el("span", "ext-meta", n.age));
    const slot = el("div", "note-body", "reading the note…");
    card.append(head, slot);
    noteBody(n, card).then((b) => drawNoteBody(n, slot, b));
    return card;
  }
  // The Notes strip, under Needs you: the open notes, oldest first, as their own cards. Not part of the Needs-you grid, bar or
  // count: a note waits on nothing and authorises nothing. Absent when there is none.
  function renderNotes(view) {
    const host = document.getElementById("ck-notes");
    if (!host) return;
    const lines = ((view.extra_panels || {}).decisions || {}).lines;
    const all = Array.isArray(lines) ? lines : [];
    const items = all.filter((ln) => ln && ln.note && ln.note.state === "open").map((ln) => ln.note);
    if (!items.length) { clearNeeds(host); noteBodies.clear(); return; }
    pruneNoteBodies(all);
    // the cockpit lists a page of the open notes; `total` is how many are open in all, so the strip says what it left out
    const total = Math.max(items.length, ...items.map((n) => n.total || 0));
    const sig = items.map((n) => n.id + "|" + n.offered).join("\n") + "\n" + total;
    if (host.dataset.sig === sig && !host.hidden) return;
    const head = el("div", "notes-headline");
    head.append(el("h3", null, "Notes"), el("span", "notes-count", String(total)),
                el("span", "notes-gloss", "Agents telling you something. Nothing to answer: read it, then dismiss."));
    if (total > items.length) head.appendChild(el("span", "notes-more ext-meta", (total - items.length) + " more not shown: the oldest are shown first"));
    const kept = new Map();
    const old = host.querySelector(".notes-grid");
    if (old) for (const c of old.children) if (c.dataset && c.dataset.noteId) kept.set(c.dataset.noteId, c);
    const grid = el("div", "notes-grid");
    for (const n of items) grid.appendChild(kept.get(String(n.id) + "|" + n.offered) || noteCard(n));
    host.replaceChildren(head, grid);
    host.hidden = false;
    host.dataset.sig = sig;
  }

  // ---- The sign card (DESIGN_SIGN D5) ----
  // A question or note row whose facet names a sign request (`sign`, the request id) is answered through levain's own
  // sign-request ceremony, hosted by this server for the caller's Signer: /cockpit/sign.json lists the request (its sha256,
  // options, write-in, recommendation), and a tap is /cockpit/sign/<id>/challenge -> navigator.credentials.get -> /answer,
  // ONE gesture. The request's own options are the buttons, since their lines are what is signed; the line shown after a
  // signature is the server's (the challenge's `line`), never the page's copy. This is the phone page's signAnswer
  // behaviour exactly: a cancelled passkey says so and signs nothing, a refusal after the assertion was sent is the server's
  // 4xx only, anything else is "not sure", and a retry is the same button. The page is not a secure context on plain http
  // and its passkey belongs to one https page: anywhere else the card says where to sign and draws no other answer. A
  // signed verb in a compose box (signedCompose) runs the same ceremony on the request the server's compose_signer files.
  const SIGN_USABLE = window.isSecureContext === true && typeof window.PublicKeyCredential === "function"
    && !!(navigator.credentials && navigator.credentials.get);
  const SIGN_NO_TOKEN = "this page carries no token, so the sign request is not shown";
  const SIGN_PICK_CLOSED = "the passkey pick was closed when the card was drawn again; nothing was signed";
  const SIGN_BUSY = "a signature is already being asked for; nothing was signed here";
  const signB64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const signUnb64u = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                            (c) => c.charCodeAt(0));
  // unpadded base64url with a length some bytes encode to; signUnb64u is only ever handed one from the server or storage
  const signDecodable = (t) => typeof t === "string" && /^[A-Za-z0-9_-]+$/.test(t) && t.length % 4 !== 1;
  // a cancelled Face ID and a passkey this device no longer holds both read NotAllowedError: one sentence for both
  const signDeclined = (e, what) => "the passkey did not sign (" + ((e && e.name) || "cancelled") + "); " + what;
  const signWhy = (r) => (r.body && typeof r.body.message === "string" && r.body.message) || ("refused (HTTP " + r.status + ")");
  const signNotes = new Map();     // request id -> [text, kind, sha256]: what the last ceremony said, kept across redraws
  const signWriteIns = new Map();  // request id -> {sha256, words}: a write-in being typed, kept for the version it was typed on
  // request id -> what a card drawn while that request's attempt is in flight must undo when the attempt gives its card back.
  // The focus view builds its card again each time it opens, so a card drawn mid-attempt is frozen (its buttons and words box
  // disabled) and registers here; the attempt's end releases it, and a signed or unknowable one stays frozen
  const signPending = new Map();
  let signBusy = 0;                // a ceremony (or a passkey pick) in progress: a second tap anywhere does nothing
  let signListed = null;           // {at, got}: the last listing, shared by every card of one render
  // which enrolled passkey THIS browser used: fingerprint -> credential id, so the OS prompt asks for that one. Browser
  // storage may be absent or throw; the ceremony works without it (the pick list, or the OS's own list)
  const signMapKey = (rp) => "levain-passkeys:" + rp;
  function signMine(rp) {
    try { const m = JSON.parse(window.localStorage.getItem(signMapKey(rp)) || "{}"); return m && typeof m === "object" ? m : {}; }
    catch (e) { return {}; }
  }
  function signRemember(rp, fp, rawId) {
    try { const m = signMine(rp); m[fp] = rawId; window.localStorage.setItem(signMapKey(rp), JSON.stringify(m)); } catch (e) { /* kept nowhere */ }
  }
  function signForget(rp, fp) {
    try { const m = signMine(rp); delete m[fp]; window.localStorage.setItem(signMapKey(rp), JSON.stringify(m)); } catch (e) { /* kept nowhere */ }
  }
  // after a verified signature: the record names the credential that signed, when it does not already
  function signKeep(rp, fp, cred) {
    if (!cred || !cred.rawId || typeof rp !== "string" || !rp) return;
    const id = signB64u(cred.rawId);
    if (signMine(rp)[fp] !== id) signRemember(rp, fp, id);
  }
  // The passkey prompt for `fp` on rpId `rp` (the challenge's), `pk` without allowCredentials: asks for `rawId`'s passkey
  // when that id decodes, else for none (the OS's own list), never a throw. An id this browser remembered that does not
  // decode is forgotten; one the authenticator refuses (NotAllowedError, which a passkey since removed reads as, and a
  // cancel too) is asked once more with no id, and stays remembered until a passkey signs (signKeep replaces it)
  function signAsk(pk, rp, fp, rawId) {
    const local = !!rawId && typeof rp === "string" && signMine(rp)[fp] === rawId;
    const id = rawId && signDecodable(rawId) ? signUnb64u(rawId) : null;
    if (rawId && !id && local) signForget(rp, fp);
    const ask = (allow) => navigator.credentials.get({ publicKey: Object.assign({}, pk,
      allow ? { allowCredentials: [{ type: "public-key", id: allow }] } : {}) });
    return ask(id).catch((e) => {
      if (!(e && e.name === "NotAllowedError" && id && local)) throw e;
      return ask(null);
    });
  }
  function signHeaders(post) {
    const t = window.LevainCockpitTransport;
    const h = Object.assign({}, t && typeof t.headers === "function" ? t.headers() : {});
    if (post) h["Content-Type"] = "application/json";
    return h;
  }
  // {ok, status, body}; status 0 when the server could not be reached
  function signCall(route, body) {
    return fetchBounded(route, { method: "POST", cache: "no-store", headers: signHeaders(true), body: JSON.stringify(body) },
      (res) => res.json().then((j) => ({ ok: res.ok, status: res.status, body: j && typeof j === "object" ? j : {} }),
                               () => ({ ok: false, status: res.status, body: {} })),
      () => ({ ok: false, status: 0, body: { message: "the cockpit could not be reached" } }));
  }
  // the listing: {requests, signers, origin, note} or {error}. One read serves every card drawn within a second or two;
  // `fresh` reads again (after a ceremony, and before a compose)
  function signListing(fresh) {
    if (!fresh && signListed && Date.now() - signListed.at < 2000) return signListed.got;
    const headers = signHeaders(false);
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ error: SIGN_NO_TOKEN });
    const got = fetchBounded("/cockpit/sign.json", { cache: "no-store", headers: headers }, (res) => {
      if (res.status === 403) return { error: "this page's token was not accepted, so the sign request is not shown" };
      if (res.status === 404) return { error: "this cockpit hosts no signing" };
      if (!res.ok) return { error: "the sign requests could not be read (HTTP " + res.status + ")" };
      return res.json().then((b) => (b && typeof b === "object" && Array.isArray(b.requests) ? b : { error: "the sign requests could not be read" }),
                             () => ({ error: "the sign requests could not be read" }));
    }, () => ({ error: "the sign requests could not be read (network)" }));
    signListed = { at: Date.now(), got: got };
    got.then((b) => { if (b.error && signListed && signListed.got === got) signListed = null; });
    return got;
  }
  // ONE request by its id, whatever the listing's cap: {request, signers, origin, note}, {request: null} when it is not
  // open (answered, denied, expired or gone), or {error}
  function signOne(id) {
    const headers = signHeaders(false);
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ error: SIGN_NO_TOKEN });
    return fetchBounded("/cockpit/sign/" + encodeURIComponent(id) + ".json", { cache: "no-store", headers: headers }, (res) => {
      // the route answers `not_open` for a request it read and found closed; a 404 without it is no signing at all
      if (res.status === 404) return res.json().then((b) => (b && b.error === "not_open" ? { request: null } : { error: "this cockpit hosts no signing" }),
                                                     () => ({ error: "this cockpit hosts no signing" }));
      if (res.status === 403) return { error: "this page's token was not accepted, so the sign request is not shown" };
      if (!res.ok) return { error: "the sign request could not be read (HTTP " + res.status + ")" };
      return res.json().then((b) => (b && typeof b === "object" && b.request && typeof b.request === "object" ? b : { error: "the sign request could not be read" }),
                             () => ({ error: "the sign request could not be read" }));
    }, () => ({ error: "the sign request could not be read (network)" }));
  }
  // the one page this cockpit signs on, or "" when the listing names none; a page anywhere else cannot run the ceremony
  const signElsewhere = (listing) => (!SIGN_USABLE || typeof listing.origin !== "string" || listing.origin !== window.location.origin);
  const signWhere = (listing) => "sign on " + (typeof listing.origin === "string" && listing.origin ? listing.origin : "the cockpit's https page")
    + ": this page cannot use your passkey";

  // One ceremony on listing row `r` with passkey `fp`: resolves {done: "signed"|"unsigned"|"unsure", line}. Only a definite
  // "nothing was signed" is "unsigned": a 4xx from the challenge or the answer, or anything before the answer was sent.
  function signCeremony(r, fp, rawId, choice, words, say) {
    const base = "/cockpit/sign/" + encodeURIComponent(r.id) + "/";
    const body = { signer_fp: fp, request_sha256: r.sha256 };
    if (choice !== undefined) body.choice = choice;
    if (words !== undefined) body.write_in = words;
    let sent = false, line;
    say(choice !== undefined ? "asking for the challenge for (" + choice + ")…" : "asking for the challenge…");
    return signCall(base + "challenge", body).then((c) => {
      if (!c.ok) { say(signWhy(c), "bad"); return { done: "unsigned" }; }
      if (!c.body || !signDecodable(c.body.challenge)) { say("the cockpit sent a malformed challenge; nothing was signed", "bad"); return { done: "unsigned" }; }
      // the exact line the signature appends, from the server's bytes: what is shown is what is signed
      if (typeof c.body.line === "string") line = c.body.line;
      say("confirm with your passkey to sign…" + (line ? " " + line : ""));
      const pk = { challenge: signUnb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      return signAsk(pk, c.body.rp_id, fp, rawId).then((cred) => {
        say("verifying the signature…");
        sent = true;
        // raw_id: the credential that signed, which the cockpit records as this key's once the signature verifies
        const answer = { authenticator_data: signB64u(cred.response.authenticatorData),
          client_data_json: signB64u(cred.response.clientDataJSON), signature: signB64u(cred.response.signature) };
        if (cred.rawId) answer.raw_id = signB64u(cred.rawId);
        return signCall(base + "answer", answer).then((a) => {
          if (!a.ok && a.status >= 400 && a.status < 500) { say("not signed: " + signWhy(a), "bad"); return { done: "unsigned" }; }
          if (!a.ok) { say("not sure this was signed (" + signWhy(a) + "): check the list", "bad"); return { done: "unsure" }; }
          signKeep(c.body.rp_id, fp, cred);
          // the reply's fields where it names them, else what was sent; a part neither names is left out
          const chose = typeof a.body.choice === "string" && a.body.choice ? a.body.choice : choice;
          const by = typeof a.body.signer_fp === "string" && a.body.signer_fp ? a.body.signer_fp : fp;
          const ns = typeof a.body.namespace === "string" && a.body.namespace ? " in " + a.body.namespace : "";
          say("signed" + (chose ? " (" + chose + ")" : "") + " · by " + by + ns + (line ? ": " + line : ""), "ok");
          return { done: "signed", line: line };
        });
      }, (e) => { say(signDeclined(e, "nothing was signed"), "bad"); return { done: "unsigned" }; });
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) { say("not sure this was signed (the page failed: " + name + "): check the list", "bad"); return { done: "unsure" }; }
      say("the page failed (" + name + "); nothing was signed", "bad");
      return { done: "unsigned" };
    });
  }
  // Which passkey signs: the one this browser used before, else the only one enrolled; with several and no record, the
  // person picks (buttons put into `host`, the phone's pick list). The credential id is this browser's own record, else
  // the one the listing's `credentials` names (display facts the cockpit holds, so a fresh browser asks for that passkey
  // at once), else none (the OS's own list). Resolves {fp, rawId}, or null when the pick was dismissed
  // by its dismiss button; rejects (an AbortError) when a redraw took `host` off the page with the pick still open, so the
  // caller's `signBusy` is released on every path. The pick holds `signBusy` like a ceremony.
  function signWho(listing, host, say) {
    const signers = Array.isArray(listing.signers) ? listing.signers : [];
    const rp = (listing.origin || "").replace(/^https?:\/\//, "").replace(/:\d+$/, "");
    const local = signMine(rp);
    // an entry that cannot be an id (empty, null, not text) names no passkey: it is dropped before it decides who signs
    for (const k of Object.keys(local)) if (!signDecodable(local[k])) { signForget(rp, k); delete local[k]; }
    const held = listing.credentials && typeof listing.credentials === "object" ? listing.credentials : {};
    const idOf = (fp) => (Object.prototype.hasOwnProperty.call(local, fp) ? local[fp]
      : (Object.prototype.hasOwnProperty.call(held, fp) && held[fp] && typeof held[fp].credential_id === "string"
        ? held[fp].credential_id : null));
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    if (known.length === 1) return Promise.resolve({ fp: known[0], rawId: local[known[0]] });
    if (!known.length && signers.length === 1) return Promise.resolve({ fp: signers[0], rawId: idOf(signers[0]) });
    const choices = known.length ? known : signers;
    say("more than one passkey is enrolled for this name; pick the one this device holds:");
    return new Promise((resolve, reject) => {
      const picks = [];
      // a panel refetch that replaces the card detaches these buttons, and nothing could ever press them again
      const watch = setInterval(() => {
        if (host.isConnected) return;
        end();
        const e = new Error("the passkey pick was closed when the card was drawn again");
        e.name = "AbortError";
        reject(e);
      }, 250);
      const end = () => { clearInterval(watch); picks.forEach((x) => x.remove()); };
      for (const fp of choices) {
        const p = el("button", "undo-btn sign-pick", fp.slice(0, 18) + "…");
        p.type = "button";
        p.title = fp;
        p.addEventListener("click", () => { end(); resolve({ fp: fp, rawId: idOf(fp) }); });
        picks.push(p);
        host.appendChild(p);
      }
      const no = el("button", "undo-btn sign-pick", "dismiss");
      no.type = "button";
      no.addEventListener("click", () => { end(); say("no passkey was picked; nothing was signed", "bad"); resolve(null); });
      picks.push(no);
      host.appendChild(no);
    });
  }
  // the card's part for request `id`: a note line, then the request's own buttons, filled once the listing answers
  function signFacet(id) {
    const wrap = el("div", "sign-facet");
    wrap.dataset.signRequest = id;
    const note = el("div", "sign-note ext-meta", "reading the sign request…");
    wrap.appendChild(note);
    const show = (text, kind) => { note.textContent = text; note.className = "sign-note ext-meta" + (kind ? " sign-" + kind : ""); };
    signListing(false).then(async (listing) => {
      if (listing.error) return show(listing.error, "bad");
      if (signElsewhere(listing)) return show(signWhere(listing), "bad");
      // a listing stops at its cap: a request it does not name is read by its own id before it is called closed
      const listed = listing.requests.find((x) => x && x.id === id);
      const one = listed ? { request: listed } : await signOne(id);
      if (one.error) return show(one.error, "bad");
      const r = one.request;
      const kept = signNotes.get(id);
      if (!r) {
        // answered here: its signed line stays until the row itself leaves; else it was answered, denied or expired elsewhere
        return show(kept && kept[1] === "ok" ? kept[0] : "this sign request is no longer open; the card updates on the next read", kept && kept[1]);
      }
      if (kept && kept[2] !== (r.sha256 || "")) signNotes.delete(id);
      if (r.refused) return show("not signable: " + r.refused, "bad");
      const say = (text, kind) => { signNotes.set(id, [text, kind || "", r.sha256 || ""]); show(text, kind); };
      const signers = Array.isArray(r.signers) ? r.signers : [];
      const k = signNotes.get(id);
      if (k) show(k[0], k[1]);
      else if (!signers.length) show(listing.note || "no passkey is enrolled for this name", "bad");
      else show("");
      const acts = el("div", "sign-actions");
      const buttons = [];
      let sheetBtn = null;    // "open the sheet": locked with the card's own controls, whether or not a key may sign
      const lock = (on) => { for (const b of buttons) b.disabled = on || !signers.length; if (sheetBtn) sheetBtn.disabled = on; };
      let freeze = null;      // the write-in's frozen state while its ceremony runs (set below when it offers one)
      const run = (choice, words) => {
        if (signBusy) return say(SIGN_BUSY, "bad");   // the lease holds a second tap, never a button drawn disabled
        signBusy++;
        lock(true);
        if (!signPending.has(id)) signPending.set(id, new Set());
        let done = "unsigned";
        let rearm = true;     // false: the card stays frozen (signed, or "not sure" until a read finds the version open)
        signWho(Object.assign({}, listing, { signers: signers }), acts, say)
          .then((who) => (who ? signCeremony(r, who.fp, who.rawId, choice, words, say) : { done: "unsigned" }),
                () => { say(SIGN_PICK_CLOSED, "bad"); return { done: "unsigned" }; })
          // a ceremony that threw (past its own catch) may have sent the answer: "not sure", which reads the request
          // again below and releases a write-in's words only if it is still open
          .catch((e) => {
            say("not sure this was signed (the page failed: " + ((e && e.name) || "error") + "): check the list", "bad");
            return { done: "unsure" };
          })
          .then((out) => {
            done = out.done;
            // only a signature spends the words. "Not sure" keeps them and every button frozen until a read of the
            // request by its id shows the same version still open twice running; anything else leaves the card
            // frozen and says so, and the board reads again below (a redrawn card offers the words only if still open)
            if (out.done === "signed") { signWriteIns.delete(id); rearm = false; return undefined; }
            if (out.done === "unsigned") { if (freeze) freeze.release(); return undefined; }
            rearm = false;
            const said = (signNotes.get(id) || [""])[0];
            const readOne = () => Promise.resolve().then(() => signOne(id)).catch((e) => ({ error: "the read failed (" + ((e && e.name) || "error") + ")" }));
            const stillOpen = (one) => !!one.request && one.request.sha256 === r.sha256 && !one.request.refused;
            const settled = (one) => say(said + (one.error ? "; where it stands could not be read (" + one.error + ")"
              : one.request ? "; the request changed while it was being signed" : "; the request is no longer open")
              + ": the board reads it again", "bad");
            // one open read does not show nothing was signed: the answer may still be verifying. The card stays frozen,
            // and comes back only if a second read, after the board's own delay, finds the same version still open
            return readOne().then((first) => {
              if (!stillOpen(first)) return settled(first);
              say(said + "; the request reads open now; not confirmed", "bad");
              return new Promise((res) => setTimeout(res, 1500)).then(readOne).then((second) => {
                if (!stillOpen(second)) return settled(second);
                rearm = true;
                if (freeze) freeze.release();
                return say(said + "; the request still reads open on a second read: sign it again", "bad");
              });
            });
          })
          .catch(() => { /* the ceremony said what happened; the lease is released below */ })
          .finally(() => {
            signBusy--;
            for (const p of acts.querySelectorAll(".sign-pick")) p.remove();
            lock(!rearm);
            if (rearm) { const later = signPending.get(id); signPending.delete(id); if (later) for (const f of later) f(); }
            signListed = null;
            // a signed (or not-knowable) answer may have closed the request: the board reads again (a row its caller has
            // answered leaves). A definite "nothing was signed" changed nothing, so nothing is read again under the operator
            if (done !== "unsigned" && !rearm) rereadBoard();
          });
      };
      const options = Array.isArray(r.options) ? r.options : [];
      if (!options.length) {
        const b = el("button", "undo-btn question-option sign-option", "sign with your passkey");
        b.type = "button";
        b.addEventListener("click", () => run(undefined, undefined));
        buttons.push(b);
        acts.appendChild(b);
      }
      for (const o of options) {
        const rec = o && o.key === r.recommended;
        const b = el("button", "undo-btn question-option sign-option" + (rec ? " question-rec" : ""), "(" + String(o.key) + ") " + String(o.text));
        b.type = "button";
        b.title = "sign: " + String(o.line);
        b.addEventListener("click", () => run(String(o.key), undefined));
        buttons.push(b);
        acts.appendChild(b);
      }
      const wi = options.length && r.write_in && typeof r.write_in === "object" ? r.write_in : null;
      if (wi) {
        const box = el("input", "sign-write-in");
        box.type = "text";
        box.maxLength = Number(wi.max) || 200;
        box.placeholder = "or write your own answer";
        const draft = signWriteIns.get(id);
        if (draft && draft.sha256 !== (r.sha256 || "")) signWriteIns.delete(id);   // typed on a replaced version
        box.value = signWriteIns.has(id) ? signWriteIns.get(id).words : "";
        box.addEventListener("input", () => signWriteIns.set(id, { sha256: r.sha256 || "", words: box.value }));
        const b = el("button", "undo-btn question-option sign-option", "(" + String(wi.key) + ") sign my words");
        b.type = "button";
        // the words being signed are frozen from the tap until the ceremony ends; only a definite "nothing was signed"
        // unfreezes them, so a retry is a new tap over the same words
        freeze = { release: () => { box.readOnly = box.disabled = false; } };
        if (signPending.has(id)) box.readOnly = box.disabled = true;
        b.addEventListener("click", () => {
          if (signBusy) return say(SIGN_BUSY, "bad");
          const w = box.value.trim();
          if (!w) return say("type your answer first", "bad");
          box.value = w;
          signWriteIns.set(id, { sha256: r.sha256 || "", words: w });
          box.readOnly = box.disabled = true;
          run(String(wi.key), w);
        });
        buttons.push(b);
        acts.append(box, b);
      }
      lock(false);
      sheetBtn = signSheetButton(r, say);
      acts.appendChild(sheetBtn);
      if (signPending.has(id)) {
        lock(true);
        signPending.get(id).add(() => { lock(false); if (freeze) freeze.release(); });
      }
      wrap.appendChild(acts);
      if (options.length) {
        const lines = el("div", "sign-lines ext-meta", "an option signs the request's message with its line appended:");
        for (const o of options) lines.appendChild(el("div", "sign-line", String(o.line)));
        if (wi) lines.appendChild(el("div", "sign-line", String(wi.line) + "<your words>"));
        wrap.appendChild(lines);
      }
    });
    return wrap;
  }

  // ---- The sign sheet over the cockpit (ONESURFACE OS-1, Q2 RULED OVERLAY) ----
  // The sheet /sign/<id> shown as a same-origin iframe over this page: the sheet runs its own ceremony and tells this page
  // "signed", "reload" or "close" by postMessage, read only from this origin and from that frame. Its headers let only this
  // origin frame it. Signing there changes the board, so the board reads again after a signature, and after a sign whose
  // outcome the sheet could not learn ("reload"), and after a close the sheet did not answer. While it is open everything
  // behind it is inert, and the sheet holds the same `signBusy` lease a card's ceremony does, so an inline ceremony and the
  // sheet never challenge at once. A close (its x, Escape) is asked of the sheet first: it answers "ack" at once, then
  // leaves only when its own rule lets it (a ceremony in flight stays, and says so).
  const SIGN_SHEET_ID = "levain-sign-sheet";
  const SIGN_CLOSE_ACK_MS = 1000;       // a sheet that has not answered the close request by then is closed by this page
  const SIGN_RELOAD_MS = 1500;
  const SIGN_RELOAD_LATE_MS = 8000;     // a second read after a close forced past a sheet that never answered
  let signSheetInerted = [];      // what openSignSheet made inert (and closeSignSheet gives back): never what was already
  let signSheetLease = false;     // the sheet holds a `signBusy` count
  let signSheetAsk = null;        // the pending close request's timer
  let signSheetWatch = null;      // the MutationObserver that makes a node added to the body while open inert
  let signBoardTimer = null;      // the one pending board read
  let signBoardPasses = 0;        // board reads still owed, a latched run's included
  function signSheetButton(r, say) {
    const b = el("button", "undo-btn sign-sheet-open", "open the sheet");
    b.type = "button";
    b.title = "sign this request in the sign sheet";
    b.addEventListener("click", () => { if (!openSignSheet(r.id, r.sha256) && signBusy && say) say(SIGN_BUSY, "bad"); });
    return b;
  }
  // `version`: the listing's sha256 the card showed (the sheet draws a pick only on it); `choice`: an option to preselect
  function openSignSheet(id, version, choice) {
    if (typeof id !== "string" || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) return null;
    return openSheet("/sign/" + encodeURIComponent(id), version, choice, "sign request");
  }
  // OS-2: the same sheet over a consent request (/sign/consent/<id>), answered or ruled there with a passkey
  function openConsentSheet(id, version, choice) {
    if (typeof id !== "string" || !/^[A-Za-z0-9._:-]{1,160}$/.test(id)) return null;
    return openSheet("/sign/consent/" + encodeURIComponent(id), version, choice, "consent request");
  }
  function openSheet(path, version, choice, what) {
    closeSignSheet();
    if (signBusy) return null;
    signBusy++;
    signSheetLease = true;
    try {
      return buildSignSheet(path, version, choice, what);
    } catch (e) {
      closeSignSheet();   // a build that threw gives back the lease and whatever it had made inert
      return null;
    }
  }
  function buildSignSheet(path, version, choice, what) {
    let frag = typeof version === "string" && /^[0-9a-f]{16}/.test(version) ? "v=" + version.slice(0, 16) : "";
    if (frag && typeof choice === "string" && /^[a-z0-9]{1,8}$/.test(choice)) frag += "&choice=" + choice;
    const veil = el("div", "modal-overlay");
    veil.id = SIGN_SHEET_ID;
    veil.setAttribute("role", "dialog");
    veil.setAttribute("aria-modal", "true");
    veil.setAttribute("aria-label", what);
    const box = el("div", "modal");
    const head = el("div", "modal-head");
    head.appendChild(el("h2", "", what.charAt(0).toUpperCase() + what.slice(1)));
    const x = el("button", "modal-close", "×");
    x.type = "button";
    x.setAttribute("aria-label", "close the sign sheet");
    x.addEventListener("click", askCloseSignSheet);
    head.appendChild(x);
    const frame = document.createElement("iframe");
    frame.id = SIGN_SHEET_ID + "-frame";
    frame.title = what;
    frame.setAttribute("allow", "publickey-credentials-get");
    frame.referrerPolicy = "no-referrer";
    frame.src = path + (frag ? "#" + frag : "");
    Object.assign(frame.style, { width: "100%", height: "min(80vh, 680px)", border: "0", display: "block", background: "transparent" });
    box.append(head, frame);
    veil.appendChild(box);
    veil.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); askCloseSignSheet(); } });
    document.body.appendChild(veil);
    document.body.classList.add("modal-open");
    for (const n of document.body.children) if (n !== veil && !n.inert) { n.inert = true; signSheetInerted.push(n); }
    // a board that draws again while the sheet is open adds nodes to the body: they are made inert as they arrive
    if (typeof MutationObserver === "function") {
      signSheetWatch = new MutationObserver((muts) => {
        for (const m of muts) for (const n of m.addedNodes) {
          if (n.nodeType === 1 && n !== veil && n.id !== "levain-modal" && !n.inert) { n.inert = true; signSheetInerted.push(n); }
        }
      });
      signSheetWatch.observe(document.body, { childList: true });
    }
    x.focus();
    return veil;
  }
  // ask the sheet to close; it answers "ack", then tells "close" when it may leave. No answer within SIGN_CLOSE_ACK_MS
  // (the frame is gone or stuck): this page closes it and latches the board read, since a signature may have been landing
  function askCloseSignSheet() {
    const frame = document.getElementById(SIGN_SHEET_ID + "-frame");
    if (!frame) {
      // no frame to ask: leave now, and read the board again if a sheet was up, since a signature may have been landing
      const was = !!document.getElementById(SIGN_SHEET_ID);
      closeSignSheet();
      if (was) rereadBoard(true);
      return;
    }
    if (signSheetAsk) return;
    signSheetAsk = setTimeout(() => { signSheetAsk = null; closeSignSheet(); rereadBoard(true); }, SIGN_CLOSE_ACK_MS);
    try { frame.contentWindow.postMessage({ levainSheetCmd: "close" }, window.location.origin); } catch (e) { /* the timer closes it */ }
  }
  function closeSignSheet() {
    clearTimeout(signSheetAsk);
    signSheetAsk = null;
    const veil = document.getElementById(SIGN_SHEET_ID);
    if (signSheetWatch) { signSheetWatch.disconnect(); signSheetWatch = null; }
    for (const n of signSheetInerted) n.inert = false;
    signSheetInerted = [];
    if (veil) veil.remove();
    if (!document.getElementById("levain-modal")) document.body.classList.remove("modal-open");
    if (signSheetLease) { signSheetLease = false; signBusy--; }
  }
  // one pending board read; `latch`: a second read follows, for a sheet closed while its signature may have been landing
  function signBoardTick() {
    signBoardTimer = null;
    signListed = null;
    signBoardPasses--;
    const t = window.LevainCockpitTransport;
    if (t && typeof t.reload === "function") t.reload();
    if (signBoardPasses > 0) signBoardTimer = setTimeout(signBoardTick, SIGN_RELOAD_LATE_MS);
  }
  function rereadBoard(latch) {
    signBoardPasses = Math.max(signBoardPasses, latch ? 2 : 1);
    clearTimeout(signBoardTimer);
    signBoardTimer = setTimeout(signBoardTick, SIGN_RELOAD_MS);
  }
  window.addEventListener("message", (ev) => {
    const frame = document.getElementById(SIGN_SHEET_ID + "-frame");
    if (!frame || ev.source !== frame.contentWindow || ev.origin !== window.location.origin) return;
    const what = ev.data && typeof ev.data === "object" ? ev.data.levainSheet : null;
    if (what === "ack") { clearTimeout(signSheetAsk); signSheetAsk = null; }
    else if (what === "close") { closeSignSheet(); dispatchStale(); }
    else if (what === "signed" || what === "reload") {
      // the sheet's line stays on it until it is closed; the board under it reads again, and the dispatch jobs with it
      dispatchStale();
      rereadBoard();
    }
  });
  window.LevainSignSheet = { open: openSignSheet, openConsent: openConsentSheet, openDispatch: openDispatchSheet, openDrop: openDropSheet, close: closeSignSheet };

  // A signed verb's send (ActionVerb.sign): the server's compose_signer files a sign request for exactly these params, the
  // passkey signs it, and only then does the caller post the verb with `sign_request`. Resolves {ok: true, request, txn} or
  // {ok: false, message}; nothing here clears a draft.
  //
  // A signed draft has ONE identity for its whole life, its `txn`: minted the first time those words are sent from that
  // panel's compose box and kept with them in this tab's session storage (it survives a reload; a page without storage keeps it
  // for this page only). The compose route answers the same request for the same txn while that request is open, so a
  // lost reply, a retry or a reload files one request, and the verb post is keyed by the txn (see `fire`), never by the
  // request it names. A signed request is remembered as signed until the send lands or is refused outright.
  const SIGN_DRAFTS = "levain-signed-drafts";
  const SIGN_DRAFT_TTL_MS = 24 * 3600 * 1000;   // a draft's identity outlives any request filed for it (they expire sooner)
  let signDraftsMem = {};                         // the same records, for this page, when session storage is absent
  const signCanon = (v) => (Array.isArray(v) ? "[" + v.map(signCanon).join(",") + "]"
    : v && typeof v === "object" ? "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + signCanon(v[k])).join(",") + "}"
    : JSON.stringify(v === undefined ? null : v));
  const signDraftKey = (verb, panel, params) => signCanon([verb, panel || null, params]);
  function signDrafts() {
    let stored = null;
    try { stored = JSON.parse(window.sessionStorage.getItem(SIGN_DRAFTS) || "{}"); } catch (e) { stored = null; }
    const o = Object.assign({}, signDraftsMem, stored && typeof stored === "object" && !Array.isArray(stored) ? stored : {});
    const now = Date.now();
    for (const k of Object.keys(o)) {
      const d = o[k];
      if (!d || typeof d !== "object" || typeof d.txn !== "string" || typeof d.at !== "number" || now - d.at > SIGN_DRAFT_TTL_MS) delete o[k];
    }
    return o;
  }
  function signDraftsSave(o) {
    signDraftsMem = o;
    try { window.sessionStorage.setItem(SIGN_DRAFTS, JSON.stringify(o)); } catch (e) { /* kept for this page only */ }
  }
  function signNewTxn() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
      const a = new Uint8Array(16);
      window.crypto.getRandomValues(a);
      return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
    } catch (e) { return "txn-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12); }
  }
  // the draft's record {txn, request, signed, at}, minted on its first send
  function signDraft(verb, panel, params) {
    const o = signDrafts();
    const k = signDraftKey(verb, panel, params);
    if (!o[k]) { o[k] = { txn: signNewTxn(), request: "", signed: false, at: Date.now() }; signDraftsSave(o); }
    return o[k];
  }
  function signDraftSet(verb, panel, params, patch) {
    const o = signDrafts();
    const k = signDraftKey(verb, panel, params);
    if (o[k]) { Object.assign(o[k], patch); signDraftsSave(o); }
  }
  // the send landed, or was refused outright: the next send of these words is a new draft (a new request, a new key)
  function signDraftDrop(verb, panel, params) {
    const o = signDrafts();
    delete o[signDraftKey(verb, panel, params)];
    signDraftsSave(o);
  }
  async function signedCompose(verb, panel, params, say, host) {
    // the lease is held from before the compose write, so a second send (a double click, a second box) files nothing
    if (signBusy) return { ok: false, message: "a signature is already being asked for; nothing was sent" };
    signBusy++;
    let last = "";
    const tell = (t, k) => { last = t; say(t, k); };
    const kept = "; nothing was sent, and your draft is kept";
    try {
      const listing = await signListing(true);
      if (listing.error) { tell(listing.error, "bad"); return { ok: false, message: listing.error }; }
      if (signElsewhere(listing)) {
        const m = signWhere(listing) + kept;
        tell(m, "bad");
        return { ok: false, message: m };
      }
      const d = signDraft(verb, panel, params);
      if (d.signed && d.request) return { ok: true, request: d.request, txn: d.txn };
      tell("asking the cockpit to file this for your signature…");
      const c = await signCall("/cockpit/sign/compose", { verb: verb, params: params, client_txn: d.txn });
      if (!c.ok) {
        // the route itself absent (no compose_signer) is "not_found"; any other refusal carries the server's words. The
        // draft keeps its identity: a compose whose reply was lost is answered with the same request next time
        const m = c.status === 404 && (!c.body.error || c.body.error === "not_found")
          ? "this cockpit cannot sign messages; nothing was sent" : "not sent: " + signWhy(c);
        tell(m, "bad");
        return { ok: false, message: m };
      }
      const rid = String(c.body.request || "");
      signDraftSet(verb, panel, params, { request: rid });
      // read by its id: a listing past its cap would not name it
      const one = await signOne(rid);
      if (one.error || !one.request) {
        const m = (one.error || "the sign request was filed but is not open") + kept;
        tell(m, "bad");
        return { ok: false, message: m };
      }
      const r = one.request;
      if (r.refused) {
        const m = "not signable: " + r.refused + kept;
        tell(m, "bad");
        return { ok: false, message: m };
      }
      const signers = Array.isArray(r.signers) ? r.signers : [];
      if (!signers.length) {
        const m = (one.note || listing.note || "no passkey is enrolled for this name") + kept;
        tell(m, "bad");
        return { ok: false, message: m };
      }
      let who;
      try { who = await signWho(Object.assign({}, listing, { signers: signers }), host, tell); }
      catch (e) { tell(SIGN_PICK_CLOSED, "bad"); return { ok: false, message: SIGN_PICK_CLOSED + kept }; }
      if (!who) return { ok: false, message: last + kept };
      const out = await signCeremony(r, who.fp, who.rawId, undefined, undefined, tell);
      if (out.done === "signed") {
        signDraftSet(verb, panel, params, { signed: true });
        return { ok: true, request: rid, txn: d.txn };
      }
      return { ok: false, message: last + (out.done === "unsure" ? "" : kept) };
    } finally {
      signBusy--;
      for (const p of host.querySelectorAll(".sign-pick")) p.remove();
      signListed = null;
    }
  }

  // ---- The Needs-you strip (K4, F4 a / F11 a) ----
  // What waits on the operator, drawn as cards under the now band: each PENDING consent request and each OPEN question of the
  // Decisions panel, as the mock draws them: the open questions first (they block work and have no clock), then the requests on a
  // clock, soonest expiry first. The strip is absent when nothing waits
  // (never an empty strip), and on a tab that is not Morning or Board it is the header alone. A consent card carries no rec:
  // Allow and Deny weigh the same and the hint line is what informs (F11 a). A question keeps its rec, which is the agent's
  // words and authorises nothing. This page signs nothing: the card says to answer on the phone, and its buttons copy the CLI
  // command for a terminal; the question's answer goes through the verb path the board already has. History (decided rows,
  // outcomes, standing-rule proposals) stays in the Decisions focus view, behind the strip's "all decisions" button.
  function needsItems(lines) {
    const consents = [], questions = [];
    for (const ln of Array.isArray(lines) ? lines : []) {
      if (!ln || typeof ln !== "object") continue;
      if (ln.consent && ln.consent.state === "pending") consents.push({ kind: "consent", c: ln.consent });
      else if (ln.question && ln.question.state === "open") questions.push({ kind: "question", q: ln.question });
    }
    const due = (it) => { const ms = Date.parse(it.c.expiresAt); return Number.isFinite(ms) ? ms : Infinity; };
    consents.sort((a, b) => due(a) - due(b));   // Array.prototype.sort is stable: equal expiries keep the kernel's order
    return questions.concat(consents);
  }
  const needsKey = (it) => (it.kind === "consent" ? "c:" + it.c.id + "|" + it.c.state + "|" + it.c.expiresAt : "q:" + it.q.n + "|" + it.q.state);
  // the name a glance tile gives the top item: the tool and who asked, or the question's number (its words are fetched, not in the row)
  function needsName(it) {
    return it.kind === "consent" ? (it.c.tool || "request") + " · " + (it.c.agent || "agent") : "question #" + it.q.n + " · " + (it.q.agent || "agent");
  }
  function needsSummary(items) {
    const nc = items.filter((it) => it.kind === "consent").length, nq = items.length - nc;
    const parts = [];
    if (nc) parts.push(nc + (nc === 1 ? " consent" : " consents"));
    if (nq) parts.push(nq + (nq === 1 ? " question" : " questions"));
    const first = items.find((it) => it.kind === "consent");
    return parts.join(" · ") + (first && Number.isFinite(Date.parse(first.c.expiresAt)) ? " · soonest " + consentLeft(first.c.expiresAt) : "");
  }
  // the focus view's dialog title already names the tool or question number and the agent: the card's head holds only the clock or age
  function needsHead(card, tail) {
    const head = el("div", "consent-head needs-head");
    if (tail) head.appendChild(tail);
    card.appendChild(head);
  }
  const needsIdOf = (it) => (it.kind === "consent" ? (it.c.id ? "consent:" + it.c.id : "") : (it.q.id ? String(it.q.id) : ""));
  // The CLI route (the digests, the note and the commands) sits at the card's foot under "Answer from a terminal".
  function needsDetails(parts, note) {
    const t = el("div", "needs-terminal");
    t.appendChild(el("div", "needs-sec-label", "Answer from a terminal"));
    if (note) t.appendChild(el("span", "needs-phone ext-meta", note));
    t.append(...parts);
    return t;
  }
  // The focus view of one waiting item: its card, drawn whole. Closes when the item is answered (refreshModal).
  function openNeedsFocus(it, opener) {
    const id = needsIdOf(it);
    modalCustom = {
      id,
      title: it.kind === "consent" ? (it.c.tool || "request") + " · " + (it.c.agent || "agent") : "Question #" + it.q.n + " · " + (it.q.agent || "agent"),
      build: () => (it.kind === "consent" ? consentCard(it.c) : questionCard(it.q)),
    };
    modalGlance = false;
    modalKey = "needs:" + id;
    modalReturnFocus = opener || null;
    modalEpisodeQuery = "";
    buildModal(null);
  }
  // The reader id a compose panel of the current view gives the seat called `name`, or null: a panel whose own action carries a recipient
  // field with a declared `transient_prefix`, whose enum (the one the view holds NOW) has prefix + name. Read from the view, never from a
  // chip on the compose card, so a card that is folded, on another tab, mid-send or drawn without that chip does not decide it.
  function seatRecipient(name) {
    if (!currentView || !commitAction || typeof name !== "string" || !name) return null;
    const ex = currentView.extra_panels || {}, verbs = currentView.action_verbs || {};
    for (const entry of (Array.isArray(currentView.layout) ? currentView.layout : [])) {
      const data = entry && ex[entry.id], act = data && data.action;
      if (!act || !act.verb || !verbs[act.verb] || !Array.isArray(act.fields)) continue;
      // the compose panel is what it is: the cockpit's compose card (a form panel with a recipient field is not one), whose recipient
      // field is the chips multiselect declaring the seat prefix
      if (act.card !== true) continue;
      const f = act.fields.find((x) => x && x.kind === "multiselect" && x.chips && typeof x.transient_prefix === "string" && x.transient_prefix);
      if (!f) continue;
      const id = f.transient_prefix + name;
      if ((Array.isArray(f.options) ? f.options : []).some((o) => (typeof o === "string" ? o : o && o.value) === id)) return { act, spec: verbs[act.verb], field: f, id };
    }
    return null;
  }
  // A seat's message box on its agent page: the compose panel's own verb, built by the same box builder the compose card uses (so the
  // confirm, the passkey signature of a signed verb and the failure words are the card's), its reader fixed to this seat.
  function seatMessageBox(name) {
    const wrap = el("div", "agent-msg");
    wrap.appendChild(el("div", "needs-sec-label", "Message " + name));
    const r = seatRecipient(name);
    if (!r) { wrap.appendChild(el("div", "agent-msg-none ext-meta", "can't message this seat right now")); return wrap; }
    const fields = r.act.fields.map((f) => (f === r.field ? Object.assign({}, f, { options: [r.id], default: [r.id] }) : f));
    const gone = (msg) => {
      msg.className = "edit-msg err"; msg.textContent = "this seat is gone; nothing was sent";
      // the view still lists it: refresh the panel so a page opened again reads the roster as it is now
      const V = window.LevainCockpitView;
      if (V && typeof V.refetchPanel === "function" && r.act.panel) { try { Promise.resolve(V.refetchPanel(r.act.panel)).catch(() => {}); } catch (_) { /* a refetch that cannot run leaves the held box as it is */ } }
    };
    wrap.appendChild(buildActionBox(Object.assign({}, r.act, { fields: fields }), r.spec, { pickKey: "seat-page:" + r.id, gone: gone }));
    return wrap;
  }
  // The agent page: one node of the Agents graph opened as a modal. The fact rows come from the caller (the graph's one row builder);
  // the transcript is read at open through the session reader and drawn by the session drawer.
  function openAgentPage(page, opener) {
    if (!page || typeof page !== "object") return;
    const id = String(page.id || "");
    const stem = typeof page.session === "string" && page.session ? page.session : (id.startsWith("session:") ? id.slice(8) : "");
    let card = null;
    const buildCard = () => {
      const c = el("div", "agent-page");
      const dl = el("dl", "consent-kv");
      for (const [k, v] of (Array.isArray(page.rows) ? page.rows : [])) { dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", null, String(v))); }
      c.appendChild(dl);
      if (typeof page.seat === "string" && page.seat) c.appendChild(seatMessageBox(page.seat));
      if (!stem) { c.appendChild(el("div", "ext-meta", "this view keeps no transcript for this agent")); return c; }
      const wrap = el("div", "sess-tx-wrap");
      const slot = el("div", "sess-tx ext-meta");
      const btn = el("button", "undo-btn sess-tx-btn", "refresh");
      btn.type = "button";
      const read = () => {
        btn.disabled = true;
        slot.replaceChildren(document.createTextNode("reading the transcript\u2026"));
        sessionTranscript(stem, wrap).then((b) => {
          drawSessionTranscript(slot, b);
          // the page opens on the newest entry (the timeline is oldest first, so that is its last row)
          const last = slot.querySelector(".sess-tl") && slot.querySelector(".sess-tl").lastElementChild;
          if (last && typeof last.scrollIntoView === "function") last.scrollIntoView({ block: "end" });
        })
          .catch(() => slot.replaceChildren(el("div", "consent-note ext-meta", SESS_BAD_READ)))
          .finally(() => { btn.disabled = false; });
      };
      btn.addEventListener("click", read);
      wrap.append(btn, slot);
      c.appendChild(wrap);
      read();
      return c;
    };
    modalCustom = { id, page: true, title: String(page.label || "agent") + " \u00b7 " + String((page.rows && page.rows[0] && page.rows[0][1]) || "agent"), build: () => (card = card || buildCard()) };
    modalGlance = false;
    modalKey = "agent:" + id;
    modalReturnFocus = opener || null;
    modalEpisodeQuery = "";
    buildModal(null);
  }
  // A JSON payload (a tool's input) as a key/value list: command, description, file_path. Anything else stays the verbatim text.
  function consentRequest(display, tool) {
    let t = String(display || "");
    const head = tool && t.replace(/^\s+/, "").startsWith(tool) ? t.replace(/^\s+/, "").slice(tool.length) : t;
    try {
      const o = JSON.parse(head.trim());
      if (o && typeof o === "object" && !Array.isArray(o) && Object.keys(o).length) {
        const dl = el("dl", "consent-kv");
        for (const [k, v] of Object.entries(o)) {
          dl.appendChild(el("dt", null, k));
          dl.appendChild(el("dd", null, typeof v === "string" ? v : JSON.stringify(v)));
        }
        return dl;
      }
    } catch (e) { /* not a JSON payload: the text below */ }
    return el("pre", "consent-display", t);
  }
  function drawConsentCard(card, slot, b) {
    const sv = (x) => (typeof x === "string" ? x : "");
    if (b.error) { slot.replaceChildren(el("div", "consent-note ext-meta", b.error)); return; }
    const label = (t) => el("div", "needs-sec-label", t);
    const egress = egressParts(b);
    const parts = egress ? [label("Request"), egress.state, egress.rows]
      : [label("Request"), consentRequest(sv(b.display), card._needsItem && card._needsItem.c.tool)];
    parts.push(label("Where"));
    const facts = el("div", "consent-facts");
    // the server's home shortens a path under it to ~/..., the full path stays as the title (never the browser's home)
    const where = el("span", "ext-meta", "in " + (sv(b.cwd_short) || sv(b.cwd)) + " \u00b7 targets " + sv(b.targets));
    if (sv(b.cwd)) where.title = sv(b.cwd);
    facts.appendChild(where);
    parts.push(facts);
    const hintBox = consentHintBox(b);
    if (hintBox) { parts.push(label("Hint"), hintBox); }
    if (card._needsItem && Number.isFinite(Date.parse(card._needsItem.c.expiresAt))) {
      parts.push(label("Expires"), el("div", "ext-meta needs-expires", new Date(card._needsItem.c.expiresAt).toLocaleString()));
    }
    // Allow and Deny are one component at one weight: neither is filled, neither leads. An answer the sheet offers here
    // (the body's `sheet_choices`, OS-2: a passkey enrolled that may sign it) opens the sign sheet with that answer picked;
    // any other copies the terminal command that answers (a passkey never allows a T3 request, so its Allow stays a
    // terminal line and never opens a sheet that offers only Deny). The terminal commands stay under the card's details.
    const acts = el("div", "needs-actions");
    const cmds = [];
    const cid = card._needsItem && card._needsItem.c ? card._needsItem.c.id : "";
    const choices = b.sheet === true && typeof cid === "string" && cid && Array.isArray(b.sheet_choices)
      ? b.sheet_choices.filter((k) => k === "allow" || k === "deny") : [];
    for (const [label, cmd] of [["Allow", sv(b.answer_allow)], ["Deny", sv(b.answer_deny)]]) {
      if (!cmd) continue;
      const btn = el("button", "needs-btn", label);
      btn.type = "button";
      btn.dataset.label = label;
      const code = el("code", "consent-code", cmd);
      if (choices.includes(label.toLowerCase())) {
        btn.title = label.toLowerCase() + " with your passkey, in the sign sheet";
        btn.dataset.consentSheet = label.toLowerCase();
        btn.addEventListener("click", () => openConsentSheet(cid, sv(b.sheet_v), label.toLowerCase()));
      } else {
        btn.title = "copy the " + label.toLowerCase() + " command for a terminal; this page signs nothing, sign on your phone with Face ID";
        btn.addEventListener("click", () => copyText(cmd, code, btn));
      }
      acts.appendChild(btn);
      const line = el("div", "consent-cmd");
      line.appendChild(code);
      cmds.push(line);
    }
    const more = [...consentDigestLines(b)];
    if (sv(b.note)) more.push(el("div", "consent-note ext-meta", sv(b.note)));
    more.push(...cmds);
    slot.replaceChildren(...parts);
    // the actions and the one-line footer are the card's own rows, pinned below the body so cards in a row end alike
    if (acts.childElementCount) card.appendChild(acts);
    const here = choices.length === 2 ? "signs with your passkey here"
      : choices.length ? choices[0] + " signs with your passkey here" : "signs on your phone";
    card.appendChild(needsDetails(more, here));
  }
  function consentCard(c) {
    const card = el("div", "needs-card needs-consent is-full");
    card.dataset.needsCard = "consent";
    card._needsItem = { kind: "consent", c };
    card.dataset.needsExpires = String(c.expiresAt);
    card.dataset.needsState = String(c.state);
    needsHead(card, (() => {
      const left = el("span", "consent-left needs-left", consentLeft(c.expiresAt));
      left.dataset.consentExpires = c.expiresAt;
      if (!consentTicker && typeof window.setInterval === "function") consentTicker = window.setInterval(tickConsent, 1000);
      return left;
    })());
    if (c.tier) card.firstChild.insertBefore(el("span", "needs-chip", c.tier), card.firstChild.firstChild);
    const slot = el("div", "needs-body ext-meta", "reading the request…");
    card.appendChild(slot);
    if (c.id) consentBody(c, card).then((b) => drawConsentCard(card, slot, b));
    else slot.textContent = "this request has no id";
    return card;
  }
  function questionCard(q) {
    const card = el("div", "needs-card needs-question is-full");
    card.dataset.needsCard = "question";
    card._needsItem = { kind: "question", q };
    needsHead(card, q.age ? el("span", "ext-meta needs-left", "asked " + q.age) : null);
    const slot = el("div", "needs-body ext-meta", "reading the question…");
    card.appendChild(slot);
    questionBody(q, card).then((b) => {
      drawQuestionBody(q, slot, b, card);
      if (!b.error) card.appendChild(needsDetails([copyLine("levain question show " + q.n, "show command")], ""));
    });
    return card;
  }
  function clearNeeds(host) {
    host.replaceChildren();
    host.hidden = true;
    delete host.dataset.sig;
  }
  // The strip's row (option A, Phill 10-26): one fixed-height summary per waiting item, a single button that opens the focus view.
  // It carries no answer control: the options, the words box, Allow/Deny and the sign row live only in the focus view, where the
  // item is drawn whole. Line one is the question's title (a consent's tool and first words), line two what it blocks (a consent's
  // working directory), the right edge the agent, the clock or age, and "rec <key>" when the item carries one. Text is drawn with
  // textContent and cut by CSS to one line each, so a long item never sets its row's height.
  function needsRow(it) {
    const row = el("button", "needs-row" + (it.kind === "consent" ? " needs-consent" : " needs-question"));
    row.type = "button";
    row.setAttribute("aria-haspopup", "dialog");
    row._needsItem = it;
    const id = needsIdOf(it);
    if (id) row.dataset.needsId = id;
    row.dataset.needsCard = it.kind;
    const title = el("span", "needs-row-title", it.kind === "consent" ? (it.c.tool || "request") : "question #" + it.q.n);
    const sub = el("span", "needs-row-sub ext-meta", "");
    const text = el("span", "needs-row-text");
    text.append(title, sub);
    const side = el("span", "needs-row-side");
    const who = el("span", "needs-chip consent-agent", (it.kind === "consent" ? it.c.agent : it.q.agent) || "agent");
    side.appendChild(who);
    const flat = (t) => String(t || "").replace(/\s+/g, " ").trim();
    if (it.kind === "consent") {
      row.dataset.needsExpires = String(it.c.expiresAt);
      row.dataset.needsState = String(it.c.state);
      const left = el("span", "ext-meta consent-left needs-left", consentLeft(it.c.expiresAt));
      left.dataset.consentExpires = it.c.expiresAt;
      if (!consentTicker && typeof window.setInterval === "function") consentTicker = window.setInterval(tickConsent, 1000);
      side.appendChild(left);
      title.className = "needs-row-title consent-tool";
      if (it.c.id) consentBody(it.c, row).then((b) => {
        if (!b || b.error) return;
        const w = needsFirstWords(b.display, it.c.tool);
        if (w) title.textContent = (it.c.tool || "request") + " " + w;
        const where = flat(b.cwd_short || b.cwd);
        sub.textContent = where ? "in " + where + (b.targets ? " \u00b7 targets " + flat(b.targets) : "") : "";
      });
    } else {
      if (it.q.age) side.appendChild(el("span", "ext-meta needs-left", "asked " + it.q.age));
      questionBody(it.q, row).then((b) => {
        if (!b || b.error) return;
        const first = flat(b.what);
        if (first) title.textContent = first;
        const more = flat(blocksBeyondTitle(b.what, b.blocks));
        if (more) sub.textContent = "blocks: " + more;
        if (typeof b.rec === "string" && b.rec) side.appendChild(el("span", "needs-rec-tag needs-row-rec", "rec " + b.rec));
      });
    }
    row.title = "open the whole request and decide there";
    row.append(text, side);
    row.addEventListener("click", () => openNeedsFocus(it, row));
    return row;
  }
  function renderNeeds(view) {
    const host = document.getElementById("ck-needs");
    if (!host) return;
    const items = needsItems(((view.extra_panels || {}).decisions || {}).lines);
    if (!items.length) { clearNeeds(host); return; }
    // the same set of waiting items leaves the strip as it is: a card with half-typed words, an open "details" and a running
    // countdown is not rebuilt under the reader by a refetch that changed nothing about what waits
    const sig = items.map(needsKey).join("\n");
    if (host.dataset.sig === sig && !host.hidden) return;
    const head = el("div", "needs-headline");
    head.appendChild(el("h3", null, "Needs you"));
    head.appendChild(el("span", "needs-count", String(items.length)));
    // no re-ping route exists (the phone is told when a request is filed, and the queue re-pings on its own clock; nothing here
    // pushes), so the gloss claims no ping: it says where the answer is signed
    head.appendChild(el("span", "needs-gloss", "Agents are waiting on a ruling. Sign it on your phone."));
    if (items.some((it) => it.kind === "question") && items.some((it) => it.kind === "consent")) {
      head.appendChild(el("span", "needs-order", "questions first, then soonest to expire"));
    }
    const hist = el("button", "needs-history", "all decisions");
    hist.type = "button";
    hist.title = "decided requests, outcomes and standing-rule proposals";
    hist.addEventListener("click", () => openDecisionsHistory(hist));
    head.appendChild(hist);
    // the rows are kept by their stable id: a row whose item still waits is moved, never rebuilt (words typed into an item live in
    // its focus view, which stays open while the item waits: refreshModal)
    let grid = host.querySelector(".needs-grid");
    const kept = new Map();
    if (grid) for (const c of grid.children) if (c.dataset && c.dataset.needsId) kept.set(c.dataset.needsId, c);
    // a row kept by id still shows what its item says NOW: a consent whose expiry moved (extended) takes the new clock in place;
    // one whose state changed is rebuilt
    const keep = (row, it) => {
      if (it.kind !== "consent") return row;
      if (row.dataset.needsState !== undefined && row.dataset.needsState !== String(it.c.state)) return needsRow(it);
      const exp = String(it.c.expiresAt);
      if (row.dataset.needsExpires !== exp) {
        row.dataset.needsExpires = exp;
        row._needsItem = it;
        const left = row.querySelector(".consent-left");
        if (left) { left.dataset.consentExpires = exp; left.textContent = consentLeft(it.c.expiresAt); }
      }
      return row;
    };
    const cards = items.map((it) => { const id = needsIdOf(it); const k = id && kept.get(id); return k ? keep(k, it) : needsRow(it); });
    if (!grid) grid = el("div", "needs-grid");
    for (let i = 0; i < cards.length; i++) if (grid.children[i] !== cards[i]) grid.insertBefore(cards[i], grid.children[i] || null);
    while (grid.children.length > cards.length) grid.removeChild(grid.lastChild);
    host.replaceChildren(head, needsBar(items), grid);
    host.hidden = false;
    host.dataset.sig = sig;   // only a strip that was drawn is recorded as drawn: a build that threw is tried again at the next render
  }
  // the Decisions focus view (decided requests, outcomes, proposals); false when this page has no Decisions panel
  function openDecisionsHistory(opener) {
    const entry = currentView && Array.isArray(currentView.layout) ? currentView.layout.find((e) => e.id === "decisions") : null;
    if (!entry) return false;
    openModal(findEntry(currentView, entryKey(entry)) || entry, opener || null, true);
    return true;
  }
  // The one orange bar Build and Memory draw in place of the cards (CSS shows it only there): dot, "Needs you", the count, one chip
  // per waiting item (its tool and first words, or the question's), the soonest clock, and "Open on Board". A chip goes to Board
  // too, with its card lit. A consent chip says its tool at once and adds the command's first words when the body arrives.
  const NEEDS_CHIPS = 6, NEEDS_CHIP_WORDS = 26;
  function needsFirstWords(display, tool) {
    let t = String(display || "").replace(/^\s+/, "");
    if (tool && t.startsWith(tool)) t = t.slice(tool.length).replace(/^\s+/, "");
    const m = /"(?:command|file_path|path|url|pattern|prompt)"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(t);
    t = (m ? m[1] : t.replace(/^[{\["]+/, "")).replace(/\\n/g, " ").replace(/\s+/g, " ").trim();
    return t.length > NEEDS_CHIP_WORDS ? t.slice(0, NEEDS_CHIP_WORDS - 1).trimEnd() + "\u2026" : t;
  }
  function needsBar(items) {
    const bar = el("div", "needs-bar");
    bar.appendChild(el("span", "needs-dot"));
    bar.appendChild(el("b", "needs-bar-title", "Needs you"));
    bar.appendChild(el("span", "needs-count", String(items.length)));
    const chips = el("div", "needs-chips");
    // the bar is the only surface on Build and Memory: as the mock's Build bar draws it, the requests on a clock come first, so an
    // expiring one is never folded behind "+N more" by questions (the cards keep the questions first)
    const barOrder = items.filter((it) => it.kind === "consent").concat(items.filter((it) => it.kind !== "consent"));
    for (const it of barOrder.slice(0, NEEDS_CHIPS)) {
      const chip = el("button", "needs-chip-btn", it.kind === "consent" ? (it.c.tool || "request") : "question #" + it.q.n);
      chip.type = "button";
      const id = it.kind === "consent" ? (it.c.id ? "consent:" + it.c.id : "") : (it.q.id ? String(it.q.id) : "");
      if (id) chip.dataset.needsId = id;
      chip.title = "open on the Board";
      chip.addEventListener("click", () => needsToBoard(id));
      chips.appendChild(chip);
      if (it.kind === "consent" && it.c.id) {
        consentBody(it.c, chip).then((b) => {
          const w = b && !b.error ? needsFirstWords(b.display, it.c.tool) : "";
          if (w) chip.textContent = (it.c.tool || "request") + " " + w;
        });
      } else if (it.kind === "question") {
        questionBody(it.q, chip).then((b) => {
          const w = b && !b.error && typeof b.what === "string" ? needsFirstWords(b.what, "") : "";
          if (w) chip.textContent = w;
        });
      }
    }
    if (items.length > NEEDS_CHIPS) chips.appendChild(el("span", "needs-more ext-meta", "+" + (items.length - NEEDS_CHIPS) + " more"));
    bar.appendChild(chips);
    const first = items.find((it) => it.kind === "consent" && Number.isFinite(Date.parse(it.c.expiresAt)));
    if (first) {
      const soon = el("span", "needs-soonest ext-meta", "soonest " + consentLeft(first.c.expiresAt));
      soon.dataset.consentExpires = first.c.expiresAt;
      soon.dataset.consentPrefix = "soonest ";
      bar.appendChild(soon);
      if (!consentTicker && typeof window.setInterval === "function") consentTicker = window.setInterval(tickConsent, 1000);
    }
    const open = el("button", "needs-history needs-open", "Open on Board");
    open.type = "button";
    open.addEventListener("click", () => needsToBoard(""));
    bar.appendChild(open);
    return bar;
  }
  // switch to Board, then focus the card the id names (the first card with no id)
  function needsToBoard(id) {
    const K = window.LevainCockpitKinds;
    if (K && typeof K.applyMode === "function") K.applyMode("board", true);
    window.setTimeout(() => { if (!(id && needsGo(id))) needsFocus(); }, 0);
  }
  // the focus the Decisions tile hands the operator in place of a modal: the first card, scrolled to
  function needsFocus() {
    const host = document.getElementById("ck-needs");
    if (!host || host.hidden) return false;
    const first = host.querySelector(".needs-row");
    if (!first || !first.offsetParent) return false;   // on Build and Memory the rows are not drawn: the bar is
    first.tabIndex = -1;
    if (typeof first.scrollIntoView === "function") first.scrollIntoView({ block: "center", behavior: "smooth" });
    first.focus({ preventScroll: true });
    return true;
  }

  // A record that names a card (the Work lens's waiting items) goes to the strip's card with that id: scrolled to and lit by an
  // animation that ends on its own (needs-lit), any earlier light cleared first. Where the strip is the bar (Build, Memory) the matching
  // chip is lit instead. False when no card or chip carries the id, so the caller can open the history.
  function needsGo(id) {
    const host = document.getElementById("ck-needs");
    if (!host || host.hidden || !id) return false;
    for (const n of host.querySelectorAll(".needs-lit")) n.classList.remove("needs-lit");
    const cards = host.querySelector(".needs-row") && host.querySelector(".needs-row").offsetParent;
    const sel = cards ? ".needs-row" : ".needs-chip-btn";
    const node = Array.prototype.find.call(host.querySelectorAll(sel), (n) => n.dataset.needsId === id);
    if (!node) return false;
    if (typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "center", behavior: "smooth" });
    void node.offsetWidth;   // restart the animation if the same node is lit twice
    node.classList.add("needs-lit");
    const off = () => node.classList.remove("needs-lit");
    node.addEventListener("animationend", off, { once: true });
    window.setTimeout(off, 3000);   // a page that plays no animation (reduced motion) still ends the light
    return true;
  }

  // A session's transcript (Q8): the Sessions modal's row button fetches /cockpit/session/<stem>.json with the token this
  // page holds, as the consent details do, and draws it as text only (textContent, never HTML: the prompts and replies are
  // whatever an agent said). The record shows the events the hooks kept (cut ones marked) with the session's consent
  // requests merged in by time; a page without its token says so, and a record that is gone says that.
  const SESS_NO_TOKEN = "this page carries no token, so the transcript is not shown";
  const SESS_BAD_READ = "the transcript could not be read";
  const sessionBodies = new Map();   // stem -> the last good body, so a redraw of the modal (a fresh view arrived) keeps an open transcript; closeModal clears it
  const SESS_BAD_TOKEN = "this page's token was not accepted, so the transcript is not shown";
  function sessionFetch(stem, headers, tryNo) {
    return fetchBounded("/cockpit/session/" + encodeURIComponent(stem) + ".json", { cache: "no-store", headers: headers },
      (res) => {
        if (res.status === 403) return { error: SESS_BAD_TOKEN };
        if (res.status === 404) return { error: "this session record is gone" };
        if (res.status === 503 && tryNo < CONSENT_TRIES) {
          return new Promise((r) => setTimeout(r, CONSENT_RETRY_MS)).then(() => sessionFetch(stem, headers, tryNo + 1));
        }
        if (!res.ok) return { error: "the transcript could not be read (HTTP " + res.status + ")" };
        return res.json().then((b) => (b && typeof b === "object" && Array.isArray(b.events) ? b : { error: "the transcript could not be read" }),
                               () => ({ error: "the transcript could not be read" }));
      },
      () => ({ error: "the transcript could not be read (network)" }));
  }
  function sessionTranscript(stem, row) {
    const t = window.LevainCockpitTransport;
    let headers;
    try { headers = t && typeof t.headers === "function" ? t.headers() : {}; } catch (e) { return Promise.resolve({ error: SESS_BAD_READ }); }
    if (!headers || !headers["X-Levain-Write-Token"]) return Promise.resolve({ error: SESS_NO_TOKEN });
    return consentQueued(() => sessionFetch(stem, headers, 1), SESS_BAD_READ, row);   // the consent fetches' two-at-a-time queue: a long Sessions list cannot stampede the server
  }
  const SESS_KIND = { prompt: "you", reply: "agent", tool: "tool", consent: "consent" };
  function sessionWhen(iso) {
    const ms = Date.parse(iso);
    return Number.isFinite(ms) ? new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
  }
  function sessionConsentLine(c) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const state = sv(c.state) || "unknown";
    const via = c.via === "signature" ? "signed answer" : c.via === "grant" ? "standing grant" : sv(c.via);
    let text = "consent: " + (sv(c.tool) || "tool") + " \u2014 " + state + (via ? " (" + via + ")" : "");
    if (sv(c.decided_at)) text += ", decided " + sessionWhen(c.decided_at);
    return text;
  }
  function drawSessionTranscript(slot, b) {
    if (b.error) { slot.replaceChildren(el("div", "consent-note ext-meta", b.error)); return; }
    const sv = (x) => (typeof x === "string" ? x : "");
    const parts = [];
    parts.push(el("div", "ext-meta sess-head", [sv(b.agent), sv(b.workspace),
      sessionWhen(b.first_seen) && "started " + sessionWhen(b.first_seen), sessionWhen(b.last_seen) && "last active " + sessionWhen(b.last_seen)].filter(Boolean).join(" \u00b7 ")));
    if (sv(b.resume)) {
      const line = el("div", "consent-cmd");
      const code = el("code", "consent-code", sv(b.resume));
      const btn = el("button", "undo-btn consent-copy", "copy resume");
      btn.type = "button";
      btn.dataset.label = btn.textContent;
      btn.title = "copy the command that reopens this session; run it in a terminal";
      btn.addEventListener("click", () => copyText(sv(b.resume), code, btn));
      line.append(code, btn);
      parts.push(line);
    }
    // one timeline, oldest first: the kept events and the consent requests by when each happened (equal times keep events first)
    const items = [];
    for (const e of b.events) if (e && typeof e === "object") items.push({ at: sv(e.at), kind: Object.prototype.hasOwnProperty.call(SESS_KIND, e.kind) ? e.kind : "tool", text: sv(e.text), cut: Number.isInteger(e.cut) ? e.cut : 0 });
    if (Array.isArray(b.consent)) for (const c of b.consent) if (c && typeof c === "object") items.push({ at: sv(c.at), kind: "consent", text: sessionConsentLine(c), cut: 0 });
    const keyed = items.map((it, i) => ({ it, i, ms: Date.parse(it.at) }));
    // a total order: the dated items by time, the undated after them, equal keys in kept order (a comparator that falls back to the index when only one side is dated is not transitive)
    const rank = (k) => [Number.isFinite(k.ms) ? 0 : 1, Number.isFinite(k.ms) ? k.ms : 0, k.i];
    keyed.sort((x, y) => { const a = rank(x), c = rank(y); return a[0] - c[0] || a[1] - c[1] || a[2] - c[2]; });
    const tl = el("div", "sess-tl");
    for (const { it } of keyed) {
      const ev = el("div", "sess-ev sess-" + it.kind);
      const lab = el("span", "sess-kind", SESS_KIND[it.kind]);
      const when = el("span", "sess-at", sessionWhen(it.at));
      const body = el("div", "sess-text");
      body.textContent = it.text;
      if (it.cut > 0) body.appendChild(el("span", "ext-meta sess-cut", " (\u2026 " + it.cut + " more characters not kept)"));
      ev.append(lab, when, body);
      tl.appendChild(ev);
    }
    if (!items.length) tl.appendChild(el("div", "ext-meta", "no events were kept for this session"));
    parts.push(tl);
    if (b.consent === null) parts.push(el("div", "ext-meta sess-note", "consent not read"));
    if (Number.isInteger(b.skipped) && b.skipped > 0) parts.push(el("div", "ext-meta sess-note", b.skipped + " log line" + (b.skipped === 1 ? "" : "s") + " could not be read and " + (b.skipped === 1 ? "is" : "are") + " not shown"));
    slot.replaceChildren(...parts);
  }
  function sessionTxControl(stem) {
    const wrap = el("div", "sess-tx-wrap");
    const btn = el("button", "undo-btn sess-tx-btn", "transcript");
    btn.type = "button";
    btn.title = "show what this session's hooks recorded";
    let slot = null;
    btn.addEventListener("click", () => {
      btn.disabled = true;
      if (!slot) { slot = el("div", "sess-tx ext-meta"); wrap.appendChild(slot); }
      slot.replaceChildren(document.createTextNode("reading the transcript\u2026"));
      sessionTranscript(stem, wrap).then((b) => {
        drawSessionTranscript(slot, b);
        if (!b.error) { btn.textContent = "refresh"; sessionBodies.set(stem, b); }
      }).catch(() => {
        slot.replaceChildren(el("div", "consent-note ext-meta", SESS_BAD_READ));
      }).finally(() => { btn.disabled = false; });
    });
    wrap.appendChild(btn);
    const kept = sessionBodies.get(stem);
    if (kept) {   // the row was rebuilt under an open transcript: draw the last body again, open
      slot = el("div", "sess-tx ext-meta");
      wrap.appendChild(slot);
      try { drawSessionTranscript(slot, kept); btn.textContent = "refresh"; } catch (e) { slot.remove(); slot = null; sessionBodies.delete(stem); }
    }
    return wrap;
  }

  // Standing-rule proposals (rows 13/17): when the operator has allowed the same agent + tool + working directory by hand
  // three times, the Decisions view offers the one `levain consent grant create` line that would make it a grant. The page
  // never signs; the card is text and a copy button. Fetched from /cockpit/consent/proposals.json with the page's token
  // through the consent queue above, and kept for 30 s so a modal repaint does not re-read it; every dashboard reload (render) and every fresh open of a
  // panel drops it. No token, or a read that fails:
  // no card (a proposal is an offer, never a gate).
  const PROPOSAL_TTL_MS = 30000;
  let proposalRead = null, proposalAt = 0;
  function proposalFetch() {
    if (proposalRead && Date.now() - proposalAt < PROPOSAL_TTL_MS) return proposalRead;
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve([]);
    proposalAt = Date.now();
    const got = consentQueued(() => fetchBounded("/cockpit/consent/proposals.json", { cache: "no-store", headers: headers },
      (res) => (res.ok ? res.json().then((b) => (b && Array.isArray(b.proposals) ? b.proposals : null), () => null) : null),
      () => null), "the proposals could not be read").then((b) => {
        if (!Array.isArray(b)) { if (proposalRead === got) proposalRead = null; return []; }
        return b;
      });
    proposalRead = got;
    return got;
  }
  function proposalCard(p) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const row = el("div", "row consent-row proposal-row ext-accent");
    row.setAttribute("data-consent-proposal", "");
    const head = el("div", "consent-head");
    head.appendChild(el("span", "consent-agent", sv(p.agent) || "agent"));
    head.appendChild(el("b", "consent-tool", sv(p.tool)));
    head.appendChild(el("span", "ext-meta", "standing rule?"));
    head.appendChild(el("span", "consent-left", "allowed by hand " + (Number.isInteger(p.count) ? p.count : "") + "\u00d7"));
    row.appendChild(head);
    const facts = el("div", "consent-facts");
    facts.appendChild(el("span", "ext-meta", "in " + sv(p.workspace)));
    facts.appendChild(el("span", "ext-meta", "for " + sv(p.expires) + ", " + sv(p.posture) + " mode"));
    row.appendChild(facts);
    const cmd = sv(p.command);
    if (cmd) {
      const line = el("div", "consent-cmd");
      const code = el("code", "consent-code", cmd);
      const btn = el("button", "undo-btn consent-copy", "copy command");
      btn.type = "button";
      btn.dataset.label = btn.textContent;
      btn.title = "copy the grant command; run it in a terminal with your signing key. This page signs nothing";
      btn.addEventListener("click", () => copyText(cmd, code, btn));
      line.append(code, btn);
      row.appendChild(line);
    }
    if (sv(p.note)) row.appendChild(el("div", "consent-note ext-meta", sv(p.note)));
    return row;
  }
  function proposalSlot() {
    const slot = el("div", "proposal-slot");
    proposalFetch().then((list) => {
      const cards = list.map(proposalCard)   // every card the backend returns (its own cap);
      if (cards.length) slot.replaceChildren(el("div", "ext-grouphead", "standing rule proposals"), ...cards);
    }).catch(() => {});
    return slot;
  }

  // M17: the standing grants, beside the proposals (a proposal offers a grant, a grant card takes one away). Each card
  // shows what the grant answered and a "revoke" button that copies the exact `levain consent grant revoke` line from
  // /cockpit/consent/grants.json (levain.consent.grants.grant_view): the page never signs, the terminal does with the
  // operator's key. A grant that answered a call which was then refused is the demotion signal: it leads the slot and
  // its refused calls lead its card. Fetched and cached exactly as the proposals are; no token or a failed read: no slot
  let grantRead = null, grantAt = 0;
  function grantFetch() {
    if (grantRead && Date.now() - grantAt < PROPOSAL_TTL_MS) return grantRead;
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve(null);
    grantAt = Date.now();
    const got = consentQueued(() => fetchBounded("/cockpit/consent/grants.json", { cache: "no-store", headers: headers },
      (res) => (res.ok ? res.json().then((b) => (b && Array.isArray(b.grants) ? b : null), () => null) : null),
      () => null), "the standing grants could not be read").then((b) => {
        // the queued read answers {error} on its own failures (a throw, a stall past the deadline): only a read that
        // carries grants is kept, so a failed one is read again at the next repaint instead of hiding the slot for 30 s
        if (!b || !Array.isArray(b.grants)) { if (grantRead === got) grantRead = null; return null; }
        return b;
      });
    grantRead = got;
    return got;
  }
  // how many of a grant's calls were refused: the server's count over all of them (`answered.refused_count`), never read
  // off the newest calls it sends (a refusal older than those still counts)
  const grantRefusedShown = (g) => (g && g.answered && Array.isArray(g.answered.recent) ? g.answered.recent : [])
    .filter((c) => c && c.outcome === "refused").length;
  const grantRefused = (g) => Math.max(g && g.answered && Number.isInteger(g.answered.refused_count) ? g.answered.refused_count : 0,
                                       grantRefusedShown(g));
  function grantCard(g, inertNote, signList) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const state = sv(g.state);
    const refused = grantRefused(g);
    const row = el("div", "row consent-row grant-row" + (refused ? " ext-accent grant-demote" : "") + (state === "live" ? "" : " ext-dim"));
    row.setAttribute("data-consent-grant", sv(g.id));
    const head = el("div", "consent-head");
    head.appendChild(el("span", "consent-agent", sv(g.agent) || sv(g.kind) || "grant"));
    const tools = Array.isArray(g.tools) ? g.tools.filter((x) => typeof x === "string").join(", ") : "";
    head.appendChild(el("b", "consent-tool", tools || sv(g.kind)));
    head.appendChild(el("span", "ext-meta", "standing grant · " + state));
    const a = g.answered && typeof g.answered === "object" ? g.answered : {};
    const count = Number.isInteger(a.count) ? a.count : 0;
    head.appendChild(el("span", "consent-left", count ? "answered " + count + "×" + (refused ? " · " + refused + " refused" : "")
                                                      : "answered nothing yet"));
    row.appendChild(head);
    const facts = el("div", "consent-facts");
    if (sv(g.workspace)) facts.appendChild(el("span", "ext-meta", "in " + g.workspace));
    if (Array.isArray(g.postures) && g.postures.length) facts.appendChild(el("span", "ext-meta", g.postures.join(", ") + " mode"));
    facts.appendChild(el("span", "ext-meta", (state === "expired" ? "expired " : "until ") + sv(g.expires_at)));
    facts.appendChild(el("span", "ext-meta", sv(g.kind) + " " + sv(g.id_shown)));
    row.appendChild(facts);
    if (state === "inert" && inertNote) row.appendChild(el("div", "consent-note ext-meta", inertNote));
    // the newest calls the server sent, refused ones first (sort is stable: each group stays newest first). A refused call
    // older than those is not on the card; the counts say how many there were in all
    const calls = (Array.isArray(a.recent) ? a.recent.slice() : [])
      .sort((x, y) => (y.outcome === "refused" ? 1 : 0) - (x.outcome === "refused" ? 1 : 0));
    if (calls.length) {
      const box = el("div", "grant-calls");
      for (const c of calls) {
        const o = sv(c.outcome) || "unknown";
        box.appendChild(el("div", "grant-call ext-meta grant-" + o, o + " · " + sv(c.tool) + " · " + sv(c.at)
                           + (sv(c.cwd) ? " · in " + c.cwd : "")));
      }
      const older = refused - grantRefusedShown(g);
      if (calls.length < count) box.appendChild(el("div", "grant-call ext-meta", "the newest " + calls.length + " of " + count
                                                   + (older > 0 ? " · " + older + " refused call" + (older === 1 ? "" : "s") + " older than these" : "")));
      row.appendChild(box);
    }
    // the passkey can revoke here when the grants' sign listing names THIS card's grant (its id AND the instance the card
    // shows: a grant renewed or re-made under the id, or one in another cockpit, is a different instance) and a passkey
    // for it, and this page is the one the ceremony is pinned to; anywhere else the button copies the line, as it always did
    const signed = signList && Array.isArray(signList.grants) && !signElsewhere(signList) && typeof g.instance === "string"
      ? signList.grants.find((x) => x && x.id === g.id && x.instance === g.instance
                                  && Array.isArray(x.signers) && x.signers.length) : null;
    if (signed) {
      const line = el("div", "consent-cmd grant-sign");
      const btn = el("button", "undo-btn grant-revoke", "revoke");
      btn.type = "button";
      btn.title = "revoke with your passkey";
      const note = el("span", "sign-note ext-meta");
      const say = (text, kind) => { note.textContent = text; note.className = "sign-note ext-meta" + (kind ? " sign-" + kind : ""); };
      btn.addEventListener("click", () => grantRevoke(g, signed, signList, btn, line, say));
      line.append(btn, note);
      row.appendChild(line);
    }
    const cmd = sv(g.revoke);
    if (cmd) {
      const line = el("div", "consent-cmd");
      const code = el("code", "consent-code", cmd);
      const btn = el("button", "undo-btn consent-copy", signed ? "copy" : "revoke");
      btn.type = "button";
      btn.dataset.label = btn.textContent;
      btn.title = signed ? "copy the revoke command; run it in a terminal with your signing key"
        : "copy the revoke command; run it in a terminal with your signing key. This page signs nothing";
      btn.addEventListener("click", () => copyText(cmd, code, btn));
      line.append(code, btn);
      row.appendChild(line);
    }
    return row;
  }
  // PLAN M17 "9a": on the https cockpit the revoke button signs with the passkey, the phone's own ceremony
  // (levain.consent.grant_phone.GrantPhone): /cockpit/grant/list.json names each grant's instance and the passkeys that
  // may revoke it (a 404 is a cockpit with no signing, and the button copies), then /cockpit/grant/<id>/revoke-challenge
  // -> navigator.credentials.get -> /revoke. The server builds and holds the link; the page recomputes the challenge from
  // the line and head it is sent and puts only that to the passkey, so what the prompt signs is the link shown
  const GRANT_SIGN_LIST = "/cockpit/grant/list.json";
  let grantSaid = "";          // what the last revoke said, kept across the slot's redraw (a revoked grant leaves it)
  function grantSignList() {
    const headers = signHeaders(false);
    if (!SIGN_USABLE || !headers["X-Levain-Write-Token"]) return Promise.resolve(null);
    return fetchBounded(GRANT_SIGN_LIST, { cache: "no-store", headers: headers },
      (res) => (res.ok ? res.json().then((b) => (b && Array.isArray(b.grants) ? b : null), () => null) : null),
      () => null);
  }
  // the bytes a passkey signs for the authority link `line` chained to `prevHash`, as levain.consent.passkey.challenge
  // builds them (the phone page's linkChallenge): "SSHSIG", then namespace, reserved, hash name and the SHA-512 of
  // prevHash + "\n" + line, each but the first as an SSH string
  function grantLinkChallenge(prevHash, line) {
    const enc = (t) => new TextEncoder().encode(t);
    const str = (b) => { const n = b.length, out = new Uint8Array(4 + n); out.set([n >>> 24 & 255, n >>> 16 & 255, n >>> 8 & 255, n & 255]); out.set(b, 4); return out; };
    return crypto.subtle.digest("SHA-512", enc(prevHash + "\n" + line)).then((h) => {
      const parts = [enc("SSHSIG"), str(enc("levain-authority")), str(new Uint8Array(0)), str(enc("sha512")), str(new Uint8Array(h))];
      const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      parts.reduce((at, p) => { out.set(p, at); return at + p.length; }, 0);
      return out;
    });
  }
  // One revoke: which passkey (signWho), the challenge, the passkey prompt (the gesture), the revoke, then the grants
  // read again. As the phone says it: only the server's own 4xx after the assertion was sent is "not revoked"
  function grantRevoke(g, signed, listing, btn, host, say) {
    if (signBusy) { say(SIGN_BUSY.replace("signed", "revoked"), "bad"); return; }
    signBusy++;
    btn.disabled = true;
    const base = "/cockpit/grant/" + encodeURIComponent(g.id) + "/";
    const slot = host.closest(".grant-slot");     // at the click: a redraw during the ceremony detaches this card
    let sent = false, revoked = false;
    signWho({ signers: signed.signers, origin: listing.origin }, host, say).then((who) => {
      if (!who) { say("nothing was revoked"); return; }
      say("asking for the revoke to sign…");
      return signCall(base + "revoke-challenge", { signer_fp: who.fp, instance: g.instance }).then((c) => {
        if (!c.ok) { say("not revoked: " + signWhy(c), "bad"); return; }
        let link = null;
        try { link = JSON.parse(c.body.line); } catch (e) { link = null; }
        // the server builds the link itself; one that does not name this grant is not put to the passkey
        if (!link || link.op !== "revoke_grant" || link.grant_id !== g.id || typeof c.body.challenge !== "string"
            || typeof c.body.prev_hash !== "string") {
          say("not revoked: the link the server sent does not name this grant, so nothing was signed", "bad");
          return;
        }
        return grantLinkChallenge(c.body.prev_hash, c.body.line).then((want) => {
          if (signB64u(want) !== c.body.challenge) {
            say("not revoked: the challenge the server sent is not over the link it shows, so nothing was signed", "bad");
            return;
          }
          say("confirm with your passkey to revoke… " + c.body.line);
          const pk = { challenge: want, rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
          return signAsk(pk, c.body.rp_id, who.fp, who.rawId).then((cred) => {
            if (!cred || !cred.response) { say("not revoked: the passkey gave nothing to send; nothing was revoked", "bad"); return; }
            say("verifying the signature…");
            sent = true;
            return signCall(base + "revoke", {
              authenticator_data: signB64u(cred.response.authenticatorData), client_data_json: signB64u(cred.response.clientDataJSON),
              signature: signB64u(cred.response.signature) }).then((r) => {
              // the assertion has gone: only a 4xx carrying the server's own error word is a definite refusal; anything
              // else (no answer, a 5xx, a 2xx/3xx whose body did not parse) may have revoked, so it is unknown. Each
              // outcome is kept (grantSaid) so the redraw below does not erase it.
              const refused = !r.ok && r.status >= 400 && r.status < 500 && r.body && typeof r.body.error === "string";
              if (!r.ok && !refused) {
                grantSaid = "result unknown for " + g.id + " (" + signWhy(r) + "): the grants were read again to show whether it is still there";
                say(grantSaid, "bad");
                return;
              }
              if (!r.ok) { grantSaid = "not revoked " + g.id + ": " + signWhy(r); say(grantSaid, "bad"); return; }
              signKeep(c.body.rp_id, who.fp, cred);
              revoked = true;
              grantSaid = "revoked " + ((typeof g.id_shown === "string" && g.id_shown) || g.id) + " · link seq " + r.body.seq + (r.body.warning ? " · " + r.body.warning : "");
              say(grantSaid, "ok");
            });
          }, (e) => say(signDeclined(e, "nothing was revoked"), "bad"));
        });
      });
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) grantSaid = "result unknown for " + g.id + " (the page failed: " + name + "): the grants were read again to show where it stands";
      say(sent ? grantSaid : "the page failed (" + name + "); nothing was revoked", "bad");
    }).finally(() => {
      signBusy--;
      btn.disabled = false;
      if (!sent && !revoked) return;
      // the grants again: a revoked grant leaves the slot, and the slot keeps what the revoke said
      grantRead = null;
      // the slot is resolved when the timer fires, not before: a redraw in the wait replaces it, and a fill into the
      // detached one would also win the fill sequence over the live slot's own fill
      setTimeout(() => {
        const live = slot && slot.isConnected ? slot : document.querySelector(".grant-slot");
        if (live) grantFill(live);
      }, 1500);
    });
  }
  function grantSlot() {
    const slot = el("div", "grant-slot");
    grantFill(slot);
    return slot;
  }
  let grantFillSeq = 0;        // the newest fill wins: an older read answering late never redraws over a newer one
  function grantFill(slot) {
    const n = ++grantFillSeq;
    Promise.all([grantFetch(), grantSignList()]).then(([b, signList]) => {
      if (!b || n !== grantFillSeq) return;
      // sort is stable: the server's order holds among grants with and among grants without a refused call
      const rows = b.grants.slice().sort((x, y) => (grantRefused(y) ? 1 : 0) - (grantRefused(x) ? 1 : 0));
      const note = typeof b.note === "string" ? b.note : "";
      // a page whose consent hook names no cockpit (root "") and that holds no grant gets no slot, as with no proposals;
      // a named cockpit that cannot be read is said
      // a revoke said here is kept (its grant has left the slot); the last grant revoked leaves the head and that line
      const said = grantSaid ? [el("div", "consent-note ext-meta grant-said", grantSaid)] : [];
      if (!rows.length && (!note || !b.root)) {
        slot.replaceChildren(...(said.length ? [el("div", "ext-grouphead", "standing grants"), ...said] : []));
        return;
      }
      const head = el("div", "ext-grouphead", "standing grants");
      const inert = typeof b.inert_note === "string" ? b.inert_note : "";
      // why no passkey can revoke on this page (a localhost cockpit, or a pin on another name than the passkeys'), in the server's words
      const why = signList && typeof signList.grants_note === "string" && signList.grants_note ? signList.grants_note : "";
      slot.replaceChildren(head, ...said, ...(note ? [el("div", "consent-note ext-meta", note)] : []),
                           ...(why ? [el("div", "consent-note ext-meta grant-why", why)] : []),
                           ...rows.map((g) => grantCard(g, inert, signList)));
    }).catch(() => {});
  }
  // OS-2, the dispatch family: the live dispatch jobs as cards, each act (Sign, Cancel, Authorise) a button that opens the sign
  // sheet at /sign/dispatch/<job> with that act picked. /cockpit/dispatch.json lists them as the sheet's rows (one object per
  // job: its message, its options each with the passkeys that may do it, the version the sheet signs on). This page signs
  // nothing: the sheet does, through the phone port's own routes. No token, a failed read, or no job: no slot.
  let dispatchRead = null, dispatchAt = 0;
  function dispatchFetch() {
    if (dispatchRead && Date.now() - dispatchAt < PROPOSAL_TTL_MS) return dispatchRead;
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve(null);
    dispatchAt = Date.now();
    const got = consentQueued(() => fetchBounded("/cockpit/dispatch.json", { cache: "no-store", headers: headers },
      (res) => (res.ok ? res.json().then((b) => (b && Array.isArray(b.requests) ? b : null), () => null) : null),
      () => null), "the dispatch jobs could not be read").then((b) => {
        if (!b || !Array.isArray(b.requests)) { if (dispatchRead === got) dispatchRead = null; return null; }
        return b;
      });
    dispatchRead = got;
    return got;
  }
  // OS-2: the sheet over a dispatch job (/sign/dispatch/<job>), signed, cancelled or authorised there with a passkey
  function openDispatchSheet(id, version, choice) {
    if (typeof id !== "string" || !/^dj-[0-9a-f]{32}$/.test(id)) return null;
    return openSheet("/sign/dispatch/" + encodeURIComponent(id), version, choice, "dispatch job");
  }
  // OS-2: the sheet over a coordinator drop (/sign/drop): words, and optionally an envelope the passkey signs there
  function openDropSheet() {
    return openSheet("/sign/drop", "", "", "coordinator drop");
  }
  // the drop card: shown only while /cockpit/coord/drops.json reads (a token, a Signer, a store that answers); the Send a drop
  // button opens the sheet, which sends and signs nothing from this page
  // the coordinator read goes through the shared bounded read queue and is held for a moment, as the dispatch read is: rapid
  // redraws share one read
  let dropRead = null, dropAt = 0, dropSlotNow = null;
  function dropFetch(headers) {
    if (dropRead && Date.now() - dropAt < PROPOSAL_TTL_MS) return dropRead;
    dropAt = Date.now();
    const got = consentQueued(() => fetchBounded("/cockpit/coord/drops.json", { cache: "no-store", headers: headers },
      (res) => (res.ok ? res.json().then((b) => (b && Array.isArray(b.coord_drops) ? b : null), () => null) : null),
      () => null), "the drops could not be read").then((b) => {
        if (!b || !Array.isArray(b.coord_drops)) { if (dropRead === got) dropRead = null; return null; }
        return b;
      });
    dropRead = got;
    return got;
  }
  function dropSlot() {
    const slot = el("div", "coord-drop-slot");
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return slot;
    dropSlotNow = slot;
    dropFetch(headers).then((b) => {
        if (!b || slot !== dropSlotNow) return;     // only the slot the latest render made is filled; an older one is gone
        const row = el("div", "row consent-row coord-drop-row");
        const head = el("div", "consent-head");
        head.appendChild(el("b", "consent-tool", "drop"));
        head.appendChild(el("span", "ext-meta", b.coord_drops.length + " recent from the phone port or this cockpit"));
        row.appendChild(head);
        const acts = el("div", "needs-actions");
        const btn = el("button", "needs-btn", "Send a drop");
        btn.type = "button";
        btn.dataset.coordDrop = "open";
        btn.title = "send words to the coordinator, optionally with an envelope your passkey signs, in the sign sheet";
        btn.addEventListener("click", () => openDropSheet());
        acts.appendChild(btn);
        row.appendChild(acts);
        if (typeof b.coord_note === "string" && b.coord_note) row.appendChild(el("div", "consent-note ext-meta", b.coord_note));
        slot.replaceChildren(el("div", "ext-grouphead", "coordinator drop"), row);
      }).catch(() => {});
    return slot;
  }
  const DISPATCH_VERB = { sign: "Sign", cancel: "Cancel job", authorise: "Authorise" };
  function dispatchCard(r) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const row = el("div", "row consent-row dispatch-row");
    row.setAttribute("data-dispatch-job", sv(r.id));
    const head = el("div", "consent-head");
    head.appendChild(el("span", "consent-agent", sv(r.agent) || "agent"));
    head.appendChild(el("b", "consent-tool", sv(r.id)));
    head.appendChild(el("span", "ext-meta", "dispatch job · " + sv(r.state)));
    if (sv(r.expires_at)) head.appendChild(el("span", "consent-left", "lease ends " + r.expires_at));
    row.appendChild(head);
    if (sv(r.goal)) row.appendChild(el("div", "consent-display", r.goal));
    const acts = el("div", "needs-actions");
    for (const o of Array.isArray(r.options) ? r.options : []) {
      if (!o || typeof o.key !== "string" || !DISPATCH_VERB[o.key]) continue;
      const btn = el("button", "needs-btn", DISPATCH_VERB[o.key]);
      btn.type = "button";
      btn.dataset.dispatchAct = o.key;
      btn.title = o.key + " this job with your passkey, in the sign sheet";
      btn.addEventListener("click", () => openDispatchSheet(r.id, sv(r.sha256), o.key));
      acts.appendChild(btn);
    }
    if (acts.childElementCount) row.appendChild(acts);
    if (sv(r.note)) row.appendChild(el("div", "consent-note ext-meta", r.note));
    return row;
  }
  function dispatchSlot() {
    const slot = el("div", "dispatch-slot");
    dispatchFill(slot);
    return slot;
  }
  // the newest fill OF THIS SLOT wins: an older read answering late never redraws over a newer one; a sequence per slot,
  // so a second slot (the board's and the history's) never stops the first from filling
  const dispatchFillSeq = new WeakMap();
  function dispatchFill(slot) {
    const n = (dispatchFillSeq.get(slot) || 0) + 1;
    dispatchFillSeq.set(slot, n);
    dispatchFetch().then((b) => {
      if (!b || n !== dispatchFillSeq.get(slot)) return;
      const note = typeof b.note === "string" ? b.note : "";
      const why = typeof b.elsewhere === "string" ? b.elsewhere : "";
      if (!b.requests.length && !note) { slot.replaceChildren(); return; }
      const over = Number.isInteger(b.overflow) && b.overflow > 0 ? [el("div", "consent-note ext-meta", b.overflow + " more not shown")] : [];
      slot.replaceChildren(el("div", "ext-grouphead", "dispatch jobs"), ...(note ? [el("div", "consent-note ext-meta", note)] : []),
                           ...(why ? [el("div", "consent-note ext-meta dispatch-why", why)] : []),
                           ...b.requests.map(dispatchCard), ...over);
    }).catch(() => {});
  }
  // the sheet signed, asked for a reload, or closed: the dispatch read is dropped (as a board reload drops it) and every
  // dispatch slot on the page reads again, so a job signed or cancelled there never stays on a card for the read's 30 s
  function dispatchStale() {
    dispatchRead = null;
    for (const s of document.querySelectorAll(".dispatch-slot")) dispatchFill(s);
  }
  // the Decisions history's standing-rule slots (and the dispatch jobs), drawn together wherever the proposals are
  function standingSlots() {
    const f = document.createDocumentFragment();
    f.append(proposalSlot(), grantSlot(), dispatchSlot(), dropSlot());
    return f;
  }

  function appendExternalLines(host, lines) {
    let lastGroup = null;
    // the Decisions panel's proposal cards (see renderExternalBody) follow the live requests, which are on a clock, and come
    // before the decided ones: ahead of the first "Resolved" / "Outcomes" heading, else after the last row
    const owner = host.closest ? host.closest("[data-proposal-slot]") : null;
    // The marker moves to the body the rows are drawn into: in the modal that body has no marked ancestor once it is shown
    // (read from the page, 10-17), and every filter / "show all" repaint empties it, slot included. The slot is due whenever
    // the body holds none right now, never on a flag set once.
    if (owner && owner !== host) host.dataset.proposalSlot = "";
    let slotDue = !!(owner && !host.querySelector(".proposal-slot"));
    for (const ln of lines) {
      const o = ln && typeof ln === "object" ? ln : {};
      // rows that carry a group (a list drawn as a count) come in the kernel's order: a heading where the group changes
      if (typeof o.grp === "string" && o.grp !== lastGroup) {
        lastGroup = o.grp;
        if (slotDue && (o.grp === "Resolved" || o.grp === "Outcomes")) { slotDue = false; host.appendChild(standingSlots()); }
        host.appendChild(el("div", "ext-grouphead", o.grp));
      }
      if (o.consent) { host.appendChild(consentRow(o)); continue; }
      if (o.question) { host.appendChild(questionRow(o)); continue; }
      if (o.note) { host.appendChild(noteRow(o)); continue; }
      const row = el("div", "row" + (o.accent ? " ext-accent" : "") + (o.dim ? " ext-dim" : ""));
      if (o.wrapMeta) row.classList.add("meta-wrap");   // a row whose meta line IS its content (the sessions' chips): it wraps, never ellipsises
      if (typeof o.key === "string" && o.key) row.dataset.keepKey = o.key;   // the id an expanded row is found by after a repaint
      if (typeof o.repo === "string" && o.repo) row.dataset.repoFacet = o.repo;   // read by the project-colour tag pass (cockpit_view.js)
      if (o.dot === "ok" || o.dot === "bad" || o.dot === "off") { row.classList.add("k7-res"); row.appendChild(el("i", "k7-rdot " + o.dot)); }   // Build's Results: the outcome dot leads
      if (o.meta && !o.addr) row.appendChild(el("span", "ext-meta", String(o.meta)));
      if (o.addr) {
        // an addressed row leads with `source -> for` ("everyone" when it names no reader)
        row.setAttribute("data-ck-row", "");
        const a = el("div", "ck-rowaddr");
        const src = el("b", null, String(o.addr.source));
        src.setAttribute("data-ck-row-source", "");
        a.append(src, document.createTextNode(" \u2192 " + (o.addr.to.length ? o.addr.to.join(", ") : "everyone") + (o.addr.age ? " \u00b7 " + o.addr.age : "")));
        row.appendChild(a);
        // the row's meta (badges, provenance, group) after the address line, never dropped
        if (o.meta) row.appendChild(el("span", "ext-meta", String(o.meta)));
      }
      appendClause(row, o.text != null ? String(o.text) : "");  // per-item expand, like episodes
      if (typeof o.session === "string" && o.session) row.appendChild(sessionTxControl(o.session));
      if (o.cancel && cancelPending) appendRowVerbs(row, buildPendingCancel(o.cancel));
      if (o.review) appendReviewVerb(row, o.review);
      host.appendChild(row);
    }
    if (slotDue) host.appendChild(standingSlots());
  }

  function renderEdits(entry, view) {
    const list = view.recent_edits || [];
    const p = panel(entry);
    if (list.length === 0) { p.appendChild(el("p", "empty", "Edits appear here once a session writes a file.")); return p; }
    const claimed = new Set();
    for (const e of list) {
      const row = el("div", "edit-row");
      row.appendChild(el("span", "tier", datePart(e.ts)));
      const isUndo = e.action === "undo";
      row.appendChild(el("span", "edit-act" + (isUndo ? " undo" : ""), isUndo ? "undo" : (e.action || "edit")));
      const label = String(e.source || "") + (e.heading ? " · " + e.heading : "");
      row.appendChild(el("span", "clause", label));
      // a manifest row lists its actions: undo shows only where the kernel offered it (a legacy row lists none)
      const undoOffered = !Array.isArray(e.actions) || e.actions.some((a) => a && a.verb === "undo"
        && a.gesture !== "refused" && a.gesture !== "laptop-only");
      if (commit && undoOffered && !isUndo && e.id && e.undoable !== false && !claimed.has(e.source)) {
        const u = el("button", "undo-btn", "undo");
        u.setAttribute("data-ck-verb", "");
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
  // A manifest view lists the verbs it offered; the rename shows only where the kernel offered it.
  function verbOffered(view, verb) {
    return !view || !view.offered_verbs || view.offered_verbs[verb] === true;
  }

  function wireEntityName(entityEl, view) {
    if (!commit || !verbOffered(view, "entity_name")) return;
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
      // Mark the editor "saving" so the SUCCESS reload's render() tears it down + restores the
      // entity line to the committed name, instead of the (passive-defer) guard leaving it
      // stranded frozen on "saving…" (spore-280 — the twin of the 247 state-line stranding). A
      // passive reload defers via hasUnsavedEdit(".name-editor"); only this save's passive:false
      // reload rebuilds #entity.
      editor.dataset.saving = "1";
      // no `expected` sent: the displayed name may come from the origin.md H1 fallback
      // (≠ the config field the server stale-checks), so an optimistic lock here would
      // false-409 the first rename. The name is a single trivial field — last-writer-wins.
      const res = await commit({ kind: "entity_name", value: input.value });
      if (res && res.ok) return;  // reloaded — the save render tore this editor down
      delete editor.dataset.saving;  // save FAILED: re-arm the guard so passive reloads defer again
      showFail(msg, res, "save failed");
      save.disabled = false; cancel.disabled = false;
    };
    cancel.addEventListener("click", restore);
    save.addEventListener("click", doSave);
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); doSave(); }
      else if (ev.key === "Escape") { ev.preventDefault(); restore(); }
    });
  }

  // The masthead state line's Class-A inline edit (commit-gated). The operator sets
  // "what's going on" IN the cockpit, a second set-path alongside `levain state`
  // (version-bound: see doSave). Mirrors wireEntityName: a small control swaps the line for an
  // input; save -> POST /edit {kind:"operator_state"}; a blank value CLEARS it. A
  // read-only port (no commit) shows no affordance.
  function enterStateEdit(fEl, state, btn, opts) {
    const kind = (opts && opts.kind) || "operator_state";
    const current = (state && state.text) || "";
    const editor = el("div", "state-editor");
    const input = el("input", "state-input");
    input.type = "text"; input.value = current; input.maxLength = 500;
    input.placeholder = (opts && opts.placeholder) || "what's going on, in your words; blank to clear";
    const save = el("button", "edit-save", "save");
    const cancel = el("button", "edit-cancel", "cancel");
    save.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    editor.append(input, save, cancel, msg);
    // Hide the line's current content + the trigger; the editor replaces them in place.
    const prior = Array.from(fEl.children);
    for (const n of prior) n.style.display = "none";
    fEl.appendChild(editor);
    input.focus(); input.select();

    const restore = () => {
      editor.remove();
      for (const n of prior) n.style.display = "";
    };
    const doSave = async () => {
      save.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "saving…";
      // Mark the editor "saving" so the SUCCESS reload's renderState rebuilds PAST the
      // in-progress guard (replacing this editor with the committed line) instead of
      // bailing and stranding it frozen on "saving…" (the L1 + codex/complement/nemotron
      // L3 HIGH, reproduced end-to-end).
      editor.dataset.saving = "1";
      // The write names the version of the line it was rendered with (state.version, a hash of
      // the stored state keys only, so a sensor writing other keys of the file does not
      // stale it); a line changed since render is refused 409 and its message shows. A blank
      // value clears (the server reads an empty line as unset). `source` is omitted → the
      // server stamps it from the credential (a client never self-declares).
      const res = await commit({ kind, text: input.value, panel_version: state && state.version });
      if (res && res.ok) return;  // reloaded — the save render rebuilt #state, removing this editor
      delete editor.dataset.saving;  // save FAILED: re-arm the guard so passive reloads defer again
      showFail(msg, res, "save failed");
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

  // ------------------------------------------------------------ focus modal ----
  // Expand-to-modal (Slice 2): the ⤢ on a dense panel opens a focused, full-screen
  // re-render of THAT panel — the SAME per-kind renderer, unbounded (no .clamped cap)
  // and given the whole screen. So per-type functionality comes for free: the episode
  // keyword-search becomes a roomy surface (search-into-modal, shape (c)), spore verbs
  // and the in-place State edit work exactly as in the grid. It is a PURE projection
  // of `currentView`, rebuilt on every render(), so a verb fired inside it reflects at
  // once and it stays put. A read-only source (no commit) opens a read+search-only
  // modal (NO THEATER — same as the grid panels).

  // A stable identity for a layout entry, so a rebuild on re-render re-locates the
  // SAME panel in the fresh view (the entry objects are new each fetch). Section/config
  // panels are uniquely identified by their write-address (source + heading), so the key
  // OMITS the list-index `ref` for them — a ref shift (a section added/removed above the
  // expanded one between fetches) must NOT spuriously close the modal. Singleton kinds
  // (episodes/spores/crystals/wraps/…) are unique by kind alone. JSON.stringify + a
  // disjoint tag keeps the two key spaces from ever colliding.
  const entryKey = (e) =>
    e.kind === "external"
      ? JSON.stringify(["ext", e.id || ""])   // external panels are id-keyed (N may coexist)
      : e.source
        ? JSON.stringify(["addr", e.kind, e.source, e.heading || ""])
        : JSON.stringify(["one", e.kind]);

  function findEntry(view, key) {
    const layout = view && Array.isArray(view.layout) ? view.layout : [];
    for (const e of layout) if (entryKey(e) === key) return e;
    return null;
  }

  // The header ⤢ control (METAL — you grip it to act). Added in panel() for the dense
  // kinds; the click opens the focus modal for this entry.
  function buildExpandBtn(entry) {
    const b = el("button", "pexpand");
    b.type = "button";
    b.title = "focus this panel";
    b.setAttribute("aria-label", "expand " + (entry.title || "panel") + " to a focus view");
    b.appendChild(el("span", "pexpand-ico", "⤢"));
    b.addEventListener("click", () => openModal(entry, b));
    return b;
  }

  function openModal(entry, triggerEl, glance) {
    modalGlance = !!glance;
    modalKey = entryKey(entry);
    modalReturnFocus = triggerEl || null;
    modalEpisodeQuery = "";  // a fresh open starts with the recent list, not a stale query
    proposalRead = null;     // a fresh open reads the proposals again: a denial or a grant since withdraws its card
    grantRead = null;        // and the grants again: a revoke since removes its card
    dispatchRead = null;     // and the dispatch jobs again
    buildModal(entry);
  }

  // Build the modal's inner content: render the panel, transplant its body (sans the
  // .phead — the modal supplies its own header), and re-attach the Class-A edit row if
  // editable. buildEditRow's handler reads host.querySelector(".pbody") → the modal
  // body, so editing works in-place exactly as the grid does. NOT clamped — the
  // .modal-body is the scroll container, so the content reads at full height.
  function modalContent(entry, view) {
    const p = renderPanelSafe(entry, view, { modal: true, glance: modalGlance });
    if (!p) return null;
    const host = el("div", "modal-panel");
    if (entry.id) host.dataset.panelId = String(entry.id);
    const phead = p.querySelector(".phead");
    const body = el("div", "pbody modal-pbody");
    if (phead) { while (phead.nextSibling) body.appendChild(phead.nextSibling); }
    else { while (p.firstChild) body.appendChild(p.firstChild); }
    host.appendChild(body);
    if (p.dataset.editable === "1" && p._levainEdit) {
      host.appendChild(buildEditRow(host, p._levainEdit.entry, p._levainEdit.doc));
    }
    return { host, zone: p.dataset.zone || "", title: entry.title || "", editClass: entry.edit_class || "" };
  }

  function customModalContent(c) {
    const card = c.build();
    if (!card) return null;
    const host = el("div", "modal-panel");
    const body = el("div", "pbody modal-pbody");
    body.appendChild(card);
    host.appendChild(body);
    return { host, zone: "", title: c.title, editClass: "" };
  }

  function buildModal(entry) {
    const gen = ++modalGen;
    modalAwaiting = false;
    // A rebuild of the same open modal keeps the rows read in it open and its scroll. If an earlier rebuild's rows
    // have not landed yet its body is empty and its scroll is clamped, so that rebuild's state is inherited instead.
    const openBody = document.querySelector("#levain-modal .modal-body");
    const modalKept = modalPending || captureKept(openBody);
    buildKept = modalKept;   // a typed panel filled in this build may owe the reader a later repaint of it
    const content = modalCustom ? customModalContent(modalCustom) : (currentView ? modalContent(entry, currentView) : null);
    if (!content) { buildKept = null; closeModal(); return; }

    let overlay = document.getElementById("levain-modal");
    const firstOpen = !overlay;
    if (!overlay) {
      overlay = el("div", "modal-overlay");
      overlay.id = "levain-modal";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.setAttribute("aria-labelledby", "levain-modal-title");
      // backdrop-dismiss ONLY when both the press AND release land on the overlay
      // itself (not the card) — robust against a text-selection drag that starts inside
      // the card and ends on the backdrop, or vice-versa (neither dismisses).
      let downOnBackdrop = false;
      overlay.addEventListener("mousedown", (ev) => { downOnBackdrop = ev.target === overlay; });
      overlay.addEventListener("mouseup", (ev) => {
        if (downOnBackdrop && ev.target === overlay) closeModal();
        downOnBackdrop = false;
      });
      overlay.addEventListener("keydown", onModalKeydown);
      document.body.appendChild(overlay);
      document.body.classList.add("modal-open");
    }
    overlay.dataset.zone = content.zone;
    const box = el("div", "modal");
    const head = el("div", "modal-head");
    const h = el("h2", null, content.title);
    h.id = "levain-modal-title";
    head.appendChild(h);
    if (content.editClass) head.appendChild(el("span", "chip chip-" + content.editClass, content.editClass));
    const close = el("button", "modal-close");
    close.type = "button";
    close.textContent = "✕";
    close.title = "close";
    close.setAttribute("aria-label", "close focus view");
    close.addEventListener("click", closeModal);
    head.appendChild(close);
    const mbody = el("div", "modal-body");
    // .modal-body is ALWAYS the scroll container (the panel renders unbounded inside it),
    // so make it keyboard-focusable + named — a text-only panel (e.g. an expanded State
    // section) has no other focusable child, and arrow-key scroll needs a focus target.
    // (The grid bolts this affordance to an OVERFLOWING .pbody; here the cap lives on
    // .modal-body, so the affordance moves with it — same a11y class as the Slice-1.5 catch.)
    mbody.tabIndex = 0;
    mbody.setAttribute("role", "region");
    mbody.setAttribute("aria-label", (content.title || "panel") + " — scrollable content");
    mbody.appendChild(content.host);
    box.append(head, mbody);

    // Focus management — capture the signal BEFORE replaceChildren detaches the focused
    // element. After the detach, document.activeElement falls to <body>, so a contains()
    // check run AFTER would ALWAYS be false → focus would yank to the close button on
    // EVERY rebuild, defeating the intent (a verb-in-modal user loses their place each
    // time). So: restore the episode SEARCH input across a rebuild (the search →
    // tombstone → search triage flow keeps its place); else enter the dialog at the close
    // button on first open OR when focus had been inside the modal; don't grab focus at
    // all if it was outside the modal (a background refresh while the user is elsewhere).
    // [complement + kimi L3 convergence — the bug L1/L2 reasoned about but mis-verified.]
    const oldSearch = overlay.querySelector(".ep-search-input");
    const wasSearchFocused = !!oldSearch && oldSearch === document.activeElement;
    const hadModalFocus = overlay.contains(document.activeElement);
    overlay.replaceChildren(box);
    pruneModalKinds();  // the typed panels the old modal body held end with it
    settleKept(box, modalKept);   // clause toggles, the held rows and scroll, the overflow cue: in that order
    buildKept = null;
    modalPending = modalAwaiting && gen === modalGen ? modalKept : null;   // rows still on the way take it when they land
    const newSearch = wasSearchFocused ? mbody.querySelector(".ep-search-input") : null;
    if (newSearch) newSearch.focus();
    else if (firstOpen || hadModalFocus) close.focus();
  }

  function refreshModal() {
    if (!modalKey) return;
    // a Needs-you card keeps its place (and any words typed into it) while the item still waits; it closes when the item is answered
    if (modalCustom) {
      if (modalCustom.page) return;   // an agent page is not tied to a waiting item
      if (!needsItems(((currentView && currentView.extra_panels || {}).decisions || {}).lines).some((it) => needsIdOf(it) === modalCustom.id)) closeModal();
      return;
    }
    const entry = findEntry(currentView, modalKey);
    if (!entry) { closeModal(); return; }  // the panel vanished from the view → close
    buildModal(entry);
  }

  // A typed panel drawn into the focus modal lives exactly while its body is on the page.
  function pruneModalKinds() {
    const K = window.LevainCockpitKinds;
    if (K && typeof K.pruneModal === "function") K.pruneModal();
  }

  function closeModal() {
    const overlay = document.getElementById("levain-modal");
    if (overlay) overlay.remove();
    sessionBodies.clear();
    questionBodies.clear();
    consentBodies.clear();   // request bodies (commands, working directories) are not kept past the view
    consentGen++;
    pruneModalKinds();
    document.body.classList.remove("modal-open");
    modalKey = null;
    modalCustom = null;
    modalGlance = false;
    modalPending = null;
    modalAwaiting = false;
    modalGen++;
    let rf = modalReturnFocus;
    modalReturnFocus = null;
    // a re-render of the widgets replaces the tile that opened the view: its successor is found by the panel id
    if (rf && !document.body.contains(rf) && rf.dataset && rf.dataset.ckPanel && rf.classList.contains("w-open")) {
      rf = Array.prototype.find.call(document.querySelectorAll(".ck-card.w.w-open"), (n) => n.dataset.ckPanel === rf.dataset.ckPanel) || null;
    }
    // restore focus to the ⤢ trigger if it's still in the DOM (an intervening board
    // re-render may have replaced it — then focus simply falls to <body>, acceptable).
    if (rf && typeof rf.focus === "function" && document.body.contains(rf)) rf.focus();
  }

  function onModalKeydown(ev) {
    if (ev.key === "Escape") { ev.preventDefault(); closeModal(); return; }
    if (ev.key !== "Tab") return;
    // a minimal focus trap — keep Tab within the dialog. Filter to VISIBLE nodes:
    // an edit/verb trigger hidden via style.display="none" (not [disabled]) still
    // matches the selector but can't take focus, so leaving it as first/last would
    // break the wrap and let Tab escape the dialog (codex L3). getClientRects().length
    // is 0 for a display:none element.
    const overlay = document.getElementById("levain-modal");
    if (!overlay) return;
    const f = Array.prototype.filter.call(
      overlay.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
        'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'),
      (n) => n.getClientRects().length > 0);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1], active = document.activeElement;
    if (ev.shiftKey && (active === first || !overlay.contains(active))) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && (active === last || !overlay.contains(active))) { ev.preventDefault(); first.focus(); }
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

  // Number keys switch the zone tab (1 All, 2 Identity, 3 Operate, 4 Mind) unless typing in a field.
  document.addEventListener("keydown", (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const t = ev.target;
    if (ev.defaultPrevented || modalKey) return;   // not behind an open overlay
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    const idx = "1234".indexOf(ev.key);
    if (idx < 0 || ev.key.length !== 1) return;
    const tabs = document.querySelectorAll(".tab");
    if (tabs[idx]) tabs[idx].click();
  });

  // Is the operator mid-edit/capture with text that an involuntary re-render would
  // discard? (spore-108) render() rebuilds the whole #board (+ modal) via
  // replaceChildren, so a visibilitychange / refresh-button re-read fired while a
  // textarea is open silently loses the typing. The boot shim calls this to DEFER those
  // passive re-reads — NOT the save's own commit→load reload, which intentionally
  // rebuilds to show saved state (that's the call-site split that avoids breaking save).
  //   - an edit-of-existing (`.edit-ta` State/config, `.verb-edit-ta` spore text) is
  //     dirty once its value diverges from the `data-orig` stamped at open;
  //   - a capture/free-text box (`.tray-dump-ta` Tray dump / Keep add-note; `.verb-ref`
  //     the ascend "what it became" ref — both always-empty at open, never prefilled) is
  //     dirty while it holds any non-whitespace text.
  // DECIDED exclusions (not blind spots — L1 review):
  //   - filter/search inputs (open-loops filter, episode search): ephemeral, already
  //     re-applied across renders, so they must not hold off a refresh;
  //   - `.verb-date` (schedule picker): a date is a 2-click re-pick (low loss) AND it's
  //     PREFILLED from an existing `next:`, so a non-empty check would false-positive on
  //     an untouched form — not worth a `data-orig` stamp for the stakes;
  //   (the masthead rename `.name-editor` USED to be excluded here — it survives a board
  //   rebuild since it lives outside `#board` — but render() now TEARS a data-saving editor
  //   down to close the rename, so a passive reload firing mid-save could strip it early and
  //   flash the OLD name; it is gated below with `.state-editor`, spore-280.)
  // Fails SAFE: a missing `data-orig` reads as dirty → defers a harmless passive re-read
  // rather than risking data loss (the boot shim's editInProgress fails closed the same way).
  function hasUnsavedEdit() {
    // A live async-job poll is "unsaved work" for reload-deferral purposes: a passive rebuild would
    // discard the polling box + its pending result mid-consult (the job keeps running server-side,
    // but the operator loses the handle). Defer the rebuild until the poll terminates.
    if (_activeJobs.size > 0) return true;
    // An open masthead state editor is unsaved work — a passive reload must defer (not
    // rebuild #state out from under the half-typed input). The authoritative SAVE reload
    // is passive:false, so it is NOT gated here and still refreshes (see load() in the boot
    // layer + the renderState data-saving exception).
    if (document.querySelector(".state-editor")) return true;
    // An open masthead RENAME editor is unsaved work too, and for the same reason render()
    // now tears a saving one down: a passive reload must defer or it could strip the editor
    // mid-save and flash the old name (or drop the half-typed one). The authoritative SAVE
    // reload is passive:false → NOT gated here → still rebuilds #entity (spore-280).
    if (document.querySelector(".name-editor")) return true;
    // a text panel's editor is prefilled with the current text: it is dirty only when it differs from that text (a closed
    // one, or one the person has not touched, must not hold off every refresh), and it is not an empty compose box
    for (const ta of document.querySelectorAll(".edit-ta, .verb-edit-ta, .text-editor textarea")) {
      if (ta.value !== ta.dataset.orig) return true;
    }
    for (const ta of document.querySelectorAll(".tray-dump-ta, .verb-ref, .question-words")) {
      if (ta.closest(".text-editor")) continue;
      if (ta.value.trim() !== "") return true;
    }
    return false;
  }

  // Export the entry points onto a namespace the surface shim calls. `jobsActive` lets the boot
  // layer DEFER a board reload while an async job is polling (a reload would detach the live result
  // box) — flushed via the injected onJobsIdle when the last job finishes (L3 codex MED).
  // A refetch repaints a panel's live copies in place and builds a fresh view of just that panel; the page's own current view
  // takes that panel from it, so an expand opened afterwards lists what the tile counts and offers the verbs the rows now offer.
  function adoptPanel(pid, fresh) {
    if (!currentView || !fresh || !fresh.layout || !currentView.layout) return;
    const i = currentView.layout.findIndex((e) => e.id === pid), j = fresh.layout.findIndex((e) => e.id === pid);
    if (i < 0 || j < 0 || !fresh.extra_panels || !fresh.extra_panels[pid]) return;
    currentView.layout[i] = fresh.layout[j];
    currentView.extra_panels[pid] = fresh.extra_panels[pid];
    const rows = currentView.targets.rows;
    for (const k of Object.keys(rows)) if (rows[k].panel_id === pid) delete rows[k];
    for (const k of Object.keys(fresh.targets.rows)) if (fresh.targets.rows[k].panel_id === pid) rows[k] = fresh.targets.rows[k];
    if (fresh.targets.panels[pid]) currentView.targets.panels[pid] = fresh.targets.panels[pid]; else delete currentView.targets.panels[pid];
    if (pid === "decisions") {
      try { renderNeeds(currentView); } catch (e) { /* the strip keeps what it showed */ }
      try { renderNotes(currentView); } catch (e) { /* the notes strip keeps what it showed */ }
    }
  }
  // A reload (the tab coming back, the refresh button, a stream reload) re-renders the whole board: emptying it
  // clamps the page's scroll to the top, so the reader's place is put back once the board is drawn again.
  function renderKeepingPlace(view, opts) {
    const y = window.scrollY || 0;
    try { return render(view, opts); }
    finally {
      if (y > 0 && window.scrollY !== y) window.scrollTo(window.scrollX, y);
      ckScheduleRefresh();   // what the draw left on screen (the tab, the jobs) decides which moves are live
    }
  }

  window.LevainAgentPage = { open: openAgentPage };
  window.LevainNeeds = { items: needsItems, name: needsName, summary: needsSummary, go: needsGo, history: openDecisionsHistory };
  window.LevainDashboard = { render: renderKeepingPlace, redraw: ckRedraw, adoptPanel, captureKept, settleKept, buildKept: () => buildKept, renderPanel: renderPanelSafe, measureClauses, measureOverflow, hasUnsavedEdit, jobsActive: () => _activeJobs.size > 0, fitLineTile, lineTileCount: () => lineFits.size, noteBodyCount: () => noteBodies.size };
})();
