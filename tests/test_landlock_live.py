"""The unix-domain connect policy, live (Linux with a working bwrap only; the CI "bwrap floor live" job).

Asserts what the running kernel can show. The tier is READ from it: below Landlock ABI 6 (T0, what a
CI runner usually has) the launch must still be offered and the tier line must withhold the claim; the
T6 and T9 checks skip there, naming the ABI they read. On a T9 or T6 kernel they run the canary's
contract end to end through a real confined bash."""
from __future__ import annotations

import os
import platform
import socket
import tempfile
from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing import landlock as L
from levain.firing.confinement import BwrapProvider, build_policy

pytestmark = pytest.mark.skipif(
    not (platform.system() == "Linux" and C.bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


@pytest.fixture
def policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    return build_policy(ent)


ABI = L.running_abi() if platform.system() == "Linux" else 0
TIER = L.tier_for(ABI)


def _connect_cmd(path: str) -> str:
    code = ("import socket,errno,sys\ns=socket.socket(socket.AF_UNIX)\ntry:\n s.connect(sys.argv[1]);print('CONNECTED')\n"
            "except OSError as e:\n print(errno.errorcode.get(e.errno,e.errno))")
    return f"python3 -I -S -c {C.shlex.quote(code)} {C.shlex.quote(path)}"


def test_the_launch_is_offered_on_every_tier_and_the_line_matches_it(policy) -> None:
    shell = BwrapProvider().spawn_shell(policy)
    try:
        r = shell.run("echo ran")
    finally:
        shell.close()
    assert r.exit_code == 0 and "ran" in r.output, r.output
    line = L.tier_line(ABI)
    assert f"({TIER}, Landlock ABI {ABI})" in line
    if TIER == "T0":
        assert "NO AGENT CLAIM" in line


def test_t9_refuses_a_host_socket_and_admits_one_made_inside(policy) -> None:
    if TIER != "T9":
        pytest.skip(f"the running kernel reads Landlock ABI {ABI} ({TIER}); T9 needs ABI 9 or later")
    d = tempfile.mkdtemp(dir="/tmp")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(os.path.join(d, "host"))
    srv.listen(4)
    shell = BwrapProvider().spawn_shell(policy)
    try:
        outside = shell.run(_connect_cmd(os.path.join(d, "host")))
        inside = shell.run(
            "python3 -I -S -c \"import socket,os;p='/tmp/lv-in-%d'%os.getpid();s=socket.socket(socket.AF_UNIX);"
            "s.bind(p);s.listen(1);c=socket.socket(socket.AF_UNIX);c.connect(p);print('CONNECTED');os.unlink(p)\"")
    finally:
        shell.close()
        srv.close()
    assert outside.output.split()[-1] in ("EACCES", "EPERM"), outside.output
    assert "CONNECTED" in inside.output, inside.output


def test_t6_leaves_pathname_sockets_reachable(policy) -> None:
    if TIER != "T6":
        pytest.skip(f"the running kernel reads Landlock ABI {ABI} ({TIER}); this is the T6 (ABI 6..8) check")
    d = tempfile.mkdtemp(dir="/tmp")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(os.path.join(d, "host"))
    srv.listen(4)
    shell = BwrapProvider().spawn_shell(policy)
    try:
        r = shell.run(_connect_cmd(os.path.join(d, "host")))
    finally:
        shell.close()
        srv.close()
    assert "CONNECTED" in r.output, r.output
