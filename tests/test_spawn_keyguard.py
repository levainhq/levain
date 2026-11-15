"""The spawn-level keyguard DENY (tests/spawn_keyguard.py). Nothing here starts ssh-keygen: the judge is called on argv, and
the wrapped-spawn cases put a recorder in place of the REAL ``Popen.__init__`` before the guard wraps it, so a refusal is
asserted to happen before anything would exec. The real home is a tmp dir (``_real_home`` is replaced)."""

from __future__ import annotations

import os
import subprocess

import pytest

from levain.cockpit.keys import RealKeyReached
from tests import spawn_keyguard as G


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "realhome"
    (h / ".ssh").mkdir(parents=True)
    (h / ".levain" / "keys").mkdir(parents=True)
    monkeypatch.setattr(G, "_real_home", lambda: str(h))
    return h


def test_the_real_key_places_are_refused_by_every_form(home, tmp_path):
    for argv in (["cat", str(home / ".ssh" / "id_ed25519")], ["ssh-keygen", "-Y", "sign", "-f", str(home / ".levain/keys/terminal")],
                 ["cat", "~/.ssh/id_x"], ["cat", "~/.ssh"], ["ls", f"-f{home}/.ssh/id_x"], ["git", "-c", f"user.signingkey={home}/.ssh/k"],
                 ["sh", "-c", "cat $HOME/.ssh/id_x"], ["sh", "-c", "cat ${HOME}/.ssh/id_x"], f"cat {home}/.ssh/id_x",
                 ["sh", "-c", f"echo hi; cat {home}/.ssh/id_x"], ["cat", str(home / ".levain" / "terminal")],
                 ["cat", str(home / ".levain" / "id_x")], ["cat", str(home / ".levain" / "k.pub")],
                 ["git", "-c", f"core.sshCommand=ssh -i {home}/.ssh/id_x", "fetch"], ["ssh-keygen", "-D", "/usr/lib/ssh-keychain.dylib"]):
        assert G.judge(argv, cwd=str(tmp_path)), argv
    assert G.judge(["cat", "id_x"], cwd=str(home / ".ssh"))                       # relative, resolved against the child's cwd
    assert G.judge(["cat", "x"], executable=str(home / ".ssh" / "x"))
    assert G.judge(["cat", str(home / ".levain" / "notes.md")]) is None           # not a key place


def test_a_symlink_from_tmp_to_the_real_ssh_is_refused(home, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(home / ".ssh")
    assert G.judge(["cat", str(link / "id_x")])
    assert G.judge(["cat", "link/id_x"], cwd=str(tmp_path))


def test_sc_auth_provider_flags_and_ssh_add_are_refused(home, tmp_path):
    for argv in (["/usr/sbin/sc_auth", "list-ctk-identities"], ["sudo", "-n", "SC_AUTH", "x"], "sc_auth list", ["sh", "-c", "sc_auth x"],
                 ["ssh-keygen", "-K"], ["ssh-keygen", "-qK"], ["ssh-keygen", "-w", "/x/p.dylib"], ["ssh-keygen", "-D/x/p.dylib"],
                 ["/usr/bin/ssh-keygen", "-t", "ed25519", "-D", "x"], ["ssh-add", "x"], ["ssh-add", "-l"], "echo a && ssh-add"):
        assert G.judge(argv, env={}), argv
    assert G.judge(["ssh-add", "k"], env={"SSH_AUTH_SOCK": "/Users/nobody/agent.sock"})


def test_unreadable_spawns_are_refused(home):
    assert G.judge("sh -c 'cat x", env={})
    assert G.judge(["sh", "-c", "echo 'x"], env={})
    assert G.judge(7, env={})


def test_scratch_keys_agents_and_fakes_pass(home, tmp_path):
    k = tmp_path / "k"
    sock = tmp_path / "agent.sock"
    assert G.judge(["ssh-keygen", "-Y", "sign", "-q", "-f", str(k), "-n", "ns"], env={}) is None
    assert G.judge(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(k), "-q"], env={}) is None
    assert G.judge(["ssh-keygen", "-t", "ed25519"], env={}) is None                # a bare -t: no home path named
    assert G.judge(["ssh-add", str(k)], env={"SSH_AUTH_SOCK": str(sock)}) is None
    assert G.judge("SSH_AUTH_SOCK=%s ssh-add %s" % (sock, k), env={}) is None
    assert G.judge([str(tmp_path / "bin" / "sc_auth"), "x"], env={}) is None
    assert G.judge([str(tmp_path / "bin" / "ssh-keygen"), "-K"], env={}) is None
    assert G.judge(["git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={k}", "commit", "-m", "x"], env={}) is None
    assert G.judge(["sh", "-c", "echo hi && ssh-keygen -l -f -"], env={}) is None


@pytest.fixture
def recorder(monkeypatch):
    seen: list = []
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, args, *a, **kw: seen.append(args))
    return seen


@pytest.fixture
def wall(recorder, home):
    """The guard wrapped over the recorder; the refusals it makes are expected."""
    before = len(G._VIOLATIONS)
    gen = G.install()
    next(gen)
    try:
        yield recorder
    finally:
        gen.close()
        G.acknowledge(before)


def test_a_refusal_comes_before_the_real_popen_and_names_test_reason_and_argv(wall, home):
    with pytest.raises(RealKeyReached) as e:
        subprocess.Popen(["cat", str(home / ".ssh" / "id_x")])
    assert wall == []
    text = str(e.value)
    assert "test_a_refusal_comes_before_the_real_popen" in text
    assert "argv names the real account's key place" in text and "{why}" not in text    # the actual reason, not a template
    assert "'cat'" in text
    with pytest.raises(RealKeyReached):
        subprocess.run(["/usr/sbin/sc_auth", "x"])
    subprocess.Popen(["ssh-keygen", "-t", "ed25519", "-f", "k"])
    assert len(wall) == 1


def test_popen_positionals_and_keywords_are_judged(wall, home, tmp_path):
    with pytest.raises(RealKeyReached):
        subprocess.Popen(["x", "-K"], -1, "/usr/bin/ssh-keygen")                  # executable by position
    with pytest.raises(RealKeyReached):
        subprocess.Popen(["cat", "id_x"], cwd=str(home / ".ssh"))
    with pytest.raises(RealKeyReached):                                           # cwd by position (10th)
        subprocess.Popen(["cat", "id_x"], -1, None, None, None, None, None, True, False, str(home / ".ssh"))
    with pytest.raises(RealKeyReached):
        subprocess.Popen("ssh-add k", shell=True, env={})
    subprocess.Popen(["cat", "id_x"], cwd=str(tmp_path))
    assert len(wall) == 1


def test_os_exec_spawn_posix_spawn_and_system_are_judged(wall, home, tmp_path, monkeypatch):
    calls: list = []
    real = {n: getattr(os, n) for n in ("execv", "posix_spawn", "spawnv", "spawnl", "system")}
    for n in real:
        monkeypatch.setattr(os, n, lambda *a, _n=n, **k: calls.append(_n))
    key = str(home / ".ssh" / "id_x")
    gen = G.install()                                                             # over the recorders just set
    next(gen)
    try:
        for call in (lambda: os.execv("/bin/cat", ["cat", key]), lambda: os.posix_spawn("/bin/cat", ["cat", key], {}),
                     lambda: os.spawnv(os.P_NOWAIT, "/bin/cat", ["cat", key]), lambda: os.spawnl(os.P_NOWAIT, "/bin/cat", "cat", key),
                     lambda: os.system(f"cat {key}"), lambda: os.posix_spawn("/usr/sbin/sc_auth", ["sc_auth"], env={})):
            with pytest.raises(RealKeyReached):
                call()
        assert calls == []
        os.execv("/bin/true", ["true"])
        os.system("true")
        assert calls == ["execv", "system"]
    finally:
        gen.close()


def test_the_session_wall_and_scrub_are_in_force_for_this_run():
    before = len(G._VIOLATIONS)
    with pytest.raises(RealKeyReached):
        subprocess.run(["ssh-keygen", "-K"])
    G.acknowledge(before)
    assert "SSH_AUTH_SOCK" not in os.environ and "SSH_SK_PROVIDER" not in os.environ
    assert os.environ["GIT_CONFIG_SYSTEM"] == "/dev/null" and os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    cfg = open(os.environ["GIT_CONFIG_GLOBAL"], encoding="utf-8").read()
    assert "Levain Test" in cfg and "signingkey" not in cfg and "gpgsign" not in cfg


def test_the_scrub_is_restored(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/x")
    prev = G.scrub_environ()
    assert "SSH_AUTH_SOCK" not in os.environ and prev["SSH_AUTH_SOCK"] == "/x"
    G.restore_environ(prev)
    assert os.environ["SSH_AUTH_SOCK"] == "/x"
