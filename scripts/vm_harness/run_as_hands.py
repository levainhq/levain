"""Run a command AS an entity's hands user (leg 6 of the hands chat launcher's live run).

    python3 -I scripts/vm_harness/run_as_hands.py --path <entity> [--env K=V ...] [--cwd DIR] [--print] -- CMD [ARG ...]

Harness code, not product code. The hands user comes from ``<entity>/.levain/confinement.json`` through the product's
own loader (``hands_login.load_hands_identity``), which refuses when the entity has no hands user, when its account is
gone, or when its uid changed. The command runs by the sudoers rule ``levain setup-isolation`` wrote
(``<operator> ALL=(<hands>) NOPASSWD: ALL``), in the product's own shape (``confinement.hands_prefix``):

    sudo -n -u <H> /usr/bin/env -i [K=V ...] HOME=<home> PATH=<HANDS_PATH> LANG=.. TERM=dumb USER=<H> LOGNAME=<H> CMD ARG ...

so the command sees nothing of the operator's environment and the hands PATH only (never a directory under the
operator's home). ``--env K=V`` adds assignments right after the ``-i`` (for example ``CODEX_HOME=<home>/.codex``). The working
directory is the hands workspace unless ``--cwd`` names another. ``--print`` prints the argv and runs nothing. ``sudo -n``
never prompts: without the rule it fails and says so. Exits with the command's own status."""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_FORBIDDEN = frozenset({"HOME", "PATH", "USER", "LOGNAME"})      # the product fixes these; an override is a refusal


class RunAsHandsRefused(Exception):
    pass


def build_argv(hands, extra_env: list[str], command: list[str]) -> list[str]:
    """The sudo argv for ``command`` as ``hands`` (a ``HandsIdentity``): ``extra_env`` right after ``env -i`` (before
    the product's own assignments, which therefore win, and before a Linux start program, which would otherwise take
    the first one as the command to exec: leg 6 L3 r1, codex MED), then the rest of the product's prefix, then the
    command. Refused: a command whose first word ``env`` would read as an assignment (it holds ``=``) or that is empty
    (gemini MED: ``PATH=/x sh`` set PATH past the fixed ones); an ``--env`` whose name is not a plain name, names a
    fixed variable, or whose value holds a control character."""
    from levain.firing.confinement import hands_prefix
    if not command:
        raise RunAsHandsRefused("no command given")
    if not command[0] or "=" in command[0]:
        raise RunAsHandsRefused(f"the command {command[0][:40]!r} is empty or holds '=', which env would read as an "
                                "assignment; pass variables with --env NAME=VALUE")
    for a in extra_env:
        if not _ENV_ASSIGN.match(a):
            raise RunAsHandsRefused(f"--env wants NAME=VALUE, not {a[:40]!r}")
        name, value = a.split("=", 1)
        if name in _FORBIDDEN:
            raise RunAsHandsRefused(f"--env {name} is fixed by the product (the hands home and PATH)")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
            raise RunAsHandsRefused(f"--env {name}: the value holds a control character")
    base = hands_prefix(hands)
    i = base.index("-i") + 1
    return [*base[:i], *extra_env, *base[i:], *command]


def hands_of(entity: Path):
    """The entity's hands identity, or a refusal (no hands user recorded, the account gone, its uid changed)."""
    from levain.firing import hands_login
    try:
        return hands_login.load_hands_identity(entity)
    except hands_login.HandsRefused as exc:
        raise RunAsHandsRefused(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_as_hands.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--path", type=Path, required=True, help="The entity directory.")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="An extra variable for the command.")
    p.add_argument("--cwd", type=Path, default=None, help="Working directory (default: the hands workspace).")
    p.add_argument("--print", dest="show", action="store_true", help="Print the argv and run nothing.")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD [ARG ...]")
    a = p.parse_args(argv)
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    try:
        hands = hands_of(a.path.expanduser())
        full = build_argv(hands, a.env, command)
    except RunAsHandsRefused as exc:
        print(f"run_as_hands: {exc}", file=sys.stderr)
        return 2
    if a.show:
        print(shlex.join(full))
        return 0
    from levain.launch import child_env
    cwd = str(a.cwd) if a.cwd is not None else str(hands.workspace)
    return subprocess.run(full, cwd=cwd, env=child_env()).returncode


if __name__ == "__main__":
    sys.exit(main())
