"""A1c1 part 2: the D57 reaper's pass over a ``levain-autonomic/3`` store (autogrant DESIGN r7.5, rev 7.2 I2/I12) and
R14-10 (the lease test before an orphan is adopted). The leases are V's (``RunJournal._take_lease``), on the store's
own directory."""
from __future__ import annotations

import json
import os

import pytest

from levain.autonomic import schema3 as s
from levain.autonomic.journal import RunJournal

EFF_SEAL = json.dumps({"posture": "ON_LOOP", "by": "on-loop", "floor": [], "grant_guard": [], "grant": None,
                       "cover": "none"})


def seal(links=2):
    return json.dumps({"context_id": "c", "confirm_class": False, "guard": [], "links": links})


def rec(rid, subject, ts):
    return {"id": rid, "kind": "gate", "subject": subject, "fired": ts == "fired",
            "action_face": {"terminal_state": ts}}


def record_for(subject, sealed, ts, reason):
    assert sealed is not None
    return rec(f"reap:{subject}", subject, ts)


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


@pytest.fixture
def leases(store):
    return RunJournal(store.directory)   # its lease files only: V's journal never opens the /3 database here


def run(c, run_id, *, links=2, started=True, binding="b"):
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, ?, 0, ?)", (run_id, binding, seal(links)))
    if started:
        c.execute("UPDATE runs SET started_at = 't' WHERE run_id = ?", (run_id,))


def intent(c, run_id, effect_id="link-0"):
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES (?, ?, 'd', 'intent', ?)",
              (run_id, effect_id, EFF_SEAL))


def end(c, run_id, effect_id, state):
    ts = "fired" if state == "done" else "outcome_unknown"
    subj = f"effect:{run_id}:{effect_id}"
    s.close_subject(c, subject=subj, record=rec(f"end:{subj}", subj, ts), state=state)


def forge_orphan(store, subject, rid, ts):
    # raw DML past the close path (the R13-1 forgery): a receipt row with no terminal row
    with store.write() as c:
        seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM gate_receipts").fetchone()[0]
        c.execute("INSERT INTO closing (subject) VALUES (?)", (subject,))
        c.execute("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES (?, ?, ?, ?)",
                  (rid, subject, json.dumps(rec(rid, subject, ts)), seq))
        c.execute("DELETE FROM closing WHERE subject = ?", (subject,))


# ---- R14-10: the lease test comes before the adoption -----------------------------------------------------------------
def test_an_orphan_on_an_effect_in_flight_is_not_adopted_until_its_owner_is_gone(store, leases):
    with store.write() as c:
        run(c, "r1")
        intent(c, "r1")
    forge_orphan(store, "effect:r1:link-0", "orph-e", "fired")
    fd = leases._take_lease("r1", "link-0")                      # the owner, alive inside the effect
    # RED (rev 7.4's adoption, no lease test): the reaper ends an effect its owner is still inside
    with store.write() as c:
        c.execute("SAVEPOINT red")
        s.adopt_orphan(c, "orph-e", "effect:r1:link-0", "fired", lease_held=lambda r, e: False)
        assert c.execute("SELECT state FROM effects").fetchone() == ("done",)
        c.execute("ROLLBACK TO red")
        c.execute("RELEASE red")
    with store.write() as c:
        assert s.reap_orphans(c, lease_held=leases._lease_held) == ([], {"effect:r1:link-0": s.IN_FLIGHT})
    with store.read() as c:
        assert c.execute("SELECT state, receipt_id FROM effects").fetchone() == ("intent", None)
    leases._drop_lease("r1", "link-0", fd)                       # the owner exits
    with store.write() as c:
        assert s.reap_orphans(c, lease_held=leases._lease_held) == ([], {"effect:r1:link-0": s.ADOPTED})
    with store.read() as c:
        assert c.execute("SELECT state, receipt_id FROM effects").fetchone() == ("done", "orph-e")
        assert s.invariant(c) == []


# ---- the pass ---------------------------------------------------------------------------------------------------------
def test_one_pass_closes_every_subject_that_cannot_progress_once(store, leases):
    with store.write() as c:
        run(c, "gone")
        intent(c, "gone")                                        # owner gone: unknown + outcome_unknown
        run(c, "live")
        intent(c, "live")                                        # owner alive: left in flight
        run(c, "fenced", started=False, binding="bf")            # fenced before it started: journal:fenced
        run(c, "half", binding="bf")                             # fenced mid-chain: link-0 of 2 done
        intent(c, "half")
        end(c, "half", "link-0", "done")
        run(c, "unk")                                            # an unknown link: it can never complete
        intent(c, "unk")
        end(c, "unk", "link-0", "unknown")
        run(c, "done1", links=1)                                 # complete: never an input
        intent(c, "done1")
        end(c, "done1", "link-0", "done")
        c.execute("INSERT INTO fences (binding_id, generation) VALUES ('bf', 1)")
        for i, pid in enumerate(("pgone", "plive")):
            c.execute("INSERT INTO manual_pendings (pending_id, seal, state, seq) VALUES (?, '{}', 'open', ?)",
                      (pid, i + 1))
            c.execute("UPDATE manual_pendings SET state = 'firing', decision = '{}' WHERE pending_id = ?", (pid,))
    fd = leases._take_lease("live", "link-0")
    try:
        out = s.reap(store, lease_held=leases._lease_held, pending_lease_held=lambda pid: pid == "plive",
                     record_for=record_for)
        assert out == {"effect:gone:link-0": s.CLOSED, "effect:live:link-0": s.IN_FLIGHT, "pending:pgone": s.CLOSED,
                       "pending:plive": s.IN_FLIGHT, "run:fenced": s.CLOSED, "run:half": s.CLOSED,
                       "run:gone": s.CLOSED, "run:unk": s.CLOSED}
        with store.read() as c:
            assert dict(c.execute("SELECT run_id, reason FROM cancels").fetchall()) == {
                "fenced": "journal:fenced", "half": "fenced-mid-chain", "unk": "unknown-link", "gone": "unknown-link"}
            assert c.execute("SELECT state FROM effects WHERE run_id = 'gone'").fetchone() == ("unknown",)
            assert c.execute("SELECT state FROM effects WHERE run_id = 'live'").fetchone() == ("intent",)
            assert dict(c.execute("SELECT pending_id, state FROM manual_pendings").fetchall()) == {
                "pgone": "terminal", "plive": "firing"}
            assert s.invariant(c) == []
        # a second pass closes nothing new; the live owners are still in flight
        assert s.reap(store, lease_held=leases._lease_held, pending_lease_held=lambda pid: pid == "plive",
                      record_for=record_for) == {"effect:live:link-0": s.IN_FLIGHT, "pending:plive": s.IN_FLIGHT}
    finally:
        os.close(fd)


def test_a_subject_whose_receipt_cannot_be_built_stays_live_and_the_pass_goes_on(store, leases):
    with store.write() as c:
        run(c, "a", started=False, binding="bf")
        run(c, "b", started=False, binding="bf")
        c.execute("INSERT INTO fences (binding_id, generation) VALUES ('bf', 1)")

    def flaky(subject, sealed, ts, reason):
        if subject == "run:a":
            raise ValueError("face_unbuildable")
        return record_for(subject, sealed, ts, reason)

    out = s.reap(store, lease_held=leases._lease_held, pending_lease_held=lambda pid: False, record_for=flaky)
    assert out == {"run:a": "refused: ValueError: face_unbuildable", "run:b": s.CLOSED}
    with store.read() as c:
        assert c.execute("SELECT run_id FROM cancels").fetchall() == [("b",)]
        assert s.invariant(c) == []
    assert s.reap(store, lease_held=leases._lease_held, pending_lease_held=lambda pid: False,
                  record_for=record_for) == {"run:a": s.CLOSED}
