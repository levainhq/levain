"""levain.cockpit.keys: enrolling, revoking and listing the cockpit's signing keys in a REPORTED install
(K2b design §6: "``levain cockpit enroll`` (operator CLI at the laptop) writes the ``keys`` table directly
with ``enrolled_via: "laptop cli"``"; §13: revoking the last ``laptop-presence`` key is refused).

A governed install's keys are written only by slice G's root helper, so every act here refuses a data
root a governed marker names, or a journal started under any class but ``reported``.

The broker generates its signers file from these rows, one line per unrevoked key:
``<principal> namespaces="levain-cockpit" <public_key>``. So ``public_key`` is stored as exactly
``<type> <base64>``: an option (``cert-authority``, ``valid-before``) or a second key can never ride in
from the enrolled text.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from levain.autonomic.confirm import _VERIFY_ENV, DEFAULT_SSH_KEYGEN
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import _CLASS_RANK, DB_NAME, CockpitDB, _meta
from levain.cockpit.install import REPORTED, marker_path, marker_present, marker_unreadable

__all__ = ["ENROLLED_VIA", "KEY_CLASSES", "KeyError_", "KeyRow", "enroll", "keys", "fingerprint_of",
           "normalise_public_key", "principal_of", "revoke"]

ENROLLED_VIA = "laptop cli"
KEY_CLASSES = tuple(sorted(_CLASS_RANK, key=_CLASS_RANK.__getitem__))
_LAPTOP = "laptop-presence"
# the key types ssh-keygen -Y signs with (sk- = FIDO / Secure Enclave via a security key provider)
_KEY_TYPES = frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                        "ssh-rsa", "sk-ssh-ed25519@openssh.com",
                        "sk-ecdsa-sha2-nistp256@openssh.com"})
_FP = re.compile(r"\d+ (SHA256:[A-Za-z0-9+/]{43}) ")


class KeyError_(Exception):      # noqa: N801 - not the builtin
    """A refused key act; the message is the operator-facing reason."""


@dataclass(frozen=True)
class KeyRow:
    fingerprint: str
    principal: str
    key_class: str
    enrolled_at: str
    enrolled_via: str
    revoked_at: str | None
    compromised_since: str | None


def principal_of(fingerprint: str) -> str:
    """§3 schema: ``"k" + 32 hex of sha256(fingerprint)``, inside confirm's signer charset."""
    return "k" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]


def normalise_public_key(text: str) -> str:
    """``<type> <base64>`` from one OpenSSH public key line (a trailing comment is dropped), or
    ``KeyError_``. Refuses options, several keys, a private key, and a blob whose embedded type is not
    the line's."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) != 1:
        raise KeyError_(f"expected exactly one public key line, found {len(lines)}")
    parts = lines[0].split()
    if parts[0] not in _KEY_TYPES:
        raise KeyError_(f"not a public key line (or it carries options): starts {parts[0][:40]!r}")
    if len(parts) < 2:
        raise KeyError_("the key line has no key data")
    ktype, blob64 = parts[0], parts[1]
    try:
        blob = base64.b64decode(blob64, validate=True)
        (n,) = struct.unpack(">I", blob[:4])
        embedded = blob[4:4 + n].decode("ascii")
    except (binascii.Error, struct.error, UnicodeDecodeError, ValueError):
        raise KeyError_("the key data is not a valid OpenSSH key blob") from None
    if embedded != ktype:
        raise KeyError_(f"the key data is a {embedded!r} key, not {ktype!r}")
    return f"{ktype} {blob64}"


def fingerprint_of(public_key: str, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> str:
    """The ``SHA256:...`` fingerprint ``ssh-keygen -Y verify`` prints for this key (the form the
    broker looks up)."""
    fd, path = tempfile.mkstemp(prefix=".enroll-", suffix=".pub")
    try:
        with open(fd, "w", encoding="ascii", closefd=True) as fh:
            fh.write(public_key + "\n")
        out = subprocess.run([str(ssh_keygen), "-l", "-E", "sha256", "-f", path],
                             capture_output=True, text=True, timeout=10, check=False,
                             env=dict(_VERIFY_ENV))   # the verifier's fixed environment, so its parse matches
    except subprocess.TimeoutExpired:
        raise KeyError_("ssh-keygen did not answer reading the key") from None
    finally:
        os.unlink(path)
    m = _FP.match(out.stdout)
    if out.returncode != 0 or m is None:
        why = (out.stderr or out.stdout).replace(path, "<key>").strip()[:200]
        raise KeyError_(f"ssh-keygen could not read the key: {why}")
    return m.group(1)


def _journal(data_root: Path | str, *, marker_directory: Path | None = None) -> CockpitDB:
    """The reported install's journal at ``data_root``; never creates one."""
    root = Path(data_root)
    marker = marker_path(root, marker_directory)
    try:
        governed = marker_present(marker)
    except OSError as exc:
        raise KeyError_(marker_unreadable(marker, exc)) from exc
    if governed:
        raise KeyError_(f"a governed marker names this data root ({marker}): its keys are the root helper's")
    if not (root / DB_NAME).is_file():
        raise KeyError_(f"no cockpit journal at {root / DB_NAME}: start the cockpit once with "
                        "`levain serve --write --install-class reported`")
    db = CockpitDB(root)
    with db.read() as conn:
        _check_reported(conn)
    return db


def _check_reported(conn: object) -> None:
    """Refuse a journal not started as ``reported``. Called again inside every key write's own
    transaction, so a class change between the first read and the write is seen."""
    cls = _meta(conn, "install_class")  # type: ignore[arg-type]
    if cls != REPORTED:
        raise KeyError_(f"this journal's install class is {cls!r}; enrolling here is for a reported install")


def _aware(when: datetime | None, name: str) -> None:
    if when is not None and (when.tzinfo is None or when.utcoffset() is None):
        raise KeyError_(f"{name} needs a zone")


def _row(r: object) -> KeyRow:
    return KeyRow(r["fingerprint"], r["principal"], r["class"], r["enrolled_at"],  # type: ignore[index]
                  r["enrolled_via"], r["revoked_at"], r["compromised_since"])  # type: ignore[index]


def enroll(data_root: Path | str, public_key_text: str, key_class: str, *, now: datetime,
           ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, marker_directory: Path | None = None) -> tuple[KeyRow, bool]:
    """Enroll one key. Returns ``(row, created)``: ``created`` is False when this exact key is already
    enrolled, unrevoked, with this class (nothing changes)."""
    _aware(now, "now")
    if key_class not in _CLASS_RANK:
        raise KeyError_(f"unknown key class {key_class!r}; one of {', '.join(KEY_CLASSES)}")
    public_key = normalise_public_key(public_key_text)
    fp = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    db = _journal(data_root, marker_directory=marker_directory)
    with db.write() as conn:
        _check_reported(conn)
        old = conn.execute("SELECT * FROM keys WHERE fingerprint = ?", (fp,)).fetchone()
        if old is not None:
            if old["revoked_at"] is not None:
                raise KeyError_(f"{fp} was revoked at {old['revoked_at']}; a revoked key stays revoked")
            if old["class"] != key_class:
                raise KeyError_(f"{fp} is enrolled as {old['class']!r}; a key keeps its class (enroll a new "
                               "key for another class)")
            return _row(old), False
        conn.execute("INSERT INTO keys (fingerprint, principal, public_key, class, enrolled_at, enrolled_via) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (fp, principal_of(fp), public_key, key_class, utc_stamp(now), ENROLLED_VIA))
        row = conn.execute("SELECT * FROM keys WHERE fingerprint = ?", (fp,)).fetchone()
    return _row(row), True


def revoke(data_root: Path | str, ref: str, *, now: datetime, compromised_since: datetime | None = None,
           marker_directory: Path | None = None) -> KeyRow:
    """Revoke the key ``ref`` names (its fingerprint or principal). A plain revoke of the last
    unrevoked ``laptop-presence`` key is refused (§13: it would leave no key that can sign a T3). With
    ``compromised_since`` it lands: a stolen key must stop signing now, and enrolling a new key needs
    only this CLI, so nobody is locked out."""
    _aware(now, "now")
    _aware(compromised_since, "compromised_since")
    if compromised_since is not None and compromised_since > now:
        raise KeyError_("--compromised-since is in the future")
    db = _journal(data_root, marker_directory=marker_directory)
    with db.write() as conn:
        _check_reported(conn)
        row = conn.execute("SELECT * FROM keys WHERE fingerprint = ? OR principal = ?", (ref, ref)).fetchone()
        if row is None:
            raise KeyError_(f"no enrolled key {ref!r}")
        if row["revoked_at"] is not None:
            raise KeyError_(f"{row['fingerprint']} is already revoked (at {row['revoked_at']})")
        if row["class"] == _LAPTOP and compromised_since is None:
            others = conn.execute("SELECT COUNT(*) FROM keys WHERE class = ? AND revoked_at IS NULL "
                                  "AND fingerprint != ?", (_LAPTOP, row["fingerprint"])).fetchone()[0]
            if others == 0:
                raise KeyError_(f"{row['fingerprint']} is the last unrevoked laptop-presence key; enroll "
                                "another first, or pass --compromised-since if it is lost or stolen")
        conn.execute("UPDATE keys SET revoked_at = ?, compromised_since = ? WHERE fingerprint = ?",
                     (utc_stamp(now), utc_stamp(compromised_since) if compromised_since else None,
                      row["fingerprint"]))
        row = conn.execute("SELECT * FROM keys WHERE fingerprint = ?", (row["fingerprint"],)).fetchone()
    return _row(row)


def keys(data_root: Path | str, *, marker_directory: Path | None = None) -> list[KeyRow]:
    db = _journal(data_root, marker_directory=marker_directory)
    with db.read() as conn:
        return [_row(r) for r in conn.execute("SELECT * FROM keys ORDER BY enrolled_at, fingerprint")]
