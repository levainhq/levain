"""M2 S2: the file editor of an entity whose bash runs as its own user reads and writes AS that user.

The S2 L1b/L2b HIGH: in hands mode the editor still read files and listed directories as the operator,
anywhere outside the crown jewels, while its bash could not. Every editor read now goes through the
same data-only helper as its writes: one fixed zsh program, run as the hands user under the same
profile, confined to the hands workspace, never following a final symlink.

Hermetic: no hands user exists on a developer machine (setup needs root). The helper program itself
runs here as plain zsh as the current user (no sudo, no sandbox), which checks its protocol; the real
run as the hands user under the profile is in the VM / CI end-to-end."""
from __future__ import annotations

import os
import platform
import pwd
import stat
import subprocess
import time
from pathlib import Path

import pytest

from levain.firing import confinement
from levain.firing.confinement import (
    HANDS_ZSH,
    HandsFileRefused,
    HandsIdentity,
    SeatbeltProvider,
    _HANDS_FILE_HELPER,
    build_policy,
)

pytestmark = pytest.mark.skipif(not Path(HANDS_ZSH).exists(), reason="needs /bin/zsh")

_NOBODY = pwd.getpwnam("nobody")


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "hands-ws"
    ws.mkdir(exist_ok=True)
    return ws.resolve()


def _helper(ws: Path, op: str, path, data: bytes = b"", limit: int = 1 << 20):
    """The helper exactly as the provider runs it, minus sudo and the sandbox driver."""
    return subprocess.run([HANDS_ZSH, "-f", "-c", _HANDS_FILE_HELPER, "zsh", op, str(ws), str(path),
                           str(limit)], input=data, capture_output=True, timeout=30, cwd="/",
                          env={"PATH": "/usr/bin:/bin"})


# --- the helper program -------------------------------------------------------------------------


def test_read_returns_a_regular_files_bytes_and_refuses_everything_else(tmp_path):
    ws = _ws(tmp_path)
    (ws / "a.txt").write_bytes(b"one\x00two\n")
    (ws / "sub").mkdir()
    outside = tmp_path / "operator-secret.txt"
    outside.write_text("do-not-leak")
    os.symlink(outside, ws / "link")
    os.mkfifo(ws / "fifo")
    os.symlink(tmp_path, ws / "up")
    (ws / "big").write_bytes(b"x" * 2048)

    r = _helper(ws, "read", ws / "a.txt")
    assert (r.returncode, r.stdout) == (0, b"one\x00two\n")
    for name, why in [("link", b"symlink"), ("fifo", b"not a regular file"), ("sub", b"not a regular file")]:
        t0 = time.monotonic()
        r = _helper(ws, "read", ws / name)
        assert r.returncode == 3 and why in r.stderr, (name, r)
        assert time.monotonic() - t0 < 10 and r.stdout == b""
    assert _helper(ws, "read", ws / "nope").returncode == 2
    for escape in (outside, ws / "up" / "operator-secret.txt", f"{ws}/sub/../../operator-secret.txt"):
        r = _helper(ws, "read", escape)
        assert r.returncode == 3 and b"outside the workspace" in r.stderr and b"do-not-leak" not in r.stdout
    r = _helper(ws, "read", ws / "big", limit=1024)
    assert r.returncode == 3 and b"larger" in r.stderr
    assert _helper(ws, "read", ws).returncode == 3
    assert _helper(ws, "read", "relative/path").returncode == 3


def test_stat_is_an_lstat_inside_the_workspace(tmp_path):
    ws = _ws(tmp_path)
    (ws / "a.txt").write_text("abc")
    os.chmod(ws / "a.txt", 0o640)
    os.symlink("a.txt", ws / "link")
    fields = _helper(ws, "stat", ws / "a.txt").stdout.split()
    st = os.lstat(ws / "a.txt")
    assert [int(f) for f in fields[:7]] == [st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid,
                                            st.st_gid, 3]
    assert stat.S_ISLNK(int(_helper(ws, "stat", ws / "link").stdout.split()[0]))
    assert stat.S_ISDIR(int(_helper(ws, "stat", ws).stdout.split()[0]))
    assert _helper(ws, "stat", ws / "nope").returncode == 2
    assert _helper(ws, "stat", tmp_path).returncode == 3


def test_list_gives_one_level_and_the_non_hidden_children_of_real_subdirectories(tmp_path):
    ws = _ws(tmp_path)
    (ws / "f").write_text("")
    (ws / ".hidden").mkdir()
    (ws / ".hidden" / "x").write_text("")
    (ws / "d").mkdir()
    (ws / "d" / "c").write_text("")
    (ws / "d" / ".dot").write_text("")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "secret").write_text("")
    os.symlink(tmp_path / "out", ws / "ln")
    r = _helper(ws, "list", ws)
    assert r.returncode == 0, r
    parts = r.stdout.split(b"\0")[:-1]
    got = sorted(zip(parts[::2], parts[1::2]))
    assert got == sorted([(b"f", b"f"), (b"d", b".hidden"), (b"d", b"d"), (b"f", b"d/c"), (b"l", b"ln")])
    assert _helper(ws, "list", ws / "ln").returncode == 3
    assert _helper(ws, "list", tmp_path / "out").returncode == 3
    assert _helper(ws, "list", ws / "f").returncode == 3
    assert _helper(ws, "list", ws / "gone").returncode == 2


def test_write_is_atomic_keeps_the_mode_and_never_writes_through_a_link(tmp_path):
    ws = _ws(tmp_path)
    assert _helper(ws, "write", ws / "new.txt", b"hello").returncode == 0
    assert (ws / "new.txt").read_bytes() == b"hello"
    assert stat.S_IMODE((ws / "new.txt").stat().st_mode) == 0o644
    (ws / "priv").write_text("old")
    os.chmod(ws / "priv", 0o600)
    assert _helper(ws, "write", ws / "priv", b"new").returncode == 0
    assert (ws / "priv").read_text() == "new" and stat.S_IMODE((ws / "priv").stat().st_mode) == 0o600
    outside = tmp_path / "operator-file"
    outside.write_text("untouched")
    os.symlink(outside, ws / "link")
    assert _helper(ws, "write", ws / "link", b"replaced").returncode == 0
    assert outside.read_text() == "untouched" and not (ws / "link").is_symlink()
    os.mkfifo(ws / "fifo")
    t0 = time.monotonic()
    assert _helper(ws, "write", ws / "fifo", b"data").returncode == 0
    assert time.monotonic() - t0 < 10 and (ws / "fifo").read_bytes() == b"data"
    (ws / "dir").mkdir()
    assert _helper(ws, "write", ws / "dir", b"x").returncode == 3
    assert _helper(ws, "write", ws / "missing" / "x", b"x").returncode == 2
    assert _helper(ws, "write", outside, b"x").returncode == 3 and outside.read_text() == "untouched"
    assert not [p for p in ws.iterdir() if ".levain-" in p.name]


# --- the provider: one fixed program, as the hands user, under the profile ----------------------------


def _hands(tmp_path: Path) -> HandsIdentity:
    return HandsIdentity("nobody", _NOBODY.pw_uid, _NOBODY.pw_dir, _ws(tmp_path))


def _entity(tmp_path: Path) -> Path:
    d = tmp_path / "coyote"
    (d / ".levain").mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.parametrize("op", ["read", "stat", "list", "write"])
def test_the_provider_runs_the_fixed_helper_as_the_hands_user_under_the_profile(tmp_path, monkeypatch, op):
    hands = _hands(tmp_path)
    seen: dict = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return subprocess.CompletedProcess(argv, 0, b"out", b"")

    monkeypatch.setattr(confinement.subprocess, "run", fake_run)
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    target = str(hands.workspace / "a.txt")
    assert SeatbeltProvider().hands_file(policy, hands, op, target, b"$(rm -rf ~)") == b"out"
    argv = seen["argv"]
    assert argv[:6] == [confinement.SUDO, "-n", "-u", "nobody", "/usr/bin/env", "-i"]
    at = argv.index(confinement.SANDBOX_EXEC)
    assert argv[at + 1] == "-p" and argv[at + 2] == SeatbeltProvider().render_profile(policy)
    assert argv[at + 3:] == [HANDS_ZSH, "-f", "-c", _HANDS_FILE_HELPER, "zsh", op, str(hands.workspace),
                             target, str(confinement._HANDS_READ_LIMIT)]
    assert seen["kw"]["start_new_session"] is True and seen["kw"]["cwd"] == "/"
    assert seen["kw"]["input"] == (b"$(rm -rf ~)" if op == "write" else b"")


def test_the_providers_answers_map_to_the_helpers_statuses(tmp_path, monkeypatch):
    hands = _hands(tmp_path)
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    for rc, err, exc in [(2, b"", FileNotFoundError), (3, b"x is a symlink", HandsFileRefused),
                         (1, b"sudo: a password is required", OSError)]:
        monkeypatch.setattr(confinement.subprocess, "run",
                            lambda argv, rc=rc, err=err, **kw: subprocess.CompletedProcess(argv, rc, b"", err))
        with pytest.raises(exc) as got:
            SeatbeltProvider().hands_file(policy, hands, "read", str(hands.workspace / "x"))
        if rc == 1:
            assert not isinstance(got.value, (FileNotFoundError, HandsFileRefused))
            assert "password is required" in str(got.value)


# --- the editor ---------------------------------------------------------------------------------------


openhands = pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")


class _LocalHands:
    """A provider whose hands_file runs the real helper as the current user (no sudo, no sandbox)."""

    def __init__(self, ws: Path) -> None:
        self.ws = ws
        self.calls: list[tuple[str, str]] = []

    def hands_file(self, policy, h, op, path, data=b"", *, timeout=60.0):
        self.calls.append((op, path))
        r = _helper(self.ws, op, path, data)
        if r.returncode == 2:
            raise FileNotFoundError(2, "No such file or directory", path)
        if r.returncode == 3:
            raise HandsFileRefused(r.stderr.decode())
        if r.returncode:
            raise OSError(r.stderr.decode())
        return r.stdout

    def hands_write(self, policy, h, path, data, *, timeout=120.0):
        self.hands_file(policy, h, "write", path, data)


def _editor(tmp_path, monkeypatch, provider=None):
    from levain.firing.openhands import tools as T

    hands = _hands(tmp_path)
    provider = provider or _LocalHands(hands.workspace)
    monkeypatch.setattr(T, "select_provider", lambda: provider)
    ed = T.CrownJewelsFileEditorExecutor(
        floor=T._SharedFloor(build_policy(_entity(tmp_path), workspace=hands.workspace), hands))
    return T, hands, provider, ed


def _no_operator_access(T, monkeypatch, *paths: Path) -> None:
    """Fail the test if levain itself opens or lists one of ``paths`` (reading its metadata inside
    the workspace is the operator's under ruling A; its content is not)."""
    names = {str(p) for p in paths} | {str(p.resolve()) for p in paths}
    real_open, real_osopen = T.builtins.open, os.open

    def guarded(fn, label):
        def g(target, *a, **k):
            if str(target) in names:
                pytest.fail(f"the operator {label} {target}")
            return fn(target, *a, **k)
        return g

    monkeypatch.setattr(T.builtins, "open", guarded(real_open, "opened"))
    # No directory is listed in levain's own process at all: the hands user lists them.
    monkeypatch.setattr(os, "scandir", lambda *a, **k: pytest.fail(f"the operator listed {a}"))
    monkeypatch.setattr(os, "open", guarded(real_osopen, "opened"))


def test_the_editor_never_reads_outside_the_workspace_for_a_hands_entity(tmp_path, monkeypatch):
    """The HIGH: a file the operator can read, outside the hands workspace, was shown by ``view``."""
    from openhands.tools.file_editor.definition import FileEditorAction

    T, hands, provider, ed = _editor(tmp_path, monkeypatch)
    secret = tmp_path / "operator-notes.txt"
    secret.write_text("do-not-leak")
    (tmp_path / "operator-dir").mkdir()
    (tmp_path / "operator-dir" / "private-name").write_text("")
    for path in (secret, tmp_path / "operator-dir"):
        obs = ed(FileEditorAction(command="view", path=str(path)))
        assert obs.is_error, obs
        assert "do-not-leak" not in str(obs) and "private-name" not in str(obs)
        assert "workspace" in str(obs)
    obs = ed(FileEditorAction(command="create", path=str(tmp_path / "planted.txt"), file_text="x"))
    assert obs.is_error and not (tmp_path / "planted.txt").exists()


def test_the_editor_reads_lists_and_edits_through_the_hands_user(tmp_path, monkeypatch):
    from openhands.tools.file_editor.definition import FileEditorAction

    T, hands, provider, ed = _editor(tmp_path, monkeypatch)
    ws = hands.workspace
    (ws / "notes.txt").write_text("one\ntwo\n")
    (ws / "pkg").mkdir()
    (ws / "pkg" / "mod.py").write_text("x = 1\n")
    _no_operator_access(T, monkeypatch, ws / "notes.txt", ws / "pkg", ws / "pkg" / "mod.py")
    obs = ed(FileEditorAction(command="view", path=str(ws / "notes.txt")))
    assert not obs.is_error and "two" in str(obs), obs
    obs = ed(FileEditorAction(command="view", path=str(ws / "pkg")))
    assert not obs.is_error and "mod.py" in str(obs), obs
    obs = ed(FileEditorAction(command="str_replace", path=str(ws / "notes.txt"), old_str="two", new_str="2"))
    assert not obs.is_error, obs
    obs = ed(FileEditorAction(command="insert", path=str(ws / "notes.txt"), insert_line=1, new_str="ins"))
    assert not obs.is_error, obs
    monkeypatch.undo()
    ed.close()
    assert (ws / "notes.txt").read_text() == "one\nins\n2\n"
    ops = {op for op, _ in provider.calls}
    assert {"read", "stat", "list", "write"} <= ops


def test_a_fifo_or_link_in_the_workspace_is_refused_without_hanging(tmp_path, monkeypatch):
    from openhands.tools.file_editor.definition import FileEditorAction

    T, hands, provider, ed = _editor(tmp_path, monkeypatch)
    ws = hands.workspace
    os.mkfifo(ws / "fifo")
    secret = tmp_path / "operator-notes.txt"
    secret.write_text("do-not-leak")
    os.symlink(secret, ws / "link")
    for name in ("fifo", "link"):
        t0 = time.monotonic()
        obs = ed(FileEditorAction(command="view", path=str(ws / name)))
        assert obs.is_error and "do-not-leak" not in str(obs), obs
        assert time.monotonic() - t0 < 15


def test_the_editor_fails_closed_when_the_hands_helper_cannot_run(tmp_path, monkeypatch):
    """sudo refusing (the rule removed): an in-band error, never a read as the operator instead."""
    from openhands.tools.file_editor.definition import FileEditorAction

    class Refusing:
        def hands_file(self, policy, h, op, path, data=b"", *, timeout=60.0):
            raise OSError("nobody could not read: sudo: a password is required")

        def hands_write(self, policy, h, path, data, *, timeout=120.0):
            self.hands_file(policy, h, "write", path, data)

    T, hands, _, ed = _editor(tmp_path, monkeypatch, provider=Refusing())
    (hands.workspace / "notes.txt").write_text("in-the-workspace")
    _no_operator_access(T, monkeypatch, hands.workspace / "notes.txt")
    obs = ed(FileEditorAction(command="view", path=str(hands.workspace / "notes.txt")))
    assert obs.is_error and "in-the-workspace" not in str(obs), obs
    assert "password is required" in str(obs)
    monkeypatch.undo()
    ed.close()
