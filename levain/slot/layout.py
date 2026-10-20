"""The store and run-directory layout the install helper makes (CONTRACT §2.1 rows written by
``install``, §2.2 dirs), as data rows plus the Steps that create them.

A cell that names only an owner means owner : that owner's own primary group (root : the host's
gid-0 group). A directory is made, then chowned, then chmodded, so the mode (setgid included) is
set last and no chown can clear it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused
from levain.slot.ids import SlotId

STORE: Mapping[str, Path] = {"darwin": Path("/private/var/levain"), "linux": Path("/var/levain")}    # §2.1
RUN: Mapping[str, Path] = {"darwin": Path("/private/var/run/levain"), "linux": Path("/run/levain")}  # §2.2
IMPORT_TREE: Mapping[str, Path] = {"darwin": Path("/usr/local/lib/levain-root"),
                                   "linux": Path("/usr/lib/levain-root")}                          # §2.1
HELPER_DIR: Mapping[str, Path] = {"darwin": Path("/usr/local/libexec/levain"),
                                  "linux": Path("/usr/libexec/levain")}                            # §2.1
ROOT_GROUP = {"darwin": "wheel", "linux": "root"}

RESERVE_BYTES = 1_342_177_280          # CONTRACT §11 V6
_MIB = 1 << 20


def runtime_helper_path(host: HostOS) -> Path:
    return HELPER_DIR[host] / "runtime-helper"


def install_helper_path(host: HostOS) -> Path:
    return HELPER_DIR[host] / "install-helper"


def operator_record_path(host: HostOS) -> Path:
    return STORE[host] / "policy" / "operator"   # CONTRACT header scope line


@dataclass(frozen=True)
class Row:
    path: Path
    owner: str
    group: str
    mode: int
    kind: Literal["dir", "file"] = "dir"


def _root(host: HostOS, path: Path, mode: int, kind: Literal["dir", "file"] = "dir") -> Row:
    return Row(path, "root", ROOT_GROUP[host], mode, kind)


def base_store_rows(host: HostOS) -> list[Row]:
    """The store rows ``install`` writes that do not depend on the slots (CONTRACT §2.1)."""
    L = STORE[host]
    return [
        _root(host, L, 0o755),
        _root(host, L / "live", 0o711),
        _root(host, L / "alloc", 0o700),
        _root(host, L / "slots", 0o755),
        _root(host, L / "slots" / "pool.lock", 0o600, "file"),
        _root(host, L / "staging", 0o711),
        _root(host, L / "export", 0o755),
        Row(L / "export" / "operator", "_levain_export", "levain-export", 0o2750),
        Row(L / "export" / "dispatch", "_levain_export", "levain-dispatch-export", 0o2750),
        _root(host, L / "policy", 0o755),
        Row(L / "keys", "_levain_keys", "_levain_keys", 0o700),
        _root(host, L / "routes", 0o755),
    ]


def policy_rows(host: HostOS) -> list[Row]:
    """What ``setup`` needs before it writes the operator record: the store root and policy dir."""
    return [r for r in base_store_rows(host) if r.path in (STORE[host], STORE[host] / "policy")]


def slot_store_rows(host: HostOS, slot: SlotId) -> list[Row]:
    """``L/slots/{e,v}<n>/`` and its lock (CONTRACT §2.1)."""
    return [_root(host, slot.slot_dir(STORE[host]), 0o755), _root(host, slot.lock_path(STORE[host]), 0o600, "file")]


def base_run_rows(host: HostOS) -> list[Row]:
    """The run-dir rows that do not depend on the slots (CONTRACT §2.2)."""
    R = RUN[host]
    return [
        _root(host, R, 0o755),
        _root(host, R / "gw", 0o755),
        _root(host, R / "qb", 0o755),
        _root(host, R / "planner", 0o755),
        Row(R / "keys", "_levain_keys", "levain-gwfronts", 0o750),
        Row(R / "control", "_levain_control", "levain-control", 0o750),
        Row(R / "reaper", "root", "levain-wake", 0o750),
    ]


def slot_run_rows(host: HostOS, slot: SlotId) -> list[Row]:
    """The per-slot socket dirs, egress class only (CONTRACT §1.1, §2.2): setgid, group the slot's own
    primary group, owned by the server that binds there."""
    if slot.slot_class != "egress":
        return []
    R, n, grp = RUN[host], slot.index, slot.primary
    return [
        Row(R / "gw" / f"slot{n}", f"_levain_gw{n}", grp, 0o2750),
        Row(R / "qb" / f"slot{n}", "_levain_qbroker", grp, 0o2750),
        Row(R / "planner" / f"slot{n}", "_levain_dispatch", grp, 0o2750),
    ]


def slot_rows(host: HostOS, slots: Iterable[SlotId]) -> list[Row]:
    out: list[Row] = []
    for s in slots:
        out += slot_store_rows(host, s) + slot_run_rows(host, s)
    return out


def _tools(host: HostOS) -> dict[str, str]:
    if host == "darwin":
        return {"mkdir": "/bin/mkdir", "chown": "/usr/sbin/chown", "chmod": "/bin/chmod", "test": "/bin/test",
                "dd": "/bin/dd"}
    if host == "linux":
        return {t: _abs(t) for t in ("mkdir", "chown", "chmod", "test", "dd")}
    raise Refused("unsupported-host", repr(host))


def row_steps(host: HostOS, rows: Iterable[Row]) -> list[Step]:
    t = _tools(host)
    steps: list[Step] = []
    for r in rows:
        p = str(r.path)
        if r.kind == "dir":
            steps.append(Step(f"create {p}", (t["mkdir"], p), skip_if=(t["test"], "-d", p)))
        else:
            steps.append(Step(f"create {p}", write=(r.path, "", r.mode)))
        steps += [
            Step(f"own {p} as {r.owner}:{r.group}", (t["chown"], f"{r.owner}:{r.group}", p)),
            Step(f"set {p} to {r.mode:04o}", (t["chmod"], f"{r.mode:04o}", p)),
        ]
    return steps


def reserve_row(host: HostOS) -> Row:
    return _root(host, STORE[host] / "reserve", 0o600, "file")


def reserve_steps(host: HostOS) -> list[Step]:
    """``L/reserve``: written zeros, never fallocate (CONTRACT §7.5)."""
    t, r = _tools(host), reserve_row(host)
    return [
        Step(f"write {r.path} as zeros", (t["dd"], "if=/dev/zero", f"of={r.path}", f"bs={_MIB}",
                                         f"count={RESERVE_BYTES // _MIB}")),
        Step(f"own {r.path} as root", (t["chown"], f"{r.owner}:{r.group}", str(r.path))),
        Step(f"set {r.path} to {r.mode:04o}", (t["chmod"], f"{r.mode:04o}", str(r.path))),
    ]
