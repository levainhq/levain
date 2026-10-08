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
    sh = _plain(tmp_path).start()
    sh.run("true", timeout=10)
    finished = set(sh._groups)   # type: ignore[attr-defined]
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
