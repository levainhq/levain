"""levain.dream.cli — ``levain dream``: an empty stub (BUILD_PLAN r7 R-16). The root parser exists so that D1-1a need not
edit ``levain/cli.py``; the command does nothing yet and exits 1."""
from __future__ import annotations

import argparse
import sys


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("dream", help="Dreaming (not built in this release).", allow_abbrev=False,
                              description="Reserved. Nothing is built behind this command yet.")
    p.set_defaults(func=run_dream)


def run_dream(args: argparse.Namespace) -> int:
    print("levain dream: not built in this release", file=sys.stderr)
    return 1
