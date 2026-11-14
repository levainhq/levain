"""G2 (levain#21): a reminder at a time rings ONCE per (item, time), on the real germinate path over a real file store.

Row 188's reminder is a nudge agenda with a timed ``next``; the path that rings it is ``germinate`` (``is_due`` then
``file_once`` keyed by the origin key and the minute, then the stamp). Nothing here mocks that logic: only the ping
seam is replaced (it would reach a phone), and the clock is the ``now`` germinate takes, except the last test, which
leaves ``now`` unset so the host's own clock decides."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from levain import germinate as g
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.intentions import guarded_intention_store

DUE = datetime(2026, 11, 14, 10, 0)


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "home" / "consent"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def pings(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(band, "_send_ping", lambda agent, q, qdir, extra="", kind="question": sent.append(q.id) or True)
    return sent


def _reminder(tmp_path, when: str, target: str = "nudge") -> tuple[Path, str]:
    root = tmp_path / "a"
    (root / ".levain").mkdir(parents=True)
    path = root / ".levain" / "memory.spores.json"
    s = guarded_intention_store(path).add(type="thought", text="call the dentist", disposition="agenda",
                                          next="2026-11-14", target=target)
    data = json.loads(path.read_text())     # anneal writes a date-only next; the List's writer places the minute
    for row in data["intentions"]:
        if row["id"] == s["id"]:
            row["next"] = when
    path.write_text(json.dumps(data))
    return path, s["id"]


def _tick(path: Path, qdir: Path, now: datetime | None) -> dict:
    return g.germinate(path, qdir, install=path.parent.parent, today=(now or datetime.now()).date(), now=now)


@pytest.mark.parametrize("late", [timedelta(minutes=1), timedelta(minutes=30), timedelta(hours=3), timedelta(days=1),
                                  timedelta(days=7)])
def test_a_reminder_rings_once_at_its_minute_and_at_any_lateness_and_never_twice(tmp_path, qdir, pings, late):
    path, sid = _reminder(tmp_path, DUE.strftime("%Y-%m-%dT%H:%M"))
    rows = lambda: QuestionStore(qdir).recent(100)  # noqa: E731
    for _ in range(2):                                          # one minute early, twice: nothing
        assert _tick(path, qdir, DUE - timedelta(minutes=1))["due"] == []
    assert rows() == [] and pings == []
    for at in (DUE, DUE + late, DUE + late):                    # on the minute, then later, then again: one ring
        _tick(path, qdir, at)
    assert len(rows()) == 1 and len(pings) == 1
    for at in (DUE + late + timedelta(minutes=5), DUE + timedelta(days=30)):
        assert _tick(path, qdir, at)["due"] == []
    assert len(rows()) == 1 and len(pings) == 1


def test_the_first_tick_after_the_minute_rings_it_whatever_the_gap_so_there_is_no_grace_cutoff(tmp_path, qdir, pings):
    """A machine that was off at 10:00 rings the reminder when it next ticks, a week on. That is the behaviour today and
    G2's grace window is not a cut: no tick inside the grace is not a reason to say nothing."""
    path, _ = _reminder(tmp_path, "2026-11-14T10:00")
    (out,) = _tick(path, qdir, DUE + timedelta(days=7))["due"]
    assert out["filed"] and out["pinged"] and len(pings) == 1


def test_the_host_clock_decides_when_no_now_is_given(tmp_path, qdir, pings):
    past = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
    path, _ = _reminder(tmp_path, past)
    assert len(_tick(path, qdir, None)["due"]) == 1 and len(pings) == 1
    assert _tick(path, qdir, None)["due"] == [] and len(pings) == 1
    far = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    other = tmp_path / "b"
    other.mkdir()
    path2, _ = _reminder(other, far)
    assert _tick(path2, qdir, None)["due"] == [] and len(pings) == 1
