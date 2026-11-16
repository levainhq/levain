"""The render core's side of the progressive boot's L3 round 1 fixes (levain#27), in real headless Chrome over a served scratch install.

The page's own manifest and panel bodies are read once, then drawn through the real mapper and render core with panels marked loading
or failed: a release is made in the view as well as on the page (a later redraw does not bring loading back, the vital says unknown,
the Neocortex card's loading line and meta go), a release keeps the panels it is told have landed, and no figure drawn from the wraps
panel, or from the Neocortex sections, reads a panel that is out or failed as an empty one."""

from __future__ import annotations

import json

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytestmark = needs_browser

_READ = r"""(async () => { const tok = localStorage.getItem("levain_write_token") || ""; const H = tok ? { "X-Levain-Write-Token": tok } : {};
  const m = await (await fetch("/cockpit/manifest.json", { headers: H })).json(); const P = {};
  for (const id of Object.keys(m.panels)) P[id] = await (await fetch("/cockpit/panel/" + encodeURIComponent(id) + ".json?profile=full", { headers: H })).json();
  window.__M = m; window.__P = P; return JSON.stringify(Object.keys(P).sort()); })()"""

_HELPERS = r"""const V = window.LevainCockpitView, D = window.LevainDashboard, ids = Object.keys(__M.panels);
  const mk = (f) => V.fromManifest({ manifest: JSON.parse(JSON.stringify(__M)), panels: Object.fromEntries(ids.map((id) => [id, f(id)])) }, { elapsedMs: 0 });
  const q = (s) => document.querySelector(s), t = (s) => (q(s) ? q(s).textContent : null);
  const snap = () => ({ vital: document.body.dataset.vital || "", loading: document.querySelectorAll("p.empty.loading").length,
    // (the notification centre's skeleton, .nc-skel, is its own read of its own feed, not a panel's)
    busy: document.querySelectorAll('[aria-busy="true"]:not(.nc-skel)').length,
    busyAt: Array.from(document.querySelectorAll('[aria-busy="true"]:not(.nc-skel)')).map((n) => n.outerHTML.slice(0, 160)), neo: t(".ck-mem-neo .ck-mem-meta"), wrap: t(".wrapline"), state: t("#state"),
    neoText: t(".ck-mem-neo") });
  const out = {};"""

# everything but the Needs panel out: released, then redrawn from the view the page holds
_RELEASE = "(() => { " + _HELPERS + r"""
  D.render(mk((id) => (id === "decisions" ? __P[id] : { loading: true })), {});
  out.before = snap();
  D.releaseLoading("no answer in 60 s");
  out.released = snap();
  D.redraw();
  out.redrawn = snap();
  // the same with an edit open (the rename editor beside #entity): the page is not redrawn under it, the loading lines are swapped in place,
  // and the redraw after the edit closes draws the released view
  D.render(mk((id) => (id === "decisions" ? __P[id] : { loading: true })), {});
  const ed = document.createElement("div"); ed.className = "name-editor"; document.getElementById("entity").parentElement.appendChild(ed);
  out.editing = D.hasUnsavedEdit();
  D.releaseLoading("no answer in 60 s");
  out.inPlace = snap();
  ed.remove();
  D.redraw();
  out.inPlaceRedrawn = snap();
  return JSON.stringify(out); })()"""

# a release told which panels have landed keeps their shells (their bodies are in hand, the draw is waiting), and the view keeps them too
_KEEP = "(() => { " + _HELPERS + r"""
  D.render(mk((id) => (id === "decisions" ? __P[id] : { loading: true })), {});
  const shell = Array.from(document.querySelectorAll("[data-ck-panel] p.empty.loading")).map((p) => p.closest("[data-ck-panel]").dataset.ckPanel)
    .find((id) => !/^(health|wraps|decisions|state)$/.test(id));
  const others = () => Array.from(document.querySelectorAll("[data-ck-panel] p.empty.loading")).filter((p) => p.closest("[data-ck-panel]").dataset.ckPanel !== shell).length;
  const kept = () => !!document.querySelector('[data-ck-panel="' + shell + '"] p.empty.loading');
  D.releaseLoading("no answer in 60 s", [shell]);
  out.shell = shell; out.kept = kept(); out.others = others();
  D.redraw();
  out.keptRedrawn = kept(); out.othersRedrawn = others();
  return JSON.stringify(out); })()"""

# the figures drawn from the wraps panel and from the Neocortex sections, with those panels out or failed
_FIGURES = "(() => { " + _HELPERS + r"""
  D.render(mk((id) => (id === "wraps" || id === "health" ? { loading: true } : __P[id])), {});
  out.graphWhileWrapsOut = t(".ck-mem-cog");
  D.render(mk((id) => (id === "wraps" || id === "health" ? { error: "HTTP 500" } : __P[id])), {});
  out.wrapsFailed = snap();
  out.sections = ids.filter((id) => /^section:/.test(id)).length;
  D.render(mk((id) => (/^section:/.test(id) ? { error: "HTTP 500" } : __P[id])), {});
  out.sectionsFailed = snap();
  return JSON.stringify(out); })()"""


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp = tmp_path_factory.mktemp("review_browser")
    _root, src, _ids = _install(tmp)
    steps = [{"until": "document.documentElement.hasAttribute('data-board-whole')", "ms": 60000},
             {"eval": _READ}, {"eval": _RELEASE}, {"eval": _KEEP}, {"eval": _FIGURES}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        res = _drive(tmp, f"{url}/#write_token=T", steps)
    vals = res["values"]
    return {"ids": json.loads(vals[-4]), "release": json.loads(vals[-3]), "keep": json.loads(vals[-2]), "figures": json.loads(vals[-1])}


def test_a_release_is_made_in_the_view_so_a_redraw_does_not_bring_loading_back(run: dict) -> None:
    r = run["release"]
    assert r["before"]["loading"] > 3 and r["before"]["vital"] == "", r["before"]
    assert r["editing"] is True, r
    for k in ("released", "redrawn", "inPlace", "inPlaceRedrawn"):
        s = r[k]
        assert s["loading"] == 0 and s["busy"] == 0, (k, s)
        # the health shell released: the vital ring says unknown, never cleared and never live or dark
        assert s["vital"] == "unknown", (k, s)
        # the Neocortex card's meta and the band's wrap line no longer say loading
        assert "loading" not in (s["neo"] or "") and "loading" not in (s["wrap"] or ""), (k, s)
        assert "loading" not in (s["neoText"] or ""), (k, s)


def test_a_release_keeps_the_shells_of_the_panels_that_landed(run: dict) -> None:
    k = run["keep"]
    assert k["shell"], k
    assert k["kept"] is True and k["others"] == 0, k
    assert k["keptRedrawn"] is True and k["othersRedrawn"] == 0, k


def test_the_wraps_figures_never_read_an_out_or_failed_wraps_panel_as_none(run: dict) -> None:
    f = run["figures"]
    assert "graph" in run["ids"], run["ids"]
    # the Cognition trace while wraps is still being read: loading, never "needs two consolidations"
    assert f["graphWhileWrapsOut"] is not None and "needs two consolidations" not in f["graphWhileWrapsOut"], f
    assert "loading" in f["graphWhileWrapsOut"], f
    # wraps (and health, its count) failed: the Neocortex meta says unavailable, and the band never says none yet
    w = f["wrapsFailed"]
    assert w["neo"] == "wraps unavailable", w
    assert "No wraps yet" not in (w["wrap"] or "") and "fresh install" not in (w["wrap"] or ""), w
    assert w["vital"] == "unknown", w


def test_failed_neocortex_sections_never_read_as_no_wrap_yet(run: dict) -> None:
    f = run["figures"]
    assert f["sections"] > 0, f
    s = f["sectionsFailed"]
    assert "No wrap yet" not in (s["neoText"] or ""), s
    assert "unavailable" in (s["neoText"] or ""), s
