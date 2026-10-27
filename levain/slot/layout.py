"""The store and run-directory layout the install helper makes (CONTRACT §2.1 rows written by
``install``, §2.2 dirs), as data rows plus the Steps that create them.

A cell that names only an owner means owner : that owner's own primary group (root : the host's
gid-0 group). A directory is made, then chowned, then (macOS) its extended ACL cleared, then chmodded,
so the mode (setgid included) is set last and no chown can clear it.
"""
from __future__ import annotations

import functools
import os
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused
from levain.slot.ids import SlotId
from levain.slot.limits_doc import POLICY_LIMITS

STORE: Mapping[str, Path] = {"darwin": Path("/private/var/levain"), "linux": Path("/var/levain")}    # §2.1
RUN: Mapping[str, Path] = {"darwin": Path("/private/var/run/levain"), "linux": Path("/run/levain")}  # §2.2
IMPORT_TREE: Mapping[str, Path] = {"darwin": Path("/usr/local/lib/levain-root"),
                                   "linux": Path("/usr/lib/levain-root")}                          # §2.1
HELPER_DIR: Mapping[str, Path] = {"darwin": Path("/usr/local/libexec/levain"),
                                  "linux": Path("/usr/libexec/levain")}                            # §2.1
ROOT_GROUP = {"darwin": "wheel", "linux": "root"}

POOL_LOCK_NAME = "pool.lock"         # the one file install writes in L/slots/ (CONTRACT §2.1)
RESERVE_BYTES = 1_342_177_280          # CONTRACT §11 V6
_MIB = 1 << 20


def runtime_helper_path(host: HostOS) -> Path:
    return HELPER_DIR[host] / "runtime-helper"


def install_helper_path(host: HostOS) -> Path:
    return HELPER_DIR[host] / "install-helper"


def repair_line(host: HostOS) -> str:
    """The one command that re-applies setup's rows to an existing install (a bare ``repair``): the remedy a refusal
    or a doctor row names for a row an older install lacks."""
    return f"sudo {install_helper_path(host)} repair"


def service_entry_path(host: HostOS) -> Path:
    """The one bootstrap entry every service unit runs (CONTRACT §23), beside the helpers' entries."""
    return HELPER_DIR[host] / "service-entry"


def session_repo(host: HostOS, session: str) -> Path:
    """``<S>/repo``, the session's worktree, as a real path (§26 S1-c: a walk from ``/`` meets no link)."""
    return STORE[host] / "live" / session / "repo"


def dispatch_store_dir(host: HostOS) -> Path:
    """``L/dispatch``: the dispatch principal's own store (job state, CONTRACT §1 dispatch row), the job store's
    directory and the supervisor lock's (``levain.slot.services.dispatch``)."""
    return STORE[host] / "dispatch"


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


def base_store_rows(host: HostOS, operator: str) -> list[Row]:
    """Every store DIRECTORY row of CONTRACT §2.1 that does not depend on a slot or a session, with the
    owner, group and mode its row gives, whoever the row's "Written by" names (§20): the named writer
    writes the files inside. Plus ``L/slots/pool.lock``, the one file ``install`` writes here.

    Per-session rows (``<S>``, ``L/retained/<session>``, ``L/trash/<session>``, ``L/staging/<session>``,
    ``L/export/*/<session>``) are made by the verb that makes the session, not here. ``operator``
    owns ``L/queue/answers/`` (§2.1)."""
    L = STORE[host]
    return [
        _root(host, L, 0o755),
        _root(host, L / "live", 0o711),
        _root(host, L / "alloc", 0o700),
        Row(L / "sessions", "_levain_control", "_levain_control", 0o700),
        # §2.1: the per-front_uid registration counters (§4.9), CONTROL's like the session records beside them.
        Row(L / "sessions" / "front-reg", "_levain_control", "_levain_control", 0o700),
        _root(host, L / "slots", 0o755),
        _root(host, L / "slots" / POOL_LOCK_NAME, 0o600, "file"),
        _root(host, L / "retained", 0o700),
        _root(host, L / "trash", 0o700),
        _root(host, L / "staging", 0o711),
        _root(host, L / "export", 0o755),
        # CONTRACT §2.1: root : the class group, 0750. Root acts in these dirs (seal-in's stale delete and probe, the
        # publish transaction), so they are root's alone; the importer writes only below wall.EXPORT_WRITABLE.
        Row(L / "export" / "operator", "root", "levain-export", 0o750),
        Row(L / "export" / "dispatch", "root", "levain-dispatch-export", 0o750),
        # §2.1 gives no row for L/queue/ itself; root 0755 so the members of its two children reach them.
        _root(host, L / "queue", 0o755),
        # Both setgid: on Linux a new file takes its creator's primary group, so without it a row (the broker's
        # requests and receipts DB; an answer the cockpit / phone server writes) reaches levain-queue's members only
        # if its writer's primary group happens to be levain-queue.
        Row(L / "queue" / "requests", "_levain_qbroker", "levain-queue", 0o2750),
        Row(L / "queue" / "answers", operator, "levain-queue", 0o2770),
        # §26 S1-b: dispatch publishes each contract the operator signed here; setgid, so every file takes
        # levain-queue and the broker (a member) reads it; slots are not members.
        Row(L / "contracts", "_levain_dispatch", "levain-queue", 0o2750),
        # dispatch's job state and its supervisor lock: its own, 0700, the one shape ``JobStore`` opens (owned by the
        # opening uid, no group or other bit).
        Row(dispatch_store_dir(host), "_levain_dispatch", "_levain_dispatch", 0o700),
        _root(host, L / "policy", 0o755),
        # S1 3c-6 (H6-4): CONTROL's lineage sink. Setgid, so the sink CONTROL creates takes levain-lineage: on Linux a
        # new file takes its creator's primary group (_levain_control) otherwise, and _levain_dispatch could not read it.
        Row(L / "lineage", "_levain_control", "levain-lineage", 0o2750),
        Row(L / "keys", "_levain_keys", "_levain_keys", 0o700),
        # S1 3b-8a T1: the fronts' tunnel logs, one dir per egress slot below it (``slot_store_rows``).
        _root(host, L / "gw", 0o755),
        _root(host, L / "routes", 0o755),
        # S1 3c-4: CONTROL writes the route maps here, keys reads them. Setgid, so every file CONTROL creates takes
        # _levain_keys: on Linux a new file takes its creator's primary group (_levain_control) otherwise, and keys
        # could read none of them. Re-applied by every setup and repair (``row_steps``: the chown, then the mode).
        Row(L / "routes" / "dispatch", "_levain_control", "_levain_keys", 0o2750),
        Row(L / "routes" / "verifiers", "_levain_control", "_levain_control", 0o700),
    ]


#: Stands in for the operator's account name in :func:`store_dir_row`'s lookup: the one row it owns is not root's,
#: which is all the trust check reads.
_ANY_OPERATOR = "<operator>"


def admits_root(st: os.stat_result) -> bool:
    """The trust root's store handle demands of a directory it acts below (a root row, or a per-session / per-slot dir
    the verb that makes it gives to root): root-owned, not group- or other-writable. Every root directory row's mode
    has no group or other write bit (tests/test_slot_layout_trust.py), so this IS each root row's own shape."""
    return st.st_uid == 0 and not st.st_mode & 0o022


def store_dir_row(host: HostOS, parts: Iterable[str]) -> "Row | None":
    """The :func:`base_store_rows` directory row of ``L/<parts>``, or None for a dir no base row makes (a session's or
    a slot's, so a trusted open of one demands the root shape; the one :func:`slot_store_rows` row that is not root's,
    an egress slot's ``L/gw/slot<n>/``, is never opened trusted: tests/test_slot_layout_trust.py)."""
    return _store_dir_rows(host).get(tuple(parts))


@functools.lru_cache(maxsize=None)
def _store_dir_rows(host: HostOS) -> Mapping[tuple, Row]:
    """:func:`base_store_rows`' directory rows by their parts below ``L``, built once per host (the helper looks one
    up for every component of every trusted open)."""
    L = STORE[host]
    return types.MappingProxyType({r.path.relative_to(L).parts: r for r in base_store_rows(host, _ANY_OPERATOR)
                                   if r.kind == "dir"})


def policy_rows(host: HostOS) -> list[Row]:
    """What ``setup`` needs before it writes the operator record: the store root and policy dir."""
    L = STORE[host]
    return [_root(host, L, 0o755), _root(host, L / "policy", 0o755)]


def slot_store_rows(host: HostOS, slot: SlotId) -> list[Row]:
    """``L/slots/{e,v}<n>/``, its lock, and (egress class) its ``keep/`` dir (CONTRACT §2.1) and its front's log dir
    ``L/gw/slot<n>/`` (S1 3b-8a T1): the front account's, so it can rotate its log there, group root and setgid, so a
    log the front makes takes root's group on Linux too (macOS gives a new file its dir's group either way), never the
    slot's; no other access, so the slot can neither list nor read it."""
    rows = [_root(host, slot.slot_dir(STORE[host]), 0o755), _root(host, slot.lock_path(STORE[host]), 0o600, "file")]
    if slot.slot_class == "egress":
        rows.append(_root(host, slot.slot_dir(STORE[host]) / "keep", 0o700))
        rows.append(Row(STORE[host] / "gw" / f"slot{slot.index}", f"_levain_gw{slot.index}", ROOT_GROUP[host],
                        0o2750))
    return rows


def base_run_rows(host: HostOS) -> list[Row]:
    """The run-dir rows that do not depend on the slots (CONTRACT §2.2)."""
    R = RUN[host]
    return [
        _root(host, R, 0o755),
        _root(host, R / "gw", 0o755),
        _root(host, R / "qb", 0o755),
        _root(host, R / "planner", 0o755),
        # §2.2 (c7.2): every uid traverses, none lists; each socket's own mode, group and peer check gate it.
        Row(R / "keys", "_levain_keys", "_levain_keys", 0o711),
        # Setgid (3c-9 B, ruling 1 of 1025-23/3c9a): CONTROL binds control.sock here, and on Linux a new socket takes
        # its creator's primary group (_levain_control) otherwise, which no peer reaching it through levain-control
        # (the operator, keys, qbroker, dispatch) is in. macOS gives a new entry its directory's group either way.
        Row(R / "control", "_levain_control", "levain-control", 0o2750),
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
            # Created only when absent: a lock file is never replaced under a process holding it.
            steps.append(Step(f"create {p}", write=(r.path, "", r.mode), skip_if=(t["test"], "-e", p)))
        steps.append(Step(f"own {p} as {r.owner}:{r.group}", (t["chown"], f"{r.owner}:{r.group}", p)))
        if host == "darwin":
            # A numeric chmod leaves an extended ACL in place, and the trust check reads only owner and mode bits, so
            # a grant an earlier owner set (add_file, delete_child) would outlive this row. Cleared after the chown:
            # until then the old owner can still add an entry, as an owner always can. Linux: no install code reads
            # or writes POSIX ACLs, so a Linux row has no such step.
            steps.append(Step(f"no extended ACL on {p}", (t["chmod"], "-N", p)))
        steps.append(Step(f"set {p} to {r.mode:04o}", (t["chmod"], f"{r.mode:04o}", p)))
    return steps


BASE_ROUTES_FORMAT = "levain-routes-base/2"   # CONTRACT §4.3, R4 §1.3: the format the writer writes
#: the formats every base-map reader accepts (`policy.BASE_MAP_FORMATS`, a test holds the equality)
BASE_ROUTES_FORMATS = ("levain-routes-base/1", "levain-routes-base/2")


def base_routes_path(host: HostOS) -> Path:
    return STORE[host] / "routes" / "base.json"


def base_routes_steps(host: HostOS) -> list[Step]:
    """``L/routes/base.json`` (CONTRACT §2.1, §4.3): the S1 base map with no routes, canonical JSON,
    root 0644. Routes are added by the install verbs that own them. Written only when absent, so a
    later ``setup`` over a store that kept its map does not empty it."""
    from levain.contracts import canon

    p = base_routes_path(host)
    t = _tools(host)
    return [Step(f"write the empty base route map {p}",
                 write=(p, canon.canonical({"format": BASE_ROUTES_FORMAT, "routes": []}), 0o644),
                 skip_if=(t["test"], "-e", str(p)))]


def reserve_row(host: HostOS) -> Row:
    return _root(host, STORE[host] / "reserve", 0o600, "file")


def reserve_steps(host: HostOS) -> list[Step]:
    """``L/reserve``: written zeros, never fallocate (CONTRACT §7.5)."""
    t, r = _tools(host), reserve_row(host)
    size = ("/usr/bin/stat", "-f", "%z") if host == "darwin" else (_abs("stat"), "-c", "%s")
    at_size = ("/bin/sh", "-c", f'[ "$({" ".join(size)} -- "$1")" = {RESERVE_BYTES} ]', "sh", str(r.path))
    return [
        # Skipped when the reserve already exists at its size (a re-run, CONTRACT §23).
        Step(f"write {r.path} as zeros", (t["dd"], "if=/dev/zero", f"of={r.path}", f"bs={_MIB}",
                                         f"count={RESERVE_BYTES // _MIB}"), skip_if=at_size),
        Step(f"own {r.path} as root", (t["chown"], f"{r.owner}:{r.group}", str(r.path))),
        Step(f"set {r.path} to {r.mode:04o}", (t["chmod"], f"{r.mode:04o}", str(r.path))),
    ]


# --- the --root record (CONTRACT §23, a §2.1 row) ------------------------------------------------------

ROOTS_FORMAT = "levain-roots/1"


def roots_path(host: HostOS) -> Path:
    return STORE[host] / "policy" / "roots"


def roots_record_text(roots: Iterable[str]) -> str:
    from levain.contracts import canon

    return canon.canonical({"format": ROOTS_FORMAT, "roots": list(roots)})


def roots_steps(host: HostOS, roots: Iterable[str]) -> list[Step]:
    """``L/policy/roots``: the resolved ``--root`` paths setup was given, root 0644."""
    return [Step(f"record the --root paths in {roots_path(host)}",
                 write=(roots_path(host), roots_record_text(roots), 0o644))]


# --- the configured pool (SESSION2_PLAN head ruling 8; contract c7.8 line owed) ---------------------------------

POOL_CONFIG_FORMAT = "levain-pool-config/1"


def pool_config_path(host: HostOS) -> Path:
    return STORE[host] / "policy" / "pool"


def pool_config_row(host: HostOS) -> Row:
    """``L/policy/pool``: root 0644, written by ``setup``, read by ``replenish``, removed by ``undo``."""
    return _root(host, pool_config_path(host), 0o644, "file")


def pool_config_text(slots: int, verifier_slots: int) -> str:
    from levain.contracts import canon

    return canon.canonical({"format": POOL_CONFIG_FORMAT, "slots": slots, "verifier_slots": verifier_slots})


def parse_pool_config(data: bytes) -> tuple[int, int]:
    """``(slots, verifier_slots)``, or ``pool-config-malformed``: canonical JSON, exactly the three keys, each
    count a non-negative int."""
    from levain.contracts import canon

    try:
        obj = canon.parse(data)
    except canon.ContractError as e:
        raise Refused("pool-config-malformed", str(e)) from None
    if not isinstance(obj, dict) or set(obj) != {"format", "slots", "verifier_slots"} \
            or obj["format"] != POOL_CONFIG_FORMAT:
        raise Refused("pool-config-malformed", f"not a {POOL_CONFIG_FORMAT} record")
    for k in ("slots", "verifier_slots"):
        if type(obj[k]) is not int or obj[k] < 0:
            raise Refused("pool-config-malformed", k)
    return obj["slots"], obj["verifier_slots"]


def policy_limits_path(host: HostOS) -> Path:
    """``L/policy/limits``: the slot sessions' limits (``levain-limits/1``), root 0644, written only by the install
    helper's ``set-limits``, read by the queue broker, removed by ``undo``. Setup writes nothing here (no file is
    the defaults)."""
    return STORE[host] / "policy" / POLICY_LIMITS


def pool_config_steps(host: HostOS, slots: int, verifier_slots: int) -> list[Step]:
    """Setup's ``--slots`` / ``--verifier-slots``, the counts ``replenish`` fills back to. Written by every setup
    run: a re-run is planned at its journal's counts (§25), so it writes the same record."""
    r = pool_config_row(host)
    return [Step(f"record the configured pool ({slots} egress, {verifier_slots} verifier) in {r.path}",
                 write=(r.path, pool_config_text(slots, verifier_slots), r.mode))]


def pool_config_undo_steps(host: HostOS) -> list[Step]:
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    p = str(pool_config_path(host))
    return [Step(f"remove {p}", (rm, "-f", p))]


# --- one filesystem (CONTRACT §2.1) -----------------------------------------------------------------------


def single_filesystem_paths(host: HostOS, slots: Iterable[SlotId]) -> list[Path]:
    """The store dirs that move entries between each other by rename (CONTRACT §2.1 heading): live,
    alloc, retained, trash, staging, export (both children), and every egress slot's keep/."""
    L = STORE[host]
    out = [L / d for d in ("live", "alloc", "retained", "trash", "staging", "export", "export/operator",
                           "export/dispatch")]
    out += [s.slot_dir(L) / "keep" for s in sorted(set(slots)) if s.slot_class == "egress"]
    return out


def same_filesystem(paths: Iterable[Path], *, stat: Callable[[str], os.stat_result] = os.stat) -> tuple[bool, str]:
    """Every path's ``st_dev`` equal (a rename between two of them never crosses filesystems)."""
    devs: dict[int, list[str]] = {}
    for p in paths:
        try:
            devs.setdefault(stat(str(p)).st_dev, []).append(str(p))
        except OSError as e:
            return False, f"{p}: {e.strerror or e}"
    if len(devs) > 1:
        return False, "the store spans filesystems: " + "; ".join(", ".join(v) for v in devs.values())
    return True, ""


def single_filesystem_steps(host: HostOS, slots: Iterable[SlotId]) -> list[Step]:
    paths = single_filesystem_paths(host, slots)
    return [Step(f"check the store is one filesystem ({len(paths)} dirs, st_dev equal)",
                 call=lambda: same_filesystem(paths))]


# --- Linux: R/ at boot (CONTRACT §22) -----------------------------------------------------------------------

TMPFILES_CONF = Path("/etc/tmpfiles.d/levain.conf")


def tmpfiles_text(host: HostOS, slots: Iterable[SlotId]) -> str:
    """One ``d`` line per §2.2 dir (the base rows, then each slot's), parents first, with the row's
    owner, group and mode (setgid kept); systemd applies it before any unit starts."""
    rows = base_run_rows(host) + [r for s in sorted(set(slots)) for r in slot_run_rows(host, s)]
    lines = ["# Written by the Levain install helper (CONTRACT §22); removed by its undo verb."]
    lines += [f"d {r.path} {r.mode:04o} {r.owner} {r.group} -" for r in rows]
    return "\n".join(lines) + "\n"


def tmpfiles_steps(host: HostOS, slots: Iterable[SlotId]) -> list[Step]:
    """Write ``/etc/tmpfiles.d/levain.conf`` for ``slots`` (every slot in service) and apply it now.
    Setup and replenish run the same list. Linux only."""
    if host != "linux":
        raise Refused("unsupported-host", f"{host}: tmpfiles.d is Linux's")
    return [
        Step(f"write {TMPFILES_CONF} (the run dirs systemd recreates at boot)",
             write=(TMPFILES_CONF, tmpfiles_text(host, slots), 0o644)),
        Step(f"apply {TMPFILES_CONF.name}", (_abs("systemd-tmpfiles"), "--create", TMPFILES_CONF.name)),
    ]


def tmpfiles_undo_steps() -> list[Step]:
    return [Step(f"remove {TMPFILES_CONF}", (_abs("rm"), "-f", str(TMPFILES_CONF)))]


def run_dir_undo_steps(host: HostOS) -> list[Step]:
    """Linux: remove ``R`` (§2.2). It is tmpfs, so with the tmpfiles conf gone it would not come back at boot, but
    until then its dirs name tombstoned owners (the 1d VM run). ``--one-file-system``: a mount found below it
    fails the step instead of being walked into. Run after every slot process is stopped."""
    if host != "linux":
        raise Refused("unsupported-host", f"{host}: only Linux's R is removed by undo")
    return [Step(f"remove {RUN[host]} (the run dirs)", (_abs("rm"), "-rf", "--one-file-system", "--", str(RUN[host])))]


# --- undo: no shared gid left on a dir (CONTRACT §23) --------------------------------------------------------


def shared_group_rows(host: HostOS, operator: str) -> list[Row]:
    """Every store and run dir whose row names a shared group (§1.2)."""
    from levain.slot.ids import SHARED_GROUPS

    return [r for r in base_store_rows(host, operator) + base_run_rows(host) if r.group in SHARED_GROUPS]


def ungroup_steps(host: HostOS, operator: str) -> list[Step]:
    """Before the shared groups are deleted: each dir that names one goes to the host's gid-0 group,
    with everything below it, so no deleted gid stays on a dir or a file for a later group to inherit.
    The walk never follows a link (``-P``) and a link's own group is the one changed (``-h``), so it never
    leaves the tree. Run after every slot process is stopped, so nothing new lands mid-walk. A dir
    already gone is skipped. Known edge: the walk resets every entry below, the operator's own files
    and any carrying another group included, so the operator keeps access by owner but loses any
    group-based permission set under a store dir."""
    chgrp = "/usr/bin/chgrp" if host == "darwin" else _abs("chgrp")
    test = _tools(host)["test"]
    return [Step(f"give {r.path} and everything below it back to {ROOT_GROUP[host]} "
                 f"(its group {r.group} is about to be deleted)",
                 (chgrp, "-R", "-h", "-P", ROOT_GROUP[host], str(r.path)), skip_if=(test, "!", "-e", str(r.path)))
            for r in shared_group_rows(host, operator)]


# --- row 33 R1b: an entry removed by its RECORDED name (§1.1 table row 12) -----------------------------------------


def _boot_text(data: bytes) -> bool:
    """A Levain entry script: a ``#!`` line, then the ``# Levain <entry> ... Written by the install helper`` header
    every entry text carries (``root_tree.entry_text``, the current form and the pre-generation one alike)."""
    lines = data.split(b"\n", 2)
    return len(lines) >= 2 and lines[0].startswith(b"#!") and lines[1].startswith(b"# Levain ") \
        and b"Written by the install helper" in lines[1]


def entry_state(host: HostOS, name: str, live) -> str:
    from levain.slot.removal import root_file

    return root_file(live, HELPER_DIR[host] / name, check=_boot_text)


def remove_entry_steps(host: HostOS, name: str, *, live) -> list[Step]:
    """``entry/1`` (R10, after the flip; its sudoers rule went at R5): ``rm -f`` the recorded entry under HELPER_DIR."""
    from levain.slot.removal import at, chain, point, remove_step

    path = HELPER_DIR[host] / name
    rm = "/bin/rm" if host == "darwin" else _abs("rm")
    return [remove_step(f"remove the entry {path}", ("entry/1", name), live, lambda: entry_state(host, name, live),
                        chain(lambda: live.act((rm, "-f", at(live, path))), point(live, "entry/1:after-rm")),
                        tools=(rm,))]
