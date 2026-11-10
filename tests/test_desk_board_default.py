"""Row 160: a desk verb opens the store its desk is in. A bare ``coord take`` on a fresh home makes the
board's desk; every verb after it, bare, follows that desk to its store, and a consent desk's
verbs stay in the consent directory."""
from __future__ import annotations

import json

import pytest

from levain.cli import main
from levain.consent.queue import queue_dir_for
from levain.desk import work_attach
from levain.desk.legacy import ROLE_DESK


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
    _json(capsys, "release", "--token", str(token), "--by", ROLE_DESK)
    assert not consent.exists()


def test_an_explicit_queue_dir_still_wins(homes, tmp_path, capsys):
    consent, board = homes
    other = tmp_path / "elsewhere"
    _json(capsys, "take", "--queue-dir", str(other))
    assert (other / "dispatch.db").exists() and not board.exists() and not consent.exists()


def test_a_board_desks_whole_family_resolves_to_the_board(homes, capsys):
    """The bare take makes the board's desk; the setting, the watch, the seam verbs and the status after it all
    reach that same dispatch.db, and the consent directory is never made."""
    consent, board = homes
    token = _json(capsys, "take")["token"]
    assert _json(capsys, "agent", "none")["agent"] == "none"
    st = _json(capsys, "status")
    assert st["setting"]["agent"] == "none" and st["lease"]["token"] == token
    _json(capsys, "watch", "--once")
    # the operator's token is known to the board: the open's refusal is about whose lease it is, not a missing lease
    code, out, err = _run(capsys, "open", "--token", str(token))
    assert "is not a live desk lease's" not in out + err
    code, out, err = _run(capsys, "launch")
    assert code == 1 and "no desk launch is pending" in err
    assert _json(capsys, "status")["lease"]["token"] == token          # the take's lease, still held, still here
    assert not consent.exists()


def test_a_consent_desks_whole_family_stays_in_the_consent_directory(homes, capsys):
    """A desk already in the consent directory keeps every bare verb there: take, status, watch, beat and
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
    assert "is not a live desk lease's" not in out + err
    _json(capsys, "release", "--token", str(token), "--by", ROLE_DESK)
    assert _json(capsys, "status")["lease"] is None
    assert not board.exists()


def test_a_token_finds_its_store_when_both_hold_a_desk(homes, capsys):
    consent, board = homes
    _json(capsys, "agent", "none")                      # a consent desk store, no lease
    token = _json(capsys, "take", "--queue-dir", str(board))["token"]
    held = _json(capsys, "beat", "--token", str(token))
    assert held["token"] == token
    # the bare status follows the one live desk, the board's lease
    assert _json(capsys, "status")["lease"]["token"] == token


def test_both_stores_holding_a_desk_and_nothing_to_choose_by_refuses(homes, capsys):
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
    assert code == 1 and "(levain desk status says why; levain desk wake asks for one)" in err
    token = _json(capsys, "take", "--queue-dir", str(board))["token"]      # now the board's lease is the live one
    code, out, err = _run(capsys, "launch", "--queue-dir", str(consent))
    assert code == 1 and f"levain desk status --queue-dir {consent} says why" in err
    code, out, err = _run(capsys, "import-status", str(board / "absent.md"), "--token", str(token + 1))
    assert code == 1 and f"levain desk status --queue-dir {board} shows the live one" in err


def test_a_store_a_drop_made_holds_no_desk(homes, capsys):
    """A phone or cockpit drop makes the consent store's tables. With the board's desk released, that store does
    not make the bare verbs ambiguous: they still reach the board's."""
    from levain.desk.envelopes import EnvelopeStore
    from levain.desk.store import CoordStore
    consent, board = homes
    token = _json(capsys, "take")["token"]
    _json(capsys, "release", "--token", str(token), "--by", ROLE_DESK)
    EnvelopeStore(consent).accept_drop("from the couch", surface="phone")
    assert CoordStore(consent).desk_present() and CoordStore(consent).holds_desk() is False
    assert _json(capsys, "agent", "none")["agent"] == "none"
    assert _json(capsys, "status")["setting"]["agent"] == "none"
    assert _json(capsys, "status", "--queue-dir", str(board))["setting"]["agent"] == "none"
    assert CoordStore(consent).holds_desk() is False


def test_a_dispatch_only_file_in_the_consent_dir_is_no_desk_store(homes, capsys):
    """A dispatch.db the job store made (no desk tables) is not a desk store: with no board store, the
    bare take still makes the board's desk."""
    from levain.desk.store import CoordStore
    from levain.dispatch.jobs import JobStore
    consent, board = homes
    with JobStore(consent)._connect():
        pass
    assert (consent / "dispatch.db").exists()
    assert not CoordStore(consent).desk_present() and CoordStore(consent).holds_desk() is False
    token = _json(capsys, "take")["token"]
    assert (board / "dispatch.db").exists()
    assert _json(capsys, "status")["lease"]["token"] == token


def test_a_stale_token_in_both_stores_is_refused_by_name(homes, capsys):
    """A token both stores hold that neither current lease uses is the "in both" refusal, never a fall-through to
    whichever store is live."""
    consent, board = homes
    for qdir in (consent, board):
        first = _json(capsys, "take", "--queue-dir", str(qdir))["token"]
        _json(capsys, "release", "--token", str(first), "--by", ROLE_DESK)
        _json(capsys, "take", "--queue-dir", str(qdir))
    code, out, err = _run(capsys, "beat", "--token", str(first))
    assert code == 1 and f"token {first} is in both {consent} and {board}" in err


def test_the_refusal_says_unknown_for_a_store_that_cannot_be_read(homes, capsys, monkeypatch):
    """An older-format store's liveness is not guessed: the refusal says it is unknown."""
    consent, board = homes
    _json(capsys, "agent", "none")
    _json(capsys, "agent", "none", "--queue-dir", str(board))
    from levain.desk import store as store_mod
    real = store_mod.CoordStore.open_readonly

    def unreadable(self, *a, **k):
        if self.directory == consent:
            raise store_mod.StoreAbsent("older format")
        return real(self, *a, **k)
    monkeypatch.setattr(store_mod.CoordStore, "open_readonly", unreadable)
    code, out, err = _run(capsys, "status")
    assert code == 1 and "unknown whether one is live in " + str(consent) in err


def _older_store(directory, *, launches_without_role=False):
    """A real older-format store file: it has `threads` (so a desk store is there) and none of the lease tables
    (or, for the launches case, a launches table that predates the role column)."""
    import sqlite3
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    conn = sqlite3.connect(directory / "dispatch.db")
    conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
    if launches_without_role:
        conn.execute("CREATE TABLE lease_events (gseq INTEGER)")
        conn.execute("CREATE TABLE coord_settings (gseq INTEGER)")
        conn.execute("CREATE TABLE launches (launch_id TEXT)")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("no_role", [False, True])
def test_an_older_format_store_is_unknown_never_empty(homes, no_role):
    from levain.desk.store import CoordStore
    consent, board = homes
    _older_store(consent, launches_without_role=no_role)
    assert CoordStore(consent).desk_present()
    assert CoordStore(consent).holds_desk() is None


@pytest.mark.parametrize("no_role", [False, True])
def test_an_older_format_store_beside_a_live_one_refuses(homes, capsys, no_role):
    consent, board = homes
    _older_store(consent, launches_without_role=no_role)
    _json(capsys, "take", "--queue-dir", str(board))                 # the board's desk is live
    code, out, err = _run(capsys, "status")
    assert code == 1 and out == ""
    assert "unknown whether one is live in " + str(consent) in err


def test_an_older_format_store_alone_is_still_the_store(homes, capsys):
    consent, board = homes
    _older_store(consent)
    from levain.desk import cli
    import argparse
    got = cli._desk_dir(argparse.Namespace(coord_cmd="status", token=None))
    assert got == consent
