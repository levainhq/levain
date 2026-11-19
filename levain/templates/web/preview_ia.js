// levain preview_ia.js: the cockpit IA previews (levain#141, Phill 11-18). Two running pages beside the live one, judged by him:
//   A (preview_home.html, body.ia-a): Home + a bar of places.
//   B (preview_desk.html, body.ia-b): Desk-first; Home is the desk conversation with the band pinned over it.
// It draws nothing of its own data and changes no renderer: every panel is the live page's, from the same code. What it does:
//   - names the place on the body (data-ia-place) from the page's own state (dv-on, rv-on, data-mode), which the CSS reads;
//   - maps the places bar and the 1-4 keys onto the page's existing wiring (data-mode-tab, data-phone-jump);
//   - keeps the page off the retired modes (Morning, Build): their content is placed on Home or in Memory;
//   - folds the agents graph to a one-line strip that opens on a click;
//   - B only: enters the Desk as Home, and opens Work or Machine as a drill-down with a way back;
//   - writes the preview ribbon (which page, that nothing is sent) and the tagline candidates he picks between.
(function () {
  "use strict";

  var B = document.body.classList.contains("ia-b");
  // The tagline candidates (the line under the wordmark). Index 0 is the recommendation; the ribbon switches between them.
  var TAGLINES = [
    "what needs you, what is moving, what it remembers",
    "your partner, kept alive between sessions",
    "where you and your partner work",
  ];
  var TAG_KEY = "levain_ia_tagline";

  function clickHidden(sel) {
    var n = document.querySelector(sel);
    if (n) n.click();
  }
  function mode() { return document.body.dataset.mode || "board"; }
  function deskOn() { return document.body.classList.contains("dv-on"); }
  function roomOn() { return document.body.classList.contains("rv-on"); }

  // ---- the place, derived from the page's own state ----
  function place() {
    if (roomOn()) return "room";
    if (deskOn()) return B ? "home" : "desk";
    if (mode() === "memory") return "memory";
    return B ? "drill" : "home";
  }
  function paint() {
    var p = place();
    if (document.body.dataset.iaPlace !== p) document.body.dataset.iaPlace = p;
    var bar = document.querySelectorAll(".ia-places [data-ia-place-btn]");
    for (var i = 0; i < bar.length; i++) {
      var on = bar[i].dataset.iaPlaceBtn === p || (B && p === "drill" && bar[i].dataset.iaPlaceBtn === "home");
      if (on) bar[i].setAttribute("aria-current", "page"); else bar[i].removeAttribute("aria-current");
    }
    if (p !== "drill") delete document.body.dataset.iaDrill;
    paintBack();
  }

  // ---- going to a place ----
  function goHome() {
    if (!B) { clickHidden("[data-ia-mode=board]"); return; }
    if (mode() !== "board") clickHidden("[data-ia-mode=board]");
    clickHidden("[data-ia-jump=desk]");
  }
  function go(where) {
    if (where === "home") return goHome();
    if (where === "desk") return clickHidden("[data-ia-jump=desk]");
    if (where === "room") return clickHidden("[data-ia-jump=room]");
    if (where === "memory") return clickHidden("[data-ia-mode=memory]");
  }
  function drill(which) {
    document.body.dataset.iaDrill = which;
    clickHidden("[data-ia-mode=board]");   // a mode tab leaves the Desk
    paint();
    window.scrollTo(0, 0);
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    if (!t) return;
    var pb = t.closest("[data-ia-place-btn]");
    if (pb) { ev.preventDefault(); go(pb.dataset.iaPlaceBtn); return; }
    var d = t.closest("[data-ia-drill-btn]");
    if (d) { ev.preventDefault(); drill(d.dataset.iaDrillBtn); return; }
    var g = t.closest("[data-ia-graph-toggle]");
    if (g) {
      var open = !document.body.classList.contains("ia-graph-open");
      document.body.classList.toggle("ia-graph-open", open);
      g.setAttribute("aria-expanded", open ? "true" : "false");
      g.textContent = open ? "Hide the graph" : "Show the graph";
      window.dispatchEvent(new Event("resize"));   // the sky mounts at the size its card has; a card that just opened has one now
      return;
    }
    var tg = t.closest("[data-ia-tag]");
    if (tg) { setTag(Number(tg.dataset.iaTag)); return; }
    // B: an action button (Drop, Grants, Notifications) opens its sheet over Home; the page's own jump wiring leaves the Desk on any
    // such click, so Home is entered again under the sheet
    if (B && actionFromDesk && t.closest(".ia-actions")) { actionFromDesk = false; window.setTimeout(goHome, 0); }
  });
  // whether an action button was pressed while the Desk (B's Home) was on: read before the page's own capture listener leaves it
  var actionFromDesk = false;
  window.addEventListener("click", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    var act = t && t.closest(".ia-actions");
    // B's Home already holds the line box the Drop button stands for ("A line for the desk"): Drop goes there, and the page's
    // own jump (which would leave the Desk for a card that Home never shows) does not run
    if (B && deskOn() && act && act.contains(t.closest("[data-phone-jump=drop]"))) {
      var box = document.querySelector(".dv-zone textarea");
      ev.stopImmediatePropagation();
      ev.preventDefault();
      actionFromDesk = false;
      if (box) box.focus();
      return;
    }
    actionFromDesk = !!(B && deskOn() && act);
    // choosing a place by hand ends the Desk entry's retries; pressing an action, a theme or Sync does not
    if (ev.isTrusted && t && !act && t.closest("[data-ia-place-btn],[data-ia-drill-btn],[data-phone-jump],[data-mode-tab],#jumpbtn")) userMoved = true;
  }, true);
  var userMoved = false;

  // The places keys (KEYS below). Captured before the page's own mode keys, which would
  // otherwise open the retired Morning and Build modes. Never while typing.
  var KEYS = B ? { 1: "home", 2: "room", 3: "memory" } : { 1: "home", 2: "desk", 3: "room", 4: "memory" };
  function dialogOpen() {
    if (document.querySelector(".modal-overlay")) return true;
    var ps = document.getElementById("pr-sheet");
    return !!(ps && ps.getClientRects().length);
  }
  document.addEventListener("keydown", function (ev) {
    if (ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.altKey || !(KEYS[ev.key] || (B && ev.key === "4"))) return;
    if (dialogOpen()) return;   // a dialog is open: its keys are its own
    var a = document.activeElement, tag = a && a.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (a && a.isContentEditable)) return;
    ev.stopImmediatePropagation();
    ev.preventDefault();
    if (!KEYS[ev.key]) return;   // B's key 4: no such place
    userMoved = true;
    go(KEYS[ev.key]);
  }, true);

  // ---- the tagline ----
  function setTag(i) {
    if (!(i >= 0 && i < TAGLINES.length)) i = 0;
    try { localStorage.setItem(TAG_KEY, String(i)); } catch (_) { /* a page that cannot store still switches */ }
    var t = document.getElementById("ia-tagline");
    if (t) t.textContent = TAGLINES[i];
    var bs = document.querySelectorAll("[data-ia-tag]");
    for (var k = 0; k < bs.length; k++) bs[k].setAttribute("aria-pressed", Number(bs[k].dataset.iaTag) === i ? "true" : "false");
  }

  // ---- the agents graph strip: a toggle beside the hero card, outside every node the page repaints ----
  function addGraphToggle() {
    var col = document.getElementById("ck-skycol");
    if (!col || col.querySelector("[data-ia-graph-toggle]")) return;
    var b = document.createElement("button");
    b.type = "button";
    b.className = "ia-graph-toggle";
    b.setAttribute("data-ia-graph-toggle", "");
    b.setAttribute("aria-expanded", "false");
    b.textContent = "Show the graph";
    col.insertBefore(b, col.firstChild);
  }

  // ---- the section heads: children of the deck, which the page never repaints; the CSS orders and shows them on Home only ----
  var HEADS = [["desk", "Desk", "the desk's lease and seats; the drop box is your reply"],
    ["work", "Work", "the board and the List"],
    ["machine", "Machine", "agents, fleet, calls, stream, jobs"],
    ["glance", "At a glance", "ops, backup, memory"]];
  function addHeads() {
    var deck = document.querySelector(".deck");
    if (!deck || deck.querySelector(".ia-head")) return;
    HEADS.forEach(function (h) {
      var d = document.createElement("div");
      d.className = "ia-head";
      d.dataset.iaHead = h[0];
      var t = document.createElement("h3");
      t.textContent = h[1];
      var s = document.createElement("span");
      s.textContent = h[2];
      d.appendChild(t);
      d.appendChild(s);
      deck.appendChild(d);
    });
  }
  var DRILL_WORDS = { work: "Work: the board and the List", machine: "Machine: agents, fleet, calls, stream, jobs" };
  function paintBack() {
    var t = document.getElementById("ia-back-t");
    if (t) t.textContent = DRILL_WORDS[document.body.dataset.iaDrill] || "Everything on one page";
  }

  function start() {
    addHeads();
    var i = 0;
    try { i = Number(localStorage.getItem(TAG_KEY) || 0); } catch (_) { i = 0; }
    setTag(i);
    addGraphToggle();
    function offRetired() {
      // the retired modes never show: a hash, the jump box or a stored choice that names one lands on Home's mode instead
      if (mode() === "morning" || mode() === "build") clickHidden("[data-ia-mode=board]");
    }
    new MutationObserver(function () { offRetired(); paint(); }).observe(document.body, { attributes: true, attributeFilter: ["class", "data-mode"] });
    offRetired();
    paint();
    if (B) {
      // Home is the Desk: enter it once the page has its token and has built the Desk's section (dashboard_boot.js); a page that
      // never gets there stays on the drill-down view, which shows everything, and says so in its header.
      var tries = 0;
      (function enter() {
        if (deskOn() || document.body.dataset.iaDrill || userMoved) return;   // he chose a place first: that choice stands
        if (document.querySelector(".dv-zone")) { goHome(); if (deskOn()) return; }
        if (++tries < 40) window.setTimeout(enter, 250);
      })();
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
