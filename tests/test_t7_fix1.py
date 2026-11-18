"""T7 fix 1 (levain#101), from the L1 + L2 reviews: the live behaviour the retirement left untested (restored here, adapted to
the v1_seed seeding), the drain's once-only and order guarantees, the claim's checks, the board block's store, and the dead
code's removal. Scratch stores and homes only; no real ``claude``."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from levain.cli import main
from levain.desk import board_cli, launch, lease, ready, seam, work_attach
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError, CoordStore
from tests import v1_seed


def _plant(directory, **kw):
    """A report queued BEFORE the switch, planted around the (now refusing) door."""
    conn = sqlite3.connect(work_attach.reports_path(directory))
    try:
        for stmt in work_attach.REPORT_SCHEMA:
            conn.execute(stmt)
        row = {"report_id": "r-old", "item_id": "levain:a", "worker": "w", "worker_kind": "seat", "event": "progress",
               "at": "2026-11-16T12:00:00Z", **kw}
        conn.execute(f"INSERT INTO reports ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        conn.commit()
    finally:
        conn.close()


def _files(directory: Path) -> dict[str, bytes]:
    wal = directory / "dispatch.db-wal"
    return {"dispatch.db": (directory / "dispatch.db").read_bytes(),
            "dispatch.db-wal": wal.read_bytes() if wal.exists() else b""}


@pytest.fixture
def board(tmp_path):
    d = tmp_path / "board"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """A Levain home whose live consent store exists (a desk store with a lease taken and released)."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    consent_dir = work_attach.live_consent_dir()
    consent = CoordStore(consent_dir)
    token = consent.coord_take()
    consent.coord_release(token, by=ROLE_DESK)
    return home, consent_dir, consent


# ------------------------------------------------------------------------------------------------ W1 (restored)
# the live-consent-store guard on the drain: the pass now WRITES (``reports.drained``), so a regression would put a file
# in the live consent store, the thing the guard exists to prevent
def test_r3a_a_queue_planted_in_the_live_consent_store_is_never_drained_by_the_default_watch(live_home):
    home, consent_dir, consent = live_home
    _plant(consent_dir, report_id="r-planted")
    before = _files(consent_dir)
    lines = launch.watch_once(consent)
    assert _files(consent_dir) == before, lines
    assert not work_attach.drained_path(consent_dir).exists() and not work_attach.drained_log_path(consent_dir).exists()
    assert not (home / "board").exists()                    # the home's board was not made either


def test_r3a_an_explicit_queue_dir_naming_the_live_consent_store_is_not_a_board(live_home):
    home, consent_dir, _consent = live_home
    _plant(consent_dir, report_id="r-planted")
    lines = launch._apply_board_reports(CoordStore(Path(str(consent_dir) + "/.")))
    assert lines == [] and not work_attach.drained_path(consent_dir).exists()
    assert work_attach.watch_board(consent_dir) == home.resolve() / "board"


def test_r3a_watch_apply_refuses_the_live_consent_store_as_its_board(live_home):
    _home, consent_dir, _consent = live_home
    _plant(consent_dir, report_id="r-planted")
    before = _files(consent_dir)
    lines = work_attach.watch_apply(consent_dir)
    assert _files(consent_dir) == before
    assert len(lines) == 1 and "live consent store" in lines[0], lines
    assert not work_attach.drained_path(consent_dir).exists()


def test_r3a_the_default_watch_still_drains_the_homes_board_once(live_home):
    home, _consent_dir, consent = live_home
    board = home / "board"
    board.mkdir(mode=0o700)
    _plant(board, report_id="r-1")
    lines = launch.watch_once(consent)
    assert any("drained" in line and "r-1" in line for line in lines), lines
    assert work_attach.drained_path(board).read_text() == "r-1\n"
    assert not any("r-1" in line for line in launch.watch_once(consent))           # once


def test_r3a_a_watch_over_another_directory_drains_only_that_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    sibling = tmp_path / "scratch" / "board"
    sibling.mkdir(parents=True, mode=0o700)
    _plant(sibling, report_id="r-1")
    scratch = CoordStore(tmp_path / "scratch" / "consent")
    scratch.coord_status()
    launch.watch_once(scratch)
    assert not work_attach.drained_path(sibling).exists()
    assert work_attach.watch_board(tmp_path / "scratch" / "consent") == (tmp_path / "scratch" / "consent").resolve()


def test_the_live_consent_store_is_never_a_board_to_the_desk_pass(live_home):
    _home, consent_dir, consent = live_home
    token = consent.coord_take()
    _plant(consent_dir, report_id="r-planted")
    lines = work_attach.desk_apply(consent, token)
    assert len(lines) == 1 and "live consent store" in lines[0]
    assert not work_attach.drained_path(consent_dir).exists()


def test_the_verb_exits_2_on_a_fault_and_on_the_live_consent_store(tmp_path, live_home, monkeypatch, capsys):
    _home, consent_dir, consent = live_home
    board = tmp_path / "scratch-board"
    board.mkdir(mode=0o700)
    store = CoordStore(board)
    token = store.coord_take()

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    with monkeypatch.context() as m:
        m.setattr(work_attach, "desk_apply", boom)
        code = main(["coord", "apply-reports", "--token", str(token), "--queue-dir", str(board)])
    err = capsys.readouterr().err
    assert code == 2 and err.count("\n") == 1 and "disk on fire" in err and "Traceback" not in err
    t = consent.coord_take()
    _plant(consent_dir, report_id="r-planted")
    code = main(["coord", "apply-reports", "--token", str(t), "--queue-dir", str(consent_dir)])
    assert code == 2 and "live consent store" in capsys.readouterr().err
    assert not work_attach.drained_path(consent_dir).exists()


def test_the_verb_names_the_live_consent_store_before_it_looks_for_a_lease(live_home, capsys):
    """The consent-store check comes before the lease check (L3 r2 codex LOW)."""
    _home, consent_dir, _consent = live_home
    code = main(["coord", "apply-reports", "--token", "999999", "--queue-dir", str(consent_dir)])
    assert code == 2 and "live consent store" in capsys.readouterr().err


def test_the_verb_with_a_wrong_token_on_a_scratch_board_exits_1_and_drains_nothing(tmp_path, capsys):
    board = tmp_path / "scratch-board"
    board.mkdir(mode=0o700)
    store = CoordStore(board)
    token = store.coord_take()
    _plant(board)
    code = main(["coord", "apply-reports", "--token", str(token + 5), "--queue-dir", str(board)])
    assert code == 1 and "not held" in capsys.readouterr().err
    assert not work_attach.drained_path(board).exists()


# ------------------------------------------------------------------------------------------------ W2 (restored)
def test_item_facts_round_trip_and_an_old_store_migrates(tmp_path, monkeypatch):
    """A store made before ``ready_items`` gained its fact columns is migrated by the first writer open and keeps its rows."""
    old_dir = tmp_path / "old"
    old_dir.mkdir(mode=0o700)
    with monkeypatch.context() as m:
        m.setattr(ready, "LIFECYCLE_SCHEMA", tuple(s.replace(ready.ITEM_FACTS_SQL, "") for s in ready.LIFECYCLE_SCHEMA
                                                   if "service_facts" not in s))
        m.setattr(work_attach, "WORK_SCHEMA", ())
        m.setattr(ready, "migrate_item_facts", lambda conn: None)       # the code before the migration existed
        old = CoordStore(old_dir)
        token = old.coord_take()
    c = sqlite3.connect(old_dir / "dispatch.db")                    # the one row the old writer made, as it wrote it
    try:
        g = c.execute("INSERT INTO gseq (at) VALUES ('x')").lastrowid
        c.execute("INSERT INTO ready_journal (id, ver, gseq, ts, iso, at, repo, from_status, to_status, from_category, "
                  "to_category, who, why, source, applied_by, lease_token) VALUES ('levain:old', 1, ?, 0, 'x', 'x', "
                  "'levain', NULL, 'backlog', NULL, 'backlog', 'desk', 'seed', 'add', NULL, ?)", (g, token))
        c.execute("INSERT INTO ready_items (id, ver, repo, title, status, category, tier, done_line, depends_on, rank, "
                  "set_by, set_at) VALUES ('levain:old', 1, 'levain', 'made before the columns', 'backlog', 'backlog', "
                  "NULL, NULL, '[]', 1, 'desk', 'x')")
        c.commit()
        assert "hold" not in {r[1] for r in c.execute("PRAGMA table_info(ready_items)")}
    finally:
        c.close()
    store = CoordStore(old_dir)                                   # the new code opens it
    store.coord_take()                                            # a writer open: _ensure runs migrate_item_facts
    assert ready.item_facts(store, "levain:old") == dict.fromkeys(ready.FACT_COLS)
    c = sqlite3.connect(old_dir / "dispatch.db")
    try:
        assert set(ready.FACT_COLS) <= {r[1] for r in c.execute("PRAGMA table_info(ready_items)")}
        assert c.execute("SELECT title FROM ready_items WHERE id = 'levain:old'").fetchone() == ("made before the columns",)
    finally:
        c.close()


# ------------------------------------------------------------------------------------------------ W7 (restored)
def test_x6_watch_once_passes_its_every_and_every_is_checked_in_both_modes(tmp_path, monkeypatch, capsys):
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    CoordStore(qdir).coord_status()
    seen = []
    monkeypatch.setattr(launch, "migrate_setting", lambda store: [])
    monkeypatch.setattr(launch, "watch_once", lambda store, **kw: seen.append(kw) or [])
    assert main(["coord", "watch", "--once", "--every", "300", "--queue-dir", str(qdir)]) == 0
    assert seen == [{"every": 300.0}]
    assert main(["coord", "watch", "--once", "--every", "0", "--queue-dir", str(qdir)]) != 0
    assert len(seen) == 1 and "--every" in capsys.readouterr().err


def test_a_pass_drains_only_the_reports_not_yet_applied_or_refused(desk_board):
    """``_unresolved``'s filter: a report already applied or refused before the switch is never drained at it."""
    store, token = desk_board
    d = store.directory
    v1_seed.add(store, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    for rid in ("r-applied", "r-refused", "r-new"):
        _plant(d, report_id=rid)
    c = sqlite3.connect(store.path)
    try:
        g1, g2 = (c.execute("INSERT INTO gseq (at) VALUES ('x')").lastrowid for _ in range(2))
        c.execute("INSERT INTO worker_attach (gseq, attach_id, item_id, item_ver, kind, worker, worker_kind, report_id, at, "
                  "applied_at, lease_token, by) VALUES (?, 'a-1', 'levain:a', 1, 'attach', 'w', 'seat', 'r-applied', 'x', "
                  f"'x', 1, '{ROLE_DESK}')", (g1,))
        c.execute("INSERT INTO report_refusals (gseq, report_id, item_id, worker, reason, detail, at, by) VALUES "
                  f"(?, 'r-refused', 'levain:a', 'w', 'unknown-item', 'x', 'x', '{ROLE_DESK}')", (g2,))
        c.commit()
    finally:
        c.close()
    lines = work_attach.watch_apply(d)
    assert len(lines) == 1 and "r-new" in lines[0] and "r-applied" not in lines[0] and "r-refused" not in lines[0]
    assert work_attach.drained_path(d).read_text() == "r-new\n"


@pytest.fixture
def desk_board(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


def test_a_watch_over_a_scratch_store_never_opens_the_real_homes_board(tmp_path, monkeypatch):
    real = tmp_path / "real-home"
    monkeypatch.setenv("LEVAIN_HOME", str(real))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    board = real / "board"
    board.mkdir(parents=True, mode=0o700)
    _plant(board, report_id="r-1")
    scratch = CoordStore(tmp_path / "scratch" / "consent")
    scratch.coord_status()
    launch.watch_once(scratch)
    assert not work_attach.drained_path(board).exists()
    assert not (tmp_path / "scratch" / "board").exists()


def test_watch_once_drains_beside_its_own_work_and_makes_no_board_when_nothing_is_queued(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    consent = CoordStore(home / "consent")
    consent.coord_status()
    before = launch.watch_once(consent)
    assert not (home / "board").exists()                    # nothing queued: no board store is made or opened
    board = home / "board"
    board.mkdir(mode=0o700)
    _plant(board, report_id="r-1")
    after = launch.watch_once(consent)
    assert after[:len(before)] == before and any("drained" in line for line in after[len(before):])


def test_watch_apply_never_raises(board, monkeypatch):
    _plant(board)

    def blows_up(_d):
        raise ValueError("the queue was not a queue")

    monkeypatch.setattr(work_attach, "_unresolved", blows_up)
    lines = work_attach.watch_apply(board)
    assert len(lines) == 1 and "not drained this pass" in lines[0] and "not a queue" in lines[0]


# ------------------------------------------------------------------------------------------------ W3, F3, N5: the drain
def test_two_concurrent_passes_name_each_report_once(board, monkeypatch):
    for i in range(20):
        _plant(board, report_id=f"r-{i}")
    barrier = threading.Barrier(2)
    real = work_attach._unresolved

    def both_read_first(directory):
        got = real(directory)
        barrier.wait(timeout=10)                          # both passes have read the queue before either writes
        return got

    monkeypatch.setattr(work_attach, "_unresolved", both_read_first)
    lines: list[str] = []
    ts = [threading.Thread(target=lambda: lines.extend(work_attach.watch_apply(board))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(lines) == 1, lines
    keys = work_attach.drained_path(board).read_text().split("\n")[:-1]
    assert sorted(keys) == sorted(f"r-{i}" for i in range(20)) and len(keys) == len(set(keys))
    log = work_attach.drained_log_path(board).read_text()
    assert log.count("board reports:") == 1


def test_the_line_is_written_before_the_key_is_recorded_and_a_crash_in_between_drains_again(board, monkeypatch):
    _plant(board, report_id="r-1")
    real = work_attach._append_durable
    calls = []

    def crash_on_the_marker(fd, text):
        calls.append(text)
        if len(calls) == 2:
            raise OSError("killed between the line and the key")
        real(fd, text)

    with monkeypatch.context() as m:
        m.setattr(work_attach, "_append_durable", crash_on_the_marker)
        with pytest.raises(OSError):
            work_attach.drain_reports(board)
    assert "board reports:" in calls[0] and calls[1] == "r-1\n"                # the line first, then the key
    assert "r-1" in work_attach.drained_log_path(board).read_text()            # the line is not lost
    assert work_attach.drained_path(board).read_text() == ""                   # the key was not recorded
    again = work_attach.drain_reports(board)
    assert again.drained == ["r-1"] and work_attach.drained_path(board).read_text() == "r-1\n"
    assert work_attach.drained_log_path(board).read_text().count("board reports:") == 2        # re-logged, never lost


def test_a_torn_last_marker_line_names_nothing_and_the_next_drain_repairs_it(board):
    _plant(board, report_id="r-a")
    _plant(board, report_id="r-b")
    _plant(board, report_id="r-c")
    work_attach.drained_path(board).write_text("r-a\nr-b")           # a crash mid-write: r-b has no newline
    got = work_attach.drain_reports(board)
    assert got.drained == ["r-b", "r-c"]
    assert work_attach._drained_keys(board) == {"r-a", "r-b", "r-c"}
    assert work_attach.drain_reports(board).drained == []


def test_the_drain_line_names_each_report_with_its_item_and_event(board):
    _plant(board, report_id="r-1", item_id="levain:a", event="landed", sha="a" * 40)
    line = work_attach.watch_apply(board)[0]
    assert "r-1 (levain:a landed)" in line and "not applied by this release" in line and "never applied" not in line


def test_the_marker_is_0600_and_never_followed_through_a_symlink(board, tmp_path):
    _plant(board, report_id="r-1")
    work_attach.drain_reports(board)
    assert work_attach.drained_path(board).stat().st_mode & 0o777 == 0o600
    other = tmp_path / "elsewhere"
    other.write_text("keep\n")
    work_attach.drained_path(board).unlink()
    work_attach.drained_path(board).symlink_to(other)
    _plant(board, report_id="r-2")
    lines = work_attach.watch_apply(board)
    assert len(lines) == 1 and "not drained this pass" in lines[0]
    assert other.read_text() == "keep\n"


def test_nothing_queued_makes_no_marker(board):
    assert work_attach.watch_apply(board) == []
    _plant(board, report_id="r-1")
    work_attach.drain_reports(board)
    work_attach.drained_path(board).unlink()
    work_attach.drained_log_path(board).unlink()
    # the queue is unchanged, so a lost marker drains it again: the marker is the only memory
    assert work_attach.drain_reports(board).drained == ["r-1"]


# ------------------------------------------------------------------------------------------------ W10, F2: report_status
def _seed_outcomes(store, token):
    v1_seed.add(store, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    c = sqlite3.connect(store.path)
    try:
        g1, g2 = (c.execute("INSERT INTO gseq (at) VALUES ('x')").lastrowid for _ in range(2))
        c.execute("INSERT INTO worker_attach (gseq, attach_id, item_id, item_ver, kind, worker, worker_kind, report_id, at, "
                  "applied_at, lease_token, by) VALUES (?, 'a-1', 'levain:a', 1, 'attach', 'w', 'seat', 'r-applied', 'x', "
                  f"'x', 1, '{ROLE_DESK}')", (g1,))
        c.execute("INSERT INTO report_refusals (gseq, report_id, item_id, worker, reason, detail, at, by) VALUES "
                  f"(?, 'r-refused', 'levain:a', 'w', 'held-by-other', 'someone else holds it', 'x', '{ROLE_DESK}')", (g2,))
        c.commit()
    finally:
        c.close()


def test_report_status_reads_applied_refused_queued_drained_and_unknown(desk_board):
    store, token = desk_board
    d = store.directory
    _seed_outcomes(store, token)
    _plant(d, report_id="r-queued")
    _plant(d, report_id="r-drained")
    work_attach.drained_path(d).write_text("r-drained\n")
    got = {rid: work_attach.report_status(d, rid) for rid in ("r-applied", "r-refused", "r-queued", "r-drained", "r-nope")}
    assert {k: v["state"] for k, v in got.items()} == {"r-applied": "applied", "r-refused": "refused", "r-queued": "queued",
                                                        "r-drained": "drained", "r-nope": "unknown"}
    assert got["r-applied"]["kind"] == "attach" and got["r-applied"]["attach_id"] == "a-1"
    assert got["r-refused"]["reason"] == "held-by-other" and got["r-refused"]["detail"] == "someone else holds it"
    assert got["r-drained"]["item_id"] == "levain:a"


def test_levain_desk_reports_says_each_outcome_in_words_and_exits_1_for_unknown(desk_board, capsys):
    store, token = desk_board
    d = store.directory
    _seed_outcomes(store, token)
    _plant(d, report_id="r-queued")
    _plant(d, report_id="r-drained")
    work_attach.drained_path(d).write_text("r-drained\n")
    for rid, words, code in (("r-applied", "applied (attach, a-1)", 0), ("r-refused", "refused (held-by-other: someone", 0),
                             ("r-queued", "the next watch pass drains it once", 0),
                             ("r-drained", "was drained into the watch's log and was never applied", 0),
                             ("r-nope", "not in the queue", 1)):
        assert main(["coord", "reports", rid, "--board-store", str(d)]) == code
        assert words in capsys.readouterr().out, rid
    assert main(["coord", "reports", "r-drained", "--board-store", str(d), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "drained"


# ------------------------------------------------------------------------------------------------ F1, F6: the refusals
def test_a_refused_report_says_landing_comes_from_git_and_import_status_says_board_add(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    assert main(["coord", "report", "levain#1", "--event", "landed"]) == 2
    err = capsys.readouterr().err
    assert "levain board add" in err and "read from git" in err and "trailer on main" in err
    assert main(["coord", "import-status", "STATUS.md"]) == 2
    err = capsys.readouterr().err
    assert "levain board add" in err and "no longer imported" in err
    assert main(["coord", "ready", "add", "levain:x"]) == 2
    assert "read from git" not in capsys.readouterr().err


def test_a_refusal_with_json_prints_a_json_error_object_on_stdout_and_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    assert main(["coord", "report", "levain#1", "--event", "landed", "--json"]) == 2
    cap = capsys.readouterr()
    got = json.loads(cap.out)
    assert got["retired"] is True and got["verb"] == "report" and got["written"] is False
    assert "levain board add" in got["why"] and "read from git" in got["why"]
    assert "nothing was written" in cap.err
    assert not (tmp_path / "home").exists()


# ------------------------------------------------------------------------------------------------ W4, N3: the claim
def test_a_malformed_claim_is_refused_before_the_board_registry_is_written(desk_board, tmp_path, monkeypatch):
    from levain.desk import board_registry

    store, token = desk_board
    registered = []
    monkeypatch.setattr(board_registry, "register_for_launch", lambda cwd: registered.append(cwd))
    for bad in ("levain:bad", "levain#0", "x" * 300 + "#1"):
        with pytest.raises(CoordError):
            store.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(tmp_path), attach_item=bad)
    assert registered == []
    store.launch_request(token, harness="claude", role=ROLE_DESK, cwd=str(tmp_path), attach_item="levain#3")
    assert registered == [str(tmp_path)]                      # a good claim still registers


def test_a_claim_id_is_bounded(desk_board):
    store, _token = desk_board
    ok = "a" * (lease.BOARD_ITEM_MAX - 2) + "#1"
    assert len(ok) == lease.BOARD_ITEM_MAX and lease.check_board_items([ok]) == [ok]
    with pytest.raises(lease.LeaseRefused, match="at most"):
        lease.check_board_items(["a" * lease.BOARD_ITEM_MAX + "#1"])


def test_a_damaged_claim_cell_is_a_named_unknown_claim_and_never_breaks_a_launch_read(desk_board):
    store, token = desk_board
    one = store.launch_request(token, harness="claude", role=ROLE_DESK, attach_item="levain#1")
    two = store.launch_request(token, harness="claude", role=ROLE_DESK, attach_item="levain#2")
    c = sqlite3.connect(store.path)
    try:
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'launch_board_items'"
                                 ).fetchall():
            c.execute(f"DROP TRIGGER {name}")
        c.execute("UPDATE launch_board_items SET items = '{not json' WHERE launch_id = ?", (one.launch_id,))
        c.commit()
        got = lease.launch(c, one.launch_id)
        assert got.board_items is None and "damaged" in got.board_items_why
        assert lease.launch(c, two.launch_id).board_items == ("levain#2",)       # the other launch reads whole
        c.execute("UPDATE launch_board_items SET items = '[1, 2]' WHERE launch_id = ?", (one.launch_id,))
        c.commit()
        assert lease.launch(c, one.launch_id).board_items_why is not None
        # the whole-state read (status, expiry, the watch) does not raise on the damaged cell
        assert {ln.launch_id for ln in lease._in_state(c, "pending")} == {one.launch_id, two.launch_id}
    finally:
        c.close()


# ------------------------------------------------------------------------------------------------ W8: the open's store
def test_the_open_reads_the_board_for_the_store_it_renders_not_the_process_home(desk_board, monkeypatch):
    store, _token = desk_board
    seen = []

    def spy(items=None, **kw):
        seen.append(kw.get("consent_dir"))
        return 0, {"schema": 3, "as_of": None, "items": [], "audit": []}, ""

    monkeypatch.setattr(board_cli, "state_doc", spy)
    assert seam._board(store, None, "") == ["Board: 0 items"]
    assert seen == [store.directory]


def test_state_doc_hands_consent_dir_to_the_native_producer(tmp_path, monkeypatch):
    from levain.desk import board_producers as bp

    seen = []
    real = bp.native_blocks
    monkeypatch.setattr(bp, "native_blocks", lambda **kw: seen.append(kw.get("consent_dir")) or real(**kw))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "nohome"))
    monkeypatch.delenv("LEVAIN_BOARD_ITEMS", raising=False)
    board_cli.state_doc(consent_dir=tmp_path / "somewhere")           # no items store: refuses before the producers run
    assert seen in ([], [tmp_path / "somewhere"])


# ------------------------------------------------------------------------------------------------ the deleted dead code
def test_the_kept_dead_service_code_is_gone():
    from levain.desk import store as store_mod

    for name in ("service_claim", "service_release", "SERVICE_HOLDER", "SERVICE_TTL", "_yield_service"):
        assert not hasattr(lease, name), name
    for name in ("_SERVICE_INSERTS", "_SERVICE_UPDATES", "_SERVICE_DELETES", "_SERVICE_NEVER", "_service_authorizer"):
        assert not hasattr(store_mod, name), name
    for name in ("parse_rows", "blob_sha"):
        from levain.desk import status_import

        assert not hasattr(status_import, name), name
    assert not hasattr(work_attach, "_resolved")
    store = CoordStore.__new__(CoordStore)
    assert not hasattr(store, "role")
    with pytest.raises(TypeError):
        CoordStore(Path("/nonexistent"), role="service")


def test_ready_has_no_unused_import_left_for_a_test_to_lean_on():
    assert not hasattr(ready, "parse_stamp") and not hasattr(ready, "next_gseq") and not hasattr(ready, "_OID")


def test_the_v1_tables_still_carry_their_service_triggers_in_sql():
    """The spec keeps the tables and their append triggers; their SQL still names the ``service`` role though no Python
    writes as it now (left in place on purpose)."""
    sql = " ".join(ready.LIFECYCLE_SCHEMA)
    assert "ready_journal_service_facts" in sql and "ready_items_service_facts" in sql
