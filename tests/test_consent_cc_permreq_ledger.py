"""The PermissionRequest decision path files its decision-time ledger row (1106-51): the live Claude Code hook is a
PermissionRequest hook only, and before this its signed allow left no row in the consent ledger."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from levain.consent import policy as policy_mod
from levain.consent.adapters import claude_code as cc
from levain.consent.ledger import Ledger
from tests.test_consent_cc_adapter import AWAY, FakeQueue, event


class DirQueue(FakeQueue):
    def __init__(self, directory, verdict=None, **kw):
        super().__init__(verdict, **kw)
        self.directory = Path(directory)


def run(monkeypatch, queue, e):
    monkeypatch.setattr(cc, "_queue_for", lambda d, s, **kw: queue)
    monkeypatch.setattr(cc, "load_settings", lambda d: AWAY)
    monkeypatch.setattr(cc, "nothing_can_decide", lambda s: False)
    out = io.StringIO()
    assert cc.main(["--queue-dir", str(queue.directory)], stdin=io.StringIO(json.dumps(e)), stdout=out) == 0
    return out.getvalue()


def counts(qdir):
    return Ledger(qdir).counts()


def test_a_verified_allow_files_an_answer_row(monkeypatch, tmp_path):
    q = DirQueue(tmp_path, "allow")
    out = run(monkeypatch, q, event())
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert counts(tmp_path) == [("claude_code", "answer", "unknown", 1)]
    row = Ledger(tmp_path).calls()[0] if hasattr(Ledger, "calls") else None
    if row is not None:
        assert row.phase == "pre" and row.request_id == q.requests[0].id and row.tool_use_id is None


def test_a_verified_deny_files_a_denied_by_gate_row(monkeypatch, tmp_path):
    q = DirQueue(tmp_path, "deny")
    run(monkeypatch, q, event())
    assert counts(tmp_path) == [("claude_code", "unknown", "refused", 1)]


def test_no_decision_files_no_row(monkeypatch, tmp_path):
    q = DirQueue(tmp_path, None)
    assert run(monkeypatch, q, event()) == ""
    assert counts(tmp_path) == []


def test_a_ledger_that_cannot_be_written_never_changes_the_answer(monkeypatch, tmp_path):
    import levain.consent.ledger as led

    def boom(*a, **k):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(led, "gate_row", boom)
    out = run(monkeypatch, DirQueue(tmp_path, "allow"), event())
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
