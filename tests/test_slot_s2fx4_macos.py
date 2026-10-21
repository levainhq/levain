"""Fix round 3, lane FX4: the three macOS real-host defects the s2-macos VM run reproduced (runs/s2-macos RESULT.md).

* F-M1 ``wall.drop_and_assert``: XNU clamps ``RLIMIT_NPROC`` to ``kern.maxprocperuid`` without an error (raw/M01d_M04:
  2000 asked, (1333, 1333) read back, every start refused). The target is min(NPROC, hard, maxprocperuid); a read
  failure refuses.
* F-M2 ``pf.load_steps``: the default optimizer folded 8 slots' rules into ``<__automatic…>`` tables (raw/M01c_pf_slots)
  and the start check refused all 8; the anchor is now checked and loaded with ``-o none`` (raw/W12: 8 READY).
* F-M3 ``proc.signal_as_uid``: the child's ``kill(-1, SIGSTOP)`` stops the child too (raw/F_M3_exp), so a plain
  ``waitpid(pid, 0)`` never returned (raw/F_M3_td1).
"""
from __future__ import annotations

import errno
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot import Refused, pf, proc as P, wall
from levain.slot.ids import SlotId
from tests.test_slot_s2d_wall import FakeOS

ROOT = str(Path(wall.__file__).resolve().parents[2])

UID, GID = 3987, 3987


# --- F-M1: RLIMIT_NPROC under kern.maxprocperuid --------------------------------------------------------------


class XnuOS(FakeOS):
    """FakeOS whose setrlimit behaves as XNU's does for RLIMIT_NPROC: both values silently clamped to
    ``kern.maxprocperuid`` (the VM: maxproc 2000 = the hard limit, maxprocperuid 1333)."""

    def __init__(self, *, hard=2000, per_uid=1333, **kw):
        super().__init__(**kw)
        self.nproc = (hard, hard)
        self.per_uid = per_uid
        self.calls.maxprocperuid = self._w("maxprocperuid", lambda: self.per_uid)

    def _setrlimit(self, which, lim):
        super()._setrlimit(which, lim)
        self.nproc = tuple(min(v, self.per_uid) for v in self.nproc)


def _drop(f, nproc=wall.NPROC):
    wall.drop_and_assert(UID, GID, (GID,), nproc=nproc, os_calls=f.calls)


def test_fm1_the_vm_case_starts_at_maxprocperuid():
    """raw/M01d_M04's host: hard 2000, maxprocperuid 1333. The set asks for 1333 and reads 1333 back."""
    f = XnuOS()
    _drop(f)
    assert f.nproc == (1333, 1333)
    assert ("setrlimit", resource.RLIMIT_NPROC, (1333, 1333)) in f.log
    # the ceiling is read after the drop is proven, before the set
    names = f.names()
    assert names.index("setegid") < names.index("maxprocperuid") < names.index("setrlimit")


@pytest.mark.parametrize("hard,per_uid,want", [
    (resource.RLIM_INFINITY, 5000, wall.NPROC),   # W-M1's host after sysctl -w: V3's 2048 stands
    (resource.RLIM_INFINITY, 1333, 1333),
    (1000, 1333, 1000),                           # the hard limit is still the lower bound
    (2000, 2000, 2000),
])
def test_fm1_target_is_the_min_of_all_three(hard, per_uid, want):
    f = XnuOS(hard=hard, per_uid=per_uid)
    _drop(f)
    assert f.nproc == (want, want)


def test_fm1_unreadable_maxprocperuid_refuses_before_the_set():
    f = XnuOS(faults={"maxprocperuid": lambda: (_ for _ in ()).throw(OSError(errno.ENOENT, "sysctlbyname"))})
    with pytest.raises(Refused) as e:
        _drop(f)
    assert e.value.name == "drop-assert" and "maxprocperuid" in e.value.detail
    assert "setrlimit" not in f.names()


@pytest.mark.parametrize("bad", [0, -1, None, True, "1333"])
def test_fm1_implausible_maxprocperuid_refuses(bad):
    f = XnuOS(faults={"maxprocperuid": lambda: bad})
    with pytest.raises(Refused) as e:
        _drop(f)
    assert e.value.name == "drop-assert"
    assert "setrlimit" not in f.names()


def test_fm1_no_ceiling_seam_is_linux_semantics():
    """Linux has no maxprocperuid: the seam is None there and the target is min(NPROC, hard) as before."""
    f = XnuOS(hard=resource.RLIM_INFINITY, per_uid=10)
    f.calls.maxprocperuid = None
    f._setrlimit = FakeOS._setrlimit.__get__(f)   # no clamp on Linux
    f.calls.setrlimit = f._w("setrlimit", f._setrlimit)
    _drop(f)
    assert f.nproc == (wall.NPROC, wall.NPROC)


def test_fm1_front_sets_no_nproc_and_reads_no_ceiling():
    f = XnuOS()
    _drop(f, nproc=None)
    assert "maxprocperuid" not in f.names() and "setrlimit" not in f.names()


def test_fm1_real_os_calls_wire_the_ceiling_per_host():
    c = wall.real_os_calls()
    if sys.platform == "darwin":
        assert c.maxprocperuid is wall._darwin_maxprocperuid
    else:
        assert c.maxprocperuid is None


@pytest.mark.skipif(sys.platform != "darwin", reason="sysctlbyname is the macOS read")
def test_fm1_the_ctypes_read_matches_sysctl():
    """The real read (unprivileged) against ``sysctl -n`` on this host."""
    r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.maxprocperuid"], capture_output=True, text=True, check=True)
    assert wall._darwin_maxprocperuid() == int(r.stdout.strip()) > 0


def test_fm1_stage_refuses_and_execs_nothing_on_an_unreadable_ceiling():
    spec = wall.Spec("vendor", UID, GID, (GID,), wall.NPROC, None, (), None, "/", ("/usr/bin/true",), False, ())
    f = XnuOS(spec_bytes=spec.to_json(),
              faults={"maxprocperuid": lambda: (_ for _ in ()).throw(OSError(errno.EPERM, "x"))})
    lines: list = []

    class Err:
        def write(self, s):
            lines.append(s)

        def flush(self):
            pass
    assert wall.stage_main(3, f.calls, err=Err()) == wall.STAGE_EXIT
    assert "execve" not in f.names()
    assert any("refused drop-assert" in s and "maxprocperuid" in s for s in lines)


# --- F-M2: the anchor loads with the optimizer off ------------------------------------------------------------

EGRESS = [SlotId("egress", i) for i in range(4, 12)]   # the VM's 8 slots (e4..e11)
VERIFY = [SlotId("none", i) for i in (2, 3)]


def _ids():
    ids, uid = {}, 3987
    for s in EGRESS + VERIFY:
        for name in s.accounts:
            ids[name] = uid
            uid -= 1
    return ids


def test_fm2_check_and_load_both_run_with_the_optimizer_off():
    steps = pf.load_steps(EGRESS + VERIFY, _ids())
    write = next(s for s in steps if s.write)
    assert write.validate == (pf.PFCTL, "-n", "-o", "none", "-a", pf.ANCHOR, "-f")
    loads = [s.argv for s in steps if s.argv[:1] == (pf.PFCTL,)]
    assert loads == [(pf.PFCTL, "-o", "none", "-a", pf.ANCHOR, "-f", str(pf.ANCHOR_FILE))]


def _lo0():
    return ("lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n"
            + "".join(f"\tinet 127.0.1.{s.index + 1} netmask 0xffffffff\n" for s in EGRESS))


def test_fm2_eight_slots_unfolded_are_all_ready():
    """What ``pfctl -sr`` prints for an ``-o none`` load: every rule as written; raw/W12's 8 READY."""
    ids = _ids()
    live = "\n".join(line for r in pf.anchor_rules(EGRESS + VERIFY, ids) for line in wall.pf_printed(r))
    for s in EGRESS:
        wall.check_egress_ready(s, ids, pf_text=live, ifconfig_text=_lo0())


def test_fm2_a_folded_anchor_is_still_refused():
    """raw/M01c_pf_slots: the default optimizer replaced the per-alias lo0 blocks with two tables. The check
    that refuses a folded anchor stays (it is what made F-M2 visible instead of a silent mismatch)."""
    ids = _ids()
    aliases = {f"127.0.1.{s.index + 1}" for s in EGRESS}
    printed = [line for r in pf.anchor_rules(EGRESS + VERIFY, ids) for line in wall.pf_printed(r)]
    folded = [line for line in printed
              if not (line.startswith("block drop out quick on lo0 inet proto") and line.split()[-1] in aliases)]
    assert len(folded) < len(printed)
    folded += ["block drop out quick on lo0 inet proto tcp from any to <__automatic_760f9cec_0>",
               "block drop out quick on lo0 inet proto udp from any to <__automatic_760f9cec_1>"]
    for s in EGRESS:
        with pytest.raises(Refused) as e:
            wall.check_egress_ready(s, ids, pf_text="\n".join(folded), ifconfig_text=_lo0())
        assert e.value.name == "pf-anchor-not-loaded" and "optimizer" in e.value.detail


# --- F-M3: the signalling child stopped by its own kill(-1) ---------------------------------------------------


def _stopped(sig):
    return (sig << 8) | 0x7F


class Reaper:
    def __init__(self, first, after=signal.SIGKILL):
        self.first, self.after = first, after
        self.waits: list = []
        self.kills: list = []

    def waitpid(self, pid, flags):
        self.waits.append((pid, flags))
        return pid, (self.first if len(self.waits) == 1 else self.after)

    def kill(self, pid, sig):
        self.kills.append((pid, sig))


def _call(sig, r):
    return P.signal_as_uid(UID, sig, fork=lambda: 4321, waitpid=r.waitpid, kill=r.kill,
                           child=lambda u, s: pytest.fail("the parent never runs the child"), exit=os._exit)


def test_fm3_child_stopped_by_its_own_sigstop_counts_as_sent_and_is_killed():
    """raw/F_M3_exp: the child reads ``T`` after ``kill(-1, SIGSTOP)``. Reap with WUNTRACED, then SIGKILL + reap."""
    r = Reaper(_stopped(signal.SIGSTOP))
    assert _call(signal.SIGSTOP, r) is True
    assert r.waits == [(4321, os.WUNTRACED), (4321, 0)]
    assert r.kills == [(4321, signal.SIGKILL)]


@pytest.mark.parametrize("sig", [signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU])
def test_fm3_every_stop_class_signal_is_handled_the_same(sig):
    r = Reaper(_stopped(sig))
    assert _call(sig, r) is True
    assert r.kills == [(4321, signal.SIGKILL)] and len(r.waits) == 2


def test_fm3_stopped_by_a_different_signal_is_not_sent_but_still_reaped():
    r = Reaper(_stopped(signal.SIGTSTP))
    assert _call(signal.SIGSTOP, r) is False
    assert r.kills == [(4321, signal.SIGKILL)] and r.waits[-1] == (4321, 0)


def test_fm3_a_stop_while_sending_a_non_stop_signal_is_not_sent():
    r = Reaper(_stopped(signal.SIGSTOP))
    assert _call(signal.SIGCONT, r) is False
    assert r.kills == [(4321, signal.SIGKILL)]


def test_fm3_child_already_gone_when_killed_is_still_reaped():
    r = Reaper(_stopped(signal.SIGSTOP))

    def gone(pid, sig):
        r.kills.append((pid, sig))
        raise ProcessLookupError(errno.ESRCH, "kill")
    r.kill = gone
    assert _call(signal.SIGSTOP, r) is True
    assert r.waits[-1] == (4321, 0)


@pytest.mark.parametrize("status,sent", [(0, True), (70 << 8, False), (signal.SIGKILL, False)])
def test_fm3_exits_reap_once_and_kill_nothing(status, sent):
    """The SIGKILL / SIGCONT / clean-exit paths: one reap, no kill, the result as before."""
    r = Reaper(status)
    assert _call(signal.SIGKILL, r) is sent
    assert r.waits == [(4321, os.WUNTRACED)] and r.kills == []


REAL_FORK = """
import os, signal, sys
sys.path.insert(0, {root!r})
from levain.slot import proc as P
stop_with = getattr(signal, sys.argv[2])
# the child stops itself, as kill(-1) from the slot uid does on macOS (no drop: this is the reap, unprivileged).
# SIGSTOP as the stopper: the kernel may discard SIGTSTP in an orphaned process group, so it cannot be relied on.
print(P.signal_as_uid(4000, getattr(signal, sys.argv[1]), child=lambda uid, sig: os.kill(os.getpid(), stop_with)))
"""


@pytest.mark.parametrize("sig,stop_with,want", [("SIGSTOP", "SIGSTOP", "True"), ("SIGTSTP", "SIGSTOP", "False")])
def test_fm3_real_fork_never_hangs(tmp_path, sig, stop_with, want):
    """A real fork whose child stops itself: the old ``waitpid(pid, 0)`` blocks here forever (the timeout is the
    hang's detector)."""
    script = tmp_path / "fork.py"
    script.write_text(REAL_FORK.format(root=ROOT))
    p = subprocess.Popen([sys.executable, "-I", str(script), sig, stop_with], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)   # the parent AND its stopped child, never left behind
        p.communicate()
        pytest.fail("signal_as_uid blocked on a child its own signal stopped")
    assert p.returncode == 0, err
    assert out.strip() == want
