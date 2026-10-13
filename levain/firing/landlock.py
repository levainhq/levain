"""levain.firing.landlock — unix-domain connect default-deny for confined bash on Linux (Landlock).

THE PROPERTY, per tier. On a kernel whose Landlock ABI is 9 or later (tier T9), a confined process
cannot connect to a unix-domain socket, pathname or abstract, that was created outside its sandbox,
except one the operator granted before it started; on ABI 6 to 8 (T6) only abstract sockets are
closed and pathname sockets stay reachable; below ABI 6 (T0) there is no socket-level control for
bash running as the operator, and levain makes no claim there (the guarantee is ``levain
setup-isolation``). T9 and T6 also refuse signals to processes outside the sandbox. The tier is read
from the RUNNING kernel (``landlock_create_ruleset`` with the version flag), never from ``uname``.

WHERE IT RUNS. Each command's bash is started through :data:`STUB`, a program run INSIDE bwrap,
between bwrap (or the hands relay) and bash: it restricts itself and then execs bash, so the policy
covers bash and everything bash starts. It cannot run before bwrap: a Landlock domain that handles a
filesystem right may not mount, and bwrap has to. It runs as ``python3 -I -S``, so it imports nothing
of levain: the program text below is the whole of it, and levain's own side (:func:`plan`,
:func:`running_abi`, the tests) runs the SAME definitions, executed from the same text, never a copy.

What the program does, in order:
1. Spawn hygiene: every fd above 2 is closed; a socket on stdin becomes /dev/null and a socket on
   stdout or stderr becomes a pipe a relay copies into it, except levain's own state channel on stdin
   (an unnamed socketpair, kept when levain says it is there). An fd held at entry is outside every
   kernel check, so this is a spawn rule, not a kernel claim.
2. Reads the running ABI and builds the ruleset for THAT ABI only: a field or right the ABI lacks is
   never sent (the scope field below ABI 6 is refused with E2BIG; the unix-resolve right before
   ABI 9 is refused with EINVAL; both measured).
3. T9 and T6: binds an abstract canary socket (created now, so outside the domain about to be made),
   applies the ruleset, then connects to it and to the pathname canary levain created outside the
   sandbox and bound into the view. T9 requires both refused (EACCES or EPERM), T6 the abstract one
   refused and the pathname one answered. Any other result (connected, ENOENT, ECONNREFUSED, a
   timeout) refuses the command with a named error: the policy is not enforcing what the tier claims.
4. Execs the command.

Never on macOS: every path that issues the Landlock syscalls checks for Linux first, and the unit
tests replace the syscall layer.
"""

from __future__ import annotations

import os
import platform
import select
import socket
import tempfile
import threading

# The program each confined bash starts through. Its argv: options, then ``--`` and the command.
#   --grant PATH          an operator-declared socket, granted by its exact inode (a socket file,
#                         never a directory: a directory grant admits every socket later put in it).
#                         Missing or not a socket: the command is refused, naming it.
#   --default-grant PATH  a levain default grant (/dev/log): granted when it resolves to a socket in
#                         the view, skipped otherwise.
#   --private-tmp DIR     the unix-resolve right beneath this directory (a per-run private tmp, never
#                         the workspace).
#   --canary PATH         the pathname canary socket, created outside the sandbox by levain.
#   --carry-stdin         stdin is levain's state channel (an unnamed socketpair): kept.
_STUB_DEFS = r"""import ctypes, errno, os, socket, stat, struct, sys
SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
FS_REFER = 1 << 13
# Kernel documentation (userspace-api/landlock.rst), LANDLOCK_ACCESS_FS_RESOLVE_UNIX: "This access
# right is available since the ninth version of the Landlock ABI."
FS_RESOLVE_UNIX = 1 << 16
UNIX_RESOLVE_ABI = 9
# "Scope flags ... This is supported since Landlock ABI version 6."
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1, 2
SCOPE_ABI = 6
PR_SET_NO_NEW_PRIVS = 38
DENIED = ("EACCES", "EPERM")
def tier_for(abi):
    return "T9" if abi >= UNIX_RESOLVE_ABI else "T6" if abi >= SCOPE_ABI else "T0"
def plan(abi, grants=(), private_tmp=None):
    # (tier, ruleset_attr bytes or None, [(path, access, kind)]): the ruleset for THIS abi only.
    # T9 handles only connect-relevant rights. REFER is handled and granted beneath "/": the kernel
    # documentation says of LANDLOCK_ACCESS_FS_REFER "This is the only access right which is denied by
    # default by any ruleset, even if the right is not specified as handled at ruleset creation time",
    # so without the grant every rename or link across directories would fail, a change to what bash
    # may do with files. "The reparented file may not gain more access rights in the destination
    # directory" (EXDEV) binds only where the unix-resolve right differs: the granted socket inodes and
    # the private tmp, when one is given.
    tier = tier_for(abi)
    if tier == "T0":
        return tier, None, []
    scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
    if tier == "T6":
        return tier, struct.pack("=QQQ", 0, 0, scoped), []
    rules = [("/", FS_REFER, "dir")]
    rules += [(g, FS_RESOLVE_UNIX, "socket") for g in grants]
    if private_tmp is not None:
        rules.append((private_tmp, FS_RESOLVE_UNIX, "dir"))
    return tier, struct.pack("=QQQ", FS_RESOLVE_UNIX | FS_REFER, 0, scoped), rules
def syscall(nr, *args):
    if os.uname().sysname != "Linux":
        raise OSError(errno.ENOSYS, "Landlock is Linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    r = libc.syscall(ctypes.c_long(nr), *args)
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return r
def running_abi():
    # 0 when the kernel has no Landlock or it is disabled: T0, never a refusal.
    try:
        return int(syscall(SYS_CREATE_RULESET, ctypes.c_void_p(None), ctypes.c_size_t(0),
                           ctypes.c_uint32(CREATE_RULESET_VERSION)))
    except OSError:
        return 0
def open_rule_target(path, kind):
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if kind == "dir" else 0))
    if kind == "socket" and not stat.S_ISSOCK(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.ENOTSOCK, "not a socket")
    return fd
def no_new_privs():
    if os.uname().sysname != "Linux":
        raise OSError(errno.ENOSYS, "prctl is Linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        e = ctypes.get_errno()
        raise OSError(e, "no_new_privs: " + os.strerror(e))
def apply(attr, rules):
    buf = ctypes.create_string_buffer(attr, len(attr))
    rs = syscall(SYS_CREATE_RULESET, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    try:
        for path, access, kind in rules:
            try:
                fd = open_rule_target(path, kind)
            except OSError as e:
                raise OSError(e.errno, "%s (%s)" % (path, e.strerror))
            try:
                rb = ctypes.create_string_buffer(struct.pack("=Qi", access, fd), 12)
                syscall(SYS_ADD_RULE, ctypes.c_int(rs), ctypes.c_int(RULE_PATH_BENEATH), rb, ctypes.c_uint32(0))
            finally:
                os.close(fd)
        no_new_privs()
        syscall(SYS_RESTRICT_SELF, ctypes.c_int(rs), ctypes.c_uint32(0))
    finally:
        os.close(rs)
def open_fds():
    for d in ("/proc/self/fd", "/dev/fd"):
        try:
            return sorted(int(x) for x in os.listdir(d))
        except OSError:
            continue
    return list(range(3, 1024))
def is_socketpair(fd):
    s = socket.socket(fileno=os.dup(fd))
    try:
        return s.family == socket.AF_UNIX and s.getpeername() in ("", b"")
    except OSError:
        return False
    finally:
        s.close()
def relay_into(fd):
    # A pipe in place of a socket on stdout/stderr, and a process that copies the pipe into it: what
    # bash gets is a byte stream, never the socket (no fd passing, no peer of its own).
    r, w = os.pipe()
    if os.fork() == 0:
        os.close(w)
        while True:
            d = os.read(r, 65536)
            if not d:
                os._exit(0)
            try:
                while d:
                    d = d[os.write(fd, d):]
            except OSError:
                os._exit(0)
    os.close(r)
    os.dup2(w, fd)
    os.close(w)
def hygiene(carry_stdin):
    for fd in open_fds():
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass
    for fd in (0, 1, 2):
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            continue
        if not stat.S_ISSOCK(mode) or (fd == 0 and carry_stdin and is_socketpair(0)):
            continue
        if fd == 0:
            n = os.open(os.devnull, os.O_RDONLY)
            os.dup2(n, 0)
            os.close(n)
        else:
            relay_into(fd)
def connect(addr):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(addr)
        return "CONNECTED"
    except socket.timeout:
        return "TIMEOUT"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
    finally:
        s.close()
def judge(tier, pathname, abstract):
    # None when the probe shows what the tier claims, else what is wrong.
    if tier == "T9" and (pathname not in DENIED or abstract not in DENIED):
        return ("a connect to a unix socket created outside the sandbox was not refused (pathname: %s, "
                "abstract: %s; the kernel's refusal, EACCES or EPERM, was expected on Landlock T9)"
                % (pathname, abstract))
    if tier == "T6" and (pathname != "CONNECTED" or abstract not in DENIED):
        return ("the unix socket check did not show the T6 policy (pathname: %s, expected CONNECTED; "
                "abstract: %s, expected EACCES or EPERM)" % (pathname, abstract))
    return None
def parse(args):
    opts = {"grant": [], "default-grant": [], "private-tmp": None, "canary": None, "carry-stdin": False}
    i = 0
    while i < len(args) and args[i] != "--":
        k = args[i][2:] if args[i].startswith("--") else None
        if k == "carry-stdin":
            opts[k] = True
            i += 1
        elif k in ("grant", "default-grant") and i + 1 < len(args):
            opts[k].append(args[i + 1])
            i += 2
        elif k in ("private-tmp", "canary") and i + 1 < len(args):
            opts[k] = args[i + 1]
            i += 2
        else:
            raise ValueError("unknown option %r" % args[i])
    if i >= len(args) - 1:
        raise ValueError("no command after --")
    return opts, args[i + 1:]
"""

_STUB_MAIN = r"""
def refuse(why):
    sys.stderr.write("levain: %s; refusing to start the command (fail-closed)\n" % why)
    sys.stderr.flush()
    os._exit(126)
if __name__ == "__main__":
    try:
        opts, argv = parse(sys.argv[1:])
    except ValueError as e:
        refuse("the unix-socket confinement stub was started wrongly: %s" % e)
    hygiene(opts["carry-stdin"])
    if os.uname().sysname == "Linux":
        abi = running_abi()
        grants = list(opts["grant"])
        for g in opts["default-grant"]:
            r = os.path.realpath(g)
            try:
                if stat.S_ISSOCK(os.lstat(r).st_mode):
                    grants.append(r)
            except OSError:
                pass
        tier, attr, rules = plan(abi, grants, opts["private-tmp"])
        if tier != "T0":
            if opts["canary"] is None:
                refuse("no canary socket was given to check the unix-socket policy with")
            for g in opts["grant"]:
                try:
                    os.close(open_rule_target(g, "socket"))
                except OSError as e:
                    refuse("the granted socket %s cannot be granted: %s" % (g, e.strerror))
            name = "\0levain-canary-%d-%s" % (os.getpid(), os.urandom(6).hex())
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(name)
                listener.listen(1)
                apply(attr, rules)
            except OSError as e:
                refuse("could not apply the unix-socket policy for Landlock ABI %d (%s): %s" % (abi, tier, e))
            problem = judge(tier, connect(opts["canary"]), connect(name))
            listener.close()
            if problem is not None:
                refuse(problem)
    os.execv(argv[0], argv)
"""

#: The whole program text (``python3 -I -S -c STUB <options> -- <command>``).
STUB = _STUB_DEFS + _STUB_MAIN

# levain's side runs the same definitions, executed from the same text (never a copy that can drift).
_ns: dict = {"__name__": "levain_landlock_stub"}
exec(compile(_STUB_DEFS, "<levain landlock stub>", "exec"), _ns)

#: levain's default grant set on Linux (write-only syslog). Printed by run/doctor as a named default grant.
DEFAULT_GRANTS: tuple[str, ...] = ("/dev/log",)

#: The claim per tier, the one table `run`, `doctor` and the public text are generated from. A claim is
#: made only where the kernel enforces it.
TIER_CLAIMS: dict[str, str] = {
    "T9": ("a confined process cannot connect to a unix-domain socket (pathname or abstract) created "
           "outside its sandbox, except one granted before it started; signals to processes outside "
           "the sandbox are refused"),
    "T6": ("abstract unix sockets created outside the sandbox are closed and signals to processes "
           "outside it are refused; pathname sockets, agents included, stay reachable from bash "
           "running as you"),
    "T0": ("NO AGENT CLAIM: nothing here stops bash running as you from connecting to a unix socket "
           "(an agent's included); the guarantee on this kernel is `levain setup-isolation`, a separate "
           "user for the entity's hands"),
}


def tier_for(abi: int) -> str:
    """``T9`` (ABI >= 9), ``T6`` (ABI 6..8) or ``T0`` (below 6, or no Landlock)."""
    return _ns["tier_for"](abi)


def plan(abi: int, grants: tuple[str, ...] | list[str] = (), private_tmp: str | None = None):
    """``(tier, ruleset_attr, rules)`` the stub applies on a kernel at ``abi``: the attr bytes (None on
    T0) and ``(path, access, kind)`` per path-beneath rule. Pure."""
    return _ns["plan"](abi, tuple(grants), private_tmp)


def judge(tier: str, pathname: str, abstract: str) -> str | None:
    """The stub's verdict on its canary connects: None when they show what ``tier`` claims, else the
    problem that refuses the command."""
    return _ns["judge"](tier, pathname, abstract)


def running_abi(system: str | None = None) -> int:
    """The running kernel's Landlock ABI, 0 without one. Never issues the syscall off Linux."""
    if (system or platform.system()) != "Linux":
        return 0
    return _ns["running_abi"]()


def tier_line(abi: int) -> str:
    """The unix-socket claim line for a kernel at ``abi``, as ``run`` and ``doctor`` print it."""
    tier = tier_for(abi)
    return f"unix sockets ({tier}, Landlock ABI {abi}): {TIER_CLAIMS[tier]}"


def stub_argv(python: str, *, canary: str | None, grants: tuple[str, ...] = (),
              default_grants: tuple[str, ...] = DEFAULT_GRANTS, private_tmp: str | None = None,
              carry_stdin: bool = True) -> list[str]:
    """What goes in front of bash inside bwrap. Every input is argv the entity cannot write (it is
    levain's bwrap command line). ``grants`` are paths in the sandbox's view, resolved by the caller."""
    argv = [python, "-I", "-S", "-c", STUB]
    for g in grants:
        argv += ["--grant", g]
    for g in default_grants:
        argv += ["--default-grant", g]
    if private_tmp is not None:
        argv += ["--private-tmp", private_tmp]
    if canary is not None:
        argv += ["--canary", canary]
    if carry_stdin:
        argv.append("--carry-stdin")
    return argv + ["--"]


class Canary:
    """The pathname canary: a listening unix socket levain creates OUTSIDE every sandbox, bound into
    each command's view at :attr:`view_path`. Connections are accepted and closed by a thread, so a T6
    run (where they succeed) never fills the backlog. ``shared``: reachable by another user (a hands
    launch), so the directory is 0711 and the socket 0666; it only ever accepts and closes."""

    def __init__(self, *, shared: bool = False, view_path: str | None = None) -> None:
        # A shared one under /tmp: another user must be able to reach it, which a TMPDIR under the
        # operator's home would not allow.
        self.dir = tempfile.mkdtemp(prefix="lvc-", dir="/tmp" if shared else None)
        self.path = os.path.join(self.dir, "c")
        self.view_path = view_path or self.path
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.bind(self.path)
            self._sock.listen(16)
            if shared:
                os.chmod(self.dir, 0o711)
                os.chmod(self.path, 0o666)
        except BaseException:
            self.stop()
            raise
        self._thread = threading.Thread(target=self._serve, name="levain-canary", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                if select.select([self._sock], [], [], 0.5)[0]:
                    self._sock.accept()[0].close()
            except (OSError, ValueError):
                return

    def bind_ops(self) -> list[str]:
        """The bwrap ops that put the canary in the view (a read-write bind: a read-only mount must
        never be what refuses the connect)."""
        return ["--bind", self.path, self.view_path]

    def stop(self) -> None:
        """Close and remove it; idempotent."""
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        for p in (self.path, self.dir):
            try:
                (os.unlink if p == self.path else os.rmdir)(p)
            except OSError:
                pass


__all__ = ["STUB", "DEFAULT_GRANTS", "TIER_CLAIMS", "Canary", "judge", "plan", "running_abi", "stub_argv",
           "tier_for", "tier_line"]
