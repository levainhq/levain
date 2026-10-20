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

UserPromptSubmit is invoked with the payload shape the harness sends, less
`session_id` (see `ups_payload`): it runs in the live install, and a session
id would make it record a session there.

The Python interpreter is resolved from the install's adapter config (the
exact Python the harness will use), falling back to sys.executable. This
matters when the venv running `levain verify-hooks` differs from the venv
the install was created in.

The record-only transcript hooks (stop.py, post_tool_use.py) print nothing, so
their check is the line they write: each runs, from the installed bytes, in a
throwaway copy of the hooks directory under a temporary install root, with a
synthetic event; the check reads the one transcript line back and the
temporary root is deleted. Nothing is written into the live install.

What it does NOT check: whether the harness itself invokes the hooks. That
requires an interactive session in the install (see the adapter README's
"Verify the install" section).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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
    launcher = _resolve_install_launcher(install)
    python = launcher[0]

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
                ".claude/settings.local.json (or .claude/settings.json) / ~/.codex/hooks.json."
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
            r = _invoke(ss, install, {"source": source}, "SessionStart", launcher)
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
        results.append(_invoke(ups, install, ups_payload(install), "UserPromptSubmit", launcher))
    else:
        results.append(
            VerifyResult(
                "user_prompt_submit.py",
                False,
                "script missing",
                "Re-run `levain init`.",
            )
        )

    for script, payload, kind, text in _RECORD_ONLY:
        if (hooks_dir / script).is_file():
            results.append(_invoke_record_only(hooks_dir, script, payload, kind, text, launcher))

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


def ups_payload(install: Path) -> dict:
    """What Claude Code sends UserPromptSubmit, less ``session_id``: with one, the hook (run in the live install,
    since its output depends on the install's activation files) records a session, its prompt and its state-line
    mark there, and the cockpit would list a session nobody ran. With none, it records nothing."""
    return {"prompt": "verify-hooks smoke test prompt", "cwd": str(install), "hook_event_name": "UserPromptSubmit"}


# (script, synthetic event, transcript kind, the text it must record). Absent scripts are skipped: an install
# from before the transcript hooks has none, and doctor's transcript-hooks row says so.
_RECORD_ONLY = (
    ("stop.py", {"hook_event_name": "Stop", "session_id": "verify-hooks-session",
                 "last_assistant_message": "verify-hooks smoke reply"}, "reply", "verify-hooks smoke reply"),
    ("post_tool_use.py", {"hook_event_name": "PostToolUse", "session_id": "verify-hooks-session",
                          "tool_name": "Read", "tool_input": {"file_path": "verify-hooks-smoke.txt"}},
     "tool", "Read: verify-hooks-smoke.txt"),
)


def _copy_hook_files(src: Path, dest: Path) -> None:
    """The hooks directory's top-level ``.py`` files into ``dest``, each copied by CONTENT (a symlinked hook file's
    target is copied, so nothing in the copy resolves back into the install). Subdirectories, directory links and
    anything that is not a regular file are left out: the hooks are one flat directory, and a link to a large tree
    must not be walked."""
    dest.mkdir(parents=True)
    for entry in sorted(src.iterdir()):
        if entry.suffix == ".py" and entry.is_file():          # is_file follows a link, and is False for a dir or a fifo
            shutil.copyfile(entry, dest / entry.name)


def _invoke_record_only(hooks_dir: Path, script: str, payload: dict, kind: str, text: str,
                        launcher: list[str]) -> VerifyResult:
    """Run a record-only hook from a copy of the installed hooks directory under a temporary install root (the
    hook derives its root from its own file location), then read back the one transcript line it wrote there.
    The temporary root is removed whatever happens; the live install's sessions directory is never touched."""
    from levain.launch import child_env

    tmp = Path(tempfile.mkdtemp(prefix="levain-verify-"))
    try:
        _copy_hook_files(hooks_dir, tmp / "activation" / "hooks")
        env = child_env()
        env.pop("LEVAIN_HOOK_SUPPRESS", None)
        env.pop("LEVAIN_SCOPE", None)
        try:
            r = subprocess.run([*launcher, str(tmp / "activation" / "hooks" / script)], cwd=str(tmp),
                               input=json.dumps({**payload, "cwd": str(tmp)}), capture_output=True, text=True,
                               timeout=10, env=env)
        except subprocess.TimeoutExpired:
            return VerifyResult(script, False, "timed out after 10s", "A record-only hook must return at once.")
        except OSError as e:
            return VerifyResult(script, False, f"failed to invoke: {e}", f"Check Python at {launcher[0]} is runnable.")
        if r.returncode != 0:
            return VerifyResult(script, False, f"exit {r.returncode}; stderr={r.stderr.strip()[:200]!r}",
                                "Hooks are fail-open and must always exit 0. Script is corrupt or import is broken.")
        if r.stdout.strip():
            return VerifyResult(script, False, f"printed output: {r.stdout.strip()[:120]!r}",
                                "A record-only hook prints nothing.")
        logs = sorted((tmp / ".levain" / "sessions").glob("*.log.jsonl"))
        lines = [ln for f in logs for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) != 1:
            return VerifyResult(script, False, f"wrote {len(lines)} transcript line(s), expected 1",
                                "The hook ran but recorded nothing: check its `should_fire()` and session_log.")
        try:
            event = json.loads(lines[0])
        except ValueError as e:
            return VerifyResult(script, False, f"transcript line is not JSON: {e}", f"Line: {lines[0][:200]!r}")
        if not (isinstance(event, dict) and event.get("v") == 1 and event.get("kind") == kind
                and event.get("text") == text and event.get("cut") == 0 and isinstance(event.get("at"), str)):
            return VerifyResult(script, False, f"transcript line has the wrong shape: {lines[0][:200]!r}",
                                f"Expected v 1, kind {kind!r}, text {text!r}.")
        return VerifyResult(script, True, f"recorded a {kind} line (in a temporary copy, since removed)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _invoke(
    script: Path,
    install: Path,
    payload: dict,
    expected_event: str,
    launcher: list[str],
) -> VerifyResult:
    """Run a hook script under conditions that match a harness invocation.

    cwd is set to the install root because the hook's `should_fire()` gate
    requires cwd to be inside the install. LEVAIN_HOOK_SUPPRESS is stripped
    from the child env because the parent shell may have it set for some
    other reason, and that would silently make every verification look broken.

    `launcher` (the interpreter and its flags) is resolved from the install's adapter config
    (the same one the harness will actually use), not necessarily the one
    running `levain verify-hooks`.
    """
    from levain.launch import child_env

    env = child_env()
    env.pop("LEVAIN_HOOK_SUPPRESS", None)

    try:
        result = subprocess.run(
            [*launcher, str(script)],
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
            "Hook likely hangs on subprocess (anneal-memory status query). Investigate _levain_hook.wrap_state (episodes_since_wrap delegates to it).",
        )
    except OSError as e:
        return VerifyResult(
            script.name,
            False,
            f"failed to invoke: {e}",
            f"Check {script} exists and Python at {launcher[0]} is runnable.",
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
    """The interpreter of :func:`_resolve_install_launcher`."""
    return _resolve_install_launcher(install)[0]


def _resolve_install_launcher(install: Path) -> list[str]:
    """Return the Python interpreter path the install's adapter is wired to.

    Reads it from the file levain wires Claude Code's hooks into (host_guard.cc_levain_hooks_file:
    `.claude/settings.local.json` or `.claude/settings.json`, then the other one) or `~/.codex/hooks.json`
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
    # Claude Code: the file levain wires its hooks into (host_guard.cc_levain_hooks_file), then the other one (an
    # install whose hooks `levain update` has not moved yet) — gated by CLAUDE.md tag file.
    if (install / "CLAUDE.md").is_file():
        from levain.host_guard import cc_levain_hooks_file

        wired = cc_levain_hooks_file(install)  # where levain wires them; the other file: an unmigrated install
        for name in dict.fromkeys((wired.name, "settings.local.json", "settings.json")):
            py = _launcher_from_hooks_config(install / ".claude" / name, install)
            if py is not None:
                return py

    # Codex: CODEX_HOME/hooks.json sidecar — gated by AGENTS.md tag file.
    if (install / "AGENTS.md").is_file():
        codex_home_env = os.environ.get("CODEX_HOME")
        codex_home = (
            Path(codex_home_env) if codex_home_env else Path("~/.codex").expanduser()
        )
        py = _launcher_from_hooks_config(codex_home / "hooks.json", install)
        if py is not None:
            return py

    return [sys.executable]


# Claude Code's settings.template.json writes the hook command with a RUNTIME
# harness placeholder, `${CLAUDE_PROJECT_DIR}`, that Claude Code substitutes at
# fire time. Where levain's hooks live in settings.local.json (host_guard.
# cc_levain_hooks_file) `levain init` fills it with the install path; where
# they live in settings.json, and on an install from before the local file
# until `levain update`, the file literally contains
# `${CLAUDE_PROJECT_DIR}/activation/hooks/session_start.py`. To match that
# against this install's hooks dir we substitute it ourselves — the same
# substitution doctor.py makes (see _hook_command_targets). Kept as a named
# constant so the two surfaces stay in lock-step; if a second harness grows its
# own placeholder, centralize here rather than scatter literal .replace calls.
_CLAUDE_PROJECT_DIR_PLACEHOLDER = "${CLAUDE_PROJECT_DIR}"


def _launcher_from_hooks_config(path: Path, install: Path) -> list[str] | None:
    """Walk a hooks-config JSON file and return the launcher (every token before the script: the python and
    any flags, ``-EP -m levain.hookrun`` on Codex) for a hook
    targeting `install/activation/hooks/`. Skip commands targeting other
    paths (foreign hooks shouldn't donate their python to verify-hooks).

    Shared between Claude Code's `.claude/settings.json` and Codex's
    `CODEX_HOME/hooks.json` because both use the same nested shape:
        {"hooks": {<event>: [{"hooks": [{"command": "..."}, ...]}]}}

    The script-path token may carry a RUNTIME harness placeholder
    (`${CLAUDE_PROJECT_DIR}`) rather than a literal install path — settings.json
    carries the template's shape wherever levain's hooks live there (the harness
    substitutes it at fire time). We substitute it here so the foreign-hook
    filter can still locate the script inside THIS install. Without this the
    filter would reject every Claude Code install's command as "foreign" and
    verify-hooks would silently fall back to its own interpreter — testing the
    wrong Python entirely.

    The script path is not guaranteed to be tokens[1] — a command may carry
    flags between the interpreter and the script (`python -X dev <script>`).
    We scan EVERY token after the interpreter and accept the command if ANY of
    them, after placeholder substitution, resolves into this install's hooks
    dir. The interpreter (tokens[0]) is returned as-is: it uses the install-time
    {{PYTHON}} placeholder which install.py has already baked to a literal, so
    no runtime substitution applies to it. If a foreign interpreter is wired
    to this install's script, returning it is correct — the harness will
    genuinely invoke it, and verify-hooks should smoke-test exactly that (and
    likely go red, which is the right outcome, not a false green).
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
                # Filter foreign hooks: the command must target a script inside
                # THIS install's activation/hooks/ dir. The script token may sit
                # at any position after the interpreter (flags may come between,
                # e.g. `python -X dev <script>`), so we scan EVERY token after
                # tokens[0]. Each candidate carries the runtime harness
                # placeholder ${CLAUDE_PROJECT_DIR} (see
                # _CLAUDE_PROJECT_DIR_PLACEHOLDER) substituted to this install
                # before resolving — matching doctor.py:_hook_command_targets.
                # The interpreter (tokens[0]) is returned verbatim: it uses the
                # install-time {{PYTHON}} placeholder, already baked to a
                # literal by install.py, so no runtime substitution applies.
                #
                # A token "targets" the hooks dir iff it resolves to an EXISTING
                # file under it — not merely a path that sits under it. The
                # existence gate is load-bearing: Path.resolve() is relative to
                # cwd, so a bare flag token (`-X`) run with cwd inside the hooks
                # dir would resolve under it and pass a plain relative_to check,
                # falsely donating a foreign interpreter to verify-hooks. Flags
                # never resolve to files, so is_file() closes that hole while
                # still accepting a foreign interpreter genuinely wired to OUR
                # script (correct — the harness will invoke it; verify-hooks
                # should smoke-test exactly that and likely go red).
                for i, tok in enumerate(tokens[1:], 1):
                    candidate = tok.replace(
                        _CLAUDE_PROJECT_DIR_PLACEHOLDER, str(install)
                    )
                    try:
                        script_path = Path(candidate).resolve()
                    except (OSError, ValueError):
                        continue
                    if not script_path.is_file():
                        continue
                    try:
                        script_path.relative_to(install_hooks_dir)
                    except ValueError:
                        continue
                    return tokens[:i]
    return None


def _python_from_hooks_config(path: Path, install: Path) -> str | None:
    """The interpreter of :func:`_launcher_from_hooks_config`."""
    launcher = _launcher_from_hooks_config(path, install)
    return launcher[0] if launcher else None
