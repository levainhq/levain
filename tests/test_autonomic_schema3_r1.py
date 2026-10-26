"""The ``levain-autonomic/3`` store, the A1c1 L3 round-1 findings (levain project_memory ``1026-22/a1c1/L3_r1.txt``,
``L3_r1_deepseek.txt``; head triage ``1026-22/RULINGS.md`` "A1c1 L3 r1 triage"). Each finding has a case that was RED on
the round-1 code; a case that code already refused is kept beside it as a pin."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.autonomic import db as vdb
from levain.autonomic import schema3 as s


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


RUN_SEAL = canon({"context_id": "c", "confirm_class": False, "guard": [], "links": 1})
RUN_SEAL_2 = canon({"context_id": "c", "confirm_class": False, "guard": [], "links": 2})
EFF_SEAL = canon({"posture": "ON_LOOP", "by": "on-loop", "floor": [], "grant_guard": [], "grant": None, "cover": "none"})
DECIDE = "UPDATE holds SET decided = 1, decided_by = ?, decisions = decisions + 1 WHERE hold_id = ?"


def rec(rid, subject, ts, fired=None) -> dict:
    return {"id": rid, "kind": "gate", "subject": subject, "fired": (ts == "fired") if fired is None else fired,
            "action_face": {"terminal_state": ts, "guard": None, "grant_guard": None}}


def tx(c: sqlite3.Connection, *stmts) -> None:
    c.execute("BEGIN IMMEDIATE")
    try:
        for st in stmts:
            st(c) if callable(st) else c.execute(*st) if isinstance(st, tuple) else c.execute(st)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


def raw(store: s.AutonomicStore3) -> sqlite3.Connection:
    c = sqlite3.connect(store.path, isolation_level=None, timeout=0)
    c.execute("PRAGMA recursive_triggers = ON")
    return c


# ---- A1-R1: a legacy row is frozen ----------------------------------------------------------------------------------
@pytest.fixture
def legacy(tmp_path):
    """A /3 store whose legacy rows were stamped as the I15 upgrade stamps them: after :func:`ddl_v3`, before
    :func:`triggers_v3` (rev74 DETAIL step 3). ``old`` is V's run (no seal, unstarted) with its V-rejected hold, a
    stamped done effect and an UNSTAMPED intent effect (step 7: a poisoned intent stays intent, legacy 0, and the D57
    reaper closes it after the upgrade). ``old2`` is a legacy run given a seal and a start, so the effect refusal
    below does not lean on a legacy run's NULL seal; it holds a stamped unknown link."""
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA recursive_triggers = ON")
    c.execute("BEGIN")
    s._v2_schema(c)
    c.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (s.STORE_FORMAT_V3,))
    s.write_tunables(c)
    s.ddl_v3(c)
    c.execute("INSERT INTO runs (run_id, binding_id, generation, legacy) VALUES ('old', 'b', 0, 1)")
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal, started_at, legacy) "
              "VALUES ('old2', 'b', 0, ?, 't0', 1)", (RUN_SEAL_2,))
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, receipt_id, legacy) "
              "VALUES ('old', 'link-0', 'd', 'done', 'v-r0', 1)")
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state) VALUES ('old', 'link-1', 'd', 'intent')")
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, receipt_id, legacy) "
              "VALUES ('old2', 'link-0', 'd', 'unknown', 'v-r1', 1)")
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq, decided, decided_by, legacy) "
              "VALUES ('h-old', 'old', 'link-0', 'b', 'd', 1, 0, 'human', 1)")
    c.execute("INSERT INTO fences (binding_id, generation) VALUES ('b', 1)")
    s.triggers_v3(c)
    c.execute("COMMIT")
    assert s.fingerprint(c) == s.expected_fingerprint()
    yield c
    c.close()


@pytest.mark.parametrize("stmt", [
    "UPDATE runs SET started_at = 't1' WHERE run_id = 'old'",      # codex's resume of a frozen run
    "UPDATE runs SET binding_id = binding_id WHERE run_id = 'old2'",
    "UPDATE holds SET seq = seq WHERE hold_id = 'h-old'",
    "UPDATE effects SET result = 'x' WHERE run_id = 'old2'",
])
def test_every_update_of_a_legacy_row_is_refused(legacy, stmt):
    with pytest.raises(sqlite3.IntegrityError, match="I15|D54"):
        tx(legacy, stmt)


def test_a_legacy_run_gains_no_effect(legacy):
    with pytest.raises(sqlite3.IntegrityError, match="I15: a legacy run is frozen"):
        tx(legacy, ("INSERT INTO effects (run_id, effect_id, digest, state, seal) "
                    "VALUES ('old2', 'link-1', 'd', 'intent', ?)", (EFF_SEAL,)))


def test_the_reaper_selections_skip_legacy_rows_and_keep_the_unstamped_intent(legacy):
    assert legacy.execute(s.REAP_FENCED_RUNS).fetchall() == []
    assert legacy.execute(s.REAP_MIDCHAIN_RUNS).fetchall() == []
    assert legacy.execute(s.REAP_UNKNOWN_LINK_RUNS).fetchall() == []
    # the poisoned intent (legacy 0) in the legacy run is still the reaper's, and closes
    assert legacy.execute(s.REAP_EFFECTS).fetchall() == [("old", "link-1", None)]
    tx(legacy, lambda c: s.close_subject(c, subject="effect:old:link-1", state="unknown",
                                         record=rec("g-x", "effect:old:link-1", "outcome_unknown", fired=False)))
    assert legacy.execute(s.REAP_EFFECTS).fetchall() == []
    assert s.invariant(legacy) == []


# ---- A1-R2: a decision row is the hold's decision generation --------------------------------------------------------
def test_a_forged_decision_row_is_refused_and_does_not_restart_the_held_count(store):
    c = raw(store)
    subj = "request:p:link-0"
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq) "
        "VALUES ('h-p', 'p', 'link-0', 'b', 'd', 'pp', 1)",),
       (DECIDE, ("human", "h-p")))
    assert c.execute("SELECT hold_id, by, n FROM hold_decisions").fetchall() == [("h-p", "human", 1)]
    tx(c, lambda conn: s.count_held(conn, subj, "subject:fault"))
    for n in (2, 1):
        with pytest.raises(sqlite3.IntegrityError, match="R13-6|UNIQUE"):
            tx(c, ("INSERT INTO hold_decisions (hold_id, run_id, pending_id, by, n) VALUES ('h-p', 'p', 'pp', "
                   "'human', ?)", (n,)))
    tx(c, lambda conn: s.count_held(conn, subj, "subject:fault"))
    assert c.execute("SELECT n FROM held_counts WHERE subject = ?", (subj,)).fetchone() == (2,)
    c.close()


@pytest.mark.parametrize("stmt, why", [
    # a decision without its generation step, a generation step without a decision, a generation step backwards
    ("UPDATE holds SET decided = 1, decided_by = 'on-loop' WHERE hold_id = 'h-p'", "I3"),
    ("UPDATE holds SET decisions = decisions + 1 WHERE hold_id = 'h-p'", "I3"),
    ("UPDATE holds SET decided = 1, decided_by = 'x', decisions = decisions + 2 WHERE hold_id = 'h-p'", "I3"),
])
def test_the_decision_generation_moves_only_with_a_decision(store, stmt, why):
    c = raw(store)
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq) "
        "VALUES ('h-p', 'p', 'link-0', 'b', 'd', 'pp', 1)",))
    with pytest.raises(sqlite3.IntegrityError, match=why):
        tx(c, stmt)
    with pytest.raises(sqlite3.IntegrityError, match="I3: a hold is born undecided"):
        tx(c, "INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq, decisions) "
              "VALUES ('h-q', 'p', 'link-0', 'b', 'd', 'qq', 2, 1)")
    c.close()


# ---- A1-R4: dry_run_close's cleanup never masks CloseUnbuildable ----------------------------------------------------
class _SavepointGone:
    """A connection whose savepoint has gone (SQLITE_FULL / IOERR rolled the transaction back under it)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    @property
    def in_transaction(self) -> bool:
        return self._c.in_transaction

    def execute(self, sql, *a):
        if sql.startswith(("ROLLBACK TO", "RELEASE")):
            raise sqlite3.OperationalError("no such savepoint: dry_run_close")
        return self._c.execute(sql, *a)


def test_a_lost_savepoint_does_not_mask_close_unbuildable(store):
    def unbuildable(ts):
        raise ValueError(f"no face for {ts}")

    with pytest.raises(s.CloseUnbuildable, match="close_unbuildable:refused:ValueError"):
        with store.write() as c:
            c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r1', 'b', 0, ?)", (RUN_SEAL,))
            s.dry_run_close(_SavepointGone(c), "run:r1", unbuildable)
    # a close that succeeded with no savepoint left to undo it is never passed over: its writes would stay
    with pytest.raises(sqlite3.OperationalError, match="no such savepoint"):
        with store.write() as c:
            c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r2', 'b', 0, ?)", (RUN_SEAL,))
            s.dry_run_close(_SavepointGone(c), "run:r2", lambda ts: rec(f"g-{ts}", "run:r2", ts))
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


# ---- the small findings ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cols, vals", [
    ("state, decision", ("firing", "approve")),
    ("state, decision", ("open", "approve")),
    ("state, receipt_id", ("open", "g")),
])
def test_a_pending_is_born_open_unreceipted_undecided(store, cols, vals):
    c = raw(store)
    with pytest.raises(sqlite3.IntegrityError, match="born open"):
        tx(c, (f"INSERT INTO manual_pendings (pending_id, seal, seq, {cols}) VALUES ('p', 's', 1, ?, ?)", vals))
    tx(c, "INSERT INTO manual_pendings (pending_id, seal, state, seq) VALUES ('p', 's', 'open', 1)")
    c.close()


@pytest.mark.parametrize("marker", ["levain-autonomic/0", "levain-autonomic/" + "9" * 5000, "levain-autonomic/03",
                                    "levain-autonomic/\u00b3", "levain-autonomic/"])
def test_format_n_refuses_a_marker_no_writer_emits(marker):
    assert s.format_n(marker) is None
    assert s.format_n("levain-autonomic/3") == 3


def test_a_marker_with_an_unbounded_suffix_is_a_format_refusal(store):
    c = sqlite3.connect(store.path, isolation_level=None)
    c.execute("DROP TRIGGER meta_u")
    c.execute("UPDATE meta SET value = ? WHERE key = 'format'", ("levain-autonomic/" + "9" * 5000,))
    c.close()
    with pytest.raises(vdb.StoreFormatError, match="is not a levain-autonomic format"):
        s.AutonomicStore3(store.directory).meta("format")


@pytest.mark.parametrize("record", [{"kind": "gate"}, {"id": None, "kind": "gate"}])
def test_close_subject_names_a_record_with_no_id(store, record):
    with pytest.raises(ValueError, match="close_subject: the receipt record for run:r1 has no id"):
        with store.write() as c:
            c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r1', 'b', 0, ?)", (RUN_SEAL,))
            s.close_subject(c, subject="run:r1", record=record, reason="refused:x")


def test_the_private_marker_alias_is_gone():
    assert not hasattr(vdb, "_marker")
