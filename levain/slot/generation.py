"""The install's generation and its record (row 33; DESIGN 1022-8/upgrade §1.1, §2.3).

* The **generation id** (:func:`generation_id`): content-addressed over the staged ``levain/`` and ``h11/``, so every
  code change moves it and nothing has to remember a version. Part of the install protocol: fixed before any host
  records one.
* ``L/policy/install-shape`` (:data:`SHAPE_FORMAT`): what the run that applied this install recorded: the generation,
  both versions, the ``txn`` that wrote it (REPAIR's only completion proof, §8 N1/N1a), each service's
  ``started_after`` threshold, and the typed ``managed`` list of what this install manages.
* The **kinds** (:data:`KINDS`): each ``managed`` record is ``(kind, identity)``. A parser accepts any well-formed
  kind token and keeps an unknown kind's identity opaque (:class:`Opaque`), so a package can READ a record naming a
  kind it lacks; :func:`check_managed` then refuses ``downgrade-unsupported:<kind>`` for such a kind and ``bad-record``
  for a known kind whose identity breaks its bound. The remove steps per kind are R1b's; this module holds data and
  checks only.

Root-side: imports and runs on the 3.9 floor (``tests/test_root_py39_floor.py``).
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping, NoReturn, Optional, Tuple

from levain.contracts import canon
from levain.firing.hands import OPERATOR_RE, HostOS
from levain.slot import Refused
from levain.slot.ids import RANGES, in_own_range, is_levain_account

SHAPE_FORMAT = "levain-install-shape/1"
GEN_RE = re.compile(r"gen-[0-9a-f]{16}")
TXN_RE = re.compile(r"[0-9a-f]{32}")
#: §2.2: any well-formed kind token parses; only a remove step interprets an identity, and only its own kind's.
KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,23}/[1-9][0-9]{0,2}")
#: The parsers' bound on one record's identity text (an unknown kind's identity is kept, never interpreted).
MAX_IDENTITY = 1024
MAX_MANAGED = 4096
#: The byte cap on reading a journal or an install-shape, the one value both reads pass (the same size as the runtime
#: helper's default store record cap): larger refuses ``record-too-large`` before it is read whole.
MAX_RECORD_BYTES = 1 << 20
_PYCACHE = "__pycache__"


def new_txn() -> str:
    """A fresh 128-bit run id (§2.3 R1)."""
    return secrets.token_hex(16)


# --- the generation id (§1.1) ------------------------------------------------------------------------------------


def _walk_error(e: OSError) -> NoReturn:
    raise Refused("bad-source", f"cannot read {e.filename} ({e.strerror or e})")


def _entries(top: Path, prefix: str) -> Iterator[Tuple[str, str, int, str]]:
    """``(type, relpath, x, sha256)`` for ``top`` and every entry below it. Every entry NAMED ``__pycache__`` (a
    directory, a file or a link) is dropped: the one rule staging's ``find -name __pycache__`` applies, so the staged
    copy and the source hash alike. A link or any other file type refuses ``bad-source``, as does anything that cannot
    be read: the id is taken over a tree of plain files and directories only."""
    try:
        st = os.lstat(top)
    except OSError as e:
        _walk_error(e)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("bad-source", f"{top} is not a real directory")
    yield ("d", prefix, 0, "")
    for dirpath, dirnames, filenames in os.walk(top, onerror=_walk_error):
        dirnames[:] = sorted(d for d in dirnames if d != _PYCACHE)
        rel_dir = os.path.relpath(dirpath, top)
        for name in dirnames + sorted(f for f in filenames if f != _PYCACHE):
            p = os.path.join(dirpath, name)
            rel = prefix + "/" + (name if rel_dir == "." else f"{rel_dir}/{name}")
            try:
                st = os.lstat(p)
                if stat.S_ISDIR(st.st_mode):
                    yield ("d", rel, 0, "")
                elif stat.S_ISREG(st.st_mode):
                    h = hashlib.sha256()
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 16), b""):
                            h.update(chunk)
                    yield ("f", rel, 1 if st.st_mode & 0o111 else 0, h.hexdigest())
                else:
                    raise Refused("bad-source", f"{p} is not a plain file or directory")
            except OSError as e:
                _walk_error(e)


def generation_id(levain_dir: Path, h11_dir: Path) -> str:
    """``gen-`` + the first 16 hex of sha256 over the sorted ``(type, relpath, x, sha256 or "")`` of EVERY entry of
    ``levain_dir`` and ``h11_dir`` (relpaths under ``levain/`` and ``h11/``), ``__pycache__`` dropped. ``x`` is 1 when a
    file carries any execute bit: exactly what staging keeps (``chmod -R u=rwX,go=rX``), so the staged copy and the
    operator's installed package yield the same id. Empty directories are entries (staging keeps them)."""
    rows = sorted([*_entries(Path(levain_dir), "levain"), *_entries(Path(h11_dir), "h11")])
    h = hashlib.sha256()
    for t, rel, x, digest in rows:
        h.update(f"{t}\0{rel}\0{x}\0{digest}\n".encode("utf-8", "surrogateescape"))
    return "gen-" + h.hexdigest()[:16]


_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([0-9A-Za-z.+!-]{1,40})["']\s*$""", re.M)


def read_version_text(path: Path) -> str:
    """A package's ``__version__`` read as TEXT from ``path`` (``h11/_version.py``, ``levain/__init__.py``): root never
    imports from a source it has not staged (§2.1). ``bad-source`` when the file holds no such line."""
    try:
        with open(path, "rb") as fh:
            text = fh.read(1 << 16).decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise Refused("bad-source", f"cannot read a version from {path} ({e})") from None
    m = _VERSION_RE.search(text)
    if m is None:
        raise Refused("bad-source", f"no __version__ line in {path}")
    return m.group(1)


# --- the managed records (§1.1 table) ---------------------------------------------------------------------------


class Opaque(str):
    """An unknown kind's identity, kept as its canonical JSON text: never interpreted, written back unchanged."""


def _freeze(x: object) -> object:
    if isinstance(x, list):
        return tuple(_freeze(v) for v in x)
    return x


def _thaw(x: object) -> object:
    if isinstance(x, Opaque):
        return canon.parse(str(x).encode("utf-8"))
    if isinstance(x, tuple):
        return [_thaw(v) for v in x]
    return x


Record = Tuple[str, object]


def parse_managed(obj: object, *, what: str) -> Tuple[Record, ...]:
    """A ``managed`` list: ``[[kind, identity], ...]``, every kind a well-formed token, no record twice. A known
    kind's identity is a string or a list of strings and ints (frozen to tuples); an unknown kind's identity is kept
    as :class:`Opaque`. Bounds are :func:`check_managed`'s, at plan time."""
    bad = Refused(f"{what}-malformed", f"not a {what} managed list")
    if not isinstance(obj, list) or len(obj) > MAX_MANAGED:
        raise bad
    out = []
    for rec in obj:
        if (not isinstance(rec, list) or len(rec) != 2 or not isinstance(rec[0], str)
                or not KIND_RE.fullmatch(rec[0])):
            raise bad
        kind, ident = rec
        text = canon.canonical(ident)
        if len(text) > MAX_IDENTITY:
            raise bad
        if kind in KINDS:
            ok = isinstance(ident, str) or (isinstance(ident, list) and all(
                isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool)) for v in ident))
            if not ok:
                raise bad
            out.append((kind, _freeze(ident)))
        else:
            out.append((kind, Opaque(text)))
    if len(set(out)) != len(out):
        raise bad
    return tuple(out)


def managed_json(records) -> list:
    return [[k, _thaw(i)] for k, i in sorted(records, key=lambda r: (r[0], canon.canonical(_thaw(r[1]))))]


def _id(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _re(pattern: str) -> Callable[[object], bool]:
    rx = re.compile(pattern)
    return lambda x: isinstance(x, str) and rx.fullmatch(x) is not None


_UNIT = _re(r"[a-z][a-z0-9-]{0,31}")
_SUDOERS = _re(r"levain-[a-z0-9-]{1,32}")
_GROUP = _re(r"levain-[a-z0-9-]{1,24}")
_ALIAS = _re(r"127\.0\.1\.(?:[1-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-4])")
_ANCHOR = _re(r"com\.apple/levain(?:-[a-z0-9-]{1,32})?")
_ANCHOR_FILE = _re(r"/etc/pf\.anchors/levain[a-z0-9-]{0,32}")
_TOKEN = _re(r"/var/db/levain/[a-z0-9][a-z0-9_-]{0,31}(?:\.[a-z0-9]{1,8})?")
_PROFILE = _re(r"levain-slot-[a-z0-9-]{1,32}")
_COPY = _re(r"/usr/libexec/levain/[a-z0-9][a-z0-9_-]{0,31}")
_DIGEST = _re(r"/var/lib/levain/[a-z0-9][a-z0-9_-]{0,31}(?:\.[a-z0-9]{1,8})?")
_NETNS = _re(r"levain-[ev][0-9]{1,3}")


def _shared(host: HostOS, name: object, gid: object) -> bool:
    return _GROUP(name) and _id(gid) and gid in RANGES[host].shared


def _pair(ident: object, n: int) -> bool:
    return isinstance(ident, tuple) and len(ident) == n


#: The 12 kinds and their BOUND (§1.1 table, last column): a record that fails its bound refuses ``bad-record``
#: before any act. ``(host, identity, operator) -> ok``. Rows in table order.
KINDS: Mapping[str, Callable[[HostOS, object, Optional[str]], bool]] = {
    "unit/1": lambda h, i, op: _UNIT(i),
    "sudoers/1": lambda h, i, op: _SUDOERS(i),
    "member/1": lambda h, i, op: (_pair(i, 3) and _shared(h, i[1], i[2]) and isinstance(i[0], str)  # type: ignore[index]
                                  and (is_levain_account(i[0]) or (op is not None and i[0] == op  # type: ignore[index]
                                                                   and OPERATOR_RE.fullmatch(op) is not None))),
    "group/1": lambda h, i, op: _pair(i, 2) and _shared(h, i[0], i[1]),                              # type: ignore[index]
    "account/1": lambda h, i, op: _pair(i, 2) and isinstance(i[0], str) and in_own_range(h, i[0], i[1]),  # type: ignore[index]
    "pf-alias/1": lambda h, i, op: _ALIAS(i),
    "pf-anchor/1": lambda h, i, op: _pair(i, 2) and _ANCHOR(i[0]) and _ANCHOR_FILE(i[1]),           # type: ignore[index]
    "pf-enable/1": lambda h, i, op: _TOKEN(i),
    "apparmor/1": lambda h, i, op: _PROFILE(i),
    "bwrap-copy/1": lambda h, i, op: _pair(i, 2) and _COPY(i[0]) and _DIGEST(i[1]),                  # type: ignore[index]
    "netns/1": lambda h, i, op: _NETNS(i),
    "entry/1": lambda h, i, op: _UNIT(i),
}


def check_managed(host: HostOS, records, *, operator: Optional[str], kinds: Optional[Mapping] = None) -> None:
    """§1.1 rules c and e: every record's kind is one this code holds (else ``downgrade-unsupported:<kind>``) and its
    identity passes the kind's bound (else ``bad-record``). Run before any host act."""
    kinds = KINDS if kinds is None else kinds
    for kind, ident in records:
        if kind not in kinds:
            raise Refused(f"downgrade-unsupported:{kind}", "this code holds no remove step for that kind")
        if isinstance(ident, Opaque) or not kinds[kind](host, ident, operator):
            raise Refused("bad-record", f"{kind} {canon.canonical(_thaw(ident))}")


# --- L/policy/install-shape -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallShape:
    generation: str
    levain: str
    h11: str
    completed_txn: str
    started_after: Mapping[str, int] = field(default_factory=dict)
    managed: Tuple[Record, ...] = ()


def shape_text(s: InstallShape) -> str:
    return canon.canonical({"format": SHAPE_FORMAT, "generation": s.generation, "levain": s.levain, "h11": s.h11,
                            "completed_txn": s.completed_txn, "started_after": dict(s.started_after),
                            "managed": managed_json(s.managed)})


_VERSION = re.compile(r"[0-9A-Za-z.+!-]{1,40}")
_SERVICE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def parse_shape(data: bytes) -> InstallShape:
    """Strict: exactly the record's keys, a generation id, a txn, versions, per-service thresholds, a managed list."""
    try:
        obj = canon.parse(data)
    except canon.ContractError as e:
        raise Refused("install-shape-malformed", str(e)) from None
    keys = {"format", "generation", "levain", "h11", "completed_txn", "started_after", "managed"}
    if (not isinstance(obj, dict) or set(obj) != keys or obj["format"] != SHAPE_FORMAT
            or not isinstance(obj["generation"], str) or not GEN_RE.fullmatch(obj["generation"])
            or not isinstance(obj["completed_txn"], str) or not TXN_RE.fullmatch(obj["completed_txn"])
            or not all(isinstance(obj[k], str) and _VERSION.fullmatch(obj[k]) for k in ("levain", "h11"))
            or not isinstance(obj["started_after"], dict)
            or not all(_SERVICE.fullmatch(k) and _id(v) and v >= 0 for k, v in obj["started_after"].items())):
        raise Refused("install-shape-malformed", f"not a {SHAPE_FORMAT} record")
    return InstallShape(obj["generation"], obj["levain"], obj["h11"], obj["completed_txn"],
                        dict(obj["started_after"]), parse_managed(obj["managed"], what="install-shape"))


def _full_fsync(fd: int) -> None:
    """macOS: ``fsync`` does not force the drive's cache; ``F_FULLFSYNC`` does (§2.3 notes)."""
    if sys.platform == "darwin":
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def write_record_durably(path: Path, text: str, *, mode: int = 0o644) -> Tuple[bool, str]:
    """A step body: ``text`` at ``path`` (tmp, fsync (macOS ``F_FULLFSYNC``), rename, fsync of the dir)."""
    from levain.slot.records import write_durably

    try:
        write_durably(Path(path), text.encode("utf-8"), mode=mode, fsync=_full_fsync)
    except OSError as e:
        return False, f"could not write {path} durably: {e.strerror or e}"
    return True, ""


__all__ = ["GEN_RE", "InstallShape", "KINDS", "KIND_RE", "MAX_IDENTITY", "Opaque", "SHAPE_FORMAT", "TXN_RE",
           "check_managed", "generation_id", "managed_json", "new_txn", "parse_managed", "parse_shape",
           "read_version_text", "shape_text", "write_record_durably"]
