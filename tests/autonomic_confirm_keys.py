"""A person's approval for the autonomic tests: a signature by an enrolled key over the gate's challenge.

One throwaway software ed25519 key, generated once per test process and enrolled in an allowed-signers file,
stands in for the operator's hardware key (a test cannot make a Secure Enclave key). ``signed_yes`` signs the
challenge the gate gives for a pending right now; where there is none (an unknown or decided pending), it
returns an unsigned approval, which is what a caller holding no challenge could send.
"""
from __future__ import annotations

import atexit
import functools
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from levain.autonomic import ConfirmDecision, GateHandle

SIGNER = "operator"


def account_home() -> str:
    """The account's real home directory (password database): the one place a trust-anchor temp dir can sit
    where no ancestor is world-writable (the suite's scratch ``$HOME`` is under the temp directory, which is).
    Callers make a private temp dir there, holding a throwaway key, and remove it at exit. An account with no
    passwd entry, or a home that is not a writable directory (a container's arbitrary uid), skips the test by
    name instead of erroring in setup."""
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        pytest.skip("no passwd entry for this uid: no account home for a trust-anchor dir")
    if not (os.path.isdir(home) and os.access(home, os.W_OK)):
        pytest.skip(f"account home {home!r} is not a writable directory: no place for a trust-anchor dir")
    return home


@functools.cache
def _key() -> tuple[Path, Path]:
    # under the ACCOUNT's home (password database, not ``$HOME``), not the system temp directory: an
    # allowed-signers file is a trust anchor, and a world-writable ancestor (``/tmp`` on Linux) is refused
    # (``confirm.trust_anchor_problem``). The suite's scratch ``$HOME`` lives under the temp directory, so it
    # cannot hold this; the directory is private, holds only a throwaway key, and is removed at exit.
    d = Path(tempfile.mkdtemp(prefix=".levain-confirm-keys-", dir=account_home()))
    atexit.register(shutil.rmtree, d, True)
    key = d / "operator"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", SIGNER, "-f", str(key)],
                   check=True, timeout=30, capture_output=True)
    signers = d / "allowed_signers"
    signers.write_text(f'{SIGNER} namespaces="levain-confirm" {(d / "operator.pub").read_text().strip()}\n')
    return key, signers


def confirm_signers() -> Path:
    """The allowed-signers file naming the test operator's key."""
    return _key()[1]


def sign(message: bytes) -> str:
    cp = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(_key()[0]), "-n", "levain-confirm"],
                        input=message, capture_output=True, timeout=30, check=True)
    return cp.stdout.decode("ascii")


def signed_yes(gate: Any, pending_id: str, **kw: Any) -> ConfirmDecision:
    """The operator's signed approval of ``pending_id`` as ``gate`` (a gate, or a handle's current gate)
    would check it now."""
    if isinstance(gate, GateHandle):
        gate = gate.current()
    message = gate.confirm_challenge(pending_id)
    if message is None:
        return ConfirmDecision(approved=True, by="human", **kw)
    return ConfirmDecision(approved=True, by="human", signer=SIGNER, signature=sign(message), **kw)
