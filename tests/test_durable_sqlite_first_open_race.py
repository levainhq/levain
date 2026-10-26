"""Two processes opening one store that does not exist yet: the first call of each must be counted.

Measured on 0c24b6f5 (the queue broker's GATE, two sessions at once on a broker with no ``limits.db`` yet): one call
answered ``fail_deny`` with ``database is locked``, raised by ``PRAGMA journal_mode = WAL`` on the new file without
the busy handler waiting (lane-limitsrace RETURN.md). Once the file is in WAL it did not recur."""
from __future__ import annotations

import json
import queue
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

from levain.consent import limits as L

TRIALS = 30
DEADLINE_S = 60.0

# One process per session, alive for every trial. A trial is two lines on stdin: ``arm <dir>`` (the worker answers
# ``armed``) and then ``go``. The parent sends ``go`` to both only after both answered ``armed``, so the two calls
# start together, and the verdict is what the queue broker's GATE asks for.
WORKER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from levain.consent import limits as CL
    from levain.qbroker.broker import slot_limits
    full = slot_limits({k: float(CL.DEFAULTS[k]) for k in CL.SLOT_KINDS})
    session = sys.argv[1]
    print("ready", flush=True)
    for line in sys.stdin:
        cmd, _, arg = line.strip().partition(" ")
        assert cmd == "arm", line
        print("armed", flush=True)
        assert sys.stdin.readline().strip() == "go"
        v = CL.count_call(Path(arg), "claude_code", session, {}, tool_name="Read",
                          tool_input="sha256:" + "0" * 64, limits=full, count_crossed=False, one_agent=True)
        print(json.dumps([v.state, v.reason or ""]), flush=True)
""")


class _Worker:
    """A worker process whose stdout is read on a thread, so every read has a deadline."""

    def __init__(self, session: str):
        self.session = session
        self.proc = subprocess.Popen([sys.executable, "-c", WORKER, session], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line.strip())
        self.lines.put(None)

    def read(self) -> str:
        try:
            line = self.lines.get(timeout=DEADLINE_S)
        except queue.Empty:
            raise AssertionError(f"worker {self.session} gave no answer in {DEADLINE_S}s") from None
        assert line is not None, f"worker {self.session} exited (code {self.proc.poll()})"
        return line

    def send(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def reap(self, ok: bool) -> None:
        """Close its stdin and wait; on a failed run kill it instead. Exit code is asserted only on success."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if not ok:
            self.proc.kill()
        try:
            self.proc.wait(timeout=DEADLINE_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"worker {self.session} did not exit") from None
        if ok:
            assert self.proc.returncode == 0, f"worker {self.session} exited {self.proc.returncode}"


def _counted(qdir: Path) -> dict[str, int]:
    conn = sqlite3.connect(qdir / L.DB_NAME)
    try:
        return dict(conn.execute("SELECT root, calls FROM sessions WHERE agent = 'claude_code'").fetchall())
    finally:
        conn.close()


def run_first_open_race(base: Path, trials: int = TRIALS) -> tuple[list, list]:
    """Two real processes, ``trials`` times, each making the first call on a store directory that does not hold
    ``limits.db`` yet. Returns (calls not answered ok, trials whose two ok calls were not both counted)."""
    sessions = ("sess-x", "sess-y")
    workers: list = []
    ok = False
    try:
        for s in sessions:
            workers.append(_Worker(s))
        assert [w.read() for w in workers] == ["ready", "ready"]
        failed, miscounted = [], []
        for i in range(trials):
            qdir = Path(tempfile.mkdtemp(dir=base)) / "limits"
            qdir.mkdir(mode=0o700)
            for w in workers:
                w.send(f"arm {qdir}")
            assert [w.read() for w in workers] == ["armed", "armed"]
            for w in workers:
                w.send("go")
            got = [json.loads(w.read()) for w in workers]
            bad = [(i, w.session, state, reason) for w, (state, reason) in zip(workers, got) if state != "ok"]
            failed += bad
            if not bad:
                counted = _counted(qdir)
                if counted != {L.session_root(s): 1 for s in sessions}:
                    miscounted.append((i, counted))
        ok = True
        return failed, miscounted
    finally:
        first = None
        for w in workers:   # reap every worker even when an earlier reap raises; raise the first failure after
            try:
                w.reap(ok)
            except BaseException as exc:
                first = first or exc
        if first is not None:
            raise first


def test_two_first_calls_on_a_store_not_yet_created_are_both_counted(tmp_path):
    failed, miscounted = run_first_open_race(tmp_path)
    assert failed == []
    assert miscounted == []


# The busy the race met was answered with no wait, which one process cannot stage on demand, so the retry's own
# rules are driven through a connection that answers as told.

class _Row:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return None if self.value is None else (self.value,)


class _Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.last = None
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
        return _Row(a)


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


def test_a_bare_locked_error_with_no_errorcode_attribute_is_retried():
    # Python before 3.11 has no ``sqlite_errorcode``; the message is then the only signal of SQLITE_BUSY.
    from levain.autonomic import durable_sqlite as D
    conn = _Answers(sqlite3.OperationalError("database is locked"), "wal")
    D._set_wal(conn)
    assert conn.asked == 2


def test_a_table_locked_error_with_no_errorcode_attribute_is_not_retried():
    import pytest

    from levain.autonomic import durable_sqlite as D
    conn = _Answers(sqlite3.OperationalError("database table is locked"))   # SQLITE_LOCKED, not SQLITE_BUSY
    with pytest.raises(sqlite3.OperationalError, match="table is locked"):
        D._set_wal(conn)
    assert conn.asked == 1


def test_a_journal_mode_other_than_wal_raises():
    import pytest

    from levain.autonomic import durable_sqlite as D
    for answer in ("delete", "memory", None):
        with pytest.raises(sqlite3.OperationalError, match="WAL"):
            D._set_wal(_Answers(answer))
    D._set_wal(_Answers("WAL"))                    # the answer's case does not matter


def test_open_durable_on_memory_raises_and_closes():
    import pytest

    from levain.autonomic import durable_sqlite as D
    with pytest.raises(sqlite3.OperationalError, match="WAL"):
        D.open_durable(":memory:")


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


# The race the first fix did not reach: SQLite may answer busy with no wait on the statements after the WAL switch too
# (the last other connection closing deletes the WAL under a connection that has not read yet). No process here stages
# that on demand, so the retry is driven through a connection that answers as told.

class _Stmt:
    """A connection whose ``execute`` answers each statement from a script: a list of answers per statement text."""

    def __init__(self, **scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.log = []
        self.closed = False

    def execute(self, sql):
        if sql.startswith("PRAGMA busy_timeout"):        # the retry's own handler switch: a wait of 30 s read back, not a statement under test
            return _Row(30000)
        self.log.append(sql)
        script = self.scripts.get(sql)
        a = script.pop(0) if script else None
        if isinstance(a, BaseException):
            raise a
        return _Row(1)

    def close(self):
        self.closed = True


def test_begin_immediate_answered_busy_with_no_wait_is_retried():
    from levain.autonomic import durable_sqlite as D
    busy = _err("database is locked", 5)
    conn = _Stmt(**{"BEGIN IMMEDIATE": [busy, busy]})
    with D.write_transaction(lambda: conn):
        pass
    assert conn.log == ["BEGIN IMMEDIATE"] * 3 + ["COMMIT"] and conn.closed


def test_begin_immediate_failing_for_another_reason_raises_at_once_and_closes():
    import pytest

    from levain.autonomic import durable_sqlite as D
    conn = _Stmt(**{"BEGIN IMMEDIATE": [_err("disk I/O error", 10)]})
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        with D.write_transaction(lambda: conn):
            raise AssertionError("the block must not run")
    assert conn.log == ["BEGIN IMMEDIATE"] and conn.closed


def test_open_durable_reads_once_after_the_pragmas_and_retries_that_read(monkeypatch):
    from levain.autonomic import durable_sqlite as D
    conn = _Stmt(**{"PRAGMA journal_mode = WAL": ["wal"], "SELECT count(*) FROM sqlite_master": [_err("database is locked", 5)]})
    real = conn.execute

    def execute(sql):
        if sql == "PRAGMA journal_mode = WAL":
            conn.log.append(sql)
            return _Row("wal")
        return real(sql)
    conn.execute = execute
    monkeypatch.setattr(D.sqlite3, "connect", lambda *a, **k: conn)
    assert D.open_durable("x.db") is conn
    assert conn.log.count("SELECT count(*) FROM sqlite_master") == 2 and not conn.closed


def test_a_begin_that_stays_busy_past_the_bound_raises(monkeypatch):
    import pytest

    from levain.autonomic import durable_sqlite as D
    monkeypatch.setattr(D, "BUSY_WAIT_S", 0.2)
    conn = _Stmt(**{"BEGIN IMMEDIATE": [_err("database is locked", 5)] * 10000})
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with D.write_transaction(lambda: conn):
            pass
    assert conn.closed
