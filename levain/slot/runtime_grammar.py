"""The runtime helper's argv + stdin grammar and its output lines (CONTRACT §5 preamble, §5.1, §5.3).

This is the first wall of the root NOPASSWD entry point. It is PURE: it reads no file, stats no path,
opens no socket and reads no environment. The caller identity (operator vs ``_levain_dispatch``) and the
configured slots are parameters; reading ``SUDO_UID`` and the slot configuration is the verb layer's.

Order of refusal, each by name (CONTRACT §5 preamble: "anything else = refusal by name before any
filesystem act"): token sanity -> verb -> the caller-verb matrix (§5.1 ``_levain_dispatch`` rule) ->
the verb's exact argv shape -> each token's exact pattern. Stdin is checked separately by
:func:`parse_stdin`: size cap before any decode, then ``levain.contracts.canon.parse`` (its refusals:
duplicate keys, floats, NaN/Infinity, a BOM, non-UTF-8, lone surrogates, depth), then canonical bytes,
then the verb's allowed-keys schema. :func:`bind_stdin` cross-checks the two (the ticket rule).

What this module does NOT check, because it needs a record or the host (the verb layer's, session 2+):
that a session exists or its alloc carries a ticket this caller minted (§5.1 dispatch rule, ticket-bound),
the full (class, kind, vendor, auth, routes, hooks, ticket facts) matrix of ``render`` step (2), the
per-vendor route counts and base-map membership of §5.3 ``routes``, ``vendor`` == prepare's, ``model`` /
``verifier_steps`` == the ticket's, ``allow_own_loopback`` against the host (§21: false on macOS), and
that ``driver`` names a template in the root-owned set.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from typing import Collection, Mapping, Sequence, Union

from levain import unshowable
from levain.contracts import ContractError
from levain.contracts import canon
from levain.slot import Refused
from levain.slot.grammar import INDEX_SPACE, SESSION_RE, SLOT_CLASSES

# --- §5.1 verbs and enums -------------------------------------------------------------------------

VERBS = ("prepare", "render", "start", "resume", "teardown", "confirm-identity",
         "adopt-open", "adopt-verify", "discard", "restore", "status")

KINDS = ("interactive", "login", "dispatch", "planner", "verifier")   # §5.1 prepare
VENDORS = ("claude", "codex")                                          # §5.1 prepare, §5.3 vendor
AUTHS = ("brokered", "passthrough", "none")                            # §5.1 prepare
CONFIRM_ANSWERS = ("yes", "no")                                        # §5.1 confirm-identity
#: §5.1 adopt-verify: ``--bundle-digest sha256:<64 lowercase hex>``, the digest adopt-open's answer gave.
BUNDLE_DIGEST_FLAG = "--bundle-digest"
_BUNDLE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)

#: §5.1 Lease column: these verbs run on a live lease (prepare's is the stdin nonce); the rest are
#: lease-free (adopt-open, adopt-verify, discard, restore are caller-bound; status is read-only).
LEASE_VERBS = frozenset({"prepare", "render", "start", "resume", "teardown", "confirm-identity"})

#: §5.1 verbs that read a stdin blob; every other verb takes none ("—").
STDIN_VERBS = frozenset({"prepare", "render"})

OPERATOR, DISPATCH = "operator", "dispatch"
CALLERS = (OPERATOR, DISPATCH)

#: §5.1 the ``_levain_dispatch`` rule: every verb except these four. ``adopt-verify`` is NOT excepted (c7.10 fix1
#: F2, head 1022+20 Q1): dispatch's consumer verifies a dispatch-class session's artifact; the verb layer splits the
#: caller check by class (``runtime_helper.verify_caller``).
DISPATCH_FORBIDDEN = frozenset({"adopt-open", "discard", "restore", "confirm-identity"})

#: DECISION (narrowest reading of §3.1 + §3.5): the operator's leases carry no ticket, so the operator
#: prepares only the operator kinds; ``_levain_dispatch`` prepares only the ticketed kinds (ticket kinds
#: executor|planner|verifier, §3.3 RUN-TICKET-MINT; executor runs as session kind ``dispatch``).
KINDS_FOR_CALLER: Mapping[str, frozenset[str]] = {
    OPERATOR: frozenset({"interactive", "login"}),
    DISPATCH: frozenset({"dispatch", "planner", "verifier"}),
}

# --- §5 preamble patterns -------------------------------------------------------------------------

#: slot ``^[0-9]{1,2}$``; DECISION: no leading zero, so one index has one spelling ("07" is refused).
_SLOT_RE = re.compile(r"0|[1-9][0-9]?", re.ASCII)

STDIN_MAX = 64 * 1024                       # §5 preamble: the stdin cap
HANDLE_BYTES = 32                           # header shared encodings + V15: handle size
_HANDLE_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)

# --- §5.3 render blob -----------------------------------------------------------------------------

RENDER_FORMAT = "levain-session-render/1"
RENDER_KEYS = frozenset({"format", "vendor", "routes", "model", "git_identity", "hooks", "mcp",
                         "allow_own_loopback", "repo_label", "verifier_steps", "driver"})
#: DECISION: the keys every render needs; the rest are optional by their §5.3 rule.
RENDER_REQUIRED = frozenset({"format", "vendor", "routes"})
HOOKS = frozenset({"levain_gate", "levain_queue"})

_ROUTE_ID_RE = re.compile(r"[a-z0-9-]{1,48}", re.ASCII)                 # §4.3 policy ``route``
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,128}", re.ASCII)           # §5.3 model
_GIT_NAME_RE = re.compile(r"[^\x00-\x1f<>]{1,64}")                      # §5.3 git_identity.name
_GIT_EMAIL_RE = re.compile(r"[^\x00-\x1f\s<>]{3,254}")                  # §5.3 git_identity.email
#: DECISION: §5.3 says "template name" with no pattern; a lowercase identifier, never a path.
_TEMPLATE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)

MAX_ROUTES = 64                 # DECISION: §5.3 bounds the count only per vendor; a hard cap on the list
MAX_REPO_LABEL = 256            # DECISION: §10.3 gives the alphabet, not a length
MAX_STEPS = 16                  # §5.6 / V20: step count
MAX_STEP_ARGV = 64              # §5.6: argv string count
MAX_STEP_ARG_BYTES = 4 * 1024   # §5.6: bytes per argv string
MAX_STEP_WALL_S = 900           # §5.6 / V20: step wall
_DISPLAY_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Me", "Nd", "Nl", "No",
                                 "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk", "So", "Zs"})
_BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")

#: Output refusal names: the name is ours, never input bytes (attacker data rides ``detail`` only).
#: The qualifier (after ``:``) may also carry ASCII uppercase and ``.``, so ``fsck:<id>`` (git's camelCase ids,
#: from the closed list) and ``git-below-floor:<version>`` ride verbatim (5d-i S2); the base stays lowercase.
_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?::[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*)?", re.ASCII)


# --- typed results ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeRequest:
    verb: str
    caller: str
    session: str | None = None
    slot_class: str | None = None
    kind: str | None = None
    vendor: str | None = None
    auth: str | None = None
    slot: int | None = None
    answer: str | None = None      # confirm-identity yes|no
    bundle_digest: str | None = None   # adopt-verify ``sha256:<hex>``

    @property
    def needs_lease(self) -> bool:
        return self.verb in LEASE_VERBS


@dataclass(frozen=True)
class PrepareInput:
    lease_nonce: str
    ticket: str | None = None


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str


@dataclass(frozen=True)
class VerifierStep:
    argv: tuple[str, ...]
    wall_s: int


@dataclass(frozen=True)
class RenderBlob:
    vendor: str
    routes: tuple[str, ...]
    model: str | None = None
    git_identity: GitIdentity | None = None
    hooks: frozenset[str] = frozenset()
    mcp: tuple[str, ...] = ()
    allow_own_loopback: bool | None = None
    repo_label: str | None = None
    verifier_steps: tuple[VerifierStep, ...] | None = None
    driver: str | None = None


SlotConfig = Mapping[str, Union[int, Collection[int]]]


# --- argv ------------------------------------------------------------------------------------------

def _enum(token: str, field: str, allowed: Sequence[str]) -> str:
    if token not in allowed:
        raise Refused("bad-token", f"{field}: {token[:64]!r}")
    return token


def _session(token: str) -> str:
    if not SESSION_RE.fullmatch(token):
        raise Refused("bad-token", f"session: {token[:64]!r}")
    return token


def _arity(verb: str, rest: Sequence[str], lo: int, hi: int) -> None:
    if len(rest) > hi:
        raise Refused("extra-argv", f"{verb}: {rest[hi][:64]!r}")
    if len(rest) < lo:
        raise Refused("missing-argv", f"{verb} takes {lo}..{hi} argument(s)" if lo != hi
                      else f"{verb} takes {lo} argument(s)")


def _configured(configured_slots: SlotConfig, slot_class: str) -> frozenset[int]:
    """The configured indices of one class. An int ``n`` means indices ``0..n-1``; a collection names
    them (a replenished slot takes a new index, §1.1, so after ``replenish`` the set is not a prefix)."""
    try:
        value = configured_slots[slot_class]
    except (KeyError, TypeError):
        raise Refused("bad-config", f"no configured slots for class {slot_class!r}") from None
    if type(value) is int:
        if not 0 <= value <= INDEX_SPACE:
            raise Refused("bad-config", f"{slot_class}: {value}")
        return frozenset(range(value))
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise Refused("bad-config", f"{slot_class}: {type(value).__name__}")
    out = frozenset(value)
    if any(type(i) is not int or not 0 <= i < INDEX_SPACE for i in out):
        raise Refused("bad-config", f"{slot_class}: an index outside 0..{INDEX_SPACE - 1}")
    return out


def _slot(token: str, slot_class: str, configured_slots: SlotConfig) -> int:
    if not _SLOT_RE.fullmatch(token):
        raise Refused("bad-token", f"slot: {token[:64]!r}")
    index = int(token)
    if index >= INDEX_SPACE:          # §1.1 index space
        raise Refused("out-of-range", f"slot {index} is not below {INDEX_SPACE}")
    if index not in _configured(configured_slots, slot_class):   # §5 preamble: slot < configured
        raise Refused("slot-not-configured", f"{slot_class} slot {index}")
    return index


def _parse_prepare(rest: Sequence[str], caller: str, configured_slots: SlotConfig) -> RuntimeRequest:
    _arity("prepare", rest, 4, 5)
    slot_class = _enum(rest[0], "class", SLOT_CLASSES)
    kind = _enum(rest[1], "kind", KINDS)
    vendor = _enum(rest[2], "vendor", VENDORS)
    auth = _enum(rest[3], "auth", AUTHS)
    if kind not in KINDS_FOR_CALLER[caller]:
        raise Refused("kind-not-allowed-for-caller", f"{caller} may not prepare kind {kind}")
    # §5.6: kind verifier is a none-class session, and ONLY it is: every other kind runs in an egress slot.
    # Both directions, so no non-verifier session is handed a verifier pair.
    if (kind == "verifier") != (slot_class == "none"):
        raise Refused("class-kind-mismatch", f"kind {kind} is not {slot_class} class")
    # §5.3 routes "none class: empty" and §5.1 render (5b) "a none-class render has no front": a none-class
    # session holds no brokered and no passthrough route, so its auth is none.
    if slot_class == "none" and auth != "none":
        raise Refused("class-auth-mismatch", f"none class with auth {auth}")
    slot = _slot(rest[4], slot_class, configured_slots) if len(rest) == 5 else None
    return RuntimeRequest("prepare", caller, slot_class=slot_class, kind=kind, vendor=vendor, auth=auth,
                          slot=slot)


def parse_runtime_argv(argv: Sequence[str], *, configured_slots: SlotConfig, caller: str) -> RuntimeRequest:
    """``argv`` without the program name. Returns the request or raises :class:`Refused` by name.

    ``caller`` is ``"operator"`` or ``"dispatch"`` (which sudoers rule admitted the call; the verb layer
    derives it from ``SUDO_UID``). ``configured_slots`` maps each class (``egress``, ``none``) to its
    configured slot count or index set; it is read only for ``prepare <...> <slot>``."""
    if caller not in CALLERS:
        raise Refused("unknown-caller", repr(caller)[:64])
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise Refused("bad-token", "argv must be a sequence of strings")
    argv = list(argv)
    for tok in argv:
        if not isinstance(tok, str) or not tok or "\x00" in tok:
            raise Refused("bad-token", repr(tok)[:64])
    if not argv:
        raise Refused("missing-argv", "no verb")
    verb, rest = argv[0], argv[1:]
    if verb not in VERBS:
        raise Refused("unknown-verb", repr(verb)[:64])
    if caller == DISPATCH and verb in DISPATCH_FORBIDDEN:      # §5.1 the _levain_dispatch rule
        raise Refused("verb-not-allowed-for-caller", f"dispatch may not {verb}")
    if verb == "prepare":
        return _parse_prepare(rest, caller, configured_slots)
    if verb == "status":
        _arity(verb, rest, 0, 1)
        return RuntimeRequest(verb, caller, session=_session(rest[0]) if rest else None)
    if verb == "confirm-identity":
        _arity(verb, rest, 2, 2)
        return RuntimeRequest(verb, caller, session=_session(rest[0]),
                              answer=_enum(rest[1], "answer", CONFIRM_ANSWERS))
    if verb == "adopt-verify":   # exactly <session> --bundle-digest sha256:<hex>, in that order
        _arity(verb, rest, 3, 3)
        session = _session(rest[0])
        _enum(rest[1], "flag", (BUNDLE_DIGEST_FLAG,))
        if not _BUNDLE_DIGEST_RE.fullmatch(rest[2]):
            raise Refused("bad-token", f"bundle digest: {rest[2][:80]!r}")
        return RuntimeRequest(verb, caller, session=session, bundle_digest=rest[2])
    # render, start, resume, teardown, adopt-open, discard, restore: exactly <session>.
    # DECISION: §5.1's "`discard` / `restore <session>`" row reads as both taking <session>.
    _arity(verb, rest, 1, 1)
    return RuntimeRequest(verb, caller, session=_session(rest[0]))


# --- stdin -----------------------------------------------------------------------------------------

def _parse_refusal(e: ContractError) -> Refused:
    """Name ``canon.parse``'s refusals (its messages name the rule; the names are pinned by test)."""
    msg = str(e)
    for prefix, name in (("duplicate key", "duplicate-key"), ("float", "float-refused"),
                         ("NaN", "float-refused"), ("Infinity", "float-refused"),
                         ("-Infinity", "float-refused"), ("a byte-order mark", "stdin-bom"),
                         ("nesting deeper", "stdin-too-deep"), ("integer", "int-out-of-range"),
                         ("not a convertible integer", "int-out-of-range")):
        if msg.startswith(prefix):
            return Refused(name, msg[:128])
    return Refused("stdin-not-json", msg[:128])


def _handle(value: object, field: str) -> str:
    """Header shared encodings: ``HANDLE_BYTES`` random bytes, base64url UNPADDED, one spelling (the spare bits of
    the last character are zero: decode then re-encode must give the same text)."""
    if type(value) is not str:
        raise Refused("wrong-type", f"{field} is a string")
    if not _HANDLE_RE.fullmatch(value):
        raise Refused("bad-handle", field)
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except (binascii.Error, ValueError):
        raise Refused("bad-handle", field) from None
    if len(raw) != HANDLE_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise Refused("bad-handle", field)
    return value


def _keys(obj: dict, allowed: frozenset[str], required: frozenset[str], where: str) -> None:
    for k in sorted(obj):
        if k not in allowed:
            raise Refused("unknown-key", f"{where}: {k[:64]!r}")
    for k in sorted(required):
        if k not in obj:
            raise Refused("missing-key", f"{where}: {k}")


def _str(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str:
        raise Refused("wrong-type", f"{field} is a string")
    if not pattern.fullmatch(value):
        raise Refused("bad-value", field)
    return value


def _list(value: object, field: str) -> list:
    if type(value) is not list:
        raise Refused("wrong-type", f"{field} is an array")
    return value


def _unique_strs(value: object, field: str, pattern: re.Pattern[str], cap: int) -> tuple[str, ...]:
    items = _list(value, field)
    if len(items) > cap:
        raise Refused("too-many", f"{field}: {len(items)} > {cap}")
    out = tuple(_str(x, field, pattern) for x in items)
    if len(set(out)) != len(out):
        raise Refused("duplicate-value", field)
    return out


def _display(value: object, field: str) -> str:
    """§10.3's render alphabet (L, M, N, P, S, Zs minus bidi overrides/isolates), as an input rule, and no character
    :mod:`levain.unshowable` calls unshowable (so no space but U+0020, no variation selector run, no filler)."""
    if type(value) is not str:
        raise Refused("wrong-type", f"{field} is a string")
    if not 1 <= len(value) <= MAX_REPO_LABEL:
        raise Refused("bad-value", f"{field} length")
    for c in value:
        if c in _BIDI_CONTROLS or unicodedata.category(c) not in _DISPLAY_CATEGORIES:
            raise Refused("bad-value", f"{field}: U+{ord(c):04X}")
    found = unshowable.first_hidden(value, allow="", selectors=False)
    if found is not None:
        raise Refused("bad-value", f"{field}: U+{ord(found[1]):04X}")
    return value


def _git_identity(value: object) -> GitIdentity:
    if type(value) is not dict:
        raise Refused("wrong-type", "git_identity is an object")
    _keys(value, frozenset({"name", "email"}), frozenset({"name", "email"}), "git_identity")
    return GitIdentity(_str(value["name"], "git_identity.name", _GIT_NAME_RE),
                       _str(value["email"], "git_identity.email", _GIT_EMAIL_RE))


def _verifier_steps(value: object) -> tuple[VerifierStep, ...]:
    """§5.6 shape: ``[{argv:[str…], wall_s:int}]`` under the ``MAX_STEP*`` caps, no NUL,
    ``wall_s`` ≤ V20. DECISION: at least one step, a non-empty argv of non-empty strings, ``wall_s`` ≥ 1."""
    items = _list(value, "verifier_steps")
    if not 1 <= len(items) <= MAX_STEPS:
        raise Refused("too-many" if items else "bad-value", f"verifier_steps: {len(items)} steps")
    steps = []
    for step in items:
        if type(step) is not dict:
            raise Refused("wrong-type", "verifier_steps[] is an object")
        _keys(step, frozenset({"argv", "wall_s"}), frozenset({"argv", "wall_s"}), "verifier_steps[]")
        argv = _list(step["argv"], "verifier_steps[].argv")
        if not 1 <= len(argv) <= MAX_STEP_ARGV:
            raise Refused("too-many" if argv else "bad-value", f"verifier_steps[].argv: {len(argv)}")
        for a in argv:
            if type(a) is not str:
                raise Refused("wrong-type", "verifier_steps[].argv[] is a string")
            if not a or "\x00" in a or len(a.encode("utf-8")) > MAX_STEP_ARG_BYTES:
                raise Refused("bad-value", "verifier_steps[].argv[]")
        wall = step["wall_s"]
        if type(wall) is not int:
            raise Refused("wrong-type", "verifier_steps[].wall_s is an integer")
        if not 1 <= wall <= MAX_STEP_WALL_S:
            # the bound, never the raw int: str() of a >4300-digit int raises ValueError
            raise Refused("out-of-range", f"verifier_steps[].wall_s is not in 1..{MAX_STEP_WALL_S}")
        steps.append(VerifierStep(tuple(argv), wall))
    return tuple(steps)


def _prepare_input(obj: dict) -> PrepareInput:
    # §5.1 prepare stdin ``{lease_nonce, ticket?}``.
    _keys(obj, frozenset({"lease_nonce", "ticket"}), frozenset({"lease_nonce"}), "prepare")
    ticket = _handle(obj["ticket"], "ticket") if "ticket" in obj else None
    return PrepareInput(_handle(obj["lease_nonce"], "lease_nonce"), ticket)


def _render_blob(obj: dict) -> RenderBlob:
    # §5.3: unknown key = refusal by name (``driver_input`` was REMOVED in c5, so it is unknown).
    _keys(obj, RENDER_KEYS, RENDER_REQUIRED, "render")
    if obj["format"] != RENDER_FORMAT:
        raise Refused("bad-format", "render")
    vendor = obj["vendor"]
    if type(vendor) is not str:
        raise Refused("wrong-type", "vendor is a string")
    if vendor not in VENDORS:
        raise Refused("bad-value", f"vendor: {vendor[:64]!r}")
    routes = _unique_strs(obj["routes"], "routes", _ROUTE_ID_RE, MAX_ROUTES)
    model = _str(obj["model"], "model", _MODEL_RE) if "model" in obj else None
    git = _git_identity(obj["git_identity"]) if "git_identity" in obj else None
    hooks: frozenset[str] = frozenset()
    if "hooks" in obj:
        hook_list = _list(obj["hooks"], "hooks")
        for h in hook_list:
            if type(h) is not str:
                raise Refused("wrong-type", "hooks[] is a string")
            if h not in HOOKS:
                raise Refused("bad-value", f"hooks: {h[:64]!r}")
        if len(set(hook_list)) != len(hook_list):
            raise Refused("duplicate-value", "hooks")
        hooks = frozenset(hook_list)
    mcp: tuple[str, ...] = ()
    if "mcp" in obj:
        mcp = _unique_strs(obj["mcp"], "mcp", _TEMPLATE_RE, MAX_ROUTES)
        if mcp:                                    # §5.3: template names, empty in 1.0
            raise Refused("bad-value", "mcp is empty in 1.0")
    loopback = obj.get("allow_own_loopback")
    if "allow_own_loopback" in obj and type(loopback) is not bool:
        raise Refused("wrong-type", "allow_own_loopback is a bool")
    label = _display(obj["repo_label"], "repo_label") if "repo_label" in obj else None
    steps = _verifier_steps(obj["verifier_steps"]) if "verifier_steps" in obj else None
    driver = _str(obj["driver"], "driver", _TEMPLATE_RE) if "driver" in obj else None
    # §5.3 + §5.6: both are verifier-only. DECISION: they travel together (a driver with no steps, or
    # steps with no driver, describes no verifier session).
    if (steps is None) != (driver is None):
        raise Refused("verifier-keys-unpaired", "verifier_steps and driver travel together")
    return RenderBlob(vendor=vendor, routes=routes, model=model, git_identity=git, hooks=hooks, mcp=mcp,
                      allow_own_loopback=loopback, repo_label=label, verifier_steps=steps, driver=driver)


def parse_stdin(verb: str, raw_bytes: bytes) -> PrepareInput | RenderBlob | None:
    """The verb's stdin blob, or :class:`Refused` by name. A verb with no stdin (§5.1 "—") returns
    ``None`` for empty input and refuses anything else (the verb layer never reads stdin for it)."""
    if verb not in VERBS:
        raise Refused("unknown-verb", repr(verb)[:64])
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise Refused("wrong-type", "stdin is bytes")
    # The cap counts BYTES, so it is taken after the conversion: a memoryview's len() counts its items,
    # which for a wider format is fewer than its bytes. §5 preamble: still before any decode or parse.
    raw = bytes(raw_bytes)
    if len(raw) > STDIN_MAX:
        raise Refused("stdin-too-large", f"{len(raw)} > {STDIN_MAX}")
    if verb not in STDIN_VERBS:
        if raw:
            raise Refused("unexpected-stdin", verb)
        return None
    if not raw:
        raise Refused("missing-stdin", verb)
    # Non-UTF-8 gets its own name before canon.parse (which would refuse it under a generic one).
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("stdin-not-utf8", verb) from None
    try:
        obj = canon.parse(raw)
    except ContractError as e:
        raise _parse_refusal(e) from None
    if type(obj) is not dict:
        raise Refused("stdin-not-object", verb)
    # Header shared encodings: canonical JSON, one byte form per object (sorted keys, no whitespace,
    # no trailing newline, the canon escapes).
    if canon.canonical_bytes(obj) != raw:
        raise Refused("stdin-not-canonical", verb)
    return _prepare_input(obj) if verb == "prepare" else _render_blob(obj)


def bind_stdin(request: RuntimeRequest, blob: PrepareInput | RenderBlob | None) -> None:
    """Cross-checks argv against stdin. §5.1: ``_levain_dispatch``'s rule is ticket-bound, so its
    ``prepare`` carries a ticket; the operator's lease carries none (§3.1), so its ``prepare`` refuses one.
    That the ticket is live and minted for this caller is CONTROL's (§3.1 step 2)."""
    if request.verb == "prepare":
        if not isinstance(blob, PrepareInput):
            raise Refused("missing-stdin", "prepare")
        if request.caller == DISPATCH and blob.ticket is None:
            raise Refused("ticket-required", "dispatch prepare")
        if request.caller == OPERATOR and blob.ticket is not None:
            raise Refused("ticket-not-allowed", "operator prepare")
    elif request.verb == "render":
        if not isinstance(blob, RenderBlob):
            raise Refused("missing-stdin", "render")
    elif blob is not None:
        raise Refused("unexpected-stdin", request.verb)


# --- output (§5 preamble: one canonical JSON line) -------------------------------------------------

def _line(obj: dict) -> str:
    text = canon.canonical(obj)
    canon.parse(text.encode("utf-8"))   # refuses a float, NaN, a lone surrogate or an out-of-range int
    return text + "\n"


def ok_line(fields: Mapping[str, object]) -> str:
    """``{"ok":true,…}`` as one canonical JSON line (newline-terminated). ``fields`` must not carry
    ``ok``; a value canonical JSON refuses (a float, NaN) raises ``ContractError``."""
    if not isinstance(fields, Mapping):
        raise TypeError("ok_line takes a mapping")
    if "ok" in fields:
        raise ValueError("ok_line sets ok itself")
    if any(type(k) is not str for k in fields):
        raise TypeError("ok_line keys are strings")
    return _line({"ok": True, **fields})


def refusal_line(refused: Refused) -> str:
    """``{"ok":false,"refused":"<name>"}`` as one canonical JSON line. Only the NAME leaves (the detail
    can carry input bytes; it is for the human's stderr). A name that is not a plain lowercase
    identifier (an optional ``:qualifier``, as ``no-baseline:<slot>``, ``fsck:hasDotgit``) becomes ``internal``, so no input byte ever reaches stdout."""
    name = refused.name if isinstance(refused, Refused) and isinstance(refused.name, str) else ""
    if not _NAME_RE.fullmatch(name) or len(name) > 64:
        name = "internal"
    return _line({"ok": False, "refused": name})


__all__ = [
    "VERBS", "KINDS", "VENDORS", "AUTHS", "CALLERS", "OPERATOR", "DISPATCH", "LEASE_VERBS", "STDIN_VERBS",
    "DISPATCH_FORBIDDEN", "KINDS_FOR_CALLER", "STDIN_MAX", "RENDER_FORMAT", "RENDER_KEYS",
    "RuntimeRequest", "PrepareInput", "RenderBlob", "GitIdentity", "VerifierStep",
    "parse_runtime_argv", "parse_stdin", "bind_stdin", "ok_line", "refusal_line",
]
