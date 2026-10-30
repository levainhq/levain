"""F0: the agent DDL, its seventeen triggers and the agent schema check (A0 r7 §3.5, §3.7, §4.2).

Built from ``a0_scratch/r6_schema_run.py`` (A0 §1.5), whose checks are asserted here against ``levain.autonomic.agent_tables``
itself: each trigger refuses its own bad write with its own message and is the only refusal on that write; the legitimate
paths pass; the schema check refuses a dropped trigger, a weakened body, an added column, an extra agent index, agent
objects with no marker and an unknown marker, and passes a store the opener made.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from levain.autonomic import agent_tables as at
from levain.autonomic.db import DB_NAME, AutonomicDB

# `sha256("\n".join(AGENT_OBJECT_DDL))`. The compare in check_agent_schema is byte-exact against these constants.
DDL_SHA256 = "2fc46682884787619f53aca1a0a1c6e53e82cb9ec5d497138cd04868c5fb456e"

NOW = "2026-10-30T18:00:00Z"


def test_the_agent_ddl_is_pinned():
    got = hashlib.sha256("\n".join(at.AGENT_OBJECT_DDL).encode()).hexdigest()
    assert got == DDL_SHA256, "an agent DDL edit is a new agent schema marker with a migration (A0 §3.5)"


def test_the_object_names_are_the_spec():
    assert len(at.AGENT_TABLES) == 8 and len(at.AGENT_TRIGGERS) == 17
    assert at.AGENT_SCHEMA == "levain-agent-schema/2"
    ref = at.reference_master()
    assert sorted(r[1] for r in ref if r[0] == "trigger") == sorted(at.AGENT_TRIGGERS)
    assert sorted(r[1] for r in ref if r[0] == "table") == sorted(at.AGENT_TABLES)
    assert [r[1] for r in ref if r[0] == "index" and not r[1].startswith("sqlite_autoindex_")] == ["agent_bars_open"]


def connect(path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def refused(conn, sql, args=()):
    try:
        conn.execute(sql, args)
        return None
    except sqlite3.DatabaseError as e:
        return str(e)


# ------------------------------------------------------------------ the reference and the host step


def test_ddl_agents_on_a_bare_memory_db_raises_and_reference_master_does_not():
    mem = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError, match="no such table: meta"):
        at.ddl_agents(mem)
    mem.close()
    assert at.reference_master()    # builds with no host table


def test_create_agent_objects_touches_no_other_table():
    mem = sqlite3.connect(":memory:")
    at.create_agent_objects(mem)
    names = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "meta" not in names
    assert {n for n in names if not n.startswith("sqlite_")} == set(at.AGENT_TABLES)
    mem.close()


def test_sqlite_strips_if_not_exists_from_the_stored_text():
    # why the check builds its reference with the running SQLite and compares stored text
    probe = sqlite3.connect(":memory:")
    probe.execute("CREATE TABLE IF NOT EXISTS o (a)")
    probe.execute("CREATE TRIGGER IF NOT EXISTS tr BEFORE INSERT ON o BEGIN SELECT 1; END")
    stored = [r[0] for r in probe.execute("SELECT sql FROM sqlite_master ORDER BY name")]
    assert stored == ["CREATE TABLE o (a)", "CREATE TRIGGER tr BEFORE INSERT ON o BEGIN SELECT 1; END"]
    probe.close()


# ------------------------------------------------------------------ the schema check on real stores


@pytest.fixture
def good(tmp_path) -> Path:
    """A /2 store made by the real opener (AutonomicDB), closed; tests work on copies of its file."""
    store = tmp_path / "good"
    db = AutonomicDB(store)
    with db.write() as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-autonomic/2"
    return store / DB_NAME


def tampered(good: Path, tmp_path: Path, name: str, sqls: list[str]) -> sqlite3.Connection:
    p = tmp_path / f"{name}.db"
    src = connect(good)
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src.close()
    p.write_bytes(good.read_bytes())
    t = connect(p)
    for s in sqls:
        t.execute(s)
    return t


def test_the_opener_creates_the_agent_objects_and_the_check_passes(good):
    c = connect(good)
    assert at.agent_marker(c) == at.AGENT_SCHEMA
    assert at.check_agent_schema(c) is None
    assert at.agent_master(c) == at.reference_master()
    c.close()


def test_a_second_open_runs_no_agent_ddl(good):
    c = connect(good)
    before = at.agent_master(c)
    c.close()
    db = AutonomicDB(good.parent)    # a fresh object: _ensure_schema runs again
    with db.write() as c2:
        assert at.agent_master(c2) == before
        assert c2.execute("SELECT count(*) FROM meta WHERE key = 'agent_schema'").fetchone()[0] == 1


def test_a_dropped_trigger_is_refused_and_a_reopen_does_not_recreate_it(good, tmp_path):
    t = tampered(good, tmp_path, "dropped", ["DROP TRIGGER agent_occurrences_one_fire"])
    reason = at.check_agent_schema(t)
    assert reason is not None and reason.startswith("agent schema differs") and "agent_occurrences_one_fire" in reason
    t.close()
    store = tmp_path / "dropped_store"    # the real opener, on that file in a store directory
    store.mkdir()
    (store / DB_NAME).write_bytes((tmp_path / "dropped.db").read_bytes())
    with AutonomicDB(store).write() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'agent_occurrences_one_fire'").fetchone() is None
        assert at.check_agent_schema(c) is not None


def test_a_weakened_body_under_the_same_name_is_refused(good, tmp_path):
    t = tampered(good, tmp_path, "weak", [
        "DROP TRIGGER agent_bars_closed_frozen",
        "CREATE TRIGGER agent_bars_closed_frozen BEFORE UPDATE ON agent_bars WHEN 0"
        " BEGIN SELECT RAISE(ABORT, 'a closed bar is never changed'); END"])
    reason = at.check_agent_schema(t)
    assert reason is not None and "agent_bars_closed_frozen" in reason
    # both sides name it: the stored text differs from the reference's
    assert "missing/changed ['agent_bars_closed_frozen'], extra/changed ['agent_bars_closed_frozen']" in reason
    t.close()


def test_an_added_column_is_refused(good, tmp_path):
    t = tampered(good, tmp_path, "col", ["ALTER TABLE agent_slots ADD COLUMN note TEXT"])
    reason = at.check_agent_schema(t)
    assert reason is not None and "agent_slots" in reason
    t.close()


def test_an_extra_agent_index_is_refused(good, tmp_path):
    t = tampered(good, tmp_path, "extra", ["CREATE INDEX agent_occ_extra ON agent_occurrences(state)"])
    reason = at.check_agent_schema(t)
    assert reason is not None and "agent_occ_extra" in reason
    t.close()


def test_an_extra_trigger_on_an_agent_table_is_refused(good, tmp_path):
    t = tampered(good, tmp_path, "extratrig", [
        "CREATE TRIGGER sneaky AFTER INSERT ON agent_slots BEGIN SELECT 1; END"])
    reason = at.check_agent_schema(t)
    assert reason is not None and "sneaky" in reason
    t.close()


def test_agent_objects_with_no_marker_are_refused_and_nothing_is_created(good, tmp_path):
    t = tampered(good, tmp_path, "nomarker", ["DELETE FROM meta WHERE key = 'agent_schema'"])
    assert at.check_agent_schema(t) == "agent objects with no agent_schema marker"
    assert at.has_agent_object(t)
    t.close()


def test_an_unknown_marker_is_refused(good, tmp_path):
    t = tampered(good, tmp_path, "marker2", [
        "UPDATE meta SET value = 'levain-agent-schema/3' WHERE key = 'agent_schema'"])
    reason = at.check_agent_schema(t)
    assert reason is not None and reason.startswith("unknown agent schema marker")
    assert "'levain-agent-schema/3'" in reason
    t.close()


def test_a_store_made_under_a0_r6s_marker_is_refused_as_unknown(good, tmp_path):
    """/1 named the twelve-trigger text F0 r1 wrote; this binary does not know it, so it refuses and names it."""
    t = tampered(good, tmp_path, "marker1", [
        "UPDATE meta SET value = 'levain-agent-schema/1' WHERE key = 'agent_schema'"])
    assert at.check_agent_schema(t) == "unknown agent schema marker 'levain-agent-schema/1'"
    t.close()


def test_no_marker_and_no_object_is_no_agent_schema(tmp_path):
    c = connect(tmp_path / "bare.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    assert at.check_agent_schema(c) == "no agent schema"
    assert not at.has_agent_object(c)
    c.close()
    assert at.check_agent_schema(sqlite3.connect(":memory:")) == "no agent schema"    # not even a meta table


def test_the_check_writes_nothing(good, tmp_path):
    t = tampered(good, tmp_path, "ro", ["DROP TRIGGER agent_specs_keep"])
    before = t.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    meta_before = t.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    assert at.check_agent_schema(t) is not None
    assert t.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == before
    assert t.execute("SELECT key, value FROM meta ORDER BY key").fetchall() == meta_before
    t.close()


def test_the_opener_hunk_runs_ddl_only_on_an_agent_empty_store(tmp_path):
    """db.py: no DDL on a store with objects and no marker (and no repair), none on a marked store."""
    store = tmp_path / "s"
    db = AutonomicDB(store)
    with db.write():
        pass
    p = store / DB_NAME
    c = connect(p)
    c.execute("DELETE FROM meta WHERE key = 'agent_schema'")
    c.execute("DROP TRIGGER agent_specs_keep")
    c.close()
    with AutonomicDB(store).write() as c2:    # re-open: _ensure_schema runs, and does nothing for agents
        assert at.agent_marker(c2) is None
        assert c2.execute("SELECT 1 FROM sqlite_master WHERE name = 'agent_specs_keep'").fetchone() is None
        assert at.check_agent_schema(c2) == "agent objects with no agent_schema marker"


def test_a_2_store_from_main_gains_the_tables_and_keeps_every_row(tmp_path):
    """A /2 store with no agent object (as current main made it) gains the tables on open and loses no row."""
    store = tmp_path / "old"
    with AutonomicDB(store).write() as c:
        c.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b-old', '{}', 1)")
        c.execute("INSERT INTO fences (binding_id, generation) VALUES ('b-old', 4)")
        store_id = c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
    p = store / DB_NAME
    raw = connect(p)
    raw.execute("BEGIN")
    for name in reversed(at.AGENT_TABLES):
        raw.execute(f"DROP TABLE {name}")    # drops its triggers and index with it
    raw.execute("DELETE FROM meta WHERE key = 'agent_schema'")
    raw.execute("COMMIT")
    assert not at.has_agent_object(raw)
    raw.close()
    with AutonomicDB(store).write() as c:
        assert at.check_agent_schema(c) is None
        assert c.execute("SELECT binding_id, record, seq FROM bindings").fetchall() == [("b-old", "{}", 1)]
        assert c.execute("SELECT generation FROM fences WHERE binding_id = 'b-old'").fetchone()[0] == 4
        assert c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0] == store_id


def test_a_2_store_carrying_agent_objects_does_not_reach_the_3_fingerprint(good):
    """R-A3 (A0 §3.5): /3 is untouched, so the /3 DDL step over a /2 store that holds the agent tables lands on a
    fingerprint that is not /3's. The day R15-5 makes /3 the opened format, the agent tables join ddl_v3 in one step."""
    from levain.autonomic import schema3

    c = connect(good)
    c.execute("BEGIN IMMEDIATE")
    schema3.ddl_v3(c)
    schema3.triggers_v3(c)
    assert schema3.fingerprint(c) != schema3.expected_fingerprint()
    c.execute("ROLLBACK")
    c.close()


# ------------------------------------------------------------------ the triggers, each alone


@pytest.fixture
def tconn(tmp_path):
    c = connect(tmp_path / "t.db")
    at.create_agent_objects(c)
    yield c
    c.close()


def occ(oid, state="admitted", key=None, run=None, **kw):
    cols = dict(occurrence_id=oid, agent_id="ag-1", kind="scheduled", key=key or oid, state=state,
                window_key="d:2026-10-30", spec_hash="s1", binding_id="b1", run_id=run or "run-" + oid,
                admitted_at=NOW)
    cols.update(kw)
    return f"INSERT INTO agent_occurrences ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(cols.values())


LAUNCH = ("UPDATE agent_occurrences SET state='launched', launch_epoch=epoch, link_id='al-1',"
          " link_incarnation='inc', launched_at=? WHERE occurrence_id=?")


def seed(c):
    for oid in ("o-adm", "o-run", "o-done"):
        c.execute(*occ(oid))
    c.execute(LAUNCH, (NOW, "o-run"))
    c.execute(LAUNCH, (NOW, "o-done"))
    c.execute("UPDATE agent_occurrences SET state='done', reason='completed', ended_at=? WHERE occurrence_id='o-done'",
              (NOW,))
    c.execute("INSERT INTO agent_specs VALUES ('ag-1', 1, 's1', 'r1', '{}', ?)", (NOW,))
    c.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES ('ab-open','ag-1',?,5)",
              (NOW,))
    c.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar, closed_at, reason)"
              " VALUES ('ab-closed','ag-2',?,5,?,'abandoned')", (NOW, NOW))


def _dup(replace):
    sql, a = occ("o-dup", key="o-adm")
    return sql.replace("INSERT", "INSERT OR REPLACE", 1) if replace else sql, a


TRIGGER_CASES = {
    "agent_occurrences_frozen": ("UPDATE agent_occurrences SET reason='x' WHERE occurrence_id='o-done'", (),
                                 "agent occurrence is terminal"),
    "agent_occurrences_identity": ("UPDATE agent_occurrences SET key='other' WHERE occurrence_id='o-adm'", (),
                                   "occurrence identity is immutable"),
    "agent_occurrences_launch_epoch": (
        "UPDATE agent_occurrences SET state='launched', launch_epoch=epoch, link_id='al-1', link_incarnation='i'"
        " WHERE occurrence_id='o-adm'", (), "launch must capture launch_epoch, link_id and link_incarnation"),
    "agent_occurrences_prelaunch": ("UPDATE agent_occurrences SET link_id='attacker' WHERE occurrence_id='o-adm'", (),
                                    "launch columns are written only by the launch"),
    "agent_occurrences_launch_immutable": ("UPDATE agent_occurrences SET link_id='al-2' WHERE occurrence_id='o-run'",
                                           (), "launch columns are immutable"),
    "agent_occurrences_insert": (*occ("o-new", state="done", reason="x", ended_at=NOW),
                                 "a claim inserts admitted or skipped-overlap"),
    # a plain duplicate INSERT is also refused by UNIQUE; one_fire is what refuses the REPLACE form
    "agent_occurrences_one_fire": (*_dup(True), "one key is one fire"),
    "agent_occurrences_keep": ("DELETE FROM agent_occurrences WHERE occurrence_id='o-done'", (),
                               "occurrences are never deleted"),
    "agent_specs_frozen": ("UPDATE agent_specs SET spec='{\"x\":1}'", (), "agent specs are immutable"),
    "agent_specs_keep": ("DELETE FROM agent_specs", (), "agent specs are never deleted"),
    "agent_bars_keep": ("DELETE FROM agent_bars WHERE bar_id='ab-open'", (), "bars are closed, never deleted"),
    "agent_bars_closed_frozen": ("UPDATE agent_bars SET last_error='stale_head' WHERE bar_id='ab-closed'", (),
                                 "a closed bar is never changed"),
    # r7 (F0 L3 r1, each reproduced as accepted before its trigger existed)
    "agent_occurrences_transition": ("UPDATE agent_occurrences SET state='admitted' WHERE occurrence_id='o-run'", (),
                                     "not a step of the occurrence state machine"),
    "agent_occurrences_epoch": ("UPDATE agent_occurrences SET epoch=epoch-1 WHERE occurrence_id='o-run'", (),
                                "an occurrence epoch never goes down"),
    # the tconn connection has recursive_triggers off, as a plain AutonomicDB connection does
    "agent_specs_no_replace": ("INSERT OR REPLACE INTO agent_specs VALUES ('ag-1', 1, 's9', 'r1', '{\"evil\":1}', ?)",
                               (NOW,), "a spec is never replaced"),
    "agent_bars_no_replace": ("INSERT OR REPLACE INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar)"
                              " VALUES ('ab-2','ag-1',?,6)", (NOW,), "a bar is never replaced"),
    "agent_bars_identity": ("UPDATE agent_bars SET agent_id='ag-7' WHERE bar_id='ab-open'", (),
                            "bar identity is immutable"),
}


def test_there_is_one_case_per_trigger():
    assert sorted(TRIGGER_CASES) == sorted(at.AGENT_TRIGGERS)


@pytest.mark.parametrize("name", at.AGENT_TRIGGERS)
def test_each_trigger_refuses_its_own_write_with_its_own_message(tconn, name):
    seed(tconn)
    sql, args, message = TRIGGER_CASES[name]
    assert refused(tconn, sql, args) == message


@pytest.mark.parametrize("name", at.AGENT_TRIGGERS)
def test_each_trigger_is_the_only_refusal_on_its_write(tconn, name):
    """With that one trigger dropped (inside a savepoint) the same write is accepted: no other guard shadows it."""
    seed(tconn)
    sql, args, _ = TRIGGER_CASES[name]
    tconn.execute("SAVEPOINT alone")
    tconn.execute(f"DROP TRIGGER {name}")
    assert refused(tconn, sql, args) is None
    tconn.execute("ROLLBACK TO alone")
    tconn.execute("RELEASE alone")


def test_the_terminal_shape_check_refuses_a_terminal_row_with_no_reason_or_end(tconn):
    seed(tconn)
    e = refused(tconn, *occ("o-ovl", state="skipped-overlap"))
    assert e is not None and "CHECK constraint failed" in e
    e = refused(tconn, "UPDATE agent_occurrences SET state='done', ended_at=? WHERE occurrence_id='o-run'", (NOW,))
    assert e is not None and "CHECK constraint failed" in e


def test_a_duplicate_claim_by_insert_or_ignore_is_refused_too(tconn):
    """RAISE(ABORT) ignores the OR clause: the claim must SELECT first (A0 §4.2)."""
    seed(tconn)
    sql, a = occ("o-dup2", key="o-adm")
    assert refused(tconn, sql.replace("INSERT", "INSERT OR IGNORE", 1), a) == "one key is one fire"


def test_the_legitimate_paths_are_accepted(tconn):
    seed(tconn)
    legit = [
        ("claim", *occ("o-c1")),
        ("skipped-overlap with its reason and end", *occ("o-c2", state="skipped-overlap", reason="overlap",
                                                         ended_at=NOW)),
        ("bar epoch raise, admitted", "UPDATE agent_occurrences SET epoch=epoch+1 WHERE occurrence_id='o-c1'", ()),
        ("honest launch", LAUNCH, (NOW, "o-c1")),
        ("bar epoch raise, launched", "UPDATE agent_occurrences SET epoch=epoch+1 WHERE occurrence_id='o-c1'", ()),
        ("lease renew, launched", "UPDATE agent_occurrences SET lease_until=? WHERE occurrence_id='o-c1'", (NOW,)),
        ("lapse recovery", "UPDATE agent_occurrences SET state='stopped', epoch=epoch+1, reason='crash_recovered',"
         " ended_at=? WHERE occurrence_id='o-c1' AND state='launched'", (NOW,)),
        ("admitted -> missed", "UPDATE agent_occurrences SET state='missed', reason='no_slot', ended_at=?"
         " WHERE occurrence_id='o-adm'", (NOW,)),
    ]
    for label, sql, args in legit:
        assert refused(tconn, sql, args) is None, label
    cur = tconn.execute("UPDATE agent_occurrences SET state='stopped', reason='bar', ended_at=?"
                        " WHERE occurrence_id='o-c1' AND state='launched'", (NOW,))
    assert cur.rowcount == 0    # a second settle guarded by state='launched' touches no row and raises nothing
    assert refused(tconn, "UPDATE agent_occurrences SET state='stopped', reason='bar', ended_at=?"
                          " WHERE occurrence_id='o-c1'", (NOW,)) == "agent occurrence is terminal"


def test_the_bar_close_guarded_by_closed_at_touches_no_row_after_an_abandon(tconn):
    tconn.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES ('ab-c8','ag-3',?,5)",
                  (NOW,))
    tconn.execute("UPDATE agent_bars SET closed_at=?, reason='abandoned' WHERE bar_id='ab-c8' AND closed_at IS NULL",
                  (NOW,))
    cur = tconn.execute("UPDATE agent_bars SET revoke_seq=6, closed_at=?, reason='signed' WHERE bar_id='ab-c8'"
                        " AND closed_at IS NULL", (NOW,))
    assert cur.rowcount == 0
    assert refused(tconn, "UPDATE agent_bars SET revoke_seq=6, closed_at=?, reason='signed' WHERE bar_id='ab-c8'",
                   (NOW,)) == "a closed bar is never changed"


def test_one_open_bar_per_agent(tconn):
    """The trigger refuses a second open bar first; the partial unique index stays as the open-bar lookup and still
    refuses it with the trigger dropped."""
    ins = "INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES (?, 'ag-1', ?, 5)"
    tconn.execute(ins, ("ab-1", NOW))
    assert refused(tconn, ins, ("ab-2", NOW)) == "a bar is never replaced"
    tconn.execute("DROP TRIGGER agent_bars_no_replace")
    e = refused(tconn, ins, ("ab-2", NOW))
    assert e is not None and "UNIQUE constraint failed" in e


def test_every_step_of_the_a0_transition_table_is_accepted_and_no_other(tconn):
    """A0 §2.5's table, whole: from admitted and from launched, each listed target is accepted and each other one is
    refused by agent_occurrences_transition (launched -> admitted and -> a skip state were accepted before r7)."""
    allowed = {"admitted": {"admitted", "launched", "skipped-no-input", "skipped-budget", "stopped", "missed"},
               "launched": {"launched", "done", "failed", "stopped"}}
    states = ("admitted", "launched", "done", "failed", "stopped", "skipped-no-input", "skipped-overlap",
              "skipped-budget", "missed")
    for i, (start, ok) in enumerate(allowed.items()):
        for j, to in enumerate(states):
            oid = f"o-{i}-{j}"
            tconn.execute(*occ(oid))
            if start == "launched":
                tconn.execute(LAUNCH, (NOW, oid))
            launch_cols = (", launch_epoch=epoch, link_id='al-1', link_incarnation='inc', launched_at='t'"
                           if start == "admitted" and to == "launched" else "")
            end = "" if to in ("admitted", "launched") else ", reason='r', ended_at='t'"
            e = refused(tconn, f"UPDATE agent_occurrences SET state='{to}'{launch_cols}{end} WHERE occurrence_id=?",
                        (oid,))
            if to in ok:
                assert e is None, (start, to, e)
            else:
                assert e == "not a step of the occurrence state machine", (start, to, e)


def test_a_bar_raised_epoch_cannot_be_written_back_after_an_abandon(tconn):
    """codex F0 L3 r1 HIGH: launch_epoch 0, a bar raises epoch to 1, the bar is abandoned, a stale full-row write puts
    epoch 0 back so fence_now would equal the launch fence again. Refused; the raise itself is accepted."""
    tconn.execute(*occ("o-e"))
    tconn.execute(LAUNCH, (NOW, "o-e"))
    tconn.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES ('ab-e','ag-1',?,5)",
                  (NOW,))
    tconn.execute("UPDATE agent_occurrences SET epoch=epoch+1 WHERE agent_id='ag-1' AND state IN ('admitted','launched')")
    tconn.execute("UPDATE agent_bars SET closed_at=?, reason='abandoned' WHERE bar_id='ab-e' AND closed_at IS NULL",
                  (NOW,))
    assert refused(tconn, "UPDATE agent_occurrences SET epoch=0, lease_until=? WHERE occurrence_id='o-e'",
                   (NOW,)) == "an occurrence epoch never goes down"
    assert tconn.execute("SELECT epoch, launch_epoch FROM agent_occurrences WHERE occurrence_id='o-e'"
                         ).fetchone() == (1, 0)


# ------------------------------------------------------------------ REPLACE


@pytest.mark.parametrize("pragma", [0, 1])
def test_replace_on_occurrences_is_refused_with_the_pragma_off_and_on(tmp_path, pragma):
    p = connect(tmp_path / f"r{pragma}.db")
    at.create_agent_objects(p)
    p.execute(f"PRAGMA recursive_triggers = {pragma}")
    p.execute(*occ("o-1"))
    p.execute(LAUNCH, (NOW, "o-1"))
    p.execute("UPDATE agent_occurrences SET state='done', reason='completed', ended_at=? WHERE occurrence_id='o-1'",
              (NOW,))
    sql, a = occ("o-1")
    assert refused(p, sql.replace("INSERT", "INSERT OR REPLACE", 1), a) == "one key is one fire"
    p.execute(*occ("o-2"))
    assert refused(p, "UPDATE OR REPLACE agent_occurrences SET key='o-1', occurrence_id='o-1x' WHERE occurrence_id='o-2'"
                   ) == "occurrence identity is immutable"
    p.close()


def _replace_spec_and_bar(p):
    p.execute("INSERT INTO agent_specs VALUES ('ag-1', 1, 's1', 'r1', '{}', ?)", (NOW,))
    e_spec = refused(p, "INSERT OR REPLACE INTO agent_specs VALUES ('ag-1', 1, 's9', 'r1', '{\"evil\":1}', ?)", (NOW,))
    spec_now = p.execute("SELECT spec_hash FROM agent_specs WHERE agent_id='ag-1' AND version=1").fetchone()
    p.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar, pending_signature,"
              " pending_signed_at) VALUES ('ab-1','ag-1',?,5,'sig',?)", (NOW, NOW))
    e_bar = refused(p, "INSERT OR REPLACE INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar)"
                       " VALUES ('ab-2','ag-1',?,6)", (NOW,))
    n = p.execute("SELECT count(*) FROM agent_bars WHERE bar_id='ab-1'").fetchone()[0]
    return e_spec, spec_now[0], e_bar, n


@pytest.mark.parametrize("pragma", [0, 1])
def test_replace_of_a_spec_and_a_bar_is_refused_with_the_pragma_off_and_on(tmp_path, pragma):
    """r6 closed this with the pragma on store.py's connections only; with it off (a plain AutonomicDB connection) the
    REPLACE swapped the spec and deleted the open bar. r7's _no_replace triggers refuse it on any connection."""
    p = connect(tmp_path / f"rep{pragma}.db")
    at.create_agent_objects(p)
    p.execute(f"PRAGMA recursive_triggers = {pragma}")
    e_spec, spec_now, e_bar, n = _replace_spec_and_bar(p)
    assert (e_spec, spec_now) == ("a spec is never replaced", "s1")
    assert (e_bar, n) == ("a bar is never replaced", 1)
    p.close()


def test_a_replace_colliding_only_on_rowid_is_what_the_pragma_still_closes(tmp_path):
    """The residue the keyed triggers leave (A0 r7 §4.2): a REPLACE naming an existing rowid deletes that row with no
    DELETE trigger unless recursive_triggers is on, which store.py sets on every connection."""
    out = {}
    for pragma in (0, 1):
        p = connect(tmp_path / f"rowid{pragma}.db")
        at.create_agent_objects(p)
        p.execute(f"PRAGMA recursive_triggers = {pragma}")
        p.execute("INSERT INTO agent_specs VALUES ('ag-1', 1, 's1', 'r1', '{}', ?)", (NOW,))
        rid = p.execute("SELECT rowid FROM agent_specs").fetchone()[0]
        out[pragma] = refused(p, "INSERT OR REPLACE INTO agent_specs (rowid, agent_id, version, spec_hash, role_hash,"
                                 " spec, created_at) VALUES (?, 'ag-1', 7, 's7', 'r1', '{}', ?)", (rid, NOW))
        p.close()
    assert out == {0: None, 1: "agent specs are never deleted"}
