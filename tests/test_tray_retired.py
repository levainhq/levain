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
    store.update(old["id"], disposition="handoff", expect_disposition="handoff")   # unchanged, proven by the CAS
    # apply(): a planned effect deserialized after an upgrade (a well-formed one: the guard, not a
    # missing expected_version, is what refuses it)
    from anneal_memory.spores import spore_version
    key = row["origin_key"]
    eff = SporeApply(op="update", origin_key=key, args={"disposition": "note"}, postcondition={"disposition": "note"},
                     expected_version=spore_version(store.get(row["id"])))
    with pytest.raises(ApplyRefused, match="Keep is retired"):
        store.apply(eff)
    assert store.get(row["id"])["disposition"] == "seed"
    # crash-after-apply: the create already landed, so the retry reports already, not a refusal
    add = SporeApply(op="add", origin_key=old["origin_key"], args={"type": "thought", "text": "old", "disposition": "handoff"},
                     postcondition={"type": "thought", "text": "old", "disposition": "handoff"})
    assert store.apply(add).outcome == "already"


def test_norm_disposition_missing_or_corrupt_still_invalid() -> None:
    """N2: only an ECHO of a retired value is exempt from 'must be one of'."""
    from levain.cockpit.effects import _norm_disposition

    for bad, cur in ((None, None), ("garbage", "garbage"), ("garbage", "seed"), (None, "seed")):
        with pytest.raises(ValueError, match="must be one of"):
            _norm_disposition(bad, cur)
    assert _norm_disposition("note", "note") == "note"          # a retired echo still passes


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


def test_guarded_update_uses_cas_value_not_an_unlocked_read(tmp_path: Path) -> None:
    """N3: with a CAS sent, the echo decision is made on the CAS value (checked inside anneal's lock)."""
    from anneal_memory.spores import SporeStore

    from levain.spores import RetiredDisposition, guarded_spore_store

    old = SporeStore(tmp_path / "s.json").add(type="thought", text="old", disposition="note")
    store = guarded_spore_store(tmp_path / "s.json")
    store.update(old["id"], disposition="note", expect_disposition="note")           # echo under CAS: passes
    with pytest.raises(RetiredDisposition):
        store.update(old["id"], disposition="note", expect_disposition="seed")       # a seed->note change: refused


# --- the store guard is a BOUND on the persisted value, and an echo is proven only by a CAS ----------------

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


def test_update_echo_without_a_cas_is_refused_even_when_a_read_says_echo(tmp_path: Path, monkeypatch) -> None:
    """The unlocked-read echo is gone: a stale read showing 'note' while the row is really 'seed' must not
    let a seed->note change land."""
    from anneal_memory.spores import SporeStore

    from levain.spores import RetiredDisposition, guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="seed")      # the concurrent re-route
    store = guarded_spore_store(tmp_path / "s.json")
    monkeypatch.setattr(store, "get", lambda _sid: {**old, "disposition": "note"})   # the stale read
    with pytest.raises(RetiredDisposition):
        store.update(old["id"], disposition="note")
    assert SporeStore(tmp_path / "s.json").get(old["id"])["disposition"] == "seed"


def test_update_echo_with_cas_passes_and_a_lost_cas_writes_nothing(tmp_path: Path) -> None:
    from anneal_memory.spores import SporeError, SporeStore

    from levain.spores import guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    store = guarded_spore_store(tmp_path / "s.json")
    store.update(old["id"], disposition="note ", expect_disposition="note", text="edited")   # the persisted form echoes
    assert store.get(old["id"])["disposition"] == "note" and store.get(old["id"])["text"] == "edited"
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="seed")
    with pytest.raises(SporeError):
        store.update(old["id"], disposition="note", expect_disposition="note")   # anneal's lock sees seed
    assert store.get(old["id"])["disposition"] == "seed"


def test_explicit_unset_sentinels(tmp_path: Path) -> None:
    """A wrapper that forwards anneal's _UNSET for every kwarg: a non-disposition edit passes; an echo with
    an _UNSET CAS carries no proof, so it is refused (there is no read to fall back to)."""
    from anneal_memory.spores import _UNSET

    from levain.spores import RetiredDisposition, guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    store = guarded_spore_store(tmp_path / "s.json")
    store.update(old["id"], text="t2", disposition=_UNSET, expect_disposition=_UNSET)
    assert store.get(old["id"])["text"] == "t2"
    with pytest.raises(RetiredDisposition):
        store.update(old["id"], disposition="note", expect_disposition=_UNSET)


def test_apply_update_echo_needs_the_cas_in_args(tmp_path: Path, monkeypatch) -> None:
    """apply(): the echo decision used an unlocked get_by_origin_key; now only args.expect_disposition,
    which anneal checks under its lock, proves it."""
    from anneal_memory.spores import ApplyRefused, SporeApply, SporeStore, spore_version

    from levain.spores import guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    SporeStore(tmp_path / "s.json").update(old["id"], disposition="seed")
    now_row = SporeStore(tmp_path / "s.json").get(old["id"])
    store = guarded_spore_store(tmp_path / "s.json")
    monkeypatch.setattr(store, "get_by_origin_key", lambda _k: {**now_row, "disposition": "note"})  # stale
    eff = SporeApply(op="update", origin_key=old["origin_key"], args={"disposition": "note", "text": "z"},
                     postcondition={"disposition": "note", "text": "z"}, expected_version=spore_version(now_row))
    with pytest.raises(ApplyRefused):
        store.apply(eff)
    assert SporeStore(tmp_path / "s.json").get(old["id"])["disposition"] == "seed"
    # with the CAS: anneal finds seed under its lock, so precondition_lost and nothing written
    eff2 = SporeApply(op="update", origin_key=old["origin_key"],
                      args={"disposition": "note", "text": "z", "expect_disposition": "note"},
                      postcondition={"disposition": "note", "text": "z"}, expected_version=spore_version(now_row))
    assert store.apply(eff2).outcome == "precondition_lost"


def test_apply_update_echo_with_cas_lands_on_a_legacy_row(tmp_path: Path) -> None:
    from anneal_memory.spores import SporeApply, spore_version

    from levain.spores import guarded_spore_store

    old = _legacy(tmp_path / "s.json", "note")
    store = guarded_spore_store(tmp_path / "s.json")
    eff = SporeApply(op="update", origin_key=old["origin_key"],
                     args={"disposition": "note", "next": "2026-12-01", "expect_disposition": "note"},
                     postcondition={"disposition": "note", "next": "2026-12-01"}, expected_version=spore_version(old))
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


def test_add_retry_of_an_existing_key_returns_the_row_even_if_retired(tmp_path: Path) -> None:
    """anneal's add is idempotent by origin_key: a retry of a create that already landed (pre-upgrade, or a
    crash after the write) returns that row. Only a NEW row with a retired value is refused."""
    from levain.spores import RetiredDisposition, guarded_spore_store

    old = _legacy(tmp_path / "s.json", "handoff")
    store = guarded_spore_store(tmp_path / "s.json")
    got = store.add(type="thought", text="old", disposition="handoff", origin_key=old["origin_key"])
    assert got["id"] == old["id"] and got["disposition"] == "handoff"
    with pytest.raises(RetiredDisposition):
        store.add(type="thought", text="new", disposition="handoff", origin_key="k.fresh")
    assert store.get_by_origin_key("k.fresh") is None


def test_cockpit_echo_on_a_legacy_row_carries_the_cas(tmp_path: Path) -> None:
    """The in-repo echo caller: a set_disposition on a legacy note that keeps it a note and moves its date
    must land (the effect sends the CAS), and its SporeApply must carry expect_disposition."""
    from levain.cockpit.effects import SporeEffectStore, canonical_effect, spore_resource_key

    _legacy(tmp_path / "s.json", "note", origin_key="k.note")
    es = SporeEffectStore(tmp_path / "s.json")
    pre = es.snapshot(spore_resource_key("k.note")).row
    eff = canonical_effect("spore_set_disposition", {"disposition": "seed", "surface_at": "2026-12-01"}, pre)
    assert "expect_disposition" not in dict(es.spore_apply(eff).args)     # a real change: no echo CAS
    eff = canonical_effect("spore_update", {"text": "refined"}, pre)
    assert "disposition" not in dict(es.spore_apply(eff).args)
    # a direct echo of the legacy value plus a date: the CAS rides in args, and the write lands
    from levain.cockpit.effects import _existing_effect
    echo = _existing_effect(pre, {"disposition": "note", "next": "2026-12-01"}, es.version_of)
    sa = es.spore_apply(echo)
    assert dict(sa.args)["expect_disposition"] == "note" and "expect_disposition" not in dict(sa.postcondition)
    (unit,) = es.units([(0, echo)])
    res = es.apply_unit(unit)
    assert res.outcome.name in ("APPLIED", "ALREADY")
    row = es.snapshot(spore_resource_key("k.note")).row
    assert row["disposition"] == "note" and row["next"] == "2026-12-01"


def test_disposition_sets_read_vs_write() -> None:
    """VALID_DISPOSITIONS is every value a row may HOLD (flow's parity tuple); WRITE_DISPOSITIONS is what a
    new write may set. The retired names are in the first and never in the second."""
    from levain.spores import RETIRED_DISPOSITIONS, VALID_DISPOSITIONS, WRITE_DISPOSITIONS

    assert VALID_DISPOSITIONS == ("loop", "seed", "handoff", "agenda", "note")
    assert WRITE_DISPOSITIONS == ("loop", "seed", "agenda")
    assert set(RETIRED_DISPOSITIONS) <= set(VALID_DISPOSITIONS) and not set(RETIRED_DISPOSITIONS) & set(WRITE_DISPOSITIONS)
