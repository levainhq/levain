"""The install helper's plans (CONTRACT §5.2): ``plan_setup``, ``plan_undo``, ``plan_replenish``.

Each takes the parsed command (:mod:`levain.slot.grammar`) and the host's facts as data, and returns
an :class:`InstallPlan` whose ``steps`` are :class:`~levain.firing.hands.Step` s. Nothing here acts on
the OS; every refusal (live lease, second operator, capacity, index space) is raised while planning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HostOS, Step, _abs, _kill_all
from levain.slot import Refused, bwrap, netns, pf, root_tree, units
from levain.slot.accounts import (Account, Group, account_steps, accounts_of, delete_group_steps,
                                  membership_pairs, membership_steps, revive_steps, shared_group_steps,
                                  shared_groups_of, tombstone_steps)
from levain.slot.grammar import LEASE_EXEMPT, InstallCommand, check_roots
from levain.slot.ids import (RANGES, RETIRED_IDS_PATH, SHARED_GROUPS, SINGLETONS, SLOT_NAME_RE, Allocation,
                             SlotClass, SlotId, allocate, check_capacity, choose_indices, is_levain_account,
                             retired_ids_text, spent_indices)
from levain.slot.layout import (Row, base_routes_steps, base_run_rows, base_store_rows, operator_record_path,
                                policy_rows, reserve_row, reserve_steps, row_steps, slot_rows)
from levain.slot.sudoers import remove_sudoers_steps, sudoers_steps

OPERATOR_FORMAT = "levain-operator/1"


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
    pending: tuple[str, ...] = ()        # programs the plan points at that are not built yet


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


# --- setup -----------------------------------------------------------------------------------------


def _singletons_to_revive(facts: HostFacts) -> dict[str, int]:
    """CONTRACT §20: a singleton name present on the host is revived only when it carries Levain's
    retired marker AND its recorded id sits in the singleton range; any other present singleton or
    shared-group name is ``account-exists``."""
    singles = RANGES[facts.host].singletons
    revive: dict[str, int] = {}
    taken: list[str] = []
    for name in SINGLETONS:
        uid = facts.levain_tombstones.get(name)
        if uid is not None and uid in singles:
            revive[name] = uid
        elif uid is not None or name in facts.existing_names:
            taken.append(name)
    taken += [g for g in SHARED_GROUPS if g in facts.existing_names]
    if taken:
        raise Refused("account-exists", ", ".join(taken))
    return revive


def _levain_package() -> Path:
    import levain

    return Path(levain.__file__).resolve().parent


def plan_setup(cmd: InstallCommand, facts: HostFacts, *,
               realpath: Callable[[str], str] = os.path.realpath,
               lstat: Callable[[str], os.stat_result] = os.lstat,
               read_sysctl: Callable[[str], str] | None = None,
               source: Path | None = None) -> InstallPlan:
    """``read_sysctl``: reads a ``/proc/sys`` path (Linux, the AppArmor userns check); ``source``: the
    ``levain`` package copied into the root import tree (default: the one running)."""
    if cmd.verb != "setup" or cmd.slots is None or cmd.verifier_slots is None or cmd.operator is None:
        raise Refused("wrong-verb", cmd.verb)
    preflight(cmd, facts)
    host = facts.host
    assert facts.operator_uid is not None
    roots = check_roots(cmd.roots, facts.operator_uid, realpath=realpath, lstat=lstat)
    revive = _singletons_to_revive(facts)
    check_capacity(host, cmd.slots, cmd.verifier_slots, facts.retired)
    # Slot, front and verifier-pair tombstones are never revived: their indices are spent (§20).
    egress = choose_indices("egress", cmd.slots, spent_indices("egress", facts.existing_names))
    verifier = choose_indices("none", cmd.verifier_slots, spent_indices("none", facts.existing_names))
    alloc = allocate(host, egress=egress, verifier=verifier, used=facts.used_ids, retired=facts.retired,
                     singletons=True, shared=True, revive=revive)
    accounts = accounts_of(alloc)
    fresh = tuple(a for a in accounts if a.name not in revive)
    revived = tuple(a for a in accounts if a.name in revive)
    groups = shared_groups_of(alloc, cmd.operator)
    early = policy_rows(host)
    rows = ([r for r in base_store_rows(host, cmd.operator) if r not in early] + base_run_rows(host)
            + slot_rows(host, alloc.slots))
    if host == "linux":
        restricted = (bwrap.userns_restricted() if read_sysctl is None
                      else bwrap.userns_restricted(read_sysctl))
        confinement = [*bwrap.bwrap_steps(apparmor_userns_restricted=restricted), *netns.netns_steps(alloc.slots)]
    else:
        confinement = pf.load_steps(alloc.slots)
    steps: list[Step] = [
        *row_steps(host, early),
        # One operator per machine, claimed before anything else is made (CONTRACT header).
        Step(f"record {cmd.operator} as this machine's one operator",
             write=(operator_record_path(host), operator_record_text(cmd.operator, facts.operator_uid), 0o644)),
        *shared_group_steps(host, groups),
        *account_steps(host, fresh),
        *revive_steps(host, revived),
        *membership_steps(host, membership_pairs(accounts, cmd.operator)),
        *row_steps(host, rows),
        *reserve_steps(host),
        *base_routes_steps(host),
        *root_tree.tree_steps(host, source or _levain_package()),
        *confinement,
        *units.install_steps(host),
        *sudoers_steps(host, cmd.operator),
    ]
    return InstallPlan("setup", host, tuple(steps), alloc, accounts, groups,
                       tuple(early + rows + [reserve_row(host)]), roots, facts.retired,
                       revived=tuple(a.name for a in revived), pending=root_tree.PENDING + units.PENDING)


# --- undo ------------------------------------------------------------------------------------------


def slots_of(names: Iterable[str]) -> tuple[SlotId, ...]:
    """The slots whose primary accounts (``_levain_s<n>``, ``_levain_v<n>``) are among ``names``."""
    out = set()
    for name in names:
        m = SLOT_NAME_RE.fullmatch(name)
        if m and m.group(1) in ("s", "v") and is_levain_account(name):
            out.add(SlotId("egress" if m.group(1) == "s" else "none", int(m.group(2))))
    return tuple(sorted(out))


def plan_undo(cmd: InstallCommand, facts: HostFacts, *, accounts: Mapping[str, int],
              shared_groups: Iterable[str]) -> InstallPlan:
    """``accounts``: every live Levain account the directory service holds, name -> uid (== gid);
    ``shared_groups``: the shared groups present. Order: sudoers first (no new helper calls), every
    service stopped and its unit removed, ids retired, processes stopped, accounts tombstoned, shared
    groups deleted, slot confinement removed (Linux netns + the slots' bwrap; macOS the pf anchor),
    operator record LAST. The store and the root import tree stay."""
    if cmd.verb != "undo":
        raise Refused("wrong-verb", cmd.verb)
    preflight(cmd, facts)
    host = facts.host
    for name in accounts:
        if not is_levain_account(name):
            raise Refused("not-levain-name", repr(name))
    groups = [g for g in shared_groups if g in SHARED_GROUPS]
    retired_after = frozenset(facts.retired) | frozenset(accounts.values())
    path = RETIRED_IDS_PATH[host]
    mkdir = "/bin/mkdir" if host == "darwin" else _abs("mkdir")
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    steps: list[Step] = [
        *remove_sudoers_steps(host),
        # Every service stops before anything is removed; their unit files go with them.
        *units.uninstall_steps(host),
        Step(f"create {path.parent}", (mkdir, "-p", "-m", "755", str(path.parent))),
        Step("retire every account id so none is reissued", write=(path, retired_ids_text(retired_after), 0o644)),
    ]
    for name, uid in sorted(accounts.items()):
        steps.append(Step(f"stop every process of {name}, and check they are gone", call=lambda u=uid: _kill_all(u)))
    for name in sorted(accounts):
        steps += tombstone_steps(host, name)
    steps += delete_group_steps(host, groups)
    # Slot confinement goes only after every slot process is stopped and its account tombstoned.
    if host == "linux":
        steps += netns.netns_undo_steps(slots_of(accounts)) + bwrap.bwrap_undo_steps()
    else:
        steps += pf.unload_steps()
    steps.append(Step("remove the operator record (last)", (rm, "-f", str(operator_record_path(host)))))
    return InstallPlan("undo", host, tuple(steps), retired_after=retired_after)


# --- replenish -------------------------------------------------------------------------------------


def plan_replenish(cmd: InstallCommand, facts: HostFacts, *, slots: int, verifier_slots: int,
                   live: Mapping[SlotClass, Iterable[int]]) -> InstallPlan:
    """Bring each class back to its configured count with NEW indices (a retired index is never
    reissued). ``live``: the indices of each class still in service."""
    if cmd.verb != "replenish":
        raise Refused("wrong-verb", cmd.verb)
    preflight(cmd, facts)
    host = facts.host
    rec = facts.operator_record
    assert rec is not None
    check_capacity(host, slots, verifier_slots, facts.retired)
    new: dict[SlotClass, tuple[SlotId, ...]] = {}
    for cls, want in (("egress", slots), ("none", verifier_slots)):
        have = set(live.get(cls, ()))
        new[cls] = choose_indices(cls, max(0, want - len(have)),
                                  spent_indices(cls, facts.existing_names) | have)
    alloc = allocate(host, egress=new["egress"], verifier=new["none"], used=facts.used_ids, retired=facts.retired)
    accounts = accounts_of(alloc)
    rows = slot_rows(host, alloc.slots)
    in_service = tuple(SlotId(cls, i) for cls in ("egress", "none") for i in live.get(cls, ()))  # type: ignore[arg-type]
    steps = [
        *account_steps(host, accounts),
        *membership_steps(host, membership_pairs(accounts, None)),
        *row_steps(host, rows),
        # The new slots' confinement: their namespaces (Linux); the anchor re-rendered for every slot
        # in service, the new ones with it, so no slot runs without its pf rules (macOS).
        *(netns.netns_steps(alloc.slots) if host == "linux" else pf.load_steps(in_service + alloc.slots)),
    ]
    return InstallPlan("replenish", host, tuple(steps), alloc, accounts, (), tuple(rows), (), facts.retired)


__all__ = ["HostFacts", "InstallPlan", "OPERATOR_FORMAT", "operator_record", "operator_record_text",
           "parse_operator_record", "plan_replenish", "plan_setup", "plan_undo", "preflight"]
