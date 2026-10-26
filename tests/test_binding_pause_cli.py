"""``levain binding pause|resume|abandon <id>`` (autogrant arch r14, R14-9; rev75 O-75-6 and its CLI contract; arch r15,
R15-1): the autonomic store opened as it is, with no schema or format step, so the verbs reach a ``/2`` store with a run
in flight (the store an upgrade refuses). ``pause`` is ``BindingStore.set_status(PAUSED)``, ``resume`` is
``BindingStore.ratify``, ``abandon`` fences the binding's admitted runs and changes no status. The busy store is
``tests/test_autonomic_gate_coverage.CWorld``'s: a COOLING_OFF binding whose dispatch left its run admitted and its hold
open."""
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

KILL = Guard(rationale="spoofed sender", dissent_author="codex",
             kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
             kill_drill={"dmarc": "fail"}, kill_authored_by="operator")


def _no_upgrade(*_a, **_k):
    raise AssertionError("an upgrade step ran")


def _binding(posture: Posture, *, guard=(KILL,), one_shot: bool = False,
             status: BindingStatus = BindingStatus.PAUSED) -> Binding:
    return Binding.create(
        created_by="operator", created_at="2026-10-07T09:00:00g",
        trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
        goal=(SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
        posture=posture, guard=guard, one_shot=one_shot, status=status)


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


def _rows(path, table: str) -> list:
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    finally:
        c.close()


def _fence(path, binding_id) -> int:
    c = sqlite3.connect(path)
    try:
        row = c.execute("SELECT generation FROM fences WHERE binding_id = ?", (binding_id,)).fetchone()
    finally:
        c.close()
    return 0 if row is None else row[0]


def _paused(bid: str, n: int) -> str:
    return f"paused {bid}: its admitted runs are fenced (fence {n}); `levain binding resume {bid}` makes it fire again\n"


def _resumed(bid: str) -> str:
    return f"resumed {bid}: it fires on its next event (runs admitted before the pause stay fenced)\n"


def _abandoned(bid: str, n: int) -> str:
    return f"abandoned {bid}: its admitted runs are fenced (fence {n}); it does not fire again\n"


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


def _claimed_one_shot(tmp_path, auto_fire=frozenset()):
    """R15-1's case: a one-shot binding admitted (claimed: REVOKED with its claimed run), the run's hold open, /2."""
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=auto_fire)
    b = _binding(Posture.COOLING_OFF, one_shot=True)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.dispatch("m1")
    path = tmp_path / "store" / vdb.DB_NAME
    assert w.store.get(b.binding_id).status is BindingStatus.REVOKED
    [(run_id, bid, gen)] = [r[:3] for r in _rows(path, "runs")]
    assert bid == b.binding_id and w.store.claimed_run(b.binding_id) == run_id
    assert len(_rows(path, "holds")) == 1
    return w, b, path, run_id, gen


@pytest.fixture
def claimed_one_shot(tmp_path):
    return _claimed_one_shot(tmp_path)


def test_pause_and_resume_a_binding_on_a_busy_v2_store_with_no_upgrade_step(busy, monkeypatch, capsys):
    w, b, path = busy
    bid = b.binding_id
    before, fence = _state(path), _fence(path, bid)
    monkeypatch.setattr(vdb.AutonomicDB, "_ensure_schema", staticmethod(_no_upgrade))
    store_dir = str(path.parent)
    assert cli.main(["binding", "pause", bid, "--store-dir", store_dir]) == 0
    assert capsys.readouterr().out == _paused(bid, fence + 1)
    assert BindingStore(store_dir, upgrade=False).get(bid).status is BindingStatus.PAUSED
    assert _fence(path, bid) == fence + 1              # its admitted run is fenced, in the same transaction
    assert _state(path) == before                      # schema, marker and every other row as they were
    assert cli.main(["binding", "pause", bid, "--store-dir", store_dir]) == 0     # a second pause fences again
    assert capsys.readouterr().out == _paused(bid, fence + 2)
    assert cli.main(["binding", "resume", bid, "--store-dir", store_dir]) == 0
    assert capsys.readouterr().out == _resumed(bid)
    assert BindingStore(store_dir, upgrade=False).get(bid).status is BindingStatus.ACTIVE
    assert _fence(path, bid) == fence + 2              # a resume does not fence
    assert _state(path) == before
    monkeypatch.undo()
    # the store V opens afterwards is the same /2 store: the binding reads ACTIVE there too
    assert BindingStore(store_dir).get(bid).status is BindingStatus.ACTIVE


def test_resume_is_ratify_a_never_ratified_candidate_that_could_not_fire_is_refused(tmp_path, capsys):
    # O-75-6: a PAUSED confirm-class candidate with no sealed kill. set_status(ACTIVE) would take it ACTIVE (where
    # list_active then bars it); ratify refuses it, and so does resume
    store = BindingStore(tmp_path / "store")
    b = _binding(Posture.CONFIRM, guard=(Guard(rationale="might be wrong", dissent_author="codex"),))
    store.add(b)
    path = tmp_path / "store" / vdb.DB_NAME
    before = _rows(path, "bindings")
    assert cli.main(["binding", "resume", b.binding_id, "--store-dir", str(tmp_path / "store")]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith(f"not resumed: ratify refuses confirm-class binding {b.binding_id!r} with NO "
                              "sealed-floor kill")
    assert store.get(b.binding_id).status is BindingStatus.PAUSED
    assert _rows(path, "bindings") == before
    # the verb resume used to be: it takes the candidate ACTIVE, and the fire set then bars it
    assert store.set_status(b.binding_id, BindingStatus.ACTIVE)
    assert store.get(b.binding_id).status is BindingStatus.ACTIVE
    assert b.binding_id not in [x.binding_id for x in store.list_active()]


def test_the_refusals_name_why_and_write_nothing(busy, tmp_path, capsys):
    w, b, path = busy
    store_dir = str(path.parent)
    before = _state(path)
    assert cli.main(["binding", "pause", "bind-nope", "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err == "not paused: no binding bind-nope\n"
    assert cli.main(["binding", "resume", "bind-nope", "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err == "not resumed: no binding bind-nope\n"
    assert cli.main(["binding", "abandon", "bind-nope", "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err == "not abandoned: no binding bind-nope\n"
    missing = tmp_path / "nowhere"
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", str(missing)]) == 1
    assert capsys.readouterr().err.startswith("not paused: no autonomic store at")
    assert not missing.exists()                        # no store is created
    BindingStore(store_dir, upgrade=False).set_status(b.binding_id, BindingStatus.REVOKED)
    assert cli.main(["binding", "resume", b.binding_id, "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err.startswith(f"not resumed: ratify refuses a revoked binding {b.binding_id!r}")
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err.startswith("not paused: illegal lifecycle transition revoked → paused")
    assert _state(path)[:2] == before[:2]


@pytest.mark.parametrize("marker, why", [
    ("levain-autonomic/4", "not paused: store-newer-than-levain: store format 'levain-autonomic/4'; upgrade levain\n"),
    (vdb.STORE_FORMAT_V1, "not paused: store format 'levain-autonomic/1' is not 'levain-autonomic/2' or "
                          "'levain-autonomic/3': nothing is written to it\n"),
])
def test_a_store_of_another_format_is_refused_and_left_as_it_was(busy, capsys, marker, why):
    w, b, path = busy
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("UPDATE meta SET value = ? WHERE key = 'format'", (marker,))
    c.close()
    before, bindings, fence = _state(path), _rows(path, "bindings"), _fence(path, b.binding_id)
    for verb in ("pause", "abandon"):
        assert cli.main(["binding", verb, b.binding_id, "--store-dir", str(path.parent)]) == 1
        assert capsys.readouterr().err == why.replace("not paused", f"not {verb}ed" if verb == "abandon" else "not paused")
    assert (_state(path), _rows(path, "bindings"), _fence(path, b.binding_id)) == (before, bindings, fence)


def _v3_store_with(tmp_path) -> tuple:
    st = schema3.AutonomicStore3(tmp_path / "store3")
    assert st.meta("format") == schema3.STORE_FORMAT_V3
    store = BindingStore(st.directory, upgrade=False)
    b = _binding(Posture.COOLING_OFF, status=BindingStatus.ACTIVE)
    store.add(b)
    return st, store, b


def test_pause_on_a_v3_store(tmp_path, capsys):
    st, store, b = _v3_store_with(tmp_path)
    fp = schema3.fingerprint(sqlite3.connect(st.path))
    assert cli.main(["binding", "pause", b.binding_id, "--store-dir", str(st.directory)]) == 0
    assert capsys.readouterr().out == _paused(b.binding_id, 1)
    assert store.get(b.binding_id).status is BindingStatus.PAUSED
    assert schema3.fingerprint(sqlite3.connect(st.path)) == fp == schema3.expected_fingerprint()
    assert st.meta("format") == schema3.STORE_FORMAT_V3


def test_a_v3_store_whose_ddl_is_not_this_levain_s_is_refused_as_the_full_opener_refuses_it(tmp_path, capsys):
    st, store, b = _v3_store_with(tmp_path)
    c = sqlite3.connect(st.path, isolation_level=None)
    c.execute("DROP TRIGGER fences_d")                 # a tampered /3 store: marker /3, DDL not this code's
    c.close()
    with pytest.raises(schema3.StoreSchemaMismatch):
        schema3.AutonomicStore3(st.directory).meta("format")   # the full /3 opener's refusal
    bindings, fence = _rows(st.path, "bindings"), _fence(st.path, b.binding_id)
    for verb, word in (("pause", "paused"), ("resume", "resumed"), ("abandon", "abandoned")):
        assert cli.main(["binding", verb, b.binding_id, "--store-dir", str(st.directory)]) == 1
        assert capsys.readouterr().err == (f"not {word}: store-schema-mismatch: the store's DDL is not this levain's "
                                           f"({schema3.STORE_FORMAT_V3}); it is not rewritten\n")
    assert (_rows(st.path, "bindings"), _fence(st.path, b.binding_id)) == (bindings, fence)


def test_a_claimed_one_shot_is_not_paused_and_is_abandoned_with_its_status_unchanged(claimed_one_shot, monkeypatch,
                                                                                       capsys):
    # R15-1: the claimed one-shot is REVOKED at admission with its run unfenced; pause refuses that transition
    w, b, path, run_id, gen = claimed_one_shot
    bid, store_dir = b.binding_id, str(path.parent)
    assert _fence(path, bid) == gen
    before_state, before_bindings = _state(path), _rows(path, "bindings")
    assert cli.main(["binding", "pause", bid, "--store-dir", store_dir]) == 1
    assert capsys.readouterr().err.startswith("not paused: illegal lifecycle transition revoked → paused")
    assert _fence(path, bid) == gen                    # the run is still unfenced
    monkeypatch.setattr(vdb.AutonomicDB, "_ensure_schema", staticmethod(_no_upgrade))
    assert cli.main(["binding", "abandon", bid, "--store-dir", store_dir]) == 0
    assert capsys.readouterr().out == _abandoned(bid, gen + 1)
    assert _fence(path, bid) == gen + 1 > gen          # the claimed run was admitted below the fence: it is fenced
    assert _rows(path, "bindings") == before_bindings  # no status change (nor any other change to the record)
    assert _state(path) == before_state
    assert cli.main(["binding", "abandon", bid, "--store-dir", store_dir]) == 0    # idempotent: it fences again
    assert capsys.readouterr().out == _abandoned(bid, gen + 2)
    assert _rows(path, "bindings") == before_bindings
    monkeypatch.undo()
    assert BindingStore(store_dir).get(bid).status is BindingStatus.REVOKED
    assert BindingStore(store_dir).claimed_run(bid) == run_id


def test_abandon_refuses_a_live_binding_because_it_would_fire_again(busy, capsys):
    # an ACTIVE or PAUSED binding fires again (on its next event, or after a resume): "it does not fire again" would be
    # false, so abandon names pause and writes nothing
    w, b, path = busy
    bid = b.binding_id
    bindings, fence = _rows(path, "bindings"), _fence(path, bid)
    for status in ("active", "paused"):
        if status == "paused":
            assert cli.main(["binding", "pause", bid, "--store-dir", str(path.parent)]) == 0
            capsys.readouterr()
            bindings, fence = _rows(path, "bindings"), _fence(path, bid)
        assert cli.main(["binding", "abandon", bid, "--store-dir", str(path.parent)]) == 1
        assert capsys.readouterr().err == (f"not abandoned: binding {bid} is {status}, so it can fire again; "
                                           f"`levain binding pause {bid}` fences its runs\n")
        assert (_rows(path, "bindings"), _fence(path, bid)) == (bindings, fence)


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


@pytest.mark.parametrize("abandoned", [False, True])
def test_an_abandoned_one_shot_s_claimed_run_does_not_take_its_next_action(tmp_path, abandoned, capsys):
    # abandon's claim on the run R15-1 names: unabandoned, the silence default fires the claimed run's held action
    w, b, path, run_id, gen = _claimed_one_shot(tmp_path, frozenset({"link0"}))
    if abandoned:
        assert cli.main(["binding", "abandon", b.binding_id, "--store-dir", str(path.parent)]) == 0
    w.sweep(LATER)
    assert w.outbox() == ([] if abandoned else ["link0"])


# ---- A1c1 L3 round 1 (levain project_memory 1026-22/a1c1/L3_r1.txt, L3_r1_deepseek.txt) ------------------------------
def _sidecars(path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name != path.name)


@pytest.mark.parametrize("ddl, why", [
    (["CREATE TABLE foo (x)"], "not paused: store format None is not"),
    (["CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
      "INSERT INTO meta VALUES ('format', 'other/1')"], "not paused: store format 'other/1' is not"),
    # A1-R3 (deepseek): a /2 marker over a database without the tables a pause writes
    (["CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
      f"INSERT INTO meta VALUES ('format', '{vdb.STORE_FORMAT_V2}')", "CREATE TABLE bindings (binding_id TEXT, "
      "record TEXT, seq INTEGER)"], "not paused: the 'levain-autonomic/2' store has no fences table"),
])
def test_a_foreign_database_is_refused_before_its_journal_mode_or_files_change(tmp_path, capsys, ddl, why):
    # A1-R3 (codex): the durable opener switches a database to WAL; a refused store must keep its rollback journal
    path = tmp_path / "store" / vdb.DB_NAME
    path.parent.mkdir()
    c = sqlite3.connect(path, isolation_level=None)
    for st in ddl:
        c.execute(st)
    assert c.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    c.close()
    before = path.read_bytes()
    for verb in ("pause", "abandon", "resume"):
        assert cli.main(["binding", verb, "bind-x", "--store-dir", str(path.parent)]) == 1
        word = {"pause": "paused", "abandon": "abandoned", "resume": "resumed"}[verb]
        assert capsys.readouterr().err.startswith(why.replace("not paused", f"not {word}"))
    with pytest.raises(vdb.StoreFormatError):
        schema3.AutonomicStore3(path.parent).meta("format")
    assert path.read_bytes() == before                 # the header's journal-mode bytes included
    assert _sidecars(path) == []                       # no -wal, no -shm, no -journal


def test_abandon_accepts_exactly_the_inert_statuses(tmp_path):
    # deepseek LOW: abandon is the remedy for the statuses set_status cannot pause; pinned by name, every status
    assert {st for st in BindingStatus if st.is_inert} == {BindingStatus.REVOKED, BindingStatus.EXPIRED}
    for st in BindingStatus:
        store = BindingStore(tmp_path / st.value)
        b = _binding(Posture.COOLING_OFF)
        store.add(b)
        if st is not BindingStatus.PAUSED:
            store.set_status(b.binding_id, st)
        assert store.get(b.binding_id).status is st
        if st in (BindingStatus.REVOKED, BindingStatus.EXPIRED):
            fence = _fence(tmp_path / st.value / vdb.DB_NAME, b.binding_id)
            assert store.abandon(b.binding_id) == fence + 1
        else:
            with pytest.raises(ValueError, match="so it can fire again"):
                store.abandon(b.binding_id)


def test_an_as_is_store_takes_no_run_journal(tmp_path):
    from levain.autonomic.journal import RunJournal

    with pytest.raises(ValueError, match="upgrade=False opens the store as it is"):
        BindingStore(tmp_path / "store", upgrade=False, journal=RunJournal(tmp_path / "store"))


def test_a_corrupt_registry_record_is_the_one_line_refusal(busy, capsys):
    # complement LOW 4: _read_raw(for_mutation=True) on a corrupt registry raises ValueError (measured by lane a1c1d
    # over not-JSON, a list, an unhashable or unknown status, a broken guard, a 100000-deep array), which the verbs print
    w, b, path = busy
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("UPDATE bindings SET record = '{nope' WHERE binding_id = ?", (b.binding_id,))
    c.close()
    for verb, word in (("pause", "paused"), ("resume", "resumed"), ("abandon", "abandoned")):
        assert cli.main(["binding", verb, b.binding_id, "--store-dir", str(path.parent)]) == 1
        err = capsys.readouterr().err
        assert err.startswith(f"not {word}: binding store ") and "a record is not JSON" in err
        assert err.count("\n") == 1
