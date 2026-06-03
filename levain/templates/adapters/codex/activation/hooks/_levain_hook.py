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
import re
import subprocess
import sys
from collections.abc import Callable
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


# Install-time-resolved anneal-memory binary path. `levain init` substitutes
# this placeholder after copying the hook scripts into the operator's install
# dir, so the hooks don't depend on PATH at fire time (Codex sanitizes hook
# env aggressively). The `"{{" in ...` guard skips this entry when the
# placeholder wasn't substituted (editable-install workshop state, or a
# future regression).
_INSTALL_ANNEAL_BIN = "{{ANNEAL_MEMORY}}"


def _anneal_json(
    sub_args: list[str],
    timeout: float,
    validator: Callable[[object], bool] | None = None,
) -> object | None:
    """Run an anneal-memory subcommand (which must produce JSON on stdout)
    against THIS install's store and return the parsed JSON, or None on any
    failure. The store is pinned explicitly with `--db` to this install's store
    (see store_path) — never anneal-memory's machine-global default, which would
    silently merge two installs' memories. Tries the install-resolved binary
    first, then the PATH console script, then the module form.

    No-stall (the load-bearing invariant): a ``TimeoutExpired`` ABORTS the
    candidate loop and returns None. A timeout means this anneal invocation is
    hanging, and the other candidates invoke the SAME anneal (different entry
    points) — they would hang too. So each query costs at most ONE `timeout`,
    not one-per-candidate.

    ``validator``, when given, must accept the parsed JSON; a candidate whose
    JSON fails it is skipped to try the next (so a stale binary returning the
    wrong shape does not block a working fallback). Fail-silent: a missing
    anneal-memory, no store yet, a too-old anneal lacking the subcommand, a parse
    error, or a wrong-shape result all return None."""
    db = str(store_path())
    candidates = []
    if "{{" not in _INSTALL_ANNEAL_BIN:
        candidates.append([_INSTALL_ANNEAL_BIN, "--db", db, *sub_args])
    candidates.extend([
        ["anneal-memory", "--db", db, *sub_args],
        [sys.executable, "-m", "anneal_memory", "--db", db, *sub_args],
    ])
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None  # anneal is hanging — don't re-invoke the same binary
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            continue
        if validator is not None and not validator(parsed):
            continue  # valid JSON, wrong shape (e.g. a stale binary) — try next
        return parsed
    return None


def _is_int_episodes(data: object) -> bool:
    return isinstance(data, dict) and isinstance(data.get("episodes_since_wrap"), int)


def episodes_since_wrap(timeout: float = 5.0) -> int | None:
    """Episodes recorded since the last wrap (the signal Layer D runs on), via
    anneal-memory's `status --json`. Returns None on any failure — Layer D then
    stays silent."""
    data = _anneal_json(["status", "--json"], timeout, validator=_is_int_episodes)
    if isinstance(data, dict):
        value = data.get("episodes_since_wrap")
        if isinstance(value, int):
            return value
    return None


# ---- Prospective layer (spores) — the germination surfaces ----

def open_spores(timeout: float = 2.0) -> list[dict]:
    """Open spores for this install, via anneal-memory's `spore list --json`.
    Returns [] on any failure (an anneal too old to have spores, no store yet,
    an error) — the prospective surfaces then show nothing. Tight default
    timeout: the collision surface runs before every prompt."""
    data = _anneal_json(
        ["spore", "list", "--json"], timeout, validator=lambda d: isinstance(d, list)
    )
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    return []


# Tokens of 3+ word-chars; common words that would create false collisions are
# dropped so a >=2-token overlap means genuine content kinship, not "the"/"and".
# The set includes generic *work* words (code/test/file/review/…) because two
# such shared tokens between a prompt and an unrelated spore would be noise, not
# kinship (codex L3 LOW — precision).
_WORD_RE = re.compile(r"[A-Za-z0-9_]{3,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "this", "that", "with", "from", "into", "your", "you",
    "are", "was", "were", "have", "has", "had", "will", "would", "should", "can",
    "could", "but", "not", "all", "any", "out", "now", "how", "what", "why",
    "who", "when", "where", "lets", "let", "get", "got", "use", "using", "need",
    "want", "about", "just", "like", "make", "made", "one", "two", "its", "our",
    # generic work/dev words — shared, but not evidence of content kinship.
    "code", "test", "tests", "file", "files", "review", "fix", "fixes", "change",
    "changes", "update", "updates", "run", "check", "build", "thing", "things",
    "stuff", "item", "items", "look", "see", "try", "done", "next", "more",
})


# Cap on the characters fed to the regex/set-build, so a pasted huge prompt or
# a pathological spore text can't burn CPU before the result cap applies
# (codex L3 MEDIUM). Collision matches on the gist; the first few KB suffice.
_MAX_TOKENIZE_CHARS = 4000


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text[:_MAX_TOKENIZE_CHARS])} - _STOPWORDS


def spores_colliding(
    prompt: str, spores: list[dict], min_overlap: int = 2, limit: int = 3
) -> list[dict]:
    """Open spores whose text collides with `prompt` by >= `min_overlap`
    distinct, non-trivial shared tokens — the EVENT-based germination surface
    (an open loop surfaces when the current work touches it). Precision-biased:
    a 2-token floor and a small result cap keep this "better nothing than
    noise." Returns the top `limit` by overlap; [] on an empty prompt or no
    real collisions."""
    p = _tokens(prompt)
    if not p:
        return []
    scored: list[tuple[int, dict]] = []
    for s in spores:
        text = s.get("text")
        if not isinstance(text, str):
            continue
        overlap = len(p & _tokens(text))
        if overlap >= min_overlap:
            scored.append((overlap, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]]


def due_dormant_spores(spores: list[dict], limit: int = 5) -> list[dict]:
    """Open spores that have gone DORMANT — quiet too long, or their `next`
    surface-date has arrived (anneal computes the `germination` tier). This is
    the TIME-based germination surface, shown once at session start. Growing,
    resting, and parked spores stay out of the way. Capped at `limit`."""
    due = [s for s in spores if s.get("germination") == "dormant"]
    return due[:limit]


def _format_spore_lines(spores: list[dict], *, with_next: bool) -> list[str]:
    lines: list[str] = []
    for s in spores:
        stype = s.get("type", "?")
        text = s.get("text", "")
        sid = s.get("id", "?")
        tail = ""
        if with_next and s.get("next"):
            tail = f", next {s['next']}"
        lines.append(f"  - ({stype}) {text}  [{sid}{tail}]")
    return lines


def format_spore_collisions(spores: list[dict]) -> str:
    """The recency-position injection for the event-based surface."""
    return "\n".join([
        "[open loops — relevant to what you're doing]",
        *_format_spore_lines(spores, with_next=False),
        "These open loops touch the current work. Advance, resolve "
        "(spore_descend / spore_ascend), or set aside as fits — don't let a "
        "relevant one go unattended.",
    ])


def format_due_spores(spores: list[dict]) -> str:
    """The primacy-position injection for the time-based surface."""
    return "\n".join([
        "[open loops — due or gone quiet]",
        *_format_spore_lines(spores, with_next=True),
        "These have gone quiet or their time has come. For each: still alive "
        "(spore_touch to keep it), or ready to compost (spore_descend)?",
    ])


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
