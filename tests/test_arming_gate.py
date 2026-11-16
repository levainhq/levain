"""The night desk's arming gate (ND-11a, board levain#42): a pure check over his three facts, default closed.

DONE line: ``arming_refusal(order, facts)`` refuses an order that has an effect line while any of the ND-7 ruling, the
ND-8 done or the ND-9 read is missing, each fact carrying its np id; an order whose effect_lines is present and an empty
list (judgement lines only, or a ping) passes in every state, and every other shape of order is treated as effects."""
from __future__ import annotations

import pytest

from levain.desk import arming_gate as ag

LINE = {"verb": "restart_service", "args": {"host": ["taoserv"]}, "per_incident": 1, "per_night": 2}
EFFECT_ORDER = {"id": "so-1", "effect_lines": [LINE], "judgement_lines": ["diagnose"]}
JUDGE_ORDER = {"id": "so-2", "effect_lines": [], "judgement_lines": ["diagnose"]}
FULL = {"nd7_ruled": "np-0123abcd", "nd8_done": "np-1123abcd", "nd9_read": "np-2123abcd"}


def without(key):
    return {k: v for k, v in FULL.items() if k != key}


def test_all_three_facts_open_the_gate():
    assert ag.arming_refusal(EFFECT_ORDER, FULL) is None


@pytest.mark.parametrize("key", sorted(FULL))
def test_each_missing_fact_refuses_and_names_it(key):
    why = ag.arming_refusal(EFFECT_ORDER, without(key))
    assert why and key in why
    for other in FULL:
        if other != key:
            assert other not in why


def test_default_is_closed():
    for facts in (None, {}, "yes", [], {"nd7_ruled": True, "nd8_done": True, "nd9_read": True}):
        assert ag.arming_refusal(EFFECT_ORDER, facts)
    assert ag.arming_refusal(EFFECT_ORDER)  # facts omitted
    assert ag.CLOSED_FACTS == {} and ag.arming_refusal(EFFECT_ORDER, ag.CLOSED_FACTS)


@pytest.mark.parametrize("bad", ["", "np-xyz", "np-0123ABCD", "np-0123abc", "0123abcd", "np-0123abcd0", None, 7, True])
def test_a_fact_without_a_real_np_id_is_missing(bad):
    facts = dict(FULL, nd8_done=bad)
    why = ag.arming_refusal(EFFECT_ORDER, facts)
    assert why and "nd8_done" in why


def test_all_missing_names_all_three():
    why = ag.arming_refusal(EFFECT_ORDER, {})
    assert all(k in why for k in FULL)


@pytest.mark.parametrize("facts", [None, {}, FULL, without("nd7_ruled")])
def test_judgement_only_orders_and_pings_pass_in_every_state(facts):
    assert ag.arming_refusal(JUDGE_ORDER, facts) is None
    assert ag.arming_refusal({"kind": "ping", "effect_lines": []}, facts) is None


class _Lines(list):
    pass


# The one open path is a mapping whose effect_lines is present and an empty list. Everything else may carry effects.
UNREADABLE_ORDERS = [
    {},                                             # no effect_lines key
    {"id": "so-3", "judgement_lines": ["x"]},       # judgement lines, effect_lines absent
    {"kind": "ping"},                               # a ping that does not say it has no effect lines
    {"id": "so-4", "effect_line": [LINE]},          # misspelt key
    {"id": "so-5", "effects": [LINE]},              # renamed key
    {"effect_lines": None},                         # JSON null
    {"effect_lines": ()},                           # empty, but not a list
    {"effect_lines": (LINE,)},
    {"effect_lines": "restart"},
    {"effect_lines": ""},
    {"effect_lines": {"verb": "x"}},
    {"effect_lines": {}},
    {"effect_lines": 5},
    {"effect_lines": 0},
    {"effect_lines": False},
    {"effect_lines": {LINE["verb"]}},               # a set
    {"effect_lines": _Lines()},                     # a list subclass is not a list
    None,                                           # a failed lookup
    [LINE],                                         # the effect lines without their order
    [],
    (),
    '{"effect_lines": [{"verb": "restart_service"}]}',  # an order never parsed
    "",
    0,
    EFFECT_ORDER.items(),
]


@pytest.mark.parametrize("order", UNREADABLE_ORDERS, ids=range(len(UNREADABLE_ORDERS)))
def test_anything_but_a_present_empty_effect_lines_list_is_closed(order):
    why = ag.arming_refusal(order, None)
    assert why and all(k in why for k in FULL)
    assert ag.arming_refusal(order, FULL) is None  # the facts on record open it, as for any effect order


def test_a_mapping_that_fails_to_read_is_closed():
    class Broken(dict):
        def __getitem__(self, key):
            raise KeyError(key)

        def get(self, key, default=None):
            raise RuntimeError("store gone")

    assert ag.arming_refusal(Broken(effect_lines=[]), None)


def test_one_np_id_cannot_stand_for_two_facts():
    for twin in ({"nd7_ruled": "np-0123abcd", "nd8_done": "np-0123abcd", "nd9_read": "np-2123abcd"},
                 dict.fromkeys(FULL, "np-00000000")):
        why = ag.arming_refusal(EFFECT_ORDER, twin)
        assert why and "same np id" in why


def test_the_exported_facts_are_immutable():
    refused = (TypeError, AttributeError)  # a read-only mapping has no pop/clear/update at all
    with pytest.raises(refused):
        ag.FACTS.pop("nd9_read")
    with pytest.raises(refused):
        ag.FACTS.clear()
    with pytest.raises(TypeError):
        ag.FACTS["nd9_read"] = "x"
    with pytest.raises(TypeError):
        del ag.FACTS["nd9_read"]
    with pytest.raises(refused):
        ag.CLOSED_FACTS.update(FULL)
    with pytest.raises(TypeError):
        ag.CLOSED_FACTS["nd7_ruled"] = "np-0123abcd"
    assert set(ag.FACTS) == set(FULL) and len(ag.CLOSED_FACTS) == 0


def test_rebinding_the_exported_names_does_not_change_the_decision(monkeypatch):
    monkeypatch.setattr(ag, "FACTS", {})
    monkeypatch.setattr(ag, "CLOSED_FACTS", dict(FULL))
    for key in FULL:
        why = ag.arming_refusal(EFFECT_ORDER, without(key))
        assert why and key in why
    assert ag.arming_refusal(EFFECT_ORDER, ag.CLOSED_FACTS) is None  # the facts given, not the exported name, decide
    assert ag.arming_refusal(EFFECT_ORDER, {})


def test_the_gate_does_not_mutate_its_inputs():
    order, facts = {"effect_lines": [dict(LINE)]}, dict(FULL)
    ag.arming_refusal(order, facts)
    assert order == {"effect_lines": [LINE]} and facts == FULL
