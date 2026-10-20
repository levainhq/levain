"""levain.coordinator.cli — ``levain drop`` (drop words for the coordinator and go) and ``levain thread`` (the threads
your drops became: list, show, and the one-tap fixes as commands).

``levain drop "words"`` stores the words verbatim and prints the drop receipt. Run inside a git work tree on a branch,
it records that repo and branch as where the drop was made from (C0 §3.1: a CLI drop's source is the branch checked out
where you typed it); a detached HEAD or no repo records none. Words come from the arguments, or from stdin with ``-``.
``levain drops split`` is the coordinator's door: it names the threads a drop becomes; ``levain drops list|show`` read
them back.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.coordinator.store import (KINDS, STATES, WORDS_MAX, CoordError, CoordStore, Drop, Part, Source, Thread,
                                      check_source)
from levain.dispatch.jobs import JobStoreError

# what a store call can refuse with: the grammar's refusal, an untrustworthy store, a database it cannot use
_REFUSED = (CoordError, JobStoreError, sqlite3.Error, OSError)

__all__ = ["add_parsers", "cwd_source"]

_GIT_TIMEOUT = 10


def _git(cwd: str, *args: str) -> str | None:
    """One git call in ``cwd`` with the dispatch store's hardened environment; ``None`` when git fails or is absent
    (a drop's source is a convenience: no repo is ``none``, never an error)."""
    from levain.dispatch.contract import git_env

    try:
        cp = subprocess.run(["git", "--no-replace-objects", "-C", cwd, *args], capture_output=True,
                            timeout=_GIT_TIMEOUT, env=git_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    out = cp.stdout.decode("utf-8", "surrogateescape")
    return out[:-1] if out.endswith("\n") else out          # git's terminator only: a path may end in a space


def cwd_source(cwd: str | None = None) -> Source:
    """Where a CLI drop is made from: the work tree's top, the checked-out branch and its head; ``Source()`` (none)
    outside a repo or on a detached HEAD."""
    cwd = cwd or os.getcwd()
    top = _git(cwd, "rev-parse", "--show-toplevel")          # already absolute; one line or it is not trusted
    ref = _git(cwd, "symbolic-ref", "-q", "HEAD")
    if not top or "\n" in top or not top.startswith("/") or not ref or not ref.startswith("refs/heads/"):
        return Source()
    # the head is read from the branch just named, never from HEAD again: a checkout in between cannot pair one
    # branch with another's commit
    head = _git(cwd, "rev-parse", "--verify", "-q", "--end-of-options", f"{ref}^{{commit}}")
    if not head:
        return Source()
    source = Source("cwd", os.path.realpath(top), ref, head)
    try:
        return check_source(source)
    except CoordError:
        return Source()          # a path the store cannot hold (not UTF-8, a hidden character) is recorded as none


def _store(args: argparse.Namespace) -> CoordStore:
    d = args.queue_dir.expanduser() if args.queue_dir is not None else None
    return CoordStore(d)


def _drop_row(d: Drop) -> dict[str, Any]:
    return {"drop_id": d.drop_id, "surface": d.surface, "outbox_id": d.outbox_id, "words": d.words,
            "words_sha256": d.words_sha256, "sent_at": d.sent_at, "received_at": d.received_at,
            "source": {"how": d.source.how, "repo": d.source.repo, "ref": d.source.ref, "head": d.source.head}}


def _thread_row(t: Thread) -> dict[str, Any]:
    return {"thread_id": t.thread_id, "kind": t.kind, "title": t.title, "state": t.state, "about": t.about,
            "repo": t.repo, "rev": t.rev, "members": list(t.members), "members_sha256": t.members_sha256,
            "created_at": t.created_at}


def _where(d: Drop) -> str:
    if d.source.how == "none":
        return "no repo or branch recorded"
    return f"from {d.source.repo} on {d.source.ref.removeprefix('refs/heads/')}"


def _thread_line(t: Thread) -> str:
    about = f" (about {t.about})" if t.about else ""
    return f"  {t.thread_id}  {t.kind.upper():5} {t.state:7} rev {t.rev}  {t.title}{about}"


def _shown(words: str) -> str:
    """Words for a terminal: quoted, with every character that would not show as itself (a control, a bidi override)
    marked by its code point. The stored words stay verbatim; this is only what a text listing prints."""
    return json.dumps(unshowable.mark(words), ensure_ascii=False)


def _emit(out: Any, as_json: bool, obj: Any, text: str) -> None:
    out.write((json.dumps(obj, ensure_ascii=False, indent=2) if as_json else text) + "\n")


def _read_words(args: argparse.Namespace) -> str:
    """The words: one argument kept as typed, several joined by one space, or ``-`` alone for stdin (strict UTF-8, one
    trailing newline dropped, so ``echo words | levain drop -`` stores what ``levain drop words`` does)."""
    if args.words == ["-"]:
        cap = WORDS_MAX * 4 + 2          # the most bytes WORDS_MAX characters can take, and a newline
        raw = sys.stdin.buffer.read(cap + 1)
        if len(raw) > cap:
            raise CoordError(f"a drop is at most {WORDS_MAX} characters; stdin holds more")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CoordError("stdin is not UTF-8 text") from None
        return text[:-1] if text.endswith("\n") else text
    return " ".join(args.words)


def run_drop(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        words = _read_words(args)
        source = Source() if args.no_source else cwd_source()
        drop, created = store.accept_drop(words, surface="cli", outbox_id=args.outbox_id, source=source)
    except _REFUSED as exc:
        print(f"levain drop: {exc}", file=sys.stderr)
        return 1
    state = "delivered" if created else "already delivered (resend, nothing written)"
    _emit(out, args.json, {**_drop_row(drop), "created": created},
          f"drop receipt: {drop.drop_id} {state} at {drop.received_at}, {_where(drop)}")
    return 0


def _parse_part(spec: str) -> Part:
    kind, sep, title = spec.partition(":")
    if not sep or kind not in KINDS or not title.strip():
        raise CoordError(f"--thread takes KIND:TITLE with KIND one of {', '.join(KINDS)} (got {spec[:80]!r})")
    return Part(kind=kind, title=title.strip())


def _parse_attach(spec: str) -> Part:
    kind, sep, thread_id = spec.partition(":")
    if not sep or kind not in KINDS or not thread_id:
        raise CoordError(f"--attach takes KIND:THREAD, KIND the intent the drop adds (one of {', '.join(KINDS)}) "
                         f"(got {spec[:80]!r})")
    return Part(kind=kind, attach=thread_id)


def run_split(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        parts = [_parse_part(s) for s in args.thread]
        if args.about is not None:
            if all(p.kind == "work" for p in parts):
                raise CoordError("--about applies to a new ask, note or relay thread; this split names none")
            parts = [Part(p.kind, p.title, about=None if p.kind == "work" else args.about) for p in parts]
        parts += [_parse_attach(a) for a in args.attach]
        threads = store.split(args.drop_id, parts, by=args.by)
    except _REFUSED as exc:
        print(f"levain drops split: {exc}", file=sys.stderr)
        return 1
    n = len(threads)
    _emit(out, args.json, [_thread_row(t) for t in threads],
          f"drop receipt: {args.drop_id} split into {n} thread{'s' if n != 1 else ''}\n"
          + "\n".join(_thread_line(t) for t in threads))
    return 0


def run_drop_show(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        drop, threads, receipts = store.drop_view(args.drop_id)
    except _REFUSED as exc:
        print(f"levain drops show: {exc}", file=sys.stderr)
        return 1
    obj = {**_drop_row(drop), "threads": [_thread_row(t) for t in threads],
           "drop_receipts": [{"reply_id": r.reply_id, "at": r.at, **r.body} for r in receipts]}
    text = (f"{drop.drop_id}  {drop.surface}  received {drop.received_at}, {_where(drop)}\n"
            f"  words (verbatim, data): {_shown(drop.words)}\n"
            + ("\n".join(_thread_line(t) for t in threads) if threads else "  not split yet")
            + "\n  drop receipts: " + ", ".join(f"{r.body.get('stage')} {r.at}" for r in receipts))
    _emit(out, args.json, obj, text)
    return 0


def run_drop_list(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        drops = _store(args).drops(limit=args.limit)
    except _REFUSED as exc:
        print(f"levain drops list: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, [_drop_row(d) for d in drops],
          "\n".join(f"{d.drop_id}  {d.received_at}  {_shown(d.words[:80])}" for d in drops)
          or "no drops")
    return 0


def run_thread(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    try:
        store = _store(args)
        cmd = args.thread_cmd
        if cmd == "list":
            threads = store.threads(include_closed=args.all)
            _emit(out, args.json, [_thread_row(t) for t in threads],
                  "\n".join(_thread_line(t) for t in threads) or "no open threads")
            return 0
        if cmd == "show":
            t, revs, events = store.thread_view(args.thread_id)
            obj = {**_thread_row(t), "revisions": [{"rev": r, "members": list(m), "members_sha256": s, "cause": c}
                                                   for r, m, s, c in revs],
                   "events": [{"seq": e[0], "field": e[1], "value": e[2], "cause": e[3], "by": e[4], "at": e[5]}
                              for e in events]}
            text = (_thread_line(t) + "\n"
                    + "\n".join(f"    rev {r} ({c}): {', '.join(m) or '(no drops)'}" for r, m, _s, c in revs) + "\n"
                    + "\n".join(f"    #{e[0]} {e[1]} = {e[2]}  [{e[4]}: {e[3]}]" for e in events))
            _emit(out, args.json, obj, text)
            return 0
        if cmd == "state":
            t = store.set_state(args.thread_id, args.state, by=args.by, cause=args.cause)
        elif cmd == "merge":
            t = store.merge(args.into, args.other, by=args.by)
        elif cmd == "detach":
            t = store.detach(args.thread_id, args.drop_id, by=args.by)
        elif cmd == "add":
            if args.about is not None and args.attach:
                raise CoordError("--about names what a NEW thread concerns; an attach joins an existing one")
            part = _parse_attach(args.attach) if args.attach else _parse_part(args.thread)
            if args.about is not None:
                part = Part(part.kind, part.title, about=args.about)
            t = store.add(args.drop_id, part, by=args.by)
        else:   # set
            t = store.set_field(args.thread_id, args.field, args.value, by=args.by, cause=args.cause)
    except _REFUSED as exc:
        print(f"levain thread {args.thread_cmd}: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.json, _thread_row(t), _thread_line(t))
    return 0


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    qdir_help = "The consent directory holding dispatch.db (default: the consent queue's)."

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None, help=qdir_help)
        p.add_argument("--json", action="store_true", help="Machine-readable output.")

    d = subparsers.add_parser(
        "drop", help="Drop words for the coordinator and go (kept verbatim; never authority).", allow_abbrev=False,
        description="Store your words as a drop for the coordinator and print its drop receipt. The words are data: "
                    "they authorise nothing. Read them back with `levain drops`. Words that start with '-' go after "
                    "`--` (levain drop -- -words) or through stdin (levain drop -).")
    d.add_argument("words", nargs="+", help="The words (or - to read them from stdin).")
    d.add_argument("--outbox-id", dest="outbox_id", default=None,
                   help="An idempotency key: a resend with the same key and words writes nothing.")
    d.add_argument("--no-source", dest="no_source", action="store_true",
                   help="Record no repo or branch, even inside a git work tree.")
    common(d)
    d.set_defaults(func=run_drop)

    ds = subparsers.add_parser("drops", help="Your drops: list, show, and split (the coordinator's door).")
    d_sub = ds.add_subparsers(dest="drops_cmd", required=True)
    sp = d_sub.add_parser("split", help="Name the threads a drop becomes (the coordinator's door).")
    sp.add_argument("drop_id")
    sp.add_argument("--thread", action="append", default=[], metavar="KIND:TITLE",
                    help=f"A new thread holding the drop; KIND is one of {', '.join(KINDS)}. Repeatable.")
    sp.add_argument("--attach", action="append", default=[], metavar="KIND:THREAD",
                    help="Put the drop into an existing open thread of that kind. Repeatable.")
    sp.add_argument("--about", default=None, metavar="THREAD",
                    help="The work thread each new ask/note/relay thread here concerns, or @N for this split's own "
                         "N-th --thread.")
    sp.add_argument("--by", default="coordinator", choices=("coordinator", "operator"))
    common(sp)
    sp.set_defaults(func=run_split)

    sh = d_sub.add_parser("show", help="One drop: its words, its threads, its drop receipts.")
    sh.add_argument("drop_id")
    common(sh)
    sh.set_defaults(func=run_drop_show)

    ls = d_sub.add_parser("list", help="Recent drops, newest first.")
    ls.add_argument("--limit", type=int, default=50)
    common(ls)
    ls.set_defaults(func=run_drop_list)

    t = subparsers.add_parser("thread", help="The threads your drops became.")
    t_sub = t.add_subparsers(dest="thread_cmd", required=True)
    tl = t_sub.add_parser("list", help="Threads not yet closed (with --all, every thread).")
    tl.add_argument("--all", action="store_true")
    common(tl)
    ts = t_sub.add_parser("show", help="One thread: its revisions and events.")
    ts.add_argument("thread_id")
    common(ts)
    tst = t_sub.add_parser("state", help=f"Move a thread ({' -> '.join(STATES)}; reopen to open).")
    tst.add_argument("thread_id")
    tst.add_argument("state", choices=STATES)
    tst.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    tst.add_argument("--cause", default="")
    common(tst)
    tm = t_sub.add_parser("merge", help="Fold OTHER into INTO (one intent the split cut in two).")
    tm.add_argument("into")
    tm.add_argument("other")
    tm.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    common(tm)
    td = t_sub.add_parser("detach", help="Take a drop out of a thread (it must stay in another).")
    td.add_argument("thread_id")
    td.add_argument("drop_id")
    td.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    common(td)
    ta = t_sub.add_parser("add", help="Put an already-split drop into one more thread.")
    ta.add_argument("drop_id")
    g = ta.add_mutually_exclusive_group(required=True)
    g.add_argument("--thread", metavar="KIND:TITLE")
    g.add_argument("--attach", metavar="KIND:THREAD")
    ta.add_argument("--about", default=None, metavar="THREAD")
    ta.add_argument("--by", default="operator", choices=("operator", "coordinator"))
    common(ta)
    tf = t_sub.add_parser("set", help="Re-kind, re-title or set the repo of a thread.")
    tf.add_argument("thread_id")
    tf.add_argument("field", choices=("kind", "title", "repo"))
    tf.add_argument("value")
    tf.add_argument("--by", default="coordinator", choices=("coordinator", "operator"))
    tf.add_argument("--cause", default="")
    common(tf)
    for p in (tl, ts, tst, tm, td, ta, tf):
        p.set_defaults(func=run_thread)
