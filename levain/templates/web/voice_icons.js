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
    mic: [["rect", { x: "9", y: "3", width: "6", height: "11", rx: "3" }], ["path", { d: "M5.5 11a6.5 6.5 0 0 0 13 0" }], ["path", { d: "M12 17.5V21" }]],
    dictate: [["path", { d: "M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" }]]
  };
  // the icon a kind wears, the words it says by default, and whether it is a toggle that is on
  var KINDS = {
    play: { shape: "play", label: "Play" },
    stop: { shape: "stop", label: "Stop" },
    dictate: { shape: "dictate", label: "Dictate", on: false },
    "dictate-stop": { shape: "stop", label: "Stop dictating", on: true },
    mic: { shape: "mic", label: "Mic", on: false },
    "mic-stop": { shape: "stop", label: "Stop mic", on: true }
  };

  function svg(doc, shape) {
    var s = doc.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "22"); s.setAttribute("height", "22");
    s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false"); s.setAttribute("class", "voice-icon");
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
    var words = label || k.label;
    btn.replaceChildren(svg(btn.ownerDocument, k.shape));
    btn.setAttribute("aria-label", words);
    btn.setAttribute("title", title || words);
    btn.setAttribute("data-voice-icon", kind);
    if (typeof k.on === "boolean") btn.setAttribute("aria-pressed", k.on ? "true" : "false");
    return btn;
  }

  var api = { set: set, KINDS: Object.keys(KINDS) };
  root.LevainVoiceIcons = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
