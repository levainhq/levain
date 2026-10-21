"""S1 session 2 CI fix, lane FX7: main's child-environment invariant (``tests/test_launch.py``) on the slot package,
and the relay's end against the operator's typing.

Whatever a child is given sits in its exec-time environment block, readable by every process of that user: the root
helpers' entries hand :func:`main` only the names it reads, git's environment is the allowlist and never a copy, and
every child the supervisor runs gets an explicit environment. Once ``start``'s helper has exited the relay forwards
no more input, so the pty's echo of it never lands after ``start``'s line.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot import install_helper as IH
from levain.slot import runtime_helper as RH
from levain.slot import stage as ST
from levain.slot import supervisor as SV
from tests.test_slot_s2fx3_supervisor import rig
from tests.test_slot_s2s_supervisor import bounded, isolated_git  # noqa: F401 - autouse: CI has no ~/.gitconfig identity

REPO = Path(__file__).resolve().parents[1]
SECRETS = {"GH_TOKEN": "ghp_secret", "AWS_SECRET_ACCESS_KEY": "aws", "ANTHROPIC_API_KEY": "sk", "FOO_BAR": "x"}


# --- the root entries -------------------------------------------------------------------------------------------------

def test_entry_env_is_only_the_names_main_reads() -> None:
    environ = {**SECRETS, "SUDO_UID": "501", "TERM": "xterm-256color", "HOME": "/var/root", "PATH": "/usr/bin"}
    assert RH.entry_env(environ) == {"SUDO_UID": "501", "TERM": "xterm-256color"}
    assert RH.entry_env(environ, IH.INSTALL_ENTRY_ENV) == {"SUDO_UID": "501"}
    assert RH.entry_env({}) == {}


def _main_block_env(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    block = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    call = next(n for n in ast.walk(block) if isinstance(n, ast.Call) and ast.unparse(n.func) == "main")
    return ast.unparse(next(k.value for k in call.keywords if k.arg == "env"))


def test_root_entries_hand_main_the_entry_env() -> None:
    assert _main_block_env(REPO / "levain/slot/runtime_helper.py") == "entry_env(os.environ)"
    assert _main_block_env(REPO / "levain/slot/install_helper.py") == "RH.entry_env(os.environ, INSTALL_ENTRY_ENV)"


def test_runtime_helper_main_reads_no_name_outside_entry_env() -> None:
    """Every ``env.get`` / ``env[...]`` / ``ctx.env.get`` in the helpers names an ENTRY_ENV name, so the entry's
    narrowing can never starve main of one it reads."""
    for path, names in ((REPO / "levain/slot/runtime_helper.py", set(RH.ENTRY_ENV)),
                        (REPO / "levain/slot/install_helper.py", set(IH.INSTALL_ENTRY_ENV))):
        read = set()
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                    and ast.unparse(n.func.value) in ("env", "ctx.env") and n.args
                    and isinstance(n.args[0], ast.Constant)):
                read.add(n.args[0].value)
            if (isinstance(n, ast.Subscript) and ast.unparse(n.value) in ("env", "ctx.env")
                    and isinstance(n.slice, ast.Constant)):
                read.add(n.slice.value)
        assert read <= names, (path.name, read - names)


# --- git's environment ------------------------------------------------------------------------------------------------

def test_git_env_is_the_allowlist_plus_the_callers_config_location() -> None:
    base = {**SECRETS, "PATH": "/usr/bin", "HOME": "/Users/op", "LANG": "C", "TMPDIR": "/tmp/x",
            "GIT_DIR": "/elsewhere", "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=x'", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "x", "GIT_ASKPASS": "/bin/x",
            "GIT_CONFIG_GLOBAL": "/Users/op/.gc", "GIT_CONFIG_NOSYSTEM": "1", "LEVAIN_CARRY_FD": "7"}
    env = ST._git_env(base)
    assert env == {"PATH": "/usr/bin", "HOME": "/Users/op", "LANG": "C", "TMPDIR": "/tmp/x",
                   "GIT_CONFIG_GLOBAL": "/Users/op/.gc", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def test_git_env_default_is_this_process_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    env = ST._git_env()
    assert "GH_TOKEN" not in env and "GIT_DIR" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env.get("PATH") == os.environ.get("PATH")


# --- the supervisor's children ----------------------------------------------------------------------------------------

def test_seams_run_default_requires_env() -> None:
    assert SV.Seams(host="darwin").run is SV.run_child
    with pytest.raises(TypeError):
        SV.run_child([sys.executable, "-c", "pass"])           # type: ignore[call-arg]
    r = SV.run_child([sys.executable, "-c", "import os; print(sorted(os.environ))"], env={"A_B": "1"},
                     capture_output=True, text=True)
    assert "A_B" in r.stdout and "PATH" not in r.stdout


def test_spawn_with_no_env_gives_the_child_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    p = SV.spawn([sys.executable, "-c", "import os, sys; sys.stdout.write(' '.join(sorted(os.environ)))"],
                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, lease_fds=())
    out = p.communicate(timeout=30)[0].decode()
    names = set(out.split())
    assert not names & set(SECRETS), names
    assert "PATH" in names


# --- the relay forwards nothing once `start`'s helper has exited ------------------------------------------------------

@bounded(60)
def test_input_typed_after_the_helper_exited_is_not_echoed_after_its_line(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # No controlling terminal, so the helper's exit does not hang the pty up, and its slave stays cooked (echo on). At
    # the relay's first two sights of its exit the master holds bytes (the answer line, again), so the relay goes
    # round, and his typing is waiting: forwarded, the pty echoes it after `start`'s line, which then reads
    # start-result-unreadable.
    r = rig(tmp_path, monkeypatch)
    in_r, in_w = os.pipe()
    r.seams.stdin = in_r
    ptys: list = []
    real_openpty, real_spawn = r.seams.openpty, r.seams.spawn

    def openpty():                                   # type: ignore[no-untyped-def]
        ptys.append(real_openpty())
        return ptys[-1]

    def spawn(argv, **kw):                           # type: ignore[no-untyped-def]
        p = real_spawn(argv, **{**kw, "ctty": False})   # no hangup at its exit: the relay reads on
        if "start" in argv:
            real_poll, injected = p.poll, []

            def poll():                              # type: ignore[no-untyped-def]
                code = real_poll()
                if code is not None and len(injected) < 2:
                    os.write(ptys[-1][1], b'{"exit":3,"ok":true}\n')
                    if not injected:
                        os.write(in_w, b"typed after the end\n")
                    injected.append(1)
                return code
            p.poll = poll
        return p
    r.seams.openpty, r.seams.spawn = openpty, spawn
    try:
        assert r.run() == 0, r.stderr()
    finally:
        os.close(in_r)
        os.close(in_w)
    assert "claude exited 3" in r.stderr()
