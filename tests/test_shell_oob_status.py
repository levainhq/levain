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
import select
import signal
import subprocess
import threading
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
def test_errexit_failure_still_carries_the_exports_but_not_errexit(tmp_path):
    with _shell(tmp_path) as sh:
        assert sh.run("set -e; export E1=yes; false; echo unreachable", timeout=10).exit_code == 1
        r = sh.run('echo "E1=$E1"; case $- in *e*) echo errexit-on;; esac', timeout=10)
        assert "E1=yes" in r.output and "errexit-on" not in r.output


@live
def test_only_the_directory_and_the_exports_carry(tmp_path):
    """By design (S2 L2 ruling): cwd, OLDPWD and exported variables carry, as data; functions,
    aliases, options, traps and plain variables start fresh, as in a new terminal."""
    with _shell(tmp_path) as sh:
        setup = (
            f"cd {tmp_path} && export EXP=$'a b\\nc=d' && PLAIN=1 && "
            "greet() { echo \"hi $1\"; } && shopt -s expand_aliases && alias ll='echo aliased' && "
            "set -o pipefail && umask 077"
        )
        assert sh.run(setup, timeout=10).exit_code == 0
        r = sh.run(
            'echo "pwd=$PWD"; printf "exp=%s|\\n" "$EXP"; echo "plain=${PLAIN-unset}"; '
            "type greet >/dev/null 2>&1 || echo no-function; alias ll >/dev/null 2>&1 || echo no-alias; "
            "set -o | grep pipefail; umask",
            timeout=10,
        )
        out = r.output
        assert out.splitlines()[0] in (f"pwd={tmp_path}", f"pwd={Path(tmp_path).resolve()}")
        assert "exp=a b\nc=d|" in out and "plain=unset" in out
        assert "no-function" in out and "no-alias" in out
        assert "off" in out.split("pipefail", 1)[1].split("\n", 1)[0]
        assert "0077" not in out


@live
def test_a_command_that_replaces_the_exit_trap_carries_nothing(tmp_path):
    """Documented: the runner's EXIT trap writes the state back, so a command that sets its own
    leaves the next command where the previous one ended; its trap does not carry either."""
    (tmp_path / "far").mkdir()
    with _shell(tmp_path) as sh:
        assert sh.run(f"cd {tmp_path}; export KEEP=1", timeout=10).exit_code == 0
        r = sh.run(f"trap 'echo TRAPPED' EXIT; cd {tmp_path}/far; export KEEP=2", timeout=10)
        assert "TRAPPED" in r.output
        r = sh.run('echo "$PWD $KEEP"', timeout=10)
        assert r.output.strip() in (f"{tmp_path} 1", f"{Path(tmp_path).resolve()} 1")
        assert "TRAPPED" not in r.output


@live
def test_cd_dash_round_trips(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    real = lambda p: {str(p), str(p.resolve())}   # noqa: E731
    with _shell(tmp_path) as sh:
        assert sh.run(f"cd {a}", timeout=10).exit_code == 0
        assert sh.run(f"cd {b}", timeout=10).exit_code == 0
        assert sh.run("cd - >/dev/null", timeout=10).exit_code == 0
        assert sh.run("pwd", timeout=10).output.strip() in real(a)
        assert sh.run("cd - >/dev/null; pwd", timeout=10).output.strip() in real(b)


_PLANTS = {
    "startup-names": (
        "export BASH_ENV={hook} ENV={hook} PROMPT_COMMAND='echo PLANTED-PC' PS4='$(echo PLANTED-PS4)' "
        "CDPATH=/ GLOBIGNORE='*' POSIXLY_CORRECT=1 LD_PRELOAD=/nonexistent.so "
        "DYLD_INSERT_LIBRARIES=/nonexistent.dylib BASH_XTRACEFD=1 GITHUB_TOKEN=planted-token; "
        "set -o errexit; export SHELLOPTS; true"
    ),
    "ifs-and-tmout": "export IFS=x TMOUT=1; true",
    "functions-aliases-options": (
        "ls() { echo PLANTED-LS; }; export -f ls; shopt -s extglob; "
        "eval 'xg() { case $1 in @(a|b)) echo m;; esac; }'; shopt -s expand_aliases; alias false=true; "
        "set -e; set -m; true"
    ),
    "shadowed-builtin": "builtin() { return 0; }; ls() { echo PLANTED-LS; }; set -e; true",
}


@live
@pytest.mark.parametrize("plant", list(_PLANTS))
def test_a_planted_startup_variable_or_function_never_reaches_the_next_command(tmp_path, plant):
    """S2 L2 H1/M1/L1/L2/L4/L5: nothing a command leaves behind runs as code in the next one. Each
    name that makes bash or the loader run code at startup is planted; so are a function shadowing
    ``ls`` (and ``builtin``, which broke the old save), an extglob function, an alias, errexit and job
    control. The next command still fails as it should and sees none of them."""
    hook = tmp_path / "hook.sh"
    hook.write_text("echo PLANTED-HOOK\ntrap 'exit 0' EXIT\n")
    with _shell(tmp_path) as sh:
        assert sh.run(_PLANTS[plant].replace("{hook}", str(hook)), timeout=10).exit_code == 0
        r = sh.run("ls / >/dev/null && echo real-ls; env; false", timeout=10)
        assert r.exit_code == 1, r.output
        assert "PLANTED" not in r.output and "real-ls" in r.output
        for name in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "PS4", "IFS", "CDPATH", "GLOBIGNORE",
                     "POSIXLY_CORRECT", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_XTRACEFD",
                     "TMOUT", "SHELLOPTS", "BASHOPTS", "GITHUB_TOKEN", "BASH_FUNC_ls"):
            assert f"\n{name}=" not in "\n" + r.output and f"\n{name}%%=" not in "\n" + r.output, name
        assert sh.run("echo still-fine", timeout=10).output.strip() == "still-fine"


@live
def test_a_timed_out_command_carries_nothing(tmp_path):
    """S2 L2 M2: a command levain had to kill leaves the next one where the last finished one ended."""
    (tmp_path / "far").mkdir()
    with _shell(tmp_path) as sh:
        assert sh.run(f"cd {tmp_path}; export KEEP=1", timeout=10).exit_code == 0
        assert sh.run(f"cd {tmp_path}/far; export KEEP=2; sleep 30", timeout=1).timed_out
        r = sh.run('echo "$PWD $KEEP"', timeout=10)
        assert r.output.strip() in (f"{tmp_path} 1", f"{Path(tmp_path).resolve()} 1")
        # levain's own discard, apart from the runner's: a frame the command wrote back itself (fd 87)
        # before it hung, or before a signal ended it, is not adopted either.
        frame = f"printf 'P%s\\0EKEEP=3\\0Z\\0' {tmp_path}/far >&87"
        assert sh.run(f"{frame}; sleep 30", timeout=1).timed_out
        if _SYSTEM == "Darwin":   # bash as pid 1 of bwrap's namespace cannot SIGKILL itself
            assert sh.run(f"{frame}; kill -9 $$", timeout=10).exit_code != 0
        r = sh.run('echo "$PWD $KEEP"', timeout=10)
        assert r.output.strip() in (f"{tmp_path} 1", f"{Path(tmp_path).resolve()} 1")
        # The trap writes last, so its frame wins over one the command wrote...
        assert sh.run(f"{frame}; exit 0", timeout=10).exit_code == 0
        assert sh.run('echo "$KEEP"', timeout=10).output.strip() == "1"
        # ...and with no trap (`exec` replaced bash) the command's own frame is adopted: data, which
        # can only set its own next directory and environment.
        assert sh.run(f"{frame}; exec true", timeout=10).exit_code == 0
        assert sh.run('echo "$KEEP"', timeout=10).output.strip() == "3"


@live
@pytest.mark.parametrize("command,status", [
    ("set -eu; echo $u_never_set", 1),
    ("set -e; echo ${x_never_set?boom}", 1),
    ("set -e; eval 'echo \"unterminated'", 1),
    ("set -e; true", 0),
    ("set -e; exit 0", 0),
    ("set -e; false || exit 0", 0),
    ("set -e; exit 4", 4),
])
def test_an_errexit_failure_is_not_reported_as_success(tmp_path, command, status):
    """S2 L2 H2: bash 3.2 exits 0 from an errexit failure when an EXIT trap is set, which the runner
    always has. The runner turns that case into exit 1; a real exit 0 stays 0. (bash 5 on Linux has
    no such bug: the same rows hold with bash's own statuses, which are non-zero for the failures.)"""
    with _shell(tmp_path) as sh:
        r = sh.run(command, timeout=10)
        if status == 0:
            assert r.exit_code == 0, r.output
        elif status == 1:
            assert r.exit_code not in (None, 0), r.output
        else:
            assert r.exit_code == status


@live
def test_a_signal_under_errexit_is_still_a_signal(tmp_path):
    """The H2 fix must not turn a signal into exit 1: the signal traps mark it."""
    with _shell(tmp_path) as sh:
        r = sh.run("set -e; kill -TERM $$; sleep 5", timeout=10)
        if _SYSTEM == "Darwin":
            assert (r.exit_code, r.signal) == (None, signal.SIGTERM), r
        else:   # bash is pid 1 of its namespace and cannot signal itself: the fallback exit
            assert r.exit_code == 143, r


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
def test_close_leaves_nothing_running(tmp_path):
    marker = _marker()
    sh = _shell(tmp_path)
    sh.run(f"sleep {marker} > /dev/null 2>&1 &", timeout=10)
    sh.close()
    time.sleep(0.3)
    leaked = _pids_with(f"sleep {marker}")
    _kill_all(leaked)
    assert not leaked
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


# --- hermetic: the runner on plain bash, no sandbox ---------------------------------------------


def _plain(tmp_path: Path, cls=SandboxedShell) -> SandboxedShell:
    if _SYSTEM == "Linux":
        pytest.skip("on Linux only a shell with a pid namespace per command runs (bwrap, S2 L3 r5)")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return cls(argv=["/bin/bash", "--noprofile", "--norc"], cwd=ws,
               env={"PATH": "/usr/bin:/bin", "SSH_AUTH_SOCK": "/tmp/agent.sock"})


def test_a_refused_spawn_runs_none_of_the_command(tmp_path):
    """S2 L2 M3: when the post-spawn hook refuses (the bwrap claim could not be recorded), bash has
    not been given the command yet, so none of it ran."""
    marker = tmp_path / "ran"

    class Refusing(SandboxedShell):
        refuse = False

        def _after_spawn(self, pgid):
            if self.refuse:
                time.sleep(0.5)   # a command fed early would have run by now
                raise ConfinementError("claim not recorded")

    sh = _plain(tmp_path, Refusing).start()
    try:
        sh.refuse = True
        with pytest.raises(ConfinementError):
            sh.run(f"touch {marker}", timeout=10)
        assert not marker.exists()
    finally:
        sh.close()


def test_background_pipes_are_capped(tmp_path):
    """S2 L2 M4: each command whose pipe a background job holds keeps a read end; past _MAX_LATE the
    oldest is closed, so background jobs cannot exhaust levain's file descriptors."""
    from levain.firing.confinement import _MAX_LATE

    marker = _marker()
    sh = _plain(tmp_path).start()
    try:
        outs = []
        for _ in range(_MAX_LATE + 5):
            assert sh.run(f"sleep {marker} &", timeout=10).exit_code == 0
            outs.extend(o for o in sh._late if o not in outs)   # type: ignore[attr-defined]
        assert len(sh._late) <= _MAX_LATE   # type: ignore[attr-defined]
        dropped = [o for o in outs if o not in sh._late]   # type: ignore[attr-defined]
        assert len(dropped) >= 5
        assert all(o.eof.wait(2.0) for o in dropped), "an abandoned pipe's read end was not closed"
    finally:
        sh.close()
        _kill_all(_pids_with(f"sleep {marker}"))


def test_a_nul_byte_in_a_command_is_refused(tmp_path):
    """S2 L2 L6: bash reads the command up to a NUL, so the rest would be silently dropped."""
    with _plain(tmp_path) as sh:
        with pytest.raises(ConfinementError, match="NUL"):
            sh.run("echo a\0rm -rf x", timeout=10)
        assert sh.run("echo fine", timeout=10).output.strip() == "fine"


def test_close_does_not_signal_a_group_that_already_emptied(tmp_path):
    """S2 L2 L7: a finished command's group number can be reused by an unrelated group, so close()
    prunes emptied groups before it signals."""
    started: list[int] = []

    class Recording(SandboxedShell):
        def _after_spawn(self, pgid):
            started.append(pgid)

    sh = _plain(tmp_path, Recording).start()
    sh.run("true", timeout=10)
    finished = set(started)
    assert not sh._groups   # type: ignore[attr-defined]  # emptied: reaped and forgotten at once
    sent: list[int] = []
    sh._signal = lambda pgid, sig: sent.append(pgid)   # type: ignore[method-assign]
    sh.close()
    assert finished and not (finished & set(sent))


def test_the_carried_environment_is_data_with_a_denylist(tmp_path):
    """The frame a command writes back is parsed as data: denylisted and credential-shaped names the
    entity introduced are dropped, a name levain started bash with stays, and a malformed frame
    carries nothing."""
    sh = _plain(tmp_path)
    frame = (b"P/x\0O/y\0EKEEP=1\0EBASH_ENV=/h\0ELD_PRELOAD=/l\0EDYLD_X=1\0EGITHUB_TOKEN=t\0"
             b"ESSH_AUTH_SOCK=/new\0Ebad name=1\0EPATH=/bin\0")
    sh._carry(frame)   # type: ignore[attr-defined]
    assert sh._carried_cwd == "/x" and sh._carried_oldpwd == "/y"   # type: ignore[attr-defined]
    assert sh._carried_env == {"KEEP": "1", "SSH_AUTH_SOCK": "/new", "PATH": "/bin"}   # type: ignore[attr-defined]
    sh._carry(b"junk\0Z\0")   # type: ignore[attr-defined]
    assert sh._carried_cwd == "/x"   # type: ignore[attr-defined]


# --- the Seatbelt profile is never a file --------------------------------------------------------


@pytest.mark.skipif(not (_SYSTEM == "Darwin" and _LIVE), reason="macOS Seatbelt only")
def test_the_seatbelt_profile_is_not_a_file_the_entity_could_rewrite(tmp_path):
    """S2 L2 C1: each command is its own driver process, so a profile FILE would be read again at
    every command, and the entity could rewrite it between two commands. The profile goes on argv as
    text; no argument of the driver names a file, and a jewel stays denied across commands."""
    from levain.firing.confinement import SANDBOX_EXEC, SeatbeltProvider

    secret = tmp_path / "creds.env"
    secret.write_text("SECRET=do-not-leak")
    policy = build_policy(_entity(tmp_path), deny_files=(secret,))
    with select_provider().spawn_shell(policy) as sh:
        argv = sh._argv   # type: ignore[attr-defined]
        assert "-f" not in argv
        assert argv[argv.index("-p") + 1] == SeatbeltProvider().render_profile(sh.effective_policy)
        assert not [a for a in argv if a.startswith("/") and Path(a).is_file()
                    and a not in ("/bin/bash", SANDBOX_EXEC)]
        for _ in range(2):
            r = sh.run(f"cat {secret} 2>&1", timeout=10)
            assert r.exit_code != 0 and "do-not-leak" not in r.output


# --- S2 review round 2 (L1b + L2b on c262aae) ------------------------------------------------------


_IS_BASH3 = subprocess.run(["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True,
                           text=True).stdout.strip() == "3"


@pytest.mark.parametrize("command,status", [
    ("set -e; \\exit 0", 0),
    ("set -e; 'exit' 0", 0),
    ('set -e; "exit" 0', 0),
    ("set -e; e=exit; $e 0", 0),
    ("set -e; X=1 exit 0", 0),
    ("set -e; command exit 0", 0),
    ("set -e; true; exit", 0),
    ("set -e; (exit 3) || true; exit", 0),
    ("set -e; \\exit 6", 6),
    ("set -eu; echo $u_never_set", 1),
])
def test_an_exit_spelled_any_way_reports_its_own_status(tmp_path, command, status):
    """L1b LOW-1 / L2b L1: the errexit correction recognised only a literal ``exit``, so on bash 3.2 a
    real ``exit 0`` spelled ``\\exit 0``, ``'exit' 0``, ``$e 0`` or ``X=1 exit 0`` was reported as 1."""
    with _plain(tmp_path) as sh:
        r = sh.run(command, timeout=10)
        if status == 1:
            assert r.exit_code not in (None, 0), r
        else:
            assert r.exit_code == status, r


def test_the_runners_own_names_never_carry(tmp_path):
    """L1b LOW-2 / L2b M1: an exported ``__levain_done`` turned the errexit correction off in every
    later command, and ``__levain_g`` froze the carried state."""
    (tmp_path / "far").mkdir()
    with _plain(tmp_path) as sh:
        assert sh.run("export __levain_done=0 __levain_g=1 __levain_x=1 __levain_b=", timeout=10).exit_code == 0
        assert "__levain" not in sh.run("env", timeout=10).output
        r = sh.run("set -eu; echo $u_never_set", timeout=10)
        assert r.exit_code not in (None, 0), r
        assert sh.run(f"cd {tmp_path}/far; export K=2", timeout=10).exit_code == 0
        assert sh.run('echo "$PWD $K"', timeout=10).output.strip() in (
            f"{tmp_path}/far 2", f"{Path(tmp_path).resolve()}/far 2")
    assert not any(k.startswith("__levain") for k in sh._carried_env)   # type: ignore[attr-defined]
    # The runner's own guard, apart from levain's filter: a ``__levain_*`` name that reaches its input
    # anyway is never exported into the command.
    with _plain(tmp_path) as sh:
        sh._carried_env.update(__levain_done="0", __levain_g="1", __levain_zz="1")   # type: ignore[attr-defined]
        assert "__levain" not in sh.run("env", timeout=10).output
        sh._carried_env.update(__levain_done="0", __levain_g="1", __levain_zz="1")   # type: ignore[attr-defined]
        r = sh.run("set -eu; echo $u_never_set", timeout=10)
        assert r.exit_code not in (None, 0), r


@pytest.mark.parametrize("fd", ["9", "87"])
def test_a_lockfile_on_a_low_or_the_state_fd_never_receives_the_environment(tmp_path, fd):
    """L1b MED-1: after ``exec 9>lockfile`` (the usual flock idiom) the EXIT trap wrote every exported
    variable into the lockfile, and the carried state was lost. The trap writes only to the socket
    levain handed it; a command that puts a file on that fd gets nothing written into it."""
    lock = tmp_path / "lock"
    (tmp_path / "far").mkdir()
    with _plain(tmp_path) as sh:
        r = sh.run(f"export LOCKVAL=s3cr3t; cd {tmp_path}/far; exec {fd}>&-; exec {fd}>{lock}; echo locked", timeout=10)
        assert r.exit_code == 0, r
        assert "s3cr3t" not in lock.read_text()
        if fd == "9":   # the flock idiom's fd is not the state channel: the state still carries
            assert sh.run('echo "$LOCKVAL"', timeout=10).output.strip() == "s3cr3t"


@pytest.mark.skipif(_SYSTEM != "Darwin", reason="under bwrap an interrupt ends the sandbox itself")
def test_ctrl_c_handled_by_the_foreground_program_does_not_abort_the_command(tmp_path):
    """L2b L2: an INT trap in the runner ran after the foreground program handled Ctrl-C and killed
    the whole command; a shell lets the command go on when its child did not die of SIGINT."""
    import threading

    with _plain(tmp_path) as sh:
        threading.Timer(0.7, sh.interrupt).start()
        r = sh.run("/bin/bash -c \"trap '' INT; sleep 1.5\"; echo after", timeout=20)
        assert (r.exit_code, r.signal) == (0, None), r
        assert "after" in r.output
        threading.Timer(0.7, sh.interrupt).start()
        r = sh.run("sleep 5; echo never", timeout=20)
        assert r.signal == signal.SIGINT and "never" not in r.output, r


def test_stopping_the_state_channel_keeps_a_frame_already_sent(tmp_path):
    """L2b L5: when a background job holds the state fd, levain stops the channel after the grace;
    on macOS a shutdown() threw away what the reader had not read yet, the trap's frame included."""
    import socket
    import threading

    from levain.firing import confinement as C

    gate = threading.Event()
    real = C._Carry._pump

    def slow(self):
        gate.wait(5)
        real(self)

    ours, theirs = socket.socketpair()
    try:
        orig, C._Carry._pump = C._Carry._pump, slow
        try:
            carry = C._Carry(ours)
        finally:
            C._Carry._pump = orig
        theirs.sendall(b"P/x\0EK=1\0Z\0")   # the trap's frame; `theirs` stays open (a background job)
        time.sleep(0.1)
        carry.stop()
        gate.set()
        assert carry.done.wait(5)
        assert carry.frame == b"P/x\0EK=1\0"
    finally:
        theirs.close()


def test_a_failed_spawn_argv_leaks_no_descriptor(tmp_path):
    """L2b L6: the pipe and socket were made before ``_spawn_argv`` ran, and leaked if it raised."""

    class Failing(SandboxedShell):
        fail = False

        def _spawn_argv(self):
            if self.fail:
                raise ConfinementError("no argv")
            return super()._spawn_argv()

    sh = _plain(tmp_path, Failing).start()
    try:
        sh.fail = True
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            with pytest.raises(ConfinementError):
                sh.run("true", timeout=10)
        assert len(os.listdir("/dev/fd")) <= before
    finally:
        sh.close()


def test_the_start_refuses_a_shell_whose_state_channel_is_broken(tmp_path):
    """L2b L3: a sudoers ``log_input`` puts a pipe on bash's stdin instead of levain's socket, so the
    runner's state frame has nowhere to go and nothing would carry, silently. The start probe requires
    the frame back. Simulated by a driver that relays its stdin to bash through a pipe."""

    class Piped(SandboxedShell):
        def _spawn_argv(self):
            argv, fds = super()._spawn_argv()
            runner = argv[argv.index("-c") + 1]
            relay = 'cat | /bin/bash --noprofile --norc -c "$1" bash'
            return ["/bin/bash", "--noprofile", "--norc", "-c", relay, "sh", runner], fds

    sh = _plain(tmp_path, Piped)
    with pytest.raises(ConfinementError, match="state"):
        sh.start()
    sh.close()


def test_bwrap_child_pid_is_read_without_waiting_for_eof_and_owned_by_the_reader(tmp_path, monkeypatch):
    """S2 L1b (not checked on Linux) and L2b L7: the --info-fd read waited for EOF, which never comes
    while another process holds the write end (each command would stall to the 20 s deadline), and
    close() from another thread could close the fd under the read."""
    import select as _select

    from levain.firing import confinement as C

    sh = object.__new__(C._BwrapShell)
    import threading

    sh._lock = threading.Lock()
    rd, wr = os.pipe()
    os.write(wr, b'{\n    "child-pid": 4242\n}\n')   # bwrap's whole object; `wr` stays open
    sh._info_r = rd
    real_select = _select.select
    calls = []

    def racing_select(r, w, x, t):
        if not calls:
            sh._close_info()   # a close() from another thread, mid-read
        calls.append(1)
        return real_select(r, w, x, t)

    monkeypatch.setattr(C.select, "select", racing_select)
    t0 = time.monotonic()
    try:
        assert sh._read_child_pid() == 4242
        assert time.monotonic() - t0 < 5
    finally:
        os.close(wr)


def test_a_close_during_a_spawn_runs_none_of_the_command(tmp_path):
    """S2 L3 r1 (codex HIGH): run() checked `_closed` without the lock and registered its group later,
    so a close() in between left a command running with nobody to kill it."""
    marker = tmp_path / "ran"

    class Racing(SandboxedShell):
        race = False

        def _spawn_argv(self):
            if self.race:
                self.close()   # what another thread's close() does between run()'s check and the spawn
            return super()._spawn_argv()

    sh = _plain(tmp_path, Racing).start()
    try:
        sh.race = True
        with pytest.raises(ConfinementError, match="closed"):
            sh.run(f"touch {marker}", timeout=10)
        time.sleep(0.3)
        assert not marker.exists() and not sh._groups   # type: ignore[attr-defined]
    finally:
        sh.close()


def test_builtin_dash_dash_exit_reports_its_own_status(tmp_path):
    """S2 L3 r2 (codex MED): `builtin -- exit 0` was reported as 1 on bash 3.2."""
    with _plain(tmp_path) as sh:
        assert sh.run("set -e; builtin -- exit 0", timeout=10).exit_code == 0
        assert sh.run("set -e; command -- exit 0", timeout=10).exit_code == 0


def test_a_command_that_can_enter_no_directory_does_not_run(tmp_path):
    """S2 L3 r2 (codex MED): when neither the carried directory nor the workspace could be entered,
    the command ran from `/`."""
    marker = tmp_path / "ran"
    sh = _plain(tmp_path).start()
    ws = tmp_path / "ws"
    try:
        sh._carried_cwd = str(tmp_path / "gone")   # type: ignore[attr-defined]
        os.chmod(ws, 0)
        r = sh.run(f"touch {marker}", timeout=10)
        assert r.exit_code == 126 and "did not run" in r.output, r
        assert not marker.exists()
    finally:
        os.chmod(ws, 0o755)
        sh.close()


def test_a_failed_socketpair_leaks_no_pipe(tmp_path, monkeypatch):
    """S2 L3 r2 (complement): the pipe made just before a failing socketpair() leaked."""
    import socket as _socket

    sh = _plain(tmp_path).start()
    try:
        def boom():
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(_socket, "socketpair", boom)
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            with pytest.raises(OSError):
                sh.run("true", timeout=10)
        assert len(os.listdir("/dev/fd")) <= before
    finally:
        monkeypatch.undo()
        sh.close()


def _stat_of(pid: int) -> str:
    """``ps``'s state letters for ``pid``, or "" once it is reaped (gone from the process table)."""
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


class _Recording(SandboxedShell):
    def _after_spawn(self, pgid):
        self.__dict__.setdefault("started", []).append(pgid)


def test_a_background_groups_leader_stays_unreaped_until_its_group_is_empty(tmp_path):
    """S2 L3 r2 (codex HIGH): a retained group is known by its number, and a number is reusable once
    the group's leader is reaped and its last member exits. The leader is held as a zombie until the
    group is empty (POSIX.1-2024 XBD 4.17), so a later kill can only reach this command's processes."""
    m = _marker()
    sh = _plain(tmp_path, _Recording).start()
    try:
        sh.run(f"sleep {m} >/dev/null 2>&1 &", timeout=10)
        pgid = sh.started[-1]   # type: ignore[attr-defined]
        assert _stat_of(pgid).startswith("Z"), "the leader was reaped while its group lives"
        assert pgid in sh._groups   # type: ignore[attr-defined]
        _kill_all(_pids_with(m))
        deadline = time.monotonic() + 5
        while _pids_with(m) and time.monotonic() < deadline:
            time.sleep(0.05)
        sh.run("true", timeout=10)
        assert _stat_of(pgid) == "" and pgid not in sh._groups   # type: ignore[attr-defined]
    finally:
        _kill_all(_pids_with(m))
        sh.close()


def test_close_keeps_a_group_it_could_not_empty_and_retries_it(tmp_path):
    """S2 L3 r2 (codex HIGH): close() used to forget a group its signals did not empty, and the
    session then released the workspace lock. Now the group stays held (leader unreaped), close() says
    which, and a later close() retries it."""
    from levain.firing import confinement as C

    m = _marker()
    sh = _plain(tmp_path, _Recording).start()
    try:
        sh.run(f"sleep {m} >/dev/null 2>&1 &", timeout=10)
        pgid = sh.started[-1]   # type: ignore[attr-defined]
        sh._signal = lambda pgid, sig: None   # type: ignore[method-assign]  # the signals do not land
        sh.close()
        assert sh.unemptied_groups == (pgid,)
        assert _pids_with(m) and _stat_of(pgid).startswith("Z")
        assert sh in C._UNEMPTIED_SHELLS
        del sh._signal                        # they land again
        sh.close()
        assert sh.unemptied_groups == () and not _pids_with(m) and _stat_of(pgid) == ""
        assert sh not in C._UNEMPTIED_SHELLS
    finally:
        _kill_all(_pids_with(m))
        sh.close()


def test_a_finished_commands_status_is_read_without_reaping_it(tmp_path):
    """The status comes from the watch on the leader (waitid WNOWAIT / kqueue NOTE_EXITSTATUS), so a
    leader held for its background job still reports its own status."""
    m = _marker()
    sh = _plain(tmp_path).start()
    try:
        r = sh.run(f"sleep {m} >/dev/null 2>&1 & exit 5", timeout=10)
        assert r.exit_code == 5 and not r.timed_out
        assert sh.run("kill -9 $$", timeout=10).signal == 9
    finally:
        _kill_all(_pids_with(m))
        sh.close()


def test_a_driver_that_exits_before_its_watch_still_reports_its_status(tmp_path, monkeypatch):
    """A leader that exited before levain's watch on it was set up (a driver that fails at once) is
    reaped for its status, after its group is emptied, and the start probe names that status."""
    from levain.firing import confinement as C

    if _SYSTEM == "Linux":
        pytest.skip("a base shell is refused on Linux (S2 L3 r5)")
    real_init = C._Leader.__init__

    def late(self, proc):
        proc_gone = time.monotonic() + 5
        while time.monotonic() < proc_gone and _stat_of(proc.pid)[:1] not in ("Z", ""):
            time.sleep(0.02)
        real_init(self, proc)   # registered only after the driver exited

    monkeypatch.setattr(C._Leader, "__init__", late)
    ws = tmp_path / "ws"
    ws.mkdir()
    sh = SandboxedShell(argv=["/bin/sh", "-c", "exit 9", "x"], cwd=ws, env={"PATH": "/usr/bin:/bin"})
    with pytest.raises(ConfinementError, match="exited 9"):
        sh.start()
    assert sh.unemptied_groups == ()


def test_close_from_another_thread_does_not_strand_the_running_command(tmp_path):
    """close() (a revoke, an interrupt teardown) runs on another thread while run() waits on the same
    leader. The leader's exit is seen by one waiter; the other must not then wait out its whole
    deadline, nor fail because the first reaped the leader under it."""
    sh = _plain(tmp_path).start()
    try:
        threading.Timer(0.5, sh.close).start()
        t0 = time.monotonic()
        try:
            sh.run("sleep 20", timeout=60)
        except ConfinementError:
            pass   # a closed shell may refuse the late result; it must not hang
        assert time.monotonic() - t0 < 15
    finally:
        sh.close()


def test_two_waiters_on_one_leader_both_see_its_exit_and_a_reap_under_one_is_not_an_error():
    """The kqueue delivers a process's exit once; a second waiter must still return promptly, and a
    waiter whose leader another thread reaped meanwhile returns instead of raising."""
    from levain.firing import confinement as C

    def run_waiters(leader, n, then=None):
        done: list[float] = []
        errors: list[BaseException] = []

        def waiter() -> None:
            try:
                assert leader.wait(30)
                done.append(time.monotonic())
            except BaseException as exc:   # noqa: BLE001
                errors.append(exc)

        t0 = time.monotonic()
        ts = [threading.Thread(target=waiter) for _ in range(n)]
        for t in ts:
            t.start()
        if then is not None:
            then()
        for t in ts:
            t.join(40)
        return errors, [d - t0 for d in done]

    leader = C._Leader(subprocess.Popen(["/bin/sleep", "0.5"], start_new_session=True))
    errors, took = run_waiters(leader, 2)
    assert not errors and len(took) == 2 and max(took) < 5, (errors, took)
    assert leader.status == 0
    leader.reap()

    # Another thread reaps it under the waiter (close() does, once its group is empty).
    leader2 = C._Leader(subprocess.Popen(["/bin/sleep", "0.5"], start_new_session=True))
    errors, took = run_waiters(leader2, 1, then=leader2.reap)
    assert not errors and len(took) == 1 and took[0] < 5, (errors, took)


def test_a_waiter_whose_leader_is_reaped_under_it_returns(monkeypatch):
    """The other thread's reap() closes the watch (the kqueue, or the pid stops being waitable) while
    this one is inside it: that ends the wait, it is not an error."""
    from levain.firing import confinement as C

    proc = subprocess.Popen(["/bin/sleep", "0.3"], start_new_session=True)
    leader = C._Leader(proc)

    def reaped_meanwhile(*_a, **_k):
        leader.exited = leader.reaped = True   # what reap() on another thread has done by now
        raise (ValueError("I/O operation on closed kqueue object") if kq is not None
               else ChildProcessError(10, "No child processes"))

    kq = leader._kq
    if kq is not None:
        class Closed:
            control = staticmethod(reaped_meanwhile)
        leader._kq = Closed()
    else:
        monkeypatch.setattr(C.os, "waitid", reaped_meanwhile)
    try:
        assert leader.wait(5) is True
    finally:
        if kq is not None:
            kq.close()
        proc.wait()


def test_a_timed_out_commands_group_is_forgotten_when_its_leader_is_reaped(tmp_path):
    """S2d codex HIGH: the timeout path reaped the leader but left its group in the shell's table, so
    a later close() could signal that number after the system reused it."""
    sh = _plain(tmp_path, _Recording).start()
    try:
        r = sh.run("sleep 30", timeout=0.5)
        assert r.timed_out
        pgid = sh.started[-1]   # type: ignore[attr-defined]
        assert pgid not in sh._groups   # type: ignore[attr-defined]
        sent: list[int] = []
        sh._signal = lambda pgid, sig: sent.append(pgid)   # type: ignore[method-assign]
        sh.close()
        assert pgid not in sent
    finally:
        sh.close()


def test_a_reaped_leaders_group_is_never_signalled(tmp_path):
    """The check that a leader is unreaped and the signal happen under the lock every reap holds."""
    from levain.firing import confinement as C

    sh = _plain(tmp_path).start()
    proc = subprocess.Popen(["/bin/sleep", "0.1"], start_new_session=True)
    leader = C._Leader(proc)
    leader.wait(5)
    leader.reap()
    sent: list[int] = []
    sh._signal = lambda pgid, sig: sent.append(pgid)   # type: ignore[method-assign]
    sh._signal_group(proc.pid, leader, signal.SIGKILL)   # type: ignore[attr-defined]
    sh._leader = leader   # type: ignore[attr-defined]
    sh.interrupt()
    assert not sent
    sh.close()


# --- S2 L3 r3 (input 1be91f8bdfc24a1b) ---------------------------------------------------------


def test_a_slow_signal_to_one_group_does_not_block_ctrl_c(tmp_path):
    """r3 complement MED: the unreaped-check-and-signal held the shell-wide lock across a hands
    signal (two sudo calls), so a Ctrl-C on another thread waited behind it. The guard is per leader."""
    from levain.firing import confinement as C

    sh = _plain(tmp_path).start()
    a = C._Leader(subprocess.Popen(["/bin/sleep", "5"], start_new_session=True))
    b = C._Leader(subprocess.Popen(["/bin/sleep", "5"], start_new_session=True))
    hit: list[int] = []

    def slow(pgid, sig):
        if pgid == a.pid:
            time.sleep(2)
        hit.append(pgid)

    sh._signal = slow   # type: ignore[method-assign]
    sh._leader = b   # type: ignore[attr-defined]
    try:
        t = threading.Thread(target=sh._signal_group, args=(a.pid, a, signal.SIGTERM))  # type: ignore[attr-defined]
        t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        sh.interrupt()
        assert time.monotonic() - t0 < 1.0 and b.pid in hit
        t.join(5)
    finally:
        sh._leader = None   # type: ignore[attr-defined]
        for lead in (a, b):
            lead.proc.kill()
            lead.reap()
        sh.close()


def test_the_exit_status_is_set_before_the_leader_reads_as_exited():
    """r3 complement LOW: a racing waiter could see `exited` with no status yet and take the
    driver-failed path for a command that ended normally."""
    from levain.firing import confinement as C

    seen: list[object] = []

    class Watched(C._Leader):
        def __setattr__(self, name, value):
            if name == "exited" and value:
                seen.append(self.__dict__.get("status"))
            object.__setattr__(self, name, value)

    lead = Watched.__new__(Watched)
    object.__setattr__(lead, "status", None)
    object.__setattr__(lead, "exited", False)

    class Ev:
        filter = select.KQ_FILTER_PROC if hasattr(select, "KQ_FILTER_PROC") else -5
        fflags = select.KQ_NOTE_EXIT if hasattr(select, "KQ_NOTE_EXIT") else 0x80000000
        data = 3 << 8

    if not hasattr(select, "KQ_FILTER_PROC"):
        pytest.skip("kqueue (macOS) only")
    lead._take([Ev()])
    assert seen == [3]


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="kqueue (macOS) only")
def test_a_watch_that_cannot_be_set_for_a_live_process_is_an_error_not_an_exit(monkeypatch):
    """r3 complement LOW: any kqueue registration error read as "already exited", so a running
    command would be killed as a failed driver. Only ESRCH means exited."""
    import errno as _errno

    from levain.firing import confinement as C

    class Ev:
        flags = select.KQ_EV_ERROR
        data = _errno.EACCES

    class FakeKq:
        def control(self, *a):
            return [Ev()]

        def close(self):
            pass

    proc = subprocess.Popen(["/bin/sleep", "5"], start_new_session=True)
    try:
        monkeypatch.setattr(C.select, "kqueue", FakeKq)
        # The kqueue registration itself: where Python has os.waitid (codex ran this on macOS 3.13.13,
        # r4 LOW) that branch is taken instead, so it is taken out here.
        monkeypatch.delattr(C.os, "waitid", raising=False)
        with pytest.raises(OSError):
            C._Leader(proc)
    finally:
        proc.kill()
        proc.wait()


# --- S2 L3 r4 (codex, complement on 0cb85b7..f942ee1) ------------------------------------------


def test_no_lock_is_held_across_a_signal_and_no_reap_runs_during_one(tmp_path):
    """r4 codex MED + complement MED: the per-leader reap lock was held across the signal (two sudo
    calls for a hands shell), so a Ctrl-C of that same command, and its reap, waited behind it. The
    signal is now sent outside the lock; a reap waits for it, so the number is never freed mid-send."""
    from levain.firing import confinement as C

    sh = _plain(tmp_path)
    lead = C._Leader(subprocess.Popen(["/bin/sleep", "5"], start_new_session=True))
    stalled, go = threading.Event(), threading.Event()
    hit: list[int] = []

    def slow(pgid, sig):
        if sig == signal.SIGTERM:
            stalled.set()
            go.wait(5)   # a sudo that has not answered yet
        hit.append(sig)

    sh._signal = slow   # type: ignore[method-assign]
    sh._leader = lead   # type: ignore[attr-defined]
    t = threading.Thread(target=sh._signal_group, args=(lead.pid, lead, signal.SIGTERM))  # type: ignore[attr-defined]
    reaper = threading.Thread(target=lead.reap)
    try:
        t.start()
        assert stalled.wait(5)
        t0 = time.monotonic()
        sh.interrupt()   # the same command's Ctrl-C
        assert time.monotonic() - t0 < 1.0 and signal.SIGINT in hit
        lead.proc.kill()
        lead.wait(5)
        reaper.start()
        reaper.join(0.3)
        assert reaper.is_alive() and not lead.reaped   # held unreaped while the signal is out
    finally:
        go.set()
        t.join(5)
        if reaper.is_alive():
            reaper.join(5)
        sh._leader = None   # type: ignore[attr-defined]
        lead.proc.kill()
        lead.reap()
    assert lead.reaped


# --- S2 L3 r5 (codex, glm, complement on f942ee1..e0a86c7) -------------------------------------


@pytest.mark.skipif(not (_SYSTEM == "Linux" and _LIVE), reason="bwrap's pid namespace, Linux only")
def test_a_linux_shell_reaps_each_commands_leader_once_its_namespace_is_gone(tmp_path):
    """r5 codex HIGH + glm HIGH: r4 held every Linux command's leader, a zombie, until close(), so a
    long session ran into RLIMIT_NPROC. Each command runs in a pid namespace of its own, and its
    init gone means the kernel has killed the rest (pid_namespaces(7)): the leader is reaped then."""
    sh = _shell(tmp_path)
    try:
        for _ in range(20):
            assert sh.run("true", timeout=10).exit_code == 0
        zombies = []
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    f = Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[-1].split()
                except OSError:
                    continue
                if f[0] == "Z" and f[1] == str(os.getpid()):
                    zombies.append(d)
        assert len(sh._groups) == 0 and zombies == []   # type: ignore[attr-defined]
    finally:
        sh.close()


def test_an_undelivered_sigkill_or_a_signal_still_out_keeps_the_group(tmp_path):
    """r5 complement MED 2 + 3: a SIGKILL whose delivery was not confirmed (sudo timed out after the
    kill ran) let the group be called empty and its leader reaped; and a reap waited without end on a
    signal still out. Both now leave the leader unreaped and the group kept."""
    from levain.firing import confinement as C

    sh = _plain(tmp_path)
    lead = C._Leader(subprocess.Popen(["/bin/sleep", "5"], start_new_session=True))

    def unconfirmed(pgid, sig):
        if sig == signal.SIGKILL:
            os.killpg(pgid, signal.SIGKILL)   # it landed, but the answer did not come back
            return False
        return True

    sh._signal = unconfirmed   # type: ignore[method-assign]
    sh._groups[lead.pid] = lead   # type: ignore[attr-defined]
    try:
        assert sh._kill_group(lead.pid, lead) is False   # type: ignore[attr-defined]
        assert not lead.reaped and lead.pid in sh._groups   # type: ignore[attr-defined]
        assert lead.hold_for_signal()
        t0 = time.monotonic()
        assert lead.reap(timeout=0.2) is False and not lead.reaped
        assert time.monotonic() - t0 < 1.0
        lead.signal_sent()
    finally:
        lead.proc.kill()
        lead.reap()


# --- S2 L3 r6 (codex, complement on e0a86c7..99424c4) ------------------------------------------


class _ExitedLeader:
    """A bwrap leader that has exited (unreaped), for the bwrap shell's bookkeeping alone."""

    def __init__(self, status: int = 0) -> None:
        self.pid, self.status, self.exited, self.reaped = 777, status, True, False

    def wait(self, timeout):
        return True

    def reap(self, timeout=None):
        self.reaped = True
        return True

    def hold_for_signal(self):
        return False   # never signal a number this test does not own

    def signal_sent(self):
        pass

    def release_watch(self):
        pass


def _bwrap_books(tmp_path):
    from levain.firing import confinement as C

    return C._BwrapShell(policy=None, manifest={}, argv=["/bin/true"], cwd=tmp_path, env={})  # type: ignore[arg-type]


def test_a_bash_record_belongs_to_its_leader_not_to_a_reused_group_number(tmp_path, monkeypatch):
    """r6 codex HIGH: a gone bash's record stayed under its group NUMBER, so a later bwrap that got
    the same number after pid wraparound was judged empty by it, and reaped and forgotten while its
    namespace could live. A record is now its leader's, and is deleted when that leader is reaped."""
    from levain.firing import confinement as C

    sh = _bwrap_books(tmp_path)
    monkeypatch.setattr(C, "_proc_start_time", lambda pid: "1")
    monkeypatch.setattr(C, "_bash_gone", lambda pid, start, timeout: True)
    sh._read_child_pid = lambda: 4242   # type: ignore[method-assign]
    first, later = _ExitedLeader(), _ExitedLeader()
    sh._groups[777] = first   # type: ignore[attr-defined]
    sh._after_spawn(777)   # type: ignore[attr-defined]
    assert sh._group_emptied(777, first, 0.0) and sh._reap(777, first)   # type: ignore[attr-defined]
    sh._groups[777] = later   # type: ignore[attr-defined]  # the number again, its bash not yet reported
    assert sh._group_emptied(777, later, 0.0) is False   # type: ignore[attr-defined]


def test_a_bwrap_that_started_no_bash_is_reaped_and_an_unreported_one_keeps_the_claim(tmp_path, monkeypatch):
    """r6 MED (codex + complement): when bwrap reported no bash, its exited leader was never reaped
    and the claim never released. r7: a bwrap that exited without a pid is NOT known to have started
    none (it may have cloned its namespace init), so it is unverified like one interrupted before it
    answered: its leader is reapable, never empty, and the claim is kept."""
    from levain.firing import confinement as C

    sh = _bwrap_books(tmp_path)
    failed = _ExitedLeader(status=1)
    sh._groups[777] = failed   # type: ignore[attr-defined]
    r, w = os.pipe()
    os.close(w)                # bwrap exited, closing its --info-fd without writing a pid
    sh._info_r = r   # type: ignore[attr-defined]
    with pytest.raises(ConfinementError):
        sh._after_spawn(777)   # type: ignore[attr-defined]
    # r7: no longer "bwrap started none": a no-pid spawn is unverified, so reapable and never empty.
    assert (not sh._group_emptied(777, failed, 0.0)   # type: ignore[attr-defined]
            and sh._reapable(777, failed, 0.0) and sh._reap(777, failed))   # type: ignore[attr-defined]

    sh2 = _bwrap_books(tmp_path)
    cut = _ExitedLeader()
    sh2._groups[778] = cut   # type: ignore[attr-defined]

    def interrupted():
        raise KeyboardInterrupt

    sh2._read_child_pid = interrupted   # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        sh2._after_spawn(778)   # type: ignore[attr-defined]
    released: list[str] = []
    monkeypatch.setattr(C, "_ledger_release", released.append)
    sh2._ledger_claim = "c"   # type: ignore[attr-defined]
    sh2.close()
    assert cut.reaped and sh2.unemptied_groups == () and released == []


# --- S2 L3 r7 (codex, complement on 99424c4..285f402) ------------------------------------------


class _LiveSignalLeader(_ExitedLeader):
    """An exited bwrap leader whose group is still signalled (a cloned namespace init may live in it)."""

    def hold_for_signal(self):
        return True


def _eof_info_pipe(sh):
    r, w = os.pipe()
    os.close(w)   # bwrap exited and closed its --info-fd without writing a pid
    sh._info_r = r


def test_r7_a_no_pid_spawn_is_killed_as_a_group_and_left_unverified(tmp_path):
    """r7 codex MED: EOF on the info pipe proves no bash ran, not that no namespace init exists (bwrap
    clones it before it writes child-pid and releases it after), and a blocked pid-ns init ignores
    SIGTERM. Whatever the exit status, the group gets a SIGKILL and the leader goes to `_unverified`."""
    sh = _bwrap_books(tmp_path)
    lead = _LiveSignalLeader(status=1)   # exited by itself, EOF, and a still-live group member
    sent: list[tuple[int, int]] = []
    sh._signal = lambda pgid, sig: sent.append((pgid, sig)) or True   # type: ignore[method-assign]
    sh._groups[777] = lead   # type: ignore[attr-defined]
    _eof_info_pipe(sh)
    with pytest.raises(ConfinementError, match="closed its info pipe"):
        sh._after_spawn(777)   # type: ignore[attr-defined]
    assert (777, signal.SIGKILL) in sent
    assert [(g, ld) for g, ld in sh._unverified] == [(777, lead)]   # type: ignore[attr-defined]
    assert (777 not in sh._bashes)   # type: ignore[attr-defined]


def test_r7_an_unverified_shell_refuses_every_later_command_before_it_spawns(tmp_path, monkeypatch):
    """r7 codex + complement MED: the next command must not retag the claim away from the unverified
    namespace. `_spawn` refuses before any bwrap is started."""
    from levain.firing import confinement as C

    sh = C._BwrapShell(policy=None, manifest={}, argv=["/bin/true", "--as-pid-1"], cwd=tmp_path,  # type: ignore[arg-type]
                       env={})
    sh._unverified.append((777, _ExitedLeader()))   # type: ignore[attr-defined]

    def no_spawn(*a, **k):
        raise AssertionError("a bwrap was spawned on a poisoned shell")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    with pytest.raises(ConfinementError, match="could not be verified gone"):
        sh._spawn()   # type: ignore[attr-defined]
    assert len(sh._unverified) == 1   # type: ignore[attr-defined]


def test_r7_an_unverified_leader_is_reapable_but_never_reported_empty(tmp_path):
    """r7 complement MED: `_group_emptied` called an unverified leader empty once it exited. It is
    now reapable (`_reapable`) and not empty, so a SIGKILL gate still fires and a caller reading
    'empty' never takes it for 'nothing of this command runs'."""
    sh = _bwrap_books(tmp_path)
    lead = _LiveSignalLeader()
    sent: list[tuple[int, int]] = []
    sh._signal = lambda pgid, sig: sent.append((pgid, sig)) or True   # type: ignore[method-assign]
    sh._groups[777] = lead   # type: ignore[attr-defined]
    sh._unverified.append((777, lead))   # type: ignore[attr-defined]
    assert sh._group_emptied(777, lead, 0.0) is False   # type: ignore[attr-defined]
    assert sh._reapable(777, lead, 0.0) is True   # type: ignore[attr-defined]
    assert sh._kill_group(777, lead) is True   # type: ignore[attr-defined]  # reaped...
    assert (777, signal.SIGKILL) in sent                # ...but only after a SIGKILL
    assert lead.reaped and sh._unverified            # type: ignore[attr-defined]  # claim stays covered


def test_r7_a_pid_with_no_leader_is_not_silently_dropped(tmp_path):
    """r7 complement LOW: a pid was read but the group is not in `_groups`: refuse, do not fall through."""
    sh = _bwrap_books(tmp_path)
    sh._read_child_pid = lambda: 4242   # type: ignore[method-assign]
    with pytest.raises(ConfinementError, match="no longer"):
        sh._after_spawn(999)   # type: ignore[attr-defined]


def test_r7_bashes_gone_does_not_resurrect_a_reaped_record(tmp_path, monkeypatch):
    from levain.firing import confinement as C

    sh = _bwrap_books(tmp_path)
    lead = _ExitedLeader()
    sh._bashes[777] = (lead, (4243, "1"))   # type: ignore[attr-defined]

    def gone_and_reaped(pid, start, timeout):
        del sh._bashes[777]   # type: ignore[attr-defined]  # _reap ran meanwhile
        return True

    monkeypatch.setattr(C, "_bash_gone", gone_and_reaped)
    assert sh._bashes_gone(1.0) is True   # type: ignore[attr-defined]
    assert 777 not in sh._bashes   # type: ignore[attr-defined]
