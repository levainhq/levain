// levain thread_view.js — the cockpit's thread view (CH-U1a read half, CH-U1b composer).
//
// A pure view-model plus a renderer for the two JSON bodies levain/cockpit/threads.py serves: the thread list and one
// thread's stream. Read-only unless mount is given {compose: true}: then a thread carries a composer (a draft, send,
// stop) that posts to /cockpit/thread/<id>/send and /stop and never decides anything itself: what may be sent, who holds
// the turn and whether a stop landed are the server's words, drawn as given. Every word that came from a thread is set with
// textContent, never as markup. The stream is drawn in the order the server sent (gseq); nothing here sorts it.
// An operator line carries the server's `unverified` mark and the view says so; it never says "verified". An approval is
// an inert line: no link, no card id (the record holds no correlation to a queue request yet).
(function (root) {
  "use strict";

  var LIST_PATH = "/cockpit/threads.json";
  var THREAD_PREFIX = "/cockpit/thread/";
  var COMPOSER_WORDS = {
    ended: "The launch has ended.",
    no_session: "No session is attached to this thread.",
    not_live: "The session is not live.",
    checking: "The session is being checked.",
    taken: "Another surface holds the turn.",
    writable: "A message can be sent."
  };
  var KIND_LABEL = {
    message: "message", vendor_user_item: "outside line", vendor_echo: "echo", vendor_agent_text: "agent",
    vendor_tool_call: "tool", vendor_tool_result: "tool result", vendor_approval_request: "approval",
    vendor_approval_resolved: "approval answered", vendor_turn_started: "turn started", vendor_turn_ended: "turn ended",
    vendor_error: "error", vendor_gap: "gap", reply_receipt: "receipt", reply_sign: "sign", reply_answer: "answer",
    reply_know: "know", reply_coordinator: "reply", envelope_bound: "envelope"
  };

  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function label(kind) {
    if (own(KIND_LABEL, kind)) return KIND_LABEL[kind];
    return String(kind).indexOf("attenuation_") === 0 ? "narrowed" : String(kind).replace(/_/g, " ");
  }
  function composerWords(c, launchId) {
    if (!c) return launchId ? "The launch's state could not be read." : "This thread has no worker launch.";
    var w = own(COMPOSER_WORDS, c.state) ? COMPOSER_WORDS[c.state] : "The composer state is not known.";
    return c.why && c.state !== "writable" ? w + " (" + c.why + ")" : w;
  }

  var STOP_STATES = { writable: 1, taken: 1, checking: 1, not_live: 1 };
  var STOP_WORDS = {
    confirmed: "Stop confirmed.",
    no_such_request: "Nothing was running to stop.",
    ambiguous: "The stop was sent; the driver did not confirm it.",
    rejected: "The driver refused the stop."
  };
  var SEND_WORDS = {
    accepted: "Sent.",
    pending: "Sent; waiting on the driver.",
    attempted: "Sent; waiting on the driver.",
    ambiguous: "Sent, but the vendor may or may not have taken it. It is not sent again."
  };
  // the words for a receipt that moved nothing; the draft stays
  var UNSENT_WORDS = { refused: "Not sent: ", cancelled: "Not delivered: ", not_delivered: "Not delivered: " };

  // what the composer shows for a thread body: sending needs the server's own `writable`; stop is offered wherever a
  // session is attached (the server's stop is unfenced), never on a launch that has ended or has no session
  function composerModel(c) {
    var st = c && own(COMPOSER_WORDS, c.state) ? c.state : "";
    return { canSend: st === "writable", canStop: own(STOP_STATES, st), state: st };
  }
  var MARK_WORDS = {
    accepted: "Marked delivered.",
    not_delivered: "Marked not delivered."
  };
  function markWords(status, j) {
    if (status === 200 && j && own(MARK_WORDS, j.state)) return MARK_WORDS[j.state];
    if (j && j.message) return "Not marked: " + j.message;
    return "Not marked: the server answered " + status + ".";
  }
  // the authority line is the server's sentence, drawn as given; a body that carries none says so rather than read as "none"
  function authorityWords(a) {
    if (a && typeof a.line === "string" && a.line) return { text: a.line, state: own(AUTHORITY_STATES, a.state) ? a.state : "unreadable" };
    return { text: "The standing authority for this thread was not reported.", state: "unreadable" };
  }
  var AUTHORITY_STATES = { none: 1, bound: 1, ended: 1, expired: 1, unreadable: 1 };
  function answerWords(status, j) {
    if (status >= 500) return "The server answered " + status + "; the message may or may not have been sent. Sending again will not duplicate it.";
    var r = j && j.receipt;
    if (status === 409 && r && own(UNSENT_WORDS, r.state)) return UNSENT_WORDS[r.state] + (r.cause || (j && j.message) || r.state);
    if (j && j.message) return "Not sent: " + j.message;
    return "Not sent: the server answered " + status + ".";
  }

  var APPROVAL_WORDS = {
    vendor_approval_request: "An approval was asked; answer it in Needs you.",
    vendor_approval_resolved: "An approval was answered."
  };

  // a tool call and its result draw collapsed (one summary line; the body opens on a click). Approvals are never in this set:
  // they stay inert lines, with no sign button and no card id.
  var COLLAPSED_KINDS = { vendor_tool_call: 1, vendor_tool_result: 1 };
  function peek(text) {
    var first = String(text || "").split("\n")[0].trim();
    return first.length > 120 ? first.slice(0, 120) + "..." : first;
  }
  function modelWords(m) { return typeof m === "string" && m ? "Model in use: " + m : ""; }

  function line(it) {
    var who = it.kind === "message" ? it.source : (it.source === "vendor" ? "agent side" : it.source);
    var status = it.status ? String(it.status).replace(/_/g, " ") : "";
    return {
      gseq: it.gseq, kind: it.kind, label: label(it.kind), who: who, ref: it.ref == null ? "" : String(it.ref),
      ambiguous: it.kind === "message" && it.status === "ambiguous",
      text: own(APPROVAL_WORDS, it.kind) ? APPROVAL_WORDS[it.kind] : (it.body == null ? "" : String(it.body)),
      status: status, unverified: it.unverified === true, truncated: it.truncated === true,
      tool: it.tool ? { open: it.tool.open === true } : null,
      collapsible: own(COLLAPSED_KINDS, it.kind),
      at: it.at || ""
    };
  }

  function threadModel(body) {
    return {
      id: String(body.thread_id), launchId: body.launch_id == null ? null : String(body.launch_id),
      gap: body.gap === true, composer: composerWords(body.composer, body.launch_id),
      authority: authorityWords(body.authority), model: modelWords(body.model),
      before: (body.before || []).map(line), stream: (body.stream || []).map(line)
    };
  }

  function listModel(body) {
    return {
      truncated: body.truncated === true,
      rows: (body.threads || []).map(function (t) {
        return {
          id: String(t.thread_id), latest: t.latest_gseq == null ? "no record yet" : "gseq " + t.latest_gseq,
          at: t.latest_at || "", composer: composerWords(t.composer, t.launch_id), state: t.composer ? t.composer.state : "none"
        };
      })
    };
  }

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // hooks.onMark(messageId, delivered, note): the two reconcile buttons of an ambiguous send; absent in a read-only view
  // ui.open is the page's own record of which collapsed lines are open, keyed by gseq: a redraw reads it, a click writes it
  function renderLine(doc, l, hooks, ui) {
    var row = el(doc, "div", "tv-line tv-" + String(l.kind).replace(/[^a-z_]/g, ""));
    var fold = l.collapsible === true && !!ui, shown = fold && ui.open[l.gseq] === true;
    var head = el(doc, fold ? "button" : "div", fold ? "tv-head tv-toggle" : "tv-head");
    if (fold) { head.setAttribute("type", "button"); head.setAttribute("aria-expanded", shown ? "true" : "false"); row.className += " tv-fold"; }
    head.appendChild(el(doc, "span", "tv-kind", l.label));
    head.appendChild(el(doc, "span", "tv-who", l.who));
    if (l.unverified) head.appendChild(el(doc, "span", "tv-unverified", "unverified"));
    if (l.status) head.appendChild(el(doc, "span", "tv-status", l.status));
    if (l.tool) head.appendChild(el(doc, "span", l.tool.open ? "tv-tool-open" : "tv-tool-done", l.tool.open ? "running" : "done"));
    if (fold && !shown && l.text) head.appendChild(el(doc, "span", "tv-peek", peek(l.text)));
    head.appendChild(el(doc, "span", "tv-at", l.at));
    row.appendChild(head);
    var pre = l.text ? el(doc, "pre", "tv-body", l.text) : null;
    if (pre) row.appendChild(pre);
    if (fold && pre) {
      pre.hidden = !shown;
      head.addEventListener("click", function () {
        var now = pre.hidden;                       // opening when it was hidden
        pre.hidden = !now; head.setAttribute("aria-expanded", now ? "true" : "false");
        if (now) ui.open[l.gseq] = true; else delete ui.open[l.gseq];
        var pk = head.querySelector(".tv-peek");
        if (now && pk) head.removeChild(pk);
        else if (!now && !pk) head.insertBefore(el(doc, "span", "tv-peek", peek(l.text)), head.querySelector(".tv-at"));
      });
    }
    if (l.truncated) row.appendChild(el(doc, "div", "tv-note", "Cut short; the vendor holds the rest."));
    if (l.ambiguous && l.ref && hooks && hooks.onMark) {
      var box = el(doc, "div", "tv-mark"), yes = el(doc, "button", "tv-mark-yes", "Mark delivered"),
        no = el(doc, "button", "tv-mark-no", "Mark not delivered"), note = el(doc, "span", "tv-marknote");
      yes.setAttribute("type", "button"); no.setAttribute("type", "button"); note.setAttribute("role", "status");
      box.appendChild(el(doc, "span", "tv-marklabel", "The vendor may or may not have taken this. You decide:"));
      [[yes, true], [no, false]].forEach(function (p) {
        p[0].addEventListener("click", function () { yes.disabled = true; no.disabled = true; hooks.onMark(l.ref, p[1], note, function () { yes.disabled = false; no.disabled = false; }); });
      });
      box.appendChild(yes); box.appendChild(no); box.appendChild(note);
      row.appendChild(box);
    }
    return row;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function renderThread(doc, host, m, readOnly, hooks, ui) {
    clear(host);
    host.appendChild(el(doc, "h3", "tv-title", m.id));
    if (m.model) host.appendChild(el(doc, "div", "tv-model", m.model));
    host.appendChild(el(doc, "div", "tv-authority tv-authority-" + m.authority.state, m.authority.text));
    if (m.gap) host.appendChild(el(doc, "div", "tv-gap", "The driver could not catch up: lines may be missing from this stream."));
    if (m.before.length) {
      var blk = el(doc, "div", "tv-before");
      blk.appendChild(el(doc, "div", "tv-note", "Before the chat record"));
      m.before.forEach(function (l) { blk.appendChild(renderLine(doc, l, hooks, ui)); });
      host.appendChild(blk);
    }
    var stream = el(doc, "div", "tv-stream");
    m.stream.forEach(function (l) { stream.appendChild(renderLine(doc, l, hooks, ui)); });
    if (!m.stream.length && !m.before.length) stream.appendChild(el(doc, "div", "tv-note", "Nothing has been said in this thread yet."));
    host.appendChild(stream);
    host.appendChild(el(doc, "div", "tv-composer", m.composer + (readOnly === false ? "" : " This view is read-only.")));
  }

  function renderList(doc, host, m, onOpen) {
    clear(host);
    if (!m.rows.length) host.appendChild(el(doc, "div", "tv-note", "No threads are recorded."));
    m.rows.forEach(function (r) {
      var b = el(doc, "button", "tv-row");
      b.setAttribute("type", "button");
      b.setAttribute("data-thread", r.id);
      b.appendChild(el(doc, "span", "tv-id", r.id));
      b.appendChild(el(doc, "span", "tv-latest", r.latest));
      b.appendChild(el(doc, "span", "tv-at", r.at));
      b.appendChild(el(doc, "span", "tv-state tv-state-" + String(r.state).replace(/[^a-z_]/g, ""), r.composer));
      b.addEventListener("click", function () { onOpen(r.id); });
      host.appendChild(b);
    });
    if (m.truncated) host.appendChild(el(doc, "div", "tv-note", "More threads exist than are listed."));
  }

  // mount(host, {fetch, headers, pollMs}): the list; a click opens a thread; "back" returns. Polls while mounted and
  // redraws only when the body changed. Each navigation takes a new generation and a reply that is not the latest generation's is dropped, on success and on failure alike. Returns {stop, open, back}.
  function mount(host, opts) {
    opts = opts || {};
    var doc = host.ownerDocument;
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var pollMs = opts.pollMs == null ? 2000 : opts.pollMs;
    var current = null, last = "", timer = 0, stopped = false, gen = 0;
    var bar = el(doc, "div", "tv-bar"), body = el(doc, "div", "tv-body-host"), dock = el(doc, "div", "tv-dock");
    var compose = opts.compose === true, ui = { open: {} }, form = null, formFor = null, outboxId = null, lastBody = null;
    host.appendChild(bar); host.appendChild(body); host.appendChild(dock);

    function paintBar() {
      paintDock(null, null);
      clear(bar);
      if (current) {
        var back = el(doc, "button", "tv-back", "All threads");
        back.setAttribute("type", "button");
        back.addEventListener("click", function () { api.back(); });
        bar.appendChild(back);
      }
    }
    function say(text) { last = ""; clear(body); body.appendChild(el(doc, "div", "tv-note", text)); }
    function newId() {
      var c = root.crypto;
      return "ob-" + (c && c.randomUUID ? c.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2));
    }
    function post(which, action, payload) {
      var h = Object.assign({ "Content-Type": "application/json" }, headers());
      return f(THREAD_PREFIX + encodeURIComponent(which) + "/" + action, { method: "POST", headers: h, body: JSON.stringify(payload) })
        .then(function (res) { return res.text().then(function (t) { var j = null; try { j = JSON.parse(t); } catch (e) { /* not JSON */ } return { status: res.status, json: j }; }); });
    }
    // an ambiguous send's reconcile: the server derives the owner from the credential; the page names only the message and the verdict
    var hooks = compose ? { onMark: function (messageId, delivered, note, done) {
      var mine = current;
      note.textContent = "Marking...";
      post(mine, "mark", { message_id: messageId, delivered: delivered }).then(function (r) {
        note.textContent = markWords(r.status, r.json);
        if (r.status === 200) { last = ""; if (current === mine) pull(); } else done();
      }).catch(function () { note.textContent = "Not marked: the server could not be reached."; done(); });
    } } : null;
    // the composer lives OUTSIDE the redrawn body, so a poll never takes the draft; it is built once per open thread.
    // A request's answer is applied only to the form object that sent it (a thread left and reopened builds a new one),
    // and a button stays disabled while its own request is in flight, whatever a poll says. Send and stop carry the
    // launch the page last drew (`launchId`): the server refuses either if the thread's launch changed under the page.
    function paintDock(c, which, launchId) {
      if (!compose || !which) { form = null; formFor = null; clear(dock); return; }
      var cm = composerModel(c);
      if (formFor !== which) {
        formFor = which; outboxId = null; lastBody = null; clear(dock);
        var ta = el(doc, "textarea", "tv-input"), row = el(doc, "div", "tv-actions");
        var sendB = el(doc, "button", "tv-send", "Send"), stopB = el(doc, "button", "tv-stop", "Stop"), note = el(doc, "div", "tv-sendnote");
        ta.setAttribute("rows", "3"); ta.setAttribute("aria-label", "Message to this thread");
        sendB.setAttribute("type", "button"); stopB.setAttribute("type", "button");
        note.setAttribute("role", "status");
        sendB.addEventListener("click", function () {
          var mine = form, text = ta.value;
          if (!mine || !text.trim() || sendB.disabled || mine.sending) return;
          // one outbox id per draft text: a resend after a lost answer reuses it (the server sends nothing twice); a
          // definite answer, or an edited draft, takes a new one
          if (outboxId === null || lastBody !== text) outboxId = newId();
          lastBody = text;
          mine.sending = true; paintButtons(); note.textContent = "Sending...";
          post(which, "send", { body: text, outbox_id: outboxId, expected_launch_id: mine.launchId }).then(function (r) {
            if (form !== mine) return;
            mine.sending = false;
            if (r.status === 200 && r.json && r.json.receipt) {
              // only the text that was sent is cleared: a draft typed while it was in flight stays
              if (ta.value === text) ta.value = "";
              outboxId = null; lastBody = null;
              note.textContent = own(SEND_WORDS, r.json.receipt.state) ? SEND_WORDS[r.json.receipt.state] : "Sent (" + r.json.receipt.state + ").";
            } else {
              if (r.status < 500) outboxId = null;
              note.textContent = answerWords(r.status, r.json);
            }
            last = ""; paintButtons(); pull();
          }).catch(function () {
            if (form !== mine) return;
            mine.sending = false;
            note.textContent = "The server could not be reached; the message may or may not have been sent. Sending again will not duplicate it.";
            paintButtons();
          });
        });
        stopB.addEventListener("click", function () {
          var mine = form;
          if (!mine || stopB.disabled || mine.stopping) return;
          mine.stopping = true; paintButtons(); note.textContent = "Stopping...";
          post(which, "stop", { expected_launch_id: mine.launchId }).then(function (r) {
            if (form !== mine) return;
            var o = r.json && r.json.outcome;
            note.textContent = o && own(STOP_WORDS, o) ? STOP_WORDS[o] : ((r.json && r.json.message) || "The stop was not sent.");
            last = ""; pull();
          }).catch(function () { if (form === mine) note.textContent = "The stop could not be sent: the server could not be reached."; })
            .then(function () { if (form === mine) { mine.stopping = false; paintButtons(); } });
        });
        row.appendChild(sendB); row.appendChild(stopB);
        dock.appendChild(ta); dock.appendChild(row); dock.appendChild(note);
        form = { ta: ta, sendB: sendB, stopB: stopB, canSend: false, canStop: false, launchId: null, sending: false, stopping: false };
      }
      form.canSend = cm.canSend; form.canStop = cm.canStop; form.launchId = launchId == null ? null : String(launchId);
      form.ta.disabled = !cm.canSend;
      paintButtons();
    }
    function paintButtons() {
      if (!form) return;
      form.sendB.disabled = !form.canSend || !form.launchId || form.sending;
      form.stopB.disabled = !form.canStop || !form.launchId || form.stopping;
    }
    function pull() {
      if (stopped) return Promise.resolve();
      var which = current, g = gen, url = which ? THREAD_PREFIX + encodeURIComponent(which) + ".json" : LIST_PATH;
      return f(url, { cache: "no-store", headers: headers() }).then(function (res) {
        if (stopped || g !== gen) return null;
        if (!res.ok) { say(res.status === 403 ? "This page carries no token for the thread record." : "The thread record could not be read."); return null; }
        return res.text().then(function (t) {
          if (stopped || g !== gen || t === last) return;
          last = t;
          var j = JSON.parse(t);
          if (which) { renderThread(doc, body, threadModel(j), !compose, hooks, ui); paintDock(j.composer, which, j.launch_id); }
          else { renderList(doc, body, listModel(j), function (id) { api.open(id); }); paintDock(null, null); }
        });
      }).catch(function () { if (!stopped && g === gen) say("The thread record could not be read."); });
    }
    function loop() { if (!stopped && pollMs > 0) timer = setTimeout(function () { pull().then(loop); }, pollMs); }
    var api = {
      open: function (id) { if (id !== current) ui.open = {}; current = id; gen++; last = ""; paintBar(); return pull(); },
      back: function () { current = null; ui.open = {}; gen++; last = ""; paintBar(); return pull(); },
      stop: function () { stopped = true; clearTimeout(timer); }
    };
    paintBar();
    pull().then(loop);
    return api;
  }

  var api = { listModel: listModel, threadModel: threadModel, renderList: renderList, renderThread: renderThread, mount: mount, composerWords: composerWords, composerModel: composerModel, authorityWords: authorityWords, modelWords: modelWords, markWords: markWords };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainThreadView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
