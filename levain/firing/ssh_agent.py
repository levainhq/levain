"""levain.firing.ssh_agent — the scoped ssh agent bash uses (B2a; design 1012-gpgesc rev 6 §7.2, §8.5).

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

import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUN_DIR = "run"
SOCKET_NAME = "agent.sock"
RECORD_NAME = "agent.json"
VIEW_DIR = "levain-ssh"
NO_PROVIDER = "!*"
MIN_OPENSSH = (8, 9)   # destination constraints (ssh-add -h) arrived in OpenSSH 8.9
DEFAULT_LIFETIME = 8 * 3600
MAX_LIFETIME = 24 * 3600
_SUN_PATH_MAX = 103   # macOS sun_path is 104 bytes with its NUL; Linux's 108 is the looser
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_START_WAIT = 5.0
_STOP_WAIT = 3.0


class AgentError(RuntimeError):
    """The scoped agent cannot be started, found or used; the text says why, for the operator."""


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


# --- OpenSSH --------------------------------------------------------------------------------------

def openssh_version(ssh: str | None = None) -> tuple[int, int] | None:
    """``(major, minor)`` from ``ssh -V`` (it prints to stderr: ``OpenSSH_10.3p1, LibreSSL ...``), or
    None when there is no ssh or it is not OpenSSH."""
    exe = ssh or shutil.which("ssh")
    if exe is None:
        return None
    try:
        done = subprocess.run([exe, "-V"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
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

def peer_pid(sock: str) -> int:
    """The pid of the process listening on ``sock``, as the kernel reports it for a connection to it:
    ``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on macOS. :class:`AgentError` when it cannot be read."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(sock)
        if hasattr(socket, "SO_PEERCRED"):
            pid, _uid, _gid = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                               struct.calcsize("3i")))
        elif platform.system() == "Darwin":
            # SOL_LOCAL = 0, LOCAL_PEERPID = 2 (<sys/un.h>); Python names neither.
            pid = struct.unpack("i", s.getsockopt(0, 2, struct.calcsize("i")))[0]
        else:
            raise AgentError(f"this platform ({platform.system()}) has no peer-pid read levain knows")
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


def start(*, entity: str = "", lifetime: int = DEFAULT_LIFETIME) -> ScopedAgent:
    """Start one session's agent and return it, or :class:`AgentError` (nothing left behind). Sweeps
    agents a killed levain left first (:func:`sweep_orphans`)."""
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
    sock = d / SOCKET_NAME
    if len(os.fsencode(str(sock))) > _SUN_PATH_MAX:
        raise AgentError(f"the agent's socket path {sock} is longer than a unix socket path may be "
                         f"({_SUN_PATH_MAX} bytes); set LEVAIN_HOME to a shorter directory")
    _private_dir(d, exclusive=True)
    proc: subprocess.Popen | None = None
    try:
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
        got = peer_pid(str(sock))
        if got != proc.pid:
            raise AgentError(f"the process answering on {sock} is pid {got}, not the agent levain started "
                             f"(pid {proc.pid})")
        write_record(d, {"session_id": sid, "pid": proc.pid, "owner": os.getpid(), "entity": entity,
                         "socket": str(sock), "lifetime": int(lifetime), "started_at": int(time.time()),
                         "keys": []})
    except BaseException:
        if proc is not None:
            _terminate(proc)
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
        if n in (SOCKET_NAME, RECORD_NAME) or n.startswith(f".{RECORD_NAME}."):
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
        elif _alive(agent.pid):
            try:
                same = peer_pid(agent.socket) == agent.pid
            except AgentError:
                same = False
            if same:
                os.kill(agent.pid, signal.SIGTERM)
                deadline = time.monotonic() + _STOP_WAIT
                while _alive(agent.pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _alive(agent.pid):
                    os.kill(agent.pid, signal.SIGKILL)
            else:
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
        if rec["owner"] == os.getpid() or _alive(rec["owner"]):
            continue
        if live(d, rec):
            try:
                os.kill(rec["pid"], signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + _STOP_WAIT
            while _alive(rec["pid"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _alive(rec["pid"]):
                try:
                    os.kill(rec["pid"], signal.SIGKILL)
                except OSError:
                    pass
        _remove_dir(d)
        swept.append(d.name)
    return swept


# --- levain ssh-add -------------------------------------------------------------------------------
#
# Keys go in only from the operator's own shell. The destinations a key is limited to are checked
# against a protected host-key manifest, ``<levain home>/ssh/known_hosts``, passed to ``ssh-add -H``:
# never ``~/.ssh/known_hosts``, which a confined process can write (``build_policy`` hides
# ``<levain home>/ssh`` like ``run``). The operator fills it with host keys they have checked, e.g. the
# matching lines of their own ``~/.ssh/known_hosts``.

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
    process that has it, or cannot tell, is refused. Off Linux there is no scoped agent to add to yet."""
    system = system or platform.system()
    if system != "Linux":
        return f"levain's scoped ssh agent is not wired on {system} yet (B2b)"
    nnp = _no_new_privs()
    if nnp is None:
        return "this process cannot read /proc/self/status, so it cannot show it is outside a levain confinement"
    if nnp:
        return ("this process runs with no_new_privs, as every levain confinement does; run `levain ssh-add` "
                "from your own shell")
    return None


def _manifest_problem(f: Path) -> str | None:
    try:
        st = os.lstat(f)
    except FileNotFoundError:
        return (f"the host-key manifest {f} does not exist; put the host keys you have checked there (e.g. "
                f"the lines for the host from your ~/.ssh/known_hosts), mode 0600")
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
    done = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=30, check=False)
    fields = done.stdout.split()
    if done.returncode != 0 or len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise AgentError(f"{key} is not a key ssh-keygen can read ({(done.stderr or done.stdout).strip()})")
    return fields[1]


def add_key(key: Path, hosts: list[str], *, lifetime: int = DEFAULT_LIFETIME, session: str | None = None,
            waive_destinations: bool = False, system: str | None = None) -> str:
    """``levain ssh-add``: add ``key`` to a running session's scoped agent, limited to ``hosts`` (checked
    against :func:`host_keys_file`) and to ``lifetime`` seconds, after checking that the agent answering
    on the session's socket is the pid levain started. Returns the line to print, or :class:`AgentError`."""
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
    if hosts:
        problem = _manifest_problem(manifest)
        if problem is not None:
            raise AgentError(problem)
        for dest in hosts:
            if not _HOST_RE.match(dest.replace(">", "")):
                raise AgentError(f"-h {dest!r} is not a destination ssh-add takes ([user@]host, hops joined by >)")
            for h in _hop_hosts(dest):
                found = subprocess.run(["ssh-keygen", "-F", h, "-f", str(manifest)], capture_output=True,
                                       stdin=subprocess.DEVNULL, timeout=30, check=False)
                if found.returncode != 0:
                    raise AgentError(f"{h} has no host key in {manifest}; add the key you have checked first")
    fp = fingerprint(key)
    d, rec = _pick_session(session)
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
    env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
    env["SSH_AUTH_SOCK"] = sock
    done = subprocess.run(argv, env=env, check=False)   # stdin and the terminal stay: a passphrase prompt
    if done.returncode != 0:
        raise AgentError(f"ssh-add exited with status {done.returncode}; nothing was recorded")
    rec = read_record(d) or rec
    rec["keys"] = [k for k in rec["keys"] if not (isinstance(k, dict) and k.get("fingerprint") == fp)]
    rec["keys"].append({"fingerprint": fp, "hosts": list(hosts), "lifetime": lifetime,
                        "added_at": int(time.time()), "waived": bool(waive_destinations)})
    write_record(d, rec)
    limit = f"for {', '.join(hosts)}" if hosts else "for ANY host (destination limit waived)"
    return (f"added {fp} to session {d.name}'s scoped agent (pid {got}, peer checked) {limit}, "
            f"for {lifetime}s")
