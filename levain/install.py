"""`levain init` orchestrator.

Walks a stranger through standing up a new Levain install:
  1. Pick adapter — Claude Code or Codex (v1 = one adapter per install).
  2. Resolve environment-dependent placeholders.
  3. Run the scripted interview to fill `world.md` + `origin.md`.
  4. Render the templates into the install's `seed/` directory.
  5. Copy the verbatim seed files (`partnership.md`, `memory.md`, the
     continuity scaffold, README).
  6. Lay down the adapter's wiring (settings, MCP registration, hooks).
  7. Initialize the install-pinned anneal-memory store.
  8. Print next-steps banner.

Idempotency: a non-empty install dir is refused unless `--force`. With
`--force`, operator-edited activation files (`posture.md`,
`recency_directives.md`) are backed up with a timestamped suffix before the
activation tree is replaced; the anneal-memory store is preserved as-is;
the Codex global `~/.codex/hooks.json` is backed up before being overwritten.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path


def run_init(path: Path, adapter: str | None, force: bool) -> int:
    # expanduser first: argparse's `type=Path` does not expand `~`, but operators
    # passing `--path ~/levain-install` reasonably expect shell semantics.
    install = Path(str(path)).expanduser().resolve()

    try:
        chosen = _resolve_adapter(adapter)
    except _UserCancelled:
        print("Cancelled.")
        return 1

    install.mkdir(parents=True, exist_ok=True)
    if not _is_safe_install_target(install) and not force:
        print(
            f"FAIL: {install} is not empty.\n"
            f"      Pass --force to install over the existing contents.\n"
            f"      With --force, operator-edited activation files are backed up "
            f"to .bak.<timestamp>; the anneal-memory store is preserved."
        )
        return 1

    python_path = sys.executable
    anneal_path = shutil.which("anneal-memory") or "anneal-memory"

    print()
    print(f"Levain init — installing to {install}")
    print(f"  adapter:    {chosen}")
    print(f"  python:     {python_path}")
    print(f"  anneal:     {anneal_path}")
    print()

    templates_root = _templates_root()
    if not (templates_root / "seed" / "world.md").is_file():
        print(
            f"FAIL: Levain templates not found in installed package at "
            f"{templates_root}. The wheel may be corrupt; reinstall with "
            f"`pip install --force-reinstall levain`."
        )
        return 1

    try:
        from levain.interview import (
            conduct_interview,
            parse_template,
            render_template,
        )
    except Exception as e:
        print(f"FAIL: interview engine unavailable: {e}")
        return 1

    spec_world = parse_template(templates_root / "seed" / "world.md")
    spec_origin = parse_template(templates_root / "seed" / "origin.md")

    print("=" * 60)
    print("Interview — fills the world.md and origin.md templates.")
    print("=" * 60)
    print("  Press Ctrl+C to cancel.")
    try:
        answers = conduct_interview([spec_world, spec_origin])
    except (KeyboardInterrupt, EOFError):
        print("\nInterview interrupted.")
        _report_partial_state(install)
        return 1

    install_seed = install / "seed"
    install_seed.mkdir(parents=True, exist_ok=True)
    (install_seed / "world.md").write_text(
        render_template(spec_world, answers), encoding="utf-8"
    )
    (install_seed / "origin.md").write_text(
        render_template(spec_origin, answers), encoding="utf-8"
    )

    for f in ("partnership.md", "memory.md", "continuity.md", "README.md"):
        src = templates_root / "seed" / f
        if src.is_file():
            shutil.copy2(src, install_seed / f)

    _install_adapter(chosen, install, templates_root, python_path, anneal_path)

    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    store_ok = _init_store(store, anneal_path)

    _print_next_steps(install, chosen, store_ok=store_ok)
    return 0 if store_ok else 1


def _report_partial_state(install: Path) -> None:
    """Tell the operator what exists in the install dir after an interrupt."""
    if not install.is_dir():
        return
    contents = sorted(p.name for p in install.iterdir())
    if not contents:
        print(f"  Install dir {install} is empty — safe to re-run.")
        return
    print(f"  Install dir {install} contains: {', '.join(contents)}")
    print(f"  Re-run `levain init` with --force to overwrite, or delete the dir first.")


class _UserCancelled(Exception):
    pass


def _resolve_adapter(arg: str | None) -> str:
    if arg in ("claude-code", "codex"):
        return arg
    return _prompt_adapter()


def _prompt_adapter() -> str:
    print("Which harness adapter do you want to install?")
    print("  1) Claude Code")
    print("  2) Codex CLI")
    print()
    print("  (v1 installs one adapter per install. To use both harnesses,")
    print("   create two separate installs.)")
    while True:
        try:
            choice = input("Enter 1 or 2: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise _UserCancelled
        if choice in ("1", "claude", "claude-code"):
            return "claude-code"
        if choice in ("2", "codex"):
            return "codex"
        print(f"  Unrecognized: {choice!r}. Try again.")


def _is_safe_install_target(path: Path) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    return not any(path.iterdir())


def _templates_root() -> Path:
    """Returns the package's `templates/` directory containing `seed/`,
    `adapters/`, and `activation/`. Templates ship as package_data in the
    wheel. v1 supports filesystem-installed packages only (`pip install`,
    `pip install -e`, `pip install --target`) — zipapp / PyInstaller /
    other zip-imported distribution shapes would need an `as_file()` context
    manager wrapping the consumers; deferred to v1.1.
    """
    return Path(str(files("levain") / "templates"))


def _install_adapter(
    name: str,
    install: Path,
    templates_root: Path,
    python_path: str,
    anneal_path: str,
) -> None:
    adapter_root = templates_root / "adapters" / name

    if name == "claude-code":
        _install_claude_code(install, templates_root, adapter_root, python_path, anneal_path)
        return

    if name == "codex":
        _install_codex(install, adapter_root, python_path, anneal_path)
        return

    raise ValueError(f"unknown adapter: {name}")  # pragma: no cover


def _install_claude_code(
    install: Path,
    templates_root: Path,
    adapter_root: Path,
    python_path: str,
    anneal_path: str,
) -> None:
    _copy_activation_tree(templates_root / "activation", install / "activation")

    shutil.copy2(adapter_root / "CLAUDE.md.template", install / "CLAUDE.md")

    settings_dir = install / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_text = (adapter_root / "settings.template.json").read_text(encoding="utf-8")
    settings_text = settings_text.replace("{{PYTHON}}", python_path)
    (settings_dir / "settings.json").write_text(settings_text, encoding="utf-8")

    mcp_text = (adapter_root / "mcp.template.json").read_text(encoding="utf-8")
    mcp_text = mcp_text.replace("{{INSTALL_DIR}}", str(install))
    mcp_text = mcp_text.replace("{{ANNEAL_MEMORY}}", anneal_path)
    (install / ".mcp.json").write_text(mcp_text, encoding="utf-8")

    print("  Claude Code adapter installed.")


def _install_codex(
    install: Path,
    adapter_root: Path,
    python_path: str,
    anneal_path: str,
) -> None:
    _copy_activation_tree(adapter_root / "activation", install / "activation")
    shutil.copy2(adapter_root / "AGENTS.md.template", install / "AGENTS.md")

    codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    codex_home.mkdir(parents=True, exist_ok=True)

    hooks_text = (adapter_root / "hooks.json.template").read_text(encoding="utf-8")
    hooks_text = hooks_text.replace("{{PYTHON}}", python_path)
    hooks_text = hooks_text.replace("{{INSTALL_DIR}}", str(install))
    hooks_target = codex_home / "hooks.json"
    if hooks_target.exists() or hooks_target.is_symlink():
        # Timestamped backup so repeated re-runs accrete instead of clobber.
        bak = _timestamped_backup_path(hooks_target)
        # `shutil.copy2` preserves perms/mtime AND is atomic-from-the-reader's-side;
        # `read_text`+`write_text` had a tiny window where Ctrl+C lost the original.
        shutil.copy2(hooks_target, bak)
        # Unlink first (in case it's a symlink into a dotfiles repo) so we don't
        # silently modify the symlink's target.
        hooks_target.unlink()
        print(f"  ! Existing {hooks_target} backed up to {bak}")
        print(f"    (Codex is one-install-per-machine at v1 — this install now owns it.)")
    hooks_target.write_text(hooks_text, encoding="utf-8")

    mcp_fragment = (adapter_root / "mcp.template.toml").read_text(encoding="utf-8")
    mcp_fragment = mcp_fragment.replace("{{ANNEAL_MEMORY}}", anneal_path)
    mcp_fragment = mcp_fragment.replace("{{INSTALL_DIR}}", str(install))
    _merge_codex_config(codex_home / "config.toml", mcp_fragment)

    print("  Codex adapter installed.")


def _timestamped_backup_path(target: Path) -> Path:
    """Return `target.bak.<ns>` — nanosecond granularity to avoid same-second collisions."""
    return target.with_suffix(target.suffix + f".bak.{time.time_ns()}")


_OPERATOR_EDITABLE = ("posture.md", "recency_directives.md")


def _copy_activation_tree(src: Path, dst: Path) -> None:
    """Copy `src` -> `dst`, but preserve operator edits to known editable files.

    `posture.md` and `recency_directives.md` are documented as operator-editable
    (the "second sourdough surface" — the activation block accretes as the
    operator finds their own RLHF-leakage patterns). If those exist at `dst`
    and differ from the template at `src`, back them up OUTSIDE the dst tree
    so the `rmtree(dst)` below doesn't immediately destroy the backup. The
    backups land at `<install>/.levain/backups/activation/<timestamp>/` so the
    operator can find them via the documented backup convention.
    """
    backups: list[tuple[Path, Path]] = []
    if dst.exists():
        # `dst` is `<install>/activation/`; parent is the install root.
        # Stage backups outside `dst` so `rmtree(dst)` doesn't consume them.
        backup_dir = dst.parent / ".levain" / "backups" / "activation" / str(time.time_ns())
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            backup_dir = None  # backups disabled if we can't stage

        if backup_dir is not None:
            for name in _OPERATOR_EDITABLE:
                current = dst / name
                template = src / name
                if not current.is_file() or not template.is_file():
                    continue
                try:
                    if current.read_bytes() == template.read_bytes():
                        continue
                except OSError:
                    continue
                bak = backup_dir / name
                try:
                    shutil.copy2(current, bak)
                    backups.append((current, bak))
                except OSError:
                    continue

        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for current, bak in backups:
        print(f"  ! Operator-edited {current.name} preserved at {bak}")


# Match the `[mcp_servers.anneal_memory]` table — from its header to the next
# table header at line start, or EOF. Previous shape was `[^\[]*` which broke
# on TOML inline arrays (`args = ["--db", ...]`) — the `[` opening the array
# was consumed as a section delimiter, truncating the match. Caught by L3
# cross-substrate review (complement + codex convergent — bug-class the
# `cross_substrate_review_codex` Proven primitive exists for).
_CODEX_MCP_BLOCK_RE = re.compile(
    r"(?ms)^\[mcp_servers\.anneal_memory\][^\n]*\n(?:(?!^\[)[^\n]*\n?)*",
)


def _merge_codex_config(path: Path, fragment: str) -> None:
    """Insert/replace the `[mcp_servers.anneal_memory]` block in config.toml.

    Idempotent — re-running init replaces the block in place rather than
    appending a duplicate section header (which TOML-parse-fails on next
    Codex startup).
    """
    if not path.is_file():
        path.write_text(fragment.rstrip() + "\n", encoding="utf-8")
        return

    existing = path.read_text(encoding="utf-8")
    new_block_match = _CODEX_MCP_BLOCK_RE.search(fragment)
    if not new_block_match:
        return

    new_block = new_block_match.group(0).rstrip() + "\n"

    if _CODEX_MCP_BLOCK_RE.search(existing):
        existing = _CODEX_MCP_BLOCK_RE.sub(new_block, existing, count=1)
    else:
        if not existing.endswith("\n"):
            existing += "\n"
        if not existing.endswith("\n\n"):
            existing += "\n"
        existing += new_block

    path.write_text(existing, encoding="utf-8")


def _init_store(store: Path, anneal_path: str) -> bool:
    """Initialize the anneal-memory store. Return True on success."""
    print()
    if store.is_file() and store.stat().st_size > 0:
        # An existing store carries the entity's memory + identity; --force
        # overlays seed/adapter files but never touches the memory store.
        print(f"anneal-memory store already present at {store} — preserved.")
        return True

    print(f"Initializing anneal-memory store at {store}...")

    candidates = [
        [anneal_path, "--db", str(store), "init"],
        [sys.executable, "-m", "anneal_memory", "--db", str(store), "init"],
    ]
    errors: list[str] = []
    for cmd in candidates:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError as e:
            errors.append(f"{cmd[0]}: {e}")
            continue
        except subprocess.TimeoutExpired:
            errors.append(f"{cmd[0]}: timed out")
            continue
        if result.returncode == 0:
            print("  Store initialized.")
            return True
        err = (result.stderr or result.stdout).strip()[:500]
        errors.append(f"{cmd[0]}: {err}")

    print("  ! Could not initialize store:")
    for e in errors:
        print(f"    - {e}")
    print(f"    Most likely cause: anneal-memory is not installed in this Python.")
    print(f"    Fix: pip install anneal-memory  (or `pip install --user anneal-memory`)")
    print(f"    Then: anneal-memory --db {store} init")
    return False


def _print_next_steps(install: Path, adapter: str, store_ok: bool = True) -> None:
    print()
    print("=" * 60)
    if store_ok:
        print("Install complete.")
    else:
        print("Install PARTIAL — files laid down, store init FAILED. See above.")
    print("=" * 60)
    print(f"  Install:   {install}")
    print(f"  Adapter:   {adapter}")
    print()
    print("Next steps:")
    if adapter == "claude-code":
        print(f"  - Open in Claude Code:")
        print(f"        cd {install} && claude")
    if adapter == "codex":
        print(f"  - IMPORTANT — Codex hook trust is per-content-hash. The very")
        print(f"    first invocation MUST be interactive `codex` (not `codex exec`)")
        print(f"    so Codex can prompt to trust the hook scripts. Editing the")
        print(f"    hook scripts invalidates trust until re-approved interactively.")
        print(f"        cd {install} && codex")
    print(f"  - Verify the install (loud):  levain doctor --path {install}")
    print(f"  - Smoke-test the hooks:       levain verify-hooks --path {install}")
    print(f"  - (`doctor` static-checks wiring; `verify-hooks` actually invokes the")
    print(f"     hook scripts. The harness still has to invoke them — verify in an")
    print(f"     interactive session.)")
    print()
