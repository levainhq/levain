"""The install helper's plans (CONTRACT §5.2): ``plan_setup``, ``plan_undo``, ``plan_replenish``.

Each takes the parsed command (:mod:`levain.slot.grammar`) and the host's facts as data, and returns
an :class:`InstallPlan` whose ``steps`` are :class:`~levain.firing.hands.Step` s. Nothing here acts on
the OS; every refusal (live lease, second operator, capacity, index space) is raised while planning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HostOS, Step, _abs, _kill_all
from levain.slot import Refused
from levain.slot.accounts import (Account, Group, account_steps, accounts_of, delete_group_steps,
                                  membership_pairs, membership_steps, shared_group_steps, shared_groups_of,
                                  tombstone_steps)
from levain.slot.grammar import LEASE_EXEMPT, InstallCommand, check_roots
from levain.slot.ids import (RETIRED_IDS_PATH, SHARED_GROUPS, SINGLETONS, Allocation, SlotClass, SlotId,
                             allocate, check_capacity, choose_indices, is_levain_account, retired_ids_text,
                             spent_indices)
from levain.slot.layout import (Row, base_run_rows, base_store_rows, operator_record_path, policy_rows,
                                reserve_row, reserve_steps, row_steps, slot_rows)
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


def plan_setup(cmd: InstallCommand, facts: HostFacts, *,
               realpath: Callable[[str], str] = os.path.realpath,
               lstat: Callable[[str], os.stat_result] = os.lstat) -> InstallPlan:
    if cmd.verb != "setup" or cmd.slots is None or cmd.verifier_slots is None or cmd.operator is None:
        raise Refused("wrong-verb", cmd.verb)
    preflight(cmd, facts)
    host = facts.host
    assert facts.operator_uid is not None
    roots = check_roots(cmd.roots, facts.operator_uid, realpath=realpath, lstat=lstat)
    taken = [n for n in (*SINGLETONS, *SHARED_GROUPS) if n in facts.existing_names]
    if taken:
        raise Refused("account-exists", ", ".join(taken))
    check_capacity(host, cmd.slots, cmd.verifier_slots, facts.retired)
    egress = choose_indices("egress", cmd.slots, spent_indices("egress", facts.existing_names))
    verifier = choose_indices("none", cmd.verifier_slots, spent_indices("none", facts.existing_names))
    alloc = allocate(host, egress=egress, verifier=verifier, used=facts.used_ids, retired=facts.retired,
                     singletons=True, shared=True)
    accounts = accounts_of(alloc)
    groups = shared_groups_of(alloc, cmd.operator)
    early = policy_rows(host)
    rows = [r for r in base_store_rows(host) if r not in early] + base_run_rows(host) + slot_rows(host, alloc.slots)
    steps: list[Step] = [
        *row_steps(host, early),
        # One operator per machine, claimed before anything else is made (CONTRACT header).
        Step(f"record {cmd.operator} as this machine's one operator",
             write=(operator_record_path(host), operator_record_text(cmd.operator, facts.operator_uid), 0o644)),
        *shared_group_steps(host, groups),
        *account_steps(host, accounts),
        *membership_steps(host, membership_pairs(accounts, cmd.operator)),
        *row_steps(host, rows),
        *reserve_steps(host),
        *sudoers_steps(host, cmd.operator),
    ]
    return InstallPlan("setup", host, tuple(steps), alloc, accounts, groups,
                       tuple(early + rows + [reserve_row(host)]), roots, facts.retired)


# --- undo ------------------------------------------------------------------------------------------


def plan_undo(cmd: InstallCommand, facts: HostFacts, *, accounts: Mapping[str, int],
              shared_groups: Iterable[str]) -> InstallPlan:
    """``accounts``: every live Levain account the directory service holds, name -> uid (== gid);
    ``shared_groups``: the shared groups present. Order: sudoers first (no new helper calls), ids
    retired, processes stopped, accounts tombstoned, shared groups deleted, operator record LAST."""
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
        Step(f"create {path.parent}", (mkdir, "-p", "-m", "755", str(path.parent))),
        Step("retire every account id so none is reissued", write=(path, retired_ids_text(retired_after), 0o644)),
    ]
    for name, uid in sorted(accounts.items()):
        steps.append(Step(f"stop every process of {name}, and check they are gone", call=lambda u=uid: _kill_all(u)))
    for name in sorted(accounts):
        steps += tombstone_steps(host, name)
    steps += delete_group_steps(host, groups)
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
    steps = [
        *account_steps(host, accounts),
        *membership_steps(host, membership_pairs(accounts, None)),
        *row_steps(host, rows),
    ]
    return InstallPlan("replenish", host, tuple(steps), alloc, accounts, (), tuple(rows), (), facts.retired)


__all__ = ["HostFacts", "InstallPlan", "OPERATOR_FORMAT", "operator_record", "operator_record_text",
           "parse_operator_record", "plan_replenish", "plan_setup", "plan_undo", "preflight"]
