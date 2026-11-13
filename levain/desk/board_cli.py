"""levain.desk.board_cli — ``levain board state``: the board's derived state, printed from git evidence (READY_SCHEMA v2).

``levain board state --json [--items PATH] [--evidence-extra PATH] [--repo PATH] [--now ISO]`` folds the item store,
reads git itself (:mod:`levain.desk.board_evidence`), merges the evidence only FLOW can supply (``seats``, ``band``,
``waiters``, ``documents`` for paths this process cannot stat) from ``--evidence-extra``, and prints the
:class:`~levain.desk.board_state.Board` as JSON. It is a read: it never writes a ref, a note, a file or the items store.

Exit codes: 0 the audit is empty; 3 the audit holds an entry (the pace gate and the desk fail closed on it); 2 the items
file (or an explicit ``--evidence-extra``/``--now``) is unreadable or unusable. Git that cannot be read is NOT exit 2:
every source it feeds reads unreadable, every item reads unknown, the audit says so, exit 3.

The JSON is the board (``schema``, ``as_of``, ``items``, ``audit``, ``unattributed``, ``problems``) plus one additive key,
``reader``: ``{repo, since, main_ref, <counts>}``, for a human checking what was read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk import board_evidence as be
from levain.desk.board_state import Evidence, derive_all, fold, parse_timestamp

__all__ = ["ITEMS_ENV", "READ_BY_CLI", "register", "run_state"]

ITEMS_ENV = "LEVAIN_BOARD_ITEMS"
#: Sources this CLI reads from git (or by stat). An extra object may not name them: two writers for one source is how
#: a stale hand-made file would silently outvote git.
READ_BY_CLI = ("fetched_at", "main", "chain", "refs", "merged", "branches", "unattributed")
EXIT_AUDIT = 3
EXIT_USAGE = 2


def register(subparsers: Any) -> None:
    # Hidden, as ``levain upgrade`` and ``levain session`` are (no ``help=``: ``levain --help`` and the README's "All
    # commands" table, which tests/test_readme_commands.py holds to the listed verbs, leave it out); ``levain board state
    # --help`` still works. Give it a ``help=`` and a README row together when it is made public.
    board = subparsers.add_parser("board", description="The work board. Item state is never stored; it is derived on every read.")
    sub = board.add_subparsers(dest="board_cmd", required=True)
    p = sub.add_parser(
        "state", help="Print the board's derived state (READY_SCHEMA v2).",
        description=("Fold the item store, read git, and print the derived state of every item. Reads only. Exit 0 when the "
                     "audit is empty, 3 when it is not, 2 when the items file or an explicit option is unusable."))
    p.add_argument("--json", action="store_true", help="Print the Board as JSON (the contract flow's readers consume).")
    p.add_argument("--items", type=Path, metavar="PATH",
                   help=f"The items store (items.jsonl). Default: ${ITEMS_ENV}; one of the two is required.")
    p.add_argument("--evidence-extra", dest="evidence_extra", type=Path, metavar="PATH",
                   help="A JSON object with the evidence flow supplies (seats, band, waiters, documents, unreadable). "
                        f"It may not name a source this command reads itself: {', '.join(READ_BY_CLI)}.")
    p.add_argument("--repo", type=Path, default=Path("."), metavar="PATH", help="The repository to read (default: here).")
    p.add_argument("--repo-name", dest="repo_name", metavar="NAME",
                   help="The repo's name in item ids, <name>#<n> (default: the directory that holds the repository's .git).")
    p.add_argument("--since", default=be.BOARD_EPOCH, metavar="ISO",
                   help=f"Commits and branches older than this are out of the attribution scan (default {be.BOARD_EPOCH}).")
    p.add_argument("--now", metavar="ISO", help="Judge the board at this instant (default: the clock). Zone-aware or UTC.")
    p.set_defaults(func=run_state)


def _fail(message: str) -> int:
    print(f"levain board state: {message}", file=sys.stderr)
    return EXIT_USAGE


def _merge_extra(evidence: dict[str, Any], extra: Any) -> str | None:
    """Fold the extra object into ``evidence``; the reason it cannot be used, or None."""
    if not isinstance(extra, dict):
        return "the extra evidence must be a JSON object"
    clash = sorted(k for k in extra if k in READ_BY_CLI)
    if clash:
        return f"the extra evidence names {', '.join(clash)}, which this command reads itself"
    for key, value in extra.items():
        if key == "evidence_v":
            if value != 1:
                return f"the extra evidence_v is {value!r}, not 1"
        elif key == "unreadable":
            if not isinstance(value, dict):
                return "the extra unreadable must be an object of source to reason"
            for src, why in value.items():
                evidence["unreadable"].setdefault(src, why)
        elif key == "documents":
            if not isinstance(value, dict):
                return "the extra documents must be an object of path to boolean"
            evidence["documents"] = {**value, **evidence.get("documents", {})}      # a path this process stat'd wins
        else:
            evidence[key] = value
    return None


def run_state(args: argparse.Namespace) -> int:
    items_path = args.items or (Path(os.environ[ITEMS_ENV]) if os.environ.get(ITEMS_ENV) else None)
    if items_path is None:
        return _fail(f"no items store: pass --items PATH or set {ITEMS_ENV}")
    try:
        lines = Path(items_path).expanduser().read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"the items file {items_path} cannot be read ({type(exc).__name__}: {exc})")
    now = datetime.now(timezone.utc) if args.now is None else parse_timestamp(args.now)
    if now is None:
        return _fail(f"--now {args.now!r} is not an ISO-8601 timestamp")
    folded = fold(lines)
    repo = Path(args.repo).expanduser()
    evidence, summary = be.read_git_evidence(repo, repo_name=args.repo_name, since=args.since)
    evidence["documents"] = be.read_documents(folded.items, repo)
    if args.evidence_extra is not None:
        try:
            extra = json.loads(Path(args.evidence_extra).expanduser().read_text("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            return _fail(f"--evidence-extra {args.evidence_extra} cannot be read ({type(exc).__name__}: {exc})")
        bad = _merge_extra(evidence, extra)
        if bad is not None:
            return _fail(f"--evidence-extra {args.evidence_extra}: {bad}")
    board = derive_all(folded, Evidence.from_dict(evidence), now=now)
    doc = board.to_dict()
    doc["reader"] = summary
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=False))
    else:
        _print_table(doc)
    return EXIT_AUDIT if board.audit else 0


def _print_table(doc: dict[str, Any]) -> None:
    print(f"board {doc['schema']} as of {doc['as_of'] or 'unknown'}: {len(doc['items'])} items, {len(doc['audit'])} audit entries")
    for s in doc["items"]:
        print(f"  {s['id']:<14} {s['state']:<18} {s['reason']}")
    for a in doc["audit"]:
        print(f"  audit {a['kind']}: {a['id'] or '-'}: {a['why']}")
    for name in doc.get("reader", {}).get("diverged", []):
        print(f"  diverged: {name}: the local head and origin's copy each hold commits the other lacks (ahead is the union)")
