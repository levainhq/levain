"""levain#223 (ruling levain#204, ``project_memory/1118-vm/j06_adopt_linux_ruling.md``): Linux provisions the verifier's
account ``_levain_verify`` and its group ``levain-verify``; macOS provisions neither (the early ``account-missing``
gate of ``tests/test_slot_l134_verifier_account.py`` stays).

Plan-layer rows, with the plan layer's own fakes: ``tests.test_slot_install_plan`` (setup), ``tests.test_repair_absent_account``
(a repair over a recorded install) and ``tests.test_repair_remove.FakeHost`` (undo's live reads and acts). The kill rows
replay the steps a plan places over a small model of the account database, stop part-way, re-plan from what the model
then holds, and finish.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import generation as G
from levain.slot import install_helper as IH
from levain.slot import removal as RM
from levain.slot import units
from levain.slot import wall
from levain.slot.accounts import (SLOT_MARKER, SLOT_RETIRED_MARKER, Account, membership_pairs,
                                  supplementary_groups)
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import (KNOWN_SHARED_GROUPS, KNOWN_SINGLETONS, RANGES, SLOT_NAME_RE, VERIFIER_ACCOUNT,
                             VERIFIER_GROUP, SlotId, allocate, in_own_range, is_levain_account,
                             provisioned_shared_groups, provisioned_singletons)
from levain.slot.intent import Intent, read_intent

from tests.test_repair_absent_account import OP, OP_UID, TXN_A, _package, _plan as _repair_plan
from tests.test_repair_remove import Killed, _bwrap, _host as _fake_host, _put
from tests.test_slot_install_plan import _facts, _set_up, _setup, _refusal

LINUX_UID = RANGES["linux"].singletons[5]      # the sixth singleton id of the range: the verifier's, on a fresh install
LINUX_GID = RANGES["linux"].shared[9]          # the tenth shared-group id: levain-verify's
TXN_B = "b" * 32


@pytest.fixture(autouse=True)
def _linux_tools(monkeypatch) -> None:
    from levain.firing import hands

    real = hands.shutil.which                   # a Linux plan built here names tools this host lacks
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


# --- the split of SINGLETONS --------------------------------------------------------------------------------------


def test_known_and_provisioned_singletons_split() -> None:
    """KNOWN is every name Levain recognises (verify LAST, so no id moves); provisioned is what a host makes."""
    assert KNOWN_SINGLETONS[-1] == VERIFIER_ACCOUNT and len(KNOWN_SINGLETONS) == 6
    assert KNOWN_SHARED_GROUPS[-1] == VERIFIER_GROUP and len(KNOWN_SHARED_GROUPS) == 10
    assert provisioned_singletons("linux") == KNOWN_SINGLETONS
    assert provisioned_singletons("darwin") == KNOWN_SINGLETONS[:-1]
    assert provisioned_shared_groups("linux") == KNOWN_SHARED_GROUPS
    assert provisioned_shared_groups("darwin") == KNOWN_SHARED_GROUPS[:-1]
    # recognition rests on KNOWN_SINGLETONS: the slot-name pattern does not match the account
    assert SLOT_NAME_RE.fullmatch(VERIFIER_ACCOUNT) is None
    assert is_levain_account(VERIFIER_ACCOUNT)
    for host in ("linux", "darwin"):                               # recognised and in range on both hosts
        assert in_own_range(host, VERIFIER_ACCOUNT, RANGES[host].singletons[5])
        assert not in_own_range(host, VERIFIER_ACCOUNT, RANGES[host].slots[0])
    assert supplementary_groups(VERIFIER_ACCOUNT) == ()           # the wall adds levain-verify at launch only
    with pytest.raises(Refused) as e:                              # a session principal is never a singleton
        wall.principal("linux", VERIFIER_ACCOUNT, {VERIFIER_ACCOUNT: LINUX_UID}, {"levain-slots": LINUX_GID + 1})
    assert e.value.name == "bad-token"


def test_the_allocator_hands_the_verifier_the_next_singleton_uid() -> None:
    """A repair's one creation: only the named singleton is allocated, past every id in use and every retired id."""
    first = allocate("linux", egress=(), verifier=(), used=frozenset(), retired=frozenset(), singletons=True)
    assert first.ids[VERIFIER_ACCOUNT] == LINUX_UID == RANGES["linux"].singletons[5]
    held = frozenset(RANGES["linux"].singletons[:5])               # an install made before: the five singletons
    one = allocate("linux", egress=(), verifier=(), used=held, retired=frozenset(),
                   only_singletons=(VERIFIER_ACCOUNT,))
    assert dict(one.ids) == {VERIFIER_ACCOUNT: LINUX_UID}
    past = allocate("linux", egress=(), verifier=(), used=held | {LINUX_UID}, retired=frozenset({LINUX_UID - 1}),
                    only_singletons=(VERIFIER_ACCOUNT,))
    assert dict(past.ids) == {VERIFIER_ACCOUNT: LINUX_UID - 2}


# --- setup ----------------------------------------------------------------------------------------------------------


def test_linux_setup_provisions_the_verifier_account_and_group_last() -> None:
    plan = IH.plan_setup(_setup(3, 2), _facts("linux"), read_sysctl=lambda p: "1", program_exists=lambda p: True)
    r = RANGES["linux"]
    ids = dict(plan.allocation.ids)
    # no fresh-install uid or gid moves: the five older singletons and the nine older groups keep their ids
    assert [ids[n] for n in KNOWN_SINGLETONS] == list(r.singletons)[:6]
    assert ids[VERIFIER_ACCOUNT] == LINUX_UID
    assert [plan.allocation.shared[g] for g in KNOWN_SHARED_GROUPS] == list(r.shared)
    assert plan.allocation.shared[VERIFIER_GROUP] == LINUX_GID
    argvs = [s.argv for s in plan.steps if s.argv]
    group = [a for a in argvs if a[0].endswith("groupadd") and a[-1] == VERIFIER_ACCOUNT]
    user = [a for a in argvs if a[0].endswith("useradd") and a[-1] == VERIFIER_ACCOUNT]
    shared = [a for a in argvs if a[0].endswith("groupadd") and a[-1] == VERIFIER_GROUP]
    assert len(group) == len(user) == len(shared) == 1
    assert group[0][group[0].index("--gid") + 1] == str(LINUX_UID)           # its own primary group, gid == uid
    assert user[0][user[0].index("--uid") + 1] == str(LINUX_UID) and user[0][user[0].index("--gid") + 1] == str(LINUX_UID)
    assert shared[0][shared[0].index("--gid") + 1] == str(LINUX_GID)
    # no persistent membership: no step puts the account in a group, and the shared group has no member
    assert not [a for a in argvs if a[0].endswith("usermod") and "--append" in a and a[-1] == VERIFIER_ACCOUNT]
    assert {g.name: g.members for g in plan.groups}[VERIFIER_GROUP] == ()
    assert plan.intent.accounts[VERIFIER_ACCOUNT] == LINUX_UID and plan.intent.groups[VERIFIER_GROUP] == LINUX_GID


def test_macos_provisions_no_verifier_account() -> None:
    """macOS is unchanged: the five singletons, the nine groups, no step names the verifier, the early gate stays."""
    plan = IH.plan_setup(_setup(3, 2), _facts("darwin"), program_exists=lambda p: True)
    assert tuple(n for n in plan.allocation.ids if n in KNOWN_SINGLETONS) == provisioned_singletons("darwin")
    assert VERIFIER_ACCOUNT not in plan.allocation.ids and VERIFIER_ACCOUNT not in plan.intent.accounts
    assert VERIFIER_GROUP not in plan.allocation.shared and VERIFIER_GROUP not in plan.intent.groups
    assert not [s for s in plan.steps if VERIFIER_ACCOUNT in s.why or VERIFIER_GROUP in s.why
                or VERIFIER_ACCOUNT in " ".join(s.argv) or VERIFIER_GROUP in " ".join(s.argv)]
    assert [(k, i) for k, i in plan.intent.managed if VERIFIER_ACCOUNT in repr(i)] == []


def _old_setup_journal(host: str) -> Intent:
    """The journal a setup of the code BEFORE levain#223 wrote: five singletons, nine shared groups."""
    plan = IH.plan_setup(_setup(3, 2), _facts(host), read_sysctl=lambda p: "1", program_exists=lambda p: True)
    j = plan.intent
    return replace(j, accounts={n: i for n, i in j.accounts.items() if n != VERIFIER_ACCOUNT},
                   groups={g: i for g, i in j.groups.items() if g != VERIFIER_GROUP})


def _resume(host: str, j: Intent):
    made = {**j.accounts, **j.groups}
    return IH.plan_setup(_setup(3, 2), _facts(host, existing_names=frozenset(made), used_ids=frozenset(made.values()),
                                              intent=j, host_ids=made),
                         read_sysctl=lambda p: "1", program_exists=lambda p: True)


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_an_old_setup_journal_resumes(host) -> None:
    """The refusal ``if not set(SINGLETONS) <= set(journal.accounts) or set(journal.groups) != set(SHARED_GROUPS)``
    (``intent-malformed``): BOTH halves accept the shape an older setup journaled (five singletons, nine groups)."""
    j = _old_setup_journal(host)
    assert len(j.accounts) == len(provisioned_singletons(host)) - (host == "linux") + 3 * 2 + 2 * 2   # slots + pairs
    assert VERIFIER_ACCOUNT not in j.accounts and VERIFIER_GROUP not in j.groups
    again = _resume(host, j)
    assert {n: again.allocation.ids[n] for n in j.accounts} == dict(j.accounts)         # kept at the journal's ids
    assert {g: again.allocation.shared[g] for g in j.groups} == dict(j.groups)
    if host == "linux":
        # the pair the journal never named is made fresh, past every id the journal holds
        assert again.allocation.ids[VERIFIER_ACCOUNT] == LINUX_UID
        assert again.allocation.shared[VERIFIER_GROUP] == LINUX_GID
        assert again.intent.accounts[VERIFIER_ACCOUNT] == LINUX_UID
    else:
        assert VERIFIER_ACCOUNT not in again.allocation.ids and VERIFIER_GROUP not in again.allocation.shared
    # each half alone: an old accounts half under a new groups half, and the reverse
    if host == "linux":
        new = IH.plan_setup(_setup(3, 2), _facts(host), read_sysctl=lambda p: "1", program_exists=lambda p: True).intent
        _resume(host, replace(j, groups=new.groups))
        _resume(host, replace(new, accounts=j.accounts))


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_a_setup_journal_missing_any_other_singleton_or_group_is_still_malformed(host) -> None:
    j = _old_setup_journal(host)
    for name in ("_levain_keys", "_levain_dispatch"):
        err = _refusal(_resume, host, replace(j, accounts={n: i for n, i in j.accounts.items() if n != name}))
        assert err == "intent-malformed"
    for g in ("levain-slots", "levain-keysadmin"):
        err = _refusal(_resume, host, replace(j, groups={n: i for n, i in j.groups.items() if n != g}))
        assert err == "intent-malformed"


def test_the_journal_whole_slots_check_recognises_the_verifier() -> None:
    """``odd = (set(intent.accounts) ^ names) - set(KNOWN_SINGLETONS)``: neither recognition of a slot nor what setup
    makes, but which singleton names a journal may carry beside its whole slots: every KNOWN one."""
    r = RANGES["linux"]
    slots = {n: u for n, u in zip(SlotId("egress", 0).accounts, r.slots)}
    got = IH._journal_slots(Intent("setup", OP, {**slots, VERIFIER_ACCOUNT: LINUX_UID, "_levain_keys": r.singletons[0]},
                                   {}, TXN_B))
    assert [s.tag for s in got["egress"]] == ["e0"]
    with pytest.raises(Refused) as e:                              # a name that is no singleton and no whole slot
        IH._journal_slots(Intent("setup", OP, {**slots, "_levain_gw5": r.slots[9]}, {}, TXN_B))
    assert e.value.name == "intent-malformed"


# --- undo -----------------------------------------------------------------------------------------------------------


def _undo_plan(tmp_path: Path, monkeypatch, *, kill_at: str | None):
    """Undo over a Linux host holding only the verifier's account, its own group and the shared ``levain-verify``."""
    from levain.slot.layout import STORE
    from tests.test_undo_remove import _facts as undo_facts, _no_real_store, _shape

    _no_real_store(monkeypatch)
    h, live = _fake_host(tmp_path, "linux")
    h.users[VERIFIER_ACCOUNT] = [LINUX_UID, SLOT_MARKER]
    h.groups[VERIFIER_ACCOUNT] = [LINUX_UID, "", set()]
    h.groups[VERIFIER_GROUP] = [LINUX_GID, "", set()]
    _bwrap(live)
    _put(live, STORE["linux"] / "slots" / "pool.lock", b"")

    def fault(point: str) -> None:
        if point == kill_at:
            raise Killed(point)
    live = RM.Live(run=h.run, root=live.root, fsync=h.fsync, fault=fault)
    shape = _shape(("account/1", (VERIFIER_ACCOUNT, LINUX_UID)), ("group/1", (VERIFIER_GROUP, LINUX_GID)))
    plan = IH.plan_undo(parse_install_argv(["undo"]), undo_facts("linux", shape), accounts={VERIFIER_ACCOUNT: LINUX_UID},
                        shared_groups={VERIFIER_GROUP: LINUX_GID}, live=live, end=lambda u, n: (True, ""))
    return h, live, plan


def _walk(plan) -> None:
    for s in plan.steps:
        if s.call is not None and s.write is None:
            ok, why = s.call()
            assert ok, (s.why, why)


def test_undo_removes_the_verifier_and_a_kill_part_way_is_finished_by_the_next_undo(tmp_path: Path, monkeypatch) -> None:
    """Undo tombstones the account (its uid retired, never reissued, label LAST) and deletes ``levain-verify``; its
    own primary group stays with the tombstone, as every account's does. A run killed after the first tombstone act
    leaves the account still live-labelled, so the next undo plans it again from the start and finishes."""
    h, live, plan = _undo_plan(tmp_path / "a", monkeypatch, kill_at="account/1:after-first-act")
    assert LINUX_UID in plan.retired_after
    with pytest.raises(Killed):
        _walk(plan)
    assert h.users[VERIFIER_ACCOUNT][1] == SLOT_MARKER, "killed before the retired label: still ours, still planned"
    assert VERIFIER_GROUP in h.groups                              # the shared group is deleted after the accounts
    # the next undo, from what the host holds, on the same fake host
    from tests.test_undo_remove import _facts as undo_facts, _shape

    live2 = RM.Live(run=h.run, root=live.root, fsync=h.fsync)
    shape = _shape(("account/1", (VERIFIER_ACCOUNT, LINUX_UID)), ("group/1", (VERIFIER_GROUP, LINUX_GID)))
    again = IH.plan_undo(parse_install_argv(["undo"]), undo_facts("linux", shape), accounts={VERIFIER_ACCOUNT: LINUX_UID},
                         shared_groups={VERIFIER_GROUP: LINUX_GID}, live=live2, end=lambda u, n: (True, ""))
    _walk(again)
    assert h.users[VERIFIER_ACCOUNT][1] == SLOT_RETIRED_MARKER["linux"]
    assert VERIFIER_GROUP not in h.groups and VERIFIER_ACCOUNT in h.groups
    assert ("/usr/sbin/chage", "--expiredate", "0", VERIFIER_ACCOUNT) in [(a[0].replace("/usr/bin/", "/usr/sbin/"), *a[1:])
                                                                          for a in h.log]
    assert LINUX_UID in again.retired_after


# --- repair ---------------------------------------------------------------------------------------------------------


def _state(tmp: Path, host: str, users: dict, groups: dict, *, journal: Intent | None = None,
           retired: frozenset = frozenset(), egress=(0, 1), verifier=(0,)):
    """(HostFacts, RepairFacts) for a host holding ``users`` (name -> (uid, label)) and ``groups`` (name -> gid), as the
    gather reads them; the install record names what the code BEFORE levain#223 set up (no verifier, nine groups)."""
    r = RANGES[host]
    slots = [SlotId("egress", i) for i in egress] + [SlotId("none", i) for i in verifier]
    old_ids = {n: u for n, (u, _) in users.items() if n in KNOWN_SINGLETONS and n != VERIFIER_ACCOUNT}
    old_ids.update((n, u) for n, u in zip([n for s in slots for n in s.accounts], r.slots))
    old_groups = {g: i for g, i in groups.items() if g in KNOWN_SHARED_GROUPS and g != VERIFIER_GROUP}
    managed = IH.desired_managed(host, accounts=old_ids, shared=old_groups, operator=OP,
                                 services=[s.name for s in units.services_for(host)], slots=slots)
    shape = G.InstallShape("gen-" + "0" * 16, "0.9.0", "0.16.0", TXN_A, {}, tuple(managed))
    live = {n: u for n, (u, label) in users.items() if label == SLOT_MARKER}
    pairs = membership_pairs([Account(n, u, supplementary_groups(n)) for n, u in live.items()], OP)
    members = {g: {a for a, gg in pairs if gg == g} for g in groups if g.startswith("levain-")}
    host_ids: dict = {}
    group_only: dict = {}
    if journal is not None:
        host_ids.update((n, users[n][0]) for n in journal.accounts if n in users)
        host_ids.update((g, groups[g]) for g in journal.groups if g in groups)
        group_only = {n: groups[n] for n in journal.accounts if n not in users and n in groups}
    user_ids = {**{n: u for n, (u, _) in users.items()}, OP: OP_UID, "root": 0}
    facts = IH.HostFacts(host=host, caller_uid=OP_UID, caller_name=OP,
                         operator_record={"format": "levain-operator/1", "name": OP, "uid": OP_UID},
                         used_ids=frozenset({*user_ids.values(), *groups.values()}),
                         existing_names=frozenset({*user_ids, *groups}), retired=retired,
                         levain_live=dict(live), install_shape=shape, intent=journal, host_ids=host_ids,
                         account_group_ids=group_only, user_ids=user_ids,
                         tool_present=lambda p: True, xcode_ready=lambda: True)
    lv, h11 = _package(tmp)
    rf = IH.RepairFacts(accounts=live, groups={g: i for g, i in groups.items() if g.startswith("levain-")},
                        members={g: frozenset(m) for g, m in members.items()}, egress=tuple(egress),
                        verifier=tuple(verifier), current=shape.generation, source=lv, h11=h11,
                        account_groups={n: groups[n] for n in live if n in groups})
    return facts, rf


def _before(host: str = "linux"):
    """The directory of an install made BEFORE levain#223: five singletons, nine groups, two egress slots, one pair."""
    r = RANGES[host]
    users = {n: (u, SLOT_MARKER) for n, u in zip(provisioned_singletons(host), r.singletons)
             if n != VERIFIER_ACCOUNT}
    users.update((n, (u, SLOT_MARKER)) for n, u in zip([n for s in (SlotId("egress", 0), SlotId("egress", 1),
                                                                    SlotId("none", 0)) for n in s.accounts], r.slots))
    groups = {g: i for g, i in zip(provisioned_shared_groups(host), r.shared) if g != VERIFIER_GROUP}
    groups.update({n: u for n, (u, _) in users.items()})
    return users, groups


def _repair(facts, rf):
    return _repair_plan(facts, rf)


def _verifier_steps(plan) -> list:
    """The steps a repair plan places for the verifier, in order: the write-ahead journal, then every act naming it."""
    keep = [s for s in plan.steps
            if s.why.startswith("journal the shared-group gids") or VERIFIER_ACCOUNT in s.argv or VERIFIER_GROUP in s.argv]
    assert keep and keep[0].why.startswith("journal the shared-group gids"), [s.why for s in keep]
    return keep


@pytest.fixture
def intent_path(tmp_path: Path, monkeypatch) -> Path:
    """The journal's path moved under tmp (a plan reads it when it BUILDS its journal steps)."""
    p = tmp_path / "state" / "install-intent"
    monkeypatch.setitem(IH.INTENT_PATH, "linux", p)
    return p


def test_repair_creates_the_verifier_on_an_install_made_before_it(tmp_path: Path, intent_path: Path) -> None:
    users, groups = _before()
    facts, rf = _state(tmp_path, "linux", users, groups)
    rf = replace(rf, members={**rf.members, "levain-control": frozenset()})     # a missing membership: R7 plans its steps
    plan = _repair(facts, rf)
    assert plan.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}
    assert plan.intent.groups[VERIFIER_GROUP] == LINUX_GID
    # the final managed set names the account, its group at its uid, and the shared group
    assert ("account/1", (VERIFIER_ACCOUNT, LINUX_UID)) in plan.intent.managed
    assert ("group/1", (VERIFIER_GROUP, LINUX_GID)) in plan.intent.managed
    whys = [s.why for s in plan.steps]
    journal = next(i for i, w in enumerate(whys) if w.startswith("journal the shared-group gids"))
    group = next(i for i, s in enumerate(plan.steps) if s.argv[1:2] == ("--system",) and s.argv[-1] == VERIFIER_ACCOUNT
                 and s.argv[0].endswith("groupadd"))
    user = next(i for i, s in enumerate(plan.steps) if s.argv[:1] and s.argv[0].endswith("useradd")
                and s.argv[-1] == VERIFIER_ACCOUNT)
    lock = next(i for i, s in enumerate(plan.steps) if s.argv[:1] and s.argv[0].endswith("usermod")
                and "--lock" in s.argv and s.argv[-1] == VERIFIER_ACCOUNT)
    first_member = next(i for i, s in enumerate(plan.steps) if s.why.startswith("put "))
    # write-ahead: the journal that names the account precedes its group, which precedes its user; the account exists
    # before any membership of the same run
    assert journal < group < user < lock
    assert lock < first_member
    uid = plan.steps[user].argv.index("--uid")
    assert plan.steps[user].argv[uid + 1] == str(LINUX_UID)
    assert plan.steps[group].skip_if[1:] == ("group", VERIFIER_ACCOUNT)          # present is skipped: a re-run is safe


@pytest.mark.parametrize("held,retired,want", [
    ({"someone": LINUX_UID}, (), LINUX_UID - 1),                 # a foreign user holds the uid
    ({}, (LINUX_UID,), LINUX_UID - 1),                           # the uid was retired (never reissued)
    ({"somegroup": LINUX_UID}, (LINUX_UID - 1,), LINUX_UID - 2),  # a foreign GROUP holds it; the next is retired
])
def test_repair_allocates_the_verifier_uid_past_used_and_retired_ids(tmp_path: Path, intent_path: Path,
                                                                     held, retired, want) -> None:
    users, groups = _before()
    for name, i in held.items():
        (groups if name == "somegroup" else users)[name] = i if name == "somegroup" else (i, "")
    facts, rf = _state(tmp_path, "linux", users, groups, retired=frozenset(retired))
    plan = _repair(facts, rf)
    assert plan.intent.accounts == {VERIFIER_ACCOUNT: want}


def test_repair_journals_the_account_in_the_write_ahead_record_before_the_group(tmp_path: Path, intent_path: Path) -> None:
    """Run the journal step and read the file back with the reader's own rules: it names the account at the uid and
    the group at its gid, and (the plan's order, checked above) nothing was made yet."""
    users, groups = _before()
    facts, rf = _state(tmp_path, "linux", users, groups)
    steps = _verifier_steps(_repair(facts, rf))
    assert steps[0].call()[0]
    j = read_intent("linux", path=intent_path)
    assert j.verb == "repair" and j.accounts == {VERIFIER_ACCOUNT: LINUX_UID} and j.groups[VERIFIER_GROUP] == LINUX_GID
    assert ("account/1", (VERIFIER_ACCOUNT, LINUX_UID)) in j.managed


class _Model:
    """The account database a replayed step changes: what ``groupadd`` / ``useradd`` / ``usermod --lock`` do."""

    def __init__(self, users: dict, groups: dict) -> None:
        self.users, self.groups, self.locked = dict(users), dict(groups), set()

    def apply(self, step) -> None:
        if step.call is not None:
            ok, why = step.call()
            assert ok, why
            return
        a = step.argv
        if step.skip_if:
            kind, name = step.skip_if[1:]
            if name in (self.groups if kind == "group" else self.users):
                return
        tool = os.path.basename(a[0])
        if tool == "groupadd":
            self.groups[a[-1]] = int(a[a.index("--gid") + 1])
        elif tool == "useradd":
            self.users[a[-1]] = (int(a[a.index("--uid") + 1]), a[a.index("--comment") + 1])
        elif tool == "usermod" and "--lock" in a:
            self.locked.add(a[-1])


def _read_journal(path: Path):
    return read_intent("linux", path=path) if path.exists() else None


def test_repair_resumes_at_the_journaled_uid_after_a_kill_at_every_point(tmp_path: Path, intent_path: Path) -> None:
    """Kill the verifier's steps after k of them (k = 0 .. all), re-plan from what the model holds and the journal on
    disk, and finish. Every k ends with ONE verifier at the uid the journal named: user, own group, the shared group,
    locked, the label Levain's; a kill after the journal (k >= 1) never moves the uid, even with the allocator's next
    answer changed in between (a foreign account took the uid the first run would now pick)."""
    base_users, base_groups = _before()
    first = _repair(*_state(tmp_path / "p0", "linux", base_users, base_groups))
    n_steps = len(_verifier_steps(first))
    assert n_steps == 5      # the journal, levain-verify, the account's group, its user, its lock
    for k in range(n_steps + 1):
        if intent_path.exists():
            intent_path.unlink()
        tmp = tmp_path / f"k{k}"
        m = _Model(base_users, base_groups)
        facts, rf = _state(tmp / "a", "linux", m.users, m.groups)
        plan = _repair(facts, rf)
        for s in _verifier_steps(plan)[:k]:
            m.apply(s)
        # what a rebooted host shows: the model's directory, the journal on disk, and (to try the pin) a foreign
        # account that now holds the uid a FRESH allocation would pick, but not the journaled one
        journaled = _read_journal(intent_path)
        assert (journaled is None) == (k == 0)
        if k >= 1:
            assert journaled.accounts == {VERIFIER_ACCOUNT: LINUX_UID}
        users2 = {**m.users, **({"squatter": (LINUX_UID - 1, "")} if k >= 1 else {})}
        facts, rf = _state(tmp / "b", "linux", users2, m.groups, journal=journaled)
        again = _repair(facts, rf)
        assert again.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}, k
        for s in _verifier_steps(again):
            m.apply(s)
        assert m.users[VERIFIER_ACCOUNT] == (LINUX_UID, SLOT_MARKER), k
        assert m.groups[VERIFIER_ACCOUNT] == LINUX_UID and m.groups[VERIFIER_GROUP] == LINUX_GID, k
        assert VERIFIER_ACCOUNT in m.locked, k
        assert [n for n, (u, _) in m.users.items() if u == LINUX_UID] == [VERIFIER_ACCOUNT], k


def test_repair_resumes_a_group_without_its_user(tmp_path: Path, intent_path: Path) -> None:
    """The kill between the account's primary group and its user: the journal names the account, the same-named
    group is present at the journaled id and no user is: resumed (the group step skips, the user is made)."""
    users, groups = _before()
    groups[VERIFIER_ACCOUNT] = LINUX_UID
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    groups[VERIFIER_GROUP] = LINUX_GID
    plan = _repair(*_state(tmp_path, "linux", users, groups, journal=j))
    assert plan.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}
    made = [s for s in _verifier_steps(plan)[1:]]
    group = next(s for s in made if s.argv[0].endswith("groupadd") and s.argv[-1] == VERIFIER_ACCOUNT)
    user = next(s for s in made if s.argv[0].endswith("useradd"))
    assert group.skip_if[1:] == ("group", VERIFIER_ACCOUNT) and user.skip_if[1:] == ("passwd", VERIFIER_ACCOUNT)
    assert user.argv[user.argv.index("--uid") + 1] == str(LINUX_UID)
    # the same name's group at ANOTHER gid is not the one the journal made
    groups[VERIFIER_ACCOUNT] = LINUX_UID - 1
    with pytest.raises(Refused) as e:
        _repair(*_state(tmp_path / "x", "linux", users, groups, journal=j))
    assert e.value.name == "account-exists" and VERIFIER_ACCOUNT in e.value.detail
    # with NO journal, a group of that name is not ours to claim, and a user of it that is not live-labelled neither
    for extra_users, extra_groups in (({}, {VERIFIER_ACCOUNT: LINUX_UID}), ({VERIFIER_ACCOUNT: (LINUX_UID, "")}, {})):
        u, g = _before()
        with pytest.raises(Refused) as e:
            _repair(*_state(tmp_path / "y", "linux", {**u, **extra_users}, {**g, **extra_groups}))
        assert e.value.name == "account-exists" and VERIFIER_ACCOUNT in e.value.detail


def test_repair_finishes_an_account_killed_before_its_lock(tmp_path: Path, intent_path: Path) -> None:
    """The user exists (labelled) and the journal names it, but ``usermod --lock`` never ran: the account is journaled,
    so the plan makes it again (every act skips what is there or is idempotent) and the lock step is planned."""
    users, groups = _before()
    users[VERIFIER_ACCOUNT] = (LINUX_UID, SLOT_MARKER)
    groups[VERIFIER_ACCOUNT] = LINUX_UID
    groups[VERIFIER_GROUP] = LINUX_GID
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    plan = _repair(*_state(tmp_path, "linux", users, groups, journal=j))
    assert any(s.argv[:1] and s.argv[0].endswith("usermod") and "--lock" in s.argv and s.argv[-1] == VERIFIER_ACCOUNT
               for s in plan.steps)
    assert plan.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}


def test_repair_on_a_linux_install_with_the_verifier_creates_nothing(tmp_path: Path, intent_path: Path) -> None:
    """An install that already has the account (set up by this code, or made by hand with the same ids) is repaired
    as before: no journaled account, no step naming it."""
    users, groups = _before()
    users[VERIFIER_ACCOUNT] = (LINUX_UID, SLOT_MARKER)
    groups[VERIFIER_ACCOUNT] = LINUX_UID
    groups[VERIFIER_GROUP] = LINUX_GID
    facts, rf = _state(tmp_path, "linux", users, groups)
    facts = replace(facts, install_shape=replace(
        facts.install_shape, managed=tuple(facts.install_shape.managed) + (
            ("account/1", (VERIFIER_ACCOUNT, LINUX_UID)), ("group/1", (VERIFIER_GROUP, LINUX_GID)))))
    plan = _repair(facts, rf)
    assert plan.intent.accounts == {}
    assert not [s for s in plan.steps if VERIFIER_ACCOUNT in s.argv]
    assert ("account/1", (VERIFIER_ACCOUNT, LINUX_UID)) in plan.intent.managed


def test_repair_makes_no_verifier_account_on_macos(tmp_path: Path) -> None:
    users, groups = _before("darwin")
    facts, rf = _state(tmp_path, "darwin", users, groups)
    plan = _repair(facts, rf)
    assert plan.intent.accounts == {} and VERIFIER_GROUP not in plan.intent.groups
    assert not [s for s in plan.steps if VERIFIER_ACCOUNT in s.why or VERIFIER_GROUP in s.why]


@pytest.mark.parametrize("gone", ["_levain_keys", "_levain_export", "_levain_dispatch"])
def test_repair_still_refuses_any_other_missing_singleton(tmp_path: Path, intent_path: Path, gone) -> None:
    """The exception is the verifier's alone: another singleton missing is still ``account-missing`` (repair never
    creates it), with the verifier's own creation unable to excuse it."""
    users, groups = _before()
    del users[gone], groups[gone]
    with pytest.raises(Refused) as e:
        _repair(*_state(tmp_path, "linux", users, groups))
    assert e.value.name == "account-missing" and gone in e.value.detail


def test_repair_phase_a_and_resume_do_not_refuse_the_missing_verifier(tmp_path: Path, intent_path: Path) -> None:
    """``repair --source`` runs phase A (stage) and a bare repair after a pre-flip kill runs the resume plan, each with
    its own ``_check_rendered_accounts``: neither refuses the account the run is about to make; each carries the
    journal's accounts forward when it rewrites the journal."""
    users, groups = _before()
    facts, rf = _state(tmp_path, "linux", users, groups)
    stage = IH.plan_repair_stage(parse_install_argv(["repair", "--source", "/opt/x/levain"]), facts, rf)
    assert stage.intent.accounts == {}
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    facts, rf = _state(tmp_path / "j", "linux", users, groups, journal=j)
    assert IH.plan_repair_stage(parse_install_argv(["repair", "--source", "/opt/x/levain"]), facts, rf
                                ).intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}
    jg = replace(j, generation="gen-" + "1" * 16)
    facts, rf = _state(tmp_path / "g", "linux", users, groups, journal=jg)
    tree = tmp_path / "tree"
    (tree / jg.generation).mkdir(parents=True)
    resume = IH.plan_repair_resume(parse_install_argv(["repair"]), facts, rf, tree=tree)   # not account-missing
    assert resume.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}
