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


def cmd_draft(args) -> int:
    from levain.coordinator import teamdrafts as T
    from levain.coordinator.store import CoordError

    store = _store(args)
    verb = args.draft_command
    try:
        if verb == "add":
            words = Path(args.words_file).read_text(encoding="utf-8").strip() if args.words_file else args.words
            v = T.add_draft(store, args.type, words=words, kind=args.kind, owner=args.owner,
                            paths=[p for p in (args.paths or [])], summary=args.summary, reason=args.reason,
                            recheck=args.recheck, thread_id=args.thread, by=args.by, token=args.token)
            print(_show(v) if not args.json else json.dumps(v, ensure_ascii=False))
        elif verb == "list":
            rows = T.list_drafts(store, state=None if args.all else "drafted")
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
            if v["state"] != "drafted":
                raise TeamError(f"draft {args.draft_id} is already {v['state']}")
            e = v["entry"]
            print(_show(v))
            if e.get("kind") == "ruling":
                print("  NOTE: these are the coordinator's words, not the decider's; a ruling is signed in the "
                      "decider's own words (--words replaces them).")
            if not args.yes:
                if not sys.stdin.isatty() or input("sign this into the team ledger with your key? [y/N] ").strip().lower() != "y":
                    raise TeamError("not signed (run in a terminal and answer y, or pass --yes)")
            from .cli import _repo, _split, _repo_paths, _actor
            repo = _repo(args)
            gl = open_ledger(repo)
            gl.require_joined()
            team, handle = _actor(gl)
            owner = e.get("owner")
            if e.get("kind") == "ruling" and not owner:
                raise TeamError("a ruling needs an owner on the draft (client:<name>, lead, or a member handle)")
            if owner and not team.owner_ok(owner):
                raise TeamError(f"owner {owner!r} is not allowed by team.toml")
            entry = E.build(handle, e["type"], kind=e.get("kind"), owner=owner, words=args.words or e.get("words"),
                            summary=e.get("summary"), reason=e.get("reason"), recheck=e.get("recheck"),
                            paths=_repo_paths(repo, list(e.get("paths") or [])), agent="coordinator-draft:" + v["draft_id"])
            sealed = gl.append(entry, push=not args.no_push)
            T.mark_promoted(store, v["draft_id"], sealed["id"])
            print(f"signed {sealed['id']}")
    except CoordError as exc:
        raise TeamError(str(exc)) from None
    return 0


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
    ls = dsub.add_parser("list", help="Open drafts (--all: every draft)."); ls.add_argument("--all", action="store_true"); common(ls)
    sh = dsub.add_parser("show", help="One draft."); sh.add_argument("draft_id"); common(sh)
    w = dsub.add_parser("withdraw", help="Withdraw a draft."); w.add_argument("draft_id"); actor(w); common(w)
    pr = dsub.add_parser("promote", help="Show a draft whole, then sign it into the ledger with your key (yours only).")
    pr.add_argument("draft_id"); pr.add_argument("--yes", action="store_true")
    pr.add_argument("--words", help="the decider's own words, replacing the draft's"); pr.add_argument("--no-push", action="store_true")
    common(pr)


def _run(args) -> int:
    from .cli import _guarded
    # promote writes the ledger: one op on the clone; the rest never touch it
    if args.draft_command == "promote":
        return _guarded(cmd_draft)(args)
    try:
        return cmd_draft(args)
    except TeamError as exc:
        print(f"levain team: {exc}", file=sys.stderr)
        return 2
