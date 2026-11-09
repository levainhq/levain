"""Row 160: a coordinator verb opens the store its coordinator is in. A bare ``coord take`` on a fresh home makes the
board's coordinator; every verb after it, bare, follows that coordinator to its store, and a consent coordinator's
verbs stay in the consent directory."""
from __future__ import annotations

import json

import pytest

from levain.cli import main
from levain.consent.queue import queue_dir_for
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


def _run(capsys, *argv):
    code = main(["coord", *argv])
    got = capsys.readouterr()
    return code, got.out, got.err


def _json(capsys, *argv):
    code, out, err = _run(capsys, *argv, "--json")
    assert code == 0, err
    return json.loads(out)


def test_take_status_ready_release_beat_reach_the_board_store_without_the_flag(homes, capsys):
    consent, board = homes
    token = _json(capsys, "take")["token"]
    assert (board / "dispatch.db").exists() and not consent.exists()
    assert _json(capsys, "status")["lease"]["token"] == token
    assert _json(capsys, "ready", "show")["store"] == str(board)
    _json(capsys, "beat", "--token", str(token))
    _json(capsys, "release", "--token", str(token), "--by", "coordinator")
    assert not consent.exists()


def test_an_explicit_queue_dir_still_wins(homes, tmp_path, capsys):
    consent, board = homes
    other = tmp_path / "elsewhere"
    _json(capsys, "take", "--queue-dir", str(other))
    assert (other / "dispatch.db").exists() and not board.exists() and not consent.exists()


def test_a_board_coordinators_whole_family_resolves_to_the_board(homes, capsys):
    """The bare take makes the board's coordinator; the setting, the watch, the seam verbs and the status after it all
    reach that same dispatch.db, and the consent directory is never made."""
    consent, board = homes
    token = _json(capsys, "take")["token"]
    assert _json(capsys, "agent", "none")["agent"] == "none"
    st = _json(capsys, "status")
    assert st["setting"]["agent"] == "none" and st["lease"]["token"] == token
    _json(capsys, "watch", "--once")
    # the operator's token is known to the board: the open's refusal is about whose lease it is, not a missing lease
    code, out, err = _run(capsys, "open", "--token", str(token))
    assert "is not a live coordinator lease's" not in out + err
    code, out, err = _run(capsys, "launch")
    assert code == 1 and "no coordinator launch is pending" in err
    assert _json(capsys, "status")["lease"]["token"] == token          # the take's lease, still held, still here
    assert not consent.exists()


def test_a_consent_coordinators_whole_family_stays_in_the_consent_directory(homes, capsys):
    """A coordinator already in the consent directory keeps every bare verb there: take, status, watch, beat and
    release all reach it, and no board is made."""
    consent, board = homes
    _json(capsys, "agent", "none")                      # nothing anywhere yet: the setting goes to the consent store
    assert (consent / "dispatch.db").exists()
    token = _json(capsys, "take")["token"]
    st = _json(capsys, "status")
    assert st["setting"]["agent"] == "none" and st["lease"]["token"] == token
    _json(capsys, "watch", "--once")
    _json(capsys, "beat", "--token", str(token))
    code, out, err = _run(capsys, "open", "--token", str(token))
    assert "is not a live coordinator lease's" not in out + err
    _json(capsys, "release", "--token", str(token), "--by", "coordinator")
    assert _json(capsys, "status")["lease"] is None
    assert not board.exists()


def test_a_token_finds_its_store_when_both_hold_a_coordinator(homes, capsys):
    consent, board = homes
    _json(capsys, "agent", "none")                      # a consent coordinator store, no lease
    token = _json(capsys, "take", "--queue-dir", str(board))["token"]
    held = _json(capsys, "beat", "--token", str(token))
    assert held["token"] == token
    # the bare status follows the one live coordinator, the board's lease
    assert _json(capsys, "status")["lease"]["token"] == token


def test_both_stores_holding_a_coordinator_and_nothing_to_choose_by_refuses(homes, capsys):
    consent, board = homes
    _json(capsys, "agent", "none")
    _json(capsys, "agent", "none", "--queue-dir", str(board))
    code, out, err = _run(capsys, "status")
    assert code == 1 and out == ""
    assert "--queue-dir" in err and str(consent) in err and str(board) in err
    # a named store still answers
    assert _json(capsys, "status", "--queue-dir", str(consent))["setting"]["agent"] == "none"


def test_a_hint_names_the_store_a_bare_command_would_miss(homes, capsys):
    """A follow-up the CLI suggests reaches the store the command ran on: bare when a bare verb opens that store,
    with its --queue-dir when it would not."""
    consent, board = homes
    _json(capsys, "agent", "none")                      # the consent store is the only one: bare reaches it
    code, out, err = _run(capsys, "launch")
    assert code == 1 and "(levain coord status says why; levain coord wake asks for one)" in err
    token = _json(capsys, "take", "--queue-dir", str(board))["token"]      # now the board's lease is the live one
    code, out, err = _run(capsys, "launch", "--queue-dir", str(consent))
    assert code == 1 and f"levain coord status --queue-dir {consent} says why" in err
    code, out, err = _run(capsys, "import-status", str(board / "absent.md"), "--token", str(token + 1))
    assert code == 1 and f"levain coord status --queue-dir {board} shows the live one" in err
