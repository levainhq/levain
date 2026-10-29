"""The ledger's BOUND columns: added in place to a released store (format marker unchanged), frozen by the trigger,
lifted onto the pre row of a gated call, and invisible to the readers that open a store read-only."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.cli import main as levain_main
from levain.consent import ledger as ledger_mod
from levain.consent.bound import BoundRecord
from levain.consent.ledger import STORE_FORMAT, Ledger, LedgerStoreError, request_outcomes

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
TOUCHED = BoundRecord("touched", 2, ("network", "other"))

# the released ledger's schema, copied literally: a store a released levain wrote is built from exactly this
OLD_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS calls ("
    "id TEXT PRIMARY KEY, "
    "agent TEXT NOT NULL CHECK (agent IN ('claude_code','codex')), "
    "agent_session_id TEXT NOT NULL, tool_use_id TEXT, received_at TEXT NOT NULL, tool_name TEXT NOT NULL, "
    "tool_input_digest TEXT NOT NULL, cwd TEXT NOT NULL, "
    "phase TEXT NOT NULL CHECK (phase IN ('pre','post','post_failure','denied_by_gate')), "
    "stamp TEXT NOT NULL CHECK (stamp IN ('grant','ruling','answer','tell','allow','deny','unknown','pending')), "
    "outcome TEXT NOT NULL CHECK (outcome IN ('accepted','refused','unknown')), "
    "stamp_detail TEXT, request_id TEXT, stamped_at TEXT, "
    "source_path TEXT, turn_id TEXT, permission_mode TEXT, "
    "CHECK ((stamp = 'pending') = (stamped_at IS NULL)), "
    "CHECK (outcome <> 'refused' OR stamp IN ('unknown', 'pending')))",
    "CREATE UNIQUE INDEX IF NOT EXISTS calls_once ON calls (agent, agent_session_id, tool_use_id, phase) "
    "WHERE tool_use_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS calls_window ON calls (agent, agent_session_id, received_at)",
    "CREATE INDEX IF NOT EXISTS calls_time ON calls (received_at)",
    "CREATE INDEX IF NOT EXISTS calls_pending ON calls (agent, agent_session_id) WHERE stamp = 'pending'",
    "CREATE TRIGGER IF NOT EXISTS calls_frozen BEFORE UPDATE ON calls WHEN NOT ("
    "OLD.stamp = 'pending' AND NEW.stamp <> 'pending' AND NEW.stamped_at IS NOT NULL "
    "AND NEW.id IS OLD.id AND NEW.agent IS OLD.agent AND NEW.agent_session_id IS OLD.agent_session_id "
    "AND NEW.tool_use_id IS OLD.tool_use_id AND NEW.received_at IS OLD.received_at "
    "AND NEW.tool_name IS OLD.tool_name AND NEW.tool_input_digest IS OLD.tool_input_digest "
    "AND NEW.cwd IS OLD.cwd AND NEW.phase IS OLD.phase AND NEW.request_id IS OLD.request_id "
    "AND NEW.source_path IS OLD.source_path AND NEW.turn_id IS OLD.turn_id "
    "AND NEW.permission_mode IS OLD.permission_mode) "
    "BEGIN SELECT RAISE(ABORT, 'a ledger row moves only from pending to a final stamp, once'); END",
    "CREATE TRIGGER IF NOT EXISTS calls_kept BEFORE DELETE ON calls "
    "BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS calls_no_replace BEFORE INSERT ON calls "
    "WHEN EXISTS (SELECT 1 FROM calls WHERE id = NEW.id) "
    "BEGIN SELECT RAISE(ABORT, 'a ledger row id is never reused'); END",
)
OLD_ROW = ("INSERT INTO calls (id, agent, agent_session_id, tool_use_id, received_at, tool_name, tool_input_digest, "
           "cwd, phase, stamp, outcome, stamp_detail, request_id, stamped_at, source_path, turn_id, permission_mode) "
           "VALUES (?, 'codex', 's', ?, '2026-10-29T11:00:00Z', 'Bash', 'd', '/w', ?, ?, ?, NULL, ?, ?, NULL, NULL, "
           "NULL)")


def _consent(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def _old_store(directory: Path) -> Path:
    path = directory / "ledger.db"
    conn = sqlite3.connect(path)
    for stmt in OLD_SCHEMA:
        conn.execute(stmt)
    conn.execute("INSERT INTO meta (key, value) VALUES ('format', 'levain-authority-ledger/1')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('ledger_id', 'al-old')")
    conn.execute(OLD_ROW, ("old-pending", "t1", "post", "pending", "unknown", None, None))
    conn.execute(OLD_ROW, ("old-stamped", "t2", "pre", "grant", "accepted", "r1", "2026-10-29T11:00:01Z"))
    conn.commit()
    conn.close()
    os.chmod(path, 0o600)
    return path


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(_consent(tmp_path), clock=lambda: NOW)


def _post(ledger: Ledger, bound: BoundRecord | None = None, **kw) -> str:
    kw = {"agent": "codex", "agent_session_id": "s", "tool_name": "Bash", "tool_input": {"command": "x"},
          "cwd": "/w", "phase": "post", "tool_use_id": "t", **kw}
    return ledger.record_call(bound=bound, **kw)


def _columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(calls)")]
    finally:
        conn.close()


def _raw_insert(path: Path, **cols) -> None:
    base = {"id": "raw-" + os.urandom(4).hex(), "agent": "codex", "agent_session_id": "s", "received_at": "t",
            "tool_name": "Bash", "tool_input_digest": "d", "cwd": "/w", "phase": "post", "stamp": "unknown",
            "outcome": "unknown", "stamped_at": "t"}
    base.update(cols)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"INSERT INTO calls ({','.join(base)}) VALUES ({','.join('?' * len(base))})", tuple(base.values()))
        conn.commit()
    finally:
        conn.close()


def test_new_store_has_columns_format_unchanged(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.counts()
    assert _columns(ledger.path)[-3:] == ["bound", "bound_denials", "bound_classes"]
    conn = sqlite3.connect(ledger.path)
    assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == "levain-authority-ledger/1"
    conn.close()
    assert STORE_FORMAT == "levain-authority-ledger/1"


def test_post_row_stores_bound(tmp_path):
    ledger = _ledger(tmp_path)
    call = ledger.get(_post(ledger, TOUCHED))
    assert (call.bound, call.bound_denials, call.bound_classes) == ("touched", 2, ("network", "other"))
    plain = ledger.get(_post(ledger, tool_use_id="t2"))
    assert (plain.bound, plain.bound_denials, plain.bound_classes) == (None, None, None)
    none = ledger.get(_post(ledger, BoundRecord("none", 0, ()), tool_use_id="t3"))
    assert (none.bound, none.bound_denials, none.bound_classes) == ("none", 0, None)


def test_bound_only_on_post_rows(tmp_path):
    ledger = _ledger(tmp_path)
    for phase in ("pre", "denied_by_gate"):
        with pytest.raises(ValueError):
            _post(ledger, TOUCHED, phase=phase, tool_use_id=phase)
    with pytest.raises(ValueError):
        _post(ledger, {"bound": "touched"})  # type: ignore[arg-type]
    assert _post(ledger, TOUCHED, phase="post_failure", tool_use_id="f")


def test_trigger_freezes_bound(tmp_path):
    ledger = _ledger(tmp_path)
    pending = _post(ledger, TOUCHED)
    assert ledger.fill_stamp(pending, "grant")
    assert ledger.get(pending).bound == "touched"
    conn = sqlite3.connect(ledger.path, isolation_level=None)
    swap = "UPDATE calls SET bound = 'clean', bound_denials = 0, bound_classes = NULL WHERE id = ?"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(swap, (pending,))
    other = _post(ledger, TOUCHED, tool_use_id="t2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE calls SET stamp = 'grant', stamped_at = 'x', bound = 'clean', bound_denials = 0, "
                     "bound_classes = NULL WHERE id = ?", (other,))
    conn.close()
    assert ledger.get(other).stamp == "pending" and ledger.get(other).bound == "touched"


@pytest.mark.parametrize("cols", [
    {"bound": "touched", "bound_denials": 0, "bound_classes": "other"},
    {"bound": "unknown", "bound_denials": 0, "bound_classes": "other"},
    {"bound": "clean", "bound_denials": 1, "bound_classes": None},
    {"bound": "none", "bound_denials": None, "bound_classes": None},
    {"bound": "touched", "bound_denials": 1, "bound_classes": "Other"},
    {"bound": "touched", "bound_denials": 1.5, "bound_classes": "other"},
    {"bound": "touched", "bound_denials": 1000, "bound_classes": "other"},
    {"bound": "bogus", "bound_denials": 0, "bound_classes": None},
])
def test_sql_checks(tmp_path, cols):
    ledger = _ledger(tmp_path)
    ledger.counts()
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(ledger.path, **cols)
    _raw_insert(ledger.path, bound="touched", bound_denials=1, bound_classes="other")


def test_migrates_a_released_store_in_place(tmp_path):
    path = _old_store(_consent(tmp_path))
    before = sqlite3.connect(path).execute("SELECT id, rowid FROM calls ORDER BY rowid").fetchall()
    assert len(_columns(path)) == 17
    ledger = Ledger(path.parent, clock=lambda: NOW)
    assert ledger.get("old-pending").bound is None
    assert _columns(path)[-3:] == ["bound", "bound_denials", "bound_classes"]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT id, rowid FROM calls ORDER BY rowid").fetchall() == before
    assert conn.execute("SELECT count(*) FROM calls WHERE bound IS NOT NULL").fetchone()[0] == 0
    assert "NEW.bound " in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'calls_frozen'").fetchone()[0]
    assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == STORE_FORMAT
    assert conn.execute("SELECT value FROM meta WHERE key = 'ledger_id'").fetchone()[0] == "al-old"
    conn.close()
    assert ledger.fill_stamp("old-pending", "grant")
    # a new bound row lands beside the old ones
    assert ledger.get(_post(ledger, TOUCHED, tool_use_id="new")).bound == "touched"


def test_an_older_writer_still_works(tmp_path):
    path = _old_store(_consent(tmp_path))
    Ledger(path.parent, clock=lambda: NOW).counts()
    conn = sqlite3.connect(path)
    conn.execute(OLD_ROW, ("older-writer", "t9", "post", "pending", "unknown", None, None))
    # the released fill_stamp statement, which names no bound column
    cur = conn.execute("UPDATE calls SET stamp = ?, outcome = ?, stamp_detail = ?, stamped_at = ? "
                       "WHERE id = ? AND stamp = 'pending'", ("grant", "accepted", None, "2026-10-29T12:00:00Z",
                                                              "older-writer"))
    conn.commit()
    assert cur.rowcount == 1
    assert conn.execute("SELECT bound, bound_denials, bound_classes FROM calls WHERE id = 'older-writer'"
                        ).fetchone() == (None, None, None)
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = _old_store(_consent(tmp_path))
    Ledger(path.parent, clock=lambda: NOW).counts()
    dump = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    first = sqlite3.connect(path).execute(dump).fetchall()
    Ledger(path.parent, clock=lambda: NOW).counts()
    assert sqlite3.connect(path).execute(dump).fetchall() == first


def test_per_call_lifts_the_post_bound(tmp_path):
    ledger = _ledger(tmp_path)
    _post(ledger, phase="pre", stamp="grant", request_id="r1")
    _post(ledger, TOUCHED)
    rows = ledger.window("codex", "s", "1h")
    assert len(rows) == 1
    assert rows[0].phase == "pre" and rows[0].bound == "touched" and rows[0].bound_classes == ("network", "other")
    assert [c.phase for c in ledger.pending()] == ["post"] or ledger.pending() == []


def test_readonly_readers_on_an_unmigrated_store(tmp_path):
    assert "bound" not in ledger_mod._PER_CALL
    assert "bound" in ledger_mod._PER_CALL_FULL
    path = _old_store(_consent(tmp_path))
    conn = sqlite3.connect(path)
    conn.execute(OLD_ROW, ("old-pre", "t3", "pre", "grant", "accepted", "req-1", "2026-10-29T11:00:02Z"))
    conn.commit()
    conn.close()
    assert request_outcomes(path.parent, ["req-1"]) == {"req-1": "unknown"}
    assert len(_columns(path)) == 17


def test_newer_format_refused(tmp_path):
    path = _old_store(_consent(tmp_path))
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = 'levain-authority-ledger/2' WHERE key = 'format'")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerStoreError):
        Ledger(path.parent).counts()
    assert len(_columns(path)) == 17


def test_cli_json_carries_bound(tmp_path, capsys):
    ledger = Ledger(tmp_path / "consent")
    ledger.counts()
    os.chmod(tmp_path / "consent", 0o700)
    ledger.record_call(agent="codex", agent_session_id="sess-1", tool_name="Bash", tool_input={}, cwd="/w",
                       phase="post", bound=TOUCHED)
    q = ["--queue-dir", str(tmp_path / "consent")]
    assert levain_main(["consent", "ledger", "window", "--agent", "codex", "--session", "sess-1", "--since", "1h",
                        *q, "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert (row["bound"], row["bound_denials"], row["bound_classes"]) == ("touched", 2, ["network", "other"])
