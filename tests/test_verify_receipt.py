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


def _leg(reds, collected=10, rc=1, platform="macos"):
    return {"platform": platform, "jobs": [{"name": "pytest-full-py312", "rc": rc, "reds": reds,
                                            "counts": dict(collected=collected, passed=0, failed=len(reds), error=0, skipped=0, xfailed=0)}]}


def _both(reds=(), rc=None, linux_reds=()):
    """The two legs a green receipt needs."""
    return [_leg(list(reds), rc=(1 if reds else 0) if rc is None else rc), _leg(list(linux_reds), platform="linux", rc=1 if linux_reds else 0)]


def _red(cls="inherited", lid="lg_1", row="1109-99", nid="t"):
    return {"nodeid": nid, "class": cls, "evidence": "x", "ledger_id": lid, "board_row": row}


def test_red_without_ledger_id_makes_the_receipt_red():
    leg = _leg([{"nodeid": "tests/a.py::t", "class": "load", "evidence": "x", "ledger_id": None}])
    rec = verify.build_receipt("a" * 40, "run1", {}, "blob", [leg], True)
    assert rec["verdict"] == "red" and "red without ledger_id: tests/a.py::t" in rec["verdict_why"]


def test_regression_red_even_with_ledger_id_and_green_when_all_excused():
    bad = _both([_red("regression")])
    assert verify.build_receipt("a" * 40, "r", {}, "b", bad, True)["verdict"] == "red"
    rec = verify.build_receipt("a" * 40, "r", {}, "b", _both([_red("inherited")]), True)
    assert rec["verdict"] == "green" and rec["schema"] == "levain-test-receipt/1"


def test_empty_run_runner_mismatch_and_pytest_crash_are_red():
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], collected=0, rc=0), _leg([], platform="linux", rc=0)], True)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", [_leg([], rc=2), _leg([], platform="linux", rc=0)], True)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, None, _both(), None)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", _both(), False)["verdict"] == "red"
    assert verify.build_receipt("a" * 40, "r", {}, "b", _both(), True)["verdict"] == "green"


def test_receipt_file_name_and_roundtrip(tmp_path):
    rec = verify.build_receipt("0123456789abcdef" * 2 + "01234567", "r", {"uv_lock_sha256": "h"}, "b", _both(), True)
    p = verify.write_receipt(rec, tmp_path / "receipts")
    assert p.name == "vr_0123456789ab_r.json"
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
    reds = [{"nodeid": f"tests/a.py::{k}", "kind": "failure", "text": "AssertionError: x\nbody"} for k in ("load", "flaky", "inh", "reg", "led")]
    reds.append({"nodeid": "tests/touched.py::t", "kind": "failure", "text": "AssertionError: x"})
    seq["t"] = []
    out = verify.classify(reds, ledger=led, run_id="r1", sha="s1", touched={"tests/touched.py"}, rerun=rerun,
                          baseline=lambda nids: {"tests/a.py::inh": {"state": "red", "sig": ["AssertionError", "AssertionError: x"]},
                                                 "tests/a.py::reg": "green"})
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


def _one_receipt(receipts, sha):
    """The one receipt in `receipts`, named for its sha and its run."""
    found = sorted(receipts.glob(f"vr_{sha[:12]}_*.json"))
    assert len(found) == 1, found
    assert found[0].name == f"vr_{sha[:12]}_{json.loads(found[0].read_text())['run_id']}.json"
    return found[0]


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


def test_sweep_cleans_working_dirs_keeps_logs_and_status_for_a_ttl_and_spares_the_rest(tmp_path):
    state = tmp_path / "state"
    runs = state / "runs"

    def mk(name, st, work=True):
        (runs / name).mkdir(parents=True)
        (runs / name / "runner.log").write_text("log " + name)
        if work:
            (runs / name / "src_at_sha" / "venv").mkdir(parents=True)
        verify.Status(runs / name).update(state=st)

    for n, st in (("dead-running", "running"), ("dead-error", "error"), ("dead-legdone", "leg-done"), ("dead-nostate", None),
                  ("finished-red", "red"), ("finished-partial", "partial"), ("finished-passed", "passed"),
                  ("current", "running"), ("fresh-starting", "starting"), ("old-starting", "starting"), ("live", "running"),
                  ("past-ttl", "killed")):
        mk(n, st)
    os.utime(runs / "old-starting" / "status.json", (time.time() - 1000,) * 2)
    past = time.time() - verify.KEPT_LOG_TTL - 100
    os.utime(runs / "past-ttl" / "status.json", (past, past))
    (runs / "dead-bare").mkdir()
    held = verify.take_run_lock(runs / "live")
    tools = FakeTools()
    done = verify.sweep_runs(state, tmp_path, tools, current="current", say=lambda *_: None)
    held.close()
    cleaned = sorted(Path(d["run"]).name for d in done)
    assert cleaned == ["dead-error", "dead-legdone", "dead-nostate", "dead-running", "old-starting", "past-ttl"]
    for n in ("dead-running", "dead-error", "dead-legdone", "dead-nostate", "old-starting"):
        assert (runs / n / "runner.log").read_text() == "log " + n and (runs / n / "status.json").exists(), n
        assert not (runs / n / "src_at_sha").exists(), n                        # only the working subdirs went
    assert verify.read_status(runs / "dead-running")["state"] == "killed"     # a dead 'running' is recorded as killed
    assert verify.read_status(runs / "dead-error")["state"] == "error"
    assert not (runs / "past-ttl").exists()                                    # past the TTL the whole dir goes
    for n in ("finished-red", "finished-partial", "finished-passed", "current", "fresh-starting", "live"):
        assert (runs / n / "src_at_sha").exists() or n.startswith("finished"), n
        assert (runs / n / "runner.log").exists(), n
    again = verify.sweep_runs(state, tmp_path, FakeTools(), current="current", say=lambda *_: None)
    assert {Path(d["run"]).name for d in again} == {"live"}         # 'live' lost its lock above; the swept runs are not redone


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
        rec = json.loads(_one_receipt(tmp_path / "receipts", sha).read_text())
        assert [l["platform"] for l in rec["legs"]] == ["macos", "linux"] and rec["verdict"] == want
        assert rec["runner_blob"] and "_diag" not in rec["legs"][0]
        _one_receipt(tmp_path / "receipts", sha).unlink()


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
    rid = f"20261109T000000Z-{sha[:8]}-ab12"
    run = state / "runs" / rid
    run.mkdir(parents=True)
    assert verify.main(["run", "--sha", sha, "--run-id", rid, *_args(r, state)]) == 3
    assert verify.read_status(run)["state"] == "refused"
    held.close()


# ---------------------------------------------------------------- the linux leg's transport

RUN_ID = "20261109T000000Z-abc12345-ab12"


REMOTE_JUNIT = ('<testsuites><testsuite>'
                '<testcase classname="tests.a" name="l"><failure message="AssertionError: x">t</failure></testcase>'
                '<testcase classname="tests.a" name="i"><failure message="AssertionError: y">t</failure></testcase>'
                '</testsuite></testsuites>')


def _remote_fixture(tmp_path, junit_text=REMOTE_JUNIT, sha="a" * 40):
    rrun = f"{verify.REMOTE_STATE}/runs/{RUN_ID}"
    reds = [{"nodeid": "tests/a.py::l", "class": "load", "evidence": "e1", "ledger_id": None},
            {"nodeid": "tests/a.py::i", "class": "inherited", "evidence": "e2", "ledger_id": None}]
    leg = {"platform": "linux", "sha": sha, "host": "argushub", "python": "3.13.1", "workers": 4, "max_load": 9.0, "load_reds": 1,
           "jobs": [{"name": "pytest-full-py313", "rc": 1, "counts": dict(collected=2, passed=0, failed=2, error=0, skipped=0, xfailed=0),
                     "junit": {"path": f"/home/u/{rrun}/junit-linux.xml", "sha256": hashlib.sha256(junit_text.encode()).hexdigest()},
                     "reds": reds},
                    verify.failed_job("bwrap-floor-live-py312", "host not provisioned: x"),
                    verify.failed_job("root-closure-py39", "uv venv")],
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
    rrun, leg, jt = _remote_fixture(tmp_path, sha=sha)
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
    rrun, leg, jt = _remote_fixture(tmp_path, sha=sha)
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
    rec = json.loads(_one_receipt(tmp_path / "receipts", sha).read_text())
    assert rec["verdict"] == "red" and Path(rec["diagnosis"]).read_text().count("advice text") == 1


def test_host_lock_is_exclusive(tmp_path):
    a = verify.lock_host(tmp_path)
    assert a is not None and verify.lock_host(tmp_path) is None
    a.close()
    b = verify.lock_host(tmp_path)
    assert b is not None
    b.close()


# ================================================================ head rulings V1-V12 (levain 5, 11-09)
# The bar: a receipt never reads green where CI would read red.

SHA40 = "a" * 40
RID = f"20261109T000000Z-{SHA40[:8]}-0001"


# ---- V1: both legs or it is not green

def test_v1_one_leg_is_partial_never_green():
    one = verify.build_receipt(SHA40, "r", {}, "b", [_leg([], rc=0)], True)
    assert one["verdict"] == "partial" and "missing leg: linux" in one["verdict_why"]
    only_linux = verify.build_receipt(SHA40, "r", {}, "b", [_leg([], rc=0, platform="linux")], True)
    assert only_linux["verdict"] == "partial" and "missing leg: macos" in only_linux["verdict_why"]
    assert verify.build_receipt(SHA40, "r", {}, "b", [], True)["verdict"] == "partial"
    assert verify.build_receipt(SHA40, "r", {}, "b", _both(), True)["verdict"] == "green"
    red_and_missing = verify.build_receipt(SHA40, "r", {}, "b", [_leg([_red("regression")])], True)
    assert red_and_missing["verdict"] == "red"                                  # a red outranks a missing leg


def test_v1_a_single_leg_run_exits_nonzero_and_its_status_says_partial(repo, tmp_path, monkeypatch):
    r, sha = repo
    monkeypatch.setattr(verify, "Tools", FakeTools)
    monkeypatch.setattr(verify, "run_leg_local", lambda *a, **k: _full_leg("macos", 6))
    state = tmp_path / "state"
    assert verify.main(["run", "--sha", sha, "--leg", "mac", *_args(r, state)]) == 1
    assert json.loads((_only_run(state) / "status.json").read_text())["state"] == "partial"
    rec = json.loads(_one_receipt(tmp_path / "receipts", sha).read_text())
    assert rec["verdict"] == "partial"


# ---- V2: the laptop re-derives the remote leg from the copied junit

def _junit_leg(path, sha=SHA40):
    parsed = verify.parse_junit(path)
    jobs = [{"name": n, "rc": 1, "counts": dict(parsed["counts"]), "skips_by_reason": dict(parsed["skips_by_reason"]),
             "junit": {"path": str(path), "sha256": "x"}, "reds": [{"nodeid": r["nodeid"], "class": "load"} for r in parsed["reds"]]}
            for n in ("pytest-full-py313", "bwrap-floor-live-py312", "root-closure-py39")]
    return {"platform": "linux", "sha": sha, "jobs": jobs}


def test_v2_rederive_accepts_a_true_leg_and_names_each_lie(junit_run):
    _, j = junit_run
    assert verify.rederive_leg(_junit_leg(j), SHA40) == []
    wrong_sha = _junit_leg(j, sha="b" * 40)
    assert any("records sha" in e for e in verify.rederive_leg(wrong_sha, SHA40))
    no_sha = _junit_leg(j)
    del no_sha["sha"]
    assert any("records sha" in e for e in verify.rederive_leg(no_sha, SHA40))
    lie_counts = _junit_leg(j)
    lie_counts["jobs"][0]["counts"]["failed"] = 0
    assert any("counts" in e for e in verify.rederive_leg(lie_counts, SHA40))
    hidden_red = _junit_leg(j)
    hidden_red["jobs"][0]["reds"].pop()
    assert any("red tests" in e for e in verify.rederive_leg(hidden_red, SHA40))
    bad_path = _junit_leg(j)
    bad_path["jobs"][0]["junit"]["path"] = str(j) + ".missing"
    assert any("does not parse" in e for e in verify.rederive_leg(bad_path, SHA40))


def test_v2_a_remote_leg_that_disagrees_with_its_junit_makes_the_receipt_red(repo, tmp_path):
    r, sha = repo
    run = tmp_path / "run"
    run.mkdir()
    rrun, leg, jt = _remote_fixture(tmp_path, sha=sha)
    ok = verify.run_leg_remote(r, sha, run, RUN_ID, verify.Ledger(tmp_path / "l1.jsonl"), None,
                               sh=_stub_sh(r, sha, leg, jt, []), script=ROOT / "scripts" / "verify.py")
    assert ok["integrity"] == []
    lie = json.loads(json.dumps(leg))
    lie["sha"] = "c" * 40                                              # the remote tested some other commit
    lie["jobs"][0]["counts"]["failed"] = 0
    lie["jobs"][0]["counts"]["passed"] = 2
    got = verify.run_leg_remote(r, sha, run, RUN_ID, verify.Ledger(tmp_path / "l2.jsonl"), None,
                                sh=_stub_sh(r, sha, lie, jt, []), script=ROOT / "scripts" / "verify.py")
    assert len(got["integrity"]) == 2
    rec = verify.build_receipt(sha, "r", {}, "b", [_leg([]), got], True)
    assert rec["verdict"] == "red" and any("records sha" in w for w in rec["verdict_why"])


# ---- V3: pytest's exit code must agree with its junit

def _job_leg(rc, failed=0, error=0, reds=(), platform="linux"):
    return {"platform": platform, "jobs": [{"name": "j", "rc": rc, "reds": list(reds),
                                             "counts": dict(collected=5, passed=5 - failed - error, failed=failed, error=error, skipped=0, xfailed=0)}]}


def test_v3_rc_and_junit_must_agree():
    mac = _leg([], rc=0)
    cases = {
        "rc 0, junit has a failure, no red recorded": _job_leg(0, failed=1),
        "rc 0, junit has an error, red recorded and excused": _job_leg(0, error=1, reds=[_red()]),
        "rc 1, junit has none": _job_leg(1),
        "rc 1, junit has a failure but no red recorded": _job_leg(1, failed=1),
    }
    for what, leg in cases.items():
        v, why = verify.verdict_of([mac, leg])
        assert v == "red", what
    v, why = verify.verdict_of([mac, _job_leg(1, failed=1, reds=[_red()])])
    assert v == "green", why                                         # rc 1 with a recorded, excused red is consistent
    v, why = verify.verdict_of([mac, _job_leg(0)])
    assert v == "green"


# ---- V4: the signal handler cannot deadlock on its own thread's lock

def test_v4_terminate_children_with_the_lock_held_by_this_thread_does_not_hang():
    def alarm(_s, _f):
        raise TimeoutError("terminate_children deadlocked on _CHILD_LOCK")
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(5)
    try:
        with verify._CHILD_LOCK:                 # the main thread is inside _spawn / _run's finally when the signal lands
            verify.terminate_children(grace=0)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ---- V5: the baseline is pinned, recorded, and 'inherited' means the SAME failure

def test_v5_failure_signature_blanks_addresses_and_paths_and_keeps_the_type():
    a = verify.failure_signature("RuntimeError: boom at 0x7f12 in /tmp/pytest-1/x.py\nTraceback")
    b = verify.failure_signature("RuntimeError: boom at 0x99aa in /tmp/pytest-2/x.py\nTraceback")
    assert a == b and a[0] == "RuntimeError"
    assert verify.failure_signature("assert 1 == 2\n...")[0] == ""
    assert verify.failure_signature("KeyError: 'a'") != verify.failure_signature("KeyError: 'b'")


def test_v5_inherited_only_when_main_fails_the_same_way(tmp_path):
    def run(base):
        led = verify.Ledger(tmp_path / f"l-{abs(hash(str(base)))}.jsonl")
        return verify.classify([{"nodeid": "tests/a.py::t", "kind": "failure", "text": "TimeoutError: waited\nx"}], ledger=led,
                               run_id="r", sha="s", touched=set(), rerun=lambda n: False, baseline=lambda n: {"tests/a.py::t": base})[0]
    same = run({"state": "red", "sig": ["TimeoutError", "TimeoutError: waited"]})
    other = run({"state": "red", "sig": ["AssertionError", "AssertionError: assert 1 == 2"]})
    unproven = run("red")                       # a baseline red whose failure was never recorded proves nothing
    assert same["class"] == "inherited" and same["ledger_id"]
    assert other["class"] == "regression" and "different failure" in other["evidence"] and other["ledger_id"] is None
    assert unproven["class"] == "regression" and "unproven" in unproven["evidence"]


def test_v5_fetch_base_updates_the_pinned_ref_from_origin_and_fails_loud_when_it_cannot(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    one, two = tmp_path / "one", tmp_path / "two"
    for d in (one, two):
        subprocess.run(["git", "clone", "-q", str(origin), str(d)], check=True, capture_output=True)
    (one / "f").write_text("1")
    _g(one, "add", "-A")
    _g(one, "commit", "-q", "-m", "c1")
    _g(one, "push", "-q", "origin", "HEAD:main")
    assert verify.fetch_base(two) == _g(one, "rev-parse", "HEAD")
    (one / "f").write_text("2")
    _g(one, "commit", "-q", "-am", "c2")
    _g(one, "push", "-q", "origin", "HEAD:main")
    newest = _g(one, "rev-parse", "HEAD")
    assert _g(two, "rev-parse", "refs/remotes/origin/main") != newest        # stale until fetched
    assert verify.fetch_base(two) == newest and _g(two, "rev-parse", "refs/remotes/origin/main") == newest
    _g(two, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(RuntimeError, match="stale"):
        verify.fetch_base(two)


class JobHarness:
    """run_job with pytest faked at `_run`: it writes the junit each call would have written."""

    def __init__(self, tmp_path, monkeypatch, *, main_msg="AssertionError: x", base_msg="AssertionError: x", rerun="fail",
                 base_rc=1):
        self.base_rc = base_rc
        self.run = tmp_path / "run"
        for d in ("home", "tmp", "bsrc"):
            (self.run / d if d != "bsrc" else tmp_path / d).mkdir(parents=True, exist_ok=True)
        self.bsrc = tmp_path / "bsrc"
        self.led = verify.Ledger(tmp_path / "ledger.jsonl")
        self.ctx = verify.Ctx(tmp_path, "s" * 40, self.run, "rid", self.led, None, "mac", self.run / "v.log")
        self.ctx.base_sha, self.ctx.touched = "b" * 40, set()
        self.msgs = dict(main=main_msg, base=base_msg)
        self.rerun, self.cmds = rerun, []
        monkeypatch.setattr(verify, "_run", self.fake_run)
        monkeypatch.setattr(self.ctx, "main_env", lambda python=None: (self.bsrc, self.bsrc / "bin" / "python"))

    @staticmethod
    def _xml(tests):
        body = "".join(f'<testcase classname="tests.a" name="{n}">{inner}</testcase>' for n, inner in tests)
        return f"<testsuites><testsuite>{body}</testsuite></testsuites>"

    def fake_run(self, cmd, *, cwd, env, log, timeout=None, stdin=None):
        self.cmds.append((cmd, env))
        j = next(c.split("=", 1)[1] for c in cmd if c.startswith("--junitxml="))
        base = next(c for c in cmd if c.startswith("--basetemp="))
        fail = lambda m: f'<failure message="{m}">tb</failure>'
        if "pt-main-" in base:
            Path(j).write_text(self._xml([("t", fail(self.msgs["base"]))]))
            return self.base_rc
        if "-r" in base.rsplit("/", 1)[-1]:                       # a serial rerun of one nodeid
            if self.rerun == "skip":
                Path(j).write_text(self._xml([("t", '<skipped type="pytest.skip" message="needs a keychain"/>')]))
                return 0
            Path(j).write_text(self._xml([("t", fail("AssertionError: x"))]))
            return 1
        Path(j).write_text(self._xml([("ok", ""), ("t", fail(self.msgs["main"]))]))
        return 1

    def job(self):
        return verify.run_job(self.ctx, name="pytest-full-py312", py=Path("/x/py"), cwd=self.run, env={"PATH": "/p", "CI": "1"},
                              targets=["tests"], junit_name="junit-mac.xml", workers=2, timeout=60)


def test_v5_the_job_records_the_baseline_sha_and_junit_hash_and_judges_sameness(tmp_path, monkeypatch):
    same = JobHarness(tmp_path / "a", monkeypatch)
    job, _ = same.job()
    assert job["reds"][0]["class"] == "inherited"
    bj = same.run / "junit-main-pytest-full-py312.xml"
    assert job["baseline"] == {"sha": "b" * 40, "rc": 1, "junit_sha256": hashlib.sha256(bj.read_bytes()).hexdigest(), "junit": str(bj)}
    diff = JobHarness(tmp_path / "b", monkeypatch, base_msg="TimeoutError: other")
    job2, _ = diff.job()
    assert job2["reds"][0]["class"] == "regression" and job2["baseline"]["sha"] == "b" * 40


# ---- V7: a skip is not a pass; a torn last ledger line is survivable, any other is not

def test_v7_a_rerun_passes_only_when_the_junit_says_passed(tmp_path, monkeypatch):
    p = tmp_path / "j.xml"
    t = lambda inner: p.write_text(JobHarness._xml([("t", inner)]))
    t("")
    assert verify.rerun_passed(p)
    t('<skipped type="pytest.skip" message="x"/>')
    assert not verify.rerun_passed(p)
    t('<skipped type="pytest.xfail" message="x"/>')
    assert not verify.rerun_passed(p)
    t('<failure message="x">t</failure>')
    assert not verify.rerun_passed(p)
    assert not verify.rerun_passed(tmp_path / "missing.xml")
    skipping = JobHarness(tmp_path / "s", monkeypatch, rerun="skip")           # rc 0 and every rerun skipped
    job, _ = skipping.job()
    assert job["reds"][0]["class"] != "load"           # three skipped reruns are no pass: it falls to the baseline, not to 'load'


def test_v7_a_torn_last_ledger_line_is_skipped_with_a_warning_and_any_other_fails_closed(tmp_path, capsys):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    lid, _ = led.file("tests/a.py::t", "flaky", "r1", "s1", "e")
    good = led.path.read_text()
    led.path.write_text(good + '{"type": "sighting", "id": "lg_x", "ru')               # the writer died mid-line
    assert [r["type"] for r in led.lines()] == ["entry"]
    assert "torn last line" in capsys.readouterr().err
    led.file("tests/b.py::t", "flaky", "r2", "s2", "e")                                # the next append drops the torn tail
    assert [r["type"] for r in led.lines()] == ["entry", "entry"] and led.path.read_text().startswith(good)
    assert led.path.read_text().endswith("\n") and "torn" not in led.path.read_text()
    lines = led.path.read_text().splitlines()
    led.path.write_text("\n".join([lines[0], "{not json", lines[1]]) + "\n")             # a hole in the middle
    with pytest.raises(RuntimeError, match="failing closed"):
        led.lines()
    with pytest.raises(RuntimeError, match="failing closed"):
        led.entries()


# ---- V8: a laptop kill stops the remote run; a dead holder's host lock is reclaimed

def test_v8_the_remote_stop_command_signals_only_this_runs_runner():
    cmd = verify.remote_stop_cmd("argushub", RUN_ID, remove_dir=True)
    assert cmd[:2] == ["ssh", "-o"] and cmd[-2] == "argushub"
    script = cmd[-1]
    assert f"{verify.REMOTE_STATE}/runs/{RUN_ID}/status.json" in script and f"--run-id {RUN_ID}" in script
    assert "kill -TERM" in script and "/proc/$pid/cmdline" in script and f"rm -rf {verify.REMOTE_STATE}/runs/{RUN_ID}" in script
    assert "rm -rf" not in verify.remote_stop_cmd("argushub", RUN_ID, remove_dir=False)[-1]
    with pytest.raises(AssertionError):
        verify.remote_stop_cmd("argushub", "x; rm -rf ~", remove_dir=True)


def test_v8_a_failed_remote_step_stops_the_remote_run_and_keeps_its_logs(repo, tmp_path):
    r, sha = repo
    run = tmp_path / "run"
    run.mkdir()
    rrun, leg, jt = _remote_fixture(tmp_path, sha=sha)
    cmds = []
    with pytest.raises(RuntimeError, match="running the linux leg"):
        verify.run_leg_remote(r, sha, run, RUN_ID, verify.Ledger(tmp_path / "l.jsonl"), None,
                              sh=_stub_sh(r, sha, leg, jt, cmds, fail_on="verify.py run --here"), script=ROOT / "scripts" / "verify.py")
    last = cmds[-1][0]
    assert last[-1] == verify.remote_stop_cmd("argushub", RUN_ID, remove_dir=False)[-1] and "kill -TERM" in last[-1]


def test_v8_a_laptop_kill_during_both_legs_stops_the_remote_run(monkeypatch, tmp_path):
    stopped = []
    monkeypatch.setattr(verify, "stop_remote_run", lambda host, rid: stopped.append((host, rid)))
    monkeypatch.setattr(verify, "terminate_children", lambda *a, **k: None)

    def local(*a, **k):
        raise verify.Killed(int(signal.SIGTERM))

    monkeypatch.setattr(verify, "run_leg_local", local)
    monkeypatch.setattr(verify, "run_leg_remote", lambda *a, **k: _full_leg("linux", 4))
    a = types.SimpleNamespace(leg="both", host="argushub", here=False, workers=None, linux_workers=None, python=None, pytest_timeout=5.0)
    with pytest.raises(verify.Killed):
        verify._run_legs(a, tmp_path, SHA40, tmp_path, RUN_ID, verify.Ledger(tmp_path / "l"), verify.Status(tmp_path))
    assert stopped == [("argushub", RUN_ID)]


def test_v8_a_host_lock_whose_holder_is_dead_is_reclaimed_but_a_live_one_is_not(tmp_path):
    held = verify.lock_host(tmp_path / "live")
    assert verify.lock_host(tmp_path / "live") is None                  # this very process holds it: refused
    held.close()
    leaked = tmp_path / "leak"
    leaked.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    fd = open(leaked / "host.lock", "w")                                 # a descriptor leaked into a survivor keeps the flock
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fd.write(f"{dead.pid} then\n")
    fd.flush()
    mine = verify.lock_host(leaked)
    assert mine is not None and mine is not fd
    assert (leaked / "host.lock").read_text().split()[0] == str(os.getpid())
    mine.close()
    fd.close()


# ---- V9: --run-id has the generated format

def test_v9_run_id_must_be_the_generated_format_for_this_sha(repo, tmp_path, capsys):
    r, sha = repo
    for bad in ("pre", "../../etc", "20261109T000000Z-deadbeef-ab12", f"20261109T000000Z-{sha[:8]}-AB12", f"20261109T000000Z-{sha[:8]}-ab12; x"):
        assert verify.main(["run", "--sha", sha, "--run-id", bad, *_args(r, tmp_path / "s")]) == 2, bad
        assert "--run-id" in capsys.readouterr().err
    assert not (tmp_path / "s").exists()
    assert verify.RUN_ID_RE.fullmatch(verify.new_run_id(sha))


# ---- V10: CI parity: CI=1, and what CI provisions is provisioned in the run dir or the leg fails loud

def test_v10_the_legs_run_with_ci_set_and_github_actions_unset(tmp_path):
    env = verify.clean_env(tmp_path)
    assert env["CI"] == "1" and "GITHUB_ACTIONS" not in env


def _prov_ctx(tmp_path):
    run = tmp_path / "run"
    run.mkdir(parents=True)
    return verify.Ctx(tmp_path, SHA40, run, "rid", None, None, "linux", run / "log")


def test_v10_h11_is_installed_into_the_run_dir_and_probed_through_it(tmp_path, monkeypatch):
    ctx = _prov_ctx(tmp_path)
    seen = {}
    monkeypatch.setattr(verify, "_capture", lambda cmd, **k: (0, "h11>=0.16.0\n"))
    monkeypatch.setattr(verify, "_run", lambda cmd, **k: seen.setdefault("pip", cmd) and 0)
    monkeypatch.setattr(verify.os.path, "exists", lambda p: True)
    monkeypatch.setattr(verify.subprocess, "run", lambda cmd, **k: seen.update(probe=(cmd, k["env"])) or types.SimpleNamespace(returncode=0, stderr=""))
    site, why = verify.provision_h11(ctx, Path("/venv/bin/python"), tmp_path, {"PATH": "/p", "CI": "1"})
    assert why is None and site == ctx.run / "h11site"
    assert seen["pip"][-3:] == ["--target", str(site), "h11>=0.16.0"] and "--no-deps" in seen["pip"]
    assert seen["probe"][0] == ["/usr/bin/python3", "-c", "import h11"] and seen["probe"][1]["PYTHONPATH"] == str(site)


def test_v10_a_leg_that_cannot_provision_h11_fails_loud_naming_it(tmp_path, monkeypatch):
    ctx = _prov_ctx(tmp_path)
    monkeypatch.setattr(verify, "_capture", lambda cmd, **k: (0, "h11>=0.16.0\n"))
    monkeypatch.setattr(verify, "_run", lambda cmd, **k: 0)
    monkeypatch.setattr(verify.os.path, "exists", lambda p: False)
    site, why = verify.provision_h11(ctx, Path("/venv/bin/python"), tmp_path, {})
    assert site is None and "h11" in why and "/usr/bin/python3" in why and "not provisioned" in why
    monkeypatch.setattr(verify, "_run", lambda cmd, **k: 1)                 # pip itself fails
    assert verify.provision_h11(ctx, Path("/venv/bin/python"), tmp_path, {})[0] is None

    monkeypatch.setattr(verify, "provision_h11", lambda *a, **k: (None, "h11 for /usr/bin/python3 not provisioned: test"))
    monkeypatch.setattr(verify.Ctx, "prepare", lambda self: None)
    monkeypatch.setattr(verify.Ctx, "make", lambda self, name, sha, py, lock_src=None: (tmp_path / "src", tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setattr(verify, "venv_version", lambda *a, **k: "3.12.1")
    monkeypatch.setattr(verify, "run_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no job may run unprovisioned")))
    for plan in (verify.plan_mac, verify.plan_linux):
        jobs, diag, _ = plan(_prov_ctx(tmp_path / plan.__name__), "3.12", 2, 60)
        assert [j["name"] for j in jobs] == ["ci-parity"] and "not provisioned" in jobs[0]["setup_failed"]
        assert verify.verdict_of([{"platform": "x", "jobs": jobs}])[0] == "red"


def test_v10_the_jobs_receive_ci_and_the_provisioned_site(tmp_path, monkeypatch):
    got = []
    site = tmp_path / "site"
    monkeypatch.setattr(verify, "provision_h11", lambda *a, **k: (site, None))
    monkeypatch.setattr(verify.Ctx, "prepare", lambda self: None)
    monkeypatch.setattr(verify.Ctx, "make", lambda self, name, sha, py, lock_src=None: (tmp_path / name, tmp_path / name / "bin" / "python"))
    monkeypatch.setattr(verify, "venv_version", lambda *a, **k: "3.12.1")
    monkeypatch.setattr(verify, "run_job", lambda ctx, **k: got.append(k) or ({"name": k["name"], "reds": [], "counts": {}}, []))
    verify.plan_mac(_prov_ctx(tmp_path / "m"), "3.12.13", 2, 60)
    assert got[0]["env"]["CI"] == "1" and got[0]["env"]["PYTHONPATH"] == str(site)


# ---- V11: a red rides only with a board row

def test_v11_a_first_seen_excused_red_is_not_green_until_a_row_is_attached(tmp_path):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    red = {"nodeid": "tests/a.py::t", "kind": "failure", "text": "AssertionError: x"}

    def run(rid):
        return verify.classify([dict(red)], ledger=led, run_id=rid, sha="s", touched=set(), rerun=lambda n: True, baseline=lambda n: {})

    first = run("r1")
    assert first[0]["class"] == "load" and first[0]["ledger_new"] and first[0]["board_row"] is None
    v, why = verify.verdict_of(_both(first))
    assert v == "red" and any("no board row" in w and "attach-row" in w and first[0]["ledger_id"] in w for w in why)
    led.attach_row(first[0]["ledger_id"], "1109-42")
    second = run("r2")                                                       # the re-run meets the same entry, row attached
    assert second[0]["ledger_id"] == first[0]["ledger_id"] and second[0]["board_row"] == "1109-42" and not second[0]["ledger_new"]
    assert verify.verdict_of(_both(second))[0] == "green"


def test_v11_a_remote_leg_gets_its_board_rows_from_the_laptop_ledger(tmp_path):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    leg = {"platform": "linux", "_diag": [], "jobs": [{"name": "j", "reds": [
        {"nodeid": "tests/a.py::l", "class": "load", "evidence": "e", "ledger_id": None, "board_row": None}]}]}
    verify.apply_ledger_to_leg(leg, led, "r1", "s")
    red = leg["jobs"][0]["reds"][0]
    assert red["ledger_id"] and red["board_row"] is None
    led.attach_row(red["ledger_id"], "row-7")
    verify.apply_ledger_to_leg(leg, led, "r2", "s")
    assert leg["jobs"][0]["reds"][0]["board_row"] == "row-7"


def test_v11_attach_row_cli_and_reverdict_turn_a_red_receipt_green(tmp_path, capsys):
    state = tmp_path / "state"
    led = verify.Ledger(state / "ledger.jsonl")
    lid, _ = led.file("tests/a.py::t", "load", "r1", SHA40, "e")
    mac = _leg([{"nodeid": "tests/a.py::t", "class": "load", "evidence": "e", "ledger_id": lid, "board_row": None}])
    receipt = verify.build_receipt(SHA40, RID, {}, "blob", [mac, _leg([], platform="linux", rc=0)], True)
    assert receipt["verdict"] == "red"
    rpath = verify.write_receipt(receipt, tmp_path / "receipts")
    run = state / "runs" / RID
    verify.Status(run).update(state="red", receipt=str(rpath), verdict="red")
    assert verify.main(["reverdict", "--run", RID, "--state-dir", str(state)]) == 1          # no row yet: still red
    assert verify.main(["ledger", "attach-row", "--id", "lg_nope", "--row", "r", "--state-dir", str(state)]) == 2
    assert verify.main(["ledger", "attach-row", "--id", lid, "--row", "1109-42", "--state-dir", str(state)]) == 0
    assert verify.main(["reverdict", "--run", RID, "--state-dir", str(state)]) == 0
    new = json.loads(rpath.read_text())
    assert new["verdict"] == "green" and new["legs"][0]["jobs"][0]["reds"][0]["board_row"] == "1109-42"
    assert [x["to"] for x in new["reverdicts"]] == ["red", "green"]
    assert verify.read_status(run)["state"] == "passed"
    old = dict(new)
    del old["runner_matches_sha"]
    with pytest.raises(ValueError, match="predates"):
        verify.reverdict_receipt(old, led)


# ---- V12: host preflight before the bwrap job; the bwrap job runs on 3.12

def _probe(fail=()):
    def probe(cmd, env=None, timeout=60):
        key = cmd[0]
        if key in fail:
            return 1, ""
        return 0, "Linger=yes\n" if key == "loginctl" else ""
    return probe


def test_v12_preflight_names_each_missing_capability_and_nothing_when_ready():
    ok = dict(which=lambda n: "/usr/bin/" + n, exists=lambda p: True, access=lambda p, m: True, uid=1000, user="u")
    assert verify.preflight_linux(_probe(), **ok) == []
    assert "bubblewrap" in verify.preflight_linux(_probe(), **{**ok, "which": lambda n: None})[0]
    assert "user namespace" in verify.preflight_linux(_probe(fail=("bwrap",)), **ok)[0]
    assert "linger" in verify.preflight_linux(_probe(fail=("loginctl",)), **ok)[0]
    assert "systemd-run" in verify.preflight_linux(_probe(fail=("systemd-run",)), **ok)[0]
    no_linger = lambda cmd, env=None, timeout=60: (0, "Linger=no\n") if cmd[0] == "loginctl" else (0, "")
    assert "linger" in verify.preflight_linux(no_linger, **ok)[0]
    assert "/run/podman" in verify.preflight_linux(_probe(), **{**ok, "access": lambda p, m: False})[0]
    assert verify.preflight_linux(_probe(), **{**ok, "exists": lambda p: False, "access": lambda p, m: False}) == []   # absent is fine
    several = verify.preflight_linux(_probe(fail=("bwrap", "systemd-run")), **{**ok, "access": lambda p, m: False})
    assert len(several) == 3


def _plan_linux_stubs(tmp_path, monkeypatch, missing):
    made, ran = [], []
    monkeypatch.setattr(verify, "provision_h11", lambda *a, **k: (tmp_path / "site", None))
    monkeypatch.setattr(verify, "preflight_linux", lambda *a, **k: list(missing))
    monkeypatch.setattr(verify.Ctx, "prepare", lambda self: None)

    def make(self, name, sha, py, lock_src=None):
        made.append((name, py))
        return tmp_path / name, tmp_path / name / "bin" / "python"

    monkeypatch.setattr(verify.Ctx, "make", make)
    monkeypatch.setattr(verify, "venv_version", lambda *a, **k: "3.13.1")
    monkeypatch.setattr(verify, "_run", lambda *a, **k: 0)
    monkeypatch.setattr(verify, "_capture", lambda *a, **k: (0, "tests/x.py\n"))
    monkeypatch.setattr(verify, "run_job", lambda ctx, **k: ran.append(k) or ({"name": k["name"], "reds": [], "counts": dict(collected=1, passed=1, failed=0, error=0, skipped=0, xfailed=0), "rc": 0}, []))
    return made, ran


def test_v12_a_host_missing_a_capability_fails_the_bwrap_job_as_not_provisioned(tmp_path, monkeypatch):
    made, ran = _plan_linux_stubs(tmp_path, monkeypatch, ["loginctl linger is not enabled for u", "/run/podman is not readable by u"])
    jobs, _, _ = verify.plan_linux(_prov_ctx(tmp_path / "p"), verify.LINUX_PYTHON, 4, 60)
    bw = next(j for j in jobs if j["name"].startswith("bwrap-floor-live"))
    assert bw["setup_failed"].startswith("host not provisioned: loginctl linger is not enabled for u; /run/podman")
    assert [k["name"] for k in ran] == ["pytest-full-py313", "root-closure-py39"]      # the bwrap job was not run
    assert not any(n == "src_bwrap" for n, _ in made)
    v, why = verify.verdict_of([{"platform": "linux", "jobs": jobs}])
    assert v == "red" and any("host not provisioned" in w for w in why) and not any(w.startswith("regression") for w in why)


def test_v12_the_bwrap_job_runs_on_312_like_ci_when_the_host_is_ready(tmp_path, monkeypatch):
    made, ran = _plan_linux_stubs(tmp_path, monkeypatch, [])
    jobs, _, _ = verify.plan_linux(_prov_ctx(tmp_path / "p"), verify.LINUX_PYTHON, 4, 60)
    assert ("src_bwrap", "3.12") in made and ("src_at_sha", "3.13") in made
    bw = next(k for k in ran if k["name"].startswith("bwrap-floor-live"))
    assert bw["name"] == "bwrap-floor-live-py312" and bw["required_run"] and bw["env"]["LEVAIN_REQUIRE_LINUX_LIVE"] == "1"
    assert bw["base_python"] == "3.12" and bw["env"]["CI"] == "1" and "PYTHONPATH" not in bw["env"]


# ---------------------------------------------------------------- L3 r1 fix round

LINUX_JOBS = ["pytest-full-py313", "bwrap-floor-live-py312", "root-closure-py39"]
CLEAN_JUNIT = '<testsuites><testsuite><testcase classname="tests.a" name="ok"/></testsuite></testsuites>'


def _clean_linux_leg(sha, names=LINUX_JOBS):
    rrun = f"{verify.REMOTE_STATE}/runs/{RUN_ID}"
    h = hashlib.sha256(CLEAN_JUNIT.encode()).hexdigest()
    jobs = [{"name": n, "rc": 0, "required_run": False, "counts": dict(collected=1, passed=1, failed=0, error=0, skipped=0, xfailed=0),
             "skips_by_reason": {}, "junit": {"path": f"/home/u/{rrun}/junit-{n}-{i}.xml", "sha256": h}, "reds": []}
            for i, n in enumerate(names)]
    return {"platform": "linux", "sha": sha, "host": "argushub", "python": "3.13.1", "workers": 4, "max_load": 1.0,
            "load_reds": 0, "jobs": jobs, "_diag": []}


def _xml_sh(leg):
    def sh(cmd, *, log, stdin=None, timeout=None):
        if cmd[0] == "scp" and cmd[-2].endswith("leg-linux.json"):
            (Path(cmd[-1]) / "leg-linux.json").write_text(json.dumps(leg))
        elif cmd[0] == "scp" and cmd[-2].endswith(".xml"):
            (Path(cmd[-1]) / os.path.basename(cmd[-2])).write_text(CLEAN_JUNIT)
        return 0
    return sh


def test_r1_v2_a_remote_leg_without_its_expected_job_set_is_never_green(repo, tmp_path):
    r, sha = repo
    mac = _leg([], rc=0)

    def remote(i, leg):
        run = tmp_path / f"run{i}"
        run.mkdir()
        return verify.run_leg_remote(r, sha, run, RUN_ID, verify.Ledger(tmp_path / f"l{i}.jsonl"), None, sh=_xml_sh(leg),
                                     script=ROOT / "scripts" / "verify.py")

    full = remote(0, _clean_linux_leg(sha))
    assert full["integrity"] == [] and verify.verdict_of([mac, full])[0] == "green"
    for i, names in enumerate(([], LINUX_JOBS[:1], LINUX_JOBS + LINUX_JOBS[:1], LINUX_JOBS[:2] + ["some-other-job"]), 1):
        got = remote(i, _clean_linux_leg(sha, names))
        v, why = verify.verdict_of([mac, got])
        assert got["integrity"] and v == "red", (names, why)
    unrun = _clean_linux_leg(sha)
    unrun["jobs"][1] = verify.failed_job(LINUX_JOBS[1], "host not provisioned: x")   # a named setup failure is red by itself
    got = remote(9, unrun)
    assert got["integrity"] == [] and verify.verdict_of([mac, got])[0] == "red"


def test_r1_v5_an_abnormal_or_self_contradicting_baseline_never_excuses_a_red(tmp_path, monkeypatch):
    for base_rc in (2, 4, 0):                      # an interrupted or crashed run, and a pass that holds a failure
        h = JobHarness(tmp_path / f"rc{base_rc}", monkeypatch, base_rc=base_rc)
        job, _ = h.job()
        red = job["reds"][0]
        assert red["class"] == "regression" and red["ledger_id"] is None, (base_rc, red["class"], red["evidence"])
        assert "baseline" in red["evidence"] and job["baseline"]["rc"] == base_rc
    ok = JobHarness(tmp_path / "ok", monkeypatch)
    job, _ = ok.job()
    assert job["reds"][0]["class"] == "inherited" and job["baseline"]["rc"] == 1


def test_r1_v7_a_newline_terminated_bad_last_record_fails_closed(tmp_path, capsys):
    led = verify.Ledger(tmp_path / "ledger.jsonl")
    led.file("tests/a.py::t", "flaky", "r1", "s1", "e")
    good = led.path.read_text()
    for tail in ("{not json\n", '{"no_type": 1}\n', '["x"]\n'):
        led.path.write_text(good + tail)               # complete lines: the writer finished them, so they are not torn
        with pytest.raises(RuntimeError, match="failing closed"):
            led.lines()
    led.path.write_text(good + '{"type": "sighting", "id": "lg_x", "ru')
    assert [x["type"] for x in led.lines()] == ["entry"]
    assert "torn last line" in capsys.readouterr().err


def _kill_in_join(monkeypatch, tmp_path, leg):
    stopped, released, mac_done = [], threading.Event(), threading.Event()
    monkeypatch.setattr(verify, "stop_remote_run", lambda host, rid: stopped.append((host, rid)))
    monkeypatch.setattr(verify, "terminate_children", lambda *a, **k: released.set())

    def local(*a, **k):
        mac_done.set()
        return _full_leg("macos", 6)

    def remote(*a, **k):
        if leg == "both":
            mac_done.wait(10)
        time.sleep(0.3)                                # the main thread is now waiting on this thread
        os.kill(os.getpid(), signal.SIGTERM)
        released.wait(10)
        return _full_leg("linux", 4)

    monkeypatch.setattr(verify, "run_leg_local", local)
    monkeypatch.setattr(verify, "run_leg_remote", remote)
    return stopped, released


@pytest.mark.parametrize("leg", ["linux", "both"])
def test_r1_v8_a_kill_while_waiting_on_the_remote_leg_stops_the_remote_run(monkeypatch, tmp_path, leg):
    stopped, released = _kill_in_join(monkeypatch, tmp_path, leg)
    a = types.SimpleNamespace(leg=leg, host="argushub", here=False, workers=None, linux_workers=None, python=None, pytest_timeout=5.0)
    prev = verify.install_signal_handlers()
    try:
        with pytest.raises(verify.Killed):
            verify._run_legs(a, tmp_path, SHA40, tmp_path, RUN_ID, verify.Ledger(tmp_path / "l"), verify.Status(tmp_path))
    finally:
        verify.restore_signal_handlers(prev)
        released.set()
    assert stopped == [("argushub", RUN_ID)]


def test_r1_v8_a_linux_run_killed_in_the_join_exits_killed_and_stops_the_remote_once(repo, tmp_path, monkeypatch):
    r, sha = repo
    monkeypatch.setattr(verify, "Tools", FakeTools)
    stopped, released = _kill_in_join(monkeypatch, tmp_path, "linux")
    state = tmp_path / "state"
    try:
        assert verify.main(["run", "--sha", sha, "--leg", "linux", *_args(r, state)]) == 128 + int(signal.SIGTERM)
    finally:
        released.set()
    run = _only_run(state)
    assert stopped == [("argushub", run.name)] and verify.read_status(run)["state"] == "killed"


def test_r1_v8_a_kill_that_escapes_the_leg_runner_is_still_stopped_by_the_run(repo, tmp_path, monkeypatch):
    r, sha = repo
    stopped = []
    monkeypatch.setattr(verify, "Tools", FakeTools)
    monkeypatch.setattr(verify, "stop_remote_run", lambda host, rid: stopped.append((host, rid)))

    def legs(*a, remote=None, **k):
        remote["started"] = True                       # the remote leg is live and nothing below stopped it
        raise verify.Killed(int(signal.SIGTERM))

    monkeypatch.setattr(verify, "_run_legs", legs)
    state = tmp_path / "state"
    assert verify.main(["run", "--sha", sha, "--leg", "linux", *_args(r, state)]) == 128 + int(signal.SIGTERM)
    assert stopped == [("argushub", _only_run(state).name)]


def test_r1_v11_two_runs_of_one_sha_keep_two_receipts_and_reverdict_reads_only_its_own(tmp_path, capsys):
    state = tmp_path / "state"
    rid_a, rid_b = f"20261109T000000Z-{SHA40[:8]}-aaaa", f"20261109T000001Z-{SHA40[:8]}-bbbb"
    led = verify.Ledger(state / "ledger.jsonl")
    lid, _ = led.file("tests/a.py::t", "load", rid_a, SHA40, "e")
    red_mac = _leg([{"nodeid": "tests/a.py::t", "class": "load", "evidence": "e", "ledger_id": lid, "board_row": None}])
    pa = verify.write_receipt(verify.build_receipt(SHA40, rid_a, {}, "blob", [red_mac, _leg([], platform="linux", rc=0)], True),
                              tmp_path / "receipts")
    pb = verify.write_receipt(verify.build_receipt(SHA40, rid_b, {}, "blob", _both(), True), tmp_path / "receipts")
    assert pa != pb and json.loads(pa.read_text())["run_id"] == rid_a and json.loads(pb.read_text())["run_id"] == rid_b
    verify.Status(state / "runs" / rid_b).update(state="passed", receipt=str(pa), verdict="green")    # B's status names A's receipt
    assert verify.main(["reverdict", "--run", rid_b, "--state-dir", str(state)]) == 2
    assert "run_id" in capsys.readouterr().err
    assert json.loads(pa.read_text())["verdict"] == "red" and "reverdicts" not in json.loads(pa.read_text())


def test_r1_run_arguments_must_name_a_run_directly_under_runs(tmp_path, capsys):
    state = tmp_path / "state"
    (state / "runs").mkdir(parents=True)
    outside = tmp_path / "outside"
    rp = verify.write_receipt(verify.build_receipt(SHA40, "outside", {}, "blob", _both(), True), tmp_path / "rc")
    verify.Status(outside).update(state="red", receipt=str(rp), verdict="red")
    good_name = f"20261109T000000Z-{SHA40[:8]}-cccc"
    (state / "runs" / good_name).symlink_to(outside, target_is_directory=True)        # a well-formed id that leaves runs/
    for bad in (str(outside), "../../outside", "outside", good_name + "/..", good_name):
        assert verify.main(["status", "--run", bad, "--state-dir", str(state)]) == 2, bad
        assert verify.main(["reverdict", "--run", bad, "--state-dir", str(state)]) == 2, bad
    assert "reverdicts" not in json.loads(rp.read_text())


def test_r1_the_bwrap_job_and_its_baseline_get_the_runtime_dir_preflight_probed(tmp_path, monkeypatch):
    made, ran = _plan_linux_stubs(tmp_path, monkeypatch, [])
    verify.plan_linux(_prov_ctx(tmp_path / "p"), verify.LINUX_PYTHON, 4, 60)
    bw = next(k for k in ran if k["name"].startswith("bwrap-floor-live"))
    want = f"/run/user/{os.getuid()}"
    assert bw["env"]["XDG_RUNTIME_DIR"] == want
    assert bw["base_cfg"](tmp_path / "bsrc", tmp_path / "bsrc" / "bin" / "python")[1]["XDG_RUNTIME_DIR"] == want
