"""Account and group plans per OS (CONTRACT §1.1, §1.2), as :class:`~levain.firing.hands.Step` lists.

Every account: a primary group of its own name with ``gid == uid``, no login shell, password locked,
hidden (macOS) or in the system range (Linux), no home. Supplementary membership is EXACTLY the
§1.2 table, derived per account by :func:`supplementary_groups`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from levain.firing.hands import HostOS, Step, _abs, _absent, _darwin_leave_groups, _nologin
from levain.slot import Refused
from levain.slot.ids import SINGLETONS, SLOT_NAME_RE, Allocation, is_levain_account

#: Stamped on every record this package creates. It must NOT start with the hands marker
#: (``levain.firing.hands.HANDS_MARKER``), so no hands undo ever matches one of ours; a test holds it.
SLOT_MARKER = "Levain session account"
SLOT_RETIRED_MARKER = {"darwin": "Levain session account (retired)", "linux": "Levain session account,retired"}

NO_HOME = {"darwin": "/var/empty", "linux": "/nonexistent"}

#: CONTRACT §1.2: the shared groups each singleton is in.
_SINGLETON_GROUPS: Mapping[str, tuple[str, ...]] = {
    "_levain_keys": ("levain-control", "levain-gwfronts"),
    "_levain_export": (),
    "_levain_control": ("levain-wake",),
    "_levain_qbroker": ("levain-control", "levain-queue"),
    "_levain_dispatch": ("levain-control", "levain-dispatch-export", "levain-lineage", "levain-queue"),
}
#: CONTRACT §1.2: the operator's shared groups.
OPERATOR_GROUPS = ("levain-control", "levain-export", "levain-queue")


def supplementary_groups(name: str) -> tuple[str, ...]:
    """The shared groups an account is in, beyond its own primary group (CONTRACT §1.2)."""
    if name in _SINGLETON_GROUPS:
        return _SINGLETON_GROUPS[name]
    m = SLOT_NAME_RE.fullmatch(name)
    if m is None or not is_levain_account(name):
        raise Refused("not-levain-name", repr(name))
    return ("levain-gwfronts",) if m.group(1) == "gw" else ("levain-slots",)


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    groups: tuple[str, ...]

    @property
    def gid(self) -> int:
        return self.uid


@dataclass(frozen=True)
class Group:
    name: str
    gid: int
    members: tuple[str, ...]


def accounts_of(alloc: Allocation) -> tuple[Account, ...]:
    return tuple(Account(n, uid, supplementary_groups(n)) for n, uid in alloc.ids.items())


def group_members(accounts: Iterable[Account], operator: str | None) -> dict[str, tuple[str, ...]]:
    """Shared group -> its members among ``accounts`` (+ the operator)."""
    out: dict[str, list[str]] = {}
    for a in accounts:
        for g in a.groups:
            out.setdefault(g, []).append(a.name)
    if operator is not None:
        for g in OPERATOR_GROUPS:
            out.setdefault(g, []).append(operator)
    return {g: tuple(sorted(m)) for g, m in out.items()}


def shared_groups_of(alloc: Allocation, operator: str) -> tuple[Group, ...]:
    members = group_members(accounts_of(alloc), operator)
    return tuple(Group(g, gid, members.get(g, ())) for g, gid in alloc.shared.items())


_DSCL = ("/usr/bin/dscl", ".")


def _darwin_group(name: str, gid: int, why: str) -> list[Step]:
    g = f"/Groups/{name}"
    return [
        Step(f"create the group {name} ({why})", (*_DSCL, "-create", g)),
        Step(f"give {name} its id", (*_DSCL, "-create", g, "PrimaryGroupID", str(gid))),
        Step(f"label {name}", (*_DSCL, "-create", g, "RealName", SLOT_MARKER)),
        Step(f"hide {name}", (*_DSCL, "-create", g, "IsHidden", "1")),
    ]


def account_steps(host: HostOS, accounts: Sequence[Account]) -> list[Step]:
    """Create each account and its own primary group (gid == uid)."""
    steps: list[Step] = []
    for a in accounts:
        if not is_levain_account(a.name):
            raise Refused("not-levain-name", repr(a.name))
        if host == "darwin":
            u = f"/Users/{a.name}"
            steps += _darwin_group(a.name, a.gid, "its own primary group")
            steps += [
                Step(f"create the account {a.name}", (*_DSCL, "-create", u)),
                Step(f"give {a.name} its user id", (*_DSCL, "-create", u, "UniqueID", str(a.uid))),
                Step(f"make {a.name}'s own group its primary group",
                     (*_DSCL, "-create", u, "PrimaryGroupID", str(a.gid))),
                Step(f"give {a.name} no login shell", (*_DSCL, "-create", u, "UserShell", "/usr/bin/false")),
                Step(f"give {a.name} no home", (*_DSCL, "-create", u, "NFSHomeDirectory", NO_HOME[host])),
                Step(f"label {a.name}", (*_DSCL, "-create", u, "RealName", SLOT_MARKER)),
                Step(f"hide {a.name} from the login window", (*_DSCL, "-create", u, "IsHidden", "1")),
                Step(f"give {a.name} no password", (*_DSCL, "-create", u, "Password", "*")),
                Step(f"disable {a.name} for login",
                     (*_DSCL, "-create", u, "AuthenticationAuthority", ";DisabledUser;")),
            ]
        elif host == "linux":
            steps += [
                # Skipped when present: a setup interrupted after this step re-runs (CONTRACT §23); the
                # planner kept such an account at the id it already holds.
                Step(f"create {a.name}'s own primary group",
                     (_abs("groupadd"), "--system", "--gid", str(a.gid), a.name),
                     skip_if=(_abs("getent"), "group", a.name)),
                Step(f"create the account {a.name} (system range, no home, no login shell)",
                     (_abs("useradd"), "--system", "--uid", str(a.uid), "--gid", str(a.gid),
                      "--no-create-home", "--home-dir", NO_HOME[host], "--shell", _nologin(),
                      "--comment", SLOT_MARKER, a.name), skip_if=(_abs("getent"), "passwd", a.name)),
                Step(f"lock {a.name}'s password", (_abs("usermod"), "--lock", a.name)),
            ]
        else:
            raise Refused("unsupported-host", repr(host))
    return steps


def revive_steps(host: HostOS, accounts: Sequence[Account]) -> list[Step]:
    """Bring a singleton tombstone back at its recorded id (CONTRACT §20): relabel it Levain's live
    marker, keep it with no login shell, no home and its password locked, and lift the expiry the
    tombstone set. Its own primary group was never removed. Supplementary groups are re-added by
    :func:`membership_steps`, as for a fresh account."""
    steps: list[Step] = []
    for a in accounts:
        if a.name not in SINGLETONS:
            raise Refused("not-levain-name", f"{a.name!r} is not a singleton; slot tombstones are never revived")
        if host == "darwin":
            # Every dscl -create sets the attribute whether or not the record exists: the same steps as a
            # fresh account, at the recorded id, relabel the tombstone and leave nothing of it behind.
            steps += [Step(f"revive {a.name} at its recorded id {a.uid}: {s.why}", s.argv)
                      for s in account_steps(host, [a])]
        elif host == "linux":
            # The live label goes on LAST: an account carrying it is one setup keeps as finished (§23), so a
            # revive interrupted before it still reads as a tombstone and is revived again, expiry included.
            steps += [
                Step(f"revive {a.name}: lift the tombstone's expiry", (_abs("chage"), "--expiredate", "-1", a.name)),
                Step(f"revive {a.name} at its recorded id {a.uid}: no login shell, no home, locked, relabelled (last)",
                     (_abs("usermod"), "--shell", _nologin(), "--home", NO_HOME[host], "--lock",
                      "--comment", SLOT_MARKER, a.name)),
            ]
        else:
            raise Refused("unsupported-host", repr(host))
    return steps


def shared_group_steps(host: HostOS, groups: Sequence[Group]) -> list[Step]:
    steps: list[Step] = []
    for g in groups:
        if host == "darwin":
            steps += _darwin_group(g.name, g.gid, "shared")
        else:
            steps.append(Step(f"create the shared group {g.name}",
                              (_abs("groupadd"), "--system", "--gid", str(g.gid), g.name),
                              skip_if=(_abs("getent"), "group", g.name)))
    return steps


def membership_steps(host: HostOS, pairs: Iterable[tuple[str, str]]) -> list[Step]:
    """Add each ``(user, group)`` membership."""
    steps: list[Step] = []
    for user, group in pairs:
        if host == "darwin":
            argv = ("/usr/sbin/dseditgroup", "-o", "edit", "-a", user, "-t", "user", group)
        else:
            argv = (_abs("usermod"), "--append", "--groups", group, user)
        steps.append(Step(f"put {user} in {group}", argv))
    return steps


def membership_pairs(accounts: Iterable[Account], operator: str | None) -> list[tuple[str, str]]:
    pairs = [(a.name, g) for a in accounts for g in a.groups]
    if operator is not None:
        pairs += [(operator, g) for g in OPERATOR_GROUPS]
    return pairs


def tombstone_steps(host: HostOS, name: str) -> list[Step]:
    """Retire an account in place: its id stays reserved, it can never log in, it is in no group but
    its own (CONTRACT §1.1 'never reissued'). The retired label is the LAST act: until it is on, the
    account still reads as live, so an undo interrupted part-way tombstones it again from the start."""
    if not is_levain_account(name):
        raise Refused("not-levain-name", repr(name))
    if host == "darwin":
        u = f"/Users/{name}"
        return [
            Step(f"tombstone {name}: no login shell", (*_DSCL, "-create", u, "UserShell", "/usr/bin/false")),
            Step(f"tombstone {name}: no password", (*_DSCL, "-create", u, "Password", "*")),
            Step(f"tombstone {name}: disabled",
                 (*_DSCL, "-create", u, "AuthenticationAuthority", ";DisabledUser;")),
            Step(f"tombstone {name}: hidden", (*_DSCL, "-create", u, "IsHidden", "1")),
            Step(f"tombstone {name}: no home", (*_DSCL, "-create", u, "NFSHomeDirectory", NO_HOME[host])),
            Step(f"take {name} out of every group", call=lambda n=name: _darwin_leave_groups(n)),
            Step(f"tombstone {name}: labelled (last)", (*_DSCL, "-create", u, "RealName", SLOT_RETIRED_MARKER[host])),
        ]
    return [
        Step(f"tombstone {name}: locked, no login shell, no home, no other groups",
             (_abs("usermod"), "--lock", "--shell", _nologin(), "--home", NO_HOME[host], "--groups", "", name)),
        Step(f"tombstone {name}: expired", (_abs("chage"), "--expiredate", "0", name)),
        Step(f"tombstone {name}: labelled (last)", (_abs("usermod"), "--comment", SLOT_RETIRED_MARKER[host], name)),
    ]


def delete_group_steps(host: HostOS, names: Iterable[str]) -> list[Step]:
    steps = []
    for g in names:
        argv = (*_DSCL, "-delete", f"/Groups/{g}") if host == "darwin" else (_abs("groupdel"), g)
        # Skipped when already gone, so a failed undo re-runs (CONTRACT §23).
        steps.append(Step(f"delete the shared group {g}", argv, skip_if=_absent("group", host, g)))
    return steps


__all__ = ["Account", "Group", "OPERATOR_GROUPS", "SINGLETONS", "SLOT_MARKER", "SLOT_RETIRED_MARKER",
           "accounts_of", "account_steps", "delete_group_steps", "group_members", "membership_pairs",
           "membership_steps", "revive_steps", "shared_group_steps", "shared_groups_of", "supplementary_groups",
           "tombstone_steps"]
