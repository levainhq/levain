"""levain.dispatch.sign — the operator's one signature over a filed contract's digest.

The human signs a DIGEST, never an editable file: the payload names the store and the job, the contract digest it was
shown, the contract's expiry, the signer and when it was signed, and its canonical bytes are signed in this module's
own SSHSIG namespace, so a dispatch signature verifies as nothing else and nothing else verifies as one. The store's
random ``store_id`` is bound so a signature made for a job in one dispatch store is refused by any other.

:func:`refusal` is the one check: exact keys and format, every field and the signature bounded in length
(:data:`FIELD_MAX`, :data:`SIGNATURE_MAX`) before anything is verified, the store, the job's id, digest and expiry,
a ``signed_at`` inside the job's unsigned lease, then the signer checks (an enrolled key, not revoked, of a class
that covers T2, a non-empty signature, a passkey's extra checks, the signature over the payload's bytes), in an
order meant to mirror the consent queue's signer check for an allow; this module's tests pin the order here. A verifier that cannot run is
never a pass and never a refusal either: it is raised as :class:`levain.authority.VerifierUnavailable`, so a caller
tells "this machine cannot check a signature right now" from "this is not the operator's signature".

WHY T2. Signing a contract is an allow at T2, so a Face ID passkey (a display-phone key, which covers T2 and never T3)
can sign a dispatch digest from the phone. The intent that makes T2 the right tier: the digest's grant (Write, Edit,
apply_patch over the exact write set, for at most a day) reaches only a throwaway worktree built from the contract's
read set, and confers nothing on the operator's own tree. Adoption into his tree needs its own human signature, which
the adoption slice is to hold to the highest consent tier of the paths it writes, so a path whose consent tier is T3
is never adopted on a phone signature. Should a contract's grant ever reach the operator's tree directly,
T2 stops being the right tier. Untested: no test yet produces a VALID passkey (WebAuthn) signature over a dispatch
payload; the passkey path is exercised only by its refusals.

THE HUMAN CANCEL. A cancel is the operator's own signed item with its own payload (:func:`cancel_payload`, format
:data:`CANCEL_FORMAT`) in its own namespace (:data:`DISPATCH_CANCEL_NAMESPACE`), so a contract signature can never be
presented as a cancel nor a cancel as a contract signature. It binds the store, the job, the digest the operator was
shown, the signer, when it was signed and a one-line reason (which may be empty). It is tied to no expiry and no lease
window: it is good from the job's filing until the job is terminal, and what makes it single-use is that the cancel
it carries is terminal. :func:`cancel_refusal` checks it in :func:`refusal`'s order, at the same tier (removing
authority never needs more than granting it did), and a verifier that cannot run is raised there as here. Stdlib
only.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from levain.authority import PASSKEY, AuthoritySnapshot, verify_link_sig
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.consent.queue import class_covers
from levain.contracts.canon import canonical_bytes

__all__ = ["CANCEL_FORMAT", "CANCEL_KEYS", "DISPATCH_CANCEL_NAMESPACE", "DISPATCH_SIGN_NAMESPACE", "FIELD_MAX", "FORMAT",
           "PAYLOAD_KEYS", "SIGNATURE_MAX", "SIGN_TIER", "cancel_payload", "cancel_refusal", "reason_problem", "refusal",
           "sign_message", "sign_payload"]

DISPATCH_SIGN_NAMESPACE = "levain-dispatch-contract"
FORMAT = "levain-dispatch-sign/1"
PAYLOAD_KEYS = frozenset({"format", "store_id", "job_id", "digest", "signer_fp", "signed_at", "expires_at"})
DISPATCH_CANCEL_NAMESPACE = "levain-dispatch-cancel"
CANCEL_FORMAT = "levain-dispatch-cancel/1"
CANCEL_KEYS = frozenset({"format", "store_id", "job_id", "digest", "signer_fp", "signed_at", "reason"})
# the tier a contract signature allows at: the class a key needs to sign one
SIGN_TIER = "T2"
# bounds checked before anything is verified or stored: every real payload field is under 100 characters and a real
# signature, SSH or a passkey's armored SSHSIG, under a kilobyte; the signature bound leaves room for the browser
# fields a passkey signature carries, each of which the passkey check holds to 16 KiB
FIELD_MAX = 256
SIGNATURE_MAX = 64 * 1024


class SignableJob(Protocol):
    """What a signature is checked against: the filed job (``levain.dispatch.jobs.Job`` is one) and its store."""
    @property
    def store_id(self) -> str: ...

    @property
    def id(self) -> str: ...

    @property
    def digest(self) -> str: ...

    @property
    def expires_at(self) -> str: ...

    @property
    def filed_at(self) -> str: ...

    @property
    def unsigned_lease_end(self) -> str: ...


def sign_payload(job: SignableJob, *, signer_fp: str, signed_at: datetime) -> dict[str, str]:
    """The payload a signer signs for ``job``: its bytes are :func:`sign_message`'s."""
    return {"format": FORMAT, "store_id": job.store_id, "job_id": job.id, "digest": job.digest, "signer_fp": signer_fp,
            "signed_at": utc_stamp(signed_at), "expires_at": job.expires_at}


def sign_message(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes signed in :data:`DISPATCH_SIGN_NAMESPACE`: the payload's canonical JSON."""
    return canonical_bytes(dict(payload))


def cancel_payload(job: SignableJob, *, signer_fp: str, signed_at: datetime, reason: str = "") -> dict[str, str]:
    """The payload a human signs to cancel ``job``: its bytes are :func:`sign_message`'s, signed in
    :data:`DISPATCH_CANCEL_NAMESPACE`."""
    return {"format": CANCEL_FORMAT, "store_id": job.store_id, "job_id": job.id, "digest": job.digest,
            "signer_fp": signer_fp, "signed_at": utc_stamp(signed_at), "reason": reason}


def reason_problem(reason: Any) -> str | None:
    """Why ``reason`` is not a cancel's reason (one line of plain text, at most :data:`FIELD_MAX` characters, which
    may be empty), or ``None``."""
    if not isinstance(reason, str):
        return "a cancel's reason is text"
    if len(reason) > FIELD_MAX:
        return f"a cancel's reason is at most {FIELD_MAX} characters"
    if any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp") for c in reason):
        return "a cancel's reason is one line of plain text"
    return None


def refusal(payload: Any, signature: Any, authority: AuthoritySnapshot, job: SignableJob, *,
            ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``payload``/``signature`` is not the operator's signature over ``job`` under ``authority``; ``None``
    when it verifies. Raises :class:`levain.authority.VerifierUnavailable` when the verifier cannot run."""
    if not isinstance(payload, Mapping) or set(payload) != PAYLOAD_KEYS:
        return "the signature payload is not a dispatch signature record"
    if not all(isinstance(payload[k], str) for k in PAYLOAD_KEYS):
        return "every field of a dispatch signature record is text"
    if any(len(payload[k]) > FIELD_MAX for k in PAYLOAD_KEYS):
        return f"a field of the dispatch signature record is over {FIELD_MAX} characters"
    if payload["format"] != FORMAT:
        return f"signature format {payload['format']!r} is not {FORMAT!r}"
    if payload["store_id"] != job.store_id:
        return "the signature is for another dispatch store"
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
    if len(signature) > SIGNATURE_MAX:
        return f"the contract signature is over {SIGNATURE_MAX} characters"
    if key.custody == PASSKEY:
        # a passkey's WebAuthn checks that ssh-keygen does not make (user verification, the page's origin): the
        # consent path's passkey check is called here so the two paths hold a passkey to one rule
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, key.public_key)
        if problem is not None:
            return problem
    try:
        message = sign_message(payload)
    except (TypeError, ValueError) as exc:
        return f"the signature payload cannot be encoded: {exc}"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    # a VerifierUnavailable propagates: a fault on this machine is not a verdict on the signature
    ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                         namespace=DISPATCH_SIGN_NAMESPACE, **kwargs)
    return None if ok else "the contract signature does not verify"


def cancel_refusal(payload: Any, signature: Any, authority: AuthoritySnapshot, job: SignableJob, *,
                   ssh_keygen: Path | str | None = None) -> str | None:
    """Why ``payload``/``signature`` is not the operator's cancel of ``job`` under ``authority``; ``None`` when it
    verifies. :func:`refusal`'s order, with the reason in place of the expiry and no lease window; raises
    :class:`levain.authority.VerifierUnavailable` when the verifier cannot run."""
    if not isinstance(payload, Mapping) or set(payload) != CANCEL_KEYS:
        return "the cancel payload is not a dispatch cancel record"
    if not all(isinstance(payload[k], str) for k in CANCEL_KEYS):
        return "every field of a dispatch cancel record is text"
    if any(len(payload[k]) > FIELD_MAX for k in CANCEL_KEYS):
        return f"a field of the dispatch cancel record is over {FIELD_MAX} characters"
    if payload["format"] != CANCEL_FORMAT:
        return f"cancel format {payload['format']!r} is not {CANCEL_FORMAT!r}"
    problem = reason_problem(payload["reason"])
    if problem is not None:
        return problem
    if payload["store_id"] != job.store_id:
        return "the cancel is for another dispatch store"
    if payload["job_id"] != job.id:
        return "the cancel is for another job"
    if payload["digest"] != job.digest:
        return "the cancel is over another contract digest"
    signed_at = payload["signed_at"]
    try:
        parse_stamp(signed_at)
    except ConsentError:
        return "the cancel's signed_at is not a timestamp"
    if signed_at < job.filed_at:
        return "the cancel was signed before the contract was filed"
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
        return "the cancel signature is empty"
    if len(signature) > SIGNATURE_MAX:
        return f"the cancel signature is over {SIGNATURE_MAX} characters"
    if key.custody == PASSKEY:
        from levain.consent.passkey import answer_signature_problem

        problem = answer_signature_problem(signature, key.public_key)
        if problem is not None:
            return problem
    try:
        message = sign_message(payload)
    except (TypeError, ValueError) as exc:
        return f"the cancel payload cannot be encoded: {exc}"
    kwargs: dict[str, Any] = {} if ssh_keygen is None else {"ssh_keygen": ssh_keygen}
    ok = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                         namespace=DISPATCH_CANCEL_NAMESPACE, **kwargs)
    return None if ok else "the cancel signature does not verify"
