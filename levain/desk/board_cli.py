"""levain.desk.board_cli — ``levain board state``: the board's derived state, printed from git evidence (READY_SCHEMA v2).

``levain board state --json [--items PATH] [--evidence-extra PATH] [--repo PATH] [--now ISO]`` folds the item store,
reads git itself (:mod:`levain.desk.board_evidence`), adds the producer blocks for ``seats``, ``band`` and ``waiters`` (Levain's
own, :mod:`levain.desk.board_producers`; external ones named by ``--producer``; flow's from ``--evidence-extra``, which also
supplies ``documents`` for paths this process cannot stat), and prints the
:class:`~levain.desk.board_state.Board` as JSON. It is a read: it never writes a ref, a note, a file or the items store.

Exit codes: 0 the audit is empty; 3 the audit holds an entry (the pace gate and the desk fail closed on it); 2 the items
file (or an explicit ``--evidence-extra``/``--now``) is unreadable or unusable. Git that cannot be read is NOT exit 2:
every source it feeds reads unreadable, every item reads unknown, the audit says so, exit 3.

The JSON is the board (``schema``, ``as_of``, ``items``, ``audit``, ``unattributed``, ``problems``) plus one additive key,
``reader``: ``{repo, since, main_ref, <counts>}`` (every field on every path, a failure board included), for a human
checking what was read. ``--evidence-extra`` may carry only :data:`EXTRA_KEYS`; anything else is exit 2.
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
from levain.desk import board_store as bs
from levain.desk import board_producers as bp
from levain.desk.board_state import (EVIDENCE_V, FLAT_EVIDENCE_V, FLAT_PRODUCER, PRODUCER_SOURCES, Evidence, derive_all, fold,
                                      parse_timestamp)

__all__ = ["EXTRA_KEYS", "ITEMS_ENV", "READ_BY_CLI", "register", "run_state"]

ITEMS_ENV = bs.ITEMS_ENV
#: Sources this CLI reads from git (or by stat). An extra object may not name them: two writers for one source is how
#: a stale hand-made file would silently outvote git.
READ_BY_CLI = ("fetched_at", "main", "chain", "refs", "merged", "branches", "unattributed")
#: The keys an extra object may carry: the sources only FLOW can supply, the ones this process cannot stat, and the version.
EXTRA_KEYS = ("evidence_v", *PRODUCER_SOURCES, "documents", "unreadable")
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
    p.add_argument("--items", type=str, metavar="PATH",
                   help=f"The items store (items.jsonl). Default: ${ITEMS_ENV}; one of the two is required.")
    p.add_argument("--evidence-extra", dest="evidence_extra", type=Path, metavar="PATH",
                   help="A JSON object with the evidence flow supplies (seats, band, waiters, documents, unreadable). "
                        f"It may not name a source this command reads itself: {', '.join(READ_BY_CLI)}.")
    p.add_argument("--producer", dest="producers", action="append", default=[], metavar="NAME",
                   help="An external evidence producer to expect (repeatable): its blocks are read from evidence.NAME.json "
                        "beside the items file. Levain's own reads of seats and band are always expected.")
    p.add_argument("--no-land-waiter", dest="no_land_waiter", action="store_true",
                   help="This install runs no land waiter: Levain then declares waiters absent. Without it, waiters are "
                        "unknown until a producer that runs them supplies them.")
    p.add_argument("--repo", type=Path, default=Path("."), metavar="PATH", help="The repository to read (default: here).")
    p.add_argument("--repo-name", dest="repo_name", metavar="NAME",
                   help="The repo's name in item ids, <name>#<n> (default: the directory that holds the repository's .git).")
    p.add_argument("--also-repo", dest="also_repo", action="append", default=[], metavar="[NAME=]PATH",
                   help=("Another repository whose items are on the board (repeatable). Each item, and each dependency it waits on, "
                         "is then judged from the evidence of the repo it belongs to; an item of a repo not read reads unknown. "
                         "The repo's name is its directory's name unless NAME=PATH says it (a worktree or a clone under another "
                         "directory name needs NAME=PATH, e.g. levain=/tmp/levain-review). --evidence-extra's documents apply "
                         "to the primary --repo only."))
    p.add_argument("--since", default=be.BOARD_EPOCH, metavar="ISO",
                   help=f"Commits and branches older than this are out of the attribution scan (default {be.BOARD_EPOCH}).")
    p.add_argument("--now", metavar="ISO", help="Judge the board at this instant (default: the clock). Zone-aware or UTC.")
    p.set_defaults(func=run_state)

    items_help = f"An explicit store (its items file or its directory); it must be this install's store. Default: ${ITEMS_ENV}."
    a = sub.add_parser("add", description="Capture an item; prints its id.")
    a.add_argument("--repo", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--origin", required=True, help="desk N | np-xxxxxxxx | STATUS_1_0.md#<n>@<blob sha> | found by <seat>, <path>")
    a.add_argument("--words", default="", help="his words verbatim when he gave them")
    a.add_argument("--done", default=None)
    a.add_argument("--done-kind", dest="done_kind", default="merged", choices=bs.DONE_KINDS)
    a.add_argument("--tier", default=None, choices=bs.TIERS)
    a.add_argument("--depends", action="append", default=[], metavar="TYPED")
    a.add_argument("--by", required=True)
    a.add_argument("--key", default=None, help="An idempotency key: the same key and --payload-hash returns the existing id.")
    a.add_argument("--payload-hash", dest="payload_hash", default=None, metavar="SHA256")
    a.add_argument("--items", type=str, metavar="PATH", help=items_help)
    a.set_defaults(func=run_add)
    m = sub.add_parser("amend", description="Replace fields of an item.")
    m.add_argument("--id", required=True)
    m.add_argument("--why", required=True)
    m.add_argument("--by", required=True)
    m.add_argument("--title")
    m.add_argument("--words")
    m.add_argument("--done")
    m.add_argument("--done-kind", dest="done_kind", choices=bs.DONE_KINDS)
    m.add_argument("--tier", choices=bs.TIERS)
    m.add_argument("--depends", action="append", default=None, metavar="TYPED", help="replaces the item's depends_on")
    m.add_argument("--clear", action="append", default=[], choices=bs.CLEARABLE)
    m.add_argument("--items", type=str, metavar="PATH", help=items_help)
    m.set_defaults(func=run_amend)
    d = sub.add_parser("decide", description="Record a ruling on an item.")
    d.add_argument("--id", required=True)
    d.add_argument("--decision", required=True, choices=bs.DECISIONS)
    d.add_argument("--words", required=True)
    d.add_argument("--by", required=True)
    d.add_argument("--date", default=None)
    d.add_argument("--items", type=str, metavar="PATH", help=items_help)
    d.set_defaults(func=run_decide)
    u = sub.add_parser("use", description="Make DIR this install's item store (created if needed); writes the pointer last.")
    u.add_argument("dir", type=Path, metavar="DIR")
    u.set_defaults(func=run_use)


def _fail(message: str) -> int:
    print(f"levain board state: {message}", file=sys.stderr)
    return EXIT_USAGE


def _split_also(raw: str) -> tuple[str | None, Path]:
    """``NAME=PATH`` -> (NAME, PATH); a bare PATH -> (None, PATH) and keeps its basename. NAME is a plain repo name
    (letters, digits, ``._-``); anything else with an ``=`` is a path."""
    name, sep, rest = raw.partition("=")
    if sep and name and rest and all(c.isalnum() or c in "._-" for c in name):
        return name, Path(rest).expanduser()
    return None, Path(raw).expanduser()


def _merge_extra(evidence: dict[str, Any], extra: Any, *, primary: bool = True, captured_at: str | None = None) -> str | None:
    """Fold the extra object into ``evidence``; the reason it cannot be used, or None. Only the keys flow supplies are
    accepted (``EXTRA_KEYS``): an unknown key would land in the evidence object unexamined, and a reason that is not a
    string would crash whatever reads it later. The flat form (``evidence_v`` 1, or none) becomes ONE block per source from
    :data:`FLAT_PRODUCER`, captured at ``captured_at`` (default: the evidence's ``fetched_at``); the blocks form
    (``evidence_v`` 2) is a list of producer blocks per source, appended as given."""
    if not isinstance(extra, dict):
        return "the extra evidence must be a JSON object"
    clash = sorted(k for k in extra if k in READ_BY_CLI)
    if clash:
        return f"the extra evidence names {', '.join(clash)}, which this command reads itself"
    unknown = sorted(str(k) for k in extra if k not in EXTRA_KEYS)
    if unknown:
        return f"the extra evidence names {', '.join(unknown)}, which flow does not supply (the keys are {', '.join(EXTRA_KEYS)})"
    form = extra.get("evidence_v", FLAT_EVIDENCE_V)
    if type(form) is not int or form not in (FLAT_EVIDENCE_V, EVIDENCE_V):
        return f"the extra evidence_v is {form!r}, not {FLAT_EVIDENCE_V} or {EVIDENCE_V}"
    when = captured_at if captured_at is not None else evidence.get("fetched_at")
    names: list[str] = evidence.setdefault("producers", [])
    if form == FLAT_EVIDENCE_V and FLAT_PRODUCER not in names:
        names.append(FLAT_PRODUCER)
    for key, value in extra.items():
        if key == "evidence_v":
            continue
        if key in PRODUCER_SOURCES:
            if form == EVIDENCE_V:
                if not isinstance(value, list) or not all(isinstance(b, dict) and "form" not in b for b in value):
                    return f"the extra {key} must be a list of producer blocks (objects with no form key)"
                evidence.setdefault(key, []).extend(value)
                names.extend(p for p in (b.get("producer") for b in value) if isinstance(p, str) and p and p not in names)
            else:
                evidence.setdefault(key, []).append({"producer": FLAT_PRODUCER, "form": "flat", "store_id": None,
                                                     "captured_at": when, "complete": True, "rows": value})
        elif key == "unreadable":
            if not isinstance(value, dict):
                return "the extra unreadable must be an object of source to reason"
            for src, why in value.items():
                if src in READ_BY_CLI:                  # the nested form of the clash above: a stale file must not outvote git
                    return f"the extra unreadable names {src}, which this command reads itself"
                if not isinstance(why, str) or not why.strip():
                    return f"the extra unreadable[{src}] must be a string that says why"
                evidence["unreadable"].setdefault(src, why)
        elif key == "documents":
            if not isinstance(value, dict):
                return "the extra documents must be an object of path to boolean"
            if primary:                 # a path is repo-relative: another repo's same path is not what flow staged
                evidence["documents"] = {**value, **evidence.get("documents", {})}      # a path this process stat'd wins
    return None


def _store_id(store_dir: Path) -> str | None:
    """The store's id (``<store dir>/store.id``), or None when it has none yet."""
    try:
        return (store_dir / "store.id").read_text("utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _attach_producers(evidence: dict[str, Any], native: dict[str, dict[str, Any]], external: dict[str, list[dict[str, Any]]],
                      external_names: list[str], failures: dict[str, str]) -> None:
    """Put the native and the external producers' blocks in front of whatever the extra supplied, and name the producers
    expected per source: Levain's own for seats and band (and for waiters only when it declared them absent), every
    external one named, and flow when its flat extra was given."""
    named = [p for p in evidence.get("producers", []) if p != bp.NATIVE_PRODUCER]
    for source in PRODUCER_SOURCES:
        evidence[source] = [*([native[source]] if source in native else []), *external.get(source, []), *evidence.get(source, [])]
    base = [*external_names, *[p for p in named if p not in external_names]]
    evidence["producers"] = {s: [*([bp.NATIVE_PRODUCER] if s in native else []), *base] for s in PRODUCER_SOURCES}
    if failures:
        evidence["producer_failures"] = failures


def _refused(exc: Exception) -> int:
    print(f"levain board: refused: {exc}", file=sys.stderr)
    return EXIT_USAGE


def _warn_unknown_deps(target: bs.Target, depends: Any) -> None:
    """Advisory only: a failure to check says so in one line and the write goes on (the write judges its own inputs)."""
    try:
        refs = [d["ref"] for d in (bs.parse_link(x) for x in depends) if d["kind"] == "item"]
        if refs:
            held = bs.known_ids(target)
            unknown = [r for r in refs if r not in held]
            if unknown:
                print(f"board: depends_on names {', '.join(unknown)}, which the store does not hold yet; the board will read it unknown until it does.",
                      file=sys.stderr)
    except (bs.BoardError, OSError) as exc:
        print(f"board: could not check depends_on against the store ({exc}); writing anyway.", file=sys.stderr)


def run_add(args: argparse.Namespace) -> int:
    try:
        target = bs.resolve_store(args.items)
        for spec in args.depends:               # a malformed link is a refusal, not an advisory
            bs.parse_link(spec)
        if target.pointered:
            _warn_unknown_deps(target, args.depends)
        item_id, _ = bs.add(target, args.repo, args.title, args.origin, args.by, words=args.words, done=args.done,
                            done_kind=args.done_kind, tier=args.tier, depends=args.depends, key=args.key,
                            payload_hash=args.payload_hash)
        print(item_id)
    except bs.BoardError as exc:
        return _refused(exc)
    return 0


def run_amend(args: argparse.Namespace) -> int:
    try:
        target = bs.resolve_store(args.items)
        fields = {k: v for k, v in (("title", args.title), ("words", args.words), ("done", args.done),
                                    ("done_kind", args.done_kind), ("tier", args.tier)) if v is not None}
        if args.depends is not None:
            for spec in args.depends:
                bs.parse_link(spec)
            if target.pointered:
                _warn_unknown_deps(target, args.depends)
            fields["depends_on"] = list(args.depends)
        for c in dict.fromkeys(args.clear):          # a repeated --clear is one clear
            key = "depends_on" if c == "depends" else c
            if key in fields:
                raise bs.BoardError(f"--{c.replace('_', '-')} and --clear {c} name the same field; give one")
            fields[key] = [] if c == "depends" else None
        print("amended", bs.amend(target, args.id, args.why, args.by, fields))
    except bs.BoardError as exc:
        return _refused(exc)
    return 0


def run_decide(args: argparse.Namespace) -> int:
    try:
        target = bs.resolve_store(args.items)
        print("decided", bs.decide(target, args.id, args.decision, args.words, args.by, when=args.date))
    except bs.BoardError as exc:
        return _refused(exc)
    return 0


def run_use(args: argparse.Namespace) -> int:
    try:
        store_dir, changed = bs.use(args.dir)
    except bs.BoardError as exc:
        return _refused(exc)
    print(f"{'using' if changed else 'already using'} {store_dir}")
    return 0


def run_state(args: argparse.Namespace) -> int:
    try:
        target = bs.resolve_store(args.items)
    except bs.BoardError as exc:
        if str(exc).startswith("no items store"):
            return _fail(f"no items store: pass --items PATH or set {ITEMS_ENV}")
        return _refused(exc)
    given = args.items or os.environ.get(ITEMS_ENV)
    if not given and not target.pointered and not target.items_path.is_file():
        return _fail(f"no items store: pass --items PATH or set {ITEMS_ENV}, or run `levain board use`")
    try:
        lines = bs.read_item_lines(target)
    except (OSError, UnicodeDecodeError, bs.BoardError) as exc:
        return _fail(f"the items file {given or target.items_path} cannot be read ({type(exc).__name__}: {exc})")
    now = datetime.now(timezone.utc) if args.now is None else parse_timestamp(args.now)
    if now is None:
        return _fail(f"--now {args.now!r} is not an ISO-8601 timestamp")
    folded = fold(lines)
    repo = Path(args.repo).expanduser()
    evidence, summary = be.read_git_evidence(repo, repo_name=args.repo_name, since=args.since)
    cross = bool(args.also_repo)
    # One repo reads every item, as before; with --also-repo each repo reads the items that belong to it.
    mine = {i: it for i, it in folded.items.items() if it.repo == summary["repo"]} if cross else folded.items
    evidence["documents"] = be.read_documents(mine, repo)
    store_dir = target.store_dir
    store_id = _store_id(store_dir)
    native = bp.native_blocks(now=now, store_id=store_id, no_land_waiter=args.no_land_waiter)
    external, failures = bp.read_external(store_dir, args.producers)
    if store_id is None and any(isinstance(b, dict) and b.get("store_id") for bl in external.values() for b in bl):
        print(f"levain board state: the store {store_dir} has no id, so an external block naming one is taken as this store's", file=sys.stderr)
    extra = None
    if args.evidence_extra is not None:
        try:
            extra = json.loads(Path(args.evidence_extra).expanduser().read_text("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            return _fail(f"--evidence-extra {args.evidence_extra} cannot be read ({type(exc).__name__}: {exc})")
        bad = _merge_extra(evidence, extra)
        if bad is not None:
            return _fail(f"--evidence-extra {args.evidence_extra}: {bad}")
    flat_at = evidence.get("fetched_at")
    _attach_producers(evidence, native, external, list(args.producers), failures)
    primary = Evidence.from_dict(evidence, now=now, store_id=store_id)
    repo_evidence: dict[str, Evidence] | None = None
    also: list[dict[str, Any]] = []
    if cross:
        if not summary["repo"]:
            return _fail(f"--repo {repo}: its repo name is empty, so no item could be matched to it; pass --repo-name")
        repo_evidence = {summary["repo"]: primary}
        for raw in args.also_repo:
            given, other = _split_also(raw)
            ev2, sum2 = be.read_git_evidence(other, repo_name=given, since=args.since)
            name = sum2["repo"]
            if not name or name in repo_evidence:
                return _fail(f"--also-repo {other}: its repo name ({name!r}) is empty or already read; say it with NAME=PATH")
            ev2["documents"] = be.read_documents({i: it for i, it in folded.items.items() if it.repo == name}, other)
            if extra is not None:
                _merge_extra(ev2, extra, primary=False, captured_at=flat_at)     # the same flow-supplied sources; validated once above
            _attach_producers(ev2, native, external, list(args.producers), failures)
            repo_evidence[name] = Evidence.from_dict(ev2, now=now, store_id=store_id)
            also.append(sum2)
    board = derive_all(folded, primary, now=now, repo_evidence=repo_evidence,
                       primary_repo=summary["repo"] if cross else None)
    doc = board.to_dict()
    doc["reader"] = summary
    if also:
        doc["reader"] = {**summary, "also": also}
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
