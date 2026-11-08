"""PREL3 2d-1: a file at an answer row's name settles nothing unless it verifies for that request.

``answers/`` is setgid group-writable to ``levain-queue``, and the agent in a slot is a member: it can create a file at
any request id's answer name (and read the request rows to learn the ids). Two attacks, run against the real store,
broker and operator library with the directory at the mode setup gives it:

(a) junk at its own rows' answer names would clear them from the pending cap, so it could file past it;
(b) junk at another session's answer name would make the operator's signed write fail, so that request expired.

Every party here runs as one uid (the test cannot hold a second one), so the agent's write is the same ``open`` the
agent would make; what is under test is what the broker and the operator do with the file it leaves.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from levain.qbroker import operator
from levain.qbroker.broker import PENDING_PER_SESSION
from levain.qbroker.store import QueueStore
import tests.test_qbroker as base
import tests.test_qbroker_receipts as rbase
from tests.test_qbroker import ask, filed, hook_fields, req, write_answer

L, broker, clock, keys, resolver = base.L, base.broker, base.clock, base.keys, base.resolver
LATER = rbase.LATER


@pytest.fixture
def answers(L):
    d = L / "queue" / "answers"
    os.chmod(d, 0o2770)          # base_store_rows' mode for L/queue/answers: setgid, group-writable, no sticky bit
    return d


def squat(L, keys, answers, rid, how):
    if how == "junk":
        (answers / f"{rid}.json").write_bytes(b"not an answer")
    elif how == "stranger":
        write_answer(L, rid, keys["stranger"])           # a well-formed row signed by a key not in the snapshot
    elif how == "unsigned":
        write_answer(L, rid, keys["operator"], sign=False)
    elif how == "dir":
        (answers / f"{rid}.json").mkdir()
    elif how == "unreadable":
        (answers / f"{rid}.json").write_bytes(b"x")
        os.chmod(answers / f"{rid}.json", 0)              # its creator's to make unreadable to the operator
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["junk", "stranger", "unsigned", "dir", "unreadable"])
def test_an_unverifiable_answer_file_keeps_its_row_in_the_pending_cap(L, broker, keys, answers, how):
    rids = [filed(broker, fields=hook_fields(f"p{i}")) for i in range(PENDING_PER_SESSION)]
    for rid in rids:
        squat(L, keys, answers, rid, how)
    got = req(broker, fields=hook_fields("over"))
    assert got["ok"] is False and got["refused"] == "queue-busy", got
    # a verified answer still frees its place
    os.rename(answers / f"{rids[0]}.json", answers / f".moved-{rids[0]}")
    write_answer(L, rids[0], keys["operator"], decision="deny")
    assert req(broker, fields=hook_fields("over"))["ok"] is True


def test_a_verified_answer_frees_a_place_without_a_poll_and_records_the_decision(L, broker, keys, answers):
    rids = [filed(broker, fields=hook_fields(f"p{i}")) for i in range(PENDING_PER_SESSION)]
    write_answer(L, rids[1], keys["operator"])
    assert req(broker, fields=hook_fields("over"))["ok"] is True
    assert ask(broker, rids[1])["decision"] == "allow"


@pytest.mark.parametrize("how", ["junk", "stranger", "unsigned", "unreadable"])
def test_the_operators_signed_answer_moves_an_unverifiable_file_aside(L, broker, keys, answers, how):
    victim = filed(broker, cap="cap-a2")                  # another session's request
    squat(L, keys, answers, victim, how)
    before = os.lstat(answers / f"{victim}.json").st_ino
    payload, sig = rbase.signed(L, victim, keys["operator"])
    path = operator.write_answer(L / "queue", victim, payload, sig, rbase.snapshot(L), now=LATER)
    assert path == answers / f"{victim}.json"
    assert ask(broker, victim, cap="cap-a2")["decision"] == "allow"
    aside = [p for p in answers.iterdir() if p.name != path.name]
    assert len(aside) == 1 and aside[0].name.startswith(f".quarantine-{victim}-") and aside[0].stat().st_ino == before
    # the broker never reads the quarantined name as an answer: it is not a request id
    with pytest.raises(ValueError):
        QueueStore(L / "queue").read_answer(aside[0].name[:-len(".json")])


def test_a_directory_squatting_the_name_is_moved_aside_too(L, broker, keys, answers):
    victim = filed(broker, cap="cap-a2")
    squat(L, keys, answers, victim, "dir")
    payload, sig = rbase.signed(L, victim, keys["operator"])
    operator.write_answer(L / "queue", victim, payload, sig, rbase.snapshot(L), now=LATER)
    assert ask(broker, victim, cap="cap-a2")["decision"] == "allow"


def test_a_verified_answer_already_there_is_never_replaced(L, broker, keys, answers):
    rid = filed(broker)
    payload, sig = rbase.signed(L, rid, keys["operator"], decision="deny")
    operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    first = (answers / f"{rid}.json").read_bytes()
    other, osig = rbase.signed(L, rid, keys["operator"], decision="allow")
    with pytest.raises(operator.OperatorError, match="already has an answer row"):
        operator.write_answer(L / "queue", rid, other, osig, rbase.snapshot(L), now=LATER)
    assert (answers / f"{rid}.json").read_bytes() == first
    assert sorted(p.name for p in answers.iterdir()) == [f"{rid}.json"]
    assert ask(broker, rid)["decision"] == "deny"


def test_a_symlink_at_the_name_is_moved_aside_never_followed(L, broker, keys, answers, tmp_path):
    victim = filed(broker, cap="cap-a2")
    target = tmp_path / "elsewhere.json"
    target.write_bytes(b"x")
    os.symlink(target, answers / f"{victim}.json")
    payload, sig = rbase.signed(L, victim, keys["operator"])
    operator.write_answer(L / "queue", victim, payload, sig, rbase.snapshot(L), now=LATER)
    assert target.read_bytes() == b"x"
    assert ask(broker, victim, cap="cap-a2")["decision"] == "allow"


def test_a_signed_answer_swapped_in_between_the_check_and_the_move_is_linked_back(L, broker, keys, answers,
                                                                                  monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")      # another writer's signed answer, already there
    first = (answers / f"{rid}.json").read_bytes()
    real, calls = operator._signed_answer, []

    def stale_once(row, path, *a):
        calls.append(path)
        if len(calls) == 1:                                      # the check saw some other file at the name
            return False, (0, 0)
        return real(row, path, *a)

    monkeypatch.setattr(operator, "_signed_answer", stale_once)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    with pytest.raises(operator.OperatorError, match="already has an answer row"):
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert (answers / f"{rid}.json").read_bytes() == first
    assert sorted(p.name for p in answers.iterdir()) == [f"{rid}.json"]
    assert ask(broker, rid)["decision"] == "deny"


def test_a_name_retaken_after_every_move_ends_in_a_refusal_not_a_loop(L, broker, keys, answers, monkeypatch):
    victim = filed(broker, cap="cap-a2")
    squat(L, keys, answers, victim, "junk")
    real = os.rename

    def retaken(src, dst):
        real(src, dst)
        if str(src).endswith(f"{victim}.json"):
            (answers / f"{victim}.json").write_bytes(b"again")

    monkeypatch.setattr(operator.os, "rename", retaken)
    payload, sig = rbase.signed(L, victim, keys["operator"])
    with pytest.raises(operator.OperatorError, match="taken again"):
        operator.write_answer(L / "queue", victim, payload, sig, rbase.snapshot(L), now=LATER)
    assert (answers / f"{victim}.json").read_bytes() == b"again"
    assert len([p for p in answers.iterdir() if p.name.startswith(".quarantine-")]) == operator._SQUAT_TRIES


# --- prel3fix r1 F1/F2: the quarantine file is the squatter's to delete, and a parked signed answer is never silent ---

def test_a_quarantine_file_deleted_after_the_move_leaves_the_name_free_for_the_signed_answer(L, broker, keys, answers,
                                                                                            monkeypatch):
    victim = filed(broker, cap="cap-a2")
    squat(L, keys, answers, victim, "junk")
    real = os.rename

    def then_deleted(src, dst):
        real(src, dst)
        if str(src).endswith(f"{victim}.json"):
            os.unlink(dst)                               # the member removes the file it caused to exist

    monkeypatch.setattr(operator.os, "rename", then_deleted)
    payload, sig = rbase.signed(L, victim, keys["operator"])
    path = operator.write_answer(L / "queue", victim, payload, sig, rbase.snapshot(L), now=LATER)
    assert path == answers / f"{victim}.json"
    assert sorted(p.name for p in answers.iterdir()) == [f"{victim}.json"]
    assert ask(broker, victim, cap="cap-a2")["decision"] == "allow"


def test_a_swapped_signed_answer_that_vanishes_before_it_is_linked_back_frees_the_name(L, broker, keys, answers,
                                                                                      monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")
    real_judge, real_link, calls = operator._signed_answer, os.link, []

    def stale_once(row, path, *a):
        calls.append(path)
        return (False, (0, 0)) if len(calls) == 1 else real_judge(row, path, *a)

    def vanished(src, dst, **kw):
        if ".quarantine-" in str(src):
            os.unlink(src)
        return real_link(src, dst, **kw)

    monkeypatch.setattr(operator, "_signed_answer", stale_once)
    monkeypatch.setattr(operator.os, "link", vanished)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    path = operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert sorted(p.name for p in answers.iterdir()) == [path.name]


def test_any_other_os_error_clearing_the_name_is_an_operator_error(L, broker, keys, answers, monkeypatch):
    victim = filed(broker, cap="cap-a2")
    squat(L, keys, answers, victim, "junk")

    def denied(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(operator.os, "rename", denied)
    payload, sig = rbase.signed(L, victim, keys["operator"])
    with pytest.raises(operator.OperatorError, match="could not be cleared"):
        operator.write_answer(L / "queue", victim, payload, sig, rbase.snapshot(L), now=LATER)


def test_a_swapped_signed_answer_that_cannot_be_linked_back_is_reported_with_its_quarantine_path(L, broker, keys,
                                                                                                answers, monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")      # another writer's signed answer
    first = (answers / f"{rid}.json").read_bytes()
    real_judge, real_rename, calls = operator._signed_answer, os.rename, []

    def stale_once(row, path, *a):
        calls.append(path)
        return (False, (0, 0)) if len(calls) == 1 else real_judge(row, path, *a)

    def retaken(src, dst):
        real_rename(src, dst)
        if str(src).endswith(f"{rid}.json"):
            (answers / f"{rid}.json").write_bytes(b"again")     # the name taken before the link-back

    monkeypatch.setattr(operator, "_signed_answer", stale_once)
    monkeypatch.setattr(operator.os, "rename", retaken)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    with pytest.raises(operator.OperatorError) as got:
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    parked = [p for p in answers.iterdir() if p.name.startswith(f".quarantine-{rid}-")]
    assert len(parked) == 1 and parked[0].read_bytes() == first
    assert str(parked[0]) in str(got.value)


# --- prel3fix r1 F3: a row lists as answered only for an answer that verifies ------------------------------------------

@pytest.mark.parametrize("how", ["junk", "stranger", "unsigned", "dir"])
def test_an_unverifiable_answer_file_never_lists_its_row_as_answered(L, broker, keys, answers, how):
    rid = filed(broker)
    squat(L, keys, answers, rid, how)
    listed = {r.request_id: r for r in operator.rows(L / "queue", now=LATER, signers=rbase.snapshot(L))}
    assert (listed[rid].state, listed[rid].answered) == ("pending", False)


def test_a_signed_answer_lists_its_row_as_answered_only_against_a_signers_snapshot(L, broker, keys, answers):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"])               # signed, not yet polled: the row still reads pending
    listed = {r.request_id: r for r in operator.rows(L / "queue", now=LATER, signers=rbase.snapshot(L))}
    assert (listed[rid].state, listed[rid].answered) == ("pending", True)
    assert [r.answered for r in operator.rows(L / "queue", now=LATER)] == [False]   # no snapshot: nothing verifies
