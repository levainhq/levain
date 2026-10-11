"""K2b-3a: the episode tombstone through the broker, over a real anneal episodic store (K2b design
§4.0, §4.1.2's anneal mapping; flow manifest design §4.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import sqlite3

import pytest
from anneal_memory import Store

from levain.cockpit.anneal_effects import EPISODES, EpisodeEffectStore, tomb_resource_key, tombstone_effects
from levain.cockpit.broker import Broker
from levain.cockpit.effects import (
    ApplyOutcome,
    ApplyUnit,
    Effect,
    PermanentApplyRefusal,
    SporeEffectStore,
    StoreGone,
    TransientApplyFault,
    plan_units,
)
from tests.test_cockpit_k2b_broker import BROWSER, Key, _label, _refused, _rows, home, keydir, spores  # noqa: F401

@pytest.fixture
def db(home: Path) -> Path:
    p = home / "memory.db"
    with Store(str(p)) as st:
        st.record("the episode to tombstone", "observation", origin_key="ok.target")
        st.record("a neighbour that stays", "observation", origin_key="ok.other")
    return p

@pytest.fixture
def broker(home: Path, spores: Path, db: Path) -> Iterator[Broker]:
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores), EPISODES: EpisodeEffectStore(db)},
               store_paths=[spores, db], install_class="reported")
    b.start()
    try:
        yield b
    finally:
        b.stop()

def _episode(db: Path, ok: str) -> Any:
    with Store(str(db), read_only=True) as st:
        return st.get_by_origin_key(ok)

def _target(db: Path, ok: str = "ok.target") -> dict[str, Any]:
    ep = _episode(db, ok)
    got = EpisodeEffectStore(db).read(ok)
    assert ep is not None and got is not None
    return {"kind": "row", "panel_id": "episodes", "row_id": f"episode:{ep.id}", "row_version": got.rendered}

def _tombstone(b: Broker, db: Path, **kw: Any) -> dict[str, Any]:
    kw.setdefault("tier", "T2")
    kw.setdefault("credential", BROWSER)
    return b.issue(verb="episode_tombstone", params={}, target=_target(db), **kw)

def test_a_reported_tombstone_deletes_the_episode_and_labels_its_tomb(broker, db):
    ep = _episode(db, "ok.target")
    out = _tombstone(broker, db, intent="reported")
    assert (out["state"], out["decision_kind"]) == ("COMMITTED", "ALLOWED_REPORTED")
    assert _episode(db, "ok.target") is None and _episode(db, "ok.other") is not None
    with Store(str(db), read_only=True) as st:
        status = st.origin_key_status("ok.target")
    hexpart = out["pending_id"][2:]
    assert (status.state, status.effect_id) == ("retired", f"ck.{hexpart}.0")
    assert _label(broker, tomb_resource_key("ok.target"), "origin_key", "ok.target") == "claims cockpit token"
    # the journal keeps the episode's id and type, never what the delete erased
    images = [r[0] for r in _rows(broker, "SELECT pre_image FROM effects WHERE operation_id = ?", out["pending_id"])]
    assert all("the episode to tombstone" not in (img or "") for img in images)
    assert any(ep.id in (img or "") for img in images)

def test_a_tombstone_of_a_row_that_changed_since_render_is_stale_and_deletes_nothing(broker, db):
    target = _target(db)
    with Store(str(db)) as st:
        st.delete(_episode(db, "ok.target").id)
        st.record("the episode to tombstone", "observation", origin_key="ok.target2")
    _refused("stale", broker.issue, verb="episode_tombstone", params={}, target=target, tier="T2",
             credential=BROWSER, intent="reported")

def test_a_signed_tombstone_over_an_episode_superseded_after_issue_ends_stale(broker, db, keydir):
    laptop = Key(keydir, "laptop").enroll(broker, "laptop-presence")
    out = _tombstone(broker, db)                     # no intent: a sign pending
    assert out["state"] == "WAITING"
    # another writer names the episode: anneal's version moves, the rendered fields do not
    with Store(str(db)) as st:
        st.record("the episode to tombstone, corrected", "observation", supersedes=[_episode(db, "ok.target").id])
    done = broker.submit(out["pending_id"], laptop.sign(out["record"].encode()))
    assert (done["state"], done["reason"]) == ("STALE", "changed")
    assert _episode(db, "ok.target") is not None

def test_a_feed_row_is_not_a_tombstone_target(broker, db):
    t = dict(_target(db), row_id="feed:other:abc123")
    _refused("bad_target", broker.issue, verb="episode_tombstone", params={}, target=t, tier="T2",
             credential=BROWSER, intent="reported")

def test_a_tombstone_takes_no_params(broker, db):
    _refused("refused", broker.issue, verb="episode_tombstone", params={"x": 1}, target=_target(db),
             tier="T2", credential=BROWSER, intent="reported")

# --- the adapter alone ------------------------------------------------------------------------------

def _unit(db: Path, effect_id: str) -> ApplyUnit:
    got = EpisodeEffectStore(db).read("ok.target")
    assert got is not None
    fx = tombstone_effects("ok.target", got.version, effect_id=effect_id)
    (unit,) = plan_units(fx, {EPISODES: EpisodeEffectStore(db)})
    return unit

def test_a_retry_of_the_same_operation_is_already_and_another_operation_is_changed(db):
    es = EpisodeEffectStore(db)
    unit = _unit(db, "ck.aa.0")
    first = es.apply_unit(unit)
    assert first.outcome is ApplyOutcome.APPLIED and first.members[0].stored == {"origin_key": "ok.target"}
    assert es.apply_unit(unit).outcome is ApplyOutcome.ALREADY
    other = ApplyUnit(unit.store, unit.indices, (
        tombstone_effects("ok.target", unit.effects[1].expect["version"], effect_id="ck.bb.0")))
    assert es.apply_unit(other).outcome is ApplyOutcome.PRECONDITION_LOST

def test_a_moved_version_is_changed_and_writes_nothing(db):
    es = EpisodeEffectStore(db)
    stale = ApplyUnit(EPISODES, (0, 1), tuple(tombstone_effects("ok.target", "0" * 64, effect_id="ck.cc.0")))
    assert es.apply_unit(stale).outcome is ApplyOutcome.PRECONDITION_LOST
    assert _episode(db, "ok.target") is not None

def test_a_lone_tomb_or_a_mismatched_pair_is_refused(db):
    es = EpisodeEffectStore(db)
    tomb, ep = _unit(db, "ck.dd.0").effects
    with pytest.raises(PermanentApplyRefusal):
        es.apply_unit(ApplyUnit(EPISODES, (0,), (tomb,)))
    other = tombstone_effects("ok.other", ep.expect["version"], effect_id="ck.dd.0")[1]
    with pytest.raises(PermanentApplyRefusal):
        es.apply_unit(ApplyUnit(EPISODES, (0, 1), (tomb, other)))
    assert _episode(db, "ok.target") is not None

def test_a_missing_store_is_gone_and_is_not_created(home, db):
    unit = _unit(db, "ck.ee.0")
    missing = home / "nowhere.db"
    with pytest.raises(StoreGone):
        EpisodeEffectStore(missing).apply_unit(unit)
    assert not missing.exists()

def test_a_tomb_has_no_snapshot_and_a_gone_episode_reads_absent(db):
    """Who retired a key is known only to apply_unit, which compares its own effect_id (L1 r1 #4)."""
    es = EpisodeEffectStore(db)
    with pytest.raises(PermanentApplyRefusal):
        es.snapshot(tomb_resource_key("ok.target"))
    es.apply_unit(_unit(db, "ck.ff.0"))
    assert es.snapshot("episode:ok.target").row is None


def test_an_origin_key_no_episode_ever_carried_is_refused(db):
    es = EpisodeEffectStore(db)
    unit = ApplyUnit(EPISODES, (0, 1), tuple(tombstone_effects("ok.never", "0" * 64, effect_id="ck.gg.0")))
    with pytest.raises(PermanentApplyRefusal, match="no episode ever carried"):
        es.apply_unit(unit)


def test_a_bad_effect_id_is_refused_before_the_store_opens(home, db):
    tomb, ep = _unit(db, "ck.hh.0").effects
    bad = Effect(tomb.resource_key, tomb.store, tomb.expect, {"effect_id": "has space"}, tomb.projection,
                 tomb.writes, ())
    with pytest.raises(PermanentApplyRefusal, match="effect_id"):
        EpisodeEffectStore(home / "nowhere.db").apply_unit(ApplyUnit(EPISODES, (0, 1), (bad, ep)))
    assert not (home / "nowhere.db").exists()


def test_a_row_whose_rendered_fields_moved_is_stale_while_the_episode_stays(broker, db):
    t = dict(_target(db), row_version="0123456789abcdef")
    _refused("stale", broker.issue, verb="episode_tombstone", params={}, target=t, tier="T2",
             credential=BROWSER, intent="reported")
    assert _episode(db, "ok.target") is not None


def test_deeply_nested_metadata_is_a_transient_read_fault_not_a_recursion_error(db):
    """L1 r1 #1: anneal parses metadata with json.loads; a nested value must not escape the read."""
    conn = sqlite3.connect(db)
    conn.execute("UPDATE episodes SET metadata = ? WHERE origin_key = 'ok.target'",
                 ("[" * 100_000 + "]" * 100_000,))
    conn.commit()
    conn.close()
    with pytest.raises(TransientApplyFault):
        EpisodeEffectStore(db).read("ok.target")


def test_a_team_entry_is_refused_with_its_reason(broker, db):
    with Store(str(db)) as st:
        st.record("a teammate's episode", "observation", source="team:alice", origin_key="ok.team")
    ep = _episode(db, "ok.team")
    got = EpisodeEffectStore(db).read("ok.team")
    t = {"kind": "row", "panel_id": "episodes", "row_id": f"episode:{ep.id}", "row_version": got.rendered}
    err = _refused("refused", broker.issue, verb="episode_tombstone", params={}, target=t, tier="T2",
                   credential=BROWSER, intent="reported")
    assert "team entry" in str(err)
    assert _episode(db, "ok.team") is not None
