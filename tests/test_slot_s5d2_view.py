"""5d-ii S4a: the operator CLI's view and ``--repo`` checks (:mod:`levain.slot.adopt_view`), from failures the RUN
reproduced (``project_memory/1023-8/adopt/s4a_expected.md``, its revisions): an unopenable repository was named
``repo-not-a-root``; the A4 header printed twice; each takeable ref printed as ``refs/heads/refs/heads/...``."""
from __future__ import annotations

import subprocess
from pathlib import Path

from levain.slot import Refused, adopt_verify
from levain.slot import adopt_view as AVW

S = "s-" + "a4" * 8
BASE = {"oid": "d" * 40, "object_format": "sha1"}


def _seams(answers):
    """His git answered from ``answers`` (argv words after ``-c core.hooksPath=/dev/null`` -> (rc, stdout, stderr));
    every call recorded."""
    calls = []

    def spawn(argv, **kw):
        assert "env" in kw
        words = tuple(argv[3:5])
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


