"""levain#155: the two costs of the board read that were paid again by every ``levain board state`` process.

* ``board_author``: one walk of main's first-parent history names every first adder (instead of one ``git log`` per
  ``document``/``run`` close), and must name the SAME commit the per-path ``git log`` names.
* ``board_evidence._patch_ids``: a commit's patch-id kept on disk, keyed by the commit's sha only, safe to share between processes
  and never trusted when it is damaged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.desk import board_author as ba
from levain.desk import board_evidence as be
from tests import _board_v2_seed as seed

MAIN = ba.MAIN_REF


def _commit(repo: Path, msg: str) -> str:
    seed.git(repo, "add", "-A")
    seed.git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return seed.git(repo, "rev-parse", "HEAD")


def _history(tmp_path: Path) -> Path:
    """A scratch repo whose ``origin/main`` has a merge, a cherry-pick, a rename, a deleted-then-re-added file and odd names."""
    repo = seed.mkrepo(tmp_path, "solo")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n")
    (repo / "outdir").mkdir()
    (repo / "outdir" / "one.txt").write_text("1\n")
    _commit(repo, "docs and outdir")
    (repo / "outdir" / "two.txt").write_text("2\n")
    (repo / "outdir" / "sub").mkdir()
    (repo / "outdir" / "sub" / "deep.txt").write_text("d\n")
    _commit(repo, "more outdir")
    seed.git(repo, "mv", "docs/a.md", "docs/b.md")                          # a rename: --no-renames reads it as an add of docs/b.md
    _commit(repo, "rename")
    seed.git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feat.txt").write_text("f\n")
    (repo / "weird name é.txt").write_text("w\n")
    (repo / "new\nline.txt").write_text("n\n")                               # a newline in a name
    picked = _commit(repo, "feature work")
    (repo / "feat2.txt").write_text("f2\n")
    _commit(repo, "feature work 2")
    seed.git(repo, "checkout", "-q", "main")
    (repo / "main_only.txt").write_text("m\n")
    _commit(repo, "main only")
    seed.git(repo, "cherry-pick", picked)                                  # the same patch under another sha
    seed.git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    seed.git(repo, "rm", "-q", "main_only.txt")
    _commit(repo, "delete")
    (repo / "main_only.txt").write_text("m again\n")
    _commit(repo, "re-add")
    seed.git(repo, "update-ref", MAIN, "HEAD")
    return repo


def _targeted(repo: Path, rel: str) -> str | None:
    out = be._git(repo, "log", "--first-parent", "--no-renames", "--diff-filter=A", "--format=%H", "--reverse", MAIN, "--",
                  f":(top,literal){rel}").split()
    return out[0] if out else None


def _every_path(repo: Path) -> list[str]:
    names = set()
    for line in be._git(repo, "log", "--first-parent", "--no-renames", "--diff-filter=A", "--name-only", "-z", "--format=", MAIN).split("\0"):
        names.add(line.strip("\n"))
    names.discard("")
    for n in list(names):
        while "/" in n:
            n = n.rpartition("/")[0]
            names.add(n)
    return sorted(names | {"nope.txt", "docs/a.md", "outdir/missing", "outdi", "docs/"[:-1] + "x"})


# ------------------------------------------------------------------------------------------------ first adders
def test_one_walk_names_the_same_first_adder_as_the_per_path_log(tmp_path):
    repo = _history(tmp_path)
    rd = ba._Reader(repo, ba.Resolvers() if hasattr(ba, "Resolvers") else None)
    index = rd.path_adders(MAIN)
    paths = _every_path(repo)
    assert len(paths) > 12
    for rel in paths:
        assert index.get(rel) == _targeted(repo, rel), rel                 # files, directories, a deleted file, a path never added
    assert index["docs"] and index["outdir"] and index["outdir/sub"]        # a directory: the first commit that added anything under it
    assert index["outdir"] == index["outdir/one.txt"] != index["outdir/two.txt"]
    assert index["main_only.txt"] == _targeted(repo, "main_only.txt")      # deleted and re-added: the FIRST add
    assert "new\nline.txt" in index and "weird name é.txt" in index


def test_the_walk_is_one_git_log_and_the_toplevel_is_asked_once(tmp_path, monkeypatch):
    repo = _history(tmp_path)
    rd = ba._Reader(repo, None)
    rd.walk_paths = True
    calls = []
    real = be._git
    monkeypatch.setattr(be, "_git", lambda r, *a, **k: (calls.append(a[:2]), real(r, *a, **k))[1])
    rels = ("docs/b.md", "outdir", "feat.txt", "outdir/one.txt", "nope.txt")
    got = {rel: ba._path_adder(repo, rel, MAIN, rd) for rel in rels}
    n_calls = len(calls)
    for rel in rels:
        assert (got[rel][0] or None) == _targeted(repo, rel)
    assert got["nope.txt"] == (None, ba.NOT_ON_MAIN)
    del calls[n_calls:]
    assert [c[0] for c in calls].count("log") == 1
    assert [c[:2] for c in calls].count(("rev-parse", "--show-toplevel")) == 1
    calls.clear()
    plain = ba._Reader(repo, None)                                          # below the threshold: the per-path log, as before
    for rel in ("docs/b.md", "feat.txt"):
        ba._path_adder(repo, rel, MAIN, plain)
    assert [c[0] for c in calls].count("log") == 2


def test_a_failed_walk_raises_and_is_not_kept(tmp_path):
    repo = _history(tmp_path)
    rd = ba._Reader(repo, None)
    rd.walk_paths = True
    with pytest.raises(be.GitFailed):
        rd.path_adders("refs/remotes/origin/nope")
    assert "refs/remotes/origin/nope" not in rd._adders
    assert rd.path_adders(MAIN)


# ------------------------------------------------------------------------------------------------ patch ids
@pytest.fixture
def cache(tmp_path, monkeypatch):
    be._PID_MEMO.clear()
    path = tmp_path / "cachehome" / "pids.json"
    monkeypatch.setattr(be, "_pid_cache_path", lambda key: path)
    yield path
    be._PID_MEMO.clear()


def _all_shas(repo: Path) -> list[str]:
    return seed.git(repo, "rev-list", "--all").split()


def _diff_trees(monkeypatch):
    runs = []
    real = be._git
    monkeypatch.setattr(be, "_git", lambda r, *a, **k: (runs.append(a[0]), real(r, *a, **k))[1])
    return runs


def test_the_disk_cache_returns_what_the_uncached_path_returns(tmp_path, cache, monkeypatch):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    monkeypatch.setattr(be, "_pid_cache_path", lambda key: None)            # the uncached answer
    be._PID_MEMO.clear()
    plain = be._patch_ids(repo, shas)
    monkeypatch.setattr(be, "_pid_cache_path", lambda key: cache)
    be._PID_MEMO.clear()
    first = be._patch_ids(repo, shas)                                      # cold: computes, writes
    assert first == plain and cache.is_file()
    be._PID_MEMO.clear()                                                   # a NEW process: nothing in memory
    runs = _diff_trees(monkeypatch)
    again = be._patch_ids(repo, shas)
    assert again == plain and "diff-tree" not in runs and "patch-id" not in runs      # warm: read from disk only
    doc = json.loads(cache.read_text())
    assert doc["ids"]                                                      # the merge (no patch-id) is kept as null, not recomputed
    assert any(v is None for v in doc["ids"].values())
    cherry = [s for s in set(plain.values()) if list(plain.values()).count(s) > 1]
    assert cherry, "the cherry-pick shares its patch-id with the original under another sha"


def test_a_new_commit_is_the_only_one_diffed_and_is_added_to_the_file(tmp_path, cache, monkeypatch):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    be._patch_ids(repo, shas)
    (repo / "later.txt").write_text("l\n")
    new = _commit(repo, "later")
    be._PID_MEMO.clear()
    stdins = []
    real = be._git
    monkeypatch.setattr(be, "_git", lambda r, *a, **k: (stdins.append(k.get("stdin")) if a[0] == "diff-tree" else None, real(r, *a, **k))[1])
    got = be._patch_ids(repo, [new, *shas])
    assert [s.decode().split() for s in stdins] == [[new]] and new in got
    assert new in json.loads(cache.read_text())["ids"]


@pytest.mark.parametrize("damage", ["not json", "[]", '{"v": 2, "stamps": "x", "ids": {}}', '{"v": 1}', "", "\x00\x01\x02",
                                    '{"v": 1, "stamps": "STAMP", "ids": {"zz": "aa"}}',            # one malformed key: the whole file goes
                                    '{"v": 1, "stamps": "STAMP", "ids": {"%s": "not-an-id"}}' % ("a" * 40),
                                    '{"v": 1, "stamps": "other config", "ids": {}}'])
def test_a_damaged_cache_file_is_ignored_and_rewritten(tmp_path, cache, damage):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    want = be._patch_ids(repo, shas)
    be._PID_MEMO.clear()
    digest = json.loads(cache.read_text())["stamps"]
    cache.write_text(damage.replace("STAMP", digest))
    got = be._patch_ids(repo, shas)                                        # never raises, never a wrong answer
    assert got == want
    doc = json.loads(cache.read_text())                                    # and the file is whole again
    assert doc["v"] == be.PID_CACHE_V and doc["stamps"] == digest and set(shas) <= set(doc["ids"])


def test_a_cache_that_is_a_link_a_directory_or_too_big_is_not_read(tmp_path, cache, monkeypatch):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    want = be._patch_ids(repo, shas)
    digest = json.loads(cache.read_text())["stamps"]
    liar = tmp_path / "liar.json"
    liar.write_text(json.dumps({"v": 1, "stamps": digest, "ids": {s: "f" * 40 for s in shas}}))
    cache.unlink()
    cache.symlink_to(liar)                                                 # a link to a well-formed file with false ids
    be._PID_MEMO.clear()
    assert be._patch_ids(repo, shas) == want
    cache.unlink()
    liar.replace(cache)
    monkeypatch.setattr(be, "PID_CACHE_MAX_BYTES", 10)                     # over the size cap
    be._PID_MEMO.clear()
    assert be._patch_ids(repo, shas) == want


def test_a_config_change_drops_the_file(tmp_path, cache, monkeypatch):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    be._patch_ids(repo, shas)
    be._PID_MEMO.clear()
    seed.git(repo, "config", "diff.algorithm", "patience")                  # the diff text may differ: the ids are not trusted
    runs = _diff_trees(monkeypatch)
    be._patch_ids(repo, shas)
    assert "diff-tree" in runs


def test_the_file_is_keyed_by_sha_only_and_holds_no_text(tmp_path, cache):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    be._patch_ids(repo, shas)
    doc = json.loads(cache.read_text())
    assert set(doc) == {"v", "stamps", "ids"}
    assert set(doc["ids"]) <= set(shas)                                    # every key is a full commit sha, nothing else
    assert all(be._HEX_ID.fullmatch(k) and (v is None or be._HEX_ID.fullmatch(v)) for k, v in doc["ids"].items())
    text = cache.read_text()
    for needle in ("feature work", "merge feature", "weird", str(repo), "t@t"):
        assert needle not in text
    assert oct(cache.stat().st_mode & 0o777) == "0o600" and oct(cache.parent.stat().st_mode & 0o777) == "0o700"


def test_the_file_is_bounded_and_keeps_what_this_read_used(tmp_path, cache, monkeypatch):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    monkeypatch.setattr(be, "PID_CACHE_MAX", 5)
    be._patch_ids(repo, shas[:12])
    be._PID_MEMO.clear()
    be._patch_ids(repo, shas[12:])                                         # more than the bound: the older entries go first
    ids = json.loads(cache.read_text())["ids"]
    assert len(ids) == 5 and set(ids) <= set(shas[12:] if len(shas) > 17 else shas)
    assert all(s in ids for s in shas[-5:]) or len(shas) < 17


def test_the_cache_lives_under_the_levain_home_and_no_home_is_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    path = be._pid_cache_path(str(tmp_path / "x"))
    assert path is not None and path.parent == tmp_path / "home" / "cache" / "board"
    assert be._pid_cache_path(str(tmp_path / "y")) != path                # one file per repo
    from levain.desk import work_attach
    def refused(*a, **k):
        raise RuntimeError("no levain home here")                           # the home guard's refusal in a test or scratch run
    monkeypatch.setattr(work_attach, "board_store_dir", refused)
    assert be._pid_cache_path(str(tmp_path / "x")) is None
    repo = _history(tmp_path)
    be._PID_MEMO.clear()
    assert be._patch_ids(repo, _all_shas(repo))                            # and the read still answers


def test_an_unwritable_cache_never_fails_the_read(tmp_path, monkeypatch):
    repo = _history(tmp_path)
    shas = _all_shas(repo)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the directory should be")
    monkeypatch.setattr(be, "_pid_cache_path", lambda key: blocker / "sub" / "p.json")
    be._PID_MEMO.clear()
    assert be._patch_ids(repo, shas)


_WRITER = """
import sys, json
from pathlib import Path
from levain.desk import board_evidence as be
path, digest, who, n = Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
for i in range(n):
    sha = f"{int(who):02x}" + f"{i:038x}"
    be._pid_cache_store(path, digest, {sha: sha[::-1]}, [sha])
"""


def test_processes_writing_at_once_never_tear_the_file(tmp_path):
    path = tmp_path / "shared" / "p.json"
    script = tmp_path / "writer.py"
    script.write_text(_WRITER)
    procs = [subprocess.Popen([sys.executable, str(script), str(path), "d", str(w), "25"], env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)})
             for w in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    doc = json.loads(path.read_text())                                     # parses: never half a file
    assert doc["v"] == be.PID_CACHE_V and doc["ids"]
    assert be._pid_cache_load(path, "d") == doc["ids"]                      # and passes every check a reader makes
    assert [p.name for p in path.parent.iterdir()] == ["p.json"]            # no temp file left behind
