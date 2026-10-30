// The cockpit page's one answer to an old phone link (ONESURFACE OS-4, one origin): the phone port and the cockpit now
// share an origin, so a link the phone already holds (`https://<name>/#phone_token=<t>&sign=<id>&v=<16 hex>&choice=<k>`,
// in a stored notification or a message) opens THIS page. A fragment never reaches a server, so the page moves it.
// It acts only on a fragment that carries `phone_token` or `sign`, and never on one that carries the cockpit's own
// `write_token`; any other fragment is left untouched. The old run token is dropped here and never forwarded: the bar
// and this history entry are scrubbed first, and `location.replace` leaves no entry of this page behind. The target is
// a path on this origin built from the sign id and version by the sheet link's own shape, never a URL read from the link.
(() => {
  "use strict";
  const fields = new Map();
  for (const part of location.hash.replace(/^#/, "").split("&")) {
    const i = part.indexOf("=");
    // the first occurrence of a name counts, as the old phone page read it
    if (i > 0 && !fields.has(part.slice(0, i))) fields.set(part.slice(0, i), part.slice(i + 1));
  }
  if (fields.has("write_token") || !(fields.has("phone_token") || fields.has("sign"))) return;
  try { history.replaceState(null, "", location.pathname); } catch (e) { /* the replace below still leaves the page */ }
  const id = fields.get("sign") || "";
  const v = fields.get("v") || "";
  const choice = fields.get("choice") || "";
  let target = "/";
  if (/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
    target = "/sign/" + id;
    // a pick holds only on the version it was made on: no valid v=, no choice=
    if (/^[0-9a-f]{16}$/.test(v)) target += "#v=" + v + (/^[a-z0-9]{1,8}$/.test(choice) ? "&choice=" + choice : "");
  }
  location.replace(target);
})();
