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
            return False
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
        return False if len(calls) == 1 else real_judge(row, path, *a)

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
        return False if len(calls) == 1 else real_judge(row, path, *a)

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
    assert [r.answered for r in operator.rows(L / "queue", now=LATER, signers=None)] == [False]   # none verifies


# --- prel3fix r1 F4: the cap test runs first; held answers are verified lazily, and refusals log once per kind ---------

def spy_checks(broker, monkeypatch):
    real, seen = broker._answer_settles, []

    def spy(request_id, *a, **kw):
        seen.append(request_id)
        return real(request_id, *a, **kw)

    monkeypatch.setattr(broker, "_answer_settles", spy)
    return seen


def test_a_session_under_its_cap_verifies_no_held_answer(L, broker, keys, answers, monkeypatch):
    rids = [filed(broker, fields=hook_fields(f"p{i}")) for i in range(PENDING_PER_SESSION - 1)]
    for rid in rids:
        squat(L, keys, answers, rid, "junk")
    seen = spy_checks(broker, monkeypatch)
    assert req(broker, fields=hook_fields("over"))["ok"] is True
    assert seen == []


def test_verification_stops_as_soon_as_one_place_is_free(L, broker, keys, answers, monkeypatch):
    rids = [filed(broker, fields=hook_fields(f"p{i}")) for i in range(PENDING_PER_SESSION)]
    write_answer(L, rids[0], keys["operator"])
    for rid in rids[1:]:
        squat(L, keys, answers, rid, "junk")
    seen = spy_checks(broker, monkeypatch)
    assert req(broker, fields=hook_fields("over"))["ok"] is True
    assert seen == [rids[0]]


def test_a_session_at_its_cap_with_junk_is_busy_and_logs_one_line_per_refusal_kind(L, broker, keys, answers,
                                                                                  monkeypatch, caplog):
    rids = [filed(broker, fields=hook_fields(f"p{i}")) for i in range(PENDING_PER_SESSION)]
    for rid in rids:
        squat(L, keys, answers, rid, "junk")
    seen = spy_checks(broker, monkeypatch)
    caplog.clear()
    with caplog.at_level("WARNING", logger="levain.qbroker"):
        got = req(broker, fields=hook_fields("over"))
    assert got["ok"] is False and got["refused"] == "queue-busy", got
    assert sorted(seen) == sorted(rids)                  # every held row had to be checked to know the cap is full
    lines = [r for r in caplog.records if r.name == "levain.qbroker"]
    assert len(lines) == 1 and all(rid in lines[0].getMessage() for rid in rids), [r.getMessage() for r in lines]


# --- prel3fix L3 r1 B1: whatever the rename moved is judged from its own fd before the new answer goes in -----------

def test_a_signed_answer_hidden_from_the_check_by_a_swap_and_restored_before_the_move_is_linked_back(L, broker, keys,
                                                                                                    answers,
                                                                                                    monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")      # another writer's signed answer
    name = answers / f"{rid}.json"
    first = name.read_bytes()
    hidden = answers / "hidden.json"
    real, calls = operator._signed_answer, []

    def swapped_once(row, path, *a):
        calls.append(path)
        if len(calls) > 1:
            return real(row, path, *a)
        os.rename(name, hidden)                  # codex-4: a symlink at the name while the check opens it ...
        os.symlink(hidden, name)
        try:
            return real(row, path, *a)
        finally:
            os.unlink(name)
            os.rename(hidden, name)              # ... and the signed answer back before the rename moves it

    monkeypatch.setattr(operator, "_signed_answer", swapped_once)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    with pytest.raises(operator.OperatorError, match="already has an answer row"):
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert name.read_bytes() == first
    assert sorted(p.name for p in answers.iterdir()) == [name.name]
    assert ask(broker, rid)["decision"] == "deny"


def test_a_signed_answer_on_the_inode_the_check_judged_is_linked_back(L, broker, keys, answers, monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")
    name = answers / f"{rid}.json"
    signed_bytes = name.read_bytes()
    name.write_bytes(b"not an answer")           # junk, on the inode the signed answer will hold
    real, calls = operator._signed_answer, []

    def rewritten_once(row, path, *a):
        calls.append(path)
        got = real(row, path, *a)
        if len(calls) == 1:
            # complement-4: the junk judged is gone and a signed answer holds its (dev, ino), as a filesystem reusing
            # the freed inode would give it
            name.write_bytes(signed_bytes)
        return got

    monkeypatch.setattr(operator, "_signed_answer", rewritten_once)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    with pytest.raises(operator.OperatorError, match="already has an answer row"):
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert name.read_bytes() == signed_bytes
    assert sorted(p.name for p in answers.iterdir()) == [name.name]
    assert ask(broker, rid)["decision"] == "deny"


# --- prel3fix L3 r1 B2: a signed answer over a head the receipts do not hold is no answer here either ---------------

def test_a_signed_answer_over_a_head_not_in_the_receipts_neither_lists_as_answered_nor_blocks_a_replacement(
        L, broker, keys, answers):
    rid = filed(broker)
    # codex-5: authentic and signed, but over a head this log does not hold (a receipts log restored to another
    # branch): the broker's Receipts.decided refuses it
    write_answer(L, rid, keys["operator"], decision="deny", receipts_head={"size": 99, "root": "ab" * 32})
    listed = {r.request_id: r for r in operator.rows(L / "queue", now=LATER, signers=rbase.snapshot(L))}
    assert (listed[rid].state, listed[rid].answered) == ("pending", False)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert ask(broker, rid)["decision"] == "allow"
    assert len([p for p in answers.iterdir() if p.name.startswith(f".quarantine-{rid}-")]) == 1


# --- prel3fix L3 r1 B3: any other OSError at the link-back names the quarantine file ---------------------------------

def test_any_other_os_error_linking_a_signed_answer_back_names_its_quarantine_file(L, broker, keys, answers,
                                                                                  monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")      # another writer's signed answer
    first = (answers / f"{rid}.json").read_bytes()
    real_judge, real_link, calls = operator._signed_answer, os.link, []

    def stale_once(row, path, *a):
        calls.append(path)
        return False if len(calls) == 1 else real_judge(row, path, *a)

    def denied(src, dst, **kw):
        if ".quarantine-" in str(src):
            raise PermissionError(1, "Operation not permitted")  # complement-3: EPERM, not the two handled
        return real_link(src, dst, **kw)

    monkeypatch.setattr(operator, "_signed_answer", stale_once)
    monkeypatch.setattr(operator.os, "link", denied)
    payload, sig = rbase.signed(L, rid, keys["operator"])
    with pytest.raises(operator.OperatorError) as got:
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    parked = [p for p in answers.iterdir() if p.name.startswith(f".quarantine-{rid}-")]
    assert len(parked) == 1 and parked[0].read_bytes() == first
    assert str(parked[0]) in str(got.value)


# --- prel3fix L3 r1 B4: rows() is never called without saying which signers verify -----------------------------------

def test_rows_requires_a_signers_argument(L, broker):
    filed(broker)
    with pytest.raises(TypeError, match="signers"):
        operator.rows(L / "queue", now=LATER)
    assert [r.answered for r in operator.rows(L / "queue", now=LATER, signers=None)] == [False]


# --- prel3fix L3 r2 (pf1r3): a verified answer linked back is "in place"; an fsync EIO never loses the aside path ------

def test_a_swapped_signed_answer_linked_back_on_the_last_try_is_reported_not_a_squat_refusal(L, broker, keys, answers,
                                                                                            monkeypatch):
    rid = filed(broker)
    write_answer(L, rid, keys["operator"], decision="deny")
    first = (answers / f"{rid}.json").read_bytes()
    real, calls = operator._signed_answer, []

    def stale_once(row, path, *a):
        calls.append(path)
        return False if len(calls) == 1 else real(row, path, *a)

    monkeypatch.setattr(operator, "_signed_answer", stale_once)
    monkeypatch.setattr(operator, "_SQUAT_TRIES", 1)             # the swap is met on the final (only) attempt
    payload, sig = rbase.signed(L, rid, keys["operator"])
    with pytest.raises(operator.OperatorError, match="already has an answer row"):
        operator.write_answer(L / "queue", rid, payload, sig, rbase.snapshot(L), now=LATER)
    assert (answers / f"{rid}.json").read_bytes() == first
    assert sorted(p.name for p in answers.iterdir()) == [f"{rid}.json"]

