"""Account ids, names, slot identity and capacity (CONTRACT §1.1).

Pure: the ids in use, the retired ids and the names present on the host are parameters. Every
account gets ``gid == uid``; allocation takes the highest id free in its range as both. A retired id
(the retired-ids file) is never reissued, and an index whose name still exists on the host (a
tombstone) is never reissued either.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Mapping

from levain.firing.hands import HostOS
from levain.slot import Refused
from levain.slot.grammar import INDEX_SPACE, MAX_SLOTS, MAX_VERIFIER_SLOTS

SlotClass = Literal["egress", "none"]


@dataclass(frozen=True)
class IdRanges:
    slots: range          # slot + front + verifier-pair ids, top-down
    singletons: range     # the singleton service users
    shared: range         # the shared groups of §1.2
    capacity: int         # the bound on 2N + 2M + tombstones


RANGES: Mapping[str, IdRanges] = {   # CONTRACT §1.1 table
    "linux": IdRanges(range(899, 799, -1), range(799, 789, -1), range(789, 779, -1), 100),
    "darwin": IdRanges(range(3999, 3799, -1), range(3799, 3789, -1), range(3789, 3779, -1), 200),
}

RETIRED_IDS_PATH: Mapping[str, Path] = {   # CONTRACT §1.1
    "darwin": Path("/var/db/levain/retired-session-ids"),
    "linux": Path("/var/lib/levain/retired-session-ids"),
}

SINGLETONS = ("_levain_keys", "_levain_export", "_levain_control", "_levain_qbroker", "_levain_dispatch")

SHARED_GROUPS = ("levain-slots", "levain-gwfronts", "levain-control", "levain-wake", "levain-lineage",
                 "levain-export", "levain-dispatch-export", "levain-queue")   # CONTRACT §1.2

_PREFIXES: Mapping[str, tuple[str, str]] = {"egress": ("s", "gw"), "none": ("v", "c")}
_TAG = {"egress": "e", "none": "v"}

SLOT_NAME_RE = re.compile(r"_levain_(s|gw|v|c)(0|[1-9][0-9]?)")


@dataclass(frozen=True, order=True)
class SlotId:
    """A slot's identity is ``{class, index}``, never the bare index (CONTRACT §1.1)."""

    slot_class: SlotClass
    index: int

    def __post_init__(self) -> None:
        if self.slot_class not in _PREFIXES:
            raise Refused("bad-token", f"class: {self.slot_class!r}")
        if not isinstance(self.index, int) or isinstance(self.index, bool) or not 0 <= self.index < INDEX_SPACE:
            raise Refused("out-of-range", f"index {self.index!r}")

    @property
    def tag(self) -> str:
        return f"{_TAG[self.slot_class]}{self.index}"

    @property
    def accounts(self) -> tuple[str, str]:
        """egress: (slot, front); none: (driver, child)."""
        a, b = _PREFIXES[self.slot_class]
        return f"_levain_{a}{self.index}", f"_levain_{b}{self.index}"

    @property
    def primary(self) -> str:
        return self.accounts[0]

    def slot_dir(self, store: Path) -> Path:
        return store / "slots" / self.tag

    def lock_path(self, store: Path) -> Path:
        return self.slot_dir(store) / "lock"

    @property
    def netns(self) -> str:
        return f"levain-{self.tag}"

    @property
    def cgroup(self) -> str:
        return f"{self.tag}.scope"


def is_levain_account(name: str) -> bool:
    m = SLOT_NAME_RE.fullmatch(name)
    return name in SINGLETONS or (m is not None and int(m.group(2)) < INDEX_SPACE)


def read_retired_ids(path: Path) -> frozenset[int]:
    """The retired-ids file: one decimal id per line; absent = none retired."""
    try:
        text = Path(path).read_text(encoding="ascii")
    except FileNotFoundError:
        return frozenset()
    except UnicodeDecodeError:
        raise Refused("retired-ids-malformed", str(path)) from None
    out: set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not re.fullmatch(r"0|[1-9][0-9]{0,9}", line):
            raise Refused("retired-ids-malformed", f"{path}: {line!r}")
        out.add(int(line))
    return frozenset(out)


def retired_ids_text(ids: Iterable[int]) -> str:
    return "".join(f"{i}\n" for i in sorted(set(ids)))


def write_retired_ids(path: Path, ids: Iterable[int]) -> None:
    """For tests and the apply layer only; the plan layer writes the file through a Step."""
    Path(path).write_text(retired_ids_text(ids), encoding="ascii")


def tombstones(host: HostOS, retired: Iterable[int]) -> int:
    return len(set(retired) & set(RANGES[host].slots))


def check_capacity(host: HostOS, slots: int, verifier_slots: int, retired: Iterable[int]) -> None:
    """CONTRACT §1.1: ``2N + 2M + tombstones`` within the host's capacity, and N, M within their caps."""
    if not 0 <= slots <= MAX_SLOTS or not 0 <= verifier_slots <= MAX_VERIFIER_SLOTS:
        raise Refused("out-of-range", f"slots {slots}, verifier slots {verifier_slots}")
    need = 2 * slots + 2 * verifier_slots + tombstones(host, retired)
    cap = RANGES[host].capacity
    if need > cap:
        raise Refused("capacity", f"2N + 2M + tombstones = {need}, over {cap}")


def spent_indices(slot_class: SlotClass, existing_names: Iterable[str]) -> frozenset[int]:
    """An index is spent when any of its class's names exists on the host (live or a tombstone)."""
    letters = _PREFIXES[slot_class]
    out = set()
    for name in existing_names:
        m = SLOT_NAME_RE.fullmatch(name)
        if m and m.group(1) in letters:
            out.add(int(m.group(2)))
    return frozenset(out)


def choose_indices(slot_class: SlotClass, count: int, spent: Iterable[int]) -> tuple[SlotId, ...]:
    """The lowest ``count`` unspent indices of the class."""
    spent = set(spent)
    free = [i for i in range(INDEX_SPACE) if i not in spent]
    if len(free) < count:
        raise Refused("index-space-exhausted", f"{slot_class}: {count} wanted, {len(free)} left")
    return tuple(SlotId(slot_class, i) for i in free[:count])


@dataclass(frozen=True)
class Allocation:
    host: HostOS
    egress: tuple[SlotId, ...]
    verifier: tuple[SlotId, ...]
    ids: Mapping[str, int] = field(default_factory=dict)      # account name -> uid (== gid)
    shared: Mapping[str, int] = field(default_factory=dict)   # shared group -> gid

    @property
    def slots(self) -> tuple[SlotId, ...]:
        return self.egress + self.verifier


def _take(pool: range, n: int, blocked: set[int], what: str) -> list[int]:
    got = [i for i in pool if i not in blocked][:n]
    if len(got) < n:
        raise Refused("id-range-exhausted", f"{what}: {n} wanted, {len(got)} free")
    blocked.update(got)
    return got


def allocate(host: HostOS, *, egress: Iterable[SlotId], verifier: Iterable[SlotId],
             used: Iterable[int], retired: Iterable[int],
             singletons: bool = False, shared: bool = False) -> Allocation:
    """Ids for the given slots (and, for ``setup``, the singletons and shared groups), highest free
    first, skipping every id in use (as a uid or a gid) and every retired id."""
    egress, verifier = tuple(egress), tuple(verifier)
    blocked = set(used) | set(retired)
    r = RANGES[host]
    names = [n for s in egress + verifier for n in s.accounts]
    ids = dict(zip(names, _take(r.slots, len(names), blocked, "slot accounts")))
    if singletons:
        ids.update(zip(SINGLETONS, _take(r.singletons, len(SINGLETONS), blocked, "singletons")))
    gids: dict[str, int] = {}
    if shared:
        gids = dict(zip(SHARED_GROUPS, _take(r.shared, len(SHARED_GROUPS), blocked, "shared groups")))
    return Allocation(host, egress, verifier, ids, gids)
