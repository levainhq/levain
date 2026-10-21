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


def test_a_recorded_workspace_stays_doored_when_its_own_config_is_gone_or_garbled(tmp_path, monkeypatch, capsys):
    """Reproduced (coordint/repro_r1_BEFORE.txt): a coordinator that deleted or garbled its own .levain/config.json
    (writable from inside a Codex turn) left the door: `coord take` ran and `team record` passed it."""
    from levain.cli import main
    from levain.coordinator import door

    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    ws = _workspace(tmp_path)
    door.record(ws)
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    monkeypatch.chdir(ws / "sub")
    config = ws / ".levain" / "config.json"
    for damage in (config.unlink, lambda: config.write_text('{"role": "coordi')):
        damage()
        assert main(["coord", "take", "--queue-dir", str(q)]) == 1
        assert main(_RECORD) == 1
        err = capsys.readouterr().err
        assert err.count("is the operator's, and this is the coordinator's workspace") == 2, err
    # a record that cannot be read shuts the door everywhere, the operator's own directories included
    monkeypatch.chdir(tmp_path)
    door.record_path().write_text("{")
    assert main(["coord", "take", "--queue-dir", str(q)]) == 1
    assert "the coordinator record cannot be read" in capsys.readouterr().err


def test_a_codex_coordinator_workspace_is_marked_by_the_one_role_render(tmp_path, capsys, monkeypatch):
    """Reproduced (door/repro_fix2_BEFORE.txt case 2): the install render refused the coordinator role for codex
    ("for the claude-code adapter only"), so a Codex coordinator's workspace carried no record and the door was open."""
    import json

    from levain import host_guard as hg
    from levain import install as inst
    from levain.coordinator import door
    from tests.test_init_answers import _filled

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    ws = tmp_path / "codex-coordinator"
    assert inst.run_init(ws, "codex", force=False, answers_file=answers, role="coordinator") == 0
    assert inst.persisted_role(ws) == "coordinator" and door.marked(ws)
    assert door.workspace(str(ws)) == ws
