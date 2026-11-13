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
  if (window.LevainCardFormat && typeof window.LevainCardFormat.installFieldGrow === "function") window.LevainCardFormat.installFieldGrow();
  // the card's commands are drawn by this page's own copy-line builder (copyLine, below), not a second one in card_format.js
  if (window.LevainCardFormat && typeof window.LevainCardFormat.setCommandBlock === "function") window.LevainCardFormat.setCommandBlock(copyLine);

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
  // The NEEDS YOU band (Q17-ALL S5b, the manifest source only): `bandRead() → Promise<band>` shows the band to this cockpit
  // credential (numbers it, records the view); `answerBand(text, storeId, viewId) → Promise<{ok}|{ok:false,...}>` answers it.
  let bandRead = null;
  let answerBand = null;
  // Dismiss an agent's note (the manifest source only): `dismissNote(rowId) → Promise<{ok}|{ok:false,...}>`. It tells the agent nothing.
  let dismissNote = null;
  // Clear or reopen a desk review item (the manifest source only): `reviewItem(rowId, verb) → Promise<{ok}|{ok:false,...}>`.
  let reviewItem = null;
  let pollJob = null;
  // The episode-search READ transport (GET /recall.json), injected by the boot layer so the core
  // stays token-agnostic — the boot layer attaches the off-box token (intention-220 gates /recall.json
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
  // focus modal. Empty = show all. Matches intention text OR intention-id.
  let openLoopsQuery = "";
  const intentionQueries = Object.create(null);  // Tray / Open loops filter queries, keyed by panel kind
  // The Tray's chosen view ("" = the kernel's own order, else a view id) and the Episodes agent filter
  // ("" = all). Both are display choices that survive a refresh re-render in the same page.
  let trayView = "";
  // The List's surface filter ("" = all, else band | board | intention | talk | drop) and the intake box's kept state: the receipt
  // line under the box and an unsent line. A landed write re-reads the page and rebuilds the box, so both live here, not in the DOM.
  let listSurface = "";
  // The List's time filter ("" = every time-state, else now | later | someday | done): it cuts across the type chips above.
  let listTime = "";
  // the List card's way to draw again from the state it shares with the focus view; closing that view calls it, so what the card
  // says is what its control opens
  let listCardRedraw = null;
  const intakeKept = { receipt: null, draft: "" };   // receipt: { text, cls } | null
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
  // The narrow page's jump into the Decisions view runs one wait at a time: a newer tap, the reader's own touch or wheel, or the
  // view closing ends the run in flight, so nothing yanks the view back. Declared here because closeModal ends it.
  let phoneJumpRun = 0;

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
  // A redraw replaces the tile the reader had focused (or is about to get back from a closed focus view) before its successor is openable
  // (a count tile gets its button role when its body has been fitted, a moment after the draw): the ask waits for that tile and gives
  // focus to it then, only while focus is still on the page body, so it never takes focus from where the reader went in the meantime.
  let focusAsk = null;
  // the ask names a DOM dataset value (always a string) and the tile model names its panel by e.id (a string or a number): both sides compare as strings
  function askTileFocus(id) { focusAsk = id ? { id: String(id), until: Date.now() + 3000 } : null; }
  // the reader's own press or key is an answer to where focus should be: a pending ask is dropped on it (capture phase, so a handler that
  // the same input runs, such as Esc closing a focus view, still arms its own ask after this one has cleared). Added once, at load.
  if (typeof document !== "undefined" && document && typeof document.addEventListener === "function") {
    for (const t of ["pointerdown", "keydown"]) document.addEventListener(t, () => { focusAsk = null; }, true);
  }
  function giveTileFocus(node, id) {
    if (!focusAsk || focusAsk.id !== String(id)) return;
    if (Date.now() > focusAsk.until || (document.activeElement && document.activeElement !== document.body)) { focusAsk = null; return; }
    focusAsk = null;
    if (typeof node.focus === "function") node.focus({ preventScroll: true });
  }
  function render(view, opts) {
    // The surface injects its write transport here; a port that provides none (the
    // parked MCP-App) renders read-only — no edit affordances, zero other change.
    commit = opts && typeof opts.commit === "function" ? opts.commit : null;
    commitAction = opts && typeof opts.commitAction === "function" ? opts.commitAction : null;
    commitJob = opts && typeof opts.commitJob === "function" ? opts.commitJob : null;
    cancelPending = opts && typeof opts.cancelPending === "function" ? opts.cancelPending : null;
    answerQuestion = opts && typeof opts.answerQuestion === "function" ? opts.answerQuestion : null;
    bandRead = opts && typeof opts.bandRead === "function" ? opts.bandRead : null;
    answerBand = opts && typeof opts.answerBand === "function" ? opts.answerBand : null;
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
    { const ae = document.activeElement;   // a focused open tile is replaced by this draw: its successor takes the focus
      if (ae && ae !== document.body && ae.dataset && ae.dataset.ckPanel && ae.classList.contains("w-open")) askTileFocus(ae.dataset.ckPanel); }
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
      // A rename editor open in the masthead (intention-280 — the pre-existing twin of the 247
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
  const CLAMP_KINDS = new Set(["config", "section", "episodes", "wraps", "crystals", "intentions", "tray", "external", "edits"]);

  // Measure whether a bounded body overflows its cap; set the overflow SCENT (.has-overflow)
  // AND a keyboard scroll affordance accordingly. RE-RUNNABLE — clears the state when content
  // no longer overflows (search shrinking results, font reflow) so the scent never goes stale.
  // Reads layout synchronously; valid because render() builds into the live #board, so the
  // .clamped max-height is actually applied (a detached board would read zero overflow).
  // The work lists draw whole rows and say how many they leave out: a list that does not fit its card folds the rows below the fold
  // (display none, never a half row) and ends in an "N more" link that opens the panel's focus view. Re-runnable: a redraw, a filter
  // or a resize unfolds first and measures again.
  const FOLD_PANELS = ["tray", "loops", "episodes", "inbox", "roster", "stream", "flow-episodes", "flow-intentions"];
  // what a folding list is made of: its rows (a work row, a roster row, a log line) and the headings that group them
  const FOLD_ROW = ".row, .rrow, .lrow", FOLD_HEAD = ".grp-head, .grp-h";
  function foldRows(body) {
    const panel = body.closest && body.closest(".panel[data-ck-panel]");
    if (!panel || (FOLD_PANELS.indexOf(panel.dataset.ckPanel) < 0 && !body.querySelector(".fl-group")) || panel.closest(".modal")) return false;   // a Fleet card folds whatever its panel id is
    // the reading place survives every fold trigger (a row opening, a search, fonts settling): unfolding and folding again
    // changes the body's height, and the browser clamps or anchors the scroll under the reader
    const place = body.scrollTop;
    try { return foldRowsAt(body, panel); } finally { if (body.scrollTop !== place) body.scrollTop = place; }
  }
  // The List card's one control: what the card folds, then what it leaves to the focus view ("26 more · desk owes 5 · later 3 ·
  // 18 held", only the non-zero parts), and it opens the focus view.
  function listApart(panel) {
    try {
      const a = JSON.parse(panel.dataset.listApart || "null");
      return a && (a.desk > 0 || a.later > 0 || a.done > 0) ? a : null;
    } catch (e) { return null; }
  }
  function listMore(panel, hidden, a) {
    const parts = [];
    if (hidden > 0) parts.push(hidden + " more");
    if (a.desk > 0) parts.push("desk owes " + a.desk);
    if (a.later > 0) parts.push("later " + a.later);
    if (a.done > 0) parts.push("done " + a.done);
    const more = el("button", "more-foot", parts.join(" · "));
    more.type = "button";
    more.dataset.listMore = "1";
    more.setAttribute("aria-label", parts.join(", ") + ": open the full list");
    more.addEventListener("click", () => {
      const x = panel.querySelector(".pexpand");
      if (x) x.click();
      modalReturnFocus = more;      // closing the focus view lands back on the control he pressed (its successor is found by closeModal)
    });
    panel.appendChild(more);
    return hidden > 0;
  }
  function foldRowsAt(body, panel) {
    for (const n of body.querySelectorAll("[data-folded]")) { n.style.display = ""; delete n.dataset.folded; }
    const old = panel.querySelector(":scope > .more-foot:not([data-readall])"); if (old) old.remove();   // the "Read all" foot is memFoot's: removing it here made memFoot re-add it, a childList change that re-ran this, without end
    const apartNow = listApart(panel);
    if (!(body.scrollHeight > body.clientHeight + 1)) return apartNow ? listMore(panel, 0, apartNow) : false;
    const top = body.getBoundingClientRect().top;
    const limit = body.clientHeight - 34;   // room kept for the link
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
    if (!hidden) return apartNow ? listMore(panel, 0, apartNow) : false;
    // a list that has more rows than it first draws ends in its own "show all N" link; once the card folds, two links to the same rest would
    // disagree on the count, so the card keeps one: the foot, saying how many the focus view holds
    const xm = body.querySelector(".xmore"), total = xm ? parseInt((/\d+/.exec(xm.textContent) || [])[0], 10) : NaN;
    if (xm) { xm.style.display = "none"; xm.dataset.folded = "1"; }
    if (apartNow) return listMore(panel, hidden, apartNow);
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
  // a failed hero render: the CONTENT mounts are emptied (stale sky beside the error line would read as current);
  // the structural mounts (#ck-hero, #ck-band, #ck-skycol) stay for the next render
  function restoreHero() {
    for (const id of ["ck-when", "ck-sky", "ck-legend", "ck-wrapline"]) { const m = document.getElementById(id); if (m) { m.replaceChildren(); if (id === "ck-legend") m.hidden = true; } }
  }
  function keShell(on) {
    const hero = document.getElementById("ck-hero"), brief = document.getElementById("ck-brief"), widgets = document.getElementById("ck-widgets");
    if (!hero || !brief || !widgets) return;
    hero.hidden = brief.hidden = widgets.hidden = !on;
    const dropHost = document.getElementById("ck-drop");
    if (dropHost && !on) dropHost.hidden = true;   // the Send box sits under the Needs-you row, outside #ck-hero: it leaves with the KE page, and the next KE render (renderDropHero) shows it again
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

  // one bare node (no panel header): the hero's sky, the brief, the widgets. It draws what a zone card draws
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
    const sky = entries.find((e) => kindOf(e) === "ambient");
    // the now band: the clock and date at the left, the state line beside them (placed by renderBriefRegion), and at the right the
    // wrap line (always something real: the last wrap, or "No wraps yet" and a fact the install has), and the sky's event count.
    // A bare install has no sky and the band is still whole.
    const clock = el("div", "clock num"), date = el("div", "date"), events = el("div", "events");
    clock.dataset.ckHero = "clock"; date.dataset.ckHero = "date"; events.dataset.ckHero = "events";
    // everything that can throw is built off the page; the structural mounts (when, wrap line, sky)
    // are touched only once all of it exists
    const whenKids = [], metaKids = [], skyKids = [];
    const stamp = el("div", "clockbox");
    stamp.append(clock, date);
    whenKids.push(stamp);
    const lastWrap = wrapLineOf(view);
    metaKids.push(lastWrap);
    metaKids.push(events);
    if (sky) skyKids.push(bareNode(sky, view, "ck-sky", { hero: { events: events, place: legendHook.place, hide: legendHook.hide } }));
    for (const e of entries) if (e !== sky) {
      const card = bareNode(e, view, "ck-card");
      if (typeof e.title === "string" && e.title) card.insertBefore(el("div", "needs-sec-label", e.title), card.firstChild);
      skyKids.push(card);
    }
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
          giveTileFocus(node, e.id);
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

  // The Board is two independent stacks, not rows that pair: Tray down the left, Open loops down the right, each as
  // tall as its own cards, so the shorter card of a pair never leaves a hole. Other tabs lay the same cards out as plain grid items
  // (the stacks are display: contents there). Only a zone that holds both sides is stacked; a card the table does not name stays a
  // direct, full-width child.
  const CK_STACK_SIDE = Object.assign(Object.create(null), { tray: 0, loops: 1 });
  // Top to bottom inside a side when nothing is saved (the CSS used to draw this with `order`, which would override a saved order).
  const CK_STACK_RANK = Object.assign(Object.create(null), { tray: 0, loops: 0 });
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
  // A card of the page's own (Tray, Identity) keeps its card: its box is its content. An empty list card folds in
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
    for (const e of view.layout) if (e.id !== DROP_THREAD_PANEL) byId.set(e.id, e);   // the drop card hosts that panel: no zone, tile or hero draws it
    for (const old of document.querySelectorAll("#ck-hero > .ck-region-err")) old.remove();   // a clean hero render clears its last error line
    guardRegion("the hero", () => renderHero(view, byId), regionErr("ck-hero", false), restoreHero);
    guardRegion("the brief", () => renderBriefRegion(view, byId), regionErr("ck-brief", true), restoreState);
    guardRegion("the desk drop", () => renderDropHero(view), regionErr("ck-drop", false));
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
      case "intentions": return renderIntentions(entry, view);
      case "tray": return renderTray(entry, view, opts);
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

  // Append a clause + its per-item expand toggle (shared by intention + episode rows). The
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
  // A date (2026-10-15) inside already-drawn inline nodes never breaks at its hyphens: each one in a text node becomes a nowrap span. Code spans
  // are left as written.
  function nowrapDates(node) {
    for (const c of Array.from(node.childNodes)) {
      if (c.nodeType === 3) {
        const parts = c.nodeValue.split(/(\b\d{4}-\d{2}-\d{2}\b)/);
        if (parts.length < 2) continue;
        const f = document.createDocumentFragment();
        parts.forEach((p, i) => { if (i % 2) f.appendChild(el("span", "nowrap", p)); else if (p) f.appendChild(document.createTextNode(p)); });
        node.replaceChild(f, c);
      } else if (c.nodeType === 1 && c.tagName !== "CODE") nowrapDates(c);
    }
  }
  // `md` draws the whole text through the inline markdown renderer (a count list's provider rows: bold, italic, code, links), where the
  // default opens only code spans; such a row also clamps to two lines collapsed (a headline), the caret opening the rest.
  function appendClause(row, text, md) {
    // .clampable is what the CSS clamps + measureClauses targets — bare .clause (crystals,
    // wrap/edit labels) stays full so it never clamps without this expand affordance.
    const clause = el("span", "clause clampable");
    if (md) {
      mdInline(String(text != null ? text : ""), clause, 0);
      nowrapDates(clause);
      row.classList.add("md-row");
    } else {
      // a date (2026-10-15) never breaks at its hyphens: each one is a nowrap span, the text around it plain text
      const parts = String(text != null ? text : "").split(/(\b\d{4}-\d{2}-\d{2}\b)/);   // nullish text draws an empty clause, as el() does
      parts.forEach((part, i) => {
        if (i % 2) clause.appendChild(el("span", "nowrap", part));
        else if (part) codeSpansInto(clause, part);
      });
    }
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

  // One intention row, shared by the two projections (Open Loops / Tray). The
  // leading chip is the panel's salient axis: Tray badges the `disposition` (the
  // operator-I/O class — seed/handoff/agenda), Open Loops badges the `tier` (a parked loop badges "parked"). `verbs`
  // rides only on a Class-B panel (Open Loops in 3a; Tray renders read-only until 3b
  // turns on the governed dump/sort/park verbs — edit_class "" → isVerbPanel false → none).
  // The ONE place a verb-bearing row takes its verbs: every row kind (intention, episode, pending cancel) passes through
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
  // The words of a List row's surface chip. A drop says when it fires, read from the kernel's own minute ("YYYY-MM-DDTHH:MM", a
  // calendar label only: no zone, no comparison with now).
  const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function listChipText(s) {
    if (s.surface === "drop" && s.fires) {
      const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})$/.exec(s.fires);
      if (m) return "drop " + m[4] + " " + WEEKDAYS[new Date(+m[1], +m[2] - 1, +m[3]).getDay()] + " " + m[2] + "-" + m[3];
    }
    return s.surface;
  }
  function intentionRow(s, verbs, badgeField, panelKind) {
    const row = el("div", "row");
    if (s.row_id) row.dataset.rowKey = s.row_id;   // where a transport notice lands after the board is rebuilt
    // A List row says which store it comes from. A band or board row is NOT an intention (an operator question, a ready work item): it
    // shows no intention id and no intention verb, whatever the row's actions say.
    const foreign = s.surface === "band" || s.surface === "board";
    // a List row keeps its meta (id, surface chip, tags) in ONE grid cell that wraps: the row grid has four
    // fixed meta columns, and a drop row carries more than four small facts
    const host = s.surface ? el("span", "list-meta") : row;
    if (s.surface) row.dataset.surface = s.surface;
    if (s.surface && s.time_state) row.dataset.timeState = s.time_state;
    // the intention id is the common reference handle (how flow tracks + you name it,
    // e.g. "compost intention-049") — show it leading so the row is addressable; the TUI
    // shows the same id trailing (tui.render_panel_lines).
    // A List row's pill names THAT ROW'S OWN origin: its repo, or the lowercase "<origin>:" its text leads with ("desk: ...",
    // "levain: ..."). Never the store the row sits in (the pointer already says that) and never a repo its words merely mention;
    // a row with no origin wears no pill. It leads the meta line, inline, so it wraps with the rest of it.
    let originDrawn = false;
    if (s.surface) {
      const lead = /^([a-z][a-z0-9_-]{1,23}):\s/.exec(String(s.text || ""));
      // the lead word is an origin only when it is a repo this page already knows (the sky's registry) or "desk" / "flow":
      // "note: call Alice" and "re: invoice" lead with a word, not an origin
      const leadOrigin = lead ? knownOrigin(lead[1]) : "";
      const origin = (typeof s.repo === "string" && s.repo) || (!foreign && typeof s.domain === "string" && s.domain) || leadOrigin;
      originDrawn = !!origin;
      if (origin) { const pill = el("span", "k7-repo", String(origin)); pill.dataset.repoFacet = String(origin); pill.dataset.originPill = "1"; host.appendChild(pill); }
    }
    if (s.id && !foreign) { host.appendChild(el("span", "sid", s.id)); row.dataset.intention = String(s.id); }   // the Work lens finds a loop's row by this
    // Tray badges the disposition (a legacy note badges "note"); Open Loops the tier (a parked loop badges "parked").
    let badge;
    if (badgeField === "disposition") badge = s.disposition;
    else badge = s.tier;
    if (s.surface) {
      const chip = tagEl("tier list-surface", listChipText(s));
      chip.dataset.surfaceChip = s.surface;
      host.appendChild(chip);
    } else if (badge) host.appendChild(tagEl("tier", badge));
    appendClause(row, s.text);  // FULL text + per-item expand (no truncation, no edit data-loss)
    if (host !== row) row.appendChild(host);
    if (foreign) {
      // read-only by construction: band rows point at the Decisions panel, board rows at the agents' queue; no verbs below
      if (s.support && !s.text_locked) host.appendChild(el("span", "muted list-support", s.support));
      if (s.wake_line && s.group === "later") host.appendChild(tagEl("muted later-tag", s.wake_line));
      else if (s.later_words && s.group === "later") host.appendChild(tagEl("muted later-tag", "back " + s.later_words));   // a carried desk row is due now (needs you), never "back Mon"
      // a desk row the desk has carried past its day is listed under Needs you, tagged as flow's footer tags it
      if (s.band_section === "desk" && s.carried) host.appendChild(tagEl("muted list-desk-owed", "desk-owed"));
      // a band row that rings an intention is acted on right here (the four controls); any other points at Decisions
      const intend = s.surface === "band" && s.intention && s.row_id && hasIntentionControls(s.actions);
      if (s.list_pointer && !intend) host.appendChild(tagEl("muted list-pointer", s.list_pointer));
      if (s.surface === "board") {
        if (s.board_tier) host.appendChild(tagEl("muted", s.board_tier));
      }
      if (s.band_age) {
        const ageEl = el("span", "muted", s.band_age);
        const KK = window.LevainCockpitKinds;
        if (s.band_since && KK && KK.armAges) { ageEl.dataset.bandSince = s.band_since; KK.armAges(); }   // ticks on the page's age timer, no read
        host.appendChild(ageEl);
      }       // the footer's clock and form, never whole days from the add
      else if (s.surface !== "band" && Number.isInteger(s.age_days) && s.age_days >= 0) host.appendChild(el("span", "muted", s.age_days + " d"));
      if (intend) appendRowVerbs(row, buildIntentionVerbs(s.row_id, s.actions, s.intention_text), ["Done", "Snooze"], 2);
      // a plain band row (it rings no reminder) offers the one write the kernel names on it: make it an agenda past the snooze window
      else if (s.surface === "band" && s.row_id && hasBandAgenda(s.actions)) appendRowVerbs(row, buildBandAgendaVerb(s), ["Make an agenda"], 1);
      return row;
    }
    if (s.surface && s.time_state === "done") {
      // resolved in the last week: read-only, the day and how it closed
      host.appendChild(tagEl("muted", (s.done_kind || "done") + " " + shortDate(s.done_on || "")));
      return row;
    }
    if (s.surface) {
      // the kernel computed `overdue` and `fires`; this only says so
      if (s.wake_line) {
        const wk = tagEl("muted", s.wake_line);
        wk.setAttribute("data-ck-tag-state", "later");
        host.appendChild(wk);
      } else if (s.overdue) {
        row.dataset.band = "overdue";
        const od = tagEl("muted", "overdue");
        od.setAttribute("data-ck-tag-state", "overdue");
        host.appendChild(od);
      }
      if (!s.wake_line && s.next && !s.fires) host.appendChild(tagEl("muted", "due " + shortDate(s.next)));
      // who does it, derived from the target; the cockpit never stores it
      const exec = executorOf(rowTarget(s));
      if (exec && s.disposition === "agenda") { const w = tagEl("muted list-who", exec); w.dataset.who = exec; w.title = "who does it, from its target"; host.appendChild(w); }
      if (s.surface === "talk" && Number.isInteger(s.age_days) && s.age_days >= 0) host.appendChild(tagEl("muted", s.age_days + " d old"));
      if (s.deliver_to && s.surface === "drop" && !s.wake_line) host.appendChild(tagEl("muted", "to " + s.deliver_to));
      if (s.age_shown && s.age) host.appendChild(el("span", "muted", s.age));
      if (verbs && s.id) appendRowVerbs(row, buildPlainIntentionVerbs(s, panelKind), panelKind === "tray" ? ["Resolve", "when?"] : null, panelKind === "tray" ? 2 : 0);
      return row;
    }
    if (s.next) {
      // a late row says how late ("overdue 4 d") and keeps its date ("due 10-17"); a row due today says so
      const late = daysLate(s.next, s.today);
      if (late > 0) {
        row.dataset.band = "overdue";
        const od = tagEl("muted", "overdue " + late + " d");
        od.setAttribute("data-ck-tag-state", "overdue");
        host.appendChild(od);
        host.appendChild(tagEl("muted", "due " + shortDate(s.next)));
      } else {
        host.appendChild(tagEl("muted", late === 0 ? "due today" : "due " + shortDate(s.next)));
      }
    }
    if (panelKind !== "tray" && s.tier) row.dataset.heat = String(s.tier);   // Open loops: a stripe per heat
    if (s.age_shown && s.age) host.appendChild(el("span", "muted", s.age));
    // Build's Open loops draw "open N d" and the repo chip under the title (cockpit.css hides both elsewhere)
    // the integer the kernel sends (the intention's own created date), never the badge words parsed back
    if (Number.isInteger(s.age_days) && s.age_days >= 0) host.appendChild(el("span", "k7-opend", "open " + s.age_days + " d"));
    if (s.domain && !originDrawn) { const rc = el("span", "k7-repo", String(s.domain)); rc.dataset.repoFacet = String(s.domain); host.appendChild(rc); }
    if (verbs && s.id) appendRowVerbs(row, buildPlainIntentionVerbs(s, panelKind), panelKind === "tray" ? ["Resolve", "when?"] : null, panelKind === "tray" ? 2 : 0);
    return row;
  }

  // The filter bar the intention panels share (Open Loops / Tray): CLIENT-side over the
  // loaded list, text (case-insensitive substring) OR intention-id. The query persists per panel
  // kind in `intentionQueries`, so a verb that rebuilds the board leaves you in your filter.
  // FILTER-EXTRACT-START (a node test lifts this block)
  // opts (optional): controls(bar, redraw) adds in-panel controls to the bar; pick(list) returns the
  // rows to filter (a view's own order), re-read on every redraw; extra() returns rows listed apart that are
  // filtered with the rest; empty is the message shown when nothing is listed and there is no query;
  // unlisted() returns rows that are not listed now (held back) but would be searched, so an empty result
  // can say how many of them match the query and offer to show them (hintShow(n, show) builds that offer).
  function mountIntentionFilter(p, all, noun, kind, drawRows, opts) {
    const bar = el("div", "ep-search");
    const input = el("input", "ep-search-input");
    input.type = "search";
    narrowPlaceholder(input, "filter " + noun + " by text or intention-id…", "filter by text or id…");
    input.setAttribute("aria-label", "filter " + kind + " by text or intention id");
    const status = el("span", "ep-search-status", "");
    bar.append(input, status);
    p.appendChild(bar);
    const results = el("div", "sp-results");
    p.appendChild(results);
    const render = (raw) => {
      const q = raw.trim().toLowerCase();
      const picked = opts && opts.pick ? opts.pick(all) : all;
      const base = opts && opts.extra ? picked.concat(opts.extra()) : picked;   // rows listed apart, filtered with the rest
      const plain = (s) => String(s.text == null ? "" : s.text).toLowerCase().includes(q) ||
                         String(s.support || "").toLowerCase().includes(q) ||
                         String(s.id || "").toLowerCase().includes(q);
      const hit = opts && opts.match ? (s) => opts.match(s, raw) : plain;
      const rows = q ? base.filter(hit) : base;
      results.replaceChildren();
      if (rows.length === 0) {
        const note = el("p", "empty", q || !(opts && opts.empty) ? "no " + noun + " match “" + raw.trim() + "”" : opts.empty);
        const unlisted = q && opts && opts.unlisted ? opts.unlisted().filter(hit).length : 0;
        if (unlisted && opts.hintShow) opts.hintShow(note, unlisted, opts.unlisted().filter(hit));
        results.appendChild(note);
      } else drawRows(rows, results);
      status.textContent = q ? (rows.length + (rows.length === 1 ? " match" : " matches") + " · ⌫ to clear") : "";
      const b = results.closest(".pbody"); if (b) measureOverflow(b);
      measureClauses(results);
    };
    input.addEventListener("input", () => { intentionQueries[kind] = input.value; render(input.value); });
    if (opts && opts.controls) opts.controls(bar, () => render(input.value));
    if (intentionQueries[kind]) input.value = intentionQueries[kind];
    render(input.value);
  }

  // FILTER-EXTRACT-END

  // The List's rows are matched the way the kernel matches memory (levain/cockpit/engine.py `_fold` and `_terms_match`: casefold, NFD,
  // marks dropped; the query split on whitespace; EVERY term a substring of the row's text, support and id joined), so one query is
  // one result set across the rows here and the "Kept in memory" group beside them. (`toLowerCase` for `casefold`: they differ only
  // for a few letters such as the German sharp s.) Used by the List only; Open Loops and the Tray keep the plain substring above.
  // `listQueryText` is the ONE normalisation of what is typed: control characters are spaces, at most KEPT_QUERY_MAX characters,
  // trimmed; the rows and the memory read both take this, never the raw box.
  const KEPT_QUERY_MAX = 200;
  const listQueryText = (raw) => Array.from(String(raw == null ? "" : raw).replace(/[\u0000-\u001f\u007f]/g, " ")).slice(0, KEPT_QUERY_MAX).join("").trim();
  const foldText = (t) => String(t == null ? "" : t).toLowerCase().normalize("NFD").replace(/\p{Mn}/gu, "");
  let listTermsFor = "", listTerms = [];
  function listRowMatches(s, raw) {
    if (raw !== listTermsFor) { listTermsFor = raw; listTerms = foldText(listQueryText(raw)).split(/\s+/).filter(Boolean); }
    if (!listTerms.length) return true;     // no searchable term (blank, or only marks): no query, as the kernel reads it
    const hay = foldText([s.text, s.support, s.id].map((x) => (x == null ? "" : String(x))).join(" "));
    return listTerms.every((t) => hay.includes(t));
  }

  // The three intention projections share one renderer — same read/verb contract, different
  // source list + badge axis + empty copy. All three read their fault from the single
  // `open_intentions` error key (the dashboard does ONE intention read that fills all three).
  // a group heading in a list is a heading to a screen reader too (VoiceOver rotor), the look is the same
  function headingEl(text) {
    const h = el("div", "grp-head", text);
    h.setAttribute("role", "heading");
    h.setAttribute("aria-level", "3");
    return h;
  }
  // the heading the kernel gives the List's desk group; a carried desk row's second listing wears it when no desk row sent it
  const DESK_OWES_TITLE = "Desk owes (the desk is on these; nothing for you)";
  function renderIntentionProjection(entry, view, list, emptyMsg, badgeField, ropts) {
    const p = panel(entry);
    // Both projections (Open Loops / Tray) come from ONE dashboard intention read,
    // whose fault is recorded under the single "open_intentions" key — so every projection
    // reads its error from there BY DESIGN (not a copy-paste slip). If a future split gives
    // Tray its own source, give each its own error key here. [complement L3 LOW]
    const err = tierErr(view, "open_intentions");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    if (!list || list.length === 0) {
      // the List with no live row still holds its search: what it kept as memory is searched from here, and the box says "no items match"
      // beside the memory group, as it does with rows. (The other panels have nothing to filter and say only that they are empty.)
      if (entry.kind === "tray" && view.tray_is_list === true) {
        mountIntentionFilter(p, [], "items", entry.kind, () => {}, { empty: emptyMsg, match: listRowMatches });
        mountListKept(p);
      } else p.appendChild(el("p", "empty", emptyMsg));
      return p;
    }
    const verbs = isVerbPanel(entry);
    // the Tray's views: the kernel names each ordering and sends the row ids in it; this only picks one
    const views = entry.kind === "tray" && Array.isArray(view.tray_views) && view.tray_view_rows ? view.tray_views : [];
    const viewOf = () => (views.some((v) => v.id === trayView) ? trayView : "");
    // the List: rows that carry a `surface` get the surface filter (the kernel names the stores; the page only filters by them)
    const isList = entry.kind === "tray" && view.tray_is_list === true;   // the panel offers list_intake: a marker, never the rows' contents
    // The List's card holds what is due for the operator and what else fits; everything else it holds (the desk's rows, the rows
    // that come back on a later day) is listed in the focus view, under its own heading, and the
    // card names them in ONE control that opens it.
    const inCard = isList && !(ropts && ropts.modal);
    const apart = (s) => s.group === "desk" || s.group === "later" || s.group === "done";
    const cardHides = (s) => inCard && apart(s);
    // what the card leaves to the focus view, counted the way the focus view draws it for the state now chosen (the type and time
    // chosen carry over to it); a function of that state, read again on every rebuild
    let apartCount = { desk: 0, later: 0, done: 0 };
    const cardHasApart = inCard && (list || []).some(apart);
    // ONE rule for "all", in the card and the focus view: the rows in this view's own list (the card's without the desk's and the
    // later ones it names in its control, the focus view's with them). So the header, the "all" chip and the control's numbers
    // follow from each other.
    const listAll = () => (list || []).filter((s) => !cardHides(s));
    const withCarriedDesk = (rows) => {
      const copies = rows.filter((s) => s.group === "band" && s.band_section === "desk" && s.carried)
        .map((s) => Object.assign({}, s, { group: "desk", group_title: DESK_OWES_TITLE }));
      if (!copies.length) return rows;
      const at = rows.findIndex((s) => s.group !== "band");     // Desk owes follows Needs you, ahead of the rows already in it
      return at < 0 ? rows.concat(copies) : rows.slice(0, at).concat(copies, rows.slice(at));
    };
    // the time-state a row is in, as the kernel computed it; the focus view's time chips filter by it, across the type chips
    const timeOf = (s) => s.time_state || "now";
    // the chosen named ordering: the ids the kernel sent for it, or null
    const viewIds = () => { const ids = views.length && viewOf() ? view.tray_view_rows[viewOf()] : null; return Array.isArray(ids) ? ids : null; };
    // THE rows of the List for a state, and the only place they are chosen: the projection (a carried desk row listed twice in the
    // focus view, once in a chosen view, which is ungrouped), then the chosen view's order, then the type, then the time. The body
    // draws rowsFor(state) and every chip's number is rowsFor(state with that chip).length, so a number is the rows its tap draws.
    // focus: the rows of the focus view for that state, asked from the card (the card counts what its one control opens)
    const rowsFor = (st, focus) => {
      let rows = focus ? (list || []) : listAll();
      if (isList && (focus || !inCard) && !st.view) rows = withCarriedDesk(rows);
      if (st.view) {
        const byId = new Map(rows.map((s) => [s.id, Object.assign({}, s, { age_shown: true })]));
        const named = new Set(st.view);
        rows = st.view.filter((id) => byId.has(id)).map((id) => byId.get(id))
          .concat(rows.filter((s) => !named.has(s.id)).map((s) => byId.get(s.id)));
      }
      if (st.surface) rows = rows.filter((s) => s.surface === st.surface && inSurfaceChip(s));
      if (st.time) rows = rows.filter((s) => timeOf(s) === st.time);
      return rows;
    };
    const timeNow = () => (isList && !inCard && LIST_TIMES.indexOf(listTime) >= 0 ? listTime : "");
    // a type chosen earlier that the chosen time and view have no rows in is no choice at all
    const listNow = () => (isList && rowsFor({ surface: listSurface, time: timeNow(), view: viewIds() }).length > 0 ? listSurface : "");
    const stateNow = () => ({ surface: listNow(), time: timeNow(), view: viewIds() });
    const countApart = () => {
      const st = { surface: listSurface, time: LIST_TIMES.indexOf(listTime) >= 0 ? listTime : "", view: viewIds() };
      let rows = rowsFor(st, true);
      if (!rows.length && st.surface) rows = rowsFor(Object.assign({}, st, { surface: "" }), true);     // the focus view drops a type that has no rows
      const n = (g) => rows.filter((s) => s.group === g).length;
      apartCount = { desk: n("desk"), later: n("later"), done: n("done") };
      if (inCard) p.dataset.listApart = JSON.stringify(apartCount);
    };
    countApart();
    if (isList && !inCard) p.dataset.listTotal = String(rowsFor({ surface: "", time: "", view: null }).length);   // the focus view's header is the unfiltered List
    let rebuildChips = null;   // a chosen view changes every chip's number: the chips are rebuilt with it
    let chipRow = null;     // the chips' row, drawn by controls(); the panel's own element is empty once the focus view moves its body
    const opts = views.length || isList ? {
      // the List's rows are matched as memory is (see listRowMatches); the other panels keep the plain substring
      match: isList ? listRowMatches : undefined,
      // a card whose due rows are all done still holds the desk's and the later ones: it says nothing is due, not that it is empty
      empty: cardHasApart ? "Nothing due for you." : emptyMsg,
      // the card's search also reads what the card leaves to the focus view, so a match there is told, never "no items match"
      unlisted: () => (inCard ? (list || []).filter(cardHides).filter((s) => !listNow() || s.surface === listNow()) : []),
      hintShow: (note, n, hits) => {
        note.appendChild(document.createTextNode(" — " + n + (n === 1 ? " row matches in the full list — " : " rows match in the full list — ")));
        const go = el("button", "list-open-match");
        go.type = "button";
        go.dataset.listOpenMatches = "1";
        go.textContent = "open it";
        go.addEventListener("click", () => {
          // the focus view opens on the time the matched rows live in (all times when they span several), so the match is drawn
          const states = new Set((hits || []).map(timeOf));
          listTime = states.size === 1 ? Array.from(states)[0] : "";
          const m = p.querySelector("[data-list-more]");
          if (m) m.click();
          else { const x = p.querySelector(".pexpand"); if (x) x.click(); }     // a card with no footer still opens its focus view
        });
        note.appendChild(go);
      },
      controls: (bar, redraw) => {
        let orderSync = null;     // set once the ordering select exists
        if (isList) {
          // every number reads rowsFor, with the chip's own value in place of the one chosen
          let timeRow = null;
          // the shared choices the card last drew from: closing a view that left them unchanged redraws nothing
          const listChoice = () => listSurface + "\u0000" + listTime + "\u0000" + trayView;
          let drawnChoice = null;
          const build = () => {
            if (orderSync) orderSync();     // the ordering select shows the ordering chosen now, wherever it was chosen
            if (inCard) drawnChoice = listChoice();
            const was = document.activeElement;
            const at = was && was.dataset && ((chipRow && was.closest(".list-surfaces") === chipRow) || (timeRow && was.closest(".list-times") === timeRow))
              ? (was.dataset.surface !== undefined ? ["surface", was.dataset.surface] : was.dataset.time !== undefined ? ["time", was.dataset.time] : null) : null;
            countApart();
            const st = stateNow();
            const fresh = buildSurfaceChips((k) => rowsFor({ surface: k, time: st.time, view: st.view }), listNow, chooseSurface, inCard ? "" : "Type");
            if (chipRow) chipRow.replaceWith(fresh); else bar.after(fresh);
            chipRow = fresh;
            if (!inCard) {
              const t = buildTimeChips((k) => rowsFor({ surface: st.surface, time: k, view: st.view }), timeNow, (v) => { listTime = v; build(); redraw(); });
              if (timeRow) timeRow.replaceWith(t); else chipRow.before(t);
              timeRow = t;
            }
            // the chips are rebuilt after a tap: the same chip, rebuilt, takes the keyboard focus back
            if (at) {
              const row = at[0] === "surface" ? chipRow : timeRow;
              // a chip with no replacement (its count fell to none) hands the focus to the row's first chip
              const again = row && (row.querySelector("[data-" + at[0] + "=\"" + at[1] + "\"]") || row.querySelector("button"));
              if (again) again.focus();
            }
          };
          const chooseSurface = (v) => { listSurface = v; build(); redraw(); };
          rebuildChips = build;
          if (inCard) listCardRedraw = () => { if (document.body.contains(p) && listChoice() !== drawnChoice) {
            // a draw that fails leaves the card unconfirmed, so the next close tries again
            try { build(); redraw(); } catch (e) { drawnChoice = null; throw e; }
          } };
          build();
        }
        if (!views.length) return;
        const sel = el("select", "tray-dump-type");
        const orderWord = isList ? "List order" : "Tray order";
        sel.setAttribute("aria-label", orderWord);
        sel.appendChild(Object.assign(el("option", null, orderWord), { value: "" }));
        for (const v of views) sel.appendChild(Object.assign(el("option", null, v.title), { value: v.id }));
        sel.value = viewOf();
        orderSync = () => { sel.value = viewOf(); };
        sel.addEventListener("change", () => { trayView = sel.value; if (rebuildChips) rebuildChips(); redraw(); });
        bar.appendChild(sel);
      },
      pick: () => rowsFor(stateNow()),
    } : undefined;
    mountIntentionFilter(p, list || [], "items", entry.kind, (rows, results) => {
      let lastGroup = null;
      let lastBucket = null;
      let box = results;
      for (const s of rows) {
        // a manifest source brings the kernel's own bands (Today / Overdue / Also pending), in its order;
        // a chosen view is ungrouped
        const g = s.group || null;
        if (s.group_title && g !== lastGroup && !viewOf()) {
          // each band is a column of its own where the page is wide (Morning's Tray: Overdue | Today); `display: contents` elsewhere
          box = entry.kind === "tray" ? results.appendChild(el("div", "grp-col")) : results;
          box.appendChild(headingEl(s.group_title));
          lastBucket = null;
        }
        // LATER groups its rows by the day they wake (the kernel's bucket; its rows are already in wake order)
        if (g === "later" && s.wake_bucket && s.wake_bucket !== lastBucket && !viewOf()) {
          lastBucket = s.wake_bucket;
          const sub = el("div", "grp-sub", WAKE_BUCKETS[s.wake_bucket] || s.wake_bucket);
          sub.dataset.wakeBucket = s.wake_bucket;
          sub.setAttribute("role", "heading");
          sub.setAttribute("aria-level", "4");
          box.appendChild(sub);
        }
        lastGroup = g;
        box.appendChild(intentionRow(s, verbs, badgeField, entry.kind));
      }
    }, opts);
    if (isList) mountListKept(p);
    return p;
  }

  // The List's memory part (the Keep, row 182): the List's search also reaches the entity's episodes, which the kernel sends only to
  // a search (`q` on the panel read; a plain read carries none of them). So the box, besides filtering the rows already here, asks
  // the kernel once the typing settles and draws the `kept` rows it sends in a "Kept in memory" group under the List rows: expanded
  // when there are hits, absent when there are none, read-only (no verb is offered on a memory), each row naming its own receipt
  // line ("kept as memory: episode ..."). A newer query retires the one in flight; a page with no read transport draws no group.
  const KEPT_DEBOUNCE_MS = 250;
  function mountListKept(p) {
    const T = window.LevainCockpitTransport;
    const input = p.querySelector(".ep-search-input");
    const results = p.querySelector(".sp-results");
    if (!T || typeof T.readJson !== "function" || !input || !results) return;
    input.maxLength = KEPT_QUERY_MAX;     // the box holds what both halves read: a longer paste is cut by the browser, not by one half
    const host = el("div", "list-kept");
    host.dataset.listKept = "1";
    host.hidden = true;
    results.after(host);
    let seq = 0, timer = 0, flight = null;
    const clear = () => { host.hidden = true; host.replaceChildren(); };
    // a panel redraw replaces this host: its timer and its read stop, and nothing is drawn into it (`=== false`: a fake DOM has no flag)
    const gone = () => host.isConnected === false;
    const stop = () => {
      window.clearTimeout(timer);
      seq++;                                                    // before the abort: the aborted read finds itself retired
      if (flight) { const f = flight; flight = null; f.abort(); }
    };
    // A fault is ONE fixed line: the server's text for it (a skipped line, a search error) and a transport's message can carry a
    // path or an OS error, which the page never shows. The kernel's own sentence about its search window is a constant, so it stays.
    const KEPT_FAULT = "memory could not be searched";
    const keptUnread = (payload) => (Array.isArray(payload.skipped) ? payload.skipped : []).some((k) => k && typeof k.reason === "string" && /^kept memory/.test(k.reason));
    const keptWords = (payload) => {
      const out = keptUnread(payload) ? [KEPT_FAULT] : [];
      const w = typeof payload.note === "string" ? /a search of memory reaches[^.]*\./.exec(payload.note) : null;
      if (w) out.push(w[0]);
      return out;
    };
    const keptRow = (r) => {
      const f = r && r.facets && typeof r.facets === "object" ? r.facets : {};
      const row = el("div", "row");
      row.dataset.surface = "kept";
      if (typeof r.id === "string") row.dataset.rowKey = r.id;
      const meta = el("span", "list-meta");
      const chip = tagEl("tier list-surface", "kept");
      chip.dataset.surfaceChip = "kept";
      meta.appendChild(chip);
      if (typeof f.kept_line === "string" && f.kept_line) meta.appendChild(tagEl("muted list-kept-line", f.kept_line));
      if (typeof f.kept_at === "string" && /^\d{4}-\d{2}-\d{2}/.test(f.kept_at)) meta.appendChild(tagEl("muted", shortDate(f.kept_at)));
      // the memory's own words; a blank line inside them would clamp to an empty second line, so a run of newlines is one
      appendClause(row, (typeof r.body === "string" && r.body ? r.body : String(r.title || "")).replace(/\n{2,}/g, "\n"));
      row.appendChild(meta);
      return row;
    };
    const draw = (payload) => {
      host.replaceChildren();
      const kept = (Array.isArray(payload.rows) ? payload.rows : []).filter((r) => r && r.facets && r.facets.surface === "kept");
      const words = keptWords(payload);
      if (payload.status === "error" || typeof payload.search_error === "string") {
        host.appendChild(el("p", "err", KEPT_FAULT));
      } else if (kept.length) {
        const g = el("details", "list-kept-group");
        g.open = true;
        g.appendChild(el("summary", "grp-head", "Kept in memory \u00b7 " + kept.length));
        for (const r of kept) g.appendChild(keptRow(r));
        host.appendChild(g);
      }
      for (const w of words) host.appendChild(el("p", "muted list-kept-note", w));
      host.hidden = host.children.length === 0;
      measureClauses(host);
    };
    // `mount`: the run a redrawn panel makes for the query it keeps, before its host is in the page; every later run is a key's, and
    // a key's run finds a replaced host and stops. The read is quiet: a token 403 on a read nobody asked for by name is a kept fault
    // (the fixed line), never the token prompt and never a dropped token.
    const run = async (mount) => {
      if (!mount && gone()) { stop(); return; }
      const q = listQueryText(input.value);
      const mine = ++seq;
      if (flight) { const f = flight; flight = null; f.abort(); }
      if (!q) { clear(); return; }
      const ctl = typeof AbortController === "function" ? new AbortController() : null;
      flight = ctl;
      let payload;
      try { payload = await T.readJson("/cockpit/panel/tray.json?profile=full&q=" + encodeURIComponent(q), ctl ? ctl.signal : undefined, { quiet: true }); }
      catch (e) {
        if (mine !== seq || (e && e.name === "AbortError")) return;     // retired by a newer key, or the panel went
        payload = { status: "error" };
      }
      if (flight === ctl) flight = null;
      if (mine !== seq || gone()) return;     // a newer query (or a cleared box) retired this one, or the panel was redrawn
      draw(payload && typeof payload === "object" ? payload : { status: "error" });
    };
    input.addEventListener("input", () => {
      stop();
      if (!listQueryText(input.value)) { clear(); return; }
      timer = window.setTimeout(() => run(false), KEPT_DEBOUNCE_MS);
    });
    if (listQueryText(input.value)) run(true);      // a redrawn panel keeps its query: its memory part comes back with it
  }

  function renderIntentions(entry, view) {
    const p = panel(entry);
    const err = tierErr(view, "open_intentions");
    if (err) { p.appendChild(el("p", "err", "unavailable — " + err)); return p; }
    const all = view.open_intentions || [];
    const verbs = isVerbPanel(entry);
    // CLIENT-side filter over the loaded loops (they all ride the wire — no server round-trip,
    // unlike the episode keyword search). Matches the intention TEXT (case-insensitive substring)
    // OR the id ("intention-103", "spore-103" or "103"), so you can jump straight to a loop by handle or
    // content [2026-06-19]. Reuses the .ep-search sticky bar for visual parity.
    const bar = el("div", "ep-search");
    const input = el("input", "ep-search-input");
    input.type = "search";
    input.placeholder = "filter loops by text or id…";
    input.setAttribute("aria-label", "filter open loops by text or intention id");
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
          q ? "no loops match “" + raw.trim() + "”" : "Nothing yet. What stays open after " + (view.entity_name || "the assistant") + " sorts the Tray is listed here."));
      } else {
        for (const s of rows) results.appendChild(intentionRow(s, verbs, "tier", entry.kind));
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

  function renderTray(entry, view, opts) {
    const p = renderIntentionProjection(entry, view, view.tray,
      "Nothing yet. Drop a line in the box above and " + (view.entity_name || "the assistant") + " sorts it.", "disposition", opts);
    // The intake box is the primary capture affordance (the List's one line) — mounted under the title when
    // writable. A read-only port (mesh) shows the List but offers no box (NO-THEATER). A panel that sends no `surface` on its
    // rows is the old Tray and keeps its dump box.
    if (isVerbPanel(entry)) {
      const isList = view.tray_is_list === true;
      mountCaptureBox(p, isList ? buildListIntake() : buildTrayDump(view.entity_name || "the assistant"));
    }
    return p;
  }

  // The List's surface filter: one chip per store that has a row now, with its count, plus All. A display choice only
  // (`listSurface`); the rows and their order are the kernel's.
  const SURFACE_WORDS = { band: "band", board: "board", intention: "intentions", talk: "talk", drop: "drops" };
  // ONE rule for which rows a surface chip stands for: the chip's count and the rows the focus view draws under it both use it, so
  // a number on a chip is the number of rows its tap shows, and the type chips add up to "all" in every time-state.
  function inSurfaceChip(s) {
    return !!(s && s.surface);
  }
  // The List's time chips: now / later / someday / done, each with its count; a second tap clears the choice. The type chips
  // beside them keep cutting across the time-states.
  const LIST_TIMES = ["now", "later", "someday", "done"];
  const WAKE_BUCKETS = { tomorrow: "Tomorrow", week: "Next 7 days", later: "Later dates" };
  // rowsFor(k) is the rows the tap on the chip k draws; a chip says how many.
  function buildTimeChips(rowsFor, current, choose) {
    const row = el("div", "list-times");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "filter the List by when a row comes due");
    row.appendChild(el("span", "list-chips-label", "When"));
    const chips = [];
    const sync = (now) => { for (const c of chips) c.setAttribute("aria-pressed", c.dataset.time === now ? "true" : "false"); };
    for (const k of LIST_TIMES) {
      const n = rowsFor(k).length;
      if (n === 0 && k !== current()) continue;
      const c = el("button", "rc-chip list-time-chip", k.charAt(0).toUpperCase() + k.slice(1) + " " + n);
      c.type = "button";
      c.dataset.time = k;
      c.addEventListener("click", () => choose(current() === k ? "" : k));
      chips.push(c);
      row.appendChild(c);
    }
    sync(current());
    return row;
  }
  function buildSurfaceChips(rowsFor, current, choose, label) {
    const row = el("div", "list-surfaces");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "filter the List by where a row comes from");
    if (label) row.appendChild(el("span", "list-chips-label", label));
    const chips = [];
    const mk = (value, word, n) => {
      const c = el("button", "rc-chip list-surface-chip", word + " " + n);
      c.type = "button";
      c.dataset.surface = value;
      c.addEventListener("click", () => choose(value));
      chips.push(c);
      row.appendChild(c);
    };
    const sync = (now) => { for (const c of chips) c.setAttribute("aria-pressed", c.dataset.surface === now ? "true" : "false"); };
    mk("", "all", rowsFor("").length);
    for (const k of Object.keys(SURFACE_WORDS)) { const n = rowsFor(k).length; if (n > 0) mk(k, SURFACE_WORDS[k], n); }
    sync(current());
    return row;
  }

  // The List's INTAKE box: ONE line, posted as list_intake {line}. The page holds no grammar: `@when words`, `? words` and a
  // plain line all go to the server, which reads the line, decides the tier (a plain line is answered at once; `@` and `?` lines are
  // signed on a device) and returns the receipt sentence, printed here verbatim under the box. A refusal prints the server's reason.
  // The idempotency key is minted per submit by the page's one write path (postVerb): fresh for each submit, kept across a retry.
  function paintIntakeReceipts() {
    for (const n of document.querySelectorAll("[data-list-receipt]")) {
      const r = intakeKept.receipt;
      n.className = "edit-msg list-receipt" + (r ? " " + r.cls : "");
      n.textContent = r ? r.text : "";
    }
  }
  // A placeholder that must fit its box: the full words on a wide screen, a shorter line on a phone (re-chosen when the width crosses).
  // ONE media listener for every such input, registered once: an input opts in by data-ph-wide / data-ph-narrow, and the
  // listener sets every opted-in input still in the document (a rebuilt List leaves its old inputs out of it).
  let phListening = false;
  let phMq = null;   // held at module scope so the browser cannot collect the list and stop delivering "change"
  function phNarrow() { try { return typeof window.matchMedia === "function" && window.matchMedia("(max-width: 720px)").matches; } catch (e) { return false; } }
  function setPlaceholders() {
    const narrow = phNarrow();
    for (const i of document.querySelectorAll("[data-ph-wide]")) i.placeholder = narrow ? i.getAttribute("data-ph-narrow") : i.getAttribute("data-ph-wide");
  }
  function narrowPlaceholder(input, wide, narrow) {
    input.setAttribute("data-ph-wide", wide);
    input.setAttribute("data-ph-narrow", narrow);
    input.placeholder = phNarrow() ? narrow : wide;
    if (!phListening && typeof window.matchMedia === "function") {
      try {
        phMq = window.matchMedia("(max-width: 720px)");
        if (phMq && typeof phMq.addEventListener === "function") { phMq.addEventListener("change", setPlaceholders); phListening = true; }   // only once it is registered
      } catch (e) { /* the placeholder keeps its first choice; a later call may try again */ }
    }
  }
  // an origin a List row's lead word may name: a repo the sky's registry knows, or "desk" / "flow".
  // Returns the canonical name (the registry's own spelling), or "" when the word is no origin.
  function knownOrigin(word) {
    const w = String(word).toLowerCase();
    if (w === "desk" || w === "flow") return w;
    const sky = window.LevainSky;
    const names = sky && typeof sky.repos === "function" ? sky.repos() : [];
    const hit = names.find((n) => String(n).toLowerCase() === w);
    return hit === undefined ? "" : String(hit);
  }
  function buildListIntake() {
    const box = el("div", "tray-dump list-intake");
    const row = el("div", "list-intake-row");
    const input = el("input", "list-intake-input");
    input.type = "text";
    noContactFill(input);
    input.setAttribute("enterkeyhint", "send");      // overrides the helper's default: this box sends on Enter
    input.setAttribute("aria-label", "add a line to the List");
    narrowPlaceholder(input, "@mon 9am call X  ·  ? talk about Y  ·  or a plain line", "@mon 9am call · ? talk Y");
    input.value = intakeKept.draft;
    input.maxLength = 500;
    const go = el("button", "tray-dump-btn list-intake-btn", "add");
    go.type = "button";
    row.append(input, go);
    const receipt = el("div", "edit-msg list-receipt");
    receipt.setAttribute("data-list-receipt", "");
    receipt.setAttribute("role", "status");
    receipt.setAttribute("aria-live", "polite");
    box.append(row, receipt);
    let busy = false;
    const submit = async () => {
      const line = input.value.trim();
      if (busy) return;
      if (!line) { intakeKept.receipt = { text: "nothing to add", cls: "err" }; paintIntakeReceipts(); return; }
      busy = true; go.disabled = true;
      intakeKept.receipt = { text: "…", cls: "busy" }; paintIntakeReceipts();
      let res;
      try { res = await commit({ kind: "list_intake", line }); } catch (e) { res = { ok: false, message: "the request failed" }; }
      busy = false;
      // ok:true is transport; the write landed only on COMMITTED/done (or pends on WAITING/applying). Any other state is a failure.
      const st = res && res.ok ? ((res.data || {}).state || "done") : "";
      const landed = st === "COMMITTED" || st === "done";
      const pending = st === "WAITING" || st === "applying";
      if (res && res.ok && (landed || pending)) {
        const d = res.data || {};
        // the server's sentence, verbatim; a state it did not word still says so
        const text = typeof d.receipt_line === "string" && d.receipt_line ? d.receipt_line
          : d.state === "WAITING" ? "waiting for your signature" : "added";
        if (intakeKept.draft.trim() === line) intakeKept.draft = "";   // what he typed since stays his
        intakeKept.receipt = { text, cls: pending ? "preview" : "ok" };
        // the page was re-read inside the write: the box on screen is a new one holding the sent line, so clear every box
        for (const n of document.querySelectorAll(".list-intake-input")) if (n.value.trim() === line) n.value = "";
      } else {
        // the page was already re-read for some refusals (stale, not_built): the unsent line is kept in the new box
        if (!intakeKept.draft) intakeKept.draft = line;   // keep what he has typed since; restore the unsent line only into an empty box
        const d = (res && res.data) || {};
        const why = res && res.ok ? ((typeof d.receipt_line === "string" && d.receipt_line) || "not written (" + st + ")")
          : (res && (res.detail || res.message || res.error)) || "failed";
        intakeKept.receipt = { text: why, cls: "err" };
      }
      go.disabled = false;
      paintIntakeReceipts();
    };
    input.addEventListener("input", () => { intakeKept.draft = input.value; });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.isComposing && !ev.shiftKey) { ev.preventDefault(); submit(); }
    });
    // a phone keyboard rises over the lower half: bring the box to the middle of what is left
    input.addEventListener("focus", () => {
      setTimeout(() => { try { box.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) { /* a convenience only */ } }, 300);
    });
    go.addEventListener("click", submit);
    // a rebuilt box carries the last receipt and the unsent line
    if (intakeKept.receipt) {
      receipt.className = "edit-msg list-receipt " + intakeKept.receipt.cls;
      receipt.textContent = intakeKept.receipt.text;
    }
    return box;
  }

  // One episode row — the SINGLE markup for both the recent list and keyword-search
  // hits (intention-107), so they render identically. `verbs` carries the panel's verb
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

    // intention-107: inline keyword search over the episodic store, READ-ONLY. Enter runs
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
        // intention-220 gates /recall.json off-box); fall back to a direct token-free fetch otherwise.
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

  // Projection history (Slice 2b-ii / intention-093 v0). Each wrap is a RE-PROJECTION of
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
  // The fence rule, one home: renderMarkdown draws a fenced block by it and the notification detail's shape finder (ncFenced) skips
  // the same lines, so the two cannot disagree on where code is. An opener is a run of 3+ backticks or tildes with an info string
  // that holds neither; a closer is a line of at least the opener's run, of the same character, and nothing else.
  function mdFenceOpen(line) {
    const m = /^([ \t]*)(```+|~~~+)[ \t]*([^`~]*)$/.exec(line);
    return m ? { marker: m[2][0], len: m[2].length } : null;
  }
  function mdFenceCloses(line, open) {
    const c = /^[ \t]*(`{3,}|~{3,})[ \t]*$/.exec(line);
    return !!c && c[1][0] === open.marker && c[1].length >= open.len;
  }
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
      const fence = mdFenceOpen(line);
      if (fence) {
        i++;
        const buf = [];
        while (i < N) {
          if (mdFenceCloses(lines[i], fence)) { i++; break; }
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
  // The one renderer is shared with the other scripts of this page (the Room draws the seat's replies with it) under the
  // same name the docs page gives it, so no page carries a second copy.
  window.LevainMD = { renderMarkdown: renderMarkdown, mdSafeHref: mdSafeHref };

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

  // A Class-B panel (intentions / episodes — the operator's INPUT layer) gets
  // verb affordances when a write transport is present (read-only port → none).
  // The verbs are anneal-validated lifecycle ops, never raw edits; a DESTRUCTIVE
  // one (resolving an intention / tombstoning an episode) sends confirm:true — and the
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
    ta.dataset.orig = ta.value;  // dirty baseline for hasUnsavedEdit (intention-108): an involuntary
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
  // Intentions + episodes are the operator's INPUTS, mutated through anneal's validated
  // verbs (never raw writes). A metal button per row; destructive verbs open a small
  // confirm/kind form that sends confirm:true (the server requires it regardless).

  // The cockpit verb each intention button fires. On a manifest row (one that lists its `actions`) a button
  // is kept only when the kernel offered that verb on the row, and is disabled, with the gesture as its
  // title, when the gesture is one a browser cannot perform. A legacy row lists none and is untouched.
  const BUTTON_VERB = { touch: "intention_touch", edit: "intention_update", park: "intention_update",
    reactivate: "intention_update", "when?": "intention_surface_at", who: "intention_retarget", "then you": "intention_then", Resolve: "intention_close", metabolize: "intention_set_disposition",
    dismiss: "intention_close", compost: "intention_close", promote: "intention_promote",
    tombstone: "episode_tombstone", keep: "intention_keep", "Make an agenda": "band_agenda" };

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

  function buildPlainIntentionVerbs(s, panelKind) {
    const wrap = el("span", "verb-actions");

    // A TRAY item (the forming workbench): edit is
    // first-class — the item isn't ready for the lifecycle verbs yet. Then the route levers.
    if (panelKind === "tray") {
      wrap.appendChild(editBtn(wrap, s));
      // metabolize — promote into cognition as a live loop (Tray->loop is never the
      // silent-loss direction, so no confirm); the type LOCKS at this boundary.
      wrap.appendChild(verbBtn("metabolize", "promote into cognition as a live loop", async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const res = await commit({ kind: "intention_set_disposition", intention_id: s.id, disposition: "loop",
          expect_disposition: s.disposition });
        if (res && res.ok) return;  // re-rendered
        b.disabled = false;
        verbErr(wrap, res);
      }));
      wrap.appendChild(verbBtn("when?", "when it comes back, and who does it", () => openSurfaceForm(wrap, s)));
      // an agenda holds a target, so it has a who to change (a seed is given one through when?)
      if (rowTarget(s)) wrap.appendChild(verbBtn("who", "who does it: you or the machine", () => openWhoForm(wrap, s)));
      // only where the cockpit offers intention_then (an agenda); a row with no actions list (a legacy row) is offered none
      if (Array.isArray(s.actions) && s.actions.some((x) => x && x.verb === "intention_then")) {
        wrap.appendChild(verbBtn("then you", "what you do after this is done: it comes to the band when this is marked done", () => openThenForm(wrap, s)));
      }
      if (Array.isArray(s.descend_kinds) && s.descend_kinds.length) {
        wrap.appendChild(verbBtn("Resolve", "resolve or compost this inbox item", () =>
          openResolveForm(wrap, "intention_close", "dismiss", s, s.descend_kinds, false)));
      }
      // keep: only where the cockpit offers intention_keep (a plain intention row); a row with no actions list (a legacy row) is offered none
      if (Array.isArray(s.actions) && s.actions.some((x) => x && x.verb === "intention_keep")) {
        wrap.appendChild(verbBtn("keep", "keep these words as a memory and take the item off the List", () => openKeepForm(wrap, s)));
      }
      return gateByActions(wrap, s.actions);
    }

    // OPEN LOOPS — the entity's live cognition: the lifecycle verbs (+ edit + schedule).
    wrap.appendChild(verbBtn("touch", "mark seen — reset its clock", async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      const res = await commit({ kind: "intention_touch", intention_id: s.id });
      if (res && res.ok) return;  // shim re-fetched + re-rendered → this DOM is gone
      b.disabled = false;
      verbErr(wrap, res);
    }));
    wrap.appendChild(editBtn(wrap, s));
    wrap.appendChild(verbBtn("when?", "when it comes back, and who does it", () => openSurfaceForm(wrap, s)));
    // park — pin dormant: hide from active cognition but the loop stays OPEN (exempt from the
    // dormancy→compost prompt), resurfaceable, and shown HERE with a parked marker (the Keep panel is gone).
    // A parked row offers the inverse, "reactivate" (un-park, back to warm). A tier change
    // (intention_update{tier}), NON-destructive (the loop isn't resolved), so no confirm.
    // intention-173: both are offered only on a loop (this Open-Loops branch) → CAS the snapshot so a
    // concurrent re-route before the write fails closed instead of tiering a now-note.
    if (s.tier === "parked") {
      wrap.appendChild(verbBtn("reactivate", "un-park → back into active cognition", async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const res = await commit({ kind: "intention_update", intention_id: s.id, tier: "warm",
                                   expect_disposition: s.disposition });
        if (res && res.ok) return;  // re-rendered
        b.disabled = false;
        verbErr(wrap, res);
      }));
    } else {
      wrap.appendChild(verbBtn("park", "pin dormant — hide from cognition, stays open (reactivate to restore)", async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        const res = await commit({ kind: "intention_update", intention_id: s.id, tier: "parked",
                                   expect_disposition: s.disposition });
        if (res && res.ok) return;  // re-rendered
        b.disabled = false;
        verbErr(wrap, res);
      }));
    }
    // compost (descend) / promote (ascend) — destructive; kind comes from the
    // intention's own type taxonomy (server-emitted, so the UI can't offer an invalid kind).
    if (Array.isArray(s.descend_kinds) && s.descend_kinds.length) {
      wrap.appendChild(verbBtn("compost", "resolve this loop downward", () =>
        openResolveForm(wrap, "intention_close", "compost", s, s.descend_kinds, false)));
    }
    if (Array.isArray(s.ascend_kinds) && s.ascend_kinds.length) {
      wrap.appendChild(verbBtn("promote", "transmute into memory / project", () =>
        openResolveForm(wrap, "intention_promote", "promote", s, s.ascend_kinds, true)));
    }
    return gateByActions(wrap, s.actions);
  }

  // WHO acts when an agenda wakes is derived from its target and never stored: a band row or a nudge rings in the band (you),
  // the desk and a board item are the machine's. Editing it sets `target` (intention_retarget), through the same words.
  // the row's germinate target as the page holds it (the cockpit view names it deliver_to)
  const rowTarget = (s) => (typeof s.deliver_to === "string" && s.deliver_to ? s.deliver_to : "");
  function executorOf(target) {
    if (target === "band" || target === "nudge") return "you";
    if (target === "desk" || (typeof target === "string" && /^board:\S+$/.test(target))) return "the machine";
    return "";
  }
  // a board target names a plain repo (the store's other reader takes nothing else)
  const REPO_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

  // The who control: "you" / "the machine" (with "desk" or a repo name), preselected from the row's own target; a seed has none.
  // value() is the target the control now names, null when nothing is chosen, or false when the machine's repo is not a name.
  function whoControl(s) {
    const cur = executorOf(rowTarget(s));
    const box = el("span", "who-ctl");
    const bYou = el("button", "verb-cancel who-opt", "you");
    const bMachine = el("button", "verb-cancel who-opt", "the machine");
    bYou.type = "button"; bMachine.type = "button";
    bYou.dataset.who = "you"; bMachine.dataset.who = "the machine";
    const repo = el("input", "verb-ref who-repo");
    repo.type = "text"; repo.setAttribute("aria-label", "which: the desk, or a repo's board");
    repo.placeholder = "desk, or a repo";
    repo.value = typeof rowTarget(s) === "string" && rowTarget(s).startsWith("board:") ? rowTarget(s).slice(6) : (cur === "the machine" ? "desk" : "");
    let chosen = cur;
    const paint = () => {
      for (const b of [bYou, bMachine]) b.setAttribute("aria-pressed", b.dataset.who === chosen ? "true" : "false");
      repo.hidden = chosen !== "the machine";
      if (chosen === "the machine" && !repo.value) repo.value = "desk";
    };
    bYou.addEventListener("click", () => { chosen = chosen === "you" ? "" : "you"; paint(); });
    bMachine.addEventListener("click", () => { chosen = chosen === "the machine" ? "" : "the machine"; paint(); });
    paint();
    box.append(el("span", "muted who-label", "who does it"), bYou, bMachine, repo);
    return {
      node: box,
      value: () => {
        if (chosen === "you") return rowTarget(s) === "nudge" ? "nudge" : "band";   // a nudge is still the operator's, keep its ping
        if (chosen !== "the machine") return null;
        const r = repo.value.trim();
        if (r === "desk") return "desk";
        return REPO_NAME.test(r) ? "board:" + r : false;
      },
    };
  }

  // The local minute a picker writes: "YYYY-MM-DDTHH:MM", read from the browser's clock, no zone in the value. The host reads it
  // in ITS zone (the page has no channel for the host's zone), so the form says the times are the host's.
  const pad2 = (n) => String(n).padStart(2, "0");
  const dayOf = (d) => d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  const minuteOf = (d) => dayOf(d) + "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  // the quick picks, from a clock: later today (a few hours on, held inside today), tonight (offered only while it is still ahead),
  // tomorrow morning, next Monday morning, now; each label names its own time
  function whenPicks(now) {
    const day = (n) => new Date(now.getFullYear(), now.getMonth(), now.getDate() + n);
    const at = (d, h) => minuteOf(new Date(d.getFullYear(), d.getMonth(), d.getDate(), h, 0));
    const picks = [];
    // later today never runs past today's last minute (a pick under a "today" label never writes tomorrow), and it is gone in today's last hour
    if (now.getHours() < 23) {
      const endOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59);
      picks.push({ key: "later", label: "Later today", value: minuteOf(new Date(Math.min(now.getTime() + 3 * 3600000, endOfDay.getTime()))) });
    }
    if (now.getHours() < 20) picks.push({ key: "tonight", label: "Tonight 20:00", value: at(day(0), 20) });
    picks.push({ key: "tomorrow", label: "Tomorrow 09:00", value: at(day(1), 9) });
    picks.push({ key: "monday", label: "Next Mon 09:00", value: at(day(((8 - now.getDay()) % 7) || 7), 9) });
    picks.push({ key: "now", label: "Now", value: minuteOf(now) });
    return picks;
  }

  // The "when?" form on an intention row: quick picks (one tap writes), a date and a time input (two inputs, never datetime-local: a
  // date-only pick stays one tap, and the phone's date and time wheels each open on their own), the who control, then
  // schedule / clear / cancel. It writes `YYYY-MM-DDTHH:MM` through intention_surface_at; when the who control names a target the
  // row does not hold, the same write goes through intention_retarget (a seed given a who becomes a dated agenda in that write).
  // Mirrors openResolveForm's hide-triggers / one-form-at-a-time / Escape-cancel shape.
  // `opts` (the intention snooze): {label, post(value), min, back(value), done(value), stale}: the same date picker, a different
  // button word, no "clear" (a snooze always names a day), the earliest day it takes (`min`, refused in plain words before
  // anything is sent), a visible line naming when the row comes back, the row's words once it is sent, and the words a stale
  // refusal reads as; the pick is posted through `post` instead of the intention write.
  function openSurfaceForm(host, s, opts) {
    if (host.querySelector(".verb-form")) return;
    const stale = host.querySelector(".edit-msg"); if (stale) stale.remove();
    const triggers = Array.prototype.slice.call(host.querySelectorAll(".verb-btn"));
    triggers.forEach((b) => (b.style.display = "none"));
    const form = el("span", "verb-form when-form");
    const picksBox = el("span", "when-picks");
    // the who control only where the cockpit offers intention_retarget (a seed or an agenda): a loop is nobody's to hand off
    // a row with no actions list (a legacy row) is offered none: the who control is drawn only on the kernel's offer
    const canWho = Array.isArray(s.actions) && s.actions.some((x) => x && x.verb === "intention_retarget");
    const who = canWho ? whoControl(s) : { node: null, value: () => null };
    const input = el("input", "verb-date");
    input.type = "date";
    input.setAttribute("aria-label", "day");
    const timeIn = el("input", "verb-time");
    // empty unless the row already holds a minute or the operator sets one: an empty time keeps a date-only write date-only
    timeIn.type = "time"; timeIn.step = 60; timeIn.value = "";
    timeIn.setAttribute("aria-label", "time, on the Levain host's clock");
    timeIn.title = "on the Levain host's clock";
    const zone = el("span", "muted when-zone", "times are the Levain host's clock");
    const m = /^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?$/.exec(String(s.fires || s.next || ""));
    if (m && !opts) { input.value = m[1]; if (m[2]) timeIn.value = m[2]; }   // prefill an existing alarm
    const go = el("button", "verb-confirm", opts && opts.label ? opts.label : "schedule");
    const clearBtn = el("button", "verb-cancel", "someday");
    const cancel = el("button", "verb-cancel", "cancel");
    go.type = "button"; clearBtn.type = "button"; cancel.type = "button";
    clearBtn.title = "no day: leave it undated";
    const msg = el("span", "edit-msg", "");
    if (opts && (opts.minToday || opts.min)) input.min = opts.minToday || opts.min;   // a timed pick may be today: the host judges the minute
    const back = opts && opts.back ? el("span", "muted intention-back", "") : null;
    const showBack = () => { if (back) back.textContent = input.value ? opts.back(timeIn.value ? input.value + "T" + timeIn.value : input.value) : ""; };
    if (back) { input.addEventListener("input", showBack); timeIn.addEventListener("input", showBack); }
    const all = [go, clearBtn, cancel];
    // the picks and the who control are the `when?` form's: a form a caller drives with its own post (a snooze) offers neither
    const picks = opts ? [] : whenPicks(new Date()).map((p) => {
      const b = el("button", "verb-cancel when-pick", p.label);
      b.type = "button"; b.dataset.pick = p.key; b.dataset.value = p.value;
      b.addEventListener("click", () => submit(p.value));
      all.push(b);
      return b;
    });
    picksBox.append(...picks);
    // an agenda (it holds a target) is undated never: flow refuses one, so "someday" is for a seed with no who chosen
    const agenda = rowTarget(s) !== "";
    if (agenda) clearBtn.hidden = true;
    // `opts.dateOnly`: the verb takes a day and nothing finer (the band row to agenda), so the form offers no time
    form.append(...(opts ? [] : [picksBox]), input, ...(opts && opts.dateOnly ? [] : [timeIn, zone]), ...(back ? [back] : []), ...(!opts && who.node ? [who.node] : []), go, ...(opts ? [] : [clearBtn]), cancel, msg);
    host.appendChild(form);
    // a touch screen's keyboard opens on a focused date input and covers the form: focus it only where a pointer, not a thumb, drives
    if (!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) input.focus();
    // the card clips what overflows it: bring the confirm row into the card's view
    go.scrollIntoView({ block: "nearest" });
    const close = () => { form.remove(); triggers.forEach((b) => (b.style.display = "")); };
    cancel.addEventListener("click", close);
    form.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!go.disabled) close();
    });
    const submit = async (value) => {
      const t = who.value();
      if (t === false) { msg.className = "edit-msg err"; msg.textContent = "the machine's repo is a plain name (letters, digits, . _ -), or desk"; return; }
      const retarget = t !== null && t !== rowTarget(s);
      if (retarget && value === "") { msg.className = "edit-msg err"; msg.textContent = "a task for you or the machine needs a day"; return; }
      for (const b of all) b.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = opts && opts.post ? await opts.post(value) : await commit(retarget
        ? { kind: "intention_retarget", intention_id: s.id, target: t, surface_at: value }
        : { kind: "intention_surface_at", intention_id: s.id, surface_at: value });
      if (res && res.ok) {   // re-rendered; a row that stays (or a redraw still to come) reads what was done
        if (opts && opts.done) { msg.className = "edit-msg"; msg.textContent = opts.done(value); }
        return;
      }
      showFail(msg, res, "failed");
      if (res && res.error === "stale") {   // the row moved under this form: keep the words, retire the form (as the Edit path does)
        if (opts && opts.stale) msg.textContent = opts.stale;
        retireStaleForm(form);
        return;
      }
      for (const b of all) b.disabled = false;
    };
    go.addEventListener("click", () => {
      if (!input.value) { msg.className = "edit-msg err"; msg.textContent = opts ? "pick a day" : "pick a day (or someday)"; return; }
      const floor = opts ? (timeIn.value ? opts.minToday || opts.min : opts.min) : null;   // date-only: after today; timed: today, the host judges the minute
      if (floor && input.value < floor) { msg.className = "edit-msg err"; msg.textContent = "pick a later day"; return; }
      submit(timeIn.value ? input.value + "T" + timeIn.value : input.value);
    });
    clearBtn.addEventListener("click", () => submit(""));  // explicit clear: key present, value ""
  }

  // The four controls of a row that rings an intention (a reminder the desk or a schedule filed into the band): the same labels on
  // the Decisions card and on the List's band row. Each posts the row's own offered action through the page's one write
  // transport; the row is re-read after, so a snoozed, done or cleared one leaves. Done asks nothing; Clear asks once.
  const INTENTION_CONTROLS = [["Snooze", "intention_snooze", "move it to a later day or time"], ["Edit", "intention_edit", "change its words"],
    ["Done", "intention_done", "mark it done"], ["Clear", "intention_clear", "drop it for good"]];
  const INTENTION_STALE = "this reminder changed: reload";
  // the page's local calendar day `offset` days from today, YYYY-MM-DD (the date input's own form)
  function localDayIso(offset) {
    const d = new Date(); d.setDate(d.getDate() + offset);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function dayWords(iso) {
    const d = new Date(iso.slice(0, 10) + "T00:00");
    if (!Number.isFinite(d.getTime())) return iso;
    return d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  // the day with its year, always: an agenda is not bound to the snooze window, so a year fat-fingered in the date box (2036) must not
  // read like the day meant. (A snooze's day is within weeks, so its words stay yearless.)
  function dayYearWords(iso) {
    const d = new Date(iso.slice(0, 10) + "T00:00");
    return Number.isFinite(d.getTime()) ? dayWords(iso) + ", " + d.getFullYear() : iso;
  }
  // a snooze value is a day (YYYY-MM-DD) or a timed same-day-or-later pick (YYYY-MM-DDTHH:MM): the words name the minute
  // when the value carries one, so a timed pick never reads back as its date alone
  function dayTimeWords(iso) {
    const t = typeof iso === "string" && /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(iso);
    return dayWords(iso) + (t ? " at " + t[1] : "");
  }
  function intentionPost(rowId, verb, confirm) {
    return async (req) => {
      const T = window.LevainCockpitTransport;
      if (!T || typeof T.rowVerb !== "function") return { ok: false, message: "this page cannot send that" };
      const res = await T.rowVerb(rowId, verb, req || {}, confirm === true);
      // what the host said it did is the page's notice (an edit's row stays, drawn with the new words)
      const said = res && res.ok && res.data && typeof res.data.said === "string" ? res.data.said : "";
      if (said && typeof T.notice === "function") T.notice(said);
      return res;
    };
  }
  function hasIntentionControls(actions) {
    // the page's one write predicate (every other write control is gated on it): a render with no `commit` is read-only and draws none
    return !!commit && Array.isArray(actions) && actions.some((a) => a && INTENTION_CONTROLS.some((c) => c[1] === a.verb));
  }
  // `text`: the reminder's own words (never the row's filed words); "" when the host sent none, and Edit then starts empty
  function buildIntentionVerbs(rowId, actions, text) {
    const wrap = el("span", "verb-actions intention-actions");
    const words = typeof text === "string" ? text : "";
    for (const [label, verb, title] of INTENTION_CONTROLS) {
      const btn = verbBtn(label, title, async (ev) => {
        if (verb === "intention_snooze") {
          openSurfaceForm(wrap, { next: null }, { label: "snooze", post: (v) => intentionPost(rowId, verb)({ until: v }),
            min: localDayIso(1), minToday: localDayIso(0), back: (v) => "comes back " + dayTimeWords(v), done: (v) => "snoozed to " + dayTimeWords(v), stale: INTENTION_STALE });
          return;
        }
        if (verb === "intention_edit") {
          openRowForm(wrap, () => {
            const ta = el("textarea", "verb-edit-ta");
            ta.value = words;
            ta.rows = Math.min(16, Math.max(3, ta.value.split("\n").length, Math.ceil(ta.value.length / 56)));
            ta.dataset.orig = ta.value;
            return { nodes: [ta], focus: ta, ta };
          }, (f, msg) => {
            if (!f.ta.value.trim()) { msg.className = "edit-msg err"; msg.textContent = "text can't be empty"; return null; }
            // only what the operator changed is written: an untouched field sends nothing
            if (f.ta.value === f.ta.dataset.orig) { msg.className = "edit-msg"; msg.textContent = "nothing changed"; return null; }
            return { text: f.ta.value };
          }, "save", false, intentionPost(rowId, verb), INTENTION_STALE);
          return;
        }
        if (verb === "intention_clear") {
          const ask = words ? "Drop \u201c" + words.slice(0, 120) + (words.length > 120 ? "\u2026" : "") + "\u201d for good?" : "Drop this reminder for good?";
          openRowForm(wrap, () => ({ nodes: [el("span", "muted", ask)], focus: null }),
            () => ({}), "Clear", true, intentionPost(rowId, verb, true), INTENTION_STALE);
          return;
        }
        const b = ev.currentTarget; b.disabled = true;
        const res = await intentionPost(rowId, verb)({});
        if (res && res.ok) return;  // the page re-reads and redraws without this row
        b.disabled = false;
        verbErr(wrap, res);
        if (res && res.error === "stale") { const m = wrap.querySelector(".edit-msg"); if (m) m.textContent = INTENTION_STALE; }
      });
      btn.dataset.verb = verb;
      wrap.appendChild(btn);
    }
    return gateByActions(wrap, actions);
  }

  // The who form: a row that already holds a target changes hands without touching its day (intention_retarget{target}).
  function openWhoForm(host, s) {
    if (host.querySelector(".verb-form")) return;
    const stale = host.querySelector(".edit-msg"); if (stale) stale.remove();
    const triggers = Array.prototype.slice.call(host.querySelectorAll(".verb-btn"));
    triggers.forEach((b) => (b.style.display = "none"));
    const form = el("span", "verb-form who-form");
    const who = whoControl(s);
    const go = el("button", "verb-confirm", "set");
    const cancel = el("button", "verb-cancel", "cancel");
    go.type = "button"; cancel.type = "button";
    const msg = el("span", "edit-msg", "");
    form.append(who.node, go, cancel, msg);
    host.appendChild(form);
    const close = () => { form.remove(); triggers.forEach((b) => (b.style.display = "")); };
    cancel.addEventListener("click", close);
    form.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault(); ev.stopPropagation();
      if (!go.disabled) close();
    });
    go.addEventListener("click", async () => {
      const t = who.value();
      if (t === false) { msg.className = "edit-msg err"; msg.textContent = "the machine's repo is a plain name (letters, digits, . _ -), or desk"; return; }
      if (t === null || t === rowTarget(s)) { msg.className = "edit-msg err"; msg.textContent = "pick who does it"; return; }
      go.disabled = true; cancel.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit({ kind: "intention_retarget", intention_id: s.id, target: t });
      if (res && res.ok) return;  // re-rendered
      showFail(msg, res, "failed");
      go.disabled = false; cancel.disabled = false;
    });
  }

  // The then form (intention_then): the second part of a split, filed as a band agenda that wakes when this row is marked done
  function openThenForm(host, s) {
    openRowForm(host, () => {
      const inp = el("input", "verb-ref");
      inp.type = "text"; inp.maxLength = 300; inp.placeholder = "then you...";
      inp.setAttribute("aria-label", "what you do after this is done");
      return { nodes: [inp], focus: inp };
    }, (f, msg) => {
      const text = f.nodes[0].value.trim();
      if (!text) { msg.className = "edit-msg err"; msg.textContent = "say what you do next"; return null; }
      return { kind: "intention_then", intention_id: s.id, text, expect_disposition: s.disposition };
    }, "file it", false, async (req) => {
      // the page re-draws on success and this form goes with it: the server's sentence is kept in the List's receipt line
      const res = await commit(req);
      if (res && res.ok && res.data && typeof res.data.note === "string") { intakeKept.receipt = { text: res.data.note, cls: "ok" }; paintIntakeReceipts(); }
      return res;
    });
  }

  // The List's receipt line (under the intake box) says what a write did, in the server's own sentence: a Keep names its episode,
  // an agenda says when it wakes. It is kept across the redraw that follows the write (intakeKept).
  function listReceipt(res) {
    const line = res && res.ok && res.data && typeof res.data.receipt_line === "string" ? res.data.receipt_line : "";
    if (line) { intakeKept.receipt = { text: line, cls: "ok" }; paintIntakeReceipts(); }
  }
  const LIST_ROW_STALE = "this row changed: reload";
  // The List's two irreversible writes (the Keep, the band row to agenda): a send that THROWS (the network dropped mid-request) is not
  // a refusal; the write may have landed. The form says so in one fixed line and its buttons come back (both writes are safe to repeat).
  const WRITE_UNSURE = "Could not tell whether that went through. Reload the List and check; trying again is safe.";
  const unsureOnThrow = (post) => async (req) => {
    try { return await post(req); } catch (_) { return { ok: false, error: "unsure", message: WRITE_UNSURE }; }
  };

  // The Keep (intention_keep): the row's words become a memory (an anneal episode), then the item leaves the List. It is
  // irreversible from here, so the form asks once, in words, and the confirm tap sends confirm:true.
  function openKeepForm(host, s) {
    const chars = Array.from(String(s.text || ""));      // by character, never by UTF-16 unit: an astral character at the cut stays whole
    const ask = "Keep \u201c" + chars.slice(0, 120).join("") + (chars.length > 120 ? "\u2026" : "") + "\u201d as a memory? It leaves the List; search finds it.";
    openRowForm(host, () => ({ nodes: [el("span", "muted keep-ask", ask)], focus: null }), () => ({}), "Keep", false,
      unsureOnThrow(async (req) => { const res = await intentionPost(s.row_id, "intention_keep", true)(req); listReceipt(res); return res; }), LIST_ROW_STALE);
  }

  // The band row to agenda (band_agenda): a machine-filed band row he wants past the snooze window becomes a dated agenda that
  // wakes back into the band on its day, and the row is answered so the stopped agent waits. The day form is the date alone;
  // the server judges the day (a day inside the window is refused in its own words, shown in the form) and the confirm tap
  // sends confirm:true.
  function buildBandAgendaVerb(s) {
    const wrap = el("span", "verb-actions band-agenda-actions");
    wrap.appendChild(verbBtn("Make an agenda", "push this row past the snooze window: it comes back on the day you pick, as an agenda", () =>
      openSurfaceForm(wrap, { next: null }, {
        label: "make agenda", dateOnly: true, min: localDayIso(1),
        post: unsureOnThrow(async (v) => { const res = await intentionPost(s.row_id, "band_agenda", true)({ day: v }); listReceipt(res); return res; }),
        back: (v) => "comes back " + dayYearWords(v) + " as an agenda; this row is answered so the stopped agent waits",
        done: (v) => "agenda for " + dayYearWords(v), stale: LIST_ROW_STALE })));
    return gateByActions(wrap, s.actions);
  }
  const hasBandAgenda = (actions) => !!commit && Array.isArray(actions) && actions.some((a) => a && a.verb === "band_agenda");

  // Shared inline-form scaffold for the per-row verbs (edit / reclassify / remove): hide the
  // trigger buttons, mount a form, wire cancel + Escape (stopPropagation so it doesn't nuke an
  // enclosing focus-modal), and run `onConfirm` which returns the commit request (or null to
  // validate-fail with `msg`). One form per row at a time.
  // A write refused as stale (intention-174) leaves the form that submitted it pointing at a row this page no
  // longer has right. Retire THAT form only: its inputs stop counting as unsaved text (so the re-read that
  // replaces the row is not held off by the refused request's own leftovers) and its buttons stay off so the old
  // request cannot be sent again. Other open editors are untouched. The boot layer re-reads on the event.
  function retireStaleForm(form) {
    for (const v of form.querySelectorAll(".verb-ref, .tray-dump-ta")) v.value = "";
    for (const v of form.querySelectorAll(".verb-edit-ta, .verb-domain")) v.value = v.dataset.orig || "";
    for (const b of form.querySelectorAll("button")) b.disabled = true;
    document.dispatchEvent(new CustomEvent("levain:editor-closed"));
  }

  // `post` (the intention verbs): sends the request instead of the legacy write; `staleWords`: what a stale refusal reads as
  function openRowForm(host, buildFields, onConfirm, confirmLabel, danger, post, staleWords) {
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
      const res = await (post || commit)(req);
      if (res && res.ok) return;  // re-rendered
      showFail(msg, res, "failed");
      if (res && res.error === "stale") { if (staleWords) msg.textContent = staleWords; retireStaleForm(form); return; }
      go.disabled = false; cancel.disabled = false;
    });
  }

  // Inline text edit — the forming-workbench refine (intention_update{text}). Any intention. When the kernel offers
  // `domain` on this row's edit, the form carries a domain input too (prefilled from the row): domain is
  // sent only when it differs from what was shown, and as "" only when the operator clears a domain on
  // purpose. A text-only edit keeps the same request.
  function offersDomain(s) {
    const a = Array.isArray(s.actions) ? s.actions.find((x) => x && x.verb === "intention_update") : null;
    return !!a && Array.isArray(a.escalates) && a.escalates.some((x) => x && x.param === "domain");
  }
  function openTextEdit(host, s) {
    const withDomain = offersDomain(s);
    openRowForm(host, () => {
      const ta = el("textarea", "verb-edit-ta");
      ta.value = s.text || "";  // FULL canonical text — the wire no longer truncates, so edit
      // reads/writes the WHOLE thing (edit-raw, no data loss). Size to content so a long intention
      // is visible without hunting in a 2-row box (it still scrolls past the cap; the full
      // value is always in .value regardless).
      const lines = ta.value.split("\n").length;
      ta.rows = Math.min(16, Math.max(3, lines, Math.ceil(ta.value.length / 56)));
      ta.dataset.orig = ta.value;  // dirty baseline for hasUnsavedEdit (intention-108)
      if (!withDomain) return { nodes: [ta], focus: ta, ta };
      const dom = el("input", "verb-domain");
      dom.type = "text"; dom.maxLength = 64; dom.placeholder = "domain";
      dom.setAttribute("aria-label", "domain");
      dom.value = s.domain || "";
      dom.dataset.orig = dom.value;
      return { nodes: [ta, dom], focus: ta, ta, dom };
    }, (f, msg) => {
      // Save the value VERBATIM — only trim for the empty-CHECK. trimming the saved text
      // would silently strip an intention's structural leading/trailing newlines on its first
      // edit (the small sibling of the truncation data-loss this slice fixes) [complement L3].
      const text = f.ta.value;
      if (!text.trim()) { msg.className = "edit-msg err"; msg.textContent = "text can't be empty"; return null; }
      if (!f.dom) return { kind: "intention_update", intention_id: s.id, text };
      const req = { kind: "intention_update", intention_id: s.id };
      const textChanged = text !== (s.text || "");
      const domChanged = f.dom.value.trim() !== (s.domain || "");
      if (!textChanged && !domChanged) { msg.className = "edit-msg err"; msg.textContent = "nothing changed"; return null; }
      if (textChanged) req.text = text;
      if (domChanged) req.domain = f.dom.value.trim();   // "" only when a non-empty domain was cleared
      return req;
    }, "save", false);
  }

  // A freeform capture box. `submit` builds the request from the typed text. Used by the
  // Tray dump (no type is asked for). No own label — it mounts right
  // under the panel's title (Tray (X)), like the content of every other panel;
  // the placeholder carries the purpose hint.
  function buildCaptureBox(placeholder, btnLabel, emptyMsg, withType, buildReq, sortEntity) {
    const box = el("div", "tray-dump");
    const ta = el("textarea", "tray-dump-ta");
    ta.placeholder = placeholder; ta.title = "⌘/Ctrl+Enter sends"; ta.rows = 2;
    box.appendChild(ta);
    const controls = el("div", "tray-dump-controls");
    // no type is asked for: the server defaults it and the entity sorts the line (the dump is "let <entity> sort")
    const go = el("button", "tray-dump-btn", btnLabel);
    go.type = "button";
    const msg = el("span", "edit-msg", "");
    const submit = async () => {
      const text = ta.value.trim();
      if (!text) { msg.className = "edit-msg err"; msg.textContent = emptyMsg; return; }
      go.disabled = true;
      msg.className = "edit-msg busy"; msg.textContent = "…";
      const res = await commit(buildReq(text));
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
  // cognition until triaged). The entity sorts it: no type is asked for.
  function buildTrayDump(entity) {
    return buildCaptureBox(
      `Drop anything. ${entity} sorts it.`,
      "drop", "nothing to drop", false,
      (text) => ({ kind: "intention_seed", text }), entity);
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
      const req = { kind: kind, intention_id: s.id, intention_kind: sel.value, confirm: true };
      // intention-173/174: the face (compost/dismiss) and the promote were chosen off the rendered row, so
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
  // the episodes/intentions idiom), plus an optional `accent` (e.g. unread) / `dim` (e.g. read)
  // flag. The kernel knows nothing domain-specific — the flow Bridge renders its inbox here.
  // Every store-derived string is textContent (via el()/appendClause), never innerHTML.
  // The governed COMPOSE affordance for an external panel's action verb (the external-panel-
  // ACTION seam — the write-peer of how intention panels carry verbs). One control per declared
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
        // the person's pick: the one place the card's need for a choice is answered
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
      noContactFill(node);
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
        + "on the laptop, levain desk review clear " + rv.item));
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
    if (entry.id === "notifications") return ncPanel(p);   // the one notification list, drawn compact (S4.2)
    const data = (view.extra_panels || {})[entry.id] || {};
    if (data.error) {
      p.appendChild(el("p", "err", "unavailable — " + data.error));
      return p;
    }
    // Governed compose affordance: mount it when the surface is writable (commitAction injected)
    // AND the panel declares an `action` whose verb is in the server's registered action_verbs.
    // Mounted under the head (mountCaptureBox) so it sits on top + the expand-to-modal transplants
    // it — like the Tray dump. Mounted EARLY so it shows even on an empty panel
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
    if (entry.id === "decisions" && bandRead) p.appendChild(bandSlot());   // the NEEDS YOU band, by number
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
    // The terminal copy lines were the Q6a answer route while the cockpit was read-only; a passkey answer (Q6b) replaced them.
    // Each stays unless the sheet offers that same answer here (a passkey never signs a T3 allow, so its choices are deny only).
    const sheetOffers = (k) => b.sheet === true && Array.isArray(b.sheet_choices) && b.sheet_choices.includes(k);
    if (pending) {
      for (const [label, cmd] of [["allow", sv(b.answer_allow)], ["deny", sv(b.answer_deny)]]) {
        if (sheetOffers(label)) continue;
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
  // question n -> the short handle of its words (needsName), learned when the words are read. It outlives a closed view on purpose
  // (the glance tile names the item after the dialog closes) and is a few words, bounded to the newest QUESTION_HANDLES_MAX.
  const questionHandles = new Map();
  // question key -> the first line of its words, whole up to QUESTION_LINE_CHARS: a wide focus view titles itself with it (the handle
  // is a strip row's few words; a desk's dialog has the room). Bounded with the handles.
  const questionLines = new Map();
  // the open Needs focus view's item ({it, id}), and whether the breakpoint listener is installed: declared here, before anything that
  // can open a focus view during init reads them (a `let` read earlier than its line throws)
  let needsFocusNow = null, wideWatched = false;
  const QUESTION_LINE_CHARS = 140;
  const QUESTION_HANDLE_WORDS = 6, QUESTION_HANDLE_CHARS = 60, QUESTION_HANDLES_MAX = 200;
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
  // a handle is kept by the question's card id (flow's "question:<n>"), else its number: one string key either way, so a number
  // that arrives as a string and as a number is one question
  const questionKeyOf = (q) => (q && q.id ? String(q.id) : "n:" + String(q && q.n));
  // cut by code points (never inside a surrogate pair); a trailing partial word goes only when the cut fell inside it
  function cutQuestionLine(line) {
    const cps = Array.from(line);
    if (cps.length <= QUESTION_LINE_CHARS) return line;
    let head = cps.slice(0, QUESTION_LINE_CHARS - 1).join("");
    if (!/\s/.test(cps[QUESTION_LINE_CHARS - 1])) head = head.replace(/\s+\S*$/, "");
    return head.replace(/\s+$/, "") + "\u2026";    // never a space before the ellipsis, whichever way the cut fell
  }
  function rememberQuestionHandle(q, handle, line) {
    const k = questionKeyOf(q);
    const prevLine = questionLines.get(k);
    const changed = questionHandles.get(k) !== handle;
    questionHandles.delete(k);
    questionHandles.set(k, handle);
    if (questionHandles.size > QUESTION_HANDLES_MAX) questionHandles.delete(questionHandles.keys().next().value);
    if (line) {
      questionLines.delete(k);
      questionLines.set(k, cutQuestionLine(line));
      if (questionLines.size > QUESTION_HANDLES_MAX) questionLines.delete(questionLines.keys().next().value);
    }
    if (changed || (line && questionLines.get(k) !== prevLine)) {
      repaintNeedsNames(k);
      // a read that lands while this question's focus view is open (another surface's read of the same words) retakes its title too
      if (needsFocusNow && needsFocusNow.it.kind === "question" && questionKeyOf(needsFocusNow.it.q) === k) retitleNeedsFocus(needsFocusNow.it, needsFocusNow.id);
    }
  }
  // The glance tile is drawn before a question's words are read, so a name learned later is painted into the tile that already
  // stands (cockpit_view.js glanceFace marks it with the item it names). Only a tile naming `key` is touched (all of them when
  // key is absent: the write token came or went).
  function repaintNeedsNames(key) {
    if (typeof document === "undefined") return;
    for (const t of document.querySelectorAll(".glance-top")) {
      const it = t._needsItem;
      if (!it || it.kind !== "question" || (key !== undefined && questionKeyOf(it.q) !== key)) continue;
      const name = needsName(it);
      if (t.textContent !== name) t.textContent = name;
    }
  }
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("levain:write-token-change", () => repaintNeedsNames());
  }
  function questionBody(q, row) {
    const key = q.n + "|" + q.state;
    if (questionBodies.has(key)) return questionBodies.get(key);
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ error: CONSENT_NO_TOKEN, noToken: true });
    const got = consentQueued(() => questionFetch(q, headers, 1), "the question could not be read", row)
      .then((b) => {
        if (b.error && questionBodies.get(key) === got) questionBodies.delete(key);
        else if (!b.error && typeof b.what === "string") {
          // the words were fetched with a token: a handle made from them is kept only while this page still holds one. A read that
          // lands after the token went is not cached either, so the read that follows the token's return learns the handle.
          if (hasWriteToken()) { const h = questionHandle(b.what); if (h) rememberQuestionHandle(q, h, String(b.what || "").split(/\r?\n/).map((l) => l.trim()).find(Boolean) || ""); }
          else if (questionBodies.get(key) === got) questionBodies.delete(key);
        }
        return b;
      }, (e) => {
        // a rejected read is not kept: a retry must be able to read again, and the title be taken then
        if (questionBodies.get(key) === got) questionBodies.delete(key);
        throw e;
      });
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
      // the prefix counts only at a word boundary: end of text, whitespace, or ?.: then whitespace/end ("Ship?" is not "Shipment ...")
      if (w && t.startsWith(w) && /^(?:$|\s|[?.:](?:\s|$))/.test(t.slice(w.length))) return t.slice(w.length).replace(/^[?.:\s]+/, "");
    }
    return t;
  }
  // a question body's long text: the one with its line breaks when the body carries it with exactly the words of `blocks`
  // (card_format.js preferFull), else `blocks` as it came
  function blocksOf(b) {
    const F = window.LevainCardFormat;
    const flat = typeof b.blocks === "string" ? b.blocks : "";
    return F && typeof F.preferFull === "function" ? F.preferFull(flat, b.blocks_full) : flat;
  }
  // a question's long text drawn by card_format.js: headed sections, lists, and the options once as rows (text only); null when
  // the module is not loaded or throws, and the caller draws the one line it drew before
  function questionText(text, dropping, options, rec, mode) {
    const F = window.LevainCardFormat;
    if (!F || typeof F.drawBody !== "function" || !text) return null;
    try { return F.drawBody(el("div", "card-body needs-q-body"), text, dropping, options, rec, mode); } catch (e) { return null; }
  }
  // how card_format.js reads blocksOf(b): by lines only when that is the body's `blocks_full`, and a command line is a copy
  // button only on a question that is not answered by a signature (q.sign): the line breaks are not signed, so a signed
  // question's commands are plain selectable monospace and never reach copyLine
  function blocksMode(q, b) {
    const F = window.LevainCardFormat;
    return { lines: !!(F && typeof F.usesFull === "function" && F.usesFull(b.blocks, b.blocks_full)), copy: !q.sign };
  }
  // the answered questions among a panel's lines, newest decision first, at most `max` of them (a Set of the line.question objects)
  const ANSWERED_ROWS_EAGER = 5;
  function newestAnswered(lines, max) {
    const at = (q) => { const ms = Date.parse(q.decidedAt); return Number.isFinite(ms) ? ms : 0; };
    return new Set((Array.isArray(lines) ? lines : []).map((ln) => ln && ln.question).filter((q) => q && q.state === "answered")
      .sort((a, b) => at(b) - at(a)).slice(0, max));
  }
  // The one line an answered question carries on every surface: "Answered: K · label | his words · signed | unsigned · time · via channel".
  // `signed` null (the store cannot say) adds nothing; an unreadable time adds nothing. Every string is the agent's or the operator's and
  // is drawn as text only.
  function answerLine(a, options) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const parts = [];
    if (a.kind === "option") {
      const opt = (Array.isArray(options) ? options : []).find((o) => o && o.key === a.value);
      const label = sv(a.label) || (opt ? sv(opt.label) : "");
      parts.push(sv(a.value) + (label ? " \u00b7 " + label : ""));
    } else {
      parts.push(sv(a.value));
    }
    if (a.signed === true) parts.push("signed");
    else if (a.signed === false) parts.push("unsigned");
    const ms = Date.parse(sv(a.answered_at));
    if (Number.isFinite(ms)) {
      parts.push(new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
    }
    if (sv(a.channel)) parts.push("via " + sv(a.channel));
    return "Answered: " + parts.join(" \u00b7 ");
  }
  // `card` (the Needs-you strip's card) draws the mock's anatomy: the question as a bold title, "blocks: ...", then "REC key. words";
  // the options, the words box and the send button go to the card's own bottom row, so cards in a row end their actions together.
  // `receipt` (a settled Needs-you card): the question is decided, so only the title, the answer line and what it blocked are
  // drawn; the options, the recommendation and every control are not built at all.
  // the mics a slot's last draw made (board row 140) end with it: a redraw never leaves a capture running into a box that left
  function offSlotMics(slot) {
    if (Array.isArray(slot.__mics)) for (const m of slot.__mics) m.off();
    slot.__mics = [];
  }
  function drawQuestionBody(q, slot, b, card, receipt) {
    offSlotMics(slot);
    const sv = (x) => (typeof x === "string" ? x : "");
    const open = q.state === "open";
    // an open row that rings a reminder reads as the reminder's own words (what its Edit changes), not the words it was filed with
    if (open && q.intention && b && !b.error && sv(b.intention_text)) b = Object.assign({}, b, { what: b.intention_text });
    const cli = "levain question show " + q.n;   // the options are listed there; answer with: levain question answer N --option KEY
    if (receipt && card && (b.error || !(b.answer && typeof b.answer === "object"))) {
      // a settled card is a receipt whatever the read did: a failed read or a missing answer never brings back the options, the
      // recommendation or any control; it keeps what is known (the title when the body came, else the question's number) and says so
      const known = !b.error;
      const parts = [el("div", "needs-q-title", (known && sv(b.what)) || "Question " + q.n),
                     el("div", "consent-note ext-meta needs-answer-line", known ? "answer not shown" : "answer not shown (" + String(b.error).slice(0, 200) + ")")];
      const more = known ? blocksBeyondTitle(sv(b.what), blocksOf(b)) : "";
      if (more) parts.push(questionText(more, null, [], "", blocksMode(q, b)) || el("div", "ext-meta needs-q-blocks", "blocks: " + more));
      slot.replaceChildren(...parts);
      return;
    }
    if (b.error) {
      const parts = [el("div", "consent-note ext-meta", b.error)];
      if (open) parts.push(copyLine(cli, "show command"));   // no token: the terminal route still answers
      slot.replaceChildren(...parts);
      return;
    }
    const options = Array.isArray(b.options) ? b.options : [];
    const parts = [];
    const settledAnswer = receipt && card && b.answer && typeof b.answer === "object" ? b.answer : null;
    if (settledAnswer) {
      parts.push(el("div", "needs-q-title", sv(b.what)));
      parts.push(el("div", "consent-note ext-meta needs-answer-line", answerLine(settledAnswer, options)));
      const more = blocksBeyondTitle(sv(b.what), blocksOf(b));
      if (more) parts.push(questionText(more, null, [], "", blocksMode(q, b)) || el("div", "ext-meta needs-q-blocks", "blocks: " + more));
      slot.replaceChildren(...parts);
      return;
    }
    let head = null;     // the focus view's title, blocks and REC lines: a needs-phill sign card is drawn in their place
    if (card) {
      parts.push(el("div", "needs-q-title", sv(b.what)));
      const more = blocksBeyondTitle(sv(b.what), blocksOf(b));
      if (more) parts.push(questionText(more, null, options, sv(b.rec), blocksMode(q, b)) || el("div", "ext-meta needs-q-blocks", "blocks: " + more));
      const recOpt = options.find((o) => o && sv(o.key) === sv(b.rec));
      if (sv(b.rec)) {
        const rl = el("div", "needs-rec");
        rl.append(el("span", "needs-rec-tag", "REC"), document.createTextNode(" " + sv(b.rec) + (recOpt && sv(recOpt.label) ? ". " + sv(recOpt.label) : "")));
        parts.push(rl);
      }
      head = parts.slice();
    } else {
      parts.push(el("pre", "consent-display", sv(b.what)));
      const facts = el("div", "consent-facts");
      facts.appendChild(el("span", "ext-meta", b.open_ended || !sv(b.rec) ? "open question: answer in your own words" : "recommends " + sv(b.rec)));
      const formatted = questionText(blocksOf(b), null, options, sv(b.rec), blocksMode(q, b));
      if (formatted) facts.appendChild(formatted);
      else facts.appendChild(el("span", "ext-meta", "blocks: " + sv(b.blocks)));
      parts.push(facts);
    }
    const a = b.answer && typeof b.answer === "object" ? b.answer : null;
    if (a) {
      parts.push(el("div", "consent-note ext-meta", answerLine(a, options)));
      slot.replaceChildren(...parts);
      return;
    }
    if (open && q.intention && hasIntentionControls(q.intentionActions)) {
      // a row that rings an intention is acted on by its four controls, in place of any answer box or signature
      const wrap = buildIntentionVerbs(q.id, q.intentionActions, b ? sv(b.intention_text) : "");     // never the filed words
      if (card) {
        const bottom = el("div", "needs-actions needs-qa needs-intention");
        bottom.appendChild(wrap);
        slot.replaceChildren(...parts);
        card.appendChild(bottom);
      } else {
        parts.push(wrap);
        slot.replaceChildren(...parts);
      }
      return;
    }
    if (open && q.sign) {
      // answered by a signature (DESIGN_SIGN D5): the sign request's own options are the buttons, and no unsigned answer is
      // drawn beside them (an unsigned tap on a row its caller asked to be signed is refused there, never his word)
      const facet = signFacet(q.sign, head, blocksOf(b));
      slot.__mics = facet.__mics;
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
    // a mic's own buttons are locked by its lock(): a live capture's Stop stays enabled
    const lockAll = (on) => { for (const c of controls) c.disabled = on; if (wmic) wmic.lock(on); };
    const send = async (params) => {
      if (wmic) wmic.stop();     // an answer under a live mic ends the mic first
      lockAll(true);
      let res;
      try { res = await answerQuestion(q.id, params); } catch (e) { res = { ok: false, message: "the answer could not be sent" }; }
      if (res && res.ok) { words.value = ""; return; }   // the board re-reads and redraws this row as answered
      const kept = res && res.error === "stale" && words.value.trim() !== "";
      if (kept) { questionDrafts.set(q.id, words.value); words.value = ""; }
      lockAll(false);
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
    noContactFill(words);
    words.placeholder = b.open_ended ? "your answer, in your own words" : card ? "or your own words" : "or answer in your own words";
    if (questionDrafts.has(q.id)) { words.value = questionDrafts.get(q.id); questionDrafts.delete(q.id); }
    const sendWords = el("button", "undo-btn question-send", "send words");
    sendWords.type = "button";
    // the streaming Mic and on-device Dictate on the reply box (board row 140), drawn only where the server and browser offer them;
    // send words waits for the mic to end, so a partial is never sent
    const wmic = window.LevainMic && typeof window.LevainMic.attach === "function" ? window.LevainMic.attach(words, { cls: "undo-btn", note: (t) => { msg.textContent = t; } }) : null;
    sendWords.addEventListener("click", () => {
      if (wmic && wmic.busy()) { msg.textContent = "Stop the mic first: your words are sent as they stand when it has ended."; return; }
      if (words.value.trim()) send({ text: words.value });
    });
    controls.push(words, sendWords);
    slot.__mics = wmic ? [wmic] : [];
    const micBtns = wmic ? wmic.buttons : [];
    // the mic icons and send words share one row: the mic never drops under the box as a ragged line of its own
    const sendRow = el("div", "voice-row");
    sendRow.append(...micBtns, sendWords);
    if (card) {
      const bottom = el("div", "needs-actions needs-qa");
      bottom.append(wrap, words, sendRow, msg);
      slot.replaceChildren(...parts);
      card.appendChild(bottom);
      return;
    }
    parts.push(wrap, words, sendRow, msg);
    slot.replaceChildren(...parts);
  }
  // iOS keyboards take a free-text box for a contact field when the card around it reads like mail (Phill 11-07, an
  // email-sourced card). These boxes hold prose, never a name or address, so say so to the keyboard and password managers.
  function noContactFill(node) {
    node.setAttribute("autocomplete", "off");
    node.setAttribute("autocapitalize", "sentences");
    node.setAttribute("inputmode", "text");
    node.setAttribute("enterkeyhint", "enter");
    node.setAttribute("data-form-type", "other");
    node.setAttribute("data-lpignore", "true");
    node.setAttribute("data-1p-ignore", "true");
  }
  // The NEEDS YOU band (Q17-ALL S5b), drawn at the head of the Decisions panel: each open item under its band number, one
  // shorthand box ("15b", "1a 2b") and a tap on an option sending "<n><letter>". Reading the band shows it to this credential
  // (numbers it, records the view); an answer carries the {store_id, view_id} shown. The box answers information items only
  // (unsigned, a human channel): an authority item is listed and says it opens its signing sheet. Every string is drawn with
  // textContent, never as HTML.
  const bandNumOf = new Map();    // question n -> band number, from the last band read: a question row leads with it
  // the cards' own copy of the band numbers (bandNumOf also feeds needsName's title, which must not change when a card reads the band)
  const cardBandOf = new Map();
  let cardBandSeq = 0;      // the render whose band read may still set cardBandOf (a superseded response is ignored)
  const cardBandNum = (n) => { const k = Number(n); return bandNumOf.has(k) ? bandNumOf.get(k) : cardBandOf.get(k); };
  let bandKeep = "";              // what the last send said about refusals and sheets: a redraw gives it back until the next send
  let bandGot = null;             // the last read, shared for a moment by the slots one render draws (the server takes one at a time)
  function bandFetch(fresh) {
    if (!fresh && bandGot && Date.now() - bandGot.at < 2000) return bandGot.p;
    const p = bandRead();
    bandGot = { at: Date.now(), p: p };
    p.catch(() => { if (bandGot && bandGot.p === p) bandGot = null; });
    return p;
  }
  function applyBandNumbers() {
    for (const row of document.querySelectorAll("[data-question-row]")) {
      const tag = row.querySelector(".band-num");
      const n = bandNumOf.get(Number(row.dataset.questionN));
      if (tag) { tag.textContent = n === undefined ? "" : String(n); tag.hidden = n === undefined; }
    }
    for (const row of document.querySelectorAll(".needs-row[data-question-n], .needs-later-line[data-question-n]")) {
      const tag = row.querySelector(".needs-row-band, .needs-later-band");
      const n = cardBandNum(row.dataset.questionN);
      if (tag) { tag.textContent = n === undefined ? "" : String(n); tag.hidden = n === undefined; }
    }
    // the LATER lines keep flow's order: soonest day, then band number (known only once the band is read)
    for (const sec of document.querySelectorAll(".needs-later")) {
      const rows = Array.from(sec.children).filter((r) => r.dataset && r.dataset.laterDay);
      const key = (r) => { const b = cardBandNum(r.dataset.questionN); return Number(b === undefined ? r.dataset.questionN : b); };
      const sorted = rows.slice().sort((a, b) => (a.dataset.laterDay < b.dataset.laterDay ? -1 : a.dataset.laterDay > b.dataset.laterDay ? 1 : key(a) - key(b)));
      if (sorted.some((r, i) => r !== rows[i])) for (const r of sorted) sec.appendChild(r);
    }
  }
  function bandSlot() {
    const slot = el("div", "band-slot");
    slot.setAttribute("data-band-slot", "");
    bandFill(slot, "", false);
    return slot;
  }
  function bandFill(slot, say, fresh, retried) {
    slot.removeAttribute("data-band-read");     // the last draw's marker must not outlive the read that replaces it
    bandFetch(fresh).then((b) => {
      // a draw that throws is terminal and said (a retry draws the same payload the same way), never a marker left unset
      try { bandDraw(slot, b, say); } catch (e) {
        slot.setAttribute("data-band-read", "error");
        slot.replaceChildren(el("div", "ext-meta band-note", "the band could not be drawn" + (e && e.message ? ": " + e.message : "")));
      }
    }, (e) => {
      const m = e && e.message ? String(e.message) : "";
      if (/no question store/.test(m)) { slot.replaceChildren(); slot.setAttribute("data-band-read", "none"); return; }     // nothing to show on a cockpit without one
      // the server reads one band at a time (a second look meeting the first is told "busy"): one more try, then say so
      if (!retried) { setTimeout(() => bandFill(slot, say, true, true), 700); return; }
      slot.replaceChildren(el("div", "ext-meta band-note", "the band could not be read" + (m ? ": " + m : "")));
      slot.setAttribute("data-band-read", "error");
    });
  }
  function bandDraw(slot, b, say) {
    b = b && typeof b === "object" ? b : {};
    const items = Array.isArray(b.items) ? b.items : [];
    bandNumOf.clear();
    for (const it of items) if (it && Number.isInteger(it.question)) bandNumOf.set(it.question, it.n);
    const kids = [];
    // the settle marker the page owns: a read has landed and been drawn (a test waits on it, never on a clock)
    const read = b.store === "absent" ? "absent" : (b.note ? "note" : "ok");
    // no store yet: nothing was ever filed, so no band and no line, unless an answer's confirmation is still to be said
    if (b.store === "absent" && !say && !bandKeep) { slot.replaceChildren(); slot.setAttribute("data-band-read", read); applyBandNumbers(); return; }
    if (items.length === 0 && !say && !bandKeep && !b.note) { slot.replaceChildren(); slot.setAttribute("data-band-read", read); applyBandNumbers(); return; }
    kids.push(el("div", "ext-grouphead", "needs you · the band"));
    const msg = el("span", "edit-msg band-msg", say || bandKeep);
    const controls = [];
    const canAnswer = !!(answerBand && b.view_id !== null && b.view_id !== undefined && typeof b.store_id === "string");
    const send = async (text) => {
      text = String(text || "").trim();
      if (!text) return;
      if (!canAnswer) { msg.textContent = "this page cannot answer the band now"; return; }
      for (const c of controls) c.disabled = true;
      bandKeep = "";
      bandGot = null;    // the board redraws on a landed answer and reads the band again: never the look from before it
      let res;
      try { res = await answerBand(text, b.store_id, b.view_id); } catch (e) { res = { ok: false, message: "the answer could not be sent" }; }
      for (const c of controls) c.disabled = false;     // never left waiting on a redraw that may not come
      if (res && res.ok) {
        // the board has re-read and redrawn the band by now (the elements above may be gone): say what the reply says on
        // whatever is on the page, and keep the words that were not recorded
        const d = (res.data && typeof res.data === "object") ? res.data : {};
        const rec = Array.isArray(d.recorded) ? d.recorded : [];
        const refused = Array.isArray(d.refused) ? d.refused : [];
        const sheets = Array.isArray(d.sheets) ? d.sheets : [];
        bandKeep = (rec.length ? "recorded: " + rec.join(", ") : "nothing recorded")
          + (refused.length ? " · refused: " + refused.join("; ") : "")
          + (sheets.length ? " · " + sheets.map((s) => (/^\s*(\d+)/.exec(String(s)) || [0, "?"])[1]).join(", ")
            + " needs your signature: sign it in Decisions" : "");
        const live = document.querySelector("[data-band-slot] .band-msg");
        if (!refused.length && !sheets.length) bandKeep = "";
        if (live) live.textContent = bandKeep;
        return;
      }
      if (res && res.error === "stale_band") {
        // another view of the band was shown since this one: nothing was recorded; the list is read again and drawn fresh
        bandFill(slot, "the band changed since it was shown to you, so nothing was recorded. It is redrawn below: check the numbers and tap again.", true);
        return;
      }
      msg.textContent = (res && (res.detail || res.message || res.error)) || "failed";
    };
    for (const it of items) {
      if (!it || typeof it !== "object") continue;
      const authority = it.kind === "authority";
      if (typeof it.later === "string" && it.later) {     // snoozed (row 131): one collapsed LATER line, number kept, no answer controls
        const lrow = el("div", "row consent-row band-row band-later");
        lrow.setAttribute("data-band-row", String(it.n));
        const lline = el("span", "ext-meta", "");      // the words wrap around the LATER tag; the tag itself stays on one line
        lline.append(el("span", "later-tag", "LATER " + (typeof it.later_label === "string" && it.later_label ? it.later_label : String(it.n) + " " + it.later)),
                     " · " + (typeof it.what === "string" ? it.what.slice(0, 80) : ""));
        lrow.appendChild(lline);
        kids.push(lrow);
        continue;
      }
      const row = el("div", "row consent-row band-row" + (authority ? "" : " ext-accent"));
      row.setAttribute("data-band-row", String(it.n));
      const head = el("div", "consent-head");
      head.appendChild(el("b", "band-num band-num-lead", String(it.n)));
      head.appendChild(el("span", "consent-agent", typeof it.tag === "string" ? it.tag : ""));
      if (authority) head.appendChild(el("span", "ext-meta", "needs your signature"));
      row.appendChild(head);
      row.appendChild(el("div", "band-what", typeof it.what === "string" ? it.what : ""));
      const blocks = Array.isArray(it.blocks) ? it.blocks.join(", ") : (typeof it.blocks === "string" ? it.blocks : "");
      if (blocks) row.appendChild(el("div", "ext-meta", "blocks: " + blocks));
      const opts = el("div", "band-options");
      for (const o of Array.isArray(it.options) ? it.options : []) {
        const label = String(o && o.letter) + " · " + (o && typeof o.label === "string" ? o.label : "") + (o && o.letter === it.rec ? " (recommended)" : "");
        if (authority) { opts.appendChild(el("div", "ext-meta", label)); continue; }
        const btn = el("button", "undo-btn question-option band-option" + (o && o.letter === it.rec ? " question-rec" : ""), label);
        btn.type = "button";
        btn.title = "answer " + it.n + String(o && o.letter);
        btn.addEventListener("click", () => send(String(it.n) + String(o && o.letter)));
        controls.push(btn);
        opts.appendChild(btn);
      }
      row.appendChild(opts);
      kids.push(row);
    }
    if (b.note) kids.push(el("div", "ext-meta band-note", String(b.note)));
    const bar = el("div", "band-bar");
    bar.append(msg);
    kids.push(bar);
    slot.replaceChildren(...kids);
    slot.setAttribute("data-band-read", read);
    applyBandNumbers();
  }
  // `eager`: this answered row is among the newest the panel draws (appendExternalLines), so its answer is fetched without the click
  function questionRow(o, eager) {
    const q = o.question;
    const open = q.state === "open";
    const row = el("div", "row consent-row question-row" + (open ? " ext-accent" : " ext-dim"));
    row.setAttribute("data-question-row", "");
    row.dataset.questionN = String(q.n);
    if (q.id) row.dataset.card = String(q.id);
    const head = el("div", "consent-head");
    const bn = bandNumOf.get(Number(q.n));
    const bandTag = el("b", "band-num", bn === undefined ? "" : String(bn));    // the band number leads; `#n` below is the source tag
    bandTag.hidden = bn === undefined;
    head.appendChild(bandTag);
    head.appendChild(el("span", "consent-agent", q.agent || "agent"));
    head.appendChild(el("b", "consent-tool", "question #" + q.n));
    head.appendChild(el("span", "ext-meta", q.options === 0 ? "open question" : q.options + " options"));
    head.appendChild(el("span", "consent-audit", q.state));
    if (q.age) head.appendChild(el("span", "ext-meta", "filed " + q.age));
    if (!open && q.decidedAge) head.appendChild(el("span", "ext-meta", "answered " + q.decidedAge));
    row.appendChild(head);
    const btn = el("button", "undo-btn consent-details", open ? "open" : "details");
    btn.type = "button";
    btn.title = open ? "read the question and answer it" : "show how this question was answered";
    let slot = null;   // one slot per row: a retry after a failed read reuses it
    let eagerBusy = false, clickedMeanwhile = false;   // the one in-flight marker: an eager read and a click never both draw
    const openBody = () => {
      btn.disabled = true;
      if (!slot) { slot = el("div", "consent-body ext-meta"); row.appendChild(slot); }
      slot.replaceChildren(document.createTextNode("reading the question…"));
      Promise.resolve().then(() => questionBody(q, row)).catch(() => null).then((b) => {
        if (!b) { btn.disabled = false; slot.remove(); slot = null; return; }
        drawQuestionBody(q, slot, b);
        if (b.error) btn.disabled = false; else btn.remove();
      });
    };
    btn.addEventListener("click", () => {
      if (eagerBusy) { clickedMeanwhile = true; btn.disabled = true; return; }   // the eager read draws, or hands the click back
      openBody();
    });
    head.appendChild(btn);
    if (!open && eager) {
      // one of the newest answered rows shows how it was answered without the click. The slot exists only once the read gave an
      // answer, so a failed, token-less or answer-less read leaves no blank space and the details button as it was; the rest of
      // the answered rows draw on that click.
      eagerBusy = true;
      Promise.resolve().then(() => questionBody(q, row)).catch(() => null).then((b) => {
        eagerBusy = false;   // a rejected read is no body: the button stays, a click still works
        if (!slot && b && !b.error && b.answer && typeof b.answer === "object") {
          slot = el("div", "consent-body ext-meta");
          row.appendChild(slot);
          drawQuestionBody(q, slot, b);
          btn.remove();
          return;
        }
        if (clickedMeanwhile) { clickedMeanwhile = false; openBody(); }
      });
    }
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
    offSlotMics(slot);        // a redraw ends the write-in mics of the sign facet it replaces
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
    if (!d && n.state === "open" && n.sign) { const facet = signFacet(n.sign); slot.__mics = facet.__mics; parts.push(facet); }
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
  // the door below refused because no tap asked (NoTapError): no prompt was shown, so it is never called a cancel
  const signDeclined = (e, what) => (e && e.name === "NoTapError"
    ? "no passkey prompt was shown (no tap asked for one in time); " + what + ": tap to try again"
    : "the passkey did not sign (" + ((e && e.name) || "cancelled") + "); " + what + ": tap to try again; if this device holds no passkey for this cockpit, make one with Make a passkey on this device");
  const signWhy = (r) => (r.body && typeof r.body.message === "string" && r.body.message) || ("refused (HTTP " + r.status + ")");
  // The one door to the authenticator. navigator.credentials.get is called from here and nowhere else in this file, and only
  // while a tap is armed. Only a person's own press arms it (event.isTrusted): a click on a control, Space or Enter on a
  // control, Enter in a one-line field, Cmd/Ctrl+Enter anywhere. Space or Enter typed into a text box does not, nor does an
  // evaluated .click() or a dispatched event, a focus, visibility, pageshow, online, reconnect, render or timer, so a passkey
  // sheet taking the window's focus (and the focus coming back) cannot ask for another. One get() consumes the arm (one tap,
  // one ask), and it lapses after SIGN_ARM_MS: long enough for the reads a tap starts before its prompt (the listing, a
  // compose, the challenge), short of the 15 s each read may take, so a flow stalled well past its tap shows no prompt and
  // says a tap tries again. A refusal or cancel is never retried here: the card says so and the next tap asks again.
  // tapArmable is the same text in sign.js (test_passkey_tap_only holds the two equal)
  const SIGN_ARM_MS = 10000;
  // tapArmable:begin
  const TAP_CONTROL = "button, a[href], summary, select, label, input, [role=button], [role=menuitem], [role=option], [role=radio], [role=checkbox], [role=tab], [role=link]";
  const TAP_TEXT = "textarea, [contenteditable]:not([contenteditable=false]), input:not([type]), input[type=text], input[type=search], input[type=email], input[type=url], input[type=tel], input[type=password], input[type=number]";
  function tapArmable(ev) {
    if (!ev || !ev.isTrusted) return false;
    const t = ev.target && typeof ev.target.closest === "function" ? ev.target : null;
    if (!t) return false;
    const text = t.closest(TAP_TEXT);
    if (ev.type === "click") return !text && !!t.closest(TAP_CONTROL);
    if (ev.type !== "keydown" || ev.isComposing || ev.repeat) return false;
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) return true;
    if (text) return ev.key === "Enter" && text.tagName === "INPUT";
    return (ev.key === "Enter" || ev.key === " ") && !!t.closest(TAP_CONTROL);
  }
  // tapArmable:end
  let signArmedAt = 0;
  const signArm = (ev) => { if (tapArmable(ev)) signArmedAt = Date.now(); };
  document.addEventListener("click", signArm, true);
  document.addEventListener("keydown", signArm, true);
  // rp|fp whose remembered id the authenticator refused: signWho passes over that key while another remembered key remains
  // (else a pick, or, with one key enrolled, the OS's own list), until a passkey signs (signKeep clears it, and forgets the
  // record when another key signed)
  const signRefusedId = new Set();
  // `onRefused` runs only when the authenticator itself refused (NotAllowedError from the get() below); the door's own
  // refusal (NoTapError) never reaches it, since no authenticator was asked
  function signGet(pk, onRefused) {
    if (!signArmedAt || Date.now() - signArmedAt > SIGN_ARM_MS) {
      signArmedAt = 0;
      return Promise.reject(Object.assign(new Error("no tap asked for this passkey prompt"), { name: "NoTapError" }));
    }
    signArmedAt = 0;
    return navigator.credentials.get({ publicKey: pk }).catch((e) => {
      if (e && e.name === "NotAllowedError" && onRefused) onRefused();
      throw e;
    });
  }
  const signNotes = new Map();     // request id -> [text, kind, sha256]: what the last ceremony said, kept across redraws
  const signWriteIns = new Map();  // request id -> {sha256, words}: a write-in being typed, kept for the version it was typed on
  // request id -> what a card drawn while that request's ceremony is in flight must undo when the ceremony gives its card back.
  // The focus view builds its card again each time it opens, so a card drawn mid-ceremony is frozen (its buttons and words box
  // disabled) and registers here; the ceremony's end releases it, a signed one stays frozen, and an unsure one is handed to
  // signPending (the cockpit's word on its attempt), which freezes and releases a card drawn after it
  const signInFlight = new Map();
  let signBusy = 0;                // a ceremony (or a passkey pick) in progress: a second tap anywhere does nothing
  // request id -> {attempt, views, asking}: an answer whose reply was lost and that the cockpit has not yet settled. Every
  // board read asks the cockpit about the attempt again (signRecheck, from renderNeeds: the strip keeps a card whose item
  // did not change, so a read alone would never reach it), one ask at a time. `views` are the facets showing it (the frozen
  // card's, and one drawn afresh after its request left the listing): each is told every answer and drops itself once its
  // facet is off the page, so a facet drawn later never takes the ask away from one still shown
  const signPending = new Map();
  const signDecidedSha = new Map();   // request id -> the sha256 of the version the sheet signed ("" when the sheet did not say)
  const signDecided = new Map();      // request id -> the note text of a request the sign sheet signed: every card of it, drawn now or later, is dead
  const signSettledIds = new Set();   // request ids whose signNotes entry is a settled outcome (signed or refused), shown even once the request is gone
  function signSettledSaid(st) {
    if (st.state === "signed") {
      return ["signed" + (typeof st.choice === "string" && st.choice ? " (" + st.choice + ")" : "")
              + " (its reply was lost; the cockpit confirmed it)", "ok"];
    }
    const why = typeof st.reason === "string" && st.reason ? st.reason.replace(/[.\s]+$/, "") : "refused";
    return ["not signed: " + why + (/nothing was signed/.test(why) ? "" : "; nothing was signed") + ": sign it again", "bad"];
  }
  function signAskPending(id) {
    const p = signPending.get(id);
    if (!p || p.asking) return;
    p.asking = true;
    Promise.resolve()
      .then(() => signAttempt(id, p.attempt))
      .catch(() => ({ state: "" }))
      .then((st) => {
        if (signPending.get(id) !== p) return;
        if (st.state === "signed" || st.state === "refused") {
          // the outcome is kept before the entry goes, so a facet drawn later (or on a repaint) shows it
          const prev = signNotes.get(id);
          const said = signSettledSaid(st);
          signNotes.set(id, [said[0], said[1], prev ? prev[2] : ""]);
          signSettledIds.add(id);
          signPending.delete(id);
        }
        for (const view of [...p.views]) { try { view(st); } catch (e) { p.views.delete(view); } }
      })
      .catch(() => undefined)
      .then(() => { p.asking = false; });
  }
  function signRecheck() {
    for (const [id, p] of signPending) if (p.views.size) signAskPending(id);
  }
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
  // after a verified signature: the record names the credential that signed, when it does not already, and a refusal
  // recorded for this rp|fp is over (the next tap asks for the recorded passkey by its id again)
  // `fp` is the key the cockpit's reply NAMES as the one that verified (a plain sign request names its signer only in the
  // reply), never the one the page asked for; null names none, and only the late refusal is cleared
  // A record the authenticator refused on this page while ANOTHER passkey then signed is forgotten: it names a passkey this
  // device no longer holds (or one only cancelled, which a later sign learns again), and kept it would be asked first,
  // and fail first, on every tap after this one (L3 picker r1)
  function signKeep(rp, fp, cred) {
    if (typeof rp === "string" && rp) {
      signRefusedId.delete(rp + "|*");
      if (fp) {
        signRefusedId.delete(rp + "|" + fp);
        for (const k of Object.keys(signMine(rp))) {
          if (k !== fp && signRefusedId.has(rp + "|" + k)) { signForget(rp, k); signRefusedId.delete(rp + "|" + k); }
        }
      }
    }
    if (!fp || !cred || !cred.rawId || typeof rp !== "string" || !rp) return;
    const id = signB64u(cred.rawId);
    if (signMine(rp)[fp] !== id) signRemember(rp, fp, id);
  }
  // The passkey prompt for `fp` on rpId `rp` (the challenge's), `pk` without allowCredentials: asks for `rawId`'s passkey
  // when that id decodes and the authenticator has not refused it on this page since a passkey last signed for rp|fp, else
  // for none (the OS's own list), never a throw. An id this browser remembered that does not decode is forgotten; one the
  // authenticator refuses (NotAllowedError, which a passkey since removed reads as, and a cancel too) is not asked again by
  // the page, and stays remembered until a passkey signs (signKeep clears the refusal, and forgets the record when another
  // key signed)
  // `fp` null is a plain sign request whose signer the answer names (signCeremony): `rawId` is then a list of the ids the
  // page knows (one remembered id, or all the enrolled ones, or none for the OS's own list), asked for together; a refusal
  // of a single remembered id is remembered under rp|<the key it was remembered for>, so signWho passes over that key on
  // the next tap (the next remembered key, else the OS's own list)
  function signAsk(pk, rp, fp, rawId) {
    if (fp === null) {
      const ids = (Array.isArray(rawId) ? rawId : []).filter(signDecodable);
      const mine = signMine(rp);
      const of = ids.length === 1 ? Object.keys(mine).find((k) => mine[k] === ids[0]) : undefined;
      const use = ids.length ? { allowCredentials: ids.map((i) => ({ type: "public-key", id: signUnb64u(i) })) } : {};
      return signGet(Object.assign({}, pk, use), of !== undefined ? () => signRefusedId.add(rp + "|" + of) : null);
    }
    const noId = rp + "|" + fp;
    const local = !!rawId && typeof rp === "string" && signMine(rp)[fp] === rawId;
    const id = rawId && signDecodable(rawId) && !signRefusedId.has(noId) ? signUnb64u(rawId) : null;
    if (rawId && !signDecodable(rawId) && local) signForget(rp, fp);
    // a refusal of the remembered id (a passkey since removed, or only a cancel) is never asked again by the page: the card
    // says so, and the NEXT TAP asks for no id (the OS's own list). Only the authenticator's own refusal counts (signGet)
    const refused = id && local ? () => signRefusedId.add(noId) : null;
    return signGet(Object.assign({}, pk, id ? { allowCredentials: [{ type: "public-key", id: id }] } : {}), refused);
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
  // where one answer stands, by the attempt id it carried (levain.consent.sign Signer.attempt): {state: "signed" |
  // "refused" | "pending" | "unknown", ...} as the cockpit says it, else {state: "", why} when it said nothing. The cockpit
  // waits a few seconds while the answer is still verifying, and an attempt it never saw it records refused, so that
  // answer arriving later is refused too
  function signAttempt(id, attempt) {
    // a fetch that throws as it is called is a read that failed, never a throw out of here
    return Promise.resolve()
      .then(() => signCall("/cockpit/sign/" + encodeURIComponent(id) + "/attempt", { attempt: attempt }))
      .then((a) => (a.ok && a.body && typeof a.body.state === "string" ? a.body : { state: "", why: signWhy(a) }));
  }
  const signNewAttempt = () => signB64u(crypto.getRandomValues(new Uint8Array(18)));
  // a decided request stays decided only while the listing names it at the version the sheet signed: one the listing no longer
  // has is dropped, and one it lists at another sha256 is a new version of the request, shown live
  function signDecidedPrune(requests) {
    for (const id of [...signDecided.keys()]) {
      const r = requests.find((x) => x && x.id === id);
      const sha = signDecidedSha.get(id) || "";
      if (!r) { signDecided.delete(id); signDecidedSha.delete(id); continue; }
      if (sha && r.sha256 !== sha) { signDecided.delete(id); signDecidedSha.delete(id); signSettledIds.delete(id); signNotes.delete(id); }
    }
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
    got.then((b) => {
      if (b.error && signListed && signListed.got === got) signListed = null;
      if (!b.error) signDecidedPrune(b.requests);
    });
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
  // "nothing was signed" is "unsigned": a 4xx from the challenge or the answer, anything before the answer was sent, or the
  // cockpit saying the answer's attempt was refused. An answer whose reply was lost is asked about by its attempt id
  // (signSettle): "signed" and "refused" are the cockpit's word; "unsure" is left only while it is still verifying the
  // answer, or said nothing, and the request is then held in signPending until a board read settles it.
  function signCeremony(r, fp, rawId, choice, words, say) {
    const base = "/cockpit/sign/" + encodeURIComponent(r.id) + "/";
    // fp null: the signer is named by the answer, so the challenge names none (the bytes signed do not name a key)
    const body = { request_sha256: r.sha256 };
    if (fp !== null) body.signer_fp = fp;
    if (choice !== undefined) body.choice = choice;
    if (words !== undefined) body.write_in = words;
    let sent = false, line;
    const attempt = signNewAttempt();
    const lost = (text) => { say(text, "bad"); return signSettle(r.id, attempt, line, choice, fp, say, text); };
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
        answer.attempt = attempt;
        return signCall(base + "answer", answer).then((a) => {
          if (!a.ok && a.status >= 400 && a.status < 500) { say("not signed: " + signWhy(a), "bad"); return { done: "unsigned" }; }
          if (!a.ok) return lost("not sure this was signed (" + signWhy(a) + ")");
          // the reply's fields where it names them, else what was sent; a part neither names is left out
          const chose = typeof a.body.choice === "string" && a.body.choice ? a.body.choice : choice;
          const by = typeof a.body.signer_fp === "string" && a.body.signer_fp ? a.body.signer_fp : fp;
          signKeep(c.body.rp_id, by, cred);
          const ns = typeof a.body.namespace === "string" && a.body.namespace ? " in " + a.body.namespace : "";
          say("signed" + (chose ? " (" + chose + ")" : "") + (by ? " · by " + by : "") + ns + (line ? ": " + line : ""), "ok");
          return { done: "signed", line: line };
        });
      }, (e) => { say(signDeclined(e, "nothing was signed"), "bad"); return { done: "unsigned" }; });
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) return lost("not sure this was signed (the page failed: " + name + ")");
      say("the page failed (" + name + "); nothing was signed", "bad");
      return { done: "unsigned" };
    });
  }
  // Ask the cockpit where the answer with `attempt` on request `id` stands, after its reply was lost (`said`: what the card
  // said then). Resolves as signCeremony does; "unsure" puts the request in signPending.
  function signSettle(id, attempt, line, choice, fp, say, said) {
    signSettledIds.delete(id);
    say(said + "; asking the cockpit where it stands…", "bad");
    return signAttempt(id, attempt).catch((e) => ({ state: "", why: "the read failed (" + ((e && e.name) || "error") + ")" }))
      .then((st) => {
        if (st.state === "signed") {
          signPending.delete(id);
          signSettledIds.add(id);
          const chose = typeof st.choice === "string" && st.choice ? st.choice : choice;
          const by = typeof st.signer_fp === "string" && st.signer_fp ? st.signer_fp : fp;
          const ns = typeof st.namespace === "string" && st.namespace ? " in " + st.namespace : "";
          say("signed" + (chose ? " (" + chose + ")" : "") + (by ? " · by " + by : "") + ns + (line ? ": " + line : "")
              + " (its reply was lost; the cockpit confirmed it)", "ok");
          return { done: "signed", line: line };
        }
        if (st.state === "refused") {
          signPending.delete(id);
          signSettledIds.add(id);
          const why = typeof st.reason === "string" && st.reason ? st.reason.replace(/[.\s]+$/, "") : "refused";
          say("not signed: " + why + (/nothing was signed/.test(why) ? "" : "; nothing was signed") + ": sign it again", "bad");
          return { done: "unsigned" };
        }
        signPending.set(id, { attempt: attempt, views: new Set(), asking: false });
        say(said + (st.state === "pending" ? "; the cockpit is still verifying it: not confirmed"
          : "; where it stands could not be read (" + (st.why || st.state || "no reply") + "): not confirmed")
          + "; the board reads it again", "bad");
        return { done: "unsure" };
      });
  }
  // The words one pick-list button carries for key `fp`: the provider its enrolment recorded (else "provider not recorded"),
  // when it was enrolled and its number in the authority log, from the listing's `credentials` (display facts the cockpit
  // holds). Never an fp prefix alone: that names nothing a person can match against the OS's own list.
  function signKeyLabel(fp, facts) {
    const f = facts && typeof facts === "object" ? facts : {};
    let prov = typeof f.provider === "string" ? f.provider : "";
    if (!prov || /^provider not/.test(prov)) prov = prov.replace(/ \(.*$/, "") || "provider not recorded";
    const at = typeof f.enrolled_at === "string" ? new Date(f.enrolled_at) : null;
    const pad = (n) => (n < 10 ? "0" : "") + n;
    const when = at && !isNaN(at.getTime())
      ? " · enrolled " + pad(at.getMonth() + 1) + "-" + pad(at.getDate()) + " " + pad(at.getHours()) + ":" + pad(at.getMinutes()) : "";
    return prov + when + (typeof f.seq === "number" ? " (#" + f.seq + ")" : "") + (when || typeof f.seq === "number" ? "" : " · " + fp.slice(0, 18) + "…");
  }
  // Which passkey signs: the one this browser used before, else the only one enrolled; with several remembered, the newest
  // enrolled of them that the authenticator has not refused on this page. With several and no record the person picks (buttons put into `host`, the phone's pick list), unless
  // `mode` is "late": a plain sign request, whose signer the cockpit's reply names, never draws a pick. Late resolves
  // {fp: null, rawIds}: the one remembered id (no OS chooser), else every enrolled id when all are known, else none (the OS's
  // own list, which holds every levain passkey this device has). The credential id is this browser's own record, else
  // the one the listing's `credentials` names (display facts the cockpit holds, so a fresh browser asks for that passkey
  // at once), else none (the OS's own list). Resolves {fp, rawId}, or null when the pick was dismissed
  // by its dismiss button; rejects (an AbortError) when a redraw took `host` off the page with the pick still open, so the
  // caller's `signBusy` is released on every path. The pick holds `signBusy` like a ceremony.
  function signWho(listing, host, say, mode) {
    const signers = Array.isArray(listing.signers) ? listing.signers : [];
    const rp = (listing.origin || "").replace(/^https?:\/\//, "").replace(/:\d+$/, "");
    const local = signMine(rp);
    // an entry that cannot be an id (empty, null, not text) names no passkey: it is dropped before it decides who signs
    for (const k of Object.keys(local)) if (!signDecodable(local[k])) { signForget(rp, k); delete local[k]; }
    const held = listing.credentials && typeof listing.credentials === "object" ? listing.credentials : {};
    const idOf = (fp) => (Object.prototype.hasOwnProperty.call(local, fp) ? local[fp]
      : (Object.prototype.hasOwnProperty.call(held, fp) && held[fp] && typeof held[fp].credential_id === "string"
        ? held[fp].credential_id : null));
    const remembered = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    // a remembered key whose id the authenticator refused on this page is passed over (L3 picker r1): asking it again would
    // fail again, and a bound challenge must never pair it with the OS's whole list, which signs with a key it is not bound to
    const known = remembered.filter((fp) => !signRefusedId.has(rp + "|" + fp));
    const seqOf = (fp) => (held[fp] && typeof held[fp].seq === "number" ? held[fp].seq : -1);
    const newest = (fps) => fps.reduce((best, fp) => (seqOf(fp) >= seqOf(best) ? fp : best), fps[0]);
    if (mode === "late") {
      if (known.length) { const ids = [local[newest(known)]]; return Promise.resolve({ fp: null, rawIds: ids }); }
      const all = signers.map(idOf);
      // every remembered key refused: the OS's own list (whichever levain passkey this device holds signs)
      const ok = !remembered.length && signers.length && all.every((i) => typeof i === "string");
      return Promise.resolve({ fp: null, rawIds: ok ? all : [] });
    }
    if (known.length) { const fp = newest(known); return Promise.resolve({ fp: fp, rawId: local[fp] }); }
    if (signers.length === 1) return Promise.resolve({ fp: signers[0], rawId: idOf(signers[0]) });
    const choices = signers;
    say("more than one passkey may sign this; pick one this device holds (a synced passkey such as iCloud Keychain is on all"
      + " your devices; any one on this device works):");
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
        const p = el("button", "undo-btn sign-pick", signKeyLabel(fp, held[fp]));
        p.type = "button";
        p.title = fp;
        // a pick is his word for that key: a refusal recorded for it is over, so it is asked for by its id (the challenge is
        // bound to it, so the OS's whole list would offer keys that cannot answer it)
        p.addEventListener("click", () => { end(); signRefusedId.delete(rp + "|" + fp); resolve({ fp: fp, rawId: idOf(fp) }); });
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
  // the card card_format.js reads from sign request `r`, or null (not a needs-phill message it reads exactly, the parser
  // not loaded, or one that throws): null leaves the question's own lines as they are
  function signCardOf(r) {
    const F = window.LevainCardFormat;
    if (!F || typeof F.parseMessage !== "function" || typeof F.drawHead !== "function") return null;
    try { return F.parseMessage(r.message, r) || (typeof F.looseCard === "function" ? F.looseCard(r.message, r) : null); } catch (e) { return null; }
  }
  // the card's part for request `id`: a note line, then the request's own buttons, filled once the listing answers.
  // `head`: the focus view's question lines a needs-phill card replaces (signCardOf), else none
  function signFacet(id, head, full) {
    const wrap = el("div", "sign-facet");
    wrap.__mics = [];       // the write-in's mic, ended with the card by its slot's next draw
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
      // an answer whose reply was lost and that the cockpit had not settled: the card is drawn frozen and asks about it
      // (settleHere, below) before anything is offered
      const unsettled = !!r && signPending.has(id);
      if (!r && signPending.has(id)) {
        // an unsettled answer whose request left the listing is never dropped: this facet is told every ask about it (on
        // this read and on every board read, signRecheck) until the cockpit says signed or refused
        const p = signPending.get(id);
        const gone = (st) => {
          if (!wrap.isConnected) { p.views.delete(gone); return; }
          if (st.state === "signed") {
            signWriteIns.delete(id);
            return show("signed" + (typeof st.choice === "string" && st.choice ? " (" + st.choice + ")" : "")
                        + " (its reply was lost; the cockpit confirmed it)", "ok");
          }
          if (st.state === "refused") {
            return show("this sign request is no longer open, and the last answer here was not signed", "bad");
          }
          show("this sign request is no longer open; the last answer here is not confirmed, and is asked about again "
               + "on the next read", "bad");
        };
        p.views.add(gone);
        show("this sign request is no longer open; asking the cockpit where the last answer here stands", "bad");
        signAskPending(id);
        return;
      }
      if (!r) {
        // answered here: its signed line stays until the row itself leaves; else it was answered, denied or expired elsewhere
        return show(kept && (kept[1] === "ok" || signSettledIds.has(id)) ? kept[0] : "this sign request is no longer open; the card updates on the next read", kept && kept[1]);
      }
      if (kept && kept[2] !== (r.sha256 || "") && !signDecided.has(id)) signNotes.delete(id);
      if (r.refused) return show("not signable: " + r.refused, "bad");
      // the focus view's needs-phill card (card_format.js, the sheet's rule and builders): drawn in place of the question's
      // title, blocks and REC lines (`head`), the signed text whole one tap away above the buttons, Detail after them
      const nc = head && head.length && head[0].parentNode ? signCardOf(r) : null;
      if (nc && full) nc.stopped_full = full;     // the stop text's lines; drawDetail uses them only when they are the signed words
      if (nc) {
        const box = window.LevainCardFormat.drawHead(nc, el("pre", "consent-display", r.message));
        box.classList.add("needs-card-head");
        head[0].before(box);
        for (const n of head) n.remove();
      }
      const say = (text, kind) => { signNotes.set(id, [text, kind || "", r.sha256 || ""]); show(text, kind); };
      const signers = Array.isArray(r.signers) ? r.signers : [];
      const k = signNotes.get(id);
      if (k) show(k[0], k[1]);
      else if (!signers.length) {
        show(listing.note || "no passkey is enrolled for this name", "bad");
        // the page that makes a passkey for this name on this device and prints the laptop commands that enrol it
        const mk = el("a", "sign-enrol-link", "Make a passkey on this device");
        mk.href = "/enrol";
        note.appendChild(mk);
      } else show("");
      const acts = el("div", "sign-actions");
      const buttons = [];
      let sheetBtn = null;    // "open the sheet": locked with the card's own controls, whether or not a key may sign
      const lock = (on) => { for (const b of buttons) b.disabled = on || !signers.length; if (sheetBtn) sheetBtn.disabled = on; for (const m of wrap.__mics) m.lock(on || !signers.length); };
      let freeze = null;      // the write-in's frozen state while its ceremony runs (set below when it offers one)
      // the frozen card's view of its unsettled answer (signPending): "signed" says so and the board reads again;
      // "refused" gives the card back; anything else keeps it frozen until the next board read asks again (signRecheck)
      let settleView = null;
      const settleHere = (ask) => {     // ask: false only right after the ceremony's own ask (the next board read asks)
        const p = signPending.get(id);
        if (!p) return;
        if (!settleView) {
          settleView = (st) => {
            if (!wrap.isConnected) { p.views.delete(settleView); return; }   // a newer card shows it now
            settleShow(st);
          };
        }
        p.views.add(settleView);
        if (ask !== false) signAskPending(id);
      };
      const settleShow = (st) => {
        if (signDecided.has(id)) return;     // the sheet signed it: no later word about an older attempt gives the card back
        if (st.state === "signed") {
          signWriteIns.delete(id);
          say("signed" + (typeof st.choice === "string" && st.choice ? " (" + st.choice + ")" : "")
              + " (its reply was lost; the cockpit confirmed it)", "ok");
          rereadBoard();
          return;
        }
        if (st.state === "refused") {
          const why = typeof st.reason === "string" && st.reason ? st.reason.replace(/[.\s]+$/, "") : "refused";
          say("not signed: " + why + (/nothing was signed/.test(why) ? "" : "; nothing was signed") + ": sign it again", "bad");
          lock(false);
          if (freeze) freeze.release();
          for (const x of acts.querySelectorAll("input")) x.readOnly = x.disabled = false;
          return;
        }
        say((st.state === "pending" ? "the cockpit is still verifying the last answer"
          : "where the last answer stands could not be read" + (st.why ? " (" + st.why + ")" : ""))
          + ": not confirmed; the card asks again on the next board read", "bad");
      };
      const run = (choice, words) => {
        if (signBusy) return say(SIGN_BUSY, "bad");   // the lease holds a second tap, never a button drawn disabled
        // a ceremony under a live mic ends the mic first, at once (off): a Dictate tail must not land in the write-in after the tap,
        // so the box shown stays the words a ceremony signs; an idle mic keeps its control for a retry
        for (const m of wrap.__mics) if (m.busy()) m.off();
        signBusy++;
        lock(true);
        if (!signInFlight.has(id)) signInFlight.set(id, new Set());
        let done = "unsigned";
        let rearm = true;     // false: the card stays frozen (signed, or "not sure" until a read finds the version open)
        signWho(Object.assign({}, listing, { signers: signers }), acts, say, "late")
          .then((who) => {
            if (!who) return { done: "unsigned" };
            // a ceremony that throws before it returns has posted nothing: the answer is only ever sent from inside the
            // promise it returns
            try { return signCeremony(r, who.fp, who.rawIds || who.rawId, choice, words, say); }
            catch (e) { say("the page failed (" + ((e && e.name) || "error") + "); nothing was signed", "bad"); return { done: "unsigned" }; }
          }, () => { say(SIGN_PICK_CLOSED, "bad"); return { done: "unsigned" }; })
          // a ceremony whose own catch threw may have sent the answer, with no attempt to ask about: "not sure", and the
          // card stays frozen
          .catch((e) => {
            say("not sure this was signed (the page failed: " + ((e && e.name) || "error") + "): check the list", "bad");
            return { done: "unsure" };
          })
          .then((out) => {
            done = out.done;
            // only a signature spends the words. "Not sure" (the cockpit has not settled the answer's attempt, or the
            // ceremony's own catch threw) keeps them and every button frozen; an unsettled attempt is asked about again
            // on every board read (settleHere)
            if (out.done === "signed") { signWriteIns.delete(id); rearm = false; return undefined; }
            if (out.done === "unsigned") { if (freeze) freeze.release(); return undefined; }
            rearm = false;
            settleHere(false);
            return undefined;
          })
          .catch(() => { /* the ceremony said what happened; the lease is released below */ })
          .finally(() => {
            signBusy--;
            for (const p of acts.querySelectorAll(".sign-pick")) p.remove();
            lock(!rearm);
            // a definite "nothing was signed" gives every card drawn meanwhile back; an unsure answer is signPending's now
            // (it freezes and releases a card drawn after it); a signed one is not given back either, and its entry is
            // dropped here so a request filed again under the same id is not drawn frozen by an ended ceremony
            if (rearm) { const later = signInFlight.get(id); signInFlight.delete(id); if (later) for (const f of later) f(); }
            else signInFlight.delete(id);
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
        const rec = o && o.key === (nc ? nc.recommended : r.recommended);      // a card's rec is the signed one
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
        const b = el("button", "undo-btn question-option sign-option sign-words", "(" + String(wi.key) + ") sign my words");
        b.type = "button";
        // the streaming Mic and on-device Dictate on the write-in (board row 140); a tap that signs the words waits for the mic to end
        const wmic = window.LevainMic && typeof window.LevainMic.attach === "function" ? window.LevainMic.attach(box, { cls: "undo-btn", note: (t) => say(t) }) : null;
        if (wmic) wrap.__mics.push(wmic);
        // the words being signed are frozen from the tap until the ceremony ends; only a definite "nothing was signed"
        // unfreezes them, so a retry is a new tap over the same words
        freeze = { release: () => { box.readOnly = box.disabled = false; } };
        if (signInFlight.has(id)) box.readOnly = box.disabled = true;
        b.addEventListener("click", () => {
          if (signBusy) return say(SIGN_BUSY, "bad");
          if (wmic && wmic.busy()) return say("Stop the mic first: your words are signed as they stand when it has ended.", "bad");
          const w = box.value.trim();
          if (!w) return say("type your answer first", "bad");
          box.value = w;
          signWriteIns.set(id, { sha256: r.sha256 || "", words: w });
          box.readOnly = box.disabled = true;
          run(String(wi.key), w);
        });
        buttons.push(b);
        if (wmic && wmic.buttons.length) {      // the box and its mic icons share one row
          const inRow = el("div", "voice-in-row");
          inRow.append(box, ...wmic.buttons);
          acts.append(inRow, b);
        } else acts.append(box, b);
      }
      lock(false);
      sheetBtn = signSheetButton(r, say);
      acts.appendChild(sheetBtn);
      if (signInFlight.has(id)) {
        lock(true);
        signInFlight.get(id).add(() => { lock(false); if (freeze) freeze.release(); });
      }
      wrap.appendChild(acts);
      // a standing way to make a passkey for this name on THIS device: the error link above shows only while nobody has one
      const mkStanding = el("a", "sign-enrol-link sign-enrol-standing", "Make a passkey on this device");
      mkStanding.href = "/enrol";
      wrap.appendChild(mkStanding);
      if (nc) wrap.appendChild(window.LevainCardFormat.drawDetail(nc, options));
      if (unsettled) {          // the last answer may still sign: nothing here is offered until the cockpit settles it
        lock(true);
        for (const x of acts.querySelectorAll("input")) x.readOnly = x.disabled = true;
        settleHere();
      }
      if (signDecided.has(id)) signCardDead(wrap, signDecided.get(id));     // decided before this card's controls existed
      if (options.length) {
        // the protocol lines are the signer's reference, not the reader's: one tap away, closed, the text unchanged
        const lines = el("div", "sign-lines ext-meta");
        lines.appendChild(el("div", "", "an option signs the request's message with its line appended:"));
        for (const o of options) lines.appendChild(el("div", "sign-line", String(o.line)));
        if (wi) lines.appendChild(el("div", "sign-line", String(wi.line) + "<your words>"));
        const disc = el("details", "sign-lines-disc");
        disc.appendChild(el("summary", "", "What each option signs"));
        disc.appendChild(lines);
        wrap.appendChild(disc);
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
  let signSheetOpen = null;       // {kind, id}: what the open sheet was opened for, given by the caller that opened it; only a plain sign sheet's "signed" message decides a card, and only for its own id
  const SIGN_SIGNED_GRACE_MS = 30000;   // a plain sign sheet closed this recently may still have its "signed" message arrive
  let signSheetRecent = [];       // {id, win, until}: the plain sign sheets closed within that grace, with the window each one's frame had
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
    return openSheet("/sign/" + encodeURIComponent(id), version, choice, "sign request", undefined, { kind: "sign", id: id });
  }
  // OS-2: the same sheet over a consent request (/sign/consent/<id>), answered or ruled there with a passkey
  function openConsentSheet(id, version, choice) {
    if (typeof id !== "string" || !/^[A-Za-z0-9._:-]{1,160}$/.test(id)) return null;
    return openSheet("/sign/consent/" + encodeURIComponent(id), version, choice, "consent request", undefined, { kind: "consent", id: id });
  }
  // `heading`: the modal's title when it should not be `what` (which names the frame for assistive tech)
  function openSheet(path, version, choice, what, heading, ident) {
    closeSignSheet();
    if (signBusy) return null;
    signSheetOpen = ident && typeof ident.kind === "string" ? { kind: ident.kind, id: String(ident.id) } : null;
    signBusy++;
    signSheetLease = true;
    try {
      return buildSignSheet(path, version, choice, what, heading);
    } catch (e) {
      closeSignSheet();   // a build that threw gives back the lease and whatever it had made inert
      return null;
    }
  }
  // THE TOP LAYER (row 1704 fix 4): the veil is a <dialog> opened with showModal(), so it sits in the browser's top layer
  // and nothing of this page (a toast, a bar, a modal appended later, whatever its z-index) can paint over the frame. That
  // matters to the frame's long-input gate: it hit-tests only its own document, and a node of THIS page over the frame is
  // invisible to it (measured in Chrome: an inert cover is invisible to this page's own elementFromPoint too, so a hit test
  // here could not see the toasts the inert set makes). The inert set and its observer stay, belt and braces. The gate in
  // the frame counts nothing unless its frame is inside the one :modal dialog of this page (card_format.js, screenOf), so
  // a browser without showModal gets the old div veil and a long input there can be denied, never allowed.
  // A dialog closes itself on a close request (Escape) and Chrome lets that be cancelled only after a user activation:
  // measured, a second Escape (or a first with no activation) fires a `cancel` that cannot be cancelled and closes it. So
  // the Escape keydown is prevented (that stops the close request: measured), a `cancel` is prevented, and a close this
  // page did not make reopens the dialog at once; each goes through askCloseSignSheet (a ceremony in flight stays).
  function buildSignSheet(path, version, choice, what, heading) {
    let frag = typeof version === "string" && /^[0-9a-f]{16}/.test(version) ? "v=" + version.slice(0, 16) : "";
    if (frag && typeof choice === "string" && /^[a-z0-9]{1,8}$/.test(choice)) frag += "&choice=" + choice;
    const top = typeof HTMLDialogElement === "function" && typeof HTMLDialogElement.prototype.showModal === "function";
    const veil = el(top ? "dialog" : "div", "modal-overlay");
    veil.id = SIGN_SHEET_ID;
    veil.setAttribute("role", "dialog");
    veil.setAttribute("aria-modal", "true");
    veil.setAttribute("aria-label", what);
    const box = el("div", "modal");
    const head = el("div", "modal-head");
    head.appendChild(el("h2", "", heading || what.charAt(0).toUpperCase() + what.slice(1)));
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
    // the envelope form is the modal's whole body: the modal takes the room it has and the frame fills it
    if (path === "/sign/drop") { box.classList.add("modal-sheet"); frame.style.height = "auto"; }
    box.append(head, frame);
    veil.appendChild(box);
    veil.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); askCloseSignSheet(); } });
    document.body.appendChild(veil);
    if (top) {
      veil.addEventListener("cancel", (ev) => { ev.preventDefault(); askCloseSignSheet(); });
      veil.addEventListener("close", () => {
        if (veil.dataset.closing === "1" || !veil.isConnected || document.getElementById(SIGN_SHEET_ID) !== veil) return;
        try { veil.showModal(); } catch (e) { /* the ask below closes it within SIGN_CLOSE_ACK_MS */ }
        askCloseSignSheet();
      });
      // one that cannot be made modal is shown in place, as the div veil was: its frame's gate then never opens Allow
      try { veil.showModal(); } catch (e) { veil.setAttribute("open", ""); }
    }
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
    const closing = document.getElementById(SIGN_SHEET_ID + "-frame");
    const now = Date.now();
    signSheetRecent = signSheetRecent.filter((e) => e.until > now);
    if (signSheetOpen && signSheetOpen.kind === "sign" && closing && closing.contentWindow) {
      signSheetRecent.push({ id: signSheetOpen.id, win: closing.contentWindow, until: now + SIGN_SIGNED_GRACE_MS });
    }
    if (signSheetWatch) { signSheetWatch.disconnect(); signSheetWatch = null; }
    for (const n of signSheetInerted) n.inert = false;
    signSheetInerted = [];
    dropPrefillClear();      // words handed to a drop sheet that never read them are not left for the next one
    dropHandedReset();       // a handoff outlives its sheet never
    if (veil) {
      veil.dataset.closing = "1";      // its `close` event (queued) is this page's own close, never one to undo
      if (veil.open && typeof veil.close === "function") veil.close();
      veil.remove();
    }
    if (!document.getElementById("levain-modal")) document.body.classList.remove("modal-open");
    if (signSheetLease) { signSheetLease = false; signBusy--; }
    signSheetOpen = null;
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
  // A card of a signed request: its note says what was signed and every control on it is dead, whatever draws it
  function signCardDead(wrap, text) {
    const note = wrap.querySelector(".sign-note");
    if (note) { note.textContent = text; note.className = "sign-note ext-meta sign-ok"; }
    for (const c of wrap.querySelectorAll("button, input, textarea, select, [contenteditable]")) {
      c.disabled = true;
      if ("readOnly" in c) c.readOnly = true;
      if (c.isContentEditable) c.setAttribute("contenteditable", "false");
    }
  }
  // The sheet signed request `id`: the card behind it is decided now, not at the next redraw (the strip keeps a card whose item
  // did not change, so the board read alone never reaches it). The decision is kept by id, so a card whose controls are built
  // after this message (a facet still reading, a redraw on the cached listing) comes out decided too.
  // the server's option key grammar (a sign request's option keys are [a-z0-9]{1,8}) plus the key a write-in signs under, "*"
  const SIGN_CHOICE_RE = /^(?:[a-z0-9]{1,8}|\*)$/;
  function signSheetSigned(id, choice, sha) {
    if (typeof id !== "string" || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) return;
    const text = "signed" + (typeof choice === "string" && SIGN_CHOICE_RE.test(choice) ? " (" + choice + ")" : "");
    signDecidedSha.set(id, typeof sha === "string" && /^[0-9a-f]{16,64}$/.test(sha) ? sha : "");
    signNotes.set(id, [text, "ok", ""]);
    signSettledIds.add(id);
    signDecided.set(id, text);
    signPending.delete(id);
    signWriteIns.delete(id);
    signListed = null;
    for (const wrap of document.querySelectorAll(".sign-facet[data-sign-request]")) {
      if (wrap.dataset.signRequest === id) signCardDead(wrap, text);
    }
  }
  window.addEventListener("message", (ev) => {
    if (ev.origin !== window.location.origin) return;
    const frame = document.getElementById(SIGN_SHEET_ID + "-frame");
    const what = ev.data && typeof ev.data === "object" ? ev.data.levainSheet : null;
    if (!frame || ev.source !== frame.contentWindow) {
      // a plain sign sheet closed a moment ago, its signature landing after the close: taken only from that sheet's own frame
      // window (a frame already gone sends nothing that can be told from any other; the board read settles that card)
      if (what === "signed" && ev.source) {
        const now = Date.now();
        const late = signSheetRecent.find((e) => e.win === ev.source && e.until > now && e.id === ev.data.id);
        if (late) { signSheetSigned(late.id, ev.data.choice, ev.data.sha256); rereadBoard(); }
      }
      return;
    }
    if (what === "ack") { clearTimeout(signSheetAsk); signSheetAsk = null; }
    else if (what === "close") { closeSignSheet(); dispatchStale(); }
    else if (what === "signed" || what === "reload") {
      // the sheet's line stays on it until it is closed; the board under it reads again, and the dispatch jobs with it
      if (what === "signed" && signSheetOpen && signSheetOpen.kind === "sign" && ev.data.id === signSheetOpen.id) signSheetSigned(ev.data.id, ev.data.choice, ev.data.sha256);
      dispatchStale();
      rereadBoard();
      // the server accepted the drop the sheet filed: the box lets go of the words only if it holds exactly those words
      if (ev.data.dropDelivered === true) dropHandedClear(ev.data.words);
      dropReload();      // a drop the sheet filed or could not confirm (either may have made the store): the drop card reads again, not from its cache
    }
  });
  // One drag-down-to-dismiss for every cockpit sheet (touch). It is delegated and attribute-driven, so a sheet needs no wiring of
  // its own: any [role="dialog"] that is a `.modal-overlay` (the panel is its `.modal` child) or carries `data-sheet-dismiss`
  // (the panel is the dialog itself; the value, when it is a media query, limits the gesture to it, for a popover that is a
  // sheet only on a phone) takes it. A drag that starts on the header (`.modal-head`, `.sheet-grabber`), or anywhere on the
  // sheet while nothing under the finger is scrolled down, moves the panel; past a distance or a speed it DISMISSES, a short one
  // springs back. Dismissing is the sheet's own close path, never a second one: a `levain:sheet-dismiss` event the sheet may
  // handle (preventDefault says it did), else a click on its `.modal-close` X, so a guard on the X (the sign sheet's ask) holds.
  // A sheet still on screen after a dismiss (the guard kept it) springs back. The X and Escape are untouched.
  const SHEET_FAR = 96, SHEET_FRAC = 0.25, SHEET_SPEED = 0.6, SHEET_SLOP = 8;
  function sheetTarget(t) {
    const dlg = t && t.closest ? t.closest('[role="dialog"]') : null;
    if (!dlg) return null;
    const own = dlg.getAttribute("data-sheet-dismiss");
    if (own === null && !dlg.classList.contains("modal-overlay")) return null;
    if (own && own !== "true" && own !== "" && window.matchMedia && !window.matchMedia(own).matches) return null;
    const panel = own !== null ? dlg : (dlg.querySelector(":scope > .modal") || dlg);
    return { dlg, panel };
  }
  // a scrolled-down scroller between the finger and the panel owns the gesture (the body scrolls back up first)
  function sheetScrolledDown(node, panel) {
    for (let n = node; n && n.nodeType === 1; n = n.parentElement) {
      if (n.scrollHeight > n.clientHeight + 1 && n.scrollTop > 0) {
        const oy = getComputedStyle(n).overflowY;
        if (oy === "auto" || oy === "scroll") return true;
      }
      if (n === panel) break;
    }
    return false;
  }
  function sheetDismiss(dlg) {
    const ev = new CustomEvent("levain:sheet-dismiss", { bubbles: false, cancelable: true });
    dlg.dispatchEvent(ev);
    if (ev.defaultPrevented) return;
    const x = dlg.querySelector(".modal-close");
    if (x) x.click();
  }
  function sheetDragInit() {
    if (typeof document === "undefined" || typeof document.addEventListener !== "function" || typeof CustomEvent === "undefined") return;
    let g = null;
    const reduce = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const settle = (panel) => {
      panel.style.transition = reduce() ? "none" : "transform 160ms ease-out";
      panel.style.transform = "";
      if (!reduce()) setTimeout(() => { panel.style.transition = ""; }, 200);
      else panel.style.transition = "";
    };
    document.addEventListener("touchstart", (e) => {
      g = null;
      if (e.touches.length !== 1) return;
      const tgt = sheetTarget(e.target);
      if (!tgt) return;
      const t0 = e.target;
      if (t0.closest && t0.closest("input, textarea, select, [contenteditable]")) return;
      const handle = !!(t0.closest && t0.closest(".modal-head, .sheet-grabber"));
      if (!handle && sheetScrolledDown(t0, tgt.panel)) return;
      const t = e.touches[0];
      g = { dlg: tgt.dlg, panel: tgt.panel, x: t.clientX, y: t.clientY, at: Date.now(), dy: 0, live: false, v: 0, ly: t.clientY, lt: Date.now() };
    }, { passive: true });
    document.addEventListener("touchmove", (e) => {
      if (!g || e.touches.length !== 1) return;
      const t = e.touches[0], dy = t.clientY - g.y, dx = t.clientX - g.x;
      if (!g.live) {
        if (Math.abs(dy) < SHEET_SLOP && Math.abs(dx) < SHEET_SLOP) return;
        if (dy <= 0 || Math.abs(dx) > Math.abs(dy)) { g = null; return; }   // up, or sideways: the page's own gesture
        g.live = true;
        g.panel.style.transition = "none";
        g.panel.style.animation = "none";    // the modal's rise animation holds its end state and would override the drag
      }
      const now = Date.now();
      if (now > g.lt) g.v = (t.clientY - g.ly) / (now - g.lt);
      g.ly = t.clientY; g.lt = now; g.dy = Math.max(0, dy);
      g.panel.style.transform = "translateY(" + g.dy + "px)";
      if (e.cancelable) e.preventDefault();
    }, { passive: false });
    const end = (cancel) => (e) => {
      const d = g;
      g = null;
      if (!d || !d.live) return;
      const far = d.dy > Math.max(SHEET_FAR, d.panel.getBoundingClientRect().height * SHEET_FRAC);
      if (!cancel && (far || (d.v > SHEET_SPEED && d.dy > 24))) {
        sheetDismiss(d.dlg);
        if (d.dlg.hidden) { d.panel.style.transition = ""; d.panel.style.transform = ""; d.panel.style.animation = ""; return; }   // closed by hiding: it reopens in place, with its entrance animation back (a spring-back keeps animation:none: clearing it on an open sheet would replay the rise)
        // a sheet that is still up (its guard kept it) is given back where it was
        setTimeout(() => {
          if (!d.panel.isConnected) return;
          if (d.dlg.hidden) { d.panel.style.transition = ""; d.panel.style.transform = ""; d.panel.style.animation = ""; } else settle(d.panel);   // a hidden sheet reopens in place
        }, 250);
      } else settle(d.panel);
    };
    document.addEventListener("touchend", end(false), { passive: true });
    document.addEventListener("touchcancel", end(true), { passive: true });
  }
  sheetDragInit();
  window.LevainSheetDismiss = { target: sheetTarget };

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
      try { who = await signWho(Object.assign({}, listing, { signers: signers }), host, tell, "late"); }
      catch (e) { tell(SIGN_PICK_CLOSED, "bad"); return { ok: false, message: SIGN_PICK_CLOSED + kept }; }
      if (!who) return { ok: false, message: last + kept };
      const out = await signCeremony(r, who.fp, who.rawIds || who.rawId, undefined, undefined, tell);
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
    const consents = [], questions = [], answered = [];
    for (const ln of Array.isArray(lines) ? lines : []) {
      if (!ln || typeof ln !== "object") continue;
      if (ln.consent && ln.consent.state === "pending") consents.push({ kind: "consent", c: ln.consent });
      // a question the desk alone owes (section desk, not carried over to him) is the Decisions card's "Desk owes" tab, not his strip
      else if (ln.question && ln.question.state === "open" && needsLaterDay(ln.question)) continue;     // dated past today: a LATER line (needsLaterItems), not a waiting item
      else if (ln.question && ln.question.state === "open") { if (!(ln.question.section === "desk" && !ln.question.carried)) questions.push({ kind: "question", q: ln.question }); }
      else if (ln.question && ln.question.state === "answered" && decidedRecently(ln.question.decidedAge, ln.question.decidedAt)) {
        answered.push({ kind: "question", q: ln.question, answered: true });
      }
    }
    const due = (it) => { const ms = Date.parse(it.c.expiresAt); return Number.isFinite(ms) ? ms : Infinity; };
    consents.sort((a, b) => due(a) - due(b));   // Array.prototype.sort is stable: equal expiries keep the kernel's order
    // the strip keeps the newest few: a morning of answers is not a second queue. A question with no readable decision age
    // (an empty decidedAge) is not in the strip by intent: the window cannot be told for it.
    const keep = newestAnswered(answered.map((it) => ({ question: it.q })), NEEDS_ANSWERED_MAX);
    const byQ = new Map(answered.map((it) => [it.q, it]));
    return questions.concat(consents, Array.from(keep).map((q) => byQ.get(q)));
  }
  // The day a question is LATER until: its question_later facet, else "" (due). The server alone judges the day, in the install's
  // zone, per request (band_view.later_of), and the panel's etag covers that zone's date, so a view refreshes when midnight passes
  // there. The page never re-judges it with its own clock: a phone in another zone would disagree with flow (L3 r2).
  function needsLaterDay(q) {
    const d = q && typeof q.later === "string" ? q.later : "";
    return /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : "";
  }
  // the open questions dated past today (row 131's one predicate, served as the question_later facet): drawn as one collapsed LATER
  // line each at the end of the strip, never counted, no answer controls. Soonest day first, then band number. `scope` "desk" is the
  // Desk owes tab's own (section desk, carried or not); anything else is Needs you's (every row but one the desk alone owes).
  function needsLaterItems(lines, scope) {
    const out = [];
    for (const ln of Array.isArray(lines) ? lines : []) {
      const q = ln && ln.question;
      const day = q && q.state === "open" ? needsLaterDay(q) : "";
      if (!day) continue;
      if (scope === "desk" ? q.section === "desk" : !(q.section === "desk" && !q.carried)) out.push({ kind: "question", q: q, later: day });
    }
    const bk = (it) => { const b = cardBandNum(it.q.n); return Number(b === undefined ? it.q.n : b); };
    return out.sort((a, b) => (a.later < b.later ? -1 : a.later > b.later ? 1 : bk(a) - bk(b)));
  }
  // the headline is cut on a word boundary (never mid-token); the page's 2-line clamp does the rest
  function needsLaterCut(t) {
    t = String(t);
    if (t.length <= 160) return t;
    const c = t.slice(0, 160), i = c.lastIndexOf(" ");
    return (i > 0 ? c.slice(0, i) : c).replace(/[\s,;:]+$/, "") + "\u2026";   // the last space anywhere before the limit; a headline with none keeps its one token to the limit
  }
  function needsLaterLine(it) {
    const row = el("div", "needs-later-line band-later");
    row.dataset.needsLater = String(it.q.n);
    row.dataset.questionN = String(it.q.n);
    row.dataset.laterDay = it.later;
    const bn = cardBandNum(it.q.n);
    const tag = el("b", "band-num needs-later-band", bn === undefined ? "" : String(bn));
    tag.hidden = bn === undefined;
    const words = el("span", "ext-meta needs-later-text", it.q.laterWords || it.q.later);
    const title = el("span", "ext-meta needs-later-title", "");
    row.append(tag, words, title);        // no per-row LATER word: the section's one header says it (a narrow screen wrapped it "LAT / ER")
    questionBody(it.q, row).then((b) => {
      if (!b || b.error) return;
      const first = needsFirstSentence(b.what);
      if (first) title.textContent = needsLaterCut(first.replace(/^\s*DESK OWES:\s*/i, "") || first);
    });
    return row;
  }
  // an item the strip shows only so the answer given stays visible for a while: it waits on nothing and is never counted
  const needsWaiting = (items) => items.filter((it) => !it.answered);
  const NEEDS_ANSWERED_MINUTES = 30, NEEDS_ANSWERED_MAX = 3;
  // the page's clock as the age timer reads it (the server's, so a skewed browser cannot misreport the window)
  function needsNow() {
    const K = typeof window !== "undefined" ? window.LevainCockpitKinds : null;
    return K && typeof K.serverNow === "function" ? K.serverNow() : Date.now();
  }
  // How long the answer still stays on the strip, in ms; null when the stamp cannot be read.
  function decidedLeftMs(at) {
    const ms = typeof at === "string" ? Date.parse(at) : NaN;
    return Number.isFinite(ms) ? ms + NEEDS_ANSWERED_MINUTES * 60000 - needsNow() : null;
  }
  // The raw stamp decides when it is readable, so the window ends on an idle page; the row's relative label ("40s ago",
  // "12m ago") is the fallback for a line with no stamp, and only seconds and minutes under the window qualify there.
  function decidedRecently(label, at) {
    const left = decidedLeftMs(at);
    if (left !== null) return left > 0;
    const m = /^(\d+)([sm]) ago$/.exec(typeof label === "string" ? label : "");
    if (!m) return false;
    return m[2] === "s" ? Number(m[1]) < 60 : Number(m[1]) < NEEDS_ANSWERED_MINUTES;
  }
  // whether this page holds a write token: an answered card's body is read with it, so the token arriving is a change of state
  function hasWriteToken() {
    const t = typeof window !== "undefined" ? window.LevainCockpitTransport : null;
    const h = t && typeof t.headers === "function" ? t.headers() : {};
    return !!(h && h["X-Levain-Write-Token"]);
  }
  const needsKey = (it) => (it.kind === "consent" ? "c:" + it.c.id + "|" + it.c.state + "|" + it.c.expiresAt : "q:" + it.q.n + "|" + it.q.state + (it.q.state === "answered" ? "|" + (hasWriteToken() ? "t" : "-") : ""));
  // the name a glance tile gives the top item, and the focus view's dialog title the same (one helper so they cannot drift).
  // A consent is its tool and who asked. A question is a short handle from its own words and who asked; the wire row carries no words
  // (they are fetched with the page's token, questionBody), so until they have been read the name is "<agent> · question" and the
  // band number when the last band read gave one. The question's long number (flow's card number) is the answer key, never a name:
  // it stays in q.n, which the answer path sends, and is not drawn here.
  // `whole`: the focus view's dialog title on a desk-wide page takes the question's first line whole instead of its few-word handle
  const wideDialog = () => !!(window.matchMedia && window.matchMedia("(min-width: 761px)").matches);
  function needsName(it, whole) {
    if (it.kind === "consent") return (it.c.tool || "request") + " · " + (it.c.agent || "agent");
    // the handle is the question's own words: it is shown only to a page that holds the token the words were read with
    const k = questionKeyOf(it.q);
    const handle = hasWriteToken() ? (whole && wideDialog() && questionLines.get(k)) || questionHandles.get(k) : "";
    if (handle) return handle + " · " + (it.q.agent || "agent");
    const band = bandNumOf.get(Number(it.q.n));
    return (it.q.agent || "agent") + " · question" + (band === undefined ? "" : " · band " + band);
  }
  // A question's handle: the first line of its words, cut to its first few words on a word boundary with an ellipsis (a long
  // unbroken token is cut by characters). "" when there are no words to take.
  function questionHandle(what) {
    const line = String(what || "").split(/\r?\n/).map((l) => l.trim()).find(Boolean) || "";
    const words = line.split(/\s+/).filter(Boolean);
    if (!words.length) return "";
    let out = words.slice(0, QUESTION_HANDLE_WORDS).join(" ");
    let cut = words.length > QUESTION_HANDLE_WORDS;
    if (out.length > QUESTION_HANDLE_CHARS) { out = out.slice(0, QUESTION_HANDLE_CHARS); cut = true; }
    if (!cut) return out;
    const body = out.replace(/[\s,;:.\-–—(]+$/, "");
    return body ? body + "…" : "";     // nothing but trailing marks is no handle: the agent-only name stands
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
      title: needsName(it, true),
      build: () => (it.kind === "consent" ? consentCard(it.c) : questionCard(it.q)),
    };
    modalGlance = false;
    modalKey = "needs:" + id;
    modalReturnFocus = opener || null;
    modalEpisodeQuery = "";
    needsFocusNow = { it, id };
    watchWideDialog();
    buildModal(null);
    // a question's handle comes from its words, which are fetched: the title is retaken when they land (the card reads the same fetch)
    if (it.kind === "question") Promise.resolve(questionBody(it.q, null)).then(() => retitleNeedsFocus(it, id), () => {});
  }
  // the desk/phone title form is read when the title is built: crossing the breakpoint with the dialog open retakes it
  function watchWideDialog() {
    if (wideWatched || !window.matchMedia) return;
    wideWatched = true;
    try {
      const mq = window.matchMedia("(min-width: 761px)");
      const on = () => { if (needsFocusNow && needsFocusNow.it.kind === "question") retitleNeedsFocus(needsFocusNow.it, needsFocusNow.id); };
      if (mq.addEventListener) mq.addEventListener("change", on); else if (mq.addListener) mq.addListener(on);
    } catch (e) { wideWatched = false; }
  }
  function retitleNeedsFocus(it, id) {
    // only the Needs focus view of this very item: a custom modal that reuses the id is another view and keeps its own title
    if (!modalCustom || modalCustom.page || modalCustom.id !== id || modalKey !== "needs:" + id) return;
    const title = needsName(it, true);
    if (title === modalCustom.title) return;
    modalCustom.title = title;
    const h = document.getElementById("levain-modal-title");
    if (h) h.textContent = title;
    const region = document.querySelector("#levain-modal .modal-body");
    if (region) region.setAttribute("aria-label", title + " — scrollable content");
  }
  // The way in for another script's focus view (the Board tab's ⤢): a custom modal with that script's own content. `live` keeps it open
  // across a dashboard refresh and rebuilds its content then (refreshModal); a modal that is not live is closed as a Needs-you card is.
  function openCustom(spec) {
    if (!spec || typeof spec.build !== "function") return;
    modalCustom = { id: String(spec.id || "custom"), title: String(spec.title || "panel"), build: spec.build, live: spec.live === true };
    modalGlance = false;
    modalKey = "custom:" + modalCustom.id;
    needsFocusNow = null;
    modalReturnFocus = spec.opener || null;
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
  const SEAT_DIRECT_NOTE = "For direct work with this seat (a high-residue build, a deep session on its own problem, or a seat stuck at a prompt). Everything else goes to the desk: use the drop box.";
  function seatMessageBox(name) {
    const wrap = el("div", "agent-msg");
    wrap.appendChild(el("div", "needs-sec-label", "Message " + name));
    wrap.appendChild(el("div", "agent-msg-frame ext-meta", SEAT_DIRECT_NOTE));
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
    // row 1704: the input is the whole of it however long. Past the long mark IN ALL (its whole display, so many short values
    // count as one long input) it is one block, head and tail with the whole one tap away, which the card's Allow is gated on
    // (drawConsentCard); under it, the key/value list as before
    const F = window.LevainCardFormat;
    if (F && typeof F.longText === "function" && Array.from(t).length > F.LONG_AT) return F.longText(t, "consent-display");
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
    const shown = egress ? null : consentRequest(sv(b.display), card._needsItem && card._needsItem.c.tool);
    const parts = egress ? [label("Request"), egress.state, egress.rows] : [label("Request"), shown];
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
    // a long input gates Allow here as on the sign sheet (row 1704 fix 1): disabled until the whole input has been opened
    // and scrolled to its end, the count of what is not yet shown beside the buttons; Deny is never gated
    const CF = window.LevainCardFormat;
    const gate = shown && CF && typeof CF.seenGate === "function" ? CF.seenGate(shown) : null;
    let allowBtn = null;
    const cid = card._needsItem && card._needsItem.c ? card._needsItem.c.id : "";
    const choices = b.sheet === true && typeof cid === "string" && cid && Array.isArray(b.sheet_choices)
      ? b.sheet_choices.filter((k) => k === "allow" || k === "deny") : [];
    for (const [label, cmd] of [["Allow", sv(b.answer_allow)], ["Deny", sv(b.answer_deny)]]) {
      if (!cmd) continue;
      const btn = el("button", "needs-btn", label);
      btn.type = "button";
      btn.dataset.label = label;
      const code = el("code", "consent-code", cmd);
      const shut = () => gate && label === "Allow" && !gate.seen();
      if (choices.includes(label.toLowerCase())) {
        btn.title = label.toLowerCase() + " with your passkey, in the sign sheet";
        btn.dataset.consentSheet = label.toLowerCase();
        btn.addEventListener("click", () => { if (!shut()) openConsentSheet(cid, sv(b.sheet_v), label.toLowerCase()); });
      } else {
        btn.title = "copy the " + label.toLowerCase() + " command for a terminal; this page signs nothing, sign on your phone with Face ID";
        btn.addEventListener("click", () => { if (!shut()) copyText(cmd, code, btn); });
      }
      if (label === "Allow") allowBtn = btn;
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
    if (gate && allowBtn) {
      const line = el("div", "ext-meta needs-unseen");
      line.setAttribute("role", "status");
      const show = () => { line.textContent = gate.line(); line.dataset.seen = gate.seen() ? "1" : "0"; allowBtn.disabled = !gate.seen(); };
      gate.on(show);
      show();
      card.appendChild(line);
    }
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
  // "answered <age>" with an age the page's age timer keeps current; a line with no stamp shows the label it was given
  function settledTag(q) {
    const tag = el("span", "ext-meta needs-left needs-settled-tag", "answered ");
    const K = window.LevainCockpitKinds;
    if (q.decidedAt && K && K.ageSpan) tag.appendChild(K.ageSpan(q.decidedAt));
    else tag.appendChild(document.createTextNode(q.decidedAge || ""));
    return tag;
  }
  function questionCard(q) {
    const card = el("div", "needs-card needs-question is-full");
    card.dataset.needsCard = "question";
    card._needsItem = { kind: "question", q };
    const settled = q.state === "answered";
    if (settled) card.classList.add("needs-settled");
    needsHead(card, settled ? settledTag(q) : (q.age ? el("span", "ext-meta needs-left", "asked " + q.age) : null));
    const slot = el("div", "needs-body ext-meta", "reading the question…");
    card.appendChild(slot);
    Promise.resolve(questionBody(q, card)).then((b) => {
      if (!b) return;
      drawQuestionBody(q, slot, b, card, settled);
      // V1-7: the speaker that reads the card aloud (card_voice.js); absent when the page has no player or the card has no words;
      // never on an answered card: a receipt has nothing left to read aloud
      const CV = window.LevainCardVoice, sp = CV && !b.error && !settled ? CV.speaker(String(q.id || ""), b) : null;
      if (sp) { const head = card.querySelector(":scope > .needs-head"); if (head) head.appendChild(sp); else card.insertBefore(sp, slot); }   // Play shares the "asked" line
      if (!b.error && !settled) card.appendChild(needsDetails([copyLine("levain question show " + q.n, "show command")], ""));
    });
    return card;
  }
  function clearNeeds(host) {
    host.replaceChildren();
    host.hidden = true;
    delete host.dataset.sig;
    delete host.dataset.waiting;
  }
  // The strip's row (option A, Phill 10-26): one fixed-height summary per waiting item, a single button that opens the focus view.
  // It carries no answer control: the options, the words box, Allow/Deny and the sign row live only in the focus view, where the
  // item is drawn whole. Line one is the question's title (a consent's tool and first words), line two what it blocks (a consent's
  // working directory), the right edge the agent, the clock or age, and "rec <key>" when the item carries one. Text is drawn with
  // textContent and cut by CSS to one line each, so a long item never sets its row's height.
  // The card face's words: the first sentence of the question (CSS clamps it to two lines); a text with no sentence end is whole.
  function needsFirstSentence(what) {
    const t = String(what || "").replace(/\s+/g, " ").trim();
    // a sentence ends at . ! ? followed by a space (or at once by a capital: "Which way?Really."), but not after a title or
    // an abbreviation (Dr. Mr. Mrs. Ms. e.g. i.e. etc. vs.)
    const abbr = /(?:^|[\s(])(?:Dr|Mr|Mrs|Ms|St|vs|etc|approx|e\.g|i\.e)\.$/i;
    const re = /[.!?](?=\s|$|[A-Z])/g;
    let m;
    while ((m = re.exec(t))) {
      if (m.index === 0) continue;
      const head = t.slice(0, m.index + 1);
      if (m[0] === "." && abbr.test(head)) continue;
      return head;
    }
    return t;
  }
  function needsRow(it) {
    const row = el("button", "needs-row" + (it.kind === "consent" ? " needs-consent" : " needs-question"));
    row.type = "button";
    row.setAttribute("aria-haspopup", "dialog");
    row._needsItem = it;
    const id = needsIdOf(it);
    if (id) row.dataset.needsId = id;
    row.dataset.needsCard = it.kind;
    // display only: the Desk owes tab says it already, and the 2-line clamp is better spent on the ask
    const title = el("span", "needs-row-title", it.kind === "consent" ? (it.c.tool || "request") : (String(needsName(it) || "").replace(/^\s*DESK OWES:\s*/i, "") || needsName(it)));
    const sub =el("span", "needs-row-sub ext-meta", "");
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
      if (it.q.age) {
        const ag = el("span", "ext-meta needs-left", "");
        ag.append(el("span", "needs-asked", "asked "), document.createTextNode(it.q.age));
        side.appendChild(ag);
      }
      if (it.q.options > 0) side.appendChild(el("span", "ext-meta needs-row-opts", it.q.options + (it.q.options === 1 ? " option" : " options")));
      const bn = cardBandNum(it.q.n);
      const tag = el("b", "band-num needs-row-band", bn === undefined ? "" : String(bn));   // the band number leads the face; applyBandNumbers retakes it
      tag.hidden = bn === undefined;
      row.dataset.questionN = String(it.q.n);
      side.insertBefore(tag, side.firstChild);
      questionBody(it.q, row).then((b) => {
        if (!b || b.error) return;
        const first = needsFirstSentence(b.what);
        if (first) title.textContent = first.replace(/^\s*DESK OWES:\s*/i, "") || first;   // display only, as the placeholder title above
        if (flat(b.what)) row.title = flat(b.what);
        const more = flat(blocksBeyondTitle(b.what, b.blocks));
        if (more) sub.textContent = "blocks: " + more;
        if (typeof b.rec === "string" && b.rec) side.appendChild(el("span", "needs-rec-tag needs-row-rec", "rec " + b.rec));
      });
    }
    if (!row.title) row.title = "open the whole request and decide there";
    row.append(text, side);
    row.addEventListener("click", () => openNeedsFocus(row._needsItem || it, row));
    return row;
  }
  // One timer for the earliest answered card to leave the strip: it re-runs the selection, which drops the card on an idle page.
  // Every render clears it and arms it again for the cards then listed.
  let needsExpiryTimer = 0;
  const ANSWERED_KEY = "levain.needs.answeredOpen";
  let answeredOpen = false;
  try { answeredOpen = window.localStorage.getItem(ANSWERED_KEY) === "1"; } catch (e) { answeredOpen = false; }
  // ---- the section's tabs, layout and fold (Phill 11-06: two tabs in this area only; rows or two-column tiles, both mocked) ----
  // The "Desk owes" tab: the open questions the desk itself owes (section desk), carried ones included (a carried one is also in
  // "Needs you", as deskTabs tags it "both"). Nothing here is counted into the badge or the app icon: those stay the waiting items.
  function deskOwesItems(lines) {
    const out = [];
    for (const ln of Array.isArray(lines) ? lines : []) {
      if (ln && ln.question && ln.question.state === "open" && ln.question.section === "desk" && !needsLaterDay(ln.question)) out.push({ kind: "question", q: ln.question });
    }
    return out;
  }
  const NEEDS_LAYOUT_KEY = "levain.needs.layout", NEEDS_TAB_KEY = "levain.needs.tab";
  const NEEDS_FOLD_TILES = 8;               // phone tiles shown before the fold (four rows of two)
  const NEEDS_FOLD = 6;                     // rows shown before the fold in the rows layout
  let needsViewRef = null;                  // the last view the strip drew from: a tab or layout change re-renders from it, no network
  let needsLayoutNow = null, needsTabNow = null;
  let needsMoreOpen = false;                // the fold, per page load
  const needsRowCache = new Map();          // needs id -> its row, kept across tab switches and folds
  function needsStore(get, key, val) {
    try { if (get) return window.localStorage.getItem(key); window.localStorage.setItem(key, val); } catch (e) { /* the choice then lasts this page only */ }
    return null;
  }
  // The page's layout (?needs=rows|tiles beats the remembered choice; default rows), the active tab, and that the layout switch is drawn
  // (the stylesheet shows it on a phone only).
  function needsPrefs(view) {
    if (needsLayoutNow === null) {
      let q = "";
      try { const m = /[?&]needs=(rows|tiles)(?:&|#|$)/.exec(window.location.search || ""); q = m ? m[1] : ""; } catch (e) { q = ""; }
      const stored = needsStore(true, NEEDS_LAYOUT_KEY);
      needsLayoutNow = q || (stored === "tiles" ? "tiles" : "rows");
      if (q) needsStore(false, NEEDS_LAYOUT_KEY, q);
    }
    if (needsTabNow === null) needsTabNow = needsStore(true, NEEDS_TAB_KEY) === "desk" ? "desk" : "you";
    return { layout: needsLayoutNow, tab: needsTabNow, sw: true };
  }
  function rerenderNeeds(focusSel) {
    const host = document.getElementById("ck-needs");
    if (!host || !needsViewRef) return;
    delete host.dataset.sig;
    renderNeeds(needsViewRef);
    if (focusSel) { const n = host.querySelector(focusSel); if (n && typeof n.focus === "function") n.focus({ preventScroll: true }); }
  }
  // `viewOnly`: switch the tab for this page without writing the reader's saved choice (a go to the first waiting card needs Needs you; it does not change what he keeps)
  function setNeedsTab(key, viewOnly) {
    needsTabNow = key === "desk" ? "desk" : "you";
    if (!viewOnly) needsStore(false, NEEDS_TAB_KEY, needsTabNow);
    needsMoreOpen = false;
    rerenderNeeds('.needs-tab[data-needs-tab="' + needsTabNow + '"]');
  }
  function setNeedsLayout(key) {
    needsLayoutNow = key === "tiles" ? "tiles" : "rows";
    needsStore(false, NEEDS_LAYOUT_KEY, needsLayoutNow);
    try {
      const u = new URL(window.location.href);
      u.searchParams.set("needs", needsLayoutNow);
      window.history.replaceState(window.history.state, "", u.pathname + u.search + u.hash);   // a reload keeps the choice the URL names
    } catch (e) { /* no history: the remembered choice stands */ }
    rerenderNeeds('.needs-layout-btn[data-needs-layout="' + needsLayoutNow + '"]');
  }
  function needsAgeSecs(it) {
    if (it.kind !== "question") return null;
    const ms = Date.parse(it.q.at || "");
    return Number.isFinite(ms) ? Math.max(0, Math.floor((needsNow() - ms) / 1000)) : null;
  }
  function needsAgeText(s) {
    return s >= 86400 ? Math.floor(s / 86400) + " d" : s >= 3600 ? Math.floor(s / 3600) + " h" : s >= 60 ? Math.floor(s / 60) + " min" : s + " s";
  }
  // The list under the tabs. Rows layout: grouped by age (Today, then Older than a day; labels only when both exist), the first
  // NEEDS_FOLD rows shown, then one fold row whose label carries the hidden count and the oldest hidden age so a row never hides
  // unseen. Tiles layout: every item, no fold; the page scrolls.
  function drawNeedsList(grid, list, prefs, rowOf) {
    const kids = [];
    if (!list.length) {
      kids.push(el("div", "needs-empty ext-meta", prefs.tab === "desk" ? "The desk owes you nothing right now." : "Nothing waits on you right now."));
    } else {
      let phone = false;
      try { phone = !!(window.matchMedia && window.matchMedia("(max-width: 599px)").matches); } catch (e) { phone = false; }
      const tiles = prefs.layout === "tiles" && phone;   // the choice is a phone one: a desktop draws the same cards whichever was stored
      const old = (it) => { const s = needsAgeSecs(it); return s !== null && s >= 86400; };
      const today = list.filter((it) => !old(it)), older = list.filter(old);
      const grouped = !tiles && today.length > 0 && older.length > 0;   // the tiles carry no group labels
      const ordered = grouped ? today.concat(older) : list;
      // both layouts fold; a phone's tiles show four rows of two before the fold, everything else six items
      const foldAt = tiles ? NEEDS_FOLD_TILES : NEEDS_FOLD;
      const hiddenN = ordered.length - foldAt;
      const fold = hiddenN >= 2;                     // one hidden item is not worth a row of its own
      const shown = !fold || needsMoreOpen ? ordered : ordered.slice(0, foldAt);
      let last = "";
      for (const it of shown) {
        const g = grouped ? (old(it) ? "older" : "today") : "";
        if (g && g !== last) {
          kids.push(el("div", "needs-group", g === "today" ? "Today (" + today.length + ")" : "Older than a day (" + older.length + ")"));
          last = g;
        }
        kids.push(rowOf(it));
      }
      if (fold) {
        const hidden = ordered.slice(foldAt);
        let label;
        if (needsMoreOpen) label = "Show fewer";
        else {
          const ages = hidden.map(needsAgeSecs).filter((s) => s !== null);
          label = "Show " + hiddenN + " more" + (ages.length ? ", oldest " + needsAgeText(Math.max.apply(null, ages)) : "");
          const due = hidden.filter((it) => it.kind === "consent" && Number.isFinite(Date.parse(it.c.expiresAt)));
          if (due.length) label += " · " + due.length + " expiring, soonest " + consentLeft(due.sort((a, b) => Date.parse(a.c.expiresAt) - Date.parse(b.c.expiresAt))[0].c.expiresAt);
        }
        const b = el("button", "needs-more-row", label);
        b.type = "button";
        b.setAttribute("aria-expanded", needsMoreOpen ? "true" : "false");
        b.addEventListener("click", () => { needsMoreOpen = !needsMoreOpen; rerenderNeeds(".needs-more-row"); });
        kids.push(b);
      }
    }
    grid.replaceChildren(...kids);
  }
  function renderNeeds(view) {
    if (needsExpiryTimer) { window.clearTimeout(needsExpiryTimer); needsExpiryTimer = 0; }
    try { renderNeedsNow(view); }
    finally { armNeedsExpiry(view); }
  }
  function armNeedsExpiry(view) {
    if (typeof window.setTimeout !== "function") return;
    let soonest = Infinity;
    for (const ln of Array.isArray(((view.extra_panels || {}).decisions || {}).lines) ? view.extra_panels.decisions.lines : []) {
      const q = ln && ln.question;
      if (!q || q.state !== "answered") continue;
      const left = decidedLeftMs(q.decidedAt);
      if (left !== null && left > 0 && left < soonest) soonest = left;
    }
    if (soonest === Infinity) return;
    needsExpiryTimer = window.setTimeout(() => {
      needsExpiryTimer = 0;
      try { renderNeeds(view); } catch (e) { /* the strip keeps what it showed */ }
    }, Math.min(soonest + 50, 2147483000));
  }
  function renderNeedsNow(view) {
    signRecheck();      // a board read: every frozen card asks the cockpit about its unsettled answer again
    const host = document.getElementById("ck-needs");
    if (!host) return;
    needsViewRef = view;
    const lines = ((view.extra_panels || {}).decisions || {}).lines;
    const all = needsItems(lines);
    const laterYou = needsLaterItems(lines, "you"), laterDesk = needsLaterItems(lines, "desk");
    const desk = deskOwesItems(lines);
    const items = needsWaiting(all), settled = all.filter((it) => it.answered);
    ncNeedsCount(items.length);               // the strip's Needs you count and the app-icon badge: the waiting items, nothing else
    syncAppBadge(view, items.length);
    if (!all.length && !desk.length && !laterYou.length && !laterDesk.length) { clearNeeds(host); return; }
    // the same set of waiting items leaves the strip as it is: a card with half-typed words, an open "details" and a running
    // countdown is not rebuilt under the reader by a refetch that changed nothing about what waits. The layout, the tab and the
    // fold are part of what is drawn, so a change of one is a new signature (a plain re-render, no network).
    const prefs = needsPrefs(view);
    const sig = all.map(needsKey).join("\n") + "\nD:" + desk.map(needsKey).join(",") + "\nL:" + laterYou.map((it) => it.q.n + "@" + it.later + "@" + (it.q.laterWords || "")).join(",") + "\nLD:" + laterDesk.map((it) => it.q.n + "@" + it.later + "@" + (it.q.laterWords || "")).join(",") + "\nP:" + prefs.layout + "|" + prefs.tab + "|" + (needsMoreOpen ? 1 : 0) + "|" + (prefs.sw ? 1 : 0);
    if (host.dataset.sig === sig && !host.hidden) return;
    // the answered cards are drawn under a quiet label of their own; with nothing waiting that is all the strip holds
    // folded by default (Phill 11-02): a count under a label you click to open; the cards stay in the page, hidden, so the
    // strip's signature and the settled-card tick are unchanged. Per viewer and per browser.
    const answeredSection = () => {
      const sec = el("div", "needs-answered");
      const toggle = el("button", "needs-answered-toggle");
      toggle.type = "button";
      toggle.appendChild(el("span", "needs-sec-label", "Just answered"));
      toggle.appendChild(el("span", "needs-answered-n", String(settled.length)));
      const body = el("div", "needs-answered-body");
      for (const it of settled) body.appendChild(questionCard(it.q));
      const show = (open) => {
        body.hidden = !open;
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        toggle.dataset.open = open ? "1" : "0";
      };
      show(answeredOpen);
      toggle.addEventListener("click", () => {
        answeredOpen = body.hidden;
        show(answeredOpen);
        try { window.localStorage.setItem(ANSWERED_KEY, answeredOpen ? "1" : "0"); } catch (e) { /* the choice then lasts this page only */ }
      });
      sec.append(toggle, body);
      return sec;
    };
    const laterSection = (rows) => {
      const sec = el("div", "needs-later");
      const hd = el("div", "needs-later-head");       // one header like Just answered's: the label and the count, then the rows
      hd.append(el("span", "needs-sec-label", "Later"), el("span", "needs-answered-n", String(rows.length)));
      sec.appendChild(hd);
      for (const it of rows) sec.appendChild(needsLaterLine(it));
      return sec;
    };
    // each tab draws its own LATER lines: a "you" row never sits in the Desk owes tab, nor a desk one in Needs you
    const laterShown = prefs.tab === "desk" ? laterDesk : laterYou;
    if (!items.length && !desk.length && !laterYou.length && !laterDesk.length) {
      host.replaceChildren(...(settled.length ? [answeredSection()] : []));
      delete host.dataset.waiting;
      host.hidden = false;
      host.dataset.sig = sig;
      return;
    }
    if (items.length) host.dataset.waiting = "1"; else delete host.dataset.waiting;
    host.dataset.needsLayout = prefs.layout;
    const head = el("div", "needs-headline");
    // The section's two tabs (Phill 11-06: the items that need him apart from the desk's own, in this area only). Each label carries its
    // live count; the choice is kept per browser; a change is a re-render from the view already held, no network.
    const tabs = el("div", "needs-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "Needs you");
    // the Desk owes number is the Decisions card's own (card_format.js deskTabs): every open desk question, dated ones included
    // (they are the LATER lines under this tab), so the strip counts the rows it draws and cannot differ from the card
    // (a page whose card module did not load has no such number to draw: the tab then carries none, never one that could disagree)
    const CFD = window.LevainCardFormat;
    const deskOwed = CFD && typeof CFD.deskTabs === "function" ? CFD.deskTabs(Array.isArray(lines) ? lines : []).desk : null;
    for (const [key, text, n] of [["you", "Needs you", items.length], ["desk", "Desk owes", deskOwed]]) {
      const b = el("button", "needs-tab" + (prefs.tab === key ? " is-on" : ""), n === null ? text : text + " (" + n + ")");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", prefs.tab === key ? "true" : "false");
      b.dataset.needsTab = key;
      b.addEventListener("click", () => setNeedsTab(key));
      tabs.appendChild(b);
    }
    head.appendChild(tabs);
    // no re-ping route exists (the phone is told when a request is filed, and the queue re-pings on its own clock; nothing here
    // pushes), so the gloss claims no ping: it says where the answer is signed
    if (items.length && prefs.tab === "you") head.appendChild(el("span", "needs-gloss", "Agents are waiting on a ruling. Sign it on your phone."));
    if (prefs.tab === "you" && items.some((it) => it.kind === "question") && items.some((it) => it.kind === "consent")) {
      head.appendChild(el("span", "needs-order", "questions first, then soonest to expire"));
    }
    if (prefs.sw) {
      // the phone's rows | tiles segmented control: CSS hides it at desktop width, where both choices draw the same cards
      const sw = el("span", "needs-layout-sw");
      sw.setAttribute("role", "group");
      sw.setAttribute("aria-label", "layout");
      for (const key of ["rows", "tiles"]) {
        const b = el("button", "needs-layout-btn" + (prefs.layout === key ? " is-on" : ""), key);
        b.type = "button";
        b.dataset.needsLayout = key;
        b.setAttribute("aria-pressed", prefs.layout === key ? "true" : "false");
        b.addEventListener("click", () => setNeedsLayout(key));
        sw.appendChild(b);
      }
      head.appendChild(sw);
    }
    const hist = el("button", "needs-history", "all decisions");
    hist.type = "button";
    hist.title = "decided requests, outcomes and standing-rule proposals";
    hist.addEventListener("click", () => openDecisionsHistory(hist));
    head.appendChild(hist);
    // the rows are kept by their stable id: a row whose item still waits is moved, never rebuilt (words typed into an item live in
    // its focus view, which stays open while the item waits: refreshModal). The cache outlives a tab switch: the other tab's rows
    // wait detached, so switching back reads nothing again.
    let grid = host.querySelector(".needs-grid");
    const kept = needsRowCache;
    const live = new Set();
    for (const it of items.concat(desk)) { const id = needsIdOf(it); if (id) live.add(id); }
    for (const id of Array.from(kept.keys())) if (!live.has(id)) kept.delete(id);
    if (grid) for (const c of grid.children) if (c.dataset && c.dataset.needsId && live.has(c.dataset.needsId)) kept.set(c.dataset.needsId, c);
    // a row kept by id still shows what its item says NOW: a consent whose expiry moved (extended) takes the new clock in place;
    // one whose state changed is rebuilt
    const keep = (row, it) => {
      row._needsItem = it;   // the click handler reads this, so a kept row (question or consent) opens its item as it is NOW
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
    const rowOf = (it) => { const id = needsIdOf(it); let r = id && kept.get(id); r = r ? keep(r, it) : needsRow(it); if (id) kept.set(id, r); return r; };
    if (!grid) grid = el("div", "needs-grid");
    grid.dataset.layout = prefs.layout;
    grid.setAttribute("role", "tabpanel");
    drawNeedsList(grid, prefs.tab === "desk" ? desk : items, prefs, rowOf);
    const kids = [head, needsBar(items), grid];   // the bar is CSS-hidden on Board and is the only Needs surface on Build and Memory, whichever tab is picked
    if (laterShown.length) kids.push(laterSection(laterShown));
    if (settled.length) kids.push(answeredSection());
    host.replaceChildren(...kids);
    host.hidden = false;
    // the cards lead with their band number: the band read gives it (the Decisions panel's own slot may not be drawn on this tab)
    // (no band transport on a read-only manifest surface: the cards then draw without numbers)
    if (typeof bandRead === "function" && (items.some((it) => it.kind === "question") || laterYou.length || laterDesk.length)) {
      const seq = ++cardBandSeq;
      let p = null;
      try { p = bandFetch(false); } catch (e) { p = null; }
      if (p && typeof p.then === "function") p.then((b) => {
        if (seq !== cardBandSeq) return;     // a newer render's read owns the numbers now
        const list = b && Array.isArray(b.items) ? b.items : [];
        cardBandOf.clear();
        for (const x of list) if (x && Number.isInteger(x.question) && x.n !== undefined) cardBandOf.set(x.question, x.n);
        applyBandNumbers();
      }, (e) => {
        if (/no question store/.test(e && e.message ? String(e.message) : "")) return;     // nothing to retry on a cockpit without one
        // a refused or busy read is not "done": the next render draws the strip again and asks again
        if (seq === cardBandSeq && host.dataset.sig === sig) delete host.dataset.sig;
      });
    }
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
      const chip = el("button", "needs-chip-btn", it.kind === "consent" ? (it.c.tool || "request") : needsName(it));
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
    if (typeof first.scrollIntoView === "function") first.scrollIntoView({ block: "center", behavior: "smooth" });
    first.focus({ preventScroll: true });
    return true;
  }

  // A record that names a card (the Work lens's waiting items) goes to the strip's card with that id: scrolled to and lit by an
  // animation that ends on its own (needs-lit), any earlier light cleared first. Where the strip is the bar (Build, Memory) the matching
  // chip is lit instead. False when no card or chip carries the id, so the caller can open the history.
  function needsGo(id, instant) {
    const host = document.getElementById("ck-needs");
    if (!host || host.hidden || !id) return false;
    for (const n of host.querySelectorAll(".needs-lit")) n.classList.remove("needs-lit");
    const cards = host.querySelector(".needs-row") && host.querySelector(".needs-row").offsetParent;
    const sel = cards ? ".needs-row" : ".needs-chip-btn";
    const node = Array.prototype.find.call(host.querySelectorAll(sel), (n) => n.dataset.needsId === id);
    if (!node) return false;
    needsLight(node, instant, false);
    return true;
  }
  // Scroll to a card or chip and light it by an animation that ends on its own. `focus`: the keyboard's place moves to it too (it is a button, so it takes
  // focus as it is), so Tab goes on from there; only goFirst, the move to the first card, asks for it, no other caller's focus moves.
  function needsLight(node, instant, focus) {
    // `instant`: the caller locks the page the next moment (the item link opens the card's sheet at once), and the lock freezes the page at the
    // scroll position it finds: a smooth scroll only begun would be frozen at the top, so the reader's place behind the sheet would be lost
    if (typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "center", behavior: instant ? "auto" : "smooth" });
    void node.offsetWidth;   // restart the animation if the same node is lit twice
    node.classList.add("needs-lit");
    if (focus && typeof node.focus === "function") { if (node.tabIndex < 0 && !node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1"); node.focus({ preventScroll: true }); }   // the scroll above is the jump's own
    const off = () => node.classList.remove("needs-lit");
    node.addEventListener("animationend", off, { once: true });
    window.setTimeout(off, 3000);   // a page that plays no animation (reduced motion) still ends the light
  }
  // The strip's first waiting card, as the reader sees the strip NOW (asked of the strip itself, not of the topology):
  // the strip is on the page and holds a waiting item, and it is drawn as cards or as the bar. True is what a caller needs before it offers a way there.
  // ONE predicate for both questions ("can it go?" and "go"): the strip's first visible waiting card ({node}) or chip ({node}), asked of the DOM as the reader sees it.
  // On the Desk owes tab the waiting-on-you rows are detached: the answer is {node: null, viaTab: true} (the strip holds waiting items, Needs you is one tab away,
  // and the page has the view it re-renders from). Null when the strip is hidden, holds nothing waiting, or draws an empty list (a stale flag over an empty grid).
  function needsFirstTarget(host) {
    if (!host || host.hidden || host.dataset.waiting !== "1") return null;
    const visible = (list) => Array.prototype.find.call(list, (n) => n.offsetParent) || null;
    const bar = host.querySelector(".needs-bar");
    if (bar && bar.offsetParent) { const chip = visible(bar.querySelectorAll(".needs-chip-btn")); return chip ? { node: chip } : null; }
    const grid = host.querySelector(".needs-grid");
    if (!grid || !grid.offsetParent) return null;
    const on = host.querySelector(".needs-tab.is-on");
    if (on && on.dataset.needsTab === "desk") return needsViewRef ? { node: null, viaTab: true } : null;
    const row = visible(grid.querySelectorAll(".needs-row"));
    return row ? { node: row } : null;
  }
  function needsCanGoFirst() { return !!needsFirstTarget(document.getElementById("ck-needs")); }
  // Light, scroll to and focus the first card the strip draws (its own order: questions first, then soonest to expire; the first row is never behind the fold).
  // The Desk owes tab holds none of the waiting-on-you cards, so the strip is switched to Needs you for this view only (the saved tab choice is not overwritten).
  // On Build the strip is the bar: its first chip. False (nothing moved) when no such card is drawn.
  function needsGoFirst() {
    const host = document.getElementById("ck-needs");
    let t = needsFirstTarget(host);
    if (!t) return false;
    if (t.viaTab) {
      setNeedsTab("you", true);
      t = needsFirstTarget(host);
      if (!t || !t.node) return false;
    }
    for (const n of host.querySelectorAll(".needs-lit")) n.classList.remove("needs-lit");
    needsLight(t.node, false, true);
    return true;
  }

  // The Open loops panel on the PAGE: the focus view (a .modal) draws a copy of the same rows, which is not where the Work lens means to go.
  function loopsPanel() {
    return Array.prototype.find.call(document.querySelectorAll('.panel[data-ck-panel="loops"]'), (p) => !p.closest(".modal")) || null;
  }
  function loopRowIn(panel, id) {
    return Array.prototype.find.call(panel.querySelectorAll(".row[data-intention]"), (n) => n.dataset.intention === id && !n.closest(".modal")) || null;
  }
  // The page's row for a loop, or null. A filter typed into the panel's bar hides rows by not drawing them, so a row that is missing under a
  // query is looked for again with the query cleared through the bar's own input path; when it is not there either, the person's query is put back.
  function loopFind(id) {
    if (!id) return null;
    const panel = loopsPanel();
    if (!panel) return null;
    let row = loopRowIn(panel, id);
    if (row) return row;
    const input = panel.querySelector(".ep-search-input");
    if (!input || !input.value) return null;
    const kept = input.value;
    input.value = "";
    // the bar applies its query synchronously on this event, so the second lookup needs no frame's wait
    input.dispatchEvent(new Event("input", { bubbles: true }));
    row = loopRowIn(panel, id);
    if (!row) { input.value = kept; input.dispatchEvent(new Event("input", { bubbles: true })); }
    else loopCleared = { id, kept, input };
    return row;
  }
  // the query a successful lookup cleared, for a jump that then fails to put it back; and why the last jump failed ("absent", "edit", "hidden")
  let loopCleared = null;
  let loopWhy = "";
  function loopRestore(id) {
    const c = loopCleared;
    loopCleared = null;
    if (c && c.id === id && c.input.isConnected && !c.input.value) { c.input.value = c.kept; c.input.dispatchEvent(new Event("input", { bubbles: true })); }
  }
  // one highlight at a time: a second tap re-arms the same timer instead of racing the first one's end
  let loopLitTimer = 0;
  // An open loop the Work lens names goes to its row in the Open loops panel: a collapsed card is expanded through its own control, a row the
  // list folded away is shown through the fold's own pin, and the row is scrolled to, focused and outlined (tv-lit) for a moment.
  // True only when the row is on screen afterwards; false (nothing lit) when the page has no such row or the panel refused to unfold.
  function loopGo(id) {
    loopWhy = "";
    const row = loopFind(id);
    if (!row) { loopWhy = "absent"; return false; }
    const panel = row.closest(".panel[data-ck-panel]");
    if (!panel) { loopRestore(id); loopWhy = "hidden"; return false; }
    if (panel.classList.contains("ck-collapsed")) {
      const f = panel.querySelector(".ck-lay-fold");
      if (f && !f.disabled) f.click();
      // an unsaved edit refuses the fold: the card stays shut, and a row inside it is not lit
      if (panel.classList.contains("ck-collapsed")) { loopRestore(id); loopWhy = "edit"; return false; }
    }
    const body = row.closest(".pbody");
    if (row.dataset.folded || row.style.display === "none") {
      // the fold keeps every row down to the one the reader opened: opening this one is the fold's own way of showing it
      setExpanded(row, true);
      if (body) measureOverflow(body);
    }
    if (!row.getClientRects().length) { loopRestore(id); loopWhy = "hidden"; return false; }
    loopCleared = null;
    for (const n of document.querySelectorAll(".tv-lit[data-intention]")) n.classList.remove("tv-lit");
    row.classList.add("tv-lit");
    window.clearTimeout(loopLitTimer);
    loopLitTimer = window.setTimeout(() => row.classList.remove("tv-lit"), 2600);
    // a row that already has a tab stop (the writable page's verbs) keeps it; one without gets a temporary -1 that blur takes away
    if (!row.hasAttribute("tabindex")) {
      row.tabIndex = -1;
      row.addEventListener("blur", () => { if (row.getAttribute("tabindex") === "-1") row.removeAttribute("tabindex"); }, { once: true });
    }
    if (typeof row.focus === "function") row.focus({ preventScroll: true });
    const calm = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    const scroll = () => { if (typeof row.scrollIntoView === "function") row.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" }); };
    const raf = window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : (f) => window.setTimeout(f, 16);
    const at = () => row.getBoundingClientRect().top + window.scrollY;
    // the unfold and the tab switch settle layout over a frame: scroll after it, and again when the row's place moved meanwhile
    raf(() => {
      const first = at();
      scroll();
      raf(() => { if (Math.abs(at() - first) > 1) scroll(); });
    });
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
  const SESS_KIND = { prompt: "you", reply: "agent", tool: "tool", consent: "consent", wake: "wake", wake_reply: "wake reply" };
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
  // a voice key answers no request, so it has no refusals to count: whatever its record carries, it is never demoted
  const grantRefused = (g) => (g && g.kind === "voice-key" ? 0
    : Math.max(g && g.answered && Number.isInteger(g.answered.refused_count) ? g.answered.refused_count : 0,
               grantRefusedShown(g)));
  // the speech providers' names as levain.voice.providers.NAMES spells them; a provider not listed shows as it is written
  const VOICE_PROVIDER_NAMES = { openai: "OpenAI", deepgram: "Deepgram", inworld: "Inworld" };
  function grantCard(g, inertNote, signList) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const state = sv(g.state);
    const refused = grantRefused(g);
    const row = el("div", "row consent-row grant-row" + (refused ? " ext-accent grant-demote" : "") + (state === "live" ? "" : " ext-dim"));
    row.setAttribute("data-consent-grant", sv(g.id));
    const head = el("div", "consent-head");
    // A voice key (levain.voice.custody) is listed here to be revoked, but it is no consent grant: it has no expiry and
    // answers no request. Its row names the provider and says it lives until revoked; the consent row's answered line and
    // "until <date>" would be an empty date and a claim about answers it never makes
    const voiceKey = sv(g.kind) === "voice-key";
    const a = g.answered && typeof g.answered === "object" ? g.answered : {};
    const count = Number.isInteger(a.count) ? a.count : 0;
    if (voiceKey) {
      const p = sv(g.provider);
      head.appendChild(el("span", "consent-agent", (Object.prototype.hasOwnProperty.call(VOICE_PROVIDER_NAMES, p) ? VOICE_PROVIDER_NAMES[p] : p) + (p ? " " : "") + "voice key"));
      head.appendChild(el("span", "ext-meta", "standing grant · " + state));
    } else {
      head.appendChild(el("span", "consent-agent", sv(g.agent) || sv(g.kind) || "grant"));
      const tools = Array.isArray(g.tools) ? g.tools.filter((x) => typeof x === "string").join(", ") : "";
      head.appendChild(el("b", "consent-tool", tools || sv(g.kind)));
      head.appendChild(el("span", "ext-meta", "standing grant · " + state));
      head.appendChild(el("span", "consent-left", count ? "answered " + count + "×" + (refused ? " · " + refused + " refused" : "")
                                                        : "answered nothing yet"));
    }
    row.appendChild(head);
    const facts = el("div", "consent-facts");
    if (sv(g.workspace)) facts.appendChild(el("span", "ext-meta", "in " + g.workspace));
    if (Array.isArray(g.postures) && g.postures.length) facts.appendChild(el("span", "ext-meta", g.postures.join(", ") + " mode"));
    // a voice key has no date: it is live until revoked, and any other state is drawn as its bare word (the head says it too)
    const untilText = voiceKey ? (state === "live" ? "Live until you revoke it" : state)
                               : (state === "expired" ? "expired " : "until ") + sv(g.expires_at);
    if (untilText) facts.appendChild(el("span", "ext-meta", untilText));
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
    signWho({ signers: signed.signers, origin: listing.origin, credentials: listing.credentials }, host, say).then((who) => {
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
      if (n !== grantFillSeq) return;
      slot.dataset.filled = "1";     // the grants' read has answered, with grants or without: the phone jump may fall back to the envelopes
      if (!b) return;
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
    }).catch(() => { if (n === grantFillSeq) slot.dataset.filled = "1"; });
  }
  // Board row 153: the envelopes beside the grants, each live one ended with a passkey, the grant revoke's own ceremony
  // (levain.desk.envelope_revoke.EnvelopeRevoke). /cockpit/envelope/list.json lists the envelopes and the passkeys
  // that may revoke (a 404 is a cockpit with no signing: no card), then /cockpit/envelope/<id>/revoke-challenge ->
  // navigator.credentials.get -> /revoke. The server builds and holds the message; the page recomputes the challenge from
  // the text it is sent, so what the prompt signs is the text shown. Without a usable passkey the button copies the
  // terminal line, as the grant card's does.
  const ENVELOPE_LIST = "/cockpit/envelope/list.json";
  let envelopeSaid = "";       // what the last revoke said, kept across the slot's redraw
  function envelopeFetch() {
    const headers = signHeaders(false);
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve(null);
    return fetchBounded(ENVELOPE_LIST, { cache: "no-store", headers: headers },
      (res) => (res.ok ? res.json().then((b) => (b && Array.isArray(b.envelopes) ? b : null), () => null) : null),
      () => null);
  }
  // the bytes a passkey signs for `line` in the envelope-revoke namespace, as levain.consent.passkey.challenge builds them
  function envelopeChallenge(line) {
    const enc = (t) => new TextEncoder().encode(t);
    const str = (b) => { const n = b.length, out = new Uint8Array(4 + n); out.set([n >>> 24 & 255, n >>> 16 & 255, n >>> 8 & 255, n & 255]); out.set(b, 4); return out; };
    return crypto.subtle.digest("SHA-512", enc(line)).then((h) => {
      const parts = [enc("SSHSIG"), str(enc("levain-envelope-revoke")), str(new Uint8Array(0)), str(enc("sha512")), str(new Uint8Array(h))];
      const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      parts.reduce((at, p) => { out.set(p, at); return at + p.length; }, 0);
      return out;
    });
  }
  // "ends in 3 h 20 min", in the words a person says it
  // (rounded up: "ended" only once the expiry has passed)
  function envelopeWhen(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const ms = t - Date.now();
    if (ms <= 0) return "ended";
    if (ms < 60000) return "ends in <1 min";
    const mins = Math.ceil(ms / 60000);
    if (mins < 60) return "ends in " + mins + " min";
    if (mins < 48 * 60) return "ends in " + Math.floor(mins / 60) + " h" + (mins % 60 ? " " + (mins % 60) + " min" : "");
    return "ends in " + Math.round(mins / 1440) + " days";
  }
  const ENVELOPE_ID = /^ev-[0-9a-f]{32}$/;
  // the states a revoke is offered for (an ended envelope is counted, not drawn)
  const ENVELOPE_REVOCABLE = ["live", "live-unknown", "unreadable"];
  // the WHOLE signed text, field by field: a line with a
  // field more, a field less or a field changed, another envelope's id, or another namespace is never put to the passkey
  const ENVELOPE_LINE = /^levain-envelope-revoke\/1\nstore: [0-9a-f]{32}\nenvelope: (ev-[0-9a-f]{32})\nat: [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\nnonce: [0-9a-f]{32}\n$/;
  function envelopeLineOk(body, id) {
    if (!body || typeof body.line !== "string" || body.namespace !== "levain-envelope-revoke") return false;
    const m = ENVELOPE_LINE.exec(body.line);
    return !!m && m[1] === id;
  }
  function envelopeCard(e, listing) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const row = el("div", "row consent-row envelope-row");
    row.setAttribute("data-envelope", sv(e.id));
    const head = el("div", "consent-head");
    head.appendChild(el("b", "consent-tool", sv(e.label) || sv(e.scope) || "envelope"));
    head.appendChild(el("span", "ext-meta", envelopeWhen(sv(e.expires))));
    row.appendChild(head);
    const facts = el("div", "consent-facts");
    if (sv(e.label) && sv(e.scope)) facts.appendChild(el("span", "ext-meta", sv(e.scope)));
    const ends = Date.parse(sv(e.expires));
    if (Number.isFinite(ends)) {
      const when = el("span", "ext-meta", new Date(ends).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }));
      when.title = sv(e.expires);
      facts.appendChild(when);
    }
    // a narrowing written for one bound thread only is said, never read as the whole envelope ending
    const tn = Number(e.threads), en = Number(e.ended_threads);
    if (en > 0) facts.appendChild(el("span", "ext-meta envelope-threads", "ended for " + en + " of " + (tn || en) + " thread" + ((tn || en) === 1 ? "" : "s")));
    facts.appendChild(el("span", "ext-meta", sv(e.id).slice(0, 11)));
    // a row the server could not read in full is still a row, said plainly, and its revoke stays
    if (e.state === "live-unknown") facts.appendChild(el("span", "ext-meta envelope-unknown", "an earlier stop it carries cannot be read; treat it as live"));
    if (e.state === "unreadable") facts.appendChild(el("span", "ext-meta envelope-unknown", sv(e.note) || "this envelope cannot be read in full"));
    row.appendChild(facts);
    if (!ENVELOPE_ID.test(sv(e.id))) return row;          // no id a revoke can name: nothing to offer
    const signers = Array.isArray(listing.signers) ? listing.signers : [];
    const line = el("div", "consent-cmd envelope-sign");
    const btn = el("button", "undo-btn envelope-revoke", "Revoke");
    btn.type = "button";
    if (signers.length && !signElsewhere(listing)) {
      btn.title = "revoke with your passkey";
      const note = el("span", "sign-note ext-meta");
      const say = (text, kind) => { note.textContent = text; note.className = "sign-note ext-meta" + (kind ? " sign-" + kind : ""); };
      btn.addEventListener("click", () => envelopeRevoke(e, signers, listing, btn, line, say));
      line.append(btn, note);
    } else {
      const cmd = "levain envelope kill " + sv(e.id);
      const code = el("code", "consent-code", cmd);
      btn.dataset.label = btn.textContent;
      btn.title = "copy the revoke command; run it in a terminal. This page signs nothing here";
      btn.addEventListener("click", () => copyText(cmd, code, btn));
      line.append(btn, code);
    }
    row.appendChild(line);
    return row;
  }
  function envelopeRevoke(e, signers, listing, btn, host, say) {
    if (signBusy) { say(SIGN_BUSY.replace("signed", "revoked"), "bad"); return; }
    signBusy++;
    btn.disabled = true;
    const base = "/cockpit/envelope/" + encodeURIComponent(e.id) + "/";
    const slot = host.closest(".envelope-slot");
    let sent = false, revoked = false;
    signWho({ signers: signers, origin: listing.origin, credentials: listing.credentials }, host, say).then((who) => {
      if (!who) { say("nothing was revoked"); return; }
      say("asking for the revoke to sign…");
      return signCall(base + "revoke-challenge", { signer_fp: who.fp, instance: e.instance }).then((c) => {
        if (!c.ok) { say("not revoked: " + signWhy(c), "bad"); return; }
        // the server builds the message itself; one that is not exactly a revoke of this envelope is not put to the passkey
        if (!envelopeLineOk(c.body, e.id) || typeof c.body.challenge !== "string") {
          say("not revoked: the text the server sent is not a revoke of this envelope, so nothing was signed", "bad");
          return;
        }
        return envelopeChallenge(c.body.line).then((want) => {
          if (signB64u(want) !== c.body.challenge) {
            say("not revoked: the challenge the server sent is not over the text it shows, so nothing was signed", "bad");
            return;
          }
          // the exact text the passkey signs, on the screen before the prompt opens
          const row = host.closest(".envelope-row");
          const old = row && row.querySelector(".envelope-line");
          if (old) old.remove();
          const shown = el("pre", "envelope-line", c.body.line);
          host.parentNode.insertBefore(shown, host);
          shown.scrollIntoView({ block: "nearest" });
          say("confirm with your passkey to end this envelope: it signs the text above…");
          const pk = { challenge: want, rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
          return signAsk(pk, c.body.rp_id, who.fp, who.rawId).then((cred) => {
            if (!cred || !cred.response) { say("not revoked: the passkey gave nothing to send; nothing was revoked", "bad"); return; }
            say("verifying the signature…");
            sent = true;
            return signCall(base + "revoke", {
              authenticator_data: signB64u(cred.response.authenticatorData), client_data_json: signB64u(cred.response.clientDataJSON),
              signature: signB64u(cred.response.signature) }).then((r) => {
              const refused = !r.ok && r.status >= 400 && r.status < 500 && r.body && typeof r.body.error === "string";
              if (!r.ok && !refused) {
                envelopeSaid = "result unknown for " + e.id + " (" + signWhy(r) + "): the envelopes were read again to show whether it is still live";
                say(envelopeSaid, "bad");
                return;
              }
              if (!r.ok) { envelopeSaid = "not revoked " + e.id + ": " + signWhy(r); say(envelopeSaid, "bad"); return; }
              signKeep(c.body.rp_id, who.fp, cred);
              revoked = true;
              envelopeSaid = "Revoked " + e.id.slice(0, 11) + ". Work it authorised stops at its next step.";
              say(envelopeSaid, "ok");
            });
          }, (err) => say(signDeclined(err, "nothing was revoked"), "bad"));
        });
      });
    }).catch((err) => {
      const name = (err && err.name) || "error";
      if (sent) envelopeSaid = "result unknown for " + e.id + " (the page failed: " + name + "): the envelopes were read again to show where it stands";
      say(sent ? envelopeSaid : "the page failed (" + name + "); nothing was revoked", "bad");
    }).finally(() => {
      signBusy--;
      btn.disabled = false;
      if (!sent && !revoked) return;
      setTimeout(() => {
        const live = slot && slot.isConnected ? slot : document.querySelector(".envelope-slot");
        if (live) envelopeFill(live);
      }, 1500);
    });
  }
  function envelopeSlot() {
    const slot = el("div", "envelope-slot");
    envelopeFill(slot);
    return slot;
  }
  let envelopeFillSeq = 0;     // the newest fill wins
  // what the last revoke said, taken once: whichever fill draws next shows it and clears it, so it is never repeated stale
  function envelopeSaidOnce() {
    const said = envelopeSaid ? [el("div", "consent-note ext-meta envelope-said", envelopeSaid)] : [];
    envelopeSaid = "";
    return said;
  }
  // Every fill that answers draws the heading and marks the slot filled (the phone jump waits on it): a failed read (no
  // token, no route, a fault) says so under the heading, never leaves the slot blank.
  function envelopeFill(slot) {
    const n = ++envelopeFillSeq;
    const unavailable = () => {
      if (n !== envelopeFillSeq) return;
      slot.replaceChildren(el("div", "ext-grouphead", "Envelopes"), ...envelopeSaidOnce(),
        el("div", "consent-note ext-meta envelope-unavailable", "The envelopes cannot be read here now"));
      slot.dataset.filled = "1";
    };
    envelopeFetch().then((b) => {
      if (n !== envelopeFillSeq) return;
      if (!b) { unavailable(); return; }
      const open = b.envelopes.filter((e) => e && ENVELOPE_REVOCABLE.includes(e.state));
      const ended = b.envelopes.length - open.length;
      const said = envelopeSaidOnce();
      const why = typeof b.note === "string" && b.note ? [el("div", "consent-note ext-meta envelope-why", b.note)] : [];
      slot.replaceChildren(el("div", "ext-grouphead", "Envelopes"), ...said, ...why,
        ...(open.length ? open.map((e) => envelopeCard(e, b)) : [el("div", "consent-note ext-meta envelope-empty", "No live envelopes")]),
        ...(ended ? [el("div", "consent-note ext-meta", ended + " ended")] : []));
      slot.dataset.filled = "1";
    }).catch(unavailable);
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
    return openSheet("/sign/dispatch/" + encodeURIComponent(id), version, choice, "dispatch job", undefined, { kind: "dispatch", id: id });
  }
  // OS-2: the sheet over a desk drop (/sign/drop): words, and optionally an envelope the passkey signs there
  function openDropSheet(words) {
    // the typed words reach the sheet through this tab's session storage (same origin, no length limit, never in the address);
    // the sheet reads them once and removes them
    // (set after openSheet, which closes any sheet first and so clears the key; the frame loads later than this returns;
    // closeSignSheet and an edit of the box clear it again, so a sheet that never reads it leaves nothing behind)
    const got = openSheet("/sign/drop", "", "", "desk drop", "Add an envelope", { kind: "drop", id: "" });
    if (got && typeof words === "string" && words) { try { window.sessionStorage.setItem(DROP_PREFILL_KEY, words); } catch (e) { /* the sheet opens empty */ } }
    return got;
  }
  function dropHandedReset() {
    const host = document.getElementById("ck-drop");
    if (host && host._drop) host._drop.handed = undefined;
  }
  // A drop the sheet filed was accepted. The handoff is spent whatever happens here (so no later delivery can match it), and the
  // box lets go only when a handoff was outstanding and the box still holds exactly the words the server accepted; the request
  // state moves on as it does after a plain Send, so a retry cannot send the cleared words again.
  function dropHandedClear(delivered) {
    const host = document.getElementById("ck-drop");
    const ui = host && host._drop;
    if (!ui) return;
    const outstanding = typeof ui.handed === "string";
    ui.handed = undefined;
    const box = ui.words;
    if (!outstanding || !box || typeof delivered !== "string" || !delivered.trim() || dropBoxText(box).trim() !== delivered.trim()) return;
    if (ui.req && ui.req.words.trim() === delivered.trim()) {
      dropForget(ui.req);
      ui.req = null;
      ui.fromKeep = false;
    }
    setDropBoxText(box, "");
    dropPrefillClear();
    if (typeof ui.resume === "function") ui.resume();
    if (typeof ui.earlierLine === "function") ui.earlierLine();
  }
  function dropPrefillClear() { try { window.sessionStorage.removeItem(DROP_PREFILL_KEY); } catch (e) { /* nothing kept */ } }
  // ---- the desk drop: a card on the home page (Phill 10-30), drawn under the Needs-you row, outside #ck-hero. An inline
  // composer, not a button that opens a sheet. Send posts through /cockpit/coord/drop, the route the /sign/drop sheet posts to,
  // with the same request (words, outbox_id, sent_at, store_id) and the same kept-before-posted rule; an envelope needs the
  // passkey, which signs on the sheet only, so "Add an envelope" opens the sheet with the words.
  const DROP_PREFILL_KEY = "levain-drop-prefill";
  // one record per outbox id under the sheet's own prefix (a test holds both derivations equal), so a new drop never overwrites
  // one whose answer is unknown
  const dropPendKey = () => "levain-drop-pending:" + window.location.hostname;
  const dropPendKeyOf = (id) => dropPendKey() + ":" + id;
  // ---- the hero Drop box is a contenteditable div (role=textbox), not a form control (Phill 11-06): Safari's AutoFill kept
  // offering cards and contacts on the live textarea whatever its attributes said, and a div has no field kind to classify.
  // Everything that read or wrote its `.value` goes through these helpers, in this one place.
  // The words in the box as one string: line breaks (a <br>, a div per Enter) are "\n", a lone <br> left by deleting everything is
  // nothing, a no-break space is a space, and trailing newlines are dropped.
  // ONE normaliser for both sides of every "is this the retained request's words" comparison (a kept request from an earlier
  // build may end in newlines or hold no-break spaces; the box can only ever show the normalised form)
  function dropNorm(s) { return String(s == null ? "" : s).replace(/\r\n?/g, "\n").replace(/\u00a0/g, " ").replace(/\n+$/, ""); }
  const dropSame = (a, b) => dropNorm(a) === dropNorm(b);
  // read by walking the DOM, never innerText: innerText is layout-dependent (a hidden host gives "" in WebKit and the bare
  // textContent, with no line breaks, in Chrome). A <br> is a newline; a block child (a div per Enter) starts and ends a line.
  function dropBoxText(box) {
    if (!box || !box.textContent) return "";
    let out = "";
    const nl = () => { if (out && out[out.length - 1] !== "\n") out += "\n"; };
    const walk = (n) => {
      for (let c = n.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 3) out += c.nodeValue;
        else if (c.nodeType === 1) {
          if (c.tagName === "BR") { out += "\n"; continue; }
          const block = /^(DIV|P|LI|UL|OL|BLOCKQUOTE|PRE|H[1-6])$/.test(c.tagName);
          if (block) nl();
          walk(c);
          if (block) nl();
        }
      }
    };
    walk(box);
    return dropNorm(out);
  }
  function setDropBoxText(box, text) { if (box) box.textContent = typeof text === "string" ? text : ""; }
  // the box takes no typing: contenteditable off and aria-disabled (a div has no `disabled`)
  function dropBoxOff(box, off) {
    box.setAttribute("contenteditable", off ? "false" : "true");
    if (off) box.setAttribute("aria-disabled", "true"); else box.removeAttribute("aria-disabled");
  }
  function dropBoxIsOff(box) { return box.getAttribute("aria-disabled") === "true"; }
  function dropCaretEnd(box) {
    const sel = window.getSelection && window.getSelection();
    if (!sel) return;
    const r = document.createRange();
    r.selectNodeContents(box);
    r.collapse(false);
    sel.removeAllRanges();
    sel.addRange(r);
  }
  // plain text in at the caret (or at `at`, a Range): never markup
  function dropInsertPlain(box, text, at) {
    const t = String(text || "").replace(/\r\n?/g, "\n");
    if (!t || dropBoxIsOff(box)) return;      // a box that is off (a send in flight, no desk) takes nothing
    const sel = window.getSelection && window.getSelection();
    if (at && sel) { sel.removeAllRanges(); sel.addRange(at); }
    // the selection must sit wholly inside the box (both ends and their common ancestor), else the text goes at the box's end:
    // never into another field, never deleting DOM outside the composer
    if (sel) {
      const inside = (n) => !!n && box.contains(n);
      let r0 = null;
      try { r0 = sel.rangeCount ? sel.getRangeAt(0) : null; } catch (e) { r0 = null; }
      if (!(r0 && inside(r0.startContainer) && inside(r0.endContainer) && inside(r0.commonAncestorContainer))) dropCaretEnd(box);
    }
    let ok = false;
    try { ok = document.execCommand("insertText", false, t); } catch (e) { ok = false; }
    if (!ok && sel) {
      if (!sel.rangeCount) dropCaretEnd(box);
      const r = sel.getRangeAt(0);
      r.deleteContents();
      const node = document.createTextNode(t);
      r.insertNode(node);
      r.setStartAfter(node);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
    }
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function dropBoxBuild() {
    const box = el("div", "drop-words");
    box.id = "ck-drop-words";
    box.setAttribute("contenteditable", "true");      // "true", the markup the 11-06 test found clean; plaintext-only was never tried on the phone
    box.setAttribute("role", "textbox");
    box.setAttribute("aria-multiline", "true");
    box.setAttribute("aria-label", "your words for the desk");
    box.setAttribute("aria-placeholder", "words for the desk");
    box.dataset.placeholder = "words for the desk";
    for (const [k, v] of [["autocapitalize", "sentences"], ["autocorrect", "on"], ["spellcheck", "true"], ["inputmode", "text"], ["enterkeyhint", "enter"]]) box.setAttribute(k, v);
    // a cleared box keeps a lone <br> in some browsers, which would hold the placeholder away: empty means empty
    // (only after a deletion: Enter in an empty box leaves <br>s too, and those are the newline the user just typed)
    box.addEventListener("input", (ev) => { if (!box.textContent && box.firstChild && ev && typeof ev.inputType === "string" && ev.inputType.indexOf("delete") === 0) box.replaceChildren(); });
    box.addEventListener("paste", (ev) => {
      ev.preventDefault();
      if (dropBoxIsOff(box)) return;
      const cd = ev.clipboardData;
      dropInsertPlain(box, cd && typeof cd.getData === "function" ? cd.getData("text/plain") : "");
    });
    box.addEventListener("drop", (ev) => {
      ev.preventDefault();
      if (dropBoxIsOff(box)) return;
      const dt = ev.dataTransfer;
      const text = dt && typeof dt.getData === "function" ? dt.getData("text/plain") : "";
      let at = null;
      try { if (document.caretRangeFromPoint) { at = document.caretRangeFromPoint(ev.clientX, ev.clientY); if (at && !box.contains(at.startContainer)) at = null; } } catch (e) { at = null; }
      dropInsertPlain(box, text, at);
    });
    // no rich text by any route (the iOS edit menu's bold and italic, a style paste)
    box.addEventListener("beforeinput", (ev) => { if (typeof ev.inputType === "string" && /^(format|insertOrderedList|insertUnorderedList|insertHorizontalRule|insertLink)/.test(ev.inputType)) ev.preventDefault(); });
    return box;
  }
  // every kept request, newest first (a record an earlier build kept under the bare key counts too)
  const dropKeptAll = () => {
    const seen = new Map();
    let n = 0;
    try { n = window.localStorage.length; } catch (e) { return []; }
    const pre = dropPendKey();
    for (let i = 0; i < n; i++) {
      try {
        const k = window.localStorage.key(i);
        if (k !== pre && !(k && k.indexOf(pre + ":") === 0)) continue;
        const v = JSON.parse(window.localStorage.getItem(k) || "null");      // one malformed value is skipped (left in place), never the scan
        if (v && typeof v === "object" && typeof v.outbox_id === "string" && typeof v.words === "string" && !seen.has(v.outbox_id)) seen.set(v.outbox_id, v);   // the bare key and the :id key of one request count once
      } catch (e) { /* this entry only */ }
    }
    return Array.from(seen.values()).sort((a, b) => String(b.sent_at || "").localeCompare(String(a.sent_at || "")));
  };
  // true when the request is kept; false when storage refuses (quota, disabled, a private window): the send goes on, and the
  // request is held in the page (ui.req) for as long as it lives
  const dropKeep = (req) => { try { window.localStorage.setItem(dropPendKeyOf(req.outbox_id), JSON.stringify(req)); return true; } catch (e) { return false; } };
  const dropForget = (req) => {
    try { window.localStorage.removeItem(dropPendKeyOf(req.outbox_id)); } catch (e) { /* nothing kept */ }
    try {
      const old = JSON.parse(window.localStorage.getItem(dropPendKey()) || "null");
      if (old && old.outbox_id === req.outbox_id) window.localStorage.removeItem(dropPendKey());
    } catch (e) { /* a malformed legacy value is left alone */ }
  };
  // what the sheet says of the envelope the reply carries
  const dropHow = (req, b) => {
    if (!req.envelope) return { text: "words only", ok: true };
    const e = b && b.envelope;
    return e && e.taken ? { text: "envelope " + String(e.envelope_id || "") + " taken", ok: true }
      : { text: "envelope NOT taken: " + String((e && e.refused) || "no envelope in the reply"), ok: false };
  };
  const dropStamp = () => new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000Z");   // the store's fixed-width UTC form
  const dropOutboxId = () => {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return "ph-" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  };
  // The panel id a provider registers to supply the desk's side of the conversation (its replies and receipts). The drop
  // card hosts that panel's rows in its own thread, so the page draws it nowhere else (and its compose box never mounts).
  const DROP_THREAD_PANEL = "desk-thread";
  const DROP_RECENT_SHOWN = 8;
  // the read goes through the shared bounded read queue and is held for a moment: rapid redraws share one read. Resolves to
  // {body} or {why} (one line saying what is missing), never to nothing.
  // A read that failed is held for DROP_FAIL_TTL_MS only. A render older than that drops the cache (renderKe), a drop filed on
  // the sheet drops it (dropReload), and dropSeq / dropDrawn keep a late older answer from redrawing over a newer one.
  const DROP_FAIL_TTL_MS = 3000;
  let dropRead = null, dropAt = 0, dropTtl = PROPOSAL_TTL_MS, dropSeq = 0, dropDrawn = 0;
  function dropFetch(headers, fresh) {
    if (!fresh && dropRead && Date.now() - dropAt < dropTtl) return dropRead;
    dropAt = Date.now();
    dropTtl = PROPOSAL_TTL_MS;
    const failed = (g) => { if (dropRead === g) dropTtl = DROP_FAIL_TTL_MS; };
    const got = consentQueued(() => fetchBounded("/cockpit/coord/drops.json", { cache: "no-store", headers: headers },
      (res) => {
        if (res.status === 404) return { why: "This cockpit has no desk store to send to (no signing is set up here)." };
        if (res.status === 401 || res.status === 403) return { why: "Sign in to send: this cockpit refused this browser's token.", auth: true };
        if (!res.ok) return { why: "The desk could not be read (HTTP " + res.status + ")." };
        return res.json().then((b) => (b && Array.isArray(b.coord_drops) ? { body: b } : { why: "The desk's answer was not a drop list." }),
                               () => ({ why: "The desk's answer could not be read." }));
      },
      () => ({ why: "The desk could not be reached." })), "the drops could not be read").then((r) => {
        const out = r && (r.body || r.why) ? r : { why: ((r && r.error) || "The desk could not be read") + "." };
        if (!out.body) failed(got);      // a failed read is asked again within seconds, not held for the TTL
        return out;
      }, () => { failed(got); return { why: "The desk could not be read." }; });
    dropRead = got;
    return got;
  }
  const dropText = (x) => (typeof x === "string" ? x : "");
  const DROP_PREVIEW_CHARS = 100;
  const dropPreview = (t) => {
    const first = t.split("\n")[0].trim();
    return first.length > DROP_PREVIEW_CHARS ? first.slice(0, DROP_PREVIEW_CHARS - 1) + "…" : first;
  };
  const dropInstant = (iso) => { const ms = typeof iso === "string" ? Date.parse(iso) : NaN; return Number.isFinite(ms) ? ms : null; };
  // local time, short: "MM-DD HH:MM"; an unreadable instant draws nothing
  // "today 14:05", "yesterday 14:05", else weekday and date ("Thu 10-29 14:05"), so a time is never a bare clock on an unnamed day
  const DROP_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const dropZ = (n) => String(n).padStart(2, "0");
  const dropWhenOf = (ms, now) => {
    if (ms === null) return "";
    const d = new Date(ms), n = new Date(typeof now === "number" ? now : Date.now());
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const ago = Math.round((day(n) - day(d)) / 86400000);
    const clock = dropZ(d.getHours()) + ":" + dropZ(d.getMinutes());
    if (ago === 0) return "today " + clock;
    if (ago === 1) return "yesterday " + clock;
    return DROP_DAYS[d.getDay()] + " " + dropZ(d.getMonth() + 1) + "-" + dropZ(d.getDate()) + " " + clock;
  };
  // the whole local instant, for the expanded row (a touch screen has no hover title)
  const dropSentOf = (ms) => {
    if (ms === null) return "";
    const d = new Date(ms);
    return DROP_DAYS[d.getDay()] + " " + d.getFullYear() + "-" + dropZ(d.getMonth() + 1) + "-" + dropZ(d.getDate()) + " "
      + dropZ(d.getHours()) + ":" + dropZ(d.getMinutes()) + ":" + dropZ(d.getSeconds());
  };
  // a short stable key for a desk row that carries no id of its own (never its position, which shifts as rows arrive)
  const dropHash = (t) => { let h = 5381; for (let i = 0; i < t.length; i++) h = ((h * 33) ^ t.charCodeAt(i)) >>> 0; return h.toString(36); };
  // one thread item from one of his drops; null for a malformed entry (skipped, never the whole list)
  function dropItem(d) {
    if (!d || typeof d !== "object") return null;
    const words = dropText(d.words);
    const stages = Array.isArray(d.receipts) ? d.receipts.map((r) => r && r.stage).filter((x) => typeof x === "string") : [];
    const how = d.envelope_id ? "signed" : d.envelope_refused ? "envelope not taken" : "unsigned";
    return { key: "drop:" + dropText(d.drop_id), dropId: dropText(d.drop_id), mine: true, who: "you", at: dropInstant(d.sent_at), iso: dropText(d.sent_at),
             preview: dropPreview(words), full: words, meta: how + " · " + (stages.join(", ") || "no receipt yet") };
  }
  // one thread item from one of the desk panel's lines (cockpit_view.js keeps the row's own title, body, instant and source on it)
  function deskItem(l) {
    if (!l || typeof l !== "object") return null;
    const t = l.thread && typeof l.thread === "object" ? l.thread : {};      // the mapper sets it on every line of this panel
    const full = dropText(t.body) || dropText(t.title);
    if (!full) return null;      // nothing to read: skipped, never a blank row
    const prev = dropText(t.title) || full;
    const src = dropText(t.source).trim();
    // a row an older send of his own filed through the desk's thread is his, whatever case or detail its source carries
    const his = src.toLowerCase().indexOf("phill") === 0;
    return { key: "desk:" + (dropText(l.key) || "h" + dropHash(src + "\n" + dropText(t.at) + "\n" + full)), mine: his, who: his ? "you" : src || "desk",
             at: dropInstant(t.at), iso: dropText(t.at), preview: dropPreview(prev), full: full, meta: "", desk: true };
  }
  // both sources, newest first; an unreadable instant sorts oldest, and equal instants keep the order they arrived in
  function dropThread(drops, view) {
    const items = [];
    for (const d of Array.isArray(drops) ? drops : []) { try { const it = dropItem(d); if (it) items.push(it); } catch (e) { /* skipped */ } }
    const panel = view && view.extra_panels ? view.extra_panels[DROP_THREAD_PANEL] : null;
    let fault = "";
    if (panel && typeof panel === "object") {
      if (panel.error) fault = String(panel.error);
      else (Array.isArray(panel.lines) ? panel.lines : []).forEach((l) => { try { const it = deskItem(l); if (it) items.push(it); } catch (e) { /* skipped */ } });
    }
    // a repeated key is numbered by its own occurrence among rows of that key, so another row arriving never renumbers it
    // the first row of each key keeps it, so a genuine key that ends in "#2" is never taken by a numbered repeat
    const used = new Set(), count = new Map(), repeats = [];
    for (const it of items) { if (used.has(it.key)) repeats.push(it); else used.add(it.key); }
    for (const it of repeats) {
      let n = (count.get(it.key) || 1) + 1;
      while (used.has(it.key + "#" + n)) n++;
      count.set(it.key, n);
      it.key += "#" + n;
      used.add(it.key);
    }
    return { fault: fault, seen: !!panel && typeof panel === "object" && !panel.error, items: items.map((it, i) => [it, i]).sort((a, b) => ((b[0].at === null ? -Infinity : b[0].at) - (a[0].at === null ? -Infinity : a[0].at)) || (a[1] - b[1])).map((x) => x[0]) };
  }
  let dropRowSeq = 0;
  function dropRow(it, ui) {
    const row = el("div", "drop-recent-row" + (it.mine ? " drop-mine" : " drop-desk"));
    row.dataset.dropKey = it.key;
    if (it.dropId) row.dataset.dropId = it.dropId;      // the real drop id, whatever suffix the row's key carries
    const head = el("button", "drop-row-head");
    head.type = "button";
    head.dataset.dropKey = it.key;
    const whenText = dropWhenOf(it.at);
    const when = el("span", "drop-when ext-meta", whenText);
    if (it.iso) when.title = it.iso;
    const first = el("span", "drop-words-first", it.preview);
    head.append(el("span", "drop-who", it.who), when, first);
    if (it.meta) head.appendChild(el("span", "drop-state ext-meta", it.meta));
    // the name keeps its separators: the visible spans run together for a reader that joins them
    head.setAttribute("aria-label", it.who + ", " + whenText + ": " + it.preview + (it.meta ? " (" + it.meta + ")" : ""));
    // the whole text is always in the page, hidden until opened, so the button can name it
    const full = el("div", "drop-full");
    full.id = "drop-full-" + (++dropRowSeq);
    full.appendChild(el("div", "drop-full-text", it.full));
    const sent = dropSentOf(it.at);
    if (sent) full.appendChild(el("p", "drop-sent ext-meta", "sent " + sent));
    head.setAttribute("aria-controls", full.id);
    const set = (on) => {
      head.setAttribute("aria-expanded", on ? "true" : "false");
      row.classList.toggle("is-open", on);
      full.hidden = !on;
      first.hidden = on;      // the opened text is not read twice
    };
    head.addEventListener("click", () => { const on = !ui.open.has(it.key); if (on) ui.open.add(it.key); else ui.open.delete(it.key); set(on); });
    row.append(head, full);
    set(ui.open.has(it.key));
    return row;
  }
  function dropHeroBuild(host) {
    const ui = { req: null, storeId: null, storeRead: false, can: false, shown: false, working: false, drops: [], open: new Set(), seen: null, q: "", sig: null, view: null, read: null };
    const head = el("div", "needs-headline");
    head.appendChild(el("h3", null, "Drop to the desk"));
    ui.say = el("span", "needs-gloss drop-note", "");
    head.appendChild(ui.say);
    // a contenteditable div, not a textarea: iOS Safari classified the textarea as a card/contact field from its page context
    // (the pairing code box beside it) whatever its attributes said; a div with role=textbox is no form control (dropBoxBuild)
    const words = dropBoxBuild();
    words.addEventListener("input", () => { dropPrefillClear(); ui.handed = undefined; });      // words changed since a handoff: that handoff is stale
    const acts = el("div", "needs-actions drop-actions");
    const send = el("button", "needs-btn needs-primary", "Send");
    send.type = "button";
    send.dataset.coordDrop = "send";
    const env = el("button", "needs-btn", "Add an envelope");
    env.type = "button";
    env.dataset.coordDrop = "envelope";
    env.title = "sign an envelope with your passkey: opens the drop sheet with these words";
    acts.append(send, env);
    ui.receipt = el("p", "drop-receipt ext-meta", "");
    ui.receipt.setAttribute("role", "status");
    ui.earlier = el("p", "drop-earlier ext-meta", "");
    ui.recent = el("div", "drop-recent");
    ui.recent.hidden = true;
    ui.fault = el("p", "drop-fault ext-meta bad", "");
    ui.fault.hidden = true;
    ui.filter = el("input", "drop-filter");
    ui.filter.type = "search";
    ui.filter.placeholder = "filter the thread";
    ui.filter.setAttribute("aria-label", "filter the thread");
    ui.filter.hidden = true;
    ui.list = el("div", "drop-list");
    ui.moreHost = el("div", "drop-more-host");
    ui.olderNote = el("p", "drop-older-note ext-meta", "Older drops are not shown here.");      // the listing reads a bounded number of the newest
    ui.olderNote.hidden = true;
    ui.sub = el("p", "drop-sub ext-meta", "Your drops and the desk's replies");
    ui.live = el("p", "drop-live");      // announces a new reply; never holds anything else
    ui.live.setAttribute("role", "status");
    ui.live.setAttribute("aria-live", "polite");
    ui.more = el("button", "needs-history drop-more", "");      // one node for good: a redraw changes its words, so it keeps its focus
    ui.more.type = "button";
    ui.more.hidden = true;
    ui.more.addEventListener("click", () => { ui.shown = !ui.shown; dropThreadDraw(host); });
    ui.moreHost.appendChild(ui.more);
    ui.recent.append(ui.sub, ui.fault, ui.filter, ui.list, ui.olderNote, ui.moreHost, ui.live);
    ui.filter.addEventListener("input", () => { ui.q = ui.filter.value; dropThreadDraw(host); });
    const main = el("div", "drop-main");      // the composer is one left-column element, so the card's grid has two children whatever the list's length
    main.append(head, words, acts, ui.receipt, ui.earlier);
    host.replaceChildren(main, ui.recent);
    Object.assign(ui, { words, send, env });
    // what is still unconfirmed besides the request in the box: each stays retryable through the same Send, one at a time,
    // newest first (the next one comes into the box when the one in it is answered)
    const earlierLine = () => {
      const mine = ui.req ? ui.req.outbox_id : null;
      const n = dropKeptAll().filter((r) => r.outbox_id !== mine).length;
      const inBox = ui.fromKeep && ui.req && dropSame(ui.req.words, dropBoxText(words)) ? "The drop in the box was not confirmed earlier: send it again, unchanged, to find out. " : "";
      ui.earlier.textContent = inBox + (n ? n + " earlier drop" + (n > 1 ? "s" : "") + " not confirmed." : "");
    };
    const resume = (msg) => {
      const next = dropKeptAll()[0];
      if (!next) return false;
      setDropBoxText(words, next.words);
      ui.req = next;
      ui.fromKeep = true;
      if (msg) paint(msg, true);
      return true;
    };
    ui.earlierLine = earlierLine;
    ui.resume = resume;
    const paint = (msg, bad) => { ui.receipt.textContent = msg; ui.receipt.classList.toggle("bad", !!bad); };
    const lock = (on) => { ui.working = on; send.disabled = env.disabled = on || !ui.can; dropBoxOff(words, dropWordsOff(words, on, ui.can)); };
    ui.paint = paint;
    ui.lock = lock;
    // (the handoff is marked after the sheet opens: opening closes any sheet first, and a close ends a handoff)
    env.addEventListener("click", () => { if (ui.can) { const got = openDropSheet(dropBoxText(words)); if (got) ui.handed = dropBoxText(words); } });
    send.addEventListener("click", () => {
      if (ui.working || !ui.can) return;
      if (ui.reading) { ui.reading.then(() => send.click()); return; }      // the read that names the store is still out
      const w = dropBoxText(words);
      if (!w.trim()) { paint("Type your words first.", true); words.focus(); return; }
      const t = window.LevainCockpitTransport;
      const headers = Object.assign({}, t && typeof t.headers === "function" ? t.headers() : {}, { "Content-Type": "application/json" });
      // the retained request (words, id, time, store, and an envelope a sheet attached) goes again as it was, so a send whose
      // answer was lost files once; words edited since are a new drop with a new id
      // (a new drop is kept under its own id: the one it replaces in the box stays kept, never overwritten)
      if (!(ui.req && dropSame(ui.req.words, w))) ui.fromKeep = false;
      const req = ui.req && dropSame(ui.req.words, w) ? ui.req : { words: w, outbox_id: dropOutboxId(), sent_at: dropStamp(), store_id: ui.storeId };
      ui.req = req;
      const kept = dropKeep(req);
      lock(true);
      paint("Sending…", false);
      const unsure = (why) => {
        paint("Not sure the drop was delivered (" + why + "). Send again, unchanged, to find out."
          + (kept ? "" : " This browser cannot keep it, so reloading before it is confirmed may send it twice."), true);
        earlierLine();
        lock(false);
        // the drop may have made the store: new words must name it, so a send waits for this read (ui.reading)
        const clear = () => { if (ui.reading === reading) ui.reading = null; };      // on both outcomes: a read that fails never leaves Send waiting
        const reading = ui.reading = Promise.resolve().then(() => dropRefresh(host, true)).then(clear, clear);
      };
      fetchBounded("/cockpit/coord/drop", { method: "POST", cache: "no-store", headers: headers, body: JSON.stringify(req) },
        (res) => res.json().then((b) => ({ status: res.status, ok: res.ok, body: b && typeof b === "object" ? b : {} }),
                                 () => ({ status: res.status, ok: res.ok, body: {} })),
        () => ({ status: 0, ok: false, body: {} })).then((a) => {
        if (a.ok) {
          dropForget(req);
          ui.req = null;
          if (typeof a.body.store_id === "string") ui.storeId = a.body.store_id;
          const d = a.body.drop || {};
          const first = w.split("\n")[0].trim().slice(0, 80) + (w.length > 80 || w.includes("\n") ? "…" : "");
          const how = dropHow(req, a.body);
          setDropBoxText(words, "");
          paint((a.body.created === false ? "Already delivered: " : "Sent to the desk: ") + "“" + first + "” · " + how.text + " · " + String(d.drop_id || ""), !how.ok);
          resume();      // an earlier unconfirmed drop comes into the box, to send unchanged
          earlierLine();
          lock(false);
          words.focus();
          dropRefresh(host, true);
        } else if (a.status >= 400 && a.status < 500) {
          dropForget(req);     // refused: nothing was delivered with this request; the words stay
          ui.req = null;
          const m = typeof a.body.message === "string" && a.body.message ? a.body.message.replace(/[.\s]+$/, "") : "refused (HTTP " + a.status + ")";
          paint("The drop was not delivered: " + m + ".", true);
          earlierLine();
          lock(false);
        } else {
          unsure(a.status ? "HTTP " + a.status : "no reply");
        }
      });
    });
    // a drop from an earlier visit whose answer never came: the newest is in the box, to send unchanged; any older stays kept
    resume("A drop from earlier was not confirmed. Send it again, unchanged, to find out.");
    earlierLine();
    host._drop = ui;
    host.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey) && !ev.isComposing && ev.target === words) { ev.preventDefault(); send.click(); } });      // after the commit above, so a failed build leaves none
    return ui;
  }
  // one draw of the drop card from one read; the box and its words are never rebuilt under the reader
  // The box is off while a send is in flight, and when the desk cannot be written to AND nobody is typing: a read that is
  // not clean (404, 401/403, 5xx, a network blip, a poll) must not take the keyboard from a box he is in or drop a draft he holds.
  // Send and the envelope button still follow `can`, so a blip never lets a send through.
  function dropWordsOff(words, working, can) {
    if (working) return true;
    if (can) return false;
    return !(document.activeElement === words || dropBoxText(words).trim() !== "");
  }
  function dropHeroDraw(host, r) {
    const ui = host._drop;
    const body = r && r.body ? r.body : null;
    ui.storeRead = !!body && body.coord_store_read === true;
    ui.storeId = body && typeof body.coord_store_id === "string" ? body.coord_store_id : null;
    ui.can = ui.storeRead;
    const note = !body ? (r && r.why) || "The desk could not be read."
      : !ui.storeRead ? (typeof body.coord_note === "string" && body.coord_note ? body.coord_note : "Still reading which Levain answers here; reload.")
      : typeof body.coord_note === "string" ? body.coord_note : "";
    ui.note = note;
    ui.say.textContent = note;
    ui.say.classList.toggle("bad", !ui.can);
    ui.send.disabled = ui.env.disabled = ui.working || !ui.can;
    dropBoxOff(ui.words, dropWordsOff(ui.words, ui.working, ui.can));
    host.classList.toggle("drop-off", !ui.can);
    // A transient failure (network, 5xx, no store read) leaves the rows already drawn; the note above says why. A refused or
    // missing token leaves nothing of the thread: neither his drops nor the desk's cached rows outlive the authority to read them.
    if (!body && r && r.auth) { ui.drops = []; ui.older = false; ui.denied = true; }
    else if (body && ui.storeRead) { ui.drops = Array.isArray(body.coord_drops) ? body.coord_drops : []; ui.older = body.coord_drops_more === true; ui.denied = false; }
    else if (ui.drops.length) { ui.say.textContent += " Showing the drops last read."; }
    ui.read = r;
    dropThreadDraw(host);
    // words a card handed over before this first read landed (see composeDrop) go in now, or are refused with the reason
    const held = ui.pending;
    ui.pending = null;
    if (held) for (const w of held) composeDrop(w);
  }
  // the thread: his drops and the desk's replies in one list. Only the list is replaced: the filter box keeps its focus and the
  // composer is never touched. A redraw that would change nothing leaves the DOM alone, so selected text stays selected.
  function dropThreadDraw(host) {
    const ui = host._drop;
    const t = dropThread(ui.drops, ui.denied ? null : ui.view);
    // a filter is only ever set while its box can be seen: with the box hidden it could not be cleared
    if (t.items.length < 2 && ui.q) { ui.q = ""; ui.filter.value = ""; }
    const q = ui.q.trim().toLowerCase();
    const now = Date.now();
    const hit = (it) => (it.who + "\n" + it.preview + "\n" + it.full + "\n" + it.meta + "\n" + dropWhenOf(it.at, now) + "\n" + dropSentOf(it.at)).toLowerCase().indexOf(q) >= 0;
    const rows = q ? t.items.filter(hit) : t.items;
    const shown = ui.shown || q ? rows : rows.slice(0, DROP_RECENT_SHOWN);
    announceDesk(ui, t);
    const today = new Date(now);      // the day is in the signature: "today" and "yesterday" change words at midnight
    const sig = JSON.stringify([today.getFullYear() + "-" + dropZ(today.getMonth() + 1) + "-" + dropZ(today.getDate()), ui.older === true, t.fault, ui.shown, q, t.items.length > 1, shown.map((it) => [it.key, it.at, it.who, it.preview, it.full, it.meta]), rows.length]);
    if (sig === ui.sig) return;
    ui.sig = sig;
    const key = document.activeElement && ui.list.contains(document.activeElement) ? document.activeElement.dataset.dropKey : null;
    ui.fault.textContent = t.fault ? "The desk's replies could not be read: " + t.fault : "";
    ui.fault.hidden = !t.fault;
    ui.filter.hidden = t.items.length < 2;
    const scrolled = ui.list.scrollTop;      // a rebuild must not send him back to the top of what he is reading
    ui.list.replaceChildren(...shown.map((it) => dropRow(it, ui)));
    ui.list.scrollTop = scrolled;
    ui.olderNote.hidden = ui.older !== true;
    if (q && !rows.length) ui.list.appendChild(el("p", "drop-none ext-meta", "nothing in the thread matches"));
    ui.more.hidden = !(!q && rows.length > DROP_RECENT_SHOWN);
    ui.more.textContent = ui.shown ? "show fewer" : "show " + (rows.length - DROP_RECENT_SHOWN) + " more";
    ui.recent.hidden = !(t.fault || t.items.length);
    if (key) {
      const again = Array.from(ui.list.querySelectorAll(".drop-row-head")).find((b) => b.dataset.dropKey === key);
      // a row folded away by "show fewer" hands its focus to the button that folded it
      if (again) again.focus(); else if (!ui.more.hidden) ui.more.focus(); else if (!ui.filter.hidden) ui.filter.focus();
    }
  }
  // A desk row not seen before (after the first draw that had the desk's panel) is announced once. Never a refresh with nothing
  // new, never his own rows.
  function announceDesk(ui, t) {
    if (!t.seen) return;
    const keys = t.items.filter((it) => it.desk && !it.mine).map((it) => it.key);
    const first = ui.seen === null;
    const fresh = first ? [] : keys.filter((k) => !ui.seen.has(k));
    ui.seen = new Set([...(ui.seen || []), ...keys]);
    if (!fresh.length) return;
    ui.live.textContent = "";
    if (ui.liveTid) clearTimeout(ui.liveTid);      // one pending announcement at a time
    ui.liveTid = setTimeout(() => { ui.live.textContent = "The desk replied."; }, 30);      // emptied first, so a second reply is spoken again
  }
  function dropRefresh(host, fresh) {
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? t.headers() : {};
    if (!headers["X-Levain-Write-Token"]) {
      dropDrawn = ++dropSeq;      // an authorized read still in flight is now older than this answer, so it is discarded when it lands
      dropRead = null;
      dropHeroDraw(host, { why: "Sign in to send: this browser holds no cockpit token.", auth: true });
      return Promise.resolve();
    }
    const mine = ++dropSeq;
    return dropFetch(headers, fresh).then((r) => {
      if (!host.isConnected || mine < dropDrawn) return;      // a newer ask already drew: this older answer must not redraw over it
      dropDrawn = mine;
      dropHeroDraw(host, r);
    }).catch(() => {});
  }
  // the drop card reads again now (a drop was filed on the sheet, which may have made the store)
  function dropReload() {
    dropRead = null;
    const host = document.getElementById("ck-drop");
    if (host && host._drop) dropRefresh(host, true);
  }
  // drawn on every KE render, whatever the read says: the drop card (under the Needs-you row, outside #ck-hero) is never silently absent
  function renderDropHero(view) {
    const host = document.getElementById("ck-drop");
    if (!host) return;
    for (const old of host.querySelectorAll(":scope > .ck-region-err")) old.remove();
    const fresh = !host._drop;
    if (fresh) dropHeroBuild(host);
    host.hidden = false;
    host._drop.view = view || null;
    if (host._drop.read) dropThreadDraw(host);      // the desk's side may have changed with this view, whatever the drop read does next
    if (dropRead && Date.now() - dropAt >= DROP_FAIL_TTL_MS) dropRead = null;      // a render reads again once the last read is a few seconds old
    dropRefresh(host, false);
  }
  // Talking about a card (or anything else) is a drop: this puts `words` at the end of the drop card's box, the cursor after
  // them, and brings the box into view. Words already typed stay (the new ones go on a new paragraph after them). Posts nothing:
  // the operator sends it with the box's own Send. A page whose drop card is not drawn opens the drop sheet with the words.
  function composeDrop(words) {
    if (typeof words !== "string") return false;
    const host = document.getElementById("ck-drop");
    const ui = host && host._drop;
    if (!ui || host.hidden) return !!openDropSheet(words);
    // a box that cannot take words now says why on its card; words put in a disabled box would look sent-ready and be lost
    // before the first read lands the box does not yet know whether it can take words (it starts off): the words wait for that
    // read, then go in or are refused with its reason, rather than a click on a card being lost in the first moments of the page
    if (!ui.read && !ui.working) { (ui.pending = ui.pending || []).push(words); return true; }
    if (ui.working || !ui.can) {
      ui.say.textContent = ui.working ? "The drop box is busy" : "The drop box is off" + (ui.note ? ": " + ui.note : "");
      ui.say.classList.add("bad");
      return false;
    }
    if (document.getElementById("levain-modal")) closeModal();      // a focus view stands over the box
    const box = ui.words;
    ui.say.textContent = ui.note || ""; ui.say.classList.remove("bad");      // an earlier refusal's line is stale now; the desk's own note stays
    const have = dropBoxText(box);
    setDropBoxText(box, have.trim() ? have.replace(/\s+$/, "") + "\n\n" + words : words);
    dropPrefillClear();      // a handoff to the sheet left earlier is stale now that these words are in the box
    try { box.scrollIntoView({ block: "center" }); } catch (e) { /* scrolling is a convenience */ }
    try { box.focus({ preventScroll: true }); dropCaretEnd(box); } catch (e) { /* focus is a convenience */ }
    return true;
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
    f.append(proposalSlot(), grantSlot(), envelopeSlot(), dispatchSlot());   // the drop is its own card under the Needs-you row (renderDropHero), not a slot here
    return f;
  }

  function appendExternalLines(host, lines, tabInfo) {
    let lastGroup = null;
    // the Decisions panel's proposal cards (see renderExternalBody) follow the live requests, which are on a clock, and come
    // before the decided ones: ahead of the first "Resolved" / "Outcomes" heading, else after the last row
    const owner = host.closest ? host.closest("[data-proposal-slot]") : null;
    // The marker moves to the body the rows are drawn into: in the modal that body has no marked ancestor once it is shown
    // (read from the page, 10-17), and every filter / "show all" repaint empties it, slot included. The slot is due whenever
    // the body holds none right now, never on a flag set once.
    if (owner && owner !== host) host.dataset.proposalSlot = "";
    let slotDue = !!(owner && !host.querySelector(".proposal-slot"));
    const eagerQ = newestAnswered(lines, ANSWERED_ROWS_EAGER);
    // the Decisions card's two tabs (Needs you / Desk owes) when the rows carry a desk section: each row is tagged here, the strip is drawn after
    // (cockpit_view hands over the classification of ALL the filtered rows and the page of the chosen tab: tags align with `lines`)
    const CF = window.LevainCardFormat;
    const dt = tabInfo && tabInfo.dt ? Object.assign({}, tabInfo.dt, { tags: tabInfo.tags })
      : (CF && typeof CF.deskTabs === "function" ? CF.deskTabs(lines) : null);
    const tabbed = !!(dt && dt.show);
    const firstAt = host.children.length;
    let li = -1;
    const mark = (node) => { if (tabbed && node) node.dataset.deskTab = dt.tags[li]; };
    for (const ln of lines) {
      li++;
      const o = ln && typeof ln === "object" ? ln : {};
      // rows that carry a group (a list drawn as a count) come in the kernel's order: a heading where the group changes
      if (typeof o.grp === "string" && o.grp !== lastGroup) {
        lastGroup = o.grp;
        if (slotDue && (o.grp === "Resolved" || o.grp === "Outcomes")) { slotDue = false; host.appendChild(standingSlots()); }
        host.appendChild(el("div", "ext-grouphead", o.grp));
      }
      if (o.consent) { host.appendChild(consentRow(o)); mark(host.lastElementChild); continue; }
      if (o.question) { host.appendChild(questionRow(o, !!o.question && eagerQ.has(o.question))); mark(host.lastElementChild); continue; }
      if (o.note) { host.appendChild(noteRow(o)); mark(host.lastElementChild); continue; }
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
      appendClause(row, o.text != null ? String(o.text) : "", o.md === true);  // per-item expand, like episodes
      if (typeof o.session === "string" && o.session) row.appendChild(sessionTxControl(o.session));
      if (o.cancel && cancelPending) appendRowVerbs(row, buildPendingCancel(o.cancel));
      if (o.review) appendReviewVerb(row, o.review);
      host.appendChild(row);
      mark(row);
    }
    if (slotDue) host.appendChild(standingSlots());
    if (tabbed) drawDeskTabs(host, firstAt, dt, tabInfo && tabInfo.onTab);
  }

  // The Decisions card's in-card tabs: "Needs you (n)" first and the default, "Desk owes (n)" second. A row tagged "both" (a desk row
  // carried past a day) shows under each; an untagged node (a heading, a proposal card) is "you", a heading following its rows' tags.
  function drawDeskTabs(host, firstAt, dt, onTab) {
    const CF = window.LevainCardFormat;
    const choice = () => (CF && typeof CF.getDeskTab === "function" ? CF.getDeskTab() : "you");
    const kids = Array.from(host.children).slice(firstAt);
    let head = null, seen = null;
    const seal = () => { if (head) head.dataset.deskTab = seen.size > 1 || seen.has("both") ? "both" : (Array.from(seen)[0] || "you"); };
    for (const k of kids) {
      if (k.classList.contains("ext-grouphead")) { seal(); head = k; seen = new Set(); continue; }
      if (!k.dataset.deskTab) k.dataset.deskTab = "you";
      if (seen) seen.add(k.dataset.deskTab);
    }
    seal();
    const nav = el("div", "ck-tabs ck-desk-tabs");
    nav.setAttribute("role", "tablist");
    const buttons = [["you", "Needs you (" + dt.you + ")"], ["desk", "Desk owes (" + dt.desk + ")"]].map(([key, text]) => {
      const b = el("button", "ck-tab", text);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.addEventListener("click", () => { if (CF && CF.setDeskTab) CF.setDeskTab(key); if (onTab) onTab(); else apply(); });
      nav.appendChild(b);
      return [key, b];
    });
    const apply = () => {
      for (const [key, b] of buttons) b.setAttribute("aria-selected", key === choice() ? "true" : "false");
      for (const k of kids) k.classList.toggle("ck-dtab-off", k.dataset.deskTab !== "both" && k.dataset.deskTab !== choice());
    };
    host.insertBefore(nav, kids[0] || null);
    apply();
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
      // stranded frozen on "saving…" (intention-280 — the twin of the 247 state-line stranding). A
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
  // keyword-search becomes a roomy surface (search-into-modal, shape (c)), intention verbs
  // and the in-place State edit work exactly as in the grid. It is a PURE projection
  // of `currentView`, rebuilt on every render(), so a verb fired inside it reflects at
  // once and it stays put. A read-only source (no commit) opens a read+search-only
  // modal (NO THEATER — same as the grid panels).

  // A stable identity for a layout entry, so a rebuild on re-render re-locates the
  // SAME panel in the fresh view (the entry objects are new each fetch). Section/config
  // panels are uniquely identified by their write-address (source + heading), so the key
  // OMITS the list-index `ref` for them — a ref shift (a section added/removed above the
  // expanded one between fetches) must NOT spuriously close the modal. Singleton kinds
  // (episodes/intentions/crystals/wraps/…) are unique by kind alone. JSON.stringify + a
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
    needsFocusNow = null;
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
    // the List's focus view counts its header as its "all" chip does (the card's count is the kernel's, without the desk's and later rows)
    const total = p.dataset.listTotal;
    const title = total ? String(entry.title || "").replace(/ \(\d+\)$/, " (" + total + ")") : entry.title || "";
    return { host, zone: p.dataset.zone || "", title, editClass: entry.edit_class || "" };
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
      if (modalKey && modalKey.indexOf("needs:") === 0) lockNeedsScroll();
    }
    // a Needs-you item's detail is a sheet over the page where the reader tapped (bottom sheet on a phone, centred on a desk)
    overlay.classList.toggle("needs-detail", !!modalCustom && !modalCustom.page && String(modalKey || "").indexOf("needs:") === 0);
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
    else if (firstOpen || hadModalFocus) close.focus({ preventScroll: true });
  }

  // Scroll lock for the Needs-you detail: the page is held fixed at the scroll position it had, so the sheet opens over the row that was
  // tapped (never at the top of the page) and the reader gets the same place back at close. overflow:hidden alone does not hold an
  // iPhone's page still behind a sheet.
  let needsLockY = null;
  function lockNeedsScroll() {
    if (needsLockY !== null) return;
    needsLockY = window.scrollY || window.pageYOffset || 0;
    const b = document.body.style;
    b.position = "fixed"; b.top = (-needsLockY) + "px"; b.left = "0"; b.right = "0";
    document.body.classList.add("needs-locked");
  }
  function unlockNeedsScroll() {
    if (needsLockY === null) return;
    const y = needsLockY;
    needsLockY = null;
    const b = document.body.style;
    b.position = ""; b.top = ""; b.left = ""; b.right = "";
    document.body.classList.remove("needs-locked");
    window.scrollTo(0, y);
  }

  function refreshModal() {
    if (!modalKey) return;
    // a Needs-you card keeps its place (and any words typed into it) while the item still waits; it closes when the item is answered
    if (modalCustom) {
      if (modalCustom.page) return;   // an agent page is not tied to a waiting item
      // a live view repaints from its own source and stays open. buildModal on an open modal is a repaint only: it keeps the one overlay (and
      // its one keydown listener), the scroll and the focus, and swaps the body for the `build` the caller returns
      if (modalCustom.live) { buildModal(null); return; }
      const dl = ((currentView && currentView.extra_panels || {}).decisions || {}).lines;
      // the desk's own questions (the "Desk owes" tab) are waiting items too: their focus view stays open while they are
      if (!needsWaiting(needsItems(dl)).concat(deskOwesItems(dl)).some((it) => needsIdOf(it) === modalCustom.id)) closeModal();
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
    phoneJumpRun++;
    pruneModalKinds();
    document.body.classList.remove("modal-open");
    unlockNeedsScroll();
    modalKey = null;
    modalCustom = null;
    needsFocusNow = null;
    modalGlance = false;
    modalPending = null;
    modalAwaiting = false;
    modalGen++;
    // after the view's own state is reset, so a card that fails to draw never leaves the modal half-closed
    if (listCardRedraw) { try { listCardRedraw(); } catch (e) { console.error(e); } }
    let rf = modalReturnFocus;
    modalReturnFocus = null;
    // a re-render of the widgets replaces the tile that opened the view: its successor is found by the panel id
    if (rf && !document.body.contains(rf) && rf.dataset && rf.dataset.ckPanel && rf.classList.contains("w-open")) {
      const was = rf;
      rf = Array.prototype.find.call(document.querySelectorAll(".ck-card.w.w-open"), (n) => n.dataset.ckPanel === was.dataset.ckPanel) || null;
      if (!rf) askTileFocus(was.dataset.ckPanel);   // its successor is not openable yet: it takes focus when it is
    }
    // a repaint of the Work lens replaces the chip that opened the agent page: its successor is found by the node it names
    // (the node AND its task's card: one worker on two tasks has a chip on each, and only the one that opened the page is its successor)
    if (rf && !document.body.contains(rf) && rf.dataset && rf.dataset.node && rf.classList.contains("tv-chip")) {
      const was = rf.closest(".tv-task"), wasTask = was && was.dataset ? was.dataset.task : null;
      rf = wasTask == null ? null : Array.prototype.find.call(document.querySelectorAll(".tv-chip.tv-open"), (n) => {
        const t = n.closest(".tv-task");
        return n.dataset.node === rf.dataset.node && !!t && t.dataset.task === wasTask;
      }) || null;
    }
    // the List card's control is rebuilt by every fold: its successor is the card's control now
    if (rf && !document.body.contains(rf) && rf.dataset && rf.dataset.listMore) {
      rf = document.querySelector('[data-ck-panel="tray"] [data-list-more]') || document.querySelector('[data-ck-panel="tray"] .pexpand');
    }
    // restore focus to the ⤢ trigger if it's still in the DOM (an intervening board
    // re-render may have replaced it — then focus simply falls to <body>, acceptable).
    // a Needs-you row replaced by a redraw is found again by its item id
    if (rf && !document.body.contains(rf) && rf.dataset && rf.dataset.needsId) {
      rf = Array.prototype.find.call(document.querySelectorAll("#ck-needs .needs-row"), (n) => n.dataset.needsId === rf.dataset.needsId) || null;
    }
    if (rf && typeof rf.focus === "function" && document.body.contains(rf)) rf.focus({ preventScroll: true });
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
  // discard? (intention-108) render() rebuilds the whole #board (+ modal) via
  // replaceChildren, so a visibilitychange / refresh-button re-read fired while a
  // textarea is open silently loses the typing. The boot shim calls this to DEFER those
  // passive re-reads — NOT the save's own commit→load reload, which intentionally
  // rebuilds to show saved state (that's the call-site split that avoids breaking save).
  //   - an edit-of-existing (`.edit-ta` State/config, `.verb-edit-ta` intention text) is
  //     dirty once its value diverges from the `data-orig` stamped at open;
  //   - a capture/free-text box (`.tray-dump-ta` Tray dump; `.verb-ref`
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
  //   flash the OLD name; it is gated below with `.state-editor`, intention-280.)
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
    // reload is passive:false → NOT gated here → still rebuilds #entity (intention-280).
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

  // A focused text field also holds a passive RE-READ off (never a reorder or a Reset-layout, which keep the dirty-edit meaning
  // above): the draw empties every region listed here, and a node removed from the page takes the keyboard with it. Only a field
  // inside one of those regions counts (the drop box, the chat and the devices sit outside and survive the draw), and only while
  // it holds text or took a key within the idle window, so a box merely left focused never freezes the board.
  const FOCUS_REBUILT = "#board, #levain-modal, #ck-hero, #ck-brief, #ck-widgets, #ck-needs, #ck-notes";
  const FOCUS_IDLE_MS = 10000;
  // recency is kept per element: typing in one field (the chat, an earlier box) says nothing about a different field focused later
  const fieldInputAt = typeof WeakMap !== "undefined" ? new WeakMap() : null;
  if (fieldInputAt && typeof document !== "undefined" && document.addEventListener) {
    const stamp = (e) => { if (e && e.target && typeof e.target === "object") fieldInputAt.set(e.target, Date.now()); };
    document.addEventListener("input", stamp, true);
    document.addEventListener("keydown", stamp, true);
    document.addEventListener("change", stamp, true);
  }
  function focusHoldsRefresh() {
    const a = document.activeElement;
    if (!a || a === document.body || !a.closest || !a.closest(FOCUS_REBUILT)) return false;
    const tag = a.tagName;
    let text = false;
    if (tag === "TEXTAREA" || (tag === "INPUT" && !/^(button|submit|reset|checkbox|radio|file|image|range|color)$/i.test(a.type || ""))) text = true;
    else if (a.isContentEditable) text = true;
    else if (tag !== "SELECT") return false;
    if (text && String(a.isContentEditable ? a.textContent : a.value || "").trim() !== "") return true;
    const at = fieldInputAt ? fieldInputAt.get(a) : 0;
    return !!at && Date.now() - at < FOCUS_IDLE_MS;
  }
  // what holds a passive re-read off: an unsaved edit, or a field the person is in
  function editHoldsRefresh() { return hasUnsavedEdit() || focusHoldsRefresh(); }

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
  // A draw that is allowed while a text field in a redrawn region has focus (an idle field, an empty one) replaces that field:
  // the keyboard would fall to <body>. So the field is known before the draw by the keys the page already finds rows by
  // (the region id; data-ck-panel / data-panel-id, data-row-key / data-keep-key, data-question-n, data-id, data-drop-key; the field's own id),
  // and its rebuilt twin takes the focus, the selection and the scroll back. A field with no twin is gone by design.
  const FIELD_KEYS = ["data-ck-panel", "data-panel-id", "data-row-key", "data-keep-key", "data-question-n", "data-id", "data-drop-key"];
  const isTextField = (a) => !!a && a.tagName && (a.tagName === "TEXTAREA" ||
    (a.tagName === "INPUT" && !/^(button|submit|reset|checkbox|radio|file|image|range|color)$/i.test(a.type || "")));
  const cssq = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/[^\w-]/g, "\\$&"));
  let fieldWarned = false;
  const fieldWarn = (e) => { if (!fieldWarned) { fieldWarned = true; try { console.warn("focus keep skipped:", e); } catch (x) { /* no console */ } } };
  // a type with no selection (email, number, date...) throws on the read and on the write: neither may break a draw
  const selOf = (a) => { try { return [a.selectionStart, a.selectionEnd]; } catch (e) { return [null, null]; } };
  function captureField() {
    try {
      const a = document.activeElement;
      if (!isTextField(a) || !a.closest) return null;
      const region = a.closest(FOCUS_REBUILT);
      if (!region || !region.id) return null;
      const [ss, se] = selOf(a);
      const base = { nth: 0, region: region.id, ss, se, st: a.scrollTop, el: a };
      if (a.id) return Object.assign(base, { sel: "#" + cssq(a.id) });
      const path = [];
      for (let n = a.parentElement; n && n !== region; n = n.parentElement) {
        for (const k of FIELD_KEYS) {
          const v = n.getAttribute && n.getAttribute(k);
          if (v !== null && v !== undefined && v !== "") { path.unshift("[" + k + "=\"" + String(v).replace(/["\\]/g, "\\$&") + "\"]"); break; }
        }
      }
      const cls = (a.className && typeof a.className === "string" ? a.className.split(/\s+/).filter(Boolean) : []).map((c) => "." + cssq(c)).join("");
      const tail = a.tagName.toLowerCase() + cls;
      const scope = "#" + cssq(region.id) + (path.length ? " " + path.join(" ") : "");
      const same = Array.from(region.querySelectorAll(path.length ? path.join(" ") + " " + tail : tail));
      return Object.assign(base, { sel: scope + " " + tail, nth: same.indexOf(a) });
    } catch (e) { fieldWarn(e); return null; }
  }
  // Focus, selection and scroll only: the value is never put back (a write that cleared the field must stay cleared).
  function restoreField(f) {
    try {
      if (!f || document.activeElement === f.el && f.el.isConnected) return;
      let t = null;
      try { const all = document.querySelectorAll(f.sel); t = all[Math.max(0, f.nth)] || (f.nth < 0 ? all[0] : null) || null; } catch (e) { t = null; }
      if (!t || !isTextField(t)) return;
      t.focus({ preventScroll: true });
      try { if (f.ss !== null && f.ss !== undefined) t.setSelectionRange(f.ss, f.se); } catch (e) { /* a type with no selection */ }
      if (f.st) t.scrollTop = f.st;
    } catch (e) { fieldWarn(e); }
  }
  function renderKeepingPlace(view, opts) {
    const y = window.scrollY || 0;
    const field = captureField();
    try { return render(view, opts); }
    finally {
      restoreField(field);
      if (y > 0 && window.scrollY !== y) window.scrollTo(window.scrollX, y);
      ckScheduleRefresh();   // what the draw left on screen (the tab, the jobs) decides which moves are live
    }
  }

  // OS-3, the narrow page's strip: one tap goes to the phone view's content. Needs you, Board and Notifications are blocks on this
  // page, and so is Drop (the card under the Needs-you row: its tab scrolls there and focuses the box); Grants are drawn inside the
  // Decisions view, so that tab opens it and takes the reader to its own slot. That
  // view fills as its reads answer (queued, each up to the fetch deadline), so the wait is for the POPULATED node, bounded by a
  // deadline a little past the read's own; a view with nothing to show (no standing grant, a read that failed) is left where the
  // slot is, or at its top when it has none.
  const PHONE_JUMP_TARGET = {
    // an answered-only strip waits on nothing: the jump goes only where something waits
    needs: "#ck-needs[data-waiting]:not([hidden])",
    board: ".panel[data-ck-panel=\"tray\"]",
    notes: ".panel[data-ck-panel=\"notifications\"]",
    room: "[data-zone=\"room\"]:not([hidden])",
  };
  const PHONE_JUMP_WAIT_MS = CONSENT_FETCH_TIMEOUT_MS + 5000, PHONE_JUMP_STEP_MS = 150, PHONE_JUMP_STEADY = 2;
  // `slot`: where the tab's section sits once it is drawn; `filled`: the same node once its read has filled it (the grants heading
  // exists only with a grant under it)
  function phoneJumpFind(which, overlay) {
    const heads = Array.from(overlay.querySelectorAll(".ext-grouphead"));
    const grants = heads.find((h) => /standing grants/i.test(h.textContent)) || null;
    if (grants) return { slot: grants, filled: grants };
    // no grant under a heading: once BOTH reads have answered (each slot says so), the envelopes' heading is where the tab
    // lands; before that a grants heading may still be drawn above it
    const gs = overlay.querySelector(".grant-slot"), es = overlay.querySelector(".envelope-slot");
    const env = gs && gs.dataset.filled && es && es.dataset.filled
      ? heads.find((h) => /^envelopes$/i.test(h.textContent.trim())) || null : null;
    return { slot: env, filled: env };
  }
  const phoneJumpDrawn = (n) => !!n && n.getClientRects().length > 0;
  // The filled node is scrolled to again until the view stops growing above it (steady looks at the same height), so a row that
  // fills in after the node appeared does not leave the reader short of it.
  // The run belongs to the one view the tap opened (or found open): a view closed and opened again is another view, whose own
  // navigation the old wait must not act on.
  function phoneJumpInView(which, run, overlay, deadline, seen) {
    if (run !== phoneJumpRun || document.getElementById("levain-modal") !== overlay) return;
    const f = phoneJumpFind(which, overlay);
    let top = seen ? seen.top : undefined, steady = seen ? seen.steady : 0;
    if (phoneJumpDrawn(f.filled)) {
      const now = f.filled.getBoundingClientRect().top;
      steady = top !== undefined && Math.abs(now - top) < 1 ? steady + 1 : 0;
      if (steady >= PHONE_JUMP_STEADY) return;
      if (steady === 0) f.filled.scrollIntoView({ block: "start" });
      top = f.filled.getBoundingClientRect().top;
    } else if (!seen && phoneJumpDrawn(f.slot)) {
      f.slot.scrollIntoView({ block: "start" });   // the slot is there before its read answers: the tap shows the move at once
    }
    if (Date.now() < deadline) {
      window.setTimeout(() => phoneJumpInView(which, run, overlay, deadline, { top: top, steady: steady }), PHONE_JUMP_STEP_MS);
    } else if (!phoneJumpDrawn(f.filled) && phoneJumpDrawn(f.slot)) {
      f.slot.scrollIntoView({ block: "start" });   // never filled: the slot is where the reader lands
    }
  }
  function phoneJump(which, opener) {
    const run = ++phoneJumpRun;
    if (which === "notifications") { openNotifyCenter(opener); return; }   // the notification center's focus view (S4.2)
    if (which === "drop") {       // the drop is a card under the Needs-you row: the tab scrolls to its box, no view to open
      const host = document.getElementById("ck-drop");
      if (!host) return;
      window.requestAnimationFrame(() => {
        if (run !== phoneJumpRun) return;
        host.scrollIntoView({ block: "start" });
        const ta = host._drop && host._drop.words;
        if (ta && !dropBoxIsOff(ta)) ta.focus({ preventScroll: true });
      });
      return;
    }
    if (which === "grants") {
      if (!document.getElementById("levain-modal") && !openDecisionsHistory(opener)) return;
      const overlay = document.getElementById("levain-modal");
      // opening can close a view it could not build, which ends the run: the wait starts from the run as it stands once the view is up
      if (overlay) phoneJumpInView(which, phoneJumpRun, overlay, Date.now() + PHONE_JUMP_WAIT_MS);
      return;
    }
    const sel = PHONE_JUMP_TARGET[which];
    const target = sel ? document.querySelector(sel) : null;
    if (!target) return;
    // a panel the saved tab hides has no box to scroll to: its tab is shown first, and the scroll waits for the layout frame
    const K = window.LevainCockpitKinds;
    if (K && typeof K.revealTab === "function") K.revealTab(target);
    window.requestAnimationFrame(() => {
      const t = target.isConnected ? target : document.querySelector(sel);
      if (run === phoneJumpRun && t) t.scrollIntoView({ block: "start" });
    });
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target && ev.target.closest ? ev.target.closest("[data-phone-jump]") : null;
    if (b) phoneJump(b.dataset.phoneJump, b);
  });
  // The jump strip hides its scrollbar, so a tab past either edge gets a cue: the edge it continues past fades (classes the CSS masks on).
  (function () {
    const strip = document.getElementById("ck-phonetabs");
    if (!strip) return;
    const cue = () => {
      strip.classList.toggle("ck-more-right", strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 1);
      strip.classList.toggle("ck-more-left", strip.scrollLeft > 1);
    };
    strip.addEventListener("scroll", cue, { passive: true });
    window.addEventListener("resize", cue);
    window.addEventListener("load", cue);
    if (typeof MutationObserver === "function") new MutationObserver(cue).observe(strip, { childList: true, subtree: true, characterData: true });
    // Labels can widen with no watched event (a late webfont swap, a text-size change, a container resize that is not a window resize).
    // Toggling the mask classes changes neither the strip's box nor a button's, so the observer cannot loop on its own cue.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(cue);
    if (typeof ResizeObserver === "function") {
      const ro = new ResizeObserver(cue);
      ro.observe(strip);
      strip.querySelectorAll("button").forEach((b) => ro.observe(b));   // the strip's own box is fixed; a widening label moves its buttons
    }
    cue();
  })();
  // Any trusted reader input ends a pending automatic jump (a late Decisions read must never replace the view the reader chose): a touch, a
// wheel, a press or click anywhere (a panel's expand, a nav link, Notifications) or a key. Script-made events (isTrusted false) do not.
for (const type of ["touchstart", "wheel", "pointerdown", "click", "keydown"]) document.addEventListener(type, (ev) => { if (ev.isTrusted) phoneJumpRun++; }, { passive: true, capture: true });

  // ====================================================================== THE NOTIFICATION CENTER (S4.2) ====
  // One list of Levain's pings and the notices posted at the operator (GET /webpush/center, levain/webpush/center.py), drawn
  // three ways from ONE store: the strip's unread pill, the page's compact panel (the five newest), and the focus view (list and
  // detail). The page never holds a mark the server did not confirm: a write is awaited, then the list is read again; a failed
  // write says what did not land and the list is the server's. Every field is set through el()/textContent or the markdown
  // renderer's DOM nodes, never innerHTML: this page shares a document with the write token.
  const NC_PATH = "/webpush/center", NC_POLL_MS = 30000, NC_TIMEOUT_MS = 15000, NC_PANEL_ROWS = 5, NC_SWIPE_PX = 96, NC_REFRESH_MAX = 200;   // 200 is the server's page cap
  const NC_ID = /^[pn][1-9][0-9]{0,17}$/;
  const nc = { sig: "", items: [], unread: 0, total: 0, needs: 0, next: null, status: "idle", goodAt: 0, readId: 0, wgen: 0, busy: 0 };
  // status: idle (never read) | ok | error (never read, last try failed) | noauth (no/refused token) | absent (this server has no center)
  const ncUi = { sel: null, pinned: null, filter: "", banner: null, menu: false, detail: false, gone: "", more: false };
  // ncDeepLink: the pending id came from the page's own address (a push tap's deep link), not a click on this page.
  // ncReadArmed: the id a deep link selected and left unread; the first trusted gesture inside the view marks it (see ncSelect).
  let ncRoot = null, ncDeepPending = null, ncDeepLink = false, ncReadArmed = null;
  const ncPanels = new Set();

  function ncApi(method, path, body) {
    const t = window.LevainCockpitTransport;
    const headers = t && typeof t.headers === "function" ? Object.assign({}, t.headers()) : {};
    if (!headers["X-Levain-Write-Token"]) return Promise.resolve({ status: 403, data: {}, notoken: true });
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl ? window.setTimeout(() => ctl.abort(), NC_TIMEOUT_MS) : 0;
    return fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal: ctl ? ctl.signal : undefined })
      .then((res) => res.json().then((d) => ({ status: res.status, data: d && typeof d === "object" ? d : {} }), () => ({ status: res.status, data: {} })))
      .catch(() => ({ status: 0, data: {} }))
      .finally(() => { if (timer) window.clearTimeout(timer); });
  }
  const ncFind = (id) => nc.items.find((x) => x.id === id) || (ncUi.pinned && ncUi.pinned.id === id ? ncUi.pinned : null);
  const ncClock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  function ncWhen(iso, full) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const d = new Date(t), now = new Date();
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (full) return time + " · " + d.toLocaleDateString([], { month: "short", day: "numeric" });
    if (d.toDateString() === now.toDateString()) return time;
    return (now - d) / 86400000 < 6 ? d.toLocaleDateString([], { weekday: "short" }) : d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function ncDelivery(dv) {
    if (!dv || typeof dv !== "object") return "";
    if (typeof dv.held === "string" && dv.held) return "Held: " + dv.held;
    const pushed = Array.isArray(dv.pushed) ? dv.pushed.map(String) : [], failed = Array.isArray(dv.failed) ? dv.failed.map(String) : [];
    let s = pushed.length ? "Pushed to " + pushed.join(", ") : failed.length ? "Push did not reach " + failed.join(", ") : "Push on its way";
    if (pushed.length && failed.length) s += " · did not reach " + failed.join(", ");
    if (pushed.length) s += dv.opened_at ? " · opened " + ncWhen(dv.opened_at, false) : " · not opened";
    return s;
  }

  // ---- the read -------------------------------------------------------------------------------------------------------
  // Only the newest read paints (a write bumps the id, so a read begun before it is dropped). A failed read keeps the list and
  // says so; a list never read says that instead.
  async function ncRead(force) {
    if (nc.busy && !force) return false;
    const mine = ++nc.readId;
    // a refresh keeps the pages the reader loaded: it asks for as many rows as are held (the server's page cap bounds it), so opening
    // an older row, a write or the 30 s pulse never collapses the list to page one and closes the open detail
    const res = await ncApi("GET", NC_PATH + "?limit=" + Math.min(NC_REFRESH_MAX, Math.max(50, nc.items.length)));
    if (mine !== nc.readId) return false;
    const d = res.data || {};
    if (res.status === 200 && Array.isArray(d.items)) {
      // the same list again repaints nothing (a pulse never closes a swiped row or drops the reader's place)
      const sig = JSON.stringify([d.items, d.unread, d.total, d.needs_count, d.next_before]);
      const same = sig === nc.sig && nc.status === "ok" && !ncUi.banner && !ncDeepPending;
      nc.sig = sig; nc.goodAt = Date.now();
      if (same) return true;
      nc.items = d.items; nc.unread = d.unread | 0; nc.total = d.total | 0; if (typeof d.needs_count === "number") nc.needs = d.needs_count | 0; nc.next = d.next_before || null;
      nc.status = "ok"; nc.goodAt = Date.now();
      if (ncUi.banner && ncUi.banner.kind === "read") ncUi.banner = null;
      if (ncUi.sel && !ncFind(ncUi.sel) && !ncDeepPending) { ncUi.sel = null; ncUi.detail = false; }
    } else if (res.status === 404 && nc.status !== "ok") {
      nc.status = "absent";
    } else {
      if (res.notoken || res.status === 403) { if (nc.status !== "ok") nc.status = "noauth"; }
      else if (nc.status !== "ok") nc.status = "error";
      // a standing write failure ("Delete didn't reach Levain") is the more specific word: a refresh that also fails never replaces it
      if ((nc.status === "ok" || nc.goodAt) && !(ncUi.banner && ncUi.banner.kind === "write")) ncUi.banner = { kind: "read", retry: true, text: "Couldn’t refresh. Showing the list as of " + ncClock(nc.goodAt || Date.now()) + "." };
    }
    ncPaint();
    if (nc.status === "ok" && ncDeepPending) ncDeepTry();
    return nc.status === "ok";
  }
  async function ncMore() {
    if (!nc.next || ncUi.more) return;
    ncUi.more = true; ncPaint();
    const gen = nc.wgen, cursor = nc.next;   // a write that lands while this page is in flight makes it stale: it is dropped, never appended
    const res = await ncApi("GET", NC_PATH + "?limit=50&before=" + encodeURIComponent(cursor));
    ncUi.more = false;
    if (gen !== nc.wgen) { ncPaint(); return; }
    // a pulse that replaced the list while this page was in flight moved the cursor: appending would leave a gap, so read again
    if (nc.next !== cursor) { ncPaint(); ncRead(true); return; }
    if (res.status === 200 && Array.isArray(res.data.items)) {
      const have = new Set(nc.items.map((x) => x.id));
      nc.items = nc.items.concat(res.data.items.filter((x) => !have.has(x.id)));
      nc.next = res.data.next_before || null;
    } else ncUi.banner = { kind: "write", retry: false, text: "Older notifications didn’t load. The list is as it was." };
    ncPaint();
  }

  // ---- the writes (each awaited; the list is read again after, so the page shows what the server holds) ----------------
  async function ncWrite(path, body, failWords, quiet404) {
    nc.busy++; nc.readId++; nc.wgen++; nc.sig = "";   // the page's list is about to differ from the server's: the next read always paints
    // a failure's banner stays until it is dismissed or the next write starts: a write that lands never wipes another's failure
    if (ncUi.banner && ncUi.banner.kind === "write") ncUi.banner = null;
    let res;
    try { res = await ncApi("POST", path, body); } finally { nc.busy--; }
    const ok = (res.status === 200 && res.data.ok === true) || (quiet404 && res.status === 404);
    if (ok) { if (typeof res.data.unread === "number") nc.unread = res.data.unread; }
    else ncUi.banner = { kind: "write", retry: false, text: failWords + " didn’t reach Levain. Nothing was changed." };
    await ncRead(true);
    return ok;
  }
  async function ncSetRead(id, read) {
    const ok = await ncWrite(NC_PATH + "/read", { id, read }, read ? "Marking it read" : "Marking it unread", false);
    if (ok && ncUi.pinned && ncUi.pinned.id === id) { ncUi.pinned = Object.assign({}, ncUi.pinned, { read }); ncPaint(); }
    return ok;
  }
  async function ncMarkAll() {
    const ok = await ncWrite(NC_PATH + "/read-all", {}, "Mark all read", false);
    if (ok && ncUi.pinned && !ncUi.pinned.read) { ncUi.pinned = Object.assign({}, ncUi.pinned, { read: true }); ncPaint(); }
    return ok;
  }
  async function ncClear() {
    ncUi.menu = false;
    const ok = await ncWrite(NC_PATH + "/clear", {}, "Clear all", false);
    if (ok) { ncUi.sel = null; ncUi.pinned = null; ncUi.detail = false; ncPaint(); }
  }
  async function ncDelete(id) {
    const it = ncFind(id), was = { items: nc.items, total: nc.total, unread: nc.unread }, wasSel = ncUi.sel === id, wasPin = ncUi.pinned;
    // the row leaves at once; a failure puts it back (the pinned copy is only dropped once the delete is confirmed) and names what did not land
    nc.items = nc.items.filter((x) => x.id !== id); nc.total = Math.max(0, nc.total - 1);
    if (it && !it.read) nc.unread = Math.max(0, nc.unread - 1);
    if (ncUi.sel === id) { ncUi.sel = null; ncUi.detail = false; }
    ncPaint();
    const ok = await ncWrite(NC_PATH + "/delete", { id }, "Delete", true);
    if (ok) { if (ncUi.pinned && ncUi.pinned.id === id) { ncUi.pinned = null; ncPaint(); } return ok; }
    // not deleted: a refresh that failed too (its sig is still empty) leaves the optimistic list, so the held one comes back whole;
    // a refresh that landed already holds the server's list, and a row beyond it stays reachable through the pin
    if (!nc.sig) { nc.items = was.items; nc.total = was.total; nc.unread = was.unread; }
    if (it && !nc.items.some((x) => x.id === id)) ncUi.pinned = it;
    else if (wasPin && wasPin.id === id) ncUi.pinned = wasPin;
    if (wasSel) { ncUi.sel = id; ncUi.detail = true; }
    ncPaint();
    return ok;
  }
  // `byLink`: the selection came from the address (/?view=notifications&n=<id>), which the cross-site guard admits so a push tap
  // that reaches the browser as a launcher intent can open (1105-7 defect A). Any other site can make that same navigation, so a
  // deep link only SELECTS; the item is marked read by the first trusted tap or key press inside the view (ncGesture), which a
  // hostile navigation never yields (codex, L3 pwa-ab r1). A click on a row keeps marking read at once.
  function ncSelect(id, byLink) {
    ncUi.sel = id; ncUi.detail = true; ncUi.gone = "";
    const it = ncFind(id);
    // the open row is always pinned (a new arrival can push it past any refresh limit); ncFind prefers the list's own copy
    if (it) ncUi.pinned = it;
    ncReadArmed = byLink && it && !it.read ? id : null;
    ncPaint();
    if (!byLink && it && !it.read) ncSetRead(id, true);
  }
  function ncGesture(ev) {
    if (!ev.isTrusted || !ncReadArmed) return;
    if (ev.type === "keydown" && ev.key === "Escape") return;   // leaving the view is not reading the item
    const id = ncReadArmed;
    ncReadArmed = null;
    // the item's own controls (its read toggle, delete, a row's dot or delete) carry their own intent: disarm, write nothing here
    const t = ev.target && ev.target.closest ? ev.target.closest("[data-tool], .nc-dot-btn, .nc-del") : null;
    if (t) return;
    const it = ncFind(id);
    if (ncUi.sel === id && it && !it.read) ncSetRead(id, true);
  }

  // ---- the deep link: /?view=notifications&n=<id> (a push tap) --------------------------------------------------------
  async function ncDeepTry() {
    const id = ncDeepPending, byLink = ncDeepLink;
    if (!id) return;
    if (nc.items.some((x) => x.id === id)) { ncDeepPending = null; ncSelect(id, byLink); return; }
    if (nc.status !== "ok") return;           // no list yet (the token may still be on its way): the next read tries again
    ncDeepPending = null;
    const res = await ncApi("GET", NC_PATH + "/item?id=" + encodeURIComponent(id));
    if (res.status === 200 && res.data.item) { ncUi.pinned = res.data.item; ncSelect(id, byLink); return; }
    if (res.status === 404) {
      ncUi.gone = res.data.error === "gone" && typeof res.data.message === "string" ? res.data.message : res.data.error === "gone" ? "That notification was deleted." : "That notification isn’t here.";
      ncUi.sel = null; ncUi.detail = false;
    } else ncUi.banner = { kind: "write", retry: false, text: "Couldn’t open that notification. The list is as it was." };
    ncPaint();
  }
  // The page's address names a view to open: `notifications` (+ `n`) is the center's focus view; `needs` (a held summary's tap,
  // held.SUMMARY_URL) is the Needs-you strip, reached the way the strip's own Needs tab reaches it. Any other `view` value is left
  // alone (the page opens at home, the address untouched). A link only OPENS a view: it never answers, marks read or writes.
  function ncUrlAsk() {
    try {
      const q = new URLSearchParams(window.location.search);
      const view = q.get("view");
      if (view !== "notifications" && view !== "needs") return null;
      const n = q.get("n");
      const q0 = new URLSearchParams(q.toString());
      q.delete("view"); q.delete("n"); q.delete("item");
      const rest = q.toString();
      window.history.replaceState(null, "", window.location.pathname + (rest ? "?" + rest : "") + window.location.hash);
      if (view === "needs") {
        // `&item=<id>`: the one waiting card the link is about. Only a bounded id is kept (the guard admits the same shape); the
        // address drops it like the other two, and a link only OPENS the card: it never answers, marks read or writes.
        const item = q0.get("item");
        return { needs: true, item: item && NEEDS_ITEM_ID.test(item) ? item : null };
      }
      return { id: n && NC_ID.test(n) ? n : null };
    } catch (_) { return null; }
  }
  // The id a deep link names a Needs-you card by (`/?view=needs&item=<id>`): the card's own id (`question:<n>`, `consent:<request id>`),
  // the bare request id a sign card was filed under, or flow's `np-<8 hex>` item id. Guessing its card as `question:<1 + hex>` is only a
  // first try (flow now numbers cards with the band number); the band read in needsItemJump is the authority. The server admits exactly this shape.
  const NEEDS_ITEM_ID = /^[A-Za-z0-9_.:-]{1,64}$/;
  const BAND_LOOKUP_MS = 8000;   // how long the tap's card lookup waits on one band read before counting it refused
  function needsItemMatch(it, item) {
    const id = needsIdOf(it);
    // a first guess only: an older flow numbered an `np-` item's card `question:<1 + the id's hex>`; when it misses, the band read decides
    const hex = /^np-([0-9a-f]{8})$/i.exec(item);
    if (hex && id === "question:" + (1 + parseInt(hex[1], 16))) return true;
    return !!id && (id === item || id === "consent:" + item || id === "question:" + item || id === "np:" + item);
  }
  function needsGoneNote() {
    const host = document.getElementById("ck-needs");
    if (!host || !host.parentNode) return;
    for (const old of document.querySelectorAll(".needs-gone-note")) old.remove();
    const note = el("div", "needs-gone-note ext-meta", "That item is no longer waiting.");
    note.setAttribute("role", "status");
    host.parentNode.insertBefore(note, host);
    window.setTimeout(() => note.remove(), 12000);
  }
  // A `needs` link with an `item`: wait (bounded, given up when the reader touches the page) for the Decisions lines to be read, then
  // scroll to the card and open its focus view; an id that is not among the waiting items falls back to the plain Needs view with a
  // one-line notice. Without a read by the deadline it falls back the same way.
  // The first board draws in parts (S4) and the whole draw then replaces every node: a jump made on a part would land where the rest of the
  // page pushes it from. The boot sets html[data-board-whole] when the whole board is drawn, or when no whole draw is coming soon (a refused
  // read, a failed one with no part drawn, or the boot's watchdog, a clock from the first inert part, which releases a part whatever its reads
  // are doing; while a retry is queued after a part the signal stays unset and the part inert until then), and never on a partial draw, so a jump waits for that, and at its deadline falls back to the target as the page has it (a slow board still gets its jump, a dead link is worse than a mispositioned one).
  function boardWhole() { return !!document.documentElement && typeof document.documentElement.hasAttribute === "function" && document.documentElement.hasAttribute("data-board-whole"); }
  function needsItemJump(item, run, deadline) {
    if (run !== phoneJumpRun) return;
    const dec = ((currentView && currentView.extra_panels || {}).decisions || {});
    const lines = dec.lines;
    // an EMPTY listing is an answer only when the panel says so itself: `clean` (a whole read, status ok or empty, nothing skipped or
    // filtered) with no error or note. Any other empty one (unread, short, partial) is not (the next read may hold the item): keep waiting
    const whole = boardWhole() && Array.isArray(lines) && (lines.length > 0 || (dec.clean === true && !dec.error && !dec.note));
    if (whole) {
      const it = needsWaiting(needsItems(lines)).find((x) => needsItemMatch(x, item));
      if (it) {
        if (document.querySelector(PHONE_JUMP_TARGET.needs)) needsGo(needsIdOf(it), true);
        if (window.LevainCardVoice) window.LevainCardVoice.opened(needsIdOf(it));   // V1-7: this card came from a notification
        openNeedsFocus(it, null);
        return;
      }
      // The item's own id is a name the card number does not carry: flow numbers its cards with the BAND number, not 1 + the id's hex
      // (11-06, np-0b62cd3b: the ping's `item=np-...` named no card). The band read pairs each item's own id (`subject.id`) with its
      // card number (`question`), so ask it before saying the item is gone.
      // `said` is true only when a FRESH, SUCCESSFUL band read lacks the item: that alone concludes "no longer waiting". A cached read
      // (bandFetch holds one ~2 s, so it may predate the item) or a read that was refused (transient) never does: the first miss
      // asks once more, fresh; a refusal waits a beat (bounded) and asks fresh once; a second refusal falls back to the plain Needs
      // view WITHOUT the notice (it was never answered, so "gone" is not known).
      const gone = (said) => {
        if (run !== phoneJumpRun) return;
        // "no longer waiting" is said only of a whole listing: a panel that carries an error or a note (a partial read says so there) is not one
        if (said && !dec.error && !dec.note) needsGoneNote();
        needsDeepJump(run, Date.now() + PHONE_JUMP_WAIT_MS);   // the fallback to plain Needs gets its own wait, not the spent one
      };
      const find = (b) => {
        const row = (b && Array.isArray(b.items) ? b.items : []).find((x) => x && x.subject && x.subject.id === item && Number.isInteger(x.question));
        return row && needsWaiting(needsItems(lines)).find((x) => needsIdOf(x) === "question:" + row.question);
      };
      // A read that never answers (a hung connection) is a refusal after BAND_LOOKUP_MS: the explicit timer below rejects the wait, so the
      // lookup falls through its refused path (one retry, then the plain Needs view, no notice) instead of waiting forever. The timer is
      // installed unconditionally (no AbortController needed). The browser request itself keeps running: the injected bandRead takes no
      // abort signal (wiring one is out of scope), so only the lookup stops waiting. A successful read passes through as is.
      const read = (fresh) => {
        let q = null;
        try { q = typeof bandRead === "function" ? bandFetch(fresh) : null; } catch (e) { q = null; }
        if (!q || typeof q.then !== "function") return null;
        return new Promise((resolve, reject) => {
          const timer = window.setTimeout(() => reject(new Error("band read timed out")), BAND_LOOKUP_MS);
          q.then((b) => { window.clearTimeout(timer); resolve(b); }, (e) => { window.clearTimeout(timer); reject(e); });
        });
      };
      const open = (hit) => {
        if (document.querySelector(PHONE_JUMP_TARGET.needs)) needsGo(needsIdOf(hit), true);
        if (window.LevainCardVoice) window.LevainCardVoice.opened(needsIdOf(hit));   // V1-7: this card came from a notification
        openNeedsFocus(hit, null);
      };
      const second = (retries) => {
        const q2 = read(true);                        // bypasses the ~2 s cache: this read cannot predate the item
        if (!q2) { gone(false); return; }
        q2.then((b2) => { if (run !== phoneJumpRun) return; const h2 = find(b2); if (h2) open(h2); else gone(true); },
          () => { if (run !== phoneJumpRun) return; if (retries > 0) window.setTimeout(() => second(retries - 1), 700); else gone(false); });   // the server reads one band at a time: "busy" is a refusal too
      };
      const p = read(false);
      if (!p) { gone(true); return; }                 // no band to ask on this cockpit: the listing alone answered
      p.then((b) => {
        if (run !== phoneJumpRun) return;
        const hit = find(b);
        if (hit) open(hit); else second(1);
      }, () => { if (run === phoneJumpRun) window.setTimeout(() => { if (run === phoneJumpRun) second(0); }, 700); });
      return;
    }
    if (Date.now() < deadline) { window.setTimeout(() => needsItemJump(item, run, deadline), PHONE_JUMP_STEP_MS); return; }
    needsDeepJump(run, Date.now() + PHONE_JUMP_WAIT_MS);   // never read (unpaired, empty or slow): not "gone"
  }
  // The strip draws once the board's first read answers, after this boot: wait (bounded, and given up the moment the reader touches
  // the page: phoneJumpRun moves) for the Needs strip to hold something waiting, then jump exactly as the Needs tab does. With
  // nothing waiting the page stays where it opened.
  function needsDeepJump(run, deadline) {
    if (run !== phoneJumpRun) return;
    const there = !!document.querySelector(PHONE_JUMP_TARGET.needs);
    if (there && (boardWhole() || Date.now() >= deadline)) { phoneJump("needs", null); return; }
    if (Date.now() < deadline) window.setTimeout(() => needsDeepJump(run, deadline), PHONE_JUMP_STEP_MS);
  }

  // ---- the markdown and the links ---------------------------------------------------------------------------------------
  // The page's own renderer (DOM nodes only; raw HTML paints as literal text), then two passes: a bare http(s) URL becomes a link,
  // and every link is held to the rule: only absolute http/https is live (target _blank, rel noopener noreferrer); any other
  // scheme, or a relative path, stays its text. Every link whose text is not its address shows the address beside the anchor text,
  // so the words never hide where it goes (Phill 4a; for every sender since the detail view's markdown pass).
  function ncLinkify(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const hits = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const host = n.parentNode;
      if (host && host.closest && host.closest("a, code, pre")) continue;
      if (/https?:\/\//i.test(n.nodeValue)) hits.push(n);
    }
    for (const node of hits) {
      const text = node.nodeValue, frag = document.createDocumentFragment();
      let last = 0;
      text.replace(/\bhttps?:\/\/[^\s<>"'`]+/gi, (m, at) => {
        const url = m.replace(/[.,;:!?)\]]+$/, "");
        if (at > last) frag.appendChild(document.createTextNode(text.slice(last, at)));
        const a = el("a", "md-link", url);
        a.setAttribute("href", url);
        frag.appendChild(a);
        last = at + url.length;
        return m;
      });
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    }
  }
  function ncHarden(root) {
    for (const a of Array.from(root.querySelectorAll("a"))) {
      let url = null;
      try { const u = new URL(a.getAttribute("href") || ""); if (u.protocol === "http:" || u.protocol === "https:") url = u; } catch (_) { /* relative or unreadable: inert */ }
      if (!url) { a.replaceWith(...Array.from(a.childNodes)); continue; }
      a.setAttribute("href", url.href);
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
      a.classList.add("nc-link");
      // every sender's link shows where it goes, whoever wrote it: link text that is not the address gets the address beside it
      const shown = a.textContent.trim();
      if (shown !== url.href && shown !== (url.href.replace(/\/$/, ""))) a.after(el("span", "nc-fullurl", " (" + url.href + ")"));
    }
  }
  // A seat's notice often has the needs-phill shape `... OPTIONS: (a) x · (b) y REC: a (why) ...`. ncShape finds that shape in
  // the markdown text and splits it: what came before, the options as keyed rows, the REC text, what came after. null when the
  // text is not in that shape (or the label sits inside a code fence), and the body is then drawn as plain markdown. It only
  // splits: no word of the stored body changes.
  const NC_OPT_LABEL = /(^|\s)\**OPTIONS:\**[ \t]+/g;
  const NC_REC_LABEL = /(^|\s)\**REC:\**[ \t]+/g;
  // an option key is one lowercase letter in parentheses, and the keys of one list run (a), (b), (c) in order: any other "(x)" inside an
  // option's prose is text
  const NC_OPT_KEY = /(^|[\s·;])(?:[-*][ \t]+)?\(([a-z])\)[ \t]*/g;
  // the [start, end) character ranges of `md` that are code fences, by the renderer's own rule (an unclosed fence runs to the end), in one pass
  function ncFenced(md) {
    const ranges = [];
    let pos = 0, open = null, from = 0;
    for (const line of md.split("\n")) {
      const end = pos + line.length + 1;
      if (open) { if (mdFenceCloses(line, open)) { ranges.push([from, end]); open = null; } }
      else if ((open = mdFenceOpen(line))) from = pos;
      pos = end;
    }
    if (open) ranges.push([from, md.length + 1]);
    return ranges;
  }
  // the first match of a label in `text` (the tail of `full`) that is not inside a code fence; the matches ascend, so the ranges are walked once
  function ncLabel(re, text, full, ranges) {
    const base = full.length - text.length;
    let k = 0;
    re.lastIndex = 0;
    for (let m; (m = re.exec(text)); ) {
      const at = base + m.index + m[1].length;
      while (k < ranges.length && ranges[k][1] <= at) k++;
      if (!(k < ranges.length && ranges[k][0] <= at)) return m;
    }
    return null;
  }
  function ncShape(md) {
    const ranges = ncFenced(md);
    const o = ncLabel(NC_OPT_LABEL, md, md, ranges);
    if (!o) return null;
    const at = o.index + o[1].length;
    const afterOpt = o.index + o[0].length;
    const rest = md.slice(afterOpt);
    const r = ncLabel(NC_REC_LABEL, rest, md, ranges);
    if (!r) return null;          // the shape is OPTIONS and REC both; options alone are plain markdown
    const stop = /\n[ \t]*\n/.exec(rest);
    const rAt = r.index + r[1].length;
    const optEnd = !stop || rAt < stop.index ? rAt : stop.index;
    const optText = rest.slice(0, optEnd);
    const keys = [];
    NC_OPT_KEY.lastIndex = 0;
    for (let m; (m = NC_OPT_KEY.exec(optText)); ) {
      if (m[2] === String.fromCharCode(97 + keys.length)) keys.push({ key: m[2], from: m.index + m[1].length, start: m.index + m[0].length });
    }
    if (keys.length < 2 || optText.slice(0, keys[0].from).trim()) return null;
    const opts = keys.map((k, i) => {
      const end = i + 1 < keys.length ? keys[i + 1].from : optText.length;
      return { key: k.key, text: optText.slice(k.start, end).replace(/[\s·;]+$/, "").trim() };
    });
    if (opts.some((x) => !x.text)) return null;
    let rec = "", recKey = "", tail = "";
    if (rAt === optEnd) {
      const body = rest.slice(r.index + r[0].length);
      const cut = /\n[ \t]*\n|\s\**[A-Z][A-Z0-9'+\/&-]{2,}:\**[ \t]/.exec(body);
      rec = (cut ? body.slice(0, cut.index) : body).trim();
      tail = cut ? body.slice(cut.index) : "";
      // the recommended option is named by the REC's first word only as a key standing alone: "REC: a quick fix" is prose
      const k = /^\(?([a-z])\)?(?=$|\s*[(:,.;\u2014\u2013-])/.exec(rec);
      recKey = k && opts.some((x) => x.key === k[1]) ? k[1] : "";
    } else tail = rest.slice(optEnd);
    return { before: md.slice(0, at).trim(), opts, rec, recKey, tail: tail.trim() };
  }
  function ncInline(into, text) {
    const f = renderMarkdown(text);
    if (f.childNodes.length === 1 && f.firstChild.nodeName === "P") into.append(...Array.from(f.firstChild.childNodes));
    else into.appendChild(f);
  }
  function ncBody(md) {
    const host = el("div", "nc-body");
    const text = String(md || "").replace(/\r\n?/g, "\n");
    const shape = ncShape(text);
    if (!shape) host.appendChild(renderMarkdown(text));
    else {
      if (shape.before) host.appendChild(renderMarkdown(shape.before));
      const opts = el("div", "nc-opts");
      opts.appendChild(el("div", "nc-blabel", "Options"));
      const ul = el("ul", "nc-optlist");
      for (const o of shape.opts) {
        const isRec = shape.recKey && o.key.toLowerCase() === shape.recKey;
        const li = el("li", "nc-opt" + (isRec ? " nc-opt-rec" : ""));
        li.appendChild(el("span", "nc-optkey", "(" + o.key + ")"));
        const t = el("span", "nc-opttext"); ncInline(t, o.text); li.appendChild(t);
        if (isRec) li.appendChild(el("span", "nc-recmark", "Rec"));
        ul.appendChild(li);
      }
      opts.appendChild(ul);
      host.appendChild(opts);
      if (shape.rec) {
        const rec = el("div", "nc-rec");
        rec.appendChild(el("div", "nc-blabel", "Recommendation"));
        rec.appendChild(renderMarkdown(shape.rec));
        host.appendChild(rec);
      }
      if (shape.tail) host.appendChild(renderMarkdown(shape.tail));
    }
    ncLinkify(host);
    ncHarden(host);
    return host;
  }

  // ---- the row (one component: the panel's compact row and the focus view's full row) ----------------------------------
  function ncChips(it) {
    const f = document.createDocumentFragment();
    f.appendChild(el("span", "nc-chip o-" + (it.origin === "token" ? it.origin : "levain"), it.source || it.origin || ""));
    if (it.priority === "urgent" || it.priority === "low") f.appendChild(el("span", "nc-chip p-" + it.priority, it.priority));
    return f;
  }
  function ncSwipe(row, rowin) {
    let x0 = 0, y0 = 0, horiz = null, live = false;
    row.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; horiz = null; live = true;
      rowin.style.transition = "none";
    }, { passive: true });
    row.addEventListener("touchmove", (e) => {
      if (!live) return;
      const dx = e.touches[0].clientX - x0, dy = e.touches[0].clientY - y0;
      if (horiz === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) horiz = Math.abs(dx) > Math.abs(dy);
      if (!horiz) return;
      const base = row.classList.contains("is-swiped") ? -NC_SWIPE_PX : 0;
      rowin.style.transform = "translateX(" + Math.max(-NC_SWIPE_PX, Math.min(0, base + dx)) + "px)";
    }, { passive: true });
    const end = () => {
      if (!live) return;
      live = false;
      const m = /-?[0-9.]+/.exec(rowin.style.transform || "");
      const v = m ? parseFloat(m[0]) : null;
      rowin.style.transition = ""; rowin.style.transform = "";
      if (v === null) return;
      const open = v < -NC_SWIPE_PX / 2;
      if (open) for (const o of row.parentNode.querySelectorAll(".is-swiped")) o.classList.remove("is-swiped");
      row.classList.toggle("is-swiped", open);
    };
    row.addEventListener("touchend", end); row.addEventListener("touchcancel", end);
  }
  function ncRow(it, o) {
    const compact = !!o.compact;
    const row = el("div", "nc-row " + (it.read ? "is-read" : "is-unread") + (o.selected ? " is-sel" : "") + (compact ? " nc-compact" : ""));
    row.dataset.id = it.id; row.dataset.origin = it.origin || "";
    row.setAttribute("role", "listitem");
    const rowin = el("div", "nc-rowin");
    let dot;
    if (compact) { dot = el("span", "nc-dot"); dot.setAttribute("aria-hidden", "true"); dot.appendChild(el("span", "nc-dotmark")); }
    else {
      dot = el("button", "nc-dot nc-dot-btn"); dot.type = "button";
      dot.setAttribute("aria-label", it.read ? "Mark unread" : "Mark read");
      dot.title = it.read ? "Mark unread" : "Mark read";
      dot.appendChild(el("span", "nc-dotmark"));
      dot.addEventListener("click", (ev) => { ev.stopPropagation(); ncSetRead(it.id, !it.read); });
    }
    const main = el("button", "nc-main"); main.type = "button";
    const l1 = el("span", "nc-l1");
    l1.appendChild(ncChips(it));
    l1.appendChild(el("span", "nc-time", ncWhen(it.at, false)));
    main.appendChild(l1);
    main.appendChild(el("span", "nc-title", it.title || "(no title)"));
    if (!compact && it.preview) main.appendChild(el("span", "nc-preview", it.preview));
    if (it.read === false) main.appendChild(el("span", "nc-sr", " (unread)"));
    main.addEventListener("click", () => {
      if (row.classList.contains("is-swiped")) { row.classList.remove("is-swiped"); return; }
      o.onOpen(it, main);
    });
    rowin.append(dot, main);
    if (!compact) {
      const del = el("button", "nc-del"); del.type = "button";
      del.setAttribute("aria-label", "Delete"); del.title = "Delete";
      del.appendChild(ncIcon("trash"));
      del.addEventListener("click", (ev) => { ev.stopPropagation(); ncDelete(it.id); });
      rowin.appendChild(del);
    }
    row.appendChild(rowin);
    if (!compact) {
      const sw = el("button", "nc-swipe-del", "Delete"); sw.type = "button";
      sw.addEventListener("click", (ev) => { ev.stopPropagation(); ncDelete(it.id); });
      row.insertBefore(sw, rowin);
      ncSwipe(row, rowin);
    }
    return row;
  }
  // the cold start: placeholder rows in the row's own shape (never a bare word), the word kept for a screen reader
  function ncSkeleton(n, compact) {
    const box = el("div", "nc-skel" + (compact ? " nc-skel-compact" : ""));
    box.setAttribute("aria-busy", "true");
    box.appendChild(el("span", "nc-sr", "Loading…"));
    for (let i = 0; i < n; i++) {
      const row = el("div", "nc-skelrow"); row.setAttribute("aria-hidden", "true");
      const lines = el("span", "nc-skellines");
      lines.append(el("span", "nc-skelbar nc-skel-chip"), el("span", "nc-skelbar nc-skel-title"));
      if (!compact) lines.appendChild(el("span", "nc-skelbar nc-skel-prev"));
      row.append(el("span", "nc-skeldot"), lines);
      box.appendChild(row);
    }
    return box;
  }
  function ncIcon(name) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "14"); svg.setAttribute("height", "14");
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); svg.setAttribute("aria-hidden", "true");
    const paths = {
      trash: ["M3 6h18", "M8 6V4h8v2", "M6 6l1 14h10l1-14", "M10 11v6", "M14 11v6"],
      bellslash: ["M18 8a6 6 0 0 0-9.3-5", "M6 8c0 7-3 9-3 9h14", "M13.7 21a2 2 0 0 1-3.4 0", "M2 2l20 20"],
      mail: ["M4 5h16v14H4z", "M4 7l8 6 8-6"],
      back: ["M15 6l-6 6 6 6"],
    }[name] || [];
    for (const d of paths) { const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); svg.appendChild(p); }
    return svg;
  }

  // ---- the strip -----------------------------------------------------------------------------------------------------
  function ncStrip() {
    const u = nc.status === "ok" ? nc.unread : 0;
    for (const n of document.querySelectorAll("[data-nc-unread]")) {
      n.hidden = u <= 0; n.textContent = u > 99 ? "99+" : String(u);
      n.title = u + " unread";
    }
  }
  function ncNeedsCount(n) {
    for (const c of document.querySelectorAll("[data-nc-needs]")) { c.hidden = !(n > 0); c.textContent = n > 99 ? "99+" : String(n); c.title = n + " waiting on you"; }
  }
  // The app-icon badge heals on every Needs-you render (the same count the strip shows; cleared at 0), so a push that carried a
  // stale number is corrected the moment the page opens. Only when the decisions panel carried its lines: a panel that did not
  // read says nothing, never a 0. Notices never count. Feature-detected and caught.
  function syncAppBadge(view, n) {
    const dec = (view && view.extra_panels || {}).decisions;
    if (!dec || !Array.isArray(dec.lines)) return;
    try {
      const nav = window.navigator;
      if (!nav) return;
      const r = n > 0 ? (typeof nav.setAppBadge === "function" ? nav.setAppBadge(n) : null)
                      : (typeof nav.clearAppBadge === "function" ? nav.clearAppBadge() : null);
      if (r && typeof r.catch === "function") r.catch(() => {});
    } catch (_) { /* a badge that cannot be set is not an error the page shows */ }
  }

  // ---- the panel (the five newest, compact) ----------------------------------------------------------------------------
  function ncPanel(p) {
    const h = p.querySelector("h2");
    if (h) h.textContent = "Notifications";
    const head = p.querySelector(".phead");
    const count = el("span", "nc-panel-count"); count.hidden = true;
    const expand = el("button", "pexpand"); expand.type = "button";
    expand.title = "focus this panel"; expand.setAttribute("aria-label", "expand Notifications to a focus view");
    expand.appendChild(el("span", "pexpand-ico", "⤢"));
    expand.addEventListener("click", () => openNotifyCenter(expand));
    if (head) {
      for (const old of head.querySelectorAll(".pexpand, .ck-count")) old.remove();   // the generic panel's own expand opened the old record
      head.append(count, expand);
    }
    const body = el("div", "pbody nc-pbody");
    p.appendChild(body);
    p.classList.add("nc-panel");
    p._ncFill = () => {
      const u = nc.status === "ok" ? nc.unread : 0;
      count.hidden = u <= 0; count.textContent = u + " unread";
      body.replaceChildren();
      if (nc.status === "idle") { body.appendChild(ncSkeleton(3, true)); return; }
      if (nc.status !== "ok") { body.appendChild(el("p", "empty", ncStateWords())); return; }
      if (!nc.items.length) { body.appendChild(el("p", "empty", "No notifications")); return; }
      const list = el("div", "nc-list"); list.setAttribute("role", "list");
      for (const it of nc.items.slice(0, NC_PANEL_ROWS)) list.appendChild(ncRow(it, { compact: true, onOpen: (x, opener) => openNotifyCenter(opener, x.id) }));
      body.appendChild(list);
      const all = el("button", "nc-openall", "Open all (" + nc.total + ")"); all.type = "button";
      all.addEventListener("click", () => openNotifyCenter(all));
      body.appendChild(all);
    };
    ncPanels.add(p);
    p._ncFill();
    return p;
  }
  function ncStateWords() {
    return nc.status === "noauth" ? "Open the cockpit with its token to read notifications."
      : nc.status === "absent" ? "This cockpit does not serve notifications."
      : "Couldn’t load notifications.";
  }

  // ---- the focus view --------------------------------------------------------------------------------------------------
  function ncMakeRoot() {
    const root = el("div", "nc-root");
    const bar = el("div", "nc-bar");
    bar.appendChild(el("span", "nc-unread"));
    const fl = el("label", "nc-filterbox");
    const filter = el("input", "nc-filter"); filter.type = "search"; filter.placeholder = "Filter"; filter.autocomplete = "off";
    filter.setAttribute("aria-label", "Filter notifications"); filter.value = ncUi.filter;
    filter.addEventListener("input", () => { ncUi.filter = filter.value; ncFillList(); });
    fl.appendChild(filter);
    bar.appendChild(fl);
    const markall = el("button", "nc-btn nc-markall", "Mark all read"); markall.type = "button";
    markall.addEventListener("click", () => { ncMarkAll(); });
    bar.appendChild(markall);
    const more = el("div", "nc-more");
    const mb = el("button", "nc-btn nc-morebtn", "⋯"); mb.type = "button";
    mb.setAttribute("aria-haspopup", "menu"); mb.setAttribute("aria-expanded", "false"); mb.setAttribute("aria-label", "More");
    mb.addEventListener("click", (ev) => { ev.stopPropagation(); ncUi.menu = !ncUi.menu; ncFillBar(); });
    const menu = el("div", "nc-menu"); menu.setAttribute("role", "menu"); menu.hidden = true;
    const mm = el("button", "nc-menuitem nc-menu-mark", "Mark all read"); mm.type = "button"; mm.setAttribute("role", "menuitem");
    mm.addEventListener("click", () => { ncUi.menu = false; ncFillBar(); ncMarkAll(); });
    const mc = el("button", "nc-menuitem nc-menu-clear", "Clear all"); mc.type = "button"; mc.setAttribute("role", "menuitem");
    mc.addEventListener("click", () => { ncClear(); });
    menu.append(mm, mc);
    more.append(mb, menu);
    bar.appendChild(more);
    root.appendChild(bar);
    const banner = el("div", "nc-banner"); banner.setAttribute("role", "alert"); banner.hidden = true;
    root.appendChild(banner);
    const split = el("div", "nc-split");
    const lp = el("div", "nc-listpane");
    const needs = el("button", "nc-needsline"); needs.type = "button"; needs.hidden = true;
    needs.addEventListener("click", () => { closeModal(); phoneJump("needs", needs); });
    const list = el("div", "nc-list"); list.setAttribute("role", "list");
    lp.append(needs, list);
    const dp = el("div", "nc-detailpane");
    const back = el("button", "nc-back"); back.type = "button";
    back.append(ncIcon("back"), document.createTextNode(" Notifications"));
    back.addEventListener("click", () => { ncUi.detail = false; ncPaint(); });
    const detail = el("div", "nc-detail");
    dp.append(back, detail);
    split.append(lp, dp);
    root.appendChild(split);
    root.addEventListener("click", (ev) => { if (ncUi.menu && !ev.target.closest(".nc-more")) { ncUi.menu = false; ncFillBar(); } });
    // capture phase: the gesture is seen before the control it lands on acts (a row's click may select another item)
    root.addEventListener("pointerdown", ncGesture, true);
    root.addEventListener("keydown", ncGesture, true);
    root.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && ncUi.menu) { ev.preventDefault(); ev.stopPropagation(); ncUi.menu = false; ncFillBar(); mb.focus(); } });
    root._nc = { bar, filter, markall, mb, menu, mm, mc, banner, needs, list, detail, split, lp, dp };
    return root;
  }
  const ncLive = () => !!ncRoot && ncRoot.isConnected && !!document.getElementById("levain-modal");
  function ncFillBar() {
    if (!ncLive()) return;
    const r = ncRoot._nc, has = nc.status === "ok" && nc.total > 0, unread = nc.status === "ok" ? nc.unread : 0;
    const u = r.bar.querySelector(".nc-unread");
    u.textContent = unread > 0 ? unread + " unread" : ""; u.hidden = unread <= 0;
    r.markall.hidden = unread <= 0;
    r.mm.hidden = unread <= 0; r.mc.hidden = !has;
    r.mb.parentNode.hidden = !has;
    r.menu.hidden = !(ncUi.menu && has);
    r.mb.setAttribute("aria-expanded", ncUi.menu && has ? "true" : "false");
  }
  function ncFillBanner() {
    if (!ncLive()) return;
    const r = ncRoot._nc, b = ncUi.banner;
    r.banner.replaceChildren(); r.banner.hidden = !b;
    if (!b) return;
    r.banner.appendChild(el("span", "nc-banner-t", b.text));
    if (b.retry) { const rt = el("button", "nc-btn", "Retry"); rt.type = "button"; rt.addEventListener("click", () => { ncRead(true); }); r.banner.appendChild(rt); }
    const x = el("button", "nc-banner-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Dismiss");
    x.addEventListener("click", () => { ncUi.banner = null; ncFillBanner(); });
    r.banner.appendChild(x);
  }
  function ncMatches(it) {
    const q = ncUi.filter.trim().toLowerCase();
    if (!q) return true;
    return [it.title, it.preview, it.source].some((s) => typeof s === "string" && s.toLowerCase().indexOf(q) >= 0);
  }
  // a repaint of the list keeps the reader's place: the scroll, and focus on the same control of the same row
  function ncFillList() {
    if (!ncLive()) return;
    const r = ncRoot._nc, list = r.list, top = list.scrollTop;
    const act = document.activeElement, keep = act && list.contains(act) && act.closest(".nc-row") ? [act.closest(".nc-row").dataset.id, act.classList.contains("nc-dot-btn") ? ".nc-dot-btn" : act.classList.contains("nc-del") ? ".nc-del" : ".nc-main"] : null;
    r.needs.hidden = !(nc.status === "ok" && nc.needs > 0);
    r.needs.textContent = "Needs you (" + nc.needs + ") · open the cards →";
    list.replaceChildren();
    if (nc.status === "idle") {
      list.appendChild(ncSkeleton(6, false));
    } else if (nc.status !== "ok") {
      const s = el("div", "nc-state"); s.appendChild(el("p", null, ncStateWords()));
      if (nc.status === "error") { const rt = el("button", "nc-btn", "Retry"); rt.type = "button"; rt.addEventListener("click", () => { ncRead(true); }); s.appendChild(rt); }
      list.appendChild(s);
    } else if (!nc.items.length) {
      const s = el("div", "nc-state"); s.appendChild(ncIcon("bellslash")); s.appendChild(el("h3", null, "No notifications"));
      s.appendChild(el("p", null, "Levain’s pings, notices and what your services post land here.")); list.appendChild(s);
    } else {
      const shown = nc.items.filter(ncMatches);
      if (!shown.length) list.appendChild(el("p", "nc-nomatch", "Nothing matches “" + ncUi.filter.trim() + "”."));
      for (const it of shown) list.appendChild(ncRow(it, { selected: it.id === ncUi.sel, onOpen: (x) => ncSelect(x.id) }));
      if (nc.next) {
        const m = el("button", "nc-btn nc-older", ncUi.more ? "Loading…" : "Load older"); m.type = "button"; m.disabled = ncUi.more;
        m.addEventListener("click", ncMore); list.appendChild(m);
      }
    }
    list.scrollTop = top;
    if (keep) {
      const row = Array.prototype.find.call(list.querySelectorAll(".nc-row"), (n) => n.dataset.id === keep[0]);
      const c = row && row.querySelector(keep[1]);
      if (c) c.focus({ preventScroll: true });
    }
  }
  // A flow Needs-you ping's detail (S4MD-2): the ask first, then the words. `it.needs` is the center's reading of the ping
  // (levain/webpush/center.py needs_item): flow's structured fields when the ping carries them, else its push body read back.
  // No link is taken from the ping: the center hands over a sign request id and a version only for a request it read as this
  // item's and found open (`sign_state` "open"), and the page builds the link on its own origin, so an option row can only
  // ever open this cockpit's own sign page, where Face ID signs. Any other state draws the rows inert with a line saying why.
  // Text only; the words go through the same renderer, linkify and harden as every notification.
  const NC_SIGN_ID = /^[A-Za-z0-9_-]{8,64}$/, NC_SIGN_V = /^[0-9a-f]{16}$/, NC_PICK = /^[a-z0-9]{1,8}$/;
  // the cockpit sign page for request `id` on this page's origin, in the link form flow's pushes carry (/sign/<id>#sign=<id>
  // &v=<version>, the pick adding &choice=<key>; sign.js reads v= and choice=); null for any part off its grammar
  function ncSignHref(id, v, choice) {
    if (!NC_SIGN_ID.test(id || "")) return null;
    let h = location.origin + "/sign/" + id;
    if (v === undefined || v === null) return h;
    if (!NC_SIGN_V.test(v)) return null;
    h += "#sign=" + id + "&v=" + v;
    if (choice === undefined || choice === null) return h;
    return NC_PICK.test(choice) ? h + "&choice=" + choice : null;
  }
  const NC_SIGN_NOTE = {
    open: "An option opens the sign page for that option; you sign there with Face ID. Nothing is sent from here.",
    closed: "This sign request is closed: it was answered or withdrawn.",
    expired: "This sign request has expired.",
    unknown: "Could not check this request; open it at the sign page.",
    unverified: "Could not verify this request; open it on the Needs-you card.",
  };
  function ncDomain(href) { try { return new URL(href).hostname.replace(/^www\./, ""); } catch (_) { return ""; } }
  // the one closing sentence a flat list may carry on its last item: split off only at a boundary that cannot be prose inside
  // a bullet (a blank line, or a closing mark and then a sentence opening with flow's closer words); anything else stays whole
  const NC_CLOSER = /(?:\n[ \t]*\n|[)\]`*.!?:][ \t]+)(?=(?:Then answer|Answer \(|Pick)\b)/;
  function ncSplitCloser(last) {
    const m = NC_CLOSER.exec(last);
    if (!m) return null;
    return [last.slice(0, m.index + m[0].length).trimEnd(), last.slice(m.index + m[0].length)];
  }
  function ncNeedsBody(n) {
    const host = el("div", "nc-body nc-nbody");
    const CF = window.LevainCardFormat;
    if (n.from === "data" || !CF) {
      // real newlines: Markdown as written, less a first line that is the whole title (it is drawn above as the heading)
      const lines = String(n.what || "").replace(/\r\n?/g, "\n").split("\n");
      const i = lines.findIndex((x) => x.trim());
      if (n.from === "data" && i >= 0 && lines[i].trim() === n.headline) lines.splice(i, 1);
      host.appendChild(renderMarkdown(lines.join("\n").trim()));
    } else {
      const rec = n.rec || "";
      for (const sec of CF.sections(n.what, n.headline)) {
        if (CF.repeatsRows(sec, n.options, rec)) continue;
        if (sec.label) host.appendChild(el("div", "nc-blabel", sec.label.charAt(0) + sec.label.slice(1).toLowerCase()));
        for (const b of sec.blocks) {
          if (b.kind === "list") {
            if (b.lead) host.appendChild(renderMarkdown(b.lead));
            // the card format ends a list only at a "Label: ..." sentence, so a closing sentence rides on the last item
            const items = b.items.slice();
            const cut = items.length ? ncSplitCloser(items[items.length - 1]) : null;
            if (cut) items[items.length - 1] = cut[0];
            const ul = el("ul", "nc-nlist");
            for (const t of items) { const li = el("li"); ncInline(li, t); ul.appendChild(li); }
            host.appendChild(ul);
            if (cut && cut[1]) host.appendChild(renderMarkdown(cut[1]));
          } else host.appendChild(renderMarkdown(b.text));
        }
      }
    }
    ncLinkify(host);
    ncHarden(host);
    for (const sp of Array.from(host.querySelectorAll(".nc-fullurl"))) {      // his rule: a link shows its domain, the page shows the rest
      const a = sp.previousElementSibling;
      const dom = a && a.getAttribute("href") ? ncDomain(a.getAttribute("href")) : "";
      if (dom) sp.textContent = " (" + dom + ")"; else sp.remove();
    }
    return host;
  }
  // the generic detail's tail, shared by the needs branch: the way to the Needs-you card while the item waits, and a token's note
  function ncNeedsBox(it) {
    if (!(it.needs_ref && it.needs_open)) return null;
    const nd = el("div", "nc-needsbox");
    const go = el("button", "nc-needsgo", "Answer on the Needs-you card →"); go.type = "button";
    go.addEventListener("click", () => ncGoNeeds(it.needs_ref, go));
    nd.appendChild(go);
    nd.appendChild(el("p", "nc-note", "Still waiting on you. Deleting this ping does not close the item."));
    return nd;
  }
  // An approval ping about a filed consent request (levain/webpush/center.py consent_sheet): the way to its sign sheet. The
  // center hands over the request id and its standing, never a link; the link is built here on the page's own origin from
  // the id alone (the sheet's route is /sign/consent/<id>), so it can only open this cockpit's sheet, where the passkey
  // ceremony is the only approval. Nothing is sent from a tap on this page (Phill: no answering from notifications).
  const NC_SHEET_ID = /^[A-Za-z0-9._:-]{1,160}$/;
  // The wording names no answer: the sheet offers Allow and Deny, or Deny only (a passkey on a phone cannot Allow an
  // agent-configuration request, which is T3), and this page does not know which.
  const NC_SHEET_NOTE = {
    open: "Opens the sign sheet for this request; you answer there with Face ID. Nothing is sent from here.",
    closed: "Already decided: this request was answered, ruled or withdrawn.",
    expired: "This request's hold has ended: it can no longer be answered here.",
    gone: "This request is no longer in the queue.",
    unread: "Could not check this request; open the sign sheet to see where it stands.",
  };
  function ncSheetBox(it) {
    const sh = it.sheet;
    // an id made only of dots would resolve as a path segment ("." / ".."), so it is no id
    if (!sh || typeof sh.id !== "string" || !NC_SHEET_ID.test(sh.id) || /^\.+$/.test(sh.id)) return null;
    const nd = el("div", "nc-sheetbox");
    const href = location.origin + "/sign/consent/" + encodeURIComponent(sh.id);
    if (sh.state === "open") {
      const go = el("a", "nc-signgo", "Review with Face ID");
      go.setAttribute("href", href); go.setAttribute("rel", "noopener noreferrer");
      nd.appendChild(go);
    }
    const known = typeof sh.state === "string" && Object.prototype.hasOwnProperty.call(NC_SHEET_NOTE, sh.state);   // never an inherited name
    const state = known ? sh.state : "unread";
    const note = el("p", "nc-note" + (state === "open" ? "" : " nc-signstate"), NC_SHEET_NOTE[state]);
    if (state === "unread") {
      const a = el("a", "nc-link", "Open the sign sheet");
      a.setAttribute("href", href); a.setAttribute("rel", "noopener noreferrer"); note.append(" ", a);
    }
    nd.appendChild(note);
    return nd;
  }
  // a prompt notice (the agent's own dialog waits; no hold, so no sheet): say plainly what it is, and offer nothing to press
  function ncDialogBox(it) {
    const dg = it.dialog;
    if (!dg || typeof dg !== "object") return null;
    const who = typeof dg.agent === "string" && dg.agent ? dg.agent : "An agent";
    const nd = el("div", "nc-dialogbox");
    nd.appendChild(el("p", "nc-dialoghead", "Left to the seat's own dialog"));
    nd.appendChild(el("p", "nc-note", who + " asked in its own dialog. Levain did not hold it, so there was nothing to answer here: if it is still open, answer it in that seat's dialog at the laptop."));
    return nd;
  }
  function ncTokenNote(it) {
    return it.origin === "token" ? el("p", "nc-note nc-tokennote", "Posted by notify token " + (it.token_name || "") + ": every link shows its full address.") : null;
  }
  // the folded Detail's summary line: the body's first block as read (a list's first item; a section label kept as "Label: ")
  function ncFirstLine(body) {
    const kids = Array.from(body.children);
    for (let i = 0; i < kids.length; i++) {
      const k = kids[i];
      if (k.classList.contains("nc-blabel")) continue;
      const text = ((k.tagName === "UL" || k.tagName === "OL") && k.firstElementChild ? k.firstElementChild : k).textContent.trim().split("\n")[0];
      if (!text) continue;
      const lab = i > 0 && kids[i - 1].classList.contains("nc-blabel") ? kids[i - 1].textContent.trim() : "";
      return lab ? lab + ": " + text : text;
    }
    return "";
  }
  const NC_FOLD_LINES = 12;     // a Detail longer than this many rendered lines starts folded, so the ask and Stopped stay on the first screen
  function ncNeedsDetail(d, it, n) {
    const meta = el("div", "nc-dmeta");
    meta.appendChild(el("span", "nc-chip nc-repo", n.repo));
    meta.appendChild(el("span", "nc-time", ncWhen(it.at, true)));
    d.appendChild(meta);
    d.appendChild(el("h2", "nc-dtitle", n.title || it.title || "(no title)"));
    const ask = el("section", "nc-ask"); ask.setAttribute("aria-label", "Your answer");
    const state = n.sign_state || null;
    const live = state === "open" && !!ncSignHref(n.sign_id, n.sign_v);
    const opts = Array.isArray(n.options) ? n.options : [];
    if (opts.length) {
      ask.appendChild(el("div", "nc-blabel", "Your answer"));
      const list = el("ul", "nc-picks");
      for (const o of opts) {
        const isRec = !!n.rec && o.key === n.rec;
        const href = live ? ncSignHref(n.sign_id, n.sign_v, o.key) : null;
        const li = el("li", "nc-pickitem");
        const row = el(href ? "a" : "div", "nc-pick" + (isRec ? " is-rec" : "") + (href ? "" : " is-inert"));
        if (href) { row.setAttribute("href", href); row.setAttribute("rel", "noopener noreferrer"); }
        row.appendChild(el("span", "nc-pickkey", o.key));
        row.appendChild(el("span", "nc-picktext", o.text));
        if (isRec) row.appendChild(el("span", "nc-recmark", "✓ Rec"));
        li.appendChild(row);
        if (isRec && n.rec_reason) li.appendChild(el("p", "nc-recwhy", n.rec_reason));
        list.appendChild(li);
      }
      ask.appendChild(list);
    } else if (live) {
      const go = el("a", "nc-signgo", "Sign your answer");      // no options: the sign page is the one route, so it is the primary button
      go.setAttribute("href", ncSignHref(n.sign_id, n.sign_v)); go.setAttribute("rel", "noopener noreferrer");
      ask.appendChild(go);
    }
    const why = live ? (opts.length ? NC_SIGN_NOTE.open : "") : state ? (NC_SIGN_NOTE[state] || NC_SIGN_NOTE.unknown) : opts.length ? "No sign page for this item: answer at the laptop." : "";
    if (why) {
      const p = el("p", "nc-note" + (live ? "" : " nc-signstate"), why);
      const page = state === "unknown" ? ncSignHref(n.sign_id) : null;
      if (page) { const a = el("a", "nc-link", "Open the sign page"); a.setAttribute("href", page); a.setAttribute("rel", "noopener noreferrer"); p.append(" ", a); }
      ask.appendChild(p);
    }
    if (ask.childNodes.length) d.appendChild(ask);
    if (n.stopped || (n.sign_id && !n.stopped_known)) {
      const w = el("section", "nc-nsec nc-quiet"); w.appendChild(el("div", "nc-blabel", "Stopped"));
      w.appendChild(el("p", "nc-nstop", n.stopped || "not available")); d.appendChild(w);
    }
    const body = ncNeedsBody(n);
    if (body.childNodes.length) {
      const det = el("details", "nc-nsec nc-ndetail");
      const sum = el("summary", "nc-nsum");
      sum.appendChild(el("span", "nc-blabel", "Detail"));
      sum.appendChild(el("span", "nc-dfirst", ncFirstLine(body)));
      det.append(sum, body);
      det.open = true;
      d.appendChild(det);
      const kept = ncUi.nfold && ncUi.nfold.id === it.id ? ncUi.nfold.open : null;
      if (kept !== null) det.open = kept;
      else {
        const lh = parseFloat(getComputedStyle(body).lineHeight) || 24, h = body.getBoundingClientRect().height;
        const lines = h > 0 ? h / lh : (body.textContent || "").length / 45;
        det.open = lines <= NC_FOLD_LINES;
      }
      sum.addEventListener("click", () => { ncUi.nfold = { id: it.id, open: !det.open }; });
    }
    for (const x of [ncNeedsBox(it), ncTokenNote(it)]) if (x) d.appendChild(x);
    const dv = ncDelivery(it.delivery);
    if (dv) d.appendChild(el("p", "nc-delivery", "Delivery: " + dv));
    const foot = el("div", "nc-tools nc-foot");
    const tg = el("button", "nc-btn nc-toggle nc-ghost"); tg.type = "button"; tg.dataset.tool = "toggle";
    tg.append(ncIcon("mail"), document.createTextNode(it.read ? " Mark unread" : " Mark read"));
    tg.addEventListener("click", () => { ncSetRead(it.id, !it.read); });
    const dl = el("button", "nc-btn nc-delete nc-ghost"); dl.type = "button"; dl.dataset.tool = "delete";
    dl.append(ncIcon("trash"), document.createTextNode(" Delete"));
    dl.addEventListener("click", () => { ncDelete(it.id); });
    foot.append(tg, dl);
    d.appendChild(foot);
  }
  function ncFillDetail() {
    if (!ncLive()) return;
    const r = ncRoot._nc, d = r.detail;
    r.split.dataset.detail = ncUi.detail ? "1" : "0";
    ncRoot.dataset.detail = ncUi.detail ? "1" : "0";
    const it = ncUi.sel ? ncFind(ncUi.sel) : null;
    const act = document.activeElement, keepTool = act && d.contains(act) ? act.dataset.tool || null : null;
    d.replaceChildren();
    if (ncUi.gone) d.appendChild(el("p", "nc-gone", ncUi.gone));
    if (!it) { if (!ncUi.gone) d.appendChild(el("p", "nc-none", "Nothing selected.")); return; }
    if (it.needs && Array.isArray(it.needs.options)) {
      ncNeedsDetail(d, it, it.needs);
      if (keepTool) { const c = d.querySelector('[data-tool="' + keepTool + '"]'); if (c) c.focus({ preventScroll: true }); }
      return;
    }
    const meta = el("div", "nc-dmeta");
    meta.appendChild(ncChips(it));
    meta.appendChild(el("span", "nc-time", ncWhen(it.at, true)));
    d.appendChild(meta);
    d.appendChild(el("h2", "nc-dtitle", it.title || "(no title)"));
    const tools = el("div", "nc-tools");
    const tg = el("button", "nc-btn nc-toggle"); tg.type = "button"; tg.dataset.tool = "toggle";
    tg.append(ncIcon("mail"), document.createTextNode(it.read ? " Mark unread" : " Mark read"));
    tg.addEventListener("click", () => { ncSetRead(it.id, !it.read); });
    const dl = el("button", "nc-btn nc-delete"); dl.type = "button"; dl.dataset.tool = "delete";
    dl.append(ncIcon("trash"), document.createTextNode(" Delete"));
    dl.addEventListener("click", () => { ncDelete(it.id); });
    tools.append(tg, dl);
    d.appendChild(tools);
    if (typeof it.image_url === "string" && /^\/[^/\\]/.test(it.image_url)) { const im = el("img", "nc-img"); im.src = it.image_url; im.alt = ""; d.appendChild(im); }
    const sheetBox = ncSheetBox(it);      // an approval's way to its sign sheet, above the words
    if (sheetBox) d.appendChild(sheetBox);
    const dialogBox = ncDialogBox(it);
    if (dialogBox) d.appendChild(dialogBox);
    if (it.body) d.appendChild(ncBody(it.body));
    for (const x of [ncNeedsBox(it), ncTokenNote(it)]) if (x) d.appendChild(x);
    const dv = ncDelivery(it.delivery);
    if (dv) d.appendChild(el("p", "nc-delivery", "Delivery: " + dv));
    if (keepTool) { const c = d.querySelector('[data-tool="' + keepTool + '"]'); if (c) c.focus({ preventScroll: true }); }
  }
  // the card the ping is about: the focus view of that Needs-you item, else the strip (never an answer control here)
  function ncGoNeeds(ref, opener) {
    closeModal();
    try {
      const lines = ((currentView && currentView.extra_panels || {}).decisions || {}).lines;
      const it = needsWaiting(needsItems(lines)).find((x) => needsIdOf(x) === ref);
      if (it) { openNeedsFocus(it, opener && opener.isConnected ? opener : null); return; }
    } catch (_) { /* fall through to the strip */ }
    phoneJump("needs", opener);
  }
  function ncFillAll() { ncFillBar(); ncFillBanner(); ncFillList(); ncFillDetail(); }
  function ncPaint() {
    ncStrip();
    for (const p of Array.from(ncPanels)) { if (!p.isConnected) ncPanels.delete(p); else p._ncFill(); }
    ncFillAll();
  }
  // opens the focus view; `id` (a panel row, or a push tap's `n`) selects that item: a panel row marks it read, a deep link
  // (`byLink`, the page's address) waits for a gesture inside the view (ncSelect)
  function openNotifyCenter(opener, id, byLink) {
    ncUi.filter = ""; ncUi.menu = false; ncUi.sel = null; ncUi.pinned = null; ncUi.gone = ""; ncUi.detail = false;
    if (ncUi.banner && ncUi.banner.kind === "write") ncUi.banner = null;
    ncDeepPending = id && NC_ID.test(id) ? id : null;
    ncDeepLink = !!byLink; ncReadArmed = null;
    modalCustom = { id: "notifications", page: true, title: "Notifications", build: () => { ncRoot = ncMakeRoot(); return ncRoot; } };
    modalGlance = false;
    modalKey = "custom:notifications";
    modalReturnFocus = opener || null;
    modalEpisodeQuery = "";
    buildModal(null);
    ncFillAll();
    if (ncDeepPending) ncDeepTry();
    ncRead(true);
  }
  function ncBoot() {
    ncStrip();
    const ask = ncUrlAsk();
    window.setTimeout(() => { ncRead(false); }, 600);
    window.setTimeout(() => { if (nc.status !== "ok") ncRead(false); }, 3000);
    window.setInterval(() => { if (!document.hidden) ncRead(false); }, NC_POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) ncRead(false); });
    // a token that arrives after the page opened (a tap on an unpaired page that then pairs, or the owner prompt) reads the center
    // at once, so a kept deep link opens its item now and not at the next 30 s pulse; same read as the pulse, no new timer.
    // `storage` fires only for another tab's change, the same-page announce is the levain:write-token-change event.
    window.addEventListener("levain:write-token-change", () => { ncRead(false); });
    window.addEventListener("storage", (e) => { if (e.key === "levain_write_token") ncRead(false); });
    if (ask && ask.needs && ask.item) needsItemJump(ask.item, ++phoneJumpRun, Date.now() + PHONE_JUMP_WAIT_MS);
    else if (ask && ask.needs) needsDeepJump(++phoneJumpRun, Date.now() + PHONE_JUMP_WAIT_MS);
    else if (ask) openNotifyCenter(null, ask.id, true);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ncBoot); else ncBoot();
  window.LevainNotifyCenter = { open: openNotifyCenter, state: () => ({ status: nc.status, unread: nc.unread, total: nc.total, needs: nc.needs }), read: ncRead };

  window.LevainAgentPage = { open: openAgentPage };
  window.LevainNeeds = { items: (lines) => needsWaiting(needsItems(lines)), name: needsName, summary: needsSummary, go: needsGo, canGoFirst: needsCanGoFirst, goFirst: needsGoFirst, history: openDecisionsHistory };
  window.LevainLoops = { find: loopFind, go: loopGo, why: () => loopWhy };
  window.LevainDashboard = { dayYearWords, render: renderKeepingPlace, redraw: ckRedraw, openCustom, composeDrop, adoptPanel, captureField, restoreField, captureKept, settleKept, buildKept: () => buildKept, renderPanel: renderPanelSafe, measureClauses, measureOverflow, hasUnsavedEdit, editHoldsRefresh, dropBoxText, setDropBoxText, dropBoxIsOff, jobsActive: () => _activeJobs.size > 0, fitLineTile, lineTileCount: () => lineFits.size, noteBodyCount: () => noteBodies.size };
})();
