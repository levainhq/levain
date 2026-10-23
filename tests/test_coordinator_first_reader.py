"""Coordinator C5: the coordinator reads the question band first (gap 7), pinned to what the C5 runs showed
(the lane's scratch runs, before and after). Before C5 the coordinator's only path was ``levain question
answer``, which recorded its pick as the operator's (``via cli``) and locked the operator out. Keys, authority log, repo
and envelope are real (the C2 tests' fixtures); the store clock is fixed. Synthetic words."""
from __future__ import annotations

import argparse
import io
import sqlite3
from datetime import timedelta

import pytest

from levain.consent import ConsentQueue
from levain.consent import band as band_mod
from levain.consent.band import ANSWER_TAIL, EXIT_REFUSED, question_context, run_question
from levain.consent.questions import Link, QuestionStore
from levain.coordinator import first_reader as fr
from levain.coordinator import review
from levain.coordinator.store import CoordError, Part
from tests.test_consent_queue import Log
from tests.test_coordinator_envelope import _lease, ctx, keys, repo, snapshot  # noqa: F401 - fixtures

SEAT = "sess-worker-1"
COORD = "sess-coord"


class Band:
    """A thread under a live structured envelope, a worker launch pinned to it (``SEAT``), an active coordinator
    launch (``COORD``) and the question store, all in one scratch consent directory."""

    def __init__(self, c):
        self.c, self.store = c, c.store
        d, _fresh, self.body, _signed = c.structured("make the readme say hello, and land it")
        (self.thread,) = self.store.split(d.drop_id, [Part("work", "hello")])
        self.token = _lease(self.store)
        w = self.store.launch_request(self.token, harness="claude", threads=[self.thread.thread_id])
        self.worker, why = self.store.launch_activate(w.launch_id, harness_session_id=SEAT)
        assert why is None and self.worker.pinned_envelope_id == self.body["envelope_id"]
        k = self.store.launch_request(self.token, harness="claude", role="coordinator")
        assert self.store.launch_activate(k.launch_id, harness_session_id=COORD)[1] is None
        self.qs = QuestionStore(self.store.directory)

    def ask(self, what="rename helper to _x?", *, session=SEAT, cwd=None, link=None):
        return self.qs.file(agent="claude_code", agent_session_id=session, what=what,
                            options=[("a", "rename it"), ("b", "keep it")], rec="a", blocks="the rename",
                            cwd=cwd or self.c.path, link=link)

    def answer(self, q, option="a", **kw):
        return fr.answer(self.store, q.id, option=option, token=self.token, authority=self.c.snap, **kw)

    def reads(self):
        return self.c.rows("SELECT question_id, outcome FROM first_reads")


@pytest.fixture
def band(ctx):  # noqa: F811
    return Band(ctx)


def test_an_answer_inside_the_envelope_is_a_know_labelled_as_its_own_and_the_question_stays_open(band):
    q = band.ask()
    (r,) = fr.read_band(band.store, authority=band.c.snap)
    assert r.may_answer and r.envelope_id == band.body["envelope_id"] and r.thread_id == band.thread.thread_id
    item, reply_id = band.answer(q)
    assert (item.feed, item.by, item.authority_relevant, item.thread_id) == ("know", "coordinator", True,
                                                                            band.thread.thread_id)
    # nothing in questions.db: the operator's one answer slot is untouched
    assert band.qs.get(q.id).answer is None and review.question_open(band.store.directory)(q.id) is True
    text = question_context(band.store.directory, "claude_code", SEAT)
    assert text.splitlines() == [
        "levain: the coordinator, not the operator, answered question #1 «rename helper to _x?» on its own reading: "
        "option a «rename it».", band_mod.KNOW_TAIL, ANSWER_TAIL]
    assert question_context(band.store.directory, "claude_code", SEAT) is None          # once
    # the operator still answers, and his answer is delivered as his
    band.qs.answer(q.id, option="b", channel="cli")
    assert "(via cli): option b «keep it»" in question_context(band.store.directory, "claude_code", SEAT)
    assert band.reads() == [(q.id, "know")]
    assert fr.read_band(band.store, authority=band.c.snap) == []


def test_the_coordinator_can_never_answer_a_question_whose_option_is_a_consent_request(band, keys, tmp_path):  # noqa: F811
    """The test that tries: a would-push question (its option tied to a real consent request) inside a live envelope,
    from the right session and repo. The coordinator's answer is refused, nothing is written anywhere, and the
    request stays exactly as filed: only the operator's signed ruling ever moves it."""
    (tmp_path / "auth").mkdir()
    lg = Log(tmp_path / "auth", keys)
    lg.enrol("laptop", "laptop-presence")
    cq = ConsentQueue(band.store.directory, authority=lg.source)
    req = cq.file(agent="claude_code", agent_session_id=SEAT, tool_name="Bash",
                  tool_input={"command": "git push origin main"}, cwd=band.c.path, hold=timedelta(hours=3))
    before = cq.get(req.id)
    q = band.ask("push the rename now?", link=Link("a", "push", req.id, "q"))
    with pytest.raises(CoordError, match="tied to consent request .* the operator's ruling"):
        band.answer(q)
    with pytest.raises(CoordError, match="tied to consent request"):
        fr.answer(band.store, q.id, text="yes, push it", token=band.token, authority=band.c.snap)
    assert cq.get(req.id) == before
    assert band.qs.get(q.id).answer is None and band.reads() == []
    assert band.c.rows("SELECT COUNT(*) FROM replies WHERE kind = 'know'") == [(0,)]
    assert question_context(band.store.directory, "claude_code", SEAT) is None
    # what it may do is leave it to the operator
    fr.escalate(band.store, q.id, "the push is his ruling", token=band.token)
    assert band.reads() == [(q.id, "escalated")] and cq.get(req.id) == before


def _answer_args(qdir, ref, option):
    return argparse.Namespace(question_cmd="answer", queue_dir=qdir, ref=ref, option=option, text=None,
                              deliver=False)


def test_the_coordinator_session_is_refused_on_the_operators_answer_channel(band, monkeypatch):
    """C5 run 1: `levain question answer` from the coordinator recorded its pick `via cli` and refused the operator
    after it. From a session Levain started as the coordinator it is refused; from anywhere else it is unchanged."""
    q = band.ask()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", COORD)
    out = io.StringIO()
    assert run_question(_answer_args(band.store.directory, str(q.n), "a"), out) == EXIT_REFUSED
    assert "this session is the coordinator" in out.getvalue() and band.qs.get(q.id).answer is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SEAT)        # a worker's session is not the coordinator
    assert run_question(_answer_args(band.store.directory, str(q.n), "a"), io.StringIO()) == 0
    assert band.qs.get(q.id).answer.channel == "cli"


def test_a_codex_coordinator_session_is_refused_too(band, monkeypatch):
    q = band.ask()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", COORD)
    assert run_question(_answer_args(band.store.directory, str(q.n), "b"), io.StringIO()) == EXIT_REFUSED


@pytest.mark.parametrize("case", ["stranger", "outside", "unchecked", "killed", "stopped", "done"])
def test_everything_outside_the_envelope_escalates(band, case):
    if case == "stranger":
        q, why = band.ask(session="sess-yours"), "not one Levain started"
    elif case == "outside":
        q, why = band.ask(cwd=str(band.c.tmp)), "outside every repo"
    else:
        q = band.ask()
        why = {"unchecked": "no authority log", "killed": "was killed", "stopped": "stopped",
               "done": "is done"}[case]
        if case == "killed":
            band.store.attenuate(band.body["envelope_id"], "kill")
        elif case == "stopped":
            band.store._clock = _later
            band.token = band.store.coord_take()      # the run's lease lapsed with the clock; the envelope stopped
        elif case == "done":
            band.store.set_state(band.thread.thread_id, "done", by="coordinator", token=band.token)
    auth = None if case == "unchecked" else band.c.snap
    (r,) = fr.read_band(band.store, authority=auth)
    assert not r.may_answer and why in r.why
    with pytest.raises(CoordError, match="escalate"):
        fr.answer(band.store, q.id, option="a", token=band.token, authority=auth)
    assert band.reads() == [] and band.c.rows("SELECT COUNT(*) FROM review_items WHERE feed = 'know'") == [(0,)]


def _later():
    from tests.test_coordinator_envelope import NOW

    return NOW + timedelta(hours=3)


def test_an_escalation_is_an_answer_waiting_on_you_and_ends_the_first_read(band):
    q = band.ask(cwd=str(band.c.tmp))
    reply_id = fr.escalate(band.store, q.id, "outside the repo", token=band.token)
    waiting = review.waiting_on_you(band.store, sign_open=lambda _r: None,
                                    answer_open=review.question_open(band.store.directory))
    assert waiting["threads"][band.thread.thread_id][0]["reply_id"] == reply_id
    with pytest.raises(CoordError, match="already read first"):
        fr.escalate(band.store, q.id, "again", token=band.token)
    assert band.qs.get(q.id).answer is None


def test_a_stale_lease_token_writes_nothing(band):
    q = band.ask()
    with pytest.raises(CoordError, match="not the live lease"):
        fr.answer(band.store, q.id, option="a", token=band.token + 7, authority=band.c.snap)
    with pytest.raises(CoordError, match="not the live lease"):
        fr.escalate(band.store, q.id, "x", token=band.token + 7)
    assert band.reads() == [] and band.c.rows("SELECT COUNT(*) FROM replies WHERE kind IN ('know', 'answer')") == [(0,)]


def test_the_coordinator_cannot_clear_its_own_know(band):
    item, _ = band.answer(band.ask())
    with pytest.raises(CoordError, match="never by the coordinator"):
        review.clear_item(band.store, item.item_id, mode="coordinator", token=band.token)
    assert review.clear_item(band.store, item.item_id, mode="manual").state == "cleared"


def test_a_know_is_not_delivered_once_the_operator_has_answered(band):
    q = band.ask()
    band.answer(q)
    band.qs.answer(q.id, option="b", channel="phone")
    text = question_context(band.store.directory, "claude_code", SEAT)
    assert "coordinator" not in text and "(via phone): option b" in text


def test_an_option_the_question_does_not_have_is_refused(band):
    with pytest.raises(CoordError, match="has no option"):
        band.answer(band.ask(), option="zz")


def test_the_hook_path_reads_nothing_without_a_coordinator_store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    QuestionStore(d).file(agent="claude_code", agent_session_id=SEAT, what="w?", options=[("a", "x"), ("b", "y")],
                          rec="a", blocks="b", cwd=str(tmp_path))
    assert fr.claim_knows(d, "claude_code", SEAT) == []
    assert not (d / "dispatch.db").exists()
    assert question_context(d, "claude_code", SEAT) is None


def test_a_coordinator_session_is_recognised_only_by_a_coordinator_launch(band):
    assert fr.is_coordinator_session(band.store.directory, COORD)
    assert not fr.is_coordinator_session(band.store.directory, SEAT)
    assert not fr.is_coordinator_session(band.store.directory, None)
    assert not sqlite3.connect(band.store.path).execute(
        "SELECT 1 FROM launch_events WHERE harness_session_id = 'nobody'").fetchone()
