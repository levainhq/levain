// levain devices_panel.js — the cockpit's Devices panel (per-device pairing, PAIR_DESIGN.md REV 1-6).
//
// Lists the paired devices (GET /cockpit/pair/devices.json), mints a one-time pairing link (POST /cockpit/pair/offer {label}) and
// revokes a device and its family (POST /cockpit/pair/revoke {id}). It rides the page's own credential: the token the cockpit
// keeps in localStorage "levain_write_token" (dashboard_boot.js WRITE_TOKEN_KEY). Everything is drawn with textContent: this
// file shares a page with that token, so it adds no HTML sink. A server with no writable cockpit answers 404 and the panel never
// appears; a page with no usable credential shows nothing either.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";
  const H = "X-Levain-Write-Token";
  let section = null, list = null, offerBox = null, note = null, labelIn = null;
  // L3 r1 F2: only the NEWEST refresh paints (an older response that lands late is dropped), and a pulse never starts a
  // second read while one is in flight. F7: a 404 (no pairing on this server) is stable for the server's life: stop asking.
  let gen = 0, inFlight = false, gone = false;

  function token() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  async function api(method, path, body) {
    const headers = {};
    const t = token();
    if (t) headers[H] = t;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    let data = {};
    try { data = await res.json(); } catch (_) { /* a non-JSON answer is a refusal below */ }
    return { status: res.status, data: data };
  }
  function when(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function build() {
    section = el("section", "ck-zone dev-zone");
    section.dataset.zone = "devices";
    section.dataset.modes = "board";
    section.hidden = true;
    const head = el("div", "zone-head");
    head.appendChild(el("div", "zone-title", "Devices"));
    head.appendChild(el("span", "zone-gloss", "who holds a key to this cockpit"));
    section.appendChild(head);
    const grid = el("div", "ck-zgrid dev-grid");
    const panel = el("div", "panel k-list dev-panel");
    panel.dataset.modes = "board";   // dashboard.css hides a zone whose panel names no tab for the current mode
    const ph = el("div", "phead");
    ph.appendChild(el("h2", null, "Paired devices"));
    panel.appendChild(ph);
    const body = el("div", "pbody dev-body");
    list = el("div", "dev-list");
    body.appendChild(list);

    const form = el("form", "dev-offer");
    labelIn = el("input", "dev-label");
    labelIn.type = "text"; labelIn.maxLength = 40; labelIn.placeholder = "name the new device";
    labelIn.setAttribute("aria-label", "Name for the new device");
    labelIn.autocomplete = "off";
    const go = el("button", "dev-btn dev-primary", "Pair a device");
    go.type = "submit";
    form.appendChild(labelIn); form.appendChild(go);
    form.addEventListener("submit", (e) => { e.preventDefault(); offer(go); });
    body.appendChild(form);
    offerBox = el("div", "dev-link"); offerBox.hidden = true;
    body.appendChild(offerBox);
    note = el("p", "dev-note"); note.hidden = true;
    body.appendChild(note);
    panel.appendChild(body);
    grid.appendChild(panel);
    section.appendChild(grid);
    const main = document.querySelector("main.frame");
    if (main) main.appendChild(section); else document.body.appendChild(section);
  }

  function say(msg) { note.textContent = msg; note.hidden = !msg; }

  function row(d) {
    const r = el("div", "dev-row" + (d.revoked_at ? " dev-revoked" : ""));
    const main = el("div", "dev-main");
    main.appendChild(el("div", "dev-name", d.label || d.id));
    main.appendChild(el("div", "dev-meta", "paired " + when(d.created) + " · last seen " + when(d.last_seen) + (d.paired_by ? " · by " + d.paired_by : "")));
    r.appendChild(main);
    if (d.revoked_at) {
      r.appendChild(el("span", "pill dev-pill", "revoked " + when(d.revoked_at)));
    } else {
      const b = el("button", "dev-btn", "Revoke");
      b.type = "button";
      b.setAttribute("aria-label", "Revoke " + (d.label || d.id));
      b.addEventListener("click", () => revoke(d, b));
      r.appendChild(b);
    }
    return r;
  }

  async function refresh(pulse) {
    if (gone) return;
    if (pulse && inFlight) return;            // a stalled read: the pulse waits for it, requests never pile up
    const my = ++gen;
    if (!token()) { section.hidden = true; return; }
    let res;
    inFlight = true;
    try { res = await api("GET", "/cockpit/pair/devices.json"); }
    catch (_) { if (my === gen && !section.hidden) say("Couldn't refresh the device list."); return; }
    finally { if (my === gen) inFlight = false; }
    if (my !== gen) return;                   // a newer refresh started: its answer is the one that paints
    if (res.status !== 200 || !Array.isArray(res.data.devices)) {
      // 404: a read-only or ledger-less server (no pairing); 403: no usable credential. Either way nothing to show.
      if (res.status === 404 || res.status === 403) section.hidden = true;
      else say("Couldn't refresh the device list.");
      if (res.status === 404) gone = true;
      return;
    }
    if (note.textContent === "Couldn't refresh the device list.") say("");
    section.hidden = false;
    list.replaceChildren();
    const devs = res.data.devices.slice().sort((a, b) => (a.revoked_at ? 1 : 0) - (b.revoked_at ? 1 : 0) || String(b.created).localeCompare(String(a.created)));
    if (!devs.length) list.appendChild(el("p", "dev-none", "No device is paired yet. Pair one below: it gets its own key, and you can revoke it here."));
    for (const d of devs) list.appendChild(row(d));
    if (!labelIn.value) labelIn.placeholder = "name the new device, e.g. iPad";
  }

  async function offer(btn) {
    btn.disabled = true; say("");
    try {
      const label = (labelIn.value || "").trim();
      const res = await api("POST", "/cockpit/pair/offer", label ? { label: label } : {});
      if (res.status !== 200 || !res.data.pair_path) {
        say(res.status === 503 ? "The cockpit is busy. Try again in a moment." : "Could not make a pairing link (" + (res.data.message || res.status) + ").");
        return;
      }
      const url = location.origin + res.data.pair_path;
      showLink(url, res.data.expires_in_s || 600);
      labelIn.value = "";
      refresh();
    } catch (_) {
      say("Could not reach the cockpit.");
    } finally { btn.disabled = false; }
  }

  function showLink(url, secs) {
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
    let msg = "";
    try {
      const res = await api("POST", "/cockpit/pair/revoke", { id: d.id });
      const n = (res.data.revoked || []).length;
      if (res.status !== 200) msg = "Could not revoke " + (d.label || d.id) + " (" + (res.data.message || res.status) + ").";
      else if (n > 1) msg = "Revoked " + (d.label || d.id) + " and " + (n - 1) + " device(s) it had paired.";
      else msg = "Revoked " + (d.label || d.id) + ".";
    } catch (_) { msg = "Could not reach the cockpit."; }
    finally { if (btn.isConnected) btn.disabled = false; }   // F3: a failed revoke whose refresh also fails keeps a live button
    await refresh();
    say(msg);                                  // after the refresh, so its own note does not wipe the revoke's answer
  }

  function start() {
    build();
    refresh();
    // The cockpit stores its token a moment after load when the link carried one; look again, then keep a slow pulse.
    setTimeout(refresh, 2500);
    const pulse = setInterval(() => { if (gone) clearInterval(pulse); else if (!document.hidden) refresh(true); }, 30000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
