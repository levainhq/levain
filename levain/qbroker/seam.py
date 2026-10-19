"""levain.qbroker.seam — THE VERIFY SEAM: whether an answer row is authority for a broker-filed request.

:func:`answer_refusal` is the one check every ``ANSWER?`` passes. It must be the consent queue's own answer check
(``ConsentQueue._refusal`` in ``levain/consent/queue.py``) applied to the broker's row. That check is not
reachable as a pure function today: it is a private method of a queue bound to its cockpit authority log, and its
signer step reads an :class:`levain.authority.AuthoritySnapshot` key (``active``, ``key_class``, ``custody``) that a
``levain-allowed-signers/1`` entry does not carry. So this module composes the
smallest pure pieces that check itself calls (the sealed request and its digest, ``answer_message``,
``head_problem``, ``parse_stamp``, ``class_covers``, the passkey check, ``verify_link_sig``), and
the field comparisons between them restate that check's one for one (they cannot be called apart from it).
The differences:

- key class: an allowed signer has no class, so every signer is held to :data:`SIGNER_CLASS`, the lowest class
  that may allow at all (a phone key's). An allow at a tier that class does not cover (T3) is refused.
- custody: an allowed signer declares none, so an ``sk-ecdsa`` key is held to the passkey checks
  (:func:`levain.consent.passkey.answer_signature_problem`), so an sk-ecdsa key that is not a passkey is refused;
  an sk-ed25519 key gets only the presence check ``verify_link_sig`` makes.
- receipts head: a broker request has no receipt log, so the head is checked for its shape only (what the queue's
  own check does; the apply-time ``head_refusal`` has no log to read here).

Stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.authority import DISPLAY_PHONE, VerifierUnavailable, verify_link_sig
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import _ANSWER_KEYS, class_covers
from levain.consent.receipt_log import head_problem
from levain.consent.record import ANSWER_FORMAT, ANSWER_NAMESPACE, DECISIONS, ExternalGateRequest, answer_message
from levain.qbroker.signers import SignersSnapshot

__all__ = ["SIGNER_CLASS", "answer_refusal", "request_of"]

SIGNER_CLASS = DISPLAY_PHONE
_REQUEST_KEYS = frozenset(ExternalGateRequest.__dataclass_fields__)


def request_of(fields: Any) -> ExternalGateRequest | None:
    """The request a row holds, only when its stored id is the seal of its fields."""
    if not isinstance(fields, Mapping) or set(fields) != _REQUEST_KEYS:
        return None
    try:
        request = ExternalGateRequest(**{k: fields[k] for k in _REQUEST_KEYS})
        return request if request.seal_matches() else None
    except (ConsentError, TypeError, ValueError):
        return None


# SEAM GAP (routed to B2): replace this body with one call to a pure consent-side check, e.g.
# levain.consent.queue.answer_refusal(request, queue_id, payload, signature, authority, *, ssh_keygen=None), whose
# ``authority`` is any object with key(fp) -> {public_key, fingerprint, active, key_class, custody}; the signers
# snapshot then needs key_class + custody per signer to stand in for it.
def answer_refusal(row: Mapping[str, Any], payload: Any, signature: Any, signers: SignersSnapshot | None,
                   now: datetime, *, ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``payload``/``signature`` is not authority for the request ``row`` holds; ``None`` when it is."""
    if signers is None:
        return "the answer-signer snapshot is missing or unverifiable: no answer is authority"
    request = request_of(row.get("request"))
    if request is None:
        return "the request row's seal does not match its fields"
    if request.expires_at <= utc_stamp(now):
        return f"the request's hold ended {request.expires_at}"
    if not isinstance(payload, Mapping) or set(payload) != _ANSWER_KEYS:
        return "the answer is not an answer record"
    if payload["format"] != ANSWER_FORMAT:
        return f"answer format {payload['format']!r} is not {ANSWER_FORMAT!r}"
    problem = head_problem(payload["receipts_head"])
    if problem is not None:
        return f"the answer's {problem}"
    if payload["queue_id"] != row.get("queue_id"):
        return "the answer names another queue"
    if payload["request_id"] != request.id:
        return "the answer is for another request"
    if payload["request_digest"] != request.digest:
        return "the answer signs another version of this request"
    if payload["expires_at"] != request.expires_at:
        return "the answer names another expiry"
    signed_at = payload["signed_at"]
    try:
        parse_stamp(signed_at)
    except ConsentError:
        return "the answer's signed_at is not a timestamp"
    if signed_at < request.received_at:
        return "the answer was signed before the request was filed"
    if signed_at >= request.expires_at:
        return "the answer was signed after the request expired"
    decision = payload["decision"]
    if decision not in DECISIONS:
        return f"decision {decision!r} is not one of {DECISIONS}"
    signer = signers.key(payload["signer_fp"])
    if signer is None:
        return "the signer is not in the answer-signer snapshot"
    if ANSWER_NAMESPACE not in signer.namespaces:
        return f"the signer may not sign in {ANSWER_NAMESPACE}"
    if decision == "allow" and not class_covers(SIGNER_CLASS, request.tier):
        return f"an allowed signer (held to class {SIGNER_CLASS}) does not cover {request.tier}"
    if not isinstance(signature, str) or not signature:
        return "the answer carries no signature"
    if signer.public_key.startswith("sk-ecdsa"):
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, signer.public_key)
        if problem is not None:
            return problem
    try:
        message = answer_message(dict(payload))
    except ConsentError as exc:
        return f"the answer cannot be encoded: {exc}"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        ok = verify_link_sig(message, signature, public_key=signer.public_key, fingerprint=signer.fingerprint,
                             namespace=ANSWER_NAMESPACE, **kwargs)
    except VerifierUnavailable as exc:
        return f"the answer could not be verified: {exc}"
    return None if ok else "the answer's signature does not verify"
