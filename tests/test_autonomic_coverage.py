"""The coverage core (14b DESIGN_REV3 §3-§5, DESIGN_REV3_1 D2): whether a signed graduation policy covers the
effect the gate will run, whether the policy is live and anchored to root's signers file, and the frozen snapshot
whose token is built from its own fields. Pure functions over plain values: no store, no authority log."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from levain.autonomic import coverage as cov
from levain.autonomic.coverage import (
    COVER_MOVED, COVER_NONE, Cover, CoverageSnapshot, Effect, GraduationPolicy, RootSigner, Uncovered,
    anchored, authority_keys, covered, live, operative, parse_root_signers, peek_snapshot, risk_dict,
    signers_digest, stricter_risk,
)
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk, RiskClass
from tests.autonomic_test_only_coverage import TEST_GUARD, terms_incarnation

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
LOW = ActionRisk(RiskClass.LOW, reversible=True, external=False, financial=False)
TRIVIAL = ActionRisk(RiskClass.TRIVIAL, reversible=True, external=False, financial=False)
EXTERNAL = ActionRisk(RiskClass.LOW, reversible=True, external=True, financial=False)
K1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k1k"
K2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k2k"
PASSKEY = "sk-ecdsa-sha2-nistp256@openssh.com AAAAInNrLWVjZHNhLXNoYTItbmlzdHAyNTZAb3BlbnNzaC5jb20AAAAIbmlzdHAyNTY="
ROOT = (f'k1 namespaces="levain-authority" {K1}\n'.encode())
HEAD = (7, "h" * 64)
STORE = "store-" + "0" * 32
OTHER_STORE = "store-" + "1" * 32


def stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(**over) -> dict:
    rec = {"id": "gp-0123456789ab", "kind": "graduation",
           "actions": {"doc.append": risk_dict(LOW)},
           "lowest_posture": "ON_LOOP", "expires_at": stamp(NOW + timedelta(days=10)),
           "triggers": None, "families": None, "store_id": STORE, "min_clean_fires": 20,
           "guard": [dict(g) for g in TEST_GUARD]}
    rec.update(over)
    return rec


def policy(born=K1, current=K1, seq=3, **over) -> GraduationPolicy:
    rec = record(**over)
    return GraduationPolicy.from_record(rec, incarnation=terms_incarnation(rec, seq), born=seq, born_signer=born,
                                        current_signer=current)


def effect(action="doc.append", risk=LOW, tools=None, trigger="email", family="fam-" + "a" * 32) -> Effect:
    return Effect(action_name=action, action_risk=risk, tools_risk=tools, trigger_type=trigger, family=family)


ROOT_KEYS = frozenset({K1})


# ---- covered ------------------------------------------------------------------------------------------------

def test_a_signed_action_at_its_signed_risk_is_covered():
    assert covered(policy(), effect())


def test_an_action_the_policy_does_not_sign_is_not_covered():
    assert not covered(policy(), effect(action="deliver"))


def test_an_action_the_manifest_does_not_know_is_not_covered():
    # UnknownAction at the gate gives action_risk None: uncovered, whatever the policy signs
    assert not covered(policy(), effect(risk=None))


def test_a_changed_manifest_risk_is_not_covered():
    assert not covered(policy(), effect(risk=TRIVIAL))


def test_a_signed_risk_dict_with_an_extra_field_is_not_covered():
    extra = dict(risk_dict(LOW), audited=True)
    assert not covered(policy(actions={"doc.append": extra}), effect())


def test_a_signed_risk_dict_missing_a_field_is_not_covered():
    short = {k: v for k, v in risk_dict(LOW).items() if k != "financial"}
    assert not covered(policy(actions={"doc.append": short}), effect())


def test_equality_is_by_json_type_not_python_truthiness():
    # Python says 1 == True and 1.0 == 1: a signed dict with "reversible": 1 or "cls": 1.0 is not the risk dict
    assert not covered(policy(actions={"doc.append": dict(risk_dict(LOW), reversible=1)}), effect())
    assert not covered(policy(actions={"doc.append": dict(risk_dict(LOW), cls=1.0)}), effect())


def test_triggers_null_covers_any_trigger_and_a_list_only_its_own():
    assert covered(policy(triggers=None), effect(trigger="cron"))
    assert covered(policy(triggers=["email"]), effect(trigger="email"))
    assert not covered(policy(triggers=["email"]), effect(trigger="cron"))
    assert not covered(policy(triggers=["email"]), effect(trigger=None))


def test_families_null_covers_any_family_and_a_list_only_its_own():
    fam = "fam-" + "b" * 32
    assert covered(policy(families=None), effect(family=fam))
    assert covered(policy(families=[fam]), effect(family=fam))
    assert not covered(policy(families=[fam]), effect())
    assert not covered(policy(families=[fam]), effect(family=None))


def test_a_manual_effect_is_covered_only_by_a_policy_with_both_lists_null():
    manual = effect(trigger=None, family=None)
    assert covered(policy(), manual)
    assert not covered(policy(triggers=["email"]), manual)
    assert not covered(policy(families=["fam-" + "a" * 32]), manual)


# ---- anchored ------------------------------------------------------------------------------------------------

def test_a_policy_whose_born_and_current_signers_are_root_listed_is_anchored():
    assert anchored(policy(), ROOT_KEYS)


def test_an_unlisted_born_signer_is_not_anchored():
    assert not anchored(policy(born=K2), ROOT_KEYS)


def test_an_unlisted_current_signer_is_not_anchored():
    # X-H2: a renew by a key root does not list leaves the current link unanchored
    assert not anchored(policy(current=K2), ROOT_KEYS)


def test_a_missing_signer_is_not_anchored():
    assert not anchored(policy(born=None), ROOT_KEYS)
    assert not anchored(policy(current=None), ROOT_KEYS)


def test_signers_compare_by_type_and_blob_not_comment():
    assert anchored(policy(born=K1 + " laptop@mac", current=K1), ROOT_KEYS)
    other_type = "ssh-rsa " + K1.split()[1]
    assert not anchored(policy(born=other_type, current=other_type), ROOT_KEYS)


# ---- live ----------------------------------------------------------------------------------------------------

def test_an_unexpired_anchored_policy_is_live():
    assert live(policy(), NOW, ROOT_KEYS)


def test_an_expired_policy_is_not_live():
    p = policy(expires_at=stamp(NOW))
    assert not live(p, NOW, ROOT_KEYS)                         # now < expires_at, strictly
    assert not live(p, NOW + timedelta(seconds=1), ROOT_KEYS)


def test_an_expiry_past_now_plus_the_cap_is_not_live():
    edge = policy(expires_at=stamp(NOW + cov.GRADUATION_GRANT_MAX))
    assert live(edge, NOW, ROOT_KEYS)
    beyond = policy(expires_at=stamp(NOW + cov.GRADUATION_GRANT_MAX + timedelta(microseconds=1)))
    assert not live(beyond, NOW, ROOT_KEYS)


def test_an_unanchored_policy_is_not_live():
    assert not live(policy(current=K2), NOW, ROOT_KEYS)


def test_live_refuses_a_naive_now():
    with pytest.raises(ValueError):
        live(policy(), NOW.replace(tzinfo=None), ROOT_KEYS)


def test_the_cap_is_the_consent_grants_cap():
    # R1 Q3 (Phill, 10:09): 30 days, the consent grants' cap. S1's GRADUATION_GRANT_MAX is pinned to the same
    from levain.authority import _CONSENT_GRANT_MAX
    assert cov.GRADUATION_GRANT_MAX == _CONSENT_GRANT_MAX
    import levain.authority as authority
    if hasattr(authority, "GRADUATION_GRANT_MAX"):
        assert cov.GRADUATION_GRANT_MAX == authority.GRADUATION_GRANT_MAX


# ---- the record ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"kind": "consent"},
    {"actions": {}},
    {"actions": {"doc.append": "low"}},
    {"actions": {"": risk_dict(LOW)}},
    {"lowest_posture": "ABOVE_LOOP"},
    {"lowest_posture": "CONFIRM"},
    {"lowest_posture": 1},
    {"expires_at": "2026-11-01"},
    {"expires_at": "2026-11-01T00:00:00Z"},
    {"triggers": []},
    {"triggers": ["email", 3]},
    {"families": []},
    {"families": "fam-x"},
    {"id": ""},
    {"unknown_term": 1},
    {"store_id": None},
    {"store_id": ""},
    {"store_id": 7},
    {"actions": {"doc.append": {1: True}}},
    {"actions": {"doc.append": dict(risk_dict(LOW), nested={2: "x"})}},
])
def test_a_malformed_record_is_refused(over):
    with pytest.raises(ValueError):
        policy(**over)


def test_a_deeply_nested_risk_dict_is_refused_not_a_recursion_error():
    deep: dict = {}
    for _ in range(5000):
        deep = {"d": deep}
    with pytest.raises(ValueError):
        policy(actions={"doc.append": deep})


def test_a_bad_list_names_what_is_wrong_with_its_value():
    with pytest.raises(ValueError, match=r"triggers must be null or a non-empty list of names \(got \[\]\)"):
        policy(triggers=[])
    with pytest.raises(ValueError, match=r"families must be null .*\(got 'fam-x'\)"):
        policy(families="fam-x")


def test_a_record_missing_a_binding_term_is_refused():
    for name in ("id", "kind", "actions", "lowest_posture", "expires_at", "store_id"):
        rec = record()
        del rec[name]
        with pytest.raises(ValueError):
            GraduationPolicy.from_record(rec, incarnation="1:x", born=1, born_signer=K1, current_signer=K1)


def test_an_unknown_key_that_is_not_a_string_is_a_value_error():
    # deepseek LOW 6: the message sorts the extra keys by repr, so a non-str key cannot raise TypeError
    rec = record()
    rec[1] = "x"
    rec["unknown_term"] = 2
    with pytest.raises(ValueError, match="carries no"):
        GraduationPolicy.from_record(rec, incarnation="1:x", born=1, born_signer=K1, current_signer=K1)


# deepseek HIGH 2: a policy built directly, not through from_record, is held to the same shape
@pytest.mark.parametrize("over", [
    {"lowest_posture": Posture.ABOVE_LOOP},
    {"lowest_posture": Posture.CONFIRM},
    {"lowest_posture": "ON_LOOP"},
    {"lowest_posture": 1},
    {"expires_at": datetime(2026, 11, 1)},
    {"expires_at": "2026-11-01T00:00:00.000000Z"},
    {"actions": ()},
    {"actions": [("doc.append", '{"cls":1}')]},
    {"actions": (("doc.append",),)},
    {"actions": (("", '{"cls":1}'),)},
    {"actions": (("doc.append", {"cls": 1}),)},
    {"actions": (("doc.append", "not json"),)},
    {"actions": (("doc.append", '{"cls": 1}'),)},    # JSON, but not the canonical text: it could never match
    {"actions": (("doc.append", "[1]"),)},
    {"actions": (("doc.append", '{"cls":1}'), ("doc.append", '{"cls":2}'))},
    {"triggers": frozenset()},
    {"triggers": {"email"}},
    {"triggers": ["email"]},
    {"families": frozenset({3})},
    {"families": frozenset({""})},
    {"born_signer": 7},
    {"current_signer": b"ssh-ed25519 AAAA"},
    {"store_id": ""},
    {"store_id": None},
    {"grant_id": ""},
    {"incarnation": None},
])
def test_a_policy_built_directly_is_held_to_the_records_shape(over):
    with pytest.raises(ValueError):
        dataclasses.replace(policy(), **over)


def test_a_directly_built_above_loop_policy_never_reaches_a_snapshot():
    # the fail-open the HIGH names: an ABOVE_LOOP policy would make operative return ABOVE_LOOP
    with pytest.raises(ValueError):
        GraduationPolicy(grant_id="gp-1", incarnation="1:a",
                         actions=(("doc.append", cov._canonical(risk_dict(LOW))),),
                         lowest_posture=Posture.ABOVE_LOOP, expires_at=NOW + timedelta(days=1), triggers=None,
                         families=None, born_signer=K1, current_signer=K1, store_id=STORE, born=1,
                         terms_text="{}")


def test_a_directly_built_policy_of_the_records_shape_is_the_records_policy():
    p = policy(triggers=["email"], families=["fam-x"])
    assert dataclasses.replace(p) == p
    q = GraduationPolicy(grant_id=p.grant_id, incarnation=p.incarnation, actions=p.actions,
                         lowest_posture=p.lowest_posture, expires_at=p.expires_at, triggers=p.triggers,
                         families=p.families, born_signer=None, current_signer=None, store_id=p.store_id,
                         born=p.born, terms_text=p.terms_text)
    assert q.cover_value == p.cover_value


def test_the_stamp_form_is_the_cockpits():
    from levain.cockpit.consent import parse_stamp, utc_stamp
    text = utc_stamp(NOW + timedelta(days=3, microseconds=7))
    assert policy(expires_at=text).expires_at == parse_stamp(text)
    assert stamp(NOW) == utc_stamp(NOW)


def test_the_policy_is_frozen_and_does_not_alias_the_record():
    rec = record()
    p = GraduationPolicy.from_record(rec, incarnation=terms_incarnation(rec, 1), born=1, born_signer=K1,
                                     current_signer=K1)
    rec["actions"]["doc.append"]["cls"] = 0
    rec["actions"]["deliver"] = risk_dict(LOW)
    assert covered(p, effect()) and not covered(p, effect(action="deliver"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.lowest_posture = Posture.ABOVE_LOOP  # type: ignore[misc]
    hash(p)


def test_risk_dict_is_the_gates_rendering():
    from levain.autonomic.gate import _risk_dict
    for r in (LOW, TRIVIAL, EXTERNAL, None):
        assert risk_dict(r) == _risk_dict(r)


# ---- operative and the snapshot -------------------------------------------------------------------------------

def snapshot(*policies, root=ROOT, head=HEAD, store=STORE) -> CoverageSnapshot:
    return CoverageSnapshot(head=head, root_bytes=root, policies=tuple(policies), store_id=store)


class Recording:
    """A view that records every cover() call, and answers covered at ON_LOOP."""

    token = (HEAD, "sha256:x")

    def __init__(self) -> None:
        self.calls = 0

    def cover(self, e, now):
        self.calls += 1
        return Cover(Posture.ON_LOOP, "1:a@x")


def test_a_candidate_at_or_above_confirm_never_consults_the_clamp():
    view = Recording()
    for r in (Posture.CONFIRM, Posture.CONFIRM_ELEVATED, Posture.REFUSE_ESCALATE):
        assert operative(r, effect(), snapshot(), NOW) == (r, None)
        assert operative(r, effect(), None, NOW) == (r, None)
        assert operative(r, effect(), view, NOW) == (r, None)
    assert view.calls == 0


def test_the_risk_floor_applies_at_or_above_confirm_too():
    # the floor can only raise: a financial tools risk under a CONFIRM candidate runs at CONFIRM_ELEVATED
    financial = ActionRisk(RiskClass.LOW, reversible=True, external=False, financial=True)
    view = Recording()
    assert operative(Posture.CONFIRM, effect(tools=financial), view, NOW) == (Posture.CONFIRM_ELEVATED, None)
    assert operative(Posture.CONFIRM, effect(risk=financial), None, NOW) == (Posture.CONFIRM_ELEVATED, None)
    assert operative(Posture.REFUSE_ESCALATE, effect(tools=financial), view, NOW) == \
        (Posture.REFUSE_ESCALATE, None)
    assert view.calls == 0


def test_the_policy_keeps_its_store_id():
    assert policy().store_id == STORE


def test_a_policy_signed_for_another_store_covers_nothing_here():
    other = policy(store_id=OTHER_STORE)
    assert other.store_id == OTHER_STORE
    assert snapshot(other).cover(effect(), NOW) == Cover(None, COVER_NONE)
    assert operative(Posture.ON_LOOP, effect(), snapshot(other), NOW) == (Posture.CONFIRM, COVER_NONE)
    # the same policy covers in the store it was signed for
    assert operative(Posture.ON_LOOP, effect(), snapshot(other, store=OTHER_STORE), NOW)[0] == Posture.ON_LOOP


def test_a_snapshot_needs_the_current_stores_id():
    with pytest.raises(TypeError):
        CoverageSnapshot(head=HEAD, root_bytes=ROOT, policies=())  # type: ignore[call-arg]
    for bad in (None, "", 7):
        with pytest.raises(TypeError):
            snapshot(store=bad)


def test_an_uncovered_effect_below_confirm_is_confirm():
    assert operative(Posture.ON_LOOP, effect(), snapshot(), NOW) == (Posture.CONFIRM, COVER_NONE)
    assert operative(Posture.COOLING_OFF, effect(), None, NOW) == (Posture.CONFIRM, COVER_NONE)


def test_a_covered_effect_runs_at_the_stricter_of_the_candidate_and_the_signed_rung():
    p = policy(lowest_posture="COOLING_OFF")
    rung, value = operative(Posture.ON_LOOP, effect(), snapshot(p), NOW)
    assert rung == Posture.COOLING_OFF
    assert value == f"{p.incarnation}@{p.expires_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
    assert p.incarnation.startswith("3:")
    assert operative(Posture.COOLING_OFF, effect(), snapshot(policy()), NOW)[0] == Posture.COOLING_OFF
    assert operative(Posture.ABOVE_LOOP, effect(), snapshot(policy()), NOW)[0] == Posture.ON_LOOP


def test_the_strictest_covering_policy_wins_and_its_value_is_the_cover():
    loose = policy(seq=1, lowest_posture="ON_LOOP")
    strict = policy(seq=2, lowest_posture="COOLING_OFF")
    rung, value = operative(Posture.ON_LOOP, effect(), snapshot(loose, strict), NOW)
    assert rung == Posture.COOLING_OFF and value == strict.cover_value and value.startswith("2:")
    assert operative(Posture.ON_LOOP, effect(), snapshot(strict, loose), NOW) == (rung, value)


def test_ties_pick_one_value_whatever_the_order():
    a = policy(seq=1)
    b = policy(seq=2)
    assert operative(Posture.ON_LOOP, effect(), snapshot(a, b), NOW) == \
        operative(Posture.ON_LOOP, effect(), snapshot(b, a), NOW)


def test_the_floor_of_the_stricter_risk_only_raises_the_rung():
    # an external tools risk floors at CONFIRM even under a covering policy
    assert operative(Posture.ON_LOOP, effect(tools=EXTERNAL), snapshot(policy()), NOW)[0] == Posture.CONFIRM
    assert stricter_risk(LOW, EXTERNAL) == EXTERNAL
    assert stricter_risk(LOW, None) == LOW and stricter_risk(None, LOW) == LOW and stricter_risk(None, None) is None
    mixed = stricter_risk(ActionRisk(RiskClass.MEDIUM, True, False, False), ActionRisk(RiskClass.LOW, False, False, True))
    assert mixed == ActionRisk(RiskClass.MEDIUM, reversible=False, external=False, financial=True)


def test_an_expired_or_unanchored_policy_in_the_snapshot_covers_nothing():
    assert operative(Posture.ON_LOOP, effect(), snapshot(policy(expires_at=stamp(NOW))), NOW)[0] == Posture.CONFIRM
    assert operative(Posture.ON_LOOP, effect(), snapshot(policy(current=K2)), NOW)[0] == Posture.CONFIRM
    # the same policy, its signer dropped from root: the snapshot's own bytes decide
    assert operative(Posture.ON_LOOP, effect(), snapshot(policy(), root=b""), NOW)[0] == Posture.CONFIRM


def test_a_view_that_raises_is_uncovered():
    class Broken:
        token = (HEAD, "sha256:x")

        def cover(self, e, now):
            raise RuntimeError("boom")

    assert operative(Posture.ON_LOOP, effect(), Broken(), NOW) == (Posture.CONFIRM, COVER_NONE)


class Answers:
    """A view whose cover() returns ``answer`` whatever it is."""

    token = (HEAD, "sha256:x")

    def __init__(self, answer) -> None:
        self.answer = answer

    def cover(self, e, now):
        return self.answer


class LooksLikeACover:
    posture = Posture.ON_LOOP
    value = "1:a@x"


@pytest.mark.parametrize("answer", [None, "1:a@x", LooksLikeACover(), (Posture.ON_LOOP, "1:a@x"),
                                    Cover("ON_LOOP", "1:a@x"), Cover(1, "1:a@x"), Cover(Posture.ON_LOOP, 7)])
def test_a_view_that_answers_anything_but_a_cover_is_uncovered(answer):
    assert operative(Posture.ON_LOOP, effect(), Answers(answer), NOW) == (Posture.CONFIRM, COVER_NONE)


# deepseek HIGH 1: a Cover that breaks the module's own invariants is no cover
_BROKEN_COVERS = [
    Cover(Posture.ON_LOOP, COVER_NONE),        # a rung with the uncovered value
    Cover(Posture.COOLING_OFF, COVER_NONE),
    Cover(None, "1:a@x"),                      # no rung with a covered value
    Cover(None, COVER_MOVED),
    Cover(Posture.ON_LOOP, COVER_MOVED),       # the peek's value is never a cover value
    Cover(Posture.ON_LOOP, "no-delimiter"),    # a covered value holds an "@"
    Cover(Posture.ABOVE_LOOP, "1:a@x"),        # no policy signs ABOVE_LOOP
    Cover(Posture.CONFIRM, "1:a@x"),           # nor anything at or above CONFIRM
]


@pytest.mark.parametrize("answer", _BROKEN_COVERS)
def test_a_cover_that_breaks_the_invariants_is_uncovered(answer):
    assert operative(Posture.ON_LOOP, effect(), Answers(answer), NOW) == (Posture.CONFIRM, COVER_NONE)
    assert operative(Posture.ABOVE_LOOP, effect(), Answers(answer), NOW) == (Posture.CONFIRM, COVER_NONE)


@pytest.mark.parametrize("answer", _BROKEN_COVERS)
def test_a_peek_at_a_cover_that_breaks_the_invariants_is_moved(answer):
    view = Answers(answer)
    assert peek_snapshot(view, view.token, effect(), NOW) == COVER_MOVED


def test_a_cover_of_the_invariants_shape_is_taken():
    assert operative(Posture.ON_LOOP, effect(), Answers(Cover(Posture.COOLING_OFF, "1:a@x")), NOW) == (
        Posture.COOLING_OFF, "1:a@x")
    assert operative(Posture.ABOVE_LOOP, effect(), Answers(Cover(Posture.ON_LOOP, "1:a@x")), NOW) == (
        Posture.ON_LOOP, "1:a@x")
    assert operative(Posture.ON_LOOP, effect(), Answers(Cover(None, COVER_NONE)), NOW) == (
        Posture.CONFIRM, COVER_NONE)
    view = Answers(Cover(None, COVER_NONE))
    assert peek_snapshot(view, view.token, effect(), NOW) == COVER_NONE


@pytest.mark.parametrize("answer", [None, "1:a@x", LooksLikeACover(), Cover(Posture.ON_LOOP, 7)])
def test_a_peek_at_a_view_that_answers_anything_but_a_cover_is_moved(answer):
    view = Answers(answer)
    assert peek_snapshot(view, view.token, effect(), NOW) == COVER_MOVED


def test_a_peek_at_a_view_that_raises_is_moved():
    class Broken:
        token = (HEAD, "sha256:x")

        def cover(self, e, now):
            raise RuntimeError("boom")

    # COVER_MOVED equals no decided fence, so a decided "none" is decided again rather than matched
    assert peek_snapshot(Broken(), Broken.token, effect(), NOW) == COVER_MOVED


def test_the_token_is_built_from_the_snapshots_own_fields():
    s = snapshot(policy())
    assert s.token == (HEAD, signers_digest(ROOT), STORE)
    assert s.signers_digest == "sha256:" + hashlib.sha256(ROOT).hexdigest()
    assert s.signers == parse_root_signers(ROOT)
    fields = {f.name for f in dataclasses.fields(CoverageSnapshot) if f.init}
    assert fields == {"head", "root_bytes", "policies", "store_id"}   # no token, digest or signers taken apart
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.head = (8, "x")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.signers_digest = "sha256:forged"  # type: ignore[misc]


def test_a_snapshot_refuses_a_bad_head_or_non_bytes_root():
    with pytest.raises((TypeError, ValueError)):
        CoverageSnapshot(head=(7,), root_bytes=ROOT, policies=(), store_id=STORE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CoverageSnapshot(head=HEAD, root_bytes=ROOT.decode(), policies=(), store_id=STORE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CoverageSnapshot(head=HEAD, root_bytes=ROOT, policies=[policy()], store_id=STORE)  # type: ignore[arg-type]


def test_uncovered_covers_nothing_at_its_token():
    u = Uncovered(head=HEAD, signers_digest="sha256:abc", store_id=STORE)
    assert u.token == (HEAD, "sha256:abc", STORE)
    assert u.cover(effect(), NOW) == Cover(None, COVER_NONE)
    assert peek_snapshot(u, (HEAD, "sha256:abc", STORE), effect(), NOW) == COVER_NONE
    assert peek_snapshot(u, (HEAD, "sha256:abc", OTHER_STORE), effect(), NOW) == COVER_MOVED


@pytest.mark.parametrize("store", ["", None, 7])
def test_uncovered_needs_a_stores_id(store):
    with pytest.raises(TypeError):
        Uncovered(head=HEAD, signers_digest="sha256:abc", store_id=store)


def test_the_token_binds_the_store():
    # deepseek LOW 7: a snapshot built for another store, at the same head and root bytes, is a moved state here
    mine, theirs = snapshot(policy()), snapshot(policy(store_id=OTHER_STORE), store=OTHER_STORE)
    assert mine.token != theirs.token
    assert operative(Posture.ON_LOOP, effect(), theirs, NOW)[0] == Posture.ON_LOOP   # it covers in its own store
    assert peek_snapshot(theirs, mine.token, effect(), NOW) == COVER_MOVED
    assert peek_snapshot(mine, mine.token, effect(), NOW) != COVER_MOVED


def test_uncovered_refuses_a_naive_now_like_the_snapshot():
    u = Uncovered(head=HEAD, signers_digest="sha256:abc", store_id=STORE)
    for view in (u, snapshot(policy())):
        with pytest.raises(ValueError):
            view.cover(effect(), NOW.replace(tzinfo=None))


def test_peek_matches_only_the_same_token():
    s = snapshot(policy())
    decided = operative(Posture.ON_LOOP, effect(), s, NOW)[1]
    assert peek_snapshot(s, s.token, effect(), NOW) == decided
    assert peek_snapshot(s, ((8, HEAD[1]), s.signers_digest), effect(), NOW) == COVER_MOVED   # the log moved
    assert peek_snapshot(s, ((7, "g" * 64), s.signers_digest), effect(), NOW) == COVER_MOVED  # same seq, other tip
    assert peek_snapshot(s, (HEAD, signers_digest(b"")), effect(), NOW) == COVER_MOVED       # root file edited
    assert peek_snapshot(None, s.token, effect(), NOW) == COVER_MOVED


def test_peek_re_runs_expiry_at_its_own_now():
    # C-4: no head move, the clock passes expiry: the cover value changes, so the fence does not match
    p = policy(expires_at=stamp(NOW + timedelta(minutes=1)))
    s = snapshot(p)
    decided = operative(Posture.ON_LOOP, effect(), s, NOW)[1]
    assert decided != COVER_NONE
    assert peek_snapshot(s, s.token, effect(), NOW + timedelta(minutes=2)) == COVER_NONE


def test_the_cover_values_never_equal_each_other_or_an_incarnation_value():
    assert COVER_MOVED != COVER_NONE
    assert "@" not in COVER_MOVED and "@" not in COVER_NONE


# ---- parse_root_signers ---------------------------------------------------------------------------------------

def keys_of(text: str) -> frozenset[str]:
    return authority_keys(parse_root_signers(text.encode()))


def test_a_plain_authority_line_is_listed():
    assert keys_of(f'k1 namespaces="levain-authority" {K1}\n') == {K1}


def test_a_list_of_namespaces_including_authority_is_listed():
    assert keys_of(f'k1 namespaces="levain-confirm,levain-authority" {K1}\n') == {K1}
    assert keys_of(f'k1 namespaces="a,b" {K1}\n') == frozenset()


def test_a_line_for_another_namespace_only_is_not_listed():
    assert keys_of(f'k1 namespaces="levain-confirm" {K1}\n') == frozenset()


def test_a_line_with_no_namespaces_option_is_not_listed():
    # ssh lets such a key sign in every namespace; the design admits only an explicit namespaces= naming it
    assert keys_of(f"k1 {K1}\n") == frozenset()


@pytest.mark.parametrize("opts", [
    'cert-authority,namespaces="levain-authority"',
    'namespaces="levain-authority",valid-before="29991231"',
    'valid-after="20200101",namespaces="levain-authority"',
    'namespaces="levain-authority",namespaces="levain-authority"',
    'no-touch-required,namespaces="levain-authority"',
])
def test_a_root_line_with_any_other_option_is_not_listed(opts):
    assert keys_of(f"k1 {opts} {K1}\n") == frozenset()


def test_a_cert_authority_line_alone_is_not_listed():
    # not even as a no-option line: the parse drops it, whatever authority_keys would make of it
    assert parse_root_signers(f"k1 cert-authority {K1}\n".encode()) == ()


def test_an_unquoted_or_escaped_namespaces_value_is_not_listed():
    assert keys_of(f"k1 namespaces=levain-authority {K1}\n") == frozenset()
    assert keys_of(f'k1 namespaces="levain-\\"authority" {K1}\n') == frozenset()


def test_a_pattern_or_negation_never_lists_a_key():
    assert keys_of(f'k1 namespaces="levain-*" {K1}\n') == frozenset()
    assert keys_of(f'k1 namespaces="*" {K1}\n') == frozenset()
    assert keys_of(f'k1 namespaces="levain-authority,!levain-authority" {K1}\n') == frozenset()
    assert keys_of(f'k1 namespaces="!git,levain-authority" {K1}\n') == frozenset()


def test_the_option_name_is_matched_as_ssh_matches_it():
    # OpenSSH matches option names case-insensitively (opt_match); the value is case-sensitive
    assert keys_of(f'k1 NAMESPACES="levain-authority" {K1}\n') == {K1}
    assert keys_of(f'k1 namespaces="LEVAIN-AUTHORITY" {K1}\n') == frozenset()


def test_comments_blank_lines_quoted_principals_and_trailing_comments():
    text = (f"# root's file\n\n   \n"
            f'"k1 laptop" namespaces="levain-authority" {K1} laptop key comment\n'
            f'k2,k3 namespaces="levain-authority" {PASSKEY}\n')
    signers = parse_root_signers(text.encode())
    assert [s.key for s in signers] == [K1, PASSKEY]
    assert signers[0].principals == "k1 laptop"
    assert authority_keys(signers) == {K1, PASSKEY}


def test_an_unknown_key_type_or_bad_blob_is_not_listed():
    assert keys_of('k1 namespaces="levain-authority" ssh-dss AAAAB3NzaC1kc3M=\n') == frozenset()
    assert keys_of('k1 namespaces="levain-authority" ssh-ed25519 not*base64\n') == frozenset()
    assert keys_of('k1 namespaces="levain-authority" ssh-ed25519\n') == frozenset()
    assert keys_of(f'namespaces="levain-authority" {K1}\n') == frozenset()   # no principal field


@pytest.mark.parametrize("blob", [
    "AAA",                                   # not a whole number of base64 quads
    K1.split()[1][:-1],                      # a truncated key
    "AAAA=",                                 # padding past a whole quad
    "AA=A",                                  # padding inside the blob
    "AAAAA===",                              # three padding characters
    "=AAA",
])
def test_a_blob_that_is_not_shaped_base64_is_not_listed(blob):
    assert keys_of(f'k1 namespaces="levain-authority" ssh-ed25519 {blob}\n') == frozenset()


@pytest.mark.parametrize("blob", ["AAAA", "AAA=", "AA==", K1.split()[1], PASSKEY.split()[1]])
def test_a_blob_of_whole_padded_quads_is_listed(blob):
    assert keys_of(f'k1 namespaces="levain-authority" ssh-ed25519 {blob}\n') == {f"ssh-ed25519 {blob}"}


def test_a_bad_line_does_not_spoil_the_good_ones():
    text = f'k1 cert-authority {K2}\njunk\nk1 namespaces="levain-authority" {K1}\n'
    assert keys_of(text) == {K1}


def test_a_line_that_is_not_utf8_is_not_listed_and_the_others_are():
    bad = b'k2 namespaces="levain-authority" ' + K2.encode() + b" \xff\xfe\n"
    assert authority_keys(parse_root_signers(bad + ROOT)) == {K1}


def test_parse_takes_bytes_only():
    with pytest.raises(TypeError):
        parse_root_signers(ROOT.decode())  # type: ignore[arg-type]


def test_the_key_types_are_the_enrol_doors():
    from levain.cockpit.keys import _KEY_TYPES
    assert cov.KEY_TYPES == _KEY_TYPES


def test_root_signer_is_frozen():
    s = RootSigner(principals="k1", namespaces=frozenset({"levain-authority"}), key=K1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.key = K2  # type: ignore[misc]


def test_the_namespace_is_the_authority_logs():
    from levain.authority_sig import NAMESPACE
    assert cov.AUTHORITY_NAMESPACE == NAMESPACE


def test_risk_dict_renders_the_four_fields():
    assert json.dumps(risk_dict(LOW), sort_keys=True) == '{"cls": 1, "external": false, "financial": false, "reversible": true}'
