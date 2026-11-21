"""levain#221: the Calls panel (levain.cockpit.calls_panel), ruling D4 np-05034d15 (a): spawned workers only in 1.0.

Every store is a real desk store (``CoordStore``) and every worker a real ``spawn_worker``: no hand-written launch row.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from levain.agents import instantiate as inst
from levain.cockpit.calls_panel import CALLS_ID, calls_discoverer, read_calls
from levain.cockpit.engine import Cockpit
from levain.desk.store import CoordStore
from tests.test_agents_spawn import a_thread, registry, role  # noqa: F401

CLOSED = {"class": "none", "device_class": None}


@pytest.fixture
def desk(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


def _spawn(store, token, registry, cwd, name="scribe"):  # noqa: F811
    _d, t = a_thread(store)
    return inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(cwd), inline=role(name), slots={"topic": "x"},
                             agent_store=registry)


def _cockpit(directory):
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(calls_discoverer(lambda: directory), retire=True)
    ck.manifest(CLOSED)
    return ck


def test_a_spawned_worker_is_a_call_and_a_seat_launch_is_not(desk, registry, tmp_path):  # noqa: F811
    # behaviour: the spawned worker (a launches row with its spawn record) is one row naming its role and state; a worker
    # launch with no spawn record is a seat, and is not a call
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path, "scribe")
    _d, t = a_thread(store)
    seat = store.launch_request(token, harness="claude", threads=[t.thread_id], cwd=str(tmp_path))
    rows, note = read_calls(store.path.parent)
    assert note is None and [r.id for r in rows] == ["call:" + ln.launch_id]
    assert "call:" + seat.launch_id not in {r.id for r in rows}
    assert rows[0].facets == {"call_state": "pending"} and rows[0].title == "scribe"
    assert rows[0].body.startswith("scribe · ") and " · pending · asked " in rows[0].body


def test_the_panel_groups_the_live_calls_first_and_an_over_call_is_not_a_figure(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    a = _spawn(store, token, registry, tmp_path, "one")
    b = _spawn(store, token, registry, tmp_path, "two")
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, 2, "
                     "(SELECT MAX(gseq) + 1 FROM launch_events), 'failed', 'test', '2026-11-20T00:00:00Z')", (a.launch_id,))
        conn.commit()
    except sqlite3.DatabaseError:
        conn.close()
        raise
    conn.close()
    ck = _cockpit(store.path.parent)
    panel = ck.panel(CALLS_ID)
    groups = {r["id"]: r["group"] for r in panel["rows"]}
    assert groups == {"call:" + a.launch_id: "over", "call:" + b.launch_id: "live"}
    assert [g["id"] for g in panel["groups"]] == ["live", "unknown", "over"] and panel["count"] == 1


def test_no_desk_store_draws_no_panel_and_an_unreadable_one_is_a_fault_never_no_calls(desk, registry, tmp_path):  # noqa: F811
    assert read_calls(None) is None and read_calls(tmp_path / "nowhere") is None
    assert calls_discoverer(None)(None) == [] and calls_discoverer(lambda: tmp_path / "nowhere")(None) == []
    store, token = desk
    _spawn(store, token, registry, tmp_path)
    ck = _cockpit(store.path.parent)
    store.path.write_bytes(b"not a database, and long enough to be read as one" * 40)
    ck.manifest(CLOSED)
    panel = ck.panel(CALLS_ID)
    assert not panel.get("rows") and "cannot be read" in str(panel), panel


def test_a_launch_whose_record_cannot_be_read_is_a_row_that_says_so():
    # the store's own trigger keeps a launch append-only, so the unreadable record is shown through the row builder itself
    from levain.cockpit.calls_panel import _row

    row = _row("l-1", '["t-1"]', "2026-11-20T00:00:00Z", "garbage", "active")
    assert row.title == "unreadable spawn record" and "record unreadable" in row.body and row.facets == {"call_state": "active"}


def test_a_store_with_no_launches_is_an_empty_panel_and_the_newest_calls_are_shown(desk, registry, tmp_path, monkeypatch):  # noqa: F811
    from levain.cockpit import calls_panel

    store, token = desk
    assert read_calls(store.path.parent) == ((), None)
    monkeypatch.setattr(calls_panel, "CALLS_SHOWN", 2)             # the live-spawn cap (4) is lower than the real window
    for _ in range(3):
        _spawn(store, token, registry, tmp_path)
    rows, note = calls_panel.read_calls(store.path.parent)
    assert len(rows) == 2 and note and "2" in note


def test_the_panel_id_is_the_one_the_machine_zones_styling_and_section_head_name():
    # the page's Machine zone names this id in its section head ownership and its CSS order; a rename on one side only is red here
    root = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"
    assert f'[data-ck-panel="{CALLS_ID}"]' in (root / "dashboard_places.js").read_text()
    assert f'[data-ck-panel="{CALLS_ID}"]' in (root / "dashboard.css").read_text()


def test_a_store_that_lacks_a_table_is_a_fault_never_no_calls_and_one_from_before_role_ref_holds_none(tmp_path):
    from levain.cockpit.calls_panel import _SQL  # noqa: F401  (the query is the one under test)

    for ddl in ("CREATE TABLE unrelated (x)", "CREATE TABLE launches (launch_id TEXT, role TEXT)"):   # none; launches without events
        path = tmp_path / "bad" / "dispatch.db"
        path.parent.mkdir(exist_ok=True)
        path.unlink(missing_ok=True)
        c = sqlite3.connect(path)
        c.execute(ddl)
        c.commit()
        c.close()
        with pytest.raises(sqlite3.DatabaseError, match="no launches or no launch events"):
            read_calls(path.parent)
        ck = _cockpit(path.parent)
        assert "cannot be read" in str(ck.panel(CALLS_ID)), ddl
    path.unlink()
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE launches (launch_id TEXT, role TEXT)")
    c.execute("CREATE TABLE launch_events (launch_id TEXT)")                     # a store made before spawned workers existed
    c.commit()
    c.close()
    assert read_calls(path.parent) == ((), None)


def test_a_call_with_no_events_is_state_unknown_and_counted_and_a_live_call_is_never_cut_by_the_window(desk, registry, tmp_path, monkeypatch):  # noqa: F811
    from levain.cockpit import calls_panel

    store, token = desk
    old = _spawn(store, token, registry, tmp_path, "old")
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, 2, "
                     "(SELECT MAX(gseq) + 1 FROM launch_events), 'failed', 'test', '2026-11-20T00:00:00Z')", (old.launch_id,))
        conn.commit()
    finally:
        conn.close()
    live = _spawn(store, token, registry, tmp_path, "live")
    monkeypatch.setattr(calls_panel, "CALLS_SHOWN", 1)
    rows, note = calls_panel.read_calls(store.path.parent)
    assert [r.id for r in rows] == ["call:" + live.launch_id] and note                  # the live one, whichever is newer
    row = calls_panel._row("l-x", '["t"]', "2026-11-20T00:00:00Z", "garbage", None)       # no event at all
    assert row.facets == {"call_state": "unknown"}
    from levain.cockpit.registry import ORDERINGS

    group = ORDERINGS["calls.state"].group
    assert group(row, None) == "unknown" and "unknown" not in ORDERINGS["calls.state"].uncounted


def test_one_bad_record_is_one_row_never_a_fault_of_the_whole_panel(desk, registry, tmp_path):  # noqa: F811
    # behaviour (L3 r1): a nested record, a NULL thread list, a kind the panel does not know and a record whose role was altered
    # without its hash are each ONE row that says so; the good call beside them is still drawn
    import json

    from levain.cockpit.calls_panel import _row

    store, token = desk
    ln = _spawn(store, token, registry, tmp_path, "scribe")
    conn = sqlite3.connect(store.path)
    try:
        good_ref = conn.execute("SELECT role_ref FROM launches WHERE launch_id = ?", (ln.launch_id,)).fetchone()[0]
    finally:
        conn.close()
    forged = json.loads(good_ref)
    forged["role"]["name"] = "admin"                                  # the hash and the spawn name still say "scribe"
    for ref in ("[" * 100_000, json.dumps(forged), None):
        row = _row("l-1", '["t"]', "2026-11-20T00:00:00Z", ref, "active")
        assert row.title == "unreadable spawn record" and "record unreadable" in row.body, (str(ref)[:30], row.body)
    assert _row("l-2", None, "2026-11-20T00:00:00Z", good_ref, "active").title == "scribe"          # a NULL thread list
    assert _row("l-3", '["t"]', "2026-11-20T00:00:00Z", good_ref, "cancelled").facets == {"call_state": "unknown"}
    assert _row("l-4", '["t"]', "2026-11-20T00:00:00Z", good_ref, "ended").facets == {"call_state": "ended"}


def test_a_call_with_no_event_is_cut_after_the_live_ones_and_before_the_over_ones(desk, registry, tmp_path, monkeypatch):  # noqa: F811
    # behaviour (L3 r1): the window's order is the ordering's (live, unknown, over), so a counted unknown call is not cut behind
    # history. A launch with no event cannot be made through the store (its triggers), so the scratch store's own two
    # triggers that keep events are dropped to lose one, which is the damaged store the panel must not read as finished
    from levain.cockpit import calls_panel

    store, token = desk
    lost = _spawn(store, token, registry, tmp_path, "lost")        # the OLDEST: only its rank keeps it ahead of the newer history
    over = _spawn(store, token, registry, tmp_path, "over1")
    live = _spawn(store, token, registry, tmp_path, "live")
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, 2, "
                     "(SELECT MAX(gseq) + 1 FROM launch_events), 'failed', 'test', '2026-11-20T00:00:00Z')", (over.launch_id,))
        for trigger in ("launch_events_frozen", "launch_events_kept"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DELETE FROM launch_events WHERE launch_id = ?", (lost.launch_id,))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(calls_panel, "CALLS_SHOWN", 2)
    rows, note = calls_panel.read_calls(store.path.parent)
    assert [r.id for r in rows] == ["call:" + live.launch_id, "call:" + lost.launch_id] and note, [r.id for r in rows]   # over1 is cut
    assert rows[1].facets == {"call_state": "unknown"}


def test_a_dispatch_directory_that_cannot_be_named_keeps_the_panel_and_its_read_says_so(tmp_path):
    def boom():
        raise OSError("the consent directory cannot be resolved")

    specs = calls_discoverer(boom)(None)
    assert [s.id for s in specs] == [CALLS_ID]                         # not "no desk": the panel stays, so a retire does not drop it
    got = specs[0].read(None)
    assert "cannot be read" in str(got) and "OSError" in str(got)


def test_the_windows_rank_and_the_rows_state_come_from_one_set_of_kinds_pinned_to_the_desks_own():
    # behaviour (L3 r2): each known kind lands in the group the registry's ordering gives it, the set is the desk's own launch
    # kinds (the store's CHECK refuses any other, so a kind the desk adds goes red HERE and is placed on purpose), and the
    # window's rank is built from the same two tuples the row's state reads
    from levain.cockpit import calls_panel
    from levain.cockpit.registry import ORDERINGS
    from levain.desk.lease import LAUNCH_KINDS

    assert set(calls_panel._KNOWN_STATES) == set(LAUNCH_KINDS)
    group = ORDERINGS["calls.state"].group
    for kind in calls_panel._LIVE_KINDS:
        assert group(calls_panel._row("l", "[]", "t", None, kind), None) == "live"
    for kind in calls_panel._OVER_KINDS:
        assert group(calls_panel._row("l", "[]", "t", None, kind), None) == "over"
    for kind in calls_panel._LIVE_KINDS + calls_panel._OVER_KINDS:
        assert f"'{kind}'" in calls_panel._SQL
    assert calls_panel._one_line("abc", 0) == "" and calls_panel._one_line("abc", 1) == "\u2026"
