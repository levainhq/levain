"""Board row 113 (option F2): a dispatch configuration item approved on the operator's PHONE, signed by the LAPTOP key.

The consent sheet signs only with a phone passkey, and the control store's trust root can never be a passkey
(:func:`levain.dispatch.control._root_problem`), so the phone cannot BE the configuration signature. It is a gate in
front of it (the countersign pattern, as terminal answers): ``config request`` files levain's own plain sign request in
:data:`CONFIG_APPROVE_NAMESPACE` whose fixed message names the store, kind, generation, prev and the SHA-256 of the
item's exact canonical bytes; the phone passkey signs it; ``config accept-approved`` verifies that approval, loads the
pending item whose bytes hash to it, and only then asks the trust-root laptop key to sign those bytes (a Touch ID touch,
no terminal question) and hands them to the unchanged :meth:`ControlStore.accept`. Nothing here widens what the store
accepts: the trust root, its custody rule and ``accept`` are untouched, so an approval alone writes nothing and no
laptop signature is ever made without a verified approval of those bytes.

What the phone is shown is levain's own one-line ``change`` and ``summary`` beside the item's hash; the full diff is
printed at the laptop before the touch. A signature in this namespace therefore binds the hash, and the words are the
operator's reading aid, not part of what is verified. The approval class floor is T2 (a ``display-phone`` passkey,
what a thread envelope and a signed review clear need): the T3 authority over configuration stays the laptop key's.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from levain.contracts.namespaces import CONFIG_APPROVE_NAMESPACE, LEVAIN_SIGN_REQUESTS
from levain.dispatch import control as dctl

__all__ = ["APPROVE_TIER", "Approval", "FORMAT_LINE", "PENDING_DIR", "REQUEST_TITLE", "REQUEST_TTL_S", "build_message",
           "file_request", "load_pending", "push_request", "read_approval", "remove_pending"]

FORMAT_LINE = "levain-dispatch-config-approve/1"
REQUEST_TITLE = "Approve a dispatch configuration change"
REQUEST_TTL_S = 3600
APPROVE_TIER = "T2"
PENDING_DIR = "config-pending"
_LINE_MAX = 300
_SHA = re.compile(r"[0-9a-f]{64}")


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


def build_message(item: dctl.ConfigItem, raw: bytes, *, change: str, summary: str) -> str:
    """The approval request's message for ``item`` (parsed from ``raw``, its exact canonical bytes)."""
    if dctl.parse_item(raw).text != item.text:
        raise ValueError("raw is not the item's bytes")
    msg = (f"{FORMAT_LINE}\nstore: {_one_line(item.store_id)}\nkind: {item.kind}\ngeneration: {item.generation}\n"
           f"prev: {_one_line(item.prev) if item.prev else 'none'}\nitem-sha256: {hashlib.sha256(raw).hexdigest()}\n"
           f"change: {_one_line(change)}\nsummary: {_one_line(summary)}\n")
    return msg


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
    d = _pending_dir(directory)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / f"{sha}.item"
    tmp = d / f".{sha}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_pending(directory: Path, sha: str) -> bytes:
    """The pending item whose bytes hash to ``sha``, or :class:`levain.dispatch.control.ConfigRefused`. The file is a
    regular file read without following a link; its bytes are re-hashed, so a swapped file is refused."""
    if _SHA.fullmatch(sha or "") is None:
        raise dctl.ConfigRefused("the approval names no item hash")
    path = _pending_dir(directory) / f"{sha}.item"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise dctl.ConfigRefused(f"the item this approval is over is not kept here ({path}): the request was filed "
                                 "elsewhere or its item is gone; file a new request") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise dctl.ConfigRefused("the kept item is not a regular file")
        raw = fh.read(dctl.ITEM_MAX_BYTES + 1)
    if len(raw) > dctl.ITEM_MAX_BYTES or hashlib.sha256(raw).hexdigest() != sha:
        raise dctl.ConfigRefused("the kept item is not the one the approval is over")
    return raw


def remove_pending(directory: Path, sha: str) -> None:
    try:
        os.unlink(_pending_dir(directory) / f"{sha}.item")
    except OSError:
        pass


def file_request(directory: Path, queue_dir: Path, raw: bytes, *, change: str, summary: str) -> str:
    """Keep ``raw`` (an item's canonical bytes) and file levain's approval request over it in ``queue_dir``'s sign
    directory; return the request id. :class:`ValueError` for a message outside the grammar (nothing is filed)."""
    from levain.consent.sign import file_levain_request

    item = dctl.parse_item(raw)
    message = build_message(item, raw, change=change, summary=summary)
    _write_pending(directory, hashlib.sha256(raw).hexdigest(), raw)
    return file_levain_request(Path(queue_dir), CONFIG_APPROVE_NAMESPACE, message, title=REQUEST_TITLE,
                               ttl_s=REQUEST_TTL_S)


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
        if not sender.spawn(Path(queue_dir), kind="question", ref=rid, title=REQUEST_TITLE,
                            body="A dispatch configuration change waits for your approval.",
                            url=deep_link(origin, rid)):
            return "the push could not be started"
        return None
    except Exception as exc:  # noqa: BLE001 - a push is a convenience; the request stands
        return f"the push failed ({type(exc).__name__})"


# --- reading the approval back ----------------------------------------------------------------------------------------
def read_approval(queue_dir: Path, request_id: object, *, keys: Callable[[], Mapping[str, Any]],
                  now: datetime | None = None) -> Approval:
    """The verified approval in sign request ``request_id``: levain's own request in :data:`CONFIG_APPROVE_NAMESPACE`
    in exactly its grammar, signed (not open, not denied) before it expired, whose signature verifies over its message
    in that namespace by an active passkey ``keys()`` names whose class covers :data:`APPROVE_TIER`. Raises
    :class:`levain.dispatch.control.ConfigRefused` saying which check failed; a passkey list or verifier that cannot be
    had is :class:`levain.dispatch.control.ControlStateUnavailable`. Writes nothing."""
    from levain.authority import VerifierUnavailable, verify_link_sig
    from levain.cockpit.consent import parse_stamp
    from levain.consent.queue import class_covers
    from levain.consent.sign import SignRefused, read_request, request_id_ok, sign_dir, state_of

    def refuse(why: str) -> dctl.ConfigRefused:
        return dctl.ConfigRefused(f"the phone approval is not usable: {why}; nothing was signed")

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
    moment = now or datetime.now(timezone.utc)
    if request.expires_at is None or request.expires_at <= moment:
        raise refuse(f"sign request {rid} expired")
    message = request.message.encode("utf-8")
    d = sign_dir(qdir)
    try:
        record = json.loads((d / f"{rid}.signed.json").read_text(encoding="utf-8"))
        signature = (d / f"{rid}.sig").read_text(encoding="ascii")
    except (OSError, ValueError):
        raise refuse(f"the signature of sign request {rid} cannot be read") from None
    if (not isinstance(record, dict) or record.get("namespace") != CONFIG_APPROVE_NAMESPACE
            or record.get("message_sha256") != hashlib.sha256(message).hexdigest()):
        raise refuse(f"the record of sign request {rid} is not over its message")
    try:
        signed_at = parse_stamp(record.get("signed_at"))
    except Exception:  # noqa: BLE001 - an unreadable stamp is an unusable record
        raise refuse(f"sign request {rid} carries no readable signing time") from None
    if signed_at > request.expires_at:
        raise refuse(f"sign request {rid} was signed after it expired")
    try:
        active = keys()
    except SignRefused as exc:
        raise dctl.ControlStateUnavailable(f"the phone passkeys cannot be read now: {exc}") from None
    fp = record.get("signer_fp")
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
