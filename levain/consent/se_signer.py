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
import time
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
        self.payload: dict = {}


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


def default_cache_dir() -> Path:
    """The per-user directory the compiled helper lives in (R7): ``~/Library/Caches/levain/native`` from the password
    database, never ``$HOME``. Holds only binaries named by their source hash; it is not the key directory."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / "Caches" / "levain" / "native"


def ensure_helper(cache_dir: Path | None = None) -> Path:
    """The compiled helper for this source, built into the per-user cache when it is not there yet (keyed by the
    source hash, so a changed source is a new binary). Raises :class:`SeSignerError`; a missing swiftc names
    ``xcode-select --install``."""
    return build_helper(cache_dir if cache_dir is not None else default_cache_dir())


def _run(helper: Path, args: list[str], *, stdin: bytes = b"", timeout: float) -> dict:
    try:
        proc = subprocess.run([str(helper), *args], input=stdin, capture_output=True, timeout=timeout, check=False,
                              env=dict(_CHILD_ENV))
    except subprocess.TimeoutExpired:
        raise SeSignerError(f"levain-sign did not answer in {timeout:g}s (TimeoutExpired)", "timeout") from None
    except OSError as exc:
        raise SeSignerError(f"levain-sign did not run: {exc}", "missing") from None
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise SeSignerError(f"levain-sign gave no JSON (exit {proc.returncode})", "no_json") from None
    if not isinstance(out, dict) or not out.get("ok"):
        err = out if isinstance(out, dict) else {}
        exc2 = SeSignerError(str(err.get("error", "levain-sign failed")), str(err.get("code", "")))
        exc2.payload = err
        raise exc2
    return out


def key_status(blob_path: Path, *, helper: Path, timeout: float = 20.0) -> tuple[bool | None, str]:
    """R5, the live check, with no interface: ``(True, ...)`` the key needs interaction to sign (presence-gated),
    ``(False, ...)`` it signed with none (not presence-gated), ``(None, ...)`` its blob does not load on this Mac or the
    helper could not answer. The helper's ``status`` cannot show a Touch ID sheet (interaction is not allowed)."""
    try:
        out = _run(helper, ["status", "--key", str(blob_path)], timeout=timeout)
    except SeSignerError as exc:
        if exc.code == "key_load":
            return None, "not this Mac's: the Secure Enclave key does not load here"
        return None, f"the levain-sign status check could not run ({exc})"
    gated = out.get("presence_gated")
    if gated is True:
        return True, "presence-gated Secure Enclave key on this Mac"
    if gated is False:
        return False, "not presence-gated: the Secure Enclave key signed without any interaction"
    return None, "the levain-sign status check gave no answer"


def create_key(blob_path: Path, *, presence: bool, helper: Path) -> str:
    """Make a Secure Enclave key at ``blob_path`` (0600, never overwritten); return its x9.63 public key, base64.
    ``presence`` adds the user-presence access control (a Touch ID sheet at each sign)."""
    args = ["create", "--out", str(blob_path)] + (["--presence"] if presence else [])
    return str(_run(helper, args, timeout=CREATE_TIMEOUT)["public_key"])


def public_key_line(blob_path: Path, *, helper: Path, application: str = APPLICATION) -> str:
    """The OpenSSH public key line (``sk-ecdsa-sha2-nistp256@openssh.com AAAA... levain-sign``)."""
    return str(_run(helper, ["pubkey", "--key", str(blob_path), "--application", application],
                    timeout=CREATE_TIMEOUT)["line"])


def sign_once(blob_path: Path, message: bytes, *, presence: bool, namespace: str, helper: Path,
              reason: str = "Levain: approve this signature", application: str = APPLICATION,
              timeout: float = SIGN_TIMEOUT) -> dict:
    """One helper ``sign`` (one Touch ID sheet when ``presence``): the helper's whole JSON answer, which holds the
    armored ``signature`` and ``attempts``, ``evaluated``, ``counter``. Raises :class:`SeSignerError`; its ``payload``
    is the helper's error JSON when it gave one. Never retried here: the helper's silent same-context retries are the
    only ones."""
    args = ["sign", "--key", str(blob_path), "--application", application, "--namespace", namespace,
            "--reason", reason] + (["--presence"] if presence else [])
    return _run(helper, args, stdin=message, timeout=timeout)


def se_signer(blob_path: Path, *, presence: bool, namespace: str, helper: Path,
              reason: str = "Levain: approve this signature", application: str = APPLICATION,
              timeout: float = SIGN_TIMEOUT) -> Callable[[bytes], str]:
    """A ``sign(message: bytes) -> armored signature`` for the key at ``blob_path``."""
    def sign(message: bytes) -> str:
        return str(sign_once(blob_path, message, presence=presence, namespace=namespace, helper=helper,
                             reason=reason, application=application, timeout=timeout)["signature"])
    return sign


# ---------------------------------------------------------------------------------------------------
# R4: the sign log, the field proof of how the helper path behaves

SIGN_LOG = "sign-log.jsonl"
SIGN_LOG_KEEP = 500


def append_sign_log(key_dir: Path, record: dict) -> str | None:
    """Append one JSON line to ``<key_dir>/sign-log.jsonl`` (0600) and prune it to its newest
    :data:`SIGN_LOG_KEEP` lines. ``record`` holds no message and no signature bytes: the caller builds it from the
    fields in :func:`sign_log_record`. A diagnostic: it never raises, it returns why it could not write (or None)."""
    path = Path(key_dir) / SIGN_LOG
    try:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        _prune_sign_log(path)
    except OSError as exc:
        return f"sign log not written ({type(exc).__name__}: {exc.strerror or exc})"
    return None


def _prune_sign_log(path: Path) -> None:
    try:
        if path.stat().st_size < SIGN_LOG_KEEP * 40:     # fewer bytes than 500 shortest lines: nothing to prune
            return
        lines = path.read_bytes().splitlines(keepends=True)
        if len(lines) <= SIGN_LOG_KEEP:
            return
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"".join(lines[-SIGN_LOG_KEEP:]))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        pass


def sign_log_record(*, outcome: str, attempts: int | None, evaluated: bool | None, elapsed_s: float,
                    code: str = "", now: float | None = None) -> dict:
    """The one shape of a sign-log line: time (UTC ISO), outcome, helper attempts, silent retries (attempts - 1), elapsed
    seconds, the evaluate result code (``evaluated``: the presence evaluation succeeded; ``code``: the helper's error
    code when it refused) and pid. Nothing else: no message bytes, no signature bytes."""
    from datetime import datetime, timezone
    t = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat()
    return {"time": t, "outcome": outcome, "attempts": attempts,
            "silent_retries": None if attempts is None else max(attempts - 1, 0),
            "elapsed_s": round(float(elapsed_s), 3), "evaluated": evaluated, "code": code, "pid": os.getpid()}
