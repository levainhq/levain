#!/usr/bin/env python3
"""Run the levain CLI for the TERMSIGN recovery drill (the scratch-root drill in the enrolment runbook, Part B).

The CLI's child environment strips ``SSH_ASKPASS`` and ``SSH_ASKPASS_REQUIRE`` (``levain.launch.allowed_env``), so
a drill whose software keys carry a passphrase can never get that passphrase to ``ssh-keygen`` through the real
entry point. This wrapper lets exactly those two names through for this one process, then calls ``levain.cli.main``.

It calls ``cli.main`` directly, so it skips ``launch.main()``'s ``reexec_if_needed()`` and ``set_not_dumpable()``:
a drill tool, never the way to run levain for real.

It never reads, prints or logs a passphrase: the passphrase lives only in the askpass script the operator wrote,
which ``ssh-keygen`` runs. It refuses to run unless HOME is a scratch directory under the system temp dir, LEVAIN_HOME
(if set) is inside it, and no argument names a path inside the real home; that keeps the real ``~/.levain`` and
``~/.ssh`` out of the drill's reach.

Usage (from a checkout, or with PYTHONPATH naming one): ``python3 scripts/termsign_drill_cli.py cockpit keys --data-root <scratch>``
"""
from __future__ import annotations

import os
import pwd
import sys
import tempfile

PASS_THROUGH = ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")


def _real_home() -> str:
    try:
        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:                       # no passwd entry: fall back to the process's own idea of ~
        return os.path.realpath(os.path.expanduser("~"))


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _refusal(argv: list[str]) -> str | None:
    """Why this run would not be confined to a scratch directory, or None. HOME must be a directory inside the
    system temp dir (the runbook's ``mktemp -d``) and not the real home; ``LEVAIN_HOME``, if set, must be inside
    HOME; and no argument may name a path inside the real home (a shell-expanded ``~/.ssh/...``)."""
    home, real = os.environ.get("HOME", ""), _real_home()
    if not home or _inside(home, real) or real == os.path.realpath(home):
        return "HOME is the real home (or unset); set HOME to a scratch directory first"
    if not _inside(home, tempfile.gettempdir()) or os.path.realpath(home) == os.path.realpath(tempfile.gettempdir()):
        return "HOME is not a scratch directory inside the system temp dir (use S=$(mktemp -d); HOME=$S/home)"
    lh = os.environ.get("LEVAIN_HOME")
    if lh and not _inside(lh, home):
        return "LEVAIN_HOME points outside the scratch HOME; unset it"
    for a in argv:
        if a.startswith("/") and _inside(a, real):
            return f"an argument names a path inside the real home ({a}); the drill uses scratch keys only"
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

    launch.allowed_env = allowed_env
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
