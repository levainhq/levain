"""The confined shell learns a command's completion and exit status OUT OF BAND (spore-1385).

Each command is its own bash process, spawned and reaped by levain; completion is levain's waitpid on
it and the status is that waitpid status. Nothing the shell prints is parsed for either, so code the
entity runs cannot end a command early or forge its status. These run levain's own floor (Seatbelt on
macOS, bwrap on Linux) through levain's spawn code, nothing else.
"""
from __future__ import annotations

import os
import platform
import random
import signal
import subprocess
import time
from pathlib import Path

import pytest

from levain.firing.confinement import (
    ConfinementError,
    SandboxedShell,
    build_policy,
    bwrap_available,
    sandbox_exec_available,
    select_provider,
)

_SYSTEM = platform.system()
_LIVE = (_SYSTEM == "Darwin" and sandbox_exec_available()) or (
    _SYSTEM == "Linux" and bwrap_available()
)
live = pytest.mark.skipif(not _LIVE, reason="needs levain's floor (macOS sandbox-exec or Linux bwrap)")


def _entity(tmp_path: Path) -> Path:
    d = tmp_path / "coyote"
    (d / ".levain").mkdir(parents=True)
    return d


def _shell(tmp_path: Path) -> SandboxedShell:
    return select_provider().spawn_shell(build_policy(_entity(tmp_path)))


def _marker() -> str:
    """A sleep duration no other run uses: a leftover from an interrupted run cannot match it."""
    return f"{random.randint(30000, 99999)}.{random.randint(1, 9)}"


def _pids_with(marker: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def _kill_all(pids: list[int]) -> None:
    for pid in pids:   # cleanup so a failed assert leaves nothing running
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# --- forgery ------------------------------------------------------------------------------------


@live
def test_a_command_cannot_forge_its_completion_or_status_by_reading_the_shells_script(tmp_path):
    """spore-1385. Under the old model bash read its commands from levain's pipe one byte at a time
    (fd 255), so a builtin in the command could read the next line, levain's status printf, and run it
    with a status of its choosing while the real command kept running. Here the command reads fd 255,
    rewrites whatever it finds to report 42, and evaluates it; the real status is 5, a second later."""
    with _shell(tmp_path) as sh:
        t0 = time.monotonic()
        r = sh.run(
            "IFS= read -r -u 255 s 2>/dev/null; eval \"${s%%\\\"*}42\" 2>/dev/null; sleep 1; (exit 5)",
            timeout=20,
        )
        elapsed = time.monotonic() - t0
        assert (r.exit_code, r.timed_out) == (5, False)
        assert elapsed >= 1.0, "the command was reported finished before it finished"
        nxt = sh.run("echo next", timeout=10)
        assert (nxt.exit_code, nxt.output.strip()) == (0, "next")


@live
def test_printing_a_status_line_changes_nothing(tmp_path):
    """Text is never parsed: a command that prints anything shaped like a completion marker gets its
    own status back, with its output intact."""
    with _shell(tmp_path) as sh:
        r = sh.run("printf '__LEVAIN_SENTINEL_00__ 0\\n'; exit 3", timeout=10)
        assert r.exit_code == 3
        assert "__LEVAIN_SENTINEL_00__ 0" in r.output


@live
def test_the_command_cannot_read_more_of_its_own_input(tmp_path):
    """The command text reaches bash on stdin and stdin is /dev/null by the time it runs: a command
    that reads stdin gets EOF at once, and nothing of levain's."""
    with _shell(tmp_path) as sh:
        r = sh.run("cat; echo rc=$?", timeout=10)
        assert r.output.strip() == "rc=0"


# --- status and state --------------------------------------------------------------------------


@live
def test_exit_n_reports_n_and_the_next_command_keeps_the_state(tmp_path):
    with _shell(tmp_path) as sh:
        r = sh.run(f"mkdir -p {tmp_path}/d && cd {tmp_path}/d && export KEEP=1 && exit 7", timeout=10)
        assert r.exit_code == 7
        assert sh.closed is False
        nxt = sh.run('echo "$PWD $KEEP"', timeout=10)
        assert nxt.output.strip() == f"{Path(tmp_path).resolve()}/d 1" or nxt.output.strip() == f"{tmp_path}/d 1"


@live
def test_a_signal_death_is_reported_as_a_signal(tmp_path):
    """macOS: bash is levain's own child, so waitpid says SIGKILL. Linux: bash is pid 1 of bwrap's pid
    namespace, which ignores signals from inside it, so the shell sends it from outside, as levain's
    timeout does; bwrap then exits 128+9 the way a shell reports it."""
    with _shell(tmp_path) as sh:
        if _SYSTEM == "Darwin":
            r = sh.run("kill -9 $$", timeout=10)
            assert (r.exit_code, r.signal) == (None, signal.SIGKILL)
        else:
            r = sh.run("sleep 30", timeout=1)
            assert r.timed_out is True
        assert sh.run("echo alive", timeout=10).output.strip() == "alive"


@live
def test_errexit_failure_still_saves_the_state(tmp_path):
    with _shell(tmp_path) as sh:
        assert sh.run("set -e; export E1=yes; false; echo unreachable", timeout=10).exit_code == 1
        r = sh.run('echo "E1=$E1"; case $- in *e*) echo errexit-on;; esac', timeout=10)
        assert "E1=yes" in r.output and "errexit-on" in r.output


@live
def test_cwd_env_functions_aliases_options_and_plain_variables_persist(tmp_path):
    with _shell(tmp_path) as sh:
        setup = (
            f"cd {tmp_path} && export EXP='a b' && PLAIN=$'two\\nlines' && arr=(x 'y z') && "
            "greet() { echo \"hi $1\"; } && shopt -s expand_aliases && alias ll='echo aliased' && "
            "set -o pipefail && unset -v PATH_UNSET_PROBE"
        )
        assert sh.run(setup, timeout=10).exit_code == 0
        r = sh.run(
            'echo "pwd=$PWD"; echo "exp=$EXP"; printf "plain=%s|\\n" "$PLAIN"; echo "arr=${arr[1]}"; '
            "greet you; ll; set -o | grep pipefail",
            timeout=10,
        )
        out = r.output
        assert f"pwd={tmp_path}" in out or f"pwd={Path(tmp_path).resolve()}" in out
        assert "exp=a b" in out and "plain=two\nlines|" in out and "arr=y z" in out
        assert "hi you" in out and "aliased" in out
        assert "pipefail" in out and "on" in out.split("pipefail", 1)[1].split("\n", 1)[0]


@live
def test_an_unset_environment_variable_stays_unset(tmp_path):
    with _shell(tmp_path) as sh:
        assert sh.run("export GONE=1", timeout=10).exit_code == 0
        assert sh.run("unset GONE", timeout=10).exit_code == 0
        assert sh.run('echo "[${GONE-unset}]"', timeout=10).output.strip() == "[unset]"
        assert sh.run('echo "[${TERM-unset}]"', timeout=10).output.strip() == "[dumb]"
        assert sh.run("unset TERM", timeout=10).exit_code == 0
        assert sh.run('echo "[${TERM-unset}]"', timeout=10).output.strip() == "[unset]"


@live
def test_a_syntax_error_is_a_status_not_a_hang(tmp_path):
    """The old model fed the command into bash's script, so an unterminated quote swallowed the status
    line and the run timed out. Here the command is parsed on its own."""
    with _shell(tmp_path) as sh:
        r = sh.run("echo 'unterminated", timeout=10)
        assert r.timed_out is False and r.exit_code not in (None, 0)
        assert sh.run("echo fine", timeout=10).output.strip() == "fine"


@live
def test_a_function_named_cd_does_not_break_the_saved_directory(tmp_path):
    with _shell(tmp_path) as sh:
        assert sh.run(f"cd() {{ echo hijacked; }}; builtin cd {tmp_path}", timeout=10).exit_code == 0
        r = sh.run("pwd", timeout=10)
        assert r.output.strip() in (str(tmp_path), str(Path(tmp_path).resolve()))


# --- background jobs, timeouts, close ----------------------------------------------------------


@live
def test_a_background_job_does_not_hold_the_command_open(tmp_path):
    marker = _marker()
    with _shell(tmp_path) as sh:
        t0 = time.monotonic()
        r = sh.run(f"(sleep 1; echo from-background) & sleep {marker} > /dev/null 2>&1 & echo started", timeout=20)
        assert time.monotonic() - t0 < 5
        assert (r.exit_code, r.output.strip()) == (0, "started")
        time.sleep(2.0)
        nxt = sh.run("echo now", timeout=10)
        if _SYSTEM == "Darwin":
            # a background job outlives its command; its later output comes with the next result
            assert "from-background" in nxt.output and nxt.output.rstrip().endswith("now")
            assert _pids_with(f"sleep {marker}"), "the background job should still be running"
        else:
            # bwrap's pid namespace ends with the command, and its background jobs with it
            assert nxt.output.strip() == "now"
            assert not _pids_with(f"sleep {marker}")
    leaked = _pids_with(f"sleep {marker}")
    _kill_all(leaked)
    assert not leaked, "close() must end background jobs"


@pytest.mark.skipif(not (_SYSTEM == "Linux" and _LIVE), reason="bwrap's pid namespace, Linux only")
def test_linux_a_setsid_child_ends_with_its_command(tmp_path):
    """Each command is its own bwrap and its bash is pid 1 of the namespace (``--as-pid-1``), so the
    kernel ends everything in it when bash exits, a child in a new session included."""
    marker = _marker()
    with _shell(tmp_path) as sh:
        r = sh.run(f"setsid sleep {marker} > /dev/null 2>&1 < /dev/null & echo started", timeout=20)
        assert (r.exit_code, r.output.strip()) == (0, "started")
        time.sleep(0.5)
        leaked = _pids_with(f"sleep {marker}")
        _kill_all(leaked)
        assert not leaked


@live
def test_a_timeout_kills_the_commands_whole_group(tmp_path):
    marker = _marker()
    with _shell(tmp_path) as sh:
        t0 = time.monotonic()
        r = sh.run(f"echo partial; sleep {marker}; echo never", timeout=1)
        assert r.timed_out is True and r.exit_code is None
        assert "partial" in r.output and "never" not in r.output
        assert time.monotonic() - t0 < 10
        time.sleep(0.3)
        leaked = _pids_with(f"sleep {marker}")
        _kill_all(leaked)
        assert not leaked, "the timed-out command's group must be gone"
        nxt = sh.run("echo fresh", timeout=10)
        assert (nxt.exit_code, nxt.output.strip()) == (0, "fresh")


@live
def test_close_leaves_nothing_running_and_removes_its_state(tmp_path):
    marker = _marker()
    sh = _shell(tmp_path)
    state_dir = sh._state_dir   # type: ignore[attr-defined]
    assert state_dir is not None and Path(state_dir).is_dir()
    sh.run(f"sleep {marker} > /dev/null 2>&1 &", timeout=10)
    sh.close()
    time.sleep(0.3)
    leaked = _pids_with(f"sleep {marker}")
    _kill_all(leaked)
    assert not leaked
    assert not Path(state_dir).exists()
    sh.close()   # idempotent
    with pytest.raises(ConfinementError):
        sh.run("echo nope")


@live
def test_output_without_a_trailing_newline_is_returned_whole(tmp_path):
    with _shell(tmp_path) as sh:
        r = sh.run("printf abc", timeout=10)
        assert (r.exit_code, r.output) == (0, "abc")


@live
def test_output_is_bounded(tmp_path):
    from levain.firing.confinement import _MAX_OUTPUT_CHARS

    with _shell(tmp_path) as sh:
        r = sh.run(f"head -c {_MAX_OUTPUT_CHARS * 2} /dev/zero | tr '\\0' x", timeout=30)
        assert r.exit_code == 0
        assert len(r.output) <= _MAX_OUTPUT_CHARS + 200
        assert "[output truncated" in r.output


# --- start ------------------------------------------------------------------------------------


def test_start_fails_closed_when_the_driver_cannot_run_bash(tmp_path):
    """Hermetic: a driver that exits at once without running the command fails the start probe."""
    ws = tmp_path / "ws"
    ws.mkdir()
    shell = SandboxedShell(argv=["/usr/bin/false"], cwd=ws, env={"PATH": "/usr/bin:/bin"})
    with pytest.raises(ConfinementError):
        shell.start()
    shell.close()
