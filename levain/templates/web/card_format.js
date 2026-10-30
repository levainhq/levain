// The CARD FORMAT for a needs-phill item's long text (Phill 10-30, "8a"): layout parsing, then the card's DOM builders
// (drawHead, drawDetail), shared by every surface that draws the card; node runs only the parsing. It only SPLITS the
// text the desk wrote and never changes a word. The only things it takes off are the structural tokens that become layout:
// a label's colon, a " - " list marker, and a " · " separator under OPTIONS. Ported from the iOS app's NPCard.swift
// (the reference); tests/card_format_check.js pins the invariant against real rows.
//
// The shape the desk writes: a headline sentence, then `WHAT: ... OPTIONS: (a) x · (b) y REC: a BLOCKS: ...`, with a
// "lead: - item - item" list inside a section.
(function (root) {
  "use strict";

  const collapse = (s) => String(s).split(/\s+/).filter(Boolean).join(" ");
  const tokens = (s) => String(s).split(/\s+/).filter(Boolean);

  // An all-caps run of 1-3 words (3+ letters in all) ending in a colon, followed by a space or the end, not preceded by a
  // letter or digit. No lookbehind (Safari before 16.4 cannot parse one): the preceding character is group 1 and is not
  // part of the label.
  const LABEL = /(^|[^A-Za-z0-9])([A-Z][A-Z0-9'+/&-]+(?: [A-Z0-9][A-Z0-9'+/&-]+){0,2}):(?=\s|$)/g;
  // A paragraph break where a sentence ends and the next opens with a short "Label words: " lead. Group 1 is the
  // sentence's closing mark, which stays with its sentence.
  const BREAK = /([.!?])\s+(?=[A-Z][^.:]{0,30}: )/g;

  function labelMatches(s) {
    const out = [];
    for (const m of s.matchAll(LABEL)) {
      if ((m[2].match(/[A-Za-z]/g) || []).length < 3) continue;
      out.push({ label: m[2], start: m.index + m[1].length, end: m.index + m[0].length });
    }
    return out;
  }

  function paragraphs(text) {
    const parts = [];
    let last = 0;
    for (const m of text.matchAll(BREAK)) {
      parts.push(text.slice(last, m.index + 1));
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

  // The headline of a text: up to its first sentence end that falls 20 or more characters in (a shorter opening sentence,
  // "Yes.", stays in the headline with what follows it), and the rest. No such end inside the first 200 characters: all
  // headline, never cut (a text over 200 characters is cut at a space near 160).
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
    for (const part of collapse(text).split(/\s*·\s*/)) {
      const m = /^\(([a-z0-9]{1,8})\)\s*(.+)$/.exec(part);
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

  // The options whose text the card shows in full, the one answer both the "Options, in full" panel and the OPTIONS
  // prose under Stopped are decided by: the signed option lines when the message carries them, else the buttons.
  function fullOptions(card, options) {
    return card.optionLines.length ? card.optionLines : (options || []);
  }

  // The signed message of a needs-phill card, in the exact grammar flow's verifier reads (needs_phill_sign._message and
  // parse_verified): `flow needs-phill answer`, then item, row_version, repo, asked, stopped, an optional
  // `recommended: (<key>) <text>`, any `option (<key>): <text>` lines (each key once), then nonce and filed, one line each,
  // in that order and nothing else. A trailing \r on a line is taken off. `request` is the sign request the message came
  // in: its namespace must be "flow-needs-phill" (the namespace flow files every needs-phill answer under), the signed
  // recommendation must be one of its options (key and text) and agree with its `recommended`, and signed option lines
  // must be its options' keys in order. Null for anything else, so the sheet draws the message as written, whole and
  // visible: the card never shows a reading of a message that differs from what is signed.
  const HEADER = "flow needs-phill answer";
  const NAMESPACE = "flow-needs-phill";
  const HEAD_FIELDS = ["item", "row_version", "repo", "asked", "stopped"];
  const TAIL_FIELDS = ["nonce", "filed"];
  const REC_LINE = /^recommended: \(([a-z0-9]{1,8})\) (.+)$/;      // the key domain is levain's option key, [a-z0-9]{1,8}
  const OPTION_LINE = /^option \(([a-z0-9]{1,8})\): (.+)$/;
  function parseMessage(message, request) {
    if (typeof message !== "string" || !request || typeof request !== "object" || request.namespace !== NAMESPACE) return null;
    const lines = message.split("\n").map((l) => (l.endsWith("\r") ? l.slice(0, -1) : l));
    const n = HEAD_FIELDS.length, t = TAIL_FIELDS.length;
    if (lines.length < 1 + n + t || lines[0] !== HEADER) return null;
    const got = {};
    const fields = HEAD_FIELDS.concat(TAIL_FIELDS);
    const at = lines.slice(1, 1 + n).concat(lines.slice(lines.length - t));
    for (let i = 0; i < fields.length; i++) {
      if (!at[i].startsWith(fields[i] + ": ")) return null;
      got[fields[i]] = at[i].slice(fields[i].length + 2);
    }
    let mid = lines.slice(1 + n, lines.length - t);
    let recKey = "", recText = null;
    if (mid.length && mid[0].startsWith("recommended: ")) {
      const m = REC_LINE.exec(mid[0]);
      if (!m) return null;
      recKey = m[1];
      recText = m[2];
      mid = mid.slice(1);
    }
    const optionLines = [];
    for (const line of mid) {
      const m = OPTION_LINE.exec(line);
      if (!m || optionLines.some((o) => o.key === m[1])) return null;
      optionLines.push({ key: m[1], text: m[2] });
    }
    const options = Array.isArray(request.options) ? request.options : [];
    if (recKey && !options.some((o) => o && o.key === recKey && o.text === recText)) return null;
    if (request.recommended !== undefined && request.recommended !== null && request.recommended !== recKey) return null;
    if (optionLines.length && (optionLines.length !== options.length || optionLines.some((o, i) => !options[i] || options[i].key !== o.key || options[i].text !== o.text))) return null;
    return { repo: got.repo, item: got.item, asked: got.asked, stopped: got.stopped, recommended: recKey, optionLines: optionLines };
  }

  // A needs-phill message the strict grammar refuses (an extra signed line, a recommendation its request does not carry)
  // still has a headline and a stop text to read: this reads those lines only, for DISPLAY, and marks the result `loose`.
  // It is never what is signed or what the buttons follow (the request's own recommendation is the card's), and the
  // caller draws the exact signed text open beside it. Null for anything that is not a flow needs-phill message.
  function looseCard(message, request) {
    if (typeof message !== "string" || !request || typeof request !== "object" || request.namespace !== NAMESPACE) return null;
    const lines = message.split("\n").map((l) => (l.endsWith("\r") ? l.slice(0, -1) : l));
    if (lines[0] !== HEADER) return null;
    const got = {};
    let signedRec = null;
    for (const l of lines.slice(1)) {
      const m = /^(item|repo|asked|stopped): (.*)$/.exec(l);
      if (m && got[m[1]] !== undefined) return null;     // a key twice: which one is read would be a guess
      if (m) got[m[1]] = m[2];
      const r = /^recommended: \(([a-z0-9]{1,8})\) /.exec(l);
      if (r) signedRec = r[1];
    }
    if (!got.asked) return null;
    // the request's recommendation marks the rows only where the signed message does not name another one
    let rec = typeof request.recommended === "string" ? request.recommended : "";
    if (signedRec !== null && signedRec !== rec) rec = "";
    return { repo: got.repo || "", item: got.item || "", asked: got.asked, stopped: got.stopped || "", recommended: rec, optionLines: [], loose: true };
  }

  // ---- the card's DOM, one builder for every surface that draws it (the sheet, the phone app, the cockpit's focus view).
  // Text only, never HTML. The caller places the nodes and gives them its own ids; nothing here reads the page.
  function node(tag, cls, text) {
    const n = root.document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  // meta (repo · item), the headline, and `msg` (the caller's node holding the message, exactly as signed) collapsed under
  // "Signed text, exactly as signed"
  function drawHead(card, msg) {
    const box = node("div", "card-head");
    const meta = [card.repo, card.item].filter((x) => x).join(" · ");
    if (meta) box.appendChild(node("p", "card-meta", meta));
    box.appendChild(node("h2", "card-headline", headline(card.asked).head));
    const raw = node("details", "card-signed");
    if (card.loose) raw.open = true;     // a message the strict grammar refused: the exact signed text stays in view
    raw.appendChild(node("summary", "", "Signed text, exactly as signed"));
    raw.appendChild(msg);
    box.appendChild(raw);
    return box;
  }
  // "WHAT" -> "What": a section's label as a heading
  const titleCase = (l) => String(l).toLowerCase().replace(/(^|[ /-])([a-z])/g, (m, a, b) => a + b.toUpperCase());
  // a paragraph that is a " · " run of two or more items is a list (R2); every word stays, only the separators go
  function dotItems(text) {
    const parts = String(text).split(" · ").map((x) => x.trim());
    return parts.length >= 2 && parts.every((x) => x) ? parts : null;
  }
  function list(items) {
    const ul = node("ul", "card-list");
    for (const i of items) ul.appendChild(node("li", "", i));
    return ul;
  }
  function drawSections(into, secs) {
    for (const s of secs) {
      const sec = node("div", "card-sec");
      if (s.label) sec.appendChild(node("h3", "card-label", titleCase(s.label)));
      for (const b of s.blocks) {
        if (b.kind === "paragraph") {
          if (!b.text) continue;
          const dots = dotItems(b.text);
          sec.appendChild(dots ? list(dots) : node("p", "card-p", b.text));
          continue;
        }
        if (b.lead) sec.appendChild(node("p", "card-p", b.lead));
        sec.appendChild(list(b.items));
      }
      into.appendChild(sec);
    }
  }
  // the structured options as rows, "(a) text", the recommended one marked "Rec" with the REC section's reason under it
  function drawOptionRows(rows, rec, why) {
    const sec = node("div", "card-sec card-options");
    sec.appendChild(node("h3", "card-label", "Options"));
    const ul = node("ul", "card-opts");
    for (const o of rows) {
      const li = node("li", "card-opt" + (o.key === rec ? " card-opt-rec" : ""));
      const row = node("div", "card-opt-row");
      row.appendChild(node("span", "card-opt-key", "(" + o.key + ")"));
      row.appendChild(node("span", "card-opt-text", o.text));
      if (o.key === rec) row.appendChild(node("span", "card-rec-mark", "Rec"));
      li.appendChild(row);
      if (o.key === rec && why) li.appendChild(node("p", "card-opt-why", why));
      ul.appendChild(li);
    }
    sec.appendChild(ul);
    return sec;
  }
  // `REC: a (reason)` -> the reason after the key when the key is `rec`, else null
  function recReason(section, rec) {
    if (!section || section.label !== "REC" || !rec || section.blocks.some((b) => b.kind !== "paragraph")) return null;
    const m = /^\(?([a-z0-9]{1,8})\)?(?:[.:,;]|\s|$)\s*([\s\S]*)$/i.exec(section.blocks.map((b) => b.text).join(" ").trim());
    return m && m[1] === rec ? m[2].trim() : null;
  }
  // one display of a long text, for every surface (R1-R3): the lead paragraph (off when it repeats `dropping`), one headed
  // section per label, lists for " - " and " · " runs, and the options once as rows from `options` ({key, text|label}) with
  // the recommended one marked and the REC reason under it. An OPTIONS section that does not match the rows stays drawn.
  // Display only: every word of `text` is drawn but the structural tokens.
  function drawBody(into, text, dropping, options, rec) {
    const rows = (options || []).map((o) => ({ key: String(o.key), text: String(o.text !== undefined ? o.text : o.label) }));
    const all = sections(text || "", dropping);
    let why = null;
    let secs = all;
    let at = -1;     // where the rows go: where the prose OPTIONS/REC sat
    if (rows.length) {
      const rs = all.find((x) => x.label === "REC");
      why = recReason(rs, rec);
      const first = all.findIndex((x) => x.label === "OPTIONS" || x.label === "REC");
      const gone = (x) => repeatsRows(x, rows, rec) || (why !== null && x === rs);
      secs = all.filter((x) => !gone(x));
      at = first < 0 ? -1 : all.slice(0, first).filter((x) => !gone(x)).length;
    }
    drawSections(into, at < 0 ? secs : secs.slice(0, at));
    if (rows.length) into.appendChild(drawOptionRows(rows, rec, why));
    if (at >= 0) drawSections(into, secs.slice(at));
    return into;
  }
  // Detail, collapsed: the rest of the question, the stop (less an OPTIONS/REC section that only repeats the rows), and the
  // signed options in full. `rec` is the card's own, the signed message's recommendation (parseMessage refused a request
  // that disagrees with it); `options` are the request's option rows
  function drawDetail(card, options) {
    const rec = card.recommended;
    const full = fullOptions(card, options);     // one decision: what the OPTIONS prose is compared to, and the panel
    const det = node("details", "card-detail");
    det.appendChild(node("summary", "", "Detail"));
    const inner = node("div", "card-detail-body");
    const rest = headline(card.asked).rest;
    if (rest) drawSections(inner, sections(rest));
    drawBody(inner, card.stopped || "", card.asked, full, rec);
    det.appendChild(inner);
    return det;
  }

  // Free-text fields (the Drop box, the question and coordinator boxes, the phone's words and state line) grow with what is
  // typed. CSS `field-sizing: content` does it where the browser has it; this is the fallback that works on iOS Safari
  // today: one delegated listener, height from scrollHeight on input. Safe to call from every surface (once per page).
  const FREE_TEXT = "textarea#drop-words, textarea.drop-words, textarea.words, textarea.question-words, .bd-tell textarea, .stateline textarea";
  function installFieldGrow() {
    const doc = root.document;
    if (!doc || doc._levainFieldGrow) return;
    doc._levainFieldGrow = true;
    doc.addEventListener("input", (ev) => {
      const t = ev.target;
      if (!t || t.tagName !== "TEXTAREA" || !t.matches(FREE_TEXT)) return;
      t.style.height = "auto";
      t.style.height = (t.scrollHeight + (t.offsetHeight - t.clientHeight)) + "px";
    });
  }

  const api = { installFieldGrow, FREE_TEXT, collapse, sections, headline, optionRows, words, repeatsRows, fullOptions, parseMessage, looseCard, drawHead, drawDetail, drawBody };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LevainCardFormat = api;
})(typeof window !== "undefined" ? window : this);
