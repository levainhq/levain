// Node tests of the reload wait's decisions (tests/_reload_wait.mjs) against stub pages on a virtual clock: no Chrome, no real time.
// Run by tests/test_reload_wait.py; also runnable by hand: node --test tests/_reload_wait.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { waitForReloadedDocument, sawNewDocument, reloadFinished } from "./_reload_wait.mjs";

const BOUND = 15000, RESEND_AFTER = 7000, PROBE_MS = 3000, FLOOR = 250;

// A stub page. `page(t)` says what the page does at virtual time t (ms since the first reload was sent):
//   { silent: true }                        no answer (the probe costs its whole timeout)
//   { old: bool, ready: string }            an answer
// `commitAt(t)` says whether a main-frame commit event has arrived by t. `resendHelps` says whether a resent reload makes the page commit and finish.
function rig({ page, commitAt = () => false, resendOk = { ok: true } }) {
  let T = 0; let resendAtT = null; let resends = 0;
  const effective = (t) => (resendAtT !== null && resendHelps(t) ? { old: false, ready: t - resendAtT >= 2000 ? "complete" : "loading" } : page(t));
  let resendHelps = () => false;
  const api = {
    now: () => T,
    sleep: (ms) => { T += ms; },
    probe: async (ms) => {
      const p = effective(T);
      if (p.silent) { T += ms; return { error: `no answer from the page within ${ms}ms` }; }
      T += 5;
      return { value: { old: p.old, ready: p.ready } };
    },
    committed: () => commitAt(T) || (resendAtT !== null && resendHelps(T)),
    resend: async () => { resends++; resendAtT = T; T += 20; return resendOk; },
  };
  return { api, helps: (f) => { resendHelps = f; }, state: () => ({ T, resends, resendAtT }) };
}
const run = (r) => waitForReloadedDocument({ ...r.api, boundMs: BOUND, resendAfterMs: RESEND_AFTER, probeMs: PROBE_MS, probeFloorMs: FLOOR });

test("a load that committed and is only slow is never reloaded again, and passes when it completes", async () => {
  // committed early, still loading when the resend would be due: a resend then would throw the load away and leave it too little time (the failure this gate removes)
  const r = rig({ page: (t) => (t < 1000 ? { old: true, ready: "complete" } : { old: false, ready: t < 12000 ? "loading" : "complete" }), commitAt: (t) => t >= 1000 });
  const out = await run(r);
  assert.equal(out.ok, true);
  assert.equal(out.resent, false);
  assert.equal(r.state().resends, 0);
});

test("a slow load whose commit event was missed is still not reloaded: the probe saw a document without the old mark", async () => {
  const r = rig({ page: (t) => (t < 1000 ? { old: true, ready: "complete" } : { old: false, ready: t < 12000 ? "loading" : "complete" }) });
  const out = await run(r);
  assert.equal(out.ok, true);
  assert.equal(out.resent, false);
  assert.equal(r.state().resends, 0);
});

test("a commit that is busy (the probe unanswered, the commit event seen) is waited for, not reloaded", async () => {
  const r = rig({ page: (t) => (t < 9000 ? { silent: true } : { old: false, ready: "complete" }), commitAt: (t) => t >= 500 });
  const out = await run(r);
  assert.equal(out.ok, true);
  assert.equal(out.resent, false);
  assert.equal(r.state().resends, 0);
});

test("a reload that never commits (nothing answers) is sent once more, on time, and the retry has a whole bound of its own", async () => {
  const r = rig({ page: () => ({ silent: true }) });
  r.helps((t) => t >= 0 && r.state().resendAtT !== null);
  const out = await run(r);
  assert.equal(out.ok, true);
  assert.equal(out.resent, true);
  const s = r.state();
  assert.equal(s.resends, 1);
  // the resend is made within one probe floor of its due time, not up to a whole probe window late
  assert.ok(s.resendAtT >= RESEND_AFTER && s.resendAtT <= RESEND_AFTER + FLOOR + 50, `resent at ${s.resendAtT}`);
});

test("a reload that never commits and is not helped by the resend fails once, after the retry's own bound", async () => {
  const r = rig({ page: () => ({ silent: true }) });
  const out = await run(r);
  assert.equal(out.ok, false);
  assert.match(out.why, /the reload did not complete/);
  const s = r.state();
  assert.equal(s.resends, 1);
  assert.ok(s.T >= s.resendAtT + BOUND, `gave up at ${s.T}, resent at ${s.resendAtT}`);
  assert.ok(s.T <= s.resendAtT + BOUND + PROBE_MS + 100, `gave up at ${s.T}`);
});

test("the old document still answering with its mark and no commit is reloaded once", async () => {
  const r = rig({ page: () => ({ old: true, ready: "complete" }) });
  r.helps(() => true);
  const out = await run(r);
  assert.equal(out.ok, true);
  assert.equal(out.resent, true);
  assert.equal(r.state().resends, 1);
});

test("a resend that is refused or unanswered fails the step by name and never goes on waiting with a reload outstanding", async () => {
  const r = rig({ page: () => ({ silent: true }), resendOk: { ok: false, why: "no answer within 3000ms" } });
  const out = await run(r);
  assert.equal(out.ok, false);
  assert.match(out.why, /could not be sent again: no answer within 3000ms/);
  assert.equal(r.state().resends, 1);
});

test("the probe outcome readers", () => {
  assert.equal(sawNewDocument({ error: "x" }), false);
  assert.equal(sawNewDocument({ value: null }), false);
  assert.equal(sawNewDocument({ value: { old: true, ready: "complete" } }), false);
  assert.equal(sawNewDocument({ value: { old: false, ready: "loading" } }), true);
  assert.equal(reloadFinished({ value: { old: false, ready: "loading" } }), false);
  assert.equal(reloadFinished({ value: { old: false, ready: "complete" } }), true);
});
