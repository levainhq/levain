"""levain.kernel — the published rendering-kernel contract.

The STABLE surface a downstream control plane consumes to render, inspect, and
govern a SINGLE anneal substrate without coupling to Levain's internal module
layout. Levain's own ``serve`` / ``tui`` / ``dashboard`` entry points build on the
same pieces; this namespace is the explicit, versioned seam for an EXTERNAL
consumer — the flow Bridge today (one N-of-1 substrate), the constellation
FleetView next (N substrates, each rendered through this same per-entity kernel and
aggregated above it).

``fork_the_kernel_not_the_product``: ``SubstrateView`` / ``build_substrate_view`` /
the drivers / the governed write seam are already source-agnostic by design.
Publishing them here means a downstream imports ``levain.kernel`` and never a
private module — notably NOT the curses driver ``_tui_curses``, whose internals
stay private behind the one published entry ``run_curses``.

Contract surface:
  - data model + render-prep : AnnealPaths, SubstrateSource, SubstrateView,
                               build_substrate_view
  - terminal driver          : run_curses(source, view, *, read_only) — the
                               SOURCE-based entry a custom/aggregated source needs.
                               (``levain.tui.run_tui`` is the PATH-based install
                               convenience layered over the same driver; a downstream
                               with an N-of-1 or aggregated source uses run_curses.)
  - web driver               : make_server — the SOURCE-based bind; the consumer runs
                               its own serve loop (cf. the Bridge's ``_serve_web``). The
                               PATH-based ``run_web_server`` convenience is intentionally
                               NOT in the contract: an N-of-1 / aggregated source has no
                               single install path (same axis as run_curses-vs-run_tui).
  - governed verb-dispatch   : WriteScope, apply_edit, EditError
  - governed ACTION-dispatch : ActionVerb, apply_action — the write-peer of extra_panels;
                               a downstream registers ``make_server(extra_verbs=...)`` to
                               steer flow's N-of-1 channels (inbox/relay/...) through the
                               same auth + confirm + audit envelope as ``POST /edit``.

This module is a pure re-export façade: it adds NO logic (the moat stays
substrate-side), it only publishes the contract. Adding to the kernel = adding a
name here + to ``__all__``; the test suite locks this surface so the contract
can't drift silently.

Import cost: every kernel dep is UNCONDITIONAL stdlib on Levain's POSIX/Mac targets —
``curses`` (terminal driver) + ``http.server`` (web driver) — so the eager re-export
is headless-safe: ``import curses`` needs no tty (only ``curses.wrapper`` does, at call
time), and a headless-import test (test_kernel.py) locks that. The trade is that
importing ANY kernel name pulls both drivers; acceptable while the deps are unconditional
stdlib. If the contract ever has to import in a curses-less / optional-web environment,
switch to a PEP 562 lazy ``__getattr__`` so a data-model-only consumer skips the drivers.
"""

from levain._tui_curses import main_loop as run_curses
from levain.dashboard import (
    AnnealPaths,
    SubstrateSource,
    SubstrateView,
    build_substrate_view,
)
from levain.web_server import make_server
from levain.writes import ActionVerb, EditError, WriteScope, apply_action, apply_edit

__all__ = [
    # data model + render-prep
    "AnnealPaths",
    "SubstrateSource",
    "SubstrateView",
    "build_substrate_view",
    # terminal driver (source-based; the private _tui_curses stays internal)
    "run_curses",
    # web driver (source-based bind; the path-based run_web_server stays out of the contract)
    "make_server",
    # governed verb-dispatch
    "WriteScope",
    "apply_edit",
    "EditError",
    # governed action-dispatch (the write-peer of extra_panels)
    "ActionVerb",
    "apply_action",
]
