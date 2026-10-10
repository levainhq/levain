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
        "process.stdout.write(JSON.stringify(V.fromManifest(snap)))")
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestManifestMapperAdversarial:
    """The L3 findings on the redo, each as a case that fails on the pre-fix mapper."""

    def _snap(self, ck: Cockpit) -> dict[str, Any]:
        return snapshot(ck)

    def test_prototype_named_group_and_panel_ids_are_data_not_prototype_keys(self) -> None:
        try:
            register_ordering(Ordering(name="test.proto", key=lambda r, t: (r.id,), group=lambda r, t: "constructor",
                                       groups=(("constructor", "Ctor band"),)))
        except Exception:  # noqa: BLE001 - registered once per process
            pass
        ck = Cockpit()
        rows = [RowIn("spore:spore-1", "t", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": "spore-1"})]
        ck.register(ProviderSpec("tray", "triage-list", "Tray", "gate", lambda c: Read(rows=tuple(rows)),
                                 order="test.proto", facets=frozenset({"at"}), version_fields=("id",)))
        ck.register(ProviderSpec("constructor", "line", "Odd id", "gauge",
                                 lambda c: Read(value={"lines": [{"label": "a", "text": "b", "at": None, "source": None}]})))
        snap = self._snap(ck)
        del snap["panels"]["constructor"]          # a payload missing for an id that names a prototype member
        view = _view(snap)
        assert view["tray"][0]["group_title"] == "Ctor band"
        odd = [e for e in view["layout"] if e["id"] == "constructor"][0]
        assert odd["kind"] == "external" and "no payload" in view["extra_panels"]["constructor"]["error"]

    def test_a_failed_header_fetch_is_an_error_and_an_extra_header_panel_is_drawn(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("focus", "line", "Focus", "gauge", lambda c: Read(value={"lines": [
            {"label": "focus", "text": "x", "at": "2026-10-09T12:00:00+00:00", "source": "s"}]}), region="header"))
        ck.register(ProviderSpec("weather", "line", "Weather", "gauge", lambda c: Read(value={"lines": [
            {"label": "now", "text": "cloudy", "at": None, "source": None}]}), region="header", rank=5))
        snap = self._snap(ck)
        snap["panels"]["focus"] = {"error": "HTTP 500"}
        view = _view(snap)
        assert view["focus"] is None and "HTTP 500" in view["errors"]["focus"]
        assert view["extra_panels"]["weather"]["lines"][0]["text"] == "cloudy"

    def test_an_unreadable_wraps_panel_is_an_error_not_a_never(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("health", "metric", "Health", "gauge", lambda c: Read(value={"metrics": [
            {"label": "wraps", "value": 4, "unit": None, "status": "ok", "read": None}], "alerts": [
            {"message": "wrap in progress", "severity": "warn"}]})))
        ck.register(ProviderSpec("wraps", "visual", "Projection history", "feed", lambda c: Fault("wraps unreadable")))
        view = _view(self._snap(ck))
        assert "wraps unreadable" in view["errors"]["wraps"]
        assert view["health"]["last_wrap_at"] is None
        h = [e for e in view["layout"] if e["id"] == "health"][0]
        assert any("wrap in progress" in b["text"] for b in h["banner"])   # an alert is never dropped

    def test_now_rows_keep_their_full_text_group_titles_and_hostile_characters_are_visible(self) -> None:
        ck = Cockpit()
        long = "x" * 200 + "\u202eEND"
        rows = [RowIn("spore:spore-1", long, {"disposition": "seed", "due": "2020-01-01", "tier": "hot", "salience": 0,
                                               "handoff_expired": False}, body=long, stored={"id": "spore-1"})]
        ck.register(ProviderSpec("tray", "triage-list", "Tray", "gate", lambda c: Read(rows=tuple(rows)), order="spore.tray",
                                 facets=frozenset({"disposition", "due", "tier", "salience", "handoff_expired"}),
                                 version_fields=("id",)))
        view = _view(self._snap(ck))
        now = view["extra_panels"]["now"]["lines"][0]
        assert now["text"].endswith("<U+202E>END") and len(now["text"]) > 160   # full body, sanitised
        assert "Overdue" in now["meta"]                                         # the kernel's band title, not "overdue"

    def test_an_unparseable_focus_stamp_is_age_unknown_not_fresh(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("focus", "line", "Focus", "gauge", lambda c: Read(value={"lines": [
            {"label": "focus", "text": "x", "at": "not-a-date", "source": "s"}]}), region="header"))
        assert _view(self._snap(ck))["focus"]["freshness"] == "unknown"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestManifestMapperBoundary:
    """L3 r2 on the redo: ONE sanitising boundary, and the state/dup cases."""

    def test_every_wire_string_is_cleaned_in_one_place_and_legitimate_text_survives(self) -> None:
        ck = Cockpit()
        rows = [RowIn("episode:e1", "title", {"at": "2026-10-01T00:00:00+00:00", "episode_type": "obs", "source": "s\u202ex",
                                              "tags": ["t\u202ey"]}, body="a\r\nb\u200d\u200cc\t\u0007", stored={"id": "e1"})]
        ck.register(ProviderSpec("episodes", "triage-list", "Episodes", "feed", lambda c: Read(rows=tuple(rows)),
                                 order="time.desc", facets=frozenset({"at", "episode_type", "source", "tags"}), version_fields=("id",)))
        ep = _view(snapshot(ck))["episodes"][0]
        assert ep["source"] == "s<U+202E>x" and ep["tags"] == ["t<U+202E>y"]      # a field the old per-field guards missed
        assert ep["content"] == "a\nb\u200d\u200cc\t<U+0007>"                      # CRLF normalised, ZWJ/ZWNJ/tab kept, BEL shown

    def test_a_downstream_metric_alert_is_drawn_once(self) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec("m", "metric", "M", "gauge", lambda c: Read(value={"metrics": [
            {"label": "x", "value": 1, "unit": None, "status": "ok", "read": None}], "alerts": [{"message": "danger", "severity": "bad"}]})))
        view = _view(snapshot(ck))
        entry = [e for e in view["layout"] if e["id"] == "m"][0]
        shown = [b["text"] for b in entry["banner"]] + [ln["text"] for ln in view["extra_panels"]["m"]["lines"]]
        assert shown.count("! danger") + shown.count("danger") == 1

    def test_unreadable_wraps_marks_the_health_history_unavailable(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("health", "metric", "Health", "gauge", lambda c: Read(value={"metrics": [
            {"label": "wraps", "value": 4, "unit": None, "status": "ok", "read": None}]})))
        ck.register(ProviderSpec("wraps", "visual", "W", "feed", lambda c: Fault("nope")))
        assert _view(snapshot(ck))["health"]["wrap_history_unavailable"] is True

    def test_a_panel_listed_twice_is_placed_once_and_a_non_string_stamp_does_not_blank_the_view(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("weather", "line", "Weather", "gauge", lambda c: Read(value={"lines": [
            {"label": "now", "text": "cloudy", "at": 12345, "source": None}]}), region="header"))
        snap = snapshot(ck)
        snap["manifest"]["regions"]["zones"][0]["panels"].append("weather")
        view = _view(snap)
        assert [e["id"] for e in view["layout"]].count("weather") == 1

    def test_wire_keys_named_like_prototype_members_are_data(self) -> None:
        ck = _tray_cockpit()
        snap = json.loads(json.dumps(snapshot(ck)))
        snap["panels"]["tray"]["value"] = {"alerts": {}}
        raw = json.dumps(snap).replace('"value": {"alerts": {}}', '"value": {"__proto__": {"alerts": {}}, "metrics": []}')
        assert '"__proto__"' in raw                  # the replacement happened (a vacuous test would pass without it)
        view = _view(json.loads(raw))        # json.loads keeps "__proto__" as an own key, as a browser's JSON.parse does
        assert [e["id"] for e in view["layout"]].count("tray") == 1

    def test_a_far_future_panel_stamp_does_not_move_every_other_ages(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("focus", "line", "Focus", "gauge", lambda c: Read(value={"lines": [
            {"label": "focus", "text": "x", "at": "2099-01-01T00:00:00+00:00", "source": "s"}]}), region="header"))
        snap = snapshot(ck)
        snap["panels"]["tray"]["as_of"] = "2100-01-01T00:00:00+00:00"
        view = _view(snap)
        assert view["focus"]["freshness"] == "unknown"

    def test_a_degraded_now_view_names_its_source_and_a_missing_wraps_panel_is_not_unavailable(self) -> None:
        ck = _tray_cockpit()
        snap = snapshot(ck)
        snap["panels"]["now"]["degraded"] = ["tray"]
        snap["panels"]["now"]["error"] = None
        view = _view(snap)
        assert view["extra_panels"]["now"]["note"] == "not complete: tray"

    def test_wraps_state_is_tri_state_missing_is_not_unavailable_but_errored_is(self) -> None:
        def health_view(with_wraps: str | None) -> dict[str, Any]:
            ck = _tray_cockpit()
            ck.register(ProviderSpec("health", "metric", "Health", "gauge", lambda c: Read(value={"metrics": [
                {"label": "wraps", "value": 4, "unit": None, "status": "ok", "read": None}]})))
            if with_wraps == "fault":
                ck.register(ProviderSpec("wraps", "visual", "W", "feed", lambda c: Fault("x")))
            return _view(snapshot(ck))["health"]
        assert health_view(None)["wrap_history_unavailable"] is False
        assert health_view("fault")["wrap_history_unavailable"] is True

    def test_one_malformed_panel_degrades_alone_and_a_colliding_cleaned_key_is_refused(self) -> None:
        ck = _tray_cockpit()
        ck.register(ProviderSpec("m", "metric", "M", "gauge", lambda c: Read(value={"metrics": [
            {"label": "x", "value": 1, "unit": None, "status": "ok", "read": None}]})))
        snap = snapshot(ck)
        snap["panels"]["m"]["value"]["metrics"][0]["value"] = {"a": 1}      # an object where a scalar belongs
        view = _view(snap)
        assert "could not be mapped" in view["extra_panels"]["m"]["error"]
        assert [e["id"] for e in view["layout"]].count("tray") == 1          # the rest of the cockpit still renders
        js = ("const C=require(process.argv[1]);let r=[];"
              "for (const j of ['{\"ops\\\\u202ey\":1,\"ops<U+202E>y\":2}', '{\"ops<U+202E>y\":2,\"ops\\\\u202ey\":1}'])"
              "{try{C.clean(JSON.parse(j));r.push('kept')}catch(e){r.push('refused')}}"
              "process.stdout.write(r.join(','))")
        out = subprocess.run([NODE, "-e", js, str(WEB / "cockpit_view.js")], capture_output=True, text=True, check=True).stdout
        assert out == "refused,refused"        # a collision is refused whole, in any key order, never resolved by a guess

    def test_a_non_string_panel_title_falls_back_to_the_head_title(self) -> None:
        ck = _tray_cockpit()
        snap = snapshot(ck)
        snap["panels"]["tray"]["title"] = {"a": 1}
        view = _view(snap)
        titles = [e["title"] for e in view["layout"] if e["id"] == "tray"]
        assert len(titles) == 1 and isinstance(titles[0], str) and titles[0]   # the head's title, not the object


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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestTrayKeepFilter:
    """Tray and Keep carry the filter bar Open Loops has (text or spore-id, client-side, persisted)."""

    DRIVER = r"""
const fs = require("fs");
const text = fs.readFileSync(process.argv[2], "utf8");
const a = text.indexOf("FILTER-EXTRACT-START"), b = text.indexOf("FILTER-EXTRACT-END");
const block = text.slice(text.lastIndexOf("\n", a) + 1, text.lastIndexOf("\n", b) + 1);
function mk(t) { return { tagName: t, className: "", children: [], attrs: {}, listeners: {}, value: "", _text: "",
  set textContent(v) { this._text = String(v); this.children = []; }, get textContent() { return this._text; },
  appendChild(c) { this.children.push(c); c.parent = this; return c; }, append(...cs) { cs.forEach((c) => this.appendChild(c)); },
  replaceChildren(...cs) { this.children = []; cs.forEach((c) => this.appendChild(c)); },
  setAttribute(k, v) { this.attrs[k] = String(v); }, addEventListener(e, f) { this.listeners[e] = f; },
  closest() { return null; } }; }
const el = (t, c, x) => { const n = mk(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const sporeQueries = {};
const f = new Function("el", "sporeQueries", "measureOverflow", "measureClauses", block + "\nreturn mountSporeFilter;");
const rowsOf = (panel) => panel.children[1].children.filter((c) => c.className === "row").length;
const ROWS = [1, 2, 3, 4, 5].map((i) => ({ id: "spore-" + i, text: i === 4 ? "Kettle bell" : "item " + i }));
for (const kind of ["tray", "keep"]) {
  const mount = f(el, sporeQueries, () => {}, () => {});
  const p = mk("div");
  mount(p, ROWS, "items", kind, (rows, results) => rows.forEach((s) => results.appendChild(el("div", "row", s.id))));
  const input = p.children[0].children[0];
  const type = (v) => { input.value = v; input.listeners.input(); };
  const fail = (m) => { console.error("FAIL " + kind + ": " + m); process.exit(1); };
  if (rowsOf(p) !== 5) fail("initial rows " + rowsOf(p));
  type("spore-2"); if (rowsOf(p) !== 1) fail("id filter gave " + rowsOf(p));
  type("KETTLE"); if (rowsOf(p) !== 1) fail("text filter gave " + rowsOf(p));
  type("nomatch"); if (rowsOf(p) !== 0 || !p.children[1].children[0].textContent.includes("nomatch")) fail("empty state");
  type(""); if (rowsOf(p) !== 5) fail("clearing did not restore, got " + rowsOf(p));
  type("spore-3");                                     // persists into the next render of the same kind
  const p2 = mk("div"); mount(p2, ROWS, "items", kind, (rows, results) => rows.forEach((s) => results.appendChild(el("div", "row", s.id))));
  if (rowsOf(p2) !== 1) fail("query not persisted across a re-render");
  sporeQueries[kind] = "";
}
console.log("OK");
"""

    def test_filtering_narrows_rows_and_clearing_restores_them_on_tray_and_keep(self, tmp_path: Path) -> None:
        drv = tmp_path / "filter_check.js"
        drv.write_text(self.DRIVER, encoding="utf-8")
        r = subprocess.run([NODE, str(drv), str(WEB / "dashboard_core.js")], capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout

    def test_tray_and_keep_projections_mount_the_filter_by_panel_kind(self) -> None:
        core = (WEB / "dashboard_core.js").read_text(encoding="utf-8")
        body = core[core.index("function renderSporeProjection"):core.index("function renderSpores(")]
        assert "mountSporeFilter(p, list" in body and "entry.kind" in body
        assert "const sporeQueries = Object.create(null)" in core            # a kind named "constructor" is data
        assert "try { return renderPanel(entry, view, opts); }" in core      # a panel that cannot be drawn degrades alone
        assert core.count("renderPanelSafe(") >= 3                          # the board AND the modal draw through it
