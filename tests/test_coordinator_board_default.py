"""Row 160: the coordinator's lease verbs (take, release, beat, ready, status, watch) default to the board store."""
from __future__ import annotations

import argparse
import json

import pytest

from levain.cli import main
from levain.consent.queue import queue_dir_for
from levain.coordinator import cli as coord_cli
from levain.coordinator import work_attach


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    consent = queue_dir_for({"LEVAIN_HOME": str(home)})
    board = work_attach.board_store_dir()
    assert board != consent
    return consent, board


def test_take_status_ready_release_beat_reach_the_board_store_without_the_flag(homes, capsys):
    consent, board = homes
    assert main(["coord", "take", "--json"]) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    assert (board / "dispatch.db").exists() and not consent.exists()
    assert main(["coord", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["lease"] is not None
    assert main(["coord", "ready", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["store"] == str(board)
    assert main(["coord", "beat", "--token", str(token), "--json"]) == 0
    capsys.readouterr()
    assert main(["coord", "release", "--token", str(token), "--by", "coordinator", "--json"]) == 0
    capsys.readouterr()
    assert not consent.exists()


def test_an_explicit_queue_dir_still_wins(homes, tmp_path, capsys):
    consent, board = homes
    other = tmp_path / "elsewhere"
    assert main(["coord", "take", "--queue-dir", str(other), "--json"]) == 0
    capsys.readouterr()
    assert (other / "dispatch.db").exists() and not board.exists() and not consent.exists()


@pytest.mark.parametrize("cmd,board_verb", [("take", True), ("release", True), ("beat", True), ("ready", True),
                                            ("status", True), ("watch", True), ("launch", False), ("claim", False),
                                            ("questions", False), ("answer", False), ("log", False),
                                            ("morning", False), ("wake", False), ("close", False)])
def test_which_verbs_open_the_board(cmd, board_verb):
    assert coord_cli._opens_board(argparse.Namespace(coord_cmd=cmd, item=None)) is board_verb
