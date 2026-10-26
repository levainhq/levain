"""The Health panel's save stamp reaches the reader as an age, and the age keeps moving on its own.

The kernel sends ``last save at`` (a stamp) instead of an age, so an idle store never changes the panel's etag. The
page draws the age from the stamp (K1), redraws it on one page-owned interval against the server's clock (K2), and
reads a missing or unreadable stamp as "unknown" (K4). The node tests use the page's fake DOM; the browser test
drives the real default cockpit in headless Chrome."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from anneal_memory import Store

from tests._ke_dom import NODE, run_node

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

BODY = r"""
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};      // the age timer never ends on its own: a real one would keep node alive
const GEN = "2026-10-12T12:00:00+00:00";
const regions = { hero: [], brief: [], widgets: ["health"], header: [], zones: [{ id: "operate", title: "Work", grid: 3, panels: [], cards: [] }] };
const healthPanel = (stamp) => ({ metrics: [
  { label: "write path", value: "live", status: "ok" }, { label: "wraps", value: 1, status: "unknown" },
  ...(stamp === undefined ? [] : [{ label: "last save at", value: stamp, status: "unknown", detail: "the wrap at the stamp" }]) ] });
const draw = (stamp, ctx) => {
  const heads = { health: head("health", "status-dots", "Health") };
  const m = fixture(heads, { health: head("health", "status-dots", "Health", { value: healthPanel(stamp) }) }, { regions });
  const v = V.fromManifest(m);
  const host = new N("div");
  K.fill(host, v.extra_panels.health, Object.assign({ entry: { title: "health" } }, ctx || {}));
  return { v, host, text: host.textContent };
};
"""


def test_the_stamp_is_drawn_as_an_age_not_as_the_raw_stamp(tmp_path: Path) -> None:
    out = run_node(BODY + r"""
const r = draw("2026-10-12T11:55:00+00:00");
out.text = r.text; out.labels = r.v.extra_panels.health.metrics.map((m) => m.label);
""", tmp_path)
    assert "5m ago" in out["text"] and "last save" in out["text"]
    assert "2026-10-12T11:55" not in out["text"] and "last save at" not in out["labels"]


@pytest.mark.parametrize("stamp", ["null", "undefined", '"garbage"', '""', "12"])
def test_a_missing_or_unreadable_stamp_reads_unknown(tmp_path: Path, stamp: str) -> None:
    out = run_node(BODY + f"""
const r = draw({stamp});
out.text = r.text;
""", tmp_path)
    if stamp == "undefined":
        assert "last save" not in out["text"]       # the kernel left the metric out: nothing is drawn for it
    else:
        assert "last save" in out["text"] and "unknown" in out["text"] and "NaN" not in out["text"]


def test_the_age_moves_on_the_pages_own_timer_with_no_server_event(tmp_path: Path) -> None:
    out = run_node(BODY + r"""
const timers = [];
globalThis.setInterval = (fn, ms) => { timers.push(ms); return timers.length; };
globalThis.clearInterval = () => {};
N.prototype.isConnected = true;
const r = draw("2026-10-12T11:55:00+00:00");
const aged = () => r.host.all((n) => n.dataset && n.dataset.ageAt);
document.querySelectorAll = (sel) => (sel === "[data-age-at]" ? aged() : []);
out.before = r.text;
out.nodes = aged().length;
const real = Date.now; Date.now = () => real() + 3600000;       // an hour passes; no snapshot arrives
K.tickAges();
out.after = r.host.textContent;
out.timers = timers; out.tick = K.AGE_TICK_MS; out.armed = K.ageTimerArmed();
Date.now = real;
""", tmp_path)
    assert "5m ago" in out["before"] and "1h ago" in out["after"] and "5m ago" not in out["after"]
    assert out["nodes"] == 1 and out["timers"] == [out["tick"]], "one interval, the page's own constant"
    assert out["armed"] is True


def test_the_native_health_component_reads_a_null_stamp_as_no_age(tmp_path: Path) -> None:
    out = run_node(r"""
const metrics = (stamp) => ({ metrics: [{ label: "write path", value: "live" }, { label: "episodes", value: 3 }, { label: "episodes since wrap", value: 1 },
  { label: "tombstones", value: 0 }, { label: "wraps", value: 1 }, { label: "graduated", value: 0 }, { label: "demoted", value: 0 },
  { label: "last save at", value: stamp }] });
const age = (stamp) => V.fromManifest(fixture({ health: head("health", "metric", "Health") },
  { health: head("health", "metric", "Health", { value: metrics(stamp) }) })).health.save_age_hours;
out.nul = age(null); out.bad = age("garbage"); out.ok = age("2026-10-12T10:00:00+00:00");
""", tmp_path)
    assert out["nul"] is None and out["bad"] is None and 1.99 < out["ok"] < 2.01


def test_a_tick_keeps_the_row_tooltips_detail_and_changes_only_the_age(tmp_path: Path) -> None:
    out = run_node(BODY + r"""
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
N.prototype.isConnected = true;
const r = draw("2026-10-12T11:55:00+00:00");
const aged = () => r.host.all((n) => n.dataset && n.dataset.ageAt);
document.querySelectorAll = (sel) => (sel === "[data-age-at]" ? aged() : []);
const rows = () => r.host.all((n) => n.title && /last save/.test(n.title));
for (const row of rows()) for (const k of row.kids) k.parentNode = row;      // the fake DOM keeps no parent link
out.before = rows().map((n) => n.title);
const real = Date.now; Date.now = () => real() + 3600000;
K.tickAges();
out.after = rows().map((n) => n.title); out.text = r.host.textContent;
Date.now = real;
""", tmp_path)
    assert out["before"] and "5m ago" in out["before"][0]
    assert "1h ago" in out["after"][0] and "5m ago" not in out["after"][0]
    assert "the wrap at the stamp" in out["before"][0] and "the wrap at the stamp" in out["after"][0], out["after"]


def test_an_aged_rows_reading_rides_in_the_tooltip_never_the_cell(tmp_path: Path) -> None:
    """K10d: a save row with a non-empty `read` shows the age alone, before and after a tick; the reading is in the tooltip."""
    out = run_node(BODY + r"""
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
N.prototype.isConnected = true;
const panel = { metrics: [{ label: "write path", value: "live", status: "ok" },
  { label: "last save at", value: "2026-10-12T11:55:00+00:00", status: "unknown", read: "the wrap READING", detail: "the wrap at the stamp" }] };
const heads = { health: head("health", "status-dots", "Health") };
const m = fixture(heads, { health: head("health", "status-dots", "Health", { value: panel }) }, { regions });
const host = new N("div");
K.fill(host, V.fromManifest(m).extra_panels.health, { entry: { title: "health" } });
const aged = () => host.all((n) => n.dataset && n.dataset.ageAt);
document.querySelectorAll = (sel) => (sel === "[data-age-at]" ? aged() : []);
const rows = () => host.all((n) => n.title && /last save/.test(n.title));
for (const row of rows()) for (const k of row.kids) k.parentNode = row;
out.cell0 = aged()[0].textContent; out.title0 = rows()[0].title;
const real = Date.now; Date.now = () => real() + 3600000;
K.tickAges();
out.cell1 = aged()[0].textContent; out.title1 = rows()[0].title;
Date.now = real;
""", tmp_path)
    assert out["cell0"] == "5m ago" and out["cell1"] == "1h ago", out
    for t in (out["title0"], out["title1"]):
        assert "the wrap READING" in t and "the wrap at the stamp" in t, t


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_the_default_cockpits_health_panel_shows_an_age_that_moves_in_a_real_page(tmp_path: Path) -> None:
    """K1/K2 on the real path: the default cockpit's own Health panel, in headless Chrome, opened in its focus view."""
    from levain.web_server import make_server
    from tests._page_drive_run import run as run_driver
    from tests.test_cockpit_k1 import _install

    _root, src = _install(tmp_path)
    md = src.anneal.continuity_md
    md.write_text("## State\nfoc\n", encoding="utf-8")
    with Store(src.anneal.episodic_db) as store:
        e = store.record("episode A", "decision")
        store.wrap_started(token="w1", episode_ids=[e.id])
        store.wrap_completed(1, 20, wrap_token="w1", episode_ids=[e.id], pair_id="p1",
                             content_hash=hashlib.sha256(md.read_bytes()).hexdigest())
    httpd = make_server(src, host="127.0.0.1", port=0, write_token="T")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    grab = ("(function(){var d=document.querySelector('[role=dialog]');if(!d)return null;var t=d.innerText;"
            "var i=t.search(/last save/i);return i<0?null:t.slice(i,i+60)})()")
    steps = [
        {"until": "document.querySelector('[data-ck-panel=health]') !== null", "ms": 20000},
        {"eval": "(document.querySelector('[data-ck-panel=health]').click(), 'opened')"},
        {"until": grab, "ms": 10000},
        {"eval": "LevainCockpitKinds.ageTimerArmed()"},
        # two hours pass on the page's clock with no server event, then the page's own tick runs
        {"eval": "(window.__real = Date.now, Date.now = function(){ return window.__real() + 7200000 }, LevainCockpitKinds.tickAges(), 'ticked')"},
        {"eval": grab},
        {"eval": "(function(){var n=document.querySelector('[data-age-at]');return n&&n.parentNode?n.parentNode.title:null})()"},
    ]
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(steps))
    driver = str(Path(__file__).resolve().parent / "_page_drive.mjs")
    try:
        r = run_driver([str(__import__("shutil").which("node")), driver, CHROME,
                        f"http://127.0.0.1:{httpd.server_address[1]}/?source=manifest#write_token=T", str(steps_file)], timeout=120)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    assert r.returncode == 0, r.stderr
    vals = json.loads(r.stdout)["values"]
    before, armed, after = vals[2], vals[3], vals[5]
    assert before and "ago" in before and "T" not in before.split("\n", 1)[-1], before
    assert armed is True
    assert "2h ago" in after and "ago" in after, after
    title = vals[6]
    assert title and title.startswith("last save: 2h ago"), title      # the tooltip kept its label and moved only the age


def _drive_default_cockpit(tmp_path: Path, steps: list, query: str = "?source=manifest") -> list:
    """The default cockpit over a store with one committed wrap, in headless Chrome: the values of `steps`."""
    from levain.web_server import make_server
    from tests._page_drive_run import run as run_driver
    from tests.test_cockpit_k1 import _install

    _root, src = _install(tmp_path)
    md = src.anneal.continuity_md
    md.write_text("## State\nfoc\n", encoding="utf-8")
    with Store(src.anneal.episodic_db) as store:
        e = store.record("episode A", "decision")
        store.wrap_started(token="w1", episode_ids=[e.id])
        store.wrap_completed(1, 20, wrap_token="w1", episode_ids=[e.id], pair_id="p1",
                             content_hash=hashlib.sha256(md.read_bytes()).hexdigest())
    httpd = make_server(src, host="127.0.0.1", port=0, write_token="T")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(steps))
    driver = str(Path(__file__).resolve().parent / "_page_drive.mjs")
    try:
        r = run_driver([str(__import__("shutil").which("node")), driver, CHROME,
                        f"http://127.0.0.1:{httpd.server_address[1]}/{query}#write_token=T", str(steps_file)], timeout=120)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["values"]


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_the_age_timer_runs_for_the_pages_life_and_ticks_only_what_is_attached(tmp_path: Path) -> None:
    """K10a on a real DOM: a subtree not yet attached is not ticked (the browser never returns it), the same subtree
    attached is, and the timer is armed still after a tick that found no age at all."""
    js = ("(function(){var K=LevainCockpitKinds;var armed0=K.ageTimerArmed();"
          "var stamp=new Date(Date.now()-330000).toISOString();"
          "var wrap=document.createElement('div');var n=document.createElement('span');n.dataset.ageAt=stamp;n.textContent='stale';"
          "wrap.appendChild(n);"
          "K.tickAges();var detached=n.textContent;"
          "document.body.appendChild(wrap);K.tickAges();var attached=n.textContent;"
          "wrap.remove();var all=document.querySelectorAll('[data-age-at]');for(var i=0;i<all.length;i++)all[i].removeAttribute('data-age-at');"
          "K.tickAges();"
          "return JSON.stringify({armed0:armed0,detached:detached,attached:attached,armedAfterEmptyTick:K.ageTimerArmed()})})()")
    vals = _drive_default_cockpit(tmp_path, [
        {"until": "document.querySelector('[data-ck-panel=health]') !== null", "ms": 20000},
        {"eval": "(document.querySelector('[data-ck-panel=health]').click(), 'opened')"},
        {"until": "document.querySelector('[data-age-at]') !== null", "ms": 10000},
        {"eval": js},
    ])
    got = json.loads(vals[3])
    assert got["armed0"] is True
    assert got["detached"] == "stale" and got["attached"] == "5m ago", got
    assert got["armedAfterEmptyTick"] is True, got


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
def test_the_native_health_panels_last_save_is_an_age_that_ticks(tmp_path: Path) -> None:
    """K10c: the native Health card (the legacy /substrate.json page) draws its "last save" through the shared age node (a stamp the page keeps current),
    not once as "Nh ago" text."""
    js = ("(function(){var spans=Array.from(document.querySelectorAll('[data-age-at]')).filter(function(n){"
          "return n.parentNode&&n.parentNode.previousSibling&&n.parentNode.previousSibling.textContent==='substrate'});"
          "if(!spans.length)return JSON.stringify({found:false});"
          "var n=spans[0];var real=Date.now;Date.now=function(){return real()+7200000};LevainCockpitKinds.tickAges();Date.now=real;"
          "return JSON.stringify({found:true,text:n.textContent,row:n.parentNode.textContent})})()")
    vals = _drive_default_cockpit(tmp_path, [
        {"until": "Array.from(document.querySelectorAll('.kv .k')).some(function(n){return n.textContent==='substrate'})", "ms": 20000},
        {"eval": js},
    ], query="?source=substrate")
    got = json.loads(vals[1])
    assert got["found"] is True, got
    assert got["text"] == "2h ago" and got["row"].startswith("last save 2h ago"), got
