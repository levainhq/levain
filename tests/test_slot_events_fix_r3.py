"""S1 3c-8 slice 2, the L3 r2 fix round (1025-25/3c8s2fix/RULINGS_R2.md, round 3). The rig is test_slot_control_events'."""
from __future__ import annotations

from levain.slot import Refused
from levain.slot import records as R
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_slot_control_events import cas, erig, to_retained  # noqa: F401 - erig is a fixture
from tests.test_tickets import SESSION, bound_session


def _owed_retained(erig):
    """The RETAINED generation's event append fails: it is owed, the lease still live."""
    bound_session(erig)
    cas(erig, R.ALLOCATED, R.RENDERING)
    real = erig.events.append

    def failing(ev):
        if ev.get("kind") != "pool":
            raise Refused("events-unavailable", "injected")
        return real(ev)

    from tests.test_slot_control_events import END_CHAIN
    from tests.test_tickets import root
    assert root(erig, "REVOKE", session=SESSION)["ok"]
    for frm, to in END_CHAIN[:-1]:
        cas(erig, frm, to)
    erig.events.append = failing
    cas(erig, R.CREDENTIAL_PARKED, R.RETAINED)
    rec = erig.control.store.get(SESSION)
    assert erig.control.owed_events() == {SESSION: rec.generation}
    return rec


def test_a_revoked_write_over_an_owed_generation_keeps_its_own_kind(erig):
    """R2 item c: the owed kind is carried only by the revoke-after-EOF `lease_live` write; a `state=REVOKED` write
    that stores `event_kind: none` keeps it."""
    rec = _owed_retained(erig)
    with erig.control.store.lock(SESSION):
        new = erig.control._write(rec, state=R.REVOKED, event_kind="none")
    assert new.event_kind == "none"
