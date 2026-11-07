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
import stat
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
        launch_kw = {"prompt": "work", **{k: kw.pop(k) for k in ("prompt", "model") if k in kw}}
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
    assert argv == [*plan.profile.argv(headless=True, model="sonnet")[1:], "do the thing"]
    assert argv[0] == "-p", "a launch is headless: the seat has no terminal"
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
    assert [s for _pg, s in killed if s != 0] == [], "only the existence probe (signal 0, which sends nothing)"
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
    held = sl.launch_seat(plan, prompt="work")
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
    held = sl.launch_seat(plan, prompt="work")
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
    assert sorted(p.name for p in seats.home.iterdir()) == [".claude.json", ".claude.json.levain-lock"], "no temp file is left behind"


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
                            tools="Bash,Edit", model=None, prompt="work", headless=False, seat_home=None, dry_run=False,
                            path=rig.tmp)
    assert cli._cmd_seat_launch(ns, rig.world.root) == 1       # Edit alone is half a probe's admission
    assert "seat launch refused" in capsys.readouterr().err and not (rig.lanes / "lane-v").exists()


def test_cli_launch_and_stop_print_the_lines_flow_parses(seats, rig, capsys, monkeypatch):
    from levain import cli
    monkeypatch.setattr(sl, "_proc_start_ns", sl._proc_start_ns)
    ns = argparse.Namespace(lane_key="lane-w", remote=str(rig.src), base=rig.sha, claude=seats.claude, python=sys.executable,
                            tools=None, model=None, prompt="work", headless=False, seat_home=seats.home, dry_run=False,
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


# --- L3 lf r1 fixes ---------------------------------------------------------------------------------------------

def _ns(rig, seats, lane, **kw):
    base = dict(lane_key=lane, remote=str(rig.src), base=rig.sha, claude=seats.claude, python=sys.executable, tools=None,
                model=None, prompt="work", headless=False, seat_home=seats.home, dry_run=False, path=rig.tmp)
    return argparse.Namespace(**{**base, **kw})


def _rig_clock(rig, monkeypatch):
    real = sl.prepare_launch
    monkeypatch.setattr(sl, "prepare_launch", lambda *a, **k: real(*a, now=rig.world.clock.now + timedelta(minutes=1), managed=(), **k))


def test_fix1_a_cli_launch_with_no_prompt_refuses_naming_it_and_mints_nothing(seats, rig, capsys, monkeypatch):
    from levain import cli
    _rig_clock(rig, monkeypatch)
    assert cli._cmd_seat_launch(_ns(rig, seats, "lane-np", prompt=None), rig.world.root) == 1
    assert "--prompt" in capsys.readouterr().err
    assert _rows(rig) == [] and not (rig.lanes / "lane-np").exists() and not seats.rec.exists()
    # --headless is an accepted no-op: the launch is headless either way
    assert cli._cmd_seat_launch(_ns(rig, seats, "lane-hl", headless=True), rig.world.root) == 0
    seats.started.append({"pgid": int(seats.recorded("pid"))})
    assert seats.recorded("argv").splitlines()[0] == "-p"


def test_fix1_launch_seat_itself_refuses_a_missing_prompt_and_removes_the_roots(seats, rig):
    plan = seats.plan("lane-np2")
    with pytest.raises(sl.SeatLaunchRefused, match="--prompt"):
        sl.launch_seat(plan, prompt="  ")
    assert not (rig.lanes / "lane-np2").exists() and not seats.rec.exists()


def _revoke_egress(rig, gid):
    fp, sign = rig.world.keys["laptop"]
    rig.world.clock.now += timedelta(minutes=1)
    sp.revoke_egress(rig.world.root, gid, signer_fp=fp, sign=sign, clock=rig.world.clock)


def test_fix2_a_revoke_between_prepare_and_release_kills_the_stopped_seat_and_refuses(seats, rig, eg, monkeypatch):
    eg.sign()
    grant = sp.live_egress(sl._replay(rig.world.root), rig.world.clock.now + timedelta(minutes=5))
    plan = sl.prepare_launch("lane-rr", str(rig.src), rig.sha, claude_bin=seats.claude, python=sys.executable,
                             cockpit_root=rig.world.root, install=rig.tmp, managed=(),
                             now=rig.world.clock.now + timedelta(minutes=5), environ=seats.environ())
    assert plan.egress_grant == grant["id"]
    real = sl.append_mint
    pids = []

    def revoke_after_the_start_row(key_dir, row):
        out = real(key_dir, row)
        if row.get("ev") == "start":
            pids.append(row["pid"])
            _revoke_egress(rig, grant["id"])          # lands after the row, before the continue
        return out
    monkeypatch.setattr(sl, "append_mint", revoke_after_the_start_row)
    with pytest.raises(sl.SeatLaunchRefused, match="no longer live"):
        sl.launch_seat(plan, prompt="work")
    assert pids and _wait(lambda: not _alive(pids[0])), "the stopped seat was killed"
    time.sleep(0.2)
    assert not (seats.rec / "pid").exists(), "the seat never ran"
    assert not (rig.lanes / "lane-rr").exists() and not (rig.returns / "lane-rr").exists()
    evs = [r.get("ev") for r in _rows(rig) if r.get("ev")]
    assert evs == ["start", "aborted"]
    with pytest.raises(sl.SeatLaunchRefused, match="no launched seat is recorded"):
        sl.stop_seat(rig.key_dir, "lane-rr")
    assert sl.stop_seats_for_grant(rig.key_dir, grant["id"]) == []


def test_fix2_a_lease_revoked_before_the_release_refuses_too(seats, rig, monkeypatch):
    from levain.consent.grants import revoke_grant
    plan = seats.plan("lane-rl")
    lease_id = plan.mint["lease_id"]
    real = sl.append_mint

    def revoke(key_dir, row):
        out = real(key_dir, row)
        if row.get("ev") == "start":
            fp, sign = rig.world.keys["laptop"]
            rig.world.clock.now += timedelta(minutes=1)
            revoke_grant(rig.world.root, lease_id, signer_fp=fp, sign=sign, clock=rig.world.clock)
        return out
    monkeypatch.setattr(sl, "append_mint", revoke)
    with pytest.raises(sl.SeatLaunchRefused, match="lease is no longer live"):
        sl.launch_seat(plan, prompt="work")
    time.sleep(0.2)
    assert not (seats.rec / "pid").exists() and not (rig.lanes / "lane-rl").exists()


def test_fix3_two_seeds_into_one_home_at_once_keep_both_entries(tmp_path, monkeypatch):
    import threading
    h = tmp_path / "h"
    h.mkdir()
    (h / ".claude.json").write_text(json.dumps({"userID": "seat"}))
    real = sl._read_regular

    def slow(p):                                  # both readers would see the same old file without the lock
        b = real(p)
        time.sleep(0.3)
        return b
    monkeypatch.setattr(sl, "_read_regular", slow)
    errors = []

    def seed(clone):
        try:
            sl.seed_trust(h, clone)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=seed, args=(f"/lane/{n}",)) for n in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    data = json.loads((h / ".claude.json").read_text())
    assert set(data["projects"]) == {"/lane/a", "/lane/b"} and data["userID"] == "seat"
    assert (h / ".claude.json.levain-lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("body", [b"", b"[]", b"{not json", b'{"projects": []}', b'{"a": 1'])
def test_fix3_a_file_that_is_not_a_json_object_is_refused_and_left_byte_identical(tmp_path, body):
    h = tmp_path / "h"
    h.mkdir()
    (h / ".claude.json").write_bytes(body)
    with pytest.raises(sl.SeatLaunchRefused, match="not touched"):
        sl.seed_trust(h, "/lane/x")
    assert (h / ".claude.json").read_bytes() == body
    assert sorted(p.name for p in h.iterdir()) == [".claude.json", ".claude.json.levain-lock"]


def test_fix3_a_fifo_or_a_linked_lock_is_refused_without_blocking(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    os.mkfifo(h / ".claude.json")
    with pytest.raises(sl.SeatLaunchRefused, match="not touched"):
        sl.seed_trust(h, "/lane/x")
    g = tmp_path / "g"
    g.mkdir()
    (g / ".claude.json.levain-lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(sl.SeatLaunchRefused, match="trust lock"):
        sl.seed_trust(g, "/lane/x")
    assert not (tmp_path / "elsewhere").exists()


def test_mut_a_fifo_claude_json_never_blocks_and_a_fifo_lock_is_refused_as_not_regular(tmp_path):
    def boom(*_a):
        raise AssertionError("seed_trust blocked on a FIFO")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(5)
    try:
        h = tmp_path / "h"
        h.mkdir()
        os.mkfifo(h / ".claude.json")
        with pytest.raises(sl.SeatLaunchRefused, match="cannot be read as JSON"):
            sl.seed_trust(h, "/lane/x")
        g = tmp_path / "g"
        g.mkdir()
        os.mkfifo(g / ".claude.json.levain-lock")
        with pytest.raises(sl.SeatLaunchRefused, match="not a regular file"):
            sl.seed_trust(g, "/lane/x")
        assert not (g / ".claude.json").exists()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_mut_a_fifo_that_holds_valid_json_is_still_refused_not_replaced(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    os.mkfifo(h / ".claude.json")
    keep = os.open(h / ".claude.json", os.O_RDWR)           # both ends held: the content is readable without a second process
    try:
        os.write(keep, b'{"projects": {}}')
        with pytest.raises(sl.SeatLaunchRefused, match="cannot be read as JSON"):
            sl.seed_trust(h, "/lane/x")
        assert stat.S_ISFIFO(os.lstat(h / ".claude.json").st_mode), "the FIFO was not replaced"
    finally:
        os.close(keep)


def test_fix2_4_eperm_with_live_members_refuses_even_when_the_leader_is_gone(monkeypatch):
    """L3 lf r2 codex / deepseek MED: a uid-changed descendant may be what remains of the seat, so EPERM on a group that
    lists a live member is never 'gone', whatever became of the leader; zombie-only (nothing live) is gone."""
    monkeypatch.setattr(sl, "_live_members", lambda pg: [(4243, 0)])
    monkeypatch.setattr(sl, "_proc_start_ns", lambda pid: None)            # the leader is gone
    with pytest.raises(sl.SeatLaunchRefused, match="EPERM.*4243 \\(uid 0\\)"):
        sl._eperm("lane-x", 4242, "SIGTERM")
    monkeypatch.setattr(sl, "_live_members", lambda pg: [])
    sl._eperm("lane-x", 4242, "SIGTERM")                                    # zombie-only: returns (gone)


def test_fix4_a_dry_run_mints_nothing_and_the_real_launch_of_that_lane_then_works(seats, rig):
    import io
    out, err = io.StringIO(), io.StringIO()
    rc = sl.validate_launch("lane-dr", str(rig.src), rig.sha, claude_bin=seats.claude, python=sys.executable,
                            cockpit_root=rig.world.root, install=rig.tmp, out=out, err=err, managed=(),
                            now=rig.world.clock.now + timedelta(minutes=1), environ=seats.environ(), prompt="work")
    assert (rc, err.getvalue()) == (0, ""), err.getvalue()
    assert "nothing minted" in out.getvalue()
    argv = json.loads(out.getvalue().split("  argv    ", 1)[1])
    assert argv[1] == "-p" and argv[-1] == "work" and str(rig.lanes.resolve() / "lane-dr") in json.dumps(argv)
    assert list(rig.lanes.iterdir()) == [] and list(rig.returns.iterdir()) == [], "no root, no throwaway left"
    assert _rows(rig) == [], "no mint row"
    plan, _info = seats.go("lane-dr")
    assert Path(plan.profile.clone).is_dir()
    # and a dry run of a lane that exists, or with a bad prompt, refuses
    for kw in ({}, {"prompt": "-x"}):
        err = io.StringIO()
        assert sl.validate_launch("lane-dr" if not kw else "lane-dq", str(rig.src), rig.sha, claude_bin=seats.claude,
                                  python=sys.executable, cockpit_root=rig.world.root, install=rig.tmp, out=io.StringIO(),
                                  err=err, managed=(), now=rig.world.clock.now + timedelta(minutes=1),
                                  environ=seats.environ(), **kw) == 1
        assert "seat launch refused" in err.getvalue()


def test_fix5_the_grant_sweep_continues_past_a_seat_that_raises(seats, rig, eg, monkeypatch):
    eg.sign()
    grant = sp.live_egress(sl._replay(rig.world.root), rig.world.clock.now + timedelta(minutes=5))
    held = []
    for lane in ("lane-s1", "lane-s2"):
        plan = sl.prepare_launch(lane, str(rig.src), rig.sha, claude_bin=seats.claude, python=sys.executable,
                                 cockpit_root=rig.world.root, install=rig.tmp, managed=(),
                                 now=rig.world.clock.now + timedelta(minutes=5), environ=seats.environ())
        held.append(sl.launch_seat(plan, prompt="work"))
        seats.started.append(held[-1])
    real = sl._stop_row

    def flaky(row, **k):
        if row["lane_key"] == "lane-s1":
            raise PermissionError("operation not permitted")
        return real(row, **k)
    monkeypatch.setattr(sl, "_stop_row", flaky)
    out = sl.stop_seats_for_grant(rig.key_dir, grant["id"], grace=5)
    assert [(r.lane, r.status) for r in out] == [("lane-s1", "refused"), ("lane-s2", "stopped")]
    assert "PermissionError" in out[0].detail and _wait(lambda: not _alive(held[1]["pid"]))


def test_fix5_eperm_on_a_signal_is_gone_when_the_group_lists_nothing_live_and_refused_exactly_when_it_does(seats, rig, monkeypatch):
    _plan, info = seats.go("lane-ep")
    seats.recorded("child")

    def eperm(pg, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(sl.os, "killpg", eperm)
    with pytest.raises(sl.SeatLaunchRefused, match="EPERM.*lists live processes"):
        sl.stop_seat(rig.key_dir, "lane-ep")
    monkeypatch.setattr(sl, "_live_members", lambda pg: [])      # a zombie-only group: macOS answers EPERM
    assert sl.stop_seat(rig.key_dir, "lane-ep").status == "gone"


def test_fix5_a_group_gone_at_the_sigterm_is_gone_not_a_crash(seats, rig, monkeypatch):
    _plan, info = seats.go("lane-pl")
    seats.recorded("child")
    real = os.killpg

    def vanish(pg, sig):
        if sig == signal.SIGTERM:
            raise ProcessLookupError(3, "No such process")
        return real(pg, sig)
    monkeypatch.setattr(sl.os, "killpg", vanish)
    assert sl.stop_seat(rig.key_dir, "lane-pl").status == "gone"


def test_fix5_any_other_error_in_stop_is_a_refusal_not_a_traceback(seats, rig, monkeypatch):
    _plan, _info = seats.go("lane-te")
    monkeypatch.setattr(sl, "_proc_start_ns", lambda pid: (_ for _ in ()).throw(RuntimeError("ps exploded")))
    with pytest.raises(sl.SeatLaunchRefused, match="RuntimeError"):
        sl.stop_seat(rig.key_dir, "lane-te")
    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ps", 10)))
    try:
        with pytest.raises(sl.SeatLaunchRefused, match="ps could not read"):
            sl._proc_start_ns(os.getpid())
    finally:
        monkeypatch.undo()


def test_fix6_a_nul_prompt_leaves_no_minted_root_and_the_lane_launches_again(seats, rig, capsys, monkeypatch):
    plan = seats.plan("lane-nul")
    with pytest.raises(sl.SeatLaunchRefused, match="NUL"):
        sl.launch_seat(plan, prompt="a\0b")
    assert not (rig.lanes / "lane-nul").exists() and not (rig.returns / "lane-nul").exists()
    seats.go("lane-nul")                                           # the lane is not wedged
    from levain import cli
    _rig_clock(rig, monkeypatch)
    before = len(_rows(rig))
    assert cli._cmd_seat_launch(_ns(rig, seats, "lane-nul2", prompt="a\0b"), rig.world.root) == 1
    assert "NUL" in capsys.readouterr().err
    assert len(_rows(rig)) == before and not (rig.lanes / "lane-nul2").exists(), "the CLI checks before the mint"


@pytest.mark.parametrize("value", ["inf", "nan", "-1", "600.5", "-inf", "x"])
def test_fix7_grace_refuses_what_is_not_finite_or_out_of_range(value, capsys):
    from levain import cli
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["seat", "stop", "lane-a", "--grace", value])
    with pytest.raises(sl.SeatLaunchRefused):
        sl.check_grace(value)


def test_fix7_grace_accepts_its_range():
    from levain import cli
    assert cli.build_parser().parse_args(["seat", "stop", "lane-a", "--grace", "0"]).grace == 0.0
    assert cli.build_parser().parse_args(["seat", "stop", "lane-a", "--grace", "600"]).grace == 600.0


def _revoke_ns(rig, grant_id):
    return argparse.Namespace(seat_cmd="egress", seat_egress_cmd="revoke", grant_id=grant_id, signing_key=Path("k"),
                              path=rig.tmp, data_root=rig.world.root)


def test_fix8_revoke_that_landed_but_could_not_stop_says_so_with_its_own_exit_code(rig, monkeypatch, capsys):
    from levain import cli
    import levain.cockpit.keys as keys
    monkeypatch.setattr(keys, "ssh_signer", lambda path: ("fp", lambda m: "sig"))
    monkeypatch.setattr(sp, "revoke_egress", lambda root, gid, **k: (7, None))
    monkeypatch.setattr(sl, "stop_seats_for_grant",
                        lambda *a, **k: [sl.StopResult("lane-x", "refused", "EPERM"), sl.StopResult("lane-y", "stopped", "ok")])
    assert cli._cmd_seat(_revoke_ns(rig, "sg-1")) == cli.EXIT_REVOKED_NOT_STOPPED == 3
    cap = capsys.readouterr()
    assert "revoked: sg-1" in cap.out and "revoked, but could not stop: lane-x" in cap.err
    monkeypatch.setattr(sl, "stop_seats_for_grant", lambda *a, **k: (_ for _ in ()).throw(sl.SeatLaunchRefused("the mint chain is broken")))
    assert cli._cmd_seat(_revoke_ns(rig, "sg-1")) == 3
    assert "revoked, but could not stop" in capsys.readouterr().err
    # a revoke that did NOT land is exit 1, "refused"
    monkeypatch.setattr(sp, "revoke_egress", lambda root, gid, **k: (_ for _ in ()).throw(sp.SeatPushRefused("no such grant")))
    assert cli._cmd_seat(_revoke_ns(rig, "sg-1")) == 1
    assert "seat egress refused: no such grant" in capsys.readouterr().err


def test_fix8_an_oserror_during_the_launch_is_a_refusal_not_a_traceback(seats, rig, monkeypatch, capsys):
    from levain import cli
    _rig_clock(rig, monkeypatch)
    monkeypatch.setattr(sl, "seed_trust", lambda home, clone: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert cli._cmd_seat_launch(_ns(rig, seats, "lane-os"), rig.world.root) == 1
    assert "seat launch refused: OSError" in capsys.readouterr().err and not (rig.lanes / "lane-os").exists()


# --- L3 lf r2 fixes ---------------------------------------------------------------------------------------------

def _strip(row):
    return {k: v for k, v in row.items() if k not in ("hash", "sig", "prev")}


def test_fix2_1_an_aborted_later_launch_hides_only_its_own_row_not_the_older_running_seat(seats, rig):
    _plan, info = seats.go("lane-a")
    seats.recorded("child")
    s1 = [r for r in _rows(rig) if r.get("ev") == "start"][0]
    s2 = sl.append_mint(rig.key_dir, {**_strip(s1), "pid": 999999, "pgid": 999999, "started_at_ns": 1})
    sl.append_mint(rig.key_dir, {"v": 1, "ev": "aborted", "lane_key": "lane-a", "start_hash": s2["hash"]})
    assert sl._start_rows(_rows(rig))["lane-a"]["hash"] == s1["hash"]
    res = sl.stop_seat(rig.key_dir, "lane-a", grace=5)
    assert res.status == "stopped" and res.pgid == info["pgid"] and _wait(lambda: not _alive(info["pid"]))


def test_fix2_2_a_stale_row_whose_pid_was_reused_elsewhere_is_gone_and_a_live_group_still_refuses(seats, rig, monkeypatch):
    _plan, info = seats.go("lane-st")
    seats.recorded("child")
    os.killpg(info["pgid"], signal.SIGKILL)
    assert _wait(lambda: not _alive(info["pid"]))
    # the pid now names an unrelated process in another group: another start time, and no group of the recorded id
    real = sl._proc_start_ns
    monkeypatch.setattr(sl, "_proc_start_ns",
                        lambda pid: info["started_at_ns"] + 86_400 * 10**9 if pid == info["pid"] else real(pid))
    res = sl.stop_seat(rig.key_dir, "lane-st")
    assert res.status == "gone", "a stale row does not refuse forever"
    # a group of that id that ps still lists may be a stranger's: the exact refusal stays
    monkeypatch.setattr(sl, "_live_members", lambda pg: [(info["pid"], 501)])
    with pytest.raises(sl.SeatLaunchRefused, match="reused.*exists"):
        sl.stop_seat(rig.key_dir, "lane-st")


def test_fix2_3_a_reused_pid_that_is_an_unrelated_exited_child_of_ours_keeps_its_exit_status(seats, rig):
    seats.plan("lane-z")                        # the launcher key and a chain; no process
    child = subprocess.Popen(["/bin/sh", "-c", "exit 7"])
    try:
        assert _wait(lambda: subprocess.run(["ps", "-o", "stat=", "-p", str(child.pid)], capture_output=True,
                                            text=True).stdout.strip()[:1] in ("Z", ""))
        sl.append_mint(rig.key_dir, {"v": 1, "ev": "start", "lane_key": "lane-r", "pid": child.pid, "pgid": child.pid,
                                     "started_at_ns": 1})
        assert sl.stop_seat(rig.key_dir, "lane-r").status == "gone"
        done, status = os.waitpid(child.pid, os.WNOHANG)     # ChildProcessError here = stop stole the status
        assert done == child.pid and os.waitstatus_to_exitcode(status) == 7
        child.returncode = 7
    finally:
        if child.returncode is None:
            child.wait()


def test_fix2_5_a_claude_json_over_the_read_bound_is_refused_untouched_and_never_read_whole(tmp_path, monkeypatch):
    assert sl._TRUST_FILE_MAX == 16 * 1024 * 1024
    monkeypatch.setattr(sl, "_TRUST_FILE_MAX", 4096)
    h = tmp_path / "h"
    h.mkdir()
    body = json.dumps({"projects": {}}).encode() + b" " * 8192        # valid JSON as a whole: only the bound refuses
    (h / ".claude.json").write_bytes(body)
    got = []
    real_read = os.read

    def counting(fd, n):
        b = real_read(fd, n)
        got.append(len(b))
        return b
    monkeypatch.setattr(sl.os, "read", counting)
    with pytest.raises(sl.SeatLaunchRefused, match="cannot be read as JSON"):
        sl.seed_trust(h, "/lane/x")
    assert sum(got) <= 4097, f"read {sum(got)} bytes of a file over the bound"
    assert (h / ".claude.json").read_bytes() == body
    (h / ".claude.json").write_bytes(json.dumps({"projects": {}}).encode())
    sl.seed_trust(h, "/lane/x")                                         # within the bound it is read and seeded
    assert json.loads((h / ".claude.json").read_text())["projects"]["/lane/x"]["hasTrustDialogAccepted"] is True


def test_fix2_6_a_held_trust_lock_refuses_in_bounded_time_and_never_hangs(tmp_path, monkeypatch):
    import fcntl
    import math
    assert math.isfinite(sl._TRUST_LOCK_WAIT) and 0 < sl._TRUST_LOCK_WAIT <= 10
    monkeypatch.setattr(sl, "_TRUST_LOCK_WAIT", 0.3)
    h = tmp_path / "h"
    h.mkdir()
    (h / ".claude.json").write_text('{"projects": {}}')
    holder = os.open(h / ".claude.json.levain-lock", os.O_RDWR | os.O_CREAT, 0o600)   # another open file: its own lock

    def boom(*_a):
        raise AssertionError("seed_trust blocked on a held lock")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(5)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        t0 = time.monotonic()
        with pytest.raises(sl.SeatLaunchRefused, match="held by another process"):
            sl.seed_trust(h, "/lane/x")
        assert time.monotonic() - t0 < 3
        assert (h / ".claude.json").read_text() == '{"projects": {}}'
        fcntl.flock(holder, fcntl.LOCK_UN)
        sl.seed_trust(h, "/lane/x")                                     # released: the seed goes through
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
        os.close(holder)
    assert "/lane/x" in json.loads((h / ".claude.json").read_text())["projects"]


def test_fix2_7_a_cli_stop_refusal_is_one_clean_line_and_exit_1(seats, rig, monkeypatch, capsys):
    from levain import cli

    def ns(lane):
        return argparse.Namespace(seat_cmd="stop", lane_key=lane, grace=5.0, path=rig.tmp, data_root=rig.world.root)
    _plan, info = seats.go("lane-cs")
    seats.recorded("child")
    real = sl._proc_start_ns
    monkeypatch.setattr(sl, "_proc_start_ns", lambda pid: real(pid) + 5_000_000_000 if pid == info["pid"] else real(pid))
    assert cli._cmd_seat(ns("lane-cs")) == 1
    err = capsys.readouterr().err
    assert err.startswith("seat stop refused: pid") and "reused" in err and "Traceback" not in err
    assert cli._cmd_seat(ns("lane-none")) == 1
    assert capsys.readouterr().err.startswith("seat stop refused: no launched seat is recorded")
    monkeypatch.undo()
    assert _alive(info["pid"]), "a refused stop signalled nothing"
