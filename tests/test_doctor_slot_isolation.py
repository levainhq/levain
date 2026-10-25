"""`doctor` rows over the facts the OPERATOR can read of the agent-session isolation install (S1 session 6a)."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path
from unittest import mock

from levain import doctor
from levain.cli import main
from levain.slot import layout
from levain.slot import records as R
from levain.slot.ids import SlotId

NOW = 1_800_000_000.0


def _store(tmp: Path, states: dict[str, str], pool: tuple[int, int] | None = None) -> Path:
    store = tmp / "store"
    (store / "slots").mkdir(parents=True)
    (store / "slots" / layout.POOL_LOCK_NAME).write_text("")
    for tag, state in states.items():
        d = store / "slots" / tag
        d.mkdir()
        slot = SlotId("egress" if tag[0] == "e" else "none", int(tag[1:]))
        sess = "s-a1b2c3d4e5f60718" if state == "leased" else None
        rec = R.SlotState(slot, 1, 1, state, True, R.timestamp(NOW), sess)
        (d / "state").write_bytes(R.slot_state_bytes(rec))
    if pool is not None:
        (store / "policy").mkdir()
        (store / "policy" / "pool").write_text(layout.pool_config_text(*pool))
    return store


def _rows(store: Path, free: int = 100 << 30):
    return {r.name: r for r in doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: free)}


def test_doctor_slot_section_absent_without_install(tmp_path: Path):
    rows = doctor._check_slot_isolation(host="linux", store=tmp_path / "none")
    assert len(rows) == 1 and rows[0].neutral and rows[0].ok and not rows[0].warn
    assert rows[0].name == "agent sessions" and "not installed" in rows[0].detail
    (tmp_path / "half").mkdir()    # the store is there and its slots directory is not: not the same as never installed
    half = doctor._check_slot_isolation(host="linux", store=tmp_path / "half", free_bytes=lambda p: 100 << 30)[0]
    assert not half.ok and "partially installed" in half.detail
    real = tmp_path / "real"
    (real / "slots").mkdir(parents=True)
    os.symlink(real, tmp_path / "linkstore")        # a linked store root, then a linked slots/: both refused
    for link in (tmp_path / "linkstore", ):
        row = doctor._check_slot_isolation(host="linux", store=link, free_bytes=lambda p: 100 << 30)[0]
        assert not row.ok and "symbolic link" in row.detail
    (tmp_path / "ls").mkdir()
    os.symlink(real / "slots", tmp_path / "ls" / "slots")
    row = doctor._check_slot_isolation(host="linux", store=tmp_path / "ls", free_bytes=lambda p: 100 << 30)[0]
    assert not row.ok and "symbolic link" in row.detail


def test_doctor_slot_linked_policy_directory_is_refused(tmp_path: Path):
    store = _store(tmp_path / "a", {"e1": "reusable"}, pool=(1, 0))
    real = tmp_path / "realpolicy"
    os.rename(store / "policy", real)       # the runtime opens every component O_NOFOLLOW: a linked policy/ is refused
    os.symlink(real, store / "policy")
    row = _rows(store)["agent sessions"]
    assert not row.ok and "symbolic link" in row.detail and "policy" in row.detail


def test_doctor_slot_pool_is_judged_against_policy_pool_and_tombstones_are_not_counted(tmp_path: Path):
    # e0 retired long ago, e1 + e2 + v1 in service; the pool wants 2 egress + 1 verifier: green despite the tombstone
    ok = _rows(_store(tmp_path / "a", {"e0": "retired", "e1": "reusable", "e2": "leased", "v1": "scrubbed"},
                      pool=(2, 1)))["agent slots"]
    assert ok.ok and "3 usable of 3 required" in ok.detail and "1 retired, not counted" in ok.detail
    # only e1 remains of a pool of 2 egress + 1 verifier: red, naming the short classes
    low = _rows(_store(tmp_path / "b", {"e0": "retired", "e1": "reusable"}, pool=(2, 1)))["agent slots"]
    assert not low.ok and "egress 1 of 2" in low.detail and "verifier 0 of 1" in low.detail
    # a state naming another slot is not usable (the runtime helper refuses it)
    store = _store(tmp_path / "c", {"e1": "reusable", "v1": "reusable"}, pool=(1, 1))
    os.replace(store / "slots" / "v1" / "state", store / "slots" / "e1" / "state")
    bad = _rows(store)["agent slots"]
    assert not bad.ok and "state names v1" in bad.detail


def test_doctor_slot_unreadable_state_is_not_missing_and_an_oserror_does_not_abort(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable", "e2": "reusable"}, pool=(3, 0))
    (store / "slots" / "e3").mkdir()      # no state file: the runtime reads absent as not reusable
    (store / "slots" / "e01").mkdir()     # non-canonical: ignored, and said so
    os.unlink(store / "slots" / "e2" / "state")
    os.symlink(store / "slots" / "e1" / "state", store / "slots" / "e2" / "state")   # a link: refused
    row = _rows(store)["agent slots"]
    assert "e2 (state unreadable: bad-root" in row.detail and "e2 (state missing" not in row.detail and not row.ok
    assert "e3 (state missing)" in row.detail and "ignored (not slots): e01" in row.detail
    with mock.patch.object(os, "listdir", side_effect=PermissionError(13, "denied")):
        rows = doctor._check_slot_isolation(host="linux", store=store, free_bytes=lambda p: 100 << 30)
    assert rows[0].name == "agent slots" and rows[0].warn and "permission denied" in rows[0].detail
    assert rows[-1].name == "agent store free space"


def test_doctor_slot_without_a_state_file_is_not_usable_and_turns_its_class_red(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable"}, pool=(2, 0))
    (store / "slots" / "e2").mkdir()
    row = _rows(store)["agent slots"]
    assert not row.ok and "egress 1 of 2" in row.detail and "e2 (state missing)" in row.detail


def test_doctor_slot_store_without_a_pool_configuration_is_a_failing_row(tmp_path: Path):
    row = _rows(_store(tmp_path, {"e1": "reusable"}, pool=None))["agent slots"]
    assert not row.ok and "pool configuration is missing" in row.detail and "capacity cannot be judged" in row.detail


def test_doctor_slot_free_space_below_floor_is_red(tmp_path: Path):
    store = _store(tmp_path, {"e1": "reusable"}, pool=(1, 0))
    assert not _rows(store, free=0)["agent store free space"].ok      # 0 is a measurement, not an absent one
    assert _rows(store)["agent store free space"].ok
    assert not any(n.startswith(("agent reaper", "agent control", "agent held")) for n in _rows(store))


class _Parsed(Exception):
    pass


def _parses(cmd: str) -> bool:
    orig = argparse.ArgumentParser.parse_args

    def once(self, args=None, namespace=None):
        orig(self, args, namespace)
        raise _Parsed

    with mock.patch.object(argparse.ArgumentParser, "parse_args", once):
        try:
            main(shlex.split(cmd)[1:])
        except _Parsed:
            return True
        except SystemExit:
            return False
    return False


def test_doctor_slot_every_printed_remedy_parses_in_the_real_cli(tmp_path: Path):
    assert _parses("levain doctor") and not _parses("levain setup-isolation --agent")   # the rig can fail
    hints: list[str] = []
    for n, (states, pool) in enumerate([({}, None), ({"e1": "retired"}, (2, 1)), ({"e1": "reusable"}, None),
                                        ({"e1": "reusable"}, (1, 0))]):
        store = _store(tmp_path / str(n), states, pool)
        hints += [r.hint or "" for r in doctor._check_slot_isolation(host="linux", store=store,
                                                                     free_bytes=lambda p: 0)]
    hints += [r.hint or "" for r in doctor._check_slot_isolation(host="linux", store=tmp_path / "nope")]
    assert any(hints)                      # the rig reached real remedy text
    assert not any("--agent" in h or "setup-isolation repair" in h or "setup-isolation replenish" in h
                   for h in hints)
