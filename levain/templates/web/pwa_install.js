// levain pwa_install.js: the install coach (PWA package, item 4). In a browser TAB on a phone where an install is possible (iOS
// and iPadOS Safari, Android Chrome) it shows how to install; in the installed app (standalone) it shows nothing. Pairing and
// pings are offered only in the installed app: pair_card.js, dashboard_boot.js and devices_panel.js ask gated() and draw
// note() in place of those controls. A desktop browser is never gated (keeps the cockpit as it was). Safari on a Mac CAN install
// (File > Add to Dock, Safari 17+): it gets the coach banner (coachKind) but is not gated, so pairing there stays as it was.
// Drawn with textContent and createElementNS only. No origin is named here.
(function () {
  "use strict";
  const DISMISS_KEY = "levain_install_coach_dismissed";   // sessionStorage: a dismissed banner stays away for this tab only
  const IOS_SAY = "Install Levain: tap Share, then Add to Home Screen.";
  const MAC_SAY = "Install Levain: in Safari choose File, then Add to Dock (macOS Sonoma or later).";
  const GATE_SAY = "Pairing and pings are offered in the installed Levain app. ";
  let deferred = null, installedNow = false, banner = null;
  const notes = new Set();   // the gate notes drawn into pairing and pings; re-rendered whenever the install state moves

  function standalone() {
    try { if (navigator.standalone === true) return true; } catch (_) { /* no such property */ }
    try { return !!(window.matchMedia && window.matchMedia("(display-mode: standalone)").matches); } catch (_) { return false; }
  }
  function ua() { return String(navigator.userAgent || ""); }
  function ios() {
    const apple = /iPhone|iPod|iPad/.test(ua()) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);   // iPadOS desktop mode reports a Mac
    return apple && !/CriOS|FxiOS|EdgiOS|OPiOS|GSA\//.test(ua());   // Safari only: the other iOS browsers keep the old controls
  }
  function androidChrome() {
    const u = ua();
    // an install is offered only on a secure origin: on plain http Chrome never arms the prompt, so there the old controls stay
    return window.isSecureContext !== false && /Android/.test(u) && /Chrome\//.test(u) && !/EdgA|OPR\/|SamsungBrowser|Firefox|UCBrowser/.test(u);
  }
  function kind() { return ios() ? "ios" : androidChrome() ? "android" : ""; }
  // Safari on a Mac.
  // Add to Dock arrived in Safari 17; the user agent freezes the OS version, so macOS Sonoma is named in the words, not tested.
  function macSafari() {
    const u = ua(), v = /Version\/(\d+)/.exec(u);
    if (!/Macintosh/.test(u) || !/Safari\//.test(u) || !v || Number(v[1]) < 17) return false;
    if (/Chrome|Chromium|CriOS|FxiOS|Firefox|Edg|OPR\/|Brave|Vivaldi/.test(u)) return false;
    return true;   // an iPad in desktop mode (a Mac user agent with touch) never reaches here: ios() claims it first
  }
  // who gets the banner: gated() phones plus Safari on a Mac
  function coachKind() { return kind() || (macSafari() ? "mac" : ""); }
  // the one question the other pages ask: is this a phone tab where the install must come first
  function gated() { return !!kind() && !standalone(); }   // appinstalled does not make THIS tab the app: only standalone lifts the gate
  function words() {
    if (installedNow) return "Levain is installed: open it from your Home Screen.";
    return kind() === "ios" ? IOS_SAY : "Install Levain from the browser menu (Install app), then open it from your Home Screen.";
  }

  function shareGlyph() {
    const ns = "http://www.w3.org/2000/svg";
    const s = document.createElementNS(ns, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", "18"); s.setAttribute("height", "18");
    s.setAttribute("aria-hidden", "true"); s.setAttribute("class", "pwa-share");
    s.style.verticalAlign = "-3px";
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", "M12 3v12M8 7l4-4 4 4M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1");
    p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.appendChild(p);
    return s;
  }
  function installBtn() {
    const b = document.createElement("button");
    b.type = "button"; b.className = "dev-btn dev-primary pwa-install-btn"; b.textContent = "Install Levain";
    b.addEventListener("click", async () => {
      const d = deferred; if (!d) return;
      deferred = null; b.disabled = true;
      // one prompt per event: whatever the answer, every Install button is re-drawn from the new state (the menu words after a dismissal)
      try { await d.prompt(); await d.userChoice; } catch (_) { /* the browser refused the prompt */ }
      refresh();
    });
    return b;
  }
  // the gate's note, in place of a pairing or pings control: the words, the glyph on iOS, the Install button on Android when armed
  function renderNote(box) {
    box.replaceChildren();
    const t = document.createElement("span");
    t.textContent = GATE_SAY + (kind() === "ios" && !installedNow ? "Tap Share" : words());
    box.appendChild(t);
    if (installedNow) return;
    if (kind() === "ios") {
      box.append(" ", shareGlyph(), document.createTextNode(", then Add to Home Screen."));
    } else if (deferred) {
      box.append(" ", installBtn());
    }
  }
  function note() {
    for (const n of [...notes]) if (!n.isConnected) notes.delete(n);   // a card redrawn on every refusal must not pile up detached nodes
    const box = document.createElement("div");
    box.className = "pwa-note";
    renderNote(box);
    notes.add(box);
    return box;
  }

  let dismissedNow = false;   // "Not now" held in memory too: where sessionStorage throws, the dismissal still lasts this load
  function dismissed() { if (dismissedNow) return true; try { return window.sessionStorage.getItem(DISMISS_KEY) === "1"; } catch (_) { return false; } }
  function drawBanner() {
    const b = document.createElement("div");
    b.id = "pwa-coach"; b.className = "pwa-coach"; b.setAttribute("role", "region"); b.setAttribute("aria-label", "Install Levain");
    b.style.cssText = "position:fixed;left:12px;right:12px;bottom:12px;z-index:60;padding:12px 14px;border-radius:14px;" +
      "background:var(--panel,#1b1d24);color:var(--text,#e8e8ee);border:1px solid var(--line,#3a3d4a);box-shadow:0 6px 24px rgba(0,0,0,.4);" +
      "display:flex;gap:10px;align-items:center;font:15px/1.35 system-ui,sans-serif";
    const msg = document.createElement("div"); msg.style.flex = "1";
    if (kind() === "ios") {
      msg.append("Install Levain: tap Share ", shareGlyph(), ", then Add to Home Screen.");
    } else if (coachKind() === "mac") {
      msg.textContent = MAC_SAY;
    } else {
      msg.textContent = "Install Levain on this phone.";
    }
    b.appendChild(msg);
    if (kind() === "android") b.appendChild(installBtn());
    const x = document.createElement("button");
    x.type = "button"; x.className = "dev-btn"; x.textContent = "Not now"; x.setAttribute("aria-label", "Hide the install hint");
    x.addEventListener("click", () => { dismissedNow = true; try { window.sessionStorage.setItem(DISMISS_KEY, "1"); } catch (_) { /* kept for this load only */ } refresh(); });
    b.appendChild(x);
    document.body.appendChild(b);
    return b;
  }
  // the banner shows exactly when a coach is due (a phone tab, or Safari on a Mac) and not standalone, a dismissal allows, and
  // (Android) the browser has armed the prompt
  function refresh() {
    for (const n of [...notes]) { if (n.isConnected) renderNote(n); else notes.delete(n); }
    const ck = coachKind();
    const want = !!ck && !standalone() && !installedNow && !dismissed() && (ck === "ios" || ck === "mac" || !!deferred);
    if (!want) { if (banner) { banner.remove(); banner = null; } return; }
    if (!banner || !banner.isConnected) { if (banner) banner.remove(); banner = drawBanner(); }
  }

  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferred = e; refresh(); });
  window.addEventListener("appinstalled", () => { installedNow = true; deferred = null; refresh(); });
  window.levainPwa = { standalone: standalone, gated: gated, kind: kind, coachKind: coachKind, note: note, words: words, refresh: refresh };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", refresh); else refresh();
})();
