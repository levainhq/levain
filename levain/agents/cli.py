"""levain.agents.cli — ``levain agents``: the seam (A0 r6 §3.7). It opens the agent store through
:func:`levain.agents.store.open_agent_store` and says whether the agent schema is the one this levain writes. The
verbs of §3.6 arrive with their slices; none exists here.

Exit 0: the agent schema is this levain's. Exit 1: refused, the reason on stderr.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from levain.agents.role import RoleError, add_role, get_role, parse_role_text
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
    verbs = p.add_subparsers(dest="agents_verb", metavar="VERB")
    role = verbs.add_parser("role", help="The role registry (P1): add, show.", allow_abbrev=False,
                            description="Roles are content-hashed and versioned. Give --store-dir before `role`.")
    rverbs = role.add_subparsers(dest="role_verb", metavar="VERB", required=True)
    add = rverbs.add_parser("add", help="Register a role from a JSON file; unknown fields are refused.",
                            allow_abbrev=False)
    add.add_argument("file", type=Path, help="The role file (JSON).")
    add.set_defaults(func=run_role_add)
    show = rverbs.add_parser("show", help="Print a role, its version and its role_hash.", allow_abbrev=False)
    show.add_argument("name", help="The role name.")
    show.add_argument("--version", type=int, default=None, help="A version (default: the latest).")
    show.set_defaults(func=run_role_show)


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


def _open(args: argparse.Namespace):
    try:
        return open_agent_store(args.store_dir)
    except AgentStoreRefused as exc:
        print(f"levain agents: {exc}", file=sys.stderr)
    except (StoreFormatError, sqlite3.Error, OSError) as exc:
        print(f"levain agents: cannot open the autonomic store: {exc}", file=sys.stderr)
    return None


def run_role_add(args: argparse.Namespace) -> int:
    try:
        role = parse_role_text(args.file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"levain agents role add: cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    except RoleError as exc:
        print(f"levain agents role add: {args.file}: {exc}", file=sys.stderr)
        return 1
    store = _open(args)
    if store is None:
        return 1
    try:
        name, digest, version, created = add_role(store, role)
    except sqlite3.Error as exc:
        print(f"levain agents role add: the store refused it: {exc}", file=sys.stderr)
        return 1
    print(f"role {name} v{version} {'added' if created else 'already registered'}: {digest}")
    return 0


def run_role_show(args: argparse.Namespace) -> int:
    store = _open(args)
    if store is None:
        return 1
    try:
        digest, version, role = get_role(store, args.name, args.version)
    except RoleError as exc:
        print(f"levain agents role show: {exc}", file=sys.stderr)
        return 1
    print(f"role {args.name} v{version} {digest}")
    print(json.dumps(role, indent=2, sort_keys=True, ensure_ascii=False))
    return 0
