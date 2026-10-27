"""O-p2-1 slice B1b: who may retry a link, and the witness that its predecessor was counted (levain project_memory
``1026-27/O-p2-1_DESIGN.md`` section 5 legality, section 7 tests 2/3/4/7/8, A1 + R2-1 the counted witness, R3-1 the typed
predicate, R3-2 + R4-2 the held branch, R3-3 + R4-4 + P1 the count arms, R3-4 the invariant, R4-1 ``hold_decision``,
10.Q1 ``not_started_receipts``).

/3 is unwired (R15-5): every retry here is written by the test into its own /3 store. The end-to-end test through the
real seal builder (R3-2) is slice B2's: no seal here is built by production code."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.autonomic import schema3 as s
from levain.autonomic.posture import Posture

SUBJ = "request:r:link-0"
LEGAL = "O-p2-1: attempt 1 opens a link"
COUNTED = "R2-1: a retry follows the count of its predecessor"
AUTHORITY = "R3-1/R3-2: a retry carries"
MISSING = object()
DROP = object()   # seal(cover=DROP): the key is removed (absent)


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def seal(**fields) -> str:
    # an effect seal; a field given DROP (or MISSING) is left out of the object: absent, not JSON null
    d = {"floor": [], "grant_guard": [], "by": "on-loop", "posture": "ON_LOOP", "grant": None, "cover": "none"}
    for k, v in fields.items():
        if v is DROP or v is MISSING:
            d.pop(k, None)
        else:
            d[k] = v
    return canon(d)




def rec(rid, subject, ts, fired=None) -> dict:
    return {"id": rid, "kind": "gate", "subject": subject, "fired": (ts == "fired") if fired is None else fired,
            "action_face": {"terminal_state": ts, "guard": None, "grant_guard": None}}


def raw(store: s.AutonomicStore3) -> sqlite3.Connection:
    c = sqlite3.connect(store.path, isolation_level=None, timeout=0)
    c.execute("PRAGMA recursive_triggers = ON")
    return c


def tx(c: sqlite3.Connection, *stmts) -> None:
    """One write transaction; raises (rolled back) on the first refusal."""
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


def started_run(c, run_id="r", links=1):
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, 'b', 0, ?)",
              (run_id, canon({"context_id": "c", "confirm_class": False, "guard": [], "links": links})))
    c.execute("UPDATE runs SET started_at = 't' WHERE run_id = ?", (run_id,))


def held_link(c, *, by="human", posture="CONFIRM"):
    # a hold on link-0, approved once (generation 1)
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
              "VALUES ('h', 'r', 'link-0', 'b', 'd', 1)")
    approve(c, by=by, posture=posture)


def approve(c, *, by="human", posture="CONFIRM"):
    c.execute("UPDATE holds SET decided = 1, decisions = decisions + 1, decided_by = ?, decided_posture = ? "
              "WHERE hold_id = 'h'", (by, posture))


REOPEN = ("UPDATE holds SET decided = NULL, decided_by = NULL, decided_posture = NULL, signer = NULL, "
          "signature = NULL, challenge = NULL, fence = NULL WHERE hold_id = 'h' AND decided = 1")   # V _reopen_in


def attempt(n, sl=None, digest="d"):
    sl = seal() if sl is None else sl
    return lambda c: c.execute("INSERT INTO effects (run_id, effect_id, digest, state, seal, attempt) "
                               "VALUES ('r', 'link-0', ?, 'intent', ?, ?)", (digest, sl, n))


def not_started(n, why="spawn"):
    def go(c):
        subj = s.effect_subject("r", "link-0", n)
        s.close_subject(c, subject=subj, record=rec(f"ns{n}", subj, "refused"), state="not_started",
                        result=f"not_started:{why}")
    return go


def count(n):
    return lambda c: s.count_held(c, SUBJ, "subject:not_started:spawn", attempt=n)


def held_row(c):
    return c.execute("SELECT n, attempt FROM held_counts WHERE subject = ?", (SUBJ,)).fetchone()


def rows(c):
    return c.execute("SELECT attempt, state FROM effects WHERE run_id = 'r' AND effect_id = 'link-0' "
                     "ORDER BY attempt").fetchall()


def first_attempt(c, sl=None, *, hold=False):
    # attempt 1 of link-0, ended not_started and counted, in one transaction (A1's order)
    tx(c, started_run, *((held_link,) if hold else ()), attempt(1, sl))
    tx(c, not_started(1), count(1))


def hand_built(tmp_path, build, name="hand") -> s.AutonomicStore3:
    """A /3 store whose rows ``build(c)`` are written between :func:`ddl_v3` and :func:`triggers_v3` (the window the
    I15 upgrade stamps legacy rows in): a test that needs a witness the triggers would refuse writes it here, so ONE
    rule is left to refuse what the test inserts afterwards."""
    st = s.AutonomicStore3(tmp_path / name)
    st.directory.mkdir(parents=True)
    c = sqlite3.connect(st.path, isolation_level=None)
    try:
        c.execute("BEGIN")
        s._v2_schema(c)
        c.execute("INSERT INTO meta (key, value) VALUES ('store_id', 'store-hand')")
        c.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (s.STORE_FORMAT_V3,))
        s.write_tunables(c)
        s.ddl_v3(c)
        build(c)
        s.triggers_v3(c)
        c.execute("COMMIT")
        assert s.fingerprint(c) == s.expected_fingerprint()
    finally:
        c.close()
    st.meta("format")
    return st


def ended_link(state, ts, *, digest="d", hold_decided=None):
    # attempt 1 of link-0 in `state` with its receipt, and a count at n = 1 naming attempt 1 (the witness a real
    # count could not write for an ended attempt), so only the legality / authority rule is left to refuse a retry
    def build(c):
        c.execute("INSERT INTO runs (run_id, binding_id, generation, seal, started_at) VALUES ('r', 'b', 0, ?, 't')",
                  (canon({"context_id": "c", "confirm_class": False, "guard": [], "links": 1}),))
        subj = s.effect_subject("r", "link-0")
        c.execute("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES ('e1', ?, ?, 1)",
                  (subj, canon(rec("e1", subj, ts))))
        c.execute("INSERT INTO effects (run_id, effect_id, digest, state, result, receipt_id, seal, attempt) "
                  "VALUES ('r', 'link-0', ?, ?, ?, 'e1', ?, 1)",
                  (digest, state, "not_started:x" if state == "not_started" else None, seal()))
        c.execute("INSERT INTO held_counts (subject, n, last_reason, mark, attempt) VALUES (?, 1, 'subject:x', 1, 1)",
                  (SUBJ,))
        if hold_decided is not None:
            c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq, decided, decided_by, "
                      "decisions) VALUES ('h', 'r', 'link-0', 'b', 'd', 1, ?, 'human', 1)", (hold_decided,))
    return build


# ---- section 5 legality: a retry follows a not_started predecessor of the same digest ---------------------------------
def test_a_retry_on_a_counted_not_started_predecessor_is_admitted_and_closes_as_its_own_attempt(store):
    c = raw(store)
    first_attempt(c)
    tx(c, attempt(2))
    tx(c, not_started(2), count(2))
    tx(c, attempt(3))
    assert rows(c) == [(1, "not_started"), (2, "not_started"), (3, "intent")]
    assert held_row(c) == (2, 2)
    c.close()


@pytest.mark.parametrize("state, ts", [("done", "fired"), ("unknown", "outcome_unknown")])
def test_a_retry_after_its_predecessor_ran_is_refused(tmp_path, state, ts):
    c = raw(hand_built(tmp_path, ended_link(state, ts)))
    with pytest.raises(sqlite3.IntegrityError, match=LEGAL):
        tx(c, attempt(2))
    c.close()


def test_a_retry_with_another_digest_is_refused(tmp_path):
    c = raw(hand_built(tmp_path, ended_link("not_started", "refused")))
    with pytest.raises(sqlite3.IntegrityError, match=LEGAL):
        tx(c, attempt(2, digest="other"))
    tx(c, attempt(2))   # the same rows, the same digest: admitted (the refusal above was the digest)
    c.close()


def test_a_retry_that_skips_an_attempt_number_is_refused(tmp_path):
    # no attempt 2 to follow: the legality, witness and authority rules each refuse it (the first to fire raises)
    c = raw(hand_built(tmp_path, ended_link("not_started", "refused")))
    with pytest.raises(sqlite3.IntegrityError, match=f"{LEGAL}|{COUNTED}|{AUTHORITY}"):
        tx(c, attempt(3))
    c.close()


def test_a_retry_must_follow_the_links_latest_attempt(tmp_path):
    # attempts 1 and 3 exist (a gap only a hand-built store can hold); attempt 2 follows a counted, authorised
    # not_started attempt 1, so only "attempt n - 1 is the LATEST" is left to refuse it
    def build(c):
        ended_link("not_started", "refused")(c)
        subj = s.effect_subject("r", "link-0", 3)
        c.execute("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES ('e3', ?, ?, 3)",
                  (subj, canon(rec("e3", subj, "refused"))))
        c.execute("INSERT INTO effects (run_id, effect_id, digest, state, result, receipt_id, seal, attempt) "
                  "VALUES ('r', 'link-0', 'd', 'not_started', 'not_started:x', 'e3', ?, 3)", (seal(),))
    c = raw(hand_built(tmp_path, build))
    with pytest.raises(sqlite3.IntegrityError, match=LEGAL):
        tx(c, attempt(2))
    c.close()


def test_a_second_attempt_1_is_still_refused(store):
    c = raw(store)
    first_attempt(c)
    with pytest.raises(sqlite3.IntegrityError, match=f"D69|{LEGAL}"):
        tx(c, attempt(1))
    c.close()


# ---- A1 / R2-1: the predecessor was COUNTED, under the bound ----------------------------------------------------------
def test_a_retry_whose_predecessor_was_never_counted_is_refused(store):
    c = raw(store)
    tx(c, started_run, attempt(1))
    tx(c, not_started(1))
    with pytest.raises(sqlite3.IntegrityError, match=COUNTED):
        tx(c, attempt(2))
    c.close()


def test_a_skipped_count_refuses_the_next_retry(store):
    # R2-1's RED: attempts 1 and 2 close not_started, attempt 2 is not counted, attempt 3 is refused
    c = raw(store)
    first_attempt(c)
    tx(c, attempt(2))
    tx(c, not_started(2))
    with pytest.raises(sqlite3.IntegrityError, match=COUNTED):
        tx(c, attempt(3))
    tx(c, count(2), attempt(3))
    c.close()


def test_a_retry_at_the_held_bound_is_refused(store):
    c = raw(store)
    first_attempt(c)
    for _ in range(s.HELD_LIMIT - 1):   # P1: an attempt-less fault at the same mark carries attempt 1 and counts
        tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    assert held_row(c) == (s.HELD_LIMIT, 1)
    with pytest.raises(sqlite3.IntegrityError, match=COUNTED):
        tx(c, attempt(2))
    c.close()


# ---- R3-3 / R4-4 / P1: the count's arms ----------------------------------------------------------------------------
def test_counting_one_attempt_twice_is_idempotent(store):
    c = raw(store)
    first_attempt(c)
    before = c.execute("SELECT * FROM held_counts").fetchall()
    got = []
    tx(c, lambda conn: got.append(s.count_held(conn, SUBJ, "subject:other:reason", attempt=1)))
    assert got == [1]
    assert c.execute("SELECT * FROM held_counts").fetchall() == before   # nothing written (not even the reason)
    c.close()


def test_an_attempt_less_count_carries_the_named_attempt_forward_and_counts(store):
    c = raw(store)
    first_attempt(c)
    got = []
    tx(c, lambda conn: got.append(s.count_held(conn, SUBJ, "subject:unavailable")))
    assert got == [2]
    assert held_row(c) == (2, 1)
    tx(c, attempt(2))   # still under the bound, still naming attempt 1
    c.close()


def test_a_same_mark_count_never_moves_its_attempt_back_or_drops_it(store):
    c = raw(store)
    first_attempt(c)
    tx(c, attempt(2))
    tx(c, not_started(2), count(2))
    assert held_row(c) == (2, 2)
    for stmt in ("UPDATE held_counts SET n = n + 1, attempt = 1", "UPDATE held_counts SET n = n + 1, attempt = NULL"):
        with pytest.raises(sqlite3.IntegrityError, match="I11"):
            tx(c, stmt)
    c.close()


def test_a_same_mark_count_never_names_a_lower_attempt_even_a_latest_one(tmp_path):
    # R3-3: the same-mark rise needs NEW.attempt > OLD.attempt. With real rows the latest-attempt witness already
    # refuses a lower one, so the count here names attempt 3 above the link's latest (2), hand-built
    def build(c):
        ended_link("not_started", "refused")(c)
        subj = s.effect_subject("r", "link-0", 2)
        c.execute("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES ('e2', ?, ?, 2)",
                  (subj, canon(rec("e2", subj, "refused"))))
        c.execute("INSERT INTO effects (run_id, effect_id, digest, state, result, receipt_id, seal, attempt) "
                  "VALUES ('r', 'link-0', 'd', 'not_started', 'not_started:x', 'e2', ?, 2)", (seal(),))
        c.execute("UPDATE held_counts SET attempt = 3, mark = " + s._progress(f"'{SUBJ}'"))
    c = raw(hand_built(tmp_path, build))
    with pytest.raises(sqlite3.IntegrityError, match="I11"):
        tx(c, "UPDATE held_counts SET n = n + 1, attempt = 2")
    c.close()


def test_a_null_count_takes_no_attempt_at_the_same_mark(store):
    # R4-4's explicit NULL arm: a count with no attempt rises only to another count with no attempt
    c = raw(store)
    tx(c, started_run, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    assert held_row(c) == (1, None)
    with pytest.raises(sqlite3.IntegrityError, match="I11"):
        tx(c, "UPDATE held_counts SET n = 2, attempt = 1")
    c.close()


@pytest.mark.parametrize("n", [1, 2])
def test_a_count_names_only_the_links_latest_not_started_attempt(store, n):
    c = raw(store)
    tx(c, started_run, attempt(1))
    mark = c.execute(f"SELECT {s._progress(':s')}", {"s": SUBJ}).fetchone()[0]
    if n == 1:   # attempt 1 is still live: count_held refuses first (F12), and so does the store
        with pytest.raises(ValueError, match="latest not_started attempt"):
            tx(c, count(1))
        with pytest.raises(sqlite3.IntegrityError, match="I11"):
            tx(c, ("INSERT INTO held_counts (subject, n, last_reason, mark, attempt) VALUES (?, 1, 'subject:x', ?, 1)",
                   (SUBJ, mark)))
        c.close()
        return
    tx(c, not_started(1), count(1), attempt(2))
    tx(c, not_started(2))
    # attempt 3 does not exist: refused by count_held and by the store. (Naming attempt 1 again at the same mark is
    # the P1 carry, which F2 admits: test_an_attempt_less_fault_after_an_uncounted_retry_ended_still_counts)
    with pytest.raises(ValueError, match="latest not_started attempt"):
        tx(c, count(3))
    with pytest.raises(sqlite3.IntegrityError, match="I11"):
        tx(c, "UPDATE held_counts SET n = n + 1, attempt = 3")
    tx(c, count(2))
    assert held_row(c) == (2, 2)
    c.close()


def test_a_born_count_naming_an_attempt_needs_that_attempt_not_started_and_latest(store):
    c = raw(store)
    tx(c, started_run, attempt(1))
    tx(c, not_started(1))
    mark = c.execute(f"SELECT {s._progress(':s')}", {"s": SUBJ}).fetchone()[0]
    with pytest.raises(ValueError, match="latest not_started attempt"):
        tx(c, count(2))   # attempt 2 does not exist
    for bad in (2, 0):    # the store refuses the same rows written around count_held
        with pytest.raises(sqlite3.IntegrityError, match="I11"):
            tx(c, ("INSERT INTO held_counts (subject, n, last_reason, mark, attempt) VALUES (?, 1, 'subject:x', ?, ?)",
                   (SUBJ, mark, bad)))
    tx(c, count(1))
    assert held_row(c) == (1, 1)
    c.close()


@pytest.mark.parametrize("bad", [True, 0, -1, 1.0, "1"])
def test_count_held_refuses_an_attempt_that_is_not_an_int_from_1(store, bad):
    c = raw(store)
    first_attempt(c)
    with pytest.raises(ValueError, match="attempt"):
        tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:x", attempt=bad))
    c.close()


# ---- R3-1 + R4-2: a hold-less retry re-uses its predecessor's cover, at no lower posture ------------------------------
@pytest.mark.parametrize("first, retry", [
    (seal(grant=DROP, cover=DROP), seal(grant=DROP, cover=DROP)),     # both absent: the keys omitted
    (seal(grant=None, cover=None), seal(grant=None, cover=None)),     # both absent: JSON null
    (seal(grant=DROP, cover=DROP), seal(grant=None, cover=None)),     # one builder omits, one writes null
    (seal(cover="none"), seal(cover="none")),                         # the same cover text
    (seal(), seal(posture="COOLING_OFF", by="human")),                # a higher posture
])
def test_a_hold_less_retry_on_its_predecessors_authority_is_admitted(store, first, retry):
    c = raw(store)
    first_attempt(c, first)
    tx(c, attempt(2, retry))
    c.close()


@pytest.mark.parametrize("first, retry", [
    (seal(cover="none"), seal(cover=DROP)),                           # cover on one side only
    (seal(cover=None), seal(cover="none")),
    (seal(cover="none"), seal(cover="other")),                        # another cover
    (seal(cover="none"), seal(cover=1)),                              # a cover that is not text
    (seal(posture="COOLING_OFF", by="human"), seal(posture="ON_LOOP", by="human")),   # a lower posture
    (seal(), seal(posture=DROP)),                                     # posture absent
    (seal(), seal(posture=None)),                                     # posture JSON null
    (seal(), seal(posture="SIDEWAYS")),                               # posture not a Posture
    (seal(), seal(posture=1)),                                        # posture as its rank number
    (seal(posture="SIDEWAYS"), seal()),                               # the predecessor's posture unknown
])
def test_a_hold_less_retry_that_widens_or_loses_its_authority_is_refused(store, first, retry):
    c = raw(store)
    first_attempt(c, first)
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, retry))
    c.close()


def test_the_posture_rank_is_emitted_from_the_posture_enum():
    sql = s._rank("x")
    for p in Posture:
        assert f"WHEN '{p.name}' THEN {int(p)}" in sql
    assert "ELSE" not in sql


# ---- R3-2 + R4-2: a held link's retry carries the link's CURRENT decision ---------------------------------------------
GEN1 = {"generation": 1, "by": "human", "posture": "CONFIRM"}


def test_a_held_retry_on_the_current_decision_is_admitted(store):
    c = raw(store)
    first_attempt(c, seal(decision=GEN1), hold=True)
    tx(c, attempt(2, seal(decision=GEN1)))
    c.close()


@pytest.mark.parametrize("decision", [
    {**GEN1, "generation": True}, {**GEN1, "generation": 1.0}, {**GEN1, "generation": "1"},
    {k: v for k, v in GEN1.items() if k != "generation"}, {**GEN1, "generation": None},
    {**GEN1, "generation": 2},                                       # a generation the hold never took
    {**GEN1, "by": "on-loop"}, {**GEN1, "by": None}, {k: v for k, v in GEN1.items() if k != "by"},
    {**GEN1, "posture": "CONFIRM_ELEVATED"}, {**GEN1, "posture": None},
    {k: v for k, v in GEN1.items() if k != "posture"},
    MISSING,                                                          # no decision object at all
])
def test_a_held_retry_whose_decision_is_not_the_holds_is_refused(store, decision):
    c = raw(store)
    first_attempt(c, seal(decision=GEN1), hold=True)
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, seal(decision=decision)))
    c.close()


@pytest.mark.parametrize("decision", [
    {"generation": 1, "by": "on-loop"},                    # the posture key omitted
    {"generation": 1, "by": "on-loop", "posture": None},   # written as JSON null
])
def test_a_null_posture_hold_admits_its_retry_with_the_posture_omitted_or_null(store, decision):
    c = raw(store)
    tx(c, started_run, lambda conn: held_link(conn, by="on-loop", posture=None), attempt(1, seal(decision=decision)))
    tx(c, not_started(1), count(1))
    tx(c, attempt(2, seal(decision=decision)))
    c.close()


def test_a_null_posture_hold_refuses_a_retry_that_names_a_posture(store):
    c = raw(store)
    d = {"generation": 1, "by": "on-loop"}
    tx(c, started_run, lambda conn: held_link(conn, by="on-loop", posture=None), attempt(1, seal(decision=d)))
    tx(c, not_started(1), count(1))
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, seal(decision={**d, "posture": "ON_LOOP"})))
    c.close()


def test_a_retry_while_the_hold_is_reopened_is_refused_and_after_reapproval_only_the_new_generation(store):
    c = raw(store)
    first_attempt(c, seal(decision=GEN1), hold=True)
    tx(c, REOPEN)
    for d in (GEN1, {**GEN1, "generation": 2}):   # an open hold admits no retry, whatever the seal says
        with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
            tx(c, attempt(2, seal(decision=d)))
    tx(c, approve)
    assert s.hold_decision(c, "h") == (2, "human", "CONFIRM")
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, seal(decision=GEN1)))   # the stale seal of generation 1
    tx(c, attempt(2, seal(decision={**GEN1, "generation": 2})))
    c.close()


def test_a_retry_after_a_reject_is_refused(tmp_path):
    # a real reject is written only once the run is closed (holds_move), so the store would refuse any effect in it;
    # here the rejected hold is hand-built with a live run so the authority rule alone is left to refuse
    c = raw(hand_built(tmp_path, ended_link("not_started", "refused", hold_decided=0)))
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, seal(decision={"generation": 1, "by": "human", "posture": None})))
    c.close()


def test_a_held_retry_does_not_fall_back_to_the_hold_less_rule(tmp_path):
    # with a hold on the link, re-using the predecessor's cover and posture is not enough: the decision is required
    c = raw(hand_built(tmp_path, ended_link("not_started", "refused", hold_decided=1)))
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2))
    tx(c, attempt(2, seal(decision={"generation": 1, "by": "human", "posture": None})))
    c.close()


# ---- R2-1 / glm F4 / R3-4: the invariant names an uncounted not_started attempt of a LIVE link ------------------------
def uncounted(c):
    return [r for r in s.invariant(c) if r[0] == "not_started_uncounted"]


def test_the_invariant_names_a_live_links_uncounted_latest_attempt(store):
    c = raw(store)
    tx(c, started_run, attempt(1))
    tx(c, not_started(1))
    assert uncounted(c) == [("not_started_uncounted", "effect:r:link-0")]
    tx(c, count(1), attempt(2))
    assert uncounted(c) == []
    tx(c, not_started(2))
    assert uncounted(c) == [("not_started_uncounted", "effect:r:link-0#2")]
    tx(c, count(2))
    assert s.invariant(c) == []
    c.close()


def test_the_invariant_is_clean_after_the_run_is_cancelled(store):
    # R3-4's RED: the held triggers refuse a count once the run is closed, so the row would stand forever
    c = raw(store)
    tx(c, started_run, attempt(1))
    tx(c, not_started(1))
    assert uncounted(c) != []
    tx(c, lambda conn: s.close_subject(conn, subject="run:r", record=rec("cx", "run:r", "refused"),
                                       reason="operator_cancel"))
    assert s.invariant(c) == []
    c.close()


# ---- R4-1 / 10.Q1: the /3-only readers --------------------------------------------------------------------------------
def test_hold_decision_reads_the_holds_generation_decider_and_posture(store):
    c = raw(store)
    tx(c, started_run, ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
                        "VALUES ('h', 'r', 'link-0', 'b', 'd', 1)",))
    assert s.hold_decision(c, "h") == (0, None, None)
    tx(c, lambda conn: approve(conn, by="on-loop", posture=None))
    assert s.hold_decision(c, "h") == (1, "on-loop", None)
    assert s.hold_decision(c, "nope") is None
    c.close()


def test_not_started_receipts_are_the_receipts_of_not_started_rows(store):
    c = raw(store)
    assert s.not_started_receipts(c) == frozenset()
    first_attempt(c)
    tx(c, attempt(2))
    tx(c, not_started(2), count(2), attempt(3))
    tx(c, lambda conn: s.close_subject(conn, subject="effect:r:link-0#3", record=rec("f3", "effect:r:link-0#3", "fired"),
                                       state="done", result="ok"))
    assert s.not_started_receipts(c) == frozenset({"ns1", "ns2"})
    c.close()


# ---- fix round 1 (1027+9 op21fix): the L3 r1 findings, each RED first ------------------------------------------------
HOLD_A = ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
          "VALUES ('h', 'r', 'link-0', 'b', 'A', 1)")


def test_a_held_retry_under_a_hold_for_another_digest_is_refused(store):
    # F1 (codex HIGH): attempt 1 of payload B ends not_started and is counted; a hold for payload A on the same link
    # is approved; attempt 2 of B carrying A's decision must not run under A's approval
    c = raw(store)
    tx(c, started_run, attempt(1, digest="B"))
    tx(c, not_started(1), count(1))
    tx(c, HOLD_A, approve)
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, seal(decision=GEN1), digest="B"))
    # a held link whose only hold is for another payload does not fall back to the hold-less rule either
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, digest="B"))
    c.close()


def test_a_held_retry_under_the_hold_for_its_own_digest_is_admitted(store):
    c = raw(store)
    tx(c, started_run, attempt(1, digest="A"))
    tx(c, not_started(1), count(1))
    tx(c, HOLD_A, approve)
    tx(c, attempt(2, seal(decision=GEN1), digest="A"))
    c.close()


def test_an_attempt_less_fault_after_an_uncounted_retry_ended_still_counts(store):
    # F2 (complement MED): the carried attempt was witnessed when it was written; a later successor does not make the
    # carry illegal
    c = raw(store)
    first_attempt(c)
    tx(c, attempt(2))
    tx(c, not_started(2))
    tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    assert held_row(c) == (2, 1)
    tx(c, count(2))
    assert held_row(c) == (3, 2)
    tx(c, attempt(3))
    c.close()


def test_a_live_retry_closes_its_request_to_every_count(store):
    # F2's scenario as complement wrote it (the fault arrives while attempt 2 is live): the request is closed while
    # an attempt is intent (_closed), so the count is refused before and after F2's fix; the witness is not the cause
    c = raw(store)
    first_attempt(c)
    tx(c, attempt(2))
    with pytest.raises(sqlite3.IntegrityError, match="I11"):
        tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    c.close()


def test_the_not_started_close_does_not_move_the_mark_and_a_null_count_takes_the_attempt(store):
    # F3 (complement LOW): _progress counts a link once (DISTINCT effect_id), so the not_started close leaves the mark
    # where the intent put it; an attempt-less count written between that close and the attempt's count then meets
    # the attempt's count at the SAME mark, and the count names the attempt (+1) instead of refusing it
    c = raw(store)
    tx(c, started_run, attempt(1))
    mark = lambda: c.execute(f"SELECT {s._progress(':s')}", {"s": SUBJ}).fetchone()[0]
    before = mark()
    tx(c, not_started(1))
    assert mark() == before
    tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    assert held_row(c) == (1, None)
    tx(c, count(1))
    assert held_row(c) == (2, 1)
    tx(c, attempt(2))
    c.close()


@pytest.mark.parametrize("first, retry", [
    (seal(by="human"), seal(by="on-loop")),       # 10.Q2(b): a human attempt's retry is human
    (seal(by="human"), seal(by=DROP)),
    (seal(by="human"), seal(by=None)),
    (seal(by=DROP), seal()),                      # the predecessor's by unknown
    (seal(), seal(by=1)),
])
def test_a_hold_less_retry_never_drops_a_human_decider(store, first, retry):
    # F4 (complement LOW): 10.Q2(b)'s `by` clause stands on the hold-less branch (R2-2 replaced 10.Q2(b) only for a
    # link with a hold), in R3-1's typed shape
    c = raw(store)
    first_attempt(c, first)
    with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
        tx(c, attempt(2, retry))
    c.close()


@pytest.mark.parametrize("first, retry", [
    (seal(by="human"), seal(by="human")), (seal(by="on-loop"), seal(by="binding")), (seal(), seal(by="human"))])
def test_a_hold_less_retry_keeps_or_raises_its_decider(store, first, retry):
    c = raw(store)
    first_attempt(c, first)
    tx(c, attempt(2, retry))
    c.close()


@pytest.mark.parametrize("n", [1, 3])
def test_count_held_refuses_an_attempt_that_is_not_the_links_latest_not_started_before_writing(store, n):
    # F12 (deepseek LOW): a clear ValueError, not the trigger's IntegrityError
    c = raw(store)
    first_attempt(c)
    tx(c, attempt(2))
    tx(c, not_started(2), count(2))
    before = c.execute("SELECT * FROM held_counts").fetchall()
    with pytest.raises(ValueError, match="latest not_started attempt"):
        tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:x", attempt=n))
    assert c.execute("SELECT * FROM held_counts").fetchall() == before
    c.close()


def test_not_started_receipts_leave_legacy_rows_out(tmp_path):
    # F13 (deepseek LOW): like every other /3 reader (REAP_EFFECTS, INVARIANT); a legacy row is frozen
    c = raw(hand_built(tmp_path, lambda conn: (ended_link("not_started", "refused")(conn),
                                               conn.execute("UPDATE effects SET legacy = 1"))))
    assert s.not_started_receipts(c) == frozenset()
    c.close()


def bound(c):
    return [r for r in s.invariant(c) if r[0] == "held_at_bound_unclosed"]


def test_the_invariant_names_a_held_count_at_the_bound_until_a_run_or_request_close(store):
    # OBS-6 (A1's second invariant row): at the bound the count's own transaction closes the run (A1 order); a count
    # left at the bound with neither close is named
    c = raw(store)
    first_attempt(c)
    for _ in range(s.HELD_LIMIT - 1):
        tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    assert bound(c) == [("held_at_bound_unclosed", SUBJ)]
    tx(c, lambda conn: s.close_subject(conn, subject="run:r", record=rec("cx", "run:r", "refused"),
                                       reason="held_limit:subject:unavailable"))
    assert bound(c) == []
    assert s.invariant(c) == []
    c.close()


def test_a_request_close_clears_the_at_bound_row(store):
    c = raw(store)
    tx(c, started_run)
    for _ in range(s.HELD_LIMIT):
        tx(c, lambda conn: s.count_held(conn, SUBJ, "subject:unavailable"))
    assert bound(c) == [("held_at_bound_unclosed", SUBJ)]
    tx(c, lambda conn: s.close_subject(conn, subject=SUBJ, record=rec("q", SUBJ, "refused"),
                                       reason="held_limit:subject:unavailable"))
    assert s.invariant(c) == []
    c.close()
