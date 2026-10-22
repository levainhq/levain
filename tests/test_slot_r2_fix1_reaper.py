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


def test_an_unreadable_alloc_keeps_its_pending_kill_and_the_fast_retry(tmp_path, monkeypatch):
    # Reproduced on a4d011b0: the unreadable alloc's lease was evicted from first_seen and pending() went false, so
    # the retry waited a V11 tick.
    """CONTROL down, an ACTIVE egress session whose first front kill errored. On the next wake its alloc cannot be
    read: the lease it already saw stays pending, and an alloc that failed on its first read is pending too."""
    from levain.slot import reaper as RP
    from levain.slot import runtime_helper as H

    w, la, table = _world(tmp_path)
    s = _started(w, la, table)

    def down(path):
        raise Refused("control-down", str(path))
    w.seams.client_factory = down
    real_kill, real_read = SS.revoke_front, H.Store.read
    kills, unreadable = [], {"n": 0}

    def flaky_kill(ctx, a):
        kills.append(a.session)
        if len(kills) == 1:
            raise OSError(5, "Input/output error")
        return real_kill(ctx, a)

    def flaky_read(self, parts, name, **kw):
        if tuple(parts) == ("alloc",) and unreadable["n"]:
            unreadable["n"] -= 1
            raise OSError(5, "Input/output error")
        return real_read(self, parts, name, **kw)
    monkeypatch.setattr(SS, "revoke_front", flaky_kill)
    monkeypatch.setattr(H.Store, "read", flaky_read)

    rv = RP.Revoker(w.seams)
    unreadable["n"] = 1
    assert rv.wake()["outcomes"][s] == "alloc:OSError"
    assert rv.pending() and not kills
    assert rv.wake()["outcomes"][s] == "error:OSError"
    unreadable["n"] = 1
    assert rv.wake()["outcomes"][s] == "alloc:OSError"
    assert rv.pending() and len(rv.first_seen) == 1
    assert rv.wake()["outcomes"][s] == "front-ended"
    assert kills == [s, s] and not rv.pending()
