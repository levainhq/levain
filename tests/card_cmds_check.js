// Pins the card's reading of a long text that carries commands (card_format.js sections / drawBody / drawDetail / preferFull)
// against the REAL needs-phill row np-c92ec798 (tests/card_cmds_row.json: the flat `stopped` and the multi-line `stopped_full`
// beside it). Run: node tests/card_cmds_check.js
"use strict";
const fs = require("fs");
const path = require("path");
class N {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this._t = ""; this.open = false; this.attrs = {}; }
  get classList() { const n = this; return { add(c) { n.className = (n.className + " " + c).trim(); } }; }
  setAttribute(k, v) { this.attrs[k] = v; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { this.children.push(c); return c; }
  all(tag) { return this.children.flatMap((c) => (c.tag === tag ? [c] : []).concat(c.all(tag))); }
  cls(name) { return this.children.flatMap((c) => (c.className.split(" ").includes(name) ? [c] : []).concat(c.cls(name))); }
}
global.window = { document: { createElement: (t) => new N(t), createTextNode: (t) => { const n = new N("#text"); n._t = String(t); return n; } } };
const C = require(path.join(__dirname, "..", "levain", "templates", "web", "card_format.js"));
const row = JSON.parse(fs.readFileSync(path.join(__dirname, "card_cmds_row.json"), "utf8"))["np-c92ec798"];
let failures = 0;
const check = (ok, what) => { if (ok) console.log("PASS " + what); else { failures++; console.log("FAIL " + what); } };
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const labels = (secs) => secs.map((s) => s.label);
const kinds = (secs) => secs.flatMap((s) => s.blocks.map((b) => b.kind + (b.step ? ":step" : "")));
const codes = (secs) => secs.flatMap((s) => s.blocks.filter((b) => b.kind === "code").map((b) => b.text));
const draw = (text, dropping, opts, rec) => C.drawBody(new N("div"), text, dropping, opts || [], rec || "");

// 1. the real row, with its lines: What and Blocks only, three steps, three commands, each one block
const full = C.sections(row.stopped_full, row.what);
check(eq(labels(full), [null, "WHAT", "BLOCKS"]) || eq(labels(full), ["WHAT", "BLOCKS"]), "real: sections " + JSON.stringify(labels(full)));
check(!labels(full).some((l) => /^url$/i.test(l || "")), "real: no URL section");
const cmds = codes(full);
check(cmds.length === 3, "real: three command blocks (" + cmds.length + ")");
check(cmds[0].endsWith("--vendor claude --slot 8") && cmds[1].endsWith("--vendor codex --slot 9") && cmds[2].endsWith("after-login --vm vend-real --vendor claude"),
  "real: each command is whole, one line, ends where the desk's line ends");
check(cmds.every((c) => !c.includes("\n") && c.startsWith("/usr/bin/python3 -I /Users/phillipclapham/")), "real: no command carries prose or a line break");
const stepCaps = full.flatMap((s) => s.blocks.filter((b) => b.step).map((b) => b.text));
check(eq(stepCaps, ["STEP 1 (paste, one line):", "STEP 2 (paste, one line):", "STEP 3 (paste, one line, read-only):"]), "real: three step captions " + JSON.stringify(stepCaps));
const sk = kinds(full).join(" ");
check(/paragraph:step code paragraph paragraph:step code paragraph paragraph:step code paragraph/.test(sk), "real: caption, command, its prose, three times (" + sk + ")");
const steps1 = full.find((s) => s.label === "WHAT").blocks;
const p1 = steps1[steps1.findIndex((b) => b.kind === "code") + 1];
check(p1.text.startsWith("Claude: pick the Claude-account") && p1.text.includes("It prints a URL: open it in your Mac browser") && p1.text.endsWith("The session ends by itself."),
  "real: STEP 1's prose is whole, the URL colon stays inside it");
// never changes a word: the card's words are the source's words less the headline and the label colons
const src = C.collapse(row.stopped_full).split(" ");
const headWords = C.collapse(row.what).split(" ").length;
const cardWords = C.words(full);
const rest = src.slice(headWords).map((w) => (w === "WHAT:" || w === "BLOCKS:" ? w.slice(0, -1) : w));
check(eq(cardWords, rest), "real: every word of the text is on the card, in order (" + cardWords.length + " words)");

// the drawn card (the fallback block: no builder registered)
const dom = draw(row.stopped_full, row.what);
check(dom.cls("card-cmd").length === 3 && dom.cls("card-cmd").every((n) => n.tag === "pre"), "drawn: three plain command blocks without a builder");
check(dom.all("h3").map((h) => h.textContent).join() === "What,Blocks", "drawn: headings What, Blocks");
check(dom.cls("card-step").length === 3, "drawn: three step captions");

// a page's own copy-line builder is the one used, and its node gains the card's class
let asked = [];
C.setCommandBlock((text, label) => { asked.push([text, label]); const n = new N("div"); n.className = "consent-cmd"; n.textContent = text; return n; });
const dom2 = draw(row.stopped_full, row.what);
check(asked.length === 3 && asked.every((a) => a[1] === "command"), "builder: asked once per command");
check(dom2.cls("card-cmd").length === 3 && dom2.cls("consent-cmd").length === 3, "builder: its nodes are the card's commands");
C.setCommandBlock(() => { throw new Error("boom"); });
check(draw(row.stopped_full, row.what).cls("card-cmd-plain").length === 3, "builder: one that throws leaves the plain block, never a missing command");
C.setCommandBlock(null);

// 2. a legacy row: the flat text, no line breaks, reads as it did (no command blocks, no URL section), every word kept
const flat = C.sections(row.stopped, row.what);
check(labels(flat).filter((l) => l).join() === "WHAT,BLOCKS" && !flat.some((s) => s.blocks.some((b) => b.kind === "code")), "flat: What and Blocks, no code blocks");
check(eq(C.words(flat), rest), "flat: every word kept");
check(C.sections("WHAT: a OPTIONS: (a) x · (b) y REC: a BLOCKS: z", null).map((s) => s.label).join() === "WHAT,OPTIONS,REC,BLOCKS", "flat: WHAT / OPTIONS / REC / BLOCKS still split");

// 3. known labels split, and only those
const lab = C.sections("lead sentence here.\nWHAT: the thing\nOPTIONS: (a) go · (b) wait\nREC: a\nWHAT IT BLOCKS: x\nDIFF: d\nTHEN: t\nWHY: w", null);
check(eq(labels(lab), [null, "WHAT", "OPTIONS", "REC", "WHAT IT BLOCKS", "DIFF", "THEN", "WHY"]), "lines: every known label starts a section " + JSON.stringify(labels(lab)));
for (const text of ["It prints a URL: open it in your browser. Then go.", "NOTE: this stays prose\nsecond line", "DESK REC: a stays prose\nsecond line", "ANALYSIS: x\nmore"]) {
  const s = C.sections(text, null);
  check(s.length === 1 && s[0].label === null, "unknown caps + colon stays prose: " + JSON.stringify(text.split("\n")[0]));
}
check(eq(C.sections("see URL: x\nWHAT: y", null).map((s) => s.label), [null, "WHAT"]), "a URL: line beside a real label");

// 4. commands: fenced, indented, path, known words, a step with its command inline
const mix = C.sections([
  "Run these.", "```", "git status", "git log -3", "```", "    ssh me@host", "      indented more",
  "~/bin/tool --flag", "/usr/bin/true", "python3 a.py x", "claude login", "curl -s http://x", "launchctl list", "levain doctor",
  "STEP 4: git pull --ff-only", "STEP 5 then do it.",
].join("\n"), null);
check(eq(codes(mix), ["git status\ngit log -3", "ssh me@host\n  indented more", "~/bin/tool --flag", "/usr/bin/true", "python3 a.py x", "claude login", "curl -s http://x", "launchctl list", "levain doctor", "git pull --ff-only"]),
  "commands: fenced, indented, path and known-word lines are code: " + JSON.stringify(codes(mix)));
check(mix[0].blocks.filter((b) => b.step).map((b) => b.text).join("|") === "STEP 4:|STEP 5 then do it.", "commands: a step's inline command is split off its caption");
const prose = C.sections("levain 37 filed this.\ngit is a tool.\nSee /usr/bin/true for it.\nthe path /quit ends it", null);
check(prose.length === 1 && prose[0].blocks.length === 4 && prose[0].blocks.every((b) => b.kind === "paragraph"), "commands: a sentence that starts with a command word, or names a path mid-line, is prose");
check(eq(C.sections("```\nno close\nstill code", null)[0].blocks, [{ kind: "code", text: "no close\nstill code" }]), "commands: an unclosed fence runs to the end");
const cr = C.sections("WHAT: a\r\n/usr/bin/true x\r\nthen words", null);
check(codes(cr).join() === "/usr/bin/true x", "commands: CRLF lines read the same");

// 5. the headline drop on a text with lines; a line is its own paragraph
const hd = C.sections("Headline here is long enough.\nWHAT: a\nsecond. Next: line\nthird", "Headline here is long enough.");
check(eq(labels(hd), ["WHAT"]), "lines: the headline is taken off the front " + JSON.stringify(labels(hd)));
check(hd[0].blocks.map((b) => b.text).join("|") === "a|second. Next: line|third", "lines: each line is its own paragraph, none cut at a sentence + Lead:");
check(C.sections("Headline here is long enough.\nWHAT: a", "Other words entirely").length === 2, "lines: a headline the text does not start with drops nothing");

// 6. preferFull: the lines only when they are the very words of the flat text
check(C.preferFull(row.stopped, row.stopped_full) === row.stopped_full, "preferFull: the same words -> the lines");
check(C.preferFull(row.stopped, row.stopped_full + " extra") === row.stopped, "preferFull: an extra word -> the flat text");
check(C.preferFull(row.stopped, row.stopped_full.replace("STEP 1", "STEP 9")) === row.stopped, "preferFull: a changed word -> the flat text");
check(C.preferFull("flat text", "") === "flat text" && C.preferFull("flat text", undefined) === "flat text" && C.preferFull("flat text", 5) === "flat text", "preferFull: no lines -> the flat text");
check(C.preferFull(undefined, "x") === "", "preferFull: no flat text and some lines -> nothing");

// 7. the card's Detail reads stopped_full when the card carries it, else the flat stopped
const card = { repo: "levain", item: "np-c92ec798", asked: row.what, stopped: row.stopped, recommended: "", optionLines: [] };
check(C.drawDetail(card, []).cls("card-cmd").length === 0, "detail: a card without stopped_full draws no command blocks");
check(C.drawDetail(Object.assign({}, card, { stopped_full: row.stopped_full }), []).cls("card-cmd").length === 3, "detail: with stopped_full, three command blocks");
check(C.drawDetail(Object.assign({}, card, { stopped_full: row.stopped_full + " extra" }), []).cls("card-cmd").length === 0, "detail: a stopped_full with other words is ignored");

process.exit(failures ? 1 : 0);
