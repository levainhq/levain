"""levain.launch — the console entry, which hardens the process before anything else is imported.

``pyproject.toml`` points ``levain`` here, and ``python -m levain`` comes here too, so every way of
starting the CLI passes through :func:`main`. :func:`levain.cli.main` stays importable and callable
on its own (the tests call it in-process), and does none of this.

What it does, in order (lane P2's research note, items 2a-2c):
  1. on Linux, marks the process not dumpable, the way ssh-agent and Chromium's setuid sandbox do;
  2. hands over to :func:`levain.cli.main`.
"""
from __future__ import annotations

import sys

_PR_SET_DUMPABLE = 4   # <linux/prctl.h>


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
    set_not_dumpable()
    from levain.cli import main as cli_main

    return cli_main()
