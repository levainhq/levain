"""``levain.pathid``: containment by identity, not by spelling (key 1106-1; the class of 1105-49).

Three kinds of test, so CI exercises the logic where no alias exists and macOS exercises the real thing:

* plain units that hold on every filesystem (a sibling whose name starts with the root's name, a symlink, a stat
  failure, ``same``);
* tests that build their own case alias of a directory at run time (``_case_alias``, the helper of
  ``test_room_dir_case_alias.py``) and skip with the reason when this filesystem is case-sensitive: a Linux CI job can
  never red-test them, only a macOS run can;
* a Linux-runnable test that replaces the module's spelling lookup (``pathid._fs_spelling``, the seam) with a
  case-insensitive walk over the real tree, so the canonical-then-contain logic runs where no alias exists.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain import pathid


def _case_alias(p):
    """``p`` spelled with its letters' case swapped, when this filesystem takes that spelling as the same directory (a
    case-insensitive one: macOS APFS by default); else the test is skipped."""
    alias = Path(str(p.resolve()).swapcase())
    if str(alias) == str(p.resolve()) or not alias.exists() or not os.path.samefile(alias, p):
        pytest.skip("this filesystem is case-sensitive: no case alias of a directory exists")
    return alias


@pytest.fixture
def tree(tmp_path):
    """``root/{sub/deep,}`` plus ``other`` and ``rootcamp`` (a sibling whose name starts with ``root``'s)."""
    base = tmp_path.resolve()
    for d in ("root/sub/deep", "other", "rootcamp"):
        (base / d).mkdir(parents=True)
    (base / "root/sub/f.txt").write_text("x")
    return base


# ---------------------------------------------------------------- plain units, any filesystem

def test_inside_is_root_or_below_and_nonexistent_tails_follow_their_parent(tree):
    root = tree / "root"
    assert pathid.inside(root, root)
    assert pathid.inside(root, root / "sub")
    assert pathid.inside(root, root / "sub/deep/not/yet/made")
    assert not pathid.inside(root, tree / "other")
    assert not pathid.inside(root, tree)                      # the parent of the root is not inside it
    assert not pathid.inside(root, tree / "other/not/yet/made")


def test_a_sibling_whose_name_starts_with_the_roots_is_not_inside(tree):
    """`/a/b` vs `/a/bc`: a string prefix says inside, a path-component test does not."""
    assert not pathid.inside(tree / "root", tree / "rootcamp")
    assert not pathid.inside(tree / "root", tree / "rootcamp/x")
    assert not pathid.maybe_inside(tree / "root", tree / "rootcamp")
    assert not pathid.inside(tree / "root", tree / "root2/never/made")


def test_a_symlink_is_followed_to_where_it_points(tree):
    (tree / "root/to_other").symlink_to(tree / "other")
    (tree / "other/to_root").symlink_to(tree / "root/sub")
    assert not pathid.inside(tree / "root", tree / "root/to_other")       # inside by name, outside by where it leads
    assert not pathid.inside(tree / "root", tree / "root/to_other/x")
    assert pathid.inside(tree / "root", tree / "other/to_root")            # outside by name, inside by where it leads
    assert pathid.inside(tree / "root", tree / "other/to_root/deep/y")
    assert pathid.canonical(tree / "other/to_root") == tree / "root/sub"


def test_canonical_never_raises_and_returns_an_absolute_path(tree, monkeypatch):
    monkeypatch.chdir(tree)
    got = pathid.canonical("root/sub/never/made")
    assert got.is_absolute() and got == tree / "root/sub/never/made"
    assert pathid.canonical(tree / "nope/at/all") == tree / "nope/at/all"
    assert pathid.canonical(str(tree / "root") + "\0bad").is_absolute()    # an embedded NUL: still an answer
    loop = tree / "loop"
    loop.symlink_to(loop)
    assert pathid.canonical(loop / "x").is_absolute()                       # a symlink loop: still an answer


def test_inside_takes_str_and_relative_paths(tree, monkeypatch):
    monkeypatch.chdir(tree / "root")
    assert pathid.inside(".", "sub/deep")
    assert not pathid.inside(".", "../other")
    assert pathid.inside(str(tree / "root"), str(tree / "root/sub"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory: no permission failure to provoke")
def test_a_stat_failure_on_an_existing_path_is_not_inside_but_may_be(tree):
    """GRANTS direction: True only when sure. DENY direction: surely or possibly. A directory with no search
    permission makes every stat below it fail with EACCES, which is a path that exists and cannot be judged."""
    locked = tree / "other/locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert not pathid.inside(tree / "root", locked / "inner")
        assert pathid.maybe_inside(tree / "root", locked / "inner")
        # the root itself unjudgeable: nothing can be said to be under it
        assert not pathid.inside(locked / "inner", tree / "root/sub")
        assert pathid.maybe_inside(locked / "inner", tree / "root/sub")
        # a stat failure never turns a SURE answer around
        assert pathid.inside(tree / "other", locked)
    finally:
        locked.chmod(0o700)


def test_same_is_samefile_with_errors_read_as_false(tree):
    (tree / "root/hard").write_text("x")
    os.link(tree / "root/hard", tree / "other/hard2")
    assert pathid.same(tree / "root/hard", tree / "other/hard2")
    assert pathid.same(tree / "root", tree / "root/sub/..")
    assert not pathid.same(tree / "root", tree / "other")
    assert not pathid.same(tree / "root", tree / "missing")
    assert not pathid.same(tree / "missing", tree / "missing")
    assert not pathid.same("a\0b", tree / "root")


# ---------------------------------------------------------------- real case aliases (macOS APFS; skipped elsewhere)

def test_canonical_spells_an_alias_as_the_filesystem_does(tree):
    alias = _case_alias(tree / "root/sub")
    assert str(alias) != str(tree / "root/sub")
    assert pathid.canonical(alias) == tree / "root/sub"
    assert pathid.canonical(alias / "f.txt") == tree / "root/sub/f.txt"


def test_canonical_of_an_alias_with_a_nonexistent_tail_keeps_the_tail_as_given(tree):
    alias = _case_alias(tree / "root")
    assert pathid.canonical(alias / "NeW" / "Deeper") == tree / "root" / "NeW" / "Deeper"


def test_an_alias_of_a_path_inside_is_inside_and_an_alias_of_a_path_outside_is_not(tree):
    root = tree / "root"
    assert pathid.inside(root, _case_alias(root / "sub"))
    assert pathid.inside(root, _case_alias(root / "sub") / "NeW")           # alias plus a tail that does not exist
    assert pathid.inside(_case_alias(root), root / "sub")                   # the root spelled as an alias
    assert pathid.inside(_case_alias(root), _case_alias(root / "sub" / "deep"))
    assert not pathid.inside(root, _case_alias(tree / "other"))
    assert not pathid.inside(_case_alias(root), tree / "other")
    assert not pathid.inside(root, _case_alias(tree / "rootcamp"))          # the prefix sibling, spelled as an alias
    assert pathid.maybe_inside(root, _case_alias(root / "sub"))
    assert not pathid.maybe_inside(root, _case_alias(tree / "other"))


def test_identity_alone_catches_an_alias_when_the_spelling_lookup_cannot(tree, monkeypatch):
    """The second step: with the filesystem's spelling unavailable (the Linux answer, identity), a path whose existing
    ancestor is the root by `(st_dev, st_ino)` is still inside."""
    monkeypatch.setattr(pathid, "_fs_spelling", lambda p: p if os.path.exists(p) else None)
    root = tree / "root"
    alias = _case_alias(root)
    assert pathid.canonical(alias) == alias                                   # the lookup is off: the alias stays
    assert pathid.inside(root, alias / "sub")
    assert pathid.inside(alias, root / "sub/deep")
    assert not pathid.inside(root, _case_alias(tree / "other"))


# ---------------------------------------------------------------- the same logic where no alias exists (CI on Linux)

def _case_insensitive_spelling(path: str):
    """Stand-in for the filesystem's own spelling: walk the real tree and match each component without case, the way
    a case-insensitive volume answers; None for a path with no match."""
    cur = Path(path).anchor or "/"
    for part in Path(path).parts[1:]:
        names = os.listdir(cur)
        hit = part if part in names else next((n for n in names if n.lower() == part.lower()), None)
        if hit is None:
            return None
        cur = os.path.join(cur, hit)
    return cur


def test_the_canonical_then_contain_logic_with_an_injected_case_fold(tree, monkeypatch):
    monkeypatch.setattr(pathid, "_fs_spelling", _case_insensitive_spelling)
    root = tree / "root"
    alias = root.with_name("ROOT")                                             # `.../ROOT`, exists only through the fold
    assert pathid.canonical(alias / "SUB" / "Never" / "Made") == root / "sub" / "Never" / "Made"
    assert pathid.canonical(alias) == root
    assert pathid.inside(root, alias / "SUB")
    assert pathid.inside(alias, root / "sub/deep")
    assert pathid.inside(root, alias / "SUB" / "Never" / "Made")
    assert pathid.maybe_inside(root, alias / "sub")
    assert not pathid.inside(root, Path(str(tree / "other")).with_name("OTHER"))
    assert not pathid.inside(root, Path(str(tree / "rootcamp")).with_name("ROOTCAMP") / "x")
    assert not pathid.inside(root, Path(str(tree / "rootcamp")).with_name("ROOTCAMP"))


# ---------------------------------------------------------------- L3 casefix round 1 (key 1106-1): F1, F3, F4

def _unknowable(monkeypatch, *paths, spelling=None):
    """Stand in for "each of ``paths`` exists and cannot be read" (EACCES) at both of pathid's doors: the spelling seam
    raises ``_Unsure`` and ``os.stat`` raises ``PermissionError``, as a real permission failure does to both. Every
    other path is answered by ``spelling`` (default: the module's own lookup) and the real ``os.stat``."""
    import errno
    bad = {os.fspath(p) for p in paths}
    inner, real_stat = spelling or pathid._fs_spelling, os.stat

    def spelled(p):
        if p in bad:
            raise pathid._Unsure("injected: exists, unreadable")
        return inner(p)

    def stat(p, *a, **k):
        if isinstance(p, (str, os.PathLike)) and os.fspath(p) in bad:
            raise PermissionError(errno.EACCES, "injected", os.fspath(p))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(pathid, "_fs_spelling", spelled)
    monkeypatch.setattr(pathid.os, "stat", stat)


def test_an_unknowable_path_lexically_under_the_root_is_not_surely_inside(tree, monkeypatch):
    """F3: `_canon` keeps the given spelling when it cannot place a path, and that spelling sat under the root, so
    `inside` (GRANTS: True only when sure) answered True for a path it could not place. Now an unsure side never
    takes the lexical True; the identity walk decides, and a match ABOVE a component it could not stat is unsure."""
    root = tree / "root"
    _unknowable(monkeypatch, root / "sub")
    for p in (root / "sub", root / "sub" / "new"):
        assert not pathid.inside(root, p)
        assert pathid.maybe_inside(root, p)                 # DENY direction unchanged: unsure is still True


def test_a_symlink_loop_under_the_root_is_not_surely_inside(tree):
    loop = tree / "root" / "loop"
    loop.symlink_to(loop)
    for p in (loop, loop / "x"):
        assert not pathid.inside(tree / "root", p)
        assert pathid.maybe_inside(tree / "root", p)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory: no permission failure to provoke")
def test_a_path_below_an_unsearchable_directory_under_the_root_is_not_surely_inside(tree):
    locked = tree / "root" / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert not pathid.inside(tree / "root", locked / "inner")
        assert pathid.maybe_inside(tree / "root", locked / "inner")
        assert pathid.inside(tree / "root", locked)          # the unsearchable directory itself is placed: sure
    finally:
        locked.chmod(0o700)


def test_canonical_sure_input_answers_as_before_the_unsure_fix(tree, monkeypatch):
    """The control the F3 fix must not move: canonical, sure input inside is still True from `inside`, outside still
    False, and `maybe_inside` agrees; with an unknowable path elsewhere in the tree."""
    root = tree / "root"
    _unknowable(monkeypatch, tree / "other")
    for p in (root, root / "sub", root / "sub" / "f.txt", root / "sub" / "deep" / "not" / "made"):
        assert pathid.inside(root, p) and pathid.maybe_inside(root, p)
    for p in (tree, tree / "rootcamp", tree / "rootcamp" / "x"):
        assert not pathid.inside(root, p) and not pathid.maybe_inside(root, p)


def test_bad_arguments_never_raise_and_are_unsure(tree, monkeypatch):
    """F4: `Path(None)` raised TypeError and a NUL in a root raised ValueError at the root's stat, despite "never
    raises". A bad argument is never surely inside and is possibly inside (a DENY caller refuses). The cwd is the
    root on purpose: a bad argument must not fall back to "." and so be the cwd, which would put the cwd's tree in it."""
    root = tree / "root"
    monkeypatch.chdir(root)
    for bad in (None, 5, "/\0bad", str(root) + "\0x"):
        assert not pathid.inside(bad, root / "sub") and pathid.maybe_inside(bad, root / "sub")
        assert not pathid.inside(root, bad) and pathid.maybe_inside(root, bad)
        assert pathid.maybe_same(bad, root) and pathid.maybe_same(root, bad)
        pathid.canonical(bad)


def test_the_consent_home_refusal_refuses_a_nul_workspace_instead_of_raising(tree):
    """F4, the codex scenario: `consent_root_problem("/\\0bad", home=...)` aborted on the root stat."""
    from levain.authority import consent_root_problem
    assert consent_root_problem("/\0bad", home=str(tree / "root")) is not None


def test_maybe_same_is_equal_spelling_same_file_or_unsure(tree, monkeypatch):
    """F1's DENY answer: one path surely or possibly. Sure input answers as `canonical` equality or `same` does."""
    root = tree / "root"
    (tree / "other" / "to_sub").symlink_to(root / "sub")
    assert pathid.maybe_same(root / "sub", tree / "other" / "to_sub")
    assert pathid.maybe_same(root / "never" / "made", root / "sub" / ".." / "never" / "made")
    assert not pathid.maybe_same(root / "sub", tree / "other")
    assert not pathid.maybe_same(root / "never", root / "made")
    _unknowable(monkeypatch, tree / "other" / "x")
    assert pathid.maybe_same(tree / "other" / "x", root / "sub")        # could not place it: possibly the same
