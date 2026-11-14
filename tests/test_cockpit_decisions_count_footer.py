"""The Decisions tile's count is the footer's NEEDS YOU rule: an open question the desk alone owes (section desk, not carried
over to him) and a question snoozed past today are not waiting on him, so neither is counted. His own row (section you), a
carried desk row and a row with no section are. The panel's ``count`` and ``actionable_count`` and the python predicate
(``band_view.desk_owed``) agree."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

from levain import band_view
from levain.cockpit.engine import Cockpit
from levain.cockpit.registry import counted_rows
from levain.consent import questions as qmod
from levain.consent.questions import QuestionStore, local_today

MARK = {                      # session id -> (section, carried)
    "you": ("you", False),
    "carried": ("desk", True),
    "desk": ("desk", False),
    "plain": ("", False),
    "later-you": ("you", False),
    "later-carried": ("desk", True),
}


def _store(tmp_path: Path, monkeypatch):
    qs = QuestionStore(tmp_path / "q")
    now = datetime.now().astimezone()
    today = datetime.fromisoformat(local_today(now)).date()
    made = {}
    for sid in MARK:
        made[sid] = qs.file(agent="claude_code", agent_session_id=sid, what=sid, options=[("a", "A"), ("b", "B")],
                            rec="a", blocks="b", cwd="/w")
    for sid in ("later-you", "later-carried"):
        qs.snooze(made[sid].id, until=(today + timedelta(days=2)).isoformat(), channel="cli", via="cli")
    orig = qmod.QuestionStore._question_of

    def with_section(row):
        q = orig(row)
        sec, car = MARK[q.agent_session_id]
        return dataclasses.replace(q, section=sec, carried=car)

    monkeypatch.setattr(qmod.QuestionStore, "_question_of", staticmethod(with_section))
    return qs, now, made


def test_the_decisions_count_is_the_footers_not_every_open_row(tmp_path: Path, monkeypatch) -> None:
    qs, now, made = _store(tmp_path, monkeypatch)
    ck = Cockpit(clock=lambda: now)
    ck.attach_question_rows(qs)
    p = ck.panel("decisions", credential_class="token")
    assert len(p["rows"]) == len(MARK)                       # every open row is listed
    want = {f"question:{made[s].n}" for s in ("you", "carried", "plain")}
    assert {r["id"] for r in counted_rows("decision.queue", p["rows"])} == want
    assert p["count"] == p["actionable_count"] == 3


def test_a_row_with_no_section_stays_counted(tmp_path: Path, monkeypatch) -> None:
    qs, now, made = _store(tmp_path, monkeypatch)
    ck = Cockpit(clock=lambda: now)
    ck.attach_question_rows(qs)
    p = ck.panel("decisions", credential_class="token")
    assert f"question:{made['plain'].n}" in {r["id"] for r in counted_rows("decision.queue", p["rows"])}


def test_the_count_and_the_one_python_rule_agree(tmp_path: Path, monkeypatch) -> None:
    qs, now, made = _store(tmp_path, monkeypatch)
    ck = Cockpit(clock=lambda: now)
    ck.attach_question_rows(qs)
    p = ck.panel("decisions", credential_class="token")
    due = [s for s in MARK if not s.startswith("later")]
    owed = [s for s in due if not band_view.desk_owed(*MARK[s])]
    assert p["count"] == len(owed)
