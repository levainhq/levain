"""ND-10c: ``levain coord nightclose --token N``, the production caller of the morning quiescence (levain#19). The real
CLI entry against a scratch store; the effects half reads the journal ONLY when ``effect_attempts.db`` is already in the
store directory, and the verb never creates it. A journal row here is seeded by the real Journal API (no production
reserve exists yet)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.cli import main
from levain.desk.effect_attempts import JOURNAL_FILE, Journal, ReadOnlyJournal
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
    assert "effects recorded 0 (no effect journal exists yet" in out.out and "events handed 2" in out.out
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


# ---- ND-10f: the journal is read READ-ONLY and refused before any write (L3 round 1) ----------------------------------

def _snapshot(qdir):
    return {p.name: p.read_bytes() for p in sorted(qdir.iterdir()) if p.is_file() and p.name.startswith(JOURNAL_FILE)}, \
        sorted(p.name for p in qdir.iterdir())


def _refuses_writing_nothing(qdir, capsys, token):
    before = rows(qdir, "SELECT COUNT(*) FROM event_states UNION ALL SELECT COUNT(*) FROM meta")
    got = main(["coord", "nightclose", "--token", str(token), "--queue-dir", str(qdir)])
    err = capsys.readouterr()
    assert got != 0 and (err.err + err.out).strip(), (got, err)
    assert rows(qdir, f"SELECT key FROM meta WHERE key GLOB '{STOP_PREFIX}*' OR key GLOB '{EFFECT_PREFIX}*'") == []
    assert rows(qdir, "SELECT COUNT(*) FROM event_states UNION ALL SELECT COUNT(*) FROM meta") == before
    return err


def _old_schema_journal(path, *, with_recipient=True, with_limits=False):
    cols = ("attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL, "
            "order_sha256 TEXT NOT NULL, incarnation INTEGER NOT NULL, verb TEXT NOT NULL, event_id TEXT NOT NULL, "
            "args_digest TEXT NOT NULL, " + ("recipient TEXT, " if with_recipient else "") +
            "incident_scope TEXT NOT NULL, night_scope TEXT NOT NULL, status TEXT NOT NULL, reserved_at TEXT NOT NULL, "
            "settled_at TEXT" + (", per_incident INTEGER, per_night INTEGER" if with_limits else ""))
    c = sqlite3.connect(path)
    try:
        c.execute(f"CREATE TABLE effect_attempts ({cols})")
        c.commit()
    finally:
        c.close()


def test_wrong_token_leaves_a_healthy_journal_byte_identical_and_makes_no_sidecar(store, tmp_path, qdir, clock, pinger,
                                                                                  capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    reserve(Journal(qdir / JOURNAL_FILE), evs[0])
    b, token = morning(store, a)
    before = _snapshot(qdir)
    _refuses_writing_nothing(qdir, capsys, token + 100)
    assert _snapshot(qdir) == before                                        # bytes and directory listing (no -wal/-shm)


def test_wrong_token_does_not_migrate_an_old_schema_journal(store, tmp_path, qdir, clock, pinger, capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    _old_schema_journal(qdir / JOURNAL_FILE)                                # no per_incident / per_night columns
    b, token = morning(store, a)
    before = _snapshot(qdir)
    _refuses_writing_nothing(qdir, capsys, token + 100)
    assert _snapshot(qdir) == before
    c = sqlite3.connect(qdir / JOURNAL_FILE)
    try:
        assert "per_incident" not in {r[1] for r in c.execute("PRAGMA table_info(effect_attempts)")}
    finally:
        c.close()


def test_right_token_still_records_the_unknown_attempt_of_an_old_schema_journal(store, tmp_path, qdir, clock, pinger,
                                                                                capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    _old_schema_journal(qdir / JOURNAL_FILE)
    c = sqlite3.connect(qdir / JOURNAL_FILE)
    c.execute("INSERT INTO effect_attempts (idem_key, order_id, order_sha256, incarnation, verb, event_id, args_digest, "
              "recipient, incident_scope, night_scope, status, reserved_at) VALUES "
              "('k', 'so-1', 'a', 1, 'restart_service', ?, 'd', NULL, 'i', 'n', 'unknown', 't')", (evs[0],))
    c.commit()
    c.close()
    b, token = morning(store, a)
    out = run(qdir, capsys, "--token", str(token))
    assert "effects recorded 1 (1)" in out.out


@pytest.mark.parametrize("kind", ["directory", "dangling_symlink"])
def test_a_wrong_type_object_named_as_the_journal_refuses_before_any_write(store, tmp_path, qdir, clock, pinger, capsys,
                                                                           kind):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    if kind == "directory":
        (qdir / JOURNAL_FILE).mkdir()
    else:
        (qdir / JOURNAL_FILE).symlink_to(tmp_path / "nowhere.db")
    b, token = morning(store, a)
    _refuses_writing_nothing(qdir, capsys, token)                           # the RIGHT token: refused for the journal
    assert not (tmp_path / "nowhere.db").exists()


def test_a_journal_table_lacking_a_selected_column_refuses_before_any_write(store, tmp_path, qdir, clock, pinger,
                                                                            capsys):
    cred, a, evs = night(store, tmp_path, qdir, clock, pinger)
    _old_schema_journal(qdir / JOURNAL_FILE, with_recipient=False)
    b, token = morning(store, a)
    before = _snapshot(qdir)
    _refuses_writing_nothing(qdir, capsys, token)
    assert _snapshot(qdir) == before


def test_read_only_journal_reads_what_journal_reads_and_never_creates_a_missing_file(tmp_path):
    full = Journal(tmp_path / "a.db")
    reserve(full, "ev-1")
    assert ReadOnlyJournal(tmp_path / "a.db").rows() == full.rows() != []
    with pytest.raises(sqlite3.Error):
        ReadOnlyJournal(tmp_path / "missing.db").rows()
    assert not (tmp_path / "missing.db").exists() and sorted(p.name for p in tmp_path.glob("missing*")) == []


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_read_only_journal_never_takes_the_immutable_path_with_any_sidecar(tmp_path, sidecar):
    db = tmp_path / "a.db"
    db.write_bytes(b"")
    assert "immutable=1" in ReadOnlyJournal(db)._uri()
    (tmp_path / f"a.db{sidecar}").write_bytes(b"")
    assert "immutable" not in ReadOnlyJournal(db)._uri() and "mode=ro" in ReadOnlyJournal(db)._uri()
