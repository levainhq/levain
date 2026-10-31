// levain thread_view.js — the cockpit's thread view, read half (CH-U1a).
//
// A pure view-model plus a renderer for the two JSON bodies levain/cockpit/threads.py serves: the thread list and one
// thread's stream. Read-only: no composer input, no send, no stop. Every word that came from a thread is set with
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

  var APPROVAL_WORDS = {
    vendor_approval_request: "An approval was asked; answer it in Needs you.",
    vendor_approval_resolved: "An approval was answered."
  };

  function line(it) {
    var who = it.kind === "message" ? it.source : (it.source === "vendor" ? "agent side" : it.source);
    var status = it.status ? String(it.status).replace(/_/g, " ") : "";
    return {
      gseq: it.gseq, kind: it.kind, label: label(it.kind), who: who,
      text: own(APPROVAL_WORDS, it.kind) ? APPROVAL_WORDS[it.kind] : (it.body == null ? "" : String(it.body)),
      status: status, unverified: it.unverified === true, truncated: it.truncated === true,
      tool: it.tool ? { open: it.tool.open === true } : null,
      at: it.at || ""
    };
  }

  function threadModel(body) {
    return {
      id: String(body.thread_id), gap: body.gap === true, composer: composerWords(body.composer, body.launch_id),
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

  function renderLine(doc, l) {
    var row = el(doc, "div", "tv-line tv-" + String(l.kind).replace(/[^a-z_]/g, ""));
    var head = el(doc, "div", "tv-head");
    head.appendChild(el(doc, "span", "tv-kind", l.label));
    head.appendChild(el(doc, "span", "tv-who", l.who));
    if (l.unverified) head.appendChild(el(doc, "span", "tv-unverified", "unverified"));
    if (l.status) head.appendChild(el(doc, "span", "tv-status", l.status));
    if (l.tool) head.appendChild(el(doc, "span", l.tool.open ? "tv-tool-open" : "tv-tool-done", l.tool.open ? "running" : "done"));
    head.appendChild(el(doc, "span", "tv-at", l.at));
    row.appendChild(head);
    if (l.text) row.appendChild(el(doc, "pre", "tv-body", l.text));
    if (l.truncated) row.appendChild(el(doc, "div", "tv-note", "Cut short; the vendor holds the rest."));
    return row;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function renderThread(doc, host, m) {
    clear(host);
    host.appendChild(el(doc, "h3", "tv-title", m.id));
    if (m.gap) host.appendChild(el(doc, "div", "tv-gap", "The driver could not catch up: lines may be missing from this stream."));
    if (m.before.length) {
      var blk = el(doc, "div", "tv-before");
      blk.appendChild(el(doc, "div", "tv-note", "Before the chat record"));
      m.before.forEach(function (l) { blk.appendChild(renderLine(doc, l)); });
      host.appendChild(blk);
    }
    var stream = el(doc, "div", "tv-stream");
    m.stream.forEach(function (l) { stream.appendChild(renderLine(doc, l)); });
    if (!m.stream.length && !m.before.length) stream.appendChild(el(doc, "div", "tv-note", "Nothing has been said in this thread yet."));
    host.appendChild(stream);
    host.appendChild(el(doc, "div", "tv-composer", m.composer + " This view is read-only."));
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
    var bar = el(doc, "div", "tv-bar"), body = el(doc, "div", "tv-body-host");
    host.appendChild(bar); host.appendChild(body);

    function paintBar() {
      clear(bar);
      if (current) {
        var back = el(doc, "button", "tv-back", "All threads");
        back.setAttribute("type", "button");
        back.addEventListener("click", function () { api.back(); });
        bar.appendChild(back);
      }
    }
    function say(text) { last = ""; clear(body); body.appendChild(el(doc, "div", "tv-note", text)); }
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
          if (which) renderThread(doc, body, threadModel(j));
          else renderList(doc, body, listModel(j), function (id) { api.open(id); });
        });
      }).catch(function () { if (!stopped && g === gen) say("The thread record could not be read."); });
    }
    function loop() { if (!stopped && pollMs > 0) timer = setTimeout(function () { pull().then(loop); }, pollMs); }
    var api = {
      open: function (id) { current = id; gen++; last = ""; paintBar(); return pull(); },
      back: function () { current = null; gen++; last = ""; paintBar(); return pull(); },
      stop: function () { stopped = true; clearTimeout(timer); }
    };
    paintBar();
    pull().then(loop);
    return api;
  }

  var api = { listModel: listModel, threadModel: threadModel, renderList: renderList, renderThread: renderThread, mount: mount, composerWords: composerWords };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LevainThreadView = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
