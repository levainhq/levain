// The page half of the push tap (web push, ONESURFACE OS-4): the worker cannot reliably steer a window that already
// exists (WindowClient.navigate rejects on an uncontrolled one, and on iOS WebKit can resolve without moving the page),
// so on a tap it asks the page to go to the card itself. Message {type: 'levain-open', url} with a reply port; this
// page acks {ok: true} on that port BEFORE it navigates (the navigation unloads the page), then assigns the location.
// A url that is not on this origin is ignored without an ack (the worker checks too; this page does not trust it).
// Loaded by every page this origin serves a window can be showing: the cockpit (/), the sign sheet and /pair.
(() => {
  "use strict";
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.addEventListener("message", (e) => {
    const d = e.data;
    if (!d || d.type !== "levain-open" || typeof d.url !== "string") return;
    let u;
    try { u = new URL(d.url, location.href); } catch (_) { return; }
    if (u.origin !== location.origin) return;
    const port = e.ports && e.ports[0];
    if (port) { try { port.postMessage({ ok: true }); } catch (_) { /* the worker falls back to its own path */ } }
    location.assign(u.href);
  });
  // an uncontrolled page (opened before the worker took over) queues worker messages until this is called
  try { navigator.serviceWorker.startMessages(); } catch (_) { /* older engines deliver without it */ }
})();
