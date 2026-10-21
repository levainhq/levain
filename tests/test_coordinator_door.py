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
