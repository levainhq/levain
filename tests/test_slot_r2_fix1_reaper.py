"""R2 fix round 1 (reaper L3 r1): one item that fails never ends the pass, and the revoker never records an outcome
that does not prove the front is gone. On lane F2's ``World`` and the fake process table of the start tests."""
from __future__ import annotations

from levain.slot import Refused
from levain.slot import start as SS
from levain.slot.ids import SlotId
from tests.test_slot_r2_reaper import _lease_to, _reaper
from tests.test_slot_s2f3_start import E0, _started, _world

E1 = SlotId("egress", 1)


def test_one_unreadable_slot_state_does_not_starve_the_next_slots_orphan_revert(tmp_path):
    """e0's state file is corrupt and e1 is ``leased`` to a session with no alloc and no record (a ``prepare`` that
    died before ATTACH). Step 3a's snapshot reads e0 first: its failure is named, and e1 is still reverted."""
    w, la, table = _world(tmp_path)
    _lease_to(w, E1, "s-00000000000000bb")
    (E0.slot_dir(w.L) / "state").write_bytes(b'{"not": "a slot state"')

    report = _reaper(w).run_pass()

    after = w.slot_state(E1)
    assert (after.state, after.session) == ("reusable", None)
    assert any(x.startswith("orphan:e0:") for x in report["skipped"]), report["skipped"]


def test_revoker_retries_a_front_kill_that_errored_while_control_is_down(tmp_path, monkeypatch):
    """CONTROL down, an ACTIVE egress session: the first front kill fails before it proves anything (an OSError,
    as from the ``front-revoked`` marker's fsync). That outcome proves nothing, so the next wake tries again and
    ends the front."""
    from levain.slot import reaper as RP

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    def down(path):
        raise Refused("control-down", str(path))
    w.seams.client_factory = down
    real = SS.revoke_front
    calls = []

    def flaky(ctx, a):
        calls.append(a.session)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        return real(ctx, a)
    monkeypatch.setattr(SS, "revoke_front", flaky)

    rv = RP.Revoker(w.seams)
    first = rv.wake()
    assert first["control"] == "down"
    assert first["outcomes"][s] == "error:OSError"
    assert rv.pending()

    second = rv.wake()
    assert calls == [s, s]
    assert second["outcomes"][s] == "front-ended"
    assert not rv.pending()
