"""``levain ping``: the web-push commands. Registered into the main parser by :func:`register`."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "ping", help="Web push to paired phones: keys, a test ping, the notification record, presence.",
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
    t.add_argument("--device", default=None, metavar="d-ID",
                   help="Send only to this paired device's subscriptions (a cockpit device id, as `levain cockpit "
                        "pair --list` prints it).")
    t.set_defaults(func=_test)
    r = common(sub.add_parser("record", help="The notification record: pings, their deliveries and opens."))
    r.add_argument("--json", action="store_true")
    r.add_argument("--limit", type=int, default=100)
    r.set_defaults(func=_record)
    rc = common(sub.add_parser(
        "receipts", help="What each phone said about each push: received, shown, tapped, duplicates.",
        description="Device receipts per ping and phone, joined with the push service's answer and the tap. A ping "
                    "with no receipt is `no receipt yet`, never lost; `silent` is said only when that phone reported "
                    "something LATER than this ping reached it."))
    rc.add_argument("--since", default=None, metavar="7d", help="Only pings from the last N days, hours or minutes (7d, 12h, 30m).")
    rc.add_argument("--ref", default=None, help="Only pings about this ref (request:<id> or question:<n>).")
    rc.add_argument("--json", action="store_true", help="The stable JSON form (levain.webpush.receipts docstring).")
    rc.set_defaults(func=_receipts)
    from levain.webpush import presence

    for word, text in (("quiet", "Hold phone pushes for a while (default 60 min), except what needs you (approvals and "
                                 "attention items); one summary and the held pings follow when it ends."),
                       ("sleep", "Hold phone pushes until you say another word or type a message into a session, except approvals and "
                                 "high-priority items; one summary and the held pings follow."),
                       ("loud", "Push every ping for a while (default 60 min), over everything else; `loud off` ends it."),
                       ("away", "Push to your devices at once until you say another word or type a message into a session."),
                       ("back", "Clear away, quiet or sleep and return to your standing default; a standing loud is left "
                                "(`loud off`).")):
        w = common(sub.add_parser(word, help=text))
        if word in presence.TIMED:
            w.add_argument("duration", nargs="?", default=None,
                           help="30, 30m or 2h" + (", or `off`" if word == "loud" else "") + " (default 60 min, most 24 h).")
        w.add_argument("--said", default=None, metavar="WORDS",
                       help="Run inside an agent session: the words the operator told you, quoted (required there, "
                            "recorded beside the word). Which words an agent may set: presence.SEAT_WORDS and "
                            "COORDINATOR_WORDS. (Use --said=\"-text\" if the quote starts with a dash.)")
        w.add_argument("--drop", default=None, metavar="DROP_ID",
                       help="The coordinator only: the drop that holds the words quoted by --said (a drop reaches the "
                            "coordinator in its brief file, not as a prompt, so it is the evidence for the quote).")
        w.set_defaults(func=_word, word=word)

    def ending(sp: argparse.ArgumentParser) -> None:
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--for", dest="for_", default=None, metavar="DUR",
                       help="Only for this long (30, 30m or 2h; 1 minute to 24 h), then your standing default again.")
        g.add_argument("--until-input", dest="until_input", action="store_true",
                       help="Only until you say another word or type a message into a session (sensed keyboard or mouse input does not end it), then your standing default again.")

    dv = common(sub.add_parser(
        "devices", help="Whether pings reach your devices: on, off, or follow where you are (operator only).",
        description="Devices on: pings reach your phone and cockpit. Off: they wait for the desk. Follow (the standing default): "
                    "they reach your devices once you have had no input at this Mac for `--after` (default 10 min), or when "
                    "you are on a remote terminal. With neither --for nor --until-input the setting becomes your standing "
                    "default; with one of them it holds only that long and your standing default returns."))
    dv.add_argument("mode", choices=("on", "off", "follow"))
    dv.add_argument("--after", default=None, metavar="DUR", help="With follow: how long without input counts as away (30m, 15, 1h; 1 minute to 24 h).")
    ending(dv)
    dv.set_defaults(func=_devices)
    lt = common(sub.add_parser(
        "lets", help="What reaches your devices: everything, needs (Approvals and needs), approvals or nothing (operator only).",
        description="Everything; needs (Approvals and needs: approvals plus attention items); approvals (Approvals only); nothing (Nothing). Held pings wait for "
                    "your devices (or the setting to widen) and are pushed then. With neither --for nor --until-input the "
                    "setting becomes your standing default."))
    lt.add_argument("level", choices=_intent_lets())
    ending(lt)
    lt.set_defaults(func=_lets)
    st = common(sub.add_parser("status", help="Whether a ping is pushed now, and why: the one line the cockpit shows too."))
    st.add_argument("--json", action="store_true", help="The full view the cockpit paints from.")
    st.set_defaults(func=_status)
    lg = common(sub.add_parser("log", help="Every presence word set or refused: who, which role, the quote, the outcome."))
    lg.add_argument("--limit", type=int, default=50, help="The newest N rows (default 50).")
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(func=_log)
    mg = common(sub.add_parser(
        "migrate", help="One-time fold of presence.json, route.json and policy.json into the one intent record.",
        description="Idempotent: a second run changes nothing. The old files move to webpush/pre-intent/<stamp>/ with a receipt; "
                    "`--undo` puts them back byte for byte. `--dry-run` shows the record and the line it would give."))
    mg.add_argument("--dry-run", dest="dry_run", action="store_true")
    mg.add_argument("--undo", action="store_true", help="Restore the files the last migration moved, and remove the record.")
    mg.add_argument("--force", action="store_true", help="With --undo: even though the record changed since the migration.")
    mg.set_defaults(func=_migrate)
    pol = sub.add_parser("policy", help="RETIRED: what each preset lets through (`policy show`); change it with `levain ping lets`.",
                         description="The notification policy is retired: each preset says what it lets through, and `levain ping "
                                     "lets` changes it for a stretch. `show` prints the presets; `set` and `reset` refuse.")
    psub = pol.add_subparsers(dest="policy_cmd", required=True)
    ps = common(psub.add_parser("show", help="What each preset lets through."))
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=_policy_show)
    pset = common(psub.add_parser("set", help="Refused: the policy is retired (exit 2)."))
    pset.add_argument("mute", nargs="?")
    pset.add_argument("kind_class", nargs="?")
    pset.add_argument("value", nargs="?")
    pset.set_defaults(func=_policy_retired, what="set")
    common(psub.add_parser("reset", help="Refused: the policy is retired (exit 2).")).set_defaults(func=_policy_retired, what="reset")


def _intent_lets() -> tuple[str, ...]:
    from levain.webpush import intent

    return intent.LETS


def _qdir(args: argparse.Namespace) -> Path:
    from levain.consent.queue import default_queue_dir

    return args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()


def _setter(qdir: Path) -> dict | None:
    """Who is setting the word when an agent session is: ``{"agent", "session", "role"}`` (agent and session as
    :func:`band._agent_and_session` names them, role ``coordinator`` for a session Levain started as one, else ``seat``), or ``None``
    for the operator's own shell. The role is a channel label under one uid, not a boundary: it records intent."""
    from levain.consent import band
    from levain.webpush import presence

    agent = band.agent_process()   # detected once: a second scan that comes back empty must not make a seat the operator
    if agent is None:
        return None
    try:
        agent, session = band._agent_and_session(agent, None)
    except ValueError as e:
        raise presence.PresenceError(f"an agent session is detected but cannot be named ({e}): the word is not set") from e
    session = "" if session == band.NO_SESSION else session
    role = "seat"
    if session:   # the role comes from the selected session alone, never from another agent's inherited variable
        try:
            from levain.coordinator.first_reader import is_coordinator_session

            if is_coordinator_session(qdir, session):
                role = "coordinator"
        except Exception:  # noqa: BLE001 - a role that cannot be checked is the narrower one
            pass
    return {"agent": agent, "session": session, "role": role}


def _evidence(by: dict, said: str | None, qdir: Path, drop: str | None):
    """Whether ``said`` is in what the operator typed to the setter's session (or, for the coordinator, in the drop it
    names): a :class:`levain.webpush.quote.Match`, never an error."""
    import os
    import time

    from levain.webpush import quote

    try:
        cwd = os.getcwd()
    except OSError:   # the working directory was removed: that root is dropped
        cwd = None
    roots = [Path(p) for p in (os.environ.get("CLAUDE_PROJECT_DIR"), cwd) if p and os.path.isabs(p)]
    try:
        return quote.match(by["agent"], by["session"], said or "", now=time.time(), roots=roots, directory=qdir,
                           drop=drop if by["role"] == "coordinator" else None)
    except (OSError, RuntimeError, ValueError) as e:
        return quote.Match(False, None, f"transcript check failed ({type(e).__name__})")


def _flush(qdir: Path) -> None:
    """A write can make held pings due (loud, away, back, a widened setting, a mute ended): start their push now, outside
    the lock. The library does not (a write that spawns a process under the lock is a hazard); callers do."""
    try:
        from levain.webpush import held

        held.flush_if_due(qdir, throttle=False)
    except Exception:  # noqa: BLE001 - the record is written; the next gate call flushes
        pass


def _word(args: argparse.Namespace) -> int:
    from levain.webpush import intent, presence

    qdir = _qdir(args)
    drop = getattr(args, "drop", None)
    try:
        by = _setter(qdir)
        if drop and (by is None or by["role"] != "coordinator"):
            raise presence.PresenceError("--drop is the coordinator's evidence for its quote; "
                                         "the operator's own word and a seat's need none")
        intent.apply_preset(qdir, args.word, duration=getattr(args, "duration", None), by=by,
                            said=getattr(args, "said", None),
                            match=None if by is None else _evidence(by, getattr(args, "said", None), qdir, drop))
    except presence.PresenceError as e:
        print(f"levain ping {args.word}: {e}")
        return 2
    _flush(qdir)
    print(intent.describe_line(qdir))
    return 0


def _until(args: argparse.Namespace):
    """``--for DUR`` -> the duration, ``--until-input`` -> ``input``, neither -> ``switch`` (the standing default)."""
    if args.for_ is not None:
        return args.for_
    return "input" if args.until_input else "switch"


def _settings(args: argparse.Namespace, what: str, **fields) -> int:
    from levain.webpush import intent, presence

    qdir = _qdir(args)
    try:
        intent.apply_settings(qdir, until=_until(args), by=_setter(qdir), **fields)
    except presence.PresenceError as e:
        print(f"levain ping {what}: {e}")
        return 2
    _flush(qdir)
    print(intent.describe_line(qdir))
    return 0


def _devices(args: argparse.Namespace) -> int:
    if args.after is not None and args.mode != "follow":
        print("levain ping devices: --after is how long without input counts as away; it goes with `follow`")
        return 2
    return _settings(args, "devices", devices=args.mode, follow_after=args.after)


def _lets(args: argparse.Namespace) -> int:
    return _settings(args, "lets", lets_through=args.level)


def _status(args: argparse.Namespace) -> int:
    from levain.webpush import intent

    qdir = _qdir(args)
    if getattr(args, "json", False):
        print(json.dumps(intent.view(qdir), indent=2, default=str))
    else:
        print(intent.describe_line(qdir))
    return 0


def _migrate(args: argparse.Namespace) -> int:
    from levain.webpush import intent, presence

    qdir = _qdir(args)
    if args.undo and args.dry_run:
        print("levain ping migrate: --undo and --dry-run do not go together")
        return 2
    if args.force and not args.undo:
        print("levain ping migrate: --force goes with --undo")
        return 2
    try:
        if args.undo:
            got = intent.unmigrate(qdir, force=args.force)
            print(got["status"] + (f" (from {got['backup']})" if got.get("backup") else ""))
            for name in got.get("restored") or []:
                print(f"- restored {name}")
            print(intent.describe_line(qdir))
            return 0
        got = intent.migrate(qdir, dry_run=args.dry_run)
    except presence.PresenceError as e:
        print(f"levain ping migrate: {e}")
        return 2
    print(got["status"])
    for line in got.get("lines") or []:
        print(f"- {line}")
    if got.get("backup"):
        print(f"old files kept in {got['backup']} (levain ping migrate --undo puts them back)")
    if got["status"] == "dry run":
        print("record: " + json.dumps(got.get("record"), sort_keys=True, default=str))
        print("the line it would give: " + str(got.get("line")))
    else:
        print(intent.describe_line(qdir))
    return 0


def _policy_show(args: argparse.Namespace) -> int:
    from levain.webpush import intent, presence

    view = presence.policy_view(_qdir(args))
    if args.json:
        print(json.dumps(view, indent=2))
        return 0
    print("what each preset lets through (the notification policy is retired):")
    for name, r in intent.PRESET_TABLE.items():
        end = "your next word or a message you type into a session" if r["until"] == "input" else "a timer (default 60 min)"
        print(f"  {name:<6} devices {r['devices']:<3}  lets through {intent._LETS_WORDS[r["lets_through"]]:<19}  until {end}")
    print("  back   clears the preset and returns to your standing default (a standing loud is left)")
    print("retired: change it with `levain ping lets everything|needs|approvals|nothing [--for 1h | --until-input]`")
    return 0


def _policy_retired(args: argparse.Namespace) -> int:
    from levain.webpush import presence

    print(f"levain ping policy {args.what}: {presence.POLICY_RETIRED}")
    return 2


def _log(args: argparse.Namespace) -> int:
    from levain.webpush import presence

    rows = presence.read_log(_qdir(args), limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no presence word has been set or refused")
    for r in rows:
        print(presence.log_line(r))
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
    device = args.device
    if device is not None:
        if re.fullmatch(r"d-[0-9a-f]{12}", device) is None:
            print(f"levain ping test: --device {device!r} is not a device id (d- and 12 hex digits, as "
                  "`levain cockpit pair --list` prints it)")
            return 2
        device = "paired-" + device
    results = sender.send(qdir, kind="test", ref=None, title=args.title, body=args.body, device=device)
    if not results:
        print("no live subscription for that device" if device is not None
              else "no live subscription: turn notifications on from the phone page first")
        return 1
    for sub_id, status in results:
        print(f"subscription {sub_id}: {_said(status)}")
    return 0 if all(200 <= s < 300 for _, s in results) else 1


def _said(status: int) -> str:
    """A delivery status in words where it is not an HTTP answer: below 0 none came within the budget, 0 none came."""
    return "no answer in time" if status < 0 else str(status) if status else "no answer"


def _receipts(args: argparse.Namespace) -> int:
    from levain.webpush import receipts
    from levain.webpush.store import PushStore

    try:
        since = None if args.since is None else receipts.parse_since(args.since)
    except ValueError as err:
        print(f"levain ping receipts: {err}")
        return 2
    report = receipts.build(PushStore(_qdir(args)), since=since, ref=args.ref)
    print(json.dumps(report, indent=2) if args.json else receipts.text(report))
    return 0


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
