#!/usr/bin/env python3
"""Local verification receipt runner (design: L2_local_receipt_design.md; levain rows 164 + 172).

usage: python scripts/verify.py run --sha S [--leg mac|linux|both]

Builds a clean environment at the exact sha (fresh git worktree, fresh uv venv from uv.lock via
scripts/ci_install_locked.sh), runs the full suite, classifies every red deterministically (no model), appends the
excused ones to a laptop-local append-only ledger, and writes a `levain-test-receipt/1` file.  This session builds the
mac leg only; `linux` and `both` exit non-zero until the argushub leg exists.

Signed rulings this builds to (Phill, 2026-11-09): mac = Python 3.12 on the laptop, linux = 3.13 on argushub, both on
every landing (A); an inherited red may ride only with a ledger entry AND a board row (B); the ledger is a laptop-local
file with one writer (C); the cloud-only jobs still gate beside the receipt and are not run here (D); no docs-only skip,
every landing runs the full receipt (E).

CI environment decision: CI is NOT set.  Four test files read it (test_broker_capability_ast, test_repo_tracks_no_
project_memory, tests/b2_keys_rig.py, tests/chat_egress/conftest.py) and every one turns a host-provisioning skip into a
failure (h11 for /usr/bin/python3, srt) or switches on CI-only behaviour (chat_egress_network tests are skipped in CI).
The laptop is not provisioned the way test.yml provisions a runner, so CI=1 would manufacture reds that are about the
host, not the commit.  Skips are instead recorded by reason (skips_by_reason) so the receipt consumer can see exactly
what did not run.  GITHUB_ACTIONS is likewise unset.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

SCHEMA = "levain-test-receipt/1"
MAC_PYTHON = "3.12.13"          # pinned full version; uv-managed so it is byte-identical wherever uv fetched it
WORKERS_MAC = 6
WAIT_SCALE = "2"
RERUNS = 3
MAIN_REF = "origin/main"
DEFAULT_STATE = Path.home() / ".local" / "state" / "levain-verify"
CLEAN_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CLASSES = ("regression", "inherited", "load", "flaky")


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def git(repo: Path, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=text, check=check, env=env)


def git_show_bytes(repo: Path, sha: str, path: str) -> bytes | None:
    r = git(repo, "show", f"{sha}:{path}", check=False, text=False)
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------- junit

def _nodeid(classname: str, name: str, root: Path | None) -> str:
    parts = [p for p in classname.split(".") if p] if classname else []
    k = None
    if root is not None:
        for i in range(len(parts), 0, -1):
            if (root / ("/".join(parts[:i]) + ".py")).is_file():
                k = i
                break
    if k is None:   # no checkout to resolve against: the first Capitalised part starts the class chain
        k = next((i for i, p in enumerate(parts) if p[:1].isupper()), len(parts))
    path = "/".join(parts[:k]) + ".py" if k else ""
    return "::".join([x for x in [path, *parts[k:], name] if x])


def parse_junit(junit: Path, root: Path | None = None) -> dict:
    """Counts, skip reasons and red nodeids re-derived from a junit file (what a receipt consumer must be able to redo)."""
    tree = ET.parse(str(junit))
    counts = dict(collected=0, passed=0, failed=0, error=0, skipped=0, xfailed=0)
    skips: dict[str, int] = {}
    reds: list[dict] = []
    seen: set[str] = set()
    for tc in tree.iter("testcase"):
        counts["collected"] += 1
        nid = _nodeid(tc.get("classname", ""), tc.get("name", ""), root)
        fail = tc.find("failure")
        err = tc.find("error")
        skip = tc.find("skipped")
        if fail is not None or err is not None:
            node = fail if fail is not None else err
            counts["failed" if fail is not None else "error"] += 1
            if nid not in seen:
                seen.add(nid)
                reds.append({"nodeid": nid, "kind": "failure" if fail is not None else "error",
                             "text": ((node.get("message") or "") + "\n" + (node.text or ""))[:4000]})
        elif skip is not None:
            if skip.get("type") == "pytest.xfail":
                counts["xfailed"] += 1
            else:
                counts["skipped"] += 1
                reason = (skip.get("message") or "").strip() or "(no reason)"
                skips[reason] = skips.get(reason, 0) + 1
        else:
            counts["passed"] += 1
    return {"counts": counts, "skips_by_reason": skips, "reds": reds}


# ---------------------------------------------------------------- ledger

class Ledger:
    """Append-only jsonl.  Lines: entry (new excused test), sighting (a later run met it again), status (open|fixed).
    Nothing is rewritten or deleted; `entries()` folds the lines into the current view."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
        return out

    def entries(self) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        for r in self.lines():
            if r["type"] == "entry":
                by_id[r["id"]] = {**r, "status": "open", "sightings": []}
            elif r["type"] == "sighting" and r["id"] in by_id:
                by_id[r["id"]]["sightings"].append(r)
            elif r["type"] == "status" and r["id"] in by_id:
                by_id[r["id"]]["status"] = r["status"]
        return by_id

    def open_entry_for(self, nodeid: str) -> dict | None:
        found = [e for e in self.entries().values() if e["nodeid"] == nodeid and e["status"] == "open"]
        return found[-1] if found else None

    def file(self, nodeid: str, cls: str, run_id: str, sha: str, evidence: str) -> tuple[str, bool]:
        """Record one excused red.  Returns (ledger_id, is_new_entry)."""
        cur = self.open_entry_for(nodeid)
        if cur is not None:
            self._append({"type": "sighting", "id": cur["id"], "run_id": run_id, "sha": sha, "class": cls,
                          "evidence": evidence, "ts": now()})
            return cur["id"], False
        lid = "lg_" + sha256_bytes(f"{nodeid}|{run_id}|{time.time_ns()}".encode())[:10]
        self._append({"type": "entry", "id": lid, "nodeid": nodeid, "class": cls, "first_run": run_id,
                      "first_sha": sha, "evidence": evidence, "board_row": None, "ts": now()})
        return lid, True

    def mark(self, lid: str, status: str) -> None:
        assert status in ("open", "fixed")
        self._append({"type": "status", "id": lid, "status": status, "ts": now()})


# ---------------------------------------------------------------- receipt

def verdict_of(legs: list[dict]) -> tuple[str, list[str]]:
    """Red when any leg ran nothing, any red is a regression or lacks a ledger_id, or a job's pytest died abnormally."""
    why: list[str] = []
    for leg in legs:
        for job in leg.get("jobs", []):
            if job["counts"]["collected"] <= 0:
                why.append(f"{leg['platform']}/{job['name']}: collected 0")
            if job.get("rc") not in (0, 1):
                why.append(f"{leg['platform']}/{job['name']}: pytest exit {job.get('rc')}")
            for r in job["reds"]:
                if r["class"] == "regression":
                    why.append(f"regression: {r['nodeid']}")
                elif not r.get("ledger_id"):
                    why.append(f"red without ledger_id: {r['nodeid']}")
    return ("red" if why else "green"), why


def build_receipt(sha: str, run_id: str, lock: dict, runner_blob: str | None, legs: list[dict],
                  runner_matches_sha: bool | None = None) -> dict:
    verdict, why = verdict_of(legs)
    if runner_blob is None:
        verdict, why = "red", why + ["scripts/verify.py is not in the sha under test"]
    elif runner_matches_sha is False:
        verdict, why = "red", why + ["the running verify.py is not the blob committed at the sha"]
    return {"schema": SCHEMA, "sha": sha, "lock": lock, "runner_blob": runner_blob, "run_id": run_id,
            "verdict": verdict, "verdict_why": why, "legs": legs}


def write_receipt(receipt: dict, receipts_dir: Path) -> Path:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    p = receipts_dir / f"vr_{receipt['sha'][:12]}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ---------------------------------------------------------------- environment

def clean_env(run: Path) -> dict:
    """env -i plus only PATH, LANG, the wait scale, and a private HOME / TMPDIR.  Nothing of the operator's ANTHROPIC_*,
    OPENAI_*, LEVAIN_* passes through."""
    return {"PATH": CLEAN_PATH, "LANG": "en_US.UTF-8", "LEVAIN_TEST_WAIT_SCALE": WAIT_SCALE,
            "HOME": str(run / "home"), "TMPDIR": str(run / "tmp")}


def _run(cmd: list[str], *, cwd: Path, env: dict, log: Path, timeout: float | None = None) -> int:
    with open(log, "ab") as lf:
        lf.write(f"\n$ {' '.join(cmd)}\n".encode())
        lf.flush()
        try:
            return subprocess.run(cmd, cwd=str(cwd), env=env, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            lf.write(b"\n[verify] TIMEOUT\n")
            return 124


class LoadSampler(threading.Thread):
    def __init__(self, every: float = 5.0):
        super().__init__(daemon=True)
        self.every, self.max, self._stop_ev = every, os.getloadavg()[0], threading.Event()

    def run(self) -> None:
        while not self._stop_ev.wait(self.every):
            self.max = max(self.max, os.getloadavg()[0])

    def finish(self) -> float:
        self._stop_ev.set()
        self.max = max(self.max, os.getloadavg()[0])
        return round(self.max, 2)


def make_env(repo: Path, sha: str, run: Path, name: str, python: str, log: Path, lock_src: Path | None = None) -> tuple[Path, Path]:
    """Fresh worktree at `sha` under run/<name>, fresh venv, locked install.  Returns (src, venv_python).
    `lock_src`: when the sha predates scripts/ci_install_locked.sh (a main baseline older than the lockfile landing), the
    installer and uv.lock come from lock_src (the sha under test) and only the editable install points at this checkout."""
    src = run / name / "src"
    (run / name).mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(src), sha)
    venv = run / name / "venv"
    env = {**clean_env(run), "UV_PYTHON_PREFERENCE": "managed"}
    # the real uv cache keeps a rebuild near a minute; every other write goes under run/home
    env["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")
    if _run(["uv", "venv", "--seed", "--python", python, str(venv)], cwd=src, env=env, log=log):
        raise RuntimeError("uv venv failed (see log)")
    installer = src / "scripts" / "ci_install_locked.sh"
    ienv = {**env, "PATH": f"{venv}/bin:{CLEAN_PATH}", "VIRTUAL_ENV": str(venv)}
    if lock_src is not None and not installer.exists():
        installer = lock_src / "scripts" / "ci_install_locked.sh"
        ienv["LOCK_DIR"] = str(lock_src)
    if _run(["bash", str(installer), "dev", "openhands"], cwd=src, env=ienv, log=log):
        raise RuntimeError("locked install failed (see log)")
    return src, venv / "bin" / "python"


def remove_worktree(repo: Path, run: Path, name: str) -> None:
    src = run / name / "src"
    if src.exists():
        git(repo, "worktree", "remove", "--force", str(src), check=False)
    shutil.rmtree(run / name / "venv", ignore_errors=True)
    shutil.rmtree(run / name, ignore_errors=True)


def host_fingerprint() -> dict:
    def cap(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return ""
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "mac_ver": platform.mac_ver()[0], "cpu_count": os.cpu_count(), "uv": cap(["uv", "--version"]),
            "system_python3": cap(["/usr/bin/python3", "--version"]), "node": cap(["node", "--version"])}


# ---------------------------------------------------------------- classification

def pytest_cmd(py: Path, *args: str) -> list[str]:
    return [str(py), "-m", "pytest", "-p", "no:cacheprovider", *args]


def classify(reds: list[dict], *, ledger: Ledger, run_id: str, sha: str, touched: set[str],
             rerun, baseline) -> list[dict]:
    """Deterministic classification, exactly the design page.  `rerun(nodeid) -> bool` is one serial run (True = pass);
    `baseline(nodeids) -> dict[nodeid -> 'red'|'green'|'absent']` runs them on a fresh main checkout, serially."""
    out: list[dict] = []
    pending: list[dict] = []
    for r in reds:
        nid = r["nodeid"]
        rec = {"nodeid": nid, "class": None, "evidence": "", "ledger_id": None, "kind": r.get("kind")}
        file_part = nid.split("::", 1)[0]
        if file_part in touched:
            rec.update({"class": "regression", "evidence": f"red in {file_part}, a test file the diff touches: never excused"})
            out.append(rec)
            continue
        results = [rerun(nid) for _ in range(RERUNS)]
        passes = sum(results)
        if passes == RERUNS:
            rec.update({"class": "load", "evidence": f"red under -n load, {RERUNS}/{RERUNS} serial reruns passed"})
            out.append(rec)
        elif passes > 0:
            rec.update({"class": "flaky", "evidence": f"serial reruns mixed: {passes}/{RERUNS} passed"})
            out.append(rec)
        else:
            cur = ledger.open_entry_for(nid)
            if cur is not None and cur["class"] in ("load", "flaky"):
                rec.update({"class": "regression",
                            "evidence": f"ledger test ({cur['id']}, {cur['class']}) failed all {RERUNS} serial reruns: never excused"})
                out.append(rec)
            else:
                rec["evidence"] = f"failed {RERUNS}/{RERUNS} serial reruns"
                pending.append(rec)
                out.append(rec)
    if pending:
        base = baseline([p["nodeid"] for p in pending])
        for rec in pending:
            b = base.get(rec["nodeid"], "absent")
            if b == "red":
                rec.update({"class": "inherited", "evidence": rec["evidence"] + f"; also red on {MAIN_REF}"})
            elif b == "green":
                rec.update({"class": "regression", "evidence": rec["evidence"] + f"; passes on {MAIN_REF}"})
            else:
                rec.update({"class": "regression", "evidence": rec["evidence"] + f"; not present on {MAIN_REF}"})
    for rec in out:
        if rec["class"] in ("load", "flaky", "inherited"):
            rec["ledger_id"], new = ledger.file(rec["nodeid"], rec["class"], run_id, sha, rec["evidence"])
            rec["ledger_new"] = new
    return out


# ---------------------------------------------------------------- the mac leg

def lock_host(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    f = open(state / "host.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.write(f"{os.getpid()} {now()}\n")
    f.flush()
    return f


def run_mac(repo: Path, sha: str, run: Path, ledger: Ledger, run_id: str, *, python: str, workers: int,
            pytest_timeout: float) -> tuple[dict, list[dict]]:
    log = run / "verify.log"
    started = now()
    t0 = time.time()
    sampler = LoadSampler()
    sampler.start()
    env = clean_env(run)
    for d in (run / "home", run / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    touched = set(git(repo, "diff", "--name-only", f"{MAIN_REF}...{sha}", check=False).stdout.split())
    junit = run / "junit-mac.xml"
    src = None
    base_name = None
    try:
        src, py = make_env(repo, sha, run, "src_at_sha", python, log)
        penv = {**env, "PATH": f"{py.parent}:{CLEAN_PATH}"}
        rc = _run(pytest_cmd(py, "-n", str(workers), "--dist", "loadfile", f"--junitxml={junit}",
                             f"--basetemp={run / 'pt'}", "tests"), cwd=src, env=penv, log=log, timeout=pytest_timeout)
        if not junit.exists():
            raise RuntimeError(f"pytest produced no junit (exit {rc}); see {log}")
        parsed = parse_junit(junit, src)
        n_rerun = [0]

        def rerun(nid: str) -> bool:
            n_rerun[0] += 1
            return _run(pytest_cmd(py, nid, f"--basetemp={run / ('pt-r%d' % n_rerun[0])}"), cwd=src, env=penv, log=log,
                        timeout=900) == 0

        def baseline(nids: list[str]) -> dict:
            nonlocal base_name
            base_name = "src_at_main"
            msha = git(repo, "rev-parse", MAIN_REF).stdout.strip()
            bsrc, bpy = make_env(repo, msha, run, base_name, python, log, lock_src=src)
            bj = run / "junit-main-baseline.xml"
            benv = {**env, "PATH": f"{bpy.parent}:{CLEAN_PATH}"}
            _run(pytest_cmd(bpy, *nids, f"--junitxml={bj}", f"--basetemp={run / 'pt-main'}"), cwd=bsrc, env=benv, log=log,
                 timeout=1800)
            if not bj.exists():
                return {}
            bp = parse_junit(bj, bsrc)
            red = {r["nodeid"] for r in bp["reds"]}
            seen = set()
            for tc in ET.parse(str(bj)).iter("testcase"):
                seen.add(_nodeid(tc.get("classname", ""), tc.get("name", ""), bsrc))
            return {n: ("red" if n in red else "green" if n in seen else "absent") for n in nids}

        reds = classify(parsed["reds"], ledger=ledger, run_id=run_id, sha=sha, touched=touched, rerun=rerun, baseline=baseline)
    finally:
        max_load = sampler.finish()
        remove_worktree(repo, run, "src_at_sha")
        if base_name:
            remove_worktree(repo, run, base_name)
        git(repo, "worktree", "prune", check=False)
    return {"platform": "macos", "host": socket.gethostname(), "python": python, "host_fingerprint": host_fingerprint(),
            "started": started, "ended": now(), "duration_s": round(time.time() - t0, 1), "workers": workers,
            "max_load": max_load,
            "jobs": [{"name": "pytest-full-py312", "rc": rc, "counts": parsed["counts"],
                      "skips_by_reason": parsed["skips_by_reason"],
                      "junit": {"path": str(junit), "sha256": sha256_file(junit)},
                      "reds": [{k: v for k, v in r.items() if k not in ("kind", "ledger_new")} for r in reds]}]}, reds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="run the verification receipt for a sha")
    rp.add_argument("--sha", required=True)
    rp.add_argument("--leg", choices=("mac", "linux", "both"), default="mac")
    rp.add_argument("--repo", default=None, help="checkout to take the sha from (default: the checkout holding this script)")
    rp.add_argument("--state-dir", default=str(DEFAULT_STATE), help="host lock, run dirs and ledger live here")
    rp.add_argument("--ledger", default=None, help="default: <state-dir>/ledger.jsonl")
    rp.add_argument("--receipts-dir", default=None, help="default: <repo>/project_memory/receipts")
    rp.add_argument("--python", default=MAC_PYTHON)
    rp.add_argument("--workers", type=int, default=WORKERS_MAC)
    rp.add_argument("--pytest-timeout", type=float, default=2400.0, help="seconds for the full-suite pytest call")
    a = ap.parse_args(argv)

    if a.leg in ("linux", "both"):
        print(f"verify: leg '{a.leg}' is not built yet (S2); only --leg mac runs", file=sys.stderr)
        return 2
    repo = Path(a.repo or Path(__file__).resolve().parents[1]).resolve()
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    sha = git(top, "rev-parse", "--verify", f"{a.sha}^{{commit}}").stdout.strip()
    state = Path(a.state_dir)
    ledger = Ledger(Path(a.ledger) if a.ledger else state / "ledger.jsonl")
    receipts = Path(a.receipts_dir) if a.receipts_dir else top / "project_memory" / "receipts"

    lk = lock_host(state)
    if lk is None:
        print(f"verify: another receipt run holds {state / 'host.lock'}; one run per host at a time", file=sys.stderr)
        return 3
    run_id = f"{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{sha[:8]}-{uuid.uuid4().hex[:4]}"
    run = state / "runs" / run_id
    run.mkdir(parents=True)
    print(f"verify: run {run_id} sha {sha[:12]} leg mac, run dir {run}")
    try:
        leg, reds = run_mac(top, sha, run, ledger, run_id, python=a.python, workers=a.workers,
                            pytest_timeout=a.pytest_timeout)
    except Exception as e:  # noqa: BLE001 - the failure is the report; no receipt is better than a half receipt
        print(f"verify: FAILED before a receipt could be written: {e}", file=sys.stderr)
        return 4
    lock = {}
    for nm, key in (("uv.lock", "uv_lock_sha256"), ("requirements-ci-py39.txt", "requirements_ci_py39_sha256")):
        blob = git_show_bytes(top, sha, nm)
        lock[key] = sha256_bytes(blob) if blob is not None else None
    rb = git(top, "rev-parse", f"{sha}:scripts/verify.py", check=False)
    runner_blob = rb.stdout.strip() if rb.returncode == 0 else None
    actual = git(top, "hash-object", str(Path(__file__).resolve())).stdout.strip()
    receipt = build_receipt(sha, run_id, lock, runner_blob, [leg], None if runner_blob is None else runner_blob == actual)
    path = write_receipt(receipt, receipts)
    job = leg["jobs"][0]
    print(f"verify: {receipt['verdict'].upper()}  receipt {path}")
    print(f"verify: counts {job['counts']}  wall {leg['duration_s']}s  max_load {leg['max_load']}")
    for r in job["reds"]:
        print(f"verify: red [{r['class']}] {r['nodeid']} ledger={r['ledger_id']}")
    for r in reds:
        if r.get("ledger_new"):
            print(f"verify: BOARD ROW OWED for new ledger entry {r['ledger_id']} ({r['class']}): {r['nodeid']}")
    for w in receipt["verdict_why"]:
        print(f"verify: why red: {w}")
    return 0 if receipt["verdict"] == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
