"""TS-1: handoff and the Keep note are RETIRED vocabulary. A new one is refused at every write seam; a
legacy row still reads, descends and re-dispositions to seed or agenda."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.spores import BUCKET_TRAY, NON_COGNITION_DISPOSITIONS, bucket_of, is_loop
from levain.writes import RETIRED_HANDOFF_MSG, RETIRED_KEEP_NOTE_MSG, EditError, apply_edit

from tests.test_writes import _add_spore, _legacy_seed, _scope, _spore_store, install  # noqa: F401


def test_retired_message_texts() -> None:
    assert RETIRED_HANDOFF_MSG == (
        "handoff is retired: ask the operator now with `levain ask`, "
        "or record a seed, or an agenda with a date, in the Tray")
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
    assert bucket_of(row) == BUCKET_TRAY   # TS-5a: no Keep bucket; a legacy note renders in the Tray
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
    store.update(old["id"], text="edited")                       # a legacy row edits freely when disposition is omitted
    assert store.get(old["id"])["disposition"] == "handoff"
    # apply(): a planned effect deserialized after an upgrade (a well-formed one: the guard, not a
    # missing expected_version, is what refuses it)
    from anneal_memory.spores import spore_version
    key = row["origin_key"]
    eff = SporeApply(op="update", origin_key=key, args={"disposition": "note"}, postcondition={"disposition": "note"},
                     expected_version=spore_version(store.get(row["id"])))
    with pytest.raises(ApplyRefused, match="Keep is retired"):
        store.apply(eff)
    assert store.get(row["id"])["disposition"] == "seed"
    # known edge: a replay of a pre-upgrade create that carries a retired value is refused, even if it landed
    add = SporeApply(op="add", origin_key=old["origin_key"], args={"type": "thought", "text": "old", "disposition": "handoff"},
                     postcondition={"type": "thought", "text": "old", "disposition": "handoff"})
    with pytest.raises(ApplyRefused):
        store.apply(add)


def test_norm_disposition_missing_or_corrupt_still_invalid() -> None:
    """N2: only an ECHO of a retired value is exempt from 'must be one of'."""
    from levain.cockpit.effects import _norm_disposition

    for bad, cur in ((None, None), ("garbage", "garbage"), ("garbage", "seed"), (None, "seed")):
        with pytest.raises(ValueError, match="must be one of"):
            _norm_disposition(bad, cur)
    assert _norm_disposition("note", "note") == "note"          # a retired echo still passes (the effect drops it)
    assert _norm_disposition("note ", "note") == "note"         # in any spelling anneal persists as the row's value


def test_set_disposition_effect_missing_disposition_refused() -> None:
    from levain.cockpit.effects import _ce_set_disposition

    pre = {"id": "spore-001", "origin_key": "ok-1", "status": "open", "text": "x", "type": "thought"}  # loop row: no key
    with pytest.raises(ValueError, match="must be one of"):
        _ce_set_disposition({}, pre, origin_key=None, version_of=lambda r: "v")


def test_undo_edit_restoring_retired_value_is_refused() -> None:
    """Known edge: an undo back into a legacy handoff/note is a CHANGE into a retired value."""
    from levain.cockpit.effects import _ce_undo_edit

    pre = {"id": "spore-001", "origin_key": "ok-1", "status": "open", "disposition": "seed", "text": "x", "type": "thought"}
    for old in ("handoff", "note"):
        with pytest.raises(ValueError, match="retired"):
            _ce_undo_edit({"restore": {"disposition": old}}, pre, origin_key=None, version_of=lambda r: "v")


def test_retired_disposition_from_store_is_422_not_500(install: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N: a RetiredDisposition out of the guarded store must be a clean 422 bad_verb_arg on update + verb paths."""
    from levain import writes
    from levain.spores import RetiredDisposition

    sid = _add_spore(install, disposition="seed")

    class Boom:
        def get(self, *_a, **_k):
            return {"id": sid, "status": "open", "disposition": "seed", "type": "thought", "text": "x"}

        def __getattr__(self, name):
            def raiser(*_a, **_k):
                raise RetiredDisposition("retired")
            return raiser

    monkeypatch.setattr(writes, "guarded_spore_store", lambda _p: Boom())
    for req in ({"kind": "spore_update", "spore_id": sid, "text": "new"},
                {"kind": "spore_descend", "spore_id": sid, "spore_kind": "composted", "confirm": True},
                {"kind": "spore_touch", "spore_id": sid}):
        with pytest.raises(EditError) as ei:
            apply_edit(_scope(install), req)
        assert ei.value.code == "bad_verb_arg" and ei.value.http_status == 422, req


@pytest.mark.parametrize("disp", ["handoff", "note"])
def test_set_disposition_missing_spore_is_404_before_retirement(install: Path, disp: str) -> None:
    with pytest.raises(EditError) as ei:
        apply_edit(_scope(install), {"kind": "spore_set_disposition", "spore_id": "spore-999", "disposition": disp})
    assert ei.value.code == "not_found" and ei.value.http_status == 404


def test_store_refuses_a_retired_value_even_as_an_echo_under_a_cas(tmp_path: Path) -> None:
    """There is no echo: a write that NAMES a retired value is refused, CAS or not. A caller leaving a legacy
    disposition as it is omits the field."""
    from anneal_memory.spores import SporeStore

    from levain.spores import RetiredDisposition, guarded_spore_store

    old = SporeStore(tmp_path / "s.json").add(type="thought", text="old", disposition="note")
    store = guarded_spore_store(tmp_path / "s.json")
    for cas in ("note", "seed", None):
        with pytest.raises(RetiredDisposition):
            store.update(old["id"], disposition="note", expect_disposition=cas)
    with pytest.raises(RetiredDisposition):
        store.update(old["id"], disposition="note")
    assert store.get(old["id"])["disposition"] == "note"


# --- the store guard is a BOUND on the persisted value: a retired value is never written --------------------

# spellings anneal persists as a retired value (normalisation) or as a value nothing reads (no casefold,
# zero-width kept): every one must be refused, not just the two exact strings
VARIANTS = ["note ", "note\t", "note\x07", "handoff\n", "NOTE", "Note", "note​", "garbage"]


def _legacy(path: Path, disp: str, **kw):
    from anneal_memory.spores import SporeStore
    return SporeStore(path).add(type="thought", text="old", disposition=disp, **kw)


def test_base_store_persists_a_padded_variant_as_the_retired_value(tmp_path: Path) -> None:
    """The threat, shown on the unguarded store: 'note ' lands as 'note'."""
    assert _legacy(tmp_path / "s.json", "note ")["disposition"] == "note"


@pytest.mark.parametrize("variant", VARIANTS)
def test_store_refuses_every_variant_on_add_update_apply(tmp_path: Path, variant: str) -> None:
    from anneal_memory.spores import ApplyRefused, SporeApply, spore_version

    from levain.spores import RetiredDisposition, guarded_spore_store

    store = guarded_spore_store(tmp_path / "s.json")
    with pytest.raises(RetiredDisposition):
        store.add(type="thought", text="x", disposition=variant)
    row = store.add(type="thought", text="live", disposition="seed")
    with pytest.raises(RetiredDisposition):
        store.update(row["id"], disposition=variant)
    upd = SporeApply(op="update", origin_key=row["origin_key"], args={"disposition": variant},
                     postcondition={"disposition": variant}, expected_version=spore_version(row))
    with pytest.raises(ApplyRefused):
        store.apply(upd)
    add = SporeApply(op="add", origin_key="k.new", args={"type": "thought", "text": "y", "disposition": variant},
                     postcondition={"type": "thought", "text": "y", "disposition": variant})
    with pytest.raises(ApplyRefused):
        store.apply(add)
    assert store.get(row["id"])["disposition"] == "seed"
    assert store.get_by_origin_key("k.new") is None
    assert [r.get("disposition") for r in store.list_open()] == ["seed"]   # nothing else landed


def test_store_still_takes_the_write_set_and_a_clear(tmp_path: Path) -> None:
    from levain.spores import guarded_spore_store

    store = guarded_spore_store(tmp_path / "s.json")
    row = store.add(type="thought", text="x", disposition="seed ")       # persisted 'seed': in the write set
    assert row["disposition"] == "seed"
    store.update(row["id"], disposition="agenda")
    store.update(row["id"], disposition=None)                              # a clear is a key-free loop
    assert "disposition" not in store.get(row["id"])


def test_legacy_row_other_fields_edit_with_disposition_omitted_and_a_normal_cas(tmp_path: Path) -> None:
    """A legacy row's other fields change when the write omits disposition. anneal's own CAS on the CURRENT
    value (expect_disposition alone, never a disposition write) stays usable: a lost CAS writes nothing."""
    from anneal_memory.spores import SporeError, SporeStore

    from levain.spores import guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    store = guarded_spore_store(tmp_path / "s.json")
    store.update(old["id"], expect_disposition="note", text="edited")
    assert store.get(old["id"])["disposition"] == "note" and store.get(old["id"])["text"] == "edited"
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="seed")
    with pytest.raises(SporeError):
        store.update(old["id"], expect_disposition="note", text="again")   # anneal's lock sees seed
    assert store.get(old["id"])["text"] == "edited"


def test_explicit_unset_sentinels(tmp_path: Path) -> None:
    """A wrapper that forwards anneal's _UNSET for every kwarg: a non-disposition edit passes; a retired
    value is refused whatever the CAS slot holds."""
    from anneal_memory.spores import _UNSET

    from levain.spores import RetiredDisposition, guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    store = guarded_spore_store(tmp_path / "s.json")
    store.update(old["id"], text="t2", disposition=_UNSET, expect_disposition=_UNSET)
    assert store.get(old["id"])["text"] == "t2"
    with pytest.raises(RetiredDisposition):
        store.update(old["id"], disposition="note", expect_disposition=_UNSET)


def test_apply_update_naming_a_retired_value_is_refused_with_or_without_a_cas(tmp_path: Path) -> None:
    from anneal_memory.spores import ApplyRefused, SporeApply, spore_version

    from levain.spores import guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    store = guarded_spore_store(tmp_path / "s.json")
    for extra in ({}, {"expect_disposition": "note"}):
        eff = SporeApply(op="update", origin_key=old["origin_key"], args={"disposition": "note", "text": "z", **extra},
                         postcondition={"disposition": "note", "text": "z"}, expected_version=spore_version(old))
        with pytest.raises(ApplyRefused):
            store.apply(eff)
    assert store.get(old["id"])["text"] == "old"
    # the same edit with disposition omitted lands on the legacy row
    eff = SporeApply(op="update", origin_key=old["origin_key"], args={"next": "2026-12-01"},
                     postcondition={"next": "2026-12-01"}, expected_version=spore_version(old))
    assert store.apply(eff).outcome == "applied"
    assert store.get(old["id"])["disposition"] == "note" and store.get(old["id"])["next"] == "2026-12-01"


@pytest.mark.parametrize("bad", [
    {"origin_key": " bad\x07key"},        # an origin_key the store refuses (padding, a control char)
    {"args": [1, 2]},                     # args that are not a mapping
])
def test_apply_malformed_retired_effect_is_apply_refused(tmp_path: Path, bad: dict) -> None:
    from anneal_memory.spores import ApplyRefused, SporeApply

    from levain.spores import guarded_spore_store

    store = guarded_spore_store(tmp_path / "s.json")
    fields = {"op": "add", "origin_key": "k.ok", "args": {"type": "thought", "text": "y", "disposition": "note"},
              "postcondition": {"type": "thought", "text": "y", "disposition": "note"}, **bad}
    with pytest.raises(ApplyRefused):
        store.apply(SporeApply(**fields))


def test_add_with_a_retired_value_is_refused_even_for_an_existing_key(tmp_path: Path) -> None:
    """No idempotent-retry shortcut: add() never returns success for a retired value."""
    from levain.spores import RetiredDisposition, guarded_spore_store

    old = _legacy(tmp_path / "s.json", "handoff")
    store = guarded_spore_store(tmp_path / "s.json")
    for key in (old["origin_key"], "k.fresh"):
        with pytest.raises(RetiredDisposition):
            store.add(type="thought", text="old", disposition="handoff", origin_key=key)
    assert store.get_by_origin_key("k.fresh") is None


def test_set_disposition_echo_on_a_legacy_row_drops_the_disposition(install: Path) -> None:
    """writes.py: a set_disposition that keeps a legacy note a note (clearing its date) or a legacy handoff a
    handoff lands, because the unchanged retired value is omitted from the store write."""
    from anneal_memory.spores import SporeStore

    sid = _legacy_seed(install, "old note", "note")
    store = _spore_store(install)
    SporeStore(store.path).update(sid, next="2026-12-01")       # a date an old install left on it
    apply_edit(_scope(install), {"kind": "spore_set_disposition", "spore_id": sid, "disposition": "note",
                                 "surface_at": None})
    row = _spore_store(install).get(sid)
    assert row["disposition"] == "note" and not row.get("next")
    hid = _legacy_seed(install, "old handoff", "handoff")
    res = apply_edit(_scope(install), {"kind": "spore_set_disposition", "spore_id": hid, "disposition": "handoff",
                                       "surface_at": "2026-12-02"})
    assert res["ok"]
    row = _spore_store(install).get(hid)
    assert row["disposition"] == "handoff" and row["next"] == "2026-12-02"


def test_cockpit_echo_on_a_legacy_row_omits_the_disposition(tmp_path: Path) -> None:
    """The cockpit: a set_disposition on a legacy note that keeps it a note and moves its date lands, its
    SporeApply writing no disposition (it rides only as anneal's expect_disposition CAS); a pure echo (nothing else changes) is ALREADY; a text
    edit on a legacy row lands."""
    from levain.cockpit.effects import ApplyOutcome, SporeEffectStore, canonical_effect, spore_resource_key

    _legacy(tmp_path / "s.json", "note", origin_key="k.note")
    es = SporeEffectStore(tmp_path / "s.json")
    pre = es.snapshot(spore_resource_key("k.note")).row

    def run(eff):
        (unit,) = es.units([(0, eff)])
        return es.apply_unit(unit).outcome

    echo = canonical_effect("spore_set_disposition", {"disposition": "note", "surface_at": "2026-12-01"}, pre)
    sa = es.spore_apply(echo)
    assert dict(sa.args) == {"next": "2026-12-01", "expect_disposition": "note"}
    assert dict(sa.postcondition) == {"next": "2026-12-01"}
    assert run(echo) is ApplyOutcome.APPLIED
    assert run(echo) is ApplyOutcome.ALREADY   # a retry of the landed write: leaves hold, CAS holds
    row = es.snapshot(spore_resource_key("k.note")).row
    assert row["disposition"] == "note" and row["next"] == "2026-12-01"
    pre = row
    assert run(canonical_effect("spore_set_disposition", {"disposition": "note"}, pre)) is ApplyOutcome.ALREADY
    assert run(canonical_effect("spore_update", {"text": "refined"}, pre)) is ApplyOutcome.APPLIED
    row = es.snapshot(spore_resource_key("k.note")).row
    assert row["disposition"] == "note" and row["text"] == "refined"
    # a real change away from the retired value still names it
    move = canonical_effect("spore_set_disposition", {"disposition": "seed"}, row)
    assert dict(es.spore_apply(move).args) == {"disposition": "seed"}
    assert run(move) is ApplyOutcome.APPLIED


def test_cockpit_pure_echo_after_the_row_moved_is_changed_not_already(tmp_path: Path) -> None:
    from anneal_memory.spores import SporeStore

    from levain.cockpit.effects import ApplyOutcome, SporeEffectStore, canonical_effect, spore_resource_key

    old = _legacy(tmp_path / "s.json", "note", origin_key="k.note")
    es = SporeEffectStore(tmp_path / "s.json")
    pre = es.snapshot(spore_resource_key("k.note")).row
    echo = canonical_effect("spore_set_disposition", {"disposition": "note"}, pre)
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="agenda")
    (unit,) = es.units([(0, echo)])
    assert es.apply_unit(unit).outcome is ApplyOutcome.PRECONDITION_LOST


def test_cockpit_dropped_echo_is_not_already_after_the_disposition_moved(tmp_path: Path) -> None:
    """L3b codex/gemini HIGH: the echo is dropped from the write, so the date was its only leaf. A note
    already dated D, an effect {note, D}, another writer moves it to agenda: the date still matches, but
    the row no longer holds what the effect kept, so it is PRECONDITION_LOST, never ALREADY."""
    from anneal_memory.spores import SporeStore

    from levain.cockpit.effects import ApplyOutcome, SporeEffectStore, canonical_effect, spore_resource_key

    old = _legacy(tmp_path / "s.json", "note", origin_key="k.note", next="2026-12-01")
    es = SporeEffectStore(tmp_path / "s.json")
    pre = es.snapshot(spore_resource_key("k.note")).row
    eff = canonical_effect("spore_set_disposition", {"disposition": "note", "surface_at": "2026-12-01"}, pre)
    sa = es.spore_apply(eff)
    # the CAS rides as anneal's own precondition; no disposition is written or postconditioned
    assert dict(sa.args) == {"next": "2026-12-01", "expect_disposition": "note"}
    assert dict(sa.postcondition) == {"next": "2026-12-01"}
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="agenda")
    before = (tmp_path / "s.json").read_bytes()
    (unit,) = es.units([(0, eff)])
    assert es.apply_unit(unit).outcome is ApplyOutcome.PRECONDITION_LOST
    assert (tmp_path / "s.json").read_bytes() == before


def test_cockpit_dropped_echo_with_a_write_still_cas_the_disposition(tmp_path: Path) -> None:
    """The applied path: the date differs, so anneal writes; the CAS (and the version) refuse it once
    the disposition moved, and nothing is written."""
    from anneal_memory.spores import SporeStore

    from levain.cockpit.effects import ApplyOutcome, SporeEffectStore, canonical_effect, spore_resource_key

    old = _legacy(tmp_path / "s.json", "note", origin_key="k.note")
    es = SporeEffectStore(tmp_path / "s.json")
    pre = es.snapshot(spore_resource_key("k.note")).row
    eff = canonical_effect("spore_set_disposition", {"disposition": "note", "surface_at": "2026-12-01"}, pre)
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="agenda")
    before = (tmp_path / "s.json").read_bytes()
    (unit,) = es.units([(0, eff)])
    assert es.apply_unit(unit).outcome is ApplyOutcome.PRECONDITION_LOST
    assert (tmp_path / "s.json").read_bytes() == before


def test_cockpit_pure_echo_on_a_stale_version_is_precondition_lost(tmp_path: Path) -> None:
    """L3b gemini HIGH / deepseek MED 1: a pure echo writes nothing, so its expected_version is checked
    by levain: another writer edited the text (the disposition untouched) -> PRECONDITION_LOST."""
    from anneal_memory.spores import SporeStore

    from levain.cockpit.effects import ApplyOutcome, SporeEffectStore, canonical_effect, spore_resource_key

    old = _legacy(tmp_path / "s.json", "note", origin_key="k.note")
    es = SporeEffectStore(tmp_path / "s.json")
    pre = es.snapshot(spore_resource_key("k.note")).row
    echo = canonical_effect("spore_set_disposition", {"disposition": "note"}, pre)
    assert es.spore_apply(echo) is None
    SporeStore(tmp_path / "s.json").update(old["id"], text="edited elsewhere")
    (unit,) = es.units([(0, echo)])
    assert es.apply_unit(unit).outcome is ApplyOutcome.PRECONDITION_LOST
    # on the current version the same echo is ALREADY
    pre = es.snapshot(spore_resource_key("k.note")).row
    (unit,) = es.units([(0, canonical_effect("spore_set_disposition", {"disposition": "note"}, pre))])
    assert es.apply_unit(unit).outcome is ApplyOutcome.ALREADY


def test_guarded_store_lets_a_retired_cas_through_but_never_a_retired_write(tmp_path: Path) -> None:
    """expect_disposition is a compare, not a write: a retired value there passes the guard (update and
    apply) and anneal checks it; the same value as ``disposition`` is still refused."""
    from anneal_memory.spores import ApplyRefused, SporeApply, SporeError, spore_version

    from levain.spores import RetiredDisposition, guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note", origin_key="k.note")
    store = guarded_spore_store(tmp_path / "s.json")
    assert store.update(old["id"], text="ok", expect_disposition="note")["text"] == "ok"
    with pytest.raises(SporeError):
        store.update(old["id"], text="no", expect_disposition="handoff")
    row = store.get(old["id"])
    res = store.apply(SporeApply(op="update", origin_key="k.note", args={"next": "2026-12-03", "expect_disposition": "note"},
                                 postcondition={"next": "2026-12-03"}, expected_version=spore_version(row)))
    assert res.outcome == "applied" and res.spore["disposition"] == "note"
    with pytest.raises(RetiredDisposition):
        store.update(old["id"], disposition="note", expect_disposition="note")
    with pytest.raises(ApplyRefused):
        store.apply(SporeApply(op="update", origin_key="k.note",
                               args={"disposition": "note", "expect_disposition": "note"},
                               postcondition={"disposition": "note"}, expected_version=spore_version(store.get(old["id"]))))


def test_disposition_sets_read_vs_write() -> None:
    """VALID_DISPOSITIONS is every value a row may HOLD (flow's parity tuple); WRITE_DISPOSITIONS is what a
    new write may set. The retired names are in the first and never in the second."""
    from levain.spores import RETIRED_DISPOSITIONS, VALID_DISPOSITIONS, WRITE_DISPOSITIONS

    assert VALID_DISPOSITIONS == ("loop", "seed", "handoff", "agenda", "note")
    assert WRITE_DISPOSITIONS == ("loop", "seed", "agenda")
    assert set(RETIRED_DISPOSITIONS) <= set(VALID_DISPOSITIONS) and not set(RETIRED_DISPOSITIONS) & set(WRITE_DISPOSITIONS)
