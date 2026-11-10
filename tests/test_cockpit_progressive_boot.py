"""The cold first board draws as its panels land (cost table #1): the manifest, then the panels the page's top is made of,
then the rest as each answers, with "connecting..." cleared on the first useful content. The pure parts (read order, the
manifest narrowed to the panels in hand, the pacing of the partial draws) are run under node; the wiring is held by
reading the boot source for the two calls that matter."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const a = src.indexOf("// BEGIN progressive draw"), b = src.indexOf("// END progressive draw");
if (a < 0 || b < 0) { console.log(JSON.stringify({ missing: true })); process.exit(0); }
const lib = new Function(src.slice(a, b) + "; return { drawOrder, manifestSubset, partialPacer, PARTIAL_GAP_MS };")();
const out = {};
const manifest = {
  panels: { activity: {}, state: {}, begin: {}, decisions: {}, loops: {}, tray: {}, crystals: {} },
  regions: {
    hero: ["activity"], brief: ["state", "begin"], widgets: ["decisions"], header: ["activity", "state", "begin", "decisions"],
    zones: [
      { id: "operate", panels: ["tray", "loops"], cards: [{ panels: ["tray"] }, { panels: ["loops"] }] },
      { id: "mind", panels: ["crystals"], cards: [{ panels: ["crystals"] }] },
    ],
  },
};
// the manifest lists the page's own order; a manifest with the Needs strip's panel late still reads it in the first wave
const ids = ["loops", "tray", "activity", "state", "begin", "crystals", "decisions"];
out.order = lib.drawOrder(manifest, ids);
out.noRegions = lib.drawOrder({ panels: {} }, ["a", "b"]);
const sub = lib.manifestSubset(manifest, new Set(["activity", "decisions", "tray"]));
out.sub = { panels: Object.keys(sub.panels), hero: sub.regions.hero, brief: sub.regions.brief, widgets: sub.regions.widgets,
            zones: sub.regions.zones.map((z) => [z.id, z.panels, z.cards.map((c) => c.panels)]) };
out.whole = Object.keys(manifest.panels).length === 7 && manifest.regions.zones.length === 2;   // the source manifest is not mutated

// the pacer, on a fake clock
let t = 0, timers = [];
const clock = { now: () => t, setTimeout: (f, ms) => { const h = { f, at: t + ms }; timers.push(h); return h; }, clearTimeout: (h) => { timers = timers.filter((x) => x !== h); } };
const advance = (ms) => { t += ms; for (const h of timers.filter((x) => x.at <= t)) { timers = timers.filter((x) => x !== h); h.f(); } };
const draws = [];
const p = lib.partialPacer(["a", "b"], 6, (n) => draws.push([t, n]), clock);
p.settled("x");                                  // not core
p.settled("a");                                  // core still waiting on b
out.beforeCore = draws.length;
p.settled("b");                                  // the core is in: the first draw is at once
out.atCore = draws.slice();
p.settled("c");                                  // inside the gap: one timer, no draw yet
p.settled("d");                                  // coalesced into the same timer
out.inGap = { draws: draws.length, timers: timers.length };
advance(lib.PARTIAL_GAP_MS);
out.afterGap = draws.slice();
p.settled("e");                                  // the last one (6 of 6): the full draw is the caller's
advance(1000);
out.lastNeverDraws = draws.length;

// a draw that takes long widens the gap
t = 0; timers = []; const d2 = [];
const slow = lib.partialPacer(["a"], 10, (n) => { d2.push(t); t += 200; }, clock);
slow.settled("a"); slow.settled("b");           // draw 1 takes 200 ms; the next waits 4 x 200 = 800 ms from its end
advance(799); out.slowEarly = d2.length;
advance(1); out.slowOnTime = d2.length;

// stop cancels a pending draw
t = 0; timers = []; const d3 = [];
const q = lib.partialPacer([], 10, (n) => d3.push(n), clock);
q.settled("a"); q.settled("b"); q.stop(); advance(5000);
out.stopped = { draws: d3.length, timers: timers.length };
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_first_board_reads_its_top_first_narrows_the_manifest_and_paces_its_partial_draws(tmp_path: Path) -> None:
    driver = tmp_path / "drive.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    run = subprocess.run([shutil.which("node"), str(driver), str(WEB / "dashboard_boot.js")],
                         capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(run.stdout)
    assert not out.get("missing"), "the boot has no progressive draw block"
    # hero, brief, then the Needs strip's panel come first, the rest keep the manifest's order
    assert out["order"] == {"core": ["activity", "state", "begin", "decisions"],
                            "order": ["activity", "state", "begin", "decisions", "loops", "tray", "crystals"]}
    assert out["noRegions"] == {"core": [], "order": ["a", "b"]}
    # the board in hand: only the panels read, a zone or card with none left is gone
    assert out["sub"] == {"panels": ["activity", "decisions", "tray"], "hero": ["activity"], "brief": [], "widgets": ["decisions"],
                          "zones": [["operate", ["tray"], [["tray"]]]]}
    assert out["whole"] is True
    # nothing draws before the core is in, the core's arrival draws at once, later landings coalesce within the gap
    assert out["beforeCore"] == 0
    assert out["atCore"] == [[0, 3]]
    assert out["inGap"] == {"draws": 1, "timers": 1}
    assert out["afterGap"] == [[0, 3], [250, 5]]
    assert out["lastNeverDraws"] == 2
    assert (out["slowEarly"], out["slowOnTime"]) == (1, 2)
    assert out["stopped"] == {"draws": 1, "timers": 0}


def test_the_cold_load_draws_partial_boards_and_a_panel_read_is_bounded() -> None:
    boot = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
    # only a page that has drawn nothing yet draws partial boards; a refresh keeps its board until the new one is whole
    assert "const onPartial = everDrawn ? null :" in boot
    assert "loadManifestView(passive, onPartial)" in boot
    # the cold placeholder goes with the first useful content, not after the last panel
    assert 'storeEl.removeAttribute("data-cold");\n          drawnOnce();' in boot
    # a panel that does not answer is its own failure state, and frees its read slot
    assert "ctl.abort()" in boot and "PANEL_TIMEOUT_MS" in boot
