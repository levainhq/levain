/* pwa_resume.js: the installed app coming back to the front resumes, it does not reload.
 *
 * visibilitychange (visible) and pageshow (restored from the back-forward cache or not) announce one `levain:resume` event on
 * the document; the board (dashboard_boot.js) and the change stream (cockpit_stream.js) listen and re-read / reconnect while the
 * page keeps showing its last state (detail.hiddenAt: when it last went to the background, 0 if it never did). The two browser events usually arrive together, so one resume per 400 ms. Also: once the app
 * has been opened standalone, it asks the browser to keep its storage (a tab is never asked; the answer is not shown). */
(function (root) {
  "use strict";
  var doc = root.document;
  var last = 0, hiddenAt = 0;   // hiddenAt: when the page was last seen going to the background

  function standalone() {
    try { return root.navigator.standalone === true || (typeof root.matchMedia === "function" && root.matchMedia("(display-mode: standalone)").matches); }
    catch (_) { return false; }
  }

  function resume() {
    if (doc.hidden) { hiddenAt = Date.now(); return; }
    var now = Date.now();
    if (now - last < 400) return;
    last = now;
    doc.dispatchEvent(new CustomEvent("levain:resume", { detail: { hiddenAt: hiddenAt } }));
  }
  doc.addEventListener("visibilitychange", resume);
  // the pageshow of the page's own first load is not a return to the app: only a restored page, or a later show, resumes
  var shown = false;
  root.addEventListener("pageshow", function (ev) {
    if (!shown && !(ev && ev.persisted)) { shown = true; return; }
    shown = true;
    resume();
  });

  // once, in the standalone app only; a browser tab never sees the request
  function persistOnce() {
    if (!standalone()) return;
    try {
      var s = root.navigator.storage;
      if (s && typeof s.persist === "function") Promise.resolve(s.persist()).catch(function () { /* the answer is not shown */ });
    } catch (_) { /* no storage manager */ }
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", persistOnce); else persistOnce();
})(window);
