"""S1 session 5, piece 5c-ii-b: the sealed run's OS parts in ``levain/slot/export_run.py`` (the per-run image, the
Linux run leaf, the macOS footprint watchdog, the domain proof, :class:`SealedRun`'s seal-in / seal-out / teardown),
plus head ruling A2 (1022-18 HEAD_rulings.md) on 5c-ii-a's transaction.

The macOS image runs for REAL here when the host is darwin and ``/usr/bin/hdiutil`` exists (uid 501 may create,
attach and detach an image; R4 measured it): :func:`test_real_hdiutil_image_create_attach_detach`. Everything else
goes through the seams the module names: the image's command runner, the cgroup file ops, the clock, the
watchdog's process listing and footprint reader, and the domain proof.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import export_run as X
from levain.slot import importer as IMP
from levain.slot.proc import Domain, PROVEN

S = "s1"
OID_A = "a" * 40


class Log:
    def __init__(self):
        self.events = []

    def __call__(self, e):
        self.events.append(e)


def _dfd(p: Path) -> int:
    return os.open(p, os.O_RDONLY | os.O_DIRECTORY)


def _line(*, result="delta") -> bytes:
    return IMP.result_line(IMP.Progress(terminal=True, entries=3, bytes=10), refused=None, detail="",
                           enospc=False, result=result)


def _bundle(s: str = S) -> bytes:
    return b"# v2 git bundle\n" + f"{OID_A} refs/levain/{s}/head\n".encode() + b"\nPACK"


def _work(tmp_path: Path, s: str = S) -> Path:
    w = tmp_path / ".work"
    (w / "out").mkdir(parents=True)
    (w / "out" / "bundle").write_bytes(_bundle(s))
    (w / "out" / "report.json").write_bytes(b'{"r":1}')
    return w


def _class_dir(tmp_path: Path, *, older: bool) -> Path:
    c = tmp_path / "operator"
    c.mkdir()
    if older:
        (c / S).mkdir()
        (c / S / "bundle").write_bytes(b"OLD")
    return c


CAPS = {"bundle": 1 << 20, "report.json": 1 << 16}


def _publish(tmp_path, *, seams, older=False):
    w, c = _work(tmp_path), _class_dir(tmp_path, older=older)
    wd, cd = _dfd(w), _dfd(c)
    try:
        ok = X.classify(X.RunObservation(exit="exit", code=0, line=_line()))
        return c, X.publish_if_success(ok, wd, cd, S, object_format="sha1", owner_uid=os.getuid(), caps=CAPS,
                                       seams=seams)
    finally:
        os.close(wd)
        os.close(cd)


# --- A2: a class-dir fsync failure after the commit -------------------------------------------------------------

@pytest.mark.parametrize("older", [False, True])
def test_a2_class_fsync_failure_commits_with_a_crash_warning(tmp_path, older):
    def fault(p):
        if p == "fsync:class":
            raise OSError(errno.EIO, os.strerror(errno.EIO))
    log = Log()
    c, (_result, pub) = _publish(tmp_path, seams=X.PublishSeams(log=log, fault=fault), older=older)
    assert (c / S / "bundle").read_bytes() == _bundle()           # committed: the rename is the commit point
    assert len(pub.warnings) == 1
    w = pub.warnings[0]
    assert "a crash may undo the publish" in w and "EIO" in w
    assert "fsync class failed EIO" in log.events


def test_a2_a_clean_publish_carries_no_warning(tmp_path):
    _c, (_r, pub) = _publish(tmp_path, seams=X.PublishSeams(log=Log()))
    assert pub.warnings == ()


# --- the real macOS image (R4: uid 501 creates, attaches, detaches) ----------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists(X.HDIUTIL), reason="macOS hdiutil only")
def test_real_hdiutil_image_create_attach_detach(tmp_path):
    exp = tmp_path / "export"
    exp.mkdir()
    log = Log()
    seams = X.ImageSeams(log=log)
    img = X.create_image(str(exp), "importer", "darwin", size=16 << 20, inodes=0, owner_uid=os.getuid(), seams=seams)
    try:
        st = os.lstat(img.path)
        assert img.path == str(exp / "work.sparseimage") and stat.S_IMODE(st.st_mode) == 0o600
        assert os.path.ismount(img.mount) and stat.S_IMODE(os.stat(img.mount).st_mode) == 0o700
        assert os.path.exists(os.path.join(img.mount, X.NEVER_INDEX))
    finally:
        assert X.detach_image(img, seams) and X._unlink_backing(img, seams)
    assert not os.path.ismount(img.mount) and not os.path.lexists(img.path)


# --- the image through its command seam ---------------------------------------------------------------------------

class FakeRunner:
    """hdiutil / mkfs / mount / umount as a fake: ``create`` makes the file at the mode its umask gives, ``attach`` and
    ``mount`` mark the mount point mounted, ``detach`` / ``umount`` unmark it unless told to fail."""

    def __init__(self, *, fail=(), detach_rc=0, ignore_umask=False):
        self.calls = []
        self.mounted = set()
        self.fail = set(fail)
        self.detach_rc = detach_rc
        self.ignore_umask = ignore_umask

    def __call__(self, argv, umask):
        self.calls.append((tuple(argv), umask))
        verb = os.path.basename(argv[0]) + ("." + argv[1] if argv[0] == X.HDIUTIL else "")
        if verb in self.fail:
            return 1, f"{verb}: seam failure"
        if verb == "hdiutil.create":
            mode = 0o666 & ~(0o022 if (umask is None or self.ignore_umask) else umask)
            fd = os.open(argv[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            os.close(fd)
            os.chmod(argv[-1], mode)
        elif verb == "hdiutil.attach":
            self.mounted.add(argv[argv.index("-mountpoint") + 1])
        elif verb == "mount":
            self.mounted.add(argv[-1])
        elif verb in ("hdiutil.detach", "umount"):
            if self.detach_rc:
                return self.detach_rc, "Resource busy"
            self.mounted.discard(argv[-1])
            for n in os.listdir(argv[-1]):                    # the image's tree leaves with it
                p = os.path.join(argv[-1], n)
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
        return 0, ""

    def seams(self, log=None):
        return X.ImageSeams(run=self, is_mounted=lambda p: p in self.mounted, log=log or Log())


def _exp(tmp_path):
    e = tmp_path / "export"
    e.mkdir(exist_ok=True)
    return str(e)


def test_image_names_come_from_the_one_path_function_and_the_walls_constant(tmp_path):
    from levain.slot import wall
    assert X.image_path("/L/export", "importer", "darwin") == "/L/export/work.sparseimage"
    assert X.image_path("/L/export", "verifier", "darwin") == "/L/export/verify.sparseimage"
    assert X.image_path("/L/export", "importer", "linux") == "/L/export/work.img"
    assert X.image_path("/L/export", "verifier", "linux") == "/L/export/verify.img"
    assert X.mount_path("/L/export", "importer") == "/L/export/" + wall.EXPORT_WRITABLE[0]
    assert X.mount_path("/L/export", "verifier") == "/L/export/" + wall.VERIFY_WRITABLE[0]
    for r in ("importer", "verifier"):
        assert X.leaf_path(r) == f"{wall.CGROUP_ROOT}/{wall._EXPORT_SCOPE[r]}/run"


@pytest.mark.parametrize("role,stem", [("importer", "work"), ("verifier", "verify")])
def test_darwin_create_runs_hdiutil_with_umask_077_for_that_child_only(tmp_path, role, stem):
    fr = FakeRunner()
    exp = _exp(tmp_path)
    um = os.umask(0o022)
    os.umask(um)
    img = X.create_image(exp, role, "darwin", size=(5 << 20) + 1, inodes=0, owner_uid=os.getuid(), seams=fr.seams())
    (create, cu), (attach, au) = fr.calls
    assert create[:2] == (X.HDIUTIL, "create") and cu == 0o077 and au is None
    assert ("-size", "6m") == create[create.index("-size"):create.index("-size") + 2] and img.size == 6 << 20
    assert {"-fs", "APFS", "-type", "SPARSE"} <= set(create) and create[-1] == f"{exp}/{stem}.sparseimage"
    assert {"-nobrowse", "-owners", "on"} <= set(attach) and attach[attach.index("-mountpoint") + 1] == img.mount
    assert stat.S_IMODE(os.lstat(img.path).st_mode) == 0o600
    assert os.umask(um) == um                                        # the process umask never moved


def test_darwin_create_refuses_an_image_file_not_0600(tmp_path):
    fr = FakeRunner(ignore_umask=True)                                # a create that ignored the child's umask
    exp = _exp(tmp_path)
    with pytest.raises(Refused) as ei:
        X.create_image(exp, "importer", "darwin", size=1 << 20, inodes=0, owner_uid=os.getuid(), seams=fr.seams())
    assert ei.value.name == "export-image-unavailable" and "644" in ei.value.detail
    assert not os.path.lexists(X.image_path(exp, "importer", "darwin")) and not fr.mounted


@pytest.mark.parametrize("host,verb", [("darwin", "hdiutil.attach"), ("darwin", "hdiutil.create"),
                                       ("linux", "mkfs.ext4"), ("linux", "mount")])
def test_an_attach_failure_is_image_unavailable_and_leaves_nothing(tmp_path, host, verb):
    fr = FakeRunner(fail={verb})
    exp = _exp(tmp_path)
    with pytest.raises(Refused) as ei:
        X.create_image(exp, "importer", host, size=300 << 20, inodes=5120, owner_uid=os.getuid(), seams=fr.seams())
    assert ei.value.name == "export-image-unavailable"
    assert not os.path.lexists(X.image_path(exp, "importer", host)) and not fr.mounted


def test_linux_small_image_is_tmpfs_large_is_a_loop_ext4(tmp_path):
    fr = FakeRunner()
    exp = _exp(tmp_path)
    small = X.create_image(exp, "importer", "linux", size=X.TMPFS_MAX_BYTES, inodes=7000, owner_uid=os.getuid(),
                           seams=fr.seams())
    (m, _),= fr.calls
    assert small.path is None and m[:3] == (X.MOUNT, "-t", "tmpfs") and m[-2:] == ("tmpfs", small.mount)
    assert set(m[4].split(",")) == {f"size={X.TMPFS_MAX_BYTES}", "nr_inodes=7000", "mode=0700", "nodev", "nosuid",
                                    "noexec"}
    assert not os.path.lexists(os.path.join(exp, "work.img"))
    fr2 = FakeRunner()
    exp2 = _exp(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None
    big = X.create_image(exp2, "importer", "linux", size=X.TMPFS_MAX_BYTES + 1, inodes=7000, owner_uid=os.getuid(),
                         seams=fr2.seams())
    (mk, _), (mt, _) = fr2.calls
    st = os.lstat(big.path)
    assert big.path == f"{exp2}/work.img" and stat.S_IMODE(st.st_mode) == 0o600 and st.st_size == X.TMPFS_MAX_BYTES + 1
    assert mk == (X.MKFS_EXT4, "-q", "-F", "-m", "0", "-O", "^has_journal", "-I", "256", "-N", "7000", big.path)
    assert mt == (X.MOUNT, "-o", "loop,nodev,nosuid,noexec", big.path, big.mount)


def test_detach_ebusy_is_the_detach_failure_path_never_lazy(tmp_path):
    fr = FakeRunner()
    exp = _exp(tmp_path)
    img = X.create_image(exp, "importer", "linux", size=300 << 20, inodes=10, owner_uid=os.getuid(), seams=fr.seams())
    fr.detach_rc = 32
    assert X.detach_image(img, fr.seams()) is False
    assert img.mount in fr.mounted and os.path.lexists(img.path)          # left for the next seal-in
    assert all("-l" not in argv for argv, _ in fr.calls)


def test_clear_stale_image(tmp_path):
    fr = FakeRunner()
    exp = _exp(tmp_path)
    X.create_image(exp, "importer", "darwin", size=1 << 20, inodes=0, owner_uid=os.getuid(), seams=fr.seams())
    X.clear_stale_image(exp, "importer", "darwin", fr.seams())
    assert not fr.mounted and not os.path.lexists(X.image_path(exp, "importer", "darwin"))
    X.create_image(exp, "importer", "darwin", size=1 << 20, inodes=0, owner_uid=os.getuid(), seams=fr.seams())
    fr.detach_rc = 16
    with pytest.raises(Refused) as ei:
        X.clear_stale_image(exp, "importer", "darwin", fr.seams())
    assert ei.value.name == "export-image-unavailable" and ".work" in ei.value.detail


class _Sv:
    def __init__(self, avail):
        self.f_bavail, self.f_frsize = avail, 1


def test_host_short_reading(tmp_path):
    p = tmp_path / "work.img"
    with open(p, "wb") as f:
        f.truncate(1 << 30)                                                # sparse: almost nothing allocated
    img = X.Image("linux", str(p), str(tmp_path / ".work"), 1 << 30)
    alloc = os.stat(p).st_blocks * 512
    assert X.host_short(str(tmp_path), img, statvfs=lambda _p: _Sv((1 << 30) - alloc - 1)) is True
    assert X.host_short(str(tmp_path), img, statvfs=lambda _p: _Sv((1 << 30) - alloc)) is False
    assert X.host_short(str(tmp_path), X.Image("linux", None, "m", 1 << 30), statvfs=lambda _p: _Sv(0)) is False


# --- the Linux run leaf through a fake cgroupfs ------------------------------------------------------------------

class FakeCg:
    """A cgroupfs as paths: ``mkdir`` makes a dir with its interface files (``memory.swap.max`` only when ``swap``),
    ``write`` fails with ``fail[path]`` when set, ``rmdir`` fails EBUSY ``busy`` times."""

    def __init__(self, *, swap=True, fail=None, busy=0, oom=0):
        self.dirs, self.files, self.swap = set(), {}, swap
        self.fail, self.busy, self.oom = dict(fail or {}), busy, oom
        self.log = Log()
        self.t = 0.0
        self.rmdirs = []

    def mkdir(self, p):
        if p in self.dirs:
            raise FileExistsError(errno.EEXIST, "exists", p)
        self.dirs.add(p)
        names = ["cgroup.subtree_control", "memory.max", "pids.max", "memory.events.local"]
        names += ["memory.swap.max"] if self.swap else []
        for n in names:
            self.files[f"{p}/{n}"] = f"oom 0\noom_kill {self.oom}\n" if n == "memory.events.local" else ""

    def write(self, p, text):
        if p in self.fail:
            raise OSError(self.fail[p], os.strerror(self.fail[p]), p)
        if p not in self.files:
            raise FileNotFoundError(errno.ENOENT, "absent", p)
        self.files[p] = text
        self.log(f"write {p}={text}")

    def read(self, p):
        if p in self.fail:
            raise OSError(self.fail[p], os.strerror(self.fail[p]), p)
        if p not in self.files:
            raise FileNotFoundError(errno.ENOENT, "absent", p)
        return self.files[p]

    def rmdir(self, p):
        self.rmdirs.append(self.t)
        if p not in self.dirs:
            raise FileNotFoundError(errno.ENOENT, "absent", p)
        if self.busy:
            self.busy -= 1
            raise OSError(errno.EBUSY, "busy", p)
        self.dirs.discard(p)
        for k in [k for k in self.files if k.startswith(p + "/")]:
            del self.files[k]

    def exists(self, p):
        return p in self.dirs or p in self.files

    def sleep(self, s):
        self.t += s

    def seams(self, *, clock=None):
        return X.LeafSeams(read=self.read, write=self.write, mkdir=self.mkdir, rmdir=self.rmdir, exists=self.exists,
                           clock=clock or (lambda: self.t), sleep=self.sleep, log=self.log)


LEAF = "/cg/levain.slice/export.scope/run"


def _writes(cg):
    return [e.split(" ", 1)[1].rsplit("/", 1)[1] for e in cg.log.events if e.startswith("write ")]


def test_make_leaf_writes_memory_max_first_then_swap_then_pids():
    cg = FakeCg()
    X.make_leaf(LEAF, memory_max=111, pids_max=64, seams=cg.seams())
    assert _writes(cg) == ["memory.max=111", "memory.swap.max=0", "pids.max=64"]


def test_make_leaf_skips_an_absent_swap_file_and_proceeds():
    cg = FakeCg(swap=False)
    X.make_leaf(LEAF, memory_max=111, pids_max=64, seams=cg.seams())
    assert _writes(cg) == ["memory.max=111", "pids.max=64"] and "memory.swap.max absent, skipped" in cg.log.events


@pytest.mark.parametrize("swap,name", [(False, "memory.max"), (True, "memory.max"), (True, "memory.swap.max"),
                                       (True, "pids.max")])
def test_a_failed_limit_write_is_image_unavailable(swap, name):
    cg = FakeCg(swap=swap, fail={f"{LEAF}/{name}": errno.EACCES})
    with pytest.raises(Refused) as ei:
        X.make_leaf(LEAF, memory_max=1, pids_max=1, seams=cg.seams())
    assert ei.value.name == "export-image-unavailable" and name in ei.value.detail


def test_enable_controllers_writes_the_chain_and_a_failure_refuses():
    cg = FakeCg()
    X.enable_controllers("importer", cg.seams(), cgroup_root="/cg/levain.slice")
    assert [e for e in cg.log.events if e.startswith("write")] == [
        "write /cg/levain.slice/cgroup.subtree_control=+memory +pids",
        "write /cg/levain.slice/export.scope/cgroup.subtree_control=+memory +pids"]
    cg2 = FakeCg(fail={"/cg/levain.slice/export.scope/cgroup.subtree_control": errno.EINVAL})
    with pytest.raises(Refused) as ei:
        X.enable_controllers("importer", cg2.seams(), cgroup_root="/cg/levain.slice")
    assert ei.value.name == "export-image-unavailable"


def test_stale_leaf_ebusy_twice_then_removed():
    cg = FakeCg(busy=2)
    cg.mkdir(LEAF)
    assert X.remove_stale_leaf(LEAF, cg.seams()) == 3 and not cg.exists(LEAF)


def test_stale_leaf_still_busy_after_its_budget_refuses_naming_the_leaf():
    cg = FakeCg(busy=10 ** 6)
    cg.mkdir(LEAF)
    with pytest.raises(Refused) as ei:
        X.remove_stale_leaf(LEAF, cg.seams())
    assert ei.value.name == "export-image-unavailable" and f"stale run leaf at {LEAF}" in ei.value.detail
    assert len(cg.rmdirs) >= X.LEAF_RMDIR_MIN_ATTEMPTS and cg.rmdirs[-1] >= X.LEAF_RMDIR_BUDGET_S
    assert all(b - a == X.LEAF_RMDIR_POLL_S for a, b in zip(cg.rmdirs, cg.rmdirs[1:]))     # paced, never a spin


@pytest.mark.parametrize("busy,removed", [(2, True), (3, False)])
def test_a_clock_jump_past_the_deadline_still_gets_three_attempts(busy, removed):
    cg = FakeCg(busy=busy)
    cg.mkdir(LEAF)
    ticks = iter([0.0, 0.0] + [100.0] * 50)                    # start, attempt 1 on time, then the clock jumps
    seams = cg.seams(clock=lambda: next(ticks))
    if removed:
        assert X.remove_stale_leaf(LEAF, seams) == 3
    else:
        with pytest.raises(Refused):
            X.remove_stale_leaf(LEAF, seams)
        assert len(cg.rmdirs) == 3                             # >= 3 attempts, two of them after the deadline


@pytest.mark.parametrize("text,want", [("oom 0\noom_kill 1\n", 1), ("oom_kill 0\n", 0), ("oom 3\n", None),
                                       ("oom_kill x\n", None)])
def test_read_oom_kill(text, want):
    cg = FakeCg()
    cg.files[f"{LEAF}/memory.events.local"] = text
    assert X.read_oom_kill(LEAF, cg.seams()) == want
    assert X.read_oom_kill("/cg/gone", cg.seams()) is None


# --- the macOS footprint watchdog ---------------------------------------------------------------------------------

def _wait(pred, s=5.0):
    import time
    end = time.monotonic() + s
    while not pred() and time.monotonic() < end:
        time.sleep(0.005)


def test_watchdog_ends_the_domain_past_the_cap():
    ended = []
    fps = {10: 100, 11: None, 12: 5000}
    w = X.FootprintWatchdog(7, 1000, lambda: ended.append(1), interval=0.01, pids=lambda uid: [10, 11, 12],
                            footprint=fps.get)
    w.start()
    _wait(lambda: ended)
    w.stop()
    assert w.killed and ended == [1] and not w.blind and w.peak == 5100     # the sum over the domain (V30)


def test_watchdog_below_the_cap_never_kills():
    w = X.FootprintWatchdog(7, 1000, lambda: pytest.fail("ended"), interval=0.01, pids=lambda uid: [1, 2],
                            footprint=lambda pid: 499)
    w.start()
    _wait(lambda: w.peak == 998)
    w.stop()
    assert not w.killed and not w.blind and w.peak == 998                   # the sum stays under the cap


def test_a_watchdog_that_cannot_sample_is_blind():
    def boom(uid):
        raise OSError(errno.EPERM, "seam")
    w = X.FootprintWatchdog(7, 1000, lambda: None, interval=0.01, pids=boom)
    w.start()
    _wait(lambda: w.blind)
    w.stop()
    assert w.blind and not w.killed


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc")
def test_phys_footprint_reads_this_process():
    fp = X.phys_footprint(os.getpid())
    assert fp is not None and fp > 1 << 20
    assert X.phys_footprint(2 ** 22 + 12345) is None                   # no such pid


# --- the domain prover --------------------------------------------------------------------------------------------

def test_domain_prover_darwin_is_end_domain_of_the_uid(monkeypatch):
    from levain.slot import proc as P
    seen = []
    monkeypatch.setattr(P, "end_domain", lambda uid, bound, children=(), *, seams=None:
                        (seen.append((uid, bound, tuple(children))) or (PROVEN, frozenset())))
    assert X.export_domain_prover("darwin", "importer", 299, bound_s=3.0)([42]) is PROVEN
    assert seen == [(299, 3.0, (42,))]


def test_domain_prover_linux_kills_the_scope_then_sweeps_the_uid(monkeypatch):
    from levain.slot import proc as P
    calls = []

    class FakeCP:
        def __init__(self, cgroup, seams):
            self.cgroup, self.seams = cgroup, seams
            calls.append(("cg", cgroup))

        def freeze(self, uid, b):
            calls.append(("freeze", uid))
            return False                                               # best effort: never names the answer

        def kill(self, uid, b):
            calls.append(("kill", uid))
            return self.populated0

    monkeypatch.setattr(P, "CgroupProcs", FakeCP)
    monkeypatch.setattr(X, "_thaw_scope", lambda c, b: calls.append(("thaw",)))
    monkeypatch.setattr(P, "end_domain", lambda uid, bound, children=(), *, seams=None:
                        (calls.append(("sweep", uid, tuple(children))) or (PROVEN, frozenset())))
    FakeCP.populated0 = True
    assert X.export_domain_prover("linux", "verifier", 298)([5]) is PROVEN
    assert calls == [("cg", X.scope_path("verifier")), ("freeze", 298), ("kill", 298), ("thaw",),
                     ("sweep", 298, (5,))]
    FakeCP.populated0 = False
    del calls[:]
    d = X.export_domain_prover("linux", "importer", 299)(())
    assert not d.proven and d.name == "kill-unproven" and ("thaw",) not in calls     # live tasks stay frozen


# --- SealedRun: seal-in, seal-out, teardown -----------------------------------------------------------------------

SESSION = "s-0000000000000001"
STAGED = SESSION + ".new"


class Rig:
    def __init__(self, tmp_path, *, host="linux", role="importer", proofs=None, cg=None, fr=None, older=False,
                 watchdog=None, publish_kw=None):
        self.log = Log()
        self.exp = _exp(tmp_path)
        self.cls = tmp_path / "operator"
        self.cls.mkdir()
        if older:
            (self.cls / SESSION).mkdir()
            (self.cls / SESSION / "bundle").write_bytes(b"OLD")
        self.proofs = list(proofs or [])
        self.fr = fr or FakeRunner()
        self.cg = cg or FakeCg()
        self.cg.log = self.log
        self.children = []

        def prove(children):
            self.children.append(tuple(children))
            self.log("prove")
            return self.proofs.pop(0) if self.proofs else PROVEN

        leaf = self.cg.seams()
        self.facts = X.SealFacts(host=host, role_key=role, export_dir=self.exp, uid=os.getuid(),
                                 image_bytes=300 << 20, image_inodes=99, memory_cap=1 << 30, pids_max=64)
        self.run = X.SealedRun(self.facts, X.SealSeams(
            prove=prove, image=self.fr.seams(self.log), leaf=leaf, statvfs=lambda _p: _Sv(1 << 50),
            publish=X.PublishSeams(log=self.log, **(publish_kw or {})), watchdog=watchdog, log=self.log))

    def seal_in(self):
        cfd = _dfd(self.cls)
        try:
            self.run.seal_in(cfd, STAGED if self.facts.role_key == "importer" else "verify.new")
        finally:
            os.close(cfd)

    @property
    def mount(self):
        return X.mount_path(self.exp, self.facts.role_key)


def _patch_leaf(monkeypatch, root="/cg/levain.slice"):
    from levain.slot import wall
    monkeypatch.setattr(wall, "CGROUP_ROOT", root)


def test_linux_seal_in_runs_step_6_in_order(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    cg = FakeCg()
    cg.mkdir(LEAF)                                                    # a stale leaf a crashed helper left
    r = Rig(tmp_path, cg=cg, older=True)
    (tmp_path / "operator" / STAGED).mkdir()                          # a stale staged name it can delete
    r.seal_in()
    ev = r.log.events
    order = ["prove", "stale leaf removed after 1 attempt(s)", "write /cg/levain.slice/cgroup.subtree_control=+memory +pids",
             f"write {LEAF}/memory.max={1 << 30}", "leaf made", "image attached", f"stale-staging deleted {STAGED}",
             "probe exchange ok", "seal-in done"]
    assert [e for e in ev if e in order] == order
    assert r.mount in r.fr.mounted and cg.exists(LEAF) and not (tmp_path / "operator" / STAGED).exists()
    assert sorted(os.listdir(tmp_path / "operator")) == [SESSION]


def test_seal_in_with_the_domain_unproven_makes_nothing(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, proofs=[Domain(False, "processes-remain", "pid 9")])
    with pytest.raises(Refused) as ei:
        r.seal_in()
    assert ei.value.name == "export-domain-unproven" and "processes-remain" in ei.value.detail
    assert r.fr.calls == [] and not r.cg.dirs


def test_seal_in_stale_staging_refuses_before_the_import_and_tears_down(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)

    def no_delete(dfd, name):
        if name.endswith(".new"):
            raise OSError(errno.EPERM, "seam")
        return False
    r = Rig(tmp_path, publish_kw={"delete": no_delete})
    with pytest.raises(Refused) as ei:
        r.seal_in()
    assert (ei.value.name, ei.value.detail) == ("importer-failed:publish", "stale-staging")
    assert L_IN(ei.value, "L/export/<class>/<s>.new")
    assert not r.fr.mounted and not os.path.lexists(X.image_path(r.exp, "importer", "linux"))
    assert not r.cg.exists(LEAF)
    ev = r.log.events
    assert ev.index("fds closed") > ev.index("image attached") and "image detached" in ev


def L_IN(refused, text):
    return text in str(refused.__cause__)


@pytest.mark.parametrize("kind", ["EINVAL", "ENOTSUP", "ENOSYS", "symbol"])
def test_seal_in_probe_refused_is_exchange_unsupported_and_nothing_stays_attached(tmp_path, monkeypatch, kind):
    _patch_leaf(monkeypatch)

    def refuse(*_a):
        raise X.ExchangeUnsupported(kind)
    r = Rig(tmp_path, older=True, publish_kw={"rename2": refuse})
    with pytest.raises(Refused) as ei:
        r.seal_in()
    assert (ei.value.name, ei.value.detail) == ("importer-failed:publish", "exchange-unsupported")
    assert not r.fr.mounted and sorted(os.listdir(tmp_path / "operator")) == [SESSION]
    assert (tmp_path / "operator" / SESSION / "bundle").read_bytes() == b"OLD"


def _ok_obs(reading, *, code=0):
    return X.RunObservation(exit="exit", code=code, memory_killed=reading.memory_killed,
                            memory_unreadable=reading.memory_unreadable, host_short=reading.host_short,
                            line=_line() if code == 0 else
                            IMP.result_line(IMP.Progress(terminal=True, entries=3, bytes=10), refused=None,
                                            detail="", enospc=False, result=None))


def test_linux_seal_out_reads_the_late_oom_kill_after_the_proof_and_classifies_memory(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path)
    r.seal_in()
    r.cg.files[f"{LEAF}/memory.events.local"] = "oom_kill 1\n"         # a git child OOM-killed after the line
    reading = r.run.seal_out([4242])
    out = X.classify(_ok_obs(reading))
    assert reading.memory_killed and (out.success, out.name) == (False, "adopt-cap:memory")
    ev = r.log.events
    i = ev.index("seal-out domain proven empty")
    assert ev[i + 1] == "events read oom_kill=1" and ev[i + 2] == "leaf removed" and ev[i + 3].startswith("host reading")
    assert r.children[-1] == (4242,)


def test_linux_seal_out_unreadable_events_ranks_first_even_on_exit_0(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path)
    r.seal_in()
    del r.cg.files[f"{LEAF}/memory.events.local"]                      # the leaf's controllers dropped mid-run
    reading = r.run.seal_out()
    out = X.classify(_ok_obs(reading))
    assert reading.memory_unreadable and (out.success, out.name) == (False, "export-image-unavailable")


def test_a_fresh_leaf_after_a_memory_kill_starts_at_zero(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, cg=FakeCg(oom=0))
    r.seal_in()
    r.cg.files[f"{LEAF}/memory.events.local"] = "oom_kill 1\n"
    assert r.run.seal_out().memory_killed
    r.run.teardown()
    r2 = Rig(tmp_path / "2" if (tmp_path / "2").mkdir() is None else None, cg=r.cg)
    r2.seal_in()
    reading = r2.run.seal_out()
    out = X.classify(_ok_obs(reading, code=1))
    assert not reading.memory_killed and out.name == "importer-failed:exit"


def test_seal_out_unproven_leaves_the_image_for_the_next_seal_in(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, proofs=[PROVEN, Domain(False, "respawn-after-bootout")])
    r.seal_in()
    with pytest.raises(Refused) as ei:
        r.run.seal_out()
    assert ei.value.name == "export-domain-unproven"
    r.run.teardown()
    assert r.mount in r.fr.mounted and os.path.lexists(X.image_path(r.exp, "importer", "linux"))
    nxt = Rig.__new__(Rig)
    nxt.__dict__.update(r.__dict__)
    nxt.run = X.SealedRun(r.facts, r.run.s)
    nxt.cls = r.cls
    nxt.seal_in()                                            # force-detaches and unlinks the stale one
    assert "stale image removed" in r.log.events and r.mount in r.fr.mounted   # the new run's image


def test_darwin_seal_out_reads_the_watchdog_flag_after_stopping_it(tmp_path):
    class W:
        def __init__(self, uid, cap, end, log):
            self.killed, self.blind, self.log = False, False, log

        def start(self):
            self.log("w start")

        def stop(self):
            self.killed = True                                          # a kill after the result line
            self.log("w stop")
    r = Rig(tmp_path, host="darwin", watchdog=W)
    r.seal_in()
    r.run.start_watchdog()
    reading = r.run.seal_out()
    ev = r.log.events
    assert ev.index("prove", ev.index("w start")) < ev.index("w stop") < ev.index(
        "seal-out domain proven empty") < ev.index("watchdog flag killed=True blind=False")    # B-2: kill, then stop
    assert X.classify(_ok_obs(reading)).name == "adopt-cap:memory"


def test_darwin_seal_out_without_a_watchdog_cannot_establish_memory(tmp_path):
    r = Rig(tmp_path, host="darwin")
    r.seal_in()
    assert r.run.seal_out().memory_unreadable


def test_linux_runs_no_watchdog(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, watchdog=lambda *a, **k: pytest.fail("a Linux watchdog"))
    r.seal_in()
    r.run.start_watchdog()
    assert r.run.watchdog is None


def test_a_good_run_publishes_and_the_detach_happens_before_the_commit(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path)
    r.seal_in()
    reading = r.run.seal_out()
    w = _work(tmp_path / "w" if (tmp_path / "w").mkdir() is None else None, SESSION)
    wd, cd = _dfd(w), _dfd(r.cls)
    r.run.fds.append(wd)
    try:
        result, pub = X.publish_if_success(X.classify(_ok_obs(reading)), wd, cd, SESSION, object_format="sha1",
                                           owner_uid=os.getuid(), caps=CAPS, seams=r.run.s.publish,
                                           before_commit=r.run.release_image)
    finally:
        os.close(cd)
    r.run.teardown()
    ev = r.log.events
    assert ev.index("fsync staged") < ev.index("image detached") < ev.index("image unlinked") < ev.index("noreplace")
    assert (r.cls / SESSION / "bundle").read_bytes() == _bundle(SESSION) and not r.fr.mounted


def test_a_detach_failure_after_a_good_run_still_publishes_and_teardown_retries(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path)
    r.seal_in()
    reading = r.run.seal_out()
    w = _work(tmp_path / "w" if (tmp_path / "w").mkdir() is None else None, SESSION)
    wd, cd = _dfd(w), _dfd(r.cls)
    r.fr.detach_rc = 32                                                  # EBUSY while an output fd is open
    try:
        X.publish_if_success(X.classify(_ok_obs(reading)), wd, cd, SESSION, object_format="sha1",
                             owner_uid=os.getuid(), caps=CAPS, seams=r.run.s.publish,
                             before_commit=r.run.release_image)
    finally:
        os.close(cd)
        os.close(wd)
    assert (r.cls / SESSION / "bundle").read_bytes() == _bundle(SESSION)        # published, image left
    assert "image left for the next seal-in" in r.log.events and r.mount in r.fr.mounted
    r.fr.detach_rc = 0
    r.run.teardown()                                                     # fds closed: the retry detaches
    assert not r.fr.mounted and not os.path.lexists(X.image_path(r.exp, "importer", "linux"))


def test_teardown_order_is_fds_then_umount_then_unlink(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path)
    r.seal_in()
    r.run.seal_out()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    r.run.fds.append(fd)
    r.run.teardown()
    ev = r.log.events
    assert ev.index("seal-out domain proven empty") < ev.index("fds closed") < ev.index("image detached") < ev.index(
        "image unlinked")
    with pytest.raises(OSError):
        os.fstat(fd)


def test_a_hostile_tree_in_the_image_goes_with_the_file_nothing_walked(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path)
    r.seal_in()
    d = r.mount
    for _ in range(50):                                                  # a deep chain (5000 is 5g's on a real fs)
        d = os.path.join(d, "d")
        os.mkdir(d)
    r.run.seal_out()
    r.run.teardown()
    assert not os.path.lexists(X.image_path(r.exp, "importer", "linux"))
    assert r.fr.calls[-1][0] == (X.UMOUNT, r.mount)                     # an umount, not a walk


def test_verifier_facts_use_the_verify_names(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, role="verifier")
    (tmp_path / "operator" / "verify.new").mkdir()
    r.seal_in()
    assert r.mount.endswith("/.verify") and "/cg/levain.slice/verify.scope/run" in r.cg.dirs
    assert "stale-staging deleted verify.new" in r.log.events
    assert X.classify(_ok_obs(r.run.seal_out()), X.VERIFIER).success


# =================================================================================================================
# 5c-ii-b L3 r1 (triage ``1022-18/adopt/L3_5c2b_r1_TRIAGE.md``: B-1 .. B-7) and the head-ruled 5c-ii-a defects D1, D2.
# Each row was run against 251133df first (raw: ``1022-18/adopt/l3r1c_failfirst.txt``).
# =================================================================================================================

SCOPE = "/sys/fs/cgroup/levain.slice/export.scope"


class FsScope:
    """A cgroupfs scope for :class:`levain.slot.proc.CgroupProcs`: ``cgroup.freeze`` / ``cgroup.kill`` writes move
    ``cgroup.events``; a child made under the scope is born frozen while the scope's ``cgroup.freeze`` reads 1."""

    def __init__(self, *, fail_thaw=None, stuck_frozen=False):
        self.freeze, self.populated, self.frozen = "0", "1", "0"
        self.fail_thaw, self.stuck = fail_thaw, stuck_frozen
        self.t = 0.0
        self.writes = []

    def write(self, p, text):
        self.writes.append((p.rsplit("/", 1)[1], text))
        if p == f"{SCOPE}/cgroup.freeze":
            if text == "0" and self.fail_thaw:
                raise OSError(self.fail_thaw, os.strerror(self.fail_thaw), p)
            self.freeze = text
            self.frozen = "1" if text == "1" or self.stuck else "0"
        elif p == f"{SCOPE}/cgroup.kill":
            self.populated = "0"
        else:
            raise FileNotFoundError(errno.ENOENT, "absent", p)

    def read(self, p):
        if p == f"{SCOPE}/cgroup.events":
            return f"populated {self.populated}\nfrozen {self.frozen}\n"
        raise FileNotFoundError(errno.ENOENT, "absent", p)

    def child_born_frozen(self):
        return self.freeze == "1"

    def seams(self):
        from levain.slot import proc as P
        return P.ProcSeams(listing=lambda: [], probe=lambda pid: None, pin=lambda pid: pid,
                           send=lambda h, s: None, read=self.read, write=self.write, clock=lambda: self.t,
                           sleep=self._sleep, self_uids=lambda: (0,))

    def _sleep(self, s):
        self.t += s


def _sweep_ok(monkeypatch):
    from levain.slot import proc as P
    monkeypatch.setattr(P, "end_domain", lambda uid, bound, children=(), *, seams=None: (PROVEN, frozenset()))


def test_b1_the_linux_scope_is_thawed_after_the_kill_and_the_next_child_is_not_born_frozen(monkeypatch):
    _sweep_ok(monkeypatch)
    fs = FsScope()
    prove = X.export_domain_prover("linux", "importer", 299, seams=fs.seams())
    for _ in range(3):                                   # seal-in, seal-out, the next run's seal-in
        assert prove(()) is PROVEN
        assert fs.freeze == "0" and fs.frozen == "0" and not fs.child_born_frozen()
    assert fs.writes[:3] == [("cgroup.freeze", "1"), ("cgroup.kill", "1"), ("cgroup.freeze", "0")]


@pytest.mark.parametrize("fail_thaw,stuck", [(errno.EACCES, False), (None, True)])
def test_b1_a_thaw_that_cannot_be_confirmed_fails_closed(monkeypatch, fail_thaw, stuck):
    _sweep_ok(monkeypatch)
    fs = FsScope(fail_thaw=fail_thaw, stuck_frozen=stuck)
    with pytest.raises(Refused) as ei:
        X.export_domain_prover("linux", "importer", 299, seams=fs.seams())(())
    assert ei.value.name == "export-image-unavailable" and "cgroup.freeze" in ei.value.detail


def _darwin_rig(tmp_path, footprint, prove):
    import dataclasses
    r = Rig(tmp_path, host="darwin", watchdog=lambda uid, cap, end, log: X.FootprintWatchdog(
        uid, cap, end, interval=0.005, pids=lambda u: [77], footprint=footprint, log=log))
    r.run.s = dataclasses.replace(r.run.s, prove=prove)
    return r


def test_b2_the_watchdog_samples_through_the_seal_out_kill_and_the_two_proofs_never_overlap(tmp_path):
    import threading
    import time
    state = {"crossing": False, "inflight": 0, "max": 0, "calls": []}
    lock = threading.Lock()

    def prove(children):
        with lock:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
        state["calls"].append(tuple(children))
        if children == (4242,):                          # seal-out's: the importer's child crosses the cap meanwhile
            state["crossing"] = True
            time.sleep(0.2)
        with lock:
            state["inflight"] -= 1
        return PROVEN

    r = _darwin_rig(tmp_path, lambda pid: (2 << 30) if state["crossing"] else 1, prove)
    r.seal_in()
    r.run.start_watchdog()
    reading = r.run.seal_out([4242])
    assert reading.memory_killed and not reading.memory_unreadable
    assert X.classify(_ok_obs(reading)).name == "adopt-cap:memory"
    assert state["max"] == 1                             # one lock: never two end_domain calls on one uid
    assert () in state["calls"][1:]                      # the watchdog's own kill ran, serialized


def test_b2_the_join_bound_covers_two_serialized_proofs_and_a_join_timeout_is_blind():
    import inspect
    import threading
    from levain.slot import proc as P
    assert inspect.signature(X.FootprintWatchdog.stop).parameters["timeout"].default == X.WATCHDOG_JOIN_S
    assert X.WATCHDOG_JOIN_S > 2 * P.V12K_SECONDS
    gate = threading.Event()
    w = X.FootprintWatchdog(7, 10, gate.wait, interval=0.005, pids=lambda u: [1], footprint=lambda p: 99)
    w.start()
    _wait(lambda: w.killed)
    w.stop(timeout=0.05)                                 # its end() is still inside the domain kill
    gate.set()
    assert w.blind and w.killed


def test_b3_an_unreadable_swap_file_is_not_an_absent_one():
    cg = FakeCg(fail={f"{LEAF}/memory.swap.max": errno.EACCES})
    seams = cg.seams()
    import dataclasses
    seams = dataclasses.replace(seams, exists=lambda p: False if p.endswith("memory.swap.max") else cg.exists(p))
    with pytest.raises(Refused) as ei:
        X.make_leaf(LEAF, memory_max=1, pids_max=1, seams=seams)
    assert ei.value.name == "export-image-unavailable" and "memory.swap.max: EACCES" in ei.value.detail


class _FakeLib:
    def __init__(self, err):
        self.err = err

    def proc_pid_rusage(self, pid, flavor, buf):
        import ctypes
        ctypes.set_errno(self.err)
        return -1


@pytest.mark.parametrize("err,want", [(errno.ESRCH, None), (errno.EPERM, OSError), (errno.EIO, OSError)])
def test_b4_phys_footprint_keeps_the_errno(monkeypatch, err, want):
    monkeypatch.setattr(X, "_LIBPROC", [_FakeLib(err)])
    if want is None:
        assert X.phys_footprint(5) is None
    else:
        with pytest.raises(OSError) as ei:
            X.phys_footprint(5)
        assert ei.value.errno == err


def test_b4_a_footprint_read_failure_makes_the_watchdog_blind():
    def fp(pid):
        raise OSError(errno.EPERM, "seam")
    w = X.FootprintWatchdog(7, 1000, lambda: pytest.fail("ended"), interval=0.005, pids=lambda u: [1], footprint=fp)
    w.start()
    _wait(lambda: w.blind)
    w.stop()
    assert w.blind and not w.killed


def _listing(monkeypatch, procs):
    from levain.slot import proc as P

    class Seams:
        def listing(self):
            return procs
    monkeypatch.setattr(P, "default_seams", lambda host: Seams())


def test_b4_only_the_export_uids_pids_count_and_an_unknown_owner_is_blindness(monkeypatch):
    from levain.slot.proc import Proc, RUN, UNKNOWN
    _listing(monkeypatch, [Proc(10, (299, 299, 299), 1, RUN), Proc(11, (501, 501, 501), 1, RUN)])
    assert list(X.uid_pids(299)) == [10]
    _listing(monkeypatch, [Proc(10, (299, 299, 299), 1, RUN), Proc(12, (), 0, UNKNOWN)])
    with pytest.raises(X.WatchdogBlind):
        X.uid_pids(299)


def test_b4_the_cap_is_on_the_sum_over_the_domain():
    ended = []
    w = X.FootprintWatchdog(7, 1000, lambda: ended.append(1), interval=0.005, pids=lambda u: [1, 2],
                            footprint=lambda p: 600)
    w.start()
    _wait(lambda: ended)
    w.stop()
    assert w.killed and w.peak == 1200 and not w.blind


def test_b5_a_verifier_success_never_reaches_the_importer_shaped_publish(tmp_path):
    w, c = _work(tmp_path), _class_dir(tmp_path, older=False)
    wd, cd = _dfd(w), _dfd(c)
    try:
        ok = X.classify(X.RunObservation(exit="exit", code=0, line=_line()))
        with pytest.raises(Refused) as ei:
            X.publish_if_success(ok, wd, cd, S, object_format="sha1", owner_uid=os.getuid(), caps=CAPS,
                                 seams=X.PublishSeams(), role=X.VERIFIER)
    finally:
        os.close(wd)
        os.close(cd)
    assert (ei.value.name, ei.value.detail) == ("verifier-failed:publish", "verifier-unbuilt")
    assert os.listdir(c) == []


def test_b6_a_leaf_whose_limit_write_failed_is_removed_by_the_teardown(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, cg=FakeCg(fail={f"{LEAF}/memory.max": errno.EINVAL}))
    with pytest.raises(Refused) as ei:
        r.seal_in()
    assert ei.value.name == "export-image-unavailable"
    assert LEAF not in r.cg.dirs and "leaf removed" in r.log.events


def test_b7_a_failed_class_fsync_keeps_the_older_tree_at_the_staged_name(tmp_path):
    def fault(p):
        if p == "fsync:class":
            raise OSError(errno.EIO, os.strerror(errno.EIO))
    log = Log()
    c, (_r, pub) = _publish(tmp_path, seams=X.PublishSeams(log=log, fault=fault), older=True)
    assert (c / S / "bundle").read_bytes() == _bundle()
    assert (c / (S + ".new") / "bundle").read_bytes() == b"OLD"      # left for the next seal-in's stale delete
    assert "delete older" not in log.events and "delete older skipped: the class dir is not synced" in log.events
    assert len(pub.warnings) == 1 and "L/export/<class>/<s>.new" in pub.warnings[0]


def _no_replace_seam(*, ignore=False):
    def rename2(fd1, n1, fd2, n2, flag):
        if flag == X.NOREPLACE:
            if ignore:
                return None                              # a filesystem that drops the flag: the rename "succeeds"
            raise X.ExchangeUnsupported("ENOTSUP")
        X.rename2(fd1, n1, fd2, n2, flag)
    return rename2


@pytest.mark.parametrize("ignore", [False, True])
def test_d1_the_probe_also_tests_the_no_replace_rename(tmp_path, ignore):
    c = _class_dir(tmp_path, older=False)                # a FIRST publish: no older tree
    cd = _dfd(c)
    try:
        with pytest.raises(Refused) as ei:
            X.probe_exchange(cd, X.IMPORTER, X.PublishSeams(rename2=_no_replace_seam(ignore=ignore)))
    finally:
        os.close(cd)
    assert (ei.value.name, ei.value.detail) == ("importer-failed:publish", "exchange-unsupported")
    assert os.listdir(c) == []


def test_d1_seal_in_always_probes_a_first_publish_too(tmp_path, monkeypatch):
    _patch_leaf(monkeypatch)
    r = Rig(tmp_path, older=False)
    cfd = _dfd(r.cls)
    try:
        r.run.seal_in(cfd, STAGED)                       # no caller's word: the probe is not optional
    finally:
        os.close(cfd)
    ev = r.log.events
    assert ev.index("probe exchange ok") < ev.index("probe noreplace ok") < ev.index("seal-in done")
    assert os.listdir(r.cls) == []


def test_d2_the_outputs_are_closed_before_the_detach_that_precedes_the_commit(tmp_path):
    w, c = _work(tmp_path), _class_dir(tmp_path, older=False)
    outs = {(st.st_dev, st.st_ino) for st in (os.stat(w / "out" / n) for n in ("bundle", "report.json"))}
    held = []

    def before_commit():
        for n in os.listdir("/dev/fd"):
            try:
                st = os.fstat(int(n))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in outs:
                held.append(n)
    wd, cd = _dfd(w), _dfd(c)
    try:
        X.publish_if_success(X.classify(X.RunObservation(exit="exit", code=0, line=_line())), wd, cd, S,
                             object_format="sha1", owner_uid=os.getuid(), caps=CAPS, seams=X.PublishSeams(),
                             before_commit=before_commit)
    finally:
        os.close(wd)
        os.close(cd)
    assert held == [] and (c / S / "bundle").read_bytes() == _bundle()
