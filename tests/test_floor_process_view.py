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
