"""Board row 113 (option F2): a dispatch configuration item approved on the operator's PHONE, signed by the LAPTOP key.

The consent sheet signs only with a phone passkey, and the control store's trust root can never be a passkey
(:func:`levain.dispatch.control._root_problem`), so the phone cannot BE the configuration signature. It is a gate in
front of it (the countersign pattern, as terminal answers): ``config request`` files levain's own plain sign request in
:data:`CONFIG_APPROVE_NAMESPACE` whose fixed message names the store, kind, generation, prev, the SHA-256 of the
item's exact canonical bytes and the passkey site (``rp``) of the cockpit the queue published at filing; a passkey
of that site signs it; ``config accept-approved`` verifies that approval, loads the
pending item whose bytes hash to it, and only then asks the trust-root laptop key to sign those bytes (a Touch ID touch,
no terminal question) and hands them to the unchanged :meth:`ControlStore.accept`. Nothing here widens what the store
accepts: the trust root, its custody rule and ``accept`` are untouched, so an approval alone writes nothing and no
laptop signature is ever made without a verified approval of those bytes.

What the phone is shown is levain's own one-line ``change`` and ``summary`` beside the item's hash; the full diff is
printed at the laptop before the touch, and the touch is the only laptop-side question. A signature in this namespace
therefore binds the hash, and the words are the operator's reading aid, not part of what is verified. The approval's
deadline is the ``expires`` line of the SIGNED message (the request file's own ``expires_at`` is a second, never a
looser, bound). The approval class floor is T2 (a ``display-phone`` passkey, what a thread envelope and a signed
review clear need): the T3 authority over configuration stays the laptop key's.

The approval is judged at the point of use: ``accept-approved`` reads it once before the touch and again after it
(fresh clock, fresh authority replay), and a signature made while the approval stopped holding is discarded. The
kept item is held under an exclusive lock from the first read to the store's accept, so two runs on one item never
both reach the key.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from levain.contracts.namespaces import CONFIG_APPROVE_NAMESPACE, LEVAIN_SIGN_REQUESTS
from levain.dispatch import control as dctl

__all__ = ["APPROVE_TIER", "Approval", "FORMAT_LINE", "PENDING_DIR", "REQUEST_TITLE", "REQUEST_TTL_S", "build_message",
           "claimed", "file_request", "load_pending", "push_request", "queue_rp", "read_approval", "remove_pending"]

FORMAT_LINE = "levain-dispatch-config-approve/1"
REQUEST_TITLE = "Approve a dispatch configuration change"
REQUEST_TTL_S = 3600
APPROVE_TIER = "T2"
PENDING_DIR = "config-pending"
_LINE_MAX = 300
_SHA = re.compile(r"[0-9a-f]{64}")
#: a kept item (or a temporary one) older than this can back no usable approval (its signed deadline is
#: REQUEST_TTL_S after filing), so the next request removes it: the pending directory stays bounded
_PRUNE_AGE_S = 2 * REQUEST_TTL_S
_SCAN_MAX = 4096


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Approval:
    """A verified phone approval: the fields its message names, the passkey that signed and the request."""
    request_id: str
    store_id: str
    kind: str
    generation: int
    prev: str
    item_sha256: str
    signer_fp: str


def _one_line(text: str) -> str:
    """``text`` as one ASCII line of at most :data:`_LINE_MAX` characters: what a phone reads is what is signed, so
    every non-ASCII character shows as its escape and a control character as a space."""
    out = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)
    out = out.encode("ascii", "backslashreplace").decode("ascii")
    out = " ".join(out.split())
    return (out[:_LINE_MAX - 3] + "...") if len(out) > _LINE_MAX else (out or "(none)")


def build_message(item: dctl.ConfigItem, raw: bytes, *, expires: datetime, rp: str, change: str, summary: str) -> str:
    """The approval request's message for ``item`` (parsed from ``raw``, its exact canonical bytes), usable until
    ``expires`` and only by a passkey made on site ``rp`` (both signed with it)."""
    from levain.cockpit.consent import utc_stamp
    from levain.consent.passkey import is_host_name

    if dctl.parse_item(raw).text != item.text:
        raise ValueError("raw is not the item's bytes")
    if not is_host_name(rp):
        raise ValueError(f"{str(rp)[:80]!r} is not a passkey site")
    msg = (f"{FORMAT_LINE}\nstore: {_one_line(item.store_id)}\nkind: {item.kind}\ngeneration: {item.generation}\n"
           f"prev: {_one_line(item.prev) if item.prev else 'none'}\nitem-sha256: {hashlib.sha256(raw).hexdigest()}\n"
           f"expires: {utc_stamp(expires)}\nrp: {rp}\nchange: {_one_line(change)}\nsummary: {_one_line(summary)}\n")
    return msg


def queue_rp(queue_dir: Path) -> str:
    """The passkey site of the cockpit ``queue_dir`` has published (its origin's host): the one site whose passkeys
    can approve a request filed there now. :class:`ValueError` when the queue has published none."""
    from urllib.parse import urlsplit

    from levain.consent.cockpit_origin import read_origin
    from levain.consent.passkey import is_host_name

    origin = read_origin(Path(queue_dir))
    host = urlsplit(origin).hostname if origin else None
    if not is_host_name(host):
        raise ValueError(f"the signing queue {queue_dir} has published no cockpit origin, so no passkey site is "
                         "known to approve it (start the cockpit)")
    return host


def _parse_message(message: str) -> dict[str, str] | None:
    if re.fullmatch(LEVAIN_SIGN_REQUESTS[CONFIG_APPROVE_NAMESPACE], message) is None:
        return None
    fields: dict[str, str] = {}
    for line in message.split("\n")[1:-1]:
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


# --- the pending item (the exact bytes an approval is over) -----------------------------------------------------------
def _pending_dir(directory: Path) -> Path:
    return Path(directory) / PENDING_DIR


def _write_pending(directory: Path, sha: str, raw: bytes) -> None:
    """Keep ``raw`` as a NEW ``<sha>.item``: written whole under a temporary name only this call uses, hard-linked
    into place (never over another file), the directory synced. An item already kept under that name is never
    replaced, refreshed or shared (:class:`FileExistsError`): each kept item belongs to the one filing that made
    it, its inode never changes while kept, and its mtime is its filing time (so :func:`_prune`'s age is honest).
    Any failure, the sync after publishing included, leaves neither the item nor the temporary behind."""
    d = _pending_dir(directory)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / f"{sha}.item"
    tmp = d / f".{sha}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    published = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path, follow_symlinks=False)
        except FileExistsError:
            raise FileExistsError(f"an item with these exact bytes is already kept ({path}); its own request "
                                  "stands") from None
        published = True
        os.unlink(tmp)
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        for leftover in ((path, tmp) if published else (tmp,)):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        raise


def _prune(directory: Path) -> None:
    """Remove kept items and temporaries older than :data:`_PRUNE_AGE_S` (no approval over them can still be used),
    looking at no more than :data:`_SCAN_MAX` entries. Never raises."""
    d = _pending_dir(directory)
    cutoff = time.time() - _PRUNE_AGE_S
    try:
        with os.scandir(d) as it:
            for n, entry in enumerate(it):
                if n >= _SCAN_MAX:
                    break
                if not (entry.name.endswith(".item") or entry.name.endswith(".tmp")):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def _read_kept(fd: int, sha: str) -> bytes:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise dctl.ConfigRefused("the kept item is not a regular file")
    chunks, size = [], 0
    while size <= dctl.ITEM_MAX_BYTES:
        chunk = os.read(fd, dctl.ITEM_MAX_BYTES + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    raw = b"".join(chunks)
    if len(raw) > dctl.ITEM_MAX_BYTES or hashlib.sha256(raw).hexdigest() != sha:
        raise dctl.ConfigRefused("the kept item is not the one the approval is over")
    return raw


def _open_kept(directory: Path, sha: str) -> int:
    if _SHA.fullmatch(sha or "") is None:
        raise dctl.ConfigRefused("the approval names no item hash")
    path = _pending_dir(directory) / f"{sha}.item"
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise dctl.ConfigRefused(f"the item this approval is over is not kept here ({path}): the request was filed "
                                 "elsewhere, its item was spent or is gone; file a new request") from None


def load_pending(directory: Path, sha: str) -> bytes:
    """The pending item whose bytes hash to ``sha``, or :class:`levain.dispatch.control.ConfigRefused`. The file is a
    regular file read without following a link; its bytes are re-hashed, so a swapped file is refused."""
    fd = _open_kept(directory, sha)
    try:
        return _read_kept(fd, sha)
    finally:
        os.close(fd)


@contextmanager
def claimed(directory: Path, sha: str) -> Iterator[bytes]:
    """The kept item's bytes (as :func:`load_pending` reads them), held under an exclusive lock on the open file for
    the block (a kept item is never replaced, so every claim of it locks the same inode): a second claim of the same
    item, while the first is held, is refused at once
    (:class:`levain.dispatch.control.ConfigRefused`), never queued behind it, so it never reaches the key."""
    fd = _open_kept(directory, sha)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise dctl.ConfigRefused("another accept-approved holds this item now; nothing was signed") from None
        yield _read_kept(fd, sha)
    finally:
        os.close(fd)


def remove_pending(directory: Path, sha: str) -> None:
    try:
        os.unlink(_pending_dir(directory) / f"{sha}.item")
    except OSError:
        pass


def file_request(directory: Path, queue_dir: Path, raw: bytes, *, change: str, summary: str) -> str:
    """Keep ``raw`` (an item's canonical bytes) and file levain's approval request over it in ``queue_dir``'s sign
    directory; return the request id. The request names the passkey site of the cockpit the queue has published NOW
    (:func:`queue_rp`), signed with it, so only a passkey of that site can approve it, however the cockpit stands at
    the laptop touch. Whatever the filing raises (:class:`ValueError` for no published site or a message outside
    the grammar, :class:`OSError` or :class:`levain.consent.sign.SignRefused` for a queue it cannot write,
    :class:`FileExistsError` for these exact bytes already kept) propagates with nothing filed and nothing this call
    kept left behind. The item this call keeps is its own (:func:`_write_pending` never shares one), so removing it
    on failure can never take another filing's item."""
    from levain.consent.sign import file_levain_request

    item = dctl.parse_item(raw)
    message = build_message(item, raw, expires=_now() + timedelta(seconds=REQUEST_TTL_S), rp=queue_rp(queue_dir),
                            change=change, summary=summary)
    _prune(directory)
    sha = hashlib.sha256(raw).hexdigest()
    _write_pending(directory, sha, raw)
    try:
        return file_levain_request(Path(queue_dir), CONFIG_APPROVE_NAMESPACE, message, title=REQUEST_TITLE,
                                   ttl_s=REQUEST_TTL_S)
    except BaseException:
        remove_pending(directory, sha)
        raise


def push_request(queue_dir: Path, rid: str) -> str | None:
    """Push the request to the phone; ``None`` when started, else why not. Never raises."""
    try:
        from levain import webpush
        from levain.consent.cockpit_origin import read_origin
        from levain.consent.sign import deep_link
        from levain.webpush import sender

        origin = read_origin(Path(queue_dir))
        if origin is None:
            return "the cockpit has published no origin to link the request to"
        if not webpush.live(Path(queue_dir)):
            return "no phone is subscribed to the signing queue"
        if not sender.spawn_sign_request(
                Path(queue_dir), rid, title=REQUEST_TITLE,
                body="A dispatch configuration change waits for your approval.", url=deep_link(origin, rid)):
            return "the push could not be started"
        return None
    except Exception as exc:  # noqa: BLE001 - a push is a convenience; the request stands
        return f"the push failed ({type(exc).__name__})"


# --- reading the approval back ----------------------------------------------------------------------------------------
def read_approval(queue_dir: Path, request_id: object, *, keys: Callable[[str], Mapping[str, Any]],
                  now: datetime | None = None, not_done: str = "nothing was signed") -> Approval:
    """The verified approval in sign request ``request_id``: levain's own request in :data:`CONFIG_APPROVE_NAMESPACE`
    in exactly its grammar, signed (not open, not denied), whose signed ``expires`` (and the request file's own
    expiry) is after ``now`` (default: the clock now) and after the signing, and whose signature verifies over its
    message in that namespace by an active passkey ``keys(rp_id)`` names, whose class covers :data:`APPROVE_TIER`.
    ``rp_id`` is the message's own signed ``rp`` line (the site the queue's cockpit had published when the request was
    filed), and the record's passkey must be of that site too: a passkey of another site, even one active in the same
    authority log, approves nothing, and the approval still reads back with no cockpit running. Raises
    :class:`levain.dispatch.control.ConfigRefused` saying which check failed and ``not_done``; a passkey list or
    verifier that cannot be had is :class:`levain.dispatch.control.ControlStateUnavailable`. Writes nothing; the
    result files are read as the queue reads them (no link followed, regular files only, bounded)."""
    from levain.authority import VerifierUnavailable, verify_link_sig
    from levain.cockpit.consent import parse_stamp
    from levain.consent.passkey import PasskeyError, key_application
    from levain.consent.queue import class_covers
    from levain.consent.sign import SignRefused, _read_bytes, read_request, request_id_ok, sign_dir, state_of

    def refuse(why: str) -> dctl.ConfigRefused:
        return dctl.ConfigRefused(f"the phone approval is not usable: {why}; {not_done}")

    if not request_id_ok(request_id):
        raise refuse("no sign request was named")
    rid = str(request_id)
    qdir = Path(queue_dir)
    try:
        request = read_request(qdir, rid)
    except SignRefused:
        raise refuse(f"there is no sign request {rid}") from None
    if request.namespace != CONFIG_APPROVE_NAMESPACE or request.refused is not None or request.requester != "levain":
        raise refuse(f"sign request {rid} is not a configuration approval levain filed")
    fields = _parse_message(request.message)
    if fields is None:
        raise refuse(f"sign request {rid} is not in the approval grammar")
    state = state_of(qdir, rid)
    if state != "signed":
        raise refuse(f"sign request {rid} is {'denied on the phone' if state == 'denied' else 'not signed yet'}")
    try:
        signed_deadline = parse_stamp(fields["expires"])
    except Exception:  # noqa: BLE001 - the grammar fences the shape; a date that is not one is unusable
        raise refuse(f"sign request {rid} names no readable deadline") from None
    if request.expires_at is None:
        raise refuse(f"sign request {rid} has no expiry")
    deadline = min(signed_deadline, request.expires_at)
    moment = now or _now()
    if deadline <= moment:
        raise refuse(f"sign request {rid} expired")
    message = request.message.encode("utf-8")
    d = sign_dir(qdir)
    try:
        record = json.loads(_read_bytes(d / f"{rid}.signed.json"))
        signature = _read_bytes(d / f"{rid}.sig").decode("ascii")
    except (SignRefused, OSError, ValueError, RecursionError):
        raise refuse(f"the signature of sign request {rid} cannot be read") from None
    if (not isinstance(record, dict) or record.get("namespace") != CONFIG_APPROVE_NAMESPACE
            or record.get("message_sha256") != hashlib.sha256(message).hexdigest()):
        raise refuse(f"the record of sign request {rid} is not over its message")
    try:
        signed_at = parse_stamp(record.get("signed_at"))
    except Exception:  # noqa: BLE001 - an unreadable stamp is an unusable record
        raise refuse(f"sign request {rid} carries no readable signing time") from None
    if signed_at > deadline:
        raise refuse(f"sign request {rid} was signed after it expired")
    fp = record.get("signer_fp")
    rp_id = fields["rp"]
    try:
        record_rp = key_application(str(record.get("public_key")))
    except (PasskeyError, ValueError, IndexError):
        raise refuse(f"the record of sign request {rid} names no passkey") from None
    if record_rp != rp_id:
        raise refuse(f"it was signed by a passkey of {record_rp[:80]}, not of {rp_id}, the site the request was "
                     "filed for")
    try:
        active = keys(rp_id)
    except SignRefused as exc:
        raise dctl.ControlStateUnavailable(f"the phone passkeys cannot be read now: {exc}") from None
    key = active.get(fp) if isinstance(fp, str) else None
    if key is None:
        raise refuse(f"{str(fp)[:80]} is not an active passkey enrolled here")
    if not class_covers(key.key_class, APPROVE_TIER):
        raise refuse(f"a {key.key_class} passkey does not cover {APPROVE_TIER}")
    try:
        verified = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                   namespace=CONFIG_APPROVE_NAMESPACE)
    except VerifierUnavailable as exc:
        raise dctl.ControlStateUnavailable(f"the approval signature could not be verified now: {str(exc)[:200]}") \
            from None
    if not verified:
        raise refuse(f"the signature of sign request {rid} does not verify")
    try:
        generation = int(fields["generation"])
    except ValueError:
        raise refuse("its generation is not a number") from None
    return Approval(request_id=rid, store_id=fields["store"], kind=fields["kind"], generation=generation,
                    prev="" if fields["prev"] == "none" else fields["prev"], item_sha256=fields["item-sha256"],
                    signer_fp=str(fp))
