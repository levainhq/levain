"""P5c-1b: the agent branch of ``RunJournal._barrier`` and the run proof (A0 section 2.3; BUILD_PLAN P5c-1).

Every store here is a real ``/2`` journal holding the ``registry`` meta row and the real agent tables, with a real
``launched`` occurrence row; the fence is the graduation cover of a real authority log (E2c's, as in A0 section 1.4).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3

import pytest

from levain.agents.controller import run_id as controller_run_id
from levain.autonomic import binding, confirm
from levain.autonomic.coverage import COVER_MOVED, COVER_NONE
from levain.autonomic.journal import EffectStatus, RunJournal
from levain.graduation_policy import AuthorityCoverage

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import GP, STORE, World, grad  # noqa: E402
from tests.test_graduation_policy import APPEND, root_dir  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

AGENT, KIND, KEY = "agent-e2c", "manual", "k1"
RUN = controller_run_id(AGENT, KIND, KEY)
BINDING = "bnd-not-in-bindings"


class _CoverMoved(Exception):
    pass


def _go(j, eid, ran=None, run=None):
    """An effect with a fence that always holds: these tests are about the barrier, not the fence."""
    return j.effect(run or RUN, eid, digest="d" + eid, fence="f", fence_now=lambda conn: "f",
                    fn=lambda: ran.append(eid) or "ok" if ran is not None else "ok")


def _journal(tmp_path, *, launched: bool = True, agent: object = AGENT, key: str = KEY,
             run: str = RUN) -> RunJournal:
    """A registry-bearing ``/2`` store (what ``binding.py`` makes on any store that ever held a Binding) with one
    agent occurrence; ``launched`` writes the real ``admitted -> launched`` update."""
    j = RunJournal(tmp_path / "autonomic")
    with j.db.write() as conn:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('registry', '1')")
        conn.execute("INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, epoch, window_key,"
                     " spec_hash, binding_id, run_id, admitted_at) VALUES ('oc-1', ?, ?, ?, 'admitted', 0, 'w',"
                     " ?, ?, ?, 't')", (agent, KIND, key, "b" * 64, BINDING, run))
        if launched:
            conn.execute("UPDATE agent_occurrences SET state = 'launched', launch_epoch = 0, link_id = 'al-1',"
                         " link_incarnation = 'inc', launched_at = 't' WHERE occurrence_id = 'oc-1'")
    j.start(run, binding_id=BINDING)
    return j


def _end(j, state: str, reason: str) -> None:
    with j.db.write() as conn:
        conn.execute("UPDATE agent_occurrences SET state = ?, epoch = epoch + 1, ended_at = 't', reason = ? "
                     "WHERE occurrence_id = 'oc-1'", (state, reason))


def test_e2c_arm_a_on_the_real_branch(tmp_path):
    # behaviour: on a registry-bearing /2 store a launched agent run reaches fence_now (e1 DONE), a revoke on the
    # cached snapshot is UNCLASSIFIED, a refreshed one STALE, and the journal holds one row
    (tmp_path / "w").mkdir(mode=0o700)
    w = World(tmp_path / "w")
    w.link({"op": "create_grant", "grant": grad(w)}, "laptop")
    signers = root_dir(tmp_path) / "allowed_signers"
    signers.write_text(f'laptop namespaces="levain-authority" {w.state().keys[w.fp("laptop")].public_key}\n')
    os.chmod(signers, 0o644)
    src = AuthorityCoverage(w.root, allowed_signers=signers, store_id=STORE)
    src.refresh()

    def fence_now(conn):
        v = src.peek(APPEND, w.clock.now)
        if v == COVER_MOVED:
            raise _CoverMoved(v)
        return hashlib.sha256(v.encode()).hexdigest()

    admitted = src.peek(APPEND, w.clock.now)
    assert admitted not in (COVER_NONE, COVER_MOVED)
    fence = hashlib.sha256(admitted.encode()).hexdigest()
    j = _journal(tmp_path)
    fired: list[str] = []

    def effect(eid):
        return j.effect(RUN, eid, digest="d" + eid, fence=fence, fence_now=fence_now,
                        fn=lambda: fired.append(eid) or "ok")

    o1 = effect("e1")
    w.link({"op": "revoke_grant", "grant_id": GP}, "pk")
    o2 = effect("e2")
    src.refresh()
    o3 = effect("e3")
    rows = sqlite3.connect(j.db.path).execute("SELECT effect_id, state FROM effects WHERE run_id = ?",
                                               (RUN,)).fetchall()
    print(f"E2c arm A on the real branch: e1 {o1.status.name} (fn ran {fired[:1]}), e2 {o2.status.name}, "
          f"e3 {o3.status.name}, journal rows {rows}")
    assert (o1.status, o2.status, o3.status) == (EffectStatus.DONE, EffectStatus.UNCLASSIFIED, EffectStatus.STALE)
    assert fired == ["e1"] and rows == [("e1", "done")]
    # the leaf's copy of the run id derivation is the controller's
    with j.db.write() as conn:
        assert binding.agent_run_proof(conn, RUN) is True


def test_a_corrupt_registry_still_bars_an_agent_run(tmp_path):
    # behaviour: a valid proof skips presence and fireability only; a registry that does not read clean fires nothing
    j = _journal(tmp_path)
    ran: list[str] = []
    assert _go(j, "e1", ran).status is EffectStatus.DONE
    with j.db.write() as conn:
        conn.execute("DELETE FROM meta WHERE key = 'registry'")
        conn.execute("INSERT INTO meta (key, value) VALUES ('registry', '1')")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bindings)")]
        conn.execute(f"INSERT INTO bindings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     ["junk"] * len(cols))
        assert binding.registry_scan_bars(conn) == "registry_corrupt"
    out = _go(j, "e2", ran)
    assert out.status is EffectStatus.BARRED and out.result == "registry_corrupt" and ran == ["e1"]
    # and a run id that matches no launched occurrence never gets the proof (a reused id skips no registry revoke)
    j2 = _journal(tmp_path / "second", launched=False)
    out = _go(j2, "e1")
    assert out.status is EffectStatus.BARRED and out.result == "binding_absent"


def test_hold_and_peek_on_an_agent_run(tmp_path):
    # behaviour: peek and hold call _barrier too; a launched agent run is not barred by the registry, an ended
    # one is (peek: a done effect REPLAYED, an effect never started BARRED; a started one without a result is
    # test_an_unresolved_effect_outlives_the_end_of_its_agent_run's)
    j = _journal(tmp_path)
    assert j.peek(RUN, "nothing") is None
    assert _go(j, "e1").status is EffectStatus.DONE
    assert j.hold(RUN, "h1", digest="dh", pending={}).status is EffectStatus.HELD
    with j.db.write() as conn:
        conn.execute("UPDATE agent_occurrences SET state = 'done', ended_at = 't', reason = 'r' "
                     "WHERE occurrence_id = 'oc-1'")
    assert j.peek(RUN, "e1", digest="de1").status is EffectStatus.REPLAYED
    out = j.hold(RUN, "h2", digest="dh", pending={})
    assert out.status is EffectStatus.BARRED and out.result == "binding_absent"
    barred = j.peek(RUN, "e2")
    assert barred is not None and barred.status is EffectStatus.BARRED and barred.result == "binding_absent"


def test_an_unresolved_effect_outlives_the_end_of_its_agent_run(tmp_path):
    # behaviour: an effect that began and has no result is POISONED (no lease) or IN_FLIGHT (leased) after crash
    # recovery or a failure ends the occurrence; it is never relabelled BARRED, and nothing runs it again
    j = _journal(tmp_path)
    with j.db.write() as conn:   # the row a process killed inside fn leaves: intent committed, no lease holder
        conn.execute("INSERT INTO effects (run_id, effect_id, digest, pid, state) VALUES (?, 'e1', 'de1', 1, "
                     "'intent')", (RUN,))
    _end(j, "stopped", "crash_recovered")
    assert j.peek(RUN, "e1", digest="de1").status is EffectStatus.POISONED
    ran: list[str] = []
    assert _go(j, "e1", ran).status is EffectStatus.POISONED and ran == []
    assert (RUN, "e1") in j.poisoned()

    j2 = _journal(tmp_path / "leased")
    seen: list = []

    def inside():   # the effect is running (intent committed, lease held) when the occurrence fails
        _end(j2, "failed", "exited")
        seen.append(j2.peek(RUN, "e2", digest="de2"))
        return "ok"

    j2.effect(RUN, "e2", digest="de2", fence="f", fence_now=lambda conn: "f", fn=inside)
    assert seen[0].status is EffectStatus.IN_FLIGHT


def test_the_proof_refuses_a_value_of_the_wrong_type(tmp_path):
    # behaviour: a BLOB id bars the run (binding_absent) rather than raising out of _barrier; a NULL binding id or
    # a text launch_epoch never passes the proof
    j = _journal(tmp_path, agent=AGENT.encode())
    out = j.peek(RUN, "e1")
    assert out is not None and out.status is EffectStatus.BARRED and out.result == "binding_absent"
    conn = sqlite3.connect(":memory:")   # tables without the real NOT NULL, so the NULL can be written
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, binding_id TEXT)")
    conn.execute("CREATE TABLE agent_occurrences (agent_id, kind, key, binding_id, state, launch_epoch, run_id)")
    conn.execute("INSERT INTO runs VALUES (?, NULL)", (RUN,))
    conn.execute("INSERT INTO agent_occurrences VALUES (?, ?, ?, NULL, 'launched', 0, ?)", (AGENT, KIND, KEY, RUN))
    assert binding.agent_run_proof(conn, RUN) is False
    conn.execute("UPDATE runs SET binding_id = 'b'")
    conn.execute("UPDATE agent_occurrences SET binding_id = 'b'")
    assert binding.agent_run_proof(conn, RUN) is True
    conn.execute("UPDATE agent_occurrences SET launch_epoch = '0'")
    assert binding.agent_run_proof(conn, RUN) is False


def test_the_run_id_copy_matches_the_controller_on_non_ascii_ids(tmp_path):
    # behaviour: the leaf's derivation byte-matches controller.run_id for non-ASCII ids, so such an agent reaches
    # fence_now instead of being barred binding_absent
    agent, key = "agent-ü", "k-é漢"
    run = controller_run_id(agent, KIND, key)
    j = _journal(tmp_path, agent=agent, key=key, run=run)
    with j.db.write() as conn:
        assert binding.agent_run_proof(conn, run) is True
    assert _go(j, "e1", run=run).status is EffectStatus.DONE


def test_a_v3_store_with_no_agent_tables_is_unchanged():
    # behaviour: the branch is gated on a /2 journal that holds agent_occurrences; the existing suites are the proof
    # for /3 and for a /2 store without the table, run unchanged (tests/test_autonomic_journal*.py)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, binding_id TEXT)")
    conn.execute("INSERT INTO runs VALUES ('run-x', 'b')")
    assert binding.agent_run_proof(conn, "run-x") is False   # no agent table: the proof answers False, never raises
