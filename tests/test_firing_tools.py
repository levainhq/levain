"""The CONFINED executor-tool bundle (spore-277 step 6 → spore-311 slice 2) — the entity's fenced HANDS.

The moat: BOTH hands are fenced to the shared crown-jewels FLOOR (slice 2 relaxes the step-6
``<entity>/workspace/`` jail). These prove:
  - the floored FILE EDITOR refuses every crown jewel on EVERY command (``view`` included — a view of
    the flow store is an isolation leak), while allowing broad NON-jewel reach (the relaxation);
  - the BASH tool drives spore-311's ``SandboxedShell`` (a real ``sandbox-exec`` sandbox on macOS),
    refuses crown jewels, keeps state across commands, and refuses interactive input;
  - both keep the LLM-FAMILIAR names (``file_editor`` / ``terminal``) while the REGISTRY keys stay
    distinct (``levain_file_editor`` / ``levain_bash``) so the unconfined stock tools are unreachable.

openhands-gated; skipped cleanly without the extra. The real-sandbox bash tests skip off macOS (no OS
confinement floor). The pure ``tool_action_summary`` render helper is tested in ``test_run.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
pytest.importorskip("openhands.tools.terminal", reason="openhands extra absent")

from openhands.sdk.tool import Tool  # noqa: E402
from openhands.tools.file_editor import FileEditorTool  # noqa: E402
from openhands.tools.file_editor.definition import FileEditorAction  # noqa: E402
from openhands.tools.terminal.definition import TerminalAction  # noqa: E402

from levain.firing.confinement import (  # noqa: E402
    ConfinementError,
    build_policy,
    confinement_supported,
    crown_jewel_reason,
)
from levain.firing.isolation import LEVAIN_ENTITY_DIR_ENV  # noqa: E402
from levain.firing.openhands.tools import (  # noqa: E402
    LEVAIN_BASH_TOOL,
    LEVAIN_FILE_EDITOR_TOOL,
    CrownJewelsFileEditorExecutor,
    LevainBashTool,
    LevainFileEditorTool,
    SandboxedBashExecutor,
    build_entity_tools,
    policy_for_conv_state,
)

_needs_sandbox = pytest.mark.skipif(
    not confinement_supported(), reason="no OS confinement floor on this platform"
)


@pytest.fixture(autouse=True)
def _clean_entity_env(monkeypatch):
    # policy_for_conv_state falls back to <workspace>/.. when $LEVAIN_ENTITY_DIR is unset; clear a
    # leaked env from another test so the derived entity dir is deterministic (the fakes put the
    # entity at <workspace>/..).
    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)


# --- fakes: the minimal conv_state surface the tools' .create touches ------------------------


class _FakeLLM:
    def vision_is_active(self) -> bool:  # FileEditorTool.create consults this for the description
        return False


class _FakeAgent:
    llm = _FakeLLM()


class _FakeWorkspace:
    def __init__(self, wd: Path) -> None:
        self.working_dir = str(wd)


class _FakeConvState:
    def __init__(self, wd: Path) -> None:
        self.workspace = _FakeWorkspace(wd)
        self.agent = _FakeAgent()


def _entity(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal initialized entity: ``<tmp>/entity/`` with ``.levain/`` + ``workspace/``."""
    ent = tmp_path / "entity"
    (ent / ".levain").mkdir(parents=True)
    ws = ent / "workspace"
    ws.mkdir()
    return ent, ws


# --- the bundle + registration ---------------------------------------------------------------


def test_build_entity_tools_bundle_is_editor_plus_bash():
    both = build_entity_tools()
    assert [t.name for t in both] == [LEVAIN_FILE_EDITOR_TOOL, LEVAIN_BASH_TOOL]
    assert all(isinstance(t, Tool) for t in both)
    # --with_bash=False (no OS sandbox on the platform) drops bash, keeps the file editor — NEVER an
    # unconfined shell as a fallback.
    editor_only = build_entity_tools(with_bash=False)
    assert [t.name for t in editor_only] == [LEVAIN_FILE_EDITOR_TOOL]


def test_registry_keys_distinct_but_llm_names_are_familiar():
    # Two identifiers per tool: the REGISTRY key (distinct → an entity spec can never resolve the
    # unconfined stock tool) vs the LLM-visible .name (familiar → better weak-model tool use).
    assert LEVAIN_FILE_EDITOR_TOOL == "levain_file_editor"
    assert LEVAIN_BASH_TOOL == "levain_bash"
    assert LevainFileEditorTool.name == "file_editor"
    assert LevainBashTool.name == "terminal"
    assert LEVAIN_FILE_EDITOR_TOOL != LevainFileEditorTool.name
    assert LEVAIN_BASH_TOOL != LevainBashTool.name


def test_policy_for_conv_state_prefers_env_then_workspace_parent(tmp_path: Path, monkeypatch):
    ent, ws = _entity(tmp_path)
    # env UNSET → entity dir derived as <workspace>/..
    pol = policy_for_conv_state(_FakeConvState(ws))
    assert pol.entity_dir == ent.resolve() and pol.workspace == ws.resolve()
    # env SET (as levain run binds it) → that is authoritative
    other = tmp_path / "other"
    (other / ".levain").mkdir(parents=True)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(other))
    pol2 = policy_for_conv_state(_FakeConvState(ws))
    assert pol2.entity_dir == other.resolve()


def test_policy_for_conv_state_threads_deny_standard_creds(tmp_path: Path, monkeypatch):
    """The confinement.json ``deny_standard_creds`` opt-in threads through policy_for_conv_state into
    the built policy (apparatus L1/complement: nothing pinned this end-to-end wiring — a dropped
    pass-through in tools.py would silently lose the opt-in while every other test still passed)."""
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(LEVAIN_DRIVE_MODE_ENV, "interactive")
    ent, ws = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"deny_standard_creds": true}')
    pol = policy_for_conv_state(_FakeConvState(ws))
    assert crown_jewel_reason(pol, tmp_path / ".config" / "gh" / "hosts.yml") is not None
    # a DIFFERENT entity with no confinement.json does NOT fold them in AT AN INTERACTIVE DRIVE
    # (gh hands intact for the operator who is sitting right there)
    ent2 = tmp_path / "e2"
    (ent2 / ".levain").mkdir(parents=True)
    ws2 = ent2 / "workspace"
    ws2.mkdir()
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent2))
    pol2 = policy_for_conv_state(_FakeConvState(ws2))
    assert crown_jewel_reason(pol2, tmp_path / ".config" / "gh" / "hosts.yml") is None


def test_policy_for_conv_state_denies_standard_creds_on_an_UNATTENDED_drive(tmp_path: Path,
                                                                            monkeypatch):
    """K4a: with no declaration, an UNATTENDED seat folds the standard cred stores into the floor
    while an interactive drive does not — same entity, same (absent) config, drive mode the only
    variable. This is the FILE-EDITOR enforcer, which is the one that matters: `view` is afferent,
    so the K3 gate never sees the read."""
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    monkeypatch.setenv("HOME", str(tmp_path))
    ent, ws = _entity(tmp_path)          # no confinement.json at all → the tri-state ABSENT
    gh = tmp_path / ".config" / "gh" / "hosts.yml"

    monkeypatch.setenv(LEVAIN_DRIVE_MODE_ENV, "interactive")
    assert crown_jewel_reason(policy_for_conv_state(_FakeConvState(ws)), gh) is None

    monkeypatch.setenv(LEVAIN_DRIVE_MODE_ENV, "unattended")
    assert crown_jewel_reason(policy_for_conv_state(_FakeConvState(ws)), gh) is not None


def test_policy_for_conv_state_honours_an_EXPLICIT_false_even_unattended(tmp_path: Path,
                                                                         monkeypatch):
    """An explicit ``false`` is an operator OPT-IN and must survive an unattended seat — a seat
    whose job is "open a PR nightly" genuinely needs gh. The unattended default is a DEFAULT, not a
    prohibition, and the tri-state exists precisely so this can be said."""
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(LEVAIN_DRIVE_MODE_ENV, "unattended")
    ent, ws = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text('{"deny_standard_creds": false}')
    assert crown_jewel_reason(
        policy_for_conv_state(_FakeConvState(ws)), tmp_path / ".config" / "gh" / "hosts.yml"
    ) is None


def test_policy_for_conv_state_fails_CLOSED_when_the_drive_mode_is_unbound(tmp_path: Path,
                                                                           monkeypatch):
    """An UNBOUND drive mode must deny, not allow. The only way it is unset in a real run is a
    WIRING failure, and a wiring failure must never WIDEN the floor: wrongly denying surfaces as a
    visible refusal the operator fixes in one line, while wrongly granting is a silent credential
    exposure on an unattended seat with nobody watching."""
    from levain.firing.drive import LEVAIN_DRIVE_MODE_ENV

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(LEVAIN_DRIVE_MODE_ENV, raising=False)
    ent, ws = _entity(tmp_path)
    assert crown_jewel_reason(
        policy_for_conv_state(_FakeConvState(ws)), tmp_path / ".config" / "gh" / "hosts.yml"
    ) is not None


# --- the file-editor hand: create + declared_resources + the floor ---------------------------


def test_file_editor_create_wires_the_floored_executor(tmp_path: Path):
    ent, ws = _entity(tmp_path)
    tools = LevainFileEditorTool.create(_FakeConvState(ws))
    assert len(tools) == 1
    tool = tools[0]
    # A REAL LevainFileEditorTool (so OUR declared_resources override runs — codex L3), .name familiar,
    # OUR floored executor, its policy's workspace == the run workspace.
    assert isinstance(tool, LevainFileEditorTool)
    assert tool.name == "file_editor"
    assert isinstance(tool.executor, CrownJewelsFileEditorExecutor)
    assert tool.executor._policy.workspace == ws.resolve()


def test_file_editor_declared_resources_never_raises_and_fences(tmp_path: Path, monkeypatch):
    # codex L3 (non-replaceable): the PRE-executor declared_resources surface must ALSO be fenced. The
    # stock version does Path(action.path).resolve() → RAISES on a malformed path (NUL) inside the
    # ParallelToolExecutor's try, so the executor's clean refusal never runs. Ours NEVER raises: it
    # declines the lock (declared=True, empty keys) on a crown jewel OR malformed path, and returns a
    # real file lock for a legit non-jewel path.
    fake_home = tmp_path / "home"
    (fake_home / ".anneal-memory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    ent, ws = _entity(tmp_path)
    tool = LevainFileEditorTool.create(_FakeConvState(ws))[0]

    dr_nul = tool.declared_resources(FileEditorAction(command="view", path="in\x00jected"))
    assert dr_nul.declared and tuple(dr_nul.keys) == ()

    dr_jewel = tool.declared_resources(
        FileEditorAction(command="view", path=str(fake_home / ".anneal-memory" / "memory.db"))
    )
    assert dr_jewel.declared and tuple(dr_jewel.keys) == ()

    dr_ok = tool.declared_resources(
        FileEditorAction(command="create", path=str(tmp_path / "repo" / "ok.txt"), file_text="x")
    )
    assert dr_ok.declared and len(tuple(dr_ok.keys)) == 1 and "ok.txt" in dr_ok.keys[0]


def _floored(ws: Path, ent: Path, *, deny_files: tuple[Path, ...] = ()) -> CrownJewelsFileEditorExecutor:
    return CrownJewelsFileEditorExecutor(
        policy=build_policy(ent, workspace=ws, deny_files=deny_files)
    )


def _viewed_text(obs) -> str:
    return "".join(c.text for c in obs.to_llm_content if getattr(c, "text", None))


def test_in_tree_create_and_view_work(tmp_path: Path):
    ent, ws = _entity(tmp_path)
    ex = _floored(ws, ent)
    created = ex(FileEditorAction(command="create", path=str(ws / "plan.md"), file_text="hello"))
    assert not created.is_error
    assert (ws / "plan.md").read_text() == "hello"
    viewed = ex(FileEditorAction(command="view", path=str(ws / "plan.md")))
    assert not viewed.is_error and "hello" in _viewed_text(viewed)


def test_broad_reach_outside_the_old_workspace_jail_is_now_allowed(tmp_path: Path):
    # THE RELAXATION (slice 2): a non-jewel path OUTSIDE <entity>/workspace/ is now allowed — the file
    # editor works on the operator's real repos like bash, not jailed to workspace/.
    ent, ws = _entity(tmp_path)
    repo = tmp_path / "realrepo"
    repo.mkdir()
    obs = _floored(ws, ent)(
        FileEditorAction(command="create", path=str(repo / "mod.py"), file_text="x = 1")
    )
    assert not obs.is_error
    assert (repo / "mod.py").read_text() == "x = 1"


def test_declared_credential_file_refused_and_not_leaked(tmp_path: Path):
    ent, ws = _entity(tmp_path)
    secret = tmp_path / "app.env"
    secret.write_text("SECRET=xyz")
    obs = _floored(ws, ent, deny_files=(secret,))(
        FileEditorAction(command="view", path=str(secret))
    )
    assert obs.is_error and "REFUSED" in obs.text
    assert "SECRET=xyz" not in obs.text  # never read


def test_view_into_flow_store_refused(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    store = fake_home / ".anneal-memory"
    store.mkdir(parents=True)
    (store / "memory.db").write_text("flow identity")
    monkeypatch.setenv("HOME", str(fake_home))
    ent, ws = _entity(tmp_path)
    obs = _floored(ws, ent)(FileEditorAction(command="view", path=str(store / "memory.db")))
    assert obs.is_error and "REFUSED" in obs.text
    assert "flow identity" not in obs.text


def test_view_ssh_key_material_refused(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    ssh = fake_home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    monkeypatch.setenv("HOME", str(fake_home))
    ent, ws = _entity(tmp_path)
    obs = _floored(ws, ent)(FileEditorAction(command="view", path=str(ssh / "id_ed25519")))
    assert obs.is_error and "REFUSED" in obs.text
    assert "PRIVATE KEY" not in obs.text


def test_symlink_into_a_crown_jewel_refused(tmp_path: Path, monkeypatch):
    # A symlink planted in a reachable dir pointing at a crown jewel is caught by its RESOLVED target.
    fake_home = tmp_path / "home"
    store = fake_home / ".anneal-memory"
    store.mkdir(parents=True)
    (store / "memory.db").write_text("flow identity")
    monkeypatch.setenv("HOME", str(fake_home))
    ent, ws = _entity(tmp_path)
    (ws / "link").symlink_to(store)
    obs = _floored(ws, ent)(FileEditorAction(command="view", path=str(ws / "link" / "memory.db")))
    assert obs.is_error and "flow identity" not in obs.text


def test_malformed_nul_path_refused_not_crashed(tmp_path: Path):
    # A NUL-byte path (LLM-emittable) returns a refusal Observation, NOT a raw ValueError crashing the
    # turn (crown_jewel_reason fail-closes an unresolvable path).
    ent, ws = _entity(tmp_path)
    obs = _floored(ws, ent)(FileEditorAction(command="view", path=str(ws / "in\x00jected")))
    assert obs.is_error and "REFUSED" in obs.text


def test_declared_resources_fix_reaches_executor_at_runtime(tmp_path: Path, monkeypatch):
    """codex L3 round-2 regression guard: route a malformed (NUL) path through the SDK's REAL
    ParallelToolExecutor. With the STOCK tool, declared_resources RAISES → an AgentErrorEvent and the
    floored executor's refusal never runs. With our LevainFileEditorTool, declared_resources declines
    the lock WITHOUT raising → the executor RUNS and returns the clean refusal."""
    from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
    from openhands.sdk.event.llm_convertible.action import ActionEvent
    from openhands.sdk.llm import TextContent
    from openhands.sdk.llm.message import MessageToolCall

    ent, ws = _entity(tmp_path)
    cs = _FakeConvState(ws)

    def _run_batch(tool):
        ran = {"called": False}

        def tool_runner(ae):
            ran["called"] = True
            return [tool.executor(ae.action)]

        ae = ActionEvent(
            action=FileEditorAction(command="view", path="in\x00jected"),
            tool_name="file_editor",
            tool_call_id="c1",
            tool_call=MessageToolCall(
                id="c1", name="file_editor", arguments="{}", origin="completion"
            ),
            thought=[TextContent(text="t")],
            llm_response_id="r1",
        )
        result = ParallelToolExecutor().execute_batch(
            [ae], tool_runner, tools={"file_editor": tool}
        )
        return ran["called"], result[0][0]

    stock = FileEditorTool.create(cs)[0].set_executor(
        CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws))
    )
    stock_ran, stock_ev = _run_batch(stock)
    assert stock_ran is False
    assert type(stock_ev).__name__ == "AgentErrorEvent"

    confined_ran, confined_obs = _run_batch(LevainFileEditorTool.create(cs)[0])
    assert confined_ran is True
    assert getattr(confined_obs, "is_error", False) and "REFUSED" in confined_obs.text


# --- the bash hand: create, contract, and the real sandbox -----------------------------------


def test_bash_create_wires_the_sandboxed_executor(tmp_path: Path):
    ent, ws = _entity(tmp_path)
    tools = LevainBashTool.create(_FakeConvState(ws))
    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, LevainBashTool)
    assert tool.name == "terminal"  # familiar LLM name
    assert isinstance(tool.executor, SandboxedBashExecutor)


def test_bash_declared_resources_always_serializes(tmp_path: Path):
    # The SandboxedShell is single-caller → bash calls MUST serialize (unconditionally, unlike the
    # stock TerminalTool which opts out under a tmux pool).
    ent, ws = _entity(tmp_path)
    tool = LevainBashTool.create(_FakeConvState(ws))[0]
    dr = tool.declared_resources(TerminalAction(command="echo hi"))
    assert dr.declared and tuple(dr.keys) == ("terminal:session",)


def test_bash_is_input_is_refused(tmp_path: Path):
    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))
    obs = ex(TerminalAction(command="", is_input=True))
    assert obs.is_error and "interactive input" in obs.text
    # a refusal is NOT a soft-timeout: exit_code 126, not -1 (which the SDK renders "still running").
    assert obs.exit_code == 126


def test_bash_reset_and_is_input_together_raises(tmp_path: Path):
    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))
    with pytest.raises(ValueError):
        ex(TerminalAction(command="", reset=True, is_input=True))


def test_bash_confinement_unavailable_is_an_in_band_refusal(tmp_path: Path, monkeypatch):
    # No OS floor here → the FIRST command returns a clean in-band refusal, never a crash (fail-closed:
    # no sandbox → no bash, never an unconfined shell).
    def _no_provider(*_a, **_k):
        raise ConfinementError("no confinement on this platform")

    monkeypatch.setattr("levain.firing.openhands.tools.select_provider", _no_provider)
    ent, ws = _entity(tmp_path)
    obs = SandboxedBashExecutor(build_policy(ent, workspace=ws))(TerminalAction(command="echo hi"))
    assert obs.is_error and "REFUSED" in obs.text and "fail-closed" in obs.text
    assert obs.exit_code == 126


@_needs_sandbox
def test_bash_runs_keeps_state_and_refuses_crown_jewels(tmp_path: Path, monkeypatch):
    # The real sandbox (macOS): a legit command executes, state persists across commands, and a crown
    # jewel read is REFUSED + never leaked. Uses a FAKE home so the deny targets a fake store (the
    # real-store byte-unchanged proof is the separate L4-live gate).
    fake_home = tmp_path / "home"
    store = fake_home / ".anneal-memory"
    store.mkdir(parents=True)
    (store / "memory.db").write_text("FLOW-IDENTITY-SECRET")
    monkeypatch.setenv("HOME", str(fake_home))
    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))
    try:
        ok = ex(TerminalAction(command="echo alive"))
        assert ok.exit_code == 0 and "alive" in ok.text and not ok.is_error
        # state persists (one long-lived shell, not per-command exec)
        ex(TerminalAction(command="export FOO=persisted"))
        got = ex(TerminalAction(command="echo $FOO"))
        assert got.text.strip() == "persisted"
        # the crown jewel is refused by the OS sandbox + never leaked
        jewel = ex(TerminalAction(command=f"cat {store / 'memory.db'}"))
        assert "FLOW-IDENTITY-SECRET" not in jewel.text
        assert jewel.is_error or "not permitted" in jewel.text.lower()
    finally:
        ex.close()


@_needs_sandbox
def test_bash_respawns_a_fresh_shell_after_exit(tmp_path: Path):
    # `exit` ends the shell (EOF); the next command must transparently respawn a fresh confined shell
    # rather than hand a dead channel.
    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))
    try:
        # the exit itself is surfaced (is_error + a note) so a silent empty result / a swallowed
        # non-zero `exit N` isn't misread as success (apparatus L1 finding 2)
        exited = ex(TerminalAction(command="exit 7"))
        assert exited.is_error and "shell exited" in exited.text
        after = ex(TerminalAction(command="echo respawned"))
        assert after.exit_code == 0 and "respawned" in after.text
    finally:
        ex.close()


def test_file_editor_refuses_case_variant_of_a_crown_jewel(tmp_path: Path, monkeypatch):
    # apparatus L2 HIGH: the floored file editor (NOT under the sandbox) must refuse a case-variant of
    # a crown jewel, matching what the seatbelt kernel denies on the case-insensitive volume.
    fake_home = tmp_path / "home"
    store = fake_home / ".anneal-memory"
    store.mkdir(parents=True)
    (store / "memory.db").write_text("flow identity")
    monkeypatch.setenv("HOME", str(fake_home))
    ent, ws = _entity(tmp_path)
    variant = str(fake_home) + "/.Anneal-Memory/memory.db"
    obs = _floored(ws, ent)(FileEditorAction(command="view", path=variant))
    assert obs.is_error and "REFUSED" in obs.text
    assert "flow identity" not in obs.text


# --- spore-768 / glm L3: the two hands must not diverge as the floor evolves -----------------

def test_both_hands_share_one_evolving_floor(tmp_path) -> None:
    """⛔ glm-5.2 L3, 2026-09-04, CONFIRMED against disk. Each hand's `create` built its OWN policy
    object. That was harmless while the spawn-time socket refresh touched only `deny_sockets` (the
    connect arm has no in-process twin) — and the fix for the round-1 HIGH made the refresh also
    update `deny_write_files`/`socket_spellings`/`deny_write_dirs`, which the file editor DOES
    enforce. So the bash hand started evolving a floor the file-editor hand never saw, falsifying
    this module's own stated invariant in two separate docstrings.

    Pin it at the level that matters: a policy update made through the shared floor is visible to
    BOTH executors, because they read through it rather than caching a copy."""
    from levain.firing.openhands.tools import (
        CrownJewelsFileEditorExecutor,
        SandboxedBashExecutor,
        _SharedFloor,
    )
    from levain.firing.confinement import build_policy
    import dataclasses

    ent = tmp_path / "ent"
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    floor = _SharedFloor(build_policy(ent, workspace=ws))

    editor = CrownJewelsFileEditorExecutor(floor=floor)
    bash = SandboxedBashExecutor(floor=floor)
    assert editor._policy is bash._policy

    # Simulate what a spawn does: widen the floor's socket arms.
    newly = (tmp_path / "late.sock").resolve()
    floor.policy = dataclasses.replace(
        floor.policy,
        deny_sockets=floor.policy.deny_sockets + (newly,),
        deny_write_files=floor.policy.deny_write_files + (newly,),
        socket_spellings=floor.policy.socket_spellings + (newly,),
    )

    assert newly in editor._policy.deny_write_files, "the file-editor hand did not see the update"
    assert editor._policy is bash._policy, "the hands diverged"


def test_create_wires_BOTH_hands_to_the_same_floor_object(tmp_path, monkeypatch) -> None:
    """⛔ THE WIRING TEST, AND IT EXISTS BECAUSE MY FIRST TEST FOR THIS PASSED BOTH WAYS.

    `test_both_hands_share_one_evolving_floor` constructs the two executors with an explicit shared
    floor, so it grades the HOLDER MECHANISM and never touches `create` — where the sharing actually
    has to happen. Reverting `create` to build separate policies left that test green. A test that
    passes under the mutation it was written for is the exact class this whole change is about, and
    it caught me one layer up from where I was looking.

    This one drives the real `create` path for both hands and asserts they reached the SAME object."""
    from levain.firing.openhands.tools import _FLOORS

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()  # module-level registry: isolate this test from any earlier one

    editor_tool = LevainFileEditorTool.create(_FakeConvState(ws))[0]
    bash_tool = LevainBashTool.create(_FakeConvState(ws))[0]

    assert editor_tool.executor._floor is bash_tool.executor._floor, (
        "create() built the two hands separate floors — they will diverge as the socket arms "
        "evolve at each spawn"
    )


def test_a_policy_only_construction_still_works_and_is_private(tmp_path) -> None:
    """The `policy=` form stays supported for tests and direct construction, and gets its OWN floor
    — so two independently-constructed executors do NOT accidentally share state through the
    module-level registry. Only `create` (via `floor_for_conv_state`) shares."""
    from levain.firing.openhands.tools import CrownJewelsFileEditorExecutor
    from levain.firing.confinement import build_policy

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    a = CrownJewelsFileEditorExecutor(policy=build_policy(tmp_path / "a", workspace=ws))
    b = CrownJewelsFileEditorExecutor(policy=build_policy(tmp_path / "b", workspace=ws))
    assert a._floor is not b._floor


def test_a_changed_drive_mode_rebuilds_the_floor_instead_of_inheriting_it(tmp_path, monkeypatch) -> None:
    """⛔ THE FAIL-OPEN (complement HIGH + glm-5.2 HIGH, convergent, 2026-09-04).

    `_FLOORS` cached on (entity_dir, workspace) with no eviction and DISCARDED the freshly-built
    policy on a hit. `policy_for_conv_state` derives `deny_standard_creds` from the DRIVE MODE, so
    in a long-running daemon — which K4a ships — an interactive REPL conversation (creds allowed)
    would create the floor and a later UNATTENDED seat against the same entity would inherit it,
    leaving ~/.config/gh, ~/.aws/credentials and ~/.netrc readable on the unattended seat. That is
    precisely the fail-open the drive-mode tri-state exists to prevent.

    Comparing the BASELINE is what makes the fix correct: the live policy legitimately grows at each
    spawn (the monotonic socket union) and carrying extra denies forward is fail-CLOSED, so only a
    changed baseline means a different floor."""
    from levain.firing.openhands.tools import _FLOORS, floor_for_conv_state
    from levain.firing import drive

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()

    monkeypatch.setattr(drive, "current_drive_mode", lambda: "interactive")
    import levain.firing.openhands.tools as _t
    monkeypatch.setattr(_t, "current_drive_mode", lambda: "interactive")
    first = floor_for_conv_state(_FakeConvState(ws))

    monkeypatch.setattr(_t, "current_drive_mode", lambda: "unattended")
    second = floor_for_conv_state(_FakeConvState(ws))

    assert second is not first, (
        "the unattended conversation inherited the interactive conversation's floor — the cred "
        "floor is stale in the PERMISSIVE direction"
    )
    assert second.policy.deny_files != first.policy.deny_files or \
        second.policy.deny_read_write != first.policy.deny_read_write


def test_an_unchanged_baseline_still_shares_and_keeps_its_accumulation(tmp_path) -> None:
    """The other half of the same rule: an identical baseline MUST keep sharing, or the two hands
    stop converging and round 2's glm finding comes straight back. And the spawn-time accumulation
    on the live policy must survive — it is fail-closed and re-deriving it would drop denies."""
    from levain.firing.openhands.tools import _FLOORS, floor_for_conv_state
    import dataclasses

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()

    a = floor_for_conv_state(_FakeConvState(ws))
    late = (tmp_path / "late.sock").resolve()
    a.policy = dataclasses.replace(a.policy, deny_sockets=a.policy.deny_sockets + (late,))

    b = floor_for_conv_state(_FakeConvState(ws))
    assert b is a, "an unchanged baseline must keep sharing one floor"
    assert late in b.policy.deny_sockets, "the spawn-time accumulation was dropped"


def test_ensure_shell_does_not_crash_when_a_provider_returns_no_shell(tmp_path, monkeypatch) -> None:
    """⛔ complement HIGH + glm MED. `spawn_shell` guards `if shell is not None` with a comment
    saying `_spawn_shell_impl` may return a falsy sentinel — so its own return can be None, and
    `_ensure_shell` dereferenced it one line later. `__call__` converts ConfinementError into an
    in-band refusal, NOT AttributeError, so it was an unhandled crash.

    ⚡ Anticipating a case in one function and not at its only call site is worse than not
    anticipating it: the existing guard reads as though the case is handled."""
    from levain.firing.openhands.tools import SandboxedBashExecutor
    from levain.firing.confinement import build_policy
    import levain.firing.openhands.tools as _t

    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))

    class _NullProvider:
        def spawn_shell(self, policy, *, env=None, default_timeout=120.0):
            return None

    monkeypatch.setattr(_t, "select_provider", lambda: _NullProvider())
    assert ex._ensure_shell() is None  # must not raise AttributeError
