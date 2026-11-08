"""Row 156 step 2D: ``levain coord watch`` applies queued reports as the ``service`` (facts only). Synthetic items;
scratch stores; no real ``claude``."""
from __future__ import annotations

import sqlite3

import pytest

from levain.coordinator import launch, ready, work_attach
from levain.coordinator.store import CoordError, CoordStore


@pytest.fixture
def board(tmp_path):
    d = tmp_path / "board"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def coord(board):
    store = CoordStore(board)
    token = store.coord_take()
    ready.add(store, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = store.launch_request(token, harness="claude", role="coordinator")
    return store, token, ln.launch_id


def _report(board, rid, **kw):
    base = {"report_id": rid, "item_id": "levain:a", "worker": "wa2d", "worker_kind": "lane",
            "event": "attach", "at": "2026-11-08T12:00:00-05:00"}
    work_attach.enqueue_report(board, **{**base, **kw})


def _rows(board, sql):
    c = sqlite3.connect(board / "dispatch.db")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _none_live(_store):
    return frozenset()


def test_a_queued_attach_is_applied_by_one_pass_and_the_lease_is_given_back(board, coord):
    store, token, launch_id = coord
    store.coord_release()                                  # no coordinator holds the board now
    _report(board, "r-1", launch_id=launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)
    assert [r[0] for r in _rows(board, "SELECT report_id FROM worker_attach")] == ["r-1"]
    assert _rows(board, "SELECT by FROM worker_attach") == [("service",)]
    assert any("applied 1" in ln for ln in lines)
    assert store.coord_lease() is None                      # claimed for the pass, released after it
    assert _rows(board, "SELECT holder, by FROM lease_events WHERE kind = 'claim' ORDER BY seq DESC LIMIT 1") \
        == [("service", "service")]
    assert work_attach.watch_apply(board, sessions=_none_live) == []        # nothing left unapplied: no lease taken
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE holder = 'service'") == [(2,)]


def test_a_live_coordinator_lease_makes_the_pass_skip_and_say_why(board, coord):
    store, token, launch_id = coord                          # the take above is live
    _report(board, "r-1", launch_id=launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)
    assert len(lines) == 1 and "held" in lines[0] and "1 report" in lines[0]
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert store.coord_lease().token == token and store.coord_lease().holder == "operator"
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE holder = 'service'") == [(0,)]


def test_an_unknown_item_is_refused_with_a_visible_record(board, coord):
    store, token, launch_id = coord
    store.coord_release()
    _report(board, "r-bad", item_id="levain:nowhere", launch_id=launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert [r[0] for r in _rows(board, "SELECT report_id FROM report_refusals")] == ["r-bad"]
    assert any("refused 1" in ln for ln in lines)


def test_a_session_listing_that_cannot_be_read_defers_and_still_gives_the_lease_back(board, coord):
    store, token, _ = coord
    store.coord_release()
    _report(board, "r-1", harness_session_id="sess-1")
    lines = work_attach.watch_apply(board, sessions=lambda _s: None)
    assert _rows(board, "SELECT count(*) FROM worker_attach") == [(0,)]
    assert _rows(board, "SELECT count(*) FROM report_refusals") == [(0,)]
    assert any("deferred 1" in ln for ln in lines) and store.coord_lease() is None


def test_the_service_role_refuses_a_deciding_write(board, coord):
    store, token, _ = coord
    svc = CoordStore(board, role="service")
    with pytest.raises(CoordError, match="service records facts only"):
        ready.move(svc, token, "levain:a", to="running", who="service", why="x")
    assert [r[0] for r in _rows(board, "SELECT category FROM ready_items WHERE id = 'levain:a'")] == ["ready"]


def test_watch_once_applies_beside_its_own_work_and_is_unchanged_without_reports(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setattr(launch, "_harness", lambda: (None, {}))
    consent = CoordStore(home / "consent")
    consent.coord_status()                                  # makes the consent store
    before = launch.watch_once(consent)
    assert not (home / "board").exists()                    # nothing queued: no board store is made or opened
    board = home / "board"
    board.mkdir(mode=0o700)
    seed = CoordStore(board)
    token = seed.coord_take()
    ready.add(seed, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = seed.launch_request(token, harness="claude", role="coordinator")
    seed.coord_release()
    _report(board, "r-1", launch_id=ln.launch_id)
    after = launch.watch_once(consent)
    assert after[:len(before)] == before and any("applied 1" in line for line in after[len(before):])
    assert [r[0] for r in _rows(board, "SELECT report_id FROM worker_attach")] == ["r-1"]


# ------------------------------------------------------------------------------------------------ the public service lease
def test_service_claim_refuses_a_live_coordinator_lease_and_hands_back_the_holder(board, coord):
    from levain.coordinator import lease
    store, token, _ = coord
    with store._write("service", None, create=False) as (conn, now):
        got, held = lease.service_claim(conn, now, cause="test")
    assert got is None and held is not None and held.token == token
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE holder = 'service'") == [(0,)]


def test_service_claim_takes_a_free_lease_and_service_release_gives_it_back(board, coord):
    from levain.coordinator import lease
    store, _, _ = coord
    store.coord_release()
    with store._write("service", None, create=False) as (conn, now):
        got, held = lease.service_claim(conn, now, cause="test")
    assert held is None and isinstance(got, int)
    assert store.coord_lease().holder == "service"
    with store._write("service", None, create=False) as (conn, now):
        assert lease.service_release(conn, now, got, cause="test done") is True
    assert store.coord_lease() is None


def test_service_release_does_not_release_a_lease_it_does_not_hold(board, coord):
    from levain.coordinator import lease
    store, token, _ = coord
    with store._write("service", None, create=False) as (conn, now):
        assert lease.service_release(conn, now, token, cause="test") is False
        assert lease.service_release(conn, now, token + 99, cause="test") is False
    assert store.coord_lease().token == token
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE kind = 'release'") == [(0,)]


# ------------------------------------------------------------------------------------------------ L1/L2 round 1 (r156fA)
class _Clock:
    def __init__(self) -> None:
        from datetime import datetime, timezone
        self.now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw: float) -> None:
        from datetime import timedelta
        self.now += timedelta(**kw)


def _running_coordinator(board, clock):
    """A board store whose coordinator launch is active and holds nothing: the operator's take lapsed, the launch
    claimed (lapsing it) and gave the lease back with its own token, so the launch keeps running."""
    from datetime import timedelta
    store = CoordStore(board, clock=clock)
    token = store.coord_take(hold=timedelta(minutes=1))
    ready.add(store, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = store.launch_request(token, harness="claude", role="coordinator")
    assert store.launch_activate(ln.launch_id, harness_session_id="sess-coord")[1] is None
    clock.advance(minutes=2)
    mine = store.coord_claim(ln.launch_id, ln.session_key)
    store.coord_release(mine, by="coordinator")
    assert store.coord_lease() is None
    return store, ln


def _service_claim(board, clock=None):
    from levain.coordinator import lease
    svc = CoordStore(board, role="service", **({"clock": clock} if clock is not None else {}))
    with svc._write("service", None, create=False) as (conn, now):
        token, held = lease.service_claim(conn, now, cause="test")
    assert held is None, held
    return svc, token


def test_f8_the_service_claim_is_short_and_a_coordinator_claim_takes_it(board):
    from levain.coordinator import lease
    clock = _Clock()
    store, ln = _running_coordinator(board, clock)
    svc, stoken = _service_claim(board, clock)
    held = store.coord_lease()
    assert held.holder == "service" and held.until == ready_stamp(clock.now + lease.SERVICE_TTL)
    assert lease.SERVICE_TTL.total_seconds() == 120
    ctoken = store.coord_claim(ln.launch_id, ln.session_key)            # the service yields
    assert store.coord_lease().token == ctoken and store.coord_lease().holder == ln.session_key
    assert _rows(board, "SELECT kind, holder, token, by FROM lease_events WHERE kind = 'release' ORDER BY seq DESC "
                        "LIMIT 1") == [("release", "service", stoken, "coordinator")]
    # the interrupted apply: its token is no longer the lease's, so it applies nothing and the reports stay queued
    _report(board, "r-1", worker=ln.launch_id, launch_id=ln.launch_id)
    with pytest.raises(CoordError, match="not the live lease"):
        work_attach.apply_reports(svc, stoken, live_sessions=frozenset())
    assert _rows(board, "SELECT count(*) FROM worker_attach WHERE report_id IS NOT NULL") == [(0,)]
    assert _rows(board, "SELECT count(*) FROM report_refusals") == [(0,)]
    with svc._write("service", None, create=False) as (conn, now):
        assert lease.service_release(conn, now, stoken, cause="done") is False     # nothing of the coordinator's ended


def test_f9_the_service_never_lapses_another_holders_lease_nor_expires_launches(board):
    from datetime import timedelta
    from levain.coordinator import lease
    clock = _Clock()
    store = CoordStore(board, clock=clock)
    token = store.coord_take(hold=timedelta(minutes=1))
    ready.add(store, token, "levain:a", repo="levain", title="item a", who="desk", why="seed", to="ready")
    ln = store.launch_request(token, harness="claude", role="coordinator")
    clock.advance(minutes=10)                                             # the take and the pending launch are stale
    svc = CoordStore(board, role="service", clock=clock)
    with svc._write("service", None, create=False) as (conn, now):
        got, held = lease.service_claim(conn, now, cause="test")
    assert got is None and held is not None and held.holder == "operator" and not held.live
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE kind = 'lapse'") == [(0,)]
    assert _rows(board, f"SELECT kind FROM launch_events WHERE launch_id = '{ln.launch_id}'") == [("pending",)]
    _report(board, "r-1", worker=ln.launch_id, launch_id=ln.launch_id)
    lines = work_attach.watch_apply(board, sessions=_none_live)          # the watch skips the pass and says why
    assert len(lines) == 1 and "lapsed" in lines[0]
    assert _rows(board, "SELECT count(*) FROM lease_events WHERE holder = 'service'") == [(0,)]
    assert _rows(board, f"SELECT kind FROM launch_events WHERE launch_id = '{ln.launch_id}'") == [("pending",)]


def test_f9_a_stale_service_claim_of_its_own_is_lapsed_as_a_fact_only(board):
    from levain.coordinator import lease
    clock = _Clock()
    store = CoordStore(board, clock=clock)
    token = store.coord_take()
    ln = store.launch_request(token, harness="claude", role="coordinator")
    store.coord_release(token, by="coordinator")                         # a holder release: the launch stays pending
    _svc, stoken = _service_claim(board, clock)                            # then the pass is killed: never released
    clock.advance(minutes=10)                                              # past the service's until and the launch's
    svc = CoordStore(board, role="service", clock=clock)
    with svc._write("service", None, create=False) as (conn, now):
        got, held = lease.service_claim(conn, now, cause="test")
    assert held is None and isinstance(got, int) and got != stoken
    assert _rows(board, "SELECT kind, holder, token FROM lease_events WHERE kind = 'lapse'") == [
        ("lapse", "service", stoken)]
    assert _rows(board, f"SELECT kind FROM launch_events WHERE launch_id = '{ln.launch_id}'") == [("pending",)]


def test_f10_a_service_claim_is_not_a_coordinator_to_the_wake_rule(board):
    from levain.coordinator import lease
    store = CoordStore(board)
    store.coord_set_agent("claude", cwd=str(board.parent))
    _service_claim(board)
    with store._write() as (conn, now):
        state, _why = lease.wake_state(conn, now)
    assert state == "wake"


def ready_stamp(when):
    from levain.cockpit.consent import utc_stamp
    return utc_stamp(when)
