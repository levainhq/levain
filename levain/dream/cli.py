"""levain.dream.cli — ``levain dream`` (BUILD_PLAN r7 R-16; D1_EXECUTOR §11).

D1-1a fills in ``run`` only: ``levain dream run --store-dir <copy> --mind claude|ollama`` consolidates a COPY of a store
through :func:`levain.dream.executor.consolidate_store`. Bare ``levain dream`` still says it is not built (the schedule,
``ready``, ``rollback`` and ``recover`` are D1-1b/c and D1-2's). ``run`` refuses the operator's live store
(``~/.anneal-memory``): it is for copies, and it is never pointed at that dir by default."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("dream", help="Dreaming (only `run` on a store copy exists in this release).",
                              allow_abbrev=False,
                              description="Consolidate a store through a compose mind. Only `run` on a COPY of a "
                                          "store is built; nothing is scheduled.")
    p.set_defaults(func=run_dream)
    sub = p.add_subparsers(dest="dream_command")
    r = sub.add_parser("run", allow_abbrev=False, help="Consolidate a store COPY once.",
                       description="One consolidate on a store copy: lock, prepare, compose (checked on a scratch "
                                   "copy, up to 3 composes), save. Writes usage.json and progress.json under the "
                                   "occurrence dir. The claude mind spends real money; the ollama mind sends the "
                                   "episodes to its endpoint.")
    r.add_argument("--store-dir", required=True, help="A directory holding memory.db (or .levain/memory.db): a COPY.")
    r.add_argument("--mind", required=True, choices=("claude", "ollama"))
    r.add_argument("--model", default=None, help="claude: the pinned model id (must have a price row); "
                                                 "ollama: the compose model.")
    r.add_argument("--claude-bin", default="claude", help="The claude executable (tests point this at a stub).")
    r.add_argument("--base-url", default="http://localhost:11434", help="ollama: the endpoint.")
    r.add_argument("--api-key-env", default=None, help="ollama: the NAME of the env var holding the key.")
    r.add_argument("--instructions", default=None, help="A file of consolidate instructions (else anneal's package).")
    r.add_argument("--max-seconds", type=float, default=None, help="Wall bound (default 3000; 0 = unbounded).")
    r.add_argument("--occurrence-dir", default=None, help="Where progress.json and usage.json go "
                                                          "(default <store dir>/dream/<new id>).")
    r.add_argument("--dry-run", action="store_true", help="Prepare and print the package, compose nothing.")
    r.add_argument("--json", action="store_true", help="Print the typed result as JSON on stdout.")
    r.set_defaults(func=run_dream_run)


def run_dream(args: argparse.Namespace) -> int:
    print("levain dream: not built in this release (only `levain dream run --store-dir <copy>` exists)",
          file=sys.stderr)
    return 1


def _resolve_store(store_dir: str) -> tuple[Path | None, str]:
    from levain.firing.isolation import _is_within_ci, flow_store_dir

    base = Path(store_dir).expanduser().resolve()
    if _is_within_ci(base, flow_store_dir().resolve()):
        return None, (f"{base} is inside the operator's live store (~/.anneal-memory): `levain dream run` is for a "
                      "COPY. Refused.")
    for cand in (base / "memory.db", base / ".levain" / "memory.db"):
        if cand.is_file():
            return cand, ""
    return None, f"no memory.db under {base} (looked in the dir and in .levain/)."


def run_dream_run(args: argparse.Namespace) -> int:
    from levain import wrap as _w
    from levain.dream import executor as ex

    store_path, err = _resolve_store(args.store_dir)
    if store_path is None:
        print(f"levain dream run: {err}", file=sys.stderr)
        return 2
    if args.mind == "claude":
        model = args.model or ex.DEFAULT_CLAUDE_MODEL
        mind: object = ex.ClaudeMind(model, claude_bin=args.claude_bin)
        budget: ex.Budget | None = ex.Budget.for_model(model)
        if budget.m_usd_micros is None:
            print(f"levain dream run: no price row for {model!r} in levain.dream.executor.PRICE_TABLE, so the "
                  "one-call ceiling M is unknown and the Claude mind refuses to compose.", file=sys.stderr)
            return 2
    else:
        key = os.environ.get(args.api_key_env) if args.api_key_env else None
        mind = ex.OllamaMind(args.model or _w.DEFAULT_COMPOSE_MODEL, base_url=args.base_url, api_key=key)
        budget = None
    occ = Path(args.occurrence_dir).expanduser() if args.occurrence_dir else \
        store_path.parent / "dream" / time.strftime("%Y%m%dT%H%M%S")
    max_seconds = ex.DREAM_BUDGET_S if args.max_seconds is None else (args.max_seconds or None)
    result = ex.consolidate_store(
        store_path, mind=mind, composer=getattr(mind, "model", None), max_seconds=max_seconds, budget=budget,
        instructions_path=args.instructions, occurrence_dir=occ, dry_run=args.dry_run,
        hard_exit_code=_w.EXIT_TIMEOUT, label="levain dream run")
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True, default=str))
    return result.exit_code
