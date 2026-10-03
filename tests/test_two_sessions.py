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
    readable. No code path re-derives a floor from an entity that is gone any more:
      - the hands DESERIALIZE the floor resolved when the binding was created (it travels as data in
        the tool spec), so a raw SDK resume with the persisted agent keeps the deny after the
        entity's deletion. That floor is trusted on content: K1 part 2 must re-create the binding
        from the live entity and never build hands from a persisted or client-supplied agent;
      - re-creating the binding (what opening a session does) refuses a deleted entity."""
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


def test_a_sessions_binding_cannot_change_after_open(tmp_path, home):
    """r2-3 (codex L3, REPRODUCED on d65d63e: a rebind raced the tool build). That needed a write path
    onto a live binding. On the session path there is none:
      - the binding is a frozen value and no bind function exists;
      - the hands are built and their floor read back INSIDE open, so THIS session's hands are
        never rebuilt from the spec params — mutating them afterwards (L1's run against the lazy
        build, 2026-10-02) moves nothing here. A later fork() DOES build its hands from the spec as
        it stands then (codex L3 r1), so in-process code that edits it is trusted host code — the
        same boundary as passing fork() any agent it likes.
    Not claimed here: a raw-SDK resume of entity A's persisted conversation under entity B's agent
    is accepted by the SDK (it checks tool names only; L1, run). No Levain path resumes; K1 part 2
    must derive a conversation's persistence from its entity."""
    import dataclasses

    import levain.firing.openhands.tools as T
    from levain.firing.binding import ConversationBinding

    ent = _entity(tmp_path, "ent")
    s = _open(ent, "unattended")
    try:
        spec = s.conversation.agent.tools[0]
        bound = ConversationBinding.from_params(spec.params["binding"])
        with pytest.raises(dataclasses.FrozenInstanceError):
            bound.mode = "interactive"  # type: ignore[misc]
        assert not [n for n in dir(T) if "bind_conversation" in n or n == "conversation_binding"]
        loose = ConversationBinding.create(ent, mode="interactive", workspace=ent / "workspace")
        spec.params["binding"] = loose.to_params()            # the lazy-build attack, after open
        gh = home / ".config" / "gh" / "hosts.yml"
        assert TOKEN not in _reads(s.conversation, gh)          # the unattended floor holds
        assert s.conversation.agent.tools_map["file_editor"].executor._policy == bound.floor
    finally:
        s.close()


def test_concurrent_sessions_for_two_entities_under_gc_pressure_each_get_their_own_floor(
        tmp_path, home):
    """The c6a6b6e test whose injected race window never ran, replaced by a RUN. r2-1 + r3-1 (a GC
    finalizer re-entering a module lock; two module locks taken in opposite orders) are closed by
    deletion — there are no module locks — and this run could not reproduce r3-1 on c6a6b6e either
    (25 rounds, 2026-10-02), so it smoke-tests isolation under pressure, nothing more:
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
                          tools["terminal"].executor._floor is tools["file_editor"].executor._floor
                          if "terminal" in tools else None)
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
        if confinement_supported():                    # bash exists here, so both hands were built
            assert one_floor is True


def test_a_fork_of_a_conversation_that_already_ran_keeps_its_binding(tmp_path, home):
    """L2 2026-10-02 (run): forking an INITIALIZED Levain conversation crashed — the SDK deep-copies
    its events, the system-prompt event holds the built hands, and their floor/shell hold locks
    (`cannot pickle '_thread.lock'`). A real `fork()` after the tools are built must work, and the
    fork's hands must be its own, fenced by the same binding's floor."""
    ent = _entity(tmp_path, "ent")
    s = _open(ent, "unattended")
    try:
        forked = s.conversation.fork()
        try:
            forked._ensure_agent_ready()
            src = s.conversation.agent.tools_map["file_editor"].executor
            dst = forked.agent.tools_map["file_editor"].executor
            assert dst._floor is not src._floor               # its own floor object…
            assert dst._policy == src._policy                 # …fenced by the same binding
            gh = home / ".config" / "gh" / "hosts.yml"
            assert TOKEN not in _reads(forked, gh)            # unattended floor survives the fork
        finally:
            forked.close()
    finally:
        s.close()


def test_open_creates_nothing_in_an_entity_whose_store_escapes(tmp_path, home):
    """L3 r1 (complement) moved the workspace fence ahead of the binding; the STORE guard must still
    come first. An entity whose `.levain` is a symlink out of its tree passes the cheap pre-flight
    (the marker is readable through the link) and is refused by the store guard — which must happen
    before `<entity>/workspace` is created. (A dir with no `.levain` at all never gets this far.)"""
    from levain.session import SessionStartError

    outside = tmp_path / "outside-store"
    outside.mkdir()
    (outside / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    ent = tmp_path / "ent"
    ent.mkdir()
    (ent / ".levain").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SessionStartError, match="escapes the entity root"):
        _open(ent, "interactive")
    assert not (ent / "workspace").exists()


@pytest.fixture
def editor_tmp(tmp_path, monkeypatch):
    """A private tempdir for the stock editor's history dirs, so a count cannot be disturbed by
    another process using the shared one (complement L3 r2)."""
    import tempfile

    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def _history_dirs(d: Path) -> list[Path]:
    return sorted(d.glob("oh_editor_history_*"))


def test_a_session_leaks_no_editor_history_dir(tmp_path, home, editor_tmp):
    """L3 r1 (codex): the stock file editor makes a history tempdir when its executor is CONSTRUCTED
    and never removes it; with the hands built inside open, every session would leak two (ours, and
    the stock tool's discarded one). Open + close must leave none behind."""
    s = _open(_entity(tmp_path, "ent"), "interactive")
    assert _history_dirs(editor_tmp), "the probe must see the dir it counts (none was made)"
    s.close()
    assert _history_dirs(editor_tmp) == []


def test_a_failed_tool_build_leaks_no_editor_history_dir(tmp_path, home, editor_tmp, monkeypatch):
    """L3 r2 (complement, glm, codex): the floored executor was constructed before steps that can
    raise, so a tool build that failed after it left its history dir behind. Reproduced by making the
    stock tool's create raise; the failed open must leave no dir."""
    from openhands.tools.file_editor import FileEditorTool

    from levain.session import SessionStartError

    def boom(*a, **k):
        raise RuntimeError("stock tool build failed")

    monkeypatch.setattr(FileEditorTool, "create", classmethod(boom))
    with pytest.raises(SessionStartError):
        _open(_entity(tmp_path, "ent"), "interactive")
    assert _history_dirs(editor_tmp) == []


@pytest.mark.skipif(not confinement_supported(), reason="needs an OS sandbox for the bash hand")
def test_a_failed_bash_build_leaks_no_editor_history_dir(tmp_path, home, editor_tmp, monkeypatch):
    """L3 r2: the editor is built first, so a bash hand that failed to build stranded the editor's
    executor. The failed open must leave no history dir."""
    from levain.firing.openhands.tools import LevainBashTool
    from levain.session import SessionStartError

    def boom(*a, **k):
        raise RuntimeError("bash hand build failed")

    monkeypatch.setattr(LevainBashTool, "create", classmethod(boom))
    with pytest.raises(SessionStartError):
        _open(_entity(tmp_path, "ent"), "interactive")
    assert _history_dirs(editor_tmp) == []


def test_an_unknown_mode_is_refused_before_the_workspace_is_created(tmp_path, home):
    """L3 r2 (codex): `open(mode="typo")` created `<entity>/workspace` and only then had the binding
    refuse the mode. A refused open must create nothing."""
    from levain.session import SessionStartError

    ent = _entity(tmp_path, "ent")
    with pytest.raises(SessionStartError, match="drive mode"):
        EntitySession.open(ent, mode="typo", with_tools=True)  # type: ignore[arg-type]
    assert not (ent / "workspace").exists()


def test_a_workspace_swapped_after_the_fence_is_refused(tmp_path, home, monkeypatch):
    """L3 r2 (codex): the last workspace fence ran before the binding resolved the workspace, so a
    symlink swapped in between gave the floor one directory and the conversation another. Reproduced
    by swapping `<entity>/workspace` for a link out of the tree inside the binding's create."""
    from levain.firing import binding as binding_mod
    from levain.session import SessionStartError

    ent = _entity(tmp_path, "ent")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real_create = binding_mod.ConversationBinding.create.__func__

    def swapping_create(cls, entity_dir, **kw):
        ws = Path(kw["workspace"])
        ws.rmdir()
        ws.symlink_to(outside, target_is_directory=True)
        return real_create(cls, entity_dir, **kw)

    monkeypatch.setattr(binding_mod.ConversationBinding, "create", classmethod(swapping_create))
    with pytest.raises(SessionStartError, match="escapes the entity dir"):
        _open(ent, "interactive")


def test_a_failure_after_the_hands_build_leaks_no_editor_history_dir(
    tmp_path, home, editor_tmp, monkeypatch
):
    """L1 (2026-10-02, RAN): when the SDK's agent init fails AFTER levain_hands built (a default
    tool's create raising), the built tools never reach the agent, `conversation.close()` closes
    nothing, and the editor's history dir was left behind. Once the failed open's references are
    gone, no dir may remain."""
    import gc

    from openhands.sdk.agent import base as agent_base

    from levain.session import SessionStartError

    class _Raising:
        @classmethod
        def create(cls, *a, **k):
            raise RuntimeError("a default tool failed to build")

    monkeypatch.setattr(
        agent_base, "BUILT_IN_TOOL_CLASSES",
        {name: _Raising for name in agent_base.BUILT_IN_TOOL_CLASSES},
    )
    try:
        _open(_entity(tmp_path, "ent"), "interactive")
    except SessionStartError:
        pass
    else:
        pytest.fail("open must refuse when a default tool cannot be built")
    gc.collect()
    assert _history_dirs(editor_tmp) == []


def test_a_no_tools_session_also_gets_the_resolved_workspace(tmp_path, home):
    """L3 r2 round 2 (codex): with no tools there is no binding, and the conversation got the
    unresolved spelling of an in-tree symlinked workspace, so retargeting the link later moved it."""
    ent = _entity(tmp_path, "ent")
    (ent / "real-ws").mkdir()
    (ent / "workspace").symlink_to(ent / "real-ws", target_is_directory=True)
    s = EntitySession.open(ent, mode="interactive", with_tools=False)
    try:
        assert Path(s.conversation.workspace.working_dir) == (ent / "real-ws").resolve()
        assert s.workspace == (ent / "real-ws").resolve()
    finally:
        s.close()


def test_the_conversation_gets_the_workspace_the_floor_fences(tmp_path, home):
    """L3 r2 (codex) + L1 (mutation): floor and conversation must name ONE workspace. An in-tree
    symlinked workspace passes every fence, the floor resolves it, and the conversation used to get
    the unresolved spelling."""
    ent = _entity(tmp_path, "ent")
    (ent / "real-ws").mkdir()
    (ent / "workspace").symlink_to(ent / "real-ws", target_is_directory=True)
    s = _open(ent, "interactive")
    try:
        floor_ws = s.conversation.agent.tools_map["file_editor"].executor._policy.workspace
        assert floor_ws == (ent / "real-ws").resolve()
        assert Path(s.conversation.workspace.working_dir) == floor_ws
        assert s.workspace == floor_ws
    finally:
        s.close()
