"""S14 D0: CONTROL's integrity rule for ``artifact_digest`` / ``artifact_failed`` (design D8 (a); the twin of A0).

The rule (control.cas): the pair is accepted only on a field-only RETAINED -> RETAINED CAS, only on a record whose
``claims.kind`` is ``dispatch`` (the executor) or ``planner``; once either is set, a CAS naming another value, the other member of the
pair, or a removal is ``artifact-immutable``, while the same value is accepted (a retry stays idempotent). Every
refusal is fail-closed: nothing is written.
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


def _put(world, state, kind="dispatch", fields=None, session=S):  # noqa: F811
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


def test_d4_inverted_probe_moves_are_refused(world, request):  # noqa: F811
    """The probe's d4: field-only artifact CASes on a non-RETAINED verifier record were ACCEPTED and replaced."""
    T2.test_none_class_no_front_no_handle(world, request)
    st = world.state(S)
    assert st.claims.get("kind") == "verifier"
    for fields in ({"artifact_digest": A}, {"artifact_failed": "adopt-cap:memory"}):
        st = world.state(S)
        with pytest.raises(Refused) as e:
            world.control.cas(S, st.generation, st.state, st.state, fields)
        assert e.value.name == "edge-not-allowed"
    assert not (R.ARTIFACT_FIELDS & set(world.state(S).fields))


@pytest.mark.parametrize("kind", ["dispatch", "planner"])
def test_field_only_retained_cas_accepted_once_and_retry_idempotent(world, kind):  # noqa: F811
    _put(world, R.RETAINED, kind=kind)
    _cas(world, R.RETAINED, R.RETAINED, {"artifact_digest": A})
    assert _fields(world)["artifact_digest"] == A
    assert world.control.store.get(S).event_kind == "artifact"
    _cas(world, R.RETAINED, R.RETAINED, {"artifact_digest": A})      # same value: a retry
    assert _fields(world)["artifact_digest"] == A


def test_failed_member_set_once_and_retry_idempotent(world):  # noqa: F811
    _put(world, R.RETAINED)
    _cas(world, R.RETAINED, R.RETAINED, {"artifact_failed": "adopt-cap:memory"})
    _cas(world, R.RETAINED, R.RETAINED, {"artifact_failed": "adopt-cap:memory"})
    assert _fields(world) == {"artifact_failed": "adopt-cap:memory"}


@pytest.mark.parametrize("kind", ["verifier", "interactive", "login"])
def test_other_kinds_refused(world, kind):  # noqa: F811
    _put(world, R.RETAINED, kind=kind)
    with pytest.raises(Refused) as e:
        _cas(world, R.RETAINED, R.RETAINED, {"artifact_digest": A})
    assert e.value.name == "artifact-not-executor"
    assert _fields(world) == {}


@pytest.mark.parametrize("state", [R.RENDERING, R.RENDERED, R.ACTIVE, R.BRAKED, R.HELD])
def test_other_states_refused(world, state):  # noqa: F811
    _put(world, state)
    with pytest.raises(Refused) as e:
        _cas(world, state, state, {"artifact_digest": A})
    assert e.value.name == "edge-not-allowed"
    assert _fields(world) == {}


@pytest.mark.parametrize("frm,to", [(R.ACTIVE, R.REVOKED), (R.REVOKED, R.FROZEN), (R.ACTIVE, R.RETAINED)])
def test_state_moving_edges_refused(world, frm, to):  # noqa: F811
    _put(world, frm)
    with pytest.raises(Refused) as e:
        _cas(world, frm, to, {"artifact_digest": A})
    assert e.value.name == "edge-not-allowed"
    assert world.control.store.get(S).state == frm


@pytest.mark.parametrize("second", [
    {"artifact_digest": B},
    {"artifact_digest": None},
    {"artifact_failed": "adopt-cap:memory"},
    {"artifact_digest": None, "artifact_failed": "adopt-cap:memory"},
])
def test_set_digest_is_immutable(world, second):  # noqa: F811
    _put(world, R.RETAINED, fields={"artifact_digest": A})
    with pytest.raises(Refused) as e:
        _cas(world, R.RETAINED, R.RETAINED, second)
    assert e.value.name == "artifact-immutable"
    assert _fields(world) == {"artifact_digest": A}


@pytest.mark.parametrize("second", [
    {"artifact_failed": "other"},
    {"artifact_failed": None},
    {"artifact_digest": A},
])
def test_set_failed_is_immutable(world, second):  # noqa: F811
    _put(world, R.RETAINED, fields={"artifact_failed": "adopt-cap:memory"})
    with pytest.raises(Refused) as e:
        _cas(world, R.RETAINED, R.RETAINED, second)
    assert e.value.name == "artifact-immutable"
    assert _fields(world) == {"artifact_failed": "adopt-cap:memory"}


def test_unrelated_field_cas_unaffected_by_set_artifact(world):  # noqa: F811
    _put(world, R.RETAINED, fields={"artifact_digest": A})
    _cas(world, R.RETAINED, R.RETAINED, {"held_attempts": 2})
    assert _fields(world)["artifact_digest"] == A
