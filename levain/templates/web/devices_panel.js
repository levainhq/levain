// levain devices_panel.js — the cockpit's Devices panel (per-device pairing, PAIR_DESIGN.md REV 1-6).
//
// Lists the paired devices (GET /cockpit/pair/devices.json), mints a one-time pairing link (POST /cockpit/pair/offer {label}),
// revokes a device behind a confirm card that names the family (POST /cockpit/pair/revoke-preview {id}, then /cockpit/pair/revoke {id, expect, mode}), and turns pings on for this browser (OS-4, /webpush/*).
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
  let confirmBox = null, pingTestBtn = null, pingRow = null, pingState = null, pingBtn = null, pushKey = "", pushAsked = false;
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
    confirmBox = el("div", "dev-confirm-host");
    body.appendChild(confirmBox);
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
    pingTestBtn = el("button", "dev-btn dev-test", "Send a test ping");
    pingTestBtn.type = "button";
    pingTestBtn.addEventListener("click", () => sendTestPing());
    pingRow.appendChild(pingTestBtn);
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
    sgrid.appendChild(buildStanding());
    sgrid.appendChild(buildTaps());
    prSetSec.appendChild(sgrid);
    const main = document.querySelector("main.frame");
    if (main) { main.appendChild(section); main.appendChild(prSetSec); } else { document.body.appendChild(section); document.body.appendChild(prSetSec); }
    buildChip();
    buildSheet();
  }

  // The devices section shows with its own read; the settings section holds two blocks (the standing default, tap tokens) and shows while either does, each with its own gone/refused state.
  function syncSection() {
    section.hidden = devPanel.hidden;
    if (prSetSec) prSetSec.hidden = (!stPanel || stPanel.hidden) && (!prPanel || prPanel.hidden);
  }
  function devShown(on) { devPanel.hidden = !on; syncSection(); }

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
      pingTestBtn.hidden = true;
      pingBtn.hidden = true; pingBtn.dataset.on = "";   // hidden, not disabled: a click that still arrives reaches turnOn(), which answers in words
      pingState.textContent = noPushSay();
      return;
    }
    pingBtn.hidden = false; pingTestBtn.hidden = false;
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
  // A test ping to THIS browser, any time (the first-run card is the only other place it was ever sent). The words are the
  // pairing card's own (one copy); a subscription the push service has dropped, or none at all, says what to do instead.
  // The wait is bounded here, not in api(): a POST the server committed late must not be taken for a failed one.
  const TEST_WAIT_MS = 20000;
  async function sendTestPing() {
    if (pingTestBtn.disabled) return;
    pingTestBtn.disabled = true; say("");
    try {
      const reg = PUSH_OK ? await registration() : null;
      const sub = reg ? await reg.pushManager.getSubscription().catch(() => null) : null;
      if (!sub) {
        say(PUSH_OK && Notification.permission === "denied"
          ? "This browser blocks notifications from this page. Allow them for this site in the browser or system settings, then turn pings on."
          : "Pings are off in this browser, so there is nothing to test. Turn pings on first.");
        return;
      }
      let timer = null;
      const wait = new Promise((_, no) => { timer = setTimeout(() => no(new Error("no answer")), TEST_WAIT_MS); });
      let res;
      try { res = await Promise.race([api("POST", "/webpush/test", {}), wait]); } finally { clearTimeout(timer); }
      if (res.status === 200) {
        const d = (res.data && res.data.deliveries) || [];
        const gone = d.some((x) => x && (x.status === 410 || x.status === 404));
        const words = typeof shared().testWords === "function" ? shared().testWords(res.data) : "The test ping was sent.";
        say(gone ? "This browser's subscription is gone: turn pings on again." : words);
      } else if (res.status === 403) {
        say("This browser is not paired as a device, so it cannot be pinged. Turn pings on to pair it.");
      } else say("The test ping was refused (" + ((res.data && res.data.message) || res.status) + ").");
    } catch (_) { say("No answer came back, so the test ping may or may not arrive."); }
    finally { pingTestBtn.disabled = false; }
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

  // Revoke is a two-step: the server names the family a revoke would take, the operator sees it on a card, and the revoke
  // carries that exact set back (`expect`) with the mode they chose. A set that moved in between comes back as family_changed
  // with the fresh set, and the card is drawn again. Each Revoke click and each Cancel starts a new flow (flowGen): an answer
  // for an older flow, landing after the operator moved on, never opens, replaces or closes the card.
  let flowGen = 0;
  function closeConfirm() { confirmBox.replaceChildren(); }
  async function revoke(d, btn) {
    const g = ++flowGen;
    btn.disabled = true; say(""); closeConfirm();
    let res;
    try { res = await api("POST", "/cockpit/pair/revoke-preview", { id: d.id }); }
    catch (_) { if (btn.isConnected) btn.disabled = false; if (g === flowGen) say("Could not reach the cockpit."); return; }
    if (btn.isConnected) btn.disabled = false;
    if (g !== flowGen) return;
    if (res.status !== 200 || !Array.isArray(res.data.family)) {
      say("Could not check what revoking " + named(d) + " would take (" + (res.data.message || res.status) + ").");
      return;
    }
    if (!res.data.family.some((f) => f.id === d.id)) { refresh(); say(named(d) + " was already revoked."); return; }
    showConfirm(d, res.data, "");
  }
  // a paired-<id> credential name reads as that device's label when the list holds it; a surface name or the box reads as itself
  function pairedByWords(name) {
    const m = /^paired-(.+)$/.exec(String(name));
    const hit = m ? lastDevs.find((x) => x.id === m[1]) : null;
    return hit ? (hit.label || hit.id) : String(name);
  }
  function plural(n) { return n + (n === 1 ? " device" : " devices"); }
  function showConfirm(d, pv, warn) {
    const fam = pv.family, caller = pv.caller || null, now = Date.now(), g = flowGen;
    const card = el("div", "dev-confirm");
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", "Confirm revoking " + named(d));
    card.appendChild(el("div", "dev-confirm-h", "Revoke " + (fam.length > 1 ? "these " + fam.length + " devices?" : named(d) + "?")));
    if (warn) card.appendChild(el("div", "dev-link-w", warn));
    card.appendChild(el("div", "dev-link-n", fam.length > 1
      ? "Revoking " + named(d) + " also revokes every device it paired, and they all sign out at once. This cannot be undone."
      : "It signs out at once. This cannot be undone."));
    const ul = el("ul", "dev-confirm-list");
    let hasSelf = false;
    for (const f of fam) {
      const li = el("li", "dev-confirm-item");
      const nm = el("span", "dev-name", f.label || f.id);
      nm.dir = "auto";
      li.appendChild(nm);
      if (f.id === caller) { hasSelf = true; li.appendChild(el("span", "pill dev-pill dev-self", "THIS DEVICE: you will be signed out")); }
      li.appendChild(el("div", "dev-meta", (f.paired_by ? "paired by " + pairedByWords(f.paired_by) + " · " : "") + "last seen " + when(f.last_seen, now)));
      ul.appendChild(li);
    }
    card.appendChild(ul);
    const acts = el("div", "dev-confirm-acts");
    const bs = [];
    const go = async (mode, b) => {
      for (const x of bs) x.disabled = true;
      let msg = "", ok = false, changed = null;
      try {
        const r = await api("POST", "/cockpit/pair/revoke", { id: d.id, expect: fam.map((f) => f.id).sort(), mode: mode });
        if (r.status === 409 && r.data.error === "family_changed" && Array.isArray(r.data.family)) changed = r.data;
        else if (r.status !== 200) msg = "Could not revoke " + named(d) + " (" + (r.data.message || r.status) + ").";
        else {
          ok = true;
          const n = (r.data.revoked || []).length, kept = (r.data.detached || []).length;
          msg = mode === "only"
            ? "Revoked " + named(d) + (kept ? "; the " + plural(kept) + " it paired stay paired." : ".")
            : n > 1 ? "Revoked " + named(d) + " and " + (n - 1) + " device(s) it had paired." : "Revoked " + named(d) + ".";
        }
      } catch (_) { msg = "Could not reach the cockpit."; }
      if (g !== flowGen) {    // the operator has moved on to another card: the outcome is said, the card left alone
        await refresh();
        if (msg) say(msg);
        return;
      }
      if (changed &&!changed.family.some((f) => f.id === d.id)) {    // revoked elsewhere since the card was drawn
        closeConfirm();
        await refresh();
        say(named(d) + " was already revoked.");
        return;
      }
      if (changed) {      // the set moved since it was shown: nothing was revoked; show the fresh set
        closeConfirm();
        showConfirm(d, { family: changed.family, caller: caller }, "The devices changed while this was open. Nothing was revoked; check the list again.");
        return;
      }
      closeConfirm();
      const listed = await refresh();
      say(ok && !listed ? msg + " Couldn't refresh the device list." : msg);
    };
    const kids = fam.length > 1;
    if (kids) {
      const only = el("button", "dev-btn dev-safe", "Revoke only " + named(d) + (d.id === caller ? " and sign this one out" : ""));
      only.type = "button";
      only.title = "The devices it paired stay paired and are handed to whatever paired it";
      only.addEventListener("click", () => go("only", only));
      bs.push(only); acts.appendChild(only);
    }
    const all = el("button", "dev-btn dev-danger",
      kids ? "Revoke " + fam.length + " devices" + (hasSelf ? " and sign this one out" : "")
           : "Revoke " + named(d) + (hasSelf ? " and sign this one out" : ""));
    all.type = "button";
    all.addEventListener("click", () => go("family", all));
    bs.push(all); acts.appendChild(all);
    card.appendChild(acts);
    if (kids) card.appendChild(el("div", "dev-link-n", "\u201CRevoke only\u201D takes just " + named(d) + "; the others keep working."));
    const cancel = el("button", "dev-linkbtn", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => { flowGen++; closeConfirm(); });
    card.appendChild(cancel);
    confirmBox.replaceChildren(card);
    try { card.scrollIntoView({ block: "nearest" }); } catch (_) { /* no scrolling here */ }
    (kids ? bs[0] : all).focus();
  }

  // ---- Presence (1b: you decide, sensing informs). One record, three settings, one sentence. The masthead chip and the sheet it
  // opens paint ONLY what the server's view says: GET /webpush/presence returns levain.webpush.intent.view() (the line, the chip
  // {word, tail, state}, the standing default `base`, the standing `overlay`, the settings in force `effective`, the sensing) and
  // every POST /webpush/presence/set returns the same view after the write. Nothing here composes the sentence or keeps a state of
  // its own: what the cockpit shows after a tap is what `levain ping status` prints at that instant.
  // Writes: {preset, for?} (away back quiet sleep loud) or {devices?, follow_after?, lets_through?, until}; `until` is "switch"
  // (the standing default, base), "input" (my next word, or a message I type into a session) or a duration (15m, 1h, 90m).
  const PR_PRESETS = [["away", "Away"], ["back", "Back"], ["quiet", "Quiet"], ["sleep", "Sleep"], ["loud", "Loud"]];
  const PR_GLOSS = { away: "devices on until your next word or a message you type into a session", back: "your standing default again",
                     quiet: "only what needs you reaches you, for a while", sleep: "only what needs you reaches you, until your next word or a message you type into a session",
                     loud: "everything reaches you, for a while" };
  const PR_TIMED = ["quiet", "loud"];
  const PR_DURS = ["15m", "1h", "2h"];
  const PR_DEVICES = [["on", "On"], ["follow", "Follow"], ["off", "Off"]];
  const PR_REFRESH_EVERY = 5;                            // pulses between device-list refreshes
  const PR_PULSE_MS = 6000;                              // how often an open page re-reads the server's Follow line while visible
  const PR_AFTER = [5, 10, 15, 30, 60];                  // minutes without a typed desk message before follow turns devices on
  const PR_UNTILS = [["switch", "I switch it"], ["for", "For…"], ["input", "Until I say or type"]];
  // the tap-token form names the words the server knows: the presets, and the two aliases (desk = follow, devices = on)
  const PR_TAP_WORDS = [["back", "Back"], ["away", "Away"], ["quiet", "Quiet"], ["sleep", "Sleep"], ["loud", "Loud"],
                        ["desk", "Desk (follow)"], ["devices", "Devices (on)"]];
  const PR_TAP_DEFAULT = ["away", "back", "quiet", "desk", "devices"];
  const PR_DUR_KEY = "levain_presence_for";
  const PR_MIN = 1, PR_MAX = 1440;                        // custom minutes, as the server allows (1 minute to 24 hours)
  let prPanel = null, prChip = null, prChipText = null, prSheet = null, prSheetText = null, prSheetWho = null, prBtns = {}, prDurBtns = {},
      prEndLoud = null, prSheetNote = null, prLink = null, prDur = "1h", prSetSec = null, prTapList = null, prNote = null,
      prNew = null, prName = null, prChecks = {}, prBusy = false, prGen = 0, prRunning = null, prRerun = false, prGone = false, prRefused = "", prCur = null,
      prDevBtns = {}, prAfterBtns = {}, prAfterCustom = null, prAfterRow = null, prLetsBtns = {}, prUntilBtns = {}, prForRow = null,
      prForBtns = {}, prForCustom = null, prUntil = "switch", prForDur = "1h", prAfterOpen = false, prForOpen = false,
      stPanel = null, stDevBtns = {}, stAfterBtns = {}, stLetsBtns = {}, stAfterCustom = null, stAfterRow = null, stNote = null, stBusy = false;

  function loadDur() {
    try {
      const v = window.localStorage.getItem(PR_DUR_KEY);
      if (v !== null && PR_DURS.indexOf(v) >= 0) return v;
    } catch (_) { /* storage blocked: the default stands for this page only */ }
    return "1h";
  }
  function saveDur(v) { try { window.localStorage.setItem(PR_DUR_KEY, v); } catch (_) { /* not remembered */ } }

  // One segmented row of big buttons: [[value, label], ...] -> {value: button}; `onTap(value)` runs on a click.
  function segment(host, items, label, onTap, cls) {
    const seg = el("div", "pr-sheet-seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", label);
    const out = {};
    for (const [v, text] of items) {
      const b = el("button", "pr-big " + (cls || ""), text);
      b.type = "button";
      b.dataset.value = String(v);
      b.addEventListener("click", () => onTap(v));
      out[v] = b; seg.appendChild(b);
    }
    host.appendChild(seg);
    Object.defineProperty(out, "_seg", { value: seg });
    return out;
  }
  // the lets-through buttons carry the server's words (view.lets: value, label, describe); the page keeps no copy of them
  function paintLets(btns, lets, onTap) {
    const list = Array.isArray(lets) ? lets : [];
    const keep = new Set(list.map((o) => String(o.value)));
    // a level the server no longer offers leaves the page, so a stale button cannot post a value the server refuses
    for (const k of Object.keys(btns)) {
      if (!keep.has(k)) { btns[k].remove(); delete btns[k]; }
    }
    for (const o of list) {
      let b = btns[o.value];
      if (!b) {
        b = el("button", "pr-big", "");
        b.type = "button"; b.dataset.value = String(o.value);
        // made while a write is in flight, it starts as disabled as its neighbours (prDisable only reaches buttons that exist)
        b.disabled = prBusy;
        b.addEventListener("click", () => onTap(o.value));
        btns[o.value] = b; btns._seg.appendChild(b);
      }
      b.textContent = o.label; b.title = o.describe;
    }
  }
  function labelled(host, text) { host.appendChild(el("div", "pr-sheet-lab", text)); }
  function pressed(btns, cur) { for (const [k, b] of Object.entries(btns)) b.setAttribute("aria-pressed", String(k) === String(cur) ? "true" : "false"); }
  // a number-of-minutes field with a Set button; `onSet(minutes)` runs on Enter or the button
  function minutesField(host, label, onSet) {
    const row = el("form", "pr-custom");
    row.hidden = true;
    const inp = el("input", "pr-num");
    inp.type = "number"; inp.min = String(PR_MIN); inp.max = String(PR_MAX); inp.step = "1"; inp.inputMode = "numeric";
    inp.setAttribute("aria-label", label);
    const go = el("button", "pr-big pr-custom-go", "Set");
    go.type = "submit";
    row.appendChild(inp); row.appendChild(el("span", "pr-sheet-lab", "min")); row.appendChild(go);
    row.addEventListener("submit", (e) => {
      e.preventDefault();
      const n = Math.round(Number(inp.value));
      if (!(n >= PR_MIN && n <= PR_MAX)) { inp.setAttribute("aria-invalid", "true"); inp.focus(); return; }
      inp.removeAttribute("aria-invalid");
      onSet(n);
    });
    host.appendChild(row);
    return row;
  }

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
    prSheet.setAttribute("data-sheet-dismiss", "(max-width: 720px)");   // a bottom sheet on a phone: dragged down it closes (dashboard_core.js)
    prSheet.addEventListener("levain:sheet-dismiss", (e) => { e.preventDefault(); closeSheet(true); });
    prSheet.hidden = true;
    prSheet.appendChild(el("div", "sheet-grabber"));
    prSheetText = el("p", "pr-sheet-text");        // THE line, verbatim from the server
    prSheetText.setAttribute("aria-live", "polite");
    prSheet.appendChild(prSheetText);

    labelled(prSheet, "Presets");
    prBtns = segment(prSheet, PR_PRESETS, "Set your presence", (w) => setPreset(w), "pr-sheet-word");
    for (const [w] of PR_PRESETS) prBtns[w].title = PR_GLOSS[w];
    const dseg = el("div", "pr-sheet-seg pr-sub");
    dseg.setAttribute("role", "group");
    dseg.setAttribute("aria-label", "How long Quiet and Loud last");
    for (const v of PR_DURS) {
      const b = el("button", "pr-big pr-dur", v);
      b.type = "button"; b.dataset.value = v;
      b.addEventListener("click", () => { prDur = v; saveDur(v); pressed(prDurBtns, prDur); });
      prDurBtns[v] = b; dseg.appendChild(b);
    }
    dseg.appendChild(el("span", "pr-sheet-hint", "for Quiet and Loud"));
    prSheet.appendChild(dseg);
    pressed(prDurBtns, prDur);
    prEndLoud = el("button", "pr-big pr-endloud", "End Loud");
    prEndLoud.type = "button"; prEndLoud.hidden = true;
    prEndLoud.addEventListener("click", () => writePresence({ preset: "loud", for: "off" }));
    prSheet.appendChild(prEndLoud);

    labelled(prSheet, "Devices");
    prDevBtns = segment(prSheet, PR_DEVICES, "Whether pings reach your devices", (v) => setSetting({ devices: v }));
    prAfterRow = el("div", "pr-sheet-seg pr-sub");
    prAfterRow.setAttribute("role", "group");
    prAfterRow.setAttribute("aria-label", "Follow: minutes without input before devices come on");
    prAfterRow.appendChild(el("span", "pr-sheet-hint", "after"));
    for (const m of PR_AFTER) {
      const b = el("button", "pr-big pr-after", String(m));
      b.type = "button"; b.dataset.value = String(m);
      b.addEventListener("click", () => { prAfterOpen = false; setSetting({ follow_after: m * 60 }); });
      prAfterBtns[m] = b; prAfterRow.appendChild(b);
    }
    const cu = el("button", "pr-big pr-after", "Custom…");
    cu.type = "button"; cu.dataset.value = "custom";
    cu.addEventListener("click", () => { prAfterOpen = !prAfterOpen; paintPresence(prCur); if (prAfterOpen) prAfterCustom.querySelector("input").focus(); });
    prAfterBtns.custom = cu; prAfterRow.appendChild(cu);
    prAfterRow.appendChild(el("span", "pr-sheet-hint", "min"));
    prSheet.appendChild(prAfterRow);
    prAfterCustom = minutesField(prSheet, "Minutes without input before devices come on", (n) => { prAfterOpen = false; setSetting({ follow_after: n * 60 }); });

    labelled(prSheet, "Lets through");
    prLetsBtns = segment(prSheet, [], "What reaches your devices", (v) => setSetting({ lets_through: v }));

    labelled(prSheet, "Until");
    prUntilBtns = segment(prSheet, PR_UNTILS, "How long the next setting you tap lasts", (v) => {
      prUntil = v; prForOpen = false; paintPresence(prCur);
    });
    prForRow = el("div", "pr-sheet-seg pr-sub");
    prForRow.setAttribute("role", "group");
    prForRow.setAttribute("aria-label", "How long");
    for (const v of PR_DURS) {
      const b = el("button", "pr-big pr-dur", v);
      b.type = "button"; b.dataset.value = v;
      b.addEventListener("click", () => { prForDur = v; prForOpen = false; paintPresence(prCur); });
      prForBtns[v] = b; prForRow.appendChild(b);
    }
    const fc = el("button", "pr-big pr-dur", "Custom…");
    fc.type = "button"; fc.dataset.value = "custom";
    fc.addEventListener("click", () => { prForOpen = !prForOpen; paintPresence(prCur); if (prForOpen) prForCustom.querySelector("input").focus(); });
    prForBtns.custom = fc; prForRow.appendChild(fc);
    prSheet.appendChild(prForRow);
    prForCustom = minutesField(prSheet, "Minutes the next setting lasts", (n) => { prForDur = n + "m"; prForOpen = false; paintPresence(prCur); });
    prSheet.appendChild(el("p", "pr-sheet-hint pr-until-note", "The choice applies to the next setting you tap, then goes back to I switch it."));

    prSheetWho = el("p", "pr-sheet-who");          // what is sensed: read only
    prSheet.appendChild(prSheetWho);
    prSheetNote = el("p", "pr-sheet-note"); prSheetNote.hidden = true;
    prSheetNote.setAttribute("role", "status");
    prSheet.appendChild(prSheetNote);
    prLink = el("a", "pr-sheet-link", "Tap tokens and standing default →");
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

  function placeSheet() {
    const r = prChip.getBoundingClientRect();
    const w = prSheet.getBoundingClientRect().width || 380;   // CSS owns the width: 380 on a desktop, 420 on an iPad (and the phone's bottom sheet)
    prSheet.style.setProperty("--pr-top", Math.round(r.bottom + 8) + "px");
    prSheet.style.setProperty("--pr-left", Math.round(Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))) + "px");
  }
  function openSheet() {
    if (!prCur) return;
    prSheet.hidden = false;
    placeSheet();
    prChip.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", sheetOutside, true);
    document.addEventListener("keydown", sheetKey, true);
    window.addEventListener("resize", placeSheet);
    readPresence();                                       // the line is as fresh as the tap that opened it
    const cur = prBtns[(prCur.overlay && prCur.overlay.preset) || "back"] || prBtns.back;
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
    const f = [...prSheet.querySelectorAll("button:not(:disabled), a[href], input")].filter((n) => !n.hidden && !n.closest("[hidden]"));
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (!prSheet.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }

  // The Standing default block (the settings section): the same three rows bound to `base` only, so "Back" always has
  // somewhere to return to that he chose. A tap here writes the standing default and clears any overlay (until = switch).
  function buildStanding() {
    stPanel = el("div", "panel k-list dev-panel pr-standing");
    stPanel.dataset.modes = ALL_MODES;
    stPanel.hidden = true;
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Standing default"));
    stPanel.appendChild(ph);
    const body = el("div", "pbody dev-body");
    body.appendChild(el("p", "dev-note pr-intro", "What Back returns to, and what holds when nothing else is set. Follow brings pings to your devices until you next type a message to a desk, once the time you choose has passed."));
    labelled(body, "Devices");
    stDevBtns = segment(body, PR_DEVICES, "Standing default: whether pings reach your devices", (v) => setStanding({ devices: v }));
    const ar = el("div", "pr-sheet-seg pr-sub");
    ar.setAttribute("role", "group");
    ar.setAttribute("aria-label", "Standing default: minutes without input before devices come on");
    stAfterRow = ar;
    ar.appendChild(el("span", "pr-sheet-hint", "after"));
    for (const m of PR_AFTER) {
      const b = el("button", "pr-big pr-after", String(m));
      b.type = "button"; b.dataset.value = String(m);
      b.addEventListener("click", () => setStanding({ follow_after: m * 60 }));
      stAfterBtns[m] = b; ar.appendChild(b);
    }
    ar.appendChild(el("span", "pr-sheet-hint", "min"));
    body.appendChild(ar);
    stAfterCustom = minutesField(body, "Standing default: custom minutes", (n) => setStanding({ follow_after: n * 60 }));
    stAfterCustom.hidden = false;
    labelled(body, "Lets through");
    stLetsBtns = segment(body, [], "Standing default: what reaches your devices", (v) => setStanding({ lets_through: v }));
    stNote = el("p", "dev-note"); stNote.hidden = true;
    stNote.setAttribute("role", "status");
    body.appendChild(stNote);
    stPanel.appendChild(body);
    return stPanel;
  }
  function stSay(msg) { stNote.textContent = msg; stNote.hidden = !msg; }


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
    prPanel.hidden = !on; stPanel.hidden = !on; syncSection();
    if (prChip) prChip.hidden = !on;
    if (!on) closeSheet(false);
  }
  function prSay(msg) { prNote.textContent = msg; prNote.hidden = !msg; }
  function sheetSay(msg) { prSheetNote.textContent = msg; prSheetNote.hidden = !msg; }
  function agoText(epochSecs) {
    const m = Math.max(0, Math.round((Date.now() / 1000 - Number(epochSecs)) / 60));
    return m < 1 ? "just now" : m + " min ago";
  }
  // what is sensed, read only (the three things the rule reads; the sentence above already says what it decided)
  function sensingText(v) {
    const parts = [];
    if (v.you && v.you.text) parts.push(v.you.text);
    if (v.desk && v.desk.text) parts.push(v.desk.text);
    parts.push(v.remote && v.remote.at ? "Remote: " + String(v.remote.source || "input").replace(/-/g, " ") + " " + agoText(v.remote.at) : "Remote: none");
    return parts.join(" · ");
  }
  function untilValue() { return prUntil === "for" ? prForDur : prUntil; }

  function paintChip(v) {
    if (!prChip) return;
    const chip = v.chip || {};
    prChipText.textContent = "";
    prChipText.appendChild(el("span", "pr-chip-w", chip.word || "Presence"));
    // the word and its tail are separate nodes so a phone's masthead can keep the word and drop the tail
    if (chip.tail) prChipText.appendChild(el("span", "pr-chip-x", " " + chip.tail));
    prChip.dataset.state = chip.state || "devices";
    prChip.setAttribute("aria-label", "Presence: " + (v.line || "") + " Change it");
  }

  // The "after N min" row and its custom field for one set of buttons: `btns` (5 10 15 30 60 + custom), the current seconds,
  // and whether the custom field is open. A value that is not one of the five marks Custom and shows its minutes.
  function paintAfter(btns, field, secs, open) {
    const m = Math.round(Number(secs) / 60);
    const known = PR_AFTER.indexOf(m) >= 0 && m * 60 === Number(secs);
    for (const k of PR_AFTER) btns[k].setAttribute("aria-pressed", known && k === m ? "true" : "false");
    if (btns.custom) {
      btns.custom.setAttribute("aria-pressed", !known || open ? "true" : "false");
      btns.custom.textContent = !known && !open ? "Custom " + m : "Custom\u2026";
    }
    if (field) {
      field.hidden = !open;
      const inp = field.querySelector("input");
      if (open && !inp.value) inp.value = String(m);
    }
  }

  function paintPresence(v) {
    if (!v) return;
    prCur = v;
    paintChip(v);
    prSheetText.textContent = v.line || "";
    const ov = v.overlay || null, eff = v.effective || v.base || {}, base = v.base || {};
    pressed(prBtns, ov ? (ov.preset || "") : "back");
    prEndLoud.hidden = !(ov && ov.preset === "loud");
    pressed(prDevBtns, eff.devices);
    prAfterRow.hidden = eff.devices !== "follow";
    if (eff.devices !== "follow") prAfterOpen = false;
    paintAfter(prAfterBtns, prAfterCustom, eff.follow_after, prAfterOpen && eff.devices === "follow");
    paintLets(prLetsBtns, v.lets, (x) => setSetting({ lets_through: x }));
    paintLets(stLetsBtns, v.lets, (x) => setStanding({ lets_through: x }));
    pressed(prLetsBtns, eff.lets_through);
    pressed(prUntilBtns, prUntil);
    prForRow.hidden = prUntil !== "for";
    if (prUntil !== "for") prForOpen = false;
    const forMin = /^(\d+)m$/.exec(prForDur);
    const forKnown = PR_DURS.indexOf(prForDur) >= 0;
    for (const k of PR_DURS) prForBtns[k].setAttribute("aria-pressed", forKnown && k === prForDur ? "true" : "false");
    prForBtns.custom.setAttribute("aria-pressed", !forKnown || prForOpen ? "true" : "false");
    prForBtns.custom.textContent = !forKnown && !prForOpen && forMin ? "Custom " + forMin[1] : "Custom…";
    prForCustom.hidden = !(prUntil === "for" && prForOpen);
    prSheetWho.textContent = sensingText(v);
    // the Standing default block: the same rows, bound to the base alone
    pressed(stDevBtns, base.devices);
    stAfterRow.hidden = base.devices !== "follow";
    paintAfter(stAfterBtns, null, base.follow_after, false);
    stAfterCustom.hidden = base.devices !== "follow";
    if (document.activeElement !== stAfterCustom.querySelector("input")) stAfterCustom.querySelector("input").value = String(Math.round(Number(base.follow_after) / 60));
    pressed(stLetsBtns, base.lets_through);
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



  // ONE read in flight, like refresh(): a trigger (the pulse, a tab coming back, a focus, a write's repaint) that finds one running
  // gets its promise back and queues exactly one rerun, so a slow server is never asked twice at once and a response is never
  // dropped for a newer request that has not answered yet.
  function readPresence() {
    if (prRunning) { prRerun = true; return prRunning; }
    prRunning = (async () => {
      try { do { prRerun = false; await readPresenceOnce(); } while (prRerun && !prGone); }
      catch (_) { /* the next pulse tries again */ }
      finally { prRunning = null; prRerun = false; }
    })();
    return prRunning;
  }
  async function readPresenceOnce() {
    if (prGone) return;
    const tok = token();
    if (!tok || tok === prRefused) { prShow(false); return; }
    const startedAt = prGen;   // a write that lands while this read is out makes it stale: the write's own reply paints
    try {
      const res = await api("GET", "/webpush/presence", undefined, READ_TIMEOUT_MS);
      if (startedAt !== prGen) return;
      if (res.status === 200 && res.data && "line" in res.data) paintPresence(res.data);
      else if (res.status === 404) { prShow(false); prGone = true; }
      else if (res.status === 403) { prShow(false); prRefused = tok; }
    } catch (_) { /* the next pulse tries again */ }
  }

  function prDisable(on) {
    for (const group of [prBtns, prDurBtns, prDevBtns, prAfterBtns, prLetsBtns, prUntilBtns, prForBtns, stDevBtns, stAfterBtns, stLetsBtns]) {
      for (const b of Object.values(group)) b.disabled = on;
    }
    prEndLoud.disabled = on;
  }
  // One write at a time. The reply is the server's view after the write, and the sheet, the chip and the Standing default all
  // repaint from it (never from local state). A refusal says why and repaints from a fresh read, so nothing shows a setting that did
  // not land.
  async function writePresence(body, opts) {
    if (prBusy) return false;
    const say = (opts && opts.say) || sheetSay;
    prBusy = true; prGen++; say(""); prDisable(true);
    let ok = false;
    try {
      const res = await api("POST", "/webpush/presence/set", body);
      prGen++;
      if (res.status === 200 && res.data && "line" in res.data) { paintPresence(res.data); ok = true; }
      else say("Could not set your presence (" + ((res.data && res.data.message) || res.status) + ").");
    } catch (e) { say("Could not reach the cockpit."); }
    finally {
      prBusy = false; prDisable(false);
      if (!ok) await readPresence();
    }
    return ok;
  }
  function setPreset(w) {
    const body = { preset: w };
    if (PR_TIMED.indexOf(w) >= 0) body.for = prDur;
    return writePresence(body);
  }
  async function setSetting(partial) {
    const ok = await writePresence(Object.assign({}, partial, { until: untilValue() }));
    if (ok) { prUntil = "switch"; prForOpen = false; paintPresence(prCur); }   // the choice was for that one setting
    return ok;
  }
  function setStanding(partial) { return writePresence(Object.assign({}, partial, { until: "switch" }), { say: stSay }); }


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
    readPresence();
    // The cockpit stores its token a moment after load when the link carried one; look again, then keep a slow pulse.
    setTimeout(() => { refresh(); readPresence(); }, 2500);
    // The Follow line is the server's one derivation, re-read while the page is visible so a second cockpit (a phone's) never
    // keeps a state the laptop's already left: on a short pulse, when the tab comes back, and when the window gains focus.
    const reread = () => { if (!document.hidden && !prGone && !prBusy) readPresence(); };
    document.addEventListener("visibilitychange", reread);
    window.addEventListener("focus", reread);
    window.addEventListener("pageshow", reread);
    // One timer: the Follow line every pulse, the device list every PR_REFRESH_EVERY pulses.
    let ticks = 0;
    const pulse = setInterval(() => {
      if (gone && prGone) { clearInterval(pulse); return; }
      if (document.hidden) return;
      if (!gone && ++ticks % PR_REFRESH_EVERY === 0) refresh();
      if (!prBusy) readPresence();
    }, PR_PULSE_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
