"""R2: the reaper's pass (reaper DESIGN §4.3), on lane F2's ``World`` (a tmp store, the REAL CONTROL) and the fake
process table of the start tests. Each test is a crash state the base leaves where it is, because nothing runs a pass.
"""
from __future__ import annotations

import fcntl
import os

from levain.slot import records as R
from levain.slot import runtime_helper as H
from tests.test_slot_s2f3_start import E0, _started, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check


def _reaper(w):
    from levain.slot import reaper as RP

    return RP.Reaper(w.seams, tuning=RP.Tuning(reserve_bytes=4096))


def _lease_to(w, slot, session: str) -> R.SlotState:
    """The slot state ``prepare``'s lease writes (§3.5 ORDER): leased to ``session`` at ``lease_seq + 1``."""
    from dataclasses import replace

    old = w.slot_state(slot)
    new = replace(old, rev=old.rev + 1, lease_seq=old.lease_seq + 1, state="leased", session=session)
    (slot.slot_dir(w.L) / "state").write_bytes(R.slot_state_bytes(new))
    return new


def test_dead_writer_new_temp_swept_live_one_kept(tmp_path):
    """A helper killed inside ``create_alloc`` leaves ``<session>.json.<16 hex>.new`` beside a slot leased to the
    session, with no alloc. While the slot lock is held (a live ``prepare`` holds it from the lease write until ATTACH
    answers) the temp is kept; once the lock is free its writer is provably dead, and one pass removes it and reverts
    the orphan lease (step 3a)."""
    w, la, table = _world(tmp_path)
    s = "s-00000000000000aa"
    _lease_to(w, E0, s)
    temp = w.L / "alloc" / f"{s}.json.0123456789abcdef.new"
    temp.write_bytes(b'{"partial":')
    rp = _reaper(w)

    lock = os.open(E0.slot_dir(w.L) / "lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)            # the live writer's hold
        rp.run_pass()
        assert temp.exists()
        assert w.slot_state(E0).state == "leased"
    finally:
        os.close(lock)

    report = rp.run_pass()
    assert not temp.exists()
    assert temp.name in report["swept"]
    after = w.slot_state(E0)
    assert (after.state, after.session) == ("reusable", None)


def test_retained_leased_slot_whose_scrub_lost_the_pool_try_is_scrubbed_again(tmp_path):
    """RH5: a teardown whose SCRUB lost its pool TRY leaves the record RETAINED and the slot leased to it with no
    process. The next pass's rule "leased + RETAINED + alloc -> SCRUB again" releases it."""
    w, la, table = _world(tmp_path)
    s = _started(w, la, table)
    pool = os.open(w.L / "slots" / "pool.lock", os.O_RDWR)
    try:
        fcntl.flock(pool, fcntl.LOCK_EX | fcntl.LOCK_NB)            # a prepare holds the pool lock
        assert w.main(["teardown", s])[1] == {"ok": True, "state": R.RETAINED, "released": False}
    finally:
        os.close(pool)
    assert w.state(s).state == R.RETAINED
    held = w.slot_state(E0)
    assert (held.state, held.session) == ("leased", s)
    assert (w.L / "alloc" / f"{s}.json").exists()

    report = _reaper(w).run_pass()
    after = w.slot_state(E0)
    assert (after.state, after.session, after.rev) == ("reusable", None, held.rev + 1)
    assert s in report["scrubbed"]
    assert H.parse_slot_state_of((E0.slot_dir(w.L) / "state").read_bytes(), E0) == after
