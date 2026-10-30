"""C10 S4: the coordinator reaching ``stopped`` files ONE band item per stop episode, and the panel's Start, Stop or
Hold closes it; an acknowledgement records and acts on nothing.
The deaths are the real store API driving ``launch_session`` against the fake ``claude`` (no real harness, no browser)."""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import timedelta

import pytest

from levain import band_view
from levain.cockpit.consent import ConsentError
from levain.cockpit.coordinator_status import HOLD_VERB, START_VERB, STOP_VERB
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.coordinator import launch as cl
from levain.coordinator import lease
from levain.writes import EditError
from tests.test_coordinator_verbs import (_deaths, _fire, _held_back, _levain_home, _panel_state, _verbs,  # noqa: F401
                                          clock, fake, live_hook, qdir, quick, store, workdir)


def _ours(q) -> bool:
    return q.agent_session_id.startswith(band.COORDINATOR_STOPPED_KEY + ":")


def _open(qdir):
    return [q for q in QuestionStore(qdir).open_questions() if _ours(q)]


def _all(qdir):
    return [q for q in QuestionStore(qdir).recent(50) if _ours(q)]


@pytest.fixture
def pings(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(band, "_send_ping", lambda agent, q, qdir, extra="", kind="question": sent.append(q.id) or True)
    return sent


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
    assert "nothing launches until you act on the Coordinator panel" in q.what
    assert why in q.blocks and q.agent == "claude_code"
    assert "Stop or Hold on the cockpit's Coordinator panel" in q.blocks
    # an answer records only: the one option acknowledges, and the panel's verbs are named as where the action is
    (opt,) = q.options
    assert opt[0] == "seen" and q.rec == "seen" and "Stop or Hold" in opt[1] and "Coordinator panel" in opt[1]
    assert len(_all(qdir)) == 1


def test_a_further_pass_files_no_second_item_and_the_number_is_unchanged(store, qdir, fake, live_hook, clock):
    (first,) = _stopped(store, fake, clock, qdir)
    qs = QuestionStore(qdir)
    assert "The coordinator stopped after three deaths" in band_view.render(qs)
    n_band = band_view._number(qs, [first.id], time.monotonic() + 5)[first.id]
    episode = first.agent_session_id.split(":", 1)[1]
    for _ in range(2):
        cl._note_stopped(store)
        assert band.file_coordinator_stopped(qdir, "claude_code", "again", episode) is None
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
    assert "Seen: I will press Stop or Hold" in out
    con = sqlite3.connect(qdir / "questions.db")
    assert con.execute("SELECT n, source FROM band_items").fetchall() == [(1, "question")]


@pytest.mark.parametrize("verb, word", [(STOP_VERB, "Stop"), (HOLD_VERB, "Hold")])
def test_stop_and_hold_close_the_item_with_text_naming_the_verb(store, qdir, fake, live_hook, clock, verb, word):
    (q,) = _stopped(store, fake, clock, qdir)
    _fire(_verbs(qdir, clock), verb)
    assert _open(qdir) == []
    (done,) = _all(qdir)
    assert done.id == q.id and (done.answer.kind, done.answer.channel) == ("text", "cockpit")
    assert done.answer.value == f"{word} pressed on the cockpit's Coordinator panel"
    assert "The coordinator stopped" not in band_view.render(QuestionStore(qdir))


def test_start_closes_it_when_the_start_goes_through(store, qdir, fake, live_hook, clock):
    (q,) = _stopped(store, fake, clock, qdir)
    store.coord_release()                                     # looked at from the CLI: the deaths no longer count
    assert _panel_state(store, clock)[0] == "wake"
    _fire(_verbs(qdir, clock, launcher=_held_back), START_VERB)
    (done,) = _all(qdir)
    assert done.id == q.id and done.answer.kind == "text" and done.answer.channel == "cockpit"
    assert done.answer.value == "Start pressed on the cockpit's Coordinator panel" and _open(qdir) == []


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


# --- the stop episode, the acknowledgement, the agent (L3 r1) --------------------------------------------------------
def _first_death(store) -> str:
    coord = [ln for ln in store.coord_status()["launches"] if ln["role"] == "coordinator"]   # newest first
    assert len(coord) == lease.MAX_DEATHS
    return coord[-1]["launch_id"]


def test_the_key_names_the_stop_episode_by_its_first_death(store, qdir, fake, live_hook, clock):
    (q,) = _stopped(store, fake, clock, qdir)
    assert q.agent_session_id == f"{band.COORDINATOR_STOPPED_KEY}:{_first_death(store)}"
    assert cl._stop_episode(store) == _first_death(store)


def test_an_item_left_open_by_a_stop_cleared_on_the_cli_does_not_hold_back_the_next_stop(store, qdir, fake, live_hook,
                                                                                         clock, pings):
    (first,) = _stopped(store, fake, clock, qdir)
    store.coord_release()                                     # looked at from the CLI: the panel's verbs never ran
    cl.watch_once(store)
    clock[0] += timedelta(hours=1)
    _deaths(store, fake, clock, lease.MAX_DEATHS)
    assert _panel_state(store, clock)[0] == "stopped"
    second = [q for q in _open(qdir) if q.id != first.id]
    assert len(second) == 1 and pings == [first.id, second[0].id]


def test_passes_while_the_item_stands_or_while_healthy_take_no_write_lock_on_the_band(store, qdir, fake, live_hook,
                                                                                     clock, monkeypatch):
    (q,) = _stopped(store, fake, clock, qdir)

    def no_write(self):
        raise AssertionError("a pass with nothing to write took the band's write lock")

    monkeypatch.setattr(QuestionStore, "_write", no_write)
    for _ in range(3):
        cl._note_stopped(store)                               # stopped, the item open: a read finds it
    monkeypatch.undo()
    _fire(_verbs(qdir, clock), STOP_VERB)
    monkeypatch.setattr(QuestionStore, "_write", no_write)
    for _ in range(3):
        cl._note_stopped(store)                               # healthy
    assert _panel_state(store, clock)[0] == "wake" and _open(qdir) == []


def test_an_acknowledged_item_is_not_filed_again_while_the_same_episode_stands(store, qdir, fake, live_hook, clock,
                                                                              pings):
    (q,) = _stopped(store, fake, clock, qdir)
    assert pings == [q.id]
    QuestionStore(qdir).answer(q.id, option="seen", channel="phone")
    for _ in range(3):
        cl._note_stopped(store)
        clock[0] += timedelta(minutes=10)
    cl.watch_once(store)
    assert _panel_state(store, clock)[0] == "stopped"         # the answer acted on nothing
    assert [x.id for x in _all(qdir)] == [q.id] and _open(qdir) == [] and pings == [q.id]


def test_a_fresh_episode_after_an_acknowledgement_files_a_new_item(store, qdir, fake, live_hook, clock, pings):
    (first,) = _stopped(store, fake, clock, qdir)
    QuestionStore(qdir).answer(first.id, option="seen", channel="cli")
    store.coord_release()                                     # he looked: the run of deaths ends
    cl._note_stopped(store)
    clock[0] += timedelta(hours=1)
    (second,) = _stopped(store, fake, clock, qdir)
    assert second.id != first.id and second.agent_session_id != first.agent_session_id
    assert second.agent_session_id == f"{band.COORDINATOR_STOPPED_KEY}:{cl._stop_episode(store)}"
    assert pings == [first.id, second.id]
    (old,) = [x for x in _all(qdir) if x.id == first.id]
    assert (old.answer.value, old.answer.channel) == ("seen", "cli")


def test_a_verb_that_lands_while_the_watcher_files_sends_no_ping(store, qdir, fake, live_hook, clock, pings,
                                                                monkeypatch):
    _deaths(store, fake, clock, lease.MAX_DEATHS - 1)
    clock[0] += timedelta(hours=1)
    real = QuestionStore.file_once

    def racing(self, **kw):
        out = real(self, **kw)
        store.coord_take()                                    # Hold commits between the filing and its ping
        return out

    monkeypatch.setattr(QuestionStore, "file_once", racing)
    _deaths(store, fake, clock, 1)
    assert _panel_state(store, clock)[0] == "held" and pings == []
    # no answer channel is Levain's, so the item stays for a person to close (the head's ruling, L3 r1 A)
    (left,) = _open(qdir)
    assert left.answer is None


@pytest.mark.parametrize("setting", [{}, {"agent": "gemini"}, None])
def test_a_missing_or_unmapped_agent_still_files_the_item(store, qdir, fake, live_hook, clock, monkeypatch, setting):
    real = store.coord_status
    monkeypatch.setattr(store, "coord_status", lambda: {**real(), "setting": setting})
    (q,) = _stopped(store, fake, clock, qdir)
    assert q.agent == "claude_code"


def test_a_single_option_is_a_system_sources_alone_and_answers_share_one_write(qdir):
    qs = QuestionStore(qdir)
    with pytest.raises(ConsentError):
        qs.file(agent="claude_code", agent_session_id="s-2", what="w", options=[("a", "A")], rec="a", blocks="b",
                cwd=str(qdir))
    q, filed = qs.file_once(agent="claude_code", key="k:1", what="w", options=[("a", "A")], rec="a", blocks="b",
                            cwd=str(qdir))
    assert filed and len(q.options) == 1
    with pytest.raises(ConsentError):                         # the close holds the answer's own refusals
        qs.close_open("k", option=["a"], channel="cockpit")
    with pytest.raises(ConsentError):
        qs.close_open("k", option="nope", channel="cockpit")
    with pytest.raises(ConsentError):
        qs.close_open("k", text="t", channel="watcher")
    assert qs.get(q.id).answer is None
    (done,) = qs.close_open("k", text="closed", channel="cockpit")
    assert qs.get(done.id).answer.value == "closed" and qs.close_open("k", text="again", channel="cockpit") == []
