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


# --- the wiring: binding, spawn checks, the session's start ----------------------------------------

def test_the_binding_carries_the_agent_and_refuses_it_beside_a_hands_user(tmp_path):
    import json

    from levain.firing.binding import BindingError, ConversationBinding

    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    a = ssh_agent.ScopedAgent("0123456789ab", "/h/run/0123456789ab/agent.sock", 4242)
    b = ConversationBinding.create(ent, mode="interactive", workspace=tmp_path / "ws", ssh_agent=a)
    params = json.loads(json.dumps(b.to_params()))   # JSON, as the SDK's autosave keeps it
    assert ConversationBinding.from_params(params).ssh_agent == a
    params["ssh_agent"]["pid"] = "4242"
    with pytest.raises(BindingError, match="ssh agent"):
        ConversationBinding.from_params(params)
    params["ssh_agent"] = a.to_params()
    params["hands"] = {"user": "_levain_x", "uid": 900, "home": "/h", "workspace": str(tmp_path / "ws")}
    with pytest.raises(BindingError, match="hands-user binding carries a scoped ssh agent"):
        ConversationBinding.from_params(params)


def test_the_spawn_binds_the_agent_only_while_its_started_pid_answers(home, tmp_path):
    from levain.firing.confinement import ConfinementError, _ssh_agent_ops

    a = ssh_agent.start()
    rt = tmp_path / "rt"
    ops, view = _ssh_agent_ops(a, [rt])
    assert view == str(rt / "levain-ssh" / "agent.sock")
    assert ops == ["--perms", "0700", "--dir", str(rt / "levain-ssh"), "--bind", a.socket, view]
    impostor = ssh_agent.ScopedAgent(a.session_id, a.socket, a.pid + 1)
    with pytest.raises(ConfinementError, match=f"answered by pid {a.pid}, not the agent levain started"):
        _ssh_agent_ops(impostor, [rt])
    with pytest.raises(ConfinementError, match="no user runtime dir"):
        _ssh_agent_ops(a, [])
    ssh_agent.stop(a)
    with pytest.raises(ConfinementError, match="cannot be read"):
        _ssh_agent_ops(a, [rt])


def test_no_agent_is_started_where_it_is_not_bound_into_bash(home):
    from levain.firing.confinement import start_session_ssh_agent

    agent, why = start_session_ssh_agent("e", system="Darwin")
    assert agent is None and "not wired on Darwin" in why
    assert not (home / "run").exists()


def test_the_session_close_stops_its_agent(home, tmp_path):
    from levain.session import EntitySession

    class _Conv:
        def close(self):
            pass

    a = ssh_agent.start()
    s = EntitySession(entity_dir=tmp_path, binding=None, conversation=_Conv(), workspace=tmp_path,
                      model_label="m", with_tools=True, bash_ok=True, ssh_agent=a)
    s.close()
    assert s.release_error is None
    assert not ssh_agent._alive(a.pid) and not a.dir.exists()


# --- levain ssh-add (RUN: a real key, a real agent, ssh-add -H/-h; the Linux branch, with the
# no_new_privs read stubbed where this host has no /proc) ------------------------------------------

@pytest.fixture
def keyed(home, tmp_path, monkeypatch):
    """A running agent, a client key, and a manifest holding one host key for ``git.example.test``."""
    monkeypatch.setattr(ssh_agent, "_no_new_privs", lambda: False)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "id")], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "hostkey")], check=True)
    (home / "ssh").mkdir(mode=0o700)
    manifest = home / "ssh" / "known_hosts"
    manifest.write_text("git.example.test " + (tmp_path / "hostkey.pub").read_text().split(" ", 2)[0] + " "
                        + (tmp_path / "hostkey.pub").read_text().split(" ", 2)[1] + "\n")
    os.chmod(manifest, 0o600)
    agent = ssh_agent.start(entity="ent")
    return agent, tmp_path / "id", manifest


def _listed(agent):
    env = dict(os.environ, SSH_AUTH_SOCK=agent.socket)
    return subprocess.run(["ssh-add", "-l"], env=env, capture_output=True, text=True).stdout


def test_a_key_goes_in_limited_to_its_host_and_is_recorded(keyed):
    agent, key, _manifest = keyed
    line = ssh_agent.add_key(key, ["git.example.test"], lifetime=600, system="Linux")
    fp = ssh_agent.fingerprint(key)
    assert f"pid {agent.pid}, peer checked" in line and "for git.example.test" in line
    assert fp in _listed(agent)
    [k] = ssh_agent.read_record(agent.dir)["keys"]
    assert k["fingerprint"] == fp and k["hosts"] == ["git.example.test"] and k["lifetime"] == 600
    assert not k["waived"]


def test_a_key_is_gone_after_its_lifetime(keyed):
    import time

    agent, key, _ = keyed
    ssh_agent.add_key(key, ["git.example.test"], lifetime=1, system="Linux")
    time.sleep(2.5)
    assert ssh_agent.fingerprint(key) not in _listed(agent)


def test_refusals_add_nothing(keyed, monkeypatch):
    agent, key, manifest = keyed
    with pytest.raises(ssh_agent.AgentError, match="name the hosts"):
        ssh_agent.add_key(key, [], system="Linux")
    with pytest.raises(ssh_agent.AgentError, match="other.example.test has no host key"):
        ssh_agent.add_key(key, ["git.example.test>other.example.test"], system="Linux")
    with pytest.raises(ssh_agent.AgentError, match="between 1 and"):
        ssh_agent.add_key(key, ["git.example.test"], lifetime=ssh_agent.MAX_LIFETIME + 1, system="Linux")
    os.chmod(manifest, 0o620)
    with pytest.raises(ssh_agent.AgentError, match="no one else can write"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    os.chmod(manifest, 0o600)
    rec = ssh_agent.read_record(agent.dir)
    rec["pid"] = agent.pid + 1
    ssh_agent.write_record(agent.dir, rec)
    with pytest.raises(ssh_agent.AgentError, match="no levain session with a scoped ssh agent is running"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    monkeypatch.setattr(ssh_agent, "_no_new_privs", lambda: True)
    with pytest.raises(ssh_agent.AgentError, match="no_new_privs"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    assert "no identities" in _listed(agent)


def test_the_waiver_adds_for_any_host_and_is_recorded_as_one(keyed):
    agent, key, _ = keyed
    line = ssh_agent.add_key(key, [], waive_destinations=True, system="Linux")
    assert "ANY host" in line
    assert ssh_agent.read_record(agent.dir)["keys"][0]["waived"] is True


def test_two_sessions_need_a_session_id(keyed):
    agent, key, _ = keyed
    other = ssh_agent.start(entity="other")
    with pytest.raises(ssh_agent.AgentError, match="more than one levain session"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    ssh_agent.add_key(key, ["git.example.test"], session=other.session_id, system="Linux")
    assert ssh_agent.fingerprint(key) in _listed(other)
    assert ssh_agent.fingerprint(key) not in _listed(agent)
