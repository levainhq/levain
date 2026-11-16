"""Host grants (board levain#66 slice S2, plan hgs2_plan_v2 "Teardown and render removal", T15 and T26): the grants file
is removed by root's ``teardown`` verb right after ``revoke`` (under the session lock, a bounded wait), by
``scrub_and_release`` (the reaper's path too) and by ``render`` (a failure there refuses the render), and a grant that is
mid-publish when teardown runs is deleted after it publishes, never left behind."""
from __future__ import annotations

import errno
import fcntl
import os
import threading

import pytest

from levain.slot import Refused
from levain.slot import egress_store as ES
from levain.slot import records as R
from levain.slot import runtime_grammar as RG
from levain.slot import runtime_helper as RH
from levain.slot import start as SS
from tests.test_slot_egress_grant_verbs import (  # noqa: F401 - fixtures and helpers of the verb tests
    FRONT_UID, S, SLOT, UID, World, entries, gid_for, ok, raw_message, refused, tap, verifier, world)


def ctx_of(w):
    return RH.Ctx(w.seams, RH.Store(w.seams), UID, RG.OPERATOR, UID, {})


def alloc_of(w):
    return R.parse_alloc((w.L / "alloc" / f"{S}.json").read_bytes())


def put_file(w, name="grants.json", data=b"{}"):
    (w.slot_dir / name).write_bytes(data)


# --- the store's removal ---------------------------------------------------------------------------------------------------

def test_remove_all_unlinks_the_file_and_every_stale_temp_and_nothing_else(world):
    put_file(world)
    put_file(world, "grants.json." + "ab" * 8 + ".new")
    put_file(world, "grants.json." + "cd" * 8 + ".new")
    put_file(world, "other")
    assert ES.remove_all(ctx_of(world), SLOT) == 3
    assert sorted(p.name for p in world.slot_dir.iterdir()) == ["other"]


def test_remove_all_with_nothing_there_or_no_dir_is_not_an_error(world):
    assert ES.remove_all(ctx_of(world), SLOT) == 0
    world.slot_dir.rmdir()
    assert ES.remove_all(ctx_of(world), SLOT) == 0
    (world.R / "grants").rmdir()
    assert ES.remove_all(ctx_of(world), SLOT) == 0


def test_remove_all_syncs_the_directory_when_it_removed_something(world):
    put_file(world)
    ES.remove_all(ctx_of(world), SLOT)
    assert world.fsyncs, "the dir was not synced after the unlink"


def test_remove_all_refuses_a_slot_dir_that_is_not_roots_and_leaves_it(world):
    put_file(world)
    world.seams = __import__("dataclasses").replace(world.seams, chowned_uid=lambda uid: os.getuid() + 1)
    with pytest.raises(Refused) as e:
        ES.remove_all(ctx_of(world), SLOT)
    assert e.value.name == ES.DIR_UNTRUSTED
    assert (world.slot_dir / "grants.json").exists()


def test_remove_all_does_not_follow_a_symlinked_slot_dir(world, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "grants.json").write_bytes(b"x")
    world.slot_dir.rmdir()
    world.slot_dir.symlink_to(elsewhere)
    with pytest.raises(Refused):
        ES.remove_all(ctx_of(world), SLOT)
    assert (elsewhere / "grants.json").exists()


# --- the bounded session-lock wait -------------------------------------------------------------------------------------------

def _hold(w):
    fd = os.open(w.L / "alloc" / RH.session_lock_name(S), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_the_session_lock_with_a_wait_takes_it_when_the_holder_lets_go_inside_the_wait(world):
    fd = _hold(world)
    ticks = []

    def sleep(s):
        ticks.append(s)
        if len(ticks) == 2:
            os.close(fd)

    world.seams = __import__("dataclasses").replace(world.seams, sleep=sleep)
    with RH.session_lock(ctx_of(world), S, wait_s=5.0):
        pass
    assert len(ticks) == 2


def test_the_session_lock_with_a_wait_gives_up_as_session_busy_after_the_wait(world):
    fd = _hold(world)
    now = [0.0]
    world.seams = __import__("dataclasses").replace(
        world.seams, monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + 1.0))
    try:
        with pytest.raises(Refused) as e:
            with RH.session_lock(ctx_of(world), S, wait_s=3.0):
                raise AssertionError("took a held lock")
    finally:
        os.close(fd)
    assert e.value.name == "session-busy" and now[0] >= 3.0


def test_the_session_lock_without_a_wait_still_tries_once(world):
    fd = _hold(world)
    try:
        with pytest.raises(Refused) as e:
            with RH.session_lock(ctx_of(world), S):
                pass
    finally:
        os.close(fd)
    assert e.value.name == "session-busy"


# --- teardown and scrub call the removal (the real verbs, in the fake-process-table world) ----------------------------------

from tests.test_slot_s2f3_start import GW0, _rendered, env  # noqa: E402,F401 - the fake-process-table world


def _grants_dir(w, n=0):
    g = w.R / "grants"
    g.mkdir(exist_ok=True, mode=0o755)
    d = g / f"slot{n}"
    d.mkdir(exist_ok=True, mode=0o750)
    return d


def _front_alive(w, table):
    for pid in w.fronts.alive():
        table.add(pid, GW0)


def test_teardown_removes_the_grants_file_and_the_session_is_retained(env):
    w, _la, table = env
    s = _rendered(w)
    _front_alive(w, table)
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"{}")
    (d / ("grants.json." + "ab" * 8 + ".new")).write_bytes(b"x")
    code, line = w.main(["teardown", s])
    assert line["ok"] and line["state"] == R.RETAINED, line
    assert list(d.iterdir()) == []


def test_teardown_removes_the_file_straight_after_revoke_and_before_end(env):
    w, _la, table = env
    s = _rendered(w)
    _front_alive(w, table)
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"{}")
    seen = {}
    w.on_step = lambda step: seen.setdefault(step, (d / "grants.json").exists())
    w.main(["teardown", s])
    assert seen["end"] is False, seen      # gone before END starts: the removal follows revoke() directly


def test_a_lock_wait_that_times_out_does_not_stop_teardown_and_scrub_still_removes_it(env, monkeypatch):
    w, _la, table = env
    s = _rendered(w)
    _front_alive(w, table)
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"{}")
    real = RH.session_lock
    calls = []

    def lock(ctx, session, wait_s=None):
        calls.append(wait_s)
        if len(calls) == 1:
            raise Refused("session-busy", session)
        return real(ctx, session, wait_s)

    monkeypatch.setattr(RH, "session_lock", lock)
    code, line = w.main(["teardown", s])
    assert line["ok"] and line["state"] == R.RETAINED, line
    assert not (d / "grants.json").exists()
    assert calls[0] is not None and calls[0] > 0       # a bounded WAIT, never the try


def test_scrub_and_release_removes_the_file_for_the_reaper_too(env, monkeypatch):
    w, _la, table = env
    s = _rendered(w)
    _front_alive(w, table)
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"{}")
    real = SS.drop_session_grants
    calls = []

    def flaky(ctx, a):
        calls.append(a.session)
        if len(calls) == 1:
            raise Refused("session-busy", "x")          # teardown's own removal fails ...
        return real(ctx, a)

    monkeypatch.setattr(SS, "drop_session_grants", flaky)
    code, line = w.main(["teardown", s])
    assert line["ok"], line
    assert len(calls) == 2 and not (d / "grants.json").exists()   # ... and scrub's own call is the one that took it


def test_a_scrub_removal_failure_is_logged_and_the_slot_is_still_released(env, monkeypatch, capsys):
    w, _la, table = env
    s = _rendered(w)
    _front_alive(w, table)
    _grants_dir(w)

    def boom(ctx, slot):
        raise OSError(errno.EIO, "disk")

    monkeypatch.setattr(ES, "remove_all", boom)
    code, line = w.main(["teardown", s])
    assert line["ok"] and line["state"] == R.RETAINED and "released" not in line, line
    assert b"reusable" in (w.L / "slots" / "e0" / "state").read_bytes()


def test_a_retried_teardown_after_the_slot_was_leased_again_leaves_the_new_sessions_file(env):
    """L3 r1 (codex 2): A is RETAINED and released, the slot goes to session B, B publishes; a retried ``teardown A``
    (``revoke`` a no-op, A's own session lock) must not delete B's file. The removal is bound to the slot's CURRENT
    lease, read under the slot lock."""
    import dataclasses
    w, _la, table = env
    s = _rendered(w)
    _front_alive(w, table)
    code, line = w.main(["teardown", s])
    assert line["ok"] and line["state"] == R.RETAINED, line
    path = w.L / "slots" / "e0" / "state"
    st = RH.parse_slot_state_of(path.read_bytes(), alloc_of_session(w, s).slot)
    assert st.state == "reusable"
    other = "s-" + "ef" * 8
    path.write_bytes(R.slot_state_bytes(dataclasses.replace(st, state="leased", session=other,
                                                            lease_seq=st.lease_seq + 1, rev=st.rev + 1)))
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"B's grants")
    (d / ("grants.json." + "ab" * 8 + ".new")).write_bytes(b"B's staged temp")
    w.main(["teardown", s])
    assert sorted(p.name for p in d.iterdir()) == ["grants.json", "grants.json." + "ab" * 8 + ".new"]


def test_the_teardown_removal_skips_a_slot_whose_state_names_another_lease(world):
    import dataclasses
    a = alloc_of(world)
    st = R.SlotState(slot=SLOT, rev=3, lease_seq=a.lease_seq + 1, state="leased", dispatch_eligible=False,
                     changed_at="2026-11-16T11:00:00Z", session="s-" + "ef" * 8)
    (world.L / "slots" / "e3" / "state").write_bytes(R.slot_state_bytes(st))
    (world.L / "slots" / "e3" / "lock").write_bytes(b"")
    put_file(world)
    SS.drop_session_grants_of_lease(ctx_of(world), a)
    assert (world.slot_dir / "grants.json").exists()
    (world.L / "slots" / "e3" / "state").write_bytes(R.slot_state_bytes(
        dataclasses.replace(st, lease_seq=a.lease_seq, session=a.session)))
    SS.drop_session_grants_of_lease(ctx_of(world), a)
    assert not (world.slot_dir / "grants.json").exists()


def alloc_of_session(w, s):
    return R.parse_alloc((w.L / "alloc" / f"{s}.json").read_bytes())


def test_a_none_class_slot_is_never_touched(env):
    w, _la, _table = env
    d = _grants_dir(w, 0)
    (d / "grants.json").write_bytes(b"{}")
    # the removal is for egress slots only: a verifier pair (class none) has no grants dir and no call
    from levain.slot.ids import SlotId
    fake = type("A", (), {"slot": SlotId("none", 0), "session": S})()
    SS.drop_session_grants(ctx_of(w), fake)
    assert (d / "grants.json").exists()


# --- render removes a stale file before the front's policy is written -------------------------------------------------------

def test_render_removes_an_old_sessions_file_before_the_front_starts(env):
    w, _la, _table = env
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"{}")
    s = _rendered(w)
    assert not (d / "grants.json").exists() and s


def test_a_render_whose_removal_fails_refuses_and_writes_no_front_policy(env, monkeypatch):
    from tests.test_slot_s2f2_runtime_helper import _blob, _prepared
    w, _la, _table = env
    d = _grants_dir(w)
    (d / "grants.json").write_bytes(b"{}")
    s = _prepared(w)
    w.session = s

    def boom(ctx, slot):
        raise OSError(errno.EIO, "disk")

    monkeypatch.setattr(ES, "remove_all", boom)
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": False, "refused": "bad-root"}, line
    assert not (w.R / "gw" / "slot0" / "session.json").exists()


def test_render_with_no_grants_dir_yet_is_unchanged(env):
    w, _la, _table = env
    assert not (w.R / "grants").exists()
    assert _rendered(w)


# --- T26: a grant paused after its ACTIVE check holds the lock; teardown's removal waits and deletes the file ------------------

def test_teardown_race_the_removal_waits_for_a_grant_mid_publish_and_then_deletes_what_it_published(world, verifier):
    gate, entered = threading.Event(), threading.Event()
    done = {}
    paused = {"n": 0}

    def removal():
        world.set_state(R.REVOKED)          # teardown's revoke(): CONTROL CAS to REVOKED
        ctx = ctx_of(world)
        with RH.session_lock(ctx, S, wait_s=10.0):
            ES.remove_all(ctx, SLOT)
        done["removed"] = True

    def hook(step):
        if step == "egress-grant:stage" and paused["n"] == 0:
            paused["n"] += 1
            t = threading.Thread(target=removal)
            done["t"] = t
            t.start()
            entered.set()
            assert gate.wait(10)

    world.hook = hook
    out = {}
    t = threading.Thread(target=lambda: out.update(line=tap(world, "files.example.org", "s")))
    t.start()
    assert entered.wait(10)
    gate.set()
    t.join(10)
    done["t"].join(10)
    assert out["line"][0] == RH.EXIT_OK, out           # the grant published (it held the lock first) ...
    assert done.get("removed") and entries(world) is None   # ... and the removal then deleted it


def test_a_revoke_before_the_active_check_refuses_the_grant_not_active(world, verifier):
    world.set_state(R.REVOKED)
    refused(tap(world, "files.example.org", "s"), "egress-grant-not-active")
    assert entries(world) is None
