"""levain#157 D15: the per-request credential cost. A paired device's token is resolved on every authenticated request
(``_credential`` -> ``_paired_device`` -> ``CockpitDB.verify_device``); these tests hold what that costs in sqlite opens,
and the security invariant that a faster path must keep: a revoked device is refused on the very next request."""

from __future__ import annotations

import gc
import http.client
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import levain
from levain.cockpit import firejournal
from levain.cockpit.firejournal import CockpitDB
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve

N_READS = 25
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
CHECKOUT = Path(__file__).resolve().parents[1]


def test_these_tests_run_against_this_checkout() -> None:
    assert Path(levain.__file__).resolve().is_relative_to(CHECKOUT), levain.__file__


@pytest.fixture
def src(tmp_path: Path) -> Any:
    return _install(tmp_path)[1]


@pytest.fixture
def db(tmp_path: Path) -> CockpitDB:
    root = tmp_path / "cockpit"
    root.mkdir()
    return CockpitDB(root)


def _enrol(db: CockpitDB, label: str = "ipad") -> tuple[str, str]:
    out = db.redeem_offer(db.mint_offer(label=label, minted_by="browser", now=NOW), now=NOW)
    assert out is not None
    return out


class _OpenSpy:
    """Every ``sqlite3.connect`` made while installed: (database path, thread name)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opens: list[tuple[str, str]] = []
        real = sqlite3.connect

        def spy(database: Any, *a: Any, **kw: Any) -> sqlite3.Connection:
            self.opens.append((str(database), threading.current_thread().name))
            return real(database, *a, **kw)

        monkeypatch.setattr(sqlite3, "connect", spy)

    def of(self, name: str) -> list[tuple[str, str]]:
        return [o for o in self.opens if o[0].endswith(name)]


def _held(db: CockpitDB) -> Any:
    """The calling thread's credential connection (a ``HeldRead``), opened if it is not."""
    return db._cred.get(lambda: db._connect(held=True), db.path)


def _lookups(db: CockpitDB) -> list[str]:
    """Trace the calling thread's credential connection: the paired_devices lookups it runs (a list that grows)."""
    seen: list[str] = []
    _held(db).conn.set_trace_callback(lambda s: seen.append(s) if "FROM paired_devices" in s else None)
    return seen


# --- the cost, measured ---------------------------------------------------------------------------------------------

def test_verify_device_opens_one_connection_for_many_lookups(db: CockpitDB, monkeypatch) -> None:
    """MEASURED before the change: every ``verify_device`` opened ``cockpit.db`` once (N calls, N opens). After: the
    first call that finds a device opens the held connection, the rest open nothing. A miss closes it (L1 W4), so a
    run of misses costs what every call cost before: one open each."""
    _dev, tok = _enrol(db)
    spy = _OpenSpy(monkeypatch)
    for _ in range(N_READS):
        assert db.verify_device(tok, now=NOW) is not None
    hits = len(spy.of("cockpit.db"))
    for _ in range(N_READS):
        assert db.verify_device("x" * 43, now=NOW) is None
    misses = len(spy.of("cockpit.db")) - hits
    print(f"MEASURE: {N_READS} verify_device hits -> {hits} cockpit.db opens; {N_READS} misses -> {misses}")
    assert hits == 1, spy.opens
    assert misses <= N_READS


# --- the security invariant: a revocation is seen on the very next call ---------------------------------------------

def test_a_revoke_by_another_connection_is_refused_on_the_next_call(db: CockpitDB) -> None:
    """Every call looks the token up (nothing is remembered between calls), so a revoke is seen by the next one."""
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    seen = _lookups(db)
    assert db.verify_device(tok, now=NOW) == dev
    db.revoke_devices([dev], by="browser", now=NOW)
    assert db.verify_device(tok, now=NOW) is None
    assert len(seen) == 2, "one lookup per call"


def test_a_revoke_by_another_process_is_refused_on_the_next_call(db: CockpitDB) -> None:
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    code = ("import sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
            "c.execute(\"UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z' WHERE id = ?\", (sys.argv[2],))\n")
    subprocess.run([sys.executable, "-I", "-c", code, str(db.path), dev], check=True, timeout=60)
    assert db.verify_device(tok, now=NOW) is None


def test_a_re_enrolled_device_old_token_refused_new_token_admitted(db: CockpitDB) -> None:
    dev1, tok1 = _enrol(db, "ipad")
    assert db.verify_device(tok1, now=NOW) == dev1
    db.revoke_devices([dev1], by="browser", now=NOW)
    dev2, tok2 = _enrol(db, "ipad")                     # the same label enrolled again: a new device, a new token
    assert dev2 != dev1
    assert db.verify_device(tok1, now=NOW) is None
    assert db.verify_device(tok2, now=NOW) == dev2
    assert db.verify_device(tok1, now=NOW) is None      # and again, after the new device was admitted


def test_an_error_resolving_the_token_refuses_it(db: CockpitDB) -> None:
    """Fail closed: a journal that cannot be read raises (the server answers 503), it never answers a device."""
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    _held(db).conn.close()                              # a held connection that is broken
    with pytest.raises(sqlite3.Error):
        db.verify_device(tok, now=NOW)


# --- the held connection ----------------------------------------------------------------------------------------------

def test_the_held_connection_is_read_only(db: CockpitDB) -> None:
    _enrol(db)
    with pytest.raises(sqlite3.OperationalError):
        _held(db).conn.execute("UPDATE paired_devices SET label = 'x'")


def test_the_held_connection_reopens_when_the_db_file_is_replaced(db: CockpitDB, monkeypatch) -> None:
    dev, tok = _enrol(db)
    dev2, tok2 = _enrol(db, "phone")
    assert db.verify_device(tok, now=NOW) == dev
    # a replacement file: the same journal with the first device revoked, moved over the old one (a new inode)
    fresh = db.path.with_name("replacement.db")
    src = sqlite3.connect(db.path)
    dst = sqlite3.connect(fresh)
    src.backup(dst)
    src.close()
    dst.execute("UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z' WHERE id = ?", (dev,))
    dst.commit()
    dst.execute("PRAGMA journal_mode = DELETE")
    dst.close()
    for side in ("-wal", "-shm"):
        Path(str(db.path) + side).unlink(missing_ok=True)
    os.replace(fresh, db.path)
    spy = _OpenSpy(monkeypatch)
    assert db.verify_device(tok, now=NOW) is None, "answered from the replaced file, not the old connection's"
    # the replacement is not in WAL mode: an open's own switch to WAL writes the file after it was looked at, so the
    # call after it reopens once more; then it settles
    for _ in range(2):
        assert db.verify_device(tok2, now=NOW) == dev2
    settled = len(spy.of("cockpit.db"))
    assert settled <= 3
    for _ in range(3):
        assert db.verify_device(tok2, now=NOW) == dev2
    assert len(spy.of("cockpit.db")) == settled


def test_the_held_connection_is_closed_when_its_thread_ends(db: CockpitDB) -> None:
    dev, tok = _enrol(db)
    box: dict[str, Any] = {}

    def run() -> None:
        assert db.verify_device(tok, now=NOW) == dev
        box["conn"] = _held(db).conn

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    with pytest.raises(sqlite3.ProgrammingError):       # no collector run: the thread's end closed it
        box["conn"].execute("SELECT 1")


def test_each_thread_holds_its_own_connection_and_reads_never_use_it(db: CockpitDB) -> None:
    dev, tok = _enrol(db)
    got: list[Any] = []
    t = threading.Thread(target=lambda: got.append(_held(db)))
    t.start()
    t.join(10)
    assert got[0] is not _held(db)
    db._cred.drop()
    with db.read() as outer:
        assert outer.execute("SELECT count(*) FROM paired_devices").fetchone()[0] == 1
        with db.read() as inner:                        # a read inside a read: each its own connection
            assert inner is not outer
            assert inner.execute("SELECT count(*) FROM paired_devices").fetchone()[0] == 1
    assert len(db.list_devices()) == 1
    assert getattr(db._cred._local, "held", None) is None, "a journal read opened the credential connection"
    assert db.verify_device(tok, now=NOW) == dev


# --- over HTTP --------------------------------------------------------------------------------------------------------

def _manifest(port: int, tok: str, conn: Any = None) -> int:
    return _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=conn)[0]


def test_http_a_revoked_device_is_refused_on_the_very_next_request(src) -> None:
    with _serve(src) as (port, httpd):
        dev, tok = _pair(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)    # one keep-alive connection: one server thread
        for _ in range(3):
            assert _manifest(port, tok, conn) == 200                        # warm: the held connection is open
        db = httpd.broker.active_db()
        db.revoke_devices([dev], by="browser", now=datetime.now(timezone.utc))
        assert _manifest(port, tok, conn) == 403
        conn.close()


def test_http_a_re_enrolled_device(src) -> None:
    with _serve(src) as (port, httpd):
        dev1, tok1 = _pair(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        assert _manifest(port, tok1, conn) == 200
        httpd.broker.active_db().revoke_devices([dev1], by="browser", now=datetime.now(timezone.utc))
        dev2, tok2 = _pair(port, label="ipad")
        assert _manifest(port, tok1, conn) == 403
        assert _manifest(port, tok2, conn) == 200
        assert _manifest(port, tok1, conn) == 403
        conn.close()


# --- the last_seen stamp leaves the request thread (D15b) ---------------------------------------------------------------

def _until(cond: Any, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def _stamp_spy(monkeypatch: pytest.MonkeyPatch, gate: "threading.Event | None" = None) -> list[tuple[str, str]]:
    """Every ``stamp_last_seen`` call: (device, the thread's name). With ``gate``, the call waits for it first."""
    calls: list[tuple[str, str]] = []
    real = CockpitDB.stamp_last_seen

    def spy(self: CockpitDB, device_id: str, *, now: datetime) -> bool:
        calls.append((device_id, threading.current_thread().name))
        if gate is not None:
            gate.wait(30)
        return real(self, device_id, now=now)

    monkeypatch.setattr(CockpitDB, "stamp_last_seen", spy)
    return calls


def test_http_paired_reads_open_the_journal_next_to_never(src, monkeypatch) -> None:
    """MEASURED before D15 (25 paired reads on one keep-alive connection): 50 cockpit.db opens, two per request (the
    credential lookup, the stamp's due-check). After: the opens do not grow with the reads."""
    with _serve(src) as (port, httpd):
        _dev, tok = _pair(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        assert _manifest(port, tok, conn) == 200            # warm: the first request pays the held connection once
        assert _until(lambda: httpd.last_seen_stamper._pending == {})
        spy = _OpenSpy(monkeypatch)
        for _ in range(N_READS):
            assert _manifest(port, tok, conn) == 200
        conn.close()
        journal = spy.of("cockpit.db")
        print(f"MEASURE: {N_READS} paired reads -> {len(journal)} cockpit.db opens, {len(spy.opens)} sqlite opens in all")
        assert len(journal) <= 2, journal       # nothing per request; the stamper thread's own stamp may open


def test_http_many_requests_one_stamp_per_device_and_never_on_the_request_thread(src, monkeypatch) -> None:
    calls = _stamp_spy(monkeypatch)
    with _serve(src) as (port, httpd):
        dev, tok = _pair(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        for _ in range(N_READS):
            assert _manifest(port, tok, conn) == 200
        assert _until(lambda: len(calls) >= 1)
        time.sleep(0.2)
        conn.close()
        assert [d for d, _t in calls] == [dev], "one write for N requests inside the window"
        assert calls[0][1] == "levain-last-seen"
        assert _until(lambda: httpd.broker.active_db().list_devices()[0]["last_seen"] is not None)


def test_http_a_slow_stamp_holds_no_request(src, monkeypatch) -> None:
    gate = threading.Event()
    calls = _stamp_spy(monkeypatch, gate)
    try:
        with _serve(src) as (port, _httpd):
            _dev, tok = _pair(port)
            t0 = time.monotonic()
            for _ in range(5):
                assert _manifest(port, tok) == 200
            assert _until(lambda: len(calls) == 1)       # the thread is inside the stamp, and is not done
            assert time.monotonic() - t0 < 10.0
    finally:
        gate.set()


def test_stamper_coalesces_by_device_and_stamps_again_after_the_window(db: CockpitDB, monkeypatch) -> None:
    import types

    from levain.cockpit import last_seen

    clock = [1000.0]
    monkeypatch.setattr(last_seen, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    calls = _stamp_spy(monkeypatch)
    a, _ = _enrol(db, "a")
    b, _ = _enrol(db, "b")
    st = last_seen.LastSeenStamper()
    window = firejournal.LAST_SEEN_EVERY.total_seconds()
    try:
        assert [st.note(db, a, NOW), st.note(db, a, NOW), st.note(db, b, NOW)] == [True, False, True]
        assert _until(lambda: len(calls) == 2)
        assert sorted(d for d, _t in calls) == sorted([a, b])
        clock[0] += window - 1
        assert st.note(db, a, NOW) is False             # still inside the window
        clock[0] += 2
        assert st.note(db, a, NOW) is True              # the window is over
        assert _until(lambda: len(calls) == 3)
    finally:
        st.close()


def test_a_failing_stamp_is_fail_soft_and_the_thread_goes_on(db: CockpitDB, monkeypatch) -> None:
    from levain.cockpit.last_seen import LastSeenStamper

    calls: list[str] = []
    real = CockpitDB.stamp_last_seen

    def flaky(self: CockpitDB, device_id: str, *, now: datetime) -> bool:
        calls.append(device_id)
        if len(calls) == 1:
            raise RuntimeError("journal gone")
        return real(self, device_id, now=now)

    monkeypatch.setattr(CockpitDB, "stamp_last_seen", flaky)
    a, _ = _enrol(db, "a")
    b, _ = _enrol(db, "b")
    st = LastSeenStamper()
    try:
        assert st.note(db, a, NOW) is True
        assert _until(lambda: calls == [a])
        assert st.note(db, b, NOW) is True
        assert _until(lambda: calls == [a, b])
        assert _until(lambda: {d["id"]: d["last_seen"] for d in db.list_devices()}[b] is not None)
    finally:
        st.close()


def test_closing_the_stamper_does_not_hang_on_a_stamp_in_flight(db: CockpitDB, monkeypatch) -> None:
    from levain.cockpit import last_seen

    gate = threading.Event()
    calls = _stamp_spy(monkeypatch, gate)
    monkeypatch.setattr(last_seen, "_JOIN_S", 0.2)
    a, _ = _enrol(db)
    st = last_seen.LastSeenStamper()
    try:
        assert st.note(db, a, NOW) is True
        assert _until(lambda: len(calls) == 1)
        t0 = time.monotonic()
        st.close()
        assert time.monotonic() - t0 < 5.0
        assert st.note(db, "d-000000000000", NOW) is False      # closed: nothing queues
    finally:
        gate.set()


def test_http_server_close_stops_the_stamper(src) -> None:
    with _serve(src) as (port, httpd):
        _dev, tok = _pair(port)
        assert _manifest(port, tok) == 200
        thread = httpd.last_seen_stamper._thread
        assert thread is not None
    thread.join(5)
    assert not thread.is_alive()


# --- the journal's other reads ----------------------------------------------------------------------------------------

def test_a_journal_read_opens_and_closes_a_connection_of_its_own(db: CockpitDB, monkeypatch) -> None:
    """``read()`` holds nothing past its block (the D15c held read was taken back in the review round): each read opens
    and closes its own connection, and sees what was committed before it began."""
    _enrol(db)
    other = sqlite3.connect(db.path, isolation_level=None)      # a writer that is none of the reader's connections
    conns: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def keep(*a: Any, **kw: Any) -> sqlite3.Connection:
        c = real(*a, **kw)
        conns.append(c)
        return c

    monkeypatch.setattr(sqlite3, "connect", keep)
    try:
        for n in range(N_READS):
            other.execute("UPDATE paired_devices SET label = ?", (f"label-{n}",))
            assert db.list_devices()[0]["label"] == f"label-{n}"
            assert db.meta("format") is not None
    finally:
        other.close()
    print(f"MEASURE: {2 * N_READS} journal reads -> {len(conns)} cockpit.db opens")
    assert conns
    for c in conns:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")


# --- the review round: the credential connection is verify_device's alone ------------------------------------------------

def _revoke_elsewhere(db: CockpitDB, dev: str) -> None:
    """Revoke ``dev`` through a connection that is none of ``db``'s (as the CLI would)."""
    other = sqlite3.connect(db.path, isolation_level=None)
    try:
        other.execute("UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z' WHERE id = ?", (dev,))
    finally:
        other.close()


def test_a_cursor_a_read_caller_leaves_behind_does_not_pin_the_credential_check(db: CockpitDB) -> None:
    """L2 M1: a partly read cursor kept past its ``read()`` block must not hold the snapshot verify_device answers from."""
    dev, tok = _enrol(db)
    _enrol(db, "phone")                                 # two rows: one fetchone leaves the statement unfinished
    assert db.verify_device(tok, now=NOW) == dev
    with db.read() as conn:
        leaked = conn.execute("SELECT id FROM paired_devices")
        leaked.fetchone()
    _revoke_elsewhere(db, dev)
    assert db.verify_device(tok, now=NOW) is None, "a revoked device was admitted"
    del leaked


def test_a_credential_check_inside_an_enclosing_read_sees_a_revoke(db: CockpitDB) -> None:
    """L1 W2: a read open on this thread must not freeze what verify_device sees."""
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    with db.read() as outer:
        outer.execute("SELECT count(*) FROM paired_devices").fetchall()
        _revoke_elsewhere(db, dev)
        assert db.verify_device(tok, now=NOW) is None, "a revoked device was admitted"


def test_a_read_left_open_does_not_freeze_the_credential_check(db: CockpitDB) -> None:
    """L2 L1: a ``read()`` context entered and never left (an abandoned generator) must not freeze verify_device."""
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    abandoned = db.read()
    conn = abandoned.__enter__()
    conn.execute("SELECT count(*) FROM paired_devices").fetchall()
    _revoke_elsewhere(db, dev)
    try:
        assert db.verify_device(tok, now=NOW) is None, "a revoked device was admitted"
    finally:
        abandoned.__exit__(None, None, None)


def test_a_broken_credential_connection_refuses_then_is_replaced(db: CockpitDB, monkeypatch) -> None:
    """L1 W3 / L2 L2: a broken connection refuses (never admits), and the next call opens a new one and answers."""
    conns: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def keep(*a: Any, **kw: Any) -> sqlite3.Connection:
        c = real(*a, **kw)
        conns.append(c)
        return c

    monkeypatch.setattr(sqlite3, "connect", keep)
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    assert conns, "verify_device opened its connection here"
    for c in conns:
        c.close()                                       # every connection this thread holds is now broken
    with pytest.raises(sqlite3.Error):
        db.verify_device(tok, now=NOW)
    assert db.verify_device(tok, now=NOW) == dev, "the broken connection was kept"
    _revoke_elsewhere(db, dev)
    assert db.verify_device(tok, now=NOW) is None


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_journal_objects_made_and_dropped_leave_no_open_file_behind(tmp_path: Path) -> None:
    """L1 W1: a ``CockpitDB`` made per call and dropped closes what it opened, with the cyclic collector off
    (refcounting alone). MEASURED before (one held connection shared by read() and verify_device, reached through
    lambdas that held the CockpitDB): 50 made, read, verified and dropped left 101 file descriptors open."""
    root = tmp_path / "cockpit"
    root.mkdir()
    dev, tok = _enrol(CockpitDB(root))
    gc.collect()
    was = gc.isenabled()
    gc.disable()
    try:
        before = _fds()
        for _ in range(50):
            one = CockpitDB(root)
            assert len(one.list_devices()) == 1
            assert one.verify_device(tok, now=NOW) == dev
            del one
        after = _fds()
    finally:
        if was:
            gc.enable()
    print(f"MEASURE: 50 CockpitDB made, read and verified, dropped -> {after - before} file descriptors still open")
    assert after - before <= 0, after - before


def test_an_in_place_restore_is_seen_by_the_next_credential_check(db: CockpitDB) -> None:
    """L2 M2: a backup copied over ``cockpit.db`` (same inode, new contents) is what the next verify answers from. A
    fresh transaction alone does not see it: SQLite keeps the held connection's cached pages while the WAL index is
    unchanged."""
    dev, tok = _enrol(db)
    other = sqlite3.connect(db.path, isolation_level=None)
    other.execute("PRAGMA wal_checkpoint(TRUNCATE)")    # the journal's newest commits are in the main file
    restored = db.path.with_name("restored.db")
    w = sqlite3.connect(restored, isolation_level=None)
    other.backup(w)
    other.close()
    w.execute("UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z'")
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.close()
    assert db.verify_device(tok, now=NOW) == dev        # the credential connection has read the journal as it was
    ino = os.stat(db.path).st_ino
    with open(restored, "rb") as s, open(db.path, "r+b") as t:
        t.write(s.read())
        t.truncate()
    assert os.stat(db.path).st_ino == ino, "the restore kept the inode"
    assert db.verify_device(tok, now=NOW) is None, "answered from the old contents"


def test_a_token_that_resolves_to_no_device_leaves_no_connection_held(db: CockpitDB) -> None:
    """L1 W4: an unauthenticated token-shaped header must not make its thread hold a journal connection. MEASURED
    before: 8 threads parked after a miss held 15 and 17 file descriptors (two runs)."""
    _enrol(db)
    n = 8
    gate = threading.Event()
    done = threading.Barrier(n + 1)
    answers: list[Any] = []

    def run(i: int) -> None:
        answers.append(db.verify_device(f"{i:043d}", now=NOW))
        done.wait(30)
        gate.wait(30)

    gc.collect()
    before = _fds()
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    try:
        done.wait(30)
        during = _fds()
    finally:
        gate.set()
        for t in threads:
            t.join(10)
    assert answers == [None] * n
    print(f"MEASURE: {n} threads after a miss, parked -> {during - before} file descriptors held")
    assert during - before <= 0, during - before


def test_short_lived_threads_leave_no_open_connection_behind(db: CockpitDB, monkeypatch) -> None:
    _dev, tok = _enrol(db)
    conns: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def keep(*a: Any, **kw: Any) -> sqlite3.Connection:
        c = real(*a, **kw)
        conns.append(c)
        return c

    monkeypatch.setattr(sqlite3, "connect", keep)
    for _ in range(20):
        t = threading.Thread(target=lambda: (db.list_devices(), db.verify_device(tok, now=NOW)))
        t.start()
        t.join(10)
    assert len(conns) == 40, "each thread: one journal read, one credential connection"
    for c in conns:                                     # no collector run: each thread's end closed its own
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
