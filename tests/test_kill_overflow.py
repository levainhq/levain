"""The kill substrate never raises on a number past float range (1026-22 RULINGS "G1", O-g1f1-1).

Event fields are untrusted and reach the gate's known-danger kill (``kill_trips``); an integer JSON carries but a float
cannot hold is a value the kill cannot compare, so it is UNKNOWN (and ``kill_trips`` stops on UNKNOWN), never an
OverflowError out of the evaluator.
"""
from __future__ import annotations

import pytest

from levain.autonomic.kill import Kleene, kill_outcome, kill_purity_error, kill_trips

HUGE = 10 ** 400


@pytest.mark.parametrize("predicate", [
    {"op": ">", "field": "n", "value": 5},
    {"op": "<=", "field": "n", "value": 5},
    {"op": "!=", "field": "n", "value": 5},
    {"op": "==", "field": "n", "value": 5},
    {"op": "in", "field": "n", "value": [5, 6]},
    {"op": "contains", "field": "n", "value": 5},
    {"op": "not", "clause": {"op": ">", "field": "n", "value": 5}},
])
def test_kill_trips_on_an_event_field_past_float_range_is_unknown_not_a_raise(predicate):
    event = {"type": "email", "fields": {"n": HUGE}}
    outcome = kill_outcome(predicate, event)
    assert outcome in (Kleene.UNKNOWN, Kleene.FALSE, Kleene.TRUE)
    assert kill_trips(predicate, event) is (outcome is not Kleene.FALSE)


def test_a_numeric_compare_against_a_field_past_float_range_is_unknown_so_it_trips():
    event = {"fields": {"n": HUGE}}
    for op in (">", ">=", "<", "<="):
        kp = {"op": op, "field": "n", "value": 5}
        assert kill_outcome(kp, event) is Kleene.UNKNOWN and kill_trips(kp, event)


def test_an_equality_kill_against_a_field_past_float_range_is_not_a_confident_let_through():
    # a huge number against a float-range threshold: `==` cannot be confidently decided, so `!=` must not be a
    # confident TRUE that hides it, and `==` must not be a confident FALSE that lets the fire through
    event = {"fields": {"n": HUGE}}
    assert kill_trips({"op": "==", "field": "n", "value": 5}, event)
    assert kill_trips({"op": "!=", "field": "n", "value": 5}, event)


def test_a_list_field_holding_a_number_past_float_range_does_not_raise():
    event = {"fields": {"n": [1, HUGE]}}
    assert kill_outcome({"op": "contains", "field": "n", "value": 5}, event) is Kleene.FALSE
    assert kill_outcome({"op": "contains", "field": "n", "value": 1}, event) is Kleene.TRUE


@pytest.mark.parametrize("predicate", [
    {"op": ">", "field": "n", "value": HUGE},
    {"op": "<", "field": "n", "value": -HUGE},
])
def test_a_numeric_threshold_past_float_range_is_a_purity_error_not_a_raise(predicate):
    why = kill_purity_error(predicate)
    assert why is not None and "finite-numeric" in why
    assert str(HUGE) not in why            # the message names the problem, never a 401-digit number


def test_an_in_list_or_equality_threshold_past_float_range_evaluates_without_raising():
    assert kill_purity_error({"op": "in", "field": "n", "value": [HUGE]}) is None
    assert kill_outcome({"op": "in", "field": "n", "value": [HUGE]}, {"fields": {"n": 1}}) is Kleene.FALSE
    assert kill_outcome({"op": "in", "field": "n", "value": [HUGE]}, {"fields": {"n": HUGE}}) is Kleene.TRUE
