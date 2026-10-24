"""`doctor` rows over the facts root already writes for the agent-session isolation install (S1 session 6a)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from levain import doctor
from levain.slot import records as R
from levain.slot.ids import SlotId

NOW = 1_800_000_000.0


def _store(tmp: Path, states: dict[str, str]) -> Path:
    store = tmp / "store"
    for tag, state in states.items():
        d = store / "slots" / tag
        d.mkdir(parents=True)
        slot = SlotId("egress" if tag[0] == "e" else "none", int(tag[1:]))
        sess = "s-a1b2c3d4e5f60718" if state == "leased" else None
        rec = R.SlotState(slot, 1, 1, state, True, R.timestamp(NOW), sess)
        (d / "state").write_bytes(R.slot_state_bytes(rec))
    if not states:
        (store / "slots").mkdir(parents=True)
    return store


def _pass(tmp: Path, *, ended_ago: float, control: str = "up", held: tuple = ()) -> Path:
    run = tmp / "run"
    (run / "reaper").mkdir(parents=True)
    rep = {"boot_id": "b", "pass_seq": 7, "started_at": R.timestamp(NOW - ended_ago - 1),
           "ended_at": R.timestamp(NOW - ended_ago), "control": control, "held": list(held), "skipped": []}
    (run / "reaper" / "pass.json").write_text(json.dumps(rep))
    return run


def _rows(store: Path, run: Path, free: int = 100 << 30):
    return {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, run=run, now=NOW,
                                                            free_bytes=lambda p: free)}


def test_doctor_slot_section_absent_without_install(tmp_path: Path):
    rows = doctor._check_slot_isolation(host="linux", store=tmp_path / "none", run=tmp_path / "none", now=NOW)
    assert len(rows) == 1 and rows[0].neutral and rows[0].ok and not rows[0].warn
    assert rows[0].name == "agent sessions" and "not installed" in rows[0].detail
    assert "sudo levain setup-isolation --agent" in rows[0].detail


def test_doctor_slot_reaper_stale_is_red_and_unreadable_is_said(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable"})
    r = _rows(store, _pass(tmp_path, ended_ago=3*3600))["agent reaper"]
    assert not r.ok and "3h ago" in r.detail and "not being ended, braked or purged" in r.detail
    run = tmp_path / "run"
    os.chmod(run / "reaper", 0)
    try:
        if os.access(run / "reaper" / "pass.json", os.R_OK):      # running as root: the mode cannot be tested
            return
        u = _rows(store, run)["agent reaper"]
        assert u.ok and u.warn and "cannot read" in u.detail
    finally:
        os.chmod(run / "reaper", 0o755)


def test_doctor_slot_held_and_control_down_are_red(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable"})
    rows = _rows(store, _pass(tmp_path, ended_ago=5, control="down", held=("s-0123abcd01234567",)))
    assert rows["agent reaper"].ok
    assert not rows["agent control"].ok and "live sessions are ended at their lease" in rows["agent control"].detail
    held = rows["agent held session s-0123abcd01234567"]
    assert not held.ok and "HELD" in held.detail and "slot stays leased" in held.detail


def test_doctor_slot_pool_below_floor_and_free_space(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable", "e2": "retired", "v1": "leased"})
    rows = _rows(store, _pass(tmp_path, ended_ago=5), free=1 << 30)
    pool = rows["agent slots"]
    assert not pool.ok and "2 of 3 usable" in pool.detail and "replenish" in pool.hint
    assert not rows["agent store free space"].ok
    assert _rows(_store(tmp_path / "ok", {"e1": "reusable"}), _pass(tmp_path / "ok", ended_ago=5))["agent slots"].ok
