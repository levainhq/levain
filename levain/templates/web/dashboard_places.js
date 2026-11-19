// levain dashboard_places.js: the places bar of the cockpit page (levain#141: Home plus a bar of places).
// It draws nothing of its own data and changes no renderer: every panel is the page's own. What it does:
//   - names the place on the body (data-ia-place) from the page's own state (dv-on, rv-on, data-mode), which dashboard.css reads;
//   - maps the places bar and the 1-4 keys onto the page's existing wiring (data-mode-tab, data-phone-jump) through the hidden drivers;
//   - keeps the page off the retired modes (Morning, Build): their content lives on Home or in Memory, so a stored mode, a hash or a
//     jump that names one lands on Home instead;
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

  // ---- the place, derived from the page's own state ----
  function place() {
    if (roomOn()) return "room";
    if (deskOn()) return "desk";
    if (mode() === "memory") return "memory";
    return "home";
  }
  function paint() {
    var p = place();
    if (document.body.dataset.iaPlace !== p) document.body.dataset.iaPlace = p;
    // scoped to the places bar: other controls carry aria-current of their own
    var bar = document.querySelectorAll(".ia-places [data-ia-place-btn]");
    for (var i = 0; i < bar.length; i++) {
      if (bar[i].dataset.iaPlaceBtn === p) bar[i].setAttribute("aria-current", "page"); else bar[i].removeAttribute("aria-current");
    }
  }

  // ---- going to a place ----
  function go(where) {
    if (where === "home") return clickHidden("[data-ia-mode=board]");
    if (where === "desk") return clickHidden("[data-ia-jump=desk]");
    if (where === "room") return clickHidden("[data-ia-jump=room]");
    if (where === "memory") return clickHidden("[data-ia-mode=memory]");
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target : null;
    if (!t) return;
    var pb = t.closest("[data-ia-place-btn]");
    if (pb) { ev.preventDefault(); go(pb.dataset.iaPlaceBtn); return; }
    var g = t.closest("[data-ia-graph-toggle]");
    if (g) {
      var open = !document.body.classList.contains("ia-graph-open");
      document.body.classList.toggle("ia-graph-open", open);
      g.setAttribute("aria-expanded", open ? "true" : "false");
      g.textContent = open ? "Hide the graph" : "Show the graph";
      window.dispatchEvent(new Event("resize"));   // the sky mounts at the size its card has; a card that just opened has one now
    }
  });

  // The places keys. Captured before the page's own mode keys, which would otherwise open the retired Morning and Build modes.
  // Never while typing, and never under a dialog: a dialog's keys are its own.
  var KEYS = { 1: "home", 2: "desk", 3: "room", 4: "memory" };
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
    b.setAttribute("aria-expanded", "false");
    b.textContent = "Show the graph";
    col.insertBefore(b, col.firstChild);
  }

  // ---- the section heads: children of the deck, which the page never repaints; dashboard.css orders and shows them on Home only ----
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

  function start() {
    addHeads();
    addGraphToggle();
    function offRetired() {
      if (mode() === "morning" || mode() === "build") clickHidden("[data-ia-mode=board]");
    }
    new MutationObserver(function () { offRetired(); paint(); }).observe(document.body, { attributes: true, attributeFilter: ["class", "data-mode"] });
    offRetired();
    paint();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
