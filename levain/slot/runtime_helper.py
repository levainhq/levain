"""The runtime helper's ``__main__``: one root NOPASSWD entry, argv + stdin in, one canonical JSON line out
(CONTRACT §5 preamble, §5.1).

:func:`main` derives the caller from ``SUDO_UID`` (the operator recorded in ``L/policy/operator``, or
``_levain_dispatch``), reads the configured slots from the store (never from argv), passes argv and stdin through
:mod:`levain.slot.runtime_grammar` and runs the verb. Every refusal is :func:`runtime_grammar.refusal_line` and a
non-zero exit, raised before the next act: nothing after the refusing check acts.

Verbs built here: ``prepare``, ``render``, ``resume``, ``status``; ``start`` and ``teardown`` dispatch to
:mod:`levain.slot.start`. ``confirm-identity`` (session 2f's) and ``adopt-open`` / ``discard`` / ``restore``
(session 5's) answer ``verb-unbuilt:<verb>``. A ticketed (dispatch) ``prepare`` answers ``verb-unbuilt:ticket``
(session 2f's).

Every root filesystem act goes through a dirfd: the store root is opened once with ``O_DIRECTORY|O_NOFOLLOW`` and
every path below it is reached one component at a time with ``O_NOFOLLOW`` (§5 preamble), so no argv token, stdin
field or planted link names a path. Every OS act a unit test cannot do as a non-root user is a field of
:class:`Seams` with a real default (owners are recorded, not applied, in the tests).

The slot state file ``L/slots/{e,v}<n>/state`` (``levain-slot-state/1``, §3.5 Pool) has ONE definition,
:mod:`levain.slot.records`; ``prepare`` (lease) and ``teardown`` (SCRUBBED) read and write it through it.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from levain.contracts import ContractError, canon
from levain.slot import Refused, wall, write_all
from levain.slot import proc as PR
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import stage as ST
from levain.slot.end import V5_FLOOR_BYTES
from levain.slot.grammar import SESSION_RE
from levain.slot.ids import SlotId, in_own_range
from levain.slot.layout import BASE_ROUTES_FORMAT, RUN, STORE
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
#: §5.1 render (4): the front program is a parameter; session 2's is the stub, session 3a's the gateway front.
FRONT_MODULE = "levain.broker.stub"

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


def _root_trusted(st: os.stat_result) -> bool:
    """A store directory root may act below: root-owned, not group- or other-writable."""
    return st.st_uid == 0 and not st.st_mode & 0o022


def _fchown(fd: int, uid: int, gid: int, rel: str) -> None:
    os.fchown(fd, uid, gid)


def _free_bytes(store_fd: int) -> int:
    st = os.fstatvfs(store_fd)
    return st.f_bavail * st.f_frsize


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
    ``/proc/<pid>/stat`` field 22, clock ticks), the ONE start-time source, so MINT's ``front.start`` and every
    later live check (keys' peer check, END, SCRUBBED) read the same units. A pid whose facts cannot be read
    counts as unknown (None): nothing is signalled on it."""
    p = PR.default_seams().probe(int(pid))
    if p is None or p.state == PR.UNKNOWN or not p.uids:
        return None
    return p.uids[0], p.start


def _sigkill_and_reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        os.waitpid(pid, 0)          # the helper is the front's parent until it exits: reap it
    except ChildProcessError:
        pass


def darwin_user_dir(uid: int, gid: int) -> str:
    """macOS: ``uid``'s ``/private/var/folders/<a>/<b>`` (render's ``var_folders``), from ``getconf
    DARWIN_USER_DIR`` run AS that uid (``/var/folders/<a>/<b>/0/``). [not run: a 2g row]"""
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
                 detach: bool) -> int:
    """``start``'s fork through the wall (§5.1 start, §5.4 step 3, §9.1): the child keeps fds 0/1/2 (the helper's
    own stdio, sudo's pty; ``detach``: ``/dev/null`` + ``setsid``, the forwarder), the spec pipe (moved to
    ``SPEC_FD`` by :func:`wall.exec_launch`) and, when ``exec_w`` is given, that pipe's write end on
    :data:`wall.EXEC_FD`: the vendor's exec marker (the last hop writes ``+`` just before the vendor's execve) or
    the forwarder's readiness pipe (:data:`wall.READY_FD`, the same fd). Every other fd is closed. A child that
    fails before its exec writes ``!`` on whichever fd holds the pipe at that moment (``exec_w`` itself until
    the dup2 onto ``EXEC_FD`` succeeded), so its failure is never only an EOF. Only root can run it (a VM row)."""
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
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


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


def stub_front_args(host: str, slot: SlotId) -> Tuple[str, ...]:
    """The FIXED argv root gives the session-2 front, ``levain.broker.stub`` (§5.1 render (4), lane F3 b): the
    Linux unix shape on ``R/gw/slot<n>/gw.sock`` (§2.2), the macOS TCP shape on ``gwport_n``. Nothing in it is
    slot- or session-supplied. DECISION: its log is ``/dev/null`` (the stub has nothing a session needs kept)."""
    if host == "darwin":
        return ("--port", str(RD.gwport_for(host, slot)), "--log", "/dev/null")
    sock = RUN[host] / "gw" / f"slot{slot.index}" / "gw.sock"
    return ("--unix", str(sock), "--slot", str(slot.index), "--log", "/dev/null")


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
    proc_identity: Callable[[int], Optional[Tuple[int, int]]] = proc_identity
    kill: Callable[[int], None] = _sigkill_and_reap
    var_folders: Callable[[int, int], str] = darwin_user_dir
    wake_reaper: Optional[Callable[[], None]] = None
    hook_templates: Mapping[str, RD.HookTemplate] = field(default_factory=dict)
    front_module: str = FRONT_MODULE
    front_args: Callable[[str, SlotId], Sequence[str]] = stub_front_args
    lock_wait_s: float = LOCK_WAIT_S
    at: Callable[[str], None] = lambda step: None
    # start / teardown (levain.slot.start):
    spawn: Callable[[Sequence[str], bytes, Optional[str], Optional[int], bool], int] = spawn_target
    wait: Callable[[int], int] = wait_child
    egress_ready: Callable[[SlotId, Mapping[str, int]], None] = wall.macos_egress_ready
    netns_names: Callable[[], Iterable[str]] = wall.read_netns_names
    host_links: Callable[[], Mapping[str, Optional[str]]] = wall.read_host_links
    make_cgroup: Callable[[str], None] = make_cgroup
    #: proc.ProcSeams for END's freeze / kill / proof and SCRUBBED (None = the host's real process table).
    proc_seams: Optional[object] = None
    #: EndSeams fields a test replaces (owner_ok, meta_reader, strip, chown, run_as, ...); empty in production.
    end_overrides: Mapping[str, object] = field(default_factory=dict)

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
            chunk = os.read(fd, 65536)
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


def create_at(dirfd: int, name: str, data: bytes, *, mode: int, fsync: Callable[[int], None]) -> None:
    """:func:`replace_at` that never replaces: the temp is LINKED to ``name`` (``linkat`` fails on an existing name,
    atomically), so an existing record raises ``FileExistsError`` and is left as it was; a reader never sees a
    partial file. The temp name is ``name.new`` (DECISION: never :func:`replace_at`'s, so neither sweeps the
    other's)."""
    tmp = name + ".new"
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
        os.link(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    finally:
        os.unlink(tmp, dir_fd=dirfd)
    fsync(dirfd)


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
        """An fd on ``L/<parts…>``. ``trusted``: the LAST component must pass ``seams.trusted`` (every
        intermediate always must)."""
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
                if (trusted or i < len(parts) - 1) and not self.seams.trusted(os.fstat(cur)):
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

    def read(self, parts: Sequence[str], name: str, *, trusted: bool = True) -> Optional[bytes]:
        with self.dir(*parts, trusted=trusted) as fd:
            return read_at(fd, name, "/".join((str(self.path), *parts, name)))

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
            if len(name) < 2 or name[0] not in "ev" or not name[1:].isdigit() or str(int(name[1:])) != name[1:]:
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


def base_route_types(store: Store) -> Dict[str, str]:
    """``L/routes/base.json`` (§4.3 S1 base map): route id -> its §4.1 type. Interactive and login sessions name
    base-map ids only (§5.3), so this map is the whole set a render may name."""
    data = store.read(("routes",), "base.json")
    if data is None:
        raise Refused("base-map-missing", "L/routes/base.json")
    obj = _canon_obj(data, "base-map")
    routes = obj.get("routes")
    if obj.get("format") != BASE_ROUTES_FORMAT or type(routes) is not list:
        raise Refused("base-map-malformed", "format or routes")
    out: Dict[str, str] = {}
    for r in routes:
        if type(r) is not dict or type(r.get("route")) is not str or r.get("type") not in RD.ROUTE_TYPES \
                or r["route"] in out:
            raise Refused("base-map-malformed", "a route")
        out[r["route"]] = r["type"]
    return out


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
    if data is None:
        return None
    try:
        obj = canon.parse(data)
    except ContractError:
        return "?"
    v = obj.get("vendor") if type(obj) is dict else None
    return v if v in VENDORS else "?"


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
    leaves leased has no alloc: the reaper's orphan revert (§3.5) is its recovery."""
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
    out = {}
    for name in slot.accounts:
        uid = ctx.seams.account_uid(name)
        if not in_own_range(ctx.seams.host, name, uid):
            raise Refused("account-out-of-range", f"{name} {uid!r}")
        out[name] = uid
    return out


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


def make_session_dirs(ctx: Ctx, session: str, rows: Sequence[DirRow]) -> None:
    with ctx.store.dir("live") as live:
        for row in rows:
            parts = [p for p in row.rel.split("/") if p]
            if not parts:
                ctx.store.mkdir(live, session, row.uid, row.gid, row.mode, f"live/{session}")
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
    seams = ctx.seams
    if ctx.caller == DISPATCH or blob.ticket is not None:
        raise Refused("verb-unbuilt:ticket", "ticketed prepare is session 2f's")
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
                        named=req.slot, ticketed=False, session=session)
    created = attach_sent = False
    try:
        slot = leased.slot
        ids = slot_ids(ctx, slot)
        first, second = slot.accounts
        linux = seams.host == "linux"
        alloc = R.Alloc(
            session=session, slot=slot, lease_seq=leased.state.lease_seq, slot_rev=leased.state.rev,
            slot_uid=ids[first], slot_gid=ids[first], kind=req.kind, vendor=req.vendor, auth=req.auth,
            operator_uid=ctx.operator_uid, supervisor_uid=ctx.sudo_uid, staging_uid=ctx.sudo_uid,
            created_at=R.timestamp(seams.clock()), boot_id=seams.boot_id(),
            child_uid=ids[second] if slot.slot_class == "none" else None,
            netns=slot.netns if linux else None, cgroup=R.cgroup_path(slot, session) if linux else None)
        seams.at("alloc")
        create_alloc(ctx, alloc)
        created = True
        claims = {"operator_uid": alloc.operator_uid, "supervisor_uid": alloc.supervisor_uid,
                  "slot": R.slot_obj(slot), "class": slot.slot_class, "kind": alloc.kind, "vendor": alloc.vendor,
                  "auth": alloc.auth, "lease_seq": alloc.lease_seq, "slot_rev": alloc.slot_rev}
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
    make_session_dirs(ctx, session, session_dir_rows(slot, ids))
    staging = make_staging(ctx, session)
    out: Dict[str, object] = {"format": PREPARE_RESULT_FORMAT, "session": session, "slot": R.slot_obj(slot),
                              "staging": str(staging)}
    if slot.slot_class == "egress":
        out["gwport"] = RD.gwport_for(seams.host, slot)
    return out


def _undo_prepare(ctx: Ctx, leased: Leased, created: Optional[str]) -> None:
    """A CLEAN prepare failure (CONTROL holds no record): the alloc it created removed, then the slot lease put back
    (:func:`revert_lease`), in that order, so a crash between them leaves the reaper's alloc-less orphan, never an
    alloc naming a reusable slot. Best effort: the refusal that got here is the answer, and a step that fails leaves
    the rest to the reaper (an alloc it cannot remove keeps the slot leased)."""
    try:
        if created is not None:
            with ctx.store.dir("alloc") as afd:
                os.unlink(f"{created}.json", dir_fd=afd)
                ctx.seams.fsync(afd)
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
    front_pid: Optional[int] = None
    front_start: Optional[int] = None
    go_w: int = -1

    @property
    def egress(self) -> bool:
        return self.alloc.slot.slot_class == "egress"


def render_facts(ctx: Ctx, alloc: R.Alloc, blob: RenderBlob) -> RD.RenderFacts:
    """The alloc facts ``render.render`` needs (§7.1: root acts on alloc only), plus the base map's route types,
    the slot's /var/folders (macOS), ``TERM`` from the supervisor's env when it is a plain terminal name, and the
    hook templates the blob names (a missing one is refused by name in ``render.check_facts``)."""
    seams = ctx.seams
    slot = alloc.slot
    egress = slot.slot_class == "egress"
    owner = alloc.slot_uid if egress else alloc.child_uid
    assert owner is not None
    folders: Tuple[str, ...] = ()
    if seams.host == "darwin":
        folders = (seams.var_folders(owner, owner),) if egress else \
            (seams.var_folders(owner, owner), seams.var_folders(alloc.slot_uid, alloc.slot_gid))
    term = ctx.env.get("TERM")
    if term is not None and not RD._TERM_RE.fullmatch(term):
        term = None
    return RD.RenderFacts(
        host=seams.host, session=alloc.session, slot=slot, kind=alloc.kind, vendor=alloc.vendor, auth=alloc.auth,
        slot_uid=owner, slot_gid=owner, gwport=RD.gwport_for(seams.host, slot) if egress else None,
        route_types=base_route_types(ctx.store), term=term, var_folders=folders,
        driver_uid=None if egress else alloc.slot_uid, driver_gid=None if egress else alloc.slot_gid,
        hook_templates={k: v for k, v in seams.hook_templates.items() if k in blob.hooks})


def render_step1_cas_rendering(run: RenderRun) -> None:
    """(1) CAS ALLOCATED -> RENDERING. An ALLOCATED record is always generation 0 (ATTACH creates it there and
    any later write moves it out of ALLOCATED or ends its lease). Refused = stop: nothing started."""
    ans = run.ctx.call("CAS", session=run.alloc.session, generation=0, **{"from": R.ALLOCATED, "to": R.RENDERING},
                       fields={})
    run.generation = int(ans["generation"])


def render_step2_policy(run: RenderRun) -> None:
    """(2) Validate the (class, kind, vendor, auth, routes, hooks) matrix and write the policy into alloc with
    ``front:{uid, pending:true}`` (egress), fsync. DECISION: ``incarnation`` = the slot's ``lease_seq``, unique per
    lease of the slot (the contract types it nowhere; records.py reads it as a uint)."""
    ctx, a, blob = run.ctx, run.alloc, run.blob
    facts = render_facts(ctx, a, blob)
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


def render_layer_rows(slot: SlotId, owner_gid: int) -> List[DirRow]:
    """§2.1 rows ``render`` makes: ``<S>/in/`` root : the slot (verifier: the child) 0750; ``<S>/root/`` root : the
    slot 0750 (verifier root : root 0755, as a none-class layer is world-readable, §2.1). DECISION: ``root/hooks/``
    takes ``root/``'s row."""
    if slot.slot_class == "egress":
        layer = (0, owner_gid, 0o750)
    else:
        layer = (0, 0, 0o755)
    return [DirRow("in", 0, owner_gid, 0o750), DirRow("root", *layer), DirRow("root/hooks", *layer)]


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
    """(3) The staging bundle -> a FRESH ``<S>/in/repo.bundle`` (§5.4: the reader runs as alloc ``staging_uid``);
    then ``<S>/root/*`` and the onboarding pre-seed from ``render.render``, each a fresh file with its §2.1 row;
    then staging is emptied, only now that every staged file's destination is durable."""
    ctx, a = run.ctx, run.alloc
    seams = ctx.seams
    facts = run.facts
    assert facts is not None
    rendered = RD.render(run.blob, facts)
    S = ("live", a.session)
    for row in render_layer_rows(a.slot, facts.slot_gid):
        *parent, name = row.rel.split("/")
        with ctx.store.dir(*S, *parent) as pfd:
            ctx.store.mkdir(pfd, name, row.uid, row.gid, row.mode, f"live/{a.session}/{row.rel}")
            seams.fsync(pfd)
    with ctx.store.dir("staging", a.session, trusted=False) as staging, ctx.store.dir(*S, "in") as dest:
        ST.stage_bundle(staging, dest, staging_uid=a.staging_uid, staging_gid=seams.user_gid(a.staging_uid),
                        dest_gid=facts.slot_gid, run_as=seams.stage_run_as, fsync=seams.fsync,
                        chown=lambda fd, u, g: seams.chown(fd, u, g, f"live/{a.session}/in/{ST.BUNDLE_NAME}"))
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
    # DECISION: only the names this render staged are removed (the verifier's driver_input.json is session 2f's to
    # stage, so it is never removed here unstaged); the dir itself is END's ("staging deleted").
    with ctx.store.dir("staging", a.session, trusted=False) as staging:
        os.unlink(ST.BUNDLE_NAME, dir_fd=staging)
        seams.fsync(staging)


def front_launch(run: RenderRun) -> Tuple[Tuple[str, ...], bytes]:
    """wall's FRONT path (§8.2 last sentence): drop + assert to ``_levain_gw<n>``, no profile, no ``asuser``; the
    program is ``seams.front_module`` with root's fixed ``seams.front_args``, and its one extra fd is the GO pipe
    on :data:`GO_FD`."""
    ctx, a = run.ctx, run.alloc
    seams = ctx.seams
    gw = a.slot.accounts[1]
    spec = wall.launch_spec(seams.host, "front", slot=a.slot, session=a.session, ids={gw: run.front_uid},
                            shared={"levain-gwfronts": seams.group_gid("levain-gwfronts")},
                            argv=tuple(seams.front_args(seams.host, a.slot)), extra_env={GO_FD_ENV: str(GO_FD)},
                            keep_fds=(GO_FD,), front_module=seams.front_module)
    if seams.host == "darwin":
        argv = wall.macos_launch_argv(spec)
    else:
        argv = wall.linux_launch_argv(spec, slot=a.slot, session=a.session, kind=a.kind).argv
    return argv, spec.to_json()


def render_step4_front(run: RenderRun) -> None:
    """(4) egress only: the front DETACHED, blocked reading the GO pipe; its ``pid`` + ``start`` into alloc, fsync."""
    if not run.egress:
        return
    seams = run.ctx.seams
    argv, spec_json = front_launch(run)
    go_r, go_w = os.pipe()
    try:
        run.front_pid = seams.spawn_front(argv, spec_json, go_r)
        run.go_w = go_w
    except BaseException:
        os.close(go_w)
        raise
    finally:
        os.close(go_r)
    ident = seams.proc_identity(run.front_pid)
    if ident is None:
        raise Refused("front-start-unknown", str(run.front_pid))
    run.front_start = ident[1]
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
        run = RenderRun(ctx, a, blob)
        try:
            for name, step in RENDER_STEPS:
                ctx.seams.at(name)
                step(run)
        except BaseException:
            kill_front(run)
            raise
    return {}


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
    hold is ``no-session``."""
    if req.session is not None:
        return {"records": [R.alloc_record(read_alloc(ctx, req.session))]}
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


VERB_HANDLERS: Dict[str, Callable[[Ctx, RuntimeRequest, object], Dict[str, object]]] = {
    "prepare": prepare, "render": render, "resume": resume, "status": status,
    "start": _start, "teardown": _teardown,
}


# --- main ----------------------------------------------------------------------------------------------------------------

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


__all__ = ["BASE_ROUTES_FORMAT", "CREDENTIAL_NAME", "Ctx", "DirRow", "EXIT_INTERNAL", "EXIT_OK", "EXIT_REFUSED",
           "FRONT_MODULE", "GO_BYTE", "GO_FD", "GO_FD_ENV", "Leased", "PREPARE_RESULT_FORMAT", "RENDER_STEPS",
           "RenderRun", "SLOT_OWNED_RENDER_DIRS", "Seams", "SlotView", "Store", "VERB_HANDLERS", "configured_slots",
           "create_alloc", "create_at", "kill_front", "lease_slot", "main", "make_cgroup", "open_slot_owned_dir",
           "parse_slot_state_of", "pick_slot", "prepare", "proc_identity", "read_view", "render",
           "render_layer_rows", "resume", "revert_lease", "run", "session_dir_rows", "session_lock",
           "session_lock_name", "session_owner_uid", "slot_owned_dir_ok", "spawn_front", "spawn_target", "status",
           "stub_front_args", "wait_child"]


if __name__ == "__main__":   # pragma: no cover - the root entry (root_tree.ENTRY_MODULES)
    _args = sys.argv[1:]
    _stdin = _read_stdin() if _args[:1] in (["prepare"], ["render"]) else b""
    raise SystemExit(main(_args, stdin=_stdin, env=dict(os.environ)))
