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


def _survivors() -> list[str]:
    out = subprocess.run(["ps", "-Ao", "pid,args"], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if "levain-run-" in ln and "ps -Ao" not in ln]


@pytest.mark.skipif(not Path(CHROME).exists() or not shutil.which("node"), reason="needs Chrome and node")
def test_shot_writes_png_and_a_raising_run_reaps_chrome(tmp_path):
    node = shutil.which("node")
    png = tmp_path / "s.png"
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps([{"shot": str(png)}]))
    r = _page_drive_run.run([node, DRIVER, CHROME, "about:blank", str(steps)], timeout=60)
    assert r.returncode == 0, r.stderr
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 100
    assert json.loads(r.stdout)["values"] == [{"shot": str(png)}]
    assert not _survivors()
    # a step that never resolves, under a short run timeout: run() raises, and nothing of it may survive
    steps.write_text(json.dumps([{"until": "false", "ms": 60000}]))
    with pytest.raises(subprocess.TimeoutExpired):
        _page_drive_run.run([node, DRIVER, CHROME, "about:blank", str(steps)], timeout=4)
    for _ in range(50):
        if not _survivors():
            break
        time.sleep(0.1)
    assert _survivors() == []
