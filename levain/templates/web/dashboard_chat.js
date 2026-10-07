// levain dashboard_chat.js — the cockpit's chat panel: talk to a registered entity from the browser.
//
// Present only when the server was started with `--chat` (GET /chat.json answers 200; a plain cockpit answers 404 and
// nothing is added). Like every route but the page shell, the /chat routes need the launch token the server printed at
// start. token.js holds it (the URL fragment, this tab's session storage, the unlock form); this file only sends it and,
// on a refusal, hands the page back to that one unlock form. A refusal before the token is entered adds no panel: the
// probe re-runs on the unlock.
//
// ⛔ CONSENT SURFACE. A turn that halts on a gated action reports it as `pending`; nothing here may approve
// it on the operator's behalf. The one POST to /chat/approve lives inside the Approve button's click handler
// and is guarded by event.isTrusted, so a script-synthesised click does nothing. It is never reached from a
// poll callback, a timer, a retry or another control, and both decision buttons are disabled for as long as
// a decision is in flight, so one click is at most one POST. Rejecting is the safe direction and may ride a
// retry-free click too. All server text (reply, tool detail, errors) is model/entity output: textContent only.
(function () {
  "use strict";
  const bar = document.querySelector("nav.tabs");
  if (!bar) return;

  const POLL_MS = 1500;
  const POLL_FAILS_MAX = 5;
  const auth = window.LevainToken;   // token.js, loaded first; without it every request goes untokened and is refused
  let session = null;        // {id, entity} once a session is open
  let panel = null, body = null;
  let run = 0;               // bumped when a session ends, so a late poll of an old job cannot paint the new one
  let deciding = false;      // an approve/reject POST or its job is in flight
  let confirmedJob = null;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  // One fetch wrapper: JSON in, {status, json, sent} out (`sent`: the launch token the request carried); a network
  // failure is a status of 0, never a thrown error.
  function api(method, path, payload) {
    const sent = auth ? auth.get() : null;
    const headers = { Accept: "application/json" };
    if (auth) auth.headers(headers);
    const init = { method: method, headers: headers, cache: "no-store" };
    if (payload !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(payload); }
    return fetch(path, init)
      .then((r) => r.json().catch(() => ({})).then((j) => ({ status: r.status, json: j || {}, sent: sent })))
      .catch(() => ({ status: 0, json: {}, sent: sent }));
  }
  function isTokenRefusal(r) { return r.status === 403 && r.json && r.json.error === "launch_token"; }
  // A token refusal is a JSON body like any other: it cannot prove the request it answered did not run (a proxy can
  // send it after forwarding). So after the page has sent a request that changes something (open, turn, decision,
  // close), or while it follows one, the refusal never reads as "refused": the page drops the session (nothing more
  // can be decided from it) and says the outcome is unknown.
  const TOKEN_LOST_MID_REQUEST = "The token was not accepted, so this page has let go of the session. The outcome " +
    "of the last request is unknown; it may already have run. Enter the token again at the top of the page.";
  function why(r) {
    if (r.status === 0) return "could not reach the server";
    return (r.json && (r.json.message || r.json.error)) || ("status " + r.status);
  }

  function ensurePanel() {
    if (panel) return;
    panel = el("section", "panel chat-panel");
    panel.setAttribute("data-zone", "operate");
    panel.setAttribute("aria-label", "chat with an entity");
    panel.appendChild(el("div", "chat-head", "▶ Chat"));
    body = el("div", "chat-body");
    panel.appendChild(body);
    const board = document.getElementById("board");
    if (board && board.parentNode) board.parentNode.insertBefore(panel, board);
    else bar.parentNode.appendChild(panel);
  }
  function note(cls, text) { const p = el("p", cls, text); body.appendChild(p); return p; }

  // ---- a refused token ---------------------------------------------------------------------------------------
  // Whatever token led here is not used again: the session is let go (nothing more can be decided from it), the
  // panel says why when there is something to say, and token.js shows the one unlock form. `r` is the refused answer.
  // The message is also kept for the picker the unlock brings back, so entering the token does not erase the word
  // that the last request's outcome is unknown. A refusal of a token the page has already replaced is late news and
  // changes nothing (codex L3 r1 HIGH): the replaced token was already refused, so it says nothing about a session
  // opened since with the new one, and letting go of that session would strand it on the server.
  let lostNote = null;
  function showTokenPrompt(message, r) {
    if (r && auth && auth.get() && r.sent !== auth.get()) return;
    session = null; run++; deciding = false;
    lostNote = message || null;
    if (message) { ensurePanel(); clear(body); note("chat-err", message); }
    else if (panel) { panel.remove(); panel = null; }
    if (auth) auth.lock(null, r ? r.sent : undefined);
  }
  if (auth) auth.onUnlock(() => loadListing(true));

  // ---- entity picker -----------------------------------------------------------------------------------------
  function loadListing(afterToken) {
    api("GET", "/chat.json").then((r) => {
      if (r.status === 404) { if (panel) { panel.remove(); panel = null; } return; }  // not a chat cockpit (or gone)
      if (isTokenRefusal(r)) {
        // Before a session there is nothing to let go of: no panel, just the unlock form (and a word if the token
        // just entered was the one refused).
        showTokenPrompt(null, r);
        if (afterToken && auth && !auth.get()) auth.lock("That token was not accepted.");
        return;
      }
      if (r.status !== 200 || !Array.isArray(r.json.entities)) {
        ensurePanel(); clear(body);
        note("chat-err", "Chat is unavailable: " + why(r));
        return;
      }
      showPicker(r.json);
    });
  }

  function showPicker(listing) {
    session = null; run++; deciding = false;
    ensurePanel(); clear(body);
    if (lostNote) { note("chat-err", lostNote); lostNote = null; }
    if (!listing.entities.length) { note("chat-note", "No entities are registered for chat."); return; }
    // One entity: one click starts a session on it. The picker appears only when there is a choice to make.
    const row = el("div", "chat-row");
    let sel = null;
    if (listing.entities.length > 1) {
      sel = el("select", "chat-input");
      sel.setAttribute("aria-label", "entity to chat with");
      listing.entities.forEach((name) => { const o = el("option", null, name); o.value = name; sel.appendChild(o); });
      row.appendChild(sel);
    } else {
      row.appendChild(el("span", "chat-note", listing.entities[0]));
    }
    const open = el("button", "chat-btn", "Start session");
    open.type = "button";
    open.addEventListener("click", () => { open.disabled = true; openSession(sel ? sel.value : listing.entities[0]); });
    row.appendChild(open);
    body.appendChild(row);
    note("chat-note dim", "model " + (listing.model || "?") + " · a session holds the entity's hands; close it when done.");
  }

  // ---- job polling (reads only; never POSTs) -----------------------------------------------------------------
  function poll(jobId, myRun, onEnd, onStatus) {
    let fails = 0;
    function tick() {
      if (myRun !== run) return;
      api("GET", "/chat/job.json?id=" + encodeURIComponent(jobId)).then((r) => {
        if (myRun !== run) return;
        if (isTokenRefusal(r)) { showTokenPrompt(TOKEN_LOST_MID_REQUEST, r); return; }
        if (r.status !== 200) {
          fails += 1;
          if (fails >= POLL_FAILS_MAX) { onEnd({ status: "lost", error: "lost contact with the server: " + why(r) }); return; }
          setTimeout(tick, POLL_MS);
          return;
        }
        fails = 0;
        const j = r.json;
        if (j.status === "running" || j.status === "pending") {
          if (onStatus) onStatus(j);
          setTimeout(tick, POLL_MS);
          return;
        }
        onEnd(j);
      });
    }
    tick();
  }

  // ---- open ---------------------------------------------------------------------------------------------------
  function openSession(entity) {
    api("POST", "/chat/open", { entity: entity }).then((r) => {
      if (isTokenRefusal(r)) { showTokenPrompt(TOKEN_LOST_MID_REQUEST, r); return; }
      if (r.status !== 202 || !r.json.session_id) {
        clear(body); note("chat-err", "Could not open a session: " + why(r));
        const back = el("button", "chat-btn", "Back"); back.type = "button";
        back.addEventListener("click", () => loadListing(false)); body.appendChild(back);
        return;
      }
      const sid = r.json.session_id, myRun = run;
      confirmedJob = r.json.job_id;
      clear(body); note("chat-note", "Opening a session on " + entity + "…");
      poll(r.json.job_id, myRun, (j) => {
        const st = j.result && j.result.session && j.result.session.state;
        if (j.status === "done" && st === "idle") { session = { id: sid, entity: entity, workspace: j.result.session.workspace }; showConversation(); return; }
        clear(body);
        note("chat-err", "The session did not open: " + (j.error || (j.result && j.result.session && j.result.session.error) || j.status));
        const back = el("button", "chat-btn", "Back"); back.type = "button";
        back.addEventListener("click", () => loadListing(false)); body.appendChild(back);
      });
    });
  }

  // ---- conversation ---------------------------------------------------------------------------------------------
  let log = null, form = null, area = null, sendBtn = null, closeBtn = null, live = null;

  function showConversation() {
    clear(body);
    const head = el("div", "chat-row chat-sess");
    head.appendChild(el("span", "chat-note", "session · " + session.entity));
    closeBtn = el("button", "chat-btn quiet", "Close session"); closeBtn.type = "button";
    closeBtn.addEventListener("click", closeSession);
    head.appendChild(closeBtn);
    body.appendChild(head);
    log = el("div", "chat-log"); log.setAttribute("aria-live", "polite");
    body.appendChild(log);
    live = el("div", "chat-live"); body.appendChild(live);
    form = el("form", "chat-compose");
    area = el("textarea", "chat-input chat-area"); area.rows = 3;
    area.setAttribute("aria-label", "message");
    sendBtn = el("button", "chat-btn", "Send"); sendBtn.type = "button";
    form.appendChild(area); form.appendChild(sendBtn);
    // A turn is sent only by a trusted activation of Send (a click, or Enter/Space on the focused button, which the
    // browser delivers as a trusted click: keyboard access is kept on purpose, Phill 2026-10-05) or by Enter in the
    // compose box (below). Never by the form's submit event: a script's form.requestSubmit() fires a submit the browser
    // marks trusted, so submit only prevents default. Nothing else sends, and no key decides a held action.
    form.addEventListener("submit", (ev) => { ev.preventDefault(); });
    sendBtn.addEventListener("click", (ev) => { if (ev.isTrusted) sendTurn(); });
    // Enter sends and Shift+Enter starts a new line, in THIS box only. Not while an input method is composing a
    // character (that Enter belongs to the IME). Nothing in the consent box listens for Enter: deciding is a click.
    // Composition is tracked from its own events too: some browsers give the Enter that confirms a composed
    // character neither isComposing nor keyCode 229.
    let composing = false;
    area.addEventListener("compositionstart", () => { composing = true; });
    area.addEventListener("compositionend", () => { setTimeout(() => { composing = false; }, 0); });
    area.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing || ev.keyCode === 229 || composing) return;
      ev.preventDefault();
      // Only a real keypress, and never while compose is disabled or a decision is in flight.
      if (!ev.isTrusted || area.disabled || deciding) return;
      sendTurn();
    });
    body.appendChild(form);
  }

  // A textarea that starts one line tall and grows with what is typed.
  function autoGrow(t) {
    t.rows = 1;
    const fit = () => { t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; };
    t.addEventListener("input", fit);
    return fit;
  }
  function setComposeEnabled(on) { if (area) area.disabled = !on; if (sendBtn) sendBtn.disabled = !on; }
  function bubble(kind, who) {
    const b = el("div", "chat-msg " + kind);
    b.appendChild(el("div", "chat-who", who));
    log.appendChild(b);
    return b;
  }
  function addLines(parent, lines) {
    if (!lines || !lines.length) return;
    const ul = el("ul", "chat-acts");
    lines.forEach((l) => ul.appendChild(el("li", null, String(l))));
    parent.appendChild(ul);
  }

  function sendTurn() {
    const text = area.value.trim();
    if (!text || !session || area.disabled || deciding) return;
    area.value = "";
    retireChecks();
    const mine = bubble("me", "you"); mine.appendChild(el("div", "chat-text", text));
    setComposeEnabled(false);
    const myRun = run;
    api("POST", "/chat/turn", { session_id: session.id, message: text }).then((r) => {
      if (myRun !== run) return;
      if (isTokenRefusal(r)) { showTokenPrompt(TOKEN_LOST_MID_REQUEST, r); return; }
      if (r.status !== 202 || !r.json.job_id) {
        // No response from this page can prove who wrote it (a proxy can answer a JSON 4xx/503 after forwarding the
        // request), so anything but a 202 with a job may have started the turn.
        failure("The turn was not confirmed: " + why(r));
        ambiguousStop(myRun, "turn", null); return;
      }
      confirmedJob = r.json.job_id;
      followJob(r.json.job_id, myRun);
    });
  }

  // A reply the server marked `unreadable_call` is the model's raw tool-call syntax, not an answer: that call failed to
  // parse and did not run. It is shown as the notice, with the text collapsed beneath it and escaped by the consent
  // surface's allowlist (visible), never as the entity's message: the bubble is levain's, not the entity's. When the
  // same turn ran other actions the notice says so, never "nothing ran". The sentences are
  // levain.firing.agent_reply's (a test holds them equal).
  const UNREADABLE_CALL_NOTICE = "The model tried to call a tool, but its call couldn't be read, so nothing ran. Ask again, or switch models.";
  const UNREADABLE_CALL_AFTER_ACTIONS_NOTICE = "The model tried to call a tool, but its last call couldn't be read, so that call did not run. The actions listed with this message did run. Ask again, or switch models.";
  function replyText(b, res, ran) {
    if (res.unreadable_call !== true) { b.appendChild(el("div", "chat-text", res.reply)); return; }
    const acted = ran || (Array.isArray(res.tool_activity) && res.tool_activity.length > 0);
    b.appendChild(el("div", "chat-text chat-unreadable", acted ? UNREADABLE_CALL_AFTER_ACTIONS_NOTICE : UNREADABLE_CALL_NOTICE));
    const d = el("details", "chat-raw");
    d.appendChild(el("summary", null, "What the model sent"));
    d.appendChild(el("pre", "chat-text", visible(res.reply, true)));
    b.appendChild(d);
  }
  function replyBubble(res) {
    const b = bubble("them", res.unreadable_call === true ? "levain" : session.entity);
    replyText(b, res); addLines(b, res.tool_activity);
  }

  function failure(text) { const b = bubble("err", "error"); b.appendChild(el("div", "chat-text", text)); }

  // Shows a job's progress, then its outcome. Reads only: a gated outcome renders the consent surface and stops.
  function followJob(jobId, myRun) {
    live.textContent = "working…";
    poll(jobId, myRun, (j) => {
      live.textContent = "";
      const res = j.result;
      // "lost" (contact lost) and "unknown" (the server no longer holds the job) say nothing about what ran.
      if (j.status === "lost" || j.status === "unknown") { failure(j.error || "This page lost track of the turn."); ambiguousStop(myRun, "turn", jobId); return; }
      if (j.status !== "done" || !res) {   // failed: never rendered as success
        failure(j.error || ("the job ended as “" + j.status + "”"));
        endOfTurn(true); return;
      }
      if (res.error) failure(res.error);
      if (res.timed_out) failure("The turn timed out before it finished.");
      if (!res.error && res.gated && Array.isArray(res.pending) && res.pending.length) {
        if (res.reply) replyBubble(res);
        showConsent(res.pending, res.decision_id, myRun);
        return;
      }
      if (res.gated && !res.error) failure("The turn halted on a gated action but reported nothing to decide.");
      if (res.reply) {
        replyBubble(res);
      } else if (!res.error && !res.timed_out && !res.gated) {
        failure("The entity returned no reply.");
      }
      endOfTurn(res.gated ? true : false);
    }, (j) => { live.textContent = (j.activity && j.activity.length) ? "working… " + j.activity[j.activity.length - 1] : "working…"; });
  }
  function endOfTurn(stuck) { setComposeEnabled(!stuck); if (!stuck && area) area.focus(); }

  // ---- the consent surface ------------------------------------------------------------------------------------
  // An ALLOWLIST: printable ASCII (U+0020 to U+007E) renders as itself, a backslash as \\, and EVERY other code
  // point (tab, CR, all non-ASCII: letters, emoji and look-alikes such as a Cyrillic "a" included, and lone
  // surrogates) as \u{XXXX}. keepNewline leaves LF literal, for the one multi-line field (the whole action);
  // anywhere else a newline would forge the layout. Injective by construction: a backslash in the output only
  // starts "\\" or "\u{...}", so a typed "\u{41}" shows as "\\u{41}" and never reads as the escape for "A".
  // levain.firing.gate.visible is the same rule; the two must change together.
  // Whether a held action's text can stand as "the whole action": a string with something in it other than spaces
  // and line feeds. levain.firing.gate.shown_in_full is the same rule (the server and the REPL); the two must
  // change together.
  function shownInFull(full) { return typeof full === "string" && /[^ \n]/.test(full); }
  function visible(text, keepNewline) {
    let out = "";
    for (const ch of String(text)) {   // by code point, so a lone surrogate is one unit
      const cp = ch.codePointAt(0);
      if (ch === "\\") out += "\\\\";
      else if (ch === "\n" && keepNewline) out += ch;
      else if (cp >= 0x20 && cp <= 0x7E) out += ch;
      else out += "\\u{" + cp.toString(16).toUpperCase().padStart(4, "0") + "}";
    }
    return out;
  }
  // AMBIGUOUS OUTCOME: the page cannot tell whether its last turn or decision ran. It NEVER builds a consent box on
  // its own here: it says the outcome is unknown, blocks compose, and offers one button. Only a trusted click on it
  // reads the session again, and whatever that read shows carries this warning.
  // `jobId` is the lost request's job when its start was confirmed (a lost poll, an evicted job), else null (the
  // request got no 202). `before` is the last job this page saw start before it: what "nothing new started" means.
  function ambiguousStop(myRun, what, jobId) {
    const ctx = { what: what, jobId: jobId || null, before: jobId ? null : confirmedJob,
      warning: "The outcome of the last " + what + " is unknown; the previous request may already have run." };
    failure(ctx.warning);
    setComposeEnabled(false);
    rereadButton(myRun, ctx);
  }
  // Every "Check what happened" row on the page. A check answers for ONE lost request, so the moment the page sends
  // another (a turn or a decision) every row is retired: a stale check could otherwise read the new request's job as
  // the old one's outcome.
  let checkRows = [];
  function retireChecks() { checkRows.forEach((r) => r.remove()); checkRows = []; }
  function rereadButton(myRun, ctx) {
    retireChecks();
    const row = el("div", "chat-row");
    checkRows.push(row);
    const btn = el("button", "chat-btn", "Check what happened"); btn.type = "button";
    btn.addEventListener("click", (ev) => {
      if (!ev.isTrusted || myRun !== run || !session) return;
      retireChecks();
      resync(myRun, ctx);
    });
    row.appendChild(btn); log.appendChild(row);
  }
  // The server no longer knows this session: a restart ends every chat session (they live in its memory), and the
  // per-launch token changes with it. Said plainly, with the outcome still unknown and where to look.
  function whereToLook() {
    return (session && typeof session.workspace === "string" && session.workspace)
      ? "the entity's workspace (" + visible(session.workspace) + ")" : "the entity's workspace";
  }
  // A 404 on the check: the server no longer has the session (a restart, or it ended and was cleaned up).
  function goneText() {
    return "The server no longer has that session (it restarted, or the session ended and was cleaned up). Your last " +
      "request may or may not have run: check " + whereToLook() + " to see what changed.";
  }
  function restartedText() {
    return "The server restarted, so that session has ended. Your last request may or may not have run: check " +
      whereToLook() + " to see what changed.";
  }
  // What the session's most recent job did, as the server recorded it: the actions that ran and the reply. Text only;
  // a consent box is never built from it (only from the session's current held set, below). True when it could say
  // what the finished job did, so the outcome is no longer unknown.
  function reportLastJob(last) {
    if (!last || typeof last !== "object") return false;
    const what = last.kind === "turn" ? "turn" : last.kind === "open" ? "open" : "decision (" + String(last.kind) + ")";
    if (last.status === "running") { failure("The last " + what + " is still running."); return false; }
    if (last.status === "unknown") { failure("The server no longer has a record of the last " + what + "."); return false; }
    if (last.status === "failed") { failure("The last " + what + " failed: " + (last.error || "no detail")); return false; }
    const res = last.result || {};
    const ran = Array.isArray(res.tool_activity) ? res.tool_activity : [];
    const b = bubble("them", (res.unreadable_call === true ? "levain" : session.entity) + " · what the last " + what + " did");
    b.appendChild(el("div", "chat-text", ran.length ? "These actions ran:" : "No action ran."));
    addLines(b, ran);
    if (res.reply) replyText(b, res, ran.length > 0);
    if (res.error) b.appendChild(el("div", "chat-text", "error: " + res.error));
    return true;
  }
  // Asks the server what the session holds NOW (GET /chat/session.json) and rebuilds the screen from that. Called
  // only from the "Check what happened" button after an ambiguous outcome; `warning` is attached to whatever is shown.
  // A gated session comes back with its held set and, while it is still approvable, its CURRENT decision id (the id
  // only ever comes from the server's answer). Reads only.
  // Which of the server's records answers for the lost request. The session's last job counts only when it IS that
  // request: the same job id when its start was confirmed, or (no 202 came back) a job that started after the last one
  // this page saw. Anything else is not an answer: "nothing new started" is said as such, never as "what happened".
  // Returns "known" (the lost request's job was reported), "none" (nothing started since), or "" (still unknown).
  function judgeLastJob(last, ctx) {
    const lid = last && typeof last === "object" ? last.job_id : undefined;
    if (ctx.jobId) {
      if (lid === ctx.jobId) return reportLastJob(last) ? "known" : "";
      failure("The server's most recent record is not the request this page lost track of.");
      return "";
    }
    if (lid === undefined || lid === ctx.before) {
      failure("No job has started on the server since your last confirmed request, so your last " + ctx.what +
        " has not run. If it was only delayed on the way it could still arrive: look at the entity's workspace before " +
        "sending it again.");
      return "none";
    }
    return reportLastJob(last) ? "known" : "";
  }
  // Asks the server what the session holds NOW (GET /chat/session.json) and rebuilds the screen from that. Called only
  // from the "Check what happened" button after an ambiguous outcome; its warning is attached to whatever is shown. A
  // gated session comes back with its held set and, while it is still approvable, its CURRENT decision id (the id only
  // ever comes from the server's answer). Reads only.
  function resync(myRun, ctx) {
    if (!session) return;
    const warning = ctx.warning;
    live.textContent = "reading the session…";
    api("GET", "/chat/session.json?id=" + encodeURIComponent(session.id)).then((r) => {
      if (myRun !== run) return;
      live.textContent = "";
      if (isTokenRefusal(r)) { showTokenPrompt(restartedText(), r); return; }
      const s = r.json || {};
      if (r.status === 404 && s.error === "unknown_session") {
        failure(goneText());
        endOfTurn(true);
        const row = el("div", "chat-row");
        const fresh = el("button", "chat-btn", "Start a new session"); fresh.type = "button";
        fresh.addEventListener("click", () => loadListing(false));
        row.appendChild(fresh); log.appendChild(row);
        return;
      }
      const verdict = r.status === 200 ? judgeLastJob(s.last_job, ctx) : "";
      if (r.status === 200 && s.state === "gated" && Array.isArray(s.pending) && s.pending.length) {
        showConsent(s.pending, s.decision_id, myRun, warning); return;
      }
      if (r.status === 200 && s.state === "busy") {
        failure("The session is still working. Check again in a moment.");
        endOfTurn(true);
        rereadButton(myRun, ctx);
        return;
      }
      if (r.status === 200 && s.state === "idle") {
        if (verdict === "known") note("chat-note", "The session is idle now; above is what the server recorded.");
        else if (verdict !== "none") failure("The session is idle now. " + warning);
        endOfTurn(false); return;
      }
      failure("Could not recover the session's state: " + (r.status === 200 ? "it is " + s.state : why(r)));
      endOfTurn(true);
      rereadButton(myRun, ctx);
    });
  }
  function showConsent(pending, decisionId, myRun, warning) {
    const box = el("div", "chat-consent");
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "actions awaiting your decision");
    box.appendChild(el("div", "chat-consent-head", "Held for your approval"));
    if (warning) box.appendChild(el("div", "chat-reason", "⚠ " + warning));
    // Fail closed: Approve exists only when this halt carries a decision id AND every held action is shown in
    // full. Anything else (an unreadable action, an older server) can still be rejected or closed.
    const decidable = typeof decisionId === "string" && decisionId !== "" && pending.every((p) => shownInFull(p.full));
    pending.forEach((p) => {
      const item = el("div", "chat-pending");
      item.appendChild(el("div", "chat-tool", visible(p.tool)));
      // The held call's arguments exactly as stored: approving runs what was built from them, and the approval
      // binds to them. The one-line detail is a convenience parsed from the same bytes, shown beside them only.
      if (p.detail) item.appendChild(el("div", "chat-reason", "in short: " + visible(p.detail)));
      if (shownInFull(p.full)) item.appendChild(el("pre", "chat-detail", visible(p.full, true)));
      else item.appendChild(el("div", "chat-reason", "The held call could not be read; it can only be rejected."));
      if (p.reason) item.appendChild(el("div", "chat-reason", visible(p.reason)));
      if (p.recognized === false) item.appendChild(el("div", "chat-reason", "This action was not recognised by the entity's policy."));
      box.appendChild(item);
    });
    if (!decidable) box.appendChild(el("div", "chat-reason", "Approve is not offered: this halt cannot be shown in full or carries no decision id, so only Reject (or Close session) is safe."));
    const reasonIn = el("textarea", "chat-input chat-grow");
    reasonIn.placeholder = "reason for rejecting (optional)";
    reasonIn.setAttribute("aria-label", "reason for rejecting");
    reasonIn.maxLength = 500;
    autoGrow(reasonIn);
    // Enter in this field only starts a new line: it sits outside any form and nothing here listens for Enter.
    // Tab order is reason -> Reject -> Approve, so the key after typing a reason reaches the safe direction first.
    const approve = el("button", "chat-btn approve", "Approve");
    const reject = el("button", "chat-btn reject", "Reject");
    approve.type = "button"; reject.type = "button";
    const row = el("div", "chat-row");
    row.appendChild(reject);
    if (decidable) row.appendChild(approve);
    box.appendChild(reasonIn); box.appendChild(row);
    log.appendChild(box);

    function lock() {
      retireChecks(); deciding = true;
      approve.disabled = true; reject.disabled = true; reasonIn.disabled = true; cancelBtn.disabled = true; runBtn.disabled = true;
    }
    function decided(j, jid) {
      deciding = false; box.remove();
      const res = j.result;
      if (j.status === "lost" || j.status === "unknown") { failure(j.error || "This page lost track of the decision."); ambiguousStop(myRun, "decision", jid); return; }
      if (j.status !== "done" || !res) { failure(j.error || ("the job ended as “" + j.status + "”")); endOfTurn(true); return; }
      if (res.error) failure(res.error);
      if (res.timed_out) failure("The turn timed out before it finished.");
      if (!res.error && res.gated && Array.isArray(res.pending) && res.pending.length) {
        if (res.reply) replyBubble(res);
        showConsent(res.pending, res.decision_id, myRun); return;
      }
      if (res.reply) replyBubble(res);
      else if (!res.error && !res.timed_out) failure("The entity returned no reply.");
      endOfTurn(!!res.gated);
    }
    function after(r) {
      if (isTokenRefusal(r)) { showTokenPrompt(TOKEN_LOST_MID_REQUEST, r); return; }
      if (r.status !== 202 || !r.json.job_id) {
        // Not retried and not re-armed: this box is withdrawn. No response this page receives can prove who wrote
        // it (a proxy can answer a JSON 4xx/503 after forwarding the request), so anything but a 202 with a job,
        // the server's own 409 stale_decision included, may have started the decision: the outcome is reported
        // unknown and the session is read only on a trusted click, never rebuilt here.
        deciding = false; box.remove();
        failure("The decision was not confirmed: " + why(r));
        ambiguousStop(myRun, "decision", null); return;
      }
      live.textContent = "working…";
      const jid = r.json.job_id;
      confirmedJob = jid;
      poll(jid, myRun, (j) => { live.textContent = ""; decided(j, jid); });
    }

    // APPROVE OPENS A CONFIRM ROW (Phill 2026-10-05, the W3C alertdialog pattern): "Run the N held actions?
    // [Cancel] [Run them]". Cancel takes Approve's place in the row and the focus; reaching Run them takes a deliberate
    // move (Tab or an arrow key), then Enter, Space or a click. Tab and the arrows stay inside the row while it is open;
    // Esc and Cancel close it and return focus to Approve. The two presses are separated by STRUCTURE, not by time, so
    // a held key's repeats land on the focused Cancel, and the second click of a double click lands on Cancel or the gap
    // and the failure direction is "nothing ran". Reject stays one step: rejecting runs nothing.
    const ask = el("div", "chat-reason chat-ask", "Run the " + pending.length + " held action" +
      (pending.length === 1 ? "" : "s") + "?");
    const cancelBtn = el("button", "chat-btn quiet", "Cancel");
    const runBtn = el("button", "chat-btn approve", "Run them");
    cancelBtn.type = "button"; runBtn.type = "button";
    function openConfirm() {
      if (approve.offsetWidth) cancelBtn.style.minWidth = approve.offsetWidth + "px";   // covers Approve's spot
      reject.style.visibility = "hidden"; reject.disabled = true;   // keeps its space, so Cancel takes Approve's slot
      box.insertBefore(ask, row);
      row.replaceChild(cancelBtn, approve);
      row.appendChild(runBtn);
      cancelBtn.focus();
    }
    function closeConfirm() {
      if (cancelBtn.parentNode !== row) return;
      row.replaceChild(approve, cancelBtn);
      if (runBtn.parentNode) runBtn.remove();
      if (ask.parentNode) ask.remove();
      reject.style.visibility = ""; reject.disabled = deciding;
      approve.focus();
    }
    approve.addEventListener("click", (ev) => {
      if (!ev.isTrusted || deciding || !session) return;
      openConfirm();
    });
    cancelBtn.addEventListener("click", (ev) => { if (ev.isTrusted) closeConfirm(); });
    // Inside the open row: Tab, Shift+Tab and the arrow keys move between Cancel and Run them only; Esc cancels. No
    // key here decides anything: Enter or Space activates whichever button has the focus, as on any button.
    row.addEventListener("keydown", (ev) => {
      if (cancelBtn.parentNode !== row) return;
      if (ev.key === "Escape") { ev.preventDefault(); closeConfirm(); return; }
      if (ev.key === "Tab" || ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
        ev.preventDefault();
        (ev.target === runBtn ? cancelBtn : runBtn).focus();
      }
    });
    // THE ONLY /chat/approve POST in this file: a trusted activation of Run them, which exists only while the confirm
    // row is open, once, with every button locked.
    runBtn.addEventListener("click", (ev) => {
      if (!ev.isTrusted || deciding || !session || runBtn.parentNode !== row) return;
      lock();
      api("POST", "/chat/approve", { session_id: session.id, expect: decisionId }).then(after);
    });
    reject.addEventListener("click", (ev) => {
      if (!ev.isTrusted || deciding || !session) return;
      lock();
      const reason = reasonIn.value.trim();
      api("POST", "/chat/reject", reason ? { session_id: session.id, reason: reason, expect: decisionId } : { session_id: session.id, expect: decisionId }).then(after);
    });
  }

  // ---- close ----------------------------------------------------------------------------------------------------
  function closeSession() {
    if (!session) return;
    closeBtn.disabled = true;
    api("POST", "/chat/close", { session_id: session.id }).then((r) => {
      if (isTokenRefusal(r)) { showTokenPrompt(TOKEN_LOST_MID_REQUEST, r); return; }
      if (r.status !== 200) {   // e.g. 409 while a turn is still running: say so, keep the session
        closeBtn.disabled = false;
        live.textContent = "Could not close yet: " + why(r);
        return;
      }
      loadListing(false);
    });
  }

  // Probe once at load. 404 leaves the page untouched; a launch_token 403 waits for the unlock and probes again.
  loadListing(false);
})();
