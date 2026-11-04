"""P6-3b: coordinator launches share the agent slot table (A0 §2.4). Refusals first. The coordinator store sits at
``$LEVAIN_HOME/coordinator`` and the agent store at ``$LEVAIN_HOME/autonomic``, the pair an install has; every other
coordinator directory launches unslotted (``launch_slots.agent_dir``)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.agents import slots
from levain.agents.store import open_agent_store
from levain.coordinator import launch as cl
from levain.coordinator import launch_slots as ls
from levain.coordinator.store import CoordStore
# the fake `claude` and the launch fixtures are test_coordinator_launch's own
from tests.test_coordinator_launch import (fake, live_hook, qdir, workdir, woken,  # noqa: F401
                                           _levain_home)

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_answers():
    ls.reset()
    yield
    ls.reset()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "levain-home"


@pytest.fixture
def store(home, workdir):
    """The install's own coordinator store (slotted when an agent store sits beside it)."""
    (home / "coordinator").mkdir(parents=True, mode=0o700)
    s = CoordStore(home / "coordinator")
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    return s


@pytest.fixture
def astore(home):
    return open_agent_store(home / "autonomic")


def _held(astore):
    with astore.read() as conn:
        return slots.held(conn)


def _cap(astore, n):
    with astore.write() as conn:
        conn.execute("INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)", (slots.MAX_SLOTS_KEY, str(n)))


def _occupy(astore, owner="occ-1"):
    with astore.write() as conn:
        assert slots.take(conn, slots.OCCURRENCE, owner, NOW) is not None


# --- refusals ----------------------------------------------------------------------------------------------------
def test_a_launch_over_the_cap_stays_pending_and_takes_and_releases_nothing(store, astore, fake, live_hook):
    _cap(astore, 1)
    _occupy(astore)
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "slots are taken" in got.refusal and "left pending" in got.refusal, got
    assert store.launch(launch_id).state == "pending"
    assert fake.spawns() == []
    assert [(k, o) for _s, k, o, _e in _held(astore)] == [("occurrence", "occ-1")]   # its own is untouched


def test_a_launch_that_does_not_start_releases_the_slot_it_took(store, astore, fake, live_hook):
    _occupy(astore)
    fake.mode("trust")
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and store.launch(launch_id).state == "failed"
    assert [(k, o) for _s, k, o, _e in _held(astore)] == [("occurrence", "occ-1")]


def test_a_pass_never_releases_a_slot_it_did_not_take(store, astore, fake, live_hook):
    """Another pass holds the launch's slot and its spawn claim: this pass's take finds the slot (not fresh), its claim
    is refused, and the other pass's slot is still there."""
    launch_id = woken(store)
    other = ls.take(store, launch_id)
    assert other is not None and other.fresh
    assert store.launch_claim_spawn(launch_id, 999999)[1] is None
    got = cl.launch_session(store, launch_id)
    assert not got.started
    assert [o for _s, _k, o, _e in _held(astore)] == [launch_id]


def test_a_started_launch_keeps_one_slot_and_a_second_take_adds_none(store, astore, fake, live_hook):
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert got.started, got
    (row,) = _held(astore)
    assert row[1:3] == ("launch", launch_id) and row[3] is not None          # a lease, not an occurrence slot
    again = ls.take(store, launch_id)
    assert again is not None and not again.fresh and again.slot == row[0]
    assert len(_held(astore)) == 1


def test_a_crash_after_the_take_is_reclaimed_by_the_agent_path_reconciler(store, astore, fake, live_hook):
    """A process took launch A's slot and died. Its lease expired and A is over: the next launch's take reclaims it
    through slots.probe_over over dispatch.db, the same probes the agent path is given."""
    _cap(astore, 1)
    a = woken(store)
    long_ago = NOW - timedelta(hours=2)
    assert ls.take(store, a, clock=lambda: long_ago).fresh
    store.launch_end(a, "failed", "the starter died")
    b = woken(store, "another drop")
    got = cl.launch_session(store, b)
    assert got.started, got
    assert [o for _s, _k, o, _e in _held(astore)] == [b]


def test_an_expired_lease_of_a_launch_still_pending_is_not_reclaimed(store, astore):
    """Expired is not enough: the launch is not over and its session is not known dead, so the slot is kept."""
    _cap(astore, 1)
    a = woken(store)
    assert ls.take(store, a, clock=lambda: NOW - timedelta(hours=2)).fresh
    with pytest.raises(ls.SlotFull):
        ls.take(store, "ln-someone-else")
    assert [o for _s, _k, o, _e in _held(astore)] == [a]


# --- the watcher's sweep -----------------------------------------------------------------------------------------
def test_the_sweep_releases_an_over_launch_and_renews_a_live_one(store, astore, fake, live_hook):
    live, dead = woken(store), "ln-ended-and-gone"      # a launch row that is gone reads as over
    t0 = NOW - timedelta(minutes=10)
    ls.take(store, live, clock=lambda: t0)
    ls.take(store, dead, clock=lambda: t0)
    before = {o: e for _s, _k, o, e in _held(astore)}
    lines = ls.sweep(store, clock=lambda: NOW)
    after = {o: e for _s, _k, o, e in _held(astore)}
    assert set(after) == {live} and after[live] > before[live]
    assert lines == [f"launch {dead}: slot released (the launch is over)"]
    assert ls.sweep(store, clock=lambda: NOW) == []                            # nothing left to change


def test_the_watch_pass_runs_the_sweep(store, astore, fake, live_hook):
    dead = woken(store)
    ls.take(store, dead)
    store.launch_end(dead, "failed", "gone")
    lines = cl.watch_once(store)
    assert f"launch {dead}: slot released (the launch is over)" in lines
    assert _held(astore) == []


# --- the schema fallback -----------------------------------------------------------------------------------------
def test_no_agent_store_launches_unslotted_silently_and_creates_nothing(store, home, fake, live_hook, capsys):
    got = cl.launch_session(store, woken(store))
    assert got.started, got
    assert not (home / "autonomic" / "autonomic.db").exists()
    assert capsys.readouterr().err == ""


def test_a_v3_store_launches_unslotted_with_no_attention_row(store, home, fake, live_hook, capsys):
    d = home / "autonomic"
    d.mkdir(parents=True, mode=0o700)
    c = sqlite3.connect(d / "autonomic.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO meta VALUES ('format', 'levain-autonomic/3')")
    c.commit()
    c.close()
    got = cl.launch_session(store, woken(store))
    assert got.started, got
    assert capsys.readouterr().err == ""


def test_a_refused_schema_launches_unslotted_with_exactly_one_attention_row(store, astore, home, fake, live_hook,
                                                                            capsys):
    c = sqlite3.connect(astore.path)
    c.execute("UPDATE meta SET value = 'levain-agent-schema/1' WHERE key = 'agent_schema'")
    c.commit()
    c.close()
    first_id = woken(store)
    first = cl.launch_session(store, first_id)
    assert first.started, first
    fake.gone()
    store.launch_end(first_id, "ended", "done")
    second = cl.launch_session(store, woken(store, "another drop"))
    assert second.started, second
    err = capsys.readouterr().err
    assert err.count(f"attention [{ls.ATTENTION_KEY}]") == 1, err
    assert "agent_schema" in err or "marker" in err
    c = sqlite3.connect(astore.path)
    try:
        assert c.execute("SELECT count(*) FROM agent_slots").fetchone()[0] == 0   # a table it did not trust, untouched
    finally:
        c.close()


def test_a_coordinator_directory_other_than_the_installs_is_never_slotted(qdir, astore, workdir, fake, live_hook):
    other = CoordStore(qdir)
    other.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    got = cl.launch_session(other, woken(other))
    assert got.started, got
    assert _held(astore) == []


def test_a_garbled_cap_refuses_and_leaves_the_launch_pending(store, astore, fake, live_hook):
    with astore.write() as conn:
        conn.execute("INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, 'many')", (slots.MAX_SLOTS_KEY,))
    launch_id = woken(store)
    got = cl.launch_session(store, launch_id)
    assert not got.started and store.launch(launch_id).state == "pending" and fake.spawns() == []
    assert _held(astore) == []
