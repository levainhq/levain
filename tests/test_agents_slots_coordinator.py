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
from tests.test_coordinator_launch import (clock, fake, live_hook, qdir, workdir, woken,  # noqa: F401
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
    # the second id is DEFENSIVE: launches are append-only, so a row that is gone cannot occur; the sweep still
    # reads it as over. The real case, an ended launch, is test_the_watch_pass_runs_the_sweep's
    live, dead = woken(store), "ln-ended-and-gone"
    t0 = NOW - timedelta(minutes=10)
    ls.take(store, live, clock=lambda: t0)
    ls.take(store, dead, clock=lambda: t0)
    before = {o: e for _s, _k, o, e in _held(astore)}
    lines = ls.sweep(store, clock=lambda: NOW)
    after = {o: e for _s, _k, o, e in _held(astore)}
    assert set(after) == {live} and after[live] > before[live]
    assert lines == [f"launch {dead}: slot released (the launch is over)"]
    assert ls.sweep(store, clock=lambda: NOW) == []                            # nothing left to change


def test_the_sweep_releases_the_slot_of_a_launch_that_really_ended(store, astore):
    launch_id = woken(store)
    ls.take(store, launch_id)
    assert ls.sweep(store) == [] and len(_held(astore)) == 1                   # pending: renewed, kept
    store.launch_end(launch_id, "failed", "the starter died")
    assert ls.sweep(store) == [f"launch {launch_id}: slot released (the launch is over)"]
    assert _held(astore) == []


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
    assert (f"attention [{ls.ATTENTION_KEY}]: launches run unslotted: unknown agent schema marker "
            "'levain-agent-schema/1': the agent tables in this store are not the ones this levain writes") in err, err
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


# --- review round 1 ----------------------------------------------------------------------------------------------
def test_spawned_workers_take_no_agent_slot_only_the_coordinator_does(store, astore, home, fake, live_hook, workdir,
                                                                       tmp_path):
    """F1: workers are capped by lease.MAX_SPAWNS_LIVE already. With the default cap (2) a coordinator and four
    workers all start, and the table holds the coordinator's slot alone."""
    from levain.agents import instantiate as inst
    from levain.coordinator.store import Part
    from tests.test_agents_spawn import role

    token = store.coord_take()
    for n in range(4):
        wdir = tmp_path / f"worker-{n}"
        wdir.mkdir()
        d, _ = store.accept_drop(f"work {n}", surface="cli")
        (t,) = store.split(d.drop_id, [Part("work", f"w{n}")], by="operator")
        ln = inst.spawn_worker(store, token, thread_id=t.thread_id, cwd=str(wdir.resolve()), agent_store=astore,
                               inline=role(), slots={"topic": "x"})
        got = cl.launch_session(store, ln.launch_id)
        assert got.started, (n, got)
    assert _held(astore) == []                               # four workers, none of them in the table
    store.coord_release()
    first = cl.launch_session(store, woken(store))
    assert first.started, first
    assert [(k, o) for _s, k, o, _e in _held(astore)] == [("launch", first.launch_id)]


def test_a_claim_lost_to_another_pass_does_not_release_the_winners_slot(store, astore, fake, live_hook):
    """F2, L2's repro: pass A takes the slot (fresh), pass B finds it (not fresh) and wins the spawn claim; A's claim
    is refused. A must not release by owner id: B's running launch would lose its slot."""
    launch_id = woken(store)
    real = store.launch_claim_spawn

    def b_wins_first(lid, pid):
        theirs = ls.take(store, lid)                       # pass B: finds A's slot
        assert theirs is not None and not theirs.fresh
        assert real(lid, 999999)[1] is None                # B wins the claim
        return real(lid, pid)                              # A's claim: refused

    store.launch_claim_spawn = b_wins_first
    got = cl.launch_session(store, launch_id)
    assert not got.started and "already being started" in got.refusal, got
    assert [o for _s, _k, o, _e in _held(astore)] == [launch_id]


def test_a_replacement_coordinator_starts_in_the_pass_that_ends_its_predecessor(store, astore, fake, live_hook,
                                                                               clock):
    """F3: the ended coordinator still holds its slot when the replacement starts, and another occupant fills the
    rest: the sweep must run before the start phase or the replacement gets SlotFull for a whole pass."""
    from datetime import timedelta

    _cap(astore, 2)
    ln = cl.launch_session(store, woken(store))
    assert ln.started, ln
    _occupy(astore)
    fake.flag("done")
    clock[0] += timedelta(minutes=2)
    store.accept_drop("a drop the silent coordinator never saw", surface="cli")
    lines = cl.watch_once(store)
    assert any(f"launch {ln.launch_id} ended: " in x for x in lines), lines
    assert len(fake.spawns()) == 2, lines
    assert sorted(k for _s, k, _o, _e in _held(astore)) == ["launch", "occurrence"]


def test_a_transient_fault_on_the_schema_check_is_not_remembered(store, astore, home, fake, live_hook, monkeypatch,
                                                                 capsys):
    """F4: only a refusal or the no-schema answer is kept for the process. A locked database launches unslotted that
    once, says so on stderr, and the next launch checks again and takes its slot."""
    import levain.agents.store as agent_store

    real = agent_store.open_agent_store_view
    calls = []

    def flaky(directory):
        calls.append(directory)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(directory)

    monkeypatch.setattr(agent_store, "open_agent_store_view", flaky)
    first_id = woken(store)
    assert cl.launch_session(store, first_id).started
    assert _held(astore) == []                                               # unslotted, that once
    assert "attention [agent-schema-refused]: launches run unslotted: OperationalError: database is locked" in \
        capsys.readouterr().err
    fake.gone()
    store.launch_end(first_id, "ended", "done")
    second_id = woken(store, "another drop")
    assert cl.launch_session(store, second_id).started
    assert [o for _s, _k, o, _e in _held(astore)] == [second_id] and len(calls) == 2   # asked again, slotted


def test_a_refusal_and_the_no_schema_answer_are_remembered(store, home, fake, live_hook, monkeypatch):
    import levain.agents.store as agent_store

    calls = []
    real = agent_store.open_agent_store_view

    def counting(directory):
        calls.append(1)
        return real(directory)

    monkeypatch.setattr(agent_store, "open_agent_store_view", counting)
    d = home / "autonomic"
    assert ls.slot_state(d) is False and ls.slot_state(d) is False           # no agent store: asked once
    assert len(calls) == 1


# --- review round 1: the faults and the guards ------------------------------------------------------------------
def test_a_locked_slot_table_is_a_slot_fault_and_the_launch_stays_pending(store, astore, fake, live_hook, monkeypatch):
    def locked(conn, *a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(slots, "take", locked)
    launch_id = woken(store)
    with pytest.raises(ls.SlotFault, match="database is locked"):
        ls.take(store, launch_id)
    got = cl.launch_session(store, launch_id)
    assert not got.started and "database is locked" in got.refusal and "left pending" in got.refusal, got
    assert store.launch(launch_id).state == "pending" and fake.spawns() == []


def test_a_schema_that_moved_after_the_check_launches_unslotted_and_releases_nothing(store, astore, monkeypatch):
    from levain.agents.store import AgentStoreRefused

    launch_id = woken(store)
    hold = ls.take(store, launch_id)
    assert hold is not None and ls.slot_state(hold.directory)

    def moved(directory):
        raise AgentStoreRefused("the schema moved")

    monkeypatch.setattr("levain.agents.store.open_agent_store", moved)
    assert ls.take(store, "ln-other") is None                      # _open said None: launches unslotted
    assert ls.release(hold) is False and ls.sweep(store) == []
    monkeypatch.undo()
    assert [o for _s, _k, o, _e in _held(astore)] == [launch_id]   # nothing was touched


def test_the_sweep_keeps_a_slot_whose_state_cannot_be_told(store, astore, monkeypatch):
    a, b = woken(store), "ln-never-recorded"
    t0 = NOW - timedelta(minutes=10)
    ls.take(store, a, clock=lambda: t0)
    ls.take(store, b, clock=lambda: t0)
    before = {o: e for _s, _k, o, e in _held(astore)}

    def probes(directory):
        def over(launch_id):
            if launch_id == a:
                return None                         # no event: cannot tell
            raise OSError("dispatch.db unreadable")
        return over, lambda _l: None

    monkeypatch.setattr("levain.agents.controller.dispatch_probes", probes)
    assert ls.sweep(store, clock=lambda: NOW) == []
    assert {o: e for _s, _k, o, e in _held(astore)} == before      # neither released nor renewed


def test_a_control_c_between_the_take_and_the_claim_releases_the_slot(store, astore, fake, live_hook):
    launch_id = woken(store)

    def interrupted(lid, pid):
        raise KeyboardInterrupt

    store.launch_claim_spawn = interrupted
    with pytest.raises(KeyboardInterrupt):
        cl.launch_session(store, launch_id)
    assert _held(astore) == [] and fake.spawns() == []


def test_a_failing_attention_callback_is_swallowed_and_raised_once(store, astore, home):
    d = home / "autonomic"
    c = sqlite3.connect(astore.path)
    c.execute("UPDATE meta SET value = 'levain-agent-schema/1' WHERE key = 'agent_schema'")
    c.commit()
    c.close()
    seen = []

    def boom(key, text):
        seen.append((key, text))
        raise RuntimeError("no one is listening")

    assert ls.slot_state(d, boom) is False and ls.slot_state(d, boom) is False
    assert len(seen) == 1 and seen[0][0] == ls.ATTENTION_KEY and seen[0][1].startswith("launches run unslotted: ")


def test_a_launch_that_ended_between_passes_frees_its_slot_before_the_next_start(store, astore, fake, live_hook):
    """F3, the top of the pass: C1 ended after the last sweep, the table is full, and a pending launch waits."""
    _cap(astore, 2)
    first_id = woken(store)
    assert cl.launch_session(store, first_id).started
    _occupy(astore)
    fake.gone()
    store.launch_end(first_id, "ended", "done")
    store.accept_drop("the next thing", surface="cli")           # a drop from elsewhere: a pending launch, no starter
    lines = cl.watch_once(store)
    assert any("started: claude attach" in x for x in lines), lines
    assert sorted(k for _s, k, _o, _e in _held(astore)) == ["launch", "occurrence"]
