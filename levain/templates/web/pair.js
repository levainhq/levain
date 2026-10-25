// levain pair.js — the new device's half of per-device cockpit pairing (PAIR_DESIGN.md REV 1-6).
//
// A paired device's "Pair a device" button shows a link `/pair#code=<one-time code>`. This page takes the code from the
// FRAGMENT (never sent to a server, never in a log or a Referer), scrubs it from the address bar at once, spends it with one
// credential-less POST /cockpit/pair/claim {code} (the label was typed on the offering device, RP-6: the claim accepts the
// code and nothing else), keeps the returned token where the cockpit already keeps its own (localStorage
// "levain_write_token", dashboard_boot.js WRITE_TOKEN_KEY), and goes to the cockpit. The token is shown to no one.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";   // MUST equal dashboard_boot.js WRITE_TOKEN_KEY: the cockpit reads this key
  const titleEl = document.getElementById("pair-title");
  const msgEl = document.getElementById("pair-msg");
  const nextEl = document.getElementById("pair-next");

  function say(title, msg, home) {
    titleEl.textContent = title;
    msgEl.textContent = msg;
    nextEl.hidden = !home;
  }

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

  async function run() {
    const code = takeCode();
    if (!code) {
      say("No pairing code", "Open this page from the link on a paired device (Devices, then Pair a device).", true);
      return;
    }
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
      say("Could not reach the cockpit", "Check that this device is on the same network as the cockpit, then make a new pairing code.", false);
      return;
    }
    if (res.status === 403) {
      say("This pairing code did not work", "This pairing code is used, expired or wrong; make a new one on a paired device.", false);
      return;
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* handled below */ }
    if (res.status === 503) {
      say("The cockpit is busy", "It is still starting or another process holds it. Make a new pairing code and try again shortly.", false);
      return;
    }
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
    data = null;
    say("Paired", "This device is paired. Opening the cockpit…", true);
    location.replace("/");
  }

  run();
})();
