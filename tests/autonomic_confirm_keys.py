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
import re
import shutil
import stat
import subprocess
import tempfile
import time
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


SCRATCH_PARENT = ".levain-test-scratch"
_LEGACY_PREFIXES = (".levain-confirm-keys-", ".levain-k2b-broker-", ".levain-t3-",
                    ".levain-k2b-crash-", ".levain-k8a-", ".levain-row12b-", ".levain-custody-broker-")   # the leaked ones, ONLY these
_DEAD_AFTER = 600          # a dead-pid entry is swept once this old (never a live run's, never a just-made one)
_LEGACY_AFTER = 86400      # legacy dirs carry no ownership marker, so the bound is a day, not a run's length
_PID_RE = re.compile(r"-(\d+)-[A-Za-z0-9_]+$")
_swept: set[str] = set()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True                      # pid 0 signals the process group: never judge it dead
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return True                      # out of range: not ours to sweep
    return True


def _rm_old_dir(path: str, age: float, now: float) -> None:
    try:
        st = os.lstat(path)
        if stat.S_ISDIR(st.st_mode) and now - st.st_mtime > age:
            shutil.rmtree(path, True)
    except OSError:
        pass


def sweep_scratch(parent: str, home: str, now: float | None = None) -> None:
    """Remove what killed runs left (their ``atexit`` / ``finally`` never ran): dirs under ``parent`` whose
    embedded pid is dead and which are older than 10 minutes, and top-level legacy dirs in ``home`` with one of
    the old prefixes older than 24 hours. Nothing else is touched. Assumes one host per account home (a pid is
    only meaningful in its own pid namespace). A sweep problem never fails a test."""
    now = time.time() if now is None else now
    try:
        names = os.listdir(parent)
    except OSError:
        names = []
    for n in names:
        try:
            m = _PID_RE.search(n)
            if m and not _pid_alive(int(m.group(1))):
                _rm_old_dir(os.path.join(parent, n), _DEAD_AFTER, now)
        except Exception:
            pass
    try:
        names = os.listdir(home)
    except OSError:
        names = []
    for n in names:
        try:
            if n.startswith(_LEGACY_PREFIXES):
                _rm_old_dir(os.path.join(home, n), _LEGACY_AFTER, now)
        except Exception:
            pass


def anchor_scratch(prefix: str) -> Path:
    """A private trust-anchor temp dir under the account home (see ``account_home``), inside ONE parent,
    ``~/.levain-test-scratch``, instead of loose in the home. The name carries this process's pid, so the first
    call per process can sweep what killed runs leaked. The caller still removes its own dir at exit."""
    home = account_home()
    parent = os.path.join(home, SCRATCH_PARENT)
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(parent)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        pytest.skip(f"{parent!r} is a symlink or not a directory: refusing it as a trust-anchor parent")
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        pytest.skip(f"{parent!r} is not owned by this uid or is group/other-writable: refusing it")
    if not os.access(parent, os.R_OK | os.W_OK | os.X_OK):
        pytest.skip(f"{parent!r} lacks read/write/search access for this uid: refusing it as a trust-anchor parent")
    if parent not in _swept:
        _swept.add(parent)
        sweep_scratch(parent, home)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}-", dir=parent))


@functools.cache
def _key() -> tuple[Path, Path]:
    # under the ACCOUNT's home (password database, not ``$HOME``), not the system temp directory: an
    # allowed-signers file is a trust anchor, and a world-writable ancestor (``/tmp`` on Linux) is refused
    # (``confirm.trust_anchor_problem``). The suite's scratch ``$HOME`` lives under the temp directory, so it
    # cannot hold this; the directory is private, holds only a throwaway key, and is removed at exit.
    d = anchor_scratch("confirm-keys-")
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
