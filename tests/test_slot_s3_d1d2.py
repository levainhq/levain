"""D1 + D2 (levain-memory 1019-s1design/runs/s2-macos/RESULT_D1D2.md, ruled in s1_reviews/S2_L3_r2_TRIAGE.md
"VM-D1D2"), each test a reproduced VM failure: X9/X7 (getconf AS the slot uid after prepare re-created the
var-folders dir END had moved: HELD retain-conflict), X1/X6 (launchd's per-user agents of the uid survived a long
END: SCRUB refused until `launchctl bootout user/<uid>`), raw_d/998_undo.txt (undo rc 0 with a slot process
alive). The VM re-run on the fix: runs/s3-macos-d1d2/."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import install_helper as IH
from levain.slot import proc as P
from levain.slot import records as R
from levain.slot import start as ST
from tests.test_slot_s2a_records import _alloc_obj, _parse_obj
from tests.test_slot_s2f1_proc import CHILD, GW, SLOT, Table
from tests.test_slot_s2f2_runtime_helper import World, _blob, _prepared


# --- D1: the var-folders path is read once, at prepare -------------------------------------------------------------

def _darwin_world(tmp_path) -> World:
    w = World(tmp_path, host="darwin")
    acc = {"_levain_s0": 3999, "_levain_gw0": 3998, "_levain_dispatch": 3799}
    w.seams.account_uid = lambda name: acc[name]
    w.seams.group_gid = lambda g: {"levain-gwfronts": 3789}[g]
    w.getconf_calls = []

    def getconf(uid, gid):
        w.getconf_calls.append(uid)
        return f"/private/var/folders/zz/u{uid}"
    w.seams.var_folders = getconf
    return w


def test_prepare_stores_the_path_and_render_never_asks_again(tmp_path):
    w = _darwin_world(tmp_path)
    s = _prepared(w)
    assert w.alloc(s).var_folders == ("/private/var/folders/zz/u3999",)
    assert w.getconf_calls == [3999]

    def boom(uid, gid):
        raise AssertionError(f"getconf as uid {uid} after prepare")
    w.seams.var_folders = boom
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": True}, w.last_err
    assert "/private/var/folders/zz/u3999" in (w.L / "live" / s / "root" / "profile.sb").read_text()
    assert w.alloc(s).var_folders == ("/private/var/folders/zz/u3999",)          # render's rewrite keeps it


def test_end_reads_the_path_from_alloc_never_getconf(tmp_path):
    a = _parse_obj(_alloc_obj(var_folders=["/private/var/folders/zz/u5001"]))

    def boom(uid, gid):
        raise AssertionError("getconf at END")
    ctx = SimpleNamespace(seams=SimpleNamespace(host="darwin", var_folders=boom), store=SimpleNamespace(path=tmp_path))
    assert ST.end_facts(ctx, a).var_folders == Path("/private/var/folders/zz/u5001")
    with pytest.raises(Refused) as e:                     # an alloc that never read it: no getconf, a refusal
        ST.end_facts(ctx, _parse_obj(_alloc_obj()))
    assert e.value.name == "var-folders-unknown"


def test_linux_alloc_carries_no_path(tmp_path):
    w = World(tmp_path)
    s = _prepared(w)
    assert w.alloc(s).var_folders is None


@pytest.mark.parametrize("value,ok", [
    (["/private/var/folders/zz/u1"], True),
    ([], False),
    (["/private/var/folders/zz/u1", "/private/var/folders/zz/u2"], False),    # an egress slot runs one uid
    (["/var/folders/zz/u1"], False),
    (["/private/var/folders/../etc"], False),
    ("/private/var/folders/zz/u1", False),
])
def test_alloc_var_folders_shape(value, ok):
    obj = _alloc_obj(var_folders=value)
    if ok:
        assert _parse_obj(obj).var_folders == tuple(value)
        assert canon.parse(R.alloc_bytes(_parse_obj(obj)))["var_folders"] == value
    else:
        with pytest.raises(Refused):
            _parse_obj(obj)


# --- D2: SCRUB ends the uid's launchd domain before its proof -------------------------------------------------------

def _launchd_table():
    """X1's shape: the slot uid's per-user agents (ppid 1, launchd's) are still there at SCRUB; only the domain's
    bootout ends them."""
    t = Table()
    t.add(100, GW)
    for pid in (210, 211, 212, 213):                      # lsd, trustd, distnoted, secd
        t.add(pid, SLOT)
    t.ignore_kill.update({210, 211, 212, 213})
    calls = []

    def domain_out(uid, timeout):
        calls.append(("bootout", uid, sorted(p for p, q in t.procs.items() if uid in q.uids)))
        for pid in [p for p, q in t.procs.items() if uid in q.uids]:
            del t.procs[pid]
        return 0
    return t, calls, domain_out


def test_scrub_without_the_bootout_names_the_launchd_survivor():
    t, _calls, _ = _launchd_table()
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=t.seams(), bound_s=1.0)
    assert e.value.name == f"scrub-processes-remain:{SLOT}"


def test_scrub_ends_every_uid_through_end_domain_gateway_first():
    """reaper DESIGN §3.1 (R1a): each uid is frozen, killed and booted out before its proof, the gateway first."""
    t, calls, domain_out = _launchd_table()
    t.ignore_kill.clear()
    seams = t.seams()
    seams.domain_out = domain_out
    P.scrub(SLOT, GW, CHILD, seams=seams, bound_s=1.0)
    assert [c[:2] for c in calls] == [("bootout", GW), ("bootout", SLOT), ("bootout", CHILD)]
    assert all(c[2] == [] for c in calls)                 # every bootout came after its uid's kill (K1 before B)
    assert t.procs == {}


def test_an_accepted_bootout_is_not_a_proof():
    t, _calls, _ = _launchd_table()
    seams = t.seams()
    seams.domain_out = lambda uid, timeout: 0              # launchctl said done, yet the scan still sees them
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-processes-remain:{SLOT}"


def test_a_bootout_launchctl_did_not_accept_refuses_by_uid():
    """L3 r1 (codex HIGH): a failed or timed-out bootout is not left to the scan, which cannot see agents launchd
    would start again."""
    t, _calls, domain_out = _launchd_table()
    seams = t.seams()
    seams.domain_out = lambda uid, timeout: 5 if uid == GW else domain_out(uid, timeout)
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-domain-remains:{GW}"
    seams.domain_out = lambda uid, timeout: None           # a launchctl past its timeout
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-domain-remains:{GW}" and "domain-not-accepted:timeout" in e.value.detail


def test_each_uid_gets_its_own_bound_and_a_slow_bootout_is_unproven():
    """reaper DESIGN §4.6 (r5): SCRUB holds the slot lock, not the pool lock, through up to three ``end_domain``
    calls, each with its own V12k; a bootout that eats one uid's bound is that uid's refusal."""
    t, _calls, domain_out = _launchd_table()
    t.ignore_kill.clear()
    seams = t.seams()
    given = []

    def slow(uid, timeout):
        given.append((uid, timeout))
        if uid == CHILD:
            t.t += 1.5                                     # past its own 1.0 s bound
        return domain_out(uid, timeout)
    seams.domain_out = slow
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, CHILD, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-processes-remain:{CHILD}" and "past-bound" in e.value.detail
    assert [u for u, _ in given] == [GW, SLOT, CHILD] and all(0.9 < x <= 1.0 for _, x in given)


def test_darwin_seams_carry_the_bootout_and_linux_none():
    assert P.ProcSeams(listing=list, probe=lambda p: None, pin=id, send=lambda h, s: None).domain_out is None
    if sys.platform == "darwin":
        assert P.darwin_seams().domain_out is P.darwin_bootout
    else:
        assert P.linux_seams().domain_out is None
    with pytest.raises(Refused) as e:
        P.darwin_bootout(0)
    assert e.value.name == "bad-target-uid"


# --- undo: the same bootout, then a proof that refuses by name -------------------------------------------------------

def test_undo_ends_the_uid_through_end_domain_and_refuses_a_survivor_by_name():
    order = []
    no_kill = dict(kill_all=lambda u: pytest.fail("macOS kills only through end_domain"),
                   none_left=lambda u: pytest.fail("end_domain is the proof"))
    ok = IH._end_processes("darwin", 3999, "_levain_s0", end=lambda u: order.append(("end", u)) or P.PROVEN, **no_kill)
    assert ok == (True, "") and order == [("end", 3999)]
    got = IH._end_processes("darwin", 3999, "_levain_s0", end=lambda u: P.Domain(False, "respawn-after-bootout"),
                            **no_kill)
    assert got[0] is False and got[1].startswith("processes-remain:_levain_s0")
    got = IH._end_processes("darwin", 3999, "_levain_s0", end=lambda u: P.Domain(False, "domain-not-accepted:5"),
                            **no_kill)
    assert got[0] is False and got[1].startswith("domain-remains:_levain_s0")
    order.clear()
    IH._end_processes("linux", 3999, "_levain_s0", end=lambda u: order.append("end"),
                      kill_all=lambda u: (True, ""), none_left=lambda u: True)
    assert order == []
