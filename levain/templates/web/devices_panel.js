// levain devices_panel.js — the cockpit's Devices panel (per-device pairing, PAIR_DESIGN.md REV 1-6).
//
// Lists the paired devices (GET /cockpit/pair/devices.json), mints a one-time pairing link (POST /cockpit/pair/offer {label}),
// revokes a device and its family (POST /cockpit/pair/revoke {id}), and turns pings on for this browser (OS-4, /webpush/*).
// It rides the page's own credential: the token the cockpit keeps in localStorage "levain_write_token" (dashboard_boot.js WRITE_TOKEN_KEY). Everything is drawn with textContent: this
// file shares a page with that token, so it adds no HTML sink. A server with no writable cockpit answers 404 and the panel never
// appears; a page with no usable credential shows nothing either.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";
  const ALL_MODES = "morning board build memory";   // the settings section is not a tab's card: it shows under every mode
  const H = "X-Levain-Write-Token";
  let section = null, devPanel = null, list = null, tabsNav = null, tabBtns = {}, offerBox = null, note = null, labelIn = null, offerBtns = [];
  // L3 r2 G1: ONE shared in-flight read. A caller that finds one running gets that promise back and queues exactly one
  // re-run (so a change made while it was in flight is read afterwards); reads run one at a time, so what paints is always
  // the newest read. No flag can stick: `running` is cleared in the same step the promise settles, and a read has a timeout.
  // F7: a 404 (no pairing here) is stable for this page: stop asking. L3 r3 H2: a 403 refuses THAT token only; remember it and
  // skip reads while the stored token is still it, so a fresh token (a new link, the prompt) brings the panel back.
  let running = null, rerun = false, gone = false, refused = "";
  const READ_TIMEOUT_MS = 20000;
  // Which list shows: "active" or "revoked". Kept in the page (not in the nodes a paint replaces), so the 30 s read, a revoke and
  // every other re-render leave the chosen tab where it was; the tab row is built once and only relabelled.
  let tab = "active", lastDevs = [];
  // ONESURFACE OS-4: pings for THIS browser. The row appears only where the cockpit serves web push (GET /webpush/key
  // answers, beside a Signer). Every subscription is a paired device's, so it has a revoke above it: Turn on under the
  // cockpit's own key pairs this browser on the spot, and the page keeps the device key it is handed (as pair.js keeps a
  // claimed one) and uses it from then on. The worker (/sw.js) holds no key: it marks a tapped
  // notification and pair_card.js, which holds the page's key, records the open.
  const PAIRED_MARK_KEY = "levain_paired_device";   // MUST equal pair.js / dashboard_boot.js PAIRED_MARK_KEY
  const PUSH_OK = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  // One copy of the push environment for every surface, so the words cannot drift; read at the moment they are drawn,
  // never kept from an earlier look.
  const shared = () => window.levainPairCard || {};
  // the words for a page with no push API (the plain-http case has its own, INSECURE_SAY)
  function noPushSay() {
    if (window.isSecureContext === false) return INSECURE_SAY;
    const f = shared().noPushWords;
    return typeof f === "function" ? f() : "This browser does not offer pings.";
  }
  let pingRow = null, pingState = null, pingBtn = null, pushKey = "", pushAsked = false;
  const b64uToBytes = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                             (c) => c.charCodeAt(0));

  function token() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  async function api(method, path, body, timeoutMs) {
    const headers = {};
    const t = token();
    if (t) headers[H] = t;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    // H3: only the list read carries a timeout (a stalled answer must not hold the one shared read forever); a POST that the
    // server committed but answered late must not read as unreachable and be repeated
    const ctl = new AbortController();
    const timer = timeoutMs ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    try {
      const res = await fetch(path, { method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal: ctl.signal });
      let data = {};
      try { data = await res.json(); } catch (_) { /* a non-JSON answer is a refusal below */ }
      return { status: res.status, data: data };
    } finally { if (timer) clearTimeout(timer); }
  }
  // ONE reading of a stamp, in milliseconds: ISO text, or an epoch number in seconds or milliseconds. NaN when it is none of those
  // (empty, null, text that is not a date), so each caller decides what an unreadable stamp means.
  function stampMs(v) {
    if (typeof v === "number") return Number.isFinite(v) ? (Math.abs(v) < 1e11 ? v * 1000 : v) : NaN;
    if (typeof v !== "string" || !v) return NaN;
    return Date.parse(v);
  }
  function when(iso, now) {
    if (!iso) return "never";
    const t = stampMs(iso);
    if (Number.isNaN(t)) return iso;
    const s = Math.max(0, ((now === undefined ? Date.now() : now) - t) / 1000);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function build() {
    section = el("section", "ck-zone dev-zone");
    section.dataset.zone = "devices";
    section.dataset.modes = "board";
    section.hidden = true;
    const head = el("div", "zone-head");
    head.appendChild(el("div", "zone-title", "Devices"));
    head.appendChild(el("span", "zone-gloss", "who holds a key to this cockpit, and who may post to you"));
    section.appendChild(head);
    const grid = el("div", "ck-zgrid dev-grid");
    const panel = el("div", "panel k-list dev-panel");
    devPanel = panel;
    panel.hidden = true;   // shown by its own read
    panel.dataset.modes = "board";   // dashboard.css hides a zone whose panel names no tab for the current mode
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Paired devices"));
    panel.appendChild(ph);
    const body = el("div", "pbody dev-body");
    // the cockpit's own tab row (.ck-tabs / .ck-tab, as the Memory card's tabs); hidden while nothing is revoked
    tabsNav = el("div", "ck-tabs dev-tabs");
    tabsNav.setAttribute("role", "tablist");
    tabsNav.hidden = true;
    for (const k of ["active", "revoked"]) {
      const b = el("button", "ck-tab", "");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.id = "dev-tab-" + k;
      b.setAttribute("aria-controls", "dev-tabpanel");
      b.addEventListener("click", () => { tab = k; paint(); });
      // roving tabindex: arrows move between the tabs and select, Home/End jump
      b.addEventListener("keydown", (e) => {
        const order = ["active", "revoked"];
        let to = null;
        if (e.key === "ArrowRight") to = order[(order.indexOf(k) + 1) % order.length];
        else if (e.key === "ArrowLeft") to = order[(order.indexOf(k) + order.length - 1) % order.length];
        else if (e.key === "Home") to = order[0];
        else if (e.key === "End") to = order[order.length - 1];
        if (!to) return;
        e.preventDefault();
        tab = to; paint(); tabBtns[to].focus();
      });
      tabBtns[k] = b; tabsNav.appendChild(b);
    }
    body.appendChild(tabsNav);
    list = el("div", "dev-list");
    list.setAttribute("role", "tabpanel");
    list.id = "dev-tabpanel";
    body.appendChild(list);
    pingRow = el("div", "dev-row dev-pings"); pingRow.hidden = true;
    const pm = el("div", "dev-main");
    pm.appendChild(el("div", "dev-name", "Pings on this browser"));
    pingState = el("div", "dev-meta", "");
    pm.appendChild(pingState);
    pingRow.appendChild(pm);
    pingBtn = el("button", "dev-btn", "Turn on");
    pingBtn.type = "button";
    pingBtn.addEventListener("click", () => { if (pingBtn.dataset.on === "1") turnOff(); else turnOn(); });
    pingRow.appendChild(pingBtn);
    body.appendChild(pingRow);

    const form = el("form", "dev-offer");
    labelIn = el("input", "dev-label");
    labelIn.type = "text"; labelIn.maxLength = 40; labelIn.placeholder = "name the new device";
    labelIn.setAttribute("aria-label", "Name for the new device");
    labelIn.autocomplete = "off";
    // two ways to pair: a link (a browser tab or a laptop opens it) or a short code (a Home Screen app types it)
    const go = el("button", "dev-btn dev-primary", "Make a link");
    go.type = "submit";
    const code = el("button", "dev-btn", "Show a code");
    code.type = "button";
    offerBtns = [go, code];
    code.addEventListener("click", () => offer(true));
    form.appendChild(labelIn); form.appendChild(go); form.appendChild(code);
    form.addEventListener("submit", (e) => { e.preventDefault(); offer(false); });
    body.appendChild(form);
    offerBox = el("div", "dev-link"); offerBox.hidden = true;
    body.appendChild(offerBox);
    note = el("p", "dev-note"); note.hidden = true;
    body.appendChild(note);
    panel.appendChild(body);
    grid.appendChild(panel);
    grid.appendChild(buildNotify());   // Notify tokens beside Paired devices (S4.2, C7)
    section.appendChild(grid);
    // the quieter half of Presence and the Notifications policy: one plainly titled section at the foot, in every mode, so the
    // sheet's link always has somewhere to land
    prSetSec = el("section", "ck-zone dev-zone pr-settings");
    prSetSec.id = "presence-settings";
    prSetSec.tabIndex = -1;
    prSetSec.dataset.zone = "presence";
    prSetSec.dataset.modes = ALL_MODES;
    prSetSec.hidden = true;
    const sh = el("div", "zone-head");
    sh.appendChild(el("h2", "zone-title", "Presence & notification settings"));
    prSetSec.appendChild(sh);
    const sgrid = el("div", "ck-zgrid dev-grid pr-settings-grid");
    sgrid.appendChild(buildTaps());
    sgrid.appendChild(buildPolicy());
    prSetSec.appendChild(sgrid);
    const main = document.querySelector("main.frame");
    if (main) { main.appendChild(section); main.appendChild(prSetSec); } else { document.body.appendChild(section); document.body.appendChild(prSetSec); }
    buildChip();
    buildSheet();
  }

  // The devices section shows with its own read; the settings section holds two independent blocks (tap tokens, the
  // Notifications policy) and shows while either does, each with its own gone/refused state.
  function syncSection() {
    section.hidden = devPanel.hidden;
    if (prSetSec) prSetSec.hidden = (!npPanel || npPanel.hidden) && (!prPanel || prPanel.hidden);
  }
  function devShown(on) { devPanel.hidden = !on; syncSection(); }
  function npShown(on) { npPanel.hidden = !on; syncSection(); }

  function say(msg) { note.textContent = msg; note.hidden = !msg; }

  // MUST equal pair.js shortId: the first 8 characters of the device id; the Paired screen's previous-key line names the row by it
  function shortId(id) { return String(id).slice(0, 8); }

  // A label inside a sentence, bidi-isolated (FSI ... PDI) so right-to-left text cannot reorder the words around it
  function named(d) { return "\u2068" + (d.label || d.id) + "\u2069"; }

  // "" for a device seen (or, never seen, paired) within the day; otherwise the chip's words
  const DAY_MS = 86400000;
  const STALE_FIX = "If this device asks for a pairing code, revoke it here and pair it again.";
  function quietFor(d, now) {
    const seen = stampMs(d.last_seen);
    if (!Number.isNaN(seen)) {
      const days = Math.floor((now - seen) / DAY_MS);
      return days >= 1 ? "not seen for " + days + (days === 1 ? " day" : " days") : "";
    }
    const made = stampMs(d.created);
    return !Number.isNaN(made) && now - made > DAY_MS ? "never seen" : "";
  }

  // `now` and `quiet` come from the one paint that draws this row, so the tab's fix line and the chip agree
  function row(d, now, quiet) {
    const r = el("div", "dev-row" + (d.revoked_at ? " dev-revoked" : ""));
    const main = el("div", "dev-main");
    const name = el("div", "dev-name", d.label || d.id);
    name.dir = "auto";                                              // the label alone: its own direction
    main.appendChild(name);
    const meta = el("div", "dev-meta", "paired " + when(d.created, now) + " · last seen " + when(d.last_seen, now) + (d.paired_by ? " · by " + d.paired_by : "") + " · ");
    meta.appendChild(el("span", "dev-id", shortId(d.id)));          // no-wrap: the id never breaks at its hyphen on a phone
    main.appendChild(meta);
    r.appendChild(main);
    if (d.revoked_at) {
      r.appendChild(el("span", "pill dev-pill", "revoked " + when(d.revoked_at, now)));
    } else {
      const b = el("button", "dev-btn", "Revoke");
      b.type = "button";
      b.setAttribute("aria-label", "Revoke " + named(d));
      b.addEventListener("click", () => revoke(d, b));
      r.appendChild(b);
      // from devices.json `subscribed`: any live push subscription under this device
      meta.appendChild(document.createTextNode(" · " + (d.subscribed ? "pings on" : "pings off")));
      // A key the server holds is not a device that is using it: one that moved origin or lost its storage still reads paired
      if (quiet) {
        r.appendChild(el("span", "pill dev-pill dev-stale", quiet));
      }
    }
    return r;
  }

  // Draws the chosen tab from the last read. Active devices newest-paired first; revoked ones newest-revoked first. The Revoked
  // tab exists only while something is revoked, and its count rides its label, so a long history is never hidden silently.
  // A stamp as milliseconds for sorting; one that does not parse sorts last.
  // (named apart from when(), the readable age: a second `when` here would replace it for every caller)
  function stamp(v) {
    const t = stampMs(v);
    return Number.isNaN(t) ? 0 : t;
  }
  function paint() {
    const now = Date.now();
    const live = lastDevs.filter((d) => !d.revoked_at).sort((a, b) => stamp(b.created) - stamp(a.created));
    const gone_ = lastDevs.filter((d) => d.revoked_at).sort((a, b) => stamp(b.revoked_at) - stamp(a.revoked_at));
    if (!gone_.length) tab = "active";
    tabsNav.hidden = !gone_.length;
    // "Paired", not "Active": it counts the devices that hold a key, which is not the same as being connected
    tabBtns.active.textContent = "Paired (" + live.length + ")";
    tabBtns.revoked.textContent = "Revoked (" + gone_.length + ")";
    for (const k of ["active", "revoked"]) {
      tabBtns[k].setAttribute("aria-selected", k === tab ? "true" : "false");
      tabBtns[k].tabIndex = k === tab ? 0 : -1;
    }
    list.setAttribute("aria-labelledby", "dev-tab-" + tab);
    list.dataset.paints = String((Number(list.dataset.paints) || 0) + 1);   // a count tests can wait on: one per completed paint
    list.replaceChildren();
    const shown = tab === "revoked" ? gone_ : live;
    if (!lastDevs.length) list.appendChild(el("p", "dev-none", "No device is paired yet. Pair one below: it gets its own key, and you can revoke it here."));
    else if (!shown.length) list.appendChild(el("p", "dev-none", "No active device. Pair one below: it gets its own key, and you can revoke it here."));
    // one line for the whole tab, not one per row: how to fix a device that holds a key it can no longer use
    const quiet = new Map(shown.map((d) => [d, d.revoked_at ? "" : quietFor(d, now)]));
    if (tab !== "revoked" && [...quiet.values()].some((q) => q)) list.insertBefore(el("p", "dev-none dev-stale-note", STALE_FIX), list.firstChild);
    for (const d of shown) list.appendChild(row(d, now, quiet.get(d)));
  }

  // One read and one paint. Resolves true when the panel is as current as it can be (including "nothing to show here"),
  // false when the read failed and said so.
  async function readOnce() {
    if (gone) return true;
    const tok = token();
    if (!tok) { devShown(false); return true; }
    if (tok === refused) { devShown(false); return true; }
    let res;
    try { res = await api("GET", "/cockpit/pair/devices.json", undefined, READ_TIMEOUT_MS); }
    catch (_) { if (!devPanel.hidden) say("Couldn't refresh the device list."); return false; }
    if (res.status !== 200 || !Array.isArray(res.data.devices)) {
      // 404: a read-only or ledger-less server (no pairing): stable, stop asking. 403: this token is refused; stop asking with
      // it, ask again once the stored token is a different one.
      if (res.status === 404) { devShown(false); gone = true; return true; }
      if (res.status === 403) { devShown(false); refused = tok; return true; }
      say("Couldn't refresh the device list.");
      return false;
    }
    if (note.textContent.endsWith("Couldn't refresh the device list.")) say("");   // G2: also the "Revoked X. Couldn't refresh…" form
    devShown(true);
    lastDevs = res.data.devices;
    paint();
    if (!labelIn.value) labelIn.placeholder = "name the new device, e.g. iPad";
    pings();
    return true;
  }

  // ---- notify tokens: what lets one of your own services post into Notifications (POST /notify) ------------------------------
  // Shown wherever the full credential reads the list (the push family's gate lets nothing else through); the new token is
  // in the mint reply alone, shown once in this node and kept nowhere, and the list never carries a hash. The block joins the
  // Devices grid beside Paired devices (S4.2, C7) and kicks its own read: a first read once the stored token has arrived, one on a
  // tab brought back, and a slow pulse.
  let nfPanel = null, nfList = null, nfNew = null, nfNote = null, nfName = null, nfSource = null, nfUrgent = null, nfBusy = false, nfGen = 0, nfReadId = 0, nfReading = false;
  function buildNotify() {
    nfPanel = el("div", "panel k-list dev-panel nf-panel");
    nfPanel.dataset.modes = "board";
    nfPanel.hidden = true;
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Notify tokens"));
    nfPanel.appendChild(ph);
    const body = el("div", "pbody dev-body");
    body.appendChild(el("p", "dev-note nf-intro", "A notify token lets one of your own services post into Notifications, and nothing else."));
    nfList = el("div", "dev-list nf-list");
    body.appendChild(nfList);
    const form = el("form", "nf-form");
    nfName = el("input", "dev-label nf-name");
    nfName.type = "text"; nfName.maxLength = 40; nfName.placeholder = "Name, e.g. Backup";
    nfName.setAttribute("aria-label", "Name for the new notify token");
    form.appendChild(nfName);
    nfSource = el("input", "dev-label nf-source");
    nfSource.type = "text"; nfSource.maxLength = 24; nfSource.placeholder = "Label, lowercase (optional)";
    nfSource.setAttribute("aria-label", "Source label for the new notify token");
    form.appendChild(nfSource);
    const boxes = el("div", "nf-checks");
    const l = el("label", "nf-check");
    nfUrgent = el("input", "nf-urgent"); nfUrgent.type = "checkbox"; nfUrgent.checked = false;
    l.appendChild(nfUrgent); l.appendChild(document.createTextNode(" urgent: this token can wake you through sleep"));
    boxes.appendChild(l);
    form.appendChild(boxes);
    const go = el("button", "dev-btn nf-mint", "Create notify token");
    go.type = "submit";
    form.appendChild(go);
    form.addEventListener("submit", (e) => { e.preventDefault(); createNotify(); });
    body.appendChild(form);
    nfNew = el("div", "dev-link nf-new"); nfNew.hidden = true;
    body.appendChild(nfNew);
    nfNote = el("p", "dev-note nf-note"); nfNote.hidden = true;
    body.appendChild(nfNote);
    nfPanel.appendChild(body);
    setTimeout(readNotify, 2500);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) readNotify(); });
    setInterval(() => { if (!document.hidden) readNotify(); }, 30000);
    return nfPanel;
  }
  function nfShow(on) { nfPanel.hidden = !on; if (on && section) section.hidden = false; }
  function nfSay(msg) { nfNote.textContent = msg; nfNote.hidden = !msg; }
  function paintNotify(tokens) {
    nfList.replaceChildren();
    if (!tokens.length) { nfList.appendChild(el("p", "dev-none", "No notify tokens yet.")); return; }
    for (const t of tokens) {
      const row = el("div", "dev-row nf-row");
      row.dataset.name = t.name;
      const main = el("div", "dev-main");
      main.appendChild(el("div", "dev-name", t.name + (t.source && t.source !== t.name ? " (" + t.source + ")" : "")));
      main.appendChild(el("div", "dev-meta", (t.urgent ? "May wake you through sleep" : "Normal and low only") + " · "
        + t.rate_per_min + "/min, " + t.rate_per_day + "/day · last used " + when(t.last_used)));
      row.appendChild(main);
      const rv = el("button", "dev-btn nf-revoke", "Revoke");
      rv.type = "button";
      rv.addEventListener("click", () => revokeNotify(t.name));
      row.appendChild(rv);
      nfList.appendChild(row);
    }
  }
  async function readNotify() {
    if (nfBusy || nfReading) return;
    if (!token()) { nfShow(false); return; }
    const startedAt = nfGen, mine = ++nfReadId;     // a read begun before a mutation never paints over it
    nfReading = true;
    try {
      const res = await api("GET", "/webpush/notify/tokens", undefined, READ_TIMEOUT_MS);
      if (startedAt !== nfGen || mine !== nfReadId) return;
      if (res.status === 200 && res.data && Array.isArray(res.data.tokens)) { paintNotify(res.data.tokens); nfShow(true); }
      else if (res.status === 403 || res.status === 404) nfShow(false);
    } catch (_) { /* the next pulse tries again */ }
    finally { if (mine === nfReadId) nfReading = false; }
  }
  async function notifyCall(path, body) {
    nfSay("");
    try {
      const res = await api("POST", path, body);
      if (res.status === 200 && res.data) { if (Array.isArray(res.data.tokens)) paintNotify(res.data.tokens); return res.data; }
      nfSay("Could not change the notify tokens (" + ((res.data && res.data.message) || res.status) + ").");
    } catch (_) { nfSay("Could not reach the cockpit."); }
    return null;
  }
  async function createNotify() {
    const name = nfName.value.trim();
    if (!name) { nfSay("Give the notify token a name."); return; }
    if (nfBusy) return;     // a second mint would replace the first one's only shown secret
    nfBusy = true; nfGen++;
    try {
      const body = { name: name };
      const src = nfSource.value.trim();
      if (src) body.source = src;
      if (nfUrgent.checked) body.urgent = true;
      const got = await notifyCall("/webpush/notify/tokens", body);
      if (!got || !got.token) return;
      nfName.value = ""; nfSource.value = ""; nfUrgent.checked = false;
      showNewNotify(got.token, got.record || {});
    } finally { nfBusy = false; nfGen++; }
  }
  function notifyCurl(tok) {
    return "curl -H \"Authorization: Bearer " + tok + "\" -H \"Content-Type: application/json\" -d '{\"title\":\"..\",\"body\":\"..\"}' "
      + window.location.origin + "/notify";
  }
  function showNewNotify(tok, rec) {
    nfNew.replaceChildren();
    nfNew.dataset.name = String(rec.name || "");
    nfNew.appendChild(el("div", "dev-name", "Token for " + (rec.name || "this service") + ": copy it now, it is not shown again"));
    const line = el("input", "dev-url nf-token");
    line.type = "text"; line.readOnly = true; line.value = tok;
    line.setAttribute("aria-label", "The new notify token");
    nfNew.appendChild(line);
    const cmd = notifyCurl(tok);
    const box = el("pre", "dev-url nf-curl", cmd);
    nfNew.appendChild(box);
    const copy = el("button", "dev-btn nf-copy", "Copy the curl");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(cmd); copy.textContent = "Copied"; }
      catch (_) { line.focus(); line.select(); copy.textContent = "Select the token and copy"; }
    });
    nfNew.appendChild(copy);
    nfNew.appendChild(el("p", "dev-link-n", "Text only: a title, a body and a priority (low, normal" + (rec.urgent ? ", urgent" : "") + "). Anything else is refused."));
    nfNew.hidden = false;
  }
  async function revokeNotify(name) {
    if (nfBusy) return;
    nfBusy = true; nfGen++;
    try {
      // the one-time secret on screen goes only when ITS token's revoke succeeded, never on a refusal or another token's
      const got = await notifyCall("/webpush/notify/tokens/revoke", { name: name });
      if (got && nfNew.dataset.name === name) { nfNew.hidden = true; nfNew.replaceChildren(); delete nfNew.dataset.name; }
    } finally { nfBusy = false; nfGen++; }
  }

  // ---- pings (OS-4) -------------------------------------------------------------------------------------------------
  // The page's registration of /sw.js, or null (none yet: pings were never turned on in this browser)
  function registration() {
    return navigator.serviceWorker.getRegistration("/").catch(() => null);
  }
  // Where the browser withholds the push API (a plain-http address that is not localhost has no service worker), the row
  // still shows when the cockpit serves push, and says why, instead of vanishing or a toggle that cannot work
  const INSECURE_SAY = "off: this browser only offers pings on https or localhost, and this address is neither. Open the cockpit at its https address.";
  async function pings() {
    if (!pushAsked) {
      pushAsked = true;
      try {
        const res = await api("GET", "/webpush/key", undefined, READ_TIMEOUT_MS);
        if (res.status === 200 && typeof res.data.key === "string" && res.data.key) pushKey = res.data.key;
        else if (res.status !== 404) { pushAsked = false; return; }   // a 5xx, an odd answer or a 200 with no key: ask again on the next read
      } catch (_) { pushAsked = false; return; }      // no answer: ask again on the next read
    }
    if (!pushKey) return;                               // this cockpit serves no web push: no row
    pingRow.hidden = false;
    if (!PUSH_OK) {
      // no live Turn on without the push API: the words say what to do instead
      pingBtn.hidden = true; pingBtn.dataset.on = "";   // hidden, not disabled: a click that still arrives reaches turnOn(), which answers in words
      pingState.textContent = noPushSay();
      return;
    }
    pingBtn.hidden = false;
    const reg = await registration();
    const sub = reg ? await reg.pushManager.getSubscription().catch(() => null) : null;
    pingBtn.dataset.on = sub ? "1" : "";
    pingBtn.textContent = sub ? "Turn off" : "Turn on";
    pingBtn.setAttribute("aria-label", sub ? "Turn pings off on this browser" : "Turn pings on for this browser");
    pingState.textContent = sub ? "on: a sign request or a question pings this browser"
      : Notification.permission === "denied" ? "off: this browser blocks notifications from this page"
      : "off: turn on to be pinged here when the cockpit needs you";
  }
  async function turnOn() {
    if (!PUSH_OK) { pings(); return; }   // no Notification here: calling it would throw; the pings row already holds the words, once
    pingBtn.disabled = true; say("");
    let sub = null;
    try {
      // the permission prompt must come straight from the tap, so it is the first thing this handler does
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { say("Pings stay off: the browser did not allow notifications for this page."); return; }
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      const reg = await navigator.serviceWorker.ready;
      const old = await reg.pushManager.getSubscription();
      if (old) await old.unsubscribe().catch(() => {});
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(pushKey) });
      const j = sub.toJSON();
      let res;
      try { res = await api("POST", "/webpush/subscribe", { endpoint: j.endpoint, keys: j.keys }); }
      catch (_) {
        // the request may have reached the cockpit: keep the browser's subscription, and do not say nothing changed
        sub = null;
        say(shared().subscribeLost || "Pings may have been turned on, but no answer came back. Check the Devices panel before trying again.");
        return;
      }
      if (res.status !== 200) {
        // a browser subscription the cockpit does not hold would read "on" and never ping
        await sub.unsubscribe().catch(() => {});
        say("Could not turn pings on (" + (res.data.message || res.status) + ").");
        return;
      }
      // the cockpit paired this browser: keep its device key where pair.js keeps a claimed one, then use it
      const minted = typeof res.data.token === "string" && /^[\x21-\x7e]+$/.test(res.data.token) ? res.data.token : "";
      let kept = !minted;
      if (minted) {
        try { window.localStorage.setItem(WRITE_TOKEN_KEY, minted); kept = true; } catch (_) { /* said below */ }
        if (kept) {
          try { window.localStorage.setItem(PAIRED_MARK_KEY, "1"); } catch (_) { /* the key is kept; the mark is not */ }
          try { window.dispatchEvent(new Event("levain:write-token-change")); } catch (_) { /* no events */ }
        }
      }
      say(kept ? "Pings are on for this browser." : "Pings are on for this browser, but it could not keep its own key (storage is blocked here); revoke its row to stop them.");
      if (minted) refresh();
    } catch (e) {
      if (sub) await sub.unsubscribe().catch(() => {});
      say(shared().turnOnFailed ? shared().turnOnFailed(e) : "Could not turn pings on. (" + ((e && e.message) || e) + ")");
    } finally { pingBtn.disabled = false; pings(); }
  }
  async function turnOff() {
    pingBtn.disabled = true; say("");
    try {
      const reg = await registration();
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      if (sub) {
        const endpoint = sub.endpoint;
        await sub.unsubscribe();
        await api("POST", "/webpush/unsubscribe", { endpoint: endpoint });
      }
      say("Pings are off for this browser.");
    } catch (e) { say("Could not turn pings off: " + ((e && e.message) || e)); }
    finally { pingBtn.disabled = false; pings(); }
  }

  function refresh() {
    if (running) { rerun = true; return running; }     // join the read in flight; it runs once more when it settles
    running = (async () => {
      let ok = true;
      try {
        do { rerun = false; ok = await readOnce(); } while (rerun && !gone);
      } catch (_) { ok = false; }
      finally { running = null; rerun = false; }
      return ok;
    })();
    return running;
  }

  // Both offer buttons are off while either request is in flight: the server keeps one live short code, and a second request
  // would replace what the first is about to show.
  async function offer(short) {
    if (offerBtns.some((b) => b.disabled)) return;            // one request at a time, whatever called it
    for (const b of offerBtns) b.disabled = true;
    say("");
    const what = short ? "a pairing code" : "a pairing link";
    try {
      const label = (labelIn.value || "").trim();
      const body = label ? { label: label } : {};
      if (short) body.short = true;
      const res = await api("POST", "/cockpit/pair/offer", body);
      const shown = short ? typeof res.data.short === "string" : !!res.data.pair_path;
      if (res.status !== 200 || !shown) {
        say(res.status === 503 ? "The cockpit is busy. Try again in a moment." : "Could not make " + what + " (" + (res.data.message || res.status) + ").");
        return;
      }
      if (short) showCode(res.data.short, res.data.short_ttl_seconds || 600);
      else showLink(location.origin + res.data.pair_path, res.data.expires_in_s || 600);
      labelIn.value = "";
      tab = "active";   // the new device is an active one: show it, not the Revoked list
      paint();
      refresh();
    } catch (_) {
      say("Could not reach the cockpit.");
    } finally { for (const b of offerBtns) b.disabled = false; }
  }

  // The short code, large, with a countdown; a new one replaces any code shown before (the server keeps one live code).
  let codeTimer = null;
  function showCode(code, secs) {
    if (codeTimer) clearInterval(codeTimer);
    offerBox.replaceChildren();
    offerBox.hidden = false;
    offerBox.appendChild(el("div", "dev-link-h", "Type this code on the new device"));
    offerBox.appendChild(el("div", "dev-code", code));
    const left = el("div", "dev-link-n");
    offerBox.appendChild(left);
    offerBox.appendChild(el("div", "dev-link-n", "Works once. This replaces any code shown before."));
    const end = Date.now() + secs * 1000;
    const tick = () => {
      const s = Math.max(0, Math.round((end - Date.now()) / 1000));
      left.textContent = s ? "Expires in " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0") : "This code has expired. Show a new one.";
      if (!s) { clearInterval(codeTimer); codeTimer = null; }
    };
    tick();
    codeTimer = setInterval(tick, 1000);
  }

  function showLink(url, secs) {
    if (codeTimer) { clearInterval(codeTimer); codeTimer = null; }
    offerBox.replaceChildren();
    offerBox.hidden = false;
    offerBox.appendChild(el("div", "dev-link-h", "Open this link on the new device"));
    const inp = el("input", "dev-url");
    inp.type = "text"; inp.readOnly = true; inp.value = url;
    inp.setAttribute("aria-label", "Pairing link");
    inp.addEventListener("focus", () => inp.select());
    const copy = el("button", "dev-btn", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(url); copy.textContent = "Copied"; }
      catch (_) { inp.focus(); inp.select(); copy.textContent = "Select and copy"; }
      setTimeout(() => { copy.textContent = "Copy"; }, 2000);
    });
    const line = el("div", "dev-url-row"); line.appendChild(inp); line.appendChild(copy);
    offerBox.appendChild(line);
    offerBox.appendChild(el("div", "dev-link-n", "Works once, for " + Math.round(secs / 60) + " minutes. The code is in the part after the # and never reaches a server log."));
    if (/^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname)) {
      offerBox.appendChild(el("div", "dev-link-w", "This address only works on this machine. To pair a phone or iPad, open the cockpit by its tailnet name first, then make the link."));
    }
  }

  async function revoke(d, btn) {
    btn.disabled = true; say("");
    let msg = "", revoked = false;
    try {
      const res = await api("POST", "/cockpit/pair/revoke", { id: d.id });
      const n = (res.data.revoked || []).length;
      if (res.status !== 200) msg = "Could not revoke " + named(d) + " (" + (res.data.message || res.status) + ").";
      else { revoked = true; msg = n > 1 ? "Revoked " + named(d) + " and " + (n - 1) + " device(s) it had paired." : "Revoked " + named(d) + "."; }
    } catch (_) { msg = "Could not reach the cockpit."; }
    // G2: a failed revoke keeps a live button; a successful one leaves the row to the re-render (no second click on a revoked key)
    if (!revoked && btn.isConnected) btn.disabled = false;
    const listed = await refresh();
    // after the refresh, so its own note does not wipe the revoke's answer; a refresh error after a good revoke is added, not lost
    say(revoked && !listed ? msg + " Couldn't refresh the device list." : msg);
  }

  // NP S2: the Notifications block. The operator's own policy for what reaches him while a mute word (quiet, sleep) is on:
  // GET /webpush/policy reads it (levain.webpush.presence.policy_view), POST /webpush/policy/set {mute, class, value} and
  // /webpush/policy/reset write it, under the same token as every other write on this panel. Hidden where the cockpit
  // serves no web push (a 404) or the token is refused, on its own: the pairing read does not decide it. The words come from the server (`sentences`, `status`).
  const MUTES = [["quiet", "Quiet"], ["sleep", "Sleep"]];
  const CLASSES = [["approval", "Approvals", "seats asking you to say yes or no"],
                   ["attention", "Attention items", "things that need you, dated ones included"],
                   ["other", "Other pings", "questions, rulings, summaries"]];
  const HOLD_WARN = "Seats that need your approval will wait silently until you unmute.";
  const DEFAULT_NOTE = "By default, approvals and urgent pings still reach you while you sleep; everything else waits until you are back.";
  let npPanel = null, npSentences = null, npStatus = null, npNote = null, npCells = {}, npReset = null, npBusy = false, npDefault = null,
      npGen = 0, npGone = false, npRefused = "", npResetOff = false;   // npGen: bumped by every write; an older read is dropped

  function buildPolicy() {
    npPanel = el("div", "panel k-list dev-panel np-panel");
    npPanel.dataset.modes = ALL_MODES;
    npPanel.hidden = true;
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Notifications"));
    npPanel.appendChild(ph);
    const body = el("div", "pbody dev-body");
    npSentences = el("div", "np-sentences");
    body.appendChild(npSentences);
    npStatus = el("p", "dev-note np-status"); npStatus.hidden = true;
    body.appendChild(npStatus);
    npDefault = el("p", "dev-note np-default", DEFAULT_NOTE); npDefault.hidden = true;
    body.appendChild(npDefault);
    for (const [mute, mname] of MUTES) {
      const grp = el("div", "np-group");
      grp.appendChild(el("div", "dev-name", "While " + mname.toLowerCase() + " is on"));
      for (const [cls, cname, gloss] of CLASSES) {
        const r = el("div", "dev-row np-row");
        r.dataset.cell = mute + "." + cls;
        const main = el("div", "dev-main");
        main.appendChild(el("div", "np-cls", cname));
        main.appendChild(el("div", "dev-meta", gloss));
        const warn = el("div", "np-warn", HOLD_WARN);
        warn.setAttribute("role", "alert");
        warn.hidden = true;
        if (cls === "approval") main.appendChild(warn);
        r.appendChild(main);
        const seg = el("div", "ck-tabs np-seg");
        seg.setAttribute("role", "group");
        seg.setAttribute("aria-label", cname + " while " + mname.toLowerCase() + " is on");
        const btns = {};
        for (const v of ["push", "hold"]) {
          const b = el("button", "ck-tab", v === "push" ? "Reach me" : "Wait");
          b.type = "button";
          b.dataset.value = v;
          b.addEventListener("click", () => setCell(mute, cls, v));
          btns[v] = b; seg.appendChild(b);
        }
        r.appendChild(seg);
        npCells[mute + "." + cls] = { btns: btns, warn: warn, cls: cls };
        grp.appendChild(r);
      }
      body.appendChild(grp);
    }
    npReset = el("button", "dev-btn np-reset", "Reset to default");
    npReset.type = "button";
    npReset.addEventListener("click", resetPolicy);
    const foot = el("div", "np-foot");
    foot.appendChild(npReset);
    body.appendChild(foot);
    npNote = el("p", "dev-note"); npNote.hidden = true;
    body.appendChild(npNote);
    npPanel.appendChild(body);
    return npPanel;
  }

  function npSay(msg) { npNote.textContent = msg; npNote.hidden = !msg; }

  function paintPolicy(v) {
    npSentences.replaceChildren();
    for (const [mute] of MUTES) npSentences.appendChild(el("p", "np-sentence", (v.sentences || {})[mute] || ""));
    const bad = v.status_kind === "malformed";
    npStatus.hidden = v.status_kind === "default";
    npStatus.className = "dev-note np-status" + (bad ? " np-bad" : "");
    npStatus.textContent = bad ? "The policy file is not usable: " + (v.reasons || []).join("; ") + ". The default is in effect until you set a cell or reset."
                               : "Your own table is in effect, not the default.";
    for (const [mute] of MUTES) for (const [cls] of CLASSES) {
      const c = npCells[mute + "." + cls], cur = ((v.cells || {})[mute] || {})[cls];
      for (const k of ["push", "hold"]) {
        c.btns[k].setAttribute("aria-pressed", k === cur ? "true" : "false");
      }
      // the warning is for a hold that is not the shipped default's own; the server names the default cells
      const dflt = (((v.default_cells || {})[mute]) || {})[cls];
      c.warn.hidden = !(cls === "approval" && cur === "hold" && cur !== dflt);
    }
    // approvals held ONLY by the shipped default: one plain, non-red line, shown when the server says a cell holds by default
    npDefault.hidden = !Object.values(v.held_by_default || {}).some(Boolean);
    npResetOff = v.status_kind === "default";
    npReset.disabled = npBusy || npResetOff;
    npShown(true);
  }

  async function readPolicy() {
    if (npGone) return;
    const tok = token();
    if (!tok || tok === npRefused) { npShown(false); return; }
    const startedAt = npGen;
    try {
      const res = await api("GET", "/webpush/policy", undefined, READ_TIMEOUT_MS);
      if (startedAt !== npGen) return;                                                  // a write landed since this read began: it is stale
      if (res.status === 200 && res.data && res.data.cells) paintPolicy(res.data);
      else if (res.status === 404) { npShown(false); npGone = true; }            // no web push here
      else if (res.status === 403) { npShown(false); npRefused = tok; }          // this token is not allowed: stop asking
    } catch (_) { /* the next pulse tries again */ }
  }

  async function writePolicy(path, body) {
    if (npBusy) return;
    npBusy = true; npGen++; npSay("");
    for (const k in npCells) for (const b of Object.values(npCells[k].btns)) b.disabled = true;
    npReset.disabled = true;
    try {
      const res = await api("POST", path, body);
      npGen++;                                                                    // reads begun while the write ran are stale too
      if (res.status === 200 && res.data && res.data.cells) paintPolicy(res.data);
      else npSay("Could not change the policy (" + ((res.data && res.data.message) || res.status) + ").");
    } catch (_) { npSay("Could not reach the cockpit."); }
    finally {
      try { await readPolicy(); }   // the server's word on what is in effect, after a refusal too; the buttons wait for it to paint
      finally {
        npBusy = false;
        for (const k in npCells) for (const b of Object.values(npCells[k].btns)) b.disabled = false;
        npReset.disabled = npResetOff;
      }
    }
  }
  function setCell(mute, cls, value) { return writePolicy("/webpush/policy/set", { mute: mute, class: cls, value: value }); }
  function resetPolicy() { return writePolicy("/webpush/policy/reset", {}); }

  // ---- Presence. The word that says whether pings reach you lives in the masthead: a chip on every page (state and time left)
  // that opens a small sheet of big buttons, so setting it as you come and go takes a tap or two. GET /webpush/presence reads
  // it ({word, until, text, route, route_source, route_until, effective, tokens}); POST /webpush/presence/set {word, for?} writes it.
  // The chip paints `effective` ({state: pushing|holding|muted, reason, source: word|measured}), the one answer the push rule gives
  // right now; with none (an older server) it paints the stored word. The route ({route, route_source, route_until}) says where an ordinary ping goes while you are active at the laptop: Desk
  // holds it for the terminal band, Devices pushes it to your phone; {word:"desk"|"devices", for?} sets the route alone.
  // Below the page sits the quieter part (tap tokens a Shortcut or an NFC tag uses to set the word with no location fix, and
  // the Notifications policy), reached from the sheet's link. Same token as every other write here; the chip hides on a 404
  // (no web push) or a refused token, without a word of error in the header.
  const PR_WORDS = [["back", "Here"], ["away", "Away"], ["quiet", "Quiet"], ["sleep", "Sleep"], ["loud", "Loud"]];
  const PR_ROUTES = [["desk", "Desk"], ["devices", "Devices"]];
  // the tap-token form names the word the server knows (back), not the sheet's friendlier "Here"
  const PR_TAP_WORDS = [["back", "Back"]].concat(PR_WORDS.slice(1), PR_ROUTES);
  const PR_TIMED = ["quiet", "loud"];
  // "" is "until back": away and sleep already end on his return and take no duration; a timed word or the Devices route
  // needs an end, so it takes the longest the server allows
  const PR_DURATIONS = [["15m", "15m"], ["1h", "1h"], ["2h", "2h"], ["", "Until back"]];
  const PR_CAP = "24h";
  const PR_DUR_KEY = "levain_presence_for";
  const PR_TAP_DEFAULT = ["away", "back", "quiet", "desk", "devices"];
  const PR_WORD_GLOSS = { back: "pings reach you as usual", away: "held for your phone", quiet: "only what needs you reaches you",
                          sleep: "only what needs you reaches you, until you are active", loud: "everything reaches you" };
  const PR_ROUTE_GLOSS = { desk: "held for the terminal while you are active at the laptop",
                           devices: "pushed to your phone and cockpit even while you are active" };
  let prPanel = null, prChip = null, prChipText = null, prSheet = null, prSheetText = null, prSheetWho = null, prBtns = {}, prRouteBtns = {}, prDurBtns = {}, prRoute = null,
      prEndLoud = null, prSheetNote = null, prLink = null, prDur = "1h", prSetSec = null, prTapList = null, prNote = null,
      prNew = null, prName = null, prChecks = {}, prBusy = false, prGen = 0, prReadId = 0, prGone = false, prRefused = "", prCur = null;

  function loadDur() {
    try {
      const v = window.localStorage.getItem(PR_DUR_KEY);
      if (v !== null && PR_DURATIONS.some((d) => d[0] === v)) return v;
    } catch (_) { /* storage blocked: the default stands for this page only */ }
    return "1h";
  }
  function saveDur(v) { try { window.localStorage.setItem(PR_DUR_KEY, v); } catch (_) { /* not remembered */ } }

  // The chip is in the page's own masthead markup, so it exists on every mode; this only wires and fills it.
  function buildChip() {
    prChip = document.getElementById("presencebtn");
    if (!prChip) return;
    prChipText = prChip.querySelector(".pr-chip-t");
    prChip.addEventListener("click", () => { if (prSheet.hidden) openSheet(); else closeSheet(true); });
  }

  function buildSheet() {
    prDur = loadDur();
    prSheet = el("div", "pr-sheet");
    prSheet.id = "pr-sheet";
    prSheet.setAttribute("role", "dialog");
    prSheet.setAttribute("aria-label", "Presence");
    prSheet.hidden = true;
    prSheetText = el("p", "pr-sheet-text");
    prSheetText.setAttribute("aria-live", "polite");
    prSheet.appendChild(prSheetText);
    prSheetWho = el("p", "pr-sheet-who");    // two different things under two names: the desk, and you
    prSheet.appendChild(prSheetWho);
    const words = el("div", "pr-sheet-words");
    words.setAttribute("role", "group");
    words.setAttribute("aria-label", "Set your presence");
    for (const [w, label] of PR_WORDS) {
      const b = el("button", "pr-big pr-sheet-word", label);
      b.type = "button";
      b.dataset.word = w;
      b.title = PR_WORD_GLOSS[w];
      b.addEventListener("click", () => setWord(w));
      prBtns[w] = b; words.appendChild(b);
    }
    prSheet.appendChild(words);
    const rt = el("div", "pr-sheet-row");
    rt.appendChild(el("span", "pr-sheet-lab", "Pings go to"));
    const rseg = el("div", "pr-sheet-seg");
    rseg.setAttribute("role", "group");
    rseg.setAttribute("aria-label", "Where pings go while you are active");
    for (const [r, label] of PR_ROUTES) {
      const b = el("button", "pr-big pr-route-btn", label);
      b.type = "button";
      b.dataset.route = r;
      b.title = PR_ROUTE_GLOSS[r];
      b.addEventListener("click", () => setRoute(r));
      prRouteBtns[r] = b; rseg.appendChild(b);
    }
    rt.appendChild(rseg);
    prSheet.appendChild(rt);
    prRoute = el("p", "pr-sheet-route");
    prSheet.appendChild(prRoute);
    const dr = el("div", "pr-sheet-row");
    dr.appendChild(el("span", "pr-sheet-lab", "For"));
    const dseg = el("div", "pr-sheet-seg");
    dseg.setAttribute("role", "group");
    dseg.setAttribute("aria-label", "How long: Quiet, Loud and Devices");
    for (const [v, label] of PR_DURATIONS) {
      const b = el("button", "pr-big pr-dur", label);
      b.type = "button";
      b.dataset.for = v;
      b.addEventListener("click", () => { prDur = v; saveDur(v); paintDur(); });
      prDurBtns[v] = b; dseg.appendChild(b);
    }
    dr.appendChild(dseg);
    prSheet.appendChild(dr);
    // visible on a phone, where a tooltip never shows: what "Until back" does for the words that cannot stand forever
    prSheet.appendChild(el("p", "pr-sheet-route", "Until back: Desk stays as set; Quiet, Loud and Devices end after " + PR_CAP + " at most; Sleep takes no duration."));
    paintDur();
    prEndLoud = el("button", "pr-big pr-endloud", "End Loud");
    prEndLoud.type = "button"; prEndLoud.hidden = true;
    prEndLoud.addEventListener("click", () => writePresence({ word: "loud", for: "off" }));
    prSheet.appendChild(prEndLoud);
    prSheetNote = el("p", "pr-sheet-note"); prSheetNote.hidden = true;
    prSheetNote.setAttribute("role", "status");
    prSheet.appendChild(prSheetNote);
    prLink = el("a", "pr-sheet-link", "Presence settings →");
    prLink.href = "#presence-settings";
    prLink.addEventListener("click", (e) => { e.preventDefault(); closeSheet(false); toSettings(); });
    prSheet.appendChild(prLink);
    document.body.appendChild(prSheet);
  }

  function toSettings() {
    if (!prSetSec || prSetSec.hidden) return;
    prSetSec.scrollIntoView({ block: "start" });
    prSetSec.focus({ preventScroll: true });
  }
  function paintDur() { for (const [v, b] of Object.entries(prDurBtns)) b.setAttribute("aria-pressed", v === prDur ? "true" : "false"); }

  function placeSheet() {
    const r = prChip.getBoundingClientRect();
    const w = Math.min(340, window.innerWidth - 16);
    prSheet.style.setProperty("--pr-top", Math.round(r.bottom + 8) + "px");
    prSheet.style.setProperty("--pr-left", Math.round(Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))) + "px");
  }
  function openSheet() {
    if (!prCur) return;
    placeSheet();
    prSheet.hidden = false;
    prChip.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", sheetOutside, true);
    document.addEventListener("keydown", sheetKey, true);
    window.addEventListener("resize", placeSheet);
    const cur = prBtns[prCur.word || "back"] || prBtns.back;
    cur.focus();
  }
  function closeSheet(refocus) {
    if (!prSheet || prSheet.hidden) return;
    prSheet.hidden = true;
    prChip.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", sheetOutside, true);
    document.removeEventListener("keydown", sheetKey, true);
    window.removeEventListener("resize", placeSheet);
    if (refocus) prChip.focus();
  }
  function sheetOutside(e) {
    if (prSheet.contains(e.target) || prChip.contains(e.target)) return;
    closeSheet(false);
  }
  function sheetKey(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSheet(true); return; }
    if (e.key !== "Tab") return;
    // keep Tab inside the sheet while it is open
    const f = [...prSheet.querySelectorAll("button:not(:disabled), a[href]")].filter((n) => !n.hidden);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!prSheet.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }

  // The tap-token block of the settings section (the word buttons moved to the sheet)
  function buildTaps() {
    prPanel = el("div", "panel k-list dev-panel pr-panel");
    prPanel.dataset.modes = ALL_MODES;
    prPanel.hidden = true;
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Tap tokens"));
    prPanel.appendChild(ph);
    const body = el("div", "pbody dev-body");
    body.appendChild(el("p", "dev-note pr-intro", "A tap token lets one Shortcut or NFC tag set your presence, and nothing else."));
    prTapList = el("div", "dev-list pr-tap-list");
    body.appendChild(prTapList);
    const form = el("form", "pr-new-form");
    prName = el("input", "dev-label pr-name");
    prName.type = "text"; prName.maxLength = 40; prName.placeholder = "Name, e.g. Front door tag";
    prName.setAttribute("aria-label", "Name for the new tap token");
    form.appendChild(prName);
    const boxes = el("div", "pr-checks");
    for (const [w, label] of PR_TAP_WORDS) {
      const l = el("label", "pr-check");
      const c = el("input"); c.type = "checkbox"; c.value = w; c.checked = PR_TAP_DEFAULT.indexOf(w) >= 0;
      l.appendChild(c); l.appendChild(document.createTextNode(" " + label));
      prChecks[w] = c; boxes.appendChild(l);
    }
    form.appendChild(boxes);
    const go = el("button", "dev-btn", "Create tap token");
    go.type = "submit";
    form.appendChild(go);
    form.addEventListener("submit", (e) => { e.preventDefault(); createTap(); });
    body.appendChild(form);
    prNew = el("div", "dev-link pr-new"); prNew.hidden = true;
    body.appendChild(prNew);
    prNote = el("p", "dev-note"); prNote.hidden = true;
    body.appendChild(prNote);
    prPanel.appendChild(body);
    return prPanel;
  }

  // the chip and the sheet follow the read; a refusal or no web push takes the chip away quietly
  function prShow(on) {
    prPanel.hidden = !on; syncSection();
    if (prChip) prChip.hidden = !on;
    if (!on) closeSheet(false);
  }
  function prSay(msg) { prNote.textContent = msg; prNote.hidden = !msg; }
  function sheetSay(msg) { prSheetNote.textContent = msg; prSheetNote.hidden = !msg; }
  function timeLeft(until) {
    const t = stampMs(until);
    if (Number.isNaN(t)) return "";
    const m = Math.round((t - Date.now()) / 60000);
    if (m <= 0) return "ending now";
    if (m < 90) return m + " min left";
    return Math.round(m / 60) + " h left";
  }
  // "42m", "1h 5m": short enough for a masthead
  function span(ms) {
    const m = Math.max(1, Math.ceil(ms / 60000));
    if (m < 60) return m + "m";
    const h = Math.floor(m / 60), r = m % 60;
    return r ? h + "h " + r + "m" : h + "h";
  }

  // the push rule's reason, cut to a masthead's length: "you: no input at the laptop for 7 min", "route: devices (his phone shows the band)" -> "route: devices"
  function shortReason(r) {
    const rm = /^remote desk: (\S+) input (\d+) min ago/.exec(String(r || ""));   // "remote terminal 2 min ago"
    if (rm) return rm[1].replace(/-/g, " ") + " " + rm[2] + " min ago";
    r = String(r || "").replace(/\s*\(.*$/, "").replace(/^no laptop input source: read as inactive$/, "no laptop reading");
    return r.length <= 40 ? r : (/\s/.test(r[40]) ? r.slice(0, 40) : r.slice(0, 40).replace(/\s+\S*$/, "")).replace(/[\s,;:.-]+$/, "") + "\u2026";
  }
  const PR_STATES = { pushing: "Pushing", holding: "Holding", muted: "Muted" };

  function paintChip(v) {
    if (!prChip) return;
    const eff = v.effective && PR_STATES[v.effective.state] ? v.effective : null;
    // a word on file that is DECIDING shows as the word (and its time left); anything else shows what the rule is doing now
    const w = eff ? (eff.source === "word" ? (eff.word || null) : null) : v.word;
    // with no word deciding, the chip says where YOU are (measured from the laptop's input; a message to a desk counts), not what the
    // push rule did: the rule's own state stays in the sheet text and the aria label
    const you = eff && eff.you && (eff.you.state === "here" || eff.you.state === "away") ? eff.you : null;
    let name = w ? (PR_WORDS.find((x) => x[0] === w) || [w, w])[1] : (you ? (you.state === "away" ? "Away" : "Here") : (eff ? PR_STATES[eff.state] : "Here"));
    let t = name, say = name;
    const ms = w && v.until ? stampMs(v.until) - Date.now() : NaN;
    if (!Number.isNaN(ms) && ms > 0) { t += " · " + span(ms); say += ", " + span(ms) + " left"; }
    if (eff && !w) {
      const why = eff.remote ? shortReason(eff.reason) : (you ? you.short : shortReason(eff.reason));
      if (why) { t += " · " + why; say += ", " + why; }
      if (you) say += ". Pings: " + eff.state;
    }
    else if (v.route === "devices") { t += " · devices"; say += ", pings go to devices"; }
    // the word and its tail (time, route) are separate nodes so a phone's masthead can keep the word and drop the tail
    prChipText.textContent = "";
    prChipText.appendChild(el("span", "pr-chip-w", name));
    if (t.length > name.length) prChipText.appendChild(el("span", "pr-chip-x", t.slice(name.length)));
    prChip.dataset.state = w || (you && you.state === "away" ? "away" : (eff ? eff.state : "here"));
    prChip.setAttribute("aria-label", "Presence: " + say + ". Change it");
  }

  function paintPresence(v) {
    prCur = v;
    const w = v.word;
    paintChip(v);
    prSheetText.textContent = v.text || "";
    const ef = v.effective || {};
    prSheetWho.textContent = [ef.desk && ef.desk.text, ef.you && ef.you.text].filter(Boolean).join(" \u00b7 ");
    prSheetWho.hidden = !prSheetWho.textContent;
    for (const [k, b] of Object.entries(prBtns)) b.setAttribute("aria-pressed", k === w || (k === "back" && !w) ? "true" : "false");
    prEndLoud.hidden = w !== "loud";
    const r = v.route === "devices" ? "devices" : "desk";
    for (const [k, b] of Object.entries(prRouteBtns)) b.setAttribute("aria-pressed", k === r ? "true" : "false");
    const rl = PR_ROUTES.find((x) => x[0] === r)[1];
    prRoute.textContent = v.route_source === "fault" ? rl + " (the route file is unreadable: pings are being pushed)"
      : r === "devices"
      ? rl + (v.route_source === "auto" ? " (suggested: a remote login is open)" : "") + (v.route_until ? " (" + timeLeft(v.route_until) + ")" : "")
      : v.route_source === "operator" && v.route_until ? rl + " (" + timeLeft(v.route_until) + ")" : "";
    prRoute.hidden = !prRoute.textContent;
    if (Array.isArray(v.tokens)) paintTaps(v.tokens);
    prShow(true);
  }

  function paintTaps(tokens) {
    prTapList.replaceChildren();
    if (!tokens.length) { prTapList.appendChild(el("p", "dev-none", "No tap tokens yet.")); return; }
    for (const t of tokens) {
      const row = el("div", "dev-row pr-tap");
      row.dataset.name = t.name;
      const main = el("div", "dev-main");
      main.appendChild(el("div", "dev-name", t.name));
      main.appendChild(el("div", "dev-meta", "May set: " + (t.words || []).join(", ") + " · last used " + when(t.last_used)));
      row.appendChild(main);
      const rv = el("button", "dev-btn pr-revoke", "Revoke");
      rv.type = "button";
      rv.addEventListener("click", () => revokeTap(t.name));
      row.appendChild(rv);
      prTapList.appendChild(row);
    }
  }

  async function readPresence() {
    if (prGone) return;
    const tok = token();
    if (!tok || tok === prRefused) { prShow(false); return; }
    const startedAt = prGen, mine = ++prReadId;   // only the newest read paints: a slow older one never overwrites it
    try {
      const res = await api("GET", "/webpush/presence", undefined, READ_TIMEOUT_MS);
      if (startedAt !== prGen || mine !== prReadId) return;
      if (res.status === 200 && res.data && "word" in res.data) paintPresence(res.data);
      else if (res.status === 404) { prShow(false); prGone = true; }
      else if (res.status === 403) { prShow(false); prRefused = tok; }
    } catch (_) { /* the next pulse tries again */ }
  }

  function prDisable(on) {
    for (const b of Object.values(prBtns)) b.disabled = on;
    for (const b of Object.values(prRouteBtns)) b.disabled = on;
    for (const b of Object.values(prDurBtns)) b.disabled = on;
    prEndLoud.disabled = on;
  }
  async function writePresence(body) {
    if (prBusy) return;
    prBusy = true; prGen++; sheetSay(""); prDisable(true);
    let ok = false;
    try {
      const res = await api("POST", "/webpush/presence/set", body);
      prGen++;
      if (res.status === 200 && res.data && "word" in res.data) { paintPresence(res.data); ok = true; }
      else sheetSay("Could not set your presence (" + ((res.data && res.data.message) || res.status) + ").");
    } catch (_) { sheetSay("Could not reach the cockpit."); }
    finally {
      try { await readPresence(); }
      finally { prBusy = false; prDisable(false); if (ok) closeSheet(true); }
    }
  }
  function setWord(w) {
    const body = { word: w };
    if (PR_TIMED.indexOf(w) >= 0) body.for = prDur || PR_CAP;
    return writePresence(body);
  }

  function setRoute(r) {
    const body = { word: r };
    // Desk with no end is the server's longest Desk; Devices with no end would fall to an hour, so it takes the cap
    const f = prDur || (r === "devices" ? PR_CAP : "");
    if (f) body.for = f;
    return writePresence(body);
  }

  let tapBusy = false;     // one tap-token change at a time: a second create would replace the first one's only shown secret
  async function tapCall(path, body) {
    prGen++; prSay("");
    try {
      const res = await api("POST", path, body);
      prGen++;
      if (res.status === 200 && res.data) { if (Array.isArray(res.data.tokens)) paintTaps(res.data.tokens); return res.data; }
      prSay("Could not change the tap tokens (" + ((res.data && res.data.message) || res.status) + ").");
    } catch (_) { prSay("Could not reach the cockpit."); }
    return null;
  }
  async function createTap() {
    const name = prName.value.trim();
    const words = PR_TAP_WORDS.map((x) => x[0]).filter((w) => prChecks[w].checked);
    if (!name) { prSay("Give the tap token a name."); return; }
    if (!words.length) { prSay("Pick at least one word it may set."); return; }
    if (tapBusy) return;
    tapBusy = true;
    try {
      const got = await tapCall("/webpush/presence/token/new", { name: name, words: words });
      if (!got || !got.token) return;
      prName.value = "";
      showNewTap(got.token, got.record || {});
    } finally { tapBusy = false; }
  }
  // The secret is shown here once, in this node, and kept nowhere: a reload or the next read never brings it back.
  function showNewTap(tok, rec) {
    prNew.replaceChildren();
    prNew.appendChild(el("div", "dev-name", "Token for " + (rec.name || "this tap") + ": copy it now, it is not shown again"));
    const url = el("input", "dev-url");
    url.type = "text"; url.readOnly = true; url.value = tok;
    url.setAttribute("aria-label", "The new tap token");
    prNew.appendChild(url);
    const copy = el("button", "dev-btn", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(tok); copy.textContent = "Copied"; }
      catch (_) { url.focus(); url.select(); copy.textContent = "Select and copy"; }
    });
    prNew.appendChild(copy);
    const origin = window.location.origin;
    prNew.appendChild(el("p", "dev-link-n", "In Shortcuts or on the tag: send a POST to " + origin + "/webpush/presence/set"));
    prNew.appendChild(el("p", "dev-link-n", "with the header X-Levain-Presence-Token set to this token and the JSON body {\"word\":\"away\"}."));
    prNew.hidden = false;
  }
  async function revokeTap(name) {
    if (tapBusy) return;
    tapBusy = true;
    try {
      // the one-time secret on screen goes only when a revoke succeeded, never on a refusal
      const got = await tapCall("/webpush/presence/token/revoke", { name: name });
      if (got) { prNew.hidden = true; prNew.replaceChildren(); }
    } finally { tapBusy = false; }
    await readPresence();
  }

  function start() {
    build();
    refresh();
    readPolicy();
    readPresence();
    // The cockpit stores its token a moment after load when the link carried one; look again, then keep a slow pulse.
    setTimeout(() => { refresh(); readPolicy(); readPresence(); }, 2500);
    // a tab brought back after a long time repaints at once, not at the next pulse
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !prGone && !prBusy) readPresence(); });
    const pulse = setInterval(() => {
      if (gone && npGone && prGone) clearInterval(pulse);
      else if (!document.hidden) { if (!gone) refresh(); if (!npBusy) readPolicy(); if (!prBusy) readPresence(); }
    }, 30000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
