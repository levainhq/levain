"""The coordinator store is only READ by those who only read it (lane door, 1021-18)."""
import sqlite3

from levain.dispatch.jobs import JobStore


def test_doctor_adds_no_coordinator_tables_to_a_dispatch_only_store(tmp_path, monkeypatch):
    """Reproduced (door/doctor_probe_BEFORE.txt): `levain doctor`'s host-sleep row opened the coordinator store
    read-write and added its tables to a consent dir that held only the dispatch store."""
    from levain import install
    from levain.doctor import _check_host_awake

    qdir = tmp_path / "consent"
    monkeypatch.setattr(install, "_consent_queue_dir", lambda: qdir)
    qdir.mkdir(mode=0o700)
    JobStore(qdir)._connect().close()

    def tables():
        con = sqlite3.connect(qdir / "dispatch.db")
        try:
            return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()

    before = tables()
    assert _check_host_awake() == []
    assert tables() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["consent"]


def _workspace(tmp_path):
    """A coordinator workspace as the coordinator install renders it: ``.levain/config.json`` records the role."""
    ws = tmp_path / "coordinator"
    (ws / ".levain").mkdir(parents=True)
    (ws / ".levain" / "config.json").write_text('{"role": "coordinator"}\n')
    (ws / "sub").mkdir()
    return ws


def _launched(qdir, sid):
    """A coordinator launch active under harness session ``sid`` in the store at ``qdir``."""
    from levain.coordinator.store import CoordStore

    s = CoordStore(qdir)
    ln = s.launch_request(s.coord_take(), harness="claude", role="coordinator")
    got, refused = s.launch_activate(ln.launch_id, harness_session_id=sid)
    assert refused is None and got.state == "active"


_RECORD = ["team", "record", "decision", "--kind", "ruling", "--owner", "ana", "--paths", "a.py", "--words", "w",
           "--no-push"]


def test_a_locked_store_does_not_open_the_door(tmp_path, monkeypatch, capsys):
    """Reproduced on d486af26 (door L3 r1, codex HIGH 3): with the store held under an exclusive lock, the launch
    lookup's sqlite error read as "not launched" and `team record` ran inside the coordinator's session."""
    from levain.cli import main

    lh = tmp_path / "lh"
    monkeypatch.setenv("LEVAIN_HOME", str(lh))
    qdir = lh / "consent"
    qdir.mkdir(parents=True, mode=0o700)
    _launched(qdir, "sess-locked")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-locked")
    monkeypatch.chdir(_workspace(tmp_path) / "sub")
    holder = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    try:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("CREATE TABLE IF NOT EXISTS door_lock_probe (x)")
        assert main(_RECORD) == 1
    finally:
        holder.close()
    assert "levain team record: `levain team record` is the operator's" in capsys.readouterr().err


def test_a_launch_in_another_store_is_doored_by_where_it_runs(tmp_path, monkeypatch, capsys):
    """Reproduced on d486af26 (door L3 r1, codex HIGH 1): a coordinator launched from a non-default store ran
    `team record` (which takes no --queue-dir), because the lookup read only the default store."""
    from levain.cli import main

    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    other = tmp_path / "q"
    other.mkdir(mode=0o700)
    _launched(other, "sess-other")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-other")
    monkeypatch.chdir(_workspace(tmp_path))
    assert main(_RECORD) == 1
    assert "levain team record: `levain team record` is the operator's" in capsys.readouterr().err
