"""S1 3c-8 slice 2, the L3 r1 fix round (1025-23/3c8s2/RULINGS_R1.md): CONTROL's event journal and stream.

Each test was RED on af2256d6 (the slice-2 tip) and names the ruling item it holds. The rig is test_slot_control_events'
(CONTROL through `Control.handle`, the event journal on disk beside the records)."""
from __future__ import annotations

import pytest

from levain.slot import events as EV
from levain.slot import records as R
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_slot_control_events import DIG, ERig, cas, erig, to_retained  # noqa: F401 - erig is a fixture
from tests.test_tickets import SESSION, Clock, bound_session, root


# ---- item 1: a prune failure after a landed append -----------------------------------------------------------------

def test_a_prune_failure_after_the_append_never_fails_the_verb(erig):
    """The append that reaches PRUNE_EVERY is fsynced and kept before the prune runs: a prune that raises `OSError`
    (a full disk writing `events.jsonl.new`) is recorded, never raised, so ATTACH answers ok with its generation-0
    event kept once, and the next write appends no event twice."""
    def boom() -> int:
        raise OSError(28, "No space left on device")

    erig.events._prune_locked = boom
    erig.events._since_prune = EV.PRUNE_EVERY - 1
    _ticket, _run, att = bound_session(erig)
    assert att["ok"], att
    assert [(e["generation"], e["state"]) for e in erig.session_events()] == [(0, R.ALLOCATED)]
    assert erig.control.owed_events() == {}
    assert [s for s in erig.control.seam_errors if s[0] == "event-prune"], erig.control.seam_errors
    cas(erig, R.ALLOCATED, R.RENDERING)
    assert [(e["generation"], e["state"]) for e in erig.session_events()] == [(0, R.ALLOCATED), (1, R.RENDERING)]
