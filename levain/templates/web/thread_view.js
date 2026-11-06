// levain thread_view.js — the cockpit's work-thread view (CH-U1a read half; a Stop for a running turn).
//
// A pure view-model plus a renderer for the two JSON bodies levain/cockpit/threads.py serves: the thread list and one
// thread's stream. Read-only unless mount is given {stop: true}: then a thread that names a launch the server reports
// `stoppable` (its service holds a session for it) carries a Stop that
// posts to /cockpit/thread/<id>/stop and never decides anything itself: whether a stop landed is the server's word,
// drawn as given. A work thread has NO composer: the operator talks in the room (room_view.js), and steering a work thread
// is a drop. Every word that came from a thread is set with textContent, never as markup. The stream is drawn in the
// order the server sent (gseq); nothing here sorts it. An operator line carries the server's `unverified` mark and the
// view says so; it never says "verified". An approval is an inert line: no link, no card id (the record holds no
// correlation to a queue request yet).
(function (root) {
  "use strict";

  var LIST_PATH = "/cockpit/threads.json";
  var THREAD_PREFIX = "/cockpit/thread/";
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

  var STOP_WORDS = {
    confirmed: "Stop confirmed.",
    no_such_request: "Nothing was running to stop.",
    ambiguous: "The stop was sent; the driver did not confirm it.",
    rejected: "The driver refused the stop."
  };
  // the authority line is the server's sentence, drawn as given; a body that carries none says so rather than read as "none"
  function authorityWords(a) {
    if (a && typeof a.line === "string" && a.line) return { text: a.line, state: own(AUTHORITY_STATES, a.state) ? a.state : "unreadable" };
    return { text: "The standing authority for this thread was not reported.", state: "unreadable" };
  }
  var AUTHORITY_STATES = { none: 1, bound: 1, ended: 1, expired: 1, unreadable: 1 };

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

  // A Read's result: the vendor numbers every line (``cat -n``: a right-aligned number, a tab, the line). The number is the
  // vendor's formatting and not the file's text, so it is not drawn. A tool result carries no tool name, so the test is the
  // shape: the leading run of lines that open with a number and a tab, each one more than the last; the run ends at the first
  // line that is not the next number. Display only: the record is not changed. This is a COPY of withoutLineNumbers in
  // room_view.js (two served files with no shared loader); tests/test_thread_rows_1105.py holds the two to the same answers.
  var NUMBERED = /^ *(\d+)\t/;
  function withoutLineNumbers(text) {
    var lines = text.split("\n"), n = 0, prev = -1;
    for (; n < lines.length; n++) {
      var m = NUMBERED.exec(lines[n]);
      if (!m || (prev >= 0 && Number(m[1]) !== prev + 1)) break;
      prev = Number(m[1]);
    }
    if (n === 0) return text;
    for (var i = 0; i < n; i++) lines[i] = lines[i].replace(NUMBERED, "");
    return lines.join("\n");
  }

  function line(it) {
    var who = it.kind === "message" ? it.source : (it.source === "vendor" ? "agent side" : it.source);
    var status = it.status ? String(it.status).replace(/_/g, " ") : "";
    return {
      gseq: it.gseq, kind: it.kind, label: label(it.kind), who: who, ref: it.ref == null ? "" : String(it.ref),
      text: own(APPROVAL_WORDS, it.kind) ? APPROVAL_WORDS[it.kind]
        : (it.body == null ? "" : (it.kind === "vendor_tool_result" ? withoutLineNumbers(String(it.body)) : String(it.body))),
      status: status, unverified: it.unverified === true, truncated: it.truncated === true,
      tool: it.tool ? { open: it.tool.open === true } : null,
      collapsible: own(COLLAPSED_KINDS, it.kind),
      at: it.at || ""
    };
  }

  // Whether a turn is running: `stoppable` says only that a session is held (an idle thread and a running one both have one),
  // so it is read from the turn rows the server sends in the stream, as the room does. Only the CURRENT launch's turn rows count
  // (the server names it, body.launch_id): a late row of an older or a room launch decides nothing about it. Its latest turn row
  // is a start with no end after it => running. With no turn row of it in the stream and the body's gap flag set, a start may be
  // missing from the record, so the turn is taken as running (unknown is running: Stop stays enabled, and a stop sent in a race
  // still gets the server's "Nothing was running to stop."). No gap and no turn row is an idle thread. This is a COPY of
  // turnRunning in room_view.js.
  function turnRunning(stream, launchId, gap) {
    if (typeof launchId !== "string" || !launchId) return false;
    var last = "";
    for (var i = 0; i < stream.length; i++) {
      var it = stream[i];
      if (it && it.launch_id === launchId && (it.kind === "vendor_turn_started" || it.kind === "vendor_turn_ended")) last = it.kind;
    }
    return last === "" ? gap === true : last === "vendor_turn_started";
  }

  function threadModel(body) {
    return {
      id: String(body.thread_id), launchId: body.launch_id == null ? null : String(body.launch_id), running: turnRunning(body.stream || [], body.launch_id, body.gap === true),
      gap: body.gap === true, authority: authorityWords(body.authority), model: modelWords(body.model),
      before: (body.before || []).map(line), stream: (body.stream || []).map(line)
    };
  }

  function listModel(body) {
    return {
      truncated: body.truncated === true,
      rows: (body.threads || []).map(function (t) {
        return {
          id: String(t.thread_id), latest: t.latest_gseq == null ? "no record yet" : "gseq " + t.latest_gseq,
          at: t.latest_at || "", launch: t.launch_id ? "worker launch" : "no worker launch"
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

  // ui.open is the page's own record of which collapsed lines are open, keyed by gseq: a redraw reads it, a click writes it
  function renderLine(doc, l, ui) {
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
    return row;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function renderThread(doc, host, m, ui) {
    clear(host);
    host.appendChild(el(doc, "h3", "tv-title", m.id));
    if (/^[A-Za-z0-9._:-]{1,160}$/.test(m.id)) {
      // the head card (extend, replace or sign a successor) lives on the drop sheet, the one page the passkey signs on
      var card = el(doc, "a", "tv-card-link", "Head card: extend, replace or sign a successor");
      // the id unencoded: it already matched the sheet's own pattern above, and that pattern refuses "%" (an encoded ":" would be dropped)
      card.setAttribute("href", "/sign/drop#thread=" + m.id);
      card.setAttribute("target", "_blank");
      card.setAttribute("rel", "noopener");
      host.appendChild(card);
    }
    if (m.model) host.appendChild(el(doc, "div", "tv-model", m.model));
    host.appendChild(el(doc, "div", "tv-authority tv-authority-" + m.authority.state, m.authority.text));
    if (m.gap) host.appendChild(el(doc, "div", "tv-gap", "The driver could not catch up: lines may be missing from this stream."));
    if (m.before.length) {
      var blk = el(doc, "div", "tv-before");
      blk.appendChild(el(doc, "div", "tv-note", "Before the chat record"));
      m.before.forEach(function (l) { blk.appendChild(renderLine(doc, l, ui)); });
      host.appendChild(blk);
    }
    var stream = el(doc, "div", "tv-stream");
    m.stream.forEach(function (l) { stream.appendChild(renderLine(doc, l, ui)); });
    if (!m.stream.length && !m.before.length) stream.appendChild(el(doc, "div", "tv-note", "Nothing has been said in this thread yet."));
    host.appendChild(stream);
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
      b.appendChild(el(doc, "span", "tv-state", r.launch));
      b.addEventListener("click", function () { onOpen(r.id); });
      host.appendChild(b);
    });
    if (m.truncated) host.appendChild(el(doc, "div", "tv-note", "More threads exist than are listed."));
  }

  // mount(host, {fetch, headers, pollMs, stop}): the list; a click opens a thread; "back" returns. Polls while mounted and
  // redraws only when the body changed. Each navigation takes a new generation and a reply that is not the latest generation's is dropped, on success and on failure alike. Returns {stop, open, back}.
  function mount(host, opts) {
    opts = opts || {};
    var doc = host.ownerDocument;
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var pollMs = opts.pollMs == null ? 2000 : opts.pollMs;
    var current = null, last = "", timer = 0, stopped = false, gen = 0;
    var bar = el(doc, "div", "tv-bar"), body = el(doc, "div", "tv-body-host"), dock = el(doc, "div", "tv-dock");
    var withStop = opts.stop === true, ui = { open: {} }, stopFor = null, stopForm = null;
    host.appendChild(bar); host.appendChild(body); host.appendChild(dock);

    function paintBar() {
      paintStop(null, null, false);
      clear(bar);
      if (current) {
        var back = el(doc, "button", "tv-back", "All threads");
        back.setAttribute("type", "button");
        back.addEventListener("click", function () { api.back(); });
        bar.appendChild(back);
      }
    }
    function say(text) { last = ""; clear(body); body.appendChild(el(doc, "div", "tv-note", text)); }
    function post(which, action, payload) {
      var h = Object.assign({ "Content-Type": "application/json" }, headers());
      return f(THREAD_PREFIX + encodeURIComponent(which) + "/" + action, { method: "POST", headers: h, body: JSON.stringify(payload) })
        .then(function (res) { return res.text().then(function (t) { var j = null; try { j = JSON.parse(t); } catch (e) { /* not JSON */ } return { status: res.status, json: j }; }); });
    }
    // the Stop lives OUTSIDE the redrawn body, so a poll never replaces it mid-request. It carries the launch the page last
    // drew (`launchId`): the server refuses the stop if the thread's launch changed under the page.
    function paintStop(which, launchId, running) {
      if (!withStop || !which || launchId == null) { stopForm = null; stopFor = null; clear(dock); return; }
      if (stopFor !== which) {
        stopFor = which; clear(dock);
        var stopB = el(doc, "button", "tv-stop", "Stop"), note = el(doc, "div", "tv-sendnote");
        stopB.setAttribute("type", "button"); note.setAttribute("role", "status");
        stopB.addEventListener("click", function () {
          var mine = stopForm;
          if (!mine || stopB.disabled || mine.stopping) return;
          mine.stopping = true; stopB.disabled = true; note.textContent = "Stopping...";
          post(which, "stop", { expected_launch_id: mine.launchId }).then(function (r) {
            if (stopForm !== mine) return;
            var o = r.json && r.json.outcome;
            note.textContent = o && own(STOP_WORDS, o) ? STOP_WORDS[o] : ((r.json && r.json.message) || "The stop was not sent.");
            last = ""; pull();
          }).catch(function () { if (stopForm === mine) note.textContent = "The stop could not be sent: the server could not be reached."; })
            .then(function () { if (stopForm === mine) { mine.stopping = false; stopB.disabled = !mine.running; } });
        });
        dock.appendChild(stopB); dock.appendChild(note);
        stopForm = { stopB: stopB, launchId: null, stopping: false, running: false };
      }
      stopForm.launchId = String(launchId);
      // pressable only while a turn runs; a stop already sent keeps the button down until its answer is drawn. A stop sent in a
      // race still gets the server's "Nothing was running to stop."
      stopForm.running = running === true;
      stopForm.stopB.disabled = stopForm.stopping || !stopForm.running;
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
          // a Stop only where the server says one can reach a session (j.stoppable): with no chat service the route is a
          // 404, and the room's service holds no work launch (409), so a Stop there could never succeed
          if (which) { var tm = threadModel(j); renderThread(doc, body, tm, ui); paintStop(which, j.stoppable === true ? j.launch_id : null, tm.running); }
          else { renderList(doc, body, listModel(j), function (id) { api.open(id); }); paintStop(null, null, false); }
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

  var api = { listModel: listModel, threadModel: threadModel, renderList: renderList, renderThread: renderThread, mount: mount, authorityWords: authorityWords, modelWords: modelWords };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainThreadView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
