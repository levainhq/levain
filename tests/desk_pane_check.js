// Pins the pure functions of the Desk view (levain/templates/web/desk_tab_view.js): the model of the server's read, the lease line,
// the event words, the merge of event batches, the send outcomes, the poll delay and the display marks. Run: node tests/desk_pane_check.js
"use strict";
const path = require("path");
const D = require(path.join(__dirname, "..", "levain", "templates", "web", "desk_tab_view.js"));
let failures = 0;
const check = (ok, what) => { if (ok) console.log("PASS " + what); else { failures++; console.log("FAIL " + what); } };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// the read: every state comes from the server's words alone
check(D.deskModel(200, { wired: false }).kind === "unwired" && D.deskModel(200, { wired: false }).words === "This cockpit has no desk wired.", "wired:false is the unwired box");
check(D.deskModel(404, null).kind === "unwired", "a 404 (no desk route) is the unwired box");
check(D.deskModel(0, null).kind === "error" && D.deskModel(500, null).kind === "error", "no answer and a 5xx are an error, not a state of the desk");
check(D.deskModel(200, "x").kind === "error" && D.deskModel(200, null).kind === "error", "a body that is not an object is an error");
const held = D.deskModel(200, { wired: true, caps: { words_max: 2000 }, lease: { state: "held", holder: "desk 25", age_s: 190 }, thread_id: "t1", events: [], cursor: "c1", readable: true });
check(held.kind === "wired" && held.lease.state === "held" && held.lease.words === "desk 25, renewed 3 min ago" && held.lease.drop === false, "lease held: holder and age: " + held.lease.words);
check(held.wordsMax === 2000 && held.thread === "t1" && held.cursor === "c1" && held.unreadable === false, "caps, thread, cursor carried");
const none = D.deskModel(200, { wired: true, caps: {}, lease: { state: "none" }, events: [] });
check(none.lease.state === "none" && none.lease.drop === true && /held for the next desk/.test(none.lease.words) && /never starts a desk/.test(none.lease.words), "no lease: held for the next desk, Drop offered, a typed line never starts a desk");
const unk = D.deskModel(200, { wired: true, lease: { state: "unknown" }, events: [] });
check(unk.lease.state === "unknown" && /will queue/.test(unk.lease.words), "lease unknown: the line will queue");
check(D.deskModel(200, { wired: true, lease: { state: "banana" } }).lease.state === "unknown" && D.deskModel(200, { wired: true }).lease.state === "unknown", "a lease state the server did not name is unknown, never a guess");
check(D.deskModel(200, { wired: true, lease: { state: "none" }, readable: false }).unreadable === true, "readable:false is unreadable");
check(D.deskModel(200, { wired: true, lease: { state: "none" }, events: [null, 3, { by: "desk", kind: "reply", text: "hi", at: "12:00:00" }] }).events.length === 1, "an event that is not an object is dropped");

// the lease's age
check(D.ageWords(5) === "renewed under a minute ago" && D.ageWords(60) === "renewed 1 min ago" && D.ageWords(3600) === "renewed 1 h ago" && D.ageWords(172800) === "renewed 2 d ago", "age words");
check(D.ageWords(-1) === "" && D.ageWords(null) === "" && D.ageWords("x") === "" && D.ageWords(true) === "", "an age the server did not give is not drawn");
check(D.leaseModel({ state: "held", holder: "", age_s: null }).words === "A desk", "a holder with no name and no age");

// events: the words are the port's kinds
check(D.eventModel({ by: "script", kind: "took", at: "09:01:02" }).words === "Desk took it at 09:01:02 (script)", "took, by script");
check(D.eventModel({ by: "script", kind: "held", at: "" }).words === "No desk holds the lease; held", "held");
const rep = D.eventModel({ by: "desk", kind: "reply", text: "on it", at: "t" });
check(rep.who === "Desk" && rep.text === "on it" && rep.cls === "rv-room", "a reply by the desk is labelled Desk");
check(D.eventModel({ by: "script", kind: "reply", text: "x" }).who === "script", "a reply by anything else is labelled by its own name");
check(D.eventModel({ by: "a", kind: "zzz", text: "x" }).who === "a zzz", "an unknown kind is drawn with its own names");

// batches: the host returns what is AFTER the cursor
const e = (t) => ({ by: "desk", kind: "reply", text: t, at: "" });
check(same(D.mergeEvents([], null, { events: [e("a")], cursor: "c1", unreadable: false }), { events: [e("a")], cursor: "c1" }), "a first batch is the whole thread");
check(same(D.mergeEvents([e("a")], "c1", { events: [e("b")], cursor: "c2", unreadable: false }), { events: [e("a"), e("b")], cursor: "c2" }), "a later batch appends and moves the cursor");
check(same(D.mergeEvents([e("a")], "c1", { events: [e("a")], cursor: "c1", unreadable: false }), { events: [e("a")], cursor: "c1" }), "a batch under the cursor already held adds nothing (no duplicate)");
check(same(D.mergeEvents([e("a")], "c1", { events: [], cursor: "c1", unreadable: true }), { events: [e("a")], cursor: "c1" }), "an unreadable batch keeps what is held and the cursor");
check(same(D.mergeEvents([e("a")], null, { events: [e("a"), e("b")], cursor: null, unreadable: false }), { events: [e("a"), e("b")], cursor: null }), "a whole-thread batch with no cursor (none was held) replaces: it cannot be followed");
check(same(D.mergeEvents([e("a"), e("b")], "c2", { events: [e("c")], cursor: null, unreadable: false }), { events: [e("a"), e("b"), e("c")], cursor: null }), "a batch with no cursor that came AFTER a held cursor appends (never replaces what is held) and drops the cursor so the next read refetches");

// the send's answers
const acc = D.sendOutcome(200, { outcome: "accepted", ref: "inbox-9", thread_id: "t9", reason: null, replayed: false });
check(acc.status === "filed" && acc.ref === "inbox-9" && acc.thread === "t9" && acc.settle && acc.clear, "accepted is filed with the port's ref");
const amb = D.sendOutcome(200, { outcome: "ambiguous", ref: null, thread_id: null, reason: null, replayed: false });
check(amb.status === "ambiguous" && amb.words === "It may not have arrived. It is not sent again; say whether it did." && !amb.settle && amb.clear, "ambiguous: the Room's mark-flow words, the id is kept");
const nd = D.sendOutcome(200, { outcome: "not_delivered", reason: "desk closed for the night" });
check(nd.status === "refused" && nd.words === "desk closed for the night" && nd.settle && !nd.clear, "not_delivered: the host's one line, the draft stays, the id is spent");
check(D.sendOutcome(200, { outcome: "not_delivered", reason: null }).words === "Not delivered.", "not_delivered with no reason still says so");
const cap = D.sendOutcome(422, { error: "words_over_cap", words_max: 2000 });
check(cap.status === "refused" && /2000 characters/.test(cap.words) && /nothing was cut/i.test(cap.words) && !cap.clear, "over the cap: refused, never cut, the draft stays");
check(D.sendOutcome(404, null).status === "refused" && D.sendOutcome(403, { error: "x" }).status === "refused" && D.sendOutcome(400, { error: "bad outbox_id" }).words === "Not sent: bad outbox_id", "4xx are refusals with the server's line");
check(D.sendOutcome(400, { error: "bad_request", message: "There is nothing to send." }).words === "Not sent: There is nothing to send." && D.sendOutcome(409, { error: "outbox_conflict", message: "that outbox_id was already used for different words" }).words === "Not sent: that outbox_id was already used for different words", "a refusal draws the server's message, not its code");
const f5 = D.sendOutcome(502, null);
check(f5.status === "failed" && !f5.settle && !f5.clear && /will not duplicate/.test(f5.words), "a 5xx keeps the id and the draft: a resend does not duplicate");
check(D.sendOutcome(200, { outcome: "banana" }).status === "failed" && !D.sendOutcome(200, { outcome: "banana" }).settle, "an outcome the page does not know is not claimed");

// the note under a filed line: the host's ref is drawn once
check(D.filedNote("") === "Filed" && D.filedNote(null) === "Filed" && D.filedNote("inbox-9") === "Filed: inbox-9", "a short ref is prefixed with the outcome word");
check(D.filedNote("Filed for the desk. (thread th-1)") === "Filed for the desk. (thread th-1)" && D.filedNote("Filed; no desk is open, so it waits.") === "Filed; no desk is open, so it waits.", "a ref that already begins with Filed is not prefixed");
check(D.filedNote("Filedaway-9") === "Filed: Filedaway-9", "only the whole word Filed counts");

// poll: 4 s open, 12 s hidden
check(D.POLL_OPEN_MS === 4000 && D.POLL_HIDDEN_MS === 12000, "poll constants");
check(D.pollDelay("visible") === 4000 && D.pollDelay("hidden") === 12000 && D.pollDelay("hidden", 5, 7) === 7 && D.pollDelay("visible", 5, 7) === 5, "poll delay follows visibility");

// display marks: a hidden character is drawn as <U+XXXX>; text and ordinary space are kept
check(D.shown("a‮b") === "a<U+202E>b" && D.shown("x​y") === "x<U+200B>y" && D.shown("p q") === "p<U+00A0>q", "bidi, zero-width and no-break space are marked");
check(D.shown("line\none\ttab") === "line\none\ttab" && D.shown("plain words") === "plain words" && D.shown(null) === "", "newline, tab and plain words stay");
check(D.shown("e‍") === "e‍" && D.shown("⠀") === "<U+2800>", "the joiner stays and a blank braille cell is marked");

// nothing in the page's words says what the server cannot observe
const drawn = [D.deskModel(200, { wired: false }).words, D.deskModel(0, null).words, held.lease.words, none.lease.words, unk.lease.words, acc.words, amb.words, nd.words, cap.words, f5.words,
  D.sendOutcome(403, null).words, D.sendOutcome(404, null).words, D.eventModel({ by: "script", kind: "took", at: "t" }).words, D.eventModel({ by: "script", kind: "held" }).words, D.AMBIGUOUS_WORDS, D.ARRIVED_WORDS, D.NOT_ARRIVED_WORDS].join(" | ");
check(!/\b(fenced|read|acted on)\b/i.test(drawn), "no drawn word says fenced, read or acted on");

console.log(failures === 0 ? "ALL PASS" : failures + " FAILED");
process.exit(failures === 0 ? 0 : 1);
