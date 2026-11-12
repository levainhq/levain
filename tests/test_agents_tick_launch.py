"""D1-Q (board row 1689): the SCHEDULED dream launches.

Before this the tick CLAIMED a scheduled slot into an ``admitted`` row and nothing started it: ``controller.launch`` and
``controller.spawn_fire`` had no production caller. The tick now launches (``controller.launch``: the signed run ceiling
and the dream's admission hook, with the arguments ``levain dream start`` passes) and spawns the fire
(``controller.spawn_fire``) of the dream slots it holds ``admitted``; a row it cannot launch is retried each tick until its
grace, when the controller ends it (tests/test_agents_close_unlaunched.py), so a fire that dies leaves a row the next slot
retries. Each case was written red first.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.agents import controller as ctl
from levain.agents import schedule, slots
from levain.agents import tick_driver as T
from levain.dream import agent as da
from levain.dream import start as st
from tests.test_agents_launch import BINDING
from tests.test_dream_start import ZONE, _live_rig

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

UTC = timezone.utc
MIN = timedelta(minutes=1)


def _rows(rig):
    with rig.store.read() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM agent_occurrences ORDER BY admitted_at, key")]


def _claim(rig, key, *, at, kind="scheduled", window="d:2026-11-12"):
    with rig.store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=rig.agent, kind=kind, key=key, spec_hash=rig.spec_hash,
                                 binding_id=BINDING, window_key=window, now=at)
    assert outcome == ctl.CLAIMED
    return oid



# ----------------------------------------------------------------------------- (2) the tick launches what it claims

class Spawns:
    """The fire's spawn seam: records ``(store dir, occurrence id)`` and starts nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, directory, oid):
        self.calls.append((Path(directory), oid))


@pytest.fixture
def hook(monkeypatch):
    """The dream's own admission hook replaced by one that says "input present" (it is tested in test_dream_agent and by
    the scratch run). It is still reached the way production reaches it, through ``da.install_on_input``."""
    seen = []

    def install_on_input(agent_dir, *, clock=None):
        seen.append((Path(agent_dir), clock))
        return lambda row: True

    monkeypatch.setattr(da, "install_on_input", install_on_input)
    return seen


def _boundary(rig) -> datetime:
    """The first */30 slot at least 10 minutes after the rig's clock: a tick before it settles the cursor, a tick a
    minute after it claims it."""
    t = rig.now().astimezone(UTC).replace(second=0, microsecond=0)
    t += timedelta(minutes=10)
    return t + timedelta(minutes=(30 - t.minute % 30) % 30)


def _tick_run(rig, at, spawn, **kw):
    return T.run_tick(rig.store, clock=lambda: at, authority=rig.authority, spawn=spawn, **kw)


def _claim_the_slot(rig, spawn, slot):
    assert _tick_run(rig, slot - 10 * MIN, spawn) == T.EXIT_OK
    return _tick_run(rig, slot + MIN, spawn)


def test_the_tick_launches_and_spawns_the_dream_slot_it_claims(tmp_path, hook):
    rig = _live_rig(tmp_path)
    spawn, slot = Spawns(), _boundary(rig)
    code = _claim_the_slot(rig, spawn, slot)
    (row,) = _rows(rig)
    assert row["kind"] == "scheduled" and row["key"] == schedule.slot_key(slot)
    assert row["state"] == "launched", row
    assert row["launched_at"] and row["link_id"] and row["launch_epoch"] is not None
    assert spawn.calls == [(Path(rig.store.directory), row["occurrence_id"])]
    with rig.store.read() as c:
        assert c.execute("SELECT state FROM agent_reservations WHERE occurrence_id = ?",
                         (row["occurrence_id"],)).fetchone() == ("open",)     # the signed ceiling's reservation was taken
    assert code in (T.EXIT_OK, T.EXIT_NOTICES), code
    assert hook and hook[0][0] == Path(rig.store.directory)


def test_the_tick_passes_the_launch_what_the_hand_start_passes(tmp_path, hook, monkeypatch):
    rig = _live_rig(tmp_path)
    seen = []
    real = ctl.launch

    def spy(store, oid, **kw):
        seen.append(kw)
        return real(store, oid, **kw)

    monkeypatch.setattr(ctl, "launch", spy)
    slot = _boundary(rig)
    _claim_the_slot(rig, Spawns(), slot)
    (kw,) = seen
    # 1111-38 fix round: a scheduled row's span is the window its own key names (this rig's schedule has no window, so
    # its key is the slot's local day), not the hand start's window_for(now)
    (row,) = _rows(rig)
    lo, hi = schedule.window_span({"window": None}, ZONE, row["window_key"])
    assert kw["window"] == ctl.Window(ctl._stamp(lo), ctl._stamp(hi), None)
    assert kw["now"] == slot + MIN
    assert kw["authority"] is rig.authority
    assert callable(kw["on_input"])                                  # the dream's hook, PASSED, never the default
    assert kw["executor"].max_exposure_usd_micros > 0 and kw["launch_over"] and kw["session_alive"]


def test_a_launched_slot_is_not_launched_or_spawned_again_by_the_next_tick(tmp_path, hook):
    rig = _live_rig(tmp_path)
    spawn, slot = Spawns(), _boundary(rig)
    _claim_the_slot(rig, spawn, slot)
    # 1111-38 fix round: the seam starts no fire, and a row no fire claims is now spawned again past SESSION_GRACE_S
    # (tests/test_agents_dq_fix.py); here its fire claims it, as a real one does within seconds
    (row,) = _rows(rig)
    with rig.store.write() as c:
        assert ctl.claim_fire(c, row["occurrence_id"], 4242, slot + MIN) is not None
    _tick_run(rig, slot + 2 * MIN, spawn)
    _tick_run(rig, slot + 3 * MIN, spawn)
    assert len(spawn.calls) == 1 and len(_rows(rig)) == 1


def test_the_signed_run_ceiling_binds_the_scheduled_launch_and_no_fire_is_spawned(tmp_path, hook):
    rig = _live_rig(tmp_path, run_ceiling=1)
    spawn, slot = Spawns(), _boundary(rig)
    earlier = _claim(rig, "earlier", kind="manual", at=slot - 20 * MIN, window=st.manual_window_key(slot, ZONE))
    res = ctl.launch(rig.store, earlier, authority=rig.authority, window=st.window_for(slot, ZONE), now=slot,
                     executor=_exec(rig, earlier), on_input=None)
    assert res.outcome == "launched"
    _finish(rig, earlier)
    _claim_the_slot(rig, spawn, slot)
    row = next(r for r in _rows(rig) if r["kind"] == "scheduled")
    assert (row["state"], row["reason"]) == ("skipped-budget", "ceiling"), row
    assert spawn.calls == []


def test_a_full_cap_leaves_the_row_admitted_and_the_next_tick_launches_it(tmp_path, hook):
    rig = _live_rig(tmp_path)
    spawn, slot = Spawns(), _boundary(rig)
    assert _tick_run(rig, slot - 10 * MIN, spawn) == T.EXIT_OK
    with rig.store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-a", slot)
        slots.take(c, slots.LAUNCH, "ln-b", slot)
    _tick_run(rig, slot + MIN, spawn)
    (row,) = _rows(rig)
    assert row["state"] == "admitted" and spawn.calls == []         # the same semantics the controller documents
    with rig.store.write() as c:
        slots.release(c, "ln-a")
    _tick_run(rig, slot + 5 * MIN, spawn)
    (row,) = _rows(rig)
    assert row["state"] == "launched" and len(spawn.calls) == 1


def test_a_fire_that_cannot_be_spawned_leaves_a_launched_row_and_is_not_a_clean_exit(tmp_path, hook, capsys):
    rig = _live_rig(tmp_path)
    slot = _boundary(rig)

    def boom(directory, oid):
        raise OSError("no fork")

    code = _claim_the_slot(rig, boom, slot)
    (row,) = _rows(rig)
    assert row["state"] == "launched"
    assert code == T.EXIT_LAUNCH
    err = capsys.readouterr().err
    assert "no fork" in err and row["occurrence_id"] in err and "lapses to takeover" in err


def test_an_input_hook_that_says_no_input_ends_the_row_and_spawns_nothing(tmp_path, monkeypatch):
    rig = _live_rig(tmp_path)
    monkeypatch.setattr(da, "install_on_input", lambda agent_dir, *, clock=None: (lambda row: False))
    spawn, slot = Spawns(), _boundary(rig)
    _claim_the_slot(rig, spawn, slot)
    (row,) = _rows(rig)
    assert (row["state"], row["reason"]) == ("skipped-no-input", "no_input") and spawn.calls == []


def test_a_manual_row_is_the_hand_starts_and_the_tick_never_launches_it(tmp_path, hook):
    rig = _live_rig(tmp_path)
    spawn, slot = Spawns(), _boundary(rig)
    assert _tick_run(rig, slot - 10 * MIN, spawn) == T.EXIT_OK
    _claim(rig, "hand-1", kind="manual", at=slot - MIN, window=st.manual_window_key(slot, ZONE))
    _tick_run(rig, slot + MIN, spawn)
    manual = [r for r in _rows(rig) if r["kind"] == "manual"]
    assert [r["state"] for r in manual] == ["admitted"] and spawn.calls == []


# ------------------------------------------------- (3) a fire that dies leaves a row the next slot can retry

def test_a_fire_that_dies_mid_run_leaves_a_row_the_next_slot_retries(tmp_path, hook):
    rig = _live_rig(tmp_path)
    spawn, slot = Spawns(), _boundary(rig)
    _claim_the_slot(rig, spawn, slot)
    (first,) = _rows(rig)
    assert first["state"] == "launched"
    # the fire was spawned and died: nothing claimed the row's pid and nothing renews its lease; the lease lapses
    lapsed = slot + MIN + timedelta(seconds=ctl.LEASE_S) + MIN
    with rig.store.write() as c:
        assert ctl.takeover(c, first["occurrence_id"], lapsed)
    retry = slot + timedelta(minutes=30)
    _tick_run(rig, retry + MIN, spawn)
    rows = {r["key"]: r for r in _rows(rig)}
    assert rows[schedule.slot_key(slot)]["state"] == "stopped"
    assert rows[schedule.slot_key(slot)]["reason"] == "crash_recovered"
    again = rows[schedule.slot_key(retry)]
    assert again["state"] == "launched" and again["kind"] == "scheduled", again
    assert len(spawn.calls) == 2


def test_a_row_the_tick_could_not_launch_does_not_block_a_hand_start_once_its_grace_is_gone(tmp_path, monkeypatch):
    rig = _live_rig(tmp_path)

    def dead(agent_dir, *, clock=None):
        def hook(row):
            raise RuntimeError("the wrap cannot be read")
        return hook

    monkeypatch.setattr(da, "install_on_input", dead)
    spawn, slot = Spawns(), _boundary(rig)
    _claim_the_slot(rig, spawn, slot)
    (row,) = _rows(rig)
    assert row["state"] == "admitted"                                # dead input inside the grace: retried
    refused = st.start(rig.store, clock=rig.now, authority=rig.authority, spawn=lambda d, o: None)
    assert refused.outcome == "refused" and "still in flight" in refused.text
    _tick_run(rig, slot + schedule.GRACE + 2 * MIN, spawn)
    (row,) = [r for r in _rows(rig) if r["kind"] == "scheduled"]
    assert row["state"] == "missed" and row["reason"] in (ctl.NEVER_LAUNCHED, ctl.INPUT_UNREADABLE), row
    monkeypatch.undo()
    started = st.start(rig.store, clock=rig.now, authority=rig.authority, spawn=lambda d, o: None)
    assert started.outcome == "launched", started


# ------------------------------------------------------------------------------------------------------- helpers

def _exec(rig, oid):
    from levain.dream import executor as ex

    return ex.DreamExecutor(agent_store_dir=rig.store.directory, occurrence_id=oid,
                            occ_dir=rig.db.parent / "dream" / oid, model="claude-opus-5-5", facts_path=rig.facts)


def _finish(rig, oid):
    with rig.store.write() as c:
        c.execute("UPDATE agent_occurrences SET state = 'done', reason = 'completed', ended_at = 'x'"
                  " WHERE occurrence_id = ?", (oid,))
        slots.release(c, oid)
        c.execute("UPDATE agent_reservations SET state = 'settled', debited_usd_micros = 0 WHERE occurrence_id = ?",
                  (oid,))
