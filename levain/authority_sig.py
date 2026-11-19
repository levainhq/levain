"""levain.authority_sig: the SSHSIG link-signature verifier (:func:`verify_link_sig`) and the SSHSIG reading it rests on.

Split out of ``levain.authority`` so a process that only verifies a signature (root's route-map check in CONTROL and
keys, ``levain.slot.routemaps``) imports this and the trust-anchor rule (``levain.trust_anchor``), never the
authority store, its replay or ``levain.autonomic``. ``levain.authority`` re-exports the names its own calls use, and
those calls go through its own module attribute, so a patch of ``levain.authority.verify_link_sig`` reaches them.
Stdlib only besides ``levain.trust_anchor``, and 3.9-valid: root's interpreter runs it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from levain.trust_anchor import _VERIFY_ENV, DEFAULT_SSH_KEYGEN, trust_anchor_problem

__all__ = ["NAMESPACE", "AuthorityError", "VerifierUnavailable", "presence_missing", "public_key_fingerprint",
           "sshsig_public_key", "verify_link_sig"]

# never the cockpit's or the confirm gate's namespace: a link signature verifies as nothing else
NAMESPACE = "levain-authority"
_MAX_SIGNATURE = 16384


class AuthorityError(Exception):
    """A refused authority act or an unusable log; the message is operator-facing."""


class VerifierUnavailable(AuthorityError):
    """The signature verifier (ssh-keygen) could not run. Never a verdict on the log: a replay raises
    this unconverted, so a caller can tell "retry" from "the log is refused"."""


# ---------------------------------------------------------------------------------------------------
# signatures


def _signers_line(principal: str, namespace: str, public_key: str) -> str:
    # no options: in particular never ``no-touch-required``, so an sk key's user-presence flag is required
    return f'{principal} namespaces="{namespace}" {public_key}\n'


_SSHSIG_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_SSHSIG_END = "-----END SSH SIGNATURE-----"
_SK_USER_PRESENT = 0x01
_WEBAUTHN_SIG_TYPE = "webauthn-sk-ecdsa-sha2-nistp256@openssh.com"
# a key type :func:`sshsig_public_key` will name: it is written into an allowed-signers line, so a space, a newline
# or a quote in it would add options or a second line (``no-touch-required``) to what ssh-keygen trusts
_KEY_TYPE_RE = re.compile(r"[A-Za-z0-9@._-]+")


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


def _sshsig_fields(armored: str) -> tuple[bytes, bytes]:
    """The ``publickey`` and ``signature`` strings of an armored SSHSIG v1 blob (the layout
    :func:`_sshsig_type_and_flags` describes); namespace, reserved and hash_algorithm are skipped, for
    ssh-keygen to judge. Raises ValueError on anything malformed."""
    text = armored.strip()
    if not text.startswith(_SSHSIG_BEGIN) or not text.endswith(_SSHSIG_END):
        raise ValueError("not an armored SSH signature")
    blob = base64.b64decode("".join(text[len(_SSHSIG_BEGIN):-len(_SSHSIG_END)].split()), validate=True)
    outer = _Wire(blob)
    if outer.take(6) != b"SSHSIG" or outer.u32() != 1:
        raise ValueError("not an SSHSIG v1 blob")
    public_key = outer.string()
    for _ in range(3):              # namespace, reserved, hash_algorithm
        outer.string()
    signature = outer.string()
    outer.end()
    return public_key, signature


def sshsig_public_key(armored: Any) -> tuple[str, str, str]:
    """What an armored SSHSIG CLAIMS about its signer: the public key embedded in it as ``<type> <base64>``, that
    key's fingerprint in the ``SHA256:...`` form ssh-keygen prints, and the signature's algorithm. Nothing here
    verifies the signature: the key is only the one the blob names, for :func:`verify_link_sig` to verify against
    (it is never authority on its own). Raises ValueError on anything malformed, longer than any signature
    levain verifies, or naming a key type outside ``[A-Za-z0-9@._-]+``."""
    if not isinstance(armored, str) or len(armored) > _MAX_SIGNATURE:
        raise ValueError("not a signature levain reads")
    try:
        key_blob, sig_blob = _sshsig_fields(armored)
        key_type = _Wire(key_blob).string().decode("ascii")
        sig_type = _Wire(sig_blob).string().decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"not an SSH signature ({type(exc).__name__})") from None
    if not key_type or not sig_type:
        raise ValueError("the signature names no key type or no algorithm")
    if not _KEY_TYPE_RE.fullmatch(key_type):
        raise ValueError("the signature's key type is not one ssh-keygen names")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
    return f"{key_type} {base64.b64encode(key_blob).decode('ascii')}", fingerprint, sig_type


def _sshsig_type_and_flags(armored: str) -> tuple[str, int | None]:
    """:func:`_sshsig_type_flags_counter` without the counter: the algorithm and the FIDO flags byte (None for a
    non-sk algorithm). Raises ValueError on anything malformed."""
    sig_type, flags, _counter = _sshsig_type_flags_counter(armored)
    return sig_type, flags


def _sshsig_type_flags_counter(armored: str) -> tuple[str, int | None, int | None]:
    """The signature algorithm inside an armored SSHSIG, and its FIDO flags byte and counter (None for a non-sk
    algorithm). Raises ValueError on anything malformed.

    The SSHSIG blob (OpenSSH PROTOCOL.sshsig §"Blob format"): byte[6] "SSHSIG", uint32 version (1),
    string publickey, string namespace, string reserved, string hash_algorithm, string signature.
    For an sk-* algorithm the ``signature`` string is (OpenSSH PROTOCOL.u2f §"SSH U2F signatures"):
    string sig-type, string inner signature, byte flags, uint32 counter; flags bit 0x01 is user
    presence. A WebAuthn signature (``webauthn-sk-ecdsa-sha2-nistp256@openssh.com``, the same section: a
    passkey's assertion, Q6b) carries string origin, string clientData, string extensions after the
    counter. Every other algorithm is string sig-type, string signature."""
    _public_key_blob, sig_blob = _sshsig_fields(armored)
    inner = _Wire(sig_blob)
    sig_type = inner.string().decode("ascii")
    inner.string()
    webauthn = sig_type == _WEBAUTHN_SIG_TYPE
    if not (sig_type.startswith("sk-") or webauthn):
        inner.end()
        return sig_type, None, None
    flags = inner.take(1)[0]
    counter = inner.u32()
    if webauthn:
        for _ in range(3):          # origin, clientData, extensions: ssh-keygen checks them against the message
            inner.string()
    inner.end()
    return sig_type, flags, counter


def sk_ecdsa_signature_empty(armored: str) -> bool:
    """True when ``armored`` is an sk-ecdsa SSHSIG whose ECDSA signature is EMPTY: both integers, r and s, have zero
    length (an 8-byte blob, two length words and nothing after them), though the flags byte and counter that follow
    look like a good signature's. This is what the Touch ID provider returned on 2026-11-19 (levain#153; 13 recorded
    failures): the flags were not the discriminator, the blob is. False for anything else, a signature that is
    non-empty and fails to verify included, and for anything malformed or not ending exactly after the flags and
    counter (the caller's own parse names that)."""
    try:
        # the strict parse first: the whole signature string must be well formed and end exactly after the flags byte
        # and counter (the one parser the callers use), so trailing bytes or a short tail are never called empty
        sig_type, flags, _counter = _sshsig_type_flags_counter(armored)
        if not sig_type.startswith("sk-ecdsa-") or flags is None:
            return False
        _public_key_blob, sig_blob = _sshsig_fields(armored)
        inner = _Wire(sig_blob)
        inner.string()
        ecdsa = _Wire(inner.string())
        r, s = ecdsa.string(), ecdsa.string()
        ecdsa.end()
    except (ValueError, UnicodeDecodeError):
        return False
    return len(r) == 0 and len(s) == 0


def presence_missing(signature: str, public_key: str) -> bool:
    """True if a FIDO (sk-*) key or signature lacks the user-presence flag, or ``signature`` cannot be
    read for one: the one presence rule for every levain signature check (authority links here, cockpit
    acts in the broker), so a key made ``no-touch-required`` never signs without a touch."""
    try:
        sig_type, flags = _sshsig_type_and_flags(signature)
    except (ValueError, UnicodeError):
        return True
    key_type = public_key.split()[0] if isinstance(public_key, str) and public_key.split() else ""
    return (key_type.startswith("sk-") or sig_type.startswith("sk-") or sig_type == _WEBAUTHN_SIG_TYPE) \
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


_KEYGEN_FINGERPRINT = re.compile(r"[0-9]+ (SHA256:[A-Za-z0-9+/]{43}) .*")


def public_key_fingerprint(public_key: Any, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
                           timeout: float = 10.0) -> str | None:
    """The ``SHA256:...`` fingerprint ssh-keygen reads for one public key line (``<type> <base64> [comment]``): the
    reading :func:`verify_link_sig` compares a pinned fingerprint against, from the same verifier binary and
    environment. ``None`` when ssh-keygen does not read the line as exactly one public key (a truncated or malformed blob among
    them). The line reaches ssh-keygen on its stdin, never a file. Raises :class:`VerifierUnavailable` when the
    verifier cannot run (that is never a verdict on the key)."""
    if not isinstance(public_key, str) or len(public_key) > _MAX_SIGNATURE:
        return None
    line = public_key.strip()
    if not line or "\n" in line or "\r" in line:
        return None
    try:
        data = (line + "\n").encode("ascii")
    except UnicodeError:
        return None
    problem, keygen = trust_anchor_problem(ssh_keygen, what="ssh-keygen")
    if problem is not None:
        raise VerifierUnavailable(f"cannot read a public key's fingerprint: {problem}")
    try:
        cp = subprocess.run([str(keygen), "-l", "-E", "sha256", "-f", "-"], input=data, capture_output=True,
                            timeout=timeout, env=dict(_VERIFY_ENV))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerifierUnavailable(f"ssh-keygen could not read a public key ({type(exc).__name__})") from None
    if cp.returncode != 0:
        return None
    out = [ln for ln in (cp.stdout or b"").decode("utf-8", "replace").splitlines() if ln.strip()]
    m = _KEYGEN_FINGERPRINT.fullmatch(out[0].strip()) if len(out) == 1 else None
    return m.group(1) if m else None


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


def verify_failure_detail(message: bytes, signature: str, *, public_key: str, namespace: str = NAMESPACE,
                          ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN, timeout: float = 2.0,
                          left: Callable[[], float] | None = None) -> str:
    """Words for a signature :func:`verify_link_sig` refused, for an error message only (never a verdict; nothing
    reads it back): what ``ssh-keygen -Y verify`` says about it against the key, and whether the signature is
    consistent over ``message`` at all (``-Y check-novalidate``, which takes the key out of the question). Told apart,
    they say whether the signer made a signature over other bytes than these (the second fails) or the key was the
    question (the second holds), the one fact a touched-but-refused sign leaves open (1118-12: a Touch ID sign
    that ssh-keygen made in 0.9 s and the check refused). Never raises: any trouble is said in its place.

    ``left``, when given, is read before each check: the seconds left of the caller's own bound. That check gets at most
    that (never more than ``timeout``) and is not run once nothing is left, so the detail never takes the caller past
    its bound. A check cut short is said as "timed out", never as the exception's type name, so the words cannot read
    as a timed-out sign to a caller that files messages by that name (``terminal_sign._classify``)."""
    try:
        sig_bytes = (signature.strip() + "\n").encode("ascii")
        problem, keygen = trust_anchor_problem(ssh_keygen, what="ssh-keygen")
        if problem is not None:
            return f"detail unavailable ({problem})"
        signers = _signers_line("authority-signer", namespace, public_key).encode("ascii")
    except Exception as exc:  # noqa: BLE001
        return f"detail unavailable ({type(exc).__name__})"

    def first(cp: "subprocess.CompletedProcess[bytes]") -> str:
        text = ((cp.stderr or b"") + b"\n" + (cp.stdout or b"")).decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return (lines[0][:160] if lines else "no output") + f" (exit {cp.returncode})"

    out: list[str] = []
    # each check names the pipes it reads: verify the signers line and the signature, check-novalidate the signature only
    # (an unread pipe passed to the child would be a pipe nothing drains). A short bound each (the sign has already
    # finished: this is error text, and must not hold the refusal back).
    for label, readers, argv in (
            ("verify", (signers, sig_bytes), lambda fds: [str(keygen), "-Y", "verify", "-f", f"/dev/fd/{fds[0]}", "-I",
                                                         "authority-signer", "-n", namespace, "-s", f"/dev/fd/{fds[1]}"]),
            ("check-novalidate", (sig_bytes,), lambda fds: [str(keygen), "-Y", "check-novalidate", "-n", namespace,
                                                           "-s", f"/dev/fd/{fds[0]}"])):
        budget = timeout
        if left is not None:
            try:
                budget = min(timeout, left())
            except Exception as exc:  # noqa: BLE001
                out.append(f"{label}: not run (the time left could not be read: {type(exc).__name__})")
                continue
            if budget <= 0:
                out.append(f"{label}: not run (the caller's time bound was spent)")
                continue
        fds: list[int] = []
        try:
            for data in readers:
                fds.append(_pipe_reading(data))
            cp = subprocess.run(argv(fds), input=message, capture_output=True, timeout=budget,
                                env=dict(_VERIFY_ENV), pass_fds=tuple(fds))
            out.append(f"{label}: {first(cp)}")
        except subprocess.TimeoutExpired:
            out.append(f"{label}: unavailable (timed out after {budget:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            out.append(f"{label}: unavailable ({type(exc).__name__})")
        finally:
            for fd in fds:
                os.close(fd)
    return "; ".join(out) + f"; message {len(message)} bytes"
