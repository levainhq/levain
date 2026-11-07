"""11-07 (Phill, band 407, board row 135 c-prime): with a phone paired, a PermissionRequest from a session that is NOT the
foreground one is held for a signed answer and pinged (a card with buttons on the phone and the cockpit); the foreground
session keeps today's presence rule and its native dialog. *"It is better to err towards pinging than not"*: a signal that
is unreadable, stale or ambiguous is NOT foreground, so it holds. Disposable queue and fakes only: no real roster, no real
lsof, no real session."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code as cc
from levain.consent.queue import ConsentQueue
from tests._decide_scenarios import _event
from tests.chat_egress.keys_rig import Cockpit

SESSION = "11111111-2222-3333-4444-555555555555"
SOCK = "/tmp/cc-daemon-test/spare/aa.pty.sock"
WORKER, SUPERVISOR, CLIENT = 4001, 4002, 4003


def _roster(**over):
    row = {"pid": WORKER, "sessionId": SESSION, "ptySock": SOCK}
    row.update(over)
    return {"supervisorPid": SUPERVISOR, "workers": {"aa": row}}


def _lsof(*, client: bool):
    """The listing shape measured 11-07 on claude 2.1.319: the worker holds the listener and the accepted socket (both
    named by the path), the supervisor holds a ``->`` row to the accepted one; an attached client adds a ``->`` row."""
    rows = [f"2.1.319   {WORKER} me   10u  unix 0xaaaa      0t0      {SOCK}",
            f"2.1.319   {WORKER} me   11u  unix 0xbbbb      0t0      {SOCK}",
            f"2.1.319   {SUPERVISOR} me   21u  unix 0xcccc      0t0      ->0xbbbb",
            "Google Chrome   99 me   5u  unix 0xdddd      0t0      ->0x1234"]
    if client:
        rows.append(f"2.1.319   {CLIENT} me   9u  unix 0xeeee      0t0      ->0xbbbb")
    return "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n" + "\n".join(rows) + "\n"


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
    if not os.path.exists(cc.LSOF_PATH):
        pytest.skip("no lsof")
    assert "COMMAND" in cc._run_lsof()


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


def _run(rig, monkeypatch, *, foreground=None, settings=None, away=0.0, entrypoint="cli"):
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
                       idle=lambda: 0.0, max_hold=cc.CLEANUP_RESERVE + 30,
                       still_away=lambda: (False, "intent", "present"), **kw)
    return out, seen


def test_a_non_foreground_request_is_held_pings_with_a_sign_sheet_and_withdraws_when_the_hold_ends(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "no client on the ptySock"))
    assert isinstance(out, cc.Abstained) and out.reason == "hold_expired" and out.held is True
    assert seen["wait"] > 0 and seen["still_away"] is None, seen
    assert seen["pings"] == [out.request.id]          # the request's own ping: the center draws its sign sheet
    assert rig["q"].withdraw(out.request.id) is False   # already withdrawn: the native dialog stands, nothing ghosts


def test_the_foreground_session_keeps_todays_path_no_hold_no_ping(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (True, "the coordinator lease holder"))
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
    monkeypatch.setattr(cc, "_read_roster", _boom if which != "lsof" else (lambda h: _roster(sessionId=None)))
    monkeypatch.setattr(cc, "_run_lsof", _boom)
    out, seen = _run(rig, monkeypatch)
    assert isinstance(out, cc.Abstained) and out.held is True and seen["wait"] > 0 and seen["pings"]


def test_a_foreground_check_that_raises_still_holds(rig, monkeypatch):
    def worse(session):
        raise RuntimeError("a check that fails")
    out, seen = _run(rig, monkeypatch, foreground=worse)   # never a silent skip
    assert isinstance(out, cc.Abstained) and out.held is True and seen["pings"]


def test_no_paired_phone_no_hold(rig, monkeypatch):
    monkeypatch.setattr("levain.webpush.live", lambda queue_dir: False)
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"))
    assert "wait" not in seen


def test_his_explicit_off_word_stops_it(rig, monkeypatch):
    s = cc.AdapterSettings(cockpit_data_root=str(rig["ck"].root), away_hold_seconds=20, explicit_off=True)
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"), settings=s)
    assert "wait" not in seen


def test_an_away_hold_already_running_keeps_its_presence_recheck(rig, monkeypatch):
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"), away=20.0)
    assert seen["wait"] > 0 and seen["still_away"] is not None


def test_no_key_that_could_sign_no_hold(rig, monkeypatch):
    s = cc.AdapterSettings(cockpit_data_root=str(rig["ck"].root) + "-absent", away_hold_seconds=20)
    out, seen = _run(rig, monkeypatch, foreground=lambda s: (False, "x"), settings=s)
    assert "wait" not in seen
