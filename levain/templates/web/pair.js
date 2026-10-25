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
  function tryAgain(code, title, msg) {
    say(title, msg, false);
    actions([["Try again", true, () => attempt(code)]]);
  }

  async function attempt(code) {
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
      tryAgain(code, "Could not reach the cockpit", "Check that this device is on the same network as the cockpit, then try again. The code is not used yet.");
      return;
    }
    if (res.status === 403) {
      say("This pairing code did not work", "This pairing code is used, expired or wrong; make a new one on a paired device.", false);
      return;
    }
    if (res.status >= 500) {
      tryAgain(code, "The cockpit is busy", "It is still starting or another process holds it. Try again in a moment.");
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
      window.localStorage.setItem(PAIRED_MARK_KEY, "1");
    } catch (_) {
      say("This browser will not keep the key", "Storage is blocked here (a private window?). Open this link in a normal window; the code is already spent, so make a new one.", false);
      return;
    }
    data = null;
    say("Paired", "This device is paired. Opening the cockpit…", true);
    location.replace("/");
  }

  function run() {
    const code = takeCode();
    if (!code) {
      say("No pairing code", "Open this page from the link on a paired device (Devices, then Pair a device).", true);
      return;
    }
    if (storedToken()) {
      // L3 r1 F5: never replace a key this browser already holds without asking (it may be the owner's own)
      say("This browser is already paired", "Pairing it again replaces the key it holds now. The code is not used until you choose.", false);
      actions([["Pair it again", true, () => attempt(code)], ["Keep the current key", false, () => location.replace("/")]]);
      return;
    }
    attempt(code);
  }

  run();
})();
