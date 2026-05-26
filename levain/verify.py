"""`levain verify-hooks` — automated smoke test for installed activation hooks.

Invokes each hook script with the stdin payload its harness would send and
checks the emitted output is a valid hook envelope (well-formed JSON,
`hookSpecificOutput.hookEventName` matches, `additionalContext` non-empty).

This validates the SCRIPT half of the hook contract independently of whether
the harness actually invokes the hooks at runtime. It is the documented
remedy for the Codex platform hook-reliability gap — Codex does not surface
failures, so wiring may look healthy while hooks never fire.

SessionStart is invoked four times — once per `source` value in
{startup, resume, clear, compact} — because the hook branches on source
(start-catch wrap check fires only for fresh sessions). All four paths must
produce valid output.

UserPromptSubmit is invoked with a realistic payload shape matching what
the harness actually sends (prompt + cwd + session_id + hook_event_name)
rather than an empty stub, so the hook's payload-reading path is exercised.

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
import re
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


SESSION_START_SOURCES = ("startup", "resume", "clear", "compact")


def run_verify_hooks(path: Path) -> int:
    install = Path(str(path)).expanduser().resolve()
    print(f"Levain verify-hooks — testing {install}\n")

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

    # Resolve the Python the harness will actually use — falls back to
    # sys.executable if no install config nominates one.
    python = _resolve_install_python(install)

    results: list[VerifyResult] = []

    ss = hooks_dir / "session_start.py"
    if ss.is_file():
        # All four `source` values must produce valid output. The hook branches
        # on source (start-catch wrap check fires only for startup/clear); each
        # branch must emit valid envelope.
        for source in SESSION_START_SOURCES:
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
    """
    # Claude Code: settings.json hooks[*].command is shaped like
    # `<python> <script-path>`. Parse the first token.
    settings = install / ".claude" / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {})
            for event_name, entries in hooks.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    inner_hooks = entry.get("hooks") if isinstance(entry, dict) else None
                    if not isinstance(inner_hooks, list):
                        continue
                    for hook in inner_hooks:
                        cmd = hook.get("command") if isinstance(hook, dict) else None
                        if isinstance(cmd, str):
                            tokens = shlex.split(cmd)
                            if tokens:
                                return tokens[0]
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # Codex: CODEX_HOME/hooks.json sidecar — same `<python> <script>` shape.
    codex_home_env = os.environ.get("CODEX_HOME")
    codex_home = Path(codex_home_env) if codex_home_env else Path("~/.codex").expanduser()
    codex_hooks = codex_home / "hooks.json"
    agents = install / "AGENTS.md"
    if agents.is_file() and codex_hooks.is_file():
        try:
            data = json.loads(codex_hooks.read_text(encoding="utf-8"))
            for event_name, entries in data.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    cmd = entry.get("command") if isinstance(entry, dict) else None
                    if isinstance(cmd, str):
                        tokens = shlex.split(cmd)
                        if tokens:
                            return tokens[0]
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    return sys.executable
