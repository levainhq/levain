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
  const forget = (fp) => { const all = mine(); delete all[fp]; store.set("localStorage", mapKey(), JSON.stringify(all)); };
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
    // undecided: the hold ended with no answer and the call did not run; the agent was told gap #N is open and
    // waits on a late ruling, signed here in the ruling namespace (never an answer's)
    const gaps = Array.isArray(data.undecided) ? data.undecided : [];
    // statements a caller filed for this phone's passkey to sign (`levain consent phone --sign-namespace`)
    const signs = Array.isArray(data.sign_pending) ? data.sign_pending : [];
    // questions an agent filed: answered by a tap or words, unsigned (an answer authorises nothing)
    const asks = Array.isArray(data.questions) ? data.questions : [];
    const waiting = rows.length + gaps.length + signs.length + asks.length;
    const unread = (Number(data.sign_overflow) > 0 ? " \u00b7 " + Number(data.sign_overflow) + " more sign requests not shown" : "")
      + (Number(data.questions_overflow) > 0 ? " \u00b7 " + Number(data.questions_overflow) + " more questions not shown" : "");
    const qnote = typeof data.questions_note === "string" && data.questions_note ? data.questions_note : "";
    // a store fault is never "nothing waiting": the page says it cannot see the agents' questions
    $("status").textContent = (waiting ? waiting + " waiting on you" : qnote ? "can't tell what is waiting on you" : "nothing is waiting on you")
      + (qnote ? " \u00b7 agent questions unavailable: " + qnote : "") + unread + (last ? " \u00b7 last: " + last : "");
    list.replaceChildren(...asks.map(questionCard), ...signs.map((r) => signCard(r, data.sign_note)), ...rows.map((r) => row(r, false)),
                         ...gaps.map((r) => row(r, true)));
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

  // a question: text only (never HTML) for everything the agent wrote. No passkey: the answer is unsigned and
  // authorises nothing; the server records it once and delivers it to the agent's session
  function questionCard(q) {
    const card = el("article", "req ask");
    const head = el("div", "req-head");
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

  function row(r, ruling) {
    const card = el("article", "req");
    const head = el("div", "req-head");
    head.append(el("span", "tool", r.tool || "(tool)"), el("span", "tier" + (r.tier === "T3" ? " t3" : ""), r.tier || ""),
                el("span", "meta", (r.agent || "") + (ruling ? " \u00b7 undecided, gap " + (r.gap_label || "#" + r.gap) : "")));
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

  function start(r, decision, signers, say, actions, ruling, go) {
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
    say("more than one passkey is enrolled for this name; pick the one this phone holds:");
    for (const old of actions.querySelectorAll(".pick")) old.remove();
    for (const fp of choices) {
      const p = el("button", "quiet pick", fp.slice(0, 18) + "…");
      p.type = "button";
      p.addEventListener("click", () => run(fp, known.length ? local[fp] : null));
      actions.appendChild(p);
    }
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
          if (rawId && e && e.name === "NotAllowedError") forget(fp);
          again();
          say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was answered or allowed", "bad");
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
              (e) => {
                // asked for one credential and the OS had none it would use: forget it here, so the next tap is not
                // pinned to a passkey this phone no longer holds
                if (rawId && e && e.name === "NotAllowedError") forget(fp);
                say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was answered", "bad");
              });
    }).catch((e) => say("the page failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  // a sign request: the whole message is shown as text (never HTML) and is exactly what the passkey signs (the
  // challenge and the deny send back the sha256 of the file this card was built from, so a file changed since is
  // refused); a request the server refused (a character that can show differently from its bytes, a malformed file)
  // shows only why, and can be denied. A note kept across redraws belongs to the version it was made on: a changed
  // file drops it, and what the server says of the request now (refused, no passkey) is shown, never kept
  function signCard(r, signNote) {
    const card = el("article", "req sign");
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
    if (!r.refused) {
      if (!signers.length && !kept) show(signNote || "no passkey is enrolled for this name");
      const b = el("button", "allow", "sign with Face ID");
      b.type = "button";
      b.disabled = !usable || !signers.length;
      b.addEventListener("click", () => signWith(r, signers, say, show, actions));
      actions.appendChild(b);
    }
    const d = el("button", "deny", "deny");
    d.type = "button";
    d.addEventListener("click", () => signDeny(r, say));
    actions.appendChild(d);
    card.append(actions, note);
    return card;
  }

  function signWith(r, signers, say, show, actions) {
    const local = mine();
    const known = signers.filter((fp) => Object.prototype.hasOwnProperty.call(local, fp));
    if (known.length === 1) return signAnswer(r, known[0], local[known[0]], say);
    if (!known.length && signers.length === 1) return signAnswer(r, signers[0], null, say);
    const choices = known.length ? known : signers;
    show("more than one passkey is enrolled for this name; pick the one this phone holds:");
    for (const old of actions.querySelectorAll(".pick")) old.remove();
    for (const fp of choices) {
      const p = el("button", "quiet pick", fp.slice(0, 18) + "…");
      p.type = "button";
      p.addEventListener("click", () => signAnswer(r, fp, known.length ? local[fp] : null, say));
      actions.appendChild(p);
    }
  }

  function signAnswer(r, fp, rawId, say) {
    if (busy) return;
    busy++;
    const base = "/sign/" + encodeURIComponent(r.id) + "/";
    say("asking for the challenge…");
    call("POST", base + "challenge", { signer_fp: fp, request_sha256: r.sha256 }).then((c) => {
      if (!c.ok) return say(why(c), "bad");
      say("confirm with your passkey to sign…");
      const pk = { challenge: unb64u(c.body.challenge), rpId: c.body.rp_id, userVerification: "required", timeout: 60000 };
      if (rawId) pk.allowCredentials = [{ type: "public-key", id: unb64u(rawId) }];
      return navigator.credentials.get({ publicKey: pk }).then((cred) => {
        say("verifying the signature…");
        return call("POST", base + "answer", {
          authenticator_data: b64u(cred.response.authenticatorData), client_data_json: b64u(cred.response.clientDataJSON),
          signature: b64u(cred.response.signature) }).then((a) => {
          // the passkey signed: once the answer is sent, only the server's own refusal (a 4xx) says nothing was
          // signed; no reply, a proxy error or any 5xx may follow a signature the server already wrote
          if (!a.ok && (a.status === 0 || a.status >= 500)) return say("result unknown (" + why(a) + "): reload to see whether it was signed", "bad");
          if (!a.ok) return say("not signed: " + why(a), "bad");
          last = "signed " + (r.title || r.id);
          say("signed · by " + a.body.signer_fp + " in " + a.body.namespace, "ok");
        });
      }, (e) => {
        if (rawId && e && e.name === "NotAllowedError") forget(fp);
        say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was signed", "bad");
      });
    }).catch((e) => say("the page failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
  }

  function signDeny(r, say) {
    if (busy) return;
    busy++;
    call("POST", "/sign/" + encodeURIComponent(r.id) + "/deny", { request_sha256: r.sha256 || "" }).then((a) => {
      if (!a.ok) return say("not denied: " + why(a), "bad");
      last = "denied " + (r.title || r.id);
      say("denied", "ok");
    }).catch((e) => say("the deny failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
      .finally(() => { busy--; setTimeout(load, 1500); });
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
    const create = made ? Promise.resolve(made) : navigator.credentials.create({ publicKey: {
      rp: { id: rpId, name: "levain" }, user: { id: rand(16), name: "levain operator", displayName: "levain operator" },
      challenge: rand(32), pubKeyCredParams: [{ type: "public-key", alg: -7 }], attestation: "none", timeout: 60000,
      authenticatorSelection: { residentKey: "required", userVerification: "required" } } })
      .then((cred) => { setMade({ rawId: b64u(cred.rawId), spki: b64u(cred.response.getPublicKey()) }); return made; });
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
    // a redraw would wipe words being typed into a question's box (and dismiss the keyboard)
    if (Array.from(document.querySelectorAll("textarea.words:not(:disabled)")).some((t) => t.value.trim() !== "")) return;
    if (!token) { $("status").textContent = "open this page from the link `levain consent phone` printed (it carries the token)"; return; }
    call("GET", "/consent.json").then((r) => { if (busy) return; if (r.ok) draw(r.body); else $("status").textContent = why(r); });
  }

  $("refresh").addEventListener("click", load);
  $("enrol-btn").addEventListener("click", enrol);
  load();
  setInterval(load, 10000);
})();
