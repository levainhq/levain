"""levain.dispatch.snapshot — the workspace snapshot: the whole repo at HEAD as ONE history-less commit, minus what a role withholds.

An unattended session (S10) works in a snapshot of the operator's repo, not in a clone of its branch: a clone carries
every old blob, and a removed secret lives in history while the run ships every read to the model provider. This module
builds the snapshot. It is the sibling of :mod:`levain.dispatch.readset` (which builds a snapshot of a contract's
listed files) for the case where no contract lists the files: the files are every tracked regular file at ``HEAD``.

* A role only WITHHOLDS. ``withhold`` is a list of :func:`levain.dispatch.policy.match` patterns; a matching path is
  not copied. A role never narrows the workspace to focus the agent.
* Fail closed, whole. Any refusal (an unreadable path, a symlink or submodule that is not withheld, a path on the
  policy's NEVER list, a content-scan hit, a size bound) refuses the whole snapshot by name; nothing is trimmed to fit
  and no partial snapshot is left behind.
* The CONTENT of every included blob is scanned (private-key armor, common token prefixes, secret-named
  assignments in ``.env``, shell, YAML, JSON and code shapes). A hit names the path and the rule, never the matched text. The scan is a floor, not a proof: it finds
  the shapes it knows. A false positive (a secret-named assignment of 12 bytes or more that holds no secret) is
  cleared only by the role withholding that path: the scan has no exemptions, and a role can only withhold.
* The :class:`Manifest` is a plain deterministic record: HEAD, the withhold list and its digest, and every included
  entry as (path, mode, blob). :func:`snapshot_commit` derives the snapshot's commit sha ``E``, tree and object count
  ``N`` from the manifest alone (pure: no git, no repo), so a later stage compares a sha and builds nothing to trust.
  Signing the manifest is NOT done here (open).

Known edges (judged, no code): the scan reads blob bytes as the committed text, so a secret split across lines or
encoded (base64, compressed) is not seen; a binary blob is scanned as bytes and is otherwise opaque. ``HEAD`` is the
repo's checked-out commit: the working tree, the index and untracked files are never read. Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import Any

from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError
from levain.dispatch.contract import GIT_TIMEOUT
from levain.dispatch.policy import ProviderPolicy, _pattern_problem, match, path_text_problem
from levain.dispatch.readset import (BRANCH, _IDENTITY, _EMAIL, _REGULAR, _SHA1, ReadSet, ReadSetRefused,
                                     _git_object, _path_checks, _remove, _scratch_env)

__all__ = ["BLOB_MAX_BYTES", "FORMAT", "Manifest", "SNAPSHOT_MAX_BYTES", "SNAPSHOT_MAX_FILES", "SnapshotRefused",
           "build_snapshot", "plan_snapshot", "scan_blob", "snapshot_commit"]

FORMAT = "levain-dispatch-snapshot/1"
#: Bounds ([judged]; to be set from the §7d proof's measured sizes). Over any one: the whole snapshot is refused.
SNAPSHOT_MAX_FILES = 20_000
# 64 MiB, not more: plan + build hold the snapshot about 4x over at peak (cat-file output, the blob map, the
# fast-import stream and its bytes copy), so this bound is what keeps a worker's memory near 256 MiB without streaming.
SNAPSHOT_MAX_BYTES = 64 * 1024 ** 2
BLOB_MAX_BYTES = 8 * 1024 ** 2
_WITHHOLD_MAX = 256

#: (rule name, pattern). Rule names are what a refusal shows; the matched text never is. Every pattern here is
#: linear: no nested or overlapping quantifiers (the assignment rule is the separate parse below).
_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple((n, re.compile(p)) for n, p in (
    ("private-key-armor", rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ("aws-access-key-id", rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}"),
    ("anthropic-or-openai-key", rb"\bsk-ant-[A-Za-z0-9_-]{20,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    ("slack-token", rb"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("google-api-key", rb"\bAIza[0-9A-Za-z_-]{35}\b"),
))
#: The assignment rule, one line at a time, every step linear and bounded. Only a line holding a secret word is
#: parsed, and only its first 64 ``=`` / ``:`` separators; on a line with more, a secret word anywhere after the
#: 64th is a finding (fail closed). Before each separator, the NAME is the last run of name bytes
#: (``[A-Za-z0-9_.-]``, at most 256), which may be quoted. A name is secret when it holds a secret word. ONE NAMED
#: EXEMPTION (head-ruled): the plural ``tokens`` as its last word (``tokens``, ``input_tokens``, ``max-tokens``) is a
#: usage counter, not a secret, so ``AUTH_TOKENS=...`` holding a real credential list is NOT a finding; the past-64
#: check ignores this exemption (accepted). EVERY separator follows one rule: the name must start the line or follow
#: a ``,`` / ``{`` / whitespace boundary, and its VALUE is the rest of the line, never cut at a quote, bracket or
#: delimiter (a cut is how rounds 1-5 kept letting a credential through). Fail closed, no exemptions: a reference, a
#: placeholder, a line of code or prose under a secret name is a finding like any other, and so is a value that is
#: only a YAML block indicator or that ends in a ``\`` continuation, whose secret lies on lines this rule does not
#: read. The role withholds the path.
_SECRET_NAME = re.compile(rb"SECRET|TOKEN|PASSWORD|PASSWD|API[_.-]?KEY|PRIVATE[_.-]?KEY")
_PLURAL_TOKENS = (b"_TOKENS", b"-TOKENS", b".TOKENS")
_SEPARATOR = re.compile(rb"[=:]")
_SEPARATOR_MAX = 64
_BOUNDARY = frozenset(b",{ \t\r\f\v")
_NAME_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_NAME_MAX = 256
_BLOCK_INDICATOR = re.compile(rb"[|>][-+0-9]{0,2}")
_ASSIGN_MIN = 12


def _trailing_name(p: bytes) -> tuple[bytes, bytes] | None:
    """``(name, rest)`` for the name ending ``p`` (optionally quoted; the quotes are not in either part), else ``None``."""
    quote = p[-1:] if p[-1:] in (b'"', b"'") else b""
    if quote:
        p = p[:-1]
    i, lo = len(p), max(0, len(p) - _NAME_MAX - 1)
    while i > lo and p[i - 1] in _NAME_BYTES:
        i -= 1
    name, rest = p[i:], p[:i]
    if not name or len(name) > _NAME_MAX:
        return None
    if quote:
        if rest[-1:] != quote:
            return None
        rest = rest[:-1]
    return name, rest


def _key_name(segment: bytes, at_line_start: bool) -> bytes | None:
    """The name before a separator (``segment`` runs from the line start or the previous separator to it), or
    ``None`` unless it starts the line or follows a ``,`` / ``{`` / whitespace boundary inside the segment."""
    got = _trailing_name(segment.rstrip())
    if got is None:
        return None
    if not got[1]:
        return got[0] if at_line_start else None
    return got[0] if got[1][-1] in _BOUNDARY else None


def _is_secret_name(name: bytes) -> bool:
    upper = name.upper()
    if upper == b"TOKENS" or upper.endswith(_PLURAL_TOKENS):
        upper = upper[:-6]
    return _SECRET_NAME.search(upper) is not None


def _secret_value(value: bytes) -> bool:
    """Whether an assignment value (the rest of the line after the separator) is a finding: a YAML block indicator
    alone (a trailing ``#`` comment allowed) or a trailing ``\\`` continuation, else ``_ASSIGN_MIN`` bytes or more
    once surrounding whitespace and ONE layer of matching quotes are stripped. No escapes read, nothing exempted."""
    value = value.strip()
    if value.endswith(b"\\"):
        return True
    head = value.split(None, 1)
    if head and _BLOCK_INDICATOR.fullmatch(head[0]) is not None and (len(head) == 1 or head[1][:1] == b"#"):
        return True
    if len(value) >= 2 and value[:1] in (b'"', b"'") and value[-1:] == value[:1]:
        value = value[1:-1]
    return len(value) >= _ASSIGN_MIN


def _secret_line(body: bytes, upper: bytes, start: int, end: int) -> bool:
    """Whether the line ``body[start:end]`` assigns a finding to a secret name, at any of its first
    ``_SEPARATOR_MAX`` separators (every one by the same rule, its value the rest of the line); past that cap, any
    secret word after the last parsed separator is a finding. Bounded: the name segments are disjoint, and the rest
    of the line is read once per secret-named key, so at most ``_SEPARATOR_MAX`` times."""
    prev = None
    for k, sep in enumerate(_SEPARATOR.finditer(body, start, end)):
        if k == _SEPARATOR_MAX:
            return _SECRET_NAME.search(upper, prev.end(), end) is not None
        name = _key_name(body[start if prev is None else prev.end():sep.start()], prev is None)
        if name is not None and _is_secret_name(name) and _secret_value(body[sep.end():end]):
            return True
        prev = sep
    return False


def _secret_assignment(body: bytes) -> bool:
    """Whether any line of ``body`` assigns a value that is a finding to a secret-named name. Each line holding a
    secret word is parsed once, at no more than ``_SEPARATOR_MAX`` separators, so the scan stays linear in
    ``len(body)``."""
    upper = body.upper()
    line_end = -1
    for m in _SECRET_NAME.finditer(upper):
        if m.start() < line_end:
            continue
        start = body.rfind(b"\n", 0, m.start()) + 1
        line_end = body.find(b"\n", m.start())
        if line_end < 0:
            line_end = len(body)
        if _secret_line(body, upper, start, line_end):
            return True
    return False


class SnapshotRefused(DispatchError):
    """A snapshot that cannot be built, by name (``name``) with a human ``detail``. Nothing was handed on."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


def scan_blob(body: bytes) -> str | None:
    """The name of the first content rule ``body`` trips, or ``None``. Never returns the matched text."""
    for name, rx in _RULES:
        if rx.search(body) is not None:
            return name
    if _secret_assignment(body):
        return "secret-assignment"
    return None


def _fi_path(path: str) -> bytes:
    """``path`` C-quoted for ``git fast-import`` (always quoted, which git accepts in every case and requires for a
    path that begins with ``"`` or holds a newline): ``\\`` and ``"`` escaped, control bytes as octal, UTF-8 as is."""
    out = bytearray(b'"')
    for b in path.encode("utf-8"):
        if b in (0x22, 0x5C):
            out += b"\\" + bytes([b])
        elif b < 0x20 or b == 0x7F:
            out += b"\\%03o" % b
        else:
            out.append(b)
    return bytes(out + b'"')


@dataclass(frozen=True)
class Manifest:
    """The plain deterministic record of one snapshot. ``entries`` is sorted ``(path, mode, blob)``."""
    head: str
    withhold: tuple[str, ...]
    withheld_paths: tuple[str, ...]
    entries: tuple[tuple[str, str, str], ...]
    history: int = 0

    def to_mapping(self) -> dict[str, Any]:
        return {"format": FORMAT, "head": self.head, "history": self.history,
                "withhold": list(self.withhold), "withhold_digest": withhold_digest(self.withhold),
                "withheld_paths": list(self.withheld_paths), "entries": [list(e) for e in self.entries]}

    def canonical(self) -> bytes:
        return canonical_bytes(self.to_mapping())

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical()).hexdigest()


def withhold_digest(withhold: tuple[str, ...]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes({"withhold": list(withhold)})).hexdigest()


def _withhold_patterns(withhold: Any) -> tuple[str, ...]:
    if isinstance(withhold, (str, bytes)) or not isinstance(withhold, (list, tuple)):
        raise SnapshotRefused("snapshot-withhold", "the withhold list must be a list of patterns")
    if len(withhold) > _WITHHOLD_MAX:
        raise SnapshotRefused("snapshot-withhold", f"more than {_WITHHOLD_MAX} withhold patterns")
    for p in withhold:
        problem = _pattern_problem(p)
        if problem is not None:
            raise SnapshotRefused("snapshot-withhold", problem)
    if len(set(withhold)) != len(withhold):
        raise SnapshotRefused("snapshot-withhold", "a withhold pattern is listed twice")
    return tuple(sorted(withhold))


def _git(repo: str, *args: str, data: bytes | None = None) -> bytes:
    env = {**_scratch_env(), "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0"}
    argv = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", repo, *args]
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=GIT_TIMEOUT, env=env, input=data,
                            stdin=None if data is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotRefused("snapshot-git-unavailable", f"git could not run ({type(exc).__name__})") from None
    if cp.returncode != 0:
        tail = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SnapshotRefused("snapshot-git-failed", f"git {args[0]}: {tail[-1][:200] if tail else cp.returncode}")
    return cp.stdout


def _read_blobs(repo: str, shas: list[str]) -> dict[str, bytes]:
    """Every blob's bytes in one ``cat-file --batch``, each hash-checked against its sha (never fetched)."""
    if not shas:
        return {}
    out = _git(repo, "cat-file", "--batch", data="".join(s + "\n" for s in shas).encode("ascii"))
    got: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        nl = out.find(b"\n", pos)
        head = out[pos:nl].split(b" ") if nl >= 0 else []
        if len(head) != 3 or head[1] != b"blob" or head[0].decode("ascii", "replace") != sha:
            raise SnapshotRefused("snapshot-blob-missing", f"blob {sha[:12]} is not readable from the repo")
        size = int(head[2])
        body = out[nl + 1:nl + 1 + size]
        if hashlib.sha1(b"blob %d\0" % size + body).hexdigest() != sha:
            raise SnapshotRefused("snapshot-blob-mismatch", f"blob {sha[:12]} does not hash to its name")
        got[sha] = body
        pos = nl + 1 + size + 1
    return got


def plan_snapshot(repo: str, withhold: Any = (), *, policy: ProviderPolicy | None = None,
                  provider: str | None = None) -> tuple[Manifest, dict[str, bytes]]:
    """Judge ``repo`` at HEAD and return ``(manifest, blobs)``, or raise :class:`SnapshotRefused`. Order: withhold
    first (a withheld path is judged by nothing else), then entry kind, path hygiene, the NEVER list (``policy`` and
    ``provider`` together, or neither), the count and size bounds from git's own sizes, then the content scan."""
    if not isinstance(repo, str) or not os.path.isabs(repo):
        raise SnapshotRefused("snapshot-repo", "the repo must be an absolute path")
    pats = _withhold_patterns(withhold)
    if (policy is None) != (provider is None):
        raise SnapshotRefused("snapshot-policy", "a policy and a provider are given together or not at all")
    head = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}").decode("ascii", "replace").strip()
    if _SHA1.fullmatch(head) is None:
        raise SnapshotRefused("snapshot-repo", "HEAD is not a commit")
    raw = _git(repo, "ls-tree", "-r", "-z", "-l", head)      # the sha resolved once, never HEAD again
    kept: list[tuple[str, str, str, int]] = []
    withheld: list[str] = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, tab, pb = rec.partition(b"\t")
        try:
            path = pb.decode("utf-8")
            mode, kind, sha, size = meta.decode("ascii").split(None, 3)
        except (UnicodeDecodeError, ValueError):
            raise SnapshotRefused("snapshot-entry", "a tracked path is not UTF-8 or its record is malformed") from None
        if not tab or path_text_problem(path) is not None or unicodedata.normalize("NFC", path) != path:
            raise SnapshotRefused("snapshot-entry", f"{path[:120]!r}: the path cannot show as itself or is not NFC")
        if any(match(p.casefold(), path.casefold()) for p in pats):
            withheld.append(path)
            continue
        if mode not in _REGULAR or kind != "blob" or _SHA1.fullmatch(sha) is None:
            raise SnapshotRefused("snapshot-entry", f"{path[:120]}: mode {mode} is not a regular file "
                                  "(a symlink or submodule is withheld by name or refused)")
        kept.append((path, mode, sha, int(size.strip())))
    if len(kept) > SNAPSHOT_MAX_FILES:
        raise SnapshotRefused("snapshot-too-many-files", f"{len(kept)} files is over {SNAPSHOT_MAX_FILES}")
    for path, _m, _s, size in kept:
        if size > BLOB_MAX_BYTES:
            raise SnapshotRefused("snapshot-blob-too-large", f"{path[:120]}: {size} bytes is over {BLOB_MAX_BYTES}")
    total = sum(k[3] for k in kept)
    if total > SNAPSHOT_MAX_BYTES:
        raise SnapshotRefused("snapshot-too-large", f"{total} bytes is over {SNAPSHOT_MAX_BYTES}")
    paths = [k[0] for k in kept]
    if policy is not None:
        why = policy.refusal(provider, paths, [])
        if why is not None:
            raise SnapshotRefused("snapshot-policy", why[:300])
    try:
        _path_checks(paths)
    except ReadSetRefused as exc:
        raise SnapshotRefused("snapshot-" + exc.name.removeprefix("readset-"), exc.detail) from None
    blobs = _read_blobs(repo, sorted({k[2] for k in kept}))
    for path, _m, sha, _z in sorted(kept):
        rule = scan_blob(blobs[sha])
        if rule is not None:
            raise SnapshotRefused("snapshot-secret", f"{path[:120]}: content rule {rule} (if it holds no secret, "
                                  "the role withholds this path; the scan exempts nothing)")
    entries = tuple(sorted((p, m, s) for p, m, s, _z in kept))
    return Manifest(head=head, withhold=pats, withheld_paths=tuple(sorted(withheld)), entries=entries), blobs


def snapshot_commit(manifest: Manifest) -> tuple[str, str, int]:
    """``(E, tree, N)`` for ``manifest``: the sha of the one parentless commit :func:`build_snapshot` makes, its tree
    and its object closure (1 commit + distinct trees + distinct blobs). Pure: no git, no repo."""
    if manifest.history != 0:
        raise SnapshotRefused("snapshot-history", "a snapshot with history is not built in this slice")
    root: dict = {}
    for path, mode, blob in manifest.entries:
        *dirs, name = path.split("/")
        node = root
        for d in dirs:
            node = node.setdefault(d, {})
        node[name] = (mode, blob)
    trees: set[bytes] = set()

    def tree_sha(node: dict) -> bytes:
        items = []
        for name, val in node.items():
            raw = name.encode("utf-8")
            if isinstance(val, dict):
                items.append((raw + b"/", b"40000 " + raw + b"\0" + tree_sha(val)))
            else:
                items.append((raw, val[0].encode("ascii") + b" " + raw + b"\0" + bytes.fromhex(val[1])))
        sha = _git_object(b"tree", b"".join(entry for _, entry in sorted(items)))
        trees.add(sha)
        return sha

    tree = tree_sha(root).hex()
    who = f"{_IDENTITY} <{_EMAIL}> 0 +0000"
    body = f"tree {tree}\nauthor {who}\ncommitter {who}\n\n{_message(manifest)}\n".encode("utf-8")
    return _git_object(b"commit", body).hex(), tree, 1 + len(trees) + len({b for _p, _m, b in manifest.entries})


def _message(manifest: Manifest) -> str:
    return f"levain snapshot {manifest.digest()}"


def build_snapshot(repo: str, dest_dir: str, withhold: Any = (), *, policy: ProviderPolicy | None = None,
                   provider: str | None = None) -> tuple[ReadSet, Manifest]:
    """Build the snapshot of ``repo`` at HEAD as a bare scratch repo in ``dest_dir`` (a real absolute path that must NOT
    exist yet, and not inside ``repo``), or raise :class:`SnapshotRefused`. The built commit must equal
    :func:`snapshot_commit` of the manifest or the build is refused; a refusal after ``dest_dir`` was created removes
    it again (only while it is still the directory this call made)."""
    manifest, blobs = plan_snapshot(repo, withhold, policy=policy, provider=provider)
    try:
        ok = (isinstance(dest_dir, str) and os.path.isabs(dest_dir) and os.path.realpath(dest_dir) == dest_dir)
        rst = os.stat(repo)
    except (OSError, ValueError) as exc:
        raise SnapshotRefused("snapshot-dest", f"a path cannot be read ({type(exc).__name__})") from None
    if not ok:
        raise SnapshotRefused("snapshot-dest", "dest_dir must be a real absolute path")
    p = dest_dir
    while True:
        try:
            st = os.stat(p)
            if (st.st_dev, st.st_ino) == (rst.st_dev, rst.st_ino):
                raise SnapshotRefused("snapshot-dest", "dest_dir is inside the operator's repo")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SnapshotRefused("snapshot-dest", f"an ancestor cannot be read ({type(exc).__name__})") from None
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        os.mkdir(dest_dir, 0o700)
        dst = os.lstat(dest_dir)
    except FileExistsError:
        raise SnapshotRefused("snapshot-dest", "dest_dir must not exist yet: build_snapshot creates it") from None
    except OSError as exc:
        raise SnapshotRefused("snapshot-dest", f"dest_dir cannot be created ({type(exc).__name__})") from None
    made = (dst.st_dev, dst.st_ino)
    try:
        want, tree, _n = snapshot_commit(manifest)
        _git(dest_dir, "init", "--bare", "-q", "--template=", f"--initial-branch={BRANCH}", ".")
        msg = (_message(manifest) + "\n").encode("utf-8")
        stream = bytearray(b"commit refs/heads/%s\ncommitter %s <%s> 0 +0000\ndata %d\n" % (
            BRANCH.encode(), _IDENTITY.encode(), _EMAIL.encode(), len(msg)) + msg)
        for path, mode, sha in manifest.entries:
            body = blobs[sha]
            stream += b"M %s inline %s\ndata %d\n" % (mode.encode(), _fi_path(path), len(body)) + body + b"\n"
        _git(dest_dir, "fast-import", "--quiet", data=bytes(stream))
        got = _git(dest_dir, "rev-parse", f"refs/heads/{BRANCH}").decode("ascii").strip()
        if got != want:
            raise SnapshotRefused("snapshot-commit-mismatch", "the built commit is not the manifest's derived one")
    except BaseException:
        _remove(dest_dir, made)
        raise
    return ReadSet(repo_dir=dest_dir, branch=BRANCH, commit=got, tree=tree), manifest
