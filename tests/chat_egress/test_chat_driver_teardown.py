"""CH-D2 chatdriver L3 r1 (`1025-23/chatdriver/RULINGS_R1.md` FIX 1-4): the chat launch end never signals a process
group whose number may have been freed (the CLI leader is watched through `confinement._Leader` and reaped last), a
reaped `Popen` is refused at attach, a teardown that could not finish keeps `chat_tmp` and stays retryable, and a
renew-due ping is counted only once it was sent.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain import chat_driver as cd
from levain.chat_egress import launch as lm
from levain.chat_egress import renew
from levain.consent.adapters import claude_code

from .test_chat_driver_launch import _STOPPED, _chat_tmp, _cli, _FakeEgress, _kill_group, _members

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

_real_killpg = os.killpg


def _record_killpg(monkeypatch, proc: subprocess.Popen, *, refuse_kill: bool = False) -> list:
    """Record every non-zero signal sent to a group, with the Popen's returncode at that moment (None: unreaped)."""
    sent: list = []

    def killpg(pgid, sig):
        if sig != 0:
            sent.append((pgid, sig, proc.returncode))
            if refuse_kill and sig == signal.SIGKILL:
                raise PermissionError(1, "Operation not permitted")
        return _real_killpg(pgid, sig)
    monkeypatch.setattr(os, "killpg", killpg)
    return sent


# --- FIX 1: the group is signalled only while the zombie leader reserves its number --------------------------------


def test_every_group_signal_is_sent_while_the_leader_is_unreaped(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    proc = _cli(t)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    sent = _record_killpg(monkeypatch, proc)
    try:
        out = launch.end()
    finally:
        monkeypatch.setattr(os, "killpg", _real_killpg)
        _kill_group(proc)
    print(f"RUN cdfix 1: group signals {sent}; end {out}")
    assert sent and all(rc is None for _pg, _sig, rc in sent)          # never after the reap
    assert out.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
    assert proc.returncode == -signal.SIGTERM and _members(proc.pid) == [] and not t.exists()


def test_a_cli_with_no_children_ends_clean_through_the_zombie_only_group(tmp_path, monkeypatch):
    """The common case under FIX 1: the leader exits on SIGTERM, leaving only itself, a zombie, in the group, and
    macOS answers the group kill with EPERM (measured): that is a group with no live member, not a failure."""
    t = _chat_tmp(tmp_path)
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    out = launch.end()
    assert out.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
    assert out.failures == () and out.complete and not t.exists()


# --- FIX 2: attach refuses a reaped Popen; a reap outside the launch is never followed by a signal ------------------


def test_attach_cli_refuses_a_reaped_popen_even_when_its_pid_now_leads_a_group(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    old = subprocess.Popen(["/usr/bin/true"], start_new_session=True)
    old.wait()
    other = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)   # an unrelated session leader
    old.pid = other.pid                                                      # the reaped pid, reused (simulated)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=_chat_tmp(tmp_path))
    try:
        with pytest.raises(ValueError, match="already reaped"):
            launch.attach_cli(old)
        launch.end()
        assert other.poll() is None                                          # the unrelated group was not killed
    finally:
        other.kill()
        other.wait()


def test_a_cli_reaped_outside_the_launch_is_never_signalled_and_chat_tmp_is_kept(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    proc.kill()
    proc.wait()                                       # the attach contract broken: the number is free now
    sent = _record_signals(monkeypatch, proc)
    try:
        out = launch.end()
        second = launch.end()
    finally:
        monkeypatch.setattr(os, "kill", _real_kill)
        monkeypatch.setattr(os, "killpg", _real_killpg)
    print(f"RUN cdfix3 3: first end {out}; second end {second}; signals {sent}")
    assert sent == []
    # r2 item 3: a terminal verdict, reported once; the CLI part is done, so the end converges
    assert out.cli == cd.CliEnd(exit_code=-signal.SIGKILL, group_gone=False, reaped=False, reaped_outside=True)
    assert out.failures == ("cli: reaped outside the launch; chat_tmp kept",) and out.complete
    assert t.exists() and out.tmp_leftovers == (str(t),)
    assert second is out


def test_cli_exit_reads_the_leader_without_reaping_it(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 0.3; exit 3"], start_new_session=True)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=_chat_tmp(tmp_path))
    launch.attach_cli(proc)
    assert launch.cli_exit(timeout=5) == 3
    assert proc.returncode is None                    # still the launch's to reap
    assert launch.end().cli == cd.CliEnd(exit_code=3, group_gone=True, reaped=True)


# --- FIX 3: a teardown that could not finish keeps chat_tmp and is retried ------------------------------------------


def test_a_group_kill_refused_leaves_chat_tmp_and_the_next_end_finishes(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    proc = _cli(t)
    stops = []
    monkeypatch.setattr(lm, "stop_egress", lambda e: stops.append(1) or _STOPPED)
    monkeypatch.setattr(cd, "CLI_GROUP_WAIT_S", 0.3)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    _record_killpg(monkeypatch, proc, refuse_kill=True)
    try:
        first = launch.end()
        monkeypatch.setattr(os, "killpg", _real_killpg)
        print(f"RUN cdfix 3: first end {first}; chat_tmp exists {t.exists()}")
        # the TERM-ignoring grandchild is still writing: nothing is removed under it, the leader is kept unreaped
        assert first.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=False, reaped=False)
        assert "cli killpg: PermissionError" in first.failures and not first.complete
        assert t.exists() and first.tmp_leftovers == (str(t),) and proc.returncode is None
        with pytest.raises(RuntimeError, match="has ended"):
            launch.attach_cli(proc)
        second = launch.end()
        print(f"RUN cdfix 3: second end {second}")
        assert second.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
        assert second.complete and second.failures == () and not t.exists() and _members(proc.pid) == []
        assert stops == [1]                            # the egress stopped once, on the first call
        assert launch.end() is second
    finally:
        monkeypatch.setattr(os, "killpg", _real_killpg)
        _kill_group(proc)


# --- FIX 4: the renew ping is counted only once sent ----------------------------------------------------------------


def _due(rid: str = "k-soon") -> renew.RenewDue:
    return renew.RenewDue(record_id=rid, host="a.example", expires_at=NOW + timedelta(days=1))


def _svc(tmp: Path) -> cd.ChatEgressService:
    q = tmp / "queue"
    q.mkdir(parents=True, exist_ok=True)
    return cd.ChatEgressService(state_dir=tmp / "state", store=None, queue_dir=q, now=NOW)


@pytest.mark.parametrize("where", ["load_settings", "notify"])
def test_a_renew_ping_that_raises_is_caught_and_not_counted(tmp_path, monkeypatch, where):
    svc = _svc(tmp_path)
    sent = []
    monkeypatch.setattr(claude_code, "load_settings", lambda q: {})
    monkeypatch.setattr(claude_code, "notify", lambda *a, **k: sent.append(k["ref"]) or True)

    def boom(*a, **k):
        raise OSError("claude_code.json mid-rewrite")
    real = getattr(claude_code, where)
    monkeypatch.setattr(claude_code, where, boom)
    svc.renew_due("L1", [_due()], harness="claude-code")  # caught and logged inside the callback
    monkeypatch.setattr(claude_code, where, real)
    svc.renew_due("L2", [_due()], harness="claude-code")
    svc.renew_due("L3", [_due()], harness="claude-code")
    assert sent == ["renew:k-soon"]                    # the failed one was not counted; the next launch pinged once


def test_a_concurrent_launch_waits_on_an_in_flight_ping_and_retries_when_it_fails(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def notify(*a, **k):
        calls.append(k["ref"])
        if len(calls) == 1:
            entered.set()
            release.wait(10)
            return False                               # the first sender had nowhere to go
        return True
    monkeypatch.setattr(claude_code, "load_settings", lambda q: {})
    monkeypatch.setattr(claude_code, "notify", notify)
    a = threading.Thread(target=svc.renew_due, args=("L1", [_due()],), kwargs={"harness": "claude-code"})
    a.start()
    assert entered.wait(10)
    b = threading.Thread(target=svc.renew_due, args=("L2", [_due()],), kwargs={"harness": "claude-code"})
    b.start()
    time.sleep(0.3)                                    # B has met the in-flight ping
    assert len(calls) == 1
    release.set()
    a.join(10)
    b.join(10)
    assert not a.is_alive() and not b.is_alive()
    assert calls == ["renew:k-soon", "renew:k-soon"]   # B rechecked after A failed, and sent it
    svc.renew_due("L3", [_due()], harness="claude-code")
    assert len(calls) == 2                             # now it is counted


# --- ROUND 3 (`1025-25/cdfix/RULINGS_R2.md`) ------------------------------------------------------------------------

_real_kill = os.kill


def _record_signals(monkeypatch, proc: subprocess.Popen) -> list:
    """Record every non-zero signal sent by pid (``kill``) or by group (``killpg``), with the Popen's returncode."""
    sent: list = []

    def kill(pid, sig):
        if sig != 0:
            sent.append(("kill", pid, sig, proc.returncode))
        return _real_kill(pid, sig)

    def killpg(pgid, sig):
        if sig != 0:
            sent.append(("killpg", pgid, sig, proc.returncode))
        return _real_killpg(pgid, sig)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return sent


def test_end_cli_never_signals_a_leader_it_already_reaped(tmp_path, monkeypatch):
    """Item 1 (deepseek HIGH 1): once the launch reaped the leader its number is free; a later teardown sends
    nothing to it, by pid or by group."""
    from levain.firing.confinement import _Leader

    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    leader = _Leader(proc)
    first: list = []
    try:
        done = cd._end_cli(leader, first)
        assert done == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True) and leader.reaped
        sent = _record_signals(monkeypatch, proc)
        again: list = []
        out = cd._end_cli(leader, again)
    finally:
        monkeypatch.setattr(os, "kill", _real_kill)
        monkeypatch.setattr(os, "killpg", _real_killpg)
    print(f"RUN cdfix3 1: signals after the reap {sent}; second _end_cli {out}, failures {again}")
    assert sent == []
    assert out == done and again == []


def test_a_stop_egress_that_raised_keeps_end_retryable_and_the_retry_runs_only_that(tmp_path, monkeypatch):
    """Item 2 (codex r2 MED): a LaunchEnd is complete only once the CLI teardown, stop_egress and the chat_tmp step
    are all done; a retry runs only what is not, so the reaped leader gets no second signal (item 1)."""
    t = _chat_tmp(tmp_path)
    proc = _cli(t)
    stops: list = []

    def stop(e):
        stops.append(1)
        if len(stops) == 1:
            raise RuntimeError("glue would not stop")
        return _STOPPED
    monkeypatch.setattr(lm, "stop_egress", stop)
    ends, removals = [], []
    real_end_cli, real_remove = cd._end_cli, cd._remove_chat_tmp
    monkeypatch.setattr(cd, "_end_cli", lambda leader, failures: ends.append(1) or real_end_cli(leader, failures))
    monkeypatch.setattr(cd, "_remove_chat_tmp", lambda p: removals.append(1) or real_remove(p))
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    try:
        first = launch.end()
        kept = t.exists()
        sent = _record_signals(monkeypatch, proc)
        second = launch.end()
    finally:
        monkeypatch.setattr(os, "kill", _real_kill)
        monkeypatch.setattr(os, "killpg", _real_killpg)
        _kill_group(proc)
    print(f"RUN cdfix3 2: first end {first}; signals on the retry {sent}; second end {second}; "
          f"_end_cli calls {len(ends)}, chat_tmp removals {len(removals)}, stop_egress calls {len(stops)}")
    assert first.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
    assert first.failures == ("egress: RuntimeError", "chat_tmp: kept, the egress is not confirmed stopped")
    assert first.egress is None and not first.complete
    assert kept and first.tmp_leftovers == (str(t),)            # chatint 5a: the egress may still write into it
    assert sent == []                                            # no second signal to the reaped pid
    assert second.cli == first.cli and second.egress == _STOPPED and second.failures == () and second.complete
    assert (len(ends), len(removals), len(stops)) == (1, 1, 2)   # only the part not done was retried
    assert launch.end() is second


def test_a_hung_ping_is_taken_over_and_its_sender_pops_only_its_own_claim(tmp_path, monkeypatch):
    """Item 4 (deepseek r2 3): a waiter whose wait timed out takes the claim over and sends the ping itself; the hung
    sender's ``finally``, when it returns at last, leaves the newer claim in place."""
    svc = _svc(tmp_path)
    monkeypatch.setattr(cd, "RENEW_WAIT_S", 0.3)
    key = (_due().record_id, _due().expires_at)
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []

    def notify(*a, **k):
        n = len(calls)
        calls.append(k["ref"])
        if n < 2:                                      # A hangs, then B (the takeover) hangs too
            entered[n].set()
            release[n].wait(10)
            return n == 1                              # A had nowhere to go in the end; B sent it
        return True
    monkeypatch.setattr(claude_code, "load_settings", lambda q: {})
    monkeypatch.setattr(claude_code, "notify", notify)
    a = threading.Thread(target=svc.renew_due, args=("L1", [_due()],), kwargs={"harness": "claude-code"})
    a.start()
    b = None
    try:
        assert entered[0].wait(10)
        b = threading.Thread(target=svc.renew_due, args=("L2", [_due()],), kwargs={"harness": "claude-code"})
        b.start()
        took_over = entered[1].wait(5)                 # B waited 0.3 s on A's hung claim, then took it over
        b_claim = svc._in_flight.get(key)
        release[0].set()
        a.join(10)
        kept = svc._in_flight.get(key)
        print(f"RUN cdfix3 4: B took over {took_over}; notify calls {calls}; claim after A returned is B's "
              f"{kept is not None and kept is b_claim}")
        assert took_over and calls == ["renew:k-soon", "renew:k-soon"]
        assert kept is not None and kept is b_claim    # A popped only its own claim
    finally:
        release[0].set()
        release[1].set()
        a.join(10)
        if b is not None:
            b.join(10)
    assert svc._in_flight == {}
    svc.renew_due("L3", [_due()], harness="claude-code")
    assert len(calls) == 2                             # B's send counted: not pinged again


def test_cli_exit_of_a_cli_that_exited_unreaped_before_attach_is_none_until_end_reaps_it(tmp_path, monkeypatch):
    """Item 5 (deepseek r2 8): the docstring's "exited before it was attached" case is reachable: attach refuses a
    REAPED Popen, not an exited one. On macOS its watch never saw the exit, so no status until the reap reads it."""
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    from levain.firing.confinement import _Leader

    # Exited before attach, deterministically (codex r3 LOW: a sleep let a slow worker attach a running child):
    # the child blocks on its stdin until a non-reaping watch is set, then exits once the pipe closes.
    proc = subprocess.Popen(["/bin/sh", "-c", "read x; exit 4"], stdin=subprocess.PIPE, start_new_session=True)
    watch = _Leader(proc)
    proc.stdin.close()
    assert watch.wait(10) and watch.status == 4                # exited, seen without a reap
    watch.release_watch()
    assert proc.returncode is None                             # a zombie, unreaped
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=_chat_tmp(tmp_path))
    launch.attach_cli(proc)
    before = launch.cli_exit(timeout=0.5)
    out = launch.end()
    after = launch.cli_exit()
    print(f"RUN cdfix3 5: cli_exit before end {before}; end {out.cli}; cli_exit after end {after}")
    if sys.platform == "darwin":
        assert before is None
    assert out.cli == cd.CliEnd(exit_code=4, group_gone=True, reaped=True) and after == 4


@pytest.mark.chat_egress_srt
def test_run_a_real_launch_whose_stop_egress_fails_once(tmp_path, monkeypatch):
    """Items 1+2 over the real glue + srt (no seams): the first stop_egress raises before it touches the egress,
    end() is incomplete, the retry stops the real egress and sends no signal to the reaped CLI leader."""
    from levain.chat_egress import classify, keys
    from levain.chat_egress import decider as dm
    from levain.coordinator.envelopes import EnvelopeStore

    from .decider_rig import Desk, Live, NoStore
    from .fixtures import env as envmod
    from .test_chat_driver_launch import _authority, _notify_queue
    from .test_launch import _node_children, real_launch

    monkeypatch.delenv(lm.TEST_SEAMS_ENV, raising=False)
    store = EnvelopeStore(tmp_path / "dispatch")
    q, _got = _notify_queue(tmp_path)
    routes = keys.RouteSet.of([keys.RouteSnapshot(
        record_id="k-soon", canonical_host="a.example", credential_source="svc", declared_repos=frozenset(),
        attach=True, credential_account="acct", incarnation="1:x")])
    before = {p.pid for p in _node_children()}
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=store, queue_dir=q, node=envmod.node_bin(),
                               srt_prefix=envmod.srt_prefix(), now=NOW)
    t = _chat_tmp(tmp_path / "run")
    launch = svc.launch(launch_id=real_launch(store), chat_tmp=t, events=tmp_path / "events.jsonl", routes=routes,
                        liveness=Live(), credentials=keys.LaunchCredentials(NoStore()), cards=Desk(),
                        mode=dm.Fixed(classify.GATED), authority=_authority(**{"k-soon": timedelta(days=2)}),
                        harness="claude-code",
                        limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0), now=NOW)
    real_stop, stops = lm.stop_egress, []

    def stop_once_failing(e):
        stops.append(1)
        if len(stops) == 1:
            raise RuntimeError("stop_egress failed once (injected before it ran)")
        return real_stop(e)
    monkeypatch.setattr(lm, "stop_egress", stop_once_failing)
    proc = _cli(t)
    launch.attach_cli(proc)
    try:
        sent = _record_signals(monkeypatch, proc)
        first = launch.end()
        kept = t.exists()
        at_first = list(sent)
        second = launch.end()
    finally:
        monkeypatch.setattr(os, "kill", _real_kill)
        monkeypatch.setattr(os, "killpg", _real_killpg)
        monkeypatch.setattr(lm, "stop_egress", real_stop)
        _kill_group(proc)
    print(f"RUN cdfix3 real: first end {first}; signals by the first end {at_first}; signals by the retry "
          f"{sent[len(at_first):]}; second end {second}; stop_egress calls {len(stops)}; group members after "
          f"{_members(proc.pid)}; chat_tmp exists {t.exists()}")
    assert at_first and all(rc is None for *_x, rc in at_first)        # every signal while the leader was unreaped
    assert sent[len(at_first):] == []                                  # none to the reaped pid on the retry
    assert first.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
    assert first.failures == ("egress: RuntimeError", "chat_tmp: kept, the egress is not confirmed stopped")
    assert not first.complete and first.egress is None and kept and first.tmp_leftovers == (str(t),)
    assert second.cli == first.cli and second.complete and second.failures == ()
    assert second.egress.glue_exit == 0 and second.egress.final_flush is not None and len(stops) == 2
    assert not t.exists() and _members(proc.pid) == []
    assert {p.pid for p in _node_children()} <= before and dm.service_threads() == []


# --- chatint (`1025-25/cdfix3/VERIFY_ALL.md`, complement N1): the Linux group wait reads /proc --------------------


class _ExitedLeader:
    """A `confinement._Leader` as `_end_cli` reads it: the CLI exited on its own, seen without a reap, so it stays an
    unreaped zombie that holds its group's number. No real process: the group is the synthetic /proc's."""

    def __init__(self, pid: int) -> None:
        self.proc = type("P", (), {"returncode": None})()
        self.pid, self.status, self.exited, self.reaped = pid, 0, True, False

    def wait(self, timeout: float) -> bool:
        return True

    def reap(self, timeout: float | None = None) -> bool:
        self.reaped = True
        return True

    def release_watch(self) -> None:
        pass


def test_on_linux_a_group_holding_only_the_unreaped_leader_is_gone(tmp_path, monkeypatch):
    """complement N1 (cdfix verify r2): off macOS `_group_gone` answers live for any group `killpg` still finds, and
    the held zombie leader keeps it findable, so the teardown never saw the group gone and chat_tmp was never removed.
    `group_gone_proc` reads /proc, which shows only that zombie: gone, and the leader is reaped."""
    from levain.firing import confinement as C

    from ..test_group_live_linux import _mountinfo, _proc

    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    _mountinfo(root, "rw")
    _proc(root, 4242, "claude", "Z", 4242)              # the CLI's leader, exited and held unreaped
    _proc(root, 4300, "sleep", "S", 9999)               # another group's live process
    monkeypatch.setattr(C, "_PROC_ROOT", str(root))
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    sent: list = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))   # the group is found (the zombie)
    monkeypatch.setattr(cd, "CLI_GROUP_WAIT_S", 0.5)
    leader, failures = _ExitedLeader(4242), []
    try:
        out = cd._end_cli(leader, failures)
    finally:
        monkeypatch.setattr(os, "killpg", _real_killpg)
    print(f"RUN chatint 3: synthetic Linux end {out}; failures {failures}; group signals {sent}")
    assert out == cd.CliEnd(exit_code=0, group_gone=True, reaped=True) and failures == []
    assert (4242, signal.SIGKILL) in sent


# --- chatint L3 r1 (`1025-30/chatint/RULINGS_R1.md`) ---------------------------------------------------------------


def test_the_group_is_sigkilled_before_any_scan_the_gone_verdict_rests_on(tmp_path, monkeypatch):
    """Item 1 (codex HIGH 1): the /proc scan reads one entry at a time, so a member that keeps forking can be missing
    from every scan while it lives. Simulated here as a live member each listing of /proc leaves out until the group
    is SIGKILLed (then it is a zombie). Every scan the gone verdict rests on comes after that kill."""
    from levain.firing import confinement as C

    from ..test_group_live_linux import _mountinfo, _proc, _stat

    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    _mountinfo(root, "rw")
    _proc(root, 4242, "claude", "Z", 4242)              # the CLI's leader, exited and held unreaped
    _proc(root, 4300, "forker", "R", 4242)              # a live member that slips past every scan before the kill
    killed, scans = [False], []
    real_listdir = os.listdir

    def listdir(path):
        names = real_listdir(path)
        if os.fspath(path) == str(root):
            scans.append(killed[0])
            if not killed[0]:
                names = [n for n in names if n != "4300"]
        return names

    def killpg(pgid, sig):
        if pgid == 4242 and sig == signal.SIGKILL:
            killed[0] = True
            (root / "4300" / "stat").write_bytes(_stat(4300, "forker", "Z", 4242))
    monkeypatch.setattr(C, "_PROC_ROOT", str(root))
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(C.os, "listdir", listdir)
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(cd, "CLI_GROUP_WAIT_S", 0.5)
    leader, failures = _ExitedLeader(4242), []
    try:
        out = cd._end_cli(leader, failures)
    finally:
        monkeypatch.setattr(os, "killpg", _real_killpg)
        monkeypatch.setattr(C.os, "listdir", real_listdir)
    print(f"RUN chatfix 1: end {out}; failures {failures}; kill sent before each scan {scans}")
    assert out == cd.CliEnd(exit_code=0, group_gone=True, reaped=True) and failures == []
    assert len(scans) >= 2 and all(scans)               # no scan before the group SIGKILL


def _stop(**bad) -> lm.StopResult:
    return lm.StopResult(**{"glue_exit": 0, "decider_clean": True, "socks_clean": True, "final_flush": None,
                            "order": ("lifeline", "glue", "decider", "socks"), **bad})


@pytest.mark.parametrize("bad, named", [
    ({"failures": ("glue: TimeoutExpired",)}, "egress glue: TimeoutExpired"),
    ({"decider_clean": False}, "egress: the decider did not close clean"),
    ({"socks_clean": False}, "egress: the SOCKS refuser did not close clean"),
    ({"glue_exit": None}, "egress: the glue was not seen to exit"),
])
def test_a_stop_result_that_is_not_a_clean_stop_keeps_chat_tmp_and_the_end_retryable(tmp_path, monkeypatch, bad,
                                                                                      named):
    """Item 2 (codex HIGH 2 + complement MED 1): stop_egress catches each step's failure into its StopResult, so a
    returned result is not a stop by itself. One with failures, an unclean decider or SOCKS close, or no glue exit
    keeps chat_tmp, names why, and the next end() calls stop_egress again."""
    t = _chat_tmp(tmp_path)
    results = [_stop(**bad), _stop()]
    calls: list = []
    monkeypatch.setattr(lm, "stop_egress", lambda e: calls.append(1) or results[len(calls) - 1])
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    first = launch.end()
    kept = t.exists()
    second = launch.end()
    print(f"RUN chatfix 2 {bad}: first end {first}; second end {second}")
    assert first.failures == (named, "chat_tmp: kept, the egress is not confirmed stopped")
    assert not first.complete and kept and first.tmp_leftovers == (str(t),) and first.egress is results[0]
    assert second.complete and second.failures == () and second.egress is results[1] and not t.exists()
    assert len(calls) == 2 and launch.end() is second


def test_a_reap_that_raised_after_reaping_is_the_launchs_reap_not_one_outside(tmp_path, monkeypatch):
    """Item 3 (complement MED 2 + codex MED 3): the launch's own reap may raise after the kernel reaped the leader;
    the returncode that then appears is its reap's, so the retry finishes the end (chat_tmp removed) and sends no
    signal, instead of calling the CLI reaped outside and keeping chat_tmp for good."""
    t = _chat_tmp(tmp_path)
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)

    def reap(timeout=None):
        proc.wait()                                    # the kernel reap ran
        raise OSError("raised after the reap")
    launch._cli.reap = reap
    try:
        first = launch.end()
        kept = t.exists()
        sent = _record_signals(monkeypatch, proc)
        second = launch.end()
    finally:
        monkeypatch.setattr(os, "kill", _real_kill)
        monkeypatch.setattr(os, "killpg", _real_killpg)
    print(f"RUN chatfix 3: first end {first}; second end {second}; signals on the retry {sent}")
    assert first.failures == ("cli reap: OSError", "chat_tmp: kept, the CLI's process group is not confirmed gone")
    assert not first.complete and kept
    assert second.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
    assert second.complete and second.failures == () and not t.exists() and sent == []


def _watch_open(leader) -> bool:
    return leader._kq is not None or leader._pidfd is not None


def test_a_cli_reaped_outside_leaves_no_watch_open(tmp_path, monkeypatch):
    """Item 4 (codex MED 4): the terminal reaped-outside path releases the leader's watch (kqueue / pidfd)."""
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=_chat_tmp(tmp_path))
    launch.attach_cli(proc)
    leader = launch._cli
    opened = _watch_open(leader)
    proc.kill()
    proc.wait()
    out = launch.end()
    print(f"RUN chatfix 4 outside: watch open at attach {opened}; after end {_watch_open(leader)}; end {out.cli}")
    assert opened and out.cli.reaped_outside and not _watch_open(leader)


def test_an_exited_leader_kept_while_its_group_is_not_gone_leaves_no_watch_open(tmp_path, monkeypatch):
    """Item 4 (codex MED 4): the retry path keeps an exited leader unreaped (its group is not confirmed gone) and
    releases its watch; the retry still reaps it."""
    t = _chat_tmp(tmp_path)
    proc = _cli(t)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    monkeypatch.setattr(cd, "CLI_GROUP_WAIT_S", 0.3)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    leader = launch._cli
    _record_killpg(monkeypatch, proc, refuse_kill=True)
    try:
        first = launch.end()
        held = _watch_open(leader)
        monkeypatch.setattr(os, "killpg", _real_killpg)
        second = launch.end()
    finally:
        monkeypatch.setattr(os, "killpg", _real_killpg)
        _kill_group(proc)
    print(f"RUN chatfix 4 held: first {first.cli}; watch open while held {held}; second {second.cli}")
    assert first.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=False, reaped=False) and not held
    assert second.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True) and second.complete


def test_a_proc_that_may_hide_members_is_named_as_the_cause_not_a_bare_timeout(tmp_path, monkeypatch):
    """Item 6 (complement LOW 4): /proc mounted hidepid answers live for every group killpg finds, so the group is
    never confirmed gone; the failure names that cause."""
    from levain.firing import confinement as C

    from ..test_group_live_linux import _mountinfo, _proc

    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    _mountinfo(root, "rw,hidepid=2")
    _proc(root, 4242, "claude", "Z", 4242)
    monkeypatch.setattr(C, "_PROC_ROOT", str(root))
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(cd, "CLI_GROUP_WAIT_S", 0.2)
    leader, failures = _ExitedLeader(4242), []
    try:
        out = cd._end_cli(leader, failures)
    finally:
        monkeypatch.setattr(os, "killpg", _real_killpg)
    print(f"RUN chatfix 6: end {out}; failures {failures}")
    assert out == cd.CliEnd(exit_code=0, group_gone=False, reaped=False) and not leader.reaped
    assert failures == ["cli group poll: /proc may hide members: chat_tmp kept"]


def test_a_reap_that_raised_after_reaping_leaves_no_watch_open_on_the_retry(tmp_path, monkeypatch):
    """Verify r2 item a (complement LOW 1 + codex MED): the retry's own-reap fast path releases the leader's watch
    before it answers reaped; the reap that raised never closed it."""
    t = _chat_tmp(tmp_path)
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    leader = launch._cli

    def reap(timeout=None):
        proc.wait()                                    # the kernel reap ran, the watch was never closed
        raise OSError("raised after the reap")
    leader.reap = reap
    first = launch.end()
    held = _watch_open(leader)
    second = launch.end()
    print(f"RUN fix2 a: watch open after the raising reap {held}; after the retry {_watch_open(leader)}; {second.cli}")
    assert not first.complete and held
    assert second.cli.reaped and second.complete and not _watch_open(leader)
