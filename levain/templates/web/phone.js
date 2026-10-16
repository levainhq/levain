// levain phone port (Q6b): answer consent requests with a passkey. The server hands back the exact answer record and
// its challenge; the passkey signs it; the consent queue verifies the signature with ssh-keygen, as for a terminal
// answer. This page decides nothing: every refusal shown is the server's own words.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls;
                                   if (text != null) n.textContent = text; return n; };
  const b64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64u = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                        (c) => c.charCodeAt(0));
  const store = {           // browser storage can be absent or throw (private mode); every use survives that
    get(area, k) { try { return window[area].getItem(k); } catch (e) { return null; } },
    set(area, k, v) { try { window[area].setItem(k, v); } catch (e) { /* kept in memory only */ } },
  };

  // the token arrives once in the link's fragment; it is kept for this tab and taken out of the address bar
  let token = null;
  const m = /(?:^#|&)phone_token=([^&]+)/.exec(location.hash);
  if (m) { token = decodeURIComponent(m[1]); store.set("sessionStorage", "levain-phone-token", token);
           history.replaceState(null, "", location.pathname); }
  else token = store.get("sessionStorage", "levain-phone-token");

  const usable = window.isSecureContext === true && typeof window.PublicKeyCredential === "function"
    && !!(navigator.credentials && navigator.credentials.get);
  let rpId = location.hostname;
  // which enrolled passkey THIS browser made: fingerprint -> credential id, so the OS prompt is asked for that one
  const mapKey = () => "levain-passkeys:" + rpId;
  const mine = () => { try { return JSON.parse(store.get("localStorage", mapKey()) || "{}") || {}; } catch (e) { return {}; } };
  const remember = (fp, rawId) => { const all = mine(); all[fp] = rawId; store.set("localStorage", mapKey(), JSON.stringify(all)); };

  function call(method, route, body) {
    const headers = { "X-Levain-Phone-Token": token || "" };
    if (body) headers["Content-Type"] = "application/json";
    return fetch(route, { method: method, cache: "no-store", headers: headers, body: body ? JSON.stringify(body) : undefined })
      .then((res) => res.json().then((j) => ({ ok: res.ok, status: res.status, body: j && typeof j === "object" ? j : {} }),
                                     () => ({ ok: false, status: res.status, body: {} })),
            () => ({ ok: false, status: 0, body: { message: "the phone port could not be reached" } }));
  }
  const why = (r) => r.body.message || ("refused (HTTP " + r.status + ")");

  let busy = 0;               // a ceremony in progress holds the list still
  let last = "";              // the last answer given here: an answered request leaves the list, its result stays
  const notes = new Map();    // request id -> [text, kind], kept across redraws

  function draw(data) {
    rpId = data.rp_id || rpId;
    const list = $("requests");
    const rows = Array.isArray(data.pending) ? data.pending : [];
    $("status").textContent = (rows.length ? rows.length + " waiting on you" : "nothing is waiting on you") + (last ? " \u00b7 last: " + last : "");
    list.replaceChildren(...rows.map(row));
    const enrolled = Array.isArray(data.enrolled) ? data.enrolled : [];
    const local = mine();
    const ours = enrolled.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    $("enrol").hidden = !usable;
    if (ours.length) $("enrol-out").replaceChildren();      // enrolled: the laptop commands are done with
    $("enrol-note").textContent = ours.length
      ? "this phone's passkey is enrolled (" + ours[0].slice(0, 18) + "…). Make another only for a new device."
      : enrolled.length
        ? "a passkey is enrolled for " + rpId + ", not one this browser made. Make one here if this phone has none."
        : "no passkey is enrolled for " + rpId + " yet. Make one here, then enrol it from the laptop.";
    if (!usable) $("status").textContent += " · this browser cannot use a passkey here (needs https)";
  }

  function row(r) {
    const card = el("article", "req");
    const head = el("div", "req-head");
    head.append(el("span", "tool", r.tool || "(tool)"), el("span", "tier" + (r.tier === "T3" ? " t3" : ""), r.tier || ""),
                el("span", "meta", r.agent || ""));
    card.append(head, el("pre", "display", typeof r.display === "string" ? r.display : ""));
    if (r.cwd) card.appendChild(el("div", "where", "in " + r.cwd));
    if (r.targets) card.appendChild(el("div", "where", "touches " + r.targets));
    const note = el("div", "note");
    const say = (text, kind) => { notes.set(r.id, [text, kind || ""]); note.textContent = text; note.className = "note " + (kind || ""); };
    if (notes.has(r.id)) say(notes.get(r.id)[0], notes.get(r.id)[1]);
    if (r.note) { say(r.note); card.appendChild(note); return card; }          // a cut display is never signed here
    const signers = Array.isArray(r.signers) ? r.signers : [];
    if (!signers.length) { if (!notes.has(r.id)) say(r.signer_note || "no passkey is enrolled for this name"); card.appendChild(note); return card; }
    const actions = el("div", "actions");
    // a passkey is a display-phone key: it denies any request and allows up to T2 (the server refuses the rest)
    const decisions = r.tier === "T3" ? ["deny"] : ["allow", "deny"];
    if (r.tier === "T3" && !notes.has(r.id)) say("allowing a T3 request is the laptop's (levain consent answer there)");
    for (const decision of decisions) {
      const b = el("button", decision, decision + " with passkey");
      b.type = "button";
      b.disabled = !usable;
      b.addEventListener("click", () => start(r, decision, signers, say, actions));
      actions.appendChild(b);
    }
    card.append(actions, note);
    return card;
  }

  function start(r, decision, signers, say, actions) {
    const local = mine();
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    if (known.length || signers.length === 1) return answer(r, decision, known[0] || signers[0], known.length ? local[known[0]] : null, say);
    // more than one passkey is enrolled and this browser made none of them: the person picks (the OS prompt then
    // offers every passkey for this name, so a pick of another device's key is refused at verify)
    say("more than one passkey is enrolled for this name; pick the one this phone holds:");
    for (const old of actions.querySelectorAll(".pick")) old.remove();
    for (const fp of signers) {
      const p = el("button", "quiet pick", fp.slice(0, 18) + "…");
      p.type = "button";
      p.addEventListener("click", () => answer(r, decision, fp, null, say));
      actions.appendChild(p);
    }
  }

  function answer(r, decision, fp, rawId, say) {
    if (busy) return;
    busy++;
    const base = "/consent/" + encodeURIComponent(r.id) + "/";
    say("asking for the answer to sign…");
    call("POST", base + "challenge", { decision: decision, signer_fp: fp }).then((c) => {
      if (!c.ok) return say(why(c), "bad");
      say("confirm with your passkey to " + decision + "…");
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => call("POST", base + "answer", {
        payload: c.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
        client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) }))
        .then((a) => {
          if (!a.ok) return say("not answered: " + why(a), "bad");
          const done = a.body.decision === "allow" ? "allowed" : "denied";
          last = done + " " + (r.tool || "");
          say(done + " · signed by " + a.body.signer_fp, "ok");
        },
              (e) => say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was answered", "bad"));
    }).catch((e) => say("the passkey step failed (" + ((e && e.name) || "error") + "); nothing was answered", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  // enrolment: the created credential is kept, so a failed key-line call retries only that call, never makes another
  let made = null;
  function enrol() {
    const btn = $("enrol-btn"), out = $("enrol-out"), note = $("enrol-note");
    btn.disabled = true;
    busy++;
    const rand = (n) => { const a = new Uint8Array(n); crypto.getRandomValues(a); return a; };
    const create = made ? Promise.resolve(made) : navigator.credentials.create({ publicKey: {
      rp: { id: rpId, name: "levain" }, user: { id: rand(16), name: "levain operator", displayName: "levain operator" },
      challenge: rand(32), pubKeyCredParams: [{ type: "public-key", alg: -7 }], attestation: "none", timeout: 60000,
      authenticatorSelection: { residentKey: "required", userVerification: "required" } } })
      .then((cred) => (made = { rawId: b64u(cred.rawId), spki: b64u(cred.response.getPublicKey()) }));
    create.then((c) => call("POST", "/passkey/key-line", { spki: c.spki }).then((r) => {
      if (!r.ok) { btn.disabled = false; btn.textContent = "try sending the key again"; note.textContent = why(r); return; }
      remember(r.body.fingerprint, c.rawId);
      made = null;
      note.textContent = "passkey made (" + r.body.fingerprint + "). On the laptop, run these to enrol it"
        + " (a passkey cannot enrol itself), then refresh:" + (r.body.note ? " " + r.body.note : "");
      out.replaceChildren(...(r.body.commands || []).map((cmd) => {
        const line = el("div", "cmd"), code = el("code", null, cmd), copy = el("button", "quiet", "copy");
        copy.type = "button";
        copy.addEventListener("click", () => {
          (navigator.clipboard ? navigator.clipboard.writeText(cmd) : Promise.reject())
            .then(() => { copy.textContent = "copied"; }, () => { copy.textContent = "select it"; });
        });
        line.append(code, copy);
        return line;
      }));
      btn.textContent = "make a passkey here";
      btn.disabled = false;
    }), (e) => { btn.disabled = false; note.textContent = "no passkey was made (" + ((e && e.name) || "cancelled") + ")"; })
      .catch((e) => { btn.disabled = false; note.textContent = "the passkey step failed (" + ((e && e.name) || "error") + ")"; })
      .finally(() => { busy--; });
  }

  function load() {
    if (busy) return;
    if (!token) { $("status").textContent = "open this page from the link `levain consent phone` printed (it carries the token)"; return; }
    call("GET", "/consent.json").then((r) => { if (busy) return; if (r.ok) draw(r.body); else $("status").textContent = why(r); });
  }

  $("refresh").addEventListener("click", load);
  $("enrol-btn").addEventListener("click", enrol);
  load();
  setInterval(load, 10000);
})();
