// levain room_view.js — the cockpit's one Claude room (RM-F).
//
// A conversation with the room over GET /cockpit/room.json and POST /cockpit/room/*: a draft box with Send, Drop and
// Stop, the room's cards (a drop it proposed, which only YOUR tap commits), the reconcile of an ambiguous line, and a
// dictation control where the browser has one. The page decides nothing: what may be sent, whether a line arrived and
// whether a card became a drop are the server's words, drawn as given. Every word that came from the record is set with
// textContent, never as markup. The stream is drawn in the order the server sent (gseq); nothing here sorts it.
//
// A card's words are bytes you sent: the field holds your quoted message (editable), and the room's own summary is a
// separate, labelled note that is information and never your words. Dictation runs only while this page is in the
// foreground: it fills the draft, you read it, and Send or Drop works exactly as typing. No audio reaches Levain.
(function (root) {
  "use strict";

  var ROOM_PATH = "/cockpit/room.json";
  var POST_PREFIX = "/cockpit/room/";
  var SEND_WORDS = {
    accepted: "Sent.",
    ambiguous: "Sent, but it may not have arrived. It is not sent again; you can say whether it did."
  };
  var STOP_WORDS = {
    confirmed: "Stopped.",
    no_such_request: "Nothing was running to stop.",
    ambiguous: "The stop was sent; it was not confirmed.",
    rejected: "The stop was refused."
  };
  var MARK_WORDS = { accepted: "Marked: it arrived.", not_delivered: "Marked: it did not arrive." };
  var COMPOSER_WORDS = {
    none: "The room starts when you send the first line.",
    ended: "The room starts again when you send a line.",
    no_session: "The room starts again when you send a line.",
    not_live: "The room is not available right now.",
    checking: "The room is getting ready.",
    writable: ""
  };
  var SEND_STATES = { none: 1, ended: 1, no_session: 1, writable: 1 };
  var STOP_STATES = { writable: 1, checking: 1, not_live: 1 };
  var COLLAPSED = { vendor_tool_call: 1, vendor_tool_result: 1 };
  var HIDDEN = { vendor_echo: 1, vendor_turn_started: 1, vendor_turn_ended: 1, vendor_user_item: 1 };

  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function peek(text) {
    var first = String(text || "").split("\n")[0].trim();
    return first.length > 120 ? first.slice(0, 120) + "..." : first;
  }

  function lineModel(it) {
    var you = it.kind === "message" && it.source === "operator";
    var room = it.kind === "vendor_agent_text";
    var text = it.body == null ? "" : String(it.body);
    if (it.kind === "vendor_approval_request") text = "The room asked for something it cannot have; nothing was granted.";
    if (it.kind === "vendor_approval_resolved") text = "";
    return {
      gseq: it.gseq, ref: it.ref == null ? "" : String(it.ref), at: it.at || "",
      who: you ? "You" : (room ? "The room" : ""), cls: you ? "rv-you" : (room ? "rv-room" : (it.kind === "vendor_error" ? "rv-error" : "")),
      kind: String(it.kind), text: text, collapsible: own(COLLAPSED, it.kind), hidden: own(HIDDEN, it.kind),
      ambiguous: it.kind === "message" && it.status === "ambiguous",
      truncated: it.truncated === true, tool: it.tool ? { open: it.tool.open === true } : null
    };
  }

  function roomModel(body) {
    var c = body.composer || null, st = c && own(COMPOSER_WORDS, c.state) ? c.state : "none";
    var thread = typeof body.room_thread === "string" ? body.room_thread : null;
    return {
      thread: thread, open: thread !== null,
      canSend: thread !== null && own(SEND_STATES, st), canStop: thread !== null && own(STOP_STATES, st),
      state: st, words: thread === null ? "No room is open." : (c && c.why && st !== "writable" ? COMPOSER_WORDS[st] + " (" + c.why + ")" : COMPOSER_WORDS[st]),
      model: typeof body.model === "string" && body.model ? "Model in use: " + body.model : "",
      gap: body.gap === true,
      stream: (body.stream || []).map(lineModel).filter(function (l) { return !l.hidden; }),
      proposals: (body.proposals || []).map(function (p) {
        return { id: String(p.proposal_id), state: String(p.state), words: String(p.words == null ? "" : p.words),
                 summary: String(p.summary == null ? "" : p.summary), about: p.about == null ? "" : String(p.about) };
      })
    };
  }

  function sendWords(status, j) {
    if (status === 200 && j && j.receipt && own(SEND_WORDS, j.receipt.state)) return SEND_WORDS[j.receipt.state];
    if (status >= 500) return "The server answered " + status + "; the line may or may not have been sent. Sending again will not duplicate it.";
    if (status === 202) return (j && j.message) || "The line was sent; it is not settled.";
    if (j && j.error === "room_rotated") return "This conversation was replaced; reading the new one. Your draft is kept.";
    if (j && j.message) return "Not sent: " + j.message;
    return "Not sent: the server answered " + status + ".";
  }

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // hooks: onMark(ref, delivered, note, done), onCommit(card, words, note, buttons), onDismiss(card, note, buttons); ui.open keyed by gseq
  function renderLine(doc, l, hooks, ui) {
    var row = el(doc, "div", "rv-line " + l.cls);
    var fold = l.collapsible && !!ui, shown = fold && ui.open[l.gseq] === true;
    var head = el(doc, fold ? "button" : "div", "rv-who");
    if (fold) { head.setAttribute("type", "button"); head.setAttribute("aria-expanded", shown ? "true" : "false"); }
    head.textContent = l.who || (l.tool ? (l.tool.open ? "A tool is running" : "A tool ran") : (l.kind === "vendor_error" ? "Error" : l.kind.replace(/_/g, " ")));
    if (fold && !shown && l.text) head.textContent += " — " + peek(l.text);
    row.appendChild(head);
    var pre = l.text ? el(doc, "div", "rv-text", l.text) : null;
    if (pre) {
      if (fold) pre.hidden = !shown;
      row.appendChild(pre);
    }
    if (fold && pre) {
      head.addEventListener("click", function () {
        var now = pre.hidden;
        pre.hidden = !now; head.setAttribute("aria-expanded", now ? "true" : "false");
        if (now) ui.open[l.gseq] = true; else delete ui.open[l.gseq];
        head.textContent = l.who || (l.tool ? (l.tool.open ? "A tool is running" : "A tool ran") : l.kind.replace(/_/g, " "));
        if (!now && l.text) head.textContent += " — " + peek(l.text);
      });
    }
    if (l.truncated) row.appendChild(el(doc, "div", "rv-note", "Cut short; the vendor holds the rest."));
    if (l.ambiguous && l.ref && hooks && hooks.onMark) {
      var box = el(doc, "div", "rv-mark"), yes = el(doc, "button", "rv-mark-yes", "It arrived"),
        no = el(doc, "button", "rv-mark-no", "It did not arrive"), note = el(doc, "span", "rv-marknote");
      yes.setAttribute("type", "button"); no.setAttribute("type", "button"); note.setAttribute("role", "status");
      box.appendChild(el(doc, "span", "rv-marklabel", "This line may not have arrived. You decide:"));
      [[yes, true], [no, false]].forEach(function (p) {
        p[0].addEventListener("click", function () { yes.disabled = true; no.disabled = true; hooks.onMark(l.ref, p[1], note, function () { yes.disabled = false; no.disabled = false; }); });
      });
      box.appendChild(yes); box.appendChild(no); box.appendChild(note);
      row.appendChild(box);
    }
    return row;
  }

  // a proposal card: the field holds the words (yours, editable); the summary is the room's note, labelled information
  function renderCard(doc, c, hooks, drafts) {
    var card = el(doc, "div", "rv-card rv-card-" + c.state.replace(/[^a-z]/g, ""));
    card.setAttribute("data-proposal", c.id);
    if (c.state === "committed") { card.appendChild(el(doc, "div", "rv-note", "Dropped: these words went to the desk.")); card.appendChild(el(doc, "div", "rv-text", c.words)); return card; }
    if (c.state === "dismissed") { card.appendChild(el(doc, "div", "rv-note", "Not dropped.")); return card; }
    card.appendChild(el(doc, "div", "rv-note", c.state === "expired" ? "This card is from an earlier conversation and cannot be sent." : "The room can drop this to the desk. Nothing is sent until you tap."));
    var ta = el(doc, "textarea", "rv-cardwords");
    ta.setAttribute("rows", "3"); ta.setAttribute("aria-label", "The words to drop");
    ta.value = own(drafts, c.id) ? drafts[c.id] : c.words;     // an edit survives a redraw
    ta.addEventListener("input", function () { drafts[c.id] = ta.value; });
    card.appendChild(ta);
    card.appendChild(el(doc, "div", "rv-summary-label rv-note", "The room's summary (information, not your words)"));
    card.appendChild(el(doc, "div", "rv-summary", c.summary));
    var note = el(doc, "div", "rv-sendnote"), go = el(doc, "button", "rv-commit", "Drop these words"), no = el(doc, "button", "rv-dismiss", "Not now");
    go.setAttribute("type", "button"); no.setAttribute("type", "button"); note.setAttribute("role", "status");
    if (c.state === "expired") go.disabled = true;
    go.addEventListener("click", function () { hooks.onCommit(c, ta.value, note, [go, no]); });
    no.addEventListener("click", function () { hooks.onDismiss(c, note, [go, no]); });
    card.appendChild(go); card.appendChild(no); card.appendChild(note);
    return card;
  }

  function renderRoom(doc, host, m, hooks, ui, drafts) {
    clear(host);
    if (m.model) host.appendChild(el(doc, "div", "rv-head", m.model));
    if (m.gap) host.appendChild(el(doc, "div", "rv-gap", "Some lines may be missing from this conversation."));
    if (!m.stream.length) host.appendChild(el(doc, "div", "rv-note", m.open ? "Nothing has been said yet." : m.words));
    m.stream.forEach(function (l) { host.appendChild(renderLine(doc, l, hooks, ui)); });
    m.proposals.forEach(function (c) { host.appendChild(renderCard(doc, c, hooks, drafts)); });
  }

  // mount(host, {fetch, headers, pollMs, speech, storage}): returns {stop, refresh, model}. Polls while mounted and redraws
  // only when the body changed. The dock lives OUTSIDE the redrawn body, so a poll never takes the draft.
  function mount(host, opts) {
    opts = opts || {};
    var doc = host.ownerDocument;
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var pollMs = opts.pollMs == null ? 2000 : opts.pollMs;
    var Speech = opts.speech === undefined ? (root.SpeechRecognition || root.webkitSpeechRecognition) : opts.speech;
    var store = opts.storage === undefined ? (function () { try { return root.sessionStorage; } catch (e) { return null; } })() : opts.storage;
    var stopped = false, timer = 0, last = "", thread = null, current = null, ui = { open: {} }, drafts = {};
    var wrap = el(doc, "div", "rv"), body = el(doc, "div", "rv-body"), dock = el(doc, "div", "rv-dock");
    wrap.appendChild(body); wrap.appendChild(dock); host.appendChild(wrap);
    var ta = el(doc, "textarea", "rv-input"), actions = el(doc, "div", "rv-actions"),
      sendB = el(doc, "button", "rv-send", "Send"), dropB = el(doc, "button", "rv-drop", "Drop"),
      stopB = el(doc, "button", "rv-stop", "Stop"), note = el(doc, "div", "rv-sendnote");
    ta.setAttribute("rows", "3"); ta.setAttribute("aria-label", "Message to the room");
    [sendB, dropB, stopB].forEach(function (b) { b.setAttribute("type", "button"); });
    note.setAttribute("role", "status");
    actions.appendChild(sendB); actions.appendChild(dropB); actions.appendChild(stopB);
    var micB = null, rec = null;
    if (Speech) {
      micB = el(doc, "button", "rv-mic", "Dictate");
      micB.setAttribute("type", "button");
      micB.setAttribute("title", "Your browser's own speech service hears this, only while this page is open");
      actions.appendChild(micB);
    }
    var rotB = null;
    if (opts.rotate !== false) { rotB = el(doc, "button", "rv-rotate", "New conversation"); rotB.setAttribute("type", "button"); actions.appendChild(rotB); }
    dock.appendChild(ta); dock.appendChild(actions); dock.appendChild(note);
    var flight = { send: false, drop: false, stop: false }, sendId = null, sendText = null, dropId = null, dropText = null;
    try { if (store && store.getItem("levain.room.draft")) ta.value = store.getItem("levain.room.draft"); } catch (e) { /* no storage */ }
    ta.addEventListener("input", function () { try { if (store) store.setItem("levain.room.draft", ta.value); } catch (e) { /* no storage */ } });

    function newId() {
      var c = root.crypto;
      return "ob-" + (c && c.randomUUID ? c.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2));
    }
    function paintDock() {
      var m = current;
      var can = !!m && m.canSend;
      sendB.disabled = !can || flight.send;
      dropB.disabled = !m || !m.open || flight.drop;
      stopB.disabled = !m || !m.canStop || flight.stop;
      ta.disabled = !m || !m.open;
      if (rotB) rotB.disabled = !m || !m.open;
    }
    function post(path, payload) {
      var h = Object.assign({ "Content-Type": "application/json" }, headers());
      return f(POST_PREFIX + path, { method: "POST", headers: h, body: JSON.stringify(payload) })
        .then(function (res) { return res.text().then(function (t) { var j = null; try { j = JSON.parse(t); } catch (e) { /* not JSON */ } return { status: res.status, json: j }; }); });
    }
    function clearDraft(text) {
      if (ta.value === text) { ta.value = ""; try { if (store) store.removeItem("levain.room.draft"); } catch (e) { /* no storage */ } }
    }
    sendB.addEventListener("click", function () {
      var text = ta.value;
      if (!current || !current.thread || !text.trim() || sendB.disabled || flight.send) return;
      if (sendId === null || sendText !== text) sendId = newId();      // one id per draft text: a resend reuses it
      sendText = text;
      flight.send = true; paintDock(); note.textContent = "Sending...";
      post("send", { room_thread_id: current.thread, body: text, outbox_id: sendId }).then(function (r) {
        flight.send = false;
        if (r.status === 200 && r.json && r.json.receipt) { clearDraft(text); sendId = null; sendText = null; }
        else if (r.status < 500 && r.status !== 202) sendId = null;
        note.textContent = sendWords(r.status, r.json);
        last = ""; paintDock(); return pull();
      }).catch(function () {
        flight.send = false;
        note.textContent = "The server could not be reached; the line may or may not have been sent. Sending again will not duplicate it.";
        paintDock();
      });
    });
    dropB.addEventListener("click", function () {
      var text = ta.value;
      if (!current || !text.trim() || dropB.disabled || flight.drop) return;
      if (dropId === null || dropText !== text) dropId = newId();
      dropText = text;
      flight.drop = true; paintDock(); note.textContent = "Dropping...";
      post("drop", { words: text, outbox_id: dropId }).then(function (r) {
        flight.drop = false;
        if (r.status === 200) { clearDraft(text); dropId = null; dropText = null; note.textContent = "Dropped: your words went to the desk."; }
        else { if (r.status < 500) dropId = null; note.textContent = (r.json && r.json.message) ? "Not dropped: " + r.json.message : "Not dropped: the server answered " + r.status + "."; }
        paintDock();
      }).catch(function () { flight.drop = false; note.textContent = "The server could not be reached; the drop may or may not have gone. Dropping again will not duplicate it."; paintDock(); });
    });
    stopB.addEventListener("click", function () {
      if (!current || !current.thread || stopB.disabled || flight.stop) return;
      flight.stop = true; paintDock(); note.textContent = "Stopping...";
      post("stop", { room_thread_id: current.thread }).then(function (r) {
        var o = r.json && r.json.outcome;
        note.textContent = o && own(STOP_WORDS, o) ? STOP_WORDS[o] : ((r.json && r.json.message) || "The stop was not sent.");
        last = ""; return pull();
      }).catch(function () { note.textContent = "The stop could not be sent: the server could not be reached."; })
        .then(function () { flight.stop = false; paintDock(); });
    });
    if (rotB) rotB.addEventListener("click", function () {
      if (rotB.disabled) return;
      rotB.disabled = true; note.textContent = "Starting a new conversation...";
      post("rotate", {}).then(function (r) {
        note.textContent = r.status === 200 ? "A new conversation is open." : ((r.json && r.json.message) || "No new conversation was started.");
        last = ""; return pull();
      }).catch(function () { note.textContent = "The server could not be reached."; }).then(paintDock);
    });
    if (micB) {
      micB.addEventListener("click", function () {
        if (rec) { rec.stop(); return; }
        var r = new Speech(), base = ta.value ? ta.value.replace(/\s*$/, " ") : "";
        r.interimResults = true; r.continuous = false;
        r.onresult = function (ev) {
          var t = "";
          for (var i = 0; i < ev.results.length; i++) t += ev.results[i][0].transcript;
          ta.value = base + t;
          try { if (store) store.setItem("levain.room.draft", ta.value); } catch (e) { /* no storage */ }
        };
        r.onend = function () { rec = null; micB.textContent = "Dictate"; };
        r.onerror = function () { rec = null; micB.textContent = "Dictate"; };
        rec = r; micB.textContent = "Stop dictating";
        try { r.start(); } catch (e) { rec = null; micB.textContent = "Dictate"; }
      });
      // foreground only: leaving the page ends dictation
      var leave = function () { if (rec) rec.stop(); };
      doc.addEventListener("visibilitychange", function () { if (doc.visibilityState === "hidden") leave(); });
      root.addEventListener && root.addEventListener("blur", leave);
    }

    var hooks = {
      onMark: function (ref, delivered, mnote, done) {
        mnote.textContent = "Marking...";
        post("mark", { room_thread_id: thread, message_id: ref, delivered: delivered }).then(function (r) {
          mnote.textContent = (r.status === 200 && r.json && own(MARK_WORDS, r.json.state)) ? MARK_WORDS[r.json.state] : "Not marked: " + ((r.json && r.json.message) || "the server answered " + r.status + ".");
          if (r.status === 200) { last = ""; pull(); } else done();
        }).catch(function () { mnote.textContent = "Not marked: the server could not be reached."; done(); });
      },
      onCommit: function (card, words, cnote, btns) {
        if (!words.trim()) { cnote.textContent = "Nothing to drop: the field is empty."; return; }
        btns.forEach(function (b) { b.disabled = true; });
        var key = card.id + "|" + words;
        if (!hooks.ids[key]) hooks.ids[key] = newId();
        cnote.textContent = "Dropping...";
        post("proposal/" + encodeURIComponent(card.id) + "/commit", { words: words, outbox_id: hooks.ids[key] }).then(function (r) {
          if (r.status === 200) { delete drafts[card.id]; last = ""; pull(); return; }
          if (r.status < 500) delete hooks.ids[key];
          cnote.textContent = "Not dropped: " + ((r.json && r.json.message) || "the server answered " + r.status + ".");
          btns.forEach(function (b) { b.disabled = card.state === "expired" && b.className === "rv-commit"; });
        }).catch(function () { cnote.textContent = "The server could not be reached; dropping again will not duplicate it."; btns.forEach(function (b) { b.disabled = false; }); });
      },
      onDismiss: function (card, cnote, btns) {
        btns.forEach(function (b) { b.disabled = true; });
        post("proposal/" + encodeURIComponent(card.id) + "/dismiss", {}).then(function (r) {
          if (r.status === 200) { delete drafts[card.id]; last = ""; pull(); return; }
          cnote.textContent = "Not dismissed: " + ((r.json && r.json.message) || "the server answered " + r.status + ".");
          btns.forEach(function (b) { b.disabled = false; });
        }).catch(function () { cnote.textContent = "The server could not be reached."; btns.forEach(function (b) { b.disabled = false; }); });
      },
      ids: {}
    };

    function pull() {
      if (stopped) return Promise.resolve();
      return f(ROOM_PATH, { cache: "no-store", headers: headers() }).then(function (res) {
        if (stopped) return null;
        if (!res.ok) { clear(body); body.appendChild(el(doc, "div", "rv-note", res.status === 403 ? "This page carries no token for the room." : "The room could not be read.")); last = ""; return null; }
        return res.text().then(function (t) {
          if (stopped) return;
          if (t !== last) {
            last = t;
            current = roomModel(JSON.parse(t));
            if (current.thread !== thread) { ui.open = {}; drafts = {}; thread = current.thread; }
            renderRoom(doc, body, current, hooks, ui, drafts);
          }
          paintDock();
        });
      }).catch(function () { if (!stopped) { clear(body); body.appendChild(el(doc, "div", "rv-note", "The room could not be read.")); last = ""; } });
    }
    function loop() { if (!stopped && pollMs > 0) timer = setTimeout(function () { pull().then(loop); }, pollMs); }
    paintDock();
    pull().then(loop);
    return { stop: function () { stopped = true; clearTimeout(timer); if (rec) rec.stop(); }, refresh: function () { last = ""; return pull(); }, model: function () { return current; } };
  }

  var api = { roomModel: roomModel, lineModel: lineModel, sendWords: sendWords, renderRoom: renderRoom, mount: mount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainRoomView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
