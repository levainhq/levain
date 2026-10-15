"""levain.consent.canary — the consent hooks' version pin and their canary (Q5).

**Version pin.** Each agent adapter was measured against particular agent versions (:data:`TESTED_VERSIONS`, the
versions the Q3/Q5 runs used). An agent's hook contract can change in any release, so installing a consent hook for
an untested version is refused unless the operator passes :data:`ALLOW_FLAG`, and ``levain doctor`` WARNs about an
installed hook whose agent is no longer a tested version.

**Canary.** ``levain consent canary --agent claude_code|codex`` makes a scratch workspace, installs the agent's
consent hook entry there with the adapter's own :func:`install` (so the agent loads exactly the entry it loads in
production), and swaps the hook script for the canary's (:data:`CANARY_TEMPLATE`): it answers ONE sentinel
request (a write of a sentinel file inside the scratch workspace) with deny, never allow, and records every event it
sees. Then it runs one real agent turn that asks for that write. PASS only when the hook recorded the sentinel
request AND the agent reported the denial AND the sentinel file does not exist; anything not observed is a FAIL.

* Claude Code fires ``PermissionRequest`` headless (``claude -p``; measured, 2.1.300): the canary runs it headless
  with ``--setting-sources project,local`` (only the scratch workspace's hooks) and reads the denial from its
  stream-json tool result.
* Codex cannot fire it headless (``codex exec`` never asks), so the canary drives an interactive ``codex`` under a
  pty with ``--dangerously-bypass-hook-trust`` (its own freshly written hook, so no trust is written to CODEX_HOME)
  and the scratch project trusted for this invocation only (``-c``). The sentinel is an ``apply_patch`` into the
  scratch workspace's ``.codex/``, which Codex's sandbox always takes to approval (measured, Q3 M4). The denial is
  read from Codex's own screen ("Blocked by hook" and the canary's message).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENTS = ("claude_code", "codex")
AGENT_BINS = {"claude_code": "claude", "codex": "codex"}
AGENT_LABELS = {"claude_code": "Claude Code", "codex": "Codex"}
# The agent versions the consent adapters were run against (`claude --version`, `codex --version`).
TESTED_VERSIONS: dict[str, tuple[str, ...]] = {"claude_code": ("2.1.300",), "codex": ("0.162.1",)}
ALLOW_FLAG = "--allow-untested-agent-version"
SENTINEL_NAME = "levain_canary_sentinel.txt"
CANARY_MESSAGE = "levain consent canary: the sentinel write was denied by the hook"
RECORD_NAME = "canary_record.jsonl"
_VERSION = re.compile(r"(\d+\.\d+\.\d+)")


class UntestedAgentVersion(RuntimeError):
    """The agent's version is not one the consent hook was tested with, and no override was given."""


def agent_version(agent: str, *, binary: str | None = None, timeout: float = 30.0) -> str | None:
    """The installed agent's version (``<bin> --version``, first ``X.Y.Z``), or ``None`` when it cannot be read."""
    binary = binary or shutil.which(AGENT_BINS[agent])
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION.search(out.stdout or "")
    return m.group(1) if m else None


def untested_message(agent: str, version: str | None) -> str:
    seen = f"{AGENT_LABELS[agent]} {version}" if version else f"{AGENT_LABELS[agent]} (version not readable)"
    return (f"{seen} is not a version levain's consent hook was tested with "
            f"(tested: {', '.join(TESTED_VERSIONS[agent])}); pass {ALLOW_FLAG} to install it anyway")


def require_tested_version(agent: str, *, allow_untested: bool = False, version: str | None = None,
                           binary: str | None = None) -> str | None:
    """The agent's version when it is tested; raises :class:`UntestedAgentVersion` otherwise, unless
    ``allow_untested``. ``version`` skips reading it."""
    version = version if version is not None else agent_version(agent, binary=binary)
    if version in TESTED_VERSIONS[agent] or allow_untested:
        return version
    raise UntestedAgentVersion(untested_message(agent, version))


# --- doctor --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    name: str
    detail: str
    hint: str | None = None
    warn: bool = False


def doctor_findings(install: Path, *, codex_home: Path | None = None) -> list[Finding]:
    """What ``levain doctor`` says about consent hooks installed in ``install`` (none installed: nothing). Every
    finding is at most a WARN: the hooks fail toward the human's own dialog, never toward an allow."""
    from levain.consent.adapters import claude_code, codex
    out: list[Finding] = []
    for agent, script in (("claude_code", install / ".claude" / "hooks" / claude_code.HOOK_SCRIPT_NAME),
                          ("codex", install / ".codex" / "hooks" / codex.HOOK_SCRIPT_NAME)):
        if not script.is_file():
            continue
        version = agent_version(agent)
        name = f"consent hook ({AGENT_LABELS[agent]})"
        if version in TESTED_VERSIONS[agent]:
            out.append(Finding(name, f"{AGENT_LABELS[agent]} {version} is a tested version"))
        else:
            out.append(Finding(name, untested_message(agent, version).split("; pass")[0],
                               "run `levain consent canary --agent " + agent + "` to check this version", warn=True))
        if agent != "codex":
            continue
        trust = codex.trust_status(install, home=codex_home, timeout=20)
        tname = "consent hook trust (Codex)"
        hints = {
            "untrusted": "start `codex` in this directory and trust levain's hook at Codex's 'Hooks need review' "
                         "screen; until then Codex does not run it",
            "changed": "the hook's definition changed since it was trusted, so Codex skips it: review what changed "
                       "at Codex's 'Hooks need review' screen before trusting it again",
            "unlisted": "Codex does not load this directory's hooks: trust the project in Codex",
            "absent": "the hook script is here but .codex/hooks.json has no levain entry: reinstall it",
        }
        if trust.status == "trusted":
            out.append(Finding(tname, "Codex trusts levain's PermissionRequest hook"))
        else:
            out.append(Finding(tname, f"{trust.status}: {trust.detail}", hints.get(trust.status), warn=True))
        if trust.script_intact is False:
            out.append(Finding("consent hook script (Codex)",
                               "the hook script differs from what levain installs (Codex does not hash scripts)",
                               "reinstall the hook", warn=True))
    return out


# --- the canary hook -----------------------------------------------------------------------------------------

CANARY_TEMPLATE = '''#!{python}
# Installed by `levain consent canary` in its own scratch workspace: it denies ONE sentinel write, never allows,
# and records every event it sees. Any failure here is no decision.
import sys

try:
    from levain.consent.canary import canary_hook_main
except BaseException:
    sys.exit(0)
sys.exit(canary_hook_main({agent!r}, {sentinel!r}, {record!r}))
'''


def _is_sentinel(agent: str, event: Mapping[str, Any], sentinel: str) -> bool:
    tool, tool_input, cwd = event.get("tool_name"), event.get("tool_input"), event.get("cwd") or "/"
    if not isinstance(tool_input, Mapping):
        return False
    if agent == "claude_code":
        paths = [tool_input.get("file_path")] if tool in ("Write", "Edit", "MultiEdit") else []
    else:
        from levain.consent.adapters.codex import PATCH_TOOL, patch_targets
        paths = (patch_targets(tool_input.get("command")) or []) if tool == PATCH_TOOL else []
    want = os.path.realpath(sentinel)
    return any(isinstance(p, str) and os.path.realpath(os.path.join(cwd, p)) == want for p in paths)


def canary_hook_main(agent: str, sentinel: str, record: str) -> int:
    """The canary's hook: deny the sentinel write, record what fired, print nothing for anything else."""
    try:
        event = json.loads(sys.stdin.read())
        hit = isinstance(event, dict) and event.get("hook_event_name") == "PermissionRequest" \
            and _is_sentinel(agent, event, sentinel)
        with open(record, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "event": event.get("hook_event_name"),
                                 "tool_name": event.get("tool_name"), "sentinel": hit}) + "\n")
        if hit:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                     "decision": {"behavior": "deny", "message": CANARY_MESSAGE}}}))
    except BaseException:  # noqa: BLE001 - a canary failure is no decision; the record shows it never fired
        pass
    return 0


@dataclass(frozen=True)
class CanaryResult:
    agent: str
    version: str | None
    status: str  # PASS | FAIL
    detail: str
    workspace: str

    def ok(self) -> bool:
        return self.status == "PASS"


def _setup(agent: str, workspace: Path | None) -> tuple[Path, Path, Path]:
    from levain.consent.adapters import claude_code, codex
    ws = Path(workspace).resolve() if workspace else Path(tempfile.mkdtemp(prefix="levain-canary-")).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    qdir = ws / ".levain-canary-queue"
    qdir.mkdir(mode=0o700, exist_ok=True)
    if agent == "claude_code":
        script, _ = claude_code.install(ws, python=sys.executable, queue_dir=qdir, max_hold=0)
    else:  # the canary checks whatever version is installed, so it installs past the pin
        script, _ = codex.install(ws, python=sys.executable, queue_dir=qdir, max_hold=0,
                                  allow_untested_agent_version=True)
    sentinel = ws / (SENTINEL_NAME if agent == "claude_code" else f".codex/{SENTINEL_NAME}")
    record = ws / RECORD_NAME
    claude_code._atomic_write(script, CANARY_TEMPLATE.format(python=sys.executable, agent=agent,
                                                             sentinel=str(sentinel), record=str(record)), mode=0o755)
    return ws, sentinel, record


def _records(record: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _verdict(agent: str, version: str | None, ws: Path, sentinel: Path, record: Path, reported: bool,
             extra: str = "") -> CanaryResult:
    fired = any(r.get("sentinel") for r in _records(record))
    exists = sentinel.exists()
    ok = fired and reported and not exists
    detail = (f"hook fired on the sentinel: {'yes' if fired else 'NO'}; agent reported the denial: "
              f"{'yes' if reported else 'NO'}; sentinel written: {'YES' if exists else 'no'}")
    return CanaryResult(agent, version, "PASS" if ok else "FAIL", detail + (f"; {extra}" if extra else ""), str(ws))


def _agent_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def run_claude_canary(*, workspace: Path | None = None, timeout: float = 240.0,
                      model: str = "haiku") -> CanaryResult:
    version = agent_version("claude_code")
    binary = shutil.which("claude")
    if not binary:
        return CanaryResult("claude_code", None, "FAIL", "claude is not on PATH", str(workspace or ""))
    ws, sentinel, record = _setup("claude_code", workspace)
    prompt = (f"Use the Write tool to create the file {sentinel} containing the word canary. "
              "Do nothing else, and do not retry if it is denied.")
    try:
        proc = subprocess.run([binary, "-p", prompt, "--model", model, "--setting-sources", "project,local",
                               "--permission-mode", "default", "--output-format", "stream-json", "--verbose"],
                              cwd=ws, env=_agent_env(), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        stream = proc.stdout
    except subprocess.TimeoutExpired as exc:
        stream = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    (ws / "canary_agent_output.jsonl").write_text(stream, encoding="utf-8")
    reported = False
    for line in stream.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        message = msg.get("message") if isinstance(msg, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("is_error") \
                    and CANARY_MESSAGE in json.dumps(part.get("content")):
                reported = True
    return _verdict("claude_code", version, ws, sentinel, record, reported)


_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>]")


def run_codex_canary(*, workspace: Path | None = None, timeout: float = 240.0) -> CanaryResult:
    """Drive one interactive ``codex`` turn under a pty (Codex cannot fire PermissionRequest headless). Never
    answers an update prompt, a folder-trust prompt or a hooks-review screen: any of them is a FAIL that says so."""
    import pty
    import select
    import signal
    version = agent_version("codex")
    binary = shutil.which("codex")
    if not binary:
        return CanaryResult("codex", None, "FAIL", "codex is not on PATH", str(workspace or ""))
    ws, sentinel, record = _setup("codex", workspace)
    prompt = (f"Use your apply_patch tool (not a shell command) to create the file {sentinel} containing the "
              "single line: canary . If it needs approval, request it. If it is denied, do not retry; stop.")
    argv = [binary, "--dangerously-bypass-hook-trust", "-c", "check_for_update_on_startup=false",
            "-c", f'projects={{"{ws}"={{trust_level="trusted"}}}}', prompt]
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        try:
            os.chdir(ws)
            import fcntl
            import struct
            import termios
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
            os.execve(binary, argv, {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")})
        finally:
            os._exit(127)
    buf = b""
    stop = ""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
            text = _ANSI.sub(b"", buf).decode("utf-8", "replace").replace(" ", "").lower()
            if "updatenow" in text:
                stop = "Codex showed an update prompt; the canary never answers it"
                break
            if "neworchanged" in text:
                stop = "Codex asked to review hooks despite --dangerously-bypass-hook-trust"
                break
            if "doyoutrust" in text:
                stop = "Codex asked to trust the scratch folder"
                break
            if "blockedbyhook" in text and CANARY_MESSAGE.replace(" ", "").lower() in text:
                time.sleep(1.0)
                break
            if "wouldyouliketo" in text:
                stop = "Codex showed its approval dialog: the hook's deny did not reach it"
                break
    finally:
        # Keep reading while the child exits, and close the pty before reaping it: on macOS a process exiting on a
        # pty waits for its output to drain, so a reap with nobody reading never returns.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    if not os.read(fd, 65536):
                        break
                except OSError:
                    break
        os.close(fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    screen = _ANSI.sub(b"", buf).decode("utf-8", "replace")
    (ws / "canary_agent_screen.txt").write_text(screen, encoding="utf-8")
    flat = screen.replace(" ", "").lower()
    reported = "blockedbyhook" in flat and CANARY_MESSAGE.replace(" ", "").lower() in flat
    return _verdict("codex", version, ws, sentinel, record, reported, stop)


def run_canary(agent: str, *, workspace: Path | None = None) -> CanaryResult:
    if agent == "claude_code":
        return run_claude_canary(workspace=workspace)
    if agent == "codex":
        return run_codex_canary(workspace=workspace)
    raise ValueError(f"agent must be one of {AGENTS}: {agent!r}")


def cli_canary(agent: str, workspace: Path | None = None) -> int:
    """``levain consent canary``: run it, print the verdict; exit 0 on PASS, 1 on FAIL."""
    version = agent_version(agent)
    tested = "tested" if version in TESTED_VERSIONS[agent] else "NOT a tested version"
    print(f"levain consent canary: {AGENT_LABELS[agent]} {version or '(version not readable)'} ({tested})")
    result = run_canary(agent, workspace=workspace)
    print(f"  {result.status}: {result.detail}")
    print(f"  workspace: {result.workspace}")
    return 0 if result.ok() else 1
