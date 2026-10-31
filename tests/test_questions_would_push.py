"""Q-3: a question whose option leads to a `levain push` (``levain ask --would-push``). The push is filed undecided and
linked; only a late ruling signed on it ever allows the push (Phill 26a: an answer never authorises). The browser
journey with a passkey and the real `levain push` are RUNs (project memory 1018-qband/Q3_RUN), not tests."""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

from levain.consent import band, push_gate
from levain.consent.queue import AnswerRefused, ConsentQueue
from levain.consent.questions import QuestionStore
from levain.consent.record import RULING_NAMESPACE, ruling_message
from tests.test_consent_queue import authority, keys  # noqa: F401 - the shared fixtures
from tests._h4 import h4_home

S = "sess-q3"


def _git(*argv, cwd=None):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True,
                          timeout=60).stdout.strip()


@pytest.fixture
def world(tmp_path, authority, monkeypatch):  # noqa: F811 - the shared fixture
    base = tmp_path.resolve()
    repo, remote = base / "repo", base / "remote.git"
    _git("init", "-q", "--bare", str(remote))
    _git("init", "-q", "-b", "main", str(repo))
    _git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "c1", cwd=repo)
    qdir = base / "home" / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    (base / "home").chmod(0o700)
    queue = ConsentQueue(qdir, authority=authority, home=h4_home())
    monkeypatch.setattr(push_gate, "_signable", lambda settings: True)
    monkeypatch.setattr(push_gate, "_queue", lambda d, settings: queue)
    monkeypatch.chdir(repo)
    url = f"file://{remote}"

    def ask(**kw):
        return band.file_question("claude_code", S, "Push main?", [("a", "push it"), ("b", "hold it")], "a",
                                  "the push of main", qdir, would_push=("a", (str(repo), url, "main")), **kw)

    call = push_gate.push_call(str(repo), url, "main")
    return {"qdir": qdir, "queue": queue, "store": QuestionStore(qdir), "ask": ask, "call": call}


def _rule(queue, request_id, key, decision="allow"):
    payload = queue.ruling_payload(request_id, decision, key.fp)
    return payload, key.sign(ruling_message(payload), RULING_NAMESPACE)


def _take(w):
    return w["queue"].take_ruled_allow("claude_code", S, push_gate.PUSH_TOOL, w["call"], cwd=w["call"]["repo"])




def test_the_answer_alone_allows_nothing_and_a_ruled_allow_is_taken_once(world, keys):  # noqa: F811
    q, _ = world["ask"]()
    world["store"].answer(q.id, option="a", channel="phone")             # 26a: words only
    assert world["queue"].get(q.link.request_id)[1] == "undecided" and _take(world) is None
    world["queue"].answer_late(q.link.request_id, *_rule(world["queue"], q.link.request_id, keys["phone"]))
    got = _take(world)
    assert got is not None and (got.decision, got.via, got.request_id) == ("allow", "ruling", q.link.request_id)
    assert _take(world) is None                                          # once


def test_a_replayed_or_tampered_ruling_is_refused_and_moves_nothing(world, keys):  # noqa: F811
    q, _ = world["ask"]()
    payload, sig = _rule(world["queue"], q.link.request_id, keys["phone"])
    with pytest.raises(AnswerRefused):
        world["queue"].answer_late(q.link.request_id, {**payload, "gap": payload["gap"] + 1}, sig)
    with pytest.raises(AnswerRefused):
        world["queue"].answer_late(q.link.request_id, *_rule(world["queue"], q.link.request_id, keys["stranger"]))
    assert world["queue"].get(q.link.request_id)[1] == "undecided"
    world["queue"].answer_late(q.link.request_id, payload, sig)
    with pytest.raises(AnswerRefused):                                   # the same ruling again
        world["queue"].answer_late(q.link.request_id, payload, sig)




def test_a_store_from_before_q3_reads_with_no_links_and_takes_one_on_its_next_write(world):
    plain, _ = band.file_question("claude_code", S, "Old?", [("a", "x"), ("b", "y")], "a", "z", world["qdir"])
    conn = sqlite3.connect(world["qdir"] / "questions.db", isolation_level=None)
    for t in ("question_links_frozen", "question_links_kept", "question_links_no_replace"):
        conn.execute(f"DROP TRIGGER {t}")
    conn.execute("DROP TABLE question_links")                            # as 0.11.7 left it
    conn.close()
    fresh = QuestionStore(world["qdir"])
    assert fresh.get(plain.n).link is None and [x.n for x in fresh.open_questions()] == [plain.n]
    q, text = world["ask"]()
    assert q is not None and q.link is not None, text


def test_a_question_that_cannot_be_written_after_its_push_was_filed_says_so(world, monkeypatch):
    def broken(self, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(QuestionStore, "file", broken)
    q, text = world["ask"]()
    assert q is None
    undecided = world["queue"].undecided_for("claude_code", S)
    assert len(undecided) == 1 and undecided[0].request.id in text and "NOT filed" in text
    bad, why = band.file_question("claude_code", S, "x", [("a", "x"), ("b", "y")], "a", "", world["qdir"],
                                  would_push=("a", (".", "file:///nowhere", "main")))
    assert bad is None and "nothing was filed" in why                    # a malformed question files no push
    assert len(world["queue"].undecided_for("claude_code", S)) == 1
