// levain pair.js — the new device's half of per-device cockpit pairing (PAIR_DESIGN.md REV 1-6).
//
// A paired device's "Make a link" button shows a link `/pair#code=<one-time code>`. This page takes the code from the
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
  function tryAgain(code, title, msg, oldTok) {
    say(title, msg, false);
    actions([["Try again", true, () => attempt(code, true, oldTok)]]);
  }

  // `again`: this is a second spend of the same code after a failure that may have reached the server (G3).
  // `oldTok`: the key this browser held before Pair it again (snapshotted before anything else), or undefined; after the claim has
  // SUCCEEDED the Paired screen looks that key up (read-only) to say which Devices row it is.
  async function attempt(code, again, oldTok) {
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
      tryAgain(code, "Could not reach the cockpit", "Check that this device is on the same network as the cockpit, then try again. The code is not used yet.", oldTok);
      return;
    }
    if (res.status === 403) {
      // G3: after a Try again the first try may have spent the code and lost only its answer; say so, in one line
      say("This pairing code did not work", again
        ? "The first try may have used this code. If this device is not paired, make a new code on a paired device."
        : "This pairing code is used, expired or wrong; make a new one on a paired device. If you did not use it, revoke the newest device in Devices.", false);
      return;
    }
    if (res.status >= 500) {
      tryAgain(code, "The cockpit is busy", "It is still starting or another process holds it. Try again in a moment.", oldTok);
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
    if (oldTok) {
      // stay on the page so the line is read; the cockpit is one click away. The claim is done: nothing below can fail it.
      say("Paired", "This device is paired. Checking the key this browser held before…", true);
      const prev = await previousKey(oldTok);
      if (prev.kind === "owner" || prev.kind === "revoked") { say("Paired", "This device is paired. Opening the cockpit…", true); location.replace("/"); return; }
      if (prev.kind === "live") { sayLive(prev); return; }
      say("Paired", "This device is paired. " + PREVIOUS_MAYBE, true);
      return;
    }
    say("Paired", "This device is paired. Opening the cockpit…", true);
    location.replace("/");
  }

  // "Pair it again" only claims; the key this browser held is NOT revoked from here (L3 r3 H1: a client cannot do that safely).
  // The claim goes FIRST (L3 r5 K2: no read ever sits before the POST, so a short-lived code cannot expire behind a slow list).
  // AFTER it succeeded, the held key (snapshotted before the claim; a claim does not revoke it) is looked up with GETs only, so the
  // Paired screen can say which Devices row it is. The result is tri-state (K1): "owner" (a surface token, positively not a paired
  // row) and "revoked" (the row is revoked) say nothing; "live" names the label and the short id; anything else (a failed, slow or
  // malformed read, a missing row) is "unknown" and gets the generic line, because the old key may still be listed.
  const PAIRED_PREFIX = "paired-";               // MUST equal the server's PAIRED_PREFIX (web_server.py): the credential name's namespace
  const TOKEN_HEADER = "X-Levain-Write-Token";   // MUST equal devices_panel.js H
  const LOOKUP_MS = 8000;
  const LABEL_MAX = 60;
  const PREVIOUS_MAYBE = "This browser's previous key may still be listed in Devices. Revoke it there.";
  // MUST equal devices_panel.js shortId: the first 8 characters of the device id ("d-" + 6 hex); the line and every Devices row show the same
  function shortId(id) { return String(id).slice(0, 8); }
  // quote-safe and bounded: bidi/control characters out, 60 characters and an ellipsis at most
  function boundLabel(label) {
    const chars = Array.from(String(label).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, "").trim());
    return chars.length > LABEL_MAX ? chars.slice(0, LABEL_MAX).join("") + "\u2026" : chars.join("");
  }

  // The previous-key line for a live row. The label is untrusted text (the server allows RTL scripts), so it sits inside the quotes in a
  // <bdi> (its own direction, isolated from the sentence), built with textContent only; the short id is a no-wrap span.
  function sayLive(prev) {
    say("Paired", "", true);
    const bdi = document.createElement("bdi");
    bdi.textContent = prev.label;
    const id = document.createElement("span");
    id.className = "dev-id";
    id.textContent = prev.short;
    msgEl.append("This device is paired. This browser's previous key is still listed in Devices as \u201c", bdi, "\u201d (", id, "). Revoke it there.");
  }

  async function previousKey(token) {
    const unknown = { kind: "unknown" };
    let timer = null;
    try {
      const ctl = new AbortController();         // inside the try (K4): a browser without it falls to "unknown", never a stuck page
      timer = setTimeout(() => ctl.abort(), LOOKUP_MS);
      const get = (path) => fetch(path, { method: "GET", headers: { [TOKEN_HEADER]: token }, cache: "no-store", referrerPolicy: "no-referrer", signal: ctl.signal });
      const man = await get("/cockpit/manifest.json");
      if (man.status === 401 || man.status === 403) return { kind: "revoked" };   // M1: a refused key is dead: no line
      if (man.status !== 200) return unknown;
      const m = await man.json();
      // M1 (measured): a revoked or unknown key is NOT refused by the manifest: it answers 200 with credential.class "none" and no name
      if (m && m.credential && m.credential.class === "none") return { kind: "revoked" };
      const name = m && m.credential && m.credential.name;
      if (typeof name !== "string") return unknown;
      if (!name.startsWith(PAIRED_PREFIX)) return { kind: "owner" };
      const id = name.slice(PAIRED_PREFIX.length);
      const lst = await get("/cockpit/pair/devices.json");
      if (lst.status !== 200) return unknown;
      const l = await lst.json();
      const row = l && Array.isArray(l.devices) ? l.devices.find((d) => d && String(d.id) === id) : null;
      if (!row) return unknown;
      if (row.revoked_at) return { kind: "revoked" };
      const label = typeof row.label === "string" ? boundLabel(row.label) : "";
      return { kind: "live", label: label || "paired device", short: shortId(id) };
    } catch (_) {
      return unknown;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function pairAgain(code) {
    attempt(code, false, storedToken());           // the held key is snapshotted here, before the claim can replace it
  }

  function run() {
    const code = takeCode();
    if (!code) {
      say("No pairing code", "Open this page from the link on a paired device (Devices, then Make a link).", true);
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
