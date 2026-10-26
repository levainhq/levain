// The sign sheet (ONESURFACE OS-1, levain project_memory/1025-32/onesurface/DESIGN.md §3, §4A.3): GET /sign/<id> on the
// cockpit's own origin, top-level or framed by the cockpit (its overlay). It runs the cockpit's existing ceremony on that one
// request and nothing else: GET /cockpit/sign/<id>.json for the request, then a tap is POST /cockpit/sign/<id>/challenge ->
// navigator.credentials.get -> POST /cockpit/sign/<id>/answer. The bytes signed are the server's; the line shown after a
// signature is the challenge's own `line`, never this page's copy.
//
// The fragment: `v=<16 hex>` (the version the link was made on) and `choice=<key>` (the option already tapped elsewhere)
// carry phone.js's rules exactly. The pick is one "Sign (<key>)" tap only while the listed version is the one in the link;
// anything else is a line saying why, and every option still signs on its own tap. Nothing is signed on load: the tap is the
// gesture. The request id is the path's; a `sign=` in the fragment is ignored. The fragment is taken out of the bar first.
//
// Who may sign (§4A.3, as this ceremony has it): the server's row `signers`, the keys its verifier accepts for this request.
// The passkey asked for is one of those, by its credential id when this browser or the cockpit knows it (allowCredentials),
// so the browser never asks for a biometric a refusal would follow. With no key that may sign, no ceremony starts: the sheet
// says what the request needs and where it can be signed.
(function () {
  "use strict";

  const WRITE_TOKEN_KEY = "levain_write_token";     // dashboard_boot.js's key: the cockpit's own token in this browser
  const PAIRED_MARK_KEY = "levain_paired_device";   // pair.js's mark: this browser holds a paired device's key
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const framed = window.parent !== window;           // only this origin may frame the sheet (its frame-ancestors)

  // ---- the link ----
  const pathId = /^\/sign\/([A-Za-z0-9_-]{8,64})$/.exec(location.pathname);
  const id = pathId ? pathId[1] : "";
  const pm = /(?:^#|&)choice=([^&]*)(?:&|$)/.exec(location.hash);
  const vm = /(?:^#|&)v=([0-9a-f]{16})(?:&|$)/.exec(location.hash);
  let pick = "";
  const pickV = vm ? vm[1] : "";
  if (pm) { try { pick = decodeURIComponent(pm[1]); } catch (e) { pick = pm[1]; } }
  if (location.hash) { try { history.replaceState(null, "", location.pathname); } catch (e) { /* the bar keeps it */ } }
  // a second link opened on this page (a fragment-only navigation does not reload it): read it afresh
  window.addEventListener("hashchange", () => { if (location.hash) location.reload(); });

  // ---- the page's parts ----
  function say(text, kind) {
    const n = $("sheet-note");
    n.textContent = text;
    n.className = kind ? "sheet-" + kind : "";
    n.dataset.kind = kind || "";
  }
  function head(title, msg) {
    $("sheet-title").textContent = title;
    $("sheet-msg").textContent = msg;
  }
  // the parent cockpit learns what happened here (framed only); the target origin is this one, never the link's
  function tell(what) {
    if (!framed) return;
    try { window.parent.postMessage({ levainSheet: what, id: id }, location.origin); } catch (e) { /* no parent to tell */ }
  }
  (function back() {
    if (!framed) return;      // top-level: the link to the cockpit is the way back
    const p = $("sheet-back");
    const b = el("button", "dev-btn", "Close");
    b.type = "button";
    b.id = "sheet-close";
    b.addEventListener("click", () => tell("close"));
    p.replaceChildren(b);
  })();

  function storedToken() {
    try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function paired() {
    try { return window.localStorage.getItem(PAIRED_MARK_KEY) === "1"; } catch (e) { return false; }
  }
  // this browser holds no key the cockpit accepts: nothing is read and nothing can be signed here
  function unpaired(refused) {
    head(refused ? "This device's key was not accepted" : "This device is not paired",
         paired() || refused
           ? "Pair it again from a paired device: open the cockpit there, then Devices, Pair a device, and open the link here."
           : "Open the cockpit on this device first (its link carries the key), or pair it from a paired device: Devices, Pair a device.");
    $("sheet").dataset.state = "unpaired";
  }

  const b64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64u = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                        (c) => c.charCodeAt(0));
  const why = (r) => (r.body && typeof r.body.message === "string" && r.body.message) || ("refused (HTTP " + r.status + ")");
  const usable = window.isSecureContext === true && typeof window.PublicKeyCredential === "function"
    && !!(navigator.credentials && navigator.credentials.get);

  function headers(post) {
    const h = { "X-Levain-Write-Token": storedToken() };
    if (post) h["Content-Type"] = "application/json";
    return h;
  }
  // {ok, status, body}; status 0 when the cockpit could not be reached
  function call(route, body) {
    return fetch(route, { method: "POST", cache: "no-store", headers: headers(true), body: JSON.stringify(body) })
      .then((res) => res.json().then((j) => ({ ok: res.ok, status: res.status, body: j && typeof j === "object" ? j : {} }),
                                     () => ({ ok: false, status: res.status, body: {} })),
            () => ({ ok: false, status: 0, body: { message: "the cockpit could not be reached" } }));
  }

  // which passkey THIS browser used, per rpId: the cockpit page's record (dashboard_core.js), fingerprint -> credential id
  const mapKey = (rp) => "levain-passkeys:" + rp;
  function mine(rp) {
    try { const m = JSON.parse(window.localStorage.getItem(mapKey(rp)) || "{}"); return m && typeof m === "object" ? m : {}; }
    catch (e) { return {}; }
  }
  function remember(rp, fp, rawId) {
    try { const m = mine(rp); m[fp] = rawId; window.localStorage.setItem(mapKey(rp), JSON.stringify(m)); } catch (e) { /* kept nowhere */ }
  }

  // ---- read the request, then draw it ----
  if (!id) { head("Not a sign request", "This link names no sign request."); return; }
  if (!storedToken()) { unpaired(false); return; }
  fetch("/cockpit/sign/" + encodeURIComponent(id) + ".json", { cache: "no-store", headers: headers(false) }).then((res) => {
    if (res.status === 403) return { unpaired: true };
    if (res.status === 404) return res.json().then((b) => (b && b.error === "not_open" ? { closed: true } : { error: "This cockpit hosts no signing." }),
                                                   () => ({ error: "This cockpit hosts no signing." }));
    if (!res.ok) return { error: "The sign request could not be read (HTTP " + res.status + ")." };
    return res.json().then((b) => (b && typeof b === "object" && b.request && typeof b.request === "object" ? b : { error: "The sign request could not be read." }),
                           () => ({ error: "The sign request could not be read." }));
  }, () => ({ error: "The cockpit could not be reached." })).then(draw);

  function draw(got) {
    if (got.unpaired) return unpaired(true);
    if (got.closed) { $("sheet").dataset.state = "closed"; return head("Nothing to sign", "This sign request is no longer open: it was signed, denied or it expired."); }
    if (got.error) { $("sheet").dataset.state = "error"; return head("Sign request", got.error); }
    const r = got.request;
    const origin = typeof got.origin === "string" ? got.origin : "";
    if (!usable || origin !== window.location.origin) {
      $("sheet").dataset.state = "elsewhere";
      head(r.title || "Sign request", "This page cannot use your passkey: sign on " + (origin || "the cockpit's https page") + ".");
      if (origin) {
        const a = el("a", "", origin + "/sign/" + id);
        a.href = origin + "/sign/" + encodeURIComponent(id);
        $("sheet-body").appendChild(el("p", "")).appendChild(a);
      }
      return;
    }
    head(r.title || "Sign request", r.requester ? "From " + r.requester : "");
    const body = $("sheet-body");
    if (r.refused) { $("sheet").dataset.state = "refused"; return body.appendChild(el("p", "", "Not signable: " + r.refused)); }
    body.appendChild(el("pre", "sheet-message", typeof r.message === "string" ? r.message : ""));
    if (r.expires_at) body.appendChild(el("p", "ext-meta", "Open until " + r.expires_at));

    const rp = window.location.hostname;
    const signers = Array.isArray(r.signers) ? r.signers.filter((fp) => typeof fp === "string") : [];
    const held = got.credentials && typeof got.credentials === "object" ? got.credentials : {};
    if (!signers.length) return escalate(rp, got.note);

    // the keys that may sign this, with the provider the cockpit names for each (display only)
    const who = el("p", "ext-meta", "May sign: ");
    who.id = "sheet-signers";
    who.textContent += signers.map((fp) => fp.slice(0, 18) + "…"
      + (held[fp] && typeof held[fp].provider === "string" && held[fp].provider ? " (" + held[fp].provider + ")" : "")).join(", ");
    body.appendChild(who);
    $("sheet").dataset.state = "open";
    offer(r, rp, signers, held);
  }

  // §4A.3 rule 4: no key that may sign this request, so no ceremony is started; what it needs, and where it is signed
  function escalate(rp, note) {
    $("sheet").dataset.state = "escalate";
    const card = el("div", "sheet-escalate");
    card.id = "sheet-escalate";
    card.appendChild(el("p", "", "No passkey can sign this here: it needs an active passkey enrolled for " + rp + "."));
    if (typeof note === "string" && note) card.appendChild(el("p", "ext-meta", note));
    card.appendChild(el("p", "", "Enrol one at the laptop (levain cockpit enroll, signed by the laptop key), then open this link again."));
    $("sheet-body").appendChild(card);
  }

  let busy = false;
  function offer(r, rp, signers, held) {
    const acts = $("sheet-actions");
    acts.hidden = false;
    const buttons = [];
    const lock = (on) => { for (const b of buttons) b.disabled = on; };
    const run = (choice, words) => {
      if (busy) return;
      busy = true;
      lock(true);
      whom(rp, signers, held, acts).then((k) => (k ? ceremony(r, k.fp, k.rawId, choice, words) : "unsigned"))
        .catch(() => "unsigned")
        .then((done) => {
          busy = false;
          if (done === "signed") { acts.replaceChildren(); acts.hidden = true; tell("signed"); return; }
          lock(false);
        });
    };
    const button = (text, cls, onClick) => {
      const b = el("button", "dev-btn" + (cls ? " " + cls : ""), text);
      b.type = "button";
      b.addEventListener("click", onClick);
      buttons.push(b);
      return b;
    };
    const options = Array.isArray(r.options) ? r.options : [];
    // his pick from the link: one tap that signs that option, only on the version he tapped it on (phone.js's rule)
    const sameV = !!pickV && typeof r.sha256 === "string" && r.sha256.slice(0, 16) === pickV;
    const po = pick && sameV && /^[a-z0-9]{1,8}$/.test(pick) ? options.find((o) => o && o.key === pick) : null;
    if (po) {
      const top = el("p", "sheet-pick", "Your pick: (" + po.key + "). Read the item; Sign (" + po.key + ") is below it.");
      top.id = "sheet-pick";
      $("sheet-body").insertBefore(top, $("sheet-body").firstChild);
      const pb = button("Sign (" + po.key + ")", "dev-primary", () => run(String(po.key), undefined));
      pb.id = "sheet-picked";
      acts.appendChild(pb);
    } else if (pick) {
      const miss = el("p", "sheet-pick-miss", !pickV ? "The link does not say which version of this item you tapped: read it and pick below."
        : !sameV ? "This item changed since your tap: read it and pick again."
        : "The option in the link (" + pick.slice(0, 24) + ") is not on this item: pick one below.");
      miss.id = "sheet-pick-miss";
      $("sheet-body").insertBefore(miss, $("sheet-body").firstChild);
    }
    if (!options.length) acts.appendChild(button("Sign with your passkey", po ? "" : "dev-primary", () => run(undefined, undefined)));
    for (const o of options) {
      if (!o || typeof o.key !== "string") continue;
      const b = button("(" + o.key + ") " + String(o.text), o.key === r.recommended ? "sheet-rec" : "", () => run(o.key, undefined));
      b.title = "signs: " + String(o.line);
      b.dataset.choice = o.key;
      acts.appendChild(b);
    }
    const wi = options.length && r.write_in && typeof r.write_in === "object" ? r.write_in : null;
    if (wi) {
      const box = el("input", "sheet-write-in");
      box.type = "text";
      box.id = "sheet-write-in";
      box.maxLength = Number(wi.max) || 200;
      box.placeholder = "or write your own answer";
      acts.append(box, button("(" + String(wi.key) + ") Sign my words", "", () => {
        const w = box.value.trim();
        if (!w) return say("Type your answer first.", "bad");
        run(String(wi.key), w);
      }));
    }
    if (options.length) {
      const lines = el("div", "ext-meta", "Each option signs the message with its line appended:");
      for (const o of options) if (o && typeof o.line === "string") lines.appendChild(el("div", "sheet-line", o.line));
      $("sheet-body").appendChild(lines);
    }
  }

  // which eligible key signs: the one this browser used before, else the only one, else his pick from a list.
  // Resolves {fp, rawId} or null (dismissed); rawId is this browser's record, else the cockpit's, else null (the OS's list)
  function whom(rp, signers, held, acts) {
    const local = mine(rp);
    const idOf = (fp) => (Object.prototype.hasOwnProperty.call(local, fp) ? local[fp]
      : held[fp] && typeof held[fp].credential_id === "string" ? held[fp].credential_id : null);
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    if (known.length === 1) return Promise.resolve({ fp: known[0], rawId: local[known[0]] });
    if (!known.length && signers.length === 1) return Promise.resolve({ fp: signers[0], rawId: idOf(signers[0]) });
    say("More than one passkey may sign this; pick the one this device holds:");
    return new Promise((resolve) => {
      const picks = [];
      const end = (v) => { picks.forEach((x) => x.remove()); resolve(v); };
      for (const fp of known.length ? known : signers) {
        const p = el("button", "dev-btn sheet-key", fp.slice(0, 18) + "…");
        p.type = "button";
        p.title = fp;
        p.addEventListener("click", () => end({ fp: fp, rawId: idOf(fp) }));
        picks.push(p);
      }
      const no = el("button", "dev-btn sheet-key", "Dismiss");
      no.type = "button";
      no.addEventListener("click", () => { say("No passkey was picked; nothing was signed.", "bad"); end(null); });
      picks.push(no);
      acts.append(...picks);
    });
  }

  // one ceremony: "signed", "unsigned" (definitely nothing signed) or "unsure" (the answer may have landed)
  function ceremony(r, fp, rawId, choice, words) {
    const base = "/cockpit/sign/" + encodeURIComponent(r.id) + "/";
    const body = { signer_fp: fp, request_sha256: r.sha256 };
    if (choice !== undefined) body.choice = choice;
    if (words !== undefined) body.write_in = words;
    let sent = false, line = "";
    say(choice !== undefined ? "Asking for the challenge for (" + choice + ")…" : "Asking for the challenge…");
    return call(base + "challenge", body).then((c) => {
      if (!c.ok) { say(why(c), "bad"); return "unsigned"; }
      if (typeof c.body.line === "string") line = c.body.line;
      say("Confirm with your passkey to sign…" + (line ? " " + line : ""));
      const pk = { challenge: unb64u(String(c.body.challenge || "")), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => {
        say("Verifying the signature…");
        sent = true;
        const answer = { authenticator_data: b64u(cred.response.authenticatorData),
          client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
        if (cred.rawId) answer.raw_id = b64u(cred.rawId);
        return call(base + "answer", answer).then((a) => {
          if (!a.ok && a.status >= 400 && a.status < 500) { say("Not signed: " + why(a), "bad"); return "unsigned"; }
          if (!a.ok) { say("Not sure this was signed (" + why(a) + "): check the cockpit.", "bad"); return "unsure"; }
          if (!rawId && cred.rawId && typeof c.body.rp_id === "string") remember(c.body.rp_id, fp, b64u(cred.rawId));
          say("signed" + (a.body.choice ? " (" + a.body.choice + ")" : "") + " · by " + a.body.signer_fp + " in " + a.body.namespace
              + (line ? ": " + line : ""), "ok");
          $("sheet").dataset.state = "signed";
          return "signed";
        });
      }, (e) => { say("The passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was signed.", "bad"); return "unsigned"; });
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) { say("Not sure this was signed (the page failed: " + name + "): check the cockpit.", "bad"); return "unsure"; }
      say("The page failed (" + name + "); nothing was signed.", "bad");
      return "unsigned";
    });
  }
})();
