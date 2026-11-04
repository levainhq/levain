"""The doctor rows for the hands account's own MCP servers (levain 15 M4): the registry against what the account
holds, the credential file's owner and mode, the last outcome. A fault in any read is its own failing row, never
silence (an absent signal is not health). The rig is tests/test_hands_mcp_verbs.py's (shim vendor CLI, fake OAuth
server, the install's own cockpit root)."""
from __future__ import annotations

import json
import sys

import pytest

from levain import doctor
from levain.firing import hands_login, hands_mcp_login as hml
from tests.test_hands_mcp_verbs import McpRig, rig  # noqa: F401  (the fixture)

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")


@pytest.fixture
def as_hands(monkeypatch):
    """The stand-in runs as the test's own user, so the file's owner reads as that user; this makes it read as the hands
    account (cross-user ownership is the macOS VM run)."""
    real = hands_login.credential_state

    def credential_state(hands, vendor, **kw):
        got = real(hands, vendor, **kw)
        return None if got is None else hands.user + " " + got.split(" ", 1)[1]
    monkeypatch.setattr(hands_login, "credential_state", credential_state)


def mcp(r):
    return [x for x in doctor._check_hands_mcp(r.entity) if x.name == "hands mcp"]


def tokens(r):
    return next(x for x in doctor._check_hands_mcp(r.entity) if x.name == "hands mcp tokens")


def test_silent_where_the_entity_has_no_hands_account(rig, monkeypatch):
    def none(e, cfg=None):
        raise hands_login.HandsRefused(hands_login.NO_HANDS)
    monkeypatch.setattr(hands_login, "load_hands_identity", none)
    assert doctor._check_hands_mcp(rig.entity) == []


def test_a_hands_account_with_nothing_registered_says_so_and_passes(rig):
    out = doctor._check_hands_mcp(rig.entity)
    assert [x.name for x in out] == ["hands mcp", "hands mcp tokens"]
    assert out[0].ok and "no hands MCP server" in out[0].detail
    assert out[1].ok and "no credential file" in out[1].detail


def test_a_registered_server_not_signed_in_is_ok_and_said(rig):
    rig.register()
    r = mcp(rig)
    assert len(r) == 1 and r[0].ok and "registered, not signed in" in r[0].detail


def test_a_signed_in_server_in_step_reports_its_last_outcome_and_a_0600_token_file(rig, as_hands):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    r = mcp(rig)
    assert len(r) == 1 and r[0].ok and "in step with the registry" in r[0].detail and "login-exited-0" in r[0].detail
    t = tokens(rig)
    assert t.ok and "mode 600" in t.detail


def test_the_row_runs_inside_the_doctor_run(rig, monkeypatch, capsys):
    rig.register()
    rows_ = doctor._guarded("hands mcp", lambda: doctor._check_hands_mcp(rig.entity))
    assert [x.name for x in rows_][-1] == "hands mcp tokens" and all(x.ok for x in rows_)


# --- drift ---------------------------------------------------------------------------------------------------------

def test_a_recorded_server_missing_from_the_accounts_config_fails_as_drift(rig):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    rig.cfg.write_text(json.dumps({"mcpServers": {}}))
    r = mcp(rig)
    assert len(r) == 1 and not r[0].ok and "DRIFT" in r[0].detail and "missing from the hands account's config" in r[0].detail


def test_a_server_the_account_holds_but_signin_did_not_add_fails_as_drift(rig):
    rig.cfg.parent.mkdir(parents=True)
    rig.cfg.write_text(json.dumps({"mcpServers": {"hand-made": {"type": "http", "url": "https://x.example/mcp"}}}))
    r = mcp(rig)
    assert len(r) == 1 and not r[0].ok and "hand-made" in r[0].detail and "not added by sign-in" in r[0].detail


def test_a_signed_in_server_whose_registration_lapsed_fails_and_names_the_remove(rig, monkeypatch):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    monkeypatch.setattr(hml, "load_registry", lambda root, now=None: ([], [("hm-x", "expired")]))
    r = mcp(rig)
    assert any(not x.ok and "no longer registered or live" in x.detail and "hands mcp remove linear" in x.detail for x in r)


def test_a_url_changed_under_the_record_fails_as_drift(rig):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    rig.cfg.write_text(json.dumps({"mcpServers": {"linear": {"type": "http", "url": "https://moved.example/mcp"}}}))
    r = mcp(rig)
    assert len(r) == 1 and not r[0].ok and "url is not the one signed in" in r[0].detail


# --- the token file ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_token_file_wider_than_0600_fails(rig, as_hands, mode):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    rig.cred.chmod(mode)
    t = tokens(rig)
    assert not t.ok and "wider than 600" in t.detail and "remove" in (t.hint or "")


def test_a_token_file_owned_by_another_account_fails_even_at_0600(rig):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    t = tokens(rig)                                   # unwrapped: the file is the test user's, the hands account is levain-hands
    assert not t.ok and "mode 600" in t.detail and "not the hands account's" in t.detail


def test_a_token_file_at_0400_is_not_wider(rig, as_hands):
    rig.register()
    assert hml.cmd_signin(rig.entity, "linear", rig.ck.root) == 0
    rig.cred.chmod(0o400)
    assert tokens(rig).ok


def test_an_unreadable_token_file_state_is_a_warning_row_never_ok_silence(rig, monkeypatch):
    def boom(*a, **k):
        raise hands_login.HandsLoginError("stat refused")
    monkeypatch.setattr(hands_login, "credential_state", boom)
    t = tokens(rig)
    assert t.ok and t.warn and "unknown" in t.detail and "stat refused" in t.detail


# --- a fault in reading is a visible row -----------------------------------------------------------------------------

def test_an_unreadable_registry_is_a_failing_row_not_an_empty_registry(rig):
    rig.register()
    (rig.ck.root / "cockpit.db").write_bytes(b"garbage")
    r = mcp(rig)
    assert any(not x.ok and "registry: unknown" in x.detail for x in r)


def test_an_unreadable_record_is_a_failing_row(rig):
    rig.register()
    hml._record_path(rig.entity).write_text("{nope")
    assert any(not x.ok and "record: unknown" in x.detail for x in mcp(rig))


def test_a_config_the_account_cannot_be_read_from_is_a_failing_row(rig):
    rig.register()
    rig.cfg.parent.mkdir(parents=True)
    rig.cfg.write_text("{broken")
    r = mcp(rig)
    assert any(not x.ok and "config: unknown" in x.detail for x in r)


def test_a_sudo_rule_that_fails_is_a_failing_row_and_not_ok(rig):
    rig.register()
    rig.broken = True
    r = mcp(rig)
    assert any(not x.ok and "unknown" in x.detail for x in r)


def test_a_builder_that_raises_is_its_own_failure_row(rig, monkeypatch):
    monkeypatch.setattr(hml, "doctor_rows", lambda install: (_ for _ in ()).throw(RuntimeError("defect")))
    out = doctor._guarded("hands mcp", lambda: doctor._check_hands_mcp(rig.entity))
    assert len(out) == 1 and not out[0].ok and "could not be built" in out[0].detail


def test_the_doctor_run_calls_the_row():
    import inspect
    assert '_guarded("hands mcp"' in inspect.getsource(doctor._run_doctor)
