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
    # uid=501(phillipclapham) gid=20(staff) groups=20(staff),12(everyone),61(localaccounts),79(_appserverusr),80(admin),81(_appserveradm),701(com.apple.sharepoint.group.1),33(_appstore),98(_lpadmin),100(_lpoperator),204(_developer),250(_analyticsusers),395(com.apple.access_ftp),398(com.apple.access_screensharing),399(com.apple.access_ssh),400(com.apple.access_remote_ae) mirrors the real ConversationState (a required UUID) — the floor registry keys on the
    # CONVERSATION, so a fake without one would silently fall back to object identity and hide
    # whether create() actually shares.
    _n = 0

    def __init__(self, wd: Path, conv_id: str | None = None) -> None:
        self.workspace = _FakeWorkspace(wd)
        self.agent = _FakeAgent()
        if conv_id is None:
            type(self)._n += 1
            conv_id = f"fake-conv-{type(self)._n}"
        self.id = conv_id


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
    """Both executors read THROUGH the floor, so a merge made by one is seen by the other."""
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

    newly = (tmp_path / "late.sock").resolve()
    floor.absorb(dataclasses.replace(floor.policy, deny_write_files=(newly,)))

    assert newly in editor._policy.deny_write_files, "the file-editor hand did not see the merge"
    assert editor._policy is bash._policy, "the hands diverged"


def test_the_floor_merge_is_monotonic_under_concurrent_spawners(tmp_path) -> None:
    """⛔ codex L3 MED, EXECUTION-REPRODUCED. The floor used to be ASSIGNED wholesale from one
    executor's snapshot under THAT EXECUTOR'S OWN lock — which serializes nothing, because the object
    being mutated is shared. Two spawners each read policy P, spawn against P+A and P+B, and the
    later assignment DISCARDED the earlier: *"a previously denied live container socket becomes
    reachable again"* after the losing executor respawned.

    `absorb` unions under the FLOOR'S lock, so the result is order-independent and no deny is ever
    lost. Mutation-checked: replacing `absorb`'s body with `self._policy = spawned` fails this."""
    from levain.firing.openhands.tools import _SharedFloor
    from levain.firing.confinement import build_policy
    import dataclasses
    import threading

    ent = tmp_path / "ent"
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    floor = _SharedFloor(build_policy(ent, workspace=ws))
    base = floor.policy

    a = (tmp_path / "a.sock").resolve()
    b = (tmp_path / "b.sock").resolve()
    pa = dataclasses.replace(base, deny_sockets=base.deny_sockets + (a,))
    pb = dataclasses.replace(base, deny_sockets=base.deny_sockets + (b,))

    ts = [threading.Thread(target=floor.absorb, args=(p,)) for p in (pa, pb) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert a in floor.policy.deny_sockets, "spawner A's deny was lost"
    assert b in floor.policy.deny_sockets, "spawner B's deny was lost"


def test_two_conversations_never_share_a_floor(tmp_path) -> None:
    """⛔ THE DESIGN CHANGE, and it dissolves TWO codex findings at once (L3, 2026-09-04).

    The registry used to key on `(entity_dir, workspace)`, which made sharing and freshness the same
    variable pulling opposite ways: share the object and a later conversation inherits a STALE cred
    floor (fail-open); rebuild it on a changed baseline and the two hands of ONE conversation get
    DIFFERENT floors (`view /secret` through the editor while bash denies it). Each was fixed one
    review round apart, each breaking the other.

    A CONVERSATION HAS EXACTLY ONE BASELINE BY CONSTRUCTION — so keying on `ConversationState.id`
    removes both failure modes rather than balancing them, and the baseline comparison is DELETED."""
    from levain.firing.openhands.tools import _FLOORS, floor_for_conv_state

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()

    c1, c2 = _FakeConvState(ws), _FakeConvState(ws)   # same entity+workspace, different conversations
    f1, f2 = floor_for_conv_state(c1), floor_for_conv_state(c2)
    keep = (f1, f2)  # hold refs: the registry is weak-valued
    assert f1 is not f2, "a second conversation inherited the first's floor — the stale-floor fail-open"

    # ...and the SAME conversation still gets the SAME floor, or the two hands diverge.
    assert floor_for_conv_state(c1) is f1
    assert len(keep) == 2


def test_an_executor_needs_exactly_one_of_policy_or_floor(tmp_path) -> None:
    """⛔ codex L3 LOW. Accepting both and silently preferring `floor` is FAIL-OPEN: a strict policy
    beside an accidentally permissive floor yielded the permissive one with no error."""
    from levain.firing.openhands.tools import CrownJewelsFileEditorExecutor, _SharedFloor
    from levain.firing.confinement import build_policy

    ent, ws = _entity(tmp_path)
    pol = build_policy(ent, workspace=ws)
    with pytest.raises(TypeError, match="EXACTLY ONE"):
        CrownJewelsFileEditorExecutor(policy=pol, floor=_SharedFloor(pol))
    with pytest.raises(TypeError, match="EXACTLY ONE"):
        CrownJewelsFileEditorExecutor()


def test_a_provider_returning_a_non_shell_is_refused_at_the_source(tmp_path, monkeypatch) -> None:
    """⛔ codex L3 MED: my earlier `None` guard only MOVED the crash. `_ensure_shell` returned None
    and its caller raised `AttributeError` on `.run` one line later — and `__call__` converts
    `ConfinementError` into an in-band refusal, NOT `AttributeError`, so it stayed unhandled.
    ⚡ A guard that relocates an unhandled crash is worse than none, because the code then READS as
    handled. The CONTRACT was the defect: `spawn_shell` declares a non-optional return, so anything
    else is a provider bug and is refused fail-closed at the source."""
    from levain.firing.openhands.tools import SandboxedBashExecutor
    from levain.firing.confinement import ConfinementError, build_policy
    import levain.firing.openhands.tools as _t

    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))

    class _NullProvider:
        def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0):
            return None

        def spawn_shell(self, policy, *, env=None, default_timeout=120.0):
            from levain.firing.confinement import ConfinementProvider
            return ConfinementProvider.spawn_shell(self, policy, env=env,
                                                   default_timeout=default_timeout)

        def render_profile(self, policy):
            return ""

    monkeypatch.setattr(_t, "select_provider", lambda: _NullProvider())
    with pytest.raises(ConfinementError, match="not a SandboxedShell"):
        ex._ensure_shell()


def test_a_resumed_conversation_never_inherits_the_live_floor(tmp_path) -> None:
    """⛔ codex L3 HIGH round 5, and it FALSIFIED THE PREMISE OF ROUND 5's DESIGN.

    Keying on `ConversationState.id` rested on "a conversation has exactly one baseline by
    construction". That is FALSE: `ConversationState.create()` is documented as *"Create a new
    conversation state OR RESUME FROM PERSISTENCE"*, and a resume may run under a different drive
    mode and even a different workspace. Start `U` interactively (creds ALLOWED), keep the old agent
    alive so its entry stays live, resume `U` unattended → the resumed agent skips
    `policy_for_conv_state()` and inherits the permissive floor. **The identical fail-open as the
    entity key, through a new door.**

    ⚡ A UUID names the LOGICAL conversation; a runtime baseline belongs to the RUNTIME. Two objects
    carrying the SAME id are two runs and must not share."""
    from levain.firing.openhands.tools import _FLOORS, floor_for_conv_state

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()

    first = _FakeConvState(ws, conv_id="U")
    resumed = _FakeConvState(ws, conv_id="U")   # SAME persisted id, a DIFFERENT runtime object
    f1 = floor_for_conv_state(first)
    f2 = floor_for_conv_state(resumed)
    keep = (f1, f2, first, resumed)

    assert f1 is not f2, (
        "the resumed conversation inherited the live floor — a run that should be MORE restricted "
        "silently keeps the earlier run's permissive floor"
    )
    assert floor_for_conv_state(first) is f1, "the same runtime object must still share one floor"
    assert len(keep) == 4


def test_a_dead_conversations_floor_is_evicted_so_a_recycled_id_cannot_alias_it(tmp_path) -> None:
    """The `id()` key's own hazard, closed by `weakref.finalize`: the entry must disappear when the
    ConversationState is collected, BEFORE CPython can hand that `id()` to a different object.
    Without the finalizer this key would be strictly worse than the UUID it replaced."""
    import gc
    from levain.firing.openhands.tools import _FLOORS, floor_for_conv_state

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()

    cs = _FakeConvState(ws)
    floor = floor_for_conv_state(cs)
    key = id(cs)
    assert key in _FLOORS
    del cs, floor
    gc.collect()
    assert key not in _FLOORS, "a dead conversation's floor was left behind for a recycled id()"


def test_an_unverified_shell_is_never_cached_fail_once_open_next(tmp_path, monkeypatch) -> None:
    """⛔ codex L3 HIGH round 5. `_ensure_shell` committed the shell to `self._shell` BEFORE
    validating `effective_policy`. The first command was refused while the LIVE shell stayed cached;
    the next command saw a non-closed `_shell`, skipped the branch, and executed through the
    unverified shell. **Fail-once/open-next** — worse than no check, because it emits exactly one
    refusal that reads as the guard working and then stops guarding.

    Assert the SECOND call refuses too, and that the rejected shell was closed rather than leaked."""
    from levain.firing.openhands.tools import SandboxedBashExecutor
    from levain.firing.confinement import ConfinementError, SandboxedShell, build_policy
    import levain.firing.openhands.tools as _t

    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))
    made: list[SandboxedShell] = []

    class _UnstampedProvider:
        def spawn_shell(self, policy, *, env=None, default_timeout=120.0):
            sh = SandboxedShell(argv=["/bin/true"], cwd=ws, env={})
            sh.effective_policy = None          # a live shell with no stamp
            made.append(sh)
            return sh

    monkeypatch.setattr(_t, "select_provider", lambda: _UnstampedProvider())

    for attempt in (1, 2):
        with pytest.raises(ConfinementError, match="no effective policy"):
            ex._ensure_shell()
        assert ex._shell is None, f"attempt {attempt}: an unverified shell was cached"
    assert all(sh.closed for sh in made), "a rejected live shell was leaked instead of closed"


def test_the_floor_registry_lock_survives_a_finalizer_reentering_on_the_same_thread(tmp_path) -> None:
    """⛔ complement L3 HIGH round 6. `_drop_floor` is a `weakref.finalize` callback and takes
    `_FLOORS_LOCK`. A finalizer for a cyclically-collected object fires SYNCHRONOUSLY on whatever
    thread crossed the GC threshold — and that trigger can be any allocation, including the ones
    inside `policy_for_conv_state()` which run WHILE THE LOCK IS HELD. With a plain `threading.Lock`
    the thread blocks acquiring a lock it already owns, forever, still holding it: every
    `floor_for_conv_state` in the process freezes.

    ⚡ The failure is SILENT — no exception, no log. A hang with no error is the worst shape a
    security-path defect can take, because every surface reads healthy.

    Drive the reentry directly rather than trying to provoke the GC: call `_drop_floor` from inside
    a held `_FLOORS_LOCK` on this thread, which is exactly what the finalizer does."""
    import levain.firing.openhands.tools as _t

    with _t._FLOORS_LOCK:
        # Under a non-reentrant Lock this blocks forever and the test hangs rather than fails.
        _t._drop_floor(-12345)          # a key that is not present; the acquire is the point
    assert True


def test_a_failing_close_does_not_mask_the_fail_closed_reason(tmp_path, monkeypatch) -> None:
    """⛔ complement L3 MED round 6. If `candidate.close()` raises while tearing down an unverified
    shell, that I/O error would propagate INSTEAD of the ConfinementError — so a caller matching on
    exception type, or a log printing only the outermost exception, sees "close failed" rather than
    "no effective policy". The leak is prevented either way; what was degraded is the FIDELITY OF
    THE FAIL-CLOSED SIGNAL, which is the entire point of that rewrite."""
    from levain.firing.openhands.tools import SandboxedBashExecutor
    from levain.firing.confinement import ConfinementError, SandboxedShell, build_policy
    import levain.firing.openhands.tools as _t

    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))

    class _ExplodingShell(SandboxedShell):
        def close(self) -> None:
            raise OSError("teardown blew up")

    class _BadProvider:
        def spawn_shell(self, policy, *, env=None, default_timeout=120.0):
            sh = _ExplodingShell(argv=["/bin/true"], cwd=ws, env={})
            sh.effective_policy = None
            return sh

    monkeypatch.setattr(_t, "select_provider", lambda: _BadProvider())
    with pytest.raises(ConfinementError, match="no effective policy"):
        ex._ensure_shell()          # NOT OSError


def test_a_recycled_id_cannot_inherit_a_stale_floor_even_if_eviction_failed(tmp_path) -> None:
    """⛔ codex L3 MED round 7. An `id()` is unique only among LIVE objects. The registry now stores
    `(weakref, floor)` and verifies `ref() is conv_state` on lookup, so a stale entry — finalizer
    failed to install, or failed to run — cannot hand a NEW conversation the OLD floor.
    ⚡ The point is that the fail-open no longer depends on a callback firing. Simulate the worst
    case directly: leave an entry whose weakref is dead and confirm it is not handed out."""
    import weakref as _wr
    from levain.firing.openhands.tools import _FLOORS, _SharedFloor, floor_for_conv_state
    from levain.firing.confinement import build_policy

    ent, ws = _entity(tmp_path)
    _FLOORS.clear()

    victim = _FakeConvState(ws)
    key = id(victim)
    stale_floor = _SharedFloor(build_policy(tmp_path / "OTHER", workspace=ws))
    dead = _FakeConvState(ws)
    ref = _wr.ref(dead)
    del dead
    import gc
    gc.collect()
    _FLOORS[key] = (ref, stale_floor)          # an entry whose object is gone, at victim's id

    got = floor_for_conv_state(victim)
    assert got is not stale_floor, "a recycled id inherited a dead conversation's floor"
    assert got.policy.entity_dir != stale_floor.policy.entity_dir


def test_a_rejected_shell_is_torn_down_even_when_its_own_close_raises(tmp_path, monkeypatch) -> None:
    """⛔ codex L3 MED round 7, and the second half is the one that matters. An OVERRIDDEN `close()`
    raising before doing any cleanup left the subprocess, FIFO and descriptors alive — held by the
    shell's own reader thread — after the only application reference was dropped; repeated refusals
    could exhaust resources. And `KeyboardInterrupt`/`SystemExit` from `close()` replaced the
    original refusal entirely.

    Now: try the object's own `close()`, fall back to the BASE-CLASS primitive a subclass cannot
    have replaced, and let nothing propagate. The refusal survives either way."""
    from levain.firing.openhands.tools import SandboxedBashExecutor
    from levain.firing.confinement import ConfinementError, SandboxedShell, build_policy
    import levain.firing.openhands.tools as _t

    ent, ws = _entity(tmp_path)
    ex = SandboxedBashExecutor(build_policy(ent, workspace=ws))
    base_closed: list[bool] = []

    class _HostileShell(SandboxedShell):
        def close(self) -> None:               # raises BEFORE any cleanup
            raise KeyboardInterrupt("teardown interrupted")

    real_close = SandboxedShell.close

    def _tracking_close(self):
        base_closed.append(True)
        return real_close(self)

    monkeypatch.setattr(SandboxedShell, "close", _tracking_close)

    class _BadProvider:
        def spawn_shell(self, policy, *, env=None, default_timeout=120.0):
            sh = _HostileShell(argv=["/bin/true"], cwd=ws, env={})
            sh.effective_policy = None
            return sh

    monkeypatch.setattr(_t, "select_provider", lambda: _BadProvider())
    with pytest.raises(ConfinementError, match="no effective policy"):
        ex._ensure_shell()                     # NOT KeyboardInterrupt
    assert base_closed, "the base-class teardown never ran after the overridden close() raised"
    assert ex._shell is None
