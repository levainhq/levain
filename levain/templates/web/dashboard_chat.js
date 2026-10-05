// levain dashboard_chat.js — the cockpit's chat panel: talk to a registered entity from the browser.
//
// Present only when the server was started with `--chat` (GET /chat.json answers 403 chat_token or 200; a
// plain cockpit answers 404 and nothing is added). Every /chat route needs the per-launch token the server
// printed at start; it is typed into a password field and held in a closure variable ONLY — never storage,
// cookie or URL — so a reload asks again by design.
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
  let token = null;          // the chat token: this variable and nowhere else
  let session = null;        // {id, entity} once a session is open
  let panel = null, body = null;
  let run = 0;               // bumped when a session ends, so a late poll of an old job cannot paint the new one
  let deciding = false;      // an approve/reject POST or its job is in flight

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  // One fetch wrapper: JSON in, {status, json} out; a network failure is a status of 0, never a thrown error.
  function api(method, path, payload) {
    const headers = { Accept: "application/json" };
    if (token) headers["X-Levain-Chat-Token"] = token;
    const init = { method: method, headers: headers, cache: "no-store" };
    if (payload !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(payload); }
    return fetch(path, init)
      .then((r) => r.json().catch(() => ({})).then((j) => ({ status: r.status, json: j || {} })))
      .catch(() => ({ status: 0, json: {} }));
  }
  function isTokenRefusal(r) { return r.status === 403 && r.json && r.json.error === "chat_token"; }
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

  // ---- token prompt ----------------------------------------------------------------------------------------
  function showTokenPrompt(message) {
    session = null; run++; deciding = false;
    ensurePanel(); clear(body);
    note("chat-note", "This cockpit serves chat. Enter the token the server printed when it started.");
    if (message) note("chat-err", message);
    const form = el("form", "chat-row");
    const input = el("input", "chat-input");
    input.type = "password"; input.autocomplete = "off"; input.spellcheck = false;
    input.setAttribute("aria-label", "chat token");
    const go = el("button", "chat-btn", "Unlock");
    go.type = "submit";
    form.appendChild(input); form.appendChild(go);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const t = input.value.trim();
      if (!t) return;
      token = t; input.value = "";
      go.disabled = true;
      loadListing(true);
    });
    body.appendChild(form);
  }

  // ---- entity picker -----------------------------------------------------------------------------------------
  function loadListing(afterToken) {
    api("GET", "/chat.json").then((r) => {
      if (r.status === 404) { if (panel) { panel.remove(); panel = null; } return; }  // not a chat cockpit (or gone)
      if (isTokenRefusal(r)) {
        token = null;
        showTokenPrompt(afterToken ? "That token was not accepted." : null);
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
    if (!listing.entities.length) { note("chat-note", "No entities are registered for chat."); return; }
    const row = el("div", "chat-row");
    const sel = el("select", "chat-input");
    sel.setAttribute("aria-label", "entity to chat with");
    listing.entities.forEach((name) => { const o = el("option", null, name); o.value = name; sel.appendChild(o); });
    const open = el("button", "chat-btn", "Open session");
    open.type = "button";
    open.addEventListener("click", () => { open.disabled = true; openSession(sel.value); });
    row.appendChild(sel); row.appendChild(open);
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
        if (isTokenRefusal(r)) { showTokenPrompt("The token was not accepted; enter it again."); return; }
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
      if (isTokenRefusal(r)) { showTokenPrompt("The token was not accepted; enter it again."); return; }
      if (r.status !== 202 || !r.json.session_id) {
        clear(body); note("chat-err", "Could not open a session: " + why(r));
        const back = el("button", "chat-btn", "Back"); back.type = "button";
        back.addEventListener("click", () => loadListing(false)); body.appendChild(back);
        return;
      }
      const sid = r.json.session_id, myRun = run;
      clear(body); note("chat-note", "Opening a session on " + entity + "…");
      poll(r.json.job_id, myRun, (j) => {
        const st = j.result && j.result.session && j.result.session.state;
        if (j.status === "done" && st === "idle") { session = { id: sid, entity: entity }; showConversation(); return; }
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
    sendBtn = el("button", "chat-btn", "Send"); sendBtn.type = "submit";
    form.appendChild(area); form.appendChild(sendBtn);
    form.addEventListener("submit", (ev) => { ev.preventDefault(); sendTurn(); });
    body.appendChild(form);
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
    if (!text || !session) return;
    area.value = "";
    const mine = bubble("me", "you"); mine.appendChild(el("div", "chat-text", text));
    setComposeEnabled(false);
    const myRun = run;
    api("POST", "/chat/turn", { session_id: session.id, message: text }).then((r) => {
      if (myRun !== run) return;
      if (isTokenRefusal(r)) { showTokenPrompt("The token was not accepted; enter it again."); return; }
      if (r.status !== 202 || !r.json.job_id) { failure("The turn was not accepted: " + why(r)); setComposeEnabled(true); return; }
      followJob(r.json.job_id, myRun);
    });
  }

  function failure(text) { const b = bubble("err", "error"); b.appendChild(el("div", "chat-text", text)); }

  // Shows a job's progress, then its outcome. Reads only: a gated outcome renders the consent surface and stops.
  function followJob(jobId, myRun) {
    live.textContent = "working…";
    poll(jobId, myRun, (j) => {
      live.textContent = "";
      const res = j.result;
      if (j.status === "lost") { failure(j.error); resync(myRun, true); return; }
      if (j.status !== "done" || !res) {   // failed, lost, unknown: never rendered as success
        failure(j.error || ("the job ended as “" + j.status + "”"));
        endOfTurn(true); return;
      }
      if (res.error) failure(res.error);
      if (res.timed_out) failure("The turn timed out before it finished.");
      if (!res.error && res.gated && Array.isArray(res.pending) && res.pending.length) {
        if (res.reply) { const b = bubble("them", session.entity); b.appendChild(el("div", "chat-text", res.reply)); addLines(b, res.tool_activity); }
        showConsent(res.pending, res.decision_id, myRun);
        return;
      }
      if (res.gated && !res.error) failure("The turn halted on a gated action but reported nothing to decide.");
      if (res.reply) {
        const b = bubble("them", session.entity);
        b.appendChild(el("div", "chat-text", res.reply));
        addLines(b, res.tool_activity);
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
  // Asks the server what the session holds NOW (GET /chat/session.json) and rebuilds the screen from that, after
  // a decision was not accepted or contact with a job was lost. It never follows a job and never reports an
  // outcome it did not see: a GATED session comes back with its CURRENT decision id and held set and gets a fresh
  // box (the id only ever comes from the server's answer); after an AMBIGUOUS loss (the page cannot tell whether
  // its decision or turn ran) anything else is reported as unknown with compose blocked; after a definite refusal
  // an idle session is ready again. Reads only: it never POSTs.
  const UNKNOWN = "The outcome of the last decision is unknown; check the session's activity before retrying.";
  function resync(myRun, ambiguous) {
    if (!session) return;
    live.textContent = "reading the session…";
    api("GET", "/chat/session.json?id=" + encodeURIComponent(session.id)).then((r) => {
      if (myRun !== run) return;
      live.textContent = "";
      if (isTokenRefusal(r)) { showTokenPrompt("The token was not accepted; enter it again."); return; }
      const s = r.json || {};
      if (r.status === 200 && s.state === "gated" && Array.isArray(s.pending) && s.pending.length) {
        showConsent(s.pending, s.decision_id, myRun); return;
      }
      if (ambiguous) { failure(UNKNOWN); endOfTurn(true); return; }
      if (r.status === 200 && s.state === "idle") { endOfTurn(false); return; }
      failure("Could not recover the session's state: " + (r.status === 200 ? "it is " + s.state : why(r)));
      endOfTurn(true);
    });
  }
  function showConsent(pending, decisionId, myRun) {
    const box = el("div", "chat-consent");
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "actions awaiting your decision");
    box.appendChild(el("div", "chat-consent-head", "Held for your approval"));
    // Fail closed: Approve exists only when this halt carries a decision id AND every held action is shown in
    // full. Anything else (an unreadable action, an older server) can still be rejected or closed.
    const decidable = typeof decisionId === "string" && decisionId !== "" && pending.every((p) => shownInFull(p.full));
    pending.forEach((p) => {
      const item = el("div", "chat-pending");
      item.appendChild(el("div", "chat-tool", visible(p.tool)));
      // The whole action, never the bounded one-line detail: approving runs all of it.
      const whole = (typeof p.full === "string" && p.full) ? p.full : p.detail;
      if (whole) item.appendChild(el("pre", "chat-detail", visible(whole, whole === p.full)));
      if (p.reason) item.appendChild(el("div", "chat-reason", visible(p.reason)));
      if (p.recognized === false) item.appendChild(el("div", "chat-reason", "This action was not recognised by the entity's policy."));
      box.appendChild(item);
    });
    if (!decidable) box.appendChild(el("div", "chat-reason", "Approve is not offered: this halt cannot be shown in full or carries no decision id, so only Reject (or Close session) is safe."));
    const reasonIn = el("input", "chat-input");
    reasonIn.type = "text"; reasonIn.placeholder = "reason for rejecting (optional)";
    reasonIn.setAttribute("aria-label", "reason for rejecting");
    reasonIn.maxLength = 500;
    // Enter in this field must not submit anything: it is a plain input outside any form.
    const approve = el("button", "chat-btn approve", "Approve");
    const reject = el("button", "chat-btn reject", "Reject");
    approve.type = "button"; reject.type = "button";
    const row = el("div", "chat-row");
    if (decidable) row.appendChild(approve);
    row.appendChild(reject);
    box.appendChild(reasonIn); box.appendChild(row);
    log.appendChild(box);

    function lock() { deciding = true; approve.disabled = true; reject.disabled = true; reasonIn.disabled = true; }
    function decided(j) {
      deciding = false; box.remove();
      const res = j.result;
      if (j.status === "lost") { failure(j.error); resync(myRun, true); return; }
      if (j.status !== "done" || !res) { failure(j.error || ("the job ended as “" + j.status + "”")); endOfTurn(true); return; }
      if (res.error) failure(res.error);
      if (res.timed_out) failure("The turn timed out before it finished.");
      if (!res.error && res.gated && Array.isArray(res.pending) && res.pending.length) {
        if (res.reply) { const b = bubble("them", session.entity); b.appendChild(el("div", "chat-text", res.reply)); addLines(b, res.tool_activity); }
        showConsent(res.pending, res.decision_id, myRun); return;
      }
      if (res.reply) { const b = bubble("them", session.entity); b.appendChild(el("div", "chat-text", res.reply)); addLines(b, res.tool_activity); }
      else if (!res.error && !res.timed_out) failure("The entity returned no reply.");
      endOfTurn(!!res.gated);
    }
    function after(r) {
      if (isTokenRefusal(r)) { showTokenPrompt("The token was not accepted; enter it again."); return; }
      if (r.status === 409 && r.json && r.json.error === "stale_decision") {
        // The server holds something other than what this box shows: the box is withdrawn, never re-armed, and
        // a fresh one is built from what the server holds now.
        deciding = false; box.remove();
        failure("The held action changed since it was shown; nothing was decided; re-reading the session.");
        resync(myRun, false); return;
      }
      if (r.status !== 202 || !r.json.job_id) {
        // Not retried and not re-armed: this box is withdrawn and the server is asked what it holds now. If
        // the halt is still undecided it comes back with its decision id and the set to show, and a fresh
        // box is built from that, never from this one. A 4xx or a 503 is a definite refusal (nothing ran);
        // anything else (no response, another 5xx, a 202 without a job) may have started the decision.
        deciding = false; box.remove();
        const definite = (r.status >= 400 && r.status < 500) || r.status === 503;
        failure("The decision was not accepted: " + why(r));
        resync(myRun, !definite); return;
      }
      live.textContent = "working…";
      poll(r.json.job_id, myRun, (j) => { live.textContent = ""; decided(j); });
    }

    // THE ONLY /chat/approve POST in this file: a trusted click on this button, once, with both buttons locked.
    approve.addEventListener("click", (ev) => {
      if (!ev.isTrusted || deciding || !session) return;
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
      if (isTokenRefusal(r)) { showTokenPrompt("The token was not accepted; enter it again."); return; }
      if (r.status !== 200) {   // e.g. 409 while a turn is still running: say so, keep the session
        closeBtn.disabled = false;
        live.textContent = "Could not close yet: " + why(r);
        return;
      }
      loadListing(false);
    });
  }

  // Probe once at load. 404 leaves the page untouched; 403 chat_token opens the token prompt.
  loadListing(false);
})();
