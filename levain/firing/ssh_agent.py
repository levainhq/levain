"""levain.firing.ssh_agent — the scoped ssh agent bash uses.

WHY ONE OF OUR OWN. bash never gets the operator's ``$SSH_AUTH_SOCK``: that agent holds every key, for
every host, for as long as it runs. Each entity session gets its own ``ssh-agent`` instead, started by
levain OUTSIDE the sandbox (each confined command runs in a fresh pid namespace, so nothing inside one
outlives a command), holding only the keys the operator adds from their own shell with
``levain ssh-add``, each limited to the hosts named with ``-h`` and to a lifetime with ``-t``.

THE PROCESS. ``ssh-agent -D -a <socket> -P '!*' -t <secs>``:
  - ``-D`` foreground: the agent does not fork, so the pid levain's Popen holds is the agent's pid,
    the one :func:`peer_pid` must read back from the socket (``-d``, debug, stays off);
  - ``-P '!*'``: the provider allow-list matches no path, so ``ssh-add -s`` cannot make the agent load
    a library (RUN on this Mac 2026-10-14, OpenSSH 10.3: "agent refused operation");
  - ``-t``: the lifetime of a key added without one (``levain ssh-add`` always passes its own).

THE SOCKET. ``<levain home>/run/<session id>/agent.sock``: the session directory is made 0700 by this
module, exclusively, and every directory above it must be root's or the operator's and writable by no
one else (the rule :func:`~levain.firing.confinement.resolve_grant` holds a grant's parents to).
:func:`~levain.firing.confinement.build_policy` hides ``<levain home>/run`` from every confined process,
read and write, so bash can neither reach the host path nor rename anything on it; on Linux the socket
is bound into bash's view at ``<runtime dir>/levain-ssh/agent.sock`` and ``SSH_AUTH_SOCK`` names that.

THE RECORD. ``agent.json`` beside the socket: the agent's pid, the levain process that owns it, and
each key ``levain ssh-add`` added (fingerprint, hosts, lifetime, when). The agent protocol does not
report a key's constraints back, so the status lines cross this record with ``ssh-add -l``.

A levain that is killed leaves its agent running until the next levain start sweeps it
(:func:`sweep_orphans`): it kills a recorded agent only when the socket's peer pid is the recorded pid.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import os
import platform
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.launch import child_env

RUN_DIR = "run"
SOCKET_NAME = "agent.sock"
RECORD_NAME = "agent.json"
KNOWN_HOSTS_NAME = "known_hosts"
VIEW_DIR = "levain-ssh"
NO_PROVIDER = "!*"
MIN_OPENSSH = (8, 9)   # destination constraints (ssh-add -h) arrived in OpenSSH 8.9
DEFAULT_LIFETIME = 8 * 3600
MAX_LIFETIME = 24 * 3600
_SUN_PATH_MAX = 103   # macOS sun_path is 104 bytes with its NUL; Linux's 108 is the looser
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_START_WAIT = 5.0
_STOP_WAIT = 3.0
# Fixed policy oracle: never derived from HOME, LEVAIN_HOME, cwd, or entity-controlled state.
# The path must already exist: on Darwin sandbox_check reports a write to any nonexistent pathname
# as denied even outside a sandbox.  This root-owned system directory is stable and not a useful
# write target; every Levain macOS profile additionally denies the literal directory itself.
MACOS_CONFINEMENT_SENTINEL = "/private/var/empty"


class AgentError(RuntimeError):
    """The scoped agent cannot be started, found or used; the text says why, for the operator."""


class AgentTimeout(AgentError):
    """A connect to a socket was not accepted: it did not complete in time, or the kernel said the
    listener's backlog is full (EAGAIN, measured on Linux 7.0 and 7.2 for a timed connect). Who listens
    is unknown, which is not the same answer as nobody listening."""


@dataclass(frozen=True)
class ScopedAgent:
    """One session's agent: its id (the directory name under ``<levain home>/run``), its socket on the
    host and the pid levain started. Carried in the conversation binding as data."""

    session_id: str
    socket: str
    pid: int

    @property
    def dir(self) -> Path:
        return Path(self.socket).parent

    def to_params(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "socket": self.socket, "pid": self.pid}

    @classmethod
    def from_params(cls, data: Any) -> "ScopedAgent":
        if not (isinstance(data, dict) and set(data) == {"session_id", "socket", "pid"}
                and isinstance(data["session_id"], str) and _SESSION_ID_RE.match(data["session_id"])
                and isinstance(data["socket"], str) and os.path.isabs(data["socket"])
                and Path(data["socket"]).name == SOCKET_NAME
                and Path(data["socket"]).parent.name == data["session_id"]
                and Path(data["socket"]).parent.parent == run_root()
                and isinstance(data["pid"], int) and not isinstance(data["pid"], bool) and data["pid"] > 1):
            raise AgentError("not a serialized scoped ssh agent (fail-closed).")
        return cls(data["session_id"], data["socket"], data["pid"])


def levain_home() -> Path:
    """``$LEVAIN_HOME``, else ``~/.levain`` (the home :func:`~levain.firing.confinement.build_policy`
    reads)."""
    return Path(os.environ.get("LEVAIN_HOME") or Path.home() / ".levain").expanduser()


def run_root() -> Path:
    """``<levain home>/run``, absolute (hidden from every confined process by ``build_policy``)."""
    return (levain_home() / RUN_DIR).resolve()


def view_socket(runtime_root: Path) -> Path:
    """Where bash finds the agent inside its view of the user runtime dir ``runtime_root``."""
    return runtime_root / VIEW_DIR / SOCKET_NAME


def view_known_hosts(runtime_root: Path) -> Path:
    """Where bash's ssh finds the session's own known_hosts (:data:`KNOWN_HOSTS_NAME`), beside the agent."""
    return runtime_root / VIEW_DIR / KNOWN_HOSTS_NAME


def _tool_env(sock: str | None = None) -> dict[str, str]:
    """The environment for an OpenSSH tool levain runs: :func:`~levain.launch.child_env` (the askpass
    variables carried, for a passphrase prompt), never the operator's ``$SSH_AUTH_SOCK``; ``sock`` instead
    when the tool talks to a scoped agent."""
    env = child_env("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")
    env.pop("SSH_AUTH_SOCK", None)
    if sock is not None:
        env["SSH_AUTH_SOCK"] = sock
    return env


# --- OpenSSH --------------------------------------------------------------------------------------

def openssh_version(ssh: str | None = None) -> tuple[int, int] | None:
    """``(major, minor)`` from ``ssh -V`` (it prints to stderr: ``OpenSSH_10.3p1, LibreSSL ...``), or
    None when there is no ssh or it is not OpenSSH."""
    exe = ssh or shutil.which("ssh")
    if exe is None:
        return None
    try:
        done = subprocess.run([exe, "-V"], capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_tool_env(),
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"OpenSSH_(\d+)\.(\d+)", done.stderr + done.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def openssh_problem() -> str | None:
    """Why levain starts no scoped agent on this host, or None: ``ssh-agent`` and ``ssh-add`` must be
    present and OpenSSH at least 8.9 (destination constraints)."""
    missing = [t for t in ("ssh", "ssh-agent", "ssh-add", "ssh-keygen") if shutil.which(t) is None]
    if missing:
        return f"{', '.join(missing)} not found"
    v = openssh_version()
    if v is None:
        return "ssh is not OpenSSH (ssh -V named no OpenSSH version)"
    if v < MIN_OPENSSH:
        return (f"OpenSSH {v[0]}.{v[1]} has no destination constraints (ssh-add -h needs "
                f"{MIN_OPENSSH[0]}.{MIN_OPENSSH[1]} or later)")
    return None


# --- the socket's peer ----------------------------------------------------------------------------

def peer_pid(sock: str, *, timeout: float = 5.0) -> int:
    """The pid of the process listening on ``sock``, as the kernel reports it for a connection to it:
    ``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on macOS. :class:`AgentError` when it cannot be read,
    the connect taking longer than ``timeout`` seconds included."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sock)
        if hasattr(socket, "SO_PEERCRED"):
            pid, _uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                               struct.calcsize("3i")))
        elif platform.system() == "Darwin":
            # SOL_LOCAL = 0, LOCAL_PEERPID = 2 (<sys/un.h>); Python names neither.
            pid = struct.unpack("i", s.getsockopt(0, 2, struct.calcsize("i")))[0]
        else:
            raise AgentError(f"this platform ({platform.system()}) has no peer-pid read levain knows")
    except TimeoutError:
        raise AgentTimeout(f"could not read the peer of {sock} (no connect within {timeout:g} s)") from None
    except BlockingIOError:
        raise AgentTimeout(f"could not read the peer of {sock} (its listener's backlog is full)") from None
    except OSError as exc:
        raise AgentError(f"could not read the peer of {sock} ({exc.strerror or exc})") from None
    finally:
        s.close()
    return pid


# --- directories ----------------------------------------------------------------------------------

def _chain_problem(d: Path) -> str | None:
    """Why ``d`` cannot hold the agent's socket, or None: ``d`` and every directory above it are root's
    or this user's and writable by no one else (root's sticky directories excepted), and ``d`` itself
    is this user's and closed to everyone else."""
    try:
        st = os.lstat(d)
    except OSError as exc:
        return f"{d} cannot be read ({exc.strerror or exc})"
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        return f"{d} must be a directory (not a link) you own that no one else can open (0700)"
    for a in d.parents:
        try:
            ast = os.stat(a)
        except OSError as exc:
            return f"{a} cannot be read ({exc.strerror or exc})"
        if ast.st_uid not in (0, os.geteuid()) or (
                ast.st_mode & 0o022 and not (ast.st_uid == 0 and ast.st_mode & stat.S_ISVTX)):
            return f"{a}, above the agent's socket, can be written by another user"
    return None


def _private_dir(d: Path, *, exclusive: bool) -> None:
    """Make ``d`` 0700 (exclusively when asked: it must not exist yet) and check its chain."""
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        if exclusive:
            raise AgentError(f"{d} already exists; a session directory is made fresh") from None
    except OSError as exc:
        raise AgentError(f"could not create {d} ({exc.strerror or exc})") from None
    problem = _chain_problem(d)
    if problem is not None:
        raise AgentError(problem)


# --- the record -----------------------------------------------------------------------------------

def read_record(d: Path) -> dict[str, Any] | None:
    """The session directory ``d``'s ``agent.json``, or None when it is absent, a link, not ours, or
    not the shape this module writes."""
    try:
        fd = os.open(d / RECORD_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            return None
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not (isinstance(data, dict) and isinstance(data.get("pid"), int) and isinstance(data.get("owner"), int)
            and isinstance(data.get("keys"), list)):
        return None
    return data


def write_record(d: Path, data: dict[str, Any]) -> None:
    """Replace ``d``'s ``agent.json`` (0600, written beside it and renamed over it)."""
    tmp = d / f".{RECORD_NAME}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, d / RECORD_NAME)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- start, stop, sweep ---------------------------------------------------------------------------

_POPENS: dict[int, subprocess.Popen] = {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start(pid: int) -> str | None:
    """Linux: when ``pid`` started (``/proc/<pid>/stat`` field 22, clock ticks since boot, read after
    the last ``)`` because comm may hold one), so a reused pid is told from the process it was; None
    when it cannot be read (no such process, not Linux)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        return data[data.rindex(")") + 1:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _start_mark(pid: int) -> str | None:
    """Which process holds ``pid`` now, as its start time: :func:`process_start` on Linux, ``ps -o
    lstart=`` elsewhere (to the second: a pid reused within the same second reads as the same process,
    the residue off Linux, where no session agent starts yet). None when it cannot be read."""
    if sys.platform.startswith("linux"):
        return process_start(pid)
    try:
        done = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, timeout=5,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    mark = done.stdout.strip()
    return mark if done.returncode == 0 and mark else None


def _owner_alive(rec: dict[str, Any]) -> bool:
    """Whether the levain process that owns the agent in ``rec`` is still running: its pid is alive and,
    where the record holds the owner's start time, the process at that pid is the one that started
    (a reused owner pid keeps no orphan alive). An unreadable start time counts as alive (never sweep
    on a guess)."""
    if not _alive(rec["owner"]):
        return False
    recorded = rec.get("owner_start")
    if not isinstance(recorded, str):
        return True   # a record from before the start time was kept: the pid is all there is
    now = _start_mark(rec["owner"])
    return now is None or now == recorded


def _pidfd_exited(pidfd: int, timeout: float) -> bool:
    """Linux: wait up to ``timeout`` seconds for the process behind ``pidfd`` to exit (a pidfd polls
    readable when it does); whether it did."""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(int(timeout * 1000)))


def _kill_answering(pid: int, sock: str) -> bool:
    """SIGTERM, then SIGKILL after :data:`_STOP_WAIT`, the process at ``pid``, only while it is the one
    that answered on ``sock``; whether it was signalled. A pid is a number the kernel hands out again,
    so the process is pinned BEFORE the socket check and every signal goes to that pinned process:
    on Linux a pidfd (``os.pidfd_open`` then ``signal.pidfd_send_signal``; the wait polls the pidfd),
    elsewhere its start time, read again immediately before each signal. An agent that is not
    levain's child is reaped by init once it exits; nothing here waits for it."""
    pidfd: int | None = None
    if hasattr(os, "pidfd_open"):
        try:
            pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        except OSError:
            pidfd = None   # a kernel without pidfds (before 5.3): the start time below
    try:
        mark = None if pidfd is not None else _start_mark(pid)
        if pidfd is None and mark is None:
            return False   # neither a pidfd nor a start time: the process cannot be pinned, so no signal
        try:
            if peer_pid(sock) != pid:
                return False
        except AgentError:
            return False
        if pidfd is not None:
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                if not _pidfd_exited(pidfd, _STOP_WAIT):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass   # it exited: the pidfd never names another process
            return True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if _start_mark(pid) != mark:
                return True   # gone, or the pid is another process now: nothing more to send
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return True
            if sig == signal.SIGTERM:
                deadline = time.monotonic() + _STOP_WAIT
                while _alive(pid) and _start_mark(pid) == mark and time.monotonic() < deadline:
                    time.sleep(0.05)
        return True
    finally:
        if pidfd is not None:
            os.close(pidfd)


def start(*, entity: str = "", lifetime: int = DEFAULT_LIFETIME, ssh_mode: str = "agent") -> ScopedAgent:
    """Start one session's agent and return it, or :class:`AgentError` (nothing left behind). Sweeps
    agents a killed levain left first (:func:`sweep_orphans`). ``ssh_mode`` is the entity's floor
    setting, recorded for :func:`add_key` (only ``agent`` hides ``~/.ssh`` from bash)."""
    problem = openssh_problem()
    if problem is not None:
        raise AgentError(problem)
    sweep_orphans()
    root = run_root()
    if not root.parent.exists():
        _private_dir(root.parent, exclusive=False)
    _private_dir(root, exclusive=False)
    sid = secrets.token_hex(6)
    d = root / sid
    # `run_root()` is resolved and this preserves that canonical spelling for Seatbelt's
    # path-literal socket capability (notably macOS /tmp -> /private/tmp).
    sock = (d / SOCKET_NAME).resolve()
    if len(os.fsencode(str(sock))) > _SUN_PATH_MAX:
        raise AgentError(f"the agent's socket path {sock} is longer than a unix socket path may be "
                         f"({_SUN_PATH_MAX} bytes); set LEVAIN_HOME to a shorter directory")
    _private_dir(d, exclusive=True)
    proc: subprocess.Popen | None = None
    try:
        # The session's own known_hosts, which confined ssh may write (never the operator's, which their
        # own ssh trusts); `add_key` puts in it the checked host keys of the hosts each key is for.
        os.close(os.open(d / KNOWN_HOSTS_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
               "LANG": os.environ.get("LANG", "C")}
        # Its own session: a Ctrl-C at the operator's terminal reaches levain, never the agent.
        proc = subprocess.Popen([shutil.which("ssh-agent") or "ssh-agent", "-D", "-a", str(sock),
                                 "-P", NO_PROVIDER, "-t", str(int(lifetime))],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, cwd="/", close_fds=True, start_new_session=True)
        deadline = time.monotonic() + _START_WAIT
        while not sock.exists():
            if proc.poll() is not None:
                raise AgentError(f"ssh-agent exited with status {proc.returncode} before making its socket")
            if time.monotonic() > deadline:
                raise AgentError(f"ssh-agent made no socket at {sock} within {_START_WAIT:g}s")
            time.sleep(0.02)
        # ssh-agent binds the socket before it listens on it, so a connect in between is refused while
        # the path exists: retried while the agent runs, until the same deadline.
        while True:
            try:
                got = peer_pid(str(sock))
                break
            except AgentError:
                if proc.poll() is not None:
                    raise AgentError(f"ssh-agent exited with status {proc.returncode} before it "
                                     "answered on its socket") from None
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)
        if got != proc.pid:
            raise AgentError(f"the process answering on {sock} is pid {got}, not the agent levain started "
                             f"(pid {proc.pid})")
        write_record(d, {"session_id": sid, "pid": proc.pid, "owner": os.getpid(),
                         "owner_start": _start_mark(os.getpid()), "entity": entity,
                         "socket": str(sock), "lifetime": int(lifetime), "started_at": int(time.time()),
                         "ssh_mode": ssh_mode, "keys": []})
    except BaseException:
        if proc is not None:
            try:
                _terminate(proc)
            except (OSError, subprocess.SubprocessError):   # the start's own error is the one to report
                pass
        _remove_dir(d)
        raise
    _POPENS[proc.pid] = proc
    return ScopedAgent(sid, str(sock), proc.pid)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(_STOP_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(_STOP_WAIT)


def _remove_dir(d: Path) -> None:
    """Remove a session directory: only the names this module makes, then the directory."""
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if n in (SOCKET_NAME, RECORD_NAME, KNOWN_HOSTS_NAME) or n.startswith(f".{RECORD_NAME}."):
            try:
                os.unlink(d / n)
            except OSError:
                pass
    try:
        os.rmdir(d)
    except OSError:
        pass


def stop(agent: ScopedAgent) -> str | None:
    """Stop ``agent`` and remove its directory. Never raises; returns why it could not be stopped, or
    None. A pid this process did not start is signalled only while the socket's peer is that pid."""
    proc = _POPENS.pop(agent.pid, None)
    left: str | None = None
    try:
        if proc is not None:
            _terminate(proc)
        elif _alive(agent.pid) and not _kill_answering(agent.pid, agent.socket):
            left = f"pid {agent.pid} no longer answers on {agent.socket}; it was not signalled"
    except BaseException as exc:  # noqa: BLE001 — a teardown step; the caller reports the text
        left = f"stopping ssh-agent pid {agent.pid} failed ({exc})"
    _remove_dir(agent.dir)
    return left


def sessions() -> list[tuple[Path, dict[str, Any]]]:
    """Every session directory under :func:`run_root` with a readable record, in name order."""
    root = run_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for n in names:
        if not _SESSION_ID_RE.match(n):
            continue
        rec = read_record(root / n)
        if rec is not None:
            out.append((root / n, rec))
    return out


def live(d: Path, rec: dict[str, Any]) -> bool:
    """Whether the agent recorded in ``rec`` is the one answering on its socket now."""
    try:
        return peer_pid(str(d / SOCKET_NAME)) == rec["pid"]
    except AgentError:
        return False


def sweep_orphans() -> list[str]:
    """Stop the agents of levain processes that are gone and remove their directories; returns what
    was swept. An agent is killed only while its socket's peer is the recorded pid; a directory whose
    record cannot be read is left alone."""
    swept = []
    for d, rec in sessions():
        if rec["owner"] == os.getpid() or _owner_alive(rec):
            continue
        try:
            _kill_answering(rec["pid"], str(d / SOCKET_NAME))
        except OSError:
            pass   # a signal refused: the directory still goes, the agent's socket with it
        _remove_dir(d)
        swept.append(d.name)
    return swept


# --- levain ssh-add -------------------------------------------------------------------------------
#
# Keys go in only from the operator's own shell. The destinations a key is limited to are checked
# against a protected host-key manifest, ``<levain home>/ssh/known_hosts``, passed to ``ssh-add -H``
# (``build_policy`` hides ``<levain home>/ssh`` like ``run``). The operator fills it with host keys
# they have checked out of band: ``ssh-keyscan <host>``, its fingerprint compared with one the host's
# owner publishes (GitHub and GitLab publish theirs). Never copied from ``~/.ssh/known_hosts``: a
# confined process could write that file until B2a fix 1 (L1 #1 / L2 #1), and on macOS still can.

HOST_KEYS_DIR = "ssh"
HOST_KEYS_FILE = "known_hosts"
WAIVER_FLAG = "--no-destination-limit"
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]@-]+$")


def host_keys_file() -> Path:
    """``<levain home>/ssh/known_hosts``, absolute."""
    return (levain_home() / HOST_KEYS_DIR).resolve() / HOST_KEYS_FILE


def _no_new_privs() -> bool | None:
    """Linux: whether this process has ``no_new_privs`` set (``/proc/self/status``), or None when that
    cannot be read."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("NoNewPrivs:"):
                    return line.split(":", 1)[1].strip() == "1"
    except OSError:
        return None
    return None


def confinement_refusal(system: str | None = None) -> str | None:
    """Why this process must not add keys, from a test of its own context, never the environment: on
    Linux every levain confinement runs with ``no_new_privs`` (bwrap and the Landlock stub set it), so a
    process that has it, or cannot tell, is refused. On macOS, ask Seatbelt about a fixed sentinel every
    Levain profile denies, independent of entity-controlled environment variables."""
    system = system or platform.system()
    if system == "Darwin":
        confined = _macos_confinement_state()
        if confined is None:
            return "this process cannot ask Seatbelt whether it is inside a levain confinement"
        if confined:
            return ("this process is denied access to levain's confinement sentinel, as every levain macOS "
                    "confinement is; run `levain ssh-add` from your own shell")
        return None
    if system != "Linux":
        return f"levain's scoped ssh agent is not wired on {system}"
    nnp = _no_new_privs()
    if nnp is None:
        return "this process cannot read /proc/self/status, so it cannot show it is outside a levain confinement"
    if nnp:
        return ("this process runs with no_new_privs, as every levain confinement does; run `levain ssh-add` "
                "from your own shell (a shell that is not inside a levain session can have it too: Claude Code's "
                "own sandbox, a flatpak or snap terminal, a container, or a systemd unit with NoNewPrivileges=yes)")
    return None


def _macos_confinement_state() -> bool | None:
    """Whether this process's Seatbelt policy denies Levain's fixed sentinel path.

    ``sandbox_check`` only queries the policy already on this process; it does not apply a profile.
    ``SANDBOX_FILTER_PATH`` is 1 in Darwin's sandbox API.  None is fail-closed uncertainty.
    """
    try:
        check = ctypes.CDLL(None, use_errno=True).sandbox_check
        check.restype = ctypes.c_int
        # ``sandbox_check`` is variadic.  ctypes must still know the fixed arguments: without
        # ``argtypes`` arm64 macOS passes the filter discriminator in the wrong ABI shape and the
        # live API returns -1/EINVAL, which looks like a sandbox denial and would refuse the
        # operator's own shell.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        result = check(
            ctypes.c_int(os.getpid()),
            ctypes.c_char_p(b"file-write-data"),
            ctypes.c_int(1),
            ctypes.c_char_p(os.fsencode(MACOS_CONFINEMENT_SENTINEL)),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if result < 0:
        return None
    return result > 0


def _manifest_problem(f: Path) -> str | None:
    try:
        st = os.lstat(f)
    except FileNotFoundError:
        return (f"the host-key manifest {f} does not exist; put there the host keys you have checked out "
                f"of band (`ssh-keyscan <host>`, its fingerprint compared with one the host publishes; "
                f"GitHub and GitLab publish theirs), never lines from ~/.ssh/known_hosts, mode 0600")
    except OSError as exc:
        return f"the host-key manifest {f} cannot be read ({exc.strerror or exc})"
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        return f"the host-key manifest {f} must be a regular file (not a link) you own that no one else can write"
    for a in f.parents:
        try:
            ast = os.stat(a)
        except OSError as exc:
            return f"{a} cannot be read ({exc.strerror or exc})"
        if ast.st_uid not in (0, os.geteuid()) or (
                ast.st_mode & 0o022 and not (ast.st_uid == 0 and ast.st_mode & stat.S_ISVTX)):
            return f"{a}, above the host-key manifest, can be written by another user"
    return None


def _hop_hosts(dest: str) -> list[str]:
    """The host names in one ``-h`` destination (``[user@]host``, hops joined by ``>``)."""
    return [hop.rsplit("@", 1)[-1] for hop in dest.split(">")]


def _pick_session(session: str | None) -> tuple[Path, dict[str, Any]]:
    running = [(d, rec) for d, rec in sessions() if live(d, rec)]
    if session is not None:
        for d, rec in running:
            if d.name == session:
                return d, rec
        raise AgentError(f"no running levain session has the ssh agent {session!r}"
                         + (f" (running: {', '.join(d.name for d, _ in running)})" if running else ""))
    if not running:
        raise AgentError("no levain session with a scoped ssh agent is running")
    if len(running) > 1:
        raise AgentError("more than one levain session is running; name one with --session: "
                         + ", ".join(f"{d.name} ({rec.get('entity') or '?'})" for d, rec in running))
    return running[0]


def fingerprint(key: Path) -> str:
    """``SHA256:...`` of ``key`` (``ssh-keygen -l``), or :class:`AgentError`."""
    done = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], capture_output=True, env=_tool_env(),
                          text=True, stdin=subprocess.DEVNULL, timeout=30, check=False)
    fields = done.stdout.split()
    if done.returncode != 0 or len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise AgentError(f"{key} is not a key ssh-keygen can read ({(done.stderr or done.stdout).strip()})")
    return fields[1]


def _readable_key_problem(key: Path, rec: dict[str, Any]) -> str | None:
    """Why bash could read ``key``, or None. ssh-agent replaces a held key's constraints when the same
    key is added again (ssh-agent.c, process_add_identity), so a bash that can read the file strips
    its ``-h`` with a plain ``ssh-add`` while ``agent.json`` still prints "for <host>" (L2 #2). The
    floor's own decision is not reachable here without the entity's built policy, so this is the
    bound form: the key must resolve under ``~/.ssh`` with one link, and the session's entity must
    run ``ssh_mode="agent"``, where that whole directory is hidden from bash. A caller-declared
    ``deny_files`` key elsewhere is refused too (stricter than the floor). There is no waiver."""
    mode = rec.get("ssh_mode")
    if mode != "agent":
        return (f"the session's entity runs ssh_mode={mode or 'unknown'}, where bash can read the keys in "
                "~/.ssh and could re-add one without its -h limit; nothing was added")
    ssh_dir = (Path.home() / ".ssh").resolve()
    real = Path(os.path.realpath(key))
    try:
        nlink = os.stat(real).st_nlink
    except OSError as exc:
        return f"{key} cannot be read ({exc.strerror or exc})"
    if ssh_dir not in real.parents or nlink != 1:
        return (f"{key} is not a file only under {ssh_dir} (resolved: {real}, links: {nlink}), the one "
                "place the floor hides from bash in ssh_mode=agent; a key bash can read can be re-added "
                "from inside without its -h limit. Move it under ~/.ssh; nothing was added")
    return None


def _append_known_hosts(d: Path, block: str) -> None:
    fd = os.open(d / KNOWN_HOSTS_NAME, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(block)


def _take_back_known_hosts(d: Path, block: str) -> None:
    """Remove the last copy of ``block`` from the session's known_hosts. Only that block: confined ssh may
    have appended its own lines to this file since, and those stay."""
    if not block:
        return
    f = d / KNOWN_HOSTS_NAME
    fd = os.open(f, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r+", encoding="utf-8") as fh:
        text = fh.read()
        at = text.rfind(block)
        if at < 0:
            return
        fh.seek(0)
        fh.write(text[:at] + text[at + len(block):])
        fh.truncate()


def _take_back(d: Path, rec: dict[str, Any], prior: list[Any], block: str) -> None:
    """Undo an add whose ssh-add did not succeed: the record's keys as they were (a key an earlier add
    recorded keeps its entry) and the known_hosts lines this add wrote."""
    rec["keys"] = prior
    write_record(d, rec)
    _take_back_known_hosts(d, block)


def add_key(key: Path, hosts: list[str], *, lifetime: int = DEFAULT_LIFETIME, session: str | None = None,
            waive_destinations: bool = False, system: str | None = None) -> str:
    """``levain ssh-add``: add ``key`` to a running session's scoped agent, limited to ``hosts`` (checked
    against :func:`host_keys_file`) and to ``lifetime`` seconds, after checking that the agent answering
    on the session's socket is the pid levain started. Returns the line to print, or :class:`AgentError`
    (an OpenSSH tool that times out or cannot run is one too, never a traceback)."""
    try:
        return _add_key(key, hosts, lifetime=lifetime, session=session,
                        waive_destinations=waive_destinations, system=system)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(f"an OpenSSH tool or the session record failed ({exc}); check `levain doctor`") from None


def _add_key(key: Path, hosts: list[str], *, lifetime: int, session: str | None, waive_destinations: bool,
             system: str | None) -> str:
    why = confinement_refusal(system)
    if why is not None:
        raise AgentError(f"refused: {why}")
    problem = openssh_problem()
    if problem is not None:
        raise AgentError(problem)
    if not 0 < lifetime <= MAX_LIFETIME:
        raise AgentError(f"-t must be between 1 and {MAX_LIFETIME} seconds")
    if not hosts and not waive_destinations:
        raise AgentError(f"name the hosts the key is for with -h <host> (any host at all needs {WAIVER_FLAG}, "
                         "which `levain run` and `levain doctor` print as a waiver)")
    if hosts and waive_destinations:
        raise AgentError(f"-h and {WAIVER_FLAG} contradict each other")
    if not key.is_file():
        raise AgentError(f"{key} is not a key file")
    manifest = host_keys_file()
    bound: list[str] = []   # per -h host, the manifest keys the limit binds to (checked by the operator)
    if hosts:
        problem = _manifest_problem(manifest)
        if problem is not None:
            raise AgentError(problem)
        for dest in hosts:
            if not _HOST_RE.match(dest.replace(">", "")):
                raise AgentError(f"-h {dest!r} is not a destination ssh-add takes ([user@]host, hops joined by >)")
            for h in _hop_hosts(dest):
                found = subprocess.run(["ssh-keygen", "-F", h, "-l", "-E", "sha256", "-f", str(manifest)],
                                       capture_output=True, env=_tool_env(), text=True,
                                       stdin=subprocess.DEVNULL, timeout=30, check=False)
                if found.returncode != 0:
                    raise AgentError(f"{h} has no host key in {manifest}; add the key you have checked first")
                fps = [f"{f[2]} ({f[1]})" for f in (ln.split() for ln in found.stdout.splitlines()
                                                    if not ln.startswith("#"))
                       if len(f) >= 3 and f[2].startswith("SHA256:")]
                bound.append(f"  bound to {h}'s host key{'s' if len(fps) > 1 else ''} in {manifest}: "
                             + (", ".join(fps) or "(fingerprint not shown by ssh-keygen)"))
    fp = fingerprint(key)
    d, rec = _pick_session(session)
    why = _readable_key_problem(key, rec)
    if why is not None:
        raise AgentError(why)
    sock = str(d / SOCKET_NAME)
    got = peer_pid(sock)
    if got != rec["pid"]:
        raise AgentError(f"the process answering on {sock} is pid {got}, not the agent levain started "
                         f"(pid {rec['pid']}); nothing was added")
    argv = ["ssh-add", "-t", str(lifetime)]
    if hosts:
        argv += ["-H", str(manifest)]
        for dest in hosts:
            argv += ["-h", dest]
    argv.append(str(key))
    lines: list[str] = []
    if hosts:
        # The checked host keys of these hosts into the session's known_hosts, so confined ssh knows them.
        for dest in hosts:
            for h in _hop_hosts(dest):
                found = subprocess.run(["ssh-keygen", "-F", h, "-f", str(manifest)], capture_output=True, env=_tool_env(),
                                       text=True, stdin=subprocess.DEVNULL, timeout=30, check=False)
                lines += [ln for ln in found.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    block = "".join(f"{ln}\n" for ln in lines)
    # The known_hosts lines and the record entry are written BEFORE ssh-add, and taken back if it fails:
    # the other order left a key in the agent that the record did not show whenever a write failed after
    # ssh-add succeeded. All of it runs under a lock on the session directory, so two `levain ssh-add` at
    # once neither drop one key's record (L1 #8) nor take back each other's lines.
    dfd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        fcntl.flock(dfd, fcntl.LOCK_EX)
        if block:
            _append_known_hosts(d, block)
        rec = read_record(d) or rec
        prior = list(rec["keys"])
        rec["keys"] = [k for k in prior if not (isinstance(k, dict) and k.get("fingerprint") == fp)]
        rec["keys"].append({"fingerprint": fp, "hosts": list(hosts), "lifetime": lifetime,
                            "added_at": int(time.time()), "waived": bool(waive_destinations)})
        try:
            write_record(d, rec)
        except BaseException:
            _take_back_known_hosts(d, block)
            raise
        try:
            # stdin and the terminal stay: a passphrase prompt
            done = subprocess.run(argv, env=_tool_env(sock), check=False)
            failed = done.returncode != 0
        except BaseException:
            _take_back(d, rec, prior, block)
            raise
        if failed:
            _take_back(d, rec, prior, block)
            raise AgentError(f"ssh-add exited with status {done.returncode}; nothing was recorded")
    finally:
        os.close(dfd)
    limit = (f"for {', '.join(hosts)}" if hosts else
             "for ANY host, including hosts it is forwarded to (destination limit waived)")
    return "\n".join([f"added {fp} to session {d.name}'s scoped agent (pid {got}, peer checked) {limit}, "
                      f"for {lifetime}s", *bound])


# --- the run/doctor lines (D2) --------------------------------------------------------------------

DARWIN_LINE = ("ssh agent: levain starts a scoped one for each session's bash, never yours; add keys "
               "from your own shell with `levain ssh-add <key> -h <host>` (none running now)")
_NO_AGENT_TAIL = "$SSH_AUTH_SOCK is not passed to bash, so git over ssh inside bash fails"


_REQUEST_IDENTITIES, _IDENTITIES_ANSWER = 11, 12   # draft-miller-ssh-agent §3.4 / §6.1
_MAX_ANSWER = 1 << 20


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("the agent closed the connection")
        buf += chunk
    return buf


def _held(sock: str) -> list[str] | None:
    """The SHA256 fingerprints the agent on ``sock`` holds, or None if it cannot say. Asked in the agent
    protocol (REQUEST_IDENTITIES) and computed from each key blob, never parsed out of ``ssh-add -l``: a
    key comment is text whoever added the key chose, and one holding a newline and an ``ssh-add -l``
    shaped line listed a fingerprint the agent did not hold (L2 #10; an anchored line regex does not
    stop it, RUN 2026-10-14: the forged line ends with the real key's type)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(10)
        s.connect(sock)
        s.sendall(struct.pack(">IB", 1, _REQUEST_IDENTITIES))
        (n,) = struct.unpack(">I", _recv_exact(s, 4))
        if not 0 < n <= _MAX_ANSWER:
            return None
        body = _recv_exact(s, n)
    except (OSError, struct.error):
        return None
    finally:
        s.close()
    if body[0] != _IDENTITIES_ANSWER or len(body) < 5:
        return None
    (count,) = struct.unpack(">I", body[1:5])
    if count > (len(body) - 5) // 8:   # each identity is at least its two 4-byte lengths
        return None
    off, out = 5, []
    for _ in range(count):
        for field in ("blob", "comment"):
            if off + 4 > len(body):
                return None
            (ln,) = struct.unpack(">I", body[off:off + 4])
            if off + 4 + ln > len(body):
                return None
            if field == "blob":
                digest = hashlib.sha256(body[off + 4:off + 4 + ln]).digest()
                out.append("SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("="))
            off += 4 + ln
    return out


def _left(secs: int) -> str:
    h, m = divmod(max(secs, 0) // 60, 60)
    return f"{h}h{m:02d}m left" if h else f"{m}m left"


def key_lines(d: Path, rec: dict[str, Any], *, now: float | None = None) -> list[str]:
    """One line per key the agent in ``d`` holds: its destinations and remaining lifetime from the record,
    or, for a key the record does not have, that no limit is known. Or the "no keys" line."""
    held = _held(str(d / SOCKET_NAME))
    if held is None:
        return [f"ssh agent keys: could not be listed (the agent on {d / SOCKET_NAME} did not answer)"]
    if not held:
        if rec.get("ssh_mode") == "raw":
            return [f"ssh agent: no keys, and none can be added: the entity runs ssh_mode=raw, where bash can "
                    f"read ~/.ssh, so a key bash can read could be added again without its -h limit"
                    f" (session {d.name})"]
        return [f"ssh agent: no keys; add one from your own shell: `levain ssh-add <key> -h <host>`"
                f" (session {d.name})"]
    now = time.time() if now is None else now
    known = {k["fingerprint"]: k for k in rec["keys"] if isinstance(k, dict) and "fingerprint" in k}
    out = []
    for fp in held:
        k = known.get(fp)
        if k is None:
            out.append(f"ssh agent key {fp}: added from inside the session, no destination limit known")
            continue
        left = _left(int(k.get("added_at", 0)) + int(k.get("lifetime", 0)) - int(now))
        if k.get("waived"):
            out.append(f"waiver (levain ssh-add {WAIVER_FLAG}) ssh agent key {fp}: ANY host, including hosts "
                       f"it is forwarded to, {left}")
        else:
            out.append(f"ssh agent key {fp}: for {', '.join(k.get('hosts') or [])}, {left}")
    return out


def status_lines(system: str, *, agent: ScopedAgent | None = None, note: str | None = None,
                 hands: bool = False, tier: str | None = None) -> list[str]:
    """The ssh lines ``levain run`` (with this session's ``agent``, or the ``note`` saying why it has
    none) and ``levain doctor`` (neither: every running session's agent) print. ``tier`` is the
    Landlock tier (:func:`levain.firing.landlock.tier_for`): only at T9 are pathname unix sockets
    outside the sandbox refused, so only there does bash reach the scoped agent alone; unknown is
    read as below T9."""
    if system not in ("Linux", "Darwin"):
        return []
    if hands:
        return [f"ssh agent: none (the entity's own user); {_NO_AGENT_TAIL}"]
    if agent is not None:
        rec = read_record(agent.dir) or {"keys": []}
        reach = ("bash reaches only it, at its $SSH_AUTH_SOCK" if system == "Darwin" or tier == "T9" else
                 f"bash's $SSH_AUTH_SOCK is it; an agent socket elsewhere in bash's view (e.g. /tmp) stays "
                 f"reachable at this tier ({tier or 'unknown'}, pathname unix sockets are refused only at T9)")
        return [f"ssh agent: levain's scoped agent for this session (pid {agent.pid}, {agent.socket}); {reach}",
                *key_lines(agent.dir, rec)]
    if note is not None:
        return [f"ssh agent: none for this session ({note}); {_NO_AGENT_TAIL}"]
    problem = openssh_problem()
    if problem is not None:
        return [f"ssh agent: none; levain starts no scoped agent here ({problem}); {_NO_AGENT_TAIL}"]
    running = [(d, rec) for d, rec in sessions() if live(d, rec)]
    if not running:
        return [DARWIN_LINE if system == "Darwin" else
                "ssh agent: levain starts a scoped one for each session's bash, never yours; add keys from "
                "your own shell with `levain ssh-add <key> -h <host>` (none running now)"]
    out = []
    for d, rec in running:
        out.append(f"ssh agent: session {d.name} ({rec.get('entity') or '?'}), pid {rec['pid']}")
        out.extend(key_lines(d, rec))
    return out
