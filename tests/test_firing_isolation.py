"""Isolation tests (spore-277) — the sovereignty guard for a runnable ISOLATED Levain entity.

The #1 requirement: an OpenHands entity firing must recall/capture ONLY under its own
``<entity>/.levain/`` store and NEVER touch the operator-laptop flow store (``~/.anneal-memory/``).
These prove it STRUCTURALLY (``structural_invariants_beat_discipline``), not by env-discipline:

  - the pure guard (``entity_store_paths`` / ``assert_entity_isolated`` / ``resolve_entity_dir``),
  - the isolated firing kind ``"anneal_entity"`` (``AnnealEntityFiring``): resolves under the
    entity, fail-closes to no-recall when unbound, refuses a raw path override,
  - the MOAT proof, two halves: recall ROUTING (which crystal path is opened — decoy flow store
    present) is deterministic; capture ROUTING is REAL end-to-end (the episode lands in the entity
    db; flow's db is never created),
  - fork/reload survival (the env-bound entity survives a zero-arg registry rebuild),
  - the dependency-isolated-leaf invariant for the new pure module.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from anneal_memory.types import RelevantPattern

from levain.firing import CaptureRequest, InjectRequest, build_firing
from levain.firing.anneal import AnnealEntityFiring
from levain.firing.isolation import (
    LEVAIN_ENTITY_DIR_ENV,
    IsolationError,
    assert_entity_isolated,
    entity_store_paths,
    flow_store_dir,
    resolve_entity_dir,
)


# --- helpers ------------------------------------------------------------------------


def _entity(tmp_path: Path, name: str = "coyote") -> Path:
    """A freshly-init'd entity dir (its ``.levain/`` exists, as after ``levain init``)."""
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True, exist_ok=True)
    return d


def _existing_crystal(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")  # exists; CrystalStore is monkeypatched, so contents are irrelevant
    return path


def _fake_pattern(name: str) -> RelevantPattern:
    return RelevantPattern(  # type: ignore[arg-type]
        name=name,
        level=3,
        explanation="isolation keeps the entity's memory sovereign",
        tags=["architecture"],
        activation="warm",
        score=5.0,
        source="keyword",
    )


# --- the pure guard: derivation -----------------------------------------------------


def test_entity_store_paths_derives_under_levain(tmp_path):
    ent = _entity(tmp_path)
    crystal, episodic = entity_store_paths(ent)
    assert crystal == (ent / ".levain" / "memory.crystal.json").resolve()
    assert episodic == (ent / ".levain" / "memory.db").resolve()


# --- the pure guard: resolve_entity_dir (fail-closed when unbound) -------------------


def test_resolve_entity_dir_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)
    assert resolve_entity_dir(tmp_path / "e") == (tmp_path / "e")


def test_resolve_entity_dir_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(tmp_path / "envent"))
    assert resolve_entity_dir() == (tmp_path / "envent")


def test_resolve_entity_dir_unbound_raises(monkeypatch):
    """No explicit dir + no env → FAIL CLOSED (never a default that could be flow's store)."""
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)
    with pytest.raises(IsolationError, match="no entity bound"):
        resolve_entity_dir()


# --- the pure guard: assert_entity_isolated -----------------------------------------


def test_guard_passes_for_legit_entity_paths(tmp_path):
    ent = _entity(tmp_path)
    crystal, episodic = entity_store_paths(ent)
    assert_entity_isolated(crystal, episodic, entity_dir=ent)  # no raise


def test_guard_rejects_flow_store_paths(tmp_path, monkeypatch):
    """A resolved path under ~/.anneal-memory/ is refused, named explicitly."""
    monkeypatch.setenv("HOME", str(tmp_path))  # repoint the flow store into the sandbox
    ent = _entity(tmp_path)
    flow_crystal = flow_store_dir() / "memory.crystal.json"
    with pytest.raises(IsolationError, match="operator-laptop flow store"):
        assert_entity_isolated(flow_crystal, entity_dir=ent)


def test_guard_rejects_path_escaping_sovereign_dir(tmp_path):
    """A path NOT under <entity>/.levain/ (but not the flow store either) is still refused."""
    ent = _entity(tmp_path)
    stray = tmp_path / "elsewhere" / "memory.db"
    with pytest.raises(IsolationError, match="escapes the entity's sovereign"):
        assert_entity_isolated(stray, entity_dir=ent)


def test_guard_catches_levain_symlink_into_flow_store(tmp_path, monkeypatch):
    """Defense-in-depth: if <entity>/.levain SYMLINKS into the flow store, .resolve() follows it —
    the forbidden-zone check (run FIRST) catches it, not the containment check (which would pass)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    flow = flow_store_dir()
    flow.mkdir(parents=True)
    ent = tmp_path / "entity"
    ent.mkdir()
    (ent / ".levain").symlink_to(flow, target_is_directory=True)  # the escape
    crystal, episodic = entity_store_paths(ent)  # resolve through the symlink → flow store
    with pytest.raises(IsolationError, match="operator-laptop flow store"):
        assert_entity_isolated(crystal, episodic, entity_dir=ent)


# --- AnnealEntityFiring: construction + refusal of raw path overrides ----------------


def test_entity_firing_refuses_raw_path_override(tmp_path):
    """A crystal_path/episodic_path override could escape the sovereign dir — refused."""
    with pytest.raises(ValueError, match="does not accept crystal_path"):
        AnnealEntityFiring(entity_dir=_entity(tmp_path), crystal_path=tmp_path / "x.json")
    with pytest.raises(ValueError, match="does not accept crystal_path"):
        AnnealEntityFiring(entity_dir=_entity(tmp_path), episodic_path=tmp_path / "x.db")


def test_entity_firing_resolves_under_entity(tmp_path):
    ent = _entity(tmp_path)
    f = AnnealEntityFiring(entity_dir=ent)
    assert f._resolve_crystal_path() == (ent / ".levain" / "memory.crystal.json").resolve()
    assert f._resolve_episodic_path() == (ent / ".levain" / "memory.db").resolve()


# --- MOAT: recall ROUTING is deterministic (decoy flow store present) ----------------


def test_recall_opens_entity_crystal_never_flow(tmp_path, monkeypatch):
    """THE MOAT (recall half). With BOTH an entity crystal AND a decoy flow crystal on disk, the
    entity firing opens the ENTITY crystal and NEVER the flow crystal — proven by capturing the
    exact path CrystalStore is constructed with (the isolation property IS which store is opened)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # the flow store lives under the sandbox
    ent = _entity(tmp_path)
    entity_crystal = _existing_crystal(ent / ".levain" / "memory.crystal.json")
    flow_crystal = _existing_crystal(flow_store_dir() / "memory.crystal.json")  # the decoy

    opened: list[Path] = []
    monkeypatch.setattr(
        "anneal_memory.crystal.CrystalStore",
        lambda path: opened.append(Path(path)) or object(),
    )
    monkeypatch.setattr(
        "anneal_memory.retrieval.retrieve_patterns",
        lambda store, query, **kw: [_fake_pattern("entity_pattern")],
    )

    f = AnnealEntityFiring(entity_dir=ent)
    out = f.inject(InjectRequest(query="which store does isolation open", turn_index=0))

    assert "entity_pattern" in out
    assert opened == [entity_crystal.resolve()]   # opened the entity store, exactly once
    assert flow_crystal.resolve() not in opened   # NEVER the flow store — the moat holds


def test_recall_env_bound_matches_explicit(tmp_path, monkeypatch):
    """Binding via $LEVAIN_ENTITY_DIR resolves identically to an explicit entity_dir — the
    fork-safe channel and the in-process channel agree."""
    ent = _entity(tmp_path)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))
    f_env = AnnealEntityFiring()          # env-bound (the fork-rebuilt shape)
    f_explicit = AnnealEntityFiring(entity_dir=ent)
    assert f_env._resolve_crystal_path() == f_explicit._resolve_crystal_path()
    assert f_env._resolve_episodic_path() == f_explicit._resolve_episodic_path()


# --- MOAT: capture ROUTING is REAL end-to-end ---------------------------------------


def test_capture_writes_entity_db_never_flow_db(tmp_path, monkeypatch):
    """THE MOAT (capture half, REAL anneal). A captured episode lands in <entity>/.levain/memory.db
    and the flow store's memory.db is NEVER created — the write-leak (the dangerous one: it would
    corrupt flow's memory) is structurally closed."""
    from anneal_memory import Store

    monkeypatch.setenv("HOME", str(tmp_path))  # a fresh, empty flow-store home
    ent = _entity(tmp_path)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))

    ok = AnnealEntityFiring().capture(
        CaptureRequest(content="entity turn: the sovereign build ran", source="vagus", session_id="s1")
    )
    assert ok is True

    entity_db = ent / ".levain" / "memory.db"
    with Store(entity_db) as store:
        contents = [e.content for e in store.episodes_since_wrap()]
    assert "entity turn: the sovereign build ran" in contents      # landed in the ENTITY store
    assert not (flow_store_dir() / "memory.db").exists()           # flow's db NEVER created


# --- fail-closed-to-SAFE: unbound firing never leaks --------------------------------


def test_unbound_recall_fails_soft_never_reads_flow(tmp_path, monkeypatch):
    """Env unset + no entity_dir: recall degrades to a marker (IsolationError caught by fail-soft)
    and NEVER opens the flow crystal — fail-closed-to-safe, not fail-open-to-leak."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)
    _existing_crystal(flow_store_dir() / "memory.crystal.json")  # a flow store IS present

    opened: list[Path] = []
    monkeypatch.setattr(
        "anneal_memory.crystal.CrystalStore", lambda path: opened.append(Path(path)) or object()
    )
    f = AnnealEntityFiring()  # unbound
    out = f.inject(InjectRequest(query="distinctive keywords here", turn_index=0))
    assert "unavailable" in out and "IsolationError" in out  # degraded, not raised
    assert opened == []                                       # never opened ANY crystal store


def test_unbound_capture_fails_soft_never_writes_flow(tmp_path, monkeypatch, caplog):
    """Env unset: capture returns False + logs loud, and the flow db is NEVER written."""
    import logging

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="levain.firing.anneal"):
        ok = AnnealEntityFiring().capture(CaptureRequest(content="should never land in flow"))
    assert ok is False
    assert any("episode LOST" in r.message for r in caplog.records)
    assert not (flow_store_dir() / "memory.db").exists()  # flow's db untouched


# --- fork / reload survival ----------------------------------------------------------


def test_entity_firing_survives_registry_rebuild(tmp_path, monkeypatch):
    """The isolation contract rides the SERIALIZED firing_kind: build_firing('anneal_entity') (the
    zero-arg rebuild a fork/reload performs) reconstructs an AnnealEntityFiring that STILL resolves
    to the env-bound entity — never the laptop-defaulting AnnealFiring."""
    ent = _entity(tmp_path)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))
    rebuilt = build_firing("anneal_entity")
    assert type(rebuilt).__name__ == "AnnealEntityFiring"
    assert rebuilt._resolve_crystal_path() == (ent / ".levain" / "memory.crystal.json").resolve()


def test_build_firing_anneal_entity_cold_lazy_registers():
    """Cold-interpreter serialization safety (mirrors the 'anneal' cold test): a fresh subprocess
    that imported only the contract must reconstruct 'anneal_entity' via the lazy import path."""
    code = (
        "import levain.firing.contract as f; "
        "from levain.firing.contract import _FIRING_REGISTRY; "
        "assert 'anneal_entity' not in _FIRING_REGISTRY, 'leaf pre-registered — cold path untested'; "
        "obj = f.build_firing('anneal_entity'); "
        "assert type(obj).__name__ == 'AnnealEntityFiring', type(obj).__name__; "
        "assert 'anneal_entity' in _FIRING_REGISTRY"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- dependency-isolated leaf (the new pure module) ---------------------------------


def test_isolation_module_imports_without_anneal_or_openhands():
    """levain.firing.isolation is pure stdlib — importing it pulls neither anneal nor openhands."""
    code = (
        "import sys; import levain.firing.isolation; "
        "leaked = sorted(m for m in sys.modules if m.startswith(('anneal_memory', 'openhands'))); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- guard hardening from the L3 apparatus ------------------------------------------


def test_guard_rejects_levain_symlink_to_nonflow_external_dir(tmp_path):
    """codex L3: a <entity>/.levain symlinked to an arbitrary NON-flow external dir makes the
    containment check pass vacuously (sovereign follows the same symlink). The sovereign-under-root
    check rejects it — the store must stay in the entity tree, not just out of the flow store."""
    external = tmp_path / "shared-store"
    external.mkdir()
    ent = tmp_path / "entity"
    ent.mkdir()
    (ent / ".levain").symlink_to(external, target_is_directory=True)
    crystal, episodic = entity_store_paths(ent)  # resolve into the external dir
    with pytest.raises(IsolationError, match="escapes the entity root"):
        assert_entity_isolated(crystal, episodic, entity_dir=ent)


def test_is_within_ci_catches_case_variant_of_forbidden():
    """L1-F4: on a case-insensitive volume ~/.Anneal-Memory is the SAME dir as ~/.anneal-memory but
    relative_to compares case-sensitively. _is_within_ci casefolds to close it; separator-anchored
    so it never false-matches a sibling like .anneal-memory-backup."""
    from levain.firing.isolation import _is_within_ci

    forbidden = Path("/home/u/.anneal-memory")
    assert _is_within_ci(Path("/home/u/.Anneal-Memory/memory.db"), forbidden)  # case variant → caught
    assert _is_within_ci(Path("/home/u/.anneal-memory/memory.db"), forbidden)  # exact → caught
    assert _is_within_ci(Path("/home/u/.anneal-memory"), forbidden)            # equal → caught
    assert not _is_within_ci(Path("/home/u/.anneal-memory-backup/x"), forbidden)   # sibling → NOT
    assert not _is_within_ci(Path("/home/u/entity/.levain/memory.db"), forbidden)  # unrelated → NOT


# --- REAL end-to-end recall (not monkeypatched) -------------------------------------


def test_entity_firing_real_recall_end_to_end(tmp_path, monkeypatch):
    """REAL anneal recall (no monkeypatch): crystallize a pattern into the ENTITY's own crystal
    store, then AnnealEntityFiring recalls it — the whole path wires against actual anneal, entity-
    scoped. (The routing tests prove WHICH store is opened; this proves that store really recalls.)"""
    from anneal_memory.crystal import CrystalStore

    monkeypatch.setenv("HOME", str(tmp_path))
    ent = _entity(tmp_path)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))
    crystal, _ = entity_store_paths(ent)
    CrystalStore(crystal).crystallize(
        name="entity_sovereign_isolation",
        level=3,
        explanation="isolation keeps the entity memory sovereign under its own levain dir",
        tags=["architecture", "isolation"],
    )
    out = AnnealEntityFiring().inject(
        InjectRequest(query="how does isolation keep entity memory sovereign", turn_index=0)
    )
    assert "entity_sovereign_isolation" in out  # the REAL entity pattern recalled end-to-end
    assert "unavailable" not in out             # real anneal ran without an exception


# --- L1 coverage gaps ----------------------------------------------------------------


def test_entity_firing_session_start_constitution_when_unbound(monkeypatch):
    """session_start returns the constitution WITHOUT resolving any store — so vagus_agent_context
    can build the suffix even before an entity is bound (no env). No IsolationError, no store touch.
    (build_entity_agent relies on this: a fresh AnnealEntityFiring's session_start inject.)"""
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)
    out = AnnealEntityFiring().inject(InjectRequest(lifecycle_point="session_start"))
    assert "governed cognitive substrate" in out  # the default constitution, no resolution


def test_capture_guard_trip_mid_op_fails_soft(tmp_path, monkeypatch, caplog):
    """env IS bound, but the entity's .levain symlinks OUT of the entity tree → the guard trips
    INSIDE capture → fail-soft-loud (episode lost, returns False), never a write to the escaped
    store. (L1 gap: only the unbound + pure-guard cases were covered before.)"""
    import logging

    external = tmp_path / "shared"
    external.mkdir()
    ent = tmp_path / "entity"
    ent.mkdir()
    (ent / ".levain").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))
    with caplog.at_level(logging.WARNING, logger="levain.firing.anneal"):
        ok = AnnealEntityFiring().capture(CaptureRequest(content="must not escape the entity tree"))
    assert ok is False
    assert any("episode LOST" in r.message for r in caplog.records)
    assert not (external / "memory.db").exists()  # nothing written to the escaped external store
