"""K2a: the verb registry and C1 (design: flow ``projects/levain/reference/cockpit_manifest_DESIGN_1009.md``
§4.1, §4.2, §4.4, §9 K2a). Each test quotes the §9 K2a DONE line it holds. Lines this file does not
hold are named at the bottom with where they are held instead."""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain.cockpit import verbs as verbs_mod
from levain.cockpit.engine import NOW_ID, ProviderSpec
from levain.cockpit.results import Absent, Read
from levain.cockpit.providers import build_default_cockpit
from levain.cockpit.verbs import VerbRegistry, VerbSpec, VerbView, dispatch
from levain.dashboard import SubstrateSource
from levain.web_server import INSTALL_CLASS, make_server
from levain.writes import ActionVerb, EditError
from tests.test_dashboard import _make_levain_install

TOKEN = "k2a-browser-token"
TODAY = date(2026, 10, 10)
CRED = {"class": "token", "name": "browser", "device_class": None}


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, d: date) -> None:
        self.now = _at(d)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: int) -> None:
        self.now += timedelta(days=days)


def _install(tmp_path: Path) -> tuple[Path, SubstrateSource, dict[str, str]]:
    root = _make_levain_install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    ids = {
        "seed": store.add(type="task", text="a tray seed", disposition="seed", today=TODAY)["id"],
        "held": store.add(type="task", text="a held seed", disposition="seed",
                          next=(TODAY + timedelta(days=5)).isoformat(), today=TODAY)["id"],
        "loop": store.add(type="task", text="a loop", tier="hot", today=TODAY)["id"],
        "parked": store.add(type="task", text="a parked loop", tier="parked", today=TODAY)["id"],
        "note": store.add(type="task", text="a keep note", disposition="note", today=TODAY)["id"],
    }
    (root / ".levain" / "context.json").write_text(json.dumps(
        {"state": "fine", "state_set_at": datetime.now(timezone.utc).isoformat(), "state_source": "cli"}))
    return root, SubstrateSource.local(root), ids


class Rig:
    """The kernel's own cockpit over a real install, its registry and a controllable clock."""

    def __init__(self, tmp_path: Path, extra: dict[str, Any] | None = None) -> None:
        self.root, self.src, self.ids = _install(tmp_path)
        self.clock = Clock(TODAY)
        self.ck = build_default_cockpit(self.src, clock=self.clock)
        self.reg = VerbRegistry(extra)
        self.ck.attach_verbs(VerbView(self.reg, self.ck, INSTALL_CLASS))
        self.store = SporeStore(self.root / ".levain" / "memory.spores.json")

    def row(self, panel: str, key: str) -> dict[str, Any]:
        res = self.ck.read_one(panel, f"spore:{self.ids[key]}")
        return res.value

    def post(self, verb: str, panel: str, key: str | None = None, params: dict | None = None, **env: Any) -> dict:
        req: dict[str, Any] = {"verb": verb, "panel_id": panel, "params": params or {}}
        if key is not None:
            r = self.row(panel, key)
            req.update(row_id=r["id"], row_version=r["version"])
        req.update(env)
        return dispatch(registry=self.reg, cockpit=self.ck, scope=self.src.write_scope, req=req,
                        credential=CRED, install_class=INSTALL_CLASS, route="cockpit")

    def tier(self, verb: str, panel: str, key: str | None = None, params: dict | None = None) -> str:
        return self.post(verb, panel, key, params, dry_run=True)["tier"]

    def spore(self, key: str) -> dict[str, Any]:
        return self.store.get(self.ids[key])

    def edit(self, req: dict[str, Any]) -> dict:
        return dispatch(registry=self.reg, cockpit=self.ck, scope=self.src.write_scope, req=req,
                        credential=CRED, install_class=INSTALL_CLASS, route="edit")

    def panel_ids(self, panel: str) -> list[str]:
        return [r["id"] for r in (self.ck.panel(panel, credential_class="token") or {}).get("rows") or []]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _refused(code: str, fn, *a, **kw) -> EditError:
    with pytest.raises(EditError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, f"{ei.value.code}: {ei.value}"
    return ei.value


@contextmanager
def _serve(src: SubstrateSource, **kw: Any):
    httpd = make_server(src, host="127.0.0.1", port=0, **kw)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _post(url: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(url: str, token: str | None = None) -> dict:
    headers = {"X-Levain-Write-Token": token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:  # noqa: S310
        return json.loads(r.read())


# --- the credential and the POST handler ---------------------------------------------------------


T2_T3_EDIT_BODIES = {
    "config": {"kind": "config", "source": "seed/world.md", "heading": "X", "expected_body": "", "new_body": "y"},
    "entity_name": {"kind": "entity_name", "value": "n", "expected": None},
    "state": {"kind": "state", "heading": "State", "expected_body": "", "new_body": "y"},
    "spore_seed": {"kind": "spore_seed", "text": "planted"},
    "spore_set_disposition": {"kind": "spore_set_disposition", "disposition": "note"},
    "spore_ascend": {"kind": "spore_ascend", "spore_kind": "x", "ref": "r", "confirm": True},
    "spore_update_text": {"kind": "spore_update", "text": "new words"},
    "episode_tombstone": {"kind": "episode_tombstone", "episode_id": "e1", "confirm": True},
    "undo": {"kind": "undo", "edit_id": "nope"},
}


class TestCredentialAndTiers:
    def test_no_token_refused_and_every_t2_t3_edit_kind_refused(self, tmp_path: Path) -> None:
        """'as a SECOND local uid with no token: a C1 POST to 127.0.0.1 is refused, and POST /edit for
        every T2 and T3 kind is refused' — the no-token half and the per-kind refusal; the second-uid
        RUN is residue (see the bottom of this file)."""
        _root, src, ids = _install(tmp_path)
        before = (_root / ".levain" / "memory.spores.json").read_text()
        with _serve(src, write_token=TOKEN) as (base, _h):
            touch = {"kind": "spore_touch", "spore_id": ids["seed"]}
            st, body = _post(f"{base}/edit", touch)
            assert (st, body["error"]) == (403, "credential_required")
            st, body = _post(f"{base}/cockpit/verb", {"verb": "spore_touch", "panel_id": "tray"})
            assert (st, body["error"]) == (403, "credential_required")
            for name, b in T2_T3_EDIT_BODIES.items():
                b = dict(b)
                if "spore" in b["kind"] and b["kind"] != "spore_seed":
                    b["spore_id"] = ids["loop"]
                for tok in (None, TOKEN):
                    st, body = _post(f"{base}/edit", b, tok)
                    assert st in (403, 409), (name, tok, st, body)
                    # an undo naming no real edit is refused as gone before its tier (it inherits one)
                    assert body["error"] in ("credential_required", "needs_broker", "needs_intent", "stale"), (name, body)
                    if tok is None:
                        assert body["error"] == "credential_required"
        assert (_root / ".levain" / "memory.spores.json").read_text() == before

    def test_with_the_token_a_c1_fires(self, tmp_path: Path) -> None:
        """'with the token a C1 fires'"""
        _root, src, ids = _install(tmp_path)
        with _serve(src, write_token=TOKEN) as (base, _h):
            rows = _get(f"{base}/cockpit/panel/tray.json", TOKEN)["rows"]
            row = next(r for r in rows if r["id"] == f"spore:{ids['seed']}")
            st, body = _post(f"{base}/cockpit/verb", {"verb": "spore_surface_at", "panel_id": "tray",
                                                       "row_id": row["id"], "row_version": row["version"],
                                                       "params": {"surface_at": "2030-01-01"}}, TOKEN)
            assert st == 200, body
        assert SporeStore(_root / ".levain" / "memory.spores.json").get(ids["seed"])["next"] == "2030-01-01"

    def test_a_writable_source_given_no_token_gets_a_per_launch_one(self, tmp_path: Path) -> None:
        _root, src, ids = _install(tmp_path)
        with _serve(src) as (base, httpd):
            tok = httpd.surface_tokens["browser"]
            assert len(tok) >= 32
            assert _post(f"{base}/edit", {"kind": "spore_touch", "spore_id": ids["seed"]})[0] == 403
            assert _post(f"{base}/edit", {"kind": "spore_touch", "spore_id": ids["seed"]}, tok)[0] == 200

    def test_no_t2_or_t3_ever_fires_from_the_post_handler(self, rig: Rig) -> None:
        """'no T2 or T3 ever fires from the POST handler (before K2b lands they are refused with
        "needs the broker")'"""
        before = rig.store.get(rig.ids["loop"])
        cases = [("spore_set_disposition", "loops", "loop", {"disposition": "note"}),
                 ("spore_ascend", "loops", "loop", {"spore_kind": "x", "ref": "r"}),
                 ("spore_update", "loops", "loop", {"text": "new"}),
                 ("spore_seed", "tray", None, {"text": "planted"})]
        for verb, panel, key, params in cases:
            e = _refused("needs_broker", rig.post, verb, panel, key, params, confirm=True, intent="reported")
            assert e.http_status == 403
        assert rig.store.get(rig.ids["loop"]) == before
        assert len(rig.store.list_open()) == 5

    def test_a_verb_without_tier_fn_reports_t2(self, tmp_path: Path) -> None:
        """'a verb without tier_fn reports T2'"""
        ran = []
        r = Rig(tmp_path, extra={"ping": ActionVerb(handler=lambda p: ran.append(p) or {}, label="ping")})
        spec = r.reg.get("ping")
        assert spec is not None and spec.tier({}, None, "unverified", TODAY) == "T2"
        _refused("needs_broker", dispatch, registry=r.reg, cockpit=r.ck, scope=r.src.write_scope,
                 req={"verb": "ping", "params": {}, "confirm": True}, credential=CRED,
                 install_class=INSTALL_CLASS, route="action")
        assert ran == []

    def test_a_c1_downstream_verbspec_is_audited_and_replay_guarded(self, tmp_path: Path) -> None:
        """A downstream VerbSpec rides apply_action's envelope: a receipt in the ledger, and an
        idempotent one fires once per key (the lane's L1 observation, closed)."""
        fired: list[dict] = []
        spec = VerbSpec("note_it", "note", "records a note", "panel", fields=("text",), floor="C1",
                        tier_fn=lambda *a: "C1", idempotent=True,
                        fire=lambda scope, p, target, confirm: fired.append(p) or {"summary": "noted"})
        r = Rig(tmp_path, extra={"note_it": spec})
        r.ck.register(ProviderSpec("ext:notes", "line", "Notes", "feed", lambda ctx: Read(value={"lines": []}),
                                   verbs=("note_it",)))
        req = {"verb": "note_it", "panel_id": "ext:notes", "params": {"text": "hi"}, "idempotency_key": "k1"}
        for _ in range(2):
            dispatch(registry=r.reg, cockpit=r.ck, scope=r.src.write_scope, req=dict(req), credential=CRED,
                     install_class=INSTALL_CLASS, route="cockpit")
        _refused("unknown_verb", dispatch, registry=r.reg, cockpit=r.ck, scope=r.src.write_scope,
                 req=dict(req, verb="note_it"), credential=CRED, install_class=INSTALL_CLASS, route="action")
        assert fired == [{"text": "hi"}]
        ledger = (r.root / ".levain" / "edits.jsonl").read_text()
        assert '"action": "note_it"' in ledger and '"outcome": "ok"' in ledger

    def test_registering_harness_approve_as_a_cockpit_verb_is_refused(self) -> None:
        """'registering harness_approve as a cockpit verb → refused'"""
        spec = VerbSpec("harness_approve", "x", "x", "panel", tier_fn=lambda *a: "C1", fire=lambda *a: {})
        with pytest.raises(ValueError, match="broker"):
            VerbRegistry({"harness_approve": spec})
        with pytest.raises(ValueError, match="broker"):
            VerbRegistry({"harness_approve": ActionVerb(handler=lambda p: {})})

    def test_a_route_broker_verb_posted_to_cockpit_verb_is_refused(self, rig: Rig) -> None:
        """'a route: "broker" verb POSTed to /cockpit/verb → refused'"""
        _refused("route_broker", rig.post, "harness_approve", "tray")

    def test_dry_run_returns_tier_and_gesture_and_writes_nothing(self, rig: Rig) -> None:
        """'a dry_run POST returns the tier and gesture and creates no pending'"""
        before = rig.spore("loop")
        out = rig.post("spore_update", "loops", "loop", {"text": "x"}, dry_run=True)
        assert (out["tier"], out["gesture"], out["dry_run"]) == ("T2", "direct-reported", True)
        assert rig.spore("loop") == before
        assert not (rig.root / ".levain" / "edits.jsonl").exists() or "spore_update" not in \
            (rig.root / ".levain" / "edits.jsonl").read_text()


# --- binding a write to what was rendered ---------------------------------------------------------


class TestBinding:
    def test_a_row_edited_after_render_is_409(self, rig: Rig) -> None:
        """'a row edited after render → 409'"""
        r = rig.row("tray", "seed")
        rig.store.update(rig.ids["seed"], text="edited elsewhere")
        e = _refused("stale", dispatch, registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope,
                     req={"verb": "spore_touch", "panel_id": "tray", "row_id": r["id"], "row_version": r["version"]},
                     credential=CRED, install_class=INSTALL_CLASS, route="cockpit")
        assert e.http_status == 409

    def test_a_stale_panel_version_is_409_for_state_section_and_config(self, rig: Rig) -> None:
        """'a focus, section_edit or config write with a stale panel_version → 409' (focus is flow's;
        operator_state is the kernel's line write and is held here the same way)"""
        state_v = rig.ck.panel("state")["value"]["value_version"]
        _refused("stale", rig.post, "operator_state", "state", None, {"text": "x"}, panel_version="0" * 16)
        assert rig.post("operator_state", "state", None, {"text": "x"}, panel_version=state_v)["ok"]
        _refused("stale", rig.post, "operator_state", "state", None, {"text": "y"}, panel_version=state_v)
        # discovered panels, named before any read discovered them: the write path discovers them itself
        for pid, verb in (("section:state", "section_edit"), ("config:posture", "config")):
            _refused("stale", rig.post, verb, pid, None, {"new_body": "x"}, panel_version="0" * 16)

    def test_touch_session_open_and_midnight_leave_the_version_alone(self, rig: Rig) -> None:
        """'a spore_touch, a session-open and a clock moved past midnight between render and write →
        the write still lands (version unchanged)'"""
        r = rig.row("tray", "seed")
        rig.store.touch(rig.ids["seed"], today=TODAY + timedelta(days=1))   # a touch, as session-open writes it
        rig.clock.advance(1)
        out = rig.post("spore_surface_at", "tray", None, {"surface_at": "2030-01-01"},
                       row_id=r["id"], row_version=r["version"])
        assert out["ok"]

    def test_a_touch_that_clears_an_elapsed_next_changes_the_version(self, rig: Rig) -> None:
        """Measured for the desk (1010+31's anneal finding): anneal's touch clears an ELAPSED ``next``,
        and ``next`` is a stored, versioned field, so that touch IS a state change and the next write
        on the old render gets 409. The §9 line above holds for a spore whose alarm has not elapsed."""
        sid = rig.store.add(type="task", text="alarm", disposition="seed", next="2026-10-09", today=TODAY)["id"]
        rig.ids["alarm"] = sid
        r = rig.row("tray", "alarm")
        rig.store.touch(sid, today=TODAY)
        if rig.store.get(sid).get("next") is None:
            assert rig.row("tray", "alarm")["version"] != r["version"]
            _refused("stale", rig.post, "spore_touch", "tray", None, {}, row_id=r["id"], row_version=r["version"])
        else:
            pytest.fail("anneal's touch kept an elapsed next; the desk's premise no longer holds, re-read it")

    @pytest.mark.xfail(strict=True, reason="needs anneal SporeStore expected_version under _transaction "
                       "(slice P, anneal seat/1010-31-spore-cas), released and installed: K2a S2. In this window "
                       "the tier is computed on a row the kernel does not hold, so a seat holding a row between "
                       "read_one and the write lets a pull-forward fire at C1 (L2 M2, run): K2a must not ship open")
    def test_a_text_edit_between_read_one_and_the_handler_is_409(self, rig: Rig, monkeypatch) -> None:
        """'a text edit landing between read_one and the handler → 409'"""
        r = rig.row("tray", "seed")      # rendered before the race is armed
        real = rig.ck.read_one

        def read_then_race(panel_id: str, row_id: str):
            res = real(panel_id, row_id)
            rig.store.update(rig.ids["seed"], text="landed in the window")
            return res
        monkeypatch.setattr(rig.ck, "read_one", read_then_race)
        _refused("stale", rig.post, "spore_surface_at", "tray", None, {"surface_at": "2030-01-01"},
                 row_id=r["id"], row_version=r["version"])

    def test_a_verb_on_a_panel_that_does_not_offer_it_is_refused(self, rig: Rig) -> None:
        """'a verb on a panel that does not offer it → refused'"""
        _refused("not_offered", rig.post, "spore_touch", "episodes")
        _refused("not_offered", rig.post, "spore_seed", "loops", None, {"text": "x"})

    def test_removing_a_panel_removes_its_verb(self, rig: Rig) -> None:
        """'removing a panel removes its verb (404)'"""
        assert "episode_tombstone" in rig.ck.manifest(CRED)["verbs"]
        specs = dict(rig.ck._specs)
        del specs["episodes"]
        rig.ck._specs = specs
        assert "episode_tombstone" not in rig.ck.manifest(CRED)["verbs"]
        e = _refused("not_offered", rig.post, "episode_tombstone", "episodes")
        assert e.http_status == 404


# --- the tier ladder (§4.2) ---------------------------------------------------------------------


class TestTierLadder:
    def test_every_row_provenance_is_null(self, rig: Rig) -> None:
        """'every row's provenance is null (no label is claimed before K2b's journal exists)'"""
        for pid in ("tray", "loops", "keep", "episodes", "edits"):
            for r in (rig.ck.panel(pid, credential_class="token") or {}).get("rows") or []:
                assert r["provenance"] is None

    def test_update_carrying_tier_and_text_reports_t2(self, rig: Rig) -> None:
        """'a spore_update carrying tier and text reports T2'"""
        assert rig.tier("spore_update", "loops", "loop", {"tier": "warm", "text": "x"}) == "T2"
        assert rig.tier("spore_update", "loops", "loop", {"tier": "warm"}) == "C1"

    def test_on_an_unverified_tray_row_a_later_snooze_and_a_retier_fire_as_c1(self, rig: Rig) -> None:
        """'on an UNVERIFIED Tray row a later-dated snooze and a re-tier fire as C1 (RULED Q4)'"""
        assert rig.post("spore_surface_at", "tray", "seed", {"surface_at": "2031-01-01"})["ok"]
        assert rig.spore("seed")["next"] == "2031-01-01"
        r = rig.ck.read_one("keep", f"spore:{rig.ids['note']}").value
        out = dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope,
                       req={"verb": "spore_update", "panel_id": "keep", "row_id": r["id"],
                            "row_version": r["version"], "params": {"tier": "hot"}},
                       credential=CRED, install_class=INSTALL_CLASS, route="cockpit")
        assert out["ok"] and rig.spore("note")["tier"] == "hot"

    def test_a_held_row_pulled_forward_or_cleared_reports_t2_and_stays_held(self, rig: Rig) -> None:
        """'on an unverified row held to a future date, a date of tomorrow, of today, in the past, or a
        clear each report T2, and after the clock is advanced past each rejected date the row is
        still out of tray and now'"""
        tomorrow, today, past = TODAY + timedelta(days=1), TODAY, TODAY - timedelta(days=3)
        for new in (tomorrow.isoformat(), today.isoformat(), past.isoformat(), None):
            assert rig.tier("spore_surface_at", "tray", "held", {"surface_at": new}) == "T2", new
            _refused("needs_intent", rig.post, "spore_surface_at", "tray", "held", {"surface_at": new})
            _refused("needs_broker", rig.post, "spore_surface_at", "tray", "held", {"surface_at": new},
                     intent="reported")
        rig.clock.advance(2)    # past every rejected date, still before the stored one
        held = f"spore:{rig.ids['held']}"
        assert held not in rig.panel_ids("tray")
        assert held not in rig.panel_ids(NOW_ID)

    def test_surface_at_on_a_keep_note_leaves_it_out_of_the_tray(self, rig: Rig) -> None:
        """'surface_at on a Keep note leaves it out of the tray panel'"""
        r = rig.ck.read_one("keep", f"spore:{rig.ids['note']}").value
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope,
                 req={"verb": "spore_surface_at", "panel_id": "keep", "row_id": r["id"], "row_version": r["version"],
                      "params": {"surface_at": TODAY.isoformat()}},
                 credential=CRED, install_class=INSTALL_CLASS, route="cockpit")
        assert f"spore:{rig.ids['note']}" not in rig.panel_ids("tray")

    def test_set_disposition_reports_t2_for_every_target_on_every_row(self, rig: Rig) -> None:
        """'spore_set_disposition reports T2 for every target on every row'"""
        for key, panel in (("seed", "tray"), ("held", "tray"), ("loop", "loops"), ("note", "keep"), ("parked", "keep")):
            for target in ("seed", "handoff", "agenda", "note", "loop"):
                assert rig.tier("spore_set_disposition", panel, key, {"disposition": target}) == "T2"

    def test_a_retier_of_an_unverified_parked_loop_to_hot_reports_t2(self, rig: Rig) -> None:
        """'a re-tier of an unverified parked loop to hot reports T2'"""
        panel = "loops" if f"spore:{rig.ids['parked']}" in rig.panel_ids("loops") else "keep"
        r = rig.ck.read_one(panel, f"spore:{rig.ids['parked']}")
        assert r.value["facets"]["tier"] == "parked"
        assert rig.tier("spore_update", panel, "parked", {"tier": "hot"}) == "T2"
        assert rig.tier("spore_update", "loops", "loop", {"tier": "parked"}) == "T2"

    def test_salience_and_free_text_descend_fields_are_refused(self, rig: Rig) -> None:
        """'a spore_update carrying salience → refused'; 'a spore_descend carrying a free-text field → refused'"""
        _refused("field_not_allowed", rig.post, "spore_update", "loops", "loop", {"salience": 9})
        _refused("field_not_allowed", rig.post, "spore_descend", "loops", "loop",
                 {"spore_kind": "x", "note": "free text"}, confirm=True)

    def test_an_escalating_snooze_without_intent_is_refused_needs_intent(self, rig: Rig) -> None:
        """'a snooze POST with an escalating date and no intent → refused "needs intent"'"""
        e = _refused("needs_intent", rig.post, "spore_surface_at", "tray", "held", {"surface_at": None})
        assert e.http_status == 409

    def test_an_escalating_snooze_through_the_edit_alias_is_refused_needs_intent(self, rig: Rig) -> None:
        """'an escalating snooze through the /edit alias, in a reported install, is refused "needs intent"'"""
        assert INSTALL_CLASS == "reported"
        _refused("needs_intent", rig.edit, {"kind": "spore_surface_at", "spore_id": rig.ids["held"], "surface_at": None})
        assert rig.spore("held")["next"] == (TODAY + timedelta(days=5)).isoformat()
        assert rig.edit({"kind": "spore_surface_at", "spore_id": rig.ids["held"], "surface_at": "2031-01-01"})["ok"]

    def test_decision_answer_is_not_registered(self, rig: Rig) -> None:
        """'a needs_phill row without owner_install → decision_answer refused': the kernel has no
        needs_phill store with a CAS yet (flow M1), so decision_answer is not registered at all (§4.4:
        a verb whose source has no CAS is not registered) and every POST of it is refused."""
        assert rig.reg.get("decision_answer") is None
        _refused("unknown_verb", rig.post, "decision_answer", "tray")


# --- what a panel shows (etags, actions) -----------------------------------------------------------


class TestRenderedActions:
    def test_row_actions_carry_tier_gesture_escalates_and_target(self, rig: Rig) -> None:
        rows = rig.ck.panel("tray", credential_class="token")["rows"]
        seed = next(r for r in rows if r["id"] == f"spore:{rig.ids['seed']}")
        acts = {a["verb"]: a for a in seed["actions"]}
        assert acts["spore_touch"]["tier"] == "C1" and acts["spore_touch"]["gesture"] == "direct"
        assert acts["spore_set_disposition"]["gesture"] == "direct-reported"
        assert acts["spore_touch"]["target"] == {"panel_id": "tray", "row_id": seed["id"], "row_version": seed["version"]}
        none_rows = rig.ck.panel("tray", credential_class="none")["rows"]
        assert all(a["gesture"] == "refused" for r in none_rows for a in r["actions"])

    def test_past_midnight_a_passed_held_date_changes_the_etag_and_the_snooze_tier(self, rig: Rig) -> None:
        """'past midnight, a row whose held date passed returns a new panel etag and the snooze's new tier'"""
        before = rig.ck.panel("loops", credential_class="token")["etag"]
        assert rig.tier("spore_surface_at", "tray", "held", {"surface_at": None}) == "T2"
        rig.clock.advance(6)    # the held date (TODAY+5) has passed
        assert rig.ck.panel("loops", credential_class="token")["etag"] != before
        assert rig.tier("spore_surface_at", "tray", "held", {"surface_at": None}) == "C1"

    def test_a_policy_change_changes_the_panel_etag_with_rows_unchanged(self, rig: Rig, monkeypatch) -> None:
        """'a policy change that alters a panel action's gesture with rows unchanged returns a new panel
        etag (no 304)'"""
        p1 = rig.ck.panel("tray", credential_class="token")
        monkeypatch.setattr(verbs_mod, "POLICY", verbs_mod.Policy(revision=2, raise_to={"spore_touch": "T2"}))
        p2 = rig.ck.panel("tray", credential_class="token")
        assert [r["version"] for r in p1["rows"]] == [r["version"] for r in p2["rows"]]
        assert p1["etag"] != p2["etag"]
        touch = next(a for a in p2["rows"][0]["actions"] if a["verb"] == "spore_touch")
        assert (touch["tier"], touch["gesture"]) == ("T2", "direct-reported")

    def test_the_manifest_verbs_map_holds_only_offered_verbs(self, rig: Rig) -> None:
        vm = rig.ck.manifest(CRED)["verbs"]
        assert "spore_touch" in vm and vm["spore_touch"]["route"] == "cockpit"
        assert "entity_name" not in vm          # no panel offers it
        assert "harness_approve" not in vm      # no panel offers it yet (the decisions panel is K2b's)


# Held elsewhere, not here:
# - 'as a SECOND local uid ...': RESIDUE, a run as another OS user (S3); this file holds the no-token half.
# - 'a needs_phill row without owner_install → decision_answer refused': flow M1 (owner_install + CAS);
#   until then decision_answer is unregistered (test_decision_answer_is_not_registered).
# - 'a consult carrying a path or a context selector → refused ... returns none of its content' and
#   'a cockpit consult's outbound prompt carries no situational-context block': consult is flow's
#   verb; the kernel holds the allowlist for any downstream VerbSpec (test_actions.py), flow holds the rest.
# - 'a grep of flow, flowConnect and levain for /edit and /action callers ...': S3.
# - 'a text edit landing between read_one and the handler → 409': xfail above until anneal's CAS is installed.


# --- L1/L2 round 1 findings, each held by a test that failed on fc9c83e ---------------------------


class TestRound1:
    def test_edit_on_a_row_the_view_drops_is_refused_not_tiered_as_no_row(self, rig: Rig) -> None:
        """L2 H1: a held row the view drops (a bad salience) was tiered C1 as 'no row' while the
        legacy handler still pulled its date forward."""
        sid = rig.store.add(type="task", text="drift", disposition="seed",
                            next=(TODAY + timedelta(days=5)).isoformat(), today=TODAY)["id"]
        p = rig.root / ".levain" / "memory.spores.json"
        data = json.loads(p.read_text())
        for sp in data["spores"]:
            if sp["id"] == sid:
                sp["salience"] = "x"
        p.write_text(json.dumps(data))
        before = p.read_text()
        e = _refused("stale", rig.edit, {"kind": "spore_surface_at", "spore_id": sid, "surface_at": TODAY.isoformat()})
        assert e.http_status == 409
        assert p.read_text() == before

    def test_the_edit_alias_supplies_the_cas_key_itself(self, rig: Rig, monkeypatch) -> None:
        """L2 L2: a legacy descend without expect_disposition fired with no CAS; the kernel now
        supplies the key from the row it read."""
        import levain.cockpit.verbs as v
        seen: list[dict] = []
        real = v.apply_edit
        monkeypatch.setattr(v, "apply_edit", lambda scope, req: seen.append(req) or real(scope, req))
        rig.edit({"kind": "spore_descend", "spore_id": rig.ids["loop"], "spore_kind": "dropped", "confirm": True,
                  "expect_disposition": None})        # the rendered snapshot, compared normalised (loop = null)
        assert seen and seen[0]["expect_disposition"] == "loop"   # the row's own, normalised by the handler

    def test_operator_state_source_comes_from_the_credential(self, rig: Rig) -> None:
        """L2 M3: a browser-token write could label its line 'cli'."""
        v = rig.ck.read_value_version("state").value
        _refused("field_not_allowed", rig.post, "operator_state", "state", None, {"text": "x", "source": "cli"},
                 panel_version=v)
        rig.edit({"kind": "operator_state", "text": "via edit", "source": "cli",
                  "panel_version": rig.ck.read_value_version("state").value})
        stored = json.loads((rig.root / ".levain" / "context.json").read_text())
        assert (stored["state"], stored["state_source"]) == ("via edit", "web")

    def test_the_first_state_write_binds_to_absent(self, rig: Rig) -> None:
        """L1 M4: with no context file the line could never be written on /cockpit/verb."""
        (rig.root / ".levain" / "context.json").unlink()
        _refused("stale", rig.post, "operator_state", "state", None, {"text": "x"}, panel_version="0" * 16)
        assert rig.post("operator_state", "state", None, {"text": "first"}, panel_version=verbs_mod.VALUE_ABSENT)["ok"]
        _refused("stale", rig.post, "operator_state", "state", None, {"text": "again"},
                 panel_version=verbs_mod.VALUE_ABSENT)

    def test_spore_update_refuses_fields_its_handler_cannot_write(self, rig: Rig) -> None:
        """L1 M3 / L2 L1: next and domain were allowlisted and silently dropped."""
        for f, val in (("next", "2030-01-01"), ("domain", "x")):
            _refused("field_not_allowed", rig.post, "spore_update", "loops", "loop", {"tier": "warm", f: val})

    def test_an_external_panel_cannot_offer_a_kernel_verb(self, tmp_path: Path) -> None:
        """L1 #12 / L2 L3."""
        r = Rig(tmp_path)
        r.ck.register(ProviderSpec("ext:sneaky", "line", "S", "feed", lambda ctx: Read(value={"lines": []}),
                                   verbs=("operator_state",)))
        _refused("not_offered", r.post, "operator_state", "ext:sneaky", None, {"text": "x"},
                 panel_version=verbs_mod.VALUE_ABSENT)

    def test_a_row_bound_downstream_verbspec_is_refused_at_registration(self) -> None:
        """L1 H1 / L2 M1: /action fired a row-bound downstream VerbSpec with row=None."""
        spec = VerbSpec("ds_row", "x", "x", "row", tier_fn=lambda p, row, *_: "T2" if row else "C1",
                        fire=lambda *a: {})
        with pytest.raises(ValueError, match="binding must be 'panel'"):
            VerbRegistry({"ds_row": spec})

    def test_an_idempotency_key_on_a_non_idempotent_verb_is_refused(self, rig: Rig) -> None:
        """L1 #9: it was accepted and silently ignored."""
        _refused("bad_request", rig.post, "spore_touch", "tray", "seed", {}, idempotency_key="k")

    def test_hung_write_reads_are_bounded_and_finished_ones_do_not_count(self, rig: Rig, monkeypatch) -> None:
        """L1 M5: write-time reads no longer join an older flight; the bound is on reads still running."""
        import levain.cockpit.engine as eng
        for _ in range(eng.WRITE_READ_CAP + 5):      # finished reads never fill the cap
            assert rig.tier("spore_touch", "tray", "seed") == "C1"
        gate = threading.Event()
        spec = rig.ck.spec("tray")
        monkeypatch.setattr(spec, "read_one", lambda ctx, rid: (gate.wait(5), Absent("x"))[1])
        monkeypatch.setattr(spec, "timeout_s", 0.05)
        outs = [rig.ck.read_one("tray", "spore:x") for _ in range(eng.WRITE_READ_CAP + 1)]
        gate.set()
        assert "still running" in outs[-1].message


# --- L3 r1 findings (codex + complement), each held by a test that failed on 346b715 ---------------


def _note_spec(fired: list[dict]) -> VerbSpec:
    return VerbSpec("note_it", "note", "records a note", "panel", fields=("text",), floor="C1",
                    tier_fn=lambda *a: "C1", idempotent=True,
                    fire=lambda scope, p, target, confirm: fired.append({**p, "panel": target["panel_id"]})
                    or {"summary": "noted"})


def _discovered_rig(tmp_path: Path, fired: list[dict]) -> tuple[Rig, dict[str, Any]]:
    """A rig with discovered downstream panels whose offers the test changes between reads."""
    r = Rig(tmp_path, extra={"note_it": _note_spec(fired)})
    feed: dict[str, Any] = {"panels": {"ext:a": ("note_it",), "ext:b": ("note_it",)}, "raise": None}

    def discover(ctx):
        if feed["raise"]:
            raise RuntimeError(feed["raise"])
        return [ProviderSpec(pid, "line", pid, "feed", lambda ctx: Read(value={"lines": []}), verbs=v)
                for pid, v in feed["panels"].items()]
    r.ck.discover(discover)
    r.ck.manifest(CRED)      # the first read registers them
    return r, feed


class TestL3Round1:
    def test_a_discovered_panel_that_stops_offering_a_verb_refuses_it(self, tmp_path: Path) -> None:
        """codex #2: an already-registered discovered panel kept its first offer for every write."""
        fired: list[dict] = []
        r, feed = _discovered_rig(tmp_path, fired)
        r.post("note_it", "ext:a", None, {"text": "one"}, idempotency_key="k1")
        feed["panels"]["ext:a"] = ()
        e = _refused("not_offered", r.post, "note_it", "ext:a", None, {"text": "two"}, idempotency_key="k2")
        assert e.http_status == 404
        assert [f["text"] for f in fired] == ["one"]
        assert r.ck.manifest(CRED)["panels"]["ext:a"]["actions"] == []   # the render follows the source too

    def test_a_write_to_a_discovered_panel_fails_closed_when_discovery_fails(self, tmp_path: Path) -> None:
        """codex #2, the bound: an offer discovery cannot confirm is no offer."""
        fired: list[dict] = []
        r, feed = _discovered_rig(tmp_path, fired)
        feed["raise"] = "feed down"
        e = _refused("source_unavailable", r.post, "note_it", "ext:a", None, {"text": "x"}, idempotency_key="k")
        assert e.http_status == 503 and fired == []

    def test_one_idempotency_key_on_two_panels_is_a_reused_key_not_a_replay(self, tmp_path: Path) -> None:
        """codex #7: the fingerprint omitted panel_id, so the second panel's write was a false replay."""
        fired: list[dict] = []
        r, _feed = _discovered_rig(tmp_path, fired)
        r.post("note_it", "ext:a", None, {"text": "same"}, idempotency_key="k")
        e = _refused("idempotency_key_reuse", r.post, "note_it", "ext:b", None, {"text": "same"},
                     idempotency_key="k")
        assert e.http_status == 422 and fired == [{"text": "same", "panel": "ext:a"}]

    def test_a_legacy_descend_rendered_on_another_list_is_409(self, rig: Rig) -> None:
        """codex #3: /edit dropped the body's expect_disposition and bound to a fresh read, so a
        'remove note' confirmed on an old render descended a now-live loop."""
        before = rig.spore("loop")
        e = _refused("stale", rig.edit, {"kind": "spore_descend", "spore_id": rig.ids["loop"],
                                         "spore_kind": "composted", "confirm": True, "expect_disposition": "note"})
        assert e.http_status == 409 and rig.spore("loop") == before
        _refused("bad_request", rig.edit, {"kind": "spore_descend", "spore_id": rig.ids["loop"],
                                           "spore_kind": "dropped", "confirm": True})
        assert rig.spore("loop") == before
        assert rig.edit({"kind": "spore_descend", "spore_id": rig.ids["loop"], "spore_kind": "dropped",
                         "confirm": True, "expect_disposition": "loop"})["ok"]

    def test_the_state_line_and_its_version_come_from_one_read(self, rig: Rig) -> None:
        """codex #4 / complement #3: the line came from a memoised view, the version from a later raw
        read, so a write could bind to content no one saw."""
        from levain.cockpit.engine import ReadContext, value_version_of
        from levain.cockpit.providers import _view
        ctx = ReadContext(rig.clock())
        _view(rig.src, ctx)                       # memoise the view with the old line
        cj = rig.root / ".levain" / "context.json"
        new = {"state": "changed", "state_set_at": rig.clock().isoformat(), "state_source": "cli"}
        cj.write_text(json.dumps(new))
        res = rig.ck.spec("state").read(ctx)
        assert res.value["lines"][0]["text"] == "changed"
        assert value_version_of(res.version_of) == value_version_of(new)

    def test_edit_operator_state_binds_the_version_it_rendered(self, rig: Rig) -> None:
        """codex #5 / complement #2: /edit operator_state had no version bind (last writer wins)."""
        from levain.dashboard import _read_state
        cj = rig.root / ".levain" / "context.json"
        _refused("bad_request", rig.edit, {"kind": "operator_state", "text": "x"})
        legacy_v = _read_state(cj, rig.clock()).version
        assert legacy_v == rig.ck.read_value_version("state").value      # one version, both renderers
        assert rig.edit({"kind": "operator_state", "text": "x", "panel_version": legacy_v})["ok"]
        _refused("stale", rig.edit, {"kind": "operator_state", "text": "y", "panel_version": legacy_v})
        assert json.loads(cj.read_text())["state"] == "x"

    def test_a_value_panel_action_names_the_version_it_was_rendered_with(self, rig: Rig) -> None:
        """codex #6: the value action's target carried no panel_version."""
        p = rig.ck.panel("state", credential_class="token")
        act = next(a for a in p["actions"] if a["verb"] == "operator_state")
        assert act["target"]["panel_version"] == p["value"]["value_version"]
        (rig.root / ".levain" / "context.json").unlink()
        p = rig.ck.panel("state", credential_class="token")
        act = next(a for a in p["actions"] if a["verb"] == "operator_state")
        assert act["target"]["panel_version"] == verbs_mod.VALUE_ABSENT

    def test_a_non_bool_dry_run_is_refused_not_a_live_write(self, rig: Rig) -> None:
        """codex #8: dry_run "true" fell through to a live write."""
        before = rig.spore("seed")
        e = _refused("bad_request", rig.post, "spore_touch", "tray", "seed", dry_run="true")
        assert e.http_status == 400 and rig.spore("seed") == before

    def test_the_verbs_map_is_what_panels_offer_and_never_a_feed_named_broker_head(self, tmp_path: Path) -> None:
        """codex #9 / complement #5: the map read raw spec.verbs, so a feed could publish a broker head
        or a kernel verb its panel may not offer."""
        r = Rig(tmp_path)
        r.ck.register(ProviderSpec("ext:x", "line", "X", "feed", lambda ctx: Read(value={"lines": []}),
                                   verbs=("harness_approve", "entity_name")))
        vm = r.ck.manifest(CRED)["verbs"]
        assert "harness_approve" not in vm and "entity_name" not in vm

    def test_write_token_cannot_equal_another_surfaces_token(self, tmp_path: Path) -> None:
        """complement #4: uniqueness was checked before write_token merged into 'browser'."""
        _root, src, _ids = _install(tmp_path)
        with pytest.raises(ValueError, match="share one token"):
            make_server(src, host="127.0.0.1", port=0, write_token="T", surface_tokens={"flowconnect": "T"})

    def test_a_token_the_page_cannot_store_is_refused_at_bind(self, tmp_path: Path) -> None:
        """complement #8: a token outside the page's stored set was stripped from the link and dropped
        silently. The server now admits only what the page stores, and both rules are the same set."""
        import re as _re
        from levain import web_server as ws
        _root, src, _ids = _install(tmp_path)
        with pytest.raises(ValueError, match="printable ASCII"):
            make_server(src, host="127.0.0.1", port=0, write_token="has space")
        boot = (Path(ws.__file__).parent / "templates" / "web" / "dashboard_boot.js").read_text()
        m = _re.search(r"if \(/\^(\[[^\]]+\])\+\$/\.test\(tok\)\)", boot)
        assert m and m.group(1) == ws._HEADER_SAFE_TOKEN.pattern[:-1]


class TestL1Round2:
    def test_a_write_reads_the_external_feed_anew_inside_its_reuse_window(self, tmp_path: Path) -> None:
        """L1 #1 on c0d4e21: offer_spec's discovery answered from ExternalPanels' reuse window, so a
        verb the feed withdrew still fired for up to reuse_s."""
        from levain.cockpit.external import ExternalPanels, discoverer
        fired: list[dict] = []
        r = Rig(tmp_path, extra={"note_it": _note_spec(fired)})
        feed = {"action": {"verb": "note_it"}}
        ext = ExternalPanels(lambda: [{"id": "n", "title": "N", "lines": [], **feed}], reuse_s=3600)
        r.ck.discover(discoverer(ext))
        r.ck.manifest(CRED)
        r.post("note_it", "ext:n", None, {"text": "one"}, idempotency_key="k1")
        feed.clear()                               # the source withdraws the verb
        _refused("not_offered", r.post, "note_it", "ext:n", None, {"text": "two"}, idempotency_key="k2")
        assert [f["text"] for f in fired] == ["one"]

    def test_a_verb_name_with_a_colon_is_refused(self) -> None:
        """L1 #5: the bound idempotency fingerprint lives under 'cockpit:<verb>'."""
        with pytest.raises(ValueError, match="without ':'"):
            VerbRegistry({"cockpit:x": ActionVerb(handler=lambda p: {})})

    def test_the_page_and_the_manifest_version_the_same_record(self, rig: Rig) -> None:
        """L1 #3: one stored-record projection, extra keys ignored, for both renderers."""
        from levain.dashboard import _read_state
        cj = rig.root / ".levain" / "context.json"
        for data in ({"state": "a", "state_set_at": rig.clock().isoformat(), "state_source": "cli", "location": "x"},
                     {"location": "only"}):
            cj.write_text(json.dumps(data))
            assert _read_state(cj, rig.clock()).version == rig.ck.read_value_version("state").value

    def test_one_hung_source_does_not_refuse_another_sources_writes(self, rig: Rig, monkeypatch) -> None:
        """complement #6 (r1): WRITE_READ_CAP was global, so one hung source starved every write."""
        import levain.cockpit.engine as eng
        gate = threading.Event()
        spec = rig.ck.spec("tray")
        monkeypatch.setattr(spec, "read_one", lambda ctx, rid: (gate.wait(5), Absent("x"))[1])
        monkeypatch.setattr(spec, "timeout_s", 0.05)
        outs = [rig.ck.read_one("tray", "spore:x") for _ in range(eng.WRITE_READ_CAP + 1)]
        try:
            assert "reads of 'tray' are still running" in outs[-1].message
            assert rig.tier("spore_touch", "loops", "loop") == "C1"      # another source still reads
        finally:
            gate.set()


class TestL2Round2:
    @pytest.mark.skipif(sys.platform == "win32", reason="no flock: an unchecked write is unlocked there")
    def test_every_levain_state_writer_takes_one_lock_on_the_real_path(self, rig: Rig, monkeypatch) -> None:
        """L2 #1/#2 on c0d4e21: the lock lived only in the cockpit fire (the TUI, levain state and
        apply_edit wrote unlocked) and was keyed on the unresolved path."""
        import fcntl
        from levain import dashboard
        from levain.writes import apply_edit
        cj = rig.root / ".levain" / "context.json"
        link = rig.root / "ctx-link.json"
        link.symlink_to(cj)
        opened: list[str] = []
        real_open = os.open
        monkeypatch.setattr(os, "open", lambda path, *a, **k: (opened.append(str(path)), real_open(path, *a, **k))[1])
        dashboard.write_state(link, "via link")                         # levain state
        apply_edit(rig.src.write_scope, {"kind": "operator_state", "text": "via kernel api"})   # TUI, kernel API
        rig.edit({"kind": "operator_state", "text": "via edit",
                  "panel_version": rig.ck.read_value_version("state").value})                # the cockpit alias
        locks = [p for p in opened if p.endswith(".lock")]
        assert locks == [os.path.realpath(cj) + ".lock"] * 3

    @pytest.mark.skipif(sys.platform == "win32", reason="no flock: an unchecked write is unlocked there")
    def test_the_cockpit_check_runs_under_the_writers_lock(self, rig: Rig, monkeypatch) -> None:
        """L2 #1: another levain writer waits behind the cockpit's in-lock compare and write."""
        from levain import dashboard
        cj = rig.root / ".levain" / "context.json"
        v = rig.ck.read_value_version("state").value
        calls, inside, release = [0], threading.Event(), threading.Event()
        real_check = verbs_mod._check_value_version

        def check(ck, pid, supplied):
            real_check(ck, pid, supplied)
            calls[0] += 1
            if calls[0] == 2:            # the second compare is the one under the lock
                inside.set()
                release.wait(5)
        monkeypatch.setattr(verbs_mod, "_check_value_version", check)
        t = threading.Thread(target=rig.post, args=("operator_state", "state", None, {"text": "cockpit"}),
                             kwargs={"panel_version": v})
        t.start()
        assert inside.wait(5)
        other = threading.Thread(target=dashboard.write_state, args=(cj, "tui"))
        other.start()
        other.join(0.3)
        assert other.is_alive()          # blocked behind the cockpit's compare + write
        release.set()
        t.join(5)
        other.join(5)
        assert json.loads(cj.read_text())["state"] == "tui"     # serialised: the later writer lands last

    def test_an_unreadable_state_file_says_so_not_panel_version_required(self, rig: Rig) -> None:
        """L2 #4: a corrupt context file answered the page's write with "'panel_version' is required"."""
        (rig.root / ".levain" / "context.json").write_text("{not json")
        e = _refused("source_unavailable", rig.edit, {"kind": "operator_state", "text": "x", "panel_version": None})
        assert e.http_status == 503 and "unreadable" in str(e)

    def test_a_discovered_panel_has_one_owner_and_offers_nothing_once_gone(self, tmp_path: Path) -> None:
        """L2 #7/#8: a second discoverer could set a panel's verbs, and a vanished panel kept
        advertising its verb in the manifest."""
        fired: list[dict] = []
        r, feed = _discovered_rig(tmp_path, fired)
        r.ck.discover(lambda ctx: [ProviderSpec("ext:a", "line", "ext:a", "feed",
                                                lambda ctx: Read(value={"lines": []}), verbs=())])
        m = r.ck.manifest(CRED)
        assert any(e["source"] == "discovery:ext:a" and "collision" in e["message"] for e in m["errors"])
        assert [a["verb"] for a in m["panels"]["ext:a"]["actions"]] == ["note_it"]   # the owner's offer stands
        del feed["panels"]["ext:a"]
        m = r.ck.manifest(CRED)
        assert m["panels"]["ext:a"]["actions"] == [] and m["panels"]["ext:b"]["actions"]
