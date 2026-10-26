"""levain.autonomic.hands_exec — the executor that writes an automation's document as the entity's hands user.

A binding's effect may run only as the entity's separate hands user, never in the operator's process
(:class:`levain.autonomic.executor.Executor`). :class:`HandsExecutor` is that executor for two actions,
``write_document`` and ``undo_document``. It stores two facts, the entity directory and the hands user's name;
everything else is derived again at each use, from the OS, never from the executor's word:

- :meth:`HandsExecutor.static_problem`: what the operator's uid can decide by itself (the user's name, uid range
  and groups, the workspace, the root-owned programs, the interpreter). It starts no process as the hands user.
- the private plan: ``static_problem`` plus two probes run AS the hands user (what it may reach, and that the
  chosen interpreter runs as it). Only :func:`hands_reserved` calls it, under the entity's hands lock.
- :func:`hands_reserved`: the hands lock (exclusive, at once), the plan, then the liveness check, in that order;
  a :class:`Reservation` that is ready, or says why not. Every hands-uid process this module starts (the two
  probes, the write, the undo move, the stop after the child) starts inside one.
- :meth:`HandsExecutor.execute_reserved`: the effect, run on the plan the reservation holds. ``ok`` only when the
  child exited 0, every process of the hands user was then stopped and shown gone, and the disk shows the one
  expected result; anything else after the child started raises :class:`OutcomeUnknown`. A definite non-start
  raises :class:`NotStarted`. :meth:`HandsExecutor.execute` (the protocol method) starts nothing.

The write lands in the hands user's workspace (``WORKSPACE_ROOT[host]/<user>/workspace/<target>/<name>``), never in
the operator's tree, and never over an existing name. The undo moves it to ``<workspace>/.levain-undone/<name>``
only while its bytes are the recorded ones.

Linux has no start program in front of the child (confinement's ``_HANDS_START``: a new session keyring, ended with
levain): the child is Levain's own fixed ``-I -S`` script, which uses no keyring, and every process of the hands user
is stopped after it on every path (autogrant r2.2). Stdlib, :mod:`levain.trust_anchor` and :mod:`levain.hands_live`
only (the package's leaf rule), and 3.9-valid: both scripts run under the developer tools' Python 3.9 (macOS),
and ``tests/test_hands_exec.py`` imports the module under root's ``/usr/bin/python3``.
"""
from __future__ import annotations

import errno
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict, Iterator, List, Optional, Tuple, Union

from levain.autonomic.executor import ExecutionResult, OutcomeUnknown
from levain.autonomic.journal import EffectNotStarted
from levain.hands_live import (
    HANDS_UID_RANGE,
    HANDS_USER_RE,
    LIVENESS_TIMEOUT,
    SUDO,
    WORKSPACE_ROOT,
    HandsLiveError,
    entity_session_live,
    hands_user_name,
    kill_quietly,
    reap_quietly,
    run_bounded,
    sweep_hands_user,
    take_exclusive,
)
from levain.trust_anchor import trust_anchor_fault

__all__ = [
    "HANDS_USER_RE", "HANDS_UID_RANGE", "WORKSPACE_ROOT", "MAX_DOCUMENT_BYTES", "UNDONE_DIR",
    "HandsExecutor", "HandsPlan", "NotStarted", "Reservation", "acl_problem", "hands_reserved", "hands_user_name",
    "remedy",
]

#: The largest document a fire writes (the body arrives on the child's stdin; the child refuses past it).
MAX_DOCUMENT_BYTES = 256 * 1024

#: Where ``undo_document`` moves a document, inside the workspace.
UNDONE_DIR = ".levain-undone"

#: The write's temporary file is ``.<name>`` + this + a token the parent chose, beside the document.
_TMP_MARK = ".levain-write-"

#: The developer tools' fixed location on macOS; C1, the first interpreter candidate, lives under it. A module
#: constant so a test can point it at a scratch tree.
_CLT_ROOT = "/Library/Developer/CommandLineTools"
_C1_TAIL = "/usr/bin/python3"
#: The one directory the interpreter rule may accept group-writable, and only for the C2 candidate (r5.3).
_APPLICATIONS = "/Applications"

#: Root-owned programs this module runs, each checked as a trust anchor before it is used.
_SH = "/bin/sh"
_ENV = "/usr/bin/env"
_LS = "/bin/ls"
_XCODE_SELECT = "/usr/bin/xcode-select"
#: The PATH of every child: root's directories, and nothing a child here looks up by name.
_PATH = "/usr/bin:/bin"

#: Each probe and each operator-side program call: at most this long.
PROBE_TIMEOUT = 5.0
#: After a child is killed (or its kill refused), the wait for it to exit: at most this long.
_REAP_TIMEOUT = PROBE_TIMEOUT
#: The child refuses to link its document past this many seconds from its start; the parent waits this plus
#: ``_CHILD_GRACE_S`` for it to exit.
CHILD_DEADLINE_S = 30
_CHILD_GRACE_S = 15

#: What the developer tools fix line says, wherever a refusal or the doctor names the interpreter.
CLT_REMEDY = ("remove /Library/Developer/CommandLineTools or reinstall the Command Line Tools "
              "(xcode-select --install)")
NO_TOOLS_REMEDY = "install them with xcode-select --install"



def remedy(why: str) -> Optional[str]:
    """The fix line a refusal or the doctor prints beside an interpreter refusal, or ``None`` when it has none."""
    if why.startswith("python_unproven:c1:"):
        return CLT_REMEDY
    if why.startswith("python_unproven:no_developer_tools:"):
        return NO_TOOLS_REMEDY
    return None


_ACTIONS = ("write_document", "undo_document")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# the child's exit codes, by name, for the UNKNOWN detail (both scripts use the same numbers)
_EXIT_NAMES = {3: "not_hands", 4: "not_owned", 5: "too_large", 6: "exists", 7: "deadline", 8: "changed",
               9: "absent"}


class NotStarted(EffectNotStarted):
    """No hands-uid process was started for this effect: it may be delivered again. ``str(e)`` is why
    (``no_reservation``, ``reservation_closed``, ``permanent:<why>``, ``spawn_failed:<why>``)."""


def _host() -> Optional[str]:
    return {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno)) if e.errno is not None else type(e).__name__


def _chain(real: str) -> List[str]:
    """``real`` and every directory above it, to ``/``."""
    out = [real]
    p = real
    while os.path.dirname(p) != p:
        p = os.path.dirname(p)
        out.append(p)
    return out


def _call(argv: List[str], *, env: Dict[str, str], timeout: float, retry_timeout: bool = False,
          stdin: Optional[bytes] = None) -> "subprocess.CompletedProcess[bytes]":
    """One program run with a bounded wait; ``retry_timeout`` runs it once more on a timeout. Raises
    ``subprocess.TimeoutExpired`` or ``OSError``.

    :func:`levain.hands_live.run_bounded`, with this module's ``Popen`` and :data:`_REAP_TIMEOUT`: a probe's child
    is sudo, whose kill from the operator uid can be refused, so no wait after the kill is unbounded; a child left
    running is the caller's to stop (the plan sweeps the hands user)."""
    return run_bounded(argv, env=env, timeout=timeout, retry_timeout=retry_timeout, stdin=stdin,
                       reap_timeout=_REAP_TIMEOUT, popen=subprocess.Popen)


def acl_problem(paths: List[str], *, host: str) -> Optional[str]:
    """Whether any of ``paths`` carries an ACL, from ONE ``/bin/ls`` call: ``None`` (none does), else
    ``python_unproven:acl:<path>``, ``python_unproven:acl:unreadable`` (an answer this rule cannot read, the
    closed side) or ``python_unproven:acl:timeout`` (two timeouts; only a timeout is retried).

    Darwin: ``ls -lde`` prints each ACL entry on its own line, starting with a space, under its path's entry
    line (the mode string alone is not enough there: a file with an extended attribute shows ``@``, not ``+``).
    Linux: ``ls -ld`` marks an ACL with ``+`` as the mode's 11th character. Both marks are refused on both hosts.
    The operands are sorted by ``str`` order (code-point order, which is ``LC_ALL=C``'s byte order for UTF-8), so
    the k-th entry line belongs to the k-th operand."""
    ops = sorted(set(paths))
    if any("\n" in p for p in ops):
        return "python_unproven:acl:unreadable"
    if _anchor(_LS) is not None:
        return "python_unproven:acl:unreadable"
    argv = [_LS, "-lde" if host == "darwin" else "-ld", "--", *ops]
    try:
        r = _call(argv, env={"PATH": _PATH, "LC_ALL": "C"}, timeout=PROBE_TIMEOUT, retry_timeout=True)
    except subprocess.TimeoutExpired:
        return "python_unproven:acl:timeout"
    except OSError:
        return "python_unproven:acl:unreadable"
    if r.returncode != 0:
        return "python_unproven:acl:unreadable"
    try:
        lines = r.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return "python_unproven:acl:unreadable"
    k = -1
    for ln in lines:
        if ln.startswith(" "):
            if k < 0 or not re.match(r"^ \d+: ", ln):
                return "python_unproven:acl:unreadable"
            return f"python_unproven:acl:{ops[k]}"
        k += 1
        if k >= len(ops) or not ln.endswith(" " + ops[k]):
            return "python_unproven:acl:unreadable"
        mode = ln.split(" ", 1)[0]
        if len(mode) >= 11 and mode[10] == "+":
            return f"python_unproven:acl:{ops[k]}"
    if k != len(ops) - 1:
        return "python_unproven:acl:unreadable"
    return None


def _anchor(path: str) -> Optional[str]:
    """``None`` if the root-owned program at ``path`` may be trusted (the trust-anchor rule), else its fault."""
    why, _real = trust_anchor_fault(path, what=os.path.basename(path))
    return why


def _strict(cand: str, host: str) -> Tuple[Optional[str], Optional[str]]:
    """The STRICT interpreter rule on ``cand``: ``(why, None)`` or ``(None, the resolved path)``."""
    why, real = trust_anchor_fault(cand, what="python3")
    if why is not None or real is None:
        return f"python_unproven:{why}", None
    acl = acl_problem(_chain(real), host=host)
    if acl is not None:
        return acl, None
    return None, real


def _select_python(host: str) -> Tuple[Optional[str], Optional[str], str]:
    """The interpreter the child runs: ``(why, None, "")`` or ``(None, resolved path, rule)``.

    Darwin: C1, the Command Line Tools' Python, when anything is at their root (a dangling link included: an
    entry there that does not resolve refuses, it never falls through); else C2, the selected developer
    directory's Python, under the strict rule, then under the ``/Applications`` exception. Linux:
    ``/usr/bin/python3`` resolved, strict only."""
    if host == "linux":
        why, real = _strict("/usr/bin/python3", host)
        return (why, None, "") if why else (None, real, "strict")
    try:
        os.lstat(_CLT_ROOT)
        clt_present = True
    except FileNotFoundError:
        clt_present = False
    except OSError as e:
        return f"python_unproven:c1:{_errno_name(e)}", None, ""
    if clt_present:
        c1 = _CLT_ROOT + _C1_TAIL
        try:
            os.stat(c1)
        except OSError as e:
            return f"python_unproven:c1:{_errno_name(e)}", None, ""
        why, real = _strict(c1, host)
        return (why, None, "") if why else (None, real, "strict")
    bad = _anchor(_XCODE_SELECT)
    if bad is not None:
        return f"python_unproven:no_developer_tools:{bad}", None, ""
    try:
        r = _call([_XCODE_SELECT, "-p"], env={"PATH": _PATH}, timeout=PROBE_TIMEOUT)
        out = r.stdout.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as e:
        return f"python_unproven:no_developer_tools:{type(e).__name__}", None, ""
    dev = out[:-1] if out.endswith("\n") else ""
    if r.returncode != 0 or not dev or "\n" in dev or not os.path.isabs(dev):
        return f"python_unproven:no_developer_tools:exit_{r.returncode}", None, ""
    c2 = dev + "/usr/bin/python3"
    if not os.path.exists(c2):
        return "python_unproven:no_developer_tools:absent", None, ""
    why, real = _strict(c2, host)
    if why is None:
        return None, real, "strict"
    try:
        admin = grp.getgrnam("admin").gr_gid
    except KeyError:
        return why, None, ""
    fault, real = trust_anchor_fault(c2, what="python3", group_write_exempt=(_APPLICATIONS, admin))
    if fault is not None or real is None:
        return f"python_unproven:{fault}", None, ""
    acl = acl_problem(_chain(real), host=host)
    if acl is not None:
        return acl, None, ""
    return None, real, "applications_exception"


#: Groups a hands user must not be in, by name, where they exist (their gids are read, not assumed).
_FORBIDDEN_GROUPS = {"darwin": frozenset({"staff", "admin"}), "linux": frozenset({"sudo", "wheel", "adm"})}


def _forbidden_gids(host: str) -> Tuple[int, ...]:
    """Groups a hands user must not be in: root's, the operator's primary group, and :data:`_FORBIDDEN_GROUPS`."""
    out = {0, os.getgid()}
    for name in sorted(_FORBIDDEN_GROUPS[host]):
        try:
            out.add(grp.getgrnam(name).gr_gid)
        except KeyError:
            pass
    return tuple(sorted(out))


@dataclass(frozen=True)
class HandsPlan:
    """What one reservation proved, and the exact argv prefix its effect runs: every element fixed, none
    supplied by a caller (no loader variable can enter)."""

    user: str
    uid: int
    home: str
    workspace: Path
    python: str
    python_rule: str
    prefix: Tuple[str, ...]


@dataclass(frozen=True)
class _Static:
    user: str
    uid: int
    home: str
    host: str
    workspace: Path
    python: str
    python_rule: str


# Run by /bin/sh as the hands user: what it may reach. Operands: the operator's home, the workspace, the workspaces'
# root, "--", then every component of the interpreter's chain. One builtin `test` of at most three arguments per
# check (no -a chains). On a violated check it prints "violated <which>" and exits 1; sudo -n's own refusal also
# exits 1 but prints no such line, so the parent reads the line, not the code.
_PROPERTY_SH = r"""
home=$1; ws=$2; root=$3; shift 3
[ "$1" = "--" ] || exit 2
shift
[ ! -x "$home" ] || { echo "violated home"; exit 1; }
[ -x "$ws" ] || { echo "violated workspace"; exit 1; }
[ ! -w "$root" ] || { echo "violated root"; exit 1; }
for p in "$@"; do
  [ ! -w "$p" ] || { echo "violated chain:$p"; exit 1; }
done
exit 0
"""

# Run by the chosen Python with -I -S (no site, no environment, nothing imported from the workspace) as the hands
# user. argv: the expected uid, seconds to the link deadline, the workspace, the document's name, the temporary
# file's token (the parent's, so the parent judges exactly this child's temporary file), then the target's
# components. The body arrives on stdin. Every directory is opened with O_NOFOLLOW from the one before it, so a
# link anywhere stops the write; the document is written beside its name and LINKED to it, so an existing name is
# never replaced. Exit 0 prints the new file's uid, inode and size.
_WRITE_SCRIPT = r"""
import json, os, sys, time
t0 = time.monotonic()
F = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
uid, budget, ws, name, token, parts = (int(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5],
                                       sys.argv[6:])
if os.geteuid() != uid:
    sys.exit(3)
d = os.open(ws, os.O_RDONLY | os.O_DIRECTORY | F)
if os.fstat(d).st_uid != uid:
    sys.exit(4)
for part in parts:
    try:
        nd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=d)
    except FileNotFoundError:
        os.mkdir(part, 0o755, dir_fd=d)
        os.fsync(d)
        nd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=d)
    os.close(d)
    d = nd
    if os.fstat(d).st_uid != uid:
        sys.exit(4)
tmp = "." + name + "%(tmp_mark)s" + token
w = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | F, 0o644, dir_fd=d)
try:
    got = 0
    while True:
        chunk = os.read(0, 1 << 16)
        if not chunk:
            break
        got += len(chunk)
        if got > %(max)d:
            os.close(w)
            os.unlink(tmp, dir_fd=d)
            sys.exit(5)
        while chunk:
            chunk = chunk[os.write(w, chunk):]
    os.fchmod(w, 0o644)
    os.fsync(w)
    os.close(w)
    if time.monotonic() - t0 > budget:
        os.unlink(tmp, dir_fd=d)
        sys.exit(7)
    try:
        os.link(tmp, name, src_dir_fd=d, dst_dir_fd=d, follow_symlinks=False)
    except FileExistsError:
        os.unlink(tmp, dir_fd=d)
        sys.exit(6)
    os.unlink(tmp, dir_fd=d)
    os.fsync(d)
except SystemExit:
    raise
except BaseException:
    try:
        os.unlink(tmp, dir_fd=d)
    except OSError:
        pass
    raise
r = os.open(name, os.O_RDONLY | F, dir_fd=d)
st = os.fstat(r)
print(json.dumps({"uid": st.st_uid, "ino": st.st_ino, "size": st.st_size}))
""" % {"max": MAX_DOCUMENT_BYTES, "tmp_mark": _TMP_MARK}

# Run as _WRITE_SCRIPT is. argv: the expected uid, seconds to the deadline, the workspace, the recorded sha256, the
# document's name, then the target's components. Moves <target>/<name> to <workspace>/.levain-undone/<name> (a link,
# then the unlink: an existing name there is never replaced) only while its bytes still hash to the recorded sha256.
_UNDO_SCRIPT = r"""
import hashlib, os, sys, time
t0 = time.monotonic()
F = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
uid, budget, ws, sha, name, parts = (int(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5],
                                     sys.argv[6:])
if os.geteuid() != uid:
    sys.exit(3)
w = os.open(ws, os.O_RDONLY | os.O_DIRECTORY | F)
if os.fstat(w).st_uid != uid:
    sys.exit(4)
d = os.dup(w)
for part in parts:
    try:
        nd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=d)
    except FileNotFoundError:
        sys.exit(9)
    os.close(d)
    d = nd
    if os.fstat(d).st_uid != uid:
        sys.exit(4)
try:
    r = os.open(name, os.O_RDONLY | F | os.O_NONBLOCK, dir_fd=d)
except FileNotFoundError:
    sys.exit(9)
st = os.fstat(r)
if not (st.st_mode & 0o170000 == 0o100000) or st.st_uid != uid or st.st_nlink != 1:
    sys.exit(4)
h = hashlib.sha256()
while True:
    chunk = os.read(r, 1 << 16)
    if not chunk:
        break
    h.update(chunk)
os.close(r)
if h.hexdigest() != sha:
    sys.exit(8)
try:
    u = os.open("%(undone)s", os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=w)
except FileNotFoundError:
    os.mkdir("%(undone)s", 0o755, dir_fd=w)
    os.fsync(w)
    u = os.open("%(undone)s", os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=w)
if os.fstat(u).st_uid != uid:
    sys.exit(4)
if time.monotonic() - t0 > budget:
    sys.exit(7)
try:
    os.link(name, name, src_dir_fd=d, dst_dir_fd=u, follow_symlinks=False)
except FileExistsError:
    sys.exit(6)
os.unlink(name, dir_fd=d)
os.fsync(u)
os.fsync(d)
""" % {"undone": UNDONE_DIR}


def _parse(action_name: str, payload: str) -> Optional[Dict[str, object]]:
    """The payload as its action's fields, or ``None`` when it does not read as one."""
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    keys = {"v", "hands_user", "target", "name", "sha256"} | ({"body"} if action_name == "write_document" else set())
    if set(doc) != keys or not all(isinstance(doc[k], str) for k in keys - {"v"}):
        return None
    return doc


def _target_parts(target: str) -> Optional[List[str]]:
    """A target's components (``ws_git.put_parts``'s rule: relative, no ``.``, ``..`` or empty part, no NUL), and
    never under :data:`UNDONE_DIR`; ``None`` when it breaks the rule, or when the filesystem encoding cannot carry
    it (a lone surrogate): the child's argv could never be built, so that is permanent, never a failed start."""
    parts = target.split("/")
    if not target or any(p in ("", ".", "..") or "\0" in p for p in parts) or parts[0] == UNDONE_DIR:
        return None
    try:
        os.fsencode(target)
    except UnicodeError:
        return None
    return parts


class Reservation:
    """What :func:`hands_reserved` answered: ``ready`` with the ``plan`` it proved (``None`` for an executor that is
    not a :class:`HandsExecutor`), or not ready with ``reason``. ``open`` is True while the block that took it runs.
    A plain object, not a string: no caller parses a reason to learn whether to retry."""

    __slots__ = ("executor", "ready", "reason", "plan", "open")

    def __init__(self, executor: object, *, ready: bool, reason: Optional[str] = None,
                 plan: Optional[HandsPlan] = None) -> None:
        self.executor = executor
        self.ready = ready
        self.reason = reason
        self.plan = plan
        self.open = True

    def __repr__(self) -> str:
        return f"Reservation(ready={self.ready}, reason={self.reason!r}, open={self.open})"


@contextmanager
def hands_reserved(executor: object, *, held: Optional[Reservation] = None) -> Iterator[Reservation]:
    """Reserve the entity's hands user for one effect. ``held`` (a reservation of this same executor, taken by
    the caller's caller) is yielded as it is, and nothing is taken again. For a :class:`HandsExecutor`, in order:
    (1) the hands lock, exclusive, at once (``hands_busy:lock`` while held; ``hands_unready:lock:<Name>`` when the
    lock file cannot be opened); (2) the plan (its clause when it fails); (3) whether anything runs as the hands
    user (``hands_busy:live``, ``hands_busy:cannot_tell``). The liveness check runs after the plan's probes, so a
    process a probe leaves behind is answered here, as busy. Not ready: the lock is released before the block
    runs. Ready: the lock is held until the block exits. A subclass of :class:`HandsExecutor` is not one (its
    methods are its own): ``hands_unready:executor_type``, nothing taken, nothing of it run. Any other executor:
    ready, nothing taken."""
    if held is not None and held.executor is executor:
        yield held
        return
    if isinstance(executor, HandsExecutor) and type(executor) is not HandsExecutor:
        yield _closed(Reservation(executor, ready=False, reason="hands_unready:executor_type"))
        return
    if type(executor) is not HandsExecutor:
        r = Reservation(executor, ready=True)
        try:
            yield r
        finally:
            r.open = False
        return
    try:
        fd = take_exclusive(executor.entity_dir)
    except OSError as e:
        yield _closed(Reservation(executor, ready=False, reason=f"hands_unready:lock:{type(e).__name__}"))
        return
    if fd is None:
        yield _closed(Reservation(executor, ready=False, reason="hands_busy:lock"))
        return
    held_fd: Optional[int] = fd
    try:
        try:
            plan = executor._plan()
        except Exception as e:   # a lookup that raised what no clause names: not ready, the closed side
            plan = f"hands_unready:plan:{type(e).__name__}"
        if isinstance(plan, str):
            r = Reservation(executor, ready=False, reason=plan)
        else:
            try:
                live = entity_session_live(plan.uid, timeout=LIVENESS_TIMEOUT)
            except Exception:
                live = None
            if live is None:
                r = Reservation(executor, ready=False, reason="hands_busy:cannot_tell")
            elif live:
                r = Reservation(executor, ready=False, reason="hands_busy:live")
            else:
                r = Reservation(executor, ready=True, plan=plan)
        if not r.ready:
            os.close(fd)
            held_fd = None
            yield _closed(r)
            return
        try:
            yield r
        finally:
            r.open = False
    finally:
        if held_fd is not None:
            os.close(held_fd)


def _closed(r: Reservation) -> Reservation:
    r.open = False
    return r


@dataclass(frozen=True)
class HandsExecutor:
    """The automation executor: its effects run only as the entity's hands user, through ``sudo -n -u <user>``.
    It stores the entity directory and the user's name; every other fact is derived at each use. ``__slots__``
    leaves no instance ``__dict__``, so no method can be shadowed on an instance; a subclass is another type, which an
    exact-type check (``type(x) is HandsExecutor``) refuses."""

    __slots__ = ("entity_dir", "user")
    entity_dir: Path
    user: str
    name: ClassVar[str] = "hands"

    # --- the proof -------------------------------------------------------------------------------------------

    def static_problem(self) -> Optional[str]:
        """The first clause the operator's uid can find false, by name, or ``None``. Starts no hands-uid process."""
        s = self._static()
        return s if isinstance(s, str) else None

    def _static(self) -> Union[_Static, str]:
        host = _host()
        if host is None:
            return "host_unsupported"
        user = self.user
        if not isinstance(user, str) or not HANDS_USER_RE.fullmatch(user):
            return "hands_user_unproven:name"
        try:
            if user != hands_user_name(self.entity_dir):
                return "hands_user_unproven:not_this_entity"
        except (OSError, RuntimeError):
            return "hands_user_unproven:entity_dir"
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return "hands_user_unproven:no_user"
        uid = pw.pw_uid
        if uid in {0, os.getuid(), os.geteuid()}:
            return "hands_user_unproven:uid"
        if uid not in HANDS_UID_RANGE[host]:
            return "hands_user_unproven:uid_range"
        try:
            groups = set(os.getgrouplist(user, pw.pw_gid))
        except OSError:
            return "hands_user_unproven:groups"
        groups.add(pw.pw_gid)
        bad = groups.intersection(_forbidden_gids(host))
        if bad:
            return f"hands_user_unproven:group:{min(bad)}"
        ws = WORKSPACE_ROOT[host] / user / "workspace"
        try:
            st = os.lstat(ws)
        except OSError:
            return "workspace_unproven"
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            return "workspace_unproven"
        if _anchor(SUDO) is not None:
            return "sudo_untrusted"
        for prog in (_SH, _ENV, _LS):
            why = _anchor(prog)
            if why is not None:
                return f"anchor_untrusted:{why}"
        why, python, rule = _select_python(host)
        if why is not None or python is None:
            return why or "python_unproven"
        return _Static(user=user, uid=uid, home=pw.pw_dir, host=host, workspace=ws, python=python,
                       python_rule=rule)

    def _plan(self) -> Union[HandsPlan, str]:
        """``static_problem``, then the two probes as the hands user. Called by :func:`hands_reserved` alone, with
        the hands lock held."""
        s = self._static()
        if isinstance(s, str):
            return s
        prefix = (SUDO, "-n", "-u", s.user, _ENV, "-i", f"HOME={s.home}", f"PATH={_PATH}", "LANG=C.UTF-8",
                  "TERM=dumb", f"USER={s.user}", f"LOGNAME={s.user}", s.python)
        try:
            operator_home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            return "hands_user_unproven:operator_home"
        prop = [SUDO, "-n", "-u", s.user, _ENV, "-i", f"PATH={_PATH}", _SH, "-c", _PROPERTY_SH, "levain-property",
                operator_home, str(s.workspace), str(WORKSPACE_ROOT[s.host]), "--", *_chain(s.python)]
        try:
            r = _call(prop, env={"PATH": _PATH}, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _after_probe_timeout(s, "sudo_refused")
        except OSError:
            return "sudo_refused"
        if r.returncode != 0:
            said = r.stdout.decode("utf-8", "replace").strip()
            if r.returncode == 1 and said.startswith("violated "):
                return f"hands_user_unproven:property:{said[len('violated '):]}"
            return "sudo_refused"
        probe = [*prefix, "-I", "-S", "-c", f"import os, sys; sys.exit(0 if os.geteuid() == {s.uid} else 3)"]
        try:
            r = _call(probe, env={"PATH": _PATH}, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _after_probe_timeout(s, "python_unproven:probe_timeout")
        except OSError:
            return "sudo_refused"
        if r.returncode != 0:
            return f"python_unproven:probe_exit_{r.returncode}"
        return HandsPlan(user=s.user, uid=s.uid, home=s.home, workspace=s.workspace, python=s.python,
                         python_rule=s.python_rule, prefix=prefix)

    # --- the protocol ----------------------------------------------------------------------------------------

    def preflight(self, action_name: str, payload: str, *, context_id: str, journaled: bool) -> Optional[str]:
        """The permanent reason this action can never run here, or ``None``. Pure: no I/O. ``unknown_action``
        (not one of the two actions), ``wrong_entity`` (the payload names another hands user),
        ``payload_mismatch`` (the payload does not read, its name is not ``<context_id>.md``, its target breaks
        the target rule, or the body is not the one its sha256 names or is over :data:`MAX_DOCUMENT_BYTES`),
        ``not_journaled`` (``write_document`` off the journal, or ``undo_document`` on it)."""
        if action_name not in _ACTIONS:
            return "unknown_action"
        doc = _parse(action_name, payload)
        if doc is None:
            return "payload_mismatch"
        if doc["hands_user"] != self.user:
            return "wrong_entity"
        name, target, sha = str(doc["name"]), str(doc["target"]), str(doc["sha256"])
        if (not isinstance(context_id, str) or name != context_id + ".md" or not _NAME_RE.fullmatch(name)
                or _target_parts(target) is None or not _SHA_RE.fullmatch(sha)):
            return "payload_mismatch"
        if action_name == "write_document":
            body = str(doc["body"]).encode("utf-8", "surrogatepass")
            if len(body) > MAX_DOCUMENT_BYTES or hashlib.sha256(body).hexdigest() != sha:
                return "payload_mismatch"
        if journaled != (action_name == "write_document"):
            return "not_journaled"
        return None

    def last_check(self, reservation: Optional[Reservation]) -> Optional[str]:
        """The last look before an effect: ``"live"`` (something runs as the hands user), ``"cannot_tell"``, or
        ``None``. Never raises."""
        try:
            plan = reservation.plan if reservation is not None else None
            if type(self) is not HandsExecutor or type(plan) is not HandsPlan:
                return "cannot_tell"
            return "live" if entity_session_live(plan.uid, timeout=LIVENESS_TIMEOUT) else None
        except Exception:
            return "cannot_tell"

    def execute(self, action_name: str, payload: str, *, context_id: str) -> ExecutionResult:
        """Starts nothing: a hands effect runs only through :meth:`execute_reserved`."""
        return ExecutionResult(ok=False, error="not_reserved", started=False)

    def execute_reserved(self, action_name: str, payload: str, *, context_id: str,
                         reservation: Optional[Reservation], journaled: bool) -> ExecutionResult:
        """Run the effect on the plan ``reservation`` holds. Between the caller's intent and the child only
        non-blocking work runs: attribute checks, :meth:`preflight`, the argv, the child's start. ``ok`` only
        when the child exited 0, the hands user was then shown quiescent, and the disk shows the one expected
        result. Raises :class:`NotStarted` when no child was started, :class:`OutcomeUnknown` for everything
        else that is not ``ok``, except that an interrupt (a ``BaseException`` that is not an ``Exception``) while
        the child runs passes through once the child is stopped, the hands user swept and the child reaped (the
        outcome is just as unknown). A subclass holds no reservation (``no_reservation``)."""
        if (type(self) is not HandsExecutor or reservation is None or reservation.executor is not self
                or not reservation.ready or type(reservation.plan) is not HandsPlan):
            raise NotStarted("no_reservation")
        if not reservation.open:
            raise NotStarted("reservation_closed")
        why = self.preflight(action_name, payload, context_id=context_id, journaled=journaled)
        if why is not None:
            raise NotStarted(f"permanent:{why}")
        plan = reservation.plan
        doc = _parse(action_name, payload)
        assert doc is not None   # preflight read it
        name, target, sha = str(doc["name"]), str(doc["target"]), str(doc["sha256"])
        parts = _target_parts(target) or []
        tmp = ""
        if action_name == "write_document":
            body: Optional[bytes] = str(doc["body"]).encode("utf-8", "surrogatepass")
            token = os.urandom(8).hex()
            tmp = "." + name + _TMP_MARK + token
            argv = [*plan.prefix, "-I", "-S", "-c", _WRITE_SCRIPT, str(plan.uid), str(CHILD_DEADLINE_S),
                    str(plan.workspace), name, token, *parts]
        else:
            body = None
            argv = [*plan.prefix, "-I", "-S", "-c", _UNDO_SCRIPT, str(plan.uid), str(CHILD_DEADLINE_S),
                    str(plan.workspace), sha, name, *parts]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if body is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/", env={"PATH": _PATH},
                                    start_new_session=True)
        except Exception as e:
            raise NotStarted(f"spawn_failed:{_spawn_why(e)}") from e
        # From here a child exists. Whatever ends the wait, an interrupt included: the child is sent a kill (best
        # effort: it is sudo, root, which the operator uid may not signal), the hands user is swept (what runs
        # under sudo is the hands user's), then a bounded wait reaps the child and its pipes are closed.
        swept: Optional[str]
        try:
            try:
                child = _wait(proc, body)
            except BaseException:
                _kill(proc)
                raise
        finally:
            try:
                swept = _sweep(plan)
            finally:
                _reap(proc)
        if swept is not None:
            raise OutcomeUnknown(f"{child}:{swept}")
        if child != "exit_0":
            raise OutcomeUnknown(f"{child}:not_read")
        if action_name == "write_document":
            assert body is not None
            disk, stale = _read_written(plan, parts, name, body, tmp)
            if disk != "ok":
                raise OutcomeUnknown(f"{child}:{disk}")
            rel = "/".join([*parts, name])
            detail = str(plan.workspace / rel)
            if stale:
                detail += f" (left by an earlier run: {', '.join(stale)})"
            return ExecutionResult(ok=True, detail=detail, downstream_id=f"{plan.user}:{rel}")
        disk = _read_undone(plan, parts, name, sha)
        if disk != "ok":
            raise OutcomeUnknown(f"{child}:{disk}")
        rel = f"{UNDONE_DIR}/{name}"
        return ExecutionResult(ok=True, detail=str(plan.workspace / rel), downstream_id=f"{plan.user}:{rel}")


def _after_probe_timeout(s: _Static, why: str) -> str:
    """``why``, after the hands user is swept: a probe's timeout stops sudo at most, and what sudo started as the
    hands user would otherwise outlive it and hold every later reservation at ``hands_busy:live``. The plan runs
    under the exclusive hands lock, so no session owns the user while it is swept. The answer is a refusal either
    way, so a sweep that fails or raises changes nothing here; the next reservation's liveness check reads what
    is left."""
    try:
        sweep_hands_user(s.user, uid=s.uid)
    except Exception:
        pass
    return why


def _spawn_why(e: BaseException) -> str:
    """Why the child's start failed: an ``OSError``'s errno number, else the exception's type name."""
    if isinstance(e, OSError) and e.errno is not None:
        return str(e.errno)
    return type(e).__name__


def _sweep(plan: HandsPlan) -> Optional[str]:
    """After the child: ``None`` when every process of the hands user was stopped and shown gone, else
    ``not_quiescent``, or ``sweep:<Type>`` when the sweep raised (it is not the child's answer, so it is never
    let out as one)."""
    try:
        left = sweep_hands_user(plan.user, uid=plan.uid)
    except Exception as e:
        return f"sweep:{type(e).__name__}"
    return None if left is None else "not_quiescent"


def _wait(proc: "subprocess.Popen[bytes]", body: Optional[bytes]) -> str:
    """The child's end, as a short name: ``exit_<n>`` (or the code's name), ``signal_<n>``, ``timeout``, or
    ``error:<Type>``. A child that outlives its wait is sent a kill; the caller reaps it."""
    try:
        proc.communicate(input=body, timeout=CHILD_DEADLINE_S + _CHILD_GRACE_S)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return "timeout"
    except Exception as e:
        _kill(proc)
        return f"error:{type(e).__name__}"
    code = proc.returncode
    if code is None:
        return "error:no_returncode"
    if code < 0:
        return f"signal_{-code}"
    return "exit_0" if code == 0 else f"exit_{code}:{_EXIT_NAMES.get(code, 'other')}"


_kill = kill_quietly


def _reap(proc: "subprocess.Popen[bytes]") -> None:
    """:func:`levain.hands_live.reap_quietly` with :data:`_REAP_TIMEOUT`."""
    reap_quietly(proc, _REAP_TIMEOUT)


_RD = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_dir(plan: HandsPlan, parts: List[str]) -> int:
    """The target directory, walked from the workspace by fds, every component O_NOFOLLOW and owned by the
    hands uid (``OSError`` or ``PermissionError`` when not)."""
    d = os.open(str(plan.workspace), _RD | os.O_DIRECTORY)
    try:
        if os.fstat(d).st_uid != plan.uid:
            raise PermissionError("workspace not the hands user's")
        for part in parts:
            nd = os.open(part, _RD | os.O_DIRECTORY, dir_fd=d)
            os.close(d)
            d = nd
            if os.fstat(d).st_uid != plan.uid:
                raise PermissionError(f"{part} not the hands user's")
        return d
    except BaseException:
        os.close(d)
        raise


def _leaf_is(d: int, name: str, plan: HandsPlan, *, size: Optional[int], sha: str) -> str:
    """``ok`` when ``name`` in ``d`` is a regular file of the hands uid with one link whose bytes hash to
    ``sha`` (and, with ``size``, are exactly that many), else what it is."""
    try:
        f = os.open(name, _RD | os.O_NONBLOCK, dir_fd=d)
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        return f"unreadable:{_errno_name(e)}"
    try:
        st = os.fstat(f)
        if not stat.S_ISREG(st.st_mode):
            return "not_regular"
        if st.st_uid != plan.uid:
            return "other_owner"
        if st.st_nlink != 1:
            return "linked"
        if size is not None and st.st_size != size:
            return "other_bytes"
        h = hashlib.sha256()
        left = st.st_size
        while left > 0:
            chunk = os.read(f, min(left, 1 << 16))
            if not chunk:
                return "other_bytes"
            h.update(chunk)
            left -= len(chunk)
        if os.read(f, 1):
            return "other_bytes"
        return "ok" if h.hexdigest() == sha else "other_bytes"
    finally:
        os.close(f)


def _read_written(plan: HandsPlan, parts: List[str], name: str, body: bytes, tmp: str) -> Tuple[str, List[str]]:
    """What the disk shows after a write: ``ok`` only for the one expected file with THIS child's temporary file
    (``tmp``, the name the parent gave it) gone. Beside the answer, the temporary files of the same document an
    earlier run left (a crash, a killed child): reported, never read as this child's."""
    try:
        d = _open_dir(plan, parts)
    except OSError as e:
        return f"dir:{_errno_name(e)}", []
    try:
        seen = _leaf_is(d, name, plan, size=len(body), sha=hashlib.sha256(body).hexdigest())
        if seen != "ok":
            return seen, []
        try:
            os.stat(tmp, dir_fd=d, follow_symlinks=False)
            return "tmp_left", []
        except FileNotFoundError:
            pass
        return "ok", sorted(e for e in os.listdir(d) if e.startswith("." + name + _TMP_MARK))
    except OSError as e:
        return f"unreadable:{_errno_name(e)}", []
    finally:
        os.close(d)


def _read_undone(plan: HandsPlan, parts: List[str], name: str, sha: str) -> str:
    """What the disk shows after an undo: ``ok`` only when the name is gone from the target and the moved file
    has the recorded bytes."""
    try:
        d = _open_dir(plan, parts)
    except OSError as e:
        return f"dir:{_errno_name(e)}"
    try:
        try:
            os.stat(name, dir_fd=d, follow_symlinks=False)
            return "still_there"
        except FileNotFoundError:
            pass
    except OSError as e:
        return f"unreadable:{_errno_name(e)}"
    finally:
        os.close(d)
    try:
        u = _open_dir(plan, [UNDONE_DIR])
    except OSError as e:
        return f"undone_dir:{_errno_name(e)}"
    try:
        return _leaf_is(u, name, plan, size=None, sha=sha)
    finally:
        os.close(u)
