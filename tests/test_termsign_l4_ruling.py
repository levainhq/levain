"""TERMSIGN L4, the seat side: a linked question's pick is signed WITH its ruling in the same touch.

A plain ed25519 key stands in for the Touch ID key (real ``ssh-keygen -Y sign``; no test reaches the Secure Enclave
or ``sc_auth``): it is enrolled as ``laptop-presence`` in a real authority log, and a real consent queue holds the
undecided request the question links to. ``tests/test_consent_terminal_ruling.py`` is the queue's own adversarial
set; these tests are what the band hands it.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain import band_view
from levain.consent import ConsentQueue
from levain.consent import terminal_sign as ts
from levain.consent.questions import Link
from tests._h4 import h4_home
from tests.test_consent_queue import Key, Log
from tests.test_consent_queue_v4 import undecided
from tests.test_termsign_l2_seat import (  # noqa: F401 - fixtures and helpers of the L2 seat tests
    SESSION, VIEWER, ask, env, human_terminal, keydir, qdir, real_factory, recorded, seat, sidecar, store,
)

OPTS = [("push", "push it"), ("hold", "hold off")]


@pytest.fixture(autouse=True)
def no_live_check(monkeypatch):
    monkeypatch.setattr(ts, "ctk_status", lambda public_key: (None, "test"))


@pytest.fixture
def world(tmp_path, keydir, qdir, store, env):
    """(Band number 1 is the undecided request itself; the linked question is 2, the other question 3.)
    The terminal key enrolled laptop-presence, a queue with one undecided request, and a question linking to it."""
    key = Key.__new__(Key)                                   # the key the L2 fixture already made in ``keydir``
    key.path = keydir / ts.KEY_LABEL
    key.public_key = " ".join((keydir / (ts.KEY_LABEL + ".pub")).read_text().split()[:2])
    key.fp = ts.find_key(keydir) and __import__("levain.cockpit.keys", fromlist=["x"]).fingerprint_of(key.public_key)
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": key})
    log.enrol("laptop", "laptop-presence")
    queue = ConsentQueue(qdir, authority=log.source, home=h4_home())
    request, gap = undecided(queue)
    q = ask(store, "Push the branch?", options=OPTS, rec="push",
            link=Link(option="push", kind="push", request_id=request.id, queue_id=queue.queue_id))
    other = ask(store, "Which colour?")
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    return {"queue": queue, "request": request, "q": q, "other": other, "key": key, "gap": gap}


def seated(store, env, text, keydir, world, **kw):
    return seat(store, env, text, keydir, queue_for=lambda q: world["queue"], **kw)


def state(world):
    return world["queue"].get(world["request"].id)[1]


def test_the_linked_pick_is_ruled_and_recorded_in_one_touch(store, env, keydir, world):
    calls = []

    def factory(path, timeout=None):
        calls.append(path)
        return real_factory(path, timeout)

    echo = seated(store, env, "2a, 3a", keydir, world, factory=factory)
    assert len(calls) == 1                                         # one message, one signature, ruling inside
    assert state(world) == "allowed"
    assert ("ruling ALLOW signed in the same touch and applied: 2a" in echo)
    assert [r[1] for r in recorded(store)] == ["push", world["other"].options[0][0]]
    assert "signed (Touch ID, laptop): 3a, 2a" in echo
    assert sidecar(store)[0][5] == "signed"
    rec = band_view.answer_sign_of(store, world["q"].id)
    parsed = ts.parse_verified(rec["message"].encode())
    mine = [e for e in parsed.entries if e.item == world["q"].id][0]
    assert mine.kind == "authority" and json.loads(mine.ruling)["request_id"] == world["request"].id
    taken = world["queue"].take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"},
                                            cwd="/work/proj")
    assert taken is not None and taken.signer_fp == world["key"].fp


def test_the_unlinked_pick_of_a_linked_question_opens_its_sheet_and_rules_nothing(store, env, keydir, world):
    echo = seated(store, env, "2b", keydir, world)
    assert state(world) == "undecided" and recorded(store) == []
    assert "only 'push it' carries the signed ruling" in echo


def test_without_an_enrolled_key_the_d8_sheet_path_stands(store, env, tmp_path, world):
    empty = tmp_path / "empty-keys"
    empty.mkdir(mode=0o700)
    echo = seat(store, env, "2a", empty, queue_for=lambda q: world["queue"])
    assert state(world) == "undecided" and recorded(store) == []
    assert "open question #1 on your phone and tap 'push it'" in echo


def test_a_cancelled_touch_applies_no_ruling_and_keeps_the_sheet(store, env, keydir, world):
    from levain.cockpit.keys import KeyError_

    def factory(path, timeout=None):
        def signer(message):
            raise KeyError_("Touch ID was cancelled or did not complete")
        return "SHA256:" + "A" * 43, signer

    echo = seated(store, env, "2a", keydir, world, factory=factory)
    assert state(world) == "undecided" and recorded(store) == []
    assert "open question #1 on your phone" in echo


def test_a_queue_that_refuses_leaves_the_question_open_and_says_why(store, env, keydir, world, qdir, tmp_path):
    class Refusing:
        queue_id = world["queue"].queue_id

        def ruling_payload(self, *a):
            return world["queue"].ruling_payload(*a)

        def answer_terminal_ruling(self, *a, **kw):
            from levain.consent import AnswerRefused
            raise AnswerRefused("the signer's key is revoked")

    echo = seat(store, env, "2a", keydir, queue_for=lambda q: Refusing())
    assert state(world) == "undecided" and recorded(store) == []
    assert "no signed ruling was applied: the signer's key is revoked" in echo


def test_a_request_decided_since_the_band_is_not_ruled_again(store, env, keydir, world):
    seated(store, env, "2a", keydir, world)
    assert state(world) == "allowed"
    echo = seated(store, env, "2a", keydir, world)               # the question is answered now: nothing new
    assert state(world) == "allowed"
    assert [r for r in recorded(store)].count(recorded(store)[0]) == 1 and "already answered" in echo
    assert len(world["queue"].receipts(world["request"].id)) == 2


def test_a_linked_question_to_another_queue_keeps_its_sheet(store, env, keydir, world):
    class Elsewhere:
        queue_id = "cq-elsewhere"

        def ruling_payload(self, *a):
            raise AssertionError("never asked")

    echo = seat(store, env, "2a", keydir, queue_for=lambda q: Elsewhere())
    assert state(world) == "undecided" and "open question #1 on your phone" in echo


def test_an_information_only_line_is_unchanged(store, env, keydir, world):
    echo = seated(store, env, "3a", keydir, world)
    assert state(world) == "undecided"
    assert "signed (Touch ID, laptop): 3a" in echo and "ruling" not in echo
    with sqlite3.connect(world["queue"].path) as c:
        assert c.execute("SELECT COUNT(*) FROM terminal_rulings").fetchone() == (0,)
