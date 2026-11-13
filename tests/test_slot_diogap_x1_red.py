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


class _Broken(io.StringIO):
    """A stdout whose write fails and which has no file descriptor beneath it."""
    def write(self, s):
        raise OSError("closed")


def test_a_permission_request_deny_whose_answer_cannot_be_written_is_a_hook_failure_exit_1():
    """DIOGAP-A2 item 1 (codex MED, deepseek MED): exit 2 is ignored on PermissionRequest, so a decision that cannot
    reach stdout is answered as a hook FAILURE (exit 1), which the rendered ``onFailure: block`` makes a deny there."""
    err = io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", "5", "echo nope >&2; exit 2"],
                                stdin=io.BytesIO(b"{}"), stdout=_Broken(), stderr=err)
    assert code == hook_run.FAILED_EXIT == 1 and "nope" in err.getvalue()


def test_a_permission_request_deny_whose_stream_breaks_is_written_raw_to_its_descriptor():
    """DIOGAP-A2 item 1: a broken text wrapper over an intact fd still delivers the decision, by one raw write."""
    import os
    r, w = os.pipe()
    try:
        class BrokenOverFd(_Broken):
            def fileno(self):
                return w
        err = io.StringIO()
        code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", "5", "echo nope-from-repo >&2; exit 2"],
                                    stdin=io.BytesIO(b"{}"), stdout=BrokenOverFd(), stderr=err)
        os.close(w)
        w = -1
        got = os.read(r, 65536).decode()
    finally:
        os.close(r)
        if w != -1:
            os.close(w)
    assert (code, err.getvalue()) == (0, "")
    assert _decision(got) == {"behavior": "deny", "message": "nope-from-repo"}


def test_a_permission_request_deny_whose_descriptor_is_broken_too_is_exit_1():
    import os
    r, w = os.pipe()
    os.close(r)
    try:
        class BrokenOverDeadFd(_Broken):
            def fileno(self):
                return w
        err = io.StringIO()
        code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", "5", "echo nope >&2; exit 2"],
                                    stdin=io.BytesIO(b"{}"), stdout=BrokenOverDeadFd(), stderr=err)
    finally:
        os.close(w)
    assert code == 1 and "nope" in err.getvalue()


def test_a_short_raw_write_of_the_decision_is_exit_1(monkeypatch):
    """The raw write counts only when the whole document went (the argv refusal: no repo command, no subprocess)."""
    import os
    r, w = os.pipe()
    try:
        class BrokenOverFd(_Broken):
            def fileno(self):
                return w
        monkeypatch.setattr(hook_run.os, "write", lambda fd, b: len(b) - 1)
        err = io.StringIO()
        code = hook_run.repo_filter("levain_repo_filter", ["PermissionRequest", "5"], stdin=io.BytesIO(b"{}"),
                                    stdout=BrokenOverFd(), stderr=err)
    finally:
        monkeypatch.undo()
        os.close(r)
        os.close(w)
    assert code == 1 and "repo-filter-argv (2 argument(s))" in err.getvalue()


def test_pre_tool_use_with_a_broken_stdout_is_still_exit_2():
    err = io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "5", "echo nope >&2; exit 2"],
                                stdin=io.BytesIO(b"{}"), stdout=_Broken(), stderr=err)
    assert code == 2 and err.getvalue() == "nope\n"


@pytest.mark.parametrize("argv", [["PermissionRequest", "5"], ["PermissionRequest", "0", "c"],
                                  ["PermissionRequest", "5", ""]])
def test_a_permission_request_filter_call_render_never_makes_is_a_deny_decision(argv):
    """Lane A: the filter's argv refusal on PermissionRequest is the filter failing, so it is the decision object."""
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", argv, stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    assert code == 0 and err.getvalue() == ""
    got = _decision(out.getvalue())
    assert got["behavior"] == "deny" and "refused" in got["message"]


@pytest.mark.parametrize("event", ["PermissionRequest", "PreToolUse"])
@pytest.mark.parametrize("rest, reason", [
    (["5"], "2 argument(s)"),
    (["0", "c"], "timeout out of range"),
    ([str(hook_run.REPO_TIMEOUT_MAX + 1), "c"], "timeout out of range"),
    (["5x", "c"], "timeout '5x'"),
    (["5", ""], "empty command"),
])
def test_the_argv_refusal_names_the_first_thing_that_failed(event, rest, reason):
    """DIOGAP-A2 item 4 (deepseek LOW): the refusal said "3 argument(s)" when the count was right and the timeout or
    the command was what failed."""
    out, err = io.StringIO(), io.StringIO()
    hook_run.repo_filter("levain_repo_filter", [event] + rest, stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    said = _decision(out.getvalue())["message"] if event == "PermissionRequest" else err.getvalue()
    assert "repo-filter-argv (%s)" % reason in said
    assert "3 argument(s)" not in said


def test_the_argv_refusal_names_an_event_the_filter_does_not_take():
    err = io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["Stop", "5", "c"], stdin=io.BytesIO(b"{}"),
                                stdout=io.StringIO(), stderr=err)
    assert code == 2 and "repo-filter-argv (event 'Stop')" in err.getvalue()


# --- DIOGAP-A2 item 2: run() (levain_queue, wired on PermissionRequest) denies with the decision object --------------

def _queue(load, environ=None, stdout=None, name="levain_queue"):
    out, err = (stdout if stdout is not None else io.StringIO()), io.StringIO()
    env = {"LEVAIN_QUEUE_SOCKET": "/x/q.sock"} if environ is None else environ
    code = hook_run.run(name, stdin=io.StringIO("{}"), stdout=out, stderr=err, environ=env, load_adapter=load)
    return code, (out.getvalue() if isinstance(out, io.StringIO) and not isinstance(out, _Broken) else None), \
        err.getvalue()


def _no_adapter():
    raise ImportError("no adapter here")


class _Answers:
    @staticmethod
    def main(argv, stdin=None, stdout=None, environ=None):
        stdout.write("{}\n")
        return 0


class _ExitsOne:
    @staticmethod
    def main(argv, stdin=None, stdout=None, environ=None):
        return 1


@pytest.mark.parametrize("load, environ, why", [
    (_no_adapter, None, "hook-adapter-unavailable"),
    (lambda: _ExitsOne, None, "hook-adapter-exit"),
    (lambda: _Answers, {}, "hook-not-in-slot"),
])
def test_a_queue_refusal_is_a_deny_decision_object_and_exit_0(load, environ, why):
    """complement MED: run()'s exit 2 + stderr is ignored on PermissionRequest, so the queue's refusal left the call
    to the session's mode. Now it is the decision object, the refusal line its message."""
    code, out, err = _queue(load, environ)
    assert (code, err) == (0, "")
    got = _decision(out)
    assert got["behavior"] == "deny" and got["message"].startswith("levain hook levain_queue: refused: %s (" % why)
    assert set(got) == {"behavior", "message"}


def test_a_queue_answer_that_cannot_be_written_is_a_hook_failure_exit_1():
    code, _out, err = _queue(lambda: _Answers, stdout=_Broken())
    assert code == 1 and "refused: hook-adapter-failed (writing the answer" in err


def test_a_queue_answer_passes_through_unchanged():
    assert _queue(lambda: _Answers) == (0, "{}\n", "")


def test_the_gate_refusal_is_still_exit_2_and_stderr():
    code, out, err = _queue(_no_adapter, name="levain_gate")
    assert (code, out) == (2, "") and "refused: hook-adapter-unavailable" in err


def test_the_real_adapter_leaves_a_slot_permission_request_undecided_through_run(monkeypatch):
    """The real adapter, in a slot, answers a PermissionRequest with no decision (Claude Code's mode answers): run()
    on the queue passes that through unchanged, exit 0 and nothing written; only a refusal becomes a deny."""
    monkeypatch.delenv("LEVAIN_QUEUE_SOCKET", raising=False)
    out, err = io.StringIO(), io.StringIO()
    ev = '{"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}'
    code = hook_run.run("levain_queue", stdin=io.StringIO(ev), stdout=out, stderr=err,
                        environ={"LEVAIN_QUEUE_SOCKET": "/nonexistent/q.sock", "LEVAIN_QUEUE_CAP": "c"})
    assert (code, out.getvalue()) == (0, "")


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
    command running. The selector is made BEFORE the command starts, so the command never starts: the fake waits
    (briefly, bounded) for a pid, and a filter that made the selector after the start would show one there and fail
    (DIOGAP-A2 item 5: the old form waited 2 s and then asserted nothing on the fixed order)."""
    import errno
    import selectors
    pidfile = tmp_path / "pid"
    started = []

    def boom():
        started.append(_wait_for(pidfile, 0.5))
        if raise_ == "emfile":
            raise OSError(errno.EMFILE, "Too many open files")
        raise hook_run._Stopped(15)
    monkeypatch.setattr(selectors, "DefaultSelector", boom)
    out, err = io.StringIO(), io.StringIO()
    code = hook_run.repo_filter("levain_repo_filter", ["PreToolUse", "30", f"echo $$ > {pidfile}; exec /bin/sleep 30"],
                                stdin=io.BytesIO(b"{}"), stdout=out, stderr=err)
    if pidfile.exists() and pidfile.read_text().strip():
        _pid_dead(int(pidfile.read_text().strip()))   # a regression's child is killed, so a failing run leaks nothing
    assert started == [False] and not pidfile.exists(), "the repo command started before the selector was made"
    assert code == 2 and err.getvalue().startswith(hook_run.REPO_FAILED)


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
