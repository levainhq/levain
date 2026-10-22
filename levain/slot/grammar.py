"""The install helper's argv grammar (CONTRACT §5 preamble, §5.2).

Every token is matched by an exact pattern; an unknown verb, an extra or missing token, or a token
that fails its pattern is a :class:`~levain.slot.Refused` by name before anything else happens. The
one path an argv may carry is ``setup --root PATH``: its syntax is checked here, and
:func:`check_roots` resolves it against the filesystem with the stat calls injected, so a test needs
no real directory.
"""
from __future__ import annotations

import os
import re
import stat as _stat
from dataclasses import dataclass
from typing import Callable, Sequence

from levain.firing.hands import OPERATOR_RE
from levain.slot import Refused

VERBS = ("setup", "repair", "undo", "replenish", "set-retention", "set-dispatch-retain",
         "set-ceiling", "set-signers", "purge", "force-retire", "set-credential", "remove-credential")

#: Verbs that run with a live lease (CONTRACT §5.2); every other verb refuses ``live-lease``. The credential verbs
#: too: keys reads a credential on demand, so a rotation takes effect on the next exchange (design 1.8 Bootstrap).
LEASE_EXEMPT = frozenset({"purge", "force-retire", "set-credential", "remove-credential"})

#: A credential's name: the pattern of :data:`levain.broker.creds.NAME_RE`, written here because the broker module
#: needs the optional h11 and this module is imported where it may be absent; a test pins the two equal.
CREDENTIAL_NAME_RE = re.compile(r"[a-z0-9-]{1,48}")

SESSION_RE = re.compile(r"s-[0-9a-f]{16}")
SLOT_CLASSES = ("egress", "none")            # CONTRACT §5.1 class enum

MAX_SLOTS = 32                               # CONTRACT §1.1 N cap
MAX_VERIFIER_SLOTS = 16                      # CONTRACT §1.1 M cap
INDEX_SPACE = 64                             # CONTRACT §1.1 index space
RETENTION_FLOOR = 7                          # CONTRACT §5.2
DISPATCH_RETAIN_FLOOR = 1                    # CONTRACT §5.2
MAX_DAYS = 9999                              # DECISION: the contract sets no cap
MAX_INODES = 999_999_999                     # DECISION: the contract sets no cap
MAX_SECONDS = 999_999                        # DECISION: the contract sets no cap
MAX_ROOT_LEN = 1024                          # DECISION: the smaller PATH_MAX of the two hosts
MAX_ROOTS = 16                               # CONTRACT §23 (the --root record)

#: Defaults the CLI front fills in; the helper's grammar itself requires the flags (CONTRACT §1).
DEFAULT_SLOTS = 8
DEFAULT_VERIFIER_SLOTS = 2

_UINT_RE = re.compile(r"0|[1-9][0-9]*")
_SUDOERS_ALIAS_RE = re.compile(r"[A-Z][A-Z0-9_]*")
#: CONTRACT §23: sudoers reserved words, matched case-insensitively (``Defaults``, any ``*_Alias``).
_SUDOERS_RESERVED_RE = re.compile(r"defaults|.*_alias", re.IGNORECASE)
#: CONTRACT §23: a ``--root`` is absolute, with no control character and no quote, so the record that
#: carries it and every message that prints it read it as one path.
_ROOT_RE = re.compile(r"/[^\x00-\x1f\x7f\"']*")


@dataclass(frozen=True)
class InstallCommand:
    verb: str
    slots: int | None = None
    verifier_slots: int | None = None
    operator: str | None = None
    roots: tuple[str, ...] = ()
    days: int | None = None
    session: str | None = None
    slot_class: str | None = None
    index: int | None = None
    max_inodes: int | None = None
    max_seconds: int | None = None
    credential: str | None = None


def _uint(token: str, field: str, lo: int, hi: int) -> int:
    if not _UINT_RE.fullmatch(token) or len(token) > len(str(hi)):
        raise Refused("bad-token", f"{field}: {token!r}")
    value = int(token)
    if not lo <= value <= hi:
        raise Refused("out-of-range", f"{field} {value} is not in {lo}..{hi}")
    return value


def is_sudoers_alias(name: str) -> bool:
    """True for a name sudoers reads as a keyword or an alias rather than a user: ``ALL``, or any
    all-uppercase name (an alias name is uppercase), or a reserved word (``Defaults``, ``*_Alias``,
    any case; CONTRACT §23). A rule naming one would be a grant for whatever the keyword or alias
    stands for, or would not parse as a user rule at all. The one home of this rule; hands imports it."""
    return bool(_SUDOERS_ALIAS_RE.fullmatch(name) or _SUDOERS_RESERVED_RE.fullmatch(name))


def check_operator_name(name: str) -> str:
    """The operator name: the imported sudoers-safe ``OPERATOR_RE``, never a Levain account name, never
    root (DECISION: the narrowest reading of 'one operator uid'), and never a name sudoers reads as a
    keyword, an alias or a reserved word (:func:`is_sudoers_alias`), which would widen the rule."""
    if (not OPERATOR_RE.fullmatch(name) or name.startswith("_levain") or name == "root"
            or is_sudoers_alias(name)):
        raise Refused("bad-operator", repr(name))
    return name


def _root_syntax(path: str) -> str:
    if (not _ROOT_RE.fullmatch(path) or len(path) > MAX_ROOT_LEN
            or any(part in (".", "..") for part in path.split("/"))):
        raise Refused("bad-root", repr(path))
    return path


def _parse_setup(rest: Sequence[str]) -> InstallCommand:
    seen: dict[str, str] = {}
    roots: list[str] = []
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag not in ("--slots", "--verifier-slots", "--operator", "--root"):
            raise Refused("extra-argv", repr(flag))
        if i + 1 >= len(rest):
            raise Refused("missing-argv", f"{flag} needs a value")
        value = rest[i + 1]
        if flag == "--root":
            root = _root_syntax(value)
            if len(roots) >= MAX_ROOTS:
                raise Refused("out-of-range", f"at most {MAX_ROOTS} --root")
            if root in roots:
                raise Refused("duplicate-root", repr(root))
            roots.append(root)
        elif flag in seen:
            raise Refused("duplicate-flag", flag)
        else:
            seen[flag] = value
        i += 2
    for flag in ("--slots", "--verifier-slots", "--operator"):
        if flag not in seen:
            raise Refused("missing-argv", flag)
    return InstallCommand(
        "setup",
        slots=_uint(seen["--slots"], "--slots", 1, MAX_SLOTS),
        verifier_slots=_uint(seen["--verifier-slots"], "--verifier-slots", 1, MAX_VERIFIER_SLOTS),
        operator=check_operator_name(seen["--operator"]),
        roots=tuple(roots),
    )


def _exact(verb: str, rest: Sequence[str], arity: int) -> None:
    if len(rest) > arity:
        raise Refused("extra-argv", repr(rest[arity]))
    if len(rest) < arity:
        raise Refused("missing-argv", f"{verb} takes {arity} argument(s)")


def _parse_force_retire(rest: Sequence[str]) -> InstallCommand:
    if len(rest) < 2:
        raise Refused("missing-argv", "force-retire <class> <index>")
    cls, index_tok, opts = rest[0], rest[1], rest[2:]
    if cls not in SLOT_CLASSES:
        raise Refused("bad-token", f"class: {cls!r}")
    index = _uint(index_tok, "index", 0, INDEX_SPACE - 1)
    found: dict[str, int] = {}
    limits = {"--max-inodes": MAX_INODES, "--max-seconds": MAX_SECONDS}
    i = 0
    while i < len(opts):
        flag = opts[i]
        if flag not in limits:
            raise Refused("extra-argv", repr(flag))
        if flag in found:
            raise Refused("duplicate-flag", flag)
        if i + 1 >= len(opts):
            raise Refused("missing-argv", f"{flag} needs a value")
        found[flag] = _uint(opts[i + 1], flag, 1, limits[flag])
        i += 2
    return InstallCommand("force-retire", slot_class=cls, index=index,
                          max_inodes=found.get("--max-inodes"), max_seconds=found.get("--max-seconds"))


def parse_install_argv(argv: Sequence[str]) -> InstallCommand:
    """``argv`` without the program name. Returns the command or raises :class:`Refused`."""
    if isinstance(argv, (str, bytes)):
        raise Refused("bad-token", "argv must be a sequence of strings")
    argv = list(argv)
    for tok in argv:
        if not isinstance(tok, str) or not tok or "\x00" in tok:
            raise Refused("bad-token", repr(tok))
    if not argv:
        raise Refused("missing-argv", "no verb")
    verb, rest = argv[0], argv[1:]
    if verb not in VERBS:
        raise Refused("unknown-verb", repr(verb))
    if verb == "setup":
        return _parse_setup(rest)
    if verb == "force-retire":
        return _parse_force_retire(rest)
    if verb in ("set-retention", "set-dispatch-retain"):
        _exact(verb, rest, 1)
        floor = RETENTION_FLOOR if verb == "set-retention" else DISPATCH_RETAIN_FLOOR
        return InstallCommand(verb, days=_uint(rest[0], "days", floor, MAX_DAYS))
    if verb == "purge":
        _exact(verb, rest, 1)
        if not SESSION_RE.fullmatch(rest[0]):
            raise Refused("bad-token", f"session: {rest[0]!r}")
        return InstallCommand(verb, session=rest[0])
    if verb in ("set-credential", "remove-credential"):
        _exact(verb, rest, 1)
        if not CREDENTIAL_NAME_RE.fullmatch(rest[0]):
            raise Refused("bad-token", f"credential: {rest[0]!r}")
        return InstallCommand(verb, credential=rest[0])
    _exact(verb, rest, 0)   # repair, undo, replenish, set-ceiling, set-signers: stdin only
    return InstallCommand(verb)


def check_roots(roots: Sequence[str], operator_uid: int, *,
                realpath: Callable[[str], str] = os.path.realpath,
                lstat: Callable[[str], os.stat_result] = os.lstat) -> tuple[str, ...]:
    """CONTRACT §5 preamble: each ``--root`` resolved with ``realpath`` must be an existing directory
    owned by the operator, and the resolved path passes the same pattern as the argv token. At most
    :data:`MAX_ROOTS`. Returns the resolved paths, in argv order."""
    if len(roots) > MAX_ROOTS:
        raise Refused("out-of-range", f"at most {MAX_ROOTS} --root")
    out: list[str] = []
    for root in roots:
        resolved = realpath(_root_syntax(root))
        if not _ROOT_RE.fullmatch(resolved) or resolved == "/":
            raise Refused("bad-root", f"{root!r} resolves to {resolved!r}")
        try:
            st = lstat(resolved)
        except OSError:   # absent, a non-directory on the way (NotADirectoryError), unreadable
            raise Refused("root-missing", repr(resolved)) from None
        if not _stat.S_ISDIR(st.st_mode):
            raise Refused("root-not-directory", repr(resolved))
        if st.st_uid != operator_uid:
            raise Refused("root-not-operators", f"{resolved!r} is owned by uid {st.st_uid}")
        if resolved in out:
            raise Refused("duplicate-root", repr(resolved))
        out.append(resolved)
    return tuple(out)
