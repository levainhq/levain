"""The decision-model calibration's label layer (F8 slice a): labels read from a REAL consent queue (real enrolled keys,
real signatures, real SQLite), then deny rate, agreement against a judge's predictions, and proposal eligibility."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from levain.dispatch import DispatchError
from levain.dispatch.calibration import (CAVEAT, Label, agreement, deny_rate, proposal_eligible, read_labels)
from tests.test_consent_queue import NOW, signed
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures
from tests.test_consent_queue import _granted_queue
from tests.test_consent_queue_v4 import ruled

_n = itertools.count()


def file_req(q, *, tool="Edit", tier="T2", agent="claude_code", hold=timedelta(minutes=5)):
    i = next(_n)
    return q.file(agent=agent, agent_session_id="sess-1", tool_name=tool, tool_input={"command": f"touch /tmp/x{i}"},
                  cwd="/work/proj", hold=hold, tier=tier)


def decide(q, keys, request, verdict):
    payload, sig = signed(q, request, keys["phone"], verdict)
    q.answer(request.id, payload, sig)


def at(q):
    return NOW + timedelta(minutes=1)


def by_id(got):
    return {lab.request_id: lab for lab in got.labels}


def test_signed_allow_and_deny_become_labels_with_their_action_class(queue, keys):
    a = file_req(queue, tool="Edit", tier="T2")
    d = file_req(queue, tool="Bash", tier="T3", agent="codex")
    decide(queue, keys, a, "allow")
    decide(queue, keys, d, "deny")
    got = by_id(read_labels(queue.directory, now=at(queue)))
    assert (got[a.id].decision, got[a.id].via, got[a.id].action_class) == ("allow", "signature",
                                                                           ("claude_code", "Edit", "T2"))
    assert (got[d.id].decision, got[d.id].via, got[d.id].action_class) == ("deny", "signature",
                                                                           ("codex", "Bash", "T3"))
    assert got[a.id].decided_at is not None


def test_undecided_is_its_own_class_and_a_ruling_turns_it_into_a_label(queue, keys, clock):
    r1, r2 = file_req(queue), file_req(queue)
    queue.mark_undecided(r1.id)
    queue.mark_undecided(r2.id)
    clock.now = NOW + timedelta(minutes=10)
    got = by_id(read_labels(queue.directory, now=clock.now))
    assert got[r1.id].decision == "undecided" and got[r1.id].via is None
    payload, sig = ruled(queue, r2.id, keys["phone"], "deny")
    queue.answer_late(r2.id, payload, sig)
    got = by_id(read_labels(queue.directory, now=clock.now))
    assert got[r1.id].decision == "undecided"
    assert (got[r2.id].decision, got[r2.id].via) == ("deny", "ruling")


def test_withdrawn_and_pending_are_not_labels(queue):
    gone, waiting = file_req(queue), file_req(queue)
    assert queue.withdraw(gone.id)
    got = read_labels(queue.directory, now=at(queue))
    assert got.labels == () and got.excluded_grants == 0
    assert waiting.id  # still pending, never read as a label


def test_a_standing_grant_answer_is_excluded_and_counted(tmp_path, keys):
    log, q, ws, outside, ask = _granted_queue(tmp_path, keys)
    granted, d = ask({"file_path": str(ws / "sub" / "a.txt"), "old_string": "a", "new_string": "b"})
    assert d.via == "grant"
    hand = q.file(agent="claude_code", agent_session_id="s2", tool_name="Edit", tool_input={"file_path": "/w/x"},
                  cwd=str(ws), hold=timedelta(minutes=5))
    payload, sig = signed(q, hand, keys["laptop"], "allow")
    q.answer(hand.id, payload, sig)
    got = read_labels(q.directory, now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert got.excluded_grants == 1
    assert [lab.request_id for lab in got.labels] == [hand.id]


def test_a_naive_now_is_refused_as_the_job_store_refuses_it(tmp_path):
    """L4 F9: a naive now read an empty dir as an empty set; it is the same refusal the job store makes."""
    with pytest.raises(DispatchError, match="aware now"):
        read_labels(tmp_path / "nowhere", now=NOW.replace(tzinfo=None))
    with pytest.raises(DispatchError, match="aware now"):
        read_labels(tmp_path / "nowhere", now="2026-10-19T20:00:00Z")  # type: ignore[arg-type]


def test_a_missing_queue_is_an_empty_label_set(tmp_path):
    got = read_labels(tmp_path / "nowhere", now=NOW)
    assert got.labels == () and got.excluded_grants == 0
    assert not (tmp_path / "nowhere").exists()


def test_decisions_outside_the_span_are_not_read(queue, keys, clock):
    old = file_req(queue)
    decide(queue, keys, old, "allow")
    clock.now = NOW + timedelta(days=100)
    assert read_labels(queue.directory, now=clock.now, span=timedelta(days=90)).labels == ()
    assert len(read_labels(queue.directory, now=clock.now, span=timedelta(days=120)).labels) == 1


# --- deny rate and agreement over hand-built labels --------------------------------------------------------------

A = ("claude_code", "Edit", "T2")
B = ("claude_code", "Bash", "T3")


def L(rid, cls, decision):
    return Label(rid, cls, decision, None if decision == "undecided" else "signature", "2026-10-13T20:00:00Z")


def test_deny_rate_per_class_with_n_and_undecided_kept_apart():
    labels = [L("1", A, "allow"), L("2", A, "allow"), L("3", A, "deny"), L("4", A, "undecided"), L("5", B, "deny")]
    got = deny_rate(labels, incomplete=False)
    assert (got[A].n, got[A].denies, got[A].undecided, got[A].rate) == (3, 1, 1, Fraction(1, 3))
    assert (got[B].n, got[B].rate) == (1, Fraction(1))
    whole = deny_rate(labels, by_class=False, incomplete=False)["all"]
    assert (whole.n, whole.denies, whole.undecided) == (4, 2, 1)
    assert deny_rate([], incomplete=False) == {}


def test_agreement_counts_both_disagreement_directions_and_the_baseline():
    labels = [L("1", A, "allow"), L("2", A, "allow"), L("3", A, "allow"), L("4", A, "deny"), L("5", A, "deny")]
    preds = {"1": "allow", "2": "deny", "3": "allow", "4": "allow", "5": "deny"}
    r = agreement(labels, preds, incomplete=False)[A]
    assert (r.n_labelled, r.n_predicted, r.agree) == (5, 5, 3)
    assert (r.judge_allow_human_deny, r.judge_deny_human_allow, r.abstained) == (1, 1, 0)
    assert r.deny_rate == Fraction(2, 5) and r.baseline_agreement == Fraction(3, 5)   # 1 - deny_rate over all
    assert r.agreement == Fraction(3, 5)
    assert r.caveat == CAVEAT


def test_an_always_allow_judge_scores_exactly_the_baseline():
    labels = [L(str(i), A, "deny" if i % 4 == 0 else "allow") for i in range(1, 21)]
    preds = {lab.request_id: "allow" for lab in labels}
    r = agreement(labels, preds, incomplete=False)[A]
    assert r.agreement == r.baseline_agreement == Fraction(1) - r.deny_rate == Fraction(3, 4)
    assert proposal_eligible(r, min_n=1, min_agreement="0.5", min_coverage=0, max_dangerous=99)[0] is False


def test_abstain_and_missing_predictions_are_not_scored_and_the_baseline_follows_the_predicted_set():
    labels = [L("1", A, "allow"), L("2", A, "deny"), L("3", A, "deny"), L("4", A, "undecided")]
    r = agreement(labels, {"1": "allow", "2": "abstain", "4": "deny", "zzz": "allow"}, incomplete=False)[A]
    assert (r.n_labelled, r.n_predicted, r.abstained, r.undecided) == (3, 1, 2, 1)   # 3 had no prediction
    assert r.agreement == Fraction(1) and r.baseline_agreement == Fraction(1)         # over the one predicted
    assert r.deny_rate == Fraction(2, 3)


def test_a_class_with_no_prediction_has_no_agreement_and_no_baseline():
    r = agreement([L("1", A, "allow")], {}, incomplete=False)[A]
    assert (r.agreement, r.baseline_agreement, r.n_predicted) == (None, None, 0)


def test_a_prediction_value_outside_the_three_is_refused():
    with pytest.raises(DispatchError):
        agreement([L("1", A, "allow")], {"1": "maybe"}, incomplete=False)


# --- eligibility -------------------------------------------------------------------------------------------------

def good_report(**override):
    """40 labelled: 30 allowed, 10 denied; the judge right on all but one of them (a costly one)."""
    labels = [L(f"a{i}", A, "allow") for i in range(30)] + [L(f"d{i}", A, "deny") for i in range(10)]
    preds = {lab.request_id: lab.decision for lab in labels}
    preds["a0"] = "deny"
    r = agreement(labels, preds, incomplete=False)[A]
    return r if not override else type(r)(**{**r.__dict__, **override})


def test_a_class_that_clears_every_test_is_eligible():
    assert proposal_eligible(good_report(), min_n=30, min_agreement=Fraction(9, 10), min_coverage=0, max_dangerous=0) == (True, "")


@pytest.mark.parametrize("kw, needle", [
    (dict(min_n=41, min_agreement="0.9", min_coverage=0, max_dangerous=0), "below min_n=41"),
    (dict(min_n=30, min_agreement="0.99", min_coverage=0, max_dangerous=0), "is below 99/100"),
    (dict(min_n=30, min_agreement=0.9, min_coverage=0, max_dangerous=-0 + 0), None),
])
def test_each_threshold_can_fail_alone(kw, needle):
    ok, why = proposal_eligible(good_report(), **kw)
    if needle is None:
        assert ok and why == ""
    else:
        assert not ok and needle in why


def test_a_dangerous_disagreement_over_the_ceiling_fails():
    r = good_report(judge_allow_human_deny=2)
    ok, why = proposal_eligible(r, min_n=30, min_agreement="0.9", min_coverage=0, max_dangerous=1)
    assert not ok and "2 judge-allow-human-deny" in why
    assert proposal_eligible(r, min_n=30, min_agreement="0.9", min_coverage=0, max_dangerous=2)[0] is True


def test_not_beating_the_baseline_fails_even_with_high_agreement():
    labels = [L(f"a{i}", A, "allow") for i in range(39)] + [L("d", A, "deny")]
    r = agreement(labels, {lab.request_id: "allow" for lab in labels}, incomplete=False)[A]      # 39/40 = the baseline exactly
    ok, why = proposal_eligible(r, min_n=10, min_agreement="0.9", min_coverage=0, max_dangerous=5)
    assert not ok and "always-allow baseline" in why


def test_every_failing_reason_is_named_together():
    labels = [L("1", A, "deny")]
    r = agreement(labels, {"1": "allow"}, incomplete=False)[A]
    ok, why = proposal_eligible(r, min_n=5, min_agreement="0.9", min_coverage=0, max_dangerous=0)
    assert not ok and why.count(";") == 3


def test_threshold_arguments_are_validated():
    r = good_report()
    for bad in (dict(min_n=0, min_agreement="0.9", min_coverage=0, max_dangerous=0),
                dict(min_n=1, min_agreement=2, min_coverage=0, max_dangerous=0),
                dict(min_n=1, min_agreement="x", min_coverage=0, max_dangerous=0),
                dict(min_n=1, min_agreement="0.9", min_coverage=0, max_dangerous=-1)):
        with pytest.raises(DispatchError):
            proposal_eligible(r, **bad)


def test_end_to_end_from_a_real_queue(queue, keys):
    reqs = [file_req(queue) for _ in range(6)]
    for i, r in enumerate(reqs):
        decide(queue, keys, r, "deny" if i < 2 else "allow")
    labels = read_labels(queue.directory, now=at(queue)).labels
    judge = {r.id: ("deny" if i < 2 else "allow") for i, r in enumerate(reqs)}
    rep = agreement(labels, judge, incomplete=False)[("claude_code", "Edit", "T2")]
    assert (rep.n_labelled, rep.agree, rep.judge_allow_human_deny, rep.deny_rate) == (6, 6, 0, Fraction(1, 3))
    assert proposal_eligible(rep, min_n=6, min_agreement=1, min_coverage=0, max_dangerous=0) == (True, "")
    assert proposal_eligible(rep, min_n=7, min_agreement=1, min_coverage=0, max_dangerous=0)[0] is False


def test_a_judge_that_abstains_on_the_hard_calls_fails_the_coverage_floor():
    """L1 NOTE N3: abstaining on everything hard could still pass min_n. 80 labelled, the judge answers 30 (all
    right, beating the baseline), so its coverage is 30/80."""
    labels = [L(f"a{i}", A, "allow") for i in range(60)] + [L(f"d{i}", A, "deny") for i in range(20)]
    preds = {f"a{i}": "allow" for i in range(20)} | {f"d{i}": "deny" for i in range(10)}
    r = agreement(labels, preds, incomplete=False)[A]
    assert (r.n_labelled, r.n_predicted, r.abstained) == (80, 30, 50)
    ok, why = proposal_eligible(r, min_n=30, min_agreement="0.9", min_coverage="0.5", max_dangerous=0)
    assert not ok and "answered 30 of 80 labelled requests" in why and "below 1/2" in why
    assert proposal_eligible(r, min_n=30, min_agreement="0.9", min_coverage=Fraction(3, 8), max_dangerous=0) == (
        True, "")


def test_min_coverage_is_required_and_validated():
    r = good_report()
    with pytest.raises(TypeError):
        proposal_eligible(r, min_n=1, min_agreement="0.9", max_dangerous=0)       # type: ignore[call-arg]
    for bad in ("1.5", -1, "x", True):
        with pytest.raises(DispatchError):
            proposal_eligible(r, min_n=1, min_agreement="0.9", min_coverage=bad, max_dangerous=0)


# --- fix round 2 (L3 r1) -----------------------------------------------------------------------------------------

def test_a_request_counts_once_however_many_times_its_label_is_repeated():
    # codex: one allow and one deny repeated twenty times scored as 40 decisions and passed min_n=30
    labels = [L("1", A, "allow"), L("2", A, "deny")] * 20
    preds = {"1": "allow", "2": "deny"}
    r = agreement(labels, preds, incomplete=False)[A]
    assert (r.n_labelled, r.n_predicted, r.agree) == (2, 2, 2)
    ok, why = proposal_eligible(r, min_n=30, min_agreement="0.9", min_coverage=0, max_dangerous=0)
    assert ok is False and "below min_n=30" in why
    assert deny_rate(labels, incomplete=False)[A].n == 2


def test_two_different_labels_for_one_request_are_refused():
    for other in (L("1", A, "deny"), L("1", B, "allow")):
        with pytest.raises(DispatchError, match="two different labels"):
            agreement([L("1", A, "allow"), other], {"1": "allow"}, incomplete=False)
        with pytest.raises(DispatchError, match="two different labels"):
            deny_rate([L("1", A, "allow"), other], incomplete=False)


def test_an_overflowing_listing_marks_the_set_incomplete_and_no_proposal_rests_on_it(monkeypatch):
    # codex + complement: the undecided cap dropped requests with no signal
    from levain.consent import listing as listing_mod
    from levain.dispatch import calibration as cal
    full = listing_mod.ConsentListing(overflow=True)
    monkeypatch.setattr(cal, "read_listing", lambda *a, **k: full)
    got = read_labels("/nonexistent", now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert got.incomplete is True
    monkeypatch.setattr(cal, "read_listing", lambda *a, **k: listing_mod.ConsentListing())
    assert read_labels("/nonexistent", now=datetime(2026, 10, 19, tzinfo=timezone.utc)).incomplete is False

    labels = [L(f"a{i}", A, "allow") for i in range(30)] + [L(f"d{i}", A, "deny") for i in range(10)]
    preds = {lab.request_id: lab.decision for lab in labels}
    from levain.dispatch.calibration import LabelSet
    r = agreement(LabelSet(tuple(labels), incomplete=True), preds)[A]
    assert r.incomplete is True
    ok, why = proposal_eligible(r, min_n=30, min_agreement="0.9", min_coverage=0, max_dangerous=0)
    assert ok is False and "incomplete" in why
    assert all(rate.incomplete for rate in deny_rate(LabelSet(tuple(labels), incomplete=True)).values())
    complete = agreement(LabelSet(tuple(labels)), preds)[A]
    assert complete.incomplete is False
    assert proposal_eligible(complete, min_n=30, min_agreement="0.9", min_coverage=0, max_dangerous=0) == (True, "")
    assert agreement(labels, preds, incomplete=True)[A].incomplete is True


def test_a_plain_label_list_must_state_whether_it_is_complete():
    # a list cut from an incomplete LabelSet must not read as complete: the caller states it or is refused
    from levain.dispatch.calibration import LabelSet
    cut = LabelSet((L("1", A, "allow"),), incomplete=True).labels
    with pytest.raises(DispatchError, match="incomplete="):
        agreement(cut, {"1": "allow"})
    with pytest.raises(DispatchError, match="incomplete="):
        deny_rate(cut)


# --- F8 slice b: the judge replay -------------------------------------------------------------------------------

from dataclasses import fields  # noqa: E402

from levain.dispatch.calibration import JudgeContext, replay, report  # noqa: E402


class Fixed:
    local = True

    def __init__(self, said):
        self.said, self.seen = said, []

    def predict(self, context):
        self.seen.append(context)
        return self.said


def seeded(queue, keys, denies=2, allows=4):
    reqs = [file_req(queue) for _ in range(denies + allows)]
    for i, r in enumerate(reqs):
        decide(queue, keys, r, "deny" if i < denies else "allow")
    return read_labels(queue.directory, now=at(queue)), reqs


def test_the_judge_sees_the_request_and_no_part_of_the_answer(queue, keys):
    got, reqs = seeded(queue, keys)
    assert {f.name for f in fields(JudgeContext)} == {"agent", "tool_name", "display", "tier"}
    judge = Fixed("allow")
    replay(got, judge)
    assert len(judge.seen) == 6 and all(type(c) is JudgeContext for c in judge.seen)
    blob = " ".join(repr(c) for c in judge.seen)
    for r in reqs:
        assert r.id not in blob                                          # the id is not shown
    for word in ("signature", "ruling", "grant", "phone", "denied", "allowed"):
        assert word not in blob
    assert judge.seen[0].tool_name == "Edit" and judge.seen[0].tier == "T2" and judge.seen[0].agent == "claude_code"
    assert "touch /tmp/x" in judge.seen[0].display


def test_a_judge_that_raises_is_an_abstain_with_its_class_name(queue, keys):
    got, _ = seeded(queue, keys)

    class Boom:
        local = True

        def predict(self, context):
            raise KeyError("x")

    run = replay(got, Boom())
    assert set(run.predictions.values()) == {"abstain"} and set(run.raised.values()) == {"KeyError"}
    assert len(run.raised) == 6

    class Weird(Boom):
        def predict(self, context):
            return "maybe"

    assert set(replay(got, Weird()).raised.values()) == {"InvalidPrediction"}


def test_a_judge_not_marked_local_is_refused(queue, keys):
    got, _ = seeded(queue, keys)

    class Remote:
        local = False

        def predict(self, context):
            return "allow"

    class Unmarked:
        def predict(self, context):
            return "allow"

    for judge in (Remote(), Unmarked()):
        with pytest.raises(DispatchError, match="local"):
            replay(got, judge)
        with pytest.raises(DispatchError, match="local"):
            report(got, judge, min_n=1, min_agreement=0, min_coverage=0, max_dangerous=0)


def test_replay_needs_a_label_set_and_skips_the_undecided(queue, keys):
    got, _ = seeded(queue, keys, 1, 1)
    with pytest.raises(DispatchError, match="LabelSet"):
        replay(list(got.labels), Fixed("allow"))
    queue_req = file_req(queue)
    queue.mark_undecided(queue_req.id)
    both = read_labels(queue.directory, now=NOW + timedelta(minutes=10))
    assert any(lab.decision == "undecided" for lab in both.labels)
    judge = Fixed("deny")
    assert len(replay(both, judge).replayed) == 2 and len(judge.seen) == 2


def test_report_end_to_end_on_a_real_queue_with_both_fixture_judges(queue, keys):
    got, _ = seeded(queue, keys, denies=2, allows=4)
    kw = dict(min_n=6, min_agreement="0.9", min_coverage=1, max_dangerous=0)
    allow = report(got, Fixed("allow"), **kw)
    deny = report(got, Fixed("deny"), **kw)
    cls = ("claude_code", "Edit", "T2")
    a, d = allow.classes[cls], deny.classes[cls]
    assert (a.deny_rate.n, a.deny_rate.denies, a.deny_rate.rate) == (6, 2, Fraction(1, 3))
    assert a.report.agreement == Fraction(4, 6) == a.report.baseline_agreement      # always-allow IS the baseline
    assert (a.report.judge_allow_human_deny, d.report.judge_deny_human_allow) == (2, 4)
    assert d.report.agreement == Fraction(2, 6)
    assert not a.eligible and "does not beat the always-allow baseline" in a.why_not
    assert not d.eligible
    assert allow.caveat == CAVEAT and (allow.replayed, allow.raised, allow.incomplete) == (6, 0, False)


def test_a_limit_samples_and_the_report_says_it_is_incomplete(queue, keys):
    got, _ = seeded(queue, keys)
    rep = report(got, Fixed("allow"), min_n=1, min_agreement=0, min_coverage=0, max_dangerous=9, limit=3)
    entry = rep.classes[("claude_code", "Edit", "T2")]
    assert rep.replayed == 3 and rep.incomplete and entry.report.n_labelled == 3
    assert not entry.eligible and "incomplete" in entry.why_not
