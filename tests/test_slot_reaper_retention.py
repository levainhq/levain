"""S1 3c-8 slice 2: the reaper's dispatch retention (CONTRACT §3.5 Retention, §7.7 3c). A RETAINED session whose alloc
carries a ticket goes to TRASH through the store verb ``discard`` once dispatch acked its result (``artifact_acked``),
or once ``L/policy/dispatch_retain_days`` (default 2, floor 1) have passed since root's ``retained_at``, never after
``restored_at``.

The world is test_slot_rootverbs_discard_restore's scratch store (a real retained object, the store verb's real
journal, lock and renames) with its CONTROL fake answering the CAS; the step runs as the pass worker runs it
(:meth:`levain.slot.reaper.Reaper.dispatch_retention` on the worker's own root context). Root's real uids and the
installed reaper unit are 3c-10's VM run."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import records as R
from levain.slot import reaper as RP
from levain.slot import runtime_helper as RH
from levain.slot.ids import SlotId
from tests.test_slot_rootverbs_discard_restore import OTHER, S, UID, World

TICKET = "A" * 43                                     # a handle: 32 zero bytes, base64url unpadded
#: root's retained_at in the world (test_slot_rootverbs_discard_restore writes it)
RETAINED_AT = R.epoch_of("2026-10-01T00:00:00Z")
DAY = 24 * 3600


class RWorld(World):
    """``S`` a dispatch session (its alloc carries a ticket), ``OTHER`` an interactive one; both RETAINED."""

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.now = float(RETAINED_AT)
        self.seams.clock = lambda: self.now
        a = R.parse_alloc((self.L / "alloc" / f"{S}.json").read_bytes())
        d = R.Alloc(session=S, slot=SlotId("egress", 3), lease_seq=a.lease_seq, slot_rev=a.slot_rev,
                    slot_uid=a.slot_uid, slot_gid=a.slot_gid, kind="dispatch", vendor="claude", auth="brokered",
                    operator_uid=UID, supervisor_uid=UID + 1, staging_uid=UID, created_at=a.created_at,
                    boot_id=a.boot_id, ticket=TICKET)
        (self.L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(d))
        self.logs: list = []

    def record(self, s: str = S, **fields) -> None:
        (self.L / "sessions" / f"{s}.json").write_bytes(R.state_bytes(R.SessionState(
            session=s, generation=3, state=R.RETAINED, claims={}, lease_live=False, event_kind="none",
            updated_at="2026-10-01T00:00:00Z", fields=fields)))

    def step(self) -> dict:
        reaper = RP.Reaper(self.seams, log=self.logs.append)
        ctx = reaper.ctx()
        rep = RP._new_report("6f1c2a9e-1b2c-4d5e-8f90-0a1b2c3d4e5f", 1, self.now)
        try:
            reaper.dispatch_retention(ctx, rep)
        finally:
            ctx.close()
            ctx.store.close()
        return rep


@pytest.fixture
def rw(tmp_path):
    return RWorld(tmp_path)


def test_dispatch_session_trashed_after_ack(rw):
    """T:5/test_dispatch_session_trashed_after_ack: acked, the RETAINED dispatch session goes to TRASH on the next pass,
    long before its age; the operator's interactive session, acked or not, stays."""
    rw.record(artifact_acked=True)
    rw.record(OTHER, artifact_acked=True)
    rep = rw.step()
    assert rep["trashed"] == [S] and rep["skipped"] == []
    assert rw.where(S) == (R.TRASH, ["trash"]) and (rw.L / "trash" / S / "trashed_at").exists()
    assert rw.where(OTHER) == (R.RETAINED, ["retained"])
    assert rw.control.calls == [("CAS", R.RETAINED, R.TRASH)]
    assert rw.step()["trashed"] == []                                   # TRASH now: nothing more to do


def test_unacked_session_trashed_by_age_only(rw):
    """Not acked: kept until `dispatch_retain_days` (default 2) since `retained_at`, then trashed."""
    rw.record()
    rw.now = RETAINED_AT + 2 * DAY - 1
    assert rw.step()["trashed"] == [] and rw.where(S)[0] == R.RETAINED
    rw.now = RETAINED_AT + 2 * DAY
    assert rw.step()["trashed"] == [S] and rw.where(S)[0] == R.TRASH


@pytest.mark.parametrize("text,days", [(b"5\n", 5), (b"1", 1), (b"0\n", 1), (b"two\n", 2), (b"-3", 2), (b"", 2)])
def test_dispatch_retain_days_policy(rw, text, days):
    """The policy file: decimal days, at most one newline; the floor is 1; anything else is the default, logged."""
    (rw.L / "policy" / RP.DISPATCH_RETAIN_DAYS).write_bytes(text)
    rw.record()
    rw.now = RETAINED_AT + days * DAY - 1
    assert rw.step()["trashed"] == []
    rw.now = RETAINED_AT + days * DAY
    assert rw.step()["trashed"] == [S]
    if days == 2 and text != b"2":
        assert any("not decimal days" in m for m in rw.logs)


def test_restored_dispatch_session_kept(rw):
    """T:5/test_restored_dispatch_session_kept: a session the operator restored (`restored_at` in its retained object)
    is his: never trashed again, by ack or by age."""
    rw.record(artifact_acked=True)
    assert rw.step()["trashed"] == [S]
    assert rw.main("restore") == (RH.EXIT_OK, {"ok": True, "state": R.RETAINED})
    assert (rw.L / "retained" / S / "restored_at").exists()
    rw.record(artifact_acked=True)                                      # the record the fake CONTROL wrote, re-acked
    rw.now = RETAINED_AT + 365 * DAY
    assert rw.step()["trashed"] == [] and rw.where(S)[0] == R.RETAINED


def test_operator_discards_dispatch_session(rw):
    """T:5/test_operator_discards_dispatch_session: the operator's caller-bound discard reaches a dispatch session
    (alloc `operator_uid` is his on a one-operator machine), before any ack."""
    rw.record()
    assert rw.main("discard") == (RH.EXIT_OK, {"ok": True, "state": R.TRASH})


def test_no_retained_at_is_never_trashed_by_age(rw):
    (rw.L / "retained" / S / "retained_at").unlink()
    rw.record()
    rw.now = RETAINED_AT + 365 * DAY
    rep = rw.step()
    assert rep["trashed"] == [] and rep["skipped"] == [f"retain:{S}:no-retained-at"]


def test_a_busy_session_is_skipped_for_the_next_pass(rw):
    import fcntl
    import os

    rw.record(artifact_acked=True)
    fd = os.open(rw.L / "alloc" / f"{S}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rep = rw.step()
    finally:
        os.close(fd)
    assert rep["trashed"] == [] and rep["skipped"] == [f"retain:{S}:session-busy"]
    assert rw.step()["trashed"] == [S]


def test_run_pass_runs_the_retention_step():
    """The pass lists 3c as a step (no longer a `seam-unbuilt` skip)."""
    import inspect

    src = inspect.getsource(RP.Reaper.run_pass)
    assert '("3c", self.dispatch_retention)' in src and '"3c", "5"' not in src
    assert canon  # noqa: B018 - imported for parity with the world module


def test_a_restore_between_the_decision_and_the_discard_is_never_trashed(rw, monkeypatch):
    """L3 r1 fix 2 (codex 2): the operator's discard + restore landing after the pass read `restored_at` absent and
    before its discard takes the session lock. The retention discard re-checks `restored_at` under that lock, so the
    restored session stays RETAINED (and keeps its `restored_at`)."""
    from levain.slot import store_verbs as SV

    real = SV.session_lock
    fired = {"done": False}

    def interleaved(facts, seams):
        if not fired["done"]:
            fired["done"] = True
            assert rw.main("discard")[0] == RH.EXIT_OK
            assert rw.main("restore")[0] == RH.EXIT_OK
        return real(facts, seams)

    rw.record(artifact_acked=True)
    monkeypatch.setattr(SV, "session_lock", interleaved)
    rep = rw.step()
    assert fired["done"]
    assert rep["trashed"] == [] and rep["skipped"] == [f"retain:{S}:restored"]
    assert rw.where(S) == (R.RETAINED, ["retained"]) and (rw.L / "retained" / S / "restored_at").exists()


def test_a_discard_stopped_between_its_cas_and_its_journal_delete_is_finished(rw):
    """RULINGS_OBS / L3 r1 fix 8: the record is TRASH (the CAS landed) and the discard's journal is still at `commit`
    (the stop came before its delete). The pass finishes that journal: the delete runs, nothing moves back."""
    from levain.slot import root_intent as RI

    rw.record(artifact_acked=True)
    assert rw.step()["trashed"] == [S]
    journal = rw.L / "alloc" / f"{S}.intent.json"
    RI.write_root_intent(journal, RI.RootIntent("discard", "commit", "2026-10-01T00:00:00Z"))
    calls = len(rw.control.calls)
    rep = rw.step()
    assert not journal.exists() and rep["reconciled"] == [S] and rep["skipped"] == []
    assert rw.where(S) == (R.TRASH, ["trash"]) and len(rw.control.calls) == calls   # the CAS was already done
    assert rw.step()["reconciled"] == []


# ---- the 3c-8 slice 2 fix round 3 ----------------------------------------------------------------------------------

def test_the_store_verb_discard_runs_a_guard_under_the_session_lock(rw):
    """R2 item a: `store_verbs.discard(..., guard=)` calls the guard with the session lock held, a `Refused` it raises
    ends the verb before anything moves, and the reaper's retention discard is that call (one home for the order)."""
    import inspect

    from levain.slot import store_verbs as SV

    a = R.parse_alloc((rw.L / "alloc" / f"{S}.json").read_bytes())
    facts = SV.facts_from_alloc(a, rw.L)
    seams = SV.StoreSeams(cas=lambda *x, **k: None, owner_ok=rw.seams.trusted, now=lambda: R.timestamp(rw.now))
    rw.record()
    held = []

    def guard():
        try:
            with SV.session_lock(facts, seams):
                held.append(False)
        except RP.Refused as e:
            held.append(e.name == "session-busy")
        raise RP.Refused("restored", S)

    with pytest.raises(RP.Refused) as ei:
        SV.discard(facts, seams, guard=guard)
    assert ei.value.name == "restored" and held == [True]
    assert rw.where(S) == (R.RETAINED, ["retained"])
    assert "SV.discard(" in inspect.getsource(RP._retention_discard)
    assert "discard_plan" not in inspect.getsource(RP._retention_discard)



def test_a_discard_journal_gone_before_the_reconcile_reports_nothing(rw, monkeypatch):
    """R2 item d: `_finish_discard` reports a session only when `reconcile_store` did something (it answers None when
    the journal was gone by the time it held the lock)."""
    from levain.slot import root_intent as RI
    from levain.slot import store_verbs as SV

    rw.record(artifact_acked=True)
    assert rw.step()["trashed"] == [S]
    RI.write_root_intent(rw.L / "alloc" / f"{S}.intent.json", RI.RootIntent("discard", "commit", "2026-10-01T00:00:00Z"))
    monkeypatch.setattr(SV, "reconcile_store", lambda facts, seams, **kw: None)
    assert rw.step()["reconciled"] == []
