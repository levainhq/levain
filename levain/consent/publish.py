"""levain.consent.publish — the effect half of ``levain publish``: the only code in levain that uploads a package
to PyPI or npm under an answer.

What the operator is asked about is read out of the artifact, never from flags: a wheel's ``*.dist-info/METADATA``,
an sdist's ``PKG-INFO``, an npm tarball's ``package/package.json``. The files are first copied into a levain-owned
snapshot (``<levain home>/publish/<id>/``, 0700) and everything after that reads the copy: the identity, the sha256
the request is filed on, and the bytes the upload sends. An agent that edits the original after asking changes
nothing; one that edits the copy is refused, because :func:`execute` re-hashes every file under the lock.

- :func:`canonical_target`: the one spelling of an index URL a request names (https only, except plain http to
  ``127.0.0.1`` / ``localhost`` for a local test registry; no userinfo, query or fragment).
- :func:`snapshot`: copy, bound and read the artifacts into the request's ``tool_input``.
- :func:`execute`: the fresh judgment under the per-target lock, the re-hash, an intent journal line, the upload
  (``twine upload`` / ``npm publish``) in an environment built clean, and the receipt.

The upload's environment (:func:`publish_env`) is :func:`levain.launch.child_env` plus the credentials the tools
read (``TWINE_USERNAME``, ``TWINE_PASSWORD``, ``TWINE_NON_INTERACTIVE``, ``NODE_AUTH_TOKEN``) and the proxies, with
every variable that could move the upload elsewhere removed: ``TWINE_REPOSITORY``/``_URL``/``TWINE_CONFIG_FILE``,
every ``npm_config_*``, ``PYTHON*``, ``LD_*``/``DYLD_*``, ``VIRTUAL_ENV``, and the CA-bundle overrides (a proxy with
a CA of the caller's choosing could otherwise answer for the index). ``PATH`` is :data:`PUBLISH_PATH`. An upload to
a local target gets no proxy at all. npm is also told, on its command line, the target for the package's scope, an
empty default scope, the dist-tag and access the card shows, ``--dry-run=false`` and ``--strict-ssl=true``, since
each of those in ``~/.npmrc``, the global npmrc or the tarball's ``publishConfig`` beats the default otherwise (npm
11.19.0, ``pickRegistry``; command-line flags beat ``publishConfig``). With ``--repository-url`` twine does not read
``~/.pypirc`` (twine 6.2.0, ``get_repository_from_config``), so :func:`pypi_credentials` fills whichever of the
username and password the environment lacks from the section naming exactly the card's target, and hands only those
to twine; a pair still incomplete is refused before anything is filed.

Known edges (one line each, not built against):

- PyPI credentials kept only in the keyring are not found: the publish is refused before it is filed, naming
  ``TWINE_USERNAME`` / ``TWINE_PASSWORD``.
- a twine too old for the artifact's Metadata-Version refuses it after the answer; its output is shown, nothing else.
- one publish names at most :data:`MAX_FILES` files (what the card shows whole): a wheel per platform goes in batches.
- twine run by levain's interpreter still loads the user's site-packages (``~/.local/lib``), the user's to write (R-k).
- npm reads its global npmrc (``<prefix>/etc/npmrc``, user-writable under Homebrew) for every setting the command
  line does not pin, auth and proxies included (R-k).
- a ``publishConfig`` setting other than the ones pinned above (a proxy, a CA, ``otp``) still reaches npm.
- npm and twine are found on :data:`PUBLISH_PATH`, whose Homebrew and ``~/.local/bin`` entries are the user's own
  to write: a binary replaced there runs (R-k; this is a path, not a boundary).
- an npm installed only under a version manager (``~/.nvm``) is not found: refused with "install npm".
- npm reads the operator's ``~/.npmrc`` for auth; a token there scoped wider than the package is the operator's.
- a TLS-intercepting proxy that needs its own CA bundle fails the upload (the CA overrides are dropped).
- the re-hash and the tool's read of the copy are two opens: a process of this uid that rewrites the copy between
  them is not caught (R-k).
- a non-zero twine exit on a multi-file publish is reported unknown (exit 4), even when it failed before the first file.
- ``levain publish`` removes the snapshot after every run, so an interrupted upload leaves only its journal and receipt lines.
- a second signal while the first one's unknown outcome is being recorded can still end the run before it reports.
- a run that waits on the per-package lock behind uploads totalling over :data:`SNAP_STALE` can have its snapshot
  swept meanwhile: it is then refused before the upload ("not levain's own private directory"), nothing published.
- a tarball holding a bare ``.`` or ``./`` entry (``tar -C dir .``) is refused, as is a wheel file whose name merely
  contains ``METADATA`` (``pkg/METADATA_helpers.py``): twine would weigh it as the metadata. Repack without them.

Stdlib only.
"""
from __future__ import annotations

import configparser
import email.parser
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from levain import unshowable
from levain.consent.push import TRUSTED_PATH, _fsync_dir, _private_dir, append_line
from levain.launch import NETWORK, child_env

__all__ = ["DEFAULT_TARGETS", "INDEXES", "MAX_ARTIFACT", "MAX_FILES", "PUBLISH_PATH", "PublishOutcomeUnknown",
           "PublishRecordIncomplete", "PublishRefused", "PublishResult", "canonical_target", "execute", "publish_argv",
           "publish_env", "pypi_credentials", "resolve_tool", "snapshot", "target_key"]

INDEXES = ("pypi", "npm")
DEFAULT_TARGETS = {"pypi": "https://upload.pypi.org/legacy/", "npm": "https://registry.npmjs.org/"}
# The largest artifact levain copies (PyPI's default upload limit is 100 MB), and the most bytes an archive may unpack to
# while its members are checked: a gzip bomb stops here instead of at the disk.
MAX_ARTIFACT = 100 * 1024 * 1024
# The most all of one publish's files may total, checked before anything is copied.
MAX_TOTAL = 300 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
# The most files one publish names: what the operator's card shows whole (a wheel per platform is published in batches).
MAX_FILES = 8
# The most a METADATA / PKG-INFO / package.json may be.
MAX_MANIFEST = 1024 * 1024
# The most an upload may run before it is reported unknown.
UPLOAD_TIMEOUT = 1800
# A snapshot directory older than this is a run that was killed (SIGKILL, a crash) and is swept at the next snapshot. A
# live run is younger: the hold ceiling (840 s) and a wait on the per-package lock come before :func:`execute`, which
# touches the directory when it takes the lock and again just before the upload (at most UPLOAD_TIMEOUT).
SNAP_STALE = 3600
# Where twine and npm are looked up, and the PATH their children see: the system's, then Homebrew's and pipx's.
PUBLISH_PATH = ":".join((TRUSTED_PATH, "/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")))
# The credential names each tool reads, carried past the allowlist.
CREDENTIALS = ("TWINE_USERNAME", "TWINE_PASSWORD", "TWINE_NON_INTERACTIVE", "NODE_AUTH_TOKEN")
# The proxies an upload may need; never the CA-bundle overrides beside them in NETWORK.
_PROXIES = tuple(n for n in NETWORK if n.upper().endswith("_PROXY"))
_LOCAL_NO_PROXY = "127.0.0.1,localhost"
# The section twine reads for a repository named only by its section, when ~/.pypirc gives it no ``repository``.
_PYPIRC_DEFAULTS = {"pypi": "https://upload.pypi.org/legacy/", "testpypi": "https://test.pypi.org/legacy/"}
# What could move an upload, dropped even when the allowlist would carry it.
_REDIRECTS = ("TWINE_REPOSITORY", "TWINE_REPOSITORY_URL", "TWINE_CONFIG_FILE", "VIRTUAL_ENV")
_REDIRECT_PREFIXES = ("NPM_CONFIG_", "PYTHON", "LD_", "DYLD_")

_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_PATH_OK = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
_LOCAL = frozenset({"127.0.0.1", "localhost"})
_NPM_NAME = re.compile(r"^(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")
_PY_NAME = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NPM_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NPM_ACCESS = ("default", "public", "restricted")
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


class PublishRefused(ValueError):
    """A publish levain will not run. Raised before any upload; the message says why."""


class PublishOutcomeUnknown(RuntimeError):
    """The upload started and did not report (it timed out, or could not be run): the index may or may not have
    the release. The journal's ``done`` line and the receipt carry status ``"unknown"`` as far as each could be
    written; ``record_errors`` names any that failed."""

    def __init__(self, msg: str, result: "PublishResult", record_errors: list[str]):
        super().__init__(msg)
        self.result = result
        self.record_errors = record_errors


class PublishRecordIncomplete(RuntimeError):
    """The upload ran and reported (``result``), but the ``done`` journal line or the receipt could not be
    written. The effect on the index is what ``result`` says."""

    def __init__(self, msg: str, result: "PublishResult", record_errors: list[str]):
        super().__init__(msg)
        self.result = result
        self.record_errors = record_errors


@dataclass(frozen=True)
class PublishResult:
    """What one answered publish did: ``status`` is ``"published"`` (the tool exited 0), ``"failed"`` (it exited
    non-zero; ``detail`` is the tail of its output) or ``"unknown"`` (only in a :class:`PublishOutcomeUnknown`).
    ``tag`` and ``access`` are an npm publish's, ``None`` for PyPI."""

    ok: bool
    status: str
    index: str
    target: str
    package: str
    version: str
    files: list[dict[str, Any]]
    exit: int
    detail: str
    tag: str | None = None
    access: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------------------
# target and fields


def _showable(name: str, text: str) -> None:
    found = unshowable.first_hidden(text, allow="")
    if found is not None:
        raise PublishRefused(f"the {name} carries an invisible or direction-changing character "
                             f"(U+{ord(found[1]):04X}); the operator could not read what he is asked to allow")


def canonical_target(url: str) -> str:
    """The one spelling of an index URL: lower-case scheme and host, https's :443 dropped, an empty path ``/``.
    https only, except ``http://127.0.0.1`` / ``http://localhost`` (a local test registry). Refused: userinfo, a
    query or fragment (even empty), a non-ASCII or percent-encoded URL, and anything not plainly a host and path."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise PublishRefused("the target must be a URL")
    if not url.isascii() or any(c in url for c in "?#%\\ ") or any(ord(c) < 0x21 or ord(c) == 0x7f for c in url):
        raise PublishRefused(f"the target {url!r} must be a plain ASCII URL with no query, fragment, escape or space")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise PublishRefused(f"the target {url!r} is not a URL: {exc}") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if "@" in parts.netloc:
        raise PublishRefused("the target may not carry a user name or password (credentials stay in their own files)")
    if not host or not _HOST.fullmatch(host):
        raise PublishRefused(f"the target {url!r} names no plain host")
    if scheme == "http" and host not in _LOCAL:
        raise PublishRefused(f"the target must be https (plain http only to {' / '.join(sorted(_LOCAL))})")
    if scheme not in ("https", "http"):
        raise PublishRefused(f"the target must be https, not {scheme or 'no'} scheme")
    path = parts.path or "/"
    if not _PATH_OK.fullmatch(path) or "/../" in path + "/" or "/./" in path + "/":
        raise PublishRefused(f"the target path {path!r} is not a plain path")
    hostport = host if port is None or (scheme == "https" and port == 443) else f"{host}:{port}"
    return f"{scheme}://{hostport}{path}"


def target_key(index: str, target: str, package: str) -> str:
    """The per-target-package lock and journal name."""
    return hashlib.sha256("\0".join((index, target, package)).encode()).hexdigest()


def _check_call(call: Any) -> None:
    """``call`` is a publish request's ``tool_input`` in the shape :func:`snapshot` makes."""
    base = {"index", "target", "package", "version", "files"}
    ok = (isinstance(call, dict) and call.get("index") in INDEXES
          and set(call) == (base | {"tag", "access"} if call["index"] == "npm" else base)
          and all(isinstance(call[k], str) for k in ("target", "package", "version"))
          and isinstance(call["files"], list) and call["files"])
    if ok and call["index"] == "npm":
        ok = (isinstance(call["tag"], str) and bool(_NPM_TAG.fullmatch(call["tag"]))
              and call["access"] in _NPM_ACCESS)
    if ok:
        for f in call["files"]:
            ok = ok and (isinstance(f, dict) and set(f) == {"name", "sha256", "size"} and isinstance(f["name"], str)
                         and bool(_FILE_NAME.fullmatch(f["name"])) and isinstance(f["sha256"], str)
                         and bool(_SHA256.fullmatch(f["sha256"])) and isinstance(f["size"], int)
                         and not isinstance(f["size"], bool) and 0 <= f["size"] <= MAX_ARTIFACT)
    if not ok or (call["index"] == "npm" and len(call["files"]) != 1):
        raise PublishRefused("the publish request is not in the shape levain files")
    if canonical_target(call["target"]) != call["target"]:
        raise PublishRefused("the publish target is not canonical")
    if len({f["name"] for f in call["files"]}) != len(call["files"]):
        raise PublishRefused("two files of one publish share a name")


# ---------------------------------------------------------------------------------------------------------------
# artifact identity (read from the snapshot copy)


def _member_problem(name: str, *, link: bool = False) -> str | None:
    """Why the archive member path (or, with ``link``, a link's target) is refused, else ``None``. A member path
    may hold no empty or ``.`` segment: npm, pacote, bsdtar and twine's readers normalise ``a//b`` and ``a/./b``
    onto ``a/b``, so such a member is a second copy of the one levain read, and the later one is what they take."""
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        return "an absolute or unreadable member path"
    parts = name.split("/")
    if ".." in parts:
        return "a member path that climbs out of the archive"
    if not link and any(part in ("", ".") for part in parts):
        return "a member path with an empty or '.' segment (another tool reads it as another member's path)"
    return None


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _headers(raw: bytes, what: str) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PublishRefused(f"the {what} is not UTF-8") from None
    msg = email.parser.HeaderParser().parsestr(text)
    names, versions = msg.get_all("Name") or [], msg.get_all("Version") or []
    if len(names) != 1 or len(versions) != 1:
        raise PublishRefused(f"the {what} must name exactly one Name and one Version")
    return str(names[0]).strip(), str(versions[0]).strip()


def _wheel(path: Path, file_name: str) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_MEMBERS:
                raise PublishRefused(f"{file_name} has more than {MAX_MEMBERS} members")
            meta = []
            for info in infos:
                # a directory entry ends in one "/"
                name = info.filename[:-1] if info.filename.endswith("/") else info.filename
                if problem := _member_problem(name):
                    raise PublishRefused(f"{file_name}: {problem} ({info.filename[:120]!r})")
                if "METADATA" not in name:
                    continue
                # twine reads, fewest path segments first, the first member whose path contains METADATA and that
                # holds a Metadata-Version line (twine 6.2.0, wheel.py): any other such member could be what it sends
                parts = name.split("/")
                if not (len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "METADATA"):
                    raise PublishRefused(f"{file_name}: the member {name[:120]!r} names METADATA outside the one "
                                         "*.dist-info/METADATA (twine could read it instead)")
                meta.append(info)
            if len(meta) != 1:
                raise PublishRefused(f"{file_name} has {len(meta)} top-level *.dist-info/METADATA, not one")
            if meta[0].file_size > MAX_MANIFEST:
                raise PublishRefused(f"{file_name}: METADATA is over {MAX_MANIFEST} bytes")
            with zf.open(meta[0]) as fh:
                raw = fh.read(MAX_MANIFEST + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, OSError, zlib.error,
            RuntimeError) as exc:                      # RuntimeError: an encrypted member
        raise PublishRefused(f"{file_name} is not a readable wheel: {type(exc).__name__}: {exc}") from None
    if len(raw) > MAX_MANIFEST:
        raise PublishRefused(f"{file_name}: METADATA is over {MAX_MANIFEST} bytes")
    name, version = _headers(raw, f"METADATA of {file_name}")
    # {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl, the name and version escaped with underscores
    stem = file_name[:-len(".whl")].split("-")
    if len(stem) not in (5, 6) or _norm(stem[0]) != _norm(name) or stem[1].lower() != version.replace("-", "_").lower():
        raise PublishRefused(f"{file_name} does not name {name} {version}, which its METADATA does")
    return name, version


class _Bounded:
    """A read-only stream that raises :class:`PublishRefused` past ``limit`` bytes read."""

    def __init__(self, raw: Any, limit: int, what: str) -> None:
        self.raw, self.left, self.what = raw, limit, what

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n if n is not None and n >= 0 else self.left + 1)
        self.left -= len(data)
        if self.left < 0:
            raise PublishRefused(f"{self.what} unpacks to more than {MAX_UNPACKED} bytes")
        return data


def _tar_manifest(path: Path, file_name: str, member_name: str, top: str | None = None) -> tuple[str, bytes]:
    """(the single top-level directory, the bytes of ``<that directory>/<member_name>``) of a gzip tarball, read as
    a bounded stream: every member's path is checked, a link member's target too. With ``top``, the directory must
    be that one."""
    found: dict[str, list[bytes]] = {}
    tops: set[str] = set()
    try:
        with gzip.open(path, "rb") as gz:
            with tarfile.open(fileobj=_Bounded(gz, MAX_UNPACKED, file_name), mode="r|") as tf:
                for count, member in enumerate(tf, 1):
                    if count > MAX_MEMBERS:
                        raise PublishRefused(f"{file_name} has more than {MAX_MEMBERS} members")
                    name = member.name.removeprefix("./")
                    if problem := _member_problem(name):
                        raise PublishRefused(f"{file_name}: {problem} ({member.name[:120]!r})")
                    if (member.issym() or member.islnk()) and (problem := _member_problem(member.linkname,
                                                                                          link=True)):
                        raise PublishRefused(f"{file_name}: a link to {problem} ({member.linkname[:120]!r})")
                    parts = name.rstrip("/").split("/")
                    tops.add(parts[0])
                    if len(parts) == 2 and parts[1] == member_name:
                        if not member.isfile():
                            raise PublishRefused(f"{file_name}: {name} is not a regular file")
                        if member.size > MAX_MANIFEST:
                            raise PublishRefused(f"{file_name}: {name} is over {MAX_MANIFEST} bytes")
                        fh = tf.extractfile(member)
                        found.setdefault(parts[0], []).append(fh.read(MAX_MANIFEST + 1) if fh is not None else b"")
    except PublishRefused:
        raise
    except (tarfile.TarError, OSError, EOFError, gzip.BadGzipFile, ValueError, zlib.error) as exc:
        raise PublishRefused(f"{file_name} is not a readable gzip tarball: {type(exc).__name__}: {exc}") from None
    if len(tops) != 1:
        raise PublishRefused(f"{file_name} has {len(tops)} top-level entries, not one directory")
    got = tops.pop()
    if top is not None and got != top:
        raise PublishRefused(f"{file_name} unpacks into {got!r}, not {top}/")
    if len(raws := found.get(got, [])) != 1:
        raise PublishRefused(f"{file_name} has {len(raws)} {got}/{member_name}, not one")
    return got, raws[0]


def _sdist(path: Path, file_name: str) -> tuple[str, str]:
    top, raw = _tar_manifest(path, file_name, "PKG-INFO")
    name, version = _headers(raw, f"PKG-INFO of {file_name}")
    # {name}-{version}.tar.gz holding {name}-{version}/PKG-INFO (PEP 625)
    if top != file_name[:-len(".tar.gz")]:
        raise PublishRefused(f"{file_name} unpacks into {top!r}, not a directory of its own name")
    t_name, _, t_version = top.rpartition("-")
    if _norm(t_name) != _norm(name) or t_version.lower() != version.lower():
        raise PublishRefused(f"{file_name} does not name {name} {version}, which its PKG-INFO does")
    return name, version


def _npm(path: Path, file_name: str) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """(name, version, the dist-tag and access the publish takes) from ``package/package.json``: the tag is
    ``publishConfig.tag`` (default ``latest``), the access ``publishConfig.access`` (default ``default``, the
    registry's own). Both are shown and pinned on npm's command line, which beats ``~/.npmrc`` and the manifest."""
    _top, raw = _tar_manifest(path, file_name, "package.json", top="package")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PublishRefused(f"package/package.json of {file_name} is not UTF-8 JSON") from None
    name = manifest.get("name") if isinstance(manifest, dict) else None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(name, str) or not isinstance(version, str):
        raise PublishRefused(f"package/package.json of {file_name} names no name and version")
    if len(name) > 214 or not _NPM_NAME.fullmatch(name):
        raise PublishRefused(f"{name[:80]!r} is not an npm package name")
    config = manifest.get("publishConfig")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise PublishRefused(f"the publishConfig of {file_name} is not an object")
    tag, access = config.get("tag", "latest"), config.get("access")
    if not isinstance(tag, str) or not _NPM_TAG.fullmatch(tag):
        raise PublishRefused(f"the publishConfig tag of {file_name} is not a dist-tag levain shows")
    if access is None:                           # absent or null: the registry's default
        access = "default"
    elif access not in ("public", "restricted"):
        raise PublishRefused(f"the publishConfig access of {file_name} is not public or restricted")
    return name, version, (("access", access), ("tag", tag))


def _identity(index: str, path: Path, file_name: str) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """(name, version, the index-specific fields the card shows: npm's tag and access, none for PyPI)."""
    extra: tuple[tuple[str, str], ...] = ()
    if index == "npm":
        if not file_name.endswith(".tgz"):
            raise PublishRefused(f"{file_name}: an npm publish takes one .tgz (npm pack's output)")
        name, version, extra = _npm(path, file_name)
    elif file_name.endswith(".whl"):
        name, version = _wheel(path, file_name)
    elif file_name.endswith(".tar.gz"):
        name, version = _sdist(path, file_name)
    else:
        raise PublishRefused(f"{file_name}: PyPI takes a .whl or a .tar.gz")
    _showable("package name", name)
    _showable("version", version)
    if index == "pypi" and not _PY_NAME.fullmatch(name):
        raise PublishRefused(f"{name[:80]!r} is not a Python project name")
    if not _VERSION.fullmatch(version):
        raise PublishRefused(f"{version[:80]!r} is not a version levain shows")
    return name, version, extra


# ---------------------------------------------------------------------------------------------------------------
# the snapshot


def _sha256(path: Path) -> tuple[str, int]:
    """(sha256, size) of the regular file at ``path``, opened without following a link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PublishRefused(f"{path} is not a regular file")
        h, size = hashlib.sha256(), 0
        while chunk := os.read(fd, 1 << 20):
            h.update(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    return h.hexdigest(), size


def _copy_in(src: str, dst: Path, limit: int = MAX_ARTIFACT) -> int:
    """Copy the regular file ``src`` (not a link, at most :data:`MAX_ARTIFACT`) to the new file ``dst`` (0600)."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise PublishRefused(f"{src}: could not open it as a file (a link is refused): {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PublishRefused(f"{src} is not a regular file")
        if st.st_size > limit:
            raise PublishRefused(f"{src} is over {limit} bytes (the per-file or the remaining total cap)")
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            copied = 0
            while chunk := os.read(fd, 1 << 20):
                copied += len(chunk)
                if copied > limit:
                    raise PublishRefused(f"{src} grew past {limit} bytes while it was copied (the per-file or "
                                         "the remaining total cap)")
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view):]
            os.fsync(out)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    return copied


def _sweep(publish_dir: Path) -> None:
    """Remove the snapshot directories under ``publish_dir`` older than :data:`SNAP_STALE` (a run killed with
    SIGKILL, or a crash, leaves its copy behind). Best effort: a directory that cannot be read is left."""
    cutoff = time.time() - SNAP_STALE
    try:
        entries = list(os.scandir(publish_dir))
    except OSError:
        return
    for entry in entries:
        try:
            if (entry.name.startswith("snap-") and entry.is_dir(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


def snapshot(index: str, paths: list[str], target: str, levain_home: str | os.PathLike) -> tuple[dict[str, Any], Path]:
    """Copy ``paths`` into a new ``<levain home>/publish/<id>/`` and read them there: ``(call, snapshot dir)``,
    ``call`` being the request's ``tool_input`` (index, canonical target, package, version, and each file's name,
    sha256 and size, sorted by name; for npm also the dist-tag and access). Every file of one publish names the same
    project and version. The count, each size and the total are checked before anything is copied; a snapshot directory a killed
    run left (older than :data:`SNAP_STALE`) is swept first. The directory is removed when anything is refused."""
    if index not in INDEXES:
        raise PublishRefused(f"the index must be one of {INDEXES}")
    canon = canonical_target(target)
    if not paths or (index == "npm" and len(paths) != 1):
        raise PublishRefused("a PyPI publish takes one or more files, an npm publish exactly one tarball")
    if len(paths) > MAX_FILES:
        raise PublishRefused(f"one publish takes at most {MAX_FILES} files (what the operator is shown whole); "
                             "publish the rest in another batch")
    names = [os.path.basename(p) for p in paths]
    for n in names:
        if not _FILE_NAME.fullmatch(n):
            raise PublishRefused(f"the file name {n[:80]!r} is not plain (letters, digits, . _ + -)")
    if len(set(names)) != len(names):
        raise PublishRefused("two files share a name")
    total = 0
    for src in paths:
        try:
            st = os.lstat(src)
        except OSError as exc:
            raise PublishRefused(f"{src}: could not read it: {exc.strerror}") from None
        if stat.S_ISLNK(st.st_mode):
            raise PublishRefused(f"{src}: a link is refused (name the file itself)")
        if not stat.S_ISREG(st.st_mode):
            raise PublishRefused(f"{src} is not a regular file")
        if st.st_size > MAX_ARTIFACT:
            raise PublishRefused(f"{src} is over {MAX_ARTIFACT} bytes")
        total += st.st_size
    if total > MAX_TOTAL:
        raise PublishRefused(f"the files are over {MAX_TOTAL} bytes together; publish them in batches")
    home = Path(os.path.abspath(os.fspath(levain_home)))
    if not os.path.isdir(home):
        raise PublishRefused(f"levain home {home} is not a directory")
    _private_dir(home / "publish")
    _sweep(home / "publish")
    snap = home / "publish" / f"snap-{os.urandom(12).hex()}"
    _private_dir(snap)
    try:
        files, idents = [], set()
        left = MAX_TOTAL          # counted from the bytes actually copied: a file that grows after the check spends it
        for src, name in zip(paths, names):
            dst = snap / name
            left -= _copy_in(src, dst, min(MAX_ARTIFACT, left))
            # the identity is read between two hashes of the copy, so what is shown is what is filed
            digest, size = _sha256(dst)
            idents.add(_identity(index, dst, name))
            if _sha256(dst) != (digest, size):
                raise PublishRefused(f"{name} changed in the snapshot while it was read")
            files.append({"name": name, "sha256": digest, "size": size})
        if len(idents) != 1:
            raise PublishRefused("the files name different projects or versions: "
                                 + ", ".join(f"{n} {v}" for n, v, _ in sorted(idents)))
        _fsync_dir(snap)
        package, version, extra = idents.pop()
        call = {"index": index, "target": canon, "package": package, "version": version,
                "files": sorted(files, key=lambda f: f["name"]), **dict(extra)}
        _check_call(call)
        return call, snap
    except BaseException:
        shutil.rmtree(snap, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------------------------------------------
# the upload


def _is_local(target: str) -> bool:
    return urlsplit(target).hostname in _LOCAL


def pypi_credentials(target: str) -> dict[str, str]:
    """The PyPI credentials the upload to ``target`` gets beyond the environment's own. ``TWINE_USERNAME`` and
    ``TWINE_PASSWORD`` both in the environment win: nothing is added. Otherwise the ``~/.pypirc`` section whose
    ``repository`` is exactly ``target`` once canonical (``[pypi]`` / ``[testpypi]`` without one name twine's
    defaults) fills whichever of the two the environment lacks. Only those two are read: the file never chooses the
    target, and twine is never handed it (with ``--repository-url`` twine ignores it). Raises
    :class:`PublishRefused`, so before anything is filed, when the pair is still incomplete, or two sections give
    the target different ones; the keyring is not consulted. A password is never part of a message."""
    env_user, env_pass = os.environ.get("TWINE_USERNAME", ""), os.environ.get("TWINE_PASSWORD", "")
    if env_user and env_pass:
        return {}
    found: set[tuple[str, str]] = set()
    path = Path(os.path.expanduser("~")) / ".pypirc"
    parser = configparser.RawConfigParser()      # twine's own reader: no % interpolation in a password
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError, OSError):
        parser = configparser.RawConfigParser()  # an unreadable file gives no credential
    for section in parser.sections():
        repository = parser.get(section, "repository", fallback=_PYPIRC_DEFAULTS.get(section))
        if section == "distutils" or not repository:
            continue
        try:
            canon = canonical_target(repository.strip())
        except PublishRefused:
            continue
        if canon == target:   # only the halves the environment lacks can conflict
            found.add(("" if env_user else parser.get(section, "username", fallback="").strip(),
                       "" if env_pass else parser.get(section, "password", fallback="")))
    if len(found) > 1:
        raise PublishRefused(f"two sections of {path} give {target} different credentials; keep one, or set "
                             "TWINE_USERNAME / TWINE_PASSWORD in the environment")
    username, password = found.pop() if found else ("", "")
    creds = {}
    if not env_user and username:
        creds["TWINE_USERNAME"] = username
    if not env_pass and password:
        creds["TWINE_PASSWORD"] = password
    if not (env_user or username) or not (env_pass or password):
        missing = " and ".join(n for n, have in (("TWINE_USERNAME", env_user or username),
                                                 ("TWINE_PASSWORD", env_pass or password)) if not have)
        raise PublishRefused(f"no complete PyPI credential for {target} ({missing} missing): set "
                             f"TWINE_USERNAME=__token__ and TWINE_PASSWORD=<an API token> in the environment, or give "
                             f"{path} a section whose repository is exactly {target} with a username and password "
                             "(the keyring is not consulted)")
    return creds


def publish_env(call: dict[str, Any] | None = None) -> dict[str, str]:
    """The upload's environment: the allowlist plus :data:`CREDENTIALS` and the proxy variables, without any
    variable that could move the upload (see the module docstring), ``PATH`` = :data:`PUBLISH_PATH`, colour off.
    With ``call``: a PyPI upload also gets :func:`pypi_credentials`, and an upload to a local target runs with no
    proxy at all (``NO_PROXY`` = the loopback names; requests does not bypass a proxy for loopback itself)."""
    env = child_env(*CREDENTIALS, *_PROXIES)
    env = {k: v for k, v in env.items()
           if k not in _REDIRECTS and not k.upper().startswith(_REDIRECT_PREFIXES)}
    env["PATH"] = PUBLISH_PATH
    env["TWINE_NON_INTERACTIVE"] = "1"
    env["NO_COLOR"] = "1"
    if call is not None:
        if call["index"] == "pypi":
            env.update(pypi_credentials(call["target"]))
        if _is_local(call["target"]):
            env = {k: v for k, v in env.items() if not k.upper().endswith("_PROXY")}
            env["NO_PROXY"] = env["no_proxy"] = _LOCAL_NO_PROXY
    return env


def _clean(text: str) -> str:
    """``text`` without terminal escapes or invisible characters (a tool's coloured output stays readable in the
    receipt and on the agent's terminal). A space other than U+0020 becomes U+0020, so two words it separated stay
    two words."""
    text = _ESCAPES.sub("", text)
    return unshowable.mark(text, lambda cp: " " if unicodedata.category(chr(cp)) == "Zs" else "", allow="\n\t")


def resolve_tool(index: str) -> list[str]:
    """The command that uploads for ``index``: levain's own interpreter running twine when it imports twine, else
    a ``twine`` on :data:`PUBLISH_PATH`; ``npm`` on :data:`PUBLISH_PATH`. Raises :class:`PublishRefused` naming
    what to install."""
    if index == "pypi":
        if importlib.util.find_spec("twine") is not None:
            return [sys.executable, "-E", "-P", "-m", "twine"]
        if got := shutil.which("twine", path=PUBLISH_PATH):
            return [os.path.abspath(got)]
        raise PublishRefused(f"no twine: install it (pipx install twine) so levain can upload; looked in levain's "
                             f"interpreter and {PUBLISH_PATH}")
    if got := shutil.which("npm", path=PUBLISH_PATH):
        return [os.path.abspath(got)]
    raise PublishRefused(f"no npm on {PUBLISH_PATH}: install npm so levain can publish")


def publish_argv(tool: list[str], call: dict[str, Any], snap: Path) -> list[str]:
    """The upload's whole argv: ``tool`` (from :func:`resolve_tool`) and the snapshot's files, to the call's target
    only."""
    files = [str(snap / f["name"]) for f in call["files"]]
    if call["index"] == "pypi":
        return [*tool, "upload", "--non-interactive", "--disable-progress-bar", "--repository-url", call["target"],
                *files]
    # the registry, scope, dist-tag, access, dry-run and TLS check are pinned here: the command line beats
    # ~/.npmrc, the global npmrc and the tarball's publishConfig (npm 11.19.0)
    access = "null" if call["access"] == "default" else call["access"]
    argv = [*tool, "publish", files[0], f"--registry={call['target']}", "--scope=", f"--tag={call['tag']}",
            f"--access={access}", "--dry-run=false", "--strict-ssl=true", "--ignore-scripts"]
    if _is_local(call["target"]):
        argv += ["--proxy=null", "--https-proxy=null", f"--noproxy={_LOCAL_NO_PROXY}"]
    if call["package"].startswith("@"):
        argv.append(f"--{call['package'].split('/', 1)[0]}:registry={call['target']}")
    return argv


Refusal = Callable[[dict[str, Any]], "str | None"]


def _record(home: Path, key: str, result: PublishResult, receipt: Callable[[dict[str, Any]], Any],
            done: set[str]) -> list[str]:
    """Write the ``done`` journal line and the receipt, skipping either already in ``done`` and adding each to it
    once tried. Anything raised, an interruption included (a ``KeyboardInterrupt``, a signal the caller's handler
    raises), is a record error, so the other record is still written and the caller still learns what the upload
    did."""
    errs = []
    for what, write in (("done journal", lambda: append_line(home / "publish" / f"{key}.journal.jsonl",
                                                             {"state": "done", "at": time.time(),
                                                              **result.to_dict()})),
                        ("receipt", lambda: receipt(result.to_dict()))):
        if what in done:
            continue
        try:
            write()
            done.add(what)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller as a record error, never swallowed
            done.add(what)
            errs.append(f"{what}{'' if isinstance(exc, Exception) else ' interrupted'}: {type(exc).__name__}: {exc}")
    return errs


def _outcome(call: dict[str, Any], base: dict[str, Any], p: subprocess.CompletedProcess) -> PublishResult:
    """What the tool's exit says: ``status`` ``"unknown"`` for a non-zero twine exit on more than one file (the first
    may be on the index already)."""
    tail = _clean((p.stderr or "").strip() or (p.stdout or "").strip()).strip()[-400:]
    if p.returncode != 0 and call["index"] == "pypi" and len(call["files"]) > 1:
        return PublishResult(ok=False, status="unknown", exit=p.returncode,
                             detail=f"twine exited {p.returncode} part way through {len(call['files'])} files: "
                                    f"{tail}"[-400:], **base)
    ok = p.returncode == 0
    return PublishResult(ok=ok, status="published" if ok else "failed", exit=p.returncode, detail="" if ok else tail,
                         **base)


def _touch(snap: Path) -> None:
    """Mark the snapshot live, so a concurrent snapshot's sweep does not take it (best effort)."""
    try:
        os.utime(snap, follow_symlinks=False)
    except OSError:
        pass


def _upload(call: dict[str, Any], base: dict[str, Any], argv: list[str], env: dict[str, str], snap: Path, home: Path,
            key: str, receipt: Callable[[dict[str, Any]], Any], run: Callable[..., subprocess.CompletedProcess]
            ) -> PublishResult:
    """The intent line, the upload and its record, under one ``try``: whatever stops this, a signal included, between
    the intent line and the return reaches the caller as :class:`PublishOutcomeUnknown` (the tool's exit not yet
    known) or :class:`PublishRecordIncomplete` (known: its result is kept), never as itself. An ``OSError`` writing
    the intent line propagates as itself: nothing was run."""
    p: subprocess.CompletedProcess | None = None
    result: PublishResult | None = None
    done: set[str] = set()
    intent = False
    try:
        append_line(home / "publish" / f"{key}.journal.jsonl", {"state": "intent", "at": time.time(), **base})
        intent = True
        # from here on the index may change: every path reaches the receipt
        _touch(snap)
        p = run(argv, env=env, cwd=str(snap), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=UPLOAD_TIMEOUT)
        result = _outcome(call, base, p)
        errs = _record(home, key, result, receipt, done)
        final: Exception | None = (_unknown(call, result, errs) if result.status == "unknown" else
                                   PublishRecordIncomplete(f"the publish ran ({result.status}) but its record is "
                                                           f"incomplete: {'; '.join(errs)}", result, errs)
                                   if errs else None)
    except BaseException as exc:  # noqa: BLE001 - after the intent line any failure is reported, never raw
        if not intent and isinstance(exc, OSError):
            raise                                       # the intent line could not be written; nothing ran
        if p is not None and result is None:
            result = _outcome(call, base, p)
        if result is None:
            detail = _clean(f"the upload did not report: {type(exc).__name__}: {exc}")[:400]
            result = PublishResult(ok=False, status="unknown", exit=-1, detail=detail, **base)
            errs = _record(home, key, result, receipt, done)
        else:
            errs = _record(home, key, result, receipt, done)
            errs.append(f"interrupted after the upload reported: {type(exc).__name__}: {exc}")
        if result.status == "unknown":
            raise _unknown(call, result, errs) from exc
        raise PublishRecordIncomplete(f"the publish ran ({result.status}) but its record is incomplete: "
                                      f"{'; '.join(errs)}", result, errs) from exc
    if final is not None:     # built inside the try: only the raise or the return is left outside it
        raise final
    return result


def _unknown(call: dict[str, Any], result: PublishResult, errs: list[str]) -> PublishOutcomeUnknown:
    if result.exit == -1:
        msg = (f"the publish of {call['package']} {call['version']} to {call['target']} may or may not have landed "
               f"({result.detail})")
    else:
        msg = (f"the publish of {call['package']} {call['version']} to {call['target']} failed part way (twine exit "
               f"{result.exit}): some files may have landed; check the index before publishing again "
               f"({result.detail})")
    return PublishOutcomeUnknown(msg + (f"; record errors: {'; '.join(errs)}" if errs else ""), result, errs)


def execute(call: dict[str, Any], *, snap: str | os.PathLike, levain_home: str | os.PathLike, refusal: Refusal,
            receipt: Callable[[dict[str, Any]], Any], resolve: Callable[[str], list[str]] = resolve_tool,
            run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> PublishResult:
    """Upload the snapshot ``snap`` as ``call`` says. Under the per-target-package lock ``refusal(call)`` re-reads
    the decision against a fresh replay and returns ``None`` (still allowed) or why not; each file is re-hashed and
    must be the bytes the call names; the tool is resolved and the argv built; an intent line is journalled under
    ``<levain home>/publish/``; then the upload runs from the snapshot directory with :func:`publish_env`.

    Raises :class:`PublishRefused` for anything refused before the intent line (nothing was uploaded); an ``OSError``
    or an interruption before it propagates as itself, so whatever reaches the caller other than the classes below
    means nothing was uploaded. From the intent line on (see :func:`_upload`): an interruption anywhere before the
    tool's exit is known, or an upload that did not report, raises :class:`PublishOutcomeUnknown`, and so does a
    non-zero twine exit for more than one file (the first may have landed before the next failed); any other
    non-zero exit is a :class:`PublishResult` with ``ok`` False; a done line or receipt that could not be written,
    or an interruption once the exit is known, raises :class:`PublishRecordIncomplete` carrying that result. The
    tool's output reaches ``detail`` without terminal escapes. ``resolve`` and ``run`` are injectable for tests."""
    _check_call(call)
    snap = Path(os.path.abspath(os.fspath(snap)))
    home = Path(os.path.abspath(os.fspath(levain_home)))
    if not os.path.isdir(home):
        raise PublishRefused(f"levain home {home} is not a directory")
    key = target_key(call["index"], call["target"], call["package"])
    _private_dir(home / "publish")
    lock_fd = os.open(home / "publish" / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        st = os.lstat(snap)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise PublishRefused(f"the snapshot {snap} is not levain's own private directory")
        _touch(snap)              # live: a sweep by another run leaves it
        if (why := refusal(call)) is not None:
            raise PublishRefused(why)
        for f in call["files"]:
            if _sha256(snap / f["name"]) != (f["sha256"], f["size"]):
                raise PublishRefused(f"{f['name']} in the snapshot is not the file that was allowed (its bytes changed)")
        argv = publish_argv(resolve(call["index"]), call, snap)
        env = publish_env(call)
        base = dict(call)
        return _upload(call, base, argv, env, snap, home, key, receipt, run)
    finally:
        os.close(lock_fd)
