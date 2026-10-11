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


# --- 4. the downstream effect seam --------------------------------------------------------------

INBOX_PANELS = [{"id": "inbox", "title": "Inbox", "lines": [], "empty": "Compose a note above.",
                 "action": {"verb": "send_inbox", "fields": [{"name": "body"}, {"name": "for"}]}}]


def _send_inbox_verb(ran: list) -> Any:
    from levain.writes import ActionVerb

    return ActionVerb(handler=lambda p: ran.append(p) or {"summary": "sent"}, label="send")


def _inbox_server(src: Any, ran: list, **kw: Any) -> Any:
    from tests.test_cockpit_k2a import _serve

    return _serve(src, extra_verbs={"send_inbox": _send_inbox_verb(ran)}, extra_panels=lambda: INBOX_PANELS,
                  surface_tokens={"browser": "T"}, install_class="reported", **kw)


def _inbox_panel(url: str) -> str:
    from tests.test_cockpit_k2a import _get

    man = _get(f"{url}/cockpit/manifest.json", token="T")
    (pid,) = [p for p in man["panels"] if p.endswith("inbox") and p != "inbox"]
    return pid


def test_a_reported_send_inbox_lands_one_labelled_note_and_a_replay_lands_none(tmp_path: Path) -> None:
    from tests._inbox_fake import inbox_downstream
    from tests.test_cockpit_k2a import _install, _post

    _root, src, _ids = _install(tmp_path)
    store, effect = inbox_downstream()
    ran: list = []
    with _inbox_server(src, ran, extra_effects={"send_inbox": effect}) as (url, _httpd):
        pid = _inbox_panel(url)
        body = {"verb": "send_inbox", "panel_id": pid, "params": {"body": "hello from the page", "for": "cli"},
                "confirm": True}
        status, pre = _post(f"{url}/cockpit/verb", {**body, "dry_run": True}, token="T")
        assert (status, pre["tier"], pre["gesture"]) == (200, "T2", "direct-reported"), pre
        fire = {**body, "intent": "reported", "idempotency_key": "send-1", "tier_seen": pre["tier"],
                "gesture_seen": pre["gesture"]}
        status, out = _post(f"{url}/cockpit/verb", fire, token="T")
        assert (status, out["state"], out["label"]) == (200, "COMMITTED", "claims cockpit token"), out
        status, again = _post(f"{url}/cockpit/verb", fire, token="T")
        assert (status, again["pending_id"], again["replay"]) == (200, out["pending_id"], True), again
    notes = list(store.notes.values())
    assert [(n["body"], n["for"]) for n in notes] == [("hello from the page", "cli")]
    assert notes[0]["origin_key"] == "ck." + out["pending_id"].removeprefix("cp") + ".0"
    assert ran == [], "a broker-fired downstream verb never runs its /action handler"


def test_a_store_less_downstream_t2_verb_stays_not_built(tmp_path: Path) -> None:
    from tests.test_cockpit_k2a import _install, _post

    _root, src, _ids = _install(tmp_path)
    ran: list = []
    with _inbox_server(src, ran) as (url, _httpd):
        pid = _inbox_panel(url)
        status, out = _post(f"{url}/cockpit/verb", {
            "verb": "send_inbox", "panel_id": pid, "params": {"body": "x"}, "confirm": True, "intent": "reported",
            "idempotency_key": "k", "tier_seen": "T2", "gesture_seen": "direct-reported"}, token="T")
        assert (status, out["error"]) == (501, "not_built"), out
    assert ran == []


def test_a_downstream_effect_is_refused_at_server_construction(tmp_path: Path) -> None:
    from levain.cockpit.effects import DownstreamEffect
    from levain.web_server import make_server
    from tests._inbox_fake import FakeInbox, inbox_downstream, send_inbox_effect
    from tests.test_cockpit_k2a import _install

    _root, src, _ids = _install(tmp_path)
    verbs = {"send_inbox": _send_inbox_verb([])}

    class NoAuthority(FakeInbox):
        authority_fields = ()

    class Squatter(FakeInbox):
        name = "spores"

    cases = {
        "no authority_fields": {"send_inbox": DownstreamEffect(NoAuthority(), send_inbox_effect)},
        "reserved": {"send_inbox": DownstreamEffect(Squatter(), send_inbox_effect)},
        "not a downstream verb": {"spore_seed": inbox_downstream()[1]},
        "no such verb": {"nope": inbox_downstream()[1]},
        "no canonical_effect": {"send_inbox": DownstreamEffect(FakeInbox(), None)},  # type: ignore[arg-type]
        "not a DownstreamEffect": {"send_inbox": FakeInbox()},
    }
    for why, effects in cases.items():
        with pytest.raises(ValueError):
            make_server(src, host="127.0.0.1", port=0, extra_verbs=verbs, extra_effects=effects,
                        install_class="reported")
            pytest.fail(f"{why}: not refused")
    # two verbs on two different stores under one name
    verbs2 = {**verbs, "send_other": _send_inbox_verb([])}
    with pytest.raises(ValueError, match="inbox"):
        make_server(src, host="127.0.0.1", port=0, extra_verbs=verbs2, install_class="reported",
                    extra_effects={"send_inbox": inbox_downstream()[1], "send_other": inbox_downstream()[1]})


@pytest.mark.parametrize("point", ["issued", "verified", "decided", "applied:0", "journaled:0", "finished"])
def test_a_killed_send_inbox_applies_exactly_once_after_recovery(home: Path, point: str) -> None:  # noqa: F811
    import json
    import subprocess
    import sys

    from anneal_memory.spores import SporeStore
    from levain.cockpit.broker import Broker
    from levain.cockpit.effects import SporeEffectStore
    from tests._inbox_fake import inbox_downstream
    from tests.test_cockpit_k2b_crash import ENTRY, _ops, _wait_state

    spores, inbox, data = home / "spores.json", home / "inbox.json", home / "data"
    SporeStore(spores).add(type="task", text="a loop", origin_key="k.loop")
    params, target = {"body": "killed mid-send", "for": "cli"}, {"kind": "panel", "panel_id": "ext:inbox"}
    cfg = {"data_root": str(data), "spores": str(spores), "inbox": str(inbox), "kill_at": point,
           "verb": "send_inbox", "params": params, "target": target, "idempotency_key": "send-k"}
    proc = subprocess.run([sys.executable, str(ENTRY), json.dumps(cfg)], capture_output=True, text=True,
                          timeout=120, cwd=str(Path(__file__).parent.parent))
    assert proc.returncode == 137, f"the kill point {point!r} did not fire: rc={proc.returncode}\n{proc.stderr}"
    (pid, _state), = _ops(data)

    def broker() -> tuple[Any, Broker]:
        store, effect = inbox_downstream(inbox)
        b = Broker(data, stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
                   install_class="reported", downstream={"send_inbox": effect})
        b.start()
        return store, b

    store, b = broker()
    try:
        b.recover()
        if point in ("issued", "verified"):
            assert store.notes == {}, "an undecided pending applies nothing"
            out = b.issue(verb="send_inbox", params=params, target=target, tier="T2",
                          credential={"class": "token", "name": "browser"}, intent="reported",
                          idempotency_key="send-k")
            assert out["pending_id"] == pid
        assert _wait_state(data, pid, "COMMITTED"), _ops(data)
    finally:
        b.stop()
    store, b2 = broker()
    try:
        b2.recover()
    finally:
        b2.stop()
    assert [(n["body"], n["origin_key"]) for n in store.notes.values()] == \
        [("killed mid-send", "ck." + pid.removeprefix("cp") + ".0")]
    assert _ops(data) == [(pid, "COMMITTED")]
