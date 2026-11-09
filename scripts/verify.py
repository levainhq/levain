#!/usr/bin/env python3
"""Local verification receipt runner (design: L2_local_receipt_design.md).

usage:
  verify.py run    --sha S [--leg mac|linux|both]   foreground
  verify.py start  --sha S [--leg ...]              detached: prints the run id and the log path, then exits
  verify.py status [--run ID]                       running / passed / red / killed, from the run's status file
  verify.py sweep  [--state-dir D]                  clean earlier runs that died (logs and status kept for a TTL)
  verify.py ledger attach-row --id LG --row ROW     the board row that owns an excused red (a red rides only with one)
  verify.py reverdict --run ID                      recompute a finished run's verdict from the ledger's board rows

A receipt is green only with BOTH legs (macos, linux) present and clean; one leg alone is "partial".

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

Linux-leg interpreter (11-09): the host's distro python3.12, not uv's standalone 3.13: that build has no
os.pidfd_open, which the code under test uses, so a preflight refuses an interpreter lacking it.  CI's 3.13 linux axis
is therefore NOT run here; CI covers it at land.  The runner also sets umask 022 on every command (as the CI runner has)
and puts pytest's basetemp in a short per-run /tmp dir (0700, removed when the run ends, whatever its result).

CI environment decision (reversed 11-09): the legs run with CI=1 and GITHUB_ACTIONS unset, because a test that reads CI
turns a host-provisioning skip into a failure and the receipt must never read green where CI reads red.  What CI
provisions for the tests (h11 for /usr/bin/python3) is provisioned inside the run dir, or the leg fails loud naming what
is missing.  Skips are also recorded by reason (skips_by_reason) so the receipt consumer sees what did not run.
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
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

SCHEMA = "levain-test-receipt/1"
MAC_PYTHON = "3.12.13"          # pinned full version; uv-managed so it is byte-identical wherever uv fetched it
LINUX_PYTHON = "/usr/bin/python3.12"   # the host's distro interpreter; path and full version are recorded on the leg
SHORT_TMP_ROOT = "/tmp"         # pytest basetemps live here: an AF_UNIX path is capped near 107 bytes, a run dir is too deep
RUNNER_UMASK = 0o022            # CI's umask; a 0002 host makes group-writable fixtures the root helpers refuse
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
FINAL_STATES = ("passed", "red", "partial")
REQUIRED_PLATFORMS = ("macos", "linux")
KEPT_LOG_TTL = 14 * 86400       # seconds a dead run's logs and status file survive the sweep (its working subdirs go at once)
BWRAP_PYTHON = LINUX_PYTHON     # the CI bwrap job runs 3.12: the same distro interpreter (uv's "3.12" can resolve to a standalone build with no pidfd_open)
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}")
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
        with open(self.path, "a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            data = f.read()
            if data and not data.endswith(b"\n"):
                # a writer died mid-line: the torn tail was never acknowledged, so it is dropped (a complete
                # record that only lost its newline is kept) before this record goes on the end
                k = data.rfind(b"\n")
                tail = data[k + 1:]
                try:
                    json.loads(tail)
                    f.write(b"\n")
                except ValueError:
                    f.truncate(k + 1)
                    print(f"verify: ledger {self.path}: dropped a torn last line ({len(tail)} bytes)", file=sys.stderr)
            f.write((json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

    def lines(self) -> list[dict]:
        """Every record.  A torn LAST line (the writer died mid-line, so the file does not end in a newline) is skipped
        with a warning; any other line that does not parse fails closed, a newline-terminated last line included: the
        writer finished that line, so it is not torn, and a ledger with a bad record is not a ledger."""
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        torn_tail = bool(text) and not text.endswith("\n")
        rows = [(i, ln) for i, ln in enumerate(text.split("\n"), 1) if ln.strip()]
        out = []
        for k, (i, ln) in enumerate(rows):
            try:
                rec = json.loads(ln)
                if not isinstance(rec, dict) or "type" not in rec:
                    raise ValueError("not a ledger record")
            except ValueError as e:
                if k == len(rows) - 1 and torn_tail:
                    print(f"verify: ledger {self.path}: skipping a torn last line (line {i})", file=sys.stderr)
                    continue
                raise RuntimeError(f"ledger {self.path} line {i} is unreadable ({e}): failing closed") from e
            out.append(rec)
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
            elif r["type"] == "board_row" and r["id"] in by_id:
                by_id[r["id"]]["board_row"] = r["row"]
        return by_id

    def board_row(self, lid: str) -> str | None:
        return (self.entries().get(lid) or {}).get("board_row") or None

    def attach_row(self, lid: str, row: str) -> None:
        if lid not in self.entries():
            raise KeyError(f"no ledger entry {lid}")
        if not str(row).strip():
            raise ValueError("an empty board row")
        self._append({"type": "board_row", "id": lid, "row": str(row).strip(), "ts": now()})

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
    """red: a leg ran nothing, a red is a regression or has no ledger entry or no board row, pytest died abnormally or
    disagrees with its own junit, or a leg's integrity check failed.  partial: nothing is red but a required leg
    is missing.  green only with both legs present and clean: a receipt never reads green where CI would read red."""
    why: list[str] = []
    for leg in legs:
        for e in leg.get("integrity", []):
            why.append(f"{leg['platform']}: {e}")
        for job in leg.get("jobs", []):
            tag = f"{leg['platform']}/{job['name']}"
            c = job["counts"]
            raw = c.get("failed", 0) + c.get("error", 0)
            rc = job.get("rc")
            if c["collected"] <= 0:
                why.append(f"{tag}: collected 0")
            if rc not in (0, 1):
                why.append(f"{tag}: pytest exit {rc}")
            elif rc == 0 and raw:
                why.append(f"{tag}: pytest exit 0 but the junit holds {raw} failed or errored")
            elif rc == 1 and raw == 0:
                why.append(f"{tag}: pytest exit 1 but the junit holds no failed or errored test")
            if raw and not job["reds"]:
                why.append(f"{tag}: the junit holds {raw} failed or errored but no red is recorded")
            if job.get("setup_failed"):
                why.append(f"{tag}: setup failed: {job['setup_failed']}")
            if job.get("required_run") and c["skipped"] > 0:
                why.append(f"{tag}: {c['skipped']} skipped under a required-run job")
            for r in job["reds"]:
                if r["class"] == "regression":
                    why.append(f"regression: {r['nodeid']}")
                elif not r.get("ledger_id"):
                    why.append(f"red without ledger_id: {r['nodeid']}")
                elif not r.get("board_row"):
                    why.append(f"no board row for ledger entry {r['ledger_id']} ({r['nodeid']}): "
                               f"verify.py ledger attach-row --id {r['ledger_id']} --row <board row>, then re-run or reverdict")
    if why:
        return "red", why
    missing = [p for p in REQUIRED_PLATFORMS if p not in {leg.get("platform") for leg in legs}]
    if missing:
        return "partial", [f"missing leg: {p}" for p in missing]
    return "green", why


def build_receipt(sha: str, run_id: str, lock: dict, runner_blob: str | None, legs: list[dict],
                  runner_matches_sha: bool | None = None) -> dict:
    verdict, why = verdict_of(legs)
    if runner_blob is None:
        verdict, why = "red", why + ["scripts/verify.py is not in the sha under test"]
    elif runner_matches_sha is False:
        verdict, why = "red", why + ["the running verify.py is not the blob committed at the sha"]
    return {"schema": SCHEMA, "sha": sha, "lock": lock, "runner_blob": runner_blob, "run_id": run_id,
            "runner_matches_sha": runner_matches_sha, "verdict": verdict, "verdict_why": why, "legs": legs}


def reverdict_receipt(receipt: dict, ledger: Ledger) -> dict:
    """Re-read every excused red's board row from the ledger and recompute the verdict (what a re-run would conclude,
    without re-running).  Classes, counts and junit hashes are the original run's; only the board rows move."""
    if "runner_matches_sha" not in receipt:
        raise ValueError("this receipt predates the runner_matches_sha field; re-run instead")
    legs = receipt["legs"]
    for leg in legs:
        for job in leg.get("jobs", []):
            for r in job["reds"]:
                if r.get("ledger_id"):
                    r["board_row"] = ledger.board_row(r["ledger_id"])
    new = build_receipt(receipt["sha"], receipt["run_id"], receipt["lock"], receipt["runner_blob"], legs,
                        receipt["runner_matches_sha"])
    if receipt.get("diagnosis"):
        new["diagnosis"] = receipt["diagnosis"]
    new["reverdicts"] = [*receipt.get("reverdicts", []), {"ts": now(), "from": receipt["verdict"], "to": new["verdict"]}]
    return new


def receipt_name(sha: str, run_id: str) -> str:
    """One file per run: a later run of the same sha never overwrites an earlier run's receipt."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(run_id)):
        raise ValueError(f"unsafe run id for a receipt file name: {run_id!r}")
    return f"vr_{sha[:12]}_{run_id}.json"


def write_receipt(receipt: dict, receipts_dir: Path, path: Path | None = None) -> Path:
    """Write atomically to the run's own file, or to `path` (a reverdict rewrites the file it read)."""
    receipts_dir.mkdir(parents=True, exist_ok=True)
    p = path or receipts_dir / receipt_name(receipt["sha"], receipt["run_id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ---------------------------------------------------------------- environment

def clean_env(run: Path) -> dict:
    """env -i plus only PATH, LANG, the wait scale, CI=1 (as on the cloud runner) and a private HOME / TMPDIR.  Nothing of
    the operator's ANTHROPIC_*, OPENAI_*, LEVAIN_* passes through."""
    return {"PATH": CLEAN_PATH, "LANG": "en_US.UTF-8", "LEVAIN_TEST_WAIT_SCALE": WAIT_SCALE, "CI": "1",
            "HOME": str(run / "home"), "TMPDIR": str(run / "tmp")}


_CHILDREN: set = set()
# re-entrant: the SIGTERM handler runs on the main thread, possibly while that same thread holds this lock in _spawn or
# in _run's finally; a plain Lock would deadlock the handler against its own thread
_CHILD_LOCK = threading.RLock()


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


def _with_umask(cmd: list[str]) -> list[str]:
    """The command under the runner's umask.  A shell sets it and execs, so no Python code runs between fork and exec
    (preexec_fn is unsafe in a process that spawns from several threads)."""
    return ["/bin/sh", "-c", f'umask {RUNNER_UMASK:03o}; exec "$@"', "sh", *cmd]


SHORT_TMP_RECORD = "short_tmp"   # the run dir's file naming the run's private short tmp dir


def make_short_tmp(run: Path) -> Path:
    """A fresh, exclusive, random, 0700 dir under SHORT_TMP_ROOT, its path recorded in the run dir so teardown and the
    sweep remove exactly that dir (a predictable name could be pre-created or symlinked by anyone)."""
    d = Path(tempfile.mkdtemp(prefix="lv-", dir=SHORT_TMP_ROOT))
    run.mkdir(parents=True, exist_ok=True)
    (run / SHORT_TMP_RECORD).write_text(str(d) + "\n", encoding="utf-8")
    return d


def recorded_short_tmp(run: Path) -> Path | None:
    try:
        txt = (run / SHORT_TMP_RECORD).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(txt) if txt else None


def remove_short_tmp(d: Path | None) -> bool:
    """Remove a recorded short tmp dir, but only if it is what we made: a real dir (not a symlink) directly under
    SHORT_TMP_ROOT, named lv-*, owned by this user.  Anything else is left alone."""
    if d is None:
        return False
    try:
        st = os.lstat(d)
    except OSError:
        return False
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or not d.name.startswith("lv-")
            or d.parent != Path(SHORT_TMP_ROOT)):
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def _spawn(cmd: list[str], *, cwd, env, stdout, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """Every child gets its own session so one kill reaches its whole group (xdist workers, browsers, mounts' helpers)."""
    p = subprocess.Popen(_with_umask(cmd), cwd=str(cwd) if cwd else None, env=env, stdin=stdin, stdout=stdout,
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


def _cmd_touches_run(cmd: str, run: Path, *extra: Path) -> bool:
    """True when some argument is a path in a SUBDIRECTORY of the run dir (a venv, a worktree), or the value of a
    `--flag=` that points under the run dir (a basetemp).  A viewer of the run's top-level log never matches."""
    for root in {str(r) for r in (run, *extra)} | {os.path.realpath(r) for r in (run, *extra)}:
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
    rec = recorded_short_tmp(run)
    st = [rec] if rec is not None else []
    pids = [pid for pid, cmd in tools.ps() if pid != me and _cmd_touches_run(cmd, run, *st)]
    for pid in pids:
        tools.kill(pid, signal.SIGTERM)
    if pids:
        time.sleep(grace)
        alive = {pid for pid, cmd in tools.ps() if _cmd_touches_run(cmd, run, *st)}
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
    remove_short_tmp(rec)
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


def _has_working_subdirs(d: Path) -> bool:
    return any(c.is_dir() and not c.is_symlink() for c in d.iterdir())


def sweep_runs(state: Path, repo: Path, tools: Tools, *, current: str | None = None, say=print) -> list[dict]:
    """Clean every earlier run dir that no live run holds and that did not finish with a receipt.  Only its working
    subdirectories go (worktrees, venvs, basetemps, mounted images); its logs and status file stay for KEPT_LOG_TTL
    seconds so a run that died is still diagnosable, then the whole dir goes.  A finished run (passed, red or partial)
    keeps its dir: its junit files are what the receipt's hashes point at."""
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
        stamp = sp.stat().st_mtime if sp.exists() else d.stat().st_mtime
        if st.get("state") == "starting" and time.time() - stamp < STARTING_GRACE:
            continue
        if time.time() - stamp > KEPT_LOG_TTL:
            say(f"verify: sweep: removing {d.name} (state {st.get('state', 'unknown')}), its logs are past {KEPT_LOG_TTL // 86400} days")
            done.append(reap_run(d, repo, tools, say=say))
            continue
        if st.get("state") in ("starting", "running"):
            Status(d).update(state="killed", note="the runner is gone and holds no lock (found by a sweep)")
        if not _has_working_subdirs(d):
            continue        # swept already: the logs and the status file are all that is left
        say(f"verify: sweep: cleaning earlier run {d.name} (state {st.get('state', 'unknown')}), keeping its logs")
        done.append(reap_run(d, repo, tools, keep_files=True, say=say))
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


def failure_signature(text: str) -> tuple[str, str]:
    """(exception type, first message line) of a junit failure text, with addresses and absolute paths blanked so two runs
    of the same failure compare equal.  The type is empty when the first line names none (a bare assert)."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    first = lines[0].strip() if lines else ""
    first = re.sub(r"0x[0-9a-fA-F]+", "0x?", first)
    first = re.sub(r"(?<![\w.])/[^\s'\"):,]+", "<path>", first)
    m = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Failed|Interrupt))\b", first)
    return (m.group(1) if m else ""), first


def _baseline_result(b) -> tuple[str, tuple[str, str] | None]:
    """A baseline answer is 'red'|'green'|'absent', {'state': 'red', 'sig': (type, line)}, or {'state': 'unusable', 'why': ...}."""
    if isinstance(b, dict):
        sig = b.get("sig")
        return b.get("state", "absent"), (tuple(sig) if sig else None)
    return b, None


def classify(reds: list[dict], *, ledger: Ledger | None, run_id: str, sha: str, touched: set[str],
             rerun, baseline) -> list[dict]:
    """Deterministic classification, exactly the design page.  `rerun(nodeid) -> bool` is one serial run (True = pass);
    `baseline(nodeids) -> dict[nodeid -> 'red'|'green'|'absent' | {'state': 'red', 'sig': (type, first line)} |
    {'state': 'unusable', 'why': ...}]` runs them on a fresh checkout of the pinned land base, serially.  A red inherits
    only when the base fails the same way (same exception type and first message line); a different failure on the sha,
    or a baseline run that exited abnormally or disagrees with its own junit, is a regression.
    `ledger=None` (a remote leg: the laptop is the only ledger writer) classifies and files nothing; the laptop then
    runs `apply_ledger_to_leg` over the copied-back leg."""
    out: list[dict] = []
    pending: list[dict] = []
    for r in reds:
        nid = r["nodeid"]
        rec = {"nodeid": nid, "class": None, "evidence": "", "ledger_id": None, "board_row": None, "kind": r.get("kind"),
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
            raw = base.get(rec["nodeid"], "absent")
            b, bsig = _baseline_result(raw)
            if b == "unusable":
                rec.update({"class": "regression",
                            "evidence": rec["evidence"] + f"; the {MAIN_REF} baseline run is not usable ({raw.get('why')}), "
                                                          f"so nothing is inherited from it"})
            elif b == "red":
                mine = failure_signature(rec["text"])
                if bsig is None:
                    rec.update({"class": "regression",
                                "evidence": rec["evidence"] + f"; red on {MAIN_REF} but its failure was not recorded, so sameness is unproven"})
                elif tuple(bsig) == mine:
                    rec.update({"class": "inherited", "evidence": rec["evidence"] + f"; also red on {MAIN_REF}, the same failure ({mine[0] or 'assert'}: {mine[1][:80]})"})
                else:
                    rec.update({"class": "regression",
                                "evidence": rec["evidence"] + f"; red on {MAIN_REF} but with a different failure "
                                            f"(base {tuple(bsig)[1][:80]!r}, here {mine[1][:80]!r})"})
            elif b == "green":
                rec.update({"class": "regression", "evidence": rec["evidence"] + f"; passes on {MAIN_REF}"})
            else:
                rec.update({"class": "regression", "evidence": rec["evidence"] + f"; not present on {MAIN_REF}"})
    if ledger is not None:
        for rec in out:
            if rec["class"] in ("load", "flaky", "inherited"):
                rec["ledger_id"], new = ledger.file(rec["nodeid"], rec["class"], run_id, sha, rec["evidence"])
                rec["ledger_new"] = new
                rec["board_row"] = ledger.board_row(rec["ledger_id"])
    return out


def apply_ledger_to_leg(leg: dict, ledger: Ledger, run_id: str, sha: str) -> None:
    """The ledger half of classification for a leg that was classified elsewhere without a ledger: an inherited red
    that the ledger already holds as load/flaky and that failed every serial rerun is a regression (never excused);
    every other excused red is filed (entry or sighting) and gets its ledger_id and board row.  Mutates `leg` and its
    `_diag`."""
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
                r["board_row"] = ledger.board_row(lid)
                if r["nodeid"] in diag:
                    diag[r["nodeid"]]["ledger_new"] = new


# ---------------------------------------------------------------- the mac leg

def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def lock_host(state: Path, _retry: bool = True):
    """One receipt run per host.  A lock still held after its recorded pid is gone (a leaked descriptor in a child that
    outlived a killed run) is reclaimed: the stale file is unlinked and a fresh one taken.  A live or unreadable
    holder is never reclaimed.  Two starters racing on the same stale file is the one edge this leaves open."""
    state.mkdir(parents=True, exist_ok=True)
    path = state / "host.lock"
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            f.seek(0)
            pid = int(f.read().split()[0])
        except (ValueError, IndexError, OSError):
            pid = None
        f.close()
        if _retry and pid is not None and pid != os.getpid() and _pid_dead(pid):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return lock_host(state, _retry=False)
        return None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()} {now()}\n")
    f.flush()
    return f


def _minor(python: str) -> str:
    """'3.12.13' -> '312'; a path such as '/usr/bin/python3.12' -> '312'."""
    m = re.search(r"(\d+)\.(\d+)", os.path.basename(python))
    return "".join(m.groups()) if m else "".join(python.split(".")[:2])


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
        self._main: dict[str, tuple[Path, Path]] = {}
        self.touched: set[str] = set()
        self.base_sha = ""
        self.h11_site: Path | None = None
        self.tmp: Path | None = None

    def progress(self, phase: str) -> None:
        if self.status is not None:
            self.status.update(**{f"phase_{self.leg_name}": phase})

    def prepare(self) -> None:
        for d in (self.run / "home", self.run / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        self.tmp = make_short_tmp(self.run)
        # the land base is read once, here, as a sha: every classification and baseline of this leg uses this one value
        self.base_sha = git(self.repo, "rev-parse", "--verify", f"{MAIN_REF}^{{commit}}", check=False).stdout.strip()
        base = self.base_sha or MAIN_REF
        self.touched = set(git(self.repo, "diff", "--name-only", f"{base}...{self.sha}", check=False).stdout.split())

    def make(self, name: str, sha: str, python: str, lock_src: Path | None = None) -> tuple[Path, Path]:
        self.names.append(name)
        return make_env(self.repo, sha, self.run, name, python, self.log, lock_src=lock_src)

    def main_env(self, python: str | None = None) -> tuple[Path, Path]:
        """The worktree + venv at the land base, built once per interpreter and only when a red needs a baseline."""
        python = python or self.python
        if python not in self._main:
            if not self.base_sha:
                raise RuntimeError(f"{MAIN_REF} is not present: no baseline can be built")
            self.progress("building the main baseline")
            self._main[python] = self.make(f"src_at_main{_minor(python)}", self.base_sha, python, lock_src=self.src)
        return self._main[python]

    def teardown(self) -> None:
        for n in self.names:
            remove_worktree(self.repo, self.run, n)
        shutil.rmtree(self.run / "venv39", ignore_errors=True)
        remove_short_tmp(self.tmp)
        git(self.repo, "worktree", "prune", check=False)


def rerun_passed(junit: Path) -> bool:
    """A serial rerun counts as a pass only when its own junit says the test PASSED: a skip, an xfail or no testcase is not."""
    try:
        c = parse_junit(junit)["counts"]
    except (OSError, ET.ParseError):
        return False
    return c["passed"] >= 1 and not (c["failed"] or c["error"] or c["skipped"] or c["xfailed"])


def run_job(ctx: Ctx, *, name: str, py: Path, cwd: Path, env: dict, targets: list[str], junit_name: str,
            workers: int | None, timeout: float, flags: tuple = (), rerun_flags: tuple = (), required_run: bool = False,
            base_cfg=None, base_python: str | None = None) -> tuple[dict, list[dict]]:
    """One pytest job: run it under xdist, classify its reds, return (job dict for the receipt, diagnosis records).
    `base_cfg(bsrc, bpy) -> (python, env, flags)` adapts the baseline run to the job's interpreter and flags;
    `base_python` names the interpreter the baseline checkout is built with (default: the leg's)."""
    junit = ctx.run / junit_name
    xd = ["-n", str(workers), "--dist", "loadfile"] if workers else []
    ctx.progress(f"{name}: pytest")
    rc = _run(pytest_cmd(py, *xd, *flags, f"--junitxml={junit}", f"--basetemp={ctx.tmp / ('pt-' + name)}", *targets),
              cwd=cwd, env=env, log=ctx.log, timeout=timeout)
    if not junit.exists():
        raise RuntimeError(f"{name}: pytest produced no junit (exit {rc}); see {ctx.log}")
    parsed = parse_junit(junit, cwd)
    n_rerun = [0]
    binfo: dict = {}

    def rerun(nid: str) -> bool:
        n_rerun[0] += 1
        rj = ctx.run / f"junit-rerun-{name}-{n_rerun[0]}.xml"
        rc2 = _run(pytest_cmd(py, nid, *rerun_flags, f"--junitxml={rj}", f"--basetemp={ctx.tmp / ('pt-%s-r%d' % (name, n_rerun[0]))}"),
                   cwd=cwd, env=env, log=ctx.log, timeout=900)
        ok = rc2 == 0 and rerun_passed(rj)
        rj.unlink(missing_ok=True)
        return ok

    def baseline(nids: list[str]) -> dict:
        bsrc, bpy = ctx.main_env(base_python)
        bj = ctx.run / f"junit-main-{name}.xml"
        if base_cfg:
            bp_py, benv, bflags = base_cfg(bsrc, bpy)
        else:
            bp_py, benv, bflags = bpy, {**env, "PATH": f"{bpy.parent}:{CLEAN_PATH}"}, rerun_flags
        brc = _run(pytest_cmd(bp_py, *nids, *bflags, f"--junitxml={bj}", f"--basetemp={ctx.tmp / ('pt-main-' + name)}"),
                   cwd=bsrc, env=benv, log=ctx.log, timeout=1800)
        binfo.update(sha=ctx.base_sha, rc=brc)
        if not bj.exists():
            return {}
        bparsed = parse_junit(bj, bsrc)
        red = {r["nodeid"]: r for r in bparsed["reds"]}
        seen = {_nodeid(tc.get("classname", ""), tc.get("name", ""), bsrc) for tc in ET.parse(str(bj)).iter("testcase")}
        binfo.update(junit_sha256=sha256_file(bj), junit=str(bj))
        braw = bparsed["counts"]["failed"] + bparsed["counts"]["error"]
        if brc not in (0, 1):
            unusable = f"pytest exit {brc}"
        elif (brc == 0) != (braw == 0):
            unusable = f"pytest exit {brc} but its junit holds {braw} failed or errored"
        else:
            unusable = None
        if unusable:
            # an abnormal run can still hold a matching failure; it proves nothing about main, so nothing inherits from it
            return {n: {"state": "unusable", "why": unusable} for n in nids}
        return {n: ({"state": "red", "sig": list(failure_signature(red[n]["text"]))} if n in red
                    else "green" if n in seen else "absent") for n in nids}

    ctx.progress(f"{name}: classifying {len(parsed['reds'])} reds")
    reds = classify(parsed["reds"], ledger=ctx.ledger, run_id=ctx.run_id, sha=ctx.sha, touched=ctx.touched,
                    rerun=rerun, baseline=baseline)
    job = {"name": name, "rc": rc, "required_run": required_run, "counts": parsed["counts"],
           "skips_by_reason": parsed["skips_by_reason"], "junit": {"path": str(junit), "sha256": sha256_file(junit)},
           "baseline": dict(binfo) if binfo else None,
           "load_reds": sum(1 for r in reds if r["class"] == "load"),
           "reds": [{k: v for k, v in r.items() if k not in ("kind", "ledger_new", "text")} for r in reds]}
    diag = [{"nodeid": r["nodeid"], "class": r["class"], "evidence": r["evidence"], "text": r.get("text", ""),
             "ledger_new": bool(r.get("ledger_new")), "job": name} for r in reds]
    return job, diag


def failed_job(name: str, why: str) -> dict:
    return {"name": name, "rc": None, "setup_failed": why, "required_run": False,
            "counts": dict(collected=0, passed=0, failed=0, error=0, skipped=0, xfailed=0), "skips_by_reason": {},
            "junit": None, "load_reds": 0, "reds": []}


# ---------------------------------------------------------------- CI parity: what CI provisions for the tests

SYSTEM_PYTHON = "/usr/bin/python3"


def provision_h11(ctx: Ctx, py: Path, src: Path, env: dict) -> tuple[Path | None, str | None]:
    """CI installs h11 (the floor root vendors) for the runner's /usr/bin/python3 so the root-py tests run instead of
    skipping.  Here it goes INSIDE the run dir: h11 is pure Python, so one `pip install --target` from the leg's own venv
    serves any interpreter, and the jobs' PYTHONPATH exposes it.  Nothing is written outside the run dir.  Returns
    (site dir, None) or (None, why): a leg that cannot provision it fails loud, it never skips the root runs."""
    site = ctx.run / "h11site"
    rc, out = _capture([str(py), "tests/ci/root_closure.py", "h11-spec"], cwd=src, env=env, log=ctx.log)
    spec = out.strip()
    if rc or not spec:
        return None, "h11 for /usr/bin/python3 not provisioned: tests/ci/root_closure.py h11-spec gave no requirement"
    if _run([str(py), "-m", "pip", "install", "--no-deps", "--target", str(site), spec], cwd=src, env=env, log=ctx.log, timeout=900):
        return None, f"h11 for /usr/bin/python3 not provisioned: pip install --target of {spec} failed (see log)"
    probe = subprocess.run([SYSTEM_PYTHON, "-c", "import h11"], capture_output=True, text=True, timeout=60,
                           env={**env, "PYTHONPATH": str(site)}) if os.path.exists(SYSTEM_PYTHON) else None
    if probe is None:
        return None, f"h11 for /usr/bin/python3 not provisioned: {SYSTEM_PYTHON} does not exist on this host"
    if probe.returncode != 0:
        return None, f"h11 for /usr/bin/python3 not provisioned: {SYSTEM_PYTHON} cannot import it from the run dir: {probe.stderr.strip()[-200:]}"
    return site, None


def host_probe(cmd: list[str], env: dict | None = None, timeout: float = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def runtime_dir(uid: int | None = None) -> str:
    """The user runtime dir (the user bus lives there) for `uid`, this process's by default."""
    return f"/run/user/{os.getuid() if uid is None else uid}"


def preflight_linux(probe=host_probe, *, which=shutil.which, exists=os.path.exists, access=os.access,
                    uid: int | None = None, user: str | None = None) -> list[str]:
    """The host capabilities the CI linux-live job provisions before it runs the bwrap floor tests.  Returns what is
    missing (empty = ready).  It only looks: it never changes a sysctl or installs anything; provisioning is the
    host owner's call."""
    uid = os.getuid() if uid is None else uid
    if user is None:
        try:
            import pwd
            user = pwd.getpwuid(uid).pw_name
        except Exception:  # noqa: BLE001
            user = str(uid)
    missing: list[str] = []
    if not which("bwrap"):
        missing.append("bubblewrap (bwrap) is not installed")
    else:
        rc, _ = probe(["bwrap", "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--unshare-pid", "/bin/true"])
        if rc != 0:
            missing.append("bwrap cannot make an unprivileged user namespace (kernel.apparmor_restrict_unprivileged_userns?)")
    rc, out = probe(["loginctl", "show-user", user, "-p", "Linger"])
    if rc != 0 or "Linger=yes" not in out:
        missing.append(f"loginctl linger is not enabled for {user}")
    rc, _ = probe(["systemd-run", "--user", "--scope", "--quiet", "--collect", "/bin/true"],
                  env={**os.environ, "XDG_RUNTIME_DIR": runtime_dir(uid)})
    if rc != 0:
        missing.append("systemd-run --user --scope does not work (no user manager or bus for this account)")
    if exists("/run/podman") and not access("/run/podman", os.R_OK | os.X_OK):
        missing.append(f"/run/podman is not readable by {user} (chmod o+rx /run/podman)")
    return missing


# ---------------------------------------------------------------- the legs' plans

def plan_mac(ctx: Ctx, python: str, workers: int, timeout: float) -> tuple[list[dict], list[dict], str]:
    ctx.python = python
    ctx.prepare()
    ctx.progress("building the environment")
    src, py = ctx.make("src_at_sha", ctx.sha, python)
    ctx.src = src
    env = {**ctx.env, "PATH": f"{py.parent}:{CLEAN_PATH}"}
    site, why = provision_h11(ctx, py, src, env)
    if why:
        return [failed_job("ci-parity", why)], [], venv_version(py, env)
    env["PYTHONPATH"] = str(site)
    job, diag = run_job(ctx, name=f"pytest-full-py{_minor(python)}", py=py, cwd=src, env=env, targets=["tests"],
                        junit_name="junit-mac.xml", workers=workers, timeout=timeout)
    return [job], diag, venv_version(py, env)


def linux_job_names(python: str = LINUX_PYTHON) -> list[str]:
    """The jobs `plan_linux` runs, by name, in order."""
    return [f"pytest-full-py{_minor(python)}", f"bwrap-floor-live-py{_minor(BWRAP_PYTHON)}",
            f"root-closure-py{_minor(CLOSURE_PYTHON)}"]


def preflight_python(python: str, probe=host_probe) -> str | None:
    """None when `python` has os.pidfd_open (the code under test needs it), else the reason it cannot be the linux leg's."""
    rc, out = probe([python, "-c", "import os,sys;sys.exit(0 if hasattr(os,'pidfd_open') else 3)"])
    if rc == 0:
        return None
    if rc == 3:
        return f"interpreter {python} has no os.pidfd_open (a python-build-standalone build?); the linux leg needs the distro python"
    return f"interpreter {python} cannot run (exit {rc}): {out.strip()[-200:]}"


def plan_linux(ctx: Ctx, python: str, workers: int, timeout: float) -> tuple[list[dict], list[dict], str]:
    """Three jobs, as CI runs them: the full suite without the static-guard files, the live floor tests on 3.12 where a
    skip fails (after a host preflight), and the root-run import closure on the oldest interpreter."""
    ctx.python = python
    ctx.prepare()
    ctx.progress("interpreter preflight")
    bad = preflight_python(python)
    if bad:
        return [failed_job("ci-parity", "interpreter preflight: " + bad)], [], "unknown"
    ctx.progress("building the environment")
    src, py = ctx.make("src_at_sha", ctx.sha, python)
    ctx.src = src
    env = {**ctx.env, "PATH": f"{py.parent}:{CLEAN_PATH}"}
    site, why = provision_h11(ctx, py, src, env)
    if why:
        return [failed_job("ci-parity", why)], [], venv_version(py, env)
    env["PYTHONPATH"] = str(site)
    full_name, bw_name, name39 = linux_job_names(python)
    not_static = ("-m", "not static_guard")
    jobs, diag = [], []
    j, d = run_job(ctx, name=full_name, py=py, cwd=src, env=env, targets=["tests"], junit_name="junit-linux.xml",
                   workers=workers, timeout=timeout, flags=not_static, rerun_flags=not_static)
    jobs.append(j)
    diag += d

    ctx.progress(f"{bw_name}: host preflight")
    missing = preflight_linux()
    if missing:
        jobs.append(failed_job(bw_name, "host not provisioned: " + "; ".join(missing)))
    else:
        bw_bad = preflight_python(BWRAP_PYTHON)
        try:
            if bw_bad:
                raise RuntimeError(bw_bad)
            bsrc, bpy = ctx.make("src_bwrap", ctx.sha, BWRAP_PYTHON)
        except RuntimeError as e:
            jobs.append(failed_job(bw_name, f"environment: {e}"))
        else:
            # the user bus the preflight probed is the one the job and its baseline must reach
            live = {"LEVAIN_REQUIRE_LINUX_LIVE": "1", "XDG_RUNTIME_DIR": runtime_dir()}
            benv = {**ctx.env, "PATH": f"{bpy.parent}:{CLEAN_PATH}", **live}
            j, d = run_job(ctx, name=bw_name, py=bpy, cwd=bsrc, env=benv, targets=list(BWRAP_TARGETS),
                           junit_name="junit-linux-floor.xml", workers=None, timeout=timeout, required_run=True,
                           base_python=BWRAP_PYTHON,
                           base_cfg=lambda s2, p2: (p2, {**ctx.env, "PATH": f"{p2.parent}:{CLEAN_PATH}", **live}, ()))
            jobs.append(j)
            diag += d

    ctx.progress(f"{name39}: building the venv")
    uenv = {**ctx.env, "UV_PYTHON_PREFERENCE": "managed",
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")}
    venv = ctx.run / "venv39"
    py39 = venv / "bin" / "python"
    penv = {**ctx.env, "PATH": f"{venv}/bin:{CLEAN_PATH}", "PYTHONPATH": f"{src}{os.pathsep}{site}"}
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
                   base_cfg=lambda bsrc, bpy: (py39, {**penv, "PYTHONPATH": f"{bsrc}{os.pathsep}{site}"},
                                               ("--noconftest", "--rootdir", str(bsrc))))
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
    return {"platform": "macos" if leg == "mac" else "linux", "sha": sha, "base_sha": ctx.base_sha,
            "host": socket.gethostname(), "python": pyver, "python_path": python,
            "host_fingerprint": host_fingerprint(), "started": started, "ended": now(),
            "duration_s": round(time.time() - t0, 1), "workers": workers, "max_load": max_load,
            "load_reds": sum(j.get("load_reds", 0) for j in jobs), "jobs": jobs, "_diag": diag}


# ---------------------------------------------------------------- the linux leg, from the laptop

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10"]


def default_sh(cmd: list[str], *, log: Path, stdin: Path | None = None, timeout: float | None = None) -> int:
    return _run(cmd, cwd=Path.cwd(), env=dict(os.environ), log=log, timeout=timeout, stdin=stdin)


def remote_stop_cmd(host: str, run_id: str, remove_dir: bool) -> list[str]:
    """The ssh command that stops a remote run: TERM its runner (whose own handler stops its process groups, removes its
    worktrees and clears its working dirs), wait for it, KILL a runner that will not go, and optionally remove the run
    dir.  The pid comes from the run's status file and is signalled only when its command line carries this run id."""
    assert RUN_ID_RE.fullmatch(run_id), "unsafe run id"
    q = shlex.quote
    rrun = f"{REMOTE_STATE}/runs/{run_id}"
    script = (f"pid=$(sed -n 's/.*\"pid\": *\\([0-9][0-9]*\\).*/\\1/p' {q(rrun)}/status.json 2>/dev/null | head -n 1); "
              f"if [ -n \"$pid\" ] && tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null | grep -q -- '--run-id {run_id}'; then "
              f"kill -TERM \"$pid\"; i=0; while kill -0 \"$pid\" 2>/dev/null && [ $i -lt 120 ]; do sleep 0.5; i=$((i+1)); done; "
              f"kill -0 \"$pid\" 2>/dev/null && kill -KILL \"$pid\"; fi; "
              + (f"rm -rf {q(rrun)}; " if remove_dir else "") + "true")
    return ["ssh", *SSH_OPTS, host, script]


def rederive_leg(leg: dict, sha: str) -> list[str]:
    """What the laptop can redo from the junit files it copied back, against what the remote leg JSON claims: the leg's
    sha, every job's counts and skip reasons, and the set of red nodeids.  Any disagreement is a string; none = [].
    The junit paths in `leg` must already point at the local copies.  The job set must be exactly the linux plan's (each
    job once), or the plan's lone early setup failure: a leg that drops or adds a job never re-derives clean."""
    errs: list[str] = []
    if leg.get("sha") != sha:
        errs.append(f"the leg JSON records sha {leg.get('sha')!r}, the sha under test is {sha}")
    if leg.get("platform") != "linux":
        errs.append(f"the leg JSON records platform {leg.get('platform')!r}, the remote leg is linux")
    jobs = leg.get("jobs") if isinstance(leg.get("jobs"), list) else []
    names = sorted(str(j.get("name")) for j in jobs)
    early_setup_failure = len(jobs) == 1 and jobs[0].get("name") == "ci-parity" and jobs[0].get("setup_failed")
    if names != sorted(linux_job_names()) and not early_setup_failure:
        errs.append(f"the leg JSON holds jobs {names}, the linux leg runs exactly {sorted(linux_job_names())}")
    for job in jobs:
        jr = job.get("junit")
        if not jr:
            if not job.get("setup_failed"):
                errs.append(f"{job.get('name')}: no junit recorded")
            continue
        try:
            parsed = parse_junit(Path(jr["path"]))
        except (OSError, ET.ParseError) as e:
            errs.append(f"{job.get('name')}: the copied junit does not parse ({e})")
            continue
        if parsed["counts"] != job["counts"]:
            errs.append(f"{job['name']}: the leg JSON counts {job['counts']} but its junit holds {parsed['counts']}")
        if parsed["skips_by_reason"] != job.get("skips_by_reason", {}):
            errs.append(f"{job['name']}: the leg JSON skip reasons differ from its junit")
        got = sorted(r["nodeid"] for r in parsed["reds"])
        want = sorted(r["nodeid"] for r in job["reds"])
        if got != want:
            errs.append(f"{job['name']}: the red tests in the leg JSON differ from its junit (junit only: "
                        f"{sorted(set(got) - set(want))[:3]}, leg JSON only: {sorted(set(want) - set(got))[:3]})")
    return errs


def run_leg_remote(repo: Path, sha: str, run: Path, run_id: str, ledger: Ledger, status: Status | None, *,
                   host: str = REMOTE_HOST, workers: int = WORKERS_LINUX, timeout: float = 4 * 3600,
                   sh=default_sh, script: Path | None = None) -> dict:
    """Ship the sha as a bundle over ssh, run this same script there with --here, copy the leg JSON and junit back, and
    re-derive the leg from the copied junit.  A failure after the runner started stops the remote run.
    `sh(cmd, log=, stdin=, timeout=) -> exit code` is the only way out of the process; tests substitute it."""
    script = script or Path(__file__).resolve()
    ldir = run / "linux"
    ldir.mkdir(parents=True, exist_ok=True)
    log = run / "linux.log"
    q = shlex.quote
    rrun = f"{REMOTE_STATE}/runs/{run_id}"
    assert RUN_ID_RE.fullmatch(run_id) and re.fullmatch(r"[0-9a-f]{40}", sha), "unsafe run id or sha"
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
    base = git(repo, "rev-parse", "--verify", f"{main_ref}^{{commit}}").stdout.strip()

    try:
        step("shipping the bundle", ssh + [f"umask 022 && mkdir -p {q(rrun)}/xfer && cat > {q(rrun)}/xfer/sha.bundle"], stdin=bundle)
        step("shipping the runner", ["scp", "-q", *SSH_OPTS, str(script), f"{host}:{rrun}/verify.py"])
        remote = (f"umask 022 && cd {q(rrun)} && git init -q xfer/repo && "
                  f"git -C xfer/repo fetch -q ../sha.bundle +{ref}:{ref} +{main_ref}:{main_ref} && "
                  f"python3 verify.py run --here --leg linux --sha {sha} --repo xfer/repo --run-id {run_id} "
                  f"--workers {int(workers)} --leg-json leg-linux.json --base-sha {base}")
        step("running the linux leg on " + host, ssh + [remote], timeout=timeout)
        step("copying the leg JSON back", ["scp", "-q", *SSH_OPTS, f"{host}:{rrun}/leg-linux.json", str(ldir) + "/"])
        leg = json.loads((ldir / "leg-linux.json").read_text(encoding="utf-8"))
        for job in leg.get("jobs", []):
            jr = job.get("junit")
            if not jr:
                continue
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", jr["path"]):
                raise RuntimeError(f"linux leg: unsafe junit path in the leg JSON: {jr['path']!r}")
            base_name = os.path.basename(jr["path"])
            step(f"copying {base_name} back", ["scp", "-q", *SSH_OPTS, f"{host}:{jr['path']}", str(ldir) + "/"])
            local = ldir / base_name
            got = sha256_file(local) if local.exists() else None
            if got != jr["sha256"]:
                raise RuntimeError(f"linux leg: {base_name} copied back with sha256 {got}, the leg recorded {jr['sha256']}")
            jr["path"] = str(local)
    except BaseException:
        try:
            sh(remote_stop_cmd(host, run_id, remove_dir=False), log=log, timeout=90)
        except Exception:  # noqa: BLE001 - a failed cleanup must not hide the failure that caused it
            pass
        raise
    leg["integrity"] = rederive_leg(leg, sha)
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


def _run_legs(a, top: Path, sha: str, run: Path, run_id: str, ledger: Ledger, status: Status,
              remote: dict | None = None) -> list[dict]:
    """mac on this host and linux over ssh, at once when --leg both.  A leg that fails aborts the whole run: no half receipt.
    A kill or failure anywhere while the remote thread lives (the mac leg, or the wait for the remote leg) stops the
    remote run.  `remote` (the caller's) records started / done / stopped, so a kill that escapes this function can
    still stop a remote run nobody stopped."""
    remote = {} if remote is None else remote
    out: dict = {}
    t = None
    try:
        if a.leg in ("linux", "both"):
            def go() -> None:
                try:
                    out["linux"] = run_leg_remote(top, sha, run, run_id, ledger, status, host=a.host,
                                                  workers=_workers(a, "linux"))
                except BaseException as e:  # noqa: BLE001 - handed to the main thread
                    out["linux_err"] = e
            th = threading.Thread(target=go, daemon=True)
            remote["started"] = True
            th.start()
            t = th          # only a started thread is ever joined below
        if a.leg in ("mac", "both"):
            out["mac"] = run_leg_local(top, sha, run, run_id, ledger, status, leg="mac", python=a.python or MAC_PYTHON,
                                       workers=_workers(a, "mac"), timeout=a.pytest_timeout)
        if t:
            t.join()
            # the remote leg ended on its own: it cleaned up after success and stopped itself after a failure
            remote["done"] = True
    except BaseException:
        terminate_children()
        if t and not remote.get("done"):
            t.join(30)
            stop_remote_run(a.host, run_id)
            remote["stopped"] = True
        raise
    if "linux_err" in out:
        raise out["linux_err"]
    return [out[k] for k in ("mac", "linux") if k in out]


def stop_remote_run(host: str, run_id: str) -> None:
    """Best effort, from the main thread and outside the tracked children (a kill is already tearing those down)."""
    try:
        subprocess.run(remote_stop_cmd(host, run_id, remove_dir=True), capture_output=True, timeout=180,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, AssertionError) as e:
        print(f"verify: could not stop the remote run on {host}: {e}", file=sys.stderr)


def fetch_base(top: Path) -> str:
    """Bring the land base up to date before anything is classified, and return its sha.  With an `origin` remote the
    fetch must succeed (a stale base would excuse a red that main has since fixed or broken); a checkout without one
    uses the ref it has."""
    ref = f"refs/remotes/{MAIN_REF}"
    if git(top, "remote", "get-url", "origin", check=False).returncode == 0:
        r = git(top, "fetch", "-q", "origin", f"+main:{ref}", check=False)
        if r.returncode != 0:
            raise RuntimeError(f"could not fetch origin main, so the baseline would be stale: {r.stderr.strip()}")
    p = git(top, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if p.returncode != 0:
        raise RuntimeError(f"{MAIN_REF} is not present in {top}")
    return p.stdout.strip()


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
    if a.run_id and not (RUN_ID_RE.fullmatch(a.run_id) and a.run_id.split("-")[1] == sha[:8]):
        print(f"verify: --run-id {a.run_id!r} is not <UTC stamp>-<sha8>-<4 hex> for this sha", file=sys.stderr)
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
    remote: dict = {}
    ledger = None if leg_here else Ledger(Path(a.ledger) if a.ledger else state / "ledger.jsonl")
    status.update(run_id=run_id, sha=sha, leg=a.leg, state="running", pid=os.getpid(), started=now(),
                  workers=_workers(a, "linux" if a.leg == "linux" else "mac"), here=bool(leg_here), log=str(run / "runner.log"))
    print(f"verify: run {run_id} sha {sha[:12]} leg {a.leg}{' (here)' if leg_here else ''}, run dir {run}", flush=True)
    try:
        sweep_runs(state, top, tools, current=run_id)
        if leg_here:
            if a.base_sha:
                have = git(top, "rev-parse", "--verify", f"refs/remotes/{MAIN_REF}^{{commit}}", check=False).stdout.strip()
                if have != a.base_sha:
                    raise RuntimeError(f"the baseline here is {have or 'absent'}, the orchestrating run pinned {a.base_sha}")
            leg_name = "mac" if a.leg == "mac" else "linux"
            leg = run_leg_local(top, sha, run, run_id, None, status, leg=leg_name,
                                python=a.python or (MAC_PYTHON if leg_name == "mac" else LINUX_PYTHON),
                                workers=_workers(a, leg_name), timeout=a.pytest_timeout)
            out = Path(a.leg_json) if a.leg_json else run / f"leg-{leg_name}.json"
            out.write_text(json.dumps(leg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            status.update(state="leg-done", leg_json=str(out), max_load=leg["max_load"], load_reds=leg["load_reds"])
            print(f"verify: leg written to {out}")
            return 0
        status.update(base_sha=fetch_base(top))
        legs = _run_legs(a, top, sha, run, run_id, ledger, status, remote=remote)
        diag = [d for leg in legs for d in leg.pop("_diag", [])]
        receipt = assemble_receipt(top, sha, run_id, legs)
        if not a.no_diagnosis:
            dp = diagnose(run, top, sha, diag)
            if dp is not None:
                receipt["diagnosis"] = str(dp)
        path = write_receipt(receipt, Path(a.receipts_dir) if a.receipts_dir else top / "project_memory" / "receipts")
        status.update(state={"green": "passed", "partial": "partial"}.get(receipt["verdict"], "red"), verdict=receipt["verdict"],
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
        if remote.get("started") and not (remote.get("done") or remote.get("stopped")):
            stop_remote_run(a.host, run_id)
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


def named_run(state: Path, run_id: str) -> Path | None:
    """The run dir a command-line run id names, or None: the id must be the generated format and its dir must sit
    directly under <state>/runs once links are resolved."""
    if not RUN_ID_RE.fullmatch(str(run_id or "")):
        return None
    runs = Path(state) / "runs"
    p = runs / run_id
    try:
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real.parent != runs.resolve() or real.name != run_id or not real.is_dir():
        return None
    return p


def cmd_status(a) -> int:
    runs = Path(a.state_dir) / "runs"
    if a.run:
        run = named_run(Path(a.state_dir), a.run)
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
    print(f"verify: sweep cleaned {len(done)} run dir(s)")
    return 0


def cmd_ledger(a) -> int:
    led = Ledger(Path(a.ledger) if a.ledger else Path(a.state_dir) / "ledger.jsonl")
    try:
        led.attach_row(a.id, a.row)
    except (KeyError, ValueError) as e:
        print(f"verify: {e}", file=sys.stderr)
        return 2
    print(f"verify: ledger entry {a.id} now carries board row {a.row!r}")
    return 0


def cmd_reverdict(a) -> int:
    """Recompute a finished run's verdict after board rows were attached, and rewrite its receipt."""
    state = Path(a.state_dir)
    run = named_run(state, a.run)
    if run is None:
        print(f"verify: {a.run!r} names no run directly under {state / 'runs'}", file=sys.stderr)
        return 2
    st = read_status(run)
    if not st.get("receipt"):
        print(f"verify: run {a.run} has no receipt to re-judge", file=sys.stderr)
        return 2
    rp = Path(st["receipt"])
    led = Ledger(Path(a.ledger) if a.ledger else state / "ledger.jsonl")
    try:
        old = json.loads(rp.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or old.get("run_id") != a.run:
            raise ValueError(f"the receipt {rp} is not run {a.run}'s (its run_id is "
                             f"{old.get('run_id') if isinstance(old, dict) else None!r}); nothing was re-judged")
        new = reverdict_receipt(old, led)
    except (OSError, ValueError) as e:
        print(f"verify: {e}", file=sys.stderr)
        return 2
    write_receipt(new, rp.parent, path=rp)
    Status(run).update(state={"green": "passed", "partial": "partial"}.get(new["verdict"], "red"),
                                          verdict=new["verdict"])
    print(f"verify: {new['verdict'].upper()}  receipt {rp}")
    for w in new["verdict_why"]:
        print(f"verify: why {new['verdict']}: {w}")
    return 0 if new["verdict"] == "green" else 1


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
    rp.add_argument("--base-sha", default=None, help=argparse.SUPPRESS)
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
    lp = sub.add_parser("ledger", help="ledger operations")
    lsub = lp.add_subparsers(dest="ledger_cmd", required=True)
    ar = lsub.add_parser("attach-row", help="record the board row that owns an excused red; a re-run or reverdict then counts it")
    ar.add_argument("--id", required=True, help="the ledger id (lg_...) the receipt printed")
    ar.add_argument("--row", required=True, help="the board row")
    ar.add_argument("--state-dir", default=str(DEFAULT_STATE))
    ar.add_argument("--ledger", default=None)
    rv = sub.add_parser("reverdict", help="recompute a finished run's verdict from the ledger's current board rows")
    rv.add_argument("--run", required=True)
    rv.add_argument("--state-dir", default=str(DEFAULT_STATE))
    rv.add_argument("--ledger", default=None)
    a = ap.parse_args(argv)
    return {"run": cmd_run, "start": cmd_start, "status": cmd_status, "sweep": cmd_sweep, "ledger": cmd_ledger,
            "reverdict": cmd_reverdict}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
