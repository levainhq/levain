"""``_drive`` runs the conversation ONCE per step, and capture never runs it again.

Capture used to re-run the conversation: ``_drive`` called ``run()``, then
``binding.capture_turn`` -> ``vagus_run`` called ``run()`` a second time before capturing. A first
run that ended in the SDK's ERROR state (the per-run iteration cap, or a model failure) was reset
to RUNNING with a fresh budget by that second run, and a turn that hit the gate inside the second
run was reported ``gated=False`` with the held action listed as work that ran.

These tests use the REAL ``EntityBinding.capture_turn`` (only its store is a stub), because the
binding doubles in the other suites do not run the conversation and so cannot show the defect.
"""
from __future__ import annotations

import pytest

pytest.importorskip("openhands.sdk", reason="openhands extra not installed")
pytest.importorskip("openhands.tools.file_editor", reason="openhands extra not installed")

from openhands.sdk import ConversationExecutionStatus, Message, MessageEvent, TextContent  # noqa: E402
from openhands.sdk.event.conversation_error import ConversationErrorEvent  # noqa: E402
from openhands.sdk.event.llm_convertible import ActionEvent  # noqa: E402
from openhands.sdk.llm import MessageToolCall  # noqa: E402
from openhands.tools.file_editor.definition import FileEditorAction  # noqa: E402

from levain.firing import StubFiring  # noqa: E402
from levain.firing.openhands.entity import EntityBinding  # noqa: E402


def _user(text: str) -> MessageEvent:
    return MessageEvent(source="user", llm_message=Message(role="user", content=[TextContent(text=text)]))


def _reply(text: str) -> MessageEvent:
    return MessageEvent(source="agent", llm_message=Message(role="assistant", content=[TextContent(text=text)]))


def _held_create(path: str) -> ActionEvent:
    action = FileEditorAction(command="create", path=path, file_text="late")
    return ActionEvent(
        thought=[TextContent(text="...")],
        action=action,
        tool_name="file_editor",
        tool_call_id="call-late",
        tool_call=MessageToolCall(
            id="call-late", name="file_editor",
            arguments='{"command": "create", "path": "%s", "file_text": "late"}' % path,
            origin="completion",
        ),
        llm_response_id="resp-late",
    )


class _State:
    def __init__(self):
        self.events: list = []
        self.agent_state: dict = {}
        self.execution_status = ConversationExecutionStatus.IDLE
        self.security_analyzer = None
        self.confirmation_policy = None


class _ScriptedConversation:
    """Each ``run()`` takes the next scripted outcome, as the SDK would leave the state:

    - ``"error"``: status ERROR plus a ``ConversationErrorEvent`` (the per-run iteration cap);
    - ``"halt"``: status WAITING_FOR_CONFIRMATION with a proposed, unexecuted ``create``;
    - ``"finish"``: status FINISHED with an agent reply.

    A run past the script on a FINISHED turn takes no step, as the SDK's does with no stop hook; any
    other run past the script is a test failure in itself: nothing should drive one."""

    def __init__(self, script: list[str], workspace: str = "/tmp/ws"):
        self._script = list(script)
        self.state = _State()
        self.id = "conv-x3"
        self.runs = 0
        self.workspace = workspace

    def send_message(self, message):
        self.state.events.append(_user(message))

    def run(self):
        self.runs += 1
        if not self._script and self.state.execution_status == ConversationExecutionStatus.FINISHED:
            return  # as the SDK does with no stop hook: a run on a finished turn takes no step
        outcome = self._script.pop(0) if self._script else "unscripted"
        if outcome == "error":
            self.state.execution_status = ConversationExecutionStatus.ERROR
            self.state.events.append(ConversationErrorEvent(
                source="environment", code="MaxIterationsReached",
                detail="Agent reached maximum iterations limit (2).",
            ))
        elif outcome == "halt":
            self.state.execution_status = ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
            self.state.events.append(_held_create(f"{self.workspace}/late.txt"))
        elif outcome == "finish":
            self.state.execution_status = ConversationExecutionStatus.FINISHED
            self.state.events.append(_reply("done"))
        elif outcome == "plan":  # a zero-tool turn that opens with a plan: the driver nudges once
            self.state.execution_status = ConversationExecutionStatus.FINISHED
            self.state.events.append(_reply("I'll run the tests now."))
        else:
            raise AssertionError("run() was called past the scripted turn")

    def close(self):
        pass


class _RealCaptureBinding:
    """The real ``EntityBinding.capture_turn``; the store under it is a ``StubFiring``."""

    capture_turn = EntityBinding.capture_turn

    def __init__(self, tmp_path):
        self.entity_dir = tmp_path
        self.episodic_path = tmp_path / "episodic.db"
        self.crystal_path = tmp_path / "crystal.json"

    def wrap_nudge(self, **kwargs):
        return None


@pytest.fixture
def store(monkeypatch):
    stub = StubFiring()
    import levain.firing.anneal as anneal_mod

    monkeypatch.setattr(anneal_mod, "AnnealEntityFiring", lambda entity_dir: stub)
    return stub


def _session(tmp_path, conv, *, gate_mode):
    from levain.session import EntitySession

    return EntitySession(
        entity_dir=tmp_path,
        binding=_RealCaptureBinding(tmp_path),
        conversation=conv,
        workspace=tmp_path / "workspace",
        model_label="openrouter/z-ai/glm-5.2",
        with_tools=True,
        bash_ok=True,
        gate_mode=gate_mode,
    )


@pytest.mark.parametrize("gate_mode", ["ungated", "gated"])
def test_a_run_that_ends_in_ERROR_is_reported_and_never_run_again(tmp_path, store, gate_mode):
    """check7: the iteration cap ends the run in ERROR. One run, an error on the result, no capture."""
    conv = _ScriptedConversation(["error", "finish"])
    sess = _session(tmp_path, conv, gate_mode=gate_mode)

    result = sess.run_turn("create six files")

    assert conv.runs == 1, "a second run() resets ERROR to RUNNING with a fresh iteration budget"
    assert result.error is not None
    assert "MaxIterationsReached" in result.error
    assert store.captured == [], "a turn that ended in ERROR did not complete and is not captured"


def test_a_run_that_ends_STUCK_is_reported_and_never_run_again(tmp_path, store):
    conv = _ScriptedConversation(["finish"])

    def stuck_run():
        conv.runs += 1
        conv.state.execution_status = ConversationExecutionStatus.STUCK

    conv.run = stuck_run
    sess = _session(tmp_path, conv, gate_mode="ungated")

    result = sess.run_turn("loop")

    assert conv.runs == 1
    assert result.error is not None
    assert store.captured == []


@pytest.mark.parametrize("gate_mode", ["ungated", "gated"])
def test_a_post_nudge_run_that_ends_in_ERROR_is_reported_and_never_run_again(tmp_path, store, gate_mode):
    """The second ``run()`` site: the first run stalls on a plan, the driver nudges, and the nudged
    run hits the iteration cap. Two runs (no third), the error on the result, nothing captured."""
    conv = _ScriptedConversation(["plan", "error"])
    sess = _session(tmp_path, conv, gate_mode=gate_mode)

    result = sess.run_turn("create late.txt")

    assert conv.runs == 2, "a third run() would reset ERROR to RUNNING with a fresh iteration budget"
    assert result.nudged is True
    assert result.error is not None
    assert "MaxIterationsReached" in result.error
    assert store.captured == []


def test_the_check7g_shape_never_hides_a_held_action(tmp_path, store):
    """check7g: the first run ends in ERROR; the old capture-time run hit the gate. The result said
    gated=False, pending=[], listed the held create as work that ran, and left the runtime parked."""
    conv = _ScriptedConversation(["error", "halt"])
    sess = _session(tmp_path, conv, gate_mode="gated")

    result = sess.run_turn("view three files then create late.txt")

    assert conv.runs == 1
    assert result.error is not None
    assert not any("late.txt" in line for line in result.tool_activity), (
        "an action that never ran is listed as work that ran"
    )
    assert conv.state.execution_status != ConversationExecutionStatus.WAITING_FOR_CONFIRMATION, (
        "the runtime is parked at the gate and the result does not say so"
    )
    assert store.captured == []


def test_a_halted_run_is_reported_gated_with_its_pending_and_not_run_again(tmp_path, store):
    conv = _ScriptedConversation(["halt", "finish"])
    sess = _session(tmp_path, conv, gate_mode="gated")

    result = sess.run_turn("create late.txt")

    assert conv.runs == 1
    assert result.gated is True
    assert [p.tool_name for p in result.pending] == ["file_editor"]
    assert not any("late.txt" in line for line in result.tool_activity)
    assert store.captured == []


def test_a_finished_turn_is_captured_exactly_once(tmp_path, store):
    """Positive control: the split must not lose the ordinary capture."""
    conv = _ScriptedConversation(["finish"])
    sess = _session(tmp_path, conv, gate_mode="gated")

    result = sess.run_turn("say done")

    assert result.error is None
    assert result.gated is False
    assert result.reply == "done"
    assert len(store.captured) == 1
    assert store.captured[0].content == "[user] say done\n[assistant] done"


def test_approving_a_held_turn_runs_once_and_captures_once(tmp_path, store):
    conv = _ScriptedConversation(["halt", "finish"])
    sess = _session(tmp_path, conv, gate_mode="gated")
    assert sess.run_turn("create late.txt").gated is True

    result = sess.resume_turn()

    assert conv.runs == 2
    assert result.gated is False
    assert len(store.captured) == 1


def test_capture_turn_never_runs_the_conversation(tmp_path, store):
    """The binding's capture is capture only. Running is the driver's, which checks the gate and the
    runtime's status after every run; a run inside capture is checked by nobody."""
    conv = _ScriptedConversation([])
    conv.send_message("say done")
    conv.state.events.append(_reply("done"))
    conv.state.execution_status = ConversationExecutionStatus.ERROR

    _RealCaptureBinding(tmp_path).capture_turn(conv)

    assert conv.runs == 0
    assert len(store.captured) == 1
