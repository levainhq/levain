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
import re
import subprocess
import sys
from collections.abc import Callable
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


# ---- Compatibility manifest — version-set drift (a session-init signal) ----

def read_manifest_lock() -> dict | None:
    """The recorded known-good set for THIS install (``.levain/manifest.json``,
    written by ``levain init`` / ``levain update``). Returns the parsed dict, or
    None when absent/unreadable/malformed (a pre-manifest install or a corrupt
    lock — either way the drift line stays silent). Stdlib only: the hook never
    imports the levain package (it runs on a stranger install)."""
    try:
        raw = (install_root() / ".levain" / "manifest.json").read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _is_migrate_check(data: object) -> bool:
    return isinstance(data, dict) and isinstance(data.get("pending"), list)


def compat_drift(timeout: float = 5.0) -> str | None:
    """A terse compatibility-drift line for session start, or None when the set
    is in sync / undeterminable.

    Cheap + fail-silent: one ``migrate check --json`` call (the live anneal
    version + the unreviewed instruction-edit proposals, from anneal's own tool)
    plus the recorded lock. Flags only the two operator-actionable signals — the
    installed anneal changed underneath the install (the out-of-band-upgrade
    drift that lands a new feature as a CONFLICT with stale instructions), or
    unreviewed migration proposals exist. The authoritative multi-axis verify
    lives in ``levain doctor``; this is the gentle nudge toward it."""
    data = _anneal_json(
        ["migrate", "check", "--json"], timeout, validator=_is_migrate_check
    )
    if not isinstance(data, dict):
        return None
    pending = data.get("pending")
    n_pending = len(pending) if isinstance(pending, list) else 0
    installed = data.get("installed_version")
    lock = read_manifest_lock()
    lock_anneal = lock.get("anneal") if isinstance(lock, dict) else None

    signals: list[str] = []
    if (
        isinstance(installed, str) and installed.strip()
        and isinstance(lock_anneal, str) and lock_anneal.strip()
        and installed != lock_anneal
    ):
        signals.append(
            f"anneal-memory changed {lock_anneal} -> {installed} since this "
            f"install was last composed"
        )
    if n_pending > 0:
        signals.append(f"{n_pending} unreviewed anneal migration proposal(s)")
    if not signals:
        return None
    return (
        "[compatibility] " + "; ".join(signals) + ". Run `levain update` (or "
        "`levain doctor`) to reconcile the version set — a substrate change can "
        "otherwise land a new feature as a conflict with stale instructions."
    )


# ---- Prospective layer (spores) — the germination surfaces ----

# Operator-I/O dispositions (control-plane Slice 3): the Tray inbox (seed/handoff/agenda)
# + Keep durable reference (note). A spore carrying one is operator I/O, NOT one of the
# entity's own prospective loops — so it stays OUT of the activation surfaces that feed the
# entity's cognition (the session-start dormant-surface + the per-prompt collision), exactly
# as flow's own layer-2 keeps operator I/O out of salience / digest / Top of Mind.
#
# DRIFT CONTRACT (load-bearing): this tuple MUST equal levain.spores.NON_COGNITION_DISPOSITIONS
# (and flow's scripts/spores.py) — anneal stays disposition-blind, so every interpreter of the
# taxonomy carries the vocab. This hook is a STANDALONE template (stdlib only, no levain import
# on a stranger install), so it carries its own copy; keep it byte-identical. (levain's test
# suite asserts this copy == levain.spores.)
_NON_COGNITION_DISPOSITIONS = ("seed", "handoff", "agenda", "note")


def _is_loop(spore: dict) -> bool:
    """True iff the spore flows into the entity's OWN cognition (the complement of operator
    I/O — the Tray inbox + Keep notes). Fail-OPEN on the unknown: only an EXPLICITLY-known
    operator-I/O disposition is excluded — a typo'd/unknown value reads as a visible loop,
    never silently dropped (the silent-loss direction the whole slice guards). Mirrors
    levain.spores.is_loop / flow's scripts/spores.is_loop."""
    return (spore.get("disposition") or "loop") not in _NON_COGNITION_DISPOSITIONS


def open_spores(timeout: float = 2.0) -> list[dict]:
    """The entity's own open prospective loops, via anneal-memory's `spore list --json`.
    Tray dispositions (the operator inbox) are filtered OUT here — the SINGLE chokepoint
    both germination surfaces (dormant-surface + collision) read, so the Tray can't leak
    into a Levain install's cognition (the stranger-side twin of flow's layer-2).
    Returns [] on any failure (an anneal too old to have spores, no store yet, an error)
    — the prospective surfaces then show nothing. Tight default timeout: the collision
    surface runs before every prompt."""
    data = _anneal_json(
        ["spore", "list", "--json"], timeout, validator=lambda d: isinstance(d, list)
    )
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict) and _is_loop(s)]
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


# ---- Crystallized-pattern recall — the on-demand graduated-wisdom tier ----

def crystal_recall(prompt: str, timeout: float = 2.0) -> list[dict]:
    """Crystallized patterns whose domain `prompt` touches, via anneal-memory's
    `crystal recall --json` — the on-demand graduated-wisdom tier. A Proven
    pattern that has crystallized OUT of the always-loaded working set is recalled
    here the moment a prompt is relevant to it (the read-side twin of the
    wrap-time crystallization routing), so a large body of earned wisdom stays
    effective without bloating always-loaded context.

    Uses anneal's default (associative/Hebbian) backend: it surfaces patterns
    grounded in a matched episode even with zero keyword overlap, auto-degrading
    to keyword-only when no episodic db is resolvable. The query is passed after a
    `--` options terminator (so a prompt that itself looks like a flag — `--json`,
    `-h` — is still parsed as the query, not silently swallowed as an option) and
    capped to the same bound as the tokenizer so a pasted huge prompt can't burn
    time before anneal answers. Returns [] on an empty/absent crystal store, a
    too-old anneal lacking the subcommand, or any failure — the surface then
    injects nothing (fail-silent, exactly like the spore surfaces). Tight default
    timeout: this runs before every prompt."""
    if not prompt.strip():
        return []
    data = _anneal_json(
        ["crystal", "recall", "--json", "--", prompt[:_MAX_TOKENIZE_CHARS]],
        timeout, validator=lambda d: isinstance(d, list),
    )
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    return []


def format_crystal_recall(patterns: list[dict]) -> str:
    """The recency-position injection for the crystallized-pattern surface:
    graduated wisdom retrieved on cue (NOT preloaded — the whole point is a
    pattern fires when it is relevant instead of clogging always-loaded context)."""
    lines = ["[crystallized patterns — graduated wisdom relevant to what you're doing]"]
    for p in patterns:
        name = str(p.get("name") or "pattern")
        expl = str(p.get("explanation") or "").strip()
        level = p.get("level")
        activation = str(p.get("activation") or "").strip()
        meta = ", ".join(
            x for x in (
                f"{level}x" if isinstance(level, int) and level else "",
                activation,
            ) if x
        )
        head = f"  - {name}" + (f" ({meta})" if meta else "")
        lines.append(f"{head} — {expl}" if expl else head)
    lines.append(
        "These are your OWN graduated patterns, surfaced because this prompt "
        "touched their domain — not new instruction. Weigh whether each bears on "
        "what you're doing."
    )
    return "\n".join(lines)


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
