// board_view.js's unknown column and off count (run by tests/test_cockpit_board_v2.py under node): prints one JSON object.
const fs = require("fs");
const vm = require("vm");
function node(tag) {
  return { tag, children: [], dataset: {}, className: "", textContent: "", setAttribute() {}, appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); }, replaceChildren() { this.children = []; }, addEventListener() {} };
}
const doc = { createElement: node };
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
const V = globalThis.LevainBoardView;
const walk = (n, out = []) => { out.push(n); (n.children || []).forEach((c) => walk(c, out)); return out; };
const text = (n) => n.textContent + n.children.map(text).join("");
const draw = (body) => {
  const g = node("div");
  V.renderGrouped(doc, g, V.boardModel(body));
  const all = walk(g);
  return { heads: all.filter((n) => n.className === "grp-head").map((n) => n.textContent),
           notes: all.filter((n) => n.className === "bd-note").map(text), held: all.filter((n) => n.className === "bd-held").map(text) };
};
const cols = (unknown) => [{ category: "unknown", cards: unknown }, { category: "ready", cards: [{ id: "a#1", title: "A" }] }];
console.log(JSON.stringify({
  with: draw({ queue: "ok", off_count: 3, columns: cols([{ id: "a#2", title: "B", held_reason: "item:a#9 is not in the store" }]) }),
  without: draw({ queue: "ok", columns: cols([]) }),
}));
