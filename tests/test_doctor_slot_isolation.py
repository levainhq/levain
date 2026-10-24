"""`doctor` rows over the facts the OPERATOR can read of the agent-session isolation install (S1 session 6a)."""

from __future__ import annotations

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


def _rows(store: Path, free: int = 100 << 30):
    return {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: free)}


def test_doctor_slot_section_absent_without_install(tmp_path: Path):
    rows = doctor._check_slot_isolation(host="linux", store=tmp_path / "none")
    assert len(rows) == 1 and rows[0].neutral and rows[0].ok and not rows[0].warn
    assert rows[0].name == "agent sessions" and "not installed" in rows[0].detail
    assert "sudo levain setup-isolation --agent" in rows[0].detail


def test_doctor_slot_pool_below_floor_is_red(tmp_path: Path):
    pool = _rows(_store(tmp_path, {"e1": "reusable", "e2": "retired", "v1": "leased"}))["agent slots"]
    assert not pool.ok and "2 of 3 usable" in pool.detail and "e2 (retired)" in pool.detail
    assert "replenish" in pool.hint
    assert _rows(_store(tmp_path / "ok", {"e1": "reusable"}))["agent slots"].ok


def test_doctor_slot_free_space_below_floor_is_red(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable"})
    assert not _rows(store, free=1 << 30)["agent store free space"].ok
    assert _rows(store)["agent store free space"].ok
    assert not any(n.startswith(("agent reaper", "agent control", "agent held")) for n in _rows(store))
