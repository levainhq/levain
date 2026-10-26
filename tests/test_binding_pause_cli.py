"""``levain binding pause|resume <id>`` (autogrant arch r14, R14-9): ``BindingStore.set_status`` on the autonomic store
opened as it is, with no schema or format step, so it reaches a ``/2`` store with a run in flight (the store an
upgrade refuses). The busy store is ``tests/test_autonomic_gate_coverage.CWorld``'s: a COOLING_OFF binding whose
dispatch left its run admitted and its hold open."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

import levain.cli as cli
from levain.autonomic import BindingStatus, BindingStore, Posture
from levain.autonomic import db as vdb
from levain.autonomic import schema3
from levain.autonomic.binding import Binding, Guard, SubGoal, TriggerSpec
from tests.autonomic_test_only_confinement import test_only_confined_executor  # noqa: F401  (the root-run closure has no conftest)
from tests.test_autonomic_gate_coverage import LATER, CWorld, _covering
from tests.test_autonomic_journal_wiring import TIGHT

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")


def _no_upgrade(*_a, **_k):
    raise AssertionError("an upgrade step ran")


def _state(path) -> tuple:
    """(schema rows, format marker, every row of every table but bindings and fences, hashed)."""
    c = sqlite3.connect(path)
    try:
        schema = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        marker = c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0]
        h = hashlib.sha256()
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            if t not in ("bindings", "fences"):
                h.update(repr((t, c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall())).encode())
    finally:
        c.close()
    return schema, marker, h.hexdigest()


def _fence(path, binding_id) -> int:
    c = sqlite3.connect(path)
    try:
        row = c.execute("SELECT generation FROM fences WHERE binding_id = ?", (binding_id,)).fetchone()
    finally:
        c.close()
    return 0 if row is None else row[0]


@pytest.fixture
def busy(tmp_path):
    """A /2 store with a run in flight: the binding's run admitted, its hold open."""
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=frozenset())
    b = w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    path = tmp_path / "store" / vdb.DB_NAME
    c = sqlite3.connect(path)
    try:
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        assert c.execute("SELECT COUNT(*) FROM holds WHERE decided IS NULL").fetchone() == (1,)
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() == (vdb.STORE_FORMAT_V2,)
    finally:
        c.close()
    return w, b, path


def test_pause_and_resume_a_binding_on_a_busy_v2_store_with_no_upgrade_step(busy, monkeypatch, capsys):
    w, b, path = busy
    before, fence = _state(path), _fence(path, b.binding_id)
    monkeypatch.setattr(vdb.AutonomicDB, "_ensure_schema", staticmethod(_no_upgrade))
    store_dir = str(path.parent)
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", store_dir]) == 0
    assert capsys.readouterr().out == f"paused: {b.binding_id} (paused)\n"
    assert BindingStore(store_dir, upgrade=False).get(b.binding_id).status is BindingStatus.PAUSED
    assert _fence(path, b.binding_id) == fence + 1     # its admitted run is fenced, in the same transaction
    assert _state(path) == before                      # schema, marker and every other row as they were
    assert cli.main(["binding", "resume", b.binding_id, "--store-dir", store_dir]) == 0
    assert capsys.readouterr().out == f"resumed: {b.binding_id} (active)\n"
    assert BindingStore(store_dir, upgrade=False).get(b.binding_id).status is BindingStatus.ACTIVE
    assert _fence(path, b.binding_id) == fence + 1     # a resume does not fence
    assert _state(path) == before
    monkeypatch.undo()
    # the store V opens afterwards is the same /2 store: the binding reads ACTIVE there too
    assert BindingStore(store_dir).get(b.binding_id).status is BindingStatus.ACTIVE


def test_the_refusals_name_why_and_write_nothing(busy, tmp_path, capsys):
    w, b, path = busy
    store_dir = str(path.parent)
    before = _state(path)
    assert cli.main(["binding", "pause", "no-such-binding", "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err == f"pause refused: no binding 'no-such-binding' in {path.parent}\n"
    missing = tmp_path / "nowhere"
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", str(missing)]) == 1
    assert "pause refused: no autonomic store at" in capsys.readouterr().err
    assert not missing.exists()                        # no store is created
    BindingStore(store_dir, upgrade=False).set_status(b.binding_id, BindingStatus.REVOKED)
    assert cli.main(["binding", "resume", b.binding_id, "--store-dir", store_dir]) == 1
    assert "resume refused: illegal lifecycle transition revoked → active" in capsys.readouterr().err
    assert _state(path)[:2] == before[:2]


def test_a_store_of_another_format_is_refused_and_left_as_it_was(busy, capsys):
    w, b, path = busy
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("UPDATE meta SET value = ? WHERE key = 'format'", (vdb.STORE_FORMAT_V1,))
    c.close()
    before = _state(path)
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", str(path.parent)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("pause refused: store format 'levain-autonomic/1' is not one of") and "nothing" in err
    assert _state(path) == before


def test_pause_on_a_v3_store(tmp_path, capsys):
    st = schema3.AutonomicStore3(tmp_path / "store3")
    assert st.meta("format") == schema3.STORE_FORMAT_V3
    store = BindingStore(st.directory, upgrade=False)
    b = Binding.create(
        created_by="operator", created_at="2026-10-07T09:00:00g",
        trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
        goal=(SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
        posture=Posture.COOLING_OFF,
        guard=(Guard(rationale="spoofed sender", dissent_author="codex",
                     kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                     kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
        status=BindingStatus.ACTIVE)
    store.add(b)
    fp = schema3.fingerprint(sqlite3.connect(st.path))
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", str(st.directory)]) == 0
    assert capsys.readouterr().out == f"paused: {b.binding_id} (paused)\n"
    assert store.get(b.binding_id).status is BindingStatus.PAUSED
    assert schema3.fingerprint(sqlite3.connect(st.path)) == fp == schema3.expected_fingerprint()
    assert st.meta("format") == schema3.STORE_FORMAT_V3


@pytest.mark.parametrize("paused", [False, True])
def test_a_paused_binding_s_started_run_does_not_take_its_next_action(tmp_path, paused, capsys):
    # the help's claim: the silence default would fire this held action (paused=False); after a pause it does not
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=frozenset({"link0"}))
    b = w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    if paused:
        assert cli.main(["binding", "pause", b.binding_id, "--store-dir", str(tmp_path / "store")]) == 0
    w.sweep(LATER)
    assert w.outbox() == ([] if paused else ["link0"])
