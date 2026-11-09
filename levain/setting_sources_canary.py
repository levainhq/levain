"""The ``--setting-sources ""`` tripwire's probe. The empty source list is undocumented in Claude Code's own help, so
what it does is a measured fact about one version. This probe re-measures it with no model call and no network: a
scratch project plants a SessionStart hook that touches a marker, ``claude -p`` runs once WITHOUT the flag (the control:
the hook must fire) and once WITH ``--setting-sources ""`` (the hook must not), each pointed at an unreachable API
address with a fake key, in a scratch HOME and config dir, never the operator's. The hook fires before the first API
call, so the marker is the whole evidence; both processes are killed once it is judged."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from levain.host_guard import SEAT_SOURCE_ARGS

__all__ = ["ProbeResult", "run_probe"]


@dataclass(frozen=True)
class ProbeResult:
    verdict: str  # "held" | "leaked" | "inconclusive"
    detail: str


def _plant(root: Path, name: str) -> tuple[Path, Path]:
    proj = root / name
    (proj / ".claude").mkdir(parents=True)
    marker = root / f"{name}.marker"
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"touch {shlex.quote(str(marker))}"}]}]}}
    (proj / ".claude" / "settings.json").write_text(json.dumps(hook))
    return proj, marker


def _spawn(binary: str, proj: Path, home: Path, extra: tuple[str, ...]) -> subprocess.Popen:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "cfg"),
           "ANTHROPIC_API_KEY": "sk-ant-levain-probe-not-a-key", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
           "DISABLE_AUTOUPDATER": "1"}
    return subprocess.Popen([binary, "-p", "levain probe", *extra], cwd=proj, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _kill(p: subprocess.Popen) -> None:
    try:
        p.kill()
        p.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def run_probe(binary: str, *, ctl_timeout: float = 30.0, min_grace: float = 3.0) -> ProbeResult:
    """``held``: the control's hook fired and the empty-list run's did not within a grace of twice the control's
    time. ``leaked``: the empty-list run fired it. ``inconclusive``: the control never fired (no evidence either way)."""
    root = Path(tempfile.mkdtemp(prefix="levain-ssprobe-"))
    procs: list[subprocess.Popen] = []
    try:
        home = root / "home"
        (home / "cfg").mkdir(parents=True)
        cproj, cmark = _plant(root, "ctl")
        eproj, emark = _plant(root, "empty")
        t0 = time.monotonic()
        procs.append(_spawn(binary, cproj, home, ()))
        procs.append(_spawn(binary, eproj, home, SEAT_SOURCE_ARGS))
        while not cmark.exists():
            if time.monotonic() - t0 > ctl_timeout:
                return ProbeResult("inconclusive", "the control run never fired its project hook, so the probe proves nothing")
            time.sleep(0.05)
        elapsed = time.monotonic() - t0
        deadline = time.monotonic() + max(min_grace, 2 * elapsed)
        while time.monotonic() < deadline:
            if emark.exists():
                break
            time.sleep(0.05)
        if emark.exists():
            return ProbeResult("leaked", '--setting-sources "" still loaded the project settings (its hook fired)')
        return ProbeResult("held", f"the control's project hook fired in {elapsed:.1f}s; the empty-list run's did not")
    except OSError as exc:
        return ProbeResult("inconclusive", f"the probe could not run: {exc}"[:200])
    finally:
        for p in procs:
            _kill(p)
        shutil.rmtree(root, ignore_errors=True)
