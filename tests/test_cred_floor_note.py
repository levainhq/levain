"""The note that names the standard credential floor when a shell reads like a missing credential.

Since the floor denies the standard credential stores in every drive but the interactive REPL, a
headless `git push` over HTTPS or a `gh` call fails with the tool's own words, which read like a
broken remote or a logged-out tool. The turn's activity says which switch it was instead."""
from types import SimpleNamespace

import pytest

from levain.session import CRED_FLOOR_NOTE, EntitySession, _activity_callback, turn_tool_activity
from tests.test_session import _Binding, _Event

GIT = "fatal: could not read Username for 'https://github.com': Device not configured"
KEYCHAIN = "failed to get: -50"
GH = "To get started with GitHub CLI, please run:  gh auth login"


def _shell(command):
    return SimpleNamespace(source="agent", tool_name="terminal",
                           action=SimpleNamespace(kind="TerminalAction", command=command))


def _output(text, tool="terminal"):
    return SimpleNamespace(source="environment", tool_name=tool, observation=SimpleNamespace(text=text))


def _turn(*events):
    return [_Event("user", ["push it"]), *events]


@pytest.mark.parametrize("text", [GIT, KEYCHAIN, GH])
def test_a_credential_failure_ends_the_activity_with_one_note(tmp_path, text):
    events = _turn(_shell("git push"), _output(text), _shell("git push"), _output(text))
    assert turn_tool_activity(events, tmp_path, cred_floor=True) == [
        "⚙ terminal: git push", "⚙ terminal: git push", CRED_FLOOR_NOTE]


def test_no_note_without_the_floor_or_the_signature(tmp_path):
    assert CRED_FLOOR_NOTE not in turn_tool_activity(_turn(_shell("git push"), _output(GIT)), tmp_path)
    assert CRED_FLOOR_NOTE not in turn_tool_activity(
        _turn(_shell("ls"), _output("README.md")), tmp_path, cred_floor=True)
    # the words in a file the editor showed are not a shell failing
    assert CRED_FLOOR_NOTE not in turn_tool_activity(
        _turn(_output(GIT, tool="file_editor")), tmp_path, cred_floor=True)
    # a failure in an earlier turn is not this turn's
    events = [_Event("user", ["a"]), _shell("git push"), _output(GIT), _Event("user", ["b"]), _shell("ls")]
    assert turn_tool_activity(events, tmp_path, cred_floor=True) == ["⚙ terminal: ls"]


def test_the_streamed_activity_carries_the_note_once_a_turn(tmp_path):
    lines = []
    cb = _activity_callback(lines.append, tmp_path, cred_floor=True)
    for e in [_Event("user", ["a"]), _shell("git push"), _output(GIT), _output(GH),
              _Event("user", ["b"]), _output(GH)]:
        cb(e)
    assert lines == ["⚙ terminal: git push", CRED_FLOOR_NOTE, CRED_FLOOR_NOTE]
    quiet = []
    cb = _activity_callback(quiet.append, tmp_path)
    cb(_output(GIT))
    assert quiet == []


def test_a_turn_result_carries_the_note_when_the_session_denies_the_stores(tmp_path):
    class _Conv:
        def __init__(self):
            self.state = SimpleNamespace(events=[], execution_status="idle")
            self.agent = SimpleNamespace(tools_map={"terminal": object()})

        def send_message(self, message):
            self.state.events.append(_Event("user", [message]))

        def run(self):
            self.state.events.extend([_shell("git push"), _output(GIT), _Event("agent", ["The push failed."])])
            self.state.execution_status = "finished"

    def session(deny):
        return EntitySession(
            entity_dir=tmp_path, binding=_Binding(), conversation=_Conv(), workspace=tmp_path / "workspace",
            model_label="m", with_tools=False, bash_ok=False, gate_mode="ungated", deny_standard_creds=deny,
        )

    assert session(True).run_turn("push it").tool_activity == ["⚙ terminal: git push", CRED_FLOOR_NOTE]
    assert session(False).run_turn("push it").tool_activity == ["⚙ terminal: git push"]
