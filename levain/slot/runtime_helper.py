"""The runtime helper's ``__main__``: one root NOPASSWD entry, argv + stdin in, one canonical JSON line out
(CONTRACT §5 preamble, §5.1).

:func:`main` derives the caller from ``SUDO_UID`` (the operator recorded in ``L/policy/operator``, or
``_levain_dispatch``), reads the configured slots from the store (never from argv), passes argv and stdin through
:mod:`levain.slot.runtime_grammar` and runs the verb. Every refusal is :func:`runtime_grammar.refusal_line` and a
non-zero exit, raised before the next act: nothing after the refusing check acts.

Verbs built here: ``prepare``, ``render``, ``resume``, ``status``, ``adopt-open``, ``adopt-verify``, ``discard`` and
``restore`` (session 5's);
``start`` and ``teardown`` dispatch to :mod:`levain.slot.start`, ``probe`` (V13) to :mod:`levain.slot.probe`. Every other verb (derive: ``VERBS`` less
``VERB_HANDLERS``) answers ``verb-unbuilt:<verb>``. A ticketed (``_levain_dispatch``) ``prepare`` takes its slot by
§3.5 Pool's point-of-use rules and writes ``ATTACH``'s ``ticket_facts`` into alloc before it returns (S1 3c-7).

Every root filesystem act goes through a dirfd: the store root is opened once with ``O_DIRECTORY|O_NOFOLLOW`` and
every path below it is reached one component at a time with ``O_NOFOLLOW`` (§5 preamble), so no argv token, stdin
field or planted link names a path. Every OS act a unit test cannot do as a non-root user is a field of
:class:`Seams` with a real default (owners are recorded, not applied, in the tests).

The slot state file ``L/slots/{e,v}<n>/state`` (``levain-slot-state/1``, §3.5 Pool) has ONE definition,
:mod:`levain.slot.records`; ``prepare`` (lease) and ``teardown`` (SCRUBBED) read and write it through it, and
:func:`slot_snapshot` reads every one for the reaper's ``SLOT-SYNC``.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, ContextManager, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import ContractError, canon
from levain.slot import Refused, wall, write_all
from levain.slot import bundlefmt as BF
from levain.slot import fsck_ids as FI
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import stage as ST
from levain.slot import verify_caps as VC
from levain.slot.end import V5_FLOOR_BYTES
from levain.slot.grammar import SESSION_RE
from levain.slot.runtime_grammar import _NAME_RE as _WIRE_NAME_RE
from levain.slot.ids import SlotId, in_own_range
from levain.slot.layout import BASE_ROUTES_FORMAT, BASE_ROUTES_FORMATS, RUN, STORE, admits_root, repair_line, store_dir_row
from levain.slot.runtime_grammar import (DISPATCH, OPERATOR, VENDORS, PrepareInput, RenderBlob, RuntimeRequest,
                                         bind_stdin, ok_line, parse_runtime_argv, parse_stdin, refusal_line)

PREPARE_RESULT_FORMAT = "levain-prepare-result/1"

EXIT_OK = 0
EXIT_REFUSED = 1       # DECISION: every refusal by name
EXIT_INTERNAL = 70     # DECISION: an unexpected error (stdout still carries one line, ``internal``)

LOCK_WAIT_S = 10.0     # §11 V23: render's ``locked`` retry window; DECISION: also the pool / slot flock wait
LOCK_SLICE_S = 0.02    # DECISION: the flock retry's first backoff (doubling to 0.5 s)
#: DECISION: fresh session ids ``prepare`` draws before it gives up on a collision (each is 64 random bits).
SESSION_ID_TRIES = 8
#: ATTACH refusals that do not say CONTROL created nothing: the answer was lost, or CONTROL failed mid-write.
_ATTACH_AMBIGUOUS = frozenset({"control-down", "internal-error", "record-unsettled"})

#: The front's GO pipe read end sits on this fd in the front (§5.1 render (4)): above 0-2 and wall's SPEC_FD.
GO_FD = 4
#: DECISION: the front learns its GO fd from this env var (the front's env is ``extra_env`` alone, wall §8.2).
GO_FD_ENV = "LEVAIN_GO_FD"
GO_BYTE = b"G"         # DECISION: §5.1 (5b) "writes the go byte" names no value
#: §5.1 render (4): the front program is a parameter: the gateway front (3b-8a); ``levain.broker.stub`` with
#: :func:`stub_front_args` is session 2's.
FRONT_MODULE = "levain.broker.front"
#: §2.2 ``R/gw/slot<n>/``: the front's policy file (§4.3 "the front's view", render (3)) and its tunnel log.
FRONT_POLICY_NAME = "session.json"
FRONT_LOG_NAME = "front.log"
#: §4.3: render's copy of a base-map entry drops these (the front holds no key and meters nothing).
FRONT_DROPPED_FIELDS = ("credential", "prices")

#: §2.1 ``keep/``: the vendor credential file a slot parks (END's ``CREDENTIAL_FILES``, by vendor).
CREDENTIAL_NAME: Mapping[str, str] = {"claude": ".credentials.json", "codex": "auth.json"}

_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_O_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_RECORD_CAP = 1 << 20
_SUDO_UID_RE = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)


# --- seams ----------------------------------------------------------------------------------------------------------

def _host() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


#: What a store directory root acts below must be: the layout's root shape (:func:`layout.admits_root`).
_root_trusted = admits_root


def _fchown(fd: int, uid: int, gid: int, rel: str) -> None:
    os.fchown(fd, uid, gid)


def _free_bytes(store_fd: int) -> int:
    st = os.fstatvfs(store_fd)
    return st.f_bavail * st.f_frsize


#: <linux/posix_acl_xattr.h>: the xattr's version and each entry's tag; an entry is (u16 tag, u16 perm, u32 id), LE.
_ACL_XATTR = "system.posix_acl_access"
_ACL_VERSION = 2
_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
_ACL_UNDEFINED_ID = 0xFFFFFFFF
_SEARCH = 1
QBROKER = "_levain_qbroker"


def linux_search_acl(mode: int, uid: int) -> bytes:
    """``system.posix_acl_access`` for a directory of ``mode`` plus one named-user entry granting ``uid`` search only.
    The mask is the group bits plus search, so the mode the kernel shows keeps its group bits whenever they already
    hold search (``<S>``'s 0750 stays 0750)."""
    import struct

    u, g, o = (mode >> 6) & 7, (mode >> 3) & 7, mode & 7
    entries = [(_ACL_USER_OBJ, u, _ACL_UNDEFINED_ID), (_ACL_USER, _SEARCH, uid), (_ACL_GROUP_OBJ, g, _ACL_UNDEFINED_ID),
               (_ACL_MASK, g | _SEARCH, _ACL_UNDEFINED_ID), (_ACL_OTHER, o, _ACL_UNDEFINED_ID)]
    return struct.pack("<I", _ACL_VERSION) + b"".join(struct.pack("<HHI", *e) for e in entries)


def grant_broker_search(host: str, fd: int, path: Path, broker: str, broker_uid: int) -> None:
    """§26 S1-c: ONE ACL entry on a dispatch session's ``<S>`` giving the queue broker search and nothing else.
    Linux: the xattr on the fd (never the ``setfacl`` binary, which the Linux VM lacks). macOS: ``chmod +a`` by
    name on ``<S>``'s path, whose every parent is root's (``L/live`` 0711), so the path names exactly this dir.
    Any failure: ``grant-reach-unset`` (a dispatch session is never started without the reach)."""
    try:
        if host == "linux":
            os.setxattr(fd, _ACL_XATTR, linux_search_acl(stat.S_IMODE(os.fstat(fd).st_mode), broker_uid))
            return
        r = subprocess.run(["/bin/chmod", "+a", f"user:{broker} allow search", str(path)], capture_output=True,
                           cwd="/", env={"PATH": "/usr/bin:/bin"}, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused("grant-reach-unset", str(e)[:120]) from None
    if r.returncode != 0:
        raise Refused("grant-reach-unset", (r.stderr or r.stdout).decode("utf-8", "replace").strip()[:120])


def _account_uid(name: str) -> int:
    import pwd

    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        raise Refused("account-missing", name) from None


def _group_gid(name: str) -> int:
    import grp

    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        raise Refused("group-missing", name) from None


def _user_gid(uid: int) -> int:
    import pwd

    try:
        return pwd.getpwuid(uid).pw_gid
    except KeyError:
        raise Refused("account-missing", str(uid)) from None


def read_boot_id() -> str:
    """The boot's id (§7.1 ``boot_id``): Linux ``/proc/sys/kernel/random/boot_id``, macOS ``kern.bootsessionuuid``."""
    if sys.platform == "darwin":
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True,
                           timeout=10, env={})
        text = r.stdout if r.returncode == 0 else ""
    else:
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                text = fh.read()
        except OSError:
            text = ""
    if not text.strip():
        raise Refused("boot-id-unknown", sys.platform)
    return text.strip()


def _new_session() -> str:
    return "s-" + os.urandom(8).hex()


def _default_client(path: Path):
    try:
        from levain.slot.control_client import ControlClient
    except ImportError:
        raise Refused("control-down", "the CONTROL client is not built") from None
    return ControlClient(path)


def proc_identity(pid: int) -> Optional[Tuple[int, int]]:
    """``(real uid, start)`` of a live process, or None: proc.py's probe (macOS libproc, microseconds; Linux
    ``/proc/<pid>/stat`` field 22, clock ticks), so MINT's ``front.start`` and the later live checks (END, SCRUBBED)
    read the same units; keys' peer check reads the same value by its own probe (``levain.broker.peer.start_of``:
    sysctl ``KERN_PROC_PID`` on macOS, where libproc cannot read another uid's process). A pid whose facts cannot be read
    counts as unknown (None): nothing is signalled on it."""
    p = PR.default_seams().probe(int(pid))
    if p is None or p.state == PR.UNKNOWN or not p.uids:
        return None
    return p.uids[0], p.start


def darwin_user_dir(uid: int, gid: int) -> str:
    """macOS: ``uid``'s ``/private/var/folders/<a>/<b>`` (alloc's ``var_folders``, read at prepare), from ``getconf
    DARWIN_USER_DIR`` run AS that uid (``/var/folders/<a>/<b>/0/``); it CREATES the dir when absent [measured:
    levain-memory runs/s2-macos/RESULT_D1D2.md X9, 2026-10-20], so it runs at prepare only."""
    r = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, timeout=10,
                       env={}, user=uid, group=gid, extra_groups=[])
    parts = [p for p in r.stdout.strip().split("/") if p] if r.returncode == 0 else []
    if len(parts) < 4 or parts[:2] != ["var", "folders"]:
        raise Refused("var-folders-unknown", str(uid))
    return "/private/var/folders/" + parts[2] + "/" + parts[3]


def _close_fds_except(keep: Sequence[int]) -> None:
    hi = min(os.sysconf("SC_OPEN_MAX"), 65536)
    lo = 3
    for fd in sorted(k for k in set(keep) if k >= 3):
        os.closerange(lo, fd)
        lo = fd + 1
    os.closerange(lo, hi)


def spawn_front(argv: Sequence[str], spec_json: bytes, go_rfd: int) -> int:
    """§5.1 render (4): start the front DETACHED and return its pid. The child: ``setsid``; fds 0/1/2 on
    ``/dev/null``; the GO pipe's read end on :data:`GO_FD`; every other fd closed but the spec pipe, which
    :func:`wall.exec_launch` moves to ``SPEC_FD`` before it execs the stage (drop + assert to ``_levain_gw<n>``,
    §8.2). The parent writes the spec and keeps no fd of the child's. Only root can run it (a VM row)."""
    spec_r, spec_w = os.pipe()
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the root child; exercised only in the VM (2g)
        try:
            os.setsid()
            null = os.open("/dev/null", os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            if null > 2:
                os.close(null)
            hi_go = fcntl.fcntl(go_rfd, fcntl.F_DUPFD, 32)
            hi_spec = fcntl.fcntl(spec_r, fcntl.F_DUPFD, 32)
            os.dup2(hi_go, GO_FD)
            _close_fds_except([GO_FD, hi_spec])
            wall.exec_launch(argv, hi_spec)
        except BaseException:   # noqa: BLE001 - the child never returns into the helper's stack
            pass
        os._exit(70)
    os.close(spec_r)
    try:
        write_all(spec_w, spec_json)
    finally:
        os.close(spec_w)
    return pid


def _child_refused(marker: Optional[int], write: Callable[[int, bytes], int] = os.write) -> None:
    """:func:`spawn_target`'s child, failing before its exec: ``!`` on the pipe's fd (None: no pipe)."""
    if marker is None:
        return
    try:
        write(marker, wall.EXEC_REFUSED)
    except OSError:
        pass


def spawn_target(argv: Sequence[str], spec_json: bytes, cgroup: Optional[str], exec_w: Optional[int],
                 detach: bool, *, stdout_w: Optional[int] = None) -> int:
    """``start``'s fork through the wall (§5.1 start, §5.4 step 3, §9.1): the child keeps fds 0/1/2 (the helper's
    own stdio, sudo's pty; ``detach``: ``/dev/null`` + ``setsid``, the forwarder), the spec pipe (moved to
    ``SPEC_FD`` by :func:`wall.exec_launch`) and, when ``exec_w`` is given, that pipe's write end on
    :data:`wall.EXEC_FD`: the vendor's exec marker (the last hop writes ``+`` just before the vendor's execve) or
    the forwarder's readiness pipe (:data:`wall.READY_FD`, the same fd). Every other fd is closed. A child that
    fails before its exec writes ``!`` on whichever fd holds the pipe at that moment (``exec_w`` itself until
    the dup2 onto ``EXEC_FD`` succeeded), so its failure is never only an EOF. ``stdout_w``: a pipe's write end put
    on the child's fd 1 instead of the helper's (a post-clone check child, :func:`capture_target`). Only root can
    run it (a VM row)."""
    spec_r, spec_w = os.pipe()
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the root child; exercised only in the VM (2g)
        marker = exec_w            # where the refusal byte goes: exec_w until the dup2 below moved it
        try:
            if detach:
                os.setsid()
                null = os.open("/dev/null", os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                if null > 2:
                    os.close(null)
            if stdout_w is not None:
                os.dup2(stdout_w, 1)
            hi_spec = fcntl.fcntl(spec_r, fcntl.F_DUPFD, 32)
            keep = [hi_spec]
            if exec_w is not None:
                hi_exec = fcntl.fcntl(exec_w, fcntl.F_DUPFD, 32)
                os.dup2(hi_exec, wall.EXEC_FD)
                marker = wall.EXEC_FD
                keep.append(wall.EXEC_FD)
            _close_fds_except(keep)
            wall.exec_launch(argv, hi_spec, cgroup=cgroup)
        except BaseException:   # noqa: BLE001 - the child never returns into the helper's stack
            pass
        _child_refused(marker)
        os._exit(70)
    os.close(spec_r)
    try:
        write_all(spec_w, spec_json)
    finally:
        os.close(spec_w)
    return pid


def wait_child(pid: int) -> int:
    """Wait for the helper's own child and return its exit status (DECISION: a signal-killed child answers
    ``128 + signal``, the shell's convention). SIGINT and SIGQUIT are ignored while waiting: the terminal's
    Ctrl-C reaches the vendor (same pty), and the helper must outlive it to answer its one line."""
    old = [(sig, signal.signal(sig, signal.SIG_IGN)) for sig in (signal.SIGINT, signal.SIGQUIT)]
    try:
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        for sig, h in old:
            signal.signal(sig, h)
    return _exit_code(status)


def _exit_code(status: int) -> int:
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


#: DECISION: the bound on one post-clone check child (``ls-files`` or one ``hash-object`` batch), from its spawn to
#: its exit (its stdout's EOF, then its reaping). Past it the child is killed and the check fails: a stdout another
#: process still holds open, or a child that closed its stdout and runs on, never stalls ``start``.
CHECK_S = 300.0


def drain(fd: int, bound: int, *, deadline_s: float = CHECK_S,
          monotonic: Callable[[], float] = time.monotonic) -> bytes:
    """Everything a check child prints on the pipe's read end ``fd``, read WHILE it runs (a full pipe never stalls
    it), until EOF. More than ``bound`` bytes = ``check-output-over-bound`` (refused, never truncated: the read
    stops at the first byte past it); no EOF within ``deadline_s`` = ``check-timeout``."""
    end = monotonic() + deadline_s
    chunks: List[bytes] = []
    size = 0
    with selectors.DefaultSelector() as sel:   # not select.select: any fd number, 1024 and past it included
        sel.register(fd, selectors.EVENT_READ)
        while True:
            left = end - monotonic()
            if left <= 0 or not sel.select(left):
                raise Refused("check-timeout", f"no EOF within {deadline_s:g}s")
            b = os.read(fd, min(65536, bound - size + 1))
            if not b:
                return b"".join(chunks)
            size += len(b)
            if size > bound:
                raise Refused("check-output-over-bound", f"more than {bound} bytes")
            chunks.append(b)


def reap_within(pid: int, deadline_s: float, *, monotonic: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep, refusal: str = "check-timeout") -> int:
    """The helper's own child ``pid`` reaped within ``deadline_s`` (its exit status as :func:`wait_child` answers
    it); past it the child is SIGKILLed and its reap polled for at most ``proc.REAP_GRACE_S``
    (:func:`levain.slot.proc.kill_and_reap`: a child that will not die joins proc's leftover set), then ``refusal``
    (``check-timeout`` for a post-clone check child). Polled (``WNOHANG``), as no portable call waits on one pid with
    a timeout; never a blocking wait, so a child in uninterruptible sleep cannot hold the caller past its bound."""
    end = monotonic() + deadline_s
    nap = 0.001
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        if done:
            return _exit_code(status)
        if monotonic() >= end:
            PR.kill_and_reap(pid, os.kill, os.waitpid, monotonic, sleep)
            raise Refused(refusal, f"the child did not exit within {deadline_s:g}s")
        sleep(nap)
        nap = min(nap * 2, 0.05)


def wait_child_within(pid: int, deadline_s: float) -> int:
    """:func:`wait_child` bounded by ``deadline_s`` (:func:`reap_within`'s poll, SIGINT and SIGQUIT ignored while it
    waits, as there): past it the child is SIGKILLed and reaped, then ``child-timeout``. For ``start``'s clone, which
    runs under the session END lock: an unbounded wait there would answer every END ``end-busy`` for as long as a
    clone hangs."""
    old = [(sig, signal.signal(sig, signal.SIG_IGN)) for sig in (signal.SIGINT, signal.SIGQUIT)]
    try:
        return reap_within(pid, deadline_s, refusal="child-timeout")
    finally:
        for sig, h in old:
            signal.signal(sig, h)


def capture_target(argv: Sequence[str], spec_json: bytes, cgroup: Optional[str], bound: int) -> Tuple[int, bytes]:
    """A post-clone check child (§5.4 step 3, wall's ``clone-check`` target) by :func:`spawn_target`'s path, its
    stdout on a pipe root drains while it runs (:func:`drain`), then the child reaped (:func:`reap_within`), both
    within one :data:`CHECK_S` from the spawn; its exit status and that output. A drain that refuses kills and reaps
    the child first, then raises. Only root can run it (a VM row)."""
    end = time.monotonic() + CHECK_S
    out_r, out_w = os.pipe()
    try:
        pid = spawn_target(argv, spec_json, cgroup, None, False, stdout_w=out_w)
    except BaseException:
        os.close(out_r)
        raise
    finally:
        os.close(out_w)
    try:
        data = drain(out_r, bound, deadline_s=end - time.monotonic())
    except BaseException:
        os.close(out_r)
        PR.kill_and_reap(pid)
        raise
    os.close(out_r)
    return reap_within(pid, end - time.monotonic()), data


def make_cgroup(path: str, *, mkdir: Callable[[str, int], None] = os.mkdir,
                lstat: Callable[[str], os.stat_result] = os.lstat) -> None:
    """Linux: the session's cgroup (§7.3), made by ``start`` before the first process joins it (wall's
    :func:`exec_launch` proves the join). Every missing directory from ``levain.slice`` down to the session leaf is
    made root 0755 (setup makes none of them: VM run s2-linux F3); every one on the way, the cgroupfs root
    included, must then be a root-owned directory, not a link, writable by no group or other, else
    ``cgroup-untrusted`` and no process joins it. DECISION: one that exists (a start that died after making it) is
    used. [known edge: a systemd-delegated ``levain.slice`` (systemd owning these names under its root) is a later
    refinement; here root's own mkdir makes them.]"""
    root = wall.CGROUP_ROOT.rsplit("/", 1)[0]          # the cgroupfs mount, never made here
    parts = path[len(root) + 1:].split("/")
    if not path.startswith(wall.CGROUP_ROOT + "/") or any(p in ("", ".", "..") for p in parts):
        raise Refused("cgroup-untrusted", f"{path!r} is not below {wall.CGROUP_ROOT}")
    cur = root
    for i, part in enumerate([""] + parts):
        cur = cur if i == 0 else f"{cur}/{part}"
        try:
            st = lstat(cur)
        except FileNotFoundError:
            if i == 0:
                raise Refused("cgroup-untrusted", f"{cur} is not mounted") from None
            try:
                mkdir(cur, 0o755)
            except FileExistsError:
                pass                                     # made between the lstat and the mkdir: checked below
            st = lstat(cur)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise Refused("cgroup-untrusted", f"{cur}: uid {st.st_uid} mode {stat.S_IMODE(st.st_mode):o}")


def stub_front_args(host: str, slot: SlotId, **_: object) -> Tuple[str, ...]:
    """The FIXED argv root gives the session-2 front, ``levain.broker.stub`` (§5.1 render (4), lane F3 b): the
    Linux unix shape on ``R/gw/slot<n>/gw.sock`` (§2.2), the macOS TCP shape on ``gwport_n``. Nothing in it is
    slot- or session-supplied. DECISION: its log is ``/dev/null`` (the stub has nothing a session needs kept).
    Takes and ignores :func:`gateway_front_args`'s keywords, so either can be ``Seams.front_args``."""
    if host == "darwin":
        return ("--port", str(RD.gwport_for(host, slot)), "--log", "/dev/null")
    sock = RUN[host] / "gw" / f"slot{slot.index}" / "gw.sock"
    return ("--unix", str(sock), "--slot", str(slot.index), "--log", "/dev/null")


def gateway_front_args(host: str, slot: SlotId, *, run: Path, store: Path, slot_uid: int) -> Tuple[str, ...]:
    """The argv root gives ``levain.broker.front`` (§5.1 render (4)): the listener (Linux: ``R/gw/slot<n>/gw.sock``
    serving only ``slot_uid``, the alloc's slot account; macOS: ``127.0.0.1:gwport_n``), the policy file render (3)
    wrote in ``R/gw/slot<n>/``, the tunnel log in ``L/gw/slot<n>/`` (3b-8a T1: an audit trail, so on the store, which
    a reboot keeps), keys' ``R/keys/front.sock`` (the STATUS connection, §4.2) and the GO pipe's fd. Every path is
    root's run dir or store; nothing is slot- or session-supplied."""
    gw = run / "gw" / f"slot{slot.index}"
    shape: Tuple[str, ...]
    if host == "darwin":
        shape = ("--port", str(RD.gwport_for(host, slot)))
    else:
        shape = ("--unix", str(gw / "gw.sock"), "--slot-uid", str(slot_uid))
    return (*shape, "--policy", str(gw / FRONT_POLICY_NAME),
            "--log", str(store / "gw" / f"slot{slot.index}" / FRONT_LOG_NAME),
            "--keys", str(run / "keys" / "front.sock"), "--go-fd", str(GO_FD))


def front_session_routes(base: Mapping[str, object], routes: Sequence[str], *,
                         dispatch: bool = False) -> List[dict]:
    """The session's routes as the front reads them (§4.3 "the front's view"): each id's base-map entry, in the
    blob's order, with :data:`FRONT_DROPPED_FIELDS` removed. An id with no base-map entry is refused
    ``front-policy-unavailable``. ``dispatch`` (a dispatch or planner session): ``max_session_microdollars`` is
    dropped too, as a dispatch-session file carries none (the ticket's reservation is the cap, kept by keys).
    Render never brings a ticketed session here: :func:`refuse_ticketed_front` refuses it ``route-policy-unavailable`` first
    (3b-8a T5), since its brokered entry is to come from the ticket's pinned generation file, its digest re-checked
    (CONTRACT §3.3 ROUTE PINNING, S2), never from the base map."""
    by_id = {r.get("route"): r for r in base.get("routes", ()) if isinstance(r, dict)}   # type: ignore[union-attr]
    drop = FRONT_DROPPED_FIELDS + (("max_session_microdollars",) if dispatch else ())
    out: List[dict] = []
    for rid in routes:
        entry = by_id.get(rid)
        if entry is None:
            raise Refused("front-policy-unavailable", f"route {rid} has no base-map entry")
        out.append({k: v for k, v in entry.items() if k not in drop})
    return out


def front_policy_bytes(store: Store, a: R.Alloc, routes: Sequence[str]) -> bytes:
    """``R/gw/slot<n>/session.json``'s bytes for alloc ``a`` and the blob's ``routes``: format, kind, session, the
    routes (:func:`front_session_routes` over ``L/routes/base.json``) and the incarnation, the lease's ``lease_seq``
    (render (2)'s DECISION). The front's own parser (``levain.broker.front.load_policy``) reads them here first: a
    file it would refuse is ``front-policy-refused``. An alloc carrying a ticket is ``route-policy-unavailable``
    (3b-8a T5): the base map is read for a session with no ticket only (render refuses it earlier too)."""
    refuse_ticketed_front(a)
    # on the call: the front's import closure (asyncio, h11) stays out of every other verb's start
    from levain.broker import front as FR

    data = store.read(("routes",), "base.json")
    if data is None:
        raise Refused("base-map-missing", "L/routes/base.json")
    routes_view = front_session_routes(_canon_obj(data, "base-map"), routes, dispatch=a.kind in FR.DISPATCH_KINDS)
    doc = {"format": FR.POLICY_FORMAT, "kind": a.kind, "session": a.session, "routes": routes_view,
           "incarnation": a.lease_seq}
    out = canon.canonical_bytes(doc)
    try:
        FR.load_policy(out)
    except FR.PolicyError as e:
        raise Refused("front-policy-refused", str(e)[:200]) from None
    return out


def refuse_ticketed_front(a: R.Alloc) -> None:
    """3b-8a T5, until S2 builds CONTRACT §3.3 ROUTE PINNING: a ticketed egress session (dispatch or planner) has no
    front policy root can build, so it is refused by name. The base map's entry is never a ticket's, even under the
    same id."""
    if a.ticket is not None and a.slot.slot_class == "egress":
        raise Refused("route-policy-unavailable", "a ticketed session's front policy needs route pinning (S2)")


def _front_dir_ok(st: os.stat_result, seams: "Seams", front_uid: int) -> bool:
    """A front-owned dir (``R/gw/slot<n>/``, ``L/gw/slot<n>/``): the front account's, no group or other write."""
    return st.st_uid == seams.chowned_uid(front_uid) and not st.st_mode & 0o022


def write_front_policy(seams: "Seams", slot: SlotId, front_uid: int, data: bytes) -> None:
    """Root writes ``R/gw/slot<n>/session.json`` (§2.2 row; here root : the front account's primary group, 0440).
    ``R/`` and ``R/gw/`` must pass ``seams.trusted``; ``R/gw/slot<n>/`` is the front account's (its §2.2 row), so a
    name left there is unlinked and the file made fresh, ``O_EXCL|O_NOFOLLOW`` at that dirfd: a planted link is never
    followed. The file is fsynced, then its dir."""
    where = f"gw/slot{slot.index}"
    fds: List[int] = []
    try:
        fd = os.open(str(seams.R), _O_DIR)
        fds.append(fd)
        if not seams.trusted(os.fstat(fd)):
            raise Refused("bad-root", f"{seams.R} is not root's alone")
        fd = _openat_dir(fd, "gw", "gw")
        fds.append(fd)
        if not seams.trusted(os.fstat(fd)):
            raise Refused("bad-root", f"{seams.R}/gw is not root's alone")
        fd = _openat_dir(fd, f"slot{slot.index}", where)
        fds.append(fd)
        if not _front_dir_ok(os.fstat(fd), seams, front_uid):
            raise Refused("bad-root", f"{where} is not the front account's alone")
        # what is left there is the front account's (a dir, a link, a file); an object root cannot unlink, or one
        # put back between the unlink and the exclusive open, is a refusal by name
        try:
            os.unlink(FRONT_POLICY_NAME, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise Refused("bad-root", f"{where}/{FRONT_POLICY_NAME}: {e.strerror}") from None
        try:
            f = os.open(FRONT_POLICY_NAME, _O_NEW, 0o440, dir_fd=fd)
        except (FileExistsError, IsADirectoryError):
            raise Refused("bad-root", f"{where}/{FRONT_POLICY_NAME} reappeared") from None
        except OSError as e:
            if e.errno != errno.ELOOP:
                raise
            raise Refused("bad-root", f"{where}/{FRONT_POLICY_NAME} reappeared") from None
        try:
            seams.chown(f, 0, seams.user_gid(front_uid), f"{where}/{FRONT_POLICY_NAME}")
            os.fchmod(f, 0o440)
            write_all(f, data)
            seams.fsync(f)
        finally:
            os.close(f)
        seams.fsync(fd)
    finally:
        for d in reversed(fds):
            os.close(d)


#: 3b-8a T1: the tunnel log's mode, front account : root (never the slot's group, so the slot cannot read it).
FRONT_LOG_MODE = 0o640


def _statat_dir(parent: int, name: str, where: str) -> os.stat_result:
    """``name`` below ``parent`` stat'd without following a link and without opening it, so a caller who may search
    ``parent`` but not read ``name`` gets a verdict: absent (``FileNotFoundError``, and nothing else) is
    ``store-missing``; a link or any other non-directory is ``bad-root``, as :func:`_openat_dir` refuses them."""
    try:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        raise Refused("store-missing", where) from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("bad-root", f"{where} is not a directory")
    return st


def _open_front_log_dir(store: "Store", slot: SlotId, front_uid: Optional[int], *,
                        open_slot: bool = True) -> Optional[int]:
    """The checking half of :func:`prepare_front_log`: ``L/gw/`` opened through ``store.dir(..., trusted=True)`` (a
    link, a non-directory, a wrong owner or a group/other-writable mode is ``bad-root``), then ``L/gw/slot<n>/``
    stat'd below it by :func:`_statat_dir` and held to :func:`_front_dir_ok`. The stat needs search on ``L/gw/`` only,
    never read on ``L/gw/slot<n>/`` (setup's row, ``layout.slot_store_rows``, is the front account's with group root
    and no other access, so the operator running ``levain doctor`` cannot read it).
    Only a miss of one of those two (each raises ``store-missing`` on ``FileNotFoundError`` and on nothing else) is
    rewritten as the pre-move install, naming the dir that is missing and :func:`layout.repair_line`; a
    ``store-missing`` from elsewhere keeps its own text.

    With ``open_slot`` (root's caller), ``L/gw/slot<n>/`` is then opened ``O_NOFOLLOW|O_DIRECTORY`` through the same
    ``L/gw/`` fd and refused ``bad-root`` unless the fd is the dir that was judged (its ``st_dev``, ``st_ino``) and
    its own fstat still passes :func:`_front_dir_ok`: a dir moved, swapped, removed or re-moded between the stat and
    the open is never used. The fd is returned for the caller to close.
    None without ``open_slot``, or with ``front_uid`` None (no front account to judge the slot dir by: only ``L/gw/``
    is walked). Any other ``OSError`` of the walk is raised as it is."""
    seams = store.seams
    name = f"slot{slot.index}"
    where = f"gw/{name}"
    gw = f"{store.path}/gw"
    try:
        with store.dir("gw", trusted=True) as gfd:
            if front_uid is None:
                return None
            st = _statat_dir(gfd, name, where)
            if not _front_dir_ok(st, seams, front_uid):
                raise Refused("bad-root", f"{where} is not the front account's alone")
            if not open_slot:
                return None
            try:
                fd = _openat_dir(gfd, name, where)
            except Refused as e:
                if e.name != "store-missing":
                    raise
                raise Refused("bad-root", f"{where} changed while it was checked") from None
            try:
                got = os.fstat(fd)
                if (got.st_dev, got.st_ino) != (st.st_dev, st.st_ino):
                    raise Refused("bad-root", f"{where} changed while it was checked")
                # the front account owns it and may chmod the same inode between the stat and the open
                if not _front_dir_ok(got, seams, front_uid):
                    raise Refused("bad-root", f"{where} is not the front account's alone")
            except BaseException:
                os.close(fd)
                raise
            return fd
    except Refused as e:
        if e.name != "store-missing" or e.detail not in (gw, where):
            raise
        what = (f"{gw} is missing, so the front log dir {store.path}/{where} is too" if e.detail == gw
                else f"{store.path}/{where}: the front log dir is missing")
        raise Refused("store-missing", f"{what} (an install made before it existed); run "
                      f"`{repair_line(seams.host)}` once") from None


def check_front_log_dirs(store: "Store", slot: SlotId, front_uid: Optional[int] = None) -> List[Refused]:
    """What a session start on egress ``slot`` would refuse about its front log dirs, without making, chowning or
    writing anything: each of the slot's accounts looked up as render's :func:`slot_ids` does (when ``front_uid`` is
    not given), then :func:`prepare_front_log`'s own walk (:func:`_open_front_log_dir`) up to its final open, which
    only root's caller makes (``levain doctor`` runs as the operator, who may search ``L/gw/`` but not read
    ``L/gw/slot<n>/``). The refusals in the order the runtime meets them, empty when the start would pass them; every
    refused account is listed (render stops at the first), then the walk (with no front account, its ``L/gw/`` half,
    which needs no account). Raises the walk's other ``OSError`` (an unreadable dir) and whatever
    ``seams.account_uid`` raises that is not a ``Refused``."""
    found: List[Refused] = []
    if front_uid is None:
        ids: Dict[str, int] = {}
        for name in slot.accounts:
            try:
                ids[name] = account_in_range(store.seams, name)
            except Refused as e:
                found.append(e)
        front_uid = ids.get(slot.accounts[1])
    try:
        _open_front_log_dir(store, slot, front_uid, open_slot=False)
    except Refused as e:
        found.append(e)
    return found


def prepare_front_log(store: "Store", slot: SlotId, front_uid: int) -> None:
    """3b-8a T1: ``L/gw/slot<n>/front.log``, root-prepared before the front starts. ``L/gw/`` must pass
    ``seams.trusted`` (``store.dir(..., trusted=True)`` refuses it ``bad-root``); ``L/gw/slot<n>/`` is the front
    account's (setup's row, ``layout.slot_store_rows``), no group or other write. Either dir missing (an install made
    before the log moved to the store) is ``store-missing`` naming :func:`layout.repair_line`. Those checks are
    :func:`_open_front_log_dir`, the walk :func:`check_front_log_dirs` runs read-only for ``levain doctor``; only here
    is ``L/gw/slot<n>/`` then opened, and only if it is still the dir the walk judged. The log
    is made ``O_EXCL|O_NOFOLLOW`` when absent; one already there (the slot's earlier sessions: an audit trail, never
    truncated) must be a regular file with one link, owned by the front account or root. Either way it is then chowned
    front account : root and set to :data:`FRONT_LOG_MODE`, so a log a front made under a loose umask is put right. A
    link, a dir, a second hard link: ``bad-root``, nothing chowned through it."""
    seams = store.seams
    rel = f"gw/slot{slot.index}/{FRONT_LOG_NAME}"
    fd = _open_front_log_dir(store, slot, front_uid)
    try:
        try:
            f = os.open(FRONT_LOG_NAME, _O_NEW, FRONT_LOG_MODE, dir_fd=fd)
        except FileExistsError:
            try:
                f = os.open(FRONT_LOG_NAME, _O_READ, dir_fd=fd)
            except OSError as e:
                raise Refused("bad-root", f"{rel}: {e.strerror}") from None
            st = os.fstat(f)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 \
                    or st.st_uid not in (seams.chowned_uid(front_uid), 0):
                os.close(f)
                raise Refused("bad-root", f"{rel} is not the front's log file")
        except OSError as e:
            raise Refused("bad-root", f"{rel}: {e.strerror}") from None
        try:
            seams.chown(f, front_uid, 0, rel)
            os.fchmod(f, FRONT_LOG_MODE)
            seams.fsync(f)
        finally:
            os.close(f)
        seams.fsync(fd)
    finally:
        os.close(fd)


def wake_reaper(run: Path) -> None:
    """A wake on ``R/reaper/wake.sock`` (§2.2, §3.5 Pool: ``prepare`` wakes the sync). Best effort: content is
    ignored and a lost wake costs one V11 interval."""
    import socket

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(b"w", str(run / "reaper" / "wake.sock"))
        finally:
            s.close()
    except OSError:
        pass


def _front_interlock():
    """The reaper's ``front.lock`` primitive and ``front-revoked`` marker, resolved on the CALL, never at this
    module's import (the module imports with the reaper's half absent). Absent or incomplete, a front start fails
    closed ``front-interlock-unavailable``: no front is ever spawned without the lock."""
    try:
        from levain.slot import start as SS

        return SS.front_lock, SS.FRONT_LOCK_WAIT_S, SS.FRONT_REVOKED, SS._marker
    except (ImportError, AttributeError):
        raise Refused("front-interlock-unavailable", "levain.slot.start") from None


def front_lock_default(ctx: Ctx, slot: SlotId) -> ContextManager[None]:
    """The slot's ``front.lock``, WAITED for (the reaper's own wait for it), not tried: render's front start queues
    behind a revoker holding it and then reads the marker that revoker wrote, instead of refusing busy."""
    lock, wait_s, _, _ = _front_interlock()
    return lock(ctx, slot, wait_s)


def front_revoked_default(ctx: Ctx, a: R.Alloc) -> bool:
    """Whether the slot's durable ``front-revoked`` marker names THIS lease (session and ``lease_seq``): the bytes
    the revoker writes for it, compared whole, so a marker of another lease or an earlier one does not refuse."""
    _, _, name, marker = _front_interlock()
    return ctx.store.read(("slots", a.slot.tag), name) == marker(a)


@dataclass
class Seams:
    """Every OS act the helper takes that a unit test cannot take as a non-root user, with its real default.

    ``chown(fd, uid, gid, rel)``: ``rel`` is the path below the store the fd names (tests record it). ``at(step)``
    is called before each named step of a verb (tests observe the order and inject faults there)."""

    host: str = field(default_factory=_host)
    store: Optional[Path] = None
    run: Optional[Path] = None
    euid: Callable[[], int] = os.geteuid
    trusted: Callable[[os.stat_result], bool] = _root_trusted
    chown: Callable[[int, int, int, str], None] = _fchown
    #: the owner a dir chowned to ``uid`` carries on disk: ``uid`` itself (a test whose ``chown`` only records maps
    #: every uid to its own, since nothing was chowned).
    chowned_uid: Callable[[int], int] = lambda uid: uid
    fsync: Callable[[int], None] = os.fsync
    free_bytes: Callable[[int], int] = _free_bytes
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    boot_id: Callable[[], str] = read_boot_id
    new_session: Callable[[], str] = _new_session
    account_uid: Callable[[str], int] = _account_uid
    group_gid: Callable[[str], int] = _group_gid
    user_gid: Callable[[int], int] = _user_gid
    client_factory: Callable[[Path], object] = _default_client
    stage_run_as: ST.RunAs = ST.fork_run_as
    spawn_front: Callable[[Sequence[str], bytes, int], int] = spawn_front
    #: render (4)'s interlock with the revoker: the slot's ``front.lock`` (a context manager) and whether its
    #: ``front-revoked`` marker names the lease. The defaults reach the reaper's primitive only when called.
    front_lock: Callable[[Ctx, SlotId], ContextManager[None]] = front_lock_default
    front_revoked: Callable[[Ctx, R.Alloc], bool] = front_revoked_default
    proc_identity: Callable[[int], Optional[Tuple[int, int]]] = proc_identity
    kill: Callable[[int], None] = PR.kill_and_reap
    var_folders: Callable[[int, int], str] = darwin_user_dir
    grant_search: Callable[[str, int, Path, str, int], None] = grant_broker_search
    wake_reaper: Optional[Callable[[], None]] = None
    hook_templates: Mapping[str, RD.HookTemplate] = field(default_factory=dict)
    #: a driver template's bytes by name (3c-8, render's ``driver-template-digest-mismatch``)
    driver_template: Callable[[str], Optional[bytes]] = RD.read_driver_template
    front_module: str = FRONT_MODULE
    #: ``(host, slot, *, run, store, slot_uid) -> argv`` (:func:`gateway_front_args`; :func:`stub_front_args` takes
    #: the same)
    front_args: Callable[..., Sequence[str]] = gateway_front_args
    lock_wait_s: float = LOCK_WAIT_S
    at: Callable[[str], None] = lambda step: None
    # start / teardown (levain.slot.start):
    spawn: Callable[[Sequence[str], bytes, Optional[str], Optional[int], bool], int] = spawn_target
    wait: Callable[[int], int] = wait_child
    #: a child's wait bounded by its second argument, in seconds (:func:`wait_child_within`)
    wait_within: Callable[[int, float], int] = wait_child_within
    #: a post-clone check child: ``(argv, spec_json, cgroup, bound) -> (exit, stdout)`` (:func:`capture_target`)
    capture: Callable[[Sequence[str], bytes, Optional[str], int], Tuple[int, bytes]] = capture_target
    egress_ready: Callable[[SlotId, Mapping[str, int]], None] = wall.macos_egress_ready
    netns_names: Callable[[], Iterable[str]] = wall.read_netns_names
    host_links: Callable[[], Mapping[str, Optional[str]]] = wall.read_host_links
    make_cgroup: Callable[[str], None] = make_cgroup
    #: proc.ProcSeams for END's freeze / kill / proof and SCRUBBED (None = the host's real process table).
    proc_seams: Optional[object] = None
    #: EndSeams fields a test replaces (owner_ok, meta_reader, strip, chown, run_as, ...); empty in production.
    end_overrides: Mapping[str, object] = field(default_factory=dict)
    #: adopt-open's seams (None = the real ones): steps 1-5 (``AdoptSeams``) and steps 6-12 (``AdoptRunSeams``).
    adopt: Optional["AdoptSeams"] = None
    adopt_run: Optional["AdoptRunSeams"] = None
    #: adopt-verify's seams (None = the real ones): its own facts (``VerifySeams``) and the sealed run's
    #: (``AdoptRunSeams``, whose real ``launch`` for this verb is :func:`verify_launch_default`).
    verify: Optional["VerifySeams"] = None
    verify_run: Optional["AdoptRunSeams"] = None
    #: probe's own seams (None = the real ones: ``probe.ProbeSeams``): resolver, CONTROL's connects, the group kill.
    probe: Optional[object] = None

    @property
    def L(self) -> Path:
        return Path(self.store) if self.store is not None else STORE[self.host]

    @property
    def R(self) -> Path:
        return Path(self.run) if self.run is not None else RUN[self.host]


# --- dirfd primitives (§5 preamble) ---------------------------------------------------------------------------------

def _openat_dir(parent: int, name: str, where: str) -> int:
    try:
        return os.open(name, _O_DIR, dir_fd=parent)
    except FileNotFoundError:
        raise Refused("store-missing", where) from None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise Refused("bad-root", f"{where} is not a directory") from None
        raise


def read_at(dirfd: int, name: str, where: str, *, cap: int = _RECORD_CAP) -> Optional[bytes]:
    """A regular file's bytes below ``dirfd``, ``None`` when absent; a link or a non-regular file is ``bad-root``."""
    try:
        fd = os.open(name, _O_READ, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENXIO):
            raise Refused("bad-root", f"{where} is a link or not a file") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("bad-root", f"{where} is not a regular file")
        if st.st_size > cap:
            raise Refused("record-too-large", where)
        out = bytearray()
        while len(out) <= cap:
            chunk = os.read(fd, min(65536, cap + 1 - len(out)))   # never more than cap + 1 in total
            if not chunk:
                break
            out += chunk
        if len(out) > cap:
            raise Refused("record-too-large", where)
        return bytes(out)
    finally:
        os.close(fd)


def exists_regular_at(dirfd: int, name: str) -> bool:
    try:
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)


def replace_at(dirfd: int, name: str, data: bytes, *, mode: int, fsync: Callable[[int], None]) -> None:
    """``records.write_durably`` below a dirfd: a fresh temp (``O_EXCL``, no link followed), written whole, fsynced,
    renamed over ``name``, the directory fsynced. A failure leaves ``name`` as it was."""
    tmp = name + ".tmp"
    try:
        os.unlink(tmp, dir_fd=dirfd)        # a temp a dead writer left
    except FileNotFoundError:
        pass
    fd = os.open(tmp, _O_NEW, mode, dir_fd=dirfd)
    try:
        os.fchmod(fd, mode)
        write_all(fd, data)
        fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp, dir_fd=dirfd)
        raise
    os.close(fd)
    try:
        os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        os.unlink(tmp, dir_fd=dirfd)
        raise
    fsync(dirfd)


class LinkedNotDurable(OSError):
    """:func:`create_at` failed AFTER its link: ``name`` EXISTS (a reader may already see it) but is not proved
    durable. The caller treats the record as created for every later decision (``prepare`` removes it durably before
    it gives any lease back)."""


def create_at(dirfd: int, name: str, data: bytes, *, mode: int, fsync: Callable[[int], None]) -> None:
    """:func:`replace_at` that never replaces: the temp is LINKED to ``name`` (``linkat`` fails on an existing name,
    atomically), so an existing record raises ``FileExistsError`` and is left as it was; a reader never sees a
    partial file. Each writer's temp is its own (``name.<random>.new``, ``O_EXCL``), and a writer only ever unlinks
    the temp it made, so two writers of one name never touch each other's (DECISION: never :func:`replace_at`'s name
    either). Any failure after the link raises :class:`LinkedNotDurable`: the record exists from the link on.
    A writer killed mid-write leaves its temp, which no reader takes for a record."""
    tmp = f"{name}.{os.urandom(8).hex()}.new"
    fd = os.open(tmp, _O_NEW, mode, dir_fd=dirfd)
    try:
        os.fchmod(fd, mode)
        write_all(fd, data)
        fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp, dir_fd=dirfd)
        raise
    os.close(fd)
    try:
        os.link(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        os.unlink(tmp, dir_fd=dirfd)
        raise
    try:
        os.unlink(tmp, dir_fd=dirfd)
        fsync(dirfd)
    except BaseException as e:
        raise LinkedNotDurable(getattr(e, "errno", None) or errno.EIO, f"{name} linked, not durable: {e}") from e


@dataclass(frozen=True)
class DirRow:
    """One §2.1 row a verb makes: ``rel`` below ``<S>`` (``""`` = ``<S>`` itself), owner, group, mode."""

    rel: str
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class FileRow:
    rel: str
    data: bytes
    uid: int
    gid: int
    mode: int


class Store:
    """Root's handle on ``L``: one dirfd opened at the store root, every path below reached by :meth:`dir`
    one ``O_NOFOLLOW`` component at a time. Each directory root acts in must pass ``seams.trusted``."""

    def __init__(self, seams: Seams) -> None:
        self.seams = seams
        self.path = seams.L
        try:
            self.fd = os.open(str(self.path), _O_DIR)
        except OSError:
            raise Refused("store-missing", str(self.path)) from None
        if not seams.trusted(os.fstat(self.fd)):
            os.close(self.fd)
            raise Refused("bad-root", str(self.path))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def open_dir(self, *parts: str, trusted: bool = True) -> int:
        """An fd on ``L/<parts…>``. ``trusted``: the LAST component must be trusted (every intermediate always
        must): its layout row (:func:`layout.store_dir_row`) is root's, or it has none, and the open dir passes
        ``seams.trusted``. A dir whose row another account owns is never trusted, whatever is on disk: that account
        could write where root acts."""
        cur = os.dup(self.fd)
        where = str(self.path)
        try:
            for i, p in enumerate(parts):
                if not p or p in (".", "..") or "/" in p:
                    raise Refused("bad-token", p[:64])
                where += "/" + p
                nxt = _openat_dir(cur, p, where)
                os.close(cur)
                cur = nxt
                if trusted or i < len(parts) - 1:
                    row = store_dir_row(self.seams.host, parts[:i + 1])
                    if row is not None and row.owner != "root":
                        raise Refused("bad-root", f"{where}: its layout row is {row.owner}'s, not root's")
                    if not self.seams.trusted(os.fstat(cur)):
                        raise Refused("bad-root", where)
            return cur
        except BaseException:
            os.close(cur)
            raise

    @contextmanager
    def dir(self, *parts: str, trusted: bool = True) -> Iterator[int]:
        fd = self.open_dir(*parts, trusted=trusted)
        try:
            yield fd
        finally:
            os.close(fd)

    def read(self, parts: Sequence[str], name: str, *, trusted: bool = True, cap: int = _RECORD_CAP) -> Optional[bytes]:
        with self.dir(*parts, trusted=trusted) as fd:
            return read_at(fd, name, "/".join((str(self.path), *parts, name)), cap=cap)

    def replace(self, parts: Sequence[str], name: str, data: bytes, *, mode: int) -> None:
        with self.dir(*parts) as fd:
            replace_at(fd, name, data, mode=mode, fsync=self.seams.fsync)

    def create(self, parts: Sequence[str], name: str, data: bytes, *, mode: int) -> None:
        """A FRESH record (:func:`create_at`): an existing ``name`` raises ``FileExistsError``."""
        with self.dir(*parts) as fd:
            create_at(fd, name, data, mode=mode, fsync=self.seams.fsync)

    def mkdir(self, parent: int, name: str, row_uid: int, row_gid: int, mode: int, rel: str) -> None:
        """A fresh directory ``name`` under ``parent`` with its §2.1 owner and mode (chown first, mode last)."""
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            raise Refused("session-exists", rel) from None
        fd = _openat_dir(parent, name, rel)
        try:
            self.seams.chown(fd, row_uid, row_gid, rel)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def create_file(self, parent: int, name: str, row: FileRow) -> None:
        """A FRESH file (``O_CREAT|O_EXCL``, never over an existing name) with its row's owner and mode, fsynced."""
        try:
            fd = os.open(name, _O_NEW, 0o600, dir_fd=parent)
        except FileExistsError:
            raise Refused("render-dest-exists", row.rel) from None
        try:
            self.seams.chown(fd, row.uid, row.gid, row.rel)
            os.fchmod(fd, row.mode)
            write_all(fd, row.data)
            self.seams.fsync(fd)
        finally:
            os.close(fd)


# --- small record readers ---------------------------------------------------------------------------------------------

def _canon_obj(data: bytes, what: str) -> dict:
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise Refused(f"{what}-malformed", str(e)[:128]) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != data:
        raise Refused(f"{what}-malformed", "not a canonical object")
    return obj


def read_operator_uid(store: Store) -> int:
    from levain.slot.install_helper import parse_operator_record

    data = store.read(("policy",), "operator")
    if data is None:
        raise Refused("no-operator", "L/policy/operator")
    return int(parse_operator_record(data)["uid"])


def configured_slots(store: Store) -> Dict[str, frozenset]:
    """The configured slots, from the store: every ``L/slots/e<n>/`` and ``L/slots/v<n>/`` directory."""
    out: Dict[str, set] = {"egress": set(), "none": set()}
    with store.dir("slots") as fd:
        names = os.listdir(fd)
        for name in names:
            if len(name) < 2 or name[0] not in "ev" or not name[1:].isascii() or not name[1:].isdigit() \
                    or str(int(name[1:])) != name[1:]:
                continue
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                idx = int(name[1:])
                if idx < 64:
                    out["egress" if name[0] == "e" else "none"].add(idx)
    return {k: frozenset(v) for k, v in out.items()}


def base_routes(store: Store) -> Tuple[Dict[str, str], Dict[str, Tuple[str, ...]]]:
    """``L/routes/base.json`` (§4.3 S1 base map), read once: ``(route id -> its §4.1 type, route id -> its hosts)``.
    Interactive and login sessions name base-map ids only (§5.3), so this map is the whole set a render may name. The
    hosts are each route's ``hosts`` list as written (render checks each through the policy's host grammar); an
    ``egress`` route whose ``hosts`` is not a non-empty list of strings is ``base-map-malformed`` (WF-1b: an egress
    route with no hosts is a refusal, never a session that renders no allow rule). A route of another type without a
    ``hosts`` list is given none."""
    data = store.read(("routes",), "base.json")
    if data is None:
        raise Refused("base-map-missing", "L/routes/base.json")
    obj = _canon_obj(data, "base-map")
    routes = obj.get("routes")
    if obj.get("format") not in BASE_ROUTES_FORMATS or type(routes) is not list:
        raise Refused("base-map-malformed", "format or routes")
    types: Dict[str, str] = {}
    hosts: Dict[str, Tuple[str, ...]] = {}
    for r in routes:
        if type(r) is not dict or type(r.get("route")) is not str or r.get("type") not in RD.ROUTE_TYPES \
                or r["route"] in types:
            raise Refused("base-map-malformed", "a route")
        types[r["route"]] = r["type"]
        hs = r.get("hosts")
        ok = type(hs) is list and bool(hs) and all(type(h) is str for h in hs)
        if r["type"] == "egress" and not ok:
            raise Refused("base-map-malformed", f"egress route {r['route'][:64]!r} has no hosts")
        if ok:
            hosts[r["route"]] = tuple(hs)
    return types, hosts


def base_route_types(store: Store) -> Dict[str, str]:
    """:func:`base_routes`' types alone (``start``'s secret names need no hosts)."""
    return base_routes(store)[0]


# --- the slot state file (§3.5 Pool) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotView:
    """What the pick reads of one slot under the pool lock: its state file, which vendors' credential ``keep/``
    holds (by name: §2.1 keep files are moved, never opened), and its baseline's vendor (``"?"`` when the file
    exists but does not parse; ``None`` when absent)."""

    state: Optional[R.SlotState]
    slot: SlotId
    credentials: frozenset = frozenset()
    baseline: Optional[str] = None

    @property
    def reusable(self) -> bool:
        return self.state is not None and self.state.state == "reusable"

    def holds(self, vendor: str) -> bool:
        return vendor in self.credentials and self.baseline == vendor

    @property
    def operator_held(self) -> bool:
        return bool(self.credentials) or self.baseline is not None


def _baseline_vendor(data: Optional[bytes]) -> Optional[str]:
    from levain.slot import identity as ID

    return ID.baseline_vendor(data)


def parse_slot_state_of(data: bytes, slot: SlotId) -> R.SlotState:
    """records' parse, plus the file's own (class, index) must be the directory it sits in."""
    st = R.parse_slot_state(data)
    if st.slot != slot:
        raise Refused("slot-state-malformed", f"{slot.tag} holds {st.slot.tag}'s state")
    return st


def read_view(store: Store, slot: SlotId) -> SlotView:
    with store.dir("slots", slot.tag) as sfd:
        raw = read_at(sfd, "state", f"slots/{slot.tag}/state")
        state = parse_slot_state_of(raw, slot) if raw is not None else None   # DECISION: absent = not reusable
        creds: frozenset = frozenset()
        baseline = None
        if slot.slot_class == "egress":
            try:
                kfd = _openat_dir(sfd, "keep", f"slots/{slot.tag}/keep")
            except Refused:
                kfd = -1
            if kfd >= 0:
                try:
                    creds = frozenset(v for v, n in CREDENTIAL_NAME.items() if exists_regular_at(kfd, n))
                finally:
                    os.close(kfd)
            baseline = _baseline_vendor(read_at(sfd, "baseline", f"slots/{slot.tag}/baseline"))
    return SlotView(state, slot, creds, baseline)


def pick_slot(views: Sequence[SlotView], *, slot_class: str, kind: str, vendor: str, auth: str,
              named: Optional[int], ticketed: bool) -> SlotId:
    """§5.1 prepare + §3.5 Pool's point-of-use rules, PURE over the views read under the pool lock.

    Operator: ``no-baseline:<slot>`` for ``passthrough`` + kind != ``login`` when no REUSABLE slot holds this
    vendor's credential AND a baseline; then the named slot if REUSABLE (a named slot that is not is refused
    ``slot-not-reusable:<tag>``, never replaced: naming it asks for its credential), else (passthrough) the lowest
    credential + baseline slot, else the lowest REUSABLE (verifier: the lowest free pair). Ticketed: no ``<slot>`` argument,
    never a slot whose ``keep/`` holds a credential or that has a baseline, and ``operator-slot-reserved`` if
    taking the slot would leave no reusable credential-free egress slot."""
    mine = sorted((v for v in views if v.slot.slot_class == slot_class), key=lambda v: v.slot.index)
    reusable = [v for v in mine if v.reusable]
    if ticketed:
        if named is not None:
            raise Refused("ticket-slot-argument", f"{slot_class} {named}")
        free = [v for v in reusable if not v.operator_held]
        if not free:
            raise Refused(f"no-free-slot:{slot_class}", "no reusable credential-free slot")
        if slot_class == "egress" and len(free) < 2:
            raise Refused("operator-slot-reserved", "the last credential-free egress slot is the operator's")
        return free[0].slot
    if auth == "passthrough" and kind != "login" and not any(v.holds(vendor) for v in reusable):
        # DECISION: <slot> is the named slot's tag (e<n>), or the class when none was named.
        raise Refused(f"no-baseline:{SlotId(slot_class, named).tag if named is not None else slot_class}",
                      f"no reusable slot holds a {vendor} credential and baseline")
    if named is not None:
        for v in reusable:
            if v.slot.index == named:
                return v.slot
        raise Refused(f"slot-not-reusable:{SlotId(slot_class, named).tag}", "the named slot is not reusable")
    if auth == "passthrough":
        held = [v for v in reusable if v.holds(vendor)]
        if held:
            return held[0].slot
    if not reusable:
        raise Refused(f"no-free-slot:{slot_class}", "no reusable slot")
    return reusable[0].slot


def slot_snapshot(store: Store) -> List[Dict[str, object]]:
    """``SLOT-SYNC``'s payload (§3.5 Pool): every configured slot's state file as its ``levain-slot-state/1`` object,
    egress then none, by index. The caller holds the pool lock (§3.5: every writer of a state file does), so the set
    is one consistent read. A slot with no state file, or one that does not parse, is left out: absent from the snapshot is
    NOT REUSABLE to CONTROL, the safe side."""
    out: List[Dict[str, object]] = []
    conf = configured_slots(store)
    for cls in ("egress", "none"):
        for i in sorted(conf[cls]):
            slot = SlotId(cls, i)
            try:
                raw = store.read(("slots", slot.tag), "state")
                if raw is None:
                    continue
                out.append(canon.parse(R.slot_state_bytes(parse_slot_state_of(raw, slot))))
            except Refused:
                continue
    return out


# --- flock (pool lock before slot lock, §3.5 LOCK ORDER) -------------------------------------------------------------

def _open_lock(dirfd: int, name: str, where: str) -> int:
    try:
        return os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    except FileNotFoundError:
        raise Refused("store-missing", where) from None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("bad-root", where) from None
        raise


def try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise


def wait_flock(fd: int, seams: Seams, name: str) -> None:
    """LOCK_EX within ``seams.lock_wait_s`` (backoff doubling to 0.5 s), else ``Refused(name)``."""
    deadline = seams.monotonic() + seams.lock_wait_s
    delay = LOCK_SLICE_S
    while not try_flock(fd):
        if seams.monotonic() >= deadline:
            raise Refused(name, "held past the wait")
        seams.sleep(delay)
        delay = min(delay * 2, 0.5)


@dataclass
class Leased:
    """A slot ``prepare`` took: its new state (written) and its HELD slot lock fd (released after ATTACH)."""

    slot: SlotId
    state: R.SlotState
    lock_fd: int
    #: the state file as it was before this lease (what a clean ``prepare`` failure puts back).
    prior: Optional[R.SlotState] = None

    def release(self) -> None:
        if self.lock_fd >= 0:
            os.close(self.lock_fd)       # closing the fd drops the flock
            self.lock_fd = -1


def lease_slot(store: Store, *, slot_class: str, kind: str, vendor: str, auth: str, named: Optional[int],
               ticketed: bool, session: str) -> Leased:
    """§3.5 Pool ORDER: the pool lock, then the slot lock; ``leased`` with ``lease_seq + 1`` and the session, durable;
    the pool lock released; the slot lock returned HELD. DECISION: under the pool lock the slot lock is only TRIED
    (a holder is mid-END or mid-revert): a held slot is passed over as not reusable and the pick runs again, so
    the pool lock never waits on a slot lock."""
    seams = store.seams
    with store.dir("slots") as slots_fd:
        pool = _open_lock(slots_fd, "pool.lock", "slots/pool.lock")
        try:
            wait_flock(pool, seams, "pool-locked")
            seams.at("pool-locked")
            indices = configured_slots(store)[slot_class]
            views = [read_view(store, SlotId(slot_class, i)) for i in sorted(indices)]
            busy: set = set()
            while True:
                live = [v if v.slot not in busy else replace(v, state=None) for v in views]
                slot = pick_slot(live, slot_class=slot_class, kind=kind, vendor=vendor, auth=auth, named=named,
                                 ticketed=ticketed)
                with store.dir("slots", slot.tag) as sfd:
                    lock = _open_lock(sfd, "lock", f"slots/{slot.tag}/lock")
                if try_flock(lock):
                    break
                os.close(lock)
                busy.add(slot)
            try:
                old = next(v.state for v in views if v.slot == slot)
                assert old is not None
                new = replace(old, rev=old.rev + 1, lease_seq=old.lease_seq + 1, state="leased", session=session,
                              changed_at=R.timestamp(seams.clock()))
                seams.at("slot-leased")
                store.replace(("slots", slot.tag), "state", R.slot_state_bytes(new), mode=0o644)
            except BaseException:
                os.close(lock)
                raise
        finally:
            os.close(pool)                 # the pool lock is released here; the slot lock stays held
    return Leased(slot, new, lock, old)


def revert_lease(store: Store, leased: Leased) -> bool:
    """Put back a lease a CLEAN ``prepare`` failure leaves behind (nothing ran in the slot, CONTROL holds no record,
    the alloc is gone): the prior state at ``rev + 1``, ``lease_seq`` kept (it is never reset, §3.5). Only if the state
    file is still EXACTLY the lease this ``prepare`` wrote; anything else is left as it is. §3.5 LOCK ORDER: the
    slot lock is held here, so the pool lock is only TRIED; on failure the slot lock is released, the pool lock taken,
    and the slot lock TRIED again (a holder now = someone else's: leave it). Returns whether it wrote. A slot this
    leaves leased has no alloc: the reaper's orphan revert (§3.5) is its recovery.

    This is the one write off ``leased`` with no ``end_domain`` before it (reaper DESIGN §3.1, invariant I1's stated
    exception, RH7), and it holds only because nothing has run in the slot: no spawn into a slot uid happens before
    the lease commit (the front spawns in render, after ATTACH). Condition: if any spawn ever moves before the lease
    commit, the revert must first run ``end_domain`` on the gateway uid under ``front.lock``."""
    seams = store.seams
    prior = leased.prior
    if prior is None:
        return False
    with store.dir("slots") as slots_fd:
        pool = _open_lock(slots_fd, "pool.lock", "slots/pool.lock")
        try:
            if not try_flock(pool):
                leased.release()
                wait_flock(pool, seams, "pool-locked")
            if leased.lock_fd < 0:
                with store.dir("slots", leased.slot.tag) as sfd:
                    lock = _open_lock(sfd, "lock", f"slots/{leased.slot.tag}/lock")
                if not try_flock(lock):
                    os.close(lock)
                    return False
                leased.lock_fd = lock
            seams.at("revert-locked")
            data = store.read(("slots", leased.slot.tag), "state")
            if data is None or parse_slot_state_of(data, leased.slot) != leased.state:
                return False
            back = replace(leased.state, rev=leased.state.rev + 1, state=prior.state, session=prior.session,
                           changed_at=R.timestamp(seams.clock()))
            store.replace(("slots", leased.slot.tag), "state", R.slot_state_bytes(back), mode=0o644)
            return True
        finally:
            os.close(pool)


# --- the caller context -------------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    seams: Seams
    store: Store
    sudo_uid: int
    caller: str
    operator_uid: int
    env: Mapping[str, str]
    _client: object = None

    @property
    def client(self):
        if self._client is None:
            self._client = self.seams.client_factory(self.seams.R / "control" / "control.sock")
        return self._client

    def call(self, op: str, **fields: object) -> dict:
        return self.client.call(op, **fields)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:   # noqa: BLE001 - closing never changes the verb's answer
                pass


def slot_ids(ctx: Ctx, slot: SlotId) -> Dict[str, int]:
    """The two accounts of ``slot`` (egress: slot + front; none: driver + child), each in its §1.1 range."""
    return {name: account_in_range(ctx.seams, name) for name in slot.accounts}


def account_in_range(seams: "Seams", name: str) -> int:
    """One slot account's uid (``seams.account_uid``), refused ``account-out-of-range`` outside its §1.1 range."""
    uid = seams.account_uid(name)
    if not in_own_range(seams.host, name, uid):
        raise Refused("account-out-of-range", f"{name} {uid!r}")
    return uid


def read_alloc(ctx: Ctx, session: str) -> R.Alloc:
    """The alloc of ``session``, bound to its caller (DECISION: a lease verb on a session is the lease peer's that
    prepared it: ``supervisor_uid == SUDO_UID``; ``_levain_dispatch`` only on an alloc carrying a ticket, §5.1).
    Anything else answers ``no-session``, so a caller learns nothing of another's sessions."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    a = R.parse_alloc(data)
    if a.session != session or a.supervisor_uid != ctx.sudo_uid or (ctx.caller == DISPATCH and a.ticket is None):
        raise Refused("no-session", session)
    return a


def write_alloc(ctx: Ctx, a: R.Alloc) -> None:
    """An alloc update (render's policy and front facts): replaces the record ``prepare`` created."""
    ctx.store.replace(("alloc",), f"{a.session}.json", R.alloc_bytes(a), mode=0o600)


def create_alloc(ctx: Ctx, a: R.Alloc) -> None:
    """``prepare``'s alloc: created FRESH, never over an existing session's record (``session-exists``)."""
    try:
        ctx.store.create(("alloc",), f"{a.session}.json", R.alloc_bytes(a), mode=0o600)
    except FileExistsError:
        raise Refused("session-exists", a.session) from None


def session_lock_name(session: str) -> str:
    """Root's per-session lock below ``L/alloc/``: the SAME file END takes (``end.EndFacts.lock``), so a render or
    resume and an END of one session never overlap."""
    return f"{session}.lock"


@contextmanager
def session_lock(ctx: Ctx, session: str) -> Iterator[None]:
    """Root's per-session TRY-lock (CONTRACT §5.1 render is one transaction per session): held = ``session-busy``.
    The lock file is never removed (as END's)."""
    name = session_lock_name(session)
    with ctx.store.dir("alloc") as afd:
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600,
                         dir_fd=afd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENXIO):
                raise Refused("bad-root", f"alloc/{name} is a link or not a file") from None
            raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not ctx.seams.trusted(st):
            raise Refused("bad-root", f"alloc/{name}")
        if not try_flock(fd):
            raise Refused("session-busy", session)
        yield
    finally:
        os.close(fd)


def _session_taken(store: Store, session: str) -> bool:
    """Whether any record or tree of ``session`` already exists (a fresh id is checked before any slot is leased;
    :func:`create_alloc` is what holds against a race)."""
    for rel in (("alloc", f"{session}.json"), ("alloc", f"{session}.intent.json"), ("sessions", f"{session}.json"),
                ("live", session), ("staging", session), ("retained", session)):
        try:
            os.lstat(store.path.joinpath(*rel))
            return True
        except FileNotFoundError:
            continue
    return False


# --- prepare (§5.1) -----------------------------------------------------------------------------------------------------

def session_dir_rows(slot: SlotId, ids: Mapping[str, int]) -> List[DirRow]:
    """§2.1's rows ``prepare`` makes under ``<S>``, in creation order. Egress: ``<S>`` root : ``_levain_s<n>`` 0750,
    the slot-owned 0700 dirs. Verifier: ``<S>`` root : root 0711, ``driver/`` ``_levain_v<n>`` 0700, the rest the
    CHILD's. DECISION: the parents ``config/`` and ``xdg/`` (no §2.1 row) are root : the owner's group 0750, so the
    owner reaches its dirs and cannot rename or replace them."""
    first, second = slot.accounts
    if slot.slot_class == "egress":
        owner = ids[first]
        rows = [DirRow("", 0, owner, 0o750)]
    else:
        owner = ids[second]
        rows = [DirRow("", 0, 0, 0o711), DirRow("driver", ids[first], ids[first], 0o700)]
    rows += [DirRow("config", 0, owner, 0o750), DirRow("config/claude", owner, owner, 0o700),
             DirRow("config/codex", owner, owner, 0o700), DirRow("xdg", 0, owner, 0o750)]
    rows += [DirRow(f"xdg/{d}", owner, owner, 0o700) for d in ("data", "cache", "config", "state")]
    rows += [DirRow("tmp", owner, owner, 0o700), DirRow("sock", owner, owner, 0o700)]
    return rows


def make_session_dirs(ctx: Ctx, session: str, rows: Sequence[DirRow], *, broker_uid: Optional[int] = None) -> None:
    """The rows under ``<S>``; with ``broker_uid`` (a ``dispatch`` session, looked up by ``prepare`` before ATTACH)
    ``<S>`` also gets the queue broker's search entry (§26 S1-c), so the broker can reach ``<S>/repo`` (0711 at
    start) and nothing else under ``<S>`` by mode."""
    with ctx.store.dir("live") as live:
        for row in rows:
            parts = [p for p in row.rel.split("/") if p]
            if not parts:
                ctx.store.mkdir(live, session, row.uid, row.gid, row.mode, f"live/{session}")
                if broker_uid is not None:
                    with ctx.store.dir("live", session) as sfd:
                        ctx.seams.grant_search(ctx.seams.host, sfd, ctx.store.path / "live" / session,
                                               QBROKER, broker_uid)
                continue
            with ctx.store.dir("live", session, *parts[:-1]) as parent:
                ctx.store.mkdir(parent, parts[-1], row.uid, row.gid, row.mode, f"live/{session}/{row.rel}")
        ctx.seams.fsync(live)


def make_staging(ctx: Ctx, session: str) -> Path:
    """``L/staging/<session>/`` owned by the LEASE PEER (alloc ``staging_uid``) 0700 (§2.1, §5.4)."""
    with ctx.store.dir("staging") as fd:
        ctx.store.mkdir(fd, session, ctx.sudo_uid, ctx.seams.user_gid(ctx.sudo_uid), 0o700, f"staging/{session}")
        ctx.seams.fsync(fd)
    return ctx.store.path / "staging" / session


def prepare(ctx: Ctx, req: RuntimeRequest, blob: PrepareInput) -> Dict[str, object]:
    """§5.1 prepare. TICKETED (S1 3c-7, §3.5): ``_levain_dispatch``'s lease carries a ticket (``bind_stdin`` holds
    caller and ticket together), the slot is taken by :func:`pick_slot`'s ticketed rules under the pool lock (no
    ``<slot>`` argument, never a credential or baseline slot, ``operator-slot-reserved`` for the operator's last
    one), alloc and the ``ATTACH`` claims name the ticket, and ``ATTACH``'s ``ticket_facts`` are written into alloc,
    durably, before ``prepare`` returns. A failure after ``ATTACH`` answered (the facts write included) leaves CONTROL's
    record ALLOCATED with the ticket bound and an alloc carrying the ticket without its facts: render refuses that
    ``ticket-facts-missing`` before it acts (:func:`levain.slot.render.check_ticket`), so nothing renders, and the
    session ends with its lease."""
    seams = ctx.seams
    ticketed = blob.ticket is not None
    if ticketed != (ctx.caller == DISPATCH):
        raise Refused("ticket-required" if ctx.caller == DISPATCH else "ticket-not-allowed", "prepare")
    seams.at("floor")
    if seams.free_bytes(ctx.store.fd) < V5_FLOOR_BYTES:        # §5.1: refuses below V5
        raise Refused("below-floor", "free space under V5")
    for _ in range(SESSION_ID_TRIES):
        session = seams.new_session()
        if not SESSION_RE.fullmatch(session):
            raise Refused("internal", "session id")
        if not _session_taken(ctx.store, session):
            break
    else:
        raise Refused("internal", "no fresh session id")
    leased = lease_slot(ctx.store, slot_class=req.slot_class, kind=req.kind, vendor=req.vendor, auth=req.auth,
                        named=req.slot, ticketed=ticketed, session=session)
    created = attach_sent = False
    try:
        slot = leased.slot
        ids = slot_ids(ctx, slot)
        first, second = slot.accounts
        linux = seams.host == "linux"
        # A missing broker account refuses here, where the lease is still given back (§26 S1-c).
        broker_uid = seams.account_uid(QBROKER) if req.kind == "dispatch" else None
        folders = None
        if seams.host == "darwin":
            # Read ONCE, here, while the uid holds no session: getconf runs AS the uid and creates the dir when it
            # is absent, so a later read (a re-run END after the dir moved) would re-create it (D1).
            folders = (seams.var_folders(ids[first], ids[first]),) if slot.slot_class == "egress" else \
                (seams.var_folders(ids[second], ids[second]), seams.var_folders(ids[first], ids[first]))
        alloc = R.Alloc(
            session=session, slot=slot, lease_seq=leased.state.lease_seq, slot_rev=leased.state.rev,
            slot_uid=ids[first], slot_gid=ids[first], kind=req.kind, vendor=req.vendor, auth=req.auth,
            operator_uid=ctx.operator_uid, supervisor_uid=ctx.sudo_uid, staging_uid=ctx.sudo_uid,
            created_at=R.timestamp(seams.clock()), boot_id=seams.boot_id(),
            child_uid=ids[second] if slot.slot_class == "none" else None,
            netns=slot.netns if linux else None, cgroup=R.cgroup_path(slot, session) if linux else None,
            var_folders=folders, ticket=blob.ticket)
        seams.at("alloc")
        try:
            create_alloc(ctx, alloc)
        except LinkedNotDurable:
            created = True                           # the alloc exists from its link on (it names the slot)
            raise
        created = True
        claims = {"operator_uid": alloc.operator_uid, "supervisor_uid": alloc.supervisor_uid,
                  "slot": R.slot_obj(slot), "class": slot.slot_class, "kind": alloc.kind, "vendor": alloc.vendor,
                  "auth": alloc.auth, "lease_seq": alloc.lease_seq, "slot_rev": alloc.slot_rev}
        if ticketed:
            claims["ticket"] = blob.ticket
        seams.at("attach")
        attach_sent = True
        try:
            ans = ctx.call("ATTACH", lease_nonce=blob.lease_nonce, sudo_uid=ctx.sudo_uid, session=session,
                           claims=claims)
        except Refused as e:
            if e.name not in _ATTACH_AMBIGUOUS:
                attach_sent = False                  # CONTROL refused it: it created nothing
            raise
        if ans.get("generation") != 0:
            raise Refused("attach-answer-malformed", repr(ans.get("generation"))[:32])
        if ticketed:
            # §3.5 "Ticket facts reach render": CONTROL's ticket record, never dispatch's word, into alloc before the
            # answer; a refusal here leaves the session at CONTROL and an alloc without the facts
            if "ticket_facts" not in ans:
                raise Refused("attach-answer-malformed", "no ticket_facts for a ticketed ATTACH")
            seams.at("ticket-facts")
            write_alloc(ctx, R.parse_alloc(R.alloc_bytes(replace(alloc, ticket_facts=ans["ticket_facts"]))))
    except BaseException:
        if not attach_sent:
            _undo_prepare(ctx, leased, session if created else None)
        raise
    finally:
        leased.release()           # §3.5: only after ATTACH answers or prepare gives up
    if seams.wake_reaper is not None:
        seams.wake_reaper()
    else:
        wake_reaper(seams.R)
    seams.at("session-dirs")
    make_session_dirs(ctx, session, session_dir_rows(slot, ids), broker_uid=broker_uid)
    staging = make_staging(ctx, session)
    out: Dict[str, object] = {"format": PREPARE_RESULT_FORMAT, "session": session, "slot": R.slot_obj(slot),
                              "staging": str(staging)}
    if slot.slot_class == "egress":
        out["gwport"] = RD.gwport_for(seams.host, slot)
    return out


def _undo_prepare(ctx: Ctx, leased: Leased, created: Optional[str]) -> None:
    """A CLEAN prepare failure (CONTROL holds no record): the alloc it created removed DURABLY (unlinked, the directory
    fsynced), then the slot lease put back (:func:`revert_lease`), in that order, so a crash between them leaves the
    reaper's alloc-less orphan, never an alloc naming a reusable slot. Best effort: the refusal that got here is the
    answer. An alloc removal that is not proved durable (any failure, the alloc already gone included) gives nothing
    back: the slot stays leased for the reaper (fail closed)."""
    if created is not None:
        try:
            with ctx.store.dir("alloc") as afd:
                os.unlink(f"{created}.json", dir_fd=afd)
                ctx.seams.fsync(afd)
        except (OSError, Refused):
            return
    try:
        revert_lease(ctx.store, leased)
    except (OSError, Refused):
        pass


# --- render (§5.1, in its ORDER (1)-(6)) ----------------------------------------------------------------------------

@dataclass
class RenderRun:
    """One render's moving parts: the generation it holds, the front it started (killed on any later refusal)."""

    ctx: Ctx
    alloc: R.Alloc
    blob: RenderBlob
    generation: int = 0
    facts: Optional[RD.RenderFacts] = None
    front_uid: Optional[int] = None
    #: egress: ``R/gw/slot<n>/session.json``'s bytes (:func:`front_policy_bytes`), built before step (1), written in (3)
    front_policy: Optional[bytes] = None
    front_pid: Optional[int] = None
    front_start: Optional[int] = None
    go_w: int = -1
    #: step (1)'s CAS may have moved the record: set as the CAS is sent, cleared only by a refusal in
    #: :data:`CAS_UNMOVED`. A lost reply leaves ``generation`` 0 with the record RENDERING, so it is this, not the
    #: generation, that decides the REVOKE.
    cas_may_have_landed: bool = False

    @property
    def egress(self) -> bool:
        return self.alloc.slot.slot_class == "egress"


def render_facts(ctx: Ctx, alloc: R.Alloc, blob: RenderBlob) -> RD.RenderFacts:
    """The alloc facts ``render.render`` needs (§7.1: root acts on alloc only), plus the base map's route types and
    hosts (one read, :func:`base_routes`), the slot's /var/folders (macOS), ``TERM`` from the supervisor's env when it
    is a plain terminal name, and the hook templates the blob names (a missing one is refused by name in
    ``render.check_facts``)."""
    seams = ctx.seams
    slot = alloc.slot
    egress = slot.slot_class == "egress"
    owner = alloc.slot_uid if egress else alloc.child_uid
    assert owner is not None
    folders: Tuple[str, ...] = ()
    if seams.host == "darwin":
        if alloc.var_folders is None:
            raise Refused("var-folders-unknown", alloc.session)
        folders = tuple(alloc.var_folders)
    term = ctx.env.get("TERM")
    if term is not None and not RD._TERM_RE.fullmatch(term):
        term = None
    route_types, route_hosts = base_routes(ctx.store)
    return RD.RenderFacts(
        host=seams.host, session=alloc.session, slot=slot, kind=alloc.kind, vendor=alloc.vendor, auth=alloc.auth,
        slot_uid=owner, slot_gid=owner, gwport=RD.gwport_for(seams.host, slot) if egress else None,
        route_types=route_types, route_hosts=route_hosts, term=term, var_folders=folders,
        driver_uid=None if egress else alloc.slot_uid, driver_gid=None if egress else alloc.slot_gid,
        hook_templates={k: v for k, v in seams.hook_templates.items() if k in blob.hooks})


#: the CAS refusals CONTROL answers only after reading the record under its lock and writing nothing
#: (``Control.cas``: the record's generation, then its state): the one proof that step (1) moved nothing.
CAS_UNMOVED = frozenset({"stale-generation", "from-mismatch"})


def render_step1_cas_rendering(run: RenderRun) -> None:
    """(1) CAS ALLOCATED -> RENDERING. An ALLOCATED record is always generation 0 (ATTACH creates it there and
    any later write moves it out of ALLOCATED or ends its lease). Refused = stop: nothing started."""
    run.cas_may_have_landed = True
    try:
        ans = run.ctx.call("CAS", session=run.alloc.session, generation=0,
                           **{"from": R.ALLOCATED, "to": R.RENDERING}, fields={})
    except Refused as e:
        if e.name in CAS_UNMOVED:
            run.cas_may_have_landed = False
        raise
    run.generation = int(ans["generation"])


def render_step2_policy(run: RenderRun) -> None:
    """(2) Validate the (class, kind, vendor, auth, routes, hooks) matrix and write the policy into alloc with
    ``front:{uid, pending:true}`` (egress), fsync. DECISION: ``incarnation`` = the slot's ``lease_seq``, unique per
    lease of the slot (the contract types it nowhere; records.py reads it as a uint)."""
    ctx, a, blob = run.ctx, run.alloc, run.blob
    facts = run.facts if run.facts is not None else render_facts(ctx, a, blob)
    RD.check_facts(blob, facts)
    run.facts = facts
    if run.egress:
        run.front_uid = slot_ids(ctx, a.slot)[a.slot.accounts[1]]
    egress_routes = tuple(r for r in blob.routes if facts.route_types[r] == "egress")
    policy = R.AllocPolicy(routes=tuple(blob.routes), egress_routes=egress_routes,
                           queue_grant="levain_queue" in blob.hooks, hooks=frozenset(blob.hooks),
                           incarnation=a.lease_seq, model=blob.model, verifier_steps=blob.verifier_steps,
                           front=R.Front(run.front_uid, True) if run.egress else None)
    run.alloc = replace(a, policy=policy)
    write_alloc(ctx, run.alloc)


def render_layer_rows(slot: SlotId, owner_gid: int, kind: str) -> List[DirRow]:
    """§2.1 rows ``render`` makes: ``<S>/in/`` root : the slot (verifier: the child) 0750; ``<S>/root/`` root : the
    slot 0750 (verifier root : root 0755, as a none-class layer is world-readable, §2.1). DECISION: ``root/hooks/``
    takes ``root/``'s row. Kind ``dispatch|planner|verifier`` adds the clone template's two dirs,
    ``root/readset-template/`` and its ``info/``, root : root 0755 (§2.1, §5.4 step 3)."""
    if slot.slot_class == "egress":
        layer = (0, owner_gid, 0o750)
    else:
        layer = (0, 0, 0o755)
    rows = [DirRow("in", 0, owner_gid, 0o750), DirRow("root", *layer), DirRow("root/hooks", *layer)]
    if kind in RD.READSET_KINDS:
        rows += [DirRow(RD.READSET_TEMPLATE, 0, 0, 0o755), DirRow(f"{RD.READSET_TEMPLATE}/info", 0, 0, 0o755)]
    return rows


#: The §2.1 dirs ``prepare`` makes owned by the SESSION OWNER (egress: the slot; verifier: the child) that ``render``
#: writes into: the onboarding pre-seed's. Every other render destination is root's and passes ``seams.trusted``.
SLOT_OWNED_RENDER_DIRS = frozenset({"config/claude", "config/codex"})


def session_owner_uid(a: R.Alloc) -> int:
    """The uid that owns the session's vendor dirs (§2.1), from alloc: egress the slot uid, verifier the child uid."""
    owner = a.slot_uid if a.slot.slot_class == "egress" else a.child_uid
    if type(owner) is not int or owner <= 0:
        raise Refused("bad-root", f"alloc {a.session} names no session owner")
    return owner


def slot_owned_dir_ok(st: os.stat_result, owner_uid: int) -> bool:
    """A dir root may create a file in although the session owns it: a directory owned by EXACTLY ``owner_uid`` (this
    session's, from alloc; never any other slot account, never root's check) and neither group- nor other-writable."""
    return stat.S_ISDIR(st.st_mode) and owner_uid > 0 and st.st_uid == owner_uid and not st.st_mode & 0o022


def open_slot_owned_dir(parent: int, name: str, owner_uid: int, where: str) -> int:
    """``name`` below the root-owned ``parent`` dirfd, ``O_NOFOLLOW|O_DIRECTORY``, its owner and mode checked on the
    opened fd (:func:`slot_owned_dir_ok`), else ``bad-root``. The owner cannot swap it after the check: its parent is
    root's and not writable by it (§2.1 ``config/`` root : the owner's group 0750)."""
    fd = _openat_dir(parent, name, where)
    try:
        if not slot_owned_dir_ok(os.fstat(fd), owner_uid):
            raise Refused("bad-root", f"{where} is not this session's owner's alone")
        return fd
    except BaseException:
        os.close(fd)
        raise


def render_step3_files(run: RenderRun) -> None:
    """(3) The staging bundle -> a FRESH ``<S>/in/repo.bundle`` (§5.4: the reader runs as alloc ``staging_uid``),
    its header parsed in the copy (§5a); then ``<S>/root/*`` and the onboarding pre-seed from ``render.render``, each
    a fresh file with its §2.1 row; then staging is emptied, only now that every staged file's destination is durable;
    and only then ``alloc.base`` is written from the parsed header."""
    ctx, a = run.ctx, run.alloc
    seams = ctx.seams
    facts = run.facts
    assert facts is not None
    rendered = RD.render(run.blob, facts)
    S = ("live", a.session)
    for row in render_layer_rows(a.slot, facts.slot_gid, a.kind):
        *parent, name = row.rel.split("/")
        with ctx.store.dir(*S, *parent) as pfd:
            ctx.store.mkdir(pfd, name, row.uid, row.gid, row.mode, f"live/{a.session}/{row.rel}")
            seams.fsync(pfd)
    # §5a: the header is parsed by bundlefmt as the bytes are copied (an incremental observer, never the whole body
    # in memory); a header off the grammar or the base rule refuses inside the copy, so the staged file is removed.
    obs = BF.HeaderObserver(check=BF.base_of)
    with ctx.store.dir("staging", a.session, trusted=False) as staging, ctx.store.dir(*S, "in") as dest:
        staged = ST.stage_bundle(staging, dest, staging_uid=a.staging_uid,
                                 staging_gid=seams.user_gid(a.staging_uid), dest_gid=facts.slot_gid,
                                 run_as=seams.stage_run_as, fsync=seams.fsync, observe=obs.feed,
                                 chown=lambda fd, u, g: seams.chown(fd, u, g,
                                                                    f"live/{a.session}/in/{ST.BUNDLE_NAME}"))
    header = obs.finish()
    oid, ref = BF.base_of(header)
    owner = session_owner_uid(a)
    for f in rendered.files:
        *parent, name = f.path.split("/")
        row = FileRow(f"live/{a.session}/{f.path}", f.data, f.uid, f.gid, f.mode)
        if "/".join(parent) not in SLOT_OWNED_RENDER_DIRS:
            with ctx.store.dir(*S, *parent) as pfd:
                ctx.store.create_file(pfd, name, row)
                seams.fsync(pfd)
            continue
        # The pre-seed: its dir is the session owner's (root's trusted check refuses it), so it is opened from its
        # root-owned parent and checked against alloc's owner; the file is created O_EXCL there and chowned to it.
        if f.uid != owner:
            raise Refused("bad-root", f"{row.rel} is rendered for uid {f.uid}, the session owner is {owner}")
        *up, last = parent
        with ctx.store.dir(*S, *up) as gfd:
            pfd = open_slot_owned_dir(gfd, last, seams.chowned_uid(owner), f"live/{a.session}/{'/'.join(parent)}")
        try:
            ctx.store.create_file(pfd, name, row)
            seams.fsync(pfd)
        finally:
            os.close(pfd)
    if run.egress:                      # §5.1 render (3): the front's session.json, before step (4) starts it
        assert run.front_uid is not None
        if run.front_policy is None:
            run.front_policy = front_policy_bytes(ctx.store, a, run.blob.routes)
        write_front_policy(seams, a.slot, run.front_uid, run.front_policy)
        prepare_front_log(ctx.store, a.slot, run.front_uid)
    # DECISION: only the names this render staged are removed (the verifier's driver_input.json is session 2f's to
    # stage, so it is never removed here unstaged); the dir itself is END's ("staging deleted").
    with ctx.store.dir("staging", a.session, trusted=False) as staging:
        os.unlink(ST.BUNDLE_NAME, dir_fd=staging)
        seams.fsync(staging)
    # alloc.base from the bytes root itself copied, written last: a base present means step (3) completed, so a
    # render refused or faulted anywhere earlier in this step leaves none and adopt answers base-unrecorded. It is
    # written before RENDERED (alloc is immutable after it); later alloc writes carry it, being replaces of run.alloc.
    run.alloc = replace(a, base=R.AllocBase(oid=oid, ref_b64=base64.b64encode(ref).decode("ascii"),
                                            object_format=header.object_format,
                                            bundle_sha256=staged.digest, bundle_bytes=staged.size))
    write_alloc(ctx, run.alloc)


def front_launch(run: RenderRun) -> Tuple[Tuple[str, ...], bytes]:
    """wall's FRONT path (§8.2 last sentence): drop + assert to ``_levain_gw<n>``, no profile, no ``asuser``; the
    program is ``seams.front_module`` with root's fixed ``seams.front_args``, and its one extra fd is the GO pipe
    on :data:`GO_FD`."""
    ctx, a = run.ctx, run.alloc
    seams = ctx.seams
    gw = a.slot.accounts[1]
    spec = wall.launch_spec(seams.host, "front", slot=a.slot, session=a.session, ids={gw: run.front_uid},
                            shared={"levain-gwfronts": seams.group_gid("levain-gwfronts")},
                            argv=tuple(seams.front_args(seams.host, a.slot, run=seams.R, store=seams.L,
                                                        slot_uid=a.slot_uid)),
                            extra_env={GO_FD_ENV: str(GO_FD)},
                            keep_fds=(GO_FD,), front_module=seams.front_module)
    if seams.host == "darwin":
        argv = wall.macos_launch_argv(spec)
    else:
        argv = wall.linux_launch_argv(spec, slot=a.slot, session=a.session, kind=a.kind).argv
    return argv, spec.to_json()


#: DECISION [judged]: render (4)'s bound on the spawned front's drop to the gateway uid while it holds ``front.lock``
#: (two interpreter starts, the stage's and the front's), below the revoker's own retry bound and SCRUB's lock wait.
FRONT_DROP_WAIT_S = 3.0


def front_dropped(run: RenderRun) -> int:
    """Under ``front.lock``: wait until the spawned front's real uid is the gateway uid (the stage dropped and
    asserted) and return its start time. Unknown, any other uid, or still root at :data:`FRONT_DROP_WAIT_S` refuses
    ``front-start-unknown``; render's failure path then kills the child (it may still be root there)."""
    seams = run.ctx.seams
    assert run.front_pid is not None
    deadline = seams.monotonic() + FRONT_DROP_WAIT_S
    delay = 0.01
    while True:
        ident = seams.proc_identity(run.front_pid)
        if ident is not None and ident[0] == run.front_uid:
            return ident[1]
        if ident is None or ident[0] != 0 or seams.monotonic() >= deadline:
            raise Refused("front-start-unknown", str(run.front_pid))
        seams.sleep(delay)
        delay = min(delay * 2, 0.2)


def render_step4_front(run: RenderRun) -> None:
    """(4) egress only: the front DETACHED, blocked reading the GO pipe; its ``pid`` + ``start`` into alloc, fsync.

    The spawn is the revoker's interlock: the slot's ``front.lock`` taken (render holds no pool or slot lock, so the
    order pool -> slot -> front holds), the ``front-revoked`` marker read under it, and a marker naming this lease
    refuses ``front-revoked`` with nothing spawned; the lock is released only once the spawned child's real uid is the
    gateway uid (:func:`front_dropped`), so a revoker that takes the lock next sweeps a uid the front is already in,
    and nothing else is taken while it is held."""
    if not run.egress:
        return
    seams = run.ctx.seams
    argv, spec_json = front_launch(run)
    go_r, go_w = os.pipe()
    try:
        with seams.front_lock(run.ctx, run.alloc.slot):
            if seams.front_revoked(run.ctx, run.alloc):
                raise Refused("front-revoked", f"{run.alloc.slot.tag} lease {run.alloc.lease_seq}")
            run.front_pid = seams.spawn_front(argv, spec_json, go_r)
            run.front_start = front_dropped(run)
        run.go_w = go_w
    except BaseException:
        os.close(go_w)
        raise
    finally:
        os.close(go_r)
    pol = run.alloc.policy
    assert pol is not None and pol.front is not None
    run.alloc = replace(run.alloc, policy=replace(pol, front=replace(pol.front, pid=run.front_pid,
                                                                     start=run.front_start)))
    write_alloc(run.ctx, run.alloc)


def mint_claims(run: RenderRun) -> Dict[str, object]:
    """§3.3 MINT claims, from alloc."""
    pol = run.alloc.policy
    assert pol is not None
    claims: Dict[str, object] = {"routes": list(pol.routes), "queue_grant": pol.queue_grant,
                                 "incarnation": pol.incarnation, "egress_routes": list(pol.egress_routes)}
    if pol.model is not None:
        claims["model"] = pol.model
    if run.egress:
        claims["front"] = {"uid": run.front_uid, "pid": run.front_pid, "start": run.front_start}
    return claims


def render_step5_mint(run: RenderRun) -> None:
    """(5) MINT with the claims (CONTROL registers the front with keys before it answers)."""
    ans = run.ctx.call("MINT", session=run.alloc.session, generation=run.generation, claims=mint_claims(run))
    run.generation = int(ans["generation"])


def render_step5b_arm(run: RenderRun) -> None:
    """(5b) egress only: FRONT-ARM, wait for its answer, then the go byte and the pipe closed: only now may the
    front open ``H``. DECISION: a go byte the pipe refuses (the front is gone) is ``front-lost``."""
    if not run.egress:
        return
    run.ctx.call("FRONT-ARM", session=run.alloc.session, generation=run.generation)
    try:
        write_all(run.go_w, GO_BYTE)
    except OSError:
        raise Refused("front-lost", str(run.front_pid)) from None
    finally:
        os.close(run.go_w)
        run.go_w = -1


def render_step6_cas_rendered(run: RenderRun) -> None:
    """(6) CAS RENDERING -> RENDERED."""
    ans = run.ctx.call("CAS", session=run.alloc.session, generation=run.generation,
                       **{"from": R.RENDERING, "to": R.RENDERED}, fields={})
    run.generation = int(ans["generation"])


RENDER_STEPS: Tuple[Tuple[str, Callable[[RenderRun], None]], ...] = (
    ("cas-rendering", render_step1_cas_rendering),
    ("policy", render_step2_policy),
    ("files", render_step3_files),
    ("front", render_step4_front),
    ("mint", render_step5_mint),
    ("arm", render_step5b_arm),
    ("cas-rendered", render_step6_cas_rendered),
)


def kill_front(run: RenderRun) -> None:
    """The helper SIGKILLs the front IT started, uid + start checked first (§5.1). DECISION: the uid may still be
    root, the stage not yet dropped: the helper is the front's unreaped parent, so the pid cannot be another
    process's, and the start time matches."""
    if run.go_w >= 0:
        os.close(run.go_w)              # the front reads EOF: it exits without opening H
        run.go_w = -1
    if run.front_pid is None:
        return
    seams = run.ctx.seams
    ident = seams.proc_identity(run.front_pid)
    if ident is not None and (run.front_start is None or ident[1] == run.front_start) \
            and ident[0] in (run.front_uid, 0):
        seams.kill(run.front_pid)


def render(ctx: Ctx, req: RuntimeRequest, blob: RenderBlob) -> Dict[str, object]:
    """§5.1 render, the whole of it under root's per-session lock (``session-busy``): a second render of the session
    can neither pass step (1) beside this one nor write its alloc. The alloc is read again under the lock."""
    assert req.session is not None
    read_alloc(ctx, req.session)             # no-session before any lock file is made
    with session_lock(ctx, req.session):
        a = read_alloc(ctx, req.session)
        if a.policy is not None:
            raise Refused("not-allocated", req.session)
        # §3.5's second gate, before step (1)'s CAS: a refusal here leaves the session ALLOCATED and acts on nothing
        RD.check_ticket(blob, a.kind, a.slot.slot_class, a.ticket, a.ticket_facts)
        refuse_ticketed_front(a)        # 3b-8a T5: before the base map's route types are read, so one name for all
        if a.ticket is not None and a.ticket_facts is not None and a.ticket_facts.get("kind") == "verifier":
            RD.check_driver_template(a.ticket_facts["verifier"], ctx.seams.driver_template)   # type: ignore[arg-type]
        run = RenderRun(ctx, a, blob)
        # 3c-8 (1025-10 obs 2): step (2)'s checks read alloc, the blob and root's own files (the base map, the hook
        # templates), none of which the CAS changes, so they run before it too; a refusal among them leaves the
        # session ALLOCATED, as the ticket gate does
        run.facts = render_facts(ctx, a, blob)
        RD.check_facts(blob, run.facts)
        if run.egress:                  # the front's policy file, refused here (if at all) like step (2)'s checks
            run.front_policy = front_policy_bytes(ctx.store, a, blob.routes)
        try:
            for name, step in RENDER_STEPS:
                ctx.seams.at(name)
                step(run)
        except BaseException:
            try:
                kill_front(run)
            finally:                    # a front cleanup that raises does not skip the REVOKE
                if run.cas_may_have_landed:
                    revoke_failed_render(run)
            raise
    return {}


def revoke_failed_render(run: RenderRun) -> None:
    """3c-8 (1025-10 obs 2): a render refused once step (1)'s CAS may have landed left the session RENDERING (or past
    it), which no later render can take up (step (1) CASes from ALLOCATED only), so root asks CONTROL to ``REVOKE``
    it: REVOKED is a state the reaper's END clears, as it would after the lease's EOF. The REVOKE goes on a NEW
    connection: a lost reply closes the cached client (``ControlClient._down``), and a call on it is refused
    ``control-down`` without reaching CONTROL. Best effort: a REVOKE that fails leaves the session to that EOF, and
    the render's own refusal is what the caller is answered."""
    seams = run.ctx.seams
    try:
        client = seams.client_factory(seams.R / "control" / "control.sock")
    except Exception:   # noqa: BLE001 - the render's refusal is the answer; the lease EOF still revokes
        return
    try:
        client.call("REVOKE", session=run.alloc.session)   # type: ignore[attr-defined]
    except Exception:   # noqa: BLE001 - as above
        pass
    finally:
        try:
            client.close()                                  # type: ignore[attr-defined]
        except Exception:   # noqa: BLE001 - closing never changes the verb's answer
            pass


# --- resume, status ---------------------------------------------------------------------------------------------------

def state_generation(ctx: Ctx, session: str) -> int:
    """The state record's generation, read as a HINT for root's CAS (root never acts on a state field, §7.1): a
    forged one only makes CONTROL refuse the CAS ``stale-generation``."""
    data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
    if data is None:
        raise Refused("no-session", session)
    return R.parse_state(data).generation


def resume(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """BRAKED -> ACTIVE, only above V5. DECISION: the CAS clears ``brake_reason``."""
    assert req.session is not None
    read_alloc(ctx, req.session)
    with session_lock(ctx, req.session):
        if ctx.seams.free_bytes(ctx.store.fd) < V5_FLOOR_BYTES:
            raise Refused("below-floor", "free space under V5")
        gen = state_generation(ctx, req.session)
        ctx.call("CAS", session=req.session, generation=gen, **{"from": R.BRAKED, "to": R.ACTIVE},
                 fields={"brake_reason": None})
    return {}


def status(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """Read-only (§5.1): the caller's alloc records (``supervisor_uid == SUDO_UID``; dispatch: ticketed only).
    DECISION: the answer is ``{records:[levain-alloc/1…]}`` sorted by session; a named session the caller does not
    hold is ``no-session``. A named ``login`` session whose state record holds an ``identity`` (past start's identity
    step, §7.6) also answers ``identity``: CONTROL's field, which the slot cannot write, so the supervisor's
    confirm prompt is decided and shown from it (1027-18 RULINGS G1b, the root-sourced id display)."""
    if req.session is not None:
        from levain.slot import identity as ID

        a = read_alloc(ctx, req.session)
        ans: Dict[str, object] = {"records": [R.alloc_record(a)]}
        rec = (ID.read_record(ctx, a.session)
               if a.kind == "login" or (a.kind == "interactive" and a.auth == "passthrough") else None)
        ident = rec.fields.get("identity") if rec is not None else None
        if ident is not None:
            ans["identity"] = dict(ident)
        return ans
    out = []
    with ctx.store.dir("alloc") as fd:
        names = sorted(os.listdir(fd))
    for name in names:
        if not name.endswith(".json") or name.endswith(".intent.json") or not SESSION_RE.fullmatch(name[:-5]):
            continue
        try:
            out.append(R.alloc_record(read_alloc(ctx, name[:-5])))
        except Refused as e:
            if e.name != "no-session":
                raise
    return {"records": out}


#: verb -> handler(ctx, request, stdin blob) -> the ok line's fields. Lanes register their verbs here.
def _start(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    from levain.slot import start as SS

    return SS.start(ctx, req, blob)


def _teardown(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    from levain.slot import start as SS

    return SS.teardown(ctx, req, blob)


def _confirm_identity(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    from levain.slot import identity as ID

    return ID.confirm_identity(ctx, req, blob)


def _probe(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    from levain.slot import probe as PB

    return PB.probe(ctx, req, blob)


# --- adopt-open steps 1-5 (S1 session 5 DESIGN §1, §2, §4.7, §4.10, §4.11) ------------------------------------------

#: V7 (CONTRACT §11): the walker's caps, bytes (logical), entries, and the wall that also bounds the adopt-lock wait.
V7_BYTES = 16 << 30
V7_ENTRIES = 2_000_000
V7_SECONDS = 1800.0
#: The alloc kinds an operator adopts from (§1 step 1).
ADOPT_KINDS = frozenset({"interactive", "login"})
#: V29's per-entry term (§4.11): two filesystem blocks, judged; 5g measures it.
ADOPT_ENTRY_BYTES = 8 << 10
#: V31's fixed filesystem overhead (§4.9), judged on both OSes; Linux adds the inode table below.
IMAGE_OVERHEAD_BYTES = 64 << 20
#: V31's Linux inode count beside its per-entry term (``refs-cap`` ref files plus a fixed margin, judged).
IMAGE_INODES_FIXED = 4096 + 1024
IMAGE_INODE_BYTES = 256


def adopt_image_bytes(host: str, *, entries: int, nbytes: int, manifest_bytes: int, bundle_bytes: int) -> int:
    """V31: the per-run image's size, V29's growth terms that live inside it (the spool, the loose objects, the pack;
    the base bundle; the manifest twice; the per-entry term) plus the filesystem overhead."""
    overhead = IMAGE_OVERHEAD_BYTES
    if host == "linux":
        overhead += (2 * entries + IMAGE_INODES_FIXED) * IMAGE_INODE_BYTES
    return bundle_bytes + 3 * nbytes + 2 * manifest_bytes + entries * ADOPT_ENTRY_BYTES + overhead


def adopt_peak_bytes(host: str, *, entries: int, nbytes: int, manifest_bytes: int, bundle_bytes: int) -> int:
    """V29: the free space adopt-open admits a run at, V5 + root's published copy of the pack + the image (one
    function under both, so the admission and the cap cannot disagree)."""
    return V5_FLOOR_BYTES + nbytes + adopt_image_bytes(host, entries=entries, nbytes=nbytes,
                                                       manifest_bytes=manifest_bytes, bundle_bytes=bundle_bytes)


def _alloc_base_field(a: R.Alloc) -> Optional[Mapping[str, object]]:
    return None if a.base is None else R.base_obj(a.base)


def _root_git_version() -> str:
    return FI.git_version_text(wall.GIT)


@dataclass
class AdoptSeams:
    """adopt-open's own injected facts: the alloc's ``base`` (render writes it; ``None`` = unrecorded), V7's wall
    in seconds (one absolute deadline over the adopt-lock wait and the walk), the walker caps, and ``git_version()``
    (root's git's ``git --version``, read against the floor)."""

    alloc_base: Callable[[R.Alloc], Optional[Mapping[str, object]]] = _alloc_base_field
    lock_wait_s: float = V7_SECONDS
    max_bytes: int = V7_BYTES
    max_entries: int = V7_ENTRIES
    git_version: Callable[[], str] = _root_git_version


@dataclass(frozen=True)
class AdoptFront:
    """What steps 1-5 established, for the sealed run (steps 6-12). ``deadline`` is V7's one absolute wall
    (``Seams.monotonic`` value), fixed before the adopt-lock wait; the walk takes the same value."""

    alloc: R.Alloc
    base: Mapping[str, object]
    retained: Path
    depth: int
    legacy_depth: bool
    entries: int
    nbytes: int
    manifest_bytes: int
    deepest_git_path: int
    deadline: float


def adopt_walk_caps(front: AdoptFront, adopt: AdoptSeams):   # -> walker.WalkCaps
    """The walker's caps for this run: V7's bytes and entries, and the SAME deadline the adopt-lock wait ran
    against, so a lock taken late leaves the walk only what remains of the one wall."""
    from levain.slot import walker as WK

    return WK.WalkCaps(bytes=adopt.max_bytes, entries=adopt.max_entries, deadline=front.deadline)


#: The keys of the ``base`` record render writes into the alloc: every one is required, or ``base-unrecorded``.
BASE_KEYS = frozenset({"oid", "ref_b64", "object_format", "bundle_sha256", "bundle_bytes"})
_OID_HEX = {"sha1": 40, "sha256": 64}
_HEX_RE = re.compile(r"[0-9a-f]+")


def _check_base(base: object, session: str) -> Mapping[str, object]:
    """``alloc.base`` as a record: a Mapping holding every :data:`BASE_KEYS` key, each well-typed; else
    ``base-unrecorded`` (never a default for a missing term)."""
    if not isinstance(base, Mapping) or not BASE_KEYS <= set(base):
        raise Refused("base-unrecorded", session)
    fmt, oid, sha, nb, ref = (base["object_format"], base["oid"], base["bundle_sha256"], base["bundle_bytes"],
                              base["ref_b64"])
    if not (isinstance(fmt, str) and fmt in _OID_HEX and isinstance(oid, str) and len(oid) == _OID_HEX[fmt]
            and _HEX_RE.fullmatch(oid) and isinstance(sha, str) and sha.startswith("sha256:") and len(sha) == 71
            and _HEX_RE.fullmatch(sha[7:]) and isinstance(nb, int) and not isinstance(nb, bool) and nb >= 0
            and isinstance(ref, str) and ref):
        raise Refused("base-unrecorded", f"{session}: a base record off its schema")
    return base


def adopt_step1_caller(ctx: Ctx, session: str) -> R.Alloc:
    """(1) The alloc, bound to its operator (``SUDO_UID`` == alloc ``operator_uid``; anything else answers
    ``no-session``, so a caller learns nothing of another's sessions); its kind one an operator adopts from."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    try:
        a = R.parse_alloc(data)
    except (ContractError, Refused) as e:
        # Read before the operator check, so its failure names nothing about the record.
        raise Refused("no-session", session) from e
    if a.session != session or a.operator_uid != ctx.sudo_uid:
        raise Refused("no-session", session)
    if a.kind not in ADOPT_KINDS:
        raise Refused(f"adopt-kind:{a.kind}", session)
    return a


def scrubbed(ctx: Ctx, a: R.Alloc) -> bool:
    """§4.10's POSITIVE predicate over root's ``L/slots/<tag>/state``, read without the slot lock: it opens
    ``O_NOFOLLOW``, regular, root's, parses, names alloc's slot, its ``lease_seq`` is not below alloc's, and it is not
    ``(leased, a.session, a.lease_seq)``. Anything else (absent, torn, a link, another slot's, regressed) is False."""
    try:
        with ctx.store.dir("slots", a.slot.tag) as sfd:
            fd = os.open("state", _O_READ, dir_fd=sfd)
    except (OSError, Refused):
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not ctx.seams.trusted(st) or st.st_size > _RECORD_CAP:
            return False
        data = os.read(fd, _RECORD_CAP + 1)
    except OSError:
        return False
    finally:
        os.close(fd)
    try:
        s = R.parse_slot_state(data)
    except (ContractError, Refused, ValueError):
        return False
    if s.slot != a.slot or s.lease_seq < a.lease_seq:
        return False
    return not (s.state == "leased" and s.session == a.session and s.lease_seq == a.lease_seq)


def _manifest_depths(path: Path, cap: int) -> Tuple[int, int, int]:
    """(deepest path, deepest GIT-TREE path, the manifest's bytes) in END levels, from root's ``manifest.jsonl``:
    a git-tree path is one under ``repo/`` that becomes a worktree entry (``repo/.git`` and any nested ``.git`` subtree
    excluded)."""
    from levain.slot import walkframe as WF

    scope = WF.PRUNE_SCOPE_PREFIX
    deepest = deepest_git = size = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        for line in iter(lambda: fh.readline(cap + 1), b""):
            size += len(line)
            if len(line) > cap or not line.endswith(b"\n"):
                raise Refused("walk-manifest-mismatch", f"manifest line past {cap} bytes or torn")
            try:
                rec = canon.parse(line[:-1])
                parts = WF.check_entry({"v": 1, "path_b64": rec["path_b64"], "type": "dir", "mode": 0, "size": 0,
                                        "digest": ""}).split(b"/")   # type: ignore[index]
            except (ContractError, Refused, KeyError, TypeError):
                raise Refused("walk-manifest-mismatch", "a manifest line that is not a record") from None
            deepest = max(deepest, len(parts))
            if tuple(parts[:len(scope)]) == scope and len(parts) > len(scope) and b".git" not in parts[len(scope):]:
                deepest_git = max(deepest_git, len(parts))
    return deepest, deepest_git, size


def adopt_step3_root_facts(ctx: Ctx, a: R.Alloc, adopt: AdoptSeams, *, deadline: float) -> AdoptFront:
    """(3) Root facts, its own files only: the retained object whole, no intent, not in trash, SCRUBBED, ``base``
    recorded, and the walk depth (``object.json`` ``depth``, or for a pre-upgrade object ``max(V28, the deepest
    manifest path)``)."""
    from levain.slot import end as E
    from levain.slot import store_verbs as SV
    from levain.slot import walkframe as WF

    facts = SV.facts_from_alloc(a, ctx.seams.L)
    sv = SV.StoreSeams(cas=lambda target, fields: None, owner_ok=ctx.seams.trusted)
    if os.path.lexists(facts.intent):
        raise Refused("intent-in-flight", a.session)
    if os.path.lexists(facts.trash):
        raise Refused("not-retained:trash", a.session)
    if not SV.retained_whole(facts, sv):
        raise Refused("not-retained:absent", a.session)
    if not scrubbed(ctx, a):
        raise Refused("not-scrubbed", a.session)
    base = _check_base(adopt.alloc_base(a), a.session)
    obj = canon.parse(E._read_durable(facts.retained / "object.json"))
    assert isinstance(obj, dict)   # retained_whole parsed it as this session's object
    deepest, deepest_git, msize = _manifest_depths(facts.retained / "manifest.jsonl", WF.header_cap() + 1024)
    depth = obj.get("depth")
    legacy = depth is None
    if legacy:
        depth = max(E.MANIFEST_DEPTH, deepest)
    elif not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise Refused("not-retained:absent", f"{a.session}: object.json depth {depth!r}")
    entries, nbytes = obj.get("entries"), obj.get("bytes")
    for v in (entries, nbytes):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise Refused("not-retained:absent", f"{a.session}: object.json entries / bytes")
    return AdoptFront(alloc=a, base=base, retained=facts.retained, depth=depth, legacy_depth=legacy,
                      entries=entries, nbytes=nbytes, manifest_bytes=msize, deepest_git_path=deepest_git,
                      deadline=deadline)


#: The adopt-lock fds this process holds. A forked child (the importer runs through a fork with no exec, and
#: ``O_CLOEXEC`` never fires there) closes them in its first step after the fork, so the flock outlives the
#: parent's release by that step at most, never by the child's lifetime.
_ADOPT_LOCK_FDS: set = set()


def _close_adopt_locks_in_child() -> None:
    for fd in tuple(_ADOPT_LOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            pass
    _ADOPT_LOCK_FDS.clear()


os.register_at_fork(after_in_child=_close_adopt_locks_in_child)


def flock_until(fd: int, deadline: float, seams: Seams) -> bool:
    """``LOCK_EX`` by ``LOCK_NB`` retries until the absolute ``deadline`` (``seams.monotonic`` value): the backoff
    of :func:`wait_flock` (which takes a duration), each sleep cut to what is left. Touches no signal state.
    False at the deadline: the clock is read before every try and after a success, and a lock taken at or past the
    deadline (a sleep that overshot it while the holder released) is unlocked and refused."""
    delay = LOCK_SLICE_S
    while True:
        if deadline - seams.monotonic() <= 0:
            return False
        if try_flock(fd):
            break
        left = deadline - seams.monotonic()
        if left <= 0:
            return False
        seams.sleep(min(delay, left))
        delay = min(delay * 2, 0.5)
    if seams.monotonic() >= deadline:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    return True


@contextmanager
def adopt_lock_wait(ctx: Ctx, deadline: float) -> Iterator[None]:
    """(4) The machine-wide adopt lock ``L/export/adopt.lock`` (root 0600, never unlinked), taken AFTER the session
    lock: ``LOCK_NB`` retries until V7's absolute ``deadline`` (``adopt-busy`` past it), so an operator adopt
    waits behind a running import and takes the lock when it releases."""
    with ctx.store.dir("export") as efd:
        try:
            fd = os.open("adopt.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600,
                         dir_fd=efd)
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENXIO):
                raise Refused("bad-root", "export/adopt.lock is a link or not a file") from None
            raise
    _ADOPT_LOCK_FDS.add(fd)   # from the open: a child forked while this OFD is locked must not keep it
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not ctx.seams.trusted(st):
            raise Refused("bad-root", "export/adopt.lock")
        if not flock_until(fd, deadline, ctx.seams):
            raise Refused("adopt-busy", "an import or verify held the adopt lock past the wait")
        yield
    finally:
        _ADOPT_LOCK_FDS.discard(fd)
        os.close(fd)   # the only descriptor: a forked child closed its copy after the fork


def adopt_step5_precaps(ctx: Ctx, front: AdoptFront, adopt: AdoptSeams) -> None:
    """(5) Pre-caps from root's facts: V7's entries and bytes; the deepest git-tree path against ``MAX_DEPTH`` (the
    retained SOURCE tree, whatever the host git); free space on the store volume against V29."""
    from levain.slot import walkframe as WF

    if front.entries > adopt.max_entries:
        raise Refused("walker-cap:entries", f"{front.entries} entries > {adopt.max_entries}")
    if front.nbytes > adopt.max_bytes:
        raise Refused("walker-cap:bytes", f"{front.nbytes} bytes > {adopt.max_bytes}")
    if front.deepest_git_path > WF.MAX_DEPTH:
        raise Refused("adopt-path-too-long",
                      "this session was retained by an END that accepted any depth; Levain refuses a session tree "
                      f"deeper than {WF.MAX_DEPTH}; git itself refuses to walk one from v2.43")
    bundle_bytes = _check_base(front.base, front.alloc.session)["bundle_bytes"]
    need = adopt_peak_bytes(ctx.seams.host, entries=front.entries, nbytes=front.nbytes,
                            manifest_bytes=front.manifest_bytes, bundle_bytes=bundle_bytes)
    with ctx.store.dir("export") as efd:
        free = ctx.seams.free_bytes(efd)
    if free < need:
        raise Refused("adopt-space", f"{free} bytes free < V29 {need}")


@contextmanager
def adopt_open_front(ctx: Ctx, session: str, adopt: Optional[AdoptSeams] = None) -> Iterator[AdoptFront]:
    """adopt-open steps 1-5: the caller and kind, the session lock (TRY, ``session-busy``), root facts, the adopt
    lock (bounded wait, ``adopt-busy``), the pre-caps, then the git floor (``git-below-floor:<version>``, CONTRACT
    §5.1 adopt-open; :func:`fsck_ids.check_floor`, the one function adopt-verify calls too), before any seal-in. Both
    locks are held while the caller runs steps 6-12 and are released adopt lock first, then the session lock."""
    adopt = adopt or AdoptSeams()
    deadline = ctx.seams.monotonic() + adopt.lock_wait_s   # V7's one wall: the lock wait and the walk
    a = adopt_step1_caller(ctx, session)
    ctx.seams.at("adopt:2")
    try:
        with session_lock(ctx, session):
            ctx.seams.at("adopt:3")
            front = adopt_step3_root_facts(ctx, a, adopt, deadline=deadline)
            ctx.seams.at("adopt:4")
            try:
                with adopt_lock_wait(ctx, front.deadline):
                    ctx.seams.at("adopt:5")
                    adopt_step5_precaps(ctx, front, adopt)
                    FI.check_floor(adopt.git_version())
                    ctx.seams.at("adopt:floor")
                    yield front
            finally:
                ctx.seams.at("adopt:adopt-unlocked")
    finally:
        ctx.seams.at("adopt:session-unlocked")


# --- adopt-open steps 6-12: the sealed import run (DESIGN §1 steps 6-12, §4.1, §4.5, §5b; 5c-ii-c) ----------------

#: V31's memory cap for the importer (the Linux leaf's ``memory.max``, the macOS watchdog's cap) [judged by adopt-cc,
#: 10-22, against DESIGN §4.9's 5g row "a one-shot allocation past 4 GiB"; no ruling names the number].
ADOPT_MEMORY_CAP = 4 << 30
#: V31's ``pids.max`` on the run leaf: the same count as the stage's ``RLIMIT_NPROC`` (``wall.EXPORT_NPROC``).
ADOPT_PIDS_MAX = wall.EXPORT_NPROC
#: Step 10: root waits for the result line until V7's time + this grace (DESIGN §1 step 10, "V7 time + 60 s").
RESULT_GRACE_S = 60.0
#: The export class adopt-open publishes into (an operator's adopt; the reaper's artifact step is ``dispatch``).
ADOPT_CLASS = "operator"
_O_LOG = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def export_rlimits(image_bytes: int, cpu_s: float = V7_SECONDS) -> Tuple[Tuple[int, int], ...]:
    """§5b "Both" (head ruling 1022+19 Q3): ``(resource, limit)`` pairs root sets in the forked export child, soft =
    hard, so the target cannot raise them back: ``RLIMIT_CORE`` 0, ``RLIMIT_FSIZE`` V31's image bytes (no file the
    importer writes can outgrow the image; the logs included), ``RLIMIT_CPU`` V7's seconds. ``RLIMIT_NPROC`` is the
    stage's (``Spec.nproc``); there is no ``RLIMIT_AS`` (A5: memory is the leaf's or the watchdog's)."""
    import resource
    return ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, int(image_bytes)),
            (resource.RLIMIT_CPU, int(cpu_s)))


def apply_rlimits(limits: Sequence[Tuple[int, int]], *, getrlimit: Optional[Callable] = None,
                  setrlimit: Optional[Callable] = None) -> None:
    """Each limit as soft AND hard, never above the hard limit already in force (lowering a hard limit needs no
    privilege; raising one would fail EPERM for a non-root caller and is never wanted). ``getrlimit`` /
    ``setrlimit``: the ``resource`` module's (a seam log stands in for them in a unit row)."""
    import resource
    get = getrlimit or resource.getrlimit
    put = setrlimit or resource.setrlimit
    for res, want in limits:
        _soft, hard = get(res)
        v = want if hard == resource.RLIM_INFINITY or hard < 0 else min(want, hard)
        put(res, (v, v))


@dataclass(frozen=True)
class ExportCmd:
    """One export target's launch: ``argv`` (macOS ``launchctl asuser`` -> the stage; Linux bwrap -> the stage),
    the stage's ``spec_json``, the Linux leaf the child joins (``cgroup``), ``cwd`` (None in production: the stage
    changes to the spec's cwd after the drop; a seam run with no stage sets it here) and ``exec_child(argv,
    spec_fd, cgroup)``, the child's last act (:func:`wall.exec_launch`)."""

    argv: Tuple[str, ...]
    spec_json: bytes
    cgroup: Optional[str] = None
    cwd: Optional[str] = None
    exec_child: Callable[..., None] = wall.exec_launch


def export_launch_default(ctx: Ctx, args: Sequence[str], export_class: str) -> ExportCmd:
    """The importer's product launch (§5b): its spec (:func:`wall.export_launch_spec`, the principal
    ``_levain_export`` with the class's group) and the host's launch argv."""
    return _export_launch(ctx, "importer", args, export_class)


def verify_launch_default(ctx: Ctx, args: Sequence[str], export_class: str) -> ExportCmd:
    """The verifier's product launch (§5b, §6.0): the principal ``_levain_verify`` with ``levain-verify`` (no
    class: the verifier reads its two inputs only, whichever class publishes its outputs)."""
    return _export_launch(ctx, "verifier", args, None)


def _export_launch(ctx: Ctx, role: str, args: Sequence[str], export_class: Optional[str]) -> ExportCmd:
    host = ctx.seams.host
    group = wall.EXPORT_GROUPS[(role, export_class)]
    ids = {wall.EXPORT_ACCOUNT[role]: ctx.seams.account_uid(wall.EXPORT_ACCOUNT[role])}
    shared = {group: ctx.seams.group_gid(group)}
    spec = wall.export_launch_spec(host, role, ids=ids, shared=shared, args=args, export_class=export_class)
    if host == "darwin":
        return ExportCmd(wall.macos_launch_argv(spec), spec.to_json())
    launch = wall.export_linux_launch_argv(spec, host_links=ctx.seams.host_links())
    return ExportCmd(launch.argv, spec.to_json(), cgroup=launch.cgroup)


def spawn_export(cmd: ExportCmd, *, frame_r: int, stdout_fd: int, stderr_fd: int, result_w: int,
                 rlimits: Sequence[Tuple[int, int]]) -> int:
    """§1 step 7's fork: the child ``setsid``s, puts the frame pipe's read end on 0, the two log files on 1 and 2,
    the result pipe's write end on :data:`wall.RESULT_FD` (4), keeps the spec pipe (moved to ``SPEC_FD`` by
    ``exec_launch``, read and closed by the stage before its exec) and closes every other fd; sets
    :func:`export_rlimits` (they survive the exec chain and the drop); then ``cmd.exec_child``. The parent writes the
    spec, closes its spec end and returns the pid. A child that fails before its exec exits 70 with no result line."""
    spec_r, spec_w = os.pipe()
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the forked child; the seam run drives it, the root path is a 5g row
        try:
            os.setsid()
            hi = [fcntl.fcntl(fd, fcntl.F_DUPFD, 32) for fd in (frame_r, stdout_fd, stderr_fd, result_w, spec_r)]
            for src, dst in zip(hi[:4], (0, 1, 2, wall.RESULT_FD)):
                os.dup2(src, dst)          # dup2 leaves the target inheritable
            _close_fds_except([wall.RESULT_FD, hi[4]])
            apply_rlimits(rlimits)
            if cmd.cwd is not None:
                os.chdir(cmd.cwd)
            cmd.exec_child(cmd.argv, hi[4], cgroup=cmd.cgroup)
        except BaseException:   # noqa: BLE001 - the child never returns into the helper's stack
            pass
        os._exit(70)
    os.close(spec_r)
    try:
        write_all(spec_w, cmd.spec_json)
    except OSError:
        pass                    # a child that died before reading its spec: its exit says so
    finally:
        os.close(spec_w)
    return pid


def collect_export(pid: int, result_r: int, deadline_s: float, *, cap: int,
                   monotonic: Callable[[], float] = time.monotonic,
                   on_reap: Optional[Callable[[], None]] = None) -> Tuple[str, Optional[int], Optional[bytes], bool]:
    """§1 step 10: the result line read from fd 4 as DATA while the importer runs, at most ``cap`` + 1 bytes (the
    classifier refuses the extra byte by size), until the importer EXITS (polled ``WNOHANG``), never until the pipe's
    EOF: a process the importer left behind can hold fd 4 open, and seal-out kills it. Returns ``(exit, code, line,
    reaped)``: ``exit`` ``"exit"`` (``code`` its status) or ``"signal"``, or ``"time"`` past ``deadline_s`` with the
    importer NOT reaped (seal-out's domain proof kills and reaps it as a child). ``on_reap`` is called as soon as the
    ``waitpid`` has reaped the importer, before anything else here can raise, so a caller never hands a reaped (and
    possibly recycled) pid to seal-out (L3 r1 L2)."""
    end = monotonic() + deadline_s
    buf = bytearray()
    open_ = True
    os.set_blocking(result_r, False)

    def take() -> bool:            # one read; False at EOF or nothing ready
        nonlocal open_
        if not open_ or len(buf) > cap:
            return False
        try:
            b = os.read(result_r, min(65536, cap + 1 - len(buf)))
        except BlockingIOError:
            return False
        if not b:
            open_ = False
            return False
        buf.extend(b)
        return True

    with selectors.DefaultSelector() as sel:
        sel.register(result_r, selectors.EVENT_READ)
        while True:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except InterruptedError:
                continue
            if done:
                if on_reap is not None:
                    on_reap()
                while take():
                    pass
                line = bytes(buf) if buf else None
                if os.WIFSIGNALED(status):
                    return "signal", None, line, True
                return "exit", os.WEXITSTATUS(status), line, True
            left = end - monotonic()
            if left <= 0:
                return "time", None, (bytes(buf) if buf else None), False
            if open_ and len(buf) <= cap:
                if sel.select(min(left, 0.05)):
                    take()
            else:
                time.sleep(min(left, 0.05))


@dataclass
class AdoptRunSeams:
    """Steps 6-12's injected facts, each with its real default. ``seal(facts) -> export_run.SealSeams`` (None: the
    real domain prover of ``_levain_export``); ``launch(ctx, args, export_class) -> ExportCmd``; ``spawn``
    (:func:`spawn_export`); ``walk_seams() -> walker.WalkSeams`` (None: root's, owner uid 0); ``publish_gid(ctx,
    export_class)`` (None: the class group's gid; a seam run as an unprivileged uid answers None from it, no chown);
    ``memory_cap`` / ``pids_max`` (V31); ``result_grace_s`` (step 10)."""

    seal: Optional[Callable[[object], object]] = None
    launch: Callable[[Ctx, Sequence[str], str], ExportCmd] = export_launch_default
    spawn: Callable[..., int] = spawn_export
    walk_seams: Optional[Callable[[], object]] = None
    publish_gid: Optional[Callable[[Ctx, str], Optional[int]]] = None
    memory_cap: int = ADOPT_MEMORY_CAP
    pids_max: int = ADOPT_PIDS_MAX
    result_grace_s: float = RESULT_GRACE_S


def make_work(ctx: Ctx, work: str, object_format: str, export_uid: int) -> Tuple[int, int]:
    """Seal-in's ``.work/`` skeleton (§4.1): :func:`importer.make_skeleton` (modes set, root's), then the export
    uid made the owner of ``repo.git/objects``, ``repo.git/refs``, ``spool``, ``out``, ``log`` and the two log files
    (``repo.git`` itself, ``config`` and ``HEAD`` stay root's), each opened ``O_NOFOLLOW`` before its ``fchown``.
    Returns the two log files opened for writing (fds 1 and 2 of the child, A7). Nothing runs in the domain yet."""
    from levain.slot import importer as IM

    IM.make_skeleton(Path(work), object_format)
    for rel in ("repo.git/objects", "repo.git/refs", "spool", "out", "log"):
        fd = os.open(os.path.join(work, rel), _O_DIR)
        try:
            ctx.seams.chown(fd, export_uid, -1, f"export/.work/{rel}")
        finally:
            os.close(fd)
    logs = []
    try:
        for name in ("stdout", "stderr"):
            fd = os.open(os.path.join(work, "log", name), _O_LOG)
            logs.append(fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise Refused(ER_UNAVAILABLE, f".work/log/{name} is not a regular file")
            ctx.seams.chown(fd, export_uid, -1, f"export/.work/log/{name}")
    except BaseException:
        for fd in logs:
            os.close(fd)
        raise
    return logs[0], logs[1]


ER_UNAVAILABLE = "export-image-unavailable"


def importer_args(session: str, base: Mapping[str, object], adopt: AdoptSeams) -> Tuple[str, ...]:
    """The importer's fixed argv (``importer._parser``); ``wall.export_launch_spec`` refuses it past the spec's
    string cap (``spec-malformed``), so a longer argv can never launch."""
    return ("--git", wall.GIT, "--session", session, "--object-format", str(base["object_format"]),
            "--base-oid", str(base["oid"]), "--max-bytes", str(adopt.max_bytes),
            "--max-input-size", str(adopt.max_bytes))


def adopt_answer(ctx: Ctx, front: AdoptFront, result: str, pub: object, export_class: str) -> Dict[str, object]:
    """§4.5: ``result`` and ``refs`` from root's own header parse (never the importer's line), ``base_oid`` and
    ``object_format`` from ``alloc.base``, the digests of the bytes root copied; head ruling A2: the transaction's
    ``warnings`` (a class-dir fsync that failed after the commit) ride beside ``ok: true`` when there are any."""
    s = front.alloc.session
    prefix = f"refs/levain/{s}/".encode()
    refs: List[str] = []
    header = getattr(pub, "header", None)
    if header is not None:
        names = {n[len(prefix):].decode("ascii") for _oid, n in header.refs if n.startswith(prefix)}
        refs = [r for r in ("head", "wip") if r in names]
    digests = getattr(pub, "digests")
    out: Dict[str, object] = {
        "result": result, "repo": str(ctx.seams.L / "export" / export_class / s),
        "bundle_digest": ("sha256:" + digests["bundle"]) if "bundle" in digests else None,
        "report_digest": "sha256:" + digests["report.json"],
        "base_oid": front.base["oid"], "object_format": front.base["object_format"], "refs": refs}
    warnings = tuple(getattr(pub, "warnings", ()))
    if warnings:
        out["warnings"] = list(warnings)
    return out


def detach_before_commit(run: object, seal: object, work_dfd: int) -> Callable[[], None]:
    """The publish's ``before_commit`` for a sealed run (adopt-open and adopt-verify): root's fd on the image root
    closed (D2: no fd of root's into the image survives the detach), then the image detached and unlinked; a detach
    that did not happen REFUSES ``export-image-unavailable`` before the commit (L3 r1 H1), so the transaction removes
    its staging and publishes nothing. ``release_image`` itself is best-effort by design (teardown retries it)."""
    def release() -> None:
        if work_dfd in run.fds:                                     # type: ignore[attr-defined]
            run.fds.remove(work_dfd)                                # type: ignore[attr-defined]
            os.close(work_dfd)
        run.release_image()                                         # type: ignore[attr-defined]
        if not run.released:                                        # type: ignore[attr-defined]
            img = run.image                                         # type: ignore[attr-defined]
            raise Refused(ER_UNAVAILABLE, "the per-run image could not be detached and removed before the "
                          f"commit (mount {img.mount if img else None}, still mounted: "
                          f"{seal.image.is_mounted(img.mount) if img else None}); "  # type: ignore[attr-defined]
                          "nothing is published")
    return release


def adopt_sealed_run(ctx: Ctx, front: AdoptFront, adopt: AdoptSeams, run_seams: AdoptRunSeams,
                     export_class: str = ADOPT_CLASS) -> Dict[str, object]:
    """Steps 6-12 inside both locks. Seal-in (``SealedRun.seal_in``) -> the skeleton and its owners -> (macOS) the
    importer profile written -> spawn -> the watchdog -> the walk into the frame pipe, whose write end is CLOSED as
    soon as the walker returns or refuses (the importer sees EOF at once, never its trailing wait) -> the result line
    and the reap -> seal-out ALWAYS (it kills anything the importer left) -> the ONE classification -> the publish
    (``before_commit`` = the image released) -> the §4.5 answer; teardown in a ``finally``. A walker refusal (head
    ruling Q5): seal-out still runs, ``export-domain-unproven`` from it wins, else the walker's own name; nothing is
    published. The locks are the caller's (:func:`adopt_open_front`), released after this returns."""
    from levain.slot import export_run as ER
    from levain.slot import importer as IM
    from levain.slot import walker as WK

    at, host, s = ctx.seams.at, ctx.seams.host, front.alloc.session
    export_dir = str(ctx.seams.L / "export")
    uid = ctx.seams.account_uid(wall.EXPORT_ACCOUNT["importer"])
    bundle_bytes = int(front.base["bundle_bytes"])   # type: ignore[call-overload]
    image_bytes = adopt_image_bytes(host, entries=front.entries, nbytes=front.nbytes,
                                    manifest_bytes=front.manifest_bytes, bundle_bytes=bundle_bytes)
    facts = ER.SealFacts(host=host, role_key="importer", export_dir=export_dir, uid=uid, image_bytes=image_bytes,
                         image_inodes=2 * front.entries + IMAGE_INODES_FIXED, memory_cap=run_seams.memory_cap,
                         pids_max=run_seams.pids_max)
    seal = run_seams.seal(facts) if run_seams.seal is not None else ER.SealSeams(
        prove=ER.export_domain_prover(host, "importer", uid))
    run = ER.SealedRun(facts, seal)   # type: ignore[arg-type]
    gid = (run_seams.publish_gid or (lambda c, k: c.seams.group_gid(wall.EXPORT_GROUPS[("importer", k)])))(
        ctx, export_class)
    with ctx.store.dir("export", export_class) as class_dfd:
        at("adopt:6")
        run.seal_in(class_dfd, s + ER.STAGING_SUFFIX)      # a refusal here has torn down what it made
        pid: Optional[int] = None
        reaped = [False]                   # L2: set by collect_export the moment its waitpid reaps the importer
        sealed_out = False
        try:
            work = ER.mount_path(export_dir, "importer")
            out_fd, err_fd = make_work(ctx, work, str(front.base["object_format"]), uid)
            run.fds += [out_fd, err_fd]
            if host == "darwin":
                with ctx.store.dir("export") as efd:
                    replace_at(efd, "importer.sb", RD.render_importer_profile(host), mode=0o644,
                               fsync=ctx.seams.fsync)
            cmd = run_seams.launch(ctx, importer_args(s, front.base, adopt), export_class)
            # L1: each pair is in run.fds the moment it exists, so a failed second pipe() leaks nothing of the first
            frame_r, frame_w = pair = os.pipe()
            run.fds += pair
            result_r, result_w = pair = os.pipe()
            run.fds += pair
            at("adopt:7")
            try:
                pid = run_seams.spawn(cmd, frame_r=frame_r, stdout_fd=out_fd, stderr_fd=err_fd, result_w=result_w,
                                      rlimits=export_rlimits(image_bytes))
            finally:
                # the child's now: root keeps only frame_w and result_r, and no fd into the image past the spawn
                for fd in (frame_r, result_w, out_fd, err_fd):
                    run.fds.remove(fd)
                    os.close(fd)
            run.start_watchdog()
            at("adopt:8")
            walk_refusal: Optional[Refused] = None
            try:
                WK.walk_frames(front.retained / "tree", front.retained / "manifest.jsonl", frame_w,
                               depth=front.depth, caps=adopt_walk_caps(front, adopt),
                               base_bundle_sha256=str(front.base["bundle_sha256"]),
                               seams=run_seams.walk_seams() if run_seams.walk_seams is not None else None)
            except Refused as e:
                walk_refusal = e
            finally:
                run.fds.remove(frame_w)
                os.close(frame_w)          # 5b1 carry 3: EOF right after the terminal frame (or the refusal)
                at("adopt:frame-closed")
            at("adopt:10")
            wait_s = max(0.0, front.deadline - ctx.seams.monotonic()) + run_seams.result_grace_s
            exit_kind, code, line, _ = collect_export(pid, result_r, wait_s, cap=IM.RESULT_CAP,
                                                      on_reap=lambda: reaped.__setitem__(0, True))
            run.fds.remove(result_r)
            os.close(result_r)
            at("adopt:11")
            sealed_out = True              # ONE seal-out: an unproven one raises and is not run again below
            reading = run.seal_out(() if reaped[0] else (pid,))
            if walk_refusal is not None:
                raise walk_refusal
            at("adopt:classify")
            outcome = ER.classify(ER.RunObservation(exit_kind, code, reading.memory_killed, reading.memory_unreadable,
                                                    reading.host_short, line))
            at("adopt:publish")
            work_dfd = os.open(work, _O_DIR)
            run.fds.append(work_dfd)
            result, pub = ER.publish_if_success(
                outcome, work_dfd, class_dfd, s, object_format=str(front.base["object_format"]), owner_uid=uid,
                caps={"bundle": image_bytes, "report.json": IM.REPORT_CAP}, seams=seal.publish,  # type: ignore[attr-defined]
                gid=gid, before_commit=detach_before_commit(run, seal, work_dfd))
            at("adopt:12")
            return adopt_answer(ctx, front, result, pub, export_class)
        finally:
            try:
                if not sealed_out:
                    # anything that failed after seal-in: the domain is still killed and proven before the locks go.
                    # L3 r1 M1 / ruling Q5: an unproven seal-out here RAISES export-domain-unproven, which replaces
                    # the pending failure (Python chains that one as its __context__); teardown still runs below and
                    # leaves the image for the next seal-in. Known edge (L3 r2, codex MED / complement LOW): a pending
                    # KeyboardInterrupt or SystemExit is replaced the same way, so a signal mid-adopt answers
                    # export-domain-unproven instead of interrupting; nothing publishes and the helper still exits.
                    run.seal_out(() if (pid is None or reaped[0]) else (pid,))
            finally:
                at("adopt:teardown")
                run.teardown()


def adopt_open(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """``adopt-open <session>`` (CONTRACT §5.1; DESIGN §1 steps 1-12): steps 1-5 (:func:`adopt_open_front`) hold
    the session lock, then the adopt lock; steps 6-12 run inside both (:func:`adopt_sealed_run`); the adopt lock is
    released first, then the session lock."""
    assert req.session is not None
    adopt = ctx.seams.adopt if ctx.seams.adopt is not None else AdoptSeams()
    run_seams = ctx.seams.adopt_run if ctx.seams.adopt_run is not None else AdoptRunSeams()
    with adopt_open_front(ctx, req.session, adopt) as front:
        return adopt_sealed_run(ctx, front, adopt, run_seams)


# --- adopt-verify: the confined verifier's root verb (DESIGN §6.0, §6.3, §4.1; CONTRACT §5.1; 5d-i session 2) ------

#: V30 (§6.3): the analysis wall, ``VERIFY_WALL_BASE_S`` + ``VERIFY_WALL_PER_GIB_S`` per GiB of (base bundle + bundle)
#: bytes; ``RLIMIT_CPU`` takes the same value, and root waits for the line that long + :data:`RESULT_GRACE_S`.
VERIFY_WALL_BASE_S = 300.0
VERIFY_WALL_PER_GIB_S = 120.0
#: The verify image's fixed margin (§6.3, judged there); V30's output caps are :mod:`verify_caps`' (one module the
#: verifier child and root both import).
VERIFY_MARGIN_BYTES = 64 << 20
#: ``verified.json``'s cap: a record whose only unbounded part is the prerequisites, which come from a bundle header
#: of at most ``bundlefmt.HEADER_MAX`` bytes [judged by adopt-5di-s2, 10-22, against ``adopt_verify.Verified.to_json``].
VERIFIED_CAP = 2 * BF.HEADER_MAX + (64 << 10)
#: The staged name of the verify publish inside ``<class>/<s>/`` (``transact`` stages ``<name>.new``).
VERIFY_DIR = "verify"
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_COPY_CHUNK = 1 << 20


def idx_bound(objects: int, object_format: str) -> int:
    """A pack idx's bound from its pack header's object count (§6.3, CONTRACT §10.3): ``8 + 1024 + objects x (oid
    bytes + 16) + 2 x oid bytes``."""
    ob = BF.OID_HEX[object_format] // 2
    return 8 + 1024 + objects * (ob + 16) + 2 * ob


def verify_image_bytes(*, bundle_bytes: int, base_bytes: int, bundle_objects: int, base_objects: int,
                       object_format: str) -> int:
    """V30's verify image (§6.3): ``2 x (bundle + base bundle)`` (the copies in, and the pack each ``index-pack
    --stdin`` writes into Q and B), both idx bounds, the expanded-output cap, the required oid list and its ``sort``
    temp files (``2 x objects x (oid hex + 1)``, ``objects`` here BOTH packs' counts, a superset of the bundle's
    [judged by adopt-5di-s2, 10-22, against §6.3's "objects from the pack header's count"]), the verifier's other
    oid inventories in ``scratch/`` (``verify_caps.inventory_bytes`` over the same count: one line per object in each,
    L3 s2 r2), the closure's tree payloads at their cap (``verify_caps.TREE_SCRATCH_CAP``, which the verifier enforces
    before writing them; L3 s2 r1 codex MED: §6.3's formula has no term for them [judged by adopt-s2fix, 10-22]) and
    the fixed margin."""
    hexlen = BF.OID_HEX[object_format]
    objects = bundle_objects + base_objects
    return (2 * (bundle_bytes + base_bytes) + idx_bound(bundle_objects, object_format)
            + idx_bound(base_objects, object_format) + VC.PREVIEW_OUTPUT_CAP + VC.TREE_SCRATCH_CAP
            + 2 * objects * (hexlen + 1) + VC.inventory_bytes(objects, hexlen) + VERIFY_MARGIN_BYTES)


def verify_wall_s(bundle_bytes: int, base_bytes: int) -> float:
    """V30's analysis wall in seconds (§6.3)."""
    return VERIFY_WALL_BASE_S + VERIFY_WALL_PER_GIB_S * (bundle_bytes + base_bytes) / (1 << 30)


def _pf_unbuilt(uid: int) -> bool:
    raise Refused("verifier-network-unisolated", "the live pf check of the _levain_verify rule is not built (5g); "
                  "adopt-verify refuses rather than run the verifier unchecked")


@dataclass
class VerifySeams:
    """adopt-verify's own injected facts: ``git_version()`` (root's git's ``git --version``, read against the floor);
    ``pf_live(uid)`` (macOS: pf enabled and the ``user _levain_verify`` rule live; the real default REFUSES
    ``verifier-network-unisolated``, because no live check is built yet, 5g); the adopt-lock wait (adopt-open's,
    V7's wall)."""

    git_version: Callable[[], str] = _root_git_version
    pf_live: Callable[[int], bool] = _pf_unbuilt
    lock_wait_s: float = V7_SECONDS


def verify_caller(ctx: Ctx, session: str) -> Tuple[R.Alloc, str]:
    """(1) The caller, split by class (CONTRACT §5.1 adopt-verify; c7.10 fix1 F2, head 1022+20 Q1). Returns the
    alloc and the export class the verify publishes into. An OPERATOR session (no ticket, kind ``interactive|login``)
    answers its operator (``SUDO_UID`` == alloc ``operator_uid``) -> ``operator``; on its own uid's ticketed or
    other-kind session the operator answers ``adopt-kind:<kind>``. A DISPATCH-CLASS session (the alloc carries a
    ticket) answers only ``_levain_dispatch``, and only as the lease peer that prepared it with that ticket
    (``supervisor_uid`` == ``SUDO_UID``, :func:`read_alloc`'s ticket-bound rule) -> ``dispatch``. Every other pairing
    answers ``no-session``, so a caller learns nothing of another's sessions."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    try:
        a = R.parse_alloc(data)
    except (ContractError, Refused) as e:
        raise Refused("no-session", session) from e
    if a.session != session:
        raise Refused("no-session", session)
    if ctx.caller == OPERATOR and a.operator_uid == ctx.sudo_uid:
        if a.ticket is not None or a.kind not in ADOPT_KINDS:
            raise Refused(f"adopt-kind:{a.kind}", session)
        return a, ADOPT_CLASS
    if ctx.caller == DISPATCH and a.ticket is not None and a.supervisor_uid == ctx.sudo_uid:
        return a, "dispatch"
    raise Refused("no-session", session)


def verify_caller_locked(ctx: Ctx, session: str, before: R.Alloc, export_class: str) -> R.Alloc:
    """(1) again, under the session lock (L3 s2 r1 complement MED): the alloc re-read and the caller re-checked by
    :func:`verify_caller`, then refused ``no-session`` when its owner (``operator_uid``, ``supervisor_uid``), class
    (kind, ticket, the export class) or ``base`` differs from the pre-lock read. The pre-lock read stays only so a
    caller with no claim on the session learns nothing of it (not even ``session-busy``)."""
    a, k = verify_caller(ctx, session)
    if (k, a.kind, a.ticket, a.operator_uid, a.supervisor_uid, a.base) != (
            export_class, before.kind, before.ticket, before.operator_uid, before.supervisor_uid, before.base):
        raise Refused("no-session", f"{session}: the alloc changed between the caller check and the session lock")
    return a


def _open_source(dfd: int, name: str, refusal: str, ctx: Ctx) -> Tuple[int, os.stat_result]:
    """One input root copies into the verify image, opened ``O_NOFOLLOW|O_NONBLOCK`` below a dirfd it already
    checked: regular, root's (``seams.trusted``), one link; else ``refusal`` by name."""
    try:
        fd = os.open(name, _O_READ, dir_fd=dfd)
    except OSError as e:
        raise Refused(refusal, f"{name}: {errno.errorcode.get(e.errno or 0, e.errno)}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not ctx.seams.trusted(st) or st.st_nlink != 1:
            raise Refused(refusal, f"{name}: not a regular root-owned file of one link")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _fd_sha256(fd: int, size: int) -> str:
    """``sha256:<hex>`` of an open file's first ``size`` + 1 bytes at most, by ``pread``. An OSError reading it (EIO)
    = ``export-image-unavailable``, the name copy-in gives the same read (L3 s2 r2 introduced LOW: it was ``internal``)."""
    h = hashlib.sha256()
    off = 0
    try:
        while off <= size:
            chunk = os.pread(fd, min(_COPY_CHUNK, size + 1 - off), off)
            if not chunk:
                break
            h.update(chunk)
            off += len(chunk)
    except OSError as e:
        raise Refused(ER_UNAVAILABLE, f"the digest scan: {errno.errorcode.get(e.errno or 0, e.errno)}") from e
    return "sha256:" + h.hexdigest()


def copy_in(src: int, size: int, dst_dfd: int, name: str, *, want: str, refusal: str,
            fsync: Callable[[int], None]) -> None:
    """Root's copy of one input into the verify image's ``in/`` (root 0644, ``O_EXCL``), hashed while copying, read by
    ``pread`` from offset 0 to at most ``size`` + 1 bytes: a length other than ``size`` or a digest other than
    ``want`` (``sha256:<hex>``) = ``refusal``. An OSError writing into the image: ENOSPC or EDQUOT =
    ``preview-space-exhausted``, any other = ``export-image-unavailable``."""
    h = hashlib.sha256()
    off = 0
    try:
        dst = os.open(name, _NEW_FILE, 0o644, dir_fd=dst_dfd)
        try:
            os.fchmod(dst, 0o644)
            while off <= size:
                chunk = os.pread(src, min(_COPY_CHUNK, size + 1 - off), off)
                if not chunk:
                    break
                h.update(chunk)
                write_all(dst, chunk)
                off += len(chunk)
            fsync(dst)
        finally:
            os.close(dst)
    except OSError as e:
        what = errno.errorcode.get(e.errno or 0, str(e.errno))
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            raise Refused("preview-space-exhausted", f"copy-in of {name}: {what}") from e
        raise Refused(ER_UNAVAILABLE, f"copy-in of {name}: {what}") from e
    if off != size:
        raise Refused(refusal, f"{name}: {off} bytes copied, the file held {size} when opened")
    if "sha256:" + h.hexdigest() != want:
        raise Refused(refusal, f"{name}: the copied bytes digest to sha256:{h.hexdigest()}, not {want}")


def make_verify_work(ctx: Ctx, work: str, verify_uid: int) -> Tuple[int, int, int]:
    """Seal-in's ``.verify/`` layout (head ruling 1022+20 Q4, §4.1): ``in/`` root 0755 (root's copies, 0644: the
    verifier reads them and cannot change them), ``scratch/``, ``out/``, ``log/`` the verify uid's 0700, and
    ``log/stdout`` / ``log/stderr`` root-created, the verify uid's 0600 (fds 1 and 2 of the child, A7). Returns
    ``(in/ dirfd, stdout fd, stderr fd)``; the caller holds all three in ``run.fds``."""
    made: List[int] = []
    try:
        os.mkdir(os.path.join(work, "in"), 0o755)
        fd = os.open(os.path.join(work, "in"), _O_DIR)
        made.append(fd)
        os.fchmod(fd, 0o755)
        for rel in ("scratch", "out", "log"):
            os.mkdir(os.path.join(work, rel), 0o700)
            d = os.open(os.path.join(work, rel), _O_DIR)
            try:
                os.fchmod(d, 0o700)
                ctx.seams.chown(d, verify_uid, -1, f"export/.verify/{rel}")
            finally:
                os.close(d)
        for name in ("stdout", "stderr"):
            f = os.open(os.path.join(work, "log", name), _NEW_FILE | os.O_NONBLOCK, 0o600)
            made.append(f)
            os.fchmod(f, 0o600)
            ctx.seams.chown(f, verify_uid, -1, f"export/.verify/log/{name}")
    except OSError as e:
        for fd in made:
            os.close(fd)
        raise Refused(ER_UNAVAILABLE, f".verify/ layout: {errno.errorcode.get(e.errno or 0, e.errno)}") from e
    except BaseException:
        for fd in made:
            os.close(fd)
        raise
    return made[0], made[1], made[2]


def verifier_args(session: str, base: Mapping[str, object], result: str, max_input: int) -> Tuple[str, ...]:
    """The verifier's fixed argv (``verify_run._parser``), within the spec's string cap (``wall._FRONT_ARGS``;
    ``wall.export_launch_spec`` refuses a longer one, ``spec-malformed``). ``result`` is root's own
    reading of the published header (prerequisites = ``delta``), never the importer's word."""
    return ("--git", wall.GIT, "--session", session, "--object-format", str(base["object_format"]),
            "--base-oid", str(base["oid"]), "--result", result, "--max-input-size", str(max_input))


def verify_sealed_run(ctx: Ctx, front: AdoptFront, export_class: str, bundle_digest: str, run_seams: AdoptRunSeams,
                      vseams: VerifySeams) -> Dict[str, object]:
    """adopt-verify inside both locks (DESIGN §6.0): the two inputs opened, checked and hashed against their digests
    (the published ``bundle`` against the one asked for, ``export-changed``; the retained base bundle against
    ``alloc.base``, ``base-bundle-mismatch``), root's own header reading (the result,
    the pack counts) -> V30's space (``preview-space``) -> SEAL-IN on ``_levain_verify`` with ``<class>/<s>/`` as the
    staging dir (a stale ``verify.new`` deleted, ``verifier-failed:publish`` / ``stale-staging`` when it cannot be;
    the early exchange probe there) -> the ``.verify/`` layout -> the copies in, hashed -> (macOS) the profile and
    the live pf check -> spawn -> the watchdog -> the line and the reap -> seal-out ALWAYS -> the ONE classification
    (the verifier's names VERBATIM, head ruling Q1) -> the publish of the three outputs (``verify.new`` ->
    ``verify``, ``before_commit`` = the detach) -> the answer; teardown in a ``finally``."""
    from levain.slot import adopt_verify as AV
    from levain.slot import export_run as ER
    from levain.slot import importer as IM

    at, host, s = ctx.seams.at, ctx.seams.host, front.alloc.session
    export_dir = str(ctx.seams.L / "export")
    uid = ctx.seams.account_uid(wall.EXPORT_ACCOUNT["verifier"])
    fmt = str(front.base["object_format"])
    base_bytes = int(front.base["bundle_bytes"])   # type: ignore[call-overload]
    held: List[int] = []                           # root's fds OUTSIDE the image, closed last
    try:
        try:
            s_dfd = ctx.store.open_dir("export", export_class, s)
        except Refused as e:
            if e.name != "store-missing":
                raise
            raise Refused("export-changed", f"no published export at L/export/{export_class}/{s}") from None
        held.append(s_dfd)
        src, sst = _open_source(s_dfd, "bundle", "export-changed", ctx)
        held.append(src)
        # L3 s2 r1 codex MED: the digest asked for, checked on the open file right after its fstat, before any header
        # read, sizing or seal-in, so a published file that is not the one asked for is export-changed whatever else
        # it would fail; the copy-in hashes again (a change after this read).
        if _fd_sha256(src, sst.st_size) != bundle_digest:
            raise Refused("export-changed", "bundle: not the digest asked for")
        try:
            with ctx.store.dir("retained", s, "tree", "in") as rin_dfd:
                base_fd, bst = _open_source(rin_dfd, "repo.bundle", "base-bundle-mismatch", ctx)
        except Refused as e:
            if e.name == "base-bundle-mismatch":
                raise
            raise Refused("base-bundle-mismatch", f"the retained base bundle: {e.name}") from None
        held.append(base_fd)
        if bst.st_size != base_bytes:
            raise Refused("base-bundle-mismatch", f"{bst.st_size} bytes, alloc.base says {base_bytes}")
        if _fd_sha256(base_fd, base_bytes) != str(front.base["bundle_sha256"]):    # the same, for the pinned base
            raise Refused("base-bundle-mismatch", "the retained base bundle: not alloc.base's digest")
        bundle_bytes = sst.st_size
        h = AV.read_header_at(src)                 # root's grammar over the importer's bytes (bundle-header-mismatch)
        ER.importer_bundle_check(s, fmt)(h)
        objects = AV.pack_count_at(src, h.size, bundle_bytes - h.size)
        result = "delta" if h.prerequisites else "unrelated"
        try:
            bh = AV.read_header_at(base_fd)
            base_objects = AV.pack_count_at(base_fd, bh.size, base_bytes - bh.size)
        except Refused as e:
            raise Refused("base-bundle-mismatch", f"the base bundle's header: {e.name}") from None
        image_bytes = verify_image_bytes(bundle_bytes=bundle_bytes, base_bytes=base_bytes, bundle_objects=objects,
                                         base_objects=base_objects, object_format=fmt)
        caps = {"verified.json": VERIFIED_CAP, "q.idx": idx_bound(objects, fmt), "preview.txt": VC.PREVIEW_OUTPUT_CAP}
        need = V5_FLOOR_BYTES + image_bytes + sum(caps.values())
        with ctx.store.dir("export") as efd:
            free = ctx.seams.free_bytes(efd)
        if free < need:
            raise Refused("preview-space", f"{free} bytes free < V30 {need}")
        wall_s = verify_wall_s(bundle_bytes, base_bytes)
        facts = ER.SealFacts(host=host, role_key="verifier", export_dir=export_dir, uid=uid, image_bytes=image_bytes,
                             image_inodes=IMAGE_INODES_FIXED, memory_cap=run_seams.memory_cap,
                             pids_max=run_seams.pids_max)
        seal = run_seams.seal(facts) if run_seams.seal is not None else ER.SealSeams(
            prove=ER.export_domain_prover(host, "verifier", uid))
        run = ER.SealedRun(facts, seal)   # type: ignore[arg-type]
        gid = (run_seams.publish_gid or (lambda c, k: c.seams.group_gid(wall.EXPORT_GROUPS[("importer", k)])))(
            ctx, export_class)
        at("verify:6")
        run.seal_in(s_dfd, VERIFY_DIR + ER.STAGING_SUFFIX)    # stale verify.new + the early probe, inside <s>/
        pid: Optional[int] = None
        reaped = [False]
        sealed_out = False
        try:
            work = ER.mount_path(export_dir, "verifier")
            in_dfd, out_fd, err_fd = make_verify_work(ctx, work, uid)
            run.fds += [in_dfd, out_fd, err_fd]
            at("verify:copy-in")
            copy_in(src, bundle_bytes, in_dfd, "bundle", want=bundle_digest, refusal="export-changed",
                    fsync=ctx.seams.fsync)
            copy_in(base_fd, base_bytes, in_dfd, "base.bundle", want=str(front.base["bundle_sha256"]),
                    refusal="base-bundle-mismatch", fsync=ctx.seams.fsync)
            run.fds.remove(in_dfd)
            os.close(in_dfd)
            if host == "darwin":
                with ctx.store.dir("export") as efd:
                    replace_at(efd, "verifier.sb", RD.render_verifier_profile(host), mode=0o644,
                               fsync=ctx.seams.fsync)
                if not vseams.pf_live(uid):
                    raise Refused("verifier-network-unisolated", "pf is off or the user _levain_verify rule is not live")
            cmd = run_seams.launch(ctx, verifier_args(s, front.base, result, bundle_bytes), export_class)
            frame_r, frame_w = pair = os.pipe()    # fd 0: a pipe at EOF (the verifier reads no stdin)
            run.fds += pair
            run.fds.remove(frame_w)
            os.close(frame_w)
            result_r, result_w = pair = os.pipe()
            run.fds += pair
            at("verify:7")
            try:
                pid = run_seams.spawn(cmd, frame_r=frame_r, stdout_fd=out_fd, stderr_fd=err_fd, result_w=result_w,
                                      rlimits=export_rlimits(image_bytes, wall_s))
            finally:
                for fd in (frame_r, result_w, out_fd, err_fd):   # the child's now; root keeps only result_r
                    run.fds.remove(fd)
                    os.close(fd)
            run.start_watchdog()
            at("verify:10")
            exit_kind, code, line, _ = collect_export(pid, result_r, wall_s + run_seams.result_grace_s,
                                                      cap=IM.RESULT_CAP, on_reap=lambda: reaped.__setitem__(0, True))
            run.fds.remove(result_r)
            os.close(result_r)
            at("verify:11")
            sealed_out = True
            reading = run.seal_out(() if reaped[0] else (pid,))
            at("verify:classify")
            outcome = ER.classify(ER.RunObservation(exit_kind, code, reading.memory_killed, reading.memory_unreadable,
                                                    reading.host_short, line), ER.VERIFIER, parse=AV.parse_verifier_line)
            if outcome.success and outcome.line is not None and outcome.line.result != result:
                outcome = ER.Outcome(False, "verifier-failed:result-malformed",
                                     f"the line's result {outcome.line.result} is not root's {result}", outcome.line)
            at("verify:publish")
            work_dfd = os.open(work, _O_DIR)
            run.fds.append(work_dfd)
            _res, pub = ER.publish_if_success(outcome, work_dfd, s_dfd, VERIFY_DIR, object_format=fmt, owner_uid=uid,
                                              caps=caps, seams=seal.publish, gid=gid,  # type: ignore[attr-defined]
                                              before_commit=detach_before_commit(run, seal, work_dfd),
                                              role=ER.VERIFIER)
            at("verify:12")
            d = pub.digests
            out: Dict[str, object] = {"verified_digest": "sha256:" + d["verified.json"],
                                      "idx_digest": "sha256:" + d["q.idx"],
                                      "preview_digest": "sha256:" + d["preview.txt"]}
            if pub.warnings:               # adopt-open's rule (A2; head 1022+20 S2-Q6): only a post-commit failure
                out["warnings"] = list(pub.warnings)
            return out
        finally:
            try:
                if not sealed_out:
                    # adopt-open's rule (L3 r1 M1 / ruling Q5): the domain is killed and proven before the locks go;
                    # an unproven seal-out raises export-domain-unproven over the pending failure.
                    run.seal_out(() if (pid is None or reaped[0]) else (pid,))
            finally:
                at("verify:teardown")
                run.teardown()
    finally:
        for fd in reversed(held):
            os.close(fd)


def adopt_verify(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """``adopt-verify <session> --bundle-digest sha256:<hex>`` (CONTRACT §5.1; DESIGN §6.0): the caller by class
    (:func:`verify_caller`), the alloc session lock (TRY, ``session-busy``), the alloc re-read under it and the caller
    re-checked (:func:`verify_caller_locked`: a changed alloc = ``no-session``), the root facts as adopt-open step 3
    (RETAINED, SCRUBBED, ``alloc.base``), the adopt lock (adopt-open's bounded wait, ``adopt-busy``: one sealed run
    machine-wide, importer or verifier), the git floor (``git-below-floor:<version>``), then the sealed verify run
    (:func:`verify_sealed_run`). Answers ``{ok, verified_digest, idx_digest, preview_digest, warnings?}``
    (``warnings`` only when the publish recorded a post-commit failure, adopt-open's rule); the adopt lock is
    released first, then the session lock."""
    assert req.session is not None and req.bundle_digest is not None
    vseams = ctx.seams.verify if ctx.seams.verify is not None else VerifySeams()
    run_seams = (ctx.seams.verify_run if ctx.seams.verify_run is not None
                 else AdoptRunSeams(launch=verify_launch_default))
    adopt = ctx.seams.adopt if ctx.seams.adopt is not None else AdoptSeams()
    at, s = ctx.seams.at, req.session
    deadline = ctx.seams.monotonic() + vseams.lock_wait_s
    a, export_class = verify_caller(ctx, s)
    at("verify:2")
    try:
        with session_lock(ctx, s):
            a = verify_caller_locked(ctx, s, a, export_class)
            at("verify:3")
            front = adopt_step3_root_facts(ctx, a, adopt, deadline=deadline)
            at("verify:4")
            try:
                with adopt_lock_wait(ctx, deadline):
                    at("verify:5")
                    FI.check_floor(vseams.git_version())
                    at("verify:floor")
                    return verify_sealed_run(ctx, front, export_class, req.bundle_digest, run_seams, vseams)
            finally:
                at("verify:adopt-unlocked")
    finally:
        at("verify:session-unlocked")


# --- discard / restore: the operator's store verbs (DESIGN §4.6, §4.7; CONTRACT §5.1, §7.4) --------------------------


def store_caller(ctx: Ctx, session: str) -> R.Alloc:
    """The alloc, bound to its operator (``SUDO_UID`` == alloc ``operator_uid``; anything else answers ``no-session``,
    so a caller learns nothing of another's sessions), as :func:`adopt_step1_caller` and the install helper's purge
    bind it. Any kind: he may discard / restore any session he operates, dispatch ones included (CONTRACT: "he can
    `discard` / `restore` / `purge` any")."""
    data = ctx.store.read(("alloc",), f"{session}.json")
    if data is None:
        raise Refused("no-session", session)
    try:
        a = R.parse_alloc(data)
    except (ContractError, Refused) as e:
        # Read before the operator check, so its failure names nothing about the record.
        raise Refused("no-session", session) from e
    if a.session != session or a.operator_uid != ctx.sudo_uid:
        raise Refused("no-session", session)
    return a


def _store_verb(ctx: Ctx, req: RuntimeRequest, verb: Callable[..., str]) -> Dict[str, object]:
    """:func:`store_verbs.discard` / :func:`store_verbs.restore` on the caller's session: the session lock (TRY,
    ``session-busy``; the same ``L/alloc/<s>.lock`` adopt-open holds, DESIGN §4.7) and the journal are the store verb's,
    its CAS goes to CONTROL. A run that stops part-way keeps its journal and is refused ``<verb>-incomplete:<phase>``
    (the install helper's purge rule); the verb run again finishes that journal first. Answers ``{state}``."""
    from levain.slot import store_verbs as SV
    from levain.slot.root_intent import PhaseFailed

    assert req.session is not None
    a = store_caller(ctx, req.session)
    seams = SV.StoreSeams(cas=SV.control_cas(ctx, req.session), owner_ok=ctx.seams.trusted)
    try:
        state = verb(SV.facts_from_alloc(a, ctx.seams.L), seams)
    except PhaseFailed as e:
        name = f"{req.verb}-incomplete:{e.phase}"
        cause = e.cause.name if isinstance(e.cause, Refused) and isinstance(e.cause.name, str) else ""
        # A cause that is itself a named refusal rides the name when the wire grammar passes it
        # (``<verb>-incomplete:<phase>.<cause>``; its own ``:qualifier`` cannot, so that one stays in the detail).
        if cause and _WIRE_NAME_RE.fullmatch(f"{name}.{cause}") and len(name) + 1 + len(cause) <= 64:
            name = f"{name}.{cause}"
        raise Refused(name, str(e.cause)) from None
    return {"state": state}


def discard(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """``discard <session>``: RETAINED -> TRASH (DESIGN §4.6). Refuses as :func:`store_verbs.discard` does
    (``not-retained:trash`` for a session already in trash)."""
    from levain.slot import store_verbs as SV
    return _store_verb(ctx, req, SV.discard)


def restore(ctx: Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """``restore <session>``: TRASH -> RETAINED, rename only (DESIGN §4.6). Refuses as :func:`store_verbs.restore`."""
    from levain.slot import store_verbs as SV
    return _store_verb(ctx, req, SV.restore)


VERB_HANDLERS: Dict[str, Callable[[Ctx, RuntimeRequest, object], Dict[str, object]]] = {
    "prepare": prepare, "render": render, "resume": resume, "status": status,
    "start": _start, "teardown": _teardown, "adopt-open": adopt_open, "adopt-verify": adopt_verify,
    "discard": discard, "restore": restore, "probe": _probe, "confirm-identity": _confirm_identity,
}


# --- main ----------------------------------------------------------------------------------------------------------------

#: The only names :func:`main` reads from its environment (``SUDO_UID`` for the caller, ``TERM`` for render). The
#: root entry hands main exactly these, never a copy of ``os.environ``: main gives no child its environment, and a name
#: it is not given cannot reach one later.
ENTRY_ENV = ("SUDO_UID", "TERM")


def entry_env(environ: Mapping[str, str], names: Sequence[str] = ENTRY_ENV) -> Dict[str, str]:
    """The part of ``environ`` a root entry's :func:`main` reads: ``names``, when set."""
    return {k: environ[k] for k in names if k in environ}


def _sudo_uid(env: Mapping[str, str]) -> int:
    raw = env.get("SUDO_UID")
    if type(raw) is not str or not _SUDO_UID_RE.fullmatch(raw):      # never root, one spelling, ASCII only
        raise Refused("no-sudo-uid", "SUDO_UID")
    return int(raw)


def _caller(seams: Seams, sudo_uid: int, operator_uid: int) -> str:
    if sudo_uid == operator_uid:
        return OPERATOR
    try:
        if sudo_uid == seams.account_uid("_levain_dispatch"):
            return DISPATCH
    except Refused:
        pass
    raise Refused("unknown-caller", str(sudo_uid))


def run(argv: Sequence[str], *, stdin: bytes, env: Mapping[str, str], seams: Seams) -> Dict[str, object]:
    """The verb's ok fields, or :class:`Refused`. Reads only until the verb's first act."""
    if seams.euid() != 0:
        raise Refused("not-root", "the runtime helper runs as root")
    sudo_uid = _sudo_uid(env)
    store = Store(seams)
    ctx = None
    try:
        operator_uid = read_operator_uid(store)
        caller = _caller(seams, sudo_uid, operator_uid)
        req = parse_runtime_argv(argv, configured_slots=configured_slots(store), caller=caller)
        blob = parse_stdin(req.verb, stdin)
        bind_stdin(req, blob)
        handler = VERB_HANDLERS.get(req.verb)
        if handler is None:
            raise Refused(f"verb-unbuilt:{req.verb}", req.verb)
        ctx = Ctx(seams, store, sudo_uid, caller, operator_uid, env)
        return handler(ctx, req, blob)
    finally:
        if ctx is not None:
            ctx.close()
        store.close()


def main(argv: Sequence[str], *, stdin: bytes, env: Mapping[str, str], seams: Optional[Seams] = None,
         out=None, err=None) -> int:
    """Exactly one canonical JSON line on ``out``; the refusal's detail (never on stdout) on ``err``."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        fields = run(argv, stdin=stdin, env=env, seams=seams or Seams())
        line = ok_line(fields)
    except Refused as e:
        err.write(f"levain runtime-helper: refused {e.name}: {e.detail!r}\n")
        out.write(refusal_line(e))
        out.flush()
        return EXIT_REFUSED
    except Exception as e:   # noqa: BLE001 - one line on stdout, whatever went wrong
        err.write(f"levain runtime-helper: internal error: {type(e).__name__}: {e}\n")
        out.write(refusal_line(Refused("internal")))
        out.flush()
        return EXIT_INTERNAL
    out.write(line)
    out.flush()
    return EXIT_OK


def _read_stdin() -> bytes:
    """At most the grammar's cap + 1 bytes (parse_stdin refuses the extra one by size). DECISION: a verb with no
    stdin reads none, so a terminal on stdin never blocks it."""
    data = bytearray()
    fd = sys.stdin.fileno()
    while len(data) <= 64 * 1024:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    return bytes(data)


__all__ = ["BASE_ROUTES_FORMAT", "CAS_UNMOVED", "CREDENTIAL_NAME", "Ctx", "DirRow", "EXIT_INTERNAL", "EXIT_OK", "EXIT_REFUSED",
           "FRONT_DROPPED_FIELDS", "FRONT_LOG_MODE", "FRONT_LOG_NAME", "FRONT_MODULE", "FRONT_POLICY_NAME", "GO_BYTE", "GO_FD", "GO_FD_ENV",
           "Leased", "LinkedNotDurable", "PREPARE_RESULT_FORMAT",
           "RENDER_STEPS", "RenderRun", "SLOT_OWNED_RENDER_DIRS", "Seams", "SlotView", "Store", "VERB_HANDLERS",
           "CHECK_S", "base_routes", "capture_target", "configured_slots", "drain", "reap_within", "wait_child_within", "ENTRY_ENV", "entry_env",
           "create_alloc", "create_at", "kill_front", "lease_slot", "main", "make_cgroup", "open_slot_owned_dir",
           "parse_slot_state_of", "pick_slot", "prepare", "proc_identity", "read_view", "render",
           "render_layer_rows", "resume", "revert_lease", "run", "session_dir_rows", "session_lock",
           "session_lock_name", "session_owner_uid", "slot_owned_dir_ok", "spawn_front", "spawn_target", "status",
           "front_policy_bytes", "front_session_routes", "gateway_front_args", "prepare_front_log",
           "account_in_range", "check_front_log_dirs",
           "refuse_ticketed_front", "stub_front_args", "wait_child", "write_front_policy"]


if __name__ == "__main__":   # pragma: no cover - the root entry (root_tree.ENTRY_MODULES)
    _args = sys.argv[1:]
    _stdin = _read_stdin() if _args[:1] in (["prepare"], ["render"]) else b""
    raise SystemExit(main(_args, stdin=_stdin, env=entry_env(os.environ)))
