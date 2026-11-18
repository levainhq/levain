"""levain.desk.board_cli — ``levain board state``: the board's derived state, printed from git evidence (READY_SCHEMA v3).

``levain board state --json [--items PATH] [--evidence-extra PATH] [--repo PATH] [--now ISO]`` folds the item store,
reads git itself (:mod:`levain.desk.board_evidence`), adds the producer blocks for ``seats``, ``band`` and ``waiters`` (Levain's
own, :mod:`levain.desk.board_producers`; external ones named by ``--producer``; flow's from ``--evidence-extra``, which also
supplies ``documents`` for paths this process cannot stat), and prints the
:class:`~levain.desk.board_state.Board` as JSON. It is a read: it never writes a ref, a note, a file or the items store.

With no ``--repo`` (and no ``--also-repo``) on a store that has a registry, every repo the registry holds as ``tracked`` is read,
each by its own evidence. The board is only as fresh as its stalest repo (``as_of`` is the oldest fetch), every repo's audit lines
carry its name, and ``reader`` counts cover every repo read. A tracked repo that cannot be read, and a repo that is not tracked but
holds items, is named on the audit and its items read unknown; they are never dropped.

Exit codes: 0 the audit is empty; 3 the audit holds an entry (the pace gate and the desk fail closed on it); 2 the items
file (or an explicit ``--evidence-extra``/``--now``) is unreadable or unusable; 4 the store's lock was held by a writer past
``STATE_LOCK_WAIT`` (retry; nothing is wrong with the board). Git that cannot be read is NOT exit 2: every source it feeds reads
unreadable, every item reads unknown, the audit says so, exit 3. The whole-board read (no ``--repo``) REFUSES, exit 2, where it
cannot know which repos to read: a torn or unreadable registry (it fails closed, as the seat launch does), or no tracked repo
that can be read. The ``--repo`` read keeps a registry problem as an audit line.

Known edges. Git writes (a pull, a checkout) take no store lock, so the three store files can come from two commits. Git evidence
is read after the store snapshot, so a trailer for an item added after the snapshot can read ``unknown_id_in_trailer`` once.
A parked or cut item of a repo that cannot be read stays parked or cut: a decision is a record in the store, not git evidence, so
the board shows it truthfully and the repo's own audit line says the repo cannot be read (an item whose STATE needs the repo's
evidence reads unknown). Exit 4 is a named read failure for the one cross-repo caller (flow's ``scripts/ready.py`` treats any exit
but 0 and 3 as a failed read of that repo and fails closed); it is not a board.
An explicit ``--items`` store that holds a ``store.id`` is read as a pointer-selected one (its lock, its registry, the whole board
when ``--repo`` is absent); a repo holding items but absent from the registry reads unregistered: not read, items unknown.
``store.id`` is checked for a regular file before it is read; ``items.jsonl`` and ``repos.jsonl`` are not, and a file swapped to a
FIFO between that check and the read can block the read: only someone building the store by hand reaches it (L3 round 3).

The JSON is the board (``schema``, ``as_of``, ``items``, ``audit``, ``unattributed``, ``problems``) plus one additive key,
``reader``: ``{repo, since, main_ref, <counts>}`` (every field on every path, a failure board included), for a human
checking what was read. ``--evidence-extra`` may carry only :data:`EXTRA_KEYS`; anything else is exit 2. A flat extra is judged by its own
``fetched_at`` (else the file's modification time), never by the moment this process read it.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import errno
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from levain.desk import board_evidence as be
from levain.desk import board_registry as br
from levain.desk import board_store as bs
from levain.desk import board_producers as bp
from levain.desk.board_state import (EVIDENCE_V, FLAT_EVIDENCE_V, FLAT_PRODUCER, PRODUCER_SOURCES, Evidence, FlatBlock, derive_all,
                                      fold, parse_timestamp)

__all__ = ["EXTRA_KEYS", "ITEMS_ENV", "READ_BY_CLI", "register", "run_state", "state_doc"]

ITEMS_ENV = bs.ITEMS_ENV
#: Read when ``--launch-key`` is not given. Nothing in Levain's launch path exports it yet; the flag is the sure way.
LAUNCH_KEY_ENV = "LEVAIN_LAUNCH_KEY"
#: Sources this CLI reads from git (or by stat). An extra object may not name them: two writers for one source is how
#: a stale hand-made file would silently outvote git.
READ_BY_CLI = ("fetched_at", "main", "chain", "refs", "merged", "branches", "unattributed")
#: The keys an extra object may carry: the sources only FLOW can supply, the ones this process cannot stat, and the version.
EXTRA_KEYS = ("evidence_v", "fetched_at", *PRODUCER_SOURCES, "documents", "unreadable")
EXIT_AUDIT = 3
EXIT_USAGE = 2
EXIT_LOCKED = 4
STATE_LOCK_WAIT = 2.0           # seconds, in all, `board state` waits for a writer's store lock before it refuses: a hold is milliseconds


def register(subparsers: Any) -> None:
    # Hidden, as ``levain upgrade`` and ``levain session`` are (no ``help=``: ``levain --help`` and the README's "All
    # commands" table, which tests/test_readme_commands.py holds to the listed verbs, leave it out); ``levain board state
    # --help`` still works. Give it a ``help=`` and a README row together when it is made public.
    board = subparsers.add_parser("board", description="The work board. Item state is never stored; it is derived on every read.")
    sub = board.add_subparsers(dest="board_cmd", required=True)
    p = sub.add_parser(
        "state", help="Print the board's derived state (READY_SCHEMA v3).",
        description=("Fold the item store, read git, and print the derived state of every item. Reads only. Exit 0 when the "
                     "audit is empty, 3 when it is not, 2 when the items file or an explicit option is unusable."))
    p.add_argument("--json", action="store_true", help="Print the Board as JSON (the contract flow's readers consume).")
    p.add_argument("--items", type=str, metavar="PATH",
                   help=f"The store directory, or its items.jsonl. Default: ${ITEMS_ENV}; one of the two is required.")
    p.add_argument("--evidence-extra", dest="evidence_extra", type=Path, metavar="PATH",
                   help="A JSON object with the evidence flow supplies (seats, band, waiters, documents, unreadable, fetched_at). "
                        f"It may not name a source this command reads itself: {', '.join(READ_BY_CLI)} (its own fetched_at, "
                        "when it carries one, is only the time it was captured).")
    p.add_argument("--producer", dest="producers", action="append", default=[], metavar="NAME",
                   help="An external evidence producer to expect (repeatable): its blocks are read from evidence.NAME.json "
                        "in the store directory. Levain's own reads of seats and band are always expected.")
    p.add_argument("--land-waiter", dest="land_waiter", action=argparse.BooleanOptionalAction, default=None,
                   help="--no-land-waiter: this install runs no land waiter, so Levain declares waiters absent; --land-waiter: it "
                        "does, so waiters are unknown until a producer that runs them supplies them. Either one, given, overrides "
                        "the store's board config; left off, the config's value stands.")
    p.add_argument("--repo", type=Path, default=None, metavar="PATH",
                   help=("The repository to read. Without it, a store with a registry reads every tracked repo (each as its own "
                         "--also-repo NAME=PATH); a store without one reads the repository here."))
    p.add_argument("--repo-name", dest="repo_name", metavar="NAME",
                   help="The repo's name in item ids, <name>#<n> (default: the directory that holds the repository's .git).")
    p.add_argument("--also-repo", dest="also_repo", action="append", default=[], metavar="[NAME=]PATH",
                   help=("Another repository whose items are on the board (repeatable). Each item, and each dependency it waits on, "
                         "is then judged from the evidence of the repo it belongs to; an item of a repo not read reads unknown. "
                         "The repo's name is its directory's name unless NAME=PATH says it (a worktree or a clone under another "
                         "directory name needs NAME=PATH, e.g. levain=/tmp/levain-review). --evidence-extra's documents apply "
                         "to the primary --repo only. Given alone, it reads these repos and not every tracked one."))
    p.add_argument("--since", default=be.BOARD_EPOCH, metavar="ISO",
                   help=f"Commits and branches older than this are out of the attribution scan (default {be.BOARD_EPOCH}).")
    p.add_argument("--now", metavar="ISO", help="Judge the board at this instant (default: the clock). Zone-aware or UTC.")
    p.set_defaults(func=run_state)

    def source_args(parser: Any) -> None:
        parser.add_argument("--source", default=None, metavar="Q-ID",
                            help=("Relay a band answer (a native q-<24 hex> question id; flow's np- rows wait for T12's reader): the record is grade relayed (author kind agent), cites the row, and its words "
                                  "must be a verbatim excerpt of the answer. Refused for an unknown or unanswered row."))
        parser.add_argument("--band-dir", dest="band_dir", default=None, metavar="DIR",
                            help="The band store --source reads (default: this install's consent directory).")

    def author_args(parser: Any) -> None:
        parser.add_argument("--sign", action="store_true",
                            help="Sign this record with the operator's Touch ID key (needs --author-kind operator, a fresh terminal, "
                                 "a terminal key and the authority log that enrols it; the record is verified against that log before it is "
                                 "written). No lock is held while the prompt is up: a write that lands on the store meanwhile "
                                 "makes the signed write refuse (\"the board changed while you signed: sign again\") and nothing is "
                                 "written; run it again to sign afresh. A refused prompt writes nothing.")
        parser.add_argument("--author-kind", dest="author_kind", choices=bs.AUTHOR_KINDS, default="agent",
                            help="Who writes: an operator or an agent (default agent). The name is --by.")
        parser.add_argument("--launch-key", dest="launch_key", default=None, metavar="KEY",
                            help=f"An agent's launch key (default: ${LAUNCH_KEY_ENV} when set). Refused for an operator.")

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
    author_args(a)
    source_args(a)
    a.add_argument("--key", default=None, help="An idempotency key: the same key and --payload-hash returns the existing id.")
    a.add_argument("--payload-hash", dest="payload_hash", default=None, metavar="SHA256")
    a.add_argument("--items", type=str, metavar="PATH", help=items_help)
    a.set_defaults(func=run_add)
    m = sub.add_parser("amend", description="Replace fields of an item.")
    m.add_argument("--id", required=True)
    m.add_argument("--why", required=True)
    m.add_argument("--by", required=True)
    author_args(m)
    source_args(m)
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
    author_args(d)
    source_args(d)
    d.add_argument("--date", default=None)
    d.add_argument("--cause", default=None, metavar="SHA256",
                   help="For a cut: the lowercase hex sha256 of the signed abandon-track record this cut derives from, over that line's "
                        "bytes exactly as written in the store file, without its trailing newline. Checked against the authority log.")
    d.add_argument("--items", type=str, metavar="PATH", help=items_help)
    d.set_defaults(func=run_decide)
    u = sub.add_parser("use", description="Make DIR this install's item store (created if needed); writes the pointer last.")
    u.add_argument("dir", type=Path, metavar="DIR")
    u.add_argument("--import-focus", dest="import_focus", type=Path, metavar="FILE",
                   help="Import a focus.json as register/pause records, replace it by a tombstone, then write the pointer (re-runnable).")
    u.add_argument("--briefcase", type=Path, metavar="DIR", help="Where the focus entries' directories are (default: three levels above FILE).")
    u.add_argument("--by", default="levain board use", help="Who writes the imported records (default: the verb).")
    u.add_argument("--producer", dest="producers", action="append", default=[], metavar="NAME",
                   help="An external evidence producer this install runs (repeatable); saved in the store's board config, which "
                        "`board state` and the cockpit read. The saved config is written exactly from the flags: a flag left off is "
                        "the default, so `use` with none clears it.")
    u.add_argument("--no-land-waiter", dest="no_land_waiter", action="store_true",
                   help="This install runs no land waiter; saved in the board config (left off, it is saved as the default).")
    author_args(u)
    u.set_defaults(func=run_use)
    br.register_cli(sub, author_args, _author, items_help, _seal, source_args, _relay)
    from levain.desk import board_context       # imported here: board_context reads this module's helpers
    board_context.register(sub)


def _fail(message: str) -> int:
    print(f"levain board state: {message}", file=sys.stderr)
    return EXIT_USAGE


def _not_a_repo(path: str) -> str | None:
    """Why ``path`` cannot be read as THE git repository the registry names, or None when it can. A directory inside another
    repository (its own ``.git`` gone) is not this repo: its top must be ``path`` itself."""
    if not os.path.isdir(path):
        return f"{path} is gone (not a directory)"
    rc, top = br._git(path, "rev-parse", "--show-toplevel")
    if rc != 0:
        return f"{path} is not a git repository"
    if os.path.realpath(top) != os.path.realpath(path):
        return f"{path} is not a git repository of its own (it lies inside the one at {top})"
    return None


def _merged_reader(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """One ``reader`` for a whole-board read: the counts summed over every repo read, ``diverged`` as ``<repo>:<branch>``, and
    the per-repo summaries under ``repos``."""
    first = summaries[0]
    out: dict[str, Any] = {}
    for key, val in first.items():
        if isinstance(val, bool) or not isinstance(val, (int, list)):
            out[key] = val
        elif isinstance(val, int):
            out[key] = sum(sm.get(key, 0) for sm in summaries)
        else:
            out[key] = [f"{sm['repo']}:{name}" for sm in summaries for name in sm.get(key, [])]
    out["repo"] = ", ".join(str(sm["repo"]) for sm in summaries)
    out["repos"] = summaries
    return out


def _oldest_fetch(evidences: list[Evidence]) -> str | None:
    """The board's freshness: the OLDEST ``fetched_at`` of the repos read; unknown when any one of them has none."""
    if any(ev.fetched_ts is None for ev in evidences):
        return None
    return min(evidences, key=lambda ev: ev.fetched_ts).fetched_at          # type: ignore[arg-type,return-value]


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
    string would crash whatever reads it later. The flat form (``evidence_v`` 1, or none) becomes ONE
    :class:`~levain.desk.board_state.FlatBlock` per source from :data:`FLAT_PRODUCER`, captured at ``captured_at`` (default:
    the extra's own ``fetched_at``, else none, which reads unreadable); the blocks form (``evidence_v`` 2) is a list of
    producer blocks per source, appended as given and refused if one carries a ``form`` key. ``evidence["producers"]`` names,
    per source, the producers that SUPPLIED it: a producer is expected only where it said something."""
    if not isinstance(extra, dict):
        return "the extra evidence must be a JSON object"
    clash = sorted(k for k in extra if k in READ_BY_CLI and k != "fetched_at")     # the extra's own fetched_at is its capture time, never git's
    if clash:
        return f"the extra evidence names {', '.join(clash)}, which this command reads itself"
    unknown = sorted(str(k) for k in extra if k not in EXTRA_KEYS)
    if unknown:
        return f"the extra evidence names {', '.join(unknown)}, which flow does not supply (the keys are {', '.join(EXTRA_KEYS)})"
    form = extra.get("evidence_v", FLAT_EVIDENCE_V)
    if type(form) is not int or form not in (FLAT_EVIDENCE_V, EVIDENCE_V):
        return f"the extra evidence_v is {form!r}, not {FLAT_EVIDENCE_V} or {EVIDENCE_V}"
    if "fetched_at" in extra and not isinstance(extra["fetched_at"], str):
        return "the extra fetched_at must be a timestamp string"
    when = captured_at if captured_at is not None else extra.get("fetched_at")
    expected: dict[str, list[str]] = evidence.setdefault("producers", {})

    def expect(source: str, producer: Any) -> None:
        if isinstance(producer, str) and producer and producer not in expected.setdefault(source, []):
            expected[source].append(producer)

    for key, value in extra.items():
        if key in ("evidence_v", "fetched_at"):
            continue
        if key in PRODUCER_SOURCES:
            if form == EVIDENCE_V:
                if not isinstance(value, list) or not all(isinstance(b, dict) and "form" not in b for b in value):
                    return f"the extra {key} must be a list of producer blocks (objects with no form key)"
                evidence.setdefault(key, []).extend(value)
                for b in value:
                    expect(key, b.get("producer"))
            else:
                evidence.setdefault(key, []).append(FlatBlock(producer=FLAT_PRODUCER, store_id=None, captured_at=when,
                                                              complete=True, rows=value))
                expect(key, FLAT_PRODUCER)
        elif key == "unreadable":
            if not isinstance(value, dict):
                return "the extra unreadable must be an object of source to reason"
            for src, why in value.items():
                if src in READ_BY_CLI:                  # the nested form of the clash above: a stale file must not outvote git
                    return f"the extra unreadable names {src}, which this command reads itself"
                if not isinstance(why, str):
                    return f"the extra unreadable[{src}] must be a string that says why"
                evidence["unreadable"].setdefault(src, why.strip() or "unreadable (no reason given)")
        elif key == "documents":
            if not isinstance(value, dict):
                return "the extra documents must be an object of path to boolean"
            if primary:                 # a path is repo-relative: another repo's same path is not what flow staged
                evidence["documents"] = {**value, **evidence.get("documents", {})}      # a path this process stat'd wins
    return None


def _store_id(store_dir: Path) -> str | None:
    """The store's id (``<store dir>/store.id``), or None when the file is missing. A file that exists but is empty or blank
    raises :class:`ValueError`, and any other failure to read it :class:`OSError` or :class:`UnicodeDecodeError`: a store whose
    id cannot be read is not one to judge blocks against."""
    path = store_dir / bs.STORE_ID_NAME
    try:
        st = os.lstat(path)                             # never open before knowing what it is: a FIFO would block the read
        if stat.S_ISLNK(st.st_mode):
            st = os.stat(path)                          # a link to a regular file is fine; a broken one raises OSError
    except FileNotFoundError:
        if os.path.lexists(path):                       # a dangling link is a store.id that cannot be read, not a missing one
            raise OSError(errno.ENOENT, "store.id is a broken symlink") from None
        return None
    if not stat.S_ISREG(st.st_mode):
        raise OSError(errno.EINVAL, "store.id is not a regular file")
    text = path.read_text("utf-8").strip()
    if not text:
        raise ValueError("store.id is empty: the store is damaged")
    return text


def _read_extra(path: Path) -> tuple[Any, str]:
    """``(the parsed extra, when it was captured)``: its own ``fetched_at``, else the modification time (UTC) of the SAME open
    descriptor the rows were read from (never a second stat by pathname, which can name another file), never the moment it was
    read: an old file must not read fresh because it was read just now."""
    with open(path, "rb") as fh:
        extra = json.loads(fh.read().decode("utf-8"))
        mtime = os.fstat(fh.fileno()).st_mtime
    if isinstance(extra, dict) and isinstance(extra.get("fetched_at"), str):
        return extra, extra["fetched_at"]
    return extra, datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attach_producers(evidence: dict[str, Any], native: dict[str, dict[str, Any]], external: dict[str, list[dict[str, Any]]],
                      failures: dict[str, str]) -> None:
    """Put the native and the external producers' blocks in front of whatever the extra supplied, and name the producers
    expected per source: Levain's own for seats and band (and for waiters only when it declared them absent), an external
    producer for the sources its file holds, flow for the sources its extra carries, and every producer whose file could not
    be read for every source (it failed, so it cannot say it has nothing)."""
    native, external = copy.deepcopy(native), copy.deepcopy(external)       # this call's own blocks: nothing is shared across repos
    supplied: dict[str, list[str]] = evidence.get("producers") or {}
    expected: dict[str, list[str]] = {}
    for source in PRODUCER_SOURCES:
        evidence[source] = [*([native[source]] if source in native else []), *external.get(source, []), *copy.deepcopy(evidence.get(source, []))]
        names = [*([bp.NATIVE_PRODUCER] if source in native else []), *(b.get("producer") for b in external.get(source, [])),
                 *failures, *supplied.get(source, [])]
        expected[source] = list(dict.fromkeys(n for n in names if isinstance(n, str) and n))
    evidence["producers"] = expected
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


def _author(args: argparse.Namespace) -> dict[str, str]:
    """The author a verb writes under: the kind, the name from ``--by`` (a missing or blank ``--by`` is refused here; no
    default is invented) and the launch key, from the flag else the environment. An empty ``--launch-key`` means no launch
    key, whatever the environment holds. An operator with a launch key is refused by the store."""
    if not args.by or not args.by.strip():
        raise bs.BoardError("--by must name who writes (not empty)")
    author = {"kind": args.author_kind, "name": args.by}
    key = args.launch_key if args.launch_key is not None else (os.environ.get(LAUNCH_KEY_ENV) or None)
    if key and not (args.launch_key is None and args.author_kind == "operator"):
        author["launch_key"] = key             # an ambient key never turns an operator write into a refusal
    return author


def _seal(args: argparse.Namespace, author: dict[str, str]):
    """The seal for ``--sign`` (None without it). An agent author with ``--sign`` is refused here, before any prompt, and so
    is an install with no authority log to verify the signature against (``board_sign.resolve_authority``)."""
    if not args.sign:
        return None
    if author["kind"] != "operator":
        raise bs.BoardError("--sign signs as the operator: add --author-kind operator")
    from levain.desk import board_sign
    return board_sign.terminal_seal(authority=board_sign.resolve_authority())


def _relay(args: argparse.Namespace):
    """``(np id, band source)`` when ``--source`` is given, else None. ``--band-dir`` alone is refused (it would do nothing).
    An operator author is refused by the writer; the kind is also checked here, before any band store is opened."""
    if args.source is not None and args.sign:
        raise bs.BoardError(bs.RELAY_AND_SIGN)
    if args.source is None:
        if args.band_dir is not None and getattr(args, "cause", None) is None:
            raise bs.BoardError("--band-dir only means something with --source (or --cause: the band a relayed abandon-track is re-read through)")
        return None
    if args.author_kind == "operator":
        raise bs.BoardError("--source makes a relayed record, and a relay is the desk's act: the author kind must be agent, not operator")
    from levain.desk import board_relay
    return args.source, board_relay.LazyBand(args.band_dir)


def run_add(args: argparse.Namespace) -> int:
    try:
        author = _author(args)
        relay = _relay(args)
        seal = _seal(args, author)
        target = bs.resolve_store(args.items)
        for spec in args.depends:               # a malformed link is a refusal, not an advisory
            bs.parse_link(spec)
        if target.pointered:
            _warn_unknown_deps(target, args.depends)
        item_id, _ = bs.add(target, args.repo, args.title, args.origin, args.by, words=args.words, done=args.done,
                            done_kind=args.done_kind, tier=args.tier, depends=args.depends, key=args.key,
                            payload_hash=args.payload_hash, author=author, seal=seal, relay=relay)
        print(item_id)
    except bs.BoardError as exc:
        return _refused(exc)
    return 0


def run_amend(args: argparse.Namespace) -> int:
    try:
        author = _author(args)
        relay = _relay(args)
        seal = _seal(args, author)
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
        print("amended", bs.amend(target, args.id, args.why, args.by, fields, author=author, seal=seal, relay=relay))
    except bs.BoardError as exc:
        return _refused(exc)
    return 0


def _cause_authority(args: argparse.Namespace, seal: Any):
    """The authority source an unsigned ``--cause`` is checked against (None when there is no cause, or a seal carries one,
    or this install has no authority log: the decide's own refusal then speaks, as ``board_registry``'s ``_source`` lets it;
    gemini 124, fix 4). A cockpit that is there and cannot be read is passed through, naming it."""
    if args.cause is None or seal is not None:
        return None
    from levain.desk import board_sign
    try:
        return board_sign.resolve_authority()
    except board_sign.AuthorityUnreadable:
        raise
    except bs.BoardError:
        return None


def _cause_band(args: argparse.Namespace, seal: Any):
    """The band an unsigned ``--cause`` re-reads a RELAYED abandon-track's row through (built on first use; None otherwise)."""
    if args.cause is None or seal is not None:
        return None
    from levain.desk import board_relay
    return board_relay.LazyBand(args.band_dir)


def run_decide(args: argparse.Namespace) -> int:
    try:
        author = _author(args)
        relay = _relay(args)
        seal = _seal(args, author)
        target = bs.resolve_store(args.items)
        print("decided", bs.decide(target, args.id, args.decision, args.words, args.by, when=args.date, author=author,
                                   cause=args.cause, seal=seal, authority=_cause_authority(args, seal), relay=relay,
                                   cause_band=_cause_band(args, seal)))
    except bs.BoardError as exc:
        return _refused(exc)
    return 0


def _write_use_config(args: argparse.Namespace) -> Callable[[Path], bool]:
    """The board config, written EXACTLY from the flags (a flag left off is the default), as the step ``use`` runs BEFORE it
    publishes the pointer or tombstones focus.json. With no flag and no config file there is nothing to write; with no flag and
    a file, the file is replaced by the default, which clears it (levain 29: ``use`` writes exactly its flags)."""
    def write(store_dir: Path) -> bool:
        if not (args.producers or args.no_land_waiter or (Path(store_dir) / bs.CONFIG_NAME).exists()):
            return False
        return bs.write_config(store_dir, args.producers, args.no_land_waiter)
    return write


def _config_said(store_dir: Path) -> str:
    """What the board config in ``store_dir`` says, in the words ``use`` prints (so a flagless re-run that cleared it says so)."""
    conf = bs.read_config(store_dir)
    producers = ", ".join(conf["producers"]) if conf["producers"] else "none"
    return f"config: producers {producers}; {'no land waiter' if conf['no_land_waiter'] else 'a land waiter assumed'}"


def run_use(args: argparse.Namespace) -> int:
    try:
        write = _write_use_config(args)
        if args.sign and args.import_focus is not None:
            raise bs.BoardError("use --import-focus writes asserted registry records (register, pause): --sign does not apply to it")
        if args.sign:
            raise bs.BoardError("use writes no record (it points this machine at a store): there is nothing to sign, so --sign does not apply")
        if args.import_focus is not None:
            store_dir, changed, note = br.import_focus_use(args.dir, args.import_focus, by=args.by, author=_author(args),
                                                           briefcase=args.briefcase, before_pointer=write)
            print(f"{'using' if changed else 'already using'} {store_dir} ({note}; {_config_said(store_dir)})")
            return 0
        store_dir, changed = bs.use(args.dir, before_pointer=write)
        said = _config_said(store_dir)
    except bs.BoardError as exc:
        return _refused(exc)
    print(f"{'using' if changed else 'already using'} {store_dir} ({said})")
    return 0


def _sfail(say: Any, message: str) -> tuple[int, None]:
    say(f"levain board state: {message}")
    return EXIT_USAGE, None


def _srefused(say: Any, exc: Exception) -> tuple[int, None]:
    say(f"levain board: refused: {exc}")
    return EXIT_USAGE, None


def run_state(args: argparse.Namespace) -> int:
    rc, doc = state_doc(args)
    if doc is None:
        return rc
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=False))
    else:
        _print_table(doc)
    return rc


#: The sources of a repository read (git and its documents): an empty store reads none, so the stand-in primary has none of them.
#: Every other source (seats, band, waiters, a producer, the store's id) is read there too, and a failure of one still shows.
_GIT_SKIPPED = frozenset({"main", "chain", "branches", "refs", "documents", "unattributed"})


def state_doc(args: argparse.Namespace, say: Any = None, read_evidence: Any = None,
              unreadable: list[str] | None = None, strict_store: bool = False,
              target: bs.Target | None = None) -> tuple[int, dict[str, Any] | None]:
    """``(exit code, the Board document or None)`` of ``levain board state --json``, with no printing of the document: a refusal is
    handed to ``say`` (a line of text; default stderr). ``read_evidence`` replaces :func:`board_evidence.read_git_evidence` (same signature):
    the cockpit hands it a reader that keeps a repo's evidence while that repo's refs are unchanged. The cockpit board reads the board through this, so it never captures a stream.

    ``unreadable`` (a list the caller owns) is filled with every SOURCE the read could not read, one line each: the items file torn,
    a git/producer source of a repo that failed, a tracked repo that cannot be read or is not tracked or registered, the registry.
    These are the audit entries with no item id; the rest of the audit (a gate, landed-not-closed, an item that reads unknown) is
    about an item, not a source, and is not here, nor is ``stale_evidence`` (old evidence is not an unread one, and the exit
    code 3 means any audit entry at all, so it does not tell the two apart).

    ``strict_store`` (the cockpit's read): a real store with no ``repos.jsonl`` tracks no repo, so NO repository is read (the command line
    reads the one in its working directory, which for a cockpit is wherever the server started): its items, if any, read unknown as
    items of a repo that is not registered, and none is an empty board. A ``repos.jsonl`` that exists but cannot be reached (a dangling
    symlink) is an unreadable registry in both modes. ``target`` (a resolved :class:`board_store.Target`) is used as given: the cockpit
    resolves the store once per request and the read does not resolve the pointer again."""
    read_git = read_evidence or be.read_git_evidence
    if say is None:
        say = lambda text: print(text, file=sys.stderr)  # noqa: E731
    if target is None:
        try:
            target = bs.resolve_store(args.items)
        except bs.BoardError as exc:
            if str(exc).startswith("no items store"):
                return _sfail(say, f"no items store: pass --items PATH or set {ITEMS_ENV}")
            return _srefused(say, exc)
    given = args.items or os.environ.get(ITEMS_ENV)
    if not target.pointered and bs.is_store(target):
        target = dataclasses.replace(target, pointered=True)    # an explicit store that holds a store.id is a store: its lock, its registry
    if not given and not target.pointered and not target.items_path.is_file():
        return _sfail(say, f"no items store: pass --items PATH or set {ITEMS_ENV}, or run `levain board use`")
    store_id: str | None = None
    store_id_exc: Exception | None = None
    view: br.View | None = None
    view_exc: Exception | None = None
    registry_file = False
    try:
        # items, repos.jsonl and store.id are read into memory under ONE shared hold, let go before any git read (a writer waits
        # on the lock, not on the git reads). One deadline bounds the hold and every lock inside it.
        deadline = time.monotonic() + STATE_LOCK_WAIT
        with (bs.store_lock(target.store_dir, exclusive=False, deadline=deadline) if target.pointered else contextlib.nullcontext()):
            lines = bs.read_item_lines(target, deadline)
            try:
                store_id = _store_id(target.store_dir)
            except (ValueError, OSError) as exc:        # UnicodeDecodeError is a ValueError
                store_id_exc = exc
            if target.pointered:
                registry_file = os.path.lexists(target.store_dir / br.REPOS_NAME)       # a dangling symlink is a registry that cannot be read
                try:
                    view = br._load(target.store_dir, deadline)
                except bs.LockWaiting:
                    raise
                except bs.BoardError as exc:
                    view_exc = exc
    except bs.LockWaiting as exc:
        say(f"levain board state: refused: {exc}")
        return EXIT_LOCKED, None
    except (OSError, UnicodeDecodeError, bs.BoardError) as exc:
        return _sfail(say, f"the items file {given or target.items_path} cannot be read ({type(exc).__name__}: {exc})")
    if store_id_exc is not None:                        # first: a store.id that cannot be read is said as that, not as a registry that cannot load
        if isinstance(store_id_exc, ValueError) and not isinstance(store_id_exc, UnicodeDecodeError):
            return _sfail(say, str(store_id_exc))
        return _sfail(say, f"store.id could not be read: {type(store_id_exc).__name__}: {store_id_exc}")
    # No --repo (and no --also-repo) on a store that has a registry file: every tracked repo is read (T3). Otherwise one repo, as
    # before (a store with no registry file reads the repository here, whatever its registry audit says).
    whole = args.repo is None and not args.also_repo and registry_file
    if whole:
        if view_exc is not None:                        # it cannot know which repos to read: fail closed, as the launch does (I5)
            return _srefused(say, view_exc)
        if view is not None and view.fold.torn:
            return _srefused(say, bs.BoardError(br._torn_text(view)))
        if args.repo_name:
            return _sfail(say, "--repo-name names one repo: give --repo with it (a registry names its own repos)")
    try:       # before any git read: a config that cannot be read refuses at once. Only a real store has one: a legacy standalone
               # --items file (no store.id beside it) is the default, whatever board.json happens to sit next to it
        config = bs.read_config(target.store_dir) if bs.is_store(target) else {"producers": [], "no_land_waiter": False}
    except bs.BoardError as exc:
        return _srefused(say, exc)
    now = datetime.now(timezone.utc) if args.now is None else parse_timestamp(args.now)
    if now is None:
        return _sfail(say, f"--now {args.now!r} is not an ISO-8601 timestamp")
    folded = fold(lines)
    reads: list[tuple[str | None, Path]]
    unread_why: dict[str, str] = {}
    empty_store = strict_store and not registry_file and args.repo is None and not args.also_repo
    if empty_store:
        reads = []
        for name in sorted({it.repo for it in folded.items.values()}):
            unread_why[name] = "the repo is not registered: not read, its items read unknown"
        cross = True
    elif whole:
        assert view is not None
        reads = []
        held = {it.repo for it in folded.items.values()}
        for name, r in sorted(view.fold.repos.items()):
            if r.mode != "tracked":                    # tracking is mid-ceremony, aborting is an abort, untracked is not on the board
                if name in held:                       # ... but its items are never dropped: they read unknown, and this says why
                    unread_why[name] = f"the repo is {r.mode}, not tracked: not read, its items read unknown"
                continue
            gone = _not_a_repo(r.path)
            if gone is not None:                       # one bad repo never blanks the board: it is named, the others still derive
                unread_why[name] = f"tracked repo cannot be read, its items read unknown: {gone}"
                continue
            reads.append((name, Path(r.path)))
        for name in sorted(held - set(view.fold.repos)):    # items of a repo the registry does not name: never dropped either
            unread_why[name] = "the repo is not registered: not read, its items read unknown"
        if not reads:
            return _sfail(say, "the registry has no tracked repo that can be read" + (": " + "; ".join(f"{n}: {w}" for n, w in unread_why.items())
                         if unread_why else " (track one with `levain board begin-track`/`track`, or pass --repo)"))
        cross = True
    else:
        reads = [(args.repo_name, args.repo if args.repo is not None else Path(".")), *[_split_also(x) for x in args.also_repo]]
        cross = bool(args.also_repo)
    if empty_store:                    # no repository to read: the primary evidence is the flow-side sources alone
        repo, evidence, summary = Path("."), {"evidence_v": EVIDENCE_V}, {"repo": None}
    else:
        repo = Path(reads[0][1]).expanduser()
        evidence, summary = read_git(repo, repo_name=reads[0][0], since=args.since)
        # One repo reads every item, as before; with --also-repo each repo reads the items that belong to it.
        mine = {i: it for i, it in folded.items.items() if it.repo == summary["repo"]} if cross else folded.items
        evidence["documents"] = be.read_documents(mine, repo)
    store_dir = target.store_dir
    # One rule: a flag GIVEN replaces the config's value, a flag left off leaves it (--land-waiter / --no-land-waiter is a tri-state).
    producers = list(dict.fromkeys(args.producers or config["producers"]))   # a name given twice is one producer
    land_waiter = getattr(args, "land_waiter", None)
    no_land_waiter = config["no_land_waiter"] if land_waiter is None else not land_waiter
    if bp.NATIVE_PRODUCER in producers:
        return _sfail(say, f"--producer {bp.NATIVE_PRODUCER}: that is Levain's own producer, always read; --producer names an external one")
    native = bp.native_blocks(now=now, store_id=store_id, no_land_waiter=no_land_waiter)
    external, failures = bp.read_external(store_dir, producers)
    if store_id is None and any(isinstance(b, dict) and b.get("store_id") for bl in external.values() for b in bl):
        say(f"levain board state: the store {store_dir} has no id, so an external block naming one is taken as this store's")
    extra = None
    flat_at: str | None = None
    if args.evidence_extra is not None:
        extra_path = Path(args.evidence_extra).expanduser()
        try:
            extra, flat_at = _read_extra(extra_path)
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            return _sfail(say, f"--evidence-extra {args.evidence_extra} cannot be read ({type(exc).__name__}: {exc})")
        bad = _merge_extra(evidence, extra, captured_at=flat_at)
        if bad is not None:
            return _sfail(say, f"--evidence-extra {args.evidence_extra}: {bad}")
    _attach_producers(evidence, native, external, failures)
    primary = Evidence.from_dict(evidence, now=now, store_id=store_id)
    repo_evidence: dict[str, Evidence] | None = None
    also: list[dict[str, Any]] = []
    if cross:
        if empty_store:
            repo_evidence = {}
        elif not summary["repo"]:
            return _sfail(say, f"--repo {repo}: its repo name is empty, so no item could be matched to it; pass --repo-name")
        else:
            repo_evidence = {summary["repo"]: primary}
        for given, other in reads[1:]:
            ev2, sum2 = read_git(other, repo_name=given, since=args.since)
            name = sum2["repo"]
            if not name or name in repo_evidence:
                return _sfail(say, f"--also-repo {other}: its repo name ({name!r}) is empty or already read; say it with NAME=PATH")
            ev2["documents"] = be.read_documents({i: it for i, it in folded.items.items() if it.repo == name}, other)
            if extra is not None:
                _merge_extra(ev2, extra, primary=False, captured_at=flat_at)     # the same flow-supplied sources; validated once above
            _attach_producers(ev2, native, external, failures)
            repo_evidence[name] = Evidence.from_dict(ev2, now=now, store_id=store_id)
            also.append(sum2)
        read_evidence = list(repo_evidence.values())
        for name, why in unread_why.items():           # not read: every git source says so, so the repo's items read unknown
            repo_evidence[name] = Evidence.from_dict(
                {"evidence_v": EVIDENCE_V, "unreadable": {src: why for src in ("main", "chain", "branches", "refs", "unattributed")}},
                now=now, store_id=store_id)
    board = derive_all(folded, primary, now=now, repo_evidence=repo_evidence,
                       primary_repo=summary["repo"] if cross else None, label_primary=whole or empty_store, unread_repos=unread_why)
    if empty_store:                    # nothing was read, and nothing failed to be: the stand-in primary's sources are not "unreadable"
        mute = {f"{src} unreadable: {why}" for src, why in primary.unreadable.items()      # by source, never by text: only the git sources
                if src in _GIT_SKIPPED and why == "not supplied"}                          # skipped because no repo was read are quiet
        keep = lambda w: w not in mute and not w.startswith("stale_evidence:")  # noqa: E731
        board = dataclasses.replace(board, audit=tuple(x for x in board.audit if x.id is not None or keep(x.why)),
                                    problems=tuple(w for w in board.problems if keep(w)))
    doc = board.to_dict()
    if unreadable is not None:         # a source the board named unreadable: an audit ``unknown`` with no item id (stale_evidence is its own line)
        unreadable.extend(a.why for a in board.audit if a.kind == "unknown" and a.id is None and not a.why.startswith("stale_evidence:"))
    # T2-b: the 72-hour rule shows on the board's audit list too (from the registry already read under the one hold)
    try:
        registry_audit = br.audit_for_state(target, now, view=view, view_exc=view_exc, deadline=deadline)
    except bs.LockWaiting as exc:
        say(f"levain board state: refused: {exc}")
        return EXIT_LOCKED, None
    doc["audit"] += [{"kind": "unknown", "id": None, "why": why} for why in registry_audit]
    if unreadable is not None:         # the registry lines are two kinds: it could not be read (here), or the 72-hour rule (an ordinary entry)
        unreadable.extend(w for w in registry_audit if w.startswith("registry: unreadable:") or "the registry is unreadable:" in w)
    doc["reader"] = summary
    if empty_store:
        doc["as_of"], doc["reader"] = None, {"repo": "", "repos": []}
    elif whole:                                           # no repo is privileged: freshness, counts and labels span every repo read
        doc["as_of"] = _oldest_fetch(read_evidence)
        doc["reader"] = _merged_reader([summary, *also])
    elif also:
        doc["reader"] = {**summary, "also": also}
    return (EXIT_AUDIT if board.audit or registry_audit else 0), doc


def _print_table(doc: dict[str, Any]) -> None:
    print(f"board {doc['schema']} as of {doc['as_of'] or 'unknown'}: {len(doc['items'])} items, {len(doc['audit'])} audit entries")
    for s in doc["items"]:
        print(f"  {s['id']:<14} {s['state']:<18} {s['reason']}")
    for a in doc["audit"]:
        print(f"  audit {a['kind']}: {a['id'] or '-'}: {a['why']}")
    for name in doc.get("reader", {}).get("diverged", []):
        print(f"  diverged: {name}: the local head and origin's copy each hold commits the other lacks (ahead is the union)")
