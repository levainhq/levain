"""S1 session 1b-i plan layer (CONTRACT c7.2 §1.1-1.3, §2.1, §8.3, §9.1, §20): services, the pf anchor,
Linux namespaces, the slots' bwrap, the root import tree, singleton revival. Every row is a plan or text
assertion; nothing touches the OS (the one file write is to a pytest tmp file, for the pf.conf hook)."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from levain.firing.hands import _ensure_line
from levain.slot import Refused, bwrap, netns, pf, root_tree, units
from levain.slot.accounts import SLOT_MARKER
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import RANGES, SHARED_GROUPS, SINGLETONS, SlotId
from levain.slot.install_helper import HostFacts, operator_record, plan_replenish, plan_setup, plan_undo
from levain.slot.layout import IMPORT_TREE, install_helper_path, runtime_helper_path

HOSTS = ("linux", "darwin")
OP, OP_UID = "alice", 501


def _setup(n=8, m=2):
    return parse_install_argv(["setup", "--slots", str(n), "--verifier-slots", str(m), "--operator", OP])


def _facts(host, **kw):
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID)
    base.update(kw)
    return HostFacts(host, **base)


def _set_up(host, **kw):
    return _facts(host, operator_record=operator_record(OP, OP_UID), **kw)


def _plan(host, n=8, m=2, restricted="0", **kw):
    return plan_setup(_setup(n, m), _facts(host, **kw), read_sysctl=lambda p: restricted)


def _refusal(fn, *a, **kw) -> str:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value.name


def _undo(host, accounts, **kw):
    return plan_undo(parse_install_argv(["undo"]), _set_up(host, **kw), accounts=accounts, shared_groups=SHARED_GROUPS)


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
		<string>/usr/local/lib/levain-root/levain/slot/services/control.py</string>
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
ExecStart=/usr/bin/python3 -IS /usr/lib/levain-root/levain/slot/services/dispatch.py
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
"""

GOLDEN_REAPER_TIMER = """# Written by the Levain install helper; removed by its undo verb.
[Unit]
Description=Levain reaper wake (CONTRACT §1.3)

[Timer]
OnBootSec=60
OnUnitActiveSec=60
Unit=levain-reaper.service

[Install]
WantedBy=timers.target
"""

#: §1.3 typed out: name -> (uid, starts after, restart, macOS label, Linux unit)
TABLE_1_3 = {
    "control": ("_levain_control", (), "always", "com.levain.control", "levain-control.service"),
    "keys": ("_levain_keys", ("control",), "always", "com.levain.keys", "levain-keys.service"),
    "qbroker": ("_levain_qbroker", ("control",), "always", "com.levain.qbroker", "levain-qbroker.service"),
    "dispatch": ("_levain_dispatch", ("control", "keys"), "always", "com.levain.dispatch", "levain-dispatch.service"),
    "reaper": ("root", ("control",), "always", "com.levain.reaper", "levain-reaper.service"),
    "pf": ("root", (), "one-shot", "com.levain.pf", None),
}


def test_service_units_golden():
    by = {s.name: s for s in units.SERVICES}
    assert set(by) == set(TABLE_1_3)
    assert units.launchd_plist_text(by["control"]) == GOLDEN_CONTROL_PLIST
    assert units.systemd_unit_text(by["dispatch"]) == GOLDEN_DISPATCH_UNIT
    assert units.reaper_timer_text() == GOLDEN_REAPER_TIMER
    for name, (uid, after, restart, label, unit) in TABLE_1_3.items():
        s = by[name]
        assert (s.user, s.after, s.restart) == (uid, after, restart), name
        job = plistlib.loads(units.launchd_plist_text(s).encode())
        assert job["Label"] == label and job["UserName"] == uid and job["KeepAlive"] is (restart == "always")
        assert job["ProgramArguments"] == ["/usr/bin/python3", "-IS",
                                           f"/usr/local/lib/levain-root/levain/slot/services/{name}.py"]
        if unit is None:
            assert "linux" not in s.hosts
            assert _refusal(units.systemd_unit_text, s) == "unsupported-host"
            continue
        text = units.systemd_unit_text(s)
        assert units.systemd_name(s) == unit
        assert f"User={uid}\n" in text and "Restart=always\n" in text
        assert f"ExecStart=/usr/bin/python3 -IS /usr/lib/levain-root/levain/slot/services/{name}.py\n" in text
        deps = " ".join(TABLE_1_3[a][4] for a in after)
        assert (f"After={deps}\n" in text) if after else ("After=" not in text)
    # the program bodies are named as not built, in the plan output
    plan = _plan("linux")
    assert all(any(f"services/{n}.py" in p and "does not exist yet" in p for p in plan.pending) for n in TABLE_1_3)


@pytest.mark.parametrize("host", HOSTS)
def test_services_start_order(host):
    plan = _plan(host)
    if host == "darwin":
        starts = [s.argv[-1] for s in plan.steps if s.argv[:3] == ("/bin/launchctl", "bootstrap", "system")]
        names = [Path(p).name.removeprefix("com.levain.").removesuffix(".plist") for p in starts]
        writes = [i for i, s in enumerate(plan.steps) if s.write and str(s.write[0]).startswith("/Library/LaunchDaemons/")]
    else:
        starts = [s.argv[-1] for s in plan.steps if s.argv[1:3] == ("enable", "--now")]
        names = [n.removeprefix("levain-").removesuffix(".service") for n in starts if n.endswith(".service")]
        assert starts[starts.index("levain-reaper.service") + 1] == "levain-reaper.timer"
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
    tree_write = next(i for i, s in enumerate(plan.steps) if s.write and s.write[0] == install_helper_path(host))
    assert tree_write < first_start
    # undo: every service stopped right after sudoers, reverse order, before anything is removed
    undo = _undo(host, dict(plan.allocation.ids))
    stops = [i for i, s in enumerate(undo.steps) if s.argv and ("bootout" in s.argv or "disable" in s.argv)]
    assert stops and min(stops) == 2
    first_removal = next(i for i, s in enumerate(undo.steps) if s.write or "tombstone" in s.why or
                         (s.argv and s.argv[0].endswith("rm") and "sudoers" not in s.argv[-1]))
    assert max(stops) < first_removal
    stopped = [undo.steps[i].argv[-1] for i in stops]
    assert stopped == ([f"system/{lb}" for lb in reversed(units.start_order(host))] if host == "darwin"
                       else list(reversed(units.start_order(host))))


# --- §8.3 pf anchor -----------------------------------------------------------------------------------


GOLDEN_ANCHOR = """# Levain session slots (CONTRACT §8.3). Written by the install helper; removed by its undo verb.
pass out quick proto tcp from any to 127.0.0.1 port 47100 user _levain_s0
block out quick proto {tcp udp} user _levain_s0
pass out quick proto tcp from any to 127.0.0.1 port 47101 user _levain_s1
block out quick proto {tcp udp} user _levain_s1
block out quick proto {tcp udp} user _levain_v0
block out quick proto {tcp udp} user _levain_c0
"""


def test_pf_anchor_golden():
    slots = [SlotId("none", 0), SlotId("egress", 1), SlotId("egress", 0)]
    assert pf.anchor_text(slots) == GOLDEN_ANCHOR
    # every egress pass names its OWN port and its own user; nothing passes a verifier account
    rules = pf.anchor_rules([SlotId("egress", n) for n in range(32)] + [SlotId("none", n) for n in range(16)])
    for n in range(32):
        i = rules.index(f"pass out quick proto tcp from any to 127.0.0.1 port {47100 + n} user _levain_s{n}")
        assert rules[i + 1] == f"block out quick proto {{tcp udp}} user _levain_s{n}"
    assert not [r for r in rules if r.startswith("pass") and ("_levain_v" in r or "_levain_c" in r)]
    assert len(rules) == 2 * 32 + 2 * 16
    assert _refusal(pf.gwport, SlotId("none", 0)) == "bad-token"


def test_pf_anchor_plan_and_undo():
    plan = _plan("darwin", 2, 1)
    w = [s for s in plan.steps if s.write and s.write[0] == pf.ANCHOR_FILE]
    assert len(w) == 1 and w[0].write[1] == pf.anchor_text(plan.allocation.slots)
    assert w[0].validate == ("/sbin/pfctl", "-n", "-a", "levain", "-f")
    argvs = [s.argv for s in plan.steps if s.argv]
    assert ("/sbin/pfctl", "-a", "levain", "-f", "/etc/pf.anchors/levain") in argvs
    assert argvs.index(("/sbin/pfctl", "-n", "-f", "/etc/pf.conf")) < argvs.index(("/sbin/pfctl", "-f", "/etc/pf.conf"))
    hooks = [s for s in plan.steps if s.call and "/etc/pf.conf" in s.why]
    assert len(hooks) == 2
    # the anchor loads after every slot account exists (pf resolves user names at load)
    last_acct = max(i for i, s in enumerate(plan.steps) if s.argv[:4] == ("/usr/bin/dscl", ".", "-create", "/Users/_levain_c0"))
    assert last_acct < plan.steps.index(w[0])
    assert not [s for s in _plan("linux").steps if "pf" in s.why.split()]
    undo = _undo("darwin", dict(plan.allocation.ids))
    uargv = [s.argv for s in undo.steps if s.argv]
    flush = uargv.index(("/sbin/pfctl", "-a", "levain", "-F", "all"))
    assert ("/bin/rm", "-f", "/etc/pf.anchors/levain") in uargv[flush:]
    last_tomb = max(i for i, s in enumerate(undo.steps) if "tombstone" in s.why)
    assert last_tomb < undo.steps.index(next(s for s in undo.steps if s.argv == uargv[flush]))
    # replenish re-renders the anchor for every slot in service plus the new one
    ids = dict(plan.allocation.ids)
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up("darwin", used_ids=frozenset(ids.values()), existing_names=frozenset(ids)),
                         slots=3, verifier_slots=1, live={"egress": range(2), "none": range(1)})
    rw = next(s for s in rep.steps if s.write and s.write[0] == pf.ANCHOR_FILE)
    assert rw.write[1] == pf.anchor_text([SlotId("egress", 0), SlotId("egress", 1), SlotId("egress", 2),
                                          SlotId("none", 0)])


def test_pf_conf_hook_idempotent(tmp_path):
    conf = tmp_path / "pf.conf"
    conf.write_text('scrub-anchor "com.apple/*"\nanchor "com.apple/*"\nload anchor "com.apple" from "/etc/pf.anchors/com.apple"\n')
    original = conf.read_text()
    for _ in range(2):
        for line in pf.PF_CONF_HOOK:
            _ensure_line(conf, line, present=True)
    once = conf.read_text()
    assert once == original + "".join(f"{ln}\n" for ln in pf.PF_CONF_HOOK)
    for line in reversed(pf.PF_CONF_HOOK):
        _ensure_line(conf, line, present=False)
    assert conf.read_text() == original


# --- §9.1 Linux namespaces ---------------------------------------------------------------------------


def test_netns_setup_boot_pass_and_undo():
    plan = _plan("linux", 2, 1)
    want = netns.netns_steps(plan.allocation.slots)
    names = [s.argv[-1] for s in want if s.argv[1:3] == ("netns", "add")]
    assert names == ["levain-e0", "levain-e1", "levain-v0"]
    for s in want:
        if s.argv[1:3] == ("netns", "add"):
            assert s.skip_if[-2:] == ("-e", f"/run/netns/{s.argv[-1]}")
    assert [s.argv[2:] for s in want if s.argv[1] == "-n"] == [(n, "link", "set", "lo", "up") for n in names]
    # setup runs exactly the boot pass's list
    got = [s.argv for s in plan.steps if s.argv and s.argv[0].endswith("ip")]
    assert got == [s.argv for s in want]
    assert not [s for s in _plan("darwin").steps if s.argv and s.argv[0].endswith("/ip")]
    undo = _undo("linux", dict(plan.allocation.ids))
    deleted = [s.argv[-1] for s in undo.steps if s.argv[1:3] == ("netns", "delete")]
    assert deleted == names
    last_kill = max(i for i, s in enumerate(undo.steps) if "stop every process" in s.why)
    first_del = next(i for i, s in enumerate(undo.steps) if s.argv[1:3] == ("netns", "delete"))
    assert last_kill < first_del


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
    touched = [a for s in undo.steps if s.argv for a in s.argv if "apparmor.d" in a]
    assert touched and all(a == "/etc/apparmor.d/levain-slot-bwrap" for a in touched)


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
        assert path_lines == [f"sys.path.insert(0, {tree!r})"]           # the tree is the ONE path added
        assert "PYTHONPATH" not in text and "site" not in text.replace("site-packages", "")
        plan = _plan(host)
        w = [s for s in plan.steps if s.write and s.write[0] == path]
        assert len(w) == 1 and w[0].write[1:] == (text, 0o755)
    plan = _plan(host)
    argvs = [s.argv for s in plan.steps if s.argv]
    cp = next(a for a in argvs if a[0].endswith("cp"))
    assert cp[1:3] == ("-R", "-P") and cp[-1] == f"{tree}/levain.new" and Path(cp[-2]).name == "levain"
    assert any(a[0].endswith("chmod") and a[1:] == ("0755", tree) for a in argvs)
    assert any(a[0].endswith("chmod") and a[1:] == ("-R", "u=rwX,go=rX", f"{tree}/levain.new") for a in argvs)
    assert any(a[-2:] == (f"{tree}/levain.new", f"{tree}/levain") for a in argvs)
    checks = [s.why for s in plan.steps if s.call]
    assert any("only root's plain files" in w for w in checks)
    assert any(f"above {tree} is root's alone" in w for w in checks)
    assert _refusal(root_tree.helper_entry_text, host, "other-helper") == "bad-token"
    assert _refusal(root_tree.tree_steps, host, Path("/tmp/notlevain")) == "bad-root"
    assert any("runtime_helper does not exist yet" in p for p in plan.pending)
