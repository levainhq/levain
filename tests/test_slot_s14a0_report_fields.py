"""S14 A0: CONTROL's integrity rule for ``verifier_report_digest`` / ``verifier_report_refused``.

The rule (control.cas): the pair is accepted only on a record whose ``claims.kind`` is ``verifier``; only on a
field-only CAS on RENDERING or ACTIVE (start's own write) or a CAS into RETAINED or HELD, and on RENDERED a field-only
CAS of ``verifier_report_refused`` alone (a clone failure's name; never the digest, never both); once either is set, a CAS
naming another value, the other member of the pair, or a removal is ``report-immutable``, while the same value is
accepted (a retry stays idempotent). Every refusal is fail-closed: nothing is written.
"""
from __future__ import annotations

import pytest

from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot.ids import SlotId
from tests import test_slot_s2f2_runtime_helper as T2
from tests.test_slot_s2f2_runtime_helper import world  # noqa: F401 - fixture

S = "s-00000000000000c1"
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64


def _put(world, state, kind="verifier", fields=None, session=S):  # noqa: F811
    slot = SlotId("none", 0) if kind == "verifier" else SlotId("egress", 0)
    claims = {"operator_uid": T2.OP_UID, "supervisor_uid": T2.DISPATCH_UID, "slot": R.slot_obj(slot),
              "class": slot.slot_class, "kind": kind, "vendor": "claude",
              "auth": "none" if kind == "verifier" else "brokered", "lease_seq": 1, "slot_rev": 1}
    world.control.store.put(R.SessionState(session=session, generation=0, state=state, claims=claims,
                                           lease_live=True, event_kind="state", updated_at="2026-10-20T00:00:00Z",
                                           fields=dict(fields or {})))
    world.control._session_lease[session] = C.Lease(C.new_handle(), T2.DISPATCH_UID, used=True, session=session)


def _cas(world, frm, to, fields, session=S):  # noqa: F811
    st = world.control.store.get(session)
    return world.control.cas(session, st.generation, frm, to, fields)


def _fields(world, session=S):  # noqa: F811
    return dict(world.control.store.get(session).fields)


def test_p2_inverted_the_two_moves_are_refused(world, request):  # noqa: F811
    """The probe's p2: the digest rode RENDERED -> REVOKED, then REVOKED -> FROZEN replaced it. Both now refuse."""
    T2.test_none_class_no_front_no_handle(world, request)
    st = world.state(S)
    with pytest.raises(Refused) as e:
        world.control.cas(S, st.generation, R.RENDERED, R.REVOKED, {"verifier_report_digest": A})
    assert e.value.name == "edge-not-allowed"
    assert "verifier_report_digest" not in world.state(S).fields
    assert world.state(S).state == R.RENDERED
    # the REVOKED -> FROZEN replacement, on a record whose digest is already set
    _put(world, R.REVOKED, fields={"verifier_report_digest": A}, session="s-00000000000000c2")
    with pytest.raises(Refused) as e:
        _cas(world, R.REVOKED, R.FROZEN, {"verifier_report_digest": B}, session="s-00000000000000c2")
    assert e.value.name == "edge-not-allowed"
    assert _fields(world, "s-00000000000000c2")["verifier_report_digest"] == A


@pytest.mark.parametrize("state", [R.RENDERING, R.ACTIVE])
def test_field_only_cas_on_rendering_or_active_is_accepted(world, state):  # noqa: F811
    _put(world, state)
    _cas(world, state, state, {"verifier_report_digest": A})
    assert _fields(world)["verifier_report_digest"] == A


def test_refused_name_also_field_only(world):  # noqa: F811
    _put(world, R.ACTIVE)
    _cas(world, R.ACTIVE, R.ACTIVE, {"verifier_report_refused": "driver-timeout"})
    assert _fields(world)["verifier_report_refused"] == "driver-timeout"


@pytest.mark.parametrize("state", [R.RENDERED, R.BRAKED])
def test_field_only_cas_on_other_live_states_is_refused(world, state):  # noqa: F811
    _put(world, state)
    with pytest.raises(Refused) as e:
        _cas(world, state, state, {"verifier_report_digest": A})
    assert e.value.name == "edge-not-allowed"
    assert _fields(world) == {}


@pytest.mark.parametrize("frm,to", [(R.ACTIVE, R.REVOKED), (R.RENDERING, R.RENDERED), (R.ACTIVE, R.BRAKED),
                                    (R.REVOKED, R.FROZEN)])
def test_state_moving_edges_other_than_retained_held_refuse(world, frm, to):  # noqa: F811
    _put(world, frm)
    with pytest.raises(Refused) as e:
        _cas(world, frm, to, {"verifier_report_refused": "driver-timeout"})
    assert e.value.name == "edge-not-allowed"
    assert world.control.store.get(S).state == frm
    assert _fields(world) == {}


@pytest.mark.parametrize("frm,to", [(R.CREDENTIAL_PARKED, R.RETAINED), (R.HELD, R.RETAINED), (R.REVOKED, R.HELD)])
def test_retained_and_held_edges_accept(world, frm, to):  # noqa: F811
    _put(world, frm)
    _cas(world, frm, to, {"verifier_report_digest": A})
    assert _fields(world)["verifier_report_digest"] == A


def test_executor_record_carrying_a_report_field_is_refused(world):  # noqa: F811
    for name, val in (("verifier_report_digest", A), ("verifier_report_refused", "driver-timeout")):
        _put(world, R.ACTIVE, kind="interactive")
        with pytest.raises(Refused) as e:
            _cas(world, R.ACTIVE, R.ACTIVE, {name: val})
        assert e.value.name == "report-not-verifier"
        assert _fields(world) == {}
    _put(world, R.CREDENTIAL_PARKED, kind="interactive")
    with pytest.raises(Refused) as e:
        _cas(world, R.CREDENTIAL_PARKED, R.RETAINED, {"verifier_report_digest": A})
    assert e.value.name == "report-not-verifier"


def test_set_value_is_immutable_same_value_is_idempotent(world):  # noqa: F811
    _put(world, R.ACTIVE, fields={"verifier_report_digest": A})
    gen = world.control.store.get(S).generation
    with pytest.raises(Refused) as e:
        _cas(world, R.ACTIVE, R.ACTIVE, {"verifier_report_digest": B})
    assert e.value.name == "report-immutable"
    with pytest.raises(Refused) as e:                                      # the other member of the pair
        _cas(world, R.ACTIVE, R.ACTIVE, {"verifier_report_refused": "driver-timeout"})
    assert e.value.name == "report-immutable"
    with pytest.raises(Refused) as e:                                      # a removal
        _cas(world, R.ACTIVE, R.ACTIVE, {"verifier_report_digest": None})
    assert e.value.name == "report-immutable"
    assert _fields(world) == {"verifier_report_digest": A}
    assert world.control.store.get(S).generation == gen
    # the same value (alone or beside another field) is accepted: a retry stays idempotent
    _cas(world, R.ACTIVE, R.ACTIVE, {"verifier_report_digest": A, "failed_step": "x"})
    assert _fields(world)["verifier_report_digest"] == A


def test_retained_move_retry_with_same_value_is_idempotent(world):  # noqa: F811
    _put(world, R.CREDENTIAL_PARKED)
    g1 = _cas(world, R.CREDENTIAL_PARKED, R.RETAINED, {"verifier_report_digest": A})
    st = world.control.store.get(S)
    assert world.control.cas(S, st.generation - 1, R.CREDENTIAL_PARKED, R.RETAINED,
                             {"verifier_report_digest": A}) == g1
    with pytest.raises(Refused) as e:
        world.control.cas(S, st.generation, R.RETAINED, R.RETAINED, {"verifier_report_digest": B})
    assert e.value.name == "report-immutable"


def test_refused_name_alone_rides_a_field_only_cas_on_rendered(world):  # noqa: F811
    """Head ruling (A0 widen): a verifier clone failure happens in RENDERED; its refusal NAME is recorded there, so
    the follower reads ``clone-failed`` instead of END's generic ``no-report:RENDERED``."""
    _put(world, R.RENDERED)
    _cas(world, R.RENDERED, R.RENDERED, {"verifier_report_refused": "clone-failed"})
    assert _fields(world) == {"verifier_report_refused": "clone-failed"}
    assert world.control.store.get(S).state == R.RENDERED
    gen = world.control.store.get(S).generation
    _cas(world, R.RENDERED, R.RENDERED, {"verifier_report_refused": "clone-failed"})   # the same value: a retry
    assert _fields(world) == {"verifier_report_refused": "clone-failed"}
    assert world.control.store.get(S).generation == gen + 1


@pytest.mark.parametrize("fields", [{"verifier_report_digest": A},
                                    {"verifier_report_digest": A, "verifier_report_refused": "clone-failed"},
                                    {"verifier_report_refused": "clone-failed", "failed_step": "x"}])
def test_rendered_takes_the_refused_name_and_nothing_else(world, fields):  # noqa: F811
    """On RENDERED only ``verifier_report_refused`` ALONE: never the digest, never both, nothing beside it."""
    _put(world, R.RENDERED)
    with pytest.raises(Refused) as e:
        _cas(world, R.RENDERED, R.RENDERED, fields)
    assert e.value.name == "edge-not-allowed"
    assert _fields(world) == {}
    assert world.control.store.get(S).generation == 0


@pytest.mark.parametrize("state", [R.CREDENTIAL_PARKED, R.BRAKED, R.ALLOCATED, R.REVOKED])
def test_refused_name_field_only_on_other_states_still_refuses(world, state):  # noqa: F811
    _put(world, state)
    with pytest.raises(Refused) as e:
        _cas(world, state, state, {"verifier_report_refused": "clone-failed"})
    assert e.value.name == "edge-not-allowed"
    assert _fields(world) == {}


def test_executor_record_refused_name_on_rendered_is_refused(world):  # noqa: F811
    _put(world, R.RENDERED, kind="interactive")
    with pytest.raises(Refused) as e:
        _cas(world, R.RENDERED, R.RENDERED, {"verifier_report_refused": "clone-failed"})
    assert e.value.name == "report-not-verifier"
    assert _fields(world) == {}


def test_rendered_refused_name_is_set_once(world):  # noqa: F811
    _put(world, R.RENDERED, fields={"verifier_report_refused": "clone-failed"})
    with pytest.raises(Refused) as e:
        _cas(world, R.RENDERED, R.RENDERED, {"verifier_report_refused": "clone-check"})
    assert e.value.name == "report-immutable"
    with pytest.raises(Refused) as e:                                      # a removal
        _cas(world, R.RENDERED, R.RENDERED, {"verifier_report_refused": None})
    assert e.value.name == "report-immutable"
    assert _fields(world) == {"verifier_report_refused": "clone-failed"}
    assert world.control.store.get(S).generation == 0
    # the set name still rides END's move into HELD unchanged, and a digest there is still the pair member
    with pytest.raises(Refused) as e:
        _cas(world, R.RENDERED, R.REVOKED, {})
        _cas(world, R.REVOKED, R.HELD, {"verifier_report_digest": A})
    assert e.value.name == "report-immutable"
    assert _fields(world) == {"verifier_report_refused": "clone-failed"}


def test_set_refused_name_cannot_change_or_take_a_digest(world):  # noqa: F811
    _put(world, R.ACTIVE, fields={"verifier_report_refused": "driver-timeout"})
    for f in ({"verifier_report_refused": "driver-exit:2"}, {"verifier_report_digest": A}):
        with pytest.raises(Refused) as e:
            _cas(world, R.ACTIVE, R.ACTIVE, f)
        assert e.value.name == "report-immutable"
    _cas(world, R.ACTIVE, R.ACTIVE, {"verifier_report_refused": "driver-timeout", "failed_step": "x"})
