// Node tests of the dialog decline's decisions (tests/_dialog_decline.mjs) against stub sends on a virtual clock: no Chrome, no real time.
// Run by tests/test_dialog_decline.py; also runnable by hand: node --test tests/_dialog_decline.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { declineDialog, boundedSend, leavesPageFrozen, frozenMessage, NOT_ATTACHED } from "./_dialog_decline.mjs";

const BOUND = 5000, PAUSE = 250;
const refused = { error: { code: -32000, message: "Not attached to an active page" } };
const ok = { result: {} };

// A stub page: `replies` are what successive sends are answered with (the last repeats); the clock only moves when the decline sleeps or a send costs `sendMs`.
function rig(replies, { sendMs = 4 } = {}) {
  let T = 0; let sends = 0; const sleeps = []; const calls = [];
  const api = {
    now: () => T,
    sleep: async (ms) => { sleeps.push(ms); T += ms; },
    send: async (method, params) => { if (sends >= 1000) throw new Error("the stub was sent to 1000 times: the loop lost its bound"); calls.push([method, params]); const r = replies[Math.min(sends, replies.length - 1)]; sends++; T += sendMs; return typeof r === "function" ? r() : r; },
    boundMs: BOUND, pauseMs: PAUSE,
  };
  return { api, state: () => ({ T, sends, sleeps, calls }) };
}

test("a decline that works first time is sent once, with no pause", async () => {
  const r = rig([ok]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, true);
  assert.equal(out.sends, 1);
  assert.equal(r.state().sends, 1);
  assert.deepEqual(r.state().sleeps, []);
});

test("every send is Page.handleJavaScriptDialog with accept:false, the first and each repeat", async () => {
  const r = rig([refused, refused, ok]);
  await declineDialog(r.api);
  assert.equal(r.state().calls.length, 3);
  for (const c of r.state().calls) assert.deepEqual(c, ["Page.handleJavaScriptDialog", { accept: false }]);
});

test("a decline refused as not attached is sent again after a pause until it lands", async () => {
  const r = rig([refused, refused, refused, ok]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, true);
  assert.equal(out.sends, 4);
  assert.equal(r.state().sends, 4);
  assert.deepEqual(r.state().sleeps, [PAUSE, PAUSE, PAUSE]);
});

test("a decline that is always refused as not attached stops at the bound and says so", async () => {
  const r = rig([refused]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, false);
  assert.equal(out.why, "bound");
  assert.match(out.last, /Not attached to an active page/);
  const s = r.state();
  assert.ok(s.sends >= 2, `sent ${s.sends} times`);
  assert.ok(s.T >= BOUND && s.T <= BOUND + PAUSE + 20, `gave up at ${s.T}`);
  assert.equal(out.sends, s.sends);
});

test("a different error is the end of it: no second send", async () => {
  const r = rig([{ error: { code: -32000, message: "No dialog is showing" } }, ok]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, false);
  assert.equal(out.why, "error");
  assert.match(out.last, /No dialog is showing/);
  assert.equal(r.state().sends, 1);
  assert.deepEqual(r.state().sleeps, []);
});

test("a refusal that turns into another error stops there (the dialog was closed in the meantime)", async () => {
  const r = rig([refused, { error: { code: -32000, message: "No dialog is showing" } }, ok]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, false);
  assert.equal(out.why, "error");
  assert.equal(out.sends, 2);
  assert.equal(r.state().sends, 2);
});

test("a send that throws (the connection is gone) is not retried and is reported", async () => {
  const r = rig([() => { throw new Error("the page connection closed"); }]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, false);
  assert.equal(out.why, "error");
  assert.match(out.last, /the page connection closed/);
  assert.equal(r.state().sends, 1);
});

test("a refusal is retried on its message alone, whatever its code or none", async () => {
  const r = rig([{ error: { message: "Not attached to an active page" } }, { error: { code: -32001, message: "Not attached to an active page" } }, ok]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, true);
  assert.equal(out.sends, 3);
  assert.equal(NOT_ATTACHED.test("No dialog is showing"), false);
});

test("a refusal followed by a send that throws stops there, and says what threw", async () => {
  const r = rig([refused, () => { throw new Error("the page connection closed"); }, ok]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, false);
  assert.equal(out.why, "error");
  assert.match(out.last, /the page connection closed/);
  assert.equal(r.state().sends, 2);
});

test("a send that is never answered ends the loop as no answer, with the sends so far", async () => {
  const r = rig([{ timedOut: true }]);
  const out = await declineDialog(r.api);
  assert.equal(out.declined, false);
  assert.equal(out.why, "no answer");
  assert.equal(out.sends, 1);
  assert.equal(r.state().sends, 1);
  assert.deepEqual(r.state().sleeps, []);
});

test("an unanswered send after refusals keeps the last refusal's text", async () => {
  const r = rig([refused, refused, { timedOut: true }]);
  const out = await declineDialog(r.api);
  assert.equal(out.why, "no answer");
  assert.equal(out.sends, 3);
  assert.match(out.last, /Not attached to an active page/);
});

test("boundedSend: a send that never answers resolves timedOut when the clock runs out, and one that answers is passed through", async () => {
  const never = () => new Promise(() => {});
  let slept = null;
  const clockOut = (ms) => { slept = ms; return Promise.resolve(); };
  assert.deepEqual(await boundedSend(never, clockOut, 3000)("Page.handleJavaScriptDialog", { accept: false }), { timedOut: true });
  assert.equal(slept, 3000);
  const reply = { result: {} };
  const clockStays = () => new Promise(() => {});
  assert.equal(await boundedSend(async () => reply, clockStays, 3000)("m", {}), reply);
  await assert.rejects(boundedSend(async () => { throw new Error("closed"); }, clockStays, 3000)("m", {}), /closed/);
});

test("a decline whose send never answers, bounded, ends as no answer", async () => {
  const api = { now: () => 0, sleep: () => Promise.resolve(), boundMs: BOUND, pauseMs: PAUSE, send: boundedSend(() => new Promise(() => {}), () => Promise.resolve(), 3000) };
  const out = await declineDialog(api);
  assert.equal(out.why, "no answer");
});

test("what leaves the page frozen: the bound, no answer and any error but a closed dialog; a decline and a closed dialog do not", () => {
  assert.equal(leavesPageFrozen({ declined: true, sends: 1 }), false);
  assert.equal(leavesPageFrozen({ declined: false, why: "bound", last: "Not attached to an active page", sends: 20 }), true);
  assert.equal(leavesPageFrozen({ declined: false, why: "no answer", last: "", sends: 1 }), true);
  assert.equal(leavesPageFrozen({ declined: false, why: "error", last: "the page connection closed", sends: 1 }), true);
  assert.equal(leavesPageFrozen({ declined: false, why: "error", last: "Some other refusal", sends: 1 }), true);
  assert.equal(leavesPageFrozen({ declined: false, why: "error", last: "No dialog is showing", sends: 2 }), false);
});

test("the failure text names the dialog, why it gave up, the last refusal and the frozen page", () => {
  const m = frozenMessage({ declined: false, why: "bound", last: "Not attached to an active page", sends: 21 });
  assert.equal(m, "a JavaScript dialog could not be declined (bound after 21 sends, last refusal: Not attached to an active page): the page is frozen behind it");
  assert.match(frozenMessage({ declined: false, why: "no answer", last: "", sends: 1 }), /\(no answer after 1 sends, last refusal: none\)/);
});
