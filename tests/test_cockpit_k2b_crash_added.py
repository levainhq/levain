"""K2b-2: the r1-r7 "Added at" cases of K2B_DESIGN §7 that the spore effects and the existing seams reach.

Reached: r1 (seed killed at ``applied:0`` then another writer edits / resolves the created row; an injected
divergent value on each ``changes`` field refused in-transaction, a NON-``changes`` field included), r2 (two
concurrent settlers of one unit; a transient ``OSError`` leaves the unit PLANNED and the re-drive finishes
it), r4/r5 (faults with an injected clock: a restart 11 minutes after nine faults applies; ten faults end
``store error`` naming the last; a fault, a restart hours later, a fault again counts attempts=2), r6 (a
second broker on the root is write-disabled and settles nothing, then takes the lock once the first stops
and recovers its DECIDED op).

Not reached (no multi-effect verb, no signing key, no thread barrier inside production code): r2 tombstone,
r4 seed + dependent ascend, the corrupt ``spores.json`` repair, the 50-listing fault bound, the
kill-inside-``record_fault`` case, r7 (drain barrier, standby panel, heal ACL probe, ``ssh-keygen`` in flight).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain.cockpit.broker import Broker, BrokerError, Lifecycle
from levain.cockpit.consent import value_sha256
from levain.cockpit.effects import (
    Effect,
    EffectState,
    EffectStatus,
    SporeEffectStore,
    TransientApplyFault,
    plan_units,
    settle,
)
from levain.cockpit.firejournal import CockpitDB
from levain.cockpit.results import Fault
from tests.test_cockpit_k2b_crash import (  # noqa: F401 - fixtures
    BROWSER,
    VERBS,
    _consent_effects,
    _count,
    _effects,
    _ops,
    _row,
    _run,
    _sql,
    _target,
    _wait_state,
    home,
    spores,
)

T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _real_clock_broker(home: Path, spores: Path, store: Any = None, **kw: Any) -> Broker:
    b = Broker(home / "data", stores={"spores": store or SporeEffectStore(spores)}, store_paths=[spores],
               install_class="reported", **kw)
    b.start()
    return b


def _recover_until(b: Broker, data: Path, query: str, want: Any, timeout: float = 10.0) -> None:
    """Recover until the journal reads ``want``. The re-drive thread shares the injected clock, so it can hold
    the op's mutex when ``recover`` arrives (a held mutex skips the pass) and its own pass can carry a stale
    ``now``; one ``recover`` is therefore not one attempt. Re-recovering is idempotent: a unit not yet due
    is passed over. Polls to a deadline, never a fixed sleep."""
    end = time.monotonic() + timeout
    while True:
        b.recover()
        if _sql(data, query)[0][0] == want or time.monotonic() >= end:
            return
        time.sleep(0.02)


def _issue(b: Broker, spores: Path, name: str, key: str = "i1") -> dict:
    verb, params, ok = VERBS[name]
    return b.issue(verb=verb, params=params, target=_target(spores, ok), tier="T2", credential=BROWSER,
                   intent="reported", idempotency_key=key)


# --- r1: a seed killed at applied:0, then another writer ---------------------------------------------

def _seed_killed(home: Path, spores: Path) -> tuple[str, str]:
    assert _run(home, spores, "seed", "applied:0").returncode == 137
    pid, state = _ops(home / "data")[0]
    assert state == "DECIDED" and [r[1] for r in _effects(home / "data", pid)] == ["PLANNED"]   # CONTROL
    key = _consent_effects(home / "data", pid)[0].resource_key.removeprefix("spore:")
    assert _count(spores, key) == 1, "the create is durable in the store, the journal has not heard"
    return pid, key


@pytest.mark.parametrize("other", ["edit", "resolve"])
def test_seed_killed_then_other_writer_recovers_applied_unlabelled(home: Path, spores: Path, other: str) -> None:
    pid, key = _seed_killed(home, spores)
    st = SporeStore(spores)
    created = st.get_by_origin_key(key)
    assert created is not None
    if other == "edit":
        st.update(created["id"], text="someone else's words")
    else:
        st.descend(created["id"], kind="done")
    moved = _row(spores, key)
    b = _real_clock_broker(home, spores)
    try:
        b.recover()
        assert _wait_state(home / "data", pid, "COMMITTED")
        (idx, status, native_id, reason, _rk), = _effects(home / "data", pid)
        assert status == "APPLIED", reason
        assert native_id == created["id"], "the create's native_id is found by origin_key, resolved or not"
        assert _count(spores, key) == 1
        if other == "edit":
            consent_text = _consent_effects(home / "data", pid)[0].changes["text"]
            assert b.db.label_for(f"spore:{key}", "text", value_sha256(consent_text)) is None, \
                "no label on a field whose value moved"
            assert _row(spores, key)["text"] == "someone else's words"
        assert _row(spores, key) == moved or other == "resolve", "recovery wrote nothing over the other writer"
    finally:
        b.stop()


def test_seed_killed_recovers_with_label_when_nobody_moved_it(home: Path, spores: Path) -> None:
    """Control for the previous case: untouched, the same recovery labels the consented text."""
    pid, key = _seed_killed(home, spores)
    b = _real_clock_broker(home, spores)
    try:
        assert _wait_state(home / "data", pid, "COMMITTED")
        text = _consent_effects(home / "data", pid)[0].changes["text"]
        assert b.db.label_for(f"spore:{key}", "text", value_sha256(text)) is not None
    finally:
        b.stop()


# --- r1/r2: an injected divergent write is refused in the store's transaction ------------------------

class Diverging(SporeEffectStore):
    """Writes ``args`` with ``mutate`` applied while the postcondition keeps the SIGNED values."""

    mutate: Any = None

    def spore_apply(self, effect: Effect) -> Any:
        sa = super().spore_apply(effect)
        return dataclasses.replace(sa, args=self.mutate(dict(sa.args)))


def _diverge(field: str, value: Any) -> Any:
    def mutate(args: dict) -> dict:
        args[field] = value
        return args
    return mutate


# (fixture, args key to diverge, divergent value, adds a key outside `changes`)
DIVERGENCES = [
    ("update_text", "text", "HIJACKED", False),
    ("update_text", "tier", "hot", True),          # a NON-`changes` version_field (r2)
    ("update_text", "next", "2031-01-01", True),
    ("surface_at", "next", "2031-01-01", False),
    ("set_disposition", "disposition", "handoff", False),
    ("undo_edit", "tier", "hot", False),
    ("descend", "kind", "dropped", False),
    ("ascend", "kind", "thread", False),
    ("ascend", "ref", "commit ZZZ", False),
]


@pytest.mark.parametrize("name,field,value,extra", DIVERGENCES, ids=lambda v: str(v))
def test_divergent_write_is_refused_in_transaction(home: Path, spores: Path, name: str, field: str,
                                                   value: Any, extra: bool) -> None:
    verb, params, ok = VERBS[name]
    pre = _row(spores, ok)
    store = Diverging(spores)
    store.mutate = _diverge(field, value)
    b = _real_clock_broker(home, spores, store)
    try:
        out = _issue(b, spores, name)
        state = _sql(home / "data", "SELECT state FROM pendings")[0][0]
        (_i, status, _n, reason, _r), = _effects(home / "data", out["pending_id"])
        assert (status, state) == ("UNAPPLIED", "COMMITTED_INCOMPLETE"), (status, state, reason)
        assert reason == "refused"
        assert _row(spores, ok) == pre, "the refusal left the pre-image whole"
        assert _count(spores, ok) == 1
    finally:
        b.stop()


@pytest.mark.parametrize("name", ["update_text", "descend", "ascend", "set_disposition"])
def test_divergence_control_undiverged_store_applies(home: Path, spores: Path, name: str) -> None:
    """Control: the same store with the mutation off applies, so the refusals above are the gate's."""
    verb, params, ok = VERBS[name]
    pre = _row(spores, ok)
    store = Diverging(spores)
    store.mutate = lambda args: args
    b = _real_clock_broker(home, spores, store)
    try:
        out = _issue(b, spores, name)
        assert out["state"] == "COMMITTED"
        assert _row(spores, ok) != pre, "the undiverged write landed"
    finally:
        b.stop()


# --- r2: two concurrent settlers of one unit ---------------------------------------------------------

def test_two_concurrent_settlers_both_record_applied_once(home: Path, spores: Path) -> None:
    assert _run(home, spores, "update_text", "decided").returncode == 137
    data = home / "data"
    pid, state = _ops(data)[0]
    assert state == "DECIDED"                                              # CONTROL
    b = Broker(data, stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
               install_class="reported")
    # no start(): the settlers below are driven by hand against the journal, like two processes would
    db = CockpitDB(data)
    store = SporeEffectStore(spores)
    effects = _consent_effects(data, pid)
    unit = plan_units(effects, {"spores": store})[0]
    barrier = threading.Barrier(2)
    results: list[Any] = [None, None]

    def settler(n: int) -> None:
        res = settle(unit, store, {0: EffectState(EffectStatus.PLANNED, None)})
        barrier.wait(10)         # both read lock-free and applied/found-applied before either records
        results[n] = db.record_unit(pid, b._unit_results(unit, res, {0: EffectState(EffectStatus.PLANNED, None)}),
                                    T0)

    ts = [threading.Thread(target=settler, args=(n,)) for n in (0, 1)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(r.recorded for r in results) == [False, True], "exactly one settler wrote the unit"
    rows = _effects(data, pid)
    assert [r[1] for r in rows] == ["APPLIED"], rows
    assert _row(spores, "k.loop")["text"] == "edited" and _count(spores, "k.loop") == 1


# --- r2/r4/r5: faults and the injected clock ---------------------------------------------------------

class Flaky(SporeEffectStore):
    def __init__(self, path: Path, fail: int | None) -> None:
        super().__init__(path)
        self.fail = fail            # None = always, n = the first n calls
        self.calls = 0

    def apply_unit(self, unit: Any) -> Any:
        self.calls += 1
        if self.fail is None or self.calls <= self.fail:
            raise TransientApplyFault("OSError", f"boom{self.calls}")
        return super().apply_unit(unit)


def test_transient_oserror_leaves_planned_and_redrive_finishes(home: Path, spores: Path) -> None:
    clock = Clock()
    store = Flaky(spores, fail=1)
    b = _real_clock_broker(home, spores, store, clock=clock)
    try:
        out = _issue(b, spores, "update_text")
        pid = out["pending_id"]
        assert out["state"] == "applying"                                   # CONTROL: not terminal
        (_i, status, _n, _r, _k), = _effects(home / "data", pid)
        assert status == "PLANNED" and _row(spores, "k.loop")["text"] == "a loop"
        assert _sql(home / "data", "SELECT attempts FROM effects")[0][0] == 1
        clock.at += timedelta(seconds=6)
        _recover_until(b, home / "data", "SELECT state FROM pendings", "COMMITTED")
        assert _sql(home / "data", "SELECT state FROM pendings")[0][0] == "COMMITTED"
        assert _row(spores, "k.loop")["text"] == "edited"
    finally:
        b.stop()


def test_nine_faults_kill_restart_eleven_minutes_later_applies(home: Path, spores: Path) -> None:
    clock = Clock()
    b = _real_clock_broker(home, spores, Flaky(spores, fail=None), clock=clock)
    try:
        pid = _issue(b, spores, "update_text")["pending_id"]
        for n in range(2, 10):
            clock.at += timedelta(seconds=130)
            _recover_until(b, home / "data", "SELECT attempts FROM effects", n)
        att = _sql(home / "data", "SELECT attempts, status FROM effects")[0]
        assert att == (9, "PLANNED"), att                                   # CONTROL: nine, still planned
    finally:
        b.stop()
    store = Flaky(spores, fail=0)
    b2 = _real_clock_broker(home, spores, store, clock=Clock(clock.at + timedelta(minutes=11)))
    try:
        assert _wait_state(home / "data", pid, "COMMITTED"), _ops(home / "data")
        assert _row(spores, "k.loop")["text"] == "edited"
        assert _sql(home / "data", "SELECT status FROM effects")[0][0] == "APPLIED"
    finally:
        b2.stop()


def test_ten_faults_end_store_error_naming_the_last(home: Path, spores: Path) -> None:
    clock = Clock()
    b = _real_clock_broker(home, spores, Flaky(spores, fail=None), clock=clock)
    try:
        pid = _issue(b, spores, "update_text")["pending_id"]
        for n in range(2, 11):
            clock.at += timedelta(seconds=130)
            _recover_until(b, home / "data", "SELECT attempts FROM effects", n)
        status, reason, att = _sql(home / "data", "SELECT status, reason, attempts FROM effects")[0]
        assert status == "UNAPPLIED" and att == 10
        assert reason.startswith("store error:") and "boom10" in reason, reason
        assert _ops(home / "data") == [(pid, "COMMITTED_INCOMPLETE")]
        assert _row(spores, "k.loop")["text"] == "a loop"
    finally:
        b.stop()


def test_fault_restart_hours_later_fault_again_counts_two(home: Path, spores: Path) -> None:
    clock = Clock()
    b = _real_clock_broker(home, spores, Flaky(spores, fail=None), clock=clock)
    try:
        _issue(b, spores, "update_text")
        assert _sql(home / "data", "SELECT attempts FROM effects")[0][0] == 1
    finally:
        b.stop()
    b2 = _real_clock_broker(home, spores, Flaky(spores, fail=None), clock=Clock(T0 + timedelta(hours=5)))
    try:
        att, status = _sql(home / "data", "SELECT attempts, status FROM effects")[0]
        assert (att, status) == (2, "PLANNED"), (att, status)
    finally:
        b2.stop()


# --- r5/r6: a second broker on the root --------------------------------------------------------------

def test_second_broker_is_write_disabled_then_takes_the_lock_and_recovers(home: Path, spores: Path) -> None:
    clock = Clock()
    b1 = _real_clock_broker(home, spores, Flaky(spores, fail=None), clock=clock)
    pid = _issue(b1, spores, "update_text")["pending_id"]
    assert _ops(home / "data") == [(pid, "DECIDED")]                          # CONTROL: pending work exists
    b2 = Broker(home / "data", stores={"spores": SporeEffectStore(spores)}, store_paths=[spores],
                install_class="reported", clock=Clock(T0 + timedelta(hours=1)), lock_poll=0.05)
    try:
        assert b2.start() is Lifecycle.STANDBY
        with pytest.raises(BrokerError) as ei:
            _issue(b2, spores, "set_disposition", key="i2")
        assert ei.value.code == "standby"
        with pytest.raises(BrokerError):
            b2.recover()
        assert isinstance(b2.outcome(pid), Fault)
        time.sleep(0.5)
        assert _ops(home / "data") == [(pid, "DECIDED")], "the standby broker settled nothing"
        assert _row(spores, "k.loop")["text"] == "a loop"
        b1.stop()
        deadline = time.monotonic() + 15
        while b2.state is not Lifecycle.ACTIVE and time.monotonic() < deadline:
            time.sleep(0.05)
        assert b2.state is Lifecycle.ACTIVE, "the standby broker took the lock once the first stopped"
        assert _wait_state(home / "data", pid, "COMMITTED"), _ops(home / "data")
        assert _row(spores, "k.loop")["text"] == "edited"
        assert _issue(b2, spores, "set_disposition", key="i2")["state"] == "COMMITTED"   # write routes open
    finally:
        b1.stop()
        b2.stop()
