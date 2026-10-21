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

THE GRANT ROW. :func:`grant_allow_refusal` is the one check made of the broker's own ``levain-queue-grant-allow/1``
row (:mod:`levain.qbroker.grants`), by ``ANSWER?``, by the filing's receipts and by the start-time reconcile (that one
without the hold-time check): its exact keys, and that it names this request and this run's contract digest. It is read only from the directory the broker alone writes, and only as that format; an
answer row, and a payload in either grant format, is never read as the other (:func:`answer_refusal` refuses a grant
format by name before the shared check). The row signs no receipts head: its receipt in the broker's log names it
as the authority (:meth:`levain.qbroker.receipts.Receipts.granted`).

The receipts head: the shared check reads its shape; a head of size 0 is refused here (it attests nothing: the
empty tree's root is fixed). Whether it is a head of the broker's own receipts log is the apply-time
``head_refusal``, run inside the write that records the decision (:meth:`levain.qbroker.receipts.Receipts.decided`).
The unchained answer format is never accepted (``unchained_ok`` stays off).

Stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent import queue as consent_queue
from levain.consent.record import ANSWER_NAMESPACE, GRANT_ANSWER_FORMAT, ExternalGateRequest
from levain.qbroker.signers import SignersSnapshot
from levain.qbroker.store import GRANT_ROW_FORMAT

__all__ = ["GRANT_ROW_KEYS", "answer_refusal", "grant_allow_refusal", "request_of"]

GRANT_ROW_KEYS = frozenset({"format", "request_id", "contract_digest", "paths", "signer_fp", "at"})
# formats that are some other authority's: never read as a queue answer
_NOT_ANSWERS = frozenset({GRANT_ROW_FORMAT, GRANT_ANSWER_FORMAT})

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
    """Why ``payload``/``signature`` is not authority for the request ``row`` holds; ``None`` when it is. The hold is
    the answer's own ``signed_at`` before the request's ``expires_at`` (:func:`levain.consent.queue.answer_refusal`),
    never the time it is read: an answer signed in time is authority when read after the hold ended. ``now`` is not
    read (kept so every refusal here takes one signature)."""
    if signers is None:
        return "the answer-signer snapshot is missing or unverifiable: no answer is authority"
    request = request_of(row.get("request"))
    if request is None:
        return "the request row's seal does not match its fields"
    if isinstance(payload, Mapping) and payload.get("format") in _NOT_ANSWERS:
        return f"a {payload.get('format')} record is not a queue answer"
    if isinstance(payload, Mapping):
        payload = dict(payload)         # one copy: the namespace check and the shared check read the same record
        signer = signers.key(payload.get("signer_fp"))
        if signer is not None and ANSWER_NAMESPACE not in signer.namespaces:
            return f"the signer may not sign in {ANSWER_NAMESPACE}"
    why = consent_queue.answer_refusal(request, row.get("queue_id"), payload, signature, signers,
                                       ssh_keygen=ssh_keygen)
    if why is None and payload["receipts_head"]["size"] == 0:
        return "the answer's receipts head is of size 0: it attests nothing"
    return why


def grant_allow_refusal(row: Mapping[str, Any], grant: Any, now: datetime, *, check_hold: bool = True) -> str | None:
    """Why ``grant`` is not the broker's allow for the request ``row`` holds; ``None`` when it is. The one judge of a
    grant row: ANSWER?, the filing's receipts, the replay repair and the start-time reconcile all ask it. ``check_hold=False``
    (the start-time reconcile and the replay repair) skips the hold-time check, so a row whose hold ended before the broker
    restarted or the repair ran is judged on the
    rest; ``now`` is then not read."""
    if not isinstance(grant, Mapping) or set(grant) != GRANT_ROW_KEYS or grant.get("format") != GRANT_ROW_FORMAT:
        return f"the grant row is not a {GRANT_ROW_FORMAT} record"
    request = request_of(row.get("request"))
    if request is None:
        return "the request row's seal does not match its fields"
    if check_hold and request.expires_at <= utc_stamp(now):
        return f"the request's hold ended {request.expires_at}"
    if grant["request_id"] != request.id:
        return "the grant row names another request"
    run = row.get("run")
    if row.get("kind") != "dispatch" or not isinstance(run, Mapping) \
            or grant["contract_digest"] != run.get("contract_digest"):
        return "the grant row names another contract than the request's run"
    paths = grant["paths"]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths) \
            or not isinstance(grant["signer_fp"], str) or not isinstance(grant["at"], str):
        return "the grant row's paths, signer and time are not text"
    return None
