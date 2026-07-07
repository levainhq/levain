"""Tests for the levain CLI dispatch (levain/cli.py)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from levain.cli import main


def test_init_web_threads_packs_to_run_init_web():
    # `--web --pack` is now supported: the browser interview composes the pack
    # too (the mutual-exclusivity guard is gone). _cmd_init must thread the
    # repeatable --pack values through to run_init_web as `packs`.
    with mock.patch("levain.init_server.run_init_web", return_value=0) as m:
        rc = main([
            "init", "--web",
            "--pack", "./pressable-domain",
            "--pack", "./pressable-solutions-role",
        ])
    assert rc == 0
    kwargs = m.call_args.kwargs
    assert kwargs["packs"] == [Path("./pressable-domain"), Path("./pressable-solutions-role")]


def test_init_web_without_pack_passes_no_packs():
    # Base-only web onboarding (no --pack) threads `packs=None` — unchanged behavior.
    with mock.patch("levain.init_server.run_init_web", return_value=0) as m:
        rc = main(["init", "--web"])
    assert rc == 0
    assert m.call_args.kwargs["packs"] is None


def test_init_web_with_bad_pack_fails_clean(capsys: pytest.CaptureFixture[str]):
    # A pack that does not compose (here: a nonexistent dir with no pack.toml) must
    # fail clean BEFORE the server binds — run_init_web validates the composition
    # up-front and returns nonzero rather than 500-ing the first request.
    rc = main(["init", "--web", "--pack", "/tmp/levain-nonexistent-pack-xyz"])
    assert rc == 1
    assert "pack composition failed" in capsys.readouterr().err
