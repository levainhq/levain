"""R1 (the dashboard drawn from the manifest, read half) and R2 (TUI renderer) of the shared cockpit (design
``cockpit_manifest_DESIGN_1009.md`` §9 rows R1/R2, §7). DONE cases discharged here, none needing a write:

* an injected provider fault shows its error text (TUI lines, JS renderer);
* a deliberately non-natural order (ids reversed) shows that order and those groups in BOTH renderers.

The JS renderer runs under node against a minimal fake DOM; the page is also fetched from a live server
to prove the assets are served and that the manifest it draws is the one the kernel sent. Fixtures are
synthetic."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import Cockpit, Fault, ProviderSpec, Read, RowIn, register_ordering
from levain.cockpit.registry import Ordering
from levain.cockpit.text import render_lines, snapshot, visible

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
NODE = shutil.which("node")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# A DELIBERATELY NON-NATURAL ordering: ids descending, two groups assigned by id parity. The natural
# kernel orderings sort by due date / tier; a renderer that re-sorts would show that instead.
try:
    register_ordering(Ordering(
        name="test.reversed",
        key=lambda row, today: (tuple(-ord(c) for c in row.id),),
        group=lambda row, today: "odd" if int(row.id.split("-")[1]) % 2 else "even",
        groups=(("odd", "Odd ids"), ("even", "Even ids")),
    ))
except Exception:  # noqa: BLE001 - registered once per process
    pass


def _cockpit(*, fault: bool = False) -> Cockpit:
    ck = Cockpit(entity=lambda ctx: {"name": "test entity", "governance": "x",
                                      "brand": {"wordmark": "W", "model": "M"}})
    rows = [RowIn(f"r-{i}", f"title {i}", {"at": f"2026-10-0{i}T00:00:00+00:00"}, stored={"id": f"r-{i}"})
            for i in range(1, 6)]
    ck.register(ProviderSpec(
        "items", "triage-list", "Items", "gate", lambda c: Read(rows=tuple(rows)),
        order="test.reversed", facets=frozenset({"at"}), version_fields=("id",), region="operate"))
    if fault:
        ck.register(ProviderSpec("broken", "triage-list", "Broken", "gauge",
                                 lambda c: Fault("source exploded: disk on fire"),
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",),
                                 region="mind"))
    return ck


def _titles(lines: list[tuple[str, str]]) -> list[str]:
    return [t.strip() for s, t in lines if s == "row"]


def _flip_cockpit() -> Cockpit:
    """A source that reads fine for the manifest and fails for the next read of the panel."""
    ck = Cockpit()
    calls = {"n": 0}

    def read(ctx):
        calls["n"] += 1
        return Read(rows=()) if calls["n"] == 1 else Fault("disk failed after the manifest")
    ck.register(ProviderSpec("flip", "triage-list", "Flip", "gate", read, order="time.desc",
                             facets=frozenset({"at"}), version_fields=("id",), stale_after_s=0.0))
    return ck


def _cached_head_then_error() -> dict[str, Any]:
    """A snapshot whose manifest head is `ok` while the panel payload, read after it, is `error`."""
    ck = _flip_cockpit()
    manifest = ck.manifest({"class": "none", "device_class": None})
    panel = ck.panel("flip")
    snap = {"manifest": manifest, "panels": {"flip": panel}}
    assert manifest["panels"]["flip"]["status"] != "error" and panel["status"] == "error"
    return snap


class TestTuiRenderer:
    def test_spaces_are_not_escaped_and_format_characters_are(self) -> None:
        assert visible("a b\u061cc\u206ad\u2028e") == "a b<U+061C>c<U+206A>d<U+2028>e"
        assert visible("x\ud800y") == "x<U+D800>y"

    def test_prose_splits_only_on_newline_so_separators_stay_visible(self) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec("pr", "prose", "Doc", "gauge",
                                 lambda c: Read(value={"markdown": "safe\u2028ERROR: forged\nnext", "headline": "H"})))
        rows = [t for s, t in render_lines(snapshot(ck), 100) if s == "row"]
        assert rows[1].strip() == "safe<U+2028>ERROR: forged" and rows[2].strip() == "next"

    def test_head_and_payload_disagreement_shows_the_payloads_error(self) -> None:
        text = "\n".join(t for _, t in render_lines(_cached_head_then_error(), 100))
        assert "disk failed after the manifest" in text

    def test_a_bad_timestamp_is_still_sanitised(self) -> None:
        from levain.cockpit.text import _age
        assert _age("bad\u202e", datetime.now(timezone.utc)) == "bad<U+202E>"

    def test_non_natural_order_and_groups_are_shown_as_served(self) -> None:
        ck = _cockpit()
        served = [r["id"] for r in ck.panel("items")["rows"]]
        assert served != sorted(served), "fixture must be non-natural"
        lines = render_lines(snapshot(ck), 100)
        assert [t for t in _titles(lines)] == [f"title {i.split('-')[1]}" for i in served]
        groups = [t.strip() for s, t in lines if s == "group"]
        assert groups == ["Odd ids (3)", "Even ids (2)"]

    def test_a_provider_fault_shows_its_error_text(self) -> None:
        lines = render_lines(snapshot(_cockpit(fault=True)), 100)
        text = "\n".join(t for _, t in lines)
        assert "Broken (unavailable)  [error]" in text
        assert "disk on fire" in text
        assert any(s == "error" and "last good" in t for s, t in lines)

    def test_top_n_cut_says_how_many_it_hid(self) -> None:
        lines = render_lines(snapshot(_cockpit()), 100, top_n=1)
        assert len(_titles(lines)) == 2            # one per group
        more = sorted(t.strip() for s, t in lines if s == "dim" and "more" in t)
        assert more == ["+1 more (cut by this view)", "+2 more (cut by this view)"]

    def test_control_and_bidi_characters_are_visible(self) -> None:
        assert visible("a‮b\x1b[2Jc") == "a<U+202E>b<U+001B>[2Jc"

    def test_now_view_rides_the_manifest_order(self) -> None:
        manifest = snapshot(_cockpit())["manifest"]
        assert manifest["regions"]["zones"][0]["panels"][0] == "now"


def _tray_cockpit(*, fault: bool = False, stale: bool = False) -> Cockpit:
    """The kernel's own `tray` id (so the dashboard's native Tray component takes it) over the
    deliberately non-natural test ordering, plus optional fault / stale panels."""
    ck = Cockpit(entity=lambda ctx: {"name": "test entity", "governance": "x", "brand": {"wordmark": "W", "model": "M"}})
    rows = [RowIn(f"spore:spore-{i}", f"title {i}", {"at": f"2026-10-0{i}T00:00:00+00:00"}, stored={"id": f"spore-{i}"})
            for i in range(1, 6)]
    ck.register(ProviderSpec("tray", "triage-list", "Tray", "gate", lambda c: Read(rows=tuple(rows)),
                             order="test.reversed", facets=frozenset({"at"}), version_fields=("id",)))
    if fault:
        ck.register(ProviderSpec("broken", "triage-list", "Broken", "gauge", lambda c: Fault("source exploded: disk on fire"),
                                 order="time.desc", facets=frozenset({"at"}), version_fields=("id",), region="mind"))
    if stale:
        ck.register(ProviderSpec("old", "line", "Old", "gauge", lambda c: Read(value={"lines": [
            {"label": "a", "text": "b", "at": None, "source": None}]}), refresh_every_s=10, stale_after_s=1, region="mind"))
    return ck


def _view(snap: dict[str, Any]) -> dict[str, Any]:
    driver = (
        "const V=require(process.argv[1]);const snap=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify(V.fromManifest(snap, Date.parse('2026-10-09T12:00:00Z'))))")
    res = subprocess.run([NODE, "-e", driver, str(WEB / "cockpit_view.js")], input=json.dumps(snap),
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(res.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestManifestMapper:
    """cockpit_view.js builds the dashboard's view from the manifest and re-orders nothing."""

    def test_non_natural_order_and_groups_reach_the_view_as_served(self) -> None:
        ck = _tray_cockpit()
        served = [r["id"] for r in ck.panel("tray")["rows"]]
        assert served != sorted(served)
        view = _view(snapshot(ck))
        assert [f"spore:{s['id']}" for s in view["tray"]] == served
        assert [s["group_title"] for s in view["tray"]] == ["Odd ids"] * 3 + ["Even ids"] * 2
        assert [e["id"] for e in view["layout"]][:2] == ["now", "tray"]   # the kernel's panel order

    def test_a_provider_fault_becomes_an_unavailable_panel_never_an_empty_one(self) -> None:
        view = _view(snapshot(_tray_cockpit(fault=True)))
        broken = [e for e in view["layout"] if e["id"] == "broken"][0]
        assert broken["kind"] == "external"
        assert "disk on fire" in view["extra_panels"]["broken"]["error"]

    def test_a_stale_panel_carries_a_banner(self) -> None:
        from datetime import timedelta
        clock = {"t": datetime(2026, 10, 9, 12, tzinfo=timezone.utc)}
        ck = _tray_cockpit(stale=True)
        ck._clock = lambda: clock["t"]
        ck.refresh("old")
        clock["t"] += timedelta(seconds=5)
        view = _view(snapshot(ck))
        entry = [e for e in view["layout"] if e["id"] == "old"][0]
        assert any(b["text"].startswith("STALE") for b in entry["banner"])


def _dump_dom(url: str) -> str:
    return subprocess.run([CHROME, "--headless=new", "--dump-dom", "--virtual-time-budget=8000", "--window-size=1440,1000", url],
                          capture_output=True, text=True, timeout=60).stdout


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome is not installed")
class TestTheDashboardPageFromTheManifest:
    """The real page in a real browser: `/?source=manifest` draws the kernel's order and its errors."""

    def _serve(self, tmp_path: Path, ck: Cockpit):
        import threading

        from levain.web_server import make_server
        from tests.test_cockpit_k1 import _install

        _root, src = _install(tmp_path)
        httpd = make_server(src, host="127.0.0.1", port=0, cockpit=ck)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, t

    def test_reversed_order_groups_and_an_error_panel_are_drawn(self, tmp_path: Path) -> None:
        import re

        ck = _tray_cockpit(fault=True)
        served = [r["id"].split(":")[1] for r in ck.panel("tray")["rows"]]
        httpd, t = self._serve(tmp_path, ck)
        try:
            dom = _dump_dom(f"http://127.0.0.1:{httpd.server_address[1]}/?source=manifest")
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
        assert re.findall(r'class="sid"[^>]*>(spore-\d)<', dom) == served
        assert dom.index("Odd ids") < dom.index("Even ids")
        assert "disk on fire" in dom and "unavailable" in dom

    def test_the_default_page_still_reads_the_substrate(self, tmp_path: Path) -> None:
        httpd, t = self._serve(tmp_path, _tray_cockpit())
        try:
            dom = _dump_dom(f"http://127.0.0.1:{httpd.server_address[1]}/")
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
        assert "Open loops" in dom and "loop one" in dom
