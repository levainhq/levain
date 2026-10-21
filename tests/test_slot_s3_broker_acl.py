"""§26 S1-c: a dispatch session's ``<S>`` gives the queue broker search (one ACL entry) and its ``repo`` is 0711, so
the broker can walk into the run's worktree and nothing else under ``<S>``. The local run that preceded these
(levain-memory 1019-s1design/runs/s3-broker-acl/RUN_local.txt) set the macOS entry on a real dir; the cross-uid walk
and the Linux kernel's acceptance of the xattr are VM rows, owed."""
from __future__ import annotations

import struct

import pytest

from levain.slot import Refused
from levain.slot import runtime_helper as H
from levain.slot.ids import SlotId
from tests.test_slot_s2f2_runtime_helper import OP_UID, World

ACL_TAGS = {0x01: "user_obj", 0x02: "user", 0x04: "group_obj", 0x10: "mask", 0x20: "other"}


def decode(blob: bytes) -> list[tuple[str, int, int]]:
    (version,) = struct.unpack_from("<I", blob)
    assert version == 2 and (len(blob) - 4) % 8 == 0
    return [(ACL_TAGS[t], p, i) for t, p, i in struct.iter_unpack("<HHI", blob[4:])]


def test_linux_acl_is_the_mode_plus_one_search_entry():
    """What the local run printed, decoded: the mode's own three classes, the broker with --x only, and a mask that
    keeps <S>'s group bits (so its 0750 reads 0750)."""
    got = decode(H.linux_search_acl(0o750, 3700))
    U = 0xFFFFFFFF
    assert got == [("user_obj", 7, U), ("user", 1, 3700), ("group_obj", 5, U), ("mask", 5, U), ("other", 0, U)]
    assert H.linux_search_acl(0o750, 501).hex() == (
        "0200000001000700ffffffff02000100f501000004000500ffffffff10000500ffffffff20000000ffffffff")


def _dirs(world: World, kind: str):
    s = "s-00000000000000aa"
    granted = []
    world.seams.grant_search = lambda host, fd, path, name, uid: granted.append((host, path, name, uid))
    world.seams.account_uid = lambda name: {"_levain_qbroker": 3701}[name]
    ctx = H.Ctx(world.seams, H.Store(world.seams), OP_UID, "operator", OP_UID, {})
    H.make_session_dirs(ctx, s, H.session_dir_rows(SlotId("egress", 0), {"_levain_s0": 3999, "_levain_gw0": 3998}),
                        broker_uid=world.seams.account_uid("_levain_qbroker") if kind == "dispatch" else None)
    return s, granted


def test_only_a_dispatch_session_grants_the_broker(tmp_path):
    w = World(tmp_path)
    s, granted = _dirs(w, "dispatch")
    assert granted == [("linux", w.L / "live" / s, "_levain_qbroker", 3701)]
    w2 = World(tmp_path / "b")
    _s, granted = _dirs(w2, "interactive")
    assert granted == []


def test_a_grant_that_cannot_be_set_refuses_the_session(tmp_path):
    w = World(tmp_path)

    def fail(*a):
        raise Refused("grant-reach-unset", "no ACL support")
    w.seams.grant_search = fail
    w.seams.account_uid = lambda name: 3701
    ctx = H.Ctx(w.seams, H.Store(w.seams), OP_UID, "operator", OP_UID, {})
    with pytest.raises(Refused) as e:
        H.make_session_dirs(ctx, "s-00000000000000ab",
                            H.session_dir_rows(SlotId("egress", 0), {"_levain_s0": 3999, "_levain_gw0": 3998}),
                            broker_uid=3701)
    assert e.value.name == "grant-reach-unset"


def test_macos_grant_failure_names_the_refusal(tmp_path):
    with pytest.raises(Refused) as e:
        H.grant_broker_search("darwin", -1, tmp_path / "absent", "_levain_qbroker", 3701)
    assert e.value.name == "grant-reach-unset"
