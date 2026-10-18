"""Presence-aware away mode: the explicit word, then input idle, then off; a held request follows a flip to off.

Built from the runs in levain project_memory/1017-presence/AUTOAWAY_RUN.md (the real hook entry point, the real
`levain consent away` verb, and Codex 0.162.1)."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import threading
import time

import pytest

from levain.cli import main as cli
from levain.consent import auto_away, away
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
from levain.consent.auto_away import input_idle_seconds as real_idle_read
from tests.test_consent_away import _cockpit, _queue


@pytest.fixture(scope="module")
def cockpit(tmp_path_factory):
    return _cockpit(tmp_path_factory.mktemp("ck"))


@pytest.fixture
def qdir(tmp_path, cockpit):
    """A queue whose settings files are what `levain init` writes now: away_word auto."""
    q = _queue(tmp_path, cockpit)
    for name in (cc.SETTINGS_NAME, cx.SETTINGS_NAME):
        (q / name).write_text(json.dumps({**json.loads((q / name).read_text()), "away_word": "auto"}))
    return q


@pytest.fixture
def idle(monkeypatch):
    """This Mac's input idle, as the test sets it (``idle[0]`` seconds)."""
    now = [0.0]
    monkeypatch.setattr(auto_away, "input_idle_seconds", lambda: now[0])
    return now


EVENTS = {
    "claude_code": (cc, {"hook_event_name": "PermissionRequest", "session_id": "s", "cwd": "/tmp", "tool_name": "Write",
                         "tool_input": {"file_path": "/tmp/x.txt", "content": "hi"}, "permission_mode": "default",
                         "tool_use_id": "tu-1"}),
    "codex": (cx, {"hook_event_name": "PermissionRequest", "session_id": "s", "cwd": "/tmp", "tool_name": "Bash",
                   "tool_input": {"command": "touch x", "description": "d"}, "permission_mode": "default"}),
}


def _hook(mod, event, qdir):
    """The hook entry point on its own thread: returns (thread, stdout, started)."""
    out = io.StringIO()
    t = threading.Thread(target=mod.main, args=(["--queue-dir", str(qdir)],),
                         kwargs={"stdin": io.StringIO(json.dumps(event)), "stdout": out}, daemon=True)
    started = time.monotonic()
    t.start()
    return t, out, started


def _row(qdir):
    c = sqlite3.connect(qdir / "queue.db")
    try:
        return c.execute("SELECT state, note FROM requests ORDER BY received_at DESC LIMIT 1").fetchone()
    finally:
        c.close()


def _wait_pending(qdir, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        try:
            if (qdir / "queue.db").exists() and (_row(qdir) or (None,))[0] == "pending":
                return
        except sqlite3.OperationalError:
            pass                                # the file exists before the hook has created its tables
        time.sleep(0.05)
    raise AssertionError("the hook never held the request")


def test_the_word_off_mid_hold_releases_to_the_agents_dialog(qdir, capsys):
    # runs A1 (Claude Code shape) and X2 (Codex 0.162.1): the hold ends about a second after `away off`, no answer
    for agent in ("claude_code", "codex"):
        mod, event = EVENTS[agent]
        assert cli(["consent", "away", "on", "--hold", "15", "--agent", agent, "--queue-dir", str(qdir)]) == 0
        t, out, started = _hook(mod, event, qdir)
        _wait_pending(qdir)
        assert cli(["consent", "away", "off", "--agent", agent, "--queue-dir", str(qdir)]) == 0
        flipped = time.monotonic()
        t.join(10)
        assert not t.is_alive() and time.monotonic() - flipped < cc.RECHECK_EVERY + 3
        assert out.getvalue() == ""  # no decision: the agent's own dialog asks
        state, note = _row(qdir)
        assert state == "withdrawn" and note.startswith("away mode ended (explicit word: off)")


def test_input_idle_holds_and_input_releases(qdir, idle, capsys, tmp_path):
    # runs S0, I1 and X1: an init file (no word) holds while input idle is past the threshold, and stops holding
    # when input idle drops under it (lane B measured HIDIdleTime snapping to ~0 on input)
    idle[0] = 700.0
    t, out, _ = _hook(cc, EVENTS["claude_code"][1], qdir)
    _wait_pending(qdir)
    idle[0] = 2.0
    t.join(10)
    assert not t.is_alive() and out.getvalue() == ""
    assert _row(qdir) == ("withdrawn", "away mode ended (input idle 2s < 600s); the native dialog stands")
    # run_h2 (a): an idle hold whose cockpit can no longer sign (its database gone) is released at the recheck
    (tmp_path / "own").mkdir()
    own = _cockpit(tmp_path / "own")
    settings = json.loads((qdir / cc.SETTINGS_NAME).read_text())
    (qdir / cc.SETTINGS_NAME).write_text(json.dumps({**settings, "cockpit_data_root": str(own)}))
    idle[0] = 700.0
    t, out, _ = _hook(cc, {**EVENTS["claude_code"][1], "tool_use_id": "tu-2"}, qdir)
    _wait_pending(qdir)
    os.rename(own / "cockpit.db", own / "cockpit.db.gone")
    t.join(10)
    assert not t.is_alive() and out.getvalue() == "" and _row(qdir) == (
        "withdrawn", "away mode ended (input idle 700s >= 600s, but no enrolled key could sign an answer); the "
                     "native dialog stands")


def test_the_explicit_word_outranks_input_idle_until_auto_clears_it(qdir, idle, capsys):
    # runs S0, I1, I2 and I3: off holds nothing however idle, on holds however present, auto hands it back to
    # idle; `show` names the source each time. Run H1: a file levain 0.11.4 wrote (its own `away on --hold 90`
    # then `away off`, from PyPI) is the word off, never handed to input idle by the upgrade
    legacy = qdir.parent / "legacy"
    legacy.mkdir(mode=0o700)
    os.chmod(legacy, 0o700)
    root = json.loads((qdir / cx.SETTINGS_NAME).read_text())["cockpit_data_root"]
    (legacy / cx.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": root, "away_mode": False,
                                                       "away_hold_seconds": 90.0}, indent=2) + "\n")
    idle[0] = 4000.0
    cli(["consent", "away", "show", "--agent", "codex", "--queue-dir", str(legacy)])
    assert "codex: away mode off;" in (out := capsys.readouterr().out)
    assert "set by explicit word: off (set before auto-away existed); `levain consent away auto` lets input idle " \
           "decide" in out
    (legacy / cx.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": root, "away_mode": True,
                                                       "away_hold_seconds": 90.0}, indent=2) + "\n")
    cli(["consent", "away", "show", "--agent", "codex", "--queue-dir", str(legacy)])
    # run_h2 R3: a legacy ON is told the way back to input idle too
    assert "`levain consent away auto` hands it back to input idle" in capsys.readouterr().out
    (legacy / cx.SETTINGS_NAME).write_text(json.dumps({"cockpit_data_root": root, "away_mode": False,
                                                       "away_hold_seconds": 90.0}, indent=2) + "\n")
    cli(["consent", "away", "auto", "--agent", "codex", "--queue-dir", str(legacy)])
    assert "away mode ON, holds up to 90s" in capsys.readouterr().out  # the 0.11.4 hold is kept, not defaulted
    assert json.loads((legacy / cx.SETTINGS_NAME).read_text())["away_hold_seconds"] == 90.0
    idle[0] = 213.0
    cli(["consent", "away", "show", "--agent", "codex", "--queue-dir", str(qdir)])
    assert "codex: away mode off; answers verify against" in (out := capsys.readouterr().out)
    assert "set by input idle 213s < 600s; on after 600s of no keyboard, mouse or trackpad input" in out
    cli(["consent", "away", "auto", "--idle-after", "1", "--agent", "codex", "--queue-dir", str(qdir)])
    assert "codex: away mode ON, holds up to 120s" in (out := capsys.readouterr().out)
    assert "set by input idle 213s >= 1s" in out
    assert cli(["consent", "away", "on", "--idle-after", "5", "--queue-dir", str(qdir)]) == 2
    idle[0] = 700.0
    assert cli(["consent", "away", "off", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert "set by explicit word: off; `levain consent away auto` hands it back to input idle" in \
        capsys.readouterr().out
    t, out, started = _hook(cc, EVENTS["claude_code"][1], qdir)
    t.join(10)
    assert time.monotonic() - started < 2 and _row(qdir) == (
        "withdrawn", "the hook stopped waiting; the native dialog stands")
    idle[0] = 0.0
    assert cli(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert auto_away.away_state(cc.load_settings(qdir))[:2] == (True, "word")
    assert cli(["consent", "away", "auto", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert json.loads((qdir / cc.SETTINGS_NAME).read_text())["away_word"] == "auto"
    assert auto_away.away_state(cc.load_settings(qdir))[:2] == (False, "idle")
    # run_h2: an integer too large for a float is refused by set_away itself (the CLI's float parse never gets there)
    with pytest.raises(ValueError, match="--idle-after"):
        away.set_away(qdir, "claude_code", None, None, 10 ** 400)


def test_a_failed_idle_read_is_no_idle_source_never_away(monkeypatch):
    # run_idle_read: ioreg's HIDIdleTime line read as seconds; a missing, hung or unparseable ioreg reads as no
    # source. Off macOS there is no source at all. (Whether this host's own ioreg answers is not asserted.)
    got = real_idle_read()
    assert got is None or got >= 0
    monkeypatch.setattr(auto_away.sys, "platform", "linux")
    assert real_idle_read() is None
    monkeypatch.setattr(auto_away.sys, "platform", "darwin")
    canned = subprocess.CompletedProcess([], 0, stdout=b'    | |   "HIDIdleTime" = 384984607958\n')
    monkeypatch.setattr(auto_away.subprocess, "run", lambda *a, **k: canned)
    assert abs(real_idle_read() - 384.984607958) < 1e-9
    monkeypatch.undo()
    monkeypatch.setattr(auto_away.sys, "platform", "darwin")
    for argv in (("/nonexistent/ioreg",), ("/bin/sleep", "5"), ("/bin/echo", "nothing here")):
        monkeypatch.setattr(auto_away, "_IOREG", argv)
        started = time.monotonic()
        assert real_idle_read() is None and time.monotonic() - started < auto_away.IDLE_READ_TIMEOUT + 1
    assert auto_away.away_state(cc.AdapterSettings(), idle=lambda: None) == (
        False, "none", "no input-idle source on this platform: only `levain consent away on` turns it on")
