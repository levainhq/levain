#!/usr/bin/env python3
"""Run the levain CLI for the TERMSIGN recovery drill (the scratch-root drill in the enrolment runbook, Part B).

The CLI's child environment strips ``SSH_ASKPASS`` and ``SSH_ASKPASS_REQUIRE`` (``levain.launch.allowed_env``), so
a drill whose software keys carry a passphrase can never get that passphrase to ``ssh-keygen`` through the real
entry point. This wrapper lets exactly those two names through for this one process, then calls ``levain.cli.main``.

It calls ``cli.main`` directly, so it skips ``launch.main()``'s ``reexec_if_needed()`` and ``set_not_dumpable()``:
a drill tool, never the way to run levain for real.

It never reads, prints or logs a passphrase: the passphrase lives only in the askpass script the operator wrote,
which ``ssh-keygen`` runs. Refuses to run unless HOME has been redirected away from the account's real home, so the
real ``~/.levain`` and ``~/.ssh`` are never read.

Usage (from a checkout, or with PYTHONPATH naming one): ``python3 scripts/termsign_drill_cli.py cockpit keys --data-root <scratch>``
"""
from __future__ import annotations

import os
import pwd
import sys

PASS_THROUGH = ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")


def _real_home() -> str:
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def main(argv: list[str]) -> int:
    home = os.environ.get("HOME", "")
    if not home or os.path.realpath(home) == _real_home():
        print("termsign_drill_cli: HOME is the real home; set HOME to a scratch directory first", file=sys.stderr)
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
