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
  if (m || fm) history.replaceState(null, "", location.pathname);
  // a link opened on a page already showing (a fragment-only navigation does not reload it): read it afresh
  window.addEventListener("hashchange", () => { if (/(?:^#|&)(?:sign|phone_token)=/.test(location.hash)) location.reload(); });

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
  let focusDone = false;      // the deep link's card was signed or denied here
  let focusMisses = 0;        // listings the linked card was not in: three, and the link is let go
  const notes = new Map();    // request id -> [text, kind], kept across redraws
  const writeIns = new Map(); // sign request id -> {sha256, words}: the write-in being typed, kept across redraws
                              // for the version it was typed on only

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
    // dispatch jobs a planner filed (levain dispatch): a contract to sign with a passkey, or a live job to cancel
    const jobs = Array.isArray(data.dispatch_pending) ? data.dispatch_pending : [];
    // notes an agent filed: read and dismissed, never answered; they wait on nothing, so they are not counted as waiting
    const memos = Array.isArray(data.notes) ? data.notes : [];
    // words typed for a request no longer listed (signed, denied, expired) go with it, so a reused id starts empty
    for (const id of writeIns.keys()) if (!signs.some((r) => r.id === id)) writeIns.delete(id);
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
      + unread + (last ? " \u00b7 last: " + last : "");
    // a deep-linked sign request comes first and is scrolled to, once; one not listed is said, never silently lost
    const linked = focus ? signs.filter((r) => r.id === focus) : [];
    const rest = linked.length ? signs.filter((r) => r.id !== focus) : signs;
    list.replaceChildren(...linked.map((r) => signCard(r, data.sign_note, true)), ...asks.map(questionCard),
                         ...rest.map((r) => signCard(r, data.sign_note)), ...jobs.map(dispatchCard),
                         ...rows.map((r) => row(r, false)), ...gaps.map((r) => row(r, true)),
                         ...(memos.length ? [el("h2", "notes-head", "Notes · nothing to answer"), ...memos.map(noteCard)] : []));
    // said once: after the linked card is signed or denied here the link goes without a note; otherwise it is let go
    // after three listings without it (a passing sign-side fault gets two more). A link opened mid-ceremony reloads
    // the page and the result shows on the reload
    if (linked.length) focusMisses = 0;                                    // consecutive listings without it
    if (focus && !linked.length && !busy && (focusDone || ++focusMisses >= 3)) {
      if (!focusDone && !data.sign_note)
        $("status").textContent += " \u00b7 the linked sign request " + focus + " is not open here (signed, denied, expired or not filed)";
      focus = "";
    }
    if (linked.length && !draw.scrolled) { draw.scrolled = true; list.firstChild.scrollIntoView({ block: "start" }); }
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
      if (!options.length) {
        const b = el("button", "allow", "sign with Face ID");
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
          unsure: () => { shown.textContent = "not sure this was signed: check the list"; },
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
  // before /answer is sent (no challenge, the passkey cancelled or failed). After the assertion is sent, no reply,
  // a reply that is not JSON or a 5xx may follow a signature the server already wrote: the words stay frozen and
  // the next redraw, built from the server's list, says where it stands
  function signAnswer(r, fp, rawId, say, choice, freeze) {
    if (busy) return;   // a second activation while a ceremony runs is ignored; it never unfreezes the first's words
    busy++;
    let keep = false;   // signed, or not knowable: the frozen words stay frozen
    let sent = false;   // the assertion went to /answer
    const unsure = (text) => { keep = true; if (freeze) freeze.unsure(); say(text, "bad"); };
    const base = "/sign/" + encodeURIComponent(r.id) + "/";
    const body = { signer_fp: fp, request_sha256: r.sha256 };
    if (choice !== undefined) body.choice = choice;
    if (freeze) body.write_in = freeze.words;
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
          signature: b64u(cred.response.signature) }).then((a) => {
          // only the server's own refusal (a 4xx) says nothing was signed
          if (!a.ok && a.status >= 400 && a.status < 500) return say("not signed: " + why(a), "bad");
          if (!a.ok) return unsure("not sure this was signed (" + why(a) + "): check the list");
          last = "signed " + (a.body.choice ? "(" + a.body.choice + ") " : "") + (r.title || r.id);
          if (r.id === focus) focusDone = true;
          keep = true;
          writeIns.delete(r.id);
          say("signed" + (a.body.choice ? " (" + a.body.choice + ")" : "") + " · by " + a.body.signer_fp + " in "
              + a.body.namespace, "ok");
        });
      }, (e) => {
        if (rawId && e && e.name === "NotAllowedError") forget(fp);
        say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was signed", "bad");
      });
    }).catch((e) => {
      const name = (e && e.name) || "error";
      if (sent) return unsure("not sure this was signed (the page failed: " + name + "): check the list");
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
    call("POST", base + (cancel ? "cancel-challenge" : "challenge"), cancel ? { signer_fp: fp, reason: reason || "" } : { signer_fp: fp }).then((c) => {
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
      }), (e) => {
        if (rawId && e && e.name === "NotAllowedError") forget(fp);
        say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); " + (cancel ? "the job is not cancelled" : "nothing was signed"), "bad");
      });
    }).catch((e) => say("the page failed (" + ((e && e.name) || "error") + "); it re-reads the queue to show where this stands", "bad"))
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
    return !!(document.activeElement && document.activeElement.matches("input.reason"));
  }

  function load() {
    if (held()) return;
    if (!token) { $("status").textContent = "open this page from the link `levain consent phone` printed (it carries the token)"; return; }
    call("GET", "/consent.json").then((r) => {
      // re-checked here, not only before the GET: any hold that began while it was in flight keeps the list still
      if (held()) return;
      if (r.ok) draw(r.body); else $("status").textContent = why(r);
    });
  }

  // K8: a cockpit write signed by this phone's passkey. issue(fp) files the write as a pending under that passkey (the
  // phone app's POST /cockpit/verb, or a cancel) and resolves to the server's answer; the challenge then comes from the
  // server's STORED record, fetched by the pending's id (GET /cockpit/pending/<id>), never from bytes this page holds;
  // review(pending, sign, dismiss) shows that same record and calls sign() from its own Sign tap (a fresh gesture for the
  // passkey prompt), and is required: there is no signing without it. The page stays held (busy) while the sheet is up,
  // until a sign() gets the server's terminal answer or the sheet calls dismiss() (a dismiss during an in-flight sign
  // waits for that sign to settle), so a refresh never redraws it away; sign() runs one ceremony at a time (a second
  // tap does nothing; a non-terminal failure lets the sheet sign again, still held; after dismiss it does nothing). A pending the server no longer holds
  // WAITING (expired, or decided elsewhere) gets no sheet and no prompt. The broker verifies the signature over its own
  // copy and decides; the outcome names the signer the server persisted. done(answer) gets every outcome. This page decides nothing:
  // what it shows is the server's words.
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
          let signing = false, dismissed = false, released = false;
          const release = () => { if (released) return; released = true; settle(); };
          const dismiss = () => { if (released) return; dismissed = true; if (!signing) release(); };
          const sign = () => {
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
                if (rawId && e && e.name === "NotAllowedError") forget(fp);
                finish(failed(e));
                return say("the passkey did not sign (" + ((e && e.name) || "cancelled") + "); nothing was applied", "bad");
              }
              // a fault after the send: the server may have applied it, so this page never says it did not
              say("result unknown (the page failed: " + ((e && e.name) || "error") + "): reload to see whether it applied", "bad");
              if (!told) finish(failed(e));
            }).finally(() => { signing = false; if (terminal || dismissed) release(); });
          };
          try { review(c.body, sign, dismiss); } catch (e) { released = true; fail(e); }
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

  $("refresh").addEventListener("click", load);
  $("enrol-btn").addEventListener("click", enrol);
  load();
  setInterval(load, 10000);
})();
