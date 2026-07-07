"""levain.firing (the presence seam) — leaf tests.

These cover the harness-neutral ``PresenceSource`` seam and its dependency-isolation
invariant: importing ``levain.firing`` must pull NEITHER OpenHands NOR vagus (the leaf is
anneal-free / openhands-free stdlib, exactly like ``vagus.firing``). They run in ANY env,
including one without the ``openhands`` extra — the condenser tests (which DO need the extra)
live in ``test_levain_condenser.py`` behind an importorskip.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from levain.firing import (
    PresenceSource,
    ReanchorRequest,
    StubPresence,
    build_presence,
    register_presence,
)


# --- dependency isolation (the leaf invariant) --------------------------------------


def test_presence_leaf_imports_without_openhands_or_anneal():
    """A fresh interpreter importing only ``levain.firing`` must NOT pull OpenHands OR anneal_memory
    into sys.modules — the dependency-isolated-leaf invariant. The OpenHands adapter is the sibling
    ``levain.firing.openhands`` (behind the extra); anneal is the lazy ``levain.firing.anneal`` leaf
    (deferred to recall time). A runtime check, not a source grep."""
    code = (
        "import sys; import levain.firing; "
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in {'openhands', 'anneal_memory'}); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- StubPresence + the protocol ----------------------------------------------------


def test_stub_presence_returns_reanchor_text():
    p = StubPresence()
    out = p.reanchor(ReanchorRequest())
    assert out is not None
    assert "re-anchor" in out.lower()


def test_stub_presence_is_a_presence_source():
    assert isinstance(StubPresence(), PresenceSource)  # runtime_checkable protocol


def test_stub_presence_empty_text_is_no_reanchor():
    # An empty re-anchor means "nothing to re-assert" → None (the condenser injects no event).
    assert StubPresence(reanchor_text="").reanchor(ReanchorRequest()) is None


def test_reanchor_request_carries_query_and_turn_index():
    req = ReanchorRequest(query="ctx", turn_index=7)
    assert req.query == "ctx"
    assert req.turn_index == 7
    # forward-looking seam: StubPresence ignores both (Slice 1); a content-aware source uses them.
    assert StubPresence().reanchor(req) is not None


# --- the presence registry (serialization-safe reconstruction) ----------------------


def test_registry_builds_registered_stub():
    p = build_presence("stub")
    assert isinstance(p, StubPresence)


def test_registry_unknown_kind_raises_clearly():
    # A typo'd presence kind should SURFACE, not silently degrade to no-re-anchor.
    with pytest.raises(ValueError, match="unknown presence kind"):
        build_presence("nope-not-a-kind")


def test_registry_new_kind_round_trips():
    class _Custom:
        def reanchor(self, req: ReanchorRequest) -> str | None:
            return "CUSTOM-REANCHOR"

    register_presence("_custom_test", _Custom)
    built = build_presence("_custom_test")
    assert built.reanchor(ReanchorRequest()) == "CUSTOM-REANCHOR"
