"""C10 S4: the coordinator reaching ``stopped`` files ONE open band item, and the panel's Start, Stop or Hold closes it.
The deaths are the real store API driving ``launch_session`` against the fake ``claude`` (no real harness, no browser)."""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import timedelta

import pytest

from levain import band_view
from levain.cockpit.coordinator_status import HOLD_VERB, START_VERB, STOP_VERB
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.coordinator import launch as cl
from levain.coordinator import lease
from levain.writes import EditError
from tests.test_coordinator_verbs import (_deaths, _fire, _held_back, _levain_home, _panel_state, _verbs,  # noqa: F401
                                          clock, fake, live_hook, qdir, quick, store, workdir)


def _open(qdir):
    return [q for q in QuestionStore(qdir).open_questions() if q.agent_session_id == band.COORDINATOR_STOPPED_KEY]


def _all(qdir):
    return [q for q in QuestionStore(qdir).recent(50) if q.agent_session_id == band.COORDINATOR_STOPPED_KEY]


def _stopped(store, fake, clock, qdir):
    _deaths(store, fake, clock, lease.MAX_DEATHS)
    assert _panel_state(store, clock)[0] == "stopped"
    return _open(qdir)


def test_three_deaths_file_exactly_one_open_item_with_the_point_and_the_why(store, qdir, fake, live_hook, clock):
    _deaths(store, fake, clock, lease.MAX_DEATHS - 1)
    assert _all(qdir) == []                                   # backoff: nothing yet
    clock[0] += timedelta(hours=1)
    _deaths(store, fake, clock, 1)
    (q,) = _open(qdir)
    why = _panel_state(store, clock)[1]
    assert len(q.what.splitlines()[0]) < 120 and q.what.startswith("The coordinator stopped after three deaths")
    assert "nothing launches until you look" in q.what
    assert why in q.blocks and q.agent == "claude_code"
    assert [k for k, _ in q.options] == ["hold", "stop"] and q.rec == "stop"
    assert len(_all(qdir)) == 1


def test_a_further_pass_files_no_second_item_and_the_number_is_unchanged(store, qdir, fake, live_hook, clock):
    (first,) = _stopped(store, fake, clock, qdir)
    qs = QuestionStore(qdir)
    assert "The coordinator stopped after three deaths" in band_view.render(qs)
    n_band = band_view._number(qs, [first.id], time.monotonic() + 5)[first.id]
    for _ in range(2):
        cl._note_stopped(store)
        assert band.file_coordinator_stopped(qdir, "claude_code", "again") is None
    cl.watch_once(store)
    (again,) = _all(qdir)
    assert (again.id, again.n) == (first.id, first.n) and again.answer is None
    assert band_view._number(qs, [first.id], time.monotonic() + 5)[first.id] == n_band
    assert band_view.render(qs).count("The coordinator stopped") == 1


def test_the_item_is_an_ordinary_numbered_open_item_in_the_band_view(store, qdir, fake, live_hook, clock, capsys):
    _stopped(store, fake, clock, qdir)
    assert band_view.run_band(argparse.Namespace(queue_dir=qdir)) == 0
    out = capsys.readouterr().out
    assert out.startswith("NEEDS YOU") and "1. [" in out and "The coordinator stopped after three deaths" in out
    assert "Hold it myself" in out and "Stop: release" in out
    con = sqlite3.connect(qdir / "questions.db")
    assert con.execute("SELECT n, source FROM band_items").fetchall() == [(1, "question")]


@pytest.mark.parametrize("verb, option", [(STOP_VERB, "stop"), (HOLD_VERB, "hold")])
def test_stop_and_hold_close_the_item(store, qdir, fake, live_hook, clock, verb, option):
    (q,) = _stopped(store, fake, clock, qdir)
    _fire(_verbs(qdir, clock), verb)
    assert _open(qdir) == []
    (done,) = _all(qdir)
    assert done.id == q.id and (done.answer.kind, done.answer.value, done.answer.channel) == ("option", option,
                                                                                               "cockpit")
    assert "The coordinator stopped" not in band_view.render(QuestionStore(qdir))


def test_start_closes_it_when_the_start_goes_through(store, qdir, fake, live_hook, clock):
    (q,) = _stopped(store, fake, clock, qdir)
    store.coord_release()                                     # looked at from the CLI: the deaths no longer count
    assert _panel_state(store, clock)[0] == "wake"
    _fire(_verbs(qdir, clock, launcher=_held_back), START_VERB)
    (done,) = _all(qdir)
    assert done.id == q.id and done.answer.kind == "text" and done.answer.channel == "cockpit"
    assert "Start" in done.answer.value and _open(qdir) == []


def test_a_refused_start_leaves_it_open(store, qdir, fake, live_hook, clock):
    (q,) = _stopped(store, fake, clock, qdir)
    with pytest.raises(EditError):
        _fire(_verbs(qdir, clock, launcher=_held_back), START_VERB)
    assert [x.id for x in _open(qdir)] == [q.id]


def test_a_coordinator_that_is_not_stopped_files_nothing(store, qdir, fake, live_hook, clock):
    cl._note_stopped(store)
    assert _panel_state(store, clock)[0] == "wake" and _all(qdir) == []
    v = _verbs(qdir, clock, launcher=_held_back)
    _fire(v, START_VERB)
    v.wait_starts(30)
    cl._note_stopped(store)
    assert _panel_state(store, clock)[0] == "launching" and _all(qdir) == []
    _fire(v, HOLD_VERB)
    cl._note_stopped(store)
    assert _panel_state(store, clock)[0] == "held" and _all(qdir) == []
    _fire(v, STOP_VERB)
    _deaths(store, fake, clock, 1)
    cl._note_stopped(store)
    assert _panel_state(store, clock)[0] == "backoff" and _all(qdir) == []


def test_a_later_stop_after_the_item_closed_files_a_new_one(store, qdir, fake, live_hook, clock):
    (first,) = _stopped(store, fake, clock, qdir)
    _fire(_verbs(qdir, clock), HOLD_VERB)
    _fire(_verbs(qdir, clock), STOP_VERB)
    clock[0] += timedelta(hours=1)
    (second,) = _stopped(store, fake, clock, qdir)
    assert second.n != first.n and len(_all(qdir)) == 2
