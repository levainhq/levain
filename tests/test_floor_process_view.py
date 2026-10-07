"""Keeping process views out of the entity's reach (lane P2's research note, item 2c).

The file editor runs inside levain's own process, so ``/proc/self/mem`` and ``/proc/<pid>/environ``
reached levain's memory and other processes' environments; on Linux bash saw every process of the
user through the sandbox's procfs. These tests read the predicate, the rendered bwrap argv and the
mount table as data. The live check (``test_linux_live_bash_sees_no_host_process``) runs only where
bwrap can establish a namespace.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing.confinement import BwrapProvider, build_policy, crown_jewel_reason


@pytest.fixture
def policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    return build_policy(ent)


@pytest.mark.parametrize("path", [
    "/proc/self/environ", "/proc/self/mem", "/proc/1/cmdline", "/proc/self/fd/3", "/proc",
    "/dev/fd/3", "/dev/fd", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/PROC/self/environ",
    "/proc/../proc/self/environ",
])
def test_the_file_editor_refuses_process_views(policy, path: str) -> None:
    assert crown_jewel_reason(policy, path) is not None, path


@pytest.mark.parametrize("path", ["/procfoo/x", "/dev/null", "/dev/fdx", "/tmp/proc/self/environ"])
def test_the_rule_is_anchored(policy, path: str) -> None:
    assert crown_jewel_reason(policy, path) is None, path


def test_a_link_to_a_process_view_is_refused_at_its_target(policy, tmp_path: Path) -> None:
    link = tmp_path / "work" / "env"
    link.parent.mkdir()
    link.symlink_to("/proc/self/environ")
    assert crown_jewel_reason(policy, link) is not None


def test_bwrap_runs_bash_in_its_own_pid_namespace(policy) -> None:
    argv = C._bwrap_argv(policy)
    assert "--unshare-pid" in argv
    assert "--unshare-pid" in BwrapProvider().render_profile(policy)


_MOUNTINFO = """\
22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
23 22 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
24 23 0:22 / /proc/sys/fs/binfmt_misc rw,relatime shared:13 - autofs systemd-1 rw
25 22 0:21 / /srv/chroot/proc rw,relatime - proc proc rw
26 22 0:21 / /var/lib/my\\040box/proc rw,relatime - proc proc rw
27 23 0:21 / /proc/x rw,relatime - proc proc rw
28 22 0:30 / /run rw,nosuid shared:5 - tmpfs tmpfs rw
"""


def test_the_mount_table_scan_finds_procfs_outside_proc(tmp_path: Path) -> None:
    mi = tmp_path / "mountinfo"
    mi.write_text(_MOUNTINFO)
    assert C._extra_procfs_mounts(str(mi)) == [Path("/srv/chroot/proc"), Path("/var/lib/my box/proc")]


def test_no_mount_table_means_none_and_a_garbled_one_refuses(tmp_path: Path) -> None:
    assert C._extra_procfs_mounts(str(tmp_path / "absent")) == []
    bad = tmp_path / "mountinfo"
    bad.write_text("22 1 8:1 / / rw\n")
    with pytest.raises(OSError):
        C._extra_procfs_mounts(str(bad))


def test_the_plan_hides_every_other_procfs_read_only(policy, tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "chroot" / "proc"
    other.mkdir(parents=True)
    monkeypatch.setattr(C, "_extra_procfs_mounts", lambda: [other])
    argv = C._bwrap_argv(policy)
    i = argv.index(str(other))
    assert argv[i - 1] == "--tmpfs"
    assert str(other) in [argv[k + 1] for k, a in enumerate(argv) if a == "--remount-ro"]


def test_a_garbled_mount_table_refuses_bash(policy, monkeypatch) -> None:
    def boom():
        raise OSError("unreadable mount table line")
    monkeypatch.setattr(C, "_extra_procfs_mounts", boom)
    with pytest.raises(C.ConfinementError, match="mount table"):
        C._bwrap_argv(policy)


def test_the_availability_probes_ask_for_a_pid_namespace(monkeypatch) -> None:
    seen: list[list[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(C, "BWRAP", sys.executable)   # any executable file: the probe only runs it
    monkeypatch.setattr(C.subprocess, "run", lambda argv, **kw: (seen.append(argv), _Done())[1])
    assert C.bwrap_available() and C.bwrap_netns_available()
    assert all("--unshare-pid" in argv for argv in seen) and len(seen) == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl(PR_SET_DUMPABLE) is Linux")
def test_levain_marks_itself_not_dumpable_on_linux() -> None:
    code = ("import ctypes; from levain.launch import set_not_dumpable; ok = set_not_dumpable(); "
            "print(ok, ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))")   # 3 = PR_GET_DUMPABLE
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.getcwd()})
    assert out.stdout.split() == ["True", "0"], out.stderr


def test_not_dumpable_is_a_no_op_off_linux(monkeypatch) -> None:
    from levain import launch

    monkeypatch.setattr(launch.sys, "platform", "darwin")
    assert launch.set_not_dumpable() is False


_live = pytest.mark.skipif(
    not (platform.system() == "Linux" and C.bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


@_live
def test_linux_live_bash_sees_no_host_process(policy) -> None:
    shell = BwrapProvider().spawn_shell(policy)
    try:
        r = shell.run(f"test -e /proc/{os.getpid()}/environ && echo VISIBLE || echo HIDDEN; "
                      "ls /proc | grep -c '^[0-9]'")
    finally:
        shell.close()
    lines = r.output.split()
    assert lines[0] == "HIDDEN"
    assert int(lines[1]) < 10, "only the sandbox's own few processes are listed"


def test_the_spelling_is_checked_as_given_not_only_resolved() -> None:
    # On Linux /proc/self/fd/N resolves to the file fd N has open, which may be an ordinary path.
    assert C._process_view_reason("/proc/self/fd/7", Path("/home/u/work/notes.txt")) is not None
    assert C._process_view_reason("/home/u/work/notes.txt", Path("/home/u/work/notes.txt")) is None


# --- the file editor judges the object it opened, not only the path it was given -----------------------


def test_a_link_flipped_after_the_path_check_is_refused_at_the_open(tmp_path: Path, monkeypatch) -> None:
    """The race, made deterministic: the path check sees a benign file, and by the time the editor
    opens the path it leads to a crown jewel."""
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel_dir = tmp_path / ".anneal-memory"
    jewel_dir.mkdir()
    (jewel_dir / "secret.txt").write_text("JEWEL-CONTENT\n")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    link = ws / "notes.txt"
    link.write_text("benign\n")
    ex = T.CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws))
    real_check = T.crown_jewel_reason

    def check_then_flip(policy, path):
        out = real_check(policy, path)
        if Path(path) == link:          # the shell swaps the file for a link right after the check
            link.unlink()
            link.symlink_to(jewel_dir / "secret.txt")
        return out
    monkeypatch.setattr(T, "crown_jewel_reason", check_then_flip)
    obs = ex(FileEditorAction(command="view", path=str(link)))
    text = "".join(getattr(c, "text", "") for c in obs.to_llm_content)
    assert "JEWEL-CONTENT" not in text
    assert obs.is_error and "crown" in text.lower()


def test_the_open_check_is_inert_outside_a_floored_call(tmp_path: Path) -> None:
    from levain.firing.openhands import tools as T

    f = tmp_path / "x"
    f.write_text("ok")
    with T._floored_open(f) as fh:
        assert fh.read() == "ok"


def test_opened_file_reason_names_a_jewel_by_the_opened_object(policy, tmp_path: Path) -> None:
    jewel = tmp_path / ".anneal-memory" / "m.db"
    jewel.parent.mkdir(exist_ok=True)
    jewel.write_text("x")
    other = tmp_path / "plain"
    other.write_text("y")
    pol = build_policy(tmp_path / "ent")
    with open(jewel) as a, open(other) as b:
        assert C.opened_file_reason(pol, a.fileno()) is not None
        assert C.opened_file_reason(pol, b.fileno()) is None
    r, w = os.pipe()
    try:
        assert "could not be identified" in (C.opened_file_reason(pol, r) or "")
    finally:
        os.close(r)
        os.close(w)


def test_a_write_through_a_flipped_link_neither_truncates_nor_writes_the_jewel(tmp_path: Path,
                                                                                monkeypatch) -> None:
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel = tmp_path / ".anneal-memory" / "secret.txt"
    jewel.parent.mkdir()
    jewel.write_text("JEWEL-CONTENT\n")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "new.txt"
    ex = T.CrownJewelsFileEditorExecutor(policy=build_policy(ent, workspace=ws))
    real_check = T.crown_jewel_reason

    def check_then_flip(policy, path):
        out = real_check(policy, path)
        if Path(path) == target and not target.is_symlink():
            target.symlink_to(jewel)
        return out
    monkeypatch.setattr(T, "crown_jewel_reason", check_then_flip)
    obs = ex(FileEditorAction(command="create", path=str(target), file_text="PLANTED"))
    assert obs.is_error
    assert jewel.read_text() == "JEWEL-CONTENT\n"


def test_a_refused_write_open_does_not_truncate_first(tmp_path: Path, monkeypatch) -> None:
    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    jewel = tmp_path / ".anneal-memory" / "secret.txt"
    jewel.parent.mkdir()
    jewel.write_text("JEWEL-CONTENT\n")
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(jewel)
    token = T._EDITOR_FLOOR.set(build_policy(ent))
    try:
        with pytest.raises(T._FloorRefusedOpen):
            T._floored_open(link, "w")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert jewel.read_text() == "JEWEL-CONTENT\n"


def test_on_linux_any_other_procfs_counts_as_a_process_view(monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(C, "_extra_procfs_mounts", lambda: [Path("/srv/chroot/proc")])
    assert C._process_view_reason("/srv/chroot/proc/1/environ", Path("/srv/chroot/proc/1/environ"))
    assert C._process_view_reason("/srv/chroot/etc/hosts", Path("/srv/chroot/etc/hosts")) is None


def test_a_host_that_cannot_give_bash_a_pid_namespace_is_named_and_refused(monkeypatch) -> None:
    monkeypatch.setattr(C.os.path, "isfile", lambda p: True if p == C.BWRAP else os.path.lexists(p))
    monkeypatch.setattr(C.os, "access", lambda p, m: True if p == C.BWRAP else os.access(p, m))
    monkeypatch.setattr(C.BwrapProvider, "available", lambda self: False)
    monkeypatch.setattr(C, "_bwrap_runs_without_a_pid_namespace", lambda: True)
    d = C.diagnose_confinement("Linux")
    assert not d.supported and "PID namespace" in d.reason and "systempaths=unconfined" in d.remedy


def test_a_file_shaped_procfs_mount_is_masked_not_tmpfsd(policy, tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "bound-environ"
    f.write_text("")
    monkeypatch.setattr(C, "_extra_procfs_mounts", lambda: [f])
    argv = C._bwrap_argv(policy)
    assert ["--ro-bind", "/dev/null", str(f)] == argv[argv.index(str(f)) - 2: argv.index(str(f)) + 1]
    assert "--tmpfs" != argv[argv.index(str(f)) - 1]


# --- L3 r1 -------------------------------------------------------------------------------------------


def _under_floor(tmp_path: Path, monkeypatch):
    from levain.firing.openhands import tools as T

    monkeypatch.setenv("HOME", str(tmp_path))
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True, exist_ok=True)
    return T, T._EDITOR_FLOOR.set(build_policy(ent))


def test_x_mode_still_refuses_an_existing_file(tmp_path: Path, monkeypatch) -> None:
    T, token = _under_floor(tmp_path, monkeypatch)
    f = tmp_path / "ws.txt"
    f.write_text("KEEP")
    try:
        with pytest.raises(FileExistsError):
            T._floored_open(f, "x")
        with T._floored_open(tmp_path / "new.txt", "x") as fh:
            fh.write("ok")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert f.read_text() == "KEEP" and (tmp_path / "new.txt").read_text() == "ok"


def test_a_planted_fifo_is_refused_without_blocking(tmp_path: Path, monkeypatch) -> None:
    T, token = _under_floor(tmp_path, monkeypatch)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    try:
        with pytest.raises(T._FloorRefusedOpen, match="not a regular file"):
            T._floored_open(fifo)
    finally:
        T._EDITOR_FLOOR.reset(token)


def test_a_dangling_link_is_refused_not_followed_into_a_create(tmp_path: Path, monkeypatch) -> None:
    T, token = _under_floor(tmp_path, monkeypatch)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "elsewhere" / "target")
    (tmp_path / "elsewhere").mkdir()
    try:
        with pytest.raises(T._FloorRefusedOpen, match="dangling"):
            T._floored_open(link, "w")
    finally:
        T._EDITOR_FLOOR.reset(token)
    assert not (tmp_path / "elsewhere" / "target").exists()


def test_the_patched_opens_are_the_editors_only_way_to_read_a_file() -> None:
    """The open-then-check rides the module-global `open` of two editor modules. A later SDK that
    reads or writes another way would bypass it silently, so this fails if one appears."""
    import inspect

    from openhands.tools.file_editor import editor
    from openhands.tools.file_editor.utils import encoding

    for mod in (editor, encoding):
        src = inspect.getsource(mod)
        for primitive in ("read_text(", "read_bytes(", "write_text(", "write_bytes(", "io.open(",
                          "os.open(", "codecs.open(", "mmap"):
            assert primitive not in src, f"{mod.__name__} uses {primitive}"
