// Pins levain/templates/web/card_format.js against two REAL needs-phill rows (tests/card_format_rows.json): np-ab37059b
// (long: headline, WHAT with a list, OPTIONS, REC, BLOCKS) and np-b70bec0b (short, no labels). The property that matters:
// the parser never changes a word. Run: node tests/card_format_check.js
"use strict";
const fs = require("fs");
const path = require("path");
const C = require(path.join(__dirname, "..", "levain", "templates", "web", "card_format.js"));
const rows = JSON.parse(fs.readFileSync(path.join(__dirname, "card_format_rows.json"), "utf8"));
const long = rows["np-ab37059b"], short = rows["np-b70bec0b"];
let failures = 0;
const check = (ok, what) => { if (ok) console.log("PASS " + what); else { failures++; console.log("FAIL " + what); } };
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// the source's words with only the structural tokens off: label colons, " - " markers, " · " separators
const LABELS = new Set(["WHAT", "OPTIONS", "REC", "BLOCKS"]);
const strip = (ws) => ws.filter((w) => w !== "-" && w !== "·");
const sourceWords = (s) => strip(C.collapse(s).split(" ").filter(Boolean)
  .map((w) => (w.endsWith(":") && LABELS.has(w.slice(0, -1)) ? w.slice(0, -1) : w)));
const cardWords = (secs) => strip(C.words(secs));

// 1. the long real row
const secs = C.sections(long.stopped, long.what);
check(eq(secs.map((s) => s.label), [null, "WHAT", "OPTIONS", "REC", "BLOCKS"]), "long: sections " + JSON.stringify(secs.map((s) => s.label)));
const what = secs.find((s) => s.label === "WHAT");
const list = what.blocks.find((b) => b.kind === "list");
check(list && list.items.length === 4 && list.lead.endsWith(":"), "long: WHAT carries a 4-item list");
check(what.blocks.filter((b) => b.kind === "paragraph").length >= 3, "long: WHAT breaks into separate paragraphs");
const prose = long.stopped.split("OPTIONS: ")[1].split(" REC:")[0];
check(eq(C.optionRows(prose).map((r) => r.key), ["a", "b", "c"]), "long: OPTIONS prose parses to rows a b c");
check(eq(cardWords(secs), sourceWords(long.stopped).slice(sourceWords(long.what).length)),
  "long: card words == source words minus the headline it dropped");
check(eq(sourceWords(long.what).concat(cardWords(secs)), sourceWords(long.stopped)), "long: headline + card words == source words");
// the structured options equal the OPTIONS prose and the rec, so those two sections are not drawn again
check(secs.filter((s) => C.repeatsRows(s, long.options, long.rec)).map((s) => s.label).join() === "OPTIONS,REC",
  "long: OPTIONS and REC repeat the rows and are left out");
check(!C.repeatsRows(secs.find((s) => s.label === "OPTIONS"), [{ key: "a", text: "other" }, long.options[1], long.options[2]], long.rec),
  "long: an option whose text differs keeps the section");
check(!C.repeatsRows(secs.find((s) => s.label === "REC"), long.options, "b"), "long: a different rec keeps the section");
const dropped = secs.filter((s) => !C.repeatsRows(s, long.options, long.rec));
const optWords = C.words(secs.filter((s) => C.repeatsRows(s, long.options, long.rec)));
check(optWords.length > 0 && cardWords(dropped).length + strip(optWords).length === cardWords(secs).length, "long: left-out sections are accounted for");

const h = C.headline("levain: SECURITY — confinement escape SHIPPED in RELEASED 0.9.0 (HIGH, desk-verified evidence). An offline, network-denied operator-floor bash achieves code execution.");
check(h.head.endsWith("evidence).") && h.rest.startsWith("An offline"), "headline is the first sentence");
const lh = C.headline(long.what);
check(lh.head === long.what && lh.rest === "", "a one-sentence what is all headline");

// 2. the short real row
const s2 = C.sections(short.stopped, short.what);
check(s2.length === 1 && s2[0].label === null, "short: one unlabelled section");
check(eq(cardWords(s2), sourceWords(short.stopped)), "short: words unchanged");
check(C.sections("", null).length === 0, "empty text: no sections");
check(eq(C.words(C.sections("Approved deployment is blocked", "Approve")), ["Approved", "deployment", "is", "blocked"]),
  "a headline that is only a word-prefix drops nothing");

// 3. the signed message of each real row is a card, and its asked/stopped are the row's
for (const r of [long, short]) {
  const card = C.parseMessage(r.message);
  check(card && card.asked === r.what && card.stopped === r.stopped && card.item === r.id && card.repo === r.repo, r.id + ": message parses to the row's own fields");
}
check(C.parseMessage("Ship the sweep fix tonight or hold it?\nnonce: 9a1f") === null, "a plain sign message is not a card");
check(C.parseMessage("tool: Write\nfile: /w/a.txt") === null, "a consent effect block is not a card");

// 4. invariant over the whole message: every word of asked + stopped is shown by the headline, the headline's rest, or a section
for (const r of [long, short]) {
  const card = C.parseMessage(r.message);
  const hd = C.headline(card.asked);
  const shown = sourceWords(hd.head)
    .concat(cardWords(hd.rest ? C.sections(hd.rest) : []))
    .concat(cardWords(C.sections(card.stopped, card.asked)));
  const src = sourceWords(card.asked).concat(sourceWords(card.stopped).slice(card.stopped.startsWith(card.asked) ? sourceWords(card.asked).length : 0));
  check(eq(shown, src), r.id + ": headline + rest + sections words == asked + stopped words (" + shown.length + ")");
}

console.log(failures === 0 ? "ALL PASS" : failures + " FAILED");
process.exit(failures === 0 ? 0 : 1);
