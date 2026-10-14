"""The scoped ssh agent (B2a; :mod:`levain.firing.ssh_agent`), RUN against this host's OpenSSH: a real
``ssh-agent`` is started, asked for its peer pid, refused a provider, swept and stopped."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import levain.firing.ssh_agent as ssh_agent

pytestmark = pytest.mark.skipif(ssh_agent.openssh_problem() is not None,
                                reason=f"no usable OpenSSH here: {ssh_agent.openssh_problem()}")


@pytest.fixture
def home(monkeypatch):
    """A short LEVAIN_HOME (a socket path has ~100 bytes) under /tmp, 0700, and the real run_root."""
    d = Path(tempfile.mkdtemp(prefix="lvh", dir="/tmp"))
    monkeypatch.setenv("LEVAIN_HOME", str(d))
    monkeypatch.setattr(ssh_agent, "run_root", lambda: (Path(os.environ["LEVAIN_HOME"]) / "run").resolve())
    started: list[ssh_agent.ScopedAgent] = []
    real_start = ssh_agent.start

    def tracked(**kw):
        a = real_start(**kw)
        started.append(a)
        return a

    monkeypatch.setattr(ssh_agent, "start", tracked)
    yield d
    for a in started:
        ssh_agent.stop(a)
    shutil.rmtree(d, ignore_errors=True)


def test_the_agent_starts_on_a_private_socket_and_answers_with_its_own_pid(home):
    a = ssh_agent.start(entity="e1")
    assert Path(a.socket).parent == ssh_agent.run_root() / a.session_id
    assert stat.S_IMODE(os.stat(a.dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(ssh_agent.run_root()).st_mode) == 0o700
    assert stat.S_ISSOCK(os.lstat(a.socket).st_mode)
    assert ssh_agent.peer_pid(a.socket) == a.pid
    rec = ssh_agent.read_record(a.dir)
    assert rec["pid"] == a.pid and rec["owner"] == os.getpid() and rec["keys"] == []
    env = dict(os.environ, SSH_AUTH_SOCK=a.socket)
    listed = subprocess.run(["ssh-add", "-l"], env=env, capture_output=True, text=True)
    assert listed.returncode == 1 and "no identities" in listed.stdout
    assert ssh_agent.stop(a) is None
    assert not ssh_agent._alive(a.pid)
    assert not a.dir.exists()


def test_ssh_add_s_cannot_make_the_agent_load_a_provider(home):
    a = ssh_agent.start()
    env = dict(os.environ, SSH_AUTH_SOCK=a.socket)
    done = subprocess.run(["ssh-add", "-s", sys.executable], env=env, capture_output=True, text=True,
                          input="\n")
    assert done.returncode != 0
    assert "refused" in done.stderr


def test_a_session_dir_under_a_directory_others_can_write_is_refused(home):
    os.chmod(home, 0o777)
    with pytest.raises(ssh_agent.AgentError, match="can be written by another user"):
        ssh_agent.start()
    assert not any(n for n in os.listdir(home / "run")), "the refused session left its directory"


def test_a_socket_path_too_long_for_a_unix_socket_is_refused(monkeypatch, home):
    deep = home / ("d" * 90)
    deep.mkdir(mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(deep))
    with pytest.raises(ssh_agent.AgentError, match="longer than a unix socket path"):
        ssh_agent.start()


def test_a_dead_levains_agent_is_swept_and_a_live_ones_is_not(home):
    mine = ssh_agent.start()
    orphan = ssh_agent.start()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    rec = ssh_agent.read_record(orphan.dir)
    rec["owner"] = gone.pid
    ssh_agent.write_record(orphan.dir, rec)
    # The orphan was started by this process, so drop the handle: the sweep must find it by its record.
    ssh_agent._POPENS.pop(orphan.pid)
    assert ssh_agent.sweep_orphans() == [orphan.session_id]
    assert not orphan.dir.exists()
    deadline = __import__("time").monotonic() + 5
    while ssh_agent._alive(orphan.pid) and __import__("time").monotonic() < deadline:
        try:
            os.waitpid(orphan.pid, os.WNOHANG)
        except ChildProcessError:
            break
    assert not ssh_agent._alive(orphan.pid)
    assert ssh_agent.peer_pid(mine.socket) == mine.pid


def test_a_record_whose_pid_is_not_the_socket_peer_is_never_signalled(home):
    a = ssh_agent.start()
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        rec = ssh_agent.read_record(a.dir)
        rec["owner"] = 2 ** 22 + 7   # no such process
        rec["pid"] = bystander.pid
        ssh_agent.write_record(a.dir, rec)
        assert ssh_agent.sweep_orphans() == [a.session_id]
        assert bystander.poll() is None, "a pid that does not answer on the socket was signalled"
    finally:
        bystander.kill()
        bystander.wait()


def test_the_binding_form_round_trips_and_refuses_another_shape():
    a = ssh_agent.ScopedAgent("0123456789ab", "/h/run/0123456789ab/agent.sock", 4242)
    assert ssh_agent.ScopedAgent.from_params(a.to_params()) == a
    for bad in ({**a.to_params(), "socket": "/h/run/ffffffffffff/agent.sock"},
                {**a.to_params(), "pid": True}, {**a.to_params(), "extra": 1}, "x"):
        with pytest.raises(ssh_agent.AgentError):
            ssh_agent.ScopedAgent.from_params(bad)
