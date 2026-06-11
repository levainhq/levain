"""levain.app_server — Levain v2, Slice 1: the MCP-App in-host render.

This is the *host-facing* half of the read-only substrate dashboard. Where
``levain dashboard`` (``dashboard.py``) renders the substrate to a terminal, this
serves it INSIDE the host (Claude desktop/web, ChatGPT, VS Code, Goose, …) as an
MCP App — a sandboxed HTML view the operator reads without leaving the chat.

It is a thin port ON PURPOSE. The compounding value is the governed cargo
underneath (anneal's developmental memory — health, associations, crystals,
spores); the MCP-Apps rails are commodity and disposable, so this wrapper stays
minimal and the data layer (the already-built ``SubstrateView``) does the work.

Three properties carry over from the CLI slice unchanged:
- **read-only is STRUCTURAL** — the server declares ONLY read tools and ZERO
  write tools. The host iframe can call only declared tools, so a steering verb
  (``:clear`` a spore, ascend a pattern) is *physically uncallable*, not merely
  disciplined-against. This is the ``govern_the_seam`` shape at the protocol
  layer: the lintel, not the hallway. Slice 2 adds write verbs ONLY behind an
  explicit approval boundary; until then the surface cannot mutate anything.
- **migration-free** — reads the operator's EXISTING store; zero re-seed.
- **billing-immune** — invoked human-present, never headless.

The MCP-Apps contract (SEP-1865, GA 2026-01-26), grounded against the live spec:
- a tool declares its UI via ``_meta.ui.resourceUri`` → a ``ui://`` resource of
  MIME ``text/html;profile=mcp-app``;
- the result splits three ways — ``content`` (model-visible summary + text
  fallback) · ``structuredContent`` (the UI render-prop, NOT added to model
  context) · ``_meta`` (UI hydration);
- the host renders the HTML in a sandboxed iframe that speaks postMessage
  JSON-RPC back to this server.

``mcp`` (the official SDK / FastMCP) is an OPTIONAL dependency — ``pip install
'levain[app]'``. The core ``levain`` install + ``levain dashboard`` CLI stay lean
and never import it. This module is importable without ``mcp`` (so the CLI can
print a clean install hint); the SDK is required only when an app is actually
built or served.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from levain.dashboard import (
    AnnealPaths,
    _resolve_store,
    build_substrate_view,
    render_summary,
)

if TYPE_CHECKING:  # pragma: no cover — type-only, never imported at runtime
    from mcp.server.fastmcp import FastMCP

__all__ = [
    "VIEW_URI",
    "VIEW_MIME",
    "build_app",
    "run_app_server",
]

# The UI resource the dashboard tool points at. ``ui://`` + the mcp-app HTML
# profile are the spec-mandated scheme/MIME; the host prefetches + security-
# reviews the resource at connect, before any tool runs.
VIEW_URI = "ui://levain/dashboard"
VIEW_MIME = "text/html;profile=mcp-app"


def _ui_resource_meta() -> dict[str, Any]:
    """The tool↔UI association ``_meta``, emitted in BOTH wire forms.

    The current MCP-Apps spec uses the NESTED form (``_meta.ui.resourceUri``);
    the flat form (``_meta["ui/resourceUri"]``) is the deprecated 0.4.0-era
    convention some hosts still read. We emit both — the same belt-and-suspenders
    the canonical ``qr-server`` example uses — so the host associates the tool
    with its UI resource regardless of which key it reads. A fresh dict per call
    avoids any shared-mutable-state surprise between the tool def and the result."""
    return {"ui": {"resourceUri": VIEW_URI}, "ui/resourceUri": VIEW_URI}

# The UI bundle loads @modelcontextprotocol/ext-apps from this CDN (the build-
# free ``app-with-deps`` bundle). The resource MUST declare it as an allowed CSP
# resource domain or the sandboxed iframe can't fetch the client library.
_CDN_ORIGIN = "https://unpkg.com"

_MCP_MISSING_HINT = (
    "The Levain MCP-App server needs the MCP SDK, which is an optional extra.\n"
    "Install it with:  pip install 'levain[app]'   (or: uv pip install 'levain[app]')"
)


def _require_mcp() -> tuple[Any, Any]:
    """Import the optional ``mcp`` SDK, or raise a clear, actionable error.

    Kept call-time (not module-import-time) so ``import levain.app_server`` works
    without the SDK and the CLI can surface the install hint instead of a raw
    ``ModuleNotFoundError``."""
    try:
        from mcp import types
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # not-installed OR broken/partial install — both
        raise ImportError(_MCP_MISSING_HINT) from exc
    return FastMCP, types


def _load_view_html() -> str:
    """Read the static UI bundle from package data (``templates/app/``).

    Shipped as ``package_data`` (``templates/**/*``); read via ``importlib`` so it
    resolves correctly from an installed wheel, an editable install, or a zip."""
    from importlib.resources import files

    return (files("levain") / "templates" / "app" / "dashboard.html").read_text(
        encoding="utf-8"
    )


def build_app(paths: AnnealPaths, *, name: str = "Levain substrate") -> "FastMCP":
    """Construct the read-only MCP-App server over an operator's anneal store.

    Pure factory — takes explicit ``AnnealPaths`` (so tests drive it against a
    fixture store) and wires exactly one read tool + the UI resource. Declaring
    no write tools is the read-only invariant: the host can only call what's
    declared, so nothing here can mutate the substrate."""
    FastMCP, types = _require_mcp()
    view_html = _load_view_html()
    app = FastMCP(name)

    @app.resource(
        VIEW_URI,
        name="levain_dashboard",
        mime_type=VIEW_MIME,
        # Let the sandboxed iframe fetch the ext-apps client bundle from the CDN.
        meta={"ui": {"csp": {"resourceDomains": [_CDN_ORIGIN]}}},
    )
    def dashboard_view() -> str:
        """The substrate dashboard's HTML UI (a ``ui://`` MCP-App resource)."""
        return view_html

    @app.tool(
        name="substrate",
        title="Levain substrate",
        description=(
            "Render this partnership entity's whole anneal substrate — memory "
            "health (is the Hebbian write-path live, or silently dark?), the "
            "association graph, the crystallized-pattern index, open prospective "
            "loops (spores), and the State / Active-Threads narrative — as an "
            "in-host dashboard. Read-only: it inspects, it never changes anything."
        ),
        meta=_ui_resource_meta(),
        # We hand-build content + structuredContent + _meta below; tell FastMCP
        # NOT to synthesize an output schema from the return annotation.
        structured_output=False,
    )
    def substrate() -> Any:
        # NB (future-transport tripwire, codex L3): this is a SYNC tool doing
        # blocking SQLite reads. FastMCP runs sync tools inline on the event loop,
        # which is correct for Slice-1's single-client stdio. If a later slice adds
        # `streamable-http` / concurrent clients, make this `async` and offload the
        # read: `await anyio.to_thread.run_sync(lambda: build_substrate_view(paths))`.
        """Build a fresh read-only snapshot and split it per the MCP-Apps contract.

        ``content`` = the model-visible summary (also the text-only fallback for
        hosts without MCP-Apps). ``structuredContent`` = the full ``to_dict()``
        view — the UI's render prop, NOT added to model context. ``_meta.ui`` =
        the resource pointer for hydration."""
        view = build_substrate_view(paths)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=render_summary(view))],
            structuredContent=view.to_dict(),
            # NB: the result-level field is ``meta`` aliased to ``_meta``; it must
            # be set via the ``_meta`` alias or it serializes under the wrong wire
            # key and the host never hydrates the UI (verified against the SDK).
            _meta=_ui_resource_meta(),
        )

    return app


def run_app_server(path: Path) -> int:
    """``levain serve-app`` entry point — serve the dashboard over stdio.

    The host spawns this as a subprocess (the same wiring adopters already use for
    the ``anneal_memory`` MCP server). Returns nonzero only if the store is
    unreachable before the server starts; a degraded sub-tier renders visibly and
    is not a startup failure (same contract as ``levain dashboard``)."""
    paths = _resolve_store(path)
    if not paths.episodic_db.exists():
        print(
            f"No anneal store at {paths.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1
    try:
        app = build_app(paths)
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    app.run(transport="stdio")
    return 0
