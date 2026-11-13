// levain desk_tab_view.js — the cockpit's Desk view (levain 33, lane C).
//
// A mailbox and a doorbell, not a conversation: a typed line is filed for the desk, and what the desk files back shows beside
// it. The page is a second full-tab view beside the Room (room_view.js is the reference for the look and for the rule): it
// decides nothing. What is wired, whether a desk holds the lease, whether a line was filed and what the desk answered are the
// server's words over GET /cockpit/desk.json and POST /cockpit/desk/send, drawn as given. Every word that came from the host
// (a holder, a ref, a refusal, an event's text and time) is set with textContent, never as markup, and a character a person
// cannot see is drawn as <U+XXXX> (display only: the words go as typed).
//
// One send per tap: the line in flight greys and says "Filing..."; a double tap sends once. The outbox id is minted once per
// message and re-sent unchanged on a retry, so the server answers the line it already holds instead of filing it twice.
// A typed line never starts a desk: with no lease it is held for the next one, and the page points at the signed Drop tab.
// This page never says what the desk did with a line beyond the server's own words: it cannot observe that.
(function (root) {
  "use strict";

  var DESK_PATH = "/cockpit/desk.json";
  var SEND_PATH = "/cockpit/desk/send";
  var POLL_OPEN_MS = 4000;      // while the view is open
  var POLL_HIDDEN_MS = 12000;   // while the page is hidden
  var KEEP_SENT = 30;
  var MAX_THREADS = 6;          // threads kept and drawn; each one the desk has not answered yet is read on every tick, one request each
  var AMBIGUOUS_WORDS = "It may not have arrived. It is not sent again; say whether it did.";
  var UNWIRED_WORDS = "This cockpit has no desk wired.";
  var LEASE_STATES = { held: 1, none: 1, unknown: 1 };

  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }

  // A character a person cannot see is drawn as <U+XXXX>: the same render escape cockpit_view.js applies to the manifest
  // (tests pin the two expressions equal). A tab, newlines, the zero-width joiner and non-joiner and the two presentation
  // selectors stay, as there. DISPLAY ONLY: the draft box and the send body are never passed through it.
  var HOSTILE = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}\p{Co}\p{Cn}\p{Zs}\p{Default_Ignorable_Code_Point}⠀\u{16fe4}\u{1d159}]/gu;
  var KEPT = { "\n": 1, "\t": 1, " ": 1, "‌": 1, "‍": 1, "︎": 1, "️": 1 };
  function shown(v) {
    return String(v === null || v === undefined ? "" : v).replace(/\r\n/g, "\n").replace(HOSTILE, function (ch) {
      return own(KEPT, ch) ? ch : "<U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0") + ">";
    });
  }

  function str(v) { return typeof v === "string" ? v : (v === null || v === undefined ? "" : String(v)); }

  // "renewed 3 min ago": the lease's age, as the server gave it in seconds
  function ageWords(s) {
    var n = Number(s);
    if (typeof s === "boolean" || s === null || s === undefined || !isFinite(n) || n < 0) return "";
    if (n < 60) return "renewed under a minute ago";
    if (n < 3600) return "renewed " + Math.floor(n / 60) + " min ago";
    if (n < 86400) return "renewed " + Math.floor(n / 3600) + " h ago";
    return "renewed " + Math.floor(n / 86400) + " d ago";
  }

  // The lease line, from the server's lease object alone. A state the server did not name is "unknown", never a guess.
  function leaseModel(lease) {
    var st = lease && typeof lease === "object" && own(LEASE_STATES, lease.state) ? lease.state : "unknown";
    if (st === "held") {
      var who = str(lease.holder), age = ageWords(lease.age_s);
      return { state: "held", words: (who ? who : "A desk") + (age ? ", " + age : ""), drop: false };
    }
    if (st === "none") {
      return { state: "none", words: "No desk holds the lease. A line you send is held for the next desk. A typed line never starts a desk: to start one, use the signed Drop.", drop: true };
    }
    return { state: "unknown", words: "The lease could not be checked; a line you send will queue.", drop: false };
  }

  // One event the desk (or its script) filed back, as the words to draw. The kinds are the port's: took, held, reply.
  function eventModel(e) {
    var by = str(e && e.by), kind = str(e && e.kind), text = str(e && e.text), at = str(e && e.at);
    if (kind === "took") return { cls: "dv-took", who: "", words: "Desk took it" + (at ? " at " + at : "") + (by ? " (" + by + ")" : ""), text: "" };
    if (kind === "held") return { cls: "dv-held", who: "", words: "No desk holds the lease; held" + (at ? " (" + at + ")" : ""), text: "" };
    if (kind === "reply") return { cls: "rv-room", who: by === "desk" ? "Desk" : (by || "Reply"), words: "", text: text, at: at };
    return { cls: "dv-other", who: (by + " " + kind).trim(), words: "", text: text, at: at };
  }

  // The read of GET /cockpit/desk.json as the page's own model. `status` is the HTTP status (0 when nothing answered).
  function deskModel(status, j) {
    if (status === 404) return { kind: "unwired", words: UNWIRED_WORDS };
    if (status !== 200 || !j || typeof j !== "object") {
      return { kind: "error", words: status ? "The desk could not be reached (the server answered " + status + ")." : "The desk could not be reached." };
    }
    if (j.wired !== true) return { kind: "unwired", words: UNWIRED_WORDS };
    var caps = j.caps && typeof j.caps === "object" ? j.caps : {};
    var evs = Array.isArray(j.events) ? j.events.filter(function (e) { return e && typeof e === "object"; }) : [];
    return {
      kind: "wired",
      caps: caps,
      wordsMax: typeof caps.words_max === "number" && caps.words_max > 0 ? caps.words_max : 0,
      lease: leaseModel(j.lease),
      thread: typeof j.thread_id === "string" && j.thread_id ? j.thread_id : null,
      events: evs.map(function (e) { return { by: str(e.by), kind: str(e.kind), text: str(e.text), at: str(e.at) }; }),
      cursor: typeof j.cursor === "string" && j.cursor ? j.cursor : null,
      unreadable: j.readable === false      // "could not be fetched", never "no events yet"
    };
  }

  // Fold one batch of events into the events held. The host returns the events AFTER the cursor it was given: a first batch (no
  // cursor held) is the whole thread; a batch whose cursor is the one already held holds nothing new; a batch with no cursor of
  // its own cannot be followed: it is appended when it came after a held cursor (those events are new) and the cursor goes null,
  // so the next read asks for the whole thread again and replaces what is held.
  function mergeEvents(held, cursor, batch) {
    if (batch.unreadable) return { events: held, cursor: cursor };
    if (batch.cursor === null) return { events: cursor === null ? batch.events : held.concat(batch.events), cursor: null };
    if (cursor === null) return { events: batch.events, cursor: batch.cursor };
    if (batch.cursor === cursor) return { events: held, cursor: cursor };
    return { events: held.concat(batch.events), cursor: batch.cursor };
  }

  // A thread is SETTLED when the events held for it include a reply from the desk.
  function isSettled(events) { return events.some(function (e) { return e.kind === "reply" && e.by === "desk"; }); }

  // The words for one answer to the send (status, parsed body). `kept` = the id stays for a retry; `draft` = the draft is
  // cleared (the line is in the log); `thread` = the thread the answer names.
  function sendOutcome(status, j) {
    var body = j && typeof j === "object" ? j : null;
    if (status === 200 && body && body.outcome === "accepted") {
      return { status: "filed", words: "Filed", ref: str(body.ref), thread: typeof body.thread_id === "string" && body.thread_id ? body.thread_id : null, settle: true, clear: true };
    }
    if (status === 200 && body && body.outcome === "ambiguous") {
      return { status: "ambiguous", words: AMBIGUOUS_WORDS, ref: "", thread: typeof body.thread_id === "string" && body.thread_id ? body.thread_id : null, settle: false, clear: true };
    }
    if (status === 200 && body && body.outcome === "not_delivered") {
      return { status: "refused", words: str(body.reason) || "Not delivered.", ref: "", thread: null, settle: true, clear: false };
    }
    if (status === 200) {
      return { status: "failed", words: "The server's answer was not understood; the line may or may not have been filed. Sending again will not duplicate it.", ref: "", thread: null, settle: false, clear: false };
    }
    if (status === 422 && body && body.error === "words_over_cap") {
      return { status: "refused", words: "Not filed: the line is over the limit" + (typeof body.words_max === "number" ? " of " + body.words_max + " characters" : "") + ". Nothing was cut and nothing was sent.", ref: "", thread: null, settle: true, clear: false };
    }
    if (status === 404) return { status: "refused", words: "Not sent: " + UNWIRED_WORDS.toLowerCase(), ref: "", thread: null, settle: true, clear: false };
    if (status === 403) return { status: "refused", words: "Not sent: the cockpit did not accept this device's credential.", ref: "", thread: null, settle: true, clear: false };
    if (status >= 400 && status < 500) {
      // the server's refusals are {error: <code>, message: <one line>}: the line is the words, the code is only a fallback
      var line = body && typeof body.message === "string" && body.message ? body.message : (body && typeof body.error === "string" && body.error ? body.error : "the server answered " + status + ".");
      return { status: "refused", words: "Not sent: " + line, ref: "", thread: null, settle: true, clear: false };
    }
    return { status: "failed", words: "The server answered " + status + "; the line may or may not have been filed. Sending again will not duplicate it.", ref: "", thread: null, settle: false, clear: false };
  }
  // The note under a filed line. The ref is the host's own first line and often says "Filed" itself: it is drawn once, never as
  // "Filed: Filed ...". Host text: the caller sets it with textContent.
  function filedNote(ref) {
    var r = str(ref);
    if (!r) return "Filed";
    return /^filed\b/i.test(r) ? r : "Filed: " + r;
  }
  // The two marks an ambiguous line offers, drawn by the page alone (no route): the server never re-sends an ambiguous claim by
  // itself, and what the person says here is theirs, never the desk's.
  var ARRIVED_WORDS = "It arrived (your word; the desk has not confirmed it).";
  var NOT_ARRIVED_WORDS = "It did not arrive (your word). Send the same words again to file them as a new line.";
  var UNREACHED_WORDS ="The server could not be reached; the line may or may not have been filed. Sending again will not duplicate it.";

  // how long to wait before the next read, from the page's visibility
  function pollDelay(visibility, openMs, hiddenMs) {
    return visibility === "hidden" ? (hiddenMs == null ? POLL_HIDDEN_MS : hiddenMs) : (openMs == null ? POLL_OPEN_MS : openMs);
  }

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function prose(node, name) {
    [["name", name], ["autocapitalize", "sentences"], ["autocorrect", "on"], ["spellcheck", "true"], ["inputmode", "text"], ["autocomplete", "off"]]
      .forEach(function (kv) { node.setAttribute(kv[0], kv[1]); });
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // One log entry as a row. `you` entries are lines this page sent; `ev` entries are what the host filed back.
  function renderEntry(doc, it, onMark) {
    if (it.k === "you") {
      var row = el(doc, "div", "rv-line rv-you dv-you dv-" + it.status);
      row.appendChild(el(doc, "div", "rv-text", shown(it.text)));
      var note = el(doc, "div", "rv-note dv-status", it.status === "sending" ? "Filing..." :
        (it.status === "filed" ? shown(filedNote(it.ref)) : shown(it.words)));
      note.setAttribute("role", "status");
      row.appendChild(note);
      if (it.status === "ambiguous" && it.id && onMark) {
        var marks = el(doc, "div", "rv-mark dv-marks");
        [["It arrived", true], ["It did not arrive", false]].forEach(function (m) {
          var b = el(doc, "button", m[1] ? "rv-mark-yes dv-mark" : "rv-mark-no dv-mark", m[0]);
          b.setAttribute("type", "button");
          b.addEventListener("click", function () { onMark(it, m[1]); });
          marks.appendChild(b);
        });
        row.appendChild(marks);
      }
      return row;
    }
    var m = eventModel(it);
    var r = el(doc, "div", "rv-line " + m.cls);
    if (m.who) r.appendChild(el(doc, "div", "rv-who", shown(m.who) + (m.at ? " · " + shown(m.at) : "")));
    if (m.words) r.appendChild(el(doc, "div", "rv-note", shown(m.words)));
    if (m.text) r.appendChild(el(doc, "div", "rv-text", shown(m.text)));
    return r;
  }

  // mount(host, {fetch, headers, pollMs, hiddenPollMs, storage}): returns {stop, refresh, model, log}. Reads while mounted (every
  // 4 s open, 12 s hidden) and redraws only when something changed. The dock lives OUTSIDE the redrawn body, so a read never
  // takes the draft.
  function mount(host, opts) {
    opts = opts || {};
    var doc = host.ownerDocument;
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var store = opts.storage === undefined ? (function () { try { return root.sessionStorage; } catch (e) { return null; } })() : opts.storage;
    var stopped = false, timer = 0, gen = 0, reading = false, again = false;
    // `threads` is the ordered list of the threads this page's sends returned (the host files each typed line under a NEW
    // thread): {id, cursor, events} each, oldest first, at most MAX_THREADS. There is no "current thread": a read names the
    // thread it asked for and is applied to that thread alone. `log` holds the lines this page sent.
    var current = null, threads = [], log = [], lastKey = "", note = "";
    var flight = { send: false };

    var wrap = el(doc, "div", "rv dv"), top = el(doc, "div", "rv-top"), bar = el(doc, "div", "dv-state"),
      stage = el(doc, "div", "rv-stage"), body = el(doc, "div", "rv-body"), dock = el(doc, "div", "rv-dock");
    top.appendChild(el(doc, "h2", "rv-title", "Desk"));
    bar.setAttribute("role", "status");
    stage.appendChild(body);
    wrap.appendChild(top); wrap.appendChild(bar); wrap.appendChild(stage); wrap.appendChild(dock); host.appendChild(wrap);

    // the composer, above the phone's keyboard (the visual viewport, as the Room's)
    var vv = root.visualViewport || null, rootEl = doc.documentElement;
    var wasEnd = true;
    function atEnd() { return body.scrollHeight - body.scrollTop - body.clientHeight <= 100; }
    function toEnd() { body.scrollTop = body.scrollHeight; }
    function onBodyScroll() { wasEnd = atEnd(); }
    body.addEventListener("scroll", onBodyScroll);
    function fitViewport() {
      if (stopped || !vv) return;
      rootEl.style.setProperty("--rv-vvh", vv.height + "px");
      rootEl.style.setProperty("--rv-vvt", vv.offsetTop + "px");
      var kb = (root.innerHeight || 0) - vv.height > 120;
      if (doc.body) doc.body.classList.toggle("dv-kb", kb);
      if (kb && root.scrollY) root.scrollTo(0, 0);
      if (wasEnd) toEnd();
    }
    if (vv) { vv.addEventListener("resize", fitViewport); vv.addEventListener("scroll", fitViewport); fitViewport(); }

    var ta = el(doc, "textarea", "rv-input"), composer = el(doc, "div", "rv-compose"), actions = el(doc, "div", "rv-actions"),
      sendB = el(doc, "button", "rv-send", "↑"), sendNote = el(doc, "div", "rv-sendnote");
    ta.setAttribute("rows", "1"); ta.setAttribute("aria-label", "A line for the desk"); ta.setAttribute("placeholder", "A line for the desk");
    sendB.setAttribute("type", "button"); sendB.setAttribute("aria-label", "Send");
    prose(ta, "deskdraft");
    sendNote.setAttribute("role", "status");
    actions.appendChild(sendB); composer.appendChild(ta); composer.appendChild(actions);
    dock.appendChild(sendNote); dock.appendChild(composer);

    // The draft, the outbox id a line was last tried under and the lines sent are kept beside each other: this mount may be
    // replaced (the page left and come back) between a try and its answer, and a replacement that minted a new id would file
    // a line twice. With no store they live in this mount only.
    var mem = {};
    function kept(key) {
      if (store) { try { var raw = store.getItem(key); return raw === null ? null : JSON.parse(raw); } catch (e) { /* fall back */ } }
      return own(mem, key) ? mem[key] : null;
    }
    function keep(key, v) {
      if (stopped) return;
      mem[key] = v;
      try { if (store) { if (v === null) store.removeItem(key); else store.setItem(key, JSON.stringify(v)); } } catch (e) { /* no storage */ }
    }
    function newId() {
      var c = root.crypto;
      return "ob-" + (c && c.randomUUID ? c.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2));
    }
    // the id to try `text` under: the kept one when it was tried for these exact words, else a new one (kept at once)
    function tryId(text) {
      var p = kept("levain.desk.send");
      var id = (p && typeof p.id === "string" && p.text === text) ? p.id : newId();
      keep("levain.desk.send", { id: id, text: text });
      return id;
    }
    function settle(id) { var p = kept("levain.desk.send"); if (p && p.id === id) keep("levain.desk.send", null); }
    function saveLog() {
      var sent = log.filter(function (x) { return x.status !== "sending"; }).slice(-KEEP_SENT);
      keep("levain.desk.sent", sent);
    }
    var priorSent = kept("levain.desk.sent");
    if (Array.isArray(priorSent)) {
      priorSent.forEach(function (x) {
        if (x && x.k === "you" && typeof x.text === "string" && typeof x.status === "string") log.push({ k: "you", id: str(x.id), text: x.text, status: x.status, words: str(x.words), ref: str(x.ref), thread: str(x.thread) });
      });
    }
    try { var d0 = store && store.getItem("levain.desk.draft"); if (d0) ta.value = d0; } catch (e) { /* no storage */ }
    function saveThreads() { keep("levain.desk.threads", threads.map(function (t) { return { id: t.id, cursor: t.cursor, events: t.events }; })); }
    function threadById(id) {
      for (var i = 0; i < threads.length; i++) if (threads[i].id === id) return threads[i];
      return null;
    }
    // the threads kept from an earlier mount; an old single remembered thread becomes a one-entry list
    var priorThreads = kept("levain.desk.threads");
    if (Array.isArray(priorThreads)) {
      priorThreads.forEach(function (x) {
        if (!x || typeof x !== "object" || typeof x.id !== "string" || !x.id || threadById(x.id)) return;
        var evs = Array.isArray(x.events) ? x.events.filter(function (e) { return e && typeof e === "object"; }).map(function (e) { return { by: str(e.by), kind: str(e.kind), text: str(e.text), at: str(e.at) }; }) : [];
        threads.push({ id: x.id, cursor: typeof x.cursor === "string" && x.cursor ? x.cursor : null, events: evs, unreadable: false });
      });
      threads = threads.slice(-MAX_THREADS);
    } else {
      var t0 = kept("levain.desk.thread");
      if (typeof t0 === "string" && t0) { threads.push({ id: t0, cursor: null, events: [], unreadable: false }); saveThreads(); }
    }
    keep("levain.desk.thread", null);
    // a send's thread joins the end of the list (a thread already kept stays where it is); the oldest past the limit go
    function addThread(id) {
      if (!id || threadById(id)) return;
      threads.push({ id: id, cursor: null, events: [], unreadable: false });
      threads = threads.slice(-MAX_THREADS);
      saveThreads();
    }

    function grow() { ta.style.height = "auto"; if (ta.scrollHeight) ta.style.height = (ta.scrollHeight + ta.offsetHeight - ta.clientHeight) + "px"; }
    function canSend() { return !!current && current.kind === "wired"; }
    function paintDock() {
      sendB.disabled = !canSend() || flight.send;
      ta.disabled = !canSend();
      ta.readOnly = flight.send;
      if (ta.value.trim()) sendB.removeAttribute("aria-disabled"); else sendB.setAttribute("aria-disabled", "true");
      sendNote.textContent = note;
    }
    ta.addEventListener("input", function () {
      try { if (store) store.setItem("levain.desk.draft", ta.value); } catch (e) { /* no storage */ }
      grow(); paintDock();
    });
    function coarse() { try { return !!(root.matchMedia && root.matchMedia("(pointer: coarse)").matches); } catch (e) { return false; } }
    ta.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing || ev.keyCode === 229) return;
      if (coarse()) return;
      ev.preventDefault();
      sendB.click();
    });

    // The rows drawn: each line this page sent is followed by the events of ITS thread (took, held, reply), in order, so a first
    // line's answers stay beside it after a second is sent. A kept thread with no line drawn (an older page's thread, or one
    // whose line has aged out of the log) comes last.
    function rows() {
      var out = [], used = {};
      function evs(t) { t.events.forEach(function (e) { out.push({ k: "ev", by: e.by, kind: e.kind, text: e.text, at: e.at }); }); }
      log.forEach(function (it) {
        out.push(it);
        var t = it.thread && !own(used, it.thread) ? threadById(it.thread) : null;
        if (t) { used[t.id] = 1; evs(t); }
      });
      threads.forEach(function (t) { if (!own(used, t.id)) evs(t); });
      return out;
    }
    // A thread is SETTLED once the events held for it include a reply from the desk: it stays drawn and is read no more (a desk
    // that answers a settled thread again is not followed). Only events a readable read delivered are held, so an unreadable read
    // never settles a thread.
    function settled(t) { return isSettled(t.events); }
    function unreadableNow() { return threads.some(function (t) { return t.unreadable; }); }

    function draw() {
      var all = rows();
      var key = JSON.stringify([current, all, unreadableNow(), note]);
      if (key === lastKey) { paintDock(); return; }
      var stick = wasEnd || atEnd();
      clear(bar);
      if (current && current.kind === "wired") {
        bar.className = "dv-state dv-lease-" + current.lease.state;
        bar.appendChild(el(doc, "div", "dv-leasewords", shown(current.lease.words)));
        if (current.lease.drop) {
          var go = el(doc, "button", "dv-droplink", "Open the signed Drop");
          go.setAttribute("type", "button");
          go.addEventListener("click", function () { var j = doc.querySelector('[data-phone-jump="drop"]'); if (j) j.click(); });
          bar.appendChild(go);
        }
        if (unreadableNow()) bar.appendChild(el(doc, "div", "rv-note dv-unreadable", "The desk's answers could not be fetched just now."));
      } else if (current) {
        bar.className = "dv-state dv-state-" + current.kind;
        bar.appendChild(el(doc, "div", "dv-leasewords", shown(current.words)));
      } else { bar.className = "dv-state"; }
      clear(body);
      var stream = el(doc, "div", "rv-stream");
      if (!all.length) stream.appendChild(el(doc, "div", "rv-note", current && current.kind === "wired" ? "Nothing here yet. A line you send is filed for the desk; what it files back shows here." : ""));
      all.forEach(function (it) { stream.appendChild(renderEntry(doc, it, mark)); });
      body.appendChild(stream);
      if (stick) toEnd();
      wasEnd = atEnd();
      lastKey = key;       // only a draw that finished is remembered: one that threw is drawn again by the next
      paintDock();
    }

    // One request, as a promise that can only settle: a synchronous throw (the transport's headers() failing, a blocked store)
    // is a rejection like any other, so no flag is left set by a call that never started. `init` builds the options.
    function ask(url, init) {
      var p;
      try { p = Promise.resolve(f(url, init())); } catch (e) { return Promise.reject(e); }
      return p.then(function (res) { return res.text().then(function (t) { var j = null; try { j = JSON.parse(t); } catch (e) { /* not JSON */ } return { status: res.status, json: j }; }); });
    }
    function post(payload) {
      return ask(SEND_PATH, function () { return { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, headers()), body: JSON.stringify(payload) }; });
    }
    function entryFor(id) {
      for (var i = 0; i < log.length; i++) if (log[i].k === "you" && log[i].id === id) return log[i];
      return null;
    }
    // ONE send per tap: the flag is set before anything is asked of the server, and the button is disabled from it.
    function doSend() {
      var text = ta.value;
      if (!canSend() || !text.trim() || flight.send) return;
      var id = tryId(text);
      var entry = entryFor(id);
      if (!entry) { entry = { k: "you", id: id, text: text, status: "sending", words: "", ref: "", thread: "" }; log.push(entry); }
      entry.status = "sending";
      flight.send = true; note = "";
      try { draw(); toEnd(); } catch (e) { /* the line still goes; the next draw repaints */ }
      // The failure path is the POST's alone: once the server has answered, nothing the page does with the answer (a draw, a
      // save, the follow-up read) can turn a filed line into one marked unreached.
      post({ words: text, outbox_id: id }).then(function (r) {
        flight.send = false;
        var o = sendOutcome(r.status, r.json);
        entry.status = o.status; entry.words = o.words; entry.ref = o.ref;
        if (o.settle) settle(id);
        if (o.clear && !stopped && ta.value === text) { ta.value = ""; try { if (store) store.removeItem("levain.desk.draft"); } catch (e) { /* no storage */ } grow(); }
        if (o.thread) { entry.thread = o.thread; addThread(o.thread); }
        try { saveLog(); draw(); toEnd(); } finally { pull(); }
      }, function () {
        flight.send = false;
        entry.status = "failed"; entry.words = UNREACHED_WORDS;
        saveLog(); draw();
      }).catch(function (e) {
        if (root.console && root.console.error) root.console.error("desk view: " + (e && e.message ? e.message : e));
      }).then(function () { flight.send = false; paintDock(); });
    }
    sendB.addEventListener("click", doSend);

    // The person's word on an ambiguous line. Either way its outbox id is spent: "arrived" settles the line as filed-unconfirmed
    // (nothing is sent), "did not arrive" puts the same words back in an EMPTY draft box, and the next tap sends them under a
    // NEW id (the server answers an old id from the claim it holds and never re-sends it by itself).
    function mark(entry, arrived) {
      if (stopped || entry.status !== "ambiguous") return;
      settle(entry.id);
      if (arrived) {
        entry.status = "arrived"; entry.words = ARRIVED_WORDS;
      } else {
        entry.status = "notarrived"; entry.words = NOT_ARRIVED_WORDS;
        if (!flight.send && !ta.value.trim()) {
          ta.value = entry.text;
          try { if (store) store.setItem("levain.desk.draft", ta.value); } catch (e) { /* no storage */ }
          grow();
        }
      }
      saveLog(); draw();
    }

    function urlFor(id, after) {
      return DESK_PATH + (id ? "?thread=" + encodeURIComponent(id) + (after ? "&after=" + encodeURIComponent(after) : "") : "");
    }
    // One read of one thread (null = the desk alone, when no send has named a thread yet). The thread's id and cursor are taken
    // when the request starts and the answer is applied to THAT thread alone, found again by its id: an answer for a thread that
    // has gone is dropped, and no answer ever changes which threads are kept or read.
    function pullOne(id, my) {
      var entry = id ? threadById(id) : null, after = entry ? entry.cursor : null;
      return ask(urlFor(id, after), function () { return { headers: headers() }; }).catch(function () { return { status: 0, json: null }; }).then(function (r) {
        if (stopped || my !== gen) return;
        // 503 is the desk's poll gate being full (another device's poll, a slow host command): try again at the next interval and
        // keep what is on the page. It is never a state, so the error line is not drawn for it.
        if (r.status === 503) return;
        var t = id ? threadById(id) : null;
        if (id && (!t || t.cursor !== after)) return;
        var m = deskModel(r.status, r.json);
        current = m;
        if (t && m.kind === "wired") {
          t.unreadable = m.unreadable;
          var merged = mergeEvents(t.events, t.cursor, m);
          if (merged.events !== t.events || merged.cursor !== t.cursor) { t.events = merged.events; t.cursor = merged.cursor; saveThreads(); }
        }
        draw();
      });
    }
    // One tick: every kept thread the desk has not answered yet, newest first, one request each, one after another (the route
    // holds few poll slots, and one device's reads must not fill them); with no such thread (none sent yet, or all settled), the
    // desk alone, so the lease line keeps living. A pull asked for while one is running is not lost: ONE follow-up runs when it
    // ends (a send's new thread is read at once, not a tick later).
    function pull() {
      if (stopped) return Promise.resolve();
      if (reading) { again = true; return Promise.resolve(); }
      reading = true; again = false;
      var my = gen;
      var ids = threads.filter(function (t) { return !settled(t); }).reverse().map(function (t) { return t.id; });
      if (!ids.length) ids = [null];
      function next(i) { return i >= ids.length || stopped || my !== gen ? Promise.resolve() : pullOne(ids[i], my).then(function () { return next(i + 1); }); }
      return next(0).catch(function (e) {
        if (root.console && root.console.error) root.console.error("desk view: " + (e && e.message ? e.message : e));
      }).then(function () {
        reading = false;
        if (again && !stopped) { again = false; return pull(); }
      });
    }
    function schedule() {
      if (stopped) return;
      clearTimeout(timer);
      timer = setTimeout(function () { pull().then(schedule); }, pollDelay(doc.visibilityState, opts.pollMs, opts.hiddenPollMs));
    }
    function onVisible() { if (stopped) return; if (doc.visibilityState !== "hidden") pull().then(schedule); else schedule(); }
    doc.addEventListener("visibilitychange", onVisible);

    grow(); paintDock(); draw();
    pull().then(schedule);
    return {
      stop: function () {
        stopped = true; gen++; clearTimeout(timer);
        doc.removeEventListener("visibilitychange", onVisible);
        body.removeEventListener("scroll", onBodyScroll);
        if (vv) {
          vv.removeEventListener("resize", fitViewport); vv.removeEventListener("scroll", fitViewport);
          rootEl.style.removeProperty("--rv-vvh"); rootEl.style.removeProperty("--rv-vvt"); if (doc.body) doc.body.classList.remove("dv-kb");
        }
      },
      refresh: function () { return pull(); },
      model: function () { return current; },
      log: function () { return rows(); }
    };
  }

  var api = {
    deskModel: deskModel, leaseModel: leaseModel, eventModel: eventModel, mergeEvents: mergeEvents, isSettled: isSettled, sendOutcome: sendOutcome, filedNote: filedNote,
    pollDelay: pollDelay, shown: shown, ageWords: ageWords, mount: mount,
    POLL_OPEN_MS: POLL_OPEN_MS, POLL_HIDDEN_MS: POLL_HIDDEN_MS, AMBIGUOUS_WORDS: AMBIGUOUS_WORDS, ARRIVED_WORDS: ARRIVED_WORDS, NOT_ARRIVED_WORDS: NOT_ARRIVED_WORDS,
    HOSTILE_SOURCE: HOSTILE.source
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainDeskView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
