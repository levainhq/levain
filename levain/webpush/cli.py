"""``levain ping``: the web-push commands. Registered into the main parser by :func:`register`."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "ping", help="Web push to the phone page: keys, a test ping, the notification record, paired devices.",
        description="Web push for the phone page. A push is a nudge that opens the page; it authorises nothing.")
    sub = p.add_subparsers(dest="ping_cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                        help="The consent queue directory (default: <levain home>/consent).")
        return sp

    kg = common(sub.add_parser("keygen", help="Create the VAPID key pair, or set/rotate its contact."))
    kg.add_argument("--sub", default=None, help="The contact claim: mailto:you@example.com or an https URL.")
    kg.add_argument("--rotate", action="store_true",
                    help="Make a new key pair (every phone must then turn notifications on again).")
    kg.set_defaults(func=_keygen)
    t = common(sub.add_parser("test", help="Send a test ping to every live subscription."))
    t.add_argument("--title", default="levain ping test")
    t.add_argument("--body", default="This is a test from `levain ping test`; it asks nothing of you.")
    t.set_defaults(func=_test)
    r = common(sub.add_parser("record", help="The notification record: pings, their deliveries and opens."))
    r.add_argument("--json", action="store_true")
    r.add_argument("--limit", type=int, default=100)
    r.set_defaults(func=_record)
    common(sub.add_parser("devices", help="List paired phones.")).set_defaults(func=_devices)
    rv = common(sub.add_parser("revoke", help="Revoke a paired phone: its token and subscriptions stop working."))
    rv.add_argument("device_id")
    rv.set_defaults(func=_revoke)


def _qdir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import default_queue_dir

    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _keygen(args: argparse.Namespace) -> int:
    from levain.webpush import vapid

    try:
        _key, public, sub = vapid.ensure_keys(_qdir(args), args.sub, rotate=args.rotate)
    except ValueError as err:
        print(f"levain ping keygen: {err}")
        return 2
    print(f"public key: {public}\ncontact:    {sub}")
    return 0


def _test(args: argparse.Namespace) -> int:
    from levain.webpush import sender

    qdir = _qdir(args)
    results = sender.send(qdir, kind="test", ref=None, title=args.title, body=args.body)
    if not results:
        print("no live subscription: turn notifications on from the phone page first")
        return 1
    for sub_id, status in results:
        print(f"subscription {sub_id}: {_said(status)}")
    return 0 if all(200 <= s < 300 for _, s in results) else 1


def _said(status: int) -> str:
    """A delivery status in words where it is not an HTTP answer: below 0 none came within the budget, 0 none came."""
    return "no answer in time" if status < 0 else str(status) if status else "no answer"


def _record(args: argparse.Namespace) -> int:
    from levain.webpush import record
    from levain.webpush.store import PushStore

    qdir = _qdir(args)
    pings = record.record(qdir, PushStore(qdir).record(args.limit))
    if args.json:
        print(json.dumps(pings, indent=2))
        return 0
    for p in pings:
        deliveries = ", ".join(f"sub {d['subscription_id']}: {_said(d['status'])}" for d in p["deliveries"]) \
            or "no subscription"
        opened = f"opened {p['opens'][0]['at']}" if p["opens"] else "not opened"
        print(f"#{p['id']} {p['at']} {p['kind']} {p['ref'] or '-'}: {p['title']} [{deliveries}] {opened}; "
              f"{p['outcome']['words']}")
    return 0


def _devices(args: argparse.Namespace) -> int:
    from levain.webpush import devices
    from levain.webpush.store import PushStore

    conn = PushStore(_qdir(args)).connection()
    try:
        rows = devices.list_devices(conn)
    finally:
        conn.close()
    if not rows:
        print("no paired device")
    for d in rows:
        state = f"revoked {d['revoked_at']}" if d["revoked_at"] else f"last seen {d['last_seen']}"
        print(f"{d['id']}  {d['label']}  paired {d['created']}  {state}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    from levain.webpush import devices
    from levain.webpush.store import PushStore

    conn = PushStore(_qdir(args)).connection()
    try:
        done = devices.revoke(conn, args.device_id, datetime.now(timezone.utc))
    finally:
        conn.close()
    print(f"revoked {args.device_id}" if done else f"no live device {args.device_id}")
    return 0 if done else 1
