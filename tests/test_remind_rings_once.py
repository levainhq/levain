"""G2 (levain#21): a reminder at a time rings ONCE per (item, time), on the real germinate path over a real file store.

Row 188's reminder is a nudge agenda with a timed ``next``; the path that rings it is ``germinate`` (``is_due``, then
``file_once``, then the ping, then the stamp). Nothing here mocks that logic: only the ping seam is replaced (it would
reach a phone), and the clock is the ``now`` germinate takes, except the host-clock test, which leaves ``now`` and
``today`` unset so germinate reads one clock for both. What is tested for the key: one reminder rings once however many
times it ticks or its stamp is lost, two reminders at one minute ring once each, and a repeating reminder rings once per
occurrence."""
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


def _reminder(tmp_path, when: str, target: str = "nudge", text: str = "call the dentist", **extra) -> tuple[Path, str]:
    root = tmp_path / "a"
    (root / ".levain").mkdir(parents=True, exist_ok=True)
    path = root / ".levain" / "memory.spores.json"
    s = guarded_intention_store(path).add(type="thought", text=text, disposition="agenda", next="2026-11-14",
                                          target=target, **extra)
    data = json.loads(path.read_text())     # anneal writes a date-only next; the List's writer places the minute
    for row in data["intentions"]:
        if row["id"] == s["id"]:
            row["next"] = when
    path.write_text(json.dumps(data))
    return path, s["id"]


def _tick(path: Path, qdir: Path, now: datetime | None) -> dict:
    today = now.date() if now is not None else None     # unset: germinate takes one clock for both, no midnight split
    return g.germinate(path, qdir, install=path.parent.parent, today=today, now=now)


@pytest.mark.parametrize("late", [timedelta(minutes=1), timedelta(minutes=30), timedelta(hours=3), timedelta(days=1),
                                  timedelta(days=7)])
def test_a_reminder_rings_once_at_its_minute_and_at_any_lateness_and_never_twice(tmp_path, qdir, pings, late):
    path, sid = _reminder(tmp_path, DUE.strftime("%Y-%m-%dT%H:%M"))
    rows = lambda: QuestionStore(qdir).recent(100)  # noqa: E731
    for _ in range(2):                                          # one minute early, twice: nothing
        assert _tick(path, qdir, DUE - timedelta(minutes=1))["due"] == []
    assert rows() == [] and pings == []
    first = _tick(path, qdir, DUE + late)                       # the first tick at or after the minute rings it
    (out,) = first["due"]
    assert out["filed"] and out["pinged"] and out["done"] and first["errors"] == [] and first["skipped"] == []
    for at in (DUE + late, DUE + late + timedelta(minutes=1)):  # again: nothing more
        assert _tick(path, qdir, at)["due"] == []
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
    assert _tick(path, qdir, DUE + timedelta(days=7, minutes=1))["due"] == [] and len(pings) == 1


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


def test_a_stamp_that_fails_once_does_not_ring_the_held_row_again(tmp_path, qdir, pings, monkeypatch):
    """The stamp fails ("changed since read") after the row is filed and rung; the item stays due, and the next tick finds
    the row held. That tick files nothing, so it rings nothing: one ring for the one (item, time)."""
    path, sid = _reminder(tmp_path, "2026-11-14T10:00")
    real = QuestionStore.file_once
    hit: list[int] = []

    def file_then_bump(self, **kw):
        out = real(self, **kw)
        if not hit:                                     # a concurrent writer moves the item after it was read
            hit.append(1)
            guarded_intention_store(path).update(sid, text="call the dentist today")
        return out

    monkeypatch.setattr(QuestionStore, "file_once", file_then_bump)
    first = _tick(path, qdir, DUE)
    assert first["due"] == [] and len(first["skipped"]) == 1 and len(pings) == 1
    second = _tick(path, qdir, DUE + timedelta(minutes=1))
    assert len(QuestionStore(qdir).recent(100)) == 1 and len(pings) == 1, second
    assert len(second["due"]) == 1 and second["due"][0]["done"]      # the retry still stamps it
    assert _tick(path, qdir, DUE + timedelta(minutes=2))["due"] == [] and len(pings) == 1


def test_two_reminders_at_one_minute_each_ring_once_into_their_own_row(tmp_path, qdir, pings):
    path, a = _reminder(tmp_path, "2026-11-14T10:00", text="call the dentist")
    b = guarded_intention_store(path).add(type="thought", text="water the plants", disposition="agenda",
                                          next="2026-11-14", target="nudge")["id"]
    data = json.loads(path.read_text())
    for row in data["intentions"]:
        row["next"] = "2026-11-14T10:00"
    path.write_text(json.dumps(data))
    out = _tick(path, qdir, DUE)["due"]
    assert {o["intention_id"] for o in out} == {a, b} and len({o["ref"] for o in out}) == 2 and len(pings) == 2
    assert _tick(path, qdir, DUE + timedelta(minutes=1))["due"] == [] and len(pings) == 2


def test_a_repeating_reminder_rings_once_per_occurrence_and_a_retry_of_one_rings_nothing_more(tmp_path, qdir, pings):
    first, second = datetime(2026, 11, 8, 21, 0), datetime(2026, 11, 15, 21, 0)       # a Sunday and the next
    path, sid = _reminder(tmp_path, first.strftime("%Y-%m-%dT%H:%M"), recur="weekly SUN 21:00",
                          recur_anchor=first.strftime("%Y-%m-%dT%H:%M"))
    (one,) = _tick(path, qdir, first)["due"]
    assert _tick(path, qdir, first + timedelta(minutes=1))["due"] == [] and len(pings) == 1
    QuestionStore(qdir).answer(one["ref"], option="done", channel="cli")
    moved = _tick(path, qdir, first + timedelta(hours=1))                             # the answer moves it to the next Sunday
    assert moved["finish"] and moved["finish"][0]["via"] == "advanced" and moved["due"] == [] and len(pings) == 1
    (two,) = _tick(path, qdir, second)["due"]
    assert two["ref"] != one["ref"] and two["pinged"] and len(pings) == 2
    assert _tick(path, qdir, second + timedelta(minutes=1))["due"] == [] and len(pings) == 2
    assert len(QuestionStore(qdir).recent(100)) == 2


def test_a_rung_file_that_is_not_utf8_reads_as_empty_and_the_reminder_still_rings(tmp_path, qdir, pings):
    """An unreadable rung file repeats a ring rather than losing it: the reminder is filed, pinged and stamped."""
    (qdir / g._RUNG_FILE).write_bytes(b"\xff\xfe\x00bad")
    path, _ = _reminder(tmp_path, DUE.strftime("%Y-%m-%dT%H:%M"))
    out = _tick(path, qdir, DUE + timedelta(minutes=1))
    assert out["errors"] == [] and len(pings) == 1 and out["due"][0]["done"]


def test_a_torn_rung_line_does_not_glue_to_the_next_id(qdir):
    """A write torn before its newline leaves an id with none; the next mark must start its own line."""
    (qdir / g._RUNG_FILE).write_text("q-aaaa", encoding="utf-8")
    g._mark_rung(qdir, "q-bbbb")
    assert "q-bbbb" in g._rung_ids(qdir)
