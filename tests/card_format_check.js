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
// the sign request a row's message comes in, as flow files it (needs_phill_sign: namespace, options, recommended)
const req = (r, extra) => Object.assign({ namespace: "flow-needs-phill", message: r.message, options: r.options, recommended: r.rec }, extra || {});
for (const r of [long, short]) {
  const card = C.parseMessage(r.message, req(r));
  check(card && card.asked === r.what && card.stopped === r.stopped && card.item === r.id && card.repo === r.repo && card.recommended === r.rec,
    r.id + ": message parses to the row's own fields");
}
check(C.parseMessage("Ship the sweep fix tonight or hold it?\nnonce: 9a1f", req(long)) === null, "a plain sign message is not a card");
check(C.parseMessage("tool: Write\nfile: /w/a.txt", req(long)) === null, "a consent effect block is not a card");

// 3b. a message the verifier's grammar does not read exactly is NOT a card (it is drawn whole and visible): L3 r1 cases,
// each first run against the old parser, which returned a card for all of them (codex HIGH, complement MED#2/LOW#4)
const lines = short.message.split("\n");          // header, item, row_version, repo, asked, stopped, recommended, nonce, filed
const withLines = (at, add) => lines.slice(0, at).concat(add, lines.slice(at)).join("\n");
const notCard = (msg, what, extra) => check(C.parseMessage(msg, req(short, Object.assign({ message: msg }, extra || {}))) === null, what);
notCard(withLines(6, ["effect: delete production data"]), "an unknown line (effect:) after stopped is not a card");
notCard(withLines(7, ["effect: delete production data"]), "an unknown line between the rec and nonce is not a card");
notCard(withLines(4, ["asked: harmless"]), "a duplicate asked: is not a card");
notCard(withLines(6, ["and also wipe the disk"]), "a multi-line stopped value is not a card");
notCard(withLines(6, [""]), "a blank line inside the fields is not a card");
notCard(short.message + "\n", "a trailing empty line is not a card");
notCard(lines.filter((l) => !l.startsWith("nonce: ")).join("\n"), "a missing nonce is not a card");
notCard(lines.filter((l) => !l.startsWith("stopped: ")).join("\n"), "a missing stopped is not a card");
notCard(withLines(7, ["option (A): delete it"]), "an option line outside the key domain is not a card");
notCard(withLines(7, ["option (a): x", "option (a): y"]), "a repeated option key is not a card");
notCard(withLines(7, ["option (z): delete it"]), "option lines that are not the request's options are not a card");
notCard(short.message.replace("recommended: (a)", "recommended: a"), "a recommended line not (<key>) <text> is not a card");
notCard(short.message.replace("recommended: (a)", "recommended: (z)"), "a recommendation that is no option is not a card");
notCard(short.message.replace("keep flow's byline", "drop flow's byline"), "a recommendation whose text is not its option's is not a card");
notCard(short.message, "the request's recommended disagreeing with the signed one is not a card", { recommended: "b" });
notCard(lines.filter((l) => !l.startsWith("recommended: ")).join("\n"), "a request rec with no signed rec is not a card");
notCard(short.message, "another namespace is not a card", { namespace: "levain-sign" });
check(C.parseMessage(short.message) === null, "no request: not a card");
// the signed rec stands when the request carries none (an older request); a CRLF message reads as its LF twin
const noJsonRec = C.parseMessage(short.message, req(short, { recommended: undefined }));
check(noJsonRec && noJsonRec.recommended === "a", "the signed recommendation is the card's when the request names none");
const crlf = C.parseMessage(short.message.replace(/\n/g, "\r\n"), req(short));
check(crlf && crlf.asked === short.what && crlf.stopped === short.stopped, "a CRLF message reads as its LF twin");
// signed option lines: read only when they are the request's keys in order AND its exact option texts (flow's verifier
// compares the signed option text to the row's own), and then they are what the OPTIONS prose is compared with, so the
// full text is shown once (deepseek MED#2, L3 r2 deepseek MED)
const full = withLines(7, short.options.map((o) => "option (" + o.key + "): " + o.text));
notCard(withLines(7, short.options.map((o, i) => "option (" + o.key + "): " + (i === 0 ? "harmless wording" : o.text))),
  "a signed option line whose text is not the request's option text is not a card (the panel would show other words than the button signs)");
const fc = C.parseMessage(full, req(short, { message: full }));
check(fc && fc.optionLines.map((o) => o.key).join() === "a,b,c", "signed option lines in the request's order are read");
check(C.fullOptions(fc, short.options) === fc.optionLines && C.fullOptions(C.parseMessage(short.message, req(short)), short.options) === short.options,
  "fullOptions: the signed lines when present, else the buttons");
const proseFull = { label: "OPTIONS", blocks: [{ kind: "paragraph", text: fc.optionLines.map((o) => "(" + o.key + ") " + o.text).join(" · ") }] };
check(C.repeatsRows(proseFull, C.fullOptions(fc, short.options), fc.recommended),
  "OPTIONS prose equal to the signed option lines is left out of Stopped (drawn once)");
check(C.optionRows("(a) x·(b) y").length === 2, "options split on a · with no spaces around it");

// 4. invariant over the whole message: every word of asked + stopped is shown by the headline, the headline's rest, or a section
for (const r of [long, short]) {
  const card = C.parseMessage(r.message, req(r));
  const hd = C.headline(card.asked);
  const shown = sourceWords(hd.head)
    .concat(cardWords(hd.rest ? C.sections(hd.rest) : []))
    .concat(cardWords(C.sections(card.stopped, card.asked)));
  const src = sourceWords(card.asked).concat(sourceWords(card.stopped).slice(card.stopped.startsWith(card.asked) ? sourceWords(card.asked).length : 0));
  check(eq(shown, src), r.id + ": headline + rest + sections words == asked + stopped words (" + shown.length + ")");
}

console.log(failures === 0 ? "ALL PASS" : failures + " FAILED");
process.exit(failures === 0 ? 0 : 1);
