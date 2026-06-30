"""`levain init` orchestrator.

Walks a stranger through standing up a new Levain install:
  1. Pick adapter — Claude Code or Codex (v1 = one adapter per install).
  2. Resolve environment-dependent placeholders.
  3. Resolve the seed roster from the pack manifest (which seed files the
     interview RENDERS vs are copied VERBATIM — see `levain/packs.py`).
  4. Run the scripted interview to fill the render templates (`world.md` +
     `origin.md` in the base pack) and render them into `seed/`.
  5. Copy the verbatim seed files byte-exact (`partnership.md`, `memory.md`,
     `spore_instructions.md`, the continuity scaffold, README in the base pack).
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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING

from levain.packs import (
    PackError,
    SeedEntry,
    compose_roster,
    render_entries,
    verbatim_entries,
)

if TYPE_CHECKING:
    from levain.interview import TemplateSpec


@dataclass
class InitResult:
    """The structured outcome of `apply_init` — the write-half's full status,
    not just the store flag (the bare bool was only step-d). Self-describing so a
    caller can render the manifest + next-steps without re-threading the inputs:
    a web init POST returns this plus the captured `emit` transcript + the pure
    `_manifest_rows` / `_next_steps_lines` projections."""

    install: Path
    adapter: str
    store_ok: bool

    @property
    def complete(self) -> bool:
        """True iff the whole install succeeded. Today the store is the only
        soft-failure step (seed/adapter writes raise on hard failure), so this
        equals `store_ok`; a future per-step status would widen here, not at the
        callsites."""
        return self.store_ok


def run_init(
    path: Path, adapter: str | None, force: bool, packs: list[Path] | None = None
) -> int:
    # expanduser first: argparse's `type=Path` does not expand `~`, but operators
    # passing `--path ~/levain-install` reasonably expect shell semantics.
    install = Path(str(path)).expanduser().resolve()
    # Pack-layers compose ON TOP of the base templates (base = pack #0). Resolved
    # the same way as --path; a bad pack dir / missing pack.toml fails loud below.
    pack_dirs = [Path(str(p)).expanduser().resolve() for p in (packs or [])]

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

        try:
            roster = compose_roster([templates_root, *pack_dirs])
        except PackError as e:
            print(f"FAIL: {e}")
            return 1
        render_specs = [parse_template(entry.path) for entry in render_entries(roster)]
        verbatim = verbatim_entries(roster)

        rendered_names = ", ".join(s.path.name for s in render_specs)
        print("=" * 60)
        print(f"Interview — fills the {rendered_names} templates.")
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
                render_specs,
                answers=initial_answers,
                checkpoint_fn=lambda a: _save_checkpoint(install, a),
            )
        except (KeyboardInterrupt, EOFError):
            print("\nInterview interrupted.")
            _report_partial_state(install)
            return 1

        result = apply_init(
            install,
            chosen,
            answers,
            templates_root,
            python_path,
            anneal_path,
            render_specs,
            verbatim,
        )

    # Interview completed successfully — clear the checkpoint so the next
    # `levain init --force` doesn't offer to resume stale answers.
    _clear_checkpoint(install)

    store = install / ".levain" / "memory.db"
    _print_manifest(install, chosen, store, store_ok=result.store_ok)
    _print_next_steps(install, chosen, store_ok=result.store_ok)
    return 0 if result.store_ok else 1


def apply_init(
    install: Path,
    chosen: str,
    answers: dict[str, str],
    templates_root: Path,
    python_path: str,
    anneal_path: str,
    specs: list[TemplateSpec],
    verbatim: Sequence[SeedEntry],
    emit: Callable[[str], None] = print,
) -> InitResult:
    """The shared WRITE-HALF of init: render each interview template from
    `answers`, copy the verbatim seed files (each from its winning layer's source
    path), install the adapter, and initialize the store. Returns an `InitResult`
    carrying the store-init success flag (plus the install/adapter, so the result
    is self-describing).

    `emit` is the progress/remediation sink threaded through the write steps
    (adapter-install notices, backup warnings, store-init remediation). It
    defaults to `print` so the CLI is byte-unchanged; the web init POST passes a
    capturing sink (e.g. `lines.append`) so install progress + failure
    remediation reach the BROWSER, not just the server console.

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

    `specs` is the list of templates to RENDER (the roster's render entries,
    parsed by the caller); each is written to `install/seed/<spec.path.name>`.
    `verbatim` is the list of non-rendered seed ENTRIES (name + source path) to
    copy byte-exact — each copied from its own `entry.path`, so a layered pack's
    verbatim file copies from the pack, not a reconstructed base path. Both derive
    from one `packs.compose_roster` call in the caller, so render and verbatim are
    the same partition the interview/form was built from.

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

    for entry in verbatim:
        # No is_file() guard: a vanished source (TOCTOU) is a hard failure that
        # must surface, not silently yield an install missing a seed file.
        shutil.copy2(entry.path, install_seed / entry.name)

    _install_adapter(chosen, install, templates_root, python_path, anneal_path, emit=emit)

    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    store_ok = _init_store(store, anneal_path, emit=emit)
    if store_ok:
        _record_compat_lock(install, store, anneal_path, emit=emit)
    return InitResult(install=install, adapter=chosen, store_ok=store_ok)


def _record_compat_lock(
    install: Path, store: Path, anneal_path: str, emit: Callable[[str], None] = print
) -> None:
    """Record the composed known-good set to ``.levain/manifest.json`` (the drift
    baseline) AND ack a fresh install's migrate marker up to the version the seed
    templates are reconciled against.

    Auto-acking is honest BECAUSE the seed templates incorporate every anneal
    migration-manifest entry through ``manifest.TEMPLATES_RECONCILED_ANNEAL`` (a
    reviewed release-checklist assertion, test-locked) — so a freshly-rendered,
    current adopter genuinely has nothing to review through that version, and
    acking it does NOT suppress a real proposal. (This is the spore-216 reconcile
    of the spore-213 default: before the templates carried the migrate-notify /
    crystal / linkgate guidance — and before the activation hook actually fired
    per-turn crystal recall — NOT acking was the honest call; now that they do,
    acking to the reconciled version is — and it gives a fresh adopter a clean
    `doctor` instead of a wall of already-incorporated proposals.) The ack is
    ADVANCE-ONLY
    (never lowers a `--force` re-install's existing higher ack) and CAPPED at the
    installed anneal; a newer anneal feature past the reconciled version still
    surfaces.

    Best-effort: a failure here never fails the install (the store is already up).
    """
    from levain import manifest

    declared = manifest.declared_set()
    installed = manifest.discover_installed_set(store, anneal_path)
    # Honesty floor: if discovery failed, do NOT record a lock from declared
    # fallbacks — a poisoned baseline reads as a VERIFIED compose next session.
    # (The same fix update._record_lock carries; codex L3 caught that the init
    # path had the old fall-back-to-declared polarity while update did not.)
    if installed.anneal is None or installed.schema is None:
        emit("  Could not verify the installed set (anneal/schema unread) — "
             "compatibility lock not recorded.")
        return
    composed = manifest.CompatSet(
        levain=declared.levain,
        anneal=installed.anneal,
        schema=installed.schema,
    )
    try:
        manifest.write_lock(install, composed)
        emit(f"  Recorded the known-good set (anneal {composed.anneal} / "
             f"schema {composed.schema}).")
    except OSError:
        pass

    # Ack a fresh install's migrate marker to the version the seed templates are
    # reconciled against (capped at installed) — so a freshly-rendered, CURRENT
    # adopter sees no false "pending drift". This is honest because the templates
    # genuinely incorporate every migration entry through that version (the
    # reviewed TEMPLATES_RECONCILED_ANNEAL assertion); it never suppresses a
    # proposal the templates don't cover. ONLY ADVANCE the marker: a `--force`
    # re-install over an existing store may already be acked further, and acking
    # lower would needlessly re-surface already-reviewed proposals. Best-effort —
    # never fails the install (the store is already up).
    ack_target = manifest.template_ack_target(installed.anneal)
    if ack_target is not None and (
        installed.migrate_acked is None
        or manifest._cmp(installed.migrate_acked, ack_target) < 0
    ):
        ok, _out, _errs = _run_anneal_cmd(
            store, anneal_path, ["migrate", "ack", ack_target]
        )
        if ok:
            emit(f"  Methodology baseline set (migrations through {ack_target} — "
                 f"your seed is current as of this release).")


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
        print("  Re-run `levain init` with --force to overwrite, or delete the dir first.")


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


class InitError(Exception):
    """A user-facing init failure with a ready-to-show message — raised by the
    shared template wrapper when the packaged seed templates are missing/corrupt.
    The web init POST maps it to an HTTP error carrying `.message`; the CLI's own
    inline preflight keeps its own print path."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@contextmanager
def open_init_templates() -> Iterator[tuple[Path, list[TemplateSpec], list[SeedEntry]]]:
    """Yield ``(templates_root, render_specs, verbatim)`` for an init run.

    The shared templates-context + corruption preflight + roster resolution, so a
    SECOND init surface (the web POST) doesn't re-implement the ``with
    _templates_root()`` block, the missing-template check, and the roster
    discovery that ``run_init`` does inline. ``render_specs`` are the parsed
    interview templates; ``verbatim`` is the list of non-rendered seed ENTRIES
    ``apply_init`` copies. The web onboarding path is base-only (no pack layers);
    pack composition is a CLI capability. MUST be consumed inside the ``with``
    (the materialized tempdir under a zipped distribution is cleaned up on exit,
    and ``apply_init`` reads from ``templates_root``). Raises ``InitError`` (with
    a ready-to-surface ``.message``) if the packaged templates are missing/corrupt.
    """
    from levain.interview import parse_template

    with _templates_root() as templates_root:
        if not (templates_root / "seed" / "world.md").is_file():
            raise InitError(
                f"Levain templates not found in the installed package at "
                f"{templates_root}. The wheel may be corrupt; reinstall with "
                f"`pip install --force-reinstall levain`."
            )
        try:
            roster = compose_roster([templates_root])
        except PackError as e:
            raise InitError(str(e)) from e
        specs = [parse_template(entry.path) for entry in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        yield templates_root, specs, verbatim


def _install_adapter(
    name: str,
    install: Path,
    templates_root: Path,
    python_path: str,
    anneal_path: str,
    emit: Callable[[str], None] = print,
) -> None:
    adapter_root = templates_root / "adapters" / name

    if name == "claude-code":
        _install_claude_code(install, templates_root, adapter_root, python_path, anneal_path, emit=emit)
        return

    if name == "codex":
        _install_codex(install, adapter_root, python_path, anneal_path, emit=emit)
        return

    raise ValueError(f"unknown adapter: {name}")  # pragma: no cover


def _install_claude_code(
    install: Path,
    templates_root: Path,
    adapter_root: Path,
    python_path: str,
    anneal_path: str,
    emit: Callable[[str], None] = print,
) -> None:
    _copy_activation_tree(
        templates_root / "activation",
        install / "activation",
        anneal_path=anneal_path,
        emit=emit,
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

    emit("  Claude Code adapter installed.")


def _install_codex(
    install: Path,
    adapter_root: Path,
    python_path: str,
    anneal_path: str,
    emit: Callable[[str], None] = print,
) -> None:
    _copy_activation_tree(
        adapter_root / "activation",
        install / "activation",
        anneal_path=anneal_path,
        emit=emit,
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
        emit(f"  ! Existing {hooks_target} backed up to {bak}")
        emit("    (Codex is one-install-per-machine at v1 — this install now owns it.)")
    hooks_target.write_text(hooks_text, encoding="utf-8")

    mcp_fragment = (adapter_root / "mcp.template.toml").read_text(encoding="utf-8")
    mcp_fragment = mcp_fragment.replace("{{ANNEAL_MEMORY}}", anneal_path)
    mcp_fragment = mcp_fragment.replace("{{INSTALL_DIR}}", str(install))
    _merge_codex_config(codex_home / "config.toml", mcp_fragment)

    emit("  Codex adapter installed.")


def _timestamped_backup_path(target: Path) -> Path:
    """Return `target.bak.<ns>` — nanosecond granularity to avoid same-second collisions."""
    return target.with_suffix(target.suffix + f".bak.{time.time_ns()}")


_OPERATOR_EDITABLE = ("posture.md", "recency_directives.md")


def _copy_activation_tree(
    src: Path,
    dst: Path,
    anneal_path: str | None = None,
    emit: Callable[[str], None] = print,
) -> None:
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
        emit(f"  ! Operator-edited {current.name} preserved at {bak}")


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


def _init_store(
    store: Path, anneal_path: str, emit: Callable[[str], None] = print
) -> bool:
    """Initialize (or schema-migrate) the anneal-memory store. Return True on success.

    `emit` (default `print`) sinks the progress + failure-remediation lines so a
    web init can surface them in the browser; the CLI is byte-unchanged."""
    emit("")
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
        emit(f"anneal-memory store already present at {store} — memory preserved.")
        if _store_schema_name(store, anneal_path) == "partnership":
            emit("  Section schema already partnership — nothing to migrate.")
            return True
        ok, _out, errors = _run_anneal_cmd(store, anneal_path, ["set-schema", "partnership"])
        if ok:
            emit("  Section schema migrated to partnership (memory content preserved).")
            return True
        emit("  ! Could not ensure the partnership schema on the existing store:")
        for e in errors:
            emit(f"    - {e}")
        emit("    The memory is preserved, but the schema may still be the ops")
        emit("    default — a partnership entity needs the 6-section schema.")
        emit("    Fix: pip install -U anneal-memory")
        emit(f"    Then: anneal-memory --db {store} set-schema partnership")
        return False

    emit(f"Initializing anneal-memory store at {store}...")

    # Persist the 6-section partnership schema at creation (anneal AM-INITSCHEMA).
    # This is the only point the felt-layer proportion-gate + schema-aware budget
    # get switched on — a store left on the default silently runs the 4-section
    # ops schema. We fail loud (below) rather than fall back to a default init,
    # because a silently-ops partnership entity is exactly the failure to prevent.
    ok, _out, errors = _run_anneal_cmd(store, anneal_path, ["init", "--schema", "partnership"])
    if ok:
        emit("  Store initialized (partnership schema).")
        return True

    emit("  ! Could not initialize store:")
    for e in errors:
        emit(f"    - {e}")
    emit("    Most likely cause: anneal-memory is not installed in this Python,")
    emit("    or is older than the release that supports `init --schema` /")
    emit("    `set-schema` (the 6-section partnership schema).")
    emit("    Fix: pip install -U anneal-memory")
    emit(f"    Then: anneal-memory --db {store} init --schema partnership")
    return False


def _manifest_rows(
    install: Path, adapter: str, store: Path, store_ok: bool = True
) -> list[tuple[str, Path]]:
    """The (label, path) rows of files the install laid down, filtered to paths
    that actually EXIST.

    Pure — built from the known install layout (the orchestrator controls exactly
    what gets written), so the conditional seed copies and a failed store init
    drop out cleanly. Includes the Codex global files (`hooks.json` /
    `config.toml`) since they live outside the install dir but ARE
    created/modified by a codex install. Extracted from `_print_manifest` so a
    web init can render the same file list as structured rows instead of stdout
    text.
    """
    rows: list[tuple[str, Path]] = []

    seed = install / "seed"
    for f in sorted(seed.glob("*.md")):
        rows.append(("seed", f))

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

    return [(label, path) for label, path in rows if path.exists()]


def _print_manifest(
    install: Path, adapter: str, store: Path, store_ok: bool = True
) -> None:
    """List every file the install laid down — so the operator knows what
    landed, that they can hand-edit it, and where to look before first launch.
    Renders `_manifest_rows` (the pure projection) to stdout."""
    print()
    print("Files created (you can hand-edit any of these):")

    present = _manifest_rows(install, adapter, store, store_ok)
    width = max((len(label) for label, _ in present), default=0)
    for label, path in present:
        print(f"  {label:<{width}}  {path}")

    if (install / "activation").is_dir():
        print()
        print(
            "  (activation/posture.md + activation/recency_directives.md are yours "
            "to tune as you find your own RLHF-leakage patterns.)"
        )


def _next_steps_lines(install: Path, adapter: str, store_ok: bool = True) -> list[str]:
    """The post-install next-steps banner as a list of lines (leading + trailing
    blank included, so a plain print-each reproduces the CLI byte-for-byte).
    Pure — extracted from `_print_next_steps` so a web init renders the same
    guidance as structured lines."""
    lines: list[str] = [""]
    lines.append("=" * 60)
    if store_ok:
        lines.append("Install complete.")
    else:
        lines.append("Install PARTIAL — files laid down, store init FAILED. See above.")
    lines.append("=" * 60)
    lines.append(f"  Install:   {install}")
    lines.append(f"  Adapter:   {adapter}")
    lines.append("")
    lines.append("Next steps:")
    if adapter == "claude-code":
        lines.append("  - Open in Claude Code:")
        lines.append(f"        cd {install} && claude")
    if adapter == "codex":
        lines.append("  - IMPORTANT — Codex hook trust is per-content-hash. The very")
        lines.append("    first invocation MUST be interactive `codex` (not `codex exec`)")
        lines.append("    so Codex can prompt to trust the hook scripts. Editing the")
        lines.append("    hook scripts invalidates trust until re-approved interactively.")
        lines.append(f"        cd {install} && codex")
    lines.append(f"  - Verify the install (loud):  levain doctor --path {install}")
    lines.append(f"  - Smoke-test the hooks:       levain verify-hooks --path {install}")
    lines.append("  - (`doctor` static-checks wiring; `verify-hooks` actually invokes the")
    lines.append("     hook scripts. The harness still has to invoke them — verify in an")
    lines.append("     interactive session.)")
    lines.append("")
    return lines


def _print_next_steps(install: Path, adapter: str, store_ok: bool = True) -> None:
    for line in _next_steps_lines(install, adapter, store_ok):
        print(line)
