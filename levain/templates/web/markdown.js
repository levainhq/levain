// levain markdown.js — the SHARED safe markdown -> DOM renderer.
//
// The SINGLE reviewed home of Levain's markdown->DOM renderer for browser surfaces
// that render store/manual prose. `levain docs` includes it directly; the
// `levain serve` dashboard currently embeds a byte-identical copy of the renderer
// core between the [MD-EXTRACT] markers below. A parity test
// (tests/test_markdown_parity.py) slices BOTH files between the markers and fails
// CI on any drift, so the two surfaces can never diverge on this security-sensitive
// code (structural_invariants_beat_discipline). Edit the renderer in BOTH files
// (or repoint dashboard_core.js to load this one); the test enforces it.
//
// Safe by construction: every text value reaches the DOM via el()/textContent/
// createTextNode -- content can NEVER become markup. The only data-derived
// attribute is a link href behind a scheme allowlist (mdSafeHref). Raw HTML is NOT
// interpreted; it paints as literal text. No dependencies, no CDN, no framework.

(function () {
  "use strict";

  // ---- tiny DOM helper (textContent only -- never innerHTML on store data) ----
  // Byte-identical to dashboard_core.js's el(); the renderer core's one dependency.
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  };

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
  // literal — flow's content is dense with them, so a naive `_` italic shreds it.
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
      const fence = /^([ \t]*)(```+|~~~+)[ \t]*([^`~]*)$/.exec(line);
      if (fence) {
        const marker = fence[2][0], minLen = fence[2].length;
        i++;
        const buf = [];
        while (i < N) {
          const cl = /^[ \t]*(`{3,}|~{3,})[ \t]*$/.exec(lines[i]);
          if (cl && cl[1][0] === marker && cl[1].length >= minLen) { i++; break; }
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

  // Exposed for the docs surface + tests. renderMarkdown(text) -> DocumentFragment;
  // mdSafeHref(raw) -> safe href | null (the scheme allowlist).
  window.LevainMD = { renderMarkdown: renderMarkdown, mdSafeHref: mdSafeHref };
})();
