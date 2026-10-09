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
    assert hands_for(cfg, "headless", system="Linux") == got        # S2-linux: bwrap runs as it
    assert hands_for(cfg, "interactive", system="Linux") is None
    assert hands_for(cfg, "headless", system="FreeBSD") is None      # no hands launch there
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

    if platform.system() == "Linux":
        pytest.skip("a base shell is refused on Linux (S2 L3 r5)")
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


# --- the Linux hands launch (S2-linux) ---------------------------------------------------------


def test_hands_for_carries_the_recorded_egress_ports(tmp_path):
    cfg = _cfg(hands_user="nobody", hands_uid=_NOBODY.pw_uid, hands_workspace=tmp_path,
               hands_egress_ports=(18080,))
    assert hands_for(cfg, "headless", system="Linux").egress_ports == (18080,)


def test_the_linux_hands_launch_refuses_naming_the_problem_before_any_hands_process(tmp_path, monkeypatch):
    """Ruling (A): probe at every hands launch. A refused probe stops the spawn before bwrap runs, and
    the refusal says what the probe found (the egress table deleted, RUN in a VM 2026-10-09)."""
    monkeypatch.setattr(confinement, "_require_hands_sudo", lambda hands: None)
    monkeypatch.setattr(confinement, "_hands_launch_problem",
                        lambda hands: "the entity's network boundary does not hold: the nftables rule is not loaded")
    monkeypatch.setattr(confinement, "_bwrap_plan", lambda *a, **k: pytest.fail("planned past a refused probe"))
    with pytest.raises(ConfinementError, match="the nftables rule is not loaded.*fail-closed"):
        BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)), hands=_hands(tmp_path))


def test_the_launch_probe_asks_every_check_and_passes_only_when_all_hold(tmp_path, monkeypatch):
    from levain.firing import hands as hands_mod

    asked: list[str] = []
    found = {"net": None, "egress": None, "ns": None}

    def check(name):
        def f(*a, **k):
            asked.append(name)
            return found[name]
        return f

    monkeypatch.setattr(hands_mod, "net_group_problem", check("net"))
    monkeypatch.setattr(hands_mod, "egress_boundary_problem", check("egress"))
    monkeypatch.setattr(confinement, "_hands_ns_problem", check("ns"))
    monkeypatch.setattr(hands_mod, "hands_net_group", lambda user: "levain_net")
    h = _hands(tmp_path)
    assert confinement._hands_launch_problem(h) is None and asked == ["net", "egress", "ns"]
    for name, said in (("net", "is a member of"), ("egress", "connected to a loopback port"), ("ns", "uid map")):
        found = {"net": None, "egress": None, "ns": None, name: said}
        assert said in confinement._hands_launch_problem(h)


def _plan_like_the_vm(home: str, entity: str) -> list[str]:
    """The shape of the floor's plan for an entity in the operator's home (RUN in a VM 2026-10-09)."""
    return [confinement.BWRAP, "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--die-with-parent",
            "--unshare-pid", "--unshare-cgroup", "--ro-bind", "/sys/fs/cgroup", "/sys/fs/cgroup",
            "--bind", "/home", "/home", "--tmpfs", home, "--ro-bind-try", f"{home}/.bashrc", f"{home}/.bashrc",
            "--bind", f"{home}/.ssh", f"{home}/.ssh", "--bind", entity, entity, "--bind", "/run", "/run",
            "--tmpfs", "/run/user/1000/systemd", "--ro-bind", "/dev/null", "/run/user/1000/bus",
            "--remount-ro", "/run/user/1000/systemd", "--ro-bind", "/dev/null", f"{home}/.netrc",
            "--remount-ro", home]


def _view_hands(monkeypatch) -> HandsIdentity:
    monkeypatch.setattr(confinement, "_hands_view_trees",
                        lambda h: (["/usr", "/etc", "/opt"], [str(h.workspace), h.home]))
    monkeypatch.setattr(confinement, "_HANDS_VIEW_LINKS", ())
    return HandsIdentity("hands", 988, "/var/lib/levain-hands/hands", Path("/var/lib/levain/hands/workspace"))


def test_the_hands_argv_is_an_allowlisted_view_keeping_only_the_floors_ops_inside_it(monkeypatch):
    """J1 RUN: the floor's argv as the hands user stops at its first jewel under the operator's home.
    Phill's ruling (A): the hands view never binds the host root; it mounts only the view's trees, and of
    the floor's plan keeps an op only when its target lies in a host tree of the view."""
    hands = _view_hands(monkeypatch)
    home, entity = "/home/admin", "/home/admin/ent"
    plan = [*_plan_like_the_vm(home, entity), "--ro-bind", "/dev/null", "/etc/levain-jewel",
            "--tmpfs", "/var/lib/levain/hands/workspace/.levain-mask"]
    out = confinement._hands_bwrap_argv(plan, hands)
    view = [confinement.BWRAP, *confinement._HANDS_VIEW_FLAGS,
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc", "--ro-bind", "/opt", "/opt",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run",
            "--bind", str(hands.workspace), str(hands.workspace), "--bind", hands.home, hands.home]
    assert out == [*view, "--ro-bind", "/dev/null", "/etc/levain-jewel",
                   "--tmpfs", "/var/lib/levain/hands/workspace/.levain-mask"]
    for flag in ("--unshare-user", "--disable-userns", "--unshare-ipc", "--unshare-net", "--unshare-pid",
                 "--unshare-cgroup"):
        assert flag in out


def test_on_linux_every_hands_process_starts_through_the_keyring_join(tmp_path):
    hands = _hands(tmp_path)
    linux = hands_prefix(hands, system="Linux")
    assert linux[-7:] == [confinement.HANDS_PYTHON, "-I", "-S", "-c", confinement._HANDS_START, "hands",
                          str(os.getpid())]
    assert confinement._HANDS_START not in hands_prefix(hands, system="Darwin")


_SESSION_KEYRING_ID = ("import ctypes, os; nr = {'x86_64': 250, 'aarch64': 219}[os.uname().machine]; "
                       "libc = ctypes.CDLL(None); libc.syscall.restype = ctypes.c_long; "
                       "print(libc.syscall(ctypes.c_long(nr), ctypes.c_long(0), ctypes.c_long(-3), ctypes.c_long(1)))")


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"),
                    reason="session keyrings are Linux's")
def test_the_keyring_join_leaves_the_callers_session_keyring_behind():
    """RUN 2026-10-09: without it, a hands bash read a key the operator added to @s (KEYCTL_GET_KEYRING_ID
    of @s, created if absent, differs once the join has run)."""
    import sys

    own = subprocess.run([sys.executable, "-I", "-S", "-c", _SESSION_KEYRING_ID], capture_output=True, text=True)
    join = confinement._START_KEYRING + "os.execv(sys.argv[1], sys.argv[1:])\n"
    joined = subprocess.run([sys.executable, "-I", "-S", "-c", join,
                             sys.executable, "-I", "-S", "-c", _SESSION_KEYRING_ID], capture_output=True, text=True)
    assert own.returncode == 0 and joined.returncode == 0, (own.stderr, joined.stderr)
    assert int(own.stdout) > 0 and int(joined.stdout) > 0
    assert own.stdout != joined.stdout


_LINUX_START = pytest.mark.skipif(platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"),
                                  reason="the start program is Linux's")


def _start(role: str, *command: str) -> subprocess.CompletedProcess[str]:
    import sys

    return subprocess.run([sys.executable, "-I", "-S", "-c", confinement._HANDS_START, role, str(os.getpid()),
                           *command], capture_output=True, text=True, timeout=30)


@_LINUX_START
def test_a_hands_start_as_levains_own_uid_refuses_before_the_command():
    """Its kill(-1) at levain's end would take every process of levain's user (L3 r2: the role was
    once inferred from /proc/<levain>'s owner, which a non-dumpable levain changes on some kernels)."""
    r = _start("hands", "/bin/echo", "ran")
    assert r.returncode == 126 and "ran" not in r.stdout
    assert "levain's user" in r.stderr


@_LINUX_START
def test_an_operator_start_outside_a_leaf_levain_made_refuses():
    r = _start("operator", "/bin/echo", "ran")
    assert r.returncode == 126 and "ran" not in r.stdout
    assert "not in a cgroup leaf levain" in r.stderr


@_LINUX_START
def test_an_unknown_start_role_refuses():
    r = _start("oper", "/bin/echo", "ran")
    assert r.returncode == 126 and "unknown start role" in r.stderr


def _user_scope_ok() -> bool:
    if platform.system() != "Linux":
        return False
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    try:
        return subprocess.run([confinement.SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect", "/bin/true"],
                              env=env, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def test_the_operator_floor_kills_its_leaf_when_levain_dies():
    """RUN 2026-10-09 (R5, operator floor): levain SIGKILLed mid-command left the namespace's pid 1 and a
    setsid child running. A stand-in levain makes a leaf the way levain names one, starts a command with
    a setsid child through the start program in it, and is SIGKILLed: the leaf must empty."""
    import sys
    import time

    if not _user_scope_ok():
        if os.environ.get("LEVAIN_REQUIRE_LINUX_LIVE") == "1":
            pytest.fail("LEVAIN_REQUIRE_LINUX_LIVE=1 but systemd-run --user --scope does not work here")
        pytest.skip("needs a systemd user manager (systemd-run --user --scope)")
    stand_in = (
        "import os, subprocess, sys, time\n"
        "from levain.firing import confinement as c\n"
        "unit = c._leaf_unit(os.urandom(6).hex(), 1)\n"
        "print(c._leaf_rel(os.getuid(), unit), flush=True)\n"
        "subprocess.Popen(['/usr/bin/env', 'XDG_RUNTIME_DIR=/run/user/%d' % os.getuid(), c.SYSTEMD_RUN, '--user',"
        " '--scope', '--quiet', '--collect', '--slice=' + c._LEVAIN_SLICE, '--unit=' + unit, '--',"
        " *c._start_argv(sys.executable, hands=False), '/bin/sh', '-c', 'setsid sleep 120 & exec sleep 120'])\n"
        "time.sleep(120)\n")
    levain = subprocess.Popen([sys.executable, "-c", stand_in], stdout=subprocess.PIPE, text=True)
    leaf = Path("/sys/fs/cgroup") / levain.stdout.readline().strip()   # type: ignore[union-attr]
    procs = leaf / "cgroup.procs"

    def members() -> list[str]:
        try:
            return procs.read_text().split()
        except FileNotFoundError:
            return []

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(members()) < 3:
            time.sleep(0.1)
        assert len(members()) >= 3, "the command and its setsid child never ran in the leaf"
        levain.kill()
        levain.wait()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and members():
            time.sleep(0.1)
        assert not members()
    finally:
        levain.kill()
        levain.wait()
        if members():   # what the test found must not outlive it (codex L3 r3)
            (leaf / "cgroup.kill").write_text("1")


def test_the_hands_argv_refuses_an_op_it_does_not_know(monkeypatch):
    with pytest.raises(ConfinementError, match="does not know how to place"):
        confinement._hands_bwrap_argv([confinement.BWRAP, "--overlay-src", "/x"], _view_hands(monkeypatch))


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
# The editor's reads in these tests run the real hands helper, a zsh program: macOS ships zsh, and
# the hands editor is macOS-only until the S2-linux slice (a Linux CI runner has no /bin/zsh).
_needs_zsh = pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the hands helper needs /bin/zsh")


@_needs_zsh
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


@_needs_zsh
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
    monkeypatch.setattr(confinement, "sweep_hands_user", lambda user, **kw: swept.append(user))
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
    monkeypatch.setattr(confinement, "sweep_hands_user", lambda user, **kw: "processes of nobody are still running (pid 4242)")
    with pytest.raises(SessionStartError, match="could not be stopped"):
        EntitySession.open(ent, model="m", base_url="http://127.0.0.1:9", with_tools=True, mode="headless")
    # The failed start keeps its locks while the processes live.
    with pytest.raises(ws_git.WsGitError, match="one runs at a time"):
        ws_git.hold_hands_session(ent)


def test_the_session_end_sweep_kills_every_process_of_the_hands_user_and_verifies(monkeypatch):
    """(d): a command can leave its process group (`setsid`), so the end of a session stops every
    process of the hands user, as that user, and checks that none of the entity's is left."""
    import subprocess as sp

    from levain.firing import confinement, ws_git

    calls: list[list[str]] = []
    monkeypatch.setattr(confinement.subprocess, "run",
                        lambda argv, **kw: calls.append(list(argv)) or sp.CompletedProcess(argv, 0, "", ""))
    live = iter([True, False])
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: next(live))
    assert confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017) is None
    kill = [confinement.SUDO, "-n", "-u", "_levain_x_000000", "/bin/kill", "-9", "--", "-1"]
    assert calls == [kill, kill]

    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: True)
    said = confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.3)
    assert said is not None and "still running" in said


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
    import time

    from levain.firing import ws_git

    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: True)
    t0 = time.monotonic()
    confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.1)   # r3: no 0.5 s floor per step
    assert timeouts and max(timeouts) <= 0.1 and time.monotonic() - t0 < 0.5


def test_the_sweeps_liveness_check_is_bounded_by_the_sweeps_timeout(monkeypatch):
    """r4 codex MED + complement MED: the check (`entity_session_live`) ran pgrep and ps with no
    timeout and stdin inherited, so a stalled one held a session's start or end, and its locks, with
    no bound. Each now gets what is left of the sweep's deadline, and one that stalls is "cannot tell"."""
    import subprocess as sp

    from levain.firing import confinement, ws_git

    monkeypatch.setattr(ws_git.platform, "system", lambda: "Darwin")
    checks: list[dict] = []

    def run(argv, **kw):
        if argv[0] == confinement.SUDO:
            return sp.CompletedProcess(argv, 0, "", "")
        checks.append(kw)
        if "timeout" not in kw:
            return sp.CompletedProcess(argv, 0, "4242\n", "")   # would have hung; the test cannot
        raise sp.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(confinement.subprocess, "run", run)
    said = confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.2)
    assert checks and all(0 <= kw.get("timeout", 99) <= 0.2 for kw in checks)
    assert all(kw.get("stdin") is sp.DEVNULL for kw in checks)
    assert said is not None and "cannot tell" in said


def test_the_sweep_takes_the_known_uid_and_says_when_it_ran_out_of_time(monkeypatch):
    """r5 codex MED: the sweep looked the hands user up by name, which can block in NSS with no bound;
    it takes the uid setup recorded. complement LOW 5: a sweep whose time ran out before it could
    check said "cannot tell"; it says it ran out of time."""
    import pwd as _pwd
    import subprocess as sp
    import time

    from levain.firing import confinement, ws_git

    def no_lookup(name):
        raise AssertionError("the sweep looked the user up by name")

    monkeypatch.setattr(_pwd, "getpwnam", no_lookup)
    checked: list[int] = []
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: checked.append(uid) or False)

    def stalled(argv, **kw):
        time.sleep(kw["timeout"])
        raise sp.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(confinement.subprocess, "run", stalled)
    said = confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.2)
    assert said is not None and "ran out" in said and checked == []
    monkeypatch.setattr(confinement.subprocess, "run", lambda argv, **kw: sp.CompletedProcess(argv, 0, "", ""))
    assert confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017) is None and checked == [4_000_017]


@pytest.mark.skipif(platform.system() != "Darwin", reason="the hands shell is macOS only")
def test_a_hands_signal_that_found_the_group_gone_is_not_unconfirmed(tmp_path, monkeypatch):
    """r6 MED (codex + complement): the hands user's kill exits non-zero when it finds nothing left,
    as it does when sudo refuses or stalls, so a group that had just emptied read as an unconfirmed
    SIGKILL and a stopped command as unstoppable. A group with no live member is confirmed gone."""
    import time

    monkeypatch.setattr(confinement, "_hands_signal", lambda hands, pgid, sig: False)
    sh = confinement._HandsSeatbeltShell(hands=_hands(tmp_path), argv=["/bin/true"], cwd=tmp_path, env={})
    done = confinement._Leader(subprocess.Popen(["/usr/bin/true"], start_new_session=True))
    live = subprocess.Popen(["/bin/sleep", "5"], start_new_session=True)
    try:
        assert done.wait(5)
        time.sleep(0.1)
        assert sh._signal(done.pid, signal.SIGKILL) is True    # only its leader, a zombie: gone
        assert sh._signal(live.pid, signal.SIGCONT) is False   # still there, the kill unconfirmed
    finally:
        done.reap()
        live.kill()
        live.wait()


def test_the_sweep_refuses_levains_own_account_and_root(monkeypatch):
    """r3 complement LOW: a config naming the operator's account (or root) would have the sweep kill
    every process of it, levain included. Refused before anything runs."""
    import getpass
    import subprocess as sp

    from levain.firing import confinement

    ran: list[list[str]] = []
    monkeypatch.setattr(confinement.subprocess, "run", lambda argv, **kw: ran.append(argv) or sp.CompletedProcess(argv, 1, "", ""))
    for user, uid in ((getpass.getuser(), os.getuid()), ("root", 0)):
        said = confinement.sweep_hands_user(user, uid=uid)
        assert said is not None and "refusing" in said
    assert ran == []


def test_the_sweep_leaves_macos_per_user_agents_and_counts_only_the_entitys(monkeypatch):
    """r3 codex HIGH: launchd starts per-user agents (cfprefsd, XPC services) for any uid that ran
    Apple code and restarts them, so raw `pgrep -U` never emptied. The check is the one ws-git
    already uses (`entity_session_live`), which tells those agents from the entity's processes."""
    import subprocess as sp

    from levain.firing import confinement, ws_git

    monkeypatch.setattr(confinement.subprocess, "run", lambda argv, **kw: sp.CompletedProcess(argv, 0, "4242\n", ""))
    checked: list[int] = []
    monkeypatch.setattr(ws_git, "entity_session_live", lambda uid, **kw: checked.append(uid) or False)
    assert confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017) is None and checked == [4_000_017]

    def cannot_tell(uid, **kw):
        raise ws_git.WsGitError("cannot tell whether the entity is running (its processes changed while being read)")
    monkeypatch.setattr(ws_git, "entity_session_live", cannot_tell)
    said = confinement.sweep_hands_user("_levain_x_000000", uid=4_000_017, timeout=0.2)
    assert said is not None and "cannot tell" in said


# --- the Linux hands launch: listener sweep, editor, proxy relays ---------------------------------


def test_the_walk_asks_as_the_hands_user_inside_its_view_and_refuses_what_it_may_write(monkeypatch):
    """J5 RUN: a 0777 listener in /var/lib/s2probe was reached from inside the default-allow hands launch.
    Under the view /var/lib is absent; what is left to ask is the view's read-only host trees, as the
    hands user, inside the view. find's exit 1 for a directory it cannot list passes; any other failure
    refuses."""
    hands = _view_hands(monkeypatch)
    out: dict[str, object] = {"stdout": b"/etc/s2probe/sock\0", "stderr": b"", "rc": 0}
    seen: list[list[str]] = []

    def as_hands(argv, h, data, timeout, what="", **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, out["rc"], out["stdout"], out["stderr"])

    monkeypatch.setattr(confinement, "_run_hands_helper", as_hands)
    said = confinement._hands_listener_problem(hands)
    argv = seen[-1]
    assert argv[:4] == [confinement.SUDO, "-n", "-u", "hands"]
    b = argv.index(confinement.BWRAP)
    f = argv.index(confinement._HANDS_FIND)
    assert argv[b:f] == [*confinement._hands_bwrap_argv([confinement.BWRAP], hands), "/usr/bin/env", "LC_ALL=C"]
    assert argv[f + 1:f + 4] == ["/usr", "/etc", "/opt"] and argv[-1] == "-print0"
    # RUN 2026-10-09: without one pair around the whole OR, -print0 printed only its last branch.
    assert argv[f + 4] == "(" and argv[-2] == ")"
    depth = 0
    for i, tok in enumerate(argv[f + 4:-1]):
        depth += {"(": 1, ")": -1}.get(tok, 0)
        assert depth > 0 or i == len(argv[f + 4:-1]) - 1
    # L2 r2 M1: a directory it may search but not list hides a reachable socket; a readable FIFO leaks.
    assert ["-type", "d", "-executable", "!", "-readable"] == argv[argv.index("d") - 1:argv.index("d") + 4]
    assert "-readable" in argv[argv.index("p"):argv.index("d")]
    assert said is not None and "/etc/s2probe/sock" in said and "tighten the mode" in said
    out.update(stdout=b"", rc=1, stderr=b"/usr/bin/find: '/etc/ssl/private': Permission denied\n")
    assert confinement._hands_listener_problem(hands) is None
    out.update(stderr=b"bwrap: Can't mount proc on /newroot/proc: Operation not permitted\n")
    said = confinement._hands_listener_problem(hands)
    assert said is not None and "could not walk" in said and "Can't mount proc" in said


def test_a_hands_shell_walks_again_before_every_command(monkeypatch):
    """RUN 2026-10-09 (R6-mid): a 0777 listener planted under /opt after the launch was CONNECTED by a
    later command of the same shell. The walk now runs before every command and refuses it."""
    shell, closed = _walking_shell(monkeypatch)
    monkeypatch.setattr(confinement, "_hands_walk", lambda h, on_start=None, cancelled=None: ["/opt/s2mid/sock"])
    with pytest.raises(ConfinementError, match="/opt/s2mid/sock.*not run"):
        shell._before_command()
    assert closed == [True]


def _walking_shell(monkeypatch):
    import threading

    shell = object.__new__(confinement._BwrapShell)
    shell._hands = _view_hands(monkeypatch)
    shell._lock = threading.Lock()
    shell._closed = False
    shell._preflight_done = threading.Event()
    shell._preflight_done.set()
    shell._preflight_thread = None
    closed: list[bool] = []
    monkeypatch.setattr(confinement._BwrapShell, "close", lambda self: closed.append(True))
    monkeypatch.setattr(confinement._BwrapShell, "_recheck", lambda self: pytest.fail("ran past the walk"))
    return shell, closed


def test_a_walk_that_could_not_run_refuses_the_command_and_keeps_the_shell(monkeypatch):
    """complement L3 r2: a timeout says nothing about the host, so only that command is refused."""
    shell, closed = _walking_shell(monkeypatch)

    def failed(h, on_start=None, cancelled=None):
        raise OSError("the walk as hands timed out")
    monkeypatch.setattr(confinement, "_hands_walk", failed)
    with pytest.raises(ConfinementError, match="timed out.*not run"):
        shell._before_command()
    assert closed == []


def test_a_walk_does_not_start_once_the_shell_is_closed(monkeypatch):
    """codex L3 r2: a walk starting after close() refuses (one already running ends by its own timeout)."""
    shell, _ = _walking_shell(monkeypatch)
    shell._closed = True
    with pytest.raises(ConfinementError, match="closed"):
        shell._refuse_once_closed(object())   # type: ignore[arg-type]


def test_the_linux_editor_refuses_by_name_without_zsh(tmp_path, monkeypatch):
    real_access = os.access
    monkeypatch.setattr(confinement.os, "access",
                        lambda p, mode, **kw: False if p == confinement.HANDS_ZSH else real_access(p, mode, **kw))
    monkeypatch.setattr(confinement, "_run_hands_helper", lambda *a, **k: pytest.fail("the helper ran without zsh"))
    hands = _hands(tmp_path)
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    with pytest.raises(ConfinementError, match=r"install zsh \(the editor's hands use it\)"):
        BwrapProvider().hands_file(policy, hands, "read", str(hands.workspace / "a.txt"))


def test_a_hands_launch_refuses_without_python3_before_any_hands_process(tmp_path, monkeypatch):
    # python3 runs the keyring join in front of every Linux hands process, ports or none.
    real_access = os.access
    monkeypatch.setattr(confinement.os, "access",
                        lambda p, mode, **kw: False if p == confinement.HANDS_PYTHON else real_access(p, mode, **kw))
    monkeypatch.setattr(confinement, "_require_hands_sudo", lambda h: pytest.fail("a hands process ran first"))
    hands = _hands(tmp_path)
    assert not hands.egress_ports
    with pytest.raises(ConfinementError, match=r"/usr/bin/python3 is missing.*session keyring.*fail-closed"):
        BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)), hands=hands)


def test_the_relays_carry_a_connection_from_the_sandbox_side_to_the_hosts_loopback():
    """Both halves of the one relay program, run as this user on this host: a TCP client on the
    namespace side's port -> its unix socket -> the host side -> a listener on 127.0.0.1 gets the
    reply. Here the two sides share one network namespace, so they use two ports, the namespace side's
    socket a symlink to the host side's."""
    import shutil
    import socket
    import stat as st
    import sys
    import tempfile
    import threading

    target = socket.socket()
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    a = target.getsockname()[1]

    def echo():
        c = target.accept()[0]
        c.sendall(b"pong:" + c.recv(100))
        c.close()

    threading.Thread(target=echo, daemon=True).start()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    b = probe.getsockname()[1]
    probe.close()
    base = tempfile.mkdtemp(prefix="lvr", dir="/tmp")   # short: a unix socket path has a small limit
    out_dir, in_dir = os.path.join(base, "o"), os.path.join(base, "i")
    py = [sys.executable, "-I", "-S", "-c", confinement._HANDS_RELAY]
    out = subprocess.Popen([*py, "out", out_dir, str(a)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, start_new_session=True)
    inn = None
    try:
        assert out.stdout.readline() == b"ready\n"
        sock = os.path.join(out_dir, f"{a}.sock")
        assert st.S_IMODE(os.stat(out_dir).st_mode) == 0o700 and st.S_IMODE(os.stat(sock).st_mode) == 0o600
        os.mkdir(in_dir)
        os.symlink(sock, os.path.join(in_dir, f"{b}.sock"))
        client = (f"import socket; s = socket.create_connection(('127.0.0.1', {b})); s.sendall(b'ping'); "
                  "s.shutdown(socket.SHUT_WR); print(s.recv(100).decode())")
        inn = subprocess.Popen([*py, "in", in_dir, str(b), "--", sys.executable, "-I", "-S", "-c", client],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
        said, err = inn.communicate(timeout=30)
        assert said.strip() == b"pong:ping", err
        out.send_signal(signal.SIGTERM)
        out.wait(timeout=10)
        assert not os.path.exists(out_dir)   # the host side removes its sockets and directory
    finally:
        for p in (out, inn):
            if p is not None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)   # the namespace side's forked relay is in this group
                except OSError:
                    pass
                p.wait()
        target.close()
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(platform.system() != "Linux", reason="the relay reads /proc")
def test_a_relay_removes_a_dead_levains_relay_directory_and_keeps_a_live_ones(tmp_path):
    """RUN 2026-10-09 (R5): a relay SIGKILLed outright left its directory in the hands home, unswept. The
    next relay removes every one whose maker (pid and start time in its name) is gone."""
    import sys
    import time

    pre = confinement._HANDS_RELAY_PREFIX
    me = f"{os.getpid()}-{confinement._proc_start_time(os.getpid())}"
    legacy, dead, live = tmp_path / f"{pre}0123abcd", tmp_path / f"{pre}{os.getpid()}-1-ab", tmp_path / f"{pre}{me}-cd"
    # a name with no maker cannot be judged, so it is kept (L3 r4)
    for d in (legacy, dead, live):
        d.mkdir()
    (dead / "18080.sock").write_text("")
    new = tmp_path / f"{pre}{me}-ef"
    relay = subprocess.Popen([sys.executable, "-I", "-S", "-c", confinement._HANDS_RELAY, "out", str(new)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert relay.stdout.readline() == b"ready\n", relay.stderr.read()   # type: ignore[union-attr]
        assert not dead.exists()
        assert legacy.exists() and live.exists() and new.exists()
    finally:
        relay.terminate()
        relay.wait(10)
    deadline = time.monotonic() + 5
    while new.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not new.exists()


def test_every_path_field_of_the_policy_is_narrowed_for_a_hands_view_or_kept_on_purpose():
    """glm L3 r4: the first narrowing left ssh_dir out. A new path field fails here until it is put in one
    list or the other. (The hardlink rule is NOT narrowed: see the test after the next.)"""
    import dataclasses
    import typing

    hints = typing.get_type_hints(confinement.CrownJewelsPolicy)
    paths = {f.name for f in dataclasses.fields(confinement.CrownJewelsPolicy) if "Path" in str(hints[f.name])}
    assert paths == set(confinement._HANDS_POLICY_NARROWED) | set(confinement._HANDS_POLICY_KEPT)
    assert not set(confinement._HANDS_POLICY_NARROWED) & set(confinement._HANDS_POLICY_KEPT)


def test_a_hands_view_drops_the_operators_ssh_dir_and_keeps_jewels_inside_it(tmp_path, monkeypatch):
    hands = _view_hands(monkeypatch)
    inside = Path("/etc/levain-jewel")
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    policy = confinement.replace(policy, ssh_dir=tmp_path / ".ssh", deny_files=(*policy.deny_files, inside))
    narrowed = confinement._hands_policy(policy, hands)
    assert narrowed.ssh_dir is None
    assert inside in narrowed.deny_files
    assert all(confinement._in_hands_view(p, hands) for p in narrowed.deny_read_write)


def test_a_cancelled_helper_is_stopped_by_its_own_thread_once(monkeypatch, tmp_path):
    """L3 r3 + r4: close() must not signal the walk (a reused pid) nor return while it runs; the run
    thread polls `cancelled` and stops its own helper."""
    import time

    stops: list[int] = []

    def stop(h, proc):
        stops.append(proc.pid)
        proc.kill()
        proc.wait()
        return True
    monkeypatch.setattr(confinement, "_stop_hands_group", stop)
    began = time.monotonic()
    with pytest.raises(OSError, match="cancelled"):
        confinement._run_hands_helper(["/bin/sleep", "30"], _hands(tmp_path), b"", 60, "the walk",
                                      cancelled=lambda: time.monotonic() - began > 0.3)
    assert len(stops) == 1 and time.monotonic() - began < 5


def test_the_hardlink_rule_sees_the_whole_policy_for_a_hands_launch(tmp_path, monkeypatch):
    """codex + complement L3 r5: narrowing the hardlink check to the view failed open: a jewel outside
    the view with its other name inside it (/opt, the workspace) was let through. Phill 10-03: refuse
    bash when ANY jewel inode has st_nlink > 1."""
    seen: list[object] = []
    monkeypatch.setattr(confinement, "_refuse_multiply_linked_jewels", lambda p: seen.append(p) or
                        (_ for _ in ()).throw(ConfinementError("2 names")))
    monkeypatch.setattr(confinement, "refresh_socket_denies", lambda p: p)
    hands = _view_hands(monkeypatch)
    policy = build_policy(_entity(tmp_path), workspace=hands.workspace)
    with pytest.raises(ConfinementError, match="2 names"):
        BwrapProvider().spawn_shell(policy, hands=hands)
    assert seen == [policy]


def test_close_waits_for_an_admitted_command_until_its_spawn_is_registered(monkeypatch):
    """codex L3 r6, RUN on w22: close() from another thread returned, and released the claim, while a
    run() past its walk had not yet spawned; that spawn then ran with no claim. close() now waits for an
    admitted run's preflight, except on the run thread itself, and no run is admitted once it is closed."""
    import threading

    shell = object.__new__(confinement._BwrapShell)
    shell._hands = None
    shell._lock = threading.Lock()
    shell._closed = False
    shell._preflight_done = threading.Event()
    shell._preflight_done.set()
    shell._preflight_thread = None
    shell._relay = None
    shell._ledger_claim = None
    monkeypatch.setattr(confinement._BwrapShell, "_recheck", lambda self: None)
    torn_down: list[bool] = []
    monkeypatch.setattr(confinement.SandboxedShell, "close", lambda self: torn_down.append(True))

    shell._before_command()                        # this thread is admitted: its spawn is being prepared
    closer = threading.Thread(target=shell.close)
    closer.start()
    closer.join(0.5)
    assert closer.is_alive() and torn_down == []   # close() waits for the admitted run
    shell._end_preflight()                         # the run's group is registered (or it was refused)
    closer.join(5)
    assert not closer.is_alive() and torn_down == [True]
    with pytest.raises(ConfinementError, match="closed"):
        shell._before_command()                    # nothing is admitted once closed

    shell._closed = False
    shell._before_command()
    shell.close()                                  # the run thread closing its own shell does not wait
    assert torn_down == [True, True]


def test_a_close_whose_wait_times_out_keeps_the_claim(monkeypatch):
    """codex + glm + complement L3 r7, RUN on w23: when close()'s wait timed out with a preflight still
    outstanding (a check stuck on a hung mount), it released the claim, and the run then spawned without
    one. The claim is now kept; once the preflight drains, a close() releases it as before."""
    import threading

    shell = object.__new__(confinement._BwrapShell)
    shell._hands = None
    shell._lock = threading.Lock()
    shell._closed = False
    shell._groups = {}
    shell._preflight_done = threading.Event()
    shell._preflight_done.set()
    shell._preflight_thread = None
    shell._relay = None
    shell._ledger_claim = "the-claim"
    monkeypatch.setattr(confinement._BwrapShell, "_recheck", lambda self: None)
    monkeypatch.setattr(confinement._BwrapShell, "_settled", lambda self: True)
    monkeypatch.setattr(confinement.SandboxedShell, "close", lambda self: None)
    released: list[str] = []
    monkeypatch.setattr(confinement, "_ledger_release", released.append)
    real_wait = shell._preflight_done.wait
    monkeypatch.setattr(shell._preflight_done, "wait", lambda timeout=None: real_wait(0.2))

    shell._before_command()                        # admitted, and never ends its preflight here
    closer = threading.Thread(target=shell.close)
    closer.start()
    closer.join(5)
    assert not closer.is_alive() and released == []   # the wait timed out: the claim is kept

    shell._end_preflight()
    shell._ledger_claim = "the-claim"
    shell.close()                                   # drained: released as before
    assert released == ["the-claim"]
