"""build_entity_agent — the isolated-entity agent chokepoint (spore-277).

Proves the structural enforcement point: the ONE blessed constructor fail-closes on the sovereignty
guard BEFORE building, binds the entity, and wires an OpenHands Agent whose firing/condenser use the
isolated kind (``anneal_entity``) — so the built agent recalls/captures only under the entity store.

Guarded on the ``openhands`` extra — skips cleanly where it's absent (levain's own core .venv).

    <venv-with-openhands>/bin/python -m pytest tests/test_firing_entity.py -p no:libtmux
"""
from __future__ import annotations

import os
import sys

from pathlib import Path

import pytest

pytest.importorskip("openhands.sdk", reason="openhands.sdk not importable (openhands extra absent)")

from openhands.sdk import LLM  # noqa: E402

from levain.firing import build_firing  # noqa: E402
from levain.firing.binding import LEVAIN_ENTITY_PROCESS_ENV, ConversationBinding  # noqa: E402
from levain.firing.isolation import IsolationError, flow_store_dir  # noqa: E402
from levain.firing.openhands import (  # noqa: E402
    ENTITY_FIRING_KIND,
    EntityBinding,
    build_entity_agent,
    guard_entity,
)
from levain.firing.openhands.levain_condenser import LevainCondenser  # noqa: E402


def _entity(tmp_path: Path, name: str = "coyote") -> Path:
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True, exist_ok=True)
    return d


def _seed(entity_dir: Path, *, name: str = "Coyote", operator: str = "Avery Lane") -> Path:
    """Lay down a minimal filled ``seed/`` so ``build_entity_agent`` has a real identity to render
    (step 4). Enough for the constitution to prove "boots as itself": an origin (who it is), a world
    (its operator), a partnership (the floor)."""
    seed = entity_dir / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "origin.md").write_text(
        f"# Who You Are — {name}\n\n"
        f"> Part of the seed. Template guidance — must be stripped.\n\n"
        f"You are **{name}**. You run on minimax-m3.\n\n"
        f"<!-- interview comment — must be stripped -->\n\n"
        f"Your job: be a sovereign partner to {operator}.\n",
        encoding="utf-8",
    )
    (seed / "world.md").write_text(
        f"# Who Your Operator Is\n\n## Identity\n\n{operator}. 39. Riverton.\n", encoding="utf-8"
    )
    (seed / "partnership.md").write_text(
        "# How We Work\n\nYou are a partner, not an assistant.\n", encoding="utf-8"
    )
    return entity_dir


def _hands(ent: Path, *, mode: str = "interactive") -> list:
    """The entity's confined hands, built the way the session builds them: from ONE binding."""
    from levain.firing.openhands.tools import build_entity_tools

    return build_entity_tools(
        ConversationBinding.create(ent, mode=mode, workspace=ent / "workspace")  # type: ignore[arg-type]
    )


def _stub_llm() -> LLM:
    # Builds with no network — an Ollama endpoint config, never called by these tests.
    return LLM(model="ollama/nemotron-3-ultra:cloud", base_url="http://localhost:11434", usage_id="entity-test")


@pytest.fixture(autouse=True)
def _track_entity_env(monkeypatch):
    """Clean slate for every entity test: the $VAGUS_* keys are the legacy "anneal"-kind overrides,
    and $LEVAIN_ENTITY_DIR is the retired process channel (cleared in case an outer shell exports
    it). The entity-process latch is reset by conftest. Set to "" (not delenv): setenv ALWAYS
    records an undo."""
    for key in ("LEVAIN_ENTITY_DIR", "VAGUS_EPISODIC_PATH", "VAGUS_CRYSTAL_PATH"):
        monkeypatch.setenv(key, "")


# --- guard_entity: the fail-closed guard ---------------------------------------------


def test_guard_entity_returns_resolved_stores_and_writes_no_process_state(tmp_path):
    ent = _entity(tmp_path)
    ed, crystal, episodic = guard_entity(ent)
    assert ed == ent.resolve()
    assert crystal == (ent / ".levain" / "memory.crystal.json").resolve()
    assert episodic == (ent / ".levain" / "memory.db").resolve()
    # spore-438: an entity is carried by the objects that serve it, never published to the process.
    assert os.environ.get("LEVAIN_ENTITY_DIR", "") == ""
    assert os.environ.get(LEVAIN_ENTITY_PROCESS_ENV) is None
    assert os.environ.get("VAGUS_EPISODIC_PATH", "") == ""


def test_an_entity_process_refuses_every_default_kind_store_op(tmp_path, monkeypatch):
    """spore-438 §6 (Phill 2026-10-02, "approve levain design"): once a process has built an entity
    agent, the laptop-defaulting "anneal" kind REFUSES — a stray bare vagus_run / wrap_nudge can
    neither read nor write ~/.anneal-memory, and is never redirected to an entity (the latch names
    none, so two entities in one process change nothing). An explicit entity firing is unaffected."""

    from levain.firing import CaptureRequest
    from levain.firing.anneal import AnnealEntityFiring, AnnealFiring, _env_episodic_path

    import levain.firing.anneal as anneal_mod

    # The defaults are frozen at import (real HOME), so point them at a tmp "flow store" — otherwise
    # the "never created" check below would look at a path the op never could have used.
    fake_flow = tmp_path / ".anneal-memory"
    monkeypatch.setattr(anneal_mod, "DEFAULT_EPISODIC_PATH", fake_flow / "memory.db")
    monkeypatch.setattr(anneal_mod, "DEFAULT_CRYSTAL_PATH", fake_flow / "memory.crystal.json")
    a = _entity(tmp_path, "entity_a")
    b = _entity(tmp_path, "entity_b")
    assert AnnealFiring()._resolve_episodic_path() == fake_flow / "memory.db"  # not latched yet
    build_entity_agent(a, _stub_llm())
    build_entity_agent(b, _stub_llm())
    assert os.environ[LEVAIN_ENTITY_PROCESS_ENV] == "1"
    with pytest.raises(IsolationError, match="hosts a Levain entity"):
        _env_episodic_path()
    # codex L3 r1: scrubbing the environment must not un-latch this process (the env var only
    # carries the latch to children).
    del os.environ[LEVAIN_ENTITY_PROCESS_ENV]
    with pytest.raises(IsolationError, match="hosts a Levain entity"):
        _env_episodic_path()
    with logging_capture() as records:
        assert AnnealFiring().capture(CaptureRequest(content="must never reach flow")) is False
    assert any("episode LOST" in r.getMessage() for r in records)
    assert not (fake_flow / "memory.db").exists()                    # flow's store never created
    for ent in (a, b):                                               # each explicit entity: its own
        got = AnnealEntityFiring(entity_dir=ent)._resolve_episodic_path()
        assert got.parent == (ent / ".levain").resolve()


class logging_capture:
    """Collect WARNING+ records from levain.firing.anneal for the duration of a block."""

    def __enter__(self):
        import logging

        self.records: list = []
        self._h = logging.Handler(level=logging.WARNING)
        self._h.emit = self.records.append  # type: ignore[method-assign]
        self._log = logging.getLogger("levain.firing.anneal")
        self._log.addHandler(self._h)
        return self.records

    def __exit__(self, *exc):
        self._log.removeHandler(self._h)


def test_guard_entity_rejects_uninitialized_dir(tmp_path):
    """A dir with no .levain/ isn't an entity — fail loud, not a cryptic store-open failure later."""
    with pytest.raises(IsolationError, match="not an initialized Levain entity"):
        guard_entity(tmp_path / "never-init'd")


def test_guard_entity_rejects_dir_inside_flow_store(tmp_path, monkeypatch):
    """An entity dir UNDER the flow store would derive stores inside ~/.anneal-memory/ — refused."""
    monkeypatch.setenv("HOME", str(tmp_path))
    inside = flow_store_dir() / "sneaky-entity"
    (inside / ".levain").mkdir(parents=True)
    with pytest.raises(IsolationError, match="operator-laptop flow store"):
        guard_entity(inside)


# --- build_entity_agent: the isolated agent -----------------------------------------


def test_confined_file_tool_lands_on_the_built_agent(tmp_path):
    """The #1 confinement invariant, end-to-end (apparatus L1 WARNING-1 regression guard): a REAL
    built agent, once initialized, resolves ``file_editor`` to the CONFINED executor — not the stock
    unconfined one. The whole moat rests on three SDK behaviors (the registry resolves the spec via
    ``.create``, ``set_executor`` sticks, ``tools_map`` keys on the resolved ``.name``); a future SDK
    bump could silently break any one and hand the entity the UNCONFINED editor with every unit test
    still green. This asserts the resolved runtime tool, so that regression fails LOUD."""
    from openhands.sdk import Conversation

    from levain.firing.openhands.tools import CrownJewelsFileEditorExecutor

    ent = _seed(_entity(tmp_path))
    ws = ent / "workspace"
    ws.mkdir()
    binding = build_entity_agent(ent, _stub_llm(), tools=_hands(ent))
    conv = Conversation(binding.agent, workspace=str(ws), visualizer=None)
    try:
        conv.agent.init_state(conv._state, on_event=lambda _e: None)  # resolves tools, no LLM call
        tool = conv.agent.tools_map["file_editor"]  # the LLM-visible name (not the spec key)
        assert isinstance(tool.executor, CrownJewelsFileEditorExecutor)
        # the floored executor's policy fences the run's workspace to the crown-jewels floor
        assert tool.executor._policy.workspace == ws.resolve()
    finally:
        conv.close()


def test_build_entity_agent_wires_isolated_kind(tmp_path):
    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())

    assert isinstance(binding, EntityBinding)
    assert binding.entity_dir == ent.resolve()
    assert binding.episodic_path == (ent / ".levain" / "memory.db").resolve()

    cond = binding.agent.condenser
    assert isinstance(cond, LevainCondenser)
    assert cond.firing_kind == ENTITY_FIRING_KIND == "anneal_entity"  # the isolated firing
    assert cond.presence_kind == "entity_seed"                        # step-4: the seed re-anchor

    # apparatus L2 MED: tool calls are serialized STRUCTURALLY (pinned, not the SDK default) so a
    # concurrent bash+file-editor batch can't TOCTOU-swap a symlink between the crown-jewels check and
    # the un-sandboxed editor's open().
    assert binding.agent.tool_concurrency_limit == 1

    # A bare .levain-only entity (no seed/) has no readable identity → the constitution FALLS BACK
    # to the firing's generic default (session_start), still baked into the set-once suffix.
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "governed cognitive substrate" in suffix


def test_build_entity_agent_boots_as_itself_from_seed(tmp_path):
    """Step 4 (spore-294): a fresh entity WITH a seed boots as ITSELF — its origin identity + operator
    + partnership floor ride the set-once constitution suffix (so "who are you?" is the seed, not the
    model's stock "I am OpenHands"), and the re-anchor kind is the seed source. Guidance blockquotes +
    interview comments are stripped; the generic default is REPLACED (not appended)."""
    ent = _seed(_entity(tmp_path), name="Coyote", operator="Avery Lane")
    binding = build_entity_agent(ent, _stub_llm())

    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "Coyote" in suffix                       # identity (origin.md)
    assert "Avery Lane. 39. Riverton." in suffix  # operator (world.md-only text)
    assert "partner, not an assistant" in suffix    # floor (partnership.md)
    assert "governed cognitive substrate" not in suffix  # the generic default was REPLACED
    assert "guidance blockquote" not in suffix      # > blockquote stripped
    assert "interview comment" not in suffix        # <!-- --> stripped

    cond = binding.agent.condenser
    assert cond.presence_kind == "entity_seed"


def test_build_entity_agent_warns_on_present_but_unreadable_seed(tmp_path, caplog):
    """apparatus HIGH-2: a seed/ dir that is PRESENT but yields no constitution (here: origin/world/
    partnership all symlinked outside the tree → refused) must NOT silently boot generic — it warns
    LOUD, so a broken step-4 identity surfaces instead of degrading invisibly."""
    import logging

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    ent = _entity(tmp_path)
    seed = ent / "seed"
    seed.mkdir()
    for name in ("origin.md", "world.md", "partnership.md"):
        (foreign / name).write_text(f"# {name}\n\nforeign content\n")
        (seed / name).symlink_to(foreign / name)  # per-file escapes → all refused

    with caplog.at_level(logging.WARNING, logger="levain.firing.openhands.entity"):
        binding = build_entity_agent(ent, _stub_llm())

    assert any("no readable constitution" in r.message for r in caplog.records)
    # and it fell back to the generic default (not a crash, not a foreign identity)
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "governed cognitive substrate" in suffix
    assert "foreign content" not in suffix


_NEOCORTEX = (
    "## State\nMidway through the PARITY_BUG_FIX; even-length median is wrong.\n\n"
    "## Active Threads\n- mediancalc bugfix\n\n## Patterns\n- parallelize_reads | 1x\n\n"
    "## Decisions\n- start with small repos\n\n## Context\nFirst real session with Avery.\n\n"
    "## Understanding\nEarly — the seed is planted.\n"
)


def test_build_entity_agent_folds_neocortex_into_the_constitution(tmp_path):
    """spore-359: a fresh run boots on the entity's OWN consolidated memory, not only crystal recall.
    With a seed AND a written neocortex, the set-once session_start suffix carries BOTH the static seed
    identity AND the dynamic neocortex (State/Context…), under the memory preamble that marks it as
    lived memory — the seed (birth) precedes the memory (life)."""
    ent = _seed(_entity(tmp_path), name="Coyote", operator="Avery Lane")
    (ent / ".levain" / "memory.continuity.md").write_text(_NEOCORTEX, encoding="utf-8")

    binding = build_entity_agent(ent, _stub_llm())
    suffix = binding.agent.agent_context.system_message_suffix or ""

    assert "Coyote" in suffix                                   # seed identity still present
    assert "partner, not an assistant" in suffix               # …and the floor
    assert "Your Memory — carried from your prior sessions" in suffix  # the memory preamble marker
    assert "PARITY_BUG_FIX" in suffix                          # State surfaced
    assert "First real session with Avery" in suffix           # Context surfaced
    assert suffix.index("Coyote") < suffix.index("Your Memory — carried")  # seed before memory


def test_build_entity_agent_seed_only_when_never_wrapped(tmp_path):
    """A never-wrapped entity (no .levain/memory.continuity.md) boots on the seed alone — the memory
    preamble is absent, no crash. The expected young-entity path (fail-soft on an absent neocortex)."""
    ent = _seed(_entity(tmp_path))
    assert not (ent / ".levain" / "memory.continuity.md").exists()

    binding = build_entity_agent(ent, _stub_llm())
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "Coyote" in suffix
    assert "Your Memory — carried from your prior sessions" not in suffix


def test_neocortex_injection_refuses_symlink_escape(tmp_path, caplog):
    """The neocortex read is re-guarded at the point of use: a .levain/memory.continuity.md symlinked
    OUTSIDE the entity tree is refused — never read into the always-loaded context — and the entity
    boots seed-only with a loud warning (the same afferent read-leak floor the store + seed guards
    close)."""
    import logging

    foreign = tmp_path / "foreign_memory.md"
    foreign.write_text("## State\nFOREIGN_LEAK_MARKER\n", encoding="utf-8")
    ent = _seed(_entity(tmp_path))
    (ent / ".levain" / "memory.continuity.md").symlink_to(foreign)

    with caplog.at_level(logging.WARNING, logger="levain.firing.openhands.entity"):
        binding = build_entity_agent(ent, _stub_llm())

    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "FOREIGN_LEAK_MARKER" not in suffix                  # NOT read into context
    assert "Your Memory — carried from your prior sessions" not in suffix
    assert "Coyote" in suffix                                   # seed still boots
    assert any("resolves outside the entity tree" in r.message for r in caplog.records)


def test_compose_constitution_memory_augments_a_real_seed_only(tmp_path):
    """_compose_constitution: memory augments a REAL seed only. A None seed stays None (the generic
    firing default), never a bare memory block that would lack the identity framing."""
    from levain.firing.openhands.entity import _compose_constitution

    assert _compose_constitution(None, "## State\nx") is None    # seedless → generic, no memory
    assert _compose_constitution("SEED", None) == "SEED"          # no memory → seed unchanged
    assert _compose_constitution("SEED", "MEM") == "SEED\n\nMEM"  # both → seed then memory


@pytest.mark.skipif(
    sys.version_info >= (3, 13),
    reason="Path.resolve() stopped raising RuntimeError on a symlink loop in 3.13, so the log line "
           "this asserts is never written; the boot itself still completes. Unsettled: the 3.13 "
           "behaviour of the entity-isolation guard on a loop has not been decided.")
def test_neocortex_injection_fail_soft_on_symlink_loop(tmp_path, caplog):
    """A symlink LOOP at .levain/memory.continuity.md must NOT crash the boot — `assert_entity_isolated`
    `.resolve()`s internally and raises `RuntimeError` (not `IsolationError`) on a loop; the guard call
    catches it and degrades to seed-only (L1 review F1: this used to hard-crash the REPL boot)."""
    import logging

    ent = _seed(_entity(tmp_path))
    loop = ent / ".levain" / "memory.continuity.md"
    loop.symlink_to(loop)  # self-referential → RuntimeError on resolve()
    with caplog.at_level(logging.WARNING, logger="levain.firing.openhands.entity"):
        binding = build_entity_agent(ent, _stub_llm())  # must not raise
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "Coyote" in suffix
    assert "Your Memory — carried from your prior sessions" not in suffix
    assert any("could not be resolved" in r.message for r in caplog.records)


def test_neocortex_injection_fail_soft_on_empty_neocortex(tmp_path):
    """An empty / whitespace-only neocortex → no memory block (seed-only boot), no crash."""
    ent = _seed(_entity(tmp_path))
    (ent / ".levain" / "memory.continuity.md").write_text("   \n\n  ", encoding="utf-8")
    binding = build_entity_agent(ent, _stub_llm())
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "Coyote" in suffix
    assert "Your Memory — carried from your prior sessions" not in suffix


def test_neocortex_injection_fail_soft_on_non_utf8(tmp_path):
    """A non-UTF-8 neocortex (a bad byte) → no memory block, never a UnicodeDecodeError into build."""
    ent = _seed(_entity(tmp_path))
    (ent / ".levain" / "memory.continuity.md").write_bytes(b"## State\n\xff\xfe bad bytes\n")
    binding = build_entity_agent(ent, _stub_llm())
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "Coyote" in suffix
    assert "Your Memory — carried from your prior sessions" not in suffix


def test_act_first_directive_injected_when_the_entity_has_tools(tmp_path):
    """Pre-emptive act-first fix (bake-off 2026-07-17): a tool-having entity's system message carries
    the act-first directive, so a task turn starts with a tool call, not a plan-as-prose stall."""
    ent = _seed(_entity(tmp_path))
    binding = build_entity_agent(ent, _stub_llm(), tools=_hands(ent))
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "ACT, don't narrate" in suffix
    assert "Coyote" in suffix  # …still after the seed identity, not replacing it


def test_act_first_directive_absent_for_a_no_tools_partner(tmp_path):
    """A --no-tools conversational partner (tools=None) has nothing to act with — no directive."""
    ent = _seed(_entity(tmp_path))
    binding = build_entity_agent(ent, _stub_llm())  # tools defaults to None
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "ACT, don't narrate" not in suffix


def test_act_first_directive_injected_for_a_seedless_but_tooled_entity(tmp_path):
    """codex L3 2026-07-17: the directive is about TOOL USE, not identity — a seedless entity that
    still has tools (it boots the generic default constitution) must get it too. The old
    `constitution is not None` gate skipped this path, so a seedless+tooled run silently lost the
    pre-emptive half of the fix and leaned on the run-loop backstop alone."""
    ent = _entity(tmp_path)  # .levain but NO seed/ → generic default constitution
    binding = build_entity_agent(ent, _stub_llm(), tools=_hands(ent))
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "ACT, don't narrate" in suffix


def test_build_entity_agent_seedless_drops_memory_even_if_neocortex_present(tmp_path):
    """F5c + the seedless policy: a seedless entity (no origin.md) with a neocortex present still boots
    the GENERIC default — memory augments a real seed only, never appears without identity framing (and
    the read is short-circuited, not even attempted)."""
    ent = _entity(tmp_path)  # .levain but NO seed/
    (ent / ".levain" / "memory.continuity.md").write_text(
        "## State\nSEEDLESS_MEMORY_MARKER\n", encoding="utf-8"
    )
    binding = build_entity_agent(ent, _stub_llm())
    suffix = binding.agent.agent_context.system_message_suffix or ""
    assert "governed cognitive substrate" in suffix    # the generic default (no seed)
    assert "SEEDLESS_MEMORY_MARKER" not in suffix       # memory NOT injected without a seed
    assert "Your Memory — carried from your prior sessions" not in suffix


def test_built_agents_firing_resolves_to_entity_store(tmp_path, monkeypatch):
    """The isolation carries THROUGH the built agent AND a fork: the condenser serializes its kind AND
    its entity (spore-438), so the serialize→validate round-trip a fork performs rebuilds an
    AnnealEntityFiring + SeedPresence on THIS entity — and a rebuild with no entity carried has no
    store at all (there is no process-level entity to fall back to)."""
    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())

    cond = binding.agent.condenser
    forked = type(cond).model_validate(cond.model_dump())  # the round-trip fork/reload performs
    firing = forked._firing
    assert type(firing).__name__ == "AnnealEntityFiring"
    assert firing._resolve_crystal_path() == (ent / ".levain" / "memory.crystal.json").resolve()
    assert Path(forked._presence.entity_dir).resolve() == ent.resolve()
    with pytest.raises(IsolationError, match="no entity given"):
        build_firing(ENTITY_FIRING_KIND)._resolve_crystal_path()


def test_hands_bound_to_another_entity_are_refused(tmp_path):
    """One conversation's floor and memory serve ONE entity: hands built from entity B's binding
    cannot be put on entity A's agent (its condenser would recall A while the floor fenced B)."""
    a = _entity(tmp_path, "entity_a")
    b = _entity(tmp_path, "entity_b")
    with pytest.raises(IsolationError, match="split across two entities"):
        build_entity_agent(a, _stub_llm(), tools=_hands(b))


def test_build_entity_agent_fail_closed_before_build(tmp_path, monkeypatch):
    """The guard fires BEFORE any agent construction — an uninitialized dir raises without ever
    touching the (network-free but real) SDK Agent path."""
    with pytest.raises(IsolationError, match="not an initialized"):
        build_entity_agent(tmp_path / "nope", _stub_llm())


# --- EntityBinding owns the capture + nudge lifecycle (F1/F2 explicit API) -----------


def test_capture_turn_pins_entity_kind(tmp_path, monkeypatch):
    """binding.capture_turn drives vagus_run with an explicit ISOLATED firing on the binding's own
    entity (NOT vagus_run's laptop-defaulting 'anneal' default, F1; spore-438: carried, never
    looked up)."""
    import levain.firing.openhands.capture as capmod
    from levain.firing.anneal import AnnealEntityFiring

    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())
    seen: dict = {}
    monkeypatch.setattr(
        capmod, "vagus_run", lambda conv, firing=None, **kw: seen.update(conv=conv, firing=firing, **kw)
    )
    sentinel_conv = object()
    binding.capture_turn(sentinel_conv, session_id="s1")
    assert seen["conv"] is sentinel_conv
    assert isinstance(seen["firing"], AnnealEntityFiring)
    assert seen["firing"]._resolve_episodic_path().parent == (ent / ".levain").resolve()
    assert seen["session_id"] == "s1"


def test_binding_wrap_nudge_reads_entity_never_flow(tmp_path, monkeypatch):
    """binding.wrap_nudge counts THIS entity's episodes (passing the guarded episodic_path) and never
    opens flow's memory.db (F2). REAL end-to-end: real anneal capture into the entity store."""
    from levain.firing import CaptureRequest
    from levain.firing.anneal import AnnealEntityFiring

    monkeypatch.setenv("HOME", str(tmp_path))  # fresh empty flow-store home
    ent = _entity(tmp_path)
    binding = build_entity_agent(ent, _stub_llm())
    f = AnnealEntityFiring(entity_dir=ent)  # the entity store, carried explicitly
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
