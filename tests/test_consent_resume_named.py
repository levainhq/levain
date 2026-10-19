"""Only a RESUME of a session blocks resuming it (1019+24, from SEAT F's Q13 L4 run, project_memory/1019-q13/L4).

``levain consent rule 1 allow --session <sid>`` on an ended Claude Code session printed "a process naming this session
may be running" with no claude process alive: :func:`levain.consent.delivery.session_named_running` matched the id as
a substring of any command line, the rule command's own included, so a late ruling was never auto-resumed. The tests
(all but the unreadable-list one) run real processes whose command lines carry the id and read them through ``ps``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from levain.consent import delivery as dv

_WAIT = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('up'); time.sleep(60)"


@pytest.fixture
def session():
    return str(uuid.uuid4())


@pytest.fixture
def spawn(tmp_path):
    """Start a python process whose command line ends with ``args``; returns it once it is running."""
    procs = []

    def start(*args: str, prog: str = _WAIT) -> subprocess.Popen:
        ready = tmp_path / f"ready-{len(procs)}"
        proc = subprocess.Popen([sys.executable, "-c", prog, str(ready), *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        end = time.monotonic() + 20
        while not ready.exists() and proc.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        assert ready.exists(), proc.communicate()
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_a_process_that_only_mentions_the_session_does_not_count(spawn, session):
    spawn("levain", "consent", "rule", "1", "allow", "--session", session, "--agent", "claude_code")
    spawn("tail", "-f", f"/Users/x/.claude/projects/-w/{session}.jsonl")
    spawn("vim", f"--session={session}", "notes about resume", session)            # a "resume" with no codex token
    assert dv.session_named_running(session) is False


def test_the_calling_process_itself_never_counts(spawn, session, monkeypatch):
    proc = spawn("claude", "--resume", session)                 # stands in for this process, whatever its argv
    monkeypatch.setattr(dv.os, "getpid", lambda: proc.pid)
    assert dv.session_named_running(session) is False


def test_an_ancestor_that_only_mentions_the_session_does_not_count(spawn, session, tmp_path):
    """The rule command run from a shell: the shell and the command both name the id; neither resumes it."""
    child = (f"import sys; sys.path.insert(0, {str(Path(dv.__file__).parents[2])!r}); "
             "from levain.consent import delivery as dv; print(dv.session_named_running(sys.argv[1]))")
    script = tmp_path / "rule.sh"
    script.write_text(f'"{sys.executable}" -c "$1" "$2" --session "$2"; echo done\n')
    got = subprocess.run(["/bin/sh", str(script), child, session, "levain", "consent", "rule", "--session", session],
                         capture_output=True, text=True, timeout=60)
    assert got.stdout.split() == ["False", "done"], got.stderr


def test_a_limits_extend_caller_does_not_count_itself(spawn, session, tmp_path):
    """Q13 L4 Case R: `levain limits --queue-dir <q> extend <sid> --hold 120` was the only process naming the sid, and
    its own check read it as a running session, so the lifted halt resumed nothing."""
    child = (f"import sys; sys.path.insert(0, {str(Path(dv.__file__).parents[2])!r}); "
             "from levain.consent import delivery as dv; print(dv.session_named_running(sys.argv[1]))")
    got = subprocess.run([sys.executable, "-c", child, session, "/v/bin/levain", "limits", "--queue-dir",
                          str(tmp_path / "consent"), "extend", session, "--hold", "120"],
                         capture_output=True, text=True, timeout=60)
    assert got.stdout.split() == ["False"], got.stderr
    spawn("/v/bin/levain", "limits", "--queue-dir", str(tmp_path / "consent"), "extend", session, "--hold", "120")
    assert dv.session_named_running(session) is False             # nor does another extend of it


def test_an_ancestor_that_resumes_the_session_counts(session, tmp_path):
    """A hook of a running resume delivering to its own session: the session is running, never resumed again."""
    child = (f"import sys; sys.path.insert(0, {str(Path(dv.__file__).parents[2])!r}); "
             "from levain.consent import delivery as dv; print(dv.session_named_running(sys.argv[1]))")
    script = tmp_path / "hook.sh"
    script.write_text(f'"{sys.executable}" -c "$1" "$2"; echo done\n')
    got = subprocess.run(["/bin/sh", str(script), child, session, "claude", "-p", "--resume", session],
                         capture_output=True, text=True, timeout=60)
    assert got.stdout.split() == ["True", "done"], got.stderr


@pytest.mark.parametrize("argv", [
    ("claude", "-p", "--resume={s}", "--permission-mode", "bypassPermissions", "levain: ruling on gap #1"),
    ("/Users/x/.local/bin/claude", "--bg", "--resume", "{s}"),
    ("claude", "-r", "{s}"),
    ("/opt/homebrew/bin/codex", "exec", "resume", "-c", "k=v", "{s}", "it's the answer"),
    ("codex", "resume", "{s}"),
])
def test_a_real_resume_of_the_session_counts(spawn, session, argv):
    spawn(*(a.format(s=session) for a in argv))
    assert dv.session_named_running(session) is True


def test_a_resume_of_another_session_does_not_count(spawn, session):
    other = str(uuid.uuid4())
    spawn("claude", f"--resume={other}", "--session", session)
    spawn("codex", "exec", "resume", other, f"look at {session}")
    assert dv.session_named_running(session) is False


def test_an_unreadable_process_list_is_none(session, monkeypatch):
    def raising(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(dv.subprocess, "run", raising)
    assert dv.session_named_running(session) is None
    monkeypatch.setattr(dv.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "denied"))
    assert dv.session_named_running(session) is None
    assert dv.session_named_running("not-a-uuid") is None


# fix r1 (L3 r1 on the self-match fix, project_memory/1019-seatB/b3/resume_FIX_r1.md)

@pytest.fixture
def standin(tmp_path):
    """Start a harmless stand-in named ``claude`` or ``codex`` (a shell script that sleeps, first on PATH), never the
    real CLI: ps then shows ``/bin/sh <dir>/<name> <args>``, the interpreter first and the name second."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        (bindir / name).write_text("#!/bin/sh\nsleep 60\n")
        (bindir / name).chmod(0o755)
    procs = []

    def start(*argv: str) -> subprocess.Popen:
        proc = subprocess.Popen(list(argv), env={**os.environ, "PATH": f"{bindir}:/bin:/usr/bin"},
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        time.sleep(0.2)
        assert proc.poll() is None
        return proc

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("argv", [("claude", "--session-id", "{s}"), ("claude", "-p", "--session-id={s}", "go")])
def test_r1_a_fresh_run_with_the_session_id_counts(standin, session, argv):
    standin(*(a.format(s=session) for a in argv))
    assert dv.session_named_running(session) is True


@pytest.mark.parametrize("argv", [
    ("claude", "-c"),
    ("claude", "-p", "--continue", "go on"),
    ("claude", "--resume"),
    ("claude", "-r", "my-named-session"),
    ("codex", "resume", "--last"),
    ("codex", "exec", "resume", "my-named-session", "go on"),
])
def test_r2_a_resume_without_a_session_id_is_indeterminate_and_holds(standin, session, argv, tmp_path):
    standin(*argv)
    assert dv.session_named_running(session) is None
    meta = {"invocation": "-p", "cwd": str(tmp_path), "codex_pid": 999999}
    got = dv.route("codex", session, meta, running=lambda pid, started=None: False)
    assert got[0] == "hook" and got[1].startswith("a resume without a session id is running"), got


def test_r2_an_id_less_word_in_a_prompt_or_another_tool_does_not_count(standin, spawn, session):
    other = str(uuid.uuid4())
    standin("codex", "exec", "please", "resume", "the", "work")          # "resume" inside a prompt
    standin("claude", "-p", f"--resume={other}", "--permission-mode", "bypassPermissions", "use", "python", "-c")
    spawn("python3", "-c", "x", "claude", "-c")                           # claude is not the program here
    assert dv.session_named_running(session) is False


def test_r3_an_upper_case_id_counts_for_claude(spawn, session):
    spawn("claude", "--resume", session.upper())
    assert dv.session_named_running(session) is True


@pytest.mark.parametrize("form", ["-r={s}", "-r{s}"])
def test_r4_attached_short_resume_forms_count(standin, session, form):
    standin("claude", "-p", form.format(s=session))
    assert dv.session_named_running(session) is True
