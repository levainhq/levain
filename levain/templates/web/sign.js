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
//
// Every read and post has a deadline over its headers and its body. A ceremony ends "signed", "unsigned" (nothing can have
// been signed: a refusal, or anything before the answer was posted) or "unsure" (the answer was posted and its reply did not
// come back). Only "unsigned" re-arms the controls; "unsure" keeps them frozen and reads the request again, and they come
// back only when that read finds the same version still open. A write-in is frozen as typed (trimmed) from its click on.
//
// A consent request (OS-2) is the same sheet at /sign/consent/<id>: its read is GET /cockpit/consent/<id>/sheet.json (the
// same row shape, its options allow and deny), and its ceremony the phone port's own: POST .../challenge and .../answer for
// a pending request, .../rule-challenge and .../rule for an undecided one (the row's `mode`), with the phone's exact bodies
// ({decision, signer_fp}, then the challenge's `payload` beside the assertion).
//
// A dispatch job (OS-2) is the same sheet at /sign/dispatch/<job>: its read is GET /cockpit/dispatch/<job>/sheet.json (the
// same row shape; its options sign, cancel and authorise, each with the `signers` that may do THAT act), and its ceremony
// the phone port's own by option: sign is .../challenge then .../answer ({signer_fp}), cancel is .../cancel-challenge then
// .../cancel ({signer_fp, reason}, the reason typed in the box the row asks for), authorise is .../authorise-challenge then
// .../authorise ({signer_fp}); each answer is the challenge's `payload` beside the assertion, nothing else.
//
// A coordinator drop (OS-2) is the same sheet at /sign/drop: its read is GET /cockpit/coord/drops.json (the phone's coord_*
// listing keys), and it composes the drop's words and, optionally, an envelope. A plain drop is POST /cockpit/coord/drop. An
// envelope is POST /cockpit/coord/envelope-review (the server keeps the body it shows), then POST .../envelope-challenge (the
// payload, signed_at now, and the WebAuthn challenge), navigator.credentials.get, and POST .../drop carrying the envelope
// {body_text, payload, authenticator_data, client_data_json, signature}: the phone port's /coord/<action> by name, bodies the
// phone's own. The bytes signed are the server's; the line shown is the challenge's own `line`.
(function () {
  "use strict";

  const WRITE_TOKEN_KEY = "levain_write_token";     // dashboard_boot.js's key: the cockpit's own token in this browser
  const PAIRED_MARK_KEY = "levain_paired_device";   // pair.js's mark: this browser holds a paired device's key
  const FETCH_TIMEOUT_MS = 15000;                    // one read or post, its headers and its body
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const framed = window.parent !== window;           // only this origin may frame the sheet (its frame-ancestors)

  // ---- the link ----
  const consentPath = /^\/sign\/consent\/([^/]{1,480})$/.exec(location.pathname);
  const dispatchPath = /^\/sign\/dispatch\/(dj-[0-9a-f]{32})$/.exec(location.pathname);     // the phone's job id shape
  const coord = location.pathname === "/sign/drop";
  const pathId = consentPath || dispatchPath || coord ? null : /^\/sign\/([A-Za-z0-9_-]{8,64})$/.exec(location.pathname);
  let id = pathId ? pathId[1] : dispatchPath ? dispatchPath[1] : coord ? "drop" : "";
  if (consentPath) {
    try { id = decodeURIComponent(consentPath[1]); } catch (e) { id = ""; }
    if (!/^[A-Za-z0-9._:-]{1,160}$/.test(id)) id = "";       // the server's consent id shape
  }
  const consent = !!consentPath;
  const dispatch = !!dispatchPath;
  const noun = consent ? "consent request" : dispatch ? "dispatch job" : coord ? "drop" : "sign request";
  const Noun = consent ? "Consent request" : dispatch ? "Dispatch job" : coord ? "Drop" : "Sign request";
  const routeOf = (rid) => (consent ? "/cockpit/consent/" : dispatch ? "/cockpit/dispatch/" : "/cockpit/sign/") + encodeURIComponent(rid);
  // the dispatch ceremony's two routes by the act picked (the phone's own names)
  const DISPATCH_STEPS = { sign: ["challenge", "answer"], cancel: ["cancel-challenge", "cancel"],
                           authorise: ["authorise-challenge", "authorise"] };
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
  // the cockpit asks to close the overlay (its x, its Escape): this page answers "ack" at once, so the parent knows a
  // sheet is listening, then applies its own close rule (askClose below), which tells "close" only when it may leave
  if (framed) {
    window.addEventListener("message", (ev) => {
      if (ev.source !== window.parent || ev.origin !== location.origin) return;
      if (ev.data && typeof ev.data === "object" && ev.data.levainSheetCmd === "close") { tell("ack"); askClose(); }
    });
  }
  (function back() {
    if (!framed) return;      // top-level: the link to the cockpit is the way back
    const p = $("sheet-back");
    const b = el("button", "dev-btn", "Close");
    b.type = "button";
    b.id = "sheet-close";
    b.addEventListener("click", () => askClose());
    p.replaceChildren(b);
    // a key pressed in this frame is this document's event, never the cockpit's: Escape closes the overlay from here,
    // except the Escape that ends an IME composition (keyCode 229 on browsers that report it only there)
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !ev.isComposing && ev.keyCode !== 229) askClose(); });
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
  // unpadded base64url with a length some bytes encode to; unb64u is only ever handed one
  const decodable = (t) => typeof t === "string" && /^[A-Za-z0-9_-]+$/.test(t) && t.length % 4 !== 1;
  const unb64u = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                        (c) => c.charCodeAt(0));
  // the server's words without their closing stop: the caller's sentence supplies its own punctuation
  const why = (r) => ((r.body && typeof r.body.message === "string" && r.body.message.replace(/[.\s]+$/, "")) || ("refused (HTTP " + r.status + ")"));
  const usable = window.isSecureContext === true && typeof window.PublicKeyCredential === "function"
    && !!(navigator.credentials && navigator.credentials.get);

  function headers(post) {
    const h = { "X-Levain-Write-Token": storedToken() };
    if (post) h["Content-Type"] = "application/json";
    return h;
  }
  // one read or post: {ok, status, body}, status 0 when no whole reply came (unreachable, or past the deadline). The abort
  // stays armed until the body is read, so a reply that stalls before its headers or mid-body is cancelled; the race ends
  // the wait a second after it for a browser without AbortController, or a body that does not honour the signal.
  function within(route, opts) {
    const ac = typeof AbortController === "function" ? new AbortController() : null;
    let late = false;
    const timer = setTimeout(() => { late = true; if (ac) ac.abort(); }, FETCH_TIMEOUT_MS);
    const none = () => ({ ok: false, status: 0,
                          body: { message: late ? "the cockpit did not answer in time" : "the cockpit could not be reached" } });
    const got = fetch(route, Object.assign({ cache: "no-store" }, opts, ac ? { signal: ac.signal } : {}))
      .then((res) => res.json().then((j) => ({ ok: res.ok, status: res.status, body: j && typeof j === "object" ? j : {} }),
                                     () => (late ? none() : { ok: false, status: res.status, body: {} })),
            none);
    let stop;
    const deadline = new Promise((r) => { stop = setTimeout(() => { late = true; r(none()); }, FETCH_TIMEOUT_MS + 1000); });
    return Promise.race([got, deadline]).finally(() => { clearTimeout(timer); clearTimeout(stop); });
  }
  const call = (route, body) => within(route, { method: "POST", headers: headers(true), body: JSON.stringify(body) });
  // the request as the cockpit holds it now: {request, rp_id, ...}, {closed}, {unpaired} or {error}
  function readRequest() {
    return within(routeOf(id) + (consent || dispatch ? "/sheet.json" : ".json"), { headers: headers(false) }).then((g) => {
      if (g.status === 403) return { unpaired: true };
      if (g.status === 404) return g.body.error === "not_open" ? { closed: true } : { error: "This cockpit hosts no signing." };
      if (g.status === 0) return { error: "The " + noun + " could not be read: " + g.body.message + "." };
      if (!g.ok) return { error: "The " + noun + " could not be read (HTTP " + g.status + ")." };
      return g.body.request && typeof g.body.request === "object" ? g.body : { error: "The " + noun + " could not be read." };
    });
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
  function forget(rp, fp) {
    try { const m = mine(rp); delete m[fp]; window.localStorage.setItem(mapKey(rp), JSON.stringify(m)); } catch (e) { /* kept nowhere */ }
  }

  // ---- read the request, then draw it ----
  if (!id) { head("Not a " + noun, "This link names no " + noun + "."); return; }
  if (consent || dispatch || coord) { document.title = "Levain · " + (consent ? "consent" : dispatch ? "dispatch" : "drop"); $("sheet-title").textContent = Noun; $("sheet-msg").textContent = "Reading the " + noun + "…"; }
  if (!storedToken()) { unpaired(false); return; }
  if (coord) readDrops().then(drawDrop);
  else readRequest().then(draw);

  function draw(got) {
    if (got.unpaired) return unpaired(true);
    if (got.closed) {
      $("sheet").dataset.state = "closed";
      return head("Nothing to sign", consent ? "This consent request is no longer open: it was answered, ruled or it expired."
        : dispatch ? "This dispatch job is no longer open: it was cancelled, finished or it is not in this store."
        : "This sign request is no longer open: it was signed, denied or it expired.");
    }
    if (got.error) { $("sheet").dataset.state = "error"; return head(Noun, got.error); }
    const r = got.request;
    const origin = typeof got.origin === "string" ? got.origin : "";
    if (!usable || origin !== window.location.origin) {
      $("sheet").dataset.state = "elsewhere";
      head(r.title || Noun, "This page cannot use your passkey: sign on " + (origin || "the cockpit's https page") + ".");
      if (origin) {
        const path = (consent ? "/sign/consent/" : dispatch ? "/sign/dispatch/" : "/sign/") + encodeURIComponent(id);
        const a = el("a", "", origin + path);
        a.href = origin + path;
        $("sheet-body").appendChild(el("p", "")).appendChild(a);
      }
      return;
    }
    head(r.title || Noun, r.requester ? "From " + r.requester : "");
    const body = $("sheet-body");
    if (r.refused) { $("sheet").dataset.state = "refused"; return body.appendChild(el("p", "", "Not signable: " + r.refused)); }
    const msg = body.appendChild(el("pre", "sheet-message", typeof r.message === "string" ? r.message : ""));
    // a dispatch job is shown whole (every path, every change): its box scrolls, it is never cut to fit
    if (dispatch) { msg.id = "sheet-contract"; msg.tabIndex = 0; Object.assign(msg.style, { maxHeight: "50vh", overflow: "auto" }); }
    if (r.expires_at) body.appendChild(el("p", "ext-meta", "Open until " + r.expires_at));
    // what the server says about this request beside its options (a consent allow only the laptop key signs)
    if (typeof r.note === "string" && r.note && signersOf(r).length) {
      const n = el("p", "ext-meta", r.note);
      n.id = "sheet-request-note";
      body.appendChild(n);
    }

    // the rpId the passkey record is kept under, written and read: the read's own (the Signer's), which a page on another
    // name of the same site does not share with its hostname; a cockpit that names none is read as this page's name
    const rp = typeof got.rp_id === "string" && got.rp_id ? got.rp_id : window.location.hostname;
    const signers = signersOf(r);
    const held = got.credentials && typeof got.credentials === "object" ? got.credentials : {};
    // a dispatch job this page cannot show whole (or read) offers nothing: it is reviewed on the laptop
    if (dispatch && r.review) return review(r.note);
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

  function signersOf(r) { return Array.isArray(r.signers) ? r.signers.filter((fp) => typeof fp === "string") : []; }

  // a dispatch job the cockpit will not have signed from this page (its change set is longer than the page lists, or its
  // record could not be read): no act and no ceremony, and where it is reviewed, in the server's words
  function review(note) {
    $("sheet").dataset.state = "review";
    const card = el("div", "sheet-escalate");
    card.id = "sheet-review";
    card.appendChild(el("p", "", "This one needs review on the laptop: nothing is signed from this page."));
    if (typeof note === "string" && note) card.appendChild(el("p", "ext-meta", note));
    $("sheet-body").appendChild(card);
  }
  // the job read again and drawn afresh (an authorise whose challenge was not the card shown): its version, its card
  function redraw() {
    return readRequest().then((got) => {
      $("sheet-body").replaceChildren();
      $("sheet-actions").replaceChildren();
      draw(got);
    });
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

  // what is being signed, beside the controls: the page's expectation at the tap ("page"), then the line the challenge
  // carries ("server"), which after a signature is the line signed
  // `label`: "Signing" while the ceremony runs; "Signed", "Not signed" or "Not confirmed" once it has ended
  function signing(text, source, label) {
    let n = $("sheet-signing");
    if (!n) {
      n = el("p", "sheet-signing");
      n.id = "sheet-signing";
      $("sheet-actions").before(n);
    }
    n.dataset.text = text;
    n.textContent = label + ": " + text;
    n.dataset.source = source;
  }
  // the line already shown keeps its text and takes the ending's label, so it never reads as in progress afterwards
  function relabel(label) {
    const n = $("sheet-signing");
    if (n && typeof n.dataset.text === "string") n.textContent = label + ": " + n.dataset.text;
  }

  // `busy`: the controls are locked (a ceremony runs, or an unsure one is not yet settled). `flight`: a ceremony is in
  // flight, which ends on every outcome; `active` is the request it signs
  let busy = false, flight = false, active = null;
  function ended() { flight = false; closeAsked = false; }
  function offer(r, rp, signers, held) {
    const acts = $("sheet-actions");
    acts.hidden = false;
    const controls = [];          // every button and the write-in: frozen together for the whole ceremony
    const lock = (on) => { for (const b of controls) b.disabled = on; };
    // a dispatch option names the keys that may do THAT act (the sign tier's and the landing tier's differ): only those are
    // offered for it; any other row signs with the row's keys
    const keysFor = (choice) => {
      const o = dispatch ? (Array.isArray(r.options) ? r.options : []).find((x) => x && x.key === choice) : null;
      return o && Array.isArray(o.signers) ? o.signers.filter((fp) => typeof fp === "string") : signers;
    };
    const run = (choice, words, expect) => {
      if (busy) return;
      busy = true;
      active = r;
      lock(true);
      if (expect) signing(expect, "page", "Signing");
      whom(rp, keysFor(choice), held, acts).then((k) => (k ? ceremony(r, rp, k, choice, words) : "unsigned"))
        .catch(() => "unsigned")
        .then((done) => {
          if (done === "signed") { busy = false; ended(); acts.replaceChildren(); acts.hidden = true; tell("signed"); return; }
          if (done === "unsure") {
            relabel("Not confirmed");
            return settle(r, () => { relabel("Not signed"); busy = false; lock(false); }).then(ended);
          }
          relabel("Not signed");
          ended();
          busy = false;
          // the authorise challenge was not the card this sheet showed: nothing was signed; the job is read again and
          // drawn afresh (its new version and card), and the cockpit reads its board again
          if (done === "changed") { tell("reload"); return redraw(); }
          lock(false);
        });
    };
    const button = (text, cls, onClick) => {
      const b = el("button", "dev-btn" + (cls ? " " + cls : ""), text);
      b.type = "button";
      b.addEventListener("click", onClick);
      controls.push(b);
      return b;
    };
    const lineOf = (o) => (o && typeof o.line === "string" ? o.line : "");
    const options = Array.isArray(r.options) ? r.options : [];
    // a dispatch cancel carries a reason (one line, optional) typed in the box the row asks for; it is frozen as typed
    // (trimmed) from the click on, like a write-in, and is the `words` of the run
    let reasonBox = null;
    if (dispatch && r.reason && typeof r.reason === "object" && options.some((o) => o && o.key === "cancel")) {
      reasonBox = el("input", "sheet-write-in");
      reasonBox.type = "text";
      reasonBox.id = "sheet-reason";
      reasonBox.maxLength = Number(r.reason.max) || 256;
      reasonBox.placeholder = "reason for cancelling (optional)";
      controls.push(reasonBox);
    }
    // what an option's tap runs: a cancel takes the reason box's words as typed now
    const tap = (o) => () => {
      if (busy) return;
      if (o.key === "cancel" && reasonBox) {
        const w = reasonBox.value.trim();
        reasonBox.value = w;
        return run("cancel", w, lineOf(o) + (w ? ": " + w : ""));
      }
      run(String(o.key), undefined, lineOf(o));
    };
    // his pick from the link: one tap that signs that option, only on the version he tapped it on (phone.js's rule)
    const sameV = !!pickV && typeof r.sha256 === "string" && r.sha256.slice(0, 16) === pickV;
    const po = pick && sameV && /^[a-z0-9]{1,8}$/.test(pick) ? options.find((o) => o && o.key === pick) : null;
    if (po) {
      const top = el("p", "sheet-pick", "Your pick: (" + po.key + "). Read the item; Sign (" + po.key + ") is below it.");
      top.id = "sheet-pick";
      $("sheet-body").insertBefore(top, $("sheet-body").firstChild);
      const pb = button("Sign (" + po.key + ")", "dev-primary", tap(po));
      pb.id = "sheet-picked";
      acts.appendChild(pb);
    } else if (pick) {
      const miss = el("p", "sheet-pick-miss", !pickV ? "The link does not say which version of this item you tapped: read it and pick below."
        : !sameV ? "This item changed since your tap: read it and pick again."
        : "The option in the link (" + pick.slice(0, 24) + ") is not on this item: pick one below.");
      miss.id = "sheet-pick-miss";
      $("sheet-body").insertBefore(miss, $("sheet-body").firstChild);
    }
    // a dispatch job is only ever signed through an act its row offers: no options, no button (never a bare "Sign")
    if (!options.length && !dispatch) acts.appendChild(button("Sign with your passkey", po ? "" : "dev-primary", () => run(undefined, undefined)));
    for (const o of options) {
      if (!o || typeof o.key !== "string") continue;
      const b = button("(" + o.key + ") " + String(o.text), o.key === r.recommended ? "sheet-rec" : "", tap(o));
      b.title = "signs: " + String(o.line);
      b.dataset.choice = o.key;
      acts.appendChild(b);
    }
    if (reasonBox) acts.appendChild(reasonBox);
    const wi = options.length && r.write_in && typeof r.write_in === "object" ? r.write_in : null;
    if (wi) {
      const box = el("input", "sheet-write-in");
      box.type = "text";
      box.id = "sheet-write-in";
      box.maxLength = Number(wi.max) || 200;
      box.placeholder = "or write your own answer";
      controls.push(box);
      acts.append(box, button("(" + String(wi.key) + ") Sign my words", "", () => {
        if (busy) return;
        // the words signed are the box's at this click, trimmed, and the box shows exactly them until the ceremony ends
        const w = box.value.trim();
        if (!w) return say("Type your answer first.", "bad");
        box.value = w;
        run(String(wi.key), w, (typeof wi.line === "string" ? wi.line : "(" + String(wi.key) + "): ") + w);
      }));
    }
    if (options.length) {
      const lines = el("div", "ext-meta", dispatch ? "Each act signs the payload the cockpit builds for it (shown above the tap):"
        : "Each option signs the message with its line appended:");
      for (const o of options) if (o && typeof o.line === "string") lines.appendChild(el("div", "sheet-line", o.line));
      $("sheet-body").appendChild(lines);
    }
  }

  // "unsure": the answer was posted and no reply said what landed. One read of the request finding it open does not show
  // nothing was signed (the answer may still be verifying), so the controls stay frozen; only a second read, after a pause,
  // finding the same version still open gives them back (`release`). Anything else leaves them frozen, says so, and tells
  // the cockpit to read again
  const SETTLE_PAUSE_MS = 1500;
  function settle(r, release) {
    say($("sheet-note").textContent + " Checking where it stands…", "bad");
    const closed = (got) => { $("sheet").dataset.state = "closed"; say(standing(got), "bad"); tell("reload"); };
    return readRequest().then((first) => {
      if (!stillOpen(r, first)) return closed(first);
      say("The request reads open now; not confirmed (the answer may still be landing). Checking once more…", "bad");
      return new Promise((res) => setTimeout(res, SETTLE_PAUSE_MS)).then(readRequest).then((second) => {
        if (!stillOpen(r, second)) return closed(second);
        say("Not signed: the cockpit says this request is still open, unsigned, on the version shown, on a second read; you can sign it again.", "bad");
        return release();
      });
    });
  }
  const stillOpen = (r, got) => !!(got.request && got.request.sha256 === r.sha256 && !got.request.refused);
  // what a read that did not find the request open and unchanged says
  function standing(got) {
    return got.unpaired ? "This device's key was not accepted when the request was read again: pair it again, then reload before signing."
      : got.closed ? "This request is no longer open (it may have been signed from here): the cockpit shows what landed."
      : got.request ? "This request changed while it was being signed: reload it and read it again."
      : "Where this request stands could not be read (" + String(got.error || "no reply") + "): reload before signing again.";
  }
  // Close / Escape. Idle: the cockpit closes the overlay and reads its board again. While a ceremony is in flight the
  // outcome is not known, so the line reads "Not confirmed", the request is read again and the sheet stays; a second
  // close leaves
  let closeAsked = false;
  function askClose() {
    if (coord && flight && !closeAsked) {
      // a drop has no request to read again: the line says the outcome is not confirmed, and a second close leaves
      closeAsked = true;
      relabel("Not confirmed");
      say("Signing is still in progress, so it is not confirmed (close again to leave).", "bad");
      return tell("reload");
    }
    if (!flight || closeAsked) return tell("close");
    closeAsked = true;
    relabel("Not confirmed");
    say("Signing is still in progress, so it is not confirmed. Checking where the request stands… (close again to leave).", "bad");
    return readRequest().then((got) => {
      // a ceremony that ended while this read was out has said its own outcome: this read does not replace it
      if (flight && $("sheet").dataset.state !== "signed") {
        say(stillOpen(active, got)
            ? "The request is still open and unsigned as of this check; the signature in progress may yet land (close again to leave)."
            : standing(got), "bad");
      }
      tell("reload");
    });
  }

  // which eligible key signs: the one this browser used before, else the only one, else his pick from a list.
  // Resolves {fp, rawId, local} or null (dismissed); rawId is this browser's record (local: true), else the cockpit's,
  // else null (the OS's list)
  function whom(rp, signers, held, acts) {
    const local = mine(rp);
    const has = (fp) => Object.prototype.hasOwnProperty.call(local, fp);
    const idOf = (fp) => (has(fp) ? { fp: fp, rawId: local[fp], local: true }
      : { fp: fp, rawId: held[fp] && typeof held[fp].credential_id === "string" ? held[fp].credential_id : null, local: false });
    const known = signers.filter(has);
    if (known.length === 1) return Promise.resolve(idOf(known[0]));
    if (!known.length && signers.length === 1) return Promise.resolve(idOf(signers[0]));
    say("More than one passkey may sign this; pick the one this device holds:");
    return new Promise((resolve) => {
      const picks = [];
      const end = (v) => { picks.forEach((x) => x.remove()); resolve(v); };
      for (const fp of known.length ? known : signers) {
        const p = el("button", "dev-btn sheet-key", fp.slice(0, 18) + "…");
        p.type = "button";
        p.title = fp;
        p.addEventListener("click", () => end(idOf(fp)));
        picks.push(p);
      }
      const no = el("button", "dev-btn sheet-key", "Dismiss");
      no.type = "button";
      no.addEventListener("click", () => { say("No passkey was picked; nothing was signed.", "bad"); end(null); });
      picks.push(no);
      acts.append(...picks);
    });
  }

  // one ceremony with key `k` ({fp, rawId, local}): "signed", "unsigned" (definitely nothing signed) or "unsure" (the
  // answer was posted and may have landed). `rp` keys this browser's passkey record (draw's).
  function ceremony(r, rp, k, choice, words) {
    const base = routeOf(r.id) + "/";
    const fp = k.fp;
    // a consent request takes the phone port's own bodies, exactly their keys: {decision, signer_fp}, then the payload the
    // challenge returned beside the assertion; an undecided one is ruled (its own pair of routes), never answered
    // a dispatch job takes the phone port's own: the pair of routes of the act picked (sign, cancel, authorise), {signer_fp}
    // (a cancel's also {reason}), then the payload beside the assertion
    const steps = dispatch ? (DISPATCH_STEPS[choice] || DISPATCH_STEPS.sign)
      : !consent ? ["challenge", "answer"] : r.mode === "rule" ? ["rule-challenge", "rule"] : ["challenge", "answer"];
    const body = dispatch ? { signer_fp: fp } : consent ? { decision: choice, signer_fp: fp } : { signer_fp: fp, request_sha256: r.sha256 };
    if (dispatch && choice === "cancel") body.reason = typeof words === "string" ? words : "";
    if (!consent && !dispatch && choice !== undefined) body.choice = choice;
    if (!consent && !dispatch && words !== undefined) body.write_in = words;
    let sent = false, line = "";
    const unsigned = (m) => { say(/nothing was signed/.test(m) ? m : m + "; nothing was signed.", "bad"); return "unsigned"; };
    say(choice !== undefined ? "Asking for the challenge for (" + choice + ")…" : "Asking for the challenge…");
    flight = true;        // from the request for the challenge: the signer picker before it posts nothing
    return call(base + steps[0], body).then((c) => {
      if (!c.ok) return unsigned("Not signed: " + why(c));
      if (!decodable(c.body.challenge)) return unsigned("The cockpit sent a malformed challenge");
      // an authorise lands the run and branch the SERVER builds now: before any passkey is asked, the challenge's payload
      // must be the run, branch and tier this sheet showed; a recommendation that moved since is never signed from here
      if (dispatch && choice === "authorise") {
        const card = r.authorise && typeof r.authorise === "object" ? r.authorise : null;
        const p = c.body.payload && typeof c.body.payload === "object" ? c.body.payload : {};
        if (!card || card.run === null || card.run === undefined || String(p.run) !== String(card.run)
            || p.onto !== card.onto || c.body.tier !== card.tier) {
          say("Not signed: the cockpit's authorise is for run " + String(p.run) + " onto " + String(p.onto)
              + " (tier " + String(c.body.tier) + "), not run " + (card ? String(card.run) + " onto " + String(card.onto)
              + " (tier " + String(card.tier) + ")" : "?") + " as this sheet showed; nothing was signed. Read it again below.", "bad");
          return "changed";
        }
      }
      if (typeof c.body.line === "string") { line = c.body.line; signing(line, "server", "Signing"); }
      say("Confirm with your passkey to sign…" + (line ? " " + line : ""));
      // the passkey asked for by its id when one decodes; an id that does not is dropped (the OS's own list), never a throw
      const allow = k.rawId && decodable(k.rawId) ? unb64u(k.rawId) : null;
      if (k.rawId && !allow && k.local) forget(rp, fp);
      const ask = (id) => {
        const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
        if (id) pk.allowCredentials = [{ type: "public-key", id: id }];
        return navigator.credentials.get({ publicKey: pk });
      };
      // a NotAllowedError for the id this browser remembered may be a passkey since removed, or only a cancel: ask once
      // more with no id. The record is kept until a passkey signs with a different credential (remembered below)
      const asked = ask(allow).catch((e) => {
        if (!(e && e.name === "NotAllowedError" && allow && k.local)) throw e;
        return ask(null);
      });
      return asked.then((cred) => {
        say("Verifying the signature…");
        const answer = { authenticator_data: b64u(cred.response.authenticatorData),
          client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
        if (consent || dispatch) answer.payload = c.body.payload;
        else if (cred.rawId) answer.raw_id = b64u(cred.rawId);
        sent = true;          // from here on a failure may follow an answer that landed
        return call(base + steps[1], answer).then((a) => {
          if (!a.ok && a.status >= 400 && a.status < 500) return unsigned("Not signed: " + why(a));
          if (!a.ok) { say("Not sure this was signed (" + why(a) + ").", "bad"); return "unsure"; }
          if (cred.rawId && mine(rp)[fp] !== b64u(cred.rawId)) remember(rp, fp, b64u(cred.rawId));
          const by = typeof a.body.signer_fp === "string" && a.body.signer_fp ? a.body.signer_fp : fp;
          const ns = typeof a.body.namespace === "string" && a.body.namespace ? " in " + a.body.namespace : "";
          // a consent answer's reply names its decision; a ruling's also says where its delivery stands
          // a dispatch reply names the job's state now (signed, cancelled, or where the land went)
          const what = a.body.choice || (consent && typeof a.body.decision === "string" ? a.body.decision
            : dispatch && typeof a.body.state === "string" ? a.body.state + (typeof a.body.outcome === "string" ? ", " + a.body.outcome : "") : "");
          const went = consent && a.body.delivery && typeof a.body.delivery.reason === "string" ? " · " + a.body.delivery.reason : "";
          say("signed" + (what ? " (" + what + ")" : "") + " · by " + by + ns + (line ? ": " + line : "") + went, "ok");
          if (line) signing(line, "server", "Signed"); else relabel("Signed");
          $("sheet").dataset.state = "signed";
          return "signed";
        });
      }, (e) => unsigned("The passkey did not sign (" + ((e && e.name) || "cancelled") + ")"));
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) { say("Not sure this was signed (the page failed: " + name + ").", "bad"); return "unsure"; }
      return unsigned("The page failed (" + name + ")");
    });
  }
  // ---- the drop sheet (OS-2, /sign/drop) ----
  const stamp = () => new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000Z");   // the store's fixed-width UTC form
  const newOutboxId = () => {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return "ph-" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  };
  // the listing as the cockpit holds it now: the phone's coord_* keys, {unpaired} or {error}
  function readDrops() {
    return within("/cockpit/coord/drops.json", { headers: headers(false) }).then((g) => {
      if (g.status === 403) return { unpaired: true };
      if (g.status === 404) return { error: "This cockpit hosts no drops." };
      if (g.status === 0) return { error: "The drops could not be read: " + g.body.message + "." };
      if (!g.ok) return { error: "The drops could not be read (HTTP " + g.status + ")." };
      return g.body;
    });
  }
  function dropLine(d) {
    const stages = Array.isArray(d.receipts) ? d.receipts.map((r) => r && r.stage).filter((x) => typeof x === "string") : [];
    return String(d.sent_at || "") + " · " + (typeof d.words === "string" ? d.words.split("\n")[0].slice(0, 120) : "")
      + " · " + (stages.join(", ") || "no receipt") + (d.envelope_id ? " · envelope " + d.envelope_id
      : d.envelope_refused ? " · envelope not taken: " + d.envelope_refused : "");
  }
  function drawDrop(got) {
    if (got.unpaired) return unpaired(true);
    if (got.error) { $("sheet").dataset.state = "error"; return head(Noun, got.error); }
    const body = $("sheet-body");
    body.replaceChildren();
    $("sheet-actions").replaceChildren();
    head("Drop", "Words for the coordinator, delivered as a drop; optionally with an envelope your passkey signs.");
    const origin = typeof got.origin === "string" ? got.origin : "";
    const rp = typeof got.rp_id === "string" && got.rp_id ? got.rp_id : window.location.hostname;
    const held = got.credentials && typeof got.credentials === "object" ? got.credentials : {};
    const signers = Array.isArray(got.coord_signers) ? got.coord_signers.filter((fp) => typeof fp === "string") : [];
    let storeId = typeof got.coord_store_id === "string" ? got.coord_store_id : null;
    const storeRead = got.coord_store_read === true;
    const drops = Array.isArray(got.coord_drops) ? got.coord_drops : [];
    if (typeof got.coord_note === "string" && got.coord_note) body.appendChild(el("p", "ext-meta", got.coord_note));
    if (drops.length) {
      const list = el("pre", "sheet-message", drops.map(dropLine).join("\n"));
      list.id = "drop-recent";
      body.appendChild(list);
    }
    const words = el("textarea", "");
    words.id = "drop-words";
    words.rows = 4;
    words.placeholder = "your words";
    words.setAttribute("aria-label", "your words");
    body.appendChild(words);
    const det = el("details", "");
    det.id = "drop-env";
    det.appendChild(el("summary", "", "sign an envelope"));
    const field = (id, label, node) => { node.id = id; const l = el("label", "ext-meta", label + " "); l.appendChild(node); det.appendChild(l); det.appendChild(el("br")); return node; };
    const repo = field("drop-repo", "repo", el("input"));
    repo.type = "text";
    repo.setAttribute("list", "drop-repos");
    const repos = el("datalist");
    repos.id = "drop-repos";
    for (const r of Array.isArray(got.coord_repos) ? got.coord_repos : []) if (typeof r === "string") { const o = el("option"); o.value = r; repos.appendChild(o); }
    det.appendChild(repos);
    const onto = field("drop-onto", "land onto", el("input"));
    onto.type = "text";
    onto.placeholder = "main";
    const stop = field("drop-stop", "stop after", el("select"));
    for (const h of ["1h", "2h", "4h", "8h", "12h", "24h"]) { const o = el("option", "", h); if (h === "2h") o.selected = true; stop.appendChild(o); }
    const nums = {};
    for (const k of ["calls", "tokens", "hours"]) { const n = field("drop-" + k, k, el("input")); n.type = "number"; n.min = "0"; n.placeholder = "0"; nums[k] = n; }
    body.appendChild(det);
    const review = el("pre", "sheet-message");
    review.id = "drop-review";
    review.hidden = true;
    body.appendChild(review);
    const acts = $("sheet-actions");
    acts.hidden = false;
    const send = el("button", "dev-btn", "Send drop");
    send.type = "button";
    send.id = "drop-send";
    const sign = el("button", "dev-btn dev-primary", "Sign envelope with passkey");
    sign.type = "button";
    sign.id = "drop-sign";
    acts.append(send, sign);
    $("sheet").dataset.state = "open";
    const why0 = !storeRead ? "still reading which Levain answers here; reload" : "";
    let oid = newOutboxId();       // held for these words: a resend after a lost reply files once, never twice
    let working = false;
    let pending = null;            // a signed drop whose reply did not come back: sent again unchanged, never re-signed
    const lock = (on) => { working = on; send.disabled = sign.disabled = on; words.disabled = on; };
    const done = (a, plain) => {
      lock(false);
      const d = a.body.drop || {};
      const e = a.body.envelope;
      oid = newOutboxId();
      pending = null;
      words.value = "";
      review.hidden = true;
      review.replaceChildren();
      acts.replaceChildren(send, sign);
      const msg = (a.body.created === false ? "already delivered" : "delivered") + " as " + String(d.drop_id || "") + " · by "
        + (plain ? "words only" : e && e.taken ? "envelope " + e.envelope_id + " taken" : "envelope NOT taken: " + String(e && e.refused));
      say(msg, plain || (e && e.taken) ? "ok" : "bad");
      tell("signed");
      return readDrops().then((g) => { if (!g.unpaired && !g.error) { const l = $("drop-recent"); const dd = Array.isArray(g.coord_drops) ? g.coord_drops : []; if (l) l.textContent = dd.map(dropLine).join("\n"); if (typeof g.coord_store_id === "string") storeId = g.coord_store_id; } });
    };
    const refusal = (a, nothing) => say(nothing + ": " + why(a), "bad");
    const post = (action, req) => call("/cockpit/coord/" + action, req);
    const wordsOf = () => { const w = words.value; if (!w.trim()) { say("type your words first", "bad"); return null; } return w; };
    const sendDrop = (req, plain) => post("drop", req).then((a) => {
      if (a.ok) return done(a, plain);
      if (a.status >= 400 && a.status < 500) { lock(false); return refusal(a, "the drop was not delivered"); }
      // no whole reply, or the cockpit's own fault: it may have landed. The same drop (same outbox id) is sent again on a tap
      pending = req;
      say("Not sure the drop was delivered (" + why(a) + "); send it again, unchanged, to find out.", "bad");
      const again = el("button", "dev-btn", "Send the same drop again");
      again.type = "button";
      again.addEventListener("click", () => { again.remove(); sendDrop(pending, plain); });
      acts.appendChild(again);
      return undefined;
    });
    send.addEventListener("click", () => {
      if (working) return;
      const w = wordsOf();
      if (w === null) return;
      if (why0) return say(why0, "bad");
      lock(true);
      sendDrop({ words: w, outbox_id: oid, sent_at: stamp(), store_id: storeId }, true);
    });
    // what the envelope needs, said before any tap: never a ceremony that cannot verify
    const cannot = !usable ? "this browser cannot use a passkey here (needs https)"
      : origin !== window.location.origin ? "this page cannot use your passkey: sign on " + (origin || "the cockpit's https page")
      : typeof got.elsewhere === "string" && got.elsewhere ? got.elsewhere
      : !signers.length ? (got.coord_signer_note || "no passkey enrolled for " + rp + " can sign an envelope") : "";
    if (cannot) { const n = el("p", "ext-meta", "Envelope: " + cannot); n.id = "drop-sign-note"; body.appendChild(n); sign.disabled = true; sign.dataset.cannot = "1"; }
    sign.addEventListener("click", () => {
      if (working || cannot) return;
      const w = wordsOf();
      if (w === null) return;
      const r = repo.value.trim(), ont = onto.value.trim();
      if (!r) { det.open = true; return say("name the repo the envelope lets it work in", "bad"); }
      if (why0) return say(why0, "bad");
      const n = (k) => { const v = nums[k].value.trim(); return v === "" ? 0 : Number(v); };
      const spec = { where: [{ repo: r, paths: ["."] }], lands: ont ? [{ repo: r, onto: ont }] : [], stop: stop.value,
                     budget: { tool_calls: n("calls"), tokens: n("tokens"), hours: n("hours") } };
      lock(true);
      busy = true;
      const release = () => { busy = false; lock(false); };
      whom(rp, signers, held, acts).then((k) => {
        if (!k) { release(); return undefined; }
        say("Building the envelope to sign…");
        return post("envelope-review", { words: w, outbox_id: oid, spec: spec, signer_fp: k.fp, store_id: storeId }).then((c) => {
          if (!c.ok) { release(); return refusal(c, "Nothing to sign"); }
          // a fresh install's review made the store: only then does a null identity take the reply's
          if (storeId === null && c.body.store_created === true && typeof c.body.store_id === "string") storeId = c.body.store_id;
          review.textContent = (Array.isArray(c.body.view) ? c.body.view.join("\n") : "") + "\n\nenvelope digest: " + String(c.body.digest || "");
          review.hidden = false;
          review.tabIndex = 0;
          say("Read the envelope, then sign it.");
          const go = el("button", "dev-btn dev-primary", "Sign with your passkey");
          go.type = "button";
          const no = el("button", "dev-btn", "Dismiss");
          no.type = "button";
          acts.replaceChildren(go, no);
          no.addEventListener("click", () => { review.hidden = true; review.replaceChildren(); acts.replaceChildren(send, sign); say("Not signed; your words are still here."); release(); });
          go.addEventListener("click", () => {
            go.disabled = no.disabled = true;
            let sent = false;
            flight = true;
            const stop1 = (m) => { relabel("Not signed"); ended(); say(m, "bad"); go.disabled = no.disabled = false; };
            post("envelope-challenge", { review_id: c.body.review_id }).then((ch) => {
              if (!ch.ok) return stop1("Not signed: " + why(ch) + "; nothing was signed.");
              if (!decodable(ch.body.challenge)) return stop1("The cockpit sent a malformed challenge; nothing was signed.");
              const line = typeof ch.body.line === "string" ? ch.body.line : "";
              if (line) signing(line, "server", "Signing");
              say("Confirm with your passkey to sign…" + (line ? " " + line : ""));
              const allow = k.rawId && decodable(k.rawId) ? unb64u(k.rawId) : null;
              const ask = (aid) => {
                const pk = { challenge: unb64u(ch.body.challenge), rpId: ch.body.rp_id, userVerification: "required", timeout: 60000 };
                if (aid) pk.allowCredentials = [{ type: "public-key", id: aid }];
                return navigator.credentials.get({ publicKey: pk });
              };
              return ask(allow).catch((e) => { if (!(e && e.name === "NotAllowedError" && allow && k.local)) throw e; return ask(null); }).then((cred) => {
                say("Sending the signed drop…");
                const envelope = { body_text: c.body.body_text, payload: ch.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
                                   client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
                sent = true;
                if (cred.rawId && mine(rp)[k.fp] !== b64u(cred.rawId)) remember(rp, k.fp, b64u(cred.rawId));
                return post("drop", { words: w, outbox_id: oid, sent_at: stamp(), store_id: storeId, envelope: envelope }).then((a) => {
                  if (a.ok) { ended(); if (line) signing(line, "server", "Signed"); else relabel("Signed"); $("sheet").dataset.state = "signed"; busy = false; return done(a, false); }
                  if (a.status >= 400 && a.status < 500) {
                    // refused: nothing was delivered with this signature (a stale one is signed again from the review)
                    relabel("Not signed"); ended(); acts.replaceChildren(send, sign); review.hidden = true; review.replaceChildren(); release();
                    return refusal(a, "the signed drop was not delivered");
                  }
                  relabel("Not confirmed"); ended();
                  pending = { words: w, outbox_id: oid, sent_at: stamp(), store_id: storeId, envelope: envelope };
                  say("Not sure the signed drop was delivered (" + why(a) + "); send it again, unchanged, to find out.", "bad");
                  const again = el("button", "dev-btn", "Send the signed drop again");
                  again.type = "button";
                  again.addEventListener("click", () => { again.remove(); busy = false; sendDrop(pending, false); });
                  acts.replaceChildren(again);
                  return undefined;
                });
              }, (e) => stop1("The passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was signed."));
            }).catch((e) => {
              const name = (e && e.name) || "error";
              if (sent) { relabel("Not confirmed"); ended(); say("Not sure the signed drop was delivered (the page failed: " + name + ").", "bad"); return; }
              stop1("The page failed (" + name + "); nothing was signed.");
            });
          });
          return undefined;
        });
      }).catch(() => { release(); });
    });
  }
})();
