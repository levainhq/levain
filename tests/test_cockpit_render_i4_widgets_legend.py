"""1012+100 item 4, the renderer's parts (the data parts are flow's; see 1012-ke/RENDER/RETURN.md):

* the Decisions count widget answers a zero with the panel's empty sentence ("Nothing waiting on you", as mock-e
  draws it), not the provider's note; the note is the line when something is waiting or the panel is alerted;
* the sky's legend is a full-width band BELOW the hero (the page's own #ck-legend mount), not a row inside the sky
  column; a page without the mount keeps it under the plot."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests._ke_dom import run_node

BODY = r"""
const NOTE = "needs_phill list, dated first; no cockpit broker: decisions waiting on a signature are not shown";
const widget = (count, alerted, extra) => {
  const heads = { decisions: head("decisions", "list", "Decisions", { face: "count" }) };
  const rows = Array.from({ length: count }, (_, i) => ({ id: "d" + i, version: "v", title: "t" + i, body: "t" + i, group: null, badges: [], facets: {} }));
  const payloads = { decisions: head("decisions", "list", "Decisions", Object.assign({ face: "count", count: count, rows: rows, note: NOTE, empty: "nothing is waiting on you." }, extra || {})) };
  const v = V.fromManifest(fixture(heads, payloads));
  const host = new N("div");
  K.fill(host, v.extra_panels.decisions, { widget: true, alerted: !!alerted, entry: { title: "Decisions" } });
  return host.all((n) => n.className === "sub").map((n) => n.textContent);
};
out.zero = widget(0, false);
out.one = widget(1, false);
out.alertedZero = widget(0, true);
out.partialZero = widget(0, false, { status: "partial", skipped: [{ count: 2, reason: "unreadable" }] });
out.staleZero = widget(0, false, { status: "stale" });
out.heldZero = widget(0, false, { filtered: [{ count: 1, reason: "held" }] });
out.identZero = widget(0, false, { empty: "npm-registry has nothing" });

const sky = { kind: "ambient", pid: "sky", typed: true, search: {}, note: "", empty: "",
  sky: { events: [{ kind: "commit", at: "2026-10-12T11:00:00+00:00", repo: "levain" }], legend: "kernel sentence" } };
const band = mount("ck-legend"); band.hidden = true;
const host = new N("div");
K.fill(host, sky, { hero: { events: new N("div") }, entry: { title: "Sky" } });
out.bandHasLegend = band.all((n) => n.hasClass("legendbar")).length;
out.bandShown = band.hidden === false;
out.hostHasLegend = host.all((n) => n.hasClass("legendbar")).length;
out.glyphs = band.all((n) => n.hasClass("lg")).length;
// no mount on the page: the legend stays under the plot
delete globalThis.__x;
const host2 = new N("div");
const saved = document.getElementById;
document.getElementById = () => null;
K.fill(host2, Object.assign({}, sky, { pid: "sky2" }), { hero: { events: new N("div") }, entry: { title: "Sky" } });
document.getElementById = saved;
out.fallbackHasLegend = host2.all((n) => n.hasClass("legendbar")).length;
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_decisions_zero_answers_with_the_empty_sentence_and_the_legend_is_a_band(tmp_path: Path) -> None:
    o = run_node(BODY, tmp_path)
    assert o["zero"] == ["Nothing is waiting on you."]
    assert o["one"] and o["one"][0].startswith("needs_phill list")     # something waiting: the provider's note stays
    for k in ("partialZero", "staleZero", "heldZero"):              # a degraded zero keeps the provider's note
        assert o[k] and o[k][0].startswith("needs_phill list"), k
    assert o["identZero"] == ["npm-registry has nothing"]            # not blindly upper-cased
    assert o["alertedZero"] and o["alertedZero"][0].startswith("needs_phill list")
    assert o["bandHasLegend"] == 1 and o["bandShown"] is True and o["hostHasLegend"] == 0
    assert o["glyphs"] >= 5                                            # the real glyph key, not just the sentence
    assert o["fallbackHasLegend"] == 1
