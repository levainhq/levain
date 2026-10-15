"""levain.authority: the one signed authority log (trust-tiering DESIGN §3, §4).

Who may sign what, and the standing policy, are not stored anywhere: they are what a VERIFIED REPLAY of
``authority_log`` says. Each link is canonical JSON, hash-chained to its predecessor and signed (SSHSIG,
namespace :data:`NAMESPACE`) by a key the replay has enrolled. Every link is judged against the state at
its predecessor: its chain position, its signature, its signer's class and every op rule. A key revoked at
seq N signs nothing after N, and the links it signed before N stay valid (no retroactive voiding).

A policy link is classified by RECOMPUTE: the declared op is applied to the replayed policy and the
difference is classified on its own (:func:`classify`). A declared op that disagrees with the computed
delta is refused, and so is a link that changes nothing.

The anchor (``anchor/authority.anchor`` in the store's data root, its directory excluded from Time Machine
so a restore of the data root cannot roll it back with the log) is written only after a commit, so it lags
and never leads, and only ever moves up. :func:`append` refuses a log behind it, and an empty log beside one; this
module reads the log without consulting it (:func:`snapshot`), so a caller that opens a store checks it
with :func:`check_anchor`.
"""
from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from levain.autonomic.confirm import _VERIFY_ENV, DEFAULT_SSH_KEYGEN, trust_anchor_problem

__all__ = [
    "ANCHOR_DIR", "ANCHOR_NAME", "CONSENT_AGENTS", "CONSENT_GRANT_KIND", "CONSENT_POSTURES", "CONSENT_SHELL_TOOLS",
    "CUSTODIES", "FIDO_ATTESTED", "FORMAT", "NAMESPACE", "RESET_FORMAT", "RESET_NAMESPACE", "SCHEMA",
    "Anchor", "AnchorError", "AuthorityError", "AuthorityKey", "AuthorityPolicy", "AuthoritySnapshot",
    "HeadBelowCache", "NotInitialised", "PROCESS_CACHE", "ReplayCache", "ReplayError", "StaleHead", "StandingPolicy",
    "UnsignedLink", "VerifierUnavailable", "append", "advance_anchor", "anchor_path", "check_anchor", "classify",
    "consent_workspace_problem", "create_schema", "custody_problem",
    "genesis_policy", "prepare", "read_anchor", "reset_anchor", "reset_message", "sign_and_append",
    "presence_missing", "snapshot", "UNSTATED", "verify_anchor_reset", "verify_link_sig",
]

FORMAT = "levain-authority/1"
# never the cockpit's or the confirm gate's namespace: a link signature verifies as nothing else
NAMESPACE = "levain-authority"
RESET_FORMAT = "levain-authority-reset/1"
RESET_NAMESPACE = "levain-authority-reset"
ANCHOR_NAME = "authority.anchor"
ANCHOR_DIR = "anchor"
# The sticky Time Machine exclusion `tmutil addexclusion` writes: a binary plist of the string
# "com.apple.backupd", as `xattr -px` reads it back.
_TM_EXCLUDE_ATTR = b"com.apple.metadata:com_apple_backup_excludeItem"
_TM_EXCLUDE_VALUE = bytes.fromhex(
    "62706c69737430305f1011636f6d2e6170706c652e6261636b7570640800000000000001010000000000000001000000"
    "0000000000000000000000001c")
ANCHOR_FORMAT = "levain-authority-anchor/1"

SCHEMA = ("CREATE TABLE IF NOT EXISTS authority_log (seq INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, "
          "record TEXT NOT NULL, signer_fp TEXT NOT NULL, signature TEXT NOT NULL, hash TEXT NOT NULL UNIQUE)")

LAPTOP = "laptop-presence"
# Custody (rev 6 presence floor, DESIGN §3.2 items 1-3): every enrol record states how its key is held. It
# is a DECLARATION, signed by the enrolling key, never a proof: the verifier cannot tell a touch key from a
# no-touch one (a Secure Enclave key echoes the presence flag it is handed), so the touch rests wholly on
# how the key was made. The replay holds the declaration to the key types it can be true of.
UNSTATED = "unstated"
FIDO_ATTESTED = "fido-attested"
CUSTODIES = ("touch-id", "passphrase", "tpm", "fido", "windows-hello", FIDO_ATTESTED, UNSTATED)
_SK_KEY_TYPES = frozenset({"sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"})
# the custody table: the key types each stated custody admits (``unstated`` is not in it)
_CUSTODY_KEY_TYPES: Mapping[str, frozenset[str]] = MappingProxyType({
    "touch-id": _SK_KEY_TYPES, "fido": _SK_KEY_TYPES, "windows-hello": _SK_KEY_TYPES, FIDO_ATTESTED: _SK_KEY_TYPES,
    "tpm": frozenset({"ecdsa-sha2-nistp256"}),
    "passphrase": frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256"}),
})
# The laptop-presence floor IS the custody table: every stated custody is accepted for the class, so the
# types it admits are the table's union (a carried legacy key with no stated custody stays inside it). The
# signers line never carries ``no-touch-required``, so an sk key's presence flag is still required.
_LAPTOP_KEY_TYPES = frozenset().union(*_CUSTODY_KEY_TYPES.values())


def custody_problem(key_class: str, custody: Any, key_type: str, *, legacy: bool = False) -> str | None:
    """Why ``custody`` cannot be declared for a ``key_class`` key of ``key_type`` (None when it can): the
    one rule the replay enforces and the CLI checks before anything is signed. ``legacy``: the key is
    carried from a levain-cockpit/2 store (its record has ``legacy_enrolled_at``), the one laptop-presence
    key that may leave its custody ``unstated``."""
    if not isinstance(custody, str) or custody not in CUSTODIES:
        return f"unknown custody {str(custody)[:40]!r}; one of {', '.join(CUSTODIES)}"
    if custody == FIDO_ATTESTED:
        return (f"custody {FIDO_ATTESTED} is refused until attestation at enrol is built (it comes with the "
                "governed install); an unattested FIDO key is custody fido")
    if custody == UNSTATED:
        if key_class != LAPTOP:
            return None
        if not legacy:
            return (f"a {LAPTOP} key must state its custody "
                    f"({', '.join(c for c in CUSTODIES if c not in (UNSTATED, FIDO_ATTESTED))}); "
                    f"{UNSTATED} is for display-phone and presence keys and a carried legacy key")
        if key_type not in _LAPTOP_KEY_TYPES:
            return f"key type {key_type} is not eligible for {LAPTOP} (one of {', '.join(sorted(_LAPTOP_KEY_TYPES))})"
        return None
    admits = _CUSTODY_KEY_TYPES[custody]
    if key_type not in admits:
        return f"custody {custody} needs a key of type {', '.join(sorted(admits))}, not {key_type}"
    return None

# Delta kinds the recompute classifier returns (§4.3).
NOOP, TIGHTEN, LOOSEN, RENEW, GRANT_REVOKE = "noop", "tighten", "loosen", "renew", "grant_revoke"
# the signer rank each delta needs: T2 = display-phone, T3 = laptop-presence, a revoke any enrolled key
_DELTA_RANK = {TIGHTEN: 2, RENEW: 2, LOOSEN: 3, GRANT_REVOKE: 1}
# what each declared policy op must compute to
_OP_DELTAS = {"raise": {TIGHTEN}, "revoke_grant": {GRANT_REVOKE}, "renew": {RENEW}, "loosen": {LOOSEN},
              "create_grant": {LOOSEN}, "timeouts": {TIGHTEN, LOOSEN}}

_BASE_FIELDS = frozenset({"format", "store_id", "seq", "at", "op"})
# per op: (required fields, optional fields), beyond the base
_OP_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "enrol": (frozenset({"fingerprint", "public_key", "class", "custody"}), frozenset({"legacy_enrolled_at"})),
    "revoke_key": (frozenset({"fingerprint"}), frozenset({"compromised_since"})),
    "raise": (frozenset({"verb", "tier"}), frozenset()),
    "loosen": (frozenset({"policy"}), frozenset()),
    "revoke_grant": (frozenset({"grant_id"}), frozenset()),
    "renew": (frozenset({"grant_id", "expires_at", "signed_at"}), frozenset()),
    "create_grant": (frozenset({"grant"}), frozenset()),
    "timeouts": (frozenset({"timeouts"}), frozenset()),
}
_GRANT_FIELDS = frozenset({"id", "entity", "remote_url", "deploy_key_sha256", "refspecs", "verbs", "expires_at"})
_GRANT_VERBS = frozenset({"push", "fetch"})
# A consent grant (Q4) answers an external agent's permission gate on the consent queue. A grant with no
# ``kind`` is an egress grant (the fields above, unchanged); a consent grant carries exactly these.
CONSENT_GRANT_KIND = "consent"
_CONSENT_GRANT_FIELDS = frozenset({"id", "kind", "agent", "tools", "workspace", "postures", "expires_at"})
CONSENT_AGENTS = ("claude_code", "codex")
# Ruling 1: a shell command is never parsed, so a grant naming a shell tool would allow every command. Claude
# Code's shell tool is Bash, and Codex's PermissionRequest names its shell escalation Bash too (Q0 item 6, the one
# name measured); the rest are unmeasured shell-tool names refused defensively until Q3 measures Codex's. Compared
# case-insensitively.
CONSENT_SHELL_TOOLS = frozenset({"bash", "shell", "local_shell", "exec_command", "unified_exec", "container.exec"})
# The posture names each agent's PermissionRequest event carries (``permission_mode``). Claude Code: its documented
# permission modes (Q0 measured ``default``); Codex: the one value measured (Q0 item 6); Q3 extends it.
CONSENT_POSTURES: Mapping[str, frozenset[str]] = MappingProxyType({
    "claude_code": frozenset({"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}),
    "codex": frozenset({"default"}),
})
# an exact tool name: no glob, no prefix; an MCP tool is mcp__<server>__<tool>
_CONSENT_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_CONSENT_MCP_TOOL = re.compile(r"mcp__[^_].*?__\S+")
_CONSENT_GRANT_MAX = timedelta(days=30)
# a branch glob only: no force (+), no src:dst, no deletion (empty source)
_REFSPEC = re.compile(r"refs/heads/[A-Za-z0-9._/*-]+")
_STORE_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_FP = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_RENEW_MAX = timedelta(days=30)
_RENEW_SKEW = timedelta(minutes=10)
_MAX_SIGNATURE = 16384


class AuthorityError(Exception):
    """A refused authority act or an unusable log; the message is operator-facing."""


class ReplayError(AuthorityError):
    """A link the replay refuses. ``seq`` is the link's position."""

    def __init__(self, seq: int, why: str) -> None:
        super().__init__(f"authority log link {seq}: {why}")
        self.seq = seq


class VerifierUnavailable(AuthorityError):
    """The signature verifier (ssh-keygen) could not run. Never a verdict on the log: a replay raises
    this unconverted, so a caller can tell "retry" from "the log is refused"."""


class NotInitialised(AuthorityError):
    """The log has no links: nothing may be signed until ``levain cockpit ledger init``."""


class StaleHead(AuthorityError):
    """The head moved between preparing a link and appending it: re-prepare and re-sign."""


class HeadBelowCache(AuthorityError):
    """The log differs, at or below a seq both hold, from the chain this process already verified, and
    does not verify in full (a rewrite). One that verifies replaces the cached chain, and a log that is
    merely shorter is a prefix and is served (see :class:`ReplayCache`)."""


class AnchorError(AuthorityError):
    """The anchor refuses this log."""


def _ranks() -> Mapping[str, int]:
    # imported at use: the cockpit modules will import this one
    from levain.cockpit.firejournal import _CLASS_RANK
    return _CLASS_RANK


def _tiers() -> tuple[str, ...]:
    from levain.cockpit.verbs import TIERS
    return TIERS


def _stamp(when: datetime) -> str:
    from levain.cockpit.consent import utc_stamp
    return utc_stamp(when)


def _parse_stamp(text: Any, what: str) -> datetime:
    from levain.cockpit.consent import parse_stamp
    if not isinstance(text, str):
        raise AuthorityError(f"{what} is not a timestamp")
    try:
        when = parse_stamp(text)
    except Exception:  # noqa: BLE001 - any parse failure is the same refusal
        raise AuthorityError(f"{what} {text[:40]!r} is not a timestamp") from None
    if _stamp(when) != text:
        raise AuthorityError(f"{what} {text[:40]!r} is not in the one timestamp form")
    return when


def _canonical(obj: Any) -> str:
    # sort_keys + compact separators + ensure_ascii=False: one byte form per record. Byte parity with
    # team/entry.py's canonical, NaN/Infinity included; replay refuses non-finite numbers where they could
    # appear (only `timeouts` carries free-form values), never by changing this form.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _link_hash(prev_hash: str, record: str) -> str:
    return hashlib.sha256((prev_hash + record).encode("utf-8")).hexdigest()


def _message(prev_hash: str, record: str) -> bytes:
    return (prev_hash + "\n" + record).encode("utf-8")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


# ---------------------------------------------------------------------------------------------------
# state


@dataclass(frozen=True)
class AuthorityKey:
    fingerprint: str
    public_key: str
    key_class: str
    enrolled_seq: int
    revoked_seq: int | None = None
    compromised_since: int | None = None
    legacy_enrolled_at: str | None = None
    # the enrol link's ``at``: provenance only, never authority
    enrolled_at: str | None = None
    # the custody its enrol record declares (CUSTODIES): a declaration, never a proof
    custody: str = UNSTATED

    @property
    def principal(self) -> str:
        from levain.cockpit.keys import principal_of
        return principal_of(self.fingerprint)

    @property
    def active(self) -> bool:
        return self.revoked_seq is None


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class StandingPolicy:
    """The standing policy a replay derives: verb tier raises, per-kind timeout defaults (carried, their
    semantics are S4b's) and egress grants (carried, consumed by S5)."""
    raise_to: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    timeouts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    grants: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: MappingProxyType({}))

    def apply(self, verb: str, tier: str) -> str:
        """``tier`` raised to this policy's floor for ``verb`` (never lowered)."""
        floor = self.raise_to.get(verb)
        if floor is None:
            return tier
        tiers = _tiers()
        return max(tier, floor, key=tiers.index)

    def as_dict(self) -> dict[str, Any]:
        return {"raise_to": _thaw(self.raise_to), "timeouts": _thaw(self.timeouts), "grants": _thaw(self.grants)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StandingPolicy:
        return cls(_freeze(dict(data["raise_to"])), _freeze(dict(data["timeouts"])),
                   _freeze(dict(data["grants"])))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StandingPolicy) and _canonical(self.as_dict()) == _canonical(other.as_dict())

    def __hash__(self) -> int:
        return hash(_canonical(self.as_dict()))


# the S3 name, kept so code written against it still imports; one type, two names
AuthorityPolicy = StandingPolicy


# The policy before any policy link, pinned for this FORMAT: replay is a pure function of the log, so
# it never reads live code (a log replays the same whatever the code ships). These are the values the
# retired ``verbs.Policy()`` shipped when the format was born: no raises. Every cockpit tier reads
# this through ``RequestAuthority.policy`` and nowhere else (S4a).
_GENESIS_RAISE_TO: Mapping[str, str] = MappingProxyType({})


def genesis_policy() -> StandingPolicy:
    """The policy before any policy link: the constant pinned for :data:`FORMAT`."""
    return StandingPolicy(raise_to=_freeze(dict(_GENESIS_RAISE_TO)))


@dataclass(frozen=True)
class AuthoritySnapshot:
    """The replayed state at ``seq``/``head_hash``. ``keys`` holds every key ever enrolled, revoked ones
    included (``revoked_seq`` set), because a revoked key may never be enrolled again."""
    store_id: str
    seq: int
    head_hash: str
    keys: Mapping[str, AuthorityKey]
    policy: StandingPolicy
    _hashes: tuple[str, ...] = field(repr=False, default=())
    # the head link's ``at``: the next link's ``at`` may not be earlier (an ordering rule, never a grant)
    head_at: datetime | None = None
    # set only on what append returns: the link committed but the anchor could not be advanced
    anchor_warning: str | None = field(default=None, compare=False)
    # per standing grant, the seq of the link that last created or changed it (a create, a renew, a loosen)
    grant_seqs: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}), compare=False, repr=False)

    def key(self, fingerprint: str) -> AuthorityKey | None:
        return self.keys.get(fingerprint)

    def grant_link(self, grant_id: str) -> tuple[int, str] | None:
        """``(seq, hash)`` of the link that created or last changed the standing grant ``grant_id``; None when
        the policy holds no such grant."""
        seq = self.grant_seqs.get(grant_id)
        link = None if seq is None else self.hash_at(seq)
        return None if seq is None or link is None or grant_id not in self.policy.grants else (seq, link)

    def active_keys(self) -> list[AuthorityKey]:
        return [k for k in self.keys.values() if k.active]

    def hash_at(self, seq: int) -> str | None:
        return self._hashes[seq - 1] if 1 <= seq <= len(self._hashes) else None


_TEXT_COLUMNS = ("prev_hash", "record", "signer_fp", "signature", "hash")


@dataclass(frozen=True)
class _Cell:
    """A stored value that is not UTF-8 text: its SQLite type and raw bytes, kept so the row still has
    a digest; the replay refuses it."""
    kind: str
    raw: bytes | None


@dataclass(frozen=True)
class _Row:
    seq: Any
    prev_hash: Any
    record: Any
    signer_fp: Any
    signature: Any
    hash: Any

    def digest(self) -> str:
        cells = [self.seq, self.prev_hash, self.record, self.signer_fp, self.signature, self.hash]
        return hashlib.sha256(json.dumps(cells, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                                         default=repr).encode("ascii")).hexdigest()


def _text_cell(kind: str, raw: bytes | None) -> str | _Cell:
    if kind == "text" and raw is not None:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return _Cell(kind, raw)


def _rows(conn: sqlite3.Connection) -> list[_Row]:
    # each text column as (typeof, bytes), decoded here: a corrupt cell becomes a _Cell the replay refuses
    # at its seq, never an exception out of the driver's own decode
    cols = ", ".join(f"typeof({c}), CAST({c} AS BLOB)" for c in _TEXT_COLUMNS)
    cur = conn.execute(f"SELECT seq, {cols} FROM authority_log ORDER BY seq")
    return [_Row(r[0], *(_text_cell(k, raw) for k, raw in zip(r[1::2], r[2::2]))) for r in cur.fetchall()]


# ---------------------------------------------------------------------------------------------------
# signatures


def _signers_line(principal: str, namespace: str, public_key: str) -> str:
    # no options: in particular never ``no-touch-required``, so an sk key's user-presence flag is required
    return f'{principal} namespaces="{namespace}" {public_key}\n'


_SSHSIG_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_SSHSIG_END = "-----END SSH SIGNATURE-----"
_SK_USER_PRESENT = 0x01


class _Wire:
    """A cursor over SSH wire-format bytes (RFC 4251 §5); any overrun or trailing byte is ValueError."""

    def __init__(self, data: bytes) -> None:
        self._data, self._pos = data, 0

    def take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ValueError("truncated")
        out = self._data[self._pos:self._pos + n]
        self._pos += n
        return out

    def u32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def string(self) -> bytes:
        return self.take(self.u32())

    def end(self) -> None:
        if self._pos != len(self._data):
            raise ValueError("trailing bytes")


def _sshsig_type_and_flags(armored: str) -> tuple[str, int | None]:
    """The signature algorithm inside an armored SSHSIG, and its FIDO flags byte (None for a non-sk
    algorithm). Raises ValueError on anything malformed.

    The SSHSIG blob (OpenSSH PROTOCOL.sshsig §"Blob format"): byte[6] "SSHSIG", uint32 version (1),
    string publickey, string namespace, string reserved, string hash_algorithm, string signature.
    For an sk-* algorithm the ``signature`` string is (OpenSSH PROTOCOL.u2f §"SSH U2F signatures"):
    string sig-type, string inner signature, byte flags, uint32 counter; flags bit 0x01 is user
    presence. Every other algorithm is string sig-type, string signature."""
    text = armored.strip()
    if not text.startswith(_SSHSIG_BEGIN) or not text.endswith(_SSHSIG_END):
        raise ValueError("not an armored SSH signature")
    blob = base64.b64decode("".join(text[len(_SSHSIG_BEGIN):-len(_SSHSIG_END)].split()), validate=True)
    outer = _Wire(blob)
    if outer.take(6) != b"SSHSIG" or outer.u32() != 1:
        raise ValueError("not an SSHSIG v1 blob")
    for _ in range(4):              # publickey, namespace, reserved, hash_algorithm: ssh-keygen judges them
        outer.string()
    inner = _Wire(outer.string())
    outer.end()
    sig_type = inner.string().decode("ascii")
    inner.string()
    if not sig_type.startswith("sk-"):
        inner.end()
        return sig_type, None
    flags = inner.take(1)[0]
    inner.u32()
    inner.end()
    return sig_type, flags


def presence_missing(signature: str, public_key: str) -> bool:
    """True if a FIDO (sk-*) key or signature lacks the user-presence flag, or ``signature`` cannot be
    read for one: the one presence rule for every levain signature check (authority links here, cockpit
    acts in the broker), so a key made ``no-touch-required`` never signs without a touch."""
    try:
        sig_type, flags = _sshsig_type_and_flags(signature)
    except (ValueError, UnicodeError):
        return True
    key_type = public_key.split()[0] if isinstance(public_key, str) and public_key.split() else ""
    return (key_type.startswith("sk-") or sig_type.startswith("sk-")) \
        and (flags is None or not flags & _SK_USER_PRESENT)


def _pipe_reading(data: bytes) -> int:
    """The read end of a pipe that yields ``data`` and then EOF. A thread writes it, so no size can
    block the caller; it ends with a broken pipe once the caller closes the read end."""
    r, w = os.pipe()

    def feed() -> None:
        try:
            with open(w, "wb") as fh:
                fh.write(data)
        except OSError:
            pass

    try:
        threading.Thread(target=feed, name="levain-authority-pipe", daemon=True).start()
    except BaseException:
        os.close(r)
        os.close(w)
        raise
    return r


def verify_link_sig(message: bytes, signature: Any, *, public_key: str, fingerprint: str,
                    namespace: str = NAMESPACE, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                    timeout: float = 10.0) -> bool:
    """True only if ``signature`` is an SSHSIG over ``message`` in ``namespace`` by ``public_key``, the
    verifier reports exactly ``fingerprint`` for the key it verified with, and, for a FIDO (sk-*) key or
    signature, the signature carries the user-presence flag. The signers line and the signature reach
    ssh-keygen through pipes (``/dev/fd/N``), never a file, so no same-uid process can swap either one
    (say, for a signers line with ``no-touch-required``) between write and use; the presence flag is
    checked here as well, so a no-touch signature is refused whatever the verifier would say.
    Raises :class:`VerifierUnavailable` when the verifier cannot run (that is never a verdict)."""
    if not isinstance(signature, str) or len(signature) > _MAX_SIGNATURE:
        return False
    try:
        sig_bytes = (signature.strip() + "\n").encode("ascii")
    except UnicodeError:
        return False
    if presence_missing(signature, public_key):
        return False
    problem, keygen = trust_anchor_problem(ssh_keygen, what="ssh-keygen")
    if problem is not None:
        raise VerifierUnavailable(f"cannot verify an authority signature: {problem}")
    principal = "authority-signer"
    try:
        signers = _signers_line(principal, namespace, public_key).encode("ascii")
    except UnicodeError:
        return False
    fds: list[int] = []
    try:
        fds.append(_pipe_reading(signers))
        fds.append(_pipe_reading(sig_bytes))
        cp = subprocess.run([str(keygen), "-Y", "verify", "-f", f"/dev/fd/{fds[0]}", "-I", principal,
                             "-n", namespace, "-s", f"/dev/fd/{fds[1]}"], input=message, capture_output=True,
                            timeout=timeout, env=dict(_VERIFY_ENV), pass_fds=tuple(fds))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # RuntimeError: a pipe's writer thread could not start (_pipe_reading has closed that pipe)
        raise VerifierUnavailable(f"ssh-keygen could not verify an authority signature ({type(exc).__name__})") \
            from None
    finally:
        for fd in fds:
            os.close(fd)
    if cp.returncode != 0:
        return False
    good = re.compile(r'Good "' + re.escape(namespace) + r'" signature for ' + re.escape(principal)
                      + r' with \S+ key (SHA256:[A-Za-z0-9+/]{43})')
    out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
    found = [m.group(1) for line in out.splitlines() if (m := good.match(line.strip()))]
    return found == [fingerprint]


# ---------------------------------------------------------------------------------------------------
# policy ops and the recompute classifier


def consent_workspace_problem(workspace: Any) -> str | None:
    """Why ``workspace`` cannot be a consent grant's workspace (None when it can): an absolute path in its
    normal form (no ``..``, no ``.``, no doubled or trailing slash), never ``/``. Pure, so the replay holds it;
    the home directory (and its ancestors) is refused at append (:func:`_append_only_checks`), which knows it."""
    if not isinstance(workspace, str) or not workspace or "\x00" in workspace:
        return "the workspace must be a non-empty path"
    if not workspace.startswith("/") or workspace.startswith("//"):
        return f"the workspace {workspace[:80]!r} must be an absolute path"
    if os.path.normpath(workspace) != workspace:
        return f"the workspace {workspace[:80]!r} is not in its normal form (no .., no trailing slash)"
    if workspace == "/":
        return "the workspace may not be / (that grants the whole disk)"
    return None


def _validate_consent_grant(grant: Mapping[str, Any], what: str, at: datetime | None) -> dict[str, Any]:
    """A consent grant's shape (Q4). ``at``: the link's own time, when the grant arrives in a link; its life
    ends at most 30 days after it (the bound a renew has), so no link can carry a longer one."""
    if set(grant) != _CONSENT_GRANT_FIELDS:
        raise AuthorityError(f"{what} (a consent grant) must carry exactly {', '.join(sorted(_CONSENT_GRANT_FIELDS))}")
    if grant["kind"] != CONSENT_GRANT_KIND:
        raise AuthorityError(f"{what}.kind must be {CONSENT_GRANT_KIND!r}")
    if not isinstance(grant["id"], str) or not grant["id"]:
        raise AuthorityError(f"{what}.id must be a non-empty string")
    agent = grant["agent"]
    if not isinstance(agent, str) or agent not in CONSENT_AGENTS:
        raise AuthorityError(f"{what}.agent must be one of {', '.join(CONSENT_AGENTS)}")
    for name, ok in (("tools", lambda t: bool(_CONSENT_TOOL.fullmatch(t)) and (
                        not t.startswith("mcp__") or bool(_CONSENT_MCP_TOOL.fullmatch(t)))),
                     ("postures", CONSENT_POSTURES[agent].__contains__)):
        items = grant[name]
        if not isinstance(items, (list, tuple)) or not items or not all(isinstance(i, str) and ok(i) for i in items):
            raise AuthorityError(f"{what}.{name} is not a non-empty list of allowed values" + (
                " (exact tool names, an MCP tool as mcp__<server>__<tool>)" if name == "tools" else
                f" (for {agent}: {', '.join(sorted(CONSENT_POSTURES[agent]))})"))
        if list(items) != sorted(set(items)):
            raise AuthorityError(f"{what}.{name} must be sorted and unique")
    shell = [t for t in grant["tools"] if t.lower() in CONSENT_SHELL_TOOLS]
    if shell:
        raise AuthorityError(f"{what} names the shell tool {shell[0]!r}: a shell command is never parsed, so a "
                             "grant for it would allow every command; shell requests go to you")
    problem = consent_workspace_problem(grant["workspace"])
    if problem is not None:
        raise AuthorityError(f"{what}: {problem}")
    expires = _parse_stamp(grant["expires_at"], f"{what}.expires_at")
    # a difference, never `at + 30 days`, which overflows near year 9999
    if at is not None and expires - at > _CONSENT_GRANT_MAX:
        raise AuthorityError(f"{what} may last at most 30 days past its link's time")
    return _thaw(grant)


def _validate_grant(grant: Any, what: str, *, at: datetime | None = None) -> dict[str, Any]:
    if isinstance(grant, Mapping) and "kind" in grant:
        return _validate_consent_grant(grant, what, at)
    if not isinstance(grant, Mapping) or set(grant) != _GRANT_FIELDS:
        raise AuthorityError(f"{what} must carry exactly {', '.join(sorted(_GRANT_FIELDS))}")
    for name in ("id", "entity", "remote_url", "deploy_key_sha256"):
        if not isinstance(grant[name], str) or not grant[name]:
            raise AuthorityError(f"{what}.{name} must be a non-empty string")
    for name, ok in (("refspecs", _REFSPEC.fullmatch), ("verbs", _GRANT_VERBS.__contains__)):
        items = grant[name]
        if not isinstance(items, (list, tuple)) or not items or not all(isinstance(i, str) and ok(i) for i in items):
            raise AuthorityError(f"{what}.{name} is not a non-empty list of allowed values")
        # sorted and unique, so equal sets are equal bytes
        if list(items) != sorted(set(items)):
            raise AuthorityError(f"{what}.{name} must be sorted and unique")
    _parse_stamp(grant["expires_at"], f"{what}.expires_at")
    return _thaw(grant)


def _validate_policy(data: Any, *, at: datetime | None = None) -> StandingPolicy:
    if not isinstance(data, Mapping) or set(data) != {"raise_to", "timeouts", "grants"}:
        raise AuthorityError("a policy must carry exactly raise_to, timeouts and grants")
    raise_to, timeouts, grants = data["raise_to"], data["timeouts"], data["grants"]
    tiers = _tiers()
    if not isinstance(raise_to, Mapping) or not all(isinstance(v, str) and v and t in tiers
                                                    for v, t in raise_to.items()):
        raise AuthorityError("policy.raise_to must map verbs to tiers")
    _validate_timeouts(timeouts)
    if not isinstance(grants, Mapping):
        raise AuthorityError("policy.grants must be a mapping")
    for gid, grant in grants.items():
        g = _validate_grant(grant, f"policy.grants[{gid!r}]", at=at)
        if g["id"] != gid:
            raise AuthorityError(f"policy.grants[{gid!r}] carries id {g['id']!r}")
    return StandingPolicy.from_dict(data)


def _finite_json(value: Any) -> bool:
    """Every leaf a finite JSON scalar (NaN and Infinity are not JSON)."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and _finite_json(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_finite_json(v) for v in value)
    return False


def _validate_timeouts(timeouts: Any) -> None:
    if not isinstance(timeouts, Mapping) or not all(isinstance(k, str) and k for k in timeouts):
        raise AuthorityError("timeouts must map pending kinds to values")
    if not _finite_json(timeouts):
        raise AuthorityError("timeouts values must be finite JSON (no NaN or Infinity)")


def _apply_policy_op(policy: StandingPolicy, rec: Mapping[str, Any]) -> StandingPolicy:
    """The policy after the declared op, refusing a malformed or impossible one. Does NOT judge the
    delta's kind: that is :func:`classify`, on the result alone."""
    op = rec["op"]
    data = policy.as_dict()
    if op == "raise":
        verb, tier = rec["verb"], rec["tier"]
        if not isinstance(verb, str) or not verb or tier not in _tiers():
            raise AuthorityError("raise needs a verb and a tier")
        data["raise_to"][verb] = tier
    elif op == "loosen":
        return _validate_policy(rec["policy"], at=_parse_stamp(rec["at"], "at"))
    elif op == "timeouts":
        _validate_timeouts(rec["timeouts"])
        data["timeouts"] = _thaw(rec["timeouts"])
    elif op == "create_grant":
        at = _parse_stamp(rec["at"], "at")
        grant = _validate_grant(rec["grant"], "grant", at=at)
        if grant["id"] in data["grants"]:
            raise AuthorityError(f"grant {grant['id']!r} already exists")
        if grant.get("kind") == CONSENT_GRANT_KIND and _parse_stamp(grant["expires_at"], "expires_at") <= at:
            raise AuthorityError("a consent grant must expire after the link that creates it")
        data["grants"][grant["id"]] = grant
    elif op in ("revoke_grant", "renew"):
        gid = rec["grant_id"]
        if not isinstance(gid, str) or gid not in data["grants"]:
            raise AuthorityError(f"no grant {gid!r}")
        if op == "revoke_grant":
            del data["grants"][gid]
        else:
            old = _parse_stamp(data["grants"][gid]["expires_at"], "the grant's expires_at")
            new = _parse_stamp(rec["expires_at"], "expires_at")
            signed = _parse_stamp(rec["signed_at"], "signed_at")
            # signed_at IS the link's at, which the ordering rule bounds below; otherwise a replayed
            # signed_at would be an unbounded input to the 30-day rule
            if rec["signed_at"] != rec["at"]:
                raise AuthorityError("a renew's signed_at must equal its at")
            if old <= signed:
                raise AuthorityError(f"grant {gid!r} has expired ({data['grants'][gid]['expires_at']}) by this "
                                     "renew's signing time; a lapsed grant is re-created at the laptop")
            if new <= old:
                raise AuthorityError("a renew must move the grant's expiry later")
            # a difference, never `signed + 30 days`, which overflows near year 9999
            if new - signed > _RENEW_MAX:
                raise AuthorityError("a renew may extend a grant at most 30 days past its signing time")
            data["grants"][gid]["expires_at"] = rec["expires_at"]
    else:
        raise AuthorityError(f"{op!r} is not a policy op")
    return StandingPolicy.from_dict(data)


def _grant_change(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """``same``, ``renew`` (only a later expiry), ``tight`` (provably narrower) or ``loose``."""
    o, n = _thaw(old), _thaw(new)
    if _canonical(o) == _canonical(n):
        return "same"
    if o.get("kind") != n.get("kind"):
        return "loose"
    o_exp, n_exp = _parse_stamp(o["expires_at"], "expires_at"), _parse_stamp(n["expires_at"], "expires_at")
    if n.get("kind") == CONSENT_GRANT_KIND:
        return _consent_grant_change(o, n, o_exp, n_exp)
    rest = [k for k in _GRANT_FIELDS if k not in ("refspecs", "verbs", "expires_at")]
    if any(o[k] != n[k] for k in rest):
        return "loose"
    narrower_sets = set(n["refspecs"]) <= set(o["refspecs"]) and set(n["verbs"]) <= set(o["verbs"])
    sets_equal = n["refspecs"] == o["refspecs"] and n["verbs"] == o["verbs"]
    if sets_equal and n_exp > o_exp:
        return "renew"
    if narrower_sets and n_exp <= o_exp:
        return "tight"
    return "loose"


def _path_within(path: str, root: str) -> bool:
    """``path`` is ``root`` or under it (both absolute normal forms; a string test, so ``/a/bc`` is not in ``/a/b``)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _consent_grant_change(o: Mapping[str, Any], n: Mapping[str, Any], o_exp: datetime, n_exp: datetime) -> str:
    """A consent grant's change: ``renew`` when only the expiry moved later; ``tight`` when the id and agent
    are the same, tools and postures a subset, the workspace the same or under the old one, and the expiry no
    later; anything else ``loose``."""
    if o["id"] != n["id"] or o["agent"] != n["agent"]:
        return "loose"
    if n["tools"] == o["tools"] and n["postures"] == o["postures"] and n["workspace"] == o["workspace"] \
            and n_exp > o_exp:
        return "renew"
    narrower = set(n["tools"]) <= set(o["tools"]) and set(n["postures"]) <= set(o["postures"]) \
        and isinstance(n["workspace"], str) and isinstance(o["workspace"], str) \
        and _path_within(n["workspace"], o["workspace"])
    return "tight" if narrower and n_exp <= o_exp else "loose"


def classify(old: StandingPolicy, new: StandingPolicy) -> str:
    """The kind of the difference from ``old`` to ``new``, computed from the two policies alone (§4.3).
    :data:`NOOP` when nothing changes in effect (a missing floor and a floor at the lowest tier are the
    same). Anything else not PROVABLY narrower is :data:`LOOSEN`; a grant extended and nothing
    else changed is :data:`RENEW`; one grant removed and nothing else is :data:`GRANT_REVOKE`."""
    if old == new:
        return NOOP
    changes: list[str] = []
    tiers = _tiers()
    for verb in set(old.raise_to) | set(new.raise_to):
        # no floor IS the lowest tier (apply never lowers), so a raise to it, or dropping it, changes nothing
        rank_o = tiers.index(old.raise_to.get(verb, tiers[0]))
        rank_n = tiers.index(new.raise_to.get(verb, tiers[0]))
        if rank_n != rank_o:
            changes.append("tight" if rank_n > rank_o else "loose")
    # timeout semantics are S4b's: until then no change is provably stricter
    if _canonical(_thaw(old.timeouts)) != _canonical(_thaw(new.timeouts)):
        changes.append("loose")
    for gid in set(old.grants) | set(new.grants):
        if gid not in new.grants:
            changes.append("revoke")
        elif gid not in old.grants:
            changes.append("loose")
        else:
            kind = _grant_change(old.grants[gid], new.grants[gid])
            if kind != "same":
                changes.append(kind)
    if "loose" in changes:
        return LOOSEN
    if "renew" in changes:
        return RENEW if len(changes) == 1 else LOOSEN
    if changes == ["revoke"]:
        return GRANT_REVOKE
    return TIGHTEN if changes else NOOP


# ---------------------------------------------------------------------------------------------------
# replay


def _parse_record(row: _Row) -> dict[str, Any]:
    seq = row.seq
    if not isinstance(row.record, str):
        raise ReplayError(seq, "the record is not text")
    try:
        rec = json.loads(row.record)
    except (ValueError, RecursionError):
        raise ReplayError(seq, "the record is not JSON") from None
    try:
        canonical = isinstance(rec, dict) and _canonical(rec) == row.record
    except RecursionError:
        raise ReplayError(seq, "the record is nested too deeply") from None
    if not canonical:
        raise ReplayError(seq, "the record is not a canonical JSON object")
    if rec.get("format") != FORMAT:
        raise ReplayError(seq, f"the record's format is not {FORMAT}")
    op = rec.get("op")
    if not isinstance(op, str) or op not in _OP_FIELDS:
        raise ReplayError(seq, f"unknown op {op!r}")
    required, optional = _OP_FIELDS[op]
    have = set(rec) - _BASE_FIELDS
    if not _BASE_FIELDS <= set(rec) or not required <= have or not have <= required | optional:
        raise ReplayError(seq, f"a {op} record carries exactly {', '.join(sorted(_BASE_FIELDS | required))}"
                               + (f" (optionally {', '.join(sorted(optional))})" if optional else ""))
    if rec["seq"] != seq or type(rec["seq"]) is not int:
        raise ReplayError(seq, f"the record names seq {rec['seq']!r}")
    return rec


def _enrolled_key(rec: Mapping[str, Any], seq: int, keys: Mapping[str, AuthorityKey],
                  ssh_keygen: Path | str) -> AuthorityKey:
    from levain.cockpit.keys import KeyError_, fingerprint_of, normalise_public_key
    fp, public_key, cls = rec["fingerprint"], rec["public_key"], rec["class"]
    if not isinstance(cls, str) or cls not in _ranks():
        raise AuthorityError(f"unknown key class {cls!r}")
    if not isinstance(public_key, str) or not isinstance(fp, str):
        raise AuthorityError("enrol needs a fingerprint and a public key")
    try:
        normal = normalise_public_key(public_key)
        if normal != public_key:
            raise AuthorityError("the public key is not in its normal form (<type> <base64>)")
        computed = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    except KeyError_ as exc:
        raise AuthorityError(str(exc)) from None
    if computed != fp:
        raise AuthorityError(f"the record's fingerprint {fp} is not the key's ({computed})")
    legacy = rec.get("legacy_enrolled_at")
    if legacy is not None and not isinstance(legacy, str):
        raise AuthorityError("legacy_enrolled_at must be text")
    custody = rec["custody"]
    problem = custody_problem(cls, custody, public_key.split()[0], legacy=legacy is not None)
    if problem is not None:
        raise AuthorityError(problem)
    old = keys.get(fp)
    if old is not None:
        if not old.active:
            raise AuthorityError(f"{fp} was revoked at seq {old.revoked_seq}; a revoked key stays revoked")
        if old.key_class != cls:
            raise AuthorityError(f"{fp} is enrolled as {old.key_class!r}; a key keeps its class")
        raise AuthorityError(f"{fp} is already enrolled")
    return AuthorityKey(fp, public_key, cls, seq, legacy_enrolled_at=legacy, enrolled_at=rec["at"], custody=custody)


def _step(state: AuthoritySnapshot | None, row: _Row, *, ssh_keygen: Path | str,
          check_signature: bool = True) -> AuthoritySnapshot:
    """The state after ``row``, judged against ``state`` (its predecessor), or :class:`ReplayError`."""
    expected = 1 if state is None else state.seq + 1
    seq = row.seq
    if type(seq) is not int or seq != expected:
        raise ReplayError(expected, f"expected seq {expected}, found {seq!r} (a link is missing or out of order)")
    for name in _TEXT_COLUMNS:
        if not isinstance(getattr(row, name), str):
            raise ReplayError(seq, f"{name} is not UTF-8 text")
    prev = "" if state is None else state.head_hash
    if row.prev_hash != prev:
        raise ReplayError(seq, "prev_hash is not the predecessor's hash (the chain is broken)")
    if not isinstance(row.record, str) or row.hash != _link_hash(prev, row.record):
        raise ReplayError(seq, "hash does not match prev_hash + record")
    rec = _parse_record(row)
    op = rec["op"]
    # `at` is an ORDERING constraint, never a grant of authority (amends DESIGN §3.1): a link is never
    # earlier than its predecessor, so a future-dated inserted link wedges every later append visibly
    # (an append is stamped now) instead of silently widening a clock-bounded rule such as renew's
    try:
        at = _parse_stamp(rec["at"], "at")
    except AuthorityError as exc:
        raise ReplayError(seq, str(exc)) from None
    if state is not None and state.head_at is not None and at < state.head_at:
        raise ReplayError(seq, f"at {rec['at']} is earlier than its predecessor's "
                               f"{_stamp(state.head_at)}; the log's times never go back")
    if state is None:
        if not isinstance(rec["store_id"], str) or not _STORE_ID.fullmatch(rec["store_id"]):
            raise ReplayError(seq, "the genesis store_id is malformed")
    elif rec["store_id"] != state.store_id:
        raise ReplayError(seq, f"the record names store {rec['store_id']!r}, not {state.store_id!r}")
    if not isinstance(row.signer_fp, str) or not _FP.fullmatch(row.signer_fp):
        raise ReplayError(seq, "signer_fp is malformed")

    ranks = _ranks()
    keys = {} if state is None else dict(state.keys)
    policy = genesis_policy() if state is None else state.policy
    grant_seqs = {} if state is None else dict(state.grant_seqs)
    try:
        if state is None:
            # TOFU genesis (§3.2): the first link enrols a laptop-presence key, signed by that key
            if op != "enrol" or rec["class"] != LAPTOP:
                raise AuthorityError(f"the first link must enrol a {LAPTOP} key")
            if row.signer_fp != rec["fingerprint"]:
                raise AuthorityError("the first link must be signed by the key it enrols")
            signer = _enrolled_key(rec, seq, keys, ssh_keygen)
            keys[signer.fingerprint] = signer
        else:
            found = keys.get(row.signer_fp)
            if found is None:
                raise AuthorityError(f"signer {row.signer_fp} is not enrolled")
            signer = found
            if not signer.active:
                raise AuthorityError(f"signer {row.signer_fp} was revoked at seq {signer.revoked_seq}")
            need = _apply_op(rec, seq, state, keys, ssh_keygen)
            if isinstance(need, StandingPolicy):
                delta = classify(policy, need)
                if delta == NOOP:
                    raise AuthorityError(f"this {op} changes nothing")
                if delta not in _OP_DELTAS[op]:
                    raise AuthorityError(f"a {op} link computes to a {delta} change")
                rank = _DELTA_RANK[delta]
                grant_seqs = {gid: (grant_seqs.get(gid, seq) if gid in policy.grants and _canonical(
                    _thaw(policy.grants[gid])) == _canonical(_thaw(g)) else seq) for gid, g in need.grants.items()}
                policy = need
            else:
                rank = need
            if ranks[signer.key_class] < rank:
                raise AuthorityError(f"a {signer.key_class} key cannot sign this {op}")
    except AuthorityError as exc:
        if isinstance(exc, (ReplayError, VerifierUnavailable)):
            raise
        raise ReplayError(seq, str(exc)) from None
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        # a field of a shape no rule above anticipated: still a refusal at this seq, never an escape
        raise ReplayError(seq, f"a malformed field ({type(exc).__name__})") from None

    if check_signature:
        ok = verify_link_sig(_message(prev, row.record), row.signature, public_key=signer.public_key,
                             fingerprint=signer.fingerprint, ssh_keygen=ssh_keygen)
        if not ok:
            raise ReplayError(seq, f"the signature does not verify as {signer.fingerprint}")
    hashes = (() if state is None else state._hashes) + (row.hash,)
    return AuthoritySnapshot(rec["store_id"], seq, row.hash, MappingProxyType(keys), policy, hashes, head_at=at,
                             grant_seqs=MappingProxyType(grant_seqs))


def _apply_op(rec: Mapping[str, Any], seq: int, state: AuthoritySnapshot, keys: dict[str, AuthorityKey],
              ssh_keygen: Path | str) -> int | StandingPolicy:
    """Applies a key op to ``keys`` and returns the signer rank it needs, or returns the new policy for
    a policy op (whose rank follows from its computed delta)."""
    op = rec["op"]
    ranks = _ranks()
    if op == "enrol":
        key = _enrolled_key(rec, seq, keys, ssh_keygen)
        keys[key.fingerprint] = key
        return ranks[LAPTOP]
    if op == "revoke_key":
        fp = rec["fingerprint"]
        target = keys.get(fp) if isinstance(fp, str) else None
        if target is None:
            raise AuthorityError(f"no enrolled key {fp!r}")
        if not target.active:
            raise AuthorityError(f"{fp} is already revoked (at seq {target.revoked_seq})")
        since = rec.get("compromised_since")
        if since is not None and (type(since) is not int or not target.enrolled_seq <= since <= state.seq):
            raise AuthorityError(f"compromised_since must be a seq between {target.enrolled_seq} and {state.seq}")
        if target.key_class == LAPTOP and since is None and not any(
                k.active and k.key_class == LAPTOP and k.fingerprint != fp for k in keys.values()):
            raise AuthorityError(f"{fp} is the last unrevoked {LAPTOP} key; enrol another first, or "
                                 "revoke it with compromised_since if it is lost or stolen")
        keys[fp] = replace(target, revoked_seq=seq, compromised_since=since)
        return ranks[target.key_class]
    return _apply_policy_op(state.policy, rec)


def _db_identity(conn: sqlite3.Connection) -> str:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return os.path.realpath(path) if path else f"memory:{id(conn)}"
    return f"memory:{id(conn)}"


@dataclass(frozen=True)
class _Entry:
    """A verified chain: the state after every link, and the digest of every row."""
    states: tuple[AuthoritySnapshot, ...]
    digests: tuple[str, ...]

    @property
    def snapshot(self) -> AuthoritySnapshot:
        return self.states[-1]


class ReplayCache:
    """Per-process verified chains, keyed by store: the state after each link and the digest of each
    row. A log whose rows match the cached chain verifies only its suffix. A log that is a
    digest-matching PREFIX of the cached chain is served its own head from the cache: that is a reader
    whose transaction began before a concurrent append (a busy cockpit), and its view is a state this
    process already verified. A log that differs from the chain at or below a seq both hold is replayed
    in full: if it verifies it replaces the cached chain (a restore, then an anchor reset), and if it
    does not it is refused (:class:`HeadBelowCache`). A log truncated to a prefix reads as that prefix;
    the anchor, not this cache, is what refuses a rollback (:func:`check_anchor`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}

    def get(self, conn: sqlite3.Connection, store_id: str, seq: int, head_hash: str) -> AuthoritySnapshot | None:
        """The cached snapshot for exactly this head, or None."""
        with self._lock:
            entry = self._entries.get((_db_identity(conn), store_id))
        if entry is not None and (entry.snapshot.seq, entry.snapshot.head_hash) == (seq, head_hash):
            return entry.snapshot
        return None

    def _load(self, key: tuple[str, str]) -> _Entry | None:
        with self._lock:
            return self._entries.get(key)

    def _store(self, key: tuple[str, str], entry: _Entry) -> None:
        """Keep the longer of two agreeing chains; a verified chain that disagrees with the cached one
        below the shorter head replaces it (the last verified chain stands, see :func:`_replay_entry`)."""
        with self._lock:
            old = self._entries.get(key)
            both = 0 if old is None else min(len(old.digests), len(entry.digests))
            if old is None or entry.snapshot.seq > old.snapshot.seq \
                    or entry.digests[:both] != old.digests[:both]:
                self._entries[key] = entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


PROCESS_CACHE = ReplayCache()


def _genesis_store_id(rows: list[_Row]) -> str:
    first = rows[0]
    try:
        rec = json.loads(first.record) if isinstance(first.record, str) else None
    except ValueError:
        rec = None
    store_id = rec.get("store_id") if isinstance(rec, dict) else None
    return store_id if isinstance(store_id, str) else ""


def _replay_entry(conn: sqlite3.Connection, cache: ReplayCache, ssh_keygen: Path | str) -> _Entry | None:
    rows = _rows(conn)
    if not rows:
        return None
    key = (_db_identity(conn), _genesis_store_id(rows))
    entry = cache._load(key)
    digests = tuple(r.digest() for r in rows)
    states: tuple[AuthoritySnapshot, ...] = ()
    diverged: str | None = None
    if entry is not None:
        n = len(entry.digests)
        both = min(n, len(rows))
        if digests[:both] != entry.digests[:both]:
            # nothing cached is reusable: replay from genesis, and the result replaces the cached chain
            diverged = f"the chain this process verified (to seq {n}) at or below seq {both}"
        elif len(rows) <= n:
            return _Entry(entry.states[:len(rows)], digests)
        else:
            states = entry.states
    state = states[-1] if states else None
    grown = list(states)
    try:
        for row in rows[len(states):]:
            state = _step(state, row, ssh_keygen=ssh_keygen)
            grown.append(state)
    except ReplayError as exc:
        if diverged is None:
            raise
        raise HeadBelowCache(f"the authority log differs from {diverged}; it was rewritten ({exc})") from None
    out = _Entry(tuple(grown), digests)
    cache._store(key, out)
    return out


def _replay(conn: sqlite3.Connection, cache: ReplayCache, ssh_keygen: Path | str) -> AuthoritySnapshot | None:
    entry = _replay_entry(conn, cache, ssh_keygen)
    return None if entry is None else entry.snapshot


def snapshot(conn: sqlite3.Connection, *, cache: ReplayCache | None = None,
             ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> AuthoritySnapshot:
    """The authority as a verified replay of ``authority_log`` in ``conn``'s current snapshot (run it
    inside the caller's read transaction so it and the caller's other reads see one head)."""
    state = _replay(conn, PROCESS_CACHE if cache is None else cache, ssh_keygen)
    if state is None:
        raise NotInitialised("the authority log is empty: run `levain cockpit ledger init`")
    return state


# ---------------------------------------------------------------------------------------------------
# append


@dataclass(frozen=True)
class UnsignedLink:
    """A link ready to sign: ``message`` is what the signer signs (namespace :data:`NAMESPACE`)."""
    seq: int
    prev_hash: str
    record: str
    signer_fp: str

    @property
    def message(self) -> bytes:
        return _message(self.prev_hash, self.record)


def _require_aware(now: Any) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AuthorityError("`now` must be an aware datetime; a naive one has no zone")


def _consent_home_check(grants: list[Any]) -> None:
    home = os.path.expanduser("~")
    homes = {os.path.normpath(home), os.path.realpath(home)}
    for grant in grants:
        if not isinstance(grant, Mapping) or grant.get("kind") != CONSENT_GRANT_KIND:
            continue
        ws = grant.get("workspace")
        if isinstance(ws, str) and any(_path_within(h, ws) for h in homes):
            raise AuthorityError(f"the workspace {ws} is the home directory or above it; name a project directory")


def _append_only_checks(state: AuthoritySnapshot, rec: Mapping[str, Any], now: datetime) -> None:
    """The rules a replay cannot hold because they need the appender's clock (§3.3a, §6), or its home
    directory: a consent grant's workspace is never the home directory or above it (that grants every
    workspace under it), judged for every consent grant a create or a loosen brings in or changes."""
    if rec["op"] == "create_grant":
        _consent_home_check([rec["grant"]])
    elif rec["op"] == "loosen":
        policy = rec["policy"]
        grants = policy.get("grants", {}) if isinstance(policy, Mapping) else {}
        old = state.policy.grants
        _consent_home_check([g for gid, g in grants.items()
                             if gid not in old or _canonical(_thaw(old[gid])) != _canonical(_thaw(g))])
    if rec["op"] != "renew":
        return
    grant = state.policy.grants.get(rec["grant_id"])
    if grant is None:
        raise AuthorityError(f"no grant {rec['grant_id']!r}")
    signed = _parse_stamp(rec["signed_at"], "signed_at")
    if abs(now - signed) > _RENEW_SKEW:
        raise AuthorityError("a renew's signed_at must be within 10 minutes of now")
    if _parse_stamp(grant["expires_at"], "the grant's expires_at") <= now:
        raise AuthorityError(f"grant {rec['grant_id']!r} has expired; a lapsed grant is re-created at the laptop")
    if _parse_stamp(rec["expires_at"], "expires_at") - now > _RENEW_MAX:      # never now + 30 days: overflow
        raise AuthorityError("a renew may extend a grant at most 30 days from now")


def prepare(conn: sqlite3.Connection, op: Mapping[str, Any], *, signer_fp: str, now: datetime,
            store_id: str | None = None, cache: ReplayCache | None = None,
            ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> UnsignedLink:
    """The next link carrying ``op`` (its op fields, ``op`` included), checked against the current head
    by every rule but the signature, so a doomed link is refused before anyone touches a key.
    ``store_id`` is for the genesis link only (a fresh one is drawn when it is None)."""
    _require_aware(now)
    if "op" not in op or set(op) & (_BASE_FIELDS - {"op"}):
        raise AuthorityError("an op names `op` and its own fields, never format/store_id/seq/at")
    state = _replay(conn, PROCESS_CACHE if cache is None else cache, ssh_keygen)
    if state is None:
        sid = store_id if store_id is not None else secrets.token_hex(16)
    else:
        if store_id is not None and store_id != state.store_id:
            raise AuthorityError(f"this log's store is {state.store_id!r}")
        sid = state.store_id
    seq = 1 if state is None else state.seq + 1
    prev = "" if state is None else state.head_hash
    try:
        record = _canonical({**op, "format": FORMAT, "store_id": sid, "seq": seq, "at": _stamp(now)})
        row = _Row(seq, prev, record, signer_fp, "", _link_hash(prev, record))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        # not JSON, a lone surrogate (no UTF-8 form), or nested too deeply
        raise AuthorityError(f"the op has no canonical UTF-8 form ({type(exc).__name__})") from None
    _step(state, row, ssh_keygen=ssh_keygen, check_signature=False)
    if state is not None:
        _append_only_checks(state, json.loads(record), now)
    return UnsignedLink(seq, prev, record, signer_fp)


def append(conn: sqlite3.Connection, link: UnsignedLink, signature: str, *, now: datetime,
           anchor: Path | None, cache: ReplayCache | None = None,
           ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> AuthoritySnapshot:
    """Append a signed link in one ``BEGIN IMMEDIATE``: replay to the head, refuse a moved head
    (:class:`StaleHead`), verify the link against that state as a replay would, apply the append-only
    clock checks, insert. ``conn`` must be in autocommit mode with no open transaction.

    ``anchor`` is required: the anchor file's path, or an explicit None for an unanchored store. With
    one, a log behind it refuses (an empty log beside an existing anchor included), and after the commit
    the anchor advances. Once committed the link stands: if the anchor cannot then be advanced, the
    snapshot comes back with ``anchor_warning`` set rather than as a failed append."""
    _require_aware(now)
    if conn.in_transaction:
        raise AuthorityError("append owns its transaction; the connection has one open")
    cache = PROCESS_CACHE if cache is None else cache
    conn.execute("BEGIN IMMEDIATE")
    try:
        entry = _replay_entry(conn, cache, ssh_keygen)
        state = None if entry is None else entry.snapshot
        if anchor is not None:
            current = read_anchor(anchor)
            if state is None and current is not None:
                raise AnchorError(f"the authority log is empty but its anchor names store {current.store_id} at "
                                  f"seq {current.seq}; a new genesis here would fork it. If this store was "
                                  f"emptied on purpose, run {_RESET_HINT}")
            if state is not None:
                check_anchor(state, current)
        head = (0, "") if state is None else (state.seq, state.head_hash)
        if head != (link.seq - 1, link.prev_hash):
            raise StaleHead(f"the authority log's head moved to seq {head[0]} since this link was prepared")
        row = _Row(link.seq, link.prev_hash, link.record, link.signer_fp, signature,
                   _link_hash(link.prev_hash, link.record))
        new = _step(state, row, ssh_keygen=ssh_keygen)
        if state is not None:
            _append_only_checks(state, json.loads(link.record), now)
        conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (row.seq, row.prev_hash, row.record, row.signer_fp, row.signature, row.hash))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    prior = _Entry((), ()) if entry is None else entry
    cache._store((_db_identity(conn), new.store_id),
                 _Entry(prior.states + (new,), prior.digests + (row.digest(),)))
    if anchor is not None:
        try:
            advance_anchor(anchor, new)
        except (AnchorError, OSError) as exc:
            return replace(new, anchor_warning=f"committed at seq {new.seq}; the anchor is behind ({exc})")
    return new


def sign_and_append(conn: sqlite3.Connection, op: Mapping[str, Any], *, signer_fp: str,
                    sign: Callable[[bytes], str], clock: Callable[[], datetime], anchor: Path | None,
                    store_id: str | None = None, cache: ReplayCache | None = None,
                    ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> AuthoritySnapshot:
    """:func:`prepare`, then ``sign(message)`` OUTSIDE any transaction (a touch can take seconds),
    then :func:`append` (``anchor`` as there: required, None for an unanchored store). ``clock`` is read
    once for prepare and again AFTER the signature, so append's clock checks (a renew of a grant that
    lapsed during the touch) judge the time of the commit, not the time the touch began."""
    link = prepare(conn, op, signer_fp=signer_fp, now=clock(), store_id=store_id, cache=cache,
                   ssh_keygen=ssh_keygen)
    signature = sign(link.message)
    return append(conn, link, signature, now=clock(), cache=cache, ssh_keygen=ssh_keygen, anchor=anchor)


# ---------------------------------------------------------------------------------------------------
# the anchor (§3.8)


@dataclass(frozen=True)
class Anchor:
    store_id: str
    seq: int
    head_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {"store_id": self.store_id, "seq": self.seq, "head_hash": self.head_hash}


_RESET_HINT = "`levain cockpit anchor reset`"


def anchor_path(data_root: Path | str) -> Path:
    """``<data_root>/anchor/authority.anchor``: its own directory, so a backup can leave it out while it
    takes the store (:func:`_anchor_dir`)."""
    return Path(data_root) / ANCHOR_DIR / ANCHOR_NAME


def _exclude_from_time_machine(directory: Path) -> None:
    """Set the sticky Time Machine exclusion on ``directory``, best effort: off macOS, or when the
    attribute cannot be set, the anchor works as before and nothing is recorded."""
    if sys.platform != "darwin":
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.setxattr
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                       ctypes.c_int]
        fn(os.fsencode(directory), _TM_EXCLUDE_ATTR, _TM_EXCLUDE_VALUE, len(_TM_EXCLUDE_VALUE), 0, 0)
    except (OSError, AttributeError):
        pass


def _anchor_dir(path: Path) -> None:
    """Make the anchor's directory and exclude it from Time Machine. The exclusion is on the directory,
    never the file: :func:`_write_anchor`'s rename replaces the file's inode on every advance."""
    directory = Path(path).parent
    directory.mkdir(mode=0o700, exist_ok=True)
    _exclude_from_time_machine(directory)


def read_anchor(path: Path) -> Anchor | None:
    """The anchor at ``path``; None when there is no file. Anything unreadable refuses."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AnchorError(f"the authority anchor {path} cannot be read ({type(exc).__name__}); "
                          f"after checking the log, run {_RESET_HINT}") from None
    try:
        data = json.loads(raw.decode("utf-8"))
        if data.get("format") != ANCHOR_FORMAT:
            raise ValueError
        store_id, seq, head = data["store_id"], data["seq"], data["head_hash"]
        if not isinstance(store_id, str) or type(seq) is not int or seq < 1 or not isinstance(head, str) \
                or not _SHA256_HEX.fullmatch(head):
            raise ValueError
    except (ValueError, KeyError, AttributeError, UnicodeDecodeError, RecursionError):
        raise AnchorError(f"the authority anchor {path} is unreadable; after checking the log, run "
                          f"{_RESET_HINT}") from None
    return Anchor(store_id, seq, head)


def check_anchor(snap: AuthoritySnapshot, anchor: Anchor | None) -> None:
    """Refuse a log behind ``anchor``: another store, fewer links, or another hash at the anchor's seq."""
    if anchor is None:
        return
    here = f"log store {snap.store_id} at seq {snap.seq} ({snap.head_hash[:12]})"
    there = f"anchor store {anchor.store_id} at seq {anchor.seq} ({anchor.head_hash[:12]})"
    if anchor.store_id != snap.store_id or snap.seq < anchor.seq or snap.hash_at(anchor.seq) != anchor.head_hash:
        raise AnchorError(f"the authority log is behind its anchor: {here}, {there}. If this store was "
                          f"restored on purpose, run {_RESET_HINT}")


def _write_anchor(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".authority-anchor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_canonical(payload) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@contextmanager
def _anchor_lock(path: Path) -> Iterator[None]:
    """An exclusive flock on ``<anchor>.lock``: every read-compare-write of the anchor runs under it, so
    two appenders (the cockpit server and the CLI) serialise their advances."""
    path = Path(path)
    _anchor_dir(path)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def advance_anchor(path: Path, snap: AuthoritySnapshot) -> Anchor:
    """Check the anchor at ``path`` against ``snap`` and move it up to ``snap``'s head; call only after
    the commit that produced ``snap``, so the anchor never leads the log. An anchor of this store already
    past ``snap`` (a later append's advance landed first) is left where it is: the anchor never moves
    down, and a stale advance is a no-op that returns the anchor as it stands."""
    with _anchor_lock(path):
        current = read_anchor(path)
        if current is not None and current.store_id == snap.store_id and current.seq > snap.seq:
            return current
        check_anchor(snap, current)
        new = Anchor(snap.store_id, snap.seq, snap.head_hash)
        if current != new:
            _write_anchor(path, {"format": ANCHOR_FORMAT, **new.as_dict()})
        return new


def reset_message(store_id: str, old_anchor: Mapping[str, Any] | None, new_seq: int, new_head: str) -> bytes:
    """What the operator signs (namespace :data:`RESET_NAMESPACE`) to move an anchor to ``new_head``."""
    return _canonical({"format": RESET_FORMAT, "store_id": store_id, "old_anchor": old_anchor,
                       "new_head": {"seq": new_seq, "head_hash": new_head}}).encode("utf-8")


def _old_anchor_view(path: Path) -> Mapping[str, Any] | None:
    """The anchor a reset replaces: its fields when readable, else the digest of its bytes."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        current = read_anchor(path)
    except AnchorError:
        return {"sha256": hashlib.sha256(raw).hexdigest()}
    return None if current is None else current.as_dict()


def verify_anchor_reset(snap: AuthoritySnapshot, old_anchor: Mapping[str, Any] | None, *, signer_fp: str,
                        signature: str, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> None:
    """Refuse unless ``signature`` is a laptop-presence signature over the reset of ``old_anchor`` to
    ``snap``'s head, by a key the log itself holds unrevoked as of that head."""
    key = snap.key(signer_fp)
    if key is None or not key.active:
        raise AnchorError(f"{signer_fp} is not an unrevoked key of this log at seq {snap.seq}")
    if key.key_class != LAPTOP:
        raise AnchorError(f"an anchor reset needs a {LAPTOP} key, not {key.key_class}")
    message = reset_message(snap.store_id, old_anchor, snap.seq, snap.head_hash)
    try:
        ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                             namespace=RESET_NAMESPACE, ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise AnchorError(str(exc)) from None
    if not ok:
        raise AnchorError(f"the anchor reset signature does not verify as {signer_fp}")


@contextmanager
def _no_live_cockpit(path: Path) -> Iterator[None]:
    """Hold the store's exclusive lock: the non-blocking ``flock`` on ``<data_root>/cockpit.lock`` that
    a live cockpit broker holds for its whole active life (``levain.cockpit.broker.LOCK_NAME``). The
    anchor sits in a directory of that data root (:func:`anchor_path`). Held, a reset is refused rather than waited
    for: a server still holding the abandoned store could advance the anchor back onto it."""
    from levain.cockpit.broker import LOCK_NAME
    lock = Path(path).parent.parent / LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AnchorError(f"a cockpit server holds {lock}: stop it, then run {_RESET_HINT}") from None
        yield
    finally:
        os.close(fd)


def reset_anchor(path: Path, snap: AuthoritySnapshot, *, signer_fp: str, signature: str,
                 ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> Anchor:
    """Move the anchor at ``path`` to ``snap``'s head on a verified reset signature, storing the proof
    in the anchor file (the log refuses to open, so it cannot be a log link). Refused while a cockpit
    server is live on the store (:func:`_no_live_cockpit`)."""
    with _no_live_cockpit(path), _anchor_lock(path):
        old = _old_anchor_view(path)
        verify_anchor_reset(snap, old, signer_fp=signer_fp, signature=signature, ssh_keygen=ssh_keygen)
        new = Anchor(snap.store_id, snap.seq, snap.head_hash)
        _write_anchor(path, {"format": ANCHOR_FORMAT, **new.as_dict(),
                             "reset": {"old_anchor": old, "signer_fp": signer_fp, "signature": signature}})
        return new
