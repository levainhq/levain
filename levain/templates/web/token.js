// levain token.js — the launch token, shared by every Levain page (`levain serve`, `levain init --web`, `levain docs`).
//
// Each server generates a token when it starts and refuses every request without it, except this page shell. It opens
// the page with the token in the URL FRAGMENT (#token=..., which a browser never sends to a server, so it reaches no
// log and no Referer); this file reads it, keeps it in this tab's sessionStorage, and strips it from the address bar at
// once. A token is used only once that strip succeeded: a token still showing in the address bar is not taken. The
// token printed in the terminal, typed into the unlock form, is the fallback (a headless box, a second browser, a
// platform where the page was opened without it). It is never put in a cookie, storage that outlives the browser
// session, or a request URL; a duplicated or restored tab keeps its sessionStorage, as browsers do.
//
// Loaded before every other script on the page. The other scripts send `LevainToken.headers()` with each request, and
// on a 403 whose JSON says `error: "launch_token"` they call `LevainToken.lock(message, sent)`, which drops the token and
// shows the one unlock form. A script that has to re-run after an unlock registers with `LevainToken.onUnlock(fn)`.
(function () {
  "use strict";
  const HEADER = "X-Levain-Token";
  const KEY = "levain.token";
  const listeners = [];
  let token = take();
  let form = null;
  let noteEl = null;

  // The fragment the server opened the page with wins; otherwise what this tab kept. `#chat_token=` is the fragment
  // the chat panel was opened with before one token covered every route. Storage can be absent or throw (a private
  // window, blocked site data), so every access is guarded and the form is the fallback.
  function take() {
    let t = null;
    try {
      const m = /^#(?:chat_)?token=(.*)$/.exec(location.hash);
      if (m) {
        history.replaceState(null, "", location.pathname + location.search);
        if (/^[A-Za-z0-9_-]+$/.test(m[1])) t = m[1];
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
        input.value = "";
        token = t; keep(t);
        unlocked();
      });
      document.body.insertBefore(form, document.body.firstChild);
    }
    noteEl.textContent = message || "";
    noteEl.hidden = !message;
  }
  function onUnlock(fn) { if (typeof fn === "function") listeners.push(fn); }
  function unlocked() {
    if (form) { form.remove(); form = null; noteEl = null; }
    listeners.slice().forEach((fn) => { try { fn(); } catch (e) { /* one listener's fault is its own */ } });
  }

  // The unlocked link opened in a tab already showing this page changes only the fragment, and a browser does not
  // reload for that: the page is still the locked one. Take the token from the new fragment the same way.
  try {
    window.addEventListener("hashchange", () => {
      const t = take();
      if (t && t !== token) { token = t; unlocked(); }
    });
  } catch (e) { /* no window events: the form still works */ }

  window.LevainToken = Object.freeze({
    HEADER: HEADER,
    get: () => token,
    headers: headers,
    isRefusal: isRefusal,
    lock: lock,
    onUnlock: onUnlock,
  });
})();
