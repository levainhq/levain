"""levain.launch — the console entry, which hardens the process before anything else is imported.

``pyproject.toml`` points ``levain`` here, and ``python -m levain`` comes here too, so every way of
starting the CLI passes through :func:`main`. :func:`levain.cli.main` stays importable and callable
on its own (the tests call it in-process), and does none of this.

What it does, in order (lane P2's research note, items 2a-2c):
  1. re-executes levain with an ALLOWLISTED environment when its own carries anything else, or when
     ``--api-key VALUE`` is on the command line (:func:`reexec_if_needed`);
  2. on Linux, marks the process not dumpable, the way ssh-agent and Chromium's setuid sandbox do;
  3. hands over to :func:`levain.cli.main`.

WHY A RE-EXEC. Both kernels report a process's environment as it was at ``execve``: Linux's
``/proc/<pid>/environ`` ("the initial environment that was set when the currently executing program
was started via execve(2)", proc_pid_environ(5)) and XNU's ``KERN_PROCARGS2`` (which copies the
strings region at the top of the user stack, readable by any process of the same user). Editing
``os.environ`` changes neither: Apple's libc ``unsetenv`` shifts the pointer array and never
overwrites the stack strings. So a token exported in the shell that launched levain (``GH_TOKEN``,
``AWS_SECRET_ACCESS_KEY``, ``ANTHROPIC_API_KEY``) stayed readable from inside the entity's sandbox for
the life of the session, through ``ps -E`` on macOS. Only a new ``execve`` replaces that block.

THE SHAPE. :data:`_ALLOW` (fixed names known not to be secret, and a few config namespaces) stays in
the exec-time environment. Everything else, the CARRY, goes to the new image through an inheritable
file descriptor (a ``memfd`` on Linux, an unlinked 0600 temp file on macOS: not a pipe, which blocks
the writer past its buffer with nobody reading), whose number travels in ``LEVAIN_CARRY_FD``. The
new image reads it first thing and puts the CARRY back into ``os.environ``, which is heap memory and
in neither kernel report, so litellm, OpenHands, proxies and CA bundles behave as before. An
allowlist and not a pattern for secret-looking names: the floor refuses to guess where a secret is,
and a proxy URL can carry ``user:password@``. The pid does not change (``execve`` keeps it), so
launchd and systemd supervision and bwrap's ``--die-with-parent`` are unaffected.

THE OTHER HALF IS IN EVERY CHILD. A child levain starts gets its own exec-time block, so each
``subprocess`` call passes ``env=`` built by :func:`child_env` (the allowlist plus the names that
child needs), never a copy of ``os.environ``; ``tests/test_launch.py`` fails on a call that does not.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PR_SET_DUMPABLE = 4   # <linux/prctl.h>
CARRY_FD_ENV = "LEVAIN_CARRY_FD"
API_KEY_ENV = "LEVAIN_API_KEY"

# Names known not to hold a secret. Everything else is carried, which costs nothing functionally.
_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "SHLVL", "LANG", "LANGUAGE", "TERM",
    "COLORTERM", "TERM_PROGRAM", "TZ", "TMPDIR", "SSH_AUTH_SOCK", "VIRTUAL_ENV", "NO_COLOR",
    "COLUMNS", "LINES", "EDITOR", "VISUAL", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
    "__CF_USER_TEXT_ENCODING", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLOUDSDK_CONFIG",
    "AZURE_CONFIG_DIR", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_LOGIN_CACHE_DIRECTORY",
    # The dynamic loader's search paths. Not secrets, and an interpreter or extension found only
    # through one (an HPC module, a conda or nix build of libpython) would not start without it.
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
})
# Config namespaces: the locale, XDG dirs, the interpreter's own switches, and this stack's settings.
# A name in one of them that is SHAPED like a credential is carried all the same (:data:`_TOKEN_SHAPED`):
# a namespace is a pattern, and a future ``LEVAIN_TEAM_TOKEN`` must not ride the exec-time block
# because its prefix was allowed. Such a name is allowed only by being listed in :data:`_ALLOW`.
_ALLOW_PREFIXES = ("LC_", "XDG_", "PYTHON", "LEVAIN_", "VAGUS_", "ANNEAL_")
_TOKEN_SHAPED = re.compile(r"(^|_)(KEY|KEYS|TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWD|PASS|PWD|"
                           r"CREDENTIAL|CREDENTIALS|AUTH|COOKIE|SESSION)(_|$)")
# Names in an allowed namespace that are secrets or plumbing, never allowed.
_NEVER = frozenset({API_KEY_ENV, CARRY_FD_ENV})
# Where the macOS carry file is made: a directory under the floor's own crown jewel
# (``~/.levain-runtime/floor``, denied to every entity), so no confined shell can open it by name.
CARRY_DIR = Path("~/.levain-runtime/floor/carry")

# What a child that reaches the network needs from the carried environment.
NETWORK = tuple(n for base in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "FTP_PROXY")
                for n in (base, base.lower())) + (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)

_lifted_api_key: str | None = None
_secret_files: list[Path] = []   # files holding a secret levain was handed (the API key file)


def add_secret_file(path: str | os.PathLike) -> None:
    """Record a file that holds a secret levain was given, so the confinement floor denies it to the
    entity both ways (``levain.firing.confinement.build_policy`` reads :func:`secret_files`)."""
    p = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if p not in _secret_files:
        _secret_files.append(p)


def secret_files() -> list[Path]:
    return list(_secret_files)
reexecuted = False   # True in an image this module re-executed (it restored a carry)


def allowed(name: str) -> bool:
    """Whether ``name`` may stay in an exec-time environment (levain's own after the re-exec, and
    every child's by default)."""
    if name in _NEVER:
        return False
    if name in _ALLOW:
        return True
    return name.startswith(_ALLOW_PREFIXES) and not _TOKEN_SHAPED.search(name)


def allowed_env(environ) -> dict:
    """The allowlisted part of ``environ`` (a ``str`` or ``bytes`` mapping), the one list every exec
    levain makes goes through: its own re-exec, :func:`child_env`, and a shell run as another user."""
    out = {}
    for k, v in environ.items():
        name = os.fsdecode(k) if isinstance(k, bytes) else k
        if allowed(name):
            out[k] = v
    return out


def child_env(*names: str, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment for a child levain starts: the allowlist, plus the carried ``names`` and
    ``prefixes`` that child needs (a git push needs the proxy variables, say). Never the whole of
    ``os.environ``: whatever a child is given sits in ITS exec-time block while it runs, readable by
    every process of this user."""
    env = allowed_env(os.environ)
    env.update({k: v for k, v in os.environ.items()
                if k in names or (prefixes and k.startswith(prefixes))})
    return env


def open_browser(url: str, unlocked: str | None = None) -> None:
    """Open ``url`` in the operator's browser (``unlocked``, a URL carrying a token, only through the
    controller that keeps it off every command line; see :mod:`levain._browser`). Done in a child
    with :func:`child_env` and the URLs on its stdin, because ``webbrowser`` hands the browser this
    process's whole environment. Best effort: no browser is fine."""
    payload = json.dumps({"url": url, "unlocked": unlocked})
    try:
        subprocess.run([sys.executable, "-P", "-m", "levain._browser"], input=payload, text=True,
                       capture_output=True, timeout=60, env=child_env("BROWSER"))
    except (OSError, subprocess.SubprocessError):
        pass


def take_lifted_api_key() -> str | None:
    """The ``--api-key`` value the re-exec lifted out of the command line, once."""
    global _lifted_api_key
    key, _lifted_api_key = _lifted_api_key, None
    return key


def _lift_api_key(args: list[str]) -> tuple[list[str], str | None]:
    """``args`` without ``--api-key VALUE`` / ``--api-key=VALUE`` (before any ``--``), and the value.
    argparse cannot abbreviate it any more: ``--api-key-file`` makes every prefix ambiguous."""
    out: list[str] = []
    key: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i:]
            break
        if a == "--api-key" and i + 1 < len(args):
            key = args[i + 1]
            i += 2
            continue
        if a.startswith("--api-key="):
            key = a[len("--api-key="):]
            i += 1
            continue
        out.append(a)
        i += 1
    return out, key


def _carry_fd(payload: bytes) -> int:
    """An inheritable descriptor holding ``payload``, which never has a name while it holds data.
    Linux: a ``memfd`` (no name at all). Elsewhere: a file created ``O_CREAT|O_EXCL`` 0600 in
    :data:`CARRY_DIR` (0700, under a directory the floor denies to every entity) and unlinked BEFORE
    anything is written to it, so whoever opened it by name in that instant opened an empty file."""
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("levain-carry", 0)   # no MFD_CLOEXEC: it must survive the exec
    else:
        d = CARRY_DIR.expanduser()
        for level in (*reversed(d.parents[:3]), d):   # each level 0700, not the umask's mode
            try:
                level.mkdir(mode=0o700)
            except FileExistsError:
                pass
        st = os.lstat(d)
        if not (os.path.isdir(d) and not os.path.islink(d) and st.st_uid == os.geteuid()):
            raise OSError(f"{d} is not a directory this user owns")
        os.chmod(d, 0o700)
        path = d / f"c-{os.urandom(16).hex()}"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.unlink(path)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _restore_carry(value: str) -> None:
    """In the re-executed image: read the carry, close it, put it back into ``os.environ``."""
    global _lifted_api_key, reexecuted
    # Not dumpable BEFORE the carry enters this process's memory: until then nothing of this user
    # may read it through /proc/<pid>/mem or /proc/<pid>/fd (Linux; execve reset the flag).
    set_not_dumpable()
    os.environ.pop(CARRY_FD_ENV, None)
    try:
        fd = int(value)
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        os.close(fd)
        data = json.loads(b"".join(chunks))
        env = {base64.b64decode(k): base64.b64decode(v) for k, v in data["env"].items()}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # The operator's own environment did not arrive. Running on without it would fail later
        # and somewhere else (a model call with no key), so say so here and stop.
        print(f"levain: could not read the carried environment ({exc}); not starting.",
              file=sys.stderr)
        raise SystemExit(2) from None
    os.environb.update(env)
    _lifted_api_key = data.get("api_key")
    reexecuted = True


def reexec_if_needed() -> None:
    """Re-execute this process with only allowlisted names in its exec-time environment, carrying
    the rest by file descriptor. Does nothing when nothing needs carrying, and runs at most once
    (the re-executed image finds ``LEVAIN_CARRY_FD`` and restores instead). POSIX only."""
    if os.name != "posix":
        return
    if (value := os.environ.get(CARRY_FD_ENV)) is not None:
        _restore_carry(value)
        return
    args, key = _lift_api_key(sys.argv[1:])
    keep = allowed_env(os.environb)
    carry = {k: v for k, v in os.environb.items() if k not in keep}
    if not carry and key is None:
        return
    head = sys.orig_argv[: len(sys.orig_argv) - len(sys.argv) + 1]
    if sys.orig_argv[len(head):] != sys.argv[1:] or not sys.executable:
        # Running on would leave the launch environment readable for the whole session, and that is
        # what this function exists to prevent, so it refuses and says how to start levain instead.
        print("levain: could not rebuild this command line to start levain without its launch "
              "environment; not starting. Run the `levain` command, or `python -m levain`.",
              file=sys.stderr)
        raise SystemExit(2)
    if key is not None:
        print("levain: --api-key on the command line was readable by other processes until now"
              " (on Linux, by every user); use --api-key-file PATH or LEVAIN_API_KEY instead.",
              file=sys.stderr)
    payload = json.dumps({
        "env": {base64.b64encode(k).decode(): base64.b64encode(v).decode() for k, v in carry.items()},
        "api_key": key,
    }).encode()
    try:
        fd = _carry_fd(payload)
    except OSError as exc:
        print(f"levain: could not hide the launch environment ({exc}); continuing with it.",
              file=sys.stderr)
        return
    keep[CARRY_FD_ENV.encode()] = str(fd).encode()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(sys.executable, [*head, *args], keep)
    except OSError as exc:
        os.close(fd)
        print(f"levain: could not re-execute to hide the launch environment ({exc}); "
              "continuing with it.", file=sys.stderr)


def set_not_dumpable() -> bool:
    """On Linux, ``prctl(PR_SET_DUMPABLE, 0)``: ``/proc/<levain>/*`` becomes root-owned and ptrace
    access mode checks fail for every other process of this user ("Deny access if the target process
    "dumpable" attribute has a value other than 1", ptrace(2)). So no other process of the operator,
    and no entity reaching one, can read levain's memory, ``environ`` or open files through procfs.
    It lasts until the next ``execve``, so the children levain starts are unaffected.
    Costs: no core dumps of levain, and attaching ``py-spy`` or ``gdb`` to it needs root.
    Returns True when the flag was set; False off Linux or when the call failed. macOS has no
    counterpart: ``PT_DENY_ATTACH`` does not gate ``KERN_PROCARGS2`` (the XNU source)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def main() -> int:
    reexec_if_needed()
    set_not_dumpable()
    if sys.platform.startswith("linux"):
        try:   # a crashed session's floor placeholders (see levain.firing.confinement)
            from levain.firing.confinement import sweep_floor_placeholders

            sweep_floor_placeholders()
        except Exception:  # noqa: BLE001 — housekeeping must never stop the CLI starting
            pass
    from levain.cli import main as cli_main

    return cli_main()
