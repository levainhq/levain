"""levain.consent.sign — a statement signed on the phone with the operator's passkey (Face ID), for a caller outside
levain to verify with stock ``ssh-keygen -Y verify``.

A caller (a script on the laptop) writes a sign request into ``<queue_dir>/sign/<id>.json``::

    {"format": "levain-sign-request/1", "id": "<id>", "namespace": "<ns>", "message": "<text>",
     "title": "<text>", "requester": "<text>", "expires_at": "<UTC timestamp>"}

(written whole: a temporary file, then a rename). ``levain consent phone --sign-namespace NS`` lists the unexpired
requests in the namespaces it was given; the operator reads the whole message on the phone and signs it with an
enrolled passkey, or denies it. The result lands beside the request:

  - ``<id>.sig``: the armored SSHSIG over the message's UTF-8 bytes, in the request's namespace;
  - ``<id>.signed.json``: ``{signer_fp, public_key, namespace, message_sha256, signed_at}``, written after the
    ``.sig``, so its presence means both are whole;
  - ``<id>.denied``: ``{at}``, when the operator denied it. The request file is never deleted here, so a caller can
    tell a denial from an expiry.

The rules, each a refusal before anything is signed:

  - a namespace is ``[a-z0-9][a-z0-9.-]{2,63}`` and never begins ``levain``: a passkey's challenge is the SSHSIG
    signed-data blob of the message (:func:`levain.consent.passkey.challenge`), so a request in a namespace levain
    itself verifies (a consent answer's, a ruling's) would make a phone signature that levain accepts as one. Refused
    at the flag and again at every signing, whatever the flags say;
  - what the operator reads is what is signed: the message is UTF-8 text of at most :data:`MESSAGE_MAX` bytes, and a
    message, title or requester holding a control character other than newline and tab, a Unicode format character
    (bidi controls, zero-width characters) or a line or paragraph separator is listed as refused and never signed,
    since each can render differently from its bytes;
  - the signer is an active passkey enrolled on this server's https name in the cockpit the queue's consent hook
    verifies against (a laptop key never signs here: the point is the operator's face);
  - the challenge is bound to the request file's SHA-256: an answer for a file whose bytes changed since its
    challenge, or that expired, is refused;
  - the signature passes the same checks as a consent answer's: user verification and a same-origin page
    (:func:`levain.consent.passkey.answer_signature_problem`), then ``ssh-keygen`` in the request's namespace
    (:func:`levain.authority.verify_link_sig`).

Nothing here touches the consent queue's database or the authority log: the files live beside them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["MESSAGE_MAX", "REQUEST_FORMAT", "SIGN_DIR", "SignRefused", "SignRequest", "Signer", "namespace_problem",
           "read_request", "sign_dir", "state_of"]

REQUEST_FORMAT = "levain-sign-request/1"
SIGN_DIR = "sign"
MESSAGE_MAX = 8 * 1024             # bytes of UTF-8: a statement a person reads whole on a phone
_TEXT_MAX = 200                    # characters of a title or a requester
_FILE_MAX = 64 * 1024              # a request file past this is not read
_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}")
_KEYS = frozenset({"format", "id", "namespace", "message", "title", "requester", "expires_at"})
# characters that can render differently from their bytes: controls (newline and tab aside), format characters (the
# bidi controls, zero-width joiners and spaces), line and paragraph separators, and a lone surrogate (no UTF-8 form)
_UNSHOWABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class SignRefused(Exception):
    """A sign request, challenge or answer refused, with the words the page shows. ``status`` is the HTTP status the
    phone port answers with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def namespace_problem(namespace: object) -> str | None:
    """Why ``namespace`` is not one the phone may sign in, or None."""
    if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
        return f"namespace {str(namespace)[:80]!r} is not [a-z0-9][a-z0-9.-]{{2,63}}"
    if namespace.startswith("levain"):
        return f"namespace {namespace!r} begins 'levain': levain's own namespaces are never signed on request"
    return None


def _text_problem(text: str, what: str) -> str | None:
    for ch in text:
        if ch in "\n\t":
            continue
        if unicodedata.category(ch) in _UNSHOWABLE:
            return (f"the {what} holds U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')}), which can show differently "
                    "from what is signed")
    return None


@dataclass(frozen=True)
class SignRequest:
    """One sign request file as read: its bytes' SHA-256 and, unless ``refused``, its fields."""
    id: str
    sha256: str
    namespace: str
    message: str
    title: str
    requester: str
    expires_at: datetime
    refused: str | None = None          # why it is never signable, shown to the operator in place of its text


def sign_dir(queue_dir: Path) -> Path:
    return Path(queue_dir) / SIGN_DIR


def _read_bytes(path: Path) -> bytes:
    """The file at ``path`` (never through a symlink), at most :data:`_FILE_MAX` bytes."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(_FILE_MAX + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > _FILE_MAX:
        raise SignRefused(f"the request file is over {_FILE_MAX} bytes")
    return data


def _parse_expiry(text: object) -> datetime:
    from levain.cockpit.consent import ConsentError, parse_stamp

    try:
        return parse_stamp(text)                     # levain's own stamp format
    except ConsentError:
        pass
    try:
        when = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None or when.utcoffset() is None:
        raise SignRefused(f"expires_at {str(text)[:40]!r} is not an ISO 8601 time with a zone")
    return when.astimezone(timezone.utc)


def read_request(queue_dir: Path, request_id: str) -> SignRequest:
    """The request ``request_id`` as its file holds it now. A request that cannot be shown truthfully comes back with
    ``refused`` set; :class:`SignRefused` (404) when there is no such file, (422) when it is not a sign request."""
    if _ID.fullmatch(request_id or "") is None:
        raise SignRefused("no such sign request", 404)
    try:
        raw = _read_bytes(sign_dir(queue_dir) / f"{request_id}.json")
    except FileNotFoundError:
        raise SignRefused("no such sign request", 404) from None
    except OSError as exc:
        raise SignRefused(f"the sign request cannot be read: {exc.strerror or exc}") from None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SignRefused("the sign request is not UTF-8 JSON") from None
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise SignRefused(f"a sign request has exactly the keys {sorted(_KEYS)}")
    if data["format"] != REQUEST_FORMAT:
        raise SignRefused(f"format {str(data['format'])[:40]!r} is not {REQUEST_FORMAT!r}")
    if data["id"] != request_id:
        raise SignRefused("the request's id is not its file name")
    if not all(isinstance(data[k], str) for k in ("namespace", "message", "title", "requester")):
        raise SignRefused("namespace, message, title and requester are strings")
    expires_at = _parse_expiry(data["expires_at"])
    namespace, message, title, requester = data["namespace"], data["message"], data["title"], data["requester"]
    refused = namespace_problem(namespace)
    if refused is None and len(message.encode("utf-8", "surrogatepass")) > MESSAGE_MAX:
        refused = f"the message is over {MESSAGE_MAX} bytes"
    if refused is None and not message:
        refused = "the message is empty"
    if refused is None and (len(title) > _TEXT_MAX or len(requester) > _TEXT_MAX):
        refused = f"the title or requester is over {_TEXT_MAX} characters"
    for text, what in ((message, "message"), (title, "title"), (requester, "requester")):
        refused = refused or _text_problem(text, what)
    return SignRequest(id=request_id, sha256=digest, namespace=namespace, message=message, title=title,
                       requester=requester, expires_at=expires_at, refused=refused)


def state_of(queue_dir: Path, request_id: str) -> str:
    """``signed``, ``denied`` or ``open``: what the files beside the request say."""
    d = sign_dir(queue_dir)
    if (d / f"{request_id}.sig").exists():
        return "signed"
    if (d / f"{request_id}.denied").exists():
        return "denied"
    return "open"


def _write_new(path: Path, text: str) -> None:
    """``text`` at ``path``, mode 0600, whole or not at all, and never over a file already there."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)             # fails when path exists: a result is never overwritten
    finally:
        tmp.unlink(missing_ok=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Signer:
    """The phone port's sign side over the queue at ``queue_dir``: lists, challenges, verifies and records. ``keys()``
    returns the active passkey keys that may sign (fingerprint -> :class:`levain.authority.AuthorityKey`), or raises
    :class:`SignRefused` saying why there are none to ask."""

    def __init__(self, queue_dir: Path, namespaces: "tuple[str, ...]", rp_id: str, keys: Any) -> None:
        for ns in namespaces:
            problem = namespace_problem(ns)
            if problem is not None:
                raise ValueError(f"--sign-namespace: {problem}")
        self.queue_dir, self.namespaces, self.rp_id, self._keys = Path(queue_dir), tuple(namespaces), rp_id, keys
        self._lock = threading.Lock()
        self._challenged: dict[str, tuple[str, str]] = {}    # request id -> (file sha256, signer fp) at challenge

    def _open(self, request_id: str) -> SignRequest:
        """The request, refused unless it is open, unexpired, signable and in a namespace this port signs."""
        request = read_request(self.queue_dir, request_id)
        if request.namespace not in self.namespaces:
            raise SignRefused("no such sign request", 404)        # outside the flags: never shown, never signed
        state = state_of(self.queue_dir, request_id)
        if state != "open":
            raise SignRefused(f"sign request {request_id} is already {state}", 409)
        if request.expires_at <= _now():
            raise SignRefused(f"sign request {request_id} expired at {request.expires_at.isoformat()}", 409)
        if request.refused is not None:
            raise SignRefused(f"sign request {request_id} is refused: {request.refused}")
        return request

    def listing(self, signers: "list[str]") -> list[dict[str, Any]]:
        """Every open, unexpired request in an allowed namespace. A refused one carries ``refused`` and none of its
        text: what cannot be shown truthfully is not shown."""
        if not self.namespaces:
            return []
        try:
            names = sorted(os.listdir(sign_dir(self.queue_dir)))
        except OSError:
            return []
        now, rows = _now(), []
        for name in names:
            rid = name[:-5] if name.endswith(".json") else ""
            if _ID.fullmatch(rid) is None:
                continue
            try:
                request = read_request(self.queue_dir, rid)
            except SignRefused:
                continue                      # not a sign request: nothing to show or sign
            if (request.namespace not in self.namespaces or request.expires_at <= now
                    or state_of(self.queue_dir, rid) != "open"):
                continue
            if request.refused is not None:
                rows.append({"id": rid, "namespace": request.namespace, "refused": request.refused, "signers": []})
                continue
            rows.append({"id": rid, "title": request.title, "message": request.message, "namespace": request.namespace,
                         "requester": request.requester, "expires_at": request.expires_at.isoformat(),
                         "signers": signers})
        return rows

    def challenge(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{signer_fp}`` -> the WebAuthn challenge for the request's message, bound to the file as it is now."""
        from levain.consent.passkey import challenge

        if not isinstance(req, dict) or set(req) != {"signer_fp"}:
            raise SignRefused("request keys must be ['signer_fp']", 400)
        request = self._open(request_id)
        fp = req["signer_fp"]
        if not isinstance(fp, str) or fp not in self._keys():
            raise SignRefused(f"{str(fp)[:80]} is not an active passkey enrolled for {self.rp_id}")
        with self._lock:
            self._challenged[request_id] = (request.sha256, fp)
        return {"challenge": _b64u(challenge(request.message.encode("utf-8"), request.namespace)), "rp_id": self.rp_id}

    def answer(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{authenticator_data, client_data_json, signature}`` (base64url) -> the signature verified and recorded."""
        from levain.authority import VerifierUnavailable, verify_link_sig
        from levain.cockpit.consent import utc_stamp
        from levain.consent.passkey import PasskeyError, answer_signature_problem, b64url_decode, to_sshsig

        need = {"authenticator_data", "client_data_json", "signature"}
        if not isinstance(req, dict) or set(req) != need:
            raise SignRefused(f"request keys must be {sorted(need)}", 400)
        with self._lock:
            bound = self._challenged.get(request_id)
            if bound is None:
                raise SignRefused(f"no challenge is open for sign request {request_id}: ask for one first", 409)
            request = self._open(request_id)
            if request.sha256 != bound[0]:
                self._challenged.pop(request_id, None)
                raise SignRefused(f"sign request {request_id} changed after its challenge: nothing was signed", 409)
            key = self._keys().get(bound[1])
            if key is None:
                raise SignRefused(f"{bound[1]} is no longer an active passkey enrolled for {self.rp_id}")
            message = request.message.encode("utf-8")
            try:
                signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                      b64url_decode(req["client_data_json"], "client_data_json"),
                                      b64url_decode(req["signature"], "signature"), namespace=request.namespace)
            except PasskeyError as exc:
                raise SignRefused(str(exc)) from None
            problem = answer_signature_problem(signature, key.public_key)
            if problem is not None:
                raise SignRefused(problem)
            try:
                ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                     namespace=request.namespace)
            except VerifierUnavailable as exc:
                raise SignRefused(f"the signature could not be verified now: {exc}", 503) from None
            if not ok:
                raise SignRefused("the passkey's signature does not verify over this message")
            d, signed_at = sign_dir(self.queue_dir), utc_stamp(_now())
            try:
                _write_new(d / f"{request_id}.sig", signature)
            except FileExistsError:
                raise SignRefused(f"sign request {request_id} is already signed", 409) from None
            _write_new(d / f"{request_id}.signed.json", json.dumps({
                "signer_fp": key.fingerprint, "public_key": key.public_key, "namespace": request.namespace,
                "message_sha256": hashlib.sha256(message).hexdigest(), "signed_at": signed_at}) + "\n")
            self._challenged.pop(request_id, None)
        return {"id": request_id, "signer_fp": key.fingerprint, "namespace": request.namespace, "signed_at": signed_at}

    def deny(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{}`` -> ``<id>.denied``. A refused request may be denied too (that is how the operator clears it)."""
        from levain.cockpit.consent import utc_stamp

        if req != {}:
            raise SignRefused("a deny carries an empty object", 400)
        with self._lock:
            request = read_request(self.queue_dir, request_id)
            if request.namespace not in self.namespaces:
                raise SignRefused("no such sign request", 404)
            state = state_of(self.queue_dir, request_id)
            if state != "open":
                raise SignRefused(f"sign request {request_id} is already {state}", 409)
            at = utc_stamp(_now())
            try:
                _write_new(sign_dir(self.queue_dir) / f"{request_id}.denied", json.dumps({"at": at}) + "\n")
            except FileExistsError:
                raise SignRefused(f"sign request {request_id} is already denied", 409) from None
            self._challenged.pop(request_id, None)
        return {"id": request_id, "denied_at": at}


def _b64u(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
