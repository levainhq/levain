"""The graduation card, the steps FEED and their hook beside the proposals route (14b S4, DESIGN_REV2 §5, REV3 §3,
§6). The steps come from ``graduate_due`` over a real store with the test-only coverage source."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from levain.autonomic import ActionManifest, ActionRisk, Posture, RiskClass
from levain.autonomic.binding import binding_family
from levain.cockpit.graduation_cards import CARD_DAYS, card_of, graduation_view, policy_create_line, read_steps
from tests.autonomic_test_only_coverage import TestOnlyCoverage
from tests.test_autonomic_graduation_due import LATER, LOW, _binding, _due, _store, policy

HIGH = ActionRisk(cls=RiskClass.HIGH, reversible=False, external=True, financial=False)


def test_an_uncovered_step_gets_one_line_narrowed_to_its_family_at_the_target_rung(tmp_path):
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, TestOnlyCoverage([]))
    card = card_of(step)
    fam = binding_family(b)
    assert card["command"] == (f"levain binding policy create --action deliver=LOW --lowest ON_LOOP --days {CARD_DAYS} "
                               f"--family {fam} --store-dir {s.path} --guard <guard.json> --signing-key <key>")
    assert card["from"] == "COOLING_OFF" and card["to"] == "ON_LOOP" and card["clean_count"] == 20
    assert card["actions"] == [{"link": 0, "action": "deliver", "risk": {"cls": 1, "reversible": True,
                                                                          "external": False, "financial": False},
                                "covered": False}]
    assert "no live policy" in card["why"] and "lets deliver run at ON_LOOP" in card["note"]


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
    assert card["to"] == Posture.ON_LOOP.name and card["command"].startswith("levain binding policy create ")
    status, body = _route(SimpleNamespace(request_gate=threading.Lock(), consent_reader=SimpleNamespace()))
    assert body == {"proposals": []}                       # nothing wired: the payload as it was


def test_the_line_names_a_store_that_is_not_the_default_and_none_that_is(tmp_path, monkeypatch):
    # RULINGS_R1 item 5: copying the line signs the store the engine runs in
    import shlex

    s = _store(tmp_path, _binding())
    [step] = _due(s, TestOnlyCoverage([]))
    assert f"--store-dir {shlex.quote(str(s.path))}" in card_of(step)["command"]
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    from levain.autonomic.db import default_store_dir
    home = _store_at(default_store_dir(), _binding())
    [step] = _due(home, TestOnlyCoverage([]))
    assert "--store-dir" not in card_of(step)["command"]


def _store_at(path, b):
    from levain.autonomic import BindingStore
    st = BindingStore(path)
    st.add(b)
    return st


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


def test_the_line_names_its_paths_absolute_whatever_the_engine_was_given(tmp_path, monkeypatch):
    # RULINGS_R2 item 3: a line copied to another directory signs the same store into the same cockpit
    import shlex

    monkeypatch.chdir(tmp_path)
    line = policy_create_line({"deliver": LOW}, "ON_LOOP", 30, ["fam"], store_dir="st/store", data_root="ck")
    assert f"--store-dir {shlex.quote(str((tmp_path / 'st' / 'store').resolve()))}" in line
    assert f"--data-root {shlex.quote(str((tmp_path / 'ck').resolve()))}" in line
