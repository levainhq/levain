"""K2b-3b L2 findings, each from a reproduced failure (L2 probes, 1011+20): the render key and the
store agree on a key-absent row's disposition; a ``fire:`` edits row carries no label; an unknown install
class never labels ``verified``."""

from __future__ import annotations

import pytest
from anneal_memory import IntentionStore

from levain.cockpit.effects import INTENTION_AUTHORITY_FIELDS, _stored_authority
from levain.cockpit.labels import label_class
from levain.cockpit.verbs import dispatch
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS
from tests.test_cockpit_k2b_broker import home  # noqa: F401 - fixture
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture


def _intentions(rig):
    return IntentionStore(rig.root / ".levain" / "memory.spores.json", id_prefix="intention")


def test_a_key_absent_rows_label_key_hashes_what_the_store_holds(rig_broker) -> None:
    # reproduced: the view named a plain loop's absent disposition "loop" and the store holds None, so
    # no fire could ever label an Open Loops row. Since the loop fold that row is a List row, reading someday,
    # and the key must still hash what the store holds (none), never the rendered default
    rig, _b, _db = rig_broker
    rig.ck.panel("tray")
    keys = rig.ck._state["tray"].snap.label_keys
    assert keys, "the tray panel has keyed rows"
    checked = 0
    for row_id, key in keys.items():
        if key is None:
            continue
        raw = _intentions(rig).get(row_id.split(":", 1)[1])
        assert dict(key.values) == {f: _stored_authority(raw, f) for f in INTENTION_AUTHORITY_FIELDS}
        checked += 1
    assert checked
    # at least one key-absent row was checked, or the test would pass over explicit dispositions alone
    assert any(_intentions(rig).get(r.split(":", 1)[1]).get("disposition") is None
               for r, k in keys.items() if k is not None)


def test_a_fire_edits_row_carries_no_label_and_the_resource_row_tracks_the_store(rig_broker) -> None:
    # reproduced: a fire: row read "claims cockpit token" after the intention's text was hand-edited
    rig, b, _db = rig_broker
    rig.ck.attach_labeller(b.labels)
    rig.ck.attach_edits_reader(b.edits)
    pid, row = next((p, r) for p in ("tray",) for r in (rig.ck.panel(p)["rows"] or []))
    req = {"verb": "intention_update", "panel_id": pid, "row_id": row["id"], "row_version": row["version"],
           "params": {"text": "fired text"}, "intent": "reported", "idempotency_key": "l2-1", "confirm": True,
           "tier_seen": "T2", "gesture_seen": "direct-reported"}
    dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
             credential=CRED, install_class=INSTALL_CLASS, route="cockpit", broker=b)

    def resource():
        return next(x for x in rig.ck.panel(pid)["rows"] if x["id"] == row["id"])

    def fires():
        return [x for x in rig.ck.panel("edits")["rows"] if x["id"].startswith("fire:")]

    assert resource()["provenance"]["fields"]["text"]["label"] == "claims cockpit token"
    assert fires() and all(x["provenance"] is None for x in fires())
    _intentions(rig).update(row["id"].split(":", 1)[1], text="hand edited")
    assert resource()["provenance"]["fields"]["text"]["label"] == "unverified"
    assert all(x["provenance"] is None for x in fires())


def test_an_unknown_install_class_never_labels_verified() -> None:
    assert label_class("ALLOWED", "governed") == "verified"
    assert label_class("ALLOWED", "reported") == "signed · reported"
    for cls in ("", None, "governd"):
        assert label_class("ALLOWED", cls) == "unverified"
