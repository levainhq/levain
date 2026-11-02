// Behavioural harness for board_view.js's painters (run by tests/test_cockpit_board.py under node): a minimal fake DOM;
// prints one JSON object of observations.
const fs = require("fs");
const vm = require("vm");
function node(tag) {
  return { tag, children: [], dataset: {}, listeners: {}, hidden: false, disabled: false, value: "", textContent: "",
    className: "", ownerDocument: null, setAttribute() {}, appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); }, replaceChildren() { this.children = []; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    click() { (this.listeners.click || []).forEach((f) => f()); } };
}
const doc = { createElement: node };
globalThis.window = globalThis;
const load = () => vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
const walk = (n, out = []) => { out.push(n); (n.children || []).forEach((c) => walk(c, out)); return out; };
const text = (n) => n.textContent + n.children.map(text).join("");
const BODY = { queue: "ok", columns: [
  { category: "backlog", cards: [{ id: "levain:c", title: "Later C", repo: "levain", tier: "sonnet", held_reason: "depends on levain:a", journal: { who: "desk", iso: "t", to_category: "backlog", why: "queued it." } }] },
  { category: "running", cards: [{ id: "levain:a", title: "Land A", repo: "levain", tier: "sonnet", journal: { who: "seat-9", iso: "t", from_category: "ready", to_category: "running", why: "took it." } }] },
  { category: "needs-you", cards: [{ id: "q-1", title: "Ship the sweep fix", number: 3, band: true }] }] };

// The grouped list and the six-column body over ONE model: what each draws.
function grouped(V) {
  const model = V.boardModel(BODY);
  const g = node("div"), c = node("div");
  V.renderGrouped(doc, g, model);
  V.renderBoard(doc, c, model);
  const gAll = walk(g), cAll = walk(c);
  const heads = gAll.filter((n) => n.className === "grp-head").map((n) => n.textContent);
  const rows = gAll.filter((n) => n.className === "bd-row");
  return {
    heads, rowIds: rows.map((n) => n.dataset.id), rowText: rows.map(text),
    glossGrouped: gAll.filter((n) => n.className === "bd-gloss").map(text), glossBoard: cAll.filter((n) => n.className === "bd-gloss").map(text),
    groupedColsBody: gAll.filter((n) => n.className === "bd-cols" || n.className === "bd-col").length,
    boardCols: cAll.filter((n) => n.className === "bd-col").map((n) => n.dataset.category),
    boardColsBody: cAll.filter((n) => n.className === "bd-cols").length,
  };
}
// The filter box's match and count, and the journal line's absence: over BODY (3 cards: backlog, running, needs-you).
function filtering(V) {
  const m = V.boardModel(BODY);
  const pick = (q) => { const f = V.filterModel(m, q); return { label: V.countLabel(f), ids: f.model.columns.flatMap((c) => c.cards.map((k) => k.id)), cols: f.model.columns.length, same: f.model === m }; };
  // every combining mark folds (a pointed Hebrew title matches the unpointed query); a depends_on entry that is no string or
  // number is dropped, never drawn as "[object Object]"; the label words a card shows are searchable
  const m2 = V.boardModel({ queue: "ok", columns: [{ category: "ready", cards: [{ id: "h", title: "שָׁלוֹם", done_line: "tests pass", depends_on: [{ a: 1 }, "x", 5, null] }] }] });
  const ready2 = (f) => f.model.columns.find((c) => c.category === "ready").cards.length;
  const folded2 = { marks: ready2(V.filterModel(m2, "שלום")), deps: m2.columns.find((c) => c.category === "ready").cards[0].dependsOn,
    label: ready2(V.filterModel(m2, "done when: tests")), after: ready2(V.filterModel(m2, "after: x")), colKeys: Object.keys(V.filterModel(m, "land").model.columns[0]).sort() };
  return { folded2, none: pick(""), blank: pick("   "), folded: pick("LATER  c"), journalWords: pick("took it"), and: pick("land levain"), miss: pick("zzz"), band: pick("sweep"),
    noJournal: V.journalLine(null), journal: V.journalLine({ who: "desk", iso: "t", to_category: "ready", why: "queued it." }) };
}
// The "Drop about this card" button: one per card in both bodies when the cockpit offers its drop box; none when it does not;
// a click hands the box the card's words, and only that (nothing is fetched or posted from here).
function dropButtons() {
  const asked = [];
  let fetched = 0;
  globalThis.fetch = () => { fetched++; return Promise.reject(new Error("no route")); };
  globalThis.LevainDashboard = { composeDrop: (w) => { asked.push(w); return true; } };
  load();
  const V = globalThis.LevainBoardView;
  const model = V.boardModel(BODY);
  const g = node("div"), c = node("div");
  V.renderGrouped(doc, g, model);
  V.renderBoard(doc, c, model);
  const btns = (root) => walk(root).filter((n) => n.className === "bd-drop");
  const gb = btns(g), cb = btns(c);
  gb.find((b) => b.dataset.dropAbout === "levain:a").click();
  cb.find((b) => b.dataset.dropAbout === "q-1").click();
  const out = { grouped: gb.map((b) => [b.dataset.dropAbout, b.textContent, b.tag]), cols: cb.length, asked, fetched };
  delete globalThis.LevainDashboard;
  load();
  const bare = node("div");
  globalThis.LevainBoardView.renderGrouped(doc, bare, globalThis.LevainBoardView.boardModel(BODY));
  out.withoutCockpit = btns(bare).length;
  out.words = globalThis.LevainBoardView.dropWords({ id: "x:1", title: "T" });
  return out;
}
// drop_here: absent (a provider's board) and true draw the buttons; false draws none, in both bodies. A 1000-char title (and id)
// is the longest a validated card can carry: the words a drop starts with are measured.
function dropHere() {
  globalThis.LevainDashboard = { composeDrop: () => true };
  load();
  const V = globalThis.LevainBoardView;
  const count = (extra) => {
    const model = V.boardModel(Object.assign({}, BODY, extra));
    const g = node("div"), c = node("div");
    V.renderGrouped(doc, g, model);
    V.renderBoard(doc, c, model);
    return [walk(g).filter((n) => n.className === "bd-drop").length, walk(c).filter((n) => n.className === "bd-drop").length];
  };
  const a = count({}), t = count({ drop_here: true }), f = count({ drop_here: false });
  delete globalThis.LevainDashboard;
  load();
  return { absent: a[0], true: t[0], false: f[0], falseCols: f[1] };
}
const hereOut = dropHere();
const longTitleWords = globalThis.LevainBoardView.dropWords({ id: "i".repeat(1000), title: "t".repeat(1000) });
load();
const V = globalThis.LevainBoardView;
const out = { exports: Object.keys(V).sort(), filtering: filtering(V), grouped: grouped(V) };
// An empty column says "nothing" only for a source that was read: an unreadable queue (or band) says "unknown", in both bodies.
function emptyWords(V, extra) {
  const model = V.boardModel(Object.assign({ queue: "ok", columns: [] }, extra));
  const g = node("div"), c = node("div");
  V.renderGrouped(doc, g, model);
  V.renderBoard(doc, c, model);
  const empties = (root) => walk(root).filter((n) => n.className === "bd-empty").map((n) => n.textContent);
  return { grouped: empties(g), cols: empties(c) };
}
out.emptyWords = { ok: emptyWords(V, {}), queue: emptyWords(V, { queue: "unreadable", unreadable: ["coordinator"] }), band: emptyWords(V, { unreadable: ["band"] }) };
out.drop = dropButtons();
out.dropHere = hereOut;
out.longTitleWords = longTitleWords;
console.log(JSON.stringify(out));
