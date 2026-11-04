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
temp dir, ``LEVAIN_HOME`` (if set) is inside ``S``, only the cockpit subcommands the drill uses are asked for, and
every argument that is a path (it contains a separator, or starts with ``~`` or ``.``; the value of ``--opt=value``
included) resolves, with ``~`` expanded and relative to the working directory, inside ``S``. It also drops
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


def _is_pathy(token: str) -> bool:
    return not token.startswith("SHA256:") and (os.sep in token or token.startswith(("~", ".")))     # a fingerprint may hold "/"


def _refusal(argv: list[str]) -> str | None:
    """Why this run would not be confined to the scratch directory, or None."""
    home = os.environ.get("HOME", "")
    if not home or os.path.basename(os.path.normpath(home)) != "home":
        return "HOME is not <scratch>/home; set S=$(mktemp -d) and HOME=$S/home first"
    scratch = os.path.dirname(_real(home))
    tmp = _real(tempfile.gettempdir())
    if scratch == tmp or not _inside(scratch, tmp):
        return "the scratch directory is not a new directory inside the system temp dir (use S=$(mktemp -d))"
    lh = os.environ.get("LEVAIN_HOME")
    if lh and not _inside(lh, scratch):
        return "LEVAIN_HOME points outside the scratch directory; unset it"
    if tuple(argv[:2]) not in ALLOWED:
        return "not a drill command (cockpit keys | enroll | revoke | ledger)"
    for a in argv:
        for token in ([a] if not a.startswith("-") else [a.partition("=")[2]]):
            if token and _is_pathy(token) and not _inside(token, scratch):
                return f"a path argument is outside the scratch directory: {token}"
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

    os.environ.pop("SSH_AUTH_SOCK", None)
    launch.allowed_env = allowed_env
    try:
        return cli.main(argv)
    finally:
        launch.allowed_env = original


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
