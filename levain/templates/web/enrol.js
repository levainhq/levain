// levain enrol.js — make a passkey on THIS device for the cockpit's name, and print the laptop commands that enrol it.
//
// Why this page exists: a passkey belongs to one https name (its rpId), so the cockpit moving to a new name strands every
// passkey made on the old one, and the only page that ever made one (the old phone port's) is gone from this origin.
// POST /cockpit/passkey/key-line (a gated route since OS-2) turns a new credential's public key into the OpenSSH line and
// the laptop commands that enrol it; until now nothing in the cockpit called it.
//
// The page enrols NOTHING: a passkey cannot enrol itself. It makes the credential here, asks the cockpit for the commands,
// shows them, and remembers which credential id belongs to the key (the sign sheet asks for that one by id). The enrolment
// is the laptop's, signed by the laptop-presence key.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";   // MUST equal dashboard_boot.js WRITE_TOKEN_KEY: the cockpit reads this key
  const KEY_LINE = "/cockpit/passkey/key-line";
  const MAP_KEY = (rp) => "levain-passkeys:" + rp;   // MUST equal dashboard_core.js signMapKey / sign.js mapKey
  const MADE = "levain-enrol-made";                  // sessionStorage: a made credential kept until its key line is sent
  const FETCH_TIMEOUT_MS = 15000;
  const $ = (id) => document.getElementById(id);
  const titleEl = $("enrol-title"), msgEl = $("enrol-msg"), actEl = $("enrol-actions"), cmdEl = $("enrol-cmds");

  // the name a passkey is made for is the page's own host. Where the browser gives the creation's authenticatorData the
  // cockpit refuses a credential made for another name (the data names the rpId); where it does not, the commands carry no
  // --attested and this page says so
  const rpId = location.hostname;
  const usable = window.isSecureContext === true && typeof window.PublicKeyCredential === "function"
    && !!(navigator.credentials && navigator.credentials.create);

  const b64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const rand = (n) => { const a = new Uint8Array(n); crypto.getRandomValues(a); return a; };
  function storedToken() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (e) { return ""; } }
  function remember(fp, rawId) {
    try {
      let m = {};
      try { m = JSON.parse(window.localStorage.getItem(MAP_KEY(rpId)) || "{}") || {}; } catch (e) { m = {}; }
      if (typeof m !== "object") m = {};
      m[fp] = rawId;
      window.localStorage.setItem(MAP_KEY(rpId), JSON.stringify(m));
    } catch (e) { /* kept nowhere: the sign sheet then asks the OS's own list */ }
  }
  let made = null;
  try { made = JSON.parse(window.sessionStorage.getItem(MADE) || "null"); } catch (e) { made = null; }
  function setMade(v) {
    made = v;
    try { if (v) window.sessionStorage.setItem(MADE, JSON.stringify(v)); else window.sessionStorage.removeItem(MADE); } catch (e) { /* memory only */ }
  }

  function say(title, msg) { titleEl.textContent = title; msgEl.textContent = msg; }
  function buttons(list) {
    actEl.replaceChildren();
    for (const [text, primary, fn] of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "dev-btn" + (primary ? " dev-primary" : "");
      b.textContent = text;
      b.addEventListener("click", fn);
      actEl.appendChild(b);
    }
    actEl.hidden = !list.length;
  }

  // one POST: {ok, status, body}; status 0 when no whole reply came
  function post(body) {
    const ac = typeof AbortController === "function" ? new AbortController() : null;
    const timer = setTimeout(() => { if (ac) ac.abort(); }, FETCH_TIMEOUT_MS);
    return fetch(KEY_LINE, { method: "POST", cache: "no-store", signal: ac ? ac.signal : undefined,
                             headers: { "Content-Type": "application/json", "X-Levain-Write-Token": storedToken() },
                             body: JSON.stringify(body) })
      .then((res) => res.json().then((j) => ({ ok: res.ok, status: res.status, body: j && typeof j === "object" ? j : {} }),
                                     () => ({ ok: false, status: res.status, body: {} })),
            () => ({ ok: false, status: 0, body: { message: "the cockpit could not be reached" } }))
      .finally(() => clearTimeout(timer));
  }
  const why = (r) => ((r.body && typeof r.body.message === "string" && r.body.message.replace(/[.\s]+$/, "")) || ("refused (HTTP " + r.status + ")"));

  function showCommands(r, c) {
    const name = c.name;
    const fp = r.body.fingerprint;
    say("Passkey made", (!c.authData ? "Not attested by this browser: the cockpit cannot check which name it was made for. " : "") + "Fingerprint " + fp + (name ? ', named "' + name + '"' : "") + ". A passkey cannot enrol itself: on the laptop, run these "
        + "in order (the second is signed by your laptop key), then open the cockpit again. Nothing is enrolled until you do."
        + (r.body.note ? " " + r.body.note : ""));
    const lines = Array.isArray(r.body.commands) ? r.body.commands : [];
    cmdEl.replaceChildren(...lines.map((cmd) => {
      const row = document.createElement("div"), code = document.createElement("code"), copy = document.createElement("button");
      row.className = "enrol-cmd";
      code.textContent = cmd;
      copy.type = "button";
      copy.className = "dev-btn";
      copy.textContent = "copy";
      copy.addEventListener("click", () => {
        (navigator.clipboard ? navigator.clipboard.writeText(cmd) : Promise.reject(new Error("no clipboard")))
          .then(() => { copy.textContent = "copied"; }, () => { copy.textContent = "select it"; });
      });
      row.append(code, copy);
      return row;
    }));
    buttons([["Make another passkey", false, start]]);
  }

  // the made credential is kept until its key line is sent, so a failed send retries only the send, never makes a second passkey
  function send(c) {
    const body = { spki: c.spki };
    if (c.authData) body.authenticator_data = c.authData;
    return post(body).then((r) => {
      if (!r.ok) {
        // a refused key (422) will never be accepted: drop it so the next tap makes a fresh passkey
        if (r.status === 422) { setMade(null); say("Not accepted", "The cockpit refused this passkey: " + why(r) + ". Nothing was enrolled; make another."); buttons([["Make a passkey", true, start]]); return; }
        say("Passkey made, not sent", "The cockpit did not take its key (" + why(r) + "). The passkey is kept: send it again.");
        buttons([["Send the key again", true, () => { buttons([]); send(made || c); }],
                 ["Discard it and make another", false, () => { setMade(null); start(); }]]);
        return;
      }
      remember(r.body.fingerprint, c.rawId);
      setMade(null);
      showCommands(r, c);
    });
  }

  function start() {
    buttons([]);
    say("Make a passkey on this device", "Asking this device for a passkey for " + rpId + "…");
    const pad = (n) => String(n).padStart(2, "0"), now = new Date(), uid = rand(16);
    const name = "levain " + now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate())
      + " " + pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds())
      + " " + Array.from(uid.slice(0, 2), (b) => b.toString(16).padStart(2, "0")).join("");
    const options = { publicKey: {
      rp: { id: rpId, name: "levain" }, user: { id: uid, name: name, displayName: name },
      challenge: rand(32), pubKeyCredParams: [{ type: "public-key", alg: -7 }], attestation: "none", timeout: 60000,
      authenticatorSelection: { residentKey: "required", userVerification: "required" } } };
    let asked;
    try { asked = made ? Promise.resolve(made) : navigator.credentials.create(options); } catch (e) { asked = Promise.reject(e); }
    // the creation and the read of its key are separate steps: a passkey the device made but this page cannot read is said
    // aloud, by name, because it now sits in the device's passwords with nothing to enrol
    const read = (cred) => {
      if (cred && cred === made) return made;
      const r = cred.response;
      if (!r || typeof r.getPublicKey !== "function" || !r.getPublicKey()) {
        const e = new Error("no public key");
        e.name = "NoPublicKey";
        throw e;
      }
      setMade({ rawId: b64u(cred.rawId), spki: b64u(r.getPublicKey()), name: name,
                authData: typeof r.getAuthenticatorData === "function" ? b64u(r.getAuthenticatorData()) : "" });
      return made;
    };
    return asked.then((cred) => {
      let kept;
      try { kept = read(cred); } catch (e) {
        say("A passkey was made, and this browser cannot enrol it", "It did not give the page the passkey's public key (" + ((e && e.name) || "error")
            + "). A passkey named \"" + name + "\" now sits in this device's passwords with nothing to enrol: delete it there, then make one on a browser that can.");
        buttons([["Make a passkey", true, start]]);
        return undefined;
      }
      return send(kept);
    }, (e) => {
      say("No passkey was made", "This device did not make one (" + ((e && e.name) || "cancelled") + "). Nothing changed; tap to try again.");
      buttons([["Make a passkey", true, start]]);
    }).catch((e) => {
      say("The passkey step failed", "(" + ((e && e.name) || "error") + "). Nothing was enrolled; tap to try again.");
      buttons([["Make a passkey", true, start]]);
    });
  }

  if (!storedToken()) {
    say("This device is not paired", "Open the cockpit on this device first (its link carries the key), or pair it from a paired device: Devices, Make a link.");
  } else if (!usable) {
    say("This page cannot make a passkey here", "A passkey needs the cockpit's https page (" + (location.protocol === "https:" ? "this browser has no passkey support" : "this page is not https") + ").");
  } else if (made) {
    say("A passkey was made and not yet sent", "Its key is kept for " + rpId + ": send it again.");
    buttons([["Send the key again", true, () => { buttons([]); send(made); }],
             ["Discard it and make another", false, () => { setMade(null); start(); }]]);
  } else {
    say("Make a passkey on this device", "This makes a passkey for " + rpId + " on this device. The laptop then enrols it with a command this page prints; until it is enrolled the passkey signs nothing.");
    buttons([["Make a passkey", true, start]]);
  }
})();
