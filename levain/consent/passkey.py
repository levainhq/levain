"""levain.consent.passkey — a consent answer signed with a WebAuthn passkey, carried as an OpenSSH signature (Q6b).

A passkey cannot sign arbitrary bytes, but it signs ``authenticatorData || SHA-256(clientDataJSON)``, and
clientDataJSON carries the challenge the page passed in. OpenSSH has a signature type for exactly that,
``webauthn-sk-ecdsa-sha2-nistp256@openssh.com`` (OpenSSH PROTOCOL.u2f), which ``ssh-keygen -Y verify`` checks: it
rebuilds the challenge from the message being verified, requires clientDataJSON to begin with
``{"type":"webauthn.get","challenge":"<that challenge>","origin":"<the signature's origin>"``, takes the rpId hash
from the key's application, then checks the ECDSA signature over all of it (flags and counter included). It checks
neither the user-presence flag nor what the origin is: levain's own presence rule
(:func:`levain.authority.presence_missing`) refuses a signature without presence, and :func:`to_sshsig` refuses one
whose origin is not exactly the key's own ``https://<host>`` or that lacks user verification. So:

  - :func:`challenge` is the challenge the page asks the passkey to sign: the whole SSHSIG signed-data blob for the
    answer's message (``"SSHSIG" || string(namespace) || string("") || string("sha512") || string(SHA-512(message))``,
    PROTOCOL.sshsig §3), passed raw;
  - :func:`public_key_line` is the OpenSSH public key of a passkey: its P-256 point and the rpId as the key's
    application. The rpId is part of the key: a passkey made on one https name is a different key on any other;
  - :func:`to_sshsig` wraps one assertion as an armored SSHSIG over that key.

This module decides nothing on its own. A wrong conversion makes ``ssh-keygen`` refuse a good answer; it cannot make
a bad one verify on the message, the rpId, user presence or the signature, which the verifier the consent queue
already uses checks on every delivery (:func:`levain.authority.verify_link_sig`, :func:`levain.authority.presence_missing`).
The checks here refuse the wrong shape early, with a reason, and add three that ``ssh-keygen`` does not make (the
exact origin, user verification, and not cross-origin); the consent queue runs those three again on every
verification of a passkey key's answer (:func:`answer_signature_problem`).

Stdlib only. The conversion was first run by a test page against a real iPhone (Safari, iCloud passkey, 2026-10-16:
``/usr/bin/ssh-keygen`` verified it and a one-bit change to the message did not); this module's output for a recorded
virtual-authenticator assertion is byte-identical to that page's (tests/test_consent_passkey.py verifies it).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
import urllib.parse

from levain.consent.record import ANSWER_NAMESPACE

__all__ = ["KEY_TYPE", "SIG_TYPE", "PasskeyError", "answer_signature_problem", "b64url_decode", "challenge", "is_host_name", "key_application",
           "public_key_line", "to_sshsig"]

KEY_TYPE = "sk-ecdsa-sha2-nistp256@openssh.com"
SIG_TYPE = "webauthn-sk-ecdsa-sha2-nistp256@openssh.com"
_HASH_ALG = "sha512"
# DER SubjectPublicKeyInfo for an uncompressed P-256 point: SEQUENCE { SEQUENCE { id-ecPublicKey, prime256v1 },
# BIT STRING (0 unused bits) 04 || X || Y }. A WebAuthn ES256 credential's getPublicKey() is exactly this.
_P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
_POINT_LEN = 65
_AUTH_DATA_MIN = 37                 # rpIdHash(32) || flags(1) || signCount(4)
_FLAG_UP, _FLAG_UV, _FLAG_AT, _FLAG_ED = 0x01, 0x04, 0x40, 0x80
_FIELD_MAX = 16 * 1024              # any one browser-supplied field; a real one is a few hundred bytes
# an rpId is a host name: lowercase labels, no port, no scheme (WebAuthn: a registrable domain suffix of the origin)
_RP_ID = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


class PasskeyError(ValueError):
    """A passkey value of the wrong shape: refused before anything is signed or verified."""


def _string(data: bytes | str) -> bytes:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return struct.pack(">I", len(raw)) + raw


def _mpint(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    return _string(b"\x00" + raw if raw[0] & 0x80 else raw)


def b64url_decode(text: object, what: str) -> bytes:
    """``text`` decoded as base64url (padding optional); :class:`PasskeyError` on anything else."""
    if not isinstance(text, str) or not text or len(text) > _FIELD_MAX * 2:
        raise PasskeyError(f"{what} is not a base64url string")
    if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", text) is None:
        raise PasskeyError(f"{what} is not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PasskeyError(f"{what} is not base64url") from exc


def challenge(message: bytes, namespace: str = ANSWER_NAMESPACE) -> bytes:
    """The bytes a passkey signs (as its WebAuthn challenge) to sign ``message`` in ``namespace``."""
    return (b"SSHSIG" + _string(namespace) + _string(b"") + _string(_HASH_ALG)
            + _string(hashlib.sha512(message).digest()))


def is_host_name(text: object) -> bool:
    """True for a lowercase DNS host name with no scheme, port or path: what an rpId (and a proxied https name) is."""
    if not isinstance(text, str) or _RP_ID.fullmatch(text) is None:
        return False
    return not text.rsplit(".", 1)[-1].isdigit()           # an IPv4 literal is never an rpId (WebAuthn)


def _rp_id(rp_id: object) -> str:
    if not is_host_name(rp_id):
        raise PasskeyError(f"rpId {str(rp_id)[:80]!r} is not a lowercase host name")
    return rp_id


def _key_blob(point: bytes, rp_id: str) -> bytes:
    return _string(KEY_TYPE) + _string("nistp256") + _string(point) + _string(rp_id)


def public_key_line(spki_der: bytes, rp_id: str) -> str:
    """The OpenSSH public key (``sk-ecdsa-sha2-nistp256@openssh.com <base64>``) of the passkey whose
    ``getPublicKey()`` is ``spki_der``, made on ``rp_id``."""
    if (not isinstance(spki_der, bytes) or len(spki_der) != len(_P256_SPKI_PREFIX) + _POINT_LEN
            or not spki_der.startswith(_P256_SPKI_PREFIX) or spki_der[len(_P256_SPKI_PREFIX)] != 0x04):
        raise PasskeyError("the public key is not an uncompressed P-256 key (a passkey made with ES256, alg -7)")
    point = spki_der[len(_P256_SPKI_PREFIX):]
    return f"{KEY_TYPE} {base64.b64encode(_key_blob(point, _rp_id(rp_id))).decode('ascii')}"


def _key_parts(public_key: str) -> tuple[bytes, str]:
    """``(key blob, application)`` of an ``sk-ecdsa-sha2-nistp256@openssh.com`` public key line."""
    parts = public_key.split() if isinstance(public_key, str) else []
    if len(parts) < 2 or parts[0] != KEY_TYPE:
        raise PasskeyError(f"the enrolled key is not a {KEY_TYPE} key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PasskeyError("the enrolled key is not base64") from exc
    fields, at = [], 0
    while at < len(blob) and len(fields) < 5:
        if at + 4 > len(blob):
            raise PasskeyError("the enrolled key is truncated")
        (size,) = struct.unpack(">I", blob[at:at + 4])
        if at + 4 + size > len(blob):
            raise PasskeyError("the enrolled key is truncated")
        fields.append(blob[at + 4:at + 4 + size])
        at += 4 + size
    if len(fields) != 4 or at != len(blob) or fields[0] != KEY_TYPE.encode() or fields[1] != b"nistp256":
        raise PasskeyError("the enrolled key is not a well-formed sk-ecdsa key")
    try:
        return blob, fields[3].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PasskeyError("the enrolled key's application is not text") from exc


def key_application(public_key: str) -> str:
    """The rpId a passkey's public key line was made on (its OpenSSH application)."""
    return _key_parts(public_key)[1]


def _der_integer(der: bytes, at: int) -> tuple[int, int]:
    if at + 2 > len(der) or der[at] != 0x02:
        raise PasskeyError("the signature is not a DER ECDSA signature")
    size = der[at + 1]
    if size == 0 or size > 33 or at + 2 + size > len(der):
        raise PasskeyError("the signature is not a DER ECDSA signature")
    raw = der[at + 2:at + 2 + size]
    if raw[0] & 0x80 or (size > 1 and raw[0] == 0 and not raw[1] & 0x80):
        raise PasskeyError("the signature is not a DER ECDSA signature")      # negative, or not minimal
    return int.from_bytes(raw, "big"), at + 2 + size


def _ecdsa_signature(der: bytes) -> bytes:
    """A DER ``ECDSA-Sig-Value`` (what WebAuthn returns) as the OpenSSH ``mpint r || mpint s``."""
    if len(der) < 8 or der[0] != 0x30 or der[1] != len(der) - 2:
        raise PasskeyError("the signature is not a DER ECDSA signature")
    r, at = _der_integer(der, 2)
    s, at = _der_integer(der, at)
    if at != len(der) or r == 0 or s == 0:
        raise PasskeyError("the signature is not a DER ECDSA signature")
    return _mpint(r) + _mpint(s)


def _client_origin(client_data_json: bytes, application: str, page_origin: "str | None" = None) -> str:
    """The origin a passkey's clientDataJSON names, once it is a ``webauthn.get`` (not cross-origin) from a page whose
    origin is exactly ``https://<application>``, no port; else :class:`PasskeyError`. Only ``localhost``, which no
    phone reaches and browsers treat as secure, also takes http and a port (a page on this machine's own browser):
    given ``page_origin`` (the phone port's own bound ``http://localhost:<port>``) it must be exactly that, since any
    other program on this machine can serve a page on localhost under another port; without one (a re-verification
    that does not know which port answered) any port passes. The port is pinned because a stock verify never checks
    the origin: another page served on the same host name under another port could otherwise ask the passkey for a
    signature levain accepts. The one rule, used at conversion and at every delivery."""
    try:
        client = json.loads(client_data_json.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PasskeyError("clientDataJSON is not JSON") from exc
    if not isinstance(client, dict) or client.get("type") != "webauthn.get":
        raise PasskeyError("clientDataJSON is not a webauthn.get assertion")
    if client.get("crossOrigin") is True:
        raise PasskeyError("the passkey signed inside a page from another site")
    origin = client.get("origin")
    try:
        parts = urllib.parse.urlsplit(origin) if isinstance(origin, str) else None
    except ValueError:  # an unparseable origin (an unclosed IPv6 bracket) is a refusal, never a crash at delivery
        parts = None
    if application == "localhost" and page_origin is not None:
        ok = origin == page_origin
    elif application == "localhost":
        try:
            port_ok = parts is not None and (parts.port is None or parts.port > 0)
        except ValueError:     # a port that is not a number
            port_ok = False
        ok = (port_ok and parts.scheme in ("https", "http") and parts.hostname == application
              and parts.username is None and parts.password is None
              and not (parts.path or parts.query or parts.fragment))
    else:
        ok = origin == f"https://{application}"
    if not ok:
        if application == "localhost" and page_origin is not None:
            raise PasskeyError(f"the passkey signed on a page that is not {page_origin} (exactly: scheme, name and "
                               "port)")
        raise PasskeyError(f"the passkey signed on a page that is not https://{application} (exactly: no port)")
    return origin


def answer_signature_problem(signature: str, public_key: str, page_origin: "str | None" = None) -> str | None:
    """Why ``signature`` is not a consent answer a passkey ``public_key`` may give, or None. Run by the consent
    queue on every verification of a passkey key's answer (on answer and at each delivery), beside ssh-keygen:
    the signature must be a WebAuthn assertion (never a plain sk signature), carry user verification (Face ID,
    Touch ID or the device passcode, as the authenticator reports it), and come from a page whose origin is exactly
    the key's own ``https://<host>`` (:func:`_client_origin`, which ``page_origin`` pins on ``localhost``). Among
    levain's own verifiers, the consent queue accepts a passkey's signature only in its answer and ruling namespaces
    (it runs this check for both), and the authority replay and the broker refuse it for anything else. The phone
    port's sign requests (:mod:`levain.consent.sign`) also run this check, for a caller outside levain, in
    namespaces levain never verifies."""
    try:
        _key_blob, application = _key_parts(public_key)
        text = signature.strip()
        head, tail = "-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----"
        if not (text.startswith(head) and text.endswith(tail)):
            return "the answer is not an armored SSH signature"
        blob = base64.b64decode("".join(text[len(head):-len(tail)].split()), validate=True)
        at = [0]

        def take(n: int) -> bytes:
            if at[0] + n > len(blob):
                raise ValueError("short")
            out = blob[at[0]:at[0] + n]
            at[0] += n
            return out

        def string() -> bytes:
            (n,) = struct.unpack(">I", take(4))
            return take(n)

        if take(6) != b"SSHSIG":
            return "the answer is not an SSHSIG"
        take(4)
        for _ in range(4):             # publickey, namespace, reserved, hash algorithm
            string()
        inner = string()
        if at[0] != len(blob):
            return "the answer's signature has trailing bytes"
        blob, at[0] = inner, 0
        if string() != SIG_TYPE.encode("ascii"):
            return "a passkey answer must be a WebAuthn signature"
        string()                       # the ECDSA signature: ssh-keygen checks it
        flags = take(1)[0]
        take(4)
        origin = string().decode("utf-8")
        client_data_json = string()
        string()                       # extensions
        if at[0] != len(blob):
            return "the WebAuthn signature has trailing bytes"
    except (ValueError, struct.error, UnicodeError, binascii.Error, PasskeyError):
        return "the answer's passkey signature cannot be read"
    if not flags & _FLAG_UV:
        return "the passkey did not verify the user (Face ID, Touch ID or the device PIN)"
    try:
        if _client_origin(client_data_json, application, page_origin) != origin:
            return "the signature's origin disagrees with its client data"
    except PasskeyError as exc:
        return str(exc)
    return None


def to_sshsig(public_key: str, authenticator_data: bytes, client_data_json: bytes, der_signature: bytes,
              namespace: str = ANSWER_NAMESPACE, page_origin: "str | None" = None) -> str:
    """One WebAuthn assertion by the passkey ``public_key`` as an armored SSHSIG in ``namespace``.

    The shape checks below say early what ``ssh-keygen`` would refuse anyway: the rpId hash is the key's
    application's, the user-presence flag is set, no attested-credential data, extensions only when flagged, and
    clientDataJSON is a ``webauthn.get`` from a page whose origin is exactly ``https://<the key's host>`` (http and
    a port only on ``localhost``, exactly ``page_origin`` when given, see :func:`_client_origin`)."""
    key_blob, application = _key_parts(public_key)
    for value, what in ((authenticator_data, "authenticatorData"), (client_data_json, "clientDataJSON"),
                        (der_signature, "signature")):
        if not isinstance(value, bytes) or not value or len(value) > _FIELD_MAX:
            raise PasskeyError(f"{what} is empty or too long")
    if len(authenticator_data) < _AUTH_DATA_MIN:
        raise PasskeyError("authenticatorData is too short")
    if authenticator_data[:32] != hashlib.sha256(application.encode("ascii")).digest():
        raise PasskeyError(f"the passkey signed for another site than {application}")
    flags = authenticator_data[32]
    if not flags & _FLAG_UP:
        raise PasskeyError("the passkey did not record user presence")
    if not flags & _FLAG_UV:
        raise PasskeyError("the passkey did not verify the user (Face ID, Touch ID or the device PIN)")
    if flags & _FLAG_AT:
        raise PasskeyError("authenticatorData carries attested-credential data, which an assertion never does")
    extensions = authenticator_data[_AUTH_DATA_MIN:]
    if bool(flags & _FLAG_ED) != bool(extensions):
        raise PasskeyError("authenticatorData's extension flag disagrees with its length")
    origin = _client_origin(client_data_json, application, page_origin)
    (counter,) = struct.unpack(">I", authenticator_data[33:37])
    signature = (_string(SIG_TYPE) + _string(_ecdsa_signature(der_signature)) + bytes([flags])
                 + struct.pack(">I", counter) + _string(origin) + _string(client_data_json) + _string(extensions))
    sshsig = (b"SSHSIG" + struct.pack(">I", 1) + _string(key_blob) + _string(namespace) + _string(b"")
              + _string(_HASH_ALG) + _string(signature))
    text = base64.b64encode(sshsig).decode("ascii")
    body = "\n".join(text[i:i + 70] for i in range(0, len(text), 70))
    return f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n"
