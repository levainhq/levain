"""The ``levain-autonomic/3`` store built fresh (autogrant A1c1; ``levain/autonomic/schema3.py``).

Each test ports a probe that closed a design ruling by a run (project_memory ``1026-20/rev74/probes/p74_schema.py``,
``1026-20/rev74b/probes/p74b_schema.py``, ``p74b_r13_8.py``) or an arch r14 build obligation (R14-2, R14-3, R14-4)."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.autonomic import Posture
from levain.autonomic import db as vdb
from levain.autonomic import schema3 as s
from levain.autonomic.authority import AuthorityScope
from levain.autonomic.db import AutonomicDB, StoreFormatError
from levain.autonomic.receipt import build_gate_face
from tests.autonomic_test_only_confinement import test_only_confined_executor  # noqa: F401  (the root-run closure has no conftest)
from tests.autonomic_test_only_coverage import TestOnlyCoverage
from tests.test_autonomic_gate_coverage import LATER, CWorld, _covering


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


RUN_SEAL = canon({"context_id": "c", "confirm_class": False, "guard": [], "links": 1})
EFF_SEAL = canon({"posture": "ON_LOOP", "by": "on-loop", "floor": [], "grant_guard": [], "grant": None, "cover": "none"})
GUARD = [{"rationale": "stop on a failed dmarc", "kill_predicate": {"field": "dmarc", "op": "==", "value": "fail"},
          "kill_drill": {"event": {"dmarc": "fail"}}}]
REOPEN = ("UPDATE holds SET decided = NULL, decided_by = NULL, decided_posture = NULL, signer = NULL, "
          "signature = NULL, challenge = NULL, fence = NULL WHERE hold_id = ? AND decided = 1")   # V _reopen_in


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


def snapshot(path) -> tuple:
    c = sqlite3.connect(path)
    try:
        dump = hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
        marker = c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    finally:
        c.close()
    return dump, marker


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


# ---- the format, the opener ----------------------------------------------------------------------------------------
def test_a_fresh_store_is_v3_with_its_tunables_and_opens_again(tmp_path, store):
    assert store.meta("format") == s.STORE_FORMAT_V3
    assert {k: store.meta(k) for k in s.TUNABLES} == {k: str(v) for k, v in s.TUNABLES.items()}
    assert store.meta("store_id").startswith("store-")
    again = s.AutonomicStore3(store.directory)
    assert again.meta("format") == s.STORE_FORMAT_V3
    with again.read() as c:
        assert s.invariant(c) == []
        assert s.fingerprint(c) == s.expected_fingerprint()


@pytest.mark.parametrize("make", ["v2", "v1"])
def test_an_older_store_is_refused_by_its_format_and_left_as_it_was(tmp_path, make):
    d = tmp_path / "old"
    AutonomicDB(d).meta("format")                      # V makes the /2 store
    if make == "v1":
        c = sqlite3.connect(d / vdb.DB_NAME)
        c.execute("UPDATE meta SET value = ? WHERE key = 'format'", (vdb.STORE_FORMAT_V1,))
        c.commit()
        c.close()
    before = snapshot(d / vdb.DB_NAME)
    with pytest.raises(s.StoreNeedsUpgrade, match="store-needs-upgrade"):
        s.AutonomicStore3(d).meta("format")
    assert snapshot(d / vdb.DB_NAME) == before
    assert sorted(p.name for p in d.iterdir() if ".pre-" in p.name) == []   # no backup: nothing was upgraded


def test_a_newer_store_and_a_foreign_database_are_refused(tmp_path, store):
    c = raw(store)
    tx(c, ("UPDATE meta SET value = 'levain-autonomic/4' WHERE key = 'format'",))
    c.close()
    with pytest.raises(s.StoreNewerThanLevain, match="store-newer-than-levain"):
        s.AutonomicStore3(store.directory).meta("format")
    other = tmp_path / "foreign"
    other.mkdir()
    f = sqlite3.connect(other / vdb.DB_NAME)
    f.execute("CREATE TABLE t (x)")
    f.commit()
    f.close()
    with pytest.raises(StoreFormatError, match="not taken over"):
        s.AutonomicStore3(other).meta("format")


def test_an_older_levain_refuses_a_v3_store_by_its_format(store):
    before = snapshot(store.path)
    with pytest.raises(StoreFormatError, match="levain-autonomic/3"):
        AutonomicDB(store.directory).meta("format")
    assert snapshot(store.path) == before


def test_known_columns_pin_every_whole_row_trigger_table(store):
    with store.read() as c:
        for table, cols in s.KNOWN_COLUMNS.items():
            assert tuple(r[1] for r in c.execute(f"PRAGMA table_info({table})")) == cols, table


# ---- R14-4 / R13-9: one fingerprint, the whole schema, the SQLite floor ----------------------------------------------
def _v2_store_then_steps(d) -> str:
    """V's own /2 store, then the /3 DDL step (what an upgrade runs before its stamp and backfill)."""
    c = sqlite3.connect(d / vdb.DB_NAME, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    s.ddl_v3(c)
    s.triggers_v3(c)
    out = s.fingerprint(c)
    c.execute("ROLLBACK")
    c.close()
    return out


def test_a_fresh_store_and_an_upgraded_one_have_one_fingerprint(tmp_path, store):
    fresh_v2 = tmp_path / "v2"
    AutonomicDB(fresh_v2).meta("format")
    # a /1 store whose holds predate the four signer columns: V's opener adds them by ALTER TABLE
    hist = tmp_path / "hist"
    hist.mkdir()
    old_holds = [st for st in vdb._SCHEMA if "TABLE IF NOT EXISTS holds" in st][0].replace(
        ", signer TEXT, signature TEXT, challenge TEXT, fence TEXT", "")
    c = sqlite3.connect(hist / vdb.DB_NAME)
    c.execute(old_holds)
    c.execute(vdb._SCHEMA[0])
    c.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (vdb.STORE_FORMAT_V1,))
    c.commit()
    c.close()
    AutonomicDB(hist).meta("format")
    with store.read() as conn:
        mine = s.fingerprint(conn)
    assert mine == s.expected_fingerprint() == _v2_store_then_steps(fresh_v2) == _v2_store_then_steps(hist)


def test_a_tunable_is_data_not_ddl(tmp_path, store, monkeypatch):
    for stmt in s.ALL_TRIGGERS:
        assert "HELD_LIMIT" not in stmt and f">= {s.HELD_LIMIT} " not in stmt
    before = s.expected_fingerprint()
    monkeypatch.setitem(s.TUNABLES, "held_limit", 2)
    monkeypatch.setattr(s, "_EXPECTED", {})
    assert s.expected_fingerprint() == before
    assert s.AutonomicStore3(store.directory).meta("held_limit") == "2"   # the opener writes the code's value
    c = raw(store)
    subj = "request:p:link-0"
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
       lambda conn: s.count_held(conn, subj, "subject:f"), lambda conn: s.count_held(conn, subj, "subject:f"))
    tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("r-q", subj, "refused"),
                                       reason="held_limit:subject:f"))
    c.close()


@pytest.mark.parametrize("tamper", [
    ["DROP INDEX holds_pending_id"],
    ["ALTER TABLE effects ADD COLUMN extra TEXT"],
    ["PRAGMA writable_schema = ON",
     "UPDATE sqlite_master SET sql = replace(sql, 'CHECK (n >= 1)', 'CHECK (n >= 0)') WHERE name = 'held_counts'",
     "PRAGMA writable_schema = OFF"],
    ["DROP TRIGGER effects_d"],
])
def test_a_tampered_v3_store_is_refused_never_rewritten(store, tamper):
    c = sqlite3.connect(store.path, isolation_level=None)
    for st in tamper:
        c.execute(st)
    c.close()
    before = snapshot(store.path)
    with pytest.raises(s.StoreSchemaMismatch, match="store-schema-mismatch"):
        s.AutonomicStore3(store.directory).meta("format")
    assert snapshot(store.path) == before


def test_the_sqlite_floor():
    c = sqlite3.connect(":memory:")
    assert tuple(int(x) for x in s.check_sqlite(c).split(".")[:3]) >= s.MIN_SQLITE
    with pytest.raises(s.StoreSqliteTooOld, match="store-sqlite-too-old"):
        s.check_sqlite(c, (99, 0, 0))


# ---- the close path -------------------------------------------------------------------------------------------------
def test_close_subject_writes_the_receipt_and_its_terminal_row_in_one_statement(store):
    with store.write() as c:
        c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r1', 'b', 0, ?)", (RUN_SEAL,))
        assert s.close_subject(c, subject="run:r1", record=rec("g1", "run:r1", "refused"), reason="refused:x") == "g1"
    with store.read() as c:
        assert c.execute("SELECT run_id, reason, receipt_id FROM cancels").fetchall() == [("r1", "refused:x", "g1")]
        assert s.invariant(c) == []
    c = raw(store)
    with pytest.raises(sqlite3.IntegrityError, match="close_subject"):
        tx(c, ("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES ('g2', 'run:r1', ?, 9)",
               (canon(rec("g2", "run:r1", "refused")),)))
    c.close()
    with pytest.raises(RuntimeError, match="write transaction"):
        s.close_subject(sqlite3.connect(":memory:"), subject="run:x", record=rec("g", "run:x", "refused"))


# ---- R13-6: held-count progress never goes down -----------------------------------------------------------------------
def test_a_reopen_does_not_restart_the_held_count(store):
    c = raw(store)
    subj = "request:p:link-0"
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq) "
        "VALUES ('h-p', 'p', 'link-0', 'b', 'd', 'pp', 1)",),
       ("UPDATE holds SET decided = 1, decided_by = 'on-loop' WHERE hold_id = 'h-p'",))
    fault = lambda conn: s.count_held(conn, subj, "subject:fault")  # noqa: E731
    tx(c, fault, fault)
    mark = lambda: c.execute(f"SELECT {s._progress(':s')}", {"s": subj}).fetchone()[0]  # noqa: E731
    before = mark()
    # rev 7.3's mark counted holds WHERE decided IS NOT NULL, which this reopen decrements (the RED of p74_schema.py)
    decided_before = c.execute("SELECT COUNT(*) FROM holds WHERE decided IS NOT NULL").fetchone()[0]
    tx(c, (REOPEN, ("h-p",)))
    assert c.execute("SELECT COUNT(*) FROM holds WHERE decided IS NOT NULL").fetchone()[0] == decided_before - 1
    assert mark() == before
    counts = []
    for _ in range(s.HELD_LIMIT - 1):
        c.execute("BEGIN IMMEDIATE")
        counts.append(s.count_held(c, subj, "subject:fault"))
        c.execute("COMMIT")
    assert counts[-1] >= s.HELD_LIMIT
    tx(c, lambda conn: s.close_subject(conn, subject=subj, record=rec("r-q", subj, "refused"),
                                       reason="held_limit:subject:fault"))
    assert c.execute("SELECT hold_id, by, n FROM hold_decisions").fetchall() == [("h-p", "on-loop", 1)]
    for bad in ("INSERT INTO hold_decisions (hold_id, run_id, pending_id, by, n) VALUES ('h-p', 'p', 'pp', 'x', 9)",
                "UPDATE hold_decisions SET by = 'human'", "DELETE FROM hold_decisions"):
        with pytest.raises(sqlite3.IntegrityError, match="R13-6"):
            tx(c, bad)
    assert s.invariant(c) == []
    c.close()


def test_a_re_approval_is_progress_and_restarts_the_count(store):
    c = raw(store)
    subj = "request:q:link-0"
    fault = lambda conn: s.count_held(conn, subj, "subject:fault")  # noqa: E731
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('q', 'b', 0, ?)", (RUN_SEAL,)),
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq) "
        "VALUES ('h-q', 'q', 'link-0', 'b', 'd', 'qq', 2)",),
       ("UPDATE holds SET decided = 1, decided_by = 'on-loop' WHERE hold_id = 'h-q'",), fault, fault,
       (REOPEN, ("h-q",)), ("UPDATE holds SET decided = 1, decided_by = 'human' WHERE hold_id = 'h-q'",), fault)
    assert c.execute("SELECT n FROM held_counts WHERE subject = ?", (subj,)).fetchone() == (1,)
    assert c.execute("SELECT by, n FROM hold_decisions WHERE hold_id = 'h-q'").fetchall() == [("on-loop", 1),
                                                                                             ("human", 2)]
    c.close()


def test_a_held_limit_close_needs_its_count_at_the_bound(store):
    c = raw(store)
    subj = "request:p:link-0"
    with pytest.raises(sqlite3.IntegrityError, match="held_limit close needs its count"):
        tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
           lambda conn: s.close_subject(conn, subject=subj, record=rec("r", subj, "refused"),
                                        reason="held_limit:subject:f"))
    c.close()


# ---- R13-1: the forged close mark is detected; the reaper adopts what it can --------------------------------------
def _forge(subject, rid, ts, seq, *, leave_mark=False):
    stmts = [("INSERT INTO closing (subject) VALUES (?)", (subject,)),
             ("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES (?, ?, ?, ?)",
              (rid, subject, canon(rec(rid, subject, ts)), seq))]
    return stmts if leave_mark else stmts + [("DELETE FROM closing WHERE subject = ?", (subject,))]


def test_forged_orphans_are_detected_and_the_reaper_adopts_each_adoptable_one(store):
    c = raw(store)
    tx(c, *[("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, 'b', 0, ?)", (r, RUN_SEAL))
            for r in ("victim", "v2", "v3", "v4", "v5")],
       ("UPDATE runs SET started_at = 't' WHERE run_id = 'v2'",),
       ("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES ('v2', 'link-0', 'd', 'intent', ?)",
        (EFF_SEAL,)))
    tx(c, *_forge("run:victim", "orphan", "refused", 1))
    tx(c, *_forge("effect:v2:link-0", "orphan-e", "fired", 2))
    tx(c, *_forge("run:v3", "orphan-f", "fired", 3))
    tx(c, *_forge("run:v4", "orphan-l", "refused", 4, leave_mark=True))
    tx(c, ("INSERT INTO closing (subject) VALUES ('run:v5')",))
    tx(c, ("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES ('raw5', 'run:v5', ?, 5)",
           (canon(rec("raw5", "run:v5", "refused")),)))
    assert sorted(s.invariant(c)) == sorted(
        [("receipt_without_terminal", x) for x in ("run:victim", "effect:v2:link-0", "run:v3", "run:v4", "run:v5")]
        + [("closing_left", "run:v4"), ("closing_left", "run:v5")])
    out = []
    tx(c, lambda conn: out.append(s.reap_orphans(conn)))
    assert out == [(["run:v4", "run:v5"], ["run:victim", "effect:v2:link-0", "run:v4", "run:v5"], ["run:v3"])]
    assert s.invariant(c) == [("receipt_without_terminal", "run:v3")]     # a fired run receipt: detected only
    with pytest.raises(sqlite3.IntegrityError, match="D69"):
        tx(c, lambda conn: s.close_subject(conn, subject="run:victim", record=rec("legit", "run:victim", "refused"),
                                           reason="refused:x"))
    with pytest.raises(sqlite3.IntegrityError, match="close_subject"):
        tx(c, ("INSERT INTO gate_receipts (receipt_id, subject, record, seq) VALUES ('raw3', 'run:zz', ?, 21)",
               (canon(rec("raw3", "run:zz", "refused")),)))
    c.close()


# ---- O-74-4 + O-74b-4: nothing the store depends on is removed --------------------------------------------------------
def test_no_effect_row_is_removed_and_the_delete_sweep_is_pinned(store):
    c = raw(store)
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('v', 'b', 0, ?)", (RUN_SEAL,)),
       ("UPDATE runs SET started_at = 't' WHERE run_id = 'v'",),
       ("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES ('v', 'link-0', 'd', 'intent', ?)",
        (EFF_SEAL,)),
       ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq) "
        "VALUES ('h-p', 'p', 'link-0', 'b', 'd', 'pp', 1)",),
       ("UPDATE holds SET decided = 1, decided_by = 'on-loop' WHERE hold_id = 'h-p'",),
       ("INSERT INTO bindings (binding_id, record, seq) VALUES ('b', '{}', 1)",),
       ("INSERT INTO fences (binding_id, generation) VALUES ('b', 1)",),
       ("INSERT INTO graduation_steps (at, old_id, new_id, family, from_posture, to_posture, clean_count, "
        "fire_count, policy_id, incarnation, authority_head) VALUES ('t', 'a', 'b', 'f', 'X', 'Y', 1, 1, 'p', 'i', 'h')",),
       lambda conn: s.count_held(conn, "request:p:link-0", "subject:f"),
       ("INSERT INTO closing (subject) VALUES ('run:zz')",),
       ("INSERT INTO env_health (k, at) VALUES (1, 't')",),
       ("INSERT INTO manual_pendings (pending_id, seal, state, seq) VALUES ('m', '{}', 'open', 1)",))
    tx(c, lambda conn: s.close_subject(conn, subject="run:p", record=rec("g", "run:p", "refused"), reason="r"),
       lambda conn: s.close_subject(conn, subject="request:q:link-0", record=rec("gq", "request:q:link-0", "refused"),
                                    reason="refused:x"))
    s.record_fault(c, "request:x:link-0", "OperationalError: x")
    with pytest.raises(sqlite3.IntegrityError, match="O-74-4"):
        tx(c, "DELETE FROM effects WHERE run_id = 'v'")
    deletable = []
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0, t
        try:
            tx(c, f"DELETE FROM {t}")
            deletable.append(t)
        except sqlite3.IntegrityError:
            pass
    # bindings: V's BindingStore rewrites the registry by DELETE + INSERT; closing: close_subject_do deletes its own
    # mark (O-a1c1-3: every other table refuses a delete)
    assert deletable == ["bindings", "closing"]
    for t, guard in (("fences", "a fence"), ("graduation_steps", "a graduation step"), ("env_health", "the health row")):
        with pytest.raises(sqlite3.IntegrityError, match=f"O-a1c1-3: {guard} is never removed"):
            tx(c, f"DELETE FROM {t}")
    # a REPLACE over a fence reaches its delete (recursive_triggers is ON); the writers' upsert does not
    with pytest.raises(sqlite3.IntegrityError, match="O-a1c1-3"):
        tx(c, "INSERT OR REPLACE INTO fences (binding_id, generation) VALUES ('b', 2)")
    tx(c, "INSERT INTO fences (binding_id, generation) VALUES ('b', 2) ON CONFLICT(binding_id) "
          "DO UPDATE SET generation = MAX(generation, excluded.generation)")
    assert s.health_probe(c, "t2")
    assert {r[0] for r in c.execute("SELECT key FROM meta")} == set(s.PROTECTED_META)
    c.close()


def test_the_meta_keys_the_store_depends_on_are_protected(store):
    c = raw(store)
    for bad in ("DELETE FROM meta WHERE key = 'format'", "DELETE FROM meta WHERE key = 'store_id'",
                "DELETE FROM meta WHERE key = 'held_limit'",
                "UPDATE meta SET value = 'levain-autonomic/2' WHERE key = 'format'",
                "UPDATE meta SET value = 'levain-autonomic/03' WHERE key = 'format'",
                "UPDATE meta SET value = 'x' WHERE key = 'store_id'",
                "UPDATE meta SET value = '0' WHERE key = 'held_limit'",
                "UPDATE meta SET value = '5x' WHERE key = 'env_fault_n'",
                "UPDATE meta SET key = 'format2' WHERE key = 'format'",
                "INSERT INTO meta (key, value) VALUES ('other', 'x')",
                ):
        if bad.startswith("INSERT"):
            tx(c, bad)
            with pytest.raises(sqlite3.IntegrityError, match="O-74b-4"):
                tx(c, "UPDATE meta SET key = 'format' WHERE key = 'other'")
            continue
        with pytest.raises(sqlite3.IntegrityError, match="O-74b-4|R14-4"):
            tx(c, bad)
    tx(c, ("INSERT OR REPLACE INTO meta (key, value) VALUES ('format', 'levain-autonomic/9')",))
    assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() == (s.STORE_FORMAT_V3,)
    tx(c, "UPDATE meta SET value = '7' WHERE key = 'held_limit'")
    tx(c, "UPDATE meta SET value = 'levain-autonomic/4' WHERE key = 'format'")      # forward only
    c.close()


# ---- R13-7 + R14-2: the per-subject environment-fault bound -----------------------------------------------------------
EPOCH = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=20)


def test_a_repeating_subject_fault_surfaces_once_and_never_closes_the_subject(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.directory.mkdir()
    # S's run row has a malformed seal, written before the triggers (a store a pre-trigger writer left): every effect
    # insert of S raises OperationalError from the effects triggers' json_extract
    p = sqlite3.connect(st.path, isolation_level=None)
    p.execute("BEGIN IMMEDIATE")
    s._v2_schema(p)
    p.execute("INSERT INTO meta (key, value) VALUES ('store_id', 'store-x'), ('format', ?)", (s.STORE_FORMAT_V3,))
    s.write_tunables(p)
    s.ddl_v3(p)
    p.execute("INSERT INTO runs (run_id, binding_id, generation, seal, started_at) VALUES ('s', 'b', 0, ?, 't')",
              ('{"links": 1, "context_id": "c"',))
    s.triggers_v3(p)
    p.execute("COMMIT")
    p.close()
    assert st.meta("format") == s.STORE_FORMAT_V3
    c = raw(st)
    pause, alerts, kinds = 0, [], []

    def deliver(subject, work, now):
        nonlocal pause
        try:
            tx(c, work)
        except sqlite3.DatabaseError as e:
            kind = s.classify(c, e, s.utc_text(now))
            kinds.append((subject, kind))
            if kind == "subject" and s.record_fault(c, subject, f"{type(e).__name__}: {e}", now):
                alerts.append((now, subject))
            elif kind == "store":
                pause += 1
            return
        s.record_success(c, subject, now)
        kinds.append((subject, "ok"))

    def t_work(i):
        def w(conn):
            run, subj = f"t{i}", f"effect:t{i}:link-0"
            conn.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, 'b', 0, ?)", (run, RUN_SEAL))
            conn.execute("UPDATE runs SET started_at = 't' WHERE run_id = ?", (run,))
            conn.execute("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES (?, 'link-0', 'd', "
                         "'intent', ?)", (run, EFF_SEAL))
            s.close_subject(conn, subject=subj, record=rec(f"r-t{i}", subj, "fired"), state="done")
        return w

    s_work = ("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES ('s', 'link-0', 'd', 'intent', ?)",
              (EFF_SEAL,))
    for i in range(6):
        deliver("request:s:link-0", s_work, EPOCH + i * STEP)
        deliver(f"request:t{i}:link-0", t_work(i), EPOCH + i * STEP)
    assert [k for k in kinds if k[0] == "request:s:link-0"] == [("request:s:link-0", "subject")] * 6
    assert alerts == [(EPOCH + 3 * STEP, "request:s:link-0")]        # n = 4 at exactly one hour
    assert pause == 0
    assert [r[:2] for r in c.execute(s.ENV_FAULT_LISTING)] == [("request:s:link-0", 6)]
    assert c.execute("SELECT COUNT(*) FROM effects WHERE run_id GLOB 't*' AND state = 'done'").fetchone() == (6,)
    assert c.execute("SELECT COUNT(*) FROM cancels WHERE run_id = 's'").fetchone() == (0,)
    assert c.execute("SELECT COUNT(*) FROM gate_receipts WHERE subject GLOB '*:s:*' OR subject = 'run:s'").fetchone() == (0,)
    assert s.invariant(c) == []
    # a store-wide fault: another connection holds the write lock; the pause counts it, env_faults does not
    before = c.execute("SELECT subject, n FROM env_faults ORDER BY subject").fetchall()
    other = raw(st)
    other.execute("BEGIN IMMEDIATE")
    deliver("request:t99:link-0", t_work(99), EPOCH + 8 * STEP)
    other.execute("ROLLBACK")
    other.close()
    assert pause == 1 and c.execute("SELECT subject, n FROM env_faults ORDER BY subject").fetchall() == before
    for bad in ("UPDATE env_faults SET alerted_at = 'x' WHERE subject = 'request:s:link-0'", "DELETE FROM env_faults",
                "UPDATE env_faults SET n = n + 2", "INSERT INTO env_faults (subject, first_at, last_at, n, "
                "last_reason) VALUES ('request:x:link-0', 'a', 'a', 5, 'r')"):
        with pytest.raises(sqlite3.IntegrityError, match="R13-7"):
            tx(c, bad)
    c.close()


def test_a_clock_step_back_is_counted_never_raised(store):
    c = raw(store)
    subj = "request:u:link-0"
    assert s.record_fault(c, subj, "OperationalError: x", EPOCH + 3 * STEP) is False
    # the RED: rev74b's writer set last_at to the new (earlier) instant, which env_faults_move refuses
    with pytest.raises(sqlite3.IntegrityError, match="R13-7"):
        tx(c, ("UPDATE env_faults SET n = n + 1, first_at = IFNULL(first_at, ?), last_at = ? WHERE subject = ?",
               (s.utc_text(EPOCH), s.utc_text(EPOCH), subj)))
    assert s.record_fault(c, subj, "OperationalError: y", EPOCH) is False      # the clock stepped back an hour
    assert c.execute("SELECT n, first_at, last_at, last_reason FROM env_faults").fetchall() == [
        (2, s.utc_text(EPOCH + 3 * STEP), s.utc_text(EPOCH + 3 * STEP), "OperationalError: y")]
    s.record_success(c, subj, EPOCH)
    assert c.execute("SELECT n, first_at, alerted_at, last_at FROM env_faults").fetchall() == [
        (0, None, None, s.utc_text(EPOCH + 3 * STEP))]
    # a naive instant is UTC; a local one is converted
    assert s.utc_text(datetime(2026, 1, 1, 12)) == "2026-01-01T12:00:00+00:00"
    assert s.utc_text(datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-5)))) == "2026-01-01T17:00:00+00:00"
    # a fault that cannot be written (the store is locked) is logged, never raised
    other = raw(store)
    other.execute("BEGIN IMMEDIATE")
    try:
        assert s.record_fault(c, subj, "OperationalError: z", EPOCH + 4 * STEP) is False
        s.record_success(c, subj, EPOCH + 4 * STEP)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert not c.in_transaction
    c.close()


# ---- R13-8 + R14-3: the admission runs the real close of every terminal state, rolled back -----------------------------
def _face_inputs(**over):
    base = {"context_id": "ctx-1", "query_text": "q", "query_date": "2026-10-26", "producers": ["afferent"],
            "gate": {"verdict": "denied", "posture": "ON_LOOP", "by": "on-loop", "reason": "x"},
            "authority": AuthorityScope(grantor="binding", grant="b-1", binding_id="b-1"),
            "actor_first_estimate": None}
    base.update(over)
    return base


def _record_for(subject, inputs, guard=None, with_guard=True):
    def build(ts):
        face = build_gate_face(**inputs, terminal_state=ts)
        if with_guard:
            face["guard"] = guard or None
            face["grant_guard"] = None
        return {"id": f"r-{subject}-{ts}", "kind": "gate", "subject": subject, "fired": ts == "fired",
                "action_face": face}
    return build


def _admit(store, run_id, guard, record_for):
    seal = canon({"context_id": "c", "confirm_class": False, "guard": guard, "links": 1})
    with store.write() as c:
        c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, 'b-1', 0, ?)", (run_id, seal))
        s.dry_run_close(c, f"run:{run_id}", record_for)


def test_a_run_with_a_floor_whose_close_would_be_refused_is_not_admitted(store):
    inputs = _face_inputs()
    # R13-8's face-only dry run passes this input (V's face builds and is strict JSON) ...
    for ts in s.CLOSE_STATES["run"]:
        json.dumps(build_gate_face(**inputs, terminal_state=ts[0]), allow_nan=False)
    # ... but its record does not carry the run's floor, so every close of it would be refused (receipts_carry_guard)
    with pytest.raises(s.CloseUnbuildable, match="close_unbuildable:refused:IntegrityError:D68"):
        _admit(store, "floor-bad", GUARD, _record_for("run:floor-bad", inputs, with_guard=False))
    _admit(store, "floor-ok", GUARD, _record_for("run:floor-ok", inputs, guard=GUARD))
    with store.read() as c:
        assert c.execute("SELECT run_id FROM runs").fetchall() == [("floor-ok",)]
        assert c.execute("SELECT COUNT(*) FROM gate_receipts").fetchone() == (0,)        # the dry run left nothing
        assert c.execute("SELECT COUNT(*) FROM cancels").fetchone() == (0,)
    with store.write() as c:
        s.close_subject(c, subject="run:floor-ok", record=_record_for("run:floor-ok", inputs, guard=GUARD)("killed"),
                        reason="killed:x")
    with store.read() as c:
        assert s.invariant(c) == []


@pytest.mark.parametrize("label,over,err", [
    ("producers None", {"producers": None}, "TypeError"),
    ("NaN first estimate", {"actor_first_estimate": math.nan}, "ValueError"),
    ("Infinity in the gate dict", {"gate": {"verdict": "denied", "score": math.inf}}, "ValueError"),
    ("a set in the gate dict", {"gate": {"verdict": "denied", "tags": {"a"}}}, "TypeError"),
    ("authority None", {"authority": None}, "AttributeError"),
])
def test_an_unbuildable_face_refuses_the_admission(store, label, over, err):
    with pytest.raises(s.CloseUnbuildable, match=f"close_unbuildable:refused:{err}"):
        _admit(store, "bad", [], _record_for("run:bad", _face_inputs(**over)))
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


def test_a_hold_birth_dry_runs_its_request_close(store):
    with store.write() as c:
        c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('h', 'b-1', 0, ?)", (RUN_SEAL,))
        s.dry_run_close(c, "request:h:link-0", _record_for("request:h:link-0", _face_inputs()))
    with pytest.raises(s.CloseUnbuildable, match="close_unbuildable:refused"):
        with store.write() as c:
            s.dry_run_close(c, "request:h:link-0", _record_for("request:h:link-0", _face_inputs(producers=None)))
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)
        assert s.invariant(c) == []


# ---- R14-1: an unattended approve/reopen loop is not progress ---------------------------------------------------------
class _ScheduledCoverage(TestOnlyCoverage):
    """Refresh i covers the effect iff ``answer(i)`` (a grant or revoke moves the head when the answer changes)."""

    __test__ = False

    def __init__(self, answer) -> None:
        self._policies_all = list(_covering("link0", lowest="COOLING_OFF")._policies.values())
        super().__init__(self._policies_all)
        self._covered, self._answer, self.reads = True, answer, 0

    def refresh(self):
        want = bool(self._answer(self.reads))
        if want != self._covered:
            for p in self._policies_all:
                self.grant(p) if want else self.revoke(p.grant_id)
            self._covered = want
        self.reads += 1
        return super().refresh()


# the dispatch reads covered, then every sweep reads T T F F T (silence approves, admission finds the rung risen and
# reopens); found by running every period-1..6 pattern after a covered dispatch through V's sweep (lane a1c1b RETURN)
_FLAP = "TTFFT"
SWEEPS = 24


@pytest.mark.usefixtures("test_only_confined_executor")
def test_an_unattended_approve_reopen_loop_does_not_restart_the_held_count(tmp_path, store):
    cov = _ScheduledCoverage(lambda i: i == 0 or _FLAP[(i - 1) % len(_FLAP)] == "T")
    w = CWorld(tmp_path / "v", coverage=cov, auto_fire=frozenset({"link0"}))
    w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    v = sqlite3.connect(tmp_path / "v" / "store" / vdb.DB_NAME, isolation_level=None)
    v.execute("CREATE TABLE rig_log (k TEXT, by TEXT)")
    v.execute("CREATE TRIGGER rig_dec AFTER UPDATE OF decided ON holds WHEN OLD.decided IS NULL AND NEW.decided IS "
              "NOT NULL BEGIN INSERT INTO rig_log VALUES ('decide:' || NEW.decided, NEW.decided_by); END")
    v.execute("CREATE TRIGGER rig_reopen AFTER UPDATE OF decided ON holds WHEN OLD.decided IS 1 AND NEW.decided IS "
              "NULL BEGIN INSERT INTO rig_log VALUES ('reopen', OLD.decided_by); END")
    per_sweep = []
    for i in range(SWEEPS):
        w.sweep(LATER + timedelta(minutes=i))
        per_sweep.append(v.execute("SELECT COUNT(*) FROM rig_log WHERE k = 'decide:1'").fetchone()[0])
    log = v.execute("SELECT k, by FROM rig_log ORDER BY rowid").fetchall()
    v.close()
    # V loops: one unattended approval and one reopen every sweep, nothing fired, nothing closed
    assert per_sweep == list(range(1, SWEEPS + 1))
    assert log == [("decide:1", "on-loop"), ("reopen", "on-loop")] * SWEEPS and w.outbox() == []
    # the same decisions on a /3 store, one subject fault held after each reopen (the hold open again: a pending
    # subject is closed while its hold is decided, _closed)
    c = raw(store)
    tx(c, ("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('p', 'b', 0, ?)", (RUN_SEAL,)),
       ("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, pending_id, seq) "
        "VALUES ('h-p', 'p', 'link-0', 'b', 'd', 'pp', 1)",))
    counts = {"request:p:link-0": [], "pending:pp": []}
    for k, by in log:
        if k == "decide:1":
            tx(c, ("UPDATE holds SET decided = 1, decided_by = ? WHERE hold_id = 'h-p'", (by,)))
        else:
            tx(c, (REOPEN, ("h-p",)),
               *(lambda conn, subj=subj: counts[subj].append(s.count_held(conn, subj, "subject:fault"))
                 for subj in counts))
    assert c.execute("SELECT COUNT(*) FROM hold_decisions").fetchone() == (SWEEPS,)
    for subj, ns in counts.items():
        assert ns == list(range(1, SWEEPS + 1)), subj   # never restarted: the loop is not progress
    tx(c, lambda conn: s.close_subject(conn, subject="request:p:link-0",
                                       record=rec("r-q", "request:p:link-0", "refused"),
                                       reason="held_limit:subject:fault"))
    assert s.invariant(c) == []
    c.close()
