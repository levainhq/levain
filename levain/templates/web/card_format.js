// The CARD FORMAT for a needs-phill item's long text (Phill 10-30, "8a"): pure layout parsing, no DOM. It only SPLITS the
// text the desk wrote and never changes a word. The only things it takes off are the structural tokens that become layout:
// a label's colon, a " - " list marker, and a " · " separator under OPTIONS. Ported from flowConnect's Shared/NPCard.swift
// (the reference); tests/card_format_check.js pins the invariant against real rows.
//
// The shape the desk writes: a headline sentence, then `WHAT: ... OPTIONS: (a) x · (b) y REC: a BLOCKS: ...`, with a
// "lead: - item - item" list inside a section.
(function (root) {
  "use strict";

  const collapse = (s) => String(s).split(/\s+/).filter(Boolean).join(" ");
  const tokens = (s) => String(s).split(/\s+/).filter(Boolean);

  // An all-caps run of 1-3 words (3+ letters in all) ending in a colon, followed by a space or the end.
  const LABEL = /(?<![A-Za-z0-9])([A-Z][A-Z0-9'+/&-]+(?: [A-Z0-9][A-Z0-9'+/&-]+){0,2}):(?=\s|$)/g;
  // A paragraph break where a sentence ends and the next opens with a short "Label words: " lead.
  const BREAK = /(?<=[.!?])\s+(?=[A-Z][^.:]{0,30}: )/g;

  function labelMatches(s) {
    const out = [];
    for (const m of s.matchAll(LABEL)) {
      if ((m[1].match(/[A-Za-z]/g) || []).length < 3) continue;
      out.push({ label: m[1], start: m.index, end: m.index + m[0].length });
    }
    return out;
  }

  function paragraphs(text) {
    const parts = [];
    let last = 0;
    for (const m of text.matchAll(BREAK)) {
      parts.push(text.slice(last, m.index));
      last = m.index + m[0].length;
    }
    parts.push(text.slice(last));
    return parts.map((p) => ({ kind: "paragraph", text: p.trim() }));
  }

  // A list when the lead ends in ":" and two or more items follow; the paragraphs after the last item's sentence (a new
  // "Word(s): ..." sentence) become paragraphs of their own.
  function blocks(text) {
    if (!text) return [];
    const parts = text.split(" - ");
    if (parts.length < 3 || !parts[0].endsWith(":")) return paragraphs(text);
    const items = parts.slice(1).map((p) => p.trim());
    const tail = paragraphs(items.pop());
    let out = [];
    let lead = parts[0];
    const r = parts[0].lastIndexOf(". ");
    if (r >= 0) {
      out = out.concat(paragraphs(parts[0].slice(0, r) + "."));
      lead = parts[0].slice(r + 2);
    }
    const first = tail.length && tail[0].kind === "paragraph" ? tail[0].text : "";
    out.push({ kind: "list", lead: lead, items: items.concat([first]).filter((x) => x) });
    return out.concat(tail.slice(1));
  }

  function append(label, chunk, out) {
    const text = chunk.trim();
    if (!text && label === null) return;
    out.push({ label: label, blocks: blocks(text) });
  }

  // The text that comes before the first label (with `headline` taken off its front when the text repeats it exactly, at a
  // word boundary), then every labelled section in order.
  function sections(text, dropping) {
    let body = collapse(text);
    if (dropping != null) {
      const h = collapse(dropping);
      if (h && body.startsWith(h) && (body.length === h.length || /\s/.test(body[h.length]))) body = body.slice(h.length).trim();
    }
    const out = [];
    let cursor = 0;
    let label = null;
    for (const m of labelMatches(body)) {
      append(label, body.slice(cursor, m.start), out);
      label = m.label;
      cursor = m.end;
    }
    append(label, body.slice(cursor), out);
    return out;
  }

  // The headline of a text: its first sentence, and the rest. No sentence end inside the first 200 characters: all headline,
  // never cut (a text over 200 characters is cut at a space near 160).
  function headline(text) {
    const t = collapse(text);
    for (const m of t.matchAll(/[.!?](?=\s)/g)) {
      const cut = m.index + 1;
      if (cut > 200) break;
      if (cut >= 20) return { head: t.slice(0, cut), rest: t.slice(cut).trim() };
    }
    if (t.length > 200) {
      const r = t.slice(0, 160).lastIndexOf(" ");
      if (r >= 0) return { head: t.slice(0, r), rest: t.slice(r + 1) };
    }
    return { head: t, rest: "" };
  }

  // `(a) first · (b) second` as keyed rows, for an item that carries its options only in prose. Empty when the text is not
  // in that shape, so a caller shows it as written.
  function optionRows(text) {
    const rows = [];
    for (const part of collapse(text).split(" · ")) {
      const m = /^\(([A-Za-z])\)\s*(.+)$/.exec(part);
      if (!m) return [];
      rows.push({ key: m[1], text: m[2].trim() });
    }
    return rows;
  }

  // Every word the card shows, in order, for the "never changes the words" check.
  function words(secs) {
    let all = [];
    for (const s of secs) {
      if (s.label) all = all.concat(tokens(s.label));
      for (const b of s.blocks) {
        if (b.kind === "paragraph") all = all.concat(tokens(b.text));
        else { all = all.concat(tokens(b.lead)); for (const i of b.items) all = all.concat(tokens(i)); }
      }
    }
    return all;
  }

  // An OPTIONS or REC section that only repeats the structured rows (keys AND text; the rec key) is not drawn again.
  function repeatsRows(section, options, rec) {
    if (!options || !options.length || section.blocks.length !== 1 || section.blocks[0].kind !== "paragraph") return false;
    const text = section.blocks[0].text;
    if (section.label === "OPTIONS") {
      const rows = optionRows(text);
      return rows.length === options.length
        && rows.every((r, i) => r.key === options[i].key && collapse(r.text) === collapse(options[i].text));
    }
    if (section.label === "REC") return text === rec;
    return false;
  }

  // The signed message of a needs-phill card is `flow needs-phill answer` then `key: value` lines (flow's
  // needs_phill_sign._message). Null when the text is not that shape, so any other message is drawn as written.
  const HEADER = "flow needs-phill answer";
  function parseMessage(message) {
    if (typeof message !== "string") return null;
    const lines = message.split("\n");
    if (lines[0] !== HEADER) return null;
    const card = { repo: "", item: "", asked: null, stopped: null, recommended: "", optionLines: [] };
    for (const line of lines.slice(1)) {
      let m;
      if ((m = /^asked: (.*)$/.exec(line))) card.asked = m[1];
      else if ((m = /^stopped: (.*)$/.exec(line))) card.stopped = m[1];
      else if ((m = /^repo: (.*)$/.exec(line))) card.repo = m[1];
      else if ((m = /^item: (.*)$/.exec(line))) card.item = m[1];
      else if ((m = /^recommended: (.*)$/.exec(line))) card.recommended = m[1];
      else if ((m = /^option \(([a-z0-9]{1,8})\): (.*)$/.exec(line))) card.optionLines.push({ key: m[1], text: m[2] });
    }
    return card.asked === null ? null : card;
  }

  const api = { collapse, sections, headline, optionRows, words, repeatsRows, parseMessage };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LevainCardFormat = api;
})(typeof window !== "undefined" ? window : this);
