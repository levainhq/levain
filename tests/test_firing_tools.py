"""The CONFINED executor-tool bundle (spore-277 step 6) — the entity's fenced file hands.

The moat: the entity's file authority is structurally confined to its own ``<entity>/workspace/``.
These prove the confined file editor REFUSES every path that resolves out of the workspace — on
EVERY command, ``view`` included (a view of the flow store is an isolation leak, not just an unsafe
write) — and that the refusal never touches the filesystem, while an in-tree op works normally.

openhands-gated (the tool wraps the real ``FileEditorExecutor``); skipped cleanly without the extra.
The pure ``tool_action_summary`` render helper is tested in ``test_run.py`` (no extra needed).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")

from openhands.sdk.tool import Tool  # noqa: E402
from openhands.tools.file_editor import FileEditorTool  # noqa: E402
from openhands.tools.file_editor.definition import FileEditorAction  # noqa: E402

from levain.firing.openhands.tools import (  # noqa: E402
    LEVAIN_FILE_EDITOR_TOOL,
    LevainFileEditorTool,
    WorkspaceConfinedFileEditorExecutor,
    build_entity_tools,
)


# --- fakes: the minimal conv_state surface FileEditorTool.create touches ---------------------


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


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "entity" / "workspace"
    ws.mkdir(parents=True)
    return ws


# --- the bundle + registration ---------------------------------------------------------------


def test_build_entity_tools_is_the_confined_file_editor_spec():
    tools = build_entity_tools()
    assert [t.name for t in tools] == [LEVAIN_FILE_EDITOR_TOOL]
    assert all(isinstance(t, Tool) for t in tools)


def test_registry_key_distinct_but_llm_name_is_familiar():
    # Two different identifiers: the REGISTRY key ("levain_file_editor") is distinct from the stock
    # tool name ("file_editor"), so an entity's spec can never resolve the unconfined stock tool —
    # while the LLM-visible tool .name stays "file_editor" (familiar → better weak-model tool use).
    assert LEVAIN_FILE_EDITOR_TOOL == "levain_file_editor"
    assert LevainFileEditorTool.name == "file_editor"
    assert LEVAIN_FILE_EDITOR_TOOL != LevainFileEditorTool.name


def test_resolved_tool_keeps_familiar_name_and_confined_executor(tmp_path: Path):
    ws = _ws(tmp_path)
    tools = LevainFileEditorTool.create(_FakeConvState(ws))
    assert len(tools) == 1
    tool = tools[0]
    # A REAL LevainFileEditorTool instance (so OUR declared_resources override runs — codex L3), with
    # .name == "file_editor" (the LLM sees the familiar name; the SDK keys tools_map on tool.name) and
    # OUR executor fenced to the workspace.
    assert isinstance(tool, LevainFileEditorTool)
    assert tool.name == "file_editor"
    assert isinstance(tool.executor, WorkspaceConfinedFileEditorExecutor)
    assert tool.executor._confine_root == str(ws)


def test_declared_resources_never_raises_and_fences(tmp_path: Path):
    # codex L3 (non-replaceable): the PRE-executor declared_resources surface must also be fenced. The
    # stock version does Path(action.path).resolve() → RAISES on a malformed path (NUL) in the
    # ParallelToolExecutor's try, surfacing a raw AgentErrorEvent and SKIPPING the executor's clean
    # refusal. Our override NEVER raises: it declines the lock (declared=True, empty keys) on any
    # rejection/malformed path (so the executor still runs + refuses) and returns a real file lock key
    # for a legit in-workspace path.
    from openhands.tools.file_editor.definition import FileEditorAction

    ws = _ws(tmp_path)
    tool = LevainFileEditorTool.create(_FakeConvState(ws))[0]

    dr_nul = tool.declared_resources(FileEditorAction(command="view", path="in\x00jected"))
    assert dr_nul.declared and tuple(dr_nul.keys) == ()

    dr_escape = tool.declared_resources(FileEditorAction(command="view", path="/etc/passwd"))
    assert dr_escape.declared and tuple(dr_escape.keys) == ()

    dr_ok = tool.declared_resources(
        FileEditorAction(command="create", path=str(ws / "ok.txt"), file_text="x")
    )
    assert dr_ok.declared and len(tuple(dr_ok.keys)) == 1 and "ok.txt" in dr_ok.keys[0]


# --- the fence: the confined executor refuses every out-of-tree path -------------------------


def _exec(ws: Path) -> WorkspaceConfinedFileEditorExecutor:
    return WorkspaceConfinedFileEditorExecutor(workspace_root=str(ws))


def test_in_tree_create_and_view_work(tmp_path: Path):
    ws = _ws(tmp_path)
    ex = _exec(ws)
    created = ex(FileEditorAction(command="create", path=str(ws / "plan.md"), file_text="hello"))
    assert not created.is_error
    assert (ws / "plan.md").read_text() == "hello"
    viewed = ex(FileEditorAction(command="view", path=str(ws / "plan.md")))
    assert not viewed.is_error
    assert "hello" in "".join(c.text for c in viewed.to_llm_content if getattr(c, "text", None))


def test_create_outside_workspace_refused_and_no_file_written(tmp_path: Path):
    ws = _ws(tmp_path)
    target = tmp_path / "escape.txt"
    obs = _exec(ws)(FileEditorAction(command="create", path=str(target), file_text="leak"))
    assert obs.is_error
    assert "REFUSED" in obs.text
    assert not target.exists()  # the fence fired BEFORE the write — filesystem untouched


def test_view_outside_workspace_refused(tmp_path: Path):
    # A read (view) of an out-of-tree file is an isolation leak → refused (unlike the SDK's own
    # allowed_edits_files, which exempts view).
    ws = _ws(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("operator secret")
    obs = _exec(ws)(FileEditorAction(command="view", path=str(secret)))
    assert obs.is_error
    assert "REFUSED" in obs.text
    assert "operator secret" not in obs.text  # never read


def test_view_into_flow_store_refused(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    store = fake_home / ".anneal-memory"
    store.mkdir(parents=True)
    (store / "memory.db").write_text("flow identity")
    monkeypatch.setenv("HOME", str(fake_home))
    ws = _ws(tmp_path)
    obs = _exec(ws)(FileEditorAction(command="view", path=str(store / "memory.db")))
    assert obs.is_error
    assert "flow store" in obs.text
    assert "flow identity" not in obs.text


def test_dotdot_escape_refused(tmp_path: Path):
    ws = _ws(tmp_path)
    obs = _exec(ws)(
        FileEditorAction(command="create", path=str(ws / ".." / "sneak.txt"), file_text="x")
    )
    assert obs.is_error
    assert not (ws.parent / "sneak.txt").exists()


def test_symlink_escape_refused(tmp_path: Path):
    # A symlink planted in the workspace pointing out is caught by its RESOLVED target.
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("leak")
    (ws / "link").symlink_to(outside)
    obs = _exec(ws)(FileEditorAction(command="view", path=str(ws / "link" / "secret.txt")))
    assert obs.is_error
    assert "leak" not in obs.text


def test_declared_resources_fix_reaches_executor_at_runtime(tmp_path: Path):
    """codex L3 round-2 regression guard: route a malformed (NUL) path through the SDK's REAL
    ParallelToolExecutor — the runtime path the direct-executor tests bypass. With the STOCK tool,
    declared_resources RAISES → an AgentErrorEvent and the confined executor's refusal never runs.
    With our LevainFileEditorTool, declared_resources declines the lock WITHOUT raising → the executor
    RUNS and returns the clean refusal. Locks the two-phase authority boundary (declared_resources +
    __call__), so an SDK bump that reintroduces the pre-executor raise fails loud."""
    from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
    from openhands.sdk.event.llm_convertible.action import ActionEvent
    from openhands.sdk.llm import TextContent
    from openhands.sdk.llm.message import MessageToolCall

    ws = _ws(tmp_path)
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

    # STOCK declared_resources raises on the NUL → AgentErrorEvent, executor never runs.
    stock = FileEditorTool.create(cs)[0].set_executor(
        WorkspaceConfinedFileEditorExecutor(workspace_root=str(ws))
    )
    stock_ran, stock_ev = _run_batch(stock)
    assert stock_ran is False
    assert type(stock_ev).__name__ == "AgentErrorEvent"

    # OUR declared_resources declines the lock (no raise) → executor RUNS + returns the refusal.
    confined_ran, confined_obs = _run_batch(LevainFileEditorTool.create(cs)[0])
    assert confined_ran is True
    assert getattr(confined_obs, "is_error", False) and "REFUSED" in confined_obs.text


def test_malformed_nul_path_refused_not_crashed(tmp_path: Path):
    # A NUL-byte path (LLM-emittable) must return a refusal Observation, NOT raise past the executor
    # and crash the turn/REPL (apparatus L2 finding #4). The fence converts the ValueError to a
    # fail-closed IsolationError, which the executor renders as an error Observation.
    ws = _ws(tmp_path)
    obs = _exec(ws)(FileEditorAction(command="view", path=str(ws / "in\x00jected")))
    assert obs.is_error
    assert "REFUSED" in obs.text
