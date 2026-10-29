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

  // the token arrives once in the link's fragment; it is kept for this tab and taken out of the address bar.
  // `sign=<id>` in the fragment (levain.consent.sign.deep_link) opens the page on that sign request's card
  let token = null;
  const fm = /(?:^#|&)sign=([A-Za-z0-9_-]{8,64})(?:&|$)/.exec(location.hash);
  let focus = fm ? fm[1] : "";
  const m = /(?:^#|&)phone_token=([^&]+)/.exec(location.hash);
  if (m) { token = decodeURIComponent(m[1]); store.set("sessionStorage", "levain-phone-token", token); }
  else token = store.get("sessionStorage", "levain-phone-token");
  // `choice=<key>` beside it is the option he already tapped on his phone (deep_link's choice), and `v=` the version of
  // the request he tapped it on (the first 16 hex of the listing's sha256): page state only. The linked card offers
  // that option as one "Sign (<key>)" tap, built from the server's listing, only while the listed version is the one
  // he tapped; the bytes signed are still the server's, and anything else draws nothing but a line. A `choice=` with
  // no `sign=` is never a pick: it is taken out of the address bar and ignored
  const pm = /(?:^#|&)choice=([^&]*)(?:&|$)/.exec(location.hash);
  const vm = fm ? /(?:^#|&)v=([0-9a-f]{16})(?:&|$)/.exec(location.hash) : null;
  let pick = "";
  const pickV = vm ? vm[1] : "";
  if (fm && pm) { try { pick = decodeURIComponent(pm[1]); } catch (e) { pick = pm[1]; } }
  if (m || fm || pm) history.replaceState(null, "", location.pathname);
  // a link opened on a page already showing (a fragment-only navigation does not reload it): read it afresh
  window.addEventListener("hashchange", () => {
    if (/(?:^#|&)(?:sign|phone_token)=/.test(location.hash)) location.reload();
    else if (/(?:^#|&)choice=/.test(location.hash)) history.replaceState(null, "", location.pathname);
  });

  // what the platform's passkey prompt is called to the person: Face ID on an iPhone or iPad (the same test phone_app.js
  // uses), the neutral "your screen lock" elsewhere (Android's sheet is the screen lock or a fingerprint)
  const ua = navigator.userAgent || "";
  const isIos = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  const unlock = isIos ? "Face ID" : "your screen lock";
  const usable = window.isSecureContext === true && typeof window.PublicKeyCredential === "function"
    && !!(navigator.credentials && navigator.credentials.get);
  let rpId = location.hostname;
  // which enrolled passkey THIS browser made: fingerprint -> credential id, so the OS prompt is asked for that one
  const mapKey = () => "levain-passkeys:" + rpId;
  const mine = () => { try { return JSON.parse(store.get("localStorage", mapKey()) || "{}") || {}; } catch (e) { return {}; } };
  // the ONE rule for a passkey prompt that did not sign, said by every navigator.credentials.get ceremony on this
  // page: a cancelled Face ID and a passkey this phone no longer holds both read NotAllowedError, and this browser
  // cannot tell them apart, so this browser's record of its passkey is never forgotten on a refusal (M17 L3 r1 M-8,
  // ruled for every ceremony at 1022+19 Q2). There is no forget: a passkey deleted from the phone stays pinned until
  // this page's storage is cleared (a known 1.0 edge, 1022+19 Q1). `what` says what did not happen
  const declined = (e, what) => "the passkey did not sign (" + ((e && e.name) || "cancelled") + "); " + what;
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
  let focusDone = false;      // the deep link's card was signed or denied here
  let focusMisses = 0;        // listings the linked card was not in: three, and the link is let go
  let stateAsking = false;    // a picked link's card was not listed: where it stands is being asked (GET /sign/<id>/state)
  let linkNote = "";          // what a terminal answer to that said, kept on the status line for this page
  const notes = new Map();    // request id -> [text, kind], kept across redraws
  const writeIns = new Map(); // sign request id -> {sha256, words}: the write-in being typed, kept across redraws
                              // for the version it was typed on only
  // sign request id -> the attempt id of an answer whose reply was lost and that the port has not settled
  // (levain.consent.sign Signer.attempt): its card is drawn frozen and asks the port again on every listing
  const signUnsettled = new Map();
  const signAsking = new Set();   // request ids whose attempt is being asked about now
  // where the answer with `attempt` on request `id` stands: the port's {state, ...}, or {state: "", why} when it said nothing
  const askAttempt = (id, attempt) => call("POST", "/sign/" + encodeURIComponent(id) + "/attempt", { attempt: attempt })
    .then((a) => (a.ok && typeof a.body.state === "string" ? a.body : { state: "", why: why(a) }));
  const refusedText = (st) => {
    const w = typeof st.reason === "string" && st.reason ? st.reason.replace(/[.\s]+$/, "") : "refused";
    return "not signed: " + w + (/nothing was signed/.test(w) ? "" : "; nothing was signed") + ": sign it again";
  };

  function draw(data, seq) {
    rpId = data.rp_id || rpId;
    const list = $("requests");
    const rows = Array.isArray(data.pending) ? data.pending : [];
    // undecided: the hold ended with no answer and the call did not run; the agent was told gap #N is open and
    // waits on a late ruling, signed here in the ruling namespace (never an answer's)
    const gaps = Array.isArray(data.undecided) ? data.undecided : [];
    // statements a caller filed for this phone's passkey to sign (`levain consent phone --sign-namespace`)
    const signs = Array.isArray(data.sign_pending) ? data.sign_pending : [];
    // questions an agent filed: answered by a tap or words, unsigned (an answer authorises nothing)
    const asks = Array.isArray(data.questions) ? data.questions : [];
    // dispatch jobs a planner filed (levain dispatch): a contract to sign with a passkey, or a live job to cancel
    const jobs = Array.isArray(data.dispatch_pending) ? data.dispatch_pending : [];
    // notes an agent filed: read and dismissed, never answered; they wait on nothing, so they are not counted as waiting
    const memos = Array.isArray(data.notes) ? data.notes : [];
    // words typed for a request no longer listed (signed, denied, expired) go with it, so a reused id starts empty
    for (const id of writeIns.keys()) if (!signs.some((r) => r.id === id)) writeIns.delete(id);
    // an unsettled answer whose request left the list (signed, denied, expired) has no card to ask for it: the port is
    // asked here, and what it says is the status line's last answer
    for (const [id, pend] of signUnsettled) {
      if (signs.some((r) => r.id === id) || signAsking.has(id)) continue;
      signAsking.add(id);
      askAttempt(id, pend).then((st) => {
        if (signUnsettled.get(id) !== pend || (st.state !== "signed" && st.state !== "refused")) return;
        signUnsettled.delete(id);
        last = st.state === "signed"
          ? "signed " + (st.choice ? "(" + st.choice + ") " : "") + id + " (its reply was lost; the port confirmed it)"
          : refusedText(st);
        setTimeout(load, 0);
      }).finally(() => signAsking.delete(id));
    }
    const waiting = rows.length + gaps.length + signs.length + asks.length + jobs.length;
    const unread = (Number(data.sign_overflow) > 0 ? " \u00b7 " + Number(data.sign_overflow) + " more sign requests not shown" : "")
      + (Number(data.questions_overflow) > 0 ? " \u00b7 " + Number(data.questions_overflow) + " more questions not shown" : "")
      + (Number(data.dispatch_overflow) > 0 ? " \u00b7 " + Number(data.dispatch_overflow) + " more dispatch jobs not shown" : "")
      + (memos.length ? " \u00b7 " + memos.length + (memos.length === 1 ? " note" : " notes") : "")
      + (Number(data.notes_overflow) > 0 ? " \u00b7 " + Number(data.notes_overflow) + " more notes not shown" : "")
      + (typeof data.notes_note === "string" && data.notes_note ? " \u00b7 agent notes unavailable: " + data.notes_note : "");
    const qnote = typeof data.questions_note === "string" && data.questions_note ? data.questions_note : "";
    const dnote = typeof data.dispatch_note === "string" && data.dispatch_note ? data.dispatch_note : "";
    // a store fault is never "nothing waiting": the page says it cannot see the agents' questions or dispatch jobs
    $("status").textContent = (waiting ? waiting + " waiting on you" : qnote || dnote ? "can't tell what is waiting on you" : "nothing is waiting on you")
      + (qnote ? " \u00b7 agent questions unavailable: " + qnote : "") + (dnote ? " \u00b7 dispatch jobs unavailable: " + dnote : "")
      + unread + (last ? " \u00b7 last: " + last : "") + linkNote;
    // a deep-linked sign request comes first and is scrolled to, once; one not listed is said, never silently lost
    const linked = focus ? signs.filter((r) => r.id === focus) : [];
    const rest = linked.length ? signs.filter((r) => r.id !== focus) : signs;
    const band = data.band && typeof data.band === "object" ? data.band : null;
    bandNumOf.clear();
    for (const it of (band && Array.isArray(band.items) ? band.items : [])) if (it && Number.isInteger(it.question)) bandNumOf.set(it.question, it.n);
    // the linked sign card stays first (a link he followed lands on THAT card); the band card follows it
    list.replaceChildren(...linked.map((r) => signCard(r, data.sign_note, true)),
                         ...(band && ((Array.isArray(band.items) && band.items.length) || band.note) ? [bandCard(band)] : []),
                         ...asks.map(questionCard),
                         ...rest.map((r) => signCard(r, data.sign_note)), ...jobs.map(dispatchCard),
                         ...rows.map((r) => row(r, false)), ...gaps.map((r) => row(r, true)),
                         ...(memos.length ? [el("h2", "notes-head", "Notes · nothing to answer"), ...memos.map(noteCard)] : []));
    // said once: after the linked card is signed or denied here the link goes without a note; otherwise it is let go
    // after three listings without it (a passing sign-side fault gets two more). A link opened mid-ceremony reloads
    // the page and the result shows on the reload
    if (linked.length) { focusMisses = 0; stateAsking = false; }           // consecutive listings without it
    // a picked link (his own tap elsewhere) whose card is not listed: where it stands is asked on each such listing,
    // before the rule below can let the link go (never while the sign side reports a fault). Only a terminal answer
    // (signed, denied, expired) is said, and it lets the link go at once; open, gone or a failed ask leaves the rule
    // below to run, and the next listing asks again
    if (focus && pick && !linked.length && !busy && !focusDone && !stateAsking && !data.sign_note) {
      stateAsking = true;
      const id = focus;
      call("GET", "/sign/" + encodeURIComponent(id) + "/state").then((s) => {
        stateAsking = false;
        const st = s.ok && s.body ? s.body.state : "";
        const said = st === "signed" ? "is already signed here" + (typeof s.body.choice === "string" ? " (" + s.body.choice + ")" : "")
          : st === "denied" ? "was denied here"
          : st === "expired" ? "expired: a fresh link comes with its next filing" : "";
        if (!said || linkNote) return;
        linkNote = " \u00b7 the linked sign request " + id + " " + said;
        $("status").textContent += linkNote;
        if (focus === id) { focus = ""; pick = ""; }
        setTimeout(load, 0);
      });
    }
    if (focus && !linked.length && !busy && (focusDone || ++focusMisses >= 3)) {
      if (!focusDone && !data.sign_note)
        $("status").textContent += " \u00b7 the linked sign request " + focus + " is not open here (signed, denied, expired or not filed)";
      focus = ""; pick = "";
    }
    // to the card's head: he reads the whole item before the button
    if (linked.length && !draw.scrolled) {
      draw.scrolled = true;
      (list.firstChild.querySelector(".req-head") || list.firstChild).scrollIntoView({ block: "start" });
    }
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
    drawGrants(data);
    drawDrops(data, seq);
  }

  // C4-c / C6-c: the coordinator's review queue and its morning card, READ-ONLY here. /coord.json carries both
  // (`coord_review`, `coord_morning`, levain.coordinator.phone); "waiting on you" and every count is the server's, never
  // recomputed. Clear and reopen are not offered here. It is its own read, not /consent.json's: asked for only while the
  // Review tab is shown, and never again while a previous ask is still out, so the 10 s tick cannot stack requests
  let coordAsking = false;
  // a signed clear's passkey picker is open (reviewSigned): no read redraws the queue under it, so its buttons stay
  // until one is picked or it is cancelled, and then the queue is read again
  let reviewPicking = false;
  function loadCoord() {
    if (!token || coordAsking || reviewPicking || !$("review") || $("review").hidden) return;
    coordAsking = true;
    call("GET", "/coord.json").then((r) => {
      // the tab was left, or a passkey picker opened while this was out: nothing to draw (a picker open is kept as it is);
      // showing the tab again asks again
      if ($("review").hidden || reviewPicking) return;
      const bad = r.ok ? "" : why(r);
      drawReview(r.ok ? r.body : { coord_review_note: bad, coord_morning_note: bad });
    }).catch(() => { /* the panel keeps what it showed; while the tab is shown the 10 s tick asks again */ }).finally(() => { coordAsking = false; });
  }
  const ago = (iso, now) => {
    const t = typeof iso === "string" ? Date.parse(iso.replace(/(\.\d{3})\d+/, "$1")) : NaN;
    const n = typeof now === "string" ? Date.parse(now.replace(/(\.\d{3})\d+/, "$1")) : Date.now();
    if (isNaN(t) || isNaN(n)) return "";
    const s = Math.max(0, Math.round((n - t) / 1000));
    return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago";
  };
  function reviewCard(i, now) {
    const card = el("article", "req review-item" + (i.state === "cleared" ? " cleared" : ""));
    const head = el("div", "req-head");
    head.append(el("span", "tool", typeof i.label === "string" && i.label ? i.label : i.feed), el("span", "meta", ago(i.at, now)));
    if (i.authority_relevant) head.appendChild(el("span", "tier", "yours to clear"));
    card.append(head, el("div", "note-what", typeof i.text === "string" ? i.text : ""));
    if (i.state === "cleared") card.appendChild(el("div", "where", "cleared " + ago(i.cleared_at, now)));
    // Clear (a plain open item) or Reopen (a cleared one), as you, from this paired phone (the run token's link is told
    // it opens the queue only). A "yours to clear" item is never cleared on a device token alone: its Clear is signed
    // with your passkey (reviewSigned), or it is cleared on the laptop
    const key = "review\u0000" + i.item_id;   // never a consent, question or note id
    const msg = el("div", "note");
    const say = (text, kind) => { notes.set(key, [text, kind || ""]); msg.textContent = text; msg.className = "note " + (kind || ""); };
    if (notes.has(key)) say(notes.get(key)[0], notes.get(key)[1]);
    if (i.authority_relevant && i.state !== "cleared") {
      const signed = el("button", "quiet", "Clear with passkey");
      signed.type = "button";
      const row = el("div", "actions");
      signed.addEventListener("click", () => reviewSigned(i, say, signed, row));
      row.appendChild(signed);
      card.append(row, el("div", "where", "or on the laptop: levain coord review clear " + i.item_id), msg);
      return card;
    }
    const action = i.state === "cleared" ? "reopen" : "clear";
    const go = el("button", "quiet", action === "clear" ? "Clear" : "Reopen");
    go.type = "button";
    go.addEventListener("click", () => {
      if (busy) return;
      busy++;
      go.disabled = true;
      // the version this card was drawn at: the server refuses a Clear or Reopen of an item that moved since (409)
      call("POST", "/coord/review/" + encodeURIComponent(i.item_id) + "/" + action, { version: String(i.version || "") }).then((a) => {
        if (!a.ok) { go.disabled = false; return say("not " + (action === "clear" ? "cleared" : "reopened") + ": " + why(a), "bad"); }
        // said on this card only (never kept for the item): the redraw moves it to its new group
        notes.delete(key);
        msg.textContent = action === "clear" ? "cleared" : "reopened";
        msg.className = "note ok";
      }).catch((e) => { go.disabled = false; say("the page failed (" + ((e && e.name) || "error") + "); try again", "bad"); })
        .finally(() => { busy--; setTimeout(loadCoord, 300); });
    });
    const row = el("div", "actions");
    row.appendChild(go);
    card.append(row, msg);
    return card;
  }
  // The signed clear of a "yours to clear" item (c4-mut R5): levain files its own sign request over the item as it is now
  // (POST /coord/review/<id>/sign-request), this phone's passkey signs it on /sign/<id>/challenge and answer, and only a
  // signed request is sent with the clear. Every step that fails leaves the item open and says why; the next tap files anew.
  function reviewSigned(i, say, go, actions) {
    if (busy || reviewPicking) return;     // an open picker belongs to its card until a pick or Cancel
    busy++;
    go.disabled = true;
    let picking = false;
    const item = encodeURIComponent(i.item_id);
    const fail = (text) => { go.disabled = false; say(text, "bad"); };
    const sign = (r, fp, rawId) => {
      say("asking for the challenge…");
      return call("POST", "/sign/" + encodeURIComponent(r.id) + "/challenge", { signer_fp: fp, request_sha256: r.sha256 }).then((c) => {
        if (!c.ok) return fail("not signed: " + why(c));
        say("confirm with your passkey to sign…");
        const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
        if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
        return navigator.credentials.get({ publicKey: pk }).then((cred) => {
          say("verifying the signature…");
          return call("POST", "/sign/" + encodeURIComponent(r.id) + "/answer", {
            authenticator_data: b64u(cred.response.authenticatorData), client_data_json: b64u(cred.response.clientDataJSON),
            signature: b64u(cred.response.signature) }).then((a) => {
            if (!a.ok) return fail("not signed: " + why(a));
            say("signed; clearing…");
            return call("POST", "/coord/review/" + item + "/clear", { sign_request: r.id }).then((x) => {
              if (!x.ok) return fail("signed, but not cleared: " + why(x));
              say("cleared with your passkey", "ok");
              notes.delete("review\u0000" + i.item_id);   // the card moves to Cleared on the next read
            });
          });
        }, (e) => fail(declined(e, "nothing was signed")));
      });
    };
    say("filing it for your signature…");
    call("POST", "/coord/review/" + item + "/sign-request", {}).then((f) => {
      if (!f.ok) return fail("not cleared: " + why(f));
      const r = f.body.request;
      const signers = Array.isArray(f.body.signers) ? f.body.signers : [];
      if (!r || r.refused) return fail("not signable: " + (r && r.refused ? r.refused : "the request is not open"));
      const local = mine();
      const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
      if (known.length === 1) return sign(r, known[0], local[known[0]]);
      if (!known.length && signers.length === 1) return sign(r, signers[0], null);
      if (!signers.length) return fail(f.body.note || "no passkey is enrolled for this name");
      // more than one could sign: the operator picks the one this phone holds, as on a sign card; the card is kept
      // (no refresh) until a pick or Cancel
      go.disabled = false;
      picking = reviewPicking = true;
      say("more than one passkey is enrolled for this name; pick the one this phone holds:");
      const closePicker = () => { reviewPicking = false; for (const old of actions.querySelectorAll(".pick")) old.remove(); };
      for (const fp of known.length ? known : signers) {
        const p = el("button", "quiet pick", fp.slice(0, 18) + "…");
        p.type = "button";
        p.addEventListener("click", () => {
          if (busy) return;
          busy++;
          closePicker();
          go.disabled = true;
          sign(r, fp, known.length ? local[fp] : null).catch((e) => fail("the page failed (" + ((e && e.name) || "error") + "); try again"))
            .finally(() => { busy--; setTimeout(loadCoord, 300); });
        });
        actions.appendChild(p);
      }
      const cancel = el("button", "quiet pick", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => {
        if (busy) return;
        closePicker();
        say("nothing was signed; the item stays open");
        setTimeout(loadCoord, 300);
      });
      actions.appendChild(cancel);
    }).catch((e) => fail("the page failed (" + ((e && e.name) || "error") + "); try again"))
      .finally(() => { busy--; if (!picking) setTimeout(loadCoord, 300); });
  }
  function drawReview(data) {
    if (!$("review-list")) return;
    const rv = data.coord_review && typeof data.coord_review === "object" ? data.coord_review : null;
    const rn = typeof data.coord_review_note === "string" && data.coord_review_note ? data.coord_review_note : "";
    const open = rv && Array.isArray(rv.open) ? rv.open : [];
    const cleared = rv && Array.isArray(rv.cleared) ? rv.cleared : [];
    const total = rv && Number.isInteger(rv.open_total) ? rv.open_total : open.length;
    // a queue that could not be read is never "nothing to review"
    $("review-meta").textContent = rn ? "can't read the review queue: " + rn : !rv ? "no coordinator store yet"
      : total ? total + " open" + (total > open.length ? " (" + open.length + " shown)" : "") : "nothing to review";
    $("review-list").replaceChildren(...open.map((i) => reviewCard(i, rv.now)));
    // a picker this redraw removed cannot be cancelled: its flag goes with it, so a later tap is not refused for good
    if (!$("review-list").querySelector(".pick")) reviewPicking = false;
    $("review-cleared-meta").textContent = rn ? "can't read" : cleared.length ? "newest first" : "none";
    $("review-cleared").replaceChildren(...cleared.map((i) => reviewCard(i, rv.now)));
    const mc = data.coord_morning && typeof data.coord_morning === "object" ? data.coord_morning : null;
    const mn = typeof data.coord_morning_note === "string" && data.coord_morning_note ? data.coord_morning_note : "";
    $("morning-meta").textContent = mn ? "can't read the morning card: " + mn : !mc ? "no coordinator store yet" : "as of " + readable(mc.at);
    if (!mc) { $("morning").replaceChildren(); return; }
    const facts = [];
    const away = Array.isArray(mc.away) ? mc.away : [], unk = Array.isArray(mc.away_unknown) ? mc.away_unknown : [];
    // known-away and unknown are independent: one agent unreadable never hides that another is away
    if (away.length || unk.length) {
      if (away.length) facts.push("away ON for " + away.join(", "));
      if (unk.length) facts.push("away UNKNOWN for " + unk.join(", "));
    } else facts.push("away off");
    facts.push(mc.waiting + " waiting on you" + (mc.waiting_unreadable ? " (" + mc.waiting_unreadable + " unreadable)" : ""));
    // a section the card could not read is null: said so, never "0 undecided"
    facts.push(Number.isInteger(mc.undecided) ? mc.undecided + " undecided" : "undecided: could not read");
    facts.push(mc.budgets + (mc.budgets === 1 ? " thread budget" : " thread budgets") + (mc.budgets_crossed ? ", " + mc.budgets_crossed + " crossed" : "")
               + (mc.budgets_unreadable ? ", " + mc.budgets_unreadable + " could not be read (new steps stop)" : ""));
    facts.push(mc.review_open + " in the review queue");
    const card = el("article", "req morning-card");
    // the counts lead; the card's own lines (what `levain coord morning` prints) are one tap down, so the queue is not read twice
    const more = el("details", "morning-more");
    more.append(el("summary", null, "the whole card"), el("pre", "display morning-lines", Array.isArray(mc.lines) ? mc.lines.slice(1).join("\n") : ""));
    card.append(el("div", "note-what", facts.join(" \u00b7 ")), more);
    $("morning").replaceChildren(card);
  }

  // The NEEDS YOU band (Q17-ALL S5b): each open item under its band number, one shorthand box ("15b", "1a 2b") and a tap on
  // an option sending "<n><letter>". The listing's `band` carries the {store_id, view_id} this page was shown; an answer posts
  // them to /band/answer and the server records it only while that is still this device's newest view (409 stale_band: nothing
  // was recorded, the list is redrawn). Unsigned, a human channel (Q1 ruled (a)); an authority item is listed and opens its
  // signing sheet, never answered here. Text only (never HTML) for everything an agent wrote.
  const bandNumOf = new Map();      // question n -> band number: a question card leads with it (`#n` stays as the source tag)
  let bandDraft = "";               // the shorthand typed and not sent: a redraw gives it back
  function bandCard(band) {
    const card = el("article", "req band");
    card.setAttribute("data-band", "");
    const items = Array.isArray(band.items) ? band.items : [];
    const head = el("div", "req-head");
    head.append(el("span", "tool", "Needs you"), el("span", "tier", "band"), el("span", "meta", items.length + " open"));
    card.appendChild(head);
    const note = el("div", "note");
    const say = (text, kind) => { notes.set("band", [text, kind || ""]); note.textContent = text; note.className = "note " + (kind || ""); };
    if (notes.has("band")) say(notes.get("band")[0], notes.get("band")[1]);
    const box = el("input", "band-box");
    box.type = "text"; box.maxLength = 400; box.value = bandDraft; box.autocomplete = "off"; box.autocapitalize = "off";
    box.setAttribute("autocorrect", "off"); box.spellcheck = false;
    box.setAttribute("aria-label", "answer by number and letter");
    box.placeholder = "e.g. 15b";
    const go = el("button", "allow band-send", "Send");
    go.type = "button";
    const buttons = [go];
    const canAnswer = band.view_id !== null && band.view_id !== undefined && typeof band.store_id === "string";
    const send = (text) => {
      text = String(text || "").trim();
      if (!text || busy) return;
      if (!canAnswer) { say(band.note || "the band could not be recorded just now, so it cannot be answered", "bad"); return; }
      busy++;
      buttons.forEach((b) => { b.disabled = true; });
      box.disabled = true;
      say("sending the answer…");
      call("POST", "/band/answer", { text: text, store_id: band.store_id, view_id: band.view_id }).then((a) => {
        if (a.status === 409) {
          // a different band was shown since this one: nothing was recorded; the list is redrawn from a fresh look
          bandDraft = box.value;
          box.blur();
          buttons.forEach((b) => { b.disabled = false; });    // never left waiting on a redraw that may fail
          box.disabled = false;
          say("the band changed since it was shown to you, so nothing was recorded. It is redrawn: check the numbers and send again.", "bad");
          return;
        }
        buttons.forEach((b) => { b.disabled = false; });
        box.disabled = false;
        if (!a.ok) return say("not answered: " + why(a), "bad");
        const b = a.body || {};
        const rec = Array.isArray(b.recorded) ? b.recorded : [];
        const refused = Array.isArray(b.refused) ? b.refused : [];
        const sheets = Array.isArray(b.sheets) ? b.sheets : [];
        // what was not recorded stays in the box (a piece runs from its number to the next number)
        const done = new Set(rec.map((l) => (/^\d+/.exec(String(l)) || [""])[0]).filter(Boolean));
        let cur = null;
        bandDraft = (rec.length ? String(text).split(/[\s,;]+/).filter((tok) => {
          const m = /^\d+/.exec(tok);
          if (m) cur = m[0];
          return !(cur !== null && done.has(cur));
        }).join(" ") : text);
        if (!refused.length && !sheets.length) bandDraft = "";
        box.value = bandDraft;
        last = "answered the band" + (rec.length ? " (" + rec.length + ")" : "");
        const bad = refused.length > 0 || sheets.length > 0;
        say((rec.length ? "recorded: " + rec.join(", ") : "nothing recorded")
          + (refused.length ? " · refused: " + refused.join("; ") : "")
          + (sheets.length ? " · " + sheets.map((s) => (/^\s*(\d+)/.exec(String(s)) || [0, "?"])[1]).join(", ")
            + " needs your signature: sign it on the phone's sign sheet" : ""), bad ? "bad" : "ok");
      }).catch((e) => { buttons.forEach((b) => { b.disabled = false; }); box.disabled = false; say("the page failed (" + ((e && e.name) || "error") + "); answer again, and if it was already recorded the page says so", "bad"); })
        .finally(() => { busy--; setTimeout(load, 300); });
    };
    for (const it of items) {
      if (!it || typeof it !== "object") continue;
      const authority = it.kind === "authority";
      const row = el("div", "band-item");
      row.setAttribute("data-band-row", String(it.n));
      const top = el("div", "req-head");
      top.append(el("b", "band-num", String(it.n)), el("span", "meta", typeof it.tag === "string" ? it.tag : ""));
      if (authority) top.appendChild(el("span", "tier", "needs your signature"));
      row.appendChild(top);
      row.appendChild(el("div", "band-what", typeof it.what === "string" ? it.what : ""));
      const blocks = Array.isArray(it.blocks) ? it.blocks.join(", ") : (typeof it.blocks === "string" ? it.blocks : "");
      if (blocks) row.appendChild(el("div", "where", "blocks: " + blocks));
      const acts = el("div", "actions");
      for (const o of (Array.isArray(it.options) ? it.options : [])) {
        const label = String(o && o.letter) + " · " + (o && typeof o.label === "string" ? o.label : "") + (o && o.letter === it.rec ? " (rec)" : "");
        if (authority) { acts.appendChild(el("div", "where", label)); continue; }
        const b = el("button", o && o.letter === it.rec ? "allow" : "quiet", label);
        b.type = "button";
        b.addEventListener("click", () => send(String(it.n) + String(o && o.letter)));
        buttons.push(b);
        acts.appendChild(b);
      }
      row.appendChild(acts);
      card.appendChild(row);
    }
    if (typeof band.note === "string" && band.note) card.appendChild(el("div", "where", band.note));
    box.addEventListener("input", () => { bandDraft = box.value; });
    box.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); send(box.value); } });
    go.addEventListener("click", () => send(box.value));
    const bar = el("div", "band-bar");
    bar.append(box, go);
    card.append(bar, note);
    return card;
  }

  // a question: text only (never HTML) for everything the agent wrote. No passkey: the answer is unsigned and
  // authorises nothing; the server records it once and delivers it to the agent's session
  function questionCard(q) {
    const card = el("article", "req ask");
    const head = el("div", "req-head");
    const bn = bandNumOf.get(Number(q.n));
    if (bn !== undefined) head.appendChild(el("b", "band-num", String(bn)));    // the band number leads; `#n` is the source tag
    head.append(el("span", "tool", "question #" + q.n), el("span", "tier", "ask"), el("span", "meta", q.agent || ""));
    card.append(head, el("pre", "display", typeof q.what === "string" ? q.what : ""));
    card.appendChild(el("div", "where", "blocks: " + (q.blocks || "")));
    const key = "ask\u0000" + q.id;   // never a consent id
    const note = el("div", "note");
    const say = (text, kind) => { notes.set(key, [text, kind || ""]); note.textContent = text; note.className = "note " + (kind || ""); };
    if (notes.has(key)) say(notes.get(key)[0], notes.get(key)[1]);
    const actions = el("div", "actions");
    const buttons = [];
    const send = (body) => {
      if (busy) return;
      busy++;
      buttons.forEach((b) => { b.disabled = true; });
      words.disabled = true;
      say("sending the answer\u2026");
      call("POST", "/question/" + encodeURIComponent(q.n) + "/answer", body).then((a) => {
        if (a.status === 409) words.value = "";   // answered elsewhere: nothing left to keep a draft for
        if (!a.ok) {
          if (a.status !== 409) { buttons.forEach((b) => { b.disabled = false; }); words.disabled = false; }   // retryable: the draft stays
          return say("not answered: " + why(a), "bad");
        }
        words.value = "";
        last = "answered question #" + q.n;
        const d = a.body.delivery || {};
        say("answered \u00b7 delivery " + (d.route || "") + ": " + (d.reason || ""), d.route === "failed" || d.route === "none" ? "bad" : "ok");
      }).catch((e) => { buttons.forEach((b) => { b.disabled = false; }); words.disabled = false; say("the page failed (" + ((e && e.name) || "error") + "); " + (words.value.trim() ? "your words are kept: " : "") + "answer again, and if it was already recorded the page says so", "bad"); })
        .finally(() => { busy--; setTimeout(load, 1500); });
    };
    // an option tied to a push (Q-3): its one tap signs a late ruling ALLOW on that exact push with the passkey,
    // and only once that verifies does the server record the answer. The answer itself still authorises nothing
    const link = q.link && typeof q.link === "object" ? q.link : null;
    const push = link && link.card && typeof link.card === "object" ? link.card : null;
    const signers = push && Array.isArray(push.signers) ? push.signers : [];
    const signable = !!push && usable && signers.length > 0 && !push.cut && push.tier !== "T3";
    for (const o of (Array.isArray(q.options) ? q.options : [])) {
      const tied = !!link && o.key === link.option;
      const b = el("button", o.key === q.rec ? "allow" : "quiet", o.label + " (" + o.key + ")" + (o.key === q.rec ? " (rec)" : "")
                   + (tied && signable ? " \u00b7 allow the push with passkey" : ""));
      b.type = "button";
      if (tied && signable) b.addEventListener("click", () => start(push, "allow", signers, say, actions, true,
                                                                     (fp, rawId) => askRule(q, push, fp, rawId, say, buttons, words)));
      else b.addEventListener("click", () => send({ option: o.key }));
      buttons.push(b);
      actions.appendChild(b);
    }
    // the push as its own undecided card shows it: tool, tier, agent and gap first, then exactly what is signed
    const leads = el("div", "leads");
    if (link && push) {
      const ph = el("div", "req-head");
      ph.append(el("span", "tool", "option " + link.option + " leads to: " + (push.tool || "(tool)")),
                el("span", "tier" + (push.tier === "T3" ? " t3" : ""), push.tier || ""),
                el("span", "meta", (push.agent || "") + " \u00b7 undecided, gap " + (push.gap_label || "#" + push.gap)));
      leads.append(ph, el("pre", "display", typeof push.display === "string" ? push.display : ""));
      if (push.cwd) leads.appendChild(el("div", "where", "in " + push.cwd));
      for (const n of checkLines(push)) leads.appendChild(n);   // the same two digests and hint the push shows as its own card
    }
    leads.hidden = !(link && push);
    // shown, never kept in `notes`: it is about this listing, and the next one may make the option signable
    if (link && !signable && !notes.has(key)) {
      note.textContent = push ? (push.signer_note || "this phone cannot sign a ruling on this push") + "; answering option "
                                + link.option + " allows nothing: the push asks when it runs"
                              : "option " + link.option + "'s push is not shown here (no longer undecided, or past the "
                                + "listing's limit): answering it allows nothing";
    }
    const words = el("textarea", "words");
    words.rows = 2;
    words.maxLength = 4000;
    words.placeholder = "or your own words";
    const go = el("button", "quiet", "Send words");
    go.type = "button";
    go.addEventListener("click", () => { if (words.value.trim()) send({ text: words.value }); else say("write something first", "bad"); });
    buttons.push(go);
    const sendRow = el("div", "actions");
    sendRow.appendChild(go);
    card.append(leads, actions, words, sendRow, note);
    return card;
  }

  // an agent's note: text only, never HTML. It carries no answer, no rec and no passkey; "Dismiss" marks it read and the
  // agent is told nothing
  function noteCard(n) {
    const card = el("article", "req agent-note");
    const head = el("div", "req-head");
    head.append(el("span", "tool", "note #" + n.n), el("span", "meta", "from " + (n.agent || "agent")));
    card.append(head, el("div", "note-what", typeof n.what === "string" ? n.what : ""));
    if (typeof n.detail === "string" && n.detail) card.appendChild(el("div", "note-detail", n.detail));
    const key = "note\u0000" + n.id;   // never a consent or question id
    const msg = el("div", "note");
    const say = (text, kind) => { notes.set(key, [text, kind || ""]); msg.textContent = text; msg.className = "note " + (kind || ""); };
    if (notes.has(key)) say(notes.get(key)[0], notes.get(key)[1]);
    const go = el("button", "quiet", "Dismiss");
    go.type = "button";
    go.addEventListener("click", () => {
      if (busy) return;
      busy++;
      go.disabled = true;
      call("POST", "/note/" + encodeURIComponent(n.n) + "/dismiss", {}).then((a) => {
        if (!a.ok && a.status !== 409) { go.disabled = false; return say("not dismissed: " + why(a), "bad"); }
        last = "dismissed note #" + n.n;
        say("dismissed", "ok");
      }).catch((e) => { go.disabled = false; say("the page failed (" + ((e && e.name) || "error") + "); try again", "bad"); })
        .finally(() => { busy--; setTimeout(load, 800); });
    });
    const row = el("div", "actions");
    row.appendChild(go);
    card.append(row, msg);
    return card;
  }

  // Q12 / Q16: the two digests the terminal prints before signing (short here, the whole value one tap away), and what a
  // shell command looks like. Text only: the command is the agent's. A hint informs and never authorises
  function digestLine(label, value) {
    const full = typeof value === "string" ? value : "";
    if (!full) {                                   // a missing digest is shown as missing, never dropped
      const gone = el("div", "dg");
      gone.append(el("span", "dg-label", label + ": "), el("code", "dg-val", "(none)"));
      return gone;
    }
    const at = full.indexOf(":") + 1;
    const short = full.length > at + 12 ? full.slice(0, at + 12) + "\u2026" : full;
    const line = el("div", "dg");
    const code = el("code", "dg-val", short);
    code.tabIndex = 0;
    code.setAttribute("role", "button");
    code.title = full;
    let open = false;
    const flip = () => { open = !open; code.textContent = open ? full : short; };
    code.addEventListener("click", flip);
    code.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
    line.append(el("span", "dg-label", label + " "), code);
    return line;
  }
  const NO_PATTERN = "no pattern matched: read the command";
  function checkLines(r) {
    const out = [];
    // the hint comes first, with a rule at its left, so it reads apart from the metadata; a shell request always has one
    const hints = Array.isArray(r.hints) ? r.hints.filter((h) => typeof h === "string" && h) : [];
    if (hints.length) {
      const box = el("div", "dg hint");
      box.appendChild(el("span", "dg-label", "hint (a few patterns only; informs, never authorises)"));
      for (const h of hints) box.appendChild(el("div", h === NO_PATTERN ? "dg-hint dg-none" : "dg-hint", "\u2022 " + h));
      out.push(box);
    }
    out.push(digestLine("input digest", r.tool_input_digest), digestLine("request digest", r.digest));
    return out.filter(Boolean);
  }

  function row(r, ruling) {
    const card = el("article", "req");
    const head = el("div", "req-head");
    head.append(el("span", "tool", r.tool || "(tool)"), el("span", "tier" + (r.tier === "T3" ? " t3" : ""), r.tier || ""),
                el("span", "meta", (r.agent || "") + (ruling ? " \u00b7 undecided, gap " + (r.gap_label || "#" + r.gap) : "")));
    card.append(head, el("pre", "display", typeof r.display === "string" ? r.display : ""));
    if (r.cwd) card.appendChild(el("div", "where", "in " + r.cwd));
    if (r.targets) card.appendChild(el("div", "where", "touches " + r.targets));
    for (const n of checkLines(r)) card.appendChild(n);
    const note = el("div", "note");
    const say = (text, kind) => { notes.set(r.id, [text, kind || ""]); note.textContent = text; note.className = "note " + (kind || ""); };
    if (notes.has(r.id)) say(notes.get(r.id)[0], notes.get(r.id)[1]);
    if (r.note) { say(r.note); card.appendChild(note); return card; }          // a cut display is never signed here
    const signers = Array.isArray(r.signers) ? r.signers : [];
    if (!signers.length) { if (!notes.has(r.id)) say(r.signer_note || "no passkey is enrolled for this name"); card.appendChild(note); return card; }
    const actions = el("div", "actions");
    // a passkey is a display-phone key: it denies any request and allows up to T2 (the server refuses the rest)
    // a ruling over a display cut at its limit: only a deny is signed for it (the server refuses an allow)
    const decisions = r.tier === "T3" || (ruling && r.cut) ? ["deny"] : ["allow", "deny"];
    if (ruling && r.cut && !notes.has(r.id)) say("too long to show whole here: only a deny can be signed for it");
    else if (r.tier === "T3" && !notes.has(r.id)) say(ruling ? "a late allow of a T3 request is the laptop's (levain consent rule there)"
                                                        : "allowing a T3 request is the laptop's (levain consent answer there)");
    for (const decision of decisions) {
      const b = el("button", decision, (ruling ? "rule " : "") + decision + " with passkey");
      b.type = "button";
      b.disabled = !usable;
      b.addEventListener("click", () => start(r, decision, signers, say, actions, ruling));
      actions.appendChild(b);
    }
    card.append(actions, note);
    return card;
  }

  let closePick = null;       // the open passkey picker's close, if one is up (it holds busy)
  // a picker left open while the page is put away (phone locked, app switched) would hold the whole list still,
  // consent cards included, until someone came back to dismiss it: leaving the page dismisses it instead
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && closePick && closePick()) setTimeout(load, 0);
  });
  // so does leaving its view (phone_app.js's tabs): a picker or a revoke still being asked for, left on a hidden tab,
  // would hold every list still where nobody can see it. A ceremony past its Face ID prompt is not dismissed
  // and so does any open K8 review (signPending): the view change dismisses every one (dismissReviews, below)
  window.levainViewChanged = (v) => {
    const picked = !!(closePick && closePick());
    if (dismissReviews() || picked) setTimeout(load, 0);
    if (v === "review") setTimeout(loadCoord, 0);     // the tab's first look does not wait for the next tick
  };

  function start(r, decision, signers, say, actions, ruling, go, what) {
    const run = go || ((fp, rawId) => answer(r, decision, fp, rawId, say, ruling));
    const local = mine();
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    // exactly one passkey this browser made is enrolled: use it, and ask the OS for that credential
    if (known.length === 1) return run(known[0], local[known[0]]);
    if (!known.length && signers.length === 1) return run(signers[0], null);
    // otherwise the person picks: among this browser's own when it made several (a deleted one is forgotten when
    // the OS refuses it), else among every enrolled one (the OS then offers every passkey for this name, and a
    // pick of another device's key is refused at verify)
    const choices = known.length ? known : signers;
    if (closePick) closePick();   // a second picker replaces the first
    if (busy) return;
    // `what` names the act the pick signs ("to SIGN job …"), so the picker never reads as a bare key choice
    say(what ? "pick a passkey " + what + " (more than one is enrolled for this name; pick the one this phone holds):"
             : "more than one passkey is enrolled for this name; pick the one this phone holds:");
    // the picker holds the list still like any ceremony: busy is taken while it is up, released by a pick (run takes
    // it again) or by dismissing it
    busy++;
    const picks = [];
    // close() releases busy once: a second activation queued before the first one's redraw (or a later picker's
    // replace) finds it closed and does nothing, so busy never goes below 0 and a pick never runs twice
    let closed = false;
    const close = () => {
      if (closed) return false;
      closed = true;
      picks.forEach((x) => x.remove());
      busy--;
      if (closePick === dismiss) closePick = null;
      return true;
    };
    // every dismissal that is not a pick (the dismiss button, a second picker, the page put away) also clears the
    // "pick a passkey" line, which the note store would otherwise draw again over a card with no picker under it
    const dismiss = () => { if (!close()) return false; say(""); return true; };
    closePick = dismiss;
    for (const fp of choices) {
      const p = el("button", "quiet pick", fp.slice(0, 18) + "…");
      p.type = "button";
      p.addEventListener("click", () => { if (close()) run(fp, known.length ? local[fp] : null); });
      picks.push(p);
    }
    const x = el("button", "quiet pick", "dismiss");
    x.type = "button";
    x.addEventListener("click", dismiss);
    picks.push(x);
    picks.forEach((b) => actions.appendChild(b));
  }

  // the one tap on a question's push option: the ruling's challenge for the linked request, the passkey, then the
  // question's own route, which applies the ruling and only then records the answer
  function askRule(q, card, fp, rawId, say, buttons, words) {
    if (busy) return;
    busy++;
    buttons.forEach((b) => { b.disabled = true; });
    words.disabled = true;
    const again = () => { buttons.forEach((b) => { b.disabled = false; }); words.disabled = false; };
    say("asking for the ruling to sign\u2026");
    call("POST", "/consent/" + encodeURIComponent(card.id) + "/rule-challenge", { decision: "allow", signer_fp: fp }).then((c) => {
      if (!c.ok) { again(); return say(why(c), "bad"); }
      say("confirm with your passkey to answer and allow the push\u2026");
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => call("POST", "/question/" + encodeURIComponent(q.n) + "/rule", {
        option: q.link.option, payload: c.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
        client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) }))
        .then((a) => {
          // a sent signature: only the server's own refusal (a 4xx) says nothing was ruled
          if (!a.ok && (a.status === 0 || a.status >= 500)) return say("result unknown (" + why(a) + "): reload to see where it stands", "bad");
          if (!a.ok) { if (a.status !== 409) again(); return say("not answered, nothing allowed: " + why(a), "bad"); }
          // ruled, but the answer was not recorded: the draft stays, and the reload below redraws the card with its
          // push no longer undecided, so the option is then a plain answer (never a second ruling)
          if (a.body.answered) words.value = "";
          last = a.body.answered ? "answered question #" + q.n + " and allowed its push"
                                 : "allowed question #" + q.n + "'s push (its answer was not recorded)";
          const d = a.body.delivery || {};
          const head = a.body.answered ? "answered and ruled the push allowed" : "the push is ruled allowed; " + (a.body.answer_note || "");
          say(head + " \u00b7 signed by " + ((a.body.ruling || {}).signer_fp || "") + " \u00b7 delivery " + (d.route || "") + ": " + (d.reason || ""),
              a.body.answered && d.route !== "failed" && d.route !== "none" ? "ok" : "bad");
        }, (e) => {
          again();
          say(declined(e, "nothing was answered or allowed"), "bad");
        });
    }).catch((e) => say("the page failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  function answer(r, decision, fp, rawId, say, ruling) {
    if (busy) return;
    busy++;
    const base = "/consent/" + encodeURIComponent(r.id) + "/";
    say("asking for the " + (ruling ? "ruling" : "answer") + " to sign…");
    call("POST", base + (ruling ? "rule-challenge" : "challenge"), { decision: decision, signer_fp: fp }).then((c) => {
      if (!c.ok) return say(why(c), "bad");
      say("confirm with your passkey to " + decision + "…");
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => call("POST", base + (ruling ? "rule" : "answer"), {
        payload: c.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
        client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) }))
        .then((a) => {
          if (!a.ok) return say("not answered: " + why(a), "bad");
          const done = (ruling ? "ruled " : "") + (a.body.decision === "allow" ? "allowed" : "denied");
          last = done + " " + (r.tool || "");
          const d = a.body.delivery || {};
          const where = d.reason ? " · delivery " + (d.route || "") + ": " + d.reason : "";
          // the ruling stands either way; a delivery that failed or had nothing to deliver is shown as a failure
          say(done + " · signed by " + a.body.signer_fp + where, d.route === "failed" || d.route === "none" ? "bad" : "ok");
        },
              (e) => say(declined(e, "nothing was answered"), "bad"));
    }).catch((e) => say("the page failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  // a sign request: the whole message is shown as text (never HTML) and is exactly what the passkey signs (the
  // challenge and the deny send back the sha256 of the file this card was built from, so a file changed since is
  // refused); a request the server refused (a character that can show differently from its bytes, a malformed file)
  // shows only why, and can be denied. A note kept across redraws belongs to the version it was made on: a changed
  // file drops it, and what the server says of the request now (refused, no passkey) is shown, never kept
  function signCard(r, signNote, linked) {
    const card = el("article", "req sign" + (linked ? " linked" : ""));
    const head = el("div", "req-head");
    head.append(el("span", "tool", r.refused ? "sign request (refused)" : (r.title || "(untitled)")),
                el("span", "tier", "sign"), el("span", "meta", r.namespace || ""));
    card.appendChild(head);
    const note = el("div", "note");
    const key = "sign\u0000" + r.id;   // never a consent id
    const show = (text, kind) => { note.textContent = text; note.className = "note " + (kind || ""); };
    const say = (text, kind) => { notes.set(key, [text, kind || "", r.sha256 || ""]); show(text, kind); };
    if (notes.has(key) && notes.get(key)[2] !== (r.sha256 || "")) notes.delete(key);
    const kept = notes.get(key);
    if (r.refused) show("not signable: " + r.refused, "bad");
    else if (kept) show(kept[0], kept[1]);
    if (!r.refused) {
      if (r.requester) card.appendChild(el("div", "where", "from " + r.requester));
      card.appendChild(el("pre", "display", typeof r.message === "string" ? r.message : ""));
      if (r.expires_at) card.appendChild(el("div", "where", "until " + r.expires_at));
    }
    const actions = el("div", "actions");
    const signers = Array.isArray(r.signers) ? r.signers : [];
    // a choice request: one button per option; the tap signs the message with that option's line appended (the
    // server's `line`, shown under the options, is exactly what is appended). The requester's recommended option is
    // marked, never selected: every option, the recommended one too, signs only on its own tap
    const options = !r.refused && Array.isArray(r.options) ? r.options : [];
    const wi = options.length && r.write_in && typeof r.write_in === "object" ? r.write_in : null;
    if (!r.refused) {
      if (!signers.length && !kept) show(signNote || "no passkey is enrolled for this name");
      // the card's one frozen write-in (set below when it offers one): an option's tap at the write-in's passkey
      // picker releases it, picker and all; a tap while a ceremony runs does nothing, so frozen words stay frozen
      let freeze = null;
      const go = (choice) => () => {
        if (busy) return;
        if (freeze) freeze.release();
        signWith(r, signers, say, show, actions, choice);
      };
      // his pick from the link, on the card the link names only: one button that signs that option, the exact line it
      // appends under it (the listing's, then the challenge's own), drawn under the message so the item is read before
      // the tap, and one line at the card's top saying so. The tap is the gesture: Face ID is never started on load.
      // Every option below still signs on its own tap. The pick holds only on the version he tapped it on: a link
      // without one, a request changed since, or a key the card does not offer draws no button, only a line in that
      // same top slot, so the first screen always says whether his tap was carried
      const isLinked = !!focus && r.id === focus;
      const sameV = !!pickV && typeof r.sha256 === "string" && r.sha256.slice(0, 16) === pickV;
      const po = isLinked && pick && sameV && /^[a-z0-9]{1,8}$/.test(pick) ? options.find((o) => o.key === pick) : null;
      if (po) {
        card.insertBefore(el("div", "pick-top", "Your pick: (" + po.key + "). Read the item; Sign (" + po.key + ") is below it."),
                          head.nextSibling);
        const block = el("div", "picked");
        const pb = el("button", "allow", "Sign (" + po.key + ") with " + unlock);
        pb.type = "button";
        pb.disabled = !usable || !signers.length;
        const pl = el("div", "line", po.line);
        const echo = el("div", "note " + (kept ? kept[1] : ""), kept ? kept[0] : "");
        const sayHere = (text, kind) => { say(text, kind); echo.textContent = text; echo.className = "note " + (kind || ""); };
        const shown = { words: null, signing: (line) => { pl.textContent = line; }, unsure: () => {},
                        release: () => { pl.textContent = po.line; } };
        pb.addEventListener("click", () => {
          if (busy) return;
          if (freeze) freeze.release();
          signWith(r, signers, sayHere, show, actions, po.key, shown);
        });
        block.append(pb, pl, el("div", "why", "you picked (" + po.key + ") on your phone; any other option below still signs instead"), echo);
        card.appendChild(block);
      } else if (isLinked && pick) {
        card.insertBefore(el("div", "where pick-miss", !pickV ? "the link does not say which version of this item you tapped: read it and pick below"
          : !sameV ? "this item changed since your tap: read it and pick again"
          : "the option in the link (" + pick.slice(0, 24) + ") is not on this card: pick one below"), head.nextSibling);
      }
      if (!options.length) {
        const b = el("button", "allow", "sign with " + unlock);
        b.type = "button";
        b.disabled = !usable || !signers.length;
        b.addEventListener("click", go(undefined));
        actions.appendChild(b);
      }
      for (const o of options) {
        const rec = o.key === r.recommended;
        const b = el("button", "allow option" + (rec ? " recommended" : ""), "(" + o.key + ") " + o.text);
        if (rec) b.appendChild(el("span", "rec", "recommended"));
        b.type = "button";
        b.disabled = !usable || !signers.length;
        b.addEventListener("click", go(o.key));
        actions.appendChild(b);
      }
      // a write-in: his own words, signed as the line with the reserved key (*) when he taps its button
      if (wi) {
        const box = el("input", "reason write-in");
        box.type = "text";
        box.maxLength = Number(wi.max) || 200;
        box.placeholder = "or write your own answer";
        box.setAttribute("aria-label", "your own answer to " + (r.title || r.id));
        const draft = writeIns.get(r.id);
        if (draft && draft.sha256 !== (r.sha256 || "")) writeIns.delete(r.id);   // typed on a replaced version
        box.value = writeIns.has(r.id) ? writeIns.get(r.id).words : "";
        box.addEventListener("input", () => writeIns.set(r.id, { sha256: r.sha256 || "", words: box.value }));
        const b = el("button", "allow option", "(" + wi.key + ") sign my words");
        b.type = "button";
        b.disabled = !usable || !signers.length;
        const shown = el("div", "line frozen");
        // the ONE copy of the words being signed: the passkey picker and the sign read `freeze.words` when they run,
        // and the line shown is the server's own (`line`, from the challenge) as soon as it answers. The tap freezes
        // the box until the ceremony ends; release() removes the picker whole, then unlocks the box, so a retry is a
        // new tap that freezes the box's words again. Trimmed, since autocorrect's trailing space is refused
        freeze = {
          words: null,
          signing: (line) => { shown.textContent = "signing: " + line; },
          unsure: () => { shown.textContent = "not sure this was signed: not confirmed"; },
          release: () => {
            for (const p of actions.querySelectorAll(".pick")) p.remove();
            freeze.words = null;
            box.readOnly = box.disabled = false;
            b.disabled = !usable || !signers.length;
            shown.remove();
          },
        };
        b.addEventListener("click", () => {
          if (busy) return;
          const w = box.value.trim();
          if (!w) return show("type your answer first", "bad");
          box.value = w;
          writeIns.set(r.id, { sha256: r.sha256 || "", words: w });
          freeze.words = w;
          box.readOnly = box.disabled = b.disabled = true;
          freeze.signing(wi.line + w);
          box.after(shown);
          signWith(r, signers, say, show, actions, wi.key, freeze);
        });
        actions.append(box, b);
      }
      if (options.length) {
        const lines = el("div", "where lines", "an option signs the boxed message with its line appended (the title "
          + "and sender above are not signed" + (r.recommended ? ", nor is the recommendation" : "") + "):");
        for (const o of options) lines.appendChild(el("div", "line", o.line));
        if (wi) lines.appendChild(el("div", "line", wi.line + "<your words>"));
        card.appendChild(lines);
      }
    }
    const d = el("button", "deny", options.length ? "deny all" : "deny");
    d.type = "button";
    d.addEventListener("click", () => signDeny(r, say));
    actions.appendChild(d);
    card.append(actions, note);
    // an answer whose reply was lost and that the port had not settled may still sign: nothing that signs is offered,
    // and the port is asked about it again; a settled answer redraws the list
    const pend = signUnsettled.get(r.id);
    if (pend && !r.refused) {
      // every control on the card that signs, the deep link's picked button included, not only those in `actions`
      for (const x of card.querySelectorAll("button.allow, button.pick, input")) x.disabled = true;
      if (!signAsking.has(r.id)) {
        signAsking.add(r.id);
        askAttempt(r.id, pend).then((st) => {
          if (signUnsettled.get(r.id) !== pend) return;
          if (st.state === "signed" || st.state === "refused") {
            signUnsettled.delete(r.id);
            if (st.state === "signed") writeIns.delete(r.id);
            say(st.state === "signed" ? "signed (its reply was lost; the port confirmed it)" : refusedText(st),
                st.state === "signed" ? "ok" : "bad");
            setTimeout(load, 0);
            return;
          }
          say((st.state === "pending" ? "the port is still verifying the last answer"
            : "where the last answer stands could not be read (" + (st.why || st.state) + ")")
            + ": not confirmed; this card asks again when the list is read", "bad");
        }).finally(() => signAsking.delete(r.id));
      }
    }
    return card;
  }

  // freeze (a write-in's, else none): the card's one frozen state, read by the picker and the sign when they run
  function signWith(r, signers, say, show, actions, choice, freeze) {
    for (const old of actions.querySelectorAll(".pick")) old.remove();   // a new tap replaces any earlier picker
    const local = mine();
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    if (known.length === 1) return signAnswer(r, known[0], local[known[0]], say, choice, freeze);
    if (!known.length && signers.length === 1) return signAnswer(r, signers[0], null, say, choice, freeze);
    const choices = known.length ? known : signers;
    show("more than one passkey is enrolled for this name; pick the one this phone holds:");
    for (const fp of choices) {
      const p = el("button", "quiet pick", fp.slice(0, 18) + "…");
      p.type = "button";
      p.addEventListener("click", () => signAnswer(r, fp, known.length ? local[fp] : null, say, choice, freeze));
      actions.appendChild(p);
    }
  }

  // only a definite "nothing was signed" releases a frozen write-in: a 4xx from /challenge or /answer, any failure
  // before /answer is sent (no challenge, the passkey cancelled or failed), or the port saying this answer's attempt
  // was refused. After the assertion is sent, no reply, a reply that is not JSON or a 5xx may follow a signature the
  // server already wrote, or one it is still verifying: the port is asked about the attempt (unsure, below)
  function signAnswer(r, fp, rawId, say, choice, freeze) {
    if (busy) return;   // a second activation while a ceremony runs is ignored; it never unfreezes the first's words
    busy++;
    let keep = false;   // signed, or not knowable: the frozen words stay frozen
    let sent = false;   // the assertion went to /answer
    const attempt = b64u(crypto.getRandomValues(new Uint8Array(18)));   // this answer's own id (Signer.attempt)
    // the answer was posted and its reply was lost: the port is asked where this attempt stands. "signed" and "refused"
    // are its word; anything else leaves the words and the card frozen until a listing's card asks again (signCard)
    const unsure = (text) => {
      keep = true;
      if (freeze) freeze.unsure();
      say(text + "; asking the port where it stands…", "bad");
      return askAttempt(r.id, attempt).catch(() => ({ state: "" })).then((st) => {
        if (st.state === "signed") {
          last = "signed " + (st.choice ? "(" + st.choice + ") " : "") + (r.title || r.id);
          if (r.id === focus) focusDone = true;
          writeIns.delete(r.id);
          return say("signed" + (st.choice ? " (" + st.choice + ")" : "") + " · by " + st.signer_fp + " in " + st.namespace
                     + " (its reply was lost; the port confirmed it)", "ok");
        }
        if (st.state === "refused") { keep = false; return say(refusedText(st), "bad"); }
        signUnsettled.set(r.id, attempt);
        return say(text + (st.state === "pending" ? "; the port is still verifying it" : "; where it stands could not be read")
                   + ": not confirmed; this card asks again when the list is read", "bad");
      });
    };
    const base = "/sign/" + encodeURIComponent(r.id) + "/";
    const body = { signer_fp: fp, request_sha256: r.sha256 };
    if (choice !== undefined) body.choice = choice;
    if (freeze && freeze.words != null) body.write_in = freeze.words;   // a picked option's line holder carries no words
    say(choice !== undefined ? "asking for the challenge for (" + choice + ")…" : "asking for the challenge…");
    call("POST", base + "challenge", body).then((c) => {
      if (!c.ok) return say(why(c), "bad");
      // the exact line the signature appends, from the server's bytes: what is shown is what is signed
      if (freeze && typeof c.body.line === "string") freeze.signing(c.body.line);
      say("confirm with your passkey to sign…");
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => {
        say("verifying the signature…");
        sent = true;
        return call("POST", base + "answer", {
          authenticator_data: b64u(cred.response.authenticatorData), client_data_json: b64u(cred.response.clientDataJSON),
          signature: b64u(cred.response.signature), attempt: attempt }).then((a) => {
          // only the server's own refusal (a 4xx) says nothing was signed
          if (!a.ok && a.status >= 400 && a.status < 500) return say("not signed: " + why(a), "bad");
          if (!a.ok) return unsure("not sure this was signed (" + why(a) + ")");
          last = "signed " + (a.body.choice ? "(" + a.body.choice + ") " : "") + (r.title || r.id);
          if (r.id === focus) focusDone = true;
          keep = true;
          writeIns.delete(r.id);
          say("signed" + (a.body.choice ? " (" + a.body.choice + ")" : "") + " · by " + a.body.signer_fp + " in "
              + a.body.namespace, "ok");
        });
      }, (e) => say(declined(e, "nothing was signed"), "bad"));
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) return unsure("not sure this was signed (the page failed: " + name + ")");
      say("the page failed (" + name + "); it re-reads the queue to show where this stands", "bad");
    }).finally(() => { busy--; if (!keep && freeze) freeze.release(); setTimeout(load, 1500); });
  }

  function signDeny(r, say) {
    if (busy) return;
    busy++;
    call("POST", "/sign/" + encodeURIComponent(r.id) + "/deny", { request_sha256: r.sha256 || "" }).then((a) => {
      if (!a.ok) return say("not denied: " + why(a), "bad");
      last = "denied " + (r.title || r.id);
      if (r.id === focus) focusDone = true;
      say("denied", "ok");
    }).catch((e) => say("the deny failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  // a stored stamp (UTC ISO, microseconds) in this phone's local time to the minute; the record keeps the raw stamp,
  // and a value that does not parse is shown as it came
  function readable(iso) {
    const d = typeof iso === "string" ? new Date(iso.replace(/(\.\d{3})\d+/, "$1")) : null;
    if (!d || isNaN(d.getTime())) return String(iso || "");
    return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  // a blob hash: the 12-char head, the whole hash on a tap (what the signature binds is reachable in full)
  function blobTap(full) {
    const short = full.slice(0, 12);
    if (full.length <= 12) return el("code", "dg-val", full);
    const code = el("code", "dg-val", short);
    code.tabIndex = 0;
    code.setAttribute("role", "button");
    let open = false;
    const flip = () => { open = !open; code.textContent = open ? full : short; };
    code.addEventListener("click", flip);
    code.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); flip(); } });
    return code;
  }

  const reasons = new Map();  // job id -> the cancel reason being typed, kept across redraws (open while present)

  // a dispatch job (levain.dispatch.phone): built with the phone app's card markup (row, title, chips, acts) so one
  // restyle covers both. It shows what the passkey signs: the contract's goal in full (exactly what the executor is
  // sent), its facts, where the read files are sent and by what, and the digest, with the read and write lists and
  // every other field the laptop's `levain dispatch show` prints behind "details" (their counts on the closed card).
  // Sign signs the DIGEST (T2); Cancel is the human cancel, signed in its own namespace, with an optional reason. The
  // two are equal weight and nothing here recommends either: the server's words are the only verdicts shown
  function dispatchCard(r) {
    const t = r.template && typeof r.template === "object" ? r.template : {};
    const b = r.budget && typeof r.budget === "object" ? r.budget : {};
    const card = el("article", "row dispatch");
    card.dataset.job = r.id;
    const repo = typeof r.repo === "string" ? r.repo.replace(/\/+$/, "").split("/").pop() : "";
    card.appendChild(el("div", "title", (t.id || "dispatch job") + (repo ? " in " + repo : "")));
    const chips = el("div", "chips");
    const filed = r.state === "filed";
    // the job id and its state are on the closed card (the id is what the picker and the laptop's `levain dispatch show` name)
    for (const text of ["dispatch", "job " + r.id, "state " + r.state, r.agent || "no agent named", filed ? "sign by " + readable(r.lease_end) : r.state,
                        "contract expires " + readable(r.expires_at)]) chips.appendChild(el("span", "chip", text));
    card.appendChild(chips);
    card.appendChild(el("pre", "display", typeof r.goal === "string" ? r.goal : ""));
    const models = Array.isArray(t.models) ? t.models.join(", ") : "";
    const q = (v) => (v === undefined || v === null || v === "" ? "?" : String(v));   // a missing field is "?", never "undefined"
    card.appendChild(el("div", "where facts", [q(t.id), q(t.tier), models + " (k=" + q(t.k) + ")",
      q(b.tool_calls) + " tool calls, " + q(b.tokens) + " tokens, " + q(b.dollars_cents) + " cents",
      (t.verifier_kind || "") + " verifier " + (t.verifier_id || "")].join(" \u00b7 ")));
    // where the read files leave this machine, on the closed card
    card.appendChild(el("div", "where sent", "sent to " + (t.provider || "?") + " via " + (t.route || "?") + " \u00b7 executor " + (t.executor || "?")));
    card.appendChild(el("div", "where", "grant " + (Array.isArray(r.grant) ? r.grant.join(", ") : "") + " over the write list, from "
                        + (r.session || "") + " in " + (r.repo || "") + " at " + (typeof r.base === "string" ? r.base.slice(0, 12) : "")));
    card.appendChild(digestLine("contract digest", r.digest));
    const det = el("details", "lists");
    const files = (n) => Number(n) + (Number(n) === 1 ? " file" : " files");
    det.appendChild(el("summary", null, "details \u00b7 reads " + files(r.read_count) + " \u00b7 writes " + files(r.write_count)));
    for (const [label, entries] of [["read", r.read], ["write", r.write]]) {
      det.appendChild(el("div", "where", label));
      const ul = el("ul", "where");
      for (const e of (Array.isArray(entries) ? entries : [])) {
        const li = el("li", null, e.path + "  ");
        if (e.mode === "new") li.append("(new file)");
        else li.append(e.mode + " ", blobTap(String(e.blob || "")));
        ul.appendChild(li);
      }
      det.appendChild(ul);
    }
    const tools = Array.isArray(t.tool_versions) ? t.tool_versions.map((p) => (Array.isArray(p) ? p.join(" ") : String(p))).join(", ") : "";
    const history = Array.isArray(r.history) ? r.history : [];
    for (const line of [
      "template " + (t.id || "") + " " + (t.content_hash || ""),
      "verifier " + (t.verifier_id || "") + " " + (t.verifier_hash || ""),
      "tools " + (tools || "(none)"),
      "template write paths " + (Array.isArray(t.write_paths) ? t.write_paths.join(", ") : ""),
      "policy " + (r.policy_hash || ""),
      "base " + (r.base || "") + " tree " + (r.base_tree || ""),
      "signer " + (r.signer_fp || "(not signed)"),
      "filed " + (r.filed_at || "") + " \u00b7 lease ends " + (r.lease_end || "") + " \u00b7 expires " + (r.expires_at || ""),
      ...history.map((h) => "history " + h.seq + " " + h.to_state + " " + h.cause + " " + h.at + (h.evidence ? " " + h.evidence : "")),
    ]) det.appendChild(el("div", "where bound", line));
    card.appendChild(det);
    const note = el("div", "note");
    const key = "dispatch\u0000" + r.id;   // never a consent id
    const say = (text, kind) => { notes.set(key, [text, kind || ""]); note.textContent = text; note.className = "note " + (kind || ""); };
    if (notes.has(key)) say(notes.get(key)[0], notes.get(key)[1]);
    const signers = Array.isArray(r.signers) ? r.signers : [];
    if (!signers.length && !notes.has(key)) say(r.signer_note || "no passkey enrolled for this name can sign a dispatch job");
    const acts = el("div", "acts");
    // Cancel's first tap reveals the optional reason; the next tap signs the cancel with whatever it holds (empty too)
    const reasonBox = el("input", "reason");
    reasonBox.type = "text";
    reasonBox.placeholder = "reason (optional)";
    reasonBox.setAttribute("aria-label", "reason for cancelling job " + r.id + " (optional)");
    reasonBox.hidden = !reasons.has(r.id);
    reasonBox.value = reasons.get(r.id) || "";
    reasonBox.addEventListener("input", () => reasons.set(r.id, reasonBox.value));
    // load() holds still while the box has focus; the next poll redraws once it has lost it
    const offer = (label, cancel) => {
      const btn = el("button", "dact", label);
      btn.type = "button";
      btn.disabled = !usable || !signers.length;
      const what = (cancel ? "to CANCEL job " : "to SIGN job ") + r.id;
      btn.addEventListener("click", () => {
        if (cancel && reasonBox.hidden) { reasonBox.hidden = false; reasons.set(r.id, reasonBox.value); reasonBox.focus(); return; }
        start(r, null, signers, say, acts, false, (fp, rawId) => dispatchRun(r, fp, rawId, say, cancel, cancel ? reasonBox.value : ""), what);
      });
      acts.appendChild(btn);
    };
    if (r.can_sign) offer("Sign with passkey", false);
    if (r.can_cancel) offer("Cancel with passkey", true);
    card.append(reasonBox, acts, note);
    return card;
  }

  function dispatchRun(r, fp, rawId, say, cancel, reason) {
    if (busy) return;
    busy++;
    const base = "/dispatch/" + encodeURIComponent(r.id) + "/";
    say("asking for the " + (cancel ? "cancel" : "signature") + " to sign\u2026");
    call("POST", base + (cancel ? "cancel-challenge" : "challenge"), cancel ? { signer_fp: fp, reason: reason || "", request_sha256: r.sha256 } : { signer_fp: fp, request_sha256: r.sha256 }).then((c) => {
      // any 409 on a challenge says the displayed card is no longer true (stale, cancelled, unreadable): the message
      // is shown and the finally's load draws the queue afresh
      if (!c.ok && c.status === 409) return say((cancel ? "not cancelled: " : "not signed: ") + why(c), "bad");
      if (!c.ok) return say(why(c), "bad");
      say("confirm with your passkey to " + (cancel ? "cancel the job" : "sign the contract") + "\u2026");
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => call("POST", base + (cancel ? "cancel" : "answer"), {
        payload: c.body.payload, authenticator_data: b64u(cred.response.authenticatorData),
        client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) }).then((a) => {
        // the passkey signed: once it is sent, only the server's own refusal (a 4xx) says nothing changed
        if (!a.ok && (a.status === 0 || a.status >= 500)) return say("result unknown (" + why(a) + "): reload to see where the job stands", "bad");
        if (!a.ok) return say((cancel ? "not cancelled: " : "not signed: ") + why(a), "bad");
        if (cancel) reasons.delete(r.id);
        last = (cancel ? "cancelled " : "signed ") + (r.template && r.template.id || r.id);
        say((cancel ? "cancelled" : "signed") + " \u00b7 by " + a.body.signer_fp + " \u00b7 job " + a.body.state, "ok");
      }), (e) => say(declined(e, cancel ? "the job is not cancelled" : "nothing was signed"), "bad"));
    }).catch((e) => say("the page failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  // M17: the Grants tab. consent.json carries `grants` (levain.consent.grant_phone): every standing grant the cockpit
  // the consent hook names holds, what it answered (a count and the newest calls, each with the outcome the ledger
  // read) and the passkeys that may revoke it. A revoke is ONE authority link the server builds and holds: the page
  // sends the grant's `instance` as listed, shows the link's record (`line`, exactly what is signed), and only the
  // sheet's own tap asks for Face ID. The page never sends a link; the log verifies the signature and every refusal
  // shown is the server's words. A grant that answered a call which was then refused is the one to look at for a
  // demotion: it leads the list, and its refused calls lead its card. Whether it has one is the server's count over all
  // its calls (`answered.refused_count`), never read off the newest calls it sends
  const grantNotes = new Map();   // grant id + instance -> [text, kind], kept across redraws of that instance only
  let grantLast = "";             // the last revoke made here: the revoked grant leaves the list, its result stays
  const grantCalls = (g) => (g.answered && Array.isArray(g.answered.recent) ? g.answered.recent : []);
  const refusedShown = (g) => grantCalls(g).filter((c) => c && c.outcome === "refused").length;
  const refusedCalls = (g) => Math.max(g.answered && Number.isInteger(g.answered.refused_count) ? g.answered.refused_count : 0,
                                       refusedShown(g));
  // a note is said about one grant instance: the same id made again starts with none
  const noteKey = (g) => (typeof g.id === "string" ? g.id : "") + "\u0000" + (typeof g.instance === "string" ? g.instance : "");

  // a read that failed (no token, a revoked device, the port unreachable) is said on the Grants tab too, and the cards
  // still there are from an earlier read: their revoke buttons are off until a read succeeds and draws them again
  function grantsUnread(text) {
    const list = $("grants-list"), status = $("grants-status");
    if (status) status.textContent = "the grants could not be read: " + text
      + (list && list.children.length ? " · the grants below are from an earlier read; revoke is off until a read succeeds" : "");
    if (list) list.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  }

  function drawGrants(data) {
    const list = $("grants-list"), status = $("grants-status");
    if (!list || !status) return;
    // sort is stable: the server's order holds among grants with and among grants without a refused call
    const rows = (Array.isArray(data.grants) ? data.grants.slice() : [])
      .sort((a, b) => (refusedCalls(b) > 0 ? 1 : 0) - (refusedCalls(a) > 0 ? 1 : 0));
    const listed = new Set(rows.map(noteKey));
    for (const k of Array.from(grantNotes.keys())) if (!listed.has(k)) grantNotes.delete(k);
    const flagged = rows.filter((g) => refusedCalls(g) > 0).length;
    const note = typeof data.grants_note === "string" ? data.grants_note : "";
    // a read the server could not make is said as that, never as "no standing grants"
    status.textContent = (rows.length ? rows.length + (rows.length === 1 ? " standing grant" : " standing grants")
                                        + (flagged ? " · " + flagged + " with a refused call" : "")
                                      : note ? "the grants: " + note : "no standing grants")
      + (grantLast ? " · last: " + grantLast : "");
    const inert = typeof data.inert_note === "string" ? data.inert_note : "";
    list.replaceChildren(...rows.map((g) => grantCard(g, note, inert)));
  }

  function grantCard(g, signerNote, inertNote) {
    const sv = (x) => (typeof x === "string" ? x : "");
    const state = sv(g.state);
    const card = el("article", "req grant" + (state === "live" ? "" : " " + (state || "unknown")) + (refusedCalls(g) ? " demote" : ""));
    card.dataset.grant = sv(g.id);
    const tools = Array.isArray(g.tools) ? g.tools.filter((t) => typeof t === "string").join(", ") : "";
    const head = el("div", "req-head");
    head.append(el("span", "tool", tools || sv(g.kind) || "(grant)"), el("span", "tier" + (state === "live" ? "" : " off"), state),
                el("span", "meta", sv(g.agent) || sv(g.kind)));
    card.appendChild(head);
    if (sv(g.workspace)) card.appendChild(el("div", "where", "in " + g.workspace));
    if (Array.isArray(g.postures) && g.postures.length) card.appendChild(el("div", "where", "modes " + g.postures.join(", ")));
    if (g.mcp_fields && typeof g.mcp_fields === "object") {
      for (const [tool, fields] of Object.entries(g.mcp_fields)) {
        const f = fields && typeof fields === "object" ? Object.entries(fields).map(([k, v]) => k + "=" + v).join(", ") : "";
        card.appendChild(el("div", "where", tool + ": " + f));
      }
    }
    card.appendChild(el("div", "where", (state === "expired" ? "expired " : "until ") + readable(g.expires_at)
                        + " · " + sv(g.kind) + " · " + sv(g.id_shown)));
    if (state === "inert" && inertNote) card.appendChild(el("div", "where", inertNote));
    // what it answered: the count, how many were refused, then the newest calls the server sent, refused ones first
    // (sort is stable, so each group stays newest first). A refused call older than those is counted, not listed
    const a = g.answered && typeof g.answered === "object" ? g.answered : {};
    const count = Number.isInteger(a.count) ? a.count : 0;
    const refused = refusedCalls(g), older = refused - refusedShown(g);
    const calls = grantCalls(g).slice().sort((x, y) => (y.outcome === "refused" ? 1 : 0) - (x.outcome === "refused" ? 1 : 0));
    card.appendChild(el("div", "where answered", count ? "answered " + count + (count === 1 ? " call" : " calls")
                        + (a.last_at ? ", last " + readable(a.last_at) : "")
                        + (refused ? " · " + refused + " refused" + (older > 0 ? ", " + older + " of them older than the calls below" : "") : "")
                        + (calls.length < count ? " · the newest " + calls.length + " of " + count + " below" : "")
                                                      : "answered nothing yet"));
    for (const c of calls) {
      const o = sv(c.outcome) || "unknown";
      card.appendChild(el("div", "call " + o, o + " · " + sv(c.tool) + " · " + readable(c.at) + (sv(c.cwd) ? " · in " + c.cwd : "")));
    }
    const note = el("div", "note");
    const key = noteKey(g);
    const say = (text, kind) => { grantNotes.set(key, [text, kind || ""]); note.textContent = text; note.className = "note " + (kind || ""); };
    if (grantNotes.has(key)) say(grantNotes.get(key)[0], grantNotes.get(key)[1]);
    const signers = Array.isArray(g.signers) ? g.signers : [];
    if (!signers.length && !grantNotes.has(key)) { note.textContent = signerNote || "no passkey here may revoke this grant"; }
    const actions = el("div", "actions");
    const b = el("button", "deny", "revoke with passkey");
    b.type = "button";
    b.disabled = !usable || !signers.length;
    b.addEventListener("click", () => start(g, null, signers, say, actions, false,
                                            (fp, rawId) => revokeAsk(g, fp, rawId, say, actions), "to REVOKE grant " + sv(g.id_shown)));
    actions.appendChild(b);
    card.append(actions, note);
    return card;
  }

  // the bytes a passkey signs to sign the authority link `line` chained to `prevHash` (WebAuthn challenge = the SSHSIG
  // signed-data blob in the levain-authority namespace: "SSHSIG", then namespace, reserved, hash name and the SHA-512 of
  // prevHash + "\n" + line, each as an SSH string), exactly as levain.consent.passkey.challenge builds it
  function linkChallenge(prevHash, line) {
    const enc = (t) => new TextEncoder().encode(t);
    const str = (b) => { const n = b.length, out = new Uint8Array(4 + n); out.set([n >>> 24 & 255, n >>> 16 & 255, n >>> 8 & 255, n & 255]); out.set(b, 4); return out; };
    return crypto.subtle.digest("SHA-512", enc(prevHash + "\n" + line)).then((h) => {
      const parts = [enc("SSHSIG"), str(enc("levain-authority")), str(new Uint8Array(0)), str(enc("sha512")), str(new Uint8Array(h))];
      const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      parts.reduce((at, p) => { out.set(p, at); return at + p.length; }, 0);
      return out;
    });
  }

  // the challenge, then the sheet: the link's record and the tap that asks for Face ID. The page recomputes the
  // challenge from the line it shows and the head the server says it chains to, and puts only that to the passkey: a
  // challenge over anything else is refused, so the line on the sheet is what Face ID signs. From the tap until Face
  // ID is asked, the ask holds the list still (busy) and is this page's closePick: leaving the view or the page drops
  // it (a reply that comes back after that is ignored, and the server's held link lapses unsigned)
  function revokeAsk(g, fp, rawId, say, actions) {
    if (busy) return;
    busy++;
    let released = false, dropped = false, closer = null;
    const release = () => {
      if (released) return;
      released = true;
      if (closePick && closePick === closer) closePick = null;
      busy--;
      setTimeout(load, 1500);
    };
    const drop = () => { if (released) return false; dropped = true; say(""); release(); return true; };
    closePick = closer = drop;
    const base = "/grant/" + encodeURIComponent(g.id) + "/";
    say("asking for the revoke to sign…");
    call("POST", base + "revoke-challenge", { signer_fp: fp, instance: g.instance }).then((c) => {
      if (dropped) return;
      if (!c.ok) { say("not revoked: " + why(c), "bad"); return release(); }
      let link = null;
      try { link = JSON.parse(c.body.line); } catch (e) { link = null; }
      // the server builds the link itself; one that does not name this grant is not put to the passkey
      if (!link || link.op !== "revoke_grant" || link.grant_id !== g.id || typeof c.body.challenge !== "string") {
        say("not revoked: the link the server sent does not name this grant, so nothing was signed", "bad");
        return release();
      }
      if (typeof c.body.prev_hash !== "string") {
        say("not revoked: the server did not say which log head the link chains to, so the page cannot check what " + unlock + " would sign; nothing was signed", "bad");
        return release();
      }
      return linkChallenge(c.body.prev_hash, c.body.line).then((want) => {
        if (dropped) return;
        if (b64u(want) !== c.body.challenge) {
          say("not revoked: the challenge the server sent is not over the link it shows, so nothing was signed", "bad");
          return release();
        }
        sheet(c, want);
      });
    }).catch((e) => { if (dropped) return; say("the page failed (" + ((e && e.name) || "error") + "); nothing was signed", "bad"); release(); });

    function sheet(c, want) {
      const box = el("div", "sheet");
      box.append(el("div", "where", (isIos ? "Face ID" : "Your screen lock") + " signs exactly this authority link (it removes grant " + (g.id_shown || g.id) + "):"),
                 el("pre", "display line", c.body.line));
      const row = el("div", "actions");
      const go = el("button", "deny", "revoke with " + unlock);
      go.type = "button";
      const no = el("button", "quiet", "cancel");
      no.type = "button";
      row.append(go, no);
      box.appendChild(row);
      actions.after(box);
      say("check the link, then revoke with " + unlock);
      const dismiss = () => { if (released) return false; box.remove(); say(""); release(); return true; };
      closePick = closer = dismiss;
      no.addEventListener("click", dismiss);
      go.addEventListener("click", () => {
        if (released || go.disabled) return;
        go.disabled = no.disabled = true;
        if (closePick === dismiss) closePick = null;   // Face ID is being asked: leaving the page no longer dismisses
        closer = null;
        say("confirm with your passkey to revoke…");
        // sent: the assertion has gone to the server, so only its answer says what happened. Everything before that
        // (a credential id that does not decode, a passkey that gives nothing) is said as nothing revoked
        let sent = false, setup = true, asked;
        try {
          const pk = { challenge: want, rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
          if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
          asked = Promise.resolve(navigator.credentials.get({ publicKey: pk }));
          setup = false;
        } catch (e) {
          asked = Promise.reject(e);
        }
        asked.then((cred) => {
          if (!cred || !cred.response) return say("not revoked: the passkey gave nothing to send; nothing was revoked", "bad");
          const assertion = { authenticator_data: b64u(cred.response.authenticatorData),
                              client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
          sent = true;
          return call("POST", base + "revoke", assertion).then((r) => {
            // the passkey signed: once it is sent, only the server's own refusal (a 4xx) says nothing was revoked
            if (!r.ok && (r.status === 0 || r.status >= 500)) return say("result unknown (" + why(r) + "): the list re-reads to show whether the grant is still there", "bad");
            if (!r.ok) return say("not revoked: " + why(r), "bad");
            grantLast = "revoked " + (g.id_shown || g.id);
            say("revoked · link seq " + r.body.seq + (r.body.warning ? " · " + r.body.warning : ""), "ok");
          });
        }, (e) => {
          if (setup) return say("not revoked: the page could not ask the passkey (" + ((e && e.name) || "error") + "); nothing was signed", "bad");
          say(declined(e, "nothing was revoked"), "bad");
        }).catch((e) => say(sent ? "result unknown (the page failed: " + ((e && e.name) || "error") + "): the list re-reads to show where it stands"
                                 : "not revoked: the page failed before anything was sent (" + ((e && e.name) || "error") + "); nothing was revoked", "bad"))
          .finally(() => { box.remove(); release(); });
      });
    }
  }

  // enrolment: the created credential is kept, so a failed key-line call retries only that call, never makes another
  // kept for this tab (across a reload too) until the key line is sent, so a retry never makes a second passkey
  const MADE = "levain-phone-made";
  let made = null;
  try { made = JSON.parse(store.get("sessionStorage", MADE) || "null"); } catch (e) { made = null; }
  const setMade = (v) => { made = v; try { if (v) window.sessionStorage.setItem(MADE, JSON.stringify(v)); else window.sessionStorage.removeItem(MADE); } catch (e) { /* memory only */ } };
  function enrol() {
    const btn = $("enrol-btn"), out = $("enrol-out"), note = $("enrol-note");
    btn.disabled = true;
    busy++;
    const rand = (n) => { const a = new Uint8Array(n); crypto.getRandomValues(a); return a; };
    // the passkey's user name carries when it was made (this phone's local time, to the second) and the head of its
    // random user id, so the phone's password list tells one levain passkey from another even when two share a
    // second; the key's fingerprint does not exist until it is made
    const pad = (n) => String(n).padStart(2, "0"), now = new Date(), uid = rand(16);
    const name = "levain " + now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate())
      + " " + pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds())
      + " " + Array.from(uid.slice(0, 2), (b) => b.toString(16).padStart(2, "0")).join("");
    const create = made ? Promise.resolve(made) : navigator.credentials.create({ publicKey: {
      rp: { id: rpId, name: "levain" }, user: { id: uid, name: name, displayName: name },
      challenge: rand(32), pubKeyCredParams: [{ type: "public-key", alg: -7 }], attestation: "none", timeout: 60000,
      authenticatorSelection: { residentKey: "required", userVerification: "required" } } })
      .then((cred) => { setMade({ rawId: b64u(cred.rawId), spki: b64u(cred.response.getPublicKey()), name: name }); return made; });
    create.then((c) => call("POST", "/passkey/key-line", { spki: c.spki }).then((r) => {
      if (!r.ok) {
        btn.disabled = false;
        // a refused key (422) will never be accepted: drop it so the next tap makes a fresh passkey; anything else
        // (the laptop unreachable, busy) keeps it and retries only the send
        if (r.status === 422) { setMade(null); btn.textContent = "make a passkey here"; } else btn.textContent = "try sending the key again";
        note.textContent = why(r);
        return;
      }
      remember(r.body.fingerprint, c.rawId);
      setMade(null);
      note.textContent = "passkey made (" + r.body.fingerprint + (c.name ? ", named \"" + c.name + "\"" : "")
        + "). On the laptop, run these to enrol it"
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

  // what holds the list still: a ceremony or a consent or dispatch passkey picker (busy), words being typed into a
  // question's box, or a focused cancel reason (a redraw would wipe the words and dismiss the keyboard). A sign
  // request's passkey picker takes no busy: a redraw while it is up rebuilds the card unfrozen, picker gone, with
  // nothing signed
  function held() {
    if (busy) return true;
    if (Array.from(document.querySelectorAll("textarea.words:not(:disabled)")).some((t) => t.value.trim() !== "")) return true;
    return !!(document.activeElement && document.activeElement.matches("input.reason, input.band-box"));
  }

  function load() {
    if (held()) return;
    if (!token) {
      $("status").textContent = "open this page from the link `levain consent phone` printed or wrote to its --link-file (it carries the token)";
      grantsUnread("this page has no token (open it from the link `levain consent phone` printed or wrote to its --link-file)");
      return;
    }
    const seq = storeSeq;     // the identity changes this page had made when the listing was asked for
    call("GET", "/consent.json").then((r) => {
      // re-checked here, not only before the GET: any hold that began while it was in flight keeps the list still
      if (held()) return;
      if (r.ok) draw(r.body, seq); else { $("status").textContent = why(r); grantsUnread(why(r)); }
    });
    loadCoord();
  }

  // K8: a cockpit write signed by this phone's passkey. issue(fp) files the write as a pending under that passkey (the
  // phone app's POST /cockpit/verb, or a cancel) and resolves to the server's answer; the challenge then comes from the
  // server's STORED record, fetched by the pending's id (GET /cockpit/pending/<id>), never from bytes this page holds;
  // review(pending, sign, dismiss) shows that same record and calls sign() from its own Sign tap (a fresh gesture for the
  // passkey prompt), and is required: there is no signing without it. The page stays held (busy) while the sheet is up,
  // until a sign() gets the server's terminal answer or dismiss() is called: by the sheet, by the page hiding, or by
  // the phone app's view change through window.levainDismissReviews (a dismiss during an in-flight sign
  // waits for that sign to settle), so a refresh never redraws it away; sign() runs one ceremony at a time (a second
  // tap does nothing; a non-terminal failure lets the sheet sign again, still held; after dismiss it does nothing). A pending the server no longer holds
  // WAITING (expired, or decided elsewhere) gets no sheet and no prompt. The broker verifies the signature over its own
  // copy and decides; the outcome names the signer the server persisted. done(answer) gets every outcome, and a review
  // that ends without the server's terminal answer ends with a DISMISSED answer: it is one terminal answer, and a
  // non-terminal result (a 4xx, say) may precede DISMISSED, so a caller treats DISMISSED, not the first answer, as the end. review() may answer the sheet's
  // closed(reason): it is called once when the review ends ("answered" or "dismissed"), so the sheet takes its Sign
  // away; a dismissal from outside the sheet (the page hiding, the view change) is also said. This page decides nothing:
  // what it shows is the server's words.
  // every open review's dismiss, so a view change can close them all (window.levainDismissReviews below)
  const openReviews = new Set();
  function dismissReviews() {
    const open = Array.from(openReviews);
    open.forEach((d) => d());
    return open.length;
  }
  function signPending(issue, fp, rawId, say, review, done) {
    if (busy) return;
    busy++;
    const finish = (a) => { if (done) done(a); };
    const failed = (e) => ({ ok: false, status: 0, body: { message: (e && e.name) || "error" } });
    say("filing the write to sign…");
    Promise.resolve(issue(fp)).then((p) => {
      if (!p || !p.ok) { finish(p || failed()); return say(p ? why(p) : "the write was not filed", "bad"); }
      if (p.body.state !== "WAITING") { finish(p); return say("the write is " + p.body.state, "bad"); }
      const base = "/cockpit/pending/" + encodeURIComponent(p.body.pending_id);
      return call("GET", base).then((c) => {
        if (!c.ok) { finish(c); return say(why(c), "bad"); }
        // the server's own state, re-read: an expired or already decided pending is never put to the passkey
        if (c.body.state !== "WAITING" || !c.body.challenge) {
          finish(c);
          return say("the write is " + c.body.state + (c.body.reason ? " (" + c.body.reason + ")" : "") + "; nothing to sign", "bad");
        }
        return new Promise((settle, fail) => {
          // one sign lifecycle per review. signing: a ceremony or its POST is in flight (one at a time); dismissed: the
          // sheet closed; released: the hold (busy) is given back, which happens exactly once: on a terminal answer
          // (the server's own 2xx), on dismiss when no sign is in flight, or when the sign in flight at a dismiss
          // settles. A non-terminal failure (a 4xx, a cancelled passkey, an unknown outcome) re-arms Sign and keeps the
          // hold while the sheet is open. After dismiss or release, sign() does nothing, however late it is called
          // answered: the server's terminal answer ended this review; away: it was dismissed from outside the sheet
          // (the page hid, the view changed); closed: what review() answered, the sheet's own closed(reason), told
          // once at release so the sheet never shows a live Sign over a review that is over; said: the last line this
          // review said, kept so a dismissal that waited on an in-flight sign is said with that sign's result
          let signing = false, dismissed = false, released = false, answered = false, away = false, closed = null, said = null;
          const tell = (text, kind) => { said = [text, kind]; say(text, kind); };
          const DEAD = "this review was dismissed (the page was hidden or the view changed), so its Sign does nothing: "
            + "start the write again to sign it";
          // L3 r3: for the review's lifetime a hidden page (a switched tab, a backgrounded PWA) dismisses it, and so
          // does the phone app's view change (window.levainDismissReviews), so the hold never freezes the list
          // behind a sheet nobody can see. Both hooks go at release, exactly once
          const onHide = () => { if (document.hidden) leave(); };
          const unhook = () => { document.removeEventListener("visibilitychange", onHide); openReviews.delete(leave); };
          // release: exactly once. A review that ends without the server's terminal answer was dismissed: done() gets a
          // DISMISSED answer (so a caller awaiting it never hangs), a dismissal from outside the sheet is said, and the
          // sheet's closed() is told either way. Nothing here says nothing was signed: an earlier attempt may have sent
          // its signature with an unknown result
          // Each notification (done, say, closed) is isolated: one that throws must never keep the hold (busy), so
          // settle() runs in a finally whatever they do
          const isolated = (f) => { try { f(); } catch (e) { try { console.error("levainSign: a review callback threw", e); } catch (_) { /* no console */ } } };
          let closedTold = false;
          const tellClosed = () => {
            if (closedTold || typeof closed !== "function") return;
            closedTold = true;
            isolated(() => closed(answered ? "answered" : "dismissed"));
          };
          const release = () => {
            if (released) return;
            released = true;
            try {
              unhook();
              if (!answered) {
                isolated(() => finish({ ok: false, status: 0, body: { state: "DISMISSED", message: "the review was dismissed" } }));
                if (away) isolated(() => say(said && said[1] === "bad" && said[0] !== DEAD ? said[0] + " · " + DEAD : DEAD, "bad"));
              }
              tellClosed();
            } finally {
              settle();
            }
          };
          const dismiss = () => { if (released) return; dismissed = true; if (!signing) release(); };
          const leave = () => { if (released) return; away = true; dismiss(); };
          document.addEventListener("visibilitychange", onHide);
          openReviews.add(leave);
          const sign = () => {
            const say = tell;        // what this review says is kept (said), for a dismissal that waits on this sign
            if (released && away && !answered) return say(DEAD, "bad");   // a stale Sign over a review dismissed from outside says so
            if (signing || dismissed || released) return;
            signing = true;
            // sent: the signature has gone to the server, so only its own answer says what happened; told: done() has
            // this attempt's answer; terminal: the server answered 2xx, so this review is decided
            let sent = false, told = false, terminal = false;
            say("confirm with your passkey to apply " + c.body.verb + " (" + c.body.tier + ")…");
            const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
            if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
            return navigator.credentials.get({ publicKey: pk }).then((cred) => {
              const assertion = { fingerprint: fp, authenticator_data: b64u(cred.response.authenticatorData),
                                  client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
              sent = true;
              return call("POST", base + "/signature", assertion);
            }).then((a) => {
              terminal = a.ok;
              told = true;
              finish(a);
              // the passkey signed: once it is sent, only the server's own refusal (a 4xx) says nothing was applied
              if (!a.ok && (a.status === 0 || a.status >= 500)) return say("result unknown (" + why(a) + "): reload to see whether it applied", "bad");
              if (!a.ok) return say("not applied: " + why(a), "bad");
              // the signer named is the one the server persisted, never the fingerprint this page sent
              say(a.body.state + (a.body.reason ? " (" + a.body.reason + ")" : "") + (a.body.signer_fp ? " · signed by " + a.body.signer_fp : ""),
                  a.body.state === "COMMITTED" ? "ok" : "bad");
            }).catch((e) => {
              if (!sent) {
                finish(failed(e));
                return say(declined(e, "nothing was applied"), "bad");
              }
              // a fault after the send: the server may have applied it, so this page never says it did not
              say("result unknown (the page failed: " + ((e && e.name) || "error") + "): reload to see whether it applied", "bad");
              if (!told) finish(failed(e));
            }).finally(() => { signing = false; if (terminal) answered = true; if (terminal || dismissed) release(); });
          };
          try {
            closed = review(c.body, sign, dismiss);
            if (released) tellClosed();      // a review that released during review() itself (a sync dismiss) is told once, now
          } catch (e) { if (!released) { released = true; unhook(); } fail(e); }
        });
      });
    }).catch((e) => { finish(failed(e)); say("the page failed (" + ((e && e.name) || "error") + ")", "bad"); })
      .finally(() => { busy--; });
  }
  // the ONE hook the phone app signs through (Q4 ruling b): the consent ceremony's own start (the passkey picker), with
  // signPending as its continuation. No second copy of the ceremony lives in phone_app.js.
  window.levainSign = (signers, say, actions, issue, review, done) => {
    // the passkey signs only what a review sheet showed (B5 condition 3): with no review there is no signature, and
    // nothing is filed
    if (typeof review !== "function") {
      say("nothing was signed: a write is signed only from its review sheet", "bad");
      if (done) done({ ok: false, status: 0, body: { message: "no review sheet" } });
      return;
    }
    start(null, null, signers, say, actions, false, (fp, rawId) => signPending(issue, fp, rawId, say, review, done));
  };
  // the hook point a view change calls (the phone app's tab/view switch): dismiss every open review, under the same
  // rule as the sheet's own dismiss (a sign in flight releases when it settles). Answers how many it dismissed
  window.levainDismissReviews = dismissReviews;

  // ---- the drop sheet (coordinator C1/C2: levain.coordinator.phone) ---------------------------------------------------
  // A drop is your words to the coordinator, the same drop `levain drop` makes. Send puts it in this phone's OUTBOX
  // first, under an outbox id picked once, then sends it; it leaves the outbox only on the server's 2xx, which IS the
  // delivered receipt. A send whose answer never came (no network, a 5xx) stays and is sent again under the SAME id, so
  // the store files it once however often it arrives (a resend answers created:false). A refusal (a 4xx) stays,
  // marked, until you send it again or discard it. An ENVELOPE (where it may work, a land, a budget, a stop) is built
  // by the server with the CLI's own builder; the sheet shows the server's own words for it, and only the sheet's Sign
  // asks for the passkey, over the challenge the server built. The signed envelope rides in the outbox with its drop.
  // A land named here is always marked "inferred, not where you dropped from": the phone records no branch it dropped
  // from, and that mark is inside what the passkey signs. This page decides nothing: every verdict is the server's.
  // WHO: the coordinator routes take a PAIRED device's token only; this run's phone link is refused (device_required),
  // said on the outbox card as why it is not delivered yet. WHERE: every outbox item is bound to the coordinator store
  // identity this page held when the item was made (coord_store_id, null while there is no store). Nothing is sent
  // until this page load has READ the current identity, and an item bound to another identity is shown as "from
  // another Levain", never sent: a phone name that now answers for another dispatch directory never files it. On a
  // fresh install the envelope REVIEW makes the store: the signed drop is bound to the id the review's reply names,
  // and a reply that says its request made the store (store_created) rebinds every item made while there was none.
  // The review carries the identity the page read and is held to the drop's rule, so a non-null identity the page
  // read is never replaced from a review reply: a name that now answers for another store refuses the review.
  // An item made while there was no store that meets one it never saw made gets ONE receipt-only retry per identity:
  // the server answers it from its resend lookup (its own first send made the store, and the reply was lost) or
  // refuses it other_store with nothing written, and then it is "from another Levain".
  // CROSS-TAB: every outbox change runs under one Web Lock (navigator.locks) and re-reads the outbox inside it, and a
  // change another tab made (the storage event) redraws this one. A SIGNED drop the server refuses is never filed
  // plain behind your back: a stale signature (envelope-stale) offers Sign again, and every refused signed drop offers
  // Send as a plain drop (the same outbox id, the envelope dropped, your words unchanged).
  const OUTBOX_KEY = "levain-outbox:" + location.hostname;
  const STORE_KEY = "levain-store-id:" + location.hostname;
  let outboxMem = null;       // the outbox when this browser keeps no storage (private mode): this visit only
  let flushing = false;
  let dropData = null;        // the last listing's coord_* keys
  let dropLast = "";          // the last delivery said here, kept across redraws
  let storeRead = false;      // this page load has read the store identity (flush waits for it)
  let storeId = null;         // that identity (null: no store yet)
  let storeSeq = 0;           // counts the identity changes this page made itself (a reply that made the store)
  const rememberedStore = () => { const v = store.get("localStorage", STORE_KEY); return v === null ? undefined : v === "" ? null : v; };
  const stamp = () => new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000Z");   // the store's fixed-width UTC form
  const outboxId = () => {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return "ph-" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  };
  function outboxRead() {
    if (outboxMem) return outboxMem.slice();
    try { const v = JSON.parse(window.localStorage.getItem(OUTBOX_KEY) || "[]"); return Array.isArray(v) ? v : []; }
    catch (e) { return []; }
  }
  function outboxWrite(items) {
    try { window.localStorage.setItem(OUTBOX_KEY, JSON.stringify(items)); outboxMem = null; }
    catch (e) { outboxMem = items.slice(); }
  }
  // the one way the outbox changes: f(all) edits the array read INSIDE the lock, then it is written back
  function withOutbox(f) {
    const run = () => { const all = outboxRead(); f(all); outboxWrite(all); };
    if (navigator.locks && typeof navigator.locks.request === "function") return navigator.locks.request(OUTBOX_KEY, run);
    return Promise.resolve().then(run);
  }
  function outboxEdit(id, f) {
    return withOutbox((all) => {
      const i = all.findIndex((x) => x.outbox_id === id);
      if (i < 0) return;
      const next = f(all[i]);
      if (next === null) all.splice(i, 1); else all[i] = next;
    });
  }
  const foreign = (x) => storeRead && x.store_id !== storeId;     // made against another store identity
  // a foreign item made while there was no store, not yet asked of this identity: its one receipt-only retry
  const unasked = (x) => foreign(x) && x.store_id === null && x.asked !== storeId;
  // a reply whose request made the store: every item made while there was none belongs to it
  function adoptStore(sid) {
    storeSeq++;
    storeId = sid;
    storeRead = true;
    store.set("localStorage", STORE_KEY, sid);
    return withOutbox((all) => { for (const x of all) if (x.store_id === null) x.store_id = sid; });
  }
  function dropSay(text, kind) {
    const n = $("drop-note");
    if (!n) return;
    n.textContent = text;
    n.className = "note " + (kind || "");
  }
  function enqueue(item) {
    return withOutbox((all) => {
      const i = all.findIndex((x) => x.outbox_id === item.outbox_id);
      if (i < 0) all.push(item); else all[i] = item;          // Sign again replaces its own item, same outbox id
    }).then(() => { drawOutbox(); flush(); });
  }
  // the identity an item made now is bound to: this load's, else the last one this phone read here
  function boundStore() {
    if (storeRead) return storeId;
    return rememberedStore();
  }
  // a refusal is a 4xx the server named (the words, the outbox id, the envelope, another store): sending it unchanged
  // gets the same answer, so it waits for you. Anything else (no answer, a 5xx, the token refused) is "not delivered yet"
  const refusal = (status) => status === 400 || status === 409 || status === 413 || status === 422;
  function flush() {
    if (flushing || !token || !storeRead) return;
    const next = outboxRead().find((x) => x.state === "waiting" && (!foreign(x) || unasked(x)));
    if (!next) return;
    flushing = true;
    let again = false;
    const req = { words: next.words, outbox_id: next.outbox_id, sent_at: next.sent_at, store_id: next.store_id };
    if (next.envelope) req.envelope = next.envelope;
    call("POST", "/coord/drop", req).then((a) => {
      if (a.ok) {
        const sid = typeof a.body.store_id === "string" ? a.body.store_id : null;
        // an item made while there was no store, accepted: this reply's request made the store, so every other item
        // this phone made here before any store existed belongs to it
        const made = next.store_id === null && sid !== null;
        if (made) { storeSeq++; storeId = sid; store.set("localStorage", STORE_KEY, sid); }
        const e = a.body.envelope;
        const d = a.body.drop || {};
        dropLast = "delivered " + d.drop_id + (a.body.created ? "" : " (it had arrived before: filed once)")
          + (e ? (e.taken ? " · envelope " + e.envelope_id + " taken" : " · envelope NOT taken: " + e.refused) : "")
          + (a.body.woken ? " · coordinator launch " + a.body.woken + " asked for" : "");
        dropSay(dropLast, e && !e.taken ? "bad" : "ok");
        again = true;
        setTimeout(load, 0);
        return withOutbox((all) => {
          const i = all.findIndex((x) => x.outbox_id === next.outbox_id);
          if (i >= 0) all.splice(i, 1);
          if (made) for (const x of all) if (x.store_id === null) x.store_id = sid;
        });
      }
      const x0 = { why: why(a), error: a.body.error || "" };
      if (a.body.error === "other_store" && next.store_id === null) {
        // an item made while there was no store, nothing written: this identity has answered for the item (it shows as
        // another Levain's once the listing names the store), and the identity is read again. An item bound to a store
        // is refused below with the server's words: it cannot be sent as it stands
        setTimeout(load, 0);
        return outboxEdit(next.outbox_id, (x) => Object.assign({}, x, { asked: storeId }, x0));
      }
      if (refusal(a.status)) {
        again = true;
        return outboxEdit(next.outbox_id, (x) => Object.assign({}, x, { state: "refused" }, x0));
      }
      return outboxEdit(next.outbox_id, (x) => Object.assign({}, x, { tries: (x.tries || 0) + 1 }, x0));
    }).finally(() => { flushing = false; drawOutbox(); if (again) flush(); });
  }
  function firstLine(words, n) {
    const line = String(words || "").split("\n")[0];
    return line.length > n ? line.slice(0, n) + "…" : line;
  }
  function button(text, f) {
    const b = el("button", null, text);
    b.type = "button";
    b.addEventListener("click", f);
    return b;
  }
  function drawOutbox() {
    const box = $("drop-outbox");
    if (!box) return;
    const items = outboxRead();
    $("drop-outbox-meta").textContent = (items.length ? items.length + " not delivered" : "empty")
      + (outboxMem ? " · this browser keeps no storage: the outbox lasts this visit only" : "")
      + (navigator.locks ? "" : " · one tab at a time: this browser cannot lock the outbox between tabs");
    box.replaceChildren(...items.map((x) => {
      const other = foreign(x);
      const refused = !other && x.state === "refused";
      const card = el("article", "row drop outbox" + (refused || other ? " refused" : ""));
      card.dataset.outbox = x.outbox_id;
      card.appendChild(el("div", "title", firstLine(x.words, 80)));
      const chips = el("div", "chips");
      const state = other ? "from another Levain" : refused ? "refused" : "waiting to be delivered";
      for (const text of [state, x.envelope ? "with a signed envelope" : "plain", "typed " + readable(x.sent_at)])
        chips.appendChild(el("span", "chip" + (refused || other ? " warn" : ""), text));
      card.appendChild(chips);
      if (other) card.appendChild(el("div", "note bad", "made for another Levain than the one answering here now; "
        + "it is not sent here. Your words are above: copy them into a new drop, or discard this one"));
      else if (x.why) card.appendChild(el("div", "note " + (refused ? "bad" : ""), (refused ? "refused: " : "not delivered yet (") + x.why + (refused ? "" : "); it is sent again")));
      const acts = el("div", "acts");
      if (refused && x.envelope) {
        // Sign again: the same words and outbox id, reviewed and signed afresh (a stale signature's remedy); both
        // retries take a fresh sent_at, as Send again does (the refusal may be of the time it was typed)
        if (x.spec && x.signer_fp) acts.appendChild(button("Sign again", () => envelopeSheet(x.signer_fp, x.raw_id || null, x.words, x.spec, x.outbox_id)));
        acts.appendChild(button("Send as a plain drop", () => {
          outboxEdit(x.outbox_id, (y) => Object.assign({}, y, { state: "waiting", why: "", error: "", envelope: null, sent_at: stamp() })).then(() => { drawOutbox(); flush(); });
        }));
      } else if (refused) {
        // a new send moment: the server's refusal may be of the time it was typed (a phone clock ahead of the laptop's)
        acts.appendChild(button("Send again", () => {
          outboxEdit(x.outbox_id, (y) => Object.assign({}, y, { state: "waiting", why: "", error: "", sent_at: stamp() })).then(() => { drawOutbox(); flush(); });
        }));
      }
      acts.appendChild(button("Discard", () => { outboxEdit(x.outbox_id, () => null).then(drawOutbox); }));
      card.appendChild(acts);
      return card;
    }));
  }
  const RECEIPT = { delivered: "delivered", split: "split", resplit: "regrouped", woken: "coordinator" };
  function dropCard(d) {
    const card = el("article", "row drop sent");
    card.dataset.drop = d.drop_id;
    card.appendChild(el("div", "title", firstLine(d.words, 120)));
    const chips = el("div", "chips");
    for (const text of ["drop " + d.drop_id, "delivered " + readable(d.received_at)]) chips.appendChild(el("span", "chip", text));
    if (d.envelope_id) chips.appendChild(el("span", "chip", "envelope " + d.envelope_id));
    else if (d.envelope_refused) chips.appendChild(el("span", "chip warn", "envelope not taken"));
    card.appendChild(chips);
    // every drop receipt, oldest first, in the store's own words: the split names its threads, the wake how it went
    const receipts = Array.isArray(d.receipts) ? d.receipts : [];
    for (const r of receipts) {
      const threads = Array.isArray(r.threads) ? r.threads : [];
      const named = threads.map((t) => String(t.kind || "").toUpperCase() + " " + (t.title || t.thread_id)).join("; ");
      let line = "receipt: " + (RECEIPT[r.stage] || r.stage) + " " + readable(r.at);
      if (r.stage === "woken") line += " · launch " + r.launch_id + (r.started ? " started: " : " not started: ") + (r.said || "");
      else if (r.stage !== "delivered") line += " · " + threads.length + (threads.length === 1 ? " thread" : " threads") + (named ? ": " + named : "");
      if (r.envelope_refused) line += " · envelope not taken: " + r.envelope_refused;
      card.appendChild(el("div", "where receipt" + (r.stage === "woken" && !r.started ? " bad" : ""), line));
    }
    if (!receipts.some((r) => r.stage === "split")) card.appendChild(el("div", "where", "not split into threads yet"));
    return card;
  }
  function drawDrops(data, seq) {
    dropData = data;
    const list = $("drop-recent");
    if (!list) return;
    const drops = Array.isArray(data.coord_drops) ? data.coord_drops : [];
    const note = typeof data.coord_note === "string" && data.coord_note ? data.coord_note : "";
    $("drop-recent-meta").textContent = note ? "can't read the coordinator's store: " + note : drops.length ? "newest first" : "none yet";
    list.replaceChildren(...drops.map(dropCard));
    const repos = Array.isArray(data.coord_repos) ? data.coord_repos : [];
    $("drop-repos").replaceChildren(...repos.map((r) => { const o = el("option"); o.value = r; return o; }));
    if (dropLast && !$("drop-note").textContent) dropSay(dropLast, "ok");
    // the identity is taken only from a listing that read the store (an unreadable store says nothing about it), and
    // only when this page has not learned the identity from a reply since the listing was asked for: that listing read
    // the store before the reply that made it, and the next one names it
    if (data.coord_store_read === true && (seq === undefined || seq === storeSeq)) {
      storeRead = true;
      storeId = typeof data.coord_store_id === "string" ? data.coord_store_id : null;
      store.set("localStorage", STORE_KEY, storeId === null ? "" : storeId);
    }
    drawOutbox();
    flush();
  }
  function dropWords() {
    const w = $("drop-words").value;          // stored verbatim: only an empty drop is refused here
    if (!w.trim()) { dropSay("type your words first", "bad"); return null; }
    return w;
  }
  if ($("drop-send")) $("drop-send").addEventListener("click", () => {   // a page without the Drop tab wires none
    const words = dropWords();
    if (words === null) return;
    const sid = boundStore();
    if (sid === undefined) return dropSay("still reading which Levain answers here; send it in a moment", "bad");
    $("drop-words").value = "";
    dropSay("in the outbox; sending…");
    enqueue({ outbox_id: outboxId(), words: words, sent_at: stamp(), state: "waiting", envelope: null, store_id: sid });
  });
  function dropSpec() {
    const repo = $("drop-repo").value.trim(), onto = $("drop-onto").value.trim();
    if (!repo) return null;
    const n = (id) => { const v = $(id).value.trim(); return v === "" ? 0 : Number(v); };
    const spec = { where: [{ repo: repo, paths: ["."] }], lands: onto ? [{ repo: repo, onto: onto }] : [],
                   stop: $("drop-stop").value, budget: { tool_calls: n("drop-calls"), tokens: n("drop-tokens"), hours: n("drop-hours") } };
    return spec;
  }
  let closeSheet = null;      // the open envelope sheet's dismiss (it holds busy while it is up)
  if ($("drop-sign")) $("drop-sign").addEventListener("click", () => {
    const words = dropWords();
    if (words === null) return;
    const spec = dropSpec();
    if (!spec) { $("drop-env").open = true; return dropSay("name the repo the envelope lets it work in", "bad"); }
    const signers = dropData && Array.isArray(dropData.coord_signers) ? dropData.coord_signers : [];
    if (!usable) return dropSay("this browser cannot use a passkey here (needs https)", "bad");
    if (!signers.length) return dropSay((dropData && dropData.coord_signer_note) || "no passkey enrolled for this name can sign an envelope", "bad");
    start(null, null, signers, dropSay, $("drop-acts"), false, (fp, rawId) => envelopeSheet(fp, rawId, words, spec, null), "to SIGN this drop's envelope");
  });
  // the review (the server keeps the body it shows), then Sign: the server mints the payload, signed_at now, and the
  // passkey signs its challenge. `again` is the outbox id of a refused signed drop being signed again (same words)
  function envelopeSheet(fp, rawId, words, spec, again) {
    if (busy || closeSheet) return;
    let sid = again ? (outboxRead().find((x) => x.outbox_id === again) || {}).store_id : boundStore();
    if (sid === undefined) return dropSay("still reading which Levain answers here; sign it in a moment", "bad");
    busy++;
    const id = again || outboxId();      // picked before the review: the server refuses one already delivered
    const box = $("drop-review");
    const text = $("drop-words");
    dropSay(again ? "building the envelope to sign again…" : "building the envelope to sign…");
    let signing = false, closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      closeSheet = null;
      openReviews.delete(close);
      box.hidden = true;
      box.replaceChildren();
      text.disabled = false;
      $("drop-send").disabled = $("drop-sign").disabled = false;
      busy--;
    };
    // the review carries the identity this page read and the server holds it to the drop's rule: a name that now
    // answers for another store refuses it (other_store), so nothing is signed for a store this page did not read
    call("POST", "/coord/envelope-review", { words: words, outbox_id: id, spec: spec, signer_fp: fp, store_id: sid }).then((c) => {
      if (!c.ok) { close(); return dropSay("nothing to sign: " + why(c), "bad"); }
      // a fresh install's review made the store: only then does a null identity take the reply's (a non-null one the
      // page read is never replaced from a reply)
      const rsid = typeof c.body.store_id === "string" ? c.body.store_id : null;
      const made = rsid !== null && c.body.store_created === true;
      if (made) adoptStore(rsid).then(drawOutbox);
      if (sid === null && made) sid = rsid;
      closeSheet = close;
      openReviews.add(close);   // a view change or a hidden page dismisses it (window.levainViewChanged)
      text.disabled = true;     // the words the envelope names are frozen while it is reviewed
      // and the sheet's own Sign / Dismiss are the only acts: a Send now would file the same words a second time
      $("drop-send").disabled = $("drop-sign").disabled = true;
      $("drop-env").open = true;
      const view = Array.isArray(c.body.view) ? c.body.view.join("\n") : "";
      const acts = el("div", "acts");
      const sign = el("button", "go", "Sign with passkey");
      sign.type = "button";
      const dismiss = el("button", null, "Dismiss");
      dismiss.type = "button";
      dismiss.addEventListener("click", () => { if (!signing) { close(); dropSay("not signed; your words are still here"); } });
      sign.addEventListener("click", () => {
        if (signing || closed) return;
        signing = true;
        dropSay("confirm with your passkey to sign the envelope…");
        call("POST", "/coord/envelope-challenge", { review_id: c.body.review_id }).then((ch) => {
          if (!ch.ok) { signing = false; return dropSay("nothing was signed: " + why(ch), "bad"); }
          const pk = { challenge: unb64u(ch.body.challenge), rpId: ch.body.rp_id, userVerification: "required", timeout: 60000 };
          if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
          return navigator.credentials.get({ publicKey: pk }).then((cred) => {
            const envelope = { body_text: c.body.body_text, payload: ch.body.payload,
                               authenticator_data: b64u(cred.response.authenticatorData),
                               client_data_json: b64u(cred.response.clientDataJSON), signature: b64u(cred.response.signature) };
            signing = false;
            close();
            if (!again) text.value = "";
            dropSay("signed; in the outbox, sending…");
            const prior = again ? outboxRead().find((x) => x.outbox_id === again) : null;
            // Sign again keeps its item's binding, unless the review made the store it was made without
            const bound = prior ? (prior.store_id === null && c.body.store_created === true ? rsid : prior.store_id) : sid;
            enqueue({ outbox_id: id, words: words, sent_at: stamp(), state: "waiting", envelope: envelope,
                      store_id: bound, spec: spec, signer_fp: fp, raw_id: rawId || null });
          }, (e) => { signing = false; dropSay(declined(e, "nothing was signed; sign again or dismiss"), "bad"); });
        }, () => { signing = false; dropSay("the phone port could not be reached; nothing was signed", "bad"); });
      });
      acts.append(sign, dismiss);
      box.replaceChildren(el("h2", null, "what your passkey signs"), el("pre", "display", view),
                          digestLine("envelope digest", c.body.digest), acts);
      box.hidden = false;
      dropSay("read the envelope, then sign it");
    }, () => { close(); dropSay("the phone port could not be reached; nothing was signed", "bad"); });
  }
  // another tab changed the outbox (or the identity): redraw from storage, and send what is now this tab's to send
  window.addEventListener("storage", (e) => { if (e.key === OUTBOX_KEY || e.key === null) { drawOutbox(); flush(); } });
  window.addEventListener("online", flush);
  drawOutbox();
  flush();
  setInterval(flush, 10000);

  $("refresh").addEventListener("click", load);
  $("enrol-btn").addEventListener("click", enrol);
  load();
  setInterval(load, 10000);
})();
