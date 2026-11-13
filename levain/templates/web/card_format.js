// The CARD FORMAT for a needs-phill item's long text (Phill 10-30, "8a"): layout parsing, then the card's DOM builders
// (drawHead, drawDetail), shared by every surface that draws the card; node runs only the parsing. It only SPLITS the
// text the desk wrote and never changes a word. The only things it takes off are the structural tokens that become layout:
// a label's colon, a " - " list marker, and a " · " separator under OPTIONS. It began as a port of the iOS app's card, which is gone;
// tests/card_format_check.js pins the invariant against real rows.
//
// The shape the desk writes: a headline sentence, then `WHAT: ... OPTIONS: (a) x · (b) y REC: a BLOCKS: ...`, with a
// "lead: - item - item" list inside a section. flow's `stopped_full`/`blocks_full` (a caller asks for it: sections' `lines`) is
// read line by line: a line is its own paragraph, and a fenced block, an indented block, a `STEP n` line's command and any
// line that starts with a path or a known command is drawn as one command block (11-05: commands ran into prose and a "URL:"
// in a step was read as a section label, which cut the step off). The block is a copy button only on a card whose words are
// not signed (drawBody's `copy`); the line breaks are not part of a signature, so a signed card draws plain monospace.
(function (root) {
  "use strict";

  const collapse = (s) => String(s).split(/\s+/).filter(Boolean).join(" ");
  const tokens = (s) => String(s).split(/\s+/).filter(Boolean);

  // The labels the desk writes (flow's fanin_log reads these same words, `_OPT_BLOCK`): only these start a section. An all-caps
  // word and a colon that is not one of them ("It prints a URL: open it") stays prose. A label is followed by a space or the
  // end and is not preceded by a letter or digit. No lookbehind (Safari before 16.4 cannot parse one): the preceding
  // character is group 1 and is not part of the label. Longest first, so WHAT IT BLOCKS is not read as BLOCKS.
  const KNOWN_LABELS = ["WHAT IT BLOCKS", "OPTIONS", "BLOCKS", "WHAT", "REC", "DIFF", "THEN", "WHY"];
  const LABEL = new RegExp("(^|[^A-Za-z0-9])(" + KNOWN_LABELS.join("|") + "):(?=\\s|$)", "g");
  // A paragraph break where a sentence ends and the next opens with a short "Label words: " lead. Group 1 is the
  // sentence's closing mark, which stays with its sentence.
  const BREAK = /([.!?])\s+(?=[A-Z][^.:]{0,30}: )/g;

  // The all-caps phrases whose last word is a known label ("DESK REC: a" is the desk's recommendation, not a REC section). An
  // explicit short list on purpose: any preceding all-caps word ("... wait for CI REC: b", "the VM BLOCKS: x") is a real
  // label after an acronym. A phrase is written whole, its last word being the label it ends in.
  const LABEL_PHRASES = ["DESK REC"];
  function labelMatches(s) {
    const out = [];
    for (const m of s.matchAll(LABEL)) {
      const start = m.index + m[1].length;
      const upTo = s.slice(0, start + m[2].length);
      const phrase = LABEL_PHRASES.some((p) => upTo.endsWith(p) && !/[A-Za-z0-9]/.test(upTo.charAt(upTo.length - p.length - 1)));
      if (phrase) continue;
      out.push({ label: m[2], start: start, end: m.index + m[0].length });
    }
    return out;
  }

  // `split` false: the text is one paragraph (a line the desk broke itself is not cut again at a sentence + "Lead: ").
  function paragraphs(text, split) {
    if (split === false) return [{ kind: "paragraph", text: text.trim() }];
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
  function blocks(text, split) {
    if (!text) return [];
    const parts = text.split(" - ");
    if (parts.length < 3 || !parts[0].endsWith(":")) return paragraphs(text, split);
    const items = parts.slice(1).map((p) => p.trim());
    const tail = paragraphs(items.pop(), split);
    let out = [];
    let lead = parts[0];
    const r = parts[0].lastIndexOf(". ");
    if (r >= 0) {
      out = out.concat(paragraphs(parts[0].slice(0, r) + ".", split));
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
  // word boundary), then every labelled section in order. Line mode (`lines` true, and only for a text that is flow's
  // `stopped_full`/`blocks_full`, which the caller knows from preferFull/usesFull) reads the text by lines (sectionsByLine);
  // every other text, line breaks or not, takes the flat path: collapsed, cut at a sentence + "Lead:".
  function sections(text, dropping, lines) {
    const raw = String(text).replace(/\r\n?/g, "\n");
    if (lines === true && raw.trim().indexOf("\n") >= 0) return sectionsByLine(dropLead(raw, dropping));
    let body = collapse(raw);
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

  // `raw` less its leading words when they are `dropping`'s words in order (the same word-boundary rule as the flat text's
  // headline drop); the line breaks after the dropped words stay with the rest.
  function dropLead(raw, dropping) {
    if (dropping == null) return raw;
    const want = tokens(dropping);
    if (!want.length) return raw;
    let at = 0, end = 0;
    for (const m of raw.matchAll(/\S+/g)) {
      if (at === want.length) break;
      if (m[0] !== want[at]) return raw;
      at++;
      end = m.index + m[0].length;
    }
    return at === want.length ? raw.slice(end) : raw;
  }

  // ---- the line-aware reading. A command is a line that starts with a path (`/usr/...`, `~/...`) or a known command word and
  // does not end like a sentence; a `STEP n` line is a step caption, and a command after its colon is split off as its own block.
  // A line that starts with a path is a command whatever its last character. A command-word line that ends in "." is still a
  // command when its last token is "." or a path (`git add .`, `cp -r a/ b/.`) or it carries a flag (`git commit -m x.`);
  // `claude login failed again.` is prose. A command-word line with no final mark that reads as prose is a known edge.
  const COMMAND = /^(?:\/|~\/|(?:python3|python|git|ssh|claude|levain|launchctl|curl)(?:\s|$))/;
  const PATH_START = /^(?:\/|~\/)/;
  const SENTENCE_END = /[.!?:]$/;
  const STEP = /^STEP\s+\d+\b/;
  const INDENT = /^(?: {4}|\t)/;
  function isCommand(t) {
    if (!COMMAND.test(t)) return false;
    if (PATH_START.test(t) || !SENTENCE_END.test(t)) return true;
    if (!t.endsWith(".")) return false;
    const last = t.split(/\s+/).pop();
    return last === "." || last.indexOf("/") >= 0 || /\s-{1,2}\w/.test(t);
  }
  const lead = (l) => (/^[ \t]*/.exec(l) || [""])[0].replace(/\t/g, "    ").length;

  function sectionsByLine(raw) {
    const lines = raw.split("\n");
    const out = [];
    let label = null;
    let cur = [];
    const flush = () => { if (label !== null || cur.length) out.push({ label: label, blocks: cur }); cur = []; };
    const text = (t) => { for (const b of blocks(t.trim(), false)) cur.push(b); };
    const code = (t) => { if (t) cur.push({ kind: "code", text: t }); };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const t = line.trim();
      if (!t) continue;
      if (t.startsWith("```")) {                    // a fenced block runs to its closing fence or the end of the text
        const body = [];
        for (i++; i < lines.length && !lines[i].trim().startsWith("```"); i++) body.push(lines[i]);
        code(body.join("\n").replace(/^\n+|\s+$/g, ""));
        continue;
      }
      if (INDENT.test(line)) {                      // an indented block: its consecutive indented lines, the common indent off
        const body = [line];
        while (i + 1 < lines.length && INDENT.test(lines[i + 1])) body.push(lines[++i]);
        const cut = Math.min(...body.map(lead));
        code(body.map((l) => l.replace(/\t/g, "    ").slice(cut)).join("\n").replace(/\s+$/, ""));
        continue;
      }
      if (isCommand(t)) { code(t); continue; }
      if (STEP.test(t)) {
        const m = /^(STEP\s+\d+[^:]*:)\s+(\S.*)$/.exec(t);
        const split = m && isCommand(m[2]);
        cur.push({ kind: "paragraph", text: split ? m[1] : t, step: true });
        if (split) code(m[2]);
        continue;
      }
      let cursor = 0;
      for (const m of labelMatches(t)) {
        const before = t.slice(cursor, m.start).trim();
        if (before) text(before);
        flush();
        label = m.label;
        cursor = m.end;
      }
      const rest = t.slice(cursor).trim();
      if (rest) text(rest);
    }
    flush();
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
        if (b.kind === "paragraph" || b.kind === "code") all = all.concat(tokens(b.text));
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
  // ---- links (Phill 11-01: the report and consent links on the attest card were plain text to copy). Same rules as the
  // the iOS card this began as (gone) had the same rules: http and https only, the URL starts at an "http(s)://" not inside
  // a longer word and ends at whitespace, <, > or ", trailing sentence punctuation is not part of it, a closing bracket
  // only when the URL has more closers than openers, and a URL needs a host. The runs concatenate to the input, so no word
  // changes. Drawn as DOM anchors, never HTML.
  const URL_TRAIL = ".,;:!?*`'\u2019\u201D";
  const CLOSERS = { ")": "(", "]": "[", "}": "{" };
  // scanned on the original string with a case-insensitive pattern, never on a lower-cased copy (case folding changes a
  // string's length for some letters, and the indices would drift); the character before the scheme is read as a whole code
  // point, so an astral letter counts as a letter
  function nextUrlStart(s, from) {
    const re = /https?:\/\//gi;
    re.lastIndex = from;
    for (let m = re.exec(s); m; m = re.exec(s)) {
      const before = m.index > 0 ? Array.from(s.slice(Math.max(0, m.index - 2), m.index)).pop() : "";
      if (before && /[\p{L}\p{N}]/u.test(before)) { re.lastIndex = m.index + 1; continue; }
      return { at: m.index, scheme: m[0].length };
    }
    return null;
  }
  function linkRuns(s) {
    const text = String(s);
    const out = [];
    let plain = "", from = 0;
    while (from < text.length) {
      const m = nextUrlStart(text, from);
      if (!m) { plain += text.slice(from); break; }
      plain += text.slice(from, m.at);
      let end = m.at;
      while (end < text.length && !/[\s<>"]/.test(text[end])) end++;
      const chars = Array.from(text.slice(m.at, end));
      const count = {};
      for (const c of chars) count[c] = (count[c] || 0) + 1;
      let keep = chars.length;
      while (keep > 0) {
        const last = chars[keep - 1];
        if (URL_TRAIL.indexOf(last) >= 0) { keep--; count[last]--; continue; }
        if (CLOSERS[last] && (count[last] || 0) > (count[CLOSERS[last]] || 0)) { keep--; count[last]--; continue; }
        break;
      }
      const url = chars.slice(0, keep).join("");
      let ok = false;
      // the host is the text between the scheme and the first / ? or #: an empty one ("https:///x") is words, not a link
      const authority = url.slice(m.scheme).split(/[/?#]/)[0];
      try { const u = new URL(url); ok = authority.length > 0 && (u.protocol === "http:" || u.protocol === "https:") && !!u.hostname; } catch (_) { ok = false; }
      if (ok) {
        if (plain) { out.push({ text: plain }); plain = ""; }
        out.push({ link: url });
        plain += chars.slice(keep).join("");
      } else {
        plain += text.slice(m.at, end);
      }
      from = end;
    }
    if (plain) out.push({ text: plain });
    return out;
  }
  // an element holding `text` with its URLs as anchors: rel="noopener", the cockpit's own origin in the same tab, any other
  // address in a new one
  function rich(tag, cls, text) {
    const n = node(tag, cls);
    appendLinked(n, text);
    return n;
  }
  function appendLinked(into, text) {
    const here = root.location && root.location.origin;
    for (const run of linkRuns(text)) {
      if (run.link === undefined) { into.appendChild(root.document.createTextNode(run.text)); continue; }
      const a = node("a", "card-link", run.link);
      a.setAttribute("href", run.link);
      let own = false;
      try { own = !!here && new URL(run.link).origin === here; } catch (_) { own = false; }
      a.setAttribute("rel", own ? "noopener" : "noopener noreferrer");
      // a framed page (the cockpit shows the sign sheet in an iframe) must not navigate away from the sheet: new tab too
      let framed = false;
      try { framed = !!root.top && root.top !== root.self; } catch (_) { framed = true; }
      if (!own || framed) a.setAttribute("target", "_blank");
      into.appendChild(a);
    }
  }
  // meta (repo · item), the headline, and `msg` (the caller's node holding the message, exactly as signed) collapsed under
  // "Signed text, exactly as signed"
  function drawHead(card, msg) {
    const box = node("div", "card-head");
    const meta = [card.repo, card.item].filter((x) => x).join(" · ");
    if (meta) box.appendChild(node("p", "card-meta", meta));
    box.appendChild(rich("h2", "card-headline", headline(card.asked).head));
    const raw = node("details", "card-signed");
    if (card.loose) raw.open = true;     // a message the strict grammar refused: the exact signed text stays in view
    raw.appendChild(node("summary", "", "Signed text, exactly as signed"));
    raw.appendChild(msg);
    box.appendChild(raw);
    return box;
  }
  // A long input (row 1704: a consent request's whole input, up to the queue's display bound) drawn as its head and its tail with
  // the WHOLE text one tap away in a block that scrolls on its own, never a second cut, and the answer buttons below stay near
  // without scrolling the whole text. Text up to LONG_AT code points is one block of class `cls`, as before: LONG_AT is the old
  // display cut, so an input that used to show whole still does and needs no gate. Counted in code points, so no cut splits a
  // surrogate pair.
  // A long box keeps whether its whole text has been SEEN: opened, and every part of its block exposed in the block's view.
  // Pages gate an Allow on it (seenGate): the head and the tail are a summary, and a payload in the middle of a long command
  // must not be one tap from being allowed unopened, nor one jump to the end away (row 1704 fix 2: fix 1 latched on the
  // scroll END, so dragging the scrollbar to the bottom, or the End key, enabled Allow over a middle never on screen).
  // COVERAGE: each view [scrollTop, scrollTop + clientHeight] the block shows is sampled (on open, on every scroll event and
  // on each animation frame while a scroll is moving) and joined into a union of exposed intervals; seen is the union
  // covering the whole [0, scrollHeight] with no gap (gaps under COVER_SLACK px, scroll rounding, are joined). A jump leaves
  // a gap, and Allow stays shut until the gap is scrolled back over. The count of what is not yet shown is the characters
  // whose painted boxes reach into a gap (a line half in view counts as not shown), read from the layout, not estimated
  // from the scroll fraction. A reflow (a new scrollHeight or width: a resize, a font arriving) moves the text under the
  // pixels, so coverage starts again for the new layout until seen; once seen, a box stays seen. A block not laid out
  // (clientHeight 0) records nothing: fails closed.
  // THE LIMIT, said plainly: coverage proves the text was EXPOSED in the block's view, not that it was read. A view the
  // browser paints but reports in no scroll event or sampled frame is not counted, so a fast fling that skips a band in
  // one frame leaves a gap: that gap stays gated (counted as not shown), never counted as seen. The page's own script can
  // set scrollTop; that is not a threat model here.
  const LONG_AT = 2000, LONG_END = 400, COVER_SLACK = 2;
  const count = (n) => Number(n).toLocaleString("en-US");
  // the union of exposed intervals, kept sorted and merged (a gap under COVER_SLACK px is joined); returns whether it grew
  function coverAdd(spans, a, b) {
    const before = JSON.stringify(spans);
    spans.push([a, b]);
    spans.sort((x, y) => x[0] - y[0]);
    const out = [];
    for (const s of spans) {
      const last = out[out.length - 1];
      if (last && s[0] <= last[1] + COVER_SLACK) last[1] = Math.max(last[1], s[1]);
      else out.push([s[0], s[1]]);
    }
    spans.length = 0;
    for (const s of out) spans.push(s);
    return JSON.stringify(spans) !== before;
  }
  // the parts of [0, height] the union does not cover
  function coverGaps(spans, height) {
    const gaps = [];
    let at = 0;
    for (const [a, b] of spans) {
      if (a > at + COVER_SLACK) gaps.push([at, a]);
      at = Math.max(at, b);
    }
    if (at < height - COVER_SLACK) gaps.push([at, height]);
    return gaps;
  }
  // how many characters of `pre`'s text (one text node, `s`) have a painted box reaching into any of `gaps` (content px,
  // 0 = the top of the block's scrolled content), read from the layout by a binary search over the text, which a
  // wrapping pre lays out top to bottom. A character that paints no box (a newline) takes the next one's. Any doubt counts
  // characters as NOT shown: a text that cannot be read from the layout is wholly not shown.
  function uncoveredChars(pre, s, gaps) {
    const tn = pre.firstChild;
    const all = Array.from(s).length;
    if (!gaps.length) return 0;
    if (!tn || tn.nodeType !== 3 || tn.data !== s) return all;
    const doc = pre.ownerDocument, r = doc.createRange(), n = s.length;
    const base = pre.getBoundingClientRect().top + pre.clientTop - pre.scrollTop;
    const box = (k) => {
      for (let j = k; j < n && j < k + 16; j++) {
        r.setStart(tn, j);
        r.setEnd(tn, j + 1);
        const b = r.getBoundingClientRect();
        if (b.height > 0 || b.width > 0) return { top: b.top - base, bottom: b.bottom - base };
      }
      return null;
    };
    // the first index whose box passes `y` on `edge`; a character with no box is taken as passed (`low`) or not, whichever
    // widens the range counted as not shown
    const first = (y, edge, low) => {
      let lo = 0, hi = n;
      while (lo < hi) {
        const m = (lo + hi) >> 1;
        const b = box(m);
        if (b === null ? low : b[edge] > y) hi = m; else lo = m + 1;
      }
      return lo;
    };
    let total = 0;
    for (const [a, b] of gaps) {
      const i = first(a, "bottom", true), j = first(b - 0.5, "top", false);
      if (j > i) total += Array.from(s.slice(i, j)).length;
    }
    // a gap is never reported as nothing: one that falls between two lines' boxes still keeps Allow shut, so it counts one
    return Math.min(all, Math.max(1, total));
  }
  function longText(text, cls) {
    const s = String(text);
    const cps = Array.from(s);
    if (cps.length <= LONG_AT) return node("pre", cls, s);
    const with_ = (extra) => (cls ? cls + " " : "") + extra;
    const box = node("div", "long-text");
    const total = cps.length, middle = total - 2 * LONG_END;
    box.dataset.chars = String(total);
    box.appendChild(node("pre", with_("long-head"), cps.slice(0, LONG_END).join("")));
    box.appendChild(node("p", "long-gap", "… " + count(middle) + " more characters here, not yet shown: open the whole input below …"));
    box.appendChild(node("pre", with_("long-tail"), cps.slice(cps.length - LONG_END).join("")));
    const whole = node("details", "long-whole");
    whole.appendChild(node("summary", "", "Show the whole input (" + count(total) + " characters)"));
    const full = node("pre", with_("long-full"), s);
    full.tabIndex = 0;                                   // its own scroll: reachable by keyboard
    full.setAttribute("aria-label", "the whole input, " + count(total) + " characters");
    whole.appendChild(full);
    // seen: latched once the union of the views the open block has shown covers it whole (see COVERAGE above). `spans` is
    // that union for the layout `layout` names; `left` the characters not yet shown, as last read from the layout (before
    // the block is first open: the middle, the part the head and the tail leave out)
    let seen = false, spans = [], layout = "", left = middle;
    box.dataset.seen = "0";
    box.dataset.unseen = String(middle);
    box.longState = () => ({ seen: seen, unseen: seen ? 0 : left, total: total });
    const measure = () => {
      if (!whole.open || seen) return;
      const sh = full.scrollHeight, ch = full.clientHeight;
      if (!(ch > 0)) return;                            // not laid out: nothing is on screen yet
      const now = sh + "x" + full.clientWidth;
      if (now !== layout) { layout = now; spans = []; }  // a reflow: the old pixels are other text now
      const top = Math.max(0, full.scrollTop);
      if (!coverAdd(spans, top, Math.min(sh, top + ch))) return;   // a view already covered: nothing moved
      const gaps = coverGaps(spans, sh);
      if (!gaps.length) { seen = true; left = 0; }       // the whole block has been in view (a fitting block is at once)
      else left = uncoveredChars(full, s, gaps);
      box.dataset.seen = seen ? "1" : "0";
      box.dataset.unseen = String(seen ? 0 : left);
      box.dispatchEvent(new CustomEvent("longseen", { detail: box.longState() }));
    };
    // while a scroll moves, each animation frame is sampled too, until the view has stood still for three frames
    let moving = false, still = 0, lastTop = -1;
    const frame = () => {
      measure();
      if (full.scrollTop === lastTop) still += 1; else { still = 0; lastTop = full.scrollTop; }
      if (still < 3 && whole.open && !seen && typeof root.requestAnimationFrame === "function") root.requestAnimationFrame(frame);
      else moving = false;
    };
    full.addEventListener("scroll", () => {
      measure();
      if (!moving && !seen && typeof root.requestAnimationFrame === "function") {
        moving = true; still = 0; lastTop = full.scrollTop;
        root.requestAnimationFrame(frame);
      }
    }, { passive: true });
    // a resize (the window, the block's own box) is measured at once: a reflow starts the coverage again
    if (typeof root.ResizeObserver === "function") new root.ResizeObserver(() => measure()).observe(full);
    // open, the whole text stands alone (the head and the tail would repeat it); closed, they are back
    whole.addEventListener("toggle", () => { box.classList.toggle("open", whole.open); measure(); });
    box.appendChild(whole);
    return box;
  }
  // The gate a page puts on an Allow over `root` (a longText box, or an element holding some): null when it holds no long box
  // (nothing to gate); else { seen(), unseen(), total(), line(), on(fn) }, `on` calling fn whenever any box's state moves.
  function seenGate(root) {
    if (!root || typeof root.querySelectorAll !== "function") return null;
    const boxes = (root.classList && root.classList.contains("long-text") ? [root] : [])
      .concat(Array.from(root.querySelectorAll(".long-text")))
      .filter((b) => typeof b.longState === "function");
    if (!boxes.length) return null;
    const sum = (k) => boxes.reduce((a, b) => a + b.longState()[k], 0);
    const g = {
      seen: () => boxes.every((b) => b.longState().seen),
      unseen: () => sum("unseen"),
      total: () => sum("total"),
      line: () => (g.seen() ? "All " + count(g.total()) + " characters of the input shown: Allow is open."
        : count(g.unseen()) + " of " + count(g.total()) + " characters not yet shown: Allow opens once the whole input is opened and scrolled through, no part skipped. Deny is open now."),
      on: (fn) => { for (const b of boxes) b.addEventListener("longseen", fn); },
    };
    return g;
  }
  // "WHAT" -> "What": a section's label as a heading
  const titleCase = (l) => String(l).toLowerCase().replace(/(^|[ /-])([a-z])/g, (m, a, b) => a + b.toUpperCase());
  // a paragraph that is a " · " run of two or more items is a list (R2); every word stays, only the separators go
  function dotItems(text) {
    const parts = String(text).split(" · ").map((x) => x.trim());
    return parts.length >= 2 && parts.every((x) => x) ? parts : null;
  }
  // A command, one copyable block. The cockpit page registers its own copy-line builder (dashboard_core.js copyLine: the code,
  // its copy button and the clipboard fallback), the one every copyable command in the cockpit is drawn by; a page without one
  // (the sign sheet) gets the text as a selectable monospace block. Text only.
  let commandBuilder = null;
  function setCommandBlock(fn) { commandBuilder = typeof fn === "function" ? fn : null; }
  // `copy` false (a signed card: its line breaks are not signed, so no line may become a copy action) never reaches the builder.
  function commandBlock(text, copy) {
    if (copy === true && commandBuilder) {
      try {
        const n = commandBuilder(text, text.indexOf("\n") >= 0 ? "block" : "command");
        if (n) { n.classList.add("card-cmd"); return n; }
      } catch (_) { /* the plain block below */ }
    }
    return node("pre", "card-cmd card-cmd-plain", text);
  }
  // The text the card draws for a long field: the multi-line `full` when it carries the very words of the flat `flat` (only
  // the line breaks differ), else `flat`. The flat text is what a signature covers, so the lines are a display of it and never
  // a second text.
  function usesFull(flat, full) {
    const f = typeof flat === "string" ? flat : "";
    return typeof full === "string" && full !== "" && collapse(full) === collapse(f);
  }
  function preferFull(flat, full) {
    return usesFull(flat, full) ? full : (typeof flat === "string" ? flat : "");
  }
  function list(items) {
    const ul = node("ul", "card-list");
    for (const i of items) ul.appendChild(rich("li", "", i));
    return ul;
  }
  function drawSections(into, secs, copy) {
    for (const s of secs) {
      const sec = node("div", "card-sec");
      if (s.label) sec.appendChild(node("h3", "card-label", titleCase(s.label)));
      for (const b of s.blocks) {
        if (b.kind === "code") { sec.appendChild(commandBlock(b.text, copy)); continue; }
        if (b.kind === "paragraph") {
          if (!b.text) continue;
          const dots = b.step ? null : dotItems(b.text);
          sec.appendChild(dots ? list(dots) : rich("p", b.step ? "card-p card-step" : "card-p", b.text));
          continue;
        }
        if (b.lead) sec.appendChild(rich("p", "card-p", b.lead));
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
      row.appendChild(rich("span", "card-opt-text", o.text));
      if (o.key === rec) row.appendChild(node("span", "card-rec-mark", "Rec"));
      li.appendChild(row);
      if (o.key === rec && why) li.appendChild(rich("p", "card-opt-why", why));
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
  // `mode` = { lines, copy }, both default false: `lines` reads `text` by lines (it is a `stopped_full`/`blocks_full` text the
  // caller got from preferFull/usesFull), `copy` lets a command line become a copy button. A card whose words are signed
  // never passes `copy`: the line breaks are not signed, so its commands are drawn as plain selectable monospace.
  function drawBody(into, text, dropping, options, rec, mode) {
    const lines = !!(mode && mode.lines === true);
    const copy = !!(mode && mode.copy === true);
    const rows = (options || []).map((o) => ({ key: String(o.key), text: String(o.text !== undefined ? o.text : o.label) }));
    const all = sections(text || "", dropping, lines);
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
    drawSections(into, at < 0 ? secs : secs.slice(0, at), copy);
    if (rows.length) into.appendChild(drawOptionRows(rows, rec, why));
    if (at >= 0) drawSections(into, secs.slice(at), copy);
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
    // a Detail is always a signed message's card: the lines read, no copy button (copy is never passed from here)
    drawBody(inner, preferFull(card.stopped || "", card.stopped_full), card.asked, full, rec, { lines: usesFull(card.stopped, card.stopped_full) });
    det.appendChild(inner);
    return det;
  }

  // The "Open until" line of a request, human and local: `Open until 9:21 AM today`, `Open until tomorrow, 9:21 AM`,
  // `Open until Wed 4 Nov, 9:21 AM` (within the week), else `Open until 4 Nov 2027, 9:21 AM`, in the viewer's time zone and
  // the page's locale; `title` keeps the exact stamp. A value that does not parse as a date is shown as it came (never
  // "Invalid Date", never a throw). `opts` is for tests: { now: Date, locale, timeZone }.
  function openUntil(stamp, opts) {
    const raw = String(stamp === undefined || stamp === null ? "" : stamp);
    const same = { text: "Open until " + raw, title: "" };
    try {
      const o = opts || {};
      const d = new Date(raw);
      if (!raw.trim() || isNaN(d.getTime())) return same;
      const now = o.now instanceof Date ? o.now : new Date();
      const tz = o.timeZone ? { timeZone: o.timeZone } : {};
      const part = (date, f) => {
        const out = {};
        for (const p of new Intl.DateTimeFormat(o.locale, Object.assign({}, f, tz)).formatToParts(date)) out[p.type] = p.value;
        return out;
      };
      const day = (date) => { const p = part(date, { year: "numeric", month: "numeric", day: "numeric" }); return Date.UTC(+p.year, +p.month - 1, +p.day) / 86400000; };
      const time = new Intl.DateTimeFormat(o.locale, Object.assign({ hour: "numeric", minute: "2-digit" }, tz)).format(d).replace(/[\u202f\u00a0]/g, " ");
      const diff = Math.round(day(d) - day(now));
      const w = part(d, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
      let text;
      if (diff === 0) text = time + " today";
      else if (diff === 1) text = "tomorrow, " + time;
      else if (diff >= 2 && diff <= 6) text = w.weekday + " " + w.day + " " + w.month + ", " + time;
      else text = w.day + " " + w.month + " " + w.year + ", " + time;
      return { text: "Open until " + text, title: raw };
    } catch (e) {
      return same;
    }
  }

  // Free-text fields (the Drop box, the question and desk boxes, the phone's words and state line) grow with what is
  // typed. CSS `field-sizing: content` does it where the browser has it; this is the fallback that works on iOS Safari
  // today: one delegated listener, height from scrollHeight on input. Safe to call from every surface (once per page).
  const FREE_TEXT = "textarea#drop-words, textarea.drop-words, textarea.words, textarea.question-words, .stateline textarea";
  // one box's height from its content; a programmatic `.value =` fires no input event, so its caller calls this
  function grow(t) {
    if (!t || t.tagName !== "TEXTAREA" || !t.matches(FREE_TEXT)) return;
    t.style.height = "auto";
    if (t.scrollHeight) t.style.height = (t.scrollHeight + (t.offsetHeight - t.clientHeight)) + "px";      // 0 = not laid out yet: left to the browser
  }
  function installFieldGrow() {
    const doc = root.document;
    if (!doc || doc._levainFieldGrow) return;
    doc._levainFieldGrow = true;
    doc.addEventListener("input", (ev) => grow(ev.target));
  }

  // The Decisions card's two in-card tabs. Each line's tab: a question with section "desk" is "desk" (and "both" while carried);
  // every other line (the other question rows, consent, notes) is "you". `you` counts the lines still waiting on him; `desk` is
  // THE desk-owes number of the Decisions rows: every open question of section desk whatever its day, as a band lists a dated or
  // snoozed desk row under DESK OWES. A tab that draws a Desk owes count takes it from this field and counts nothing itself.
  // Tabs show only when a desk question exists. Pure: the page draws the strip and hides by these tags.
  function deskTabs(lines) {
    const out = { tags: [], you: 0, desk: 0, show: false };
    for (const ln of lines) {
      const q = ln && ln.question, c = ln && ln.consent, n = ln && ln.note;
      const desk = !!(q && q.section === "desk");
      const tag = desk ? (q.carried ? "both" : "desk") : "you";
      // an open question served with a `later` day was judged dated past today by the server: it waits on nobody today, so it is
      // not in `you`; the desk still owes it, so it is in `desk`
      const later = !!(q && typeof q.later === "string" && /^\d{4}-\d{2}-\d{2}$/.test(q.later));
      const open = q ? q.state === "open" && !later : c ? c.state === "pending" : n ? n.state === "open" : false;
      out.tags.push(tag);
      if (desk) out.show = true;
      if (open && tag !== "desk") out.you++;
      if (desk && q.state === "open") out.desk++;
    }
    return out;
  }

  // The tab choice, one cell: only the Decisions lines carry a desk section, so the card and its focus view share it.
  let deskAt = "you";
  const getDeskTab = () => deskAt;
  const setDeskTab = (k) => { deskAt = k === "desk" ? "desk" : "you"; };
  // One screenful of the SELECTED tab, from the FULL row set: the tabs, their counts and the Decisions label come from every
  // filtered row, then the chosen tab's rows are cut to `cap`. {dt, shown, total, tags}: dt is deskTabs(all); shown is the page of
  // rows; tags are those rows' tab tags (for the drawer); total is the chosen tab's row count. dt.show false: no tabs, the plain cut.
  function deskTabPage(all, at, cap) {
    const dt = deskTabs(all);
    if (!dt.show) return { dt, shown: all.slice(0, cap), total: all.length, tags: null };
    const pick = [];
    all.forEach((ln, i) => { if (dt.tags[i] === "both" || dt.tags[i] === at) pick.push(i); });
    const cut = pick.slice(0, cap);
    return { dt, shown: cut.map((i) => all[i]), total: pick.length, tags: cut.map((i) => dt.tags[i]) };
  }

  const api = { deskTabPage, getDeskTab, setDeskTab, deskTabs, preferFull, usesFull, setCommandBlock, linkRuns, installFieldGrow, grow, FREE_TEXT, collapse, sections, headline, optionRows, words, repeatsRows, fullOptions, parseMessage, looseCard, openUntil, drawHead, drawDetail, drawBody, longText, LONG_AT, seenGate };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LevainCardFormat = api;
})(typeof window !== "undefined" ? window : this);
