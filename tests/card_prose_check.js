// Pins the display formatter of levain/templates/web/card_format.js (drawBody, drawDetail, looseCard) against six REAL
// needs-phill rows (tests/card_prose_cases.json: each row with the message flow's build_message made for it). A minimal
// fake DOM stands in for the browser. Run: node tests/card_prose_check.js
"use strict";
const fs = require("fs");
const path = require("path");
class N {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this._t = ""; this.open = false; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(""); }
  appendChild(c) { this.children.push(c); return c; }
  all(tag) { return this.children.flatMap((c) => (c.tag === tag ? [c] : []).concat(c.all(tag))); }
  cls(name) { return this.children.flatMap((c) => (c.className.split(" ").includes(name) ? [c] : []).concat(c.cls(name))); }
}
global.window = { document: { createElement: (t) => new N(t) } };
const C = require(path.join(__dirname, "..", "levain", "templates", "web", "card_format.js"));
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, "card_prose_cases.json"), "utf8"));
let failures = 0;
const check = (ok, what) => { if (ok) console.log("PASS " + what); else { failures++; console.log("FAIL " + what); } };
const byId = (id) => cases.find((c) => c.row.id === id);
const headings = (n) => n.all("h3").map((h) => h.textContent);

// every real row parses, and its detail draws headed sections, not one block
for (const c of cases) {
  const card = C.parseMessage(c.message, c.request);
  check(!!card, c.row.id + ": strict parse reads it");
  const det = C.drawDetail(card, c.request.options);
  const hs = headings(det);
  const labelled = /\b[A-Z]{3,}:/.test(c.row.stopped);
  check(!labelled || hs.length >= 2, c.row.id + ": headings " + JSON.stringify(hs));
  check(hs.every((h) => h === h[0] + h.slice(1).toLowerCase() || /^[A-Z][a-z]+( [A-Z][a-z]+)*$/.test(h)), c.row.id + ": headings are title-cased");
}

// the real np-33e53967 text: What / Blocks as sections, the options drawn once as rows, the REC key marked
const real = byId("np-33e53967");
const card = C.parseMessage(real.message, real.request);
const det = C.drawDetail(card, real.request.options);
check(headings(det).join() === "What,Options,Blocks", "np-33e53967: sections What, Options, Blocks (got " + headings(det).join() + ")");
check(det.cls("card-opt").length === 2 && det.cls("card-opt-rec").length === 1, "np-33e53967: two option rows, one marked Rec");
check(det.cls("card-rec-mark")[0].textContent === "Rec", "np-33e53967: the Rec mark");
check(!/OPTIONS|REC:/.test(det.textContent), "np-33e53967: the prose OPTIONS and REC are not drawn again");

// the REC reason lands under the marked row (np-482b89b3: "REC: a. Test Out exists ...")
const bj = byId("np-482b89b3");
const bd = C.drawDetail(C.parseMessage(bj.message, bj.request), bj.request.options);
const rec = bd.cls("card-opt-rec")[0];
check(rec && rec.cls("card-opt-why")[0] && rec.cls("card-opt-why")[0].textContent.startsWith("Test Out exists to grade him"), "np-482b89b3: the REC reason is under the marked row");
check(!bd.textContent.includes("REC"), "np-482b89b3: no separate REC section");

// a prose OPTIONS that disagrees with the rows is still drawn
const body = new N("div");
C.drawBody(body, "Lead. WHAT: x. OPTIONS: (a) one · (b) two REC: a because", "Lead.", [{ key: "a", text: "one" }, { key: "b", text: "DIFFERENT" }], "a");
check(headings(body).includes("Options") && body.all("li").some((l) => l.textContent === "(b) two" || l.textContent === "(b) two".slice(0)), "disagreeing OPTIONS prose stays drawn as a list");
check(body.cls("card-opt").length === 2, "the structured rows are drawn too");

// a " · " run and a " - " run are lists; no word is lost
const lists = new N("div");
C.drawBody(lists, "WHAT: first · second · third BLOCKS: lead: - a - b - c", "", [], "");
check(lists.all("ul").length === 2 && lists.all("li").length === 6, "· and - runs become two lists of 3");
const words = (s) => s.split(/\s+/).filter((w) => w && w !== "·" && w !== "-" && !/^[A-Z]+:$/.test(w));
const drawn = lists.textContent;
check(["first", "second", "third", "lead:", "a", "b", "c"].every((w) => drawn.includes(w)), "every word is drawn");
void words;

// a strict-refused message still gets sections, with the exact signed text kept open and whole beside it
const ref = byId("np-482b89b3");
const lines = ref.message.split("\n");
const odd = lines.slice(0, 6).concat(["effect: delete the world"], lines.slice(6)).join("\n");
check(C.parseMessage(odd, ref.request) === null, "an extra signed line is refused by the strict parser");
const loose = C.looseCard(odd, ref.request);
check(loose && loose.loose === true && loose.asked === ref.row.what, "looseCard reads the headline");
const pre = new N("pre"); pre.textContent = odd;
const head = C.drawHead(loose, pre);
check(head.cls("card-signed")[0].open === true && head.cls("card-signed")[0].textContent.includes("effect: delete the world"), "the exact signed text is open beside the card");
check(headings(C.drawDetail(loose, ref.request.options)).includes("Blocks"), "refused message: Detail still has sections");
check(C.looseCard("plain message\nnonce: 1", ref.request) === null, "a non-needs-phill message is no loose card");
check(C.looseCard(odd, { namespace: "other" }) === null, "a wrong namespace is no loose card");

process.exit(failures ? 1 : 0);
