"""levain#157 D15: the per-request credential cost. A paired device's token is resolved on every authenticated request
(``_credential`` -> ``_paired_device`` -> ``CockpitDB.verify_device``); these tests hold what that costs in sqlite opens,
and the security invariant that a faster path must keep: a revoked device is refused on the very next request."""

from __future__ import annotations

import ast
import contextlib
import gc
import http.client
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import levain
from levain import web_server
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


@contextlib.contextmanager
def _sidecars_kept(db: CockpitDB) -> Iterator[None]:
    """A reader that is none of ``db``'s, open for the block. The last connection to close deletes ``-wal`` and
    ``-shm``, and the held connection's identity includes them: without this reader, closing the held connection by
    hand makes the next call open a new one instead of using the broken one."""
    other = sqlite3.connect(db.path, isolation_level=None)
    try:
        other.execute("SELECT count(*) FROM paired_devices").fetchall()
        yield
    finally:
        other.close()


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
    with _sidecars_kept(db):
        _held(db).conn.close()                          # a held connection that is broken
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


# --- the revocation revision (levain#228): a revoke by ANY process is seen by the very next request ---------------------
# The server keeps a verified credential in memory so a page's reads share one lookup. Every revoke replaces a file beside
# the journal; an entry records that file's identity as it was before its lookup, and a hit needs it unchanged.

def _verify_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ``verify_device`` call made while installed: its token (a list that grows)."""
    calls: list[str] = []
    real = CockpitDB.verify_device

    def spy(self: CockpitDB, token: str, *, now: datetime) -> "str | None":
        calls.append(token)
        return real(self, token, now=now)

    monkeypatch.setattr(CockpitDB, "verify_device", spy)
    return calls


def test_the_revision_file_holds_a_state_and_a_nonce_and_every_clean_write_is_a_new_revision(db: CockpitDB) -> None:
    _enrol(db)
    assert not firejournal.revision_path(db.data_root).exists()
    firejournal.ensure_revision(db.data_root)
    first = firejournal.revision_identity(db.data_root)
    assert first is not None, "a creation is clean"
    firejournal.ensure_revision(db.data_root)
    assert firejournal.revision_identity(db.data_root) == first, "a creation never replaces a file in place"
    path = firejournal.revision_path(db.data_root)
    assert path.name == firejournal.DB_NAME + firejournal.REVISION_SUFFIX and path.parent == db.path.parent
    assert path.stat().st_mode & 0o777 == db.path.stat().st_mode & 0o777, "the journal's permissions"
    assert re.fullmatch(r"clean [0-9a-f]{32}\n", path.read_text()), "a state and random bytes, nothing else"
    seen = [first]
    for _ in range(30):
        firejournal.bump_revision(db.data_root, dirty=True)
        assert re.fullmatch(r"dirty [0-9a-f]{32}\n", path.read_text())
        assert firejournal.revision_identity(db.data_root) is None, "a dirty revision is no revision to trust"
        firejournal.bump_revision(db.data_root, dirty=False)
        seen.append(firejournal.revision_identity(db.data_root))
        assert seen[-1] is not None and seen[-1] not in seen[:-1], "every clean write is a revision never seen before"
    assert sorted(p.name for p in db.data_root.iterdir() if p.name.startswith(".revrev-")) == [], "no temporary left"


def test_the_revision_is_the_files_content_never_its_metadata(db: CockpitDB, monkeypatch) -> None:
    """F2: a replace whose inode, times and size all repeat the old file's (a filesystem may hand them out again) is
    still a new revision, because the nonce the file holds is what is compared. Malformed content is no revision."""
    firejournal.ensure_revision(db.data_root)
    path = firejournal.revision_path(db.data_root)
    first = firejournal.revision_identity(db.data_root)
    first_content = path.read_text()
    frozen = os.stat(path)
    real_stat, real_fstat = os.stat, os.fstat

    def same_metadata(p: Any, *a: Any, **kw: Any) -> Any:
        return frozen if Path(p) == path else real_stat(p, *a, **kw)

    def same_metadata_open(fd: int) -> Any:
        st = real_fstat(fd)
        return frozen if (st.st_dev, st.st_ino) == (frozen.st_dev, frozen.st_ino) else st

    monkeypatch.setattr(os, "stat", same_metadata)
    monkeypatch.setattr(os, "fstat", same_metadata_open)
    path.write_text(f"clean {'0' * 32}\n")              # in place: the inode is the old one's
    assert firejournal.revision_identity(db.data_root) not in (None, first)
    monkeypatch.undo()
    path.write_text(first_content)                      # the old content written back is still a new revision
    assert firejournal.revision_identity(db.data_root) not in (None, first)
    for junk in ("", "clean\n", f"clean {'0' * 31}\n", f"{'0' * 32} 12.5\n", f"stale {'0' * 32}\n",
                 f"clean {'0' * 32}\n" + "x" * 200):
        path.write_text(junk)
        assert firejournal.revision_identity(db.data_root) is None, junk


def test_a_revision_creation_whose_temporary_cannot_be_removed_still_reports_success(db: CockpitDB,
                                                                                    monkeypatch) -> None:
    """F5: once the link has put the new file in place, a temporary that cannot be removed is no failed creation."""
    real_unlink = os.unlink

    def busy(p: Any, *a: Any, **kw: Any) -> None:
        if os.path.basename(str(p)).startswith(".revrev-"):
            raise OSError(16, "Resource busy")
        real_unlink(p, *a, **kw)

    monkeypatch.setattr(os, "unlink", busy)
    firejournal.ensure_revision(db.data_root)
    monkeypatch.undo()
    assert firejournal.revision_identity(db.data_root) is not None


def test_every_writer_of_revoked_at_moves_the_revision_around_its_commit() -> None:
    """``revoked_at`` is set in one statement, in ``_revoke_members``; every caller of that helper opens its transaction
    with ``_revoking_write``, the one that bumps the revision before and after the commit. A new writer fails here."""
    tree_root = CHECKOUT / "levain"
    writer = re.compile(r"UPDATE\s+paired_devices\s+SET\s+[^\"']*?\brevoked_at\s*=|REPLACE\s+INTO\s+paired_devices", re.I)
    hits = sorted(p.relative_to(tree_root).as_posix() for p in tree_root.rglob("*.py") if writer.search(p.read_text()))
    assert hits == ["cockpit/firejournal.py"], hits
    source = (tree_root / "cockpit" / "firejournal.py").read_text()
    tree = ast.parse(source)
    members = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_revoke_members")
    assert len(writer.findall(ast.get_source_segment(source, members))) == 1
    assert len(writer.findall(source)) == 1, "no other statement of the journal sets revoked_at"
    callers: list[str] = []

    def walk(node: ast.AST, inside: bool, fn: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.With):
            inside = inside or any(isinstance(i.context_expr, ast.Call)
                                   and getattr(i.context_expr.func, "attr", "") == "_revoking_write" for i in node.items)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_revoke_members":
            callers.append(f"{fn}:{inside}")
        for child in ast.iter_child_nodes(node):
            walk(child, inside, fn)

    walk(tree, False, "")
    assert sorted(callers) == ["revoke_confirmed:True", "revoke_confirmed:True", "revoke_devices:True"], callers


def test_a_bump_that_cannot_be_written_makes_the_revoke_fail_and_change_nothing(db: CockpitDB, monkeypatch) -> None:
    dev, tok = _enrol(db)

    def disk_full(*_a: Any, **_kw: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(firejournal, "bump_revision", disk_full)
    with pytest.raises(OSError):
        db.revoke_devices([dev], by="browser", now=NOW)
    assert db.verify_device(tok, now=NOW) == dev, "the revoke was rolled back: the operator retries it"
    assert [d["revoked_at"] for d in db.list_devices()] == [None]


def test_a_second_write_that_cannot_be_written_leaves_the_revision_dirty_and_the_revoke_stands(
        db: CockpitDB, monkeypatch, caplog) -> None:
    """F1: the revoke is durable once committed, and the marker its first write left stays dirty, so no process answers
    from memory (fail closed: every lookup is full) until a later write under the journal's write lock makes it clean."""
    dev, tok = _enrol(db)
    firejournal.ensure_revision(db.data_root)
    real = firejournal.bump_revision
    calls: list[int] = []

    def only_the_first(*a: Any, **kw: Any) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        real(*a, **kw)

    monkeypatch.setattr(firejournal, "bump_revision", only_the_first)
    monkeypatch.setattr(firejournal, "_REVISION_CLEAN_PAUSE_S", 0.0, raising=False)
    with caplog.at_level("ERROR", logger=firejournal.__name__):
        assert db.revoke_devices([dev], by="browser", now=NOW) == [dev]
    assert db.verify_device(tok, now=NOW) is None, "the revoke committed"
    assert firejournal.revision_identity(db.data_root) is None, "the marker stayed dirty: no answer from memory"
    assert len(calls) == 1 + firejournal._REVISION_CLEAN_TRIES, "the second write was retried, then given up"
    assert [r.levelname for r in caplog.records if "revocation revision" in r.getMessage()] == ["ERROR"]
    monkeypatch.setattr(firejournal, "bump_revision", real)
    assert db.settle_revision() is True
    assert firejournal.revision_identity(db.data_root) is not None


def test_http_a_lookup_between_the_first_write_and_the_commit_stores_nothing(src, monkeypatch) -> None:
    with _serve(src) as (port, httpd):
        dev, tok = _pair(port)
        assert _manifest(port, tok) == 200
        real = firejournal.bump_revision
        bumps: list[int] = []
        between: list[int] = []

        def bump(*a: Any, **kw: Any) -> None:
            real(*a, **kw)
            bumps.append(1)
            if len(bumps) == 1:
                # the revoke's transaction is open and its dirty marker is in place: this request reads the device
                # live, and stores no answer under a dirty marker
                between.append(_manifest(port, tok))

        monkeypatch.setattr(firejournal, "bump_revision", bump)
        httpd.broker.active_db().revoke_devices([dev], by="browser", now=datetime.now(timezone.utc))
        assert between == [200] and len(bumps) == 2
        assert _manifest(port, tok) == 403


def test_http_a_second_write_that_fails_leaves_no_answer_that_outlives_the_revoke(src, monkeypatch) -> None:
    """F1, fail closed: a lookup between the first write and the commit reads the device live; the second write then
    fails, every retry of it too, so the marker stays dirty and the very next request looks the token up: refused."""
    with _serve(src) as (port, httpd):
        dev, tok = _pair(port)
        real = firejournal.bump_revision
        bumps: list[int] = []
        between: list[int] = []

        def first_only(*a: Any, **kw: Any) -> None:
            bumps.append(1)
            if len(bumps) > 1:
                raise OSError(28, "No space left on device")
            real(*a, **kw)
            between.append(_manifest(port, tok))

        monkeypatch.setattr(firejournal, "bump_revision", first_only)
        monkeypatch.setattr(firejournal, "_REVISION_CLEAN_PAUSE_S", 0.0, raising=False)
        httpd.broker.active_db().revoke_devices([dev], by="browser", now=datetime.now(timezone.utc))
        assert between == [200]
        assert _manifest(port, tok) == 403, "a revoked device is refused on the very next request"


def test_a_stuck_dirty_revision_is_made_clean_only_under_the_journals_write_lock(db: CockpitDB) -> None:
    """A dirty marker is written inside its revoke's transaction, so whoever holds the write lock and still sees one
    knows that revoke has committed or rolled back: making it clean then lets no lookup outlive a revoke. While a
    transaction holds the lock the marker may be that revoke's own, and it is left dirty."""
    _enrol(db)
    firejournal.ensure_revision(db.data_root)
    firejournal.bump_revision(db.data_root, dirty=True)     # a revoke that ended without its second write
    holder = sqlite3.connect(db.path, isolation_level=None, timeout=0)
    holder.execute("BEGIN IMMEDIATE")                         # a revoke in progress holds the write lock
    try:
        assert db.settle_revision() is False
        assert firejournal.revision_identity(db.data_root) is None, "left dirty while the lock is held"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert db.settle_revision() is True
    assert firejournal.revision_identity(db.data_root) is not None
    path = firejournal.revision_path(db.data_root)
    path.write_text(f"{'0' * 32} 12.5\n")                     # malformed (an earlier build's format): settled the same way
    assert firejournal.revision_identity(db.data_root) is None
    assert db.settle_revision() is True
    assert firejournal.revision_identity(db.data_root) is not None
    clean = path.read_text()
    assert db.settle_revision() is True and path.read_text() == clean, "a clean revision is left as it is"


def test_every_write_of_the_revision_by_a_revoke_holds_the_journals_write_lock(db: CockpitDB, monkeypatch) -> None:
    """Two overlapping revokes: no write of the marker can land between another revoke's dirty marker and its commit,
    because every write, the clean one after the commit included, is made under the journal's write lock."""
    dev, _tok = _enrol(db)
    firejournal.ensure_revision(db.data_root)
    real = firejournal.bump_revision
    held: list[bool] = []

    def probe(*a: Any, **kw: Any) -> None:
        other = sqlite3.connect(db.path, isolation_level=None, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            held.append(False)
        except sqlite3.OperationalError:
            held.append(True)
        finally:
            other.close()
        real(*a, **kw)

    monkeypatch.setattr(firejournal, "bump_revision", probe)
    assert db.revoke_devices([dev], by="browser", now=NOW) == [dev]
    assert held == [True, True], held
    assert firejournal.revision_identity(db.data_root) is not None, "the revoke ended clean"


def test_http_a_stuck_dirty_revision_is_cleared_by_the_next_lookup_and_the_cache_resumes(src, monkeypatch) -> None:
    with _serve(src) as (port, httpd):
        _dev, tok = _pair(port)
        calls = _verify_spy(monkeypatch)
        firejournal.bump_revision(httpd.broker.data_root, dirty=True)
        assert _manifest(port, tok) == 200 and len(calls) == 1, "dirty: a full lookup, never an entry"
        assert firejournal.revision_identity(httpd.broker.data_root) is not None, "that lookup made it clean"
        assert _manifest(port, tok) == 200 and len(calls) == 2, "the next lookup stores an entry under the clean one"
        assert _manifest(port, tok) == 200 and len(calls) == 2, "and the request after it is served from it"


def test_http_an_unreadable_revision_file_turns_the_cache_off_and_every_request_looks_up(src, monkeypatch) -> None:
    with _serve(src) as (port, httpd):
        dev, tok = _pair(port)
        calls = _verify_spy(monkeypatch)
        assert [_manifest(port, tok) for _ in range(3)] == [200] * 3
        assert len(calls) == 1, "warm: served from the entry"

        def denied(_root: Any) -> Any:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(web_server, "revision_identity", denied)
        assert [_manifest(port, tok) for _ in range(3)] == [200] * 3
        assert len(calls) == 4, "each request took the full lookup: no admission from the cache"
        httpd.broker.active_db().revoke_devices([dev], by="browser", now=datetime.now(timezone.utc))
        assert _manifest(port, tok) == 403, "and a revoked device is refused by the full path"


def test_http_a_missing_revision_file_is_made_again_and_the_cache_resumes(src, monkeypatch) -> None:
    with _serve(src) as (port, httpd):
        _dev, tok = _pair(port)
        calls = _verify_spy(monkeypatch)
        assert _manifest(port, tok) == 200 and len(calls) == 1
        firejournal.revision_path(httpd.broker.data_root).unlink()
        assert _manifest(port, tok) == 200 and len(calls) == 2, "no file: a full lookup, never the entry"
        assert firejournal.revision_path(httpd.broker.data_root).exists()
        assert _manifest(port, tok) == 200 and len(calls) == 3, "the next lookup stores an entry under the new file"
        assert _manifest(port, tok) == 200 and len(calls) == 3, "and the request after it is served from it"


def test_http_a_server_started_over_a_journal_without_the_file_makes_it(src) -> None:
    with _serve(src) as (port, httpd):
        _pair(port)
        root = httpd.broker.data_root
    firejournal.revision_path(root).unlink()
    with _serve(src) as (_port, httpd):
        assert firejournal.revision_path(httpd.broker.data_root).exists(), "made at start, before any request"


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
    stamper: "threading.Thread | None" = None
    try:
        with _serve(src) as (port, httpd):
            _dev, tok = _pair(port)
            t0 = time.monotonic()
            for _ in range(5):
                assert _manifest(port, tok) == 200
            assert _until(lambda: len(calls) == 1)       # the thread is inside the stamp, and is not done
            stamper = httpd.last_seen_stamper._thread
            assert time.monotonic() - t0 < 10.0
    finally:
        gate.set()
        if stamper is not None:
            stamper.join(10)


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
        if st._thread is not None:
            st._thread.join(10)


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
    broken = list(conns)                                # before the reader below, which ``keep`` records too
    with _sidecars_kept(db):
        for c in broken:
            c.close()                                  # every connection this thread holds is now broken
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
    was = gc.isenabled()
    gc.disable()
    try:
        gc.collect()        # an explicit run (the collector is off); none before ``after``: refcounting alone is tested
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

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    gc.collect()
    before = _fds()
    for t in threads:
        t.start()
    try:
        done.wait(30)
        gc.collect()
        during = _fds()
    finally:
        gate.set()
        for t in threads:
            t.join(10)
    assert answers == [None] * n
    print(f"MEASURE: {n} threads after a miss, parked -> {during - before} file descriptors held")
    assert during - before <= 0, during - before


def test_the_stamper_thread_keeps_no_journal_alive_between_stamps(tmp_path: Path) -> None:
    """L1 N3: once a stamp is written, the stamper thread holds no reference to the journal it wrote through."""
    import weakref

    from levain.cockpit.last_seen import LastSeenStamper

    root = tmp_path / "cockpit"
    root.mkdir()
    db = CockpitDB(root)                                # not a fixture: nothing but this test refers to it
    dev, _tok = _enrol(db)
    st = LastSeenStamper()
    try:
        assert st.note(db, dev, NOW) is True
        assert _until(lambda: db.list_devices()[0]["last_seen"] is not None)
        assert _until(lambda: st._pending == {} and not st._wake.is_set())
        ref = weakref.ref(db)
        del db
        assert _until(lambda: (gc.collect(), ref() is None)[1]), "the stamper thread still holds the journal"
    finally:
        st.close()


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


# --- the L3 r1 round: the held connection's identity, its process-wide cap, the stamper's queue bound ------------------

def test_a_same_size_in_place_restore_with_its_mtime_put_back_is_seen(db: CockpitDB) -> None:
    """L3 r1 (1a): a restore over ``cockpit.db`` that keeps its inode and size and puts its modification time back
    (``os.utime``) is still seen: the inode's change time moves, and it cannot be set from userland."""
    dev, tok = _enrol(db)
    other = sqlite3.connect(db.path, isolation_level=None)
    other.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    restored = db.path.with_name("restored.db")
    w = sqlite3.connect(restored, isolation_level=None)
    other.backup(w)
    other.close()
    w.execute("UPDATE paired_devices SET revoked_at = '2026-11-19T12:00:01Z'")
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.execute("PRAGMA journal_mode = DELETE")
    w.close()
    assert db.verify_device(tok, now=NOW) == dev
    was = os.stat(db.path)
    assert os.stat(restored).st_size == was.st_size, "the restore is the same size"
    probe = db.path.with_name("tick-probe")             # a coarse filesystem clock (ext4: up to several ms per tick):
    deadline = time.monotonic() + 5                     # wait for a tick past the original ctime, so the rewrite's
    while True:                                         # ctime cannot land in the same tick and read as unchanged
        probe.write_bytes(b"x")
        if os.stat(probe).st_ctime_ns > was.st_ctime_ns:
            break
        assert time.monotonic() < deadline, "the filesystem clock did not tick past the database's ctime"
        time.sleep(0.005)
    probe.unlink()
    with open(restored, "rb") as s, open(db.path, "r+b") as t:
        t.write(s.read())
        t.truncate()
    os.utime(db.path, ns=(was.st_atime_ns, was.st_mtime_ns))
    now = os.stat(db.path)
    assert (now.st_ino, now.st_size, now.st_mtime_ns) == (was.st_ino, was.st_size, was.st_mtime_ns)
    assert now.st_ctime_ns != was.st_ctime_ns, "ctime did not move: the rewrite landed in the original's clock tick"
    assert db.verify_device(tok, now=NOW) is None, "answered from the old contents"


def test_a_replaced_wal_file_reopens_the_held_connection(db: CockpitDB, monkeypatch) -> None:
    """L3 r1 (1b): a ``-wal`` sidecar with a new inode under an unchanged ``cockpit.db`` reopens the held connection."""
    dev, tok = _enrol(db)
    for _ in range(2):
        assert db.verify_device(tok, now=NOW) == dev
    wal = Path(str(db.path) + "-wal")
    assert wal.exists()
    spy = _OpenSpy(monkeypatch)
    assert db.verify_device(tok, now=NOW) == dev
    assert spy.of("cockpit.db") == [], "settled: a hit opens nothing"
    copy = wal.with_name("copy-wal")
    copy.write_bytes(wal.read_bytes())
    os.replace(copy, wal)
    assert db.verify_device(tok, now=NOW) == dev
    assert len(spy.of("cockpit.db")) == 1, "the held connection was kept across a replaced -wal"


def test_the_first_hit_after_a_cold_journal_settles_in_one_open(db: CockpitDB, monkeypatch) -> None:
    """The open itself makes the ``-wal`` and ``-shm`` sidecars of a journal no connection had open; that is not a
    replacement, so the next call keeps the connection."""
    dev, tok = _enrol(db)
    assert not Path(str(db.path) + "-wal").exists(), "precondition: no connection holds the journal open"
    spy = _OpenSpy(monkeypatch)
    for _ in range(5):
        assert db.verify_device(tok, now=NOW) == dev
    assert len(spy.of("cockpit.db")) == 1, spy.opens


def test_held_connections_are_capped_process_wide_and_over_the_cap_answer_the_same(db: CockpitDB,
                                                                                   monkeypatch) -> None:
    """L3 r1 (2): more client threads than the cap each hold at most a cap's worth of connections in all; a thread over
    the cap answers on a connection it opens and closes, with the same verdict, and a revoke is refused on both."""
    from levain.autonomic import durable_sqlite

    free, n = 3, 7
    gc.collect()
    base = getattr(getattr(durable_sqlite, "_SLOTS", None), "taken", 0)     # slots still held by other tests' journals
    monkeypatch.setattr(durable_sqlite, "HELD_READS_MAX", base + free, raising=False)
    cap = free
    dev, tok = _enrol(db)
    conns: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def keep(*a: Any, **kw: Any) -> sqlite3.Connection:
        c = real(*a, **kw)
        conns.append(c)
        return c

    monkeypatch.setattr(sqlite3, "connect", keep)

    def is_open(c: sqlite3.Connection) -> bool:
        try:
            c.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return False
        return True

    first: dict[int, Any] = {}
    second: dict[int, Any] = {}
    held: dict[int, bool] = {}
    parked = threading.Barrier(n + 1)
    turn = [threading.Event() for _ in range(n)]       # phase 1: the driver lets thread i take its second answer
    release = [threading.Event() for _ in range(n)]    # phase 2: only the driver's finally lets thread i end
    done = [threading.Event() for _ in range(n)]

    def run(i: int) -> None:
        first[i] = db.verify_device(tok, now=NOW)
        held[i] = getattr(db._cred._local, "held", None) is not None
        parked.wait(30)
        turn[i].wait(30)
        second[i] = db.verify_device(tok, now=NOW)
        done[i].set()
        release[i].wait(30)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    try:
        parked.wait(30)
        gc.collect()
        open_now = sum(is_open(c) for c in conns)
        assert first == {i: dev for i in range(n)}, "every thread admitted the live device"
        assert sum(held.values()) == cap, held
        assert open_now <= cap, f"{open_now} connections held open by {n} parked threads, cap {cap}"
        _revoke_elsewhere(db, dev)
        order = [i for i in range(n) if not held[i]] + [i for i in range(n) if held[i]]
        for i in order:                                 # the over-cap threads first: every slot is still taken
            turn[i].set()
            assert done[i].wait(30)
    finally:
        for e in turn + release:
            e.set()
        for t in threads:
            t.join(10)
    assert second == {i: None for i in range(n)}, "a revoked device was admitted"
    assert durable_sqlite._SLOTS.taken == base, "a slot outlived its connection"


def test_the_stamper_queue_is_bounded_while_the_thread_is_stuck(monkeypatch) -> None:
    """L3 r1 (3): ``_pending`` never grows past ``_MAX_DEVICES``, however many devices note while a stamp is stuck."""
    import types

    from levain.cockpit import last_seen

    cap = 4
    monkeypatch.setattr(last_seen, "_MAX_DEVICES", cap)
    clock = [1000.0]
    monkeypatch.setattr(last_seen, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    gate = threading.Event()
    entered = threading.Event()

    class Stuck:
        def stamp_last_seen(self, device_id: str, *, now: datetime) -> bool:
            entered.set()
            gate.wait(30)
            return True

    st = last_seen.LastSeenStamper()
    window = firejournal.LAST_SEEN_EVERY.total_seconds()
    peak = 0
    try:
        assert st.note(Stuck(), "d-first", NOW) is True
        assert entered.wait(10)
        for k in range(cap * 5):
            clock[0] += window + 1                      # every earlier device is out of its window
            st.note(Stuck(), f"d-{k:04d}", NOW)
            peak = max(peak, len(st._pending))
    finally:
        gate.set()
        thread = st._thread
        st.close()
        if thread is not None:
            thread.join(10)
    assert peak <= cap, peak
