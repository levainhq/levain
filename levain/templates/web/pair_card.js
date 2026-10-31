// levain pair_card.js: W1b slice B. The in-app half of pairing a device, on every cockpit page that carries devices_panel.js.
//
//  1. claim(code, ui): the pair card (dashboard_boot.js draws it) hands the typed short code here. It POSTs
//     /cockpit/pair/claim {"short"}, keeps the device key in localStorage "levain_write_token" (the one credential), removes
//     the old phone port's keys from the storage each lives in, and asks an existing worker registration to update, so the
//     new worker replaces the old one and deletes the old database.
//  2. the pings step, on the same card: Turn on pings (permission first, then subscribe, then a test ping to this device).
//  3. the open recorder: the service worker marks a tapped notification in IndexedDB; this page, which holds the key, posts it.
// No token is ever handed to a worker: the worker holds no credential. Drawn with textContent only.
(function () {
  "use strict";
  const WRITE_TOKEN_KEY = "levain_write_token";   // MUST equal dashboard_boot.js WRITE_TOKEN_KEY
  const PAIRED_MARK_KEY = "levain_paired_device"; // MUST equal dashboard_boot.js PAIRED_MARK_KEY
  const TOKEN_HEADER = "X-Levain-Write-Token";
  const OLD_PHONE_KEYS = ["levain-phone-token", "levain-device-token"];   // phone_app.js: sessionStorage and localStorage
  const SW_DB = "levain-sw-2";                    // MUST equal levain/webpush/static/sw.js (slice C): version 1, store "opened"
  const SW_STORE = "opened";
  const OPENED_MESSAGE = "levain-opened";
  const PUSH_OK = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const b64uToBytes = (t) => Uint8Array.from(atob(t.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((t.length + 3) % 4)),
                                             (c) => c.charCodeAt(0));

  function token() { try { return window.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  // the typed code as the server reads it (levain/cockpit/shortcode.py normalize_short): no spaces, hyphens, or invisible
  // format and control characters (Cf, Cc: a zero-width space, a soft hyphen), upper case, the look-alikes O, I and L as 0, 1, 1
  function fold(text) {
    return String(text || "").replace(/[\s‐-―−\-\p{Cf}\p{Cc}]/gu, "").toUpperCase().replace(/O/g, "0").replace(/[IL]/g, "1");
  }
  async function api(method, path, body) {
    const headers = {};
    const t = token();
    if (t) headers[TOKEN_HEADER] = t;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    let data = {};
    try { data = await res.json(); } catch (_) { /* a non-JSON answer is read by its status */ }
    return { status: res.status, data: data };
  }

  // ---- the claim --------------------------------------------------------------------------------------------------
  // The phone port kept its token where phone_app.js put it; this origin now holds the cockpit, so those keys go, in both storages.
  function purgeOldPhoneKeys() {
    for (const name of ["sessionStorage", "localStorage"]) {
      try { for (const k of OLD_PHONE_KEYS) window[name].removeItem(k); } catch (_) { /* a blocked storage holds none */ }
    }
  }
  // A phone-page install has a registration of the old worker: ask the browser to fetch /sw.js again so the new one replaces it.
  // A device with no registration holds no old worker and gets none here.
  async function updateWorker() {
    if (!("serviceWorker" in navigator)) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration("/");
      if (reg) await reg.update();
    } catch (_) { /* the browser's own update check still runs on a later navigation */ }
  }
  function refusal(res) {
    if (res.status === 400) return "Type all eight characters of the code.";
    if (res.status === 403) return (res.data && res.data.message) || "That code was not accepted. If you typed it right, ask for a new one.";
    if (res.status === 503) return "The cockpit is busy. Try again in a moment.";
    return "Could not pair (" + ((res.data && res.data.message) || res.status) + ").";
  }
  // A claim that may have spent its code without this page keeping the key: the POST went out and its answer was lost, or the
  // key came back and storage refused it. A later refusal then says so, as pair.js does after a lost answer.
  let maybeSpent = false;
  const MAYBE_SPENT = "The code may have worked: a device may be paired without this one holding its key. Ask for a new code, and revoke the newest device in Devices.";
  // Returns true when this device is now paired.
  async function claim(typed, ui) {
    const say = (m) => { ui.message.textContent = m; ui.message.hidden = !m; };
    const code = fold(typed);
    if (code.length !== 8) { say("Type all eight characters of the code."); return false; }
    let res;
    try { res = await api("POST", "/cockpit/pair/claim", { short: code }); }
    catch (_) { maybeSpent = true; say("Could not reach the cockpit. If the code reached it, it may already be used."); return false; }
    if (res.status !== 200) { say(res.status === 403 && maybeSpent ? MAYBE_SPENT : refusal(res)); return false; }
    const tok = res.data && res.data.token;
    res = null;
    if (typeof tok !== "string" || !/^[\x21-\x7e]+$/.test(tok)) { say("The cockpit did not hand back a usable key. Ask for a new code."); return false; }
    try { window.localStorage.setItem(WRITE_TOKEN_KEY, tok); }
    catch (_) {
      maybeSpent = true;
      say("This browser will not keep the key (storage is blocked here). The code is spent: ask for a new one, and revoke the newest device in Devices.");
      return false;
    }
    maybeSpent = false;
    try { window.localStorage.setItem(PAIRED_MARK_KEY, "1"); } catch (_) { /* the key is kept; the mark is not */ }
    purgeOldPhoneKeys();
    try { window.dispatchEvent(new Event("levain:write-token-change")); } catch (_) { /* no events */ }
    await updateWorker();
    await pingsStep(ui);
    return true;
  }

  // ---- the pings step ---------------------------------------------------------------------------------------------
  function finish() { try { window.location.reload(); } catch (_) { /* nothing to reload */ } }
  function draw(ui, title, text, buttons) {
    ui.card.replaceChildren();
    const brand = el("div", "pair-brand"); brand.append(el("span", "pair-dot"), "Levain");
    ui.card.append(brand, el("h1", null, title), el("p", null, text));
    const out = el("p", "pair-msg"); out.setAttribute("role", "status"); out.hidden = true;
    const row = el("div", "pair-actions");
    for (const b of buttons) row.appendChild(b);
    ui.card.append(out, row);
    return out;
  }
  function button(text, primary, onclick) {
    const b = el("button", "dev-btn" + (primary ? " dev-primary" : ""), text);
    b.type = "button";
    b.addEventListener("click", () => onclick(b));
    return b;
  }
  async function pushKey() {
    try {
      const res = await api("GET", "/webpush/key");
      return res.status === 200 && typeof res.data.key === "string" ? res.data.key : "";
    } catch (_) { return ""; }
  }
  async function pingsStep(ui) {
    const key = PUSH_OK ? await pushKey() : "";
    if (!key) {
      const why = PUSH_OK ? "This cockpit does not send pings." : "Pings need the Levain app on the Home Screen (or a browser that offers them on this address).";
      draw(ui, "Paired", "This device is paired. " + why, [button("Continue", true, finish)]);
      return;
    }
    let out = null;
    // every way out of the handler (a refusal, a failed subscribe, a throw, the test ping) gives the button back for another tap
    const turnOn = async (btn) => {
      btn.disabled = true; out.hidden = true;
      let sub = null;
      try {
        // the permission prompt must come straight from the tap, so it is the first await in this handler
        const perm = await Notification.requestPermission();
        if (perm !== "granted") { say("Pings stay off: the browser did not allow notifications for this page."); return; }
        await navigator.serviceWorker.register("/sw.js", { scope: "/" });
        const reg = await navigator.serviceWorker.ready;
        const old = await reg.pushManager.getSubscription();
        if (old) await old.unsubscribe().catch(() => {});
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(key) });
        const j = sub.toJSON();
        const subbed = await api("POST", "/webpush/subscribe", { endpoint: j.endpoint, keys: j.keys });
        if (subbed.status !== 200) {
          await sub.unsubscribe().catch(() => {});
          say("Could not turn pings on (" + ((subbed.data && subbed.data.message) || subbed.status) + ").");
          return;
        }
        sub = null;
        const test = await api("POST", "/webpush/test", {});
        say(test.status === 200 ? testWords(test.data) : "Pings are on, but the test ping was refused (" + ((test.data && test.data.message) || test.status) + ").");
        btn.textContent = "Test again";
      } catch (e) {
        if (sub) await sub.unsubscribe().catch(() => {});
        say("Could not turn pings on: " + ((e && e.message) || e));
      } finally { btn.disabled = false; }
    };
    const say = (m) => { out.textContent = m; out.hidden = !m; };
    out = draw(ui, "Paired", "Pings are not on yet. Nothing reaches this phone until you turn them on.",
      [button("Turn on pings", true, turnOn), button("Done", false, finish)]);
  }
  function testWords(data) {
    const d = (data && data.deliveries) || [];
    if (!d.length) return "Pings are on, but the test ping reached no subscription.";
    const ok = d.filter((x) => x && x.status >= 200 && x.status < 300).length;
    return ok ? "A test ping was sent to this phone." : "The test ping was not accepted by the push service (status " + d.map((x) => x && x.status).join(", ") + ").";
  }

  // ---- the open recorder ------------------------------------------------------------------------------------------
  // The worker writes a tapped notification's ping id into levain-sw-2/opened; this page posts each one under its own key.
  // Page and worker open the database with the same version and the same upgrade, so whichever opens first makes the store.
  function openDb() {
    return new Promise((ok, no) => {
      const q = indexedDB.open(SW_DB, 1);
      q.onupgradeneeded = () => { if (!q.result.objectStoreNames.contains(SW_STORE)) q.result.createObjectStore(SW_STORE); };
      q.onsuccess = () => ok(q.result);
      q.onerror = () => no(q.error);
    });
  }
  function keys(db) {
    return new Promise((ok, no) => {
      const g = db.transaction(SW_STORE, "readonly").objectStore(SW_STORE).getAllKeys();
      g.onsuccess = () => ok(g.result);
      g.onerror = () => no(g.error);
    });
  }
  function remove(db, key) {
    return new Promise((ok) => {
      const t = db.transaction(SW_STORE, "readwrite");
      t.objectStore(SW_STORE).delete(key);
      t.oncomplete = () => ok(); t.onerror = () => ok(); t.onabort = () => ok();
    });
  }
  let draining = null, again = false, onlineArmed = false;
  // A failed post waits for the network: one listener, removed by its own firing, and the visibilitychange trigger below.
  function retryWhenOnline() {
    if (onlineArmed) return;
    onlineArmed = true;
    window.addEventListener("online", () => { onlineArmed = false; drainOpened(); }, { once: true });
  }
  // What an answer does to the pending key: a 2xx or a 4xx settles it, except a 429 (asked to wait) and a 401 / 403 (this key
  // is refused, and a re-paired key can still record the open); those, a 3xx and a 5xx keep it. A kept key stops the pass.
  function settles(status) {
    return (status >= 200 && status < 300) || (status >= 400 && status < 500 && status !== 401 && status !== 403 && status !== 429);
  }
  // One drain at a time; a trigger that arrives meanwhile asks for one more pass.
  function drainOpened() {
    if (!token() || !("indexedDB" in window)) return Promise.resolve();
    if (draining) { again = true; return draining; }
    draining = (async () => {
      let db = null;
      try {
        do {
          again = false;
          db = await openDb();
          for (const key of await keys(db)) {
            const ping = Number(key);
            if (!Number.isFinite(ping)) { await remove(db, key); continue; }   // not a ping id: {"ping": null} records nothing
            let res = null;
            try { res = await api("POST", "/webpush/opened", { ping: ping }); }
            catch (_) { retryWhenOnline(); break; }   // offline: keep it until the network or the page comes back
            if (!settles(res.status)) break;
            await remove(db, key);
          }
          db.close(); db = null;
        } while (again);
      } catch (_) { /* no database: nothing was tapped */ } finally { if (db) db.close(); draining = null; }
    })();
    return draining;
  }

  window.levainPairCard = { claim: claim, drainOpened: drainOpened, fold: fold };

  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") drainOpened(); });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data && e.data.type === OPENED_MESSAGE) drainOpened(); });
  }
  drainOpened();
})();
