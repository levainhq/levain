// The page driver's decline of a JavaScript dialog, kept apart from the driver so a node test can run its decisions against stub sends
// (tests/_dialog_decline.test.mjs). Nothing here touches CDP or the clock itself: the caller hands in what it can do.
//
// Why a retry: Chrome can open the dialog in the window where the new document is committed but the page target is not yet attached, and
// Page.handleJavaScriptDialog is then refused with "Not attached to an active page". Nothing else closes the dialog, and an open dialog freezes
// the page, so a single refused decline hangs everything after it. That one refusal, and only that one, is a "try again in a moment".
// Any other answer is final: "No dialog is showing" means the dialog is already closed, and a lost connection will not come back.
//
// Why a retry is not the whole answer: whether the refusal clears while the dialog stays open is not established, so a decline that gives up is
// reported in a form the caller can fail the drive on by name (leavesPageFrozen, frozenMessage), rather than leaving the page to time out elsewhere.

export const NOT_ATTACHED = /Not attached to an active page/;
export const NO_DIALOG = /No dialog is showing/;

// A send that does not answer within `ms` resolves { timedOut: true } instead of never. `sleep` is the caller's clock.
export const boundedSend = (send, sleep, ms) => (method, params) =>
  Promise.race([send(method, params), sleep(ms).then(() => ({ timedOut: true }))]);

// deps (all supplied by the caller):
//   send(method, params) -> the reply object ({ result } or { error: { message } }), or { timedOut: true } when it was not answered in time (see
//                           boundedSend); a rejection is a send that failed outright
//   now(), sleep(ms)     -> the clock
//   boundMs              -> the wall time after which a still-refused decline is given up
//   pauseMs              -> the pause between one refusal and the next send
// Returns { declined: true, sends } or { declined: false, why: "error" | "bound" | "no answer", last, sends }, `last` being the last refusal's text
// ("" when there was none).
export async function declineDialog({ send, now, sleep, boundMs, pauseMs }) {
  const t0 = now(); let sends = 0; let last = "";
  for (;;) {
    let reply;
    sends++;
    try { reply = await send("Page.handleJavaScriptDialog", { accept: false }); }   // decline, exactly as a driver without a token would: never accept
    catch (e) { return { declined: false, why: "error", last: String((e && e.message) || e), sends }; }
    if (reply && reply.timedOut) return { declined: false, why: "no answer", last, sends };   // an unanswered send ends the loop: waiting on it again would hide the stall
    if (!reply || !reply.error) return { declined: true, sends };
    last = String(reply.error.message || JSON.stringify(reply.error));
    if (!NOT_ATTACHED.test(last)) return { declined: false, why: "error", last, sends };
    if (now() - t0 >= boundMs) return { declined: false, why: "bound", last, sends };
    await sleep(pauseMs);
  }
}

// Does this outcome leave the dialog open, and the page frozen behind it? Every outcome that is not a decline does, except an error saying no dialog
// is showing any more: that dialog closed by itself (a navigation took it), so nothing is left to decline.
export const leavesPageFrozen = (out) => !out.declined && !(out.why === "error" && NO_DIALOG.test(out.last));

export const frozenMessage = (out) =>
  `a JavaScript dialog could not be declined (${out.why} after ${out.sends} sends, last refusal: ${String(out.last || "none").slice(0, 120)}): the page is frozen behind it`;
