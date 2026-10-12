"""levain.cockpit.keys: enrolling, revoking and listing the cockpit's signing keys in a REPORTED install.

Who may sign is a verified replay of the signed authority log (``levain.authority``, trust-tiering
DESIGN §3), never a table a writer can edit: an enrol or a revoke is a LINK, signed by a key the log
already holds (an enrol by a laptop-presence key; a revoke by a key of at least the revoked key's class),
judged by the replay at its predecessor. The first link (the genesis) is ``levain cockpit ledger init``'s,
so an empty log refuses here, naming it.

A governed install's keys are written only by slice G's root helper, so every act here refuses a data
root a governed marker names, or a journal started under any class but ``reported``.

The broker generates its signers file from the snapshot, one line per unrevoked key:
``<principal> namespaces="levain-cockpit" <public_key>``. So ``public_key`` is held as exactly
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.authority import (NAMESPACE, AuthorityError, AuthorityKey, AuthoritySnapshot, VerifierUnavailable,
                              _pipe_reading, anchor_path, sign_and_append)
from levain.autonomic.confirm import _VERIFY_ENV, DEFAULT_SSH_KEYGEN
from levain.cockpit.authority import RequestAuthority
from levain.cockpit.firejournal import _CLASS_RANK, DB_NAME, CockpitDB, _meta
from levain.cockpit.install import REPORTED, marker_path, marker_present, marker_unreadable

__all__ = ["KEY_CLASSES", "KeyError_", "KeyRow", "Signer", "enroll", "keys", "fingerprint_of",
           "normalise_public_key", "principal_of", "revoke", "ssh_signer"]

KEY_CLASSES = tuple(sorted(_CLASS_RANK, key=_CLASS_RANK.__getitem__))
# the key types ssh-keygen -Y signs with (sk- = FIDO / Secure Enclave via a security key provider)
_KEY_TYPES = frozenset({"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                        "ssh-rsa", "sk-ssh-ed25519@openssh.com",
                        "sk-ecdsa-sha2-nistp256@openssh.com"})
_FP = re.compile(r"\d+ (SHA256:[A-Za-z0-9+/]{43}) ")


class KeyError_(Exception):      # noqa: N801 - not the builtin
    """A refused key act; the message is the operator-facing reason."""


@dataclass(frozen=True)
class KeyRow:
    """A key as the authority log's replay holds it: positions are authority seqs."""
    fingerprint: str
    principal: str
    key_class: str
    enrolled_seq: int
    enrolled_at: str | None
    revoked_seq: int | None
    compromised_since: int | None
    legacy_enrolled_at: str | None = None


# what signs a link's message (namespace ``levain-authority``); returns the armored SSHSIG
Signer = Callable[[bytes], str]


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


def fingerprint_of(public_key: str, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, timeout: float = 10.0) -> str:
    """The ``SHA256:...`` fingerprint ``ssh-keygen -Y verify`` prints for this key (the form the
    broker looks up). The key reaches ssh-keygen through a pipe (``/dev/fd/N``), never a file, so no
    same-uid process can swap it between write and read. Raises :class:`VerifierUnavailable` when
    ssh-keygen cannot run or does not answer (never a verdict on the key), ``KeyError_`` when it reads
    the key and refuses it."""
    try:
        data = (public_key + "\n").encode("ascii")
    except UnicodeError:
        raise KeyError_("the public key is not ASCII") from None
    fd: int | None = None
    try:
        fd = _pipe_reading(data)
        out = subprocess.run([str(ssh_keygen), "-l", "-E", "sha256", "-f", f"/dev/fd/{fd}"],
                             capture_output=True, text=True, timeout=timeout, check=False, pass_fds=(fd,),
                             env=dict(_VERIFY_ENV))   # the verifier's fixed environment, so its parse matches
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # RuntimeError: the pipe's writer thread could not start (_pipe_reading has closed that pipe)
        raise VerifierUnavailable(f"ssh-keygen could not fingerprint the key ({type(exc).__name__})") from None
    finally:
        if fd is not None:
            os.close(fd)
    m = _FP.match(out.stdout)
    if out.returncode != 0 or m is None:
        why = (out.stderr or out.stdout).replace(f"/dev/fd/{fd}", "<key>").strip()[:200]
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
    return CockpitDB(root)


def _check_reported(conn: object) -> None:
    """Refuse a journal not started as ``reported`` (its class is set once, insert-or-ignore)."""
    cls = _meta(conn, "install_class")  # type: ignore[arg-type]
    if cls != REPORTED:
        raise KeyError_(f"this journal's install class is {cls!r}; enrolling here is for a reported install")


def _row(k: AuthorityKey) -> KeyRow:
    return KeyRow(k.fingerprint, k.principal, k.key_class, k.enrolled_seq, k.enrolled_at, k.revoked_seq,
                  k.compromised_since, k.legacy_enrolled_at)


def _capture(db: CockpitDB, now: datetime, ssh_keygen: Path | str) -> RequestAuthority:
    with db.read() as conn:
        _check_reported(conn)
        return RequestAuthority.capture(conn, now=now, anchor=anchor_path(db.data_root), source=str(db.path),
                                        ssh_keygen=ssh_keygen)


def _signing_authority(auth: RequestAuthority) -> AuthoritySnapshot:
    """The snapshot a new link is judged against, or the refusal naming why there is none."""
    if auth.authority is None or auth.authority_error is not None:
        raise KeyError_(str(auth.authority_error))
    return auth.authority


def _append(db: CockpitDB, op: Mapping[str, Any], *, signer_fp: str, sign: Signer,
            clock: Callable[[], datetime], ssh_keygen: Path | str) -> AuthoritySnapshot:
    """One signed link, appended under the replay's rules (``levain.authority.sign_and_append``): the
    signature is made outside any transaction, so a touch never holds the journal's write lock."""
    try:
        with db.autocommit() as conn:
            return sign_and_append(conn, op, signer_fp=signer_fp, sign=sign, clock=clock,
                                   anchor=anchor_path(db.data_root), ssh_keygen=ssh_keygen)
    except VerifierUnavailable:
        raise
    except AuthorityError as exc:
        raise KeyError_(str(exc)) from None


def enroll(data_root: Path | str, public_key_text: str, key_class: str, *, signer_fp: str, sign: Signer,
           clock: Callable[[], datetime], ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
           marker_directory: Path | None = None) -> tuple[KeyRow, bool, str | None]:
    """Enrol one key by a link ``signer_fp`` signs (a laptop-presence key). Returns ``(row, created,
    warning)``: ``created`` is False when this exact key is already enrolled, unrevoked, with this class
    (nothing is signed); ``warning`` says the link committed but its anchor did not advance."""
    if key_class not in _CLASS_RANK:
        raise KeyError_(f"unknown key class {key_class!r}; one of {', '.join(KEY_CLASSES)}")
    public_key = normalise_public_key(public_key_text)
    fp = fingerprint_of(public_key, ssh_keygen=ssh_keygen)
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    old = state.key(fp)
    if old is not None:
        if not old.active:
            raise KeyError_(f"{fp} was revoked at seq {old.revoked_seq}; a revoked key stays revoked")
        if old.key_class != key_class:
            raise KeyError_(f"{fp} is enrolled as {old.key_class!r}; a key keeps its class (enroll a new "
                            "key for another class)")
        return _row(old), False, None
    new = _append(db, {"op": "enrol", "fingerprint": fp, "public_key": public_key, "class": key_class},
                  signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
    key = new.key(fp)
    assert key is not None
    return _row(key), True, new.anchor_warning


def revoke(data_root: Path | str, ref: str, *, signer_fp: str, sign: Signer, clock: Callable[[], datetime],
           compromised_since: int | None = None, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
           marker_directory: Path | None = None) -> tuple[KeyRow, str | None]:
    """Revoke the key ``ref`` names (its fingerprint or principal) by a link ``signer_fp`` signs (a key
    of at least its class). The replay refuses a plain revoke of the last unrevoked laptop-presence
    key (it would leave nothing to sign a T3); with ``compromised_since`` (an authority seq, which
    ``levain cockpit keys`` lists) it lands, and that key's fires decided at or after that seq read
    "signed by a revoked key". Returns ``(row, warning)`` as :func:`enroll`."""
    if compromised_since is not None and (type(compromised_since) is not int or compromised_since < 1):
        raise KeyError_("--compromised-since is an authority seq (an integer >= 1)")
    db = _journal(data_root, marker_directory=marker_directory)
    state = _signing_authority(_capture(db, _now(clock), ssh_keygen))
    target = next((k for k in state.keys.values() if ref in (k.fingerprint, k.principal)), None)
    if target is None:
        raise KeyError_(f"no enrolled key {ref!r}")
    if not target.active:
        raise KeyError_(f"{target.fingerprint} is already revoked (at seq {target.revoked_seq})")
    op: dict[str, Any] = {"op": "revoke_key", "fingerprint": target.fingerprint}
    if compromised_since is not None:
        op["compromised_since"] = compromised_since
    new = _append(db, op, signer_fp=signer_fp, sign=sign, clock=clock, ssh_keygen=ssh_keygen)
    key = new.key(target.fingerprint)
    assert key is not None
    return _row(key), new.anchor_warning


def keys(data_root: Path | str, *, now: datetime, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
         marker_directory: Path | None = None) -> tuple[list[KeyRow], str | None]:
    """Every key the authority log's replay holds, revoked ones included, in enrol order, and the
    reason nothing may sign right now (None when keys may sign)."""
    db = _journal(data_root, marker_directory=marker_directory)
    auth = _capture(db, now, ssh_keygen)
    if auth.authority is None:
        if not auth.log_empty:
            raise KeyError_(str(auth.authority_error))
        return [], auth.authority_error
    rows = sorted(auth.authority.keys.values(), key=lambda k: k.enrolled_seq)
    return [_row(k) for k in rows], auth.authority_error


def _now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise KeyError_("the clock must give an aware time")
    return now


_SIGN_TIMEOUT = 120.0          # a touch-required key waits on a person
_PUBLIC_KEY_MAX = 16 * 1024


def ssh_signer(key_path: Path, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> tuple[str, Signer]:
    """``(fingerprint, sign)`` for the signing key at ``key_path``: a private key (or an agent-held
    key's public half) that ``ssh-keygen -Y sign -f`` accepts, its public half read from ``<path>.pub``
    (or ``key_path`` itself when it is the ``.pub``). ``sign`` runs ``ssh-keygen -Y sign`` in the
    authority namespace with the terminal's stderr, so a touch-required key's prompt reaches the
    operator."""
    key_path = Path(key_path).expanduser()
    pub = key_path if key_path.suffix == ".pub" else key_path.with_name(key_path.name + ".pub")
    try:
        with pub.open("rb") as fh:
            raw = fh.read(_PUBLIC_KEY_MAX + 1)
    except OSError as exc:
        raise KeyError_(f"cannot read the signing key's public half {pub}: {exc.strerror or exc}") from None
    if len(raw) > _PUBLIC_KEY_MAX:
        raise KeyError_(f"{pub} is larger than a public key file")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise KeyError_(f"{pub} is not a public key file") from None
    fp = fingerprint_of(normalise_public_key(text), ssh_keygen=ssh_keygen)

    def sign(message: bytes) -> str:
        from levain.launch import child_env
        try:
            # the launch allowlist (it carries SSH_AUTH_SOCK, for an agent-held key) and a configured
            # FIDO provider; stderr stays the terminal's, for the touch prompt
            cp = subprocess.run([str(ssh_keygen), "-Y", "sign", "-f", str(key_path), "-n", NAMESPACE],
                                input=message, stdout=subprocess.PIPE, timeout=_SIGN_TIMEOUT, check=False,
                                env=child_env("SSH_SK_PROVIDER"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyError_(f"ssh-keygen could not sign ({type(exc).__name__})") from None
        if cp.returncode != 0:
            raise KeyError_(f"ssh-keygen could not sign with {key_path} (exit {cp.returncode})")
        try:
            return cp.stdout.decode("ascii")
        except UnicodeDecodeError:
            raise KeyError_("ssh-keygen's signature is not ASCII") from None

    return fp, sign
