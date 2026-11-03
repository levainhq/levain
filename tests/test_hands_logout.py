"""`levain hands logout` (levain 15 V2) and the doctor row. The shims and the rig are tests/test_hands_login.py's (the
logout branches are in its shims); the sweep is a recorder that writes into the shim log, so ORDER is read from one
file. No real logout, no user, no keychain, no live app; the operator's ~/.claude and ~/.codex are sentinels that
must not change."""
from __future__ import annotations

import os
import sys
import time

import pytest

from levain import doctor, hands_live
from levain.cli import build_parser
from levain.firing import hands_login
from tests.test_hands_login import Rig

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


@pytest.mark.parametrize("vendor,logout_args,cred", [("claude", "auth logout", ".claude/.credentials.json"),
                                                     ("codex", "logout", ".codex/auth.json")])
def test_sweep_runs_before_the_vendor_logout_and_the_file_is_gone(rig, capsys, vendor, logout_args, cred):
    assert hands_login.cmd_hands_login(rig.entity, vendor) == 0
    capsys.readouterr()
    rig.log.write_text("")
    assert hands_login.cmd_hands_logout(rig.entity, vendor) == 0
    calls = rig.calls()
    sweep = next(i for i, c in enumerate(calls) if c.startswith("SWEEP levain-hands"))
    assert calls.index(logout_args) > sweep
    assert not (rig.hands_home / cred).exists()
    out = capsys.readouterr().out
    assert f"hands {vendor}: signed out (logged-out)" in out
    assert "LOCAL ONLY" in out and "Revoke it there" in out and "could have used the login" in out
    assert hands_login._VENDOR[vendor]["page"].split()[0] in out
    assert hands_login.read_outcomes(rig.entity)[vendor]["outcome"] == "logged-out"
    assert str(rig.operator_home) not in rig.log.read_text()


def test_a_second_logout_is_idempotent(rig, capsys):
    hands_login.cmd_hands_login(rig.entity, "claude")
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 0
    capsys.readouterr()
    rig.log.write_text("")
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 0
    assert "nothing signed in" in capsys.readouterr().out
    assert not any(c.startswith("SWEEP") or "logout" in c for c in rig.calls())   # status reads only


def test_file_removal_fallback_removes_exactly_the_one_file(rig, capsys):
    hands_login.cmd_hands_login(rig.entity, "codex")
    other = rig.hands_home / ".codex" / "config.toml"
    keep = rig.hands_home / ".codex" / "sessions"
    keep.mkdir()
    before = other.read_text()
    rig.knobs["SHIM_LEAVE"] = "1"
    assert hands_login.cmd_hands_logout(rig.entity, "codex") == 0
    assert not (rig.hands_home / ".codex" / "auth.json").exists()
    assert other.read_text() == before and keep.is_dir()
    assert "signed out (file-removed)" in capsys.readouterr().out
    assert hands_login.read_outcomes(rig.entity)["codex"]["outcome"] == "file-removed"
    assert hands_login.remove_argv(rig.h, "codex")[-4:] == ["/bin/rm", "-f", "--", f"{rig.hands_home}/.codex/auth.json"]


def test_a_file_that_cannot_be_removed_fails(rig, capsys, monkeypatch):
    hands_login.cmd_hands_login(rig.entity, "claude")
    rig.knobs["SHIM_LEAVE"] = "1"
    monkeypatch.setattr(hands_login, "remove_argv", lambda hands, v: ["/usr/bin/false"])
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    assert "is still there" in capsys.readouterr().out
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "failed"


def test_processes_left_refuse_the_logout(rig, capsys, monkeypatch):
    hands_login.cmd_hands_login(rig.entity, "claude")
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda u, *, uid, timeout=5.0: "processes of levain-hands are still running")
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    assert "nothing was logged out" in capsys.readouterr().out
    assert (rig.hands_home / ".claude" / ".credentials.json").exists()


def test_a_live_session_refuses_the_logout(rig, capsys):
    hands_login.cmd_hands_login(rig.entity, "claude")
    fd = hands_live.take_exclusive(rig.entity)
    try:
        assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    finally:
        os.close(fd)
    assert "a session of this entity is open" in capsys.readouterr().out
    assert (rig.hands_home / ".claude" / ".credentials.json").exists()


def test_no_hands_account_is_one_refusal_for_both_vendors(tmp_path, capsys):
    msgs = []
    for v in ("claude", "codex"):
        assert hands_login.cmd_hands_logout(tmp_path, v) == 1
        msgs.append(capsys.readouterr().out)
    assert msgs[0] == msgs[1] == f"hands logout: {hands_login.NO_HANDS}\n"
    assert not (tmp_path / ".levain").exists()


def test_operator_files_are_never_touched(rig):
    before = {p: p.read_text() for p in rig.operator_home.rglob("sentinel")}
    for v in ("claude", "codex"):
        hands_login.cmd_hands_login(rig.entity, v)
        hands_login.cmd_hands_logout(rig.entity, v)
    assert {p: p.read_text() for p in rig.operator_home.rglob("sentinel")} == before
    assert sorted(p.name for p in rig.operator_home.rglob("*") if p.is_file()) == ["sentinel", "sentinel"]


def test_cli_registers_logout():
    a = build_parser().parse_args(["hands", "logout", "codex", "--path", "/e"])
    assert (a.hands_cmd, a.vendor, str(a.path)) == ("logout", "codex", "/e")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["hands", "logout", "gemini"])


# --- L3 r1 reproductions (each fails on V2 as landed in eda2f593) ----------------------------------------------

def test_a_state_that_cannot_be_read_refuses_the_logout(rig, capsys):
    """V2 codex HIGH 1 / complement MED 1: with the sudo rule gone, stat and status both fail; that is not
    "nothing signed in". The logout exits nonzero with the real error and changes nothing."""
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 0
    capsys.readouterr()
    rig.broken = True
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    out = capsys.readouterr().out
    assert "a password is required" in out and "nothing signed in" not in out
    assert (rig.hands_home / ".claude" / ".credentials.json").exists()
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "logged-in"   # a refusal records nothing


def test_a_login_outside_the_file_whose_logout_fails_is_not_signed_out(rig, capsys, tmp_path, monkeypatch):
    """V2 codex HIGH 2: status says signed in, no credential file (a non-file store), the vendor logout fails; the
    `rm -f` of the absent file is not a sign-out."""
    removed: list[str] = []
    real_remove = hands_login.remove_argv
    monkeypatch.setattr(hands_login, "remove_argv", lambda h, v: removed.append(v) or real_remove(h, v))
    rig.knobs["SHIM_KEYSTORE"] = str(tmp_path / "keystore")
    assert hands_login.cmd_hands_login(rig.entity, "claude") == 0
    assert "the vendor kept this login somewhere else" in capsys.readouterr().out
    rig.knobs["SHIM_LOGOUT_FAIL"] = "1"
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    out = capsys.readouterr().out
    assert "not confirmed signed out" in out and "keychain locked" in out and "signed out (" not in out
    assert removed == []                                                 # no fallback without the file
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "failed"


def test_an_ineffective_successful_logout_is_not_signed_out(rig, capsys, tmp_path):
    """V2 codex HIGH 2, the exit-0 half: the vendor says it logged out, but its status still says signed in."""
    rig.knobs["SHIM_KEYSTORE"] = str(tmp_path / "keystore")
    hands_login.cmd_hands_login(rig.entity, "claude")
    rig.knobs["SHIM_LOGOUT_NOOP"] = "1"
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    assert "not confirmed signed out" in capsys.readouterr().out


def test_a_login_outside_the_file_logs_out_when_the_vendor_does(rig, capsys, tmp_path):
    rig.knobs["SHIM_KEYSTORE"] = str(tmp_path / "keystore")
    hands_login.cmd_hands_login(rig.entity, "claude")
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 0
    assert "signed out (logged-out)" in capsys.readouterr().out
    assert not (tmp_path / "keystore").exists()


@pytest.mark.parametrize("vendor,says", [
    ("claude", "could not determine whether logged out: keychain locked"),
    ("codex", "Error checking login status: could not determine whether logged out: keychain locked"),
])
def test_a_failed_status_that_mentions_logged_out_refuses_the_logout(rig, capsys, tmp_path, vendor, says):
    """L3 r2 of V1/V2 (codex HIGH): a login outside the file (a non-file store), then a status that FAILS with a
    diagnostic merely containing "logged out". That parsed as signed out, so the logout answered "nothing signed in"
    and left the login active. It is unknown: the logout refuses, changes nothing and records nothing."""
    keystore = tmp_path / "keystore"
    rig.knobs["SHIM_KEYSTORE"] = str(keystore)
    assert hands_login.cmd_hands_login(rig.entity, vendor) == 0
    capsys.readouterr()
    rig.knobs["SHIM_STATUS_SAYS"] = says
    rig.log.write_text("")
    assert hands_login.cmd_hands_logout(rig.entity, vendor) == 1
    out = capsys.readouterr().out
    assert "nothing signed in" not in out and "cannot tell whether" in out and "keychain locked" in out
    assert keystore.exists()
    assert not any(c.startswith("SWEEP") or "logout" in c for c in rig.calls())
    assert hands_login.read_outcomes(rig.entity)[vendor]["outcome"] == "logged-in"


def test_the_no_op_check_is_inside_the_lock(rig, capsys):
    """V2 codex HIGH 3 / complement LOW 2: with nothing signed in but a session holding the lock, the logout is
    refused instead of answering "nothing" from outside it."""
    fd = hands_live.take_exclusive(rig.entity)
    try:
        assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    finally:
        os.close(fd)
    out = capsys.readouterr().out
    assert "a session of this entity is open" in out and "nothing signed in" not in out
    assert rig.calls() == []


def test_a_logout_that_runs_out_is_swept_before_the_lock_is_released(rig, capsys, monkeypatch):
    """V2 codex MED 4: the vendor logout's descendant outlives the killed sudo; the sweep runs while the lock is
    still held, and the verb fails."""
    hands_login.cmd_hands_login(rig.entity, "claude")
    capsys.readouterr()
    monkeypatch.setattr(hands_login, "STEP_TIMEOUT", 1.0)
    rig.knobs["SHIM_LOGOUT_HANG"] = "1"
    rig.log.write_text("")
    t0 = time.monotonic()
    rc = hands_login.cmd_hands_logout(rig.entity, "claude")
    assert rc == 1 and time.monotonic() - t0 < 5.0
    calls = rig.calls()
    sweeps = [i for i, c in enumerate(calls) if c.startswith("SWEEP")]
    assert len(sweeps) == 2 and sweeps[1] > calls.index("auth logout")
    assert calls[sweeps[1]].endswith("lock-held=True")
    assert "did not finish" in capsys.readouterr().out
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "failed"
    pid = int(rig.pids.read_text().split()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_refusal_does_not_overwrite_the_last_real_outcome(rig, capsys):
    """V2 complement LOW 4: "a session is open" attempted nothing."""
    hands_login.cmd_hands_login(rig.entity, "claude")
    assert hands_login.cmd_hands_logout(rig.entity, "claude") == 0
    hands_login.cmd_hands_login(rig.entity, "claude")
    fd = hands_live.take_exclusive(rig.entity)
    try:
        assert hands_login.cmd_hands_logout(rig.entity, "claude") == 1
    finally:
        os.close(fd)
    assert hands_login.read_outcomes(rig.entity)["claude"]["outcome"] == "logged-in"


def test_outcomes_for_two_vendors_both_survive(rig):
    hands_login.record_outcome(rig.entity, "claude", "logged-in")
    hands_login.record_outcome(rig.entity, "codex", "logged-out")
    got = hands_login.read_outcomes(rig.entity)
    assert got["claude"]["outcome"] == "logged-in" and got["codex"]["outcome"] == "logged-out"
    assert sorted(p.name for p in (rig.entity / ".levain").iterdir() if "hands-login" in p.name) == \
        ["hands-login.json", "hands-login.json.lock"]


# --- the doctor row ---------------------------------------------------------------------------------------------

def _rows(entity):
    return {r.name: r for r in doctor._check_hands_login(entity)}


def test_doctor_row_is_quiet_without_a_hands_account(tmp_path):
    assert doctor._check_hands_login(tmp_path) == []


def test_doctor_row_states(rig, monkeypatch):
    real = hands_login.credential_state

    def as_hands(hands, vendor, **kw):      # the shims run as this user; on a real host the file is the hands user's
        got = real(hands, vendor, **kw)
        return None if got is None else f"{hands.user} {got.split()[-1]}"

    monkeypatch.setattr(hands_login, "credential_state", as_hands)
    rows = _rows(rig.entity)
    assert set(rows) == {"hands login claude", "hands login codex"}
    r = rows["hands login claude"]
    assert r.ok and r.neutral and "no credential file" in r.detail and "last: none recorded" in r.detail
    hands_login.cmd_hands_login(rig.entity, "claude")
    r = _rows(rig.entity)["hands login claude"]
    assert r.ok and not r.warn and not r.neutral
    assert "credential file present, not verified" in r.detail and "signed in" not in r.detail
    assert "owner levain-hands, mode 600" in r.detail
    assert "last: logged-in" in r.detail
    os.chmod(rig.hands_home / ".claude" / ".credentials.json", 0o644)
    r = _rows(rig.entity)["hands login claude"]
    assert r.warn and "mode 644" in r.detail and "levain hands logout claude" in r.hint
    hands_login.cmd_hands_logout(rig.entity, "claude")
    r = _rows(rig.entity)["hands login claude"]
    assert r.neutral and "last: logged-out" in r.detail
    assert _rows(rig.entity)["hands login codex"].neutral


def test_doctor_row_owner_other_than_hands_warns(rig, monkeypatch):
    monkeypatch.setattr(hands_login, "credential_state", lambda hands, v, **kw: "someone-else 600")
    r = _rows(rig.entity)["hands login claude"]
    assert r.warn and "owner someone-else" in r.detail


def test_doctor_row_that_cannot_read_the_file_warns_unknown(rig):
    """V2 codex MED 5: a failed read (here: the sudo rule gone) is a warning, never OK and never "not signed in"."""
    hands_login.cmd_hands_login(rig.entity, "claude")
    rig.broken = True
    r = _rows(rig.entity)["hands login claude"]
    assert r.ok and r.warn and not r.neutral
    assert r.detail.startswith("unknown") and "a password is required" in r.detail


def test_doctor_row_exception_is_a_warning(rig, monkeypatch):
    """V2 codex MED 5: an exception was "[OK] not determinable"."""
    def boom(hands, v, **kw):
        raise OSError("boom")
    monkeypatch.setattr(hands_login, "credential_state", boom)
    r = _rows(rig.entity)["hands login claude"]
    assert r.warn and "not determinable" in r.detail
