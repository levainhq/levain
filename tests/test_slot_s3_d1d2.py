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
from tests.test_slot_s2f1_proc import CHILD, GW, SLOT, Table, pids_sent
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
        return True
    return t, calls, domain_out


def test_scrub_without_the_bootout_names_the_launchd_survivor():
    t, _calls, _ = _launchd_table()
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=t.seams(), bound_s=1.0)
    assert e.value.name == f"scrub-processes-remain:{SLOT}"


def test_scrub_boots_out_every_uid_after_the_gateway_kill_then_proves():
    t, calls, domain_out = _launchd_table()
    seams = t.seams()
    seams.domain_out = domain_out
    P.scrub(SLOT, GW, CHILD, seams=seams, bound_s=1.0)
    assert pids_sent(t, 9) == {100}                       # the gateway's kill came first: its process is gone
    assert [c[:2] for c in calls] == [("bootout", SLOT), ("bootout", GW), ("bootout", CHILD)]
    assert calls[0][2] == [210, 211, 212, 213] and calls[1][2] == []
    assert t.procs == {}


def test_a_failed_bootout_leaves_the_proof_to_refuse():
    t, _calls, _ = _launchd_table()
    seams = t.seams()
    seams.domain_out = lambda uid, timeout: True           # launchctl said done, yet the scan still sees them
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-processes-remain:{SLOT}"


def test_a_bootout_launchctl_did_not_report_done_refuses_by_uid():
    """L3 r1 (codex HIGH): a failed or timed-out bootout is not left to the scan, which cannot see agents launchd
    would start again."""
    t, _calls, domain_out = _launchd_table()
    seams = t.seams()
    seams.domain_out = lambda uid, timeout: (domain_out(uid, timeout) and uid != GW)
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-domain-remains:{GW}"


def test_the_bootouts_share_one_deadline():
    """L3 r1 (codex MED): SCRUB holds the pool lock; the bootouts together get bound_s, never one timeout each."""
    t, _calls, domain_out = _launchd_table()
    seams = t.seams()
    given = []

    def slow(uid, timeout):
        given.append(timeout)
        t.t += 0.6                                         # each bootout takes 0.6 s of a 1.0 s bound
        return domain_out(uid, timeout)
    seams.domain_out = slow
    with pytest.raises(Refused) as e:
        P.scrub(SLOT, GW, CHILD, seams=seams, bound_s=1.0)
    assert e.value.name == f"scrub-domain-remains:{CHILD}"      # 0.6 + 0.6 passes the 1.0 s deadline before the third
    assert given[0] <= 1.0 and all(b < a for a, b in zip(given, given[1:]))


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

def test_undo_boots_out_before_the_kill_and_refuses_a_survivor_by_name():
    order = []
    ok = IH._end_processes("darwin", 3999, "_levain_s0", bootout=lambda u: order.append(("bootout", u)) or True,
                           kill_all=lambda u: (order.append(("kill", u)), (True, ""))[1],
                           none_left=lambda u: (order.append(("proof", u)), True)[1])
    assert ok == (True, "") and order == [("bootout", 3999), ("kill", 3999), ("proof", 3999)]
    got = IH._end_processes("darwin", 3999, "_levain_s0", bootout=lambda u: True, kill_all=lambda u: (True, ""),
                            none_left=lambda u: False)
    assert got[0] is False and got[1].startswith("processes-remain:_levain_s0")
    got = IH._end_processes("darwin", 3999, "_levain_s0", bootout=lambda u: False, kill_all=lambda u: (True, ""),
                            none_left=lambda u: True)
    assert got[0] is False and got[1].startswith("domain-remains:_levain_s0")
    order.clear()
    IH._end_processes("linux", 3999, "_levain_s0", bootout=lambda u: order.append("bootout"),
                      kill_all=lambda u: (True, ""), none_left=lambda u: True)
    assert order == []


# --- Z5b: END's kill boots out the domain first, so launchd cannot start an agent between the sweep and the proof -----

def _respawning_table():
    """runs/s3-macos-2f-final/raw/Z5b_300k.txt's shape: distnoted of the slot uid (2913) dies of the sweep's SIGKILL,
    the sweep's two scans then find none, and launchd has started it again (2942) by END's proof. Here launchd starts
    it three listings after the kill, unless the uid's domain has been booted out."""
    t = Table()
    t.add(2913, SLOT)
    booted: list[int] = []
    killed_at: list[int] = []

    def launchd(tab, n):
        if 2913 not in tab.procs and not killed_at:
            killed_at.append(n)
        if killed_at and n == killed_at[0] + 2 and SLOT not in booted and 2942 not in tab.procs:
            tab.add(2942, SLOT)
    t.on_list.append(launchd)

    def domain_out(uid, timeout):
        booted.append(uid)
        for pid in [p for p, q in t.procs.items() if uid in q.uids]:
            del t.procs[pid]
        return True
    return t, booted, domain_out


def test_without_the_bootout_the_proof_after_the_kill_finds_the_respawned_agent():
    t, _booted, _ = _respawning_table()
    u = P.UidProcs(t.seams())
    assert u.kill(SLOT, 1.0) is True                       # the sweep saw two empty scans
    assert u.no_process(SLOT) is False                     # END's proof then sees 2942: Z5b's HELD kill-unproven
    assert 2942 in t.procs


def test_the_darwin_kill_boots_out_the_domain_before_its_sweep():
    t, booted, domain_out = _respawning_table()
    seams = t.seams()
    seams.domain_out = domain_out
    u = P.UidProcs(seams)
    assert u.kill(SLOT, 1.0) is True
    assert booted == [SLOT]
    assert u.no_process(SLOT) is True and t.procs == {}


def test_a_bootout_launchctl_did_not_report_done_fails_the_kill():
    t, _booted, _ = _respawning_table()
    seams = t.seams()
    seams.domain_out = lambda uid, timeout: False
    assert P.UidProcs(seams).kill(SLOT, 1.0) is False
    assert pids_sent(t, 9) == set()                        # nothing signalled under a domain launchd still runs


def test_scrub_boots_each_uid_out_once():
    """SCRUB's gateway sweep has no bootout of its own: each uid is booted out once, by SCRUB's loop."""
    t, calls, domain_out = _launchd_table()
    seams = t.seams()
    seams.domain_out = domain_out
    P.scrub(SLOT, GW, CHILD, seams=seams, bound_s=1.0)
    assert [c[1] for c in calls] == [SLOT, GW, CHILD]


@pytest.mark.parametrize("rc,ok", [(0, True), (124, True), (1, False), (3, False), (113, False)])
def test_darwin_bootout_reads_tearing_down_as_under_way(monkeypatch, rc, ok):
    """124 'Domain is tearing down' answered a bootout right after one (runs/s3-macos-2f-final/raw/Z1_Z2.txt)."""
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=rc, stdout=b"", stderr=b""))
    assert P.darwin_bootout(4000, 1.0) is ok
