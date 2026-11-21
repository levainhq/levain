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
from levain.slot.grammar import INDEX_SPACE
from levain.slot.ids import (KNOWN_SHARED_GROUPS, KNOWN_SINGLETONS, ORIGINAL_SHARED_GROUPS, ORIGINAL_SINGLETONS,
                             RANGES, SLOT_NAME_RE, VERIFIER_ACCOUNT, VERIFIER_GROUP, SlotId, allocate, in_own_range,
                             is_levain_account, provisioned_shared_groups, provisioned_singletons)
from levain.slot.intent import Intent, read_intent

from tests.test_repair_absent_account import OP, OP_UID, TXN_A, _names, _package, _plan as _repair_plan
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


#: FROZEN copy of the singletons the code before levain#223 shipped (``git show cd884ce00:levain/slot/ids.py``:
#: ``SINGLETONS = ("_levain_keys", "_levain_export", "_levain_control", "_levain_qbroker", "_levain_dispatch")``).
#: Never derive it from the package: it is what installed, older code recognises, whatever this tree says.
PRE_223_SINGLETONS = ("_levain_keys", "_levain_export", "_levain_control", "_levain_qbroker", "_levain_dispatch")
#: FROZEN copy of cd884ce00's ``SHARED_GROUPS`` (same file), on the same rule.
PRE_223_SHARED_GROUPS = ("levain-slots", "levain-gwfronts", "levain-control", "levain-wake", "levain-lineage",
                         "levain-export", "levain-dispatch-export", "levain-queue", "levain-keysadmin")


def test_the_original_sets_and_what_repair_may_make_derive_from_one_place() -> None:
    """``ORIGINAL_*`` is the shipped pre-#223 set; the old-journal exemption and ``_REPAIR_MAKES`` are KNOWN minus it,
    so a singleton added later is both made by repair and optional in an older setup journal with no third list. A
    singleton repair makes is made AFTER R7's memberships (``plan_repair``, levain#223 fix 1): it must be in no group."""
    assert ORIGINAL_SINGLETONS == PRE_223_SINGLETONS
    assert ORIGINAL_SHARED_GROUPS == PRE_223_SHARED_GROUPS
    assert IH._REPAIR_MAKES == frozenset({VERIFIER_ACCOUNT})
    assert IH._REPAIR_MAKES_GROUPS == frozenset({VERIFIER_GROUP})
    for name in IH._REPAIR_MAKES:
        assert supplementary_groups(name) == (), name
        assert [p for p in membership_pairs([Account(name, LINUX_UID, supplementary_groups(name))], OP)
                if p[0] == name] == []
    # a shared group repair makes is made in R10 too (L3 r1): no account, the operator or a row may name it before
    from levain.slot.layout import base_run_rows, base_store_rows, slot_rows
    accts = [Account(n, LINUX_UID, supplementary_groups(n)) for n in (*KNOWN_SINGLETONS, *SlotId("egress", 0).accounts,
                                                                       *SlotId("none", 0).accounts)]
    rows = (base_store_rows("linux", OP) + base_run_rows("linux", OP)
            + slot_rows("linux", (SlotId("egress", 0), SlotId("none", 0))))
    for g in IH._REPAIR_MAKES_GROUPS:
        assert [p for p in membership_pairs(accts, OP) if p[1] == g] == [], g
        assert [r for r in rows if r.group == g] == [], g


def test_the_wall_names_the_verifier_as_ids_does() -> None:
    """The adopt gate and the launch read the wall's names; setup and repair provision the ids ones. ``wall`` imports
    ``levain.slot.ids`` only inside functions, so its two tables keep literals: pinned equal here."""
    assert wall.EXPORT_ACCOUNT["verifier"] == VERIFIER_ACCOUNT
    assert wall.EXPORT_GROUPS[("verifier", None)] == VERIFIER_GROUP


def test_allocate_refuses_singletons_with_only_singletons() -> None:
    with pytest.raises(Refused) as e:
        allocate("linux", egress=(), verifier=(), used=frozenset(), retired=frozenset(), singletons=True,
                 only_singletons=(VERIFIER_ACCOUNT,))
    assert e.value.name == "internal"


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
    slot_names = {n for s in [*(SlotId("egress", i) for i in range(3)), *(SlotId("none", i) for i in range(2))]
                  for n in s.accounts}
    assert set(j.accounts) == set(ORIGINAL_SINGLETONS) | slot_names       # the shape the older setup journaled
    assert set(j.groups) == set(ORIGINAL_SHARED_GROUPS)
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
        for half in (replace(j, groups=new.groups), replace(new, accounts=j.accounts)):
            got = _resume(host, half)
            # every journaled id is kept, and the end state is the whole pair at the fresh-install ids
            assert {n: got.allocation.ids[n] for n in half.accounts} == dict(half.accounts)
            assert {g: got.allocation.shared[g] for g in half.groups} == dict(half.groups)
            assert got.intent.accounts[VERIFIER_ACCOUNT] == LINUX_UID
            assert got.intent.groups[VERIFIER_GROUP] == LINUX_GID


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


def _undo_plan(tmp_path: Path, monkeypatch, *, kill_at: str | None, record_group: bool = True):
    """Undo over a Linux host holding only the verifier's account, its own group and the shared ``levain-verify``
    (``record_group`` False: the install record does not name that group, so it is a same-name foreign one)."""
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
    shape = _shape(("account/1", (VERIFIER_ACCOUNT, LINUX_UID)),
                   *((("group/1", (VERIFIER_GROUP, LINUX_GID)),) if record_group else ()))
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
    # the tool's directory is the host's (``shutil.which``), so the pin is its name and its whole argument list
    assert ("chage", "--expiredate", "0", VERIFIER_ACCOUNT) in [(os.path.basename(a[0]), *a[1:]) for a in h.log]
    assert LINUX_UID in again.retired_after


def test_undo_leaves_a_same_name_levain_verify_the_record_does_not_name(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 (codex HIGH 1): ``levain-verify`` came after the install record, so its NAME is no proof: a live group of
    that name the record does not name (and no journal names) is not Levain's and undo leaves it, while every
    original name still goes by name (the pre-record installs) and a recorded ``levain-verify`` goes (the test above)."""
    h, live, plan = _undo_plan(tmp_path, monkeypatch, kill_at=None, record_group=False)
    assert not [s.why for s in plan.steps if s.why.startswith(f"delete the group {VERIFIER_GROUP}")]
    _walk(plan)
    assert h.groups[VERIFIER_GROUP][0] == LINUX_GID
    assert not [a for a in h.log if os.path.basename(a[0]) == "groupdel" and a[-1] == VERIFIER_GROUP]
    from tests.test_undo_remove import _facts as undo_facts, _shape
    legacy = IH.plan_undo(parse_install_argv(["undo"]), undo_facts("linux", _shape()), accounts={},
                          shared_groups={"levain-slots": RANGES["linux"].shared[0]}, live=live,
                          end=lambda u, n: (True, ""))
    assert [s.why for s in legacy.steps if s.why.startswith("delete the group levain-slots")]


def test_setup_records_the_verifier_group_so_undo_owns_it(tmp_path: Path, monkeypatch) -> None:
    """The other half of the rule above: an install made by THIS setup names ``group/1 levain-verify`` in its install
    record (the record step's own text, read back), so its undo still deletes the group."""
    path = tmp_path / "install-shape"
    monkeypatch.setattr(IH, "install_shape_path", lambda host: path)
    plan = IH.plan_setup(_setup(3, 2), _facts("linux"), read_sysctl=lambda p: "1", program_exists=lambda p: True)
    step = next(s for s in plan.steps if s.why.startswith("write the install record"))
    assert step.call()[0]
    shape = G.parse_shape(path.read_bytes())
    assert ("group/1", (VERIFIER_GROUP, LINUX_GID)) in shape.managed
    assert ("account/1", (VERIFIER_ACCOUNT, LINUX_UID)) in shape.managed


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


R4B = "journal the shared-group gids"
LATE = "journal the singletons and groups this repair makes"
FLIP = "flip "
ENTRY = "write the install-helper entry script"


def _account_steps(plan) -> list:
    """Every act a repair plan places that names the verifier's ACCOUNT (its own group, its user, its lock)."""
    return [s for s in plan.steps if VERIFIER_ACCOUNT in s.argv]


def _kill_steps(plan) -> list:
    """The points a kill test stops between, in plan order: every journal write (R1, R3, R4b and the late one), every
    act naming the verifier's account or group, the flip (R9) and the install-helper entry script (R10)."""
    return [s for s in plan.steps
            if (s.call is not None and s.why.startswith("journal")) or VERIFIER_ACCOUNT in s.argv
            or VERIFIER_GROUP in s.argv or s.why.startswith(FLIP) or s.why.startswith(ENTRY)]


def _pre_223_objections(host: str, journal: Intent | None, users: dict) -> list[str]:
    """What the code BEFORE levain#223 (still ``current`` until the flip and the entry scripts) refuses in the journal on
    disk and the live accounts, by a FROZEN copy of its rules (cd884ce00): ``ids.is_levain_account`` / ``in_own_range``
    over :data:`PRE_223_SINGLETONS`; ``intent.parse_intent`` refusing ``intent-malformed`` on any journaled account that
    fails them (groups are judged by the bound, unchanged since); ``generation.KINDS`` ``account/1`` (``in_own_range``)
    and ``member/1`` (``is_levain_account`` or the operator), which ``check_records`` applies to the journal's
    ``managed`` (``bad-record``); ``check_account_ids`` over every live labelled account (``not-levain-name``, as
    L2's run of that code printed for ``_levain_verify``); and (L3 r1, complement MED 1) its repair's R1b over that
    journal: ``leaving`` takes every journaled ``group/1`` whose name is not in its ``SHARED_GROUPS`` and ``remove =
    reconcile - desired`` deletes it (its ``desired`` names only those groups), so any such record is one the old
    resume removes. Empty: the old code reads this state and removes nothing of it."""
    def known(name: str) -> bool:
        m = SLOT_NAME_RE.fullmatch(name)
        return name in PRE_223_SINGLETONS or (m is not None and int(m.group(2)) < INDEX_SPACE)

    def in_range(name: str, uid: object) -> bool:
        if not isinstance(uid, int) or isinstance(uid, bool) or not known(name):
            return False
        r = RANGES[host]
        return uid in (r.singletons if name in PRE_223_SINGLETONS else r.slots)

    out: list[str] = []
    if journal is not None:
        out += [f"intent-malformed: {n}" for n, u in journal.accounts.items() if not in_range(n, u)]
        for kind, ident in journal.managed:
            if kind == "account/1" and not in_range(ident[0], ident[1]):
                out.append(f"bad-record: {kind} {ident!r}")
            if kind == "member/1" and not (known(ident[0]) or ident[0] == OP):
                out.append(f"bad-record: {kind} {ident!r}")
            if kind == "group/1" and ident[0] not in PRE_223_SHARED_GROUPS:
                out.append(f"R1b-removes: {kind} {ident!r}")
    out += [f"not-levain-name: {n}" for n, (u, label) in users.items() if label == SLOT_MARKER and not known(n)]
    return out


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
    ahead = next(i for i, w in enumerate(whys) if w.startswith(R4B))
    shared = next(i for i, s in enumerate(plan.steps) if s.argv[:1] and s.argv[0].endswith("groupadd")
                  and s.argv[-1] == VERIFIER_GROUP)
    flip = next(i for i, w in enumerate(whys) if w.startswith(FLIP))
    entry = next(i for i, w in enumerate(whys) if w.startswith(ENTRY))
    journal = next(i for i, w in enumerate(whys) if w.startswith(LATE))
    group = next(i for i, s in enumerate(plan.steps) if s.argv[1:2] == ("--system",) and s.argv[-1] == VERIFIER_ACCOUNT
                 and s.argv[0].endswith("groupadd"))
    user = next(i for i, s in enumerate(plan.steps) if s.argv[:1] and s.argv[0].endswith("useradd")
                and s.argv[-1] == VERIFIER_ACCOUNT)
    lock = next(i for i, s in enumerate(plan.steps) if s.argv[:1] and s.argv[0].endswith("usermod")
                and "--lock" in s.argv and s.argv[-1] == VERIFIER_ACCOUNT)
    first_member = next(i for i, s in enumerate(plan.steps) if s.why.startswith("put "))
    # levain#223 fix 1 and L3 r1: the shared group and the account are journaled and made only after the flip AND the
    # install-helper entry script, write-ahead: the journal that names them precedes the shared group, then the
    # account's own group, then its user
    assert ahead < first_member < flip < entry < journal < shared < group < user < lock
    # so nothing before its creation may name it: no membership, row (chown), probe or written file
    assert not [s.why for s in plan.steps[:journal] if VERIFIER_ACCOUNT in _names(s)]
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


def test_repair_journals_the_account_in_the_late_write_ahead_record_only(tmp_path: Path, intent_path: Path) -> None:
    """Run each journal step and read the file back with the reader's own rules. R4b's names NOTHING of the account
    (no ``accounts`` entry, no ``account/1``) nor of the shared group ``levain-verify`` (no ``groups`` entry, no
    ``group/1``: L3 r1); the late one, after the flip, names the account at the uid and the group at the gid with
    their records, and every other record R4b's had."""
    users, groups = _before()
    facts, rf = _state(tmp_path, "linux", users, groups)
    plan = _repair(facts, rf)
    r4b = next(s for s in plan.steps if s.why.startswith(R4B))
    late = next(s for s in plan.steps if s.why.startswith(LATE))
    assert r4b.call()[0]
    a = read_intent("linux", path=intent_path)
    assert a.verb == "repair" and a.accounts == {} and VERIFIER_GROUP not in a.groups
    assert set(a.groups) == set(ORIGINAL_SHARED_GROUPS)
    assert not [r for r in a.managed if VERIFIER_ACCOUNT in repr(r) or VERIFIER_GROUP in repr(r)]
    assert late.call()[0]
    b = read_intent("linux", path=intent_path)
    assert b.accounts == {VERIFIER_ACCOUNT: LINUX_UID} and b.txn == a.txn
    assert b.groups == {**a.groups, VERIFIER_GROUP: LINUX_GID}
    assert set(b.managed) - set(a.managed) == {("account/1", (VERIFIER_ACCOUNT, LINUX_UID)),
                                               ("group/1", (VERIFIER_GROUP, LINUX_GID))}
    assert b == plan.intent                                         # the plan's intent is the whole final record


class _Model:
    """The account database a replayed step changes: what ``groupadd`` / ``useradd`` / ``usermod --lock`` do."""

    def __init__(self, users: dict, groups: dict) -> None:
        self.users, self.groups, self.locked = dict(users), dict(groups), set()
        self.flipped = self.entries = False                    # R9 ran / R10's install-helper entry script written

    def apply(self, step) -> None:
        if step.why.startswith(FLIP):                          # recorded, never run: it moves the real import tree
            self.flipped = True
            return
        if step.why.startswith(ENTRY):
            self.entries = True
            return
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


def _kill_at_every_point(tmp_path: Path, intent_path: Path):
    """Kill a repair of a pre-#223 install after k of its :func:`_kill_steps` (k = 0 .. all). Yields, per k, the model
    as the kill left it, the journal on disk and the step names run; then re-plans from that (with ``current`` the
    target once the flip ran) under THIS code, replays the new plan's kill steps and yields the finished model."""
    base_users, base_groups = _before()
    first = _repair(*_state(tmp_path / "p0", "linux", base_users, base_groups))
    whys = [s.why for s in _kill_steps(first)]
    assert [w.split(" ")[0] for w in whys].count("flip") == 1 and sum(w.startswith(ENTRY) for w in whys) == 1, whys
    for k in range(len(whys) + 1):
        if intent_path.exists():
            intent_path.unlink()
        tmp = tmp_path / f"k{k}"
        m = _Model(base_users, base_groups)
        plan = _repair(*_state(tmp / "a", "linux", m.users, m.groups))
        assert [s.why.split(" (")[0] for s in _kill_steps(plan)] == [w.split(" (")[0] for w in whys]   # txn differs
        for s in _kill_steps(plan)[:k]:
            m.apply(s)
        journaled = _read_journal(intent_path)
        yield "killed", k, m, journaled, whys[:k]
        # what a rebooted host shows: the model's directory, the journal on disk, and (to try the pin, once a journal
        # names the account) a foreign account that now holds an id the allocator could otherwise pick
        named = journaled is not None and VERIFIER_ACCOUNT in journaled.accounts
        users2 = {**m.users, **({"squatter": (LINUX_UID - 1, "")} if named else {})}
        facts, rf = _state(tmp / "b", "linux", users2, m.groups, journal=journaled)
        if m.flipped:
            rf = replace(rf, current=journaled.generation)
        again = _repair(facts, rf)
        assert again.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}, k
        for s in _kill_steps(again):
            m.apply(s)
        yield "finished", k, m, _read_journal(intent_path), whys[:k]


def test_a_repair_killed_before_the_flip_leaves_what_the_pre_223_code_reads(tmp_path: Path, intent_path: Path) -> None:
    """L1 HIGH-1 / L2 H1: until the flip (R9) and R10's entry scripts, the operator's next verb (bare repair's resume,
    ``repair --source``'s phase A, undo) runs the INSTALLED generation's code, which on this install is pre-#223. At
    every kill point before both, the journal on disk and the live accounts pass that code's frozen rules, its R1b
    removal included (:func:`_pre_223_objections`), and NOTHING new is live: no user or group the install did not
    already hold (L3 r1: the shared group ``levain-verify`` too is journaled and made only after them). A point after
    them is read by this code only."""
    base_users, base_groups = _before()
    checked = 0
    for what, k, m, journaled, ran in _kill_at_every_point(tmp_path, intent_path):
        if what != "killed" or (m.flipped and m.entries):
            continue
        assert _pre_223_objections("linux", journaled, m.users) == [], (k, ran)
        assert set(m.users) == set(base_users) and set(m.groups) == set(base_groups), (k, ran)
        assert journaled is None or VERIFIER_GROUP not in journaled.groups, (k, ran)
        checked += 1
    assert checked >= 5, checked                                    # 0, R1, R3, R4b, the flip (not the entry)


def test_repair_resumes_at_the_journaled_uid_after_a_kill_at_every_point(tmp_path: Path, intent_path: Path) -> None:
    """Every kill point (:func:`_kill_at_every_point`) re-planned under this code ends with ONE verifier at the uid the
    late journal names: user, own group, the shared group, locked, the label Levain's; a kill after that journal never
    moves the uid, even with a foreign account taking an id in between. A kill before it leaves no account."""
    points = 0
    for what, k, m, journaled, ran in _kill_at_every_point(tmp_path, intent_path):
        points += what == "finished"
        if what == "killed":
            late_ran = any(w.startswith(LATE) for w in ran)
            assert (journaled is not None and VERIFIER_ACCOUNT in journaled.accounts) == late_ran, (k, ran)
            if not late_ran:
                assert VERIFIER_ACCOUNT not in m.users and not {VERIFIER_ACCOUNT, VERIFIER_GROUP} & set(m.groups), (k, ran)
            continue
        assert m.users[VERIFIER_ACCOUNT] == (LINUX_UID, SLOT_MARKER), k
        assert m.groups[VERIFIER_ACCOUNT] == LINUX_UID and m.groups[VERIFIER_GROUP] == LINUX_GID, k
        assert VERIFIER_ACCOUNT in m.locked, k
        assert [n for n, (u, _) in m.users.items() if u == LINUX_UID] == [VERIFIER_ACCOUNT], k
        assert journaled is not None and journaled.accounts == {VERIFIER_ACCOUNT: LINUX_UID}, k
    # R1, R3, R4b, the flip, the entry script, the late journal, levain-verify, the account's group, user and lock
    assert points == 10 + 1, points


def test_repair_resumes_a_group_without_its_user(tmp_path: Path, intent_path: Path) -> None:
    """The kill between the account's primary group and its user: the journal names the account, the same-named
    group is present at the journaled id and no user is: resumed (the group step skips, the user is made)."""
    users, groups = _before()
    groups[VERIFIER_ACCOUNT] = LINUX_UID
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    groups[VERIFIER_GROUP] = LINUX_GID
    plan = _repair(*_state(tmp_path, "linux", users, groups, journal=j))
    assert plan.intent.accounts == {VERIFIER_ACCOUNT: LINUX_UID}
    made = _account_steps(plan)
    assert not [s for s in plan.steps if s.why.startswith(LATE)]    # already journaled: no second write-ahead
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


def test_a_live_journaled_verifier_with_its_group_moved_is_refused(tmp_path: Path, intent_path: Path) -> None:
    """L2 L3: the journal names the account and its user is live (a kill after ``useradd``), but its same-named group
    now sits at another gid (a hand edit in between): ``group-missing`` before any act, as for every other live
    account. An ABSENT journaled account (its group planned with it) is still passed over."""
    users, groups = _before()
    users[VERIFIER_ACCOUNT] = (LINUX_UID, SLOT_MARKER)
    groups[VERIFIER_ACCOUNT] = LINUX_UID - 3
    groups[VERIFIER_GROUP] = LINUX_GID
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    with pytest.raises(Refused) as e:
        _repair(*_state(tmp_path, "linux", users, groups, journal=j))
    assert e.value.name == "group-missing" and VERIFIER_ACCOUNT in e.value.detail
    del users[VERIFIER_ACCOUNT], groups[VERIFIER_ACCOUNT]
    assert _repair(*_state(tmp_path / "absent", "linux", users, groups, journal=j)).intent.accounts == {
        VERIFIER_ACCOUNT: LINUX_UID}


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


def test_repair_of_a_pre_record_install_refuses_a_same_name_levain_verify(tmp_path: Path, intent_path: Path) -> None:
    """L3 r1 (codex HIGH 2): with no install record (a pre-row-33 install) a shared group is claimed by name and a
    shared-range gid only when an install of that age had it (``ORIGINAL_SHARED_GROUPS``). A ``levain-verify`` there
    at a shared-range gid is not one Levain made: ``account-exists``, never recorded or handed to the verifier. The
    same group journaled at its gid (a run killed after the late journal made it) is Levain's and resumed."""
    users, groups = _before()
    facts, rf = _state(tmp_path, "linux", users, groups)
    plain = _repair(replace(facts, install_shape=None), rf)        # the nine original groups still claimed by name
    assert plain.intent.groups[VERIFIER_GROUP] == LINUX_GID
    groups[VERIFIER_GROUP] = LINUX_GID
    facts, rf = _state(tmp_path / "f", "linux", users, groups)
    with pytest.raises(Refused) as e:
        _repair(replace(facts, install_shape=None), rf)
    assert e.value.name == "account-exists" and VERIFIER_GROUP in e.value.detail
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    facts, rf = _state(tmp_path / "j", "linux", users, groups, journal=j)
    plan = _repair(replace(facts, install_shape=None), rf)
    assert ("group/1", (VERIFIER_GROUP, LINUX_GID)) in plan.intent.managed
    assert not [s for s in plan.steps if s.argv[:1] and s.argv[-1] == VERIFIER_GROUP]   # present: not made again


@pytest.mark.parametrize("case", ["retired", "live-elsewhere", "uid-held"])
def test_a_journaled_verifier_uid_is_held_to_the_retired_and_used_ids(tmp_path: Path, intent_path: Path,
                                                                      case) -> None:
    """L3 r1 (deepseek MED 1, complement LOW 2): the journal names the account at a uid. That uid retired (§1.1 g: never
    reissued) is refused in ``_repair_creates``; the name live at another uid, or absent with the uid held by another
    account, is refused before it by ``_journal_presence`` (both pinned here). Each is ``account-exists``."""
    users, groups = _before()
    groups[VERIFIER_GROUP] = LINUX_GID
    retired: frozenset = frozenset()
    if case == "retired":
        retired = frozenset({LINUX_UID})
    elif case == "live-elsewhere":
        users[VERIFIER_ACCOUNT] = (LINUX_UID - 3, SLOT_MARKER)
        groups[VERIFIER_ACCOUNT] = LINUX_UID - 3
    else:
        users["someone"] = (LINUX_UID, "")
    j = Intent("repair", OP, {VERIFIER_ACCOUNT: LINUX_UID}, {VERIFIER_GROUP: LINUX_GID}, TXN_B)
    with pytest.raises(Refused) as e:
        _repair(*_state(tmp_path, "linux", users, groups, journal=j, retired=retired))
    assert e.value.name == "account-exists" and VERIFIER_ACCOUNT in e.value.detail
