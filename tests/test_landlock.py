"""The unix-domain connect policy (levain.firing.landlock): the ruleset per running ABI, the grants, the
spawn hygiene, the canary verdicts and the wiring in front of bash. Every Landlock syscall here is
REPLACED: these tests never issue one, on any platform. The live run is tests/test_landlock_live.py."""
from __future__ import annotations

import errno
import os
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from levain.firing import landlock as L

NS = L._ns


def _attr(abi):
    return L.plan(abi)[1]


@pytest.mark.parametrize("abi", [0, 1, 4, 5])
def test_below_abi_6_there_is_no_ruleset_at_all(abi):
    tier, attr, rules = L.plan(abi, ["/run/agent.sock"])
    assert (tier, attr, rules) == ("T0", None, [])


@pytest.mark.parametrize("abi", [6, 8])
def test_abi_6_to_8_scopes_only_and_never_handles_the_unix_resolve_right(abi):
    tier, attr, rules = L.plan(abi, ["/run/agent.sock"], "/tmp/private")
    fs, net, scoped = struct.unpack("=QQQ", attr)
    assert tier == "T6" and rules == []
    assert fs == 0 and net == 0
    assert scoped == NS["SCOPE_ABSTRACT_UNIX_SOCKET"] | NS["SCOPE_SIGNAL"]


@pytest.mark.parametrize("abi", [9, 10])
def test_abi_9_on_handles_unix_resolve_with_exact_inode_grants(abi):
    tier, attr, rules = L.plan(abi, ["/run/agent.sock"], "/tmp/private")
    fs, net, scoped = struct.unpack("=QQQ", attr)
    assert tier == "T9"
    # Only connect-relevant rights, plus REFER granted everywhere so renames across directories are
    # exactly as before (a filesystem-handling ruleset denies them otherwise).
    assert fs == NS["FS_RESOLVE_UNIX"] | NS["FS_REFER"] and net == 0
    assert scoped == NS["SCOPE_ABSTRACT_UNIX_SOCKET"] | NS["SCOPE_SIGNAL"]
    assert rules == [("/", NS["FS_REFER"], "dir"),
                     ("/run/agent.sock", NS["FS_RESOLVE_UNIX"], "socket"),
                     ("/tmp/private", NS["FS_RESOLVE_UNIX"], "dir")]
    # The unix-resolve right is granted only to the socket itself and the private tmp: never "/".
    assert all(acc != NS["FS_RESOLVE_UNIX"] or p != "/" for p, acc, _ in rules)


def test_the_unix_resolve_bit_and_its_abi_are_the_documented_ones():
    assert NS["FS_RESOLVE_UNIX"] == 1 << 16 and NS["UNIX_RESOLVE_ABI"] == 9 and NS["SCOPE_ABI"] == 6
    assert "ninth version of the Landlock ABI" in L.STUB


class _Calls:
    def __init__(self):
        self.log = []

    def syscall(self, nr, *args):
        self.log.append(("syscall", nr, args))
        # The "ruleset" is a real fd the code under test may close.
        return os.open(os.devnull, os.O_RDONLY) if nr == NS["SYS_CREATE_RULESET"] else 0


def test_apply_creates_adds_each_rule_then_restricts(monkeypatch):
    c = _Calls()
    opened = []
    monkeypatch.setitem(NS, "syscall", c.syscall)
    monkeypatch.setitem(NS, "no_new_privs", lambda: c.log.append(("nnp",)))
    monkeypatch.setitem(NS, "open_rule_target", lambda p, k: opened.append((p, k)) or os.open(os.devnull, os.O_RDONLY))
    tier, attr, rules = L.plan(10, ["/run/a.sock"])
    NS["apply"](attr, rules)
    kinds = [e[1] if e[0] == "syscall" else e[0] for e in c.log]
    assert kinds == [444, 445, 445, "nnp", 446]
    assert opened == [("/", "dir"), ("/run/a.sock", "socket")]
    raw = bytes(c.log[2][2][2])
    access, _fd = struct.unpack("=Qi", raw)
    assert access == NS["FS_RESOLVE_UNIX"]


def test_a_failing_rule_names_its_path(monkeypatch):
    c = _Calls()
    monkeypatch.setitem(NS, "syscall", c.syscall)
    monkeypatch.setitem(NS, "no_new_privs", lambda: None)

    def gone(p, k):
        if p == "/":
            return os.open(os.devnull, os.O_RDONLY)
        raise OSError(errno.ENOENT, "No such file or directory")

    monkeypatch.setitem(NS, "open_rule_target", gone)
    with pytest.raises(OSError, match="/run/a.sock"):
        NS["apply"](*L.plan(10, ["/run/a.sock"])[1:])
    assert not any(e[1] == 446 for e in c.log if e[0] == "syscall")


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the off-Linux refusal")
def test_off_linux_the_syscall_layer_refuses_without_calling():
    with pytest.raises(OSError) as e:
        NS["syscall"](444, None, 0, 1)
    assert e.value.errno == errno.ENOSYS
    with pytest.raises(OSError):
        NS["no_new_privs"]()


def test_running_abi_never_calls_the_kernel_off_linux(monkeypatch):
    def boom():
        raise AssertionError("syscall reached")

    monkeypatch.setitem(NS, "running_abi", boom)
    assert L.running_abi(system="Darwin") == 0


def test_running_abi_reads_the_version_flag_and_is_0_without_landlock(monkeypatch):
    seen = []
    monkeypatch.setitem(NS, "syscall", lambda nr, *a: seen.append((nr, a[2].value)) or 10)
    assert NS["running_abi"]() == 10 and seen == [(444, 1)]

    def nosys(nr, *a):
        raise OSError(errno.EOPNOTSUPP, "disabled")

    monkeypatch.setitem(NS, "syscall", nosys)
    assert NS["running_abi"]() == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="O_PATH is Linux")
def test_a_grant_is_a_socket_inode_never_a_directory_or_file(tmp_path):
    s = socket.socket(socket.AF_UNIX)
    sock = tmp_path / "s"
    s.bind(str(sock))
    try:
        os.close(NS["open_rule_target"](str(sock), "socket"))
        for bad in (tmp_path, tmp_path / "f"):
            (tmp_path / "f").write_text("x")
            with pytest.raises(OSError):
                NS["open_rule_target"](str(bad), "socket")
        link = tmp_path / "l"
        link.symlink_to(sock)
        with pytest.raises(OSError):
            NS["open_rule_target"](str(link), "socket")
    finally:
        s.close()


# --- spawn hygiene ------------------------------------------------------------------------------

_HYGIENE_CHILD = r"""
import os, stat, sys
exec(compile(sys.argv[1], "defs", "exec"))
hygiene(sys.argv[2] == "carry")
out = []
for fd in (0, 1, 2):
    out.append("sock" if stat.S_ISSOCK(os.fstat(fd).st_mode) else "fifo" if stat.S_ISFIFO(os.fstat(fd).st_mode) else "other")
out.append("extra-open" if os.path.exists("/dev/fd/%s" % sys.argv[3]) and _alive(int(sys.argv[3])) else "extra-closed")
os.write(1, (" ".join(out) + "\n").encode())
"""


def _hygiene(stdin, stdout, carry, extra):
    child = "def _alive(fd):\n    try:\n        os.fstat(fd)\n        return True\n    except OSError:\n        return False\n" + _HYGIENE_CHILD
    return subprocess.Popen([sys.executable, "-I", "-S", "-c", child, L._STUB_DEFS, carry, str(extra)],
                            stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL, pass_fds=(extra,))


def test_a_socketpair_handed_as_stdio_never_reaches_the_command():
    a, b = socket.socketpair()
    out_a, out_b = socket.socketpair()
    extra_r, extra_w = os.pipe()
    p = _hygiene(b.fileno(), out_b.fileno(), "no", extra_w)
    b.close()
    out_b.close()
    os.close(extra_w)
    p.wait(timeout=30)
    out_a.settimeout(10)
    data = b""
    while not data.endswith(b"\n"):
        chunk = out_a.recv(4096)
        if not chunk:
            break
        data += chunk
    # stdin a socket -> /dev/null (a character device); stdout a socket -> a pipe whose bytes still
    # arrive (the relay); an fd above 2 the parent passed -> closed.
    assert data.decode().split() == ["other", "fifo", "other", "extra-closed"]
    a.close()
    out_a.close()
    os.close(extra_r)


def test_levains_own_state_channel_on_stdin_is_kept():
    a, b = socket.socketpair()
    r, w = os.pipe()
    extra_r, extra_w = os.pipe()
    p = _hygiene(b.fileno(), w, "carry", extra_w)
    b.close()
    os.close(w)
    os.close(extra_w)
    out = os.read(r, 4096).decode().split()
    p.wait(timeout=30)
    assert out[0] == "sock" and out[1] == "fifo" and out[3] == "extra-closed"
    a.close()
    os.close(r)
    os.close(extra_r)


def test_a_connected_named_socket_on_stdin_is_not_taken_for_the_state_channel():
    srv = socket.socket(socket.AF_UNIX)
    path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "agent")   # short: AF_UNIX paths are bounded
    srv.bind(path)
    srv.listen(1)
    cli = socket.socket(socket.AF_UNIX)
    cli.connect(path)
    r, w = os.pipe()
    extra_r, extra_w = os.pipe()
    p = _hygiene(cli.fileno(), w, "carry", extra_w)
    cli.close()
    os.close(w)
    os.close(extra_w)
    out = os.read(r, 4096).decode().split()
    p.wait(timeout=30)
    assert out[0] == "other"
    srv.close()
    os.close(r)
    os.close(extra_r)


# --- the canary verdict -------------------------------------------------------------------------

@pytest.mark.parametrize("tier,pathname,abstract,ok", [
    ("T9", "EACCES", "EPERM", True),
    ("T9", "EPERM", "EPERM", True),
    ("T9", "CONNECTED", "EPERM", False),
    ("T9", "ENOENT", "EPERM", False),
    ("T9", "ECONNREFUSED", "EPERM", False),
    ("T9", "EACCES", "CONNECTED", False),
    ("T9", "TIMEOUT", "EPERM", False),
    ("T6", "CONNECTED", "EPERM", True),
    ("T6", "EACCES", "EPERM", False),
    ("T6", "CONNECTED", "CONNECTED", False),
    ("T6", "ENOENT", "EPERM", False),
    ("T0", "CONNECTED", "CONNECTED", True),
    ("T0", "ENOENT", "ECONNREFUSED", True),
])
def test_the_canary_verdict_per_tier(tier, pathname, abstract, ok):
    assert (L.judge(tier, pathname, abstract) is None) is ok


def test_the_stub_argv_is_levains_and_ends_at_the_command():
    argv = L.stub_argv("/usr/bin/python3", canary="/run/levain-canary", grants=("/run/g.sock",))
    assert argv[:5] == ["/usr/bin/python3", "-I", "-S", "-c", L.STUB] and argv[-1] == "--"
    opts, cmd = NS["parse"](argv[5:] + ["/bin/bash", "-p"])
    assert opts["grant"] == ["/run/g.sock"] and opts["default-grant"] == ["/dev/log"]
    assert opts["canary"] == "/run/levain-canary" and opts["carry-stdin"] is True
    assert cmd == ["/bin/bash", "-p"]
    with pytest.raises(ValueError):
        NS["parse"](["--bogus", "--", "/bin/bash"])
    with pytest.raises(ValueError):
        NS["parse"](["--canary", "/x", "--"])


def test_the_stub_imports_nothing_of_levain():
    assert "levain" not in [ln.split()[1] for ln in L.STUB.splitlines() if ln.startswith(("import ", "from "))]
    assert "import levain" not in L.STUB and "from levain" not in L.STUB


def test_tier_lines_come_from_the_one_table():
    assert "NO AGENT CLAIM" in L.tier_line(4) and "(T0, Landlock ABI 4)" in L.tier_line(4)
    assert L.tier_line(8).endswith(L.TIER_CLAIMS["T6"]) and L.tier_line(10).endswith(L.TIER_CLAIMS["T9"])


def test_the_canary_answers_and_is_removed(tmp_path):
    c = L.Canary()
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        for _ in range(40):   # more than the backlog: each is accepted and closed
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(5)
            s.connect(c.path)
            s.close()
        assert c.bind_ops() == ["--bind", c.path, c.path]
    finally:
        c.stop()
    assert not os.path.exists(c.path) and not os.path.exists(c.dir)
    c.stop()


def test_the_stub_runs_right_before_bash_with_the_canary_bound(tmp_path, monkeypatch):
    import levain.firing.confinement as conf
    from levain.firing.confinement import build_policy

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    seen = {}
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: seen.setdefault("shell", self))
    monkeypatch.setattr(conf._BwrapShell, "_recheck", lambda self: None)
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    conf.BwrapProvider()._spawn_shell_impl(build_policy(ent), env={"PATH": "/usr/bin"})
    sh = seen["shell"]
    argv = sh._argv
    at = argv.index(L.STUB)
    bash = argv.index("/bin/bash", at)
    assert argv[at - 4:at] == [sys.executable, "-I", "-S", "-c"]
    assert argv[bash - 1] == "--" and argv[bash:] == ["/bin/bash", "--noprofile", "--norc", "-p"]
    canary = sh._canary
    assert argv[argv.index("--canary", at) + 1] == canary.path
    i = argv.index("--new-session")
    assert argv[i - 3:i] == ["--bind", canary.path, canary.path]
    assert os.path.exists(canary.path)
    sh.close()
    assert not os.path.exists(canary.path)
