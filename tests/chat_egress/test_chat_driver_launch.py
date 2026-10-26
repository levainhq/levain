"""CH-D2: the chat driver's call sites into the egress (`levain.chat_driver.ChatEgressService` / `ChatLaunch`).

One CA per service start (`ca.new_egress_ca`), handed to every launch; the srt prefix handed to `start_egress`, which
checks it; 19a's renew-due ping through the consent gate's own `notify`; and the launch end in order: the CLI leader
signalled and seen exited, its process group killed and polled for live members only, the leader reaped last (C11),
`stop_egress`, then `chat_tmp` removed by `tmp_clean`. The `chat_egress_srt` run drives the real glue over srt 0.0.81 (no network: nothing is
requested through it).
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import chat_driver as cd
from levain.chat_egress import ca, classify, keys, renew, srt_pin
from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.cockpit.consent import utc_stamp

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

# A CLI stand-in: the leader runs a grandchild that ignores SIGTERM and keeps writing into chat_tmp, then waits.
_CLI_SCRIPT = """
mkdir "$1/locked" && touch "$1/locked/f" && chmod 000 "$1/locked"
/bin/sh -c 'trap "" TERM; while :; do date > "$1/live"; sleep 0.05; done' sh "$1" &
wait
"""


def _cli(chat_tmp: Path, *, ignore_term: bool = False) -> subprocess.Popen:
    script = ("trap '' TERM\n" if ignore_term else "") + _CLI_SCRIPT
    p = subprocess.Popen(["/bin/sh", "-c", script, "cli", str(chat_tmp)], start_new_session=True)
    end = time.monotonic() + 5
    while not (chat_tmp / "live").exists():
        assert time.monotonic() < end, "the CLI stand-in never wrote into chat_tmp"
        time.sleep(0.02)
    return p


def _members(pgid: int) -> list[str]:
    return subprocess.run(["/usr/bin/pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout.split()


def _exited_unreaped(pid: int) -> bool:
    """The leader has exited and is not reaped: a zombie in /proc on Linux (state Z); on macOS, where pgrep lists no
    zombie, gone from its group's members."""
    stat = Path(f"/proc/{pid}/stat")
    if Path("/proc/self/stat").exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return False
    return str(pid) not in _members(pid)

def _kill_group(proc: subprocess.Popen) -> None:
    """Test cleanup: kill the CLI's group only while its leader is unreaped (its number still its own), then reap."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _chat_tmp(tmp: Path) -> Path:
    d = tmp / "chat-tmp" / "L1"
    d.mkdir(parents=True, mode=0o700)
    return d


def _describe_children(pids) -> list[str]:
    """Each pid as `pid ppid pgid state command` from `ps`, which lists a zombie on macOS and Linux alike (Linux's
    pgrep lists one too; macOS's does not): what a leftover child of this process is, named in the failure."""
    if not pids:
        return []
    out = subprocess.run(["ps", "-o", "pid=,ppid=,pgid=,stat=,command=", "-p", ",".join(str(p) for p in pids)],
                         capture_output=True, text=True).stdout
    return [" ".join(line.split()) for line in out.splitlines()] or [f"{p}: gone by the time ps ran" for p in pids]


def _record_spawns(monkeypatch) -> list[tuple[int, list[str], subprocess.Popen]]:
    """Record (pid, argv, the Popen) of every Popen this process starts from here on: a zombie's `ps` command carries
    no argv on Linux (`[python3] <defunct>`), so a leftover is told apart by what it was started as. Holding the Popen
    keeps its exit status (`returncode`) and stops subprocess's own cleanup of a dropped one."""
    spawned: list[tuple[int, list[str], subprocess.Popen]] = []
    real = subprocess.Popen

    class Recording(real):
        def __init__(self, args, *a, **k):
            super().__init__(args, *a, **k)
            spawned.append((self.pid, [str(x) for x in (args if isinstance(args, (list, tuple)) else [args])], self))
    monkeypatch.setattr(subprocess, "Popen", Recording)
    return spawned


def _ping_spawns(spawned, tmp: Path) -> dict[int, subprocess.Popen]:
    """The renew ping's two detached children among the recorded spawns: its notify command (`ping.sh`, argv[0] the
    absolute path) and its record writer (`python -P -m levain.webpush send`, `webpush.sender.spawn`)."""
    return {pid: proc for pid, argv, proc in list(spawned)
            if argv[0] == str(tmp / "ping.sh") or "levain.webpush" in argv}


def _wait_detached(procs: dict[int, subprocess.Popen], timeout: float = 10.0) -> dict[int, int | None]:
    """Reap each detached child once it exits, up to `timeout` s in all; pid -> exit status, None for one still
    running then. The renew ping's notify command and its record writer are started detached and never waited on by
    design (`consent.adapters.claude_code.notify`, `webpush.sender.spawn`), so the test reaps its own."""
    end = time.monotonic() + timeout
    status = {pid: proc.poll() for pid, proc in procs.items()}
    while any(rc is None for rc in status.values()) and time.monotonic() < end:
        time.sleep(0.05)
        status = {pid: proc.poll() for pid, proc in procs.items()}
    return status


def _in_process_table(pids) -> list[int]:
    """Which of `pids` `ps` still lists (a zombie included, on macOS and Linux alike)."""
    if not pids:
        return []
    out = subprocess.run(["ps", "-o", "pid=", "-p", ",".join(str(p) for p in pids)],
                         capture_output=True, text=True).stdout
    return sorted(int(x) for x in out.split())


def _only_the_ping_is_left(spawned, before: set[int], tmp: Path) -> set[int]:
    """After a real launch whose renew ping fired (`_notify_queue(tmp)`): every other process this one started since
    `before` is exited, reaped by its owner (never polled here, which would reap it) and gone from the process table (the glue included), and the ping's two detached
    children are reaped here once they exit 0. Returns the ping's pids. Neither ping child is waited on by the launch,
    so either may outlive its end (Linux CI 10-25 failed a bare child count on the pid just before the CLI's, the
    slot the record writer takes), and the record writer is spawned after the notify command, so the census waits for
    both spawns to be recorded first."""
    from .test_launch import _node_children

    end = time.monotonic() + 10.0
    while len(_ping_spawns(spawned, tmp)) < 2 and time.monotonic() < end:
        time.sleep(0.05)
    ping = _ping_spawns(spawned, tmp)
    assert len(ping) == 2, f"the renew ping started {len(ping)} detached children, expected 2: {sorted(ping)}"
    left = {p.pid for p in _node_children()} - before
    print(f"RUN children left: {_describe_children(sorted(left))}; the renew ping's {sorted(ping)}")
    assert left - set(ping) == set(), _describe_children(sorted(left - set(ping)))   # the glue, and anything else, is gone
    others = {pid: proc for pid, _argv, proc in list(spawned) if pid not in ping}
    unreaped = sorted(pid for pid, proc in others.items() if proc.returncode is None)
    assert unreaped == [], f"started and not exited or not reaped: {_describe_children(unreaped)}"
    still = _in_process_table(sorted(others))
    assert still == [], f"in the process table after the launch ended: {_describe_children(still)}"
    status = _wait_detached(ping)
    assert all(rc == 0 for rc in status.values()), (
        f"the renew ping's children exit 0 (pid: status, None = still running): {status}; "
        f"{_describe_children([pid for pid, rc in status.items() if rc != 0])}")
    return set(ping)


class _FakeEgress:
    launch_id = "L1"


_STOPPED = lm.StopResult(glue_exit=0, decider_clean=True, socks_clean=True, final_flush=None,
                         order=("lifeline", "glue", "decider", "socks"))   # a clean stop


# --- the launch end (C11, stop_egress, tmp_clean) -------------------------------------------------------------------


def test_end_reaps_the_cli_then_kills_its_group_then_stops_egress_then_removes_chat_tmp(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    proc = _cli(t)
    seen = []

    def stop(e):
        seen.append(("egress", _members(proc.pid), t.exists()))
        return _STOPPED
    monkeypatch.setattr(lm, "stop_egress", stop)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    try:
        out = launch.end()
    finally:
        _kill_group(proc)
    print(f"RUN chatdriver C11: {out}; at stop_egress: {seen}")
    # the leader died of SIGTERM; its TERM-ignoring grandchild was killed with the group before egress stopped
    assert out.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
    assert seen == [("egress", [], True)] and out.egress == _STOPPED
    assert out.tmp_leftovers == () and not t.exists() and out.failures == ()
    assert launch.end() is out                       # idempotent


def test_a_cli_leader_that_ignores_term_is_killed_and_reaped(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    proc = _cli(t, ignore_term=True)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    monkeypatch.setattr(cd, "CLI_TERM_WAIT_S", 0.3)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    try:
        out = launch.end()
    finally:
        _kill_group(proc)
    assert out.cli == cd.CliEnd(exit_code=-signal.SIGKILL, group_gone=True, reaped=True) and _members(proc.pid) == []
    assert not t.exists()


def test_a_cli_that_already_exited_leaves_its_orphans_to_the_group_kill(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    # the leader exits at once, leaving its TERM-ignoring child behind in the group
    proc = subprocess.Popen(["/bin/sh", "-c", "/bin/sh -c 'trap \"\" TERM; exec /bin/sleep 60' & exit 0"],
                            start_new_session=True)
    end = time.monotonic() + 5
    # the leader stays unreaped (a zombie still in the group): Linux's pgrep lists it, macOS's does not, so the
    # orphan is counted apart from the leader, and the leader's exit is waited for without reaping it
    while [m for m in _members(proc.pid) if m != str(proc.pid)] == [] or not _exited_unreaped(proc.pid):
        assert time.monotonic() < end
        time.sleep(0.02)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    try:
        out = launch.end()
    finally:
        _kill_group(proc)
    assert out.cli == cd.CliEnd(exit_code=0, group_gone=True, reaped=True) and _members(proc.pid) == []


def test_attach_cli_refuses_a_child_in_another_group_a_second_cli_and_an_ended_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=_chat_tmp(tmp_path))
    same_group = subprocess.Popen(["/bin/sleep", "30"])
    lead = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        with pytest.raises(ValueError, match="lead its own process group"):
            launch.attach_cli(same_group)
        launch.attach_cli(lead)
        with pytest.raises(RuntimeError, match="already has its CLI"):
            launch.attach_cli(lead)
        launch.end()
        late = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        try:
            with pytest.raises(RuntimeError, match="has ended"):
                cd.ChatLaunch.attach_cli(launch, late)
        finally:
            late.kill()
            late.wait()
        assert lead.returncode == -signal.SIGTERM
    finally:
        for p in (same_group, lead):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_every_end_step_runs_whatever_an_earlier_one_raised_and_the_end_is_retried(tmp_path, monkeypatch):
    t = _chat_tmp(tmp_path)
    proc = _cli(t)
    real_stop, real_end_cli = lm.stop_egress, cd._end_cli

    def boom(e):
        raise RuntimeError("glue would not stop")
    monkeypatch.setattr(lm, "stop_egress", boom)
    monkeypatch.setattr(cd, "_end_cli", lambda leader, failures: (_ for _ in ()).throw(OSError("teardown")))
    launch = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t)
    launch.attach_cli(proc)
    try:
        out = launch.end()
        assert out.failures == ("cli: OSError", "egress: RuntimeError",
                                "chat_tmp: kept, the CLI teardown did not finish")
        assert out.cli is None and out.egress is None and not out.complete
        assert t.exists() and out.tmp_leftovers == (str(t),)          # its writer may still be running
        monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
        monkeypatch.setattr(cd, "_end_cli", real_end_cli)
        again = launch.end()                                          # both failed steps are tried again
        assert again.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True)
        assert again.egress == _STOPPED and again.failures == () and again.complete and not t.exists()
    finally:
        monkeypatch.setattr(lm, "stop_egress", real_stop)
        _kill_group(proc)


def test_chat_tmp_with_a_symlink_out_and_a_mode_000_dir_is_removed_and_the_target_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "stop_egress", lambda e: _STOPPED)
    t = _chat_tmp(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    (t / "out").symlink_to(outside)
    (t / "deep" / "er").mkdir(parents=True)
    (t / "deep" / "er" / "f").write_text("x")
    os.chmod(t / "deep", 0)
    out = cd.ChatLaunch(egress=_FakeEgress(), chat_tmp=t).end()
    assert out.tmp_leftovers == () and not t.exists() and (outside / "keep").read_text() == "k"


# --- the service: one CA, the srt prefix, a refused start ------------------------------------------------------------


def _store(launches=("L1", "L2")):
    return SimpleNamespace(launch=lambda lid: object() if lid in launches else None)


def _launch_kw(tmp: Path, lid: str) -> dict:
    return dict(launch_id=lid, chat_tmp=_chat_tmp(tmp / lid), events=tmp / f"{lid}.jsonl",
                routes=keys.RouteSet.of([]), liveness=None, credentials=None, cards=None,
                mode=dm.Fixed(classify.GATED), authority=SimpleNamespace(policy=SimpleNamespace(grants={})),
                harness="claude-code")


def test_one_ca_per_service_and_the_default_srt_prefix_reach_every_start(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    minted = []
    real_mint = ca.new_egress_ca
    monkeypatch.setattr(ca, "new_egress_ca", lambda *a, **k: minted.append(1) or real_mint(*a, **k))
    calls = []
    monkeypatch.setattr(lm, "start_egress", lambda **kw: calls.append(kw) or SimpleNamespace(launch_id=kw["launch_id"]))
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=tmp_path / "q", now=NOW)
    a = svc.launch(**_launch_kw(tmp_path, "L1"))
    b = svc.launch(**_launch_kw(tmp_path, "L2"))
    assert minted == [1] and [c["launch_id"] for c in calls] == ["L1", "L2"]
    assert calls[0]["ca_pem"] == calls[1]["ca_pem"] == svc.ca.glue_pem()
    assert b"PRIVATE KEY" in calls[0]["ca_pem"]
    assert {c["srt_prefix"] for c in calls} == {srt_pin.default_prefix()} == {(tmp_path / "home" / "chat-node").resolve()}
    assert all(c["on_renew_due"].func == svc.renew_due and c["on_renew_due"].keywords == {"harness": "claude-code"}
               for c in calls)
    assert all(c["authority"].policy.grants == {} for c in calls)            # the launch's authority reaches the start
    assert [p.name for p in (tmp_path / "state" / ca.CA_DIR).iterdir()] == [f"{svc.ca.start_id}.pem"]
    assert a.chat_tmp.exists() and b.chat_tmp.exists()


def test_a_launch_within_a_day_of_the_ca_expiry_re_mints_it_and_keeps_the_old_cert(tmp_path, monkeypatch):
    """chatdriver-2 (`1025-23/chatdriver/RULINGS_OBS.md` 1): a service up past its CA's validity would hand launches
    an expired CA; a launch within `CA_REMINT_MARGIN` of `not_valid_after` gets a new one. The earlier cert stays on
    disk: launches started on it may still be running."""
    calls = []
    monkeypatch.setattr(lm, "start_egress", lambda **kw: calls.append(kw) or SimpleNamespace(launch_id=kw["launch_id"]))
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=tmp_path / "q", now=NOW)
    first = svc.ca
    svc.launch(**_launch_kw(tmp_path, "L1"), now=first.not_valid_after - cd.CA_REMINT_MARGIN - timedelta(seconds=1))
    assert svc.ca is first and calls[0]["ca_pem"] == first.glue_pem()
    svc.launch(**_launch_kw(tmp_path, "L2"), now=first.not_valid_after - cd.CA_REMINT_MARGIN)
    second = svc.ca
    print(f"RUN chatdriver-2 1: first CA {first.start_id} expires {first.not_valid_after}; the launch at the margin "
          f"got {second.start_id}, expiring {second.not_valid_after}")
    assert second is not first and second.not_valid_after > first.not_valid_after
    assert calls[1]["ca_pem"] == second.glue_pem() != calls[0]["ca_pem"]
    assert first.cert_path.exists() and second.cert_path.exists()


def test_each_launch_renders_the_trust_env_of_the_ca_its_egress_got(tmp_path, monkeypatch):
    """chd2fix P1: the env a launch renders names the cert of the CA handed to ITS start (a re-mint between two
    launches gives the second the new cert), with pip on legacy-certs (truststore cannot verify in the sandbox)."""
    monkeypatch.setattr(lm, "start_egress", lambda **kw: SimpleNamespace(launch_id=kw["launch_id"]))
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=tmp_path / "q", now=NOW)
    first = svc.ca
    a = svc.launch(**_launch_kw(tmp_path, "L1"), now=NOW)
    b = svc.launch(**_launch_kw(tmp_path, "L2"), now=first.not_valid_after)       # re-minted for this one
    assert a.trust_env == first.trust_env() and a.trust_env["PIP_CERT"] == str(first.cert_path)
    assert b.trust_env == svc.ca.trust_env() and b.trust_env["SSL_CERT_FILE"] == str(svc.ca.cert_path)
    assert svc.ca is not first
    assert a.trust_env["PIP_USE_DEPRECATED"] == b.trust_env["PIP_USE_DEPRECATED"] == "legacy-certs"


def test_close_removes_every_ca_cert_the_start_minted_and_refuses_later_launches(tmp_path, monkeypatch):
    """chatdriver RULINGS_R1 CARRIED (complement 6 + deepseek 4): per-start CA certs accumulated; close() removes
    this start's, the re-minted ones too, and a launch after it is refused with its chat_tmp removed."""
    monkeypatch.setattr(lm, "start_egress", lambda **kw: SimpleNamespace(launch_id=kw["launch_id"]))
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=tmp_path / "q", now=NOW)
    first = svc.ca
    svc.launch(**_launch_kw(tmp_path, "L1"), now=first.not_valid_after)          # re-minted: two certs now
    other = tmp_path / "state" / ca.CA_DIR / "another-start.pem"                  # not this start's: left alone
    other.write_bytes(b"x")
    certs = [first.cert_path, svc.ca.cert_path]
    assert all(c.exists() for c in certs) and certs[0] != certs[1]
    assert svc.close() == () and svc.close() == ()
    print(f"RUN chatdriver-2 close: certs after close {sorted(p.name for p in other.parent.iterdir())}")
    assert not any(c.exists() for c in certs) and other.exists()
    kw = _launch_kw(tmp_path, "L2")
    with pytest.raises(RuntimeError, match="closed"):
        svc.launch(**kw)
    assert not kw["chat_tmp"].exists()


def test_a_refused_start_removes_chat_tmp_and_propagates(tmp_path, monkeypatch):
    def refuse(**kw):
        raise lm.EgressRefused("chat network refused: not set up")
    monkeypatch.setattr(lm, "start_egress", refuse)
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=tmp_path / "q",
                               srt_prefix=tmp_path / "nowhere", now=NOW)
    kw = _launch_kw(tmp_path, "L1")
    with pytest.raises(lm.EgressRefused, match="not set up"):
        svc.launch(**kw)
    assert not kw["chat_tmp"].exists()


def test_a_real_start_over_an_unset_up_prefix_is_refused_and_chat_tmp_removed(tmp_path):
    """No srt needed: srt_pin.check refuses a prefix with no stamp before anything is spawned."""
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=tmp_path / "q",
                               srt_prefix=tmp_path / "empty", node="node", now=NOW)
    kw = _launch_kw(tmp_path, "L1")
    try:
        svc.launch(**kw)
    except lm.EgressRefused as exc:
        print(f"RUN chatdriver refused start: {exc}")
    else:
        pytest.fail("a launch over an unset-up prefix started")
    assert not kw["chat_tmp"].exists()


# --- 19a: the renew-due ping ----------------------------------------------------------------------------------------


def _notify_queue(tmp: Path) -> tuple[Path, Path]:
    q = tmp / "queue"
    q.mkdir(parents=True)
    got = tmp / "pinged.txt"
    script = tmp / "ping.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{got}"\n')
    script.chmod(0o755)
    (q / "claude_code.json").write_text(json.dumps({"notify_argv": [str(script)]}))
    return q, got


def _wait_lines(path: Path, n: int, timeout: float = 10.0) -> list[str]:
    end = time.monotonic() + timeout
    while True:
        lines = path.read_text().splitlines() if path.exists() else []
        if len(lines) >= n or time.monotonic() > end:
            return lines
        time.sleep(0.05)


def _pings(q: Path, n: int) -> list[tuple]:
    """The ping records, once `n` are written (each by its own detached sender child) or after 10 s."""
    db = q / "webpush" / "webpush.db"
    end = time.monotonic() + 10
    while True:
        try:
            with sqlite3.connect(db) as c:
                rows = c.execute("SELECT kind, ref, body, command FROM pings ORDER BY id").fetchall()
            if len(rows) >= n or time.monotonic() > end:
                return rows
        except sqlite3.Error:
            if time.monotonic() > end:
                return []
        time.sleep(0.05)


def _due(rid: str, host: str, days: float) -> renew.RenewDue:
    return renew.RenewDue(record_id=rid, host=host, expires_at=NOW + timedelta(days=days))


def test_the_renew_ping_reads_the_settings_of_the_harness_that_drives_the_chat(tmp_path, monkeypatch):
    """chatdriver-2 (`1025-23/chatdriver/RULINGS_OBS.md` 3): a Codex-driven chat's ping goes where `codex.json` says,
    a Claude Code chat's where `claude_code.json` says; a harness with no settings file of its own is refused."""
    q, claude_got = _notify_queue(tmp_path)
    codex_got, script = tmp_path / "codex-pinged.txt", tmp_path / "codex-ping.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{codex_got}"\n')
    script.chmod(0o755)
    (q / "codex.json").write_text(json.dumps({"notify_argv": [str(script)]}))
    calls = []
    monkeypatch.setattr(lm, "start_egress", lambda **kw: calls.append(kw) or SimpleNamespace(launch_id=kw["launch_id"]))
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=q, now=NOW)
    svc.launch(**{**_launch_kw(tmp_path, "L1"), "harness": "codex"})
    svc.launch(**{**_launch_kw(tmp_path, "L2"), "harness": "claude-code"})
    calls[0]["on_renew_due"]("L1", [_due("k-codex", "a.example", 1)])
    calls[1]["on_renew_due"]("L2", [_due("k-claude", "b.example", 1)])
    codex_lines, claude_lines = _wait_lines(codex_got, 1), _wait_lines(claude_got, 1)
    print(f"RUN chatdriver-2 3: codex.json command got {codex_lines}; claude_code.json command got {claude_lines}")
    assert len(codex_lines) == 1 and "k-codex" in codex_lines[0]
    assert len(claude_lines) == 1 and "k-claude" in claude_lines[0]
    kw = {**_launch_kw(tmp_path, "L3"), "harness": "openhands"}
    with pytest.raises(ValueError, match="openhands"):
        svc.launch(**kw)
    assert not kw["chat_tmp"].exists() and len(calls) == 2


def test_renew_due_pings_through_the_notify_path_once_per_record_and_expiry(tmp_path):
    q, got = _notify_queue(tmp_path)
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=q, now=NOW)
    svc.renew_due("L1", [_due("k-soon", "a.example", 1), _due("k-edge", "c.example", 3)], harness="claude-code")
    svc.renew_due("L2", [_due("k-soon", "a.example", 1)], harness="claude-code")  # a later launch: already pinged
    lines = _wait_lines(got, 2)
    time.sleep(0.3)
    lines = got.read_text().splitlines()
    print(f"RUN chatdriver 19a ping lines: {lines}")
    # each ping's notify command is its own detached child: they land in either order
    assert sorted(lines) == sorted([cd.renew_message(_due("k-soon", "a.example", 1)),
                                    cd.renew_message(_due("k-edge", "c.example", 3))])
    assert ("levain: the chat key for a.example (record k-soon) expires 2026-10-26 12:00 UTC; renew it with "
            "`levain chat key setup --replace`.") in lines
    rows = _pings(q, 2)
    print(f"RUN chatdriver 19a ping records: {rows}")
    assert sorted((k, r) for k, r, _b, _c in rows) == [("renew", "renew:k-edge"), ("renew", "renew:k-soon")]
    svc.renew_due("L3", [_due("k-soon", "a.example", 30)], harness="claude-code")  # renewed: a new expiry is a new ping
    assert _wait_lines(got, 3)[-1].startswith("levain: the chat key for a.example (record k-soon) expires 2026-11-24")


def test_a_renew_ping_with_nowhere_to_go_is_not_counted(tmp_path):
    q = tmp_path / "queue"
    q.mkdir()
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=q, now=NOW)
    svc.renew_due("L1", [_due("k-soon", "a.example", 1)], harness="claude-code")  # no notify command, no phone
    assert not (q / "webpush").exists()
    q2, got = _notify_queue(tmp_path / "later")
    svc.queue_dir = q2                                                # configured since: the next launch pings
    svc.renew_due("L2", [_due("k-soon", "a.example", 1)], harness="claude-code")
    assert len(_wait_lines(got, 1)) == 1


# --- the real path: the glue over srt 0.0.81 -------------------------------------------------------------------------


def _authority(**expiries: timedelta) -> SimpleNamespace:
    return SimpleNamespace(policy=SimpleNamespace(grants={rid: {"expires_at": utc_stamp(NOW + d)}
                                                          for rid, d in expiries.items()}))


@pytest.mark.chat_egress_srt
def test_run_two_real_launches_on_one_service(tmp_path, monkeypatch):
    from levain.coordinator.envelopes import EnvelopeStore

    from .decider_rig import Desk, Live, NoStore
    from .fixtures import env as envmod
    from .test_launch import _node_children, real_launch

    monkeypatch.delenv(lm.TEST_SEAMS_ENV, raising=False)
    store = EnvelopeStore(tmp_path / "dispatch")
    q, got = _notify_queue(tmp_path)
    route = keys.RouteSnapshot(record_id="k-soon", canonical_host="a.example", credential_source="svc",
                               declared_repos=frozenset(), attach=True, credential_account="acct", incarnation="1:x")
    routes = keys.RouteSet.of([route])
    minted = []
    real_mint = ca.new_egress_ca
    monkeypatch.setattr(ca, "new_egress_ca", lambda *a, **k: minted.append(1) or real_mint(*a, **k))
    pems = []
    real_start = lm.start_egress
    monkeypatch.setattr(lm, "start_egress", lambda **kw: pems.append(kw["ca_pem"]) or real_start(**kw))
    before = {p.pid for p in _node_children()}
    spawned = _record_spawns(monkeypatch)
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=store, queue_dir=q, node=envmod.node_bin(),
                               srt_prefix=envmod.srt_prefix(), now=NOW)
    ends = []
    for n in (1, 2):
        lid = real_launch(store) if n == 1 else _second_launch(store)
        t = _chat_tmp(tmp_path / f"run{n}")
        launch = svc.launch(launch_id=lid, chat_tmp=t, events=tmp_path / f"events{n}.jsonl", routes=routes,
                            liveness=Live(), credentials=keys.LaunchCredentials(NoStore()), cards=Desk(),
                            mode=dm.Fixed(classify.GATED), authority=_authority(**{"k-soon": timedelta(days=2)}),
                            harness="claude-code",
                            limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0), now=NOW)
        proc = _cli(t)
        launch.attach_cli(proc)
        try:
            out = launch.end()
        finally:
            _kill_group(proc)
        ends.append((lid, launch.egress.renew_due, out, _members(proc.pid), t.exists()))
        print(f"RUN chatdriver real launch {n}: id {lid}; renew_due {launch.egress.renew_due}; end {out}; "
              f"group members after {_members(proc.pid)}; chat_tmp exists {t.exists()}")
    lines = _wait_lines(got, 1)
    time.sleep(0.3)
    lines = got.read_text().splitlines()
    print(f"RUN chatdriver real: CA minted {len(minted)}x; ca_pem identical across launches "
          f"{len(pems) == 2 and pems[0] == pems[1]}; CA fingerprint {svc.ca.fingerprint_sha256}; ping lines {lines}")
    assert minted == [1] and len(pems) == 2 and pems[0] == pems[1] == svc.ca.glue_pem()
    for lid, due, out, members, exists in ends:
        assert [d.record_id for d in due] == ["k-soon"]                   # the callback fired on both launches
        assert out.cli == cd.CliEnd(exit_code=-signal.SIGTERM, group_gone=True, reaped=True) and members == [] and not exists
        assert out.egress.glue_exit == 0 and out.egress.final_flush is not None and out.failures == ()
        assert out.tmp_leftovers == ()
    assert lines == [cd.renew_message(renew.RenewDue("k-soon", "a.example", NOW + timedelta(days=2)))]   # pinged once
    assert len(_only_the_ping_is_left(spawned, before, tmp_path)) == 2 and dm.service_threads() == []


def _second_launch(store) -> str:
    """A second launch in the same dispatch store, through the coordinator's own path."""
    from levain.coordinator.store import Part

    token = store.coord_take()
    d = store.accept_drop("chat two", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "chat two")])[0]
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id="sess-chat-2")
    assert why is None and got.state == "active"
    return ln.launch_id


def test_a_renew_due_after_the_service_is_closed_sends_no_ping(tmp_path, caplog):
    """Verify r2 item b (deepseek MED 2): `close()` says every launch has ended, so a renew-due tick landing after it
    is logged and sends nothing, and the record stays un-pinged."""
    q, got = _notify_queue(tmp_path)
    svc = cd.ChatEgressService(state_dir=tmp_path / "state", store=_store(), queue_dir=q, now=NOW)
    svc.close()
    with caplog.at_level("INFO", logger="levain.chat_driver"):
        svc.renew_due("L1", [_due("k-soon", "a.example", 1)], harness="claude-code")
    time.sleep(0.3)
    print(f"RUN fix2 b: lines after close {got.read_text().splitlines() if got.exists() else []}; "
          f"pinged {svc._pinged}; log {[r.getMessage() for r in caplog.records]}")
    assert not got.exists() and not _pings(q, 1) and not svc._pinged
    assert any("closed" in r.getMessage() for r in caplog.records)
