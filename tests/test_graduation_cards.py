"""The graduation card, the steps FEED and their hook beside the proposals route (14b S4, DESIGN_REV2 §5, REV3 §3,
§6). The steps come from ``graduate_due`` over a real store with the test-only coverage source."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from levain.autonomic import ActionManifest, ActionRisk, Posture, RiskClass
from levain.cockpit.graduation_cards import NO_POLICY_NOTE
from levain.cockpit.graduation_cards import card_of, graduation_view, read_steps
from tests.autonomic_test_only_coverage import TestOnlyCoverage
from tests.test_autonomic_graduation_due import LATER, LOW, _binding, _due, _store, policy

HIGH = ActionRisk(cls=RiskClass.HIGH, reversible=False, external=True, financial=False)


def test_an_uncovered_step_gets_a_card_with_no_command_and_its_own_note(tmp_path):
    # S-A removed the `levain binding` verbs: the card prints no command, and its note is the card's own sentence, not a stopped grant's way back
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, TestOnlyCoverage([]))
    card = card_of(step)
    assert card["command"] is None and card["note"] == NO_POLICY_NOTE and not card["note"].startswith("revoke it")
    assert "levain binding" not in json.dumps(card)
    assert card["from"] == "COOLING_OFF" and card["to"] == "ON_LOOP" and card["clean_count"] == 20
    assert card["actions"] == [{"link": 0, "action": "deliver", "risk": {"cls": 1, "reversible": True,
                                                                          "external": False, "financial": False},
                                "covered": False}]
    assert "no live policy" in card["why"]


def test_no_line_is_offered_for_an_action_no_policy_may_sign_or_the_manifest_does_not_declare(tmp_path):
    s = _store(tmp_path, _binding())
    [step] = _due(s, TestOnlyCoverage([]), link_action=lambda b, i: "undeclared")
    card = card_of(step)
    assert card["command"] is None and "does not declare undeclared" in card["why"]
    from levain.autonomic.graduation import graduate_due
    [step] = graduate_due(s, TestOnlyCoverage([]), clock=lambda: LATER, manifest=ActionManifest({"mail.send": HIGH}),
                          link_action=lambda b, i: "mail.send", risk_resolver=lambda b: LOW,
                          link_risk_resolver=lambda b, i: LOW)
    card = card_of(step)
    assert card["command"] is None and "above what a graduation policy may sign" in card["why"]


def test_read_steps_shows_a_taken_step_and_reads_nothing_where_there_is_no_store(tmp_path):
    assert read_steps(tmp_path / "none") == [] and not (tmp_path / "none").exists()
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    [row] = read_steps(s.path)
    assert row["old_id"] == b.binding_id and row["new_id"] == step.new_id
    assert row["line"] == f"graduated {b.binding_id} COOLING_OFF -> ON_LOOP under gp-1, 20/20 clean"


def test_the_view_asks_the_engine_to_preview_only_and_a_failing_engine_shows_no_cards(tmp_path):
    s = _store(tmp_path, _binding())
    asked = []

    def engine(apply):
        asked.append(apply)
        return _due(s, TestOnlyCoverage([]), apply=apply)

    view = graduation_view(s.path, engine)
    assert asked == [False] and len(view["cards"]) == 1 and view["steps"] == []

    def broken(apply):
        raise RuntimeError("no replay")

    assert graduation_view(s.path, broken)["cards"] == []
    assert graduation_view(None, None) == {"steps": [], "cards": []}


def _route(server) -> tuple[int, dict]:
    import levain.web_server as ws

    h = ws._Handler.__new__(ws._Handler)
    sent: list = []
    h.server = server
    h._off_box_token_valid = lambda: True
    h._send = lambda body, ctype, **kw: sent.append((kw.get("status", 200), json.loads(body)))
    h._serve_consent_body("/cockpit/consent/proposals.json", head=False)
    return sent[0]


def test_the_proposals_route_carries_the_graduations_beside_the_proposals(tmp_path):
    s = _store(tmp_path, _binding())
    status, body = _route(SimpleNamespace(request_gate=threading.Lock(), consent_reader=SimpleNamespace(),
                                          autonomic_store_dir=s.path,
                                          graduation_engine=lambda apply: _due(s, TestOnlyCoverage([]), apply=apply)))
    assert status == 200 and body["proposals"] == []
    [card] = body["graduations"]["cards"]
    assert card["to"] == Posture.ON_LOOP.name and card["command"] is None
    status, body = _route(SimpleNamespace(request_gate=threading.Lock(), consent_reader=SimpleNamespace()))
    assert body == {"proposals": []}                       # nothing wired: the payload as it was


def test_a_slow_engine_does_not_hold_the_request_gate(tmp_path):
    # RULINGS_R1 item 7: the cards are built after the gate is released, so a slow authority log never 503s the
    # other consent-body routes
    s = _store(tmp_path, _binding())
    gate = threading.Lock()
    free_while_building = []

    def engine(apply):
        took = gate.acquire(blocking=False)
        free_while_building.append(took)
        if took:
            gate.release()
        return _due(s, TestOnlyCoverage([]), apply=apply)

    status, body = _route(SimpleNamespace(request_gate=gate, consent_reader=SimpleNamespace(),
                                          autonomic_store_dir=s.path, graduation_engine=engine))
    assert status == 200 and free_while_building == [True] and len(body["graduations"]["cards"]) == 1


def test_read_steps_with_no_room_reads_nothing(tmp_path):
    # RULINGS_R1 item 11 (deepseek 5): sqlite's LIMIT -1 is every row
    s = _store(tmp_path, _binding())
    _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    assert len(read_steps(s.path)) == 1
    assert read_steps(s.path, limit=0) == [] and read_steps(s.path, limit=-1) == []
