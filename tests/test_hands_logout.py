"""`levain hands logout` (levain 15 V2) and the doctor row. Same shims as tests/test_hands_login.py, with the logout
branches added; the sweep is a recorder that writes into the shim log, so ORDER is read from one file. No real
logout, no user, no keychain, no live app; the operator's ~/.claude and ~/.codex are sentinels that must not change."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from levain import doctor, hands_live
from levain.cli import build_parser
from levain.firing import hands_login
from levain.firing.confinement import HandsIdentity
from levain.firing.hands_login import HandsLoginError
from tests.test_hands_login import CLAUDE_SHIM, CODEX_SHIM

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")

# logout branches: LEAVE=1 makes the vendor logout leave the file behind (the fallback case); FAIL=1 exits 1 and keeps it
CLAUDE_OUT = CLAUDE_SHIM.replace('''  "auth status")''', '''  "auth logout")
    [ -n "$SHIM_LEAVE" ] || rm -f "$CLAUDE_CONFIG_DIR/.credentials.json"
    exit 0;;
  "auth status")''')
CODEX_OUT = CODEX_SHIM.replace('''  "login status")''', '''  "logout ")
    [ -n "$SHIM_LEAVE" ] || rm -f "$CODEX_HOME/auth.json"
    exit 0;;
  "login status")''')


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("claude", CLAUDE_OUT), ("codex", CODEX_OUT)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    hands_home = tmp_path / "hands-home"
    hands_home.mkdir()
    operator_home = tmp_path / "operator-home"
    for d in (".claude", ".codex"):
        (operator_home / d).mkdir(parents=True)
        (operator_home / d / "sentinel").write_text("operator's own")
    monkeypatch.setenv("HOME", str(operator_home))
    log = tmp_path / "shim.log"
    entity = tmp_path / "entity"
    entity.mkdir()
    h = HandsIdentity("levain-hands", os.getuid(), str(hands_home), tmp_path / "ws")
    leave = {"on": False}

    def fake_prefix(hands, vendor):
        env = hands_login.vendor_env(hands, vendor)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["SHIM_LOG"] = str(log)
        if leave["on"]:
            env["SHIM_LEAVE"] = "1"
        return ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items())]

    def fake_sweep(user, *, uid, timeout=5.0):
        with open(log, "a") as f:
            f.write(f"SWEEP {user} {uid}\n")
        return None

    monkeypatch.setattr(hands_login, "_launch_prefix", fake_prefix)
    monkeypatch.setattr(hands_live, "sweep_hands_user", fake_sweep)
    monkeypatch.setattr(hands_login, "load_hands_identity", lambda e, cfg=None: h)
    return h, hands_home, operator_home, log, entity, leave


def _calls(log: Path) -> list[str]:
    return [ln.split("|", 3)[-1] if "|" in ln else ln for ln in log.read_text().splitlines()]


@pytest.mark.parametrize("vendor,logout_args,cred", [("claude", "auth logout", ".claude/.credentials.json"),
                                                     ("codex", "logout", ".codex/auth.json")])
def test_sweep_runs_before_the_vendor_logout_and_the_file_is_gone(rig, capsys, vendor, logout_args, cred):
    h, hands_home, operator_home, log, entity, _ = rig
    assert hands_login.cmd_hands_login(entity, vendor) == 0
    capsys.readouterr()
    log.write_text("")
    assert hands_login.cmd_hands_logout(entity, vendor) == 0
    calls = _calls(log)
    assert calls[0].startswith("SWEEP levain-hands")
    assert logout_args in calls and calls.index(logout_args) > 0
    assert not (hands_home / cred).exists()
    out = capsys.readouterr().out
    assert f"hands {vendor}: signed out (logged-out)" in out
    assert "LOCAL ONLY" in out and "Revoke it there" in out and "could have used the login" in out
    assert hands_login._VENDOR[vendor]["page"].split()[0] in out
    assert hands_login.read_outcomes(entity)[vendor]["outcome"] == "logged-out"
    assert str(operator_home) not in log.read_text()


def test_a_second_logout_is_idempotent(rig, capsys):
    h, _, _, log, entity, _ = rig
    hands_login.cmd_hands_login(entity, "claude")
    assert hands_login.cmd_hands_logout(entity, "claude") == 0
    capsys.readouterr()
    log.write_text("")
    assert hands_login.cmd_hands_logout(entity, "claude") == 0
    assert "nothing signed in" in capsys.readouterr().out
    assert not any(c.startswith("SWEEP") or "logout" in c for c in _calls(log))   # status reads only


def test_file_removal_fallback_removes_exactly_the_one_file(rig, capsys):
    h, hands_home, operator_home, log, entity, leave = rig
    hands_login.cmd_hands_login(entity, "codex")
    other = hands_home / ".codex" / "config.toml"
    keep = hands_home / ".codex" / "sessions"
    keep.mkdir()
    before = other.read_text()
    leave["on"] = True
    assert hands_login.cmd_hands_logout(entity, "codex") == 0
    assert not (hands_home / ".codex" / "auth.json").exists()
    assert other.read_text() == before and keep.is_dir()
    assert "signed out (file-removed)" in capsys.readouterr().out
    assert hands_login.read_outcomes(entity)["codex"]["outcome"] == "file-removed"
    assert hands_login.remove_argv(h, "codex")[-4:] == ["/bin/rm", "-f", "--", f"{hands_home}/.codex/auth.json"]


def test_a_file_that_cannot_be_removed_fails(rig, capsys, monkeypatch):
    h, hands_home, _, log, entity, leave = rig
    hands_login.cmd_hands_login(entity, "claude")
    leave["on"] = True
    monkeypatch.setattr(hands_login, "remove_argv", lambda hands, v: ["/usr/bin/false"])
    assert hands_login.cmd_hands_logout(entity, "claude") == 1
    assert "is still there" in capsys.readouterr().out
    assert hands_login.read_outcomes(entity)["claude"]["outcome"] == "failed"


def test_processes_left_refuse_the_logout(rig, capsys, monkeypatch):
    h, hands_home, _, log, entity, _ = rig
    hands_login.cmd_hands_login(entity, "claude")
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda u, *, uid, timeout=5.0: "processes of levain-hands are still running")
    assert hands_login.cmd_hands_logout(entity, "claude") == 1
    assert "nothing was logged out" in capsys.readouterr().out
    assert (hands_home / ".claude" / ".credentials.json").exists()


def test_a_live_session_refuses_the_logout(rig, capsys):
    h, hands_home, _, _, entity, _ = rig
    hands_login.cmd_hands_login(entity, "claude")
    (entity / ".levain").mkdir(exist_ok=True)
    fd = hands_live.take_exclusive(entity)
    try:
        assert hands_login.cmd_hands_logout(entity, "claude") == 1
    finally:
        os.close(fd)
    assert "a session of this entity is open" in capsys.readouterr().out
    assert (hands_home / ".claude" / ".credentials.json").exists()


def test_no_hands_account_is_one_refusal_for_both_vendors(tmp_path, capsys):
    msgs = []
    for v in ("claude", "codex"):
        assert hands_login.cmd_hands_logout(tmp_path, v) == 1
        msgs.append(capsys.readouterr().out)
    assert msgs[0] == msgs[1] == f"hands logout: {hands_login.NO_HANDS}\n"
    assert not (tmp_path / ".levain").exists()


def test_operator_files_are_never_touched(rig):
    h, _, operator_home, _, entity, _ = rig
    before = {p: p.read_text() for p in operator_home.rglob("sentinel")}
    for v in ("claude", "codex"):
        hands_login.cmd_hands_login(entity, v)
        hands_login.cmd_hands_logout(entity, v)
    assert {p: p.read_text() for p in operator_home.rglob("sentinel")} == before
    assert sorted(p.name for p in operator_home.rglob("*") if p.is_file()) == ["sentinel", "sentinel"]


def test_cli_registers_logout():
    a = build_parser().parse_args(["hands", "logout", "codex", "--path", "/e"])
    assert (a.hands_cmd, a.vendor, str(a.path)) == ("logout", "codex", "/e")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hands", "logout", "gemini"])


# --- the doctor row ---------------------------------------------------------------------------------------------

def _rows(entity):
    return {r.name: r for r in doctor._check_hands_login(entity)}


def test_doctor_row_is_quiet_without_a_hands_account(tmp_path, monkeypatch):
    monkeypatch.undo()
    assert doctor._check_hands_login(tmp_path) == []


def test_doctor_row_states(rig, monkeypatch):
    h, hands_home, _, _, entity, _ = rig
    real = hands_login.credential_state

    def as_hands(hands, vendor):      # the shims run as this user; on a real host the file is the hands user's
        got = real(hands, vendor)
        return None if got is None else f"{hands.user} {got.split()[-1]}"

    monkeypatch.setattr(hands_login, "credential_state", as_hands)
    rows = _rows(entity)
    assert set(rows) == {"hands login claude", "hands login codex"}
    r = rows["hands login claude"]
    assert r.ok and r.neutral and "not signed in" in r.detail and "last: none recorded" in r.detail
    hands_login.cmd_hands_login(entity, "claude")
    r = _rows(entity)["hands login claude"]
    assert r.ok and not r.warn and not r.neutral
    assert "owner levain-hands, mode 600" in r.detail
    assert "last: logged-in" in r.detail
    os.chmod(hands_home / ".claude" / ".credentials.json", 0o644)
    r = _rows(entity)["hands login claude"]
    assert r.warn and "mode 644" in r.detail and "levain hands logout claude" in r.hint
    hands_login.cmd_hands_logout(entity, "claude")
    r = _rows(entity)["hands login claude"]
    assert r.neutral and "last: logged-out" in r.detail
    assert _rows(entity)["hands login codex"].neutral


def test_doctor_row_owner_other_than_hands_warns(rig, monkeypatch):
    h, *_, entity, _ = rig
    monkeypatch.setattr(hands_login, "credential_state", lambda hands, v: "someone-else 600")
    r = _rows(entity)["hands login claude"]
    assert r.warn and "owner someone-else" in r.detail
