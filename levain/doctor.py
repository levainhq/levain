"""`levain doctor` — loud, in-environment health check of a Levain install.

What it checks:
  - Install layout: `seed/`, `activation/`, hook scripts present and parse.
  - Runtime: Python interpreter; `anneal-memory` available as CLI or module.
  - Store: `.levain/memory.db` opens cleanly via sqlite3.
  - Per detected adapter (Claude Code if `CLAUDE.md` + `.claude/`; Codex if
    `AGENTS.md`):
      * Config files parse.
      * Hooks wired (and, for Codex, wired to THIS install).
      * MCP server registered under `anneal_memory` with the install's store
        path in its arguments.
      * For Claude Code: the Python interpreter referenced in hook commands
        is resolvable.

Exits nonzero on any failure. Counter to fail-open hooks — a silent dead
install is otherwise indistinguishable from a working one.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_COLOR = _supports_color()
_OK = "\033[32m✓\033[0m" if _COLOR else "[OK]"
_FAIL = "\033[31m✗\033[0m" if _COLOR else "[FAIL]"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    hint: str | None = None


def _emit(r: CheckResult) -> None:
    badge = _OK if r.ok else _FAIL
    print(f"  {badge} {r.name}: {r.detail}")
    if not r.ok and r.hint:
        print(f"      → {r.hint}")


def run_doctor(path: Path, invoke: bool = False) -> int:
    install = Path(str(path)).expanduser().resolve()
    print(f"Levain doctor — checking {install}\n")

    core: list[CheckResult] = []
    core.extend(_check_install_layout(install))
    core.extend(_check_runtime(install))
    core.extend(_check_store(install))
    core.extend(_check_compat_set(install))
    for r in core:
        _emit(r)

    # Adapter detection: presence of the harness tag-file in the install root.
    # Wiring is checked downstream — a tagged install with missing wiring
    # surfaces as a wiring FAIL, not "no adapter."
    adapter_results: dict[str, list[CheckResult]] = {}
    if (install / "CLAUDE.md").is_file():
        adapter_results["claude-code"] = _check_claude_code(install)
    if (install / "AGENTS.md").is_file():
        adapter_results["codex"] = _check_codex(install)

    if not adapter_results:
        no_adapter = CheckResult(
            "adapter",
            False,
            "no adapter detected (no CLAUDE.md or AGENTS.md at install root)",
            "Run `levain init` in this directory to install one.",
        )
        print()
        _emit(no_adapter)
        core.append(no_adapter)

    for adapter, rs in adapter_results.items():
        print(f"\n  Adapter: {adapter}")
        for r in rs:
            _emit(r)

    all_results = core + [r for rs in adapter_results.values() for r in rs]
    failed = [r for r in all_results if not r.ok]

    # --invoke: layer the dynamic verify-hooks check on top of static checks.
    # Run UNCONDITIONALLY when --invoke is set — the failure class --invoke
    # was built to surface (silent dead hooks) is most common precisely when
    # static is partial-fail. Gating dynamic on full-static-green defeats the
    # composition. The operator chose --invoke; trust the choice.
    verify_rc = 0
    if invoke:
        print("\n  Live-fire (--invoke): running verify-hooks dynamic check...")
        from levain.verify import run_verify_hooks
        verify_rc = run_verify_hooks(install)

    print()
    if failed and verify_rc != 0:
        print(f"{len(failed)} static check(s) FAILED + live-fire verify-hooks FAILED.")
    elif failed:
        print(f"{len(failed)} check(s) FAILED.")
    elif verify_rc != 0:
        print("Static checks passed but live-fire verify-hooks FAILED.")
    else:
        print("All checks passed.")
    if not invoke:
        print(
            "  Note: doctor is static — it does NOT invoke the activation hooks.\n"
            "        For the actual firing test, run `levain verify-hooks`\n"
            "        or `levain doctor --invoke`.\n"
            "        Also: hooks no-op when LEVAIN_HOOK_SUPPRESS=1 is in the env."
        )
    return 1 if failed or verify_rc != 0 else 0


# The base-install REQUIRED-minimum seed set (a static check on an installed
# dir). NOT the full seed taxonomy: the canonical seed classification — which
# files load as harness context (the roster-driven adapter @import list) vs the
# non-context files (continuity.md / README.md) — lives in `levain.packs`
# (NON_IMPORT_SEED / BASE_IMPORT_ORDER / import_entries). A PACK extends the seed
# set, so this fixed list checks only the base minimum; if doctor ever needs to
# validate a pack-layered install's full import list it must read the installed
# adapter file or a recorded roster, not grow this constant (Slice 3 deferral).
_SEED_REQUIRED = ("origin.md", "partnership.md", "world.md", "memory.md")
_SEED_EXPECTED = _SEED_REQUIRED + ("continuity.md", "README.md")
_HOOK_REQUIRED = ("session_start.py", "user_prompt_submit.py", "_levain_hook.py")
_ACTIVATION_FILES = ("posture.md", "recency_directives.md")


def _check_install_layout(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []

    if not install.is_dir():
        return [
            CheckResult(
                "install root",
                False,
                f"not a directory: {install}",
                "Pass --path pointing to a Levain install.",
            )
        ]
    results.append(CheckResult("install root", True, str(install)))

    seed = install / "seed"
    if not seed.is_dir():
        results.append(
            CheckResult(
                "seed/",
                False,
                "missing",
                "Required: seed/{origin,partnership,world,memory}.md",
            )
        )
    else:
        missing = [f for f in _SEED_REQUIRED if not (seed / f).is_file()]
        if missing:
            results.append(
                CheckResult(
                    "seed/",
                    False,
                    f"missing required files: {', '.join(missing)}",
                    "Re-run `levain init` or restore from adapter source.",
                )
            )
        else:
            present = [f for f in _SEED_EXPECTED if (seed / f).is_file()]
            results.append(
                CheckResult(
                    "seed/",
                    True,
                    f"{len(present)} files present ({', '.join(present)})",
                )
            )

    activation = install / "activation"
    if not activation.is_dir():
        results.append(
            CheckResult(
                "activation/",
                False,
                "missing",
                "Required: activation/{posture.md, recency_directives.md, hooks/}",
            )
        )
        return results

    missing_files = [f for f in _ACTIVATION_FILES if not (activation / f).is_file()]
    if missing_files:
        results.append(
            CheckResult(
                "activation/",
                False,
                f"missing: {', '.join(missing_files)}",
                "Re-run `levain init` or restore from adapter source.",
            )
        )
    else:
        results.append(
            CheckResult(
                "activation/",
                True,
                f"{len(_ACTIVATION_FILES)} activation files present",
            )
        )

    hooks = activation / "hooks"
    if not hooks.is_dir():
        results.append(
            CheckResult(
                "activation/hooks/",
                False,
                "missing",
                "Required: activation/hooks/{session_start,user_prompt_submit,_levain_hook}.py",
            )
        )
        return results

    missing_hooks = [f for f in _HOOK_REQUIRED if not (hooks / f).is_file()]
    if missing_hooks:
        results.append(
            CheckResult(
                "activation/hooks/",
                False,
                f"missing scripts: {', '.join(missing_hooks)}",
                "Re-run `levain init` or restore from adapter source.",
            )
        )
        return results

    syntax_errors = []
    for f in _HOOK_REQUIRED:
        script = hooks / f
        try:
            compile(script.read_text(), str(script), "exec")
        except SyntaxError as e:
            syntax_errors.append(f"{f}: {e.msg} line {e.lineno}")
    if syntax_errors:
        results.append(
            CheckResult(
                "hook scripts",
                False,
                "; ".join(syntax_errors),
                "Hook scripts are corrupt. Re-run `levain init`.",
            )
        )
    else:
        results.append(
            CheckResult(
                "hook scripts",
                True,
                f"{len(_HOOK_REQUIRED)} scripts present and parse OK",
            )
        )

    return results


def _check_runtime(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []

    py = Path(sys.executable)
    py_version = ".".join(map(str, sys.version_info[:3]))
    results.append(
        CheckResult("python interpreter", True, f"{py} ({py_version})")
    )

    am_cli = shutil.which("anneal-memory")
    if am_cli:
        ok, out = _probe([am_cli, "--version"])
        if ok:
            version = (out.strip().splitlines() or ["present"])[0]
            results.append(
                CheckResult("anneal-memory CLI", True, f"{am_cli} ({version})")
            )
        else:
            results.append(
                CheckResult(
                    "anneal-memory CLI",
                    False,
                    f"{am_cli} ran but failed: {out}",
                    "pip install --upgrade anneal-memory",
                )
            )
        return results

    ok, out = _probe([sys.executable, "-m", "anneal_memory", "--version"])
    if ok:
        version = (out.strip().splitlines() or ["present"])[0]
        results.append(
            CheckResult(
                "anneal-memory module",
                True,
                f"importable via {sys.executable} ({version})",
            )
        )
    else:
        results.append(
            CheckResult(
                "anneal-memory",
                False,
                "not on PATH and not importable as a Python module",
                "Install with: pip install anneal-memory",
            )
        )
    return results


def _probe(cmd: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    """Run a command; return (ok, stdout-or-truncated-stderr).

    Strips LEVAIN_HOOK_SUPPRESS from the child environment so a probe that
    happens to invoke a Levain-aware tool isn't silenced by a parent shell
    that set the var for an unrelated reason.
    """
    env = {k: v for k, v in os.environ.items() if k != "LEVAIN_HOOK_SUPPRESS"}
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        if r.returncode == 0:
            return True, r.stdout
        return False, (r.stderr or r.stdout).strip()[:500]
    except FileNotFoundError:
        return False, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def _check_store(install: Path) -> list[CheckResult]:
    store = install / ".levain" / "memory.db"

    if not store.is_file():
        return [
            CheckResult(
                ".levain/memory.db",
                False,
                "missing",
                f"Initialize: anneal-memory --db {store} init",
            )
        ]

    try:
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2) as con:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name LIMIT 5"
            )
            tables = [row[0] for row in cur.fetchall()]
    except sqlite3.Error as e:
        return [
            CheckResult(
                ".levain/memory.db",
                False,
                f"sqlite open failed: {e}",
                "Check perms; try `anneal-memory --db <path> status`.",
            )
        ]

    if not tables:
        return [
            CheckResult(
                ".levain/memory.db",
                False,
                "empty (no tables)",
                f"Initialize: anneal-memory --db {store} init",
            )
        ]
    label = f"reachable ({len(tables)} table(s): {', '.join(tables[:3])}{'…' if len(tables) > 3 else ''})"
    return [CheckResult(".levain/memory.db", True, label)]


def _check_compat_set(install: Path) -> list[CheckResult]:
    """Compatibility-manifest drift: is the installed version SET (anneal +
    schema + acked migrations) at the known-good this levain release declares?

    Folds the manifest's verify into doctor (the natural home — doctor already
    checks the anneal CLI + the store). Each drift axis becomes a check; only an
    in_sync axis is green (honesty floor — `unknown` is reported, never passed).
    Gated on the store existing: a missing store is already reported by
    `_check_store`, so we don't double-fail it here."""
    from levain import manifest

    store = install / ".levain" / "memory.db"
    if not store.is_file():
        return []

    anneal_path = shutil.which("anneal-memory") or "anneal-memory"
    declared = manifest.declared_set()
    installed = manifest.discover_installed_set(store, anneal_path)
    lock, lock_status = manifest.read_lock_status(install)
    drift = manifest.compute_drift(declared, installed, lock)

    results: list[CheckResult] = []
    # A CORRUPT lock (the file exists but is unreadable) is an UNKNOWN provenance
    # state worth a loud FAIL; an ABSENT lock is benign (a pre-manifest install),
    # so it gets no check line.
    if lock_status == "corrupt":
        results.append(CheckResult(
            "compat: lock", False,
            "the recorded set (.levain/manifest.json) exists but is unreadable/corrupt",
            "Run `levain update` to re-record a verified compose.",
        ))
    # `pending` (unreviewed migration proposals) and `ahead` (anneal NEWER than
    # this release's known-good — pip allowed it, the install works, and `update`
    # won't downgrade it) are ADVISORIES, not version-SET failures. A fresh
    # `levain init` legitimately has pending proposals, and an operator who runs
    # `pip install -U anneal-memory` within the pin is `ahead` — failing doctor on
    # either would false-alarm a healthy install. Report loudly, green.
    advisory = {"pending", "ahead"}
    for v in drift.verdicts:
        if v.status in advisory:
            results.append(CheckResult(
                f"compat: {v.axis}", True,
                f"{v.detail} — advisory (run `levain update`); not a set failure",
            ))
        else:
            results.append(
                CheckResult(f"compat: {v.axis}", v.status == "in_sync", v.detail, v.hint)
            )
    # Release-gate: the reviewed known-good constant vs the actual pip floor. This
    # is a RELEASE-INTEGRITY check, not an operator-actionable one — a drift
    # (mis-cut wheel: KNOWN_GOOD != the dependency pin) or unknown (unreadable dep
    # metadata) is something the OPERATOR cannot fix, and `doctor`'s exit code
    # composes with their shell pipelines. So surface it loudly but NEVER fail
    # their doctor on it; CI / a release script reads `pip_floor_verdict()` to gate.
    pin = manifest.pip_floor_verdict()
    pin_detail = (
        pin.detail if pin.status == "in_sync"
        else f"{pin.detail} — advisory (release integrity; not operator-actionable)"
    )
    results.append(CheckResult(f"compat: {pin.axis}", True, pin_detail))
    return results


def _python_resolvable(token: str) -> bool:
    """True if `token` names a usable interpreter (absolute file OR on PATH).

    For absolute paths, requires both presence AND executable bit — a
    non-+x interpreter would PermissionError at hook-fire time.
    """
    if not token:
        return False
    p = Path(token)
    if p.is_absolute():
        return p.is_file() and os.access(p, os.X_OK)
    return shutil.which(token) is not None


def _extract_command_python(entries: list[dict]) -> str | None:
    """Extract the python interpreter token from the first hook command."""
    tokens = _first_command_tokens(entries)
    return tokens[0] if tokens else None


def _first_command_tokens(entries: list[dict]) -> list[str] | None:
    """Tokenize the first hook command via shlex; return None on parse error."""
    for entry in entries:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command")
            if not cmd:
                continue
            try:
                return shlex.split(cmd)
            except ValueError:
                return None
    return None


def _hook_command_targets(
    entries: list[dict],
    install: Path,
    expected_script: str,
) -> bool:
    """True iff the first hook command's script-path token resolves to the
    expected script INSIDE this install. Catches substring-prefix false matches
    (`/tmp/levain` vs `/tmp/levain-test`) AND foreign hooks wired under the
    same event but pointing at unrelated scripts.
    """
    tokens = _first_command_tokens(entries)
    if not tokens or len(tokens) < 2:
        return False
    expected = (install / "activation" / "hooks" / expected_script).resolve()
    for tok in tokens[1:]:
        # Token may be a literal path OR contain a harness placeholder like
        # ${CLAUDE_PROJECT_DIR}. Substitute the known placeholder and check.
        candidate = tok.replace("${CLAUDE_PROJECT_DIR}", str(install))
        try:
            if Path(candidate).resolve() == expected:
                return True
        except OSError:
            continue
    return False


def _check_claude_code(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(CheckResult("CLAUDE.md", True, "present"))

    settings_path = install / ".claude" / "settings.json"
    if not settings_path.is_file():
        results.append(
            CheckResult(
                ".claude/settings.json",
                False,
                "missing",
                "Re-run `levain init --adapter claude-code`.",
            )
        )
    else:
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError as e:
            results.append(
                CheckResult(
                    ".claude/settings.json",
                    False,
                    f"invalid JSON: {e}",
                    "Restore from settings.template.json or re-run `levain init`.",
                )
            )
            settings = None

        if settings is not None:
            hooks = settings.get("hooks", {})
            ss = hooks.get("SessionStart", [])
            ups = hooks.get("UserPromptSubmit", [])
            if ss and ups:
                results.append(
                    CheckResult(
                        ".claude/settings.json hooks",
                        True,
                        "SessionStart + UserPromptSubmit configured",
                    )
                )
                ev_scripts = (
                    ("SessionStart", ss, "session_start.py"),
                    ("UserPromptSubmit", ups, "user_prompt_submit.py"),
                )
                for ev_name, entries, script_name in ev_scripts:
                    py = _extract_command_python(entries)
                    if py is None:
                        results.append(
                            CheckResult(
                                f"settings {ev_name} command",
                                False,
                                "could not parse command",
                                "Re-run `levain init`.",
                            )
                        )
                        continue

                    if not _python_resolvable(py):
                        results.append(
                            CheckResult(
                                f"settings {ev_name} python",
                                False,
                                f"unresolvable: {py}",
                                "Re-run `levain init` to re-resolve {{PYTHON}}.",
                            )
                        )
                        continue

                    # New: verify the command actually targets THIS install's
                    # hook script, not just "some script with a working python."
                    if _hook_command_targets(entries, install, script_name):
                        results.append(
                            CheckResult(
                                f"settings {ev_name} → {script_name}",
                                True,
                                f"wired to this install ({py})",
                            )
                        )
                    else:
                        results.append(
                            CheckResult(
                                f"settings {ev_name} → {script_name}",
                                False,
                                "command does not target this install's hook script",
                                "Re-run `levain init` to point hooks at this install.",
                            )
                        )
            else:
                results.append(
                    CheckResult(
                        ".claude/settings.json hooks",
                        False,
                        "SessionStart or UserPromptSubmit missing",
                        "Re-run `levain init`.",
                    )
                )

            allow = settings.get("permissions", {}).get("allow", [])
            # Match the exact server name or `server__tool` form — not any
            # entry that happens to start with the server name as substring.
            if any(
                a == "mcp__anneal_memory" or a.startswith("mcp__anneal_memory__")
                for a in allow
            ):
                results.append(
                    CheckResult(
                        "settings MCP allowlist",
                        True,
                        "mcp__anneal_memory allowed",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "settings MCP allowlist",
                        False,
                        "mcp__anneal_memory not in permissions.allow",
                        "Add 'mcp__anneal_memory' to permissions.allow, or re-run `levain init`.",
                    )
                )

    mcp_path = install / ".mcp.json"
    if not mcp_path.is_file():
        results.append(
            CheckResult(
                ".mcp.json",
                False,
                "missing",
                "Re-run `levain init --adapter claude-code`.",
            )
        )
        return results

    try:
        mcp = json.loads(mcp_path.read_text())
    except json.JSONDecodeError as e:
        results.append(
            CheckResult(
                ".mcp.json",
                False,
                f"invalid JSON: {e}",
                "Restore from mcp.template.json or re-run `levain init`.",
            )
        )
        return results

    server = mcp.get("mcpServers", {}).get("anneal_memory")
    if not server:
        results.append(
            CheckResult(
                ".mcp.json anneal_memory",
                False,
                "server registration missing",
                "Re-run `levain init`.",
            )
        )
    else:
        results.append(_match_store(".mcp.json anneal_memory", install, server.get("args", [])))

    return results


def _check_codex(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(CheckResult("AGENTS.md", True, "present"))

    codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"

    if not hooks_path.is_file():
        results.append(
            CheckResult(
                f"{hooks_path}",
                False,
                "missing",
                "Re-run `levain init --adapter codex`.",
            )
        )
    else:
        try:
            hooks = json.loads(hooks_path.read_text())
        except json.JSONDecodeError as e:
            results.append(
                CheckResult(
                    f"{hooks_path}",
                    False,
                    f"invalid JSON: {e}",
                    "Restore from hooks.json.template or re-run `levain init`.",
                )
            )
            hooks = None

        if hooks is not None:
            hh = hooks.get("hooks", {})
            ss = hh.get("SessionStart", [])
            ups = hh.get("UserPromptSubmit", [])
            ev_scripts = (
                ("SessionStart", ss, "session_start.py"),
                ("UserPromptSubmit", ups, "user_prompt_submit.py"),
            )
            for ev_name, entries, script_name in ev_scripts:
                # Exact-path match — substring-only match would false-positive
                # against ~/levain vs ~/levain-test (prefix overlap).
                if not _hook_command_targets(entries, install, script_name):
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} → {script_name}",
                            False,
                            "not wired to this install (Codex is one-install-per-machine at v1)",
                            "Another install may own ~/.codex/hooks.json. Re-run `levain init` here to take over.",
                        )
                    )
                    continue

                py = _extract_command_python(entries)
                if py and not _python_resolvable(py):
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} python",
                            False,
                            f"unresolvable: {py}",
                            "Re-run `levain init` to re-resolve {{PYTHON}}.",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            f"~/.codex/hooks.json {ev_name} → {script_name}",
                            True,
                            f"wired to this install ({py})" if py else "wired",
                        )
                    )

    if not config_path.is_file():
        results.append(
            CheckResult(
                f"{config_path}",
                False,
                "missing",
                "Re-run `levain init --adapter codex`.",
            )
        )
        return results

    try:
        config = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as e:
        results.append(
            CheckResult(
                f"{config_path}",
                False,
                f"invalid TOML: {e}",
                "Fix the syntax error in ~/.codex/config.toml.",
            )
        )
        return results

    server = config.get("mcp_servers", {}).get("anneal_memory")
    if not server:
        results.append(
            CheckResult(
                "config.toml [mcp_servers.anneal_memory]",
                False,
                "missing",
                "Re-run `levain init --adapter codex`.",
            )
        )
    else:
        results.append(
            _match_store("config.toml [mcp_servers.anneal_memory]", install, server.get("args", []))
        )

    return results


def _match_store(name: str, install: Path, args: list) -> CheckResult:
    """Verify the MCP server args point at this install's anneal-memory store.

    Compares resolved paths so symlinked prefixes (e.g. macOS /tmp -> /private/tmp)
    match correctly. Args shape is `--db <path> serve` (Levain template convention).
    """
    expected = (install / ".levain" / "memory.db").resolve()
    configured: str | None = None
    for i, a in enumerate(args):
        if a == "--db" and i + 1 < len(args):
            configured = args[i + 1]
            break

    if configured is None:
        return CheckResult(
            name,
            False,
            f"no --db arg in registration (args={args})",
            "Re-run `levain init`.",
        )

    try:
        configured_resolved = Path(configured).resolve()
    except OSError:
        configured_resolved = Path(configured)

    if configured_resolved == expected:
        return CheckResult(name, True, f"registered, store={configured}")

    return CheckResult(
        name,
        False,
        f"store path mismatch: {configured} != {expected}",
        f"Re-run `levain init` to point at {expected}.",
    )
