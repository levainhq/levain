# ruff: noqa: F811
"""remotegov B1: the desk is remote when its ancestry holds sshd, sshd-session or mosh-server, or when it was reached
by ``claude attach`` from such a terminal; a remote desk never reaches the Touch ID sheet.

Grounded in project_memory/1105-31/RETURN_remotegov_m1.md: the chains below are the ones measured at a real
UserPromptSubmit hook (names real, ids and pids synthetic). No ssh, mosh or Touch ID sheet is involved: the ancestry
and the ``ps`` table are the two seams of ``terminal_sign`` (``_ancestor_commands`` over the one walker in
``levain.consent.band``, and ``_ps_rows``), and the signer is a stub that counts the call a sheet would be.
"""
from __future__ import annotations

import subprocess

import pytest

from levain.cockpit.keys import KeyError_
from levain.consent import band as consent_band
from levain.consent import terminal_sign as ts
from tests.test_termsign_l2_seat import (SESSION, VIEWER, ask, env, human_terminal, keydir, qdir, recorded,  # noqa: F401
                                         seat, seen, sidecar, store)

# the real functions, taken before any autouse fixture replaces them (tests/conftest.py puts an empty tree there)
REAL_ANCESTOR_COMMANDS = ts._ancestor_commands
REAL_PS_ROWS = ts._ps_rows

CLAUDE = "/Users/phillipclapham/.local/bin/claude"
SID = "1e37cf45-7708-4a11-9b0e-0123456789ab"

# --- ancestries as `ps -o args=` prints them (a daemon's rewritten title keeps a trailing colon) ---------------
SSH_RUN = [(199, [CLAUDE, "--model", "haiku"]), (198, ["sshd-session:", "phillipclapham@ttys004"]),
           (197, ["sshd-session:", "phillipclapham", "[priv]"]), (196, ["sshd:", "/usr/sbin/sshd", "-D"])]
MOSH_RUN = [(299, [CLAUDE, "--model", "haiku"]),
            (298, ["/opt/homebrew/bin/mosh-server", "new", "-c", "256", "-s", "-l", "LANG=en_US.UTF-8"])]
LOCAL_RUN = [(399, [CLAUDE]), (398, ["-zsh"]), (397, ["login", "-fp", "phillipclapham"]),
             (396, ["/Applications/iTerm.app/Contents/MacOS/iTerm2"])]
DESK = [(400, ["claude", "bg-spare", "--bg-spare", "/tmp/cc-daemon-501/x/spare/7a618357.claim.sock"]),
        (399, ["claude", "bg-pty-host", "--bg-pty-host", "/tmp/cc-daemon-501/x/spare/7a618357.pty.sock"]),
        (3237, [CLAUDE, "daemon", "run", "--origin", "transient"])]


def use_ancestry(monkeypatch, chain):
    """The real detector over a fixed ancestry: the one walker is replaced, the wrapper is the real function."""
    monkeypatch.setattr(consent_band, "_ancestors", lambda: iter(chain))
    monkeypatch.setattr(ts, "_ancestor_commands", REAL_ANCESTOR_COMMANDS)


def ps_text(*rows):
    """``ps -Ao pid,ppid,tty,command`` text from ``(pid, ppid, tty, command)`` rows."""
    return "  PID  PPID TTY      COMMAND\n" + "".join(f"{p} {pp} {tty} {cmd}\n" for p, pp, tty, cmd in rows)


SSH_ATTACH_ROWS = ps_text(
    (10, 1, "??", "sshd: /usr/sbin/sshd -D"),
    (11, 10, "??", "sshd-session: phillipclapham [priv]"),
    (12, 11, "??", "sshd-session: phillipclapham@ttys006"),
    (13, 12, "ttys006", f"{CLAUDE} attach 1e37cf45"),
    (20, 1, "??", f"{CLAUDE} daemon run --origin transient"))
MOSH_ATTACH_ROWS = ps_text(
    (30, 1, "??", "mosh-server new -c 256 -s -l LANG=en_US.UTF-8"),
    (31, 30, "ttys007", "-zsh"),
    (32, 31, "ttys007", f"{CLAUDE} attach 1e37cf45"))
LOCAL_ATTACH_ROWS = ps_text(
    (40, 1, "??", "/Applications/iTerm.app/Contents/MacOS/iTerm2"),
    (41, 40, "??", "login -fp phillipclapham"),
    (42, 41, "ttys002", "-zsh"),
    (43, 42, "ttys002", f"{CLAUDE} attach 1e37cf45"))


def use_ps(monkeypatch, text):
    monkeypatch.setattr(ts, "_ps_rows", lambda: text)


# --- (i) the ancestry leg -----------------------------------------------------------------------------------------

def test_ancestry_with_sshd_session_is_remote_with_env_scrubbed(monkeypatch):
    use_ancestry(monkeypatch, SSH_RUN)
    reason = ts.remote_reason({})
    assert reason is not None and "ancestor process sshd-session" in reason
    assert ts.is_remote({}) is True


def test_ancestry_with_mosh_server_is_remote_with_env_scrubbed(monkeypatch):
    use_ancestry(monkeypatch, MOSH_RUN)
    reason = ts.remote_reason({})
    assert reason is not None and "ancestor process mosh-server" in reason


def test_bare_sshd_title_form_is_remote(monkeypatch):
    use_ancestry(monkeypatch, [(5, ["sshd:", "phillipclapham@notty"])])
    assert "ancestor process sshd" in (ts.remote_reason({}) or "")


def test_local_terminal_ancestry_is_not_remote(monkeypatch):
    use_ancestry(monkeypatch, LOCAL_RUN)
    assert ts.remote_reason({}) is None and ts.is_remote({}) is False


@pytest.mark.parametrize("argv0", ["ssh", "sshd-agent", "not-sshd", "mosh", "mosh-client", "mosh-server-wrapper",
                                   "/usr/bin/ssh-agent"])
def test_only_the_exact_names_count_as_a_remote_ancestor(monkeypatch, argv0):
    use_ancestry(monkeypatch, [(5, [argv0, "x"])])
    assert ts.remote_reason({}) is None


@pytest.mark.parametrize("name", ["SSH_AUTH_SOCK", "P9K_SSH", "_P9K_SSH_TTY", "SSH_ASKPASS", "SSH_CONNECTION_X"])
def test_names_that_only_look_like_ssh_are_not_signals(monkeypatch, name):
    use_ancestry(monkeypatch, LOCAL_RUN)
    assert ts.remote_reason({name: "1"}) is None


@pytest.mark.parametrize("name", ["SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"])
def test_the_three_exact_env_names_are_signals(monkeypatch, name):
    use_ancestry(monkeypatch, LOCAL_RUN)
    assert ts.remote_reason({name: "x"}) == f"env {name}"


def test_presence_remote_is_a_signal_and_other_presence_is_not(monkeypatch):
    use_ancestry(monkeypatch, LOCAL_RUN)
    assert ts.remote_reason({}, presence="remote desk") == "presence says remote"
    assert ts.remote_reason({}, presence="at the desk") is None


# --- (ii) the attach leg ------------------------------------------------------------------------------------------

def attached(monkeypatch, rows, *, chain=DESK, sid=SID):
    use_ancestry(monkeypatch, chain)
    use_ps(monkeypatch, rows)
    return ts.remote_reason({"CLAUDE_CODE_SESSION_ID": sid})


def test_attach_client_under_sshd_makes_the_session_remote(monkeypatch):
    reason = attached(monkeypatch, SSH_ATTACH_ROWS)
    assert reason is not None and "attach client 1e37cf45 reached from sshd-session" in reason


def test_attach_client_under_mosh_server_makes_the_session_remote(monkeypatch):
    assert "attach client 1e37cf45 reached from mosh-server" in (attached(monkeypatch, MOSH_ATTACH_ROWS) or "")


def test_attach_client_for_another_session_is_not_remote(monkeypatch):
    assert attached(monkeypatch, SSH_ATTACH_ROWS, sid="9a9a9a9a-0000-4000-8000-000000000000") is None


def test_local_attach_client_is_not_remote(monkeypatch):
    assert attached(monkeypatch, LOCAL_ATTACH_ROWS) is None


@pytest.mark.parametrize("argv", ["claude attach", "claude attach my-desk-name", "claude attach 1e37cf4",
                                  "claude attach --help", "claude agents 1e37cf45"])
def test_a_client_without_a_matching_id_in_argv_is_not_seen(monkeypatch, argv):
    rows = ps_text((10, 1, "??", "sshd: /usr/sbin/sshd -D"), (12, 10, "??", "sshd-session: phillipclapham@ttys006"),
                   (13, 12, "ttys006", f"/Users/phillipclapham/.local/bin/{argv}"))
    assert attached(monkeypatch, rows) is None


def test_two_clients_one_local_one_remote_reads_remote(monkeypatch):
    rows = LOCAL_ATTACH_ROWS + SSH_ATTACH_ROWS.split("\n", 1)[1]
    assert "attach client 1e37cf45" in (attached(monkeypatch, rows) or "")


def test_the_attach_leg_needs_the_session_id_in_the_env(monkeypatch):
    use_ancestry(monkeypatch, DESK)
    use_ps(monkeypatch, SSH_ATTACH_ROWS)
    assert ts.remote_reason({}) is None and ts.remote_reason({"CLAUDE_CODE_SESSION_ID": ""}) is None
    assert ts.remote_reason({"CLAUDE_CODE_SESSION_ID": "1e37cf4"}) is None       # shorter than the id minimum


def test_the_client_ancestry_is_capped_at_the_walkers_hop_cap(monkeypatch):
    # the sshd sits past the cap: not seen (the same cap the own-ancestry walker has)
    hops = consent_band._HOPS
    rows = [(1000 + i, 1000 + i + 1, "??", "-zsh") for i in range(hops + 2)]
    rows.append((1000 + hops + 2, 1, "??", "sshd-session: x@ttys001"))
    rows.append((9, 1000, "ttys001", f"{CLAUDE} attach 1e37cf45"))
    assert attached(monkeypatch, ps_text(*rows)) is None


# --- fail-safe: a leg that cannot read is local, and nothing raises -------------------------------------------------

@pytest.mark.parametrize("exc", [OSError("no ps"), subprocess.TimeoutExpired("ps", 2), ValueError("x")])
def test_ps_that_fails_or_times_out_is_not_remote(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(ts, "_ps_rows", REAL_PS_ROWS)
    monkeypatch.setattr(subprocess, "run", boom)
    assert REAL_PS_ROWS() is None
    use_ancestry(monkeypatch, DESK)
    assert ts.remote_reason({"CLAUDE_CODE_SESSION_ID": SID}) is None


def test_ps_garbage_is_not_remote(monkeypatch):
    for text in ("", "not a table at all\n\x00\x01", "1 2\n", "x y z w\n", "13 12 ttys006 claude attach '1e37cf45\n"):
        assert attached(monkeypatch, text) is None


def test_a_walker_that_raises_is_not_remote_and_does_not_raise(monkeypatch):
    def boom():
        raise RuntimeError("ps gone")
    monkeypatch.setattr(consent_band, "_ancestors", boom)
    monkeypatch.setattr(ts, "_ancestor_commands", REAL_ANCESTOR_COMMANDS)
    assert ts.remote_reason({}) is None
    monkeypatch.setattr(ts, "_ancestor_commands", boom)
    assert ts.remote_reason({}) is None
    monkeypatch.setattr(ts, "_ps_rows", boom)
    assert ts.remote_reason({"CLAUDE_CODE_SESSION_ID": SID}) is None


def test_real_ps_rows_reads_a_table_in_this_environment():
    # the real runner, no seam: a table with this very process in it (ps is always present where the suite runs)
    import os

    text = REAL_PS_ROWS()
    assert text is not None and str(os.getpid()) in {line.split()[0] for line in text.splitlines()[1:] if line.split()}


# --- (iii) is_remote() with no arguments sees the environment and the ancestry -------------------------------------

def test_is_remote_without_arguments_reads_os_environ(monkeypatch):
    use_ancestry(monkeypatch, LOCAL_RUN)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    assert ts.is_remote() is False
    monkeypatch.setenv("SSH_CLIENT", "10.0.0.2 5555 22")
    assert ts.is_remote() is True


def test_is_remote_without_arguments_sees_the_ancestry(monkeypatch):
    for name in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"):
        monkeypatch.delenv(name, raising=False)
    use_ancestry(monkeypatch, SSH_RUN)
    assert ts.is_remote() is True
    use_ancestry(monkeypatch, MOSH_RUN)
    assert ts.is_remote() is True
    use_ancestry(monkeypatch, LOCAL_RUN)
    assert ts.is_remote() is False


def test_is_remote_without_arguments_sees_an_attach_client_of_the_environs_session(monkeypatch):
    for name in ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    use_ancestry(monkeypatch, DESK)
    use_ps(monkeypatch, SSH_ATTACH_ROWS)
    assert ts.is_remote() is True
    use_ps(monkeypatch, LOCAL_ATTACH_ROWS)
    assert ts.is_remote() is False


def test_is_remote_keeps_its_signature_and_old_meaning(monkeypatch):
    use_ancestry(monkeypatch, LOCAL_RUN)
    assert ts.is_remote({}) is False
    assert ts.is_remote({"SSH_CONNECTION": "1 2 3 4"}) and ts.is_remote({"SSH_TTY": "/dev/ttys001"})
    assert ts.is_remote({}, presence="remote desk") and not ts.is_remote({}, presence="at the desk")


def test_sign_names_the_leg_that_fired_in_its_detail():
    out = ts.sign(b"x", remote=True, remote_why="ancestor process sshd-session")
    assert (out.state, out.reason, out.detail) == ("unsigned", "remote", "the desk is remote (ancestor process sshd-session)")
    assert ts.sign(b"x", remote=True).detail == "the desk is remote"     # flow's termsign_hook passes no reason


# --- (iv) the end to end: the real _seat_answer for a desk attached from ssh, SSH_* absent from its env --------------

WHERE_TO_SIGN = "sign it on your phone: open the card in the cockpit"


def run_seat(monkeypatch, store, env, keydir, rows):
    seen(store, env, 2)
    environ = {**env, "CLAUDE_CODE_SESSION_ID": SID, "SSH_AUTH_SOCK": "/x", "P9K_SSH": "1"}   # measured: no SSH_CONNECTION
    use_ancestry(monkeypatch, DESK)
    use_ps(monkeypatch, rows)
    sheet_calls: list[str] = []
    outcomes: list[ts.SignOutcome] = []

    def sheet(path, timeout=None):
        sheet_calls.append("the sheet would be raised")

        def signer(message):
            raise KeyError_("ssh-keygen could not sign (TimeoutExpired)")      # an unattended sheet runs out of time
        return "SHA256:" + "A" * 43, signer

    real_sign = ts.sign

    def spy(*a, **k):
        out = real_sign(*a, **k)
        outcomes.append(out)
        return out
    monkeypatch.setattr(ts, "sign", spy)
    echo = seat(store, env, "1a", keydir, sheet, environ=environ)
    return echo, sheet_calls, outcomes


def test_attached_from_ssh_desk_never_reaches_the_sheet_and_says_where_to_sign(monkeypatch, store, env, keydir):
    echo, sheet_calls, outcomes = run_seat(monkeypatch, store, env, keydir, SSH_ATTACH_ROWS)
    assert sheet_calls == []                                                   # no Touch ID sheet on the laptop
    assert ts.post_notice.posted == []                                         # and no notice that announces one
    assert recorded(store) == [(1, "fix")]                                     # recording is unchanged
    assert [(r[5], r[6]) for r in sidecar(store)] == [("unsigned", "remote")]
    assert "recorded UNSIGNED (the desk is remote" in echo and WHERE_TO_SIGN in echo
    assert [(o.state, o.reason) for o in outcomes] == [("unsigned", "remote")]
    assert "attach client 1e37cf45 reached from sshd-session" in outcomes[0].detail


def test_the_same_desk_with_a_local_attach_client_calls_the_signer_once(monkeypatch, store, env, keydir):
    echo, sheet_calls, outcomes = run_seat(monkeypatch, store, env, keydir, LOCAL_ATTACH_ROWS)
    assert sheet_calls == ["the sheet would be raised"]
    assert len(ts.post_notice.posted) == 1
    assert recorded(store) == [(1, "fix")]
    assert [(r[5], r[6]) for r in sidecar(store)] == [("unsigned", "timeout")]
    assert "no touch in time" in echo and WHERE_TO_SIGN not in echo
    assert [o.reason for o in outcomes] == ["timeout"]


def test_ssh_run_with_the_env_scrubbed_never_reaches_the_sheet(monkeypatch, store, env, keydir):
    seen(store, env, 2)
    use_ancestry(monkeypatch, SSH_RUN)
    calls: list[str] = []

    def sheet(path, timeout=None):
        calls.append("x")
        raise AssertionError("the sheet was reached")
    echo = seat(store, env, "1a", keydir, sheet, environ={**env})
    assert calls == [] and recorded(store) == [(1, "fix")] and WHERE_TO_SIGN in echo
    assert [(r[5], r[6]) for r in sidecar(store)] == [("unsigned", "remote")]
