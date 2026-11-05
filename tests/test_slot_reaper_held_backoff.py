"""1105-17 heldbuild: a HELD session's retry follows the reaper's backoff, and a cause no redo can clear is
quarantined (derived from the record, never stored as a state).

The world is lane F2's ``World`` (a tmp store, the REAL CONTROL, the fake process table of the start tests). A
session is left HELD by a process that would not die, the process then gone and a retain journal left at ``init``;
the retain's ``tree`` step is the one that fails (a refusal this test names), so each reaper pass meets the session in
step 2 (the journal) and, when its backoff is due, in step 3 (END). ``E.reconcile_session`` and ``E.end_session`` are
spied on (they still run for real); the reaper's clock is the World's ``monotonic`` seam, set by hand."""
from __future__ import annotations

import hashlib
import os

from levain.slot import Refused
from levain.slot import end as end_mod
from levain.slot import reaper as RP
from levain.slot import records as R
from levain.slot import start as SS
from levain.slot.root_intent import RootIntent, write_root_intent
from tests.test_slot_r2_reaper import _reaper
from tests.test_slot_s2f3_start import E0, S0, _started, _world
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

NOW = "2026-11-05T12:00:00Z"
HOUR = 3600.0


class Held:
    """One session HELD, a journal pending, a clock, and counters on the two END entry points."""

    def __init__(self, tmp_path, monkeypatch, refusal="tree-changed"):
        self.w, la, self.table = _world(tmp_path)
        self.s = _started(self.w, la, self.table)
        self.clock = [1000.0]
        self.w.seams.monotonic = lambda: self.clock[0]
        self.rp = _reaper(self.w)
        self.reconciles: list[float] = []
        self.ends: list[float] = []
        self.refusal = [refusal]
        real_rec, real_end, real_tree = end_mod.reconcile_session, end_mod.end_session, end_mod._tree_step

        def rec(*a, **kw):
            self.reconciles.append(self.clock[0])
            return real_rec(*a, **kw)

        def end(*a, **kw):
            self.ends.append(self.clock[0])
            return real_end(*a, **kw)

        def tree_step(facts, seams):
            if self.refusal[0] is None:
                return real_tree(facts, seams)
            if isinstance(self.refusal[0], BaseException):    # a fault that is no refusal (EIO): no name to record
                raise self.refusal[0]
            raise Refused(self.refusal[0], "test")
        monkeypatch.setattr(end_mod, "reconcile_session", rec)
        monkeypatch.setattr(end_mod, "end_session", end)
        monkeypatch.setattr(end_mod, "_tree_step", tree_step)
        # HELD at kill by a process that will not die; then the process gone and a retain journal left at init
        self.table.add(70001, S0, immortal=True, cg=True)
        assert self.w.main(["teardown", self.s])[1] == {"ok": True, "state": R.HELD}
        self.table.procs.pop(70001)
        self.table.immortal.discard(70001)
        self.table.cg.discard(70001)
        ctx = self.rp.ctx()
        try:
            self.journal = SS.end_facts(ctx, self.rp._allocs(ctx)[self.s]).intent
        finally:
            ctx.close()
            ctx.store.close()
        write_root_intent(self.journal, RootIntent("retain", "init", NOW))
        self.reconciles.clear()
        self.ends.clear()

    def at(self, seconds: float):
        """Advance the clock to ``seconds`` past the start and run one pass."""
        self.clock[0] = 1000.0 + seconds
        return self.rp.run_pass()

    def record(self):
        return self.w.state(self.s)

    def fields(self):
        return dict(self.record().fields)

    def snapshot(self):
        """Names, sizes, content and mtimes of everything a quarantine must leave alone: ``live/``, ``retained/``
        and the journal."""
        out = {}
        for base in (self.w.L / "live", self.w.L / "retained"):
            for root, dirs, files in os.walk(base):
                for n in dirs + files:
                    p = os.path.join(root, n)
                    st = os.lstat(p)
                    digest = None
                    if os.path.isfile(p) and not os.path.islink(p):
                        with open(p, "rb") as fh:
                            digest = hashlib.sha256(fh.read()).hexdigest()
                    out[p] = (st.st_mode, st.st_size, st.st_mtime_ns, digest)
        out[str(self.journal)] = self.journal.read_bytes()
        return out


def test_step2_leaves_a_held_session_to_step3_and_its_backoff(tmp_path, monkeypatch):
    """OBSERVED on the unfixed code (RETURN): a pass at a time inside the backoff window redid the journal in step 2
    (the retry the backoff exists to prevent) without raising ``held_attempts``. Now step 2 skips a HELD session:
    END in step 3 is the one retry, due once, and the second pass 5 s later does nothing."""
    h = Held(tmp_path, monkeypatch)
    h.at(0)
    assert (h.reconciles, len(h.ends)) == ([], 1)
    tries = h.fields()["held_attempts"]
    h.at(5)
    assert (h.reconciles, len(h.ends)) == ([], 1)
    assert h.fields()["held_attempts"] == tries


def test_the_vm_shape_gets_two_tries_not_one_per_pass(tmp_path, monkeypatch):
    """The gaps of RETURN_vmsunlnk's uid 3979 (11 tries in 313 s, one per reaper pass and extra wake): 11 passes
    reach END twice, and step 2 never, because END's backoff (60 s doubling per ``held_attempts``) is honoured."""
    h = Held(tmp_path, monkeypatch)
    gaps = [58.8, 0.4, 8.3, 4.7, 60.4, 53.2, 0.4, 6.0, 60.5, 60.2]
    t = 0.0
    h.at(t)
    for g in gaps:
        t += g
        h.at(t)
    assert h.reconciles == []
    assert [round(x - 1000.0, 1) for x in h.ends] == [0.0, 132.6]


def test_a_permanent_cause_is_quarantined_after_three_tries_and_nothing_is_touched(tmp_path, monkeypatch):
    h = Held(tmp_path, monkeypatch, refusal="var-folders-residue")
    h.at(0)                                                  # try 2 (the teardown's HELD was try 1)
    assert h.fields()["held_reason"] == "var-folders-residue" and h.fields()["held_attempts"] == 2
    assert not end_mod.quarantined(h.fields())
    h.at(120)                                                # try 3, as the backoff allows
    assert h.fields()["held_attempts"] == 3 and end_mod.quarantined(h.fields())
    assert len(h.ends) == 2
    before, rec, gen = h.snapshot(), h.record(), h.record().generation
    for hours in range(1, 11):
        rep = h.at(120 + hours * HOUR)
        assert h.s in rep["held"]                            # doctor and the pass record still name it
    assert len(h.ends) == 2 and h.reconciles == []
    assert h.record().generation == gen and h.record() == rec  # no CAS
    assert h.snapshot() == before                            # live/, retained/ and the journal: untouched
    assert h.w.slot_state(E0).state == "leased"             # the slot stays out of use


def test_a_transient_cause_keeps_retrying_at_the_cap(tmp_path, monkeypatch):
    h = Held(tmp_path, monkeypatch, refusal="link-reader-failed")
    t = 0.0
    for i in range(10):
        h.at(t)
        t += HOUR + 1
    assert len(h.ends) == 10
    assert h.fields()["held_attempts"] >= 10 and not end_mod.quarantined(h.fields())
    h.at(t)
    assert len(h.ends) == 11                                 # still due once an hour, never quarantined


def test_a_budget_refusal_is_quarantined_at_its_first_recorded_try(tmp_path, monkeypatch):
    """A cap refusal fails identically on every redo and each try can hold the one reaper pass for up to V2 s."""
    h = Held(tmp_path, monkeypatch, refusal="manifest-inode-cap")
    h.at(0)
    assert h.fields()["held_reason"] == "manifest-inode-cap" and end_mod.quarantined(h.fields())
    before = h.snapshot()
    for hours in range(1, 11):
        h.at(hours * HOUR)
    assert len(h.ends) == 1 and h.reconciles == []
    assert h.snapshot() == before                            # the quiet period acts on nothing


def test_a_restarted_reaper_tries_a_quarantined_session_once_then_stops(tmp_path, monkeypatch):
    """The way back is a restart (DESIGN Q6: a new process has no ``next_try`` for it): one END on its first pass, and
    the cause still standing, quarantined again."""
    h = Held(tmp_path, monkeypatch, refusal="var-folders-residue")
    h.at(0)
    h.at(120)
    assert end_mod.quarantined(h.fields()) and len(h.ends) == 2
    h.rp = _reaper(h.w)                                      # the restart
    h.at(200)
    assert len(h.ends) == 3
    h.at(200 + 10 * HOUR)
    assert len(h.ends) == 3 and end_mod.quarantined(h.fields())


def test_a_fixed_cause_and_a_restart_retain_the_session_and_clear_the_reason(tmp_path, monkeypatch):
    h = Held(tmp_path, monkeypatch, refusal="var-folders-residue")
    h.at(0)
    h.at(120)
    assert end_mod.quarantined(h.fields())
    h.refusal[0] = None                                      # the cause fixed
    h.at(400)
    assert len(h.ends) == 2                                  # still quarantined in this process
    h.rp = _reaper(h.w)                                      # the restart
    h.at(500)
    rec = h.record()
    assert rec.state == R.RETAINED
    assert not {"failed_step", "held_attempts", "held_reason"} & set(rec.fields)


def test_a_transient_failure_clears_a_permanent_reason_an_earlier_try_left(tmp_path, monkeypatch):
    h = Held(tmp_path, monkeypatch, refusal="var-folders-residue")
    h.at(0)
    assert h.fields()["held_reason"] == "var-folders-residue"
    h.refusal[0] = OSError(5, "Input/output error")
    h.at(120)
    assert h.fields()["held_attempts"] == 3 and h.fields()["failed_step"] == "tree"
    assert "held_reason" not in h.fields()
    assert not end_mod.quarantined(h.fields())


def test_the_backoff_clock_is_set_from_the_record_a_reconcile_leaves(tmp_path, monkeypatch):
    """Step 2 still serves the non-HELD sessions: a reconcile whose proof fails CASes HELD (with the reason), and
    ``_held_by_reconcile`` starts the backoff from that record's fields, so step 3 of the same pass does not run END
    on it. A permanent reason at its limit is quarantined (never due), a transient one backs off 60 s * 2**(n-1)."""
    h = Held(tmp_path, monkeypatch)
    h.rp._back_off(h.s, {"held_attempts": 3, "held_reason": "slot-not-ours"})
    assert h.rp.next_try[h.s] == float("inf")
    h.rp._back_off(h.s, {"held_attempts": 3, "held_reason": "kill-unproven"})
    assert h.rp.next_try[h.s] == h.clock[0] + RP.HELD_BACKOFF_S * 4
    h.rp._back_off(h.s, {"held_attempts": 50})
    assert h.rp.next_try[h.s] == h.clock[0] + RP.V22_S
