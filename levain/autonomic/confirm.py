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
(:data:`DEFAULT_SSH_KEYGEN` unless the gate is configured with another), never found through ``PATH``. Both
the binary and the allowed-signers file are trust anchors (:func:`trust_anchor_problem`): root-owned,
writable by nobody else, in root-owned directories, so enrolling a key is an act of root. Stdlib only.

What this module cannot check, and must be held elsewhere:
  - that an enrolled key lives in hardware the operator's uid cannot export (a Secure Enclave key): the
    file names public keys, and a software key in it verifies exactly as well;
  - who root lets enrol a key: the allowed-signers file is checked to be root's, never derived from a
    signed record of enrolments (the authenticator ledger, when it lands, is to be that record);
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

__all__ = ["NAMESPACE", "DEFAULT_SSH_KEYGEN", "challenge", "ssh_keygen_problem", "trust_anchor_problem",
           "verify_signature", "verify_signature_status", "VERIFIED", "NOT_VERIFIED", "UNAVAILABLE"]

NAMESPACE = "levain-confirm"
CHALLENGE_FORMAT = "levain-confirm/2"
# the verifier: an absolute path, never a PATH lookup (a binary planted earlier on PATH could print the
# "Good signature" line for any signer). Overridable only by an explicit gate argument.
DEFAULT_SSH_KEYGEN = Path("/usr/bin/ssh-keygen")
# the verifier's whole environment: fixed, nothing copied from this process
_VERIFY_ENV = {"LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never"}
# st_mode bits (the values of stat.S_IFMT / S_IFREG / S_IWGRP / S_IWOTH, which are fixed by POSIX; the
# package's standard-library allowlist does not include ``stat``)
_S_IFMT, _S_IFREG, _S_IFDIR, _S_IWGRP, _S_IWOTH = 0o170000, 0o100000, 0o040000, 0o000020, 0o000002
# who may own a trust anchor and every directory above it: root alone
_ROOT: frozenset[int] = frozenset({0})
# who may own the allowed-signers file and its directories: root alone. (The tests' conftest adds the test
# process's uid, for the throwaway key it enrols; no production code path changes it.)
_SIGNERS_FILE_OWNERS: frozenset[int] = _ROOT
# the three answers of :func:`verify_signature_status`
VERIFIED, NOT_VERIFIED, UNAVAILABLE = "verified", "not_verified", "unavailable"
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


def trust_anchor_problem(path: Path | str, *, what: str,
                         owners: frozenset[int] = _ROOT) -> tuple[str | None, str | None]:
    """``(why, None)`` if the file at ``path`` may not be trusted to verify a signature, else ``(None, the
    resolved path)``, which is the path to use. A trust anchor (the verifier binary, the allowed-signers
    file) must be an absolute path that resolves to a regular file owned by one of ``owners`` (root),
    writable by neither its group nor anyone else nor, through an ACL, this process's uid, in directories that
    are all the same, up to ``/``: the requester's uid can then neither edit it nor swap it between this
    check and its use (the rule of sshd's StrictModes and sudo's sudoers, with root as the only owner)."""
    p = Path(path)
    if not p.is_absolute():
        return f"{what} path {p} is not absolute", None
    real = os.path.realpath(p)
    try:
        st = os.lstat(real)
    except OSError as e:
        return f"{what} {p} cannot be read ({type(e).__name__})", None
    if st.st_mode & _S_IFMT != _S_IFREG:
        return f"{what} {p} is not a regular file", None
    if st.st_uid not in owners:
        return f"{what} {p} is not owned by root (uid {st.st_uid})", None
    if st.st_mode & (_S_IWGRP | _S_IWOTH):
        return f"{what} {p} is group- or world-writable", None
    # the mode bits do not show an ACL: ask the kernel whether THIS uid may write (access(2) applies ACLs)
    check_access = os.geteuid() not in owners
    if check_access and os.access(real, os.W_OK):
        return f"{what} {p} is writable by uid {os.geteuid()}", None
    parent = os.path.dirname(real)
    while True:
        try:
            dst = os.lstat(parent)
        except OSError as e:
            return f"{what} directory {parent} cannot be read ({type(e).__name__})", None
        if dst.st_mode & _S_IFMT != _S_IFDIR:
            return f"{what} directory {parent} is not a directory", None
        if dst.st_uid not in owners:
            return f"{what} directory {parent} is not owned by root (uid {dst.st_uid})", None
        if dst.st_mode & (_S_IWGRP | _S_IWOTH):
            return f"{what} directory {parent} is group- or world-writable", None
        if check_access and os.access(parent, os.W_OK):
            return f"{what} directory {parent} is writable by uid {os.geteuid()}", None
        if parent == os.path.dirname(parent):
            return None, real
        parent = os.path.dirname(parent)


def ssh_keygen_problem(path: Path | str) -> str | None:
    """Why the binary at ``path`` may not verify a signature, or ``None`` (:func:`trust_anchor_problem`)."""
    return trust_anchor_problem(path, what="ssh-keygen")[0]


def verify_signature(message: bytes, *, signature: str | None, signer: str | None,
                     allowed_signers: Path | None, timeout: float = 10.0,
                     ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> bool:
    """True only if :func:`verify_signature_status` is :data:`VERIFIED`."""
    return verify_signature_status(message, signature=signature, signer=signer, allowed_signers=allowed_signers,
                                   timeout=timeout, ssh_keygen=ssh_keygen) == VERIFIED


def verify_signature_status(message: bytes, *, signature: str | None, signer: str | None,
                            allowed_signers: Path | None, timeout: float = 10.0,
                            ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN) -> str:
    """:data:`VERIFIED` only if ``signature`` is a valid SSHSIG over ``message`` in :data:`NAMESPACE` by the key
    the allowed-signers file names for ``signer``, as the ``ssh_keygen`` binary reports it, both files being
    trust anchors (:func:`trust_anchor_problem`). :data:`NOT_VERIFIED` when the check ran and refused (or
    the signature or signer is malformed); :data:`UNAVAILABLE` when it could not run (no allowed-signers
    file, a verifier or signers file that is not a trust anchor, no temporary file, a timeout). Never
    raises; neither non-verdict grants anything."""
    try:
        if not isinstance(signature, str) or not isinstance(signer, str):
            return NOT_VERIFIED
        if not _SIGNER.fullmatch(signer) or len(signature) > _MAX_SIGNATURE or _BEGIN not in signature:
            return NOT_VERIFIED
        if allowed_signers is None:
            return UNAVAILABLE
        problem, keygen = trust_anchor_problem(ssh_keygen, what="ssh-keygen")
        if problem is None:
            problem, signers_file = trust_anchor_problem(allowed_signers, what="allowed-signers file",
                                                         owners=_SIGNERS_FILE_OWNERS)
        if problem is None and not os.access(signers_file, os.R_OK):
            problem = f"allowed-signers file {allowed_signers} is not readable by uid {os.geteuid()}"
        if problem is not None:
            _log.error("confirm: refusing to verify a signature: %s", problem)
            return UNAVAILABLE
        sig_path: str | None = None
        try:
            fd, sig_path = tempfile.mkstemp(prefix="levain-confirm-", suffix=".sig")
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(signature.strip() + "\n")
            cp = subprocess.run(
                [str(keygen), "-Y", "verify", "-f", str(signers_file), "-I", signer, "-n", NAMESPACE,
                 "-s", sig_path],
                input=message, capture_output=True, timeout=timeout, env=dict(_VERIFY_ENV))
        finally:
            if sig_path is not None:
                try:
                    os.unlink(sig_path)
                except OSError:
                    pass
        if cp.returncode != 0:
            return NOT_VERIFIED
        good = f'Good "{NAMESPACE}" signature for {signer} with '
        out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
        return VERIFIED if any(line.strip().startswith(good) for line in out.splitlines()) else NOT_VERIFIED
    except Exception as e:  # noqa: BLE001 — a verifier that cannot run grants nothing, and never raises
        _log.warning("confirm: signature not verified (%s): %s", type(e).__name__, e)
        return UNAVAILABLE
