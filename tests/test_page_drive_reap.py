"""The page-driver runner leaves no Chrome behind, and its ``shot`` step writes a PNG."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _page_drive_run  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DRIVER = str(Path(__file__).parent / "_page_drive.mjs")


def _runs() -> set[str]:
    """PIDs of every process whose args name a levain-run scratch dir (any worktree, worker or CLI shot)."""
    out = subprocess.run(["ps", "-Ao", "pid,args"], capture_output=True, text=True).stdout
    return {ln.split()[0] for ln in out.splitlines() if "levain-run-" in ln and "ps -Ao" not in ln}


def _survivors(before: set[str]) -> set[str]:
    """Matching PIDs that were not there before this test's run: only its own, polled for up to 5 s."""
    left = _runs() - before
    for _ in range(50):
        if not left:
            break
        time.sleep(0.1)
        left = _runs() - before
    return left


@pytest.mark.skipif(not Path(CHROME).exists() or not shutil.which("node"), reason="needs Chrome and node")
def test_shot_writes_png_and_a_raising_run_reaps_chrome(tmp_path):
    node = shutil.which("node")
    png = tmp_path / "s.png"
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps([{"shot": str(png)}]))
    before = _runs()
    r = _page_drive_run.run([node, DRIVER, CHROME, "about:blank", str(steps)], timeout=60)
    assert r.returncode == 0, r.stderr
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 100
    assert json.loads(r.stdout)["values"] == [{"shot": str(png)}]
    assert not _survivors(before)
    # a step that never resolves, under a short run timeout: run() raises, and nothing of it may survive
    steps.write_text(json.dumps([{"until": "false", "ms": 60000}]))
    before = _runs()
    with pytest.raises(subprocess.TimeoutExpired):
        _page_drive_run.run([node, DRIVER, CHROME, "about:blank", str(steps)], timeout=4)
    assert _survivors(before) == set()


@pytest.mark.skipif(not Path(CHROME).exists() or not shutil.which("node"), reason="needs Chrome and node")
def test_an_until_that_never_comes_true_fails_the_drive_unless_it_may_time_out(tmp_path):
    node = shutil.which("node")
    steps = tmp_path / "steps.json"

    def drive(step):
        steps.write_text(json.dumps([step]))
        return _page_drive_run.run([node, DRIVER, CHROME, "about:blank", str(steps)], timeout=60)

    r = drive({"until": "document.title === 'never'", "ms": 300})
    assert r.returncode != 0 and "step 0" in r.stderr and "document.title === 'never'" in r.stderr, (r.returncode, r.stderr)
    r = drive({"until": "document.title === 'never'", "ms": 300, "may_timeout": True})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["values"] == [{"timeout": "document.title === 'never'"}]


@pytest.mark.skipif(not Path(CHROME).exists() or not shutil.which("node"), reason="needs Chrome and node")
def test_a_posts_wait_that_comes_up_short_fails_the_drive_and_leaves_a_picture_of_the_page(tmp_path):
    node = shutil.which("node")
    steps = tmp_path / "steps.json"

    def drive(step):
        steps.write_text(json.dumps([step]))
        return _page_drive_run.run([node, DRIVER, CHROME, "about:blank", str(steps)], timeout=60)

    r = drive({"posts": 1, "ms": 300})
    assert r.returncode != 0 and "step 0: posts never reached 1" in r.stderr, (r.returncode, r.stderr)
    assert (tmp_path / "timeout-step-0.png").stat().st_size > 0 and "timeout-step-0.png" in r.stderr       # what the page showed
    r = drive({"posts": 1, "ms": 300, "may_timeout": True})
    assert r.returncode == 0, r.stderr
