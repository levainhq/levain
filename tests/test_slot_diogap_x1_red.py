"""RED (DIOGAP stage 1, finding X1): a repo ``PermissionRequest`` hook's deny, and a failure of the repo filter, leave
the filter as exit 2 + stderr. Claude Code's hooks reference says exit 2 "isn't honored" for ``PermissionRequest`` ("the
permission flow proceeds unchanged ... Deny through the ``decision`` object instead"; a hook that exits 2 without a
decision object leaves the flow unchanged and its stderr is discarded), so the deny never reaches Claude Code. The fix
lane makes the filter answer a ``PermissionRequest`` deny, and its own failure, with the decision object on stdout and
exit 0, and keeps exit 2 for ``PreToolUse``. These tests fail on the code as it stands."""
from __future__ import annotations

import io
import json

import pytest

from levain.slot import hook_run


def _filter(command: str, timeout: int = 5) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", str(timeout), command],
                                stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _decision(out: str) -> dict:
    doc = json.loads(out)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    return hso["decision"]


@pytest.mark.parametrize("command", [
    "echo nope-from-repo >&2; exit 2",
    "printf '%s' '{\"hookSpecificOutput\":{\"hookEventName\":\"PermissionRequest\","
    "\"decision\":{\"behavior\":\"deny\",\"message\":\"nope-from-repo\"}}}'",
])
def test_a_repo_permission_request_deny_is_a_decision_object_on_stdout_and_exit_0(command):
    code, out, _err = _filter(command)
    assert code == 0
    got = _decision(out)
    assert got["behavior"] == "deny" and "nope-from-repo" in got["message"]


def test_a_filter_failure_on_permission_request_is_a_deny_decision_object_and_exit_0():
    code, out, _err = _filter("sleep 5", timeout=1)
    assert code == 0
    got = _decision(out)
    assert got["behavior"] == "deny" and hook_run.REPO_FAILED in got["message"]


def test_pre_tool_use_keeps_exit_2_and_stderr():
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "5", "echo nope >&2; exit 2"],
                                stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    assert code == 2 and out.getvalue() == "" and "nope" in err.getvalue()


def test_a_permission_request_deny_whose_answer_cannot_be_written_falls_back_to_exit_2():
    """Lane A: a decision that cannot reach stdout is still a deny; exit 2 + stderr is the one answer left."""
    class Broken(io.StringIO):
        def write(self, s):
            raise OSError("closed")
    err = io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", "5", "echo nope >&2; exit 2"],
                                stdin=io.BytesIO(b"{}"), stdout=Broken(), stderr=err)
    assert code == 2 and "nope" in err.getvalue()


@pytest.mark.parametrize("argv", [["PermissionRequest", "5"], ["PermissionRequest", "0", "c"],
                                  ["PermissionRequest", "5", ""]])
def test_a_permission_request_filter_call_render_never_makes_is_a_deny_decision(argv):
    """Lane A: the filter's argv refusal on PermissionRequest is the filter failing, so it is the decision object."""
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", argv, stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    assert code == 0 and err.getvalue() == ""
    got = _decision(out.getvalue())
    assert got["behavior"] == "deny" and "refused" in got["message"]


def test_a_permission_request_decision_is_the_deny_and_nothing_else():
    """Lane A: the decision object carries behavior and message only: no allow-only field, no interrupt."""
    code, out, err = _filter("echo nope-from-repo >&2; exit 2")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "deny", "message": "nope-from-repo"}}}


# --- X4/C3: the repo command never outlives a filter failure between its start and the cleanup -------------------

def _pid_dead(pid: int) -> bool:
    """Whether ``pid`` is gone within a few seconds; a survivor is then killed, so a RED run leaks nothing."""
    import os
    import signal
    import time
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return False


def _wait_for(path, seconds: float) -> bool:
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("raise_", ["emfile", "stopped"])
def test_a_selector_that_cannot_be_made_leaves_no_repo_command_running(tmp_path, monkeypatch, raise_):
    """X4/C3: the selector failing (EMFILE) or the filter being stopped while it is made must not leave the repo
    command running to its own end. The fake waits (bounded) for the command to have written its pid, so a filter that
    makes the selector after starting the command is caught with the command alive."""
    import errno
    import selectors
    pidfile = tmp_path / "pid"

    def boom():
        _wait_for(pidfile, 2.0)
        if raise_ == "emfile":
            raise OSError(errno.EMFILE, "Too many open files")
        raise hook_run._Stopped(15)
    monkeypatch.setattr(selectors, "DefaultSelector", boom)
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "30", f"echo $$ > {pidfile}; exec /bin/sleep 30"],
                                stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    assert code == 2 and err.getvalue().startswith(hook_run.REPO_FAILED)
    if pidfile.exists() and pidfile.read_text().strip():
        assert _pid_dead(int(pidfile.read_text().strip()))


def test_a_register_that_fails_after_the_start_kills_the_repo_command(tmp_path, monkeypatch):
    """X4/C3 (the reorder's other half): a failure in the selector's first use, after the command started, is inside
    the cleanup."""
    import selectors
    pidfile = tmp_path / "pid"
    real = selectors.DefaultSelector

    class Sel:
        def __init__(self):
            self._s = real()

        def register(self, *a, **k):
            _wait_for(pidfile, 2.0)
            raise OSError("register failed")

        def close(self):
            self._s.close()
    monkeypatch.setattr(selectors, "DefaultSelector", Sel)
    err = io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "30", f"echo $$ > {pidfile}; exec /bin/sleep 30"],
                                stdin=io.BytesIO(b"{}"), stdout=io.StringIO(), stderr=err)
    assert code == 2 and err.getvalue().startswith(hook_run.REPO_FAILED)
    assert pidfile.read_text().strip() and _pid_dead(int(pidfile.read_text().strip()))


# --- D5: a close that raises in the cleanup never replaces the specific cause --------------------------------------

def test_a_close_that_raises_keeps_the_timeout_cause(monkeypatch):
    import selectors
    real = selectors.DefaultSelector

    class Sel(real):    # type: ignore[misc,valid-type]
        def close(self):
            super().close()
            raise RuntimeError("close broke")
    monkeypatch.setattr(selectors, "DefaultSelector", Sel)
    err = io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "1", "exec /bin/sleep 5"],
                                stdin=io.BytesIO(b"{}"), stdout=io.StringIO(), stderr=err)
    assert code == 2 and "ran past its timeout" in err.getvalue()


# --- D2: a present matcher that is not a string is dropped and named, never widened to every tool ---------------

@pytest.mark.parametrize("matcher", [["Bash"], None, 5, {"x": 1}])
def test_a_group_whose_matcher_is_not_a_string_is_dropped_and_named(matcher):
    from levain.slot import render as rd
    from tests.test_slot_r161_freeze import frozen_blob
    doc = {".claude/settings.json": {"hooks": {"PreToolUse": [
        {"matcher": matcher, "hooks": [{"type": "command", "command": "odd-matcher"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "kept"}]}]}}}
    fz = rd.freeze_repo_settings(doc)
    assert "hooks.PreToolUse:matcher" in fz.dropped_other
    blob, facts, _ = frozen_blob("linux", doc)
    groups = rd.render_cc_settings(blob, facts)["hooks"]["PreToolUse"]
    wired = [g for g in groups for h in g["hooks"] if "levain_repo_filter" in h["command"]]
    assert [h["command"].rsplit(" ", 1)[-1] for g in wired for h in g["hooks"]] == ["kept"]
    assert all(g.get("matcher") == "Bash" for g in wired)


@pytest.mark.parametrize("matcher", ["", "Bash"])
def test_a_string_matcher_is_kept(matcher):
    from levain.slot import render as rd
    from tests.test_slot_r161_freeze import frozen_blob
    doc = {".claude/settings.json": {"hooks": {"PreToolUse": [
        {"matcher": matcher, "hooks": [{"type": "command", "command": "kept"}]}]}}}
    fz = rd.freeze_repo_settings(doc)
    assert fz.dropped_other == ()
    blob, facts, _ = frozen_blob("linux", doc)
    wired = [g for g in rd.render_cc_settings(blob, facts)["hooks"]["PreToolUse"]
             for h in g["hooks"] if "levain_repo_filter" in h["command"]]
    assert len(wired) == 1 and wired[0]["matcher"] == matcher


def test_render_drops_a_non_string_matcher_that_reaches_it_past_the_freeze():
    """D2, render half: a blob's repo layer that did not come through the freeze (the grammar accepts any matcher) is
    never widened to every tool either."""
    import dataclasses
    from levain.slot import render as rd
    from tests.test_slot_r161_freeze import frozen_blob
    blob, facts, _ = frozen_blob("linux", {})
    blob = dataclasses.replace(blob, repo_settings={"hooks": {"PreToolUse": [
        {"matcher": ["Bash"], "hooks": [{"type": "command", "command": "odd"}]}]}})
    groups = rd.render_cc_settings(blob, facts)["hooks"]["PreToolUse"]
    assert not [g for g in groups for h in g["hooks"] if "levain_repo_filter" in h["command"]]
