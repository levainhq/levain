"""Account and group plans per OS (CONTRACT §1.1, §1.2), as :class:`~levain.firing.hands.Step` lists.

Every account: a primary group of its own name with ``gid == uid``, no login shell, password locked,
hidden (macOS) or in the system range (Linux). Its home is ``NO_HOME``, except on macOS the accounts that exec a
vendor CLI (:func:`vendor_home_of`), whose home is root's empty ``L/vendorhome``. Supplementary membership is EXACTLY
the §1.2 table, derived per account by :func:`supplementary_groups`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from levain.firing.hands import HostOS, Step, _abs, _absent, _darwin_leave_groups, _nologin
from levain.slot import Refused
from levain.slot.ids import SINGLETONS, SLOT_NAME_RE, Allocation, SlotId, is_levain_account
from levain.slot.layout import vendor_home

if TYPE_CHECKING:
    from levain.slot.removal import Live

#: Stamped on every record this package creates. It must NOT start with the hands marker
#: (``levain.firing.hands.HANDS_MARKER``), so no hands undo ever matches one of ours; a test holds it.
SLOT_MARKER = "Levain session account"
SLOT_RETIRED_MARKER = {"darwin": "Levain session account (retired)", "linux": "Levain session account,retired"}

NO_HOME = {"darwin": "/var/empty", "linux": "/nonexistent"}

#: The slot-name prefixes of the accounts a session execs a vendor CLI as: an egress slot's ``_levain_s<n>`` and a
#: verifier pair's child ``_levain_c<n>`` (``ids._PREFIXES``). Fronts and drivers never exec one.
VENDOR_PREFIXES = ("s", "c")


def vendor_runs_as(name: str) -> bool:
    """Whether a session execs a vendor CLI as ``name`` (:data:`VENDOR_PREFIXES`)."""
    m = SLOT_NAME_RE.fullmatch(name)
    return m is not None and is_levain_account(name) and m.group(1) in VENDOR_PREFIXES


def vendor_home_of(host: HostOS, name: str) -> str | None:
    """The home ``name``'s directory record holds when it is not ``NO_HOME``: on macOS, ``layout.vendor_home`` for an
    account a vendor runs as (R7: Codex's managed-preferences sync fails with a ``/var/empty`` home, and a ChatGPT
    login's ``account/read`` then fails -32603). None for every other account and on Linux."""
    home = vendor_home(host)
    return str(home) if home is not None and vendor_runs_as(name) else None


def home_of(host: HostOS, name: str) -> str:
    return vendor_home_of(host, name) or NO_HOME[host]

#: CONTRACT §1.2: the shared groups each singleton is in. A primary group stays its account's only: keys gives its
#: admin socket a group itself (not root), which only a member may do, so that socket's group is the shared
#: levain-keysadmin, whose members are keys and CONTROL, the socket's one admitted peer.
_SINGLETON_GROUPS: Mapping[str, tuple[str, ...]] = {
    "_levain_keys": ("levain-control", "levain-gwfronts", "levain-keysadmin"),
    "_levain_export": (),
    "_levain_control": ("levain-keysadmin", "levain-wake"),
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
                Step(f"give {a.name} the root-owned vendor home" if vendor_home_of(host, a.name)
                     else f"give {a.name} no home", (*_DSCL, "-create", u, "NFSHomeDirectory", home_of(host, a.name))),
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


#: ``$1`` = the account. pkill exits 1 when no process matched: a user with no cfprefsd running has nothing to re-read,
#: so that is success; 2 (bad options) and 3 (internal error) stay failures.
_END_CFPREFSD = '/usr/bin/pkill -u "$1" -x cfprefsd; [ "$?" -le 1 ]'


def vendor_home_steps(host: HostOS, slots: Iterable[SlotId]) -> list[Step]:
    """Repair's re-point (R8, after the rows that make ``L/vendorhome``): each in-service slot's vendor-running account
    (:func:`vendor_runs_as`) gets the home :func:`account_steps` gives a fresh one, then the directory cache is
    flushed once and each such account's cfprefsd is ended, so the next session resolves the new home. Every step is
    idempotent: an account already pointed is pointed again. It changes one attribute of accounts repair has already
    proved live (``install_helper._check_rendered_accounts``); repair still creates and removes none. Linux: none."""
    if vendor_home(host) is None:
        return []
    names = [n for s in slots for n in s.accounts if vendor_runs_as(n)]
    if not names:
        return []
    steps = [Step(f"point {n}'s home at the root-owned vendor home",
                  (*_DSCL, "-create", f"/Users/{n}", "NFSHomeDirectory", home_of(host, n))) for n in names]
    steps.append(Step("flush the directory cache, so the new homes are read", ("/usr/bin/dscacheutil", "-flushcache")))
    steps += [Step(f"end {n}'s cfprefsd, so it re-reads the home (none running is success)",
                   ("/bin/sh", "-c", _END_CFPREFSD, "sh", n)) for n in names]
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


@dataclass(frozen=True)
class LeaveGroupsStep(Step):
    """The macOS tombstone's "out of every group" act: the one callback :func:`tombstone_steps` emits, marked by its
    type so :func:`remove_account_steps` routes it to its live body by type, never by matching its ``why`` text."""


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
            LeaveGroupsStep(f"take {name} out of every group", call=lambda n=name: _darwin_leave_groups(n)),
            Step(f"tombstone {name}: labelled (last)", (*_DSCL, "-create", u, "RealName", SLOT_RETIRED_MARKER[host])),
        ]
    return [
        Step(f"tombstone {name}: locked, no login shell, no home, no other groups",
             (_abs("usermod"), "--lock", "--shell", _nologin(), "--home", NO_HOME[host], "--groups", "", name)),
        Step(f"tombstone {name}: expired", (_abs("chage"), "--expiredate", "0", name)),
        Step(f"tombstone {name}: labelled (last)", (_abs("usermod"), "--comment", SLOT_RETIRED_MARKER[host], name)),
    ]


__all__ = ["Account", "Group", "LeaveGroupsStep", "OPERATOR_GROUPS", "SINGLETONS", "SLOT_MARKER", "SLOT_RETIRED_MARKER",
           "VENDOR_PREFIXES", "accounts_of", "account_steps", "group_members", "home_of", "membership_pairs",
           "membership_steps", "revive_steps", "shared_group_steps", "shared_groups_of", "supplementary_groups",
           "tombstone_steps", "vendor_home_of", "vendor_home_steps", "vendor_runs_as"]


# --- row 33 R1b: removal by the RECORDED identity (§1.1 table rows 3-5) -------------------------------------------


def _directory(host: HostOS, live: "Live"):   # -> install_helper.Directory
    import subprocess

    from levain.slot.install_helper import read_directory
    from levain.slot.removal import ReadFailed

    try:
        return read_directory(host, run=live.timed)
    except Refused as e:
        raise ReadFailed(f"{e.name}: {e.detail}") from None
    except subprocess.TimeoutExpired:
        raise ReadFailed(f"the directory read timed out after {live.timeout:g}s") from None


def _directory_tools(host: HostOS) -> tuple[str, ...]:
    """The programs :func:`_directory` runs (``read_directory``'s)."""
    return ("/usr/bin/dscl",) if host == "darwin" else (_abs("getent"),)


_DSEDITGROUP = "/usr/sbin/dseditgroup"


def _group_ours(host: HostOS, d, name: str, gid: int) -> str:
    from levain.slot.removal import ABSENT, NOT_OURS, OURS

    if name not in d.groups:
        return ABSENT
    if d.groups[name] != gid or (host == "darwin" and d.group_labels.get(name) != SLOT_MARKER):
        return NOT_OURS
    return OURS


def member_state(host: HostOS, account: str, group: str, gid: int, live: "Live") -> str:
    """OURS: the group is present at the RECORDED gid (macOS: under Levain's label) and lists the account. NOT_OURS:
    the name at another gid or label. ABSENT: no such group, or the account is not listed."""
    from levain.slot.removal import ABSENT, OURS

    d = _directory(host, live)
    state = _group_ours(host, d, group, gid)
    if state != OURS:
        return state
    return OURS if account in d.group_members.get(group, frozenset()) else ABSENT


def remove_member_steps(host: HostOS, account: str, group: str, gid: int, *, live: "Live") -> list[Step]:
    """``member/1`` (R7): macOS ``dseditgroup -o edit -d``, Linux ``gpasswd -d``."""
    from levain.slot.removal import chain, point, remove_step

    argv = ((_DSEDITGROUP, "-o", "edit", "-d", account, "-t", "user", group) if host == "darwin"
            else (_abs("gpasswd"), "-d", account, group))
    return [remove_step(f"take {account} out of {group}", ("member/1", (account, group, gid)), live,
                        lambda: member_state(host, account, group, gid, live),
                        chain(lambda: live.act(argv), point(live, "member/1:after-remove")),
                        tools=(*_directory_tools(host), argv[0]))]


def group_state(host: HostOS, name: str, gid: int, live: "Live") -> str:
    return _group_ours(host, _directory(host, live), name, gid)


def remove_group_steps(host: HostOS, name: str, gid: int, *, live: "Live", retire: bool = True,
                       record_held: bool = True) -> list[Step]:
    """``group/1`` (R7, after every ``member/1``). The gid rule (§1.1 g): the RECORDED gid is retired DURABLY
    (:func:`levain.slot.ids.retire_ids_durably`) BEFORE ``dscl -delete`` / ``groupdel``, for OURS (then deleted),
    NOT_OURS (left alone) and ABSENT when ``record_held`` (a completed run had it, so rows may carry it); an ABSENT
    group the write-ahead journal alone held was never made and is dropped unretired. ``retire=False``: undo."""
    from levain.slot.ids import RETIRED_IDS_PATH, retire_ids_durably
    from levain.slot.removal import ABSENT, NOT_OURS, OURS, ReadFailed, RemoveStep

    record = ("group/1", (name, gid))
    argv = (*_DSCL, "-delete", f"/Groups/{name}") if host == "darwin" else (_abs("groupdel"), name)

    def go() -> tuple[bool, str]:
        try:
            state = group_state(host, name, gid, live)
        except ReadFailed as e:
            return False, f"group/1: the live read failed, record kept: {e}"
        if retire and (state != ABSENT or record_held):
            try:
                retire_ids_durably(live.at(RETIRED_IDS_PATH[host]), {gid}, fsync=live.fsync)
            except OSError as e:
                return False, f"could not retire gid {gid} durably: {e.strerror or e}"
            live.fault("group/1:after-retire")
        if state == NOT_OURS:
            live.skips.add(record)
            return True, f"the group {name} is present and not Levain's: left in place (install-skips)"
        if state != OURS:
            return True, ""
        ok, why = live.act(argv)
        if ok:
            live.fault("group/1:after-delete")
        return ok, why

    return [RemoveStep(f"delete the group {name} (its gid {gid} retired first)" if retire else f"delete the group {name}",
                       call=go, tools=(*_directory_tools(host), argv[0]))]


def account_state(host: HostOS, name: str, uid: int, live: "Live") -> str:
    """OURS: present at the recorded uid with Levain's LIVE label. ABSENT: no account, or it carries the retired
    label. NOT_OURS: the name at another uid or under a foreign label."""
    return _account_state_in(host, _directory(host, live), name, uid)


def _account_state_in(host: HostOS, d, name: str, uid: int) -> str:
    from levain.slot.removal import ABSENT, NOT_OURS, OURS

    if name not in d.users:
        return ABSENT
    label = d.user_labels.get(name, "")
    if d.users[name] != uid:
        return NOT_OURS
    if label == SLOT_RETIRED_MARKER[host]:
        return ABSENT
    return OURS if label == SLOT_MARKER else NOT_OURS


def uid_retires(name: str, uid: int, *, users: Mapping[str, int], levain: Mapping[str, int]) -> bool:
    """THE rule for whether a recorded ``account/1`` (``name``, ``uid``) has its uid retired: undo's plan
    (``install_helper._uid_is_undos``, from ``HostFacts``) and repair's account step (the live directory, at the act)
    both call this, and nothing else decides it. ``users``: every user on the host, name -> uid, any label;
    ``levain``: the users Levain holds as its own (its live label, its tombstone, or for undo journal-present), name ->
    uid. Retired: the name present at the uid and Levain's, or the name ABSENT (deleted by hand, say, while files it
    owned remain: the uid must never be reissued; L3 r2 codex HIGH) and no other account at the uid. Left: the name at
    another uid or not Levain's, or another account holding the uid (FIX1 L1)."""
    have = users.get(name)
    if have is not None:
        return have == uid and levain.get(name) == uid
    return uid not in users.values()


def remove_account_steps(host: HostOS, name: str, uid: int, *, live: "Live",
                         end: "Callable[[int, str], tuple[bool, str]]", retire: bool = True) -> list[Step]:
    """``account/1`` (R7): the uid retired durably (``retire``; undo passes False and retires uids itself), every process ended (``end``: the caller's proven end), then the
    tombstone (never a delete). Each act re-reads the live label first, so a run killed inside the tombstone re-runs
    it from its first act while the label is still live. Every act goes through ``live``: the macOS tombstone's
    "out of every group" is read and acted here, never :func:`levain.firing.hands._darwin_leave_groups` (the host's
    own ``subprocess``)."""
    from levain.slot.ids import RETIRED_IDS_PATH, retire_ids_durably
    from levain.slot.removal import ABSENT, NOT_OURS, ReadFailed, RemoveStep, remove_step

    record = ("account/1", (name, uid))
    classify = lambda: account_state(host, name, uid, live)   # noqa: E731
    reads = _directory_tools(host)

    def leave_groups() -> tuple[bool, str]:
        try:
            d = _directory(host, live)
        except ReadFailed as e:
            return False, str(e)
        for g in sorted(g for g, members in d.group_members.items() if name in members):
            ok, why = live.act((_DSEDITGROUP, "-o", "edit", "-d", name, "-t", "user", g))
            if not ok:
                return False, why
        return True, ""

    def retire_then_end() -> tuple[bool, str]:
        # The retire is decided from the live read, BEFORE the outcome gates the act (as group/1's gid): an ABSENT
        # account and our tombstone at the uid are retired too, by :func:`uid_retires` (r1b-fix3).
        try:
            d = _directory(host, live)
        except ReadFailed as e:
            return False, f"account/1: the live read failed, record kept: {e}"
        levain = {n: u for n, u in d.users.items()
                  if d.user_labels.get(n, "") in (SLOT_MARKER, SLOT_RETIRED_MARKER[host])}
        if retire and uid_retires(name, uid, users=d.users, levain=levain):
            try:
                retire_ids_durably(live.at(RETIRED_IDS_PATH[host]), {uid}, fsync=live.fsync)
            except OSError as e:
                return False, f"could not retire uid {uid} durably: {e.strerror or e}"
            live.fault("account/1:after-retire")
        state = _account_state_in(host, d, name, uid)
        if state == NOT_OURS:
            live.skips.add(record)
            return True, f"account/1 {record[1]!r} is present and not Levain's: left in place (install-skips)"
        if state == ABSENT:
            return True, ""
        return end(uid, name)

    first = f"retire {name}'s uid {uid}, then stop every process of {name}" if retire else \
        f"stop every process of {name}, and check they are gone"
    steps: list[Step] = [RemoveStep(first, call=retire_then_end, tools=reads)]
    for i, s in enumerate(tombstone_steps(host, name)):
        if s.call is not None and not isinstance(s, LeaveGroupsStep):   # a new callback needs a live body here
            raise AssertionError(f"tombstone step {s.why!r} has a callback remove_account_steps cannot route")
        body = leave_groups if s.call is not None else (lambda s=s: live.act(s.argv))
        tools = (*reads, _DSEDITGROUP) if s.call is not None else (*reads, s.argv[0])

        def act(body: "Callable[[], tuple[bool, str]]" = body, first: bool = i == 0) -> tuple[bool, str]:
            ok, why = body()
            if ok and first:
                live.fault("account/1:after-first-act")
            return ok, why
        steps.append(remove_step(s.why, record, live, classify, act, tools=tools))
    return steps
