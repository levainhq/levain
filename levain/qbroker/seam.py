"""levain.qbroker.seam — THE VERIFY SEAM: whether an answer row is authority for a broker-filed request.

:func:`answer_refusal` is the one check every ``ANSWER?`` passes. Its answer check IS the consent queue's own:
:func:`levain.consent.queue.answer_refusal`, called with the broker's row's sealed request and queue id, and the
answer-signer snapshot as its ``authority`` (a :class:`levain.qbroker.signers.AllowedSigner` reads as an authority
key: ``active``, ``key_class``, ``custody``, ``public_key``, ``fingerprint``; CONTRACT c7 §3.7). What this module adds
around that call, and nothing else:

- the snapshot: a missing or unverifiable one makes no answer authority.
- the row: its stored request must be the seal of its fields, and its hold must not have ended.
- namespaces: a snapshot signer lists the namespaces it may sign in; one that may not sign in
  ``levain-consent-answer`` is refused here, before the shared check (which has no such step).

The receipts head: a broker request has no receipt log, so the head is checked for its shape only (what the
shared check does; the apply-time ``head_refusal`` has no log to read here). The unchained answer format is never
accepted (``unchained_ok`` stays off).

Stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent import queue as consent_queue
from levain.consent.record import ANSWER_NAMESPACE, ExternalGateRequest
from levain.qbroker.signers import SignersSnapshot

__all__ = ["answer_refusal", "request_of"]

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
    if isinstance(payload, Mapping):
        payload = dict(payload)         # one copy: the namespace check and the shared check read the same record
        signer = signers.key(payload.get("signer_fp"))
        if signer is not None and ANSWER_NAMESPACE not in signer.namespaces:
            return f"the signer may not sign in {ANSWER_NAMESPACE}"
    return consent_queue.answer_refusal(request, row.get("queue_id"), payload, signature, signers,
                                        ssh_keygen=ssh_keygen)
