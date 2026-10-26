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
         "set-ceiling", "set-signers", "purge", "force-retire", "set-credential", "remove-credential", "set-limits",
         "routes-base", "routes-trust", "routes-egress", "enrol-authority-signer", "remove-authority-signer")

#: Verbs that run with a live lease (CONTRACT §5.2); every other verb refuses ``live-lease``. The credential verbs
#: too: keys reads a credential on demand, so a rotation takes effect on the next exchange (design 1.8 Bootstrap).
#: ``set-limits`` too: a slot session held over a limit goes on when the operator raises that limit, which happens
#: while its lease is live (slot limits DESIGN §3).
#: ``routes-base`` / ``routes-trust`` too (3c-9): keys re-reads ``base.json`` at every ``Q`` and CONTROL reads the trust
#: pair at each route submission, so neither write waits for the operator's sessions to end.
#: ``routes-egress`` too (WF-1b): one root file, as ``routes-base``.
#: The authority signer verbs too (14b S5): one root file the coverage source re-reads at every refresh.
LEASE_EXEMPT = frozenset({"purge", "force-retire", "set-credential", "remove-credential", "set-limits", "routes-base",
                          "routes-trust", "routes-egress", "enrol-authority-signer", "remove-authority-signer"})

#: Q16, L3 r1 Q1: the intent marker ``routes-trust --replace`` writes in ``L/routes`` before its first move and removes
#: after the new pair is read back. While it is there a replace is half done: CONTROL's start and keys' start refuse
#: ``routes-trust-replace-unfinished`` (:func:`replace_intent_problem`), and the next ``--replace`` finishes or undoes it
#: before anything else. Here because CONTROL, keys and the install helper all import this module.
ROUTES_REPLACE_INTENT = ".replace-intent"


def replace_intent_problem(routes_dir: "os.PathLike[str] | str") -> str | None:
    """None when no half-done ``routes-trust --replace`` is marked in ``routes_dir``; else why a service must not
    start. A marker that cannot be checked (any error but its absence) is a problem too: a start never assumes it away."""
    path = os.path.join(routes_dir, ROUTES_REPLACE_INTENT)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"{path} cannot be checked ({e.strerror or e})"
    return f"{path} marks a routes-trust --replace that did not finish: run levain dispatch config trust --replace"


#: A credential's name: the pattern of :data:`levain.broker.creds.NAME_RE`, written here because the broker module
#: needs the optional h11 and this module is imported where it may be absent; a test pins the two equal.
CREDENTIAL_NAME_RE = re.compile(r"[a-z0-9-]{1,48}")

SESSION_RE = re.compile(r"s-[0-9a-f]{16}")

#: The authority signer verbs' tokens (14b S5). A key is ONE ``<type> <base64>`` (no options, no comment), the shape
#: ``routes-trust`` takes; a principal is the confirm gate's signer pattern (``levain.autonomic.confirm._SIGNER``, a
#: test pins the two equal) and never a key type's name (:data:`KEY_TYPE_NAMES`, the root file reader's
#: ``levain.autonomic.coverage.KEY_TYPES``, a test pins the two equal), which that reader would take for the key.
PUBLIC_KEY_RE = re.compile(r"([a-z0-9@.-]{1,64}) ([A-Za-z0-9+/]{1,8192}={0,2})")
PRINCIPAL_RE = re.compile(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]{0,127}")
FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
KEY_TYPE_NAMES = frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                            "ssh-rsa", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"})
MAX_PASSKEYS = 8                             # DECISION: rev 2 sets no cap
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
    source: str | None = None             # repair --source PATH (row 33, head ruling Q1)
    principal: str | None = None          # enrol-authority-signer (14b S5)
    public_key: str | None = None
    passkeys: tuple[str, ...] = ()
    confirm: bool = False                 # the laptop key's line names levain-confirm too
    key_fingerprint: str | None = None    # remove-authority-signer --key
    replace: bool = False                 # routes-trust --replace (3c design r3 Q16; R4 §5 3c-9)


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


def _key_token(flag: str, token: str) -> str:
    if not PUBLIC_KEY_RE.fullmatch(token):
        raise Refused("bad-token", f"{flag}: one '<type> <base64>' key, no options and no comment")
    return token


def _parse_enrol_signer(rest: Sequence[str]) -> InstallCommand:
    """``enrol-authority-signer --principal P --public-key K [--passkey K]... [--confirm]`` (14b S5): each flag once
    but ``--passkey`` (at most :data:`MAX_PASSKEYS`), every key distinct (by its ``<type> <base64>``)."""
    seen: dict[str, str] = {}
    passkeys: list[str] = []
    confirm = False
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--confirm":
            if confirm:
                raise Refused("duplicate-flag", flag)
            confirm = True
            i += 1
            continue
        if flag not in ("--principal", "--public-key", "--passkey"):
            raise Refused("extra-argv", repr(flag))
        if i + 1 >= len(rest):
            raise Refused("missing-argv", f"{flag} needs a value")
        value = rest[i + 1]
        if flag == "--passkey":
            if len(passkeys) >= MAX_PASSKEYS:
                raise Refused("out-of-range", f"at most {MAX_PASSKEYS} --passkey")
            passkeys.append(_key_token(flag, value))
        elif flag in seen:
            raise Refused("duplicate-flag", flag)
        else:
            seen[flag] = value
        i += 2
    for flag in ("--principal", "--public-key"):
        if flag not in seen:
            raise Refused("missing-argv", flag)
    principal = seen["--principal"]
    if not PRINCIPAL_RE.fullmatch(principal) or principal in KEY_TYPE_NAMES:
        raise Refused("bad-token", f"--principal: {principal[:40]!r}")
    public_key = _key_token("--public-key", seen["--public-key"])
    keys = [public_key, *passkeys]
    if len(set(keys)) != len(keys):
        raise Refused("duplicate-key", "a key may be named once")
    return InstallCommand("enrol-authority-signer", principal=principal, public_key=public_key,
                          passkeys=tuple(passkeys), confirm=confirm)


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
    if verb == "enrol-authority-signer":
        return _parse_enrol_signer(rest)
    if verb == "remove-authority-signer":
        if rest[:1] != ["--key"]:
            raise Refused("missing-argv" if not rest else "extra-argv", "remove-authority-signer --key <SHA256:...>")
        _exact(verb, rest, 2)
        if not FINGERPRINT_RE.fullmatch(rest[1]):
            raise Refused("bad-token", f"--key: {rest[1][:60]!r} is not SHA256:<43 base64>")
        return InstallCommand(verb, key_fingerprint=rest[1])
    if verb == "routes-trust" and rest:
        # Q16: the one flag, rotating the pin to another store or key; the request still comes on stdin.
        if rest[0] != "--replace":
            raise Refused("extra-argv", repr(rest[0]))
        _exact(verb, rest, 1)
        return InstallCommand(verb, replace=True)
    if verb == "repair" and rest:
        # Row 33 (head ruling Q1, a §5.2 amendment): ``repair --source PATH``, the operator's installed levain
        # package dir, checked as ``--root`` is. The helper checks ownership, the final component and links.
        if rest[0] != "--source":
            raise Refused("extra-argv", repr(rest[0]))
        _exact(verb, rest, 2)
        return InstallCommand(verb, source=_root_syntax(rest[1]))
    _exact(verb, rest, 0)   # repair, undo, replenish, routes-base; set-ceiling, set-signers, set-limits, routes-trust,
    #                         routes-egress: stdin only
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
