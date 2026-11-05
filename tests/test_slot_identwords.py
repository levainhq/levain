"""identword (levain 39, key 1105-24; Phill 11-05 on np-5b605b42, "(a) keep the identity seeding, word it as a
recorded-account LABEL, not a check"): the account the identity step compares is the one the slot's login recorded. It
does not tell which account the credential belongs to, and it does not see an expired login. So no sentence an operator
reads about the identity result may say Levain verified, confirmed or matched the account.

The strings are built by the real code paths (the supervisor's confirm prompt and its three refusal lines, start's two
gate refusals, the launcher's help), never a hand-written copy: change one back to a check-style claim and this goes
red.
"""
from __future__ import annotations

import re

import pytest

from levain.slot import Refused
from levain.slot import launch as L
from levain.slot import start as SS
from tests.test_slot_g3_gate import E0, OTHER, PASS, UUID, _blob, _gated, _rendered, _world
from tests.test_slot_idg1fx_identity import _Sup
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

S = "s-0000000000000001"


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """The g3 gate world (tests/test_slot_g3_gate.py ``env``)."""
    monkeypatch.setattr(SS, "slot_owner_ok", lambda st: True)
    w, la, table = _world(tmp_path)
    w.la = la
    return w, la, table

#: wording that says or implies Levain checked which account the login belongs to, or saw it expire.
CHECK_CLAIMS = re.compile(
    r"\bverified\b|\bconfirms? which account|\bconfirmed\b|\bmatch(?:es|ed)\b|server's answer"
    r"|looks expired|refreshed and checked|after the identity check"
    # identfix F-D2: a contrast that tells the reader the other vendor has a check, and a host label naming one
    r"|server-(?:checked|validated)|login check",
    re.IGNORECASE)


def _route_refusal_detail() -> str:
    """The live ``passthrough-route-lacks-token-host`` detail naming both host labels."""
    from levain.slot import supervisor as SV
    routes = [{"route": "claude-sub", "type": "subscription_passthrough", "hosts": ["claude.ai"]}]
    with pytest.raises(Refused) as e:
        SV.pick_routes(routes, "claude", "passthrough")
    assert e.value.name == "passthrough-route-lacks-token-host"
    return e.value.detail


def _said_by_supervisor() -> list[str]:
    from levain.slot import supervisor as SV
    out: list[str] = [SV.CODEX_NO_ACCOUNT_SAID, _route_refusal_detail()]
    for vendor, ident in (("claude", {"observed_id": UUID}), ("codex", {"observed_id": "not-a-check"})):
        sup = _Sup(vendor, True, ident)
        sup._confirm_identity(S)
        out += sup.prompts + sup.said
    for result, oid, call in (("identity-changed", UUID, "_identity_refused"),
                              ("identity-changed", None, "_identity_refused"),
                              ("identity-expired", None, "_identity_expired"),
                              ("identity-unreachable", None, "_identity_unreachable")):
        sup = _Sup("claude", True)
        sup._slot_index = 3
        sup.replies["status"] = {"records": [], "identity": {**({"observed_id": oid} if oid else {}),
                                                              "result": result}}
        getattr(sup, call)(S)
        out += sup.said
    return out


def _refusals_from_the_gate(w, la) -> list[str]:
    out: list[str] = []
    s = _gated(w, la, profile=OTHER)                       # a sign-in that now names another account
    line = w.main(["start", s])[1]
    assert line["refused"] == "identity-changed", line
    # identseed: pins identity.gate_check's detail (a refusal's words), written to stderr by runtime_helper.main
    assert "e0: the sign-in now names a different account than the one recorded at this slot's login" in w.last_err
    out += [w.last_err]                                    # the helper writes the refusal detail to stderr
    return out


def test_no_operator_facing_identity_sentence_claims_a_check(gate_env):
    w, la, _ = gate_env
    sentences = _said_by_supervisor() + _refusals_from_the_gate(w, la)
    sentences.append(" ".join(L.build_parser().format_help().split()))
    assert len(sentences) >= 10 and all(sentences), sentences     # nothing was silently empty
    bad = [s for s in sentences if CHECK_CLAIMS.search(s)]
    assert not bad, bad


def test_the_confirm_prompt_says_it_records_a_label_and_does_not_check_the_account():
    sup = _Sup("claude", True, {"observed_id": UUID})
    sup._confirm_identity(S)
    prompt = sup.prompts[0]
    assert UUID in prompt and "account label" in prompt
    # identfix F-D1: the account a later session reports is the one render seeded from the record, so the prompt
    # says the refusal fires only on a difference, and never suggests a credential of another account is caught
    assert "does not see which account the stored login belongs to" in prompt
    assert "refused only if the account it reports differs from the recorded one" in prompt
    assert "names another account is refused" not in prompt and "same recorded account" not in prompt
    # identseed: no later session prints the label (only a refusal names it), so the prompt promises no showing
    assert "show" not in prompt
    written = [m for m in sup.said if m.startswith("baseline written")]
    assert written and "label" in written[0] and "does not check it" in written[0] and "shows" not in written[0]


def test_the_expired_line_does_not_claim_to_see_an_expiry():
    sup = _Sup("claude", True)
    sup._slot_index = 3
    sup.replies["status"] = {"records": [], "identity": {"result": "identity-expired"}}
    sup._identity_expired(S)
    said = " ".join(sup.said)
    assert "reports no sign-in" in said and "does not see a login that has expired or been revoked" in said


def test_the_check_pattern_catches_the_old_sentences():
    """The pattern itself is live: each retired sentence trips it."""
    for old in ("levain: this login is Claude account abc (the server's answer). Keep it as this slot's Claude "
                "baseline?",
                "slot 0's stored Claude login looks expired or revoked. Nothing was launched.",
                "the login for slot 0 could not be refreshed and checked; nothing was launched",
                "uses the slot's stored login after the identity check",
                "the account verified for this slot", "Levain confirmed the account",
                "Codex has no server-checked identity, so Levain cannot tell which account",
                "levain: Codex has no server-validated identity call, so this login is recorded as not a check",
                "add api.anthropic.com to the hosts of route claude-sub in the base map: the login check needs it"):
        assert CHECK_CLAIMS.search(old), old


def test_the_changed_line_names_the_sessions_own_vendor_binary():
    """identfix F-D4 (gemini): the identity-changed line names the session's vendor binary, never a fixed Claude."""
    sup = _Sup("codex", True)
    sup._slot_index = 3
    sup.replies["status"] = {"records": [], "identity": {"observed_id": UUID, "result": "identity-changed"}}
    sup._identity_refused(S)
    said = " ".join(sup.said)
    assert "read from the Codex binary's local data" in said and "Claude" not in said


def test_the_changelog_bullet_says_what_the_code_does():
    """identfix F-D1 + F-D5: no detection the seeding makes impossible, no claim about a run that has not happened."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    bullet = next(line for line in text.splitlines() if "identity-seed-missing:<slot>" in line)
    assert "refused only if the account it reports differs from the recorded one" in bullet
    for gone in ("same recorded account", "names another account is refused", "no longer refused",
                 "what it does catch", "no access token"):
        assert gone not in bullet, gone


def test_the_seed_missing_refusal_says_no_recorded_account_not_a_check(gate_env):
    w, la, _ = gate_env
    w.give_credential(E0, "claude", seed=False)            # a login parked before accounts were recorded
    s = _rendered(w, "egress", "interactive", "claude", "passthrough", blob=_blob(vendor="claude", routes=PASS))
    line = w.main(["start", s])[1]
    assert line["refused"] == "identity-seed-missing:e0", line
    detail = w.last_err
    assert "no recorded account" in detail and "log this slot in again" in detail and not CHECK_CLAIMS.search(detail)
