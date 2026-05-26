"""Shared helpers for the Levain activation hooks.

Levain's Claude Code adapter wires two hooks:
  - session_start.py      -> SessionStart    (Layer A: primacy posture; Layer D: start-catch)
  - user_prompt_submit.py -> UserPromptSubmit (Layer B: recency directive; Layer D: nudge)

Both share install detection, scoping, activation-file parsing, the
unwrapped-episode query, temporal formatting, and the harness output format.
That common surface lives here.

Harness-portability seam: emit() is the primary harness-coupled function — it
formats output as Claude Code hook JSON. Two smaller Claude-Code couplings live
in the hook scripts rather than here: the CLAUDE_PROJECT_DIR env-var name read
in install_root(), and the SessionStart `source` vocabulary parsed in
session_start.py. A non-Claude-Code adapter swaps emit() and re-verifies those.

FAIL-OPEN — structural rule: the GUARANTEE that a hook never crashes or writes
stderr noise into the operator's session is enforced at the harness ENTRY
POINTS — session_start.py and user_prompt_submit.py each wrap their entire
main() body in a catch-all and exit 0, and guard the import of this module.
The helpers below also aim to fail open, and most are individually guarded —
but the entry-point guard is the load-bearing structural invariant; a helper
that raises is caught there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def install_root() -> Path:
    """The Levain install directory (the operator's partnership working dir).

    The source of truth is THIS hook file's own location — activation/hooks/ is
    two parents below the install root. $CLAUDE_PROJECT_DIR is consulted only as
    confirmation: it names the *current Claude Code project*, which equals the
    install only when the hook file actually lives inside it. With a
    project-scoped settings.json it always does; with global wiring it may not —
    so trusting it unconditionally would resolve a globally-wired hook to an
    unrelated project. It is used only when verified to contain this file.

    Fully guarded: an unexpectedly shallow install path or a bad env value
    degrades rather than raising."""
    try:
        file_root: Path | None = Path(__file__).resolve().parents[2]
    except (IndexError, OSError, RuntimeError):
        file_root = None

    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        try:
            candidate = Path(env_dir).resolve()
            Path(__file__).resolve().relative_to(candidate)
            return candidate
        except (OSError, RuntimeError, ValueError):
            pass

    if file_root is not None:
        return file_root
    try:
        return Path.cwd()
    except (OSError, RuntimeError):
        return Path(".")


def store_path() -> Path:
    """The anneal-memory store for THIS install — install-relative, so every
    Levain install has its own memory and its own continuity. Never the
    machine-global anneal-memory default: a shared default would silently merge
    the memories — and therefore the identities — of two installs."""
    return install_root() / ".levain" / "memory.db"


def in_install_session() -> bool:
    """True when the session's cwd is within the Levain install.

    With a project-scoped .claude/settings.json this is structurally almost
    always true — Claude Code only loads project hooks for that project. It is
    kept as a cheap defensive assertion, and it correctly scopes the hooks for
    an operator who wires them globally (~/.claude/settings.json) instead."""
    try:
        cwd = Path.cwd().resolve()
        cwd.relative_to(install_root())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def suppressed() -> bool:
    """A subprocess spawner that itself launches the harness from inside the
    install — a consultation tool, a batch runner — sets LEVAIN_HOOK_SUPPRESS=1
    to keep the activation directives out of contexts that must stay
    independent of the partnership's cognitive posture."""
    return os.environ.get("LEVAIN_HOOK_SUPPRESS", "").strip() == "1"


def should_fire() -> bool:
    """The gate every hook checks before emitting anything."""
    return not suppressed() and in_install_session()


def read_blocks(path: Path) -> list[str]:
    """Parse a Levain activation file into a list of block bodies.

    File format: a markdown preamble, then one or more `## ` blocks. The
    preamble (everything before the first `## `) and each `## ` title line are
    dropped; each block's body is returned, stripped. Blank blocks are skipped.
    Returns [] on ANY read/parse failure — the hook then stays silent."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                blocks.append("\n".join(current).strip())
            current = []
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


# Install-time-resolved anneal-memory binary path. `levain init` substitutes
# this placeholder after copying the hook scripts into the operator's install
# dir, so the hooks don't depend on PATH at fire time (Claude Code + Codex
# both sanitize hook env aggressively). The `"{{" in ...` guard skips this
# entry when the placeholder wasn't substituted (editable-install workshop
# state, or a future regression).
_INSTALL_ANNEAL_BIN = "{{ANNEAL_MEMORY}}"


def episodes_since_wrap(timeout: float = 5.0) -> int | None:
    """Episodes recorded since the last wrap, via anneal-memory's public CLI.

    This is the signal Layer D runs on. The store is pinned explicitly with
    `--db` to this install's store (see store_path) — never left to
    anneal-memory's machine-global default. Returns None when anneal-memory is
    not installed, the store does not exist yet, or anything errors — Layer D
    then stays silent. Tries the install-resolved binary first, then the
    PATH-lookup console script, then the module form."""
    db = str(store_path())
    candidates = []
    if "{{" not in _INSTALL_ANNEAL_BIN:
        candidates.append([_INSTALL_ANNEAL_BIN, "--db", db, "status", "--json"])
    candidates.extend([
        ["anneal-memory", "--db", db, "status", "--json"],
        [sys.executable, "-m", "anneal_memory", "--db", db, "status", "--json"],
    ])
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                errors="replace", timeout=timeout,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            data = json.loads(result.stdout)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        value = data.get("episodes_since_wrap")
        if isinstance(value, int):
            return value
    return None


def temporal() -> str:
    """Operator-local date and time. Models have no clock; this is cheap and
    load-bearing context."""
    return datetime.now().astimezone().strftime("%I:%M %p %Z — %A %B %d, %Y")


def read_stdin() -> dict:
    """Consume and parse the hook's stdin JSON payload.

    Returns {} on any failure. Reading stdin also prevents broken-pipe noise on
    some platforms even when the payload is not needed."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def emit(additional_context: str, event_name: str) -> None:
    """Write hook output in Claude Code's hook-JSON format. additionalContext
    is injected into the model's context at the hook's position; event_name is
    the hook event ("SessionStart" / "UserPromptSubmit"), emitted as
    hookEventName per the documented schema (omitting it relies on undocumented
    permissiveness — not safe for a distributable artifact).

    THIS IS THE PRIMARY HARNESS-COUPLED SEAM — a non-Claude-Code adapter swaps
    this function. The stdout writes are guarded: if the harness has already
    closed the pipe (a killed-session race), emit() degrades to silence rather
    than raising."""
    try:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            }
        }
        sys.stdout.write(json.dumps(payload))
        sys.stdout.write("\n")
    except (OSError, ValueError):
        pass
