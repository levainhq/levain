"""levain.autonomic.confirm — who answered a confirm: a signature, never a delivered "yes".

The rule (Phill, 2026-10-08): "a delivered 'yes' is never authority; authority = a signature by an
authenticator the operator's uid cannot hold, over content shown on a display the requester cannot write."

A person's approval of a pending action is authority only when it carries an SSH signature (SSHSIG, in
the :data:`NAMESPACE` namespace) by a key named in the gate's allowed-signers file, over the challenge
:func:`challenge` builds: the pending's id, a hash of its whole sealed record, the risk-catalog revision the
gate decides under and the rung the approval must meet. Anything else (no signature, another key, another
namespace, a signature over an earlier revision or a lower rung) is not authority, and the decision stays
open. A "yes" that arrives over any channel is a request to be checked, not an answer.

Verification is ``ssh-keygen -Y verify`` against the allowed-signers file. Stdlib only.

What this module cannot check, and must be held elsewhere:
  - that an enrolled key lives in hardware the operator's uid cannot export (a Secure Enclave key): the
    file names public keys, and a software key in it verifies exactly as well;
  - that the allowed-signers file itself cannot be written by the requester: it is the trust root. Kept
    inside the autonomic store directory, the confinement floor denies it to an entity's hands; the
    operator's own uid can still write it;
  - that the device showing the content to sign cannot be written by the requester.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from levain.autonomic.pending import PendingAction

__all__ = ["NAMESPACE", "challenge", "verify_signature"]

NAMESPACE = "levain-confirm"
CHALLENGE_FORMAT = "levain-confirm/1"
_SIGNER = re.compile(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]{0,127}")
_MAX_SIGNATURE = 16384
_BEGIN = "-----BEGIN SSH SIGNATURE-----"


def challenge(*, pending: PendingAction, risk_revision: int | None, rung: str) -> bytes:
    """The exact bytes an enrolled key signs to approve ``pending`` at ``rung`` under ``risk_revision``
    (``None`` for a manual pending, which no journal fences). Canonical JSON, so the signer and the gate
    build the same bytes."""
    record = json.dumps(pending.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body = {
        "format": CHALLENGE_FORMAT, "approve": True, "pending_id": pending.pending_id,
        "content_sha256": hashlib.sha256(record.encode("utf-8")).hexdigest(),
        "risk_revision": risk_revision, "rung": rung,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_signature(message: bytes, *, signature: str | None, signer: str | None,
                     allowed_signers: Path | None, timeout: float = 10.0) -> bool:
    """True only if ``signature`` is a valid SSHSIG over ``message`` in :data:`NAMESPACE` by the key the
    allowed-signers file names for ``signer``. Every failure, including one of this machine (no
    ``ssh-keygen``, a timeout, an unreadable file), is ``False``: a check that could not run grants
    nothing."""
    if allowed_signers is None or not isinstance(signature, str) or not isinstance(signer, str):
        return False
    if not _SIGNER.fullmatch(signer) or len(signature) > _MAX_SIGNATURE or _BEGIN not in signature:
        return False
    if not Path(allowed_signers).is_file():
        return False
    fd, sig_path = tempfile.mkstemp(prefix="levain-confirm-", suffix=".sig")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(signature.strip() + "\n")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never"}
        cp = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", signer, "-n", NAMESPACE, "-s", sig_path],
            input=message, capture_output=True, timeout=timeout, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeEncodeError):
        return False
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass
    if cp.returncode != 0:
        return False
    good = f'Good "{NAMESPACE}" signature for {signer} with '
    out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
    return any(line.strip().startswith(good) for line in out.splitlines())
