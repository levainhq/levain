"""K2b-1 review fixes: the broker's start sequence never strands its lock, a decided op whose finish
failed is finished in-process, a corrupt op is set aside instead of spun on, a first seed creates the
spore file, an absent target is STALE, and the cap names what occupies it. Real anneal SporeStores and
real cockpit journals in throwaway directories under the home directory, as in the broker tests."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.broker import Broker, BrokerError, Lifecycle
from levain.cockpit.effects import SporeEffectStore, TransientApplyFault
from levain.cockpit.firejournal import CockpitDB, CockpitFormatError
from levain.cockpit.results import Absent, Fault
from tests.test_cockpit_k2b_broker import (  # noqa: F401 - fixtures
    BROWSER,
    _broker,
    _move,
    _rows,
    _state,
    _wait,
    home,
    spores,
)


def _lock_free(data_root: Path) -> bool:
    fd = os.open(data_root / "cockpit.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise
    finally:
        os.close(fd)


class AlwaysFault(SporeEffectStore):
    def apply_unit(self, unit: Any) -> Any:
        raise TransientApplyFault("OSError", "down")


def _decided(home: Path, spores: Path, keys: tuple[str, ...]) -> tuple[Path, list[str]]:
    """Ops left DECIDED with one faulted, planned unit each, by a broker that then stopped."""
    b = _broker(home, spores, AlwaysFault(spores))
    pids = [_move(b, spores, k, intent="reported")["pending_id"] for k in keys]
    b.stop()
    return b.data_root, pids


def _set_due(data_root: Path, when: str) -> None:
    with sqlite3.connect(data_root / "cockpit.db") as conn:
        conn.execute("UPDATE effects SET next_attempt_at = ?", (when,))


PAST, FUTURE = "2000-01-01T00:00:00.000000Z", "2999-01-01T00:00:00.000000Z"


# --- #1: every failure after the lock is taken leaves a terminal or retryable state, fd closed ------

def test_a_garbage_cockpit_db_disables_the_broker_and_releases_the_lock(home, spores):
    root = home / "data"
    root.mkdir(mode=0o700)
    (root / "cockpit.db").write_bytes(b"not a database at all" * 100)
    b = Broker(root, stores={"spores": SporeEffectStore(spores)}, install_class="reported", store_paths=[spores])
    try:
        assert b.start() is Lifecycle.INSTALL_DISABLED
        assert "DatabaseError" in (b.disabled_reason or "")
        assert b._lock_fd is None and _lock_free(root)
        assert b.start() is Lifecycle.INSTALL_DISABLED
    finally:
        b.stop()


def test_a_busy_journal_at_start_is_standby_and_the_healer_retries(home, spores, monkeypatch):
    real = CockpitDB.expire_due
    calls = {"n": 0}

    def busy_once(self: CockpitDB, now: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, now)

    monkeypatch.setattr(CockpitDB, "expire_due", busy_once)
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores)}, install_class="reported",
               store_paths=[spores], lock_poll=0.05)
    try:
        assert b.start() is Lifecycle.STANDBY
        assert _wait(lambda: b.state is Lifecycle.ACTIVE, 3.0)
        assert _move(b, spores, intent="reported")["state"] == "COMMITTED"
    finally:
        b.stop()


def test_one_recovery_op_failing_does_not_stop_the_start(home, spores, monkeypatch):
    root, (pid,) = _decided(home, spores, ("k.seed",))
    _set_due(root, PAST)
    real = Broker._settle_pass

    def flaky(self: Broker, db: Any, p: str, *, now: Any, mode: str) -> None:
        if mode == "start":
            raise sqlite3.OperationalError("database is locked")
        real(self, db, p, now=now, mode=mode)

    monkeypatch.setattr(Broker, "_settle_pass", flaky)
    b = _broker(home, spores)
    try:
        assert b.state is Lifecycle.ACTIVE
        assert _wait(lambda: _state(b, pid) == "COMMITTED", 3.0)
    finally:
        b.stop()


# --- #6: a stop racing a failed start keeps INSTALL_DISABLED and logs no false outlived settler -------

def test_a_stop_during_a_failing_start_keeps_the_disable(home, spores, monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()

    def held(self: CockpitDB, install_class: str) -> str:
        entered.set()
        release.wait(5)
        raise CockpitFormatError("not a cockpit journal")

    monkeypatch.setattr(CockpitDB, "set_install_class", held)
    b = Broker(home / "data", stores={"spores": SporeEffectStore(spores)}, install_class="reported",
               store_paths=[spores], drain_timeout=5)
    starter = threading.Thread(target=b.start, daemon=True)
    starter.start()
    assert entered.wait(5)
    got: list[Lifecycle] = []
    stopper = threading.Thread(target=lambda: got.append(b.stop()), daemon=True)
    with caplog.at_level(logging.WARNING, logger="levain.cockpit.broker"):
        stopper.start()
        assert _wait(lambda: b.state is Lifecycle.DRAINING, 2)
        release.set()
        starter.join(5)
        stopper.join(5)
    assert got == [Lifecycle.INSTALL_DISABLED] and b.state is Lifecycle.INSTALL_DISABLED
    assert "not a cockpit journal" in (b.disabled_reason or "")
    assert not [r for r in caplog.records if "outlived" in r.getMessage()]
    assert _lock_free(home / "data")


# --- #2: a DECIDED op with no planned unit is finished in-process -----------------------------------

def test_a_finish_that_failed_once_is_finished_by_the_thread(home, spores, monkeypatch):
    real = CockpitDB.finish
    calls = {"n": 0}

    def flaky(self: CockpitDB, op: str, now: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, op, now)

    monkeypatch.setattr(CockpitDB, "finish", flaky)
    b = _broker(home, spores, max_wait=60.0)
    try:
        out = _move(b, spores, intent="reported")
        assert out["state"] == "applying"
        assert _rows(b, "SELECT status FROM effects") == [("APPLIED",)]
        assert b.db is not None and b.db.due_ops(b._now()) in ([out["pending_id"]], [])
        assert _wait(lambda: _state(b, out["pending_id"]) == "COMMITTED", 3.0)
    finally:
        b.stop()


# --- #3: a corrupt op is set aside, durably, and one op never blocks the others ----------------------

def test_a_corrupt_op_is_set_aside_and_never_spun_on(home, spores, monkeypatch):
    root, (pid,) = _decided(home, spores, ("k.seed",))
    _set_due(root, PAST)
    with sqlite3.connect(root / "cockpit.db") as conn:
        conn.execute("UPDATE pendings SET record_sha256 = ? WHERE pending_id = ?", ("0" * 64, pid))
    passes = {"n": 0}
    real = Broker._settle_pass

    def counted(self: Broker, db: Any, p: str, **kw: Any) -> None:
        passes["n"] += 1
        real(self, db, p, **kw)

    monkeypatch.setattr(Broker, "_settle_pass", counted)
    b = _broker(home, spores, max_wait=0.5)
    try:
        time.sleep(1.5)
        assert passes["n"] == 1
        assert b.db is not None
        assert b.db.due_ops(b._now()) == [] and b.db.earliest_due() is None and b.db.decided_ops() == []
    finally:
        b.stop()
    again = _broker(home, spores)
    try:
        assert passes["n"] == 1 and _state(again, pid) == "DECIDED"
    finally:
        again.stop()


def test_one_op_failing_in_a_thread_pass_does_not_block_the_next(home, spores, monkeypatch):
    root, (first, second) = _decided(home, spores, ("k.seed", "k.seed2"))
    _set_due(root, FUTURE)
    real = Broker._settle_pass

    def broken_first(self: Broker, db: Any, p: str, *, now: Any, mode: str) -> None:
        if p == first and mode == "thread":
            raise RuntimeError("a bad op")
        real(self, db, p, now=now, mode=mode)

    monkeypatch.setattr(Broker, "_settle_pass", broken_first)
    b = _broker(home, spores)
    try:
        _set_due(root, PAST)
        b._bump()
        assert _wait(lambda: _state(b, second) == "COMMITTED", 3.0)
        assert _state(b, first) == "DECIDED"
    finally:
        b.stop()


# --- #4: the first seed on an install with no spore file creates it --------------------------------

def test_the_first_seed_on_a_fresh_install_creates_the_spore_file(home):
    path = home / "spores.json"
    b = _broker(home, path)
    try:
        out = b.issue(verb="spore_seed", params={"text": "first", "type": "task"},
                      target={"kind": "create", "panel_id": "tray"}, tier="T2", credential=BROWSER,
                      intent="reported")
        assert out["state"] == "COMMITTED", out
        key = "ck." + out["pending_id"][2:] + ".0"
        row = SporeStore(path).get_by_origin_key(key)
        assert row is not None and row["text"] == "first"
    finally:
        b.stop()


# --- #5: an absent target is STALE; a faulted read is source unavailable -------------------------------

@pytest.mark.parametrize("answer, want", [
    (Absent("row gone"), ("STALE", "changed")),
    (Fault("read failed"), ("REFUSED", "source unavailable")),
    (None, ("REFUSED", "source unavailable")),
])
def test_the_recomputed_tier_tells_an_absent_target_from_a_fault(home, spores, answer, want):
    b = _broker(home, spores, tier_of=lambda verb, params, target: answer)
    try:
        out = _move(b, spores, intent="reported")
        assert (out["state"], out["reason"]) == want
    finally:
        b.stop()


# --- #7, #8: the op mutexes are pruned; a replay of an applying op wakes the thread -----------------

def test_op_mutexes_are_pruned_after_each_pass(home, spores):
    b = _broker(home, spores)
    try:
        assert _move(b, spores, intent="reported")["state"] == "COMMITTED"
        assert b._op_locks == {}
    finally:
        b.stop()


def test_a_replay_of_an_applying_op_wakes_the_thread(home, spores):
    b = _broker(home, spores, AlwaysFault(spores))
    try:
        _move(b, spores, intent="reported", idempotency_key="r1")
        woke = {"n": 0}
        b.wake = lambda: woke.__setitem__("n", woke["n"] + 1)  # type: ignore[method-assign]
        out = b.replay(verb="spore_set_disposition", params={"disposition": "note"}, intent="reported",
                       credential=BROWSER, idempotency_key="r1",
                       target={"kind": "row", "panel_id": "tray", "row_id": held_row_id(spores)})
        assert out is not None and out["state"] == "applying" and woke["n"] == 1
    finally:
        b.stop()


def test_dispatch_decides_gestures_under_the_brokers_own_install_class(tmp_path):
    from tests.test_cockpit_k2a import _get, _install, _post, _serve

    root, src, ids = _install(tmp_path)
    body = {"verb": "spore_set_disposition", "panel_id": "tray", "params": {"disposition": "note"},
            "dry_run": True}
    for declared, gesture in ((None, "sign"), ("reported", "direct-reported")):
        with _serve(src, surface_tokens={"browser": "T"}, install_class=declared) as (url, httpd):
            rows = _get(f"{url}/cockpit/panel/tray.json", token="T")["rows"]
            r = next(x for x in rows if x["id"] == f"spore:{ids['seed']}")
            status, out = _post(f"{url}/cockpit/verb", {**body, "row_id": r["id"], "row_version": r["version"]},
                                token="T")
            assert (status, out["gesture"]) == (200, gesture), (declared, out)


# --- #11: a cap hit names the pendings that occupy it --------------------------------------------------

def test_a_cap_hit_names_the_occupying_pendings(home, spores):
    b = _broker(home, spores)
    try:
        for i in range(8):
            _move(b, spores, idempotency_key=f"s{i}")
        with pytest.raises(BrokerError) as ei:
            _move(b, spores, idempotency_key="s8")
        assert ei.value.code == "pending_cap" and ei.value.status == 429
        occ = ei.value.extra["occupying"]
        assert [o["idempotency_key"] for o in occ] == [f"s{i}" for i in range(8)]
        assert {o["verb"] for o in occ} == {"spore_set_disposition"}
        assert all(isinstance(o["issued_at"], str) and o["issued_at"] for o in occ)
    finally:
        b.stop()


def test_the_cap_receipt_reaches_the_http_body(tmp_path):
    from tests.test_cockpit_k2a import _get, _install, _post, _serve

    root, src, ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        rows = _get(f"{url}/cockpit/panel/tray.json", token="T")["rows"]
        r = next(x for x in rows if x["id"] == f"spore:{ids['seed']}")
        at = {"verb": "spore_set_disposition", "panel_id": "tray", "params": {"disposition": "note"},
              "row_id": r["id"], "row_version": r["version"]}
        for i in range(8):
            assert _post(f"{url}/cockpit/verb", {**at, "idempotency_key": f"h{i}"}, token="T")[0] == 200
        status, out = _post(f"{url}/cockpit/verb", {**at, "idempotency_key": "h8"}, token="T")
        assert (status, out["error"]) == (429, "pending_cap")
        assert [o["idempotency_key"] for o in out["occupying"]] == [f"h{i}" for i in range(8)]


# --- #9: a read transaction's own error is the one that propagates ----------------------------------

def test_a_read_transaction_never_masks_the_blocks_error(tmp_path):
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(tmp_path / "x.db", isolation_level=None)

    with pytest.raises(ValueError, match="the real error"):
        with read_transaction(connect) as conn:
            conn.execute("ROLLBACK")       # what SQLite does to a transaction on some errors
            raise ValueError("the real error")


# --- code L3 r1 (complement + codex, 2026-10-10) -----------------------------------------------------


def test_the_returning_floor_is_the_cockpit_journals_alone(home, monkeypatch):
    """complement L3 r1: the SQLite >= 3.35 floor exists for the journal's RETURNING; the autonomic
    store, which K2b moved onto open_durable, runs on an older library as it did before."""
    from levain.autonomic import durable_sqlite
    from levain.autonomic.db import AutonomicDB

    monkeypatch.setattr(durable_sqlite, "_library_version", lambda: (3, 31, 1))
    AutonomicDB(home / "autonomic")._connect().close()
    (home / "data").mkdir()
    with pytest.raises(RuntimeError, match="RETURNING"):
        CockpitDB(home / "data")._connect()


def test_an_idempotency_key_is_bound_to_its_target(home, spores):
    """codex L3 r1: one key and the same params aimed at another row is another request: the replay
    lookup and the journal's own issue both refuse it, instead of answering (or firing) the first."""
    from tests.test_cockpit_k2b_broker import _target

    broker = _broker(home, spores)
    try:
        _key_bound_to_target(broker, spores, _target)
    finally:
        broker.stop()


def _key_bound_to_target(broker: Broker, spores: Path, _target: Any) -> None:
    first = _move(broker, spores, "k.seed", intent="reported", idempotency_key="same")
    assert first["state"] == "COMMITTED"
    other = _target(spores, "k.seed2")
    e = _refused_code(broker.replay, verb="spore_set_disposition", params={"disposition": "note"},
                      intent="reported", credential=BROWSER, idempotency_key="same", target=other)
    assert e == "idempotency_conflict"
    # the journal's issue (the crash window: a key whose pending was issued and never decided)
    held = _move(broker, spores, "k.loop", idempotency_key="held")
    assert held["state"] == "WAITING"
    assert _refused_code(_move, broker, spores, "k.seed2", idempotency_key="held") == "idempotency_conflict"
    assert SporeStore(spores).get_by_origin_key("k.seed2")["disposition"] == "seed"
    # the same row again is still a replay, though its version moved with the fire
    again = broker.replay(verb="spore_set_disposition", params={"disposition": "note"}, intent="reported",
                          credential=BROWSER, idempotency_key="same", target=_target(spores, "k.seed"))
    assert again is not None and again["pending_id"] == first["pending_id"] and again["replay"] is True


def _refused_code(fn: Any, *a: Any, **kw: Any) -> str:
    with pytest.raises(BrokerError) as ei:
        fn(*a, **kw)
    return ei.value.code


def test_a_read_past_the_ttl_answers_expired_and_hands_out_no_record(home, spores):
    """codex L3 r1: expiry is a time; outcome and replay read past it answer EXPIRED, not WAITING."""
    from datetime import datetime, timedelta, timezone

    now = [datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)]
    b = _broker(home, spores, clock=lambda: now[0])
    try:
        held = _move(b, spores, "k.seed", idempotency_key="sig")
        assert held["state"] == "WAITING" and "record" in held
        now[0] += timedelta(minutes=16)
        out = b.outcome(held["pending_id"])
        assert (out["state"], out["reason"], "record" in out) == ("EXPIRED", "expired", False), out
        again = b.replay(verb="spore_set_disposition", params={"disposition": "note"}, intent=None,
                         credential=BROWSER, idempotency_key="sig",
                         target={"kind": "row", "panel_id": "tray", "row_id": held_row_id(spores)})
        assert again is not None and again["state"] == "EXPIRED" and "record" not in again
    finally:
        b.stop()


def held_row_id(spores: Path) -> str:
    return f"spore:{SporeStore(spores).get_by_origin_key('k.seed')['id']}"


def test_the_manifest_renders_gestures_under_the_brokers_install_class(tmp_path):
    """codex L3 r1: with no --install-class the broker is disabled, so no action renders
    direct-reported (dispatch would refuse it 'run the installer'); a reported install renders it."""
    import json as _json

    from tests.test_cockpit_k2a import _get, _install, _serve

    _root, src, _ids = _install(tmp_path)

    def gestures(url: str) -> set[str]:
        raw = _json.dumps(_get(f"{url}/cockpit/panel/tray.json", token="T"))
        return {g for g in ("direct-reported", "sign", "laptop-only") if f'"{g}"' in raw}

    with _serve(src, surface_tokens={"browser": "T"}) as (url, _httpd):
        none = gestures(url)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        reported = gestures(url)
    assert "direct-reported" in reported, reported
    assert "direct-reported" not in none and "sign" in none, none


# --- code L3 r2 (complement + codex, 2026-10-10) -----------------------------------------------------


def test_a_disabled_or_standby_broker_renders_no_reported_gesture(tmp_path, monkeypatch):
    """complement + codex L3 r2: a declared `reported` broker whose start then fails (or that waits on
    another broker's lock) is not write-enabled, so the manifest renders no direct-reported."""
    import json as _json

    from tests.test_cockpit_k2a import _get, _install, _serve

    _root, src, _ids = _install(tmp_path)

    def disabled(self: Broker) -> bool:
        self._disable("test: the start sequence failed")
        return False

    monkeypatch.setattr(Broker, "_attempt", disabled)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, httpd):
        raw = _json.dumps(_get(f"{url}/cockpit/panel/tray.json", token="T"))
        assert httpd.broker.install_class == "reported" and not httpd.broker.enabled
    assert '"direct-reported"' not in raw and '"sign"' in raw


def test_a_replayed_cross_principal_cancel_answers_from_the_journal(home, spores):
    """codex L3 r2: the cancel ended its target, so a retried identical cancel must not refuse that
    target: every issue looks its key up before it builds."""
    from tests.test_cockpit_k2b_broker import PHONE

    b = _broker(home, spores)
    try:
        pid = _move(b, spores)["pending_id"]
        done = b.cancel(pid, credential=PHONE, intent="reported", idempotency_key="c1")
        assert done["state"] == "COMMITTED"
        again = b.cancel(pid, credential=PHONE, intent="reported", idempotency_key="c1")
        assert (again["pending_id"], again["state"], again["replay"]) == (done["pending_id"], "COMMITTED", True)
    finally:
        b.stop()


def test_an_op_whose_expiry_column_differs_from_its_record_is_corrupt(home, spores):
    """codex L3 r2: expiry is enforced on the journal column and read off the signed record; a row
    where the two differ is refused as corrupt, never read one way and decided the other."""
    from datetime import datetime, timezone

    from levain.cockpit.consent import utc_stamp
    from levain.cockpit.firejournal import JournalCorruptError

    b = _broker(home, spores)
    try:
        pid = _move(b, spores)["pending_id"]
        conn = sqlite3.connect(b.data_root / "cockpit.db")
        later = utc_stamp(datetime(2099, 1, 1, tzinfo=timezone.utc))
        conn.execute("UPDATE pendings SET expires_at = ? WHERE pending_id = ?", (later, pid))
        conn.commit()
        conn.close()
        assert b.db is not None
        with pytest.raises(JournalCorruptError, match="expires_at column differs"):
            b.db.load(pid)
    finally:
        b.stop()


def test_a_replay_with_a_non_string_row_id_is_a_bad_request(tmp_path):
    """complement L3 r2: the replay's target is checked before it is compared."""
    from tests.test_cockpit_k2a import _install, _post, _serve

    from tests.test_cockpit_k2a import _get

    _root, src, ids = _install(tmp_path)
    body = {"verb": "spore_set_disposition", "panel_id": "tray", "params": {"disposition": "note"},
            "intent": "reported", "idempotency_key": "k"}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        row = next(r for r in _get(f"{url}/cockpit/panel/tray.json", token="T")["rows"]
                   if r["id"] == f"spore:{ids['seed']}")
        status, fired = _post(f"{url}/cockpit/verb", {**body, "row_id": row["id"], "row_version": row["version"]},
                              token="T")
        assert (status, fired["state"]) == (200, "COMMITTED"), fired
        status, out = _post(f"{url}/cockpit/verb", {**body, "row_id": 5, "row_version": "x"}, token="T")
    assert (status, out["error"]) == (400, "bad_request"), out
