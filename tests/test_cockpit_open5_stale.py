"""HANDOFF OPEN #5 (carried from K2a L2 #5): a spore that moved after render must read ``stale``, not
``not_ascendable`` / ``type_locked`` from a lock-free pre-check. Reproduced on 11d579a (1011+40): every
route below already answered ``stale``, because the row binding (cockpit route: the row left its
panel; ``/edit``: the rendered list) and the broker's version compare run before any pre-check.
This pins that order."""

from __future__ import annotations

from typing import Any

import pytest

from levain.cockpit.verbs import dispatch
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS
from tests.test_cockpit_k2b_broker import home, keydir  # noqa: F401
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401

REPORTED = dict(confirm=True, intent="reported", tier_seen="T2", gesture_seen="direct-reported")


def _code(rig: Any, b: Any, req: dict[str, Any], route: str) -> str:
    with pytest.raises(Exception) as ei:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
                 credential=CRED, install_class=INSTALL_CLASS, route=route, broker=b)
    return getattr(ei.value, "code", None) or getattr(ei.value, "error", None)


def _rendered(rig: Any, verb: str, panel: str, key: str, params: dict[str, Any], idem: str) -> dict[str, Any]:
    r = rig.row(panel, key)
    return {"verb": verb, "panel_id": panel, "params": params, "row_id": r["id"],
            "row_version": r["version"], "idempotency_key": idem, **REPORTED}


def test_a_spore_moved_after_render_reads_stale_on_every_route(rig_broker) -> None:
    rig, b, _db = rig_broker
    ascend = _rendered(rig, "spore_ascend", "loops", "loop", {"spore_kind": "project", "ref": "x"}, "o5-1")
    retype = _rendered(rig, "spore_update", "tray", "seed", {"type": "question"}, "o5-2")
    rig.store.update(rig.ids["loop"], disposition="note")      # the loop became a Keep note
    rig.store.update(rig.ids["seed"], disposition=None)        # the seed left the Tray
    assert _code(rig, b, ascend, "cockpit") == "stale"
    assert _code(rig, b, retype, "cockpit") == "stale"

    rig.store.update(rig.ids["parked"], disposition="note")
    rig.store.update(rig.ids["held"], disposition=None)
    assert _code(rig, b, {"kind": "spore_ascend", "spore_id": rig.ids["parked"], "spore_kind": "project",
                          "ref": "x", "confirm": True, "expect_disposition": None}, "edit") == "stale"
    assert _code(rig, b, {"kind": "spore_update", "spore_id": rig.ids["held"], "type": "question",
                          "expect_disposition": "seed"}, "edit") == "stale"
