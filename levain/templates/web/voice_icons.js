// levain voice_icons.js — the one drawing of every voice control (Play / Stop, Dictate, Mic) as an inline-SVG icon. A button is put in a
// state with LevainVoiceIcons.set(button, kind, label, title): the icon is swapped, and aria-label and title carry the state's words,
// so a screen reader and a hover still hear "Play", "Stop", "Dictate", "Stop dictating", "Mic" or "Stop mic". Nothing here sends or
// plays anything; the callers own what a tap does. Everything is drawn with DOM nodes, never as HTML.
(function (root) {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  // each shape: [tag, attributes]. Strokes use the button's text colour, so an icon follows the theme with its button.
  var SHAPES = {
    play: [["path", { d: "M8 5.5v13l10.5-6.5z", fill: "currentColor" }]],
    stop: [["rect", { x: "6.5", y: "6.5", width: "11", height: "11", rx: "2", fill: "currentColor" }]],
    microphone: [["rect", { x: "9", y: "3", width: "6", height: "11", rx: "3" }], ["path", { d: "M5.5 11a6.5 6.5 0 0 0 13 0" }], ["path", { d: "M12 17.5V21" }]],
    waveform: [["path", { d: "M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" }]]
  };
  // the icon a kind wears, the words it says by default, and what a screen reader is told when a button moves into it from another
  // state (never on the first draw). Dictate wears the microphone (the dictation convention), the streaming Mic the waveform (the
  // voice-mode convention). The label itself changes with the state, so no aria-pressed: a swapping name plus a pressed state is
  // announced twice ("Stop dictating, toggle button, on").
  var KINDS = {
    play: { shape: "play", label: "Play", said: "Stopped" },
    stop: { shape: "stop", label: "Stop", said: "Playing" },
    dictate: { shape: "microphone", label: "Dictate", said: "Stopped" },
    "dictate-stop": { shape: "stop", label: "Stop dictating", said: "Dictating" },
    mic: { shape: "waveform", label: "Mic", said: "Stopped" },
    "mic-stop": { shape: "stop", label: "Stop mic", said: "Mic on" }
  };
  var STATUS_ID = "levain-voice-status";

  var HIDE = "position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0";
  function makeStatus(doc, id) {
    var n = doc.createElement("div");
    n.id = id; n.className = "voice-status"; n.setAttribute("role", "status"); n.setAttribute("aria-live", "polite");
    n.style.cssText = HIDE;
    return n;
  }
  // A polite status node, visually hidden, that says what a control just did. A screen reader announces a change to a live region that
  // was already in the page, so the page's node is made empty as this file loads. A control inside an open modal dialog gets a node
  // inside that dialog (a sibling outside it is not read while the dialog is open); that one is made on first use, so its text is
  // set a moment after it is added.
  function announce(doc, text, btn) {
    var dlg = btn && btn.closest ? btn.closest('[aria-modal="true"]') : null;
    var scope = dlg || doc.body;
    if (!scope) return;
    var id = dlg ? STATUS_ID + "-dialog" : STATUS_ID;
    var n = dlg ? dlg.querySelector("#" + id) : doc.getElementById(id);
    if (!n) {
      n = makeStatus(doc, id);
      scope.appendChild(n);
      // the node waits a moment before its first text; a later change in that moment replaces the waiting text, never races it
      n.dataset.pending = text;
      root.setTimeout(function () { if (n.dataset.pending !== undefined) { n.textContent = n.dataset.pending; delete n.dataset.pending; } }, 60);
      return;
    }
    if (n.dataset.pending !== undefined) { n.dataset.pending = text; return; }
    n.textContent = text;
  }
  function ready() {
    var d = root.document;
    if (!d) return;
    var add = function () { if (d.body && !d.getElementById(STATUS_ID)) d.body.appendChild(makeStatus(d, STATUS_ID)); };
    if (d.body) add(); else d.addEventListener("DOMContentLoaded", add);
  }

  function svg(doc, shape) {
    var s = doc.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "22"); s.setAttribute("height", "22");
    s.setAttribute("data-voice-shape", shape); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false"); s.setAttribute("class", "voice-icon");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    SHAPES[shape].forEach(function (p) {
      var n = doc.createElementNS(NS, p[0]);
      Object.keys(p[1]).forEach(function (k) { n.setAttribute(k, p[1][k]); });
      if (p[1].fill) n.setAttribute("stroke", "none");
      s.appendChild(n);
    });
    return s;
  }

  // Put `btn` in the state `kind`. `label` overrides the state's words (the card's "Tap to hear this card"); `title` overrides the
  // hover text (it is the label when not given). Unknown kinds are refused loudly: a typo must not draw an empty button.
  function set(btn, kind, label, title) {
    var k = KINDS[kind];
    if (!k) throw new Error("voice_icons: unknown kind " + kind);
    var words = label || k.label, before = btn.getAttribute("data-voice-icon");
    var same = before === kind && btn.getAttribute("aria-label") === words && btn.querySelector("svg");
    if (!same) btn.replaceChildren(svg(btn.ownerDocument, k.shape));      // a repaint of the same state keeps the drawn icon
    btn.setAttribute("aria-label", words);
    btn.setAttribute("title", title || words);
    btn.setAttribute("data-voice-icon", kind);
    btn.classList.remove("voice-words");
    if (before && before !== kind) announce(btn.ownerDocument, k.said, btn);
    return btn;
  }

  var api = { set: set, KINDS: Object.keys(KINDS) };
  root.LevainVoiceIcons = api;
  ready();
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
