#!/usr/bin/env python3
"""Run the levain CLI for the TERMSIGN recovery drill (the scratch-root drill in the enrolment runbook, Part B).

The CLI's child environment strips ``SSH_ASKPASS`` and ``SSH_ASKPASS_REQUIRE`` (``levain.launch.allowed_env``), so
a drill whose software keys carry a passphrase can never get that passphrase to ``ssh-keygen`` through the real
entry point. This wrapper lets exactly those two names through, for the duration of one ``levain.cli.main`` call,
and puts the original back afterwards.

It calls ``cli.main`` directly, so it skips ``launch.main()``'s ``reexec_if_needed()`` and ``set_not_dumpable()``:
a drill tool, never the way to run levain for real.

It never reads, prints or logs a passphrase: the passphrase lives only in the askpass script the operator wrote,
which ``ssh-keygen`` runs.

Confinement is a bound, not a scanner of arbitrary arguments. The drill runs under a scratch directory ``S``
(``S=$(mktemp -d)``, ``HOME=$S/home``). The wrapper refuses unless HOME is ``<S>/home`` with ``S`` inside the system
temp dir and private to you, ``LEVAIN_HOME`` (if set) is inside ``S``, only the cockpit subcommands the drill uses are asked for, and
every argument (the value of ``--opt=value`` included; a bare name counts) resolves, with ``~`` expanded and relative to
``S``, which becomes the working directory; single-dash options are refused. It also drops
``SSH_AUTH_SOCK`` so a software key cannot reach the operator's real agent.

Usage (from a checkout, or with PYTHONPATH naming one): ``python3 scripts/termsign_drill_cli.py cockpit keys --data-root <S>/data``
"""
from __future__ import annotations

import os
import sys
import tempfile

PASS_THROUGH = ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")
# the (first two words of the) commands the drill uses; anything else is not the drill
ALLOWED = (("cockpit", "keys"), ("cockpit", "enroll"), ("cockpit", "revoke"), ("cockpit", "ledger"))


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _inside(path: str, root: str) -> bool:
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _scratch() -> tuple[str | None, str | None]:
    """``(scratch, None)`` when HOME is ``<scratch>/home`` with ``<scratch>`` a private directory of ours directly
    inside the system temp dir, else ``(None, why)``."""
    home = os.environ.get("HOME", "")
    if not home or os.path.basename(_real(home)) != "home":
        return None, "HOME is not <scratch>/home; set S=$(mktemp -d) and HOME=$S/home first"
    scratch = os.path.dirname(_real(home))
    tmp = _real(tempfile.gettempdir())
    if os.path.dirname(scratch) != tmp:
        return None, "the scratch directory is not a direct child of the system temp dir (use S=$(mktemp -d))"
    st = os.stat(scratch)
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        return None, "the scratch directory is not private to you (mode 0700, yours): use S=$(mktemp -d)"
    lh = os.environ.get("LEVAIN_HOME")
    if lh and not _inside(lh, scratch):
        return None, "LEVAIN_HOME points outside the scratch directory; unset it"
    return scratch, None


def _refusal(argv: list[str]) -> str | None:
    """Why this run would not be confined to the scratch directory, or None. Every argument (and the value after the
    ``=`` of a ``--opt=value``) is read as a path relative to the scratch directory, which ``main`` also makes the
    working directory, so a bare name or an omitted ``--data-root`` lands inside it; one that resolves outside is
    refused. A single-dash option (``-xVALUE``) is refused outright."""
    scratch, why = _scratch()
    if scratch is None:
        return why
    if tuple(argv[:2]) not in ALLOWED:
        return "not a drill command (cockpit keys | enroll | revoke | ledger)"
    for a in argv:
        if a.startswith("-") and not a.startswith("--"):
            return f"short options are not allowed in the drill: {a}"
        token = a.partition("=")[2] if a.startswith("--") else a
        if token and not _inside(os.path.join(scratch, os.path.expanduser(token)), scratch):
            return f"an argument names a path outside the scratch directory: {token}"
    return None


def main(argv: list[str]) -> int:
    why = _refusal(argv)
    if why is not None:
        print(f"termsign_drill_cli: {why}", file=sys.stderr)
        return 2
    from levain import cli, launch

    original = launch.allowed_env

    def allowed_env(environ):
        out = original(environ)
        for k, v in environ.items():
            name = os.fsdecode(k) if isinstance(k, bytes) else k
            if name in PASS_THROUGH:
                out[k] = v
        return out

    scratch, _ = _scratch()
    old_cwd, old_sock = os.getcwd(), os.environ.pop("SSH_AUTH_SOCK", None)
    os.chdir(scratch)                      # a bare name, or an omitted --data-root (it defaults to the cwd), lands in scratch
    launch.allowed_env = allowed_env
    try:
        return cli.main(argv)
    finally:
        launch.allowed_env = original
        os.chdir(old_cwd)
        if old_sock is not None:
            os.environ["SSH_AUTH_SOCK"] = old_sock


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
