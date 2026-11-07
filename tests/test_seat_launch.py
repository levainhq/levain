"""The seat launcher (PATHZERO Z5, L-F): ``launch_seat`` starts claude from a prepared plan in its own process group and
records it in the mint chain BEFORE it may run; ``stop_seat`` signals that group only after checking the process's own
start time; a revoke of the egress grant stops the seats holding it; the trust of the minted clone is seeded in the seat's
HOME and nowhere else. Nothing here runs a real claude: the seat binary is a stub script that records what it was given
and sleeps (its child too), on the rig of ``test_seat_lease``."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from levain import seat_lease as sl  # noqa: E402
from levain import seat_push as sp  # noqa: E402
from levain.seat_profile import SeatRefusal  # noqa: E402
from tests.test_seat_lease import _git, rig  # noqa: E402,F401  (rig is a fixture)
from tests.test_seat_push import eg  # noqa: E402,F401  (eg is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")

_STUB = """#!/bin/sh
out='{rec}'
mkdir -p "$out"
printf '%s\\n' "$@" > "$out/argv"
env > "$out/env"
pwd > "$out/cwd"
echo $$ > "$out/pid"
/bin/sleep 300 &
echo $! > "$out/child"
wait
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:        # a zombie of ours counts as gone
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    return sl._proc_start_ns(pid) is not None and subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] != "Z"


def _wait(cond, secs=10.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Seats:
    """The rig plus a stub claude that records, a fake seat HOME and the launches made (all killed at teardown)."""

    def __init__(self, rig, tmp_path):
        self.rig, self.tmp = rig, tmp_path
        self.rec = tmp_path / "rec"
        stub = tmp_path / "bin" / "claude-stub"
        stub.write_text(_STUB.format(rec=self.rec))
        stub.chmod(0o755)
        self.claude = str(stub)
        self.home = tmp_path / "seathome"
        self.home.mkdir(mode=0o700)
        self.started: list[dict] = []

    def environ(self, home=None):
        return {**os.environ, "HOME": str(home or self.home)}

    def plan(self, lane="lane-a", *, home=None, **kw):
        return sl.prepare_launch(lane, str(self.rig.src), self.rig.sha, claude_bin=self.claude, python=sys.executable,
                                 cockpit_root=self.rig.world.root, install=self.tmp, managed=(),
                                 now=self.rig.world.clock.now + timedelta(minutes=1), environ=self.environ(home), **kw)

    def go(self, lane="lane-a", **kw):
        launch_kw = {k: kw.pop(k) for k in ("prompt", "model", "headless") if k in kw}
        plan = self.plan(lane, **kw)
        info = sl.launch_seat(plan, **launch_kw)
        self.started.append(info)
        return plan, info

    def recorded(self, name):
        assert _wait(lambda: (self.rec / name).exists() and (self.rec / name).read_text().strip()), f"stub wrote no {name}"
        return (self.rec / name).read_text()


@pytest.fixture
def seats(rig, tmp_path):
    s = Seats(rig, tmp_path)
    rig.sign_lease()
    yield s
    for info in s.started:
        try:
            os.killpg(info["pgid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if (s.rec / "child").exists():
        try:
            os.kill(int((s.rec / "child").read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def _rows(rig):
    return sl.read_mints(rig.key_dir)


# --- launch -----------------------------------------------------------------------------------------------------

def test_a_launch_runs_claude_in_the_clone_in_its_own_group_and_the_row_records_it(seats, rig):
    plan, info = seats.go("lane-a", prompt="do the thing", model="sonnet")
    start = [r for r in _rows(rig) if r.get("ev") == "start"]
    assert len(start) == 1
    row = start[0]
    assert (row["pid"], row["pgid"], row["lane_key"], row["mint_hash"]) == (info["pid"], info["pgid"], "lane-a", plan.mint["hash"])
    assert row["pid"] == row["pgid"] != os.getpgid(0), "the seat is the leader of its own group"
    assert row["started_at_ns"] == sl._proc_start_ns(info["pid"]), "the row holds the process's own start time"
    assert row["claude"] == plan.mint["claude"] and row["egress_grant"] is None
    assert seats.recorded("cwd").strip() == str(plan.profile.clone)
    argv = seats.recorded("argv").splitlines()
    assert argv == [*plan.profile.argv(model="sonnet")[1:], "do the thing"]
    assert dict(l.split("=", 1) for l in seats.recorded("env").splitlines() if "=" in l)["HOME"] == str(seats.home)
    assert int(seats.recorded("pid")) == info["pid"], "exec kept the recorded pid"
    assert Path(info["log"]) == plan.profile.return_dir / "seat-launch.log" and Path(info["log"]).stat().st_mode & 0o777 == 0o600


def test_the_row_is_appended_before_the_seat_can_run(seats, rig, monkeypatch):
    real = sl.append_mint
    seen = {}

    def spy(key_dir, row):
        if row.get("ev") == "start":
            time.sleep(0.3)                    # a seat that was already running would have written its pid by now
            seen["ran_before_row"] = (seats.rec / "pid").exists()
            seen["stopped"] = subprocess.run(["ps", "-o", "stat=", "-p", str(row["pid"])], capture_output=True,
                                             text=True).stdout.strip()[:1] == "T"
        return real(key_dir, row)
    monkeypatch.setattr(sl, "append_mint", spy)
    seats.go()
    assert seen == {"ran_before_row": False, "stopped": True}
    seats.recorded("pid")                      # and it runs once the row is down


def test_a_crash_between_the_spawn_and_the_append_leaves_no_live_seat_and_no_roots(seats, rig, monkeypatch):
    real = sl.append_mint
    pids = []

    def boom(key_dir, row):
        if row.get("ev") == "start":
            pids.append(row["pid"])
            raise OSError("disk full")
        return real(key_dir, row)
    monkeypatch.setattr(sl, "append_mint", boom)
    with pytest.raises(OSError):
        seats.go("lane-x")
    assert pids and _wait(lambda: not _alive(pids[0])), "the stopped seat was killed"
    time.sleep(0.2)
    assert not (seats.rec / "pid").exists(), "the seat never ran"
    assert not (rig.lanes / "lane-x").exists() and not (rig.returns / "lane-x").exists()
    assert not [r for r in _rows(rig) if r.get("ev") == "start"]


def test_a_launch_under_an_expired_lease_refuses_and_mints_and_starts_nothing(seats, rig):
    with pytest.raises(sl.SeatLaunchRefused):
        sl.prepare_launch("lane-e", str(rig.src), rig.sha, claude_bin=seats.claude, python=sys.executable,
                          cockpit_root=rig.world.root, install=rig.tmp, managed=(), environ=seats.environ(),
                          now=rig.world.clock.now + timedelta(days=15))
    assert _rows(rig) == [] and list(rig.lanes.iterdir()) == [] and not seats.rec.exists()


def test_a_claude_that_exits_at_once_is_still_recorded_and_stop_calls_it_gone(seats, rig, tmp_path):
    bad = tmp_path / "bin" / "claude-bad"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    seats.claude = str(bad)
    # the wrapper stops BEFORE exec, so an immediate-exit claude is still recorded first
    info = seats.go("lane-b")[1]
    assert _wait(lambda: not _alive(info["pid"]))
    res = sl.stop_seat(rig.key_dir, "lane-b")
    assert res.status == "gone"


# --- stop -------------------------------------------------------------------------------------------------------

def test_stop_kills_the_group_and_its_children_and_leaves_an_unrelated_process_alone(seats, rig):
    _plan, info = seats.go()
    child = int(seats.recorded("child"))
    other = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
    try:
        assert _alive(child) and _alive(info["pid"])
        res = sl.stop_seat(rig.key_dir, "lane-a", grace=5)
        assert res.status == "stopped" and res.pgid == info["pgid"]
        assert "SIGKILL" not in res.detail, "one SIGTERM to the group was enough: the children got it too"
        assert _wait(lambda: not _alive(info["pid"])) and _wait(lambda: not _alive(child))
        assert other.poll() is None, "an unrelated process was not signalled"
    finally:
        other.kill()
        other.wait()


def test_stop_of_a_seat_already_gone_is_a_success_that_says_so(seats, rig):
    _plan, info = seats.go()
    seats.recorded("child")
    os.killpg(info["pgid"], signal.SIGKILL)
    assert _wait(lambda: not _alive(info["pid"]))
    res = sl.stop_seat(rig.key_dir, "lane-a")
    assert res.status == "gone" and "already exited" in res.detail


def test_stop_refuses_a_lane_with_no_row(seats, rig):
    with pytest.raises(sl.SeatLaunchRefused, match="no launched seat is recorded"):
        sl.stop_seat(rig.key_dir, "lane-nope")


def test_a_reused_pid_is_never_signalled(seats, rig, monkeypatch):
    _plan, info = seats.go()
    seats.recorded("child")
    real = sl._proc_start_ns
    monkeypatch.setattr(sl, "_proc_start_ns", lambda pid: real(pid) + 5_000_000_000 if pid == info["pid"] else real(pid))
    killed = []
    monkeypatch.setattr(sl.os, "killpg", lambda pg, sig: killed.append((pg, sig)))
    with pytest.raises(sl.SeatLaunchRefused, match="reused"):
        sl.stop_seat(rig.key_dir, "lane-a")
    assert killed == []
    monkeypatch.undo()
    assert _alive(info["pid"])


def test_stop_refuses_when_the_pid_left_the_recorded_group(seats, rig, monkeypatch):
    _plan, info = seats.go()
    seats.recorded("child")
    monkeypatch.setattr(sl.os, "getpgid", lambda pid: info["pgid"] + 7)
    killed = []
    monkeypatch.setattr(sl.os, "killpg", lambda pg, sig: killed.append(sig))
    with pytest.raises(sl.SeatLaunchRefused, match="no longer in the recorded process group"):
        sl.stop_seat(rig.key_dir, "lane-a")
    assert killed == []


def test_stop_uses_the_newest_start_row_of_the_lane(seats, rig):
    _plan, info = seats.go()
    rows = [r for r in _rows(rig) if r.get("ev") == "start"]
    stale = {**{k: v for k, v in rows[0].items() if k not in ("hash", "sig", "prev")}, "pid": 999999, "pgid": 999999,
             "started_at_ns": 1}
    sl.append_mint(rig.key_dir, stale)       # a LATER row for the same lane wins: it names a pid that is not there
    assert sl.stop_seat(rig.key_dir, "lane-a").status == "gone"
    assert _alive(info["pid"]), "the older row's seat was not the one stopped"


# --- revoke kills the seat --------------------------------------------------------------------------------------

def test_revoke_stops_only_the_seat_holding_that_grant(seats, rig, eg):
    _p, no_grant = seats.go("lane-a")                       # launched before any egress grant
    eg.sign()
    grant = sp.live_egress(sl._replay(rig.world.root), rig.world.clock.now + timedelta(minutes=5))
    plan = sl.prepare_launch("lane-b", str(rig.src), rig.sha, claude_bin=seats.claude, python=sys.executable,
                             cockpit_root=rig.world.root, install=rig.tmp, managed=(),
                             now=rig.world.clock.now + timedelta(minutes=5), environ=seats.environ())
    held = sl.launch_seat(plan)
    seats.started.append(held)
    assert plan.egress_grant == grant["id"]
    assert [r["egress_grant"] for r in _rows(rig) if r.get("ev") == "start"] == [None, grant["id"]]
    seats.recorded("child")
    out = sl.stop_seats_for_grant(rig.key_dir, grant["id"], grace=5)
    assert [(r.lane, r.status) for r in out] == [("lane-b", "stopped")]
    assert _wait(lambda: not _alive(held["pid"]))
    assert _alive(no_grant["pid"]), "a seat launched with no grant is untouched"
    assert sl.stop_seats_for_grant(rig.key_dir, "sg-other") == []


def test_the_egress_revoke_command_stops_the_seats_after_the_revoke_row(seats, rig, eg, monkeypatch, capsys):
    from levain import cli
    eg.sign()
    grant = sp.live_egress(sl._replay(rig.world.root), rig.world.clock.now + timedelta(minutes=5))
    plan = sl.prepare_launch("lane-c", str(rig.src), rig.sha, claude_bin=seats.claude, python=sys.executable,
                             cockpit_root=rig.world.root, install=rig.tmp, managed=(),
                             now=rig.world.clock.now + timedelta(minutes=5), environ=seats.environ())
    held = sl.launch_seat(plan)
    seats.started.append(held)
    seats.recorded("child")
    order = []
    import levain.cockpit.keys as keys
    monkeypatch.setattr(keys, "ssh_signer", lambda path: ("fp", lambda m: "sig"))
    monkeypatch.setattr(sp, "revoke_egress", lambda root, gid, **k: (order.append("revoke") or (7, None)))
    real_stop = sl.stop_seats_for_grant
    monkeypatch.setattr(sl, "stop_seats_for_grant", lambda *a, **k: (order.append("stop") or real_stop(*a, **{**k, "grace": 5})))
    ns = argparse.Namespace(seat_cmd="egress", seat_egress_cmd="revoke", grant_id=grant["id"], signing_key=Path("k"),
                            path=rig.tmp, data_root=rig.world.root)
    assert cli._cmd_seat(ns) == 0
    assert order == ["revoke", "stop"] and _wait(lambda: not _alive(held["pid"]))
    out = capsys.readouterr().out
    assert f"revoked: {grant['id']}" in out and "seat lane-c: stopped" in out


# --- pre-trust --------------------------------------------------------------------------------------------------

def _digest(p: Path) -> tuple[str, int]:
    return hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns


def test_the_trust_is_seeded_in_the_seat_home_only_and_the_operators_file_is_untouched(seats, rig, tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    operator.mkdir()
    real = operator / ".claude.json"
    real.write_text(json.dumps({"projects": {"/x": {"hasTrustDialogAccepted": False}}, "userID": "me"}))
    monkeypatch.setenv("HOME", str(operator))
    before = _digest(real)
    (seats.home / ".claude.json").write_text(json.dumps({"userID": "seat", "projects": {"/old": {"k": 1}}}))
    plan, _info = seats.go()
    assert _digest(real) == before and sorted(p.name for p in operator.iterdir()) == [".claude.json"]
    seat_file = seats.home / ".claude.json"
    data = json.loads(seat_file.read_text())
    assert data["projects"][str(plan.profile.clone)] == {"hasTrustDialogAccepted": True}
    assert data["userID"] == "seat" and data["projects"]["/old"] == {"k": 1}, "the rest of the file is kept"
    assert data["hasCompletedOnboarding"] is True and seat_file.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in seats.home.iterdir()) == [".claude.json"], "no temp file is left behind"


def test_seed_trust_into_a_fresh_home_writes_the_first_run_state_and_refuses_a_file_it_cannot_read(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    sl.seed_trust(h, "/lane/x")
    assert json.loads((h / ".claude.json").read_text()) == {
        "hasCompletedOnboarding": True, "theme": "dark", "projects": {"/lane/x": {"hasTrustDialogAccepted": True}}}
    junk = tmp_path / "j"
    junk.mkdir()
    (junk / ".claude.json").write_text("{not json")
    with pytest.raises(sl.SeatLaunchRefused, match="not touched|cannot be read"):
        sl.seed_trust(junk, "/lane/x")
    assert (junk / ".claude.json").read_text() == "{not json"


def test_a_failed_seed_launches_nothing_and_removes_the_roots(seats, rig, tmp_path):
    (seats.home / ".claude.json").write_text("[]")
    with pytest.raises(sl.SeatLaunchRefused):
        seats.go("lane-s")
    assert not seats.rec.exists() and not (rig.lanes / "lane-s").exists()


# --- the fallback tool set and the digest -----------------------------------------------------------------------

def test_a_narrowed_tool_set_reaches_the_seats_argv_and_an_unadmitted_tool_refuses(seats, rig):
    plan, _info = seats.go("lane-t", tools=("Bash", "Read", "Glob", "Grep"))
    assert seats.recorded("argv").splitlines()[seats.recorded("argv").splitlines().index("--tools") + 1] == "Bash,Read,Glob,Grep"
    assert plan.profile.tools == ("Bash", "Read", "Glob", "Grep")
    with pytest.raises(SeatRefusal, match="subset of the admitted seat tools"):
        seats.plan("lane-u", tools=("Bash", "NotebookEdit"))
    assert not (rig.lanes / "lane-u").exists()


def test_the_cli_validates_tools_as_the_canary_does(seats, rig, capsys):
    from levain import cli
    ns = argparse.Namespace(lane_key="lane-v", remote=str(rig.src), base=rig.sha, claude=seats.claude, python=sys.executable,
                            tools="Bash,Edit", model=None, prompt=None, headless=False, seat_home=None, dry_run=False,
                            path=rig.tmp)
    assert cli._cmd_seat_launch(ns, rig.world.root) == 1       # Edit alone is half a probe's admission
    assert "seat launch refused" in capsys.readouterr().err and not (rig.lanes / "lane-v").exists()


def test_cli_launch_and_stop_print_the_lines_flow_parses(seats, rig, capsys, monkeypatch):
    from levain import cli
    monkeypatch.setattr(sl, "_proc_start_ns", sl._proc_start_ns)
    ns = argparse.Namespace(lane_key="lane-w", remote=str(rig.src), base=rig.sha, claude=seats.claude, python=sys.executable,
                            tools=None, model=None, prompt=None, headless=False, seat_home=seats.home, dry_run=False,
                            path=rig.tmp)
    # the rig's lease clock is the world's: run prepare/launch through the CLI with the rig's `now`
    real = sl.prepare_launch
    monkeypatch.setattr(sl, "prepare_launch", lambda *a, **k: real(*a, now=rig.world.clock.now + timedelta(minutes=1), managed=(), **k))
    assert cli._cmd_seat_launch(ns, rig.world.root) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "seat launched: lane lane-w"
    kv = {l.split()[0]: l.split(None, 1)[1] for l in out[1:]}
    assert set(kv) == {"pid", "pgid", "clone", "return", "log", "digest", "stop"} and kv["stop"] == "levain seat stop lane-w"
    pid = int(kv["pid"])
    seats.started.append({"pgid": int(kv["pgid"])})
    seats.recorded("child")
    sn = argparse.Namespace(seat_cmd="stop", lane_key="lane-w", grace=5.0, path=rig.tmp, data_root=rig.world.root)
    assert cli._cmd_seat(sn) == 0
    assert capsys.readouterr().out.startswith("seat lane-w: stopped") and _wait(lambda: not _alive(pid))


def test_profile_digest_differs_between_two_launches_of_one_shape_and_a_path_normalised_one_does_not(seats, rig):
    """MEASUREMENT for the head (L-F item F): the digest binds the clone and RETURN paths, so a verdict bound to it never
    matches a later launch. What stays equal across launches of one shape is the digest with the per-launch paths
    replaced by placeholders (same argv shape, tools, env, claude identity, key dir)."""
    a = seats.plan("lane-d1").profile
    b = seats.plan("lane-d2").profile
    assert a.digest() != b.digest()

    def shape(p):
        blob = json.dumps([p.argv(), p.argv(headless=True), sorted(p.env.items())], sort_keys=True)
        return blob.replace(str(p.clone), "<CLONE>").replace(str(p.return_dir), "<RETURN>")
    assert shape(a) == shape(b)
