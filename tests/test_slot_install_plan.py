"""The install helper's plan layer (CONTRACT c7.1 §1.1, §1.2, §2.1, §2.2, §5.1, §5.2): every row here is
a plan assertion; nothing touches the OS."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.firing import hands
from levain.firing.hands import Step
from levain.slot import Refused
from levain.slot.accounts import OPERATOR_GROUPS, SLOT_MARKER, supplementary_groups
from levain.slot.grammar import LEASE_EXEMPT, VERBS, parse_install_argv
from levain.slot.ids import (RANGES, SHARED_GROUPS, SINGLETONS, SlotId, read_retired_ids, retired_ids_text,
                             write_retired_ids)
from levain.slot.install_helper import (HostFacts, operator_record, operator_record_text, parse_operator_record,
                                        plan_replenish, plan_setup, plan_undo, preflight)
from levain.slot.intent import INTENT_PATH
from levain.slot.layout import RUN, STORE, operator_record_path
from levain.slot.sudoers import (INSTALL_SUDOERS, RUNTIME_SUDOERS, install_sudoers_text, runtime_sudoers_text)

HOSTS = ("linux", "darwin")


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    """A plan for the OTHER host names tools this test host lacks; the §23 preflight refuses a bare
    name, so here every tool resolves (to its real path when present)."""
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")
OP, OP_UID = "alice", 501


def _setup(n=8, m=2, operator=OP):
    return parse_install_argv(["setup", "--slots", str(n), "--verifier-slots", str(m), "--operator", operator])


def _facts(host, **kw):
    # every host tool present: the plan, not this test host, decides what it names (§23 preflight)
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                xcode_ready=lambda: True)
    base.update(kw)
    return HostFacts(host, **base)


def _set_up(host, **kw):
    return _facts(host, operator_record=operator_record(OP, OP_UID), **kw)


def _refusal(fn, *a, **kw) -> str:
    with pytest.raises(Refused) as ei:
        fn(*a, **kw)
    return ei.value.name


def _argvs(plan):
    return [s.argv for s in plan.steps if s.argv]


# --- §1.1 ids ---------------------------------------------------------------------------------------


def test_id_ranges_disjoint_from_hands():
    ours = {h: [set(RANGES[h].slots), set(RANGES[h].singletons), set(RANGES[h].shared)] for h in HOSTS}
    hands_ids = set(hands._DARWIN_IDS) | set(hands._LINUX_IDS)
    for host in HOSTS:
        a, b, c = ours[host]
        assert not (a & b) and not (a & c) and not (b & c)
        assert not ((a | b | c) & hands_ids), host
    assert hands_ids >= {400, 499, 900, 999}


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("n,m", [(8, 2), (32, 16)])
def test_default_setup_fits_gid_range(host, n, m):
    plan = plan_setup(_setup(n, m), _facts(host))
    r, alloc = RANGES[host], plan.allocation
    assert len(alloc.egress) == n and len(alloc.verifier) == m
    slot_names = [x for s in alloc.slots for x in s.accounts]
    assert len(slot_names) == 2 * n + 2 * m
    assert all(alloc.ids[x] in r.slots for x in slot_names)
    assert [alloc.ids[x] for x in slot_names] == list(r.slots)[:len(slot_names)]   # top-down
    assert all(alloc.ids[x] in r.singletons for x in SINGLETONS)
    assert set(alloc.shared) == set(SHARED_GROUPS) and len(SHARED_GROUPS) <= len(r.shared)
    assert all(g in r.shared for g in alloc.shared.values())
    every = list(alloc.ids.values()) + list(alloc.shared.values())
    assert len(every) == len(set(every))
    assert all(a.gid == a.uid for a in plan.accounts)


@pytest.mark.parametrize("host", HOSTS)
def test_capacity_refused_by_name(host):
    r = RANGES[host]
    room = r.capacity - (2 * 32 + 2 * 16)
    retired = frozenset(list(r.slots)[-room:])     # tombstones at the bottom of the slot range
    plan_setup(_setup(32, 16), _facts(host, retired=retired))     # positive control: exactly full
    over = frozenset(list(r.slots)[-(room + 1):])
    assert _refusal(plan_setup, _setup(32, 16), _facts(host, retired=over)) == "capacity"
    # replenish counts tombstones the same way
    assert _refusal(plan_replenish, parse_install_argv(["replenish"]), _set_up(host, retired=over),
                    slots=32, verifier_slots=16, live={"egress": range(32), "none": range(16)}) == "capacity"


def test_index_space_exhausted_by_name():
    spent = frozenset(f"_levain_s{i}" for i in range(60))
    assert _refusal(plan_setup, _setup(8, 2), _facts("linux", existing_names=spent)) == "index-space-exhausted"
    rec = _set_up("darwin", existing_names=frozenset(f"_levain_v{i}" for i in range(64)))
    assert _refusal(plan_replenish, parse_install_argv(["replenish"]), rec, slots=8, verifier_slots=2,
                    live={"egress": range(8), "none": ()}) == "index-space-exhausted"


@pytest.mark.parametrize("host", HOSTS)
def test_retired_id_never_reissued(host, tmp_path):
    top = list(RANGES[host].slots)
    retired = frozenset({top[0], top[2]})
    plan = plan_setup(_setup(), _facts(host, retired=retired, used_ids=frozenset({top[3]})))
    issued = set(plan.allocation.ids.values())
    assert not issued & retired and top[3] not in issued and top[1] in issued
    # the file round-trips; malformed is refused by name
    f = tmp_path / "retired-session-ids"
    assert read_retired_ids(f) == frozenset()
    write_retired_ids(f, retired)
    assert read_retired_ids(f) == retired
    f.write_text("899\n../etc\n")
    assert _refusal(read_retired_ids, f) == "retired-ids-malformed"
    # undo retires every account id it tombstones, keeping the old ones
    accounts = dict(plan.allocation.ids)
    undo = plan_undo(parse_install_argv(["undo"]), _set_up(host, retired=retired), accounts=accounts,
                     shared_groups={g: RANGES[host].shared[i] for i, g in enumerate(SHARED_GROUPS)})
    assert undo.retired_after == retired | set(accounts.values())
    write = [s.write for s in undo.steps if s.write and s.write[0].name == "retired-session-ids"]
    assert write and write[0][1] == retired_ids_text(undo.retired_after) and write[0][2] == 0o644
    # replenish after a retire takes a NEW index and a NEW id
    gone = frozenset({accounts["_levain_s0"], accounts["_levain_gw0"]})
    rep = plan_replenish(parse_install_argv(["replenish"]),
                         _set_up(host, retired=retired | gone, used_ids=frozenset(accounts.values()),
                                 existing_names=frozenset(accounts)),
                         slots=8, verifier_slots=2, live={"egress": range(1, 8), "none": range(2)},
                         accounts={k: v for k, v in accounts.items() if k not in ("_levain_s0", "_levain_gw0")
                                   and k not in SINGLETONS})
    assert [s.tag for s in rep.allocation.slots] == ["e8"]
    assert not set(rep.allocation.ids.values()) & (retired | gone | set(accounts.values()))


# --- operator ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_second_operator_refused_by_name(host):
    rec = operator_record(OP, OP_UID)
    bob = _facts(host, caller_uid=502, caller_name="bob", operator_uid=502, operator_record=rec)
    assert _refusal(plan_setup, _setup(operator="bob"), bob) == "second-operator"
    same_name_other_uid = _facts(host, caller_uid=777, operator_uid=777, operator_record=rec)
    assert _refusal(plan_setup, _setup(), same_name_other_uid) == "second-operator"
    assert _refusal(plan_setup, _setup(), _facts(host, operator_record=rec)) == "already-set-up"
    for verb in ("repair", "replenish", "set-ceiling", "undo"):
        assert _refusal(preflight, parse_install_argv([verb]), bob) == "operator-mismatch"
        assert _refusal(preflight, parse_install_argv([verb]),
                        _facts(host, caller_name="bob", operator_record=rec)) == "operator-mismatch"
        assert _refusal(preflight, parse_install_argv([verb]), _facts(host)) == "not-set-up"
        preflight(parse_install_argv([verb]), _facts(host, operator_record=rec))   # positive control
    assert _refusal(plan_setup, _setup(), _facts(host, caller_uid=0)) == "caller-not-operator"
    assert _refusal(plan_setup, _setup(), _facts(host, operator_uid=None)) == "no-such-operator"


@pytest.mark.parametrize("host", HOSTS)
def test_operator_record_written_last_and_removed_last(host):
    """§23 + §25: the record is setup's last act on the install (``already-set-up`` = a complete
    install), followed only by the journal's removal, and undo's last removal; undo takes the runtime
    rule out first and the install rule out just before the journal and the record."""
    plan = plan_setup(_setup(), _facts(host))
    writes = [i for i, s in enumerate(plan.steps) if s.write and s.write[0] == operator_record_path(host)]
    assert writes == [len(plan.steps) - 2]
    assert plan.steps[-1].argv[1:] == ("-f", str(INTENT_PATH[host]))
    path, text, mode = plan.steps[writes[0]].write
    assert mode == 0o644 and parse_operator_record(text.encode()) == operator_record(OP, OP_UID)
    assert text == operator_record_text(OP, OP_UID)
    for bad in (b'{"name":"alice","uid":501}', b'{"format":"levain-operator/1","name":"alice","uid":0}',
                b'{"format":"levain-operator/1","name":"alice","uid":5.0}', b"not json"):
        assert _refusal(parse_operator_record, bad) == "operator-record-malformed"
    uid = RANGES[host].slots[0]
    undo = plan_undo(parse_install_argv(["undo"]), _set_up(host), accounts={"_levain_s0": uid},
                     shared_groups={g: RANGES[host].shared[i] for i, g in enumerate(SHARED_GROUPS)})
    # FIX1 M1: the check after the install rule's removal is the LAST step, after the operator record
    assert undo.steps[-1].why.startswith("check the sudoers config with visudo -c (after ")
    assert undo.steps[-2].argv[1:] == ("-f", str(operator_record_path(host)))
    # FIX1 M1/M2: the sudoers preflight, then undo's own journal, before the runtime rule
    assert undo.steps[0].why.startswith("check the sudoers config with visudo -c before undo removes anything")
    assert undo.steps[1].why.startswith("journal what this undo ")
    assert undo.steps[2].why.startswith(f"remove {RUNTIME_SUDOERS} ")
    assert undo.steps[-3].argv[1:] == ("-f", str(INTENT_PATH[host]))
    # the install rule's removal (R1b: sudoers/1 through its per-identity step), checked while it still stands
    assert undo.steps[-4].why.startswith(f"remove {INSTALL_SUDOERS} ")
    assert undo.steps[-5].why == f"check the sudoers config with visudo -c before {INSTALL_SUDOERS} goes"
    assert [s.why.split()[1] for s in undo.steps if s.why.startswith("remove /") and "sudoers" in s.why] == [
        str(RUNTIME_SUDOERS), str(INSTALL_SUDOERS)]
    retire = next(i for i, s in enumerate(undo.steps) if s.write)
    first_tomb = next(i for i, s in enumerate(undo.steps) if "tombstone" in s.why)
    first_kill = next(i for i, s in enumerate(undo.steps) if s.call and "stop every process" in s.why)
    assert 1 <= retire < first_kill < first_tomb


# --- §1.2 groups ---------------------------------------------------------------------------------------

#: CONTRACT §1.2's table, typed out: the test's oracle is the contract, not the module.
TABLE = {
    "levain-slots": "every slot account (s, v, c)",
    "levain-gwfronts": {"_levain_gw0", "_levain_gw1", "_levain_gw2", "_levain_keys"},   # every gw, and keys (E12)
    "levain-control": {"OP", "_levain_keys", "_levain_qbroker", "_levain_dispatch"},
    "levain-wake": {"_levain_control"},
    "levain-lineage": {"_levain_dispatch"},
    "levain-export": {"OP"},
    "levain-dispatch-export": {"_levain_dispatch"},
    "levain-queue": {"OP", "_levain_dispatch", "_levain_qbroker"},
    "levain-keysadmin": {"_levain_keys", "_levain_control"},
}


@pytest.mark.parametrize("host", HOSTS)
def test_slot_groups_exact(host):
    plan = plan_setup(_setup(3, 2), _facts(host))
    members = {g.name: set(g.members) for g in plan.groups}
    assert set(members) == set(TABLE) == set(SHARED_GROUPS)
    slots = {f"_levain_{p}{i}" for p in "s" for i in range(3)} | {f"_levain_{p}{i}" for p in "vc" for i in range(2)}
    assert members["levain-slots"] == slots
    assert members["levain-gwfronts"] == {f"_levain_gw{i}" for i in range(3)} | {"_levain_keys"}
    for g, want in TABLE.items():
        if isinstance(want, set):
            assert members[g] == {OP if x == "OP" else x for x in want}, g
    # a slot uid is in exactly its own group + levain-slots; a front in its own + levain-gwfronts
    acct = {a.name: a for a in plan.accounts}
    for name in slots:
        assert acct[name].groups == ("levain-slots",)
    assert set(OPERATOR_GROUPS) == {g for g, m in members.items() if OP in m}
    # the membership steps add exactly these pairs, nothing else
    if host == "darwin":
        pairs = {(s.argv[4], s.argv[7]) for s in plan.steps if s.argv[:1] == ("/usr/sbin/dseditgroup",)}
    else:
        pairs = {(s.argv[4], s.argv[3]) for s in plan.steps if s.argv[1:3] == ("--append", "--groups")}
    assert pairs == {(m, g) for g, ms in members.items() for m in ms}
    assert _refusal(supplementary_groups, "_levain_s64") == "not-levain-name"
    assert _refusal(supplementary_groups, "bob") == "not-levain-name"


def test_egress0_and_verifier0_disjoint():
    e0, v0 = SlotId("egress", 0), SlotId("none", 0)
    store = STORE["linux"]
    assert set(e0.accounts).isdisjoint(v0.accounts)
    assert e0.lock_path(store) != v0.lock_path(store)
    assert (e0.lock_path(store), v0.lock_path(store)) == (store / "slots/e0/lock", store / "slots/v0/lock")
    assert (e0.netns, v0.netns) == ("levain-e0", "levain-v0")
    assert (e0.cgroup, v0.cgroup) == ("e0.scope", "v0.scope")
    plan = plan_setup(_setup(1, 1), _facts("linux"))
    paths = {r.path for r in plan.rows}
    assert {e0.lock_path(store), v0.lock_path(store), e0.slot_dir(store), v0.slot_dir(store)} <= paths
    ids = plan.allocation.ids
    assert len({ids[x] for x in e0.accounts + v0.accounts}) == 4


@pytest.mark.parametrize("verb", VERBS)
def test_install_verbs_refuse_with_live_lease(verb):
    argv = {"setup": ["setup", "--slots", "8", "--verifier-slots", "2", "--operator", OP],
            "set-retention": ["set-retention", "30"], "set-dispatch-retain": ["set-dispatch-retain", "3"],
            "purge": ["purge", "s-0123456789abcdef"], "force-retire": ["force-retire", "egress", "0"],
            "set-credential": ["set-credential", "anthropic"],
            "remove-credential": ["remove-credential", "anthropic"],
            "enrol-authority-signer": ["enrol-authority-signer", "--principal", OP, "--public-key",
                                       "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA=="],
            "remove-authority-signer": ["remove-authority-signer", "--key", "SHA256:" + "A" * 43]}.get(verb, [verb])
    cmd = parse_install_argv(argv)
    facts = _set_up("linux", live_leases=1) if verb != "setup" else _facts("linux", live_leases=1)
    assert LEASE_EXEMPT == {"purge", "force-retire", "set-credential", "remove-credential", "set-limits",
                            "routes-base", "routes-trust", "routes-egress", "enrol-authority-signer",
                            "remove-authority-signer"}
    if verb in LEASE_EXEMPT:
        preflight(cmd, facts)                 # the lease-exempt verbs pass
    else:
        assert _refusal(preflight, cmd, facts) == "live-lease"
    # the lease refusal comes before the operator check
    if verb not in LEASE_EXEMPT:
        assert _refusal(preflight, cmd, _facts("linux", live_leases=1, caller_uid=9)) == "live-lease"


@pytest.mark.parametrize("host", HOSTS)
def test_slot_account_shape(host):
    plan = plan_setup(_setup(1, 1), _facts(host))
    # §1.1's char bound is on the slot names (widest index); every name stays inside Linux's limit
    assert all(len(n) <= 15 for n in SlotId("egress", 63).accounts + SlotId("none", 63).accounts)
    assert all(len(n) <= 32 for n in SINGLETONS)
    for name in ("_levain_s0", "_levain_gw0", "_levain_v0", "_levain_c0", *SINGLETONS):
        uid = plan.allocation.ids[name]
        argvs = _argvs(plan)
        if host == "darwin":
            u, g = f"/Users/{name}", f"/Groups/{name}"
            want = {("-create", g, "PrimaryGroupID", str(uid)), ("-create", u, "UniqueID", str(uid)),
                    ("-create", u, "PrimaryGroupID", str(uid)), ("-create", u, "UserShell", "/usr/bin/false"),
                    ("-create", u, "IsHidden", "1"), ("-create", u, "Password", "*"),
                    ("-create", u, "AuthenticationAuthority", ";DisabledUser;"),
                    ("-create", u, "RealName", SLOT_MARKER)}
            have = {a[2:] for a in argvs if a[:2] == ("/usr/bin/dscl", ".")}
            assert want <= have, name
        else:
            gadd = [a for a in argvs if a[-1] == name and a[0].endswith("groupadd")]
            uadd = [a for a in argvs if a[-1] == name and a[0].endswith("useradd")]
            assert gadd == [(gadd[0][0], "--system", "--gid", str(uid), name)]
            assert len(uadd) == 1
            u = uadd[0]
            assert "--system" in u and u[u.index("--uid") + 1] == str(uid) == u[u.index("--gid") + 1]
            assert u[u.index("--shell") + 1].endswith("nologin") or u[u.index("--shell") + 1] == "/bin/false"
            assert "--no-create-home" in u and "--create-home" not in u
            assert any(a[0].endswith("usermod") and a[1:] == ("--lock", name) for a in argvs)
    assert not hands.HANDS_USER_RE.fullmatch("_levain_s0")
    assert not SLOT_MARKER.startswith(hands.HANDS_MARKER)


@pytest.mark.parametrize("host", HOSTS)
def test_slot_socket_group_inherited(host):
    plan = plan_setup(_setup(2, 2), _facts(host))
    R = RUN[host]
    by_path = {r.path: r for r in plan.rows}
    for n in range(2):
        for sub, owner in (("gw", f"_levain_gw{n}"), ("qb", "_levain_qbroker"), ("planner", "_levain_dispatch")):
            row = by_path[R / sub / f"slot{n}"]
            assert (row.owner, row.group, row.mode) == (owner, f"_levain_s{n}", 0o2750)
            p = str(row.path)
            idx = {k: i for i, s in enumerate(plan.steps) for k in ("chown", "chmod")
                   if s.argv and s.argv[-1] == p and s.argv[0].endswith(k)}
            assert idx["chown"] < idx["chmod"]          # mode (setgid) set after the chown
            assert plan.steps[idx["chmod"]].argv[1] == "2750"
    # verifier class has no socket dirs
    assert not [p for p in by_path if p.parent in (R / "gw", R / "qb", R / "planner") and p.name not in ("slot0", "slot1")]


#: CONTRACT §2.1, every DIRECTORY row not per-session, typed out (§20: install makes each one whoever
#: "Written by" names). "R" = root : the host's gid-0 group; "OP" = the operator.
STORE_DIRS = {
    "": ("R", "R", 0o755), "live": ("R", "R", 0o711), "alloc": ("R", "R", 0o700),
    "sessions": ("_levain_control", "_levain_control", 0o700),
    "sessions/front-reg": ("_levain_control", "_levain_control", 0o700),
    "slots": ("R", "R", 0o755), "retained": ("R", "R", 0o700), "trash": ("R", "R", 0o700),
    "staging": ("R", "R", 0o711), "export": ("R", "R", 0o755),
    "export/operator": ("R", "levain-export", 0o750),
    "export/dispatch": ("R", "levain-dispatch-export", 0o750),
    "queue/requests": ("_levain_qbroker", "levain-queue", 0o2750),
    "queue/answers": ("OP", "levain-queue", 0o2770),
    "contracts": ("_levain_dispatch", "levain-queue", 0o2750),
    "policy": ("R", "R", 0o755),
    # 3c-6 H6-4: setgid, so the sink CONTROL creates takes levain-lineage (CONTRACT §2.1 row)
    "lineage": ("_levain_control", "levain-lineage", 0o2750),
    "keys": ("_levain_keys", "_levain_keys", 0o700), "routes": ("R", "R", 0o755),
    "routes/dispatch": ("_levain_control", "_levain_keys", 0o2750),
    "routes/verifiers": ("_levain_control", "_levain_control", 0o700),
    "slots/e0": ("R", "R", 0o755), "slots/v0": ("R", "R", 0o755), "slots/e0/keep": ("R", "R", 0o700),
}


@pytest.mark.parametrize("host", HOSTS)
def test_store_layout(host):
    plan = plan_setup(_setup(1, 1), _facts(host))
    L = STORE[host]
    root_grp = "wheel" if host == "darwin" else "root"
    rows = {r.path: (r.owner, r.group, r.mode, r.kind) for r in plan.rows}
    sub = {"R": "root", "OP": OP}
    for rel, (owner, group, mode) in STORE_DIRS.items():
        want = (sub.get(owner, owner), root_grp if group == "R" else sub.get(group, group), mode, "dir")
        assert rows[L / rel if rel else L] == want, rel
        p = str(L / rel if rel else L)
        mk = [i for i, s in enumerate(plan.steps) if s.argv and s.argv[-1] == p and s.argv[0].endswith("mkdir")]
        chmod = [i for i, s in enumerate(plan.steps) if s.argv and s.argv[-1] == p and s.argv[0].endswith("chmod")
                 and s.argv[1] != "-N"]                     # -N: the macOS ACL clear (tests/test_slot_layout_acl.py)
        assert mk and chmod and mk[0] < chmod[0] and plan.steps[chmod[0]].argv[1] == f"{mode:04o}", rel
    # a verifier slot has no keep/ (§2.1: L/slots/e<n>/keep only)
    assert L / "slots/v0/keep" not in rows
    # the parent of a child row exists before the child
    order = [s.argv[-1] for s in plan.steps if s.argv and s.argv[0].endswith("mkdir")]
    for rel in STORE_DIRS:
        if "/" in rel:
            assert order.index(str(L / rel.rsplit("/", 1)[0])) < order.index(str(L / rel)), rel
    assert rows[L / "reserve"] == ("root", root_grp, 0o600, "file")
    assert rows[RUN[host] / "control"] == ("_levain_control", "levain-control", 0o2750, "dir")
    assert L == (Path("/private/var/levain") if host == "darwin" else Path("/var/levain"))
    # L/routes/base.json: §4.3's base map, no routes, canonical, root 0644, never over an existing map
    base = [s for s in plan.steps if s.write and s.write[0] == L / "routes" / "base.json"]
    assert len(base) == 1 and base[0].write[1:] == ('{"format":"levain-routes-base/2","routes":[]}', 0o644)
    assert base[0].skip_if[-2:] == ("-e", str(L / "routes" / "base.json"))
    # every file the plan writes is under the store or a fixed system path
    fixed = ("/etc/sudoers.d/", "/usr/local/libexec/levain/", "/usr/libexec/levain/", "/Library/LaunchDaemons/",
             "/etc/systemd/system/", "/etc/pf.anchors/", "/etc/apparmor.d/", "/etc/tmpfiles.d/")
    for s in plan.steps:
        if s.write:
            assert str(s.write[0]).startswith((str(L) + "/", *fixed)), s.write[0]


@pytest.mark.parametrize("host", HOSTS)
def test_keys_dir_traversable_by_both_peers(host):
    """§2.2 (c7.2): R/keys/ is _levain_keys:_levain_keys 0711. Its two sockets' peers (every
    _levain_gw<n> via levain-gwfronts; _levain_control) are both OTHER to the dir: they traverse, none lists."""
    plan = plan_setup(_setup(2, 1), _facts(host))
    row = next(r for r in plan.rows if r.path == RUN[host] / "keys")
    assert (row.owner, row.group, row.mode) == ("_levain_keys", "_levain_keys", 0o711)
    groups = {a.name: set(a.groups) | {a.name} for a in plan.accounts}
    for peer in ("_levain_gw0", "_levain_gw1", "_levain_control"):
        assert peer != row.owner and row.group not in groups[peer]
        assert row.mode & 0o001 and not row.mode & 0o004      # other: x, not r


# --- §5.1 / §5.2 sudoers --------------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_sudoers_text(host):
    for p in (RUNTIME_SUDOERS, INSTALL_SUDOERS):
        assert "." not in p.name and not p.name.endswith("~") and p.parent == Path("/etc/sudoers.d")
    h = "/usr/local/libexec/levain" if host == "darwin" else "/usr/libexec/levain"
    rt = runtime_sudoers_text(host, OP)
    assert f"Defaults!{h}/runtime-helper use_pty\n" in rt
    assert f"\n{OP} ALL=(root) NOPASSWD: {h}/runtime-helper\n" in rt
    disp = next(line for line in rt.splitlines() if line.startswith("_levain_dispatch "))
    assert "NOPASSWD:" in disp
    # adopt-s2fix (L3 s2 r1 codex HIGH): both sets derived from the grammar, never a hand list, so a verb the
    # grammar admits for _levain_dispatch is a verb sudo admits (built red: adopt-verify was missing here)
    from levain.slot import runtime_grammar as RG
    from levain.slot import runtime_helper as RH
    assert set(RH.VERB_HANDLERS) <= set(RG.VERBS) and RG.DISPATCH_FORBIDDEN <= set(RG.VERBS)
    admitted = [v for v in RG.VERBS if v not in RG.DISPATCH_FORBIDDEN]
    specs = disp.split("NOPASSWD: ", 1)[1].split(", ")
    for verb in RG.DISPATCH_FORBIDDEN:
        assert not any(s.split()[1] == verb for s in specs), verb
    # sudo's `<verb> *` needs at least one argument (measured in the 1c VM: bare `status` was denied by
    # `status *`), so bare `status` has a spec of its own
    assert sorted(specs[:-1]) == sorted(f"{h}/runtime-helper {v} *" for v in admitted)
    assert specs[-1] == f"{h}/runtime-helper status"
    assert disp.rstrip().endswith(f", {h}/runtime-helper status")
    it = install_sudoers_text(host, OP)
    assert "NOPASSWD" not in it
    for bad in ("ALL", "a b", "x,ALL", "#0", "root", "Defaults", "defaults", "DEFAULTS", "User_Alias",
                "cmnd_alias", "Host_ALIAS", "runas_alias", "x_alias"):
        assert _refusal(runtime_sudoers_text, host, bad) == "bad-operator"
        assert _refusal(install_sudoers_text, host, bad) == "bad-operator"
    assert _refusal(parse_install_argv, ["setup", "--slots", "1", "--verifier-slots", "1", "--operator",
                                         "Defaults"]) == "bad-operator"
    for ok in ("alice", "default", "alias", "bob_aliases"):
        assert install_sudoers_text(host, ok)                 # positive control: the reserved words only
    plan = plan_setup(_setup(), _facts(host))
    last = plan.steps[-5:-3]          # row 33: the install record is written between them and the operator record
    assert plan.steps[-3].why.startswith("write the install record")
    assert [s.write[0] for s in last] == [RUNTIME_SUDOERS, INSTALL_SUDOERS]
    assert all(s.write[2] == 0o440 and s.validate[-1] == "-cf" for s in last)


@pytest.mark.parametrize("host", HOSTS)
def test_install_rule_tagged_passwd(host):
    """§23 / §5.2: the install rule carries the PASSWD tag and its Defaults pin authenticate +
    timestamp_timeout=0. A LATER root-written NOPASSWD rule for the path would still win (sudo applies
    the last match), so the text claims only what it holds."""
    h = "/usr/local/libexec/levain" if host == "darwin" else "/usr/libexec/levain"
    lines = install_sudoers_text(host, OP).splitlines()
    rules = [ln for ln in lines if not ln.startswith("#")]
    assert rules == [f"Defaults!{h}/install-helper authenticate, timestamp_timeout=0",
                     f"{OP} ALL=(root) PASSWD: {h}/install-helper"]


def test_setup_refuses_existing_singleton_or_shared_group():
    for name in ("_levain_keys", "levain-queue"):
        assert _refusal(plan_setup, _setup(), _facts("linux", existing_names=frozenset({name}))) == "account-exists"


def test_undo_refuses_a_foreign_account():
    assert _refusal(plan_undo, parse_install_argv(["undo"]), _set_up("linux"), accounts={"bob": 501},
                    shared_groups={}) == "not-levain-name"


def test_plans_are_steps_only():
    plan = plan_setup(_setup(), _facts("linux"))
    assert all(isinstance(s, Step) for s in plan.steps)
    assert not any(s.argv and s.argv[0].endswith("sudo") for s in plan.steps)


def test_base_map_formats_are_the_policy_readers_formats():
    # 3c-2a (R4 §1.3): layout writes base /2, and every reader accepts exactly what keys' parser accepts
    from levain.broker import policy
    from levain.slot import layout
    assert layout.BASE_ROUTES_FORMATS == policy.BASE_MAP_FORMATS
    assert layout.BASE_ROUTES_FORMAT == policy.BASE_MAP_FORMATS[-1] == "levain-routes-base/2"
