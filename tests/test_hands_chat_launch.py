"""`levain hands launch` (1107-26): the hands-uid chat launcher. The reproduced failure (the M2 L3 HIGH): a server revoked
after an earlier `levain hands mcp prepare` stays in both launch files until something rewrites them, so a launch on
those files runs it. The launcher rewrites them from the live registry, under the entity's locks, strictly before the
vendor starts, and starts nothing when that fails. The rig is the M2/M3 one (a sudo shim, the hands steps run as the
current user, a real scratch authority log); the vendor is a fake that reads the launch files at the moment it starts.
No real claude or codex runs."""
from __future__ import annotations

import json
import os
import sys
import tomllib

import pytest

from levain import hands_live
from levain.cli import build_parser
from levain.firing import hands_chat as hc, hands_login, hands_mcp_login as hml
from levain.firing.ws_git import WsGitError, hold_hands_session
from tests.test_hands_mcp_verbs import McpRig

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = McpRig(tmp_path, monkeypatch)
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
        assert seen["argv"][-1] == "codex" and f"CODEX_HOME={rig.hands_home}/.codex" in seen["argv"]
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
