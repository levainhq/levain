"""S1 session 5 unit 5b2: git's ``.gitmodules`` / ``.gitattributes`` predicates and the structural finder.

``test_git_t0060_vectors``: the 5b2 DONE row V (session 1, ``DONE_s1_run*.txt``): git's own vectors, the
'match .gitmodules' and 'match .gitattributes' tests of ``t/t0060-path-utils.sh`` at v2.39.4, with ``u200c`` the
U+200C test-lib.sh defines and each word as the shell's eval leaves it.
``test_finder_follows_fsck_tree``: the 5b2 DONE row F1: the finder collects what git's ``fsck_tree`` collects (any
non-symlink entry at a special name; after a backslash only the NTFS ``.gitmodules`` spelling), nothing else.
"""
from __future__ import annotations

from levain.slot import special_paths as sp

GITMODULES_POS = [
    b'.gitmodules', b'.git\xe2\x80\x8cmodules', b'.Gitmodules', b'.gitmoduleS', b'.gitmodules ', b'.gitmodules.',
    b'.gitmodules  ', b'.gitmodules. ', b'.gitmodules .', b'.gitmodules..', b'.gitmodules   ', b'.gitmodules.  ',
    b'.gitmodules . ', b'.gitmodules  .', b'.Gitmodules ', b'.Gitmodules.', b'.Gitmodules  ', b'.Gitmodules. ',
    b'.Gitmodules .', b'.Gitmodules..', b'.Gitmodules   ', b'.Gitmodules.  ', b'.Gitmodules . ', b'.Gitmodules  .',
    b'GITMOD~1', b'gitmod~1', b'GITMOD~2', b'gitmod~3', b'GITMOD~4', b'GITMOD~1 ', b'gitmod~2.', b'GITMOD~3  ',
    b'gitmod~4. ', b'GITMOD~1 .', b'gitmod~2   ', b'GITMOD~3.  ', b'gitmod~4 . ', b'GI7EBA~1', b'gi7eba~9',
    b'GI7EB~10', b'GI7EB~11', b'GI7EB~99', b'GI7EB~10', b'GI7E~100', b'GI7E~101', b'GI7E~999', b'~1000000',
    b'~9999999', b'.gitmodules:$DATA', b'gitmod~4 . :$DATA',
]
GITMODULES_NEG = [
    b'.gitmodules x', b'.gitmodules .x', b' .gitmodules', b'..gitmodules', b'gitmodules', b'.gitmodule',
    b'.gitmodules x ', b'.gitmodules .x', b'GI7EBA~', b'GI7EBA~0', b'GI7EBA~~1', b'GI7EBA~X', b'Gx7EBA~1',
    b'GI7EBX~1', b'GI7EB~1', b'GI7EB~01', b'GI7EB~1X', b'.gitmodules,:$DATA',
]
GITATTRIBUTES_POS = [b'.gitattributes', b'.git\xe2\x80\x8cattributes', b'.Gitattributes', b'.gitattributeS',
                     b'GITATT~1', b'GI7D29~1']


def test_git_t0060_vectors() -> None:
    assert [w for w in GITMODULES_POS if not sp.is_dotgitmodules(w)] == []
    assert [w for w in GITMODULES_NEG if sp.is_dotgitmodules(w)] == []
    assert [w for w in GITATTRIBUTES_POS if not sp.is_dotgitattributes(w)] == []


def _entry(mode: str, name: bytes, n: int) -> bytes:
    return mode.encode() + b" " + name + b"\0" + bytes([n]) * 20


def test_finder_follows_fsck_tree() -> None:
    payload = b"".join([
        _entry("100644", b".gitmodules", 1),
        _entry("100644", b"GITMOD~1", 2),
        _entry("100644", b"sub\\.gitmodules", 3),
        _entry("100644", b".gitattributes", 4),
        _entry("100644", b"sub\\.gitattributes", 5),
        _entry("120000", b".gitmodules ", 6),
        _entry("120000", b".gitattributes", 7),
        _entry("160000", b"GI7D29~1", 8),
        _entry("100644", b"README", 9),
    ])
    found = sp.special_blobs([payload], 40)
    assert found == {(bytes([n]) * 20).hex() for n in (1, 2, 3, 4, 8)}
