"""The scoped ssh agent (B2a; :mod:`levain.firing.ssh_agent`), RUN against this host's OpenSSH: a real
``ssh-agent`` is started, asked for its peer pid, refused a provider, swept and stopped."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import levain.firing.ssh_agent as ssh_agent

# Only the tests that run OpenSSH skip without it (L1 #12): the binding shape and the no-agent paths
# run everywhere, so a regression in them never goes silent on a host without ssh tools.
needs_openssh = pytest.mark.skipif(ssh_agent.openssh_problem() is not None,
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


@needs_openssh
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


@needs_openssh
def test_ssh_add_s_cannot_make_the_agent_load_a_provider(home):
    a = ssh_agent.start()
    env = dict(os.environ, SSH_AUTH_SOCK=a.socket)
    done = subprocess.run(["ssh-add", "-s", sys.executable], env=env, capture_output=True, text=True,
                          input="\n")
    assert done.returncode != 0
    assert "refused" in done.stderr


@needs_openssh
def test_a_session_dir_under_a_directory_others_can_write_is_refused(home):
    os.chmod(home, 0o777)
    with pytest.raises(ssh_agent.AgentError, match="can be written by another user"):
        ssh_agent.start()
    assert not any(n for n in os.listdir(home / "run")), "the refused session left its directory"


@needs_openssh
def test_a_socket_path_too_long_for_a_unix_socket_is_refused(monkeypatch, home):
    deep = home / ("d" * 90)
    deep.mkdir(mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(deep))
    with pytest.raises(ssh_agent.AgentError, match="longer than a unix socket path"):
        ssh_agent.start()


@needs_openssh
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


@needs_openssh
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
    a = ssh_agent.ScopedAgent("0123456789ab", str(ssh_agent.run_root() / "0123456789ab" / "agent.sock"), 4242)
    assert ssh_agent.ScopedAgent.from_params(a.to_params()) == a
    for bad in ({**a.to_params(), "socket": str(ssh_agent.run_root() / "ffffffffffff" / "agent.sock")},
                {**a.to_params(), "socket": "/h/run/0123456789ab/agent.sock"},   # not under run_root()
                {**a.to_params(), "pid": True}, {**a.to_params(), "extra": 1}, "x"):
        with pytest.raises(ssh_agent.AgentError):
            ssh_agent.ScopedAgent.from_params(bad)


# --- the wiring: binding, spawn checks, the session's start ----------------------------------------

def test_the_binding_carries_the_agent_and_refuses_it_beside_a_hands_user(tmp_path):
    import json

    from levain.firing.binding import BindingError, ConversationBinding

    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    a = ssh_agent.ScopedAgent("0123456789ab", str(ssh_agent.run_root() / "0123456789ab" / "agent.sock"), 4242)
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


@needs_openssh
def test_the_spawn_binds_the_agent_only_while_its_started_pid_answers(home, tmp_path):
    from levain.firing.confinement import ConfinementError, _ssh_agent_ops

    a = ssh_agent.start()
    rt = tmp_path / "rt"
    ops, view = _ssh_agent_ops(a, [rt])
    assert view == str(rt / "levain-ssh" / "agent.sock")
    assert ops == ["--perms", "0700", "--dir", str(rt / "levain-ssh"), "--bind", a.socket, view,
                   "--bind", str(a.dir / "known_hosts"), str(rt / "levain-ssh" / "known_hosts")]
    impostor = ssh_agent.ScopedAgent(a.session_id, a.socket, a.pid + 1)
    with pytest.raises(ConfinementError, match=f"answered by pid {a.pid}, not the agent levain started"):
        _ssh_agent_ops(impostor, [rt])
    with pytest.raises(ConfinementError, match="no user runtime dir"):
        _ssh_agent_ops(a, [])
    ssh_agent.stop(a)
    with pytest.raises(ConfinementError, match="cannot be read"):
        _ssh_agent_ops(a, [rt])


@needs_openssh
def test_an_agent_restarted_on_its_path_closes_the_shell_though_the_socket_kept_its_inode(home, monkeypatch):
    """VM item 11 (T9, 2026-10-14): the operator's agent was killed and another started on the same
    path; ext4 gave the new socket the freed inode (12/12), so the manifest saw nothing and bash's
    `ssh-add -l` reached the new agent. Here the agents and the peer read are real; the inode reuse is
    stood in for by holding `_identity` to the recorded value (this filesystem may not reuse it)."""
    import levain.firing.confinement as conf

    monkeypatch.setattr(conf, "_refuse_plantable_sqlite_jewels", lambda policy: None)
    a = ssh_agent.start()
    held = conf._identity(Path(a.socket))

    def shell():
        sh = conf._BwrapShell(policy=None, manifest={a.socket: held}, argv=["/bin/false"],  # type: ignore[arg-type]
                              cwd=home, env={})
        sh._agent = (a.socket, a.pid, conf._process_start(a.pid))
        return sh

    with pytest.raises(conf.ConfinementError, match="not running"):   # the started agent answers: checks pass
        shell().run("echo x")
    ssh_agent._terminate(ssh_agent._POPENS.pop(a.pid))
    assert not os.path.exists(a.socket)   # ssh-agent removes its socket on SIGTERM
    rogue = subprocess.Popen(["ssh-agent", "-D", "-a", a.socket], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ssh_agent._tool_env())
    try:
        for _ in range(250):
            if os.path.exists(a.socket):
                break
            time.sleep(0.02)
        assert ssh_agent.peer_pid(a.socket) == rogue.pid
        monkeypatch.setattr(conf, "_identity", lambda p: held)   # ext4's reuse: the manifest sees no change
        sh = shell()
        with pytest.raises(conf.ConfinementError,
                           match=rf"now answered by pid {rogue.pid}, not the agent levain started \(pid {a.pid}\)"):
            sh.run("echo x")
        assert sh._closed
    finally:
        rogue.terminate()
        rogue.wait(5)
    with pytest.raises(conf.ConfinementError, match="cannot be checked"):   # nobody answers: refused too
        shell().run("echo x")


@needs_openssh
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the start time is read from /proc (Linux)")
def test_a_reused_pid_answering_on_the_path_closes_the_shell(home, monkeypatch):
    """L3 b2a r1 (codex MED): a replacement agent that got both the freed inode and the recorded pid back
    passed the peer-pid check. The pid's start time is compared too. The agent and both reads are real;
    the reuse is stood in for by recording a start time the answering process does not have."""
    import levain.firing.confinement as conf

    monkeypatch.setattr(conf, "_refuse_plantable_sqlite_jewels", lambda policy: None)
    a = ssh_agent.start()
    held = conf._identity(Path(a.socket))
    started = conf._process_start(a.pid)
    assert started is not None and started.isdigit()

    def shell(start):
        sh = conf._BwrapShell(policy=None, manifest={a.socket: held}, argv=["/bin/false"],  # type: ignore[arg-type]
                              cwd=home, env={})
        sh._agent = (a.socket, a.pid, start)
        return sh

    try:
        with pytest.raises(conf.ConfinementError, match="not running"):   # the same process: checks pass
            shell(started).run("echo x")
        sh = shell(str(int(started) + 1))   # same pid, another process
        with pytest.raises(conf.ConfinementError, match=rf"pid {a.pid}, but that pid is not the process"):
            sh.run("echo x")
        assert sh._closed
        with pytest.raises(conf.ConfinementError, match="recorded unreadable"):   # fail-closed
            shell(None).run("echo x")
    finally:
        ssh_agent.stop(a)


def test_the_process_start_time_is_field_22_read_after_the_last_paren(tmp_path, monkeypatch):
    """comm may hold `)` and spaces; field 22 is counted from after the last one."""
    import builtins

    import levain.firing.confinement as conf

    line = "4242 (a) b c) S " + " ".join(str(n) for n in range(4, 22)) + " 987654 23 24\n"
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: real_open(tmp_path / "stat", *a, **k)
                        if p == "/proc/4242/stat" else real_open(p, *a, **k))
    (tmp_path / "stat").write_text(line)
    assert conf._process_start(4242) == "987654"
    (tmp_path / "stat").write_text("4242 (no close paren S 1 2")
    assert conf._process_start(4242) is None
    assert conf._process_start(2 ** 22 + 7) is None   # above any pid_max: no such process


@needs_openssh
def test_gits_ssh_names_the_scoped_agent_over_any_identity_agent_in_the_ssh_config(tmp_path):
    """L2 #3: an `IdentityAgent` in ~/.ssh/config (1Password, Secretive, gpg) overrides $SSH_AUTH_SOCK;
    a command-line -o wins over the config file. Read back by ssh itself (`ssh -G`), not by eye."""
    import shlex

    from levain.firing.confinement import git_ssh_command

    cmd = git_ssh_command(tmp_path / "rt")
    assert cmd.startswith("ssh -o IdentityAgent=SSH_AUTH_SOCK -o UserKnownHostsFile=")
    cfg = tmp_path / "config"
    cfg.write_text("Host *\n  IdentityAgent /nonexistent/other-agent.sock\n")
    argv = [*shlex.split(cmd)[:1], "-F", str(cfg), *shlex.split(cmd)[1:], "-G", "git.example.test"]
    sock = str(tmp_path / "scoped.sock")
    done = subprocess.run(argv, env=dict(os.environ, SSH_AUTH_SOCK=sock), capture_output=True, text=True,
                          check=True)
    resolved = [ln for ln in done.stdout.splitlines() if ln.lower().startswith("identityagent ")]
    assert resolved == ["identityagent SSH_AUTH_SOCK"] or resolved == [f"identityagent {sock}"]
    control = subprocess.run(["ssh", "-F", str(cfg), "-G", "git.example.test"], capture_output=True, text=True,
                             env=dict(os.environ, SSH_AUTH_SOCK=sock), check=True)
    assert "identityagent /nonexistent/other-agent.sock" in control.stdout   # the config wins without the -o


def test_no_agent_is_started_where_it_is_not_bound_into_bash(home):
    from levain.firing.confinement import start_session_ssh_agent

    agent, why = start_session_ssh_agent("e", system="Darwin")
    assert agent is None and "not wired on Darwin" in why
    assert not (home / "run").exists()


@needs_openssh
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
    """A running agent, a client key under ``~/.ssh`` (HOME is ``tmp_path``), and a manifest holding one
    host key for ``git.example.test``."""
    if ssh_agent.openssh_problem() is not None:
        pytest.skip(f"no usable OpenSSH here: {ssh_agent.openssh_problem()}")
    monkeypatch.setattr(ssh_agent, "_no_new_privs", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir(mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / ".ssh" / "id")], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "hostkey")], check=True)
    (home / "ssh").mkdir(mode=0o700)
    manifest = home / "ssh" / "known_hosts"
    manifest.write_text("git.example.test " + (tmp_path / "hostkey.pub").read_text().split(" ", 2)[0] + " "
                        + (tmp_path / "hostkey.pub").read_text().split(" ", 2)[1] + "\n")
    os.chmod(manifest, 0o600)
    agent = ssh_agent.start(entity="ent")
    return agent, tmp_path / ".ssh" / "id", manifest


def _listed(agent):
    env = dict(os.environ, SSH_AUTH_SOCK=agent.socket)
    return subprocess.run(["ssh-add", "-l"], env=env, capture_output=True, text=True).stdout


def test_a_key_goes_in_limited_to_its_host_and_is_recorded(keyed):
    agent, key, _manifest = keyed
    line = ssh_agent.add_key(key, ["git.example.test"], lifetime=600, system="Linux")
    fp = ssh_agent.fingerprint(key)
    assert f"pid {agent.pid}, peer checked" in line and "for git.example.test" in line
    # the manifest key the limit binds to, by fingerprint, so the operator can check it (L1 #1)
    host_fp = ssh_agent.fingerprint(key.parent.parent / "hostkey.pub")
    assert f"bound to git.example.test's host key in {_manifest.resolve()}: {host_fp} (ED25519)" in line
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


def test_a_key_file_bash_could_read_is_refused(keyed, tmp_path):
    """L2 #2: re-adding a held key replaces its constraints, so a key file bash can read lets bash strip
    the -h limit. Only a key under ~/.ssh (one link) of an ssh_mode=agent entity goes in."""
    agent, key, _ = keyed
    outside = tmp_path / "keys"
    outside.mkdir()
    shutil.copy(key, outside / "deploy")
    os.chmod(outside / "deploy", 0o600)
    with pytest.raises(ssh_agent.AgentError, match="not a file only under"):
        ssh_agent.add_key(outside / "deploy", ["git.example.test"], system="Linux")
    os.symlink(outside / "deploy", key.parent / "via_link")   # resolves outside ~/.ssh
    with pytest.raises(ssh_agent.AgentError, match="not a file only under"):
        ssh_agent.add_key(key.parent / "via_link", ["git.example.test"], system="Linux")
    os.link(key, outside / "hard")                            # a second name bash could read
    with pytest.raises(ssh_agent.AgentError, match="links: 2"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    os.unlink(outside / "hard")
    rec = ssh_agent.read_record(agent.dir)
    rec["ssh_mode"] = "raw"
    ssh_agent.write_record(agent.dir, rec)
    with pytest.raises(ssh_agent.AgentError, match="ssh_mode=raw"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    assert "no identities" in _listed(agent)


def test_the_waiver_adds_for_any_host_and_is_recorded_as_one(keyed):
    agent, key, _ = keyed
    line = ssh_agent.add_key(key, [], waive_destinations=True, system="Linux")
    assert "ANY host, including hosts it is forwarded to" in line
    assert ssh_agent.read_record(agent.dir)["keys"][0]["waived"] is True


def test_ssh_add_t_0_is_refused_not_turned_into_the_default(keyed, capsys, monkeypatch):
    """L1 #6: `-t 0` became 8 hours (`args.lifetime or DEFAULT`)."""
    from levain.cli import main

    _agent, key, _ = keyed
    monkeypatch.setattr(ssh_agent, "confinement_refusal", lambda system=None: None)   # the Linux branch, here
    assert main(["ssh-add", str(key), "-h", "git.example.test", "-t", "0"]) == 1
    assert "-t must be between 1 and" in capsys.readouterr().err
    assert "no identities" in _listed(_agent)


def test_a_forged_line_in_a_key_comment_does_not_list_a_key(keyed, tmp_path):
    """L2 #10: a key comment is the adder's text; one carrying a newline and an ssh-add -l shaped line
    must not make run/doctor show a fingerprint the agent does not hold."""
    agent, _key, _ = keyed
    forged = "SHA256:" + "A" * 43
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"x\n256 {forged} y (ED25519)",
                    "-f", str(tmp_path / "evil")], check=True)
    subprocess.run(["ssh-add", "-q", str(tmp_path / "evil")], env=dict(os.environ, SSH_AUTH_SOCK=agent.socket),
                   check=True, capture_output=True)
    held = ssh_agent._held(agent.socket)
    assert held == [ssh_agent.fingerprint(tmp_path / "evil")] and forged not in held


def test_two_sessions_need_a_session_id(keyed):
    agent, key, _ = keyed
    other = ssh_agent.start(entity="other")
    with pytest.raises(ssh_agent.AgentError, match="more than one levain session"):
        ssh_agent.add_key(key, ["git.example.test"], system="Linux")
    ssh_agent.add_key(key, ["git.example.test"], session=other.session_id, system="Linux")
    assert ssh_agent.fingerprint(key) in _listed(other)
    assert ssh_agent.fingerprint(key) not in _listed(agent)


# --- the run/doctor ssh lines (D2), from a live agent ----------------------------------------------

def test_the_run_lines_show_this_sessions_agent_and_each_key_with_its_limit(keyed):
    agent, key, _ = keyed
    lines = ssh_agent.status_lines("Linux", agent=agent)
    assert lines[0].startswith(f"ssh agent: levain's scoped agent for this session (pid {agent.pid}, ")
    # tier-true (L1 #2 / L2 #4): "only it" at T9 alone, where pathname unix sockets are refused
    assert "reachable at this tier (unknown" in lines[0] and "only it" not in lines[0]
    assert "reachable at this tier (T6" in ssh_agent.status_lines("Linux", agent=agent, tier="T6")[0]
    assert ssh_agent.status_lines("Linux", agent=agent, tier="T9")[0].endswith(
        "bash reaches only it, at its $SSH_AUTH_SOCK")
    assert lines[1].startswith("ssh agent: no keys; add one from your own shell: `levain ssh-add <key> -h <host>`")
    ssh_agent.add_key(key, ["git.example.test"], lifetime=7200, system="Linux")
    fp = ssh_agent.fingerprint(key)
    assert ssh_agent.status_lines("Linux", agent=agent)[1] in (
        f"ssh agent key {fp}: for git.example.test, 2h00m left",
        f"ssh agent key {fp}: for git.example.test, 1h59m left")


def test_a_key_added_around_levain_and_a_waived_one_are_named_as_such(keyed, tmp_path):
    agent, key, _ = keyed
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "raw")], check=True)
    env = dict(os.environ, SSH_AUTH_SOCK=agent.socket)
    subprocess.run(["ssh-add", "-q", str(tmp_path / "raw")], env=env, check=True, capture_output=True)
    ssh_agent.add_key(key, [], waive_destinations=True, lifetime=600, system="Linux")
    lines = ssh_agent.status_lines("Linux", agent=agent)[1:]
    raw_fp, fp = ssh_agent.fingerprint(tmp_path / "raw"), ssh_agent.fingerprint(key)
    assert f"ssh agent key {raw_fp}: added from inside the session, no destination limit known" in lines
    assert any(ln.startswith(f"waiver (levain ssh-add --no-destination-limit) ssh agent key {fp}: ANY host")
               for ln in lines)


def test_the_doctor_lines_list_every_running_session_and_the_other_cases(keyed, monkeypatch):
    agent, _, _ = keyed
    lines = ssh_agent.status_lines("Linux")
    assert lines[0] == f"ssh agent: session {agent.session_id} (ent), pid {agent.pid}"
    assert lines[1].startswith("ssh agent: no keys")
    ssh_agent.stop(agent)
    assert ssh_agent.status_lines("Linux")[0].endswith("(none running now)")


@needs_openssh
def test_a_raw_mode_session_says_no_key_can_be_added_instead_of_advising_ssh_add(home):
    """L3 b2a r1 (codex LOW): add_key refuses ssh_mode=raw, so the line must not tell you to run it."""
    agent = ssh_agent.start(entity="ent", ssh_mode="raw")
    try:
        line = ssh_agent.status_lines("Linux", agent=agent)[1]
        assert line.startswith("ssh agent: no keys, and none can be added: the entity runs ssh_mode=raw")
        assert "levain ssh-add" not in line
    finally:
        ssh_agent.stop(agent)


def test_an_identities_count_larger_than_the_answer_is_refused_before_looping(monkeypatch):
    """L3 b2a r1 (glm LOW): a `count` the answer is too short to hold is refused before the loop. The loop
    already returned None at its first read past the end, so this pins the result, not the bound itself."""
    import struct
    import threading

    d = Path(tempfile.mkdtemp(prefix="lvq", dir="/tmp"))
    sock = str(d / "a.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock)
    srv.listen(1)
    body = bytes([ssh_agent._IDENTITIES_ANSWER]) + struct.pack(">I", 2 ** 32 - 1) + b"\0" * 8

    def answer():
        c, _ = srv.accept()
        c.recv(5)
        c.sendall(struct.pack(">I", len(body)) + body)
        c.close()

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    try:
        assert ssh_agent._held(sock) is None
    finally:
        t.join(5)
        srv.close()
        shutil.rmtree(d, ignore_errors=True)
    assert ssh_agent.status_lines("Linux", note="why")[0].startswith("ssh agent: none for this session (why)")
    assert ssh_agent.status_lines("Linux", hands=True)[0].startswith("ssh agent: none (the entity's own user)")
    assert ssh_agent.status_lines("Darwin") == [ssh_agent.DARWIN_LINE]
    monkeypatch.setattr(ssh_agent, "openssh_problem", lambda: "OpenSSH 8.4 has no destination constraints")
    assert "OpenSSH 8.4" in ssh_agent.status_lines("Linux")[0]


def test_the_session_known_hosts_holds_the_checked_keys_of_each_keys_hosts_and_goes_with_the_agent(keyed):
    agent, key, manifest = keyed
    kh = agent.dir / "known_hosts"
    assert stat.S_IMODE(os.lstat(kh).st_mode) == 0o600 and kh.read_text() == ""
    ssh_agent.add_key(key, ["git.example.test"], lifetime=600, system="Linux")
    assert kh.read_text() == manifest.read_text()
    found = subprocess.run(["ssh-keygen", "-F", "git.example.test", "-f", str(kh)], capture_output=True)
    assert found.returncode == 0
    ssh_agent.stop(agent)
    assert not agent.dir.exists()


# --- B1 r1 B-H2: a pid is pinned before it is checked, and signalled only while it is that process ----

def _term_ignoring_child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, "
                             "signal.SIG_IGN); print('up', flush=True); time.sleep(30)"], stdout=subprocess.PIPE)


def test_a_pid_reused_after_the_term_gets_no_kill_off_linux(tmp_path, monkeypatch):
    """The start-time path (no pidfd): the agent answered on its socket, ignored SIGTERM, and its pid is
    another process by the time SIGKILL would go. Real child and real signals; the reuse is stood in
    for by the start time changing after the TERM (a pid cannot be made to recycle on demand)."""
    child = _term_ignoring_child()
    child.stdout.readline()   # the TERM handler is set
    sent: list[int] = []
    reused = []
    real_kill = os.kill

    def kill(pid, sig):
        if pid == child.pid and sig:
            sent.append(sig)
            if sig == ssh_agent.signal.SIGTERM:
                reused.append(1)
        real_kill(pid, sig)

    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(ssh_agent, "_STOP_WAIT", 0.3)
    monkeypatch.setattr(ssh_agent, "peer_pid", lambda sock: child.pid)
    monkeypatch.setattr(ssh_agent, "_start_mark", lambda pid: "B" if reused else "A", raising=False)
    try:
        agent = ssh_agent.ScopedAgent("0" * 12, str(tmp_path / "agent.sock"), child.pid)
        ssh_agent.stop(agent)
        assert sent == [ssh_agent.signal.SIGTERM], f"signals to a reused pid: {sent}"
    finally:
        child.kill()
        child.wait()


def test_on_linux_the_agent_is_pinned_by_a_pidfd_before_the_socket_check(tmp_path, monkeypatch):
    """The pidfd path, mocked (os.pidfd_open and signal.pidfd_send_signal are stand-ins here; on Linux CI
    too, so no real pidfd is signalled): the pidfd is opened before the peer check, both signals go
    through it, and no signal goes to the pid by number."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    order: list[str] = []
    via_pidfd: list[int] = []
    by_number: list[int] = []
    fds: list[int] = []
    real_kill = os.kill

    def pidfd_open(pid, flags=0):
        order.append("pidfd_open")
        fds.append(os.open(os.devnull, os.O_RDONLY))
        return fds[-1]

    def peer(sock):
        order.append("peer_pid")
        return child.pid

    monkeypatch.setattr(os, "pidfd_open", pidfd_open, raising=False)
    monkeypatch.setattr(ssh_agent.signal, "pidfd_send_signal", lambda fd, sig: via_pidfd.append(sig), raising=False)
    monkeypatch.setattr(ssh_agent, "_pidfd_exited", lambda fd, timeout: False, raising=False)
    monkeypatch.setattr(ssh_agent, "peer_pid", peer)
    monkeypatch.setattr(os, "kill", lambda pid, sig: by_number.append(sig) if sig else real_kill(pid, sig))
    monkeypatch.setattr(ssh_agent, "_STOP_WAIT", 0.2)
    try:
        ssh_agent.stop(ssh_agent.ScopedAgent("0" * 12, str(tmp_path / "agent.sock"), child.pid))
        assert by_number == [], f"signalled by pid number: {by_number}"
        assert order == ["pidfd_open", "peer_pid"]
        assert via_pidfd == [ssh_agent.signal.SIGTERM, ssh_agent.signal.SIGKILL]
        with pytest.raises(OSError):
            os.fstat(fds[0])   # the pidfd is closed
    finally:
        child.kill()
        child.wait()


def _orphan_dir(owner: int, owner_start: str | None) -> Path:
    root = ssh_agent.run_root()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    d = root / "abcdef012345"
    d.mkdir(mode=0o700)
    rec = {"session_id": d.name, "pid": 2 ** 22 + 7, "owner": owner, "entity": "", "socket": str(d / "agent.sock"),
           "lifetime": 60, "started_at": 0, "ssh_mode": "agent", "keys": []}
    if owner_start is not None:
        rec["owner_start"] = owner_start
    ssh_agent.write_record(d, rec)
    return d


def test_an_owner_pid_held_by_another_process_does_not_keep_its_agent(home):
    """complement LOW2: the owner is alive only while its pid holds the process that started (its start
    time is recorded). Real process and real start-time read (/proc on Linux, ps elsewhere)."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        d = _orphan_dir(other.pid, "not the start time of the process at that pid")
        assert ssh_agent.sweep_orphans() == [d.name]
        assert not d.exists()
        assert ssh_agent._start_mark(other.pid) is not None
        d = _orphan_dir(other.pid, ssh_agent._start_mark(other.pid))
        assert ssh_agent.sweep_orphans() == []   # the owner is that process: left alone
        assert d.exists()
    finally:
        other.kill()
        other.wait()


# --- B1 r1 B-M3: the record and known_hosts are written before ssh-add, and taken back if it fails ----

def test_a_known_hosts_that_cannot_be_written_adds_no_key(keyed):
    """codex MED: ssh-add ran first, so a failed known_hosts write left the key in the agent while the
    command reported a failure. Now the write comes first: it fails, nothing reaches the agent."""
    agent, key, _manifest = keyed
    os.chmod(agent.dir / "known_hosts", 0o400)
    with pytest.raises(ssh_agent.AgentError):
        ssh_agent.add_key(key, ["git.example.test"], lifetime=600, system="Linux")
    assert "no identities" in _listed(agent)
    assert ssh_agent.read_record(agent.dir)["keys"] == []


def test_a_failed_ssh_add_takes_back_its_record_entry_and_known_hosts_lines(keyed, monkeypatch):
    """complement LOW3: when ssh-add fails, the record entry and the known_hosts lines written for it
    go, and a key recorded by an earlier add keeps its entry."""
    agent, key, _manifest = keyed
    kh = agent.dir / "known_hosts"
    ssh_agent.add_key(key, [], waive_destinations=True, lifetime=600, system="Linux")
    [before] = ssh_agent.read_record(agent.dir)["keys"]
    real = subprocess.run

    def failing_ssh_add(argv, *a, **kw):
        if argv[0] == "ssh-add":
            return subprocess.CompletedProcess(argv, 1)
        return real(argv, *a, **kw)

    monkeypatch.setattr(ssh_agent.subprocess, "run", failing_ssh_add)
    with pytest.raises(ssh_agent.AgentError, match="ssh-add exited with status 1"):
        ssh_agent.add_key(key, ["git.example.test"], lifetime=600, system="Linux")
    assert kh.read_text() == ""
    assert ssh_agent.read_record(agent.dir)["keys"] == [before]
