"""Row 33 R1b, the undo half (DESIGN 1022-8/upgrade §5 R1b; CONTRACT §23 undo order).

Undo walks ``record.managed ∪ journal.managed`` plus its own desired set through the SAME per-identity remove steps
repair runs, with ``retire=False``, in §23's order: the runtime sudoers rule first, every unit stopped before any unit
file goes, slot state and pool config, the account ids retired, each account's processes ended and the account
tombstoned, the store dirs given back to root's group, the memberships and groups, slot confinement, the install
record, the install sudoers rule (the last removal), the journal, the operator record.

* ``test_undo_removes_recorded_names_in_section_23_order``: a record naming a unit, a drop-in and a group this code
  no longer ships; each goes by its recorded identity, in order (red against the bundles, which cannot name them).
* ``test_undo_over_the_same_record_retires_no_gid``: the recorded group is deleted and its gid is NOT retired (§1.1 g:
  only repair retires a leaving gid); the retired ids undo writes are the account uids only.
"""
from __future__ import annotations

from pathlib import Path

from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import pf, units
from levain.slot.accounts import SLOT_MARKER
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, RETIRED_IDS_PATH, SlotId, loopback_alias, read_retired_ids
from levain.slot.sudoers import INSTALL_SUDOERS, RUNTIME_SUDOERS

from tests.test_repair_remove import GID, LGID, SUDOERS_DIR, _host, _plist, _put

OP, OP_UID = "phill", 501
S0 = 3999                                     # _levain_s0's uid (darwin slot range)


def _facts(host: str, shape: G.InstallShape | None) -> IH.HostFacts:
    return IH.HostFacts(host=host, caller_uid=OP_UID, caller_name=OP,
                        operator_record={"format": "levain-operator/1", "name": OP, "uid": OP_UID},
                        install_shape=shape, tool_present=lambda p: True, xcode_ready=lambda: True)


def _shape(*managed) -> G.InstallShape:
    return G.InstallShape("gen-" + "0" * 16, "0.11.14", "0.16.0", "c" * 32, {}, tuple(managed))


def _darwin_host(tmp: Path):
    h, live = _host(tmp, "darwin")
    slots_gid = RANGES["darwin"].shared[0]
    _put(live, units.LAUNCHD_DIR / "com.levain.old-svc.plist", _plist("old-svc"))
    _put(live, units.LAUNCHD_DIR / "com.levain.qbroker.plist", _plist("qbroker"))
    h.loaded |= {"com.levain.old-svc", "com.levain.qbroker"}
    for d in (RUNTIME_SUDOERS, INSTALL_SUDOERS, SUDOERS_DIR / "levain-old"):
        _put(live, d)
    h.users["_levain_s0"] = [S0, SLOT_MARKER]
    h.groups["levain-slots"] = [slots_gid, SLOT_MARKER, {"_levain_s0"}]
    h.groups["levain-gone"] = [GID, SLOT_MARKER, {OP}]
    h.aliases.add(loopback_alias(SlotId("egress", 0)))
    _put(live, pf.ANCHOR_FILE)
    h.rules[pf.ANCHOR] = True
    _put(live, pf.PF_TOKEN, b"12345\n")
    return h, live, {"levain-slots": slots_gid}


RECORD = (("unit/1", "old-svc"), ("sudoers/1", "levain-old"), ("group/1", ("levain-gone", GID)),
          ("member/1", (OP, "levain-gone", GID)), ("account/1", ("_levain_s0", S0)))


def _undo(tmp: Path):
    h, live, shared = _darwin_host(tmp)
    plan = IH.plan_undo(parse_install_argv(["undo"]), _facts("darwin", _shape(*RECORD)),
                        accounts={"_levain_s0": S0}, shared_groups=shared, live=live, end=lambda uid, name: (True, ""))
    events: list = []
    for s in plan.steps:
        before = len(h.log)
        if s.call is not None and s.write is None:
            ok, why = s.call()
            assert ok, (s.why, why)
            events += h.log[before:]
        else:
            events.append(("plain", s.why))
    return h, live, plan, events


def _first(events, pred) -> int:
    return next(i for i, e in enumerate(events) if pred(e))


def _last(events, pred) -> int:
    return max(i for i, e in enumerate(events) if pred(e))


def test_undo_removes_recorded_names_in_section_23_order(tmp_path: Path) -> None:
    h, live, plan, ev = _undo(tmp_path)
    rm_of = lambda p: (lambda e: e[0].endswith("/rm") and e[-1] == str(live.at(p)))   # noqa: E731
    runtime, old_drop = _first(ev, rm_of(RUNTIME_SUDOERS)), _first(ev, rm_of(SUDOERS_DIR / "levain-old"))
    install = _first(ev, rm_of(INSTALL_SUDOERS))
    bootouts = [i for i, e in enumerate(ev) if e[0].endswith("launchctl") and e[1] == "bootout"]
    plist_rms = [i for i, e in enumerate(ev) if e[0].endswith("/rm") and e[-1].endswith(".plist")]
    assert ("/bin/launchctl", "bootout", "system/com.levain.old-svc") in ev       # the recorded name, not SERVICES
    assert runtime < min(bootouts) and old_drop < min(bootouts)                   # grants first
    assert max(bootouts) < min(plist_rms)                                         # every stop before any unit file
    tomb = _first(ev, lambda e: e[0].endswith("dscl") and "RealName" in e)
    ungroup = _first(ev, lambda e: e[0] == "plain" and e[1].startswith("give "))
    member = _first(ev, lambda e: e[0].endswith("dseditgroup") and e[4] == OP and e[-1] == "levain-gone")
    gone = _first(ev, lambda e: e[0].endswith("dscl") and e[-1] == "/Groups/levain-gone")
    slots = _first(ev, lambda e: e[0].endswith("dscl") and e[-1] == "/Groups/levain-slots")
    assert tomb < ungroup < member < gone and ungroup < slots
    alias = _first(ev, lambda e: e[0].endswith("ifconfig") and "-alias" in e)
    flush = _first(ev, lambda e: e[0].endswith("pfctl") and e[-2:] == ("-F", "all"))
    assert max(gone, slots) < alias < flush < install
    assert install == _last(ev, lambda e: e[0].endswith("/rm") and str(SUDOERS_DIR) in e[-1])
    tail = [e[1] for e in ev[install + 1:] if e[0] == "plain"]
    assert tail[-2:] == [IH.intent_remove_step("darwin").why, "remove the operator record (last)"]
    # the host after: nothing the record or undo's own desired set named is left
    assert not h.loaded and not h.aliases and not h.rules[pf.ANCHOR]
    assert "levain-gone" not in h.groups and "levain-slots" not in h.groups
    assert not live.at(SUDOERS_DIR / "levain-old").exists() and not live.at(INSTALL_SUDOERS).exists()
    assert not live.at(pf.PF_TOKEN).exists() and not live.skips


def test_undo_over_the_same_record_retires_no_gid(tmp_path: Path) -> None:
    h, live, plan, ev = _undo(tmp_path)
    assert "levain-gone" not in h.groups
    assert GID not in plan.retired_after and S0 in plan.retired_after
    assert GID not in read_retired_ids(live.at(RETIRED_IDS_PATH["darwin"]))      # no durable retire either
    assert not [e for e in ev if e == ("fsync",)]


def test_undo_linux_confinement_after_the_tombstones(tmp_path: Path, monkeypatch) -> None:
    from levain.firing import hands
    from levain.slot import bwrap

    real = hands.shutil.which                   # a Linux plan built here names tools this host lacks
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")

    h, live = _host(tmp_path, "linux")
    lslots = RANGES["linux"].shared[0]
    s0 = RANGES["linux"].slots[0]
    h.users["_levain_s0"] = [s0, SLOT_MARKER]
    h.groups["levain-slots"] = [lslots, SLOT_MARKER, {"_levain_s0"}]
    h.netns.add(SlotId("egress", 0).netns)
    _put(live, bwrap.LEVAIN_BWRAP)
    _put(live, bwrap.BWRAP_DIGEST)
    _put(live, bwrap.APPARMOR_LOADED, b"")
    shape = _shape(("group/1", ("levain-gone", LGID)), ("account/1", ("_levain_s0", s0)))
    h.groups["levain-gone"] = [LGID, SLOT_MARKER, set()]
    plan = IH.plan_undo(parse_install_argv(["undo"]), _facts("linux", shape), accounts={"_levain_s0": s0},
                        shared_groups={"levain-slots": lslots}, live=live, end=lambda uid, name: (True, ""))
    ev: list = []
    for s in plan.steps:
        before = len(h.log)
        if s.call is not None and s.write is None:
            assert s.call()[0], s.why
            ev += h.log[before:]
        else:
            ev.append(("plain", s.why))
    tomb = _first(ev, lambda e: e[0].endswith("usermod") and "--comment" in e)
    gdel = _last(ev, lambda e: e[0].endswith("groupdel"))
    ns = _first(ev, lambda e: e[0].endswith("/ip") and e[1:3] == ("netns", "delete"))
    copy = _first(ev, lambda e: e[0].endswith("/rm") and e[-1] == str(live.at(bwrap.LEVAIN_BWRAP)))
    assert tomb < gdel < copy and gdel < ns
    assert "levain-gone" not in h.groups and not h.netns and not live.at(bwrap.LEVAIN_BWRAP).exists()
    assert LGID not in plan.retired_after


def test_the_tombstone_callback_is_routed_by_type_not_text(tmp_path: Path, monkeypatch) -> None:
    """L3 r2 complement LOW-1: the leave-groups callback is found by its type; a reworded ``why`` still routes to the
    live body (red against matching ``s.why`` text, which raised on the reworded step)."""
    from levain.slot import accounts as A

    real = A.tombstone_steps

    def reworded(host, name):
        return [A.LeaveGroupsStep("leave every group", call=s.call) if isinstance(s, A.LeaveGroupsStep) else s
                for s in real(host, name)]

    monkeypatch.setattr(A, "tombstone_steps", reworded)
    h, live = _host(tmp_path, "darwin")
    h.users["_levain_s0"] = [S0, SLOT_MARKER]
    h.groups["levain-slots"] = [RANGES["darwin"].shared[0], SLOT_MARKER, {"_levain_s0"}]
    steps = A.remove_account_steps("darwin", "_levain_s0", S0, live=live, end=lambda u, n: (True, ""), retire=False)
    assert all(s.call()[0] for s in steps)
    assert ("/usr/sbin/dseditgroup", "-o", "edit", "-d", "_levain_s0", "-t", "user", "levain-slots") in h.log
