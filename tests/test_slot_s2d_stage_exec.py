"""S1 session 2d: the stage's exec half in a REAL process (T:2/test_lease_fd_absent_in_vendor, the env and cwd rows).

The stage runs as this test's own uid: the three id-setting calls are no-ops (the process already IS that user)
and the group read returns the spec's set, but ``seteuid(0)`` / ``setegid(0)`` are the REAL calls and really fail
(EPERM), and the fd close, the chdir and the ``execve`` are real. The spec carries no profile, and the runner's
execve refuses a sandbox-exec path outright: no Seatbelt profile is ever applied here (trg-069)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot import wall
from tests.execve_guard import refuse_real_execve

pytestmark = pytest.mark.skipif(os.getuid() == 0, reason="the stage refuses uid 0 by design")

@pytest.fixture(autouse=True)
def _no_real_execve(monkeypatch):
    """The stage runs only in the RUNNER's own process here; nothing in this one may exec."""
    yield from refuse_real_execve(monkeypatch, wall.os)


ROOT = str(Path(wall.__file__).resolve().parents[2])

RUNNER = """
import os, sys
sys.path.insert(0, {root!r})
from levain.slot import wall
data = open(sys.argv[1], "rb").read()
groups = [int(g) for g in sys.argv[2].split(",")]
fd = os.open("/dev/null", os.O_RDONLY)
os.dup2(fd, 9)            # the lease fd stand-in: open, NOT close-on-exec
os.close(fd)
fd = os.open("/dev/null", os.O_RDWR)
os.dup2(fd, 7)            # the pty stand-in
os.close(fd)
r, w = os.pipe()
os.write(w, data)
os.close(w)
if r != 3:                # the pipe may already sit on fd 3
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
calls.getgroups = lambda: list(groups)
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
             "print(sorted(live)); print(os.getcwd())\n")


def _run(tmp_path, argv, *, env_text="HOME=/h\nPATH=/usr/bin:/bin\n", extra=(("TERM", "xterm"),), env_file=True):
    root = tmp_path / "S" / "root"
    repo = tmp_path / "S" / "repo"
    root.mkdir(parents=True)
    repo.mkdir()
    (root / "env").write_text(env_text)
    gid = os.getgid()
    spec = wall.Spec("vendor", os.getuid(), gid, (gid,), wall.NPROC, str(root / "env") if env_file else
                     str(root / "absent"), tuple(extra), None, str(repo.resolve()), tuple(argv), False, (7,))
    (tmp_path / "spec").write_bytes(spec.to_json())
    (tmp_path / "runner.py").write_text(RUNNER.format(root=ROOT))
    return subprocess.run([sys.executable, "-I", str(tmp_path / "runner.py"), str(tmp_path / "spec"), str(gid)],
                          capture_output=True, text=True, timeout=60), repo.resolve()


def test_lease_fd_absent_in_vendor_real_exec(tmp_path):
    r, repo = _run(tmp_path, [sys.executable, "-I", "-c", FD_LISTER])
    assert r.returncode == 0, r.stderr
    fds, cwd = r.stdout.splitlines()
    assert fds == "[0, 1, 2, 7]"          # the pty stand-in kept; the lease stand-in (9) and the spec fd (3) gone
    assert Path(cwd).resolve() == repo


def test_env_is_exactly_the_file_plus_extras_real_exec(tmp_path):
    r, _ = _run(tmp_path, ["/usr/bin/env"], env_text="HOME=/h\nPATH=/usr/bin:/bin\nA=b=c\n",
                extra=(("LEVAIN_QUEUE_CAP", "cap"), ("TERM", "xterm")))
    assert r.returncode == 0, r.stderr
    assert sorted(r.stdout.splitlines()) == sorted(["HOME=/h", "PATH=/usr/bin:/bin", "A=b=c",
                                                    "LEVAIN_QUEUE_CAP=cap", "TERM=xterm"])


def test_refusal_execs_nothing_real(tmp_path):
    r, _ = _run(tmp_path, ["/usr/bin/env"], env_file=False)
    assert r.returncode == wall.STAGE_EXIT and r.stdout == ""
    assert "levain wall: refused" in r.stderr and "xterm" not in r.stderr
