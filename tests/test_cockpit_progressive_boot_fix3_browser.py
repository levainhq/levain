"""The render core's side of the progressive boot's L3 round 2 fixes (levain#27), in real headless Chrome over a served scratch install.

The page's own manifest and panel bodies are read once, then drawn through the real mapper and render core with panels marked loading
or failed:
- a line drawn from more than one panel is released when ANY of its inputs is released, and kept only while ALL of them are kept: the
  band's wrap line and the Memory meta with Health landed and wraps hung, and the Neocortex placeholder with one section landed and
  another out;
- a wraps read that failed is drawn as its failure, never as loading because the Health panel (another panel) is still out."""

from __future__ import annotations

import json

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_progressive_boot_review_browser import _HELPERS, _READ

pytestmark = needs_browser

# an edit is open (the rename editor beside #entity), so the release is made on the page in place, and the redraw after it closes
# draws the released view
_EDIT = r"""const ed = document.createElement("div"); ed.className = "name-editor"; document.getElementById("entity").parentElement.appendChild(ed);"""

# Health landed, wraps hung: the watchdog keeps every panel but wraps
_WRAPS_HUNG = "(() => { " + _HELPERS + _EDIT + r"""
  D.render(mk((id) => (id === "wraps" ? { loading: true } : __P[id])), {});
  out.before = snap();
  out.editing = D.hasUnsavedEdit();
  D.releaseLoading("no answer in 60 s", ids.filter((id) => id !== "wraps"));
  out.released = snap();
  ed.remove();
  D.redraw();
  out.redrawn = snap();
  return JSON.stringify(out); })()"""

# every Neocortex section out, then one of them lands (kept) and the others time out
_SECTIONS = "(() => { " + _HELPERS + _EDIT + r"""
  const secs = ids.filter((id) => /^section:/.test(id));
  out.sections = secs.length;
  D.render(mk((id) => (/^section:/.test(id) ? { loading: true } : __P[id])), {});
  const ph = () => q(".ck-mem-neo p.empty.loading[data-waits]");
  out.before = { placeholder: !!ph(), waits: ph() ? ph().dataset.waits.split(" ").filter((x) => /^section:/.test(x)).length : 0 };
  D.releaseLoading("no answer in 60 s", ids.filter((id) => !/^section:/.test(id) || id === secs[0]));
  out.after = { placeholder: !!ph(), loading: document.querySelectorAll(".ck-mem-neo p.empty.loading").length,
    unavailable: /unavailable/.test(t(".ck-mem-neo") || "") };
  ed.remove();
  return JSON.stringify(out); })()"""

# wraps' own read failed while Health (which counts the wraps) is still out
_PRECEDENCE = "(() => { " + _HELPERS + r"""
  D.render(mk((id) => (id === "wraps" ? { error: "HTTP 500" } : id === "health" ? { loading: true } : __P[id])), {});
  out.drawn = snap();
  return JSON.stringify(out); })()"""


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp = tmp_path_factory.mktemp("fix3_browser")
    _root, src, _ids = _install(tmp)
    steps = [{"until": "document.documentElement.hasAttribute('data-board-whole')", "ms": 60000},
             {"eval": _READ}, {"eval": _WRAPS_HUNG}, {"eval": _SECTIONS}, {"eval": _PRECEDENCE}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        res = _drive(tmp, f"{url}/#write_token=T", steps)
    vals = res["values"]
    return {"ids": json.loads(vals[-4]), "wraps": json.loads(vals[-3]), "sections": json.loads(vals[-2]), "precedence": json.loads(vals[-1])}


def test_the_wrap_figures_are_released_when_wraps_is_released_though_health_is_kept(run: dict) -> None:
    w = run["wraps"]
    assert w["editing"] is True, w
    # the scenario: both figures say loading while wraps is out
    assert "loading" in (w["before"]["wrap"] or "") and "loading" in (w["before"]["neo"] or ""), w["before"]
    for k in ("released", "redrawn"):
        s = w[k]
        assert "loading" not in (s["wrap"] or ""), (k, s)
        assert "loading" not in (s["neo"] or ""), (k, s)
        assert not any("wrapline" in b for b in s["busyAt"]), (k, s)


def test_the_neocortex_placeholder_is_released_when_one_of_its_sections_is_released(run: dict) -> None:
    s = run["sections"]
    assert s["sections"] >= 2, s
    assert s["before"] == {"placeholder": True, "waits": s["sections"]}, s
    assert s["after"]["placeholder"] is False and s["after"]["loading"] == 0, s
    assert s["after"]["unavailable"] is True, s


def test_a_failed_wraps_read_is_drawn_as_its_failure_while_health_is_still_out(run: dict) -> None:
    d = run["precedence"]["drawn"]
    assert "loading" not in (d["wrap"] or ""), d
    assert d["neo"] == "wraps unavailable", d
    assert not any("wrapline" in b for b in d["busyAt"]), d
