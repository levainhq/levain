"""O-p2-1 slice B1a: the ``levain-autonomic/3`` ``effects`` table holds attempts (levain project_memory
``1026-27/O-p2-1_DESIGN.md``, sections 2-5 as amended by 9-14).

The first test is R2-8's: fingerprint parity on the A6 rebuild plan (rename the OLD table away, create the authored
text, copy with attempt 1, drop), run over a POPULATED ``/2`` store that V's own opener made."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.autonomic import db as vdb
from levain.autonomic import schema3 as s
from levain.autonomic.db import AutonomicDB


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _populated_v2(d):
    """A /2 store made by V's opener (db.py), with rows in the table the rebuild copies."""
    AutonomicDB(d).meta("format")
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    c.execute("INSERT INTO runs (run_id, binding_id, generation) VALUES ('r', 'b', 0)")
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, receipt_id, result) "
              "VALUES ('r', 'link-0', 'd0', 'done', 'v-r0', 'ok')")
    c.execute("INSERT INTO effects (run_id, effect_id, digest, pid, state) VALUES ('r', 'link-1', 'd1', 7, 'intent')")
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
              "VALUES ('h', 'r', 'link-2', 'b', 'd2', 1)")
    c.close()


def _master(c) -> list:
    return sorted(c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall(), key=repr)


# ---- R2-8: the build lane's FIRST test ------------------------------------------------------------------------------
def test_a_fresh_store_and_ddl_v3_over_a_populated_v2_store_share_one_sqlite_master(tmp_path):
    fresh = s.AutonomicStore3(tmp_path / "fresh")
    fresh.meta("format")
    with fresh.read() as c:
        want = _master(c)
    d = tmp_path / "v2"
    _populated_v2(d)
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        s.ddl_v3(c)
        s.triggers_v3(c)
        assert _master(c) == want
        assert s.fingerprint(c) == s.expected_fingerprint()
        # A6(a): the stored DDL is the authored text, not SQLite's rename rewrite
        assert c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'effects'").fetchone() == (
            s.EFFECTS_V3,)
        assert c.execute("SELECT run_id, effect_id, digest, pid, state, result, receipt_id, seal, legacy, attempt "
                         "FROM effects ORDER BY effect_id").fetchall() == [
            ("r", "link-0", "d0", None, "done", "ok", "v-r0", None, 0, 1),
            ("r", "link-1", "d1", 7, "intent", None, None, None, 0, 1)]
        assert "effects_v2" not in {n for (n,) in c.execute("SELECT name FROM sqlite_master")}
        c.execute("ROLLBACK")
    finally:
        c.close()


# ---- shared builders ------------------------------------------------------------------------------------------------
GUARD = [{"rationale": "stop on a failed dmarc", "kill_predicate": {"field": "dmarc", "op": "==", "value": "fail"},
          "kill_drill": {"event": {"dmarc": "fail"}}}]


def run_seal(links=1, guard=()):
    return canon({"context_id": "c", "confirm_class": False, "guard": list(guard), "links": links})


def eff_seal(floor=()):
    return canon({"posture": "ON_LOOP", "by": "on-loop", "floor": list(floor), "grant_guard": [], "grant": None,
                  "cover": "none"})


EFF_SEAL = eff_seal()
REOPEN = ("UPDATE holds SET decided = NULL, decided_by = NULL, decided_posture = NULL, signer = NULL, "
          "signature = NULL, challenge = NULL, fence = NULL WHERE hold_id = ? AND decided = 1")   # V _reopen_in


def rec(rid, subject, ts, fired=None, guard=None, **face) -> dict:
    return {"id": rid, "kind": "gate", "subject": subject, "fired": (ts == "fired") if fired is None else fired,
            "action_face": {"terminal_state": ts, "guard": guard, "grant_guard": None, **face}}


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


def snapshot(path) -> tuple:
    c = sqlite3.connect(path)
    try:
        dump = "\n".join(c.iterdump())
        marker = c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    finally:
        c.close()
    return dump, marker


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


def hand_built(tmp_path, rows, name="hand") -> s.AutonomicStore3:
    """A /3 store whose rows ``rows(c)`` are written after :func:`ddl_v3` and before :func:`triggers_v3` (the window
    the I15 upgrade stamps legacy rows in): an attempt >= 2 row the retry rules would refuse without its counted,
    authorised predecessor is written here. The store is then this code's (fingerprint) and its own opener takes it."""
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
        rows(c)
        s.triggers_v3(c)
        c.execute("COMMIT")
        assert s.fingerprint(c) == s.expected_fingerprint()
    finally:
        c.close()
    st.meta("format")
    return st


def started_run(c, run_id="r", *, links=1, guard=(), binding="b"):
    # born unstarted, then started once (runs_sealed / runs_u, D71)
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, ?, 0, ?)",
              (run_id, binding, run_seal(links, guard)))
    c.execute("UPDATE runs SET started_at = 't' WHERE run_id = ?", (run_id,))


def by_hand(c, run_id, effect_id, attempt, state, *, receipt=None, result=None, seal=EFF_SEAL, legacy=0):
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, result, receipt_id, seal, legacy, attempt) "
              "VALUES (?, ?, 'd', ?, ?, ?, ?, ?, ?)", (run_id, effect_id, state, result, receipt, seal, legacy, attempt))


def receipt_by_hand(c, rid, subject, ts, seq, **kw):
    c.execute("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES (?, ?, ?, ?)",
              (rid, subject, canon(rec(rid, subject, ts, **kw)), seq))


def not_started_then_live(c, *, floor2=(), run_guard=()):
    # link-0: attempt 1 ended not_started with its refused receipt, attempt 2 live
    started_run(c, guard=run_guard)
    receipt_by_hand(c, "ns1", "effect:r:link-0", "refused", 1)
    by_hand(c, "r", "link-0", 1, "not_started", receipt="ns1", result="not_started:spawn")
    by_hand(c, "r", "link-0", 2, "intent", seal=eff_seal(floor2))


def intent(c, run_id="r", effect_id="link-0"):
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES (?, ?, 'd', 'intent', ?)",
              (run_id, effect_id, EFF_SEAL))


def close_not_started(c, run_id="r", effect_id="link-0", why="spawn"):
    subj = f"effect:{run_id}:{effect_id}"
    s.close_subject(c, subject=subj, record=rec(f"ns:{subj}", subj, "refused"), state="not_started",
                    result=f"not_started:{why}")


def rows_of(c, run_id="r", effect_id="link-0"):
    return c.execute("SELECT attempt, state, receipt_id FROM effects WHERE run_id = ? AND effect_id = ? "
                     "ORDER BY attempt", (run_id, effect_id)).fetchall()


# ---- section 4 / R2-7 / R2-12: one attempt has exactly one subject ----------------------------------------------------
@pytest.mark.parametrize("args, subject", [
    (("r", "link-0"), "effect:r:link-0"), (("r", "link-0", 1), "effect:r:link-0"),
    (("r", "link-0", 2), "effect:r:link-0#2"), (("r", "link-10", 12), "effect:r:link-10#12"),
    # F5: one rule for '#': a run id may hold it (the subject splits at its first colon), and an effect id may hold
    # it so long as it does not END in '#<digits>' (the only spelling of an attempt)
    (("r#x", "link-0"), "effect:r#x:link-0"), (("r#2", "link-0", 3), "effect:r#2:link-0#3"),
    (("r", "foo#bar"), "effect:r:foo#bar"), (("r", "foo#bar", 2), "effect:r:foo#bar#2"),
    (("r", "link-0#"), "effect:r:link-0#"), (("r", "link-0#-2"), "effect:r:link-0#-2"),
    (("r", "link-0#٢"), "effect:r:link-0#٢"), (("r", "a:b"), "effect:r:a:b"),
    (("r", "link-0", 10 ** 18 - 1), "effect:r:link-0#" + "9" * 18)])
def test_an_effect_subject_names_its_attempt_and_round_trips(args, subject):
    assert s.effect_subject(*args) == subject
    run_id, effect_id = args[:2]
    assert s._effect_key(subject) == (run_id, effect_id, args[2] if len(args) > 2 else 1)


@pytest.mark.parametrize("args", [("r:x", "link-0"), ("", "link-0"), ("r", ""), ("r", "link-0#2"),
                                  ("r", "link-0#02"), ("r", "link-0#2", 3), ("r", "link-0", 0),
                                  ("r", "link-0", -1), ("r", "link-0", True), ("r", "link-0", "2"),
                                  ("r", "link-0", 2.0)])
def test_effect_subject_refuses_what_would_not_split_back(args):
    with pytest.raises(ValueError):
        s.effect_subject(*args)


@pytest.mark.parametrize("subject", ["effect:r:link-0#1", "effect:r:link-0#02", "effect:r:link-0#0",
                                     "effect:r:link-0#2#3", "effect:r:#2", "effect::link-0",
                                     "effect:r", "effect:r:",
                                     # F7: a digit run past _ATTEMPT_DIGITS is malformed, not int()'s ValueError
                                     "effect:r:link-0#1" + "0" * 5000, "effect:r:link-0#" + "1" * 19])
def test_effect_key_refuses_a_non_canonical_subject(subject):
    with pytest.raises(sqlite3.IntegrityError, match="malformed effect subject"):
        s._effect_key(subject)


# ---- section 3 + A5: the not_started close ----------------------------------------------------------------------------
def test_a_not_started_close_ends_the_attempt_with_a_refused_receipt(store):
    c = raw(store)
    tx(c, started_run, intent, close_not_started)
    assert c.execute("SELECT attempt, state, result, receipt_id FROM effects").fetchall() == [
        (1, "not_started", "not_started:spawn", "ns:effect:r:link-0")]
    face = json.loads(c.execute("SELECT record FROM gate_receipts").fetchone()[0])
    assert (face["fired"], face["action_face"]["terminal_state"]) == (False, "refused")
    # R2-1 / R3-4: until it is counted, a live link's not_started attempt is an invariant row
    assert s.invariant(c) == [("not_started_uncounted", "effect:r:link-0")]
    tx(c, lambda conn: s.count_held(conn, "request:r:link-0", "subject:not_started:spawn", attempt=1))
    assert s.invariant(c) == []
    assert c.execute(s.REAP_ORPHAN_RECEIPTS).fetchall() == []
    assert s.TERMINAL_MODES[("effects", "not_started")] == ("refused",)
    c.close()


@pytest.mark.parametrize("ts, fired, face, state, result", [
    ("refused", False, {"refuse_class": "prudential"}, "not_started", "not_started:x"),
    ("refused", False, {"refuse_class": "kill_triggered"}, "not_started", "not_started:x"),
    ("refused", False, {}, "not_started", None),
    ("refused", False, {}, "not_started", "not_started:"),
    ("refused", False, {}, "not_started", "x:not_started:y"),
    ("refused", False, {}, "not_started", "Not_started:x"),
    ("outcome_unknown", False, {}, "not_started", "not_started:x"),
    ("fired", True, {}, "not_started", "not_started:x"),
    ("fired", False, {}, "not_started", "not_started:x"),
    ("refused", True, {}, "not_started", "not_started:x"),
    ("refused", False, {}, "done", "not_started:x"),
    ("refused", False, {}, "unknown", "not_started:x"),
])
def test_a_not_started_close_needs_its_refused_receipt_and_its_marker(store, ts, fired, face, state, result):
    c = raw(store)
    tx(c, started_run, intent)
    subj = "effect:r:link-0"
    with pytest.raises(sqlite3.IntegrityError, match="I4"):
        tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("g", subj, ts, fired=fired, **face),
                                           state=state, result=result))
    assert rows_of(c) == [(1, "intent", None)]
    c.close()


def test_a_not_started_attempt_is_final(store):
    c = raw(store)
    tx(c, started_run, intent, close_not_started)
    for bad in ("UPDATE effects SET state = 'intent', receipt_id = NULL", "UPDATE effects SET result = 'not_started:y'",
                "DELETE FROM effects"):
        with pytest.raises(sqlite3.IntegrityError, match="D54|O-74-4"):
            tx(c, bad)
    c.close()


# ---- section 2 / R2-8: one end and one live attempt per link (the indexes) --------------------------------------------
@pytest.mark.parametrize("rows", [
    (("intent", None), ("intent", None)), (("done", "g1"), ("unknown", "g2")), (("done", "g1"), ("done", "g2"))])
def test_a_link_has_one_live_attempt_and_one_end(tmp_path, rows):
    def build(c):
        started_run(c)
        for n, (state, rid) in enumerate(rows, 1):
            by_hand(c, "r", "link-0", n, state, receipt=rid)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: effects.run_id, effects.effect_id"):
        hand_built(tmp_path, build)


def test_many_not_started_attempts_and_one_end_are_one_link(tmp_path):
    def build(c):
        started_run(c)
        for n in (1, 2):
            receipt_by_hand(c, f"ns{n}", s.effect_subject("r", "link-0", n), "refused", n)
            by_hand(c, "r", "link-0", n, "not_started", receipt=f"ns{n}", result="not_started:x")
        by_hand(c, "r", "link-0", 3, "intent")
    st = hand_built(tmp_path, build)
    c = raw(st)
    subj = "effect:r:link-0#3"
    tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("g3", subj, "fired"), state="done"))
    assert rows_of(c) == [(1, "not_started", "ns1"), (2, "not_started", "ns2"), (3, "done", "g3")]
    assert s.invariant(c) == []
    c.close()


def test_a_second_end_on_one_link_is_refused_at_its_close(tmp_path):
    def build(c):
        started_run(c)
        receipt_by_hand(c, "g1", "effect:r:link-0", "fired", 1)
        by_hand(c, "r", "link-0", 1, "done", receipt="g1")
        by_hand(c, "r", "link-0", 2, "intent")     # no writer makes this; the index still holds the end
    c = raw(hand_built(tmp_path, build))
    subj = "effect:r:link-0#2"
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("g2", subj, "fired"), state="done"))
    c.close()


# ---- the B1a placeholder for effects_attempt (B1b builds the retry rule) -------------------------------------------
def test_a_retry_follows_its_counted_predecessor_and_numbers_are_never_skipped_or_reused(store):
    # the retry rules in full: tests/test_autonomic_retry_authority.py
    c = raw(store)
    retry = lambda n: ("INSERT INTO effects (run_id, effect_id, digest, state, seal, attempt) "  # noqa: E731
                       "VALUES ('r', 'link-0', 'd', 'intent', ?, ?)", (EFF_SEAL, n))
    tx(c, started_run, intent, close_not_started)
    with pytest.raises(sqlite3.IntegrityError, match="R2-1"):
        tx(c, retry(2))   # not counted yet
    tx(c, lambda conn: s.count_held(conn, "request:r:link-0", "subject:not_started:spawn", attempt=1))
    with pytest.raises(sqlite3.IntegrityError, match="O-p2-1: attempt 1 opens a link|R2-1|R3-1/R3-2"):
        tx(c, retry(3))   # no attempt 2 to follow: each retry rule refuses it, the first to fire raises
    with pytest.raises(sqlite3.IntegrityError, match="D69|O-p2-1: attempt 1 opens a link"):
        tx(c, intent)
    tx(c, retry(2))
    assert rows_of(c) == [(1, "not_started", "ns:effect:r:link-0"), (2, "intent", None)]
    c.close()


def test_an_attempt_number_is_immutable(store):
    c = raw(store)
    tx(c, started_run, intent)
    with pytest.raises(sqlite3.IntegrityError, match="D70"):
        tx(c, "UPDATE effects SET attempt = 2")
    c.close()


# ---- section 5: the reapers read a not_started attempt as not live ------------------------------------------------------
def test_the_midchain_reaper_selects_a_run_whose_only_open_link_did_not_start(store):
    c = raw(store)
    tx(c, started_run, intent, close_not_started,
       "INSERT INTO fences (binding_id, generation) VALUES ('b', 1) ON CONFLICT(binding_id) DO UPDATE SET generation = 1")
    assert [r[0] for r in c.execute(s.REAP_MIDCHAIN_RUNS)] == ["r"]
    c.close()


def test_the_unknown_link_reaper_selects_a_run_whose_other_link_did_not_start(store):
    c = raw(store)
    subj = "effect:r:link-0"
    tx(c, lambda conn: started_run(conn, links=2), intent,
       lambda conn: s.close_subject(conn, subject=subj, record=rec("u", subj, "outcome_unknown"), state="unknown"),
       lambda conn: intent(conn, effect_id="link-1"), lambda conn: close_not_started(conn, effect_id="link-1"))
    assert [r[0] for r in c.execute(s.REAP_UNKNOWN_LINK_RUNS)] == ["r"]
    c.close()


def test_a_live_retry_attempt_keeps_its_run_from_the_run_reapers(tmp_path):
    def build(c):
        not_started_then_live(c)
        c.execute("INSERT INTO fences (binding_id, generation) VALUES ('b', 1)")
    c = raw(hand_built(tmp_path, build))
    assert c.execute(s.REAP_MIDCHAIN_RUNS).fetchall() == []
    c.close()


# ---- section 5: a request whose link only did not start is live; its attempts are one unit of progress -------------
def test_a_link_that_only_did_not_start_is_a_live_request_and_counts_held(store):
    c = raw(store)
    subj = "request:r:link-0"
    closed = lambda: c.execute(f"SELECT {s._closed(':s')}", {"s": subj}).fetchone()[0]  # noqa: E731
    tx(c, started_run)
    assert closed() == 0
    tx(c, intent)
    assert closed() == 1
    tx(c, close_not_started)
    assert closed() == 0
    tx(c, lambda conn: s.count_held(conn, subj, "subject:not_started:spawn"))
    assert c.execute("SELECT n FROM held_counts WHERE subject = ?", (subj,)).fetchone() == (1,)
    c.close()


def test_a_links_attempts_are_one_unit_of_progress(tmp_path):
    def build(c):
        started_run(c, links=2)
        for n in (1, 2):
            receipt_by_hand(c, f"ns{n}", s.effect_subject("r", "link-0", n), "refused", n)
            by_hand(c, "r", "link-0", n, "not_started", receipt=f"ns{n}", result="not_started:x")
        by_hand(c, "r", "link-0", 3, "intent")
    c = raw(hand_built(tmp_path, build))
    assert c.execute(f"SELECT {s._progress(':s')}", {"s": "request:r:link-1"}).fetchone() == (1,)
    c.close()


def test_a_hold_reopens_after_its_link_did_not_start_and_not_after_it_ran(store):
    c = raw(store)
    tx(c, started_run,
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) VALUES ('h', 'r', 'link-0', 'b', 'd', 1)",),
       ("UPDATE holds SET decided = 1, decisions = decisions + 1, decided_by = 'human' WHERE hold_id = 'h'",), intent)
    with pytest.raises(sqlite3.IntegrityError, match="I3"):
        tx(c, (REOPEN, ("h",)))
    tx(c, close_not_started, (REOPEN, ("h",)))
    assert c.execute("SELECT decided, decisions FROM holds").fetchall() == [(None, 1)]
    c.close()


# ---- R4-3 + P2: one hold per link -------------------------------------------------------------------------------------
def test_a_second_hold_for_one_link_is_refused(store):
    c = raw(store)
    ins = "INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) VALUES (?, 'r', 'link-0', 'b', 'd', ?)"
    tx(c, started_run, (ins, ("h1", 1)))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: holds.run_id, holds.effect_id"):
        tx(c, (ins, ("h2", 2)))
    c.close()


def test_an_upgrade_over_a_v2_store_with_two_holds_on_one_link_aborts_and_leaves_it(tmp_path):
    d = tmp_path / "v2"
    AutonomicDB(d).meta("format")
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    for hid, seq in (("h1", 1), ("h2", 2)):    # a tampered store: no V writer makes a second hold for a link
        c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
                  "VALUES (?, 'r', 'link-0', 'b', 'd', ?)", (hid, seq))
    c.close()
    before = snapshot(d / vdb.DB_NAME)
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: holds.run_id, holds.effect_id"):
        s.ddl_v3(c)
    c.execute("ROLLBACK")
    c.close()
    assert snapshot(d / vdb.DB_NAME) == before
    assert before[1] == (vdb.STORE_FORMAT,)
    AutonomicDB(d).meta("format")    # V still opens its store


# ---- R2-7: the rebuild runs in the caller's transaction; a crash mid-rebuild leaves the /2 store -----------------------
_CRASH_CHILD = """
import os, sqlite3, sys
from levain.autonomic import schema3 as s

class CrashBeforeDrop:
    def __init__(self, conn):
        self.conn = conn
    def execute(self, sql, *args):
        if sql.startswith("DROP TABLE effects_v2"):
            os._exit(9)     # the process dies with the copy written and the transaction open
        return self.conn.execute(sql, *args)

c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("BEGIN IMMEDIATE")
s.ddl_v3(CrashBeforeDrop(c))
"""


def test_a_crash_mid_rebuild_leaves_the_v2_store_as_it_was(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    d = tmp_path / "v2"
    _populated_v2(d)
    before = snapshot(d / vdb.DB_NAME)
    child = tmp_path / "crash_child.py"
    child.write_text(_CRASH_CHILD)
    root = str(Path(s.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=root)
    done = subprocess.run([sys.executable, str(child), str(d / vdb.DB_NAME)], cwd=root, env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 9, done.stderr
    assert snapshot(d / vdb.DB_NAME) == before
    AutonomicDB(d).meta("format")
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    s.ddl_v3(c)
    assert c.execute("SELECT effect_id, attempt FROM effects ORDER BY effect_id").fetchall() == [
        ("link-0", 1), ("link-1", 1)]
    c.execute("ROLLBACK")
    c.close()


# ---- A8 (section 7 test 10): one spelling of an effect subject, and every rule that spells it reads the attempt ---------
def test_the_effect_subject_is_spelled_once_in_sql():
    from pathlib import Path
    src = Path(s.__file__).read_text()
    assert src.count("'effect:' ||") == 1, "an inline effect subject outside _effect_subj"
    # the link key (run_id || ':' || effect_id) has one home too: the subject and the request helpers build on it
    assert src.count("|| ':' ||") == 1


def test_an_attempt_2_close_carries_its_own_rows_guard_and_ends_only_that_row(tmp_path):
    # receipts_carry_guard, receipts_content (I10), effects_close, effects_close_mode, close_subject_do, _ONE_TERMINAL
    c = raw(hand_built(tmp_path, lambda conn: not_started_then_live(conn, floor2=GUARD, run_guard=GUARD)))
    subj = "effect:r:link-0#2"
    with pytest.raises(sqlite3.IntegrityError, match="D68/I1/D98"):
        tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("g2", subj, "fired"), state="done"))
    # I10: a failed fire (fired=false, terminal_state fired) is legal only for an intent effect: this attempt
    tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("g2", subj, "fired", fired=False, guard=GUARD),
                                       state="done", result="failed"))
    assert rows_of(c) == [(1, "not_started", "ns1"), (2, "done", "g2")]
    assert s.invariant(c) == []
    c.close()


def test_the_invariant_reads_each_attempts_subject(tmp_path):
    def build(c):
        started_run(c, links=2)
        receipt_by_hand(c, "ns1", "effect:r:link-0", "refused", 1)
        by_hand(c, "r", "link-0", 1, "not_started", receipt="ns1", result="not_started:x")
        by_hand(c, "r", "link-0", 2, "done", receipt="ns1")              # carries attempt 1's receipt
        by_hand(c, "r", "link-1", 1, "not_started", receipt="gone", result="not_started:x")
    c = raw(hand_built(tmp_path, build))
    assert sorted(s.invariant(c)) == [("not_started_uncounted", "effect:r:link-1"),
                                      ("terminal_without_receipt", "effect:r:link-0#2"),
                                      ("terminal_without_receipt", "effect:r:link-1")]
    c.close()


def test_an_attempt_2_orphan_is_adopted_by_its_own_row(tmp_path):
    def build(c):
        not_started_then_live(c)
        c.execute("INSERT INTO closing (subject) VALUES ('effect:r:link-0#2')")
        receipt_by_hand(c, "o2", "effect:r:link-0#2", "fired", 2)
        c.execute("DELETE FROM closing")
    c = raw(hand_built(tmp_path, build))
    assert s.invariant(c) == [("receipt_without_terminal", "effect:r:link-0#2")]
    assert [r[:2] for r in c.execute(s.REAP_ORPHAN_RECEIPTS)] == [("o2", "effect:r:link-0#2")]
    seen = []
    out = []
    tx(c, lambda conn: out.append(s.reap_orphans(conn, lease_held=lambda r, e: seen.append((r, e)) or False,
                                                  pending_lease_held=lambda p: False)))
    assert out[0][1] == {"effect:r:link-0#2": s.ADOPTED}
    assert seen == [("r", "link-0")]           # the lease is the link's: attempts are serial (effects_one_live)
    assert rows_of(c) == [(1, "not_started", "ns1"), (2, "done", "o2")]
    assert s.invariant(c) == []
    c.close()


def test_a_refused_receipt_on_an_effect_subject_is_never_adopted(store):
    # R2-9 / 9.r1b F3: a not_started close is one close_subject statement, so a refused effect orphan is never its
    # half; it stays an INVARIANT row for doctor
    c = raw(store)
    subj = "effect:r:link-0"
    tx(c, started_run, intent, ("INSERT INTO closing (subject) VALUES (?)", (subj,)),
       lambda conn: receipt_by_hand(conn, "o", subj, "refused", 1), "DELETE FROM closing")
    out = []
    tx(c, lambda conn: out.append(s.reap_orphans(conn, lease_held=lambda r, e: False,
                                                  pending_lease_held=lambda p: False)))
    assert out[0][1][subj].startswith("unadoptable:")
    assert rows_of(c) == [(1, "intent", None)]
    assert s.invariant(c) == [("receipt_without_terminal", subj)]
    c.close()


def test_the_effect_reaper_keys_and_closes_the_live_attempt(tmp_path):
    st = hand_built(tmp_path, not_started_then_live)
    c = raw(st)
    assert c.execute(s.REAP_EFFECTS).fetchall() == [("r", "link-0", 2, EFF_SEAL)]
    seen = []
    out = s.reap(st, lease_held=lambda r, e: seen.append((r, e)) or False, pending_lease_held=lambda p: False,
                 record_for=lambda subj, sealed, ts, reason: rec(f"reap:{subj}", subj, ts))
    assert out == {"effect:r:link-0#2": s.CLOSED}
    assert set(seen) == {("r", "link-0")}
    assert rows_of(c) == [(1, "not_started", "ns1"), (2, "unknown", "reap:effect:r:link-0#2")]
    c.close()


def test_the_legacy_census_names_each_attempt(tmp_path):
    def build(c):
        c.execute("INSERT INTO runs (run_id, binding_id, generation, legacy) VALUES ('old', 'b', 0, 1)")
        by_hand(c, "old", "link-0", 1, "not_started", receipt="v1", result="not_started:x", seal=None, legacy=1)
        by_hand(c, "old", "link-0", 2, "done", receipt="v2", seal=None, legacy=1)
    c = raw(hand_built(tmp_path, build))
    assert [k for t, k in s.census(c) if t == "effects"] == ["old:link-0", "old:link-0#2"]
    c.close()


# ---- A3: the dry run admits every end an attempt can reach ------------------------------------------------------------
def _effect_builder(broken=None):
    def build(ts):
        if ts == broken:
            raise KeyError(f"face builder for {ts} is broken")
        return rec(f"dry-{ts}", subj_box[0], ts)
    subj_box = [None]
    return build, subj_box


@pytest.mark.parametrize("subject", ["effect:r:link-0#2", "effect:r:link-1"])
def test_the_dry_run_closes_an_attempt_in_every_end_it_can_reach(tmp_path, subject):
    def build(c):
        not_started_then_live(c)
        c.execute("UPDATE runs SET seal = ? WHERE run_id = 'r'", (run_seal(2),))
        by_hand(c, "r", "link-1", 1, "intent")
    c = raw(hand_built(tmp_path, build))
    assert {st for _ts, st in s.CLOSE_STATES["effect"]} == {"done", "unknown", "not_started"}
    good, box = _effect_builder()
    box[0] = subject
    before = c.execute("SELECT * FROM effects ORDER BY effect_id, attempt").fetchall()
    tx(c, lambda conn: s.dry_run_close(conn, subject, good))
    assert c.execute("SELECT * FROM effects ORDER BY effect_id, attempt").fetchall() == before
    bad, box = _effect_builder(broken="refused")
    box[0] = subject
    with pytest.raises(s.CloseUnbuildable, match="close_unbuildable:refused:KeyError"):
        tx(c, lambda conn: s.dry_run_close(conn, subject, bad))
    c.close()


def test_the_dry_run_refuses_a_non_canonical_attempt_subject(store):
    c = raw(store)
    tx(c, started_run, intent)
    good, box = _effect_builder()
    box[0] = "effect:r:link-0#1"
    with pytest.raises(sqlite3.IntegrityError, match="malformed effect subject"):
        tx(c, lambda conn: s.dry_run_close(conn, "effect:r:link-0#1", good))
    c.close()


# ---- fix round 1 (1027+9 op21fix): the B1a L3 r1 findings --------------------------------------------------------------
def _upgraded_v2(d, effects):
    """A /2 store made by V's opener with these (run_id, effect_id) intent rows (V's writers enforce no id shape),
    upgraded by ddl_v3 + triggers_v3."""
    AutonomicDB(d).meta("format")
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    for run_id in sorted({r for r, _e in effects}):
        c.execute("INSERT INTO runs (run_id, binding_id, generation) VALUES (?, 'b', 0)", (run_id,))
    for run_id, effect_id in effects:
        c.execute("INSERT INTO effects (run_id, effect_id, digest, state) VALUES (?, ?, 'd', 'intent')",
                  (run_id, effect_id))
    c.execute("BEGIN IMMEDIATE")
    s.ddl_v3(c)
    s.triggers_v3(c)
    c.execute("COMMIT")
    return c


def test_sql_and_python_spell_and_split_every_legacy_hash_subject_alike(tmp_path):
    # F5 (codex MED + deepseek MED): a '#' in a /2 run id, or inside a /2 effect id, is one subject in SQL and in
    # Python, and the reaper's subject for each row splits back to that row
    c = _upgraded_v2(tmp_path / "v2", [("r#x", "link-0"), ("r", "foo#bar"), ("r", "a#b:c")])
    rows = c.execute(f"SELECT run_id, effect_id, attempt, {s._effect_subj('e')} FROM effects e "
                     "ORDER BY run_id, effect_id").fetchall()
    assert len(rows) == 3
    for run_id, effect_id, attempt, sql_subject in rows:
        assert s._subject_of("effect", (run_id, effect_id, attempt)) == sql_subject
        assert s._effect_key(sql_subject) == (run_id, effect_id, attempt)
    assert sorted(r[:3] for r in c.execute(s.REAP_EFFECTS)) == [r[:3] for r in rows]   # the reaper selects each
    c.close()


def test_a_legacy_effect_id_ending_like_an_attempt_fails_closed(tmp_path):
    # F5 KNOWN EDGE: a /2 effect id that itself ENDS in '#<digits>' is the one shape SQL's spelling makes ambiguous
    # (its subject reads as attempt 7 of 'link-1'). No V writer makes one (effect ids are 'link-<i>'). Python refuses
    # to name it (the reaper logs and skips the row) and an orphan on its subject adopts no row
    c = _upgraded_v2(tmp_path / "v2", [("r", "link-1#7")])
    (sql_subject,) = c.execute(f"SELECT {s._effect_subj('e')} FROM effects e").fetchone()
    with pytest.raises(ValueError, match="ends in"):
        s._subject_of("effect", ("r", "link-1#7", 1))
    assert s._effect_key(sql_subject) == ("r", "link-1", 7)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="no intent effect row adopts"):
        s.adopt_orphan(c, "x", sql_subject, "fired", lease_held=lambda *_: False, pending_lease_held=lambda _: False)
    c.execute("ROLLBACK")
    c.close()


def test_a_born_run_id_may_hold_a_hash_and_its_attempts_split_back(store):
    # F5: runs_sealed refuses only ':' (the one character the split needs absent from a run id)
    c = raw(store)
    tx(c, lambda conn: started_run(conn, "r#2"), lambda conn: intent(conn, "r#2"),
       lambda conn: close_not_started(conn, "r#2"))
    assert rows_of(c, "r#2") == [(1, "not_started", "ns:effect:r#2:link-0")]
    assert s._effect_key(s.effect_subject("r#2", "link-0", 2)) == ("r#2", "link-0", 2)
    with pytest.raises(sqlite3.IntegrityError, match="D56/D68/D71/I2"):
        tx(c, lambda conn: started_run(conn, "r:2"))
    c.close()


def test_one_form_reads_that_a_receipt_says_nothing_fired():
    # F6 (deepseek MED): effects_close_mode reads `fired` as I10 and REAP_UNKNOWN_LINK_RUNS do; receipts_content types it a
    # JSON boolean at birth, so the forms could not disagree on a stored record (next test)
    every = "\n".join(s.ALL_TRIGGERS) + s.INVARIANT + s.REAP_ORPHAN_RECEIPTS
    assert "json_type(g.record, '$.fired')" not in every
    assert "json_extract(g.record, '$.fired') = 0" in "\n".join(s.ALL_TRIGGERS)


@pytest.mark.parametrize("fired", [0, 1, "false", None])
def test_a_receipt_whose_fired_is_not_a_json_boolean_is_refused_at_birth(store, fired):
    c = raw(store)
    tx(c, started_run, intent)
    subj = "effect:r:link-0"
    r = rec("g", subj, "refused")
    r["fired"] = fired
    with pytest.raises(sqlite3.IntegrityError, match="I4"):
        tx(c, lambda conn: s.close_subject(conn, subject=subj, record=r, state="not_started",
                                           result="not_started:x"))
    assert rows_of(c) == [(1, "intent", None)]
    c.close()


def test_the_dry_run_refuses_an_overlong_attempt_as_malformed_not_valueerror(store):
    # F7 (deepseek): the IntegrityError contract holds past int()'s digit limit
    c = raw(store)
    tx(c, started_run, intent)
    good, box = _effect_builder()
    subj = "effect:r:link-0#1" + "0" * 5000
    box[0] = subj
    with pytest.raises(sqlite3.IntegrityError, match="malformed effect subject"):
        tx(c, lambda conn: s.dry_run_close(conn, subj, good))
    c.close()


def test_the_dry_run_closes_every_end_with_the_real_face_builder(store):
    # F8 (complement LOW-MED), measured: V's build_gate_face leaves refuse_class null unless the caller passes one
    # (only the prediction-error kill does), so the not_started close is no stricter than the real builder. The record
    # envelope ({id, kind, subject, fired}) is B2's /3 seal builder's; it is written here as the other dry-run tests do
    from levain.autonomic.authority import AuthorityScope
    from levain.autonomic.receipt import build_gate_face
    inputs = {"context_id": "ctx-1", "query_text": "q", "query_date": "2026-10-27", "producers": ["afferent"],
              "gate": {"verdict": "denied", "posture": "ON_LOOP", "by": "on-loop", "reason": "x"},
              "authority": AuthorityScope(grantor="binding", grant="b-1", binding_id="b-1"),
              "actor_first_estimate": None}
    subj = "effect:r:link-0"

    def build(ts, **kw):
        face = build_gate_face(**inputs, terminal_state=ts, **kw)
        face["guard"], face["grant_guard"] = None, None
        return {"id": f"r-{ts}", "kind": "gate", "subject": subj, "fired": ts == "fired", "action_face": face}

    c = raw(store)
    tx(c, started_run, intent)
    tx(c, lambda conn: s.dry_run_close(conn, subj, build))
    with pytest.raises(s.CloseUnbuildable, match="close_unbuildable:refused:IntegrityError:I4"):
        tx(c, lambda conn: s.dry_run_close(conn, subj, lambda ts: build(ts, refuse_class="prudential")))
    assert rows_of(c) == [(1, "intent", None)]
    c.close()


def test_the_upgrade_gives_held_counts_its_attempt_column(tmp_path):
    # F9 (deepseek HIGH, a claim) REFUTED by this run: V's /2 opener makes no held_counts table, so ddl_v3's CREATE
    # TABLE makes it whole, attempt included; count_held names an attempt on the upgraded store
    c = _upgraded_v2(tmp_path / "v2", [])
    assert s.v2_columns("held_counts") == frozenset()
    assert [r[1] for r in c.execute("PRAGMA table_info(held_counts)")] == list(s.KNOWN_COLUMNS["held_counts"])
    c.execute("BEGIN IMMEDIATE")
    s.write_tunables(c)
    started_run(c)
    intent(c)
    close_not_started(c)
    assert s.count_held(c, "request:r:link-0", "subject:not_started:spawn", attempt=1) == 1
    c.execute("COMMIT")
    assert c.execute("SELECT n, attempt FROM held_counts").fetchall() == [(1, 1)]
    c.close()
