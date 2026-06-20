"""levain.kernel — lock the published rendering-kernel contract.

The kernel is the stable seam a downstream control plane (the flow Bridge today,
the constellation FleetView next) imports INSTEAD of reaching into Levain's internal
modules (notably the private ``_tui_curses`` driver). These tests fix that surface:
a rename / move / accidental drop of a kernel export is a CI failure, not a silent
break of a downstream that pinned to the contract. Mirrors the cross-repo parity
discipline (a doc-only contract becomes a refusal).
"""

import inspect

import pytest

import levain.kernel as kernel

# The published contract. A change here is a deliberate contract change (and must be
# reflected in the kernel docstring + any downstream); a DRIFT here is the bug this
# test exists to catch.
CONTRACT = {
    "AnnealPaths",
    "SubstrateSource",
    "SubstrateView",
    "build_substrate_view",
    "run_curses",
    "make_server",
    "WriteScope",
    "apply_edit",
    "EditError",
}


class TestKernelSurface:
    def test_all_matches_contract(self) -> None:
        """__all__ is EXACTLY the contract — no silent additions, no drops."""
        assert set(kernel.__all__) == CONTRACT

    def test_no_duplicate_exports(self) -> None:
        assert len(kernel.__all__) == len(set(kernel.__all__))

    def test_every_export_is_importable_and_present(self) -> None:
        """Each name in __all__ actually resolves on the module (a re-export typo
        would otherwise only blow up at a downstream's import site)."""
        for name in kernel.__all__:
            assert hasattr(kernel, name), f"levain.kernel missing exported {name!r}"
            assert getattr(kernel, name) is not None


class TestKernelIdentity:
    """Each export must BE the canonical object from its source module — the kernel
    is a pure re-export façade, never a reimplementation (the moat stays
    substrate-side)."""

    def test_data_model_is_from_dashboard(self) -> None:
        from levain import dashboard

        assert kernel.AnnealPaths is dashboard.AnnealPaths
        assert kernel.SubstrateSource is dashboard.SubstrateSource
        assert kernel.SubstrateView is dashboard.SubstrateView
        assert kernel.build_substrate_view is dashboard.build_substrate_view

    def test_write_seam_is_from_writes(self) -> None:
        from levain import writes

        assert kernel.WriteScope is writes.WriteScope
        assert kernel.apply_edit is writes.apply_edit
        assert kernel.EditError is writes.EditError

    def test_web_driver_is_from_web_server(self) -> None:
        from levain import web_server

        # make_server is the SOURCE-based bind (the kernel's web seam); the path-based
        # run_web_server convenience is deliberately NOT in the contract.
        assert kernel.make_server is web_server.make_server

    def test_run_curses_is_the_source_based_driver(self) -> None:
        """run_curses publishes the SOURCE-based curses entry (the private
        _tui_curses.main_loop) — what an N-of-1 / aggregated source needs. The
        downstream must never have to import the private module itself."""
        from levain import _tui_curses

        assert kernel.run_curses is _tui_curses.main_loop


class TestKernelContractDepth:
    """The bridge's pre-flight probes these signatures; lock them so a stale Levain
    that lost the param is caught by the bridge's ImportError-with-install-hint."""

    def test_run_curses_accepts_read_only(self) -> None:
        params = inspect.signature(kernel.run_curses).parameters
        assert "read_only" in params

    def test_make_server_accepts_host(self) -> None:
        params = inspect.signature(kernel.make_server).parameters
        assert "host" in params


class TestKernelEncapsulation:
    def test_does_not_publish_private_driver_module(self) -> None:
        """The curses driver internals stay private — only run_curses is published,
        never the module. ``not hasattr`` (not ``not in __all__``) is the REAL guard:
        it catches an accidental ``from _tui_curses import *`` that would make a name
        ACCESSIBLE on the kernel without ever touching ``__all__``. [complement L3]"""
        assert not hasattr(kernel, "_tui_curses")

    @pytest.mark.parametrize(
        "leak",
        # curses-driver internals + the deliberately-DROPPED path-based run_web_server:
        # none may be accessible on the kernel (an `import *` regression would bind them).
        ["_loop", "_paint", "_apply", "_handle_verb", "run_web_server"],
    )
    def test_no_internals_or_dropped_names_leak(self, leak: str) -> None:
        assert not hasattr(kernel, leak)


class TestKernelHeadlessImport:
    def test_imports_in_a_fresh_interpreter(self) -> None:
        """``import levain.kernel`` must succeed HEADLESS (no tty) — the property the
        Bridge's ``--web`` / pre-flight ``--check`` paths rely on. A fresh interpreter
        (piped stdio, no tty) catches an import-time regression — a future _tui_curses
        that touches the terminal at import, or a non-stdlib web dep — HERE, loudly,
        instead of silently downstream at the bridge's install-hint path. [L1]"""
        import subprocess
        import sys

        res = subprocess.run(
            [sys.executable, "-c", "import levain.kernel as k; assert k.make_server and k.run_curses"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, f"headless `import levain.kernel` failed:\n{res.stderr}"
