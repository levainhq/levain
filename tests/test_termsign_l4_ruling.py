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


# --- L4 L3 r1 -----------------------------------------------------------------------------------------

def test_two_linked_picks_on_one_queue_are_both_ruled_in_one_touch(store, env, keydir, world):
    """T2 (complement) NOT reproduced: the finding read the first apply's receipt as staling the second ruling's
    ``receipts_head``. The head check is of the head OF ITS SIZE (``receipt_log.head_refusal``): a receipt appended
    after it leaves that prefix, and so the second ruling, as signed. Pinned, so a change to the head rule shows here."""
    second, _ = undecided(world["queue"], "touch /tmp/y")
    q2 = ask(store, "Push the other branch?", options=OPTS, rec="push",
             link=Link(option="push", kind="push", request_id=second.id, queue_id=world["queue"].queue_id))
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    n2 = next(ln for ln in out.splitlines() if "Push the other branch?" in ln).split()[0].strip("#.:")
    calls = []

    def factory(path, timeout=None):
        calls.append(path)
        return real_factory(path, timeout)

    echo = seated(store, env, f"2a, {n2}a", keydir, world, factory=factory)
    assert len(calls) == 1
    assert state(world) == "allowed" and world["queue"].get(second.id)[1] == "allowed"
    assert f"ruling ALLOW signed in the same touch and applied: 2a, {n2}a" in echo and "receipts" not in echo
    assert sorted(recorded(store)) == sorted([(world["q"].n, "push"), (q2.n, "push")])


def test_a_receipt_written_during_the_touch_leaves_the_signed_head_standing(store, env, keydir, world):
    """The same rule from the other side: a receipt of an unrelated request landing while Touch ID waits does not
    change the receipts the ruling signed over, so the ruling applies."""
    from tests.test_consent_queue_v4 import ask as file_request
    other = file_request(world["queue"], "touch /tmp/z")              # filed BEFORE the line, still pending
    landed = []

    def during_the_wait():
        landed.append(world["queue"].withdraw(other.id))             # an unrelated receipt lands while Touch ID waits
    echo = seated(store, env, "2a", keydir, world, on_sign=during_the_wait)
    assert landed == [True]
    assert state(world) == "allowed" and "ruling ALLOW signed in the same touch and applied: 2a" in echo


def test_a_ruling_applied_whose_answer_the_store_refuses_is_still_said(store, env, keydir, world):
    """T3 (codex HIGH + deepseek + complement, scoped by the reachability line): the queue applies the ALLOW inside
    the sign step; when the question-store write then refuses (answered elsewhere in the same moment), the echo
    said nothing of the ruling. The ALLOW is what the touch signed: it stands, and the echo says so."""
    from levain.consent import AnswerRefused

    def answered_elsewhere():
        store.answer(world["q"].id, option="hold", channel="phone")
    echo = seated(store, env, "2a", keydir, world, on_sign=answered_elsewhere)
    assert state(world) == "allowed"
    assert "ruling ALLOW signed in the same touch and applied, the answer was not recorded: 2a" in echo
    assert "2: " in echo                                              # the item's own refusal is named
    assert [r[1] for r in recorded(store)] == ["hold"]
    with pytest.raises(AnswerRefused, match="undecided"):           # a sheet can never ask for this ruling again
        world["queue"].ruling_payload(world["request"].id, "allow", world["key"].fp)


def test_no_view_is_unsigned_before_any_message_and_applies_no_ruling(store, env, keydir, world, monkeypatch):
    """T5 (deepseek LOW): a call with no view never builds a message naming view "None"."""
    built = []
    monkeypatch.setattr(ts, "build_message", lambda **kw: built.append(kw))
    sign_with = band_view._terminal_signer(store, VIEWER, "2a", __import__("types").SimpleNamespace(rest=""), SESSION, env,
                                           real_factory, keydir, view_id=None, queue_for=lambda q: world["queue"])
    got = sign_with([], rulings=[(type("Sh", (), {"pick": "a", "label": "2a", "n": 2})(), world["q"])])
    assert built == [] and got.state == "unsigned" and got.reason == "signer-error"
    assert got.ruled == {} and got.ruling_refusals == {world["q"].id: "the answer was not signed"}
    assert state(world) == "undecided"


@pytest.fixture
def cockpit_world(tmp_path, keydir, qdir, store, env):
    """The production path: no ``queue_for``. A cockpit data root whose anchored authority log enrols the terminal key
    laptop-presence, named by the queue's ``claude_code.json`` as the hook's cockpit, and the queue in the very
    directory ``queue_dir_for(env)`` resolves."""
    import subprocess
    from datetime import datetime, timezone

    import levain.authority as auth
    from levain.authority import ReplayCache, anchor_path
    from levain.cockpit.firejournal import CockpitDB
    from levain.cockpit.keys import fingerprint_of
    from levain.consent.queue import authority_source

    path = keydir / ts.KEY_LABEL
    pub = " ".join((keydir / (ts.KEY_LABEL + ".pub")).read_text().split()[:2])
    fp = fingerprint_of(pub)
    root = tmp_path / "cockpit"
    root.mkdir()
    CockpitDB(root).set_install_class("reported")
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        auth.sign_and_append(
            conn, {"op": "enrol", "fingerprint": fp, "public_key": pub, "class": "laptop-presence",
                   "custody": "passphrase"}, signer_fp=fp,
            sign=lambda m: subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(path), "-n", auth.NAMESPACE],
                                          input=m, capture_output=True, check=True).stdout.decode(),
            clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=anchor_path(root))
    finally:
        conn.close()
    (qdir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(root), "away_mode": False}))
    queue = ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)),
                         home=h4_home())
    request, _ = undecided(queue)
    q = ask(store, "Push the branch?", options=OPTS, rec="push",
            link=Link(option="push", kind="push", request_id=request.id, queue_id=queue.queue_id))
    out, commit = band_view.hook_prompt({"session_id": SESSION, "prompt": "hello"}, env)
    commit()
    return {"queue": queue, "request": request, "q": q, "root": root, "fp": fp}


def test_the_production_queue_for_resolves_the_hooks_queue_and_cockpit(store, env, keydir, qdir, cockpit_world):
    """T5 (deepseek MED): every band test injected ``queue_for``; the production resolution had no test."""
    from levain.consent.answering import answer_root
    from levain.consent.queue import queue_dir_for

    w = cockpit_world
    assert queue_dir_for(env) == qdir.resolve() == w["queue"].directory.resolve()
    assert answer_root(queue_dir_for(env), w["request"]) == w["root"]
    resolved = band_view._default_queue_for(env)(w["q"])
    assert resolved is not None and resolved.path.resolve() == w["queue"].path.resolve()
    echo = seat(store, env, "2a", keydir)                            # no queue_for: the production path
    assert w["queue"].get(w["request"].id)[1] == "allowed"
    assert "ruling ALLOW signed in the same touch and applied: 2a" in echo
    taken = w["queue"].take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd="/work/proj")
    assert taken is not None and taken.signer_fp == w["fp"]
