// levain board_view.js — the cockpit's read-only ready-queue board (C12).
//
// The audit window over the coordinator's work: six DERIVED columns (backlog, ready, running, waiting, needs-you, done)
// drawn from GET /cockpit/board.json. The Board tab lists the cards grouped under those column headings; the six columns
// side by side are drawn (by renderBoard) in the panel's ⤢ focus modal. Read-only: no drag, no edit, no rename (C14 is a 1.1 fast-follow). Each card shows
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

  // The journal line of one card: "who · when · why". A card with no record draws no line: absence is the ordinary case for a
  // card nothing here ever moved (a projected seat or CI row, a downstream's own cards), so a line saying so reads as a fault.
  function journalLine(j) {
    if (!j) return "";
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
              done: k.done_line == null ? "" : String(k.done_line),
              dependsOn: Array.isArray(k.depends_on)
                ? k.depends_on.filter(function (x) { return typeof x === "string" || typeof x === "number"; }).slice(0, 50).map(String) : [],
              journal: k.band ? "" : journalLine(k.journal)
            };
          })
        };
      })
    };
  }

  // The filter box's match, shared with the cockpit's own filter (case and accent folded, every term must hit): the text a
  // reader sees on a card, so what they type is what they can find.
  function fold(s) { return String(s).normalize("NFD").replace(/\p{M}/gu, "").toLowerCase(); }
  function termsOf(q) { return fold(q || "").split(/\s+/).filter(Boolean); }
  function hayOf(col, k) {
    return fold([col.category, k.number != null ? k.number : "", k.title, k.repo, k.tier, k.source, k.id, k.held, k.blocks,
      k.unknownCategory, k.done && "done when " + k.done, k.dependsOn.length ? "after " + k.dependsOn.join(" ") : "",
      k.held && "held " + k.held, k.malformed ? "not well formed" : "", k.journal].filter(function (x) { return x !== "" && x != null; }).join(" "));
  }
  // The model narrowed to the cards a query matches (the columns stay, so a column with no match still says so), with the
  // counts a count label needs: `total` over the whole board, `shown` after the filter. No terms: the model itself, whole.
  function filterModel(model, query) {
    var terms = termsOf(query);
    var total = 0;
    model.columns.forEach(function (c) { total += c.cards.length; });
    if (!terms.length) return { model: model, total: total, shown: total, filtered: false };
    var shown = 0;
    var columns = model.columns.map(function (c) {
      var cards = c.cards.filter(function (k) {
        var h = hayOf(c, k);
        return terms.every(function (t) { return h.indexOf(t) >= 0; });
      });
      shown += cards.length;
      return Object.assign({}, c, { cards: cards });
    });
    var out = {};
    Object.keys(model).forEach(function (key) { out[key] = model[key]; });
    out.columns = columns;
    return { model: out, total: total, shown: shown, filtered: true };
  }
  function countLabel(f) { return f.filtered ? f.shown + " of " + f.total : f.total + (f.total === 1 ? " card" : " cards"); }

  function el(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // What a card is FOR and what it waits on: its done line (the point of a work card) and the ids it comes after, each drawn only
  // when the card has one, as text.
  function drawPlan(doc, host, k) {
    if (k.done) host.appendChild(el(doc, "div", "bd-done", "done when: " + k.done));
    if (k.dependsOn.length) host.appendChild(el(doc, "div", "bd-deps", "after: " + k.dependsOn.join(", ")));
  }

  // The needs-you column's one sentence: said by the grouped tab and the six columns alike, so the two cannot drift.
  function needsYouNote(model) { return "This column is the Needs you band. Answer there (`" + model.hint + "`)."; }

  // The queue's own state, said before any column (shared by the grouped tab and the modal's columns).
  function drawNote(doc, host, model) {
    var note = [];
    if (model.queue === "none") note.push("No queue yet: the coordinator has not filed any work.");
    if (model.queue === "unreadable") note.push("The queue could not be read; this is not an empty queue.");
    model.unreadable.forEach(function (u) { note.push("Could not read: " + u + "."); });
    if (model.note && model.queue !== "none") note.push(model.note);
    if (note.length) host.appendChild(el(doc, "p", "bd-note", note.join(" ")));
  }

  // The Board tab's body: the cards GROUPED UNDER COLUMN HEADINGS (one heading per column, in the model's order, each with its
  // count), the cards under it as list rows. The six-column body is renderBoard's, drawn in the focus modal.
  function renderGrouped(doc, host, model) {
    host.replaceChildren();
    drawNote(doc, host, model);
    var wrap = el(doc, "div", "bd-groups");
    model.columns.forEach(function (col) {
      var h = el(doc, "div", "grp-head", col.category + " " + col.cards.length);
      h.dataset.category = col.category;
      wrap.appendChild(h);
      if (col.category === "needs-you") {
        wrap.appendChild(el(doc, "p", "bd-gloss", needsYouNote(model)));
      }
      if (!col.cards.length) wrap.appendChild(el(doc, "p", "bd-empty", "nothing"));
      col.cards.forEach(function (k) {
        var row = el(doc, "div", "bd-row");
        row.dataset.id = k.id;
        row.appendChild(el(doc, "div", "bd-title", (k.number != null ? k.number + ". " : "") + k.title));
        var meta = [k.repo, k.tier, k.source, k.id].filter(Boolean).join(" · ");
        if (meta) row.appendChild(el(doc, "div", "bd-meta", meta));
        drawPlan(doc, row, k);
        if (k.held) row.appendChild(el(doc, "div", "bd-held", "held: " + k.held));
        if (k.malformed) row.appendChild(el(doc, "div", "bd-held", "not well formed (no done line or tier)"));
        if (k.unknownCategory) row.appendChild(el(doc, "div", "bd-held", "unknown category: " + k.unknownCategory));
        if (!k.band && k.journal) row.appendChild(el(doc, "div", "bd-journal", k.journal));
        wrap.appendChild(row);
      });
    });
    host.appendChild(wrap);
  }

  function renderBoard(doc, host, model) {
    host.replaceChildren();
    drawNote(doc, host, model);
    var wrap = el(doc, "div", "bd-cols");
    model.columns.forEach(function (col) {
      var c = el(doc, "section", "bd-col");
      c.dataset.category = col.category;
      var h = el(doc, "h3", "bd-colhead", col.category);
      h.appendChild(el(doc, "span", "bd-count", " " + col.cards.length));
      c.appendChild(h);
      if (col.category === "needs-you") {
        c.appendChild(el(doc, "p", "bd-gloss", needsYouNote(model)));
      }
      if (!col.cards.length) c.appendChild(el(doc, "p", "bd-empty", "nothing"));
      col.cards.forEach(function (k) {
        var card = el(doc, "article", "bd-card");
        card.dataset.id = k.id;
        card.appendChild(el(doc, "div", "bd-title", (k.number != null ? k.number + ". " : "") + k.title));
        var meta = [k.repo, k.tier, k.source, k.id].filter(Boolean).join(" · ");
        if (meta) card.appendChild(el(doc, "div", "bd-meta", meta));
        drawPlan(doc, card, k);
        if (k.held) card.appendChild(el(doc, "div", "bd-held", "held: " + k.held));
        if (k.malformed) card.appendChild(el(doc, "div", "bd-held", "not well formed (no done line or tier)"));
        if (k.unknownCategory) card.appendChild(el(doc, "div", "bd-held", "unknown category: " + k.unknownCategory));
        if (!k.band && k.journal) card.appendChild(el(doc, "div", "bd-journal", k.journal));
        c.appendChild(card);
      });
      wrap.appendChild(c);
    });
    host.appendChild(wrap);
  }

  // The composer prefill: `/#board&compose=re%20<card id>%3A%20` (the Board tab, the box's words started with "re <card id>: ").
  // Plain text only: it is decoded by URLSearchParams (which never throws on a malformed %), stripped of control characters,
  // cut at COMPOSE_MAX, and set as the textarea's VALUE. It reads no storage, posts nothing, and holds no token; the
  // operator edits it and sends it through the one drop route like any other words.
  var COMPOSE_MAX = 300;
  function composePrefill(hash) {
    try {
      var v = new URLSearchParams(String(hash || "").replace(/^#/, "")).get("compose");
      if (v == null) return "";
      v = v.replace(/[\u0000-\u001f\u007f]/g, " ");
      var cps = Array.from(v);       // by code points: a cut never splits a surrogate pair
      return cps.length > COMPOSE_MAX ? cps.slice(0, COMPOSE_MAX).join("") : v;
    } catch (e) { return ""; }
  }
  // once the box is offered, the address no longer carries it (a reload does not refill words the operator sent or cleared).
  // Only the compose segment goes; every other fragment part stays byte-identical.
  function forgetCompose() {
    try {
      if (!root.history || !root.location) return;
      var rest = String(root.location.hash || "").replace(/^#/, "").split("&").filter(function (s) {
        return s !== "compose" && s.indexOf("compose=") !== 0;
      }).join("&");
      root.history.replaceState(null, "", root.location.pathname + root.location.search + (rest ? "#" + rest : ""));
    } catch (e) { /* the address stays */ }
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
      if (offered && forgetPending) {   // the field is shown now: the link is spent (used, or beaten by kept words)
        forgetPending = false;
        forgetCompose();
      }
      if (offered && wantFocus) {       // focus it, cursor at the end of the prefill, unless the operator is already in another field
        wantFocus = false;
        var ae = doc.activeElement;
        if (!ae || ae === doc.body || ae === doc.documentElement) {
          try { words.focus(); words.setSelectionRange(words.value.length, words.value.length); } catch (e) { /* focus is a convenience */ }
        }
      }
    }
    var wantFocus = false, forgetPending = false;
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
    // a link's `compose=` fragment part starts the words (flow's board copies/links, it never posts); never over kept words.
    // Used or beaten by kept words, it is forgotten once the box is offered (never while hidden: the words would be lost).
    var pre = composePrefill(opts.hash != null ? opts.hash : (root.location && root.location.hash));
    if (pre) forgetPending = true;
    if (left) {       // an earlier visit's drop whose answer never came: shown, sent again unchanged on a tap
      words.value = left.words;
      oid = left.outbox_id;
      say.textContent = "Words kept from an earlier visit; send them again (they file once).";
    } else if (pre) { words.value = pre; wantFocus = true; }
    // following a compose link inside an already-open cockpit (no reload) fills the same way, when nothing would be overwritten
    function onHash() {
      if (working || words.value !== "" || kept()) return;
      var pre = composePrefill(root.location && root.location.hash);
      if (!pre) return;
      words.value = pre; wantFocus = true; forgetPending = true;
      if (offered) offer(true);
    }
    if (root.addEventListener) root.addEventListener("hashchange", onHash);
    return { box: box, offer: offer, dispose: function () { if (root.removeEventListener) root.removeEventListener("hashchange", onHash); } };
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
    // The page's bus: the last good model and the watchers belong to the PAGE, shared by every view it mounts (the Board tab
    // stops a view when it is left or hidden and mounts a new one when it is back; the ⤢ modal must outlive that). `bus.model`
    // is the last good pull's model, cleared on a failed one; `bus.failed` says the latest read did not succeed.
    var bus = opts.bus || newBus();
    function pull() {
      // a stopped view takes no sequence number: its request must not invalidate the mounted view's in-flight one
      if (stopped) return Promise.resolve();
      // a per-bus sequence number, taken at request start: only the LATEST request's answer (good or failed) is applied, so a
      // slow older response never overwrites a newer one (the poll and a caller's `pull()` can overlap)
      var seq = bus.seq = (bus.seq || 0) + 1;
      var current = function () { return !stopped && seq === bus.seq; };
      // the one place a read fails: the model is cleared, the watchers told, and the Send box retired with the words the operator
      // already typed left in the field (the board it would have talked to is no longer readable)
      var fail = function (line) {
        if (!current()) return;
        view.textContent = line;
        busFail(bus);
        tellBox.offer(false, "the board could not be read just now");
      };
      return f(BOARD_PATH, { cache: "no-store", headers: headers() }).then(function (res) {
        if (res.status === 403) return fail("The board needs this page's write token.");
        if (!res.ok) return fail("The board could not be read (HTTP " + res.status + ").");
        return res.json().then(function (b) {
          if (!current()) return;
          var m = bus.model = boardModel(b);
          bus.failed = false;
          renderGrouped(doc, view, m);
          tellBox.offer(m.tell.offered, m.tell.why);
          busTell(bus, m, false);
        });
      }).catch(function () { fail("The board could not be reached."); });
    }
    function loop() { if (!stopped && pollMs > 0) timer = setTimeout(function () { pull().then(loop); }, pollMs); }
    pull().then(loop);
    return {
      // stopping a view drops none of the page's watchers: the next view's pull reaches them (`watch` is used by the tests only;
      // the page's one production subscriber, the modal's, ends itself when the modal is gone)
      stop: function () { stopped = true; if (timer) clearTimeout(timer); tellBox.dispose(); },
      pull: pull,
      model: function () { return bus.model; },
      // `fn(model, failed)` after every pull: `fn(model, false)` on a good one, `fn(null, true)` on a failed one; the returned function stops it
      watch: function (fn) { return busWatch(bus, fn); }
    };
  }

  function newBus() { return { model: null, failed: false, lacked: false, seq: 0, watchers: [] }; }
  function busWatch(bus, fn) { bus.watchers.push(fn); return function () { bus.watchers = bus.watchers.filter(function (w) { return w !== fn; }); }; }
  // one watcher's throw touches neither the others nor the board the pull just drew
  function busTell(bus, m, failed) { bus.watchers.slice().forEach(function (w) { try { w(m, failed); } catch (_) { /* a watcher's fault stays its own */ } }); }
  // a read that did not succeed (a 403, a 5xx, no network, the token removed): what was last read is no longer the board
  function busFail(bus) { bus.model = null; bus.failed = true; busTell(bus, null, true); }
  // The write token's presence, as sync() sees it. Gone: what was read is not the operator's to see, so the bus fails (once; an
  // open modal says so). Back after a gone: the failure it caused is cleared and the watchers told there is no model yet (the
  // modal says "not read yet", never the stale line); the next mounted view pulls. A failure from a bad READ is left alone.
  function busToken(bus, has) {
    if (!has) {
      if (!bus.failed) { busFail(bus); bus.lacked = true; }       // `lacked` names a failure THIS gone-token caused
    } else if (bus.lacked) {
      bus.lacked = false;
      bus.failed = false;
      busTell(bus, bus.model, false);
    }
  }

  var api = { boardModel: boardModel, filterModel: filterModel, countLabel: countLabel, journalLine: journalLine, renderBoard: renderBoard, renderGrouped: renderGrouped, mount: mount, busToken: busToken };
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
    var bus = newBus();       // shared by every view this page mounts (see mount)
    // ⤢ "focus this panel": the six columns, drawn by renderBoard (the same renderer), in the dashboard's own focus modal. It is
    // drawn only when the dashboard exports the way in (a page without it has no modal to open), in the panel head and never in
    // the mounted host (the tell box holds the one button there). The modal has no tell box. Each pull repaints it: the columns on a good one, one plain line on a failed one.
    var D = root.LevainDashboard;
    if (D && typeof D.openCustom === "function") {
      var expand = el(doc, "button", "pexpand");
      expand.type = "button";
      expand.title = "focus this panel";
      expand.setAttribute("aria-label", "expand Board to a focus view");
      expand.appendChild(el(doc, "span", "pexpand-ico", "⤢"));
      var modalHost = null, unwatch = null;
      // `failed`: the latest read did not succeed, so the columns give way to one plain line (never the last authorized queue)
      var paint = function (m, failed) {
        modalHost.replaceChildren();
        if (failed) modalHost.appendChild(el(doc, "p", "bd-note", "the board could not be read just now"));
        else if (m) renderBoard(doc, modalHost, m);
        else modalHost.appendChild(el(doc, "p", "bd-note", "The board has not been read yet."));
      };
      expand.addEventListener("click", function () {
        if (unwatch) unwatch();
        unwatch = null;
        D.openCustom({
          id: "ready-board", title: "Board", live: true, opener: expand,
          build: function () {      // one host per build (a dashboard refresh builds again); the pull paints the latest one
            modalHost = el(doc, "div", "bd-modal");
            paint(bus.model, bus.failed);
            return modalHost;
          }
        });
        // subscribed to the page's bus, not to a view: a view stopped and remounted (the tab hidden and back) leaves this
        // subscription standing. It ends when the modal is gone (seen on the next notice). A host detached for a moment is not
        // "gone": the dashboard's rebuild (buildModal) is synchronous, so no notice can land between the old host and the new.
        var mine = busWatch(bus, function (m, failed) {
          if (modalHost && doc.body.contains(modalHost)) paint(m, failed);
          else mine();
        });
        unwatch = mine;
      });
      ph.appendChild(expand);
    }
    function sync() {
      var want = !!token() && doc.body.dataset.mode === "board" && doc.visibilityState !== "hidden";
      section.hidden = !token();
      busToken(bus, !!token());       // no token: what was read is not the operator's to see (an open modal says so); back: that is cleared
      if (want && !view) {
        host.replaceChildren();
        var T = root.LevainCockpitTransport;
        view = mount(host, { bus: bus, headers: function () { return T ? T.headers() : {}; } });
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
