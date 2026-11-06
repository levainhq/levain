"""Stores as an OLDER levain made them, for the agent-schema migration tests (K2a, key 1105-36).

A historical store is made by the historical code, not hand-written and not a current store with columns dropped (that
would carry every table added since). ``tests/fixtures/agent_tables_v2_v0_11_27.py`` is
``git show v0.11.27:levain/autonomic/agent_tables.py`` and ``tests/fixtures/agent_tables_v3_a0a64485.py`` is
``git show a0a64485:levain/autonomic/agent_tables.py``, each saved unchanged. The opener (``levain.autonomic.db``) is the
same code those releases ran apart from its agent-migration hook, so a store is made by running the CURRENT opener with
the opener's four agent-schema names swapped for the old module's. ``tests/test_agent_tables.py`` pins the two files'
hashes, so an edit to either is a red test, not a quiet change of the past.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from levain.autonomic import db as autonomic_db
from levain.autonomic.db import DB_NAME, AutonomicDB

FIXTURES = Path(__file__).resolve().parent / "fixtures"
V2_FILE = FIXTURES / "agent_tables_v2_v0_11_27.py"
V3_FILE = FIXTURES / "agent_tables_v3_a0a64485.py"

# the file hashes of the saved copies (sha256 of the bytes `git show <rev>:levain/autonomic/agent_tables.py` prints)
V2_SHA256 = "6edc27134ddff9179f0f1dc466efed77cff1127f7c7507c23e724d9e61e0143a"
V3_SHA256 = "08fa05010a41a444b8245ed0e516b5768640ae4bb7b3fedf3190dbe09b6a6f92"

# agent_reservations as 0.11.27 stored it: the table a real /2 store (made by the P5c-3 opener) holds
V2_RESERVATIONS_SQL = ("CREATE TABLE agent_reservations (occurrence_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
                       "window_key TEXT NOT NULL, reserved_usd_micros INTEGER NOT NULL, debited_usd_micros INTEGER, "
                       "state TEXT NOT NULL CHECK (state IN ('open','settled')), settled_reason TEXT)")


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_store(directory: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> Path:
    """The store file an older levain's opener made in ``directory``: ``which`` is ``"v2"`` or ``"v3"``. Returns the
    database path. The patch is undone when the test's ``monkeypatch`` is."""
    old = load(V2_FILE if which == "v2" else V3_FILE, f"agent_tables_{which}_fixture")
    with monkeypatch.context() as m:
        m.setattr(autonomic_db, "ddl_agents", old.ddl_agents)
        m.setattr(autonomic_db, "agent_marker", old.agent_marker)
        m.setattr(autonomic_db, "has_agent_object", old.has_agent_object)
        # 0.11.27 had no agent migration in the opener; the /3 binary had one, and a fresh store never reaches it
        m.setattr(autonomic_db, "migrate_agent_schema", getattr(old, "migrate_agent_schema", lambda conn: False))
        with AutonomicDB(directory).write():
            pass
    path = directory / DB_NAME
    c = sqlite3.connect(path)
    try:
        got = c.execute("SELECT value FROM meta WHERE key = 'agent_schema'").fetchone()[0]
    finally:
        c.close()
    assert got == old.AGENT_SCHEMA, (which, got)
    return path
