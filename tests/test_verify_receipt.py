"""The receipt writer and the ledger of scripts/verify.py (levain row 164/172, L2 session 1).

The junit fixture is a real pytest run (pass, fail, skip, xfail, error) executed in a tmp dir, so the counts are
re-derived from a file pytest wrote, not from a hand-typed one.  Nothing here touches the real ledger or run dir.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("levain_verify_script", ROOT / "scripts" / "verify.py")
verify = importlib.util.module_from_spec(_spec)
sys.modules["levain_verify_script"] = verify
_spec.loader.exec_module(verify)

FIXTURE_TESTS = textwrap.dedent('''
    import pytest

    def test_ok(): pass

    @pytest.mark.parametrize("n", [1, 2])
    def test_param(n): assert n == 1

    def test_boom(): assert False, "boom"

    @pytest.mark.skip(reason="no mac keychain")
    def test_skipped(): pass

    @pytest.mark.xfail(reason="known")
    def test_xf(): assert False

    @pytest.fixture
    def broken(): raise RuntimeError("setup died")

    def test_err(broken): pass

    class TestK:
        def test_in_class(self): assert False
''')


@pytest.fixture(scope="module")
def junit_run(tmp_path_factory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("fx")
    (d / "test_fx.py").write_text(FIXTURE_TESTS)
    j = d / "junit.xml"
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", f"--junitxml={j}",
                        "--rootdir", str(d), "-c", "/dev/null", str(d / "test_fx.py")],
                       cwd=d, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr    # the fixture has reds on purpose
    return d, j


def test_counts_and_reds_rederived_from_junit(junit_run):
    d, j = junit_run
    p = verify.parse_junit(j, d)
    c = p["counts"]
    assert c == dict(collected=8, passed=2, failed=3, error=1, skipped=1, xfailed=1), c
    assert p["skips_by_reason"] == {"no mac keychain": 1}
    assert [r["nodeid"] for r in p["reds"]] == [
        "test_fx.py::test_param[2]", "test_fx.py::test_boom", "test_fx.py::test_err", "test_fx.py::TestK::test_in_class"]
    assert {r["kind"] for r in p["reds"]} == {"failure", "error"}
    assert "boom" in next(r for r in p["reds"] if r["nodeid"].endswith("test_boom"))["text"]


def test_nodeid_without_checkout_uses_class_heuristic(junit_run):
    _, j = junit_run
    ids = [r["nodeid"] for r in verify.parse_junit(j, None)["reds"]]
    assert "test_fx.py::TestK::test_in_class" in ids


def _leg(reds, collected=10, rc=1):
    return {"platform": "macos", "jobs": [{"name": "pytest-full-py312", "rc": rc, "reds": reds,
                                           "counts": dict(collected=collected, passed=0, failed=len(reds), error=0, skipped=0, xfailed=0)}]}


def test_red_without_ledger_id_makes_the_receipt_red():
    leg = _leg([{"nodeid": "tests/a.py::t", "class": "load", "evidence": "x", "ledger_id": None}])
    rec = verify.build_receipt("a" * 40, "run1", {}, "blob", [leg], True)
    assert rec["verdict"] == "red" and "red without ledger_id: tests/a.py::t" in rec["verdict_why"]


def test_regression_red_even_with_ledger_id_and_green_when_all_excused():
    bad = _leg([{"nodeid": "t", "class": "regression", "evidence": "x", "ledger_id": "lg_1"}])
    assert verify.build_receipt("a" * 40, "r", {}, "b", [bad], True)["verdict"] == "red"
    ok = _leg([{"nodeid": "t", "class": "inherited", "evidence": "x", "ledger_id": "lg_1"}])
    rec = verify.build_receipt("a" * 40, "r", {}, "b", [ok], True)
    assert rec["verdict"] == "green" and rec["schema"] == "levain-test-receipt/1"


def test_empty_run_runner_mismatch_and_pytest_crash_are_red():
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], collected=0, rc=0)], True)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=2)], True)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, None, [_leg([], rc=0)], None)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=0)], False)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=0)], True)["verdict"] == "green"


def test_receipt_file_name_and_roundtrip(tmp_path):
    rec = verify.build_receipt("0123456789abcdef" * 2 + "01234567", "r", {"uv_lock_sha256": "h"}, "b", [_leg([], rc=0)], True)
    p = verify.write_receipt(rec, tmp_path / "receipts")
    assert p.name == "vr_0123456789ab.json"
    assert json.loads(p.read_text()) == rec


def test_ledger_is_append_only_with_sightings(tmp_path):
    led = verify.Ledger(tmp_path / "l" / "ledger.jsonl")
    lid, new = led.file("tests/a.py::t", "flaky", "run1", "s1", "mixed")
    assert new and led.open_entry_for("tests/a.py::t")["board_row"] is None
    before = led.path.read_text()
    lid2, new2 = led.file("tests/a.py::t", "flaky", "run2", "s2", "mixed again")
    assert lid2 == lid and not new2
    after = led.path.read_text()
    assert after.startswith(before) and after.count("\n") == before.count("\n") + 1     # only appended
    led.mark(lid, "fixed")
    assert led.open_entry_for("tests/a.py::t") is None
    lid3, new3 = led.file("tests/a.py::t", "flaky", "run3", "s3", "back")
    assert new3 and lid3 != lid                                                           # a fixed entry is history, not reopened
    e = led.entries()
    assert [s["run_id"] for s in e[lid]["sightings"]] == ["run2"] and e[lid]["status"] == "fixed"
    assert [r["type"] for r in led.lines()] == ["entry", "sighting", "status", "entry"]


def test_classification_rules_end_to_end(tmp_path):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    seq = {"load": [True] * 3, "flaky": [True, False, True], "inh": [False] * 3, "reg": [False] * 3, "touched": [], "led": [False] * 3}
    calls = {}

    def rerun(nid):
        k = nid.split("::")[1]
        calls[k] = calls.get(k, 0) + 1
        return seq[k].pop(0)

    prior, _ = led.file("tests/a.py::led", "flaky", "r0", "s0", "was flaky")
    reds = [{"nodeid": f"tests/a.py::{k}", "kind": "failure"} for k in ("load", "flaky", "inh", "reg", "led")]
    reds.append({"nodeid": "tests/touched.py::t", "kind": "failure"})
    seq["t"] = []
    out = verify.classify(reds, ledger=led, run_id="r1", sha="s1", touched={"tests/touched.py"}, rerun=rerun,
                          baseline=lambda nids: {"tests/a.py::inh": "red", "tests/a.py::reg": "green"})
    cls = {r["nodeid"].split("::")[1] if "touched" not in r["nodeid"] else "touched": r["class"] for r in out}
    assert cls == {"load": "load", "flaky": "flaky", "inh": "inherited", "reg": "regression", "led": "regression", "touched": "regression"}
    assert all(calls[k] == 3 for k in ("load", "flaky", "inh", "reg", "led")) and "t" not in calls
    ids = {r["nodeid"].split("::")[1]: r["ledger_id"] for r in out if r["class"] != "regression"}
    assert all(v for v in ids.values()) and all(r["ledger_id"] is None for r in out if r["class"] == "regression")
    assert led.entries()[prior]["sightings"] == []        # a regression never counts as a sighting
    assert verify.verdict_of([_leg(out)])[0] == "red"


def test_absent_on_main_is_a_regression(tmp_path):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    out = verify.classify([{"nodeid": "tests/new.py::t"}], ledger=led, run_id="r", sha="s", touched=set(),
                          rerun=lambda n: False, baseline=lambda n: {})
    assert out[0]["class"] == "regression" and "not present" in out[0]["evidence"]


def test_required_run_skip_and_setup_failure_are_red():
    leg = _leg([], rc=0)
    leg["jobs"][0]["required_run"] = True
    leg["jobs"][0]["counts"]["skipped"] = 2
    rec = verify.build_receipt("a" * 40, "r", {}, "b", [leg], True)
    assert rec["verdict"] == "red" and any("skipped under a required-run job" in w for w in rec["verdict_why"])
    bad = {"platform": "linux", "jobs": [verify.failed_job("root-closure-py39", "pip install")]}
    rec = verify.build_receipt("a" * 40, "r", {}, "b", [bad], True)
    assert rec["verdict"] == "red" and any("setup failed: pip install" in w for w in rec["verdict_why"])


def test_the_live_floor_targets_exist():
    for t in verify.BWRAP_TARGETS:
        assert (ROOT / t.split("::")[0]).is_file(), t


# ---------------------------------------------------------------- fixtures for the orchestration tests

def _g(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git repo: a base commit (also origin/main) and one commit on top, carrying the real runner script."""
    r = tmp_path / "repo"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    (r / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "verify.py", r / "scripts" / "verify.py")
    (r / "tests").mkdir()
    (r / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    _g(r, "add", "-A")
    _g(r, "commit", "-q", "-m", "base")
    _g(r, "update-ref", "refs/remotes/origin/main", _g(r, "rev-parse", "HEAD"))
    (r / "tests" / "test_b.py").write_text("def test_b(): pass\n")
    _g(r, "add", "-A")
    _g(r, "commit", "-q", "-m", "change")
    return r, _g(r, "rev-parse", "HEAD")


class FakeTools:
    """git / hdiutil / diskutil / ps / kill, all fake: nothing here touches the machine."""

    def __init__(self, images=None, procs=None, worktrees=None, eject_ok=True):
        self.images = list(images or [])
        self.procs = list(procs or [])
        self.worktrees = list(worktrees or [])
        self.eject_ok = eject_ok
        self.git_calls, self.ejected, self.kills = [], [], []

    def git(self, repo, *args):
        self.git_calls.append(args)
        out = ""
        if args[:2] == ("worktree", "list"):
            out = "".join(f"worktree {w}\nHEAD abc\ndetached\n\n" for w in self.worktrees)
        return types.SimpleNamespace(stdout=out, returncode=0)

    def hdiutil_images(self):
        return list(self.images)

    def eject(self, dev):
        self.ejected.append(dev)
        if not self.eject_ok:
            return False
        self.images = [i for i in self.images if dev not in {m.group(1) for d in i["devs"] if (m := re.match(r"^(/dev/disk\d+)", d))}]
        return True

    def ps(self):
        return list(self.procs)

    def kill(self, pid, sig):
        self.kills.append((pid, int(sig)))
        self.procs = [(p, c) for p, c in self.procs if p != pid]


def _full_leg(plat, workers, max_load=3.0, load_reds=0, reds=(), diag=()):
    leg = _leg(list(reds), rc=1 if reds else 0)
    leg.update({"platform": plat, "host": "h", "python": "3.12.1", "duration_s": 1.0, "workers": workers,
                "max_load": max_load, "load_reds": load_reds, "_diag": list(diag)})
    return leg


def _args(r, state, *extra):
    return ["--repo", str(r), "--state-dir", str(state), "--receipts-dir", str(state.parent / "receipts"), "--no-diagnosis", *extra]


def _only_run(state):
    return next((state / "runs").iterdir())


# ---------------------------------------------------------------- state dir

def test_default_state_dir_is_outside_every_checkout():
    assert verify.DEFAULT_STATE.parts[-3:] == (".local", "state", "levain-verify")    # the test home is faked, so the tail only
    assert not verify.state_in_repo(verify.DEFAULT_STATE, ROOT)


def test_a_state_dir_inside_the_checkout_or_project_memory_is_refused(repo, tmp_path, capsys):
    r, sha = repo
    for inside in (r / "verify-state", tmp_path / "project_memory" / "verify-state"):
        for cmd in ("run", "start"):
            assert verify.main([cmd, "--sha", sha, "--repo", str(r), "--state-dir", str(inside)]) == 2
            assert "refusing a state dir" in capsys.readouterr().err
        assert not inside.exists()


# ---------------------------------------------------------------- the sweep and the reap

def _image(path, devs, mounts=()):
    return {"image_path": str(path), "devs": list(devs), "mounts": [str(m) for m in mounts]}


def test_hdiutil_plist_parse_and_whole_disk_selection(tmp_path):
    run = tmp_path / "run"
    plist = plistlib.dumps({"images": [
        {"image-path": str(run / "pt" / "a.dmg"),
         "system-entities": [{"dev-entry": "/dev/disk5"}, {"dev-entry": "/dev/disk5s1", "mount-point": str(run / "pt" / "m")}]},
        {"image-path": "/Users/x/other.dmg", "system-entities": [{"dev-entry": "/dev/disk7s2", "mount-point": str(run / "mnt")}]},
        {"image-path": "/Users/x/keep.dmg", "system-entities": [{"dev-entry": "/dev/disk9", "mount-point": "/Volumes/keep"}]},
        {"image-path": str(run / "pt" / "apfs.dmg"), "system-entities": [{"dev-entry": "/dev/disk11s1s1"}]},
    ]})
    imgs = verify.parse_hdiutil_plist(plist)
    assert verify.images_to_eject(imgs, run) == ["/dev/disk5", "/dev/disk7", "/dev/disk11"]


def test_reap_ejects_only_images_under_the_run_and_removes_worktrees_and_dir(tmp_path):
    run = tmp_path / "runs" / "r1"
    (run / "pt").mkdir(parents=True)
    (run / "src_at_sha").mkdir()
    keep = tmp_path / "elsewhere.dmg"
    tools = FakeTools(images=[_image(run / "pt" / "a.dmg", ["/dev/disk5", "/dev/disk5s1"], [run / "pt" / "m"]),
                              _image(keep, ["/dev/disk9", "/dev/disk9s1"], ["/Volumes/keep"])],
                      worktrees=[str(tmp_path / "main_checkout"), str(run / "src_at_sha" / "src"), str(tmp_path / "other" / "wt")])
    s = verify.reap_run(run, tmp_path / "main_checkout", tools, grace=0)
    assert tools.ejected == ["/dev/disk5"] and s["ejected"] == ["/dev/disk5"]
    assert [c for c in tools.git_calls if c[:2] == ("worktree", "remove")] == [("worktree", "remove", "--force", str(run / "src_at_sha" / "src"))]
    assert ("worktree", "prune") in tools.git_calls
    assert s["removed"] and not run.exists()
    assert tools.images and tools.images[0]["devs"][0] == "/dev/disk9"        # the other image was never touched


def test_reap_deletes_nothing_while_an_image_is_still_mounted(tmp_path):
    run = tmp_path / "runs" / "r1"
    (run / "pt").mkdir(parents=True)
    tools = FakeTools(images=[_image(run / "pt" / "a.dmg", ["/dev/disk5"], [run / "pt" / "m"])], eject_ok=False)
    s = verify.reap_run(run, tmp_path, tools, grace=0, say=lambda *_: None)
    assert s["left_mounted"] == ["/dev/disk5"] and run.exists() and not s["removed"]
    assert not [c for c in tools.git_calls if c[:2] == ("worktree", "remove")]


def test_reap_kills_only_processes_working_in_a_subdir_of_the_run(tmp_path):
    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    tools = FakeTools(procs=[
        (101, f"{run}/src_at_sha/venv/bin/python -m pytest -n 6"),
        (102, f"/usr/bin/python -m pytest --basetemp={run}/pt tests"),
        (103, f"tail -f {run}/runner.log"),
        (104, "/usr/bin/python3 unrelated.py"),
        (os.getpid(), f"{run}/src_at_sha/venv/bin/python me")])
    verify.reap_run(run, tmp_path, tools, grace=0)
    assert tools.kills == [(101, int(signal.SIGTERM)), (102, int(signal.SIGTERM))]


def test_sweep_reaps_dead_runs_and_spares_live_finished_current_and_fresh(tmp_path):
    state = tmp_path / "state"
    runs = state / "runs"
    mk = lambda name, st: (runs / name).mkdir(parents=True) or verify.Status(runs / name).update(state=st)
    mk("dead-running", "running")
    mk("dead-nostate", None)
    (runs / "dead-bare").mkdir()
    mk("finished-red", "red")
    mk("finished-passed", "passed")
    mk("current", "running")
    mk("fresh-starting", "starting")
    mk("old-starting", "starting")
    old = time.time() - 1000
    os.utime(runs / "old-starting" / "status.json", (old, old))
    mk("live", "running")
    held = verify.take_run_lock(runs / "live")
    tools = FakeTools()
    done = verify.sweep_runs(state, tmp_path, tools, current="current", say=lambda *_: None)
    held.close()
    assert sorted(Path(d["run"]).name for d in done) == ["dead-bare", "dead-nostate", "dead-running", "old-starting"]
    assert sorted(p.name for p in runs.iterdir()) == ["current", "finished-passed", "finished-red", "fresh-starting", "live"]


def test_run_is_live_follows_the_lock(tmp_path):
    run = tmp_path / "r"
    assert not verify.run_is_live(run)
    f = verify.take_run_lock(run)
    assert verify.run_is_live(run)
    f.close()
    assert not verify.run_is_live(run)


def test_sigterm_and_sigint_raise_killed_and_handlers_restore():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    prev = verify.install_signal_handlers()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with pytest.raises(verify.Killed) as ei:
                os.kill(os.getpid(), sig)
                time.sleep(2)
            assert ei.value.signum == int(sig)
    finally:
        verify.restore_signal_handlers(prev)
    assert {s: signal.getsignal(s) for s in before} == before


def test_terminate_children_kills_the_whole_group(tmp_path):
    marker = tmp_path / "grandchild.pid"
    log = tmp_path / "log"
    cmd = ["sh", "-c", f"sleep 300 & echo $! > {marker}; wait"]
    t = threading.Thread(target=lambda: verify._run(cmd, cwd=tmp_path, env=dict(os.environ), log=log, timeout=60), daemon=True)
    t.start()
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    gpid = int(marker.read_text())
    verify.terminate_children(grace=1)
    t.join(10)
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(gpid, 0)


def test_a_sigterm_inside_a_run_cleans_up_marks_killed_and_keeps_the_logs(repo, tmp_path, monkeypatch, capsys):
    r, sha = repo
    state = tmp_path / "state"
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(verify, "Tools", FakeTools)

    def leg(_repo, _sha, run, *a, **k):
        (run / "src_at_sha" / "venv").mkdir(parents=True)
        (run / "verify.log").write_text("log")
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(3)
        raise AssertionError("the handler should have raised")

    monkeypatch.setattr(verify, "run_leg_local", leg)
    assert verify.main(["run", "--sha", sha, *_args(r, state)]) == 128 + int(signal.SIGTERM)
    run = _only_run(state)
    assert json.loads((run / "status.json").read_text())["state"] == "killed"
    assert (run / "verify.log").exists() and not (run / "src_at_sha").exists()
    assert not list((tmp_path / "receipts").glob("*")) if (tmp_path / "receipts").exists() else True
    assert signal.getsignal(signal.SIGTERM) == before


# ---------------------------------------------------------------- start and status

def test_start_detaches_prints_id_and_log_and_status_follows_the_run(repo, tmp_path, monkeypatch, capsys):
    r, sha = repo
    state = tmp_path / "state"
    argsfile = tmp_path / "args.txt"
    fake = tmp_path / "fakepy"
    fake.write_text(f'#!/bin/sh\necho "$@" > {argsfile}\n')
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake))
    assert verify.main(["start", "--sha", sha, "--leg", "both", "--repo", str(r), "--state-dir", str(state), "--workers", "3"]) == 0
    out = capsys.readouterr().out
    run_id = re.search(r"^run_id: (\S+)$", out, re.M).group(1)
    log = Path(re.search(r"^log: (\S+)$", out, re.M).group(1))
    assert log == state / "runs" / run_id / "runner.log" and log.exists()
    for _ in range(100):
        if argsfile.exists() and argsfile.read_text().strip():
            break
        time.sleep(0.1)
    args = argsfile.read_text()
    assert f"--run-id {run_id}" in args and f"--sha {sha}" in args and "--leg both" in args and "--workers 3" in args
    assert f"--state-dir {state}" in args

    run = state / "runs" / run_id
    st = lambda: (verify.main(["status", "--run", run_id, "--state-dir", str(state), "--json"]), json.loads(capsys.readouterr().out))[1]
    assert st()["state"] == "starting"                       # fresh: the child has not taken its lock yet
    old = time.time() - 1000
    os.utime(run / "status.json", (old, old))
    got = st()
    assert got["state"] == "killed" and "no lock" in got["note"]      # gone without ever running
    verify.Status(run).update(state="running", phase_mac="pytest")
    held = verify.take_run_lock(run)
    assert st()["state"] == "running"
    held.close()
    assert st()["state"] == "killed"
    verify.Status(run).update(state="passed", verdict="green")
    assert st()["state"] == "passed"
    assert verify.main(["status", "--state-dir", str(state)]) == 0           # newest run by default
    assert "state: passed" in capsys.readouterr().out
    assert verify.main(["status", "--run", "nope", "--state-dir", str(state)]) == 2


# ---------------------------------------------------------------- workers, load and the merged receipt

def test_workers_default_by_leg_and_are_recorded_with_max_load(repo, tmp_path, monkeypatch):
    r, sha = repo
    seen = {}

    def local(_r, _s, run, run_id, ledger, status, *, leg, python, workers, timeout):
        seen["local"] = (leg, python, workers)
        return _full_leg("macos" if leg == "mac" else "linux", workers, max_load=42.5, load_reds=3)

    def remote(_r, _s, run, run_id, ledger, status, *, host, workers, **k):
        seen["remote"] = (host, workers)
        return _full_leg("linux", workers)

    monkeypatch.setattr(verify, "Tools", FakeTools)
    monkeypatch.setattr(verify, "run_leg_local", local)
    monkeypatch.setattr(verify, "run_leg_remote", remote)
    s1 = tmp_path / "s1"
    assert verify.main(["run", "--sha", sha, "--leg", "both", *_args(r, s1)]) == 0
    assert seen["local"] == ("mac", verify.MAC_PYTHON, 6) and seen["remote"] == ("argushub", 4)
    st = json.loads((_only_run(s1) / "status.json").read_text())
    assert st["state"] == "passed" and st["max_load"] == 42.5 and st["load_reds"] == 3 and st["workers"] == 6
    s2 = tmp_path / "s2"
    assert verify.main(["run", "--sha", sha, "--leg", "both", "--workers", "2", "--linux-workers", "5", *_args(r, s2)]) == 0
    assert seen["local"][2] == 2 and seen["remote"][1] == 5
    s3 = tmp_path / "s3"
    assert verify.main(["run", "--sha", sha, "--leg", "linux", "--here", *_args(r, s3)]) == 0
    assert seen["local"] == ("linux", verify.LINUX_PYTHON, 4)
    s4 = tmp_path / "s4"
    assert verify.main(["run", "--sha", sha, "--leg", "linux", "--here", "--workers", "2", *_args(r, s4)]) == 0
    assert seen["local"][2] == 2


def test_here_writes_the_leg_json_and_no_receipt_and_touches_no_ledger(repo, tmp_path, monkeypatch):
    r, sha = repo
    got = {}

    def local(_r, _s, run, run_id, ledger, status, **k):
        got["ledger"] = ledger
        return _full_leg("linux", 4)

    monkeypatch.setattr(verify, "Tools", FakeTools)
    monkeypatch.setattr(verify, "run_leg_local", local)
    state = tmp_path / "state"
    out = tmp_path / "out" / "leg.json"
    out.parent.mkdir()
    assert verify.main(["run", "--sha", sha, "--leg", "linux", "--here", "--leg-json", str(out), *_args(r, state)]) == 0
    assert json.loads(out.read_text())["platform"] == "linux" and got["ledger"] is None
    assert not (tmp_path / "receipts").exists() and not (state / "ledger.jsonl").exists()
    assert json.loads((_only_run(state) / "status.json").read_text())["state"] == "leg-done"
    assert verify.main(["run", "--sha", sha, "--leg", "both", "--here", *_args(r, tmp_path / "s9")]) == 2


def test_two_legs_merge_into_one_receipt_and_either_red_leg_makes_it_red(repo, tmp_path, monkeypatch):
    r, sha = repo
    bad = [{"nodeid": "tests/x.py::t", "class": "regression", "evidence": "e", "ledger_id": None}]
    for linux_reds, want in (([], "green"), (bad, "red")):
        monkeypatch.setattr(verify, "Tools", FakeTools)
        monkeypatch.setattr(verify, "run_leg_local", lambda *a, **k: _full_leg("macos", 6))
        monkeypatch.setattr(verify, "run_leg_remote", lambda *a, **k: _full_leg("linux", 4, reds=linux_reds))
        state = tmp_path / f"state-{want}"
        assert verify.main(["run", "--sha", sha, "--leg", "both", *_args(r, state)]) == (0 if want == "green" else 1)
        rec = json.loads((tmp_path / "receipts" / f"vr_{sha[:12]}.json").read_text())
        assert [l["platform"] for l in rec["legs"]] == ["macos", "linux"] and rec["verdict"] == want
        assert rec["runner_blob"] and "_diag" not in rec["legs"][0]
        (tmp_path / "receipts" / f"vr_{sha[:12]}.json").unlink()


def test_a_failed_leg_aborts_the_run_with_no_receipt(repo, tmp_path, monkeypatch, capsys):
    r, sha = repo

    def remote(*a, **k):
        raise RuntimeError("linux leg: shipping the bundle failed (exit 255)")

    monkeypatch.setattr(verify, "Tools", FakeTools)
    monkeypatch.setattr(verify, "run_leg_local", lambda *a, **k: _full_leg("macos", 6))
    monkeypatch.setattr(verify, "run_leg_remote", remote)
    state = tmp_path / "state"
    assert verify.main(["run", "--sha", sha, "--leg", "both", *_args(r, state)]) == 4
    assert "shipping the bundle failed" in capsys.readouterr().err
    assert not (tmp_path / "receipts").exists()
    assert json.loads((_only_run(state) / "status.json").read_text())["state"] == "error"


def test_a_second_run_is_refused_and_its_status_says_so(repo, tmp_path, monkeypatch):
    r, sha = repo
    state = tmp_path / "state"
    held = verify.lock_host(state)
    run = state / "runs" / "pre"
    run.mkdir(parents=True)
    assert verify.main(["run", "--sha", sha, "--run-id", "pre", *_args(r, state)]) == 3
    assert verify.read_status(run)["state"] == "refused"
    held.close()


# ---------------------------------------------------------------- the linux leg's transport

RUN_ID = "20261109T000000Z-abc12345-ab12"


def _remote_fixture(tmp_path, junit_text="<testsuites/>"):
    rrun = f"{verify.REMOTE_STATE}/runs/{RUN_ID}"
    reds = [{"nodeid": "tests/a.py::l", "class": "load", "evidence": "e1", "ledger_id": None},
            {"nodeid": "tests/a.py::i", "class": "inherited", "evidence": "e2", "ledger_id": None}]
    leg = {"platform": "linux", "host": "argushub", "python": "3.13.1", "workers": 4, "max_load": 9.0, "load_reds": 1,
           "jobs": [{"name": "pytest-full-py313", "rc": 1, "counts": dict(collected=2, passed=0, failed=2, error=0, skipped=0, xfailed=0),
                     "junit": {"path": f"/home/u/{rrun}/junit-linux.xml", "sha256": hashlib.sha256(junit_text.encode()).hexdigest()},
                     "reds": reds}],
           "_diag": [{"nodeid": "tests/a.py::l", "class": "load", "evidence": "e1", "text": "t1"},
                     {"nodeid": "tests/a.py::i", "class": "inherited", "evidence": "e2", "text": "t2"}]}
    return rrun, leg, junit_text


def _stub_sh(r, sha, leg, junit_text, cmds, fail_on=None, junit_override=None):
    def sh(cmd, *, log, stdin=None, timeout=None):
        cmds.append((cmd, stdin, timeout))
        if fail_on and fail_on in " ".join(cmd):
            return 255
        if cmd[0] == "ssh" and "cat > " in cmd[-1]:
            heads = subprocess.run(["git", "bundle", "list-heads", str(stdin)], capture_output=True, text=True, check=True).stdout
            assert f"refs/verify/{sha[:12]}" in heads and "refs/remotes/origin/main" in heads
        if cmd[0] == "scp" and cmd[-2].endswith("leg-linux.json"):
            (Path(cmd[-1]) / "leg-linux.json").write_text(json.dumps(leg))
        if cmd[0] == "scp" and cmd[-2].endswith("junit-linux.xml"):
            (Path(cmd[-1]) / "junit-linux.xml").write_text(junit_override if junit_override is not None else junit_text)
        return 0
    return sh


def test_remote_leg_command_lines_and_ledger_filing(repo, tmp_path):
    r, sha = repo
    run = tmp_path / "run"
    run.mkdir()
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    prior, _ = led.file("tests/a.py::i", "flaky", "r0", "s0", "was flaky")        # held as flaky: failing all reruns is never excused
    rrun, leg, jt = _remote_fixture(tmp_path)
    cmds = []
    got = verify.run_leg_remote(r, sha, run, RUN_ID, led, None, host="argushub", workers=4,
                                sh=_stub_sh(r, sha, leg, jt, cmds), script=ROOT / "scripts" / "verify.py")
    c = [x[0] for x in cmds]
    ssh_pre = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10", "argushub"]
    assert c[0][:-1] == ssh_pre and c[0][-1] == f"mkdir -p {rrun}/xfer && cat > {rrun}/xfer/sha.bundle"
    assert cmds[0][1] == run / "linux" / "sha.bundle"
    assert c[1][0] == "scp" and c[1][-2:] == [str(ROOT / "scripts" / "verify.py"), f"argushub:{rrun}/verify.py"]
    run_cmd = c[2][-1]
    assert c[2][:-1] == ssh_pre
    for frag in (f"cd {rrun}", f"fetch -q ../sha.bundle +refs/verify/{sha[:12]}:refs/verify/{sha[:12]}", "+refs/remotes/origin/main:refs/remotes/origin/main",
                 f"python3 verify.py run --here --leg linux --sha {sha} --repo xfer/repo --run-id {RUN_ID} --workers 4 --leg-json leg-linux.json"):
        assert frag in run_cmd, frag
    assert cmds[2][2] == 4 * 3600
    assert c[3][-2:] == [f"argushub:{rrun}/leg-linux.json", str(run / "linux") + "/"]
    assert c[4][-2:] == [f"argushub:/home/u/{rrun}/junit-linux.xml", str(run / "linux") + "/"]
    assert c[-1][:-1] == ssh_pre and c[-1][-1] == f"rm -rf {rrun}"
    assert got["jobs"][0]["junit"]["path"] == str(run / "linux" / "junit-linux.xml")
    reds = {x["nodeid"]: x for x in got["jobs"][0]["reds"]}
    assert reds["tests/a.py::l"]["class"] == "load" and reds["tests/a.py::l"]["ledger_id"]
    assert reds["tests/a.py::i"]["class"] == "regression" and reds["tests/a.py::i"]["ledger_id"] is None
    assert got["load_reds"] == 1
    assert {d["nodeid"]: d["class"] for d in got["_diag"]}["tests/a.py::i"] == "regression"
    assert [e["nodeid"] for e in led.entries().values()] == ["tests/a.py::i", "tests/a.py::l"]
    assert _g(r, "for-each-ref", "refs/verify") == ""                                  # the temporary ref is gone
    assert not (run / "linux" / "sha.bundle").exists()


def test_remote_leg_fails_loudly_on_a_failed_step_and_on_a_corrupt_copy(repo, tmp_path):
    r, sha = repo
    run = tmp_path / "run"
    run.mkdir()
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    rrun, leg, jt = _remote_fixture(tmp_path)
    with pytest.raises(RuntimeError, match="running the linux leg on argushub failed"):
        verify.run_leg_remote(r, sha, run, RUN_ID, led, None, sh=_stub_sh(r, sha, leg, jt, [], fail_on="verify.py run --here"),
                              script=ROOT / "scripts" / "verify.py")
    with pytest.raises(RuntimeError, match="copied back with sha256"):
        verify.run_leg_remote(r, sha, run, RUN_ID, led, None, sh=_stub_sh(r, sha, leg, jt, [], junit_override="<tampered/>"),
                              script=ROOT / "scripts" / "verify.py")
    assert led.lines() == []                                                           # nothing filed from a failed leg
    bad = json.loads(json.dumps(leg))
    bad["jobs"][0]["junit"]["path"] = "/home/u/x; rm -rf ~"
    with pytest.raises(RuntimeError, match="unsafe junit path"):
        verify.run_leg_remote(r, sha, run, RUN_ID, led, None, sh=_stub_sh(r, sha, bad, jt, []), script=ROOT / "scripts" / "verify.py")


def test_remote_leg_refuses_an_unsafe_run_id(repo, tmp_path):
    r, sha = repo
    with pytest.raises(AssertionError):
        verify.run_leg_remote(r, sha, tmp_path, "x; rm -rf ~", verify.Ledger(tmp_path / "l"), None, sh=lambda *a, **k: 0)


# ---------------------------------------------------------------- diagnosis

def _diag(cls="regression", new=False):
    return {"nodeid": "tests/x.py::t", "class": cls, "evidence": "ev", "text": "AssertionError: boom", "ledger_new": new}


def test_diagnosis_writes_a_file_from_the_text_the_stat_and_the_evidence(repo, tmp_path):
    r, sha = repo
    seen = {}
    run = tmp_path / "run"
    run.mkdir()
    p = verify.diagnose(run, r, sha, [_diag(), _diag("load", False)], call=lambda prompt: seen.setdefault("p", prompt) and "hypothesis: x")
    assert p == run / "diagnosis.md" and "hypothesis: x" in p.read_text() and verify.HAIKU_MODEL in p.read_text()
    assert "AssertionError: boom" in seen["p"] and "tests/test_b.py" in seen["p"] and "evidence: ev" in seen["p"]
    assert seen["p"].count("--- tests/x.py::t") == 1               # the excused load red is not sent


def test_diagnosis_runs_for_a_new_ledger_entry_but_not_for_an_old_one_or_a_clean_run(repo, tmp_path):
    r, sha = repo
    run = tmp_path / "run"
    run.mkdir()
    calls = []
    call = lambda prompt: calls.append(prompt) or "ok"
    assert verify.diagnose(run, r, sha, [], call=call) is None
    assert verify.diagnose(run, r, sha, [_diag("flaky", False)], call=call) is None
    assert verify.diagnose(run, r, sha, [_diag("flaky", True)], call=call) is not None
    assert len(calls) == 1


def test_diagnosis_skips_with_a_printed_line_and_never_raises(repo, tmp_path):
    r, sha = repo
    run = tmp_path / "run"
    run.mkdir()
    said = []

    def no_sdk(_):
        raise ImportError("no anthropic")

    def boom(_):
        raise RuntimeError("api down")

    assert verify.diagnose(run, r, sha, [_diag()], call=no_sdk, say=said.append) is None
    assert verify.diagnose(run, r, sha, [_diag()], call=boom, say=said.append) is None
    assert "SDK is not importable" in said[0] and "api down" in said[1] and not (run / "diagnosis.md").exists()


def test_diagnosis_prompt_is_capped():
    items = [{"nodeid": f"t{i}", "class": "regression", "evidence": "e", "text": "x" * 5000} for i in range(60)]
    prompt = verify.diagnosis_prompt(items, "s" * 9000)
    assert len(prompt) <= verify.DIAG_PROMPT_CAP + 200 and "prompt cap" in prompt


def test_a_diagnosis_is_recorded_on_the_receipt_and_never_changes_the_verdict(repo, tmp_path, monkeypatch):
    r, sha = repo
    bad = [{"nodeid": "tests/x.py::t", "class": "regression", "evidence": "e", "ledger_id": None}]
    monkeypatch.setattr(verify, "Tools", FakeTools)
    monkeypatch.setattr(verify, "run_leg_local", lambda *a, **k: _full_leg("macos", 6, reds=bad, diag=[_diag()]))
    orig = verify.diagnose
    monkeypatch.setattr(verify, "diagnose", lambda run, top, s, d, **k: orig(run, top, s, d, call=lambda p: "advice text"))
    state = tmp_path / "state"
    args = ["--repo", str(r), "--state-dir", str(state), "--receipts-dir", str(tmp_path / "receipts")]      # diagnosis ON
    assert verify.main(["run", "--sha", sha, *args]) == 1
    rec = json.loads((tmp_path / "receipts" / f"vr_{sha[:12]}.json").read_text())
    assert rec["verdict"] == "red" and Path(rec["diagnosis"]).read_text().count("advice text") == 1


def test_host_lock_is_exclusive(tmp_path):
    a = verify.lock_host(tmp_path)
    assert a is not None and verify.lock_host(tmp_path) is None
    a.close()
    b = verify.lock_host(tmp_path)
    assert b is not None
    b.close()
