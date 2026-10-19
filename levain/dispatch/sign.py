"""levain.dispatch.sign — the operator's one signature over a filed contract's digest.

The human signs a DIGEST, never an editable file: the payload names the job, the contract digest it was shown, the
contract's expiry, the signer and when it was signed, and its canonical bytes are signed in this module's own SSHSIG
namespace, so a dispatch signature verifies as nothing else and nothing else verifies as one.

:func:`refusal` is the one check: exact keys and format, the job's id, digest and expiry, a ``signed_at`` inside the
job's unsigned lease, then the signer checks in the order the consent queue makes them for an allow (an enrolled key,
not revoked, of a class that covers T2, a non-empty signature, a passkey's extra checks, the signature over the
payload's bytes). Signing a contract is an allow at T2: a Face ID passkey is a display-phone key, which covers T2 and
never T3, so the phone can sign a dispatch digest. A verifier that cannot run is a refusal, never a pass. Stdlib only.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from levain.authority import PASSKEY, AuthoritySnapshot, VerifierUnavailable, verify_link_sig
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import class_covers
from levain.contracts.canon import canonical_bytes

__all__ = ["DISPATCH_SIGN_NAMESPACE", "FORMAT", "PAYLOAD_KEYS", "SIGN_TIER", "refusal", "sign_message",
           "sign_payload"]

DISPATCH_SIGN_NAMESPACE = "levain-dispatch-contract"
FORMAT = "levain-dispatch-sign/1"
PAYLOAD_KEYS = frozenset({"format", "job_id", "digest", "signer_fp", "signed_at", "expires_at"})
# the tier a contract signature allows at: the class a key needs to sign one
SIGN_TIER = "T2"


class SignableJob(Protocol):
    """What a signature is checked against: the filed job (``levain.dispatch.jobs.Job`` is one)."""
    id: str
    digest: str
    expires_at: str
    filed_at: str

    @property
    def unsigned_lease_end(self) -> str: ...


def sign_payload(job: SignableJob, *, signer_fp: str, signed_at: datetime) -> dict[str, str]:
    """The payload a signer signs for ``job``: its bytes are :func:`sign_message`'s."""
    return {"format": FORMAT, "job_id": job.id, "digest": job.digest, "signer_fp": signer_fp,
            "signed_at": utc_stamp(signed_at), "expires_at": job.expires_at}


def sign_message(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes signed in :data:`DISPATCH_SIGN_NAMESPACE`: the payload's canonical JSON."""
    return canonical_bytes(dict(payload))


def refusal(payload: Any, signature: Any, authority: AuthoritySnapshot, job: SignableJob, *,
            ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``payload``/``signature`` is not the operator's signature over ``job`` under ``authority``; ``None``
    when it verifies."""
    if not isinstance(payload, Mapping) or set(payload) != PAYLOAD_KEYS:
        return "the signature payload is not a dispatch signature record"
    if not all(isinstance(payload[k], str) for k in PAYLOAD_KEYS):
        return "every field of a dispatch signature record is text"
    if payload["format"] != FORMAT:
        return f"signature format {payload['format']!r} is not {FORMAT!r}"
    if payload["job_id"] != job.id:
        return "the signature is for another job"
    if payload["digest"] != job.digest:
        return "the signature is over another contract digest"
    if payload["expires_at"] != job.expires_at:
        return "the signature names another expiry"
    signed_at = payload["signed_at"]
    try:
        parse_stamp(signed_at)
    except ConsentError:
        return "the signature's signed_at is not a timestamp"
    if signed_at < job.filed_at:
        return "the signature was signed before the contract was filed"
    if signed_at >= job.unsigned_lease_end:
        return "the signature was signed after the job's lease ended"
    if not isinstance(authority, AuthoritySnapshot):
        return "no authority snapshot to verify the signer against"
    signer_fp = payload["signer_fp"]
    key = authority.key(signer_fp)
    if key is None:
        return "the signer is not an enrolled key"
    if not key.active:
        return "the signer's key is revoked"
    if not class_covers(key.key_class, SIGN_TIER):
        return f"key class {key.key_class} does not cover {SIGN_TIER}"
    if not isinstance(signature, str) or not signature:
        return "the contract signature is empty"
    if key.custody == PASSKEY:
        # a passkey's WebAuthn checks that ssh-keygen does not make (user verification, the page's origin), reused
        # from the consent path; its refusal wording says "answer"
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, key.public_key)
        if problem is not None:
            return problem
    try:
        message = sign_message(payload)
    except (TypeError, ValueError) as exc:
        return f"the signature payload cannot be encoded: {exc}"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    try:
        ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                             namespace=DISPATCH_SIGN_NAMESPACE, **kwargs)
    except VerifierUnavailable as exc:
        return f"the contract signature could not be verified: {exc}"
    return None if ok else "the contract signature does not verify"
