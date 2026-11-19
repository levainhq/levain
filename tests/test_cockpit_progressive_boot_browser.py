"""The cold cockpit's loading states, in real headless Chrome over a served scratch install whose panel reads are slowed (levain#27).

A panel still being read is visibly loading, never empty and never dark: not in its own body, and not in any figure the page draws from
its payload elsewhere (the band's wrap line, the vital ring, the Memory card's Cognition tabs, the Needs strip, the state line). The slowed
reads are the page's own fetches, delayed in the page (the scratch install answers each panel in about a millisecond, so the delay is put in the page's
fetch and nothing is faked in the boot). The same run records every storage, IndexedDB and Cache API write the page makes, since a
cold board must never be painted from a cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

pytestmark = needs_browser

# the delays: wraps and health answer last, decisions and the state line next, the rest early
_INIT = r"""
window.__s = []; window.__t0 = performance.now(); window.__views = []; window.__stored = [];
let _d; Object.defineProperty(window, "LevainDashboard", { configurable: true, get: () => _d, set: (v) => { _d = v; const r = v.render;
  v.render = function (view) { window.__views.push(view); return r.apply(this, arguments); }; } });
const of = window.fetch.bind(window);
window.fetch = (u, o) => { const s = String(u && u.url || u); const m = /\/cockpit\/panel\/([^/.?]+)/.exec(s);
  const d = !m ? 0 : /^(wraps|health)$/.test(m[1]) ? 2500 : /^(decisions|state)$/.test(m[1]) ? 1200 : 300;
  return d ? new Promise((r) => setTimeout(r, d)).then(() => of(u, o)) : of(u, o); };
const log = (kind, a) => window.__stored.push([kind, String(a[0]), a[1] === undefined ? "" : String(a[1]).slice(0, 4000)]);
const sset = Storage.prototype.setItem; Storage.prototype.setItem = function () { log("storage", arguments); return sset.apply(this, arguments); };
if (window.IDBFactory) { const o = IDBFactory.prototype.open; IDBFactory.prototype.open = function () { log("indexedDB", arguments); return o.apply(this, arguments); }; }
if (window.CacheStorage) { const o = CacheStorage.prototype.open; CacheStorage.prototype.open = function () { log("caches", arguments); return o.apply(this, arguments); }; }
if (window.Cache) for (const k of ["put", "add", "addAll"]) { const o = Cache.prototype[k]; Cache.prototype[k] = function () { log("cache." + k, arguments); return o.apply(this, arguments); }; }
setInterval(() => { const d = document; const q = (s) => d.querySelector(s); const t = (n) => n ? n.textContent : null; const n = (s) => d.querySelectorAll(s).length;
  window.__s.push({ t: Math.round(performance.now() - window.__t0), vital: (d.body && d.body.dataset.vital) || "", wrap: t(q(".wrapline")),
    needs: t(q("#ck-needs")), needsBusy: !!(q("#ck-needs") && q("#ck-needs").getAttribute("aria-busy")), state: t(q("#state")),
    stateBusy: !!(q("#state") && q("#state").getAttribute("aria-busy")), shells: n('[aria-busy="true"] p.empty.loading[role=status]'),
    healthLoading: n('[data-ck-panel=health] p.empty.loading'), wrapsShell: n('[data-ck-panel=wraps]'), cogTabs: n(".ck-mem-cog .ck-tabs"),
    memMeta: t(q(".ck-mem-neo .ck-mem-meta")), whole: d.documentElement.hasAttribute("data-board-whole"), busy: n('[aria-busy="true"]') }); }, 25);
"""

# a panel that is both loading and failed draws its failure: at the zone's external panels and at the bare (widget / hero) ones
_PRECEDENCE = r"""(() => { const D = window.LevainDashboard, v = window.__views[window.__views.length - 1]; const out = {};
  const zone = v.layout.find((e) => e.kind === "external" && v.extra_panels[e.id] && !v.extra_panels[e.id].error);
  v.extra_panels[zone.id] = { loading: true, error: "boomz" };
  const n = D.renderPanel(zone, v);
  out.zone = { err: /boomz/.test(n.textContent), loading: !!n.querySelector("p.empty.loading"), busy: n.getAttribute("aria-busy") };
  const bare = (v.ke.widgets || []).concat(v.ke.hero || []).find((id) => v.extra_panels[id] && !v.extra_panels[id].error);
  v.extra_panels[bare] = { loading: true, error: "boomb" };
  D.render(v, {});
  const b = document.querySelector('[data-ck-panel="' + bare + '"]');
  out.bare = b ? { err: /boomb/.test(b.textContent), loading: !!b.querySelector("p.empty.loading"), busy: b.getAttribute("aria-busy") } : null;
  return JSON.stringify(out); })()"""

# a shell whose read is not coming becomes its failure, and nothing is left busy
_RELEASE = r"""(() => { const D = window.LevainDashboard;
  if (typeof D.releaseLoading !== "function") return JSON.stringify({ missing: true });
  D.render({ layout: [], extra_panels: {}, errors: {}, loading: {}, failed: {}, ke: null, writable: false, targets: { rows: {}, panels: {} } }, {});
  const host = document.getElementById("ck-needs"); host.hidden = false; host.setAttribute("aria-busy", "true");
  host.replaceChildren(Object.assign(document.createElement("p"), { className: "empty loading", textContent: "loading what needs you" }));
  const st = document.getElementById("state"); st.hidden = false; st.setAttribute("aria-busy", "true"); st.replaceChildren(Object.assign(document.createElement("span"), { className: "ftext", textContent: "loading state" }));
  D.releaseLoading("the cockpit refused this device");
  return JSON.stringify({ busy: document.querySelectorAll('[aria-busy="true"]').length, needs: host.textContent, state: st.textContent,
                          loadingLines: document.querySelectorAll("p.empty.loading").length }); })()"""


@pytest.fixture(scope="module")
def cold_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp = tmp_path_factory.mktemp("cold_boot")
    _root, src, _ids = _install(tmp)
    steps = [{"init": _INIT},
             {"until": "document.documentElement.hasAttribute('data-board-whole')", "ms": 60000},
             {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"},
             {"eval": "JSON.stringify({s: window.__s, stored: window.__stored})"},
             {"eval": _PRECEDENCE},
             {"eval": _RELEASE}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        res = _drive(tmp, f"{url}/#write_token=T", steps)
    data = json.loads(res["values"][-3])
    data["precedence"] = json.loads(res["values"][-2])
    data["release"] = json.loads(res["values"][-1])
    return data


def test_the_wrap_line_the_vital_ring_and_the_memory_card_load_while_wraps_and_health_are_out(cold_run: dict) -> None:
    samples = cold_run["s"]
    out_wraps = [x for x in samples if not x["whole"] and x["wrapsShell"]]
    assert len(out_wraps) >= 5, "the run must have sampled the board while the wraps panel was still out"
    for x in out_wraps:
        # the band's wrap line says loading: never "none yet" or "a fresh install", which are claims about an install that has not answered
        assert x["wrap"].startswith("loading wraps"), x
        assert "No wraps yet" not in x["wrap"] and "fresh install" not in x["wrap"], x
        # the Memory card's Cognition keeps its tab row and the wraps shell, and its header says loading
        assert x["cogTabs"] == 1, x
        assert x["memMeta"] == "loading wraps…", x
    # the vital ring draws neither live nor dark while the health panel is still out
    health_out = [x for x in samples if x["healthLoading"]]
    assert len(health_out) >= 5
    assert all(x["vital"] == "" for x in health_out), [x for x in health_out if x["vital"] != ""][:2]
    # once the board is whole the loading is gone: a wrap line without the loading words and a vital that says live or dark
    last = samples[-1]
    assert last["whole"] and last["busy"] == 0 and last["shells"] == 0, last
    assert "loading" not in last["wrap"] and last["vital"] in ("live", "dark"), last


def test_a_panel_still_out_draws_its_loading_line_and_the_needs_strip_and_state_line_say_loading(cold_run: dict) -> None:
    samples = cold_run["s"]
    shells = [x for x in samples if x["shells"] > 0]
    assert shells and max(x["shells"] for x in shells) >= 20, "the panel shells: a status line under an aria-busy panel, one per panel out"
    needs = [x for x in samples if x["needsBusy"]]
    assert len(needs) >= 5 and all(x["needs"] == "loading what needs you…" for x in needs), needs[:2]
    state = [x for x in samples if x["stateBusy"]]
    assert len(state) >= 5 and all(x["state"] == "loading state…" for x in state), state[:2]
    # and the Needs strip and the state line stop saying so once their panels have landed (the whole board is the later of them)
    assert not samples[-1]["needsBusy"] and not samples[-1]["stateBusy"]


def test_a_panel_that_is_loading_and_failed_draws_its_failure_at_both_external_draw_sites(cold_run: dict) -> None:
    p = cold_run["precedence"]
    assert p["zone"] == {"err": True, "loading": False, "busy": None}, p
    assert p["bare"] == {"err": True, "loading": False, "busy": None}, p


def test_a_release_turns_every_shell_still_busy_into_its_failure(cold_run: dict) -> None:
    r = cold_run["release"]
    assert "missing" not in r, "the render core exports releaseLoading"
    assert r["busy"] == 0 and r["loadingLines"] == 0, r
    assert "refused this device" in r["needs"] and "refused this device" in r["state"], r


def test_a_cold_board_is_never_painted_from_a_cache_in_a_real_run(cold_run: dict) -> None:
    """The page keeps a few stores of its own (the open-tap trace cache, the service worker's token database); none may hold a panel."""
    stored = cold_run["stored"]
    for kind, key, value in stored:
        if kind == "indexedDB":
            assert key.startswith("levain-sw"), (kind, key)         # the service worker's own database, the only one the page opens
        elif kind.startswith("cache"):
            assert "/cockpit/" not in key and "panel" not in key.lower(), (kind, key)      # a cache entry is never a manifest or a panel read
        elif kind == "caches":
            assert key in ("levain-open", "levain-trace"), (kind, key)
        else:
            # a storage write is never a panel's body or a view of it (the write token and the page's own small settings are all it holds)
            assert '"rows"' not in value and '"as_of"' not in value and "panel" not in key.lower(), (kind, key, value[:80])
            assert len(value) < 2000, (key, len(value))


def test_an_edit_open_at_boot_does_not_strand_landed_panels_under_loading_shells(tmp_path: Path) -> None:
    """L2 MED-2: the rename editor opened right after the shells paint made the whole draw refuse, and every panel stayed a loading shell."""
    _root, src, _ids = _install(tmp_path)
    # the page itself opens the editor the moment its shells are up (a driver step would come after the reads had landed)
    init = r"""
    window.__edit = null;
    const of = window.fetch.bind(window);
    window.fetch = (u, o) => { const s = String(u && u.url || u); return /\/cockpit\/panel\//.test(s) ? new Promise((r) => setTimeout(r, 600)).then(() => of(u, o)) : of(u, o); };
    const iv = setInterval(() => {
      if (document.querySelectorAll('[aria-busy="true"] p.empty.loading').length < 4 || !document.getElementById("entity")) return;
      clearInterval(iv);
      // the rename editor: a `.name-editor` beside #entity, outside the regions the shells are drawn in
      const ed = document.createElement("div"); ed.className = "name-editor"; document.getElementById("entity").parentElement.appendChild(ed);
      window.__edit = window.LevainDashboard.hasUnsavedEdit();
    }, 10);
    """
    steps = [{"init": init},
             {"until": "document.documentElement.hasAttribute('data-board-whole')", "ms": 60000},
             {"eval": "JSON.stringify({edit: window.__edit, busy: document.querySelectorAll('[aria-busy=\"true\"]').length, "
                      "loading: document.querySelectorAll('p.empty.loading').length, panels: document.querySelectorAll('[data-ck-panel]').length})"}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        res = _drive(tmp_path, f"{url}/#write_token=T", steps)
    after = json.loads(res["values"][-1])
    assert after["edit"] is True, after      # the edit was open while the panels were still out
    assert after["busy"] == 0 and after["loading"] == 0 and after["panels"] > 3, after
