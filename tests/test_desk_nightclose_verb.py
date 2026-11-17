"""ND-10c: ``levain coord nightclose --token N``, the production caller of the morning quiescence (levain#19). The real
CLI entry against a scratch store; the effects half reads the journal ONLY when ``effect_attempts.db`` is already in the
store directory, and the verb never creates it. A journal row here is seeded by the real Journal API (no production
reserve exists yet)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.desk.effect_attempts import JOURNAL_FILE, Journal
from levain.desk.nightclose import EFFECT_PREFIX, STOP_PREFIX
from tests.test_desk_events import Clock, Pinger, pinger, rows, sensor, store, down, qdir  # noqa: F401
from tests.test_desk_nightclose import latest, morning, night
from tests.test_desk_nightclose_effects import reserve


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))


@pytest.fixture
def clock():
    c = Clock()
    c.now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=22)
    return c                                    # the night's 21 minutes end at the real now the CLI reads


def run(qdir, capsys, *argv, rc=0):
    got = main(["coord", "nightclose", *argv, "--queue-dir", str(qdir)])
    out = capsys.readouterr()
    assert got == rc, (argv, out.out, out.err)
    return out


def test_events_half_a_claimed_event_of_another_launch_is_handed_to_the_owner_and_the_stop_is_recorded(
        store, tmp_path, qdir, clock, pinger, capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    out = run(qdir, capsys, "--token", str(token))
    assert out.out.startswith(f"nightclose: owner {b.launch_id}; stop written; events handed 2 (")
    for eid in evs[:2]:
        assert latest(qdir, eid) == ("claimed", b.launch_id) and eid in out.out
    assert len(rows(qdir, f"SELECT key FROM meta WHERE key GLOB '{STOP_PREFIX}*'")) == 1
    assert latest(qdir, evs[2])[0] == "received"                            # nobody's, left for the owner's brief


def test_journal_present_an_unknown_attempt_is_recorded_for_the_owner(store, tmp_path, qdir, clock, pinger, capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    row = reserve(Journal(qdir / JOURNAL_FILE), evs[0])                     # seeded by the real Journal API
    b, token = morning(store, a)
    out = run(qdir, capsys, "--token", str(token))
    assert f"effects recorded 1 ({row.attempt_id})" in out.out
    assert rows(qdir, f"SELECT key FROM meta WHERE key GLOB '{EFFECT_PREFIX}*'") == [
        (f"{EFFECT_PREFIX}{row.attempt_id:020d}:{b.launch_id}",)]


def test_journal_absent_says_so_writes_the_events_half_and_creates_no_file(store, tmp_path, qdir, clock, pinger,
                                                                           capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    before = sorted(p.name for p in qdir.iterdir())
    out = run(qdir, capsys, "--token", str(token))
    assert "no effect journal exists yet" in out.out and "events handed 2" in out.out
    assert latest(qdir, evs[0]) == ("claimed", b.launch_id)
    assert not (qdir / JOURNAL_FILE).exists()
    assert sorted(p.name for p in qdir.iterdir()) == before                 # no new file of any name
    j = json.loads(run(qdir, capsys, "--token", str(token), "--json").out)
    assert j["journal"] is False and j["handed"] == [] and j["effects"] == [] and j["stopped"] is False


def test_no_lease_refuses_clearly_and_writes_nothing(store, tmp_path, qdir, clock, pinger, capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    b, token = morning(store, a)
    before = rows(qdir, "SELECT COUNT(*) FROM event_states UNION ALL SELECT COUNT(*) FROM meta")
    out = main(["coord", "nightclose", "--token", str(token + 100), "--queue-dir", str(qdir)])
    err = capsys.readouterr()
    assert out != 0 and "lease" in (err.err + err.out)
    assert rows(qdir, "SELECT COUNT(*) FROM event_states UNION ALL SELECT COUNT(*) FROM meta") == before
    assert not (qdir / JOURNAL_FILE).exists()
