"""A phone's plain List line (a *sort*), signed, becomes one coordinator drop (levain 67, key 1108-18): the
``coordinator_drop`` effect on ``CoordDropEffectStore``, through a real Broker, a real passkey-signed device pending and
a real ``CoordStore`` in a tmp dir. Nothing here touches an operator's live apps."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

pytest.importorskip("cryptography")
from anneal_memory.spores import SporeStore  # noqa: E402

from levain import intake  # noqa: E402
from levain.cockpit.broker import Broker, BrokerError  # noqa: E402
from levain.cockpit.coord_effects import (  # noqa: E402
    COORD_CANONICAL,
    COORDINATOR,
    CoordDropEffectStore,
    coordinator_drop_effect,
    drop_resource_key,
)
from levain.cockpit.effects import (  # noqa: E402
    INTAKE_TIERS,
    INTAKE_VERB,
    ApplyOutcome,
    ApplyUnit,
    EffectStatus,
    SporeEffectStore,
    StoreGone,
    create_origin_key,
    registration_refusals,
    settle,
)
from levain.coordinator.store import CoordStore  # noqa: E402
from tests.autonomic_confirm_keys import anchor_scratch  # noqa: E402
from tests.cockpit_authority_rig import RealKey, enrol, enrol_any  # noqa: E402
from tests.test_cockpit_k8a_phone_authority import SoftPasskey  # noqa: E402

PID = "cp" + "ab" * 16
BROWSER = {"class": "token", "name": "browser"}
WORDS = "look at the sticky note on the fridge"


@pytest.fixture
def home() -> Iterator[Path]:
    d = anchor_scratch("sortdrop-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def world(home: Path) -> Iterator[SimpleNamespace]:
    spores = home / "spores.json"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    qdir = home / "dispatch"
    qdir.mkdir(mode=0o700)
    where: list[Path | None] = [qdir]
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores),
                                      COORDINATOR: CoordDropEffectStore(lambda: where[0])},
               store_paths=[spores], install_class="reported")
    b.start()
    keys = home / "keys"
    keys.mkdir(mode=0o700)
    laptop = RealKey(keys, "laptop")
    assert b.db is not None
    enrol_any(b.db, laptop, "laptop-presence")
    phone = SoftPasskey()
    enrol(b.db, phone, "display-phone", by=laptop, custody="passkey")    # type: ignore[arg-type]
    try:
        yield SimpleNamespace(b=b, phone=phone, qdir=qdir, where=where, spores=spores,
                              dev={"class": "device", "fingerprint": phone.fingerprint, "transport": "paired-a"})
    finally:
        b.stop()


def _issue(w: SimpleNamespace, line: str = WORDS) -> dict[str, Any]:
    return w.b.issue(verb=INTAKE_VERB, params={"line": line}, target={"kind": "create", "panel_id": "tray"},
                     tier="C1", credential=w.dev, intent="sign")


def _drops(qdir: Path) -> list[Any]:
    return CoordStore(qdir).drops()


# --- the effect itself ------------------------------------------------------------------------------------------

def test_the_effect_is_one_create_of_the_words_and_refuses_anything_else():
    ok = create_origin_key(PID, 0)
    eff = coordinator_drop_effect({"text": WORDS}, None, origin_key=ok)
    assert (eff.store, eff.resource_key, eff.is_create) == (COORDINATOR, drop_resource_key(ok), True)
    assert dict(eff.changes) == {"text": WORDS} and eff.writes == ("text",)
    assert coordinator_drop_effect({"text": WORDS}, None, origin_key=ok) == eff       # pure
    for bad in ({}, {"text": ""}, {"text": 3}, {"text": "x", "when": "y"}):
        with pytest.raises(ValueError):
            coordinator_drop_effect(bad, None, origin_key=ok)
    with pytest.raises(ValueError):
        coordinator_drop_effect({"text": "x"}, {"drop_id": "d"}, origin_key=ok)
    with pytest.raises(ValueError):
        coordinator_drop_effect({"text": "x"}, None, origin_key=None)
    assert registration_refusals(CoordDropEffectStore(lambda: None), COORD_CANONICAL) == []


def test_the_create_origin_key_fits_the_coordinators_outbox_rule(home):
    ok = create_origin_key(PID, 0)
    assert ok == "ck." + "ab" * 16 + ".0"
    drop, created = CoordStore(home / "d").accept_drop("words", surface="cockpit", outbox_id=ok)
    assert created and drop.outbox_id == ok


def test_applying_twice_is_one_drop_and_the_second_is_already(home):
    st = CoordDropEffectStore(lambda: home / "d")
    eff = coordinator_drop_effect({"text": WORDS}, None, origin_key=create_origin_key(PID, 0))
    unit = ApplyUnit(COORDINATOR, (0,), (eff,))
    first, second = st.apply_unit(unit), st.apply_unit(unit)
    assert (first.outcome, second.outcome) == (ApplyOutcome.APPLIED, ApplyOutcome.ALREADY)
    assert first.members[0].native_id == second.members[0].native_id
    assert [d.words for d in _drops(home / "d")] == [WORDS]
    # a recovery's settle after the write (a crash before the record) reads it as APPLIED, with the label
    res = settle(unit, st, {})
    assert res.status is EffectStatus.APPLIED and res.members[0].label_fields == ("text",)
    assert st.snapshot(eff.resource_key).row["text"] == WORDS


def test_other_words_under_the_same_key_are_refused_not_retried(home):
    st = CoordDropEffectStore(lambda: home / "d")
    ok = create_origin_key(PID, 0)
    st.apply_unit(ApplyUnit(COORDINATOR, (0,), (coordinator_drop_effect({"text": "one"}, None, origin_key=ok),)))
    other = coordinator_drop_effect({"text": "two"}, None, origin_key=ok)
    res = settle(ApplyUnit(COORDINATOR, (0,), (other,)), st, {})
    assert (res.status, res.reason) == (EffectStatus.UNAPPLIED, "refused")
    assert [d.words for d in _drops(home / "d")] == ["one"]


def test_no_directory_is_store_gone_and_a_read_makes_nothing(home):
    st = CoordDropEffectStore(lambda: None)
    eff = coordinator_drop_effect({"text": WORDS}, None, origin_key=create_origin_key(PID, 0))
    with pytest.raises(StoreGone):
        st.apply_unit(ApplyUnit(COORDINATOR, (0,), (eff,)))
    res = settle(ApplyUnit(COORDINATOR, (0,), (eff,)), st, {})
    assert (res.status, res.reason) == (EffectStatus.UNAPPLIED, "store gone")
    absent = CoordDropEffectStore(lambda: home / "nowhere")
    assert absent.snapshot(eff.resource_key).row is None
    assert not (home / "nowhere").exists()


# --- through the broker -----------------------------------------------------------------------------------------

def test_the_tier_table_is_unchanged_a_sort_is_still_c1():
    assert INTAKE_TIERS == {"drop": "T2", "talk": "T2", "sort": "C1"}


def test_a_signed_sort_writes_exactly_one_drop_with_the_words_verbatim(world):
    w = world
    out = _issue(w, f"  {WORDS}  ")
    assert out["state"] == "WAITING" and _drops(w.qdir) == []      # nothing before the signature
    rec = json.loads(out["record"])
    ok = create_origin_key(out["pending_id"], 0)
    assert rec["effects"][0]["store"] == COORDINATOR and rec["effects"][0]["changes"] == {"text": WORDS}
    assert rec["target"]["intake"] == {"kind": "sort", "text": WORDS}
    assert out["receipt_line"].endswith("awaiting your signature") and "desk to sort" in out["receipt_line"]
    done = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert (done["state"], done["decision_kind"]) == ("COMMITTED", "ALLOWED")
    (drop,) = _drops(w.qdir)
    assert (drop.words, drop.surface, drop.outbox_id) == (WORDS, "cockpit", ok)
    assert done["receipt"] == {"kind": "sort", "id": None, "fires": None, "text": WORDS}
    assert done["receipt_line"] == "sent to the desk to sort" and done["drop_id"] == drop.drop_id
    # accept_drop wrote the delivered receipt in the same transaction
    conn = CoordStore(w.qdir).open_readonly()
    try:
        n = conn.execute("SELECT COUNT(*) FROM replies WHERE kind = 'drop_receipt'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    # a second submit of the settled pending is a replay: still one drop
    again = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert again["state"] == "COMMITTED" and len(_drops(w.qdir)) == 1


def test_a_sort_with_no_coordinator_directory_is_a_501_at_issue_and_writes_nothing(world):
    w = world
    w.where[0] = None
    with pytest.raises(BrokerError) as e:
        _issue(w)
    assert (e.value.code, e.value.status) == ("not_built", 501)
    assert "no coordinator store" in str(e.value) and _drops(w.qdir) == []


def test_a_sort_with_no_coordinator_store_registered_is_a_501(home):
    spores = home / "spores.json"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
               install_class="reported")
    b.start()
    try:
        with pytest.raises(BrokerError) as e:
            b.issue(verb=INTAKE_VERB, params={"line": "plain words"}, target={"kind": "create", "panel_id": "tray"},
                    tier="C1", credential={"class": "device", "fingerprint": "SHA256:" + "B" * 43,
                                           "transport": "run"}, intent="sign")
        assert e.value.status == 501
    finally:
        b.stop()


def test_the_store_going_away_between_issue_and_decide_is_unapplied_store_gone(world):
    w = world
    out = _issue(w)
    w.where[0] = None
    done = w.b.submit(out["pending_id"], w.phone.sign(out["record"].encode()))
    assert done["state"] != "COMMITTED"
    assert _drops(w.qdir) == []
    assert "store gone" in json.dumps(w.b.outcome(out["pending_id"]))


def test_a_tampered_reading_does_not_rebuild_the_signed_effect(world):
    w = world
    out = _issue(w)
    rec = json.loads(out["record"])
    signed = rec["effects"][0]
    target = rec["target"]
    same = w.b._build_intake({"line": WORDS}, target, out["pending_id"], at_issue=False)
    assert same.effects[0].to_record() == signed
    # changed words: the rebuild differs from the signed effect, so the decision's compare refuses it
    other = w.b._build_intake({"line": WORDS}, {**target, "intake": {"kind": "sort", "text": "other"}},
                              out["pending_id"], at_issue=False)
    assert other.effects[0].to_record() != signed
    # changed kind: a drop reading carries no minute, so it does not build at all
    with pytest.raises(ValueError):
        w.b._build_intake({"line": WORDS}, {**target, "intake": {"kind": "drop", "text": WORDS}},
                          out["pending_id"], at_issue=False)
    # no reading at all
    with pytest.raises(ValueError):
        w.b._build_intake({"line": WORDS}, {"kind": "create"}, out["pending_id"], at_issue=False)


def test_a_malformed_body_is_still_refused(world):
    w = world
    with pytest.raises(BrokerError) as e:
        w.b.issue(verb=INTAKE_VERB, params={"line": "x", "extra": 1}, target={"kind": "create", "panel_id": "tray"},
                  tier="C1", credential=w.dev, intent="sign")
    assert e.value.status == 400
    assert intake.receipt({"kind": "sort", "text": "x"})[0] == "sent to the desk to sort"
