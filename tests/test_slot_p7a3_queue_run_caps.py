"""P7-A3 CONTROL side: ``QUEUE-RESOLVE``'s ``run`` carries the optional ``max_calls`` / ``deadline`` the broker's
``RUN_OPTIONAL_KEYS`` accepts, and only when the ticket facts hold them."""
from __future__ import annotations

import pytest

from levain.qbroker.broker import cap_digest, slot_ref
from levain.slot import control as C
from tests.test_slot_s2a_control import S2, Env, to_rendered
from tests.test_slot_s3_queue_resolve import FACTS, as_dispatch, cap_of


@pytest.fixture
def env():
    facts: dict[str, dict] = {}
    e = Env(run_facts=lambda s: facts.get(s), host="linux")
    e.facts = facts
    yield e
    e.close()


def resolve(env: Env, facts: dict) -> dict:
    to_rendered(env, S2)
    as_dispatch(env, S2)
    env.facts[S2] = facts
    return env.client("qbroker").ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(cap_of(env, S2)))


def test_without_the_caps_the_answer_is_as_before(env):
    ans = resolve(env, dict(FACTS))
    print("BASELINE", ans)
    assert set(ans["run"]) == set(FACTS) | {"repo_root"}


def test_the_caps_ride_in_run_when_the_facts_hold_them(env):
    ans = resolve(env, {**FACTS, "max_calls": 40, "deadline": 1_900_000_000})
    print("WITH CAPS", ans)
    assert ans["ok"] and ans["run"]["max_calls"] == 40 and ans["run"]["deadline"] == 1_900_000_000


def test_one_cap_alone_rides_alone(env):
    ans = resolve(env, {**FACTS, "max_calls": 1})
    assert ans["run"]["max_calls"] == 1 and "deadline" not in ans["run"]


@pytest.mark.parametrize("bad", [
    {"max_calls": True}, {"max_calls": 0}, {"max_calls": 1.0}, {"max_calls": "3"}, {"max_calls": 10**9 + 1},
    {"deadline": True}, {"deadline": 0}, {"deadline": 1.5e9}, {"deadline": 10**12 + 1}, {"deadline": "1"},
    {"max_calls": None}, {"deadline": None},
])
def test_a_malformed_cap_is_run_malformed(env, bad):
    assert resolve(env, {**FACTS, **bad})["refused"] == "run-malformed"


def test_the_ticket_seam_derives_the_deadline_from_the_attach_clock(env):
    class T:
        run_id = "r-1"
        facts = {**{k: v for k, v in FACTS.items() if k != "run_id"}, "max_calls": 7, "deadline_s": 600}
    env.control._attached_at[S2] = 1000.0
    real = env.control.tickets
    env.control.tickets = type("S", (), {"bound": lambda self, s: T, "close": lambda self: None})()
    try:
        got = env.control._ticket_run_facts(S2)
        assert got["max_calls"] == 7 and got["deadline"] == 1600
        T.facts = {k: v for k, v in T.facts.items() if k not in ("max_calls", "deadline_s")}
        assert set(env.control._ticket_run_facts(S2)) == set(FACTS)
    finally:
        env.control.tickets = real


def _seam_resolve(env: Env, facts: dict, attached: float | None) -> dict:
    """QUEUE-RESOLVE through the real ticket seam (not a stub of the facts), with a given attach clock."""
    class T:
        run_id = "r-1"
    T.facts = {**{k: v for k, v in FACTS.items() if k != "run_id"}, **facts}
    if attached is not None:
        env.control._attached_at[S2] = attached
    env.control.tickets = type("S", (), {"bound": lambda self, s: T, "close": lambda self: None})()
    env.control._run_facts = env.control._ticket_run_facts
    to_rendered(env, S2)
    as_dispatch(env, S2)
    return env.client("qbroker").ask("QUEUE-RESOLVE", slot=slot_ref(0), cap_digest=cap_digest(cap_of(env, S2)))


def test_a_deadline_ticket_with_no_attach_clock_fails_closed(env):
    """A ticket with deadline_s but no attach entry (CONTROL restarted) must never answer max_calls without a deadline."""
    ans = _seam_resolve(env, {"max_calls": 7, "deadline_s": 600}, None)
    print("NO ATTACH CLOCK", ans)
    assert ans["refused"] == "run-malformed" and "run" not in ans


def test_a_deadline_ticket_with_an_attach_clock_still_answers_both(env):
    ans = _seam_resolve(env, {"max_calls": 7, "deadline_s": 600}, 1000.0)
    assert ans["run"]["max_calls"] == 7 and ans["run"]["deadline"] == 1600


def test_a_ticket_without_deadline_s_needs_no_attach_clock(env):
    ans = _seam_resolve(env, {"max_calls": 7}, None)
    assert ans["run"]["max_calls"] == 7 and "deadline" not in ans["run"]
