"""git's ``.gitmodules`` / ``.gitattributes`` name predicates, ported, and the structural special-blob finder
(S1 session 5 DESIGN §5 step 6, §6.1 step 4, T34, A10; symlink targets as the third class, A4).

A tree in an adopt bundle may name, at a ``.gitmodules`` or ``.gitattributes`` entry, a blob the bundle does not carry
(a blob of the base). ``index-pack --fsck-objects`` in an empty store then looks that blob up after the pack and either
prints its oid (``.gitmodules``) or dies ``gitattributesMissing``. So the importer SENDS every such blob, and the
verifier computes the same list to hold it to: :func:`special_blobs`, one function, both callers. It finds them the way
git's ``fsck_tree`` does, from the trees' own entries, never from a message git printed.

Ported from git at :data:`PORT_TAG` (``utf8.c`` ``next_hfs_char`` / ``is_hfs_dot_generic``; ``path.c``
``is_ntfs_dot_generic``; ``fsck.c`` ``fsck_tree``'s collection of ``gitmodules_found`` and ``gitattributes_found``);
the same functions are byte-identical at v2.50.1. Names are bytes, as git sees them: an entry name ends where git's
NUL-terminated string would, so any index past the end reads as NUL. ``is_dir_sep`` is the POSIX one (``/`` only):
the gits adopt runs are macOS and Linux builds. A spelling a later git adds is not matched here until the port is
refreshed (the DESIGN's known edge).
"""
from __future__ import annotations

from typing import Iterable, Iterator

#: The git tag the predicates and the collection rule were ported from.
PORT_TAG = "v2.39.4"

#: Code points HFS+ ignores entirely (git's ``next_hfs_char``).
_HFS_IGNORED = frozenset({0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                          0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F, 0xFEFF})
#: The most octal digits a tree entry's mode may take here (git's own modes take 5 or 6).
MODE_DIGITS_MAX = 7
#: The hex lengths of the two object formats (sha1, sha256).
_HEXLENS = (40, 64)
_S_IFMT = 0o170000
_S_IFLNK = 0o120000


def _at(name: bytes, i: int) -> int:
    return name[i] if i < len(name) else 0


def _lower(c: int) -> int:
    """C-locale ``tolower``: ASCII only."""
    return c + 32 if 0x41 <= c <= 0x5A else c


def _utf8_char(name: bytes, i: int) -> tuple[int, int | None]:
    """git's ``pick_one_utf8_char`` over a NUL-terminated string: ``(code point, next index)``, or ``(0, None)`` for
    invalid UTF-8 (git sets the cursor to NULL). Past the end is the NUL terminator (code point 0, one byte)."""
    s0 = _at(name, i)
    s1, s2, s3 = _at(name, i + 1), _at(name, i + 2), _at(name, i + 3)
    if s0 < 0x80:
        return s0, i + 1
    if s0 & 0xE0 == 0xC0:
        if s1 & 0xC0 != 0x80 or s0 & 0xFE == 0xC0:
            return 0, None
        return ((s0 & 0x1F) << 6) | (s1 & 0x3F), i + 2
    if s0 & 0xF0 == 0xE0:
        if (s1 & 0xC0 != 0x80 or s2 & 0xC0 != 0x80
                or (s0 == 0xE0 and s1 & 0xE0 == 0x80)            # overlong
                or (s0 == 0xED and s1 & 0xE0 == 0xA0)            # surrogate
                or (s0 == 0xEF and s1 == 0xBF and s2 & 0xFE == 0xBE)):  # U+FFFE, U+FFFF
            return 0, None
        return ((s0 & 0x0F) << 12) | ((s1 & 0x3F) << 6) | (s2 & 0x3F), i + 3
    if s0 & 0xF8 == 0xF0:
        if (s1 & 0xC0 != 0x80 or s2 & 0xC0 != 0x80 or s3 & 0xC0 != 0x80
                or (s0 == 0xF0 and s1 & 0xF0 == 0x80)            # overlong
                or (s0 == 0xF4 and s1 > 0x8F) or s0 > 0xF4):     # past U+10FFFF
            return 0, None
        return ((s0 & 0x07) << 18) | ((s1 & 0x3F) << 12) | ((s2 & 0x3F) << 6) | (s3 & 0x3F), i + 4
    return 0, None


def _next_hfs_char(name: bytes, i: int | None) -> tuple[int, int | None]:
    """git's ``next_hfs_char``: the next code point HFS+ does not ignore; invalid UTF-8 reads as 0 (end)."""
    while i is not None:
        c, i = _utf8_char(name, i)
        if i is None:
            return 0, None
        if c in _HFS_IGNORED:
            continue
        return c, i
    return 0, None


def _is_hfs_dot(name: bytes, needle: bytes) -> bool:
    c, i = _next_hfs_char(name, 0)
    if c != 0x2E:
        return False
    for want in needle:
        c, i = _next_hfs_char(name, i)
        if c > 127 or _lower(c) != want:
            return False
    c, i = _next_hfs_char(name, i)
    return not (c and c != 0x2F)


def _is_ntfs_dot(name: bytes, needle: bytes, shortname_prefix: bytes, start: int = 0) -> bool:
    """git's ``is_ntfs_dot_generic`` on the name that begins at ``start`` of ``name``: every read indexes the one
    buffer, as git's pointer does, so checking each backslash segment never copies a suffix."""
    def at(i: int) -> int:
        return _at(name, start + i)

    def only_spaces_and_periods(i: int) -> bool:
        while True:
            c = at(i)
            i += 1
            if c == 0 or c == 0x3A:
                return True
            if c not in (0x20, 0x2E):
                return False

    n = len(needle)
    if at(0) == 0x2E and len(name) - start >= 1 + n and name[start + 1:start + 1 + n].lower() == needle:
        return only_spaces_and_periods(n + 1)
    # A regular NTFS short name: the first 6 characters, then ~1 .. ~4.
    if (len(name) - start >= 6 and name[start:start + 6].lower() == needle[:6] and at(6) == 0x7E
            and 0x31 <= at(7) <= 0x34):
        return only_spaces_and_periods(8)
    # A fall-back NTFS short name.
    i = 0
    saw_tilde = False
    while i < 8:
        c = at(i)
        if c == 0:
            return False
        if saw_tilde:
            if not 0x30 <= c <= 0x39:
                return False
        elif c == 0x7E:
            i += 1
            if not 0x31 <= at(i) <= 0x39:
                return False
            saw_tilde = True
        elif i >= 6 or c & 0x80 or _lower(c) != shortname_prefix[i]:
            return False
        i += 1
    return only_spaces_and_periods(i)


def is_hfs_dotgitmodules(name: bytes) -> bool:
    return _is_hfs_dot(name, b"gitmodules")


def is_ntfs_dotgitmodules(name: bytes) -> bool:
    return _is_ntfs_dot(name, b"gitmodules", b"gi7eba")


def is_hfs_dotgitattributes(name: bytes) -> bool:
    return _is_hfs_dot(name, b"gitattributes")


def is_ntfs_dotgitattributes(name: bytes) -> bool:
    return _is_ntfs_dot(name, b"gitattributes", b"gi7d29")


def is_dotgitmodules(name: bytes) -> bool:
    return is_hfs_dotgitmodules(name) or is_ntfs_dotgitmodules(name)


def is_dotgitattributes(name: bytes) -> bool:
    return is_hfs_dotgitattributes(name) or is_ntfs_dotgitattributes(name)


def tree_entries(payload: bytes, hexlen: int) -> Iterator[tuple[int, bytes, str]]:
    """A tree object's entries, ``(mode, name, oid hex)``, in order. Bounded: a mode of 1 to
    :data:`MODE_DIGITS_MAX` octal digits, a NUL ending the name, a whole raw oid of the format's length. Anything else
    raises ValueError (the payload is not a tree this reader accepts), as does a ``hexlen`` that is not 40 or 64."""
    if hexlen not in _HEXLENS:
        raise ValueError(f"hexlen {hexlen!r}: not an object format's (40 or 64)")
    raw = hexlen // 2
    i = 0
    end = len(payload)
    while i < end:
        sp = payload.find(b" ", i, i + MODE_DIGITS_MAX + 1)
        if sp <= i:
            raise ValueError(f"tree entry at {i}: no mode of 1-{MODE_DIGITS_MAX} digits")
        mode_s = payload[i:sp]
        if any(c < 0x30 or c > 0x37 for c in mode_s):
            raise ValueError(f"tree entry at {i}: a non-octal mode")
        nul = payload.find(b"\0", sp + 1)
        if nul < 0:
            raise ValueError(f"tree entry at {i}: no NUL after the name")
        if nul + 1 + raw > end:
            raise ValueError(f"tree entry at {i}: a truncated oid")
        yield int(mode_s, 8), payload[sp + 1:nul], payload[nul + 1:nul + 1 + raw].hex()
        i = nul + 1 + raw


def special_entry_oids(payload: bytes, hexlen: int) -> Iterator[str]:
    """The oids one tree names at a ``.gitmodules`` or ``.gitattributes`` entry, by ``fsck_tree``'s rule: any entry
    that is not a symlink (a symlink there is git's own ``gitmodulesSymlink`` / ``gitattributesSymlink`` report, not a
    lookup of this class; its oid is a symlink target, :func:`symlink_entry_oids`); and every segment after a backslash in a name is checked once more with the NTFS ``.gitmodules``
    predicate (git checks no ``.gitattributes`` spelling there). The entry's mode is not read otherwise, so a gitlink
    (``160000``) or a tree (``040000``) at a special name is collected too, exactly as ``fsck_tree`` collects it: an oid
    here is not always a blob, and a caller sending or requiring them must treat a non-blob oid explicitly."""
    for mode, name, oid in tree_entries(payload, hexlen):
        if mode & _S_IFMT == _S_IFLNK:
            continue
        if is_dotgitmodules(name) or is_dotgitattributes(name):
            yield oid
            continue
        bs = name.find(b"\\")
        while bs >= 0:
            if _is_ntfs_dot(name, b"gitmodules", b"gi7eba", bs + 1):
                yield oid
                break
            bs = name.find(b"\\", bs + 1)


def special_blobs(tree_payloads: Iterable[bytes], hexlen: int) -> set[str]:
    """Every oid the given trees name at a ``.gitmodules`` or ``.gitattributes`` entry (:func:`special_entry_oids`;
    gitlinks and trees at such a name included, as git collects them).
    Meant for the trees of ``closure(tips) - closure(base)``: of what it returns, the oids that list lacks are the
    ones to send, or to require."""
    found: set[str] = set()
    for payload in tree_payloads:
        found.update(special_entry_oids(payload, hexlen))
    return found


def symlink_entry_oids(payload: bytes, hexlen: int) -> Iterator[str]:
    """The oids one tree names at a symlink entry (mode ``120000`` by ``S_ISLNK``), whatever the name: ``fsck_tree``
    collects each as a symlink target, and a git that checks them (v2.39.4 does; some later releases do not) makes
    ``index-pack --fsck-objects`` in an empty store die ``symlinkTargetMissing`` when one is absent."""
    for mode, _name, oid in tree_entries(payload, hexlen):
        if mode & _S_IFMT == _S_IFLNK:
            yield oid


def adopt_specials(tree_payloads: Iterable[bytes], hexlen: int) -> set[str]:
    """The adopt special set, three classes: :func:`special_blobs`' ``.gitmodules`` and ``.gitattributes`` (A10) and
    every symlink target (A4). The importer sends the oids of it that its object list lacks, and the verifier requires
    exactly those: one function, both callers."""
    found: set[str] = set()
    for payload in tree_payloads:
        found.update(special_entry_oids(payload, hexlen))
        found.update(symlink_entry_oids(payload, hexlen))
    return found
