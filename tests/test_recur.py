"""``levain.recur`` and what it does natively: DONE on a repeating agenda advances it, CLEAR ends the series (row 188).

The arithmetic is pinned against ``tests/vectors/recur_vectors.json`` (generated from flow's own ``parse_recur`` /
``next_occurrence``, sha-pinned; where flow is on the machine the same vectors are re-derived live, so drift on either side is
red). The behaviour is run on real anneal stores in tmp dirs with the notify seam stubbed: nothing here pings a phone.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from anneal_memory.spores import intention_version

from levain import germinate as g
from levain import recur
from levain.consent import questions as questions_mod
from levain.consent.questions import INTENTION_CLOSE, QuestionStore
from levain.intentions import guarded_intention_store

VECTORS = Path(__file__).parent / "vectors" / "recur_vectors.json"
VECTORS_SHA256 = "314360cc759cf03743a1fe875f9a4e01f6e994cbab7002002d545b63e038f056"
_DATA = json.loads(VECTORS.read_text(encoding="utf-8"))
_FMT = "%Y-%m-%dT%H:%M"


def test_recur_vector_file_sha_pinned():
    assert hashlib.sha256(VECTORS.read_bytes()).hexdigest() == VECTORS_SHA256, \
        "tests/vectors/recur_vectors.json drifted; regenerate it from flow's functions and re-pin deliberately"


@pytest.mark.parametrize("case", [c for c in _DATA["cases"] if c["canon"] is None], ids=lambda c: repr(c["rule"]))
def test_a_rule_that_does_not_read_refuses_with_the_grammar(case):
    with pytest.raises(ValueError, match="Grammar"):
        recur.parse_recur(case["rule"])


@pytest.mark.parametrize("case", [c for c in _DATA["cases"] if c["canon"] is not None],
                         ids=lambda c: f"{c['rule']}|{c['after']}|{c['prev']}")
def test_the_canon_and_the_next_occurrence_equal_flows(case):
    assert recur.parse_recur(case["rule"])["canon"] == case["canon"]
    after = datetime.fromisoformat(case["after"])
    if case["next"] is None:
        with pytest.raises(ValueError):
            recur.next_occurrence(case["rule"], after, case["prev"])
    else:
        assert recur.next_occurrence(case["rule"], after, case["prev"]).strftime(_FMT) == case["next"]


def test_the_vectors_equal_flows_live_where_flow_is_on_the_machine():
    path = os.environ.get("LEVAIN_FLOW_INTENTIONS")
    if not path or not Path(path).is_file():
        pytest.skip("LEVAIN_FLOW_INTENTIONS (flow's scripts/intentions.py) is not set")
    sys.path.insert(0, str(Path(path).parent))
    try:
        spec = importlib.util.spec_from_file_location("flow_intentions_for_recur", path)
        flow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flow)
    finally:
        sys.path.remove(str(Path(path).parent))
    for c in _DATA["cases"]:
        if c["canon"] is None:
            with pytest.raises(ValueError):
                flow.parse_recur(c["rule"])
            continue
        assert flow.parse_recur(c["rule"])["canon"] == c["canon"]
        try:
            want = flow.next_occurrence(c["rule"], datetime.fromisoformat(c["after"]), c["prev"]).strftime(_FMT)
        except ValueError:
            want = None
        assert want == c["next"], c


def test_advance_changes_moves_next_and_anchor_to_the_first_occurrence_after_now_and_clears_the_stamp():
    item = {"recur": "weekly SUN 21:00", "next": "2026-11-08T21:00", "recur_anchor": "2026-11-08T21:00",
            "germinated": {"ref": "q1"}}
    assert recur.advance_changes(item, datetime(2026, 11, 8, 21, 5, 33)) == {
        "next": "2026-11-15T21:00", "recur_anchor": "2026-11-15T21:00", "germinated": {}}
    # a missed occurrence is never re-filed: done late, the series lands on the first one STILL AHEAD
    assert recur.advance_changes(item, datetime(2026, 11, 20, 8, 0))["next"] == "2026-11-22T21:00"


def test_every_n_days_counts_from_the_anchor_never_from_a_snoozed_next():
    item = {"recur": "every 14 days 10:00", "next": "2026-11-13T10:00", "recur_anchor": "2026-11-12T10:00"}   # snoozed a day
    assert recur.advance_changes(item, datetime(2026, 11, 13, 11, 0))["next"] == "2026-11-26T10:00"


@pytest.mark.parametrize("item", [{}, {"recur": None}, {"recur": 7}, {"recur": "hourly 5"}, {"recur": "daily 25:00"}])
def test_no_readable_rule_is_no_advance(item):
    assert recur.advance_changes(item, datetime(2026, 11, 8, 12, 0)) is None
    assert recur.repeats_words(item) == ""


def test_the_filed_row_words_name_the_repeat():
    assert recur.repeats_words({"recur": "weekly sun 21:00"}) == " (repeats weekly SUN 21:00)"


# --- the native series advance, on real stores --------------------------------------------------------------------------

RUN = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)         # the QuestionStore's clock
LOCAL_NOW = RUN.astimezone().replace(tzinfo=None)               # what intention_act reads as the wall clock


class _FarWall(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return datetime(2031, 6, 1, 12, 0, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2031, 6, 1, 12, 0)


@pytest.fixture(autouse=True)
def _wall_clock_far_away(monkeypatch):
    monkeypatch.setattr(questions_mod, "datetime", _FarWall)


def _ringing_series(tmp_path: Path, *, rule: str = "weekly SUN 21:00", first: str = "2026-11-06T09:00"):
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    intentions = root / ".levain" / "memory.spores.json"
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    sp = guarded_intention_store(intentions)
    sid = sp.add(type="task", text="bins", disposition="agenda", next=first, target="nudge", recur=rule, recur_anchor=first)["id"]
    pings: list[tuple] = []
    res = g.germinate(intentions, qdir, install=root, today=date(2026, 11, 7), now=datetime(2026, 11, 7, 12, 0),
                      ping=lambda *a, **k: pings.append((a, k)) or True)
    assert not res["errors"], res
    qs = QuestionStore(qdir, clock=lambda: RUN)
    (q,) = qs.open_questions()
    return root, intentions, qdir, sp, sid, qs, q


def test_done_on_a_repeating_agenda_advances_it_instead_of_resolving_it(tmp_path):
    root, intentions, qdir, sp, sid, qs, q = _ringing_series(tmp_path)
    said = qs.intention_act(q.id, "done", channel="cockpit")
    nxt = recur.next_occurrence("weekly SUN 21:00", LOCAL_NOW).strftime(_FMT)
    got = sp.get(sid)
    assert got["status"] == "open" and got["next"] == nxt and got["recur_anchor"] == nxt and not got.get("germinated")
    assert got["recur"] == "weekly SUN 21:00" and f"done; next {nxt}" in said
    closed = qs.get(q.id)
    assert closed.answer.value == INTENTION_CLOSE + f"done; next {nxt}" and qs.open_questions() == []


def test_clear_on_a_repeating_agenda_ends_the_series(tmp_path):
    root, intentions, qdir, sp, sid, qs, q = _ringing_series(tmp_path)
    qs.intention_act(q.id, "clear", channel="cockpit")
    got = sp.get(sid)
    assert got["status"] != "open" and got["resolution"]["kind"] == "dropped"


def test_snooze_moves_this_occurrence_only_and_keeps_the_series(tmp_path):
    root, intentions, qdir, sp, sid, qs, q = _ringing_series(tmp_path)
    qs.intention_act(q.id, "snooze", until="2026-11-12", channel="cockpit")
    got = sp.get(sid)
    assert (got["next"], got["recur"], got["status"]) == ("2026-11-12", "weekly SUN 21:00", "open")


def test_a_repeating_agenda_with_an_unreadable_rule_is_resolved_as_before(tmp_path):
    """A hand-edited bad rule must not strand a stamped agenda: done resolves it (the old behaviour)."""
    root, intentions, qdir, sp, sid, qs, q = _ringing_series(tmp_path, rule="daily 09:00")
    item = next(i for i in sp.list_open() if i["id"] == sid)
    sp.update(sid, recur="hourly 5", expect_disposition="agenda", expected_version=intention_version(item))
    qs.intention_act(q.id, "done", channel="cockpit")
    assert sp.get(sid)["status"] != "open"


def test_an_answered_band_row_advances_a_repeating_agenda_at_the_next_germinate_run(tmp_path):
    """The other door: the question is answered on the band (not through the intention controls); germinate's finish pass used
    to resolve the agenda as done, which ended a series at its first answer."""
    root, intentions, qdir, sp, sid, qs, q = _ringing_series(tmp_path)
    qs.answer(q.id, text="did it", channel="cli")
    now = datetime(2026, 11, 8, 12, 0)
    res = g.germinate(intentions, qdir, install=root, today=date(2026, 11, 8), now=now, ping=lambda *a, **k: True)
    assert res["errors"] == [] and [o["intention_id"] for o in res["finish"]] == [sid]
    got = sp.get(sid)
    nxt = recur.next_occurrence("weekly SUN 21:00", now).strftime(_FMT)
    assert (got["status"], got["next"], got["recur_anchor"]) == ("open", nxt, nxt) and not got.get("germinated")
    assert qs.open_questions() == []                                 # advanced, not filed again until it wakes
    later = g.germinate(intentions, qdir, install=root, today=date(2026, 11, 15), now=datetime(2026, 11, 15, 21, 1),
                        ping=lambda *a, **k: True)
    assert [o["intention_id"] for o in later["due"]] == [sid]        # the next occurrence files a fresh row


def test_a_plain_agenda_still_resolves_on_an_answered_row(tmp_path):
    """No `recur` on it: the finish pass is unchanged."""
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    intentions = root / ".levain" / "memory.spores.json"
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    sp = guarded_intention_store(intentions)
    sid = sp.add(type="task", text="once", disposition="agenda", next="2026-11-06T09:00", target="nudge")["id"]
    g.germinate(intentions, qdir, install=root, today=date(2026, 11, 7), now=datetime(2026, 11, 7, 12, 0), ping=lambda *a, **k: True)
    qs = QuestionStore(qdir, clock=lambda: RUN)
    (q,) = qs.open_questions()
    qs.answer(q.id, text="did it", channel="cli")
    g.germinate(intentions, qdir, install=root, today=date(2026, 11, 8), now=datetime(2026, 11, 8, 12, 0), ping=lambda *a, **k: True)
    assert sp.get(sid)["status"] != "open"


def test_the_band_row_a_repeating_agenda_files_says_it_repeats(tmp_path):
    root, intentions, qdir, sp, sid, qs, q = _ringing_series(tmp_path)
    assert q.what == f"Due 11-06: bins ({sid}) (repeats weekly SUN 21:00)"
