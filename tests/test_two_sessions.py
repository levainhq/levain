"""spore-438: two ``EntitySession``s in ONE process — the K1-part-2 prerequisite.

Each test is the reproduction that ran against 62e16b2 (before the conversation binding), turned
into an assertion. On that tree the first two FAILED as described in their docstrings; the third
pins the guard the fix deliberately kept.

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
from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV  # noqa: E402
from levain.firing.isolation import AMBIGUOUS_ENTITY, LEVAIN_ENTITY_DIR_ENV  # noqa: E402
from levain.session import EntitySession, SessionStartError  # noqa: E402

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
    monkeypatch.delenv(LEVAIN_DRIVE_MODE_ENV, raising=False)
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


def test_the_widen_refusal_still_fires_and_says_why(tmp_path, home):
    """The guard spore-438 keeps: a process whose recorded mode is unattended refuses to open an
    interactive session (it would widen the recorded floor). The message names the refusal, not
    the generic "check --model / --base-url" advice it fell into before."""
    ent = _entity(tmp_path, "ent")
    a = _open(ent, "unattended")
    try:
        with pytest.raises(SessionStartError) as exc:
            _open(ent, "interactive")
        msg = str(exc.value)
        assert "STRICTER" in msg and "--model" not in msg
    finally:
        a.close()
