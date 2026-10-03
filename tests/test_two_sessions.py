"""spore-438: two ``EntitySession``s in ONE process — the K1-part-2 prerequisite.

The first three tests are the reproductions that ran against 62e16b2 (before any conversation
binding), turned into assertions; on that tree all three FAILED as described in their docstrings.
The rest pin the review-found classes of the earlier registry design (seat/1002-spore438) as
unrepresentable in this one.

openhands-gated. Real sessions, real ``Conversation`` objects, and the real lazy tool build (the
SDK's ``_ensure_agent_ready``, which the first turn calls) — no model is contacted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("openhands.sdk", reason="openhands extra absent")
pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")

from levain.firing.confinement import confinement_supported  # noqa: E402
from levain.firing.binding import LEVAIN_ENTITY_PROCESS_ENV  # noqa: E402
from levain.session import EntitySession  # noqa: E402

TOKEN = "PROBE-TOKEN-438"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch HOME holding a planted gh credential. The retired process channels are cleared in
    case an outer shell exports them; the entity-process latch is reset by conftest."""
    h = tmp_path / "home"
    gh = h / ".config" / "gh" / "hosts.yml"
    gh.parent.mkdir(parents=True)
    gh.write_text(f"github.com:\n  oauth_token: {TOKEN}\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("LEVAIN_ENTITY_DIR", raising=False)
    monkeypatch.delenv("LEVAIN_DRIVE_MODE", raising=False)   # retired channel; stale shells only
    return h


def _entity(root: Path, name: str) -> Path:
    """A minimal clean openhands entity: the adapter marker is all `require_openhands_entity` needs."""
    ent = root / name
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    return ent


def _open(path: Path, mode: str) -> EntitySession:
    return EntitySession.open(path, mode=mode, with_tools=True)


def _gh_reads(session: EntitySession, home: Path) -> dict[str, bool]:
    """Did each hand of THIS session's floor read the planted credential? Builds the tools first,
    exactly as the first turn would."""
    from openhands.tools.file_editor.definition import FileEditorAction

    session.conversation._ensure_agent_ready()
    gh = home / ".config" / "gh" / "hosts.yml"
    tools = session.conversation.agent.tools_map
    out = {"file_editor": TOKEN in str(tools["file_editor"].executor(
        FileEditorAction(command="view", path=str(gh))))}
    if "terminal" in tools:
        from openhands.tools.terminal.definition import TerminalAction

        out["bash"] = TOKEN in str(tools["terminal"].executor(TerminalAction(command=f"cat {gh}")))
    return out


def test_one_sessions_drive_mode_does_not_reach_anothers_floor(tmp_path, home):
    """62e16b2: A (interactive) opened, then B (unattended) on the same entity. A's banner said the
    standard creds were allowed while BOTH of A's hands refused them — B's bind had moved the
    process channel both enforcers read. Now each hand resolves its own conversation's mode."""
    ent = _entity(tmp_path, "ent")
    a = _open(ent, "interactive")
    b = _open(ent, "unattended")
    try:
        assert a.deny_standard_creds is False and b.deny_standard_creds is True
        reads_a, reads_b = _gh_reads(a, home), _gh_reads(b, home)
        assert all(reads_a.values()), reads_a          # A's floor matches A's banner
        assert not any(reads_b.values()), reads_b      # B stays denied
        if confinement_supported():
            assert "bash" in reads_a and "bash" in reads_b   # both enforcers were actually probed
    finally:
        a.close()
        b.close()


def test_two_entities_in_one_process_each_resolve_their_own_store(tmp_path, home):
    """62e16b2: the second entity was REFUSED, and once the process channel named it (what serving
    it requires) the first session's recall, re-anchor and capture all moved into its store. Now
    both open, and every reader resolves the entity its own objects carry: there is no process-level
    entity to look up (the latch only says "an entity lives here")."""
    import levain.firing.openhands.capture as capmod

    x, y = _entity(tmp_path, "entX"), _entity(tmp_path, "entY")
    a = _open(x, "interactive")
    b = _open(y, "interactive")
    try:
        assert os.environ[LEVAIN_ENTITY_PROCESS_ENV] == "1"
        os.environ["LEVAIN_ENTITY_DIR"] = str(y.resolve())   # the retired channel, pointed at B…
        for s, ent in ((a, x), (b, y)):
            s.conversation._ensure_agent_ready()
            store = (ent / ".levain").resolve()
            tools = s.conversation.agent.tools_map
            assert tools["file_editor"].executor._policy.entity_dir == ent.resolve()      # floor
            cond = s.conversation.agent.condenser
            assert cond._firing._resolve_episodic_path().parent == store          # recall
            assert Path(cond._presence.entity_dir).resolve() == ent.resolve()     # re-anchor
            seen = {}
            real = capmod.vagus_run
            capmod.vagus_run = lambda conv, firing=None, **kw: seen.update(f=firing)
            try:
                s.binding.capture_turn(object())
            finally:
                capmod.vagus_run = real
            assert seen["f"]._resolve_episodic_path().parent == store              # capture
    finally:
        a.close()
        b.close()


def test_an_interactive_session_opens_after_an_unattended_one_with_its_own_floor(tmp_path, home):
    """62e16b2: once an unattended session had opened, the process REFUSED every interactive one
    (bind_drive_mode's widen guard, with an operator message pointing at --model). The guard was
    retired with the env channel (Phill, 2026-10-02: "b on levain") because no floor reads it.
    Now the interactive session opens and each floor is its own — the reverse order of the first
    test, the one the guard used to refuse."""
    ent = _entity(tmp_path, "ent")
    u = _open(ent, "unattended")
    i = _open(ent, "interactive")
    try:
        assert not any(_gh_reads(u, home).values())
        assert all(_gh_reads(i, home).values())
        assert "LEVAIN_DRIVE_MODE" not in os.environ   # nothing publishes the retired channel
    finally:
        u.close()
        i.close()


def _bare_conversation(ent: Path, *, mode: str = "interactive", **kw):
    """An entity agent + a real Conversation, built the way the session builds them (one binding),
    without EntitySession, so a test can drive the lazy tool build itself."""
    from openhands.sdk import LLM, Conversation

    from levain.firing.binding import ConversationBinding
    from levain.firing.openhands.entity import build_entity_agent
    from levain.firing.openhands.tools import build_entity_tools

    binding = ConversationBinding.create(ent, mode=mode, workspace=ent / "workspace")  # type: ignore[arg-type]
    b = build_entity_agent(ent, LLM(usage_id="t", model="openai/x", api_key="x"),
                           tools=build_entity_tools(binding, with_bash=False))
    return b.agent, Conversation(b.agent, workspace=str(ent / "workspace"), visualizer=None, **kw)


def _reads(conversation, path: Path) -> str:
    from openhands.tools.file_editor.definition import FileEditorAction

    conversation._ensure_agent_ready()
    return str(conversation.agent.tools_map["file_editor"].executor(
        FileEditorAction(command="view", path=str(path))))


def test_a_resumed_conversation_of_a_deleted_entity_is_refused_not_rebuilt_without_its_denies(
        tmp_path, home):
    """r2-2 (codex L3, REPRODUCED on d65d63e): a resumed conversation whose entity had been deleted
    got a floor REBUILT from <workspace>/.. with no confinement.json — the operator's deny_files
    readable. No code path rebuilds a floor from a record any more:
      - a raw SDK resume with the persisted agent keeps the floor that was RESOLVED at creation (it
        travels as data in the tool spec), so the deny survives the entity's deletion;
      - Levain's resume path re-creates the binding, and creation refuses a deleted entity."""
    import shutil

    from openhands.sdk import Conversation

    secret = home / "secret-token.txt"
    secret.write_text("S3CRET")
    ent = _entity(tmp_path, "ent")
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"deny_files": [str(secret)]}))
    pdir = tmp_path / "persist"
    agent, c = _bare_conversation(ent, persistence_dir=str(pdir))
    assert "S3CRET" not in _reads(c, secret)
    cid = c.id
    c.close()

    shutil.rmtree(ent / ".levain")          # the entity is deleted between runs
    resumed = Conversation(agent.model_copy(), workspace=str(ent / "workspace"),
                           persistence_dir=str(pdir), conversation_id=cid, visualizer=None)
    try:
        out = _reads(resumed, secret)
        assert "S3CRET" not in out and "REFUSED" in out
    finally:
        resumed.close()
    from levain.session import SessionStartError

    with pytest.raises(SessionStartError, match="not an initialized Levain entity"):
        _open(ent, "interactive")


def test_a_conversations_binding_cannot_change_after_creation(tmp_path, home):
    """r2-3 (codex L3, REPRODUCED on d65d63e: a rebind raced the tool build) and r3-2 (a resumed
    state re-bound to another entity). Both needed a write path onto a live binding. There is none:
    the binding is a frozen value inside a frozen agent, and no bind function exists."""
    import dataclasses

    import levain.firing.openhands.tools as T
    from levain.firing.binding import ConversationBinding

    ent = _entity(tmp_path, "ent")
    agent, c = _bare_conversation(ent)
    try:
        spec = agent.tools[0]
        bound = ConversationBinding.from_params(spec.params["binding"])
        with pytest.raises(dataclasses.FrozenInstanceError):
            bound.mode = "unattended"  # type: ignore[misc]
        with pytest.raises(Exception, match="frozen"):
            agent.tools = []  # type: ignore[misc]
        assert not [n for n in dir(T) if "bind_conversation" in n or n == "conversation_binding"]
    finally:
        c.close()


def test_concurrent_sessions_for_two_entities_under_gc_pressure_each_get_their_own_floor(
        tmp_path, home):
    """r2-1 + r3-1 (a GC finalizer re-entering a module lock; two module locks taken in opposite
    orders) and the c6a6b6e test whose injected race window never ran. This is a RUN, not a window:
    threads open sessions for two entities with OPPOSITE modes and build their tools at the same
    moment, with the cyclic GC firing on nearly every allocation. Every thread must finish (a hang
    fails the timeout), and every conversation's hands must read its OWN binding's floor."""
    import gc
    import threading

    from levain.firing.confinement import crown_jewel_reason

    x, y = _entity(tmp_path, "entX"), _entity(tmp_path, "entY")
    plan = [(x, "interactive"), (y, "unattended")] * 4
    gh = home / ".config" / "gh" / "hosts.yml"
    barrier = threading.Barrier(len(plan))
    results: list = [None] * len(plan)
    sessions: list = []

    def worker(i: int, ent: Path, mode: str) -> None:
        try:
            s = _open(ent, mode)
            sessions.append(s)
            barrier.wait(timeout=60)
            s.conversation._ensure_agent_ready()
            tools = s.conversation.agent.tools_map
            pol = tools["file_editor"].executor._policy
            results[i] = (pol.entity_dir, crown_jewel_reason(pol, gh) is not None,
                          tools["file_editor"].executor._floor is (
                              tools["terminal"].executor._floor if "terminal" in tools
                              else tools["file_editor"].executor._floor))
        except BaseException as exc:  # noqa: BLE001 — reported by the assertion below
            results[i] = exc

    old = gc.get_threshold()
    gc.set_threshold(1, 1, 1)
    try:
        threads = [threading.Thread(target=worker, args=(i, e, m), daemon=True)
                   for i, (e, m) in enumerate(plan)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "a session build hung"
    finally:
        gc.set_threshold(*old)
        for s in sessions:
            s.close()
    for (ent, mode), got in zip(plan, results):
        assert not isinstance(got, BaseException), got
        entity_dir, denies_gh, one_floor = got
        assert entity_dir == ent.resolve()
        assert denies_gh is (mode == "unattended")
        assert one_floor
