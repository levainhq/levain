// levain card_voice.js — reading a Needs-you card aloud: the card script and the speaker on the opened card (voice V1-7;
// design project_memory/1104-voice/V1_DESIGN.md section 4.3).
//
// script(what, options, rec) is the NPCardVoice port (flowConnect's Shared/NPCardVoice.swift, removed 1105-13): the headline
// verbatim (card_format.js headline, the card's first sentence), then each option as "<key>, <its full text>", the rec's line
// ending ", recommended". Those are the only words added. No options reads the headline alone; an empty `what` reads nothing.
// levain/cockpit/card_script.py builds the SAME script for the premium route; tests/test_card_voice.py pins the two against
// each other on fixed cards.
//
// The page half calls LevainVoice.play() and nothing else of the player: a card is played as the pseudo-reply
// {thread: "question:<n>", gseq: 0, card: "question:<n>", text: <script>}. The premium tier sends {card_id} (the server builds
// the words from the filed question); the on-device tier speaks `text`.
//
// Opened from a notification: dashboard_core.js calls opened(id) when the deep link /?view=needs&item=<id> opens a card. The
// speaker that card then draws (speaker()) reads it at once ONLY when the "Read a notification aloud when I open it" setting is
// on AND the page may play (a gesture has unlocked it, or the browser says it holds a live user activation). A fresh page load from
// a notification tap has neither on iOS [inferred], so the speaker then shows the large "Tap to hear this card" and plays on the
// tap. Setting off: the card shows Play. A card opened any other way shows Play and never plays by itself.
// Everything is drawn with textContent.
(function (root) {
  "use strict";
  const CARD_ID = /^question:[1-9][0-9]{0,11}$/;
  const OPENED_MS = 60000;      // how long an opened(id) waits for its card to be drawn
  const WORDS = { play: "Play", stop: "Stop", tap: "Tap to hear this card" };

  function headlineOf(what) {
    const F = root.LevainCardFormat || (typeof require === "function" ? require("./card_format.js") : null);
    return F && typeof F.headline === "function" ? F.headline(String(what || "")).head : String(what || "");
  }
  // options: [{key, text}] in the card's order
  function script(what, options, rec) {
    const lines = [];
    const head = headlineOf(what).replace(/^[ \t]+|[ \t]+$/g, "");
    if (head) lines.push(head);
    for (const o of Array.isArray(options) ? options : []) {
      if (!o) continue;
      const text = String(o.text === undefined || o.text === null ? "" : o.text).replace(/^[ \t]+|[ \t]+$/g, "");
      if (!text) continue;
      let line = o.key + ", " + text;
      if (rec && o.key === rec) line += ", recommended";
      lines.push(line);
    }
    return lines.join("\n");
  }
  // a question's body as GET /cockpit/question/<n>.json serves it: {what, options: [{key, label}], rec}
  function scriptOfBody(b) {
    if (!b || typeof b !== "object" || b.error || typeof b.what !== "string") return "";
    const opts = (Array.isArray(b.options) ? b.options : []).map((o) => ({ key: o && o.key, text: o && o.label }));
    return script(b.what, opts, typeof b.rec === "string" && b.rec ? b.rec : null);
  }

  const api = { CARD_ID, WORDS, script, scriptOfBody, headlineOf };
  if (typeof module === "object" && module.exports) { module.exports = api; return; }

  // ---- the page half ------------------------------------------------------------------------------------------------------
  let openedId = null, openedAt = 0;
  function opened(id) { if (typeof id === "string" && CARD_ID.test(id)) { openedId = id; openedAt = Date.now(); } }
  function takeOpened(id) {
    const hit = openedId === id && Date.now() - openedAt <= OPENED_MS;
    if (openedId === id) openedId = null;
    return hit;
  }
  // May this page start a reading now without a tap of its own? An unlocked player, or a live user activation.
  function mayPlay(V) {
    if (root.document && root.document.hidden) return false;
    if (V.isUnlocked()) return true;
    const ua = root.navigator && root.navigator.userActivation;
    return !!(ua && ua.isActive);
  }

  // The speaker for the card `id` ("question:<n>") whose body is `body` (the question JSON). A node, or null when there is
  // nothing to read or no player.
  function speaker(id, body) {
    const V = root.LevainVoice;
    if (!V || typeof id !== "string" || !CARD_ID.test(id)) return null;
    const text = scriptOfBody(body);
    if (!text) return null;
    const reply = { thread: id, gseq: 0, card: id, text };
    const wrap = root.document.createElement("div");
    wrap.className = "card-voice";
    const b = root.document.createElement("button");
    b.type = "button"; b.className = "voice-btn card-voice-btn";
    const note = root.document.createElement("span");
    note.className = "voice-say"; note.setAttribute("role", "status"); note.hidden = true;
    wrap.appendChild(b); wrap.appendChild(note);
    let mine = false, large = false, run = 0, attached = false, off = null;
    const paint = () => {
      if (wrap.isConnected) attached = true;
      else if (attached) { if (off) off(); off = null; return; }     // the card was closed: stop listening
      b.textContent = mine ? WORDS.stop : (large ? WORDS.tap : WORDS.play);
      b.setAttribute("aria-pressed", mine ? "true" : "false");
      wrap.classList.toggle("card-voice-large", large && !mine);
      const m = V.noteFor(reply) || "";
      note.textContent = m; note.hidden = !m;
    };
    const start = () => {
      large = false; mine = true;
      const me = ++run;
      paint();
      Promise.resolve(V.play(reply)).then(() => {}, () => {}).then(() => { if (me === run) { mine = false; paint(); } });
    };
    b.addEventListener("click", () => {
      V.unlock();
      if (mine) { run++; mine = false; V.stop(); paint(); } else start();
    });
    if (typeof V.onChange === "function") off = V.onChange(paint);
    if (takeOpened(id) && V.settings().notify === true) {
      if (mayPlay(V)) { V.unlock(); start(); } else large = true;
    }
    paint();
    return wrap;
  }

  api.opened = opened; api.speaker = speaker;
  root.LevainCardVoice = api;
})(typeof window !== "undefined" ? window : globalThis);
