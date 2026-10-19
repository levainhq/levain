"""levain.consent.sign — a statement signed on the phone with the operator's passkey, for a caller outside levain to
verify with stock ``ssh-keygen -Y verify``.

The signature is a user-verified passkey signature (Face ID, Touch ID or the device passcode, as the authenticator
reports it): levain refuses one without the user-verified flag before it records anything. A stock verify proves
that the key signed these bytes in this namespace; it does not check how the user was verified.

A caller (a script on the laptop) writes a sign request into ``<queue_dir>/sign/<id>.json``::

    {"format": "levain-sign-request/1", "id": "<id>", "namespace": "<ns>", "message": "<text>",
     "title": "<text>", "requester": "<text>", "expires_at": "<UTC timestamp>"}

(written whole: a temporary file, then a rename). ``levain consent phone --sign-namespace NS`` lists the unexpired
requests in the namespaces it was given; the operator reads the whole message on the phone and signs it with an
enrolled passkey, or denies it. A file in the sign directory that is not a well-formed sign request is listed as
refused, with the reason, never skipped. The result lands beside the request:

  - ``<id>.sig``: the armored SSHSIG over the message's UTF-8 bytes, in the request's namespace;
  - ``<id>.signed.json``: ``{signer_fp, public_key, namespace, message_sha256, signed_at}``, written last: it is the
    completion marker, and only its presence makes a request signed. A ``.sig`` without it is the orphan of an
    answer that did not finish, and the next successful answer replaces it;
  - ``<id>.denied``: ``{at, request_sha256}``, when the operator denied it: the version of the request the page
    showed (the file's SHA-256, or for a file that could not be read a version taken from its metadata), sent back
    with the deny, which a request changed since is refused. The request file is never deleted here, so a caller
    can tell a denial from an expiry.

The rules, each a refusal before anything is signed:

  - a namespace is ``[a-z0-9][a-z0-9.-]{2,63}``, never begins ``levain``, is none of :data:`REFUSED_NAMESPACES`
    (ssh's conventional ``git``, ``file`` and ``email``) and is no entry or family of levain's namespace registry
    (:mod:`levain.contracts.namespaces`): a passkey's challenge is the SSHSIG signed-data blob of the message
    (:func:`levain.consent.passkey.challenge`), so a request in a namespace levain itself verifies (a consent
    answer's, a ruling's, a team ledger commit's in ``git``) would make a phone signature that levain accepts as one.
    Refused at the flag and again at every signing, whatever the flags say;
  - what the operator reads is what is signed: the message is UTF-8 text of at most :data:`MESSAGE_MAX` bytes, and a
    message, title or requester holding a control character other than newline and tab, a Unicode format character
    (bidi controls, zero-width characters), a line or paragraph separator, a space other than U+0020, or a
    private-use or unassigned code point is listed as refused and never signed, since each can render differently
    from its bytes;
  - the operator signs what was shown: the listing carries each request file's SHA-256, the page sends it back with
    the challenge, and a challenge that names another (the file changed since it was listed) or none is refused;
  - the signer is an active passkey enrolled on this server's https name in the cockpit the queue's consent hook
    verifies against (a laptop key never signs here: the point is the operator's own passkey);
  - the challenge is bound to the request file's SHA-256: an answer for a file whose bytes changed since its
    challenge, or that expired, is refused, checked again after the signature verifies and just before the result
    is written (the verify runs outside the lock, so a file changed or expired during it is refused too);
  - the signature passes the same checks as a consent answer's: user verification and a page whose origin is
    exactly ``https://<rp id>``, or on ``localhost`` exactly the phone port's own ``http://localhost:<port>``
    (:func:`levain.consent.passkey.answer_signature_problem`), then ``ssh-keygen`` in the request's namespace
    (:func:`levain.authority.verify_link_sig`).

One bad file never takes the listing down: a file that cannot be read, is not a regular file (a FIFO is opened
non-blocking and refused, never waited on) or holds a time that cannot be converted is a refused row, and the
consent requests beside it still list. A file whose namespace is malformed or one no signer may claim is listed as
refused (and may be denied); with no ``--sign-namespace`` nothing in the directory is listed. The listing reads at
most :data:`LIST_MAX` requests and says how many it did not read.

Nothing here touches the consent queue's database or the authority log: the files live beside them.

Known edges:

  - two phone servers on one queue dir can race a deny against a sign (the lock is per process): one server per
    queue is the supported shape;
  - identical messages in one namespace share a challenge and so a signature: the caller embeds a unique id (a
    report id, a period) in the message, and its verifier checks it;
  - look-alike letters and NFC vs NFD forms are not refused: a caller ships the exact message bytes as a file, never
    re-typed or normalised.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["LIST_MAX", "MESSAGE_MAX", "REFUSED_NAMESPACES", "REQUEST_FORMAT", "SIGN_DIR", "SignRefused", "SignRequest",
           "Signer", "namespace_problem", "read_request", "sign_dir", "state_of"]

REQUEST_FORMAT = "levain-sign-request/1"
SIGN_DIR = "sign"
MESSAGE_MAX = 8 * 1024             # bytes of UTF-8: a statement a person reads whole on a phone
_TEXT_MAX = 200                    # characters of a title or a requester
_FILE_MAX = 64 * 1024              # a request file past this is not read
LIST_MAX = 50                      # open requests one listing shows; past it the page shows how many were left
_SCAN_MAX = 4096                   # directory entries one listing looks at
_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}")
_KEYS = frozenset({"format", "id", "namespace", "message", "title", "requester", "expires_at"})
# ssh's conventional namespaces: git's commit signing (levain's team ledger verifies in it), file and email
REFUSED_NAMESPACES = frozenset({"git", "file", "email"})
# characters that can render differently from their bytes: controls (newline and tab aside), format characters (the
# bidi controls, zero-width joiners and spaces), line and paragraph separators, a lone surrogate (no UTF-8 form),
# private-use and unassigned code points (tofu or blank); every space but U+0020 is refused beside these
_UNSHOWABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})


class SignRefused(Exception):
    """A sign request, challenge or answer refused, with the words the page shows. ``status`` is the HTTP status the
    phone port answers with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def namespace_problem(namespace: object) -> str | None:
    """Why ``namespace`` is not one the phone may sign in, or None."""
    from levain.contracts.namespaces import FAMILIES, REGISTRY

    if not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None:
        return f"namespace {str(namespace)[:80]!r} is not [a-z0-9][a-z0-9.-]{{2,63}}"
    if namespace.startswith("levain"):
        return f"namespace {namespace!r} begins 'levain': levain's own namespaces are never signed on request"
    if namespace in REFUSED_NAMESPACES or namespace in REGISTRY or namespace.startswith(tuple(FAMILIES)):
        return f"namespace {namespace!r} is one levain or ssh verifies signatures in: never signed on request"
    return None


def _text_problem(text: str, what: str) -> str | None:
    for ch in text:
        if ch in "\n\t ":
            continue
        category = unicodedata.category(ch)
        if category in _UNSHOWABLE or category == "Zs":
            return (f"the {what} holds U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')}), which can show differently "
                    "from what is signed")
    return None


@dataclass(frozen=True)
class SignRequest:
    """One sign request file as read: its version (its bytes' SHA-256, or for a file that could not be read one
    taken from its metadata) and, unless ``refused``, its fields. A refused request keeps what could be read of it:
    ``namespace`` is ``""`` when the file names no well-formed one, and ``expires_at`` is None when its expiry could
    not be read."""
    id: str
    sha256: str
    namespace: str
    message: str
    title: str
    requester: str
    expires_at: "datetime | None"
    refused: str | None = None          # why it is never signable, shown to the operator in place of its text


def sign_dir(queue_dir: Path) -> Path:
    return Path(queue_dir) / SIGN_DIR


def _read_bytes(path: Path) -> bytes:
    """The regular file at ``path`` (never through a symlink), at most :data:`_FILE_MAX` bytes. Opened non-blocking,
    so a FIFO in its place is refused, never waited on."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SignRefused("the request is not a regular file")
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
        if when is None or when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("no zone")
        return when.astimezone(timezone.utc)
    except (OverflowError, ValueError):              # an in-range local time can overflow on conversion to UTC
        raise SignRefused(f"expires_at {str(text)[:40]!r} is not an ISO 8601 time with a zone") from None


def read_request(queue_dir: Path, request_id: str) -> SignRequest:
    """The request ``request_id`` as its file holds it now. :class:`SignRefused` (404) only when there is no such
    file; any file there that cannot be shown truthfully, or is not a well-formed sign request, comes back with
    ``refused`` set, so the operator sees it and the caller can be told."""
    if _ID.fullmatch(request_id or "") is None:
        raise SignRefused("no such sign request", 404)

    path = sign_dir(queue_dir) / f"{request_id}.json"

    def refused(why: str, digest: str = "", data: Any = None) -> SignRequest:
        ns = data.get("namespace") if isinstance(data, dict) else None
        return SignRequest(id=request_id, sha256=digest or _stat_version(path),
                           namespace=ns if isinstance(ns, str) and _NAMESPACE.fullmatch(ns) else "",
                           message="", title="", requester="", expires_at=None, refused=f"not a sign request: {why}")

    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        raise SignRefused("no such sign request", 404) from None
    except SignRefused as exc:
        return refused(str(exc))
    except OSError as exc:
        return refused(f"it cannot be read: {exc.strerror or exc}")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return refused("it is not UTF-8 JSON", digest)
    if not isinstance(data, dict) or set(data) != _KEYS:
        return refused(f"a sign request has exactly the keys {sorted(_KEYS)}", digest, data)
    if data["format"] != REQUEST_FORMAT:
        return refused(f"format {str(data['format'])[:40]!r} is not {REQUEST_FORMAT!r}", digest, data)
    if data["id"] != request_id:
        return refused("its id is not its file name", digest, data)
    if not all(isinstance(data[k], str) for k in ("namespace", "message", "title", "requester")):
        return refused("namespace, message, title and requester are strings", digest, data)
    try:
        expires_at = _parse_expiry(data["expires_at"])
    except SignRefused as exc:
        return refused(str(exc), digest, data)
    namespace, message, title, requester = data["namespace"], data["message"], data["title"], data["requester"]
    problem = namespace_problem(namespace)
    if problem is None and len(message.encode("utf-8", "surrogatepass")) > MESSAGE_MAX:
        problem = f"the message is over {MESSAGE_MAX} bytes"
    if problem is None and not message:
        problem = "the message is empty"
    if problem is None and (len(title) > _TEXT_MAX or len(requester) > _TEXT_MAX):
        problem = f"the title or requester is over {_TEXT_MAX} characters"
    for text, what in ((message, "message"), (title, "title"), (requester, "requester")):
        problem = problem or _text_problem(text, what)
    return SignRequest(id=request_id, sha256=digest, namespace=namespace if _NAMESPACE.fullmatch(namespace) else "",
                       message=message, title=title, requester=requester, expires_at=expires_at, refused=problem)


def _stat_version(path: Path) -> str:
    """The version of a request file that could not be read: what its metadata says (never followed through a
    symlink), so a deny names the file the page showed and a file replaced since is refused. ``""`` when it is gone."""
    try:
        st = os.lstat(path)
    except OSError:
        return ""
    seen = f"{st.st_dev}:{st.st_ino}:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
    return "stat:" + hashlib.sha256(seen.encode("ascii")).hexdigest()


def state_of(queue_dir: Path, request_id: str) -> str:
    """``signed``, ``denied`` or ``open``: what the files beside the request say. ``<id>.signed.json`` is the
    completion marker: a ``.sig`` alone is an orphan and leaves the request open."""
    d = sign_dir(queue_dir)
    if (d / f"{request_id}.signed.json").exists():
        return "signed"
    if (d / f"{request_id}.denied").exists():
        return "denied"
    return "open"


def _write_tmp(path: Path, text: str) -> Path:
    """``text`` in a new 0600 temporary file beside ``path``, flushed to disk."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _fsync_dir(directory: Path) -> None:
    """The directory's entries to disk, so a result reported written survives a crash."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, text: str) -> None:
    """``text`` at ``path``, mode 0600, whole or not at all, and never over a file already there."""
    tmp = _write_tmp(path, text)
    try:
        os.link(tmp, path)             # fails when path exists: a result is never overwritten
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _write_over(path: Path, text: str) -> None:
    """``text`` at ``path``, mode 0600, whole or not at all, replacing what is there (an orphan ``.sig``)."""
    tmp = _write_tmp(path, text)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _publish(write: Any, path: Path, text: str, exists: str) -> None:
    """``write(path, text)``: a result already at ``path`` is a 409 saying ``exists``, any other failure to write is a
    503 naming the path and the error, never an internal error the page cannot read."""
    try:
        write(path, text)
    except FileExistsError:
        raise SignRefused(exists, 409) from None
    except OSError as exc:
        raise SignRefused(f"could not write {path}: [Errno {exc.errno}] {exc.strerror or exc}", 503) from None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Signer:
    """The phone port's sign side over the queue at ``queue_dir``: lists, challenges, verifies and records. ``keys()``
    returns the active passkey keys that may sign (fingerprint -> :class:`levain.authority.AuthorityKey`), or raises
    :class:`SignRefused` saying why there are none to ask. ``page_origin`` is the phone port's own origin when its
    name is ``localhost`` (a passkey's page must be exactly it), else None."""

    def __init__(self, queue_dir: Path, namespaces: "tuple[str, ...]", rp_id: str, keys: Any) -> None:
        for ns in namespaces:
            problem = namespace_problem(ns)
            if problem is not None:
                raise ValueError(f"--sign-namespace: {problem}")
        self.queue_dir, self.namespaces, self.rp_id, self._keys = Path(queue_dir), tuple(namespaces), rp_id, keys
        self.page_origin: str | None = None
        self._lock = threading.Lock()
        self._challenged: dict[str, tuple[str, str]] = {}    # request id -> (file sha256, signer fp) at challenge

    def _ours(self, request: SignRequest) -> bool:
        """A request in a namespace this port signs, or a refused one whose namespace no signer could own (malformed,
        or one levain or ssh verifies in): the operator still sees it, and may deny it. With no namespaces at all,
        nothing is ours."""
        if not self.namespaces:
            return False
        return request.namespace in self.namespaces or (
            request.refused is not None and namespace_problem(request.namespace) is not None)

    def _open(self, request_id: str) -> SignRequest:
        """The request, refused unless it is open, unexpired, signable and in a namespace this port signs."""
        request = read_request(self.queue_dir, request_id)
        if not self._ours(request):
            raise SignRefused("no such sign request", 404)        # outside the flags: never shown, never signed
        state = state_of(self.queue_dir, request_id)
        if state != "open":
            raise SignRefused(f"sign request {request_id} is already {state}", 409)
        if request.expires_at is not None and request.expires_at <= _now():
            raise SignRefused(f"sign request {request_id} expired at {request.expires_at.isoformat()}", 409)
        if request.refused is not None:
            raise SignRefused(f"sign request {request_id} is refused: {request.refused}")
        return request

    def listing(self, signers: "list[str]") -> list[dict[str, Any]]:
        """The rows of :meth:`page`."""
        return self.page(signers)["rows"]

    def page(self, signers: "list[str]") -> dict[str, Any]:
        """``{rows, overflow}``: every open, unexpired request in an allowed namespace, with its file's ``sha256``
        (what the page sends back with a challenge or a deny), read in name order up to :data:`LIST_MAX` files;
        ``overflow`` counts the request files not read (at least that many when the directory holds more than one
        listing looks at). A refused one carries ``refused``, its ``sha256`` and none of its text: what cannot be
        shown truthfully is not shown, and a file that is not a sign request is listed as refused, never skipped.
        A challenge whose request is no longer listed (gone, expired, answered or past the cap) is dropped."""
        if not self.namespaces:
            return {"rows": [], "overflow": 0}
        names: list[str] = []
        more = False
        try:
            with os.scandir(sign_dir(self.queue_dir)) as entries:
                for n, entry in enumerate(entries):
                    if n >= _SCAN_MAX:
                        more = True
                        break
                    rid = entry.name[:-5] if entry.name.endswith(".json") else ""
                    if _ID.fullmatch(rid) is not None:
                        names.append(rid)      # not a result file (<id>.signed.json) or a temporary one
        except OSError:
            names = []
        names.sort()
        now, rows, read = _now(), [], 0
        for rid in names:
            if len(rows) >= LIST_MAX:
                break
            read += 1
            try:
                if state_of(self.queue_dir, rid) != "open":   # a decided request is never parsed again
                    continue
                request = read_request(self.queue_dir, rid)
                if not self._ours(request) or (request.expires_at is not None and request.expires_at <= now):
                    continue
            except SignRefused:
                continue                      # gone since the directory was read
            except Exception as exc:          # one file never takes the listing (and the consent page) down
                rows.append({"id": rid, "namespace": "", "signers": [],
                             "sha256": _stat_version(sign_dir(self.queue_dir) / f"{rid}.json"),
                             "refused": f"not a sign request: it cannot be read ({type(exc).__name__})"})
                continue
            if request.refused is not None:
                rows.append({"id": rid, "namespace": request.namespace, "refused": request.refused,
                             "sha256": request.sha256, "signers": []})
                continue
            rows.append({"id": rid, "title": request.title, "message": request.message, "namespace": request.namespace,
                         "requester": request.requester, "expires_at": request.expires_at.isoformat(),
                         "sha256": request.sha256, "signers": signers})
        listed = {r["id"] for r in rows if not r.get("refused")}
        with self._lock:
            for rid in [rid for rid in self._challenged if rid not in listed]:
                del self._challenged[rid]
        return {"rows": rows, "overflow": len(names) - read + (1 if more else 0)}

    def challenge(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{signer_fp, request_sha256}`` -> the WebAuthn challenge for the request's message, bound to the file as
        it is now. ``request_sha256`` is the listing's ``sha256`` for the request the page showed: a file that
        changed since it was listed is refused (409), so the operator never signs bytes the page did not show."""
        from levain.consent.passkey import challenge

        if not isinstance(req, dict) or "signer_fp" not in req or not set(req) <= {"signer_fp", "request_sha256"}:
            raise SignRefused("request keys must be ['request_sha256', 'signer_fp']", 400)
        request = self._open(request_id)
        if not req.get("request_sha256"):
            raise SignRefused(f"the challenge for sign request {request_id} names no listed version: reload and "
                              "read it again", 409)
        if req["request_sha256"] != request.sha256:
            raise SignRefused(f"sign request {request_id} changed since it was shown: reload and read it again; "
                              "nothing was signed", 409)
        fp = req["signer_fp"]
        if not isinstance(fp, str) or fp not in self._keys():
            raise SignRefused(f"{str(fp)[:80]} is not an active passkey enrolled for {self.rp_id}")
        with self._lock:
            self._challenged[request_id] = (request.sha256, fp)
        return {"challenge": _b64u(challenge(request.message.encode("utf-8"), request.namespace)), "rp_id": self.rp_id}

    def answer(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{authenticator_data, client_data_json, signature}`` (base64url) -> the signature verified and recorded:
        the ``.sig`` (over an orphan one), then ``.signed.json`` last, never over one already there. The key read
        and the verify run outside the lock; the request is read again under it just before anything is written,
        and one that changed, expired or was answered meanwhile is refused (409)."""
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
        changed = f"sign request {request_id} changed after its challenge: nothing was signed"
        request = self._open(request_id)
        if request.sha256 != bound[0]:
            with self._lock:
                if self._challenged.get(request_id) == bound:
                    del self._challenged[request_id]
            raise SignRefused(changed, 409)
        key = self._keys().get(bound[1])
        if key is None:
            raise SignRefused(f"{bound[1]} is no longer an active passkey enrolled for {self.rp_id}")
        message = request.message.encode("utf-8")
        try:
            signature = to_sshsig(key.public_key, b64url_decode(req["authenticator_data"], "authenticator_data"),
                                  b64url_decode(req["client_data_json"], "client_data_json"),
                                  b64url_decode(req["signature"], "signature"), namespace=request.namespace,
                                  page_origin=self.page_origin)
        except PasskeyError as exc:
            raise SignRefused(str(exc)) from None
        problem = answer_signature_problem(signature, key.public_key, self.page_origin)
        if problem is not None:
            raise SignRefused(problem)
        try:
            ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                 namespace=request.namespace)
        except VerifierUnavailable as exc:
            raise SignRefused(f"the signature could not be verified now: {exc}", 503) from None
        if not ok:
            raise SignRefused("the passkey's signature does not verify over this message")
        with self._lock:
            # what was verified is what is still there: the same challenge, the same bytes, open and unexpired now
            if self._challenged.get(request_id) != bound:
                raise SignRefused(f"sign request {request_id} was answered or re-challenged meanwhile: nothing was "
                                  "signed", 409)
            now = self._open(request_id)
            if now.sha256 != bound[0] or now.namespace != request.namespace:
                del self._challenged[request_id]
                raise SignRefused(changed, 409)
            d, signed_at = sign_dir(self.queue_dir), utc_stamp(_now())
            _publish(_write_over, d / f"{request_id}.sig", signature, "")   # over an orphan: never a 409
            _publish(_write_new, d / f"{request_id}.signed.json", json.dumps({
                "signer_fp": key.fingerprint, "public_key": key.public_key, "namespace": request.namespace,
                "message_sha256": hashlib.sha256(message).hexdigest(), "signed_at": signed_at}) + "\n",
                f"sign request {request_id} is already signed")
            del self._challenged[request_id]
        return {"id": request_id, "signer_fp": key.fingerprint, "namespace": request.namespace, "signed_at": signed_at}

    def deny(self, request_id: str, req: Any) -> dict[str, Any]:
        """``{request_sha256}`` (the listing's ``sha256`` for the row the page showed) -> ``<id>.denied``, which
        records it. A request changed since it was shown is refused (409). A refused request may be denied too
        (that is how the operator clears it)."""
        from levain.cockpit.consent import utc_stamp

        if not isinstance(req, dict) or set(req) != {"request_sha256"} or not isinstance(req["request_sha256"], str):
            raise SignRefused("request keys must be ['request_sha256']", 400)
        with self._lock:
            request = read_request(self.queue_dir, request_id)
            if not self._ours(request):
                raise SignRefused("no such sign request", 404)
            state = state_of(self.queue_dir, request_id)
            if state != "open":
                raise SignRefused(f"sign request {request_id} is already {state}", 409)
            if not req["request_sha256"] or req["request_sha256"] != request.sha256:
                raise SignRefused(f"sign request {request_id} changed since it was shown: reload and read it again; "
                                  "nothing was denied", 409)
            at = utc_stamp(_now())
            _publish(_write_new, sign_dir(self.queue_dir) / f"{request_id}.denied",
                     json.dumps({"at": at, "request_sha256": request.sha256}) + "\n",
                     f"sign request {request_id} is already denied")
            self._challenged.pop(request_id, None)
        return {"id": request_id, "denied_at": at}


def _b64u(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
