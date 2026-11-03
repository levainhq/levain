"""``levain notify``: list the notify tokens a service posts a notice with.

Registered into the main parser by :func:`register`. Reads the same queue directory ``levain serve`` reads
(``--queue-dir``, default the consent queue). Listing shows no secret. Minting and revoking a token is the cockpit
Devices card's act alone (the terminal verbs were retired: the card derives the preconditions the operator cannot see).
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
    tok = sub.add_parser("token", help="List notify tokens.")
    tsub = tok.add_subparsers(dest="token_cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                        help="The consent queue directory the server uses (default: <levain home>/consent).")
        return sp

    ls = common(tsub.add_parser("list", help="The live tokens: never a secret."))
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=_list)


def _qdir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import default_queue_dir

    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


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
