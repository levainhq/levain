"""M2 S2 slice 2: outside the interactive REPL, an entity with a hands user runs its bash AS that user
(``sudo -n -u``, outside the sandbox driver), and its file editor reads and writes as that user too.

Hermetic: no hands user exists on a developer machine (setup needs root), so the spawn argv, the
fail-closed path, the signal path and the editor's write path are checked here with fakes; the real
spawn under real sudo runs in the CI end-to-end (tests/ci/isolation_e2e.sh)."""
from __future__ import annotations

import os
import platform
import pwd
import signal
import subprocess
from pathlib import Path

import pytest

from levain.firing import confinement
from levain.firing.confinement import (
    HANDS_PATH,
    BwrapProvider,
    ConfinementConfig,
    ConfinementError,
    HandsIdentity,
    SeatbeltProvider,
    build_policy,
    hands_for,
    hands_prefix,
)

_NOBODY = pwd.getpwnam("nobody")


def _entity(tmp_path: Path) -> Path:
    d = tmp_path / "coyote"
    (d / ".levain").mkdir(parents=True)
    return d


def _hands(tmp_path: Path) -> HandsIdentity:
    ws = tmp_path / "hands-ws"
    ws.mkdir(exist_ok=True)
    ws = ws.resolve()   # setup records a real path
    return HandsIdentity("nobody", _NOBODY.pw_uid, _NOBODY.pw_dir, ws)


def _cfg(**kw) -> ConfinementConfig:
    return ConfinementConfig(**kw)


# --- who bash runs as -------------------------------------------------------------------------


def test_hands_for_runs_as_the_hands_user_only_outside_the_repl_on_macos(tmp_path):
    ws = tmp_path / "ws"
    cfg = _cfg(hands_user="nobody", hands_uid=_NOBODY.pw_uid, hands_workspace=ws)
    got = hands_for(cfg, "headless", system="Darwin")
    assert got == HandsIdentity("nobody", _NOBODY.pw_uid, _NOBODY.pw_dir, ws)
    assert hands_for(cfg, "unattended", system="Darwin") == got
    assert hands_for(cfg, "interactive", system="Darwin") is None   # D4: a human reads each turn
    assert hands_for(cfg, "headless", system="Linux") is None       # not built on Linux yet
    assert hands_for(_cfg(), "headless", system="Darwin") is None   # no setup


def test_hands_for_refuses_when_the_hands_account_is_gone(tmp_path):
    cfg = _cfg(hands_user="_levain_gone_000000", hands_uid=480, hands_workspace=tmp_path)
    with pytest.raises(ConfinementError, match="does not exist"):
        hands_for(cfg, "headless", system="Darwin")


def test_hands_for_refuses_an_account_whose_uid_changed(tmp_path):
    """S2 L1b LOW-3: the account was checked by name only; one deleted and re-created under the same
    name is another account, and the workspace was set up for the recorded uid."""
    cfg = _cfg(hands_user="nobody", hands_uid=_NOBODY.pw_uid + 1, hands_workspace=tmp_path)
    with pytest.raises(ConfinementError, match="uid"):
        hands_for(cfg, "headless", system="Darwin")


def test_a_hands_capable_provider_raising_typeerror_is_not_misreported(tmp_path):
    """S2 L1b LOW-5: any TypeError from inside a hands spawn was reported as "cannot run bash as the
    entity's own user". The provider's signature decides that; its own errors propagate as they are."""

    class Buggy(confinement.ConfinementProvider):
        def available(self):
            return True

        def render_profile(self, policy):
            return ""

        def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0, hands=None):
            raise TypeError("a bug inside the provider")

    with pytest.raises(TypeError, match="a bug inside"):
        Buggy().spawn_shell(build_policy(_entity(tmp_path)), hands=_hands(tmp_path))


# --- the spawn ----------------------------------------------------------------------------------


def test_the_hands_argv_puts_sudo_outside_the_sandbox_with_a_clean_environment(tmp_path, monkeypatch):
    """sudo first (the profile refuses to exec a setuid binary), ``-n`` (never prompt), ``env -i`` with
    the hands home and the fixed PATH, and nothing of the operator's environment."""
    monkeypatch.setenv("OPERATOR_SECRET_TOKEN", "do-not-leak")
    hands = _hands(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(confinement, "sandbox_exec_available", lambda: True)
    monkeypatch.setattr(confinement, "_require_hands_sudo", lambda h: None)
    monkeypatch.setattr(confinement._HandsSeatbeltShell, "start",
                        lambda self: seen.setdefault("shell", self))
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    SeatbeltProvider()._spawn_shell_impl(policy, hands=hands)
    sh = seen["shell"]
    argv = sh._argv
    assert argv[:6] == [confinement.SUDO, "-n", "-u", "nobody", "/usr/bin/env", "-i"]
    at = argv.index(confinement.SANDBOX_EXEC)
    assert argv[at + 1] == "-p" and argv[at + 3:] == ["/bin/bash", "--noprofile", "--norc"]
    env_args = argv[6:at]
    assert f"HOME={_NOBODY.pw_dir}" in env_args and f"PATH={HANDS_PATH}" in env_args
    assert not any("do-not-leak" in a for a in argv)
    assert not any(str(Path.home()) in d for d in HANDS_PATH.split(":"))
    assert sh._cwd == hands.workspace and sh._carried_env["HOME"] == _NOBODY.pw_dir
    assert "OPERATOR_SECRET_TOKEN" not in sh._env


def test_a_refused_sudo_fails_closed(tmp_path, monkeypatch):
    """The sudoers rule gone or the account retired: the spawn refuses with the redo hint, and never
    falls back to a shell running as the operator."""
    hands = _hands(tmp_path)
    monkeypatch.setattr(confinement, "sandbox_exec_available", lambda: True)
    calls: list = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"sudo: a password is required")

    monkeypatch.setattr(confinement.subprocess, "run", fake_run)
    monkeypatch.setattr(confinement.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("a shell was spawned after sudo refused"))
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    with pytest.raises(ConfinementError, match="fail-closed") as exc:
        SeatbeltProvider()._spawn_shell_impl(policy, hands=hands)
    assert "setup-isolation" in str(exc.value) and "password is required" in str(exc.value)
    assert calls == [[confinement.SUDO, "-n", "-u", "nobody", "/usr/bin/true"]]


def test_signals_to_a_hands_shell_are_sent_as_the_hands_user(tmp_path, monkeypatch):
    """levain may not signal another uid's processes: the timeout, close and interrupt signals go
    through `sudo -n -u <hands> /bin/kill -- -<pgid>`. sudo, the group leader, has the operator's real
    uid, so levain signals it itself; and both get SIGCONT, so a command that stopped itself (and sudo
    with it) cannot outlive the kill (S2 L2b M2)."""
    hands = _hands(tmp_path)
    sent: list = []
    direct: list = []
    monkeypatch.setattr(confinement.subprocess, "run",
                        lambda argv, **kw: sent.append(argv) or subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(confinement.os, "killpg", lambda pgid, sig: direct.append((pgid, sig)))
    sh = confinement._HandsSeatbeltShell(hands=hands, argv=["/bin/true"], cwd=tmp_path, env={})
    sh._signal(4242, signal.SIGTERM)
    kill = [confinement.SUDO, "-n", "-u", "nobody", "/bin/kill"]
    assert sent == [[*kill, f"-{int(signal.SIGTERM)}", "--", "-4242"],
                    [*kill, f"-{int(signal.SIGCONT)}", "--", "-4242"]]
    assert direct == [(4242, signal.SIGTERM), (4242, signal.SIGCONT)]


def test_a_timeout_whose_kill_did_not_land_is_not_reported_as_killed(tmp_path, monkeypatch):
    """S2 L1b MED-2 / L2b L4: sudo refusing or stalling while signalling was swallowed, and levain
    reported a killed command that kept running as the hands user. Now the shell is closed and the
    run refuses, saying the group is still running."""
    from levain.firing.confinement import SandboxedShell

    ws = tmp_path / "ws"
    ws.mkdir()
    sh = SandboxedShell(argv=["/bin/bash", "--noprofile", "--norc"], cwd=ws,
                        env={"PATH": "/usr/bin:/bin"}).start()
    monkeypatch.setattr(sh, "_signal", lambda pgid, sig: None)   # every signal silently lost
    monkeypatch.setattr(confinement, "_KILL_GRACE", 0.1)
    real_gone = confinement._group_gone
    monkeypatch.setattr(confinement, "_group_gone", lambda pgid, timeout: real_gone(pgid, timeout=min(timeout, 0.2)))
    try:
        with pytest.raises(ConfinementError, match="could not stop it"):
            sh.run("sleep 30", timeout=0.5)
        assert sh.closed
    finally:
        monkeypatch.undo()
        sh.close()


def test_the_linux_provider_refuses_a_hands_spawn(tmp_path):
    with pytest.raises(ConfinementError, match="not built for Linux"):
        BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)), hands=_hands(tmp_path))


def test_a_provider_without_hands_support_refuses_rather_than_running_as_the_operator(tmp_path):
    class Old(confinement.ConfinementProvider):
        def available(self):
            return True

        def render_profile(self, policy):
            return ""

        def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0):
            pytest.fail("an operator-uid shell was spawned for a hands entity")

    with pytest.raises(ConfinementError, match="fail-closed"):
        Old().spawn_shell(build_policy(_entity(tmp_path)), hands=_hands(tmp_path))


def test_the_editor_write_goes_through_the_hands_file_helper(tmp_path, monkeypatch):
    """D3: ``hands_write`` is the helper's ``write`` (tests/test_hands_files.py checks the helper and
    its argv): a fixed program as the hands user under the floor, the content its stdin."""
    hands = _hands(tmp_path)
    seen: dict = {}

    def fake_run(argv, h, data, timeout):
        seen.update(argv=argv, input=data)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(confinement, "_run_hands_helper", fake_run)
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    target = str(hands.workspace / "a.txt")
    SeatbeltProvider().hands_write(policy, hands, target, b"data; $(rm -rf ~)")
    argv = seen["argv"]
    assert argv[:4] == [confinement.SUDO, "-n", "-u", "nobody"]
    assert argv[argv.index("zsh") + 1:argv.index("zsh") + 4] == ["write", str(hands.workspace), target]
    assert seen["input"] == b"data; $(rm -rf ~)"

    monkeypatch.setattr(confinement, "_run_hands_helper",
                        lambda argv, h, d, t: subprocess.CompletedProcess(argv, 3, b"", b"cannot write: Permission denied"))
    with pytest.raises(OSError, match="Permission denied"):
        SeatbeltProvider().hands_write(policy, hands, target, b"x")


def test_a_hands_shell_is_not_given_the_ssh_agent(tmp_path, monkeypatch):
    """M2 S3: the agent refuses another uid anyway; levain does not pass its socket, and the shell's
    ssh finds the entity's own key under the hands home."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/operator-agent.sock")
    hands = _hands(tmp_path)
    assert "SSH_AUTH_SOCK" not in confinement._hands_env(hands)
    assert not any("SSH_AUTH_SOCK" in a for a in hands_prefix(hands))
    assert confinement._hands_env(hands)["HOME"] == _NOBODY.pw_dir


# --- the binding --------------------------------------------------------------------------------


def test_the_binding_carries_the_hands_user_and_fences_its_workspace(tmp_path):
    from levain.firing.binding import BindingError, ConversationBinding

    ent = _entity(tmp_path)
    hands = _hands(tmp_path)
    b = ConversationBinding.create(ent, mode="headless", workspace=hands.workspace,
                                   config=ConfinementConfig(), hands=hands)
    assert b.hands == hands and b.floor.workspace == hands.workspace.resolve()
    again = ConversationBinding.from_params(b.to_params())
    assert again.hands == hands
    plain = ConversationBinding.create(ent, mode="headless", workspace=ent / "workspace",
                                       config=ConfinementConfig())
    assert plain.hands is None and "hands" not in plain.to_params()
    with pytest.raises(BindingError):
        ConversationBinding.create(ent, mode="headless", workspace=ent / "workspace",
                                   config=ConfinementConfig(), hands=hands)
    forged = b.to_params()
    forged["hands"]["workspace"] = str(tmp_path / "elsewhere")
    with pytest.raises(BindingError):
        ConversationBinding.from_params(forged)


# --- the editor writes as the hands user --------------------------------------------------------


openhands = pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")


def test_the_editor_writes_through_the_hands_user_and_never_as_the_operator(tmp_path, monkeypatch):
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    ent = _entity(tmp_path)
    hands = _hands(tmp_path)
    writes: list = []

    class FakeProvider:
        def hands_file(self, policy, h, op, path, data=b"", *, timeout=60.0):
            assert h == hands
            if op == "write":
                writes.append((path, data))
                Path(path).write_bytes(data)   # stands in for the hands user's write
                return b""
            from tests.test_hands_files import _helper   # the real helper, as this user
            r = _helper(hands.workspace, op, path)
            if r.returncode == 2:
                raise FileNotFoundError(path)
            assert r.returncode == 0, r
            return r.stdout

    monkeypatch.setattr(T, "select_provider", lambda: FakeProvider())
    floor = T._SharedFloor(build_policy(ent, workspace=hands.workspace), hands)
    ed = T.CrownJewelsFileEditorExecutor(floor=floor)
    target = hands.workspace / "notes.txt"
    real_open = T.builtins.open

    def no_operator_write(file, mode="r", *a, **k):
        if any(c in mode for c in "wax+") and str(file) == str(target):
            pytest.fail(f"the operator wrote {file}")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(T.builtins, "open", no_operator_write)
    obs = ed(FileEditorAction(command="create", path=str(target), file_text="one\ntwo\n"))
    assert not obs.is_error, obs
    obs = ed(FileEditorAction(command="str_replace", path=str(target), old_str="two", new_str="2"))
    assert not obs.is_error, obs
    obs = ed(FileEditorAction(command="insert", path=str(target), insert_line=1, new_str="ins"))
    assert not obs.is_error, obs
    assert [p for p, _ in writes] == [str(target)] * 3
    assert target.read_text() == "one\nins\n2\n"


def test_an_editor_write_the_hands_user_cannot_make_is_an_in_band_error(tmp_path, monkeypatch):
    from openhands.tools.file_editor.definition import FileEditorAction

    from levain.firing.openhands import tools as T

    ent = _entity(tmp_path)
    hands = _hands(tmp_path)

    class Refusing:
        def hands_file(self, policy, h, op, path, data=b"", *, timeout=60.0):
            if op != "write":
                from tests.test_hands_files import _helper
                r = _helper(hands.workspace, op, path)
                if r.returncode == 2:
                    raise FileNotFoundError(path)
                return r.stdout
            raise OSError(f"nobody could not write {path}: Permission denied")

    monkeypatch.setattr(T, "select_provider", lambda: Refusing())
    ed = T.CrownJewelsFileEditorExecutor(
        floor=T._SharedFloor(build_policy(ent, workspace=hands.workspace), hands))
    target = hands.workspace / "x.txt"
    obs = ed(FileEditorAction(command="create", path=str(target), file_text="x"))
    assert obs.is_error and "Permission denied" in str(obs)
    assert not target.exists()
    # `insert` goes through the editor's move, which the stock editor does not catch.
    other = hands.workspace / "y.txt"
    other.write_text("a\n")
    obs = ed(FileEditorAction(command="insert", path=str(other), insert_line=1, new_str="b"))
    assert obs.is_error and "Permission denied" in str(obs)
    assert other.read_text() == "a\n"


def test_a_hands_session_banner_names_the_user(capsys, tmp_path):
    from types import SimpleNamespace

    from levain.run import _print_banner

    binding = SimpleNamespace(episodic_path=tmp_path / "e", crystal_path=tmp_path / "c")
    _print_banner(tmp_path / "coyote", binding, model="m", with_tools=True, bash_ok=True,
                  hands_user="_levain_coyote_abc123", workspace=Path("/Users/Shared/levain/x/workspace"))
    out = capsys.readouterr().out
    assert "bash runs as the entity's own user _levain_coyote_abc123" in out
    assert "~_levain_coyote_abc123/.ssh/id_ed25519" in out and "agent-auth only" not in out
    assert "workspace: /Users/Shared/levain/x/workspace" in out
    _print_banner(tmp_path / "coyote", binding, model="m", with_tools=True, bash_ok=True)
    assert "entity's own user" not in capsys.readouterr().out


def test_a_headless_session_binds_the_hands_user_and_the_repl_does_not(tmp_path, monkeypatch):
    """The session asks `hands_for` once; a headless session's floor, workspace and both hands carry
    the hands user, and the REPL keeps the operator's account and `<entity>/workspace`."""
    import json

    from levain import session as session_mod
    from levain.session import EntitySession

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEVAIN_ENTITY_DIR", raising=False)
    ent = tmp_path / "e"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    hands = _hands(tmp_path)
    monkeypatch.setattr(session_mod, "hands_for",
                        lambda cfg, mode: None if mode == "interactive" else hands)
    # Never a real UID-wide kill from the suite (a CI runner's sudo needs no password).
    from levain.firing import confinement

    swept: list[str] = []
    monkeypatch.setattr(confinement, "sweep_hands_user", lambda user: swept.append(user))
    s = EntitySession.open(ent, model="m", base_url="http://127.0.0.1:9", with_tools=True, mode="headless")
    try:
        # S2d codex HIGH: anything of the hands user left by an earlier levain is stopped at the start.
        assert swept == ["nobody"]
        assert s.hands_user == "nobody" and s.workspace == hands.workspace
        tools = s.conversation.agent.tools_map
        assert tools["file_editor"].executor._floor.hands == hands
        assert tools["file_editor"].executor._policy.workspace == hands.workspace
    finally:
        s.close()
    assert swept == ["nobody", "nobody"] and s.left_running is None
    s = EntitySession.open(ent, model="m", base_url="http://127.0.0.1:9", with_tools=True, mode="interactive")
    try:
        assert s.hands_user is None and s.workspace == (ent / "workspace").resolve()
        assert s.conversation.agent.tools_map["file_editor"].executor._floor.hands is None
    finally:
        s.close()
    assert swept == ["nobody", "nobody"]   # the REPL runs nothing as the hands user


def test_a_hands_session_does_not_start_over_processes_it_cannot_stop(tmp_path, monkeypatch):
    """S2d codex HIGH: a levain killed before its session's end leaves a setsid process of the hands
    user running; the next session stops it first, and refuses to start if it cannot."""
    import json

    from levain import session as session_mod
    from levain.firing import confinement
    from levain.firing import ws_git
    from levain.session import EntitySession, SessionStartError

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEVAIN_ENTITY_DIR", raising=False)
    ent = tmp_path / "e"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    monkeypatch.setattr(session_mod, "hands_for", lambda cfg, mode: _hands(tmp_path))
    monkeypatch.setattr(confinement, "sweep_hands_user", lambda user: "processes of nobody are still running (pid 4242)")
    with pytest.raises(SessionStartError, match="could not be stopped"):
        EntitySession.open(ent, model="m", base_url="http://127.0.0.1:9", with_tools=True, mode="headless")
    # The failed start keeps its locks while the processes live.
    with pytest.raises(ws_git.WsGitError, match="one runs at a time"):
        ws_git.hold_hands_session(ent)


def test_the_session_end_sweep_kills_every_process_of_the_hands_user_and_verifies(monkeypatch):
    """(d): a command can leave its process group (`setsid`), so the end of a session stops every
    process of the hands user, as that user, and checks that none is left."""
    import subprocess as sp

    from levain.firing import confinement

    calls: list[list[str]] = []
    left = iter([0, 1])

    def fake_run(argv, **kw):
        calls.append(list(argv))
        rc = next(left) if argv[0].endswith("pgrep") else 0
        return sp.CompletedProcess(argv, rc, "4242\n" if rc == 0 else "", "")

    monkeypatch.setattr(confinement.subprocess, "run", fake_run)
    assert confinement.sweep_hands_user("_levain_x_000000") is None
    assert calls[0] == [confinement.SUDO, "-n", "-u", "_levain_x_000000", "/bin/kill", "-9", "--", "-1"]
    assert calls[1] == ["/usr/bin/pgrep", "-U", "_levain_x_000000"] and len(calls) == 4

    monkeypatch.setattr(confinement.subprocess, "run",
                        lambda argv, **kw: sp.CompletedProcess(argv, 0, "4242\n", ""))
    said = confinement.sweep_hands_user("_levain_x_000000", timeout=0.3)
    assert said is not None and "4242" in said and "still running" in said


def test_the_sweep_never_waits_longer_than_its_timeout_on_a_stalled_step(monkeypatch):
    """S2d codex MED: each sudo/pgrep step had its own 10 s timeout, so a 5 s sweep could take 20 s."""
    import subprocess as sp

    from levain.firing import confinement

    timeouts: list[float] = []

    def stalled(argv, **kw):
        timeouts.append(kw["timeout"])
        if argv[0].endswith("pgrep"):
            return sp.CompletedProcess(argv, 0, "4242\n", "")
        raise sp.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(confinement.subprocess, "run", stalled)
    confinement.sweep_hands_user("_levain_x_000000", timeout=1.0)
    assert timeouts and max(timeouts) <= 1.0
