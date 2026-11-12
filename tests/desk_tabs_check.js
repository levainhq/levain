// Pins deskTabs (levain/templates/web/card_format.js), the Decisions card's tab split. Run: node tests/desk_tabs_check.js
"use strict";
const path = require("path");
const C = require(path.join(__dirname, "..", "levain", "templates", "web", "card_format.js"));
let failures = 0;
const check = (ok, what) => { if (ok) console.log("PASS " + what); else { failures++; console.log("FAIL " + what); } };
const q = (section, state, carried) => ({ question: { section, state, carried: !!carried } });
const lines = [q("you", "open"), q("desk", "open"), q("desk", "open", true), q("desk", "answered"),
               { consent: { state: "pending" } }, { consent: { state: "allowed" } }, { note: { state: "open" } }, { meta: "x", text: "y" }];
const r = C.deskTabs(lines);
check(JSON.stringify(r.tags) === JSON.stringify(["you", "desk", "both", "desk", "you", "you", "you", "you"]), "tags " + r.tags);
check(r.you === 4, "needs-you counts open rows incl. carried: " + r.you);
check(r.desk === 2, "desk-owes counts open desk rows incl. carried: " + r.desk);
check(r.show === true, "strip shows when a desk row exists");
check(C.deskTabs([q("you", "open"), q("", "open")]).show === false, "no desk row: no strip");
check(C.deskTabs([{ question: { state: "open" } }]).show === false, "no section facet: no strip");
// the tabs and counts come from the FULL row set; the chosen tab's rows are what is cut to a page (codex MED 3)
const many = []; for (let i = 0; i < 10; i++) many.push(q("you", "open")); many.push(q("desk", "open"));
let pg = C.deskTabPage(many, "you", 10);
check(pg.dt.show === true && pg.dt.desk === 1 && pg.dt.you === 10, "11 rows, row 11 desk: tabs show, counts " + pg.dt.you + "/" + pg.dt.desk);
check(pg.shown.length === 10 && pg.total === 10, "you tab holds its 10 rows");
pg = C.deskTabPage(many, "desk", 10);
check(pg.shown.length === 1 && pg.tags[0] === "desk", "desk tab holds the 11th row");
const desks = []; for (let i = 0; i < 15; i++) desks.push(q("desk", "open"));
pg = C.deskTabPage(desks, "desk", 10);
check(pg.dt.desk === 15 && pg.shown.length === 10 && pg.total === 15, "15 desk rows: label counts 15, page cuts to 10");
check(C.deskTabPage([q("you", "open")], "you", 10).dt.show === false, "no desk row: plain cut, no tabs");
// an open question dated past today (its served later day) counts in no tab, as on the strip (L3 later2 r1, codex MED)
const dated = { question: { section: "you", state: "open", carried: false, later: "2099-01-01" } };
const dt2 = C.deskTabs([q("you", "open"), dated, q("desk", "open")]);
check(dt2.you === 1 && dt2.desk === 1, "a dated question is not counted: " + dt2.you + "/" + dt2.desk);
check(C.deskTabs([{ question: { section: "you", state: "open", later: "soon" } }, q("desk", "open")]).you === 1, "a malformed later day is due");
// a desk row dated or snoozed to a later day waits on nobody today (not in you) but the desk still owes it: it is THE desk-owes
// number's row, as flow's footer lists it under DESK OWES (a dated row of his own is not)
const deskLater = { question: { section: "desk", state: "open", carried: false, later: "2099-01-01" } };
const dt3 = C.deskTabs([q("you", "open"), deskLater, deskLater, q("desk", "open"), q("desk", "open", true), dated]);
check(dt3.desk === 4 && dt3.you === 2, "a dated desk row is owed by the desk today: " + dt3.you + "/" + dt3.desk);
check(JSON.stringify(dt3.tags) === JSON.stringify(["you", "desk", "desk", "desk", "both", "you"]), "a dated desk row keeps its desk tag: " + dt3.tags);
check(C.deskTabs([deskLater]).show === true && C.deskTabs([deskLater]).desk === 1, "a band of dated desk rows alone still shows the tab, counted");
check(C.deskTabs([{ question: { section: "desk", state: "answered", later: "2099-01-01" } }]).desk === 0, "an answered desk row is not owed");
console.log(failures === 0 ? "ALL PASS" : failures + " FAILED");
process.exit(failures === 0 ? 0 : 1);
