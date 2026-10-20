"""Only a RESUME of a session blocks resuming it (1019+24, from SEAT F's Q13 L4 run, project_memory/1019-q13/L4).

``levain consent rule 1 allow --session <sid>`` on an ended Claude Code session printed "a process naming this session
may be running" with no claude process alive: :func:`levain.consent.delivery.session_named_running` matched the id as
a substring of any command line, the rule command's own included, so a late ruling was never auto-resumed.

A ``False`` is only true of the process list it read, and any machine running a ``claude -c`` or a ``--resume`` picker
(every dev seat runs inside one) reads ``None``. So every test that asserts ``False`` reads a list it controls: a
synthetic one, or the real ``ps`` cut down to the processes the test started (:func:`ps_only`). The ``True`` and
``None`` tests read the whole real list: no stray process can turn either into another answer.
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
_REAL_RUN = subprocess.run


@pytest.fixture
def session():
    return str(uuid.uuid4())


@pytest.fixture
def ps_only(monkeypatch):
    """Make ``ps`` (and only ``ps``: ``lsof`` stays real) show the given lines, or the real list's lines for the given
    pids. Call it with ``lines=[...]`` and/or ``pids=[...]``."""
    def use(lines=(), pids=()):
        keep = {str(p) for p in pids}

        def run(argv, *a, **k):
            if argv[0] != "ps":
                return _REAL_RUN(argv, *a, **k)
            real = _REAL_RUN(argv, *a, **k).stdout.splitlines() if keep else []
            shown = [ln for ln in real if ln.strip().partition(" ")[0] in keep] + list(lines)
            return subprocess.CompletedProcess(argv, 0, "\n".join(shown) + "\n", "")

        monkeypatch.setattr(dv.subprocess, "run", run)
    return use


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


@pytest.fixture
def standin(tmp_path):
    """Start a harmless stand-in named ``claude`` or ``codex`` (a shell script that sleeps, first on PATH), never the
    real CLI: ps then shows ``/bin/sh <dir>/<name> <args>``, the interpreter first and the name second. ``start.bindir``
    is the stand-ins' directory, for a test that names one by its full path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        (bindir / name).write_text("#!/bin/sh\nsleep 60\n")
        (bindir / name).chmod(0o755)
    procs = []

    def start(*argv: str, cwd: Path | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(list(argv), env={**os.environ, "PATH": f"{bindir}:/bin:/usr/bin"}, cwd=cwd,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        time.sleep(0.2)
        assert proc.poll() is None
        return proc

    start.bindir = bindir
    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


# a child that runs the check over the real ps cut down to its parent's line: the parent's own argv is the subject
_CHILD = (f"import os, subprocess, sys; sys.path.insert(0, {str(Path(dv.__file__).parents[2])!r}); "
          "from levain.consent import delivery as dv; real = subprocess.run; parent = str(os.getppid())\n"
          "def run(argv, *a, **k):\n"
          "    got = real(argv, *a, **k)\n"
          "    if argv[0] != 'ps': return got\n"
          "    lines = [ln for ln in got.stdout.splitlines() if ln.strip().partition(' ')[0] == parent]\n"
          "    return subprocess.CompletedProcess(argv, 0, '\\n'.join(lines) + '\\n', '')\n"
          "dv.subprocess.run = run\n"
          "print(dv.session_named_running(sys.argv[1]))")


def test_a_process_that_only_mentions_the_session_does_not_count(ps_only, session):
    ps_only(lines=[f"101 levain consent rule 1 allow --session {session} --agent claude_code",
                   f"102 tail -f /Users/x/.claude/projects/-w/{session}.jsonl",
                   f"103 vim --session={session} notes about {session}"])          # the id, never a resume option's value
    assert dv.session_named_running(session) is False


def test_the_calling_process_itself_never_counts(ps_only, session):
    ps_only(lines=[f"{os.getpid()} claude --resume {session}"])     # stands in for this process, whatever its argv
    assert dv.session_named_running(session) is False


def test_an_ancestor_that_only_mentions_the_session_does_not_count(session, tmp_path):
    """The rule command run from a shell: the shell and the command both name the id; neither resumes it."""
    script = tmp_path / "rule.sh"
    script.write_text(f'"{sys.executable}" -c "$1" "$2" --session "$2"; echo done\n')
    got = subprocess.run(["/bin/sh", str(script), _CHILD, session, "levain", "consent", "rule", "--session", session],
                         capture_output=True, text=True, timeout=60)
    assert got.stdout.split() == ["False", "done"], got.stderr


def test_a_limits_extend_caller_does_not_count_itself(ps_only, spawn, session, tmp_path):
    """Q13 L4 Case R: `levain limits --queue-dir <q> extend <sid> --hold 120` was the only process naming the sid, and
    its own check read it as a running session, so the lifted halt resumed nothing."""
    script = tmp_path / "extend.sh"
    script.write_text(f'"{sys.executable}" -c "$1" "$2"; echo done\n')
    got = subprocess.run(["/bin/sh", str(script), _CHILD, session, "/v/bin/levain", "limits", "--queue-dir",
                          str(tmp_path / "consent"), "extend", session, "--hold", "120"],
                         capture_output=True, text=True, timeout=60)
    assert got.stdout.split() == ["False", "done"], got.stderr
    proc = spawn("/v/bin/levain", "limits", "--queue-dir", str(tmp_path / "consent"), "extend", session, "--hold",
                 "120")
    ps_only(pids=[proc.pid])
    assert dv.session_named_running(session) is False             # nor does another extend of it


def test_an_ancestor_that_resumes_the_session_counts(session, tmp_path):
    """A hook of a running resume delivering to its own session: the session is running, never resumed again. The
    ancestor is a stand-in named ``claude`` run as ``claude -p --resume <id>``, which runs the check."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text('#!/bin/sh\n"$LV_PY" -c "$LV_CHILD" "$LV_SID"; echo done\n')
    (bindir / "claude").chmod(0o755)
    got = subprocess.run([str(bindir / "claude"), "-p", "--resume", session], capture_output=True, text=True,
                         timeout=60, env={**os.environ, "LV_PY": sys.executable, "LV_CHILD": _CHILD,
                                          "LV_SID": session})
    assert got.stdout.split() == ["True", "done"], got.stderr


@pytest.mark.parametrize("argv", [
    ("claude", "-p", "--resume={s}", "--permission-mode", "bypassPermissions", "levain: ruling on gap #1"),
    ("{bin}/claude", "--bg", "--resume", "{s}"),
    ("claude", "-r", "{s}"),
    ("{bin}/codex", "exec", "resume", "-c", "k=v", "{s}", "it's the answer"),
    ("codex", "resume", "{s}"),
])
def test_a_real_resume_of_the_session_counts(standin, session, argv):
    standin(*(a.format(s=session, bin=standin.bindir) for a in argv))
    assert dv.session_named_running(session) is True


def test_a_resume_of_another_session_does_not_count(ps_only, session):
    other = str(uuid.uuid4())
    ps_only(lines=[f"201 claude --resume={other} --session {session}",
                   f"202 codex exec resume {other} look at {session}"])
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
    standin(*argv, cwd=tmp_path)                    # a -c / --continue holds only a session of its own directory
    assert dv.session_named_running(session) is None
    meta = {"invocation": "-p", "cwd": str(tmp_path), "codex_pid": 999999}
    got = dv.route("codex", session, meta, running=lambda pid, started=None: False)
    assert got[0] == "hook" and got[1].startswith("a resume without a session id is running"), got


def test_r2_an_id_less_word_in_a_prompt_or_another_tool_does_not_count(ps_only, standin, spawn, session, tmp_path):
    other = str(uuid.uuid4())
    procs = [standin("codex", "exec", "please", "resume", "the", "work"),          # "resume" inside a prompt
             standin("claude", "-p", f"--resume={other}", "--permission-mode", "bypassPermissions", "use", "python",
                     "-c"),
             spawn("python3", "-c", "x", "claude", "-c")]                          # claude is not the program here
    ps_only(pids=[p.pid for p in procs])
    assert dv.session_named_running(session, str(tmp_path)) is False   # r3 R2: the -c holds only in its cwd


def test_r3_an_upper_case_id_counts_for_claude(standin, session):
    standin("claude", "--resume", session.upper())
    assert dv.session_named_running(session) is True


@pytest.mark.parametrize("form", ["-r={s}", "-r{s}"])
def test_r4_attached_short_resume_forms_count(standin, session, form):
    standin("claude", "-p", form.format(s=session))
    assert dv.session_named_running(session) is True


# fix r2 (L3 r2, project_memory/1019-seatB/b4/resume_FIX_r2.md)

@pytest.mark.parametrize("line, want", [
    ("codex resume my-session compare {o}", None),             # a name resumes; the UUID is the prompt's
    ("codex exec resume my-session --json {s}", None),
    ("codex exec resume --output-schema {o} {s} go", True),    # an option's value is not the selector
    ("codex exec resume --skip-git-repo-check -c k=v {s} go", True),
    ("codex exec resume -o {s} {o} go", False),
    ("codex resume --last {s}", True),                         # r3 R1: a strong form
])
def test_f1_the_codex_selector_is_the_first_positional_word(ps_only, session, line, want):
    ps_only(lines=["301 " + line.format(s=session, o=uuid.uuid4())])
    assert dv.session_named_running(session) is want


@pytest.mark.parametrize("line, want", [
    ("worker --session-id {s}", False),
    ("worker -r={s}", False),
    ("worker -r{s}", True),                                    # r3 R1: strong forms need no program
    ("worker --resume {s}", True),
    ("grep claude -c file", None),                             # r3 R2: holds (no cwd to compare)
    ("vim claude -c notes", None),
    ("python3 -c x claude --resume", None),
    ("/Users/x/.local/share/claude/versions/2.1.304 --resume {s}", True),       # the native install's argv[0]
    ("/Users/x/.local/share/claude/versions/2.1.304 --resume /Users/x/.claude/projects/-w/{s}.jsonl -n seat", True),
    ("/Users/x/.local/share/claude/versions/2.1.304 --resume /Users/x/.claude/projects/-w/{o}.jsonl", False),
    ("/Users/x/.local/share/claude/versions/2.1.304 --resume", None),
    ("node /x/lib/node_modules/@anthropic-ai/claude-code/cli.js --resume {s}", True),
    ("node /x/lib/node_modules/@anthropic-ai/claude-code/cli.js -r my-name", None),
    ("env FOO=1 claude -r {s}", True),
    ("/bin/zsh /x/bin/claude --session-id {s}", True),
    ("env FOO=1 codex resume {s}", True),
    ("node /opt/homebrew/bin/codex exec resume --last", None),
])
def test_f2_only_a_claude_or_codex_process_counts(ps_only, session, line, want):
    ps_only(lines=["401 " + line.format(s=session, o=uuid.uuid4())])
    assert dv.session_named_running(session) is want


def test_f4_a_continue_holds_only_a_session_of_its_own_directory(ps_only, standin, session, tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    (tmp_path / "link").symlink_to(here)
    procs = [standin("claude", "-c", cwd=here), standin("claude", "-p", "--continue", "go on", cwd=here)]
    ps_only(pids=[p.pid for p in procs])
    assert dv.session_named_running(session, str(here)) is None
    assert dv.session_named_running(session, str(tmp_path / "link")) is None    # both sides resolved
    assert dv.session_named_running(session, str(there)) is False
    assert dv.session_named_running(session) is None                            # no directory to compare: holds
    for cwd, held in ((there, False), (here, True)):
        meta = {"invocation": "-p", "cwd": str(cwd), "codex_pid": 999999}
        got = dv.route("codex", session, meta, running=lambda pid, started=None: False)
        assert got[1].startswith("a resume without a session id is running") is held, got


def test_f4_a_continue_whose_directory_cannot_be_read_holds(ps_only, session, tmp_path):
    ps_only(lines=["2147483000 claude --continue"])              # no such process: lsof reads no cwd
    assert dv.session_named_running(session, str(tmp_path)) is None


# fix r3 (L3 r3, project_memory/1019-seatB/b5/resume_FIX_r3.md): the argv reading fails CLOSED

_SPACED = "/Users/x/Library/Application Support/Claude/claude-code/2.1.304/claude"
_NATIVE = "/Users/John Doe/.local/share/claude/versions/2.1.304"
# a pid no process can hold (above Linux PID_MAX_LIMIT, 2**22, and macOS's 99999), so a row's cwd can never be
# read: a small number such as 501 is a live process of the runner's user on some CI hosts
_NO_PID = 2 ** 22 + 1


@pytest.mark.parametrize("line, want", [
    # R1: the id as a resume option's value counts in any process, with no program check
    ("env -u ANTHROPIC_API_KEY claude --resume {s}", True),
    ("nohup claude -r {s}", True),
    ("caffeinate -i claude --resume={s}", True),
    ("time nice -n 5 codex exec resume {s} go", True),
    ("sudo -u phill codex resume --skip-git-repo-check {s}", True),
    (_SPACED + " --resume {s}", True),
    (_NATIVE + " --resume /Users/John Doe/.claude/projects/-w/{s}.jsonl", True),   # r4 LOW 1: the path splits
    ("tail -f /Users/x/.claude/projects/-w/{s}.jsonl", False),                     # no resume option before it
    ("node --require x /x/lib/node_modules/@anthropic-ai/claude-code/cli.js -r {s}", True),
    ("worker --resume {s}", True),
    ("worker -r{s}", True),
    ("worker --resume=/x/.claude/projects/-w/{s}.jsonl", True),
    ("notes about resume {s}", True),
    # R2: the weaker forms count after any word that may be the CLI, with no wrapper list
    ("env -u ANTHROPIC_API_KEY claude --session-id {s}", True),
    ("nice -n 5 env -u K claude -r={s}", True),
    (_SPACED + " --session-id={s}", True),
    ("node --require x /x/lib/node_modules/@anthropic-ai/claude-code/cli.js --session-id {s}", True),
    ("env -u ANTHROPIC_API_KEY claude --resume", None),
    ("caffeinate -i codex resume --last", None),
    ("time codex exec resume my-name go", None),
    (_SPACED + " --resume", None),
    (_NATIVE + " -r my-name", None),
    ("node --require x /x/lib/node_modules/@anthropic-ai/claude-code/cli.js --resume", None),
    ("nohup claude -c", None),                                # its cwd cannot be read (_NO_PID)
    # still not a resume of the session
    ("levain consent rule --session {s}", False),
    ("worker --session-id {s}", False),
    ("worker -r={s}", False),
    ("env -u K claude --resume {o} --session {s}", False),
    ("caffeinate -i codex exec resume {o} look at {s}", False),
])
def test_r3_the_argv_reading_fails_closed(ps_only, session, line, want, tmp_path):
    with pytest.raises(ProcessLookupError):
        os.kill(_NO_PID, 0)
    ps_only(lines=[f"{_NO_PID} " + line.format(s=session, o=uuid.uuid4())])
    assert dv.session_named_running(session, str(tmp_path)) is want


def test_r3_a_real_wrapped_resume_counts(standin, session):
    standin("env", "-u", "ANTHROPIC_API_KEY", "claude", "--session-id", session)
    assert dv.session_named_running(session) is True


def test_r3_a_word_claude_in_another_tool_holds_only_on_a_cwd_match(ps_only, spawn, session, tmp_path):
    proc = spawn("claude", "-c", "file")              # a python process whose args read like `grep claude -c file`
    ps_only(pids=[proc.pid])
    assert dv.session_named_running(session, str(tmp_path)) is False
    assert dv.session_named_running(session, os.getcwd()) is None


def test_r3_a_continue_matches_its_directory_whatever_the_case(ps_only, standin, session, tmp_path):
    here = tmp_path / "Here"
    here.mkdir()
    upper = str(here).upper()
    if not os.path.isdir(upper):
        pytest.skip("a case-sensitive file system")
    ps_only(pids=[standin("claude", "-c", cwd=here).pid])
    assert dv.session_named_running(session, upper) is None
