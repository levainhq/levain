"""levain.hands_live — whether anything runs as a hands user, the stop of all of it, and the hands lock.

One home for what ws-git and the session lifecycle check before the hands user's workspace may change, and what the
automation executor (:mod:`levain.autonomic.hands_exec`) shares with them: :func:`entity_session_live` (the
liveness check), :func:`sweep_hands_user` (the session-end stop), the per-entity hands lock (:data:`HANDS_LOCK`,
:func:`take_exclusive`), and the root-owned PATH and elevator every child here is started with (:data:`SECURE_PATH`,
:data:`SUDO`).

Held like :mod:`levain.trust_anchor`: stdlib only and nothing of ``levain``, so ``levain.autonomic`` may import it
(the leaf test's ``ALLOWED_OUTSIDE``), and 3.9-valid, since root's ``/usr/bin/python3`` imports it through
``levain.firing.hands``. Every failure to tell is a :class:`HandsLiveError`; :mod:`levain.firing.ws_git` re-raises it
as its own ``WsGitError`` with the same text. Every child here is run by :func:`run_bounded`, which the automation
executor shares.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

#: The elevator by absolute path, the ONE home: a bare ``sudo`` would run whatever the operator's PATH finds first. A
#: host whose sudo is elsewhere fails closed.
SUDO = "/usr/bin/sudo"

#: Every hands user's name matches this; anything else is refused. With :data:`WORKSPACE_ROOT`,
#: :data:`HANDS_UID_RANGE` and :func:`hands_user_name`, one home for setup (``levain.firing.hands``) and the
#: automation executor (``levain.autonomic.hands_exec``). Here, not in the autonomic package, because root's
#: route-map check imports setup without that package (``tests/test_ci_root_closure.py``).
HANDS_USER_RE = re.compile(r"^_levain_[a-z0-9]{1,12}_[0-9a-f]{6}$")

#: Where each host keeps the hands users' workspaces.
WORKSPACE_ROOT = {"darwin": Path("/Users/Shared/levain"), "linux": Path("/var/lib/levain")}

#: The ids setup allocates hands users from, top down. macOS: below 500 sorts with the system accounts, and the
#: low end is the dangerous end (macOS claims low ids for new daemons). Linux: the top of the usual
#: system-account range.
HANDS_UID_RANGE = {"darwin": range(499, 399, -1), "linux": range(999, 899, -1)}

#: Where root resolves a bare program name: root-owned directories only.
SECURE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

#: How long a caller outside the session-end sweep gives :func:`entity_session_live` before it
#: cannot tell (a refusal).
LIVENESS_TIMEOUT = 10.0

#: Per entity, in the operator's tree (out of the hands user's reach). A session of a hands entity
#: holds it SHARED for its whole life (several chat sessions may run at once); ws-git and ws-adopt
#: hold it EXCLUSIVE for their whole run. So no session starts while they work, and they never start
#: while a session is open. Busy is a refusal on both sides, never a wait without end.
HANDS_LOCK = "hands.lock"

#: After a child here is killed (or its kill refused), the wait for it to exit: at most this long.
_REAP_TIMEOUT = 0.5


def hands_user_name(entity_dir: Union[Path, str]) -> str:
    """The hands user's name for this entity: a slug of the directory name plus a hash of the
    resolved path, so two entities with the same directory name still get different users."""
    resolved = Path(entity_dir).expanduser().resolve()
    slug = re.sub(r"[^a-z0-9]", "", resolved.name.lower())[:12] or "entity"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
    name = f"_levain_{slug}_{digest}"
    assert HANDS_USER_RE.match(name), name
    return name


class HandsLiveError(RuntimeError):
    """Whether the hands user is running cannot be told; the caller refuses."""


def kill_quietly(proc: "subprocess.Popen[bytes]") -> None:
    """SIGKILL, best effort and nothing else: a refused kill (``PermissionError``, which a child running as root
    under sudo can raise) is not waited on here, so no later step depends on it."""
    try:
        proc.kill()
    except Exception:
        pass


def _close_pipes(proc: "subprocess.Popen[bytes]") -> None:
    """Close the child's three pipes, each one tried whatever closing another raised. Never raises an
    ``Exception``; an interrupt raised by a close passes through once all three were tried."""
    pending: Optional[BaseException] = None
    for f in (proc.stdin, proc.stdout, proc.stderr):
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
            except BaseException as e:
                if pending is None:
                    pending = e
    if pending is not None:
        raise pending


def reap_quietly(proc: "subprocess.Popen[bytes]", timeout: float) -> None:
    """Wait at most ``timeout`` for the child to exit, then close its pipes; the close is tried even when the wait
    is interrupted. Never raises an ``Exception``; an interrupt passes through once the pipes were closed. A child
    still running then is left to the ``Popen`` object's own cleanup."""
    try:
        try:
            proc.wait(timeout=max(0.0, timeout))
        except Exception:
            pass
    finally:
        _close_pipes(proc)


def run_bounded(argv: List[str], *, env: Dict[str, str], timeout: Optional[float], retry_timeout: bool = False,
                stdin: Optional[bytes] = None, reap_timeout: float = _REAP_TIMEOUT,
                popen: Optional[type] = None) -> "subprocess.CompletedProcess[bytes]":
    """One program run from ``/`` with a bounded wait, its output as bytes; ``retry_timeout`` runs it once more on
    a timeout, but only when the first child was seen to exit: one still running after its bounded reap (its kill
    refused) is not run beside, and the timeout is raised. Raises ``subprocess.TimeoutExpired``, ``OSError``, or
    whatever interrupted the wait.

    Not ``subprocess.run``: on a timeout (or an interrupt) it kills the child and then waits for it without a bound,
    and when the child runs as root (sudo) the kill from the operator uid can be refused, so that wait lasts as long
    as the child does (``tests/test_hands_live.py``'s unkillable-child tests). Here the kill is best effort and the wait after it is at most
    ``reap_timeout``; a child left running is the caller's to stop. The kill, the reap and the pipes' close are each
    tried whatever interrupts the one before; an interrupt passes through after them. ``popen`` is the ``Popen`` to
    start it with (default: ``subprocess.Popen``, looked up at call time)."""
    start = popen if popen is not None else subprocess.Popen
    tries = 2 if retry_timeout else 1
    for n in range(tries):
        proc = start(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/", env=env)
        try:
            out, err = proc.communicate(input=stdin, timeout=timeout)
        except BaseException as e:
            try:
                kill_quietly(proc)
            finally:
                reap_quietly(proc, reap_timeout)
            if isinstance(e, subprocess.TimeoutExpired) and n < tries - 1 and proc.returncode is not None:
                continue
            raise
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    raise AssertionError("unreachable")   # pragma: no cover


def _secure(name: str) -> str:
    """A program for root to run, resolved on :data:`SECURE_PATH` (the bare name if not found, so a
    plan built on a host without the tool still renders; running it then fails)."""
    return shutil.which(name, path=SECURE_PATH) or name


def _child_env() -> Dict[str, str]:
    """The environment of every child here: :data:`SECURE_PATH`, plus the operator's ``LANG`` and ``LC_*``
    so ``ps`` prints as it would under the operator's locale. pgrep, ps and kill need nothing else."""
    env = {"PATH": SECURE_PATH}
    env.update((k, v) for k, v in os.environ.items() if k == "LANG" or k.startswith("LC_"))
    return env


#: The per-user agents launchd starts for a uid on its own once it has run Apple code, measured on a
#: CI runner 2026-10-07 (exact paths), plus XPC services, which launchd alone starts. They live on
#: the sealed system volume, which no user can write.
_DARWIN_USER_AGENTS = frozenset({
    "/usr/sbin/cfprefsd", "/usr/sbin/distnoted", "/usr/libexec/lsd", "/usr/libexec/trustd",
    "/usr/libexec/secd", "/usr/libexec/containermanagerd", "/usr/libexec/secinitd",
    "/usr/libexec/UserEventAgent",
})
_DARWIN_XPC_SERVICE = re.compile(r"^/System/Library/[^\0]+\.xpc/Contents/MacOS/[^/]+$")


def _darwin_agent(ppid: str, path: str) -> bool:
    return ppid == "1" and (path in _DARWIN_USER_AGENTS or bool(_DARWIN_XPC_SERVICE.match(path)))


def proc_hides_processes(mountinfo: str) -> bool:
    """Whether ``/proc`` is mounted with ``hidepid`` (other users' processes invisible), from
    ``/proc/self/mountinfo`` text."""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/proc" and re.search(r"hidepid=(?!0\b|off\b)", line):
            return True
    return False


def entity_session_live(hands_uid: int, *, timeout: Optional[float] = None) -> bool:
    """Whether anything runs as the hands user right now. The workspace is the hands user's and the
    operator can only read it, so whatever can change it while ws-git works (an entity session's
    bash, anything it left running) runs as that user: this is the session's liveness measured where
    it cannot be missed, an orphaned background job included. Raises when it cannot tell (a process
    table the operator cannot see all of), so callers fail closed. It is checked once, when a command
    starts; a session started while the command runs is not seen. With ``timeout``, the whole check
    takes at most that long, and one that runs out cannot tell (raises)."""
    deadline = None if timeout is None else time.monotonic() + timeout

    def run(argv: List[str]) -> "subprocess.CompletedProcess[str]":
        # Any failure here, an output that does not decode included, cannot tell: the whole check fails closed
        # as a HandsLiveError, never as another type. stdout is decoded as UTF-8, strictly; stderr only feeds a
        # refusal's text.
        left = None if deadline is None else deadline - time.monotonic()
        try:
            if left is not None and left <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
            r = run_bounded(argv, env=_child_env(), timeout=left)
            return subprocess.CompletedProcess(argv, r.returncode, r.stdout.decode("utf-8"),
                                               r.stderr.decode("utf-8", "replace"))
        except Exception as exc:
            raise HandsLiveError(f"cannot tell whether the entity is running ({argv[0]}: {exc}); refusing") from None

    if platform.system() == "Linux":
        try:
            mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except Exception as exc:
            raise HandsLiveError(f"cannot tell whether the entity is running ({exc}); refusing") from None
        if proc_hides_processes(mounts):
            raise HandsLiveError("cannot tell whether the entity is running (/proc hides other users' "
                                 "processes); refusing")

    def pids() -> Set[str]:
        r = run([_secure("pgrep"), "-U", str(hands_uid)])
        if r.returncode not in (0, 1):
            raise HandsLiveError(f"cannot tell whether the entity is running (pgrep: {r.stderr.strip() or r.returncode}); "
                                 "refusing")
        return set(r.stdout.split())

    found = pids()
    if not found:
        return False
    if platform.system() != "Darwin":
        return True
    # macOS starts per-user system agents for any uid that has run Apple code (git and python3 in
    # /usr/bin are xcrun shims). Those, and only those, are not a session; an orphan of the
    # entity's is. The pid set is read again after ps: a process that exited and forked in between
    # would otherwise leave only agents in view.
    ps = run(["/bin/ps", "-o", "pid=,ppid=,comm=", "-p", ",".join(sorted(found))])
    rows = {}
    for ln in ps.stdout.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3:
            rows[parts[0]] = (parts[1], parts[2].strip())
    if not all(_darwin_agent(ppid, path) for ppid, path in rows.values()):
        return True
    if set(rows) != found or pids() != found:
        raise HandsLiveError("cannot tell whether the entity is running (its processes changed while being read); "
                             "refusing")
    return False


def _open_lock(entity_dir: Union[Path, str]) -> int:
    return os.open(Path(entity_dir) / ".levain" / HANDS_LOCK,
                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)


def take_exclusive(entity_dir: Union[Path, str]) -> Optional[int]:
    """The hands lock, exclusive, at once: its fd (the caller closes it to release), or None while a
    session (or another exclusive holder) has it. A lock file that cannot be opened raises its ``OSError``."""
    fd = _open_lock(entity_dir)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def sweep_hands_user(user: str, *, uid: Optional[int], timeout: float = 5.0) -> Optional[str]:
    """Stop every process of the hands user ``user`` (SIGKILL to all it may signal, sent as it), and
    verify none of the entity's is left. For a session's start and end: a command can leave its
    process group (``setsid``), so killing the groups levain started does not reach everything the
    entity left running. Safe only while one session owns the user
    (:func:`levain.firing.ws_git.hold_hands_session`). None when none is left, else what is.

    The check is :func:`entity_session_live`, the one ws-git uses: on macOS
    launchd starts (and restarts) per-user system agents for a uid that ran Apple code, and those
    are not the entity's (S2 L3 r3, codex HIGH). Root and levain's own account are refused. ``uid`` is
    the hands user's id as setup recorded it: no name lookup runs here, since one can block in NSS
    with no bound (S2 L3 r5, codex MED); None (not known) is refused."""
    deadline = time.monotonic() + timeout
    if uid is None:
        return f"the id of {user} is not known, so its processes cannot be checked"
    if uid in (0, os.getuid()):
        return f"refusing to stop every process of {user} (uid {uid}): it is root or levain's own account"
    said: Optional[str] = None
    while True:
        # Every step, the check's pgrep and ps included, is bounded by what is left of `timeout`, with
        # no floor, so a stalled one cannot stretch the sweep (S2d codex MED, r3, r4). A child killed at the
        # deadline adds its bounded reap (_REAP_TIMEOUT, run_bounded), and then the sweep returns.
        left = deadline - time.monotonic()
        if left > 0:
            try:
                run_bounded([SUDO, "-n", "-u", user, "/bin/kill", "-9", "--", "-1"], env=_child_env(), timeout=left)
            except (OSError, subprocess.TimeoutExpired):
                pass
        left = deadline - time.monotonic()
        if left <= 0:   # what the last check found, or that none could run (S2 L3 r5)
            return said or (f"the stop of {user}'s processes ran out of its {timeout:g} s before it "
                            "could check that none is left")
        try:
            if not entity_session_live(uid, timeout=left):
                return None
            said = f"processes of {user} are still running"
        except HandsLiveError as exc:
            said = str(exc)
        left = deadline - time.monotonic()
        if left <= 0:
            return said
        time.sleep(min(0.2, left))
