"""``levain ping``: the web-push commands. Registered into the main parser by :func:`register`."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "ping", help="Web push to the phone page: keys, a test ping, the notification record, paired devices, pairing.",
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
    pr = common(sub.add_parser("pair", help="Print a short pairing code to type into the installed phone app."))
    pr.add_argument("--label", default=None, help="What to call the phone in `levain ping devices` (default: phone).")
    pr.set_defaults(func=_pair)
    from levain.webpush import presence

    for word, text in (("quiet", "Hold phone pushes for a while (default 60 min), except urgent ones; one summary and the held "
                                 "pings follow when it ends."),
                       ("sleep", "Hold every phone push, urgent too, until your next keyboard or mouse input at this "
                                 "Mac (read from the Mac's input idle time); one summary and the held pings follow."),
                       ("loud", "Push every ping for a while (default 60 min), over everything else; `loud off` ends it."),
                       ("away", "Push at once until you next type here (optional override)."),
                       ("remote", "Push for a while (default 60 min) whatever the desk shows (optional override)."),
                       ("back", "Clear away, remote, quiet or sleep; a standing loud is left (`loud off`).")):
        w = common(sub.add_parser(word, help=text))
        if word in presence.TIMED:
            w.add_argument("duration", nargs="?", default=None,
                           help="30, 30m or 2h" + (", or `off`" if word == "loud" else "") + " (default 60 min, most 24 h).")
        w.add_argument("--said", default=None, metavar="WORDS",
                       help="Run inside an agent session: the words the operator told you, quoted (required there, "
                            "recorded beside the word). Which words an agent may set: presence.SEAT_WORDS and "
                            "COORDINATOR_WORDS.")
        w.set_defaults(func=_word, word=word)
    common(sub.add_parser("status", help="Whether a ping is pushed now, and why.")).set_defaults(func=_status)


def _qdir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import default_queue_dir

    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _setter(qdir: Path) -> dict | None:
    """Who is setting the word when an agent session is: ``{"agent", "session", "role"}`` (agent and session as
    :func:`band._agent_and_session` names them, role ``coordinator`` for a session Levain started as one, else ``seat``), or ``None``
    for the operator's own shell. The role is a channel label under one uid, not a boundary: it records intent."""
    from levain.consent import band

    if band.agent_process() is None:
        return None
    try:
        agent, session = band._agent_and_session(None, None)
    except ValueError:
        return None
    return {"agent": agent, "session": "" if session == band.NO_SESSION else session,
            "role": "coordinator" if band._coordinator_caller(qdir) else "seat"}


def _word(args: argparse.Namespace) -> int:
    from levain.webpush import presence

    qdir = _qdir(args)
    try:
        presence.set_word(qdir, args.word, duration=getattr(args, "duration", None), by=_setter(qdir),
                          said=getattr(args, "said", None))
    except presence.PresenceError as e:
        print(f"levain ping {args.word}: {e}")
        return 2
    try:   # a word can make held pings due (loud, away, remote, back, a quiet ended): start their push now
        from levain.webpush import held

        held.flush_if_due(qdir, throttle=False)
    except Exception:  # noqa: BLE001 - the word is written; the next gate call or the server tick flushes
        pass
    print(presence.describe(qdir))
    return 0


def _status(args: argparse.Namespace) -> int:
    from levain.webpush import presence

    print(presence.describe(_qdir(args)))
    return 0


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
        if p.get("command") is not None:
            deliveries += "; notify command " + ("started" if p["command"] else "did not start")
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


def _pair(args: argparse.Namespace) -> int:
    from levain.webpush import devices
    from levain.webpush.store import PushStore

    conn = PushStore(_qdir(args)).connection()
    try:
        code = devices.mint_short_pairing(conn, args.label, datetime.now(timezone.utc))
    finally:
        conn.close()
    minutes = int(devices.SHORT_PAIRING_TTL.total_seconds() // 60)
    print(f"pairing code: {code}\n"
          f"type it in the phone app's Pairing code box within {minutes} minutes; it works once, and a newer code "
          "replaces it")
    return 0
