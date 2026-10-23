"""S1 session 5 piece 5c-ii-c session 2: the Linux child of ``adopt-open`` (DESIGN §5b; head rulings 1022+19 Q2, Q3).

Rows: the stage's session keyring join (A6: an export target on Linux joins a fresh session keyring while still root,
before the drop, and proves it; a failure execs nothing), the rlimit set root puts on the forked export child (CORE,
FSIZE, CPU, soft = hard, never RLIMIT_AS), the Linux launch (the leaf the child joins is the leaf seal-in makes) and
the leaf join through ``wall.exec_launch``'s own proof, run in a real forked child. ``HELPER_MODULES`` holds the
sudo helpers only (session-1 ruling Q2).

The stage rows run the s2d fake kernel; the keyctl syscall itself runs only on a Linux host (its own row, skipped here). The real
bwrap view, the real cgroup leaf and the real drop are 5g rows: this Mac runs none of them.
"""
from __future__ import annotations

import errno
import functools
import os
import resource
import sys
import types

import pytest

from levain.slot import Refused
from levain.slot import export_run as ER
from levain.slot import keyring_join as KJ
from levain.slot import root_tree
from levain.slot import runtime_helper as RH
from levain.slot import wall
from tests.execve_guard import GUARD_EXIT, pid_locked_real_execve, refuse_real_execve
from tests.test_slot_s2d_wall import FakeOS, fake_for
from tests.test_slot_s2d_wall import spec as slot_spec

@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    yield from refuse_real_execve(monkeypatch, wall.os, route=True)


IDS = {"_levain_export": 798, "_levain_verify": 797}
SHARED = {"levain-export": 784, "levain-dispatch-export": 783, "levain-verify": 781, "levain-slots": 789}
LINKS = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}


def _export_spec(role: str, host: str = "linux") -> wall.Spec:
    cls = "operator" if role == "importer" else None
    ids = IDS if host == "linux" else {"_levain_export": 3798, "_levain_verify": 3797}
    shared = SHARED if host == "linux" else {k: v + 3000 for k, v in SHARED.items()}
    return wall.export_launch_spec(host, role, ids=ids, shared=shared, export_class=cls)


# --- the keyring join itself ---------------------------------------------------------------------------------------

class Kernel:
    """A session keyring the way the kernel answers for it: a join makes a new serial and makes it current."""

    def __init__(self, *, join=None, get=None) -> None:
        self.current, self.log = 7, []
        self.join, self.get = join, get

    def __call__(self, op, arg2, arg3=0):
        self.log.append((op, arg2, arg3))
        if op == KJ.KEYCTL_JOIN_SESSION_KEYRING:
            if self.join is not None:
                return self.join()
            self.current += 1
            return self.current
        if op == KJ.KEYCTL_GET_KEYRING_ID:
            return self.get() if self.get is not None else self.current
        raise OSError(errno.EINVAL, "op")


def _errno(e):
    def f():
        raise OSError(e, os.strerror(e))
    return f


def test_the_join_makes_a_new_anonymous_session_keyring_and_proves_it_current():
    k = Kernel()
    assert KJ.join_session_keyring(k) == 8
    # anonymous join (name NULL), then the session keyring read back WITHOUT create
    assert k.log == [(1, 0, 0), (0, -3, 0)]


@pytest.mark.parametrize("kernel, why", [
    (Kernel(join=_errno(errno.EDQUOT)), "join: errno"),
    (Kernel(join=lambda: -1), "join returned"),
    (Kernel(join=lambda: 0), "join returned"),
    (Kernel(join=lambda: True), "join returned"),
    (Kernel(get=lambda: 7), "session keyring is 7"),
    (Kernel(get=_errno(errno.EACCES)), "get session keyring: errno"),
    (None, "no keyctl"),
])
def test_a_join_that_is_not_proven_refuses(kernel, why):
    with pytest.raises(Refused) as ei:
        KJ.join_session_keyring(kernel)
    assert ei.value.name == "keyring-not-joined" and why in ei.value.detail


def test_the_syscall_number_is_named_for_two_machines_and_refused_for_any_other():
    assert KJ.keyctl_nr("Linux", "x86_64") == 250 and KJ.keyctl_nr("Linux", "aarch64") == 219
    for sysname, machine in (("Linux", "armv7l"), ("Linux", "riscv64"), ("Darwin", "x86_64"), ("Darwin", "arm64")):
        with pytest.raises(Refused) as ei:
            KJ.keyctl_nr(sysname, machine)
        assert ei.value.name == "keyring-not-joined"


# --- the stage ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["importer", "verifier"])
def test_the_export_stage_joins_a_fresh_session_keyring_as_root_before_the_drop(role, monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    fake = FakeOS(linux=True, fds=(0, 1, 2, 3, 4), spec_bytes=_export_spec(role).to_json())
    before = fake.session_keyring
    wall.stage_main(os_calls=fake.calls)
    names = fake.names()
    assert names.count("keyctl") == 2 and "execve" in names
    assert names.index("keyctl") < names.index("clear_inheritable" if "clear_inheritable" in names else "setgroups")
    assert names.index("keyctl") < names.index("setgroups") < names.index("setuid")
    assert fake.session_keyring == before + 1


@pytest.mark.parametrize("fault", ["raise", "noop", lambda op, a2, a3=0: 101 if op == 1 else 100])
def test_a_keyring_join_that_fails_execs_nothing_and_drops_nothing(fault, monkeypatch, capsys):
    """``raise``: the join errors; ``noop``: it answers None; the lambda: the read-back is the OLD keyring. Each
    refuses before any credential call, and the stage execs nothing."""
    monkeypatch.setattr(wall.sys, "platform", "linux")
    fake = FakeOS({"keyctl": fault}, linux=True, fds=(0, 1, 2, 3, 4), spec_bytes=_export_spec("importer").to_json())
    assert wall.stage_main(os_calls=fake.calls) == wall.STAGE_EXIT
    names = fake.names()
    assert "execve" not in names and "setgroups" not in names and "setuid" not in names
    assert "refused keyring-not-joined" in capsys.readouterr().err


def test_an_injected_os_calls_without_keyctl_refuses_rather_than_skip_the_join(monkeypatch, capsys):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    fake = FakeOS(linux=True, fds=(0, 1, 2, 3, 4), spec_bytes=_export_spec("importer").to_json())
    del fake.calls.keyctl
    assert wall.stage_main(os_calls=fake.calls) == wall.STAGE_EXIT
    assert "execve" not in fake.names() and "setgroups" not in fake.names()
    assert "refused keyring-not-joined: no keyctl" in capsys.readouterr().err


def test_no_keyring_call_for_a_slot_target_or_on_macos(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    slot = fake_for(slot_spec("linux", "vendor"), linux=True, fds=(0, 1, 2, 3, 4))
    wall.stage_main(3, slot.calls)
    assert "execve" in slot.names() and "keyctl" not in slot.names()
    monkeypatch.setattr(wall.sys, "platform", "darwin")
    from levain.slot import render as rd
    sp = _export_spec("importer", "darwin")
    mac = FakeOS(files={sp.profile: rd.render_importer_profile("darwin")}, fds=(0, 1, 2, 3, 4),
                 spec_bytes=sp.to_json())
    wall.stage_main(os_calls=mac.calls)
    assert "execve" in mac.names() and "keyctl" not in mac.names()


# --- the rlimit set (head ruling Q3) ----------------------------------------------------------------------------------

def test_the_export_rlimits_are_core_fsize_cpu_soft_equal_hard_and_never_as():
    lims = RH.export_rlimits(5 << 20, 1800)
    assert [r for r, _v in lims] == [resource.RLIMIT_CORE, resource.RLIMIT_FSIZE, resource.RLIMIT_CPU]
    assert dict(lims) == {resource.RLIMIT_CORE: 0, resource.RLIMIT_FSIZE: 5 << 20, resource.RLIMIT_CPU: 1800}
    assert resource.RLIMIT_AS not in dict(lims) and resource.RLIMIT_NPROC not in dict(lims)
    hard = {resource.RLIMIT_CORE: resource.RLIM_INFINITY, resource.RLIMIT_FSIZE: 1 << 20,
            resource.RLIMIT_CPU: resource.RLIM_INFINITY}
    log: list = []
    RH.apply_rlimits(lims, getrlimit=lambda r: (0, hard[r]), setrlimit=lambda r, v: log.append((r, v)))
    # soft = hard for each; never above a hard limit already lower (FSIZE here): lowering needs no privilege
    assert log == [(resource.RLIMIT_CORE, (0, 0)), (resource.RLIMIT_FSIZE, (1 << 20, 1 << 20)),
                   (resource.RLIMIT_CPU, (1800, 1800))]


# --- the Linux launch and the leaf join ------------------------------------------------------------------------------

def _linux_ctx():
    seams = types.SimpleNamespace(host="linux", account_uid=lambda name: IDS[name], group_gid=lambda g: SHARED[g],
                                  host_links=lambda: LINKS)
    return types.SimpleNamespace(seams=seams)


def test_the_linux_launch_joins_the_leaf_seal_in_makes_and_runs_the_stage_under_bwrap():
    from levain.slot.bwrap import LEVAIN_BWRAP
    args = ("--git", "/usr/bin/git", "--session", "s-" + "c2" * 8)
    cmd = RH.export_launch_default(_linux_ctx(), args, "operator")
    assert cmd.cgroup == ER.leaf_path("importer")            # the one leaf: seal-in's make_leaf and the join agree
    assert cmd.argv[0] == str(LEVAIN_BWRAP) and cmd.argv[-len(wall.stage_argv("linux")):] == wall.stage_argv("linux")
    assert "--unshare-net" in cmd.argv and "nsenter" not in " ".join(cmd.argv)
    spec = wall.parse_spec(cmd.spec_json)
    assert (spec.target, spec.uid, spec.profile) == ("importer", 798, None)
    assert spec.argv[1:] == args and spec.keep_fds == (wall.RESULT_FD,)


def _probe_argv(out_path: str):
    code = ("import os, sys\n"
            "fds = []\n"
            "for fd in range(256):\n"
            "    try:\n"
            "        os.fstat(fd)\n"
            "        fds.append(fd)\n"
            "    except OSError:\n"
            "        pass\n"
            "open(sys.argv[1], 'w').write(repr(fds))\n")
    return ("/usr/bin/python3", "-I", "-S", "-c", code, out_path)


@pytest.mark.parametrize("joined", [True, False])
def test_the_forked_child_joins_the_leaf_through_exec_launch_and_execs_only_when_proven(joined, tmp_path,
                                                                                         monkeypatch):
    """The real fork of ``spawn_export`` with ``wall.exec_launch`` as its last act; the cgroupfs is a fake (a
    file for ``cgroup.procs`` and the ``/proc/self/cgroup`` text), everything else is the real OS. Proven: the
    child writes its own pid, then execs. Not proven: it exits 70 and execs nothing."""
    procs = tmp_path / "cgroup.procs"
    leaf = ER.leaf_path("importer")
    member = "0::" + leaf[len("/sys/fs/cgroup"):] if joined else "0::/user.slice"

    def write_file(path, text):
        assert path == f"{leaf}/cgroup.procs"
        procs.write_text(text)

    # the forked child really execs the probe; the real execve is locked to every pid but pytest's
    monkeypatch.setattr(wall.os, "execve", pid_locked_real_execve(os.getpid()))
    calls = wall.OsCalls(write_file=write_file, read_text=lambda p: member + "\n" if p == "/proc/self/cgroup" else "")
    seen = tmp_path / "fds.txt"
    cmd = RH.ExportCmd(_probe_argv(str(seen)), b"{}", cgroup=leaf,
                       exec_child=functools.partial(wall.exec_launch, os_calls=calls))
    frame_r, frame_w = os.pipe()
    result_r, result_w = os.pipe()
    out = os.open(str(tmp_path / "out"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        pid = RH.spawn_export(cmd, frame_r=frame_r, stdout_fd=out, stderr_fd=out, result_w=result_w,
                              rlimits=RH.export_rlimits(1 << 30, 60))
        _done, status = os.waitpid(pid, 0)
    finally:
        for fd in (frame_r, frame_w, result_r, result_w, out):
            os.close(fd)
    assert procs.read_text() == f"{pid}\n"                    # the child wrote ITS pid before anything else
    if joined:
        assert os.WEXITSTATUS(status) == 0 and seen.exists()
        # the spec pipe sits on SPEC_FD (3): with no stage here nothing reads and closes it (the stage does, 5g)
        assert eval(seen.read_text()) == [0, 1, 2, 3, 4]       # noqa: S307 - our probe's repr
    else:
        assert os.WEXITSTATUS(status) == 70 and not seen.exists()


def test_the_pid_locked_execve_raises_in_the_pytest_pid():
    with pytest.raises(AssertionError, match="pytest process"):
        pid_locked_real_execve(os.getpid())("/bin/true", ["/bin/true"], {})


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_guard_hit_in_a_forked_child_exits_with_the_distinctive_status():
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the child
        try:
            wall.os.execve("/bin/true", ["/bin/true"], {})
        finally:
            os._exit(1)                                       # only reached if the guard did not exit
    _done, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == GUARD_EXIT


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the keyctl syscall runs on a Linux kernel only")
def test_the_real_keyctl_joins_and_proves_on_this_linux_kernel():
    """Unprivileged is enough to join a new anonymous session keyring; run in a child so the test process keeps its
    own. Root's join inside the real stage is a 5g row."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the child
        try:
            before = KJ.real_keyctl(KJ.KEYCTL_GET_KEYRING_ID, KJ.KEY_SPEC_SESSION_KEYRING, 0)
            serial = KJ.join_session_keyring(KJ.real_keyctl)
            os.write(w, f"{before} {serial}".encode())
        except BaseException as e:   # noqa: BLE001
            os.write(w, f"ERR {e!r}".encode())
        os._exit(0)
    os.close(w)
    got = os.read(r, 4096).decode()
    os.close(r)
    os.waitpid(pid, 0)
    before, serial = got.split()
    assert before != "ERR" and int(serial) > 0 and int(serial) != int(before), got


# --- root_tree: HELPER_MODULES are the sudo helpers only (session-1 ruling Q2) -------------------------------------

def test_helper_modules_are_the_two_sudo_helpers_and_an_export_entry_is_not_one():
    assert root_tree.HELPER_MODULES == {"install-helper": "levain.slot.install_helper",
                                        "runtime-helper": "levain.slot.runtime_helper"}
    assert "export-importer" in root_tree.ENTRY_MODULES
    with pytest.raises(Refused) as ei:
        root_tree.helper_entry_text("darwin", "export-importer")
    assert ei.value.name == "bad-token"
