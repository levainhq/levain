"""S1 session 2, fix round 2, lane K2a: the launch layer (head rulings on S2_L3_r1 g2 codex 3/4/7/8, complement
1/2/3/4/6/7/8, g1 glm on ``spawn_target``).

* EXEC MARKER: only a POSITIVE ``+`` from the last hop before the vendor means the exec happened (Linux: the stage;
  macOS: the trampoline under the applied profile); EOF, ``!``, ``+`` then ``!``, or no byte in the bound = the start
  failed, never ACTIVE. ``spawn_target``'s child that fails before its dup2 writes ``!`` on the ORIGINAL fd.
* KILL DOMAIN: a vendor that exec'd but is not recorded ACTIVE dies with its whole domain (Linux: the session
  cgroup; macOS: the slot uid), never only the launcher pid; Linux bwrap runs ``--die-with-parent``.
* FORWARDER: a readiness byte after bind + listen, waited for (bounded) before the vendor exec; the unix socket is
  closed when its connect fails; an accept error backs off and the loop goes on.
* NNP: the Linux stage sets and proves ``no_new_privs`` after the drop.
* CLONE RETRY: a failed clone leaves ``<S>/repo`` emptied; ``already-started`` keys off what was recorded.
* DISCOVERY: §5.5's HOME names also under ``<S>/xdg/config`` (and ``xdg/data`` is listed).

The real-process tests run the stage, the trampoline and ``spawn_target``'s child as THIS user: no profile, no
sandbox, no namespace, no privileged call (the stage's three id-setting calls are no-ops, as in s2d_stage_exec).
"""
from __future__ import annotations

import errno
import os
import select
import signal
import socket
import subprocess
import sys
import stat as stat_mod
import threading
import types
from pathlib import Path, PurePosixPath

import pytest

from levain.contracts import canon
from levain.slot import Refused, forwarder, wall
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot.ids import RANGES, SlotId
from levain.slot.root_tree import VENDOR_PREFIX
from tests.execve_guard import refuse_real_execve
from tests.test_slot_s2d_forwarder import sockdir  # noqa: F401,F811 - the short /tmp socket dir fixture
from tests.test_slot_s2f2_runtime_helper import OP_UID
from tests.test_slot_s2f3_start import (DARWIN_IDS, E0, S0, Execd, StageFake, _rendered, _S, _world)
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

ROOT = str(Path(wall.__file__).resolve().parents[2])
SESSION = "s-00000000000000aa"


@pytest.fixture
def env(tmp_path):
    return _world(tmp_path)


def scripted(w, la, *, vendor=b"+", forwarder_byte=b"+", hold=()):
    """Replace the fake spawn's marker bytes: ``vendor`` / ``forwarder_byte`` are what that target's pipe gets (None:
    nothing, then the close = EOF); a target in ``hold`` keeps a dup of its pipe open (no EOF: the bound decides).
    Returns the held fds for the test to close."""
    la.mode, la.forwarder_ready = "script", False
    real, held = la.spawn, []

    def spawn(argv, spec_json, cgroup, exec_w, detach):
        pid = real(argv, spec_json, cgroup, exec_w, detach)
        target = canon.parse(spec_json)["target"]
        if exec_w is not None:
            data = {"vendor": vendor, "forwarder": forwarder_byte}.get(target)
            if data:
                os.write(exec_w, data)
            if target in hold:
                held.append(os.dup(exec_w))
        return pid
    w.seams.spawn = spawn
    return held


def _no_active(w, s):
    return not any(op == "CAS" and f.get("to") == R.ACTIVE for op, f in w.calls)


# --- EXEC MARKER: the helper's read --------------------------------------------------------------------------------

@pytest.mark.parametrize("data,close,want", [
    (b"+", True, "exec"),            # + then EOF: the last hop's close-on-exec copy closed at the execve
    (b"+", False, "timeout"),        # + then nothing in the bound: never an exec (S2_L3_r2 g2-N2)
    (b"+!", True, "failed"),         # the vendor's execve failed after the marker
    (b"!", True, "failed"),
    (b"", True, "failed"),           # EOF with no byte: every holder exited, no marker
    (b"", False, "timeout"),
])
def test_await_exec_reads_the_positive_marker(data, close, want):
    r, w = os.pipe()
    try:
        if data:
            os.write(w, data)
        if close:
            os.close(w)
            w = -1
        assert SS.await_exec(r, bound_s=0.05) == want
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)


def test_eof_without_marker_is_a_failed_start(env):
    """codex 3 / comp 1: EOF used to mean "exec happened"; a hop that died before the vendor (a profile sandbox-exec
    rejected, a failing nsenter / bwrap) closed the pipe with no byte and the session went ACTIVE."""
    w, la, _ = env
    s = _rendered(w)
    scripted(w, la, vendor=None)
    la.codes["vendor"] = 65
    assert w.main(["start", s])[1]["refused"] == "exec-failed"
    assert w.state(s).state == R.RENDERED and _no_active(w, s)


def test_marker_then_refusal_is_a_failed_start(env):
    w, la, _ = env
    s = _rendered(w)
    scripted(w, la, vendor=b"+!")
    la.codes["vendor"] = wall.STAGE_EXIT
    assert w.main(["start", s])[1]["refused"] == "exec-failed"
    assert w.state(s).state == R.RENDERED and _no_active(w, s)


def test_no_marker_in_the_bound_kills_the_domain(env, monkeypatch):
    w, la, table = env
    s = _rendered(w)
    held = scripted(w, la, vendor=None, hold=("vendor",))
    monkeypatch.setattr(SS, "EXEC_MARKER_S", 0.05)
    try:
        assert w.main(["start", s])[1]["refused"] == "exec-failed"
    finally:
        for fd in held:
            os.close(fd)
    (v,) = la.of("vendor")
    assert v["pid"] in la.killed and table.of(S0) == []        # the cgroup killed: forwarder and launcher gone
    assert w.state(s).state == R.RENDERED and _no_active(w, s)


# --- EXEC MARKER: who writes it (the stage on Linux, the trampoline on macOS) -----------------------------------------

def _vendor_spec(host):
    gid = min(RANGES[host].shared)
    return wall.launch_spec(host, "vendor", slot=E0, session=SESSION, ids={"_levain_s0": 3999 if host == "darwin"
                                                                               else S0, "_levain_gw0": 3998},
                            shared={"levain-slots": gid}, argv=(f"{VENDOR_PREFIX[host]}/bin/claude", "--x"),
                            extra_env={"ANTHROPIC_API_KEY": "tok"}, exec_fd=wall.EXEC_FD)


@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    yield from refuse_real_execve(monkeypatch, wall.os, route=True)


def _env_bytes(sp):
    return b"HOME=" + sp.cwd.rsplit("/", 1)[0].encode() + b"\n"


def test_linux_stage_writes_the_marker_last_before_execve(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    sp = _vendor_spec("linux")
    fake = StageFake(sp, fds={0, 1, 2, 3, 4, 9}, files={sp.env_file: _env_bytes(sp)})
    with pytest.raises(Execd):
        wall.stage_main(3, fake.calls)
    at = fake.at_exec
    assert at["written"] == [(wall.EXEC_FD, b"+")]               # written before the execve, and only it
    assert at["fds"] == {0, 1, 2, 4} and at["inheritable"] == {0, 1, 2}   # close-on-exec: the vendor never holds it
    assert at["path"] == sp.argv[0]


def test_linux_execve_failing_after_the_marker_writes_the_refusal(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    sp = _vendor_spec("linux")
    fake = StageFake(sp, fds={0, 1, 2, 3, 4}, files={sp.env_file: _env_bytes(sp)})

    def enoexec(path, argv, env):
        raise OSError(errno.ENOEXEC, "exec format error")
    fake.execve = enoexec
    assert wall.stage_main(3, fake.calls) == wall.STAGE_EXIT
    assert fake.written == [(wall.EXEC_FD, b"+"), (wall.EXEC_FD, b"!")]


def test_macos_stage_hands_the_marker_to_the_trampoline_under_the_profile(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "darwin")
    sp = _vendor_spec("darwin")
    fake = StageFake(sp, fds={0, 1, 2, 3, 4}, files={sp.env_file: _env_bytes(sp), sp.profile: b"(version 1)\n"})
    with pytest.raises(Execd):
        wall.stage_main(3, fake.calls)
    at = fake.at_exec
    assert at["path"] == wall.SANDBOX_EXEC and at["written"] == []
    assert at["argv"][:3] == [wall.SANDBOX_EXEC, "-f", sp.profile]
    tramp = at["argv"][3:]
    assert tramp[0] == "/usr/bin/python3" and tramp[1] == "-IS"
    assert tramp[4:8] == ["levain.slot.wall", "--exec-fd", "4", "--"] and tramp[8:] == list(sp.argv)
    assert 4 in at["inheritable"]                                 # rides through sandbox-exec to the trampoline
    # sandbox-exec itself failing to start: the stage's refusal byte, no marker
    bad = StageFake(sp, fds={0, 1, 2, 3, 4}, files={sp.env_file: _env_bytes(sp), sp.profile: b"(version 1)\n"})

    def enoent(path, argv, env):
        raise FileNotFoundError(errno.ENOENT, path)
    bad.execve = enoent
    assert wall.stage_main(3, bad.calls) == wall.STAGE_EXIT and bad.written == [(wall.EXEC_FD, b"!")]


def test_trampoline_marker_order_and_refusals(monkeypatch):
    log = []
    tw = lambda fd, b: log.append(("write", fd, b))
    ti = lambda fd, v: log.append(("cloexec", fd, v))

    def ex(path, argv, env):
        log.append(("execve", path, argv, dict(env)))
        raise OSError(errno.ENOENT, "gone")
    monkeypatch.setattr(wall.os, "execve", ex)
    argv = ["--exec-fd", "4", "--", "/v/claude", "--x"]
    assert wall.trampoline_main(argv, write=tw, set_inheritable=ti, environ={b"K": b"v"}) == wall.STAGE_EXIT
    assert log[:2] == [("cloexec", 4, False), ("write", 4, b"+")]
    assert log[2] == ("execve", b"/v/claude", [b"/v/claude", b"--x"], {b"K": b"v"}) and log[3] == ("write", 4, b"!")
    for bad in (["--exec-fd", "5", "--", "/v"], ["--exec-fd", "4", "/v"], ["--exec-fd", "4", "--", "rel"]):
        log.clear()
        assert wall.trampoline_main(bad, write=tw, set_inheritable=ti) == wall.STAGE_EXIT
        assert ("execve" not in [x[0] for x in log]) and all(x[2] != b"+" for x in log if x[0] == "write")
        assert [x for x in log if x[0] == "write"] == ([] if bad[1] != "4" else [("write", 4, b"!")])


RUNNER = """
import os, sys
sys.path.insert(0, {root!r})
from levain.slot import wall
w = int(sys.argv[1])
os.dup2(w, 4)
os.close(w)
mode = sys.argv[2]
if mode == "trampoline":
    sys.exit(wall.main(sys.argv[3:]))
data = open(sys.argv[3], "rb").read()
fd = os.open("/dev/null", os.O_RDWR)
os.dup2(fd, 7)            # the pty stand-in
os.close(fd)
r, pw = os.pipe()
os.write(pw, data)
os.close(pw)
if r != 3:
    os.dup2(r, 3)
    os.close(r)

real_execve = os.execve


def execve(path, argv, env):
    if "sandbox-exec" in path or any("sandbox-exec" in a for a in argv):
        raise SystemExit("refusing to run a profile in a unit test")
    real_execve(path, argv, env)


calls = wall.real_os_calls()
calls.setgroups = lambda gs: None
calls.setgid = lambda g: None
calls.setuid = lambda u: None
calls.getgroups = lambda: [os.getgid()]
calls.set_nnp = None      # a test user's stage: the host's prctl is not this test's subject
wall.os.execve = execve   # wall calls os.execve itself
sys.exit(wall.stage_main(3, calls))
"""

FD_LISTER = ("import os, sys\n"
             "d = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'\n"
             "live = []\n"
             "for n in os.listdir(d):\n"
             "    try:\n"
             "        os.fstat(int(n)); live.append(int(n))\n"
             "    except OSError:\n"
             "        pass\n"
             "print(sorted(live))\n")


def _run_real(tmp_path, mode, args):
    (tmp_path / "runner.py").write_text(RUNNER.format(root=ROOT))
    r, w = os.pipe()
    try:
        p = subprocess.Popen([sys.executable, "-I", str(tmp_path / "runner.py"), str(w), mode, *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(w,))
    finally:
        os.close(w)
    try:
        out, err = p.communicate(timeout=60)
        got = b""
        while True:
            b = os.read(r, 16)
            if not b:
                break
            got += b
    finally:
        os.close(r)
    return p.returncode, out, err, got


def _real_spec(tmp_path, argv):
    S = tmp_path / "S"
    (S / "root").mkdir(parents=True)
    (S / "repo").mkdir()
    (S / "root" / "env").write_text("HOME=/h\nPATH=/usr/bin:/bin\n")
    gid = os.getgid()
    sp = wall.Spec("vendor", os.getuid(), gid, (gid,), wall.NPROC, str(S / "root" / "env"), (("TERM", "xterm"),),
                   None, str((S / "repo").resolve()), tuple(argv), False, (7,), wall.EXEC_FD)
    (tmp_path / "spec").write_bytes(sp.to_json())
    return str(tmp_path / "spec")


@pytest.mark.skipif(os.getuid() == 0, reason="the stage refuses uid 0 by design")
def test_real_stage_marker_then_vendor_without_the_pipe(tmp_path):
    """The Linux protocol in a real process (no profile: the stage is the last hop): ``+`` arrives, the vendor runs
    holding 0, 1, 2 and its pty only (the pipe was close-on-exec), and the pipe's EOF follows."""
    code, out, err, got = _run_real(tmp_path, "stage", [_real_spec(tmp_path, [sys.executable, "-I", "-c", FD_LISTER])])
    assert code == 0, err
    assert got == b"+" and out.strip() == "[0, 1, 2, 7]"


@pytest.mark.skipif(os.getuid() == 0, reason="the stage refuses uid 0 by design")
def test_real_stage_execve_failure_after_the_marker(tmp_path):
    code, out, err, got = _run_real(tmp_path, "stage", [_real_spec(tmp_path, ["/nonexistent/levain-vendor"])])
    assert code == wall.STAGE_EXIT and got == b"+!"


def test_real_trampoline(tmp_path):
    """The macOS last hop in a real process, run directly (never under sandbox-exec here)."""
    code, out, err, got = _run_real(tmp_path, "trampoline",
                                    ["--exec-fd", "4", "--", sys.executable, "-I", "-c", FD_LISTER])
    assert code == 0, err
    assert got == b"+" and out.strip() == "[0, 1, 2]"
    code, out, err, got = _run_real(tmp_path, "trampoline", ["--exec-fd", "4", "--", "/nonexistent/levain-vendor"])
    assert code == wall.STAGE_EXIT and got == b"+!"


SPAWN_RUNNER = """
import fcntl, os, sys, types
sys.path.insert(0, {root!r})
fd = os.open("/dev/null", os.O_RDWR)
os.dup2(fd, 4)            # fd 4 before the fork: a stray byte written there lands nowhere
os.close(fd)
from levain.slot import runtime_helper as H
r, w = os.pipe()
if sys.argv[1] == "fail-before-dup2":
    real = fcntl.fcntl
    def fake(f, cmd, arg=0):
        if f == w:
            raise OSError(24, "EMFILE")
        return real(f, cmd, arg)
    H.fcntl = types.SimpleNamespace(F_DUPFD=fcntl.F_DUPFD, fcntl=fake)
    argv = ["/bin/sh", "-c", "exit 0"]
else:
    argv = ["/bin/sh", "-c", "cat <&3 >/dev/null; printf + >&4"]
pid = H.spawn_target(argv, b"{{}}", None, w, False)
os.close(w)
got = b""
while True:
    b = os.read(r, 16)
    if not b:
        break
    got += b
os.waitpid(pid, 0)
sys.stdout.write(repr(got))
"""


@pytest.mark.parametrize("mode,want", [("fail-before-dup2", "b'!'"), ("ok", "b'+'")])
def test_spawn_target_child_marker_fd(tmp_path, mode, want):
    """glm (g1): a child failing BEFORE its dup2 onto EXEC_FD writes ``!`` on the original pipe fd, not on fd 4;
    and on the success path the pipe IS fd 4 in the exec'd target. A real fork as this user, no root act."""
    (tmp_path / "spawn.py").write_text(SPAWN_RUNNER.format(root=ROOT))
    r = subprocess.run([sys.executable, "-I", str(tmp_path / "spawn.py"), mode], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == want


# --- KILL DOMAIN ----------------------------------------------------------------------------------------------------

def _lease_dies_at_active(w, also=None):
    def eof(step):
        if step == "active":
            if also:
                also()
            w.control.lease_eof(w.conn.lease)
            w.control.join_eof(10)
    w.on_step = eof


def test_active_refused_kills_the_session_cgroup_linux(env):
    """codex 4 / comp 4: the launcher pid is bwrap's monitor on Linux; the vendor and whatever it forked are other
    processes in the cgroup. All of them die, then the launcher is reaped."""
    w, la, table = env
    s = _rendered(w)
    _lease_dies_at_active(w, also=lambda: table.add(77777, S0, cg=True))     # the vendor's own child
    assert w.main(["start", s])[1]["refused"] in ("lease-dead", "stale-generation", "from-mismatch")
    (v,) = la.of("vendor")
    assert la.killed == [v["pid"]]                                           # reaped
    assert table.of(S0) == []                                                # the forwarder, the child, the vendor


def test_active_refused_kills_the_slot_uid_macos(tmp_path):
    w, la, table = _world(tmp_path, host="darwin")
    w.seams.account_uid = lambda name: DARWIN_IDS[name] if name in DARWIN_IDS else 3799
    w.seams.group_gid = lambda g: {"levain-gwfronts": 3789, "levain-slots": 3788}[g]
    s = _rendered(w)
    _lease_dies_at_active(w, also=lambda: table.add(77777, 3999))
    assert w.main(["start", s])[1]["refused"] in ("lease-dead", "stale-generation", "from-mismatch")
    assert table.of(3999) == [] and (77777, signal.SIGKILL) in table.signals


def test_bwrap_dies_with_its_parent():
    sp = wall.launch_spec("linux", "vendor", slot=E0, session=SESSION, ids={"_levain_s0": S0},
                          shared={"levain-slots": min(RANGES["linux"].shared)},
                          argv=(f"{VENDOR_PREFIX['linux']}/bin/claude",))
    la = wall.linux_launch_argv(sp, slot=E0, session=SESSION, kind="interactive", netns_names=lambda: {"levain-e0"},
                                host_links={"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None})
    i = la.argv.index("/usr/libexec/levain/bwrap")
    assert la.argv[i + 1] == "--die-with-parent"


# --- FORWARDER ------------------------------------------------------------------------------------------------------

def test_forwarder_not_ready_refused_before_the_vendor(env):
    w, la, table = env
    s = _rendered(w)
    scripted(w, la, forwarder_byte=None)                       # it died before listening: EOF, no byte
    assert w.main(["start", s])[1]["refused"] == "forwarder-not-ready"
    (f,) = la.of("forwarder")
    assert la.of("vendor") == [] and f["pid"] in la.killed and table.of(S0) == []
    assert f["exec_w"] is not None and f["spec"]["keep_fds"] == [wall.READY_FD]
    assert w.steps[-2:] == ["clone", "forwarder"]


def test_forwarder_silent_past_the_bound_refused(env, monkeypatch):
    w, la, _ = env
    s = _rendered(w)
    held = scripted(w, la, forwarder_byte=None, hold=("forwarder",))
    monkeypatch.setattr(SS, "FORWARDER_READY_S", 0.05)
    try:
        assert w.main(["start", s])[1]["refused"] == "forwarder-not-ready"
    finally:
        for fd in held:
            os.close(fd)
    assert la.of("vendor") == []


def test_forwarder_ready_byte_only_once_listening(sockdir):
    assert forwarder.READY == wall.EXEC_OK                       # the forwarder does not import the wall
    r, wfd = os.pipe()
    seen = []
    signal_ready = forwarder._signal_ready(wfd)

    def ready(port):
        c = socket.create_connection(("127.0.0.1", port), timeout=5)   # it listens before the byte
        c.close()
        seen.append(port)
        signal_ready(port)
    stop = threading.Event()
    t = threading.Thread(target=forwarder.serve, args=(os.path.join(sockdir, "gw.sock"),),
                         kwargs={"port": 0, "ready": ready, "stop": stop}, daemon=True)
    t.start()
    try:
        assert select.select([r], [], [], 5)[0] and os.read(r, 1) == b"+"
        assert os.read(r, 1) == b"" and seen                      # the fd is closed after the byte
    finally:
        stop.set()
        t.join(5)
        os.close(r)


def test_forwarder_main_bind_failure_is_clean_and_writes_nothing(capsys):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    r, wfd = os.pipe()
    try:
        assert forwarder.main(["--port", str(port), "--unix", "/nonexistent/gw.sock", "--ready-fd", str(wfd)]) == 1
        assert not select.select([r], [], [], 0)[0]               # no ready byte
        assert "refused listen-failed" in capsys.readouterr().err
    finally:
        taken.close()
        os.close(r)
        os.close(wfd)


@pytest.mark.parametrize("argv", [["--port", "1", "--unix", "/a"], ["--port", "1", "--unix", "/a", "--ready-fd", "x"],
                                  ["--port", "1", "--unix", "/a", "--ready-fd", "2"],
                                  ["--port", "1", "--unix", "/a", "--fd", "4"]])
def test_forwarder_main_needs_its_ready_fd(argv):
    assert forwarder.main(argv) == 2


def test_relay_closes_the_unix_socket_when_connect_fails(monkeypatch, tmp_path):
    closed = []
    real = socket.socket

    class Spy(real):
        def close(self):
            if self.family == socket.AF_UNIX:
                closed.append(self)
            return super().close()
    a, b = socket.socketpair()                                       # the "TCP" side, made before the spy
    monkeypatch.setattr(forwarder.socket, "socket", Spy)
    try:
        slots = threading.BoundedSemaphore(1)
        slots.acquire()                                              # serve's acquire before each relay
        forwarder._relay(a, str(tmp_path / "absent.sock"), slots)
        assert len(closed) == 1
    finally:
        b.close()


def test_accept_error_backs_off_and_serves_on(monkeypatch, sockdir):
    path = os.path.join(sockdir, "gw.sock")
    front = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    front.bind(path)
    front.listen(4)
    real = socket.socket
    fails = []

    class Flaky(real):
        def accept(self):
            if not fails:
                fails.append(1)
                raise OSError(errno.EMFILE, "too many open files")
            return super().accept()
    monkeypatch.setattr(forwarder.socket, "socket", Flaky)
    monkeypatch.setattr(forwarder, "ACCEPT_BACKOFF_S", 0.01)
    ports, stop = [], threading.Event()
    t = threading.Thread(target=forwarder.serve, args=(path,), kwargs={"port": 0, "ready": ports.append, "stop": stop},
                         daemon=True)
    t.start()
    try:
        for _ in range(100):
            if fails and ports:
                break
            threading.Event().wait(0.02)
        c = real(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(("127.0.0.1", ports[0]))
        front.settimeout(5)
        up, _ = front.accept()                                       # relayed after the failed accept
        c.sendall(b"hi")
        assert up.recv(2) == b"hi"
        up.close()
        c.close()
        assert fails and t.is_alive()
    finally:
        stop.set()
        t.join(5)
        front.close()


# --- NNP ------------------------------------------------------------------------------------------------------------

def _nnp_fake(monkeypatch, *, value=1, fail=False):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    sp = _vendor_spec("linux")
    fake = StageFake(sp, fds={0, 1, 2, 3, 4}, files={sp.env_file: _env_bytes(sp)})
    order = []
    real_setuid = fake.calls.setuid
    fake.calls.setuid = lambda u: (order.append("setuid"), real_setuid(u))
    state = {"nnp": 0}

    def set_nnp():
        order.append("set_nnp")
        if fail:
            raise OSError(errno.EINVAL, "prctl")
        state["nnp"] = value
    fake.calls.set_nnp, fake.calls.get_nnp = set_nnp, lambda: state["nnp"]
    return fake, order


def test_linux_stage_sets_and_proves_no_new_privs(monkeypatch):
    fake, order = _nnp_fake(monkeypatch)
    with pytest.raises(Execd):
        wall.stage_main(3, fake.calls)
    assert order == ["setuid", "set_nnp"]                          # after the drop, before the exec


@pytest.mark.parametrize("kw,name", [(dict(value=0), "drop-assert"), (dict(fail=True), "drop-failed")])
def test_linux_stage_without_no_new_privs_execs_nothing(monkeypatch, capsys, kw, name):
    fake, _ = _nnp_fake(monkeypatch, **kw)
    assert wall.stage_main(3, fake.calls) == wall.STAGE_EXIT and fake.at_exec is None
    assert f"refused {name}" in capsys.readouterr().err and fake.written == [(wall.EXEC_FD, b"!")]


def test_real_os_calls_carry_nnp_on_linux_only(monkeypatch):
    monkeypatch.setattr(wall.sys, "platform", "linux")
    for name in ("getresuid", "getresgid"):                          # absent on a macOS test host
        monkeypatch.setattr(wall.os, name, getattr(os, name, lambda: (1, 1, 1)), raising=False)
    c = wall.real_os_calls()
    assert c.set_nnp is wall._linux_set_nnp and c.get_nnp is wall._linux_get_nnp
    monkeypatch.setattr(wall.sys, "platform", "darwin")
    assert wall.real_os_calls().set_nnp is None


# --- CLONE RETRY ----------------------------------------------------------------------------------------------------

def _repo_chowns(w, s):
    return [(u, g) for r, u, g in w.chowns if r == f"live/{s}/repo"]


def test_failed_clone_leaves_repo_empty_and_start_can_retry(env):
    """comp 6: a failed clone used to leave its partial dir, and every retry then hit ``already-started``."""
    w, la, _ = env
    s = _rendered(w)
    real = w.seams.spawn

    def partial_clone(argv, spec_json, cgroup, exec_w, detach):
        if canon.parse(spec_json)["target"] == "clone":
            (_S(w, s) / "repo" / ".git").mkdir()
            (_S(w, s) / "repo" / ".git" / "HEAD").write_text("partial\n")
        return real(argv, spec_json, cgroup, exec_w, detach)
    w.seams.spawn = partial_clone
    la.codes["clone"] = 128
    assert w.main(["start", s])[1]["refused"] == "clone-failed"
    repo = _S(w, s) / "repo"
    assert os.listdir(repo) == [] and w.mode(f"live/{s}/repo") == 0o700
    assert _repo_chowns(w, s)[-1] == (S0, S0)
    la.codes["clone"] = 0
    assert w.main(["start", s])[1]["ok"]
    assert len(la.of("clone")) == 2 and w.state(s).state == R.ACTIVE


def test_leftover_repo_without_a_clone_record_is_emptied_never_followed(env, tmp_path):
    w, la, _ = env
    s = _rendered(w)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the session's\n")
    repo = _S(w, s) / "repo"
    repo.mkdir()
    (repo / "half").write_text("x")
    os.symlink(outside, repo / "link")
    assert w.main(["start", s])[1]["ok"]
    assert (outside / "keep.txt").read_text() == "not the session's\n"
    assert not (repo / "half").exists() and not (repo / "link").exists()


def test_a_recorded_clone_is_already_started(env):
    """The clone marker is root's record that the repo was cloned (a vendor may have worked in it): a second start
    after a later failure never clones again, whatever the record state says."""
    w, la, _ = env
    s = _rendered(w)
    scripted(w, la, vendor=b"!")
    la.codes["vendor"] = wall.STAGE_EXIT
    assert w.main(["start", s])[1]["refused"] == "exec-failed"
    assert w.state(s).state == R.RENDERED
    assert w.main(["start", s])[1]["refused"] == "already-started"
    assert len(la.of("clone")) == 1


# --- DISCOVERY ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["git", "anthropic"])
def test_planted_xdg_config_name_refused(env, name):
    """comp 7: the env sends git's and Claude's config lookups to ``<S>/xdg/config``; a name planted there is
    refused like its ``.config`` twin."""
    w, la, _ = env
    s = _rendered(w)
    (_S(w, s) / "xdg" / "config" / name).mkdir()
    assert w.main(["start", s])[1]["refused"] == f"planted-discovery:home-xdg-config-{name}"
    assert la.spawns == []


def test_xdg_dirs_listed_under_home(env):
    w, _, _ = env
    s = _rendered(w)
    (_S(w, s) / "xdg" / "config" / "other").mkdir()
    (_S(w, s) / "xdg" / "data" / "claude").mkdir()
    ctx = H.Ctx(w.seams, H.Store(w.seams), OP_UID, "operator", OP_UID, {})
    try:
        home = SS.discovery_listings(ctx, s)["home"]
    finally:
        ctx.store.close()
    assert "xdg/config/other" in home and "xdg/data/claude" in home


# --- F3 (VM run s2-linux): make_cgroup makes the missing ancestors, trusts only root's ------------------------------

class CgFS:
    """A fake cgroupfs below ``/sys/fs/cgroup``: path -> (uid, mode); a mkdir is root's, 0755."""

    def __init__(self, **extra):
        self.nodes = {"/sys/fs/cgroup": (0, stat_mod.S_IFDIR | 0o555)}
        self.nodes.update(extra)
        self.made: list = []

    def lstat(self, p):
        if p not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, p)
        uid, mode = self.nodes[p]
        return types.SimpleNamespace(st_uid=uid, st_mode=mode)

    def mkdir(self, p, mode):
        if p in self.nodes:
            raise FileExistsError(errno.EEXIST, p)
        if p.rsplit("/", 1)[0] not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, p)       # what the bare os.mkdir hit in the VM
        self.nodes[p] = (0, stat_mod.S_IFDIR | mode)
        self.made.append((p, mode))


CG = "/sys/fs/cgroup/levain.slice/e0.scope/" + SESSION


def test_make_cgroup_makes_every_missing_ancestor_root_0755():
    fs = CgFS()
    H.make_cgroup(CG, mkdir=fs.mkdir, lstat=fs.lstat)
    assert fs.made == [("/sys/fs/cgroup/levain.slice", 0o755), ("/sys/fs/cgroup/levain.slice/e0.scope", 0o755),
                       (CG, 0o755)]
    H.make_cgroup(CG, mkdir=fs.mkdir, lstat=fs.lstat)               # a second start's: used as it is
    assert len(fs.made) == 3


@pytest.mark.parametrize("node", [
    ("/sys/fs/cgroup/levain.slice", (1000, stat_mod.S_IFDIR | 0o755)),            # not root's
    ("/sys/fs/cgroup/levain.slice", (0, stat_mod.S_IFDIR | 0o775)),               # group-writable
    ("/sys/fs/cgroup/levain.slice/e0.scope", (0, stat_mod.S_IFDIR | 0o757)),      # other-writable
    ("/sys/fs/cgroup/levain.slice/e0.scope", (0, stat_mod.S_IFLNK | 0o777)),      # a link
    (CG, (899, stat_mod.S_IFDIR | 0o755)),                                        # the leaf itself
])
def test_make_cgroup_refuses_an_untrusted_ancestor(node):
    path, st = node
    parents = {"/sys/fs/cgroup/levain.slice": (0, stat_mod.S_IFDIR | 0o755),
               "/sys/fs/cgroup/levain.slice/e0.scope": (0, stat_mod.S_IFDIR | 0o755)}
    fs = CgFS(**{p: v for p, v in parents.items() if len(p) < len(path)})
    fs.nodes[path] = st
    with pytest.raises(Refused) as e:
        H.make_cgroup(CG, mkdir=fs.mkdir, lstat=fs.lstat)
    assert e.value.name == "cgroup-untrusted"
    assert all(len(p) <= len(path) for p, _ in fs.made)             # nothing made below the refused one


@pytest.mark.parametrize("bad", ["/sys/fs/cgroup/other/x", "/sys/fs/cgroup/levain.slice/../x",
                                 "/sys/fs/cgroup/levain.slice//x", "/sys/fs/cgroup/levain.slice"])
def test_make_cgroup_refuses_a_path_off_the_slice(bad):
    fs = CgFS()
    with pytest.raises(Refused) as e:
        H.make_cgroup(bad, mkdir=fs.mkdir, lstat=fs.lstat)
    assert e.value.name == "cgroup-untrusted" and fs.made == []


def test_make_cgroup_without_the_cgroupfs_refused():
    fs = CgFS()
    del fs.nodes["/sys/fs/cgroup"]
    with pytest.raises(Refused) as e:
        H.make_cgroup(CG, mkdir=fs.mkdir, lstat=fs.lstat)
    assert e.value.name == "cgroup-untrusted" and fs.made == []


# --- F4 (VM run s2-linux): the view's ancestor dirs, with §2.1 / §2.2's modes ---------------------------------------

def _dirs(args):
    out, i = [], 0
    while i < len(args):
        if args[i] == "--perms" and args[i + 2] == "--dir":
            out.append((args[i + 3], int(args[i + 1], 8)))
            i += 4
        else:
            i += 1
    return out


@pytest.mark.parametrize("slot,kw", [(E0, dict(kind="interactive", queue_grant=False)),
                                     (SlotId("egress", 1), dict(kind="planner", queue_grant=True)),
                                     (SlotId("none", 0), dict(kind="verifier", queue_grant=False))])
def test_view_ancestors_precede_their_binds_with_layouts_modes(slot, kw):
    from levain.slot.layout import base_run_rows, base_store_rows

    links = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}
    args = list(wall.linux_view_args(slot, SESSION, host_links=links, **kw))
    dirs = _dirs(args)
    first_bind = args.index("--bind")
    assert all(args.index(p) < first_bind for p, _ in dirs)
    binds = [args[i + 1] for i, x in enumerate(args) if x == "--bind"]
    made = [p for p, _ in dirs]
    for b in binds:
        anc = [str(a) for a in reversed(PurePosixPath(b).parents) if str(a) != "/"]
        assert all(a in made for a in anc) and [m for m in made if m in anc] == anc    # parents first
    rows = {str(r.path): r.mode for r in base_store_rows("linux", "-") + base_run_rows("linux") if r.kind == "dir"}
    for p, mode in dirs:
        assert mode == rows.get(p, 0o755), p                         # §2.1 / §2.2's row, where it has one
    assert ("/var/levain", 0o755) in dirs and ("/var/levain/live", 0o711) in dirs
    assert len(made) == len(set(made))
