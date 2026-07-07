"""build_entity_agent — the isolated-entity agent chokepoint (spore-277).

Proves the structural enforcement point: the ONE blessed constructor fail-closes on the sovereignty
guard BEFORE building, binds the entity, and wires an OpenHands Agent whose firing/condenser use the
isolated kind (``anneal_entity``) — so the built agent recalls/captures only under the entity store.

Guarded on the ``openhands`` extra — skips cleanly where it's absent (levain's own core .venv).

    <venv-with-openhands>/bin/python -m pytest tests/test_firing_entity.py -p no:libtmux
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("openhands.sdk", reason="openhands.sdk not importable (openhands extra absent)")

from openhands.sdk import LLM  # noqa: E402

from levain.firing import build_firing  # noqa: E402
from levain.firing.isolation import LEVAIN_ENTITY_DIR_ENV, IsolationError, flow_store_dir  # noqa: E402
from levain.firing.openhands import (  # noqa: E402
    ENTITY_FIRING_KIND,
    EntityBinding,
    bind_entity,
    build_entity_agent,
)
from levain.firing.openhands.levain_condenser import LevainCondenser  # noqa: E402


def _entity(tmp_path: Path, name: str = "coyote") -> Path:
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True, exist_ok=True)
    return d


def _stub_llm() -> LLM:
    # Builds with no network — an Ollama endpoint config, never called by these tests.
    return LLM(model="ollama/nemotron-3-ultra:cloud", base_url="http://localhost:11434", usage_id="entity-test")


@pytest.fixture(autouse=True)
def _track_entity_env(monkeypatch):
    """Clean, UNBOUND slate for every entity test. bind_entity writes $LEVAIN_ENTITY_DIR directly
    (its process binding); the $VAGUS_* keys are the legacy "anneal"-kind overrides that the entity-
    aware _env_* consults as a FALLBACK — clear all three so a leak from another test can't skew
    resolution. Set to "" (not delenv): setenv ALWAYS records an undo, whereas delenv of an already-
    absent key is skipped by pytest and would leak bind_entity's later $LEVAIN_ENTITY_DIR write into
    the next file. "" reads as UNBOUND everywhere (.strip() → falsy); teardown deletes the keys."""
    for key in (LEVAIN_ENTITY_DIR_ENV, "VAGUS_EPISODIC_PATH", "VAGUS_CRYSTAL_PATH"):
        monkeypatch.setenv(key, "")


# --- bind_entity: the fail-closed guard ---------------------------------------------


def test_bind_entity_returns_resolved_stores_and_binds_env(tmp_path):
    import os

    ent = _entity(tmp_path)
    ed, crystal, episodic = bind_entity(ent)
    assert ed == ent.resolve()
    assert crystal == (ent / ".levain" / "memory.crystal.json").resolve()
    assert episodic == (ent / ".levain" / "memory.db").resolve()
    assert os.environ[LEVAIN_ENTITY_DIR_ENV] == str(ent.resolve())  # the fork-safe binding is set
    # NO $VAGUS_* backstop is written — the "anneal"-kind default is entity-aware + re-guarded per op
    # instead (a runtime guard, not a cached bind-time path — codex round-2).
    assert os.environ.get("VAGUS_EPISODIC_PATH", "") == ""


def test_bound_process_default_anneal_kind_resolves_to_entity(tmp_path):
    """After bind_entity, the laptop-defaulting "anneal" kind resolves to the ENTITY (not flow), via
    the entity-aware _env_* — so a stray bare vagus_run/wrap_nudge in the entity process can't leak
    (F1/F2), and it's re-guarded PER OP (a runtime guard, not a cached path — codex round-2)."""
    from levain.firing.anneal import AnnealFiring, _env_episodic_path

    ent = _entity(tmp_path)
    _, _, episodic = bind_entity(ent)
    assert _env_episodic_path() == episodic              # the "anneal" default now → the entity
    assert AnnealFiring()._resolve_episodic_path() == episodic


def test_env_default_reguards_per_op_on_post_bind_escape(tmp_path):
    """The entity-aware _env_* RE-GUARDS at USE time: bind a good entity, then swap .levain to escape
    the tree → _env_episodic_path RAISES (not a cached bind-time pass). Callers wrap it fail-soft."""
    from levain.firing.anneal import _env_episodic_path

    ent = _entity(tmp_path)
    bind_entity(ent)  # binds $LEVAIN_ENTITY_DIR; .levain is a real dir → passes
    # now relocate .levain OUT of the entity tree (post-bind FS mutation)
    (ent / ".levain").rmdir()
    (ent / ".levain").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(IsolationError, match="escapes the entity root"):
        _env_episodic_path()  # re-derived + re-guarded → refuses the escaped store


def test_bind_entity_refuses_rebind_to_different_entity(tmp_path):
    """Single-entity-per-process: a second bind to a DIFFERENT initialized entity is refused
    (the entity↔entity cross-wire, apparatus L1/complement). Idempotent same-entity re-bind is OK."""
    a = _entity(tmp_path, "entity_a")
    b = _entity(tmp_path, "entity_b")
    bind_entity(a)
    bind_entity(a)  # idempotent — same entity, no raise
    with pytest.raises(IsolationError, match="already bound to a different entity"):
        bind_entity(b)


def test_bind_entity_rejects_uninitialized_dir(tmp_path):
    """A dir with no .levain/ isn't an entity — fail loud, not a cryptic store-open failure later."""
    with pytest.raises(IsolationError, match="not an initialized Levain entity"):
        bind_entity(tmp_path / "never-init'd")


def test_bind_entity_rejects_dir_inside_flow_store(tmp_path, monkeypatch):
    """An entity dir UNDER the flow store would derive stores inside ~/.anneal-memory/ — refused."""
    monkeypatch.setenv("HOME", str(tmp_path))
    inside = flow_store_dir() / "sneaky-entity"
    (inside / ".levain").mkdir(parents=True)
    with pytest.raises(IsolationError, match="operator-laptop flow store"):
        bind_entity(inside)


# --- build_entity_agent: the isolated agent -----------------------------------------


def test_build_entity_agent_wires_isolated_kind(tmp_path):
    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())

    assert isinstance(binding, EntityBinding)
    assert binding.entity_dir == ent.resolve()
    assert binding.episodic_path == (ent / ".levain" / "memory.db").resolve()

    cond = binding.agent.condenser
    assert isinstance(cond, LevainCondenser)
    assert cond.firing_kind == ENTITY_FIRING_KIND == "anneal_entity"  # the isolated firing
    assert cond.presence_kind == "stub"                               # step-4 seed source not yet wired

    # the constitution rode into the set-once trusted suffix (session_start), no store access
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "governed cognitive substrate" in suffix


def test_built_agents_firing_resolves_to_entity_store(tmp_path):
    """The isolation carries THROUGH the built agent: with the binding env set, the agent's firing
    kind rebuilds (as a fork would) to an AnnealEntityFiring resolving to the ENTITY crystal — never
    the laptop default."""
    ent = _entity(tmp_path)
    build_entity_agent(ent, _stub_llm())  # sets $LEVAIN_ENTITY_DIR

    firing = build_firing(ENTITY_FIRING_KIND)  # the zero-arg rebuild a fork performs
    assert type(firing).__name__ == "AnnealEntityFiring"
    assert firing._resolve_crystal_path() == (ent / ".levain" / "memory.crystal.json").resolve()


def test_build_entity_agent_fail_closed_before_build(tmp_path, monkeypatch):
    """The guard fires BEFORE any agent construction — an uninitialized dir raises without ever
    touching the (network-free but real) SDK Agent path."""
    with pytest.raises(IsolationError, match="not an initialized"):
        build_entity_agent(tmp_path / "nope", _stub_llm())


# --- EntityBinding owns the capture + nudge lifecycle (F1/F2 explicit API) -----------


def test_capture_turn_pins_entity_kind(tmp_path, monkeypatch):
    """binding.capture_turn drives vagus_run with firing_kind='anneal_entity' (NOT vagus_run's
    laptop-defaulting 'anneal' default) — the owned, correct-by-construction capture path (F1)."""
    import levain.firing.openhands.capture as capmod

    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())
    seen: dict = {}
    monkeypatch.setattr(capmod, "vagus_run", lambda conv, **kw: seen.update(conv=conv, **kw))
    sentinel_conv = object()
    binding.capture_turn(sentinel_conv, session_id="s1")
    assert seen["conv"] is sentinel_conv
    assert seen["firing_kind"] == ENTITY_FIRING_KIND == "anneal_entity"
    assert seen["session_id"] == "s1"


def test_binding_wrap_nudge_reads_entity_never_flow(tmp_path, monkeypatch):
    """binding.wrap_nudge counts THIS entity's episodes (passing the guarded episodic_path) and never
    opens flow's memory.db (F2). REAL end-to-end: real anneal capture into the entity store."""
    from levain.firing import CaptureRequest
    from levain.firing.anneal import AnnealEntityFiring

    monkeypatch.setenv("HOME", str(tmp_path))  # fresh empty flow-store home
    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())  # binds env
    f = AnnealEntityFiring()  # env-bound → the entity store
    for i in range(4):
        f.capture(CaptureRequest(content=f"entity episode {i}", source="vagus"))
    out = binding.wrap_nudge(threshold=3)
    assert out is not None and "4 episodes" in out          # counted the ENTITY's episodes
    assert not (flow_store_dir() / "memory.db").exists()    # flow's db never created/read


def test_binding_wrap_nudge_reguards_on_post_bind_escape(tmp_path):
    """binding.wrap_nudge RE-DERIVES + RE-GUARDS from entity_dir at use time (not the cached
    episodic_path), so a post-bind .levain symlink-swap can't relocate the read to flow's store
    (codex round-2 TOCTOU). A tripped guard degrades to None (no nudge), never a wrong-store read."""
    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())  # episodic_path cached at bind time
    # relocate .levain OUT of the entity tree after binding
    (ent / ".levain").rmdir()
    (tmp_path / "elsewhere").mkdir()
    (ent / ".levain").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert binding.wrap_nudge(threshold=1) is None  # re-guard trips → fail-soft None, no leak
