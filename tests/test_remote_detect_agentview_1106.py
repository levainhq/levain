# ruff: noqa: F811
"""B1-AV: a prompt typed in a remote agent view client is found by its keystroke.

A ``--bg`` desk's hook runs under the daemon, and an agent view client carries no session id in its argv, so neither the
ancestry nor the attach leg sees it. Leg 4 ties the client's tty to the session's tty (both are READ for one keystroke,
so their access times sit within ``TIE_S``: milliseconds apart, or up to 2 s when the kernel defers one update), with a
fallback of "a remote client is open and the laptop is idle".
Everything here is synthetic: a ``ps`` table text, tty access times and a laptop idle time, all seams of
``terminal_sign``; no process, tty, mosh or Touch ID sheet is touched.
"""
from __future__ import annotations

import time

import pytest

from levain import band_view
from levain.cockpit.keys import KeyError_
from levain.consent import terminal_sign as ts
from tests.test_termsign_l2_seat import (VIEWER, env, keydir, qdir, recorded, seen, sidecar, store)  # noqa: F401

CLAUDE = "/Users/phillipclapham/.local/bin/claude"
SID = "1e37cf45-7708-4a11-9b0e-0123456789ab"
HOOK_PID = 5100                                  # the hook, no tty of its own; its parent is the session's host


def ps_text(*rows):
    return "  PID  PPID TTY      COMMAND\n" + "".join(f"{p} {pp} {tty} {cmd}\n" for p, pp, tty, cmd in rows)


DESK = [(20, 1, "??", f"{CLAUDE} daemon run --origin transient"),
        (21, 20, "??", f"{CLAUDE} bg-pty-host --bg-pty-host /tmp/x/pty.sock"),
        (22, 21, "ttys006", f"{CLAUDE} bg-spare --bg-spare /tmp/x/claim.sock"),
        (HOOK_PID, 22, "??", "python3 /x/termsign_hook.py")]
MOSH = [(30, 1, "??", "mosh-server new -c 256 -s -l LANG=en_US.UTF-8"),
        (31, 30, "ttys002", "-zsh"),
        (32, 31, "ttys002", f"{CLAUDE} agents")]
LOCAL = [(40, 1, "??", "/Applications/iTerm.app/Contents/MacOS/iTerm2"),
         (41, 40, "??", "login -fp phillipclapham"),
         (42, 41, "ttys000", "-zsh"),
         (43, 42, "ttys000", f"{CLAUDE} agents")]


def world(monkeypatch, rows, atimes, idle=0.0):
    """The seams: one ps text, tty access times (seconds before now; absent = stat fails), laptop idle seconds."""
    now = time.time()
    monkeypatch.setattr(ts, "_ps_rows", lambda: ps_text(*rows))
    monkeypatch.setattr(ts, "_own_pid", lambda: HOOK_PID)
    monkeypatch.setattr(ts, "_tty_atime", lambda tty: (now - atimes[tty]) if tty in atimes else None)
    if callable(idle):
        monkeypatch.setattr(ts, "_hid_idle_s", idle)
    else:
        monkeypatch.setattr(ts, "_hid_idle_s", lambda: idle)


def reason():
    return ts.remote_reason({"CLAUDE_CODE_SESSION_ID": SID})


# 1. the desk evidence: mosh agent view client, its tty moved with the session's -----------------------------------

def test_remote_agent_view_client_tied_to_the_session_is_remote(monkeypatch):
    world(monkeypatch, DESK + MOSH, {"ttys006": 2.000, "ttys002": 2.001}, idle=0.0)
    why = reason()
    assert why is not None and "agent view client ttys002 reached from mosh-server" in why and "matched" in why


def test_end_to_end_the_sheet_is_never_raised_and_the_answer_records_unsigned_remote(monkeypatch, store, env, keydir):
    seen(store, env, 2)
    world(monkeypatch, DESK + MOSH, {"ttys006": 2.000, "ttys002": 2.001}, idle=0.0)
    sheet_calls: list[str] = []
    outcomes: list[ts.SignOutcome] = []

    def sheet(path, timeout=None):
        sheet_calls.append("x")

        def signer(message):
            raise KeyError_("ssh-keygen could not sign (TimeoutExpired)")
        return "SHA256:" + "A" * 43, signer

    real_sign = ts.sign

    def spy(*a, **k):
        out = real_sign(*a, **k)
        outcomes.append(out)
        return out
    monkeypatch.setattr(ts, "sign", spy)
    environ = {**env, "CLAUDE_CODE_SESSION_ID": SID}
    echo = band_view._seat_answer(store, VIEWER, "1a", environ, None, session=SID, signer_factory=sheet, key_dir=keydir)
    assert sheet_calls == []
    assert recorded(store) == [(1, "fix")]
    assert [(r[5], r[6]) for r in sidecar(store)] == [("unsigned", "remote")]
    assert "the desk is remote, so no Touch ID prompt was raised" in echo
    assert "agent view client ttys002 reached from mosh-server" in outcomes[0].detail


# 2. both views open: the tie decides, not the laptop's idle time ----------------------------------------------------

def test_the_tie_names_the_local_view_so_a_stale_remote_view_is_not_remote(monkeypatch):
    world(monkeypatch, DESK + LOCAL + MOSH, {"ttys006": 1.0, "ttys000": 1.001, "ttys002": 2400.0}, idle=3 * 3600)
    assert reason() is None                         # the idle heuristic alone would have said remote


def test_swap_which_view_ties_and_it_is_remote(monkeypatch):
    world(monkeypatch, DESK + LOCAL + MOSH, {"ttys006": 1.0, "ttys000": 2400.0, "ttys002": 1.001}, idle=0.0)
    assert "agent view client ttys002 reached from mosh-server" in (reason() or "")


# 3. the fallback -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("idle,remote", [(120.0, True), (5.0, False), (60.0, True), (59.0, False)])
def test_no_tie_falls_back_to_remote_client_and_idle_laptop(monkeypatch, idle, remote):
    world(monkeypatch, DESK + MOSH, {"ttys006": 1.0, "ttys002": 900.0}, idle=idle)
    why = reason()
    assert (why is not None) is remote
    if remote:
        assert why.startswith("fallback: remote agent view client ttys002 reached from mosh-server")


def test_an_unreadable_laptop_activity_reads_remote_and_says_so(monkeypatch):
    # deliberate (L3 r1): with a remote client proven open and no tie, an unknown idle time cannot say the laptop is
    # in use, and the safe side is remote (a phone card) over local (an unseen sheet); the echo does not claim idle
    def boom():
        raise OSError("ioreg")
    world(monkeypatch, DESK + MOSH, {"ttys006": 1.0, "ttys002": 900.0}, idle=boom)
    assert "laptop activity unreadable" in (reason() or "")
    world(monkeypatch, DESK + MOSH, {"ttys006": 1.0, "ttys002": 900.0}, idle=lambda: None)
    why = reason() or ""
    assert "laptop activity unreadable" in why and "idle" not in why


def test_a_shared_stamp_that_is_old_is_not_this_prompt(monkeypatch):
    world(monkeypatch, DESK + LOCAL + MOSH, {"ttys006": 7200.0, "ttys000": 7200.0, "ttys002": 7200.0}, idle=0.0)
    assert reason() is None                         # no recent keystroke to tie; the laptop is busy: not remote


# 4. both tied, and every failure ---------------------------------------------------------------------------------------

def test_two_clients_tied_in_the_same_window_read_remote(monkeypatch):
    world(monkeypatch, DESK + LOCAL + MOSH, {"ttys006": 1.0, "ttys000": 1.1, "ttys002": 1.2}, idle=0.0)
    assert "agent view client ttys002" in (reason() or "")


def test_every_failure_reads_not_remote_and_never_raises(monkeypatch):
    world(monkeypatch, DESK + LOCAL + MOSH, {}, idle=0.0)              # every stat fails, laptop busy
    assert reason() is None
    monkeypatch.setattr(ts, "_ps_rows", lambda: None)
    assert reason() is None
    monkeypatch.setattr(ts, "_ps_rows", lambda: ps_text(*(DESK + MOSH)))
    monkeypatch.setattr(ts, "_own_pid", lambda: 999999)               # the hook is not in the table: no session tty
    monkeypatch.setattr(ts, "_hid_idle_s", lambda: 500.0)
    assert "fallback" in (reason() or "")                              # the fallback still stands
    monkeypatch.setattr(ts, "_own_pid", lambda: HOOK_PID)
    monkeypatch.setattr(ts, "_tty_atime", lambda tty: (_ for _ in ()).throw(RuntimeError("stat")))
    assert ts.remote_reason({"CLAUDE_CODE_SESSION_ID": SID}) is None   # a raising seam is swallowed


# 5. L3 r1: the kernel's lazy atime, and a Linux ps ----------------------------------------------------------------------

def test_a_keystroke_split_by_the_lazy_atime_flush_still_ties(monkeypatch):
    # measured on this Mac (L3_av_r1_triage.md, L7): one keystroke read on both ptys 1 ms apart, the session's pty
    # read alone 0.5 s before, so its update was deferred and flushed later: the two atimes came out 1.006 s apart.
    # The laptop is in use, so the fallback would have said local: the Touch ID sheet nobody sees.
    world(monkeypatch, DESK + LOCAL + MOSH, {"ttys006": 1.0, "ttys002": 2.006, "ttys000": 2400.0}, idle=0.0)
    assert "agent view client ttys002 reached from mosh-server" in (reason() or "")


def test_the_tie_window_is_bounded(monkeypatch):
    # a deferred flush lands under 2 s from the read; a client last read further off than TIE_S is not this keystroke
    world(monkeypatch, DESK + MOSH, {"ttys006": 1.0, "ttys002": 1.0 + ts.TIE_S + 0.5}, idle=0.0)
    assert reason() is None


LINUX_DESK = [(20, 1, "?", f"{CLAUDE} daemon run --origin transient"),
              (21, 20, "?", f"{CLAUDE} bg-pty-host --bg-pty-host /tmp/x/pty.sock"),
              (22, 21, "pts/6", f"{CLAUDE} bg-spare --bg-spare /tmp/x/claim.sock"),
              (HOOK_PID, 22, "?", "python3 /x/termsign_hook.py")]
LINUX_MOSH = [(30, 1, "?", "mosh-server new -c 256 -s -l LANG=en_US.UTF-8"),
              (31, 30, "pts/2", "-bash"),
              (32, 31, "pts/2", f"{CLAUDE} agents")]


def test_linux_ps_no_tty_marker_is_not_a_tty(monkeypatch):
    # procps prints "?" for no tty: the hook's own row must not become the session pty (that killed the tie)
    world(monkeypatch, LINUX_DESK + LINUX_MOSH, {"pts/6": 2.000, "pts/2": 2.001}, idle=0.0)
    assert "agent view client pts/2 reached from mosh-server" in (reason() or "")


def test_the_real_kernel_moves_a_pty_atime_on_read_within_the_tie_bound():
    # the tie's premise, against the kernel and a pty this test opens (never anyone's terminal)
    import os
    import pty
    import tty as tty_mod

    real = REAL_TTY_ATIME
    master, slave = pty.openpty()
    try:
        tty_mod.setraw(slave)
        name = os.path.relpath(os.ttyname(slave), "/dev")
        before = real(name)
        time.sleep(1.1)
        os.write(master, b"x")
        read_at = time.time()
        os.read(slave, 1)
        after = real(name)
        assert before is not None and after is not None and after > before
        assert abs(after - read_at) < ts.TIE_S
    finally:
        os.close(master)
        os.close(slave)


REAL_TTY_ATIME = ts._tty_atime                  # captured at import, before conftest's autouse stub replaces the seam
