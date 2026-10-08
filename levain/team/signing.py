"""SSH commit signatures: who actually signed a ledger commit, judged without trusting anyone's git config.

A team member is known by the fingerprint of an SSH public key, never by the email on a commit (anyone can type
any email). This module answers one question per commit, "which key signed this?", in one of three verdicts:

* ``signed``: the signature is cryptographically valid for the key it carries; ``fingerprint`` names that key.
  Whether that key belongs to a member is the caller's question, not this module's.
* ``unsigned``: there is no signature, it is not an SSH signature, or ``ssh-keygen`` ran and rejected it (exit 255:
  a changed payload, a wrong namespace, a truncated, garbled or oversized signature all end there).
* ``indeterminate``: the check could not run (no ``ssh-keygen``, a timeout, an unreadable object, an exit code or
  output we do not recognise). It is never cached and never read as either of the other two.

The split matters: a non-member can push a commit with a garbage signature, and if that read as "could not check"
every clone would stop judging the ledger. Only a failure of THIS machine's environment is indeterminate.

Verification calls ``ssh-keygen -Y check-novalidate`` directly on the payload git signed, rather than
``git verify-commit``, so a repository's ``gpg.ssh.allowedSignersFile``, ``gpg.ssh.program`` or
``log.showSignature`` cannot change the verdict. POSIX only, stdlib only.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .transport import _NO_HOOKS, _SCRUB_ENV

SSH_SIG_BEGIN = "-----BEGIN SSH SIGNATURE-----"
CACHE_SCHEMA = "sigcache-v2"   # bump whenever a verdict rule or the accepted key set changes
# key type -> the curve name an ecdsa blob must carry (None for the non-ecdsa types)
KEY_TYPES = {
    "ssh-ed25519": None,
    "ecdsa-sha2-nistp256": "nistp256",
    "ecdsa-sha2-nistp384": "nistp384",
    "ecdsa-sha2-nistp521": "nistp521",
}
# uncompressed point length (0x04 || X || Y) per curve; OpenSSH accepts only uncompressed points
_EC_POINT_LEN = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
_ED25519_LEN = 32
_GOOD = re.compile(r'^Good "git" signature with \S+ key (SHA256:[A-Za-z0-9+/]+)$')
_FP = re.compile(r"^SHA256:[A-Za-z0-9+/]+$")
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# the signature header git writes for each object format: a SHA-256 repository's commits carry ``gpgsig-sha256``
_HEADERS = {40: b"gpgsig ", 64: b"gpgsig-sha256 "}


class SigningError(ValueError):
    """A key or object id was refused. The message is meant for a person to read."""


# ---- fingerprints -----------------------------------------------------------------------------------------


def _read_string(blob: bytes, off: int, what: str) -> tuple[bytes, int]:
    """One SSH wire ``string`` (uint32 big-endian length, then that many bytes) at ``off``."""
    if off + 4 > len(blob):
        raise SigningError(f"the key blob is truncated before its {what}")
    (n,) = struct.unpack(">I", blob[off:off + 4])
    off += 4
    if off + n > len(blob):
        raise SigningError(f"the key blob is truncated inside its {what}")
    return blob[off:off + n], off + n


def _check_blob(declared: str, blob: bytes) -> None:
    """Refuse a blob whose fields do not match ``declared`` exactly, including any trailing bytes."""
    embedded, off = _read_string(blob, 0, "type")
    if embedded != declared.encode():
        raise SigningError(f"the key says {declared} but its blob says {embedded.decode('ascii', 'replace')!r}")
    curve = KEY_TYPES[declared]
    if declared == "ssh-ed25519":
        key, off = _read_string(blob, off, "public key")
        if len(key) != _ED25519_LEN:
            raise SigningError(f"an ed25519 public key is {_ED25519_LEN} bytes, this one is {len(key)}")
    elif curve is not None:
        name, off = _read_string(blob, off, "curve name")
        if name != curve.encode():
            raise SigningError(f"the key says {declared} but its curve is {name.decode('ascii', 'replace')!r}")
        point, off = _read_string(blob, off, "public point")
        if len(point) != _EC_POINT_LEN[curve] or point[:1] != b"\x04":
            raise SigningError(f"the {curve} public point is not an uncompressed point of the curve's size")
    if off != len(blob):
        raise SigningError(f"the key blob has {len(blob) - off} unexpected trailing bytes")


def fingerprint(pubkey_line: str) -> str:
    """The ``SHA256:...`` fingerprint ``ssh-keygen -lf`` prints for one OpenSSH public key line.

    Accepts ``ssh-ed25519`` and ``ecdsa-sha2-nistp{256,384,521}`` only. ``ssh-rsa`` is refused: whether an RSA
    signature verifies depends on the OpenSSH build (SHA-1 RSA signatures are disabled in newer ones), so two clones
    could disagree about the same commit. Hardware-backed ``sk-*`` keys
    and ``*-cert-v01@openssh.com`` certificates are refused on purpose: a member is one plain key, and a
    certificate would make the verdict depend on a CA this module does not judge.
    """
    fields = pubkey_line.strip().split()
    if len(fields) < 2:
        raise SigningError("a public key line is '<type> <base64> [comment]'")
    declared, b64 = fields[0], fields[1]
    if declared.startswith("sk-"):
        raise SigningError(f"security-key types ({declared}) are not accepted")
    if declared.endswith("-cert-v01@openssh.com"):
        raise SigningError(f"certificates ({declared}) are not accepted; use the plain public key")
    if declared not in KEY_TYPES:
        raise SigningError(f"unknown or unaccepted key type {declared!r}")
    try:
        blob = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise SigningError("the key's base64 does not decode") from None
    _check_blob(declared, blob)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


# ---- commit objects ---------------------------------------------------------------------------------------


def split_signed(raw: bytes, header: bytes = _HEADERS[40]) -> tuple[bytes, bytes | None]:
    """Split ``git cat-file commit`` output into (the bytes that were signed, the signature or None).

    The signature is the ``gpgsig`` header: its first line plus the continuation lines (those starting with one
    space, which is stripped), joined with ``\\n``. The payload is the object with every line of that header
    removed and every other byte left alone. Only the header block (before the first blank line) is searched, so
    a message that mentions ``gpgsig`` is not a signature. ``header`` is the object format's signature header
    (``_HEADERS``): in a SHA-1 repository ``gpgsig-sha256`` is part of the signed content and stays in the payload,
    and in a SHA-256 repository the reverse. If the header appears more than
    once, every copy is removed and their lines are concatenated, as git does.
    """
    parts = raw.split(b"\n")  # only LF ends a line in a git object; a CR is content
    lines = [p + b"\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    payload: list[bytes] = []
    sig: list[bytes] = []
    in_headers, in_sig, found = True, False, False
    for line in lines:
        if in_headers and line == b"\n":
            in_headers = in_sig = False
            payload.append(line)
            continue
        if in_headers and in_sig and line.startswith(b" "):
            sig.append(line[1:].rstrip(b"\n"))
            continue
        in_sig = False
        if in_headers and line.startswith(header):
            in_sig = found = True
            sig.append(line[len(header):].rstrip(b"\n"))
            continue
        payload.append(line)
    return b"".join(payload), (b"\n".join(sig) if found else None)


def _sig_is_last_header(raw: bytes, header: bytes) -> bool:
    head = raw.split(b"\n\n", 1)[0].split(b"\n")
    starts = [i for i, ln in enumerate(head) if ln.startswith(header)]
    if len(starts) != 1:
        return False
    rest = head[starts[0] + 1:]
    return all(ln.startswith(b" ") for ln in rest)


# ---- verification -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What one commit's signature says. ``kind`` is "signed", "unsigned" or "indeterminate"."""

    kind: str
    fingerprint: str | None
    reason: str

    @property
    def definitive(self) -> bool:
        """True for a verdict the same bytes will always produce again (so it may be cached)."""
        return self.kind in ("signed", "unsigned")


def _signed(fp: str, reason: str = "good signature") -> Verdict:
    return Verdict("signed", fp, reason)


def _unsigned(reason: str) -> Verdict:
    return Verdict("unsigned", None, reason)


def _indeterminate(reason: str) -> Verdict:
    return Verdict("indeterminate", None, reason)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    # GIT_NO_REPLACE_OBJECTS: `git replace` would make one id read as another object's bytes, so an unsigned commit
    # could verify as a signed one (L3 r2 codex, measured)
    env.update(LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE="/dev/null", **extra)
    return env


def _output_tail(cp: subprocess.CompletedProcess) -> str:
    text = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-2:]) if lines else f"exit {cp.returncode}"


def _cat_commit(repo_dir: Path, sha: str, timeout: float) -> bytes | Verdict:
    """The raw commit object, or an indeterminate verdict saying why it could not be read."""
    try:
        cp = subprocess.run(["git", *_NO_HOOKS, "cat-file", "commit", sha], cwd=str(repo_dir), env=_env(),
                            capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _indeterminate(f"git cat-file timed out after {timeout:.0f}s")
    except (FileNotFoundError, NotADirectoryError):
        return _indeterminate("git is not on PATH, or the repository directory is missing")
    if cp.returncode != 0:
        return _indeterminate(f"git cat-file failed: {_output_tail(cp)}")
    return cp.stdout


def verify_commit(repo_dir: Path, sha: str, *, timeout: float = 10.0) -> Verdict:
    """Judge one commit's SSH signature by the key embedded in the signature itself (no allowed-signers file).

    ``sha`` must be a full object id; anything else is refused with ``SigningError`` (a caller bug, not a verdict).
    """
    if not isinstance(sha, str) or not _OID.match(sha):
        raise SigningError(f"{sha!r} is not a full lowercase object id")
    raw = _cat_commit(repo_dir, sha, timeout)
    if isinstance(raw, Verdict):
        return raw
    payload, sig = split_signed(raw, _HEADERS[len(sha)])
    if sig is None:
        return _unsigned("no signature")
    if not _sig_is_last_header(raw, _HEADERS[len(sha)]):
        # git writes the signature as the LAST header. One moved between `tree` and `parent` still verifies (the
        # whole block is stripped) while hiding the parent from git: refused (L2 1007+19 F2, RAN)
        return _unsigned("the signature is not the last header")
    if not sig.startswith(SSH_SIG_BEGIN.encode()):
        return _unsigned("not an SSH signature")
    fd, sig_path = tempfile.mkstemp(prefix="levain-sig-", suffix=".sig")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(sig + b"\n")
        try:
            cp = subprocess.run(["ssh-keygen", "-Y", "check-novalidate", "-n", "git", "-s", sig_path],
                                input=payload, capture_output=True, timeout=timeout,
                                env=_env(SSH_ASKPASS_REQUIRE="never"))
        except FileNotFoundError:
            return _indeterminate("ssh-keygen is not on PATH")
        except subprocess.TimeoutExpired:
            return _indeterminate(f"ssh-keygen timed out after {timeout:.0f}s")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(sig_path)
    out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
    if cp.returncode == 0:
        for line in out.splitlines():
            m = _GOOD.match(line.strip())
            if m:
                return _signed(m.group(1))
    elif cp.returncode == 255:
        # ssh-keygen ran and refused the signature. It says "incorrect signature" for a changed payload and "Could not
        # verify signature." for a wrong namespace or a malformed blob; both are the commit's fault, not this machine's.
        return _unsigned(f"bad signature ({_output_tail(cp)})")
    return _indeterminate(f"ssh-keygen exit {cp.returncode}: {_output_tail(cp)}")


# ---- the cache --------------------------------------------------------------------------------------------


class SigCache:
    """A JSON file of definitive verdicts by commit id, so a long ledger is verified once, not on every read.

    A commit id names fixed bytes, so its signed/unsigned verdict never changes; ``indeterminate`` is never
    stored, so a check that could not run is retried next time. A file with a missing or different schema tag,
    or that does not parse, reads as empty. Writes are atomic (temp file + ``os.replace``) and merge with what is
    on disk at write time, so two writers lose nothing worse than a re-verification.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA or not isinstance(data.get("entries"), dict):
            return {}
        good: dict[str, dict] = {}
        for sha, rec in data["entries"].items():
            if not isinstance(sha, str) or not _OID.match(sha) or not isinstance(rec, dict):
                continue
            kind, fp = rec.get("kind"), rec.get("fp")
            if kind == "signed" and isinstance(fp, str) and _FP.match(fp):
                good[sha] = {"kind": kind, "fp": fp}
            elif kind == "unsigned" and fp is None:
                good[sha] = {"kind": kind, "fp": None}
        return good

    @property
    def entries(self) -> dict[str, dict]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def get(self, sha: str) -> Verdict | None:
        rec = self.entries.get(sha)
        if rec is None:
            return None
        return Verdict(rec["kind"], rec["fp"], "cached")

    def _write(self, new: dict[str, dict]) -> None:
        merged = self._load()
        merged.update(new)
        self._entries = merged
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"schema": CACHE_SCHEMA, "entries": merged}, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def verify(self, repo_dir: Path, shas: list[str]) -> dict[str, Verdict]:
        """Verdicts for ``shas``: cached ones as stored, the rest verified now and stored if definitive."""
        out: dict[str, Verdict] = {}
        new: dict[str, dict] = {}
        for sha in shas:
            if sha in out:
                continue
            cached = self.get(sha)
            if cached is not None:
                out[sha] = cached
                continue
            v = verify_commit(repo_dir, sha)
            out[sha] = v
            if v.definitive:
                new[sha] = {"kind": v.kind, "fp": v.fingerprint}
        if new:
            self._write(new)
        return out


# ---- signing ----------------------------------------------------------------------------------------------


def sign_config(key: str) -> list[str]:
    """The git ``-c`` arguments that make a commit SSH-signed with ``key`` (a public key path or literal)."""
    if not key or "\n" in key or "\r" in key:
        raise SigningError("the signing key must be one non-empty line")
    return ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "-c", "commit.gpgsign=true"]


def signing_env() -> dict[str, str]:
    """Environment additions so signing fails instead of prompting: a passphrase is asked of ``false``, not a tty."""
    return {"SSH_ASKPASS_REQUIRE": "force", "SSH_ASKPASS": shutil.which("false") or "/usr/bin/false"}
