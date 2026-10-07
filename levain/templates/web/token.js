// levain token.js — the launch token, shared by every Levain page (`levain serve`, `levain init --web`, `levain docs`).
//
// Each server generates a token when it starts and refuses every request without it, except this page shell. The link
// it opens (or prints) carries a single-use CODE in the URL fragment (#code=...): this file strips it from the address
// bar at once and trades it, by one POST /unlock, for the token, which it keeps in this tab's sessionStorage; a
// browser never sends a fragment to a server, so the code reaches no server log and no Referer; the browser's own
// history does keep it, and there it is a code that has already been spent. The token printed in a terminal, typed
// into the unlock form, is the fallback (a headless box, a second browser, a spent or expired link). An old
// #token= / #chat_token= link still works when the page loads. The token is never put in a cookie, storage that
// outlives the browser session, or a request URL; a duplicated or restored tab keeps its sessionStorage, as browsers do.
//
// Loaded before every other script on the page. The other scripts send `LevainToken.headers()` with each request, and
// on a 403 whose JSON says `error: "launch_token"` they call `LevainToken.lock(message, sent)`, which drops the token and
// shows the one unlock form. A script that has to re-run after an unlock registers with `LevainToken.onUnlock(fn)`.
(function () {
  "use strict";
  const HEADER = "X-Levain-Token";
  const KEY = "levain.token";
  const listeners = [];
  let pendingCode = null;    // a single-use link code from the fragment, traded once for the token
  let exchanging = false;    // that trade is in flight: a refusal meanwhile waits for its answer
  let deferredLock = null;   // the message such a refusal brought
  let token = take();
  let form = null;
  let noteEl = null;

  // The fragment the server opened the page with wins; otherwise what this tab kept. `#chat_token=` is the fragment
  // the chat panel was opened with before one token covered every route. Storage can be absent or throw (a private
  // window, blocked site data), so every access is guarded and the form is the fallback.
  function take() {
    let t = null;
    try {
      const frag = location.hash;
      const c = /^#code=(.*)$/.exec(frag);
      const m = /^#(chat_)?token=(.*)$/.exec(frag);
      if (c || m) history.replaceState(null, "", location.pathname + location.search);
      if (c && /^[A-Za-z0-9_-]+$/.test(c[1])) pendingCode = c[1];
      if (m) {
        if (/^[A-Za-z0-9_-]+$/.test(m[2])) t = m[2];
        if (m[1]) {
          try { console.warn("levain: the #chat_token= link is deprecated; the server now prints a single-use #code= link."); }
          catch (e) { /* no console */ }
        }
      }
    } catch (e) { t = null; /* no location or history, or the strip failed: the form still works */ }
    if (t) { keep(t); return t; }
    try { return sessionStorage.getItem(KEY) || null; } catch (e) { return null; }
  }
  function keep(t) { try { sessionStorage.setItem(KEY, t); } catch (e) { /* held in memory only */ } }
  function drop() { token = null; try { sessionStorage.removeItem(KEY); } catch (e) { /* nothing kept */ } }

  function headers(h) {
    const out = h || {};
    if (token) out[HEADER] = token;
    return out;
  }
  function isRefusal(status, json) { return status === 403 && !!json && json.error === "launch_token"; }

  // The unlock form: one per page, at the top of <body>. Every refusal on the page lands here, so two scripts that
  // are refused at once show one form, with the latest message. `sent` is the token the refused request carried
  // (`get()` when it was sent): a refusal of a token this page has since replaced is late news and changes nothing.
  function lock(message, sent) {
    if (sent !== undefined && sent !== token) return;
    if (exchanging) { deferredLock = message || ""; return; }   // the link's code may still unlock the page
    drop();
    if (!document.body) return;
    if (!form) {
      form = document.createElement("form");
      form.className = "levain-lock";
      form.setAttribute("aria-label", "unlock this page");
      const p = document.createElement("p");
      p.className = "levain-lock-text";
      p.textContent = "This page needs the token the server printed when it started.";
      noteEl = document.createElement("p");
      noteEl.className = "levain-lock-note";
      const input = document.createElement("input");
      input.type = "password"; input.autocomplete = "off"; input.spellcheck = false;
      input.setAttribute("aria-label", "token");
      const go = document.createElement("button");
      go.type = "submit"; go.textContent = "Unlock";
      form.appendChild(p); form.appendChild(noteEl); form.appendChild(input); form.appendChild(go);
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const t = input.value.trim();
        if (!t) return;
        if (!/^[A-Za-z0-9_-]+$/.test(t)) {
          // A header value must be plain ASCII, and a printed token is only these characters (codex L3 r2: anything
          // else made fetch throw before a request left, so no refusal ever brought the form back).
          noteEl.textContent = "That is not a token: the printed token uses only letters, digits, - and _.";
          noteEl.hidden = false;
          return;
        }
        input.value = "";
        token = t; keep(t);
        unlocked();
      });
      document.body.insertBefore(form, document.body.firstChild);
    }
    // A refusal that brings no message of its own leaves the form's current one (why the last link failed) in place.
    if (message) noteEl.textContent = message;
    noteEl.hidden = !noteEl.textContent;
  }
  function onUnlock(fn) { if (typeof fn === "function") listeners.push(fn); }
  function unlocked() {
    if (form) { form.remove(); form = null; noteEl = null; }
    listeners.slice().forEach((fn) => { try { fn(); } catch (e) { /* one listener's fault is its own */ } });
  }

  // The server's links carry a single-use code, never the token (a browser's history keeps the link as opened, and
  // Chrome's did, fragment included, measured 2026-10-07). Trade it once for the token. Until the answer comes, a
  // refusal elsewhere on the page does not show the form; a failed trade shows it, saying why.
  if (pendingCode) {
    exchanging = true;
    let traded = null;
    fetch("/unlock", { method: "POST", cache: "no-store", headers: { "X-Levain-Link-Code": pendingCode } })
      .then((r) => r.json().catch(() => ({})).then((j) => { if (r.status === 200) traded = j && j.token; }))
      .catch(() => {})
      .then(() => {
        exchanging = false;
        pendingCode = null;
        if (typeof traded === "string" && /^[A-Za-z0-9_-]+$/.test(traded)) {
          token = traded; keep(traded); deferredLock = null;
          unlocked();
        } else {
          deferredLock = null;
          lock("That link was already used or has expired. Open a new one with `levain serve --open-running`, " +
               "or paste the token the server printed.");
        }
      });
  }

  // A token is taken from the fragment only when the page loads, never from a later change of it (head ruling
  // 2026-10-07, L1/L2): a page that holds a window reference to this one, or a page on another localhost port, can
  // change its fragment, and must not be able to swap the token. A #token= fragment that arrives later is removed
  // from the address bar and ignored; to use a new link, open it in a new tab or reload.
  try {
    window.addEventListener("hashchange", () => {
      try {
        if (/^#(?:(?:chat_)?token|code)=/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
      } catch (e) { /* nothing to strip with */ }
    });
  } catch (e) { /* no window events */ }

  window.LevainToken = Object.freeze({
    HEADER: HEADER,
    get: () => token,
    headers: headers,
    isRefusal: isRefusal,
    lock: lock,
    onUnlock: onUnlock,
  });
})();
