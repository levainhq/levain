"""1105-32 finding 1: the CROSS test. The launcher mints a passthrough ticket with ``egress_routes: []`` (Phill 11-02,
np-b0cbc7de (a): none by default); the runtime builds the render blob from the base map. Each half was tested alone
(the runtime tests inject ``routes=``), so no test ever put the launcher's facts beside the runtime's blob built from a
base map as ``setup`` writes it, and every setup-default install refused ``ticket-routes-mismatch`` at render.

Here: the mint facts come from ``RealLauncher._facts``, the base map from the REAL ``vendor_routes.merge``, the blob
from ``HelperSlotRuntime._render_blob``, and the pair goes through ``render.check_ticket``.
"""
from __future__ import annotations

import pytest

from levain.agents import session_launcher as L
from levain.contracts import canon
from levain.slot import Refused
from levain.slot import dispatch_runtime as DR
from levain.slot import render as R
from levain.slot import vendor_routes as VR
from levain.slot.runtime_grammar import parse_stdin
from tests.test_agents_session_launch import EXP
from tests.test_agents_session_launcher import KEY, FakeControl, FakeRuntime, FakeSources, launch


def _runtime(tmp_path, monkeypatch, doc):
    merged, _ids, _notes = VR.merge(doc)
    path = tmp_path / "base.json"
    path.write_bytes(canon.canonical_bytes(merged))
    monkeypatch.setattr(DR.layout, "base_routes_path", lambda host: path)
    return DR.HelperSlotRuntime(host="darwin", helper=("true",), store=tmp_path / "store",
                                control_socket=tmp_path / "c.sock")


def _pair(tmp_path, monkeypatch, doc):
    ln = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "idx"))
    lau = launch()
    facts = ln._facts(lau)
    req = L.SlotStart("tkt", "r-0123456789abcdef", KEY, "claude", lau.model, lau.brief, str(facts["brief_digest"]),
                      L.HOOKS)
    blob = parse_stdin("render", _runtime(tmp_path, monkeypatch, doc)._render_blob(req))
    return blob, {**facts, "deadline": 0}


def _check(blob, ticket_facts):
    R.check_ticket(blob, "dispatch", "egress", "tkt", ticket_facts, "passthrough")


def test_a_default_install_ticket_and_blob_agree_at_render(tmp_path, monkeypatch):
    blob, tf = _pair(tmp_path, monkeypatch, None)            # None: setup's own merge of an absent map
    assert tf["egress_routes"] == []
    assert any(r["route"] == VR.EGRESS_DEFAULT for r in VR.merge(None)[0]["routes"])   # the map does hold egress-default
    _check(blob, tf)                                          # red on 23c79b16: ticket-routes-mismatch
    assert len(blob.routes) == 1 and VR.EGRESS_DEFAULT not in blob.routes


def test_two_egress_routes_in_the_base_map_still_agree(tmp_path, monkeypatch):
    doc = {"format": VR.BASE_FORMAT, "routes": [
        {"route": "egress-operator", "type": "egress", "hosts": ["docs.example.org"]},
        {"route": "egress-second", "type": "egress", "hosts": ["pkgs.example.org"]}]}
    blob, tf = _pair(tmp_path, monkeypatch, doc)
    _check(blob, tf)
    assert len(blob.routes) == 1


def test_render_still_refuses_an_egress_route_the_ticket_never_listed(tmp_path, monkeypatch):
    blob, tf = _pair(tmp_path, monkeypatch, None)
    wide = type(blob)(**{**{f: getattr(blob, f) for f in blob.__dataclass_fields__},
                         "routes": (VR.EGRESS_DEFAULT, *blob.routes)})
    with pytest.raises(Refused) as e:
        _check(wide, tf)
    assert e.value.name == "ticket-routes-mismatch"
