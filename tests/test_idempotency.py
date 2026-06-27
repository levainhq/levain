"""Tests for levain.idempotency — the at-most-once dedup keystore for irreversible actions.

The state machine (claim_or_replay → fresh / replay / in_flight / collision / corrupt), the
finalize/release transitions, the dedup window + lazy prune, the fail-soft read + strict parse,
and the concurrency invariant (exactly one of N concurrent claims of a fresh key wins).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from levain.idempotency import (
    DEFAULT_WINDOW_S,
    IdempotencyRecord,
    IdempotencyStore,
    request_fingerprint,
)

T0 = "2026-06-27T12:00:00+00:00"
T_SOON = "2026-06-27T12:05:00+00:00"          # +5m — well within the window
T_EXPIRED = "2026-06-28T13:00:00+00:00"       # +25h — past the 24h window


def _store(tmp_path: Path) -> IdempotencyStore:
    return IdempotencyStore(tmp_path / "idempotency.json")


# --- the fingerprint ------------------------------------------------------------------

def test_fingerprint_is_deterministic_and_order_independent() -> None:
    a = request_fingerprint("send_relay", {"message": "hi", "to": ["chip"]})
    b = request_fingerprint("send_relay", {"to": ["chip"], "message": "hi"})  # key order flipped
    assert a == b                                   # canonical JSON sorts keys
    assert request_fingerprint("send_relay", {"message": "hi"}) != a       # different params
    assert request_fingerprint("send_inbox", {"message": "hi", "to": ["chip"]}) != a  # diff verb


# --- the core state machine -----------------------------------------------------------

def test_fresh_claim_reserves_in_flight(tmp_path: Path) -> None:
    s = _store(tmp_path)
    out = s.claim_or_replay("k1", "fp1", T0)
    assert out.kind == "fresh"
    rec = s.get("k1")
    assert rec is not None and rec.status == "in_flight" and rec.fingerprint == "fp1"


def test_replay_returns_recorded_result_without_refire(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    assert s.finalize("k1", {"ok": True, "id": "abc", "summary": "relay → chip"}, T0) is True
    out = s.claim_or_replay("k1", "fp1", T_SOON)    # the retry, same key + fingerprint
    assert out.kind == "replay"
    assert out.result == {"ok": True, "id": "abc", "summary": "relay → chip"}


def test_in_flight_when_not_yet_finalized(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    # a second claim before finalize → the first still owns it (NO re-fire)
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "in_flight"


def test_collision_on_same_key_different_fingerprint(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    s.finalize("k1", {"ok": True}, T0)
    # the SAME key reused for a DIFFERENT request → never silently serve the old / fire the new
    assert s.claim_or_replay("k1", "fp_DIFFERENT", T_SOON).kind == "collision"


def test_expired_record_allows_a_fresh_reclaim(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    s.finalize("k1", {"ok": True}, T0)
    # past the window → the old record is treated as absent → a fresh re-claim (which prunes it)
    out = s.claim_or_replay("k1", "fp1", T_EXPIRED)
    assert out.kind == "fresh"
    rec = s.get("k1")
    assert rec is not None and rec.status == "in_flight" and rec.created_at == T_EXPIRED


# --- corrupt-record fail-closed (the at-most-once safety direction) -------------------

def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps(records), encoding="utf-8")


def test_corrupt_matching_record_fails_closed_unknown_status(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _write_records(s.path, [{"key": "k1", "fingerprint": "fp1", "status": "bogus",
                             "created_at": T0, "result": None, "completed_at": None}])
    # a matching record we cannot trust must NEVER re-fire → corrupt, not fresh
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "corrupt"


def test_corrupt_matching_record_done_with_null_result(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _write_records(s.path, [{"key": "k1", "fingerprint": "fp1", "status": "done",
                             "created_at": T0, "result": None, "completed_at": T0}])
    # a `done` record must carry a dict result to replay; a null one is malformed → fail closed
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "corrupt"


def test_corrupt_matching_record_unparseable_created_at(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _write_records(s.path, [{"key": "k1", "fingerprint": "fp1", "status": "done",
                             "created_at": "not-a-date", "result": {"ok": True}, "completed_at": T0}])
    # cannot age or trust a matching record with an unusable timestamp → fail closed (never re-fire)
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "corrupt"


def test_corrupt_non_matching_record_is_skipped_not_fatal(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _write_records(s.path, [{"key": "other", "status": "bogus"}])  # malformed, different key
    # a malformed NON-matching record must not block claiming a fresh key
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"


# --- finalize / release ----------------------------------------------------------------

def test_finalize_is_noop_when_absent_or_already_done(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.finalize("missing", {"ok": True}, T0) is False        # never claimed
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    assert s.finalize("k1", {"ok": True}, T0) is True
    assert s.finalize("k1", {"ok": True, "again": 1}, T_SOON) is False   # already done → no-op
    assert s.get("k1").result == {"ok": True}                       # first result stands


def test_release_drops_an_in_flight_reservation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    assert s.release("k1") is True
    assert s.get("k1") is None
    # after release the SAME key re-claims fresh (a corrected retry re-attempts — pre-fire refusal)
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "fresh"


def test_release_does_not_remove_a_done_record(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.claim_or_replay("k1", "fp1", T0)
    s.finalize("k1", {"ok": True}, T0)
    assert s.release("k1") is False               # a fired action's record must survive
    assert s.get("k1") is not None


# --- the faulted (ambiguous-execution) state ------------------------------------------

def test_mark_faulted_poisons_the_key_and_refuses_retry(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"
    assert s.mark_faulted("k1", T0) is True
    assert s.get("k1").status == "faulted"
    # a retry of a faulted key is refused (never re-fires) — regardless of fingerprint
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "faulted"
    assert s.claim_or_replay("k1", "fp_DIFFERENT", T_SOON).kind == "faulted"


def test_faulted_never_expires(tmp_path: Path) -> None:
    # an AMBIGUOUS reservation must NOT silently age into re-fireable (L2 MED-2)
    s = _store(tmp_path)
    s.claim_or_replay("k1", "fp1", T0)
    s.mark_faulted("k1", T0)
    assert s.claim_or_replay("k1", "fp1", T_EXPIRED).kind == "faulted"   # still poisoned past 24h
    # a fresh claim of ANOTHER key must NOT prune the faulted record
    s.claim_or_replay("k2", "fp2", T_EXPIRED)
    assert s.get("k1") is not None and s.get("k1").status == "faulted"


def test_mark_faulted_is_noop_when_absent_or_done(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.mark_faulted("missing", T0) is False
    s.claim_or_replay("k1", "fp1", T0)
    s.finalize("k1", {"ok": True}, T0)
    assert s.mark_faulted("k1", T0) is False        # a done record is not poisoned
    assert s.get("k1").status == "done"


def test_release_does_not_remove_a_faulted_record(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.claim_or_replay("k1", "fp1", T0)
    s.mark_faulted("k1", T0)
    assert s.release("k1") is False                 # an ambiguous record must survive
    assert s.get("k1").status == "faulted"


# --- timezone normalization (no permanent brick) --------------------------------------

def test_naive_timestamp_is_normalized_not_bricked(tmp_path: Path) -> None:
    # a tz-NAIVE created_at (hand-edit / cross-version) must be assumed UTC + aged normally, NOT
    # raise on the aware/naive subtraction → which would brick + never-prune the key (L2 LOW-2).
    s = _store(tmp_path)
    _write_records(s.path, [{"key": "k1", "fingerprint": "fp1", "status": "done",
                             "created_at": "2026-06-27T12:00:00", "result": {"ok": True},
                             "completed_at": "2026-06-27T12:00:00"}])  # naive (no offset)
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "replay"     # within window → replay
    assert s.claim_or_replay("k1", "fp1", T_EXPIRED).kind == "fresh"   # past window → fresh re-claim


# --- prune + fail-soft -----------------------------------------------------------------

def test_prune_drops_expired_on_a_fresh_write(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.claim_or_replay("old", "fp_old", T0)
    s.finalize("old", {"ok": True}, T0)
    # a fresh claim well past the window prunes the expired "old" record
    s.claim_or_replay("new", "fp_new", T_EXPIRED)
    raw = json.loads(s.path.read_text())
    keys = {r["key"] for r in raw}
    assert keys == {"new"}                          # "old" pruned


def test_missing_file_reads_clean(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.get("k1") is None
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"   # a MISSING file is a legit first use


def test_corrupt_file_FAILS_CLOSED_and_is_preserved(tmp_path: Path) -> None:
    # THE POLARITY INVERSION (L2 HIGH-1): a present-but-corrupt store could be hiding a `done`
    # record → a fresh re-claim would RE-FIRE. So a bad-JSON file fails CLOSED (corrupt, never
    # re-fire) and is NOT overwritten (forensics preserved) — unlike the vagus pending-queue, whose
    # safe-direction is the opposite (drop).
    s = _store(tmp_path)
    s.path.write_text("}{ not json", encoding="utf-8")
    assert s.claim_or_replay("k1", "fp1", T0).kind == "corrupt"
    assert s.path.read_text(encoding="utf-8") == "}{ not json"   # NOT overwritten


def test_non_list_top_level_fails_closed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert s.claim_or_replay("k1", "fp1", T0).kind == "corrupt"


def test_non_dict_element_fails_closed(tmp_path: Path) -> None:
    # a mangled `done` record could have become a non-dict element; dropping it would re-fire its
    # key → the whole store is untrustworthy → fail closed (NOT silently skip the bad element).
    s = _store(tmp_path)
    s.path.write_text(json.dumps([{"key": "k1", "fingerprint": "fp1", "status": "in_flight",
                                   "created_at": T0}, "GARBAGE"]), encoding="utf-8")
    assert s.claim_or_replay("k2", "fp2", T_SOON).kind == "corrupt"


def test_record_with_non_string_key_fails_closed(tmp_path: Path) -> None:
    # a record whose `key` was mangled to null no longer MATCHES its own key → its done-ness would
    # be invisible → a re-fire; refuse the whole store instead (the matchability-loss class).
    s = _store(tmp_path)
    s.path.write_text(json.dumps([{"key": None, "fingerprint": "fp1", "status": "done",
                                   "created_at": T0, "result": {"ok": True}}]), encoding="utf-8")
    assert s.claim_or_replay("k1", "fp1", T_SOON).kind == "corrupt"


def test_duplicate_key_fails_closed(tmp_path: Path) -> None:
    # L3 codex HIGH: a duplicate key is a state this store never writes → corruption. With first-
    # match + the fresh path dropping ALL records for the key, an expired-first duplicate could HIDE
    # a live done/faulted later one → re-fire. Refuse the whole store.
    s = _store(tmp_path)
    s.path.write_text(json.dumps([
        {"key": "k1", "fingerprint": "fp1", "status": "done", "created_at": "2026-06-26T00:00:00+00:00",
         "result": {"ok": True}, "completed_at": T0},                              # expired-first
        {"key": "k1", "fingerprint": "fp1", "status": "faulted", "created_at": T_SOON,
         "result": None, "completed_at": T_SOON},                                  # live poison
    ]), encoding="utf-8")
    # without the duplicate-key guard this would re-fire k1; now it fails closed (never re-fire)
    assert s.claim_or_replay("k1", "fp1", "2026-06-27T13:00:00+00:00").kind == "corrupt"


def test_expired_in_flight_poisons_to_faulted_never_refires(tmp_path: Path) -> None:
    # L3 codex HIGH: an UNRESOLVED in_flight that outlives the window is an orphan (a real handler
    # resolves in seconds; >window ⇒ the process died, possibly AFTER firing). It must NOT age into
    # a fresh re-claim — it poisons to faulted (ambiguous), refusing a re-fire.
    s = _store(tmp_path)
    assert s.claim_or_replay("k1", "fp1", T0).kind == "fresh"     # reserve in_flight, never resolve
    out = s.claim_or_replay("k1", "fp1", T_EXPIRED)               # claim past the window
    assert out.kind == "faulted"                                  # NOT fresh — no re-fire
    assert s.get("k1").status == "faulted"                        # transitioned under the lock
    assert s.claim_or_replay("k1", "fp1", "2099-01-01T00:00:00+00:00").kind == "faulted"  # stays


def test_window_must_be_positive(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(ValueError, match="window_s must be positive"):
        IdempotencyStore(tmp_path / "x.json", window_s=0)
    with pytest.raises(ValueError, match="window_s must be positive"):
        IdempotencyStore(tmp_path / "x.json", window_s=-1)


def test_fingerprint_rejects_non_string_keys_and_nonfinite(tmp_path: Path) -> None:
    import pytest
    # L3 codex/nemotron: json.dumps coerces {1:"x"} == {"1":"x"} → two requests one fingerprint;
    # reject non-string dict keys (and non-finite floats) so the fingerprint is unambiguous.
    with pytest.raises(ValueError, match="non-string dict key"):
        request_fingerprint("v", {1: "x"})            # type: ignore[dict-item]
    with pytest.raises(ValueError, match="non-string dict key"):
        request_fingerprint("v", {"nested": [{2: "y"}]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="non-finite"):
        request_fingerprint("v", {"x": float("nan")})
    # a normal string-keyed request is unaffected
    assert request_fingerprint("v", {"a": 1, "b": [1, 2]})


def test_mutations_fail_closed_on_a_corrupt_store(tmp_path: Path) -> None:
    from levain.idempotency import StoreCorruptError
    import pytest
    # L3 nemotron M5: finalize/mark_faulted/release are MUTATIONS, not diagnostics — on a corrupt
    # store they must NOT silently "heal" it (dropping records → a different key re-fires). They
    # fail CLOSED (StoreCorruptError) and preserve the file.
    s = _store(tmp_path)
    corrupt = json.dumps([{"key": "k1"}, "GARBAGE"])  # a non-dict element → untrustworthy
    s.path.write_text(corrupt, encoding="utf-8")
    for op in (lambda: s.finalize("k1", {"ok": True}, T0),
               lambda: s.mark_faulted("k1", T0),
               lambda: s.release("k1")):
        with pytest.raises(StoreCorruptError):
            op()
    assert s.path.read_text(encoding="utf-8") == corrupt   # preserved, never healed/overwritten


def test_record_roundtrip_strict_parse() -> None:
    rec = IdempotencyRecord(key="k", fingerprint="fp", status="done", created_at=T0,
                            result={"ok": True}, completed_at=T0)
    assert IdempotencyRecord.from_dict(rec.to_dict()) == rec


# --- the at-most-once concurrency invariant -------------------------------------------

def test_concurrent_claims_yield_exactly_one_fresh(tmp_path: Path) -> None:
    s = _store(tmp_path)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()                              # maximize the contention window
        out = s.claim_or_replay("hot", "fp", T0)
        with lock:
            outcomes.append(out.kind)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # the flock serializes the read-modify-write: EXACTLY one fresh; the rest see in_flight
    assert outcomes.count("fresh") == 1
    assert all(o in ("fresh", "in_flight") for o in outcomes)
    assert outcomes.count("in_flight") == 7


def test_default_window_is_24h() -> None:
    assert DEFAULT_WINDOW_S == 24 * 3600
