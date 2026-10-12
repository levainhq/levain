"""Test-only helpers for the cockpit's signed authority log (trust-tiering DESIGN §3).

Two rigs:

- :func:`fake_authority` for ``CockpitDB`` unit tests that call ``decide`` directly: it inserts a
  stand-in head row and returns a snapshot whose head is that row. ``decide`` trusts the snapshot it is
  handed (capture verified it) and checks only that the log's head has not moved, so no signature is
  needed. A capture of such a log is refused, which those tests never take.
- :class:`RealKey` + :func:`link` for broker, label and CLI tests: real ssh-keygen keys, real signed
  links appended through ``levain.authority.sign_and_append``. Every enrol states its custody (rev 6):
  a software ed25519 laptop-presence key declares ``passphrase`` (the replay holds the declaration to the
  key type; it never sees the file), every other class ``unstated``.
"""
from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import levain.authority as auth
from levain.authority import AuthorityKey, AuthoritySnapshot, anchor_path, genesis_policy, sign_and_append
from levain.cockpit.firejournal import CockpitDB, authority_fence

STORE = "teststore0001"


def fake_authority(db: CockpitDB, keys: Mapping[str, str], *, revoked: tuple[str, ...] = (),
                   compromised: Mapping[str, int] | None = None) -> AuthoritySnapshot:
    """A new stand-in head row in ``db``'s log, and a snapshot at it holding ``keys``
    (fingerprint -> class); ``revoked`` keys are revoked at that seq."""
    compromised = compromised or {}
    with db.write() as conn:
        row = conn.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (1, "") if row is None else (row[0] + 1, row[1])
        head = hashlib.sha256(f"{prev}:{seq}".encode()).hexdigest()
        conn.execute("INSERT INTO authority_log (seq, prev_hash, record, signer_fp, signature, hash) "
                     "VALUES (?, ?, '{}', 'SHA256:stand-in', 'stand-in', ?)", (seq, prev, head))
        hashes = tuple(r[0] for r in conn.execute("SELECT hash FROM authority_log ORDER BY seq"))
    held = {fp: AuthorityKey(fp, "ssh-ed25519 AAAA", cls, 1, revoked_seq=seq if fp in revoked else None,
                             compromised_since=compromised.get(fp), enrolled_at="2026-10-09T12:00:00.000000Z")
            for fp, cls in keys.items()}
    return AuthoritySnapshot(STORE, seq, head, MappingProxyType(held), genesis_policy(), hashes)


def fence(authority: AuthoritySnapshot | None) -> dict:
    return authority_fence(authority)


class RealKey:
    """An ed25519 key made by ssh-keygen in ``d``."""

    def __init__(self, d: Path, name: str) -> None:
        self.path = d / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        self.public = " ".join((d / f"{name}.pub").read_text().split()[:2])
        out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(d / f"{name}.pub")], check=True,
                             capture_output=True, text=True, timeout=30).stdout
        self.fingerprint = out.split()[1]
        self.principal = "k" + hashlib.sha256(self.fingerprint.encode()).hexdigest()[:32]

    def sign(self, message: bytes, namespace: str = "levain-cockpit") -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, timeout=30, check=True).stdout.decode()

    def sign_link(self, message: bytes) -> str:
        return self.sign(message, auth.NAMESPACE)


def link(db: CockpitDB, op: dict, signer: RealKey, *, now: datetime | None = None) -> AuthoritySnapshot:
    """One signed link appended to ``db``'s log, its anchor advanced."""
    when = now or datetime.now(timezone.utc)
    with db.autocommit() as conn:
        return sign_and_append(conn, op, signer_fp=signer.fingerprint, sign=signer.sign_link, clock=lambda: when,
                               anchor=anchor_path(db.data_root))


def default_custody(cls: str) -> str:
    return "passphrase" if cls == "laptop-presence" else "unstated"


def enrol(db: CockpitDB, key: RealKey, cls: str, *, by: RealKey | None = None,
          now: datetime | None = None, custody: str | None = None) -> AuthoritySnapshot:
    """Enrol ``key`` as ``cls``, signed by ``by`` (``key`` itself for the genesis link)."""
    return link(db, {"op": "enrol", "fingerprint": key.fingerprint, "public_key": key.public, "class": cls,
                     "custody": custody or default_custody(cls)}, by or key, now=now)


def revoke(db: CockpitDB, key: RealKey, *, by: RealKey, compromised_since: int | None = None) -> AuthoritySnapshot:
    op = {"op": "revoke_key", "fingerprint": key.fingerprint}
    if compromised_since is not None:
        op["compromised_since"] = compromised_since
    return link(db, op, by)


_ROOTS: dict[str, RealKey] = {}


def root_of(db: CockpitDB) -> RealKey | None:
    return _ROOTS.get(str(db.path))


def enrol_any(db: CockpitDB, key: RealKey, cls: str) -> AuthoritySnapshot:
    """Enrol ``key`` as ``cls`` however the log stands: the genesis when ``key`` is the first
    laptop-presence key, else signed by the log's root (a laptop-presence key made, and made the
    genesis, beside ``key`` the first time one is needed)."""
    root = _ROOTS.get(str(db.path))
    if root is None:
        if cls == "laptop-presence":
            _ROOTS[str(db.path)] = key
            return enrol(db, key, cls)
        root = _ROOTS[str(db.path)] = RealKey(key.path.parent, f"root-{key.path.name}")
        enrol(db, root, "laptop-presence")
    return enrol(db, key, cls, by=root)


def revoke_any(db: CockpitDB, key: RealKey, *, compromised_since: int | None = None) -> AuthoritySnapshot:
    """Revoke ``key``, signed by the log's root."""
    root = _ROOTS[str(db.path)]
    return revoke(db, key, by=root, compromised_since=compromised_since)
