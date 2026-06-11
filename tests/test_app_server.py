"""Tests for levain.app_server — the Slice-1 MCP-App in-host render.

The server is exercised against a real FastMCP instance over a real (temp)
anneal store — the tool is actually called and the wire-shape of the result
(content / structuredContent / _meta.ui) asserted, not mocked. The one thing
NOT machine-checkable here is the in-host iframe render (needs a live MCP-Apps
host); that's the documented L4 manual canary.

The load-bearing test is ``test_read_only_declares_zero_write_tools`` — read-only
is a STRUCTURAL invariant (the host can only call declared tools), so the guard
is "the server declares exactly the read tool and nothing that mutates."
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from levain.app_server import VIEW_MIME, VIEW_URI, build_app, run_app_server
from levain.dashboard import (
    AnnealPaths,
    build_substrate_view,
    render_summary,
)


def _run(coro):
    """Drive an async FastMCP call from a sync test (no pytest-asyncio dep)."""
    return anyio.run(lambda: coro)


def _store_with_data(tmp_path: Path) -> AnnealPaths:
    from anneal_memory import Store
    from anneal_memory.spores import SporeStore

    db = tmp_path / "memory.db"
    with Store(db) as store:
        a = store.record("decided X because Y", "decision")
        b = store.record("noticed Z", "observation")
        store.record_associations({(a.id, b.id)})
    SporeStore(tmp_path / "memory.spores.json").add(
        type="task", text="an open loop", tier="hot", salience=2
    )
    (tmp_path / "memory.continuity.md").write_text(
        "## State\ncurrent focus line\n\n## Active Threads\n- thread one\n",
        encoding="utf-8",
    )
    return AnnealPaths.from_db(db)


# --- render_summary (the model-visible content half) -----------------------

class TestRenderSummary:
    def test_dark_write_path(self, tmp_path: Path) -> None:
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        s = render_summary(build_substrate_view(AnnealPaths.from_db(db)))
        assert "DARK" in s
        assert "Crystallized patterns: 0" in s
        assert "Open loops: 0" in s

    def test_live_write_path_and_headlines(self, tmp_path: Path) -> None:
        s = render_summary(build_substrate_view(_store_with_data(tmp_path)))
        assert "LIVE" in s
        assert "Hebbian links" in s
        assert "Open loops: 1" in s
        # the entity's own State headline surfaces, not the whole dump
        assert "current focus line" in s
        assert "Active Threads:" in s

    def test_missing_store_is_named(self, tmp_path: Path) -> None:
        v = build_substrate_view(AnnealPaths.from_db(tmp_path / "nope" / "memory.db"))
        s = render_summary(v)
        assert "unavailable" in s

    def test_summary_is_compact_not_full_dump(self, tmp_path: Path) -> None:
        """content must be a digest — ONE headline per section, not the full
        body dump. Guards the whole reason the split exists (the body rides in
        structuredContent so it never bloats model context)."""
        from anneal_memory import Store

        db = tmp_path / "memory.db"
        with Store(db):
            pass
        (tmp_path / "memory.continuity.md").write_text(
            "## State\nfocus headline\nsecond state line\nthird state line\n",
            encoding="utf-8",
        )
        s = render_summary(build_substrate_view(AnnealPaths.from_db(db)))
        assert "focus headline" in s        # the headline surfaces
        assert "second state line" not in s  # but not the rest of the body
        assert "third state line" not in s
        assert len(s.splitlines()) < 12


# --- build_app: the server factory -----------------------------------------

class TestBuildApp:
    def test_declares_the_view_resource(self, tmp_path: Path) -> None:
        app = build_app(_store_with_data(tmp_path))
        resources = _run(app.list_resources())
        uris = {str(r.uri) for r in resources}
        assert VIEW_URI in uris
        view = next(r for r in resources if str(r.uri) == VIEW_URI)
        assert view.mimeType == VIEW_MIME
        # the CSP resourceDomains must land or the sandboxed iframe can't fetch
        # the ext-apps client from the CDN and the UI silently fails to render —
        # the exact invisible_infrastructure_failure this dashboard exists to fight
        assert view.meta is not None
        assert view.meta["ui"]["csp"]["resourceDomains"] == ["https://unpkg.com"]

    def test_csp_allows_the_exact_cdn_the_ui_imports(self, tmp_path: Path) -> None:
        """Cross-check the two halves that must agree: the CSP origin the resource
        allows MUST be the origin the UI HTML actually imports from. Drift here is
        a silent no-render."""
        from levain.app_server import _load_view_html

        app = build_app(_store_with_data(tmp_path))
        view = next(r for r in _run(app.list_resources()) if str(r.uri) == VIEW_URI)
        allowed = view.meta["ui"]["csp"]["resourceDomains"]
        html = _load_view_html()
        assert any(origin in html for origin in allowed), (
            f"UI imports from no allowed CSP origin {allowed}"
        )

    def test_resource_serves_the_ui_html(self, tmp_path: Path) -> None:
        app = build_app(_store_with_data(tmp_path))
        contents = list(_run(app.read_resource(VIEW_URI)))
        assert contents, "resource read returned nothing"
        html = contents[0].content
        assert "<!DOCTYPE html>" in html
        # the build-free CDN client + the structuredContent reader
        assert "@modelcontextprotocol/ext-apps" in html
        assert "ontoolresult" in html

    def test_read_only_declares_zero_write_tools(self, tmp_path: Path) -> None:
        """The structural read-only invariant: the host can only call declared
        tools, so the server must declare exactly the read tool and nothing that
        could mutate the substrate (no ascend/descend/clear/retire/write verbs)."""
        app = build_app(_store_with_data(tmp_path))
        tools = _run(app.list_tools())
        names = {t.name for t in tools}
        assert names == {"substrate"}, f"unexpected tools declared: {names}"
        mutating = ("ascend", "descend", "clear", "retire", "write", "save",
                    "delete", "update", "set", "add", "wrap")
        for n in names:
            assert not any(verb in n.lower() for verb in mutating), n

    def test_tool_declares_its_ui_resource(self, tmp_path: Path) -> None:
        app = build_app(_store_with_data(tmp_path))
        tool = next(t for t in _run(app.list_tools()) if t.name == "substrate")
        assert tool.meta is not None
        # BOTH wire forms — nested (current spec) AND flat (deprecated 0.4.0-era
        # convention some hosts still read), so the UI associates either way
        assert tool.meta.get("ui", {}).get("resourceUri") == VIEW_URI
        assert tool.meta.get("ui/resourceUri") == VIEW_URI

    def test_tool_result_splits_content_and_structured(self, tmp_path: Path) -> None:
        paths = _store_with_data(tmp_path)
        app = build_app(paths)
        result = _run(app.call_tool("substrate", {}))

        # content = the model-visible summary (matches render_summary)
        assert result.content, "no content blocks"
        text = result.content[0].text
        assert text == render_summary(build_substrate_view(paths))
        assert "Levain substrate" in text

        # structuredContent = the full SubstrateView (UI render-prop)
        sc = result.structuredContent
        assert sc is not None
        for key in ("paths", "health", "graph", "crystal_index", "open_spores",
                    "sections", "errors"):
            assert key in sc
        assert sc["health"]["total_episodes"] == 2
        assert len(sc["open_spores"]) == 1

        # _meta points the host at the resource for hydration — both wire forms
        assert result.meta is not None
        assert result.meta.get("ui", {}).get("resourceUri") == VIEW_URI
        assert result.meta.get("ui/resourceUri") == VIEW_URI

    def test_tool_call_is_non_mutating(self, tmp_path: Path) -> None:
        """Calling the read tool must not write to the store (pure read)."""
        paths = _store_with_data(tmp_path)
        before = paths.episodic_db.stat().st_mtime_ns
        app = build_app(paths)
        _run(app.call_tool("substrate", {}))
        _run(app.call_tool("substrate", {}))
        assert paths.episodic_db.stat().st_mtime_ns == before


# --- run_app_server / CLI: the startup contract ----------------------------

class TestServeEntry:
    def test_missing_store_returns_1(self, tmp_path: Path, capsys) -> None:
        # the no-store branch returns before app.run(), so it doesn't block
        rc = run_app_server(tmp_path)
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_via_cli_main_missing_store(self, tmp_path: Path, capsys) -> None:
        from levain.cli import main

        rc = main(["serve-app", "--path", str(tmp_path)])
        assert rc == 1
        assert "No anneal store" in capsys.readouterr().err

    def test_missing_mcp_returns_1_with_hint(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """The one real operator failure mode: store present but the SDK isn't
        installed. run_app_server must catch the ImportError and surface the
        `levain[app]` hint with rc=1, not crash."""
        import builtins

        from anneal_memory import Store

        from levain.app_server import run_app_server

        levain_dir = tmp_path / ".levain"
        levain_dir.mkdir()
        with Store(levain_dir / "memory.db"):
            pass

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError("No module named 'mcp'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        rc = run_app_server(tmp_path)
        assert rc == 1
        assert "levain[app]" in capsys.readouterr().err


# --- the optional-dependency contract --------------------------------------

class TestOptionalDependency:
    def test_require_mcp_gives_install_hint(self, monkeypatch) -> None:
        """Without the SDK, the error must name `levain[app]`, not raw
        ModuleNotFoundError — the optional-extra contract."""
        import builtins

        from levain.app_server import _require_mcp

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError("No module named 'mcp'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError) as ei:
            _require_mcp()
        assert "levain[app]" in str(ei.value)

    def test_module_imports_with_mcp_blocked(self) -> None:
        """Structural proof (not a trivially-passing reload): the module imports
        cleanly in a subprocess where `mcp` is BLOCKED — so the CLI can import it
        to print the install hint. Only build_app/run_app_server need the SDK."""
        import subprocess
        import sys
        import textwrap

        code = textwrap.dedent(
            """
            import sys
            # block the SDK: `import mcp` now raises ImportError
            sys.modules["mcp"] = None
            sys.modules["mcp.server"] = None
            sys.modules["mcp.server.fastmcp"] = None
            import levain.app_server as a
            assert a.VIEW_URI == "ui://levain/dashboard"
            assert hasattr(a, "build_app") and hasattr(a, "run_app_server")
            print("OK")
            """
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout
