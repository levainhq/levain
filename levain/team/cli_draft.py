"""``levain team draft ...``: the coordinator drafts team-ledger entries, the operator signs (C8).

``add`` is the coordinator's (or yours); ``promote`` is the one step that signs, and it runs the ordinary record path
under THIS clone's key after showing the whole draft. Inside a session Levain launched, only ``add`` (as the
coordinator), ``list`` and ``show`` run: the coordinator's door refuses the rest before this module is reached. That
keeps the draft path honest; it is not a lock on the key, which any process of the same user can still use.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import entry as E
from .ledger_api import open_ledger
from .transport import TeamError


def _store(args):
    from levain.coordinator.store import CoordStore

    return CoordStore(args.queue_dir.expanduser() if args.queue_dir is not None else None)


def _show(v: dict) -> str:
    e = v["entry"]
    lines = [f"draft {v['draft_id']}  [{v['state']}]  drafted by {v['drafted_by']} at {v['drafted_at']}",
             f"  {v['type']}" + (f" . {e['kind']}" if e.get("kind") else "") +
             (f"  owner {e['owner']}" if e.get("owner") else "")]
    for k in ("words", "summary", "reason", "recheck"):
        if e.get(k):
            lines.append(f"  {k}: {json.dumps(e[k], ensure_ascii=False)}")
    if e.get("paths"):
        lines.append("  governs: " + ", ".join(e["paths"]))
    if v.get("ledger_id"):
        lines.append(f"  signed as ledger entry {v['ledger_id']}")
    return "\n".join(lines)


# a test seam: called with the draft id once a promote has reserved it, before it reads or signs the ledger
_after_reserve = None


def _signed(gl, handle: str, key: str) -> tuple[dict | None, dict[str, dict]]:
    """(this member's entry whose agent is ``key``, or None; every verified entry by id) read from the ledger now.
    Per file too, so a line that is WAITING under a frozen ledger is found as well as an enforced one."""
    gl.forget_derivation()
    led = gl.ledger()
    by_id: dict[str, dict] = {}
    for f in led.files:
        for e in f.entries:
            by_id[e["id"]] = e
    for e in led.entries:
        by_id.setdefault(e["id"], e)
    mine = sorted((e for e in by_id.values() if e.get("agent") == key and e.get("author") == handle),
                  key=lambda e: (e.get("ts", ""), e["id"]))
    return (mine[0] if mine else None), by_id


def _words(args) -> str | None:
    if getattr(args, "words_file", None):
        return Path(args.words_file).read_text(encoding="utf-8").strip()
    return args.words


def cmd_draft(args) -> int:
    from levain.coordinator import teamdrafts as T
    import sqlite3

    from levain.coordinator.store import CoordError
    from levain.dispatch.jobs import JobStoreError

    verb = args.draft_command
    try:
        store = _store(args)      # opening the store refuses like any of its verbs: an error line, never a traceback
        if verb == "add":
            v = T.add_draft(store, args.type, words=_words(args), kind=args.kind, owner=args.owner,
                            paths=[p for p in (args.paths or [])], summary=args.summary, reason=args.reason,
                            recheck=args.recheck, thread_id=args.thread, by=args.by, token=args.token)
            print(_show(v) if not args.json else json.dumps(v, ensure_ascii=False))
        elif verb == "list":
            rows = T.list_drafts(store, state=None if args.all else ("drafted", "promoting"))
            print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else
                  ("\n".join(_show(r) for r in rows) or "no open team drafts"))
        elif verb == "show":
            v = T.get_draft(store, args.draft_id)
            if v is None:
                raise TeamError(f"no team draft {args.draft_id}")
            print(json.dumps(v, ensure_ascii=False, indent=2) if args.json else _show(v))
        elif verb == "withdraw":
            print(_show(T.withdraw_draft(store, args.draft_id, by=args.by, token=args.token)))
        elif verb == "promote":
            v = T.get_draft(store, args.draft_id)
            if v is None:
                raise TeamError(f"no team draft {args.draft_id}")
            _promote(args, store, v)
    except (CoordError, JobStoreError, sqlite3.Error) as exc:
        raise TeamError(str(exc)) from None
    return 0


def _promote(args, store, v: dict) -> None:
    import os

    from levain.coordinator import teamdrafts as T

    if v["state"] not in ("drafted", "promoting"):
        raise TeamError(f"draft {args.draft_id} is already {v['state']}")
    e = v["entry"]
    print(_show(v))
    words = _words(args)
    if e.get("kind") == "ruling":
        # a ruling is signed in the decider's own words: the coordinator's draft words are shown, never signed as his
        if not (words or "").strip():
            raise TeamError("a ruling is signed in the decider's own words: pass them with --words or --words-file "
                            "(the draft's words above are the coordinator's and are not signed)")
    else:
        words = words or e.get("words")
    if not args.yes:
        if not sys.stdin.isatty() or input("sign this into the team ledger with your key? [y/N] ").strip().lower() != "y":
            raise TeamError("not signed (run in a terminal and answer y, or pass --yes)")
    from .cli import _repo, _repo_paths, _actor
    repo = _repo(args)
    gl = open_ledger(repo)
    gl.require_joined()
    team, handle = _actor(gl)
    owner = e.get("owner")
    if e.get("kind") == "ruling" and not owner:
        raise TeamError("a ruling needs an owner on the draft (client:<name>, lead, or a member handle)")
    if owner and not team.owner_ok(owner):
        raise TeamError(f"owner {owner!r} is not allowed by team.toml")
    key = T.draft_agent(v["draft_id"])
    draft_id = v["draft_id"]
    me = T.promoter(os.path.realpath(gl.base))
    v, owns = T.reserve_promotion(store, draft_id, me, resume=args.resume)
    if _after_reserve is not None:
        _after_reserve(draft_id)
    push = not args.no_push
    if not owns:
        # another promote owns the reservation: only what it already signed may be taken up here, never a signature
        sealed, _ = _signed(gl, handle, key)
        if sealed is None:
            held = v.get("owner") or {}
            raise TeamError(f"a promote of draft {draft_id} is in progress (pid {held.get('pid')}) or was interrupted; "
                            f"`levain team draft promote {draft_id} --resume` signs it if no entry exists")
        print(f"draft {draft_id} was already signed as {sealed['id']}: adopting it (nothing signed again)")
        if push and gl.remote:
            gl.sync(push=True)
        _, by_id = _signed(gl, handle, key)
        T.mark_promoted(store, draft_id, sealed["id"], by_id)
        print(f"adopted {sealed['id']}")
        return
    try:
        sealed, _ = _signed(gl, handle, key)
        adopted = sealed is not None
        if adopted:
            # a promote of this draft that this one took over (--resume) signed it and stopped before marking it:
            # adopt that entry, no second signature; what is left is publishing it
            print(f"draft {draft_id} was already signed as {sealed['id']}: adopting it (nothing signed again)")
            if push and gl.remote:
                gl.sync(push=True)
        else:
            top = os.path.realpath(repo.toplevel)
            # a draft's paths are from the repository root, wherever this promote runs
            entry = E.build(handle, e["type"], kind=e.get("kind"), owner=owner, words=words,
                            summary=e.get("summary"), reason=e.get("reason"), recheck=e.get("recheck"),
                            paths=_repo_paths(repo, [os.path.join(top, p) for p in e.get("paths") or []]), agent=key)
            sealed = gl.append(entry, push=push)
    except BaseException as exc:
        try:
            got, _ = _signed(gl, handle, key)
        except Exception:      # noqa: BLE001 - unreadable now: the reservation stays, and --resume decides
            raise exc from None
        if got is None:
            try:
                T.release_promotion(store, draft_id, me["nonce"])
            except Exception as rel:      # noqa: BLE001 - the error that stopped the promote is the one to report
                print(f"levain team: WARNING: draft {draft_id} stays reserved ({rel}); `levain team draft promote "
                      f"{draft_id} --resume` takes it up", file=sys.stderr)
            raise
        if isinstance(exc, TeamError):
            raise TeamError(f"{exc}. Draft {draft_id} stays reserved as signed entry {got['id']}: `levain team "
                            "draft promote` on it again publishes that entry and signs nothing new") from None
        raise
    _, by_id = _signed(gl, handle, key)
    T.mark_promoted(store, draft_id, sealed["id"], by_id)
    print(f"{'adopted' if adopted else 'signed'} {sealed['id']}")


def register(sub, add) -> None:
    p = sub.add_parser("draft", help="The coordinator drafts ledger entries; promote signs one (the operator's).")
    dsub = p.add_subparsers(dest="draft_command", required=True)

    def common(q, *, repo: bool = False) -> None:
        q.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                       help="The consent directory holding dispatch.db (default: the consent queue's).")
        q.add_argument("--json", action="store_true")
        q.add_argument("--repo", help="a path inside the project's git repository (default: cwd)")
        q.set_defaults(func=_run, team_command="draft")

    def actor(q) -> None:
        q.add_argument("--by", choices=("operator", "coordinator"), default="coordinator")
        q.add_argument("--token", type=int, default=None, help="With --by coordinator: the lease token.")

    a = dsub.add_parser("add", help="Draft an entry (nothing is signed).")
    a.add_argument("type", choices=("decision", "constraint", "finding", "question", "tension"))
    a.add_argument("--kind", choices=("ruling", "practice"))
    a.add_argument("--owner"); a.add_argument("--words"); a.add_argument("--words-file")
    a.add_argument("--summary"); a.add_argument("--reason"); a.add_argument("--recheck")
    a.add_argument("--paths", action="append", metavar="GLOB"); a.add_argument("--thread")
    actor(a); common(a)
    ls = dsub.add_parser("list", help="Open drafts and any being promoted (--all: every draft)."); ls.add_argument("--all", action="store_true"); common(ls)
    sh = dsub.add_parser("show", help="One draft."); sh.add_argument("draft_id"); common(sh)
    w = dsub.add_parser("withdraw", help="Withdraw a draft."); w.add_argument("draft_id"); actor(w); common(w)
    pr = dsub.add_parser("promote", help="Show a draft whole, then sign it into the ledger with your key (yours only).")
    pr.add_argument("draft_id"); pr.add_argument("--yes", action="store_true")
    pr.add_argument("--words", help="the decider's own words (a ruling's are required: the draft's are never signed)")
    pr.add_argument("--words-file", help="the decider's own words, from a file")
    pr.add_argument("--no-push", action="store_true")
    pr.add_argument("--resume", action="store_true",
                    help="take over a reservation whose promote is gone (run in the clone that made it)")
    common(pr)


def _run(args) -> int:
    from .cli import _guarded
    # promote writes the ledger: one op on the clone; the rest never touch it
    if args.draft_command == "promote":
        return _guarded(cmd_draft)(args)
    try:
        return cmd_draft(args)
    except (TeamError, E.EntryError, ValueError, OSError) as exc:     # ValueError covers UnicodeError
        print(f"levain team: {exc}", file=sys.stderr)
        return 2
