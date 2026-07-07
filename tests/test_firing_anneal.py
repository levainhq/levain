"""Slice 2 tests — the real-anneal firing kind (``AnnealFiring``).

Deterministic units (monkeypatched anneal) cover render / query-handling / fail-soft /
rotation; a subprocess guard proves the firing CORE stays anneal-free (the dependency-
isolated-leaf invariant); a gated integration test proves real-anneal recall wires
end-to-end against the live crystal store.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from anneal_memory.types import RelevantPattern

from levain.firing import CaptureRequest, InjectRequest, build_firing, select_directive
from levain.firing.anneal import AnnealFiring, wrap_nudge

REAL_CRYSTAL = Path.home() / ".anneal-memory" / "memory.crystal.json"


def _existing_store(tmp_path: Path) -> Path:
    p = tmp_path / "crystal.json"
    p.write_text("{}")  # exists; CrystalStore is monkeypatched, so contents are irrelevant
    return p


def _fake_pattern(**kw) -> RelevantPattern:
    base = dict(
        name="structural_invariants_beat_discipline",
        level=3,
        explanation="make the guard structural so it refuses rather than drifts",
        tags=["architecture"],
        activation="warm",
        score=5.0,
        source="keyword",
    )
    base.update(kw)
    return RelevantPattern(**base)  # type: ignore[arg-type]


# --- render -------------------------------------------------------------------------


def test_inject_renders_real_patterns(monkeypatch, tmp_path):
    monkeypatch.setattr("anneal_memory.crystal.CrystalStore", lambda path: object())
    monkeypatch.setattr(
        "anneal_memory.retrieval.retrieve_patterns",
        lambda store, query, **kw: [_fake_pattern()],
    )
    f = AnnealFiring(constitution="CONSTI", crystal_path=_existing_store(tmp_path))
    out = f.inject(InjectRequest(query="how do structural invariants beat discipline", turn_index=0))

    assert "CONSTI" not in out                                # constitution is session_start-only now
    assert "structural_invariants_beat_discipline" in out     # the recalled pattern name
    assert "3x" in out and "warm" in out                      # level + activation rendered
    assert "make the guard structural" in out                 # the explanation
    assert select_directive(0) in out                         # the drift-defense directive
    # the constitution lives at the session_start lifecycle point (the set-once suffix)
    assert f.inject(InjectRequest(lifecycle_point="session_start")) == "CONSTI"


# --- query handling -----------------------------------------------------------------


def test_empty_query_skips_recall(tmp_path):
    f = AnnealFiring(crystal_path=_existing_store(tmp_path))
    out = f.inject(InjectRequest(query="   "))  # whitespace-only == no context
    assert "no query context" in out


def test_no_patterns_marker(monkeypatch, tmp_path):
    monkeypatch.setattr("anneal_memory.crystal.CrystalStore", lambda path: object())
    monkeypatch.setattr("anneal_memory.retrieval.retrieve_patterns", lambda store, query, **kw: [])
    f = AnnealFiring(crystal_path=_existing_store(tmp_path))
    out = f.inject(InjectRequest(query="a query with several distinctive keywords here"))
    assert "nothing relevant" in out


# --- fail-soft (the contract: "no recall beats a crash") ----------------------------


def test_missing_store_fails_soft():
    f = AnnealFiring(crystal_path=Path("/no/such/crystal/store.json"))
    out = f.inject(InjectRequest(query="distinctive query keywords"))
    assert "no crystal store" in out  # never raises


def test_retrieve_raising_fails_soft(monkeypatch, tmp_path):
    from anneal_memory.crystal import CrystalError

    monkeypatch.setattr("anneal_memory.crystal.CrystalStore", lambda path: object())

    def _boom(store, query, **kw):
        raise CrystalError("corrupt store")

    monkeypatch.setattr("anneal_memory.retrieval.retrieve_patterns", _boom)
    f = AnnealFiring(crystal_path=_existing_store(tmp_path))
    out = f.inject(InjectRequest(query="distinctive query keywords"))
    assert "unavailable" in out and "CrystalError" in out  # degraded, did not propagate


def test_malformed_pattern_fails_soft(monkeypatch, tmp_path):
    """codex L3 HIGH: rendering is INSIDE the fail-soft boundary, so a malformed pattern object
    (anneal API drift / a future shape change) degrades to a marker — it must NOT raise out of
    inject(). This is codex's exact repro ([object()] → AttributeError on `.name`)."""
    monkeypatch.setattr("anneal_memory.crystal.CrystalStore", lambda path: object())
    monkeypatch.setattr(
        "anneal_memory.retrieval.retrieve_patterns", lambda store, query, **kw: [object()]
    )
    f = AnnealFiring(crystal_path=_existing_store(tmp_path))
    out = f.inject(InjectRequest(query="distinctive query keywords"))
    assert "unavailable" in out and "AttributeError" in out  # marker, not a raised exception


def test_anneal_absent_fails_soft(monkeypatch, tmp_path):
    """If anneal isn't importable, recall degrades — never an ImportError into the turn."""
    import builtins

    real_import = builtins.__import__

    def _no_anneal(name, *a, **k):
        if name.startswith("anneal_memory"):
            raise ImportError("anneal not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_anneal)
    f = AnnealFiring(crystal_path=_existing_store(tmp_path))
    out = f.inject(InjectRequest(query="distinctive query keywords"))
    assert "unavailable" in out and "ImportError" in out


# --- rotation (pure, on turn_index) -------------------------------------------------


def test_directive_rotates_on_turn_index(tmp_path):
    f = AnnealFiring(crystal_path=Path("/no/such/store.json"))  # constant no-recall slot
    a = f.inject(InjectRequest(query="x", turn_index=0))
    b = f.inject(InjectRequest(query="x", turn_index=1))
    assert a != b
    assert select_directive(0) in a and select_directive(1) in b


# --- registry / serialization-safety ------------------------------------------------


def test_build_firing_anneal_cold_lazy_registers():
    """The serialization-safety the registry EXISTS for: a COLD interpreter that imported only
    vagus.firing (never the leaf) must reconstruct 'anneal' via the lazy import_module path.

    This must run in a FRESH subprocess: this test module imports AnnealFiring at the top, which
    pre-registers 'anneal' in-process — so an in-process build_firing('anneal') would hit the
    registered fast path and never exercise the lazy import (apparatus L2 MED — the prior test
    gave false confidence). The subprocess asserts the leaf is NOT pre-registered, then that
    build_firing triggers the lazy import + registration + returns an AnnealFiring."""
    code = (
        "import levain.firing.contract as f; "
        "from levain.firing.contract import _FIRING_REGISTRY; "
        "assert 'anneal' not in _FIRING_REGISTRY, 'leaf pre-registered — cold path untested'; "
        "obj = f.build_firing('anneal'); "
        "assert type(obj).__name__ == 'AnnealFiring', type(obj).__name__; "
        "assert 'anneal' in _FIRING_REGISTRY"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_unknown_kind_raises_clean_error():
    """A kind whose leaf module genuinely doesn't exist falls through to the clean
    ValueError (the narrowed catch only swallows the absent TARGET module)."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="unknown firing kind"):
        build_firing("definitely_no_such_kind")


# --- dependency isolation (the leaf invariant for the NEW dep) ----------------------


def test_firing_core_imports_without_anneal():
    """Importing vagus.firing must NOT pull anneal_memory into sys.modules — only the
    lazily-imported vagus.firing.anneal does, and even it defers the anneal import to
    recall time. A runtime check, not a source grep."""
    code = (
        "import sys; import levain.firing.contract; "
        "leaked = sorted(m for m in sys.modules if m.startswith('anneal_memory')); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_leaf_import_defers_anneal_to_recall():
    """Even importing the LEAF (vagus.firing.anneal) must not pull anneal_memory — the heavy
    import is deferred to recall time (the docstring's stronger claim, now locked)."""
    code = (
        "import sys; import levain.firing.anneal; "
        "leaked = sorted(m for m in sys.modules if m.startswith('anneal_memory')); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- capture: the afferent-safe substrate-write (append-only, never consolidate) ----


def test_capture_writes_real_episode_append_only(tmp_path):
    """Against a REAL anneal store: capture appends an episode that reads back. The store is
    fresh (no wrap), so the captured episode is in ``episodes_since_wrap()``."""
    from anneal_memory import Store

    db = tmp_path / "memory.db"
    AnnealFiring(episodic_path=db).capture(
        CaptureRequest(content="agent ran the build then verified disk", source="vagus", session_id="s1")
    )
    with Store(db) as store:
        episodes = store.episodes_since_wrap()
    contents = [e.content for e in episodes]
    assert "agent ran the build then verified disk" in contents  # the raw episode landed
    assert any(e.source == "vagus" for e in episodes)


def test_capture_never_consolidates(monkeypatch, tmp_path):
    """The membrane invariant, structurally proven: capture calls ``Store.record`` and NEVER
    ``Store.save_continuity`` — append-only by construction, not by discipline."""
    calls: list[str] = []

    class _SpyStore:
        def __init__(self, path, **kw):
            calls.append("init")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def record(self, **kw):
            calls.append("record")

        def save_continuity(self, text):  # the gated consolidate — must NEVER be reached
            calls.append("save_continuity")

    monkeypatch.setattr("anneal_memory.Store", _SpyStore)
    result = AnnealFiring(episodic_path=tmp_path / "x.db").capture(CaptureRequest(content="something happened"))
    assert result is True                   # a confirmed write returns True
    assert "record" in calls               # the append ran
    assert "save_continuity" not in calls  # the consolidate was NEVER reached


def test_capture_opens_store_with_audit_disabled(monkeypatch, tmp_path):
    """codex L3 HIGH: anneal's audit sidecar is single-writer (concurrent AuditTrail instances
    corrupt the hash chain). The vagus is a general layer where captures can race, so the capture
    store MUST open with audit=False — the episode INSERT stays WAL-safe; the audit chain is
    reserved for the single-writer consolidate."""
    seen_kwargs: dict = {}

    class _SpyStore:
        def __init__(self, path, **kw):
            seen_kwargs.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def record(self, **kw):
            pass

    monkeypatch.setattr("anneal_memory.Store", _SpyStore)
    AnnealFiring(episodic_path=tmp_path / "x.db").capture(CaptureRequest(content="c"))
    assert seen_kwargs.get("audit") is False  # the concurrency-safety contract


def test_capture_returns_false_on_failure(monkeypatch, tmp_path):
    """A swallowed write failure returns False (so vagus_run leaves the turn retryable)."""

    class _BoomStore:
        def __init__(self, path, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def record(self, **kw):
            raise RuntimeError("disk full")

    monkeypatch.setattr("anneal_memory.Store", _BoomStore)
    assert AnnealFiring(episodic_path=tmp_path / "x.db").capture(CaptureRequest(content="c")) is False


def test_capture_empty_content_is_noop(monkeypatch, tmp_path):
    """Empty/whitespace content is a no-op (not a loss) — it must not even open the store."""
    opened = []
    monkeypatch.setattr("anneal_memory.Store", lambda *a, **k: opened.append(1))
    AnnealFiring(episodic_path=tmp_path / "x.db").capture(CaptureRequest(content="   "))
    assert opened == []  # never opened a store for an empty capture


def test_capture_fails_soft_loud_on_error(monkeypatch, tmp_path, caplog):
    """A write failure must NOT raise into the agent's turn-end — but must LOG (a lost capture
    is data loss, unlike recall's silent best-effort miss). fail-soft ≠ fail-silent."""
    import logging

    class _BoomStore:
        def __init__(self, path, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def record(self, **kw):
            raise RuntimeError("disk full")

    monkeypatch.setattr("anneal_memory.Store", _BoomStore)
    with caplog.at_level(logging.WARNING, logger="vagus.firing.anneal"):
        AnnealFiring(episodic_path=tmp_path / "x.db").capture(CaptureRequest(content="c"))  # must not raise
    assert any("episode LOST" in r.message for r in caplog.records)  # loud, not silent
    assert any("RuntimeError" in r.message for r in caplog.records)


def test_capture_resolves_episodic_path_from_env(monkeypatch, tmp_path):
    """With no explicit ``episodic_path``, capture resolves ``VAGUS_EPISODIC_PATH`` per-capture
    (env override → default) — the serialization-safe resolution, mirroring crystal_path."""
    from anneal_memory import Store

    db = tmp_path / "env-store.db"
    monkeypatch.setenv("VAGUS_EPISODIC_PATH", str(db))
    AnnealFiring().capture(CaptureRequest(content="captured via env path"))
    with Store(db) as store:
        assert any(e.content == "captured via env path" for e in store.episodes_since_wrap())


def test_capture_invalid_type_degrades_to_observation_keeps_episode(tmp_path, caplog):
    """An invalid episode_type must NOT lose the episode (fail-soft-loud applied to the type):
    it degrades to 'observation' + logs, and the content still lands. ('finding' is flow's
    vocabulary; anneal's enum rejects it — the exact HIGH-2 data-loss bug, now closed.)"""
    import logging

    from anneal_memory import Store

    db = tmp_path / "memory.db"
    with caplog.at_level(logging.WARNING, logger="vagus.firing.anneal"):
        AnnealFiring(episodic_path=db).capture(
            CaptureRequest(content="real content survives a bad type", episode_type="finding")
        )
    assert any("invalid episode_type" in r.message for r in caplog.records)  # loud
    with Store(db) as store:
        eps = store.episodes_since_wrap()
    landed = [e for e in eps if e.content == "real content survives a bad type"]
    assert landed and landed[0].type.value == "observation"  # kept the episode, degraded the type


def test_capture_session_id_persists_into_metadata(tmp_path):
    """session_id has no Store.record parameter, so it is folded into the episode metadata as
    ``vagus_session_id`` (MED-1) rather than silently dropped."""
    from anneal_memory import Store

    db = tmp_path / "memory.db"
    AnnealFiring(episodic_path=db).capture(
        CaptureRequest(content="grouped episode", session_id="conv-xyz", metadata={"k": "v"})
    )
    with Store(db) as store:
        ep = next(e for e in store.episodes_since_wrap() if e.content == "grouped episode")
    assert ep.metadata.get("vagus_session_id") == "conv-xyz"  # persisted, not dropped
    assert ep.metadata.get("k") == "v"  # caller metadata preserved alongside


def test_capture_afferent_leaf_defers_anneal_import():
    """The dependency-isolated-leaf invariant holds for CAPTURE too: importing the leaf must not
    pull anneal_memory; the Store import is deferred to capture time (same as recall)."""
    code = (
        "import sys; import levain.firing.anneal; "
        "leaked = sorted(m for m in sys.modules if m.startswith('anneal_memory')); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- wrap_nudge: the SessionEnd afferent surfacing (read-only, never consolidates) --


def test_wrap_nudge_fires_above_threshold(tmp_path):
    """Against a REAL store: with >= threshold episodes accumulated since the last wrap, the nudge
    fires and reports the count."""
    db = tmp_path / "memory.db"
    f = AnnealFiring(episodic_path=db)
    for i in range(5):
        f.capture(CaptureRequest(content=f"episode {i}"))
    out = wrap_nudge(episodic_path=db, threshold=3)
    assert out is not None
    assert "5 episodes" in out and "wrap is due" in out


def test_wrap_nudge_silent_below_threshold(tmp_path):
    db = tmp_path / "memory.db"
    AnnealFiring(episodic_path=db).capture(CaptureRequest(content="lonely episode"))
    assert wrap_nudge(episodic_path=db, threshold=12) is None  # 1 < 12 → no nudge


def test_wrap_nudge_no_store_returns_none(tmp_path):
    assert wrap_nudge(episodic_path=tmp_path / "absent.db", threshold=1) is None


def test_wrap_nudge_reads_only_and_never_consolidates(monkeypatch, tmp_path):
    """Membrane: the nudge opens the store read_only and counts episodes — it must NEVER call a
    consolidate. Spy-proven."""
    db = tmp_path / "memory.db"
    db.write_text("")  # exists, so wrap_nudge proceeds to open it
    calls: list[str] = []
    seen_kwargs: dict = {}

    class _SpyStore:
        def __init__(self, path, **kw):
            seen_kwargs.update(kw)
            calls.append("init")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def episodes_since_wrap(self):
            calls.append("episodes_since_wrap")
            return [object()] * 15  # over the default threshold

        def save_continuity(self, text):  # must NEVER be reached
            calls.append("save_continuity")

    monkeypatch.setattr("anneal_memory.Store", _SpyStore)
    out = wrap_nudge(episodic_path=db)
    assert out is not None and "15 episodes" in out
    assert seen_kwargs.get("read_only") is True   # opened read-only
    assert "episodes_since_wrap" in calls
    assert "save_continuity" not in calls         # never consolidated


def test_wrap_nudge_threshold_from_env(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    f = AnnealFiring(episodic_path=db)
    for i in range(3):
        f.capture(CaptureRequest(content=f"e{i}"))
    monkeypatch.setenv("VAGUS_WRAP_NUDGE_THRESHOLD", "3")
    assert wrap_nudge(episodic_path=db) is not None  # env threshold 3, count 3 → fires
    monkeypatch.setenv("VAGUS_WRAP_NUDGE_THRESHOLD", "99")
    assert wrap_nudge(episodic_path=db) is None      # env threshold 99 → silent


def test_wrap_nudge_nonpositive_threshold_clamps_to_default(tmp_path):
    """codex L3 LOW: a non-positive threshold (0 / negative, or env '0') must not produce a
    spurious '0 episodes ... wrap is due' on an empty store — it clamps to the default."""
    db = tmp_path / "memory.db"
    AnnealFiring(episodic_path=db).capture(CaptureRequest(content="one episode"))
    assert wrap_nudge(episodic_path=db, threshold=0) is None     # clamps to default 12; 1 < 12
    assert wrap_nudge(episodic_path=db, threshold=-5) is None


def test_wrap_nudge_fail_soft_on_error(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    db.write_text("")

    class _BoomStore:
        def __init__(self, path, **kw):
            raise RuntimeError("corrupt store")

    monkeypatch.setattr("anneal_memory.Store", _BoomStore)
    assert wrap_nudge(episodic_path=db, threshold=1) is None  # no nudge beats a crash


# --- gated integration: real-anneal recall wires end-to-end -------------------------


@pytest.mark.skipif(not REAL_CRYSTAL.exists(), reason="no live crystal store")
def test_real_crystal_recall_wires(tmp_path):
    """Against the LIVE crystal store: inject runs end-to-end through real anneal recall.
    Asserts the wiring (constitution + a recall slot + a directive), not a specific pattern
    (recall thresholds are corpus-dependent — a specific hit would be a flaky assertion)."""
    f = AnnealFiring(constitution="GOVERNED")  # default path → the live store
    out = f.inject(InjectRequest(query="structural invariants beat discipline verify", turn_index=2))
    assert isinstance(out, str)
    assert "[recall" in out  # real patterns rendered OR a graceful marker — wiring proven
    assert select_directive(2) in out
    assert "unavailable" not in out  # real anneal ran without an exception
    # the constitution rides session_start (the set-once suffix), not the per-turn inject
    assert f.inject(InjectRequest(lifecycle_point="session_start")) == "GOVERNED"
