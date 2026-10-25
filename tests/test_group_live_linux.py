"""`group_live_proc` / `group_gone_proc` off macOS: a /proc scan tells a live member of a process group
from a zombie, while `_group_live` keeps its base answer (always live off macOS) for its callers.

The shell keeps each group's leader unreaped while its group lives (`_Leader`), and a PID 1 that does not
reap (a container without init) leaves zombie members, so a group `killpg` still finds may hold only
zombies. The scan runs here against a synthetic /proc tree through `_PROC_ROOT`; the real-Linux test at
the end runs only on Linux."""
from __future__ import annotations

import errno
import os
import platform
import subprocess
from pathlib import Path

import pytest

from levain.firing import confinement as C


def _stat(pid: int, comm: str, state: str, pgrp: int, ppid: int = 1) -> bytes:
    # pid (comm) state ppid pgrp session tty_nr ... (fields after pgrp are not read)
    return f"{pid} ({comm}) {state} {ppid} {pgrp} {pgrp} 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 123\n".encode()


def test_a_stat_line_is_parsed_after_the_last_paren() -> None:
    assert C._stat_pgrp_state(_stat(7, "a) (b", "S", 4242)) == (4242, "S")
    assert C._stat_pgrp_state(_stat(7, ") Z 1 999 (", "R", 4242)) == (4242, "R")
    assert C._stat_pgrp_state(_stat(7, "sh", "Z", 31)) == (31, "Z")
    assert C._stat_pgrp_state(b"7 (truncated") is None
    assert C._stat_pgrp_state(b"7 (sh) S 1 notanumber 1\n") is None


@pytest.fixture
def linux(monkeypatch, tmp_path: Path) -> Path:
    """Linux as `group_live_proc` sees it: a synthetic /proc, and a killpg that finds the group."""
    root = tmp_path / "proc"
    root.mkdir()
    (root / "self").mkdir()          # non-numeric entries are not processes
    (root / "meminfo").write_text("")
    _mountinfo(root, "rw")
    monkeypatch.setattr(C, "_PROC_ROOT", str(root))
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(C.os, "killpg", lambda pgid, sig: None)
    return root


def _mountinfo(root: Path, super_opts: str) -> None:
    (root / "self" / "mountinfo").write_text(
        "1 0 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
        f"22 1 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc {super_opts}\n"
        "30 22 0:30 / /proc/sys/fs/binfmt_misc rw - binfmt_misc binfmt_misc rw\n")


def _proc(root: Path, pid: int, comm: str, state: str, pgrp: int, tids: tuple[int, ...] = ()) -> None:
    (root / str(pid)).mkdir()
    (root / str(pid) / "stat").write_bytes(_stat(pid, comm, state, pgrp))
    (root / str(pid) / "task").mkdir()
    for tid in (pid, *tids):
        (root / str(pid) / "task" / str(tid)).mkdir()


def test_a_group_of_only_zombies_is_not_live(linux: Path) -> None:
    _proc(linux, 4242, "bash", "Z", 4242)          # the leader, held unreaped
    _proc(linux, 4250, "a) (b", "Z", 4242)         # a member a non-reaping PID 1 left
    _proc(linux, 4300, "sleep", "S", 9999)         # another group's live process
    assert C.group_live_proc(4242) is False


@pytest.mark.parametrize("state", ["S", "R", "D", "T", "t", "I"])
def test_one_live_member_beside_a_zombie_leader_is_live(linux: Path, state: str) -> None:
    _proc(linux, 4242, "bash", "Z", 4242)
    _proc(linux, 4260, ") Z 1 4242 (", state, 4242)  # comm forging a zombie's fields
    assert C.group_live_proc(4242) is True


def test_dead_states_do_not_count(linux: Path) -> None:
    _proc(linux, 4242, "bash", "Z", 4242)
    _proc(linux, 4243, "x", "X", 4242)
    assert C.group_live_proc(4242) is False


def test_an_unreadable_entry_is_skipped(linux: Path) -> None:
    _proc(linux, 4242, "bash", "Z", 4242)
    (linux / "4244").mkdir()                        # vanished between the listing and the read
    assert C.group_live_proc(4242) is False
    _proc(linux, 4245, "sleep", "S", 4242)
    assert C.group_live_proc(4242) is True


def test_a_group_killpg_finds_but_proc_does_not_show_is_live(linux: Path) -> None:
    # No member visible (hidden, or gone between the two reads): no evidence it is gone.
    _proc(linux, 4300, "sleep", "S", 9999)
    assert C.group_live_proc(4242) is True


def test_an_unlistable_proc_keeps_the_group_live(linux: Path, monkeypatch) -> None:
    monkeypatch.setattr(C, "_PROC_ROOT", str(linux / "absent"))
    assert C.group_live_proc(4242) is True


def test_a_group_killpg_cannot_find_is_gone(linux: Path, monkeypatch) -> None:
    def esrch(pgid, sig):
        raise ProcessLookupError(errno.ESRCH, "no such process")
    monkeypatch.setattr(C.os, "killpg", esrch)
    _proc(linux, 4242, "sleep", "S", 4242)          # never read: ESRCH settles it
    assert C.group_live_proc(4242) is False


def test_a_permission_refused_killpg_is_live(linux: Path, monkeypatch) -> None:
    # EPERM: a member of another uid, which /proc may not show truthfully: never gone on that.
    def eperm(pgid, sig):
        raise PermissionError(errno.EPERM, "operation not permitted")
    monkeypatch.setattr(C.os, "killpg", eperm)
    _proc(linux, 4242, "sudo", "Z", 4242)
    assert C.group_live_proc(4242) is True


@pytest.mark.parametrize("opts", ["rw,hidepid=2", "rw,hidepid=1", "rw,hidepid=invisible",
                                  "rw,hidepid=noaccess,gid=4", "rw,hidepid=ptraceable"])
def test_a_proc_mounted_with_hidepid_is_live(linux: Path, opts: str) -> None:
    _mountinfo(linux, opts)
    _proc(linux, 4242, "bash", "Z", 4242)
    assert C.group_live_proc(4242) is True


@pytest.mark.parametrize("opts", ["rw", "rw,hidepid=0", "rw,hidepid=off", "rw,subset=pid"])
def test_a_proc_without_hiding_is_read(linux: Path, opts: str) -> None:
    _mountinfo(linux, opts)
    _proc(linux, 4242, "bash", "Z", 4242)
    assert C.group_live_proc(4242) is False


def test_an_unreadable_mountinfo_is_treated_as_restricted(linux: Path) -> None:
    (linux / "self" / "mountinfo").unlink()
    _proc(linux, 4242, "bash", "Z", 4242)
    assert C.group_live_proc(4242) is True


def test_a_mountinfo_without_proc_is_treated_as_restricted(linux: Path) -> None:
    (linux / "self" / "mountinfo").write_text("1 0 8:1 / / rw - ext4 /dev/sda1 rw\n")
    _proc(linux, 4242, "bash", "Z", 4242)
    assert C.group_live_proc(4242) is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_pid_entry_that_refuses_its_read_is_live(linux: Path) -> None:
    _proc(linux, 4242, "bash", "Z", 4242)
    _proc(linux, 4247, "hidden", "S", 9999)
    (linux / "4247" / "stat").chmod(0)
    try:
        assert C.group_live_proc(4242) is True
    finally:
        (linux / "4247" / "stat").chmod(0o644)


def test_a_zombie_leader_with_a_live_thread_is_live(linux: Path) -> None:
    _proc(linux, 4242, "bash", "Z", 4242, tids=(4249,))
    assert C.group_live_proc(4242) is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a mode-000 directory")
def test_a_zombie_whose_threads_cannot_be_listed_is_live(linux: Path) -> None:
    _proc(linux, 4242, "bash", "Z", 4242)
    (linux / "4242" / "task").chmod(0)
    try:
        assert C.group_live_proc(4242) is True
    finally:
        (linux / "4242" / "task").chmod(0o755)


def test_macos_never_reads_proc(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(C, "_proc_group_live", lambda pgid: pytest.fail("macOS read /proc"))
    monkeypatch.setattr(C, "_group_live", lambda pgid: pgid == 4242)
    monkeypatch.setattr(C, "_group_gone", lambda pgid, timeout: ("gone", pgid, timeout))
    assert C.group_live_proc(4242) is True and C.group_live_proc(7) is False
    assert C.group_gone_proc(4242, timeout=1.5) == ("gone", 4242, 1.5)


def test_group_live_keeps_its_base_answer_off_macos(linux: Path) -> None:
    # `_stop_hands_group` and the hands `_signal` rest on this: off macOS a group killpg finds is live.
    _proc(linux, 4242, "bash", "Z", 4242)
    assert C._group_live(4242) is True
    assert C.group_live_proc(4242) is False


def _answers(monkeypatch, seq: list[bool]) -> list[float]:
    """`group_live_proc` answering ``seq`` in turn (its last answer after that); the call times."""
    at: list[float] = []
    it = iter(seq)

    def live(pgid: int) -> bool:
        at.append(C.time.monotonic())
        return next(it, seq[-1])
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(C, "group_live_proc", live)
    return at


def test_gone_needs_two_agreeing_scans_an_interval_apart(monkeypatch) -> None:
    at = _answers(monkeypatch, [True, False, False])
    assert C.group_gone_proc(4242, timeout=2.0) is True
    assert len(at) == 3 and at[2] - at[1] >= C._PROC_SCAN_INTERVAL


def test_one_gone_scan_between_live_ones_is_not_gone(monkeypatch) -> None:
    at = _answers(monkeypatch, [False, True, False, True])
    assert C.group_gone_proc(4242, timeout=0.3) is False
    assert len(at) >= 4


def test_a_gone_scan_at_the_deadline_still_gets_its_second(monkeypatch) -> None:
    at = _answers(monkeypatch, [False, False])
    assert C.group_gone_proc(4242, timeout=0.0) is True
    assert len(at) == 2


@pytest.mark.skipif(platform.system() != "Linux", reason="the real /proc path runs only on Linux")
def test_real_linux_zombie_leader_and_live_member() -> None:
    # Leader and member are both this process's children, in the leader's group, and both reaped here.
    leader = subprocess.Popen(["sleep", "30"], process_group=0)
    member = subprocess.Popen(["sleep", "30"], process_group=leader.pid)
    try:
        os.kill(leader.pid, 9)
        os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)   # exited, left unreaped: a zombie
        assert C.group_live_proc(leader.pid) is True               # the member lives
        member.kill()
        member.wait()
        assert C.group_gone_proc(leader.pid, timeout=5.0) is True  # only the zombie leader is left
    finally:
        for p in (member, leader):
            if p.poll() is None:
                p.kill()
            p.wait()
