// The sign sheet (ONESURFACE OS-1, levain project_memory/1025-32/onesurface/DESIGN.md §3, §4A.3): GET /sign/<id> on the
// cockpit's own origin, top-level or framed by the cockpit (its overlay). It runs the cockpit's existing ceremony on that one
// request and nothing else: GET /cockpit/sign/<id>.json for the request, then a tap is POST /cockpit/sign/<id>/challenge ->
// navigator.credentials.get -> POST /cockpit/sign/<id>/answer. The bytes signed are the server's; the line shown after a
// signature is the challenge's own `line`, never this page's copy.
//
// The fragment: `v=<16 hex>` (the version the link was made on) and `choice=<key>` (the option already tapped elsewhere)
// follow one rule. The pick is one "Sign (<key>)" tap only while the listed version is the one in the link;
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
// come back). Only "unsigned" re-arms the controls. A sign request's answer carries an attempt id this page made, and
// "unsure" asks the cockpit where that attempt stands (POST /cockpit/sign/<id>/attempt, levain.consent.sign
// Signer.attempt): "signed" shows it signed, only "refused" re-arms, and "pending" (still verifying past the cockpit's
// wait) or no answer keeps the controls frozen, "Not confirmed", with a "Check again" that asks once more. No read of
// the request can stand in for that: the answer may still be verifying while the request reads open. A consent request's
// or a dispatch job's answer has no attempt record, so after "unsure" its controls stay frozen until the page is
// reloaded. A write-in is frozen as typed (trimmed) from its click on.
//
// A consent request (OS-2) is the same sheet at /sign/consent/<id>: its read is GET /cockpit/consent/<id>/sheet.json (the
// same row shape, its options allow and deny), and its ceremony: POST .../challenge and .../answer for
// a pending request, .../rule-challenge and .../rule for an undecided one (the row's `mode`)
// ({decision, signer_fp}, then the challenge's `payload` beside the assertion).
//
// A dispatch job (OS-2) is the same sheet at /sign/dispatch/<job>: its read is GET /cockpit/dispatch/<job>/sheet.json (the
// same row shape; its options sign, cancel and authorise, each with the `signers` that may do THAT act), and its ceremony
// by option: sign is .../challenge then .../answer ({signer_fp}), cancel is .../cancel-challenge then
// .../cancel ({signer_fp, reason}, the reason typed in the box the row asks for), authorise is .../authorise-challenge then
// .../authorise ({signer_fp}); each answer is the challenge's `payload` beside the assertion, nothing else. Each challenge
// also carries `request_sha256`, the row's `sha256` this sheet showed (as a plain sign request's does): the cockpit refuses
// it with a 409 when the job changed since, and the sheet then reads the job again and draws it afresh. Only the acts
// DISPATCH_STEPS names are offered; any other key the row lists is not drawn and signs nothing.
//
// A coordinator drop (OS-2) is the same sheet at /sign/drop: its read is GET /cockpit/coord/drops.json (the coord_*
// listing keys), and it composes the drop's words and, optionally, an envelope. A plain drop is POST /cockpit/coord/drop. An
// envelope is POST /cockpit/coord/envelope-review (the server keeps the body it shows), then POST .../envelope-challenge (the
// payload, signed_at now, and the WebAuthn challenge), navigator.credentials.get, and POST .../drop carrying the envelope
// {body_text, payload, authenticator_data, client_data_json, signature}: POST /cockpit/coord/<action> by name.
// The bytes signed are the server's; the line shown is the challenge's own `line`.
//
// A head card (CH-H1 H1-3) is the same sheet at /sign/drop#thread=<id> (the thread view links it): no new GET, only
// POST /cockpit/coord/draft-cards, draft-rule, draft-withdraw, draft-review, draft-challenge and draft-accept. The LAPSED
// card offers Extend, the REVOKED card Replace (an unsigned draft, granting nothing), the ENVELOPE card Review and Withdraw;
// the review draws the diffs the server's builder returned, and Sign mints the challenge at the tap (draft-challenge), asks
// the passkey and posts the assertion to draft-accept. No field of the card is typed here; the key is the drop sheet's.
(function () {
  "use strict";
  if (window.LevainCardFormat && typeof window.LevainCardFormat.installFieldGrow === "function") window.LevainCardFormat.installFieldGrow();

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
  // the drop sheet inside the cockpit's modal IS the modal's body: no card, no brand line, no title of its own (the modal's
  // heading names it), the full width. Opened on its own (the phone) it keeps the card.
  const embedded = framed && location.pathname === "/sign/drop";
  { const ml = document.getElementById("sheet-enrol-link"); if (ml && framed) ml.target = "_top"; }   // /enrol refuses a frame
  if (embedded) {
    document.body.classList.add("sheet-embedded");
    const h1 = document.getElementById("sheet-title");
    if (h1) h1.hidden = true;
    const brand = document.querySelector(".pair-brand");
    if (brand) brand.hidden = true;
  }

  // ---- the link ----
  const consentPath = /^\/sign\/consent\/([^/]{1,480})$/.exec(location.pathname);
  const dispatchPath = /^\/sign\/dispatch\/(dj-[0-9a-f]{32})$/.exec(location.pathname);     // the dispatch job id shape
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
  // the dispatch ceremony's two routes by the act picked
  const DISPATCH_STEPS = { sign: ["challenge", "answer"], cancel: ["cancel-challenge", "cancel"],
                           authorise: ["authorise-challenge", "authorise"] };
  const dispatchAct = (key) => typeof key === "string" && Object.prototype.hasOwnProperty.call(DISPATCH_STEPS, key);
  const pm = /(?:^#|&)choice=([^&]*)(?:&|$)/.exec(location.hash);
  const vm = /(?:^#|&)v=([0-9a-f]{16})(?:&|$)/.exec(location.hash);
  // the thread whose head card the drop sheet draws (the thread view's link): read here, the next line takes the fragment out of the bar
  const tm = /(?:^#|&)thread=([A-Za-z0-9._:-]{1,160})(?:&|$)/.exec(location.hash);
  const headThread = location.pathname === "/sign/drop" && tm ? tm[1] : "";
  let pick = "";
  const pickV = vm ? vm[1] : "";
  if (pm) { try { pick = decodeURIComponent(pm[1]); } catch (e) { pick = pm[1]; } }
  if (location.hash) { try { history.replaceState(null, "", location.pathname); } catch (e) { /* the bar keeps it */ } }
  // a second link opened on this page (a fragment-only navigation does not reload it): read it afresh
  window.addEventListener("hashchange", () => { if (location.hash) location.reload(); });

  // The one door to the authenticator in this page. navigator.credentials.get is called from pkGet and nowhere else, and only
  // while a tap is armed. Only a person's own press arms it (event.isTrusted): a click on a control, Space or Enter on a
  // control, Enter in a one-line field, Cmd/Ctrl+Enter anywhere. Space or Enter typed into a text box does not, nor does an
  // evaluated .click() or a dispatched event, a focus, visibility, pageshow, online, reconnect, render or timer, so a passkey
  // sheet taking the window's focus (and giving it back) cannot ask for another. One get() consumes the arm (one tap, one
  // ask), and it lapses after PK_ARM_MS: long enough for the challenge read a tap starts before its prompt, so a flow stalled
  // well past its tap shows no prompt and says a tap tries again. A refusal or cancel is never retried here: the page says so
  // and the next tap asks again. A remembered credential the authenticator refused is dropped for the next tap's ask (the
  // OS's own list), kept on disk, until a passkey signs for that rpId|fp (pkSigned).
  // tapArmable is the same text in dashboard_core.js (test_passkey_tap_only holds the two equal)
  const PK_ARM_MS = 10000;
  // tapArmable:begin
  const TAP_CONTROL = "button, a[href], summary, select, label, input, [role=button], [role=menuitem], [role=option], [role=radio], [role=checkbox], [role=tab], [role=link]";
  const TAP_TEXT = "textarea, [contenteditable]:not([contenteditable=false]), input:not([type]), input[type=text], input[type=search], input[type=email], input[type=url], input[type=tel], input[type=password], input[type=number]";
  function tapArmable(ev) {
    if (!ev || !ev.isTrusted) return false;
    const t = ev.target && typeof ev.target.closest === "function" ? ev.target : null;
    if (!t) return false;
    const text = t.closest(TAP_TEXT);
    if (ev.type === "click") return !text && !!t.closest(TAP_CONTROL);
    if (ev.type !== "keydown" || ev.isComposing || ev.repeat) return false;
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) return true;
    if (text) return ev.key === "Enter" && text.tagName === "INPUT";
    return (ev.key === "Enter" || ev.key === " ") && !!t.closest(TAP_CONTROL);
  }
  // tapArmable:end
  let pkArmedAt = 0;
  const pkArm = (ev) => { if (tapArmable(ev)) pkArmedAt = Date.now(); };
  document.addEventListener("click", pkArm, true);
  document.addEventListener("keydown", pkArm, true);
  // rpId|fp whose remembered id the authenticator refused on this page: whom passes over that key while another remembered
  // key remains, until a passkey signs (signed clears it, and forgets the record when another key signed). A plain sign
  // request (k.fp null) asking one remembered id records the refusal under the key that id was remembered for (k.of)
  const pkRefused = new Set();
  const pkKey = (pk, k) => String(pk.rpId) + "|" + String(k && (k.fp === null && k.of ? k.of : k.fp));
  // the door's own refusal (no tap asked) is NoTapError, never NotAllowedError: no authenticator was asked, so it is never
  // read as a cancel and never drops the remembered credential (only the get() below's own refusal does)
  function pkGet(pk, id, k) {
    if (!pkArmedAt || Date.now() - pkArmedAt > PK_ARM_MS) {
      pkArmedAt = 0;
      return Promise.reject(Object.assign(new Error("no tap asked for this passkey prompt"), { name: "NoTapError" }));
    }
    pkArmedAt = 0;
    const key = pkKey(pk, k);
    // `id`: one credential id, or a list of them (a plain sign request names its signer in the answer, so every id the page
    // knows is asked for together); an empty list asks for none (the OS's own list)
    const ids = Array.isArray(id) ? id : id ? [id] : [];
    const use = ids.length && !pkRefused.has(key) ? ids : null;
    return navigator.credentials.get({ publicKey: use ? Object.assign({}, pk, { allowCredentials: use.map((i) => ({ type: "public-key", id: i })) }) : pk })
      .catch((e) => { if (e && e.name === "NotAllowedError" && use && k && k.local) pkRefused.add(key); throw e; });
  }
  // a passkey signed for rpId|fp (where the record is updated to the credential that signed): a refusal recorded for it is
  // over, so the next tap asks for the recorded passkey by its id again
  function pkSigned(pk, k) { pkRefused.delete(pkKey(pk, k)); }
  // after key `by` signed on rpId `rp`: a refusal for it is over, and a remembered record the authenticator refused on this
  // page while ANOTHER key then signed is forgotten (it names a passkey this device no longer holds, or one only cancelled,
  // which a later sign learns again); kept, it would be asked first, and fail first, on every tap after this one
  function pkSettle(rp, by) {
    if (!by) return;
    pkRefused.delete(rp + "|" + by);
    for (const k of Object.keys(mine(rp))) {
      if (k !== by && pkRefused.has(rp + "|" + k)) { forget(rp, k); pkRefused.delete(rp + "|" + k); }
    }
  }

  // what a refused passkey ceremony says: a cancel and a timeout both raise NotAllowedError, so that name is said in words
  function passkeyWhy(e) {
    const n = e && e.name;
    if (n === "NoTapError") return "No passkey prompt was shown (no tap asked for one in time), so the passkey did not sign";
    if (!n || n === "NotAllowedError") return "You cancelled, or the passkey prompt timed out, so the passkey did not sign (if this device holds no passkey for this cockpit, make one with Make a passkey on this device?)";
    return "The passkey did not sign (" + n + ")";
  }

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
  function tell(what, extra) {
    if (!framed) return;
    try { window.parent.postMessage(Object.assign({ levainSheet: what, id: id }, extra || {}), location.origin); } catch (e) { /* no parent to tell */ }
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
           ? "Pair it again from a paired device: open the cockpit there, then Devices, Make a link, and open the link here."
           : "Open the cockpit on this device first (its link carries the key), or pair it from a paired device: Devices, Make a link.");
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
  // a refusal the cockpit made because the item changed since this sheet read it (DispatchSheet.shown's words)
  const staleReply = (r) => !!(r.body && typeof r.body.message === "string" && /changed since it was shown/.test(r.body.message));
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

  let cardView = null;           // the needs-phill card the sheet is drawing (set by draw, read by offer), else null

  // ---- read the request, then draw it ----
  if (!id) { head("Not a " + noun, "This link names no " + noun + "."); return; }
  if (consent || dispatch || coord) { document.title = "Levain · " + (consent ? "consent" : dispatch ? "dispatch" : "drop"); $("sheet-title").textContent = Noun; $("sheet-msg").textContent = "Reading the " + noun + "…"; }
  if (!storedToken()) { unpaired(false); return; }
  if (coord) readDrops().then(drawDrop);
  else readRequest().then(draw);

  // ---- the card: headline now, Detail below the option rows ----
  // The card is two nodes, both marked data-card: #sheet-card (meta, headline, the signed text) above the controls and
  // #sheet-detail below them. clearCard takes every such node off and forgets the card, first thing in every draw.
  function clearCard() {
    cardView = null;
    for (const n of document.querySelectorAll("[data-card]")) n.remove();
    const acts = $("sheet-actions");
    if (acts) acts.classList.remove("card-rows");
  }
  // the nodes are card_format.js's; the ids are this page's
  function drawCardHead(body, card, msg) {
    const box = window.LevainCardFormat.drawHead(card, msg);
    box.id = "sheet-card";
    box.dataset.card = "head";
    box.querySelector(".card-headline").id = "sheet-headline";
    // the signed text is verification detail, not the reading: it leaves the head box and stands after the body (below the open-until
    // line), collapsed, and holds the signer line and the option lines too, one disclosure for what exactly gets signed
    const raw = box.querySelector(".card-signed");
    raw.id = "sheet-signed";
    raw.dataset.card = "signed";
    const rawSummary = raw.querySelector("summary");
    if (rawSummary) rawSummary.textContent = "What exactly gets signed";
    raw.remove();
    body.appendChild(box);
    body.after(raw);
  }
  // where the signing detail (May sign, the option lines) goes: inside the card's signed disclosure, else the body
  function signingHome() { return (cardView && $("sheet-signed")) || $("sheet-body"); }
  // Detail stands where it is read, in the DOM and not only by CSS `order`, so keyboard and screen-reader order follow the page:
  // from 800px it is the reading column's (before the signing detail and the deciding column, #sheet-decide); below 800px it follows the answers
  // (the group is display: contents there, so Detail sits between the answers and the status line, as it always did)
  function placeDetail(det) {
    const wide = window.matchMedia && window.matchMedia("(min-width: 800px)").matches;
    const decide = $("sheet-decide"), acts = $("sheet-actions");
    const signed = $("sheet-signed");
    if (wide && signed) signed.before(det);          // the grid reads Detail, then the signing detail, down the reading column
    else if (wide && decide) decide.before(det);
    else if (acts && acts.parentNode) acts.after(det);
    else $("sheet").appendChild(det);
  }
  function drawCardDetail(card, options) {
    const stale = $("sheet-detail");
    if (stale) stale.remove();
    const det = window.LevainCardFormat.drawDetail(card, options);
    det.id = "sheet-detail";
    det.dataset.card = "detail";
    placeDetail(det);
    if (!placeDetail.watching && window.matchMedia) {
      placeDetail.watching = true;
      const mq = window.matchMedia("(min-width: 800px)");
      const again = () => { const d = $("sheet-detail"); if (d) placeDetail(d); };
      if (mq.addEventListener) mq.addEventListener("change", again); else if (mq.addListener) mq.addListener(again);
    }
  }

  function draw(got) {
    clearCard();
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
    const msg = el("pre", "sheet-message", typeof r.message === "string" ? r.message : "");
    // a needs-phill card is drawn as headline, option rows, then a collapsed Detail (card_format.js, which began as a port of the iOS card, now gone);
    // its signed text stays whole, one tap away, above the controls. Any other message, and any needs-phill
    // message parseMessage does not read exactly (it returns null), is one visible block.
    const F = window.LevainCardFormat;
    const card = !dispatch && !consent && F && typeof F.drawHead === "function" ? (F.parseMessage(r.message, r) || (typeof F.looseCard === "function" ? F.looseCard(r.message, r) : null)) : null;
    cardView = card;
    $("sheet-actions").classList.toggle("card-rows", !!card);
    if (card) drawCardHead(body, card, msg);
    else body.appendChild(msg);
    // a card's headline IS the title: the header above it drops the request's title, so it is said once
    { const t = $("sheet-title"); if (t) t.hidden = !!card; }
    // a dispatch job is shown whole (every path, every change): its box scrolls, it is never cut to fit
    if (dispatch) { msg.id = "sheet-contract"; msg.tabIndex = 0; Object.assign(msg.style, { maxHeight: "50vh", overflow: "auto" }); }
    if (r.expires_at) {
      const when = window.LevainCardFormat && typeof window.LevainCardFormat.openUntil === "function" ? window.LevainCardFormat.openUntil(r.expires_at) : { text: "Open until " + r.expires_at, title: "" };
      const p = el("p", "ext-meta", when.text);
      p.id = "sheet-open-until";
      if (when.title) p.title = when.title;
      body.appendChild(p);
    }
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
      + (held[fp] && typeof held[fp].provider === "string" && held[fp].provider && !/^provider not recorded/i.test(held[fp].provider)
        ? " (" + held[fp].provider + ")" : "")).join(", ");     // an unrecorded provider is not worth a clause
    signingHome().appendChild(who);
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
  // The status line (#sheet-note) is outside #sheet-body, so the "Not signed" that led here stays; a read or a draw that
  // fails says so beside it and locks every control left in the actions (the old ones were for the version that changed;
  // a draw that threw part way may have left new ones)
  function redraw() {
    let before = "";
    // from a resolved promise, so a read (or a missing part of the page) that throws at once is this catch's too, never
    // an unhandled one; the catch itself reads no part it has not checked is there
    return Promise.resolve().then(() => {
      const n = $("sheet-note");
      before = n ? n.textContent : "";
    }).then(readRequest).then((got) => {
      $("sheet-body").replaceChildren();
      $("sheet-actions").replaceChildren();
      draw(got);
    }).catch((e) => {
      const acts = $("sheet-actions");
      if (acts) for (const c of acts.querySelectorAll("button, input")) c.disabled = true;
      if ($("sheet-note")) say((before ? before + " " : "") + "The " + noun + " could not be drawn again ("
          + ((e && e.name) || "error") + "): reload this page before signing.", "bad");
      tell("reload");
    });
  }

  // §4A.3 rule 4: no key that may sign this request, so no ceremony is started; what it needs, and where it is signed
  function escalate(rp, note) {
    $("sheet").dataset.state = "escalate";
    const card = el("div", "sheet-escalate");
    card.id = "sheet-escalate";
    card.appendChild(el("p", "", "No passkey can sign this here: it needs an active passkey enrolled for " + rp + "."));
    if (typeof note === "string" && note) card.appendChild(el("p", "ext-meta", note));
    card.appendChild(el("p", "", "Make a passkey on this device, enrol it at the laptop (levain cockpit enroll, signed by the laptop key), then open this link again."));
    const mk = el("a", "sign-enrol-link", "Make a passkey on this device");
    mk.href = "/enrol";
    // the overlay frames this sheet and /enrol refuses to be framed: the link leaves the frame
    if (framed) mk.target = "_top";
    card.appendChild(mk);
    $("sheet-body").appendChild(card);
  }

  // what is being signed, beside the controls: the page's expectation at the tap ("page"), then the line the challenge
  // carries ("server"), which after a signature is the line signed
  // `label`: "Signing" while the ceremony runs; "Signed", "Not signed" or "Not confirmed" once it has ended
  // After a card is signed the page leads with what happened, in the reader's words: "Signed (b): Hold for the morning". What
  // the page said before (the pick line, when the request is open until) goes, and the exact record (the status line and the
  // signed line) moves into the card's "What exactly gets signed" disclosure, whole, for audit.
  function showOutcome(what, line) {
    if (!cardView) return;
    const pick = $("sheet-pick"); if (pick) pick.remove();
    const miss = $("sheet-pick-miss"); if (miss) miss.remove();
    const until = $("sheet-open-until"); if (until) until.remove();
    // with the choice known, its own "(choice):" marks where the words begin; else the first one in the line does
    const text = String(line || "");
    const at = what ? text.indexOf("(" + what + "):") : -1;
    const m = at >= 0 ? /^\(([^)]*)\):\s*([\s\S]*)$/.exec(text.slice(at)) : /\(([^)]*)\):\s*([\s\S]*)$/.exec(text);
    const choice = what || (m ? m[1] : "");
    const words = m && m[2] ? m[2] : "";
    let out = $("sheet-outcome");
    if (!out) {
      // a live region is announced when its text changes after it is in the page, so it goes in empty and is filled after
      out = el("p", "sheet-outcome"); out.id = "sheet-outcome"; out.setAttribute("role", "status");
      const head = $("sheet-card"); if (head) head.after(out); else $("sheet-body").prepend(out);
    }
    out.textContent = "Signed" + (choice ? " (" + choice + ")" : "") + (words ? ": " + words : "");
    // the exact record moves into the disclosure whole (same nodes, same ids), where "What exactly gets signed" opens it
    const home = $("sheet-signed");
    if (home) for (const id of ["sheet-signing", "sheet-note"]) { const n = $(id); if (n && n.parentNode !== home) home.appendChild(n); }
  }
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
  let busy = false, flight = false, unsure = false, active = null;     // `unsure`: a drop's delivery is not known
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
      whom(rp, keysFor(choice), held, acts, !consent && !dispatch).then((k) => (k ? ceremony(r, rp, k, choice, words) : "unsigned"))
        .catch(() => "unsigned")
        .then((done) => {
          if (done === "signed") { busy = false; ended(); acts.replaceChildren(); acts.hidden = true; tell("signed"); return; }
          if (done === "unsure") {
            relabel("Not confirmed");
            return settle(r, () => { relabel("Not signed"); busy = false; lock(false); },
                          () => { busy = false; acts.replaceChildren(); acts.hidden = true; tell("signed"); }).then(ended);
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
    // a dispatch row's options are only the acts this sheet has routes for (DISPATCH_STEPS): any other key is not drawn
    const options = (Array.isArray(r.options) ? r.options : []).filter((o) => !dispatch || (o && dispatchAct(o.key)));
    // a dispatch cancel carries a reason (one line, optional) typed in the box the row asks for; it is frozen as typed
    // (trimmed) from the click on, like a write-in, and is the `words` of the run
    let reasonBox = null;
    if (dispatch && r.reason && typeof r.reason === "object" && options.some((o) => o && o.key === "cancel")) {
      reasonBox = el("input", "sheet-write-in");
      reasonBox.type = "text";
      reasonBox.id = "sheet-reason";
      reasonBox.setAttribute("name", "sheetreason");        // dash-free, like the Drop box (Safari AutoFill Contact, 11-04)
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
    // his pick from the link: one tap that signs that option, only on the version he tapped it on
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
      const b = button("(" + o.key + ") " + String(o.text), o.key === (cardView ? cardView.recommended : r.recommended) ? "sheet-rec" : "", tap(o));
      if (cardView) b.replaceChildren(el("span", "card-key", "(" + o.key + ")"), " " + String(o.text));      // same text, the key set off
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
      box.setAttribute("name", "writein");        // dash-free, like the Drop box (Safari AutoFill Contact, 11-04)
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
    if (cardView) drawCardDetail(cardView, options);
    if (options.length) {
      const lines = el("div", "ext-meta", dispatch ? "Each act signs the payload the cockpit builds for it (shown above the tap):"
        : "Each option signs the message with its line appended:");
      for (const o of options) if (o && typeof o.line === "string") lines.appendChild(el("div", "sheet-line", o.line));
      signingHome().appendChild(lines);
    }
  }

  // "unsure": the answer was posted and no reply said what landed. The cockpit is asked where THAT answer stands, by the
  // attempt id it carried (`unsureAttempt`, set by the ceremony): "refused" gives the controls back (`release`), "signed"
  // shows it signed (`signedNow`), and anything else leaves them frozen, "Not confirmed", with "Check again". A consent
  // request's answer carries no attempt: its controls stay frozen and the page says to reload it
  let unsureAttempt = null;      // {attempt, line} of the sign answer whose reply was lost, until it settles
  function settle(r, release, signedNow) {
    const a = unsureAttempt;
    if (!a) {
      say($("sheet-note").textContent + " Reload this page to see where it stands before signing again.", "bad");
      tell("reload");
      return Promise.resolve();
    }
    say($("sheet-note").textContent + " Asking the cockpit where this answer stands…", "bad");
    // a read that fails outright is a read that said nothing: frozen, "Not confirmed", Check again; never re-armed
    return Promise.resolve().then(() => call(routeOf(r.id) + "/attempt", { attempt: a.attempt }))
      .catch((e) => ({ ok: false, status: 0, body: { message: "the read failed (" + ((e && e.name) || "error") + ")" } }))
      .then((st) => {
      const state = st.ok && st.body && typeof st.body.state === "string" ? st.body.state : "";
      if (state === "signed") {
        unsureAttempt = null;
        dropRecheck();
        const by = typeof st.body.signer_fp === "string" && st.body.signer_fp ? " · by " + st.body.signer_fp : "";
        const ns = typeof st.body.namespace === "string" && st.body.namespace ? " in " + st.body.namespace : "";
        say("signed" + (st.body.choice ? " (" + st.body.choice + ")" : "") + by + ns + (a.line ? ": " + a.line : "")
            + " (its reply was lost; the cockpit confirmed it)", "ok");
        if (a.line) signing(a.line, "server", "Signed"); else relabel("Signed");
        $("sheet").dataset.state = "signed";
        showOutcome(st.body.choice, a.line);
        return signedNow();
      }
      if (state === "refused") {
        unsureAttempt = null;
        dropRecheck();
        const reason = typeof st.body.reason === "string" && st.body.reason ? st.body.reason.replace(/[.\s]+$/, "") : "refused";
        say("Not signed: " + reason + (/nothing was signed/.test(reason) ? "" : "; nothing was signed") + ". You can sign it again.", "bad");
        return release();
      }
      say(state === "pending"
        ? "Not confirmed: the cockpit is still verifying this answer. The controls stay locked until it says; Check again asks once more."
        : "Not confirmed: the cockpit did not say where this answer stands (" + why(st) + "). The controls stay locked; Check again asks once more.", "bad");
      offerRecheck(r, release, signedNow);
      tell("reload");
      return undefined;
    });
  }
  // "Check again": one more status read of the unsettled attempt; it is never pressed for him
  function offerRecheck(r, release, signedNow) {
    if ($("sheet-recheck")) return;
    const b = el("button", "dev-btn", "Check again");
    b.type = "button";
    b.id = "sheet-recheck";
    b.addEventListener("click", () => { b.disabled = true; settle(r, release, signedNow).then(() => { b.disabled = false; }); });
    $("sheet-actions").appendChild(b);
  }
  function dropRecheck() { const b = $("sheet-recheck"); if (b) b.remove(); }
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
    if (coord && (flight || unsure) && !closeAsked) {
      // a drop has no request to read again: the line says the outcome is not confirmed, and a second close leaves
      closeAsked = true;
      relabel("Not confirmed");
      say("Signing is still in progress, so it is not confirmed (close again to leave).", "bad");
      return tell("reload");
    }
    if (!(flight || (coord && unsure)) || closeAsked) return tell("close");
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

  // The words one pick-list button carries for key `fp` (the same as dashboard_core.js's signKeyLabel): the provider its
  // enrolment recorded (else "provider not recorded"), when it was enrolled and its number in the authority log, from the
  // read's `credentials`; an fp prefix only when the cockpit sent neither.
  function keyLabel(fp, facts) {
    const f = facts && typeof facts === "object" ? facts : {};
    let prov = typeof f.provider === "string" ? f.provider : "";
    if (!prov || /^provider not/.test(prov)) prov = prov.replace(/ \(.*$/, "") || "provider not recorded";
    const at = typeof f.enrolled_at === "string" ? new Date(f.enrolled_at) : null;
    const pad = (n) => (n < 10 ? "0" : "") + n;
    const when = at && !isNaN(at.getTime())
      ? " · enrolled " + pad(at.getMonth() + 1) + "-" + pad(at.getDate()) + " " + pad(at.getHours()) + ":" + pad(at.getMinutes()) : "";
    const num = typeof f.seq === "number" ? " (#" + f.seq + ")" : "";
    return prov + when + num + (when || num ? "" : " · " + fp.slice(0, 18) + "…");
  }
  // which eligible key signs: the one this browser used before (the newest enrolled of them, when it used several, passing
  // over one the authenticator refused on this page), else the
  // only one, else his pick from a list. Resolves {fp, rawId, local} or null (dismissed); rawId is this browser's record
  // (local: true), else the cockpit's, else null (the OS's list). `late` (a plain sign request, whose signer the cockpit's
  // reply names) never draws a pick: it resolves {fp: null, ids, local}, `ids` the one remembered id, else every enrolled
  // id when all are known, else none (the OS's list, which holds every levain passkey this device has)
  function whom(rp, signers, held, acts, late) {
    const local = mine(rp);
    // an entry that cannot be an id names no passkey: it is forgotten before it decides who signs (late mode too)
    for (const k of Object.keys(local)) if (!decodable(local[k])) { forget(rp, k); delete local[k]; }
    const has = (fp) => Object.prototype.hasOwnProperty.call(local, fp);
    const idOf = (fp) => (has(fp) ? { fp: fp, rawId: local[fp], local: true }
      : { fp: fp, rawId: held[fp] && typeof held[fp].credential_id === "string" ? held[fp].credential_id : null, local: false });
    const remembered = signers.filter(has);
    // a remembered key whose id the authenticator refused on this page is passed over (L3 picker r1): asking it again would
    // fail again, and a bound challenge must never pair it with the OS's whole list, which signs with a key it is not bound to
    const known = remembered.filter((fp) => !pkRefused.has(rp + "|" + fp));
    const seqOf = (fp) => (held[fp] && typeof held[fp].seq === "number" ? held[fp].seq : -1);
    const newest = (fps) => fps.reduce((best, fp) => (seqOf(fp) >= seqOf(best) ? fp : best), fps[0]);
    if (late) {
      if (known.length) { const of = newest(known); return Promise.resolve({ fp: null, ids: [local[of]], local: true, of: of }); }
      const all = signers.map((fp) => idOf(fp).rawId);
      // every remembered key refused: the OS's own list (whichever levain passkey this device holds signs)
      const ok = !remembered.length && signers.length && all.every((i) => typeof i === "string");
      return Promise.resolve({ fp: null, ids: ok ? all : [], local: false });
    }
    if (known.length) return Promise.resolve(idOf(newest(known)));
    if (signers.length === 1) return Promise.resolve(idOf(signers[0]));
    say("More than one passkey may sign this; pick one this device holds (a synced passkey such as iCloud Keychain is on all"
      + " your devices; any one on this device works):");
    return new Promise((resolve) => {
      const picks = [];
      const end = (v) => { picks.forEach((x) => x.remove()); resolve(v); };
      for (const fp of signers) {
        const p = el("button", "dev-btn sheet-key", keyLabel(fp, held[fp]));
        p.type = "button";
        p.title = fp;
        // a pick is his word for that key: a refusal recorded for it is over, so it is asked for by its id (the challenge is
        // bound to it, so the OS's whole list would offer keys that cannot answer it)
        p.addEventListener("click", () => { pkRefused.delete(rp + "|" + fp); end(idOf(fp)); });
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
    // a consent request takes the bodies: {decision, signer_fp}, then the payload the
    // challenge returned beside the assertion; an undecided one is ruled (its own pair of routes), never answered
    // a dispatch job takes the pair of routes of the act picked (sign, cancel, authorise), {signer_fp}
    // (a cancel's also {reason}), then the payload beside the assertion
    const unsigned = (m) => { say(/nothing was signed/.test(m) ? m : m + "; nothing was signed.", "bad"); return "unsigned"; };
    // an act with no routes here signs nothing (never another act's routes in its place)
    if (dispatch && !dispatchAct(choice)) return Promise.resolve(unsigned("Not signed: (" + String(choice).slice(0, 24) + ") is not an act this sheet signs"));
    const steps = dispatch ? DISPATCH_STEPS[choice]
      : !consent ? ["challenge", "answer"] : r.mode === "rule" ? ["rule-challenge", "rule"] : ["challenge", "answer"];
    // a dispatch challenge, like a plain sign request's, names the version this sheet showed: the cockpit refuses a job
    // that changed since (409), so no passkey is asked for a change set the operator did not read
    // fp null (a plain sign request): the answer names the signer, so the challenge names none
    const body = dispatch || !consent ? { request_sha256: r.sha256 } : { decision: choice };
    if (fp !== null) body.signer_fp = fp;
    if (dispatch && choice === "cancel") body.reason = typeof words === "string" ? words : "";
    if (!consent && !dispatch && choice !== undefined) body.choice = choice;
    if (!consent && !dispatch && words !== undefined) body.write_in = words;
    let sent = false, line = "", attempt = null;
    // the answer was posted and may have landed: settle asks the cockpit about this attempt (none for a consent or a
    // dispatch answer)
    const unsure = () => { unsureAttempt = attempt; return "unsure"; };
    unsureAttempt = null;
    say(choice !== undefined ? "Asking for the challenge for (" + choice + ")…" : "Asking for the challenge…");
    flight = true;        // from the request for the challenge: the signer picker before it posts nothing
    return call(base + steps[0], body).then((c) => {
      if (!c.ok) {
        unsigned("Not signed: " + why(c));
        // a dispatch job the cockpit says changed or moved on since it was read (its 409 in those words): nothing was
        // signed; it is read and drawn again. Any other refusal, a 409 included (an authorise that needs the laptop), is
        // a plain "not signed" in the server's words, the sheet left as it is
        return dispatch && c.status === 409 && staleReply(c) ? "changed" : "unsigned";
      }
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
      const allow = fp === null ? (k.ids || []).filter(decodable).map(unb64u) : k.rawId && decodable(k.rawId) ? unb64u(k.rawId) : null;
      if (fp !== null && k.rawId && !allow && k.local) forget(rp, fp);
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      const asked = pkGet(pk, allow, k);
      return asked.then((cred) => {
        say("Verifying the signature…");
        const answer = { authenticator_data: b64u(cred.response.authenticatorData),
          client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
        if (consent || dispatch) answer.payload = c.body.payload;
        else {
          if (cred.rawId) answer.raw_id = b64u(cred.rawId);
          // this answer's own id: what the cockpit is asked about if its reply is lost (settle)
          answer.attempt = b64u(crypto.getRandomValues(new Uint8Array(18)));
          attempt = { attempt: answer.attempt, line: line };
        }
        sent = true;          // from here on a failure may follow an answer that landed
        return call(base + steps[1], answer).then((a) => {
          if (!a.ok && a.status >= 400 && a.status < 500) return unsigned("Not signed: " + why(a));
          if (!a.ok) { say("Not sure this was signed (" + why(a) + ").", "bad"); return unsure(); }
          pkSigned(pk, k);
          // the key the cockpit's reply NAMES as the one that verified, not the one this page asked for
          const by = typeof a.body.signer_fp === "string" && a.body.signer_fp ? a.body.signer_fp : fp;
          pkSettle(rp, by);
          if (by && cred.rawId && mine(rp)[by] !== b64u(cred.rawId)) remember(rp, by, b64u(cred.rawId));
          const ns = typeof a.body.namespace === "string" && a.body.namespace ? " in " + a.body.namespace : "";
          // a consent answer's reply names its decision; a ruling's also says where its delivery stands
          // a dispatch reply names the job's state now (signed, cancelled, or where the land went)
          const what = a.body.choice || (consent && typeof a.body.decision === "string" ? a.body.decision
            : dispatch && typeof a.body.state === "string" ? a.body.state + (typeof a.body.outcome === "string" ? ", " + a.body.outcome : "") : "");
          const went = consent && a.body.delivery && typeof a.body.delivery.reason === "string" ? " · " + a.body.delivery.reason : "";
          say("signed" + (what ? " (" + what + ")" : "") + (by ? " · by " + by : "") + ns + (line ? ": " + line : "") + went, "ok");
          if (line) signing(line, "server", "Signed"); else relabel("Signed");
          $("sheet").dataset.state = "signed";
          showOutcome(what, line);
          return "signed";
        });
      }, (e) => unsigned(passkeyWhy(e)));
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) { say("Not sure this was signed (the page failed: " + name + ").", "bad"); return unsure(); }
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
  // the listing as the cockpit holds it now: the coord_* keys, {unpaired} or {error}
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
    if (headThread) head("Head card", "Extend, replace or sign this thread's head here. Nothing is signed until you tap Sign this draft.");
    else head("Drop", "Say what you want done, where it may work and how big a job it is. You sign once, with your passkey.");
    const origin = typeof got.origin === "string" ? got.origin : "";
    const rp = typeof got.rp_id === "string" && got.rp_id ? got.rp_id : window.location.hostname;
    const held = got.credentials && typeof got.credentials === "object" ? got.credentials : {};
    const signers = Array.isArray(got.coord_signers) ? got.coord_signers.filter((fp) => typeof fp === "string") : [];
    let storeId = typeof got.coord_store_id === "string" ? got.coord_store_id : null;
    const storeRead = got.coord_store_read === true;
    const drops = Array.isArray(got.coord_drops) ? got.coord_drops : [];
    if (typeof got.coord_note === "string" && got.coord_note) body.appendChild(el("p", "ext-meta", got.coord_note));
    const list = el("pre", "sheet-message", drops.map(dropLine).join("\n"));     // always made: the first delivery fills it
    list.id = "drop-recent";
    list.hidden = !drops.length;
    const grow = (t) => { const f = window.LevainCardFormat; if (f && typeof f.grow === "function") f.grow(t); };      // a programmatic value fires no input event
    // the form is laid out in two columns (one on a phone): your words, where, landing | size, what you sign, the buttons
    const grid = el("div", "env-grid");
    const colL = el("div", "env-col"), colR = el("div", "env-col");
    grid.append(colL, colR);
    body.appendChild(grid);
    const words = el("textarea", "");
    words.id = "drop-words";
    words.rows = 4;
    words.placeholder = "what you want done";
    words.setAttribute("aria-label", "your words");
    for (const [k, v] of [["autocomplete", "off"], ["autocapitalize", "sentences"], ["autocorrect", "on"],
                          ["spellcheck", "true"], ["inputmode", "text"], ["enterkeyhint", "enter"], ["name", "dropwords"]]) words.setAttribute(k, v);
    // words typed in the cockpit's drop box and handed over by "Add an envelope" (dashboard_core.js openDropSheet): read once
    try {
      const pre = window.sessionStorage.getItem("levain-drop-prefill");
      if (pre) { words.value = pre; grow(words); window.sessionStorage.removeItem("levain-drop-prefill"); }
    } catch (e) { /* the sheet opens empty */ }
    const keyBox = el("div", "env-keybox");
    keyBox.hidden = true;
    const acts = $("sheet-actions");
    acts.hidden = false;
    const send = el("button", "dev-btn", "Send words only");
    send.type = "button";
    send.id = "drop-send";
    const sign = el("button", "dev-btn dev-primary", "Sign with your passkey");
    sign.type = "button";
    sign.id = "drop-sign";
    sign.disabled = true;
    acts.append(sign, send);
    // beside the buttons: why Sign is off, what each button does, and what comes after signing
    const signWhy = el("p", "env-hint env-why");
    signWhy.id = "drop-sign-why";
    const buttonNotes = el("div", "env-notes");
    const sendNote = el("p", "env-hint", "Send words only: sends your words with no limits attached.");
    sendNote.id = "drop-send-note";
    send.setAttribute("aria-describedby", sendNote.id);
    buttonNotes.append(sendNote, el("p", "env-hint", "Sign, then the coordinator may start work inside these limits."));
    let oid = newOutboxId();       // held for these words: a resend after a lost reply files once, never twice
    let cannot = "";
    const why0 = !storeRead ? "still reading which Levain answers here; reload" : "";
    // The store this sheet is bound to. Every reply that names a store passes through here: a reply may SET the identity only
    // while the page holds none, and a different one named while one is held stops the sheet (it is never re-bound: that
    // would sign into, or send to, a store the person never opened the sheet for).
    const MOVED = "This sheet was opened for a different Levain store than the one answering now. Reload the page to see the current store; nothing more can be signed or sent here.";
    let moved = "";
    const otherStore = (id) => typeof id === "string" && storeId !== null && id !== storeId;
    const refuseOther = () => { moved = MOVED; form.stop(MOVED); };
    // the fields, the spec and the review are the shared form's (envelope_form.js); this page passes how a review is asked for
    const form = window.LevainEnvelopeForm.create({
      words: words, wordsGroup: true, left: colL, right: colR, lockRoots: [grid], repos: got.coord_repos, sign: sign, signWhy: signWhy, why: why,
      storeId: () => storeId,
      // asked only after a review sent with no store id: a store identity the page already holds is never replaced
      refreshStore: () => readDrops().then((g) => {
        if (!g || typeof g.coord_store_id !== "string") return;
        if (storeId === null) storeId = g.coord_store_id;
        else if (otherStore(g.coord_store_id)) refuseOther();
      }),
      blocked: () => moved || (cannot ? "Envelope: " + cannot : why0),
      review: (req) => {
        const k = keyNow();
        return post("envelope-review", { words: req.words, outbox_id: oid, spec: req.spec, signer_fp: k.fp, store_id: storeId }).then((c) => {
          // a fresh install's review made the store: only then does a null identity take the reply's
          if (c.ok && storeId === null && c.body.store_created === true && typeof c.body.store_id === "string") storeId = c.body.store_id;
          else if (c.ok && otherStore(c.body.store_id)) {
            refuseOther();
            return { ok: false, status: 409, body: { error: "other_store", message: MOVED }, k: k };
          }
          c.k = k;
          return c;
        });
      },
    });
    const bar = el("div", "env-bar");      // the reason and the buttons, kept together (and held at the bottom of a phone-width page)
    bar.append(signWhy, acts);
    form.foot.append(keyBox, bar, buttonNotes, $("sheet-note"));
    // the sheet is a live region for its progress lines; the form inside it is not, or "Reading…" would be read out on every keystroke (the form announces a settled review itself)
    grid.setAttribute("aria-live", "off");
    $("sheet-note").setAttribute("aria-live", "polite");
    body.appendChild(list);
    if (words.value) grow(words);      // sized once it is in the page: a box that is not laid out yet measures nothing
    $("sheet").dataset.state = "open";
    let working = false;
    let pending = null;            // a signed drop whose reply did not come back: sent again unchanged, never re-signed
    const queue = (ms) => form.queue(ms);
    const changed = () => form.changed();
    let ksel = null;               // the signing key's picker, when this device holds several
    const lock = (on) => {
      working = on;
      send.disabled = on;
      if (ksel) ksel.disabled = on;          // the key a signing under way uses is the one shown
      words.disabled = on;
      form.lock(on);
    };
    // a drop whose answer is not known is kept in this browser BEFORE it is posted, so a
    // closed sheet loses nothing: the next open sends it again, unchanged (the same outbox id files once), and it leaves
    // only on a definite answer
    // one record per outbox id, so a new drop never overwrites one whose answer is unknown (the cockpit's hero keeps the same way)
    const PEND_KEY = "levain-drop-pending:" + window.location.hostname;
    const pendKey = (id) => PEND_KEY + ":" + id;
    const keep = (req) => { try { window.localStorage.setItem(pendKey(req.outbox_id), JSON.stringify(req)); } catch (e) { /* kept in this visit only */ } };
    const drop0 = (req) => {
      try { window.localStorage.removeItem(pendKey(req.outbox_id)); } catch (e) { /* nothing kept */ }
      try {
        const old = JSON.parse(window.localStorage.getItem(PEND_KEY) || "null");      // a record an earlier build kept under the bare key
        if (old && old.outbox_id === req.outbox_id) window.localStorage.removeItem(PEND_KEY);
      } catch (e) { /* a malformed legacy value is left alone */ }
    };
    // the newest unresolved one; the others stay kept, for the cockpit's hero
    const kept = () => {
      const seen = new Map();
      let n = 0;
      try { n = window.localStorage.length; } catch (e) { return null; }
      for (let i = 0; i < n; i++) {
        try {
          const k = window.localStorage.key(i);
          if (k !== PEND_KEY && !(k && k.indexOf(PEND_KEY + ":") === 0)) continue;
          const v = JSON.parse(window.localStorage.getItem(k) || "null");      // a malformed value is skipped, never the scan
          if (v && typeof v === "object" && typeof v.outbox_id === "string" && typeof v.words === "string" && !seen.has(v.outbox_id)) seen.set(v.outbox_id, v);
        } catch (e) { /* this entry only */ }
      }
      const all = Array.from(seen.values());
      all.sort((a, b) => String(b.sent_at || "").localeCompare(String(a.sent_at || "")));
      return all.length ? all[0] : null;
    };
    let signedLine = "";
    const done = (a, plain, req) => {
      const d = a.body.drop || {};
      const e = a.body.envelope;
      // a fresh store's first drop made it: its identity is held before the controls unlock, so a fast second send names it.
      // A drop answered by a store other than the one held (a kept drop sent again after the store changed) is reported as
      // delivered there, and the sheet stops: it is never re-bound to the store that answered
      const other = otherStore(a.body.store_id);
      if (storeId === null && typeof a.body.store_id === "string") storeId = a.body.store_id;
      if (other) refuseOther();
      busy = false;
      lock(false);
      oid = newOutboxId();
      pending = null;
      words.value = "";
      acts.replaceChildren(sign, send);
      queue(0);
      const msg = (a.body.created === false ? "already delivered" : "delivered") + " as " + String(d.drop_id || "") + " · by "
        + (plain ? "words only" : e && e.taken ? "envelope " + e.envelope_id + " taken" : "envelope NOT taken: " + String((e && e.refused) || "no envelope in the reply"));
      if (other) say(msg + "; but by a different Levain store than this sheet was opened for. Reload the page to see the current store; nothing more can be signed or sent here.", "bad");
      else say(msg, plain || (e && e.taken) ? "ok" : "bad");
      // the server accepted this drop (a 2xx): the message names the exact words it accepted, so the cockpit's box lets go of
      // them only when it still holds those very words
      tell("signed", req && typeof req.words === "string" ? { dropDelivered: true, words: req.words } : null);
      return readDrops().then((g) => {
        if (g.unpaired || g.error) return;
        const dd = Array.isArray(g.coord_drops) ? g.coord_drops : [];
        list.textContent = dd.map(dropLine).join("\n");
        list.hidden = !dd.length;
        // the store this sheet is bound to is kept: a listing that names another one never re-binds it, and stops the sheet
        if (storeId === null && typeof g.coord_store_id === "string") storeId = g.coord_store_id;
        else if (otherStore(g.coord_store_id) && !moved) refuseOther();
      });
    };
    const refusal = (a, nothing) => say(nothing + ": " + why(a), "bad");
    const post = (action, req) => call("/cockpit/coord/" + action, req);
    const wordsOf = () => { const w = words.value; if (!w.trim()) { say("type your words first", "bad"); return null; } return w; };
    // the normal controls again, the words kept: after a refusal that is final (nothing was delivered with this request)
    const restore = () => {
      unsure = false;
      busy = false;
      pending = null;
      acts.replaceChildren(sign, send);
      $("sheet").dataset.state = "open";
      lock(false);
      queue(0);
    };
    const sendDrop = (req) => {
      const signed = req.envelope != null;
      keep(req);
      lock(true);
      const unsureNow = (why1) => {
        // no whole reply, or the cockpit's own fault: it may have landed. The same drop (same outbox id and envelope) is sent
        // again on a tap, and is kept for the next open
        pending = req;
        if (signed) { relabel("Not confirmed"); ended(); }
        unsure = true;
        say("Not sure the " + (signed ? "signed " : "") + "drop was delivered (" + why1 + "); send it again, unchanged, to find out.", "bad");
        const again = el("button", "dev-btn", signed ? "Send the signed drop again" : "Send the same drop again");
        again.type = "button";
        again.addEventListener("click", () => { again.remove(); sendDrop(pending); });
        acts.replaceChildren(again);
      };
      return post("drop", req).then((a) => {
        if (a.ok) {
          drop0(req);
          unsure = false;
          if (signed) { ended(); if (signedLine) signing(signedLine, "server", "Signed"); else relabel("Signed"); $("sheet").dataset.state = "signed"; }
          return done(a, !signed, req);
        }
        if (a.status >= 400 && a.status < 500) {
          // refused: nothing was delivered with this request (a stale signature is signed again from the review); the
          // controls come back with the words, whether this was the first send or a retry
          drop0(req);
          if (signed) { relabel("Not signed"); ended(); }
          restore();
          return refusal(a, signed ? "the signed drop was not delivered; review and sign again, or send the words alone" : "the drop was not delivered");
        }
        return unsureNow(why(a));
      }, (e) => unsureNow("the page failed: " + ((e && e.name) || "error")));
    };
    send.addEventListener("click", () => {
      if (working) return;
      const w = wordsOf();
      if (w === null) return;
      if (why0) return say(why0, "bad");
      if (moved) return say(moved, "bad");
      sendDrop({ words: w, outbox_id: oid, sent_at: stamp(), store_id: storeId });
    });
    // what the envelope needs, said before any tap: never a ceremony that cannot verify
    cannot = !usable ? "this browser cannot use a passkey here (needs https)"
      : origin !== window.location.origin ? "this page cannot use your passkey: sign on " + (origin || "the cockpit's https page")
      : typeof got.elsewhere === "string" && got.elsewhere ? got.elsewhere
      : !signers.length ? (got.coord_signer_note || "no passkey enrolled for " + rp + " can sign an envelope") : "";
    if (cannot) { const n = el("p", "ext-meta", "Envelope: " + cannot); n.id = "drop-sign-note"; form.foot.insertBefore(n, bar); sign.disabled = true; }

    // ---- the passkey this device signs with: the one it holds; a choice only when it holds several ----
    const localKeys = mine(rp);
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(localKeys, fp));
    const pool = known.length ? known : signers;
    let keyFp = pool[0];
    let onKey = () => changed();     // a head thread puts its own here: the drop form is not on that page
    if (pool.length > 1) {
      ksel = el("select");
      ksel.id = "drop-key";
      ksel.setAttribute("aria-label", "the passkey to sign with");
      for (const fp of pool) { const o = el("option", "", fp.slice(0, 18) + "…"); o.value = fp; o.title = fp; ksel.appendChild(o); }
      ksel.addEventListener("change", () => { keyFp = ksel.value; onKey(); });
      keyBox.append(el("span", "env-flabel", "Signing key"), ksel);
      keyBox.hidden = false;
    }
    const keyNow = () => {
      const local = mine(rp);
      return Object.prototype.hasOwnProperty.call(local, keyFp) ? { fp: keyFp, rawId: local[keyFp], local: true }
        : { fp: keyFp, rawId: held[keyFp] && typeof held[keyFp].credential_id === "string" ? held[keyFp].credential_id : null, local: false };
    };

    // the form (envelope_form.js) holds the review the button signs; a change to the form retires it
    sign.addEventListener("click", () => {
      const c = form.current();
      if (working || cannot || !c) return;
      const k = c.reply.k;
      lock(true);
      busy = true;
      flight = true;
      say("Confirm with your passkey to sign…");
      // a refusal before any passkey tap: nothing was signed; the review is asked for again when the server no longer holds it
      const stop1 = (m, again) => { relabel("Not signed"); ended(); say(m, "bad"); busy = false; lock(false); if (again) queue(0); };
      post("envelope-challenge", { review_id: c.id }).then((ch) => {
        if (!ch.ok) return stop1("Not signed: " + why(ch) + "; nothing was signed.", ch.status === 409);
        if (otherStore(ch.body.store_id)) { stop1("Not signed: the challenge came from a different Levain store than this sheet was opened for; nothing was signed."); return refuseOther(); }
        if (!decodable(ch.body.challenge)) return stop1("The cockpit sent a malformed challenge; nothing was signed.");
        const line = typeof ch.body.line === "string" ? ch.body.line : "";
        if (line) signing(line, "server", "Signing");
        say("Confirm with your passkey to sign…" + (line ? " " + line : ""));
        const allow = k.rawId && decodable(k.rawId) ? unb64u(k.rawId) : null;
        const pk = { challenge: unb64u(ch.body.challenge), rpId: ch.body.rp_id, userVerification: "required", timeout: 60000 };
        let sent = false;
        return pkGet(pk, allow, k).then((cred) => {
          say("Sending the signed drop…");
          const envelope = { body_text: c.body_text, payload: ch.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
                             client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
          sent = true;
          pkSigned(pk, k);
          if (cred.rawId && mine(rp)[k.fp] !== b64u(cred.rawId)) remember(rp, k.fp, b64u(cred.rawId));
          signedLine = line;
          return sendDrop({ words: c.words, outbox_id: oid, sent_at: stamp(), store_id: storeId, envelope: envelope });
        }, (e) => stop1(passkeyWhy(e) + "; nothing was signed."))
          .catch((e) => {
            const name = (e && e.name) || "error";
            if (sent) { relabel("Not confirmed"); ended(); say("Not sure the signed drop was delivered (the page failed: " + name + ").", "bad"); return undefined; }
            return stop1("The page failed (" + name + "); nothing was signed.");
          });
      }, () => stop1("The page failed (the cockpit did not answer); nothing was signed."));
    });
    // ---- the head card of a thread (CH-H1 H1-3): opened by `#thread=<id>`, drawn from the server's draft-cards ----
    // A LAPSED card offers a draft of an extension, a REVOKED card a draft of a replacement (each writes an UNSIGNED draft:
    // it grants nothing), an ENVELOPE card offers Review and Withdraw. A review draws the diffs the server's builder
    // returned (never the card's own), and Sign mints the challenge at the tap: draft-challenge, the passkey, draft-accept.
    // The words that are signed are the server's (`body_text`); this page only puts the diffs in plain words.
    // A head thread REPLACES the drop form (it is another act, a drop), so the form is taken out of the page at the end of
    // this block, and this ceremony is independent of the drop sheet's own state: it never touches `busy`, `flight`,
    // `ended()`, `signing()`, `relabel()` or `#sheet-signing`; every line it draws is its own element, below.
    if (headThread) {
      const sec = el("section", "head-card");
      sec.id = "head-card";
      body.prepend(sec);
      const line = (id) => { const p = el("p", "env-hint head-line"); p.id = id; p.setAttribute("role", "status"); p.hidden = true; return p; };
      const put = (p, text, bad) => { p.textContent = text; p.hidden = !text; p.dataset.kind = bad ? "bad" : ""; };
      const cardsBox = el("div", "head-cards");
      const hsigning = line("head-signing");   // the line the server says is being signed
      const hnote = line("head-note");         // the outcome of the last action
      const hunsure = line("head-unsure");     // an accept whose outcome is not known: its own element, no read ever overwrites it
      const hreadl = line("head-read");        // a card read: its progress or its failure, beside the lines above, never instead of them
      const hdet = el("details", "head-detail");
      hdet.append(el("summary", "", "Details"));
      const hdetText = el("p", "env-hint", "Thread " + headThread);
      hdet.appendChild(hdetText);
      sec.append(el("p", "env-label head-title", "Head card"), cardsBox, hsigning, hnote, hunsure, hreadl, hdet);
      let hbusy = false;          // a request or the passkey ceremony of THIS card is under way (every verb guards itself with it)
      let hreview = null;         // the review the Sign control signs: {id, body_text, fp, draft_id, revision}
      let hsignText = "";
      const hsay = (text, bad) => put(hnote, text, bad);
      const hline = (label) => { if (hsignText) put(hsigning, label + ": " + hsignText); };
      const hmoved = () => { refuseOther(); hsay(MOVED, true); };
      // a refusal because the store answering is not the one this sheet was opened for: the sheet stops, it is never a stale card
      const hother = (r) => !!r && r.status === 409 && !!r.body && r.body.error === "other_store";
      const hstop = () => moved || (!storeId ? "this Levain has no coordinator store to read a head card from yet" : "");
      const hbtn = (label, id, fn, primary) => {
        const b = el("button", primary ? "dev-btn dev-primary" : "dev-btn", label);
        b.type = "button";
        b.id = id;      // ids serve the tests; the page scopes by card (acts / box below), and a card list holds one card
        b.addEventListener("click", fn);
        return b;
      };
      const KIND = { ENVELOPE: "Draft to sign", REVOKED: "Revoked", LAPSED: "Lapsed" };
      const FIELD = { where: "Where it may work", budget: "Size", stop_at: "Stops", lands: "Landings", pushes: "Pushes", publishes: "Publishes" };
      const BUDGET = { tool_calls: "tool calls", tokens: "tokens", dollars_cents: "dollars", hours: "hours" };
      const EFFECT = { lands: "landing", pushes: "push", publishes: "publish" };
      const VERB = { "draft-rule": "Drafting", "draft-withdraw": "Withdrawing", "draft-review": "Reviewing" };
      const unset = (v) => v === null || v === undefined;
      const budgetVal = (k, v) => (unset(v) ? "not set" : k === "dollars_cents" ? "$" + (Number(v) / 100).toFixed(2) : String(v));
      const hfield = (v) => (unset(v) ? "not set" : typeof v === "string" ? v : JSON.stringify(v));
      // one field of the draft in plain words; a shape this does not know is shown as it came
      const plainField = (name, v) => {
        if (name === "where" && Array.isArray(v)) {
          return v.length ? v.map((w) => "folder " + String(w && w.repo) + (w && Array.isArray(w.paths) && w.paths.length ? " (" + w.paths.join(", ") + ")" : "")).join("; ") : "nowhere";
        }
        if (name === "budget" && v && typeof v === "object" && !Array.isArray(v)) {
          const parts = Object.keys(BUDGET).filter((k) => !unset(v[k])).map((k) => (k === "dollars_cents" ? budgetVal(k, v[k]) : v[k] + " " + BUDGET[k]));
          return parts.length ? parts.join(", ") : "not set";
        }
        if (Object.prototype.hasOwnProperty.call(EFFECT, name) && Array.isArray(v)) return v.length ? v.length + " " + EFFECT[name] + (v.length === 1 ? "" : "s") : "none";
        return hfield(v);
      };
      const has = (card, a) => Array.isArray(card.actions) && card.actions.indexOf(a) >= 0;
      // the diffs as the builder returned them, one plain line each; the exact text is in the disclosure below them
      const diffLines = (a) => {
        const out = [];
        const d = a && a.diff && typeof a.diff === "object" ? a.diff : {};
        const w = d.where || {};
        for (const p of w.added || []) out.push("adds where it may work: " + p);
        for (const p of w.removed || []) out.push("removes where it may work: " + p);
        for (const kind of Object.keys(d.effects || {})) {
          const e = d.effects[kind] || {};
          const noun = EFFECT[kind] || kind;
          for (let i = 0; i < (e.added || []).length; i++) out.push("adds a " + noun);
          for (let i = 0; i < (e.removed || []).length; i++) out.push("removes a " + noun);
          for (let i = 0; i < (e.changed || []).length; i++) out.push("changes a " + noun + ": same target, other limits");
        }
        for (const k of Object.keys(d.budget || {})) out.push((BUDGET[k] || k) + ": from " + budgetVal(k, d.budget[k][0]) + " to " + budgetVal(k, d.budget[k][1]));
        if (Array.isArray(d.stop_at)) out.push("stops: from " + hfield(d.stop_at[0]) + " to " + hfield(d.stop_at[1]));
        return out;
      };
      const hcards = (cards) => {
        hreview = null;
        cardsBox.replaceChildren();
        if (!cards.length) cardsBox.appendChild(el("p", "env-hint", "This thread shows no head card: nothing to extend, replace or sign."));
        else for (const card of cards) cardsBox.appendChild(hdrawCard(card));
      };
      // read the card; it answers with the cards (drawn) or null (said in `hreadl`, never in the outcome lines)
      const hrefresh = () => {
        const s = hstop();
        if (s) { put(hreadl, s, true); return Promise.resolve(null); }
        put(hreadl, "Reading the head card…");
        return post("draft-cards", { thread_id: headThread, store_id: storeId }).then((c) => {
          if (hother(c)) { put(hreadl, ""); hmoved(); return null; }
          if (!c.ok) { put(hreadl, "The head card could not be read: " + why(c), true); return null; }
          if (otherStore(c.body.store_id)) { hmoved(); return null; }
          if (!Array.isArray(c.body.cards)) { put(hreadl, "The head card could not be read: the cockpit sent no list of cards.", true); return null; }
          try { hcards(c.body.cards); } catch (e) { put(hreadl, "The head card could not be drawn (" + ((e && e.name) || "error") + ").", true); return null; }
          put(hreadl, "");
          return c.body.cards;
        }, () => { put(hreadl, "The head card could not be read (the cockpit did not answer).", true); return null; })
          .catch((e) => { put(hreadl, "The head card could not be read (the page failed: " + ((e && e.name) || "error") + ").", true); return null; });
      };
      // the cards a committed write answered with: a list is drawn; anything else means UNKNOWN (the write stands), never "no card"
      const hset = (b) => {
        if (Array.isArray(b.cards)) { hcards(b.cards); return Promise.resolve(); }
        put(hreadl, typeof b.cards_note === "string" && b.cards_note ? b.cards_note : "The cockpit did not send the card back; reading it again.", true);
        return hrefresh().then(() => undefined);
      };
      // a card the server no longer holds as drawn (409 draft_stale, a changed thread): read again, say so
      const hstale = (r) => { hsay("The card changed since it was shown (" + why(r) + "); read again, nothing was signed."); return hrefresh(); };
      const hcall = (action, req, ok) => {
        if (hbusy) return undefined;
        const s = hstop();
        if (s) return hsay(s, true);
        hbusy = true;
        const verb = VERB[action] || action;
        // hbusy is held until the whole answer settles, the card read it may ask for included: a redraw that lands after
        // it was released would wipe a control drawn in between
        return post(action, Object.assign({ store_id: storeId }, req)).then((r) => {
          if (r.ok && otherStore(r.body.store_id)) return hmoved();
          if (r.ok) return ok(r.body);
          if (hother(r)) return hmoved();
          if (r.status === 409) return hstale(r);
          // no whole reply: the write may have landed (another tap would write another revision), so the card is read
          if (r.status === 0) {
            hsay(verb + ": no answer came back (" + why(r) + "), so it is not known whether it was done; reading the card again.", true);
            return hrefresh();
          }
          return hsay(verb + " was refused: " + why(r), true);
        }, () => { hsay(verb + ": the cockpit did not answer.", true); })
          .catch((e) => { hsay(verb + ": the page failed (" + ((e && e.name) || "error") + ").", true); })
          .finally(() => { hbusy = false; });
      };
      const hrule = (card, rule) => {
        const head = card.head && typeof card.head.envelope_id === "string" ? card.head.envelope_id : "";
        if (!head) return hsay("This card names no head to " + (rule === "renew" ? "extend" : "replace") + ".", true);
        return hcall("draft-rule", { thread_id: headThread, rule: rule, head: head }, (b) => {
          hsay("A draft is written. It grants nothing until you review it and sign.");
          return hset(b);
        });
      };
      const hwithdraw = (d) => hcall("draft-withdraw", { draft_id: d.draft_id, revision: d.revision }, (b) => {
        hsay("Draft withdrawn.");
        return hset(b);
      });
      const hreviewTap = (card, box, acts) => {
        const d = card.detail || {};
        if (cannot) return hsay("Envelope: " + cannot, true);
        const k = keyNow();
        return hcall("draft-review", { draft_id: d.draft_id, revision: d.revision, digest: d.digest, signer_fp: k.fp }, (b) => {
          if (!b.review_id || typeof b.body_text !== "string") return hsay("The cockpit sent no review to sign; nothing was signed.", true);
          hreview = { id: b.review_id, body_text: b.body_text, fp: k.fp, draft_id: d.draft_id, revision: d.revision };
          const old = box.querySelector(".head-diffs");
          if (old) old.remove();
          const dbox = el("div", "env-opt head-diffs");
          dbox.id = "head-diffs";
          const t = el("div", "env-opt-t");
          const ag = b.against || {};
          // the widest claim first: a grant bigger than the head's, or no head left to extend; the two never both appear
          if (ag.base_kind === "revoked") t.appendChild(el("div", "env-opt-name head-new", "This is a new grant, not an extension of the old head"));
          else if (ag.wider) t.appendChild(el("div", "env-opt-name head-wider", "This signature grants MORE than the head it replaces"));
          for (const l of Array.isArray(b.view) ? b.view : []) t.appendChild(el("div", "env-hint", String(l)));
          t.appendChild(el("div", "env-opt-name", "What this signature changes"));
          const lines = diffLines(ag);
          const since = Array.isArray(b.since_parent) ? b.since_parent.map((x) => "since the last draft, " + (FIELD[x.field] || x.field) + ": " + plainField(x.field, x.before) + " -> " + plainField(x.field, x.after)) : [];
          const members = (Array.isArray(ag.members_added) && ag.members_added.length) || (Array.isArray(ag.members_removed) && ag.members_removed.length);
          t.appendChild(el("pre", "sheet-message", lines.concat(since).join("\n") || (members ? "unchanged except members" : "no change from the head")));
          const det = el("details", "");
          det.appendChild(el("summary", "", "What exactly gets signed"));
          det.appendChild(el("pre", "sheet-message", b.body_text));
          t.appendChild(det);
          dbox.appendChild(t);
          acts.before(dbox);
          const sg = hbtn("Sign this draft", "head-sign", hsign, true);
          sg.dataset.headSign = "1";
          const prior = acts.querySelector("[data-head-sign]");
          if (prior) prior.remove();
          acts.prepend(sg);
          hsay("Read the changes above, then sign this draft with your passkey. The challenge is made when you tap.");
        });
      };
      // the envelope id the reviewed body names (the body is the envelope's canonical JSON); "" when it cannot be read
      const bodyEnvelope = (text) => {
        try { const b = JSON.parse(text); return b && typeof b.envelope_id === "string" ? b.envelope_id : ""; } catch (e) { return ""; }
      };
      // another signing key chosen after a review: that review was made for the old key, so it is retired here, not at the tap
      onKey = () => {
        if (!hreview) return;
        hreview = null;
        for (const n of document.querySelectorAll("#head-card [data-head-sign], #head-card .head-diffs")) n.remove();
        hsay("The signing key changed; review the changes again to sign with it. Nothing was signed.", true);
      };
      if (ksel) sec.insertBefore(keyBox, cardsBox);     // the key picker lives on the card: the drop form leaves the page below
      const hsign = () => {
        const r = hreview;
        if (!r || hbusy) return;
        const k = keyNow();
        if (k.fp !== r.fp) return hsay("The signing key changed since this review; review again. Nothing was signed.", true);
        const s = hstop();
        if (s) return hsay(s, true);
        hbusy = true;
        if (ksel) ksel.disabled = true;       // the key this signing uses is the one shown
        put(hunsure, "");
        // a line the server gave for an earlier attempt is never shown beside this one
        hsignText = "";
        put(hsigning, "");
        const stop1 = (m, again) => { hline("Not signed"); hsay(m, true); return again ? hrefresh() : undefined; };
        // an accept whose outcome is not known: said in its own line, then settled by reading the card. Only what the card
        // SHOWS is said: signed only when a card names this signature's envelope as the head; a draft that is simply gone
        // may have been signed, withdrawn or replaced elsewhere, and the page says that, not a guess
        const unsure = (why1) => {
          hreview = null;
          hline("Not confirmed");
          const noSign = document.querySelector("#head-card [data-head-sign]");
          if (noSign) noSign.remove();
          const base = "Not sure this was signed (" + why1 + ").";
          const eid = bodyEnvelope(r.body_text);
          put(hunsure, base + " Reading the card to see…", true);
          return hrefresh().then((cards) => {
            if (cards === null) { put(hunsure, base + " The card could not be read either, so it is not known: look at this thread before signing again.", true); return; }
            const signed = !!eid && cards.some((c) => c && c.head && c.head.envelope_id === eid);
            const still = !signed && cards.some((c) => c && c.kind === "ENVELOPE" && c.detail && c.detail.draft_id === r.draft_id);
            put(hunsure, base + " " + (signed ? "Signed: the card shows this signature's envelope as the thread's head."
              : still ? "Not signed: the card still shows the draft."
              : "The draft is no longer on the card: it may have been signed, or withdrawn or replaced elsewhere. Look at this thread before signing again."), !signed);
            hline(signed ? "Signed" : still ? "Not signed" : "Not confirmed");
            if (signed) hdetText.textContent = "Thread " + headThread + " · envelope " + eid;
          });
        };
        hsay("Confirm with your passkey to sign…");
        // the challenge is minted now, at the tap: a draft that went stale while this was read is refused before any byte
        return post("draft-challenge", { review_id: r.id }).then((ch) => {
          if (hother(ch)) { hline("Not signed"); return hmoved(); }
          if (!ch.ok) return stop1("Not signed: " + why(ch) + "; nothing was signed.", ch.status === 409);
          if (otherStore(ch.body.store_id)) { refuseOther(); return hsay("Not signed: the challenge came from a different Levain store than this sheet was opened for; nothing was signed. " + MOVED, true); }
          if (!decodable(ch.body.challenge)) return stop1("The cockpit sent a malformed challenge; nothing was signed.");
          hsignText = typeof ch.body.line === "string" ? ch.body.line : "";
          hline("Signing");
          hsay("Confirm with your passkey to sign…" + (hsignText ? " " + hsignText : ""));
          const allow = k.rawId && decodable(k.rawId) ? unb64u(k.rawId) : null;
          const pk = { challenge: unb64u(ch.body.challenge), rpId: ch.body.rp_id, userVerification: "required", timeout: 60000 };
          let sent = false;
          return pkGet(pk, allow, k).then((cred) => {
            hsay("Sending the signature…");
            // the key set is the server's own (draft_sign `_need`): the review id and the signed parts, no store id
            const req = { review_id: r.id, body_text: r.body_text, payload: ch.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
                          client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
            sent = true;
            pkSigned(pk, k);
            if (cred.rawId && mine(rp)[k.fp] !== b64u(cred.rawId)) remember(rp, k.fp, b64u(cred.rawId));
            return post("draft-accept", req).then((a) => {
              if (a.ok) {
                hline("Signed");
                const id = String((a.body.envelope && a.body.envelope.envelope_id) || "");
                hsay(a.body.created === false ? "Already signed." : "Signed.");
                if (id) hdetText.textContent = "Thread " + headThread + " · envelope " + id;
                return hset(a.body);
              }
              if (hother(a)) { hline("Not signed"); return hmoved(); }
              // a 503 is a definite answer: every one the cockpit gives is before the write or a write that did not commit
              // ("503 a fault on this machine (nothing written)", the busy and closing gates), and the
              // server keeps the review for a machine fault, so the same Sign button may be tapped again
              if (a.status === 503) return stop1("Not signed: " + why(a) + ". The cockpit did not sign it; tap Sign again when it answers.", false);
              // any 4xx spends the review (and may mean the card moved): the card is read again and the Sign button goes with it
              if (a.status >= 400 && a.status < 500) return stop1("Not signed: " + why(a) + ".", true);
              return unsure(why(a));
            }, () => unsure("the cockpit did not answer"));
          }, (e) => stop1(passkeyWhy(e) + "; nothing was signed."))
            .catch((e) => {
              const name = (e && e.name) || "error";
              if (sent) return unsure("the page failed: " + name);
              return stop1("The page failed (" + name + "); nothing was signed.");
            });
        }, () => stop1("The page failed (the cockpit did not answer); nothing was signed."))
          .catch((e) => { hsay("The page failed (" + ((e && e.name) || "error") + "); look at the card before signing again.", true); })
          // released only when the ceremony and any card read it asked for have settled (a late redraw never lands after it)
          .finally(() => { hbusy = false; if (ksel) ksel.disabled = false; });
      };
      function hdrawCard(card) {
        const box = el("div", "head-card-box");
        box.dataset.kind = String(card.kind);
        box.appendChild(el("p", "env-label", (KIND[card.kind] || String(card.kind)) + ": " + String(card.line || "")));
        const d = card.detail || {};
        const acts = el("div", "pair-actions");
        acts.id = "head-card-actions";
        const draftOnly = () => box.appendChild(el("p", "env-hint", "This only writes a draft. Nothing is signed until you review it and sign."));
        if (card.kind === "LAPSED" && has(card, "extend")) { draftOnly(); acts.appendChild(hbtn("Draft an extension", "head-extend", () => hrule(card, "renew"), true)); }
        if (card.kind === "REVOKED" && has(card, "replace")) { draftOnly(); acts.appendChild(hbtn("Draft a replacement", "head-replace", () => hrule(card, "replace"), true)); }
        if (card.kind === "ENVELOPE") {
          const fl = Array.isArray(d.fields) ? d.fields.map((f) => (FIELD[f.field] || f.field) + ": " + plainField(f.field, f.value) + (Array.isArray(f.cites) && f.cites.length ? " (source given)" : " (no source given)")) : [];
          if (fl.length) box.appendChild(el("pre", "sheet-message", fl.join("\n")));
          if (typeof d.cite_note === "string" && d.cite_note) box.appendChild(el("p", "env-hint", d.cite_note));
          if (has(card, "sign")) acts.appendChild(hbtn("Review the changes", "head-review", () => hreviewTap(card, box, acts), false));
          if (has(card, "withdraw")) acts.appendChild(hbtn("Withdraw this draft", "head-withdraw", () => hwithdraw(d), false));
        }
        box.appendChild(acts);
        return box;
      }
      hrefresh();
      // the drop form is not this page's business here: out of the page, and its progress line (a shared element) put back
      const noteHome = $("sheet-note");
      if (noteHome) acts.after(noteHome);
      grid.remove();
      list.remove();
      acts.replaceChildren();
      acts.hidden = true;
      return;
    }
    form.start();
    // a drop kept from an earlier visit whose answer never came: asked again now, unchanged (the outbox id files it once)
    const left = kept();
    if (left) {
      words.value = left.words;
      grow(words);
      oid = left.outbox_id;
      say("A drop from earlier was not confirmed; asking whether it landed…");
      sendDrop(left);
    }
  }
})();
