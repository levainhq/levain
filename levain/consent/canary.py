"""levain.consent.canary — the consent hooks' version pin and their canary (Q5).

**Version pin.** Each agent adapter was measured against particular agent versions (:data:`TESTED_VERSIONS`, the
versions the Q3/Q5 runs used). An agent's hook contract can change in any release, so installing a consent hook for
an untested version is refused unless the caller passes :data:`ALLOW_PARAMETER` to the adapter's ``install``, and
``levain doctor`` WARNs about an installed hook whose agent is no longer a tested version.

**Canary.** ``levain consent canary --agent claude_code|codex`` makes a scratch workspace (``--workspace`` must be a
new or empty directory, never a real project), installs the agent's consent hook entry there with the adapter's own
:func:`install` (so the agent loads exactly the entry it loads in production), and swaps the hook script for the
canary's (:func:`_canary_template`): it answers ONE sentinel
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

import contextlib
import contextvars
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.launch import child_env

AGENTS = ("claude_code", "codex")
AGENT_BINS = {"claude_code": "claude", "codex": "codex"}
AGENT_LABELS = {"claude_code": "Claude Code", "codex": "Codex"}
# The one credential variable each agent may need beyond levain's allowlist (an operator on an API key).
AGENT_KEY_VARS = {"claude_code": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
# The agent versions the consent adapters were run against (`claude --version`, `codex --version`).
TESTED_VERSIONS: dict[str, tuple[str, ...]] = {"claude_code": ("2.1.300", "2.1.301", "2.1.306", "2.1.308", "2.1.310"), "codex": ("0.162.1",)}
# What a caller passes to an adapter's install() to install for an untested version (no levain command installs a
# consent hook, so there is no CLI flag for it).
ALLOW_PARAMETER = "allow_untested_agent_version=True"
SENTINEL_NAME = "levain_canary_sentinel.txt"
CANARY_MESSAGE = "levain consent canary: the sentinel write was denied by the hook"
RECORD_NAME = "canary_record.jsonl"
# The whole of `<bin> --version`'s output, per agent (measured: "2.1.300 (Claude Code)", "codex-cli 0.162.1"). The
# version is the complete token, compared exactly: "0.162.1-alpha.9" is not 0.162.1.
_VERSION_OUTPUT = {"claude_code": re.compile(r"(\S+) \(Claude Code\)"), "codex": re.compile(r"codex-cli (\S+)")}


class UntestedAgentVersion(RuntimeError):
    """The agent's version is not one the consent hook was tested with, and no override was given."""


# Inside versions_read_once(): each (agent, binary) read's answer, reused; None outside it. A ContextVar, so
# overlapping runs in two threads (or tasks) each keep their own memo and reset by token.
_VERSIONS_READ: contextvars.ContextVar[dict[tuple[str, str | None], str] | None] = contextvars.ContextVar(
    "levain_versions_read", default=None)


@contextlib.contextmanager
def versions_read_once() -> Iterator[None]:
    """Within it each agent's version is read at most once (a successful answer is reused; a failed read is
    not memoized and re-reads): for a caller with several rows about one agent version, so one run spawns
    ``<bin> --version`` once."""
    token = _VERSIONS_READ.set({} if _VERSIONS_READ.get() is None else _VERSIONS_READ.get())
    try:
        yield
    finally:
        _VERSIONS_READ.reset(token)


def agent_version(agent: str, *, binary: str | None = None, timeout: float = 30.0) -> str | None:
    """The installed agent's version: ``<bin> --version`` exited 0 and its whole output is the agent's version line
    (:data:`_VERSION_OUTPUT`). ``None`` when it cannot be read, or reads any other way. Inside
    :func:`versions_read_once`, a successful read is reused per agent and binary."""
    memo = _VERSIONS_READ.get()
    if memo is None:
        return _read_agent_version(agent, binary, timeout)
    if (agent, binary) in memo:
        return memo[(agent, binary)]
    got = _read_agent_version(agent, binary, timeout)
    if got is not None:  # a failure re-reads: the key omits timeout, so a short-timeout failure must not stick
        memo[(agent, binary)] = got
    return got


def _read_agent_version(agent: str, binary: str | None, timeout: float) -> str | None:
    binary = binary or shutil.which(AGENT_BINS[agent])
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, env=child_env())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    m = _VERSION_OUTPUT[agent].fullmatch((out.stdout or "").strip()) if out.returncode == 0 else None
    return m.group(1) if m else None


def untested_message(agent: str, version: str | None) -> str:
    seen = f"{AGENT_LABELS[agent]} {version}" if version else f"{AGENT_LABELS[agent]} (version not readable)"
    return (f"{seen} is not a version levain's consent hook was tested with "
            f"(tested: {', '.join(TESTED_VERSIONS[agent])}); pass {ALLOW_PARAMETER} to install() to install it "
            "anyway")


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


# The most ``levain doctor`` waits on the agents altogether (measured: well under a second when they answer).
DOCTOR_BUDGET = 5.0


def _config_references(config: Path, script: Path) -> bool:
    """True when the agent config ``config`` has a PermissionRequest handler that runs ``script``."""
    from levain.consent.adapters.claude_code import _is_ours
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get("PermissionRequest") if isinstance(hooks, dict) else None
    script = Path(os.path.realpath(script))
    return any(_is_ours(h, "", script)
               for g in (groups if isinstance(groups, list) else []) if isinstance(g, dict)
               for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else []))


def _agent_findings(agent: str, install: Path, script: Path, config: Path, codex_home: Path | None,
                    deadline: float, *, wired_by_init: bool = False) -> list[Finding]:
    """``wired_by_init``: ``script`` is the static one `levain init` puts in the activation tree, which is there
    whether or not ``config`` (for Codex the machine-global hooks.json) still runs it; doctor's wiring row reports
    that, so this says nothing unless it does."""
    from levain.consent.adapters import codex
    name = f"consent hook ({AGENT_LABELS[agent]})"
    if wired_by_init and not _config_references(config, script):
        return []
    if not script.is_file():
        if _config_references(config, script):
            return [Finding(name, f"{config} runs {script}, which does not exist (no consent hook runs)",
                            "reinstall the hook, or remove levain's entry from " + str(config), warn=True)]
        return []
    out: list[Finding] = []
    version = agent_version(agent, timeout=max(0.5, deadline - time.monotonic()))
    if version in TESTED_VERSIONS[agent]:
        out.append(Finding(name, f"{AGENT_LABELS[agent]} {version} is a tested version"))
    else:
        out.append(Finding(name, untested_message(agent, version).split("; pass")[0],
                           "run `levain consent canary --agent " + agent + "` to check this version", warn=True))
    if agent != "codex":
        return out
    if wired_by_init:
        trust = codex.trust_status(install, home=codex_home, timeout=max(0.5, deadline - time.monotonic()),
                                   hooks_path=config, script=script)
    else:
        trust = codex.trust_status(install, home=codex_home, timeout=max(0.5, deadline - time.monotonic()))
    tname = "consent hook trust (Codex)"
    hints = {
        "untrusted": "start `codex` in this directory and trust levain's hook at Codex's 'Hooks need review' "
                     "screen; until then Codex does not run it",
        "changed": "the hook's definition changed since it was trusted, so Codex skips it: review what changed "
                   "at Codex's 'Hooks need review' screen before trusting it again",
        "disabled": "Codex has the hook turned off, so it does not run it: enable it in Codex's hooks settings",
        "unlisted": "Codex does not load this directory's hooks: trust the project in Codex",
        "absent": "the hook script is here but .codex/hooks.json has no levain entry: reinstall it",
    }
    if wired_by_init:
        # levain writes the machine-wide hooks' trust itself, so its own command restores it
        hints["untrusted"] = hints["changed"] = ("run `levain update`, which writes Codex's trust for levain's hooks "
                                                 "and confirms it with Codex")
        # A user-level hook: what Codex lists for it when it is not loaded is not measured, so no cause is named.
        hints["unlisted"] = (f"Codex does not list levain's hook from {config}: start `codex` and look at its "
                             "hooks review screen")
    if trust.status == "trusted":
        out.append(Finding(tname, "Codex trusts levain's PermissionRequest hook"))
    else:
        out.append(Finding(tname, f"{trust.status}: {trust.detail}", hints.get(trust.status), warn=True))
    if trust.script_intact is False:
        out.append(Finding("consent hook script (Codex)",
                           "the hook script differs from what levain installs, or names another queue "
                           "(Codex does not hash scripts)", "reinstall the hook", warn=True))
    return out


def doctor_findings(install: Path, *, codex_home: Path | None = None) -> list[Finding]:
    """What ``levain doctor`` says about consent hooks installed in ``install`` (none installed and none
    referenced: nothing). Every finding is at most a WARN: the hooks fail toward the human's own dialog, never
    toward an allow. Waits on the agents at most about :data:`DOCTOR_BUDGET` seconds; an error is a WARN."""
    from levain.consent.adapters import claude_code, codex
    from levain.host_guard import cc_levain_hooks_file
    from levain.install import CONSENT_HOOK_SCRIPTS
    deadline = time.monotonic() + DOCTOR_BUDGET
    out: list[Finding] = []
    hooks_dir = install / "activation" / "hooks"
    chome = codex_home if codex_home is not None else codex.codex_home()
    for agent, script, config, by_init in (
            ("claude_code", install / ".claude" / "hooks" / claude_code.HOOK_SCRIPT_NAME,
             install / ".claude" / "settings.json", False),
            ("codex", install / ".codex" / "hooks" / codex.HOOK_SCRIPT_NAME, install / ".codex" / "hooks.json",
             False),
            # the shape `levain init` wires: a static script in the activation tree
            ("claude_code", hooks_dir / CONSENT_HOOK_SCRIPTS["claude-code"], cc_levain_hooks_file(install), True),
            ("codex", hooks_dir / CONSENT_HOOK_SCRIPTS["codex"], chome / "hooks.json", True)):
        try:
            out += _agent_findings(agent, install, script, config, codex_home, deadline, wired_by_init=by_init)
        except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
            out.append(Finding(f"consent hook ({AGENT_LABELS[agent]})", f"could not be checked: {exc}"[:300],
                               warn=True))
    return out


# --- the canary hook -----------------------------------------------------------------------------------------

def _canary_template() -> str:
    from levain.consent.adapters.claude_code import ISOLATE_PRELUDE
    return ('''#!{python}
# Installed by `levain consent canary` in its own scratch workspace: it denies ONE sentinel write, never allows,
# and records every event it sees. Any failure here is no decision.
''' + ISOLATE_PRELUDE.replace("{", "{{").replace("}", "}}") + '''
try:
    from levain.consent.canary import canary_hook_main
except BaseException:
    sys.exit(0)
sys.exit(canary_hook_main({agent!r}, {sentinel!r}, {record!r}))
''')


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
    """A scratch workspace with the canary's hook installed. ``workspace`` must be a new or empty directory: the
    canary installs a deny-only hook there and leaves its records, so it never runs in a real project."""
    from levain.consent.adapters import claude_code, codex
    if workspace:
        ws = Path(workspace).absolute()
        if os.path.lexists(ws) and (os.path.islink(ws) or not ws.is_dir() or any(os.scandir(ws))):
            raise ValueError(f"refusing {ws}: the canary needs a new or empty directory")
        ws.mkdir(parents=True, exist_ok=True)
        ws = ws.resolve()
    else:
        ws = Path(tempfile.mkdtemp(prefix="levain-canary-")).resolve()
    qdir = ws / ".levain-canary-queue"
    qdir.mkdir(mode=0o700, exist_ok=True)
    # the canary checks whatever version is installed, so it installs past the pin
    if agent == "claude_code":
        script, _ = claude_code.install(ws, python=sys.executable, queue_dir=qdir, max_hold=0,
                                        allow_untested_agent_version=True)
    else:
        script, _ = codex.install(ws, python=sys.executable, queue_dir=qdir, max_hold=0,
                                  allow_untested_agent_version=True)
    sentinel = ws / (SENTINEL_NAME if agent == "claude_code" else f".codex/{SENTINEL_NAME}")
    record = ws / RECORD_NAME
    claude_code._atomic_write(script, _canary_template().format(python=sys.executable, agent=agent,
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



def run_claude_canary(*, workspace: Path | None = None, timeout: float = 240.0,
                      model: str = "haiku") -> CanaryResult:
    version = agent_version("claude_code")
    binary = shutil.which("claude")
    if not binary:
        return CanaryResult("claude_code", None, "FAIL", "claude is not on PATH", str(workspace or ""))
    try:
        ws, sentinel, record = _setup("claude_code", workspace)
    except Exception as exc:  # noqa: BLE001 - a canary that cannot set up is a FAIL that says why
        return CanaryResult("claude_code", version, "FAIL", f"setup failed: {exc}"[:500], str(workspace or ""))
    prompt = (f"Use the Write tool to create the file {sentinel} containing the word canary. "
              "Do nothing else, and do not retry if it is denied.")
    try:
        proc = subprocess.run([binary, "-p", prompt, "--model", model, "--setting-sources", "project,local",
                               "--permission-mode", "default", "--output-format", "stream-json", "--verbose"],
                              cwd=ws, env=child_env(AGENT_KEY_VARS["claude_code"]), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        stream = proc.stdout
    except subprocess.TimeoutExpired as exc:
        stream = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    except OSError as exc:
        return _verdict("claude_code", version, ws, sentinel, record, False, f"claude could not be started: {exc}")
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


def _toml_str(value: str) -> str:
    """``value`` as a TOML basic string."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the agent's whole process group (it was started in a session of its own)."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>]")


def run_codex_canary(*, workspace: Path | None = None, timeout: float = 240.0) -> CanaryResult:
    """Drive one interactive ``codex`` turn under a pty (Codex cannot fire PermissionRequest headless). Never
    answers an update prompt, a folder-trust prompt or a hooks-review screen: any of them is a FAIL that says so."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    version = agent_version("codex")
    binary = shutil.which("codex")
    if not binary:
        return CanaryResult("codex", None, "FAIL", "codex is not on PATH", str(workspace or ""))
    try:
        ws, sentinel, record = _setup("codex", workspace)
    except Exception as exc:  # noqa: BLE001 - a canary that cannot set up is a FAIL that says why
        return CanaryResult("codex", version, "FAIL", f"setup failed: {exc}"[:500], str(workspace or ""))
    prompt = (f"Use your apply_patch tool (not a shell command) to create the file {sentinel} containing the "
              "single line: canary . If it needs approval, request it. If it is denied, do not retry; stop.")
    argv = [binary, "--dangerously-bypass-hook-trust", "-c", "check_for_update_on_startup=false",
            "-c", f"projects={{{_toml_str(str(ws))}={{trust_level=\"trusted\"}}}}", prompt]
    try:
        fd, slave = pty.openpty()
    except OSError as exc:
        return _verdict("codex", version, ws, sentinel, record, False, f"no pty: {exc}")
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))
        # A new session whose controlling terminal is the pty: Codex's TUI opens /dev/tty.
        proc = subprocess.Popen(argv, cwd=ws, env=child_env(AGENT_KEY_VARS["codex"]), stdin=slave, stdout=slave,
                                stderr=slave, start_new_session=True,
                                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    except (OSError, subprocess.SubprocessError) as exc:
        os.close(fd)
        return _verdict("codex", version, ws, sentinel, record, False, f"codex could not be started: {exc}")
    finally:
        os.close(slave)
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
        # Keep reading while the agent's process group exits, and close the pty before reaping: on macOS a process
        # exiting on a pty waits for its output to drain, so a reap with nobody reading never returns.
        _signal_group(proc, signal.SIGTERM)
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
        os.close(fd)
        _signal_group(proc, signal.SIGKILL)
        proc.wait()
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
