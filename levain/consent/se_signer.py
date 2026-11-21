"""The Secure Enclave signer behind the terminal key (levain#191 / #153): the Python side of ``levain-sign``.

``levain/native/levain_sign.swift`` is compiled to a cached helper. It is built OUTSIDE any sign: by
:func:`ensure_helper` (``terminal-key create`` / ``rotate``, ``levain doctor --fix``). The sign and status paths use
:func:`require_helper`, which never compiles: a missing or stale binary is a plain refusal naming the command that
builds it. :func:`sign_once` is the low-level call (it returns the helper's whole JSON, signature unchecked);
:func:`se_signer` returns the ``bytes -> armored SSH signature`` callable shape and verifies every signature against
the key's ``.pub`` before returning it. The signature is an ``sk-ecdsa-sha2-nistp256@openssh.com`` one (PROTOCOL.u2f
inside PROTOCOL.sshsig), so ``ssh-keygen -Y verify`` accepts it. Whether the key is presence-gated is read by the
helper from ``<blob>.meta.json`` (written at create), never from an argument. Note that the user-presence flag in a
signature is asserted by the helper (signed by the key, not attested by hardware): the property comes from the key's
access control and from Levain's own check, so no consumer may treat flags 0x01 as proof of a touch.
Every subprocess is an argv list with a bounded timeout and the message on stdin; nothing goes through a shell.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

SWIFTC = "/usr/bin/swiftc"
DEFAULT_SWIFTC = "/usr/bin/swiftc"   # always there (the xcode-select stub): a toolchain is probed, never assumed from it
XCODE_SELECT = "/usr/bin/xcode-select"
TOOLCHAIN_PROBE_TIMEOUT = 10.0
SSH_KEYGEN = "/usr/bin/ssh-keygen"
SOURCE = Path(__file__).resolve().parent.parent / "native" / "levain_sign.swift"
APPLICATION = "ssh:"
BUILD_TIMEOUT = 300.0
CREATE_TIMEOUT = 30.0
SIGN_TIMEOUT = 120.0   # a presence sheet waits on a person; the helper's wall is this less WALL_MARGIN_S (it clamps to its ceiling)
# held back from a sign's subprocess timeout for the helper's own presence wall (``--wall-ms``), so the helper's
# presence_timeout answer always comes before Python kills it mid-sheet; the wall never goes below 1 s
WALL_MARGIN_S = 2.0
WALL_FLOOR_S = 1.0
VERIFY_TIMEOUT = 15.0
BUILD_COMMAND = "levain doctor --fix"
# an explicit child environment (tests/test_launch.py): the tool paths and the home the helper and swiftc need, nothing else
_CHILD_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/var/empty"),
              **{k: os.environ[k] for k in ("TMPDIR", "DEVELOPER_DIR") if k in os.environ}}


class SeSignerError(Exception):
    """The helper could not build, or refused. ``code`` is the helper's JSON error code when it gave one."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.payload: dict = {}


def _digest() -> str:
    try:
        return hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
    except OSError as exc:
        raise SeSignerError(f"the helper source {SOURCE} cannot be read ({type(exc).__name__})") from None


def helper_path(dest_dir: Path) -> Path:
    """Where the helper built from the current source lives in ``dest_dir`` (named by the source hash)."""
    return Path(dest_dir) / f"levain-sign-{_digest()}"


def toolchain_present() -> bool:
    """True when the Command Line Tools (or Xcode) are installed: ``xcode-select -p`` exits 0 within
    :data:`TOOLCHAIN_PROBE_TIMEOUT` and the directory it prints exists. ``/usr/bin/swiftc`` alone proves nothing: it is
    the xcode-select stub, present on every Mac, and running it without a toolchain opens the install dialog."""
    try:
        cp = subprocess.run([XCODE_SELECT, "-p"], capture_output=True, text=True, timeout=TOOLCHAIN_PROBE_TIMEOUT,
                            check=False, env=dict(_CHILD_ENV))
    except (OSError, subprocess.TimeoutExpired):
        return False
    where = (cp.stdout or "").strip()
    return cp.returncode == 0 and bool(where) and os.path.isdir(where)


def compiler_problem(swiftc: str | Path | None = None) -> str | None:
    """Why the compiler cannot be used, or None. The default ``/usr/bin/swiftc`` is judged by :func:`toolchain_present`
    (never by its existence); any other path (a test's) by whether it exists."""
    compiler = str(swiftc if swiftc is not None else SWIFTC)
    if compiler == DEFAULT_SWIFTC:
        if not toolchain_present():
            return ("the Command Line Tools are not installed (`xcode-select -p` finds none); "
                    "`xcode-select --install` adds them")
        return None
    if not Path(compiler).exists():
        return f"{compiler} is missing (xcode-select --install adds it)"
    return None


def build_helper(dest_dir: Path, *, defines: tuple[str, ...] = (), name_suffix: str = "") -> Path:
    """Compile the Swift helper into ``dest_dir`` (rebuilt when the source hash changes); return the binary path.
    ``defines`` are ``-D`` flags with a ``name_suffix`` so a variant never shares a cache name (the test-only
    force-presence-flag build, which no product code passes). swiftc is never run without a toolchain
    (:func:`compiler_problem`). Each call compiles into its own temporary file (``O_EXCL``), removed unless it was put
    in place."""
    dest_dir = Path(dest_dir)
    binary = helper_path(dest_dir).with_name(helper_path(dest_dir).name + name_suffix)
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    why = compiler_problem()
    if why is not None:
        raise SeSignerError(f"the Secure Enclave helper cannot be built: {why}", "no_toolchain")
    tmp: Path | None = None
    replaced = False
    try:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            for _ in range(8):
                cand = dest_dir / f".levain-sign-{binary.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
                try:
                    os.close(os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
                except FileExistsError:
                    continue
                tmp = cand
                break
            if tmp is None:
                raise SeSignerError("no unique temporary file for the build could be made")
            cmd = [SWIFTC, "-O", *[a for d in defines for a in ("-D", d)], str(SOURCE), "-o", str(tmp)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=BUILD_TIMEOUT, check=False,
                                  env=dict(_CHILD_ENV))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SeSignerError(f"swiftc did not run: {exc}") from None
        if proc.returncode != 0:
            raise SeSignerError(f"swiftc failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
        try:
            os.chmod(tmp, 0o755)
            os.replace(tmp, binary)
            replaced = True
        except OSError as exc:
            raise SeSignerError(f"the built helper could not be put in place ({type(exc).__name__})") from None
    finally:
        if tmp is not None and not replaced:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return binary


def default_cache_dir() -> Path:
    """The per-user directory the compiled helper lives in (R7): ``~/Library/Caches/levain/native`` from the password
    database, never ``$HOME``. Holds only binaries named by their source hash; it is not the key directory."""
    import pwd
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        raise SeSignerError(f"uid {os.getuid()} has no password entry, so the helper cache directory cannot be named",
                            "no_home") from None
    return Path(home) / "Library" / "Caches" / "levain" / "native"


def ensure_helper(cache_dir: Path | None = None) -> Path:
    """The compiled helper for this source, BUILT into the per-user cache when it is not there yet. Only for a build
    point outside any sign (``terminal-key create`` / ``rotate``, ``levain doctor --fix``). Raises
    :class:`SeSignerError`; a missing swiftc names ``xcode-select --install``."""
    return build_helper(cache_dir if cache_dir is not None else default_cache_dir())


def require_helper(cache_dir: Path | None = None) -> Path:
    """The compiled helper for this source, NEVER built here (the sign and status paths call this: a compile bounded by
    :data:`BUILD_TIMEOUT` must not run inside a bounded step). Absent or stale (the source changed since it was built) is a
    :class:`SeSignerError` naming the command that builds it."""
    binary = helper_path(cache_dir if cache_dir is not None else default_cache_dir())
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    raise SeSignerError(f"the Secure Enclave helper is not built for this Levain: run `{BUILD_COMMAND}` "
                        "(it builds the helper; nothing is compiled while signing)", "helper_not_built")


def _run(helper: Path, args: list[str], *, stdin: bytes = b"", timeout: float) -> dict:
    try:
        proc = subprocess.run([str(helper), *args], input=stdin, capture_output=True, timeout=timeout, check=False,
                              env=dict(_CHILD_ENV))
    except subprocess.TimeoutExpired:
        raise SeSignerError(f"levain-sign did not answer in {timeout:g}s (TimeoutExpired)", "timeout") from None
    except OSError as exc:
        raise SeSignerError(f"levain-sign did not run: {exc}", "missing") from None
    text = proc.stdout.decode("utf-8", "replace")
    if not text.strip():
        raise SeSignerError(f"levain-sign crashed or printed nothing (exit {proc.returncode})", "no_json")
    try:
        out = json.loads(text)
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
    helper could not answer. The helper's ``status`` cannot show a Touch ID sheet (interaction is not allowed); it says
    True only for the one measured error (LocalAuthentication -1004, see the Swift source)."""
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
    """Make a Secure Enclave key at ``blob_path`` (0600, never overwritten; ``<blob>.meta.json`` records whether it is
    presence-gated); return its x9.63 public key, base64. ``presence`` adds the biometry-only user-presence access
    control (a Touch ID sheet at each sign)."""
    args = ["create", "--out", str(blob_path)] + (["--presence"] if presence else [])
    out = _run(helper, args, timeout=CREATE_TIMEOUT)
    key = out.get("public_key")
    if not isinstance(key, str) or not key:
        raise SeSignerError("levain-sign made a key but gave no public key", "no_public_key")
    return key


def public_key_line(blob_path: Path, *, helper: Path, application: str = APPLICATION) -> str:
    """The OpenSSH public key line (``sk-ecdsa-sha2-nistp256@openssh.com AAAA... levain-sign``)."""
    line = _run(helper, ["pubkey", "--key", str(blob_path), "--application", application],
                timeout=CREATE_TIMEOUT).get("line")
    if not isinstance(line, str) or not line:
        raise SeSignerError("levain-sign gave no public key line", "no_line")
    return line


def sign_once(blob_path: Path, message: bytes, *, namespace: str, helper: Path,
              reason: str = "Levain: approve this signature", application: str = APPLICATION,
              timeout: float = SIGN_TIMEOUT) -> dict:
    """One helper ``sign`` (one Touch ID sheet when the key is presence-gated, which the helper reads from the key's
    ``.meta.json``): the helper's whole JSON answer, which holds the armored ``signature`` and ``attempts``,
    ``evaluations`` (at most 1), ``counter``. The signature is NOT checked here (see :func:`se_signer`). Raises
    :class:`SeSignerError`; its ``payload`` is the helper's error JSON when it gave one. Never retried here: the
    helper's silent same-context retries (named transient codes only) are the only ones. The helper's presence wall
    (``--wall-ms``) is ``timeout`` less :data:`WALL_MARGIN_S` (never under :data:`WALL_FLOOR_S`), so its own
    ``presence_timeout`` answer comes before this side's kill."""
    wall_ms = int(max(timeout - WALL_MARGIN_S, WALL_FLOOR_S) * 1000)
    args = ["sign", "--key", str(blob_path), "--application", application, "--namespace", namespace,
            "--reason", reason, "--wall-ms", str(wall_ms)]
    return _run(helper, args, stdin=message, timeout=timeout)


def verify_signature(public_key_line_: str, message: bytes, signature: str, namespace: str, *,
                     ssh_keygen: str = SSH_KEYGEN, timeout: float = VERIFY_TIMEOUT) -> bool:
    """True only when ``ssh-keygen -Y verify`` accepts ``signature`` over ``message`` in ``namespace`` for exactly the
    key ``public_key_line_`` (the allowed-signers line holds that one key and no option). It does not read the
    user-presence flag (ssh-keygen does not enforce it); the callers that need it check it themselves."""
    if not isinstance(signature, str) or not signature.strip() or not isinstance(namespace, str) or not namespace:
        return False
    words = str(public_key_line_).split()
    if len(words) < 2 or any(c in namespace for c in " \t\r\n\"'"):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="levain-sesign-") as d:
            allowed, sigf = Path(d) / "allowed", Path(d) / "sig"
            allowed.write_text(f"levain-sign {words[0]} {words[1]}\n", encoding="ascii")
            sigf.write_text(signature.strip() + "\n", encoding="ascii")
            cp = subprocess.run([ssh_keygen, "-Y", "verify", "-f", str(allowed), "-I", "levain-sign", "-n", namespace,
                                 "-s", str(sigf)], input=message, capture_output=True, timeout=timeout, check=False,
                                env=dict(_CHILD_ENV))
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def se_signer(blob_path: Path, *, namespace: str, helper: Path, pub_path: Path | None = None,
              reason: str = "Levain: approve this signature", application: str = APPLICATION,
              timeout: float = SIGN_TIMEOUT) -> Callable[[bytes], str]:
    """A ``sign(message: bytes) -> armored signature`` for the key at ``blob_path``. EVERY signature it returns was
    verified first against the key's public half (``pub_path``, default ``<blob>.pub``) in ``namespace`` over
    ``message``; an empty, foreign, mutated or otherwise unverifiable answer raises :class:`SeSignerError` and nothing
    is returned. (A presence-gated product key signs through ``levain.cockpit.keys.ssh_signer``, which also checks the
    presence flag and writes the sign log and failure evidence.)"""
    pub = Path(pub_path) if pub_path is not None else Path(str(blob_path) + ".pub")

    def sign(message: bytes) -> str:
        try:
            line = pub.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise SeSignerError(f"the key's public half {pub} cannot be read ({type(exc).__name__}): nothing was "
                                "signed", "no_pub") from None
        out = sign_once(blob_path, message, namespace=namespace, helper=helper, reason=reason, application=application,
                        timeout=timeout)
        signature = out.get("signature")
        if not isinstance(signature, str) or not signature.strip():
            raise SeSignerError("levain-sign answered with no signature: nothing was signed", "empty_signature")
        evaluations = out.get("evaluations", 0)
        if not isinstance(evaluations, int) or isinstance(evaluations, bool) or evaluations > 1:
            raise SeSignerError(f"levain-sign evaluated presence {evaluations!r} times in one sign: refused",
                                "too_many_evaluations")
        if not verify_signature(line, message, signature, namespace):
            raise SeSignerError("levain-sign returned a signature that does not verify against the key's public "
                                "half: nothing was signed", "unverified")
        return signature
    return sign


# ---------------------------------------------------------------------------------------------------
# R4: the sign log, the field proof of how the helper path behaves

SIGN_LOG = "sign-log.jsonl"
SIGN_LOG_LOCK = "sign-log.lock"
SIGN_LOG_KEEP = 500


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def append_sign_log(key_dir: Path, record: dict) -> str | None:
    """Append one JSON line to ``<key_dir>/sign-log.jsonl`` (0600, tightened when an older file is looser) and prune
    it to its newest :data:`SIGN_LOG_KEEP` lines, both under an exclusive ``flock`` on ``<key_dir>/sign-log.lock``
    (0600, a stable file never replaced, opened without following a link), so two signers at once lose no line.
    ``record`` holds no message and no signature bytes: the caller builds it from the fields in
    :func:`sign_log_record`. A diagnostic: it never raises, it returns why it could not write (or None)."""
    path = Path(key_dir) / SIGN_LOG
    try:
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        lfd = os.open(Path(key_dir) / SIGN_LOG_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(lfd, 0o600)
            fcntl.flock(lfd, fcntl.LOCK_EX)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, line)
            finally:
                os.close(fd)
            _prune_sign_log(path)
        finally:
            os.close(lfd)          # releases the lock
    except OSError as exc:
        return f"sign log not written ({type(exc).__name__}: {exc.strerror or exc})"
    return None


def _prune_sign_log(path: Path) -> None:
    """Called with the sign-log lock held. Reads the log without following a link, writes the newest
    :data:`SIGN_LOG_KEEP` lines to a unique temporary file (``O_EXCL``, 0600) and renames it over the log; the
    temporary file is removed if the rename fails."""
    tmp: Path | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            size = os.fstat(fd).st_size
            if size < SIGN_LOG_KEEP * 40:     # fewer bytes than SIGN_LOG_KEEP shortest lines: nothing to prune
                return
            chunks = []
            while True:
                b = os.read(fd, 1 << 16)
                if not b:
                    break
                chunks.append(b)
        finally:
            os.close(fd)
        lines = b"".join(chunks).splitlines(keepends=True)
        if len(lines) <= SIGN_LOG_KEEP:
            return
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            _write_all(wfd, b"".join(lines[-SIGN_LOG_KEEP:]))
        finally:
            os.close(wfd)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def sign_log_record(*, outcome: str, attempts: int | None, evaluated: bool | None, elapsed_s: float,
                    code: str = "", evaluations: int | None = None, now: float | None = None) -> dict:
    """The one shape of a sign-log line: time (UTC ISO), outcome, helper attempts, silent retries (attempts - 1), elapsed
    seconds, whether presence was evaluated and how many times (``evaluations``, at most 1), the helper's error
    ``code`` when it refused, and pid. Helper-supplied values are coerced to their type (an int, a bool, a short
    string): a malformed helper answer cannot write arbitrary JSON here. Nothing else: no message bytes, no
    signature bytes."""
    from datetime import datetime, timezone
    t = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat()
    attempts = _int_or_none(attempts)
    return {"time": t, "outcome": str(outcome)[:40], "attempts": attempts,
            "silent_retries": None if attempts is None else max(attempts - 1, 0),
            "elapsed_s": round(float(elapsed_s), 3), "evaluated": evaluated if isinstance(evaluated, bool) else None,
            "evaluations": _int_or_none(evaluations), "code": str(code)[:80], "pid": os.getpid()}
