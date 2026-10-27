"""S1 session 1b plan layer (CONTRACT c7.5 §1.1-1.3, §2.1, §8.3, §9.1, §20, §22, §23): services and their
shared bootstrap, the pf rules, Linux namespaces, the slots' bwrap, the root import tree, singleton
revival. Every row is a plan or text assertion; nothing touches the OS (file writes go to pytest tmp
dirs; the executed entry runs a stub program from a tmp tree)."""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain.firing import hands
from levain.slot import Refused, bwrap, netns, pf, root_tree, service_boot, units
from levain.slot.accounts import SLOT_MARKER
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS, SINGLETONS, SlotId, loopback_alias
from levain.slot.install_helper import HostFacts, operator_record, plan_replenish, plan_setup, plan_undo
from levain.slot.layout import IMPORT_TREE, RUN, install_helper_path, runtime_helper_path, service_entry_path
from levain.slot.sudoers import RUNTIME_SUDOERS

HOSTS = ("linux", "darwin")


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    """A plan for the OTHER host names tools this test host lacks; the §23 preflight refuses a bare
    name, so here every tool resolves (to its real path when present)."""
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")
OP, OP_UID = "alice", 501


def _setup(n=8, m=2):
    return parse_install_argv(["setup", "--slots", str(n), "--verifier-slots", str(m), "--operator", OP])


def _facts(host, **kw):
    # every host tool present: the plan, not this test host, decides what it names (§23 preflight)
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                xcode_ready=lambda: True)
    base.update(kw)
    return HostFacts(host, **base)


def _set_up(host, **kw):
    return _facts(host, operator_record=operator_record(OP, OP_UID), **kw)


def _plan(host, n=8, m=2, restricted="0", **kw):
    return plan_setup(_setup(n, m), _facts(host, **kw), read_sysctl=lambda p: restricted,
                      program_exists=lambda p: True)


def _refusal(fn, *a, **kw) -> str:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value.name


def _undo(host, accounts, **kw):
    return plan_undo(parse_install_argv(["undo"]), _set_up(host, **kw), accounts=accounts,
                     shared_groups={g: RANGES[host].shared[i] for i, g in enumerate(SHARED_GROUPS)})


# --- §20 re-setup after undo ------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_setup_after_undo_revives_singletons(host):
    first = _plan(host)
    ids = dict(first.allocation.ids)
    undo = _undo(host, ids)
    # what the host holds after undo: every account a tombstone, its id retired; shared groups deleted
    after = _facts(host, retired=undo.retired_after, used_ids=frozenset(ids.values()),
                   existing_names=frozenset(ids), levain_tombstones=ids)
    again = plan_setup(_setup(), after, read_sysctl=lambda p: "0")
    got = again.allocation.ids
    assert set(again.revived) == set(SINGLETONS)
    assert all(got[s] == ids[s] for s in SINGLETONS)                          # same role, same id
    # slot, front and verifier-pair tombstones are never revived: fresh indices, fresh ids
    assert [s.tag for s in again.allocation.egress] == [f"e{i}" for i in range(8, 16)]
    assert [s.tag for s in again.allocation.verifier] == ["v2", "v3"]
    slot_ids = {got[n] for s in again.allocation.slots for n in s.accounts}
    assert not slot_ids & set(ids.values()) and slot_ids <= set(RANGES[host].slots)
    # shared groups re-created at range ids
    assert set(again.allocation.shared) == set(SHARED_GROUPS)
    assert all(g in RANGES[host].shared for g in again.allocation.shared.values())
    # a revived singleton is relabelled, never created afresh (Linux: no useradd for it)
    argvs = [s.argv for s in again.steps if s.argv]
    for name in SINGLETONS:
        if host == "linux":
            assert not [a for a in argvs if a[0].endswith("useradd") and a[-1] == name]
            assert any(a[0].endswith("usermod") and a[-1] == name and SLOT_MARKER in a for a in argvs)
            assert any(a[0].endswith("chage") and a[1:] == ("--expiredate", "-1", name) for a in argvs)
        else:
            assert ("/usr/bin/dscl", ".", "-create", f"/Users/{name}", "RealName", SLOT_MARKER) in argvs
            assert ("/usr/bin/dscl", ".", "-create", f"/Users/{name}", "UniqueID", str(ids[name])) in argvs


@pytest.mark.parametrize("host", HOSTS)
def test_foreign_singleton_name_refused(host):
    # a same-named account WITHOUT the marker
    assert _refusal(_plan, host, existing_names=frozenset({"_levain_keys"})) == "account-exists"
    # the marker, but its id outside the singleton range
    outside = RANGES[host].slots[0]
    assert _refusal(_plan, host, existing_names=frozenset({"_levain_keys"}),
                    levain_tombstones={"_levain_keys": outside}) == "account-exists"
    # a shared group name present
    assert _refusal(_plan, host, existing_names=frozenset({"levain-wake"})) == "account-exists"
    # positive control: the marker, in range
    uid = RANGES[host].singletons[3]
    plan = _plan(host, existing_names=frozenset({"_levain_keys"}), used_ids=frozenset({uid}),
                 levain_tombstones={"_levain_keys": uid})
    assert plan.revived == ("_levain_keys",) and plan.allocation.ids["_levain_keys"] == uid
    others = {plan.allocation.ids[s] for s in SINGLETONS if s != "_levain_keys"}
    assert uid not in others and len(others) == len(SINGLETONS) - 1


def test_slot_names_fit():
    """§20 reading of §1.1: per-slot names <= 15 chars at the widest index; singletons <= Linux's 32."""
    widest = SlotId("egress", 63).accounts + SlotId("none", 63).accounts
    assert max(len(n) for n in widest) <= 15
    assert all(len(n) <= 32 for n in SINGLETONS) and max(len(n) for n in SINGLETONS) == 16
    for s in (SlotId("egress", 63), SlotId("none", 63)):
        assert len(s.netns) <= 15            # a Linux interface-name-sized bound holds for the netns too


# --- §1.3 services ------------------------------------------------------------------------------------


GOLDEN_CONTROL_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>GroupName</key>
	<string>_levain_control</string>
	<key>InitGroups</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>Label</key>
	<string>com.levain.control</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/python3</string>
		<string>-IS</string>
		<string>/usr/local/libexec/levain/service-entry</string>
		<string>control</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>UserName</key>
	<string>_levain_control</string>
</dict>
</plist>
"""

GOLDEN_DISPATCH_UNIT = """# Written by the Levain install helper; removed by its undo verb.
[Unit]
Description=Levain dispatch (CONTRACT §1.3)
After=levain-control.service levain-keys.service
Wants=levain-control.service levain-keys.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=_levain_dispatch
Group=_levain_dispatch
ExecStart=/usr/bin/python3 -IS /usr/libexec/levain/service-entry dispatch
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=78 77

[Install]
WantedBy=multi-user.target
"""

#: §1.3 typed out: name -> (uid, starts after, restart, macOS label, Linux unit)
TABLE_1_3 = {
    "control": ("_levain_control", (), "always", "com.levain.control", "levain-control.service"),
    "keys": ("_levain_keys", ("control",), "always", "com.levain.keys", "levain-keys.service"),
    "qbroker": ("_levain_qbroker", ("control",), "always", "com.levain.qbroker", "levain-qbroker.service"),
    "dispatch": ("_levain_dispatch", ("control", "keys"), "on-failure", "com.levain.dispatch",
                 "levain-dispatch.service"),
    "reaper": ("root", ("control",), "always", "com.levain.reaper", "levain-reaper.service"),
    "pf": ("root", (), "one-shot", "com.levain.pf", None),
}


def test_service_units_golden():
    by = {s.name: s for s in units.SERVICES}
    assert set(by) == set(TABLE_1_3)
    assert units.launchd_plist_text(by["control"]) == GOLDEN_CONTROL_PLIST
    assert units.systemd_unit_text(by["dispatch"]) == GOLDEN_DISPATCH_UNIT
    for name, (uid, after, restart, label, unit) in TABLE_1_3.items():
        s = by[name]
        assert (s.user, s.after, s.restart) == (uid, after, restart), name
        job = plistlib.loads(units.launchd_plist_text(s).encode())
        assert job["Label"] == label and job["UserName"] == uid
        # a one-shot is retried while it exits non-zero (F5), never left failed after one boot attempt
        assert job["KeepAlive"] == (True if restart == "always" else {"SuccessfulExit": False})
        assert job["ProgramArguments"] == ["/usr/bin/python3", "-IS", "/usr/local/libexec/levain/service-entry", name]
        if unit is None:
            assert "linux" not in s.hosts
            assert _refusal(units.systemd_unit_text, s) == "unsupported-host"
            continue
        text = units.systemd_unit_text(s)
        assert units.systemd_name(s) == unit
        assert f"User={uid}\n" in text and f"Restart={restart}\n" in text
        assert f"ExecStart=/usr/bin/python3 -IS /usr/libexec/levain/service-entry {name}\n" in text
        if restart == "always":
            # §23 item 8: a failing service backs off instead of looping at 1 Hz
            assert "RestartSteps=5\n" in text and "RestartMaxDelaySec=30\n" in text
        else:
            # §1.3's dispatch cell: on-failure, RestartSec=5
            assert "RestartSec=5\n" in text and "RestartSteps=" not in text
        deps = " ".join(TABLE_1_3[a][4] for a in after)
        assert (f"After={deps}\n" in text) if after else ("After=" not in text)


@pytest.mark.parametrize("host", HOSTS)
def test_reaper_unit_has_no_timer(host):
    """§23: the reaper is ONE long-running root service; its interval lives in its program."""
    reaper = units.by_name("reaper")
    assert reaper.restart == "always"
    plan = _plan(host)
    written = [s.write for s in plan.steps if s.write]
    assert not [w for w in written if str(w[0]).endswith(".timer")]
    for path, text, _ in written:
        if str(path).startswith(("/Library/LaunchDaemons/", "/etc/systemd/system/")):
            assert "StartInterval" not in text and "OnUnitActiveSec" not in text and "[Timer]" not in text, path
    assert not [n for n in units.start_order(host) if n.endswith(".timer")]
    assert not [s for s in _undo(host, {}).steps if s.argv and any(a.endswith(".timer") for a in s.argv)]


@pytest.mark.parametrize("host", HOSTS)
def test_units_install_only_with_their_program(host, tmp_path):
    """§23: setup installs a unit only when its program is in the package it copies; the plan names
    every skipped one."""
    pkg = tmp_path / "levain"
    (pkg / "slot" / "services").mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.0"\n')          # row 33 R1: setup reads it as text
    (pkg / "slot" / "services" / "control.py").write_text("")
    plan = plan_setup(_setup(), _facts(host), read_sysctl=lambda p: "0", source=pkg)
    assert plan.services == ("control",)
    written = [str(s.write[0]) for s in plan.steps if s.write]
    unit_dir = "/Library/LaunchDaemons/" if host == "darwin" else "/etc/systemd/system/"
    assert [w for w in written if w.startswith(unit_dir)] == [
        "/Library/LaunchDaemons/com.levain.control.plist" if host == "darwin"
        else "/etc/systemd/system/levain-control.service"]
    skipped = [p for p in plan.pending if "not installed" in p]
    assert len(skipped) == len(units.services_for(host)) - 1
    assert all("services/control.py" not in p for p in skipped)
    # nothing built: no unit, no start step at all
    empty = tmp_path / "x" / "levain"
    empty.mkdir(parents=True)
    (empty / "__init__.py").write_text('__version__ = "0.0.0"\n')
    bare = plan_setup(_setup(), _facts(host), read_sysctl=lambda p: "0", source=empty)
    assert len(bare.pending) - len(plan.pending) == 1                     # control is named skipped too
    assert bare.services == () and not [s for s in bare.steps if s.write and str(s.write[0]).startswith(unit_dir)]
    assert not [s for s in bare.steps if s.argv and ("bootstrap" in s.argv or "--now" in s.argv)]


@pytest.mark.parametrize("host", HOSTS)
def test_services_start_order(host):
    plan = _plan(host)
    if host == "darwin":
        starts = [s.argv[-1] for s in plan.steps if s.argv[:3] == ("/bin/launchctl", "bootstrap", "system")]
        names = [Path(p).name.removeprefix("com.levain.").removesuffix(".plist") for p in starts]
        writes = [i for i, s in enumerate(plan.steps) if s.write and str(s.write[0]).startswith("/Library/LaunchDaemons/")]
    else:
        starts = [s.argv[-1] for s in plan.steps if s.argv[1:3] == ("enable", "--now")]
        names = [n.removeprefix("levain-").removesuffix(".service") for n in starts]
        writes = [i for i, s in enumerate(plan.steps) if s.write and str(s.write[0]).startswith("/etc/systemd/system/")]
    want = [n for n in TABLE_1_3 if host == "darwin" or TABLE_1_3[n][4]]
    assert sorted(names) == sorted(want)
    for n in names:                                       # every dependency started before its dependant
        for dep in TABLE_1_3[n][1]:
            assert names.index(dep) < names.index(n), (n, dep)
    first_start = next(i for i, s in enumerate(plan.steps)
                       if s.argv and ("bootstrap" in s.argv or "--now" in s.argv))
    assert max(writes) < first_start                       # every unit written before any starts
    # services start after the accounts, the import tree and the confinement they run under
    tree_write = next(i for i, s in enumerate(plan.steps) if s.write and s.write[0] == service_entry_path(host))
    assert tree_write < first_start
    # undo: every service stopped right after the runtime sudoers rule (and its visudo -c), reverse order, before
    # anything is removed (R1b: each unit/1 through its per-identity stop)
    undo = _undo(host, dict(plan.allocation.ids))
    whys = [s.why for s in undo.steps]
    stops = [i for i, w in enumerate(whys) if w.startswith(("stop com.levain.", "stop and disable levain-"))]
    # FIX1: the sudoers preflight [0] and undo's journal [1] first, then the runtime rule [2] and its check [3]
    assert stops and min(stops) == 4 and whys[3].startswith("check the sudoers config") \
        and whys[2].startswith(f"remove {RUNTIME_SUDOERS} ")
    first_removal = next(i for i, s in enumerate(undo.steps) if s.write or "tombstone" in s.why or
                         (s.why.startswith("remove /") and "sudoers" not in s.why))
    assert max(stops) < first_removal
    stopped = [whys[i].split()[-1] for i in stops]
    assert stopped == list(reversed(units.start_order(host)))


# --- the shared service bootstrap (§22, §23) ------------------------------------------------------------


def _st(mode, uid=0, gid=0, kind=stat.S_IFDIR):
    return os.stat_result((kind | mode, 0, 0, 0, uid, gid, 0, 0, 0, 0))


_UIDS = {"_levain_control": 3799, "_levain_keys": 3798, "_levain_qbroker": 3797, "_levain_dispatch": 3796}
_GIDS = {**_UIDS, "levain-control": 3788}


@pytest.mark.parametrize("host", HOSTS)
def test_service_exits_without_run_dir(host):
    """§22: a non-root service exits non-zero when its R/ dir is absent or not root-made with the §2.2
    owner, group and mode; KeepAlive / Restart brings it back once the reaper made it."""
    R = str(RUN[host])
    good = {R: _st(0o755), f"{R}/control": _st(0o2750, 3799, 3788)}

    def lstat_from(table):
        def lstat(p):
            if p not in table:
                raise FileNotFoundError(2, "No such file or directory", p)
            return table[p]
        return lstat

    def check(table):
        return service_boot.check_run_dir(host, "control", lstat=lstat_from(table), uid_of=_UIDS.__getitem__,
                                          gid_of=_GIDS.__getitem__)

    assert check(good) == (True, "")                                               # positive control
    assert not check({R: good[R]})[0]                                               # dir absent
    assert not check({})[0]                                                         # R/ absent
    assert not check({**good, R: _st(0o755, uid=501)})[0]                          # R/ not root's
    assert not check({**good, R: _st(0o775)})[0]                                   # R/ writable by a group
    assert not check({**good, f"{R}/control": _st(0o750, 3799, 3788, stat.S_IFLNK)})[0]   # a link
    assert not check({**good, f"{R}/control": _st(0o755, 3799, 3788)})[0]          # wrong mode
    assert not check({**good, f"{R}/control": _st(0o750, 501, 3788)})[0]           # wrong owner
    assert not check({**good, f"{R}/control": _st(0o750, 3799, 20)})[0]            # wrong group
    # the run dir each non-root service checks is its §2.2 row
    assert {n: service_boot.run_dir_row(host, n).path.name for n in service_boot.RUN_DIR_OF} == {
        "control": "control", "keys": "keys", "qbroker": "qb", "dispatch": "planner"}
    assert set(service_boot.RUN_DIR_OF) == {s.name for s in units.SERVICES if s.user != "root"}
    # main exits non-zero (and never runs the program) when the check fails; root services skip it
    ran = []
    assert service_boot.main(["control"], host=host, check=lambda h, n: (False, "absent")) == service_boot.EXIT_NO_RUN_DIR
    assert service_boot.main(["nope"], host=host, check=lambda h, n: ran.append(n) or (True, "")) == service_boot.EXIT_USAGE
    assert service_boot.main([], host=host) == service_boot.EXIT_USAGE
    assert ran == []


def _tree(tmp_path, host, program: str):
    tree = tmp_path / "tree"
    gen = tree / "gen-0123456789abcdef"
    shutil.copytree(Path(levain.__file__).parent, gen / "levain", ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "current").symlink_to(gen.name)                              # row 33 R1: the one link, relative
    (gen / "levain" / "slot" / "services" / "reaper.py").write_text(program)
    entry = tmp_path / "service-entry"
    entry.write_text(root_tree.entry_text(host, "service-entry", tree=tree))
    return tree, entry


def test_service_entry_imports_levain(tmp_path):
    """§23: a unit runs its program through the shared bootstrap entry with -IS; the entry puts the
    import tree on sys.path, so the program imports levain FROM THE TREE (and no site-packages)."""
    host = service_boot.host_os()
    program = ("import sys, levain, levain.slot.layout\n"
               "print(levain.__file__)\n"
               "print(any('site-packages' in p for p in sys.path))\n")
    tree, entry = _tree(tmp_path, host, program)
    r = subprocess.run([sys.executable, "-IS", str(entry), "reaper"], capture_output=True, text=True, cwd=tmp_path,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    imported, site = r.stdout.split("\n")[:2]
    # FROM THE TREE: its current generation, never a copy elsewhere (the path the boot line resolved)
    assert Path(imported) == (tree / "gen-0123456789abcdef" / "levain" / "__init__.py").resolve()
    assert site == "False"
    # a name not in §1.3 is refused before any program runs
    r = subprocess.run([sys.executable, "-IS", str(entry), "evil"], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == service_boot.EXIT_USAGE and r.stdout == ""


@pytest.mark.skipif(RUN[service_boot.host_os()].exists(), reason="this host has a Levain run dir")
def test_service_entry_exits_without_run_dir(tmp_path):
    """§22, executed: control's entry exits EX_TEMPFAIL on a host with no R/ and never runs its program."""
    host = service_boot.host_os()
    tree, entry = _tree(tmp_path, host, "")
    (tree / "current" / "levain" / "slot" / "services" / "control.py").write_text("print('ran')\n")
    r = subprocess.run([sys.executable, "-IS", str(entry), "control"], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == service_boot.EXIT_NO_RUN_DIR and "ran" not in r.stdout


# --- §8.3 / §23 pf rules -------------------------------------------------------------------------------


GOLDEN_ANCHOR = """# Levain session slots (CONTRACT §8.3, §24). Written by the install helper; removed by its undo verb.
block in quick proto tcp from any to ! 127.0.1.1 user 3999 flags S/SA
block in quick proto tcp from any to ! 127.0.1.2 user 3997 flags S/SA
block in quick inet6 proto tcp from any to any user { 3999 3997 } flags S/SA
block in quick on lo0 proto tcp from any to 127.0.1.1 user != 3999 flags S/SA
block in quick on lo0 proto tcp from any to 127.0.1.2 user != 3997 flags S/SA
pass in quick on lo0 proto tcp from any to 127.0.1.1 user 3999 keep state
pass in quick on lo0 proto tcp from any to 127.0.1.2 user 3997 keep state
pass out quick on lo0 proto tcp from any to 127.0.0.1 port 47100 user 3999 keep state
pass out quick on lo0 proto tcp from any to 127.0.0.1 port 47101 user 3997 keep state
pass out quick on lo0 proto tcp from any to 127.0.1.1 user 3999 keep state
pass out quick on lo0 proto tcp from any to 127.0.1.2 user 3997 keep state
block out quick proto { tcp udp } user 3999
block out quick proto { tcp udp } user 3997
block out quick on lo0 proto { tcp udp } from any to { 127.0.1.1 127.0.1.2 }
block out quick proto {tcp udp} user 3995
block out quick proto {tcp udp} user 3994
"""

_PF_IDS = {"_levain_s0": 3999, "_levain_gw0": 3998, "_levain_s1": 3997, "_levain_gw1": 3996,
           "_levain_v0": 3995, "_levain_c0": 3994}


def test_pf_anchor_golden():
    slots = [SlotId("none", 0), SlotId("egress", 1), SlotId("egress", 0)]
    assert pf.anchor_text(slots, _PF_IDS) == GOLDEN_ANCHOR
    # every egress pass names its OWN port and its own uid; nothing passes a verifier account
    every = [SlotId("egress", n) for n in range(32)] + [SlotId("none", n) for n in range(16)]
    ids = {}
    for i, name in enumerate(x for s in every for x in s.accounts):
        ids[name] = 3999 - i
    rules = pf.anchor_rules(every, ids)
    for n in range(32):
        uid = ids[f"_levain_s{n}"]
        gw = rules.index(f"pass out quick on lo0 proto tcp from any to 127.0.0.1 port {47100 + n} user {uid} keep state")
        own = rules.index(f"pass out quick on lo0 proto tcp from any to 127.0.1.{n + 1} user {uid} keep state")
        assert gw < own < rules.index(f"block out quick proto {{ tcp udp }} user {uid}")
    verifier_uids = {str(ids[x]) for s in every if s.slot_class == "none" for x in s.accounts}
    passed = {u for r in rules if r.startswith("pass") for u in re.findall(r"\buser (\d+)", r)}
    assert len(passed) == 32 and not passed & verifier_uids
    assert len(rules) == 6 * 32 + 2 + 2 * 16
    assert not [r for r in rules if "_levain" in r]                      # §23: numeric uids only
    assert _refusal(pf.gwport, SlotId("none", 0)) == "bad-token"
    # a uid outside §1.1's slot range (or none at all) is refused, never rendered
    assert _refusal(pf.anchor_rules, [SlotId("egress", 0)], {"_levain_s0": 0}) == "bad-token"
    assert _refusal(pf.anchor_rules, [SlotId("egress", 0)], {}) == "bad-token"


#: The anchor R-M1b measured as PASSING (§24), copied verbatim: slot A uid 621 on alias 127.0.1.21,
#: slot B uid 622 on 127.0.1.22, the front-port stand-in 127.0.0.1:47100 for both. Never edit it to
#: match the renderer; a rule that differs from it is a rule the VM did not measure.
MEASURED_RM1B = """\
# lvrm1b: A=621 alias 127.0.1.21, B=622 alias 127.0.1.22, gateway 127.0.0.1:47100
# inbound guards (inbound user = the LISTENER owner, pf.conf(5)); decided on the SYN only
block in quick proto tcp from any to ! 127.0.1.21 user 621 flags S/SA
block in quick proto tcp from any to ! 127.0.1.22 user 622 flags S/SA
block in quick inet6 proto tcp from any to any user { 621 622 } flags S/SA
block in quick on lo0 proto tcp from any to 127.0.1.21 user != 621 flags S/SA
block in quick on lo0 proto tcp from any to 127.0.1.22 user != 622 flags S/SA
pass in quick on lo0 proto tcp from any to 127.0.1.21 user 621 keep state
pass in quick on lo0 proto tcp from any to 127.0.1.22 user 622 keep state
# outbound (outbound user = the socket that sends)
pass out quick on lo0 proto tcp from any to 127.0.0.1 port 47100 user 621 keep state
pass out quick on lo0 proto tcp from any to 127.0.0.1 port 47100 user 622 keep state
pass out quick on lo0 proto tcp from any to 127.0.1.21 user 621 keep state
pass out quick on lo0 proto tcp from any to 127.0.1.22 user 622 keep state
block out quick proto { tcp udp } user 621
block out quick proto { tcp udp } user 622
block out quick on lo0 proto { tcp udp } from any to { 127.0.1.21 127.0.1.22 }
"""


def _measured(subs, drop=()):
    """MEASURED_RM1B's rules (comments dropped), each ``(old, new)`` of ``subs`` applied in order, then
    the lines still naming any of ``drop`` removed."""
    out = []
    for line in MEASURED_RM1B.splitlines():
        if line.startswith("#"):
            continue
        for old, new in subs:
            line = line.replace(old, new)
        if not any(d in line for d in drop):
            out.append(line)
    return out


def test_pf_alias_rules_golden():
    """§24, V27: the egress rules ARE the measured R-M1b anchor, rule for rule and in its order (pf is
    first-match on `quick`), with numeric uids, each slot's own alias and its own front port."""
    # one slot: e0 owns 127.0.1.1 and front port 47100; B's lines (and B in each list) normalised away
    one = _measured([("{ 621 622 }", "{ 621 }"), ("{ 127.0.1.21 127.0.1.22 }", "{ 127.0.1.21 }"),
                     ("127.0.1.21", "127.0.1.1"), ("621", "3999")], drop=("622", "127.0.1.22"))
    assert len(one) == 8
    assert pf.anchor_rules([SlotId("egress", 0)], {"_levain_s0": 3999}) == one
    # two slots: e20 / e21 own exactly the measured aliases; each gets its OWN front port
    ids = {"_levain_s20": 3961, "_levain_s21": 3959}
    two = _measured([("port 47100 user 621", "port 47120 user 621"), ("port 47100 user 622", "port 47121 user 622"),
                     ("621", "3961"), ("622", "3959")])
    assert len(two) == 14
    assert pf.anchor_rules([SlotId("egress", 21), SlotId("egress", 20)], ids) == two
    # the none class is unchanged and follows the egress block
    rules = pf.anchor_rules([SlotId("egress", 20), SlotId("egress", 21), SlotId("none", 0)],
                            {**ids, "_levain_v0": 3995, "_levain_c0": 3994})
    assert rules == two + ["block out quick proto {tcp udp} user 3995", "block out quick proto {tcp udp} user 3994"]
    # no egress slot: no alias rule at all (an empty `{ }` list would not load)
    assert pf.anchor_rules([SlotId("none", 0)], {"_levain_v0": 3995, "_levain_c0": 3994}) == rules[-2:]


def test_loopback_alias_v27():
    """V27: egress slot n owns 127.0.1.<n+1>, n < 64; a verifier pair owns none."""
    assert loopback_alias(SlotId("egress", 0)) == "127.0.1.1"
    assert loopback_alias(SlotId("egress", 63)) == "127.0.1.64"
    assert len({loopback_alias(SlotId("egress", n)) for n in range(64)}) == 64
    assert _refusal(loopback_alias, SlotId("none", 0)) == "bad-token"
    assert _refusal(SlotId, "egress", 64) == "out-of-range"


_LO0 = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
\tinet 127.0.1.21 netmask 0xffffffff
\tnd6 options=201<PERFORMNUD,DAD>
"""


def test_pf_alias_skip_if_reads_ifconfig(tmp_path):
    """§24: the alias skip_if reads `ifconfig lo0` (a fake here, printing the measured output) and
    matches the inet address field exactly: no prefix match, no regex dot, no inet6 line."""
    fake = tmp_path / "ifconfig"
    fake.write_text(f"#!/bin/sh\n[ \"$1\" = lo0 ] || exit 2\ncat <<'EOF'\n{_LO0}EOF\n")
    fake.chmod(0o755)

    def rc(argv):
        return subprocess.run(list(argv), capture_output=True).returncode

    assert rc(pf.alias_present("127.0.1.21", str(fake))) == 0
    assert rc(pf.alias_absent("127.0.1.21", str(fake))) == 1
    for other in ("127.0.1.2", "127.0.1.211", "127.0.1.2x", "127x0.1.21", "::1", "0xffffffff"):
        assert rc(pf.alias_present(other, str(fake))) == 1, other
        assert rc(pf.alias_absent(other, str(fake))) == 0, other
    # the plan's steps use exactly these tests, on the real ifconfig path
    add = pf.alias_steps([SlotId("egress", 20)])
    assert add[0].skip_if == pf.alias_present("127.0.1.21") and pf.IFCONFIG in add[0].skip_if[2]


def test_pf_alias_steps_in_plans():
    """§24: setup and replenish add every egress slot's lo0 alias (the measured form, a /32) before the
    rules load; undo removes them after every slot is stopped and tombstoned, before the flush; the
    verifier class gets none; Linux plans name no alias (its netns isolates)."""
    def add(n):
        return ("/sbin/ifconfig", "lo0", "alias", f"127.0.1.{n + 1}", "netmask", "255.255.255.255")

    plan = _plan("darwin", 2, 1)
    argvs = [s.argv for s in plan.steps]
    assert [a for a in argvs if a[:1] == ("/sbin/ifconfig",)] == [add(0), add(1)]
    anchor_write = next(i for i, s in enumerate(plan.steps) if s.write and s.write[0] == pf.ANCHOR_FILE)
    assert max(argvs.index(add(0)), argvs.index(add(1))) < anchor_write
    assert pf.alias_steps(plan.allocation.slots) == [s for s in plan.steps if s.argv[:1] == ("/sbin/ifconfig",)]
    ids = dict(plan.allocation.ids)
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up("darwin", used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                         slots=3, verifier_slots=1, live={"egress": range(2), "none": range(1)}, accounts=ids)
    rargv = [s.argv for s in rep.steps]
    assert [a for a in rargv if a[:1] == ("/sbin/ifconfig",)] == [add(0), add(1), add(2)]
    assert rargv.index(add(2)) < next(i for i, s in enumerate(rep.steps) if s.write and s.write[0] == pf.ANCHOR_FILE)
    undo = _undo("darwin", ids)
    uwhys = [s.why for s in undo.steps]
    rems = [i for i, w in enumerate(uwhys) if w.startswith("remove the lo0 alias ")]
    assert [uwhys[i].split()[-1] for i in rems] == ["127.0.1.1", "127.0.1.2"]
    last_tomb = max(i for i, s in enumerate(undo.steps) if "tombstone" in s.why)
    flush = uwhys.index("flush the pf anchor com.apple/levain")
    assert last_tomb < min(rems) and max(rems) < flush
    for p in _every_plan("linux"):
        assert not [s for s in p.steps if "ifconfig" in " ".join([*s.argv, *s.skip_if]) or "alias" in s.why]


def test_pf_anchor_plan_and_undo():
    plan = _plan("darwin", 2, 1)
    w = [s for s in plan.steps if s.write and s.write[0] == pf.ANCHOR_FILE]
    assert len(w) == 1 and w[0].write[1] == pf.anchor_text(plan.allocation.slots, plan.allocation.ids)
    assert w[0].validate == ("/sbin/pfctl", "-n", "-o", "none", "-a", "com.apple/levain", "-f")
    argvs = [s.argv for s in plan.steps if s.argv]
    load = argvs.index(("/sbin/pfctl", "-o", "none", "-a", "com.apple/levain", "-f", "/etc/pf.anchors/levain"))
    enable = next(i for i, s in enumerate(plan.steps) if s.call and "enable reference" in s.why)
    assert plan.steps.index(w[0]) < plan.steps.index(next(s for s in plan.steps if s.argv == argvs[load])) < enable
    # the rules load after every slot account exists
    last_acct = max(i for i, s in enumerate(plan.steps) if s.argv[:4] == ("/usr/bin/dscl", ".", "-create", "/Users/_levain_c0"))
    assert last_acct < plan.steps.index(w[0])
    assert not [s for s in _plan("linux").steps if "pf" in s.why.split()]
    undo = _undo("darwin", dict(plan.allocation.ids))
    uwhys = [s.why for s in undo.steps]
    flush = uwhys.index("flush the pf anchor com.apple/levain")
    assert "remove /etc/pf.anchors/levain" in uwhys[flush:]
    assert f"release pf's enable reference (the token in {pf.PF_TOKEN})" in uwhys[flush:]   # it removes the token
    last_tomb = max(i for i, s in enumerate(undo.steps) if "tombstone" in s.why)
    assert last_tomb < flush
    # replenish re-renders the rules for every slot in service plus the new one, by uid
    ids = dict(plan.allocation.ids)
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up("darwin", used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                         slots=3, verifier_slots=1, live={"egress": range(2), "none": range(1)}, accounts=ids)
    rw = next(s for s in rep.steps if s.write and s.write[0] == pf.ANCHOR_FILE)
    assert rw.write[1] == pf.anchor_text([SlotId("egress", 0), SlotId("egress", 1), SlotId("egress", 2),
                                          SlotId("none", 0)], {**ids, **rep.allocation.ids})
    # without the in-service slots' uids it refuses rather than render a slot with no rules
    assert _refusal(plan_replenish, parse_install_argv(["replenish"]),
                    _set_up("darwin", used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                    slots=3, verifier_slots=1, live={"egress": range(2), "none": range(1)}) == "bad-token"


def _every_plan(host):
    plan = _plan(host, 2, 1)
    ids = dict(plan.allocation.ids)
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up(host, used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                         slots=3, verifier_slots=1, live={"egress": range(2), "none": range(1)}, accounts=ids)
    return [plan, _undo(host, ids), rep]


def test_pf_never_touches_pf_conf():
    """§23: Levain never edits /etc/pf.conf and never reloads the main ruleset (`pfctl -f
    /etc/pf.conf` flushes every service's dynamic anchors); its rules go only to com.apple/levain."""
    for p in _every_plan("darwin"):
        for s in p.steps:
            blob = " ".join([s.why, *s.argv, *s.skip_if, *s.validate, str(s.write[0]) if s.write else ""])
            assert "/etc/pf.conf" not in blob, s.why
            if s.argv[:1] == ("/sbin/pfctl",) and "-f" in s.argv:
                assert s.argv[1:5] == ("-o", "none", "-a", "com.apple/levain"), s.argv
            if s.argv[:1] == ("/sbin/pfctl",) and "-F" in s.argv:
                assert s.argv[1:3] == ("-a", "com.apple/levain"), s.argv
    assert pf.ANCHOR == "com.apple/levain"
    assert not hasattr(pf, "PF_CONF_HOOK")


class _Pf:
    """A fake pfctl: -E hands out tokens, -X releases one it handed out."""

    def __init__(self):
        self.calls, self.live, self.next = [], set(), 1000

    def __call__(self, argv, **kw):
        self.calls.append(tuple(argv))
        if argv[1] == "-E":
            self.next += 1
            self.live.add(str(self.next))
            return subprocess.CompletedProcess(argv, 0, "", f"pf enabled\nToken : {self.next}\n")
        if argv[1] == "-X":
            ok = argv[2] in self.live
            self.live.discard(argv[2])
            return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "token not found")
        raise AssertionError(argv)


def test_pf_token_released_on_undo(tmp_path):
    """§23: setup takes pf's enable reference with -E and keeps its token root 0600; undo releases
    exactly that token with -X and removes the file; a re-run never holds two references."""
    tok = tmp_path / "pf.token"
    fake = _Pf()
    assert pf.take_enable_reference(tok, fake) == (True, "")
    assert tok.read_text() == "1001\n" and stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert pf.take_enable_reference(tok, fake) == (True, "")             # a re-run of setup
    assert fake.live == {"1002"} and ("/sbin/pfctl", "-X", "1001") in fake.calls
    ok, _ = pf.release_enable_reference(tok, fake)
    assert ok and fake.live == set() and not tok.exists()
    assert fake.calls[-1] == ("/sbin/pfctl", "-X", "1002")
    assert pf.release_enable_reference(tok, fake) == (True, "")          # nothing left: a no-op
    tok.write_text("1; rm -rf /\n")
    n = len(fake.calls)
    ok, why = pf.release_enable_reference(tok, fake)                      # nothing pf could release: removed
    assert ok and "held no pf token" in why and not tok.exists()
    assert len(fake.calls) == n                                           # never passes a non-token to pfctl
    # the plan steps call exactly these
    undo = _undo("darwin", {"_levain_s0": 3999})
    rel = [s for s in undo.steps if s.call and "release pf's enable reference" in s.why]
    flush = next(i for i, s in enumerate(undo.steps) if s.why.startswith("flush the pf anchor "))
    assert len(rel) == 1 and undo.steps.index(rel[0]) > flush
    setup = _plan("darwin")
    assert len([s for s in setup.steps if s.call and "take pf's enable reference" in s.why]) == 1


# --- §9.1 Linux namespaces ---------------------------------------------------------------------------


def test_netns_setup_boot_pass_and_undo():
    plan = _plan("linux", 2, 1)
    want = netns.netns_steps(plan.allocation.slots)
    names = [s.argv[-1] for s in want if s.argv[1:3] == ("netns", "add")]
    assert names == ["levain-e0", "levain-e1", "levain-v0"]
    for s in want:
        if s.argv[1:3] == ("netns", "add"):
            # present = `ip netns list` names it (not a path test)
            assert s.skip_if[0] == "/bin/sh" and "netns list" in s.skip_if[2] and s.skip_if[-1] == s.argv[-1]
            assert "test" not in s.skip_if[2]
    assert [s.argv[2:] for s in want if s.argv[1] == "-n"] == [(n, "link", "set", "lo", "up") for n in names]
    # setup runs exactly the boot pass's list
    got = [s.argv for s in plan.steps if s.argv and s.argv[0].endswith("/ip")]
    assert got == [s.argv for s in want]
    assert not [s for s in _plan("darwin").steps if s.argv and s.argv[0].endswith("/ip")]
    undo = _undo("linux", dict(plan.allocation.ids))
    deleted = [s.why.split()[-1] for s in undo.steps if s.why.startswith("delete the network namespace ")]
    assert deleted == names
    last_kill = max(i for i, s in enumerate(undo.steps) if "stop every process" in s.why)
    first_del = next(i for i, s in enumerate(undo.steps) if s.why.startswith("delete the network namespace "))
    assert last_kill < first_del


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="no /bin/sh")
def test_netns_probe_reads_ip_netns_list(tmp_path):
    """The skip_if, run against a fake `ip` printing both line shapes: present only on an exact name."""
    ip = tmp_path / "ip"
    ip.write_text("#!/bin/sh\nprintf 'levain-e10 (id: 3)\\nlevain-v0\\n'\n")
    ip.chmod(0o755)
    real = hands.shutil.which

    def which(n, path=None):
        return str(ip) if n == "ip" else real(n, path=path) or real(n)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hands.shutil, "which", which)
        for name, present in (("levain-e10", True), ("levain-v0", True), ("levain-e1", False), ("levain", False)):
            assert subprocess.run(netns.netns_present(name)).returncode == (0 if present else 1), name
            assert subprocess.run(netns.netns_absent(name)).returncode == (1 if present else 0), name



# --- §1.2 the slots' bwrap ---------------------------------------------------------------------------


GOLDEN_PROFILE = """# Levain session slots' bubblewrap (CONTRACT §1.2). Written by the install helper; removed by its undo verb.
abi <abi/4.0>,
include <tunables/global>

profile levain-slot-bwrap /usr/libexec/levain/bwrap flags=(unconfined) {
  userns,

  include if exists <local/levain-slot-bwrap>
}
"""


def test_bwrap_copy_and_apparmor_profile():
    assert bwrap.apparmor_profile_text() == GOLDEN_PROFILE
    # never hands' floor profile name, so ours never replaces it
    assert bwrap.APPARMOR_PROFILE_NAME == "levain-slot-bwrap" != "levain-bwrap"
    assert bwrap.APPARMOR_PROFILE == Path("/etc/apparmor.d/levain-slot-bwrap")
    for restricted, want in (("1\n", True), ("0\n", False)):
        plan = _plan("linux", restricted=restricted)
        cp = [s.argv for s in plan.steps if s.argv and s.argv[0].endswith("install")]
        assert len(cp) == 1 and cp[0][1:7] == ("-o", "root", "-g", "levain-slots", "-m", "0750")
        assert cp[0][-1] == "/usr/libexec/levain/bwrap"
        prof = [s for s in plan.steps if s.write and s.write[0] == bwrap.APPARMOR_PROFILE]
        assert bool(prof) is want
        if want:
            assert prof[0].write[1] == GOLDEN_PROFILE
    # the read is of the sysctl R-L2 named; an absent file = unrestricted
    seen = []
    assert bwrap.userns_restricted(lambda p: seen.append(p) or "1") and seen == [bwrap.USERNS_SYSCTL]

    def absent(p):
        raise FileNotFoundError(p)

    assert bwrap.userns_restricted(absent) is False
    assert not [s for s in _plan("darwin").steps if "bwrap" in s.why]
    # undo unloads and removes only ours
    undo = _undo("linux", {"_levain_s0": 899})
    aa = [s.why for s in undo.steps if "AppArmor" in s.why or "apparmor.d" in s.why]
    assert aa == ["unload the AppArmor profile levain-slot-bwrap", "remove /etc/apparmor.d/levain-slot-bwrap"]


# --- §2.1 the root import tree -----------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_helper_imports_root_tree_only(host):
    tree = str(IMPORT_TREE[host])
    for helper, path in (("install-helper", install_helper_path(host)), ("runtime-helper", runtime_helper_path(host))):
        text = root_tree.helper_entry_text(host, helper)
        lines = text.splitlines()
        assert lines[0] == "#!/usr/bin/python3 -IS"                      # isolated (-I), no site dir (-S)
        assert "-I" in lines[0]
        path_lines = [ln for ln in lines if "sys.path" in ln and not ln.startswith("#")]
        # the tree is the ONE path added: its current generation, resolved once at start (row 33 R1)
        assert path_lines == [f"import os, sys; sys.path.insert(0, os.path.realpath({tree + '/current'!r}))"]
        assert "PYTHONPATH" not in text and "site" not in text.replace("site-packages", "")
        plan = _plan(host)
        w = [s for s in plan.steps if s.write and s.write[0] == path]
        assert len(w) == 1 and w[0].write[1:] == (text, 0o755)
    plan = _plan(host)
    argvs = [s.argv for s in plan.steps if s.argv]
    cp = next(a for a in argvs if a[0].endswith("cp"))
    partial = f"{tree}/staging.partial"                                   # row 33 R1: one staged generation
    assert cp[1:3] == ("-R", "-P") and cp[-1] == f"{partial}/levain" and Path(cp[-2]).name == "levain"
    assert any(a[0].endswith("chmod") and a[1:] == ("0755", tree) for a in argvs)
    assert any(a[0].endswith("chmod") and a[1:] == ("-R", "u=rwX,go=rX", partial) for a in argvs)
    whys = [s.why for s in plan.steps]
    into = whys.index(f"rename {partial} to {tree}/{plan.generation}")
    assert whys.index(f"flip {tree}/current -> {plan.generation}") > into
    checks = [s.why for s in plan.steps if s.call]
    assert any("only root's plain files" in w for w in checks)
    assert any(f"above {tree} is root's alone" in w for w in checks)
    assert _refusal(root_tree.helper_entry_text, host, "other-helper") == "bad-token"
    assert _refusal(root_tree.tree_steps, host, Path("/tmp/notlevain")) == "bad-root"
    # The pending lines are derived from the helpers (S1 2e): the runtime helper exists, so nothing says it does
    # not; each helper's line names exactly its verbs with no handler.
    assert not any("does not exist" in p or "apply layer is not built" in p for p in plan.pending)
    from levain.slot import install_helper, runtime_helper
    from levain.slot.grammar import VERBS as INSTALL_VERBS
    from levain.slot.runtime_grammar import VERBS as RUNTIME_VERBS
    lines = {p.split(" answers ")[0]: p.split(": ", 1)[1].split(", ") for p in plan.pending if " answers verb-unbuilt" in p}
    assert lines.get("levain.slot.install_helper") == [v for v in INSTALL_VERBS if v not in install_helper.BUILT_VERBS]
    assert lines.get("levain.slot.runtime_helper", []) == [v for v in RUNTIME_VERBS
                                                           if v not in runtime_helper.VERB_HANDLERS]
    assert "setup" not in lines["levain.slot.install_helper"] and "prepare" not in lines.get("levain.slot.runtime_helper", [])
