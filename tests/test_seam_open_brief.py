"""C15 S2b: the open's tables, ``open_brief`` and the two build paths. Every expectation was first read from a real run
on a scratch store (an active coordinator launch holding the lease for the fenced path; a pending launch whose spawn
the starter claimed for the cold-start path)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.consent.questions import QuestionStore
from levain.coordinator import seam, seam_build, seam_handoffs, seam_open
from levain.coordinator.lease import LeaseRefused
from levain.coordinator.store import CoordError, CoordStore

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
HANDOFF = {"state_line": "main is green", "in_flight": [], "next": "open the queue", "contingencies": [], "notes": "",
           "attested_operator_present": False, "attested_walked": False}


def _dir(tmp_path, name="d"):
    d = tmp_path / name
    d.mkdir(mode=0o700)
    return d


def _sql(store, sql, args=()):
    c = sqlite3.connect(store.path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _raw(store, sql, args=()):
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        c.execute(sql, args)
    finally:
        c.close()


@pytest.fixture
def world(tmp_path):
    """An active coordinator launch holds the lease (activation claims it)."""
    s = CoordStore(_dir(tmp_path), clock=lambda: NOW)
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.accept_drop("first words", surface="cli")
    ln = s.coord_status()["launches"][0]
    got, why = s.launch_activate(ln["launch_id"], harness_session_id="probe-A")
    assert why is None and got.state == "active"
    s.session_key = got.session_key
    s.launch_id = got.launch_id
    s.qdir = _dir(tmp_path, "q")
    return s


def _token(s):
    return s.coord_status()["lease"]["token"]


def _brief(s, kind="operator", **kw):
    return seam.open_brief(s, s.qdir, kind, NOW, **kw)


def _build(s, key="k1", kind="operator", **kw):
    return seam_build.build(s, kind=kind, signal_kind="silence", signal_key=key, queue_dir=s.qdir, token=_token(s), **kw)


def _ack(s, open_id, deliver=True, **kw):
    # an acknowledgement is of a brief that was handed over: the helper delivers first unless told not to
    if deliver:
        seam_open.record_delivery(s, open_id, "light_verb", recipient=s.launch_id)
    return seam_open.acknowledge(s, open_id, token=_token(s), **kw)


def _close(s, request_id="c1", prev=None, kind="rotation"):
    return seam_handoffs.close(s, kind=kind, body=dict(HANDOFF), request_id=request_id, prev_handoff_id=prev,
                               token=_token(s))


def _old_binary_drop(s, n):
    _raw(s, "INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, source_how) "
            "VALUES (?, 'cli', ?, 'w', 'h', 't', 't', 'none')", (f"dr-{n:032x}", f"old-{n}"))


# --- the fixture stores ---------------------------------------------------------------------------------------------
def test_an_empty_store(tmp_path):
    s = CoordStore(_dir(tmp_path))
    s._connect().close()
    b = seam.open_brief(s, tmp_path / "q", "light", NOW)
    assert (b.cursor_gseq, b.shown_through_gseq, b.handoff_id, b.omitted_rows) == (0, 0, None, 0)
    for line in ("Handoff: no handoff; no COUNTED row yet; derived", "Last acknowledged open: no prior acknowledged open",
                 "Handoff section: absent (no seam_handoffs row)", "Since (gseq 0 to 0): 0 counted rows",
                 "Unsequenced (rows written by an older release): 0", "Unacknowledged: 0", "Board: 0 items",
                 f"Not built: {seam.NOT_BUILT}"):
        assert line in b.text.splitlines(), line


def test_no_store_at_all_is_absent_and_nothing_is_made(tmp_path):
    s = CoordStore(tmp_path / "nowhere")
    b = seam.open_brief(s, tmp_path / "q", "light", NOW)
    assert "store: absent: there is no coordinator store here" in b.text and b.cursor_gseq == 0
    assert not (tmp_path / "nowhere").exists() and not (tmp_path / "q").exists()


def test_a_handoff_only_store_shows_the_handoff_whole(world):
    h = _close(world)
    b = _brief(world)
    lines = b.text.splitlines()
    assert b.handoff_id == h.handoff_id
    assert f"Handoff section: cut at gseq {h.covers_gseq}, 0 counted row(s) since (rows, not events)" in lines
    assert "  state: main is green" in lines and "  next: open the queue" in lines
    assert any(ln.startswith(f"Handoff: #1 (rotation) written ") for ln in lines)


def test_a_store_behind_head_counts_the_rows_since_the_cut(world):
    h = _close(world)
    world.accept_drop("landed after the close", surface="cli")           # a drop and its receipt: two counted rows
    b = _brief(world)
    assert f"Handoff section: cut at gseq {h.covers_gseq}, 2 counted row(s) since (rows, not events)" in b.text
    assert b.cursor_gseq > h.covers_gseq


def test_a_crash_store_has_rows_and_no_handoff(world):
    b = _brief(world)
    assert b.handoff_id is None
    assert any(ln.startswith("Handoff: no handoff; last COUNTED row at ") and ln.endswith("; derived")
               for ln in b.text.splitlines())
    assert "  drops: 1" in b.text and "  replies: 1" in b.text


def test_an_unreadable_queue_dir_says_so_and_the_store_sections_stand(world, tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("not a directory")
    junk = tmp_path / "junk"
    junk.mkdir(mode=0o700)
    (junk / "questions.db").write_bytes(b"this is not a database" * 50)
    for qdir in (afile, junk):
        b = seam.open_brief(world, qdir, "operator", NOW)
        lines = b.text.splitlines()
        assert any(ln.startswith("Band: cannot-derive (") for ln in lines), qdir
        assert any(ln.startswith("Card: cannot-derive (the queue directory could not be read") for ln in lines)
        assert "0 open items" not in b.text and "away: off" not in b.text      # never an empty answer for an unread one
        assert "Board: 0 items" in lines and "Handoff: no handoff" in b.text and b.cursor_gseq > 0
    gone = seam.open_brief(world, tmp_path / "never-made", "operator", NOW)    # no queue directory is a band of nothing
    assert "0 open items" in gone.text and not (tmp_path / "never-made").exists()


def test_tables_absent_are_not_tables_present_and_empty(world):
    present = _brief(world).text.splitlines()
    assert "Unacknowledged: 0" in present and "Board: 0 items" in present
    for t in ("open_unsequenced_cuts", "open_deliveries", "open_acks", "open_builds", "ready_items", "ready_journal"):
        _raw(world, f"DROP TABLE {t}")
    absent = _brief(world).text.splitlines()
    assert any(ln.startswith("Unacknowledged: absent (") for ln in absent)
    assert "Board: no lifecycle tables" in absent
    assert "Unacknowledged: 0" not in absent and "Board: 0 items" not in absent


# --- the cursor rule ------------------------------------------------------------------------------------------------
def test_a_build_never_acked_does_not_move_since(world):
    before = _brief(world)
    b = _build(world)
    assert (b.cursor_gseq, b.shown_through_gseq) == (before.cursor_gseq, before.cursor_gseq)
    again = _brief(world)
    assert f"Since (gseq 0 to {before.cursor_gseq})" in again.text            # still the window from the start
    assert f"open {b.open_id} (operator) built 0m ago, never acknowledged; never delivered" in again.text
    assert seam_open.record_delivery(world, b.open_id, "launch_brief", recipient=world.launch_id) is True
    assert seam_open.record_delivery(world, b.open_id, "launch_brief", recipient=world.launch_id) is False
    delivered = _brief(world)
    assert "delivered via launch_brief" in delivered.text and f"Since (gseq 0 to {before.cursor_gseq})" in delivered.text
    _ack(world, b.open_id)
    acked = _brief(world)
    assert f"Since (gseq {b.shown_through_gseq} to " in acked.text and "Unacknowledged: 0" in acked.text


def test_a_duplicate_signal_returns_the_same_build(world):
    a = _build(world, "same")
    world.accept_drop("rows landed between", surface="cli")
    assert _build(world, "same") == a
    assert len(_sql(world, "SELECT open_id FROM open_builds")) == 1
    assert _build(world, "other").open_id == a.open_id + 1


def test_the_cursor_is_the_max_shown_through_over_acked_opens_and_never_regresses(world):
    small = _build(world, "small")
    world.accept_drop("more", surface="cli")
    large = _build(world, "large")
    assert large.shown_through_gseq > small.shown_through_gseq
    _ack(world, large.open_id)
    _ack(world, small.open_id)                      # the smaller build is acknowledged LATER
    with sqlite3.connect(world.path) as c:
        assert seam_open.acked_cursor(c) == (large.shown_through_gseq, large.open_id)
    assert f"Since (gseq {large.shown_through_gseq} to " in _brief(world).text


def test_a_later_truncated_build_acked_last_does_not_pull_the_cursor_back(world):
    for i in range(30):
        world.accept_drop(f"drop number {i}", surface="cli")
    full = _build(world, "full")
    capped = _build(world, "capped", body_cap=len(full.body.encode()) - 600)
    assert capped.open_id > full.open_id and capped.shown_through_gseq < full.shown_through_gseq
    _ack(world, full.open_id)
    _ack(world, capped.open_id)                       # the newer build, which showed less, is acknowledged last
    with sqlite3.connect(world.path) as c:
        assert seam_open.acked_cursor(c) == (full.shown_through_gseq, full.open_id)


def test_truncation_stops_at_the_last_row_rendered_and_the_ack_reaches_no_further(world):
    for i in range(30):
        world.accept_drop(f"drop number {i}", surface="cli")
    full = _brief(world)
    cap = len(full.text.encode()) - 600
    cut = _brief(world, body_cap=cap)
    assert cut.omitted_rows > 0 and cut.shown_through_gseq < cut.cursor_gseq == full.cursor_gseq
    detail = [ln for ln in cut.text.splitlines() if ln.startswith("  gseq ")]
    assert int(detail[-1].split()[1]) == cut.shown_through_gseq              # the last row actually rendered
    assert len(detail) + cut.omitted_rows == len([ln for ln in full.text.splitlines() if ln.startswith("  gseq ")])
    assert len(cut.text.encode()) <= cap
    b = _build(world, "cut", body_cap=cap)
    assert (b.cursor_gseq, b.shown_through_gseq) == (cut.cursor_gseq, cut.shown_through_gseq)
    _ack(world, b.open_id)
    nxt = _brief(world)
    assert f"Since (gseq {b.shown_through_gseq} to {b.cursor_gseq}" in nxt.text
    shown_again = [ln for ln in nxt.text.splitlines() if ln.startswith("  gseq ")]
    assert len(shown_again) == cut.omitted_rows                              # the omitted rows return, none lost


def test_the_never_cut_sections_must_fit_the_cap(world):
    with pytest.raises(seam.BriefTooBig):
        _brief(world, body_cap=200)


def test_unsequenced_rows_are_their_own_class_and_are_cut_able(world):
    assert "Unsequenced (rows written by an older release): 0" in _brief(world).text
    _old_binary_drop(world, 1)
    b = _brief(world)
    assert "  drops: 1 row(s) written by an older release (rowids 3 to 3)" in b.text.splitlines() or \
        any(ln.startswith("  drops: 1 row(s) written by an older release") for ln in b.text.splitlines())
    rowid = _sql(world, "SELECT rowid FROM drops WHERE gseq IS NULL")[0][0]
    assert b.unsequenced_cuts == {"drops": rowid}
    built = _build(world, "u")
    assert _sql(world, "SELECT table_name, max_rowid FROM open_unsequenced_cuts WHERE open_id = ?",
                (built.open_id,)) == [("drops", rowid)]
    assert any(ln.startswith("  drops: 1 row(s)") for ln in _brief(world).text.splitlines())    # unacked: still there
    _ack(world, built.open_id)
    assert "Unsequenced (rows written by an older release): 0" in _brief(world).text
    _old_binary_drop(world, 2)
    assert any(ln.startswith("  drops: 1 row(s) written by an older release") for ln in _brief(world).text.splitlines())


def test_the_brief_records_no_band_view_and_numbers_nothing(world):
    qs = QuestionStore(world.qdir)
    qs.file(agent="claude_code", agent_session_id="s", what="Which?", options=[("a", "A"), ("b", "B")], rec="a",
            blocks="x", cwd="/tmp")

    def dump():
        out = {}
        for p in sorted(world.qdir.iterdir()):
            if p.suffix == ".db":
                c = sqlite3.connect(p)
                try:
                    out[p.name] = list(c.iterdump())
                finally:
                    c.close()
        return out

    before = dump()
    b = _build(world, "band")
    assert dump() == before                                                # no row, table or view in the queue side
    tables = {r[0] for r in sqlite3.connect(world.qdir / "questions.db").execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables.isdisjoint({"band_items", "band_views", "band_closed_shown"})
    assert "  new: Which?" in b.body.splitlines() and "#1" not in b.body


def test_the_brief_is_a_function_of_the_stores_and_now(world):
    assert _brief(world) == _brief(world)
    assert _brief(world) != seam.open_brief(world, world.qdir, "operator", NOW + timedelta(hours=1))


@pytest.mark.parametrize("kind", ["weekly", "", None])
def test_an_unknown_kind_is_refused(world, kind):
    with pytest.raises(CoordError, match="kind rotation, operator, dream, light"):
        _brief(world, kind=kind)


def test_now_is_required_and_aware(world):
    with pytest.raises(CoordError):
        seam.open_brief(world, world.qdir, "light", None)
    with pytest.raises(CoordError, match="aware"):
        seam.open_brief(world, world.qdir, "light", datetime(2026, 10, 31, 12, 0))


# --- the fence and the acknowledging write ---------------------------------------------------------------------------
def test_the_operators_lease_is_refused_by_every_fenced_write(tmp_path):
    s = CoordStore(_dir(tmp_path))
    tok = s.coord_take()                                                   # holder is literally "operator"
    with pytest.raises(LeaseRefused, match="written by an active coordinator launch; the lease holder is the operator"):
        seam_build.build(s, kind="light", signal_kind="silence", signal_key="k", queue_dir=tmp_path / "q", token=tok)
    with pytest.raises(LeaseRefused, match="the lease holder is the operator"):
        seam_open.acknowledge(s, 1, token=tok)
    assert _sql(s, "SELECT COUNT(*) FROM open_builds") == [(0,)]


def test_a_missing_or_stale_token_writes_nothing(world):
    with pytest.raises(CoordError, match="live lease token"):
        seam_build.build(world, kind="light", signal_kind="silence", signal_key="k", queue_dir=world.qdir, token=None)
    with pytest.raises(LeaseRefused, match="not the live lease's"):
        seam_build.build(world, kind="light", signal_kind="silence", signal_key="k", queue_dir=world.qdir,
                         token=_token(world) + 7)
    assert _sql(world, "SELECT COUNT(*) FROM open_builds") == [(0,)]


def test_the_reader_session_is_derived_and_a_read_back_is_required_on_rotation_and_dream(world):
    rot = _build(world, "r", kind="rotation")
    with pytest.raises(CoordError, match="read-back"):
        _ack(world, rot.open_id)
    assert _sql(world, "SELECT COUNT(*) FROM open_acks") == [(0,)]
    assert _ack(world, rot.open_id, read_back="main green; next the queue") == (rot.open_id, world.session_key, True)
    assert _ack(world, rot.open_id, read_back="again") == (rot.open_id, world.session_key, False)
    assert _sql(world, "SELECT reader_session, read_back FROM open_acks") == [(world.session_key, "main green; next the queue")]
    op = _build(world, "o")
    assert _ack(world, op.open_id)[2] is True                              # optional on an operator open
    with pytest.raises(CoordError, match="no open build 99"):
        _ack(world, 99)
    with pytest.raises(sqlite3.DatabaseError, match="read-back"):          # the trigger holds it for a raw writer too
        _raw(world, "INSERT INTO open_acks VALUES (?, 's-x', '  ', 't')", (rot.open_id,))


def test_a_delivery_surface_is_one_of_two(world):
    b = _build(world)
    with pytest.raises(CoordError, match="launch_brief, light_verb"):
        seam_open.record_delivery(world, b.open_id, "email", recipient=world.launch_id)
    with pytest.raises(CoordError, match="no open build"):
        seam_open.record_delivery(world, 99, "light_verb", recipient=world.launch_id)


# --- the starter path -----------------------------------------------------------------------------------------------
@pytest.fixture
def pending(tmp_path):
    clock = [datetime.now(timezone.utc)]
    s = CoordStore(_dir(tmp_path, "dp"), clock=lambda: clock[0])
    s.coord_set_agent("claude", cwd=str(tmp_path))
    s.accept_drop("first words", surface="cli")
    s.launch_id = s.coord_status()["launches"][0]["launch_id"]
    s.clock = clock
    s.qdir = tmp_path / "q"
    return s


def _starter(s, launch_id=None, pid=None, kind="operator"):
    return seam_build.starter_build(s, s.launch_id if launch_id is None else launch_id,
                                    pid=os.getpid() if pid is None else pid, kind=kind, queue_dir=s.qdir)


def _footprint(s):
    return [_sql(s, f"SELECT COUNT(*) FROM {t}")[0][0] for t in
            ("lease_events", "launch_events", "launch_spawns", "gseq", "open_builds", "open_unsequenced_cuts")]


def test_a_cold_start_builds_for_the_launch_it_claimed(pending):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    b = _starter(pending)
    assert (b.signal_kind, b.signal_key, b.requires_ack, b.seam_kind) == ("launch", pending.launch_id, 1, "operator")
    assert b.cursor_gseq == b.shown_through_gseq > 0 and b.handoff_id is None
    assert "no handoff" in b.body
    assert _starter(pending) == b and len(_sql(pending, "SELECT 1 FROM open_builds")) == 1   # a duplicate signal


def test_a_launch_claimed_by_another_pid_is_refused_and_nothing_is_written(pending):
    pending.launch_claim_spawn(pending.launch_id, os.getpid() + 1)
    before = _footprint(pending)
    with pytest.raises(LeaseRefused, match=f"claimed by pid {os.getpid() + 1}, not by pid {os.getpid()}"):
        _starter(pending)
    assert _footprint(pending) == before


def test_a_launch_whose_spawn_nobody_claimed_is_refused(pending):
    before = _footprint(pending)
    with pytest.raises(LeaseRefused, match="not claimed"):
        _starter(pending)
    assert _footprint(pending) == before


def test_an_unknown_or_non_pending_launch_is_refused(pending, world):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    with pytest.raises(LeaseRefused, match="no launch"):
        _starter(pending, launch_id="ln-" + "0" * 32)
    with pytest.raises(CoordError, match="launch id"):
        _starter(pending, launch_id=7)
    world_launch = world.coord_status()["launches"][0]["launch_id"]       # `world`'s launch is active, not pending
    with pytest.raises(LeaseRefused, match="active, not pending"):
        seam_build.starter_build(world, world_launch, pid=os.getpid(), kind="light", queue_dir=world.qdir)


def test_a_launch_past_its_until_is_refused_and_inherits_no_expiry_write(pending):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    pending.clock[0] += timedelta(minutes=6)                               # pending lasts five minutes
    before = _footprint(pending)
    with pytest.raises(LeaseRefused, match="was pending only until"):
        _starter(pending)
    assert _footprint(pending) == before                                   # claim_spawn would have written `expired`


def test_a_launch_whose_lease_moved_is_refused(pending):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    pending.coord_take()                                                   # the operator takes the lease: a new token
    with pytest.raises(LeaseRefused):
        _starter(pending)
    assert _sql(pending, "SELECT COUNT(*) FROM open_builds") == [(0,)]


# --- the records ----------------------------------------------------------------------------------------------------
def test_an_arrival_signal_is_one_per_agent_and_mark_and_is_counted(world):
    def put(agent, mark):
        with world._write("coordinator", _token(world)) as (conn, now):
            return seam_open.record_arrival(conn, now, agent=agent, mark_id=mark, queue_id="q-1")

    first = put("claude_code", 7)
    gseq = _sql(world, "SELECT gseq FROM arrival_signals")
    assert put("claude_code", "7") == (first[0], False) and _sql(world, "SELECT gseq FROM arrival_signals") == gseq
    assert put("codex", 7)[1] is True
    assert "  arrival_signals: 2" in _brief(world).text.splitlines()
    with pytest.raises(CoordError, match="agent"):
        with world._write("coordinator", _token(world)) as (conn, now):
            seam_open.record_arrival(conn, now, agent="", mark_id=1, queue_id="q")


def test_every_open_table_is_append_only_and_the_build_is_bounded(world):
    b = _build(world)
    _ack(world, b.open_id)
    seam_open.record_delivery(world, b.open_id, "light_verb", recipient=world.launch_id)
    with world._write("coordinator", _token(world)) as (conn, now):
        seam_open.record_arrival(conn, now, agent="a", mark_id=1, queue_id="q")
    for table, col in (("open_builds", "seam_kind"), ("open_deliveries", "surface"), ("open_acks", "reader_session"),
                       ("arrival_signals", "agent")):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            _raw(world, f"UPDATE {table} SET {col} = {col}")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            _raw(world, f"DELETE FROM {table}")
    ins = ("INSERT INTO open_builds (seam_kind, signal_kind, signal_key, cursor_gseq, shown_through_gseq, requires_ack, "
           "body, body_sha256, built_at) VALUES ('light', 'silence', ?, ?, ?, ?, ?, 'h', 't')")
    _raw(world, ins, ("ok", 5, 5, 1, "x"))
    for args in (("big", 5, 5, 1, "x" * (seam_open.BODY_CAP + 1)), ("ahead", 5, 6, 1, "x"), ("noack", 5, 5, 0, "x")):
        with pytest.raises(sqlite3.IntegrityError):
            _raw(world, ins, args)
    with pytest.raises(sqlite3.IntegrityError):
        _raw(world, "INSERT OR REPLACE INTO open_builds (seam_kind, signal_kind, signal_key, cursor_gseq, "
                    "shown_through_gseq, requires_ack, body, body_sha256, built_at) VALUES ('light', 'silence', 'ok', "
                    "0, 0, 1, 'y', 'h', 't')")


# --- review fixes: a degraded brief, the ack's preconditions, the records, the cap -----------------------------------
def _counts(s):
    return {t: _sql(s, f"SELECT COUNT(*) FROM {t}")[0][0] for t in
            ("open_builds", "open_deliveries", "open_acks", "open_unsequenced_cuts", "arrival_signals", "lease_events",
             "launch_events", "gseq")}


def _fail_once(monkeypatch, module, name, exc):
    real = getattr(module, name)
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise exc
        return real(*a, **kw)

    monkeypatch.setattr(module, name, flaky)


def test_a_brief_whose_store_read_failed_is_never_stored_and_a_retry_builds_it_whole(world, monkeypatch):
    _fail_once(monkeypatch, seam, "_counted_head", sqlite3.OperationalError("disk I/O error"))
    before = _counts(world)
    with pytest.raises(CoordError, match=r"store: cannot-derive: Cut: cannot-derive \(OperationalError: disk I/O error\)"):
        _build(world, "flaky")
    assert _counts(world) == before
    b = _build(world, "flaky")
    assert "store: cannot-derive" not in b.body and b.cursor_gseq > 0 and "Since (gseq 0 to" in b.body


def test_a_cold_start_whose_store_read_failed_is_never_stored(pending, monkeypatch):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    _fail_once(monkeypatch, seam, "_counted_head", sqlite3.OperationalError("disk I/O error"))
    before = _footprint(pending)
    with pytest.raises(CoordError, match="cannot-derive"):
        _starter(pending)
    assert _footprint(pending) == before
    b = _starter(pending)
    assert "store: cannot-derive" not in b.body and b.cursor_gseq > 0


def test_a_detail_read_that_fails_degrades_the_brief_and_the_build_refuses_it(world, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("detail broke")

    monkeypatch.setattr(seam, "_detail", broken)
    b = _brief(world)
    assert b.degraded and "Since: cannot-derive (RuntimeError: detail broke)" in b.text.splitlines()
    assert b.shown_through_gseq == 0 and b.cursor_gseq > 0                   # nothing of the window was read
    before = _counts(world)
    with pytest.raises(CoordError, match="detail broke"):
        _build(world, "d")
    assert _counts(world) == before


def test_an_ack_of_a_build_never_delivered_is_refused(world):
    b = _build(world, "undelivered")
    before = _counts(world)
    with pytest.raises(CoordError, match="never delivered"):
        _ack(world, b.open_id, deliver=False)
    assert _counts(world) == before


def test_an_arrival_outside_a_write_transaction_is_refused(world):
    c = sqlite3.connect(world.path, isolation_level=None)
    try:
        with pytest.raises(CoordError, match="write transaction"):
            seam_open.record_arrival(c, "2026-10-31T12:00:00Z", agent="a", mark_id=1, queue_id="q")
    finally:
        c.close()
    assert _sql(world, "SELECT COUNT(*) FROM arrival_signals") == [(0,)]


class _MissFirstLookup:
    """A connection whose first arrival lookup sees nothing: the shape of a racing writer between the read and the
    insert."""

    def __init__(self, conn):
        self.conn, self.missed = conn, False

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def execute(self, sql, args=()):
        if not self.missed and sql.startswith("SELECT signal_id FROM arrival_signals"):
            self.missed = True
            return self.conn.execute("SELECT 1 WHERE 0")
        return self.conn.execute(sql, args)


def test_an_arrival_whose_duplicate_the_lookup_missed_returns_the_stored_one(world):
    with world._write("coordinator", _token(world)) as (conn, now):
        first = seam_open.record_arrival(conn, now, agent="a", mark_id=1, queue_id="q")
    gseq = _sql(world, "SELECT COUNT(*) FROM gseq")
    with world._write("coordinator", _token(world)) as (conn, now):
        again = seam_open.record_arrival(_MissFirstLookup(conn), now, agent="a", mark_id="1", queue_id="other-q")
    assert again == (first[0], False)
    assert _sql(world, "SELECT COUNT(*) FROM arrival_signals") == [(1,)]
    assert _sql(world, "SELECT COUNT(*) FROM gseq")[0][0] == gseq[0][0] + 1       # only the fence's own write record


def _counted_rows(s, n):
    c = sqlite3.connect(s.path)
    try:
        with c:
            for i in range(n):
                g = c.execute("INSERT INTO gseq (at) VALUES ('2026-10-31T11:00:00Z')").lastrowid
                c.execute("INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
                          "source_how, gseq) VALUES (?, 'cli', ?, 'w', 'h', 't', 't', 'none', ?)",
                          (f"dr-{i:032x}", f"bulk-{i}", g))
    finally:
        c.close()


def test_a_cap_over_the_column_cap_is_clamped_and_a_non_ascii_brief_is_cut_in_bytes(world, monkeypatch):
    monkeypatch.setattr(seam, "DETAIL_ROWS_MAX", 6000)
    seam_handoffs.close(world, kind="operator", body={**HANDOFF, "notes": "€" * 4000}, request_id="big",
                        prev_handoff_id=None, token=_token(world))
    _counted_rows(world, 4000)
    full = _brief(world, body_cap=10 * seam_open.BODY_CAP)
    assert len(full.text.encode()) > seam_open.BODY_CAP                    # the brief this store renders uncut
    b = _build(world, "big", body_cap=10 * seam_open.BODY_CAP)
    size = len(b.body.encode("utf-8"))
    assert size <= seam_open.BODY_CAP and size > len(b.body)             # bytes, not characters
    assert b.shown_through_gseq < b.cursor_gseq
    assert "  notes: " + "€" * 4000 in b.body.splitlines()                 # the handoff is never cut


@pytest.mark.parametrize("text", ["x" * (seam_open.BODY_CAP + 1), "a lone surrogate \ud800"], ids=["over", "surrogate"])
def test_a_body_the_column_cannot_hold_is_a_coord_error_and_nothing_is_written(world, monkeypatch, text):
    monkeypatch.setattr(seam_build, "open_brief", lambda *a, **kw: seam.Brief("operator", text, "t", 0, 0, None))
    before = _counts(world)
    with pytest.raises(CoordError, match="body"):
        _build(world, "odd")
    assert _counts(world) == before


def test_a_stored_build_of_another_kind_is_refused(world):
    a = _build(world, "same", kind="operator")
    with pytest.raises(CoordError, match="operator"):
        _build(world, "same", kind="rotation")
    assert _sql(world, "SELECT open_id, seam_kind FROM open_builds") == [(a.open_id, "operator")]


def test_a_cold_start_runs_the_several_threads_check_and_writes_nothing(pending):
    from levain.coordinator import lease
    from levain.coordinator.store import Part

    d = pending.accept_drop("two pieces of work", surface="cli")[0]
    t1, t2 = pending.split(d.drop_id, [Part("work", "one"), Part("work", "two")])
    with pending._write() as (conn, now):
        tok = lease.lease(conn, now).token
        worker = lease._pending(conn, now, token=tok, harness="claude", role="worker",
                                threads=[t1.thread_id, t2.thread_id], model=None, cwd=None, cause="requested")
    before = _footprint(pending)
    with pytest.raises(LeaseRefused, match="a worker holds one thread"):
        _starter(pending, launch_id=worker.launch_id)
    assert _footprint(pending) == before


def test_a_live_lease_held_by_an_active_worker_writes_no_open_record(world):
    from levain.coordinator.store import Part

    d = world.accept_drop("work for a worker", surface="cli")[0]
    (t,) = world.split(d.drop_id, [Part("work", "w")])
    wl = world.launch_request(_token(world), harness="claude", role="worker", threads=[t.thread_id])
    got, why = world.launch_activate(wl.launch_id, harness_session_id="probe-W")
    assert why is None and got.state == "active" and got.role == "worker"
    b = _build(world, "w")
    seam_open.record_delivery(world, b.open_id, "light_verb", recipient=world.launch_id)
    c = sqlite3.connect(world.path)
    try:
        with c:
            g = c.execute("INSERT INTO gseq (at) VALUES ('2026-10-31T12:00:00Z')").lastrowid
            tok = c.execute("INSERT INTO lease_events (gseq, kind, token, holder, until, cause, by, at) VALUES "
                            "(?, 'claim', NULL, ?, '2099-01-01T00:00:00Z', 'probe', 'coordinator', "
                            "'2026-10-31T12:00:00Z')", (g, got.session_key)).lastrowid
    finally:
        c.close()
    before = _counts(world)
    with pytest.raises(LeaseRefused, match="active coordinator launch"):
        seam_open.acknowledge(world, b.open_id, token=tok)
    with pytest.raises(LeaseRefused, match="active coordinator launch"):
        seam_build.build(world, kind="light", signal_kind="silence", signal_key="w2", queue_dir=world.qdir, token=tok)
    assert _counts(world) == before


# --- review fixes 2: one degraded rule, one input bound, the same build, the recipient, the exact cap ---------------
def _malformed_handoff(s, body='{"state_line": "a handoff with no other key"}'):
    # a raw row the writer would refuse but the triggers let through: its body passes the state_line floor only
    c = sqlite3.connect(s.path)
    try:
        with c:
            g = c.execute("INSERT INTO gseq (at) VALUES ('2026-10-31T11:00:00Z')").lastrowid
            c.execute("INSERT INTO seam_handoffs (close_key, seam_kind, written_at, writer, gseq, covers_gseq, "
                      "prev_handoff_id, request_digest, body) VALUES ('raw', 'rotation', 't', 'w', ?, 0, NULL, 'd', ?)",
                      (g, body))
    finally:
        c.close()


def test_a_handoff_that_does_not_parse_degrades_the_brief_and_the_build_refuses_it(world):
    _malformed_handoff(world)
    b = _brief(world, kind="rotation")
    assert b.degraded and b.degraded.startswith("Handoff section: cannot-derive (KeyError")
    assert b.degraded in b.text.splitlines()
    before = _counts(world)
    with pytest.raises(CoordError, match="degraded brief is never stored"):
        _build(world, "h", kind="rotation")
    assert _counts(world) == before


@pytest.mark.parametrize("name, exc", [("acked_cuts", TypeError("a malformed cut")),
                                       ("acked_cursor", sqlite3.OperationalError("database is locked")),
                                       ("_latest_handoff", sqlite3.OperationalError("disk I/O error")),
                                       ("_unsequenced", TypeError("NULL baseline")),
                                       ("_unacknowledged", sqlite3.OperationalError("database is locked"))])
def test_a_read_the_successor_must_see_that_fails_degrades_the_brief(world, monkeypatch, name, exc):
    def broken(*a, **kw):
        raise exc

    monkeypatch.setattr(seam, name, broken)
    b = _brief(world)
    assert b.degraded and str(exc) in b.degraded and str(exc) in b.text
    with pytest.raises(CoordError, match="degraded brief is never stored"):
        seam_build._storable(b, seam_open.BODY_CAP)


def test_a_band_or_card_failure_is_shown_and_the_brief_still_stores(world, tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("not a directory")
    b = seam.open_brief(world, afile, "operator", NOW)
    assert any(ln.startswith("Band: cannot-derive (") for ln in b.text.splitlines()) and b.degraded is None


def test_a_coordinator_directory_open_to_others_is_a_degraded_brief_never_a_crash(world):
    os.chmod(world.directory, 0o755)
    try:
        b = _brief(world)
        assert b.degraded and "reachable by group or others" in b.degraded and b.cursor_gseq == 0
        with pytest.raises(CoordError, match="degraded brief is never stored"):
            seam_build._storable(b, seam_open.BODY_CAP)
    finally:
        os.chmod(world.directory, 0o700)
    assert _sql(world, "SELECT COUNT(*) FROM open_builds") == [(0,)]


def test_a_read_back_is_bounded_in_bytes_and_a_lone_surrogate_is_a_coord_error(world):
    b = _build(world, "rb", kind="rotation")
    seam_open.record_delivery(world, b.open_id, "light_verb", recipient=world.launch_id)
    before = _counts(world)
    for text in ("x" * 8193, "€" * 2731, "a lone surrogate \ud800"):         # 2731 euro signs are 8193 bytes
        with pytest.raises(CoordError, match="read_back"):
            seam_open.acknowledge(world, b.open_id, token=_token(world), read_back=text)
    assert _counts(world) == before
    assert seam_open.acknowledge(world, b.open_id, token=_token(world), read_back="€" * 2730 + "xx")[2] is True


def test_every_caller_text_is_checked_before_the_write_opens(world):
    before = _counts(world)
    with pytest.raises(CoordError, match="signal key"):
        _build(world, "a lone surrogate \ud800")
    with pytest.raises(CoordError, match="agent"):
        with world._write("coordinator", _token(world)) as (conn, now):
            seam_open.record_arrival(conn, now, agent="\udfff", mark_id=1, queue_id="q")
    with pytest.raises(CoordError, match="queue_id"):
        with world._write("coordinator", _token(world)) as (conn, now):
            seam_open.record_arrival(conn, now, agent="a", mark_id=1, queue_id="q\ud800")
    assert _counts(world) == before                    # a refusal inside a write rolls its fence record back too
    for name, value in (("signal key", "\ud800"), ("read_back", "\ud800")):
        with pytest.raises(CoordError, match=name):
            seam_open.checked_text(value, name)


def _no_render(*a, **kw):
    raise AssertionError("a signal already built is returned before anything is rendered")


def test_a_built_signal_is_returned_before_anything_renders(world, monkeypatch):
    a = _build(world, "same")
    monkeypatch.setattr(seam_build, "open_brief", _no_render)
    assert _build(world, "same") == a
    with pytest.raises(CoordError, match="operator"):
        _build(world, "same", kind="rotation")                           # the kind mismatch is still refused


def test_a_cold_start_already_built_is_returned_before_anything_renders(pending, monkeypatch):
    pending.launch_claim_spawn(pending.launch_id, os.getpid())
    b = _starter(pending)
    monkeypatch.setattr(seam_build, "open_brief", _no_render)
    assert _starter(pending) == b
    with pytest.raises(LeaseRefused, match="claimed by pid"):           # the preconditions still come first
        _starter(pending, pid=os.getpid() + 1)


def test_a_launch_signal_is_the_starters_alone(world):
    before = _counts(world)
    with pytest.raises(CoordError, match="starter_build"):
        seam_build.build(world, kind="operator", signal_kind="launch", signal_key=world.launch_id,
                         queue_dir=world.qdir, token=_token(world))
    assert _counts(world) == before


def _worker_launch(s):
    from levain.coordinator.store import Part

    d = s.accept_drop("work for a worker", surface="cli")[0]
    (t,) = s.split(d.drop_id, [Part("work", "w")])
    return s.launch_request(_token(s), harness="claude", role="worker", threads=[t.thread_id]).launch_id


def test_a_delivery_names_its_recipient_and_only_that_launch_acknowledges(world):
    other = _worker_launch(world)
    b = _build(world, "to-other")
    assert seam_open.record_delivery(world, b.open_id, "launch_brief", recipient=other) is True
    assert _sql(world, "SELECT recipient_launch_id FROM open_deliveries WHERE open_id = ?", (b.open_id,)) == [(other,)]
    before = _counts(world)
    with pytest.raises(CoordError, match=f"delivered to launch {other}, not to launch {world.launch_id}"):
        _ack(world, b.open_id, deliver=False)
    assert _counts(world) == before
    assert seam_open.record_delivery(world, b.open_id, "launch_brief", recipient=world.launch_id) is True
    assert _ack(world, b.open_id, deliver=False) == (b.open_id, world.session_key, True)
    with sqlite3.connect(world.path) as c:
        assert seam_open.acked_cursor(c) == (b.shown_through_gseq, b.open_id) and b.shown_through_gseq > 0


def test_a_delivery_to_an_unknown_launch_is_refused(world):
    b = _build(world, "nobody")
    before = _counts(world)
    for who in ("ln-" + "0" * 32, None, 7):
        with pytest.raises(CoordError, match="launch"):
            seam_open.record_delivery(world, b.open_id, "light_verb", recipient=who)
    assert _counts(world) == before


def test_a_brief_with_no_omitted_rows_reserves_nothing(world):
    full = _brief(world)
    exact = _brief(world, body_cap=len(full.text.encode("utf-8")))
    assert exact == full and exact.omitted_rows == 0
    with pytest.raises(seam.BriefTooBig):
        _brief(world, body_cap=len(full.text.encode("utf-8")) - len(full.text.splitlines()[-1]) - 400)


def test_a_cut_brief_fits_its_cap_exactly_and_leaves_no_room_for_another_row(world):
    _raw(world, "INSERT INTO gseq (gseq, at) VALUES (1000000000000000000, 't')")    # wide gseqs: a long omitted line
    _counted_rows(world, 60)
    full = _brief(world, body_cap=10 * seam_open.BODY_CAP)
    rows = [ln for ln in full.text.splitlines() if ln.startswith("  gseq ")]
    base = len(full.text.encode("utf-8")) - 1200
    for cap in range(base, base + 40):
        b = _brief(world, body_cap=cap)
        size = len(b.text.encode("utf-8"))
        shown = [ln for ln in b.text.splitlines() if ln.startswith("  gseq ")]
        assert size <= cap and 10 <= b.omitted_rows <= 99, cap
        assert size + len(rows[len(shown)].encode("utf-8")) + 1 > cap, cap   # one row more would not have fitted


def test_a_body_over_the_callers_cap_is_never_stored(world, monkeypatch):
    monkeypatch.setattr(seam_build, "open_brief", lambda *a, **kw: seam.Brief("operator", "x" * 5000, "t", 0, 0, None))
    before = _counts(world)
    with pytest.raises(CoordError, match="over the cap of 4096"):
        _build(world, "capped", body_cap=4096)
    assert _counts(world) == before


def _counted_gseqs(s):
    from levain.coordinator import seam_classes

    c = sqlite3.connect(s.path)
    try:
        present = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out = []
        for t in sorted(seam_classes.COUNTED & present):
            out += [(g, t) for (g,) in c.execute(f"SELECT gseq FROM {t} WHERE gseq IS NOT NULL")]
        return out
    finally:
        c.close()


def _shared(gseqs):
    from collections import Counter

    return sorted(g for g, n in Counter(g for g, _ in gseqs).items() if n > 1)


def test_no_two_counted_rows_share_a_gseq_after_a_mixed_run(world):
    _close(world)
    world.accept_drop("a second drop", surface="cli")
    with world._write("coordinator", _token(world)) as (conn, now):
        seam_open.record_arrival(conn, now, agent="a", mark_id=1, queue_id="q")
    _worker_launch(world)
    b = _build(world, "mixed")
    _ack(world, b.open_id)
    world.coord_take()
    gseqs = _counted_gseqs(world)
    assert len({t for _, t in gseqs}) >= 6 and _shared(gseqs) == []
    # the probe fails on the shape it guards against: a second table's row reusing a gseq
    g = _sql(world, "SELECT gseq FROM drops ORDER BY gseq LIMIT 1")[0][0]
    _raw(world, "INSERT INTO arrival_signals (agent, mark_id, queue_id, ingested_at, gseq) VALUES ('b', 2, 'q', 't', ?)",
         (g,))
    assert _shared(_counted_gseqs(world))


def test_the_delivery_surfaces_are_listed_in_order(world):
    b = _build(world, "both")
    seam_open.record_delivery(world, b.open_id, "light_verb", recipient=world.launch_id)
    seam_open.record_delivery(world, b.open_id, "launch_brief", recipient=world.launch_id)
    other = _worker_launch(world)
    seam_open.record_delivery(world, b.open_id, "light_verb", recipient=other)
    assert f"  open {b.open_id} (operator) built 0m ago, never acknowledged; delivered via launch_brief,light_verb" \
        in _brief(world).text.splitlines()


def test_the_card_renders_its_real_lines_in_the_brief(world):
    QuestionStore(world.qdir).file(agent="claude_code", agent_session_id="s", what="Which?",
                                   options=[("a", "A"), ("b", "B")], rec="a", blocks="x", cwd="/tmp")
    lines = _brief(world).text.splitlines()
    card = lines.index("Card (store parts in the cut, queue parts read at the brief's time):")
    assert lines[card + 1:card + 4] == ["  NEEDS YOU: see Band above", "  away: off", "  undecided (0):"]
    assert "  review queue: 0 open item(s)" in lines and not any(ln.startswith("Card: cannot-derive") for ln in lines)


def test_an_arrival_on_a_store_without_the_open_tables_is_refused(world):
    _raw(world, "DROP TABLE arrival_signals")
    with sqlite3.connect(world.path) as c:
        assert seam_open.open_tables_present(c) is False
    with pytest.raises(CoordError, match="open tables"):
        with world._write("coordinator", _token(world)) as (conn, now):
            seam_open.record_arrival(conn, now, agent="a", mark_id=1, queue_id="q")
