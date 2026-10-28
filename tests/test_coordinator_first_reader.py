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
from tests._h4 import h4_home

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
    cq = ConsentQueue(band.store.directory, authority=lg.source, home=h4_home())
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
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SEAT)        # Q1 (a): any agent session is refused, not only the coordinator
    assert run_question(_answer_args(band.store.directory, str(q.n), "a"), io.StringIO()) == EXIT_REFUSED
    assert band.qs.get(q.id).answer is None
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    # a Codex child that carries no marker variable is still an agent session: its `codex` ancestor says so
    monkeypatch.setattr(band_mod, "_ancestors", lambda: iter([(10, ["/bin/bash", "-lc", "levain question answer"]),
                                                              (42, ["codex", "exec", "go"]), (1111, ["-zsh"])]))
    assert run_question(_answer_args(band.store.directory, str(q.n), "a"), io.StringIO()) == EXIT_REFUSED
    monkeypatch.setattr(band_mod, "_ancestors", lambda: iter([(1111, ["-zsh"])]))
    monkeypatch.setenv("CODEX_SANDBOX", "danger-full-access")   # ... and so does CODEX_SANDBOX
    assert run_question(_answer_args(band.store.directory, str(q.n), "a"), io.StringIO()) == EXIT_REFUSED
    assert band.qs.get(q.id).answer is None
    monkeypatch.delenv("CODEX_SANDBOX")                       # a terminal outside any agent session is unchanged
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
    assert fr.claim_knows(d, "claude_code", SEAT, band_mod.know_line) == []
    assert not (d / "dispatch.db").exists()
    assert question_context(d, "claude_code", SEAT) is None


def test_a_coordinator_session_is_recognised_only_by_a_coordinator_launch(band):
    assert fr.is_coordinator_session(band.store.directory, COORD)
    assert not fr.is_coordinator_session(band.store.directory, SEAT)
    assert not fr.is_coordinator_session(band.store.directory, None)
    assert not sqlite3.connect(band.store.path).execute(
        "SELECT 1 FROM launch_events WHERE harness_session_id = 'nobody'").fetchone()


# --- C5 fix round (L3 r1): the door, the stale reads, the realpath, format-before-mark ----------------------------
def _seam(monkeypatch, store, act):
    """Run ``act`` (the operator, between the coordinator's read and its fenced transaction) as ``store._write`` opens."""
    import contextlib

    orig = store._write

    @contextlib.contextmanager
    def write(*a, **k):
        act()
        with orig(*a, **k) as got:
            yield got

    monkeypatch.setattr(store, "_write", write)


def test_the_coordinators_question_verbs_pass_its_door(band, monkeypatch, capsys):
    """L3 r1 codex HIGH, reproduced by run_door_BEFORE.txt: run from its recorded workspace, the coordinator's three
    question verbs (the ones its brief names) were refused by the door before they ran."""
    from levain.cli import main
    from levain.coordinator import cli as ccli
    from levain.coordinator import door

    q1, q2 = band.ask(), band.ask("clean my home?", cwd=str(band.c.tmp))
    ws = band.c.tmp / "coordws"
    (ws / "sub").mkdir(parents=True)
    door.record(ws)
    monkeypatch.chdir(ws / "sub")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", COORD)
    monkeypatch.setattr(ccli, "_store", lambda args: band.store)          # the store on the test's fixed clock
    monkeypatch.setattr(ccli, "_snapshot", lambda args, store: band.c.snap)
    monkeypatch.setattr(ccli, "_head_reader", lambda args, store: lambda: (band.c.snap.seq, band.c.snap.head_hash))
    qd = ["--queue-dir", str(band.store.directory)]
    assert main(["coord", "questions", *qd]) == 0, capsys.readouterr().err
    assert "MAY ANSWER" in capsys.readouterr().out
    assert main(["coord", "answer", str(q1.n), "--option", "a", "--token", str(band.token), *qd]) == 0
    assert main(["coord", "escalate", str(q2.n), "--why", "outside", "--token", str(band.token), *qd]) == 0
    assert sorted(band.reads()) == sorted([(q1.id, "know"), (q2.id, "escalated")])
    assert main(["coord", "take", *qd]) == 1             # a verb not in the door stays refused there
    assert "is the operator's, and this is the coordinator's workspace" in capsys.readouterr().err


def test_an_operator_answer_between_the_read_and_the_write_files_no_know(band, monkeypatch):
    """L3 r1 consensus MED: ``answer`` checked the question it read BEFORE its fenced transaction. The operator answers
    in that gap: nothing is filed."""
    q = band.ask()
    _seam(monkeypatch, band.store, lambda: band.qs.answer(q.id, option="b", channel="cli"))
    with pytest.raises(CoordError, match="the operator has answered it"):
        band.answer(q)
    assert band.reads() == [] and band.c.rows("SELECT COUNT(*) FROM replies WHERE kind = 'know'") == [(0,)]
    assert band.c.rows("SELECT COUNT(*) FROM review_items WHERE feed = 'know'") == [(0,)]


def test_a_signer_revoked_between_the_snapshot_and_the_write_files_no_know(band, keys, tmp_path, monkeypatch):  # noqa: F811
    """L3 r1 codex MED: the CLI took the authority snapshot before the fenced transaction. The envelope's signer is
    revoked in that gap: the log's head moved, so the snapshot is taken again (outside the lock, L3 r2), and nothing
    is filed."""
    from levain.coordinator import cli as ccli

    (tmp_path / "auth2").mkdir()
    lg = Log(tmp_path / "auth2", keys)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("phone", "display-phone")
    monkeypatch.setattr(ccli, "_snapshot", lambda args, store: lg.source())
    monkeypatch.setattr(ccli, "_head_reader", lambda args, store: lg.source.head)
    q = band.ask()
    assert fr.read_band(band.store, authority=lg.source())[0].may_answer      # live before the revoke
    _held(monkeypatch, band.store, act=lambda: lg.revoke("phone"))      # once: the retry re-opens the lock
    args = argparse.Namespace(coord_cmd="answer", ref=str(q.n), option="a", text=None, token=band.token,
                              data_root=None, json=False)
    with pytest.raises(CoordError, match="escalate"):
        ccli._coord_first_reader(args, io.StringIO(), band.store)
    assert band.reads() == [] and band.c.rows("SELECT COUNT(*) FROM replies WHERE kind = 'know'") == [(0,)]


def test_a_know_is_not_delivered_when_the_operator_answers_between_the_read_and_the_claim(band, monkeypatch):
    """L3 r1 codex MED: ``claim_knows`` read the session's open questions before its delivery transaction. The operator
    answers in that gap: the KNOW is not delivered and not marked delivered."""
    from levain.autonomic.durable_sqlite import write_transaction as real

    q = band.ask()
    band.answer(q)

    def write(connect):
        band.qs.answer(q.id, option="b", channel="cli")
        return real(connect)

    monkeypatch.setattr(fr, "write_transaction", write)
    assert fr.claim_knows(band.store.directory, "claude_code", SEAT, band_mod.know_line) == []
    assert band.c.rows("SELECT COUNT(*) FROM know_deliveries") == [(0,)]


def test_a_know_that_fails_to_format_is_not_marked_delivered(band, monkeypatch):
    """L3 r1 LOW: the KNOW was marked delivered before it was formatted, so a formatting error lost it unseen."""
    q = band.ask()
    band.answer(q)

    def broken(question, body):
        raise ValueError("cannot format")

    monkeypatch.setattr(band_mod, "know_line", broken)
    assert question_context(band.store.directory, "claude_code", SEAT) is None
    assert band.c.rows("SELECT COUNT(*) FROM know_deliveries") == [(0,)]
    monkeypatch.undo()
    assert "the coordinator, not the operator, answered question #1" in question_context(
        band.store.directory, "claude_code", SEAT)


def test_a_symlinked_repo_in_the_envelope_still_contains_its_own_real_path(tmp_path):
    """L3 r1 LOW: only the question's cwd was resolved; an envelope repo spelled through a link never matched."""
    real = tmp_path / "real-repo"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link-repo"
    link.symlink_to(real)
    assert fr._inside(str(real / "sub"), [{"repo": str(link)}])
    assert fr._inside(str(link), [{"repo": str(link)}])
    assert not fr._inside(str(tmp_path), [{"repo": str(link)}])


# --- C5 fix round 2 (L3 r2): no replay under the dispatch lock; escalate re-reads the question -----------------------
def _held(monkeypatch, store, act=None):
    """Mark while ``store._write`` holds the dispatch lock; run ``act`` (once) just before it opens."""
    import contextlib

    orig, held, fired = store._write, [False], [False]

    @contextlib.contextmanager
    def write(*a, **k):
        if act is not None and not fired[0]:
            fired[0] = True
            act()
        with orig(*a, **k) as got:
            held[0] = True
            try:
                yield got
            finally:
                held[0] = False

    monkeypatch.setattr(store, "_write", write)
    return held


def _counting(monkeypatch, held):
    """Count signature verifications in all and while the dispatch lock is held."""
    from levain import authority as auth

    real, n = auth.verify_link_sig, {"all": 0, "locked": 0}

    def verify(*a, **k):
        n["all"] += 1
        n["locked"] += held[0]
        return real(*a, **k)

    monkeypatch.setattr(auth, "verify_link_sig", verify)
    return n


def _cold_log(tmp_path, keys):  # noqa: F811
    from levain.authority import ReplayCache
    from levain.consent.queue import authority_source

    (tmp_path / "auth3").mkdir()
    lg = Log(tmp_path / "auth3", keys)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("phone", "display-phone")
    return lg, authority_source(lg.path, anchor=None, cache=ReplayCache())     # this process has verified nothing


def _cli_answer(band, q):
    from levain.coordinator import cli as ccli

    args = argparse.Namespace(coord_cmd="answer", ref=str(q.n), option="a", text=None, token=band.token,
                              data_root=None, json=False)
    return ccli._coord_first_reader(args, io.StringIO(), band.store)


def test_answer_verifies_no_signature_while_it_holds_the_dispatch_lock(band, keys, tmp_path, monkeypatch):  # noqa: F811
    """L3 r2 consensus MED: ``answer`` replayed the whole authority log (one ssh-keygen per uncached link) inside the
    ``dispatch.db`` write lock, so every other writer waited. The replay runs before the lock; inside it only the log's
    head is compared."""
    from levain.coordinator import cli as ccli

    _lg, cold = _cold_log(tmp_path, keys)
    snaps = []
    monkeypatch.setattr(ccli, "_snapshot", lambda args, store: snaps.append(1) or cold())
    monkeypatch.setattr(ccli, "_head_reader", lambda args, store: cold.head, raising=False)
    q = band.ask()
    held = _held(monkeypatch, band.store)
    n = _counting(monkeypatch, held)
    assert _cli_answer(band, q) == 0
    assert band.reads() == [(q.id, "know")]
    assert n["all"] > 0 and n["locked"] == 0, n
    assert len(snaps) == 1


def test_a_log_that_moves_between_the_snapshot_and_the_lock_is_replayed_outside_it(band, keys, tmp_path, monkeypatch):  # noqa: F811
    """The head moved (a link appended) between the snapshot and the lock: the transaction is abandoned, the log is
    replayed again OUTSIDE the lock, and the answer is filed on the new head; no signature is verified under it."""
    from levain.coordinator import cli as ccli

    lg, cold = _cold_log(tmp_path, keys)
    snaps = []
    monkeypatch.setattr(ccli, "_snapshot", lambda args, store: snaps.append(1) or cold())
    monkeypatch.setattr(ccli, "_head_reader", lambda args, store: cold.head, raising=False)
    q = band.ask()
    held = _held(monkeypatch, band.store, act=lambda: lg.enrol("presence", "presence"))
    n = _counting(monkeypatch, held)
    assert _cli_answer(band, q) == 0
    assert band.reads() == [(q.id, "know")]
    assert len(snaps) == 2 and n["locked"] == 0, (snaps, n)


def test_a_log_that_never_stops_moving_is_refused_without_a_know(band, keys, tmp_path, monkeypatch):  # noqa: F811
    from levain.coordinator import cli as ccli

    _lg, cold = _cold_log(tmp_path, keys)
    monkeypatch.setattr(ccli, "_snapshot", lambda args, store: cold())
    monkeypatch.setattr(ccli, "_head_reader", lambda args, store: (lambda: (10**6, "elsewhere")), raising=False)
    q = band.ask()
    with pytest.raises(CoordError, match="kept moving"):
        _cli_answer(band, q)
    assert band.reads() == [] and band.c.rows("SELECT COUNT(*) FROM replies WHERE kind = 'know'") == [(0,)]


def test_escalating_a_question_the_operator_answered_meanwhile_is_refused(band, monkeypatch):
    """L3 r2 complement LOW: ``escalate`` read the question before its transaction and never checked it was still
    open. The operator answers in that gap: nothing is filed."""
    q = band.ask()
    _seam(monkeypatch, band.store, lambda: band.qs.answer(q.id, option="b", channel="cli"))
    with pytest.raises(CoordError, match="answered"):
        fr.escalate(band.store, q.id, "outside", token=band.token)
    assert band.reads() == [] and band.c.rows("SELECT COUNT(*) FROM replies WHERE kind = 'answer'") == [(0,)]
