"""One bounded grammar for a git bundle HEADER, and the writer of the importer's own (S1 session 5 DESIGN §4.12).

A git bundle starts with a signature line (``# v2 git bundle`` or ``# v3 git bundle``), v3 capability lines
(``@<key>[=<value>]``), prerequisite lines (``-<oid>[ <comment>]``), ref lines (``<oid> <refname>``) and one
blank line; the pack follows. Every reader of a header Levain trusts with a decision reads it HERE: render's
staging-bundle parse, root's check of the importer's bundle, the CLI's and the dispatch consumer's verify, so the
four cannot disagree on a byte.

Bounds: the header is at most :data:`HEADER_MAX` bytes and :data:`LINES_MAX` lines including the signature and the
blank line; every oid is exactly the object format's hex length, lowercase. A header past a bound, with no blank
line, or with any line off the grammar is ``bundle-header-mismatch``. The only capability is ``@object-format``;
any other (``@filter``, an unknown key) is ``bundle-capability:<key>``.

:class:`HeaderObserver` is fed the bundle's bytes in the order they are copied and keeps only the header's, so a
caller copying a large bundle never buffers its body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from levain.slot import Refused

SIG_V2 = b"# v2 git bundle\n"
SIG_V3 = b"# v3 git bundle\n"
#: The most bytes a header may take, signature and blank line included.
HEADER_MAX = 64 << 10
#: The most lines a header may take: refs and prerequisites, the signature and the blank line. Every such line is
#: at least an oid long, so with sha1's 40-hex oids the byte bound is reached first; the line bound is kept as the
#: design's second fence.
LINES_MAX = 4096 + 2
OID_HEX = {"sha1": 40, "sha256": 64}
MISMATCH = "bundle-header-mismatch"

_HEX = re.compile(rb"[0-9a-f]+")
_CAP = re.compile(rb"@([a-z][a-z0-9-]*)(?:=(.*))?")
#: A ref name as a header carries it: no control bytes, no space (git's ``check-ref-format`` is the importer's).
_REF = re.compile(rb"[\x21-\x7e\x80-\xff]+")


@dataclass(frozen=True)
class BundleHeader:
    version: int
    object_format: str
    prerequisites: tuple[str, ...]
    refs: tuple[tuple[str, bytes], ...]     # (oid, refname) in header order
    size: int                               # bytes, signature through the blank line


def _bad(detail: str) -> Refused:
    return Refused(MISMATCH, detail)


def _oid(raw: bytes, fmt: str) -> str:
    if len(raw) != OID_HEX[fmt] or not _HEX.fullmatch(raw):
        raise _bad(f"a malformed {fmt} oid {raw[:80]!r}")
    return raw.decode("ascii")


def parse(header: bytes) -> BundleHeader:
    """The header (signature through the blank line, nothing after it) by the grammar, or a refusal by name."""
    if len(header) > HEADER_MAX:
        raise _bad(f"header past {HEADER_MAX} bytes")
    if not header.endswith(b"\n\n"):
        raise _bad("no blank line")
    lines = header[:-1].split(b"\n")[:-1]    # each line without its newline; the blank line dropped
    if len(lines) + 1 > LINES_MAX:
        raise _bad(f"more than {LINES_MAX} lines")
    sig = lines[0] + b"\n"
    if sig == SIG_V2:
        version, fmt = 2, "sha1"
    elif sig == SIG_V3:
        version, fmt = 3, "sha1"
    else:
        raise _bad("not a v2 or v3 git bundle signature")
    i = 1
    if version == 3:
        seen_format = False
        while i < len(lines) and lines[i].startswith(b"@"):
            m = _CAP.fullmatch(lines[i])
            if m is None:
                raise _bad(f"a malformed capability line {lines[i][:80]!r}")
            key, value = m.group(1).decode("ascii"), m.group(2)
            if key != "object-format":
                raise Refused(f"bundle-capability:{key}", "Levain reads object-format only")
            if seen_format or value is None or value.decode("ascii", "replace") not in OID_HEX:
                raise _bad(f"an object-format capability {lines[i][:80]!r}")
            fmt, seen_format = value.decode("ascii"), True
            i += 1
    prereqs: list[str] = []
    refs: list[tuple[str, bytes]] = []
    for line in lines[i:]:
        if not line:
            raise _bad("an empty line inside the header")
        if line.startswith(b"-"):
            if refs:
                raise _bad("a prerequisite after a ref")
            oid, _, _comment = line[1:].partition(b" ")
            prereqs.append(_oid(oid, fmt))
            continue
        oid, sp, name = line.partition(b" ")
        if not sp or not _REF.fullmatch(name):
            raise _bad(f"a malformed ref line {line[:80]!r}")
        refs.append((_oid(oid, fmt), name))
    return BundleHeader(version, fmt, tuple(prereqs), tuple(refs), len(header))


class HeaderObserver:
    """Fed every byte of a bundle in order (``feed``), then ``feed(b"")`` once at its end. It keeps the header's
    bytes only and refuses INSIDE ``feed`` (so a copy loop that calls it removes what it wrote): past
    :data:`HEADER_MAX` with no blank line, a header off the grammar the moment its blank line arrives, ``check``'s
    refusal of the parsed header, or an end of stream before the blank line. :meth:`finish` returns the header."""

    def __init__(self, check: Callable[[BundleHeader], object] | None = None) -> None:
        self._buf = bytearray()
        self._header: BundleHeader | None = None
        self._check = check
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            if self._header is None:
                raise _bad("the bundle ended before its blank line")
            return
        self.total += len(chunk)
        if self._header is not None:
            return
        start = max(0, len(self._buf) - 1)
        self._buf += chunk
        end = self._buf.find(b"\n\n", start)
        if end >= 0:
            del self._buf[end + 2:]
            header = parse(bytes(self._buf))
            if self._check is not None:
                self._check(header)
            self._header = header
        elif len(self._buf) > HEADER_MAX:
            raise _bad(f"no blank line in the first {HEADER_MAX} bytes")

    def finish(self) -> BundleHeader:
        if self._header is None:
            raise _bad("the bundle ended before its blank line")
        return self._header


def base_of(h: BundleHeader) -> tuple[str, bytes]:
    """Render's base rule (§5a) over the staging bundle's header: no prerequisite (``bundle-prerequisite``), a
    ``HEAD`` line (``bundle-no-head``) whose oid equals the bundle's single branch ref's (``bundle-head-mismatch``).
    Returns ``(oid, refname)``: that oid is the base."""
    if h.prerequisites:
        raise Refused("bundle-prerequisite", f"{len(h.prerequisites)} prerequisite line(s)")
    heads = [oid for oid, name in h.refs if name == b"HEAD"]
    if not heads:
        raise Refused("bundle-no-head", "the staging bundle carries no HEAD line")
    branches = [(oid, name) for oid, name in h.refs if name != b"HEAD"]
    if len(heads) != 1 or len(branches) != 1 or branches[0][0] != heads[0]:
        raise Refused("bundle-head-mismatch", "HEAD does not name the bundle's single branch ref")
    if not branch_ref_ok(branches[0][1]):
        raise Refused("bundle-head-mismatch", "the ref beside HEAD is not refs/heads/<a valid branch name>")
    return branches[0]


_REF_BAD = re.compile(rb"[\x00-\x20\x7f~^:?*\[\\]")


def branch_ref_ok(name: bytes) -> bool:
    """``refs/heads/<name>`` with ``<name>`` a valid ref name by git's ``check-ref-format`` rules: no control
    byte, space or ``~^:?*[\\``, no ``..`` or ``@{``, no empty component, none starting with ``.`` or
    ending ``.lock``, and not ending ``/`` or ``.``."""
    prefix = b"refs/heads/"
    if not name.startswith(prefix) or len(name) == len(prefix):
        return False
    rest = name[len(prefix):]
    if _REF_BAD.search(rest) or b".." in rest or b"@{" in rest or rest.endswith((b"/", b".")):
        return False
    return all(c and not c.startswith(b".") and not c.endswith(b".lock") for c in rest.split(b"/"))


def write_header(object_format: str, refs: list[tuple[str, bytes]] | tuple[tuple[str, bytes], ...],
                 prerequisites: tuple[str, ...] | list[str] = ()) -> bytes:
    """The header the importer writes: v2 for sha1, v3 with ``@object-format=sha256`` for sha256; the result parses
    back to the same refs and prerequisites by :func:`parse`, or this refuses."""
    if object_format not in OID_HEX:
        raise _bad(f"object format {object_format!r}")
    out = bytearray(SIG_V2 if object_format == "sha1" else SIG_V3 + b"@object-format=sha256\n")
    for oid in prerequisites:
        out += b"-" + oid.encode("ascii") + b"\n"
    for oid, name in refs:
        out += oid.encode("ascii") + b" " + name + b"\n"
    out += b"\n"
    data = bytes(out)
    h = parse(data)
    if h.refs != tuple((o, n) for o, n in refs) or h.prerequisites != tuple(prerequisites):
        raise _bad("the written header does not parse back to its refs")
    return data
