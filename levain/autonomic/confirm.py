"""levain.autonomic.confirm — who answered a confirm: a signature, never a delivered "yes".

The rule (Phill, 2026-10-08): "a delivered 'yes' is never authority; authority = a signature by an
authenticator the operator's uid cannot hold, over content shown on a display the requester cannot write."

A person's approval of a pending action is authority only when it carries an SSH signature (SSHSIG, in
the :data:`NAMESPACE` namespace) by a key named in the gate's allowed-signers file, over the challenge
:func:`challenge` builds: the store it is decided in, the pending's id and its hold's id, a hash of its whole
sealed record, the risk fence (a digest of the risk inputs the gate classifies the action with) and the rung
the approval must meet. Anything else (no signature, another key, another namespace, another store or
decision, a signature under another fence or at a lower rung) is not authority, and the decision stays
open. A "yes" that arrives over any channel is a request to be checked, not an answer.

Verification is ``ssh-keygen -Y verify`` against the allowed-signers file, run from an absolute path
(:data:`DEFAULT_SSH_KEYGEN` unless the gate is configured with another), never found through ``PATH``, and
only if that binary is a regular file owned by root and writable by nobody else. Stdlib only.

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
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from levain.autonomic.pending import PendingAction

__all__ = ["NAMESPACE", "DEFAULT_SSH_KEYGEN", "challenge", "ssh_keygen_problem", "verify_signature"]

NAMESPACE = "levain-confirm"
CHALLENGE_FORMAT = "levain-confirm/2"
# the verifier: an absolute path, never a PATH lookup (a binary planted earlier on PATH could print the
# "Good signature" line for any signer). Overridable only by an explicit gate argument.
DEFAULT_SSH_KEYGEN = Path("/usr/bin/ssh-keygen")
# the verifier's whole environment: fixed, nothing copied from this process
_VERIFY_ENV = {"LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never"}
# st_mode bits (the values of stat.S_IFMT / S_IFREG / S_IWGRP / S_IWOTH, which are fixed by POSIX; the
# package's standard-library allowlist does not include ``stat``)
_S_IFMT, _S_IFREG, _S_IWGRP, _S_IWOTH = 0o170000, 0o100000, 0o000020, 0o000002
_SIGNER = re.compile(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]{0,127}")
_MAX_SIGNATURE = 16384
_log = logging.getLogger(__name__)
_BEGIN = "-----BEGIN SSH SIGNATURE-----"


def challenge(*, pending: PendingAction, fence: str | None, rung: str, store_id: str,
              hold_id: str | None) -> bytes:
    """The exact bytes an enrolled key signs to approve ``pending`` at ``rung`` under the risk ``fence``, in
    the store ``store_id``, for the hold ``hold_id`` (``fence`` and ``hold_id`` are ``None`` for a manual
    pending, which no journal holds or fences). Canonical JSON, so the signer and the gate build the same
    bytes."""
    record = json.dumps(pending.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body = {
        "format": CHALLENGE_FORMAT, "approve": True, "store_id": store_id, "pending_id": pending.pending_id,
        "hold_id": hold_id, "content_sha256": hashlib.sha256(record.encode("utf-8")).hexdigest(),
        "fence": fence, "rung": rung,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def ssh_keygen_problem(path: Path | str) -> str | None:
    """Why the binary at ``path`` may not verify a signature, or ``None``: it must be an absolute path to a
    regular file owned by root and writable by neither its group nor anyone else."""
    p = Path(path)
    if not p.is_absolute():
        return f"ssh-keygen path {p} is not absolute"
    try:
        st = os.stat(p)
    except OSError as e:
        return f"ssh-keygen {p} cannot be read ({type(e).__name__})"
    if st.st_mode & _S_IFMT != _S_IFREG:
        return f"ssh-keygen {p} is not a regular file"
    if st.st_uid != 0:
        return f"ssh-keygen {p} is not owned by root (uid {st.st_uid})"
    if st.st_mode & (_S_IWGRP | _S_IWOTH):
        return f"ssh-keygen {p} is group- or world-writable"
    return None


def verify_signature(message: bytes, *, signature: str | None, signer: str | None,
                     allowed_signers: Path | None, timeout: float = 10.0,
                     ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> bool:
    """True only if ``signature`` is a valid SSHSIG over ``message`` in :data:`NAMESPACE` by the key the
    allowed-signers file names for ``signer``, as the ``ssh_keygen`` binary at that absolute path reports
    it. Never raises: every failure, including one of this machine (a verifier that is missing or not
    root-owned, no temporary file, a timeout, an unreadable file), is ``False``, because a check that could
    not run grants nothing."""
    try:
        if allowed_signers is None or not isinstance(signature, str) or not isinstance(signer, str):
            return False
        if not _SIGNER.fullmatch(signer) or len(signature) > _MAX_SIGNATURE or _BEGIN not in signature:
            return False
        if not Path(allowed_signers).is_file():
            return False
        problem = ssh_keygen_problem(ssh_keygen)
        if problem is not None:
            _log.error("confirm: refusing to verify a signature: %s", problem)
            return False
        sig_path: str | None = None
        try:
            fd, sig_path = tempfile.mkstemp(prefix="levain-confirm-", suffix=".sig")
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(signature.strip() + "\n")
            cp = subprocess.run(
                [str(ssh_keygen), "-Y", "verify", "-f", str(allowed_signers), "-I", signer, "-n", NAMESPACE,
                 "-s", sig_path],
                input=message, capture_output=True, timeout=timeout, env=dict(_VERIFY_ENV))
        finally:
            if sig_path is not None:
                try:
                    os.unlink(sig_path)
                except OSError:
                    pass
        if cp.returncode != 0:
            return False
        good = f'Good "{NAMESPACE}" signature for {signer} with '
        out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
        return any(line.strip().startswith(good) for line in out.splitlines())
    except Exception as e:  # noqa: BLE001 — a verifier that cannot run grants nothing, and never raises
        _log.warning("confirm: signature not verified (%s): %s", type(e).__name__, e)
        return False
