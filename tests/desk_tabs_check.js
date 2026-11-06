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
console.log(failures === 0 ? "ALL PASS" : failures + " FAILED");
process.exit(failures === 0 ? 0 : 1);
