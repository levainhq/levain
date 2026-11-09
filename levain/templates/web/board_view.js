// levain board_view.js — the painters of the cockpit's board kind (C12).
//
// The audit window over the coordinator's work: six DERIVED columns (backlog, ready, running, waiting, needs-you, done) from a
// board value (levain's own, or a provider's of the same shape). The panel lists the cards grouped under those column headings;
// the six columns side by side are drawn (by renderBoard) in the panel's ⤢ focus modal. Read-only: no drag, no edit, no rename.
// Each card shows its journal line (who moved it, when, why). The one way to talk about a card is a drop: every card carries a
// "Drop about this card" button that puts `About card <id> (<title>): ` in the cockpit's drop box (no route of its own here).
// Every word from the record is set with textContent, never as markup.
(function (root) {
  "use strict";

  var ORDER = ["backlog", "ready", "running", "waiting", "needs-you", "done"];

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
      // false only when levain's own board says its drop route files to another coordinator than the one this board reads; a
      // provider's board is the coordinator's own work and says nothing, which reads as true
      dropHere: body.drop_here !== false,
      unreadable: Array.isArray(body.unreadable) ? body.unreadable.map(String) : [],
      columns: ORDER.map(function (name) {
        var c = cols[name] || { cards: [] };
        return {
          category: name,
          // a band row the desk alone owes (section desk, not carried over to him) is not his: left out as the Needs strip leaves
          // it out (dashboard_core needsItems) and the app badge does (webpush/needs.py). Both read the host's own two fields
          cards: (c.cards || []).filter(function (k) { return !(k.band === true && k.section === "desk" && !k.carried); }).map(function (k) {
            return {
              id: String(k.id), title: k.title == null ? "" : String(k.title), repo: k.repo || "",
              tier: k.tier || "", held: k.held_reason || "", number: k.number == null ? null : k.number,
              band: k.band === true, later: k.band === true && typeof k.later === "string" && /^\d{4}-\d{2}-\d{2}$/.test(k.later) ? k.later : "",
              laterLabel: k.band === true && typeof k.later === "string" && /^\d{4}-\d{2}-\d{2}$/.test(k.later) && typeof k.later_label === "string" ? k.later_label.slice(0, 80) : "", source: k.source || "", blocks: k.blocks || "",
              malformed: k.well_formed === false, unknownCategory: k.unknown_category || "",
              done: k.done_line == null ? "" : String(k.done_line),
              dependsOn: Array.isArray(k.depends_on)
                ? k.depends_on.filter(function (x) { return (typeof x === "string" && x !== "") || typeof x === "number"; }).slice(0, 50).map(String) : [],
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
      k.unknownCategory, k.later && "later", k.later && k.laterLabel, k.later, k.done && "done when: " + k.done, k.dependsOn.length ? "after: " + k.dependsOn.join(", ") : "",
      k.held && "held: " + k.held, k.malformed ? "not well formed" : "", k.journal].filter(function (x) { return x !== "" && x != null; }).join(" "));
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

  // The words a drop about one card starts with; the cursor goes after them.
  function dropWords(k) { return "About card " + k.id + (k.title ? " (" + k.title + ")" : "") + ": "; }

  // The card's one way to talk back: the cockpit's drop box, filled with the words above. Without the cockpit's opener (a page
  // that is not the cockpit) the button is not drawn: it would do nothing.
  function dropButton(doc, k) {
    var D = root.LevainDashboard;
    if (!D || typeof D.composeDrop !== "function") return null;
    var b = el(doc, "button", "bd-drop", "Drop about this card");
    b.type = "button";
    b.dataset.dropAbout = k.id;
    b.addEventListener("click", function () { D.composeDrop(dropWords(k)); });
    return b;
  }
  function drawDrop(doc, host, k, model) {
    if (model && model.dropHere === false) return;      // the drop box would file to a coordinator that is not this board's
    var b = dropButton(doc, k);
    if (b) host.appendChild(b);
  }

  // What an empty column says. ONE rule: a column is "nothing" only when every source that feeds it was read. The sources, from the
  // producer (cockpit/board.py, coordinator/ready.py): the store feeds every column but needs-you ("coordinator" in unreadable, or the
  // queue state is anything but "ok"/"none": fail closed); the seat roster adds running and waiting ("seats"); the band is needs-you's
  // alone ("band"). The journal only annotates cards, so it never empties a column.
  var COLUMN_SOURCES = { "needs-you": ["band"], running: ["coordinator", "seats"], waiting: ["coordinator", "seats"] };
  function sourceMissing(model, src) {
    if (!Array.isArray(model.unreadable)) return true;      // the producer always sends the list: a payload without it cannot say what was read
    var un = model.unreadable;
    if (src === "coordinator" && model.queue !== "ok" && model.queue !== "none") return true;
    return un.indexOf(src) >= 0;
  }
  function emptyText(model, col) {
    var bad = (COLUMN_SOURCES[col.category] || ["coordinator"]).some(function (s) { return sourceMissing(model, s); });
    return bad ? "unknown (could not read)" : "nothing";
  }

  // A snoozed question is one collapsed line: its label (number and day), then the first words of the title.
  function laterWords(t) { t = String(t == null ? "" : t); return t.length > 60 ? t.slice(0, 57) + "..." : t; }
  // the line's own words wrap around the LATER tag; the tag itself (LATER and its day) stays on one line
  function laterTitle(doc, k) {
    var t = el(doc, "div", "bd-title");
    t.appendChild(el(doc, "span", "later-tag", "LATER: " + (k.laterLabel || k.later)));
    t.appendChild(el(doc, "span", "", " " + laterWords(k.title)));
    return t;
  }

  // The needs-you column's one sentence: said by the grouped tab and the six columns alike, so the two cannot drift.
  function needsYouNote(model) { return "This column is the Needs you band. Answer there (`" + model.hint + "`)."; }

  // The queue's own state, said before any column (shared by the grouped tab and the modal's columns).
  function drawNote(doc, host, model) {
    var note = [];
    if (model.queue === "none") note.push("No queue yet: the desk has not filed any work.");
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
      if (!col.cards.length) wrap.appendChild(el(doc, "p", "bd-empty", emptyText(model, col)));
      col.cards.forEach(function (k) {
        var row = el(doc, "div", "bd-row" + (k.later ? " bd-later" : ""));
        row.dataset.id = k.id;
        if (k.later) { row.appendChild(laterTitle(doc, k)); drawDrop(doc, row, k, model); wrap.appendChild(row); return; }
        row.appendChild(el(doc, "div", "bd-title", (k.number != null ? k.number + ". " : "") + (String(k.title == null ? "" : k.title).replace(/^\s*DESK OWES:\s*/i, "") || String(k.title == null ? "" : k.title))));
        var meta = [k.repo, k.tier, k.source, k.id].filter(Boolean).join(" · ");
        if (meta) row.appendChild(el(doc, "div", "bd-meta", meta));
        drawPlan(doc, row, k);
        if (k.held) row.appendChild(el(doc, "div", "bd-held", "held: " + k.held));
        if (k.malformed) row.appendChild(el(doc, "div", "bd-held", "not well formed (no done line or tier)"));
        if (k.unknownCategory) row.appendChild(el(doc, "div", "bd-held", "unknown category: " + k.unknownCategory));
        if (!k.band && k.journal) row.appendChild(el(doc, "div", "bd-journal", k.journal));
        drawDrop(doc, row, k, model);
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
      if (!col.cards.length) c.appendChild(el(doc, "p", "bd-empty", emptyText(model, col)));
      col.cards.forEach(function (k) {
        var card = el(doc, "article", "bd-card" + (k.later ? " bd-later" : ""));
        card.dataset.id = k.id;
        if (k.later) { card.appendChild(laterTitle(doc, k)); drawDrop(doc, card, k, model); c.appendChild(card); return; }
        card.appendChild(el(doc, "div", "bd-title", (k.number != null ? k.number + ". " : "") + (String(k.title == null ? "" : k.title).replace(/^\s*DESK OWES:\s*/i, "") || String(k.title == null ? "" : k.title))));
        var meta = [k.repo, k.tier, k.source, k.id].filter(Boolean).join(" · ");
        if (meta) card.appendChild(el(doc, "div", "bd-meta", meta));
        drawPlan(doc, card, k);
        if (k.held) card.appendChild(el(doc, "div", "bd-held", "held: " + k.held));
        if (k.malformed) card.appendChild(el(doc, "div", "bd-held", "not well formed (no done line or tier)"));
        if (k.unknownCategory) card.appendChild(el(doc, "div", "bd-held", "unknown category: " + k.unknownCategory));
        if (!k.band && k.journal) card.appendChild(el(doc, "div", "bd-journal", k.journal));
        drawDrop(doc, card, k, model);
        c.appendChild(card);
      });
      wrap.appendChild(c);
    });
    host.appendChild(wrap);
  }

  var api = { boardModel: boardModel, filterModel: filterModel, countLabel: countLabel, journalLine: journalLine, renderBoard: renderBoard, renderGrouped: renderGrouped, dropWords: dropWords };
  root.LevainBoardView = api;
})(typeof window !== "undefined" ? window : this);
