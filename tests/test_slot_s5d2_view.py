"""5d-ii S4a: the operator CLI's view and ``--repo`` checks (:mod:`levain.slot.adopt_view`), from failures the RUN
reproduced (``project_memory/1023-8/adopt/s4a_expected.md``, its revisions): an unopenable repository was named
``repo-not-a-root``; the A4 header printed twice; each takeable ref printed as ``refs/heads/refs/heads/...``."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from levain.slot import Refused, adopt_verify
from levain.slot import adopt_take as AT
from levain.slot import adopt_view as AVW
from levain.slot import bundlefmt as BF
from levain.slot import runtime_helper as RH

S = "s-" + "a4" * 8
BASE = {"oid": "d" * 40, "object_format": "sha1"}


def _seams(answers):
    """His git answered from ``answers`` (argv words after ``-c core.hooksPath=/dev/null`` -> (rc, stdout, stderr));
    every call recorded."""
    calls = []

    def spawn(argv, **kw):
        assert "env" in kw
        assert argv[3:5] == [f"--git-dir={kw['cwd']}/.git", f"--work-tree={kw['cwd']}"]     # never discovered
        words = tuple(argv[5:7])
        calls.append(words)
        rc, out, err = answers[words]
        return subprocess.CompletedProcess(argv, rc, out, err)

    return AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git="git", spawn=spawn), calls


def test_view_unopenable_repo_is_repo_unreadable(tmp_path: Path) -> None:
    # git 2.54 without Rust refuses a repo with extensions.compatObjectFormat at its first command (s4a run 1, R5b).
    (tmp_path / ".git").mkdir()
    seams, calls = _seams({("config", "--local"): (128, b"", b"fatal: compatibility hash algorithm support requires "
                                                              b"Rust\n")})
    try:
        AVW.check_repo(seams, tmp_path, BASE)
    except Refused as e:
        assert e.name == "repo-unreadable:128" and "requires Rust" in e.detail
    else:
        raise AssertionError("an unopenable repository passed")
    assert calls == [("config", "--local")]           # nothing after the read that failed


def test_view_render_header_once_and_ref_names(tmp_path: Path) -> None:
    preview = (adopt_verify.PREVIEW_HEADER + "\nresult delta\nevil \x1b[31m\x07\n").encode()
    lines = AVW.render(session=S, result="delta", refs=["head", "wip"], preview=preview,
                       report={"named": [], "truncated": False}, repo=tmp_path, private=tmp_path / "p")
    text = "\n".join(lines)
    assert text.count(adopt_verify.PREVIEW_HEADER) == 1 and lines[0] == adopt_verify.PREVIEW_HEADER
    assert f"refs you can take: head -> refs/heads/levain/{S}, wip -> refs/heads/levain/{S}-wip" in lines
    assert "\x1b" not in text and "\x07" not in text and "\\u001b" in text


# --- fix1 (L3 s4a r1), each from a row that failed on the pre-fix tip (1023-8/adopt/s4af1_red_prefix.txt) ---------

GIT = shutil.which("git")
_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}


def _real_repo(d: Path) -> str:
    for a in (["init", "-q", str(d)], ["-C", str(d), "commit", "-q", "--allow-empty", "-m", "b"]):
        subprocess.run([GIT, *a], env=_ENV, check=True, capture_output=True)
    return subprocess.run([GIT, "-C", str(d), "rev-parse", "HEAD"], env=_ENV, check=True,
                          capture_output=True).stdout.decode().strip()


@pytest.mark.skipif(GIT is None, reason="no git")
def test_view_bare_promisor_is_partial(tmp_path: Path) -> None:
    # complement MED 1: a bare `promisor` line is git's true; the pre-fix hand parser read it as false (row R8).
    oid = _real_repo(tmp_path)
    with open(tmp_path / ".git" / "config", "a") as fh:
        fh.write('[remote "origin"]\n\turl = /nonexistent\n\tpromisor\n')
    with pytest.raises(Refused) as e:
        AVW.check_repo(AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git=GIT), tmp_path,
                       {"oid": oid, "object_format": "sha1"})
    assert e.value.name == "repo-partial"


@pytest.mark.skipif(GIT is None, reason="no git")
def test_empty_git_dir_never_reaches_the_parent(tmp_path: Path) -> None:
    # complement MED 2: an empty .git inside a real repo; the pre-fix git walked up and wrote the parent (row R9).
    oid = _real_repo(tmp_path)
    child = tmp_path / "child"
    (child / ".git").mkdir(parents=True)
    before = subprocess.run([GIT, "-C", str(tmp_path), "for-each-ref"], env=_ENV, capture_output=True).stdout
    with pytest.raises(Refused) as e:
        AVW.check_repo(AVW.Seams(helper=("FAKE",), store=Path("/nonexistent"), git=GIT), child,
                       {"oid": oid, "object_format": "sha1"})
    assert e.value.name.startswith("repo-unreadable:")
    assert AT._git(GIT, child, "update-ref", "refs/heads/probe", oid).returncode != 0
    assert subprocess.run([GIT, "-C", str(tmp_path), "for-each-ref"], env=_ENV, capture_output=True).stdout == before


def test_verify_wall_derives_from_the_helpers_bounds() -> None:
    # codex HIGH 2: the pre-fix fixed 4,200 s was below a valid adopt-verify deadline at V7's sizes (row H1).
    mx = RH.V7_BYTES + BF.HEADER_MAX
    got = AVW.helper_timeout("adopt-verify", bundle_bytes=mx, base_bytes=mx)
    assert got == RH.VerifySeams().lock_wait_s + RH.verify_wall_s(mx, mx) + RH.RESULT_GRACE_S + AVW.HELPER_SLACK_S
    assert got > 4200
    assert AVW.helper_timeout("adopt-open") == RH.AdoptSeams().lock_wait_s + RH.RESULT_GRACE_S + AVW.HELPER_SLACK_S


def test_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    # codex HIGH 2: a grandchild outlived the pre-fix subprocess.run timeout (row H2).
    pidf = tmp_path / "pid"
    with pytest.raises(subprocess.TimeoutExpired):
        AVW._run(["/bin/sh", "-c", 'sleep 300 & echo $! > "$0"; wait', str(pidf)], env={"PATH": "/bin:/usr/bin"},
                 timeout=1.0)
    pid = int(pidf.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    raise AssertionError("the grandchild outlived the timeout")


def test_copy_private_unlinks_a_bad_copy(tmp_path: Path) -> None:
    # codex MED 7: the pre-fix copy left an unauthenticated destination behind (row C2).
    src_path = tmp_path / "src"
    src_path.write_bytes(b"x" * 100)
    fd = os.open(str(src_path), os.O_RDONLY)
    try:
        with pytest.raises(Refused) as e:
            AVW.copy_private(fd, 100, tmp_path / "dst", "0" * 64)
    finally:
        os.close(fd)
    assert e.value.name == "export-changed" and not (tmp_path / "dst").exists()


def test_warnings_shown_escaped_and_typed() -> None:
    # codex MED 6: the pre-fix CLI dropped every helper warning (rows W1, W2).
    err = io.StringIO()
    assert AVW.show_warnings({"warnings": ["fsync \x1b[31m"]}, "adopt-verify", err) == ["fsync \\u001b[31m"]
    assert err.getvalue() == "WARNING (adopt-verify): fsync \\u001b[31m\n"
    with pytest.raises(Refused) as e:
        AVW.show_warnings({"warnings": [7]}, "adopt-open", io.StringIO())
    assert e.value.name == "adopt-answer-malformed"
