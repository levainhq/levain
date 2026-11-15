"""The spawn-level keyguard DENY (tests/spawn_keyguard.py). Nothing here starts ssh-keygen: the judge is called on argv, and
the wrapped-spawn cases put a recorder in place of the REAL ``Popen.__init__`` before the guard wraps it, so a refusal is
asserted to happen before anything would exec. The real home is a tmp dir (``_real_home`` is replaced) holding real files.
Every test runs in ``enforce`` mode unless it says ``warn``."""

from __future__ import annotations

import gc
import os
import subprocess
import sys

import pytest

from levain.cockpit.keys import RealKeyReached
from tests import spawn_keyguard as G


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setenv("LEVAIN_KEYGUARD", "enforce")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "realhome"
    (h / ".ssh").mkdir(parents=True)
    (h / ".levain" / "keys").mkdir(parents=True)
    for f in (".ssh/id_x", ".ssh/id_ed25519", ".ssh/k", ".ssh/x", ".ssh/my key", ".ssh/id_test", ".levain/keys/terminal"):
        (h / f).write_text("k")
    (h / ".levain" / "notes.md").write_text("n")
    monkeypatch.setattr(G, "_real_home", lambda: str(h))
    return h


def test_the_real_key_places_are_refused_by_every_form(home, tmp_path):
    for argv in (["cat", str(home / ".ssh" / "id_ed25519")], ["ssh-keygen", "-Y", "sign", "-f", str(home / ".levain/keys/terminal")],
                 ["cat", "~/.ssh/id_x"], ["ls", f"-f{home}/.ssh/id_x"], ["git", "-c", f"user.signingkey={home}/.ssh/k"],
                 f"cat {home}/.ssh/id_x",
                 ["sh", "-c", f"echo hi; cat {home}/.ssh/id_x"], ["ssh-keygen", "-D", "/usr/lib/ssh-keychain.dylib"]):
        assert G.judge(argv, cwd=str(tmp_path)), argv
    for script in ("cat $HOME/.ssh/id_x", "cat ${HOME}/.ssh/id_x"):                 # $HOME is the judged child's HOME
        assert G.judge(["sh", "-c", script], env={"HOME": str(home)}), script
    assert G.judge(["cat", "id_x"], cwd=str(home / ".ssh"))                       # relative, resolved against the child's cwd
    assert G.judge(["cat", "x"], executable=str(home / ".ssh" / "x"))
    assert G.judge(["cat", str(home / ".levain" / "notes.md")]) is None           # not a key place
    assert G.judge(["cat", str(home / ".ssh" / "no_such_key")]) is None           # a key handle is an EXISTING file
    assert G.judge(["cat", str(home / ".ssh")]) is None                           # the directory is not a handle


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


def test_unparsable_shell_text_fails_open_and_structured_argv_is_never_parsed(home):
    assert G.judge("sh -c 'cat x", env={}) is None
    assert G.judge(["sh", "-c", "echo 'x"], env={}) is None                       # sh -c 'unbalanced: fail open
    assert G.judge(["python", "-c", "print('x"], env={}) is None
    assert G.judge(["systemd-run", "--", "echo 'x", "a\0b"], env={}) is None
    assert G.judge(["cat", "a\0b"], env={}) is None                               # an embedded NUL is skipped
    assert G.judge(7, env={})                                                     # a spawn that cannot be read at all: refused


def test_a_provider_library_is_data_not_a_key(home):
    assert G.judge(["ls", "/usr/lib/ssh-keychain.dylib"], env={}) is None
    assert G.judge(["cat", "/usr/libexec/ssh-sk-helper"], env={}) is None
    assert G.judge(["ssh-keygen", "-D", "/usr/lib/ssh-keychain.dylib"], env={})   # as the value of -D it is still refused


def test_a_token_equal_to_an_existing_fake_home_key_is_denied(home, tmp_path):
    assert G.judge(["cat", str(home / ".ssh" / "id_x")], env={})
    assert G.judge(["cat", str(home / ".levain" / "keys" / "terminal")], env={})
    assert G.judge(["cat", str(home / ".ssh" / "id_missing")], env={}) is None


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
    assert "argv names a real key file of the account" in text and "{why}" not in text    # the actual reason, not a template
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


_ENV = {"A": "b"}


def _positional(n, argv, env):
    """The positional arguments of ``os.<n>`` for ``argv`` (and ``env`` where the function takes one)."""
    if n == "system":
        return (" ".join(argv),)
    if n.startswith("spawnl"):
        return (os.P_NOWAIT, argv[0], *argv, *((env,) if n.endswith("e") else ()))
    head = (os.P_NOWAIT,) if n.startswith("spawn") else ()
    return (*head, argv[0], argv, *((env,) if (n.endswith("e") or n.startswith("posix")) else ()))


def test_every_wrapped_os_function_is_judged_positionally_and_by_keyword(home, tmp_path, monkeypatch):
    names = ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe", "system", "spawnl", "spawnle", "spawnlp",
             "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe")
    names = [n for n in names if hasattr(os, n)]
    sigs = {n: G.real_signature(getattr(os, n), n) for n in names}                # the REAL signatures, through the wrapper
    calls: list = []
    for n in names:
        def rec(*a, _n=n, **k):
            calls.append(_n)
        rec.__signature__ = sigs[n]
        monkeypatch.setattr(os, n, rec)
    key = str(home / ".ssh" / "id_x")
    before = len(G._VIOLATIONS)
    gen = G.install()                                                             # over the recorders just set
    next(gen)
    try:
        for n in names:
            for argv, refused in ((["cat", str(tmp_path / "fine")], False), (["cat", key], True)):
                pos = _positional(n, argv, _ENV)
                forms = [dict(args=pos)]
                params = list(sigs[n].parameters.values())
                if not any(p.kind in (p.POSITIONAL_ONLY, p.VAR_POSITIONAL) for p in params):
                    forms.append(dict(kwargs=dict(zip((p.name for p in params), pos))))
                for form in forms:
                    seen = len(calls)
                    call = (lambda: getattr(os, n)(*form["args"])) if "args" in form else (lambda: getattr(os, n)(**form["kwargs"]))
                    if refused:
                        with pytest.raises(RealKeyReached):
                            call()
                        assert len(calls) == seen, (n, form)
                    else:
                        call()
                        assert calls[-1] == n and len(calls) == seen + 1, (n, form)
        with pytest.raises(RealKeyReached):
            os.posix_spawn("/usr/sbin/sc_auth", ["sc_auth"], {})
    finally:
        gen.close()
        G.acknowledge(before)


def test_a_refused_popen_leaves_no_unraisable_del(wall, home):
    p = subprocess.Popen.__new__(subprocess.Popen)
    with pytest.raises(RealKeyReached):
        p.__init__(["cat", str(home / ".ssh" / "id_x")])
    assert p._child_created is False
    p.__del__()                                                                   # AttributeError before the attribute was set
    seen: list = []
    old, sys.unraisablehook = sys.unraisablehook, seen.append
    try:
        with pytest.raises(RealKeyReached):
            subprocess.Popen(["cat", str(home / ".ssh" / "id_x")])
        gc.collect()
    finally:
        sys.unraisablehook = old
    assert seen == []


def test_dollar_home_follows_the_judged_childs_home(home, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for script in ("cat $HOME/.ssh/id_test", "cat ${HOME}/.ssh/id_test"):
        assert G.judge(["sh", "-c", script], env={"HOME": str(scratch)}) is None
        assert G.judge(["sh", "-c", script], env={"HOME": str(home)})
    assert G.judge(["cat", "$HOME/.ssh/id_test"], env={"HOME": str(home)})
    assert G.judge(["sh", "-c", "cat $HOME/.ssh/id_test"], env={})                # unset HOME: a shell falls back to passwd


def test_structured_argv_is_data_except_for_paths_and_shell_code(home):
    assert G.judge(["printf", "%s", "documentation: ~/.ssh/id"], env={}) is None
    assert G.judge(["echo", "see sc_auth and ssh-add docs"], env={}) is None
    assert G.judge(["cat", f"{home}/.ssh/my key"], env={})                        # the token is itself a path
    assert G.judge(["sh", "-c", "cat ~/.ssh/id_x"], env={})
    assert G.judge(["eval", "cat ~/.ssh/id_x"], env={})
    assert G.judge(["git", "-c", f"gpg.ssh.program=sh -c 'cat {home}/.ssh/k'", "x"], env={}) is None   # structured: not parsed


def test_executable_keeps_argv0_and_an_argv_socket_assignment_needs_env_or_a_shell(home, tmp_path):
    sock = str(tmp_path / "s")
    assert G.judge([str(home / ".ssh" / "id_x"), "ok"], executable="/bin/cat", env={})
    assert G.judge(["ssh-add", "k"], executable="/bin/echo", env={})
    assert G.judge(["ssh-add", f"SSH_AUTH_SOCK={sock}"], env={"SSH_AUTH_SOCK": "/Users/nobody/a"})   # not an env assignment
    assert G.judge(["printf", f"SSH_AUTH_SOCK={sock}", "ssh-add"], env={})
    assert G.judge(["env", f"SSH_AUTH_SOCK={sock}", "ssh-add", "k"], env={}) is None
    assert G.judge(["sh", "-c", f"SSH_AUTH_SOCK={sock} ssh-add k"], env={}) is None
    assert G.judge(["ssh-add", "k"], env={"SSH_AUTH_SOCK": sock}) is None         # the child's own env decides


@pytest.mark.skipif(sys.platform != "darwin", reason="needs a case-insensitive volume")
def test_the_protected_prefixes_compare_case_and_normalisation_insensitively_on_darwin(home, monkeypatch):
    shouty = str(home).upper() + "/.SSH/id_x"
    monkeypatch.setattr(G, "_FOLD", False)
    assert G.judge(["cat", shouty], env={}) is None
    monkeypatch.setattr(G, "_FOLD", True)
    assert G.judge(["cat", shouty], env={})


def test_norm_folds_case_and_normalisation(monkeypatch):
    monkeypatch.setattr(G, "_FOLD", True)
    assert G._norm("e\u0301") == G._norm("\u00e9") == "\u00e9"


def test_cleanup_is_idempotent_and_reachable_from_unconfigure_and_atexit(monkeypatch):
    closed, restored = [], []
    monkeypatch.setattr(G, "restore_environ", restored.append)

    class Session:
        def close(self):
            closed.append(1)

    prev = {"SSH_AUTH_SOCK": None}
    done = G.make_cleanup(prev, Session())
    done(), done(), done()
    assert closed == [1] and restored == [prev]
    calls: list = []
    monkeypatch.setattr(G, "cleanup", lambda: calls.append(1))
    G.keyguard_cleanup()
    assert calls == [1]


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


def test_warn_mode_records_and_does_not_raise_and_enforce_raises(wall, home, monkeypatch):
    key = str(home / ".ssh" / "id_x")
    before = len(G._REPORT)
    monkeypatch.setenv("LEVAIN_KEYGUARD", "warn")
    subprocess.Popen(["cat", key])
    subprocess.Popen(["/usr/sbin/sc_auth", "x"])
    assert len(wall) == 2                                                         # both spawns went through
    new = G._REPORT[before:]
    del G._REPORT[before:]
    assert len(new) == 2 and "test_warn_mode_records" in new[0] and "id_x" in new[0] and "sc_auth" in new[1]
    assert G._pending() == []                                                     # no violation: the test is not failed
    monkeypatch.setenv("LEVAIN_KEYGUARD", "enforce")
    with pytest.raises(RealKeyReached):
        subprocess.Popen(["cat", key])
    assert len(wall) == 2 and len(G._REPORT) == before


def test_mode_defaults_to_the_module_constant(monkeypatch):
    monkeypatch.delenv("LEVAIN_KEYGUARD", raising=False)
    assert G.mode() == G._DEFAULT_MODE == "warn"
    monkeypatch.setenv("LEVAIN_KEYGUARD", "Enforce")
    assert G.mode() == "enforce"


def test_the_terminal_summary_prints_the_block_and_writes_the_file(tmp_path, monkeypatch):
    lines: list[str] = []

    class Rep:
        config = object()

        def write_line(self, t):
            lines.append(t)

    out = tmp_path / "r.txt"
    monkeypatch.setenv("LEVAIN_KEYGUARD_REPORT", str(out))
    monkeypatch.setattr(G, "_REPORT", ["one"])
    monkeypatch.setattr(G, "_NODES", ["two"])
    G.pytest_terminal_summary(Rep())
    assert lines == ["KEYGUARD REPORT BEGIN", "one\ntwo", "KEYGUARD REPORT END"] and out.read_text() == "one\ntwo\n"
    lines.clear()
    monkeypatch.setattr(G, "_REPORT", [])
    monkeypatch.setattr(G, "_NODES", [])
    G.pytest_terminal_summary(Rep())
    assert lines == [] and out.read_text() == ""                                  # an empty report prints nothing, file says empty
