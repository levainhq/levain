"""``levain notify``: mint, list and revoke the notify tokens a service posts a notice with.

Registered into the main parser by :func:`register`. The tokens live in the same queue directory ``levain serve`` reads
(``--queue-dir``, default the consent queue), so a token minted here is valid on the running server at once. A mint and a
revoke are the operator's own act: they are refused inside an agent session, as a presence word the operator alone may
say is, because a token that can wake him is not a thing a seat gives itself. Listing shows no secret.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "notify", help="Notify tokens: the credential a service posts a notice at your phone with.",
        description="A notice is text posted at you by a service you named: a doorbell, never an action. Each token is "
                    "bound to one source label you choose, a priority ceiling and rate caps.")
    sub = p.add_subparsers(dest="notify_cmd", required=True)
    tok = sub.add_parser("token", help="Mint, list or revoke notify tokens.")
    tsub = tok.add_subparsers(dest="token_cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                        help="The consent queue directory the server uses (default: <levain home>/consent).")
        return sp

    m = common(tsub.add_parser("mint", help="Make a token. It is printed once and kept nowhere."))
    m.add_argument("--name", required=True, help="The token's name (1-40 letters, digits, spaces, dots, dashes, underscores).")
    m.add_argument("--source", default=None,
                   help="The label every notice from it carries (default: the name, lowercased). It cannot change later.")
    m.add_argument("--origin", choices=("token", "flow"), default="token",
                   help="flow: flow's own poster (it may name the notice's source; its links render as Levain's own). "
                        "token (default): a service you gave a key; its links show in full.")
    m.add_argument("--urgent", action="store_true",
                   help="Let this token post urgent notices (they pass the presence record's attention rule: `levain ping status` says what is let through now).")
    m.add_argument("--max-priority", dest="max_priority", choices=("low", "normal", "urgent"), default=None,
                   help="The highest priority this token may post (default normal; urgent needs --urgent).")
    m.add_argument("--rate-per-min", dest="rate_per_min", type=int, default=None, help="Notices per minute (default 6).")
    m.add_argument("--rate-per-day", dest="rate_per_day", type=int, default=None, help="Notices per day (default 200).")
    m.add_argument("--urgent-per-hour", dest="urgent_per_hour", type=int, default=None,
                   help="Urgent notices per hour (default 3).")
    m.add_argument("--json", action="store_true", help="Print the token and its record as JSON.")
    m.set_defaults(func=_mint)
    ls = common(tsub.add_parser("list", help="The live tokens: never a secret."))
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=_list)
    rv = common(tsub.add_parser("revoke", help="Revoke a token by name; its next post is refused."))
    rv.add_argument("name")
    rv.set_defaults(func=_revoke)


def _qdir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import default_queue_dir

    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _in_agent_session() -> bool:
    from levain.consent import band

    return band.agent_process() is not None


def _mint(args: argparse.Namespace) -> int:
    from levain.webpush import notify_tokens

    if _in_agent_session():
        print("levain notify token mint: run this from your own terminal, not inside an agent session "
              "(a token can wake you; a seat does not give itself one).")
        return 2
    kw = {k: getattr(args, k) for k in ("rate_per_min", "rate_per_day", "urgent_per_hour")}
    if args.max_priority is not None:
        kw["max_priority"] = args.max_priority
    try:
        rec, token = notify_tokens.create(_qdir(args), args.name, args.source, urgent=bool(args.urgent), origin=args.origin, **kw)
    except notify_tokens.NotifyTokenError as e:
        print(f"levain notify token mint: {e}")
        return 2
    if args.json:
        print(json.dumps({"token": token, "record": rec}))
        return 0
    print(f"notify token {rec['name']!r}  source {rec['source']!r}  highest priority {rec['max_priority']}"
          f"{'  (may post urgent)' if rec['urgent'] else ''}")
    print(f"token (shown once, kept nowhere): {token}")
    print(f"post with:  curl -s -X POST <server>/notify -H 'Content-Type: application/json' "
          f"-H '{notify_tokens.HEADER}: {token}' -d '{{\"title\":\"...\",\"body\":\"...\"}}'")
    print("or send it as  Authorization: Bearer <token>")
    return 0


def _list(args: argparse.Namespace) -> int:
    from levain.webpush import notify_tokens

    try:
        rows = notify_tokens.list_public(_qdir(args))
    except notify_tokens.NotifyTokenError as e:     # never an empty or partial listing of a file it could not read
        print(f"levain notify token list: {e}")
        return 2
    if args.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no notify tokens")
        return 0
    for r in rows:
        print(f"{r['name']}  source {r['source']}  max {r['max_priority']}{'  urgent' if r['urgent'] else ''}  "
              f"{r['rate_per_min']}/min {r['rate_per_day']}/day  sent today {r['sent_today']}  "
              f"last used {r['last_used'] if r['last_used'] is not None else 'never'}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    from levain.webpush import notify_tokens

    if _in_agent_session():
        print("levain notify token revoke: run this from your own terminal, not inside an agent session.")
        return 2
    try:
        hit = notify_tokens.revoke(_qdir(args), args.name)
    except notify_tokens.NotifyTokenError as e:
        print(f"levain notify token revoke: {e}")
        return 2
    if not hit:
        print(f"levain notify token revoke: no live notify token is named {args.name!r}")
        return 1
    print(f"revoked {args.name!r}; its next post is refused")
    return 0
