// The page driver's decline of a JavaScript dialog, kept apart from the driver so a node test can run its decisions against stub sends
// (tests/_dialog_decline.test.mjs). Nothing here touches CDP or the clock itself: the driver hands in what it can do.
//
// Why a retry: Chrome can open the dialog in the window where the new document is committed but the page target is not yet attached, and
// Page.handleJavaScriptDialog is then refused with "Not attached to an active page". Nothing else closes the dialog, and an open dialog freezes
// the page, so a single refused decline hangs everything after it. That one refusal, and only that one, is a "try again in a moment".
// Any other answer is final: "No dialog is showing" means the dialog is already closed, and a lost connection will not come back.

export const NOT_ATTACHED = /Not attached to an active page/;

// deps (all supplied by the caller):
//   send(method, params) -> the reply object ({ result } or { error: { message } }); a rejection is a send that failed outright
//   now(), sleep(ms)     -> the clock
//   boundMs              -> the wall time after which a still-refused decline is given up
//   pauseMs              -> the pause between one refusal and the next send
// Returns { declined: true, sends } or { declined: false, why: "error" | "bound", last, sends }, `last` being the last refusal's text.
export async function declineDialog({ send, now, sleep, boundMs, pauseMs }) {
  const t0 = now(); let sends = 0; let last = "";
  for (;;) {
    let reply;
    sends++;
    try { reply = await send("Page.handleJavaScriptDialog", { accept: false }); }   // decline, exactly as a driver without a token would: never accept
    catch (e) { return { declined: false, why: "error", last: String((e && e.message) || e), sends }; }
    if (!reply || !reply.error) return { declined: true, sends };
    last = String(reply.error.message || JSON.stringify(reply.error));
    if (!NOT_ATTACHED.test(last)) return { declined: false, why: "error", last, sends };
    if (now() - t0 >= boundMs) return { declined: false, why: "bound", last, sends };
    await sleep(pauseMs);
  }
}
