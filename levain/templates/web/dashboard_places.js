// levain dashboard_places.js: the places bar of the cockpit page (levain#141: Home plus a bar of places).
// It draws nothing of its own data and changes no renderer: every panel is the page's own. What it does:
//   - names the place on the body (data-ia-place) from the page's own state (dv-on, rv-on, data-mode), which dashboard.css reads;
//   - maps the places bar and the 1-5 keys onto the page's existing wiring (data-mode-tab, data-phone-jump) through the hidden drivers;
//   - Morning and Build are retired in the page's own applyMode (cockpit_view.js), which resolves either to Home, so nothing here
//     redirects after the fact;
//   - Settings (key 5) is the board page with body.ia-settings-on; dashboard.css draws only the settings sections there, and
//     window.LevainPlaces.openSettings(id) is how a link to one of them lands;
//   - folds the agents graph to a one-line strip that opens on a click;
//   - adds the section heads Home groups its blocks under.
(function () {
  "use strict";

  function clickHidden(sel) {
    var n = document.querySelector(sel);
    if (n) n.click();
  }
  function mode() { return document.body.dataset.mode || "board"; }
  function deskOn() { return document.body.classList.contains("dv-on"); }
  function roomOn() { return document.body.classList.contains("rv-on"); }
  // Settings is the one place the page has no state of its own for: it is the board page (mode board, so every panel's own read keeps
  // running underneath) with this body class on, which only go("settings") sets. The Desk, the Room and any other mode leave it.
  var SETTINGS_ON = "ia-settings-on";
  function settingsOn() { return document.body.classList.contains(SETTINGS_ON); }

  // ---- the place, derived from the page's own state ----
  function place() {
    if (roomOn()) return "room";
    if (deskOn()) return "desk";
    if (mode() === "memory") return "memory";
    if (settingsOn()) return "settings";
    return "home";
  }
  var remeasure = null;   // trackMasthead's measure: the place decides whether the places bar is stuck (Home's grid holds it in its row)
  function paint() {
    // the flag is only ever true on the board page with no view up: anything else that changed the page's own state ends it
    if (settingsOn() && (roomOn() || deskOn() || mode() !== "board")) document.body.classList.remove(SETTINGS_ON);
    var p = place();
    if (document.body.dataset.iaPlace !== p) { document.body.dataset.iaPlace = p; if (remeasure) remeasure(); queueHeads(); }
    // scoped to the places bar: other controls carry aria-current of their own
    var bar = document.querySelectorAll(".ia-places [data-ia-place-btn]");
    for (var i = 0; i < bar.length; i++) {
      if (bar[i].dataset.iaPlaceBtn === p) bar[i].setAttribute("aria-current", "page"); else bar[i].removeAttribute("aria-current");
    }
  }

  // ---- going to a place ----
  function go(where) {
    document.body.classList.remove(SETTINGS_ON);
    if (where === "settings") {
      clickHidden("[data-ia-mode=board]");   // the page's own wiring leaves the Desk, the Room or Memory for the board page first
      document.body.classList.add(SETTINGS_ON);
      paint();
      return;
    }
    if (where === "home") return clickHidden("[data-ia-mode=board]");
    if (where === "desk") return clickHidden("[data-ia-jump=desk]");
    if (where === "room") return clickHidden("[data-ia-jump=room]");
    if (where === "memory") return clickHidden("[data-ia-mode=memory]");
  }

  // A link to a settings section goes to the Settings place, then to the section. The section is drawn only once the place is up (a
  // rendered frame later), so the scroll waits for it.
  window.LevainPlaces = {
    place: place,
    go: go,
    openSettings: function (id) {
      go("settings");
      var tries = 0;
      (function land() {
        var n = id && document.getElementById(id);
        if (n && n.getClientRects().length) {
          try { n.scrollIntoView({ block: "start" }); n.focus({ preventScroll: true }); } catch (_) { /* no scrolling here */ }
          return;
        }
        if (++tries < 20) requestAnimationFrame(land);
      })();
    }
  };

  // The masthead's own actions are not places. Drop scrolls to a box Memory does not draw, so from Memory it goes Home first (the
  // attribute changes here; the page's jump scrolls and focuses a frame later, once the box is drawn). Grants and Notifications only
  // open a dialog, and the page's boot handler leaves the view they were pressed on as it is.
  document.addEventListener("click", function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest(".ia-act[data-phone-jump]") : null;
    if (!a) return;
    // the painted place, not place(): the attribute is repainted on the microtask after the state changes
    if (a.dataset.phoneJump === "drop" && document.body.dataset.iaPlace !== "home") go("home");
  });

  document.addEventListener("click", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    if (!t) return;
    var pb = t.closest("[data-ia-place-btn]");
    if (pb) { ev.preventDefault(); go(pb.dataset.iaPlaceBtn); return; }
    var g = t.closest("[data-ia-graph-toggle]");
    if (g) {
      var open = !document.body.classList.contains("ia-graph-open");
      document.body.classList.toggle("ia-graph-open", open);
      g.textContent = open ? "Hide the graphs" : "Show the graphs";
      window.dispatchEvent(new Event("resize"));   // the sky mounts at the size its card has; a card that just opened has one now
    }
  });

  // The places keys. Captured before the page's own mode keys, which would otherwise open the retired Morning and Build modes.
  // Never while typing, and never under a dialog: a dialog's keys are its own.
  var KEYS = { 1: "home", 2: "desk", 3: "room", 4: "memory", 5: "settings" };
  function dialogOpen() {
    if (document.querySelector(".modal-overlay")) return true;
    var ps = document.getElementById("pr-sheet");
    return !!(ps && ps.getClientRects().length);
  }
  document.addEventListener("keydown", function (ev) {
    if (ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey || !KEYS[ev.key]) return;
    if (dialogOpen()) return;
    var a = document.activeElement, tag = a && a.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (a && a.isContentEditable)) return;
    ev.stopImmediatePropagation();
    ev.preventDefault();
    go(KEYS[ev.key]);
  }, true);

  // ---- the agents graph strip: a toggle beside the hero card, outside every node the page repaints ----
  function addGraphToggle() {
    var col = document.getElementById("ck-skycol");
    if (!col || col.querySelector("[data-ia-graph-toggle]")) return;
    var b = document.createElement("button");
    b.type = "button";
    b.className = "ia-graph-toggle";
    b.setAttribute("data-ia-graph-toggle", "");
    b.setAttribute("aria-controls", "ck-skycard");   // the card it opens (the lens bar and the graph); the text flip is the state (a screen reader hears "Hide the graphs", not also "expanded")
    b.textContent = "Show the graphs";
    var row = document.getElementById("ck-mctl") || col;   // the Machine control row: Show/Hide the graphs, Calm, Ambient
    row.insertBefore(b, row.firstChild);
  }

  // ---- the section heads: children of the deck, which the page never repaints; dashboard.css orders and shows them on Home only ----
  var HEADS = [["desk", "Desk", "the desk's lease and seats; the drop box is your reply"],
    ["work", "Work", "the board and the List"],
    ["machine", "Machine", "agents, fleet, calls, stream, jobs"],
    ["glance", "At a glance", "ops, backup, memory"]];
  // levain#141 polish: each head is put in the DOCUMENT where Home draws it (the Desk head before the drop, the glance head before the
  // glance tiles, Work and Machine before the board), not at the deck's end, so the document order of the Home blocks is the drawn
  // order and Tab walks the page top to bottom. (dashboard.css still places them by `order`; the two agree.) A block missing from the
  // page leaves its head at the end, as before.
  var HEAD_BEFORE = { desk: "#ck-drop", glance: "#ck-widgets", work: "main.frame", machine: "main.frame" };
  function addHeads() {
    var deck = document.querySelector(".deck");
    if (!deck || deck.querySelector(".ia-head")) return;
    HEADS.forEach(function (h) {
      var d = document.createElement("div");
      d.className = "ia-head";
      d.dataset.iaHead = h[0];
      d.setAttribute("data-ia-empty", "1");   // hidden until the first sync finds a block under it, so no head flashes over an undrawn page
      var t = document.createElement("h3");
      t.textContent = h[1];
      var s = document.createElement("span");
      s.textContent = h[2];
      d.appendChild(t);
      d.appendChild(s);
      var anchor = HEAD_BEFORE[h[0]] ? deck.querySelector(":scope > " + HEAD_BEFORE[h[0]]) : null;
      if (anchor) deck.insertBefore(d, anchor); else deck.appendChild(d);
    });
  }

  // levain#164: a Home section head is shown only while a block it owns is drawn. A bare install (no desk, no flow-desk panel, no Machine
  // panels) would otherwise show "Desk" and "Machine" over nothing. dashboard.css orders each block under its head by the same selectors
  // (the `order` rules for the drop box, the flow-desk, board, tray, inbox, roster and so on); this map is the page's copy of that ownership.
  var HEAD_OWNS = {
    desk: ["#ck-drop", '.panel[data-ck-panel="flow-desk"]'],
    work: ['.panel[data-ck-panel="tray"]', '.panel[data-ck-panel="board"]', '.panel[data-ck-panel="inbox"]'],
    machine: ["#ck-skycol", '.panel[data-ck-panel="roster"]', '.panel[data-ck-panel="agents"]', '.panel[data-ck-panel="agent_calls"]',
      '.panel[data-ck-panel="stream"]', '.panel[data-ck-panel="scheduled_jobs"]', '.panel[data-ck-panel="jobs"]', '.panel[data-ck-panel="edits"]'],
    glance: ["#ck-widgets"]
  };
  // drawn = it has a box with some height (a hidden block has none, and an emptied container can have a box of no height)
  function drawn(el) {
    if (!el || !el.getClientRects) return false;
    var rs = el.getClientRects();
    for (var i = 0; i < rs.length; i++) if (rs[i].height > 0) return true;
    return false;
  }
  // only on Home: elsewhere the heads are not displayed and the blocks under them are not either, so nothing there can be measured
  function syncHeads() {
    if (document.body.dataset.iaPlace !== "home") return;
    var heads = document.querySelectorAll(".deck .ia-head");
    for (var i = 0; i < heads.length; i++) {
      var sels = HEAD_OWNS[heads[i].dataset.iaHead], any = false;
      if (!sels) { heads[i].removeAttribute("data-ia-empty"); continue; }   // a head this map does not know stays shown: never hide what is not understood
      for (var j = 0; j < sels.length && !any; j++) {
        var els = document.querySelectorAll(sels[j]);
        for (var k = 0; k < els.length && !any; k++) any = drawn(els[k]);
      }
      if (any) heads[i].removeAttribute("data-ia-empty"); else heads[i].setAttribute("data-ia-empty", "1");
    }
  }
  var headsQueued = false;
  function queueHeads() {
    if (headsQueued) return;
    headsQueued = true;
    var run = function () { headsQueued = false; syncHeads(); };
    if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(run); else window.setTimeout(run, 0);
  }

  // levain#141 polish: the masthead (and, on Home, the places bar under it) stays pinned while the page scrolls, on a screen at least
  // PIN_MIN_W wide AND PIN_MIN_H tall (a landscape phone is 932 x 430: the bars would take 40% of it). These two numbers are the ONE
  // source: dashboard.css mirrors them in its `(min-width: 721px) and (min-height: 600px)` pinned blocks (test_cockpit_polish_fix4
  // holds the two in agreement). Home's deck is a grid, and a sticky grid item only sticks inside its own grid area (its row), so on
  // Home dashboard.css fixes both bars and the deck pads by their height. The heights are measured here: --ia-mast-h is the masthead's
  // true height while it is pinned (the places bar's `top`), --ia-bars-h is everything that is stuck to the top of the viewport, which
  // `html { scroll-padding-top }` keeps focus and jumps clear of. Where nothing pins (a phone, a short screen) the masthead scrolls
  // away; the places strip is still sticky in the flow there (everywhere but Home on a short wide screen, where the grid holds it in
  // its row), so it is what --ia-bars-h carries. `html.ia-mast-measured` is the switch: without script the bars stay in flow.
  var PIN_MIN_W = 721, PIN_MIN_H = 600;
  function trackMasthead() {
    var m = document.querySelector(".deck > .masthead");
    if (!m) return;
    var root = document.documentElement;
    var bar = document.querySelector(".deck > .ia-places");
    var mq = typeof window.matchMedia === "function" ? window.matchMedia("(min-width: " + PIN_MIN_W + "px) and (min-height: " + PIN_MIN_H + "px)") : null;
    function set() {
      var pinned = mq ? mq.matches : (window.innerWidth >= PIN_MIN_W && window.innerHeight >= PIN_MIN_H);
      var phone = window.innerWidth < PIN_MIN_W;
      // the true (fractional) height: the places bar sits at exactly the masthead's bottom, no ceil gap between them
      var mh = pinned ? m.getBoundingClientRect().height : 0;
      var barStuck = !!bar && (pinned || phone || document.body.dataset.iaPlace !== "home");
      var bh = barStuck ? bar.getBoundingClientRect().height : 0;
      root.style.setProperty("--ia-mast-h", mh + "px");
      root.style.setProperty("--ia-bars-h", (mh + bh) + "px");
    }
    remeasure = set;
    set();
    root.classList.add("ia-mast-measured");
    if (typeof ResizeObserver === "function") { var ro = new ResizeObserver(set); ro.observe(m); if (bar) ro.observe(bar); }
    window.addEventListener("resize", set);
    if (mq && mq.addEventListener) mq.addEventListener("change", set);
  }

  // The Notifications button's aria-label replaces its content for assistive tech, so the unread count in the pill (written by the
  // page) is carried into the label here.
  function syncNotifyLabel() {
    var pill = document.querySelector(".ia-act[data-phone-jump=notifications] [data-nc-unread]");
    var btn = pill && pill.closest(".ia-act");
    if (!btn) return;
    var t = (pill.textContent || "").trim();
    var n = pill.hidden || t === "0" ? "" : t;
    var label = n ? "Notifications, " + n + " unread" : "Notifications";
    if (btn.getAttribute("aria-label") !== label) btn.setAttribute("aria-label", label);
    if (btn.getAttribute("title") !== label) btn.setAttribute("title", label);
  }
  function watchNotifyLabel() {
    var pill = document.querySelector(".ia-act[data-phone-jump=notifications] [data-nc-unread]");
    if (!pill) return;
    new MutationObserver(syncNotifyLabel).observe(pill, { attributes: true, attributeFilter: ["hidden"], childList: true, characterData: true, subtree: true });
    syncNotifyLabel();
  }

  function start() {
    addHeads();
    trackMasthead();
    addGraphToggle();
    watchNotifyLabel();
    new MutationObserver(paint).observe(document.body, { attributes: true, attributeFilter: ["class", "data-mode"] });
    // a block drawn, removed or shown later (the board's panels arrive after the first paint) re-asks which heads have something under them;
    // the heads' own attribute is outside the filter, so this cannot feed itself
    var deck = document.querySelector(".deck");
    if (deck) new MutationObserver(queueHeads).observe(deck, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });
    window.addEventListener("resize", queueHeads);   // a block a media query hides or shows changes with the width
    paint();
    queueHeads();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
