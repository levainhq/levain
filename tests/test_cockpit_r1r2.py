"""R1 (web renderer, read half) and R2 (TUI renderer) of the shared cockpit (design
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


def _node_render(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Run cockpit.js's render against a fake DOM and return a flat description of what it drew."""
    driver = r"""
const C = require(process.argv[1]);
class El { constructor(t){this.tag=t;this.className="";this.textContent="";this.children=[];this.attrs={};}
  appendChild(c){this.children.push(c);return c;} replaceChildren(...c){this.children=c;}
  setAttribute(k,v){this.attrs[k]=v;} }
const doc = { createElement: (t) => new El(t) };
const root = new El("main");
const snap = JSON.parse(require("fs").readFileSync(0, "utf8"));
C.render(doc, root, snap, { now: Date.parse("2026-10-09T12:00:00Z"), topN: 12 });
const out = [];
(function walk(e, d){ out.push({tag:e.tag, cls:e.className, text:e.textContent, attrs:e.attrs}); e.children.forEach(c=>walk(c,d+1)); })(root,0);
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run([NODE, "-e", driver, str(WEB / "cockpit.js")], input=json.dumps(snap), capture_output=True,
                         text=True, check=True, timeout=30)
    return json.loads(res.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestWebRenderer:
    def test_head_and_payload_disagreement_shows_the_payloads_error(self) -> None:
        nodes = _node_render(_cached_head_then_error())
        panel = [n for n in nodes if n["attrs"].get("data-panel") == "flip"][0]
        assert panel["attrs"]["data-status"] == "error"
        assert any("disk failed after the manifest" in n["text"] for n in nodes)

    def test_prototype_named_groups_do_not_defeat_the_cut(self) -> None:
        ck = Cockpit()
        try:
            register_ordering(Ordering(name="test.proto", key=lambda r, t: (r.id,), group=lambda r, t: "constructor",
                                       groups=(("constructor", "Ctor"),)))
        except Exception:  # noqa: BLE001
            pass
        rows = [RowIn(f"r-{i:02d}", f"t{i}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r-{i}"}) for i in range(20)]
        ck.register(ProviderSpec("p", "triage-list", "P", "gate", lambda c: Read(rows=tuple(rows)),
                                 order="test.proto", facets=frozenset({"at"}), version_fields=("id",)))
        nodes = _node_render(snapshot(ck))
        assert len([n for n in nodes if "data-row" in n["attrs"]]) == 12
        assert [n["text"] for n in nodes if n["cls"] == "ck-more"] == ["+8 more (cut by this view)"]

    def test_full_prose_renders_its_markdown_text_and_compact_feed_lines_get_an_open_button(self) -> None:
        ck = Cockpit()
        ck.register(ProviderSpec("pr", "prose", "Doc", "gauge",
                                 lambda c: Read(value={"markdown": "the body text", "headline": "Head"})))
        ck.register(ProviderSpec("ln", "line", "Feed", "feed",
                                 lambda c: Read(value={"lines": [{"label": "a", "text": "b", "at": None, "source": None}]})))
        full = _node_render(snapshot(ck))
        assert any(n["text"] == "the body text" for n in full)
        compact = _node_render(snapshot(ck, profile="compact"))
        assert [n for n in compact if n["cls"] == "ck-open"], "a compact feed line must offer its content on open"

    def test_format_characters_and_naive_timestamps(self) -> None:
        out = subprocess.run(
            [NODE, "-e", "const C=require(process.argv[1]);const n=Date.parse('2026-10-09T12:00:00Z');"
             "process.stdout.write(C.visible('a b\\u061c\\u206a')+'|'+C.age('2026-10-09T11:00:00',n))",
             str(WEB / "cockpit.js")], capture_output=True, text=True, check=True).stdout
        assert out == "a b<U+061C><U+206A>|1h ago"

    def test_non_natural_order_and_groups_are_shown_as_served(self) -> None:
        ck = _cockpit()
        served = [r["id"] for r in ck.panel("items")["rows"]]
        nodes = _node_render(snapshot(ck))
        rows = [n["attrs"]["data-row"] for n in nodes if "data-row" in n["attrs"]]
        assert rows == served
        groups = [n["text"] for n in nodes if n["cls"] == "ck-group"]
        assert groups == ["Odd ids (3)", "Even ids (2)"]

    def test_a_provider_fault_shows_its_error_text(self) -> None:
        nodes = _node_render(snapshot(_cockpit(fault=True)))
        errs = [n["text"] for n in nodes if "ck-msg-error" in n["cls"]]
        assert any("disk on fire" in t and "last good" in t for t in errs)
        broken = [n for n in nodes if n["attrs"].get("data-panel") == "broken"][0]
        assert broken["attrs"]["data-status"] == "error"

    def test_top_n_cut_says_how_many_it_hid(self) -> None:
        ck = Cockpit()
        rows = [RowIn(f"r-{i}", f"t{i}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r-{i}"}) for i in range(30)]
        ck.register(ProviderSpec("many", "triage-list", "Many", "gate", lambda c: Read(rows=tuple(rows)),
                                 order="test.reversed", facets=frozenset({"at"}), version_fields=("id",)))
        nodes = _node_render(snapshot(ck))
        drawn = [n for n in nodes if "data-row" in n["attrs"]]
        assert len(drawn) == 24   # 12 per group
        mores = sorted(n["text"] for n in nodes if n["cls"] == "ck-more")
        assert mores == ["+3 more (cut by this view)"] * 2

    def test_control_characters_are_visible(self) -> None:
        out = subprocess.run(
            [NODE, "-e", "const C=require(process.argv[1]);process.stdout.write(C.visible('a\\u202eb\\x1b'))",
             str(WEB / "cockpit.js")], capture_output=True, text=True, check=True).stdout
        assert out == "a<U+202E>b<U+001B>"


class TestServedPage:
    def test_the_page_and_its_assets_are_served_and_draw_the_kernels_order(self, tmp_path: Path) -> None:
        import threading
        import urllib.request

        from levain.dashboard import SubstrateSource
        from levain.web_server import make_server
        from tests.test_cockpit_k1 import _install

        root, src = _install(tmp_path)
        ck = _cockpit()
        httpd = make_server(src, host="127.0.0.1", port=0, cockpit=ck)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            def get(path: str):
                with urllib.request.urlopen(base + path, timeout=5) as r:  # noqa: S310 - loopback
                    return r.status, r.headers.get("Content-Type"), r.read().decode()
            st, ct, html = get("/cockpit")
            assert st == 200 and ct.startswith("text/html") and "/cockpit.js" in html and "<script>" not in html
            for path, kind in (("/cockpit.js", "javascript"), ("/cockpit_boot.js", "javascript"), ("/cockpit.css", "css")):
                st, ct, body = get(path)
                assert st == 200 and kind in ct and body
            st, _, m = get("/cockpit/manifest.json")
            served = json.loads(get("/cockpit/panel/items.json")[2])
            assert [r["id"] for r in served["rows"]] == [r["id"] for r in ck.panel("items")["rows"]]
            assert json.loads(m)["panels"]["items"]["groups"][0]["id"] == "odd"
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
