"""spore-438: two ``EntitySession``s in ONE process — the K1-part-2 prerequisite.

Each test is the reproduction that ran against 62e16b2 (before the conversation binding), turned
into an assertion; on that tree all three FAILED as described in their docstrings.

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
from levain.firing.isolation import AMBIGUOUS_ENTITY, LEVAIN_ENTITY_DIR_ENV  # noqa: E402
from levain.session import EntitySession  # noqa: E402

TOKEN = "PROBE-TOKEN-438"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch HOME holding a planted gh credential, and a clean process channel for both
    bindings (restored afterwards, so the sticky ambiguous marker cannot leak into later tests)."""
    h = tmp_path / "home"
    gh = h / ".config" / "gh" / "hosts.yml"
    gh.parent.mkdir(parents=True)
    gh.write_text(f"github.com:\n  oauth_token: {TOKEN}\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)
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
    both open, every session-path reader resolves its own entity, and the ambient channel is marked
    ambiguous so nothing un-pinned can pick either."""
    import levain.firing.openhands.capture as capmod
    from levain.firing.openhands.tools import floor_for_conv_state

    x, y = _entity(tmp_path, "entX"), _entity(tmp_path, "entY")
    a = _open(x, "interactive")
    b = _open(y, "interactive")
    try:
        assert os.environ[LEVAIN_ENTITY_DIR_ENV] == AMBIGUOUS_ENTITY
        os.environ[LEVAIN_ENTITY_DIR_ENV] = str(y.resolve())   # even pointed squarely at B…
        for s, ent in ((a, x), (b, y)):
            s.conversation._ensure_agent_ready()
            store = (ent / ".levain").resolve()
            assert floor_for_conv_state(s.conversation.state).policy.entity_dir == ent.resolve()
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


def _bare_conversation(ent: Path, **kw):
    """An entity agent + a real Conversation, without EntitySession, so a test can drive the binding
    and the lazy tool build itself."""
    from openhands.sdk import LLM, Conversation

    from levain.firing.openhands.entity import build_entity_agent
    from levain.firing.openhands.tools import build_entity_tools

    b = build_entity_agent(ent, LLM(usage_id="t", model="openai/x", api_key="x"),
                           tools=build_entity_tools(with_bash=False), publish_entity=False)
    return b.agent, Conversation(b.agent, workspace=str(ent / "workspace"), visualizer=None, **kw)


def test_a_resumed_conversation_of_a_deleted_entity_is_refused_not_rebuilt_without_its_denies(
        tmp_path, home):
    """d65d63e (codex L3 r2 HIGH, reproduced): the persisted binding was dropped whenever this process
    held no mode for the state, so a resumed conversation whose entity had been deleted got a floor
    derived from <workspace>/.. with NO confinement.json — the operator's deny_files readable. The
    persisted entity is now kept, so the floor build sees it is gone and refuses."""
    import shutil

    from levain.firing.confinement import crown_jewel_reason
    from levain.firing.openhands.tools import (
        ConversationBindingError,
        bind_conversation,
        policy_for_conv_state,
    )

    secret = home / "secret-token.txt"
    secret.write_text("S3CRET")
    ent = _entity(tmp_path, "ent")
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"deny_files": [str(secret)]}))
    pdir = tmp_path / "persist"
    agent, c = _bare_conversation(ent, persistence_dir=str(pdir))
    bind_conversation(c, entity_dir=ent, mode="interactive")
    assert crown_jewel_reason(policy_for_conv_state(c.state), secret) is not None
    cid = c.id
    c.close()

    shutil.rmtree(ent / ".levain")          # the entity is deleted between runs
    from openhands.sdk import Conversation

    resumed = Conversation(agent.model_copy(), workspace=str(ent / "workspace"),
                           persistence_dir=str(pdir), conversation_id=cid, visualizer=None)
    try:
        with pytest.raises(ConversationBindingError, match="not an initialized entity"):
            policy_for_conv_state(resumed.state)
    finally:
        resumed.close()


def test_a_bound_conversation_cannot_be_rebound_so_no_rebind_can_race_its_tool_build(
        tmp_path, home, monkeypatch):
    """d65d63e (codex L3 r2 HIGH, reproduced): a rebind passed its "tools unbuilt" check, the first
    turn built the tools on the OLD (interactive) floor inside that window, and the rebind then
    published "unattended" — a conversation whose binding said unattended while its tools read gh.
    The binding is now fixed for the life of the state, so the rebind is refused before any window;
    an identical rebind is a no-op."""
    import levain.firing.openhands.tools as T
    from openhands.tools.file_editor.definition import FileEditorAction

    ent = _entity(tmp_path, "ent")
    _, c = _bare_conversation(ent)
    T.bind_conversation(c, entity_dir=ent, mode="interactive")
    T.bind_conversation(c, entity_dir=ent, mode="interactive")       # identical: no-op

    real = T._require_agent_agrees
    fired: list = []

    def window(agent, ed):              # the first turn lands INSIDE the rebind, after its check
        real(agent, ed)
        if not fired:
            fired.append(1)
            c._ensure_agent_ready()

    monkeypatch.setattr(T, "_require_agent_agrees", window)
    try:
        with pytest.raises(T.ConversationBindingError, match="already bound"):
            T.bind_conversation(c, entity_dir=ent, mode="unattended")
        monkeypatch.setattr(T, "_require_agent_agrees", real)
        c._ensure_agent_ready()
        gh = home / ".config" / "gh" / "hosts.yml"
        reads = TOKEN in str(c.agent.tools_map["file_editor"].executor(
            FileEditorAction(command="view", path=str(gh))))
        assert T.conversation_binding(c.state)[1] == "interactive" and reads   # binding == floor
    finally:
        c.close()
