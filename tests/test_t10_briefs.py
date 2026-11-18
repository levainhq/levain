"""T10 (levain#102): the worker brief for an item, the launch's recorded close protocol and durable key, the close
predicate by protocol, and the desk brief's board section. Scratch stores and scratch git repos only; no real ``claude``."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.agents import instantiate as inst
from levain.desk import launch as cl
from levain.desk import lease, seat_close
from levain.desk.store import CoordError, CoordStore
from tests.test_agents_spawn import a_thread, registry, role  # noqa: F401
from tests.test_board_registry import (  # noqa: F401
    A, bare_store, begin, git, home, mkrepo, ok, run, store as board_store_dir, track_ready)


@pytest.fixture
def desk(tmp_path):
    store = CoordStore(tmp_path / "consent")
    return store, store.coord_take()


def _spawn(store, token, registry, cwd, **kw):
    _d, t = a_thread(store)
    return inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(cwd), inline=role(), slots={"topic": "x"},
                             agent_store=registry, **kw)


# --- 1: the worker brief for an item -----------------------------------------------------------------------------------
def test_an_attached_launch_brief_carries_the_five_rules_and_an_unattached_one_none(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path, item="levain#7")
    text = cl._brief_text(store, ln)
    assert "## Your board item" in text
    assert "board context levain#7` before anything else" in text and "carries no copy of it" in text
    assert "`Board-Item: levain#7`" in text and "`Board-Closes: levain#7`" in text
    assert f"`Board-Seat: {ln.session_key}`" in text and "`repo:levain` and `item:levain#7`" in text
    assert "recorded as `legacy`" in text
    plain = cl._brief_text(store, _spawn(store, token, registry, tmp_path))
    assert "Board-Item" not in plain and "Your board item" not in plain and "board context" not in plain


# --- 2: the launch record's protocol and durable key -----------------------------------------------------------------
def test_a_launch_with_no_registry_is_legacy_and_writes_no_protocol_row(desk, registry, home, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path)                    # no pointer, no registry, not a repo
    assert ln.protocol == "legacy" and ln.migration is None and ln.durable_key == ln.session_key
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT COUNT(*) FROM launch_protocol").fetchone()[0] == 0
    conn.close()


def test_every_mode_derives_its_protocol_and_the_lock_refuses_the_rest(desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    a = mkrepo("alpha")
    ok(capsys, "register", "alpha", "--path", str(a), "--words", "w", *A)
    assert _spawn(store, token, registry, a).protocol == "legacy"                     # untracked
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")
    m = [json.loads(x) for x in (board_store_dir / "repos.jsonl").read_text().splitlines()
         if '"begin-track"' in x][-1]["migration"]
    with pytest.raises(inst.SpawnRefused, match="locked to new work"):                # tracking, no migration id
        _spawn(store, token, registry, a)
    with pytest.raises(inst.SpawnRefused, match="locked to new work"):                # tracking, another id
        _spawn(store, token, registry, a, migration="not-this-one")
    ln = _spawn(store, token, registry, a, migration=m, item="alpha#1")               # the ceremony's own seat
    assert (ln.protocol, ln.migration) == ("ceremony", m) and ln.board_items == ("alpha#1",)
    assert "recorded as `ceremony`" in cl._brief_text(store, ln)
    ok(capsys, "abandon-track", "alpha", "--words", "no", *A)
    with pytest.raises(inst.SpawnRefused, match="locked to new work"):                # aborting
        _spawn(store, token, registry, a)
    b = mkrepo("beta")
    track_ready(capsys, tmp_path, board_store_dir, b, "beta")
    ok(capsys, "track", "beta", "--words", "yes", *A)
    ln = _spawn(store, token, registry, b, item="beta#1", migration="ignored-once-tracked")      # tracked
    assert (ln.protocol, ln.migration) == ("tracked", None)
    assert "recorded as `tracked`" in cl._brief_text(store, ln)
    assert store.launch(ln.launch_id).protocol == "tracked"                           # read back from the record


def test_a_refused_launch_leaves_no_launch(desk, registry, mkrepo, board_store_dir, tmp_path, capsys):  # noqa: F811
    store, token = desk
    a = mkrepo("alpha")
    track_ready(capsys, tmp_path, board_store_dir, a, "alpha")
    conn = sqlite3.connect(store.path)
    before = conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    with pytest.raises(CoordError, match="locked to new work"):
        store.launch_request(token, harness="claude", role="worker", threads=[], cwd=str(a))
    assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == before
    conn.close()


def test_a_record_made_before_the_protocol_table_reads_legacy(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path, item="levain#7")
    conn = sqlite3.connect(store.path)
    conn.execute("DROP TRIGGER IF EXISTS launch_protocol_no_replace")
    conn.executescript("DROP TABLE IF EXISTS launch_protocol")
    conn.commit()
    conn.close()
    again = CoordStore(store.directory).launch(ln.launch_id)       # a reader's open does not recreate the table
    assert again.protocol == "legacy" and again.migration is None
    ro = CoordStore(store.directory)
    c = ro.open_readonly()
    try:
        assert lease.launch(c, ln.launch_id).protocol == "legacy"
    finally:
        c.close()


def test_a_protocol_is_taken_once_and_legacy_is_never_written(desk, registry, tmp_path):  # noqa: F811
    store, token = desk
    ln = _spawn(store, token, registry, tmp_path)
    conn = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="close protocol"):
        for _ in range(2):
            conn.execute("INSERT INTO launch_protocol (launch_id, gseq, protocol, migration, at) VALUES (?, ?, 'tracked', NULL, 't')",
                         (ln.launch_id, 9990 + _))
    conn.close()
    c2 = sqlite3.connect(store.path)
    with pytest.raises(Exception, match="tracked or ceremony"):
        lease.record_protocol(c2, "t", ln.launch_id, "legacy")
    c2.close()


# --- 3: the close predicate by protocol ---------------------------------------------------------------------------------
KEY, T0 = "s-abc", "2026-11-17T10:00:00Z"


def _ep(item, *, source=KEY, at="2026-11-17T11:00:00Z"):
    return {"source": source, "at": at, "tags": ["repo:levain", f"item:{item}"]}


def _close(protocol, **kw):
    base = dict(key=KEY, launched_at=T0, episodes=[], closed_items=[], old_close_ok=False, repo_tracked_now=False)
    base.update(kw)
    return seat_close.check_close(protocol, ["levain#1"], **base)


def test_the_tracked_close_wants_an_episode_and_a_trailer_per_item():
    assert not _close("tracked").ok
    both = _close("tracked", episodes=[_ep("levain#1")], closed_items=["levain#1"])
    assert both.ok and both.via == "tracked" and both.missing == ()
    only_ep = _close("tracked", episodes=[_ep("levain#1")])
    assert not only_ep.ok and len(only_ep.missing) == 1 and "Board-Closes" in only_ep.missing[0]
    only_trailer = _close("tracked", closed_items=["levain#1"])
    assert not only_trailer.ok and "no episode" in only_trailer.missing[0]


@pytest.mark.parametrize("ep", [_ep("levain#1", source="another"), _ep("levain#1", at="2026-11-17T09:00:00Z"),
                                _ep("levain#2"), {"source": KEY, "at": "2026-11-17T11:00:00Z", "tags": []}])
def test_an_episode_of_another_source_time_or_item_does_not_count(ep):
    assert not _close("tracked", episodes=[ep], closed_items=["levain#1"]).ok


def test_an_episode_may_carry_its_tags_in_metadata():
    ep = {"source": KEY, "at": "2026-11-17T11:00:00Z", "metadata": {"tags": ["item:levain#1"]}}
    assert _close("tracked", episodes=[ep], closed_items=["levain#1"]).ok


def test_a_legacy_seat_closes_the_old_way_and_a_tracked_repo_accepts_either():
    assert _close("legacy", old_close_ok=True).ok and _close("ceremony", old_close_ok=True).via == "old"
    assert not _close("legacy").ok and not _close("legacy", repo_tracked_now=False, closed_items=["levain#1"]).ok
    became = _close("legacy", repo_tracked_now=True, episodes=[_ep("levain#1")], closed_items=["levain#1"])
    assert became.ok and became.via == "tracked"                       # its pickup file is a stub: the tracked close holds
    assert not _close("legacy", repo_tracked_now=True, episodes=[_ep("levain#1")]).ok
    assert _close("legacy", repo_tracked_now=True, old_close_ok=True).via == "old"
    assert not _close("tracked", old_close_ok=True).ok                 # a tracked seat is never judged by the old file


# --- 4: the desk brief's section ---------------------------------------------------------------------------------------
def test_the_desk_brief_teaches_the_two_modes_and_the_ceremony_in_order(desk):
    store, _ = desk
    text = "\n".join(cl._board_modes_section())
    assert "## The work board: two modes" in text and "untracked or tracked" in text
    order = ["\n1. If this install has no item store", "\n2. Read its existing memory", "\n3. Draft ONE review page",
             "\n4. He answers in conversation", "\n5. Execute, in this order: `board begin-track`", "`board add --key M:K`",
             "`charter check` passes there", "`board track`", "pre-track-<date>/", "`board abandon-track`", "`board abandon-done`",
             "leaving tracked mode is `untrack`", "\n6. Write one `decision` episode"]
    at = [text.index(x) for x in order]
    assert at == sorted(at), dict(zip(order, at))
    assert "--migration M" in text and "Board-Seat" in text
