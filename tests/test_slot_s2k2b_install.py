"""S1 session 2, fix round 2 (lane K2b): the install verbs' quiesce (codex r1 g2 5) and replenish's configured pool
(codex r1 g2 6, SESSION2_PLAN head ruling 8). pytest tmp stores, the runtime helper's real ``lease_slot`` and a fake
directory service: nothing here runs as root."""
from __future__ import annotations

import dataclasses
import fcntl
import io
import os
from contextlib import contextmanager

import pytest

from levain.contracts import canon
from levain.slot import Refused, install_helper as IH, records as R, runtime_helper as RH
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SlotId
from levain.slot.install_helper import Gathered, InstallPlan, Seams, gather_facts, main, plan_for, plan_setup
from levain.slot.layout import POOL_CONFIG_FORMAT, parse_pool_config, pool_config_path, pool_config_row, pool_config_text
from tests.test_slot_s2h_install_main import (OP, OP_UID, S1, SETUP, TS, _PLAN, _Run, _facts, _session_record,
                                              _set_up, _store_seams, _tools_resolve)  # noqa: F401 - autouse fixture

ENV = {"SUDO_UID": str(OP_UID)}
E0 = SlotId("egress", 0)


def _linux_run():
    return _Run({"passwd": f"root:x:0:0:root:/root:/bin/bash\n{OP}:x:{OP_UID}:{OP_UID}:Alice,,,:/home/a:/bin/bash\n",
                 "group": "root:x:0:\n"})


def _store(tmp_path, *slots, pool=None, retired=()):
    """A set-up store: the operator record, an empty sessions dir, ``L/slots/pool.lock`` and each slot's dir, lock and
    state (``reusable``, or ``retired`` for the ones named)."""
    L = tmp_path / "L"
    (L / "policy").mkdir(parents=True)
    (L / "policy" / "operator").write_text(IH.operator_record_text(OP, OP_UID))
    if pool is not None:
        (L / "policy" / "pool").write_text(pool_config_text(*pool))
    (L / "sessions").mkdir()
    (L / "slots").mkdir()
    (L / "slots" / "pool.lock").write_text("")
    for s in slots:
        s.slot_dir(L).mkdir()
        s.lock_path(L).write_text("")
        st = R.SlotState(s, rev=0, lease_seq=0, state="retired" if s in retired else "reusable",
                         dispatch_eligible=True, changed_at=TS)
        (s.slot_dir(L) / "state").write_bytes(R.slot_state_bytes(st))
    return L


def _gather(L, tmp_path, argv=("undo",), host="linux"):
    return gather_facts(parse_install_argv(list(argv)), host, OP_UID, run=_linux_run(),
                        store_seams=_store_seams(L, host), state_dir=tmp_path, user_name=lambda uid: OP)


def _lease(L, session=S1):
    store = RH.Store(_store_seams(L, "linux"))
    try:
        leased = RH.lease_slot(store, slot_class="egress", kind="interactive", vendor="claude", auth="brokered",
                               named=None, ticketed=False, session=session)
        leased.release()
        return leased.slot
    finally:
        store.close()


def _lease_refusal(L):
    with pytest.raises(Refused) as ei:
        _lease(L)
    return ei.value.name


@contextmanager
def _nolock(host):
    yield


def _main(argv, seams):
    out, err = io.StringIO(), io.StringIO()
    rc = main(argv, env=ENV, seams=seams, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


# --- QUIESCE (codex r1 g2 5) ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["undo", "replenish"])
def test_a_prepare_during_an_install_verb_cannot_lease_and_can_after(tmp_path, verb):
    """Through main with the real gather: from the live-lease count through the last act the pool lock is held, so
    the runtime's own ``lease_slot`` refuses ``pool-locked``; once main returns it leases. Red with the gather's hold
    removed (the prepare leased e0 beneath the undo)."""
    L = _store(tmp_path, E0, pool=(1, 0))
    seen = []

    def apply(plan, err):
        seen.append(_lease_refusal(L))
        return None

    seams = Seams(euid=lambda: 0, host=lambda: "linux", lock=_nolock,
                  gather=lambda cmd, host, uid: _gather(L, tmp_path, [verb]),
                  plan=lambda cmd, g: InstallPlan(cmd.verb, "linux", ()), apply=apply)
    rc, out, err = _main([verb], seams)
    assert rc == 0, err
    assert seen == ["pool-locked"]
    assert _lease(L) == E0
    assert IH._store_key(L) not in IH._QUIESCING


def test_the_hold_is_released_when_the_plan_refuses(tmp_path):
    L = _store(tmp_path, E0)

    def plan(cmd, g):
        raise Refused("live-lease", "test")

    seams = Seams(euid=lambda: 0, host=lambda: "linux", lock=_nolock,
                  gather=lambda cmd, host, uid: _gather(L, tmp_path), plan=plan, apply=lambda p, e: None)
    assert _main(["undo"], seams)[1] == '{"ok":false,"refused":"live-lease"}\n'
    assert _lease(L) == E0 and IH._store_key(L) not in IH._QUIESCING


def test_a_prepare_before_its_attach_counts_as_a_live_lease(tmp_path):
    """``prepare`` holds its slot lock from the lease until ATTACH has made the record: under the pool lock a held slot
    lock is counted live, so undo refuses ``live-lease`` though no session record says so yet. Red with
    ``busy_slots`` returning nothing (undo ran)."""
    L = _store(tmp_path, E0, SlotId("egress", 1))
    fd = os.open(E0.lock_path(L), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        g = _gather(L, tmp_path)
        try:
            assert g.facts.live_leases == 1
            assert g.quiesce is not None and g.quiesce.busy_slots() == ["e0"]
        finally:
            g.quiesce.release()
        seams = Seams(euid=lambda: 0, host=lambda: "linux", lock=_nolock,
                      gather=lambda cmd, host, uid: _gather(L, tmp_path), plan=plan_for, apply=lambda p, e: None)
        assert _main(["undo"], seams)[1] == '{"ok":false,"refused":"live-lease"}\n'
    finally:
        os.close(fd)
    g = _gather(L, tmp_path)
    try:
        assert g.facts.live_leases == 0
    finally:
        g.quiesce.release()


def test_session_records_are_counted_under_the_hold(tmp_path):
    L = _store(tmp_path, E0)
    (L / "sessions" / f"{S1}.json").write_bytes(_session_record(True))
    g = _gather(L, tmp_path)
    try:
        assert g.quiesce.held and g.facts.live_leases == 1
    finally:
        g.quiesce.release()


def test_slot_state_writes_run_under_the_hold(tmp_path):
    """The install's own slot-state steps under its hold: no second flock that would wait on the first (red without
    pool_lock's re-entry: ``pool-locked``)."""
    L = _store(tmp_path)
    e1 = SlotId("egress", 1)
    e1.slot_dir(L).mkdir()
    q = IH.Quiesce(L, waits=_store_seams(L))
    try:
        assert q.hold()
        assert IH.write_initial_slot_state(L, e1, waits=_store_seams(L)) == (True, "")
        assert IH.remove_slot_state(L, e1, waits=_store_seams(L)) == (True, "")
    finally:
        q.release()


def test_a_fresh_setup_takes_the_hold_at_its_step_before_any_slot_is_reusable(tmp_path):
    """No pool lock at the gather (a fresh store): the hold is pending, and setup's own step takes it once the rows
    have made the file; from there no prepare leases."""
    L = tmp_path / "L"
    L.mkdir()
    g = gather_facts(parse_install_argv(SETUP), "linux", OP_UID, run=_linux_run(), store_seams=_store_seams(L, "linux"),
                     state_dir=tmp_path, user_name=lambda uid: OP)
    try:
        assert g.quiesce is not None and not g.quiesce.held
        step = IH.quiesce_step("linux", store=L)
        assert step.call()[0] is False                              # the rows have not made the file
        (L / "slots").mkdir()
        (L / "slots" / "pool.lock").write_text("")
        E0.slot_dir(L).mkdir()
        E0.lock_path(L).write_text("")
        IH.write_initial_slot_state(L, E0)
        assert step.call() == (True, "") and g.quiesce.held
        assert _lease_refusal(L) == "pool-locked"
    finally:
        g.quiesce.release()
    assert _lease(L) == E0
    assert IH.quiesce_step("linux", store=L).call() == (True, "no install run is quiescing this store")


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_setup_holds_after_the_pool_lock_row_and_before_every_slot_state(host):
    plan = _PLAN(parse_install_argv(SETUP), Gathered(_facts(host)))
    whys = [s.why for s in plan.steps]
    hold = next(i for i, w in enumerate(whys) if w.startswith("hold ") and w.endswith("(no prepare leases beneath it)"))
    made = next(i for i, w in enumerate(whys) if w.startswith("create ") and w.endswith("/slots/pool.lock"))
    first_state = next(i for i, w in enumerate(whys) if w.startswith("mark ") and "reusable" in w)
    assert made < hold < first_state


# --- REPLENISH's configured pool (codex r1 g2 6, head ruling 8) --------------------------------------------------------


def test_replenish_refills_a_retired_slot_on_a_new_index(tmp_path):
    """8 configured, e3 retired: replenish creates exactly ONE egress slot, at a new index (8), and no verifier. Red
    with the target taken from the slots in service (0 created), and with the retired index not spent (e3 again)."""
    slots = [SlotId("egress", i) for i in range(8)] + [SlotId("none", 0)]
    L = _store(tmp_path, *slots, pool=(8, 1), retired=(SlotId("egress", 3),))
    g = _gather(L, tmp_path, ["replenish"])
    g.quiesce.release()
    assert g.pool == (8, 1) and g.live["egress"] == (0, 1, 2, 4, 5, 6, 7)
    g = dataclasses.replace(g, facts=dataclasses.replace(g.facts, tool_present=lambda p: True))
    plan = plan_for(parse_install_argv(["replenish"]), g)
    assert plan.allocation is not None and plan.allocation.slots == (SlotId("egress", 8),)


def test_replenish_without_the_pool_record_refuses_by_name(tmp_path):
    L = _store(tmp_path, E0)
    g = _gather(L, tmp_path, ["replenish"])
    g.quiesce.release()
    assert g.pool is None
    with pytest.raises(Refused) as ei:
        plan_for(parse_install_argv(["replenish"]), g)
    assert ei.value.name == "pool-config-missing"
    # The shared refusals still come first.
    live = dataclasses.replace(g, facts=dataclasses.replace(g.facts, live_leases=1))
    with pytest.raises(Refused) as ei:
        plan_for(parse_install_argv(["replenish"]), live)
    assert ei.value.name == "live-lease"


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_setup_writes_the_pool_record_and_undo_removes_it(host):
    plan = _PLAN(parse_install_argv(SETUP), Gathered(_facts(host)))
    writes = [s.write for s in plan.steps if s.write and s.write[0] == pool_config_path(host)]
    assert writes == [(pool_config_path(host), pool_config_text(2, 1), 0o644)]
    assert canon.parse(writes[0][1].encode()) == {"format": POOL_CONFIG_FORMAT, "slots": 2, "verifier_slots": 1}
    assert pool_config_path(host) in {r.path for r in plan.rows}
    undo = _PLAN(parse_install_argv(["undo"]), Gathered(_set_up(host),
                                                        accounts={"_levain_s0": RANGES[host].slots[0]}))
    whys = [s.why for s in undo.steps]
    rm = [i for i, s in enumerate(undo.steps) if s.argv and s.argv[1:] == ("-f", str(pool_config_path(host)))]
    assert len(rm) == 1
    assert rm[0] > max(i for i, w in enumerate(whys) if w.startswith("remove ") and w.endswith("(under the pool lock)"))


@pytest.mark.parametrize("bad", [b"", b"{}", b'{"format":"levain-pool-config/1","slots":1}',
                                 b'{"format":"levain-pool-config/2","slots":1,"verifier_slots":0}',
                                 b'{"format":"levain-pool-config/1","slots":-1,"verifier_slots":0}',
                                 b'{"format":"levain-pool-config/1","slots":true,"verifier_slots":0}',
                                 b'{"format":"levain-pool-config/1","slots":1,"verifier_slots":0,"x":1}'])
def test_a_malformed_pool_record_refuses(bad):
    with pytest.raises(Refused) as ei:
        parse_pool_config(bad)
    assert ei.value.name == "pool-config-malformed"
    assert parse_pool_config(pool_config_text(8, 2).encode()) == (8, 2)


def test_the_pool_record_row_is_root_0644():
    row = pool_config_row("linux")
    assert (row.path, row.owner, row.group, row.mode, row.kind) == (pool_config_path("linux"), "root", "root", 0o644,
                                                                     "file")
