"""`levain init` orchestrator.

Walks a stranger through standing up a new Levain install:
  1. Pick adapter — Claude Code or Codex (v1 = one adapter per install).
  2. Resolve environment-dependent placeholders.
  3. Run the scripted interview to fill `world.md` + `origin.md`.
  4. Render the templates into the install's `seed/` directory.
  5. Copy the verbatim seed files (`partnership.md`, `memory.md`,
     `spore_instructions.md`, the continuity scaffold, README).
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

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from levain.interview import TemplateSpec


def run_init(path: Path, adapter: str | None, force: bool) -> int:
    # expanduser first: argparse's `type=Path` does not expand `~`, but operators
    # passing `--path ~/levain-install` reasonably expect shell semantics.
    install = Path(str(path)).expanduser().resolve()

    try:
        chosen = _resolve_adapter(adapter)
    except _UserCancelled:
        print("Cancelled.")
        return 1

    if install.exists() and not install.is_dir():
        print(
            f"FAIL: {install} exists but is not a directory.\n"
            f"      Pass --path pointing at a directory (or a non-existent path)."
        )
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

    # All template reads + `_install_adapter` (which copies the activation
    # tree) must stay inside this `with` block. Under zipped distributions
    # (zipapp / PyInstaller / pip --target into zip), `as_file()` materializes
    # templates to a tempdir that's cleaned up on context exit. Code outside
    # this block must NOT depend on `templates_root` being live.
    with _templates_root() as templates_root:
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
                # Imported here only to validate the interview engine UPFRONT —
                # a corrupt/partial module fails cleanly before the interview
                # rather than mid-install. apply_init re-imports it where used.
                render_template,  # noqa: F401
            )
        except Exception as e:
            print(f"FAIL: interview engine unavailable: {e}")
            return 1

        spec_world = parse_template(templates_root / "seed" / "world.md")
        spec_origin = parse_template(templates_root / "seed" / "origin.md")

        print("=" * 60)
        print("Interview — fills the world.md and origin.md templates.")
        print("=" * 60)

        # Resume from prior Ctrl+C if a checkpoint exists.
        initial_answers: dict[str, str] = {}
        checkpoint = _load_checkpoint(install)
        if checkpoint:
            n = len(checkpoint)
            try:
                response = input(
                    f"  Found interview checkpoint with {n} answer(s) from "
                    f"a prior interrupted run.\n"
                    f"  Resume from checkpoint? [Y/n] "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Cancelled before resume decision.")
                return 1
            if response in ("", "y", "yes"):
                initial_answers = checkpoint
                print(f"  Resuming — {n} answer(s) restored. Continuing where you left off.")
            else:
                _clear_checkpoint(install)
                print("  Discarded checkpoint. Starting fresh.")

        print("  Press Ctrl+C to interrupt — answers so far will be saved for resume.")
        try:
            answers = conduct_interview(
                [spec_world, spec_origin],
                answers=initial_answers,
                checkpoint_fn=lambda a: _save_checkpoint(install, a),
            )
        except (KeyboardInterrupt, EOFError):
            print("\nInterview interrupted.")
            _report_partial_state(install)
            return 1

        store_ok = apply_init(
            install,
            chosen,
            answers,
            templates_root,
            python_path,
            anneal_path,
            [spec_world, spec_origin],
        )

    # Interview completed successfully — clear the checkpoint so the next
    # `levain init --force` doesn't offer to resume stale answers.
    _clear_checkpoint(install)

    store = install / ".levain" / "memory.db"
    _print_manifest(install, chosen, store, store_ok=store_ok)
    _print_next_steps(install, chosen, store_ok=store_ok)
    return 0 if store_ok else 1


def apply_init(
    install: Path,
    chosen: str,
    answers: dict[str, str],
    templates_root: Path,
    python_path: str,
    anneal_path: str,
    specs: list[TemplateSpec],
) -> bool:
    """The shared WRITE-HALF of init: render each interview template from
    `answers`, copy the verbatim seed files, install the adapter, and initialize
    the store. Returns the store-init success flag.

    Called by BOTH `run_init` (the CLI, after the terminal interview) and the
    web init POST (after the form submit) so the two surfaces perform the WRITE
    steps IDENTICALLY from an already-resolved `answers` map. The shared surface
    is the writes ONLY — each caller resolves its OWN inputs first (install path,
    adapter choice, python/anneal paths, the template preflight, and
    `parse_template`-ing the specs); that resolution half stays per-surface.
    apply_init does NOT validate `answers` completeness — `render_template`
    substitutes a missing slot with `""`, so the CALLER must ensure `answers`
    covers the slots (the CLI via `conduct_interview`; the web via a form driven
    from `build_field_plan`).

    `specs` is the list of templates to RENDER (world.md + origin.md today);
    each is written to `install/seed/<spec.path.name>`. The verbatim
    (non-rendered) seed files are copied separately below.

    MUST be called inside a live `_templates_root()` context: the render/copy/
    adapter steps read from `templates_root`, which a zipped distribution
    materializes only for that context's lifetime. The store-init is
    `templates_root`-independent but folded in so one call IS the whole write
    sequence — it now runs inside the context (vs. just after it in the old
    inline form), which is benign because `_init_store` never reads
    `templates_root`.
    """
    from levain.interview import render_template

    install_seed = install / "seed"
    install_seed.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        (install_seed / spec.path.name).write_text(
            render_template(spec, answers), encoding="utf-8"
        )

    for f in (
        "partnership.md",
        "memory.md",
        "spore_instructions.md",
        "continuity.md",
        "README.md",
    ):
        src = templates_root / "seed" / f
        if src.is_file():
            shutil.copy2(src, install_seed / f)

    _install_adapter(chosen, install, templates_root, python_path, anneal_path)

    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    return _init_store(store, anneal_path)


# ---------- interview checkpoint persistence ----------

def _checkpoint_path(install: Path) -> Path:
    """Where the interview-resume checkpoint lives. Co-located with the
    memory store so the whole `.levain/` dir is the operator-state surface."""
    return install / ".levain" / "interview-checkpoint.json"


def _save_checkpoint(install: Path, answers: dict[str, str]) -> None:
    """Write `answers` atomically to the checkpoint file. Best-effort —
    silent on filesystem errors (the interview must not fail because the
    checkpoint can't be written)."""
    target = _checkpoint_path(install)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: temp file + rename so a Ctrl+C mid-write
        # doesn't leave a half-truncated checkpoint.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(answers, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass


def _load_checkpoint(install: Path) -> dict[str, str] | None:
    """Return checkpoint answers if a valid checkpoint exists, else None.
    Silently treats corrupt/unreadable checkpoints as no-checkpoint."""
    target = _checkpoint_path(install)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Defend against non-string entries (corrupted or hand-edited).
    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def _clear_checkpoint(install: Path) -> None:
    """Delete the checkpoint file. No-op if absent."""
    target = _checkpoint_path(install)
    try:
        target.unlink()
    except (FileNotFoundError, OSError):
        pass


def _report_partial_state(install: Path) -> None:
    """Tell the operator what exists in the install dir after an interrupt."""
    if not install.is_dir():
        return
    contents = sorted(p.name for p in install.iterdir())
    checkpoint = _load_checkpoint(install)
    if checkpoint:
        print(
            f"  Interview checkpoint saved with {len(checkpoint)} answer(s). "
            f"Re-run `levain init --path {install} --force` to resume."
        )
    if not contents:
        print(f"  Install dir {install} is empty — safe to re-run.")
        return
    print(f"  Install dir {install} contains: {', '.join(contents)}")
    if not checkpoint:
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


@contextmanager
def _templates_root() -> Iterator[Path]:
    """Yields the package's `templates/` directory as a filesystem Path.

    Uses `importlib.resources.as_file` so the path is real and usable with
    `shutil.copytree`, `shutil.copy2`, `Path.read_text`, etc. For filesystem
    distributions (the normal case for `pip install`), yields the real
    package path with no copy. For zipped distributions (zipapp,
    PyInstaller, `pip install --target` into a zip), files are materialized
    to a tempdir for the duration of the `with` block.

    Callers MUST consume `templates_root` inside the `with` block — the
    materialized tempdir is cleaned up on exit under zipped distributions.

    Requires Python >=3.12 — directory-resource support for `as_file()`
    arrived in 3.12 (https://docs.python.org/3/library/importlib.resources.html).
    The package's `requires-python` floor matches.

    Namespace-package installs (a `levain` package split across multiple
    directories) will return a `MultiplexedPath` from `files()`, which
    `as_file()` cannot materialize as a directory. Not supported at v1.
    """
    with as_file(files("levain") / "templates") as path:
        yield Path(path)


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
    _copy_activation_tree(
        templates_root / "activation",
        install / "activation",
        anneal_path=anneal_path,
    )

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
    _copy_activation_tree(
        adapter_root / "activation",
        install / "activation",
        anneal_path=anneal_path,
    )
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


def _copy_activation_tree(src: Path, dst: Path, anneal_path: str | None = None) -> None:
    """Copy `src` -> `dst`, but preserve operator edits to known editable files.

    `posture.md` and `recency_directives.md` are documented as operator-editable
    (the "second sourdough surface" — the activation block accretes as the
    operator finds their own RLHF-leakage patterns). If those exist at `dst`
    and differ from the template at `src`, back them up OUTSIDE the dst tree
    so the `rmtree(dst)` below doesn't immediately destroy the backup. The
    backups land at `<install>/.levain/backups/activation/<timestamp>/` so the
    operator can find them via the documented backup convention.

    `anneal_path`, when provided, is substituted into the `{{ANNEAL_MEMORY}}`
    placeholder in any hook .py file under `dst/hooks/`. The hooks use the
    substituted absolute path as their first CLI candidate so hook firing
    doesn't depend on PATH (which Claude Code + Codex sanitize aggressively).
    """
    backups: list[tuple[Path, Path]] = []
    if dst.exists():
        # `dst` is `<install>/activation/`; parent is the install root.
        # Stage backups outside `dst` so `rmtree(dst)` doesn't consume them.
        staging = dst.parent / ".levain" / "backups" / "activation" / str(time.time_ns())
        backup_dir: Path | None
        try:
            staging.mkdir(parents=True, exist_ok=True)
            backup_dir = staging
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
    if anneal_path is not None:
        _substitute_hook_placeholders(dst / "hooks", {"{{ANNEAL_MEMORY}}": anneal_path})
    for current, bak in backups:
        print(f"  ! Operator-edited {current.name} preserved at {bak}")


def _substitute_hook_placeholders(hooks_dir: Path, mapping: dict[str, str]) -> None:
    """Replace install-time placeholders in every .py file under `hooks_dir`.

    Hooks ship with `{{ANNEAL_MEMORY}}` (and potentially more keys later) so
    they can use the install-time-resolved absolute path of anneal-memory
    without depending on PATH at fire time. The substitution is a simple
    string replace — placeholder is unique enough that false positives are
    not a real risk.
    """
    if not hooks_dir.is_dir():
        return
    for py_file in hooks_dir.glob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = text
        for placeholder, value in mapping.items():
            new_text = new_text.replace(placeholder, value)
        if new_text != text:
            try:
                py_file.write_text(new_text, encoding="utf-8")
            except OSError:
                continue


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


def _run_anneal_cmd(
    store: Path, anneal_path: str, sub_args: list[str]
) -> tuple[bool, str, list[str]]:
    """Run an anneal-memory subcommand against ``store``, trying the console
    script first then ``python -m anneal_memory``. Returns ``(ok, stdout,
    errors)`` — ``ok`` True on the first candidate that exits 0 (with its
    stdout), else False with the collected per-candidate error strings.
    """
    candidates = [
        [anneal_path, "--db", str(store), *sub_args],
        [sys.executable, "-m", "anneal_memory", "--db", str(store), *sub_args],
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
            return True, result.stdout, errors
        errors.append(f"{cmd[0]}: {(result.stderr or result.stdout).strip()[:500]}")
    return False, "", errors


def _store_schema_name(store: Path, anneal_path: str) -> str | None:
    """Best-effort read of the store's persisted schema name via `status --json`.
    Returns the name (e.g. ``"partnership"`` / ``"default"``), or ``None`` if it
    can't be determined (anneal too old to report it, or a parse/read failure)."""
    ok, out, _errors = _run_anneal_cmd(store, anneal_path, ["status", "--json"])
    if not ok:
        return None
    try:
        name = json.loads(out).get("schema")
    except (ValueError, TypeError):
        return None
    return name if isinstance(name, str) else None


def _init_store(store: Path, anneal_path: str) -> bool:
    """Initialize (or schema-migrate) the anneal-memory store. Return True on success."""
    print()
    if store.is_file() and store.stat().st_size > 0:
        # An existing store carries the entity's memory + identity; --force
        # overlays seed/adapter files but never touches the memory CONTENT. We DO
        # ensure its section schema is partnership: a store created on the old ops
        # schema, re-installed under a partnership seed, would otherwise be a
        # silently-ops partnership entity — the exact invariant this kit protects.
        # Preflight the schema first and migrate ONLY when needed: skipping a
        # redundant `set-schema` on an already-partnership store avoids its audit
        # entry AND the wrap-guard edge (set-schema refuses mid-wrap, before any
        # same-schema short-circuit — so a no-op migrate could spuriously fail).
        # The migration itself (`set-schema`) preserves memory content (episodes,
        # wraps, continuity text); it rewrites the schema metadata row and records
        # a `section_schema_set` audit event.
        print(f"anneal-memory store already present at {store} — memory preserved.")
        if _store_schema_name(store, anneal_path) == "partnership":
            print("  Section schema already partnership — nothing to migrate.")
            return True
        ok, _out, errors = _run_anneal_cmd(store, anneal_path, ["set-schema", "partnership"])
        if ok:
            print("  Section schema migrated to partnership (memory content preserved).")
            return True
        print("  ! Could not ensure the partnership schema on the existing store:")
        for e in errors:
            print(f"    - {e}")
        print(f"    The memory is preserved, but the schema may still be the ops")
        print(f"    default — a partnership entity needs the 6-section schema.")
        print(f"    Fix: pip install -U anneal-memory")
        print(f"    Then: anneal-memory --db {store} set-schema partnership")
        return False

    print(f"Initializing anneal-memory store at {store}...")

    # Persist the 6-section partnership schema at creation (anneal AM-INITSCHEMA).
    # This is the only point the felt-layer proportion-gate + schema-aware budget
    # get switched on — a store left on the default silently runs the 4-section
    # ops schema. We fail loud (below) rather than fall back to a default init,
    # because a silently-ops partnership entity is exactly the failure to prevent.
    ok, _out, errors = _run_anneal_cmd(store, anneal_path, ["init", "--schema", "partnership"])
    if ok:
        print("  Store initialized (partnership schema).")
        return True

    print("  ! Could not initialize store:")
    for e in errors:
        print(f"    - {e}")
    print(f"    Most likely cause: anneal-memory is not installed in this Python,")
    print(f"    or is older than the release that supports `init --schema` /")
    print(f"    `set-schema` (the 6-section partnership schema).")
    print(f"    Fix: pip install -U anneal-memory")
    print(f"    Then: anneal-memory --db {store} init --schema partnership")
    return False


def _print_manifest(
    install: Path, adapter: str, store: Path, store_ok: bool = True
) -> None:
    """List every file the install laid down — so the operator knows what
    landed, that they can hand-edit it, and where to look before first launch.

    Built from the known install layout (the orchestrator controls exactly
    what gets written), filtered to paths that actually exist so the
    conditional seed copies and a failed store init drop out cleanly. Includes
    the Codex global files (`hooks.json` / `config.toml`) since they live
    outside the install dir but ARE created/modified by a codex install.
    """
    print()
    print("Files created (you can hand-edit any of these):")

    rows: list[tuple[str, Path]] = []

    seed = install / "seed"
    for name in (
        "world.md",
        "origin.md",
        "partnership.md",
        "memory.md",
        "spore_instructions.md",
        "continuity.md",
        "README.md",
    ):
        rows.append(("seed", seed / name))

    if adapter == "claude-code":
        rows.append(("adapter", install / "CLAUDE.md"))
        rows.append(("adapter", install / ".claude" / "settings.json"))
        rows.append(("adapter", install / ".mcp.json"))
    elif adapter == "codex":
        rows.append(("adapter", install / "AGENTS.md"))
        codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
        rows.append(("codex (global)", codex_home / "hooks.json"))
        rows.append(("codex (global)", codex_home / "config.toml"))

    activation = install / "activation"
    if activation.is_dir():
        for f in sorted(activation.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                rows.append(("activation", f))

    if store_ok and store.exists():
        rows.append(("store", store))

    present = [(label, path) for label, path in rows if path.exists()]
    width = max((len(label) for label, _ in present), default=0)
    for label, path in present:
        print(f"  {label:<{width}}  {path}")

    if activation.is_dir():
        print()
        print(
            "  (activation/posture.md + activation/recency_directives.md are yours "
            "to tune as you find your own RLHF-leakage patterns.)"
        )


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
