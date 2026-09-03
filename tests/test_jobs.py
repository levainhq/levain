"""Tests for the async-job runtime (levain.jobs) — the propose→job→poll seam for I/O-bound verbs.

The JobStore state machine (pending→running→done|failed) + the bounded JobRuntime executor +
the fail-closed / crash-sweep / lease discipline that mirrors levain.idempotency. The safety
core: a job EXECUTES once (the single-winner pending→running claim), a corrupt store FAILS CLOSED
at the poll (never a false unknown → no duplicate expensive re-propose), and a process restart
sweeps orphaned jobs to failed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from levain.jobs import (
    JOB_LEASE_S,
    JobRecord,
    JobRuntime,
    JobStore,
    JobStoreCorruptError,
)

_T0 = "2026-06-28T10:00:00+00:00"


def _later(seconds: int) -> str:
    # a fixed timestamp `seconds` after _T0 (10:00:00) — for lease/prune tests without a real clock.
    base_min, sec = divmod(seconds, 60)
    hh, mm = divmod(base_min, 60)
    return f"2026-06-28T{10 + hh:02d}:{mm:02d}:{sec:02d}+00:00"


# --- JobRecord -------------------------------------------------------------------------

class TestJobRecord:
    def test_round_trip(self) -> None:
        r = JobRecord(job_id="j1", verb="consult", status="done", created_at=_T0,
                      started_at=_T0, finished_at=_T0, result={"text": "hi"})
        assert JobRecord.from_dict(r.to_dict()) == r

    def test_done_requires_dict_result(self) -> None:
        with pytest.raises(ValueError):
            JobRecord.from_dict({"job_id": "j", "verb": "c", "status": "done",
                                 "created_at": _T0, "result": None})

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValueError):
            JobRecord.from_dict({"job_id": "j", "verb": "c", "status": "weird", "created_at": _T0})

    def test_missing_job_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            JobRecord.from_dict({"verb": "c", "status": "pending", "created_at": _T0})


# --- JobStore state machine ------------------------------------------------------------

class TestJobStoreStateMachine:
    def _store(self, tmp_path: Path) -> JobStore:
        return JobStore(tmp_path / "jobs.json")

    def test_create_then_pending(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        s.create("j1", "consult", _T0)
        assert s.read_status("j1", _T0) == {"job_id": "j1", "status": "pending"}

    def test_claim_running_single_winner(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        s.create("j1", "consult", _T0)
        assert s.claim_running("j1", _T0) is True
        assert s.read_status("j1", _T0)["status"] == "running"
        # a second claim of the same job → False (not re-runnable)
        assert s.claim_running("j1", _T0) is False

    def test_claim_missing_job_false(self, tmp_path: Path) -> None:
        assert self._store(tmp_path).claim_running("nope", _T0) is False

    def test_finish_done_carries_result(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        s.create("j1", "consult", _T0)
        s.claim_running("j1", _T0)
        assert s.finish("j1", "done", {"text": "review"}, None, _T0) is True
        st = s.read_status("j1", _T0)
        assert st["status"] == "done" and st["result"] == {"text": "review"}

    def test_finish_failed_carries_error(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        s.create("j1", "consult", _T0)
        s.claim_running("j1", _T0)
        s.finish("j1", "failed", None, "TimeoutError: 300s", _T0)
        st = s.read_status("j1", _T0)
        assert st["status"] == "failed" and "Timeout" in st["error"]

    def test_finish_only_from_running(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        s.create("j1", "consult", _T0)  # still pending, never claimed
        assert s.finish("j1", "done", {"x": 1}, None, _T0) is False  # no-op: not running

    def test_unknown_job(self, tmp_path: Path) -> None:
        assert self._store(tmp_path).read_status("ghost", _T0) == {"job_id": "ghost", "status": "unknown"}

    def test_duplicate_create_rejected(self, tmp_path: Path) -> None:
        s = self._store(tmp_path)
        s.create("j1", "consult", _T0)
        with pytest.raises(ValueError):
            s.create("j1", "consult", _T0)

    def test_missing_file_reads_unknown_not_corrupt(self, tmp_path: Path) -> None:
        # a never-written store is legitimate first use → unknown, NOT a fail-closed corrupt.
        assert self._store(tmp_path).read_status("j", _T0)["status"] == "unknown"


# --- fail-closed on corruption (the load-bearing read polarity) ------------------------

class TestFailClosed:
    def _corrupt(self, tmp_path: Path, text: str) -> JobStore:
        p = tmp_path / "jobs.json"
        p.write_text(text, encoding="utf-8")
        return JobStore(p)

    def test_poll_fails_closed_on_bad_json(self, tmp_path: Path) -> None:
        with pytest.raises(JobStoreCorruptError):
            self._corrupt(tmp_path, "{not json").read_status("j", _T0)

    def test_poll_fails_closed_on_non_list(self, tmp_path: Path) -> None:
        with pytest.raises(JobStoreCorruptError):
            self._corrupt(tmp_path, '{"job_id":"j"}').read_status("j", _T0)

    def test_poll_fails_closed_on_duplicate_id(self, tmp_path: Path) -> None:
        dup = json.dumps([
            {"job_id": "j", "verb": "c", "status": "done", "created_at": _T0, "result": {}},
            {"job_id": "j", "verb": "c", "status": "pending", "created_at": _T0},
        ])
        with pytest.raises(JobStoreCorruptError):
            self._corrupt(tmp_path, dup).read_status("j", _T0)

    def test_create_fails_closed_on_corrupt(self, tmp_path: Path) -> None:
        # a mutation must NOT silently heal a corrupt store by overwriting it (it could drop a live
        # job's record) — the caller maps this to a 500 + releases the idempotency key.
        with pytest.raises(JobStoreCorruptError):
            self._corrupt(tmp_path, "garbage").create("new", "c", _T0)

    def test_claim_fails_closed_on_corrupt(self, tmp_path: Path) -> None:
        with pytest.raises(JobStoreCorruptError):
            self._corrupt(tmp_path, "garbage").claim_running("j", _T0)

    def test_corrupt_file_not_overwritten(self, tmp_path: Path) -> None:
        s = self._corrupt(tmp_path, "garbage-preserved")
        with pytest.raises(JobStoreCorruptError):
            s.read_status("j", _T0)
        assert (tmp_path / "jobs.json").read_text() == "garbage-preserved"  # forensic evidence kept


# --- lease + crash recovery + prune ----------------------------------------------------

class TestLeaseAndRecovery:
    def test_pending_past_lease_reads_failed(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        s.create("j1", "consult", _T0)
        st = s.read_status("j1", _later(JOB_LEASE_S + 60))  # 11min later > 10min lease
        assert st["status"] == "failed" and st["error"] == "interrupted"

    def test_running_past_lease_reads_failed(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        s.create("j1", "consult", _T0)
        s.claim_running("j1", _T0)
        st = s.read_status("j1", _later(JOB_LEASE_S + 60))
        assert st["status"] == "failed"

    def test_done_within_ttl_survives_lease(self, tmp_path: Path) -> None:
        # a DONE record is not lease-bound (only the result TTL); it stays readable well past the
        # lease so the operator can read a long-finished result.
        s = JobStore(tmp_path / "jobs.json")
        s.create("j1", "consult", _T0)
        s.claim_running("j1", _T0)
        s.finish("j1", "done", {"text": "ok"}, None, _T0)
        st = s.read_status("j1", _later(JOB_LEASE_S + 120))
        assert st["status"] == "done"

    def test_sweep_marks_nonterminal_failed(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        s.create("p", "consult", _T0)
        s.create("r", "consult", _T0)
        s.claim_running("r", _T0)
        s.create("d", "consult", _T0)
        s.claim_running("d", _T0)
        s.finish("d", "done", {"x": 1}, None, _T0)
        assert s.sweep(_T0) == 2  # p (pending) + r (running) → failed; d (done) untouched
        assert s.read_status("p", _T0)["status"] == "failed"
        assert s.read_status("r", _T0)["status"] == "failed"
        assert s.read_status("d", _T0)["status"] == "done"
        assert "interrupted by restart" in s.read_status("p", _T0)["error"]

    def test_sweep_skips_corrupt_store(self, tmp_path: Path) -> None:
        # a corrupt store can't be safely rewritten — sweep leaves it (returns 0); a later poll
        # fails closed on it, the safe direction.
        p = tmp_path / "jobs.json"
        p.write_text("garbage", encoding="utf-8")
        assert JobStore(p).sweep(_T0) == 0
        assert p.read_text() == "garbage"

    def test_prune_drops_expired_terminals_and_FAILS_orphans(self, tmp_path: Path) -> None:
        """⛔ THIS TEST USED TO PIN THE DEFECT, and it is why fourteen nights of review missed it.

        It asserted `old_pending` reads `unknown` after a prune — the code and this test agreed
        with each other and BOTH disagreed with the prose at four sites (jobs.py:41, :438, :519
        and the operator-facing web_server.py route docstring), each promising a lease-expired
        job reads `failed` at poll. A green suite therefore actively CERTIFIED the half that
        contradicted the documentation.

        ⚡ `sweep()` settles which side was wrong: it performs the SAME operation on the SAME
        records and marks them failed/"interrupted". One operation, two implementations,
        opposite outcomes. `_prune` now matches `sweep`, so an orphan becomes terminal and then
        ages out under `result_ttl_s` — which is what makes the deliberate TTL/idempotency
        matching hold (see the composition test below).
        """
        s = JobStore(tmp_path / "jobs.json", result_ttl_s=3600, lease_s=600)
        # an old done (past TTL) + an old pending orphan (past lease), written far in the past
        s.create("old_done", "consult", _T0)
        s.claim_running("old_done", _T0)
        s.finish("old_done", "done", {"x": 1}, None, _T0)
        s.create("old_pending", "consult", _T0)
        # a fresh create at +2h reaps both (TTL 1h, lease 10min both exceeded)
        s.create("fresh", "consult", _later(7200))
        assert s.read_status("old_done", _later(7200))["status"] == "unknown"    # terminal → dropped
        orphan = s.read_status("old_pending", _later(7200))
        assert orphan["status"] == "failed", "a lease-expired orphan must READ failed, not vanish"
        assert orphan["error"] == "interrupted"
        assert s.read_status("fresh", _later(7200))["status"] == "pending"

    def test_a_reaped_orphan_still_ages_out_under_the_RESULT_ttl(self, tmp_path: Path) -> None:
        """Transitioning must not make orphans immortal — they become ordinary terminal records."""
        s = JobStore(tmp_path / "jobs.json", result_ttl_s=3600, lease_s=600)
        s.create("orphan", "consult", _T0)
        s.create("reaper", "consult", _later(1200))          # +20m: past lease → transitioned
        assert s.read_status("orphan", _later(1200))["status"] == "failed"
        s.create("reaper2", "consult", _later(9000))         # +2.5h: past result TTL → dropped
        assert s.read_status("orphan", _later(9000))["status"] == "unknown"

    def test_the_replayed_HANDLE_and_the_STORE_agree(self, tmp_path: Path) -> None:
        """THE ACTUAL HARM, and the reason this outranked a prose fix.

        `RESULT_TTL_S` is matched to the 24h idempotency window DELIBERATELY so a replayed
        propose can still poll its handle. Non-terminal orphans reaped at the 600s lease broke
        exactly that composition: a same-key retry replayed a handle reading `pending` while the
        store answered `unknown` for that job_id — a cached handle over a record the store said
        never existed, which is the dead-handle case the TTL matching exists to prevent. And
        `unknown` is the one status this module reserves for "the operator re-proposes", so the
        failure mode was silently re-running an expensive job.
        """
        s = JobStore(tmp_path / "jobs.json", result_ttl_s=86400, lease_s=600)
        s.create("wedged", "consult", _T0)
        s.claim_running("wedged", _T0)
        s.create("someone_elses_propose", "consult", _later(1200))   # +20m: runs the prune

        # The replayed handle would hand the operator back "wedged". The store must not
        # contradict it within the window the two TTLs were matched to cover.
        for at in (_later(1200), _later(7200), _later(80000)):
            got = s.read_status("wedged", at)["status"]
            assert got != "unknown", (
                f"at {at} the store said 'unknown' for a job whose handle is still replayable "
                "— the dead-handle case RESULT_TTL_S is matched to the idempotency window to "
                "prevent"
            )
            assert got == "failed"


# --- JobRuntime (the bounded executor) -------------------------------------------------

class TestJobRuntime:
    def test_submit_runs_and_finishes(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        rt = JobRuntime(s, max_concurrent=2)
        done: list = []
        ok = rt.submit("j1", "consult", lambda: {"text": "result", "summary": "s"},
                       lambda st, r, e: done.append((st, r)), _T0)
        assert ok is True
        _wait(lambda: s.read_status("j1", _T0)["status"] == "done")
        assert s.read_status("j1", _T0)["result"] == {"text": "result", "summary": "s"}
        assert done == [("done", {"text": "result", "summary": "s"})]

    def test_handler_raise_marks_failed(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        rt = JobRuntime(s, max_concurrent=1)

        def boom() -> dict:
            raise RuntimeError("kaboom")

        rt.submit("j1", "consult", boom, lambda *a: None, _T0)
        _wait(lambda: s.read_status("j1", _T0)["status"] == "failed")
        assert "kaboom" in s.read_status("j1", _T0)["error"]  # the message, not just the type

    def test_non_dict_result_fails(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        rt = JobRuntime(s, max_concurrent=1)
        rt.submit("j1", "consult", lambda: "not a dict", lambda *a: None, _T0)
        _wait(lambda: s.read_status("j1", _T0)["status"] == "failed")
        assert "bad_handler_result" in s.read_status("j1", _T0)["error"]

    def test_back_pressure_rejects_when_full(self, tmp_path: Path) -> None:
        s = JobStore(tmp_path / "jobs.json")
        rt = JobRuntime(s, max_concurrent=1)
        gate = threading.Event()
        rt.submit("slow", "consult", lambda: (gate.wait(2), {"text": "x"})[1], lambda *a: None, _T0)
        _wait(lambda: s.read_status("slow", _T0)["status"] == "running")
        # the one slot is busy → a second submit is rejected (False) and writes NO record.
        assert rt.submit("rej", "consult", lambda: {"text": "y"}, lambda *a: None, _T0) is False
        assert s.read_status("rej", _T0)["status"] == "unknown"
        gate.set()
        _wait(lambda: s.read_status("slow", _T0)["status"] == "done")

    def test_slot_released_after_completion(self, tmp_path: Path) -> None:
        # after a job completes the slot frees → a subsequent submit on a max_concurrent=1 runtime
        # succeeds (the semaphore was released in the worker's finally).
        s = JobStore(tmp_path / "jobs.json")
        rt = JobRuntime(s, max_concurrent=1)
        rt.submit("a", "consult", lambda: {"text": "a"}, lambda *a: None, _T0)
        _wait(lambda: s.read_status("a", _T0)["status"] == "done")
        assert rt.submit("b", "consult", lambda: {"text": "b"}, lambda *a: None, _T0) is True
        _wait(lambda: s.read_status("b", _T0)["status"] == "done")

    def test_submit_propagates_create_corruption(self, tmp_path: Path) -> None:
        # a corrupt store at create → submit re-raises (the caller 500s + releases the key) AND the
        # slot is given back (a corrupt-store submit doesn't permanently leak a concurrency slot).
        p = tmp_path / "jobs.json"
        p.write_text("garbage", encoding="utf-8")
        rt = JobRuntime(JobStore(p), max_concurrent=1)
        with pytest.raises(JobStoreCorruptError):
            rt.submit("j", "consult", lambda: {"x": 1}, lambda *a: None, _T0)
        # slot recovered: a subsequent submit (on a now-fixed store path) can acquire it
        p.write_text("[]", encoding="utf-8")
        assert rt.submit("ok", "consult", lambda: {"text": "ok"}, lambda *a: None, _T0) is True


def _wait(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")
