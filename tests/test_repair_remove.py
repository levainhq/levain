"""Row 33 R1b (project_memory/1022-8/upgrade/DESIGN.md §5 R1b): the remove-step table.

* ``test_removal_is_by_identity_in_phase_order``: each kind's record, removed alone with every other kind's object
  present, emits argv naming only its own identity (the r3 bundle mappings go red); the plan fixtures (c) a drop-in at
  R5 before any membership act and its entry after the flip, (d) an unfinished journal's write-ahead group removed
  pair -> retire -> delete; and the pin of the 12 shipped kinds.
* ``test_remove_outcomes_and_retire_order``: per kind, ABSENT / OURS / NOT_OURS / a failed read; the group's durable
  retire precedes its delete, and the crash-table kills at ``group/1:after-retire`` / ``group/1:after-delete`` re-run
  to one retired gid, no SKIP, the group gone.

A :class:`FakeHost` answers every argv a remove step runs (reads and acts) and keeps the state they change; file kinds
sit under a tmp root (``Live.root``), where the tmp root's owner stands in for root.
"""
from __future__ import annotations

import os
from dataclasses import replace
import plistlib
from pathlib import Path

import pytest

from levain.slot import Refused, bwrap, layout, pf, removal as RM, units
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot.accounts import SLOT_MARKER, SLOT_RETIRED_MARKER
from levain.slot.ids import RANGES, RETIRED_IDS_PATH, read_retired_ids
from levain.slot.intent import Intent, intent_text, parse_intent
from levain.slot.sudoers import RUNTIME_SUDOERS

from tests.test_repair_plan import HOST, OP, TXN_B, _pre_change, _plan

SUDOERS_DIR = RUNTIME_SUDOERS.parent
GID = RANGES["darwin"].shared[8]           # the shared gid no fixture group holds (keysadmin's natural pick)
LGID = RANGES["linux"].shared[8]


class Killed(BaseException):
    """The fake SIGKILL a crash-table fault raises (escapes every ``except Exception``)."""


class _Proc:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, "" if rc == 0 else "failed"


class FakeHost:
    def __init__(self, host: str) -> None:
        self.host = host
        self.log: list = []                    # every argv, and ("fsync",) events
        self.loaded: set = set()               # launchd labels
        self.systemd: dict = {}                # unit -> [LoadState, ActiveState]
        self.aliases: set = set()
        self.rules: dict = {}                  # anchor -> holds rules
        self.netns: set = set()
        self.groups: dict = {}                 # name -> [gid, label, set(members)]
        self.users: dict = {"root": [0, "System Administrator"]}
        self.fail: set = set()                 # tool basenames whose reads fail

    def acts(self) -> list:
        reads = ("print", "show", "-list", "list", "passwd", "group", "-sr")
        return [a for a in self.log if a[0] != "fsync" and not (len(a) > 1 and (a[1] in reads or a[-1] in reads
                or a[-1] == "lo0" or (len(a) > 2 and a[2] in reads)))]

    def fsync(self, fd: int) -> None:
        self.log.append(("fsync",))

    def run(self, argv, **kw):
        a = list(argv)
        self.log.append(tuple(a))
        tool = os.path.basename(a[0])
        if tool in self.fail:
            return _Proc(5)
        if tool == "launchctl":
            label = a[2].split("/", 1)[1]
            if a[1] == "print":
                return _Proc(0 if label in self.loaded else 113)
            self.loaded.discard(label)
            return _Proc(0)
        if tool == "systemctl":
            if a[1] == "show":
                st = self.systemd.get(a[-1], ["not-found", "inactive"])
                return _Proc(0, (st[0] if a[3] == "LoadState" else st[1]) + "\n")
            if a[1] in ("disable", "stop"):
                if a[-1] in self.systemd:
                    self.systemd[a[-1]][1] = "inactive"
            if a[1] == "daemon-reload":
                for u in list(self.systemd):
                    if not (self.root / "etc/systemd/system" / u).exists():
                        self.systemd[u][0] = "not-found"
            return _Proc(0)
        if tool == "ifconfig":
            if len(a) == 2:
                return _Proc(0, "".join(f"\tinet {x} netmask 0xffffffff\n" for x in sorted(self.aliases)))
            self.aliases.discard(a[3])
            return _Proc(0)
        if tool == "pfctl":
            if a[-1] == "-sr":
                return _Proc(0, "pass all\n" if self.rules.get(a[2]) else "")
            if a[-2:] == ["-F", "all"]:
                self.rules[a[2]] = False
            return _Proc(0)
        if tool == "ip":
            if a[1:] == ["netns", "list"]:
                return _Proc(0, "".join(f"{n}\n" for n in sorted(self.netns)))
            self.netns.discard(a[3])
            return _Proc(0)
        if tool == "dscl" and a[2] == "-list":
            kind, key = a[3], a[4]
            if kind == "/Users":
                return _Proc(0, "".join(f"{n} {v[0] if key == 'UniqueID' else v[1]}\n" for n, v in self.users.items()))
            if key == "GroupMembership":
                return _Proc(0, "".join(f"{n} {' '.join(sorted(v[2]))}\n" for n, v in self.groups.items()))
            return _Proc(0, "".join(f"{n} {v[0] if key == 'PrimaryGroupID' else v[1]}\n" for n, v in self.groups.items()))
        if tool == "getent":
            if a[1] == "passwd":
                return _Proc(0, "".join(f"{n}:x:{v[0]}:{v[0]}:{v[1]}:/:/bin/false\n" for n, v in self.users.items()))
            return _Proc(0, "".join(f"{n}:x:{v[0]}:{','.join(sorted(v[2]))}\n" for n, v in self.groups.items()))
        if tool in ("dseditgroup", "gpasswd"):
            acct, grp = (a[4], a[7]) if tool == "dseditgroup" else (a[2], a[3])
            self.groups[grp][2].discard(acct)
            return _Proc(0)
        if tool == "groupdel" or (tool == "dscl" and a[2] == "-delete"):
            self.groups.pop(a[-1].rsplit("/", 1)[-1], None)
            return _Proc(0)
        if tool == "dscl" and a[2] == "-create" and a[4:5] == ["RealName"]:
            self.users[a[3].rsplit("/", 1)[-1]][1] = a[5]
            return _Proc(0)
        if tool == "usermod" and "--comment" in a and "--lock" not in a:
            self.users[a[-1]][1] = a[a.index("--comment") + 1]
            return _Proc(0)
        if tool == "rm":
            p = self.root / a[-1].lstrip("/")
            if p.is_symlink() or p.is_file():
                p.unlink()
            return _Proc(0)
        if tool == "apparmor_parser":
            prof = self.root / "sys/kernel/security/apparmor/profiles"
            name = Path(a[-1]).name
            prof.write_text("".join(ln + "\n" for ln in prof.read_text().splitlines() if not ln.startswith(name + " ")))
            return _Proc(0)
        return _Proc(0)                        # visudo -c, chage, the other usermod / dscl tombstone acts


def _host(tmp: Path, host: str = "darwin") -> tuple[FakeHost, RM.Live]:
    h = FakeHost(host)
    h.root = tmp / "root"
    h.root.mkdir(parents=True)
    return h, RM.Live(run=h.run, root=h.root, fsync=h.fsync)


def _put(live: RM.Live, path, data: bytes = b"x\n") -> None:
    p = live.at(Path(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _plist(n: str) -> bytes:
    return plistlib.dumps({"Label": f"com.levain.{n}"})


ENTRY = b"#!/usr/bin/python3 -IS\n# Levain old-helper (CONTRACT). Written by the install helper. Imports only\n"
PROFILE = bwrap.apparmor_profile_text().encode()


def _everything(h: FakeHost, live: RM.Live) -> None:
    """Every kind's object present and Levain's (both hosts' kinds; each step reads only its own)."""
    _put(live, units.LAUNCHD_DIR / "com.levain.old-svc.plist", _plist("old-svc"))
    h.loaded |= {"com.levain.old-svc", "com.levain.qbroker"}
    _put(live, units.SYSTEMD_DIR / "levain-old-svc.service")
    h.systemd["levain-old-svc.service"] = ["loaded", "active"]
    _put(live, SUDOERS_DIR / "levain-old")
    _put(live, SUDOERS_DIR / "levain-runtime")
    gid = GID if h.host == "darwin" else LGID
    h.groups["levain-gone"] = [gid, SLOT_MARKER, {"_levain_keys", "_levain_control"}]
    h.groups["levain-control"] = [gid - 1, SLOT_MARKER, {"_levain_keys"}]
    h.users["_levain_s7"] = [3990, SLOT_MARKER]
    h.aliases |= {"127.0.1.9", "127.0.1.1"}
    _put(live, pf.ANCHOR_FILE)
    h.rules[pf.ANCHOR] = True
    h.rules["com.apple/levain-t"] = True
    _put(live, pf.PF_TOKEN, b"12345\n")
    _put(live, bwrap.APPARMOR_PROFILE, PROFILE)
    _put(live, bwrap.APPARMOR_LOADED, b"levain-slot-bwrap (enforce)\nlevain-bwrap (enforce)\n")
    _put(live, bwrap.LEVAIN_BWRAP)
    _put(live, bwrap.BWRAP_DIGEST)
    h.netns |= {"levain-e7", "levain-e0"}
    _put(live, layout.HELPER_DIR[h.host] / "old-helper", ENTRY)


def _run_all(steps) -> list:
    return [s.call() for s in steps]


def _remove_alone(tmp: Path, host: str, kind: str, ident) -> FakeHost:
    h, live = _host(tmp, host)
    _everything(h, live)
    steps = [s for ph in IH.REMOVE_PHASES[kind]
             for s in IH.removal_steps(host, [(kind, ident)], ph, live=live, held=frozenset({(kind, ident)}),
                                       end=lambda uid, name: (True, ""))]
    assert all(ok for ok, _ in _run_all(steps)), (kind, _run_all(steps))
    assert not live.skips
    return h


def test_removal_is_by_identity_in_phase_order(tmp_path: Path) -> None:
    # --- per kind, alone, every other object present: argv name only its own identity ---------------------------
    # apparmor/1: never the bwrap copy or its digest (red against r3's mapping to bwrap_undo_steps)
    h = _remove_alone(tmp_path / "aa", "linux", "apparmor/1", "levain-slot-bwrap")
    flat = " ".join(" ".join(a) for a in h.acts())
    assert "apparmor_parser -R" in flat and str(bwrap.APPARMOR_PROFILE) in flat
    assert str(bwrap.LEVAIN_BWRAP) not in flat and str(bwrap.BWRAP_DIGEST) not in flat and "levain-bwrap " not in flat
    # pf-anchor/1: a flush, and no release of pf's enable reference (red against r3's alias_undo_steps)
    h = _remove_alone(tmp_path / "pa", "darwin", "pf-anchor/1", (pf.ANCHOR, str(pf.ANCHOR_FILE)))
    assert (pf.PFCTL, "-a", pf.ANCHOR, "-F", "all") in h.acts()
    assert not [a for a in h.log if "-X" in a] and h.rules["com.apple/levain-t"]
    # unit/1 old-svc, a name SERVICES lacks (red against uninstall_steps, which cannot name it)
    assert "old-svc" not in {s.name for s in units.SERVICES}
    h = _remove_alone(tmp_path / "ud", "darwin", "unit/1", "old-svc")
    assert ("/bin/launchctl", "bootout", "system/com.levain.old-svc") in h.acts()
    assert "com.levain.qbroker" in h.loaded and not [a for a in h.acts() if "qbroker" in " ".join(a)]
    h = _remove_alone(tmp_path / "ul", "linux", "unit/1", "old-svc")
    assert [a for a in h.acts() if a[1:] == ("disable", "--now", "levain-old-svc.service")]
    # pf-alias/1: its address only
    h = _remove_alone(tmp_path / "al", "darwin", "pf-alias/1", "127.0.1.9")
    assert h.acts() == [(pf.IFCONFIG, "lo0", "-alias", "127.0.1.9")] and "127.0.1.1" in h.aliases
    # the rest: every act argv names the record's own identity
    for host, kind, ident, own in (
            ("darwin", "sudoers/1", "levain-old", "levain-old"),
            ("darwin", "member/1", ("_levain_keys", "levain-gone", GID), "levain-gone"),
            ("linux", "member/1", ("_levain_keys", "levain-gone", LGID), "levain-gone"),
            ("darwin", "group/1", ("levain-gone", GID), "levain-gone"),
            ("linux", "account/1", ("_levain_s7", 3990), "_levain_s7"),
            ("darwin", "pf-enable/1", str(pf.PF_TOKEN), "12345"),
            ("linux", "bwrap-copy/1", (str(bwrap.LEVAIN_BWRAP), str(bwrap.BWRAP_DIGEST)), "levain"),
            ("linux", "netns/1", "levain-e7", "levain-e7"),
            ("darwin", "entry/1", "old-helper", "old-helper")):
        h = _remove_alone(tmp_path / kind.replace("/", "") / host, host, kind, ident)
        acts = [a for a in h.acts() if a[0] != "/usr/sbin/visudo"]
        assert acts and all(own in " ".join(a) for a in acts), (kind, acts)
    # the member act leaves the group's other member and the group itself
    h = _remove_alone(tmp_path / "m2", "darwin", "member/1", ("_levain_keys", "levain-gone", GID))
    assert h.groups["levain-gone"][2] == {"_levain_control"}

    # --- (c) a recorded drop-in at R5 before any membership act; a recorded entry after the flip -----------------
    gen = "gen-" + "0" * 16
    shape = G.InstallShape(gen, "0.9.0", "0.16.0", "c" * 32, {},
                           (("entry/1", "levain-old-helper"), ("sudoers/1", "levain-old")))
    facts, rf = _pre_change(tmp_path / "c", shape=shape)
    whys = [s.why for s in _plan(facts, rf).steps]
    drop = next(i for i, w in enumerate(whys) if w.startswith(f"remove {SUDOERS_DIR / 'levain-old'} "))
    entry = next(i for i, w in enumerate(whys) if w.endswith("/levain-old-helper"))
    flip = next(i for i, w in enumerate(whys) if w.startswith("flip "))
    puts = [i for i, w in enumerate(whys) if w.startswith("put ")]
    assert puts and drop < min(puts) and entry > flip, (drop, puts, flip, entry)
    assert drop > max(i for i, w in enumerate(whys) if w.startswith("stop "))    # grants go after the stops

    # --- (d) an UNFINISHED journal's write-ahead group, planned by a package that lacks it ----------------------
    wa = (("group/1", ("levain-gone", GID)), ("member/1", ("_levain_keys", "levain-gone", GID)))
    j = Intent("repair", OP, txn=TXN_B, managed=wa)
    facts, rf = _pre_change(tmp_path / "d", intent=parse_intent(intent_text(j).encode(), HOST))
    plan = _plan(facts, rf)
    whys = [s.why for s in plan.steps]
    pair = whys.index("take _levain_keys out of levain-gone")
    group = next(i for i, w in enumerate(whys) if w.startswith("delete the group levain-gone (its gid"))
    assert pair < group
    assert set(wa) <= set(plan.intent.managed)          # the write-ahead set keeps it until R14 writes desired
    assert plan.allocation.shared["levain-keysadmin"] != GID   # R6 never hands out the gid R7 retires
    # its steps run against the host: pair removed, gid retired (durably), group deleted, in that order
    h, live = _host(tmp_path / "dh")
    h.groups["levain-gone"] = [GID, SLOT_MARKER, {"_levain_keys"}]
    for s in IH.removal_steps(HOST, wa, "R7", live=live, held=frozenset()):
        assert s.call()[0]
    order = [("pair" if a[0].endswith("dseditgroup") else "delete" if "-delete" in a else "fsync")
             for a in h.log if a[0] == "fsync" or a[0].endswith("dseditgroup") or "-delete" in a]
    assert order[0] == "pair" and order[-1] == "delete" and "fsync" in order[1:-1], order
    retired = read_retired_ids(live.at(RETIRED_IDS_PATH[HOST]))
    assert GID in retired and "levain-gone" not in h.groups
    # re-planned with that gid retired: allocation never hands it out
    facts2_ctl, rf2 = _pre_change(tmp_path / "d2")
    facts2 = facts2_ctl
    facts2 = replace(facts2_ctl, retired=frozenset(retired))
    assert _plan(facts2_ctl, rf2).allocation.shared["levain-keysadmin"] == GID          # control: free, it is chosen
    assert GID not in _plan(facts2, rf2).allocation.shared.values()

    # --- the pin [C3-F2]: a release that drops a kind goes red ----------------------------------------------------
    assert IH.SHIPPED_KINDS == {"unit/1", "sudoers/1", "member/1", "group/1", "account/1", "pf-alias/1",
                                "pf-anchor/1", "pf-enable/1", "apparmor/1", "bwrap-copy/1", "netns/1", "entry/1"}
    assert IH.SHIPPED_KINDS == set(G.KINDS)


# --- test 2 ---------------------------------------------------------------------------------------------------------

def _notdir(live: RM.Live, path) -> None:
    """Make every read of ``path`` fail (its parent is a file: lstat answers ENOTDIR, not ENOENT)."""
    parent = live.at(Path(path)).parent
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("")


#: kind -> (host, identity, {state: setup(h, live)})
def _cases():
    sym = lambda p: lambda h, lv: (lv.at(Path(p)).parent.mkdir(parents=True, exist_ok=True),   # noqa: E731
                                   lv.at(Path(p)).symlink_to("/dev/null"))
    put = lambda p, b=b"x\n": lambda h, lv: _put(lv, p, b)                                       # noqa: E731
    nd = lambda p: lambda h, lv: _notdir(lv, p)                                                  # noqa: E731
    plist = units.LAUNCHD_DIR / "com.levain.old-svc.plist"

    def grp(gid, label=SLOT_MARKER, members=("_levain_keys",)):
        return lambda h, lv: h.groups.__setitem__("levain-gone", [gid, label, set(members)])

    def fail(tool):
        return lambda h, lv: h.fail.add(tool)

    def both(*fs):
        return lambda h, lv: [f(h, lv) for f in fs]

    return {
        "unit/1": ("darwin", "old-svc", {"ours": put(plist, _plist("old-svc")),
                                         "not-ours": put(plist, plistlib.dumps({"Label": "com.other.x"})),
                                         "fail": fail("launchctl")}),
        "sudoers/1": ("darwin", "levain-old", {"ours": put(SUDOERS_DIR / "levain-old"),
                                               "not-ours": sym(SUDOERS_DIR / "levain-old"),
                                               "fail": nd(SUDOERS_DIR / "levain-old")}),
        "member/1": ("darwin", ("_levain_keys", "levain-gone", GID),
                     {"ours": grp(GID), "not-ours": grp(GID - 1), "fail": both(grp(GID), fail("dscl"))}),
        "group/1": ("darwin", ("levain-gone", GID),
                    {"ours": grp(GID), "not-ours": grp(GID, label="someone else's"), "fail": fail("dscl")}),
        "account/1": ("linux", ("_levain_s7", 3990),
                      {"ours": lambda h, lv: h.users.__setitem__("_levain_s7", [3990, SLOT_MARKER]),
                       "not-ours": lambda h, lv: h.users.__setitem__("_levain_s7", [3991, SLOT_MARKER]),
                       "fail": fail("getent")}),
        "pf-alias/1": ("darwin", "127.0.1.9", {"ours": lambda h, lv: h.aliases.add("127.0.1.9"),
                                               "fail": fail("ifconfig")}),
        "pf-anchor/1": ("darwin", (pf.ANCHOR, str(pf.ANCHOR_FILE)),
                        {"ours": put(pf.ANCHOR_FILE), "not-ours": sym(pf.ANCHOR_FILE), "fail": fail("pfctl")}),
        "pf-enable/1": ("darwin", str(pf.PF_TOKEN), {"ours": put(pf.PF_TOKEN, b"12345\n"),
                                                     "not-ours": sym(pf.PF_TOKEN), "fail": nd(pf.PF_TOKEN)}),
        "apparmor/1": ("linux", "levain-slot-bwrap",
                       {"ours": both(put(bwrap.APPARMOR_PROFILE, PROFILE),
                                     put(bwrap.APPARMOR_LOADED, b"levain-slot-bwrap (enforce)\n")),
                        "not-ours": put(bwrap.APPARMOR_PROFILE, b"# hands' profile\n"),
                        "fail": lambda h, lv: lv.at(bwrap.APPARMOR_LOADED).mkdir(parents=True)}),
        "bwrap-copy/1": ("linux", (str(bwrap.LEVAIN_BWRAP), str(bwrap.BWRAP_DIGEST)),
                         {"ours": put(bwrap.LEVAIN_BWRAP), "not-ours": sym(bwrap.LEVAIN_BWRAP),
                          "fail": nd(bwrap.LEVAIN_BWRAP)}),
        "netns/1": ("linux", "levain-e7", {"ours": lambda h, lv: h.netns.add("levain-e7"), "fail": fail("ip")}),
        "entry/1": ("darwin", "old-helper",
                    {"ours": put(layout.HELPER_DIR["darwin"] / "old-helper", ENTRY),
                     "not-ours": put(layout.HELPER_DIR["darwin"] / "old-helper", b"#!/bin/sh\necho mine\n"),
                     "fail": nd(layout.HELPER_DIR["darwin"] / "old-helper")}),
    }


def _steps(host, kind, ident, live, *, held=True):
    return [s for ph in IH.REMOVE_PHASES[kind]
            for s in IH.removal_steps(host, [(kind, ident)], ph, live=live, held=frozenset({(kind, ident)}) if held
                                      else frozenset(), end=lambda uid, name: (True, ""))]


def test_remove_outcomes_and_retire_order(tmp_path: Path) -> None:
    cases = _cases()
    assert set(cases) == IH.SHIPPED_KINDS
    for kind, (host, ident, setups) in cases.items():
        for state in ("absent", "ours", "not-ours", "fail"):
            if state not in setups and state != "absent":
                continue                      # an address or a namespace carries no owner: never NOT_OURS
            h, live = _host(tmp_path / kind.replace("/", "") / state, host)
            if state != "absent":
                setups[state](h, live)
            results = _run_all(_steps(host, kind, ident, live, held=False))
            acts = [a for a in h.acts() if a[0] != "/usr/sbin/visudo"]
            where = (kind, state, acts, results)
            if state == "absent":
                assert all(ok for ok, _ in results) and not acts and not live.skips, where
            elif state == "ours":
                assert all(ok for ok, _ in results) and acts and not live.skips, where
            elif state == "not-ours":
                assert all(ok for ok, _ in results) and not acts and live.skips == {(kind, ident)}, where
            else:
                assert not results[0][0] and "live read failed" in results[0][1] and not acts, where

    # --- the group: the durable retire precedes the delete argv ---------------------------------------------------
    h, live = _host(tmp_path / "g")
    h.groups["levain-gone"] = [GID, SLOT_MARKER, set()]
    step, = _steps(HOST, "group/1", ("levain-gone", GID), live)
    assert step.call() == (True, "")
    marks = [("fsync" if a[0] == "fsync" else "delete") for a in h.log if a[0] == "fsync" or "-delete" in a]
    assert marks[-1] == "delete" and marks[:-1] and set(marks[:-1]) == {"fsync"}, marks

    # --- the crash table's group rows: kill, then re-run -> the gid once, no SKIP, the group gone -----------------
    retired = RETIRED_IDS_PATH[HOST]
    for at in ("group/1:after-retire", "group/1:after-delete"):
        h, live = _host(tmp_path / at.replace("/", "").replace(":", "-"))
        h.groups["levain-gone"] = [GID, SLOT_MARKER, set()]

        def kill(p: str, at: str = at) -> None:
            if p == at:
                raise Killed(p)

        live.fault = kill
        step, = _steps(HOST, "group/1", ("levain-gone", GID), live)
        with pytest.raises(Killed):
            step.call()
        rerun = RM.Live(run=h.run, root=h.root, fsync=h.fsync)
        step, = _steps(HOST, "group/1", ("levain-gone", GID), rerun)
        assert step.call()[0]
        text = live.at(retired).read_text()
        assert text.splitlines().count(str(GID)) == 1 and not rerun.skips and "levain-gone" not in h.groups, at

    # a journal-only ABSENT group was never made: not retired
    h, live = _host(tmp_path / "jo")
    step, = _steps(HOST, "group/1", ("levain-gone", GID), live, held=False)
    assert step.call() == (True, "") and not live.at(retired).exists()
    # a record-held ABSENT group: retired, silently
    step, = _steps(HOST, "group/1", ("levain-gone", GID), live, held=True)
    assert step.call() == (True, "") and GID in read_retired_ids(live.at(retired)) and not live.skips
    # a member/1 whose live group sits at another gid: NOT_OURS, no dseditgroup -d / gpasswd -d
    for host, gid in (("darwin", GID), ("linux", LGID)):
        h, live = _host(tmp_path / f"mx-{host}", host)
        h.groups["levain-gone"] = [gid - 1, SLOT_MARKER, {"_levain_keys"}]
        step, = _steps(host, "member/1", ("_levain_keys", "levain-gone", gid), live)
        assert step.call()[0] and not h.acts() and live.skips
    # undo's group step (retire=False) retires nothing
    h, live = _host(tmp_path / "undo")
    h.groups["levain-gone"] = [GID, SLOT_MARKER, set()]
    step, = IH.removal_steps(HOST, [("group/1", ("levain-gone", GID))], "R7", live=live, retire=False,
                             held=frozenset({("group/1", ("levain-gone", GID))}))
    assert step.call()[0] and not live.at(retired).exists() and "levain-gone" not in h.groups


def test_failed_live_reads_stop_before_the_record(tmp_path: Path) -> None:
    """HEAD 1022-8 X4-M1 binding rows: a failing ``ifconfig lo0`` fails the alias removal (never ABSENT), so R14 is not
    reached and the record keeps the alias; the same for a ``launchctl print`` exit that is not launchd's not-found."""
    h, live = _host(tmp_path / "x")
    h.fail.add("ifconfig")
    step, = IH.removal_steps(HOST, [("pf-alias/1", "127.0.1.9")], "R8", live=live)
    ok, why = step.call()
    assert not ok and "ifconfig lo0 exited 5" in why and not h.acts()
    h.fail = {"launchctl"}
    step, = IH.removal_steps(HOST, [("unit/1", "old-svc")], "R5", live=live)
    assert not step.call()[0]
    # in the plan, both removals come before the record write, and run_plan stops at the first failing step
    shape = G.InstallShape("gen-" + "0" * 16, "0.9.0", "0.16.0", "c" * 32, {},
                           (("pf-alias/1", "127.0.1.9"), ("unit/1", "old-svc")))
    facts, rf = _pre_change(tmp_path / "p", shape=shape)
    plan = _plan(facts, rf, live=live)
    whys = [s.why for s in plan.steps]
    rec = next(i for i, w in enumerate(whys) if w.startswith("write the install record"))
    assert whys.index("remove the lo0 alias 127.0.1.9") < rec and whys.index("stop com.levain.old-svc") < rec


def test_install_skips_is_written_before_the_shape(tmp_path: Path) -> None:
    """X4-M2: ``install-skips`` durably before ``install-shape``; an earlier entry whose object is gone drops out."""
    h, live = _host(tmp_path / "s")
    live.skips.add(("sudoers/1", "levain-mine"))
    path = live.at(IH.install_skips_path(HOST))
    path.parent.mkdir(parents=True)
    old = [["pf-alias/1", "127.0.1.8"], ["sudoers/1", "levain-kept"]]
    path.write_text(G.canon.canonical({"format": IH.SKIPS_FORMAT, "skips": old}))
    os.symlink("/dev/null", live.at(SUDOERS_DIR / "levain-kept")) if live.at(SUDOERS_DIR).mkdir(parents=True) is None \
        else None
    assert IH.write_install_skips(HOST, live) == (True, "")
    got = G.canon.parse(path.read_bytes())["skips"]
    assert got == [["sudoers/1", "levain-kept"], ["sudoers/1", "levain-mine"]]   # the gone alias dropped out
