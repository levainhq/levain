"""A question put to the operator in prose is a band row: the shape detector (:mod:`levain.operator_ask`) and the Stop
hook's refuse-and-file check (:func:`levain.band_view._ask_reason`)."""
from __future__ import annotations

import sqlite3

import pytest

from levain import band_view, operator_ask
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]

PRICE = ("Price for the ChatGPT Business profile setup on /agent-ready/: is $150 flat OK, photos and details ready?\n"
         "(a) $150 flat, materials ready\n(b) $130/hr, open scope\nSay a or b and I'll send the quote.")
DESK_LIST = ("Three passes are possible on the test list:\n1. Re-ask what you passed this morning\n"
             "2. Re-ask what you use daily\n3. Skip the list and ship\nWhich of these do you want me to run?")


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


def other_row(store):
    """An open row some other session filed, so the seat's band has an item to reprint."""
    return store.file(agent="claude_code", agent_session_id="sess-other", what="Which retry policy?", options=OPTS,
                      rec="exp", blocks="the feed PR", cwd="/tmp/proj")


def no_desk_launch(monkeypatch):
    from levain.desk import lease
    from levain.desk.store import CoordStore

    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(lease, "by_harness_session", lambda conn, sid: None)


def deliver(sid, e):
    """What the UserPromptSubmit hook does for one prompt: render the band, write the turn record."""
    text, commit = band_view.hook_prompt({"session_id": sid, "prompt": "hi"}, e)
    commit()
    return text


def stop(sid, message, e, **more):
    return band_view.hook_stop({"session_id": sid, "last_assistant_message": message, **more}, e)


def test_a_priced_pick_in_prose_is_an_ask_to_the_operator():
    v = operator_ask.detect(PRICE)
    assert v is not None and v.addressee == "operator"
    assert {"lettered_options", "say_and_ill", "confirm_pick"} <= set(v.shapes)
    assert "$150" in v.what_hint


def test_a_numbered_desk_style_list_with_a_decision_question_is_an_ask():
    v = operator_ask.detect(DESK_LIST)
    assert v is not None and "numbered_options" in v.shapes and v.addressee == "operator"
    assert operator_ask.detect("Done:\n1. fixed the retry\n2. added the test\n3. pushed the branch") is None


def test_a_reply_that_only_reprints_the_band_block_is_no_ask():
    block = ["band@3 · NEEDS YOU · 1 open", "1. Which retry policy? (a) fix (b) exp  rec: exp  blocks: the feed PR"]
    text = "Done, the test passes.\n\n" + "\n".join(block)
    assert operator_ask.detect(text, drop_lines=block) is None
    assert operator_ask.detect(text) is not None                         # the band's own item reads as an ask
    assert operator_ask.detect(text + "\nWant me to push it?", drop_lines=block) is not None


def test_a_message_to_another_agent_is_not_refused(qdir, store, monkeypatch):
    msg = "ROUTE to levain 54: next: ship the retry change?"
    v = operator_ask.detect(msg)
    assert v is not None and v.addressee == "agent"
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    assert stop("s1", f"{msg}\n\n{band}", e) is None


def test_the_stop_hook_refuses_a_prose_question_with_no_row_once(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    reply = f"{PRICE}\n\n{band}"
    reason = stop("s1", reply, e)
    assert reason and "levain ask" in reason and "--option" in reason and "--rec" in reason
    assert stop("s1", reply, e, stop_hook_active=True) is None
    assert stop("s1", f"all good\n\n{band}", e) is None


def test_the_same_turn_with_a_row_filed_after_it_began_is_allowed(qdir, store, monkeypatch):
    no_desk_launch(monkeypatch)
    e = env(qdir)
    other_row(store)
    band = deliver("s1", e)
    store.file(agent="claude_code", agent_session_id="s1", what="Is $150 flat OK?", options=OPTS, rec="fix",
               blocks="the quote", cwd="/tmp/proj")
    assert stop("s1", f"{PRICE}\n\n{band}", e) is None
    assert store.filed_since("claude_code", "s1", "9999-01-01T00:00:00.000000Z") == 0
    assert store.filed_since("claude_code", "nobody", "2000-01-01T00:00:00.000000Z") == 0
