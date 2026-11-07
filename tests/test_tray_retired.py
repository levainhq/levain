"""TS-1: handoff and the Keep note are RETIRED vocabulary. A new one is refused at every write seam; a
legacy row still reads, descends and re-dispositions to seed or agenda."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.spores import BUCKET_KEEP, BUCKET_TRAY, NON_COGNITION_DISPOSITIONS, bucket_of, is_loop
from levain.writes import RETIRED_HANDOFF_MSG, RETIRED_KEEP_NOTE_MSG, EditError, apply_edit

from tests.test_writes import _add_spore, _legacy_seed, _scope, _spore_store, install  # noqa: F401


def test_retired_message_texts() -> None:
    assert RETIRED_HANDOFF_MSG == (
        "handoff is retired: ask the operator with `levain ask` (or `--on <date>` for later), "
        "or put the work on the board")
    assert RETIRED_KEEP_NOTE_MSG == (
        "the Keep is retired: a fact or command goes to memory (Durable Facts), an intention is a seed")
    assert "Keep" in RETIRED_KEEP_NOTE_MSG


@pytest.mark.parametrize("disp,msg", [("handoff", RETIRED_HANDOFF_MSG), ("note", RETIRED_KEEP_NOTE_MSG)])
def test_seed_refuses_retired(install: Path, disp: str, msg: str) -> None:
    with pytest.raises(EditError) as ei:
        apply_edit(_scope(install), {"kind": "spore_seed", "text": "x", "disposition": disp})
    assert ei.value.code == "bad_verb_arg" and ei.value.http_status == 422 and msg in str(ei.value)


@pytest.mark.parametrize("disp,msg", [("handoff", RETIRED_HANDOFF_MSG), ("note", RETIRED_KEEP_NOTE_MSG)])
def test_set_disposition_refuses_retired(install: Path, disp: str, msg: str) -> None:
    sid = _add_spore(install, disposition="seed")
    with pytest.raises(EditError) as ei:
        apply_edit(_scope(install), {"kind": "spore_set_disposition", "spore_id": sid, "disposition": disp})
    assert ei.value.http_status == 422 and msg in str(ei.value)
    assert _spore_store(install).get(sid)["disposition"] == "seed"   # nothing written


def test_seed_still_takes_seed_and_agenda(install: Path) -> None:
    for d in ("seed", "agenda"):
        res = apply_edit(_scope(install), {"kind": "spore_seed", "text": f"t-{d}", "disposition": d})
        assert _spore_store(install).get(res["spore_id"])["disposition"] == d


@pytest.mark.parametrize("disp", ["handoff", "note"])
def test_legacy_row_reads_descends_and_redispositions(install: Path, disp: str) -> None:
    sid = _legacy_seed(install, "old row", disp)
    row = _spore_store(install).get(sid)
    assert not is_loop(row) and disp in NON_COGNITION_DISPOSITIONS
    assert bucket_of(row) == (BUCKET_KEEP if disp == "note" else BUCKET_TRAY)
    # re-disposition to seed / agenda works
    for target in ("agenda", "seed"):
        tsid = _legacy_seed(install, f"old row {target}", disp)
        apply_edit(_scope(install), {"kind": "spore_set_disposition", "spore_id": tsid, "disposition": target})
        assert _spore_store(install).get(tsid)["disposition"] == target
    # an echo of the row's own legacy value is not a new retired row
    apply_edit(_scope(install), {"kind": "spore_set_disposition", "spore_id": sid, "disposition": disp})
    # a second legacy row descends
    sid2 = _legacy_seed(install, "old row 2", disp)
    res = apply_edit(_scope(install), {"kind": "spore_descend", "spore_id": sid2,
                                       "spore_kind": "composted", "confirm": True})
    assert res["ok"]


def test_is_loop_still_false_for_legacy_note() -> None:
    assert is_loop({"disposition": "note"}) is False
    assert is_loop({"disposition": "handoff"}) is False


def test_cockpit_effects_refuse_retired() -> None:
    from levain.cockpit.effects import _ce_set_disposition

    pre = {"id": "spore-001", "origin_key": "ok-1", "status": "open", "disposition": "seed", "text": "x", "type": "thought"}
    for d, msg in (("note", RETIRED_KEEP_NOTE_MSG), ("handoff", RETIRED_HANDOFF_MSG)):
        with pytest.raises(ValueError) as ei:
            _ce_set_disposition({"disposition": d}, pre, origin_key=None, version_of=lambda *a, **k: "v")
        assert msg in str(ei.value)


def test_legacy_echo_passes_cockpit_update_and_new_retired_change_refused() -> None:
    from levain.cockpit.effects import _ce_undo_edit, _ce_update  # noqa: F401
    from levain.cockpit.effects import _norm_update_field

    pre = {"id": "spore-001", "origin_key": "ok-1", "status": "open", "disposition": "note", "text": "x", "type": "thought"}
    assert _norm_update_field("disposition", "note", pre) == "note"          # echo of the legacy value
    with pytest.raises(ValueError):
        _norm_update_field("disposition", "handoff", pre)                   # a CHANGE into a retired value
    with pytest.raises(ValueError):
        _norm_update_field("disposition", "note", {"disposition": "seed"})


def test_store_refuses_retired_on_every_writer(tmp_path: Path) -> None:
    from anneal_memory.spores import ApplyRefused, SporeApply

    from levain.spores import RetiredDisposition, guarded_spore_store

    store = guarded_spore_store(tmp_path / "s.json")
    for d in ("handoff", "note"):
        with pytest.raises(RetiredDisposition):
            store.add(type="thought", text="x", disposition=d)
    row = store.add(type="thought", text="live", disposition="seed")
    with pytest.raises(RetiredDisposition):
        store.update(row["id"], disposition="note")
    # a legacy row: written through the unguarded base class, as an old install left it
    from anneal_memory.spores import SporeStore
    old = SporeStore(tmp_path / "s.json").add(type="thought", text="old", disposition="handoff")
    store.update(old["id"], disposition="handoff")                             # unchanged: passes
    # apply(): a planned effect deserialized after an upgrade
    key = row["origin_key"]
    eff = SporeApply(op="update", origin_key=key, args={"disposition": "note"}, postcondition={"disposition": "note"})
    with pytest.raises(ApplyRefused):
        store.apply(eff)
    assert store.get(row["id"])["disposition"] == "seed"
    # crash-after-apply: the create already landed, so the retry reports already, not a refusal
    add = SporeApply(op="add", origin_key=old["origin_key"], args={"type": "thought", "text": "old", "disposition": "handoff"},
                     postcondition={"type": "thought", "text": "old", "disposition": "handoff"})
    assert store.apply(add).outcome == "already"
