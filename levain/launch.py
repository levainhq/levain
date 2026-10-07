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
import sys

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
})
# Config namespaces: the locale, XDG dirs, the interpreter's own switches, and this stack's settings.
_ALLOW_PREFIXES = ("LC_", "XDG_", "PYTHON", "LEVAIN_", "VAGUS_", "ANNEAL_")
# Names in an allowed namespace that are secrets or plumbing, never allowed.
_NEVER = frozenset({API_KEY_ENV, CARRY_FD_ENV})

# What a child that reaches the network needs from the carried environment.
NETWORK = tuple(n for base in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "FTP_PROXY")
                for n in (base, base.lower())) + (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)

_lifted_api_key: str | None = None
reexecuted = False   # True in an image this module re-executed (it restored a carry)


def allowed(name: str) -> bool:
    """Whether ``name`` may stay in an exec-time environment (levain's own after the re-exec, and
    every child's by default)."""
    return name not in _NEVER and (name in _ALLOW or name.startswith(_ALLOW_PREFIXES))


def child_env(*names: str, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment for a child levain starts: the allowlist, plus the carried ``names`` and
    ``prefixes`` that child needs (a git push needs the proxy variables, say). Never the whole of
    ``os.environ``: whatever a child is given sits in ITS exec-time block while it runs, readable by
    every process of this user."""
    return {k: v for k, v in os.environ.items()
            if allowed(k) or k in names or (prefixes and k.startswith(prefixes))}


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
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("levain-carry", 0)   # no MFD_CLOEXEC: it must survive the exec
    else:
        import tempfile

        fd, path = tempfile.mkstemp(prefix="levain-carry-")   # 0600
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
    keep = {k: v for k, v in os.environb.items() if allowed(os.fsdecode(k))}
    carry = {k: v for k, v in os.environb.items() if k not in keep}
    if not carry and key is None:
        return
    head = sys.orig_argv[: len(sys.orig_argv) - len(sys.argv) + 1]
    if sys.orig_argv[len(head):] != sys.argv[1:] or not sys.executable:
        return   # an interpreter invocation this cannot rebuild faithfully: run as started
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
