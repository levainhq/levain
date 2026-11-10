"""``levain voice key add|revoke|list``: a premium voice provider's key (voice V1-1, design §3.2).

Kept beside the custody it drives, and registered by one call from ``levain.cli`` (the shape ``levain.agents.cli``
has), so the trust-boundary commands do not grow the main parser's file. The key is entered HERE, at a hidden prompt on
the laptop: it is never an argument, never read from a file or the environment, and never shown. The cockpit's
settings page can show that a key is set and revoke it; it cannot receive one."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

__all__ = ["add_parsers"]


def add_parsers(subparsers: argparse._SubParsersAction, common: object) -> None:
    from levain.authority import VOICE_KEY_PROVIDERS, VOICE_KEY_USES

    voice = subparsers.add_parser("voice", help="Premium voice: the provider key Levain holds for read-aloud.")
    voice_sub = voice.add_subparsers(dest="voice_cmd", required=True)
    key = voice_sub.add_parser(
        "key", help="Set, revoke and list a voice provider's API key.",
        description="A voice key record names the keychain item the Levain server reads one provider's API key "
                    "from, and the uses allowed (tts, stt). The value is typed at a hidden prompt here, written to "
                    "this Mac's login keychain and never to an argument, a file, a record or a log; the record, "
                    "signed by a laptop-presence key, names only the item. A key stands until you revoke it, from "
                    "any enrolled key (the phone included); a revoke stops the next read. The keychain item stays "
                    "where it is until you delete it.")
    key_sub = key.add_subparsers(dest="voice_key_cmd", required=True)
    add = key_sub.add_parser("add", help="Store a provider key and sign its record (a laptop-presence key).")
    add.add_argument("provider", choices=VOICE_KEY_PROVIDERS, help="The speech provider the key belongs to.")
    add.add_argument("--use", dest="uses", action="append", choices=VOICE_KEY_USES, default=None,
                     help="A use the key is allowed (tts: read aloud; stt: premium dictation, which is not "
                          "offered in this version); repeatable. Default: every use the provider serves (Inworld reads aloud only).")
    add.add_argument("--source", default=None,
                     help="The keychain item's service (default: levain-voice-<provider>-<record id>, one new item "
                          "per record). It must name no item that exists: a key is never written over one.")
    add.add_argument("--account", default=None, help="The keychain item's account (default: levain-voice).")
    add.add_argument("--replace", action="store_true",
                     help="Supersede this provider's existing key, in the same signed link.")
    add.add_argument("--yes", action="store_true",
                     help="Store and sign the record shown without asking (required when stdin is not a terminal).")
    add.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                     help="An enrolled laptop-presence key that signs the record.")
    common(add)  # type: ignore[operator]
    add.set_defaults(func=_cmd_add)
    revoke = key_sub.add_parser("revoke", help="Revoke a voice key record (any enrolled key).")
    revoke.add_argument("record_id", help="The record's id, as `levain voice key list` shows it.")
    revoke.add_argument("--signing-key", dest="signing_key", type=Path, required=True,
                        help="An enrolled key that signs the revoke.")
    common(revoke)  # type: ignore[operator]
    revoke.set_defaults(func=_cmd_revoke)
    lst = key_sub.add_parser("list", help="List the voice key records the authority log holds.")
    common(lst)  # type: ignore[operator]
    lst.set_defaults(func=_cmd_list)


#: printed with every add, ``--yes`` included, before anything is stored or signed: a voice key link is
#: levain-authority/5, and an older replay refuses the log at it (tests/test_voice_custody.py, the older-replay test)
UPGRADE_ORDER_LINE = (
    "  upgrade order: this is a levain-authority/5 link. Once one is signed, a levain older than this build refuses "
    "this whole authority log (its replay stops at that link, so it allows nothing and can append no link, a revoke "
    "included). Every levain that reads this log, each desk and daemon, must be this build or newer before "
    "you sign.")


def _confirm(preview: object, yes: bool) -> bool:
    """Show the record exactly as it will be signed and the upgrade order, then ask: y/N on a terminal; with no
    terminal only ``--yes`` goes on. Nothing is stored in the keychain, nor signed, before this answers yes."""
    from levain.cockpit.display_safe import visible

    rec = preview.record  # type: ignore[attr-defined]
    print(f"voice key {rec['id']} for {rec['provider']}: {', '.join(rec['uses'])}")
    print(f"  keychain item: service {visible(rec['source'])!r}, account {visible(rec['account'])!r} (a new item)")
    print("  stands until revoked; the key itself is stored in the keychain, never in the record")
    if preview.replaces:  # type: ignore[attr-defined]
        gone = ", ".join(visible(x) for x in preview.replaces)  # type: ignore[attr-defined]
        print(f"  replaces: {gone} (its keychain item is left in place)")
    print(UPGRADE_ORDER_LINE)
    if yes:
        return True
    if not sys.stdin.isatty():
        print("not a terminal: pass --yes to store and sign the record shown", file=sys.stderr)
        return False
    try:
        answer = input("store the key and sign this record? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _cmd_add(args: argparse.Namespace) -> int:
    from levain.cli import _cockpit_refusals, _cockpit_root, _utc_now
    from levain.cockpit.keys import ssh_signer
    from levain.voice import custody

    try:
        entered = getpass.getpass(f"{args.provider} API key (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("add refused: no key was entered", file=sys.stderr)
        return 1
    value = custody.Secret(entered)
    del entered
    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        rec, warning = custody.add_key(
            _cockpit_root(args), provider=args.provider, uses=args.uses or custody.default_uses(args.provider),
            source=args.source,
            account=args.account or custody.DEFAULT_ITEM_ACCOUNT, value=value, signer_fp=signer_fp, sign=sign,
            clock=_utc_now, replace=args.replace, confirm=lambda p: _confirm(p, args.yes))
    except (*custody._REFUSALS, *_cockpit_refusals()) as exc:
        print(f"add refused: {exc}", file=sys.stderr)
        return 1
    print(f"set: {rec['id']} ({rec['provider']}), signed as shown; it stands until revoked")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    from levain.cli import _cockpit_refusals, _cockpit_root, _utc_now
    from levain.cockpit.keys import ssh_signer
    from levain.voice import custody

    try:
        signer_fp, sign = ssh_signer(args.signing_key)
        seq, warning = custody.revoke_key(_cockpit_root(args), args.record_id, signer_fp=signer_fp, sign=sign,
                                          clock=_utc_now)
    except (*custody._REFUSALS, *_cockpit_refusals()) as exc:
        print(f"revoke refused: {exc}", file=sys.stderr)
        return 1
    print(f"revoked: {args.record_id} at seq {seq}; the next read finds no key (the keychain item is left in place)")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from levain.cli import _cockpit_refusals, _cockpit_root, _utc_now
    from levain.cockpit.display_safe import visible
    from levain.voice import custody

    try:
        rows = custody.list_keys(_cockpit_root(args), now=_utc_now())
    except (*custody._REFUSALS, *_cockpit_refusals()) as exc:
        print(f"list refused: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no voice keys")
    for r in rows:
        print(f"{visible(r['id'])}  {r['provider']}  {','.join(r['uses'])}  service {visible(r['source'])}  "
              f"account {visible(r['account'])}  stands until revoked")
    return 0
