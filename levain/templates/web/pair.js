// levain pair.js — the new device's half of per-device cockpit pairing (PAIR_DESIGN.md REV 1-6).
//
// A paired device's "Pair a device" button shows a link `/pair#code=<one-time code>`. This page takes the code from the
// FRAGMENT (never sent to a server, never in a log or a Referer), scrubs it from the address bar at once, spends it with a
// credential-less POST /cockpit/pair/claim {code} (the label was typed on the offering device, RP-6: the claim accepts the
// code and nothing else), keeps the returned token where the cockpit already keeps its own (localStorage
// "levain_write_token", dashboard_boot.js WRITE_TOKEN_KEY), and goes to the cockpit. The token is shown to no one.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";   // MUST equal dashboard_boot.js WRITE_TOKEN_KEY: the cockpit reads this key
  const titleEl = document.getElementById("pair-title");
  const msgEl = document.getElementById("pair-msg");
  const nextEl = document.getElementById("pair-next");
  const actEl = document.getElementById("pair-actions");
  // This browser was paired by a link (not by the owner's own startup link): dashboard_boot.js reads it to show "this device
  // was unpaired" in place of its token prompt when this browser's key is later refused (L3 r1 F6). MUST equal its key.
  const PAIRED_MARK_KEY = "levain_paired_device";

  function say(title, msg, home) {
    titleEl.textContent = title;
    msgEl.textContent = msg;
    nextEl.hidden = !home;
    actEl.replaceChildren();
    actEl.hidden = true;
  }
  // one or two buttons under the message; the code stays in this page's memory (the address bar is already scrubbed)
  function actions(btns) {
    actEl.replaceChildren();
    for (const [text, primary, fn] of btns) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "dev-btn" + (primary ? " dev-primary" : "");
      b.textContent = text;
      b.addEventListener("click", fn);
      actEl.appendChild(b);
    }
    actEl.hidden = false;
  }
  function storedToken() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }

  function takeCode() {
    let code = "";
    try {
      const params = new URLSearchParams((location.hash || "").replace(/^#/, ""));
      code = params.get("code") || "";
      params.delete("code");
      const rest = params.toString();
      history.replaceState(null, "", location.pathname + location.search + (rest ? "#" + rest : ""));
    } catch (_) { /* no history API: the code stays in the bar, the claim still works */ }
    return code.trim();
  }

  async function claim(code) {
    return fetch("/cockpit/pair/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code }),
      cache: "no-store",
      referrerPolicy: "no-referrer",
    });
  }

  // A network error or a 5xx after the one RT-4 retry leaves the code UNSPENT (L3 r1 F4): offer to try it again rather
  // than send him back for a new one. A 403 is final (used, expired or wrong).
  function tryAgain(code, title, msg, repaired) {
    say(title, msg, false);
    actions([["Try again", true, () => attempt(code, true, repaired)]]);
  }

  // `again`: this is a second spend of the same code after a failure that may have reached the server (G3).
  // `repaired`: this browser held a key before (Pair it again); the "Paired" screen then says the old key is still listed.
  async function attempt(code, again, repaired) {
    say("Pairing this device", "Claiming your one-time code…", false);
    let res;
    try {
      res = await claim(code);
      if (res.status === 503) {
        // RT-4: the cockpit is waking; retry once after the server's own delay
        const wait = Math.min(10, Math.max(1, parseInt(res.headers.get("Retry-After") || "2", 10) || 2));
        say("Pairing this device", "The cockpit is starting. Trying again in a moment…", false);
        await new Promise((r) => setTimeout(r, wait * 1000));
        res = await claim(code);
      }
    } catch (_) {
      tryAgain(code, "Could not reach the cockpit", "Check that this device is on the same network as the cockpit, then try again. The code is not used yet.", repaired);
      return;
    }
    if (res.status === 403) {
      // G3: after a Try again the first try may have spent the code and lost only its answer; say so, in one line
      say("This pairing code did not work", again
        ? "The first try may have used this code. If this device is not paired, make a new code on a paired device."
        : "This pairing code is used, expired or wrong; make a new one on a paired device.", false);
      return;
    }
    if (res.status >= 500) {
      tryAgain(code, "The cockpit is busy", "It is still starting or another process holds it. Try again in a moment.", repaired);
      return;
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* handled below */ }
    if (res.status !== 200 || !data || typeof data.token !== "string" || !/^[\x21-\x7e]+$/.test(data.token)) {
      say("Pairing failed", "The cockpit did not hand back a usable key. Make a new pairing code on a paired device.", false);
      return;
    }
    try {
      window.localStorage.setItem(WRITE_TOKEN_KEY, data.token);
    } catch (_) {
      say("This browser will not keep the key", "Storage is blocked here (a private window?). Open this link in a normal window; the code is already spent, so make a new one.", false);
      return;
    }
    // G4: the mark is a nicety (it only picks the unpaired card over a prompt later); a failed write of it never says the key was lost
    try { window.localStorage.setItem(PAIRED_MARK_KEY, "1"); } catch (_) { /* the key is kept; the mark is not */ }
    data = null;
    if (repaired) {
      // stay on the page so the line is read; the cockpit is one click away
      say("Paired", "This device is paired. " + OLD_KEY_LISTED, true);
      return;
    }
    say("Paired", "This device is paired. Opening the cockpit…", true);
    location.replace("/");
  }

  // "Pair it again" only claims. The key this browser held is NOT revoked from here (L3 r3 H1): a client cannot do that safely
  // (revoking first strands the browser when the claim fails; revoking after can cascade onto the device that issued the code).
  // The old row stays listed in Devices; the Paired screen says so on every path, Try again included.
  const OLD_KEY_LISTED = "This browser's previous key is still listed in Devices. Revoke it there.";

  function pairAgain(code) { attempt(code, false, true); }

  function run() {
    const code = takeCode();
    if (!code) {
      say("No pairing code", "Open this page from the link on a paired device (Devices, then Pair a device).", true);
      return;
    }
    if (storedToken()) {
      // L3 r1 F5: never replace a key this browser already holds without asking (it may be the owner's own)
      say("This browser is already paired", "Pairing it again replaces the key it holds now. The code is not used until you choose.", false);
      actions([["Pair it again", true, () => pairAgain(code)], ["Keep the current key", false, () => location.replace("/")]]);
      return;
    }
    attempt(code);
  }

  run();
})();
