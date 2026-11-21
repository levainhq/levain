"""The page driver's decline of a JavaScript dialog (tests/_dialog_decline.mjs): its retry decisions, run in node against stub sends on a virtual
clock (tests/_dialog_decline.test.mjs). No Chrome and no real time: a decline refused only because the page target is not attached yet is sent
again after a pause, up to a bound; success, any other error, or the bound ends it; the decline stays accept:false."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
TESTS = Path(__file__).parent


@pytest.mark.skipif(NODE is None, reason="node is needed")
def test_the_dialog_decline_decisions_hold_against_stub_sends() -> None:
    r = subprocess.run([str(NODE), "--test", str(TESTS / "_dialog_decline.test.mjs")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    assert "# fail 0" in r.stdout or "ℹ fail 0" in r.stdout, r.stdout[-2000:]
    assert "# pass 0" not in r.stdout and "ℹ pass 0" not in r.stdout, "no test ran"
