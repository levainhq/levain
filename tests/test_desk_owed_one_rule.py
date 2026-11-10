"""One rule for "a band row the desk alone owes" (section desk, not carried over to him): the Python predicate
(band_view.desk_owed, which the app badge and the board producer use) and the Board's JS filter (board_view.js isDeskOwed /
visibleCards) answer the same on the same fixture rows, and every count the board draws or serves is of the visible set."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from levain import band_view
from levain.cockpit import board, engine

ROOT = Path(__file__).resolve().parent.parent
# (section, carried, desk_owed?)
ROWS = [("desk", False, True), ("desk", True, False), ("you", False, False), ("", False, False), ("desk", None, True)]
SCRIPT = """
const fs = require("fs"), vm = require("vm");
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
const V = window.LevainBoardView;
const rows = JSON.parse(process.argv[3]);
const cards = rows.map((r, i) => { const k = { id: "c" + i, title: "t", band: true }; if (r[0]) k.section = r[0]; if (r[1] !== null) k.carried = r[1]; return k; });
console.log(JSON.stringify({ owed: cards.map((k) => V.isDeskOwed(k)), visible: V.visibleCards(cards).map((k) => k.id) }));
"""


def _js(tmp_path):
    s = tmp_path / "probe.js"
    s.write_text(SCRIPT)
    r = subprocess.run(["node", str(s), str(ROOT / "levain/templates/web/board_view.js"), json.dumps([[a, b] for a, b, _ in ROWS])],
                       capture_output=True, text=True, timeout=60, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_python_and_js_agree_on_the_desk_owed_rule(tmp_path):
    want = [w for _, _, w in ROWS]
    assert [band_view.desk_owed(s, c) for s, c, _ in ROWS] == want
    got = _js(tmp_path)
    assert got["owed"] == want
    assert got["visible"] == [f"c{i}" for i, w in enumerate(want) if not w]


def test_the_compact_board_counts_the_visible_set():
    cards = [{"id": f"b{i}", "band": True, **({"section": s} if s else {}), **({"carried": c} if c is not None else {})}
             for i, (s, c, _) in enumerate(ROWS)]
    val = {"queue": "ok", "columns": [{"category": "needs-you", "count": len(cards), "cards": cards}]}
    got = engine._compact_value("board", val)
    assert got["columns"] == [{"category": "needs-you", "count": 3}]
    assert [c["id"] for c in board.visible_cards(cards)] == ["b1", "b2", "b3"]


def test_a_store_row_without_the_host_fields_is_his_and_a_read_fault_is_logged(tmp_path, caplog):
    from types import SimpleNamespace
    from levain.webpush.needs import open_needs_count

    class Store:
        def open_questions(self):
            return [SimpleNamespace(), SimpleNamespace(section="desk", carried=False)]

        def later_facets(self, q):
            return None

    # a row with no section or carried attribute is his (a host that sends neither); the desk's fresh row is not
    n = open_needs_count(tmp_path / "q", Store())
    assert n == 1

    class Broken:
        def open_questions(self):
            raise OSError("db gone")
    with caplog.at_level(logging.WARNING, logger="levain.webpush.needs"):
        assert open_needs_count(tmp_path / "q", Broken()) is None
    assert any("db gone" in r.getMessage() or "db gone" in str(r.exc_info) for r in caplog.records)


def test_the_board_touch_summary_line_counts_the_cards_the_board_draws(tmp_path):
    """The one-line summary a touch reader gets (cards and per-column counts) leaves out a desk-owed card, as the drawn board does."""
    from levain.cockpit import Cockpit, ProviderSpec, Read
    from levain.cockpit.text import snapshot

    cards = [{"id": f"band:{i}", "title": "t", "band": True, **({"section": s} if s else {}), **({"carried": c} if c is not None else {})}
             for i, (s, c, _) in enumerate(ROWS)]
    value = {"schema": 1, "generated": None, "queue": "ok", "unreadable": [], "note": None,
             "columns": [{"category": c, "count": len(cards) if c == "needs-you" else 0, "cards": cards if c == "needs-you" else []}
                         for c in board.CATEGORIES]}
    ck = Cockpit(entity=lambda ctx: {"name": "t", "governance": "x", "brand": {"wordmark": "W", "model": "M"}})
    ck.register(ProviderSpec(id="bk", kind="board", title="B", priority="feed", read=lambda c: Read(value=value), region="operate"))
    driver = (
        "globalThis.window=globalThis;const fs=require('fs');require('vm').runInThisContext(fs.readFileSync(process.argv[1],'utf8'));"
        "const V=require(process.argv[2]);const snap=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(V.fromManifest(snap)))")
    r = subprocess.run(["node", "-e", driver, str(ROOT / "levain/templates/web/board_view.js"), str(ROOT / "levain/templates/web/cockpit_view.js")],
                       input=json.dumps(snapshot(ck)), capture_output=True, text=True, check=True, timeout=60)
    assert "3 cards" in r.stdout and "needs-you 3" in r.stdout, r.stdout[:2000]


NONBAND_SCRIPT = """
const fs = require("fs"), vm = require("vm");
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
const V = window.LevainBoardView;
const cards = [{ id: "n1", title: "t", section: "desk" }, { id: "n2", title: "t", section: "desk", band: false },
               { id: "b1", title: "t", band: true, section: "desk" }];
console.log(JSON.stringify({ owed: cards.map((k) => V.isDeskOwed(k)), visible: V.visibleCards(cards).map((k) => k.id) }));
"""


def test_a_card_that_is_not_a_band_row_is_never_hidden_by_a_section(tmp_path):
    """The engine's board validator keeps extra keys, so any card may carry ``section``: only a band row is the desk's to owe."""
    cards = [{"id": "n1", "section": "desk"}, {"id": "n2", "section": "desk", "band": False},
             {"id": "b1", "band": True, "section": "desk"}]
    assert [board.is_desk_owed(c) for c in cards] == [False, False, True]
    assert [c["id"] for c in board.visible_cards(cards)] == ["n1", "n2"]
    s = tmp_path / "probe.js"
    s.write_text(NONBAND_SCRIPT)
    r = subprocess.run(["node", str(s), str(ROOT / "levain/templates/web/board_view.js")], capture_output=True, text=True,
                       timeout=60, check=True)
    assert json.loads(r.stdout) == {"owed": [False, False, True], "visible": ["n1", "n2"]}
