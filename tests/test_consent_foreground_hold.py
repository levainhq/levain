"""11-07 (Phill, band 407, board row 135 c-prime): with a phone paired, a PermissionRequest from a session that is NOT the
foreground one is held for a signed answer and pinged (a card with buttons on the phone and the cockpit); the foreground
session keeps today's presence rule and its native dialog. *"It is better to err towards pinging than not"*: a signal that
is unreadable, stale or ambiguous is NOT foreground, so it holds. Disposable queue and fakes only: no real roster, no real
lsof, no real session."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code as cc
from levain.consent.queue import ConsentQueue
from tests._decide_scenarios import _event
from tests.chat_egress.keys_rig import Cockpit

SESSION = "11111111-2222-3333-4444-555555555555"
SOCK = "/tmp/cc-daemon-test/spare/aa.pty.sock"
WORKER, SUPERVISOR, CLIENT = 4001, 4002, 4003


REPL = 4005
_SUPERVISOR = object()


def _roster(supervisor=_SUPERVISOR, **over):
    row = {"pid": WORKER, "sessionId": SESSION, "ptySock": SOCK}
    row.update(over)
    out = {"workers": {"aa": row}}
    if supervisor is not None:
        out["supervisorPid"] = SUPERVISOR if supervisor is _SUPERVISOR else supervisor
    return out


A_LISTEN, A_ACCEPTED = "0x7b2abcc7968afd2a", "0xbfac1701e9e24764"


def _lsof(*, client: bool, extra=()):
    """The macOS listing shape measured 11-07 on this machine (claude 2.1.319, ``/usr/sbin/lsof -U``; only the pids and
    the user replaced): the worker holds the listener and the accepted socket (both named by the path, the socket's
    kernel address in DEVICE, NODE empty), the supervisor holds a ``->`` row to the accepted one, a 15-digit address is
    printed without its leading zero; an attached client adds a ``->`` row."""
    rows = [f"2.1.319   {WORKER} me   10u  unix {A_LISTEN}      0t0      {SOCK}",
            f"2.1.319   {WORKER} me   11u  unix {A_ACCEPTED}      0t0      {SOCK}",
            f"2.1.319   {SUPERVISOR} me   21u  unix 0x225a7cf7d653a8d5      0t0      ->{A_ACCEPTED}",
            f"2.1.319   {REPL} me   13u  unix  0x231a2161311233b      0t0      /tmp/cc-daemon-test/rv/aa.sock",
            "iTermServ  2070 me    0u  unix 0xdb96c961ac3647ae      0t0      /Users/me/Library/Application Support/x.socket",
            "loginwind   413 me    4u  unix 0x173049b5122f1e23      0t0      ->0x50dbe14e9b0422ed",
            *extra]
    if client:
        rows.append(f"2.1.319   {CLIENT} me   9u  unix 0xc32fd6880c2ee4b2      0t0      ->{A_ACCEPTED}")
    return "COMMAND     PID           USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n" + "\n".join(rows) + "\n"


def _lsof_linux(*, addr_w="0xffff8d2b3c1e5000", addr_a="0xffff8d2b3c1e5400"):
    """The Linux shape (lsof 4.9x: the inode in NODE, ``type=`` and a state after NAME, no ``->`` peer by address).
    Written from lsof's Linux output format, not measured on a Linux host."""
    return ("COMMAND     PID USER   FD   TYPE             DEVICE SIZE/OFF    NODE NAME\n"
            "systemd       1 root   14u  unix 0xffff8d2b3c1e4400      0t0   20843 /run/systemd/notify type=DGRAM\n"
            f"2.1.319    {WORKER} me   10u  unix {addr_w}      0t0   41001 {SOCK} type=STREAM (LISTEN)\n"
            f"2.1.319    {WORKER} me   11u  unix {addr_a}      0t0   41002 {SOCK} type=STREAM (CONNECTED)\n"
            f"2.1.319    {SUPERVISOR} me   21u  unix 0xffff8d2b3c1e5800      0t0   41003 type=STREAM (CONNECTED)\n")


def _boom(*a, **k):
    raise OSError("boom")


def _fg(**kw):
    base = dict(home=Path("/nonexistent-home"), queue_dir=None, roster_reader=lambda h: _roster(),
                lsof_runner=lambda: _lsof(client=False))
    base.update(kw)
    return cc.foreground_session(SESSION, **base)


# --- the pure function ---------------------------------------------------------------------------------------------
def test_a_client_on_the_ptysock_is_foreground():
    fg, why = _fg(lsof_runner=lambda: _lsof(client=True))
    assert fg and str(CLIENT) in why


def test_the_supervisors_own_row_is_not_a_client():
    fg, _ = _fg()
    assert fg is False


def test_the_lease_holder_is_foreground_without_reading_the_roster():
    fg, why = _fg(queue_dir="/q", lease_reader=lambda q, s: s == SESSION, roster_reader=_boom, lsof_runner=_boom)
    assert fg and "lease" in why


@pytest.mark.parametrize("kw", [
    dict(roster_reader=_boom),                                          # unreadable roster
    dict(roster_reader=lambda h: "not a roster"),                       # malformed
    dict(roster_reader=lambda h: {"workers": {}}),                      # no row for the session
    dict(roster_reader=lambda h: _roster(ptySock="relative.sock")),     # no usable ptySock
    dict(roster_reader=lambda h: _roster(sessionId="someone-else")),
    dict(lsof_runner=_boom),                                            # lsof missing / timed out / failed
    dict(lsof_runner=lambda: "garbage with no rows\n"),                 # stale roster: the socket has no rows
])
def test_every_unreadable_or_ambiguous_signal_is_not_foreground(kw):
    fg, why = _fg(**kw)
    assert fg is False and why


def test_a_lease_read_error_falls_through_to_the_socket_signal():
    fg, _ = _fg(queue_dir="/q", lease_reader=_boom, lsof_runner=lambda: _lsof(client=True))
    assert fg is True
    fg, why = _fg(queue_dir="/q", lease_reader=_boom)
    assert fg is False and "lease unreadable" in why


def test_an_empty_session_is_not_foreground_and_a_subagent_id_reads_as_its_session():
    assert cc.foreground_session("", home=Path("/x"), queue_dir=None)[0] is False
    assert cc.foreground_session(SESSION + "#agent:x", home=Path("/x"), queue_dir=None,
                                 roster_reader=lambda h: _roster(),
                                 lsof_runner=lambda: _lsof(client=True))[0] is True


def test_the_default_lsof_runner_runs_the_absolute_binary_bounded():
    try:
        cc._lsof_path()
    except FileNotFoundError:
        pytest.skip("no lsof")
    assert "COMMAND" in cc._run_lsof()


def test_every_unix_row_of_this_hosts_real_lsof_parses():
    """The regex against the real binary's output, not only the fixtures (L3 c-prime deepseek HIGH)."""
    try:
        cc._lsof_path()
    except FileNotFoundError:
        pytest.skip("no lsof")
    rows = [ln for ln in cc._run_lsof().splitlines()[1:] if " unix " in ln]
    if not rows:
        pytest.skip("no unix sockets listed")
    missed = [ln for ln in rows if not cc._LSOF_ROW.match(ln)]
    assert not missed, missed[:3]
    for ln in rows:
        name = cc._LSOF_ROW.match(ln)["name"]
        assert name == "" or name.startswith(("/", "->")) or "type=" in name, ln


# --- L3 c-prime: every identifier the socket signal leans on must be there, or it is unknown -------------------------
@pytest.mark.parametrize("roster", [
    _roster(supervisor=None),                   # no supervisorPid: its permanent row would read as a client
    _roster(supervisor="4002"),                 # a string
    _roster(supervisor=True),                   # a bool
    _roster(supervisor=0),                      # not positive
    _roster(pid="4001"),                        # the worker pid not an int
    _roster(pid=None),
    _roster(replPid="4005"),                    # a replPid that is there but malformed
    _roster(replPid=True),                      # a bool is malformed too
])
def test_a_roster_without_usable_pids_is_unknown_even_with_only_the_supervisors_row(roster):
    fg, why = _fg(roster_reader=lambda h: roster)
    assert fg is False and "unknown" in why


def test_a_null_replpid_is_no_repl_not_unknown():
    """L3 c-prime v1 deepseek MED 2: a roster that serialises ``"replPid": null`` for a worker with no REPL reads as no
    REPL (0), so the socket signal is still read; only a non-null malformed value stays unknown."""
    fg, why = _fg(roster_reader=lambda h: _roster(replPid=None), lsof_runner=lambda: _lsof(client=True))
    assert fg is True and str(CLIENT) in why
    fg, why = _fg(roster_reader=lambda h: _roster(replPid=None))
    assert fg is False and why == "no client on the ptySock"


@pytest.mark.parametrize("name", ["", "->", "->0xc32fd6880c2ee4b2"])
def test_the_workers_own_row_at_its_address_under_another_name_is_not_ambiguous(name):
    """L3 c-prime v1 deepseek LOW 4: a worker fd at one of the ptySock's addresses that lsof names other than the path
    (empty, a bare ``->`` or a peer) is still the worker's own; only a foreign pid there makes it unknown."""
    row = f"2.1.319   {WORKER} me   12u  unix {A_ACCEPTED}      0t0      {name}"
    assert cc._LSOF_ROW.match(row)["name"] == name, "the fixture row parses with that name"
    fg, why = _fg(lsof_runner=lambda: _lsof(client=True, extra=[row]))
    assert fg is True and str(CLIENT) in why
    fg, why = _fg(lsof_runner=lambda: _lsof(client=False, extra=[row]))
    assert fg is False and why == "no client on the ptySock"


def test_the_repl_pids_own_row_is_not_a_client():
    fg, _ = _fg(roster_reader=lambda h: _roster(replPid=REPL),
                lsof_runner=lambda: _lsof(client=False, extra=[f"2.1.319 {REPL} me 7u unix 0xabc 0t0 ->{A_ACCEPTED}"]))
    assert fg is False


def test_a_non_claude_process_connected_to_the_socket_is_not_a_client():
    fg, _ = _fg(lsof_runner=lambda: _lsof(client=False, extra=[f"lsof-mon 5000 me 3u unix 0xabc 0t0 ->{A_ACCEPTED}"]))
    assert fg is False


@pytest.mark.parametrize("extra", [
    f"2.1.319 5001 me 4u unix 0xabd 0t0 {SOCK}",                     # another process holds the ptySock path
    f"2.1.319 5002 me 4u unix {A_ACCEPTED} 0t0 /tmp/other.sock",       # the address is not unique to the worker
])
def test_an_ambiguous_ptysock_is_unknown_even_with_a_client(extra):
    fg, why = _fg(lsof_runner=lambda: _lsof(client=True, extra=[extra]))
    assert fg is False and "unknown" in why


def test_an_uppercase_peer_address_still_names_the_client():
    fg, _ = _fg(lsof_runner=lambda: _lsof(client=False, extra=[
        f"2.1.319 {CLIENT} me 9u unix 0xABC 0t0 ->{A_ACCEPTED.upper().replace('0X', '0x')}"]))
    assert fg is True


def test_linux_rows_parse_and_answer_no_client():
    fg, why = _fg(lsof_runner=lambda: _lsof_linux())
    assert fg is False and why == "no client on the ptySock"   # the worker's rows were found: parsed, not stale


def test_linux_hidden_kernel_addresses_are_unknown():
    zero = "0x0000000000000000"
    fg, why = _fg(lsof_runner=lambda: _lsof_linux(addr_w=zero, addr_a=zero) +
                  f"2.1.319 {CLIENT} me 9u unix {zero} 0t0 41009 ->{zero} type=STREAM\n")
    assert fg is False and "hidden" in why


def _ticks(*values):
    it = iter(values)
    last = [values[-1]]

    def clock():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return clock


def test_an_answer_past_the_budget_is_unknown_even_with_a_client():
    fg, why = _fg(lsof_runner=lambda: _lsof(client=True), clock=_ticks(0.0, 0.2, 1.5), budget=1.0)
    assert fg is False and "budget" in why


def test_no_budget_left_runs_no_lsof():
    fg, why = _fg(lsof_runner=_boom, clock=_ticks(0.0, 1.2), budget=1.0)
    assert fg is False and "no budget left" in why


def test_the_default_runner_gets_what_is_left_of_the_budget(monkeypatch):
    got = []
    monkeypatch.setattr(cc, "_run_lsof", lambda timeout: got.append(timeout) or _lsof(client=True))
    fg, _ = cc.foreground_session(SESSION, home=Path("/x"), queue_dir=None, roster_reader=lambda h: _roster(),
                                  clock=_ticks(0.0, 0.3, 0.4), budget=1.0)
    assert fg is True and got == [pytest.approx(0.7)]


def test_a_check_that_blocks_answers_unknown_at_the_budget_not_after_it():
    """L3 c-prime v1 codex MED: the budget is enforced, not only read after the check returns: a roster read that
    blocks (an unresponsive filesystem) answers unknown when the budget runs out."""
    release = threading.Event()
    try:
        t0 = time.monotonic()
        fg, why = _fg(roster_reader=lambda h: release.wait(10) and _roster(),
                      lsof_runner=lambda: _lsof(client=True), budget=0.2)
        took = time.monotonic() - t0
    finally:
        release.set()
    assert fg is False and "budget" in why
    assert took < 2.0, took


def test_no_budget_at_all_runs_nothing():
    fg, why = _fg(roster_reader=_boom, lsof_runner=_boom, budget=0.0)
    assert fg is False and "budget" in why


def test_lsof_resolves_from_the_fixed_list_never_path(tmp_path, monkeypatch):
    exe = tmp_path / "lsof"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(cc, "LSOF_CANDIDATES", (str(tmp_path / "absent"), str(exe)))
    assert cc._lsof_path() == str(exe)
    monkeypatch.setattr(cc, "LSOF_CANDIDATES", (str(tmp_path / "absent"),))
    monkeypatch.setenv("PATH", str(tmp_path))           # an lsof on PATH is never used
    with pytest.raises(FileNotFoundError):
        cc._lsof_path()
    fg, why = cc.foreground_session(SESSION, home=Path("/x"), queue_dir=None, roster_reader=lambda h: _roster())
    assert fg is False and "FileNotFoundError" in why


def test_the_candidate_list_covers_macos_and_linux():
    assert cc.LSOF_CANDIDATES == ("/usr/sbin/lsof", "/usr/bin/lsof")


# --- the hook ------------------------------------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = home / "proj"
    ws.mkdir(parents=True)
    ck = Cockpit(tmp_path)          # one enrolled key: a signed answer can verify
    q = ConsentQueue(tmp_path / "levain-home" / "consent", authority=ck.source(), home=os.path.realpath(home))
    monkeypatch.setattr("levain.webpush.live", lambda queue_dir: True)   # a paired phone
    return {"ws": os.path.realpath(ws), "home": Path(os.path.realpath(home)), "ck": ck, "q": q}


def _run(rig, monkeypatch, *, foreground=None, settings=None, away=0.0, entrypoint="cli",
         max_hold=cc.CLEANUP_RESERVE + 30):
    seen: dict = {"pings": []}

    def fake_hold(queue, request, wait, **kw):
        seen["wait"], seen["still_away"] = wait, kw.get("still_away")
        return None, None

    def fake_ping(settings, queue_dir, agent, what, request_id, wait, *, request=None):
        seen["pings"].append(request_id)
        return True

    monkeypatch.setattr(cc, "hold_for_answer", fake_hold)
    monkeypatch.setattr(cc, "hold_ping", fake_ping)
    monkeypatch.setattr(cc, "away_hold", lambda *a, **k: away)   # the presence record is stood in for
    settings = settings or cc.AdapterSettings(cockpit_data_root=str(rig["ck"].root), away_hold_seconds=20)
    env = {"HOME": str(rig["home"]), "PATH": "/usr/bin:/bin", "CLAUDE_CODE_ENTRYPOINT": entrypoint}
    ev = _event("Bash", {"command": "echo hi"}, cwd=rig["ws"])
    kw = {} if foreground is None else {"foreground": foreground}
    out = cc.decide_ex(ev, rig["q"], settings, environ=env, home=rig["home"], launch_nonce="", contained_launch=None,
                       idle=lambda: 0.0, max_hold=max_hold,
                       still_away=lambda: (False, "intent", "present"), **kw)
    return out, seen


def test_a_non_foreground_request_is_held_pings_with_a_sign_sheet_and_withdraws_when_the_hold_ends(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "no client on the ptySock"))
    assert isinstance(out, cc.Abstained) and out.reason == "hold_expired" and out.held is True
    assert seen["wait"] > 0 and seen["still_away"] is None, seen
    assert seen["pings"] == [out.request.id]          # the request's own ping: the center draws its sign sheet
    assert rig["q"].withdraw(out.request.id) is False   # already withdrawn: the native dialog stands, nothing ghosts


def test_the_foreground_session_keeps_todays_path_no_hold_no_ping(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (True, "the desk lease holder"))
    assert "wait" not in seen and seen["pings"] == []


def test_the_lease_holder_end_to_end_keeps_todays_path(rig, monkeypatch):
    monkeypatch.setattr(cc, "_lease_holds", lambda q, s: True)
    monkeypatch.setattr(cc, "_read_roster", _boom)
    monkeypatch.setattr(cc, "_run_lsof", _boom)
    out, seen = _run(rig, monkeypatch)
    assert "wait" not in seen


@pytest.mark.parametrize("which", ["roster", "lsof", "lease_and_roster"])
def test_an_unreadable_signal_end_to_end_holds_and_pings(rig, monkeypatch, which):
    monkeypatch.setattr(cc, "_lease_holds", _boom if which == "lease_and_roster" else (lambda q, s: False))
    # the "lsof" case reads a good roster row, so the lsof failure itself is what holds (L3 c-prime complement LOW)
    monkeypatch.setattr(cc, "_read_roster", _boom if which != "lsof" else (lambda h: _roster()))
    monkeypatch.setattr(cc, "_run_lsof", _boom)
    out, seen = _run(rig, monkeypatch)
    assert isinstance(out, cc.Abstained) and out.held is True and seen["wait"] > 0 and seen["pings"]


def test_a_foreground_check_that_raises_still_holds(rig, monkeypatch):
    def worse(session):
        raise RuntimeError("a check that fails")
    out, seen = _run(rig, monkeypatch, foreground=worse)   # never a silent skip
    assert isinstance(out, cc.Abstained) and out.held is True and seen["pings"]


def test_a_slow_foreground_check_near_the_deadline_still_leaves_a_hold_and_pings(rig, monkeypatch):
    """L3 c-prime v1 deepseek LOW 5 + codex MED: with about a second left before the deadline, a foreground check that
    blocks must not eat the window: its time comes out of the hold before the wait is computed, bounded so a wait is
    left, and the request is still pinged."""
    release = threading.Event()
    monkeypatch.setattr(cc, "_lease_holds", lambda q, s: False)
    monkeypatch.setattr(cc, "_read_roster", lambda h: release.wait(10) and _roster())
    monkeypatch.setattr(cc, "_run_lsof", _boom)
    try:
        out, seen = _run(rig, monkeypatch, max_hold=cc.CLEANUP_RESERVE + 1.0)
    finally:
        release.set()
    assert isinstance(out, cc.Abstained) and out.held is True, out
    assert seen["wait"] > 0 and seen["pings"] == [out.request.id], seen


def test_a_diagnostic_print_that_fails_never_escapes(rig, monkeypatch):
    """L3 c-prime v1 deepseek MED 1 / gemini LOW 2: a closed or broken stderr must not turn the hold into an
    exception out of decide_ex."""
    class Broken:
        def write(self, *_):
            raise BrokenPipeError("stderr is gone")

        def flush(self):
            raise BrokenPipeError("stderr is gone")

    monkeypatch.setattr(cc.sys, "stderr", Broken())
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "no client on the ptySock"))
    assert isinstance(out, cc.Abstained) and out.held is True and seen["pings"]


def test_no_paired_phone_no_hold(rig, monkeypatch):
    monkeypatch.setattr("levain.webpush.live", lambda queue_dir: False)
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"))
    assert "wait" not in seen


def test_his_explicit_off_word_stops_it(rig, monkeypatch):
    s = cc.AdapterSettings(cockpit_data_root=str(rig["ck"].root), away_hold_seconds=20, explicit_off=True)
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"), settings=s)
    assert "wait" not in seen


def test_an_away_hold_for_a_non_foreground_session_loses_its_presence_recheck_and_keeps_its_length(rig, monkeypatch):
    """L3 c-prime codex MED: away when the request came, then he returns to the foreground window: the background
    session's hold must not be cut by that return."""
    called = []
    out, seen = _run(rig, monkeypatch, foreground=lambda s: called.append(s) or (False, "x"), away=7.0)
    assert called, "the foreground check runs even with an away hold already above zero"
    assert seen["wait"] == pytest.approx(7.0, abs=0.5) and seen["still_away"] is None


def test_an_away_hold_for_the_foreground_session_keeps_its_presence_recheck(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (True, "the desk lease holder"), away=20.0)
    assert seen["wait"] > 0 and seen["still_away"] is not None


def test_no_key_that_could_sign_no_hold(rig, monkeypatch):
    s = cc.AdapterSettings(cockpit_data_root=str(rig["ck"].root) + "-absent", away_hold_seconds=20)
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"), settings=s)
    assert "wait" not in seen
