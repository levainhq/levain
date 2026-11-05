"""identseed (levain 38, key 1105-21): np-8645041e (a), signed by Phill 11-05. Claude Code's ``auth status`` reads
the account (email, orgId, orgName) from ``oauthAccount`` in the session's ``.claude.json``, not from the credential
(RETURN_c1p addendum), and every session after the login renders a fresh ``.claude.json``. So a ``login`` session's
END parks a WHITELIST of that block beside the credential in ``L/slots/e<n>/keep/``, and render seeds it into each
gated session's ``.claude.json``; start's gate refuses ``identity-seed-missing:<tag>`` (log this slot in again) when
no record is parked.

Two harnesses: the 2f start world (prepare, render, start, confirm, teardown through the helper's ``main``) for the
round trip and the gate, and the 2c END world for the park's own cases."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import end as E
from levain.slot import identity_child as IC
from levain.slot import render as RD
from levain.slot import start as SS
from levain.slot.end import RETAINED, REVOKED, end_session
from levain.slot.ids import SlotId
from tests.test_slot_s2c_end import Control
from tests.test_slot_idg2_relogin import confirm as confirm_end, login_world
from tests.test_slot_s2f2_runtime_helper import _blob, _prepared
from tests.test_slot_s2f3_start import _S, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

E0 = SlotId("egress", 0)
PASS = ["claude-sub", "github"]
ORG = "0b4c4a52-7f0e-4c39-9b8e-1d2c3e4f5a6b"
EMAIL = "operator@example.test"
ORG2 = "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b1a"
EMAIL2 = "someone.else@example.test"
#: values that must never leave the login's own file: a non-whitelisted key of the block, a token-shaped one inside
#: the block, and a token-shaped key at the file's top level.
CANARY = "CANARY-display-name-7f3a"
TOKEN = "sk-ant-oat01-CANARY-TOKEN-9b1c"
APIKEY = "sk-ant-api03-CANARY-KEY-44de"
CRED_BYTES = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-CRED", "scopes": ["user:inference"]}}).encode()


def login_config(org: str = ORG, email: str = EMAIL, **extra) -> dict:
    """A login session's ``.claude.json`` as the vendor leaves it: the account block with whitelisted and other keys,
    plus unrelated top-level keys (one token-shaped)."""
    block = {"accountUuid": "11111111-2222-4333-8444-555555555555", "emailAddress": email, "organizationUuid": org,
             "organizationName": "Example Org", "displayName": CANARY, "accessToken": TOKEN, "billingType": "x"}
    block.update(extra)
    return {"hasCompletedOnboarding": True, "userID": "u" * 64, "primaryApiKey": APIKEY, "oauthAccount": block}


def fake_auth_status(cfg_dir: Path):
    """A stand-in for ``claude auth status --json`` that reads what 2.1.318 reads (identseed read its code):
    ``loggedIn`` from the credential's access token, ``email`` / ``orgId`` / ``orgName`` from ``oauthAccount``'s
    ``emailAddress`` / ``organizationUuid`` / ``organizationName`` in ``.claude.json``, null when absent. Returns a
    :data:`levain.slot.identity_child.Runner`."""
    def runner(argv, env, timeout_s):
        if argv[-1] == "--version":
            return 0, b"2.1.318 (Claude Code)\n"
        try:
            cred = json.loads((cfg_dir / ".credentials.json").read_bytes())
            logged = bool(cred.get("claudeAiOauth", {}).get("accessToken"))
        except (OSError, ValueError):
            logged = False
        try:
            acct = json.loads((cfg_dir / ".claude.json").read_bytes()).get("oauthAccount") or {}
        except (OSError, ValueError):
            acct = {}
        out = {"loggedIn": logged, "authMethod": "claude.ai" if logged else "none"}
        if logged:
            out.update(email=acct.get("emailAddress"), orgId=acct.get("organizationUuid"),
                       orgName=acct.get("organizationName"))
        return (0 if logged else 1), json.dumps(out).encode()
    return runner


def child_capture(w, la):
    """start's capture of the identity target answered by the REAL identity child (``identity_child.fetch``) over the
    fake binary above, reading the current session's own config dir; every other target unchanged."""
    real = la.capture

    def cap(argv, spec_json, cgroup, bound):
        if canon.parse(spec_json)["target"] != "identity":
            return real(argv, spec_json, cgroup, bound)
        cfg = _S(w, w.session) / "config" / "claude"
        ans = IC.fetch({}, runner=fake_auth_status(cfg), binary="claude")
        line = json.dumps(ans, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        return (0 if ans.get("logged_in") is True else 1), line
    w.seams.capture = cap


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(SS, "slot_owner_ok", lambda st: True)
    w, la, table = _world(tmp_path)
    w.la = la
    # the harness never chowns to the slot: the slot's config file is the test user's here
    w.seams.end_overrides = {**w.seams.end_overrides, "seed_owner_ok": lambda st: st.st_uid == os.getuid()}
    child_capture(w, la)
    return w, la


def _keep(w) -> Path:
    return E0.slot_dir(w.L) / "keep"


def _session(w, kind: str) -> str:
    s = _prepared(w, "egress", kind, "claude", "passthrough")
    w.session = s
    line = w.main(["render", s], _blob(vendor="claude", routes=PASS))[1]
    assert line["ok"], line
    return s


def _login_and_confirm(w, la, cfg: dict | None) -> str:
    """A login session: the vendor's sign-in written into the session tree (the fake vendor writes nothing), start
    records the observed id through the real child, ``confirm-identity yes``, then teardown (END parks)."""
    s = _session(w, "login")
    d = _S(w, s) / "config" / "claude"
    (d / ".credentials.json").write_bytes(CRED_BYTES)
    os.chmod(d / ".credentials.json", 0o600)
    (d / ".claude.json").unlink()
    (d / ".claude.json").write_text(json.dumps(cfg if cfg is not None else {"hasCompletedOnboarding": True}))
    la.codes["vendor"] = 0
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True, "baseline": "written"}
    assert w.main(["teardown", s])[1] == {"ok": True, "state": "RETAINED"}
    return s


def _seed(w) -> dict:
    return canon.parse((_keep(w) / E.IDENTITY_SEED_NAME).read_bytes())


# --- (1) the round trip --------------------------------------------------------------------------------------------

def test_round_trip_login_parks_the_block_and_a_later_session_passes_the_gate(env):
    w, la = env
    login = _login_and_confirm(w, la, login_config())
    baseline = canon.parse((E0.slot_dir(w.L) / "baseline").read_bytes())
    assert baseline["observed_id"] == IC.id_digest(ORG, EMAIL)          # the login's own read, block present
    seed = _seed(w)
    assert seed == {"format": E.IDENTITY_SEED_FORMAT, "vendor": "claude", "session": login,
                    "account": {"emailAddress": EMAIL, "organizationName": "Example Org", "organizationUuid": ORG}}
    assert (_keep(w) / ".credentials.json").read_bytes() == CRED_BYTES  # the credential parked beside it

    s = _session(w, "interactive")
    rendered = json.loads((_S(w, s) / "config" / "claude" / ".claude.json").read_bytes())
    assert rendered["oauthAccount"] == seed["account"]
    assert rendered["hasCompletedOnboarding"] is True and f"{RD.STORE['linux']}/live/{s}/repo" in rendered["projects"]
    assert w.main(["start", s])[1] == {"ok": True, "exit": 0}
    assert w.state(s).fields["identity"] == {"observed_id": baseline["observed_id"], "result": "matched"}


def test_without_the_seed_the_same_session_is_refused_bad_shape(env):
    """The measured gap, reproduced: the same round trip with the render's seed taken out answers ``bad-shape``."""
    w, la = env
    _login_and_confirm(w, la, login_config())
    s = _session(w, "interactive")
    cfg = _S(w, s) / "config" / "claude" / ".claude.json"
    obj = json.loads(cfg.read_bytes())
    del obj["oauthAccount"]
    cfg.write_text(json.dumps(obj))
    line = w.main(["start", s])[1]
    assert line["refused"] == "identity-unparsable", line
    assert (_keep(w) / ".credentials.json").exists()                     # parked back


# --- (2) no parked block: the named refusal --------------------------------------------------------------------------

def test_a_login_with_no_block_parks_no_seed_and_the_next_session_refuses_by_name(env):
    w, la = env
    s0 = _session(w, "login")
    d = _S(w, s0) / "config" / "claude"
    (d / ".credentials.json").write_bytes(CRED_BYTES)
    la.codes["vendor"] = 0
    assert w.main(["start", s0])[1]["ok"]          # the login's own read answers bad-shape: no observed id, no yes
    assert w.main(["teardown", s0])[1]["ok"]
    assert (_keep(w) / ".credentials.json").exists() and not (_keep(w) / E.IDENTITY_SEED_NAME).exists()
    w.give_credential(E0, "claude", seed=False)    # a baseline, as an old (pre-identseed) confirmed login left it
    s = _session(w, "interactive")
    assert "oauthAccount" not in json.loads((_S(w, s) / "config" / "claude" / ".claude.json").read_bytes())
    la.spawns.clear()
    code, line = w.main(["start", s])
    assert line["refused"] == "identity-seed-missing:e0", line
    assert (_keep(w) / ".credentials.json").exists()                     # nothing moved
    assert not (_S(w, s) / "config" / "claude" / ".credentials.json").exists()
    assert la.spawns == [] and "identity" not in w.state(s).fields


def test_an_old_parked_login_refuses_identity_seed_missing_before_any_move(env):
    w, la = env
    w.give_credential(E0, "claude", seed=False)
    s = _session(w, "interactive")
    assert w.main(["start", s])[1]["refused"] == "identity-seed-missing:e0"
    assert (_keep(w) / ".credentials.json").read_bytes() == b"{}"
    assert la.spawns == []


# --- (3) only whitelisted keys, ever ---------------------------------------------------------------------------------

def _bytes_under(*roots: Path) -> bytes:
    out = b""
    for root in roots:
        for dp, _, fns in os.walk(root):
            for fn in fns:
                p = Path(dp) / fn
                if not p.is_symlink():
                    out += p.read_bytes()
    return out


def test_no_other_key_reaches_the_parked_record_or_a_later_session(env):
    w, la = env
    login = _login_and_confirm(w, la, login_config())
    s = _session(w, "interactive")
    assert w.main(["start", s])[1]["ok"]
    seed = _seed(w)
    assert sorted(seed["account"]) == sorted(E.IDENTITY_SEED_KEYS)
    later = _bytes_under(_keep(w), w.L / "live" / s)
    for canary in (CANARY, TOKEN, APIKEY, "accountUuid", "billingType", "displayName", "primaryApiKey", "userID"):
        assert canary.encode() not in later, canary
    # the login's own retained tree still holds what the vendor wrote (END retains it as it always did)
    assert CANARY.encode() in _bytes_under(w.L / "retained" / login)


def test_render_seeds_only_whitelisted_keys_whatever_facts_carry():
    facts = RD.RenderFacts(host="linux", session="s-0123456789abcdef", slot=E0, kind="interactive", vendor="claude",
                           auth="passthrough", slot_uid=899, slot_gid=899,
                           oauth_account={"emailAddress": EMAIL, "organizationUuid": ORG, "accessToken": TOKEN})
    assert RD.render_cc_onboarding(facts)["oauthAccount"] == {"emailAddress": EMAIL, "organizationUuid": ORG}
    assert "oauthAccount" not in RD.render_cc_onboarding(replace(facts, oauth_account=None))


def test_check_seed_block_keeps_only_the_whitelist_and_its_shapes():
    assert E.check_seed_block(login_config()["oauthAccount"]) == {
        "emailAddress": EMAIL, "organizationName": "Example Org", "organizationUuid": ORG}
    assert E.check_seed_block({"emailAddress": EMAIL, "organizationUuid": ORG}) == {
        "emailAddress": EMAIL, "organizationUuid": ORG}
    for bad in ({"emailAddress": EMAIL, "organizationUuid": "not-a-uuid"},
                {"emailAddress": "no-at-sign", "organizationUuid": ORG},
                {"emailAddress": " " + EMAIL, "organizationUuid": ORG},
                {"emailAddress": EMAIL},
                {"emailAddress": EMAIL, "organizationUuid": ORG, "organizationName": 7},
                {"emailAddress": EMAIL, "organizationUuid": ORG, "organizationName": "x" * 257},
                "a string", None):
        assert E.check_seed_block(bad) is None, bad


def test_a_parked_record_with_an_extra_key_is_refused(env):
    w, la = env
    w.give_credential(E0, "claude")
    p = _keep(w) / E.IDENTITY_SEED_NAME
    obj = canon.parse(p.read_bytes())
    obj["account"]["accessToken"] = TOKEN
    p.write_bytes(canon.canonical_bytes(obj))
    s = _prepared(w, "egress", "interactive", "claude", "passthrough")
    w.session = s
    assert w.main(["render", s], _blob(vendor="claude", routes=PASS))[1]["refused"] == "bad-identity-seed"


def test_the_two_whitelists_are_one():
    assert tuple(sorted(RD.SEED_KEYS)) == tuple(sorted(E.IDENTITY_SEED_KEYS))
    assert tuple(RD.SEEDED_VENDORS) == tuple(E.IDENTITY_SEED_SOURCE)


# --- (4) root's record; the slot cannot write it ---------------------------------------------------------------------

def test_the_record_is_root_0600_in_root_0700_keep(env):
    w, la = env
    _login_and_confirm(w, la, login_config())
    p = _keep(w) / E.IDENTITY_SEED_NAME
    st = os.lstat(p)
    assert st.st_mode & 0o7777 == 0o600 and st.st_uid == os.getuid()     # root, in this harness: the test user
    assert os.lstat(_keep(w)).st_mode & 0o777 == 0o700
    assert sorted(os.listdir(_keep(w))) == [".credentials.json", E.IDENTITY_SEED_NAME]   # no temp left


@pytest.mark.parametrize("damage", ["world-writable", "symlink", "directory", "oversize", "not-json", "codex"])
def test_a_record_not_roots_own_is_refused_at_render(env, damage):
    w, la = env
    w.give_credential(E0, "claude")
    p = _keep(w) / E.IDENTITY_SEED_NAME
    if damage == "world-writable":
        p.chmod(0o666)                      # the harness's trusted() refuses a group- or world-writable file
    elif damage == "symlink":
        real = p.with_name("elsewhere.json")
        p.rename(real)
        os.symlink(real, p)
    elif damage == "directory":
        p.unlink()
        p.mkdir()
    elif damage == "oversize":
        p.write_bytes(b" " * (E.IDENTITY_SEED_MAX_BYTES + 1))
    elif damage == "not-json":
        p.write_bytes(b"{nope")
    elif damage == "codex":
        obj = canon.parse(p.read_bytes())
        obj["vendor"] = "codex"
        p.write_bytes(canon.canonical_bytes(obj))
    s = _prepared(w, "egress", "interactive", "claude", "passthrough")
    w.session = s
    assert w.main(["render", s], _blob(vendor="claude", routes=PASS))[1]["refused"] == "bad-identity-seed"


def test_a_login_session_render_carries_no_block(env):
    """The login writes its own block; render never pre-seeds a login (nor a Codex session)."""
    w, la = env
    w.give_credential(E0, "claude")
    s = _session(w, "login")
    assert "oauthAccount" not in json.loads((_S(w, s) / "config" / "claude" / ".claude.json").read_bytes())


# --- the park itself (2c END world) ----------------------------------------------------------------------------------

def _end_login(tmp_path, cfg, *, parked=None, kind="login", seed: bytes | None = None, yes=False, **seams_kw):
    w = login_world(tmp_path, kind=kind, parked=parked)
    if cfg is not None:
        (w.S / "config" / "claude" / ".claude.json").write_text(json.dumps(cfg))
    if seed is not None:
        (w.facts.keep / E.IDENTITY_SEED_NAME).write_bytes(seed)
        os.chmod(w.facts.keep / E.IDENTITY_SEED_NAME, 0o600)
    if yes:
        confirm_end(w)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, **seams_kw), record_state=REVOKED) == RETAINED
    return w


def _old_seed() -> bytes:
    return E.identity_seed_bytes(vendor="claude", session="s-fedcba9876543210",
                                 account={"emailAddress": EMAIL2, "organizationUuid": ORG2})


def _seed_at(w):
    p = w.facts.keep / E.IDENTITY_SEED_NAME
    return canon.parse(p.read_bytes())["account"] if p.exists() else None


def test_park_first_login_writes_the_whitelisted_record(tmp_path):
    w = _end_login(tmp_path, login_config())
    assert _seed_at(w) == {"emailAddress": EMAIL, "organizationName": "Example Org", "organizationUuid": ORG}


def test_park_confirmed_relogin_replaces_the_record_with_the_new_logins(tmp_path):
    w = _end_login(tmp_path, login_config(), parked=b"{}", seed=_old_seed(), yes=True)
    assert _seed_at(w)["emailAddress"] == EMAIL


def test_park_unconfirmed_relogin_leaves_keeps_record_with_keeps_credential(tmp_path):
    w = _end_login(tmp_path, login_config(), parked=b"{}", seed=_old_seed())
    assert _seed_at(w)["emailAddress"] == EMAIL2 and (w.facts.keep / ".credentials.json").read_bytes() == b"{}"


def test_park_of_another_kind_never_touches_the_record(tmp_path):
    """An interactive session's END parks its credential back; the block in ITS config (the seeded one, or anything
    the vendor or the slot wrote there) never becomes the record."""
    w = _end_login(tmp_path, login_config(org=ORG2, email=EMAIL2), kind="interactive", seed=E.identity_seed_bytes(
        vendor="claude", session="s-fedcba9876543210", account={"emailAddress": EMAIL, "organizationUuid": ORG}))
    assert _seed_at(w) == {"emailAddress": EMAIL, "organizationUuid": ORG}


def test_a_login_with_no_usable_block_drops_the_old_record(tmp_path):
    w = _end_login(tmp_path, {"hasCompletedOnboarding": True}, parked=b"{}", seed=_old_seed(), yes=True)
    assert _seed_at(w) is None and (w.facts.keep / ".credentials.json").exists()


@pytest.mark.parametrize("shape", ["symlink", "hardlink", "other-owner", "oversize", "not-json", "bad-uuid"])
def test_a_source_file_off_shape_is_never_read_into_a_record(tmp_path, shape):
    w = login_world(tmp_path, parked=None)
    cfg = w.S / "config" / "claude" / ".claude.json"
    body = json.dumps(login_config())
    owner = None
    if shape == "symlink":
        real = tmp_path / "planted.json"
        real.write_text(body)
        os.symlink(real, cfg)
    elif shape == "hardlink":
        real = tmp_path / "planted.json"
        real.write_text(body)
        os.link(real, cfg)
    elif shape == "other-owner":
        cfg.write_text(body)
        owner = lambda st: False   # noqa: E731 - as if the file's owner were not the slot
    elif shape == "oversize":
        cfg.write_text(json.dumps({**login_config(), "pad": "x" * E.IDENTITY_SEED_SOURCE_MAX}))
    elif shape == "not-json":
        cfg.write_text(body[:-3])
    elif shape == "bad-uuid":
        cfg.write_text(json.dumps(login_config(org="not-a-uuid")))
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, seed_owner_ok=owner) if owner else w.seams(ctl),
                       record_state=REVOKED) == RETAINED
    assert _seed_at(w) is None and (w.facts.keep / ".credentials.json").exists()


def test_seed_from_tree_takes_the_slot_uid_by_default(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / ".claude.json").write_text(json.dumps(login_config()))
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert E.seed_from_tree(fd, b".claude.json", "oauthAccount", os.getuid())["organizationUuid"] == ORG
        assert E.seed_from_tree(fd, b".claude.json", "oauthAccount", os.getuid() + 1) is None
    finally:
        os.close(fd)


@pytest.mark.parametrize("point", ["park:seed:written", "park:keep:renamed"])
def test_a_crash_around_the_record_converges(tmp_path, point):
    """A death after the record and before (or right after) the credential's rename; the redo ends with both."""
    w = login_world(tmp_path, parked=None)
    (w.S / "config" / "claude" / ".claude.json").write_text(json.dumps(login_config()))
    ctl = Control()

    def fault(p):
        if p == point:
            raise OSError(5, "injected")
    first = end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
    assert first != RETAINED
    assert _seed_at(w)["organizationUuid"] == ORG
    assert end_session(w.facts, w.seams(ctl), record_state=ctl.state, failed_step=ctl.failed_step) == RETAINED
    assert _seed_at(w)["organizationUuid"] == ORG and (w.facts.keep / ".credentials.json").exists()
    assert sorted(os.listdir(w.facts.keep)) == [".credentials.json", E.IDENTITY_SEED_NAME]
