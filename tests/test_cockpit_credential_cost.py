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


def _lookups(db: CockpitDB) -> list[str]:
    """Trace the calling thread's held connection: the paired_devices lookups it runs (a list that grows)."""
    seen: list[str] = []
    db._held.get().conn.set_trace_callback(lambda s: seen.append(s) if "FROM paired_devices" in s else None)
    return seen


# --- the cost, measured ---------------------------------------------------------------------------------------------

def test_verify_device_opens_one_connection_for_many_lookups(db: CockpitDB, monkeypatch) -> None:
    """MEASURED before the change: every ``verify_device`` opened ``cockpit.db`` once (N calls, N opens). After: the
    first call opens the held connection, the rest open nothing."""
    _dev, tok = _enrol(db)
    spy = _OpenSpy(monkeypatch)
    for _ in range(N_READS):
        assert db.verify_device(tok, now=NOW) is not None
        assert db.verify_device("x" * 43, now=NOW) is None      # a miss costs no open either
    print(f"MEASURE: {2 * N_READS} verify_device calls -> {len(spy.of('cockpit.db'))} cockpit.db opens")
    assert len(spy.of("cockpit.db")) == 1, spy.opens


# --- the security invariant: a revocation is seen on the very next call ---------------------------------------------

def test_a_revoke_by_another_connection_is_refused_on_the_next_call_cache_warm(db: CockpitDB, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)      # the window can never be what refuses
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    seen = _lookups(db)
    assert db.verify_device(tok, now=NOW) == dev
    assert seen == [], "the second answer came from the cache (an unchanged journal)"
    db.revoke_devices([dev], by="browser", now=NOW)
    assert db.verify_device(tok, now=NOW) is None
    assert len(seen) == 1, "the revoke changed the journal's version: the token was looked up again"


def test_a_revoke_by_another_process_is_refused_on_the_next_call(db: CockpitDB, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    code = ("import sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
            "c.execute(\"UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z' WHERE id = ?\", (sys.argv[2],))\n")
    subprocess.run([sys.executable, "-I", "-c", code, str(db.path), dev], check=True, timeout=60)
    assert db.verify_device(tok, now=NOW) is None


def test_a_cache_cannot_serve_after_any_change_to_the_journal(db: CockpitDB, monkeypatch) -> None:
    """Not only a revoke: any commit by another connection changes the version, and the token is looked up again."""
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    seen = _lookups(db)
    db.mint_offer(label="other", minted_by="browser", now=NOW)      # an unrelated commit
    assert db.verify_device(tok, now=NOW) == dev
    assert len(seen) == 1


def test_a_cached_answer_does_not_outlive_its_window(db: CockpitDB, monkeypatch) -> None:
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    seen = _lookups(db)
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 0.0)
    assert db.verify_device(tok, now=NOW) == dev
    assert len(seen) == 1


def test_a_device_not_found_is_never_remembered(db: CockpitDB, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    junk = "j" * 43
    assert db.verify_device(junk, now=NOW) is None
    seen = _lookups(db)
    for _ in range(3):
        assert db.verify_device(junk, now=NOW) is None
    assert len(seen) == 3


def test_a_re_enrolled_device_old_token_refused_new_token_admitted(db: CockpitDB, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    dev1, tok1 = _enrol(db, "ipad")
    assert db.verify_device(tok1, now=NOW) == dev1
    db.revoke_devices([dev1], by="browser", now=NOW)
    dev2, tok2 = _enrol(db, "ipad")                     # the same label enrolled again: a new device, a new token
    assert dev2 != dev1
    assert db.verify_device(tok1, now=NOW) is None
    assert db.verify_device(tok2, now=NOW) == dev2
    assert db.verify_device(tok1, now=NOW) is None      # and again, with the new device's answer cached


def test_an_error_resolving_the_token_refuses_it(db: CockpitDB, monkeypatch) -> None:
    """Fail closed: a journal that cannot be read raises (the server answers 503), it never answers a cached device."""
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    db._held.get().conn.close()                         # a held connection that is broken
    with pytest.raises(sqlite3.Error):
        db.verify_device(tok, now=NOW)


# --- the held connection ----------------------------------------------------------------------------------------------

def test_the_held_connection_is_read_only(db: CockpitDB) -> None:
    _enrol(db)
    held = db._held.get()
    with pytest.raises(sqlite3.OperationalError):
        held.conn.execute("UPDATE paired_devices SET label = 'x'")


def test_the_held_connection_reopens_when_the_db_file_is_replaced(db: CockpitDB, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    dev, tok = _enrol(db)
    assert db.verify_device(tok, now=NOW) == dev
    first = db._held.get()
    # a replacement file: the same journal with the device revoked, moved over the old one (a new inode)
    fresh = db.path.with_name("replacement.db")
    src = sqlite3.connect(db.path)
    dst = sqlite3.connect(fresh)
    src.backup(dst)
    src.close()
    dst.execute("UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z'")
    dst.commit()
    dst.execute("PRAGMA journal_mode = DELETE")
    dst.close()
    for side in ("-wal", "-shm"):
        Path(str(db.path) + side).unlink(missing_ok=True)
    os.replace(fresh, db.path)
    spy = _OpenSpy(monkeypatch)
    assert db.verify_device(tok, now=NOW) is None, "answered from the replaced file, not the old connection's"
    assert db._held.get() is not first
    assert len(spy.of("cockpit.db")) == 1


def test_the_held_connection_is_closed_when_its_thread_ends(db: CockpitDB) -> None:
    dev, tok = _enrol(db)
    box: dict[str, Any] = {}

    def run() -> None:
        assert db.verify_device(tok, now=NOW) == dev
        box["conn"] = db._held.get().conn

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    gc.collect()
    with pytest.raises(sqlite3.ProgrammingError):
        box["conn"].execute("SELECT 1")


def test_each_thread_holds_its_own_connection_and_a_nested_read_is_not_broken(db: CockpitDB) -> None:
    dev, tok = _enrol(db)
    got: list[Any] = []
    t = threading.Thread(target=lambda: got.append(db._held.get()))
    t.start()
    t.join(10)
    assert got[0] is not db._held.get()
    with db._held.read_transaction() as outer:
        assert outer.execute("SELECT count(*) FROM paired_devices").fetchone()[0] == 1
        with db._held.read_transaction() as inner:          # a read inside a read, one thread: a connection of its own
            assert inner is not outer
            assert inner.execute("SELECT count(*) FROM paired_devices").fetchone()[0] == 1
        assert db.verify_device(tok, now=NOW) == dev


# --- over HTTP --------------------------------------------------------------------------------------------------------

def _manifest(port: int, tok: str, conn: Any = None) -> int:
    return _call(port, "GET", "/cockpit/manifest.json", token=tok, headers=PROXY, conn=conn)[0]


def test_http_a_revoked_device_is_refused_on_the_very_next_request(src, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
    with _serve(src) as (port, httpd):
        dev, tok = _pair(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)    # one keep-alive connection: one server thread
        for _ in range(3):
            assert _manifest(port, tok, conn) == 200                        # warm: held connection and cache populated
        db = httpd.broker.active_db()
        db.revoke_devices([dev], by="browser", now=datetime.now(timezone.utc))
        assert _manifest(port, tok, conn) == 403
        conn.close()


def test_http_a_re_enrolled_device(src, monkeypatch) -> None:
    monkeypatch.setattr(firejournal, "CRED_CACHE_S", 3600.0)
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
