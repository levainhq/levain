"""The install helper's plans (CONTRACT §5.2, §23): ``plan_setup``, ``plan_undo``, ``plan_replenish``.

Each takes the parsed command (:mod:`levain.slot.grammar`) and the host's facts as data, and returns
an :class:`InstallPlan` whose ``steps`` are :class:`~levain.firing.hands.Step` s. Nothing here acts on
the OS; every refusal (live lease, second operator, capacity, index space, a missing tool) is raised
while planning, before step 1.

Re-runs (§23): setup writes the operator record LAST, so ``already-set-up`` means a complete install,
and a setup that failed part-way re-runs: every step is idempotent (a ``skip_if`` where the act is
not), and what the interrupted run made is kept at the ids it holds (:attr:`HostFacts.levain_live`).
Undo removes the runtime rule first and the install rule last, before the record, so a failed undo
re-runs through the install helper.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HostOS, Step, _abs, _kill_all
from levain.slot import Refused, bwrap, netns, pf, root_tree, units
from levain.slot.intent import Intent, intent_remove_step, intent_write_step
from levain.slot.accounts import (Account, Group, account_steps, accounts_of, delete_group_steps,
                                  membership_pairs, membership_steps, revive_steps, shared_group_steps,
                                  shared_groups_of, tombstone_steps)
from levain.slot.grammar import LEASE_EXEMPT, InstallCommand, check_roots
from levain.slot.ids import (RANGES, RETIRED_IDS_PATH, SHARED_GROUPS, SINGLETONS, SLOT_NAME_RE, Allocation,
                             SlotClass, SlotId, allocate, check_capacity, choose_indices, in_own_range,
                             is_levain_account, retired_ids_text, spent_indices)
from levain.slot.layout import (Row, base_routes_steps, base_run_rows, base_store_rows, operator_record_path,
                                policy_rows, reserve_row, reserve_steps, roots_steps, row_steps,
                                single_filesystem_steps, slot_rows, tmpfiles_steps, tmpfiles_undo_steps,
                                ungroup_steps)
from levain.slot.sudoers import remove_install_sudoers_steps, remove_runtime_sudoers_steps, sudoers_steps

OPERATOR_FORMAT = "levain-operator/1"


def _tool_present(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _xcode_ready() -> bool:
    """macOS: ``/usr/bin/python3`` is a shim that runs only once the developer tools are installed."""
    try:
        return subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode == 0
    except OSError:
        return False


@dataclass(frozen=True)
class HostFacts:
    """What the apply layer reads before planning; the plan reads nothing else."""

    host: HostOS
    caller_uid: int                                   # SUDO_UID
    caller_name: str | None = None                    # the name SUDO_UID resolves to
    operator_uid: int | None = None                   # setup: the uid ``--operator`` resolves to
    operator_record: Mapping[str, object] | None = None   # parsed ``L/policy/operator``, or None
    live_leases: int = 0
    used_ids: frozenset[int] = frozenset()            # every uid and gid in use on the host
    existing_names: frozenset[str] = frozenset()      # every user and group name on the host
    retired: frozenset[int] = frozenset()             # the retired-ids file
    #: Accounts whose label is Levain's retired marker (``accounts.SLOT_RETIRED_MARKER``), name -> uid.
    levain_tombstones: Mapping[str, int] = field(default_factory=dict)
    #: Accounts and shared groups whose label is Levain's live marker (``accounts.SLOT_MARKER``),
    #: name -> id: what an interrupted setup made (§23).
    levain_live: Mapping[str, int] = field(default_factory=dict)
    #: Whether a host tool (an absolute path) is present and executable (§23 preflight).
    tool_present: Callable[[str], bool] = _tool_present
    #: macOS: ``xcode-select -p`` succeeds (§23 preflight).
    xcode_ready: Callable[[], bool] = _xcode_ready
    #: The intent journal (§25), read under the install lock; None when there is none.
    intent: Intent | None = None
    #: For each name the journal lists that is present on the host, the id it holds there (the uid of
    #: an account, the gid of a group): a journaled name at its journaled id is Levain's (§25).
    host_ids: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class InstallPlan:
    verb: str
    host: HostOS
    steps: tuple[Step, ...]
    allocation: Allocation | None = None
    accounts: tuple[Account, ...] = ()
    groups: tuple[Group, ...] = ()
    rows: tuple[Row, ...] = ()
    roots: tuple[str, ...] = ()
    retired_after: frozenset[int] = field(default_factory=frozenset)
    revived: tuple[str, ...] = ()        # singletons brought back at their recorded id (§20)
    kept: tuple[str, ...] = ()           # names an interrupted setup made, kept at their ids (§23)
    pending: tuple[str, ...] = ()        # programs the plan points at that are not built yet
    services: tuple[str, ...] = ()       # the §1.3 services whose units this plan installs
    intent: Intent | None = None         # what the plan's first step journals (§25)


# --- the operator record ---------------------------------------------------------------------------


def operator_record(name: str, uid: int) -> dict[str, object]:
    return {"format": OPERATOR_FORMAT, "name": name, "uid": uid}


def operator_record_text(name: str, uid: int) -> str:
    return canon.canonical(operator_record(name, uid))


def parse_operator_record(data: bytes) -> dict[str, object]:
    try:
        obj = canon.parse(data)
    except canon.ContractError as e:
        raise Refused("operator-record-malformed", str(e)) from None
    if (not isinstance(obj, dict) or set(obj) != {"format", "name", "uid"} or obj["format"] != OPERATOR_FORMAT
            or not isinstance(obj["name"], str) or not OPERATOR_RE.fullmatch(obj["name"])
            or not isinstance(obj["uid"], int) or isinstance(obj["uid"], bool) or obj["uid"] <= 0):
        raise Refused("operator-record-malformed", "not a levain-operator/1 record")
    return obj


# --- preflight: the refusals every verb shares ----------------------------------------------------


def preflight(cmd: InstallCommand, facts: HostFacts) -> None:
    """Live lease first, then the one-operator rule (CONTRACT header, §5.2)."""
    if cmd.verb not in LEASE_EXEMPT and facts.live_leases > 0:
        raise Refused("live-lease", f"{cmd.verb} refuses while {facts.live_leases} lease(s) are live")
    rec = facts.operator_record
    if cmd.verb == "setup":
        if facts.operator_uid is None:
            raise Refused("no-such-operator", repr(cmd.operator))
        if facts.operator_uid == 0:
            raise Refused("bad-operator", "the operator cannot be uid 0")
        if rec is not None:
            if rec.get("name") == cmd.operator and rec.get("uid") == facts.operator_uid:
                raise Refused("already-set-up", "use repair")
            raise Refused("second-operator", f"configured for {rec.get('name')!r}, not {cmd.operator!r}")
        if facts.caller_uid != facts.operator_uid:
            raise Refused("caller-not-operator", f"uid {facts.caller_uid} is not {cmd.operator!r}")
        return
    if rec is None:
        raise Refused("not-set-up", cmd.verb)
    if facts.caller_uid != rec.get("uid") or facts.caller_name != rec.get("name"):
        raise Refused("operator-mismatch", f"configured for {rec.get('name')!r}")


def plan_tools(steps: Sequence[Step]) -> tuple[str, ...]:
    """Every host program a step runs: its argv's, its ``skip_if``'s and its validator's."""
    return tuple(sorted({x[0] for s in steps for x in (s.argv, s.skip_if, s.validate) if x}))


def extra_tools(host: HostOS, verb: str) -> tuple[str, ...]:
    """The host programs a plan runs that no step's argv names first: the system bwrap (Linux) is only
    the copy's SOURCE argument, and pkill, pgrep and dseditgroup run inside step callbacks. A fixed
    list per host and verb, checked with every plan's own tools, so a missing one refuses before step 1
    instead of failing part-way. Only setup and replenish copy bwrap; undo removes the copy and never
    reads the system one, so a host whose bwrap is gone can still undo. (``/usr/bin/python3`` is
    covered by the ``xcode-select`` check.)"""
    if host == "darwin":
        return ("/usr/sbin/dseditgroup", "/usr/bin/pkill", "/usr/bin/pgrep")
    if host == "linux":
        copies = (bwrap.system_bwrap(),) if verb in ("setup", "replenish") else ()
        return (*copies, _abs("pkill"), _abs("pgrep"))
    raise Refused("unsupported-host", repr(host))


def check_tools(facts: HostFacts, steps: Sequence[Step], *, verb: str, python: bool = False) -> None:
    """CONTRACT §23: every tool a step names, and every one of :func:`extra_tools`, is an absolute path
    that is present, refused by name before step 1 (a bare name is a tool the plan could not resolve on
    this host). ``python``: the plan installs root-run Python, so on macOS ``xcode-select -p`` must
    succeed too."""
    wanted = sorted(set(plan_tools(steps)) | set(extra_tools(facts.host, verb)))
    missing = [t for t in wanted if not t.startswith("/") or not facts.tool_present(t)]
    if missing:
        raise Refused("tool-missing", ", ".join(missing))
    if python and facts.host == "darwin" and not facts.xcode_ready():
        raise Refused("tool-missing", f"{root_tree.ROOT_PYTHON['darwin']} (xcode-select -p fails: no developer tools)")


# --- setup -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reuse:
    revive: dict[str, int]              # singleton tombstones, at their recorded id (§20)
    keep: dict[str, int]                # accounts an interrupted setup made (§23)
    keep_shared: dict[str, int]         # shared groups an interrupted setup made (§23)
    kept_slots: tuple[SlotId, ...]      # the slots whose accounts those are


def _reuse(facts: HostFacts, slots: int, verifier_slots: int) -> _Reuse:
    """CONTRACT §20 + §23: what setup brings back or keeps, and what it refuses as ``account-exists``.

    A singleton carrying Levain's live marker at an id in its range is kept (an interrupted setup made
    it); one carrying the retired marker at such an id is revived; any other present singleton or
    shared-group name is refused. Slot accounts carrying the live marker are kept with their slot."""
    r = RANGES[facts.host]
    live = dict(facts.levain_live)
    revive: dict[str, int] = {}
    keep: dict[str, int] = {}
    keep_shared: dict[str, int] = {}
    taken: list[str] = []
    if facts.intent is not None and facts.intent.verb == "setup":
        # §25: what the journal names, present at its journaled id, is ours with or without a marker. A
        # singleton tombstone stays on the revive path below: until it is relabelled it is not live.
        ours, foreign = _journal_presence(facts)
        taken += foreign
        for name, i in ours.items():
            if name not in facts.levain_tombstones:
                live.setdefault(name, i)
    for name in SINGLETONS:
        cur, tomb = live.get(name), facts.levain_tombstones.get(name)
        if cur is not None and in_own_range(facts.host, name, cur):
            keep[name] = cur
        elif cur is not None:
            taken.append(name)
        elif tomb is not None and tomb in r.singletons:
            revive[name] = tomb
        elif tomb is not None or name in facts.existing_names:
            taken.append(name)
    for g in SHARED_GROUPS:
        cur = live.get(g)
        if cur is not None and isinstance(cur, int) and cur in r.shared:
            keep_shared[g] = cur
        elif cur is not None or g in facts.existing_names:
            taken.append(g)
    kept: set[SlotId] = set()
    for name, uid in sorted(live.items()):
        m = SLOT_NAME_RE.fullmatch(name)
        if m is None or not is_levain_account(name):
            continue
        if not in_own_range(facts.host, name, uid):
            taken.append(name)
            continue
        keep[name] = uid
        kept.add(SlotId("egress" if m.group(1) in ("s", "gw") else "none", int(m.group(2))))
    if taken:
        raise Refused("account-exists", ", ".join(taken))
    for cls, want in (("egress", slots), ("none", verifier_slots)):
        have = sum(1 for s in kept if s.slot_class == cls)
        if have > want:
            raise Refused("account-exists", f"an interrupted setup made {have} {cls} slots; re-run it with "
                                            f"at least that many")
    return _Reuse(revive, keep, keep_shared, tuple(sorted(kept)))


def _journal_presence(facts: HostFacts) -> tuple[dict[str, int], list[str]]:
    """CONTRACT §25: (the journal's names present on the host at their journaled id, which are Levain's;
    the names that refuse as ``account-exists``: present at another id, or absent while their id is in
    use by something else)."""
    assert facts.intent is not None
    ours: dict[str, int] = {}
    foreign: list[str] = []
    for name, i in sorted(facts.intent.names.items()):
        have = facts.host_ids.get(name)
        if have == i:
            ours[name] = i
        elif have is not None:
            foreign.append(f"{name} (journaled at {i}, present at {have})")
        elif i in facts.used_ids:
            foreign.append(f"{name} (journaled id {i} is in use)")
    return ours, foreign


def _journal_slots(intent: Intent) -> dict[SlotClass, tuple[SlotId, ...]]:
    """The slots a journal allocated, by class. Every account of each is journaled and nothing else
    but singletons, or the journal is not one a plan wrote."""
    slots = slots_of(intent.accounts)
    names = {n for s in slots for n in s.accounts}
    odd = (set(intent.accounts) ^ names) - set(SINGLETONS)
    if odd:
        raise Refused("intent-malformed", f"the journal's accounts are not whole slots: {', '.join(sorted(odd))}")
    return {cls: tuple(s for s in slots if s.slot_class == cls) for cls in ("egress", "none")}  # type: ignore[misc]


def _levain_package() -> Path:
    import levain

    return Path(levain.__file__).resolve().parent


def plan_setup(cmd: InstallCommand, facts: HostFacts, *,
               realpath: Callable[[str], str] = os.path.realpath,
               lstat: Callable[[str], os.stat_result] = os.lstat,
               read_sysctl: Callable[[str], str] | None = None,
               source: Path | None = None,
               program_exists: Callable[[Path], bool] = Path.is_file) -> InstallPlan:
    """``read_sysctl``: reads a ``/proc/sys`` path (Linux, the AppArmor userns check); ``source``: the
    ``levain`` package copied into the root import tree (default: the one running);
    ``program_exists``: whether a service's program file is in ``source`` (§23)."""
    if cmd.verb != "setup" or cmd.slots is None or cmd.verifier_slots is None or cmd.operator is None:
        raise Refused("wrong-verb", cmd.verb)
    journal = facts.intent
    if journal is not None and journal.verb != "setup":
        # Another verb was interrupted: that verb finishes it, never a setup planned over its half.
        raise Refused("install-interrupted", journal.verb)
    preflight(cmd, facts)
    host = facts.host
    assert facts.operator_uid is not None
    roots = check_roots(cmd.roots, facts.operator_uid, realpath=realpath, lstat=lstat)
    reuse = _reuse(facts, cmd.slots, cmd.verifier_slots)
    check_capacity(host, cmd.slots, cmd.verifier_slots, facts.retired)
    chosen: dict[SlotClass, tuple[SlotId, ...]] = {}
    keep, keep_shared = dict(reuse.keep), dict(reuse.keep_shared)
    if journal is not None:
        # §25: an interrupted setup is re-planned at the journal's allocation, the same names at the same
        # ids, never allocated afresh; so it is re-run as it was first asked for.
        chosen = _journal_slots(journal)
        asked = (cmd.operator, cmd.slots, cmd.verifier_slots)
        was = (journal.operator, len(chosen["egress"]), len(chosen["none"]))
        if asked != was:
            raise Refused("install-interrupted", f"setup: re-run it as setup --slots {was[1]} "
                                                 f"--verifier-slots {was[2]} --operator {was[0]}")
        if not set(SINGLETONS) <= set(journal.accounts) or set(journal.groups) != set(SHARED_GROUPS):
            raise Refused("intent-malformed", "a setup journal names every singleton and shared group")
        unjournaled = sorted((set(keep) - set(journal.accounts)) | (set(keep_shared) - set(journal.groups)))
        if unjournaled:
            raise Refused("account-exists", ", ".join(unjournaled))
        keep.update((n, i) for n, i in journal.accounts.items() if n not in reuse.revive)
        keep_shared.update(journal.groups)
    else:
        # Slot, front and verifier-pair tombstones are never revived: their indices are spent (§20).
        # Slots an interrupted setup made come first, at their own indices (§23).
        for cls, want in (("egress", cmd.slots), ("none", cmd.verifier_slots)):
            mine = tuple(s for s in reuse.kept_slots if s.slot_class == cls)
            spent = spent_indices(cls, facts.existing_names) | {s.index for s in mine}
            chosen[cls] = mine + choose_indices(cls, want - len(mine), spent)
    alloc = allocate(host, egress=chosen["egress"], verifier=chosen["none"], used=facts.used_ids,
                     retired=facts.retired, singletons=True, shared=True, revive=reuse.revive,
                     keep=keep, keep_shared=keep_shared)
    accounts = accounts_of(alloc)
    fresh = tuple(a for a in accounts if a.name not in reuse.revive)
    revived = tuple(a for a in accounts if a.name in reuse.revive)
    groups = shared_groups_of(alloc, cmd.operator)
    early = policy_rows(host)
    rows = ([r for r in base_store_rows(host, cmd.operator) if r not in early] + base_run_rows(host)
            + slot_rows(host, alloc.slots))
    package = source or _levain_package()
    services, skipped = units.built_services(host, package, exists=program_exists)
    if host == "linux":
        restricted = (bwrap.userns_restricted() if read_sysctl is None
                      else bwrap.userns_restricted(read_sysctl))
        confinement = [*bwrap.bwrap_steps(apparmor_userns_restricted=restricted), *netns.netns_steps(alloc.slots)]
        boot_dirs = tmpfiles_steps(host, alloc.slots)
    else:
        # Each egress slot's lo0 alias (§24) before the rules that name it.
        confinement = [*pf.alias_steps(alloc.slots), *pf.load_steps(alloc.slots, alloc.ids)]
        boot_dirs = []
    intent = Intent("setup", cmd.operator, dict(alloc.ids), dict(alloc.shared))
    steps: list[Step] = [
        # FIRST (§25): the exact allocation, before anything is made, so an interrupted run re-plans it.
        intent_write_step(host, intent),
        *row_steps(host, early),
        *shared_group_steps(host, groups),
        *account_steps(host, fresh),
        *revive_steps(host, revived),
        *membership_steps(host, membership_pairs(accounts, cmd.operator)),
        *row_steps(host, rows),
        *boot_dirs,
        *reserve_steps(host),
        *single_filesystem_steps(host, alloc.slots),
        *base_routes_steps(host),
        *roots_steps(host, roots),
        *root_tree.tree_steps(host, package),
        *confinement,
        *units.install_steps(host, services),
        *sudoers_steps(host, cmd.operator),
        # LAST (§23): only a complete install is `already-set-up`; until this, setup re-runs.
        Step(f"record {cmd.operator} as this machine's one operator (last)",
             write=(operator_record_path(host), operator_record_text(cmd.operator, facts.operator_uid), 0o644)),
        # After the record: a run dying between the two leaves a finished install with a stale setup
        # journal, which replenish overwrites and undo removes.
        intent_remove_step(host),
    ]
    check_tools(facts, steps, verb="setup", python=True)
    kept = tuple(sorted(reuse.keep)) + tuple(sorted(reuse.keep_shared))
    return InstallPlan("setup", host, tuple(steps), alloc, accounts, groups,
                       tuple(early + rows + [reserve_row(host)]), roots, facts.retired,
                       revived=tuple(a.name for a in revived), kept=kept,
                       pending=root_tree.PENDING + skipped, services=tuple(s.name for s in services),
                       intent=intent)


# --- undo ------------------------------------------------------------------------------------------


def slots_of(names: Iterable[str]) -> tuple[SlotId, ...]:
    """The slots whose primary accounts (``_levain_s<n>``, ``_levain_v<n>``) are among ``names``."""
    out = set()
    for name in names:
        m = SLOT_NAME_RE.fullmatch(name)
        if m and m.group(1) in ("s", "v") and is_levain_account(name):
            out.add(SlotId("egress" if m.group(1) == "s" else "none", int(m.group(2))))
    return tuple(sorted(out))


def check_account_ids(host: HostOS, accounts: Mapping[str, int]) -> None:
    """CONTRACT §23: every name a Levain account name, every uid in §1.1's range for its class, so no
    caller-supplied id can aim a kill or a retire at an account that is not one of ours."""
    for name, uid in accounts.items():
        if not is_levain_account(name):
            raise Refused("not-levain-name", repr(name))
        if not in_own_range(host, name, uid):
            raise Refused("bad-token", f"{name}: uid {uid!r} is not in its §1.1 range")


def _abandoned_setup(facts: HostFacts) -> Intent | None:
    """CONTRACT §25 (c7.7a): the setup journal undo runs against when there is no operator record (the
    setup died before writing it), or None when undo takes its ordinary path. With no record, only the
    journal's operator may run it, by the name the caller's uid resolves to; a live lease still refuses."""
    j = facts.intent
    if facts.operator_record is not None or j is None or j.verb != "setup":
        return None
    if facts.live_leases > 0:
        raise Refused("live-lease", f"undo refuses while {facts.live_leases} lease(s) are live")
    if facts.caller_uid == 0 or facts.caller_name != j.operator:
        raise Refused("operator-mismatch", f"the interrupted setup was for {j.operator!r}")
    return j


def plan_undo(cmd: InstallCommand, facts: HostFacts, *, accounts: Mapping[str, int],
              shared_groups: Iterable[str]) -> InstallPlan:
    """``accounts``: every live Levain account the directory service holds, name -> uid (== gid);
    ``shared_groups``: the shared groups present. Order (§23): the runtime sudoers rule first (no new
    runtime-helper calls), every service stopped and its unit removed, ids retired, processes stopped,
    accounts tombstoned, every dir naming a shared group given back to root's group, the shared groups
    deleted, slot confinement removed (Linux netns + the slots' bwrap + tmpfiles.d; macOS the slots'
    lo0 aliases, the pf rules and pf's enable reference), the install sudoers rule, the intent journal,
    the operator record LAST. Every step is safe to re-run. The store and the root import tree stay.
    With a journal (§25), the accounts and groups it names that are present at their journaled ids are
    tombstoned and deleted with the rest. With no operator record and a SETUP journal (the setup died
    before its record), undo runs against the journal alone, for its operator, and ends by removing the
    journal: there is no record to remove."""
    if cmd.verb != "undo":
        raise Refused("wrong-verb", cmd.verb)
    abandoned = _abandoned_setup(facts)
    if abandoned is None:
        preflight(cmd, facts)
        rec = facts.operator_record
        assert rec is not None and isinstance(rec.get("name"), str)
        operator = str(rec["name"])
    else:
        # §25 (c7.7a): only the journal speaks for what that setup made, so the directory service's
        # reports are set aside and the journal's own names, at their journaled ids, are undone.
        operator = abandoned.operator
        accounts, shared_groups = {}, ()
    host = facts.host
    check_account_ids(host, accounts)
    accounts = dict(accounts)
    journaled_groups: list[str] = []
    if facts.intent is not None:
        # §25: whatever an interrupted run made at its journaled ids goes with the rest, a Linux shared
        # group with no label included. A journaled name present at another id is not ours: left alone.
        ours, _ = _journal_presence(facts)
        for name, i in ours.items():
            if name in facts.intent.accounts:
                accounts.setdefault(name, i)
            else:
                journaled_groups.append(name)
        check_account_ids(host, accounts)
    groups = list(dict.fromkeys(g for g in (*shared_groups, *journaled_groups) if g in SHARED_GROUPS))
    retired_after = frozenset(facts.retired) | frozenset(accounts.values())
    path = RETIRED_IDS_PATH[host]
    mkdir = "/bin/mkdir" if host == "darwin" else _abs("mkdir")
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    steps: list[Step] = [
        *remove_runtime_sudoers_steps(host),
        # Every service stops before anything is removed; their unit files go with them.
        *units.uninstall_steps(host),
        Step(f"create {path.parent}", (mkdir, "-p", "-m", "755", str(path.parent))),
        Step("retire every account id so none is reissued", write=(path, retired_ids_text(retired_after), 0o644)),
    ]
    for name, uid in sorted(accounts.items()):
        steps.append(Step(f"stop every process of {name}, and check they are gone", call=lambda u=uid: _kill_all(u)))
    for name in sorted(accounts):
        steps += tombstone_steps(host, name)
    steps += ungroup_steps(host, operator)
    steps += delete_group_steps(host, groups)
    # Slot confinement goes only after every slot process is stopped and its account tombstoned.
    if host == "linux":
        steps += netns.netns_undo_steps(slots_of(accounts)) + bwrap.bwrap_undo_steps() + tmpfiles_undo_steps()
    else:
        # The slots' lo0 aliases go before the rules guarding them are flushed (§24).
        steps += pf.alias_undo_steps(slots_of(accounts)) + pf.unload_steps()
    steps += remove_install_sudoers_steps(host)
    # Just before the record (§25): until undo is complete a re-run still reads what the journal names.
    steps.append(intent_remove_step(host))
    if abandoned is None:
        steps.append(Step("remove the operator record (last)", (rm, "-f", str(operator_record_path(host)))))
    check_tools(facts, steps, verb="undo")
    return InstallPlan("undo", host, tuple(steps), retired_after=retired_after)


# --- replenish -------------------------------------------------------------------------------------


def plan_replenish(cmd: InstallCommand, facts: HostFacts, *, slots: int, verifier_slots: int,
                   live: Mapping[SlotClass, Iterable[int]],
                   accounts: Mapping[str, int] | None = None) -> InstallPlan:
    """Bring each class back to its configured count with NEW indices (a retired index is never
    reissued). ``live``: the indices of each class still in service; ``accounts``: their accounts,
    name -> uid (macOS: the pf rules name every slot in service by uid, §23). With a replenish journal
    (§25) the plan is that journal's slots, resumed, and nothing more."""
    if cmd.verb != "replenish":
        raise Refused("wrong-verb", cmd.verb)
    preflight(cmd, facts)
    host = facts.host
    rec = facts.operator_record
    assert rec is not None
    accounts = dict(accounts or {})
    check_account_ids(host, accounts)
    check_capacity(host, slots, verifier_slots, facts.retired)
    journal = facts.intent
    new: dict[SlotClass, tuple[SlotId, ...]] = {}
    if journal is not None and journal.verb == "replenish":
        # §25: an interrupted replenish is resumed at the slots, indices and ids it journaled, and no new
        # index is chosen until its journal is gone; the next run fills to the configured counts.
        if journal.operator != rec.get("name"):
            raise Refused("install-interrupted", f"replenish: journaled for {journal.operator!r}")
        _, foreign = _journal_presence(facts)
        if foreign:
            raise Refused("account-exists", ", ".join(foreign))
        new = _journal_slots(journal)
        alloc = allocate(host, egress=new["egress"], verifier=new["none"], used=facts.used_ids,
                         retired=facts.retired, keep=journal.accounts)
    else:
        # A setup journal here is stale (the operator record, written before its removal, proves that
        # setup finished); this run's journal write replaces it.
        for cls, want in (("egress", slots), ("none", verifier_slots)):
            have = set(live.get(cls, ()))
            new[cls] = choose_indices(cls, max(0, want - len(have)),
                                      spent_indices(cls, facts.existing_names) | have)
        alloc = allocate(host, egress=new["egress"], verifier=new["none"], used=facts.used_ids,
                         retired=facts.retired)
    made = accounts_of(alloc)
    rows = slot_rows(host, alloc.slots)
    resumed = set(alloc.slots)
    in_service = tuple(s for s in (SlotId(cls, i) for cls in ("egress", "none") for i in live.get(cls, ()))  # type: ignore[arg-type]
                       if s not in resumed)
    if host == "linux":
        # Every slot in service keeps its boot-time run dirs; the slots' bwrap re-copied if the system's changed.
        confinement = [*netns.netns_steps(alloc.slots), *tmpfiles_steps(host, in_service + alloc.slots),
                       *bwrap.copy_steps()]
    else:
        # The rules re-rendered for every slot in service, the new ones with it, so no slot runs
        # without its pf rules; every egress slot's alias (§24) added where lo0 lacks it.
        confinement = [*pf.alias_steps(in_service + alloc.slots),
                       *pf.load_steps(in_service + alloc.slots, {**accounts, **alloc.ids})]
    intent = Intent("replenish", str(rec.get("name")), dict(alloc.ids))
    steps = [
        # FIRST (§25): the slots this run makes, so a run that dies part-way resumes them, never spends more.
        intent_write_step(host, intent),
        *account_steps(host, made),
        *membership_steps(host, membership_pairs(made, None)),
        *row_steps(host, rows),
        *confinement,
        intent_remove_step(host),   # LAST, after the confinement steps
    ]
    check_tools(facts, steps, verb="replenish")
    return InstallPlan("replenish", host, tuple(steps), alloc, made, (), tuple(rows), (), facts.retired,
                       intent=intent)


#: The entry's exit while the apply layer is unbuilt: a caller that trusts the exit code must never read
#: an install that did nothing as one that worked.
EXIT_NOT_BUILT = 2

__all__ = ["EXIT_NOT_BUILT", "HostFacts", "InstallPlan", "OPERATOR_FORMAT", "check_account_ids", "check_tools",
           "extra_tools", "operator_record",
           "operator_record_text", "parse_operator_record", "plan_replenish", "plan_setup", "plan_tools",
           "plan_undo", "preflight"]


if __name__ == "__main__":
    import sys

    sys.stderr.write("levain install helper: apply layer not built (S1 session 2)\n")
    raise SystemExit(EXIT_NOT_BUILT)
