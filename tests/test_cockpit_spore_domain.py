"""spore_update carries ``domain`` (Phill 10-11: "for 2 yes I agree on the spore thing"): a spore planted
outside the cockpit earns a row label only once text, domain and disposition are each cockpit-fired
(invariant 5), so the cockpit must be able to fire a domain edit. The legacy C1 write has no domain
field and refuses one rather than dropping it."""

from __future__ import annotations

import pytest
from anneal_memory import SporeStore

from levain.cockpit.verbs import dispatch
from levain.writes import EditError, apply_edit
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS
from tests.test_cockpit_k2b_broker import home  # noqa: F401 - fixture
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture


def _fire(rig, b, pid, row_id, verb, params, key):
    row = next(x for x in rig.ck.panel(pid)["rows"] if x["id"] == row_id)
    req = {"verb": verb, "panel_id": pid, "row_id": row_id, "row_version": row["version"],
           "params": params, "intent": "reported", "idempotency_key": key, "confirm": True}
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
                    credential=CRED, install_class=INSTALL_CLASS, route="cockpit", broker=b)


def _row(rig, pid, row_id):
    return next(x for x in rig.ck.panel(pid)["rows"] if x["id"] == row_id)


def test_a_cockpit_domain_edit_lands_and_with_text_and_disposition_labels_the_row(rig_broker) -> None:
    rig, b, _db = rig_broker
    rig.ck.attach_labeller(b.labels)
    pid, row = next((p, r) for p in ("tray",) for r in (rig.ck.panel(p)["rows"] or []))
    sid = row["id"].split(":", 1)[1]
    store = SporeStore(rig.root / ".levain" / "memory.spores.json")
    disposition = store.get(sid).get("disposition")

    _fire(rig, b, pid, row["id"], "spore_update", {"domain": "cockpit-domain"}, "dom-1")
    assert store.get(sid)["domain"] == "cockpit-domain"
    assert _row(rig, pid, row["id"])["provenance"]["fields"]["domain"]["label"] == "claims cockpit token"

    _fire(rig, b, pid, row["id"], "spore_update", {"text": "cockpit text"}, "dom-2")
    # the disposition the store already holds was not cockpit-fired: move it to another Tray list
    assert disposition == "seed"
    _fire(rig, b, pid, row["id"], "spore_set_disposition", {"disposition": "agenda"}, "dom-3")
    fields = _row(rig, pid, row["id"])["provenance"]["fields"]
    assert {fields[f]["label"] for f in ("text", "domain")} == {"claims cockpit token"}
    assert fields["disposition"]["label"] == "claims cockpit token"
    assert _row(rig, pid, row["id"])["provenance"]["label"] == "claims cockpit token"


def test_the_legacy_spore_update_refuses_a_domain_rather_than_dropping_it(rig_broker) -> None:
    rig, _b, _db = rig_broker
    row = next(r for p in ("tray", "loops") for r in (rig.ck.panel(p)["rows"] or []))
    sid = row["id"].split(":", 1)[1]
    store = SporeStore(rig.root / ".levain" / "memory.spores.json")
    before = store.get(sid)
    with pytest.raises(EditError) as exc:
        apply_edit(rig.src.write_scope, {"kind": "spore_update", "spore_id": sid, "text": "t", "domain": "d"})
    assert exc.value.http_status == 422
    assert store.get(sid)["text"] == before["text"] and store.get(sid).get("domain") == before.get("domain")


def _keep_row(rig):
    return next((p, r) for p in ("keep", "loops") for r in (rig.ck.panel(p)["rows"] or []))


def test_the_broker_refuses_a_type_change_outside_the_tray(rig_broker) -> None:
    # L2 HIGH (reproduced): the Tray-only reclassify lock lived only in the legacy write, so a T2 type
    # change on a Keep note or a loop committed through the broker
    rig, b, _db = rig_broker
    pid, row = _keep_row(rig)
    sid = row["id"].split(":", 1)[1]
    store = SporeStore(rig.root / ".levain" / "memory.spores.json")
    before = store.get(sid)["type"]
    new = "question" if before != "question" else "task"
    with pytest.raises(EditError):
        _fire(rig, b, pid, row["id"], "spore_update", {"type": new}, "type-1")
    assert store.get(sid)["type"] == before
    # domain carries no such lock: it lands on the same row
    _fire(rig, b, pid, row["id"], "spore_update", {"domain": "kept"}, "type-2")
    assert store.get(sid)["domain"] == "kept"


@pytest.mark.parametrize("bad", [0, False, [], {}])
def test_a_non_string_domain_is_refused_not_read_as_a_clear(rig_broker, bad) -> None:
    rig, b, _db = rig_broker
    pid, row = next((p, r) for p in ("tray",) for r in (rig.ck.panel(p)["rows"] or []))
    sid = row["id"].split(":", 1)[1]
    store = SporeStore(rig.root / ".levain" / "memory.spores.json")
    _fire(rig, b, pid, row["id"], "spore_update", {"domain": "set"}, f"nd-{bad!r}-0")
    with pytest.raises(EditError):
        _fire(rig, b, pid, row["id"], "spore_update", {"domain": bad}, f"nd-{bad!r}")
    assert store.get(sid)["domain"] == "set"
