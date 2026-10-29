"""R15-5 S3: the callers of a ``levain-autonomic/3`` store's upkeep (levain/autonomic/upkeep3.py) and doctor's read of
it (levain project_memory 1027-11/RULINGS.md "R15-5", 1027-11/r155scout/SCOUT.md W8, W19):
  - the projection of the store's receipts to the receipt file, marked only for the lines the file holds;
  - the /3-wired liveness call passes the store's not_started set (R2-4);
  - the reaper over a real lease: an owner SIGKILLed inside ``fn`` leaves an intent with no end, and the reaper closes
    it ``unknown`` with an ``outcome_unknown`` receipt, while a live owner's attempt is left in flight;
  - doctor shows every INVARIANT row, A1's second row (``held_at_bound_unclosed``) among them."""
from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from levain.autonomic import schema3 as s
from levain.autonomic import upkeep3
from levain.autonomic.journal import (EffectNotStarted, EffectStatus, Receipt, RunJournal, V3Fire, run_seal,
                                      unprojected_receipts)
from levain.autonomic.liveness import gate_liveness
from levain.autonomic.posture import Posture
from levain.autonomic.store import GateReceiptStore, gate_record
from levain import doctor


def _receipt(subject: str, terminal_state: str, *, guard=None, grant_guard=None) -> Receipt:
    # a complete gate record (GateReceiptStore.read needs every _GATE_KEYS field); the nonce keeps every id fresh
    rid, record = gate_record(created_at="2026-10-27T12:00:00+00:00", action_name="send", proposal_id=None,
                              posture=Posture.ON_LOOP, fired=terminal_state == "fired",
                              action_face={"terminal_state": terminal_state, "guard": guard,
                                           "grant_guard": grant_guard, "nonce": time.monotonic_ns()},
                              subject=subject)
    return Receipt(rid, record)


def _fire(run_id: str) -> V3Fire:
    return V3Fire(by="on-loop", posture="ON_LOOP",
                  end_receipt=lambda e: _receipt(e.subject, e.terminal_state, guard=e.seal["floor"] or None,
                                                 grant_guard=e.seal["grant_guard"] or None),
                  run_receipt=lambda reason: _receipt(f"run:{run_id}", "refused"))


def _effect(j: RunJournal, fn, run_id="r1", effect_id="link-0"):
    return j.effect(run_id, effect_id, digest="d", fence="f", fence_now=lambda c: "f", fn=fn, needs_decision=False,
                    authorize=None, v3=_fire(run_id))


def _not_yet():
    raise EffectNotStarted("spawn refused")


@pytest.fixture
def j(tmp_path):
    journal = RunJournal(s.AutonomicStore3(tmp_path / "store"))
    journal.start("r1", binding_id="b", seal=run_seal(confirm_class=False, guard=[], links=1))
    return journal


def _unprojected(j):
    with j.db.read() as c:
        return [rid for rid, _ in unprojected_receipts(c)]


# ---- the projector ---------------------------------------------------------------------------------------------------
def test_project_copies_every_committed_receipt_to_the_file_and_marks_it(j, tmp_path):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    assert _effect(j, lambda: "sent").status is EffectStatus.DONE
    ids = _unprojected(j)
    assert len(ids) == 2
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    assert upkeep3.project(j, trace) == ids
    assert _unprojected(j) == []
    assert sorted(r.id for r in trace.read()) == sorted(ids)
    assert upkeep3.project(j, trace) == []                       # nothing left: the file is not appended again
    assert len((tmp_path / "receipts.jsonl").read_text().splitlines()) == 2


def test_project_leaves_unmarked_a_receipt_the_file_holds_another_record_for(j, tmp_path):
    assert _effect(j, lambda: "sent").status is EffectStatus.DONE
    [rid] = _unprojected(j)
    path = tmp_path / "receipts.jsonl"
    other = {"id": rid, "kind": "gate", "created_at": "x", "action_name": "other", "proposal_id": None,
             "posture": "ON_LOOP", "fired": True, "action_face": {}}
    path.write_text(json.dumps(other) + "\n")
    assert upkeep3.project(j, GateReceiptStore(path)) == []
    assert _unprojected(j) == [rid]                              # doctor reads it unprojected


def test_the_upkeep_callers_refuse_a_v2_journal(tmp_path):
    v2 = RunJournal(tmp_path / "v2")
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    for call in (lambda: upkeep3.project(v2, trace), lambda: upkeep3.gate_liveness(v2, trace),
                 lambda: upkeep3.reap(v2)):
        with pytest.raises(ValueError, match="levain-autonomic/3"):
            call()


# ---- R2-4: the /3-wired liveness call passes the not_started set -----------------------------------------------------
def test_the_v3_wired_liveness_call_passes_the_not_started_set(j, tmp_path):
    assert _effect(j, _not_yet).status is EffectStatus.HELD      # attempt 1 ends not_started (receipt: refused)
    assert _effect(j, lambda: "sent").status is EffectStatus.DONE  # attempt 2 fires
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    stats = upkeep3.gate_liveness(j, trace)
    assert stats["total"] == 2 and stats["fired"] == 1
    assert stats["by_terminal_state"] == {"not_started": 1, "fired": 1}
    # the same trace without the set: the not_started receipt counts as refused (R2-4's silent misreport)
    assert gate_liveness(trace)["by_terminal_state"] == {"refused": 1, "fired": 1}


def test_the_v3_wired_liveness_call_reads_the_set_after_the_trace(j, tmp_path, monkeypatch):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    order = []
    read = trace.read
    monkeypatch.setattr(trace, "read", lambda **kw: order.append("trace") or read(**kw))
    real = s.not_started_receipts
    monkeypatch.setattr(s, "not_started_receipts", lambda c: order.append("set") or real(c))
    assert upkeep3.gate_liveness(j, trace)["by_terminal_state"] == {"not_started": 1}
    assert order == ["trace", "set"]


def test_a_failed_projection_does_not_stop_the_liveness_read(j, tmp_path, monkeypatch):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")

    def full(rows):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(trace, "project_rows", full)
    assert upkeep3.gate_liveness(j, trace)["total"] == 0          # the file is as it was: empty
    assert len(_unprojected(j)) == 1


# ---- the reaper over a real lease, after a real crash ----------------------------------------------------------------
_CHILD = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, {tests!r})
    from test_autonomic_upkeep3 import _effect
    from levain.autonomic import schema3 as s
    from levain.autonomic.journal import RunJournal
    root = Path({root!r})
    j = RunJournal(s.AutonomicStore3(root / "store"))

    def fn():
        (root / "inside").write_text("1")
        time.sleep(60)
        return "sent"
    _effect(j, fn)
""")


def _crash_inside_fn(root: Path, child: str = _CHILD) -> RunJournal:
    """A child process records link-0's intent and is SIGKILLed inside ``fn``, before its end is written. While it
    is inside, the reaper leaves the attempt in flight. A child that exits on its own before the kill fails the
    helper with the child's own stderr, whatever else failed first."""
    p = subprocess.Popen([sys.executable, "-c", child.format(tests=str(Path(__file__).parent), root=str(root))],
                         env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    j = RunJournal(s.AutonomicStore3(root / "store"))
    try:
        deadline = time.monotonic() + 30
        while not (root / "inside").exists():
            assert p.poll() is None, p.communicate()[1].decode()
            assert time.monotonic() < deadline, "the child never reached fn"
            time.sleep(0.02)
        assert j._lease_held("r1", "link-0")
        assert upkeep3.reap(j) == {"effect:r1:link-0": s.IN_FLIGHT}
    finally:
        p.send_signal(signal.SIGKILL)          # a no-op on a child already reaped by poll()
        rc = p.wait(timeout=10)
        err = p.communicate()[1].decode()
        assert rc == -signal.SIGKILL, f"the child exited on its own ({rc}) before the kill:\n{err}"
    return j


def test_a_crash_between_fn_and_its_end_is_closed_unknown_by_the_reaper(j, tmp_path):
    j = _crash_inside_fn(tmp_path)
    with j.db.read() as c:
        assert c.execute("SELECT attempt, state, receipt_id FROM effects").fetchall() == [(1, "intent", None)]
        assert s.invariant(c) == []
    assert not j._lease_held("r1", "link-0") and j.poisoned() == [("r1", "link-0")]
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    reaped, projected = upkeep3.tick(j, trace)
    assert reaped == {"effect:r1:link-0": s.CLOSED}
    with j.db.read() as c:
        [(state, rid)] = c.execute("SELECT state, receipt_id FROM effects").fetchall()
        assert state == "unknown" and s.invariant(c) == []
        record = json.loads(c.execute("SELECT record FROM gate_receipts WHERE receipt_id = ?", (rid,)).fetchone()[0])
    assert projected == [rid]
    face = record["action_face"]
    assert (record["subject"], record["fired"], record["action_name"], record["posture"]) == \
        ("effect:r1:link-0", False, upkeep3.REAPER_ACTION, "ON_LOOP")
    assert face["terminal_state"] == "outcome_unknown" and face["gate"] == {"verdict": "auto", "by": "on-loop",
                                                                            "binding_id": "b"}
    assert [r.id for r in trace.read()] == [rid]
    assert upkeep3.reap(j) == {}                                 # a second pass finds nothing
    ran = []
    again = _effect(j, lambda: ran.append(1) or "sent")          # the redelivery never runs it again
    assert again.status is EffectStatus.POISONED and ran == []


def test_the_reaper_closes_a_crashed_attempt_with_the_guard_its_run_sealed(tmp_path):
    guard = [{"rationale": "spoofed sender", "kill_predicate": {"op": "==", "field": "dmarc", "value": "fail"},
              "kill_drill": {"dmarc": "fail"}}]
    j = RunJournal(s.AutonomicStore3(tmp_path / "store"))
    j.start("r1", binding_id="b", seal=run_seal(confirm_class=False, guard=guard, links=1))
    j = _crash_inside_fn(tmp_path)
    assert upkeep3.reap(j) == {"effect:r1:link-0": s.CLOSED}   # receipts_carry_guard admitted the reaper's receipt
    with j.db.read() as c:
        record = json.loads(c.execute("SELECT record FROM gate_receipts").fetchone()[0])
    assert record["action_face"]["guard"] == guard


# ---- doctor: the store's INVARIANT and census --------------------------------------------------------------------------
def _held_at_bound(j: RunJournal) -> None:
    # a held count driven to its bound by attempt-less faults, with no close written (op21b OBS-6)
    with j.db.write() as c:
        for _ in range(s.held_limit(c)):
            s.count_held(c, "request:r1:link-0", "subject:unavailable")


def test_doctor_shows_a_held_count_at_the_bound_as_an_invariant_row(j, tmp_path):
    _held_at_bound(j)
    rows = doctor._check_autonomic_store(tmp_path / "store")
    assert [(r.name, r.ok) for r in rows] == [("autonomic store INVARIANT", False)]
    assert rows[0].detail.startswith("held_at_bound_unclosed request:r1:link-0: ")
    with j.db.write() as c:                                      # the run's close clears it
        s.close_subject(c, subject="run:r1", record=_receipt("run:r1", "refused").record,
                        reason="held_limit:subject:unavailable")
    [ok] = doctor._check_autonomic_store(tmp_path / "store")
    assert ok.ok and not ok.warn and "invariant holds; 0 legacy row(s); 1 receipt(s) not yet in the receipt file" \
        in ok.detail


def _bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def test_doctor_reads_the_store_without_writing_it(j, tmp_path):
    """The store is in WAL mode, so a write lands in ``-wal`` first: a connection held open keeps the frames the
    journal wrote there (no checkpoint on its close), and doctor's read leaves both the db and those frames as they
    were. ``-shm`` is the readers' own index (a reader writes its read mark there), so it is not compared."""
    import sqlite3
    db = tmp_path / "store" / "autonomic.db"
    wal = db.with_name(db.name + "-wal")
    keeper = sqlite3.connect(db)
    try:
        keeper.execute("SELECT 1 FROM meta").fetchall()
        _held_at_bound(j)
        before = (_bytes(db), _bytes(wal))
        assert before[1], "the journal's frames are in the -wal while a connection stays open"
        assert [r.ok for r in doctor._check_autonomic_store(tmp_path / "store")] == [False]
        assert (_bytes(db), _bytes(wal)) == before
    finally:
        keeper.close()


def test_doctor_has_no_autonomic_row_without_a_v3_store(tmp_path):
    assert doctor._check_autonomic_store(tmp_path / "none") == []
    RunJournal(tmp_path / "v2").start("r1", binding_id="b")      # a /2 store: the binding commands report it
    assert doctor._check_autonomic_store(tmp_path / "v2") == []


def test_doctor_fails_a_v3_store_whose_schema_is_not_this_levains(j, tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / "store" / "autonomic.db", isolation_level=None)
    c.execute("CREATE TABLE stray (x)")
    c.close()
    [row] = doctor._check_autonomic_store(tmp_path / "store")
    assert not row.ok and "store-schema-mismatch" in row.detail


# ---- r155s3fx: fix round 1 (levain project_memory 1027-19/r155s3fx/BRIEF.md F1-F7) ------------------------------------
_CHILD_EXITS = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    sys.stderr.write("CHILD-BOOM: the child failed on its own\\n")
    sys.stderr.flush()
    (Path({root!r}) / "inside").write_text("1")
    os._exit(3)  # {tests!r}
""")


def test_the_crash_helper_surfaces_a_child_that_exited_before_the_kill(tmp_path):
    with pytest.raises(AssertionError, match="CHILD-BOOM"):
        _crash_inside_fn(tmp_path, child=_CHILD_EXITS)


def test_doctor_reads_this_users_store_and_says_so(j, tmp_path):
    """F1: the store a user's autonomic commands write is per user (``default_store_dir``: ``$LEVAIN_HOME/autonomic``,
    the default of every ``--store-dir``), not per install, so doctor reads that one and its row says whose it is."""
    from levain.autonomic.db import default_store_dir
    home = default_store_dir()
    RunJournal(s.AutonomicStore3(home)).start("r1", binding_id="b",
                                              seal=run_seal(confirm_class=False, guard=[], links=1))
    [row] = doctor._check_autonomic_store()
    assert row.ok and str(home) in row.detail and "this user's store ($LEVAIN_HOME/autonomic)" in row.detail


def test_a_doctor_test_never_reads_the_operators_real_store():
    """F1: every test runs with its own ``$LEVAIN_HOME`` (tests/conftest.py ``_isolate_levain_home``), so a
    ``_run_doctor`` test reads a scratch store, never the account's ``~/.levain``."""
    import pwd
    from levain.autonomic.db import default_store_dir
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".levain"
    assert os.environ.get("LEVAIN_HOME")
    assert not default_store_dir().is_relative_to(real)


def _plain_db(path: Path, marker: str | None) -> Path:
    import sqlite3
    path.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path / "autonomic.db", isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS bindings (x)")
    if marker is not None:
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO meta VALUES ('format', ?)", (marker,))
    c.close()
    return path


@pytest.mark.parametrize("marker, says", [
    ("levain-autonomic/4", "store-newer-than-levain"),
    (None, "no store format marker"),
    ("someone-else/1", "is not a levain-autonomic format"),
])
def test_doctor_fails_a_store_of_a_format_it_cannot_read(tmp_path, marker, says):
    """F2: a store doctor cannot read as /3, /2 or /1 gets a FAIL row, worded by refuse_unless_this_v3's refusal."""
    [row] = doctor._check_autonomic_store(_plain_db(tmp_path / "store", marker))
    assert not row.ok and not row.warn and says in row.detail


@pytest.mark.parametrize("marker", ["levain-autonomic/1", "levain-autonomic/2"])
def test_doctor_has_no_row_for_a_v1_or_v2_store(tmp_path, marker):
    assert doctor._check_autonomic_store(_plain_db(tmp_path / "store", marker)) == []


def test_doctor_has_no_row_for_an_empty_database(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "autonomic.db").write_bytes(b"")
    assert doctor._check_autonomic_store(tmp_path / "store") == []


def test_a_failed_not_started_read_is_reported_never_raised(j, tmp_path, monkeypatch):
    """F3: the liveness read keeps going without the set, and its result names the failed read: the receipts the set
    would have named count as their receipts say (``refused``)."""
    import sqlite3
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(s, "not_started_receipts", locked)
    stats = upkeep3.gate_liveness(j, trace)
    assert stats["by_terminal_state"] == {"refused": 1}
    assert stats["not_started_unread"].endswith("database is locked")   # the journal's own error type wraps it


def test_a_liveness_read_whose_set_reads_has_no_unread_key(j, tmp_path):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    assert "not_started_unread" not in upkeep3.gate_liveness(j, GateReceiptStore(tmp_path / "r.jsonl"))


@pytest.mark.parametrize("decision, verdict", [
    ({"generation": 1, "by": "human", "posture": "COOLING_OFF"}, "approved"),
    ({"generation": 1, "by": "on-loop", "posture": "COOLING_OFF"}, "auto"),
    (None, "auto"),
    ("legacy", "auto"),
])
def test_a_reaped_effect_receipts_verdict_is_its_sealed_deciders(j, decision, verdict):
    """F4: ``approved`` only for a decision a human made, as the gate's own fire of a journaled hold words it
    (levain/autonomic/gate.py ``verdict = "approved" if decider == "human" else "auto"``): a cooling-off hold approved
    by its silence (``on-loop``) is ``auto``."""
    seal = {"floor": [], "grant_guard": [], "by": "binding", "posture": "COOLING_OFF", "grant": None, "cover": None}
    if decision is not None:
        seal["decision"] = decision
    record = upkeep3.reaper_record(j, "2026-10-27T12:00:00+00:00")("effect:r1:link-0", json.dumps(seal),
                                                                   "outcome_unknown", None)
    assert record["action_face"]["gate"]["verdict"] == verdict


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_doctor_fails_a_store_it_cannot_open(j, tmp_path):
    """F5: an existing store doctor cannot read is a FAIL: unreadable cannot be told from corrupt."""
    db = tmp_path / "store" / "autonomic.db"
    db.chmod(0)
    try:
        [row] = doctor._check_autonomic_store(tmp_path / "store")
    finally:
        db.chmod(0o600)
    assert not row.ok and not row.warn and "cannot be read" in row.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a mode-000 directory")
def test_doctor_fails_rather_than_aborts_when_the_store_directory_is_not_searchable(j, tmp_path):
    """The existence probe is inside the guarded read: a store directory this user cannot search is a FAIL row."""
    store = tmp_path / "store"
    store.chmod(0)
    try:
        [row] = doctor._check_autonomic_store(store)
    finally:
        store.chmod(0o700)
    assert not row.ok and not row.warn and "cannot be read" in row.detail


def test_doctor_has_no_row_for_a_missing_store(tmp_path):
    assert doctor._check_autonomic_store(tmp_path / "nowhere") == []


def test_doctor_fails_rather_than_aborts_when_the_invariant_read_raises(j, tmp_path, monkeypatch):
    def broken(conn):
        raise ValueError("a row the query cannot read")
    monkeypatch.setattr(s, "invariant", broken)
    [row] = doctor._check_autonomic_store(tmp_path / "store")
    assert not row.ok and not row.warn and "ValueError: a row the query cannot read" in row.detail


def test_doctor_fails_a_corrupt_store(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "autonomic.db").write_bytes(b"not a database" * 512)
    [row] = doctor._check_autonomic_store(tmp_path / "store")
    assert not row.ok and not row.warn and "cannot be read" in row.detail


# F7: a /3 manual pending has no owner lease (upkeep3._pending_owner_unknown says every firing pending is in flight).
# That is safe only while nothing writes one: this tripwire fails when a module under levain/ gains a string that
# writes the table, so the pending lease is built with the first writer.
_PENDING_WRITE = re.compile(r"\b(?:INSERT|REPLACE)\b[^;]*?\bINTO\s+manual_pendings\b[^;]*|"
                            r"\bUPDATE\s+manual_pendings\b[^;]*", re.I)
# Every statement under levain/ that writes manual_pendings, by module and exact (whitespace-normalised) text: a new
# one anywhere, schema3.py included, fails the tripwire until its pending lease is built and it is named here.
_PENDING_WRITES_KNOWN = {
    ("schema3.py", "UPDATE manual_pendings SET state = 'terminal', receipt_id = NEW.receipt_id "
                   "WHERE NEW.subject = 'pending:' || pending_id AND state IN ('open', 'firing')"),
    ("schema3.py", "UPDATE manual_pendings SET state = 'terminal', receipt_id = ? WHERE pending_id = ? "
                   "AND state IN ('open', 'firing')"),
}


def _string_texts(tree: ast.AST):
    """Every string in ``tree`` as ``(line, text)``: a str constant, and an f-string with its constant parts joined and
    each substitution as ``{}``. Adjacent literals are one constant to the parser, so a split statement is whole."""
    inside_fstring = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inside_fstring.update(id(c) for c in ast.walk(node) if isinstance(c, ast.Constant))
            parts = [v.value if isinstance(v, ast.Constant) else "{}" for v in node.values]
            yield node.lineno, "".join(parts)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inside_fstring:
            yield node.lineno, node.value


def _pending_writers(root: Path) -> list[str]:
    """Each INSERT, REPLACE or UPDATE of ``manual_pendings`` in a string under ``root`` that is not in
    ``_PENDING_WRITES_KNOWN``, as ``file:line: text``."""
    out = []
    for path in sorted(root.rglob("*.py")):
        for line, text in _string_texts(ast.parse(path.read_text())):
            for m in _PENDING_WRITE.finditer(text):
                stmt = " ".join(m.group(0).split())
                if (path.name, stmt) not in _PENDING_WRITES_KNOWN:
                    out.append(f"{path.relative_to(root)}:{line}: {stmt[:120]}")
    return out


def test_no_module_writes_a_manual_pending_without_its_lease():
    root = Path(__file__).resolve().parent.parent / "levain"
    assert _pending_writers(root) == [], (
        "a module writes /3 manual_pendings rows: build the pending lease the reaper tests "
        "(upkeep3._pending_owner_unknown) with it (levain project_memory 1027-19/RULINGS.md, R15-5 S3 O3)")


def test_the_pending_tripwire_catches_a_writer(tmp_path):
    (tmp_path / "schema3.py").write_text('X = "update manual_pendings SET state = 1"\n'
                                         'Y = "INSERT OR IGNORE INTO manual_pendings (pending_id) VALUES (?)"\n'
                                         'Z = "UPDATE manual_pendings SET state = \'terminal\', receipt_id = ? '
                                         'WHERE pending_id = ? " "AND state IN (\'open\', \'firing\')"\n')
    (tmp_path / "other.py").write_text('# manual_pendings in a comment is not a string\n'
                                       'A = "SELECT 1 FROM manual_pendings"\n'
                                       'B = "INSERT INTO manual_pendings VALUES (?)"\n'
                                       'C = "manual_pendings"\n'
                                       'D = "SELECT 1; UPDATE manual_pendings SET x = 1"\n'
                                       'n = 1\n'
                                       'E = f"INSERT INTO manual_pendings VALUES ({n})"\n'
                                       'F = f"replace into {n} manual_pendings" "x"\n'
                                       'G = f"REPLACE INTO manual_pendings (a) VALUES (?)" " -- {n}"\n'
                                       'H = "INSERT INTO " "manual_pendings VALUES (1)"\n')
    found = [f.split(": ", 1)[0] for f in _pending_writers(tmp_path)]
    assert found == ["other.py:3", "other.py:5", "other.py:7", "other.py:9", "other.py:10", "schema3.py:1",
                     "schema3.py:2"]
