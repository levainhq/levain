// The page driver's wait for a reloaded document (the `init` step), kept apart from the driver so a node test can run its decisions against
// a stub page (tests/_reload_wait.test.mjs). Nothing here touches CDP or the clock itself: the driver hands in what it can do.
//
// What the wait must tell apart:
//   a reload that never COMMITTED   (the old document is still the page, or nothing answers, and no main-frame Page.frameNavigated event has arrived)
//   a reload that committed and is only SLOW   (the old mark is gone, the new document is loading)
// The first is sent once more; the second is only waited for, always: sending a second reload into a load that is going to finish throws that
// load away, and the retry then needs a whole load of its own. So the resend is gated on commit, never on how long the load has taken.

// the page's own answer: whether the document is still the one the driver marked, and how far the document has loaded
export const NEW_DOCUMENT_PROBE = "({ old: window.__driveOldDocument === true, ready: document.readyState })";

// a probe outcome ({ value } or { error }, as the driver's evaluateOutcome gives it) that came from a document without the old mark
export const sawNewDocument = (o) => !!o && !("error" in o) && !!o.value && o.value.old === false;
export const reloadFinished = (o) => sawNewDocument(o) && o.value.ready === "complete";

// deps (all supplied by the caller):
//   probe(ms)         -> the outcome of NEW_DOCUMENT_PROBE, giving up after ms
//   committed()       -> whether a main-frame commit event has arrived since the first reload was sent
//   resend()          -> { ok: true } or { ok: false, why } once the second Page.reload has been sent (an unanswered or refused one is not ok)
//   now(), sleep(ms)  -> the clock
//   boundMs           -> the bound of one load; the first reload gets it from the start, a resent one gets it afresh from the resend
//   resendAfterMs     -> when, with nothing committed, the one resend is due
// Returns { ok: true, resent } or { ok: false, why, resent }.
export async function waitForReloadedDocument({ probe, committed, resend, now, sleep, boundMs, resendAfterMs, probeMs = 3000, probeFloorMs = 250, pollMs = 100 }) {
  const t0 = now();
  const resendAt = t0 + resendAfterMs;
  let end = t0 + boundMs; let resent = false; let seenNew = false;
  const resendWanted = () => !resent && !seenNew && !committed();
  for (;;) {
    // while a resend is still possible the probe is cut at its due time, so the decision is made when it is due and not up to a probe
    // late: a probe that never answers would otherwise hold the whole probe window past it
    const horizon = resendWanted() ? Math.min(resendAt, end) : end;
    const o = await probe(Math.min(probeMs, Math.max(horizon - now(), probeFloorMs)));
    if (sawNewDocument(o)) seenNew = true;
    if (reloadFinished(o)) return { ok: true, resent };
    if (now() >= end) return { ok: false, why: "init: the reload did not complete", resent };
    if (resendWanted() && now() >= resendAt) {
      resent = true;
      const r = await resend();
      if (!r.ok) return { ok: false, why: "init: the reload could not be sent again: " + r.why, resent };
      end = Math.max(end, now() + boundMs);
    }
    await sleep(pollMs);
  }
}
