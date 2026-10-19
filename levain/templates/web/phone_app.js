/* The phone app's added views: the state line, the Board (Tray to act on, Loops and Keep to read) and Notifications (the
   pings card and the record). Loaded BEFORE phone.js, so the boot section below settles the page's credential first.
   Everything drawn from the server is set with textContent, never as markup. */
(function () {
  "use strict";
  const TOKEN_KEY = "levain-phone-token", DEVICE_KEY = "levain-device-token";
  const store = {
    get(area, k) { try { return window[area].getItem(k); } catch (e) { return null; } },
    set(area, k, v) { try { window[area].setItem(k, v); return true; } catch (e) { return false; } },
    remove(area, k) { try { window[area].removeItem(k); } catch (e) { /* nothing kept to remove */ } },
  };
  let pairFailed = false, forgets = false;
  // the credentials as this page holds them: memory first, so a browser that refuses storage keeps them for this visit
  let memToken = "", memDevice = "";
  function keepToken(t) { memToken = t; store.set("sessionStorage", TOKEN_KEY, t); }
  function keepDevice(t) { memDevice = t; if (!store.set("localStorage", DEVICE_KEY, t)) forgets = true; }
  const deviceToken = () => memDevice || store.get("localStorage", DEVICE_KEY) || "";
  // one unpaired state, said in the same words by every part of the page (Needs-you's status line included)
  const UNPAIRED = "this phone was unpaired; pair it again from the laptop";
  let unpaired = false;
  function sayUnpaired() { const st = document.getElementById("status"); if (st && st.textContent !== UNPAIRED) st.textContent = UNPAIRED; }
  function forgetDevice(dev) {
    // the laptop revoked this device: forget it, so the page stops offering what it can no longer open
    store.remove("localStorage", DEVICE_KEY); memDevice = "";
    if (token() === dev) { store.remove("sessionStorage", TOKEN_KEY); memToken = ""; }
    if (token()) return;
    unpaired = true;
    sayUnpaired(); renderState(); renderBoard(); renderPings(); renderRecord();
  }

  // ---- BOOT: runs before phone.js reads its token ---------------------------------------------------------------
  // The pairing redeem is a synchronous request on purpose: phone.js runs right after this file and must find the
  // device token already in place. It happens once per install, on the first launch of the installed app.
  (function boot() {
    const pair = new URLSearchParams(location.search).get("pair");
    if (pair) {
      try {
        const x = new XMLHttpRequest();
        x.open("POST", "/webpush/pair/redeem", false);
        x.setRequestHeader("Content-Type", "application/json");
        x.send(JSON.stringify({ code: pair }));
        if (x.status === 200) {
          const t = JSON.parse(x.responseText).token;
          if (typeof t === "string" && t) { keepDevice(t); keepToken(t); }
        } else if (!store.get("localStorage", DEVICE_KEY)) pairFailed = true;
        // 403 with a device token already stored is the normal relaunch: the installed app opens with its spent code
      } catch (e) { if (!store.get("localStorage", DEVICE_KEY)) pairFailed = true; }
      try { history.replaceState(null, "", location.pathname + location.hash); } catch (e) { /* the address stays */ }
    }
    const m = /(?:^#|&)phone_token=([^&]+)/.exec(location.hash);
    if (m) keepToken(decodeURIComponent(m[1]));
    else if (!memToken && !store.get("sessionStorage", TOKEN_KEY)) {
      const dev = deviceToken();
      if (dev) keepToken(dev);
    }
  })();

  // ---- helpers --------------------------------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const token = () => memToken || store.get("sessionStorage", TOKEN_KEY) || "";
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  }
  function api(method, route, body, tok) {
    const headers = { "X-Levain-Phone-Token": tok || token() };
    if (body) headers["Content-Type"] = "application/json";
    return fetch(route, { method: method, cache: "no-store", headers: headers, body: body ? JSON.stringify(body) : undefined })
      .then((res) => res.json().then((j) => ({ ok: res.ok, status: res.status, body: j && typeof j === "object" ? j : {} }),
                                     () => ({ ok: false, status: res.status, body: {} })),
            () => ({ ok: false, status: 0, body: { message: "the phone port could not be reached" } }));
  }
  const why = (r) => r.body.message || ("refused (HTTP " + r.status + ")");
  function ago(iso) {
    const t = Date.parse(iso);
    if (!isFinite(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function when(iso) {
    const d = new Date(iso);
    if (!isFinite(d.getTime())) return "";
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function ymd(offsetDays) {
    const d = new Date(); d.setDate(d.getDate() + offsetDays);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  const b64uToBytes = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                             (c) => c.charCodeAt(0));

  // ---- the service worker, and the credential it keeps for recording an open -------------------------------------
  const swOk = "serviceWorker" in navigator;
  let swReg = null;
  function giveToken() {
    const t = deviceToken() || token();     // the run token dies with the port; an open should still name the phone
    if (!swOk || !t) return;
    navigator.serviceWorker.ready.then((reg) => {
      const w = reg.active || navigator.serviceWorker.controller;
      if (w) w.postMessage({ type: "levain-token", token: t });
    }, () => {});
  }
  if (swOk) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).then((reg) => { swReg = reg; giveToken(); renderPings(); },
                                                                     () => { renderPings(); });
    navigator.serviceWorker.addEventListener("controllerchange", giveToken);
  }

  // ---- tabs -----------------------------------------------------------------------------------------------------
  let view = store.get("sessionStorage", "levain-view") || "needs";
  if (!["needs", "board", "notes"].includes(view)) view = "needs";
  function show(v) {
    view = v; store.set("sessionStorage", "levain-view", v);
    $("view-needs").hidden = v !== "needs"; $("board").hidden = v !== "board"; $("notes").hidden = v !== "notes";
    for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", b.dataset.view === v ? "true" : "false");
    refreshView();
  }
  for (const b of document.querySelectorAll("#tabs button")) b.addEventListener("click", () => show(b.dataset.view));

  // ---- the state line ---------------------------------------------------------------------------------------------
  const SL_MAX = 280;
  let slData = null, slEditing = false, slNote = "", slNoteKind = "";
  function loadState() {
    const used = token();
    return api("GET", "/app/state.json").then((r) => {
      if (r.status === 403 && used && used === deviceToken()) return forgetDevice(used);
      slData = r.ok ? r.body : { error: why(r) };
      renderState();
    });
  }
  function renderState() {
    const box = $("stateline");
    if (slEditing && box.querySelector("textarea")) return;       // a redraw would eat the words being typed
    box.replaceChildren();
    if (unpaired) { box.append(h("div", { class: "meta", text: UNPAIRED })); return; }
    if (!slData) return;
    if (slData.error) { box.append(h("div", { class: "meta", text: "state line unavailable: " + slData.error })); return; }
    const lines = (slData.value && slData.value.lines) || [];
    const line = lines[0];
    const act = (slData.actions || [])[0];
    const canWrite = !!act && act.gesture === "direct";
    const textEl = line && line.text
      ? h("div", { class: "sl-text" }, line.text, h("span", { class: "sl-age", text: [ago(line.at), line.source].filter(Boolean).join(" · ") }))
      : h("div", { class: "sl-text none", text: "no state line. Say where you are and what you are on." });
    const edit = canWrite ? h("button", { type: "button", class: "quiet", id: "sl-open", text: line && line.text ? "edit" : "set",
                                         onclick: () => startEdit(line && line.text || "", act) }) : null;
    box.append(h("div", { class: "sl-row" }, textEl, edit));
    if (slNote) box.append(h("p", { class: "note " + slNoteKind, role: "status", text: slNote }));
    if (forgets) box.append(h("p", { class: "note bad", text: "This browser will not remember the pairing: it works until the page is closed, then open the install link from the laptop again." }));
  }
  function startEdit(text, act) {
    slEditing = true; slNote = "";
    const target = act.target || {};          // the line this edit was opened on: a save never lands on a newer one
    const box = $("stateline");
    const ta = h("textarea", { id: "sl-text", maxlength: String(SL_MAX), "aria-label": "state line" });
    ta.value = text;
    const count = h("div", { class: "sl-count" });
    const upd = () => { count.textContent = ta.value.length + " / " + SL_MAX; };
    ta.addEventListener("input", upd); upd();
    const busy = (on) => { for (const b of box.querySelectorAll("button")) b.disabled = on; };
    const send = (val) => {
      busy(true);
      api("POST", "/app/verb", { verb: "operator_state", panel_id: "state", params: { text: val }, panel_version: target.panel_version })
        .then((r) => {
          slEditing = false;
          if (r.ok) { slNote = val ? "saved" : "cleared"; slNoteKind = "ok"; }
          else if (r.status === 409) { slNote = "it changed since you opened it; here it is now"; slNoteKind = ""; }
          else { slNote = why(r); slNoteKind = "bad"; }
          setTimeout(() => { slNote = ""; renderState(); }, 4000);
          return loadState();
        });
    };
    const row = h("div", { class: "sl-edit" }, ta, count,
      h("div", { class: "actions" },
        h("button", { type: "button", class: "go", id: "sl-save", text: "save", onclick: () => send(ta.value.trim().slice(0, SL_MAX)) }),
        text ? h("button", { type: "button", id: "sl-clear", text: "clear", onclick: () => send("") }) : null,
        h("button", { type: "button", class: "quiet", text: "cancel", onclick: () => { slEditing = false; renderState(); } })));
    box.replaceChildren(h("div", { class: "meta", text: "State line: one freeform line, shown to every agent on its next prompt, and hidden after 12 hours." }), row);
    ta.focus();
  }

  // ---- the board ------------------------------------------------------------------------------------------------------
  const LABELS = { spore_touch: "touch", spore_update: "edit", spore_move: "move", spore_set_disposition: "move",
                   spore_ascend: "promote", spore_promote: "promote", spore_descend: "resolve", spore_surface_at: "snooze",
                   spore_seed: "capture" };
  const label = (verb) => LABELS[verb] || String(verb).replace(/^spore_/, "").replace(/_/g, " ");
  let boardData = null, boardNote = "", boardNoteKind = "", boardNoteTimer = null;
  // {row, version, mode: "resolve"|"snooze", kind}: kept across redraws, so a poll never closes a form on the same row
  let ui = null;
  function noteBoard(text, kind) {
    boardNote = text; boardNoteKind = kind || "";
    clearTimeout(boardNoteTimer);
    if (text) boardNoteTimer = setTimeout(() => { boardNote = ""; renderBoard(); }, 4000);
  }
  function loadBoard() {
    return api("GET", "/app/board.json").then((r) => { boardData = r.ok ? r.body : { error: why(r) }; renderBoard(); });
  }
  function directAct(row, verb) {
    return (row.actions || []).find((a) => a.verb === verb && a.gesture === "direct");
  }
  function fire(act, params, extra) {
    const t = act.target || {};
    const body = Object.assign({ verb: act.verb, panel_id: t.panel_id, row_id: t.row_id, row_version: t.row_version, params: params }, extra || {});
    ui = null; boardNote = "working…"; boardNoteKind = ""; clearTimeout(boardNoteTimer); renderBoard();
    return api("POST", "/app/verb", body).then((r) => {
      if (r.ok) noteBoard(act.verb === "spore_descend" ? "resolved" : "snoozed", "ok");
      else if (r.status === 409) noteBoard("it changed; here it is now");
      else noteBoard(why(r), "bad");
      return loadBoard();
    });
  }
  function rowCard(panel, row) {
    const f = row.facets || {};
    const od = Number(f.overdue_days) || 0;
    const open = ui && ui.row === row.id;
    const card = h("div", { class: "row" + (od > 0 ? " overdue" : "") + (open ? " open" : ""), "data-row": row.id });
    card.append(h("div", { class: "title", text: row.title || row.id }));
    if (row.body && row.body !== row.title) {
      const body = h("div", { class: "body", text: row.body });
      body.addEventListener("click", () => card.classList.toggle("open"));
      card.append(body);
    }
    const chips = [];
    for (const b of row.badges || []) {
      if (b.kind === "age_days") chips.push(h("span", { class: "chip", text: Number(b.value) > 0 ? b.value + " d old" : "new" }));
      else if (b.kind === "due") chips.push(h("span", { class: "chip" + (od > 0 ? " warn" : ""), text: "due " + b.value }));
      else chips.push(h("span", { class: "chip", text: b.kind + " " + b.value }));
    }
    if (od > 0) chips.push(h("span", { class: "chip warn", text: od + " d overdue" }));
    if (chips.length) card.append(h("div", { class: "chips" }, chips));
    const resolve = directAct(row, "spore_descend"), snooze = directAct(row, "spore_surface_at");
    if (resolve || snooze) {
      card.append(h("div", { class: "acts" },
        resolve ? h("button", { type: "button", class: "resolve", text: "Resolve", onclick: () => {
          const kinds = (resolve.options && resolve.options.spore_kind) || ["done"];
          ui = { row: row.id, version: row.version, mode: "resolve", kind: kinds.includes("done") ? "done" : kinds[0] }; renderBoard(); } }) : null,
        snooze ? h("button", { type: "button", class: "snooze", text: "Snooze", onclick: () => { ui = { row: row.id, version: row.version, mode: "snooze" }; renderBoard(); } }) : null));
    }
    if (open && ui.mode === "resolve" && resolve) {
      const kinds = (resolve.options && resolve.options.spore_kind) || ["done"];
      card.append(h("div", { class: "choices" }, kinds.map((k) =>
        h("button", { type: "button", "aria-pressed": ui.kind === k ? "true" : "false", text: k, onclick: () => { ui.kind = k; renderBoard(); } }))));
      card.append(h("div", { class: "confirm-line", text: "Resolve this as “" + ui.kind + "”? It leaves the Tray." }));
      card.append(h("div", { class: "acts" },
        h("button", { type: "button", class: "go confirm", text: "Confirm " + ui.kind, onclick: () => fire(resolve, { spore_kind: ui.kind }, { confirm: true, row_version: ui.version }) }),
        h("button", { type: "button", class: "quiet", text: "cancel", onclick: () => { ui = null; renderBoard(); } })));
    } else if (open && ui.mode === "snooze" && snooze) {
      card.append(h("div", { class: "choices" },
        [["tomorrow", 1], ["in 3 days", 3], ["next week", 7]].map(([name, d]) =>
          h("button", { type: "button", class: "snz", text: name, onclick: () => fire(snooze, { surface_at: ymd(d) }, { row_version: ui.version }) })),
        h("button", { type: "button", class: "quiet", text: "cancel", onclick: () => { ui = null; renderBoard(); } })));
    }
    const others = [];
    for (const a of row.actions || []) {
      const direct = a.gesture === "direct" && (a.verb === "spore_descend" || a.verb === "spore_surface_at");
      const name = label(a.verb);
      if (!direct && !others.includes(name) && name !== "touch") others.push(name);
    }
    if (others.length) card.append(h("div", { class: "laptop", text: others.join(" · ") + ": on the laptop" }));
    return card;
  }
  function rowsOf(panel, rows) {
    if (!rows.length) return [h("div", { class: "card empty", text: panel.empty || "Nothing here." })];
    const groups = (panel.groups || []).filter((g) => g.count > 0);
    if (!groups.length) return rows.map((r) => rowCard(panel, r));
    const out = [];
    for (const g of groups) {
      const mine = rows.filter((r) => r.group === g.id);
      if (!mine.length) continue;
      out.push(h("div", { class: "group-title", text: g.title + " (" + mine.length + ")" }));
      for (const r of mine) out.push(rowCard(panel, r));
    }
    const rest = rows.filter((r) => !groups.some((g) => g.id === r.group));
    return out.concat(rest.map((r) => rowCard(panel, r)));
  }
  function renderBoard() {
    const box = $("board");
    box.replaceChildren();
    if (unpaired) { box.append(h("p", { class: "status", text: UNPAIRED })); return; }
    if (!boardData) { box.append(h("p", { class: "status", text: "reading the board…" })); return; }
    if (boardData.error) { box.append(h("p", { class: "status", text: "board unavailable: " + boardData.error })); return; }
    if (ui) {
      const now = (boardData.panels || []).flatMap((p) => (p.rows || []).concat(p.held || [])).find((r) => r.id === ui.row);
      if (!now || now.version !== ui.version) { ui = null; noteBoard("this row changed"); }
    }
    if (boardNote) box.append(h("p", { class: "note " + boardNoteKind, role: "status", text: boardNote }));
    for (const p of boardData.panels || []) {
      const rows = p.rows || [];
      const held = p.held || [];
      if (p.id === "tray") {
        box.append(h("div", { class: "panel-head" }, h("h2", { text: "Tray" }),
          (p.actions || []).some((a) => a.verb === "spore_seed") ? h("span", { class: "meta", text: "capture: on the laptop" }) : null));
        box.append(...rowsOf(p, rows));
        if (held.length) box.append(h("details", { class: "panel", id: "held" }, h("summary", {}, h("h2", { text: "Snoozed (" + held.length + ")" })),
                                       held.map((r) => rowCard(p, r))));
      } else {
        const d = h("details", { class: "panel", id: "panel-" + p.id });
        if (store.get("sessionStorage", "levain-open-" + p.id) === "1") d.open = true;
        d.addEventListener("toggle", () => store.set("sessionStorage", "levain-open-" + p.id, d.open ? "1" : "0"));
        d.append(h("summary", {}, h("h2", { text: (p.title || p.id) })), ...rowsOf(p, rows));
        box.append(d);
      }
    }
    if (boardData.as_of) box.append(h("p", { class: "meta", text: "as of " + ago(boardData.as_of) }));
  }

  // ---- notifications: the pings card -------------------------------------------------------------------------------
  const ua = navigator.userAgent || "";
  const isIos = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  const standalone = navigator.standalone === true || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
  const pushOk = swOk && "PushManager" in window && "Notification" in window;
  let pingNote = "", pingNoteKind = "", subState = null, installCode = null, installNote = "";
  function currentSub() {
    if (!pushOk) return Promise.resolve(null);
    return navigator.serviceWorker.ready.then((reg) => reg.pushManager.getSubscription()).catch(() => null);
  }
  function refreshSub() { return currentSub().then((s) => { subState = s ? "on" : "off"; renderPings(); }); }
  function turnOn() {
    // the permission prompt must come straight from the tap, so it is the first thing this handler does
    pingNote = "asking the browser…"; pingNoteKind = ""; renderPings();
    Notification.requestPermission().then((perm) => {
      if (perm !== "granted") { pingNote = ""; return renderPings(); }
      return api("GET", "/webpush/key").then((k) => {
        if (!k.ok || !k.body.key) throw new Error(why(k));
        return navigator.serviceWorker.ready.then((reg) => reg.pushManager.getSubscription().then((old) => {
          const make = () => reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(k.body.key) });
          return old ? old.unsubscribe().then(make) : make();
        }));
      }).then((sub) => sendSub(sub).then((r) => {
        // a browser subscription the laptop does not hold would read "on" and never ping
        if (!r.ok) return sub.unsubscribe().catch(() => {}).then(() => { throw new Error(why(r)); });
        giveToken(); pingNote = "pings are on for this phone"; pingNoteKind = "ok"; return refreshSub();
      }));
    }).catch((e) => { pingNote = "could not turn pings on: " + ((e && e.message) || e); pingNoteKind = "bad"; renderPings(); });
  }
  function sendSub(sub) {
    const j = sub.toJSON(), body = { endpoint: j.endpoint, keys: j.keys }, dev = deviceToken();
    const kept = (r) => {
      // on the run token the server pairs this page as a device, which owns the subscription from here on
      if (r.ok && typeof r.body.token === "string" && r.body.token) keepDevice(r.body.token);
      return r;
    };
    return api("POST", "/webpush/subscribe", body, dev).then((r) => {
      if (r.status !== 403 || !dev) return kept(r);
      // the device was revoked: forget it, then once more on the run token, when the page holds one
      forgetDevice(dev);
      return token() ? api("POST", "/webpush/subscribe", body, token()).then(kept) : r;
    });
  }
  function resync() {
    if (!token() && !deviceToken()) return;
    currentSub().then((s) => s ? sendSub(s).then((r) => {
      if (r.ok) return;
      // a 4xx is the laptop refusing to hold it, so the browser drops it too; a 503 or no answer keeps the phone's pings
      if (r.status >= 400 && r.status < 500) return s.unsubscribe().catch(() => {}).then(refreshSub);
      pingNote = "could not confirm with the laptop: " + why(r); pingNoteKind = "bad"; renderPings();
    }) : null);
  }
  function turnOff() {
    currentSub().then((s) => {
      if (!s) return refreshSub();
      const endpoint = s.endpoint;
      return s.unsubscribe().then(() => api("POST", "/webpush/unsubscribe", { endpoint: endpoint })).then(() => {
        pingNote = "pings are off for this phone"; pingNoteKind = ""; return refreshSub();
      });
    }).catch((e) => { pingNote = "could not turn pings off: " + ((e && e.message) || e); pingNoteKind = "bad"; renderPings(); });
  }
  function install() {
    api("POST", "/webpush/pair", { label: "iPhone" }).then((r) => {
      if (r.status === 403) { installNote = "To install, open the link the laptop printed (this page was opened from a saved copy, which cannot make an install code)."; return renderPings(); }
      if (!r.ok || !r.body.code) { installNote = "could not make an install code: " + why(r); return renderPings(); }
      installCode = r.body.code;
      const m = $("manifest");
      if (m) m.setAttribute("href", "/app.webmanifest?pair=" + encodeURIComponent(r.body.code));
      installNote = ""; renderPings();
    });
  }
  const IOS_STEPS = "On iPhone, pings need this page on your Home Screen: tap Share, then Add to Home Screen, then open Levain from there.";
  function renderPings() {
    const box = $("pings");
    if (!box) return;
    box.replaceChildren(h("h2", { text: "Pings on this phone" }));
    if (unpaired) { box.append(h("div", { class: "pstate bad", text: UNPAIRED })); return; }
    if (isIos && !standalone) {
      box.append(h("div", { class: "pstate", text: "Not set up yet" }), h("p", { class: "meta", text: IOS_STEPS }));
      if (installCode) box.append(h("ol", {}, h("li", { text: "Tap Share." }), h("li", { text: "Tap Add to Home Screen." }), h("li", { text: "Open Levain from the Home Screen." })));
      else box.append(h("div", { class: "btnrow" }, h("button", { type: "button", id: "install-btn", text: "Install on this phone", onclick: install })));
      if (installNote) box.append(h("p", { class: "note bad", text: installNote }));
      return;
    }
    if (!pushOk) {
      box.append(h("div", { class: "pstate", text: "This browser cannot receive pings" }),
        h("p", { class: "meta", text: window.isSecureContext ? "Try Chrome on Android, or Safari with Levain on the Home Screen." : "Pings need a secure (https) address; this page was opened over plain http." }));
      return;
    }
    if (Notification.permission === "denied") {
      box.append(h("div", { class: "pstate bad", text: "Blocked by the browser" }),
        h("p", { class: "meta", text: "Allow notifications for this site in the browser's site settings, then come back." }));
      return;
    }
    if (subState === null) { box.append(h("div", { class: "pstate", text: "checking…" })); refreshSub(); return; }
    if (subState === "on") {
      box.append(h("div", { class: "pstate on", text: "Pings are on for this phone" }),
        h("div", { class: "btnrow" }, h("button", { type: "button", class: "quiet", id: "ping-off", text: "Turn off", onclick: turnOff })));
    } else {
      box.append(h("div", { class: "pstate", text: "Pings are off" }),
        h("p", { class: "meta", text: "A ping tells you a gated call or a question is waiting. It carries no buttons; answering still needs your passkey on the page." }),
        h("div", { class: "btnrow" }, h("button", { type: "button", class: "go", id: "ping-on", text: "Turn on pings for this phone", onclick: turnOn })));
    }
    if (pingNote) box.append(h("p", { class: "note " + pingNoteKind, role: "status", text: pingNote }));
  }

  // ---- notifications: the record ---------------------------------------------------------------------------------------
  let recData = null;
  const WAITING = ["pending", "undecided", "open"];
  function loadRecord() {
    return api("GET", "/webpush/record").then((r) => { recData = r.ok ? r.body : { error: why(r) }; renderRecord(); });
  }
  const delivery = (d) => d.status >= 200 && d.status < 300 ? "accepted" : (d.status === 404 || d.status === 410 ? "gone"
    : d.status < 0 ? "no answer in time" : d.status === 0 ? "no answer" : "refused " + d.status);
  function renderRecord() {
    const box = $("record");
    if (!box) return;
    box.replaceChildren();
    const tab = $("tab-notes");
    if (unpaired) { box.append(h("p", { class: "status", text: UNPAIRED })); return; }
    if (!recData) return;
    if (recData.error) { box.append(h("p", { class: "status", text: "the record is unavailable: " + recData.error })); return; }
    const pings = Array.isArray(recData.pings) ? recData.pings : [];
    const waiting = pings.filter((p) => p.outcome && WAITING.includes(p.outcome.state) && !(p.opens || []).length).length;
    tab.replaceChildren("Notifications");
    if (waiting) tab.append(h("span", { class: "badge", text: String(waiting) }));
    box.append(h("div", { class: "panel-head" }, h("h2", { text: "Every ping sent" }), h("span", { class: "meta", text: pings.length + " shown" })));
    if (!pings.length) { box.append(h("div", { class: "card empty", text: "No pings sent yet." })); return; }
    for (const p of pings) {
      const ds = p.deliveries || [], opens = p.opens || [];
      const out = p.outcome;
      const wait = out && WAITING.includes(out.state);
      const where = ds.length ? ds.map((d) => "phone " + d.subscription_id + ": " + delivery(d)).join(" · ") : "sent to no phone";
      box.append(h("div", { class: "row ping" },
        h("div", { class: "head" }, h("span", { class: "kind", text: String(p.kind || "ping").replace(/_/g, " ") }), h("span", { class: "when", text: when(p.at) })),
        h("div", { class: "title", text: p.title || "" }),
        p.body ? h("div", { class: "body", text: p.body }) : null,
        h("div", { class: "chips" }, h("span", { class: "chip", text: where }),
          h("span", { class: "chip", text: opens.length ? "opened " + ago(opens[0].at) : "not opened" })),
        out ? h("div", { class: "outcome " + (wait ? "wait" : (out.state === "allowed" || out.state === "answered" ? "done" : "")), text: out.words }) : null));
    }
  }

  // ---- wiring -----------------------------------------------------------------------------------------------------------
  function refreshView() {
    if (!token()) return;
    if (view === "board") loadBoard();
    if (view === "notes") { loadRecord(); renderPings(); }
  }
  function tick() {
    if (document.hidden || !token()) return;
    loadState();
    if (view !== "notes") loadRecord();       // keeps the tab's count current
    refreshView();
  }
  $("refresh").addEventListener("click", () => { tick(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  if (window.MutationObserver && $("status")) {
    new MutationObserver(() => { if (unpaired) sayUnpaired(); }).observe($("status"), { childList: true, characterData: true, subtree: true });
  }
  if (pairFailed) $("stateline").append(h("p", { class: "note bad", text: "This install link was already used or has expired. Open the link the laptop printed to install." }));
  show(view);
  renderPings();
  if (token()) { loadState(); loadRecord(); } else if (!pairFailed) renderState();
  resync();
  setInterval(tick, 10000);
})();
