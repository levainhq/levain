// levain board_view.js — the cockpit's read-only ready-queue board (C12).
//
// The audit window over the coordinator's work: six DERIVED columns (backlog, ready, running, waiting, needs-you, done)
// drawn from GET /cockpit/board.json. Read-only: no drag, no edit, no rename (C14 is a 1.1 fast-follow). Each card shows
// its journal line (who moved it, when, why). The one way to change anything is the "tell the coordinator" box, which
// sends the operator's WORDS through the existing drop route (POST /cockpit/coord/drop), with the drop sheet's own
// outbox idiom: the outbox id is picked once per words and kept in this browser before the post, so a resend after a lost
// reply files once. Every word from the record is set with textContent, never as markup.
(function (root) {
  "use strict";

  var BOARD_PATH = "/cockpit/board.json";
  var DROPS_PATH = "/cockpit/coord/drops.json";
  var DROP_PATH = "/cockpit/coord/drop";
  var WRITE_TOKEN_KEY = "levain_write_token";
  var ORDER = ["backlog", "ready", "running", "waiting", "needs-you", "done"];

  function stamp() { return new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000Z"); }
  function newOutboxId() {
    var b = new Uint8Array(16);
    root.crypto.getRandomValues(b);
    return "ph-" + Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
  }

  // The journal line of one card: "who · when · why", or the plain absence (a state change with no record is a defect).
  function journalLine(j) {
    if (!j) return "no journal record (as of read)";
    var move = j.from_category ? j.from_category + " → " + j.to_category : "added as " + j.to_category;
    var by = j.who ? String(j.who) : "unknown";
    if (j.source === "operator-proposal" && j.applied_by) by += " (applied by " + j.applied_by + ")";
    if (j.source === "correction") move += " (correction)";
    return "as of read: " + by + " · " + (j.iso || "no time") + " · " + move + " · " + (j.why || "no reason given");
  }

  function boardModel(body) {
    var cols = {};
    (body.columns || []).forEach(function (c) { cols[c.category] = c; });
    return {
      queue: body.queue || "none",
      note: body.note || "",
      hint: body.band_hint || "levain band",
      tell: body.tell && body.tell.offered === true ? { offered: true, why: "" }
        : { offered: false, why: String((body.tell && body.tell.why) || "the board does not say where a drop would go") },
      unreadable: Array.isArray(body.unreadable) ? body.unreadable.map(String) : [],
      columns: ORDER.map(function (name) {
        var c = cols[name] || { cards: [] };
        return {
          category: name,
          cards: (c.cards || []).map(function (k) {
            return {
              id: String(k.id), title: k.title == null ? "" : String(k.title), repo: k.repo || "",
              tier: k.tier || "", held: k.held_reason || "", number: k.number == null ? null : k.number,
              band: k.band === true, source: k.source || "", blocks: k.blocks || "",
              malformed: k.well_formed === false, unknownCategory: k.unknown_category || "",
              journal: k.band ? "" : journalLine(k.journal)
            };
          })
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

  function renderBoard(doc, host, model) {
    host.replaceChildren();
    var note = [];
    if (model.queue === "none") note.push("No queue yet: the coordinator has not filed any work.");
    if (model.queue === "unreadable") note.push("The queue could not be read; this is not an empty queue.");
    model.unreadable.forEach(function (u) { note.push("Could not read: " + u + "."); });
    if (model.note && model.queue !== "none") note.push(model.note);
    if (note.length) host.appendChild(el(doc, "p", "bd-note", note.join(" ")));
    var wrap = el(doc, "div", "bd-cols");
    model.columns.forEach(function (col) {
      var c = el(doc, "section", "bd-col");
      c.dataset.category = col.category;
      var h = el(doc, "h3", "bd-colhead", col.category);
      h.appendChild(el(doc, "span", "bd-count", " " + col.cards.length));
      c.appendChild(h);
      if (col.category === "needs-you") {
        c.appendChild(el(doc, "p", "bd-gloss", "This column is the Needs you band. Answer there (`" + model.hint + "`)."));
      }
      if (!col.cards.length) c.appendChild(el(doc, "p", "bd-empty", "nothing"));
      col.cards.forEach(function (k) {
        var card = el(doc, "article", "bd-card");
        card.dataset.id = k.id;
        card.appendChild(el(doc, "div", "bd-title", (k.number != null ? k.number + ". " : "") + k.title));
        var meta = [k.repo, k.tier, k.source, k.id].filter(Boolean).join(" · ");
        if (meta) card.appendChild(el(doc, "div", "bd-meta", meta));
        if (k.held) card.appendChild(el(doc, "div", "bd-held", "held: " + k.held));
        if (k.malformed) card.appendChild(el(doc, "div", "bd-held", "not well formed (no done line or tier)"));
        if (k.unknownCategory) card.appendChild(el(doc, "div", "bd-held", "unknown category: " + k.unknownCategory));
        if (!k.band) card.appendChild(el(doc, "div", "bd-journal", k.journal));
        c.appendChild(card);
      });
      wrap.appendChild(c);
    });
    host.appendChild(wrap);
  }

  // The "tell the coordinator" box: words only, through POST /cockpit/coord/drop.
  function mountTell(doc, host, opts) {
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var store = opts.storage || (function () { try { return root.localStorage; } catch (e) { return null; } })();
    var PEND_KEY = "levain-board-drop-pending";
    var box = el(doc, "div", "bd-tell");
    box.appendChild(el(doc, "label", "bd-gloss", "Tell the coordinator (your words; the coordinator moves the cards)"));
    var words = el(doc, "textarea");
    words.rows = 3;
    words.setAttribute("aria-label", "tell the coordinator");
    var send = el(doc, "button", "dev-btn", "Send");
    send.type = "button";
    var say = el(doc, "p", "bd-say");
    var gate = el(doc, "p", "bd-note");
    box.append(gate, words, send, say);
    host.appendChild(box);
    var offered = false;
    function offer(on, why) {       // the box is offered only when the drop writes to the directory the board reads
      offered = on === true;
      gate.textContent = offered ? "" : "Telling the coordinator is off here: " + String(why || "the board has not said") + ".";
      gate.hidden = offered;
      words.hidden = send.hidden = !offered;
    }
    offer(false, "reading the board");
    var oid = newOutboxId();
    var working = false;

    function keep(req) { try { if (store) store.setItem(PEND_KEY, JSON.stringify(req)); } catch (e) { /* this visit only */ } }
    function drop0() { try { if (store) store.removeItem(PEND_KEY); } catch (e) { /* nothing kept */ } }
    function kept() {
      try {
        var v = JSON.parse((store && store.getItem(PEND_KEY)) || "null");
        return v && typeof v.outbox_id === "string" && typeof v.words === "string" ? v : null;
      } catch (e) { return null; }
    }
    function post(req) {
      var h = headers(); h["Content-Type"] = "application/json";
      return f(DROP_PATH, { method: "POST", headers: h, body: JSON.stringify(req) }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) { return { ok: r.ok, status: r.status, body: b }; });
      });
    }
    function deliver(req) {
      if (!offered) { say.textContent = "Not sent: telling the coordinator is off here."; return Promise.resolve(); }
      working = send.disabled = words.disabled = true;
      keep(req);
      var unsure = function (why) {
        working = send.disabled = words.disabled = false;
        send.textContent = "Send the same words again";
        say.textContent = "Not sure it was delivered (" + why + "); send again, unchanged, to find out (changed words are sent as a new request).";
        words.value = req.words;
        oid = req.outbox_id;
      };
      return post(req).then(function (a) {
        if (a.ok) {
          drop0();
          working = send.disabled = words.disabled = false;
          send.textContent = "Send";
          words.value = "";
          oid = newOutboxId();
          var d = (a.body && a.body.drop) || {};
          say.textContent = (a.body && a.body.created === false ? "already delivered" : "delivered") + (d.drop_id ? " as " + d.drop_id : "") + ".";
          return;
        }
        if (a.status >= 400 && a.status < 500) {
          drop0();
          oid = newOutboxId();       // a refused request was never filed
          working = send.disabled = words.disabled = false;
          say.textContent = "Not delivered: " + String((a.body && a.body.message) || ("HTTP " + a.status)) + ".";
          return;
        }
        unsure("HTTP " + a.status);
      }, function () { unsure("the page could not reach the cockpit"); });
    }
    // the drop sheet's identity step: the listing says which store answers; the drop is bound to it
    function storeId() {
      return f(DROPS_PATH, { cache: "no-store", headers: headers() }).then(function (r) {
        if (!r.ok) return { fail: r.status === 403 ? "this page holds no token" : r.status === 404 ? "this cockpit hosts no drops" : "HTTP " + r.status };
        return r.json().then(function (b) {
          // an unreadable store is not "no store": a drop is posted only on a verified identity read
          if (!b || b.coord_store_read !== true) return { fail: String((b && b.coord_note) || "the coordinator store could not be read") };
          return { id: typeof b.coord_store_id === "string" ? b.coord_store_id : null };
        }, function () { return { fail: "the cockpit's answer could not be read" }; });
      }, function () { return { fail: "the page could not reach the cockpit" }; });
    }
    send.addEventListener("click", function () {
      if (working || !offered) return;
      var pend = kept();
      if (pend && pend.words === words.value) { deliver(pend); return; }
      if (!words.value.trim()) { say.textContent = "Type your words first."; return; }
      var w = words.value;
      // changed words are a new request: the old pending one (never reconciled) is abandoned and a fresh id minted
      if (pend) { drop0(); oid = newOutboxId(); }
      working = send.disabled = words.disabled = true;      // locked BEFORE the first await: a second click does nothing
      storeId().then(function (s) {
        if (s.fail) {
          working = send.disabled = words.disabled = false;
          say.textContent = "Not sent: " + s.fail + ".";
          return;
        }
        return deliver({ words: w, outbox_id: oid, sent_at: stamp(), store_id: s.id });
      });
    });
    var left = kept();
    if (left) {       // an earlier visit's drop whose answer never came: shown, sent again unchanged on a tap
      words.value = left.words;
      oid = left.outbox_id;
      say.textContent = "Words kept from an earlier visit; send them again (they file once).";
    }
    return { box: box, offer: offer };
  }

  function mount(host, opts) {
    opts = opts || {};
    var f = opts.fetch || root.fetch.bind(root);
    var headers = opts.headers || function () { return {}; };
    var pollMs = opts.pollMs == null ? 5000 : opts.pollMs;
    var doc = host.ownerDocument;
    var view = el(doc, "div", "bd-view");
    var tell = el(doc, "div", "bd-tellhost");
    host.append(view, tell);
    var tellBox = mountTell(doc, tell, opts);
    var timer = null, stopped = false;
    function pull() {
      return f(BOARD_PATH, { cache: "no-store", headers: headers() }).then(function (res) {
        if (res.status === 403) { view.textContent = "The board needs this page's write token."; return; }
        if (!res.ok) { view.textContent = "The board could not be read (HTTP " + res.status + ")."; return; }
        return res.json().then(function (b) { if (stopped) return; var m = boardModel(b); renderBoard(doc, view, m); tellBox.offer(m.tell.offered, m.tell.why); });
      }).catch(function () { if (!stopped) view.textContent = "The board could not be reached."; });
    }
    function loop() { if (!stopped && pollMs > 0) timer = setTimeout(function () { pull().then(loop); }, pollMs); }
    pull().then(loop);
    return { stop: function () { stopped = true; if (timer) clearTimeout(timer); }, pull: pull };
  }

  var api = { boardModel: boardModel, journalLine: journalLine, renderBoard: renderBoard, mount: mount };
  root.LevainBoardView = api;

  // The Board tab's zone (the thread view's mounting idiom): hidden on a page holding no write token; polls only while the
  // Board tab is shown and the page visible.
  function token() { try { return root.localStorage.getItem(WRITE_TOKEN_KEY) || ""; } catch (_) { return ""; } }
  function start() {
    var doc = root.document;
    if (!doc || !doc.querySelector("main.frame")) return;
    var section = el(doc, "section", "ck-zone bd-zone");
    section.dataset.zone = "ready-board";
    section.dataset.modes = "board";
    section.hidden = true;
    var head = el(doc, "div", "zone-head");
    head.appendChild(el(doc, "div", "zone-title", "Ready queue"));
    head.appendChild(el(doc, "span", "zone-gloss", "read-only: the coordinator moves the cards, you tell it in words"));
    section.appendChild(head);
    var grid = el(doc, "div", "ck-zgrid bd-grid");
    var panel = el(doc, "div", "panel k-list bd-panel");
    panel.dataset.modes = "board";
    var ph = el(doc, "div", "phead");
    ph.appendChild(el(doc, "h2", null, "Board"));
    panel.appendChild(ph);
    var host = el(doc, "div", "pbody bd-body");
    panel.appendChild(host);
    grid.appendChild(panel);
    section.appendChild(grid);
    doc.querySelector("main.frame").appendChild(section);
    var view = null;
    function sync() {
      var want = !!token() && doc.body.dataset.mode === "board" && doc.visibilityState !== "hidden";
      section.hidden = !token();
      if (want && !view) {
        host.replaceChildren();
        var T = root.LevainCockpitTransport;
        view = mount(host, { headers: function () { return T ? T.headers() : {}; } });
      } else if (!want && view) { view.stop(); view = null; }
    }
    new root.MutationObserver(sync).observe(doc.body, { attributes: true, attributeFilter: ["data-mode"] });
    doc.addEventListener("visibilitychange", sync);
    root.addEventListener("storage", function (e) { if (e.key === WRITE_TOKEN_KEY) sync(); });
    root.addEventListener("levain:write-token-change", sync);
    sync();
  }
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : this);
