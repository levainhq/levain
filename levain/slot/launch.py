"""``levain session claude|codex --repo PATH`` (and ``adopt|discard|restore <session>``, handed whole to
:func:`levain.slot.adopt_view.main`): the launcher in front of :mod:`levain.slot.supervisor` (CONTRACT §1
"interactive supervisor", §5.1 prepare's argv; BUILD_PLAN §4 row 2).

:func:`main` parses the operator's words and hands one :class:`levain.slot.supervisor.Request` to the supervisor.
Every refusal is by name on stderr; the exit code is 0 (the session ran and its work was retained) or 1 (anything
else, a usage error included). :func:`register` is the whole of ``levain/cli.py``'s part: one ``session`` subcommand
whose words pass to :func:`main` untouched, so the two spellings cannot parse differently.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import IO, Any, Callable, Optional, Sequence

from levain.slot import Refused

VENDORS = ("claude", "codex")
#: Vendors that have an identity call in `levain.slot.identity` (not imported here: that module pulls in the root
#: helper). tests/test_slot_g3_launch.py asserts it equals `identity.IDENTITY_CALLS`' keys.
IDENTITY_GATED = ("claude", "codex")
AUTHS = ("brokered", "passthrough")      # §5.1 prepare's auth, minus `none` (the none class is dispatch's)
MAX_SLOT = 99                            # §5 preamble: slot `^[0-9]{1,2}$`; the helper checks it is configured


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:   # type: ignore[override]
        raise Refused("bad-argument", message)


def _slot(text: str) -> int:
    if not text.isascii() or not text.isdigit() or len(text) > 2 or (len(text) > 1 and text[0] == "0"):
        raise argparse.ArgumentTypeError(f"a slot index 0-{MAX_SLOT}, got {text!r}")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="levain session", description=(
        "Run Claude Code or Codex as a dedicated slot account on a clone of your repo: tracked files only, no "
        "remotes or credentials, the vendor's terminal relayed to yours with clipboard writes and terminal "
        "reports stripped. When it exits the work is kept for adopt, never merged into your repo."))
    p.add_argument("vendor", choices=VENDORS, help="The agent to run.")
    p.add_argument("--repo", required=True, type=Path, metavar="PATH",
                   help="A git work tree you own; its current branch is what the session gets.")
    p.add_argument("--slot", type=_slot, default=None, metavar="N", help="Ask for this slot (default: a free one).")
    p.add_argument("--auth", choices=AUTHS, default="brokered",
                   help="brokered: Levain's gateway holds the API key (default); passthrough: the slot's own "
                        "vendor login. With --login it signs the slot in; without it the session uses the slot's stored "
                        "login after the identity check (Codex has no server-checked identity: not a check).")
    p.add_argument("--login", action="store_true",
                   help="A login session: sign the slot in to the vendor (needs --auth passthrough).")
    return p


def parse(argv: Sequence[str]) -> Any:
    from levain.slot.supervisor import Request

    args = build_parser().parse_args(list(argv))
    if args.login and args.auth != "passthrough":
        # DECISION: a login session exists to store the slot's own vendor credential (§5.1 prepare: kind `login`
        # is the passthrough first login); a brokered slot never holds one.
        raise Refused("login-needs-passthrough", "--login signs the slot in; use it with --auth passthrough")
    if args.auth == "passthrough" and not args.login and args.vendor not in IDENTITY_GATED:
        # A vendor with no identity call cannot be started on its stored login: refused here, before a slot is taken.
        raise Refused("passthrough-needs-identity-gate", f"{args.vendor} has no identity call")
    return Request(vendor=args.vendor, repo=args.repo, auth=args.auth,
                   kind="login" if args.login else "interactive", slot=args.slot)


def main(argv: Optional[Sequence[str]] = None, *, run: Optional[Callable[[Any], int]] = None,
         err: Optional[IO[str]] = None) -> int:
    err = err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("adopt", "discard", "restore"):     # §4.8: the operator's adopt verbs, no cli.py line
        from levain.slot import adopt_view

        return adopt_view.main(argv, err=err)
    try:
        req = parse(argv)
    except SystemExit as e:     # --help (usage errors raise Refused, never exit)
        return 0 if e.code in (0, None) else 1
    except Refused as e:
        err.write(f"levain session: refused {e.name}: {e.detail}\n")
        return 1
    try:
        if run is None:
            from levain.slot.supervisor import run_session as run
        return 1 if run(req) != 0 else 0
    except Refused as e:        # Seams() refuses a host it has no layout for (`unsupported-host`)
        err.write(f"levain session: refused {e.name}: {e.detail}\n")
        return 1
    except OSError as e:        # DECISION: an OS failure no step named, by its errno text, never a traceback
        err.write(f"levain session: refused os-error: {e}\n")
        return 1


def register(subparsers: Any) -> None:
    """``levain session …``: every word after ``session`` reaches :func:`main` as it was typed (no option of the
    top-level parser applies inside it)."""
    # Hidden (Phill 10-20, np-cd6de24a "(a)"): not listed in `levain --help` until it is runnable for a user (no
    # public install path for the root helpers exists); it still parses, for the
    # VM runs. No `help=`: argparse lists only subcommands added with one (`help=SUPPRESS` prints "==SUPPRESS==" on
    # some Pythons), and the `<command>` metavar keeps the name out of the usage line.
    p = subparsers.add_parser("session",
                              add_help=False, prefix_chars="\x01")
    p.add_argument("words", nargs=argparse.REMAINDER)
    p.set_defaults(func=lambda a: main(a.words))


__all__ = ["AUTHS", "IDENTITY_GATED", "VENDORS", "build_parser", "main", "parse", "register"]
