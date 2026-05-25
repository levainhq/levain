"""Shared helpers for the Levain activation hooks (Codex adapter).

Levain's Codex adapter wires two hooks:
  - session_start.py      -> SessionStart    (Layer A: primacy posture; Layer D: start-catch)
  - user_prompt_submit.py -> UserPromptSubmit (Layer B: recency directive; Layer D: ambient nudge)

Both hooks share install detection, scoping, activation-file parsing, the
unwrapped-episode query, temporal formatting, and the harness output format.
That common surface lives here.

The Stop event is NOT used on Codex: per Codex 0.133 schema, `stop.command.output`
accepts only `continue` / `decision` / `reason` / `stopReason` / `suppressOutput`
/ `systemMessage` — it does NOT accept `hookSpecificOutput.additionalContext`,
the shape this helper's emit() produces. The Claude Code adapter folds Layer D
(wrap discipline) into SessionStart (start-catch) + UserPromptSubmit (ambient
nudge); the Codex adapter does the same for the same reason — wrap discipline
stays model-driven via the seed's memory.md instructions, structurally
unenforced by the harness on session end.

Harness-portability seam: emit() is the primary harness-coupled function — it
formats output as Codex hook JSON (`hookSpecificOutput` + `hookEventName` +
`additionalContext`). Codex and Claude Code use the same shape for SessionStart
and UserPromptSubmit. The Codex-specific divergences are:
  - install_root() falls back to __file__ parents resolution only (no
    CODEX_PROJECT_DIR equivalent; Codex hook env is heavily sanitized)
  - SessionStart `source` vocabulary is `startup|resume|clear|compact` per
    Codex 0.133 schema (matches Claude Code; empirical 0.132 saw startup|resume
    only but the matcher now covers all four for forward compatibility)
  - Stop event excluded entirely per Codex Stop output schema constraint above

FAIL-OPEN — structural rule: the GUARANTEE that a hook never crashes or writes
stderr noise into the operator's session is enforced at the harness ENTRY
POINTS — session_start.py / user_prompt_submit.py each wrap their entire
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
    two parents below the install root. Codex does not (at v1) expose a project
    directory env var equivalent to Claude Code's CLAUDE_PROJECT_DIR, so the
    file-location fallback is the only mechanism. The hooks.json sidecar lives
    at ~/.codex/hooks.json (global) and references {{INSTALL_DIR}} as an
    absolute path resolved at install time, so the hook file's absolute path
    points to the right install.

    Fully guarded: an unexpectedly shallow install path degrades rather than
    raising."""
    try:
        return Path(__file__).resolve().parents[2]
    except (IndexError, OSError, RuntimeError):
        pass
    try:
        return Path.cwd()
    except (OSError, RuntimeError):
        return Path(".")


def store_path() -> Path:
    """The anneal-memory store for THIS install — install-relative, so every
    Levain install has its own memory and its own continuity. Never the
    machine-global anneal-memory default: a shared default would silently
    merge the memories — and therefore the identities — of two installs."""
    return install_root() / ".levain" / "memory.db"


def in_install_session() -> bool:
    """True when the session's cwd is within the Levain install.

    With hooks.json wired globally at ~/.codex/hooks.json, the hooks fire for
    every Codex session on the machine — not only sessions inside the install.
    This gate scopes them: outside the install, the hooks no-op silently.
    Without this gate, an unrelated Codex session (working on a different
    codebase) would receive Levain's posture and recency directives,
    contaminating the wrong workspace."""
    try:
        cwd = Path.cwd().resolve()
        cwd.relative_to(install_root())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def suppressed() -> bool:
    """A subprocess spawner that itself launches Codex from inside the
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


def episodes_since_wrap(timeout: float = 5.0) -> int | None:
    """Episodes recorded since the last wrap, via anneal-memory's public CLI.

    This is the signal Layer D runs on. The store is pinned explicitly with
    `--db` to this install's store (see store_path) — never left to
    anneal-memory's machine-global default. Returns None when anneal-memory
    is not installed, the store does not exist yet, or anything errors —
    Layer D then stays silent. Tries the console script first, then the
    module form."""
    db = str(store_path())
    for cmd in (
        ["anneal-memory", "--db", db, "status", "--json"],
        [sys.executable, "-m", "anneal_memory", "--db", db, "status", "--json"],
    ):
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
    """Write hook output in Codex's hook-JSON format. `additionalContext`
    is injected into the model's context at the hook's position; `event_name`
    is the hook event ("SessionStart" / "UserPromptSubmit"), emitted as
    `hookEventName` per the documented schema (omitting it relies on
    undocumented permissiveness — not safe for a distributable artifact).
    Stop event is excluded: its output schema does not accept
    `hookSpecificOutput` (see module docstring).

    Codex uses the same hook output shape as Claude Code at v1; this is the
    primary harness-coupled seam — a future non-Codex adapter that diverges
    on output format swaps this function. The stdout writes are guarded: if
    the harness has already closed the pipe (a killed-session race), emit()
    degrades to silence rather than raising."""
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
