// The old phone page's one script (ONESURFACE OS-4, `levain consent phone --redirect-to <origin>`): a link the phone
// already holds opens the cockpit's sign sheet instead. A fragment never reaches a server, so this cannot be a 30x.
// The link's `phone_token=` is dropped here and never forwarded; the bar and this history entry are scrubbed before
// anything else runs, and `location.replace` leaves no entry of this page behind. The target origin is the server's
// (the page's meta, written by levain), never read from the link: an open redirect otherwise.
(() => {
  "use strict";
  const hash = location.hash;
  try { history.replaceState(null, "", location.pathname); } catch (e) { /* the replace below still leaves the page */ }
  const meta = document.querySelector('meta[name="levain-redirect-to"]');
  const origin = meta ? meta.getAttribute("content") || "" : "";
  if (!/^https?:\/\/[a-z0-9.-]+(?::[0-9]{1,5})?$/.test(origin)) return;
  // the first occurrence of a name counts, as the phone page read it
  const fields = new Map();
  for (const part of hash.replace(/^#/, "").split("&")) {
    const i = part.indexOf("=");
    if (i > 0 && !fields.has(part.slice(0, i))) fields.set(part.slice(0, i), part.slice(i + 1));
  }
  const id = fields.get("sign") || "";
  const v = fields.get("v") || "";
  const choice = fields.get("choice") || "";
  let target = origin + "/";
  if (/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
    target = origin + "/sign/" + id;
    // a pick holds only on the version it was made on: no valid v=, no choice=
    if (/^[0-9a-f]{16}$/.test(v)) target += "#v=" + v + (/^[a-z0-9]{1,8}$/.test(choice) ? "&choice=" + choice : "");
  }
  location.replace(target);
})();
