"""The cold cockpit draws every panel's SHELL at once, loading, then fills each panel as its read lands (levain#27).

Three layers, all under node with the real boot and the real view mapper and fake pages: the mapper's loading state (a panel still out
is a shell, never an empty panel; a failed read is its error; the Health panel waits on the wraps panel it reads), the boot's first paint
(shells before any read resolves, the Needs strip's panel read first, an open edit is never clobbered), and a source assertion that no
panel content is cached anywhere (no storage, no IndexedDB, no Cache API, no service-worker interception of /cockpit)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_cockpit_progressive_boot import _PAGE

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# ---- the mapper: loading is a third state --------------------------------------------------------------------------------------------
_MAPPER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
class N {
  constructor(tag) { this.tag = tag; this.kids = []; this.className = ""; this.attrs = {}; this.dataset = {}; this.text = "";
    const self = this; this.classList = { add: (c) => { self.className = (self.className + " " + c).trim(); }, contains: () => false, toggle() {}, remove() {} }; }
  set textContent(t) { this.text = String(t); this.kids = []; }
  get textContent() { return this.text; }
  appendChild(k) { this.kids.push(k); return k; }
  append(...ks) { for (const k of ks) this.appendChild(k); }
  replaceChildren() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener() {}
}
globalThis.document = { readyState: "complete", activeElement: null, body: new N("body"), addEventListener() {}, querySelectorAll: () => [],
  querySelector: () => null, createElement: (t) => new N(t), createElementNS: (ns, t) => new N(t), createTextNode: (t) => Object.assign(new N("#t"), { text: t }) };
new Function(src)();
const V = globalThis.LevainCockpitView;
const AT = "2026-10-12T12:00:00+00:00";
const head = (id, kind, title, extra) => Object.assign({ id, kind, title, status: "ok", as_of: AT }, extra || {});
const manifest = { schema: "levain.cockpit/2", entity: { name: "t", governance: "reported", brand: {} }, generated_at: AT,
  regions: { header: ["state"], zones: [{ id: "operate", title: "Operate", panels: ["tray", "health", "wraps", "decisions"] }] },
  panels: { state: head("state", "line", "State"), tray: head("tray", "list", "Tray"), health: head("health", "status-dots", "Health"),
            wraps: head("wraps", "visual", "Wraps"), decisions: head("decisions", "list", "Decisions") },
  verbs: {}, errors: [], transport: { writable: false } };
const body = {
  state: head("state", "line", "State", { value: { lines: [{ label: "state", text: "hi", at: "2026-10-12T11:00:00.000Z", source: "web" }], value_version: "v" } }),
  tray: head("tray", "list", "Tray", { rows: [], actions: [] }),
  health: head("health", "status-dots", "Health", { value: { metrics: [["episodes", 1], ["episodes since wrap", 1], ["tombstones", 0], ["wraps", 1], ["graduated", 0], ["demoted", 0],
    ["write path", "live"]].map(([label, value]) => ({ label, value, status: "ok" })) } }),
  wraps: head("wraps", "visual", "Wraps", { value: { data: [{ wrapped_at: "2026-10-12T10:00:00.000Z", continuity_chars: 5 }] } }),
  decisions: head("decisions", "list", "Decisions", { rows: [] }),
};
const ids = Object.keys(manifest.panels);
const draw = (landed) => {
  const panels = {};
  for (const id of ids) panels[id] = Object.prototype.hasOwnProperty.call(landed, id) ? landed[id] : { loading: true };
  const m = JSON.parse(JSON.stringify(manifest));
  return V.fromManifest({ manifest: m, panels }, { elapsedMs: 0 });
};
const layoutIds = ["tray", "health", "wraps", "decisions"];     // the panels of the layout (the state line is a header panel, drawn under the masthead)
const shape = (v, id) => JSON.stringify({ l: v.layout.filter((e) => e.id === id), x: v.extra_panels[id] });
const out = {};

const none = draw({});
out.none = { layout: none.layout.map((e) => [e.id, e.kind, e.zone]), ex: Object.fromEntries(Object.entries(none.extra_panels).map(([k, x]) => [k, x])),
  state: none.state, errors: none.errors, tray: none.tray, health: none.health };

// one panel lands: it fills, no other panel's entry or body moves
const one = draw({ decisions: body.decisions });
out.onlyItself = Object.fromEntries(layoutIds.map((id) => [id, shape(one, id) === shape(none, id)]));
out.stateUnmoved = JSON.stringify(one.state) === JSON.stringify(none.state);
out.decisionsNow = { ex: one.extra_panels.decisions, kind: one.layout.find((e) => e.id === "decisions").kind };

// a failed read is its error, not loading and not empty
const bad = draw({ tray: { error: "HTTP 500" } });
out.failed = bad.extra_panels.tray;

// Health reads the wraps panel: loading until wraps has landed, drawn once it has
const hOnly = draw({ health: body.health });
out.healthAlone = { loading: !!hOnly.extra_panels.health && hOnly.extra_panels.health.loading === true, health: hOnly.health };
const hBoth = draw({ health: body.health, wraps: body.wraps });
out.healthBoth = { exHealth: hBoth.extra_panels.health || null, lastWrap: hBoth.health && hBoth.health.last_wrap_at, ids: hBoth.layout.map((e) => [e.id, e.kind]) };
const wOnly = draw({ wraps: body.wraps });
out.wrapsAlone = { healthLoading: wOnly.extra_panels.health && wOnly.extra_panels.health.loading === true, wrapsKind: wOnly.layout.find((e) => e.id === "wraps").kind };

// the explicit marks: which panels are out (loading) and which were read and failed (failed), never inferred from an empty list
const keys = (o) => Object.keys(o).sort();
out.marks = { none: keys(none.loading), one: keys(one.loading), hOnly: keys(hOnly.loading), hBoth: keys(hBoth.loading), wOnly: keys(wOnly.loading),
              badLoading: keys(bad.loading), badFailed: keys(bad.failed), noneFailed: keys(none.failed), allLanded: keys(draw(body).loading) };

// the state line
out.stateLoading = draw({ tray: body.tray }).state;
out.stateLanded = draw({ state: body.state }).state && draw({ state: body.state }).state.text;
console.log(JSON.stringify(out));
"""


def _run(tmp_path: Path, name: str, source: str, *args: str) -> dict:
    f = tmp_path / name
    f.write_text(source, encoding="utf-8")
    run = subprocess.run([NODE, str(f), *args], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(run.stdout)


def test_the_mapper_draws_a_panel_still_out_as_loading_and_fills_only_the_one_that_lands(tmp_path: Path) -> None:
    out = _run(tmp_path, "mapper.js", _MAPPER, str(WEB / "cockpit_view.js"))
    none = out["none"]
    # every panel of the manifest has a shell, in its zone, nothing is empty and nothing is an error
    assert sorted(i for i, _k, _z in none["layout"]) == ["decisions", "health", "tray", "wraps"]
    assert all(k == "external" and z == "operate" for _i, k, z in none["layout"]), none["layout"]
    assert none["ex"] == {i: {"loading": True} for i in ["tray", "health", "wraps", "decisions"]}
    assert none["errors"] == {} and none["tray"] == [] and none["health"] is None
    # the masthead state line is its own loading, not "no state set"
    assert none["state"] == {"text": None, "loading": True}
    # a landing panel fills only itself: every other panel's entry and body is byte for byte what it was
    assert out["onlyItself"] == {"tray": True, "health": True, "wraps": True, "decisions": False}
    assert out["stateUnmoved"] is True      # the state line is its own panel: another panel landing leaves it loading
    assert "loading" not in out["decisionsNow"]["ex"]
    # a failed read keeps its error, which is neither the loading state nor an empty panel
    assert out["failed"].get("error", "").startswith("HTTP 500") and "loading" not in out["failed"]
    # a cross-panel value is drawn only when its input panel has landed: Health waits on wraps, and draws once wraps is in
    assert out["healthAlone"] == {"loading": True, "health": None}
    assert out["healthBoth"]["lastWrap"] == "2026-10-12T10:00:00.000Z"
    assert out["healthBoth"]["exHealth"] is None
    assert out["wrapsAlone"] == {"healthLoading": True, "wrapsKind": "wraps"}
    assert out["stateLoading"] == {"text": None, "loading": True}
    assert out["stateLanded"] == "hi"
    # the page's figures drawn outside a panel (the band's wrap line, the vital ring, the Memory card) read these marks, never an empty list
    m = out["marks"]
    assert m["none"] == ["decisions", "health", "state", "tray", "wraps"] and m["noneFailed"] == []
    assert m["one"] == ["health", "state", "tray", "wraps"]
    assert m["hOnly"] == ["decisions", "health", "state", "tray", "wraps"]          # health landed, but it waits on wraps
    assert m["hBoth"] == ["decisions", "state", "tray"]
    assert m["wOnly"] == ["decisions", "health", "state", "tray"]
    assert m["badLoading"] == ["decisions", "health", "state", "wraps"] and m["badFailed"] == ["tray"]
    assert m["allLanded"] == []


# ---- the boot: the first paint ---------------------------------------------------------------------------------------------------------
_PREFIX = _PAGE[: _PAGE.index("(async () => {\n  const out = {};")]
_BOOT = _PREFIX + r"""
(async () => {
  const out = {};
  const quiet = console.warn; console.warn = () => {};
  const loadingOf = (v) => Object.keys(v.extra_panels).filter((k) => v.extra_panels[k].loading === true).sort();
  // every panel read held: the first paint is the shells, and no read has landed
  { const e = await makeEnv({ holdAll: true }).start();
    const v = e.renders[0];
    out.held = { renders: e.renders.length, whole: e.whole(), reads: e.reads.slice(), loading: v ? loadingOf(v) : null, state: v ? v.state : null,
                 layout: v ? v.layout.map((x) => x.id).sort() : null, inert: e.inert(), store: e.store.textContent };
    out.heldCold = e.store.hasAttribute("data-cold");
    e.release(); await settle();
    const f = e.renders[e.renders.length - 1];
    out.landed = { whole: e.whole(), loading: loadingOf(f), state: f.state && f.state.text, extra: Object.keys(f.extra_panels).sort() }; }
  // a read that fails leaves its error, never loading
  { const e = await makeEnv({ fail: "loops" }).start();
    const f = e.renders[e.renders.length - 1];
    out.failed = { whole: e.whole(), loops: f.extra_panels.loops, loading: loadingOf(f) }; }
  // an edit in progress: the shells do not clobber it
  { const e = await makeEnv({ holdAll: true, editing: true }).start();
    out.editing = { renders: e.renders.length }; }
  console.warn = quiet;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def test_the_first_paint_is_every_shell_loading_before_any_read_resolves(tmp_path: Path) -> None:
    out = _run(tmp_path, "boot.js", _BOOT, str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"))
    h = out["held"]
    # all three panels of the fixture's manifest are drawn as shells while none of their reads has answered, and the board is not whole
    assert h["renders"] == 1 and h["whole"] is False, h
    assert h["layout"] == ["decisions", "loops"] and h["loading"] == ["decisions", "loops"], h
    assert h["state"] == {"text": None, "loading": True}, h
    assert out["heldCold"] is False          # "connecting..." went with the shells
    # when the reads land the board is whole and nothing is left loading
    assert out["landed"]["whole"] is True and out["landed"]["loading"] == [] and out["landed"]["state"] == "hi", out["landed"]
    # a failed read is its own error on the whole board
    assert out["failed"]["whole"] is True and out["failed"]["loading"] == []
    assert str(out["failed"]["loops"]["error"]).startswith("HTTP 500")
    # an edit in progress holds: no shell is drawn over it
    assert out["editing"]["renders"] == 0


def test_the_needs_panel_is_the_first_read_issued(tmp_path: Path) -> None:
    out = _run(tmp_path, "boot.js", _BOOT, str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"))
    # the fixture lists state, loops, decisions: decisions (the NEEDS YOU strip's panel) is asked for first
    assert out["held"]["reads"] == ["decisions", "state", "loops"], out["held"]["reads"]


def test_the_needs_panel_constant_names_a_real_panel_of_the_servers_manifest(tmp_path: Path) -> None:
    import os
    import tempfile
    import urllib.request

    from tests.test_cockpit_k2a import _install, _serve

    os.environ.setdefault("LEVAIN_HOME", tempfile.mkdtemp(prefix="boot_needs_"))
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        req = urllib.request.Request(url + "/cockpit/manifest.json", headers={"X-Levain-Write-Token": "T"})
        man = json.loads(urllib.request.urlopen(req, timeout=30).read())   # noqa: S310 - loopback
    boot = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
    m = re.search(r"const NEEDS_PANELS = (\[[^\]]*\]);", boot)
    assert m, "the boot names its needs panels"
    needs = json.loads(m.group(1))
    assert needs and all(n in man["panels"] for n in needs), (needs, sorted(man["panels"]))
    # the panel is the one the page's band is drawn from (the manifest puts it in the page's top)
    assert all(n in man["regions"]["header"] for n in needs)


# ---- no stale cache ---------------------------------------------------------------------------------------------------------------------
def test_no_panel_content_is_cached_by_the_page_or_a_service_worker() -> None:
    for name in ("dashboard_boot.js", "cockpit_view.js", "cockpit_stream.js"):
        src = (WEB / name).read_text(encoding="utf-8")
        assert not re.search(r"indexedDB|\bcaches\s*\.|serviceWorker", src), name
    # the one place a panel body is held is the boot's in-memory conditional-read map, kept with its ETag, never written to a store
    boot = (WEB / "dashboard_boot.js").read_text(encoding="utf-8")
    assert "const panelBodies = new Map()" in boot
    for m in re.finditer(r"(?:localStorage|sessionStorage)\.setItem\(([^,]+),", boot + (WEB / "cockpit_stream.js").read_text(encoding="utf-8")):
        assert "panel" not in m.group(1).lower() and "body" not in m.group(1).lower(), m.group(0)
    # the service worker leaves /cockpit (data) alone
    sw = (WEB.parent.parent / "webpush" / "static" / "sw.js").read_text(encoding="utf-8")
    assert re.search(r"const NO_INTERCEPT = \[[^\]]*'/cockpit'", sw)


# ---- the boot: an edit, a second read of a cold board, a refusal, the watchdog -------------------------------------------------------------
_BOOT2 = _PREFIX + r"""
(async () => {
  const out = {};
  const quiet = console.warn; console.warn = () => {};
  const loadingOf = (v) => Object.keys(v.extra_panels).filter((k) => v.extra_panels[k].loading === true).sort();
  const lastOf = (e) => e.renders[e.renders.length - 1];

  // S1: an edit opens after the shells are drawn and before any read lands. The shells are not a part: the whole draw is not refused for it,
  // so the landed panels are not stranded under loading shells
  { const e = await makeEnv({ holdAll: true }).start();
    e.setEditing(true); e.release(); await settle();
    out.s1 = { renders: e.renders.length, whole: e.whole(), loading: loadingOf(lastOf(e)) }; }

  // S1b: the edit guard keeps its meaning: while an edit is open no landing panel is painted over it (the shells were drawn, a panel lands,
  // its partial draw comes due and is held back), and the whole draw comes only when every read is in
  { const e = await makeEnv({ holdLoops: true }).start();
    e.setEditing(true); await sleep(450);
    out.s1b = { rendersWhileEditing: e.renders.length };
    e.release(); await settle();
    out.s1bAfter = { renders: e.renders.length, whole: e.whole() }; }

  // S2: panels landed (a part drawn), an edit opens, every read finishes: the whole draw is refused for the edit. When the edit ends the re-read
  // starts, and the board is painted from what had landed AT ONCE (every read of the re-read still out), not left on the old part until it is whole
  { const e = await makeEnv({ holdLoops: true }).start();
    await sleep(450);
    e.setEditing(true); e.release(); await settle();
    out.s2Refused = { whole: e.whole(), loading: loadingOf(lastOf(e)) };
    const before = e.renders.length;
    e.hold(); e.setEditing(false); e.listeners["levain:editor-closed"][0](); await sleep(200);
    out.s2Reread = { added: e.renders.length - before, whole: e.whole(), loading: loadingOf(lastOf(e)) };
    e.open(); await settle();
    out.s2Whole = { whole: e.whole(), loading: loadingOf(lastOf(e)) }; }

  // S3: a second read of a cold board (the first superseded by a foreground after it had gone quiet) keeps the progressive path: the shells and what
  // had landed are drawn at once while its own reads are out
  { const e = await makeEnv({ holdLoops: true }).start();
    await sleep(450);
    const before = e.renders.length, firstLoading = loadingOf(lastOf(e));
    e.hold();
    e.listeners["levain:resume"][0]({ detail: { hiddenAt: Date.now() + 5000 } }); await sleep(200);
    out.s3 = { firstLoading, added: e.renders.length - before, loading: loadingOf(lastOf(e)), whole: e.whole() };
    e.open(); e.release(); await settle();
    out.s3Whole = { whole: e.whole(), loading: loadingOf(lastOf(e)) }; }

  // S4: the shells are drawn, then the board's read is refused with no retry coming: every shell is released as a failure, never left loading
  { const e = await makeEnv({ holdLoops: true }).start();
    e.armRefuse(1);
    e.listeners["levain:resume"][0]({ detail: { hiddenAt: Date.now() + 5000 } }); await settle();
    out.s4 = { released: e.released.slice(), whole: e.whole() }; }

  // S5: the watchdog is a stretch with no panel answering, not a clock from the shell draw: it starts again at every panel that settles, and
  // when it does fire every shell still loading is released with the reason
  { const e = await makeEnv({ holdAll: true }).start();
    out.s5Armed = e.holdArms;
    e.release(); await settle();
    out.s5AfterReads = { arms: e.holdArms, hold: e.timers.filter((t) => t.ms === HOLD_MS).length }; }
  { const e = await makeEnv({ holdLoops: true }).start();
    await e.fireHold();
    out.s5Fired = e.released.slice(); }
  console.warn = quiet;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.fixture(scope="module")
def boot2(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _run(tmp_path_factory.mktemp("boot2"), "boot2.js", _BOOT2, str(WEB / "cockpit_view.js"), str(WEB / "dashboard_boot.js"))


def test_an_edit_open_after_the_shells_does_not_strand_the_landed_panels_under_loading_shells(boot2: dict) -> None:
    assert boot2["s1"] == {"renders": 2, "whole": True, "loading": []}, boot2["s1"]


def test_an_edit_in_progress_is_never_painted_over_by_a_landing_panel(boot2: dict) -> None:
    # the shells were drawn (1), a panel landed and its partial draw came due while the edit was open: nothing was painted over it
    assert boot2["s1b"]["rendersWhileEditing"] == 1, boot2["s1b"]
    assert boot2["s1bAfter"] == {"renders": 2, "whole": True}, boot2["s1bAfter"]


def test_what_landed_while_an_edit_deferred_the_whole_draw_is_drawn_when_the_edit_ends_without_waiting_for_a_full_re_read(boot2: dict) -> None:
    assert boot2["s2Refused"]["whole"] is False and boot2["s2Refused"]["loading"] == ["loops"], boot2["s2Refused"]
    # the re-read's reads are all still out, and the board has already been painted from the panels that landed before
    assert boot2["s2Reread"]["added"] >= 1 and boot2["s2Reread"]["whole"] is False and boot2["s2Reread"]["loading"] == [], boot2["s2Reread"]
    assert boot2["s2Whole"] == {"whole": True, "loading": []}, boot2["s2Whole"]


def test_a_second_read_of_a_cold_board_keeps_the_shells_and_the_panels_that_landed(boot2: dict) -> None:
    s3 = boot2["s3"]
    assert s3["firstLoading"] == ["loops"], s3
    assert s3["added"] >= 1 and s3["loading"] == ["loops"] and s3["whole"] is False, s3
    assert boot2["s3Whole"] == {"whole": True, "loading": []}, boot2["s3Whole"]


def test_a_refusal_after_the_shells_releases_them_as_a_failure(boot2: dict) -> None:
    assert boot2["s4"]["released"] == ["the cockpit refused this device"], boot2["s4"]
    assert boot2["s4"]["whole"] is True


def test_the_watchdog_restarts_at_each_panel_that_answers_and_releases_the_shells_when_it_fires(boot2: dict) -> None:
    assert boot2["s5Armed"] == 1                                    # armed by the shell draw, before any read
    after = boot2["s5AfterReads"]
    assert after["arms"] >= 4 and after["hold"] == 0, after         # again at each of the three panels that settled; cleared by the whole draw
    assert boot2["s5Fired"] == ["no answer in 60 s"], boot2["s5Fired"]


# ---- no cache: the boot path's code, comments set aside ---------------------------------------------------------------------------------
def _code_only(src: str) -> str:
    """The source without its comments and string literals (a word in a comment or a message is not a use)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    src = re.sub(r"(?<![:\"'\\])//[^\n]*", "", src)
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', src)


def test_no_boot_path_code_names_a_cache_store() -> None:
    for name in ("dashboard_boot.js", "cockpit_view.js", "cockpit_stream.js"):
        code = _code_only((WEB / name).read_text(encoding="utf-8"))
        assert not re.search(r"\b(indexedDB|caches|CacheStorage|serviceWorker|IDBFactory|cacheStorage)\b", code), name
        # the only storage the boot reads and writes by name is the write token's and the page's own small settings, never a panel
        for m in re.finditer(r"\b(?:localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*([^,)]*)", code):
            assert "panel" not in m.group(1).lower() and "body" not in m.group(1).lower(), (name, m.group(0))


def test_the_code_filter_sees_a_cache_use_and_not_a_word_in_a_comment() -> None:
    assert re.search(r"\bcaches\b", _code_only("const c = await caches.open(NAME);"))
    assert not re.search(r"\bcaches\b", _code_only('// caches are not used\nconst m = "caches"; /* caches */'))
