/* pwa_update.js: a new version of the app never takes over silently.
 *
 * The page registers /sw.js once at load. When the service worker has a new version waiting (registration.waiting), or a new one finishes installing while this page
 * already has a controller, a bar says "New version available. Tap to reload." A tap posts {type: "levain-skip-waiting"} to the
 * waiting worker (which calls skipWaiting only on that message) and the page reloads once, when the new worker takes control.
 * A reload for any other controller change (the first claim of a page that had none) never happens. */
(function (root) {
  "use strict";
  var doc = root.document;
  var sw = root.navigator && root.navigator.serviceWorker;
  if (!sw || typeof sw.register !== "function") return;
  var bar = null, tapped = false, reloaded = false, reg = null;

  function show() {
    if (bar || !doc.body) return;
    bar = doc.createElement("button");
    bar.type = "button";
    bar.className = "pwa-update-bar";
    bar.setAttribute("role", "status");
    bar.textContent = "New version available. Tap to reload.";
    bar.addEventListener("click", function () {
      var w = reg && reg.waiting;
      tapped = true;
      if (w) w.postMessage({ type: "levain-skip-waiting" });
      else root.location.reload();     // the worker took over between the prompt and the tap
    });
    doc.body.appendChild(bar);
  }

  function watch(r) {
    reg = r;
    if (!r) return;
    if (r.waiting && sw.controller) show();
    r.addEventListener("updatefound", function () {
      var w = r.installing;
      if (!w) return;
      w.addEventListener("statechange", function () {
        if (w.state === "installed" && sw.controller) show();
      });
    });
  }

  sw.addEventListener("controllerchange", function () {
    if (!tapped || reloaded) return;
    reloaded = true;
    root.location.reload();
  });

  // The worker is registered at load, not only after a user action (pair_card, devices_panel and phone_app register the same
  // script and scope too: registering again returns the one registration). Without it there is no offline shell and nothing to update.
  function start() {
    Promise.resolve(sw.register("/sw.js", { scope: "/" })).then(watch, function () { /* an unregisterable origin: nothing to watch */ });
  }
  // an app left open for days looks for a new worker each time it comes back to the front
  doc.addEventListener("levain:resume", function () {
    if (reg && typeof reg.update === "function") Promise.resolve(reg.update()).catch(function () { /* offline */ });
    if (reg && reg.waiting && sw.controller) show();
  });
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start); else start();
})(window);
