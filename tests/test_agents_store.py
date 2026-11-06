"""F0: ``levain.agents.store`` (the one opener, A0 r6 §3.5/§3.7) and the ``levain agents`` seam."""
from __future__ import annotations

import sqlite3

import pytest

from levain import cli
from levain.agents.store import AgentStoreRefused, open_agent_store, open_agent_store_view
from levain.autonomic import agent_tables as at
from levain.autonomic import schema3
from levain.autonomic.db import DB_NAME, AutonomicDB
from tests import _agent_history as hist

NOW = "2026-10-30T18:00:00Z"


def master(path) -> list[tuple]:
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        c.close()


def edit(store_dir, *sqls: str) -> None:
    c = sqlite3.connect(store_dir / DB_NAME, isolation_level=None)
    for s in sqls:
        c.execute(s)
    c.close()


def pre_agent_store(store_dir) -> None:
    """A /2 store as current main made it: no agent marker, no agent object."""
    with AutonomicDB(store_dir).write():
        pass
    c = sqlite3.connect(store_dir / DB_NAME, isolation_level=None)
    c.execute("BEGIN")
    for name in reversed(at.AGENT_TABLES):
        c.execute(f"DROP TABLE {name}")
    c.execute("DELETE FROM meta WHERE key = 'agent_schema'")
    c.execute("COMMIT")
    c.close()


# ------------------------------------------------------------------ the opener


def test_a_fresh_store_opens_with_the_agent_tables_and_a_passing_check(tmp_path):
    s = open_agent_store(tmp_path / "fresh")
    with s.read() as c:
        assert at.check_agent_schema(c) is None
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-autonomic/2"


def test_a_2_store_from_main_gains_the_tables_and_keeps_its_rows(tmp_path):
    d = tmp_path / "old"
    with AutonomicDB(d).write() as c:
        c.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b-old', '{}', 1)")
    pre_agent_store(d)
    assert not any(r[1] in at.AGENT_TABLES for r in master(d / DB_NAME))
    s = open_agent_store(d)
    with s.read() as c:
        assert at.check_agent_schema(c) is None
        assert c.execute("SELECT binding_id FROM bindings").fetchall() == [("b-old",)]


def test_a_3_store_is_refused_by_name_and_nothing_is_created(tmp_path):
    d = tmp_path / "v3"
    with schema3.AutonomicStore3(d).write():
        pass
    before = master(d / DB_NAME)
    with pytest.raises(AgentStoreRefused) as e:
        open_agent_store(d)
    assert str(e.value) == "agents need the /3 agent-proof slice (R15-5)"
    after = master(d / DB_NAME)
    assert after == before
    assert not any(r[1] in at.AGENT_TABLES or r[1] in at.AGENT_TRIGGERS for r in after)
    c = sqlite3.connect(d / DB_NAME)
    assert c.execute("SELECT 1 FROM meta WHERE key = 'agent_schema'").fetchone() is None
    assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-autonomic/3"
    c.close()


def test_agent_objects_with_no_marker_are_refused_and_nothing_is_repaired(tmp_path):
    d = tmp_path / "nomarker"
    open_agent_store(d)
    edit(d, "DELETE FROM meta WHERE key = 'agent_schema'", "DROP TRIGGER agent_specs_keep")
    before = master(d / DB_NAME)
    with pytest.raises(AgentStoreRefused, match="agent objects with no agent_schema marker") as e:
        open_agent_store(d)
    assert "restore `autonomic.db` from a backup made by this version" in str(e.value)
    # r7 (complement F0 L3 r1 LOW): no marker names a writer, so the text does not tell him to run one
    assert "run the levain that wrote" not in str(e.value)
    assert "no agent_schema marker names the levain that wrote them" in str(e.value)
    assert master(d / DB_NAME) == before      # not recreated: the trigger is still missing, no marker written
    c = sqlite3.connect(d / DB_NAME)
    assert c.execute("SELECT 1 FROM meta WHERE key = 'agent_schema'").fetchone() is None
    c.close()


def test_a_marked_store_with_a_dropped_trigger_is_refused_and_reopened_it_still_lacks_it(tmp_path):
    d = tmp_path / "dropped"
    open_agent_store(d)
    edit(d, "DROP TRIGGER agent_occurrences_one_fire")
    for _ in range(2):    # the second open is a fresh object running _ensure_schema again
        with pytest.raises(AgentStoreRefused) as e:
            open_agent_store(d)
        text = str(e.value)
        assert "agent_occurrences_one_fire" in text
        assert "run the levain that wrote marker levain-agent-schema/4" in text
    assert not any(r[1] == "agent_occurrences_one_fire" for r in master(d / DB_NAME))


def test_a_weakened_trigger_body_is_refused(tmp_path):
    d = tmp_path / "weak"
    open_agent_store(d)
    edit(d, "DROP TRIGGER agent_bars_closed_frozen",
         "CREATE TRIGGER agent_bars_closed_frozen BEFORE UPDATE ON agent_bars WHEN 0"
         " BEGIN SELECT RAISE(ABORT, 'a closed bar is never changed'); END")
    with pytest.raises(AgentStoreRefused, match="agent schema differs"):
        open_agent_store(d)


def test_an_unknown_marker_is_refused_and_named(tmp_path):
    d = tmp_path / "m2"
    open_agent_store(d)
    edit(d, "UPDATE meta SET value = 'levain-agent-schema/5' WHERE key = 'agent_schema'")
    with pytest.raises(AgentStoreRefused) as e:
        open_agent_store(d)
    assert "unknown agent schema marker 'levain-agent-schema/5'" in str(e.value)
    assert "run the levain that wrote marker levain-agent-schema/5" in str(e.value)


def test_a_refusal_touches_agents_only_the_autonomic_db_still_opens(tmp_path):
    d = tmp_path / "agentsonly"
    open_agent_store(d)
    edit(d, "DROP TRIGGER agent_specs_frozen")
    with pytest.raises(AgentStoreRefused):
        open_agent_store(d)
    with AutonomicDB(d).write() as c:    # every non-agent path opens the store as before
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-autonomic/2"


# ------------------------------------------------------------------ recursive_triggers


def test_recursive_triggers_is_on_for_every_connection_the_store_hands_out(tmp_path):
    s = open_agent_store(tmp_path / "pragma")
    with s.write() as c:
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    with s.read() as c:
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 1


def test_a_plain_autonomic_db_connection_leaves_the_pragma_off(tmp_path):
    # the contrast: what store.py's subclass changes
    with AutonomicDB(tmp_path / "plain").write() as c:
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 0


def test_replace_of_a_spec_and_of_an_open_bar_is_refused_on_the_store_connection(tmp_path):
    s = open_agent_store(tmp_path / "replace")
    with s.write() as c:
        c.execute("INSERT INTO agent_specs VALUES ('ag-1', 1, 's1', 'r1', '{}', ?)", (NOW,))
        c.execute("INSERT INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar) VALUES ('ab-1','ag-1',?,5)",
                  (NOW,))
    with s.write() as c:
        with pytest.raises(sqlite3.DatabaseError, match="a spec is never replaced"):
            c.execute("INSERT OR REPLACE INTO agent_specs VALUES ('ag-1', 1, 's9', 'r1', '{\"x\":1}', ?)", (NOW,))
    with s.write() as c:
        with pytest.raises(sqlite3.DatabaseError, match="a bar is never replaced"):
            c.execute("INSERT OR REPLACE INTO agent_bars (bar_id, agent_id, barred_at, head_seq_at_bar)"
                      " VALUES ('ab-2','ag-1',?,6)", (NOW,))
    with s.read() as c:
        assert c.execute("SELECT spec_hash FROM agent_specs").fetchall() == [("s1",)]
        assert c.execute("SELECT bar_id FROM agent_bars").fetchall() == [("ab-1",)]


# ------------------------------------------------------------------ the CLI seam


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(["agents", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_levain_agents_passes_a_clean_store(tmp_path, capsys):
    rc, out, err = run_cli(capsys, "--store-dir", str(tmp_path / "ok"))
    assert rc == 0 and err == ""
    assert out.startswith("agent schema levain-agent-schema/4: ok")


def test_levain_agents_refuses_a_3_store(tmp_path, capsys):
    d = tmp_path / "v3"
    with schema3.AutonomicStore3(d).write():
        pass
    rc, out, err = run_cli(capsys, "--store-dir", str(d))
    assert rc == 1 and out == ""
    assert err.strip() == "levain agents: agents need the /3 agent-proof slice (R15-5)"


def test_levain_agents_refuses_a_tampered_store_with_the_way_out(tmp_path, capsys):
    d = tmp_path / "bad"
    open_agent_store(d)
    edit(d, "DROP TRIGGER agent_specs_keep")
    rc, out, err = run_cli(capsys, "--store-dir", str(d))
    assert rc == 1 and out == ""
    assert "agent_specs_keep" in err and "restore `autonomic.db` from a backup made by this version" in err


def test_levain_agents_names_a_store_it_cannot_open(tmp_path, capsys):
    d = tmp_path / "junk"
    d.mkdir()
    (d / DB_NAME).write_text("not a database")
    rc, out, err = run_cli(capsys, "--store-dir", str(d))
    assert rc == 1 and err.startswith("levain agents: cannot open the autonomic store:")


def test_the_seam_has_no_verbs(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["agents", "register"])
    assert e.value.code == 2
    capsys.readouterr()


# ------------------------------------------------------------------ K2a: the marker is read inside every transaction


def test_a_held_agent_store_refuses_its_next_transaction_after_the_marker_moves(tmp_path):
    """The design's 1b' flipped: an object opened BEFORE the marker moved kept reading and writing (a fresh open refused
    it by name, a held one did not). The marker is now read inside every transaction of an opened store."""
    d = tmp_path / "held"
    held = open_agent_store(d)
    with held.read() as c:
        assert at.agent_marker(c) == at.AGENT_SCHEMA
    edit(d, "UPDATE meta SET value = 'levain-agent-schema/5' WHERE key = 'agent_schema'")
    with pytest.raises(AgentStoreRefused, match="unknown agent schema marker 'levain-agent-schema/5'") as e:
        with held.read():
            raise AssertionError("the block must not run")
    assert "run the levain that wrote marker levain-agent-schema/5" in str(e.value)
    with pytest.raises(AgentStoreRefused, match="unknown agent schema marker 'levain-agent-schema/5'"):
        with held.write():
            raise AssertionError("the block must not run")
    edit(d, "UPDATE meta SET value = 'levain-agent-schema/4' WHERE key = 'agent_schema'")
    with held.write() as c:      # and the same object works again once the marker is this binary's
        assert at.agent_marker(c) == at.AGENT_SCHEMA


def test_a_held_store_whose_marker_row_is_gone_is_refused_too(tmp_path):
    d = tmp_path / "heldgone"
    held = open_agent_store(d)
    edit(d, "DELETE FROM meta WHERE key = 'agent_schema'")
    with pytest.raises(AgentStoreRefused, match="no agent_schema marker"):
        with held.read():
            raise AssertionError("the block must not run")


@pytest.mark.parametrize("which,marker", [("v2", "levain-agent-schema/2"), ("v3", "levain-agent-schema/3")])
def test_the_read_only_view_of_an_older_store_names_this_versions_verb_not_the_older_binary(tmp_path, monkeypatch,
                                                                                            which, marker):
    path = hist.make_store(tmp_path / which, monkeypatch, which)
    before = master(path)
    with pytest.raises(AgentStoreRefused) as e:
        open_agent_store_view(path.parent)
    text = str(e.value)
    assert f"unknown agent schema marker '{marker}'" in text
    assert f"{marker} is an older agent schema that the next `levain agents` verb of this version migrates" in text
    assert "run the levain that wrote marker" not in text      # the way out is not an older binary
    assert master(path) == before                              # a view writes nothing
    open_agent_store(path.parent)                              # and that verb does migrate it
    assert open_agent_store_view(path.parent) is not None


def test_a_held_read_only_view_refuses_its_next_read_after_the_marker_moves(tmp_path):
    """fixk2 (L3 k2 r1, codex MED): a view opened at /4 read a store another process had moved to a later marker."""
    d = tmp_path / "heldview"
    open_agent_store(d)
    view = open_agent_store_view(d)
    with view.read() as c:
        assert at.agent_marker(c) == at.AGENT_SCHEMA
    edit(d, "UPDATE meta SET value = 'levain-agent-schema/5' WHERE key = 'agent_schema'")
    with pytest.raises(AgentStoreRefused, match="unknown agent schema marker 'levain-agent-schema/5'"):
        with view.read():
            raise AssertionError("the block must not run")
    edit(d, "UPDATE meta SET value = 'levain-agent-schema/4' WHERE key = 'agent_schema'")
    with view.read() as c:       # the same view reads again once the marker is this binary's
        assert at.agent_marker(c) == at.AGENT_SCHEMA


def test_the_in_memory_view_of_no_store_still_reads_its_empty_tables(tmp_path):
    view = open_agent_store_view(tmp_path / "none")
    assert view.path is None
    with view.read() as c:
        assert c.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
    assert not (tmp_path / "none").exists()


def test_a_future_marker_is_still_sent_to_the_levain_that_wrote_it_even_from_the_view(tmp_path):
    d = tmp_path / "future"
    open_agent_store(d)
    edit(d, "UPDATE meta SET value = 'levain-agent-schema/5' WHERE key = 'agent_schema'")
    with pytest.raises(AgentStoreRefused) as e:
        open_agent_store_view(d)
    assert "run the levain that wrote marker levain-agent-schema/5" in str(e.value)
    assert "older agent schema" not in str(e.value)
