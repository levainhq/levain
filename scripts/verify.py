#!/usr/bin/env python3
"""Local verification receipt runner (design: L2_local_receipt_design.md).

usage:
  verify.py run    --sha S [--leg mac|linux|both]   foreground
  verify.py start  --sha S [--leg ...]              detached: prints the run id and the log path, then exits
  verify.py status [--run ID]                       running / passed / red / killed, from the run's status file
  verify.py sweep  [--state-dir D]                  reap earlier runs that died without cleaning up

Builds a clean environment at the exact sha (fresh git worktree, fresh uv venv from uv.lock via
scripts/ci_install_locked.sh), runs the full suite, classifies every red deterministically (no model), appends the
excused ones to a laptop-local append-only ledger, and writes a `levain-test-receipt/1` file.  The linux leg runs on
the host `argushub` over ssh: the sha travels as a git bundle on the ssh pipe, the same script runs there with
`--here`, and the leg JSON and junit files are copied back.  The remote never writes the ledger; the laptop files the
remote leg's excused reds after the copy-back.

Signed rulings this builds to (Phill, 2026-11-09): mac = Python 3.12 on the laptop, linux = 3.13 on argushub, both on
every landing (A); an inherited red may ride only with a ledger entry AND a board row (B); the ledger is a laptop-local
file with one writer (C); the cloud-only jobs still gate beside the receipt and are not run here (D); no docs-only skip,
every landing runs the full receipt (E).

Kill safety: SIGTERM and SIGINT stop the run's process groups, remove its worktrees, eject any disk image mounted
under the run dir and clear its working subdirectories, keeping only the logs and status file.  A run that dies without
that (SIGKILL, power) is reaped by the next run's start-of-run sweep, which only touches run dirs no live run holds.

CI environment decision: CI is NOT set.  Tests that read it turn host-provisioning skips into failures or switch on
CI-only behaviour, and the laptop is not provisioned the way a cloud runner is, so setting it would manufacture reds
that are about the host, not the commit.  Skips are instead recorded by reason (skips_by_reason) so the receipt
consumer can see exactly what did not run.  GITHUB_ACTIONS is likewise unset.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import signal
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
LINUX_PYTHON = "3.13"           # minor only: the full version is read back from the venv and recorded on the leg
CLOSURE_PYTHON = "3.9"
WORKERS_MAC = 6
WORKERS_LINUX = 4
WAIT_SCALE = "2"
RERUNS = 3
MAIN_REF = "origin/main"
REMOTE_HOST = "argushub"
REMOTE_STATE = ".local/state/levain-verify"     # relative to the remote home
DEFAULT_STATE = Path.home() / ".local" / "state" / "levain-verify"
HAIKU_MODEL = "claude-haiku-5-5"
DIAG_MAX_TOKENS = 1500
DIAG_PROMPT_CAP = 24000
STARTING_GRACE = 120.0          # seconds a freshly started run may have no lock yet
FINAL_STATES = ("passed", "red")
BWRAP_TARGETS = [
    "tests/test_firing_confinement.py", "tests/test_jewel_recheck.py", "tests/test_nlink_floor.py",
    "tests/test_masks_last.py", "tests/test_floor_project_memory.py", "tests/test_floor_spellings.py",
    "tests/test_floor_cloud_creds.py", "tests/test_floor_process_view.py",
    "tests/test_hands_spawn.py::test_the_operator_floor_kills_its_leaf_when_levain_dies",
    "tests/test_runtime_view.py", "tests/test_floor_autonomic_store.py", "tests/test_landlock_live.py",
    "tests/test_landlock.py"]


def _clean_path() -> str:
    if platform.system() == "Linux":
        h = Path.home()
        return f"{h}/.local/bin:{h}/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


CLEAN_PATH = _clean_path()
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
            if job.get("setup_failed"):
                why.append(f"{leg['platform']}/{job['name']}: setup failed: {job['setup_failed']}")
            if job.get("required_run") and job["counts"]["skipped"] > 0:
                why.append(f"{leg['platform']}/{job['name']}: {job['counts']['skipped']} skipped under a required-run job")
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


_CHILDREN: set = set()
_CHILD_LOCK = threading.Lock()


def _kill_group(p: subprocess.Popen, grace: float = 5.0) -> None:
    """TERM the child's process group, give it `grace` seconds, then KILL whatever is left in the group."""
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.time() + grace
    while time.time() < end and p.poll() is None:
        time.sleep(0.1)
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_children(grace: float = 3.0) -> None:
    with _CHILD_LOCK:
        kids = list(_CHILDREN)
    for p in kids:
        _kill_group(p, grace)


def _spawn(cmd: list[str], *, cwd, env, stdout, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """Every child gets its own session so one kill reaches its whole group (xdist workers, browsers, mounts' helpers)."""
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env, stdin=stdin, stdout=stdout,
                         stderr=subprocess.STDOUT, start_new_session=True)
    with _CHILD_LOCK:
        _CHILDREN.add(p)
    return p


def _run(cmd: list[str], *, cwd: Path, env: dict, log: Path, timeout: float | None = None, stdin: Path | None = None) -> int:
    with open(log, "ab") as lf:
        lf.write(f"\n$ {' '.join(cmd)}\n".encode())
        lf.flush()
        sf = open(stdin, "rb") if stdin else subprocess.DEVNULL
        p = _spawn(cmd, cwd=cwd, env=env, stdout=lf, stdin=sf)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(p)
            p.wait()
            lf.write(b"\n[verify] TIMEOUT\n")
            return 124
        except BaseException:
            _kill_group(p)
            p.wait()
            raise
        finally:
            with _CHILD_LOCK:
                _CHILDREN.discard(p)
            if stdin:
                sf.close()


def _capture(cmd: list[str], *, cwd: Path, env: dict, log: Path) -> tuple[int, str]:
    """Run to completion and return (exit code, stdout); stderr goes to the log."""
    with open(log, "ab") as lf:
        lf.write(f"\n$ {' '.join(cmd)}\n".encode())
        r = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=lf, text=True,
                           stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout


# ---------------------------------------------------------------- kill safety: signals, sweep, reap

class Killed(BaseException):
    """Raised from the SIGTERM / SIGINT handler.  A BaseException so no `except Exception` swallows it and every
    `finally` on the way out (worktree removal, sampler stop) still runs."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def install_signal_handlers() -> dict:
    prev: dict = {}

    def handler(signum, _frame):
        terminate_children()
        raise Killed(signum)

    for s in (signal.SIGTERM, signal.SIGINT):
        prev[s] = signal.signal(s, handler)
    return prev


def restore_signal_handlers(prev: dict) -> None:
    for s, h in prev.items():
        signal.signal(s, h)


def _under(path: str, root: str) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root).rstrip("/")
    return p == r or p.startswith(r + "/")


def parse_hdiutil_plist(data: bytes) -> list[dict]:
    """`hdiutil info -plist` -> [{image_path, devs, mounts}]."""
    out = []
    for img in plistlib.loads(data).get("images", []):
        ents = img.get("system-entities", []) or []
        out.append({"image_path": img.get("image-path") or "",
                    "devs": [e["dev-entry"] for e in ents if e.get("dev-entry")],
                    "mounts": [e["mount-point"] for e in ents if e.get("mount-point")]})
    return out


def images_to_eject(images: list[dict], run: Path) -> list[str]:
    """Whole-disk device ids of exactly the images whose file or mount point lies under `run`; no other image."""
    devs: list[str] = []
    for img in images:
        hit = (bool(img["image_path"]) and _under(img["image_path"], str(run))) or any(_under(m, str(run)) for m in img["mounts"])
        if hit:
            for d in img["devs"]:
                m = re.match(r"^(/dev/disk\d+)", d)
                whole = m.group(1) if m else d
                if whole not in devs:
                    devs.append(whole)
    return devs


class Tools:
    """The outside-world calls the sweep makes.  Tests substitute a fake with the same methods."""

    def git(self, repo: Path, *args: str) -> subprocess.CompletedProcess:
        return git(repo, *args, check=False)

    def hdiutil_images(self) -> list[dict]:
        try:
            r = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode != 0 or not r.stdout:
            return []
        try:
            return parse_hdiutil_plist(r.stdout)
        except Exception:  # noqa: BLE001
            return []

    def eject(self, dev: str) -> bool:
        try:
            if subprocess.run(["diskutil", "eject", dev], capture_output=True, timeout=120).returncode == 0:
                return True
            return subprocess.run(["hdiutil", "detach", dev, "-force"], capture_output=True, timeout=120).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def ps(self) -> list[tuple[int, str]]:
        try:
            r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return []
        out = []
        for ln in r.stdout.splitlines():
            parts = ln.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                out.append((int(parts[0]), parts[1]))
        return out

    def kill(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _cmd_touches_run(cmd: str, run: Path) -> bool:
    """True when some argument is a path in a SUBDIRECTORY of the run dir (a venv, a worktree), or the value of a
    `--flag=` that points under the run dir (a basetemp).  A viewer of the run's top-level log never matches."""
    for root in {str(run), os.path.realpath(run)}:
        for tok in cmd.split():
            is_flag = tok.startswith("--") and "=" in tok
            tok = tok.split("=", 1)[1] if is_flag else tok
            if tok.startswith(root + "/") and (is_flag or "/" in tok[len(root) + 1:]):
                return True
    return False


def reap_run(run: Path, repo: Path, tools: Tools, *, keep_files: bool = False, grace: float = 2.0, say=print) -> dict:
    """Clean everything one run left: stray processes, mounted disk images, registered worktrees, the files.
    keep_files=True (the run's own kill cleanup) removes only the subdirectories, keeping logs and status; False (the
    sweep of an earlier dead run) deletes the dir.  Nothing is deleted while an image is still mounted under it."""
    summary: dict = {"run": str(run), "killed": [], "ejected": [], "worktrees": [], "left_mounted": [], "removed": False}
    me = os.getpid()
    pids = [pid for pid, cmd in tools.ps() if pid != me and _cmd_touches_run(cmd, run)]
    for pid in pids:
        tools.kill(pid, signal.SIGTERM)
    if pids:
        time.sleep(grace)
        alive = {pid for pid, cmd in tools.ps() if _cmd_touches_run(cmd, run)}
        for pid in pids:
            if pid in alive:
                tools.kill(pid, signal.SIGKILL)
        summary["killed"] = pids
    devs = images_to_eject(tools.hdiutil_images(), run)
    for dev in devs:
        if tools.eject(dev):
            summary["ejected"].append(dev)
    if devs:
        still = images_to_eject(tools.hdiutil_images(), run)
        if still:
            summary["left_mounted"] = still
            say(f"verify: sweep: image still mounted under {run} ({', '.join(still)}); nothing deleted there")
            return summary
    listing = tools.git(repo, "worktree", "list", "--porcelain").stdout or ""
    for ln in listing.splitlines():
        if ln.startswith("worktree "):
            wt = ln[len("worktree "):]
            if _under(wt, str(run)) and os.path.realpath(wt) != os.path.realpath(repo):
                tools.git(repo, "worktree", "remove", "--force", wt)
                summary["worktrees"].append(wt)
    tools.git(repo, "worktree", "prune")
    if run.exists():
        if keep_files:
            for child in run.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
        else:
            shutil.rmtree(run, ignore_errors=True)
            summary["removed"] = not run.exists()
    return summary


def take_run_lock(run: Path):
    """Held for the life of the run; the kernel drops it when the process dies, which is how a sweep tells a dead run."""
    run.mkdir(parents=True, exist_ok=True)
    f = open(run / "run.lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def run_is_live(run: Path) -> bool:
    lk = run / "run.lock"
    if not lk.exists():
        return False
    fd = os.open(str(lk), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def sweep_runs(state: Path, repo: Path, tools: Tools, *, current: str | None = None, say=print) -> list[dict]:
    """Reap every earlier run dir that no live run holds and that did not finish with a receipt.  A finished run
    (passed or red) keeps its dir: its junit files are what the receipt's hashes point at."""
    runs = state / "runs"
    done: list[dict] = []
    if not runs.is_dir():
        return done
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or d.name == current or run_is_live(d):
            continue
        st = read_status(d)
        if st.get("state") in FINAL_STATES:
            continue
        sp = d / "status.json"
        if st.get("state") == "starting" and sp.exists() and time.time() - sp.stat().st_mtime < STARTING_GRACE:
            continue
        say(f"verify: sweep: reaping earlier run {d.name} (state {st.get('state', 'unknown')})")
        done.append(reap_run(d, repo, tools, say=say))
    return done


# ---------------------------------------------------------------- status file

def read_status(run: Path) -> dict:
    try:
        return json.loads((Path(run) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Status:
    """status.json, kept current by the run; atomic replace, safe from the two leg threads."""

    def __init__(self, run: Path):
        self.run = Path(run)
        self.path = self.run / "status.json"
        self._lock = threading.Lock()

    def update(self, **kw) -> None:
        with self._lock:
            cur = read_status(self.run)
            cur.update(kw)
            cur["updated"] = now()
            self.run.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name("status.json.tmp")
            tmp.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)


def effective_status(run: Path) -> dict:
    """The status file, except that a run recorded as starting/running whose lock is gone is reported killed."""
    st = read_status(run)
    if st.get("state") in ("starting", "running") and not run_is_live(run):
        sp = Path(run) / "status.json"
        young = st["state"] == "starting" and sp.exists() and time.time() - sp.stat().st_mtime < STARTING_GRACE
        if not young:
            st = {**st, "state": "killed", "note": "the runner is gone and holds no lock (killed without cleanup)"}
    return st


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
    # the real uv cache keeps a rebuild cheap; every other write goes under run/home
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


def classify(reds: list[dict], *, ledger: Ledger | None, run_id: str, sha: str, touched: set[str],
             rerun, baseline) -> list[dict]:
    """Deterministic classification, exactly the design page.  `rerun(nodeid) -> bool` is one serial run (True = pass);
    `baseline(nodeids) -> dict[nodeid -> 'red'|'green'|'absent']` runs them on a fresh main checkout, serially.
    `ledger=None` (a remote leg: the laptop is the only ledger writer) classifies and files nothing; the laptop then
    runs `apply_ledger_to_leg` over the copied-back leg."""
    out: list[dict] = []
    pending: list[dict] = []
    for r in reds:
        nid = r["nodeid"]
        rec = {"nodeid": nid, "class": None, "evidence": "", "ledger_id": None, "kind": r.get("kind"),
               "text": (r.get("text") or "")[:1500]}
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
            cur = ledger.open_entry_for(nid) if ledger is not None else None
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
    if ledger is not None:
        for rec in out:
            if rec["class"] in ("load", "flaky", "inherited"):
                rec["ledger_id"], new = ledger.file(rec["nodeid"], rec["class"], run_id, sha, rec["evidence"])
                rec["ledger_new"] = new
    return out


def apply_ledger_to_leg(leg: dict, ledger: Ledger, run_id: str, sha: str) -> None:
    """The ledger half of classification for a leg that was classified elsewhere without a ledger: an inherited red
    that the ledger already holds as load/flaky and that failed every serial rerun is a regression (never excused);
    every other excused red is filed (entry or sighting) and gets its ledger_id.  Mutates `leg` and its `_diag`."""
    diag = {d["nodeid"]: d for d in leg.get("_diag", [])}
    for job in leg.get("jobs", []):
        for r in job["reds"]:
            if r["class"] == "inherited":
                cur = ledger.open_entry_for(r["nodeid"])
                if cur is not None and cur["class"] in ("load", "flaky"):
                    r["class"] = "regression"
                    r["evidence"] += f"; ledger test ({cur['id']}, {cur['class']}) failed all {RERUNS} serial reruns: never excused"
                    if r["nodeid"] in diag:
                        diag[r["nodeid"]].update(**{"class": "regression", "evidence": r["evidence"]})
                    continue
            if r["class"] in ("load", "flaky", "inherited"):
                lid, new = ledger.file(r["nodeid"], r["class"], run_id, sha, r["evidence"])
                r["ledger_id"] = lid
                if r["nodeid"] in diag:
                    diag[r["nodeid"]]["ledger_new"] = new


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


def _minor(python: str) -> str:
    return "".join(python.split(".")[:2])


def venv_version(py: Path, env: dict) -> str:
    try:
        r = subprocess.run([str(py), "-c", "import platform;print(platform.python_version())"], capture_output=True,
                           text=True, env=env, timeout=30)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Ctx:
    """One leg's shared state: where it runs, what it logs, and the worktrees it must give back."""

    def __init__(self, repo: Path, sha: str, run: Path, run_id: str, ledger: Ledger | None, status: Status | None,
                 leg_name: str, log: Path):
        self.repo, self.sha, self.run, self.run_id = repo, sha, run, run_id
        self.ledger, self.status, self.leg_name, self.log = ledger, status, leg_name, log
        self.env = clean_env(run)
        self.names: list[str] = []
        self.src: Path | None = None
        self.python = ""
        self._main: tuple[Path, Path] | None = None
        self.touched: set[str] = set()

    def progress(self, phase: str) -> None:
        if self.status is not None:
            self.status.update(**{f"phase_{self.leg_name}": phase})

    def prepare(self) -> None:
        for d in (self.run / "home", self.run / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        self.touched = set(git(self.repo, "diff", "--name-only", f"{MAIN_REF}...{self.sha}", check=False).stdout.split())

    def make(self, name: str, sha: str, python: str, lock_src: Path | None = None) -> tuple[Path, Path]:
        self.names.append(name)
        return make_env(self.repo, sha, self.run, name, python, self.log, lock_src=lock_src)

    def main_env(self) -> tuple[Path, Path]:
        """The worktree + venv at the land base, built once and only when a red needs a baseline."""
        if self._main is None:
            msha = git(self.repo, "rev-parse", MAIN_REF).stdout.strip()
            self.progress("building the main baseline")
            self._main = self.make("src_at_main", msha, self.python, lock_src=self.src)
        return self._main

    def teardown(self) -> None:
        for n in self.names:
            remove_worktree(self.repo, self.run, n)
        shutil.rmtree(self.run / "venv39", ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)


def run_job(ctx: Ctx, *, name: str, py: Path, cwd: Path, env: dict, targets: list[str], junit_name: str,
            workers: int | None, timeout: float, flags: tuple = (), rerun_flags: tuple = (), required_run: bool = False,
            base_cfg=None) -> tuple[dict, list[dict]]:
    """One pytest job: run it under xdist, classify its reds, return (job dict for the receipt, diagnosis records).
    `base_cfg(bsrc, bpy) -> (python, env, flags)` adapts the baseline run to the job's interpreter and flags."""
    junit = ctx.run / junit_name
    xd = ["-n", str(workers), "--dist", "loadfile"] if workers else []
    ctx.progress(f"{name}: pytest")
    rc = _run(pytest_cmd(py, *xd, *flags, f"--junitxml={junit}", f"--basetemp={ctx.run / ('pt-' + name)}", *targets),
              cwd=cwd, env=env, log=ctx.log, timeout=timeout)
    if not junit.exists():
        raise RuntimeError(f"{name}: pytest produced no junit (exit {rc}); see {ctx.log}")
    parsed = parse_junit(junit, cwd)
    n_rerun = [0]

    def rerun(nid: str) -> bool:
        n_rerun[0] += 1
        return _run(pytest_cmd(py, nid, *rerun_flags, f"--basetemp={ctx.run / ('pt-%s-r%d' % (name, n_rerun[0]))}"),
                    cwd=cwd, env=env, log=ctx.log, timeout=900) == 0

    def baseline(nids: list[str]) -> dict:
        bsrc, bpy = ctx.main_env()
        bj = ctx.run / f"junit-main-{name}.xml"
        if base_cfg:
            bp_py, benv, bflags = base_cfg(bsrc, bpy)
        else:
            bp_py, benv, bflags = bpy, {**ctx.env, "PATH": f"{bpy.parent}:{CLEAN_PATH}"}, rerun_flags
        _run(pytest_cmd(bp_py, *nids, *bflags, f"--junitxml={bj}", f"--basetemp={ctx.run / ('pt-main-' + name)}"),
             cwd=bsrc, env=benv, log=ctx.log, timeout=1800)
        if not bj.exists():
            return {}
        red = {r["nodeid"] for r in parse_junit(bj, bsrc)["reds"]}
        seen = {_nodeid(tc.get("classname", ""), tc.get("name", ""), bsrc) for tc in ET.parse(str(bj)).iter("testcase")}
        return {n: ("red" if n in red else "green" if n in seen else "absent") for n in nids}

    ctx.progress(f"{name}: classifying {len(parsed['reds'])} reds")
    reds = classify(parsed["reds"], ledger=ctx.ledger, run_id=ctx.run_id, sha=ctx.sha, touched=ctx.touched,
                    rerun=rerun, baseline=baseline)
    job = {"name": name, "rc": rc, "required_run": required_run, "counts": parsed["counts"],
           "skips_by_reason": parsed["skips_by_reason"], "junit": {"path": str(junit), "sha256": sha256_file(junit)},
           "load_reds": sum(1 for r in reds if r["class"] == "load"),
           "reds": [{k: v for k, v in r.items() if k not in ("kind", "ledger_new", "text")} for r in reds]}
    diag = [{"nodeid": r["nodeid"], "class": r["class"], "evidence": r["evidence"], "text": r.get("text", ""),
             "ledger_new": bool(r.get("ledger_new")), "job": name} for r in reds]
    return job, diag


def failed_job(name: str, why: str) -> dict:
    return {"name": name, "rc": None, "setup_failed": why, "required_run": False,
            "counts": dict(collected=0, passed=0, failed=0, error=0, skipped=0, xfailed=0), "skips_by_reason": {},
            "junit": None, "load_reds": 0, "reds": []}


def plan_mac(ctx: Ctx, python: str, workers: int, timeout: float) -> tuple[list[dict], list[dict], str]:
    ctx.python = python
    ctx.prepare()
    ctx.progress("building the environment")
    src, py = ctx.make("src_at_sha", ctx.sha, python)
    ctx.src = src
    env = {**ctx.env, "PATH": f"{py.parent}:{CLEAN_PATH}"}
    job, diag = run_job(ctx, name=f"pytest-full-py{_minor(python)}", py=py, cwd=src, env=env, targets=["tests"],
                        junit_name="junit-mac.xml", workers=workers, timeout=timeout)
    return [job], diag, venv_version(py, env)


def plan_linux(ctx: Ctx, python: str, workers: int, timeout: float) -> tuple[list[dict], list[dict], str]:
    """Three jobs: the full suite without the static-guard files, the live floor tests where a skip fails, and the
    root-run import closure on the oldest interpreter."""
    ctx.python = python
    ctx.prepare()
    ctx.progress("building the environment")
    src, py = ctx.make("src_at_sha", ctx.sha, python)
    ctx.src = src
    env = {**ctx.env, "PATH": f"{py.parent}:{CLEAN_PATH}"}
    m = _minor(python)
    not_static = ("-m", "not static_guard")
    jobs, diag = [], []
    j, d = run_job(ctx, name=f"pytest-full-py{m}", py=py, cwd=src, env=env, targets=["tests"], junit_name="junit-linux.xml",
                   workers=workers, timeout=timeout, flags=not_static, rerun_flags=not_static)
    jobs.append(j)
    diag += d
    j, d = run_job(ctx, name=f"bwrap-floor-live-py{m}", py=py, cwd=src, env={**env, "LEVAIN_REQUIRE_LINUX_LIVE": "1"},
                   targets=list(BWRAP_TARGETS), junit_name="junit-linux-floor.xml", workers=None, timeout=timeout,
                   required_run=True, base_cfg=lambda bsrc, bpy: (bpy, {**ctx.env, "PATH": f"{bpy.parent}:{CLEAN_PATH}",
                                                                          "LEVAIN_REQUIRE_LINUX_LIVE": "1"}, ()))
    jobs.append(j)
    diag += d

    name39 = f"root-closure-py{_minor(CLOSURE_PYTHON)}"
    ctx.progress(f"{name39}: building the venv")
    uenv = {**ctx.env, "UV_PYTHON_PREFERENCE": "managed",
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")}
    venv = ctx.run / "venv39"
    py39 = venv / "bin" / "python"
    penv = {**ctx.env, "PATH": f"{venv}/bin:{CLEAN_PATH}", "PYTHONPATH": str(src)}
    steps = [(["uv", "venv", "--seed", "--python", CLOSURE_PYTHON, str(venv)], uenv),
             ([str(py39), "-m", "pip", "install", "pip==25.3"], penv),
             ([str(py39), "-m", "pip", "install", "--require-hashes", "-r", str(src / "requirements-ci-py39.txt")], penv),
             ([str(py39), "tests/ci/root_closure.py", "compile"], penv),
             ([str(py39), "tests/ci/root_closure.py", "import-check"], penv)]
    why = None
    for cmd, cenv in steps:
        if _run(cmd, cwd=src, env=cenv, log=ctx.log, timeout=1800):
            why = " ".join(cmd[:4])
            break
    files: list[str] = []
    if why is None:
        rc, out = _capture([str(py39), "tests/ci/root_closure.py", "tests"], cwd=src, env=penv, log=ctx.log)
        files = out.split()
        if rc or not files:
            why = "the closure selected no test files"
    if why is not None:
        jobs.append(failed_job(name39, why))
        return jobs, diag, venv_version(py, env)
    flags = ("--noconftest", "--rootdir", str(src))
    j, d = run_job(ctx, name=name39, py=py39, cwd=src, env=penv, targets=files, junit_name="junit-linux-closure.xml",
                   workers=workers, timeout=timeout, flags=flags, rerun_flags=flags,
                   base_cfg=lambda bsrc, bpy: (py39, {**penv, "PYTHONPATH": str(bsrc)}, ("--noconftest", "--rootdir", str(bsrc))))
    jobs.append(j)
    diag += d
    return jobs, diag, venv_version(py, env)


def run_leg_local(repo: Path, sha: str, run: Path, run_id: str, ledger: Ledger | None, status: Status | None, *,
                  leg: str, python: str, workers: int, timeout: float) -> dict:
    """Run one leg on THIS host.  Returns the leg dict; its `_diag` key carries the failure text for diagnosis."""
    log = run / ("verify.log" if leg == "mac" else "verify-linux-here.log")
    ctx = Ctx(repo, sha, run, run_id, ledger, status, leg, log)
    started, t0 = now(), time.time()
    sampler = LoadSampler()
    sampler.start()
    try:
        jobs, diag, pyver = (plan_mac if leg == "mac" else plan_linux)(ctx, python, workers, timeout)
    finally:
        max_load = sampler.finish()
        ctx.teardown()
    return {"platform": "macos" if leg == "mac" else "linux", "host": socket.gethostname(), "python": pyver,
            "host_fingerprint": host_fingerprint(), "started": started, "ended": now(),
            "duration_s": round(time.time() - t0, 1), "workers": workers, "max_load": max_load,
            "load_reds": sum(j.get("load_reds", 0) for j in jobs), "jobs": jobs, "_diag": diag}


# ---------------------------------------------------------------- the linux leg, from the laptop

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10"]


def default_sh(cmd: list[str], *, log: Path, stdin: Path | None = None, timeout: float | None = None) -> int:
    return _run(cmd, cwd=Path.cwd(), env=dict(os.environ), log=log, timeout=timeout, stdin=stdin)


def run_leg_remote(repo: Path, sha: str, run: Path, run_id: str, ledger: Ledger, status: Status | None, *,
                   host: str = REMOTE_HOST, workers: int = WORKERS_LINUX, timeout: float = 4 * 3600,
                   sh=default_sh, script: Path | None = None) -> dict:
    """Ship the sha as a bundle over ssh, run this same script there with --here, copy the leg JSON and junit back.
    `sh(cmd, log=, stdin=, timeout=) -> exit code` is the only way out of the process; tests substitute it."""
    script = script or Path(__file__).resolve()
    ldir = run / "linux"
    ldir.mkdir(parents=True, exist_ok=True)
    log = run / "linux.log"
    q = shlex.quote
    rrun = f"{REMOTE_STATE}/runs/{run_id}"
    assert re.fullmatch(r"[0-9A-Za-z-]+", run_id) and re.fullmatch(r"[0-9a-f]{40}", sha), "unsafe run id or sha"
    ref = f"refs/verify/{sha[:12]}"
    main_ref = f"refs/remotes/{MAIN_REF}"
    bundle = ldir / "sha.bundle"
    ssh = ["ssh", *SSH_OPTS, host]

    def step(what: str, cmd: list[str], **kw) -> None:
        if status is not None:
            status.update(phase_linux=what)
        rc = sh(cmd, log=log, **kw)
        if rc != 0:
            raise RuntimeError(f"linux leg: {what} failed (exit {rc}); see {log}")

    if status is not None:
        status.update(phase_linux="bundling the sha")
    git(repo, "update-ref", ref, sha)
    try:
        b = git(repo, "bundle", "create", str(bundle), ref, main_ref, check=False)
        if b.returncode != 0:
            raise RuntimeError(f"git bundle failed (is {MAIN_REF} present?): {b.stderr.strip()}")
    finally:
        git(repo, "update-ref", "-d", ref, check=False)

    step("shipping the bundle", ssh + [f"mkdir -p {q(rrun)}/xfer && cat > {q(rrun)}/xfer/sha.bundle"], stdin=bundle)
    step("shipping the runner", ["scp", "-q", *SSH_OPTS, str(script), f"{host}:{rrun}/verify.py"])
    remote = (f"cd {q(rrun)} && git init -q xfer/repo && "
              f"git -C xfer/repo fetch -q ../sha.bundle +{ref}:{ref} +{main_ref}:{main_ref} && "
              f"python3 verify.py run --here --leg linux --sha {sha} --repo xfer/repo --run-id {run_id} "
              f"--workers {int(workers)} --leg-json leg-linux.json")
    step("running the linux leg on " + host, ssh + [remote], timeout=timeout)
    step("copying the leg JSON back", ["scp", "-q", *SSH_OPTS, f"{host}:{rrun}/leg-linux.json", str(ldir) + "/"])
    leg = json.loads((ldir / "leg-linux.json").read_text(encoding="utf-8"))
    for job in leg.get("jobs", []):
        jr = job.get("junit")
        if not jr:
            continue
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", jr["path"]):
            raise RuntimeError(f"linux leg: unsafe junit path in the leg JSON: {jr['path']!r}")
        base = os.path.basename(jr["path"])
        step(f"copying {base} back", ["scp", "-q", *SSH_OPTS, f"{host}:{jr['path']}", str(ldir) + "/"])
        local = ldir / base
        got = sha256_file(local) if local.exists() else None
        if got != jr["sha256"]:
            raise RuntimeError(f"linux leg: {base} copied back with sha256 {got}, the leg recorded {jr['sha256']}")
        jr["path"] = str(local)
    leg["remote_run"] = f"{host}:{rrun}"
    apply_ledger_to_leg(leg, ledger, run_id, sha)
    leg["load_reds"] = sum(1 for j in leg.get("jobs", []) for r in j["reds"] if r["class"] == "load")
    sh(ssh + [f"rm -rf {q(rrun)}"], log=log, timeout=300)
    bundle.unlink(missing_ok=True)
    return leg


# ---------------------------------------------------------------- diagnosis (advice only)

def haiku_call(prompt: str) -> str:
    import anthropic    # an ImportError is reported by the caller as a skip
    client = anthropic.Anthropic()
    msg = client.messages.create(model=HAIKU_MODEL, max_tokens=DIAG_MAX_TOKENS, messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def diagnosis_prompt(items: list[dict], diffstat: str) -> str:
    head = ("You are advising the engineer who is about to land a commit. Below are the tests that went red in the local "
            "verification run, how each was classified, and the diffstat of the commit against main. For EACH red give "
            "one hypothesis for the cause and one suspect file:line. Do not restate the logs. Do not reclassify.\n\n"
            f"git diff origin/main...SHA --stat\n{diffstat[:3000]}\n")
    parts, used = [head], len(head)
    for it in items:
        block = (f"\n--- {it['nodeid']}\nclass: {it['class']}\nevidence: {it['evidence']}\n"
                 f"failure text (trimmed):\n{(it.get('text') or '')[:1500]}\n")
        if used + len(block) > DIAG_PROMPT_CAP:
            parts.append("\n[further reds omitted: prompt cap]\n")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts)


def diagnose(run: Path, repo: Path, sha: str, diag: list[dict], *, call=haiku_call, say=print) -> Path | None:
    """One capped model call when the run has a regression or a new ledger entry; writes <run>/diagnosis.md.  It never
    changes a class or the verdict, and a failure of any kind is a printed line, not a failed run."""
    items = [d for d in diag if d["class"] == "regression" or d.get("ledger_new")]
    if not items:
        return None
    try:
        stat = git(repo, "diff", f"{MAIN_REF}...{sha}", "--stat", check=False).stdout
        text = call(diagnosis_prompt(items, stat))
    except ImportError:
        say("verify: diagnosis skipped: the anthropic SDK is not importable in this interpreter")
        return None
    except Exception as e:  # noqa: BLE001
        say(f"verify: diagnosis skipped: {type(e).__name__}: {e}")
        return None
    p = run / "diagnosis.md"
    p.write_text(f"# Diagnosis for {sha[:12]} ({HAIKU_MODEL}; advice only, no class or verdict was changed)\n\n{text}\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------- orchestration

def state_in_repo(state: Path, top: Path) -> bool:
    s = os.path.realpath(state)
    return _under(s, str(top)) or "project_memory" in Path(s).parts


def new_run_id(sha: str) -> str:
    return f"{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{sha[:8]}-{uuid.uuid4().hex[:4]}"


def lock_hashes(top: Path, sha: str) -> dict:
    lock = {}
    for nm, key in (("uv.lock", "uv_lock_sha256"), ("requirements-ci-py39.txt", "requirements_ci_py39_sha256")):
        blob = git_show_bytes(top, sha, nm)
        lock[key] = sha256_bytes(blob) if blob is not None else None
    return lock


def assemble_receipt(top: Path, sha: str, run_id: str, legs: list[dict]) -> dict:
    """Two legs merge into ONE receipt: the legs list, the lock hashes and the runner blob are the receipt's."""
    rb = git(top, "rev-parse", f"{sha}:scripts/verify.py", check=False)
    runner_blob = rb.stdout.strip() if rb.returncode == 0 else None
    actual = git(top, "hash-object", str(Path(__file__).resolve())).stdout.strip()
    return build_receipt(sha, run_id, lock_hashes(top, sha), runner_blob, legs,
                         None if runner_blob is None else runner_blob == actual)


def _resolve(a) -> tuple[Path, str, Path]:
    repo = Path(a.repo or Path(__file__).resolve().parents[1]).resolve()
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    sha = git(top, "rev-parse", "--verify", f"{a.sha}^{{commit}}").stdout.strip()
    return top, sha, Path(a.state_dir)


def _workers(a, leg: str) -> int:
    """--workers is the count for the leg that runs on this host (mac, or linux under --here); --linux-workers is the
    remote leg's count when the laptop orchestrates it."""
    if leg == "linux":
        if a.here or a.leg == "linux":
            return a.workers or a.linux_workers or WORKERS_LINUX
        return a.linux_workers or WORKERS_LINUX
    return a.workers or WORKERS_MAC


def _run_legs(a, top: Path, sha: str, run: Path, run_id: str, ledger: Ledger, status: Status) -> list[dict]:
    """mac on this host and linux over ssh, at once when --leg both.  A leg that fails aborts the whole run: no half receipt."""
    out: dict = {}
    t = None
    if a.leg in ("linux", "both"):
        def go() -> None:
            try:
                out["linux"] = run_leg_remote(top, sha, run, run_id, ledger, status, host=a.host,
                                              workers=_workers(a, "linux"))
            except BaseException as e:  # noqa: BLE001 - handed to the main thread
                out["linux_err"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
    try:
        if a.leg in ("mac", "both"):
            out["mac"] = run_leg_local(top, sha, run, run_id, ledger, status, leg="mac", python=a.python or MAC_PYTHON,
                                       workers=_workers(a, "mac"), timeout=a.pytest_timeout)
    except BaseException:
        terminate_children()
        if t:
            t.join(30)
        raise
    if t:
        t.join()
    if "linux_err" in out:
        raise out["linux_err"]
    return [out[k] for k in ("mac", "linux") if k in out]


def _print_legs(legs: list[dict]) -> None:
    for leg in legs:
        print(f"verify: leg {leg['platform']} python {leg['python']} workers {leg['workers']} wall {leg['duration_s']}s "
              f"max_load {leg['max_load']} load_reds {leg.get('load_reds', 0)}")
        for job in leg["jobs"]:
            print(f"verify:   {job['name']}: {job['counts']}")
            for r in job["reds"]:
                print(f"verify:   red [{r['class']}] {r['nodeid']} ledger={r['ledger_id']}")


def cmd_run(a) -> int:
    top, sha, state = _resolve(a)
    if state_in_repo(state, top):
        print(f"verify: refusing a state dir inside the checkout or under project_memory: {state}", file=sys.stderr)
        return 2
    if a.here and a.leg == "both":
        print("verify: --here runs one leg; name mac or linux", file=sys.stderr)
        return 2
    run_id = a.run_id or new_run_id(sha)
    run = state / "runs" / run_id
    lk = lock_host(state)
    if lk is None:
        print(f"verify: another receipt run holds {state / 'host.lock'}; one run per host at a time", file=sys.stderr)
        if run.exists():
            Status(run).update(state="refused", note="another receipt run holds the host lock")
        return 3
    runlock = take_run_lock(run)    # noqa: F841 - held for the life of the process
    status = Status(run)
    prev = install_signal_handlers()
    tools = Tools()
    leg_here = a.here
    ledger = None if leg_here else Ledger(Path(a.ledger) if a.ledger else state / "ledger.jsonl")
    status.update(run_id=run_id, sha=sha, leg=a.leg, state="running", pid=os.getpid(), started=now(),
                  workers=_workers(a, "linux" if a.leg == "linux" else "mac"), here=bool(leg_here), log=str(run / "runner.log"))
    print(f"verify: run {run_id} sha {sha[:12]} leg {a.leg}{' (here)' if leg_here else ''}, run dir {run}", flush=True)
    try:
        sweep_runs(state, top, tools, current=run_id)
        if leg_here:
            leg_name = "mac" if a.leg == "mac" else "linux"
            leg = run_leg_local(top, sha, run, run_id, None, status, leg=leg_name,
                                python=a.python or (MAC_PYTHON if leg_name == "mac" else LINUX_PYTHON),
                                workers=_workers(a, leg_name), timeout=a.pytest_timeout)
            out = Path(a.leg_json) if a.leg_json else run / f"leg-{leg_name}.json"
            out.write_text(json.dumps(leg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            status.update(state="leg-done", leg_json=str(out), max_load=leg["max_load"], load_reds=leg["load_reds"])
            print(f"verify: leg written to {out}")
            return 0
        legs = _run_legs(a, top, sha, run, run_id, ledger, status)
        diag = [d for leg in legs for d in leg.pop("_diag", [])]
        receipt = assemble_receipt(top, sha, run_id, legs)
        if not a.no_diagnosis:
            dp = diagnose(run, top, sha, diag)
            if dp is not None:
                receipt["diagnosis"] = str(dp)
        path = write_receipt(receipt, Path(a.receipts_dir) if a.receipts_dir else top / "project_memory" / "receipts")
        status.update(state="passed" if receipt["verdict"] == "green" else "red", verdict=receipt["verdict"],
                      receipt=str(path), max_load=max(l["max_load"] for l in legs),
                      load_reds=sum(l.get("load_reds", 0) for l in legs))
        print(f"verify: {receipt['verdict'].upper()}  receipt {path}")
        _print_legs(legs)
        for d in diag:
            if d.get("ledger_new"):
                print(f"verify: BOARD ROW OWED for new ledger entry ({d['class']}): {d['nodeid']}")
        for w in receipt["verdict_why"]:
            print(f"verify: why red: {w}")
        return 0 if receipt["verdict"] == "green" else 1
    except Killed as k:
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, signal.SIG_IGN)
        reap_run(run, top, tools, keep_files=True)
        status.update(state="killed", signal=int(k.signum))
        print(f"verify: killed by signal {int(k.signum)}; worktrees, venvs and mounted images cleaned", file=sys.stderr)
        return 128 + int(k.signum)
    except Exception as e:  # noqa: BLE001 - the failure is the report; no receipt is better than a half receipt
        status.update(state="error", error=f"{type(e).__name__}: {e}")
        print(f"verify: FAILED before a receipt could be written: {e}", file=sys.stderr)
        return 4
    finally:
        restore_signal_handlers(prev)


def passthrough(a) -> list[str]:
    out = ["--state-dir", a.state_dir]
    for flag, val in (("--repo", a.repo), ("--ledger", a.ledger), ("--receipts-dir", a.receipts_dir), ("--python", a.python),
                      ("--workers", a.workers), ("--linux-workers", a.linux_workers), ("--host", a.host),
                      ("--pytest-timeout", a.pytest_timeout)):
        if val is not None:
            out += [flag, str(val)]
    if a.no_diagnosis:
        out.append("--no-diagnosis")
    return out


def cmd_start(a) -> int:
    top, sha, state = _resolve(a)
    if state_in_repo(state, top):
        print(f"verify: refusing a state dir inside the checkout or under project_memory: {state}", file=sys.stderr)
        return 2
    run_id = new_run_id(sha)
    run = state / "runs" / run_id
    run.mkdir(parents=True)
    log = run / "runner.log"
    status = Status(run)
    status.update(run_id=run_id, sha=sha, leg=a.leg, state="starting", started=now(), log=str(log))
    argv = [sys.executable, str(Path(__file__).resolve()), "run", "--sha", sha, "--leg", a.leg, "--run-id", run_id,
            *passthrough(a)]
    with open(log, "ab") as lf:
        p = subprocess.Popen(argv, cwd=str(top), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
    print(f"run_id: {run_id}")
    print(f"log: {log}")
    print(f"status: python {Path(__file__).resolve()} status --run {run_id} --state-dir {state}")
    return 0


def cmd_status(a) -> int:
    runs = Path(a.state_dir) / "runs"
    if a.run:
        run = runs / a.run
    else:
        dirs = sorted(d for d in runs.iterdir() if d.is_dir()) if runs.is_dir() else []
        run = dirs[-1] if dirs else None
    if run is None or not run.is_dir():
        print(f"verify: no such run under {runs}", file=sys.stderr)
        return 2
    st = effective_status(run)
    if a.json:
        print(json.dumps({"run": run.name, **st}, indent=2, sort_keys=True))
        return 0
    print(f"state: {st.get('state', 'unknown')}")
    print(f"run: {run.name}")
    for k in ("sha", "leg", "pid", "started", "updated", "phase_mac", "phase_linux", "workers", "max_load", "load_reds",
              "verdict", "receipt", "log", "note", "error"):
        if st.get(k) is not None:
            print(f"{k}: {st[k]}")
    return 0


def cmd_sweep(a) -> int:
    repo = Path(a.repo or Path(__file__).resolve().parents[1]).resolve()
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    done = sweep_runs(Path(a.state_dir), top, Tools())
    print(f"verify: sweep reaped {len(done)} run dir(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=None, help="checkout to take the sha from (default: the checkout holding this script)")
        p.add_argument("--state-dir", default=str(DEFAULT_STATE), help="host lock, run dirs and ledger live here")
        p.add_argument("--ledger", default=None, help="default: <state-dir>/ledger.jsonl")
        p.add_argument("--receipts-dir", default=None, help="default: <repo>/project_memory/receipts")
        p.add_argument("--python", default=None, help="the mac leg's interpreter (default %s); the linux leg uses %s" % (MAC_PYTHON, LINUX_PYTHON))
        p.add_argument("--workers", type=int, default=None, help="xdist workers for the leg run on this host (default %d mac, %d linux)" % (WORKERS_MAC, WORKERS_LINUX))
        p.add_argument("--linux-workers", type=int, default=None, help="xdist workers on the remote host (default %d)" % WORKERS_LINUX)
        p.add_argument("--host", default=REMOTE_HOST, help="ssh host alias of the linux leg")
        p.add_argument("--pytest-timeout", type=float, default=5400.0, help="seconds for each full pytest call")
        p.add_argument("--no-diagnosis", action="store_true", help="skip the on-red model call")

    rp = sub.add_parser("run", help="run the verification receipt for a sha, in the foreground")
    rp.add_argument("--sha", required=True)
    rp.add_argument("--leg", choices=("mac", "linux", "both"), default="mac")
    rp.add_argument("--here", action="store_true", help="run the leg on THIS host and write its JSON (what the remote end runs)")
    rp.add_argument("--leg-json", default=None)
    rp.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    common(rp)
    sp = sub.add_parser("start", help="launch a run detached; prints the run id and the log path")
    sp.add_argument("--sha", required=True)
    sp.add_argument("--leg", choices=("mac", "linux", "both"), default="mac")
    common(sp)
    st = sub.add_parser("status", help="state of a run: running, passed, red, killed")
    st.add_argument("--run", default=None, help="run id (default: the newest)")
    st.add_argument("--state-dir", default=str(DEFAULT_STATE))
    st.add_argument("--json", action="store_true")
    sw = sub.add_parser("sweep", help="reap earlier runs that died without cleaning up")
    sw.add_argument("--state-dir", default=str(DEFAULT_STATE))
    sw.add_argument("--repo", default=None)
    a = ap.parse_args(argv)
    return {"run": cmd_run, "start": cmd_start, "status": cmd_status, "sweep": cmd_sweep}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
