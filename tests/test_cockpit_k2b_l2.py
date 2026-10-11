"""K2b-3b L2 findings, each from a reproduced failure (L2 probes, 1011+20): the render key and the
store agree on a plain loop's disposition; a ``fire:`` edits row carries no label; an unknown install
class never labels ``verified``."""

from __future__ import annotations

from anneal_memory import SporeStore

from levain.cockpit.effects import SPORE_AUTHORITY_FIELDS, _stored_authority
from levain.cockpit.labels import label_class
from levain.cockpit.verbs import dispatch
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS
from tests.test_cockpit_k2b_broker import home  # noqa: F401 - fixture
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture


def _spores(rig):
    return SporeStore(rig.root / ".levain" / "memory.spores.json")


def test_a_loop_rows_label_key_hashes_what_the_store_holds(rig_broker) -> None:
    # reproduced: the view named a plain loop's absent disposition "loop" and the store holds None, so
    # no fire could ever label an Open Loops row
    rig, _b, _db = rig_broker
    rig.ck.panel("loops")
    keys = rig.ck._state["loops"].snap.label_keys
    assert keys, "the loops panel has keyed rows"
    checked = 0
    for row_id, key in keys.items():
        if key is None:
            continue
        raw = _spores(rig).get(row_id.split(":", 1)[1])
        assert dict(key.values) == {f: _stored_authority(raw, f) for f in SPORE_AUTHORITY_FIELDS}
        checked += 1
    assert checked


def test_a_fire_edits_row_carries_no_label_and_the_resource_row_tracks_the_store(rig_broker) -> None:
    # reproduced: a fire: row read "claims cockpit token" after the spore's text was hand-edited
    rig, b, _db = rig_broker
    rig.ck.attach_labeller(b.labels)
    rig.ck.attach_edits_reader(b.edits)
    pid, row = next((p, r) for p in ("loops", "tray", "keep") for r in (rig.ck.panel(p)["rows"] or []))
    req = {"verb": "spore_update", "panel_id": pid, "row_id": row["id"], "row_version": row["version"],
           "params": {"text": "fired text"}, "intent": "reported", "idempotency_key": "l2-1", "confirm": True}
    dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
             credential=CRED, install_class=INSTALL_CLASS, route="cockpit", broker=b)

    def resource():
        return next(x for x in rig.ck.panel(pid)["rows"] if x["id"] == row["id"])

    def fires():
        return [x for x in rig.ck.panel("edits")["rows"] if x["id"].startswith("fire:")]

    assert resource()["provenance"]["fields"]["text"]["label"] == "claims cockpit token"
    assert fires() and all(x["provenance"] is None for x in fires())
    _spores(rig).update(row["id"].split(":", 1)[1], text="hand edited")
    assert resource()["provenance"]["fields"]["text"]["label"] == "unverified"
    assert all(x["provenance"] is None for x in fires())


def test_an_unknown_install_class_never_labels_verified() -> None:
    assert label_class("ALLOWED", "governed") == "verified"
    assert label_class("ALLOWED", "reported") == "signed · reported"
    for cls in ("", None, "governd"):
        assert label_class("ALLOWED", cls) == "unverified"
