"""O-75-3 (1026-21 RULINGS, owed to A1c2): a re-admission of a LIVE run on a ``levain-autonomic/3`` store.

On /3 a BEFORE INSERT trigger fires before conflict resolution, so V's ``INSERT OR IGNORE INTO runs`` of a run that
exists is refused (``runs_sealed`` or D69) instead of being the no-op it is on /2 (DESIGN r71: ``_start_in`` becomes
read-first). RED on the base (c0b55b96): the unsealed arm raised IntegrityError (``runs_sealed``); the sealed arms
raised TypeError (no ``seal`` parameter; V's statement with a seal is refused D69, the lane's probe)."""
from __future__ import annotations

import json

from levain.autonomic import schema3 as s
from levain.autonomic.journal import RunJournal

import pytest

RUN_SEAL = json.dumps({"context_id": "c", "confirm_class": False, "guard": [], "links": 2})


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    with st.write() as c:
        c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r1', 'b', 0, ?)", (RUN_SEAL,))
        c.execute("UPDATE runs SET started_at = 't' WHERE run_id = 'r1'")
    return st


@pytest.mark.parametrize("seal", [None, RUN_SEAL])
def test_re_admitting_a_live_sealed_run_is_a_no_op_that_writes_nothing(store, seal):
    with store.read() as c:
        before = c.execute("SELECT * FROM runs").fetchall()
    with store.write() as c:
        if seal is None:
            RunJournal._start_in(c, "r1", "b", 0)        # V's call, as binding.py's admit makes it
        else:
            RunJournal._start_in(c, "r1", "b", 0, seal=seal)
    with store.read() as c:
        assert c.execute("SELECT * FROM runs").fetchall() == before
        assert s.invariant(c) == []


def test_a_new_run_is_admitted_with_its_seal_and_unstarted(store):
    with store.write() as c:
        RunJournal._start_in(c, "r2", "b", 0, seal=RUN_SEAL)
    with store.read() as c:
        assert c.execute("SELECT binding_id, generation, seal, started_at FROM runs WHERE run_id = 'r2'").fetchall() \
            == [("b", 0, RUN_SEAL, None)]


def test_a_new_run_with_no_seal_is_still_refused_on_v3(store):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="born sealed"):
        with store.write() as c:
            RunJournal._start_in(c, "r3", "b", 0)
