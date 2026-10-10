"""K2b-1 spore effects (design §4.0-§4.1): settle over the SporeEffectStore against real anneal
SporeStores in tmp dirs. Divergent values are injected by wrapping the args the adapter hands to
anneal, never by mocking anneal."""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path

import pytest
from anneal_memory.spores import SporeStore

from levain.cockpit.effects import (
    ApplyUnit,
    Effect,
    EffectState,
    EffectStatus,
    PermanentApplyRefusal,
    SporeEffectStore,
    TransientApplyFault,
    canonical_effect,
    create_origin_key,
    plan_units,
    registration_refusals,
    settle,
    spore_resource_key,
)

PENDING = "cp" + "ab" * 16


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    p = tmp_path / "spores.json"
    st = SporeStore(p)
    st.add(type="task", text="a loop", origin_key="k.loop")
    st.add(type="thought", text="a tray seed", disposition="seed", origin_key="k.seed")
    st.add(type="thought", text="a note", disposition="note", origin_key="k.note")
    return p


def _row(path: Path, key: str) -> dict | None:
    return SporeStore(path).get_by_origin_key(key)


def _effect(es: SporeEffectStore, verb: str, params: dict, key: str | None, *, idx: int = 0) -> Effect:
    if verb == "spore_seed":
        ok = create_origin_key(PENDING, idx)
        return canonical_effect(verb, params, es.snapshot(spore_resource_key(ok)).row, origin_key=ok)
    assert key is not None
    return canonical_effect(verb, params, es.snapshot(spore_resource_key(key)).row)


def _settle(es: SporeEffectStore, eff: Effect, idx: int = 0, prior: dict | None = None):
    (unit,) = es.units([(idx, eff)])
    return settle(unit, es, prior or {})


# every spore kind of the §13 row, with the stored check of its applied state
KINDS = [
    ("seed", "spore_seed", {"text": "  new capture  ", "type": "task"}, None,
     lambda r: r["text"] == "new capture" and r["disposition"] == "seed" and r["status"] == "open"),
    ("text", "spore_update", {"text": "edited\r\n"}, "k.loop", lambda r: r["text"] == "edited"),
    ("domain", "spore_update", {"domain": "levain"}, "k.loop", lambda r: r["domain"] == "levain"),
    ("type", "spore_update", {"type": "question"}, "k.seed", lambda r: r["type"] == "question"),
    ("disposition", "spore_set_disposition", {"disposition": "loop", "surface_at": "2026-12-01"}, "k.seed",
     lambda r: "disposition" not in r and r["next"] == "2026-12-01"),
    ("pull_forward", "spore_surface_at", {"surface_at": "2026-10-11"}, "k.loop",
     lambda r: r["next"] == "2026-10-11"),
    ("ascend", "spore_ascend", {"spore_kind": "project", "ref": "commit abc"}, "k.loop",
     lambda r: r["status"] == "resolved" and r["resolution"]["ref"] == "commit abc"),
    ("undo_edit", "spore_undo_edit", {"restore": {"text": "a loop", "tier": "cold"}}, "k.loop",
     lambda r: r["tier"] == "cold"),
]


@pytest.mark.parametrize("name,verb,params,key,check", KINDS, ids=[k[0] for k in KINDS])
def test_each_kind_applies_then_reapplies_as_already(path, name, verb, params, key, check):
    es = SporeEffectStore(path)
    eff = _effect(es, verb, params, key)
    first = _settle(es, eff)
    assert first.status is EffectStatus.APPLIED, first
    okey = key or create_origin_key(PENDING, 0)
    row = _row(path, okey)
    assert row is not None and check(row)
    before = path.read_bytes()
    again = _settle(es, eff)
    assert again.status is EffectStatus.APPLIED
    assert path.read_bytes() == before
    if verb == "spore_seed":
        assert first.members[0].native_id == row["id"] == again.members[0].native_id
        assert set(first.members[0].label_fields) == {"text", "disposition"}


def test_ascend_kind_validated_against_spore_type(path):
    es = SporeEffectStore(path)
    with pytest.raises(ValueError):
        _effect(es, "spore_ascend", {"spore_kind": "nonsense", "ref": "x"}, "k.loop")
    with pytest.raises(ValueError):
        _effect(es, "spore_ascend", {"spore_kind": "project", "ref": "x"}, "k.note")


def test_undo_of_a_seed_deletes_then_already(path):
    es = SporeEffectStore(path)
    seed = _effect(es, "spore_seed", {"text": "oops"}, None)
    assert _settle(es, seed).status is EffectStatus.APPLIED
    key = create_origin_key(PENDING, 0)
    undo = _effect(es, "spore_undo_seed", {}, key)
    assert undo.changes == {"deleted": True} and undo.projection is None
    r = _settle(es, undo)
    assert r.status is EffectStatus.APPLIED and r.members[0].label_fields == ()
    assert _row(path, key) is None
    before = path.read_bytes()
    assert _settle(es, undo).status is EffectStatus.APPLIED
    assert path.read_bytes() == before
    # the create, settled again after the delete, is still ALREADY: the key is never reused
    assert _settle(es, seed).status is EffectStatus.APPLIED
    assert _row(path, key) is None


@pytest.mark.parametrize("other", ["edit", "resolve"])
def test_create_found_by_origin_key_after_another_writer_is_already(path, other):
    es = SporeEffectStore(path)
    seed = _effect(es, "spore_seed", {"text": "planted", "disposition": "agenda"}, None)
    first = _settle(es, seed)
    native = first.members[0].native_id
    st = SporeStore(path)
    if other == "edit":
        st.update(native, text="rewritten by the CLI")
    else:
        st.update(native, disposition=None)
        st.descend(native, kind="composted")
    r = _settle(es, seed)
    assert r.status is EffectStatus.APPLIED
    assert r.members[0].native_id == native
    labels = set(r.members[0].label_fields)
    if other == "edit":
        assert labels == {"disposition"}            # text moved: no label on it
    else:
        assert labels == {"text"}                    # disposition moved
    assert len(st.list_open()) + len(json.loads(path.read_text())["resolved"]) == 4


def test_precondition_moved_is_changed(path):
    es = SporeEffectStore(path)
    eff = _effect(es, "spore_update", {"text": "mine"}, "k.loop")
    row = _row(path, "k.loop")
    SporeStore(path).update(row["id"], tier="hot")
    before = path.read_bytes()
    r = _settle(es, eff)
    assert (r.status, r.reason) == (EffectStatus.UNAPPLIED, "changed")
    assert path.read_bytes() == before


def test_resolved_by_another_writer_is_changed(path):
    es = SporeEffectStore(path)
    eff = _effect(es, "spore_update", {"text": "mine"}, "k.loop")
    SporeStore(path).descend(_row(path, "k.loop")["id"], kind="composted")
    assert _settle(es, eff).reason == "changed"


class _Divergent(SporeEffectStore):
    """Writes ``value`` instead of the signed value of ``field`` (a nested ``resolution`` field is
    named ``resolution.<leaf>``), leaving the postcondition the signed one."""

    def __init__(self, path, field: str, value):
        super().__init__(path)
        self.field, self.value = field, value

    def spore_apply(self, effect):
        sa = super().spore_apply(effect)
        args = dict(sa.args)
        leaf = self.field.split(".")[-1]
        assert leaf in args, (self.field, args)
        args[leaf] = self.value
        return dataclasses.replace(sa, args=args)


DIVERGENT = [
    ("seed.text", "spore_seed", {"text": "signed"}, None, "text", "not signed"),
    ("seed.type", "spore_seed", {"text": "signed", "type": "task"}, None, "type", "question"),
    ("seed.disposition", "spore_seed", {"text": "signed"}, None, "disposition", "handoff"),
    ("update.text", "spore_update", {"text": "signed"}, "k.loop", "text", "other"),
    ("update.domain", "spore_update", {"domain": "signed"}, "k.loop", "domain", "other"),
    ("update.type", "spore_update", {"type": "question"}, "k.seed", "type", "task"),
    ("update.tier", "spore_update", {"tier": "hot"}, "k.loop", "tier", "cold"),
    ("update.next", "spore_update", {"next": "2026-11-01"}, "k.loop", "next", "2026-11-02"),
    ("disposition.disposition", "spore_set_disposition", {"disposition": "handoff"}, "k.seed",
     "disposition", "agenda"),
    ("disposition.next", "spore_set_disposition", {"disposition": "note"}, "k.seed", "next", "2026-11-02"),
    ("pull_forward.next", "spore_surface_at", {"surface_at": "2026-10-12"}, "k.loop", "next", "2026-10-13"),
    ("descend.kind", "spore_descend", {"spore_kind": "composted"}, "k.loop", "resolution.kind", "dropped"),
    ("ascend.kind", "spore_ascend", {"spore_kind": "project", "ref": "r"}, "k.loop",
     "resolution.kind", "thread"),
    ("ascend.ref", "spore_ascend", {"spore_kind": "project", "ref": "r"}, "k.loop", "resolution.ref", "r2"),
    ("undo_edit.text", "spore_undo_edit", {"restore": {"text": "back"}}, "k.loop", "text", "elsewhere"),
]


@pytest.mark.parametrize("name,verb,params,key,fld,value", DIVERGENT, ids=[d[0] for d in DIVERGENT])
def test_divergent_value_on_each_changes_field_is_refused_in_transaction(path, name, verb, params, key, fld,
                                                                         value):
    from anneal_memory.spores import ASCEND_BY_TYPE, DESCEND_BY_TYPE
    if fld == "resolution.kind":   # the injected kind must be one anneal takes for the type
        valid = (ASCEND_BY_TYPE if verb == "spore_ascend" else DESCEND_BY_TYPE)["task"]
        assert value in valid
    good = SporeEffectStore(path)
    eff = _effect(good, verb, params, key)
    before = path.read_bytes()
    r = _settle(_Divergent(path, fld, value), eff)
    assert (r.status, r.reason) == (EffectStatus.UNAPPLIED, "refused"), r
    assert path.read_bytes() == before


def test_divergent_symbolic_ascend_ref_is_refused(path):
    """An ascend whose loop ``ref`` names effect 0, the seed this same fire creates: the store must
    hold exactly the created id, and any other id is refused."""
    es = SporeEffectStore(path)
    seed = _effect(es, "spore_seed", {"text": "what it became"}, None)
    asc = _effect(es, "spore_ascend", {"spore_kind": "project", "ref": {"from_effect": 0, "field": "native_id"}},
                  "k.loop", idx=1)
    assert asc.changes["resolution"]["ref"] == {"from_effect": 0, "field": "native_id"}
    r0 = _settle(es, seed)
    prior = {0: EffectState(EffectStatus.APPLIED, r0.members[0].native_id)}
    before = path.read_bytes()
    bad = _settle(_Divergent(path, "resolution.ref", "spore-1"), asc, idx=1, prior=prior)
    assert (bad.status, bad.reason) == (EffectStatus.UNAPPLIED, "refused")
    assert path.read_bytes() == before
    ok = _settle(es, asc, idx=1, prior=prior)
    assert ok.status is EffectStatus.APPLIED
    assert _row(path, "k.loop")["resolution"]["ref"] == r0.members[0].native_id


def test_symbolic_ref_waits_on_planned_and_fails_on_unapplied(path):
    es = SporeEffectStore(path)
    asc = _effect(es, "spore_ascend", {"spore_kind": "project", "ref": {"from_effect": 0, "field": "native_id"}},
                  "k.loop", idx=1)
    before = path.read_bytes()
    wait = _settle(es, asc, idx=1, prior={0: EffectState(EffectStatus.PLANNED)})
    assert (wait.status, wait.reason) == (EffectStatus.PLANNED, None)
    dep = _settle(es, asc, idx=1, prior={0: EffectState(EffectStatus.UNAPPLIED)})
    assert (dep.status, dep.reason) == (EffectStatus.UNAPPLIED, "dependency")
    bad = _settle(es, asc, idx=0, prior={})     # a ref to itself
    assert (bad.status, bad.reason) == (EffectStatus.UNAPPLIED, "refused")
    assert path.read_bytes() == before


class _ExtraField(SporeEffectStore):
    """Also writes ``tier``, a version field the effect does not change."""

    def spore_apply(self, effect):
        sa = super().spore_apply(effect)
        return dataclasses.replace(sa, args={**sa.args, "tier": "parked"})


def test_write_of_a_non_changes_version_field_is_refused(path):
    eff = _effect(SporeEffectStore(path), "spore_update", {"text": "signed"}, "k.loop")
    before = path.read_bytes()
    r = _settle(_ExtraField(path), eff)
    assert (r.status, r.reason) == (EffectStatus.UNAPPLIED, "refused")
    assert path.read_bytes() == before


def test_corrupt_store_is_transient_then_applies_after_repair(path):
    es = SporeEffectStore(path)
    eff = _effect(es, "spore_update", {"text": "survives"}, "k.loop")
    good = path.read_bytes()
    path.write_text("{ not json")
    with pytest.raises(TransientApplyFault) as fault:
        _settle(es, eff)
    assert fault.value.fault_class == "SporeError"
    path.write_bytes(good)
    assert _settle(es, eff).status is EffectStatus.APPLIED
    assert _row(path, "k.loop")["text"] == "survives"


def test_missing_store_is_store_gone(path):
    es = SporeEffectStore(path)
    eff = _effect(es, "spore_seed", {"text": "x"}, None)
    path.unlink()
    r = _settle(es, eff)
    assert (r.status, r.reason) == (EffectStatus.UNAPPLIED, "store gone")
    assert not path.exists()


class _Boom(SporeEffectStore):
    def apply_unit(self, unit):
        raise RuntimeError("a bug")


def test_any_other_exception_is_transient(path):
    eff = _effect(SporeEffectStore(path), "spore_update", {"text": "x"}, "k.loop")
    with pytest.raises(TransientApplyFault) as fault:
        _settle(_Boom(path), eff)
    assert (fault.value.fault_class, fault.value.message) == ("RuntimeError", "a bug")


@pytest.mark.parametrize("verb,params,key", [
    ("spore_seed", {"text": "raced"}, None),
    ("spore_update", {"text": "raced"}, "k.loop"),
    ("spore_ascend", {"spore_kind": "project", "ref": "r"}, "k.loop"),
], ids=["seed", "update", "ascend"])
def test_two_concurrent_settlers_both_end_applied(path, verb, params, key):
    eff = _effect(SporeEffectStore(path), verb, params, key)
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def run() -> None:
        try:
            es = SporeEffectStore(path)            # own SporeStore, own lock fd
            es.snapshot(eff.resource_key)          # the lock-free read
            barrier.wait(timeout=10)
            results.append(_settle(es, eff))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors, errors
    assert [r.status for r in results] == [EffectStatus.APPLIED, EffectStatus.APPLIED]
    if verb == "spore_seed":
        assert results[0].members[0].native_id == results[1].members[0].native_id
        data = json.loads(path.read_text())
        assert sum(1 for s in data["spores"] if s.get("text") == "raced") == 1


def test_effect_record_round_trip_and_units(path):
    es = SporeEffectStore(path)
    effs = [_effect(es, "spore_seed", {"text": "a"}, None),
            _effect(es, "spore_ascend", {"spore_kind": "project", "ref": {"from_effect": 0, "field": "native_id"}},
                    "k.loop", idx=1)]
    assert [Effect.from_record(json.loads(json.dumps(e.to_record()))) for e in effs] == effs
    units = plan_units(effs, {"spores": es})
    assert [u.indices for u in units] == [(0,), (1,)]
    with pytest.raises(ValueError):
        Effect.from_record({"resource_key": "x"})
    with pytest.raises(PermanentApplyRefusal):
        es.apply_unit(ApplyUnit("spores", (0, 1), tuple(effs)))


def test_registration_declarations(path):
    es = SporeEffectStore(path)
    assert registration_refusals(es, {"spore_update": object()}) == []
    assert registration_refusals(object(), {"x": object()}) == ["no EffectStore adapter"]
    assert "no canonical_effect" in registration_refusals(es, {})
    assert set(es.authority_fields) <= set(es.version_fields)
