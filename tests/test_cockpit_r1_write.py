"""R1 write half, server lane (R1_WRITE_PLAN.md "Design r2 TRIAGE" bound fixes and REV 3): the fire is
bound to its preview's tier and gesture, the manifest names the transport, a resolve row action names
its kinds, and a downstream verb fires through a registered EffectStore. Each test was run against the
code before its fix and seen to fail."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit.verbs import dispatch
from tests.test_cockpit_k2a import CRED, INSTALL_CLASS, Rig, _refused, rig  # noqa: F401 - fixture
from tests.test_cockpit_k2b_broker import home  # noqa: F401 - fixture
from tests.test_cockpit_k2b_section import rig_broker  # noqa: F401 - fixture


def _fire(rig: Rig, b: Any, verb: str, panel: str, key: str, params: dict, **env: Any) -> dict:
    r = rig.row(panel, key)
    req = {"verb": verb, "panel_id": panel, "row_id": r["id"], "row_version": r["version"], "params": params,
           **env}
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, req=req,
                    credential=CRED, install_class=INSTALL_CLASS, route="cockpit", broker=b)


# --- 1. tier_seen + gesture_seen ----------------------------------------------------------------


def test_a_fire_with_intent_must_name_the_tier_and_gesture_it_previewed(rig_broker) -> None:
    rig, b, _db = rig_broker
    for env in ({}, {"tier_seen": "T2"}, {"gesture_seen": "direct-reported"}):
        err = _refused("bad_request", _fire, rig, b, "spore_update", "tray", "seed", {"domain": "work"},
                       intent="reported", idempotency_key="k-missing", **env)
        assert "tier_seen" in str(err) or "gesture_seen" in str(err), err
    assert rig.spore("seed").get("domain") in (None, "")


def test_tier_seen_and_gesture_seen_must_be_known_values(rig_broker) -> None:
    rig, b, _db = rig_broker
    for env in ({"tier_seen": "T9", "gesture_seen": "direct-reported"},
                {"tier_seen": "T2", "gesture_seen": "teleport"}):
        err = _refused("bad_request", _fire, rig, b, "spore_update", "tray", "seed", {"domain": "work"},
                       intent="reported", idempotency_key="k-bad", **env)
        assert "T9" in str(err) or "teleport" in str(err), err


def test_a_dry_run_refuses_tier_seen_and_gesture_seen(rig_broker) -> None:
    rig, b, _db = rig_broker
    for env in ({"tier_seen": "T2"}, {"gesture_seen": "direct-reported"}):
        err = _refused("bad_request", _fire, rig, b, "spore_update", "tray", "seed", {"domain": "work"},
                       dry_run=True, **env)
        assert "dry run" in str(err), err


def test_a_previewed_fire_matching_its_preview_lands(rig_broker) -> None:
    rig, b, _db = rig_broker
    pre = _fire(rig, b, "spore_update", "tray", "seed", {"domain": "work"}, dry_run=True)
    out = _fire(rig, b, "spore_update", "tray", "seed", {"domain": "work"}, intent="reported",
                idempotency_key="k-ok", confirm=True, tier_seen=pre["tier"], gesture_seen=pre["gesture"])
    assert (out["state"], out["label"]) == ("COMMITTED", "claims cockpit token"), out
    assert rig.spore("seed")["domain"] == "work"


def test_a_tier_that_moved_since_the_preview_is_refused_tier_moved(rig_broker) -> None:
    """T2 at the preview, C1 at the fire: the page holds a key minted for a T2 fire, which a
    non-idempotent C1 verb would refuse 400; the move answers first."""
    rig, b, _db = rig_broker
    err = _refused("tier_moved", _fire, rig, b, "spore_update", "tray", "seed", {"tier": "hot"},
                   intent="reported", idempotency_key="k-moved", tier_seen="T2", gesture_seen="direct-reported")
    assert err.http_status == 409 and (err.extra["tier"], err.extra["tier_seen"]) == ("C1", "T2"), err.extra
    assert rig.spore("seed").get("tier") != "hot"


def test_a_gesture_that_moved_since_the_preview_is_refused_gesture_moved(rig_broker) -> None:
    rig, b, _db = rig_broker
    err = _refused("gesture_moved", _fire, rig, b, "spore_update", "tray", "seed", {"domain": "work"},
                   intent="reported", idempotency_key="k-g", confirm=True, tier_seen="T2", gesture_seen="sign")
    assert err.http_status == 409 and (err.extra["gesture"], err.extra["gesture_seen"]) == ("direct-reported", "sign")
    assert rig.spore("seed").get("domain") in (None, "")


# --- 2. transport in the manifest ---------------------------------------------------------------


def test_the_manifest_names_the_transport_from_the_substrate_views_flags(tmp_path: Path) -> None:
    from tests.test_cockpit_k2a import _get, _install, _serve
    from levain.dashboard import SubstrateSource

    _root, src, _ids = _install(tmp_path / "w")
    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        man = _get(f"{url}/cockpit/manifest.json", token="T")
        sub = _get(f"{url}/substrate.json", token="T")
    assert man["transport"] == {"writable": sub["writable"], "write_token_required": sub["write_token_required"]}
    assert man["transport"] == {"writable": True, "write_token_required": True}
    assert man["schema"] == "levain.cockpit/1"

    root, _src, _ids = _install(tmp_path / "r")
    ro = dataclasses.replace(SubstrateSource.local(root), write_scope=None)
    with _serve(ro) as (url, _httpd):
        man = _get(f"{url}/cockpit/manifest.json")
    assert man["transport"] == {"writable": False, "write_token_required": False}


def test_the_transport_is_in_the_manifest_etag(rig) -> None:
    a = rig.ck.manifest(CRED, transport={"writable": True, "write_token_required": True})
    b = rig.ck.manifest(CRED, transport={"writable": False, "write_token_required": False})
    assert a["etag"] != b["etag"]


# --- 3. resolve kinds per row -------------------------------------------------------------------


def test_a_resolve_row_action_names_the_kinds_its_rows_type_takes(rig) -> None:
    from anneal_memory.spores import ASCEND_BY_TYPE, DESCEND_BY_TYPE

    rows = rig.ck.panel("loops", credential_class="token")["rows"]
    seen = set()
    for row in rows:
        stype = row["facets"]["spore_type"]
        for a in row["actions"]:
            if a["verb"] == "spore_descend":
                assert a["options"] == {"spore_kind": sorted(DESCEND_BY_TYPE[stype])}, a
            elif a["verb"] == "spore_ascend":
                assert a["options"] == {"spore_kind": sorted(ASCEND_BY_TYPE[stype])}, a
            else:
                assert "options" not in a, a
                continue
            seen.add(a["verb"])
    assert seen == {"spore_descend", "spore_ascend"}
