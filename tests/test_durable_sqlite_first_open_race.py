"""Two processes opening one store that does not exist yet: the first call of each must be counted.

Measured on 0c24b6f5 (the queue broker's GATE, two sessions at once on a broker with no ``limits.db`` yet): one call
answered ``fail_deny`` with ``database is locked``, raised by ``PRAGMA journal_mode = WAL`` on the new file without
the busy handler waiting (lane-limitsrace RETURN.md). Once the file is in WAL it did not recur."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from levain.consent import limits as L

TRIALS = 30

# One process per session, alive for every trial: it reads a fresh store directory per line and answers the verdict
# as the queue broker's GATE asks for it, so the two calls of a trial start together (both are blocked on the read).
WORKER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from levain.consent import limits as CL
    from levain.qbroker.broker import slot_limits
    full = slot_limits({k: float(CL.DEFAULTS[k]) for k in CL.SLOT_KINDS})
    session = sys.argv[1]
    print("ready", flush=True)
    for line in sys.stdin:
        v = CL.count_call(Path(line.strip()), "claude_code", session, {}, tool_name="Read",
                          tool_input="sha256:" + "0" * 64, limits=full, count_crossed=False, one_agent=True)
        print(json.dumps([v.state, v.reason or ""]), flush=True)
""")


def _counted(qdir: Path) -> dict[str, int]:
    conn = sqlite3.connect(qdir / L.DB_NAME)
    try:
        return dict(conn.execute("SELECT root, calls FROM sessions WHERE agent = 'claude_code'").fetchall())
    finally:
        conn.close()


def test_two_first_calls_on_a_store_not_yet_created_are_both_counted(tmp_path):
    sessions = ("sess-x", "sess-y")
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, s], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True) for s in sessions]
    try:
        assert [p.stdout.readline().strip() for p in procs] == ["ready", "ready"]
        failed = []
        for i in range(TRIALS):
            qdir = Path(tempfile.mkdtemp(dir=tmp_path)) / "limits"
            qdir.mkdir(mode=0o700)
            for p in procs:
                p.stdin.write(f"{qdir}\n")
            for p in procs:
                p.stdin.flush()
            got = [json.loads(p.stdout.readline()) for p in procs]
            failed += [(i, s, state, reason) for s, (state, reason) in zip(sessions, got) if state != "ok"]
            if not failed:
                assert _counted(qdir) == {L.session_root(s): 1 for s in sessions}
        assert failed == []
    finally:
        for p in procs:
            p.stdin.close()
        assert [p.wait(timeout=60) for p in procs] == [0, 0]


# The busy the race met was answered with no wait, which one process cannot stage on demand, so the retry's own
# rules are driven through a connection that answers as told.

class _Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0
        self.statements = []

    def execute(self, sql):
        self.statements.append(sql)
        if sql.startswith("PRAGMA busy_timeout"):
            return None
        assert sql == "PRAGMA journal_mode = WAL"
        self.asked += 1
        a = self.answers.pop(0) if self.answers else self.last
        self.last = a
        if isinstance(a, BaseException):
            raise a
        return a


def _err(text, code):
    e = sqlite3.OperationalError(text)
    e.sqlite_errorcode = code
    return e


def test_the_wal_switch_retries_busy_until_it_is_answered():
    from levain.autonomic import durable_sqlite as D
    conn = _Answers(_err("database is locked", 5), _err("database is locked", 5 | (2 << 8)), "wal")
    D._set_wal(conn)
    assert conn.asked == 3                         # a plain and an extended busy code are both retried
    assert conn.statements[0] == "PRAGMA busy_timeout = 0"   # every try answers at once; the pauses are the wait


def test_the_wal_switch_raises_any_other_error_at_once():
    import pytest

    from levain.autonomic import durable_sqlite as D
    conn = _Answers(_err("disk I/O error", 10))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        D._set_wal(conn)
    assert conn.asked == 1


def test_the_wal_switch_still_busy_at_the_bound_raises_busy(monkeypatch):
    import time

    import pytest

    from levain.autonomic import durable_sqlite as D
    monkeypatch.setattr(D, "BUSY_WAIT_S", 0.2)
    conn = _Answers(_err("database is locked", 5))          # busy every time
    t0 = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        D._set_wal(conn)
    assert conn.asked > 1
    assert time.monotonic() - t0 < 2


def _held_new_file(path: Path) -> sqlite3.Connection:
    """A new database file (not yet WAL) another connection holds the exclusive lock on."""
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def test_a_store_opened_while_another_holds_it_waits_and_keeps_the_busy_wait(tmp_path):
    import threading

    from levain.autonomic import durable_sqlite as D
    holder = _held_new_file(tmp_path / "s.db")
    release = threading.Timer(0.2, lambda: holder.execute("COMMIT"))
    release.start()
    try:
        conn = D.open_durable(tmp_path / "s.db")
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        finally:
            conn.close()
    finally:
        release.join()
        holder.close()
