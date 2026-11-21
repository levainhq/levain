"""A Secure Enclave signer for the terminal key (levain#191 / #153 prototype; no caller yet).

``levain/native/levain_sign.swift`` is compiled to a cached helper (:func:`build_helper`); :func:`se_signer` returns the
``bytes -> armored SSH signature`` callable shape of ``levain.cockpit.keys.ssh_signer``'s ``sign``. The signature is an
``sk-ecdsa-sha2-nistp256@openssh.com`` one (PROTOCOL.u2f inside PROTOCOL.sshsig), so ``ssh-keygen -Y verify`` accepts it.
Every subprocess is an argv list with a bounded timeout and the message on stdin; nothing goes through a shell.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

SWIFTC = "/usr/bin/swiftc"
SOURCE = Path(__file__).resolve().parent.parent / "native" / "levain_sign.swift"
APPLICATION = "ssh:"
BUILD_TIMEOUT = 300.0
CREATE_TIMEOUT = 30.0
SIGN_TIMEOUT = 120.0   # a presence sheet waits on a person
# an explicit child environment (tests/test_launch.py): the tool paths and the home the helper and swiftc need, nothing else
_CHILD_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/var/empty"),
              **{k: os.environ[k] for k in ("TMPDIR", "DEVELOPER_DIR") if k in os.environ}}


class SeSignerError(Exception):
    """The helper could not build, or refused. ``code`` is the helper's JSON error code when it gave one."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def build_helper(dest_dir: Path) -> Path:
    """Compile the Swift helper into ``dest_dir`` (rebuilt when the source hash changes); return the binary path."""
    dest_dir = Path(dest_dir)
    if not Path(SWIFTC).exists():
        raise SeSignerError(f"{SWIFTC} is missing: install the Command Line Tools (xcode-select --install)")
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
    binary = dest_dir / f"levain-sign-{digest}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".levain-sign-{digest}.{os.getpid()}.tmp"
    try:
        proc = subprocess.run([SWIFTC, "-O", str(SOURCE), "-o", str(tmp)], capture_output=True, text=True,
                              timeout=BUILD_TIMEOUT, check=False, env=dict(_CHILD_ENV))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SeSignerError(f"swiftc did not run: {exc}") from None
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SeSignerError(f"swiftc failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    os.replace(tmp, binary)
    return binary


def _run(helper: Path, args: list[str], *, stdin: bytes = b"", timeout: float) -> dict:
    try:
        proc = subprocess.run([str(helper), *args], input=stdin, capture_output=True, timeout=timeout, check=False,
                              env=dict(_CHILD_ENV))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SeSignerError(f"levain-sign did not run: {exc}") from None
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise SeSignerError(f"levain-sign gave no JSON (exit {proc.returncode})") from None
    if not isinstance(out, dict) or not out.get("ok"):
        err = out if isinstance(out, dict) else {}
        raise SeSignerError(str(err.get("error", "levain-sign failed")), str(err.get("code", "")))
    return out


def create_key(blob_path: Path, *, presence: bool, helper: Path) -> str:
    """Make a Secure Enclave key at ``blob_path`` (0600, never overwritten); return its x9.63 public key, base64.
    ``presence`` adds the user-presence access control (a Touch ID sheet at each sign)."""
    args = ["create", "--out", str(blob_path)] + (["--presence"] if presence else [])
    return str(_run(helper, args, timeout=CREATE_TIMEOUT)["public_key"])


def public_key_line(blob_path: Path, *, helper: Path, application: str = APPLICATION) -> str:
    """The OpenSSH public key line (``sk-ecdsa-sha2-nistp256@openssh.com AAAA... levain-sign``)."""
    return str(_run(helper, ["pubkey", "--key", str(blob_path), "--application", application],
                    timeout=CREATE_TIMEOUT)["line"])


def se_signer(blob_path: Path, *, presence: bool, namespace: str, helper: Path,
              reason: str = "Levain: approve this signature", application: str = APPLICATION,
              timeout: float = SIGN_TIMEOUT) -> Callable[[bytes], str]:
    """A ``sign(message: bytes) -> armored signature`` for the key at ``blob_path``."""
    def sign(message: bytes) -> str:
        args = ["sign", "--key", str(blob_path), "--application", application, "--namespace", namespace,
                "--reason", reason] + (["--presence"] if presence else [])
        return str(_run(helper, args, stdin=message, timeout=timeout)["signature"])
    return sign
