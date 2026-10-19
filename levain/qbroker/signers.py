"""levain.qbroker.signers — the answer-signer snapshot (CONTRACT c7 §3.7), verified before any answer is.

``L/policy/allowed_signers.json`` is the canonical ``levain-allowed-signers/1`` record ``{generation, issued_at,
signers: [{principal, key_type, key_b64, key_class, custody, namespaces}]}`` (``key_class`` and ``custody`` checked
by the consent authority's own rules: a class it ranks, a custody ``custody_problem`` admits for that class and key
type); ``allowed_signers.json.sig`` is an SSHSIG over its bytes by the dispatch trust root (``L/routes/trust.pub``)
in its own namespace, :data:`SIGNERS_NAMESPACE` (never a config item's). :class:`SignersSource` re-verifies the pair whenever any of the three files changes or the last try could
not run the verifier, and accepts a generation only when it is strictly greater than the last it accepted (the same
bytes read again are not a change). While the pair is missing, unverifiable or a rollback, :meth:`SignersSource.current` returns ``None`` and
every answer is refused (fail closed).

The high-water generation lives in this process only: a restarted broker accepts any validly signed generation,
and only the install helper's own check (strictly greater before the rename) stops a rollback across a restart.
Stdlib only.
"""
from __future__ import annotations

import os
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.authority import VerifierUnavailable, custody_problem, verify_link_sig
from levain.cockpit.firejournal import _CLASS_RANK
from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse
from levain.contracts.keys import fingerprint_line

__all__ = ["SIGNERS_FORMAT", "SIGNERS_NAMESPACE", "AllowedSigner", "SignersSnapshot",
           "SignersSource", "load"]

SIGNERS_NAMESPACE = "levain-allowed-signers"
SIGNERS_FORMAT = "levain-allowed-signers/1"
_KEYS = frozenset({"format", "generation", "issued_at", "signers"})
# CONTRACT c7 §3.7: key_class + custody as the consent queue's authority defines them, signed with the rest
_SIGNER_KEYS = frozenset({"principal", "key_type", "key_b64", "key_class", "custody", "namespaces"})
# a signers file is a handful of public keys; a signature is under a kilobyte (a passkey's, a few)
_MAX_FILE = 256 * 1024


class SignersError(Exception):
    """Why a signers snapshot is not accepted."""


@dataclass(frozen=True)
class AllowedSigner:
    """One snapshot signer, readable as an authority key (``active``, ``key_class``, ``custody``, ``public_key``,
    ``fingerprint``: the attributes ``levain.consent.queue``'s answer check reads off ``authority.key(fp)``)."""
    principal: str
    public_key: str
    fingerprint: str
    key_class: str
    custody: str
    namespaces: frozenset[str]

    @property
    def active(self) -> bool:
        return True               # a removed signer is absent from the snapshot, never listed inactive


@dataclass(frozen=True)
class SignersSnapshot:
    generation: int
    issued_at: str
    signers: Mapping[str, AllowedSigner]    # by fingerprint

    def key(self, fingerprint: Any) -> AllowedSigner | None:
        return self.signers.get(fingerprint) if isinstance(fingerprint, str) else None


def _read_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SignersError(f"{path.name} is not a regular file")
        if st.st_size > _MAX_FILE:
            raise SignersError(f"{path.name} is over {_MAX_FILE} bytes")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(_MAX_FILE + 1)
    finally:
        os.close(fd)


def _signer(entry: Any) -> AllowedSigner:
    if not isinstance(entry, dict) or set(entry) != _SIGNER_KEYS:
        raise SignersError("a signer is not a {principal, key_type, key_b64, key_class, custody, namespaces} record")
    principal, key_type, key_b64, spaces = (entry[k] for k in ("principal", "key_type", "key_b64", "namespaces"))
    key_class, custody = entry["key_class"], entry["custody"]
    if not isinstance(key_class, str) or key_class not in _CLASS_RANK:
        raise SignersError(f"a signer's key_class {str(key_class)[:40]!r} is not one of {', '.join(_CLASS_RANK)}")
    if not all(isinstance(v, str) and v and v.isprintable() and " " not in v for v in (principal, key_type, key_b64)):
        raise SignersError("a signer's principal, key_type and key_b64 are non-empty words")
    if not isinstance(spaces, list) or not spaces or not all(isinstance(s, str) and s for s in spaces):
        raise SignersError("a signer's namespaces are a non-empty list of names")
    public_key = f"{key_type} {key_b64}"
    try:
        fp = fingerprint_line(public_key)
    except ContractError as exc:
        raise SignersError(f"a signer's key does not parse: {exc}") from None
    # the authority's enrol rule for a declared custody (a snapshot entry is never a carried legacy key)
    problem = custody_problem(key_class, custody, key_type)
    if problem is not None:
        raise SignersError(f"a signer's custody: {problem}")
    return AllowedSigner(principal=principal, public_key=public_key, fingerprint=fp, key_class=key_class,
                         custody=custody, namespaces=frozenset(spaces))


def load(policy_dir: Path, trust_pub: Path, *, ssh_keygen: Path | str | None = None) -> SignersSnapshot:
    """Read and verify ``policy_dir/allowed_signers.json`` + ``.sig`` against ``trust_pub``; raise
    :class:`SignersError` (or :class:`levain.authority.VerifierUnavailable`) for anything but a verified snapshot."""
    try:
        body = _read_regular(policy_dir / "allowed_signers.json")
        sig = _read_regular(policy_dir / "allowed_signers.json.sig").decode("ascii")
        trust_fields = _read_regular(trust_pub).decode("ascii").split()
    except (OSError, UnicodeError) as exc:
        raise SignersError(f"the signers snapshot cannot be read ({type(exc).__name__})") from None
    if len(trust_fields) < 2:
        raise SignersError("trust.pub is not a public key line")
    trust_key = f"{trust_fields[0]} {trust_fields[1]}"
    try:
        trust_fp = fingerprint_line(trust_key)
    except ContractError as exc:
        raise SignersError(f"trust.pub does not parse: {exc}") from None
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    if not verify_link_sig(body, sig, public_key=trust_key, fingerprint=trust_fp, namespace=SIGNERS_NAMESPACE,
                           **kwargs):
        raise SignersError("the signers snapshot's signature does not verify by the trust root")
    try:
        record = parse(body)
    except ContractError as exc:
        raise SignersError(f"the signers snapshot does not parse: {exc}") from None
    if not isinstance(record, dict) or set(record) != _KEYS or canonical_bytes(record) != body:
        raise SignersError("the signers snapshot is not a canonical levain-allowed-signers/1 record")
    if record["format"] != SIGNERS_FORMAT:
        raise SignersError(f"signers format {record['format']!r} is not {SIGNERS_FORMAT!r}")
    generation, issued_at = record["generation"], record["issued_at"]
    if type(generation) is not int or generation < 1 or not isinstance(issued_at, str) or not issued_at:
        raise SignersError("the signers snapshot's generation is not a positive integer or issued_at is missing")
    if not isinstance(record["signers"], list):
        raise SignersError("the signers snapshot's signers are not a list")
    signers: dict[str, AllowedSigner] = {}
    for entry in record["signers"]:
        s = _signer(entry)
        if s.fingerprint in signers:
            raise SignersError(f"signer {s.fingerprint} is listed twice")
        signers[s.fingerprint] = s
    return SignersSnapshot(generation=generation, issued_at=issued_at, signers=signers)


def _identity(paths: tuple[Path, ...]) -> tuple[Any, ...]:
    out: list[Any] = []
    for p in paths:
        try:
            st = os.lstat(p)
        except OSError:
            out.append(None)
            continue
        out.append((st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
    return tuple(out)


class SignersSource:
    """The broker's view of the snapshot: :meth:`current` is the verified snapshot or ``None``."""

    def __init__(self, policy_dir: Path | str, trust_pub: Path | str, *, ssh_keygen: Path | str | None = None
                 ) -> None:
        self._policy = Path(policy_dir)
        self._trust = Path(trust_pub)
        self._paths = (self._policy / "allowed_signers.json", self._policy / "allowed_signers.json.sig", self._trust)
        self._ssh_keygen = ssh_keygen
        self._mutex = threading.Lock()
        self._seen: tuple[Any, ...] | None = None
        self._snapshot: SignersSnapshot | None = None
        self._accepted: SignersSnapshot | None = None   # the last snapshot accepted, kept past a refusal
        self.problem: str | None = "not loaded"

    def _reload(self) -> bool:
        """Re-read the pair; ``False`` when the verifier could not run, so nothing was decided about these files."""
        try:
            snap = load(self._policy, self._trust, ssh_keygen=self._ssh_keygen)
        except VerifierUnavailable as exc:
            self._snapshot, self.problem = None, str(exc)
            return False
        except SignersError as exc:
            self._snapshot, self.problem = None, str(exc)
            return True
        last = self._accepted
        if last is not None and snap != last and snap.generation <= last.generation:
            self._snapshot = None
            self.problem = (f"signers generation {snap.generation} is not above the accepted generation "
                            f"{last.generation}")
            return True
        self._snapshot = self._accepted = snap
        self.problem = None
        return True

    def current(self) -> SignersSnapshot | None:
        with self._mutex:
            seen = _identity(self._paths)
            if seen != self._seen:
                # files the verifier could not judge are not recorded as seen: the next call tries them again
                self._seen = seen if self._reload() else None
            return self._snapshot
