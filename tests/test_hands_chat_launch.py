"""`levain hands launch` (1107-26): the hands-uid chat launcher. The reproduced failure (the M2 L3 HIGH): a server revoked
after an earlier `levain hands mcp prepare` stays in both launch files until something rewrites them, so a launch on
those files runs it. The launcher rewrites them from the live registry, under the entity's locks, strictly before the
vendor starts, and starts nothing when that fails. The rig is the M2/M3 one (a sudo shim, the hands steps run as the
current user, a real scratch authority log); the vendor is a fake that reads the launch files at the moment it starts.
No real claude or codex runs."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import tomllib

import pytest

from levain import hands_live
from levain.cli import build_parser
from levain.firing import hands_chat as hc, hands_login, hands_mcp_login as hml
from levain.firing.ws_git import WsGitError, hold_hands_session
from tests.test_hands_mcp_verbs import McpRig

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")

_REAL_HOST_REFUSAL = getattr(hc, "host_refusal", None)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = McpRig(tmp_path, monkeypatch)
    # the launch's own logic runs on any test host; the macOS-only refusal has its own test
    monkeypatch.setattr(hc, "host_refusal", lambda: None, raising=False)
    r.claude_file = r.hands_home / ".claude" / hml.CLAUDE_LAUNCH_FILE
    r.codex_file = r.hands_home / ".codex" / "config.toml"
    r.events = []
    real_sweep = hands_live.sweep_hands_user

    def sweep(user, *, uid, timeout=5.0):
        r.events.append(("sweep", locks(r)))
        return real_sweep(user, uid=uid, timeout=timeout)
    monkeypatch.setattr(hands_live, "sweep_hands_user", sweep)
    real_prepare = hml.prepare_launch

    def prepare(*a, **k):
        r.events.append(("prepare", locks(r)))
        return real_prepare(*a, **k)
    monkeypatch.setattr(hml, "prepare_launch", prepare)
    yield r
    r.oauth.close()


def locks(rig) -> tuple[bool, bool]:
    """(session lock held by someone, one-session lock held by someone), probed without keeping either."""
    fd = hands_live.take_exclusive(rig.entity)
    session = fd is None
    if fd is not None:
        os.close(fd)
    try:
        one = hold_hands_session(rig.entity)
    except WsGitError:
        one_held = True
    else:
        os.close(one)
        one_held = False
    return session, one_held


class FakeVendor:
    """The vendor at the moment it starts: what both launch files hold then, the argv, and the locks."""

    def __init__(self, rig, *, rc: int = 0):
        self.rig, self.rc, self.started = rig, rc, []

    def __call__(self, argv, cwd):
        r = self.rig
        r.events.append(("spawn", locks(r)))
        self.started.append({
            "argv": argv, "cwd": cwd,
            "claude": json.loads(r.claude_file.read_text())["mcpServers"],
            "codex": tomllib.loads(r.codex_file.read_text()).get("mcp_servers", {}),
        })
        return self

    def wait(self):
        return self.rc


def launch(rig, vendor="claude", **kw):
    return hc.launch_hands_chat(rig.entity, vendor, rig.ck.root, emit=lambda s: None, **kw)


def assert_released(rig):
    assert locks(rig) == (False, False)


# --- (1) a revoked server is absent from both launch files when the vendor starts ---------------------------------

@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_a_server_revoked_after_an_earlier_prepare_is_absent_from_both_files_at_spawn(rig, vendor):
    a = rig.register("alpha", callback_port=49601)
    rig.register("beta", callback_port=49602)
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 0
    rig.events.clear()
    rig.ck.link({"op": "revoke_grant", "grant_id": a["id"]})
    # the reproduced failure: after the revoke, the files an earlier prepare wrote still hold alpha
    assert "alpha" in json.loads(rig.claude_file.read_text())["mcpServers"]
    assert "alpha" in tomllib.loads(rig.codex_file.read_text())["mcp_servers"]
    fake = FakeVendor(rig, rc=7)
    assert launch(rig, vendor, spawn=fake) == 7
    [seen] = fake.started
    assert set(seen["claude"]) == {"beta"} and set(seen["codex"]) == {"beta"}
    if vendor == "claude":
        assert seen["argv"][-4:] == ["claude", "--strict-mcp-config", "--mcp-config", hml.claude_launch_path(rig.h)]
    else:
        assert seen["argv"][-5:] == ["codex", "--disable", "plugins", "--disable", "remote_plugin"]
        assert f"CODEX_HOME={rig.hands_home}/.codex" in seen["argv"]
    assert seen["cwd"] == str(rig.h.workspace)
    assert_released(rig)


# --- (2) a refusal starts nothing and releases both locks ---------------------------------------------------------

def _foreign_codex_server(rig, monkeypatch):
    rig.codex_file.parent.mkdir(parents=True, exist_ok=True)
    rig.codex_file.write_text('[mcp_servers.planted]\nurl = "https://elsewhere.example/mcp"\n')
    return "already holds a server named 'planted'"


def _unreadable_registry(rig, monkeypatch):
    (rig.ck.root / "cockpit.db").write_bytes(b"not a database")
    return "authority log cannot be read"


def _partial_prepare(rig, monkeypatch):
    real = hands_login.conf_commit

    def commit(h, v, d, n, t, **k):
        if v == "codex":
            raise hands_login.HandsLoginError("could not replace")
        return real(h, v, d, n, t, **k)
    monkeypatch.setattr(hands_login, "conf_commit", commit)
    return "was already replaced, the other was not"


def _stale_process(rig, monkeypatch):
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda user, *, uid, timeout=5.0: "pid 4242 is left")
    return "could not be stopped"


def _spawn_fails(rig, monkeypatch):
    return "no such program"


@pytest.mark.parametrize("make", [_foreign_codex_server, _unreadable_registry, _partial_prepare, _stale_process,
                                  _spawn_fails])
def test_a_refusal_before_the_start_starts_no_vendor_and_releases_both_locks(rig, monkeypatch, make):
    a = rig.register("alpha", callback_port=49601)
    assert hml.cmd_prepare(rig.entity, rig.ck.root) == 0        # the earlier files: alpha in both
    rig.events.clear()
    rig.ck.link({"op": "revoke_grant", "grant_id": a["id"]})
    why = make(rig, monkeypatch)
    fake = FakeVendor(rig)

    def failing(argv, cwd):
        raise FileNotFoundError("no such program")
    with pytest.raises(hc.HandsChatRefused, match=why) as info:
        launch(rig, spawn=failing if make is _spawn_fails else fake)
    assert str(info.value).endswith("no chat was started")
    assert fake.started == [] and "spawn" not in [e for e, _ in rig.events]
    assert_released(rig)


def _during_commit(monkeypatch, act):
    """``act()`` once, while prepare commits the launch files: after the registry was read, before the start."""
    real = hands_login.conf_commit
    done = []

    def commit(h, v, d, n, t, **k):
        out = real(h, v, d, n, t, **k)
        if not done:
            done.append(1)
            act()
        return out
    monkeypatch.setattr(hands_login, "conf_commit", commit)
    return done


@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_a_revoke_appended_while_the_files_are_written_refuses_the_start(rig, monkeypatch, vendor):
    # L3 r3 G5 (codex HIGH): a revoke is an authority-log append that takes no hands lock, so it can land after the
    # launch read the registry; the launcher wrote alpha and started the vendor anyway
    a = rig.register("alpha", callback_port=49601)
    rig.register("beta", callback_port=49602)
    done = _during_commit(monkeypatch, lambda: rig.ck.link({"op": "revoke_grant", "grant_id": a["id"]}))
    fake = FakeVendor(rig)
    with pytest.raises(hc.HandsChatRefused, match=f"entry {a['id']} was revoked or ran out") as info:
        launch(rig, vendor, spawn=fake)
    assert done and str(info.value).endswith("no chat was started")
    assert fake.started == [] and "spawn" not in [e for e, _ in rig.events]
    assert_released(rig)
    assert launch(rig, vendor, spawn=fake) == 0                  # the next launch writes the files without alpha
    assert set(fake.started[0]["claude"]) == {"beta"} and set(fake.started[0]["codex"]) == {"beta"}


def test_an_entry_that_runs_out_while_the_files_are_written_refuses_the_start(rig, monkeypatch):
    from datetime import datetime as real_dt, timedelta
    a = rig.register("alpha", callback_port=49601)
    late = []

    class Clock(real_dt):
        @classmethod
        def now(cls, tz=None):
            t = real_dt.now(tz)
            return t + timedelta(days=40) if late else t      # past every 30-day lease once the commit ran
    monkeypatch.setattr(hml, "datetime", Clock)
    _during_commit(monkeypatch, lambda: late.append(1))
    fake = FakeVendor(rig)
    with pytest.raises(hc.HandsChatRefused, match=f"entry {a['id']} was revoked or ran out"):
        launch(rig, spawn=fake)
    assert fake.started == []
    assert_released(rig)


def test_an_entry_registered_after_the_read_does_not_refuse_the_start(rig, monkeypatch):
    rig.register("alpha", callback_port=49601)
    _during_commit(monkeypatch, lambda: rig.register("beta", callback_port=49602))
    fake = FakeVendor(rig)
    assert launch(rig, spawn=fake) == 0
    assert set(fake.started[0]["claude"]) == {"alpha"}           # the files only lack beta until the next launch


def test_an_open_session_refuses_the_launch_and_its_locks_are_left_to_it(rig):
    rig.register("alpha", callback_port=49601)
    held = hold_hands_session(rig.entity)                       # a session of this entity is running as its user
    try:
        fake = FakeVendor(rig)
        with pytest.raises(hc.HandsChatRefused, match="another session of this entity"):
            launch(rig, spawn=fake)
        assert fake.started == [] and [e for e, _ in rig.events] == []
        fd = hands_live.take_exclusive(rig.entity)              # the launcher's shared lock was released
        assert fd is not None
        os.close(fd)
    finally:
        os.close(held)


def test_the_verb_reports_a_refusal_and_exits_1(rig, capsys, monkeypatch):
    _unreadable_registry(rig, monkeypatch)
    monkeypatch.setattr(hc, "_spawn", lambda argv, cwd: pytest.fail("the vendor must not start"))
    assert hc.cmd_hands_chat(rig.entity, "claude", rig.ck.root) == 1
    out = capsys.readouterr().out
    assert out.startswith("hands chat: ") and "no chat was started" in out
    assert_released(rig)


# --- (3) the order: locks, stop, prepare, then the vendor ---------------------------------------------------------

def test_prepare_runs_strictly_before_the_vendor_start_and_both_under_both_locks(rig):
    rig.register("alpha", callback_port=49601)
    assert launch(rig, spawn=FakeVendor(rig)) == 0
    assert rig.events == [("sweep", (True, True)), ("prepare", (True, True)), ("spawn", (True, True)),
                          ("sweep", (True, True))]
    assert_released(rig)


# --- (4) a host that is not macOS is refused up front (L3 r1, codex HIGH: the Linux start program has no PATH search) -

def test_a_host_that_is_not_macos_refuses_before_any_lock_sweep_or_prepare(rig, monkeypatch, capsys):
    monkeypatch.setattr(hc, "host_refusal", _REAL_HOST_REFUSAL)
    monkeypatch.setattr(hc.platform, "system", lambda: "Linux")
    taken = []
    monkeypatch.setattr(hc, "hold_session_lock", lambda e: taken.append("session") or pytest.fail("a lock was taken"))
    rig.register("alpha", callback_port=49601)
    fake = FakeVendor(rig)
    with pytest.raises(hc.HandsChatRefused, match="macOS only in this slice") as info:
        launch(rig, spawn=fake)
    assert str(info.value).endswith("no chat was started")
    assert fake.started == [] and rig.events == [] and taken == []
    assert hc.cmd_hands_chat(rig.entity, "codex", rig.ck.root) == 1
    assert "macOS only in this slice" in capsys.readouterr().out
    assert_released(rig)


# --- (5) a project layer in the hands workspace is refused (L3 r1, codex HIGH: Codex loads a trusted workspace's ---
# --- .codex/config.toml, so a server the registry does not hold would start) ---------------------------------------

def _codex_layer(ws):
    (ws / ".codex").mkdir()
    (ws / ".codex" / "config.toml").write_text('[mcp_servers.evil]\ncommand = "/bin/sh"\n')
    return ".codex/config.toml"


def _codex_other_config(ws):
    (ws / ".codex").mkdir()
    (ws / ".codex" / "config.json").write_text("{}")
    return ".codex/config.json"


def _codex_link(ws):
    elsewhere = ws.parent / "elsewhere"         # a link is refused as such, whatever its target holds now
    elsewhere.mkdir()
    (ws / ".codex").symlink_to(elsewhere)
    return ".codex"


def _codex_upper(ws):
    # L3 r2 N1: APFS is case-insensitive by default, so Codex opening config.toml reads this file
    (ws / ".codex").mkdir()
    (ws / ".codex" / "CONFIG.TOML").write_text('[mcp_servers.evil]\ncommand = "/bin/sh"\n')
    return ".codex/CONFIG.TOML"


def _codex_mixed(ws):
    (ws / ".codex").mkdir()
    (ws / ".codex" / "Settings.JSON").write_text("{}")
    return ".codex/Settings.JSON"


def _claude_mcp_json(ws):
    (ws / ".mcp.json").write_text('{"mcpServers": {"evil": {"command": "/bin/sh"}}}')
    return ".mcp.json"


_CODEX_ONLY = [_codex_layer, _codex_other_config, _codex_link, _codex_upper, _codex_mixed]


@pytest.mark.parametrize("vendor, plant", [("codex", p) for p in _CODEX_ONLY]
                         + [("claude", _claude_mcp_json), ("codex", _claude_mcp_json)])
def test_a_project_layer_in_the_hands_workspace_refuses_and_starts_no_vendor(rig, vendor, plant):
    rig.register("alpha", callback_port=49601)
    ws = rig.h.workspace
    ws.mkdir(parents=True, exist_ok=True)
    named = plant(ws)
    fake = FakeVendor(rig)
    with pytest.raises(hc.HandsChatRefused, match="project layer") as info:
        launch(rig, vendor, spawn=fake)
    assert str(ws / named) in str(info.value) and str(info.value).endswith("no chat was started")
    assert fake.started == [] and "spawn" not in [e for e, _ in rig.events]
    assert_released(rig)


@pytest.mark.parametrize("plant", _CODEX_ONLY)
def test_a_codex_project_layer_does_not_refuse_a_claude_chat(rig, plant):
    # L3 r2 (codex MED): Claude does not read .codex/, so a repository's Codex config must not block a Claude launch
    rig.register("alpha", callback_port=49601)
    ws = rig.h.workspace
    ws.mkdir(parents=True, exist_ok=True)
    plant(ws)
    fake = FakeVendor(rig)
    assert launch(rig, "claude", spawn=fake) == 0 and len(fake.started) == 1
    assert_released(rig)


def test_a_codex_dir_without_a_config_file_is_not_a_project_layer(rig):
    rig.register("alpha", callback_port=49601)
    ws = rig.h.workspace
    (ws / ".codex").mkdir(parents=True)
    (ws / ".codex" / "notes.txt").write_text("not read by codex")
    fake = FakeVendor(rig)
    assert launch(rig, "codex", spawn=fake) == 0 and len(fake.started) == 1


# --- (6) a signal or an interrupt during the chat stops it, sweeps and releases; survivors exit non-zero ------------

class BlockingVendor(FakeVendor):
    """A vendor whose wait blocks on an event that only ``terminate``/``kill`` set; ``on_wait`` runs at the first
    wait, as a signal arriving mid-chat would."""

    def __init__(self, rig, on_wait):
        super().__init__(rig)
        self.on_wait, self.ended, self.terminated = on_wait, threading.Event(), 0
        self.waited = 0

    def wait(self, timeout=None):
        self.waited += 1
        if self.waited == 1:
            self.on_wait()
        if not self.ended.wait(timeout if timeout is not None else 5):
            raise subprocess.TimeoutExpired("vendor", timeout)
        return -15

    def terminate(self):
        self.terminated += 1
        self.ended.set()

    def kill(self):
        self.ended.set()


def _deliver(signum):
    """The signal through whatever handler is installed now (a default disposition is not callable: the test fails
    there, rather than the signal ending the test process)."""
    def go():
        handler = signal.getsignal(signum)
        assert callable(handler), f"no handler for {signum} while the chat runs"
        handler(signum, None)
    return go


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_the_chat_stops_the_vendor_sweeps_releases_and_re_raises(rig, signum):
    rig.register("alpha", callback_port=49601)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    fake = BlockingVendor(rig, _deliver(signum))
    with pytest.raises(SystemExit) as info:
        launch(rig, spawn=fake)
    assert info.value.code == 128 + signum
    assert fake.terminated == 1
    assert [e for e in rig.events] == [("sweep", (True, True)), ("prepare", (True, True)), ("spawn", (True, True)),
                                       ("sweep", (True, True))]
    assert {s: signal.getsignal(s) for s in before} == before
    assert_released(rig)


def test_a_programmatic_exit_during_the_wait_still_stops_sweeps_and_releases(rig):
    rig.register("alpha", callback_port=49601)

    def boom():
        raise SystemExit(3)
    fake = BlockingVendor(rig, boom)
    with pytest.raises(SystemExit, match="3"):
        launch(rig, spawn=fake)
    assert fake.terminated == 1 and rig.events[-1] == ("sweep", (True, True))
    assert_released(rig)


def test_an_interrupt_inside_the_spawn_still_sweeps_the_hands_user(rig):
    rig.register("alpha", callback_port=49601)

    def interrupted(argv, cwd):
        rig.events.append(("spawn", locks(rig)))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        launch(rig, spawn=interrupted)
    assert [e for e, _ in rig.events] == ["sweep", "prepare", "spawn", "sweep"]
    assert rig.events[-1] == ("sweep", (True, True))
    assert_released(rig)


class SignalledInSpawn(BlockingVendor):
    """A start that forks the child and then takes a stop signal before it returns (L3 r3 G3, codex HIGH): the child
    exists, so the cleanup must hold its handle and stop it."""

    def __init__(self, rig, signum):
        super().__init__(rig, lambda: None)
        self.signum = signum

    def __call__(self, argv, cwd):
        child = super().__call__(argv, cwd)          # the fork happened: a child is running
        _deliver(self.signum)()                      # the stop signal lands before Popen returns
        return child


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_inside_the_spawn_stops_the_child_it_forked_then_exits(rig, signum):
    rig.register("alpha", callback_port=49601)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    fake = SignalledInSpawn(rig, signum)
    with pytest.raises(SystemExit) as info:
        launch(rig, spawn=fake)
    assert info.value.code == 128 + signum
    assert fake.terminated == 1                      # the old tree raised inside the spawn: proc None, never stopped
    assert fake.waited == 1                          # the stop's wait, not the chat's: the chat wait never began
    assert [e for e, _ in rig.events] == ["sweep", "prepare", "spawn", "sweep"]
    assert {s: signal.getsignal(s) for s in before} == before
    assert_released(rig)


def test_a_signal_after_the_spawn_returned_and_before_the_store_is_still_deferred(rig, monkeypatch):
    rig.register("alpha", callback_port=49601)
    fake = BlockingVendor(rig, lambda: None)
    real = hc._Signals.raise_pending

    def late(self):
        _deliver(signal.SIGTERM)()                   # the handler still defers until raise_pending ends the deferral
        assert self.pending == signal.SIGTERM
        real(self)
    monkeypatch.setattr(hc._Signals, "raise_pending", late)
    with pytest.raises(SystemExit) as info:
        launch(rig, spawn=fake)
    assert info.value.code == 128 + signal.SIGTERM and fake.terminated == 1
    assert_released(rig)


def test_processes_left_after_the_chat_make_the_command_exit_non_zero_with_the_warning(rig, monkeypatch):
    rig.register("alpha", callback_port=49601)
    said = []
    answers = iter([None, "pid 4242 is left"])
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda user, *, uid, timeout=5.0: next(answers))
    rc = hc.launch_hands_chat(rig.entity, "claude", rig.ck.root, spawn=FakeVendor(rig, rc=0), emit=said.append)
    assert rc != 0
    assert any("were left after the chat" in s and "pid 4242" in s for s in said)
    assert_released(rig)


def test_a_closed_stdout_at_the_survivor_warning_does_not_replace_the_outcome(rig, monkeypatch):
    # L3 r2 N2: the warning is emitted inside the cleanup finally; a BrokenPipeError there replaced the return code
    rig.register("alpha", callback_port=49601)
    answers = iter([None, "pid 4242 is left"])
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda user, *, uid, timeout=5.0: next(answers))

    def closed(s):
        if "were left" in s:
            raise BrokenPipeError(32, "Broken pipe")
    rc = hc.launch_hands_chat(rig.entity, "claude", rig.ck.root, spawn=FakeVendor(rig, rc=5), emit=closed)
    assert rc == 5
    assert_released(rig)


def test_a_closed_stdout_at_the_survivor_warning_keeps_the_signal_exit(rig, monkeypatch):
    rig.register("alpha", callback_port=49601)
    answers = iter([None, "pid 4242 is left"])
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda user, *, uid, timeout=5.0: next(answers))

    def closed(s):
        if "were left" in s:
            raise BrokenPipeError(32, "Broken pipe")
    with pytest.raises(SystemExit) as info:
        hc.launch_hands_chat(rig.entity, "claude", rig.ck.root, spawn=BlockingVendor(rig, _deliver(signal.SIGTERM)),
                             emit=closed)
    assert info.value.code == 128 + signal.SIGTERM
    assert_released(rig)


# --- (7) the module imports where SIGHUP is absent (L3 r2: evaluated at import, Windows has none) --------------------

def test_the_module_imports_on_a_host_without_sighup():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import signal; del signal.SIGHUP; import levain.firing.hands_chat as h; "
            "assert h._STOP_SIGNALS == (signal.SIGTERM,), h._STOP_SIGNALS; h._Signals().restore(); print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


# --- (8) the chat gets the operator's terminal type, not the prefix's TERM=dumb (L3 r2 M1, measured: codex refuses
# --- its TUI under TERM=dumb without a "Continue anyway? [y/N]") -------------------------------------------------------

@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_the_chat_argv_carries_the_operators_terminal_type(rig, monkeypatch, vendor):
    monkeypatch.setenv("TERM", "xterm-256color")
    argv = hc.chat_argv(rig.h, vendor)
    assert "TERM=xterm-256color" in argv and "TERM=dumb" not in argv
    env_at = argv.index("-i")
    vendor_at = argv.index(vendor)
    assert env_at < argv.index("TERM=xterm-256color") < vendor_at


@pytest.mark.parametrize("term", ["", "dumb", "../../tmp/x", "/etc/passwd", ".hidden", "a b", "x" * 65])
def test_a_term_that_is_not_a_terminfo_name_leaves_dumb(rig, monkeypatch, term):
    monkeypatch.setenv("TERM", term)
    argv = hc.chat_argv(rig.h, "codex")
    assert "TERM=dumb" in argv and not any(a.startswith("TERM=") and a != "TERM=dumb" for a in argv)


def test_only_the_env_assignments_are_rewritten():
    argv = ["sudo", "-n", "-u", "h", "/usr/bin/env", "-i", "HOME=/x", "TERM=dumb", "codex", "TERM=dumb"]
    assert hc.interactive_term(argv, "screen") == [
        "sudo", "-n", "-u", "h", "/usr/bin/env", "-i", "HOME=/x", "TERM=screen", "codex", "TERM=dumb"]
    assert hc.interactive_term(["codex", "TERM=dumb"], "screen") == ["codex", "TERM=dumb"]


# --- the verb ------------------------------------------------------------------------------------------------------

def test_the_verb_parses_under_hands_and_says_it_is_the_hands_uid_chat(rig, capsys):
    ns = build_parser().parse_args(["hands", "launch", "codex", "--path", str(rig.entity)])
    assert ns.hands_cmd == "launch" and ns.vendor == "codex" and ns.data_root is None
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hands", "launch", "claude", "--mcp-config", "x.json"])   # no passthrough
    capsys.readouterr()
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hands", "launch", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "hands-uid chat" in text and "not the cockpit room" in text
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hands", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "hands-uid chat" in text
