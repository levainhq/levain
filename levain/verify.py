"""`levain verify-hooks` — automated smoke test for installed activation hooks.

Invokes each hook script with the stdin payload its harness would send and
checks the emitted output is a valid hook envelope (well-formed JSON,
`hookSpecificOutput.hookEventName` matches, `additionalContext` non-empty).

This validates the SCRIPT half of the hook contract independently of whether
the harness actually invokes the hooks at runtime. It is the documented
remedy for the Codex platform hook-reliability gap — Codex does not surface
failures, so wiring may look healthy while hooks never fire.

SessionStart is invoked twice — once per `source` equivalence class:
{startup, clear} fire the start-catch wrap check; {resume, compact} skip
it. The hook reads `payload['source']` at exactly one branch site, so
testing one representative from each class is the honest coverage. The
representative-per-class shape leaves room to add a third class without
forcing the test to grow N×.

UserPromptSubmit is invoked with the payload shape the harness actually
sends (prompt + cwd + session_id + hook_event_name). The current hook
discards the payload after a JSON parse, so the "realistic" shape mainly
hardens against the hook starting to read payload fields in a future
revision — the parse-and-discard surface is also exercised by an empty
stub, but the realistic shape documents what callers will send.

The Python interpreter is resolved from the install's adapter config (the
exact Python the harness will use), falling back to sys.executable. This
matters when the venv running `levain verify-hooks` differs from the venv
the install was created in.

What it does NOT check: whether the harness itself invokes the hooks. That
requires an interactive session in the install (see the adapter README's
"Verify the install" section).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_COLOR = _supports_color()
_OK = "\033[32m✓\033[0m" if _COLOR else "[OK]"
_FAIL = "\033[31m✗\033[0m" if _COLOR else "[FAIL]"


@dataclass(frozen=True)
class VerifyResult:
    name: str
    ok: bool
    detail: str
    hint: str | None = None


def _emit(r: VerifyResult) -> None:
    badge = _OK if r.ok else _FAIL
    print(f"  {badge} {r.name}: {r.detail}")
    if not r.ok and r.hint:
        print(f"      → {r.hint}")


# SessionStart `source` values fall into two equivalence classes in the
# canonical session_start.py: (startup, clear) → start-catch wrap-check
# fires; (resume, compact) → start-catch skipped, posture-only. Testing
# one representative per class is honest coverage; iterating all four
# tests the same code path twice on each side and would green-light a
# regression that inverted the branch condition.
SESSION_START_REPRESENTATIVES = ("startup", "compact")
# All four documented sources — kept as a public constant for adapters/
# tests that want to confirm vocabulary parity with Claude Code.
SESSION_START_SOURCES = ("startup", "resume", "clear", "compact")


def run_verify_hooks(path: Path) -> int:
    install = Path(str(path)).expanduser().resolve()
    print(f"Levain verify-hooks — testing {install}\n")

    # A clean hookless entity (openhands) has no activation hooks — verifying them is N/A,
    # not a FAIL. Use the SHARED `effective_adapter` classifier (the same one doctor + run
    # use, so all three agree) where hosted files dominate a possibly-stale marker: an
    # openhands marker sitting on top of a stray hook tree from a `--force` adapter switch is
    # NOT a clean hookless entity, so it falls through and the real hooks get verified.
    from levain.install import effective_adapter

    if effective_adapter(install) == "openhands":
        print("  openhands: a hookless adapter — its activation is the runtime condenser,")
        print("  so there are no activation hooks to smoke-test. Run it with `levain run`.")
        print("\nNothing to verify (0 hooks).")
        return 0

    hooks_dir = install / "activation" / "hooks"
    if not hooks_dir.is_dir():
        miss = VerifyResult(
            "activation/hooks/",
            False,
            "missing — no hooks to verify",
            "Run `levain init` in this directory first.",
        )
        _emit(miss)
        print("\n1 check(s) FAILED.")
        return 1

    # Warn if LEVAIN_HOOK_SUPPRESS is set globally — verify-hooks strips it
    # from the child env to test the script half cleanly, but live harness
    # hooks honor it. Operator gets green here, silence at fire time.
    if os.environ.get("LEVAIN_HOOK_SUPPRESS"):
        print(
            "  ⚠ LEVAIN_HOOK_SUPPRESS is set in your environment. verify-hooks\n"
            "    will strip it for this run, but live harness hooks honor it —\n"
            "    your real sessions will see no hook output until you unset it.\n"
        )

    # Resolve the Python the harness will actually use — falls back to
    # sys.executable if no install config nominates one.
    python = _resolve_install_python(install)

    # Pre-check: the resolved interpreter must exist. Most common operator
    # failure is "I deleted/moved the venv this install was created with."
    # Surface a distinct error rather than letting subprocess.run produce
    # a generic OSError downstream.
    if not Path(python).is_file():
        _emit(VerifyResult(
            "configured python",
            False,
            f"interpreter not found at {python}",
            (
                "The install was wired to this Python (likely in a venv that's "
                "been deleted or moved). Re-run `levain init --force` from the "
                "venv you want to wire it to, or fix the path in "
                ".claude/settings.json / ~/.codex/hooks.json."
            ),
        ))
        print("\n1 check(s) FAILED.")
        return 1

    results: list[VerifyResult] = []

    ss = hooks_dir / "session_start.py"
    if ss.is_file():
        # One representative per source equivalence class — see comment on
        # SESSION_START_REPRESENTATIVES above for why this isn't all four.
        for source in SESSION_START_REPRESENTATIVES:
            r = _invoke(ss, install, {"source": source}, "SessionStart", python)
            # Tag the source onto the result name so failures are localized.
            r = VerifyResult(
                name=f"session_start.py (source={source})",
                ok=r.ok,
                detail=r.detail,
                hint=r.hint,
            )
            results.append(r)
    else:
        results.append(
            VerifyResult(
                "session_start.py",
                False,
                "script missing",
                "Re-run `levain init`.",
            )
        )

    ups = hooks_dir / "user_prompt_submit.py"
    if ups.is_file():
        # Realistic payload shape — what Claude Code actually sends rather
        # than an empty stub. Exercises the hook's payload-reading path.
        ups_payload = {
            "prompt": "verify-hooks smoke test prompt",
            "cwd": str(install),
            "session_id": "verify-hooks-session",
            "hook_event_name": "UserPromptSubmit",
        }
        results.append(_invoke(ups, install, ups_payload, "UserPromptSubmit", python))
    else:
        results.append(
            VerifyResult(
                "user_prompt_submit.py",
                False,
                "script missing",
                "Re-run `levain init`.",
            )
        )

    for r in results:
        _emit(r)

    failed = [r for r in results if not r.ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) FAILED.")
        print(
            "  Note: this verifies the script half of the hook contract. "
            "Whether the harness actually invokes the hooks at runtime "
            "(notably under Codex `codex exec`) is a separate question — "
            "see the adapter README."
        )
        return 1
    print("All hooks emitted valid output.")
    print(
        "  Note: this confirms the script half of the hook contract is "
        "sound. Whether the harness actually invokes the hooks at runtime "
        "is a separate question — verify in an interactive session."
    )
    return 0


def _invoke(
    script: Path,
    install: Path,
    payload: dict,
    expected_event: str,
    python: str,
) -> VerifyResult:
    """Run a hook script under conditions that match a harness invocation.

    cwd is set to the install root because the hook's `should_fire()` gate
    requires cwd to be inside the install. LEVAIN_HOOK_SUPPRESS is stripped
    from the child env because the parent shell may have it set for some
    other reason, and that would silently make every verification look broken.

    `python` is the interpreter resolved from the install's adapter config
    (the same one the harness will actually use), not necessarily the one
    running `levain verify-hooks`.
    """
    env = {k: v for k, v in os.environ.items() if k != "LEVAIN_HOOK_SUPPRESS"}

    try:
        result = subprocess.run(
            [python, str(script)],
            cwd=str(install),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(
            script.name,
            False,
            "timed out after 10s",
            "Hook likely hangs on subprocess (anneal-memory status query). Investigate _levain_hook.episodes_since_wrap.",
        )
    except OSError as e:
        return VerifyResult(
            script.name,
            False,
            f"failed to invoke: {e}",
            f"Check {script} exists and Python at {python} is runnable.",
        )

    if result.returncode != 0:
        stderr_preview = result.stderr.strip()[:200]
        return VerifyResult(
            script.name,
            False,
            f"exit {result.returncode}; stderr={stderr_preview!r}",
            "Hooks are fail-open and must always exit 0. Script is corrupt or import is broken.",
        )

    out = result.stdout.strip()
    if not out:
        return VerifyResult(
            script.name,
            False,
            "empty output — hook stayed silent",
            (
                f"cwd was {install}; check `should_fire()` "
                f"(in_install_session + not LEVAIN_HOOK_SUPPRESS=1) and that "
                f"activation/{{posture,recency_directives}}.md is readable."
            ),
        )

    try:
        envelope = json.loads(out)
    except json.JSONDecodeError as e:
        return VerifyResult(
            script.name,
            False,
            f"invalid JSON output: {e}",
            f"Hook output must be parseable JSON. Got: {out[:200]!r}",
        )

    hso = envelope.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return VerifyResult(
            script.name,
            False,
            "missing hookSpecificOutput in envelope",
            f"Envelope was: {out[:200]!r}",
        )

    event = hso.get("hookEventName")
    if event != expected_event:
        return VerifyResult(
            script.name,
            False,
            f"hookEventName mismatch: expected {expected_event!r}, got {event!r}",
            "Hook output schema bug.",
        )

    ctx = hso.get("additionalContext")
    if not isinstance(ctx, str) or not ctx.strip():
        return VerifyResult(
            script.name,
            False,
            "additionalContext missing or empty",
            "Hook fired but emitted no context — check activation/{posture,recency_directives}.md is non-empty.",
        )

    preview = ctx.replace("\n", " ⏎ ").strip()
    if len(preview) > 80:
        preview = preview[:80] + "…"
    return VerifyResult(
        script.name,
        True,
        f"{len(ctx)} chars injected: {preview}",
    )


def _resolve_install_python(install: Path) -> str:
    """Return the Python interpreter path the install's adapter is wired to.

    Reads it from `.claude/settings.json` (Claude Code) or `~/.codex/hooks.json`
    via CODEX_HOME (Codex). Falls back to `sys.executable` when no config is
    available or no python can be extracted. The point is to test the same
    interpreter the harness will actually invoke at fire time, which may
    differ from the venv currently running `levain verify-hooks`.

    Both Claude Code and Codex use the same nested shape:
        {"hooks": {<event>: [{"hooks": [{"command": "<py> <script>", ...}]}]}}
    so the walk is shared between branches. Claude Code is checked first;
    Codex secondary. Both also gate on the install's tag-file presence
    (CLAUDE.md or AGENTS.md) — the config file may exist from a previous
    install but only the tagged adapter is the active one.

    Anti-foreign-hook: the walker only trusts a command whose target script
    lives inside `<install>/activation/hooks/`. A foreign hook command in
    the same config (operator-added, or shared across installs) silently
    gets skipped — so verify-hooks never accidentally tests a non-Levain
    interpreter.
    """
    # Claude Code: .claude/settings.json — gated by CLAUDE.md tag file.
    if (install / "CLAUDE.md").is_file():
        py = _python_from_hooks_config(install / ".claude" / "settings.json", install)
        if py is not None:
            return py

    # Codex: CODEX_HOME/hooks.json sidecar — gated by AGENTS.md tag file.
    if (install / "AGENTS.md").is_file():
        codex_home_env = os.environ.get("CODEX_HOME")
        codex_home = (
            Path(codex_home_env) if codex_home_env else Path("~/.codex").expanduser()
        )
        py = _python_from_hooks_config(codex_home / "hooks.json", install)
        if py is not None:
            return py

    return sys.executable


def _python_from_hooks_config(path: Path, install: Path) -> str | None:
    """Walk a hooks-config JSON file and return the python for a hook
    targeting `install/activation/hooks/`. Skip commands targeting other
    paths (foreign hooks shouldn't donate their python to verify-hooks).

    Shared between Claude Code's `.claude/settings.json` and Codex's
    `CODEX_HOME/hooks.json` because both use the same nested shape:
        {"hooks": {<event>: [{"hooks": [{"command": "..."}, ...]}]}}
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return None
    install_hooks_dir = (install / "activation" / "hooks").resolve()
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            inner_hooks = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner_hooks, list):
                continue
            for hook in inner_hooks:
                cmd = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(cmd, str):
                    continue
                # shlex parse is per-command — a bad command shouldn't poison
                # the entire walk.
                try:
                    tokens = shlex.split(cmd)
                except ValueError:
                    continue
                if len(tokens) < 2:
                    continue
                # Filter foreign hooks: command must target a script inside
                # THIS install's activation/hooks/ dir.
                try:
                    script_path = Path(tokens[1]).resolve()
                except (OSError, ValueError):
                    continue
                try:
                    script_path.relative_to(install_hooks_dir)
                except ValueError:
                    continue
                return tokens[0]
    return None
