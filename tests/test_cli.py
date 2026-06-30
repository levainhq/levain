"""Tests for the levain CLI dispatch (levain/cli.py)."""

from __future__ import annotations

import pytest

from levain.cli import main


def test_init_web_with_pack_fails_loud(capsys: pytest.CaptureFixture[str]):
    # --pack is unsupported by web onboarding — it must fail loud, never quietly
    # produce a base-only install the operator believes includes their pack
    # (codex L3 LOW, Slice 2). The guard is the first thing _cmd_init does, so no
    # server is bound and no interview runs.
    rc = main(["init", "--web", "--pack", "/tmp/somepack"])
    assert rc == 1
    assert "--pack is not supported with --web" in capsys.readouterr().out
