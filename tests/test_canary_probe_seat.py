"""The terminal seat arms of levain/canary_probe.py (Z1c). Run UNSANDBOXED in a tmp tree, so every wall-arm must read
OPEN: that is the instrument showing it can fail. The real, sandboxed run is Z1e in the VM."""
from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import threading

import pytest

from levain import canary_probe as cp


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null"})


@pytest.fixture
def world(tmp_path):
    lane_a, lane_b, home = tmp_path / "laneA", tmp_path / "laneB", tmp_path / "home"
    for d in (lane_a, lane_b, home):
        d.mkdir()
    _git(["init", "-q"], lane_a)
    key = tmp_path / "launcher.key"
    key.write_text("secret")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                srv.accept()[0].close()
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    cfg = {"nonce": "ab12cd34", "lane_a": str(lane_a), "lane_b": str(lane_b), "key_file": str(key),
           "port": srv.getsockname()[1], "home": str(home), "offhost": "127.0.0.1:1", "cockpit": "none"}
    yield cfg
    stop.set()
    t.join(2)
    srv.close()


def _run(cfg):
    lines = []
    cp.run_seat(cfg, out=lines.append)
    return {ln.split()[1]: ln.split()[2] for ln in lines}


def test_arm_names_are_the_design_list():
    assert cp.SEAT_ARMS == ("XLANE", "GITHOOK", "GITCONFIG", "GITINFO", "GITMODULES", "BIND", "LOOPCONNECT", "KEYCAT",
                            "HOMEWRITE", "OFFHOST", "COCKPITPORT", "RENAMEGIT", "RENAMEHOOKS")
    assert cp.SEAT_CONTROLS == ("CTRLWRITE", "CTRLCOMMIT")


def test_an_unsandboxed_run_reports_every_wall_arm_open(world):
    got = _run(world)
    assert got["CTRLWRITE"] == got["CTRLCOMMIT"] == "OK"
    for arm in ("XLANE", "GITHOOK", "GITCONFIG", "GITINFO", "GITMODULES", "BIND", "LOOPCONNECT", "KEYCAT",
                "HOMEWRITE", "RENAMEGIT"):
        assert got[arm] == "OPEN", arm
    assert got["RENAMEHOOKS"] == "OPEN"
    # a refused connection and an absent cockpit are never a pass
    assert got["OFFHOST"] == "INCONCLUSIVE" and got["COCKPITPORT"] == "INCONCLUSIVE"


def test_rename_is_undone(world):
    _run(world)
    assert os.path.isdir(os.path.join(world["lane_a"], ".git", "hooks"))
    assert not os.path.exists(os.path.join(world["lane_a"], ".g2"))


def test_a_failed_control_attempts_no_arm(world, tmp_path):
    (tmp_path / "plain").mkdir()
    world["lane_a"] = str(tmp_path / "plain")   # not a git repo: the commit control fails
    got = _run(world)
    assert got["CTRLWRITE"] == "OK" and got["CTRLCOMMIT"] == "FAILED"
    assert not set(cp.SEAT_ARMS) & set(got)
    assert not os.path.exists(os.path.join(world["lane_b"], "xlane-ab12cd34.txt"))


def test_blocked_only_for_a_refusal_errno():
    assert cp.seat_status(PermissionError(errno.EPERM, "x"))[0] == "BLOCKED"
    assert cp.seat_status(OSError(errno.EROFS, "x"))[0] == "BLOCKED"
    assert cp.seat_status(ConnectionRefusedError(errno.ECONNREFUSED, "x"))[0] == "INCONCLUSIVE"
    assert cp.seat_status(FileNotFoundError(errno.ENOENT, "x"))[0] == "INCONCLUSIVE"


def test_main_dispatches_the_seat_config(world, tmp_path, capsys):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(world))
    assert cp.main(["seat", str(p)]) == 0
    assert "ab12cd34 CTRLWRITE OK" in capsys.readouterr().out
    assert cp.main(["seat"]) == 2


def test_flipper_counts_flips_and_survives_a_landed_file(tmp_path):
    link, decoy, counter = tmp_path / "swap", tmp_path / "decoy", tmp_path / "flips.txt"
    decoy.mkdir()
    link.mkdir()
    (link / "s0.json").write_text("x")   # a write that landed in the real directory
    assert cp.seat_flip(str(link), str(link), str(decoy), 0.3, str(counter)) == 0
    count, end = counter.read_text().split()
    assert int(count) > 2 and end == "timeout"
    assert (link.is_symlink() and os.readlink(link) == str(decoy)) or link.is_dir()
    assert not decoy.joinpath("s0.json").exists()   # the flipper never writes into the decoy


def test_the_flipper_stops_on_the_seats_stop_file_and_says_so(tmp_path):
    import threading
    link, decoy, counter = tmp_path / "swap", tmp_path / "decoy", tmp_path / "flips.txt"
    decoy.mkdir()
    link.mkdir()
    t = threading.Thread(target=cp.seat_flip, args=(str(link), str(link), str(decoy), 30.0, str(counter)))
    t.start()
    assert cp.seat_flip_stop(str(counter)) == 0                       # bounded wait for the final line
    t.join(5)
    assert not t.is_alive()
    count, end = counter.read_text().split()
    assert end == "stop" and int(count) >= 0


def test_the_flipper_ends_when_its_directory_is_gone(tmp_path):
    parent = tmp_path / "lane"
    parent.mkdir()
    link, decoy, counter = parent / "swap", tmp_path / "decoy", tmp_path / "flips.txt"
    decoy.mkdir()
    link.mkdir()
    import threading
    import time
    t = threading.Thread(target=cp.seat_flip, args=(str(link), str(link), str(decoy), 30.0, str(counter)))
    t.start()
    time.sleep(0.2)
    os.rename(parent, tmp_path / "moved")                            # atomic: the race cannot refill it
    t.join(5)
    assert not t.is_alive() and counter.read_text().split()[1] == "gone"
