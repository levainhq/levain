"""LATER in the counts: a band question dated past today is not WAITING anywhere. The Work panel's topology feed (its
"waiting on you" list and count) and the Decisions tile's count read the same predicate as the Needs you strip
(band_view.later_facets / later_of); a fault there is due, so the question stays counted."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from levain.band_view import later_facets
from levain.cockpit import topology as topo
from levain.cockpit.engine import ReadContext, _question_rows_in
from levain.cockpit.registry import ORDERINGS, apply_ordering, counted_rows, validate_facets
from levain.consent.questions import QuestionStore, local_today
from tests.test_cockpit_activity import _session

DECISION = "decision.queue"


def _store(tmp_path: Path):
    """Three open questions: one due, two snoozed past today."""
    qs = QuestionStore(tmp_path / "q")
    now = datetime.now().astimezone()
    today = datetime.fromisoformat(local_today(now)).date()
    mk = lambda name, sid: qs.file(agent="claude_code", agent_session_id=sid, what=name, options=[("a", "A"), ("b", "B")], rec="a", blocks="b", cwd="/w")
    due, l1, l2 = mk("Due", "s-due"), mk("Later one", "s-l1"), mk("Later two", "s-l2")
    qs.snooze(l1.id, until=(today + timedelta(days=1)).isoformat(), channel="cli", via="cli")
    qs.snooze(l2.id, until=(today + timedelta(days=3)).isoformat(), channel="cli", via="cli")
    return qs, now, due, l1, l2


def test_the_work_panels_waiting_list_leaves_out_a_dated_question(tmp_path: Path) -> None:
    qs, now, due, l1, l2 = _store(tmp_path)
    root = tmp_path / "inst"
    for sid in ("s-due", "s-l1", "s-l2"):
        _session(root, sid, "/w", timedelta(minutes=1))
    read = topo.topology_reader(install_root=root, consent=lambda: None, questions=lambda: qs, hub_label=lambda c: "x")
    v = read(ReadContext(now.astimezone(timezone.utc))).value      # the instant _store judged its days at: no midnight race
    waiting = [t["card"] for t in v["tasks"] if t["kind"] == "question"]
    assert waiting == [f"question:{due.n}"], waiting          # the strip's and flow's footer's count: one
    assert [n["state"] for n in v["nodes"] if n["state"] == "waiting"] == ["waiting"], "only the due question's session waits"


def test_a_fault_in_the_one_predicate_leaves_the_question_waiting(tmp_path: Path, monkeypatch) -> None:
    qs, now, due, l1, l2 = _store(tmp_path)
    import levain.band_view as bv
    monkeypatch.setattr(bv, "_later_pair", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert later_facets(qs, l1, now) is None
    assert len(topo._questions_at(qs, None, now)) == 3


def test_the_decisions_tile_counts_only_due_questions(tmp_path: Path) -> None:
    qs, now, due, l1, l2 = _store(tmp_path)
    rows, _left = _question_rows_in(qs, now)
    for r in rows:
        validate_facets(r.facets)
    done = [{"id": r.id, "facets": r.facets, "group": g} for r, g in apply_ordering(DECISION, rows, now.date())]
    groups = {d["id"]: d["group"] for d in done}
    assert groups[f"question:{due.n}"] == "needs_input"
    assert groups[f"question:{l1.n}"] == groups[f"question:{l2.n}"] == "later"
    assert [d["id"] for d in counted_rows(DECISION, done)] == [f"question:{due.n}"]
    assert ("later", "Later") in ORDERINGS[DECISION].groups and "later" in ORDERINGS[DECISION].uncounted
    assert [d["id"] for d in done if d["group"] == "later"] == [f"question:{l1.n}", f"question:{l2.n}"]    # soonest day first


def test_the_push_badge_counts_only_due_questions(tmp_path: Path) -> None:
    from levain.webpush.needs import open_needs_count
    _store(tmp_path)
    assert open_needs_count(tmp_path / "q") == 1        # the strip's count, not the three open rows


def test_a_later_facet_kept_past_its_day_counts_again(tmp_path: Path) -> None:
    """A row whose question_later day has come (kept across the midnight its snooze ended at) is due on the tile."""
    qs, now, due, l1, l2 = _store(tmp_path)
    rows, _left = _question_rows_in(qs, now)
    groups = {r.id: g for r, g in apply_ordering(DECISION, rows, now.date() + timedelta(days=1))}
    assert groups[f"question:{l1.n}"] == "needs_input"     # snoozed until tomorrow: on tomorrow it is due
    assert groups[f"question:{l2.n}"] == "later"
