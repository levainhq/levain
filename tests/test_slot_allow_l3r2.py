"""Row 165, L3 round 2 (1112-22 q6, lane C): regressions for the two defects round 1's fixes introduced in slot_allow."""
from __future__ import annotations

import sqlite3

import pytest

from levain.consent import terminal_sign as ts
from levain.slot import slot_allow as SA
from tests.test_slot_allow import Rig, _carried_one, _launch, qdir  # noqa: F401 - qdir is a fixture


@pytest.fixture
def rig(tmp_path, qdir):  # noqa: F811
    return Rig(tmp_path, qdir)


# --- 1. a full rule store refuses the one new rule, never every stored one ----------------------------------------

def _full_store_with_one_answered_yes(rig, qdir, monkeypatch):
    allow = _carried_one(rig, qdir, "WebSearch")
    monkeypatch.setattr(SA, "FILE_CAP", allow.stat().st_size + 64)    # the stored file reads; one more rule will not fit
    q = rig.file("Bash(make *)")[SA.asked_text("Bash(make *)")]
    rig.yes(q)
    assert "Bash(make *)" in SA._pending(qdir)
    return allow


def test_a_full_store_refuses_the_new_rule_consumes_its_record_and_says_why(rig, qdir, monkeypatch):
    allow = _full_store_with_one_answered_yes(rig, qdir, monkeypatch)
    before = allow.read_bytes()
    refused: list = []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge, refused=refused) == []
    assert refused == [("Bash(make *)", SA.FULL_WHY)]
    assert "Bash(make *)" not in SA._pending(qdir)                       # consumed, as any refusal is
    assert SA._pending_doc(qdir)["refused"] == {"Bash(make *)": SA.FULL_WHY}
    assert allow.read_bytes() == before and SA.rules_of(allow) == ["WebSearch"]   # the full store still reads
    # nothing left to raise on: the next apply is a no-op
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == []


def test_a_launch_against_a_full_store_still_carries_the_stored_rules_and_tells_him(rig, qdir, monkeypatch):
    _full_store_with_one_answered_yes(rig, qdir, monkeypatch)
    for _ in range(2):                                                    # this launch and the next
        settings, said = _launch(rig, qdir)
        assert settings["permissions"]["allow"] == ["WebSearch"], said
        assert not any("not read" in m for m in said), said


def test_the_first_launch_after_the_yes_names_the_full_store(rig, qdir, monkeypatch):
    _full_store_with_one_answered_yes(rig, qdir, monkeypatch)
    _settings, said = _launch(rig, qdir)
    assert any("Bash(make *)" in m and "the rule store is full" in m for m in said), said


def test_a_full_store_does_not_stop_the_rows_after_it(rig, qdir, monkeypatch):
    allow = _full_store_with_one_answered_yes(rig, qdir, monkeypatch)
    q = rig.file("WebFetch(domain:example.com)")[SA.asked_text("WebFetch(domain:example.com)")]
    rig.qs.answer(q.id, option="no", channel="cli")
    SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge)
    assert SA._pending(qdir) == {}                                        # the no after the full one was consumed too


def test_other_save_refusals_are_not_the_full_store(rig, qdir, monkeypatch):
    allow = _carried_one(rig, qdir, "WebSearch")
    q = rig.file("Bash(make *)")[SA.asked_text("Bash(make *)")]
    rig.yes(q)
    monkeypatch.setattr(SA, "load", lambda p: (_ for _ in ()).throw(SA.AllowRefused(f"{p}: rules is malformed")))
    with pytest.raises(SA.AllowRefused, match="malformed"):
        SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge)
    monkeypatch.undo()
    assert "Bash(make *)" in SA._pending(qdir)                            # a damaged store keeps the yes for later


# --- 2. a store or I/O error inside the verifier keeps the signed yes ---------------------------------------------

@pytest.mark.parametrize("exc", [PermissionError(13, "Permission denied"), OSError(5, "Input/output error"),
                                 sqlite3.OperationalError("database is locked")])
def test_an_io_or_store_error_inside_the_verifier_keeps_the_yes(rig, qdir, monkeypatch, exc):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")[SA.asked_text("WebSearch")]
    rig.yes(q)
    real = ts.verify

    def down(*a, **k):
        raise exc
    monkeypatch.setattr(ts, "verify", down)
    refused: list = []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge, refused=refused) == []
    assert refused == [] and "WebSearch" in SA._pending(qdir)
    monkeypatch.setattr(ts, "verify", real)
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge) == ["WebSearch"]


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "No such file"), ValueError("bad"), TypeError("bad")])
def test_a_verdict_shaped_error_inside_the_verifier_is_still_a_refusal(rig, qdir, monkeypatch, exc):
    allow = qdir / SA.FILE_NAME
    q = rig.file("WebSearch")[SA.asked_text("WebSearch")]
    rig.yes(q)

    def broken(*a, **k):
        raise exc
    monkeypatch.setattr(ts, "verify", broken)
    refused: list = []
    assert SA.apply_answered(qdir, allow, store=rig.qs, judge=rig.judge, refused=refused) == []
    assert [r for r, _ in refused] == ["WebSearch"] and "WebSearch" not in SA._pending(qdir)
