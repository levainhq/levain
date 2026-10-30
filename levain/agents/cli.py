"""levain.agents.cli — ``levain agents``: the seam (A0 r6 §3.7). It opens the agent store through
:func:`levain.agents.store.open_agent_store` and says whether the agent schema is the one this levain writes. The
verbs of §3.6 arrive with their slices; none exists here.

Exit 0: the agent schema is this levain's. Exit 1: refused, the reason on stderr.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from levain.agents.store import AgentStoreRefused, open_agent_store
from levain.autonomic.agent_tables import AGENT_SCHEMA
from levain.autonomic.db import StoreFormatError


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "agents", help="Named agents on the autonomic store (the store check).", allow_abbrev=False,
        description="Open the autonomic store's agent tables and check them against this levain's schema. "
                    "Refuses a /3 store, and a store whose agent tables are not the ones this levain writes.")
    p.add_argument("--store-dir", dest="store_dir", type=Path, default=None,
                   help="The autonomic store directory (default: $LEVAIN_HOME/autonomic).")
    p.set_defaults(func=run_agents)


def run_agents(args: argparse.Namespace) -> int:
    try:
        store = open_agent_store(args.store_dir)
    except AgentStoreRefused as exc:
        print(f"levain agents: {exc}", file=sys.stderr)
        return 1
    except (StoreFormatError, sqlite3.Error, OSError) as exc:
        print(f"levain agents: cannot open the autonomic store: {exc}", file=sys.stderr)
        return 1
    print(f"agent schema {AGENT_SCHEMA}: ok ({store.path})")
    return 0
