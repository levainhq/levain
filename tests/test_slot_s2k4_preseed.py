"""S1 session 2 fix K4: render (3) writes the onboarding pre-seed INTO the slot-owned ``<S>/config/<vendor>``.

The tmp-store World records owners rather than applying them, so every dir is ours. Here the store's ``trusted``
seam stands in for root ownership honestly: a dir passes it only if root would own it (no recorded chown to a
non-root uid), so ``config/claude`` / ``config/codex`` fail it exactly as they fail ``_root_trusted`` on the VM
(1019-s1design runs/s2-linux raw X4b: ``bad-root: .../config/claude``). The owner check on those dirs is NOT
replaced: it is the real ``fstat`` against the uid the alloc names, which a test can satisfy only by naming ours.
What only a VM run can show: the same pass with a real root-owned chain and a real slot uid.
"""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from levain.contracts import canon
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot.ids import SlotId
from tests.test_slot_s2f2_runtime_helper import BOOT, DISPATCH_UID, OP_UID, World, _blob

ME = os.getuid()
ONBOARD = "config/claude/.claude.json"


def _root_world(tmp_path) -> World:
    """A World whose ``trusted`` admits a dir only when root would own it, and whose ``slot_owned`` is the REAL
    default."""
    world = World(tmp_path)
    not_root: set[tuple[int, int]] = set()
    chowns = world.chowns

    def chown(fd, u, g, rel):
        chowns.append((rel, u, g))
        if u != 0:
            st = os.fstat(fd)
            not_root.add((st.st_dev, st.st_ino))

    def trusted(st):
        return st.st_uid == ME and not st.st_mode & 0o022 and (st.st_dev, st.st_ino) not in not_root

    world.seams.chown = chown
    world.seams.trusted = trusted
    world.seams.slot_owned = H._slot_owned
    return world


def _prepared_session(world: World, slot_uid: int | None = None) -> str:
    """A prepared egress session; ``slot_uid`` rewrites the alloc to name that uid as the slot's (prepare itself
    refuses a uid outside §1.1's ranges, so a test names ours only through the alloc root reads)."""
    line = world.prepare()
    assert line["ok"], line
    s = line["session"]
    world.stage(s)
    a = world.alloc(s)
    assert a.slot == SlotId("egress", 0)
    if slot_uid is not None:
        (world.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(replace(a, slot_uid=slot_uid,
                                                                             slot_gid=slot_uid)))
    return s


def _render(world: World, s: str, blob: bytes | None = None, uid: int = OP_UID) -> dict:
    return world.main(["render", s], blob or _blob(), uid=uid)[1]


def test_old_trusted_check_refuses_the_slot_dir(tmp_path):
    """The VM defect, reproduced: the slot-owned dir is not root's, so the root-only check refuses it."""
    world = _root_world(tmp_path)
    s = _prepared_session(world, ME)
    store = H.Store(world.seams)
    try:
        with pytest.raises(H.Refused) as e:
            store.open_dir("live", s, "config", "claude")
        assert e.value.name == "bad-root"
    finally:
        store.close()


def test_preseed_written_when_the_alloc_names_the_dir_owner(tmp_path):
    world = _root_world(tmp_path)
    s = _prepared_session(world, ME)
    assert world.alloc(s).slot_uid == ME
    assert _render(world, s) == {"ok": True}, world.last_err
    data = (world.L / "live" / s / ONBOARD).read_bytes()
    assert canon.parse(data)["hasCompletedOnboarding"] is True
    assert world.chown_of(f"live/{s}/{ONBOARD}") == (ME, ME)
    assert world.mode(f"live/{s}/{ONBOARD}") == 0o600


def test_preseed_refused_when_the_alloc_names_another_uid(tmp_path):
    """The dir is ours; the alloc's slot uid is 899: never "any slot account", exactly the alloc's."""
    world = _root_world(tmp_path)
    s = _prepared_session(world)
    assert world.alloc(s).slot_uid == 899
    assert _render(world, s) == {"ok": False, "refused": "bad-root"}
    assert "config/claude" in world.last_err
    assert not (world.L / "live" / s / ONBOARD).exists()


def test_preseed_refused_in_a_group_writable_dir(tmp_path):
    world = _root_world(tmp_path)
    s = _prepared_session(world, ME)
    os.chmod(world.L / "live" / s / "config" / "claude", 0o770)
    assert _render(world, s) == {"ok": False, "refused": "bad-root"}
    assert not (world.L / "live" / s / ONBOARD).exists()


def test_preseed_refused_through_a_symlinked_dir(tmp_path):
    world = _root_world(tmp_path)
    s = _prepared_session(world, ME)
    cfg = world.L / "live" / s / "config"
    os.rename(cfg / "claude", cfg / "elsewhere")
    os.symlink("elsewhere", cfg / "claude")
    assert _render(world, s) == {"ok": False, "refused": "bad-root"}
    assert not (cfg / "elsewhere" / ".claude.json").exists()


def test_preseed_never_replaces_an_existing_file(tmp_path):
    world = _root_world(tmp_path)
    s = _prepared_session(world, ME)
    planted = world.L / "live" / s / ONBOARD
    planted.write_bytes(b"planted")
    assert _render(world, s) == {"ok": False, "refused": "render-dest-exists"}
    assert planted.read_bytes() == b"planted"


def test_preseed_refused_on_a_planted_symlink_file(tmp_path):
    world = _root_world(tmp_path)
    s = _prepared_session(world, ME)
    target = tmp_path / "victim"
    target.write_bytes(b"victim")
    os.symlink(target, world.L / "live" / s / ONBOARD)
    assert _render(world, s) == {"ok": False, "refused": "render-dest-exists"}
    assert target.read_bytes() == b"victim"


def _verifier(world: World, *, slot_uid: int, child_uid: int) -> str:
    """A ticketed verifier alloc + record + dirs, as session 2f2's none-class render test builds them."""
    s = "s-00000000000000c4"
    slot = SlotId("none", 0)
    ticket = C.new_handle()
    a = R.Alloc(session=s, slot=slot, lease_seq=1, slot_rev=1, slot_uid=slot_uid, slot_gid=slot_uid,
                kind="verifier", vendor="claude", auth="none", operator_uid=OP_UID, supervisor_uid=DISPATCH_UID,
                staging_uid=OP_UID, created_at="2026-10-20T00:00:00Z", boot_id=BOOT, child_uid=child_uid,
                ticket=ticket, netns=slot.netns, cgroup=R.cgroup_path(slot, s))
    (world.L / "alloc" / f"{s}.json").write_bytes(R.alloc_bytes(a))
    claims = {"operator_uid": OP_UID, "supervisor_uid": DISPATCH_UID, "slot": R.slot_obj(slot), "class": "none",
              "kind": "verifier", "vendor": "claude", "auth": "none", "lease_seq": 1, "slot_rev": 1,
              "ticket": ticket}
    world.control.store.put(R.SessionState(session=s, generation=0, state=R.ALLOCATED, claims=claims,
                                           lease_live=True, event_kind="state", updated_at="2026-10-20T00:00:00Z"))
    world.control._session_lease[s] = C.Lease(C.new_handle(), DISPATCH_UID, used=True, session=s)
    store = H.Store(world.seams)
    try:
        ctx = H.Ctx(world.seams, store, DISPATCH_UID, "dispatch", OP_UID, {})
        H.make_session_dirs(ctx, s, H.session_dir_rows(slot, {"_levain_v0": slot_uid, "_levain_c0": child_uid}))
    finally:
        store.close()
    (world.L / "staging" / s).mkdir()
    world.stage(s)
    return s


_VBLOB = dict(routes=[], verifier_steps=[{"argv": ["pytest", "-q"], "wall_s": 60}], driver="pytest-report")


def test_verifier_preseed_owner_is_the_child(tmp_path):
    world = _root_world(tmp_path)
    s = _verifier(world, slot_uid=893, child_uid=ME)
    assert _render(world, s, _blob(**_VBLOB), uid=DISPATCH_UID) == {"ok": True}, world.last_err
    assert world.chown_of(f"live/{s}/{ONBOARD}") == (ME, ME)


def test_verifier_preseed_refused_when_only_the_driver_is_the_owner(tmp_path):
    """The dir is ours and the alloc names us as the DRIVER (slot uid), not the child: refused."""
    world = _root_world(tmp_path)
    s = _verifier(world, slot_uid=ME, child_uid=892)
    assert _render(world, s, _blob(**_VBLOB), uid=DISPATCH_UID) == {"ok": False, "refused": "bad-root"}
    assert not (world.L / "live" / s / ONBOARD).exists()
