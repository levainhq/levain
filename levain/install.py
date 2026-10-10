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
`--force` the whole `activation/` tree is REPLACED, and the previous tree is
first moved whole to `.levain/backups/activation/tree-<ts>/`; old trees are
removed only when they prove unedited against their install receipt
(spore-861, spore-900). The anneal-memory store is preserved as-is. The Codex global `~/.codex/hooks.json` is backed up
before being overwritten, and `~/.codex/config.toml` is backed up and the
change announced when the MCP block is repointed at a DIFFERENT store.

⚠ This paragraph said "operator-edited activation files (`posture.md`,
`recency_directives.md`) ... with a timestamped suffix" until 2026-09-06, which
was wrong in BOTH the scope (two names, when the backup now covers the tree)
and the shape (a suffixed file, when it is a directory). A maintainer trusting
it over `_copy_activation_tree`'s own docstring would misjudge what survives a
re-install. Found by complement at L3, in the same diff that made it stale.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING

from levain.answers import AnswersError
from levain.packs import (
    BASE_SEED_REACHABLE,
    ON_DEMAND_SUMMARY,
    PackBrand,
    PackError,
    PackManifest,
    SeedEntry,
    compose_brand,
    compose_roster,
    import_entries,
    layer_wins,
    load_pack_manifest,
    on_demand_entries,
    order_activation_roots,
    render_entries,
    verbatim_entries,
)

if TYPE_CHECKING:
    from levain.interview import TemplateSpec


# Adapters that have NO trusted harness hook surface: they lay down the seed + store but
# no activation tree, no hooks, no MCP registration — their "activation" is a Python
# condenser wired at RUNTIME (`levain run`), not files installed here. `openhands` is the
# first. A predicate, not scattered name-checks, so the install/doctor/verify paths branch
# on the CAPABILITY (has-hooks) rather than re-enumerating adapter names.
HOOKLESS_ADAPTERS = frozenset({"openhands"})

# Every adapter the CLI accepts (the interactive prompt + `--adapter` resolution).
KNOWN_ADAPTERS = ("claude-code", "codex", "openhands")


def _adapter_has_hooks(adapter: str) -> bool:
    """True if the adapter installs a hook/MCP activation tree (claude-code, codex);
    False for a hookless adapter (openhands) whose activation is the runtime condenser."""
    return adapter not in HOOKLESS_ADAPTERS


# The hosted-harness artifacts a claude-code / codex install lays down. Their PRESENCE means
# an install carries hooks and is NOT cleanly hookless, regardless of the config marker.
_HOSTED_ARTIFACTS = ("CLAUDE.md", "AGENTS.md", "activation", ".claude", ".mcp.json")


def hosted_artifacts(install: Path) -> list[str]:
    """The hosted-harness files/dirs present in ``install`` (tag-files, activation tree, MCP
    config). Non-empty → the install carries hooks and is not cleanly hookless."""
    return [name for name in _HOSTED_ARTIFACTS if (install / name).exists()]


def effective_adapter(install: Path) -> str | None:
    """The adapter identity the INSTALL actually attests — the SINGLE source of truth shared
    by ``doctor`` / ``verify-hooks`` / ``run`` so they cannot diverge on adapter identity.

    Hosted tag-files DOMINATE a possibly-stale ``.levain/config.json`` marker (a ``--force``
    adapter switch leaves the OLD marker, and no hosted installer clears it — so the FILES are
    ground truth, not the marker): ``CLAUDE.md`` → ``claude-code``, ``AGENTS.md`` → ``codex``.
    A hookless (openhands) identity is honored ONLY when its marker is present AND no hosted
    residue exists — otherwise the install is a hosted or incoherent one, never a clean
    sovereign entity. ``None`` when nothing coherent is installed."""
    from levain.dashboard import installed_adapter

    if (install / "CLAUDE.md").is_file():
        return "claude-code"
    if (install / "AGENTS.md").is_file():
        return "codex"
    marker = installed_adapter(install)
    if marker is not None and not _adapter_has_hooks(marker) and not hosted_artifacts(install):
        return marker
    return None


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


def _copy_pack_docs(install: Path, packs: Sequence[tuple[PackManifest, Path]]) -> list[str]:
    """Copy each pack's ``docs/*.md`` into ``<install>/.levain/docs/<seq>-<pack>/``
    so `levain docs` composes a SELF-CONTAINED view independent of the original
    ``--pack`` directory (which may be gone by then). Returns the copied chapter
    labels (for the install print).

    ``packs`` is a sequence of ``(manifest, pack_dir)`` pairs — the manifests
    ALREADY validated in ``run_init``'s pre-interview snapshot, threaded in rather
    than re-read here. That (a) keeps the ONE-manifest-snapshot discipline
    ``run_init`` uses for seed+activation (a pack.toml edited during the interview
    can't make docs use a different order/name than the roster did), and (b) means
    this function raises no ``PackError`` at copy time (L1 review).

    The tree is DERIVED, not operator-edited, so it is rebuilt from scratch every
    install — a ``--force`` reinstall with a changed pack set never strands a stale
    chapter. A pack that ships no ``docs/`` (or an empty one) is skipped: pack docs
    are optional. Base docs ship in the wheel and are NOT copied here.

    Layer dirs are numbered by COMPOSITION RANK — the same ``(order, input-index)``
    stable ordering ``packs.compose_roster`` uses — zero-padded, so `levain.docs`
    reads layers in composition order by a plain lexical sort, AND two packs sharing
    an ``order``/``name`` get DISTINCT dirs instead of silently merging (codex L3
    MED). The resolved layer path is asserted to stay under the docs root as
    defense-in-depth (``packs.load_pack_manifest`` already rejects path separators
    in ``name``; codex L3 HIGH). If any copy fails mid-way, the whole derived tree
    is removed before the error propagates, so the caller's "shows base only"
    fallback is TRUE, never a half-copied manual (codex L3 LOW)."""
    from levain.docs import INSTALL_DOCS_SUBPATH, PACK_DOCS_DIRNAME

    dest_root = install.joinpath(*INSTALL_DOCS_SUBPATH)
    if dest_root.exists():
        shutil.rmtree(dest_root)

    # Order by (manifest.order, input-index) — the same stable sort compose_roster
    # uses — so the docs layer order matches the seed composition order even for
    # equal `order` values (a lexical dir-name sort alone would reorder them).
    ranked = sorted(
        ((manifest, i, pack_dir) for i, (manifest, pack_dir) in enumerate(packs)),
        key=lambda t: (t[0].order, t[1]),
    )
    dest_root_resolved = dest_root.resolve()
    copied: list[str] = []
    try:
        for seq, (manifest, _idx, pack_dir) in enumerate(ranked):
            src = pack_dir / PACK_DOCS_DIRNAME
            if not src.is_dir():
                continue
            md_files = sorted((f for f in src.glob("*.md") if f.is_file()), key=lambda f: f.name)
            if not md_files:
                continue
            layer = dest_root / f"{seq:03d}-{manifest.name}"
            if not layer.resolve().is_relative_to(dest_root_resolved):
                raise InitError(f"pack {manifest.name!r}: docs layer path escapes {dest_root}")
            layer.mkdir(parents=True, exist_ok=True)
            for f in md_files:
                shutil.copy2(f, layer / f.name)
                copied.append(f"{manifest.name}/{f.name}")
    except BaseException:
        # Any mid-copy failure (OSError, containment, KeyboardInterrupt) must not
        # leave a PARTIAL manual behind — remove the whole derived tree so the
        # caller's "base only" fallback is accurate, then re-raise.
        if dest_root.exists():
            shutil.rmtree(dest_root, ignore_errors=True)
        raise
    return copied


def _refuse_input(prompt: str = "") -> str:
    """The `input_fn` a non-interactive init hands the interview engine.

    A STRUCTURAL invariant, not a belt-and-braces nicety: the first scripted
    `levain init` attempt did not fail, it HUNG — the engine reached an
    undiscoverable `Skip this section? [y/N]` gate and blocked forever on a pipe
    that would never answer, after silently checkpointing four answers. "Validate
    the answers first so nothing can prompt" is a discipline; refusing to read a
    prompt at all is a guarantee. If this ever fires, a slot escaped validation —
    the correct outcome is a loud, named failure, never a hang.
    """
    raise AnswersError(
        f"non-interactive init tried to prompt for input ({prompt.strip()!r}). "
        f"Every field must be supplied by --answers; regenerate the skeleton with "
        f"`levain init --answers-template`."
    )


def run_answers_template(packs: list[Path] | None = None) -> int:
    """`levain init --answers-template` — emit a blank answer file for this install's
    interview (composed with any `--pack` layers, which add their own slots).

    JSON skeleton to STDOUT, the human field guide to STDERR, so the obvious
    invocation just works::

        levain init --answers-template > answers.json

    Read-only by construction: resolves no adapter, creates no directory, writes
    nothing. Discovering what an install will ask must never be a step that alters
    anything.
    """
    from levain.answers import answers_template_json, field_guide

    pack_dirs = [Path(str(p)).expanduser().resolve() for p in (packs or [])]
    with _templates_root() as templates_root:
        if not (templates_root / "seed" / "world.md").is_file():
            print(
                f"FAIL: Levain templates not found in installed package at "
                f"{templates_root}. The wheel may be corrupt; reinstall with "
                f"`pip install --force-reinstall levain`.",
                file=sys.stderr,
            )
            return 1
        try:
            from levain.interview import build_field_plan, parse_template

            roster = compose_roster([templates_root, *pack_dirs])
            specs = [parse_template(entry.path) for entry in render_entries(roster)]
        except PackError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — a malformed template surfaces, never crashes
            print(f"FAIL: could not read the interview templates: {e}", file=sys.stderr)
            return 1
        fields = build_field_plan(specs)

    print(field_guide(fields), file=sys.stderr)
    sys.stdout.write(answers_template_json(fields))
    return 0


def run_init(
    path: Path,
    adapter: str | None,
    force: bool,
    packs: list[Path] | None = None,
    answers_file: Path | None = None,
) -> int:
    # expanduser first: argparse's `type=Path` does not expand `~`, but operators
    # passing `--path ~/levain-install` reasonably expect shell semantics.
    install = Path(str(path)).expanduser().resolve()
    # Pack-layers compose ON TOP of the base templates (base = pack #0). Resolved
    # the same way as --path; a bad pack dir / missing pack.toml fails loud below.
    pack_dirs = [Path(str(p)).expanduser().resolve() for p in (packs or [])]
    # Filled from the pre-interview snapshot below; initialized here so the
    # post-interview docs-refresh always has a bound value (mypy flow-analysis).
    pack_manifests: list[PackManifest] = []

    # NON-INTERACTIVE MODE. `--answers` is a promise that no human is present, so
    # every prompt in this function has to be either satisfied up-front or refused
    # — a "mostly non-interactive" init is just an init that hangs somewhere less
    # obvious. The three prompts are: the adapter menu (required below), the
    # checkpoint-resume question (skipped below), and the interview itself
    # (satisfied by the validated answer file, and refused by `_refuse_input`).
    non_interactive = answers_file is not None
    file_answers: dict[str, str] = {}
    if answers_file is not None:
        from levain.answers import load_answers_file

        if adapter not in KNOWN_ADAPTERS:
            print(
                f"FAIL: --answers requires --adapter (one of "
                f"{', '.join(KNOWN_ADAPTERS)}).\n"
                f"      Without it the adapter menu would prompt, and there is no "
                f"one to answer it."
            )
            return 1
        try:
            # READ here, before the install directory is created, so a typo'd path
            # or malformed JSON fails with nothing made. (The against-the-plan
            # VALIDATION cannot happen this early — it needs the composed roster —
            # so a plan mismatch can leave an EMPTY install dir behind. Harmless:
            # an empty dir is a safe install target, so the retry needs no --force.)
            file_answers = load_answers_file(Path(str(answers_file)).expanduser())
        except AnswersError as e:
            print(f"FAIL: {e}")
            return 1

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

    from levain.manifest import resolve_anneal_bin

    python_path = sys.executable
    anneal_path = resolve_anneal_bin()  # spore-751: the interpreter's anneal, never PATH's

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
            # Validate + SNAPSHOT the pack manifests once, up-front (same discipline
            # as the roster/activation snapshot below) — a PackError here fails clean
            # BEFORE the interview, and the docs-copy step reuses this snapshot rather
            # than re-reading a pack.toml that could be edited mid-interview.
            pack_manifests = [load_pack_manifest(p) for p in pack_dirs]
            roster = compose_roster([templates_root, *pack_dirs])
            # Resolve the activation-tree layer stack HERE — from the same manifest
            # read as compose_roster, BEFORE the interview — so (a) a bad/mutated
            # manifest fails cleanly at this gate (not as a traceback deep in the
            # write-half), and (b) seed and activation layering share ONE manifest
            # snapshot (a pack mutated mid-interview can't make seed use the old
            # order while activation uses the new one). codex + L2 + nemotron L3.
            # Hookless adapters (openhands) have NO activation tree to layer — skip the
            # activation-root resolution entirely (it would raise on the missing base
            # tree, or silently borrow claude's). The seed roster above STILL composes
            # (a pack can carry entity seed); only the activation tree is skipped, and
            # `_install_adapter` installs seed + store only. `[]` = no activation.
            activation_roots = (
                order_activation_roots(
                    templates_root, _base_activation_root(chosen, templates_root), pack_dirs
                )
                if _adapter_has_hooks(chosen)
                else []
            )
        except PackError as e:
            print(f"FAIL: {e}")
            return 1
        render_specs = [parse_template(entry.path) for entry in render_entries(roster)]
        verbatim = verbatim_entries(roster)

        rendered_names = ", ".join(s.path.name for s in render_specs)
        print("=" * 60)
        print(f"Interview — fills the {rendered_names} templates.")
        print("=" * 60)

        initial_answers: dict[str, str] = {}
        if non_interactive:
            # VALIDATE AGAINST THE COMPOSED PLAN, NOT THE BASE TEMPLATES — the plan
            # is derived from THIS run's roster, so a `--pack` layer's extra slots are
            # required here exactly as the terminal interview would ask them, and a
            # base-only answer file against a packed install fails LOUD instead of
            # rendering the pack's slots empty.
            from levain.answers import (
                SHAPE_IMPOSSIBLE,
                shape_violations,
                validate_answers,
            )
            from levain.interview import build_field_plan

            plan = build_field_plan(render_specs)
            errors = validate_answers(plan, file_answers)
            if errors:
                print("FAIL: the --answers file does not match this install's interview:")
                for err in errors:
                    print(f"  - {err}")
                pack_flags = "".join(f" --pack {p}" for p in pack_dirs)
                print(
                    f"      Regenerate a blank skeleton with:\n"
                    f"        levain init --answers-template{pack_flags} > answers.json"
                )
                return 1
            initial_answers = file_answers
            print(f"  Non-interactive — {len(file_answers)} answer(s) from {answers_file}.")
            # Only the JUDGMENT-CALL shape findings reach here: the
            # can't-have-happened ones are hard errors inside `validate_answers`
            # above, so a fleet never reads exit 0 over a scrambled file. What is
            # left warns and continues — a heuristic gets to raise its hand, never
            # to overrule the operator about their own answers.
            for sev, note in shape_violations(plan, file_answers):
                if sev != SHAPE_IMPOSSIBLE:
                    print(f"  note: {note}")
        else:
            # Resume from prior Ctrl+C if a checkpoint exists.
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
                    print(
                        f"  Resuming — {n} answer(s) restored. Continuing where you left off."
                    )
                else:
                    _clear_checkpoint(install)
                    print("  Discarded checkpoint. Starting fresh.")

            print("  Press Ctrl+C to interrupt — answers so far will be saved for resume.")

        try:
            # Non-interactive: the validated file already fills every planned slot, so
            # the engine's walk is empty and it returns without asking anything.
            # `_refuse_input` makes that a GUARANTEE rather than an expectation, and no
            # checkpoint is written because nothing is asked (a checkpoint from a run
            # that prompts for nothing is just a stale file for the next run to offer).
            answers = conduct_interview(
                render_specs,
                answers=initial_answers,
                input_fn=_refuse_input if non_interactive else input,
                checkpoint_fn=(
                    None if non_interactive else (lambda a: _save_checkpoint(install, a))
                ),
            )
        except (KeyboardInterrupt, EOFError):
            print("\nInterview interrupted.")
            _report_partial_state(install)
            return 1
        except AnswersError as e:
            # `_refuse_input` fired (a slot escaped validation), or the terminal walk
            # refused a blank identity slot. Loud, named, no hang.
            print(f"FAIL: {e}")
            _report_partial_state(install)
            return 1

        # One install lock across every write (spore-1023): the docs rebuild below
        # wipes .levain/docs, so a concurrent init must not interleave with it either.
        try:
            with install_lock(install):
                result = apply_init(
                    install,
                    chosen,
                    answers,
                    templates_root,
                    python_path,
                    anneal_path,
                    render_specs,
                    verbatim,
                    activation_roots=activation_roots,
                    packs=list(zip(pack_manifests, pack_dirs)),
                    require_empty=not force,
                )
                # Interview completed successfully — clear the checkpoint so the next
                # `levain init --force` doesn't offer to resume stale answers.
                _clear_checkpoint(install)
                copied_docs = _refresh_pack_docs(install, pack_manifests, pack_dirs)
        except InstallLockError as e:
            print(f"FAIL: {e.message}")  # refused before any write: no partial-state report
            return 1
        except InitError as e:
            # The write-half fails loud (a corrupt-wheel activation tree, an
            # operator edit that can't be preserved) — render it as a clean FAIL +
            # partial-state report, not a traceback. codex L3 re-verify MED.
            print(f"FAIL: {e.message}")
            _report_partial_state(install)
            return 1
    if copied_docs:
        print(
            f"  docs:      {len(copied_docs)} pack chapter(s) → .levain/docs/ "
            f"(compose with `levain docs`)"
        )

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
    *,
    activation_roots: Sequence[Path] | None = None,
    packs: Sequence[tuple[PackManifest, Path]] = (),
    emit: Callable[[str], None] = print,
    require_empty: bool = False,
) -> InitResult:
    """:func:`_apply_init` under the per-install lock (:func:`install_lock`); raises
    :class:`InstallBusy` before writing anything when another process holds it.

    ``require_empty`` (a fresh init, no ``--force``) re-checks the target UNDER the lock.
    The caller's own emptiness check runs before the interview, minutes earlier, so two
    fresh inits could both pass it and the second would overwrite the first's seeds with
    no authorization and no backup (codex L3 on 19811f7, reproduced). Raises
    :class:`InstallTargetTaken` before writing anything."""
    with install_lock(install), (
            codex_home_lock(_codex_home()) if chosen == "codex" else contextlib.nullcontext()):
        # The CODEX_HOME lock is taken here, before the first write, so a refusal still
        # means nothing was written; _install_codex takes it again (a nested no-op).
        if require_empty and not _is_safe_install_target_locked(install):
            raise InstallTargetTaken(
                f"{install} is no longer empty: something was written there while this "
                f"`levain init` was running (another init, say). Nothing was written by this "
                f"one. Re-run with --force to replace it, or choose another path."
            )
        return _apply_init(
            install, chosen, answers, templates_root, python_path, anneal_path, specs,
            verbatim, activation_roots=activation_roots, packs=packs, emit=emit,
        )


def _apply_init(
    install: Path,
    chosen: str,
    answers: dict[str, str],
    templates_root: Path,
    python_path: str,
    anneal_path: str,
    specs: list[TemplateSpec],
    verbatim: Sequence[SeedEntry],
    *,
    activation_roots: Sequence[Path] | None = None,
    packs: Sequence[tuple[PackManifest, Path]] = (),
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

    `activation_roots` is the (keyword-only) ordered activation-tree layer stack
    (base first, then composing packs by `pack.toml` order — see
    `packs.order_activation_roots`). It is needed for the ACTIVATION tree layering,
    which (unlike the seed roster's import list) CANNOT be reconstructed from
    `specs`/`verbatim`: those carry only seed-file paths, never a pack's
    `activation/` tree. The CLI resolves it ONCE up-front (in `run_init`, from the
    same manifest read as `compose_roster`, BEFORE the interview) and passes it
    here — so seed and activation layering share one manifest snapshot. `None`
    (the default) means base-only: the adapter's own base activation tree, which is
    what the web onboarding path (base-only by design) gets unchanged.

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

    # Reconstruct the composed roster from the two halves of one compose_roster
    # (render `specs` + `verbatim` entries) to derive the adapter import list —
    # the seed files that load as always-on context. The import list IS
    # reconstructable from `specs`/`verbatim` (every seed path is in one of them),
    # so it needs no extra parameter; the ACTIVATION tree is not (it lives in each
    # pack's `activation/` subtree, not in the seed roster) — that is what
    # `pack_dirs` is threaded for, below. Each render spec carries `spec.path` =
    # the winning layer's source; `verbatim` entries are already SeedEntry.
    render_seed = [
        SeedEntry(name=spec.path.name, path=spec.path, mode="render") for spec in specs
    ]
    roster_seed = [*render_seed, *verbatim]
    import_seed = import_entries(roster_seed)
    on_demand_seed = on_demand_entries(roster_seed)

    # Honesty floor (the base half): every base methodology-core seed must REACH
    # the entity. A corrupt wheel that dropped one would otherwise generate an
    # adapter SILENTLY missing it — recreating, for a base seed, the
    # invisible-infrastructure failure this seam closes (the old hard-coded
    # template named it, so the break was visible). Fail loud, named.
    #
    # ⚠ CHECKED AGAINST REACHABILITY, NOT THE EAGER IMPORT LIST. When
    # spore_instructions.md moved to ON_DEMAND_SEED, an import-list check would
    # have kept passing while quietly ceasing to cover it — a guard that reports
    # itself armed and no longer defends the file it was written for. Reachable =
    # eagerly imported OR carrying a carrier pointer.
    reachable = {entry.name for entry in import_seed}
    reachable |= {entry.name for entry in on_demand_seed}
    missing_base = [name for name in BASE_SEED_REACHABLE if name not in reachable]
    if missing_base:
        raise InitError(
            f"base methodology seed file(s) missing from the install roster: "
            f"{missing_base}. The wheel may be corrupt; reinstall with "
            f"`pip install --force-reinstall levain`."
        )

    # Hookless adapters (openhands) have NO activation tree — force empty roots even when
    # apply_init is called directly with activation_roots=None (the openhands install branch
    # ignores roots, but computing claude's base tree here for a hookless adapter is the
    # exact latent trap codex L3 flagged: inert only by coincidence). Base-only
    # (activation_roots is None) = the adapter's own base tree — the web onboarding path.
    if not _adapter_has_hooks(chosen):
        roots: list[Path] = []
    elif activation_roots is not None:
        roots = list(activation_roots)
    else:
        roots = [_base_activation_root(chosen, templates_root)]
    _install_adapter(
        chosen, install, templates_root, python_path, anneal_path, import_seed,
        on_demand_seed, activation_roots=roots, emit=emit,
    )
    record_adapter_receipt(install, chosen, templates_root, python_path, import_seed,
                           on_demand_seed, emit=emit)

    store = install / ".levain" / "memory.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    # Persist the interview answers (operator-private) so a later `levain update` can
    # RE-RENDER a changed pack render-template with the SAME answers + a targeted
    # re-prompt for only NEW slots, instead of losing them (the render-slot reconcile
    # root-cause fix). Best-effort — never fails the install.
    #
    # STILL best-effort, but no longer SILENT about the second thing it costs. The
    # record is now also what `doctor` content-checks against, so a failed write
    # produces a genuinely healthy install that `doctor` nonetheless reports as
    # unverifiable — and `write_answers`'s own note only mentions pack re-renders,
    # which sends the operator hunting the wrong problem. Name both consequences at
    # the moment it fails; the install itself is fine and must not be torn down.
    if not write_answers(install, answers, emit):
        emit(
            "  note: the interview was NOT recorded, so `levain doctor` will report "
            "seed content as unverifiable. The install itself is fine — re-run "
            "`levain init --force` on this path to record it (your store is kept)."
        )
    # Bake the resolved pack white-label into the operator-facing .levain/config.json
    # (build-time pack.toml [brand] → the runtime channel entity_name travels). Runs
    # BEFORE the store init so it lands even on a store-init failure (it's chrome, not
    # store-dependent). Composed from the up-front pack snapshot, not a re-read.
    _write_brand_config(install, compose_brand([mf for mf, _ in packs]), emit)
    store_ok = _init_store(store, anneal_path, emit=emit)
    if store_ok:
        winning = [(spec.path.name, spec.path) for spec in specs]
        winning += [(entry.name, entry.path) for entry in verbatim]
        _record_compat_lock(install, store, anneal_path, packs=packs, emit=emit,
                            winning=winning)
    return InitResult(install=install, adapter=chosen, store_ok=store_ok)


def _refresh_pack_docs(
    install: Path, pack_manifests: Sequence[PackManifest], pack_dirs: Sequence[Path]
) -> list[str]:
    """ALWAYS refresh the persisted pack docs so `levain docs` renders a
    SELF-CONTAINED composed view — even with NO --pack this session. Calling it
    unconditionally is load-bearing: a --force reinstall that DROPS a pack must
    CLEAR that pack's stale (possibly company-private) chapters, not serve them
    forever — the exact IP-boundary failure class (complement L3 CRITICAL).
    `_copy_pack_docs` handles an empty pack list correctly (wipes .levain/docs,
    copies nothing). Base docs ship in the wheel; only pack docs copy. A copy
    failure must NOT fail an otherwise-good install (the manual is a read surface,
    not install-critical), so it warns and continues."""
    try:
        return _copy_pack_docs(install, list(zip(pack_manifests, pack_dirs)))
    except (OSError, InitError) as e:
        print(f"  note: could not refresh pack docs ({e}); `levain docs` shows base only.")
        return []


def _atomic_write_text(target: Path, payload: str) -> None:
    """Atomically replace ``target`` with ``payload`` — a UNIQUE temp (``mkstemp``)
    + fsync + ``os.replace`` + parent-dir fsync, mirroring ``write_answers`` /
    ``manifest.write_lock``. A torn write (Ctrl+C / ENOSPC / crash) never leaves the
    target truncated: the old bytes stay until the atomic rename. Raises ``OSError``
    on failure (caller decides fatal-vs-best-effort). [codex L3: config.json is the
    SHARED operator file — a plain write_text could blank entity_name on a torn write.]"""
    # ⛔ REPLACE THE SYMLINK'S TARGET, NOT THE SYMLINK (Diogenes MEDIUM, 2026-09-09,
    # reproduced on disk) — the same class `_merge_codex_config` was fixed for on 2026-09-07,
    # at the other atomic-replace site: `os.replace` onto a symlinked `.levain/config.json`
    # replaces the LINK with a regular file, leaving the dotfiles-managed real file holding
    # the OLD content, restored over the top on the next `stow`/`chezmoi apply`. Resolved
    # before `mkdir`/`mkstemp` so the temp file lands beside the REAL file (same filesystem,
    # so `os.replace` stays atomic) and `copymode`/`exists()` below read the real inode.
    target = target.resolve()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".tmp", dir=str(parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # ⛔ CARRY THE EXISTING MODE ACROSS THE INODE SWAP — the SAME class as the codex
        # `config.toml` widening, running in the OPPOSITE direction (Diogenes MEDIUM
        # 2026-09-07 found that one; L2 found this one by asking what else swaps inodes).
        # `mkstemp` always creates 0o600 REGARDLESS OF UMASK, so `os.replace` silently
        # NARROWS an operator's mode here rather than widening it. MEASURED on this helper:
        # 0o644 -> 0o600 and 0o640 -> 0o600, on `.levain/config.json`, which this function's
        # own docstring calls the SHARED operator file. An operator who opened it up so a
        # second account could read the dashboard config had it closed again by the next
        # `levain update`, with an unrelated-looking permission error at the reader.
        # ⚖ FIXED IN THE HELPER, NOT AT THE CALL SITES, because the defect belongs to the
        # inode swap and every caller inherits it — `guard_scoped_by_symptom_misses_the_class`
        # is what fixing this at `_write_brand_config` would have been.
        # ⚠ Only when the target EXISTS: for a new file `mkstemp`'s 0o600 is the safer
        # default and there is no operator intent to preserve. `copymode` and not `copystat`
        # — restore the property that was lost, not every property the API offers; the
        # mtime MUST advance here, the file changed.
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:  # dir fsync so the rename itself survives power-loss (parity with write_lock)
        dfd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _write_brand_config(
    install: Path, brand: PackBrand | None, emit: Callable[[str], None]
) -> None:
    """Bake the resolved pack white-label into the operator-facing
    ``.levain/config.json`` — build-time ``pack.toml [brand]`` → the runtime channel
    ``entity_name`` already travels (``dashboard._read_levain_config`` →
    ``build_substrate_view``).

    Called from BOTH ``apply_init`` (init) AND ``reconcile.run_pack_reconcile``
    (``levain update``), the exact two events ``_copy_pack_docs`` runs on — so the
    IP-boundary parity below is REAL, not init-only: a ``pack.toml [brand]`` edit is
    drift (it is in the hashed set), so update re-bakes it, and a dropped ``[brand]``
    clears the stale chrome on the same update the docs are wiped on.

    MERGE, never clobber: a ``--force`` reinstall / an update preserves an
    operator-set ``entity_name`` (and any future config key) — only the brand keys
    are touched (the same key-preserving merge ``writes._apply_entity_name`` does in
    reverse). CLEAR-on-drop: no brand (dropped pack / dropped ``[brand]``) removes the
    stale brand keys, so company chrome never outlives its pack — the same IP-boundary
    discipline ``_copy_pack_docs`` follows when it wipes dropped-pack chapters. A no-op
    change writes nothing (no spurious mtime churn).

    Two-writer note (accepted): this shares ``.levain/config.json`` with the runtime
    ``writes._apply_entity_name`` rename, in a DIFFERENT process, with no cross-process
    lock — so an ``init --force`` concurrent with a live-dashboard rename is an
    unprotected lost-update (last atomic write wins on its own keys). The write is
    atomic (``_atomic_write_text``), so a reader never sees a torn file; the residual
    race is a rare lost-update inherent to config.json's design (the rename writer is
    likewise not cross-process serialized — reinstall-while-serving is the pathological
    case), NOT this feature's introduction. Fixing it fully = a config.json file lock,
    disproportionate for chrome. [codex/kimi/complement L3.]

    Best-effort: a config-write failure never fails an otherwise-good install (the
    brand is chrome, not install-critical) — it warns through ``emit`` and returns."""
    from levain.dashboard import LEVAIN_CONFIG_REL, _read_levain_config

    config_path = install.joinpath(*LEVAIN_CONFIG_REL)
    # Refuse to clobber an UNREADABLE-but-present config. `_read_levain_config` fails
    # soft to `{}` for BOTH absent AND corrupt; merging brand onto `{}` and writing
    # would ERASE an `entity_name` we merely couldn't parse (a hand-edit JSON typo).
    # Absent = safe to create; present-but-unparseable = bail (fix it, then re-run) —
    # so the "never clobber entity_name" guarantee holds even against a pre-corrupt
    # file, not just a torn write (the atomic write closes the torn-write half). [L1]
    if config_path.is_file():
        try:
            json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            emit("  note: .levain/config.json is unreadable — brand NOT written (refusing to "
                 "overwrite possibly-recoverable config; fix it, then re-run).")
            return

    current = _read_levain_config(install)
    new_config = dict(current)
    for key, value in (
        ("surface_name", brand.surface_name if brand else None),
        ("subtitle", brand.subtitle if brand else None),
    ):
        if value is not None:
            new_config[key] = value
        else:
            new_config.pop(key, None)  # clear-on-drop: no stale chrome across re-install
    if new_config == current:
        return  # nothing to write (no brand now, none before) — avoid mtime churn
    try:
        _atomic_write_text(
            config_path, json.dumps(new_config, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        emit(f"  note: could not write brand config ({e}); surfaces show default Levain chrome.")
        return
    if brand is not None:
        emit(f"  brand:     {brand.surface_name or '(subtitle only)'} → .levain/config.json")


def write_answers(
    install: Path, answers: dict[str, str], emit: Callable[[str], None]
) -> bool:
    """Persist the interview answers to ``.levain/answers.json`` (operator-private,
    gitignored) — the render-slot reconcile re-render input. Returns True iff it
    persisted; the reconcile REQUIRES a True return before advancing provenance after
    answering new slots (codex L3 #3 — else a failed write leaves the manifest advanced
    while the only answer source lacks the new slot).

    ATOMIC + concurrency-safe: a UNIQUE temp (``mkstemp``) + fsync + ``os.replace`` — a
    torn write (Ctrl+C / ENOSPC) never leaves it truncated, and two concurrent
    ``levain update`` runs can't clobber each other's temp (the fixed ``.json.tmp``
    path raced — codex L3 #4). Mirrors ``manifest.write_lock``."""
    levain_dir = install / ".levain"
    levain_dir.mkdir(parents=True, exist_ok=True)
    target = levain_dir / "answers.json"
    payload = json.dumps(answers, indent=2, sort_keys=True) + "\n"
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="answers.", suffix=".json.tmp", dir=str(levain_dir))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # Directory fsync so the rename itself is durable on power-loss — parity with
        # write_lock, for equally-irreplaceable operator data (complement L3 #5).
        try:
            dfd = os.open(str(levain_dir), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        _ensure_gitignored(levain_dir, "answers.json")
        return True
    except OSError as e:
        emit(f"  note: could not persist interview answers ({e}); a future pack "
             f"render-template change will need a re-onboard, not a targeted re-prompt.")
        return False


def _ensure_gitignored(levain_dir: Path, name: str) -> None:
    """Ensure ``name`` is listed in ``.levain/.gitignore`` (the operator's private
    answers must never be committed). Creates or appends; best-effort."""
    gi = levain_dir / ".gitignore"
    try:
        existing = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
        if name not in existing:
            with open(gi, "a", encoding="utf-8") as fh:
                if existing and existing[-1].strip():
                    fh.write("\n")
                fh.write(name + "\n")
    except OSError:
        pass


def read_answers(install: Path) -> dict[str, str]:
    """The persisted interview answers (``{}`` if absent/unreadable — the reconcile
    then falls back to surfacing a render change for re-onboard, never blank-fill)."""
    try:
        data = json.loads((install / ".levain" / "answers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _record_compat_lock(
    install: Path,
    store: Path,
    anneal_path: str,
    packs: Sequence[tuple[PackManifest, Path]] = (),
    emit: Callable[[str], None] = print,
    winning: Sequence[tuple[str, Path]] | None = None,
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
    # Snapshot each composed pack's source provenance (per-file hashes) so a later
    # `levain update` can detect PACK drift (the pulled source changed) and
    # reconcile it — the compat-manifest pattern extended to the pack axis. Version
    # is optional pack.toml sugar for the notice; detection is hash-based. The
    # `rendered` map records each render seed's INSTALLED-file hash so the reconcile
    # can tell an operator edit from a template change (a render file's install copy
    # never equals its source template).
    pack_provenance = [
        manifest.pack_provenance(
            mf.name, pack_dir, mf.version,
            rendered=manifest.rendered_hashes(install, [f"seed/{n}" for n in mf.render]),
            render=mf.render,
            # The filenames this pack WON (``winning`` = every installed seed file's
            # source, from the one compose_roster the writes used); None when the
            # caller passed no roster, which the reconcile reads as "not recorded".
            wins=None if winning is None else layer_wins(winning, pack_dir),
        )
        for mf, pack_dir in packs
    ]
    try:
        manifest.write_lock(install, composed, packs=pack_provenance)
        pack_note = f" + {len(pack_provenance)} pack(s)" if pack_provenance else ""
        emit(f"  Recorded the known-good set (anneal {composed.anneal} / "
             f"schema {composed.schema}{pack_note}).")
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
    if arg in KNOWN_ADAPTERS:
        return arg
    return _prompt_adapter()


def _prompt_adapter() -> str:
    print("Which harness adapter do you want to install?")
    print("  1) Claude Code")
    print("  2) Codex CLI")
    print("  3) OpenHands — a sovereign, runnable entity (no hooks; drive it with")
    print("        `levain run`, on an open model, with its own memory)")
    print()
    print("  (v1 installs one adapter per install. To use both harnesses,")
    print("   create two separate installs.)")
    while True:
        try:
            choice = input("Enter 1, 2, or 3: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise _UserCancelled
        if choice in ("1", "claude", "claude-code"):
            return "claude-code"
        if choice in ("2", "codex"):
            return "codex"
        if choice in ("3", "openhands"):
            return "openhands"
        print(f"  Unrecognized: {choice!r}. Try again.")


def _is_safe_install_target(path: Path) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    return not any(path.iterdir())


def _is_safe_install_target_locked(path: Path) -> bool:
    """:func:`_is_safe_install_target` for a caller holding :func:`install_lock`: this
    run's own bookkeeping (the lock file, the ``.gitignore`` entry the lock writes, and
    the interview checkpoint) does not count as content."""
    if not path.is_dir():
        return False
    for entry in path.iterdir():
        if entry.name != INSTALL_LOCK_REL[0] or not entry.is_dir():
            return False
        ckpt = _checkpoint_path(path).name
        own = {INSTALL_LOCK_REL[1], ".gitignore", ckpt, ckpt + ".tmp"}
        if any(e.name not in own for e in entry.iterdir()):
            return False
    return True


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


class InstallLockError(InitError):
    """The install lock could not be taken; nothing was written."""


class InstallBusy(InstallLockError):
    """Another levain process is writing this install right now; nothing was written."""


class InstallTargetTaken(InstallLockError):
    """A fresh (non --force) init found the target no longer empty once it held the lock:
    another init finished there first. Nothing was written."""


INSTALL_LOCK_REL = (".levain", "install.lock")
_held_install_locks: dict[tuple[Path, int], int] = {}
_held_install_locks_guard = threading.Lock()


@contextmanager
def install_lock(install: Path, *, create: bool = True) -> Iterator[None]:
    """Hold the per-install SINGLE-WRITER lock (``.levain/install.lock``, an exclusive
    non-blocking ``flock``) for the duration, or raise :class:`InstallBusy` at once.

    Two ``levain init --force`` runs on one install used to interleave their activation
    swaps: reproduced through the CLI as an uncaught FileNotFoundError mid-swap, and as
    a receipt describing a tree that was not installed (spore-1023).

    REENTRANT per (install, thread): a nested ``install_lock`` inside a held one is a
    no-op, so a caller can hold it across ``apply_init`` (which takes it too) and the
    work after it: ``run_init`` and the web init handler both do, around the docs
    refresh. A second ``flock`` on a fresh fd would refuse even its own process,
    because a BSD ``flock`` belongs to the open file, not the process. Without
    ``fcntl`` (Windows) there is no lock and this yields unguarded."""
    path = install.joinpath(*INSTALL_LOCK_REL)

    def precheck() -> None:
        if not create and not path.parent.is_dir():
            # `update` / `adopt-answers` on a path that is not an install must not leave a
            # .levain/ behind, which would make a later `init` there refuse a non-empty dir.
            raise InstallLockError(
                f"{install} is not a levain install (no .levain/ directory).")

    with _single_writer_lock(
        path, precheck=precheck, what="the install lock", guards="a concurrent init/update",
        busy=lambda: InstallBusy(
            f"another levain process (an `init` or `update`) is writing {install} "
            f"right now. This run installed nothing; re-run once that one has finished."),
    ):
        yield


class CodexHomeBusy(InstallLockError):
    """Another levain process is writing CODEX_HOME's config.toml / hooks.json right now;
    nothing was written."""


class CodexHomeUnlockable(InstallLockError):
    """CODEX_HOME cannot be locked here (no ``flock`` on the platform, or the filesystem
    answers "not supported") and :data:`CODEX_HOME_UNLOCKED_ENV` is not ``1``; nothing was
    written."""


CODEX_HOME_LOCK_NAME = "levain.lock"
# The named override for a CODEX_HOME that cannot be locked (Phill 2026-10-10, option B:
# fail closed there, with a named override). Exactly "1" opts in; any other value refuses.
CODEX_HOME_UNLOCKED_ENV = "LEVAIN_CODEX_HOME_UNLOCKED"


@contextmanager
def codex_home_lock(codex_home: Path) -> Iterator[None]:
    """Hold the CODEX_HOME-wide single-writer lock (``<CODEX_HOME>/levain.lock``) across
    reading and writing codex's two machine-global files, config.toml and hooks.json.

    The per-install lock cannot cover them: two ``levain init --adapter codex`` runs from
    DIFFERENT installs hold different install locks, and interleaved between the two
    writes they left config.toml registering one install's store and hooks.json firing
    the other's hooks, both exiting 0 (codex L3 r2 2026-10-10; reproduced through the CLI
    on 0.7.0). Taken after the install lock, never before, so two levain processes
    cannot wait on each other. Same semantics as :func:`install_lock`, except where no lock
    can be taken: there it raises :class:`CodexHomeUnlockable` (fail closed) unless
    ``LEVAIN_CODEX_HOME_UNLOCKED=1`` is set, the operator's statement that no other install
    writes this codex home at the same time."""
    lock = codex_home / CODEX_HOME_LOCK_NAME

    def unlockable(why: str) -> InstallLockError:
        return CodexHomeUnlockable(
            f"{lock} cannot be locked ({why}), so two codex installs writing {codex_home} at "
            f"once could mix config.toml and hooks.json. Nothing was written there. If no other "
            f"levain install writes this codex home at the same time, re-run with "
            f"{CODEX_HOME_UNLOCKED_ENV}=1.")

    with _single_writer_lock(
        lock, what="the CODEX_HOME lock",
        guards="a concurrent codex init/update from another install",
        busy=lambda: CodexHomeBusy(
            f"another levain process (an `init` or `update` of a codex install) is "
            f"writing {codex_home} right now. This run wrote nothing there; re-run once "
            f"that one has finished."),
        unlockable=None if os.environ.get(CODEX_HOME_UNLOCKED_ENV) == "1" else unlockable,
    ):
        yield


@contextmanager
def _single_writer_lock(
    path: Path, *, what: str, guards: str, busy: Callable[[], InstallLockError],
    precheck: Callable[[], None] | None = None,
    unlockable: Callable[[str], InstallLockError] | None = None,
) -> Iterator[None]:
    """An exclusive non-blocking ``flock`` on ``path``, reentrant per (path, thread); the
    body of :func:`install_lock` and :func:`codex_home_lock`. Where no lock can be taken,
    ``unlockable`` (when given) builds the refusal to raise instead of running unguarded."""
    try:
        import fcntl
    except ImportError:
        if precheck is not None:
            precheck()  # a path that is not an install is refused here too, lock or none
        if unlockable is not None:
            raise unlockable("this platform has no flock") from None
        yield
        return
    key = (path.resolve(), threading.get_ident())
    with _held_install_locks_guard:
        if key in _held_install_locks:
            _held_install_locks[key] += 1
            nested = True
        else:
            nested = False
    if nested:
        try:
            yield
        finally:
            with _held_install_locks_guard:
                _held_install_locks[key] -= 1
        return
    if precheck is not None:
        precheck()
    fd = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Never through a link: a lock file swapped for a symlink to a file levain later
        # replaces would hand a second process a different inode, and so a second lock.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
        if path.parent.name == ".levain":
            _ensure_gitignored(path.parent, path.name)
    except OSError as e:
        if fd >= 0:
            os.close(fd)
        if e.errno == errno.ELOOP:
            raise InstallLockError(
                f"{what} {path} is a symlink, and levain never takes a lock through one. "
                f"Nothing was written; remove the link and re-run.") from None
        raise InstallLockError(
            f"cannot open {what} {path} ({e.strerror or e}). Nothing was "
            f"written; fix the permissions on {path.parent} and re-run."
        ) from None
    # ENOLCK is NOT here: it also means a full lock table or lockd down, both transient,
    # and running unguarded then is the corruption the lock exists to stop (L3 r2).
    unsupported = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
    try:
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except InterruptedError:
                    continue
        except OSError as e:
            if e.errno not in {errno.EWOULDBLOCK, errno.EAGAIN} | unsupported:
                raise InstallLockError(
                    f"could not lock {path} ({e.strerror or e}). Nothing was written; "
                    f"re-run, and check the filesystem if it repeats."
                ) from None
            if e.errno in unsupported:
                if unlockable is not None:
                    raise unlockable(f"the filesystem answers {e.strerror or e}") from None
                # A filesystem without lock support (NFS without lockd, some SMB/FUSE
                # homes) is not "another process": proceed unguarded, and say so.
                print(f"  note: this filesystem cannot lock {path} ({e.strerror or e}); "
                      f"continuing without the guard against {guards}.",
                      file=sys.stderr)
                os.close(fd)
                fd = -1
                with _held_install_locks_guard:
                    _held_install_locks[key] = 1
                try:
                    yield
                finally:
                    with _held_install_locks_guard:
                        del _held_install_locks[key]
                return
            raise busy() from None
        with _held_install_locks_guard:
            _held_install_locks[key] = 1
        try:
            yield
        finally:
            with _held_install_locks_guard:
                del _held_install_locks[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def open_init_templates(
    pack_dirs: Sequence[Path] = (),
) -> Iterator[tuple[Path, list[TemplateSpec], list[SeedEntry]]]:
    """Yield ``(templates_root, render_specs, verbatim)`` for an init run.

    The shared templates-context + corruption preflight + roster resolution, so a
    SECOND init surface (the web POST) doesn't re-implement the ``with
    _templates_root()`` block, the missing-template check, and the roster
    discovery that ``run_init`` does inline. ``render_specs`` are the parsed
    interview templates; ``verbatim`` is the list of non-rendered seed ENTRIES
    ``apply_init`` copies.

    ``pack_dirs`` (default empty = base-only) are pack-layer directories composed
    ON TOP of the base templates (base = pack #0), the same ``compose_roster``
    stack ``run_init`` builds — so the web onboarding surface composes packs
    identically to the CLI (the ``--web --pack`` path), never a second composition
    implementation. A ``PackError`` (missing/malformed manifest, render-lists a
    missing file, non-.md asset) surfaces as ``InitError`` so the caller renders
    it directly. MUST be consumed inside the ``with`` (the materialized tempdir
    under a zipped distribution is cleaned up on exit, and ``apply_init`` reads
    from ``templates_root``). Raises ``InitError`` (with a ready-to-surface
    ``.message``) if the packaged templates are missing/corrupt.
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
            roster = compose_roster([templates_root, *pack_dirs])
        except PackError as e:
            raise InitError(str(e)) from e
        specs = [parse_template(entry.path) for entry in render_entries(roster)]
        verbatim = verbatim_entries(roster)
        yield templates_root, specs, verbatim


def _base_activation_root(adapter: str, templates_root: Path) -> Path:
    """The adapter's BASE activation tree. Claude Code's is `templates/activation`;
    Codex's is `adapters/codex/activation`. Distinct from `templates_root` itself
    (the base PACK dir, whose `pack.toml` carries the base order that
    `order_activation_roots` reads)."""
    if adapter == "codex":
        return templates_root / "adapters" / "codex" / "activation"
    return templates_root / "activation"


def _install_adapter(
    name: str,
    install: Path,
    templates_root: Path,
    python_path: str,
    anneal_path: str,
    import_seed: Sequence[SeedEntry],
    on_demand_seed: Sequence[SeedEntry],
    *,
    activation_roots: Sequence[Path],
    emit: Callable[[str], None] = print,
) -> None:
    adapter_root = templates_root / "adapters" / name

    if name == "claude-code":
        _install_claude_code(
            install, templates_root, adapter_root, python_path, anneal_path,
            import_seed, on_demand_seed,
            activation_roots=activation_roots, emit=emit,
        )
        return

    if name == "codex":
        _install_codex(
            install, adapter_root, python_path, anneal_path, import_seed,
            on_demand_seed, activation_roots=activation_roots, emit=emit,
        )
        return

    if name == "openhands":
        # Hookless: seed + store only (both already written around this call). No
        # activation tree, no hooks, no MCP — the condenser is the runtime activation.
        # `activation_roots` / `import_seed` are ignored (there is no @import context
        # file); we only stamp the adapter marker so doctor/verify can identify the
        # install without a tag-file (openhands lays down no CLAUDE.md/AGENTS.md).
        _install_openhands(install, emit=emit)
        return

    raise ValueError(f"unknown adapter: {name}")  # pragma: no cover


# ---------- adapter seed-import-list generation (the load-side @import seam) ----------

_SEED_IMPORTS_PLACEHOLDER = "{{SEED_IMPORTS}}"
_SEED_ON_DEMAND_PLACEHOLDER = "{{SEED_ON_DEMAND}}"


def _seed_role_title(path: Path) -> str | None:
    """The seed file's role label for the Codex read-list — its first ``# `` H1's
    text, stripped of any ``" — <suffix>"`` (the suffix is the entity name or a
    parenthetical, e.g. ``# Who You Are — {{ENTITY_NAME}}`` -> ``Who You Are``;
    ``# Your Memory — anneal-memory`` -> ``Your Memory``). Read from the seed
    SOURCE, so a render file's H1 placeholder sits after the separator and drops
    out cleanly. Returns ``None`` when the file has no ``# `` H1 — the Codex line
    then omits the description (a reading hint is not a correctness signal, so its
    absence never fails the install)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        # A real Markdown H1 sits at column 0 ("# Title"); do NOT strip leading
        # whitespace first, or an indented "    # cmd" code line would be misread
        # as the title.
        if line.startswith("# "):
            title = line[2:].split(" — ", 1)[0].strip()
            # Drop an empty title OR one still holding an unrendered placeholder (a
            # pack H1 like "# {{NAME}}'s Method", placeholder BEFORE any " — "): a
            # description carrying raw {{...}} is meaningless, so omit it (bare
            # entry) rather than leak the placeholder into the read-list. The base
            # render files keep their {{ENTITY_NAME}} AFTER the " — ", so it drops
            # out cleanly and the title is placeholder-free.
            if not title or "{{" in title:
                return None
            return title
    return None


def _claude_import_block(import_seed: Sequence[SeedEntry]) -> str:
    """The Claude Code ``@seed/<name>`` import lines, one per importable seed file,
    in load order — fills ``{{SEED_IMPORTS}}`` in CLAUDE.md.template."""
    return "\n".join(f"@seed/{entry.name}" for entry in import_seed)


def _codex_import_block(import_seed: Sequence[SeedEntry]) -> str:
    """The Codex numbered read-list — one entry per importable seed file, in load
    order: ``N. `seed/<name>` — <role>`` (role from the file's H1, omitted when
    absent). Fills ``{{SEED_IMPORTS}}`` in AGENTS.md.template."""
    lines: list[str] = []
    for i, entry in enumerate(import_seed, start=1):
        title = _seed_role_title(entry.path)
        if title:
            lines.append(f"{i}. `seed/{entry.name}` — {title}")
        else:
            lines.append(f"{i}. `seed/{entry.name}`")
    return "\n".join(lines)


def _on_demand_block(on_demand_seed: Sequence[SeedEntry]) -> str:
    """The carrier's on-demand pointer block — one entry per seed file that
    installs to disk but is NOT eagerly loaded. Fills ``{{SEED_ON_DEMAND}}`` in
    both adapter templates (the text is prose, so it needs no per-adapter form).

    Each entry carries its RETENTION SUMMARY from :data:`ON_DEMAND_SUMMARY` — the
    part the entity must hold without opening the file — because a bare filename
    pointer would silently kill the layer it points at. Returns ``""`` when there
    are no on-demand entries, so the block disappears rather than leaving an empty
    heading.

    Fail-soft on a MISSING summary: emit the pointer with the file's H1 role
    instead. A summary-less pointer is a weaker pointer, but dropping the entry
    entirely would leave a file that installs to disk and reaches context by no
    path at all — fail toward reachable. (`test_packs` fails the build for a
    missing summary, so this branch is a backstop, not the plan.)
    """
    if not on_demand_seed:
        return ""
    base_seed_root = _base_seed_root()
    lines = [
        "## Read these when you need them",
        "",
        "These seed files install with your entity but are deliberately NOT loaded",
        "above. Each line here is the part to RETAIN; the file is one read away when",
        "you act on it. This keeps your always-on context about who you are rather",
        "than how the machinery works.",
        "",
    ]
    for entry in on_demand_seed:
        summary = ON_DEMAND_SUMMARY.get(entry.name)
        # ⚠ A PACK OVERRIDE MUST NOT INHERIT THE BASE SUMMARY (both L3 lineages found
        # this independently). `on_demand_entries` resolves to the WINNING layer, so a
        # pack can replace this file's content entirely — but ON_DEMAND_SUMMARY is
        # keyed by FILENAME, so the pointer would keep asserting the BASE discipline
        # over pack-authored content. The retained text IS the point of the pointer,
        # which makes the wrong retained text worse than none: it confidently
        # describes a procedure the file does not contain. No pack-manifest field for
        # a custom summary exists today, so fall back to the file's OWN H1 whenever
        # the winning entry is not the base file.
        if summary and not _is_base_seed(entry, base_seed_root):
            summary = None
        if not summary:
            title = _seed_role_title(entry.path)
            summary = (
                f"{title} — read this file before working in that area."
                if title
                else "Read this file before working in the area it covers."
            )
        lines.append(f"- `seed/{entry.name}` — {summary}")
    return "\n".join(lines)


def _base_seed_root() -> Path | None:
    """The package's own shipped `seed/` directory, resolved ONCE.

    Hoisted out of the per-entry loop deliberately: `_templates_root` is a context
    manager that may EXTRACT the wheel to a temp dir, so calling it per seed file would
    re-extract per file. ``None`` means "could not determine", handled by the caller.

    ⚠ KNOWN DEFECT, NAMED RATHER THAN DENIED (0.4.1). An earlier version of this
    docstring also claimed the hoist avoids "compar[ing] against a directory already
    torn down" — and this function does precisely that: it returns a path from INSIDE
    its own `with` block, so the context manager has exited before any caller compares
    against it.

    IMPACT IS BOUNDED, AND IS CURRENTLY ZERO IN THE FIELD. `importlib.resources.as_file`
    only materializes a temp dir for a ZIPPED distribution; pip unpacks wheels into
    site-packages, so every real install yields a persistent path and this is inert.
    Under a zipped distribution the returned path is dead, `_is_base_seed` reads EVERY
    base seed as a pack override, and the retention summary degrades to the bare H1
    fallback — the layer the code below calls the whole point.

    The real fix is STRUCTURAL (callers must hold the CM open across the comparison),
    not a bigger hoist, so it is deliberately not in this patch release. Do not attempt
    to "fix" it by hoisting further — that cannot work, and the previous docstring's
    confidence is what kept anyone from looking.
    """
    try:
        with _templates_root() as templates_root:
            return (templates_root / "seed").resolve()
    except (OSError, RuntimeError):
        return None


def _is_base_seed(entry: SeedEntry, base_seed_root: Path | None) -> bool:
    """Is this roster entry the BASE file, or a pack's override of it?

    Compared against the package's own shipped seed directory, `.resolve()`d on both
    sides — never a substring match on the path, which an editable install, a
    temp-extracted wheel, and a symlinked venv would each get wrong differently.
    """
    if base_seed_root is None:
        return False
    try:
        return entry.path.resolve() == base_seed_root / entry.name
    except OSError:
        # Cannot tell -> treat as NOT base, which drops to the file's own H1. A
        # generic-but-true pointer beats a specific-but-possibly-false one.
        return False


def _fill_seed_on_demand(template_text: str, block: str) -> str:
    """Substitute the single ``{{SEED_ON_DEMAND}}`` placeholder with the pointer
    block.

    Same placeholder-count honesty floor as :func:`_fill_seed_imports`, and for
    the same reason: a template missing it writes a carrier whose on-demand files
    are unreachable. It differs on the EMPTY case, which is legitimate here — an
    empty :data:`ON_DEMAND_SEED` means nothing was moved behind a load, so the
    block is dropped along with its surrounding blank line rather than leaving a
    hole in the rendered file. (The guarantee that a file dropped from the EAGER
    list actually got a pointer is `BASE_SEED_REACHABLE`, enforced in
    `apply_init` — it is a roster-level invariant, not a text-substitution one.)
    """
    count = template_text.count(_SEED_ON_DEMAND_PLACEHOLDER)
    if count != 1:
        raise InitError(
            f"adapter template must contain exactly one {_SEED_ON_DEMAND_PLACEHOLDER} "
            f"placeholder (found {count}) — the wheel may be corrupt or the template "
            f"hand-edited; reinstall with `pip install --force-reinstall levain`."
        )
    if not block.strip():
        # Drop the placeholder's OWN LINE, preserving the surrounding line structure.
        #
        # Scoped to a placeholder that OWNS its line, which is the only shape the
        # shipped templates use and the only one this claims to handle (L3, both
        # lineages: the earlier docstring claimed more than the code delivered). An
        # INLINE placeholder — "a {{X}}\nb" — is left to the plain substitution below
        # it, because silently eating an inline placeholder's whole line would drop
        # the operator's own text with it.
        #
        # The naive form (replacing "\n{{X}}\n" with "") consumes BOTH newlines and
        # JOINS the lines either side — caught by this function's own unit test, not
        # by review. Collapsing "\n\n{{X}}\n" to "\n" also absorbs the blank line the
        # placeholder was spaced with, so the carrier does not gain a double blank.
        for old, new in (
            (f"\n\n{_SEED_ON_DEMAND_PLACEHOLDER}\n", "\n"),
            (f"\n{_SEED_ON_DEMAND_PLACEHOLDER}\n", "\n"),
            (f"{_SEED_ON_DEMAND_PLACEHOLDER}\n", ""),
        ):
            if old in template_text:
                return template_text.replace(old, new, 1)
        # End-of-file placeholder (no trailing newline), or an inline one: remove the
        # token itself and leave surrounding text exactly as authored.
        return template_text.replace(_SEED_ON_DEMAND_PLACEHOLDER, "")
    return template_text.replace(_SEED_ON_DEMAND_PLACEHOLDER, block)


def _fill_seed_imports(template_text: str, block: str) -> str:
    """Substitute the single ``{{SEED_IMPORTS}}`` placeholder with the generated
    import block. Honesty floor — BOTH failure modes write an import-less adapter
    file (the invisible-infrastructure failure this seam closes), so fail loud on
    either, self-contained (not relying on a caller's preflight):
      - the template MISSING the placeholder (a corrupt/hand-edited template); and
      - an EMPTY generated block (a roster with no loadable seed files)."""
    count = template_text.count(_SEED_IMPORTS_PLACEHOLDER)
    if count != 1:
        raise InitError(
            f"adapter template must contain exactly one {_SEED_IMPORTS_PLACEHOLDER} "
            f"placeholder (found {count}) — the wheel may be corrupt or the template "
            f"hand-edited; reinstall with `pip install --force-reinstall levain`."
        )
    if not block.strip():
        raise InitError(
            "the generated seed-import block is empty — refusing to write an "
            "import-less adapter file (no loadable seed files in the roster). "
            "The wheel may be corrupt; reinstall with "
            "`pip install --force-reinstall levain`."
        )
    return template_text.replace(_SEED_IMPORTS_PLACEHOLDER, block)


def _install_claude_code(
    install: Path,
    templates_root: Path,
    adapter_root: Path,
    python_path: str,
    anneal_path: str,
    import_seed: Sequence[SeedEntry],
    on_demand_seed: Sequence[SeedEntry] = (),
    *,
    activation_roots: Sequence[Path],
    emit: Callable[[str], None] = print,
) -> None:
    # `activation_roots` was resolved up-front (run_init / apply_init) — base
    # (templates/activation) first, then any pack activation/ trees by order.
    # base_activation is passed explicitly so its OWN completeness can be checked
    # (a pack must not mask an empty base).
    _copy_activation_tree(
        activation_roots,
        install / "activation",
        base_activation=templates_root / "activation",
        anneal_path=anneal_path,
        emit=emit,
    )

    for target, text in _claude_code_files(
        install, adapter_root, python_path, import_seed, on_demand_seed
    ).items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    emit("  Claude Code adapter installed.")


def _claude_code_files(
    install: Path,
    adapter_root: Path,
    python_path: str,
    import_seed: Sequence[SeedEntry],
    on_demand_seed: Sequence[SeedEntry],
) -> dict[Path, str]:
    """The install-local files the claude-code adapter renders from its templates:
    the carrier, the hook settings and the MCP registration. One builder for both
    `init` and `levain update`'s refresh, so the two cannot render different bytes."""
    # The @seed import list is roster-driven, not hard-coded — so a pack's added
    # seed file actually LOADS (install-to-disk without import = the bug this seam
    # fixes). Fill {{SEED_IMPORTS}} rather than copy the template byte-for-byte.
    claude_md = (adapter_root / "CLAUDE.md.template").read_text(encoding="utf-8")
    claude_md = _fill_seed_imports(claude_md, _claude_import_block(import_seed))
    claude_md = _fill_seed_on_demand(claude_md, _on_demand_block(on_demand_seed))

    settings_text = (adapter_root / "settings.template.json").read_text(encoding="utf-8")
    settings_text = _fill(settings_text, {"PYTHON": _hook_arg_body(python_path)})

    mcp_text = (adapter_root / "mcp.template.json").read_text(encoding="utf-8")
    # spore-751: the MCP server is `<levain's interpreter> -m anneal_memory`, so the anneal
    # that serves memory is structurally the one levain imports.
    mcp_text = _fill(mcp_text, {"INSTALL_DIR": _string_body(str(install)),
                                "PYTHON": _string_body(python_path)})
    return {
        install / "CLAUDE.md": claude_md,
        install / ".claude" / "settings.json": settings_text,
        install / ".mcp.json": mcp_text,
    }


def _install_codex(
    install: Path,
    adapter_root: Path,
    python_path: str,
    anneal_path: str,
    import_seed: Sequence[SeedEntry],
    on_demand_seed: Sequence[SeedEntry] = (),
    *,
    activation_roots: Sequence[Path],
    emit: Callable[[str], None] = print,
) -> None:
    # `activation_roots` was resolved up-front (run_init / apply_init) — base
    # (adapters/codex/activation, NOT templates/activation) first, then any pack
    # activation/ trees by order. base_activation passed explicitly for the
    # base-completeness check (a pack must not mask an empty base).
    _copy_activation_tree(
        activation_roots,
        install / "activation",
        base_activation=adapter_root / "activation",
        anneal_path=anneal_path,
        emit=emit,
    )
    (install / "AGENTS.md").write_text(
        _codex_agents_md(adapter_root, import_seed, on_demand_seed), encoding="utf-8")

    codex_home = _codex_home()
    codex_home.mkdir(parents=True, exist_ok=True)
    with codex_home_lock(codex_home):
        _write_codex_pair(codex_home, _codex_fragment(adapter_root, python_path, install),
                          _codex_hooks_json(adapter_root, python_path, install), emit)

    emit("  Codex adapter installed.")


ADAPTER_RECEIPT_REL = (".levain", "adapter-receipt.json")


def _read_adapter_receipt(install: Path) -> dict[str, str] | None:
    """``{key: sha256}`` of the bytes levain last wrote, or last staged under
    ``.levain/pending/`` (adopting a staged copy is accepting it), for the adapter files:
    the carrier, settings, MCP and base seeds by install-relative path, codex's machine-global
    files as ``codex-home/<name>``, and the codex config block. None when there is
    no usable record (an install from before it existed, or a damaged file). Separate
    from the activation receipt on purpose: that one describes the activation tree only,
    and doctor validates every entry in it against that tree."""
    try:
        data = json.loads(install.joinpath(*ADAPTER_RECEIPT_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _SHA256_HEX.fullmatch(v)
        for k, v in files.items()
    ):
        return None
    return dict(files)


def _write_adapter_receipt(install: Path, files: Mapping[str, str]) -> None:
    """Record the adapter files' written hashes (atomic). Raises OSError."""
    _ensure_gitignored(install / ".levain", ADAPTER_RECEIPT_REL[-1])
    _atomic_write_text(
        install.joinpath(*ADAPTER_RECEIPT_REL),
        json.dumps({"schema": 1, "files": dict(sorted(files.items()))}, indent=2) + "\n",
    )


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The bytes every release from v0.3.0 through v0.5.4 installed for each base verbatim
# seed file, read from `git show <tag>:levain/templates/seed/<name>` across those tags.
# Those releases recorded no hash for seed files, so this is how `levain update` tells
# an unedited copy from an operator's edit on such an install. Frozen: from 0.5.5 on,
# init and update record every base seed in the adapter receipt, so no later release
# adds a row. tests/test_seed_refresh.py re-derives it from the tags when git has them.
_RELEASED_BASE_SEED_SHA256: dict[str, frozenset[str]] = {
    "seed/partnership.md": frozenset({
        "6a56af919badacf27ff43d6aff1b022f7c2fd7674f8bb3b46ad02d270a820380",
        "c906ac694a0ff60b3b19eaba7a15a9819993f205bb0445ee8ea333cd1dbe64ec",
    }),
    "seed/memory.md": frozenset({
        "0397786e79cf6a30bf2b6adbd11d0f32da2e1b66f0e567f8f5fa587456a3b58b",
        "d0fda4b4362b6944b7be4a608dce8dc6fd002f6e941250f044d37603fa12e4cb",
        "884c95f0d8c74e30391bba3108013d712331ed34c2e4ea54add0e73beff15537",
        "fb09110a644e26f885a5a273cdb23545753e35e4a0174363574f6587ea416e7b",
        "daa5913e99f21cafd4b116a291895cc52e42a685d7e056e25aa1cd5c4851be19",
    }),
    "seed/spore_instructions.md": frozenset({
        "e23c7a439c852ebbc872bb0a1b14e4ca24f1a01485e34059c9b12c1baab0b670",
        "4b66417ef61a4e7c0fc3754d944967af94d819e93883bee6f263b8199a94a162",
        "01e246c4df5ab23c6d83e5151b72e034e340be90b4421322e5d7699350b4dd56",
    }),
}


def _base_seed_files(
    install: Path, templates_root: Path, entries: Sequence[SeedEntry],
) -> dict[Path, str]:
    """The base layer's verbatim seed files among ``entries``, as ``{install target: the
    text this levain ships}``. A file a pack supplies is left out (the pack reconcile owns
    it), and so is every rendered file (the interview owns those)."""
    base_seed = (templates_root / "seed").resolve()
    files: dict[Path, str] = {}
    for e in entries:
        if e.is_render or e.path.resolve().parent != base_seed:
            continue
        files[install / "seed" / e.name] = e.path.read_text(encoding="utf-8")
    return files


def _adapter_local_files(
    adapter: str,
    install: Path,
    adapter_root: Path,
    python_path: str,
    import_seed: Sequence[SeedEntry],
    on_demand_seed: Sequence[SeedEntry],
) -> dict[Path, str]:
    """The adapter's files INSIDE the install. Codex's hooks.json and config.toml live in
    the machine-global ``CODEX_HOME`` and are deliberately not in this set."""
    if adapter == "claude-code":
        return _claude_code_files(install, adapter_root, python_path, import_seed,
                                  on_demand_seed)
    if adapter == "codex":
        return {install / "AGENTS.md": _codex_agents_md(adapter_root, import_seed,
                                                        on_demand_seed)}
    return {}


def record_adapter_receipt(
    install: Path, adapter: str, templates_root: Path, python_path: str,
    import_seed: Sequence[SeedEntry], on_demand_seed: Sequence[SeedEntry],
    emit: Callable[[str], None] = print,
) -> None:
    """After an adapter install: record what was written, so a later ``levain update``
    can tell an operator's edit from a file the package has since moved. Best-effort."""
    files = _adapter_local_files(adapter, install, templates_root / "adapters" / adapter,
                                 python_path, import_seed, on_demand_seed)
    try:
        files = {**files, **_base_seed_files(install, templates_root,
                                             [*import_seed, *on_demand_seed])}
    except (OSError, ValueError):
        pass  # unrecorded seeds are treated as an older install's: staged, never clobbered
    if not files:
        return
    extra: dict[str, str] = {}
    if adapter == "codex":  # init writes codex's hooks.json + config block too (global)
        codex_root = templates_root / "adapters" / adapter
        files = {**files, _codex_home() / "hooks.json": _codex_hooks_json(
            codex_root, python_path, install)}
        block = _codex_block_hash(_codex_fragment(codex_root, python_path, install))
        if block is not None:
            extra[CODEX_CONFIG_KEY] = block
    try:
        _write_adapter_receipt(install, {
            **{_adapter_key(install, target): _sha256_text(text)
               for target, text in files.items()},
            **extra,
        })
    except OSError as e:
        emit(f"  note: could not record the adapter receipt ({e}); the next `levain "
             f"update` will back up the adapter files before refreshing them.")


@dataclass
class AdapterRefresh:
    """What :func:`refresh_adapter` did (or, without ``apply``, would do)."""

    refreshed: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)


def _expected_activation(
    layer_roots: Sequence[Path], anneal_path: str | None
) -> dict[str, tuple[bytes, Path]]:
    """``{rel: (bytes init would install, winning source)}`` for the composed activation
    tree: the source bytes, with ``{{ANNEAL_MEMORY}}`` substituted in every hook .py the way
    :func:`_substitute_hook_placeholders` does it."""
    out: dict[str, tuple[bytes, Path]] = {}
    for rel, src in _compose_activation_layers(layer_roots).items():
        data = src.read_bytes()
        if anneal_path is not None and rel.startswith("hooks/") and rel.endswith(".py"):
            try:
                data = data.decode("utf-8").replace(
                    "{{ANNEAL_MEMORY}}", _py_literal_body(anneal_path)).encode("utf-8")
            except UnicodeDecodeError:
                pass
        out[rel] = (data, src)
    return out


PENDING_REL = (".levain", "pending")


def refresh_adapter(
    install: Path, *, apply: bool, emit: Callable[[str], None] = print,
    carrier: bool = True,
) -> AdapterRefresh:
    """Bring the install's activation tree and adapter files up to what THIS levain
    renders, after ``pip install -U levain`` (gap #19: until now only ``levain init
    --force`` did, re-running the whole interview).

    Decided PER FILE (:func:`_refresh_decision`) from three hashes: the file on disk, what
    the package renders now, and what levain last wrote (the activation receipt;
    ``.levain/adapter-receipt.json`` for the carrier, settings, MCP and codex hooks). An
    operator's edit is never overwritten: where the package ALSO moved, the package's
    version is put beside the install under ``.levain/pending/`` and the file is listed
    for review ONCE (exit 1), after which the record holds the package's hash so later
    runs are quiet. With no record at all (an install from before the receipts), only
    activation hooks are replaced, because they are levain's own code and ``doctor``
    already calls a stale one a pending upgrade; the old copy is backed up first.
    Nothing is ever replaced whole, so a file the operator added is never moved.

    The base layer's verbatim seed files (partnership.md, memory.md, ...) are decided the
    same way, recorded in the adapter receipt. On an install from before that record, a
    copy whose bytes equal what some earlier release shipped (``_RELEASED_BASE_SEED_SHA256``)
    is replaced, with the old copy kept under ``.levain/backups/seed/``; any other copy is
    staged. Rendered seeds and a pack's seeds are not touched: the interview and the pack
    reconcile own those. So the carrier (CLAUDE.md / AGENTS.md, whose import list IS the
    seed set) and the base seeds are refreshed only when the caller says the pack layer is
    settled (``carrier``) and every seed the carrier would import is on disk; otherwise it
    would load a seed the reconcile held back. An
    activation file a drifted pack changed is never refreshed here: the pack reconcile owns
    it and lists it for review.
    Codex's machine-global hooks.json is replaced only while its bytes are what levain last
    recorded writing for this install (the adapter receipt); any other copy, a legacy
    install's included, is staged for review. config.toml is handled only when it already
    registers this install's store. Otherwise they are left alone, because replacing them
    repoints every codex session on the machine."""
    from levain import manifest

    install = install.resolve()  # every path compare below is against resolved paths
    out = AdapterRefresh()
    adapter = effective_adapter(install)
    if adapter not in ("claude-code", "codex", "openhands"):
        return out
    recorded, status = manifest.read_pack_locks_status(install)
    pack_dirs = [Path(p.source) for p in recorded]
    if status == "corrupt" or any(not (d / "pack.toml").is_file() for d in pack_dirs):
        out.review.append("adapter")
        emit("\n• adapter files NOT refreshed: a recorded pack's source is unreadable or "
             "gone, so the install cannot be re-composed faithfully. Restore it, or "
             "re-onboard (`levain init --force`).")
        return out
    if adapter == "openhands":  # hookless: its seed files are all there is to refresh
        return _refresh_seeds_only(install, pack_dirs, apply=apply, emit=emit, carrier=carrier)
    python_path = sys.executable
    anneal_path = manifest.resolve_anneal_bin()
    lines: list[str] = []
    with contextlib.ExitStack() as codex_held, _templates_root() as templates_root:
        if adapter == "codex" and apply:
            # Across the read of hooks.json/config.toml and both writes: another install's
            # init between them would otherwise be overwritten from a stale read.
            try:
                codex_held.enter_context(codex_home_lock(_codex_home()))
            except (CodexHomeBusy, CodexHomeUnlockable) as e:
                out.review.append("adapter")
                emit(f"\n• adapter files NOT refreshed, none of them (activation, carrier and "
                     f"codex's files alike): {e.message} Re-run `levain update`.")
                return out
        try:
            roster = compose_roster([templates_root, *pack_dirs])
            adapter_root = templates_root / "adapters" / adapter
            base_activation = _base_activation_root(adapter, templates_root)
            activation_roots = order_activation_roots(templates_root, base_activation,
                                                      pack_dirs)
            expected = _expected_activation(activation_roots, anneal_path)
            files = _adapter_local_files(adapter, install, adapter_root, python_path,
                                         import_entries(roster), on_demand_entries(roster))
            # The same files rendered with a sentinel interpreter: the leaves that differ
            # are the ones levain substitutes per machine (see _levain_render_variant).
            variants = {t.name: x for t, x in _adapter_local_files(
                adapter, install, adapter_root, _VARIABLE, import_entries(roster),
                on_demand_entries(roster)).items()}
            # `expected` is composed from the LIVE pack sources, so a path a pack changed since
            # it was recorded would be installed in the same run the pack reconcile lists it for
            # review (codex L3 on 19811f7, reproduced: pack hook v1 -> v2, update exit 1,
            # installed bytes v2). Hold exactly those paths; everything else still refreshes.
            # Inside the try so an unreadable pack file fails closed (complement L3 r3). A pack
            # source edited AND reverted while this runs is not caught (codex L3 r3, routed).
            held = _pack_held_activation(recorded)
        except (PackError, InitError, OSError, ValueError) as e:
            out.review.append("adapter")
            emit(f"\n• adapter files NOT refreshed: the package could not be composed ({e}).")
            return out
        _refresh_activation(install, expected, apply=apply, out=out, lines=lines, held=held)
        carrier_name = "CLAUDE.md" if adapter == "claude-code" else "AGENTS.md"
        missing = sorted(e.name for e in [*import_entries(roster), *on_demand_entries(roster)]
                         if not (install / "seed" / e.name).is_file())
        held = None
        if not carrier:
            held = "the pack reconcile above is holding seed changes for review"
        elif missing:
            held = f"it would load seed file(s) not installed yet: {', '.join(missing)}"
            out.review.append(carrier_name)  # not a clean install: exit 1 until settled
        if held is not None:
            files = {t: x for t, x in files.items() if t.name != carrier_name}
            lines.append(f"  {carrier_name}: not refreshed, because {held}; re-onboard "
                         f"(`levain init --force`) or settle that first.")
        else:
            try:
                files = {**files, **_base_seed_files(
                    install, templates_root,
                    [*import_entries(roster), *on_demand_entries(roster)])}
            except (OSError, ValueError) as e:
                out.review.append("seed")
                lines.append(f"  seed files: not refreshed, because this levain's copies "
                             f"could not be read ({e}).")
        if adapter == "codex":
            hooks = _codex_home() / "hooks.json"
            want = _codex_hooks_json(adapter_root, python_path, install)
            try:
                here = _read_or_none(hooks)
            except _Unreadable as e:
                here = None
                out.review.append(str(hooks))
                lines.append(f"  {hooks}: could not be read ({e}), so whether it is "
                             f"current, or this install's, cannot be told.")
            if here is not None and here != want:
                try:
                    json.loads(here)
                except (ValueError, RecursionError):
                    # A note, never a gate: the receipt below still decides, so levain's own
                    # unreadable write (an install path holding `"` or `\` before 0.7.0) is
                    # replaced, and anyone else's is staged.
                    lines.append(f"  note: {hooks} is not valid JSON, so codex cannot read its "
                                 f"hooks.")
            if here is not None:
                # The adapter receipt alone decides (_refresh_decision): replaced only while
                # its bytes are what levain last wrote for THIS install; anything else,
                # another install's file, an edit, or a file from before the receipt, is
                # staged under .levain/pending/ and never overwritten. Ownership is not
                # inferred from the file's text (spore-866 L3 r1-r7; Phill 2026-10-10 "go
                # with B"), the way dpkg treats a conffile.
                files = {**files, hooks: want}
        _refresh_adapter_files(install, files, apply=apply, out=out, lines=lines,
                               variants=variants)
        if adapter == "codex":
            _refresh_codex_config(install, adapter_root, python_path, apply=apply, out=out,
                                  lines=lines)
    if out.refreshed or lines:
        emit(f"\n• adapter files ({adapter}):")
        verb = "refreshed" if apply else "would refresh"
        for rel in out.refreshed:
            emit(f"  {rel}: {verb} to this levain's version")
        for ln in lines:
            emit(ln)
    return out


def _refresh_seeds_only(
    install: Path, pack_dirs: Sequence[Path], *, apply: bool, emit: Callable[[str], None],
    carrier: bool,
) -> AdapterRefresh:
    """:func:`refresh_adapter` for a hookless (OpenHands) install: the base verbatim seeds,
    by the same decision and receipt, and nothing else (it has no activation tree and no
    adapter files). Held on the same conditions as the carrier."""
    out = AdapterRefresh()
    lines: list[str] = []
    with _templates_root() as templates_root:
        try:
            roster = compose_roster([templates_root, *pack_dirs])
            entries = [*import_entries(roster), *on_demand_entries(roster)]
            files = _base_seed_files(install, templates_root, entries)
        except (PackError, InitError, OSError, ValueError) as e:
            out.review.append("seed")
            emit(f"\n• seed files NOT refreshed: the package could not be composed ({e}).")
            return out
        missing = sorted(e.name for e in entries if not (install / "seed" / e.name).is_file())
        if not carrier:
            lines.append("  seed files: not refreshed, because the pack reconcile above is "
                         "holding seed changes for review.")
        elif missing:
            out.review.append("seed")
            lines.append(f"  seed files: not refreshed, because seed file(s) are missing: "
                         f"{', '.join(missing)}; re-onboard (`levain init --force`).")
        else:
            _refresh_adapter_files(install, files, apply=apply, out=out, lines=lines)
    if out.refreshed or lines:
        emit("\n• seed files:")
        verb = "refreshed" if apply else "would refresh"
        for rel in out.refreshed:
            emit(f"  {rel}: {verb} to this levain's version")
        for ln in lines:
            emit(ln)
    return out


class _Unreadable(Exception):
    pass


def _read_or_none(path: Path) -> str | None:
    """The file's text, None when absent; raises :class:`_Unreadable` otherwise, so an
    unreadable file is never mistaken for an empty or foreign one."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise _Unreadable(str(e)) from None


def _refresh_decision(
    here: bytes | None, want: bytes, last: str | None, *, levain_code: bool,
    legacy: bool = False,
) -> str:
    """What to do with one file. ``here`` is its bytes on disk (None: absent), ``want``
    what the package renders now, ``last`` the hash levain last recorded for it (None: no
    record). Returns ``current`` | ``write`` | ``write_backup`` | ``keep`` | ``pending``.

    ``pending`` = keep the operator's file, put the package's version under
    .levain/pending/, list it for review, and record the package's hash so the review
    fires once per package change rather than on every run."""
    import hashlib

    def sha(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    if here == want:
        return "current"
    if here is None:
        if last is None:
            # New in this release, unless there is no record at all (an install from
            # before the receipt), where an absent file may be one the operator deleted.
            return "pending" if legacy and not levain_code else "write"
        return "keep" if sha(want) == last else "pending"  # the operator deleted it
    if last is None:
        return "write_backup" if levain_code else "pending"
    if sha(here) == last:
        return "write"  # unedited since levain wrote it
    return "keep" if sha(want) == last else "pending"


def _put_pending(install: Path, key: str, data: bytes) -> Path:
    target = install.joinpath(*PENDING_REL, *key.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    _ensure_gitignored(install / ".levain", PENDING_REL[-1] + "/")
    _atomic_write_bytes(target, data)
    return target


def _drop_pending(install: Path, key: str, lines: list[str], review: list[str]) -> None:
    """Remove ``key``'s staged copy once the file is settled (current, or written by levain).
    A copy left behind is an older release's render: adopting it later downgrades the file,
    and the next update calls the downgrade ``keep``, because the record already holds the
    newer hash (reproduced through `levain update` 2026-10-10)."""
    target = install.joinpath(*PENDING_REL, *key.split("/"))
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        review.append(key)  # not settled until it is gone: update stays nonzero
        lines.append(f"  {key}: could not remove its stale staged copy {target} ({e}); it is "
                     f"an older version, so do not copy it over the file. Remove it yourself.")
        return
    lines.append(f"  {key}: its staged copy under .levain/pending/ is settled and removed.")


def _pack_held_activation(recorded: Sequence) -> set[str]:
    """Activation paths (relative to ``activation/``) that a recorded pack has added,
    changed or removed since it was recorded. The pack reconcile surfaces these for review
    and keeps their old provenance, so the adapter refresh must leave them alone."""
    from levain import manifest

    prefix = "activation/"
    return {rel[len(prefix):] for d in manifest.compute_pack_drift(recorded) if d.drifted
            for rel in (*d.added, *d.modified, *d.removed) if rel.startswith(prefix)}


def _refresh_activation(
    install: Path,
    expected: Mapping[str, tuple[bytes, Path]],
    *,
    apply: bool,
    out: AdapterRefresh,
    lines: list[str],
    held: set[str] = frozenset(),
) -> None:
    import hashlib

    dst = install / "activation"
    if dst.is_symlink():
        lines.append(f"  note: activation/ is a symlink to {os.readlink(dst)}; it is yours "
                     f"to manage, so it was not refreshed.")
        return

    unreadable: list[str] = []

    def disk(rel: str) -> bytes | None:
        try:
            return (dst / rel).read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            unreadable.append(rel)
            return None  # never b"": an unreadable file must not read as an empty one

    receipt, status = read_activation_receipt(install)
    if status == "ok" and all(disk(rel) == want for rel, (want, _src) in expected.items()) \
            and set(receipt or {}) <= set(expected):
        if apply:
            for rel in sorted(expected):
                _drop_pending(install, f"activation/{rel}", lines, out.review)
        return
    if status in ("corrupt", "empty"):
        out.review.append("activation/")
        lines.append(f"  activation/: NOT refreshed — its install receipt is unreadable "
                     f"({status}), so your edits cannot be told from the package's files. "
                     f"Re-onboard (`levain init --force`) to replace it whole.")
        return
    record = receipt or {}
    legacy = status == "absent"
    new_receipt = dict(record)
    stamp = time.time_ns()
    # Files levain installed that this release no longer ships are NAMED and dropped from
    # the record, never deleted. Deleting was tried and removed (codex L3 r2, reproduced): a
    # receipt key like "../../x" made update delete a file outside the install, and the
    # check-then-unlink raced an editor's save. Removing a file is the operator's call.
    for rel in sorted(held):
        out.review.append(f"activation/{rel}")  # unsettled executable drift: never exit 0
        lines.append(f"  activation/{rel}: not refreshed, because a pack changed it and the "
                     f"pack reconcile above is holding it for review.")
    expected = {rel: v for rel, v in expected.items() if rel not in held}
    for rel in sorted(set(record) - set(expected) - held):
        del new_receipt[rel]
        lines.append(f"  note: activation/{rel} is no longer shipped by this levain; it is "
                     f"left in place (delete it yourself if you do not want it).")
    for rel, (want, src) in sorted(expected.items()):
        here = disk(rel)
        if rel in unreadable:
            out.review.append(f"activation/{rel}")
            lines.append(f"  activation/{rel}: could not be read, so whether it is current "
                         f"cannot be told.")
            continue
        last = record[rel]["installed"] if rel in record else None
        action = _refresh_decision(here, want, last, levain_code=rel.startswith("hooks/")
                                   and rel.endswith(".py"), legacy=legacy)
        entry = {"installed": hashlib.sha256(want).hexdigest(),
                 "source": _sha256_stream(src)}
        if action == "keep":
            continue
        key = f"activation/{rel}"
        if action == "current":
            if rel not in record:
                new_receipt[rel] = entry  # a pre-receipt install gains its record
            if apply:
                _drop_pending(install, key, lines, out.review)
            continue
        if action == "pending":
            out.review.append(key)
            if apply:
                try:
                    where = _put_pending(install, key, want)
                    new_receipt[rel] = entry
                except OSError as e:
                    lines.append(f"  {key}: could not stage the package's version ({e}).")
                    continue
            else:
                where = install.joinpath(*PENDING_REL, *key.split("/"))
            lines.append(f"  {key}: yours is kept (edited, or levain has no record of "
                         f"writing it), and this levain ships a different version, now at "
                         f"{where}. Merge it in, or copy it over yours if you never edited "
                         f"it. Listed once: the next update is quiet about it.")
            continue
        out.refreshed.append(key)
        if not apply:
            continue
        target = dst / rel
        try:
            if action == "write_backup" and here is not None:
                backup = install / ".levain" / "backups" / "activation" / f"files-{stamp}" / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                lines.append(f"  {key}: levain has no record of writing it, so the "
                             f"previous copy is kept at {backup}")
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(target, want, like=src)
            new_receipt[rel] = entry
            _drop_pending(install, key, lines, out.review)
        except OSError as e:
            out.refreshed.pop()
            out.review.append(key)
            lines.append(f"  {key}: could not be written ({e}).")
    if apply and new_receipt != record:
        try:
            _write_activation_receipt(install, new_receipt)
        except OSError as e:
            lines.append(f"  note: could not update the activation receipt ({e}); the next "
                         f"`levain update` re-checks these files.")


def _atomic_write_bytes(target: Path, data: bytes, *, like: Path | None = None) -> None:
    """Unique temp + fsync + ``os.replace``, keeping ``like``'s permission bits (a hook
    script must stay executable). A symlinked target is resolved first, so the LINK
    survives and its target gets the bytes (the class ``_atomic_write_text`` was fixed for:
    a dotfiles-managed file would otherwise turn into a plain file)."""
    target = target.resolve()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if like is not None:
            os.chmod(tmp, stat.S_IMODE(like.stat().st_mode))
        elif target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _string_body(value: str) -> str:
    """``value`` as the body of a double-quoted string literal, for a template slot that sits
    inside one: JSON, a TOML basic string or a Python ``"..."`` (the hooks' anneal path).
    spore-866: a raw path with a `\\`, `"` or newline broke the file around it (and could
    inject keys or code). JSON's escapes with ensure_ascii=False, plus DEL as ``\\u007f``
    (TOML forbids it raw), are valid in all three; the ASCII form would emit surrogate pairs
    TOML rejects. A lone surrogate (a path whose bytes are not UTF-8) cannot be written to a
    UTF-8 file at all, so it is refused by name rather than crashing the write."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InitError(f"{value!r} is not valid UTF-8, so levain cannot write it into a "
                        f"config file; use a path whose name is UTF-8.") from None
    return json.dumps(value, ensure_ascii=False)[1:-1].replace("\x7f", "\\u007f")


def _hook_arg_body(value: str) -> str:
    """``value`` for a hook ``command`` template slot: one argument inside shell double
    quotes, inside a JSON string. `\\` and `"` are backslashed; `$` and a backquote leave the
    double quotes for a single-quoted segment (``"'$'"``), because inside double quotes
    ``shlex.split`` (doctor's and verify's reader) keeps a backslash before them that sh drops
    (L3 r1, run). Windows paths cannot hold `"`, and cmd does not treat `\\` as an escape, so
    there only the JSON escaping applies."""
    if os.name != "nt":
        value = re.sub(r'([\\"])', r"\\\1", value)
        value = re.sub(r"([$`])", "\"'\\1'\"", value)
    return _string_body(value)


def _py_literal_body(value: str) -> str:
    """``value`` as the body of a Python string literal in either quote style, for the hooks'
    ``{{ANNEAL_MEMORY}}`` (base hooks use ``"..."``, a pack hook may use ``'...'``; codex L3
    r1). `_string_body`'s escapes are all valid Python; `'` is added for the single-quoted form."""
    return _string_body(value).replace("'", "\\'")


_SLOT_RE = re.compile(r"\{\{(PYTHON|INSTALL_DIR)\}\}")


def _fill(template: str, slots: Mapping[str, str]) -> str:
    """Fill ``{{PYTHON}}`` / ``{{INSTALL_DIR}}`` in one pass over the template, so a value that
    itself contains a slot name is never filled again (codex L3 r1: an install path holding
    ``{{PYTHON}}`` became the interpreter path)."""
    def one(m: re.Match[str]) -> str:
        if m.group(1) not in slots:
            raise InitError(f"a levain template holds {{{{{m.group(1)}}}}}, which its renderer "
                            f"does not fill.")
        return slots[m.group(1)]
    return _SLOT_RE.sub(one, template)


CODEX_CONFIG_KEY = "codex-home/config.toml#anneal_memory"


def _codex_fragment(adapter_root: Path, python_path: str, install: Path) -> str:
    fragment = (adapter_root / "mcp.template.toml").read_text(encoding="utf-8")
    return _fill(fragment, {"PYTHON": _string_body(python_path),
                            "INSTALL_DIR": _string_body(str(install))})


def _splice_codex_block(existing: str, new_block: str) -> str:
    """``existing`` with its ``[mcp_servers.anneal_memory]`` block replaced by ``new_block``,
    or ``new_block`` appended after a blank line when there is none."""
    if _CODEX_MCP_BLOCK_RE.search(existing):
        # `new_block` is data, not a template: a literal replacement, so a store path
        # containing a backslash cannot be read as a group reference and corrupt the file.
        return _CODEX_MCP_BLOCK_RE.sub(lambda _m: new_block, existing, count=1)
    if not existing.endswith("\n"):
        existing += "\n"
    if not existing.endswith("\n\n"):
        existing += "\n"
    return existing + new_block


def _toml_error(text: str) -> str | None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return str(e)
    return None


def _require_toml(text: str, path: Path, before: str | None) -> str | None:
    """Refuse (``InitError``) when levain's change is what would stop codex's machine-global
    ``path`` parsing: a parse failure there takes down every MCP server codex has, not only
    levain's (spore-866). A file that did not parse BEFORE the change either (``before``) is not
    levain's to judge, since codex's parser may accept what ``tomllib`` refuses: returns the
    error it has AFTER the change, for the caller to report once the write has landed;
    otherwise None."""
    err = _toml_error(text)
    if err is None:
        return None
    if before is not None and _toml_error(before) is not None:
        return err
    raise InitError(f"{path} would not parse after levain's change ({err}), so levain did not "
                    f"write it.")


def _codex_block_hash(fragment_or_config: str) -> str | None:
    m = _CODEX_MCP_BLOCK_RE.search(fragment_or_config)
    return None if m is None else _sha256_text(m.group(0).rstrip() + "\n")


def _refresh_codex_config(
    install: Path,
    adapter_root: Path,
    python_path: str,
    *,
    apply: bool,
    out: AdapterRefresh,
    lines: list[str],
) -> None:
    """config.toml's ``[mcp_servers.anneal_memory]`` block, only when it already registers
    THIS install's store (so the write is never a repoint), and then three-way like every
    other file: replaced when it is the block levain recorded writing, or (no record) a
    launcher levain itself writes with nothing of the operator's around it; otherwise the
    new block is staged under .levain/pending/ and listed once. ``_merge_codex_config``
    keeps its own backup and says what it replaced."""
    config = _codex_home() / "config.toml"
    fragment = _codex_fragment(adapter_root, python_path, install)
    try:
        existing = _read_or_none(config)
    except _Unreadable as e:
        out.review.append(str(config))
        lines.append(f"  {config}: could not be read ({e}), so whether codex's memory "
                     f"registration is current cannot be told.")
        return
    if not existing:
        return
    old_block = _CODEX_MCP_BLOCK_RE.search(existing)
    new_block = _CODEX_MCP_BLOCK_RE.search(fragment)
    if old_block is None or new_block is None:
        return
    old_text, new_text = old_block.group(0).rstrip() + "\n", new_block.group(0).rstrip() + "\n"
    if old_text == new_text:
        if apply:
            _drop_pending(install, "codex-home/config.toml.anneal_memory", lines, out.review)
        return
    old_store = _codex_block_store(old_text)
    if old_store != _codex_block_store(new_text):
        lines.append(f"  note: {config} registers another store for codex, so `levain "
                     f"update` left it alone (rewriting it repoints every codex session).")
        return
    record = (_read_adapter_receipt(install) or {}).get(CODEX_CONFIG_KEY)
    old_dict, new_dict = _codex_block_dict(old_text), _codex_block_dict(new_text)
    if record is not None:
        levains = _sha256_text(old_text) == record
    else:
        levains = (_is_levain_launcher(old_dict, old_store)
                   and _without_levain_keys(old_dict) == _without_levain_keys(new_dict))
    if not levains:
        if record == _sha256_text(new_text):
            return  # already listed once against this version
        out.review.append(CODEX_CONFIG_KEY)
        where = install.joinpath(*PENDING_REL, "codex-home", "config.toml.anneal_memory")
        if apply:
            try:
                _put_pending(install, "codex-home/config.toml.anneal_memory",
                             new_text.encode("utf-8"))
                _record_adapter_key(install, CODEX_CONFIG_KEY, _sha256_text(new_text))
            except OSError as e:
                lines.append(f"  {config}: could not stage levain's block ({e}).")
                return
        lines.append(f"  {config} [mcp_servers.anneal_memory]: yours is kept (it carries "
                     f"settings levain did not write), and this levain renders it "
                     f"differently, now at {where}. Merge it in. Listed once.")
        return
    out.refreshed.append(f"{config} [mcp_servers.anneal_memory]")
    if apply:
        try:
            _merge_codex_config(config, fragment, emit=lines.append)
            _record_adapter_key(install, CODEX_CONFIG_KEY, _sha256_text(new_text))
            _drop_pending(install, "codex-home/config.toml.anneal_memory", lines, out.review)
        except (OSError, InitError) as e:
            out.refreshed.pop()
            out.review.append(CODEX_CONFIG_KEY)
            lines.append(f"  {config}: could not be written "
                         f"({getattr(e, 'message', None) or e}).")


def _record_adapter_key(install: Path, key: str, digest: str) -> None:
    files = _read_adapter_receipt(install)
    if files is None and install.joinpath(*ADAPTER_RECEIPT_REL).exists():
        return  # unreadable: rewriting it with one key would drop every other record
    files = files or {}
    files[key] = digest
    _write_adapter_receipt(install, files)


def _adapter_key(install: Path, target: Path) -> str:
    """The adapter receipt's key: a path inside the install, or ``codex-home/<name>`` for a
    file in the machine-global CODEX_HOME."""
    try:
        return target.relative_to(install).as_posix()
    except ValueError:
        return f"codex-home/{target.name}"


_VARIABLE = "\ue000levain-variable\ue001"


def _levain_render_variant(
    here: str, want: str, variant: str, *, install: Path, mcp: bool = False
) -> bool:
    """Whether ``here`` (a JSON file with no record) is levain's own render from another
    machine or release: the same keys and list lengths as ``want``, differing only at
    leaves that are a per-machine variable (the leaf in ``variant``, rendered with a
    sentinel interpreter, holds the sentinel). With ``mcp``, the anneal_memory server's
    ``command``/``args`` are levain's launcher (spore-751 changed its shape) and are set
    aside, but only while the old args still serve THIS install's store."""
    try:
        x, y, v = json.loads(here), json.loads(want), json.loads(variant)
    except ValueError:
        return False
    if mcp:
        try:
            store = str(install / ".levain" / "memory.db")
            # Only a launcher levain itself writes (the same test the codex config uses):
            # a wrapper command or an extra flag is the operator's (codex L3 r2).
            if not _is_levain_launcher({"mcp_servers": {
                    "anneal_memory": x["mcpServers"]["anneal_memory"]}}, store):
                return False
            for tree in (x, y, v):
                tree["mcpServers"]["anneal_memory"].pop("command", None)
                tree["mcpServers"]["anneal_memory"].pop("args", None)
        except (KeyError, TypeError, AttributeError, ValueError):
            return False

    def same(a: object, b: object, c: object) -> bool:
        if isinstance(a, dict) and isinstance(b, dict) and isinstance(c, dict):
            return (a.keys() == b.keys() == c.keys()
                    and all(same(a[k], b[k], c[k]) for k in a))
        if isinstance(a, list) and isinstance(b, list) and isinstance(c, list):
            return len(a) == len(b) == len(c) and all(map(same, a, b, c))
        if any(isinstance(t, (dict, list)) for t in (a, b, c)):
            return False
        return a == b or (isinstance(c, str) and _VARIABLE in c)

    return same(x, y, v)


def _refresh_adapter_files(
    install: Path,
    files: Mapping[Path, str],
    *,
    apply: bool,
    out: AdapterRefresh,
    lines: list[str],
    variants: Mapping[str, str] | None = None,
) -> None:
    receipt = _read_adapter_receipt(install)
    record = receipt or {}
    new_receipt = dict(record)
    for target, text in files.items():
        key = _adapter_key(install, target)
        want_text = text
        want = text.encode("utf-8")
        try:
            here_text: str | None = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            here_text = None
        except (OSError, ValueError) as e:
            out.review.append(key)
            lines.append(f"  {key}: could not be read ({e}), so whether it is current "
                         f"cannot be told.")
            continue
        here = None if here_text is None else here_text.encode("utf-8")
        action = _refresh_decision(here, want, record.get(key), levain_code=False)
        variant = (variants or {}).get(target.name)
        if (action == "pending" and key not in record and here_text is not None
                and variant is not None and target.suffix == ".json"
                and _levain_render_variant(here_text, want_text, variant, install=install,
                                           mcp=target.name == ".mcp.json")):
            # No record, but the file differs from this release's render ONLY where levain
            # substitutes a per-machine value (the interpreter path), or, in .mcp.json,
            # only in its own anneal_memory launcher for this install's store: an earlier
            # release's render, not an operator's file. Anything else is staged. The old
            # copy is still kept.
            action = "write_backup"
        released_copy = False
        if (action == "pending" and key not in record and here is not None
                and _sha256_text(here_text) in _RELEASED_BASE_SEED_SHA256.get(key, ())):
            # No record (an install from before seeds were recorded), and the bytes are
            # exactly what an earlier release installed: levain's copy, unedited.
            action = "write_backup"
            released_copy = True
        if action == "keep":
            continue
        if action == "current":
            new_receipt[key] = _sha256_text(want_text)
            if apply:
                _drop_pending(install, key, lines, out.review)
            continue
        if action == "pending":
            out.review.append(key)
            if apply:
                try:
                    where = _put_pending(install, key, want)
                    new_receipt[key] = _sha256_text(want_text)
                except OSError as e:
                    lines.append(f"  {key}: could not stage levain's version ({e}).")
                    continue
            else:
                where = install.joinpath(*PENDING_REL, *key.split("/"))
            if key.startswith("codex-home/"):
                lines.append(f"  {target}: kept as it is (edited, another install's, or levain "
                             f"has no record of writing it for this install), and this levain "
                             f"renders it differently, now at {where}. Copying that over it "
                             f"points every codex session on this machine at this install. "
                             f"Listed once: the next update is quiet about it.")
                continue
            lines.append(f"  {key}: yours is kept (edited, or levain has no record of "
                         f"writing it), and this levain renders it differently, now at "
                         f"{where}. Merge it in, or copy it over yours if you never edited "
                         f"it. Listed once: the next update is quiet about it.")
            continue
        out.refreshed.append(str(target) if key.startswith("codex-home/") else key)
        if not apply:
            continue
        try:
            if key.startswith("codex-home/"):
                _write_codex_hooks(target, want_text, lines.append)  # backs up itself
            else:
                if action == "write_backup" and here_text is not None:
                    is_seed = key.startswith("seed/")
                    if is_seed:
                        # Not beside it: seed/ is the entity's own directory.
                        _ensure_gitignored(install / ".levain", "backups/")
                        backup = install.joinpath(
                            ".levain", "backups", "seed",
                            f"{target.name}.{time.time_ns()}")
                        backup.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        backup = _timestamped_backup_path(target)
                    shutil.copy2(target, backup)
                    shown = backup.relative_to(install) if is_seed else backup.name
                    if released_copy:
                        lines.append(f"  {key}: an earlier release's copy, unedited; kept "
                                     f"at {shown}")
                    else:
                        lines.append(f"  {key}: levain has no record of writing it, so the "
                                     f"previous copy is kept at {shown}")
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(target, want_text)
            new_receipt[key] = _sha256_text(want_text)
            _drop_pending(install, key, lines, out.review)
        except OSError as e:
            out.refreshed.pop()
            out.review.append(key)
            lines.append(f"  {key}: could not be written ({e}).")
    if apply and new_receipt != record:
        try:
            _write_adapter_receipt(install, new_receipt)
        except OSError as e:
            lines.append(f"  note: could not record the adapter receipt ({e}).")


def _first_foreign_line(spec: TemplateSpec, text: str, record: Mapping[str, str]) -> str:
    """Where a refused seed first departs from a render of the current record: a pointer
    for the operator (the record may itself be stale, so it is a hint, not a diagnosis)."""
    import difflib

    from levain.interview import render_template

    ours = render_template(spec, dict(record)).splitlines()
    theirs = text.splitlines()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=ours, b=theirs).get_opcodes():
        if tag in ("replace", "insert") and j1 < j2:
            return f" The first line that departs from your recorded answers is line {j1 + 1}: {theirs[j1][:80]!r}."
    return ""


def run_adopt_answers(
    path: Path, *, dry_run: bool = False, emit: Callable[[str], None] = print
) -> int:
    """``levain adopt-answers``: make the install's RENDERED seed files the interview record
    (``.levain/answers.json``), so a hand edit stops being something the next re-render
    (``init --force`` from the record, a pack render reconcile) silently reverts
    (spore-423; reproduced: an edited LOCATION in world.md was reverted by a re-onboard
    from the record, with no backup of the seed).

    ALL-OR-REFUSE: every render seed must read back to exactly one answer set that
    re-renders to it (``answers.extract_answers``), and the result must pass the same
    validation as ``init --answers``. Anything less writes nothing and exits 1."""
    install = Path(str(path)).expanduser().resolve()
    if dry_run:
        return _adopt_answers(install, dry_run=True, emit=emit)
    try:
        # Held across the READS too: a concurrent init/update between reading the seeds
        # and writing the record would otherwise be overwritten by a stale reading.
        with install_lock(install, create=False):
            return _adopt_answers(install, dry_run=False, emit=emit)
    except InstallLockError as e:
        emit(f"FAIL: {e.message}")
        emit("  Nothing was written; the record is unchanged.")
        return 1


def _adopt_answers(install: Path, *, dry_run: bool, emit: Callable[[str], None]) -> int:
    from levain import manifest
    from levain.answers import AdoptRefused, extract_answers, validate_answers
    from levain.interview import build_field_plan, parse_template, render_template

    def refuse(why: str) -> int:
        emit(f"FAIL: {why}")
        emit("  Nothing was written; the record is unchanged.")
        return 1

    if not (install / "seed").is_dir():
        return refuse(f"{install} has no seed/ directory, so it is not a levain install.")
    recorded, status = manifest.read_pack_locks_status(install)
    drifted = [d.name for d in manifest.compute_pack_drift(recorded) if d.drifted]
    if drifted:
        return refuse(f"pack(s) {', '.join(drifted)} changed since the seeds were rendered, "
                      f"so the templates read here are not the ones your seeds came from. "
                      f"Run `levain update` first.")
    old = read_answers(install)
    if status == "corrupt":
        return refuse("the lock's pack provenance is unreadable, so which templates this "
                      "install was rendered from cannot be known.")
    pack_dirs = [Path(p.source) for p in recorded]
    gone = [str(d) for d in pack_dirs if not (d / "pack.toml").is_file()]
    if gone:
        return refuse(f"a recorded pack's source is gone ({', '.join(gone)}), so its "
                      f"templates cannot be read.")
    with _templates_root() as templates_root:
        try:
            roster = compose_roster([templates_root, *pack_dirs])
            specs = [parse_template(e.path) for e in render_entries(roster)]
        except (PackError, OSError, ValueError) as e:
            return refuse(f"the install's templates could not be composed ({e}).")
        merged: dict[str, str] = {}
        whitespace_only: list[str] = []
        for spec in specs:
            name = spec.path.name
            try:
                text = (install / "seed" / name).read_text(encoding="utf-8")
            except (OSError, ValueError) as e:
                return refuse(f"seed/{name} could not be read ({e}).")
            slots = [sl for sec in spec.sections for sl in sec.slots]
            optional = [sec.slots for sec in spec.sections if sec.title and sec.optional]
            try:
                got = extract_answers(text, slots=slots, optional_sections=optional,
                                      render=lambda a, spec=spec: render_template(spec, a),
                                      prior=old)
            except AdoptRefused as e:
                return refuse(f"seed/{name}: {e}"
                              f"{_first_foreign_line(spec, text, read_answers(install))}")
            if render_template(spec, got) != text:
                whitespace_only.append(name)
            for slot, value in got.items():
                if merged.setdefault(slot, value) != value:
                    return refuse(f"{slot} reads differently in two seed files; make them "
                                  f"agree, then re-run.")
    errors = validate_answers(build_field_plan(specs), merged)
    if errors:
        return refuse("what the seed files say is not a valid record:\n    "
                      + "\n    ".join(errors))
    if old:
        # A slot the record never held was never asked. Recording it as "" would make
        # `update` treat it as answered and re-render it blank (L1, reproduced: a field
        # an upstream template added after the seed was rendered).
        unasked = sorted(sl for sl in merged if sl not in old and not merged[sl])
        if unasked:
            return refuse(f"the template asks {', '.join(unasked)}, which your record never "
                          f"answered and your seed files do not fill. Answer it in the seed "
                          f"(or re-onboard), then re-run.")
    changed = sorted(sl for sl in merged if old.get(sl) != merged[sl])
    dropped = sorted(set(old) - set(merged))
    if not changed and not dropped:
        emit("The seed files already match the record; nothing to adopt.")
        return 0

    def show(value: str | None) -> str:
        if value is None:
            return "(absent)"
        one = value.replace("\n", " / ")
        return repr(one if len(one) <= 60 else one[:57] + "...")

    emit(f"{'Would adopt' if dry_run else 'Adopting'} {len(changed)} field(s) from the "
         f"seed files into {install / '.levain' / 'answers.json'}:")
    for sl in changed:
        emit(f"  {sl}: {show(old.get(sl))} -> {show(merged[sl])}")
    for sl in dropped:
        emit(f"  {sl}: {show(old[sl])} -> dropped (no template asks it any more)")
    for name in whitespace_only:
        emit(f"  note: seed/{name} differs from a render only in whitespace; a re-render "
             f"would tidy it.")
    if dry_run:
        emit("--dry-run: nothing was written.")
        return 0
    if not write_answers(install, merged, emit):
        return refuse("the record could not be written.")
    emit("Recorded. A re-render from the record now reproduces your seed files.")
    return 0


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def _codex_agents_md(
    adapter_root: Path, import_seed: Sequence[SeedEntry], on_demand_seed: Sequence[SeedEntry]
) -> str:
    # Roster-driven read-list (same load-side seam as claude-code): a pack's added
    # seed file appears in the numbered "read these, in order" list it must load.
    agents_md = (adapter_root / "AGENTS.md.template").read_text(encoding="utf-8")
    agents_md = _fill_seed_imports(agents_md, _codex_import_block(import_seed))
    return _fill_seed_on_demand(agents_md, _on_demand_block(on_demand_seed))


def _write_codex_hooks(hooks_target: Path, hooks_text: str,
                       emit: Callable[[str], None]) -> None:
    if hooks_target.exists() or hooks_target.is_symlink():
        # Timestamped backup so repeated re-runs accrete instead of clobber.
        bak = _timestamped_backup_path(hooks_target)
        # `shutil.copy2` preserves perms/mtime AND is atomic-from-the-reader's-side;
        # `read_text`+`write_text` had a tiny window where Ctrl+C lost the original.
        # A link that resolves is backed up by its target's bytes; a dangling one has no
        # bytes, so the link itself is kept (following it raised FileNotFoundError before
        # the write, glm L3 r3 2026-10-10, reproduced through `levain init`).
        shutil.copy2(hooks_target, bak, follow_symlinks=hooks_target.exists())
        emit(f"  ! Existing {hooks_target} backed up to {bak}")
        emit("    (Codex is one-install-per-machine at v1 — this install now owns it.)")
    # Written beside it, fsynced, then renamed over the PATH: a failed or interrupted write
    # leaves the old file in place (an unlink then a write left none, reproduced with ENOSPC),
    # and a symlink into a dotfiles repo is replaced, never written through.
    try:
        mode: int | None = hooks_target.stat().st_mode & 0o777  # through a link: the target's
    except FileNotFoundError:
        mode = None  # a new file keeps mkstemp's 0600, never wider than the umask allowed
    fd, tmp = tempfile.mkstemp(dir=hooks_target.parent, prefix=f".{hooks_target.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(hooks_text)
            f.flush()
            if mode is not None and hasattr(os, "fchmod"):  # Windows has it from 3.13
                os.fchmod(f.fileno(), mode)  # before the fsync, so the mode is durable too
            os.fsync(f.fileno())
        if mode is not None and not hasattr(os, "fchmod"):
            os.chmod(tmp, mode)
        os.replace(tmp, hooks_target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):  # the rename itself, durable where the OS allows it
        dfd = os.open(hooks_target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _write_codex_pair(codex_home: Path, fragment: str, hooks_text: str,
                     emit: Callable[[str], None]) -> None:
    """Write codex's two machine-global files as one unit, under :func:`codex_home_lock`
    held by the caller. Both texts are rendered before either file is touched. config.toml
    goes first, so its parse refusal (``_require_toml``, judged on the exact text written)
    fires before anything changes. If hooks.json then does not hold the new text, config.toml
    is put back, so an I/O failure between the two cannot leave codex registering this
    install's store while firing another install's hooks.

    The undo is a RENAME, never a rewrite (the lockfile / conffile frame, git's lockfile.c and
    dpkg's .dpkg-old): before the merge, the file config.toml resolves to (a symlink is written
    through, so the link stays) is kept beside itself under a second name
    (:func:`_keep_original`); failing to keep it writes nothing. Putting it back renames that
    original over the live name, which needs no free space. It is put back only while the live
    name is still the very file the merge wrote (same link state, same inode, same bytes): an
    edit made in between, or a file levain cannot confirm as its own, is kept, and the original
    is named. Messages are held until the end,
    so a failing output channel cannot interrupt the undo. What this cannot cover: codex
    reading in the moment between the two writes, a crash (SIGKILL, power loss) in it, and an
    edit saved between the identity check and the rename that puts the original back (codex
    L3 r8; no portable no-clobber exchange closes it)."""
    config = codex_home / "config.toml"
    hooks = codex_home / "hooks.json"
    written = config.resolve()
    was_link = config.is_symlink()
    notes: list[str] = []
    try:
        st0 = os.stat(written)
        before: tuple[int, int] | None = (st0.st_dev, st0.st_ino)  # the original live file
    except OSError:
        before = None
    kept = _keep_original(written)
    ours: tuple[int, int, bytes] | None = None  # the file the merge wrote: (dev, inode, bytes)
    leave_kept = False
    try:
        try:
            _merge_codex_config(config, fragment, emit=notes.append)
            ours = _file_identity(written)
            _write_codex_hooks(hooks, hooks_text, notes.append)
        except BaseException:
            try:
                landed = hooks.read_text(encoding="utf-8") == hooks_text
            except BaseException:
                landed = False
            if not landed:
                leave_kept = kept is not None  # owned by the undo until it proves otherwise
                try:
                    leave_kept = _put_codex_config_back(config, written, was_link, before, ours,
                                                        kept, hooks, notes)
                except Exception as e:  # the original stays on disk, named; hooks' error raises
                    where = f"; the original is kept at {kept}" if kept is not None else ""
                    notes.append(f"  ! {hooks} could not be written, and putting {config} back "
                                 f"failed ({e}){where}. It may register this install's store.")
            raise
    finally:
        if kept is not None and not leave_kept:
            with contextlib.suppress(OSError):
                kept.unlink()
        for n in notes:
            try:
                emit(n)
            except Exception:  # a broken output channel must not mask the real error
                print(n, file=sys.stderr)


def _keep_original(written: Path) -> Path | None:
    """Keep ``written`` (when it exists) under a second name beside it, for
    :func:`_write_codex_pair`'s undo: a hard link, which keeps its mode, owner and inode, or
    where the filesystem has none, a copy of its bytes and mode created exclusively (never
    through a planted link). None when there is nothing to keep. Raises :class:`InitError`
    before anything is written when it cannot be kept."""
    if not written.exists():
        return None
    kept = written.with_name(f".{written.name}.levain-prior.{time.time_ns()}")
    try:
        try:
            os.link(written, kept)
        except FileExistsError:
            raise
        except OSError:
            fd = os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(written.read_bytes())
                    fh.flush()
                    os.fsync(fh.fileno())
                shutil.copymode(written, kept)
            except BaseException:
                with contextlib.suppress(OSError):
                    kept.unlink()
                raise
    except OSError as e:
        raise InitError(f"could not keep a copy of {written} to put back if codex's hooks.json "
                        f"then fails ({e}). Nothing was written.") from None
    return kept


def _file_identity(path: Path) -> tuple[int, int, bytes] | None:
    try:
        st = os.stat(path)
        return st.st_dev, st.st_ino, path.read_bytes()
    except OSError:
        return None


def _put_codex_config_back(config: Path, written: Path, was_link: bool,
                           before: tuple[int, int] | None, ours: tuple[int, int, bytes] | None,
                           kept: Path | None, hooks: Path, notes: list[str]) -> bool:
    """:func:`_write_codex_pair`'s undo. ``before`` is the original live file's (dev, inode),
    ``ours`` the file the merge wrote (None when it could not be read). True when the kept
    original must stay on disk (it is named in ``notes``)."""
    try:  # the name still leads where it led: a link swapped in since (one to `kept`, say) does not
        same_name = config.is_symlink() == was_link and config.resolve() == written
    except (OSError, RuntimeError):
        same_name = False
    gone = False
    try:
        st = os.stat(written)
        if same_name and before is not None and (st.st_dev, st.st_ino) == before:
            return False  # the original is still the live file: nothing to undo
    except FileNotFoundError:
        if kept is None:
            return False  # nothing was created
        # removed: the name is absent, or it is still the same link and its target is gone; a
        # dangling link swapped in is not "removed"
        gone = same_name or not os.path.lexists(config)
    except OSError:
        pass
    try:
        still_ours = (ours is not None and same_name and written.is_file()
                      and _file_identity(written) == ours)
    except (OSError, RuntimeError):
        still_ours = False
    if not still_ours:
        where = f"; the original is kept at {kept}" if kept is not None else ""
        state = ("was removed by something else while levain ran, so it was not put back"
                 if gone else
                 "could not be confirmed as the file levain wrote, so it was left as it is")
        risk = "" if gone else " It may register this install's store:"
        notes.append(f"  ! {hooks} could not be written, and {config} {state}{where}.{risk} "
                     f"Re-run `levain init --adapter codex` from the install codex should use.")
        return kept is not None
    try:
        if kept is None:
            written.unlink()
        else:
            os.replace(kept, written)
    except OSError as e:
        where = f" The original is kept at {kept}." if kept is not None else ""
        notes.append(f"  ! {hooks} could not be written, AND {config} could not be put back "
                     f"({e}): it now registers this install's store.{where} Re-run `levain init "
                     f"--adapter codex` from the install codex should use.")
        return kept is not None
    notes.append(f"  ! {hooks} could not be written, so the change to {config} above was undone: "
                 f"it is back as it was (any backup listed above is a copy of it).")
    return False


def _codex_hooks_json(adapter_root: Path, python_path: str, install: Path) -> str:
    hooks_text = (adapter_root / "hooks.json.template").read_text(encoding="utf-8")
    return _fill(hooks_text, {"PYTHON": _hook_arg_body(python_path),
                              "INSTALL_DIR": _hook_arg_body(str(install))})


def _install_openhands(install: Path, *, emit: Callable[[str], None] = print) -> None:
    """Install the hookless OpenHands adapter: stamp the adapter marker, nothing else.

    Unlike claude-code/codex, an OpenHands entity has no trusted hook surface — its
    activation is the `LevainCondenser` wired at runtime by `levain run`, not files laid
    down here. The seed (`seed/*.md`) and the store (`.levain/`) are written by `apply_init`
    around this call; all this does is record `adapter = "openhands"` in `.levain/config.json`
    so `doctor` / `verify-hooks` can identify a hookless install (there is no CLAUDE.md /
    AGENTS.md tag-file to detect it by). Idempotent + config-preserving (merge, never
    clobber a brand/entity_name already present)."""
    _write_adapter_marker(install, "openhands", emit)
    emit("  OpenHands entity scaffolded (no hooks — drive it with `levain run`).")


def _write_adapter_marker(
    install: Path, adapter: str, emit: Callable[[str], None] = print
) -> None:
    """Record `adapter` in `.levain/config.json` (the doctor/verify detection channel for
    a hookless install). MERGE, never clobber: preserve any `entity_name` / brand keys
    already written; refuse to overwrite an unreadable-but-present config (same honesty
    floor as `_write_brand_config` — a parse failure must not blank a recoverable config).
    Atomic write. Best-effort: a failure warns and returns (the marker is detection chrome,
    not install-critical — a store-backed install is still usable)."""
    from levain.dashboard import LEVAIN_CONFIG_REL

    config_path = install.joinpath(*LEVAIN_CONFIG_REL)
    existing: dict = {}
    if config_path.is_file():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            emit("  note: .levain/config.json is unreadable — adapter marker NOT written "
                 "(refusing to overwrite possibly-recoverable config; fix it, then re-run).")
            return
        if not isinstance(existing, dict):
            existing = {}
    if existing.get("adapter") == adapter:
        return  # idempotent — no spurious mtime churn
    merged = {**existing, "adapter": adapter}
    try:
        _atomic_write_text(
            config_path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        emit(f"  note: could not write the adapter marker to .levain/config.json ({e}).")


def _timestamped_backup_path(target: Path) -> Path:
    """Return `target.bak.<ns>` — nanosecond granularity to avoid same-second collisions."""
    return target.with_suffix(target.suffix + f".bak.{time.time_ns()}")


def _activation_excluded(rel: Path) -> bool:
    """Whether a relative activation path matches the copytree
    `ignore_patterns("__pycache__", "*.pyc")` semantics — a basename fnmatch at ANY
    path component (exactly what `shutil.ignore_patterns` does, including its
    `os.path.normcase` case handling). So a `__pycache__` dir, a `*.pyc` file, AND a
    directory whose name matches `*.pyc` are all excluded, just like the legacy
    copytree — the per-component check is strictly closer to copytree than the old
    `path.suffix == ".pyc"` (file-suffix-only) form."""
    return any(
        part == "__pycache__" or fnmatch.fnmatch(part, "*.pyc")
        for part in rel.parts
    )




def hook_body(text: str) -> str:
    """A hook script's comparable body: the ONE install-time substitution normalised
    away, so a healthy install is not reported as drifted.

    ⛔ SINGULAR, DELIBERATELY. This used to say "placeholder substitutionS", which promised more
    than the regex below delivers — it normalises exactly `_INSTALL_ANNEAL_BIN`, the line
    `{{ANNEAL_MEMORY}}` becomes. `_substitute_hook_placeholders` is called with a dict
    (`{"{{ANNEAL_MEMORY}}": anneal_path}`) and its own docstring says "and potentially more keys
    later" — so the day a second key ships, this function silently UNDER-normalises and every
    install reads stale. The plural was a description of an intention, and it would have read as
    coverage.

    ⚠ AND THE BLAST RADIUS DOUBLED 2026-09-03: the pack branch of `doctor._check_hook_freshness`
    normalises through here too, so an under-normalisation would false-red pack hooks as well as
    base ones. `test_hook_body_normalises_every_placeholder_install_substitutes` pins the coupling
    so adding a key without teaching this function fails loudly instead of shipping.

    ⚠ IT LIVES HERE, NOT IN `doctor`, AS OF 2026-09-06 — beside the substitution it inverts.
    The docstring above worried about staying in lockstep with `_substitute_hook_placeholders`
    while sitting in a different module; that is the coupling, and adjacency is the cheapest
    guard for it. `_copy_activation_tree`'s operator-edit backup used it too until
    `spore-900` (2026-09-13), which replaced the normalisation there with the install receipt.
    """
    return re.sub(
        r"^_INSTALL_ANNEAL_BIN = .*$", "_INSTALL_ANNEAL_BIN = <>", text, flags=re.M
    ).strip()


# ---------------------------------------------------------------------------
# The activation install receipt — `spore-900`, ruled by Phill 2026-09-13.
#
# What install WROTE into `activation/`, per file: the sha256 of the installed bytes
# (after placeholder substitution) and of the composed layer source they came from.
#
# ⛔ WHAT IT IS FOR, AND WHAT IT IS NOT. It answers "has this file changed since install
# wrote it?" — the question `init --force`'s backup notice needs, and one no normalisation
# of line shapes can answer (an edit confined to a substituted line is byte-identical to
# install's own output). It CANNOT answer "has the PACKAGE moved since install?": a receipt
# agrees with the install it describes by construction. That second question stays with
# `doctor._check_hook_freshness`, which compares against the live package tree. Diogenes'
# 2026-08-12 HIGH against `spore-492` is the reason both maps are recorded and neither
# check replaces the other.
#
# ⛔ An ABSENT, CORRUPT or EMPTY receipt is UNKNOWN, never "nothing was edited". Every
# install made before this receipt existed reads as absent.
ACTIVATION_RECEIPT_REL = (".levain", "activation-manifest.json")
ACTIVATION_RECEIPT_SCHEMA = 1
_RECEIPT_MAX_BYTES = 16 << 20
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _sha256_stream(path: Path) -> str:
    """sha256 of a file read in bounded chunks (an operator file can be any size)."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def activation_receipt_path(install: Path) -> Path:
    return install.joinpath(*ACTIVATION_RECEIPT_REL)


def read_activation_receipt(
    install: Path,
) -> tuple[dict[str, dict[str, str]] | None, str]:
    """The install's live receipt; see :func:`read_activation_receipt_file`."""
    return read_activation_receipt_file(activation_receipt_path(install))


def _read_receipt_text(path: Path) -> str:
    """A receipt's text from ONE open, reading at most `_RECEIPT_MAX_BYTES` (codex MED: a
    `stat` followed by a separate `read_text` let a file grow between the two). Raises
    OSError, or ValueError (UnicodeDecodeError included) for oversize or undecodable bytes."""
    with path.open("rb") as fh:
        raw = fh.read(_RECEIPT_MAX_BYTES + 1)
    if len(raw) > _RECEIPT_MAX_BYTES:
        raise ValueError(f"{path} is larger than a receipt can be")
    return raw.decode("utf-8")


def read_activation_receipt_file(
    path: Path,
) -> tuple[dict[str, dict[str, str]] | None, str]:
    """Read a receipt as ``({relpath: {"installed": sha, "source": sha}}, status)``.

    Takes a file path, not an install, so a receipt copied beside a retained backup tree
    (``spore-861`` ruling B, 2026-09-13) is read by the same validation as the live one.

    ``status`` is ``"ok"``, ``"absent"``, ``"corrupt"`` or ``"empty"``; the map is None
    unless ``"ok"``. A receipt that parses but carries a malformed entry is CORRUPT as a
    whole — a partial trust decision about a file that was half-written is the guess this
    record exists to remove.
    """
    try:
        raw = _read_receipt_text(path)
    except FileNotFoundError:
        return None, "absent"
    except (OSError, UnicodeError, ValueError):
        return None, "corrupt"
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError, MemoryError):
        return None, "corrupt"
    if not isinstance(data, dict) or data.get("schema") != ACTIVATION_RECEIPT_SCHEMA:
        return None, "corrupt"
    files = data.get("files")
    if not isinstance(files, dict):
        return None, "corrupt"
    out: dict[str, dict[str, str]] = {}
    for rel, entry in files.items():
        if not (
            isinstance(rel, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("installed"), str)
            and isinstance(entry.get("source"), str)
            and _SHA256_HEX.fullmatch(entry["installed"])
            and _SHA256_HEX.fullmatch(entry["source"])
        ):
            return None, "corrupt"
        out[rel] = {"installed": entry["installed"], "source": entry["source"]}
    if not out:
        return None, "empty"
    return out, "ok"


def _write_activation_receipt(install: Path, files: dict[str, dict[str, str]]) -> None:
    """Atomically write the receipt (unique temp + fsync + ``os.replace``). Raises OSError."""
    from levain import __version__

    path = activation_receipt_path(install)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"schema": ACTIVATION_RECEIPT_SCHEMA, "written_by": f"levain {__version__}",
         "files": dict(sorted(files.items()))},
        indent=2,
    ) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".activation-manifest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same_contents(a: Path, b: Path, *, chunk: int = 1 << 16) -> bool:
    """Whether two files hold identical bytes, compared in BOUNDED chunks.

    ⛔ Never loads a whole file. An operator can put an artifact of any size in the
    activation tree, and deciding whether to back it up must not OOM the install:
    `read_bytes()` on both sides allocates BOTH files at once. Size is checked
    first, which settles the common case without reading anything.
    """
    if a.stat().st_size != b.stat().st_size:
        return False
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            block = fa.read(chunk)
            if block != fb.read(chunk):
                return False
            if not block:
                return True


def _compose_activation_layers(layer_roots: Sequence[Path]) -> dict[str, Path]:
    """Compose an ordered STACK of activation-tree roots into one
    ``{relative_posix_path: winning_source_path}`` map.

    ``layer_roots`` is in winning order (see :func:`packs.order_activation_roots`):
    a LATER layer overrides an earlier one per RELATIVE path, so a pack's
    ``activation/posture.md`` replaces base's and a pack's ``hooks/x.py`` replaces
    base's ``hooks/x.py``. ``__pycache__`` directories and ``*.pyc`` files are
    excluded (:func:`_activation_excluded`) — mirroring the ``ignore_patterns`` the
    pre-layering ``copytree`` used, so a single-root (base-only) stack composes
    byte-for-byte (file CONTENT) to the legacy copy. A non-existent root is skipped
    (a pack's activation tree is optional; base always exists)."""
    composed: dict[str, Path] = {}
    for root in layer_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _activation_excluded(rel):
                continue
            composed[rel.as_posix()] = path
    return composed


_ACTIVATION_TREES_KEPT = 3  # spore-861: the newest three trees that PROVE pristine are kept
_TREE_BACKUP_PREFIX = "tree-"


def _tree_receipt_path(tree: Path) -> Path:
    """Where a retained tree's receipt lives: BESIDE it, never inside (spore-861 ruling B).

    Inside the tree it would be one more file every reader of `activation/` has to exclude,
    including the pristine proof's own "no extra files" rule."""
    return tree.parent / f"{tree.name}.receipt.json"


def _is_bytecode_residue(rel: Path, tree: Path) -> bool:
    """True only for bytecode Python itself writes: a `.pyc` directly inside `__pycache__`,
    named for a `.py` beside that `__pycache__` (`hooks/__pycache__/x.cpython-312.pyc` for
    `hooks/x.py`). ⛔ Anything else under an excluded-looking name — `hooks/__pycache__/notes.md`,
    a directory called `research.pyc/` — is operator content: the proof skipping it while
    `rmtree` deletes it was an L2 HIGH, reproduced 2026-09-13."""
    parts = rel.parts
    if len(parts) < 2 or parts[-2] != "__pycache__":
        return False
    # Exactly the name `importlib` gives a cache file — `<stem>.<impl>-<ver>[.opt-N].pyc`. A
    # looser `<stem>.*.pyc` let `h.notes.pyc` beside `h.py` pass as residue, so a tree holding
    # it proved pristine and rotation deleted the only copy (codex HIGH, reproduced
    # 2026-09-14). ⚠ A file an operator writes UNDER that exact name is still skipped; a
    # filename cannot prove provenance, and closing that means keeping every tree whose
    # hooks ever ran.
    match = _BYTECODE_NAME.fullmatch(parts[-1])
    if match is None:
        return False
    return tree.joinpath(*parts[:-2], f"{match['stem']}.py").is_file()


# The tag must be a CPython cache tag or this interpreter's own: `[a-z]+-\d+` accepted
# `h.notes-1.pyc` (codex HIGH, round 2, reproduced). The stem may hold dots, as a dotted
# source name's cache file does.
_CACHE_TAGS = r"cpython-3\d+" + (
    f"|{re.escape(sys.implementation.cache_tag)}" if sys.implementation.cache_tag else ""
)
_BYTECODE_NAME = re.compile(rf"(?P<stem>.+)\.(?:{_CACHE_TAGS})(?:\.opt-[12])?\.pyc")


def _edits_against_receipt(
    tree: Path, receipt: Mapping[str, Mapping[str, str]]
) -> tuple[list[str], list[str]]:
    """Compare a whole activation tree with the receipt of the install that wrote it.

    Returns ``(edited, unexamined)`` as relative posix paths. ``edited`` holds every file
    whose bytes differ from its receipt entry, every file the receipt does not list (install
    never wrote it), and every symlink or non-regular entry (install writes neither).
    ``unexamined`` holds anything that could not be read or listed. Only bytecode Python
    wrote itself is skipped (`_is_bytecode_residue`): hooks import `_levain_hook` from their
    own directory, so a tree whose hooks ever ran contains it. Every filesystem call is
    inside a guard, because this runs after the swap has completed."""
    edited: list[str] = []
    unexamined: list[str] = []

    def _walk_error(err: OSError) -> None:
        unexamined.append(str(err.filename))

    for walk_root, walk_dirs, walk_files in os.walk(tree, onerror=_walk_error):
        walk_dirs.sort()
        root = Path(walk_root)
        for dname in list(walk_dirs):
            drel = (root / dname).relative_to(tree).as_posix()
            try:
                if (root / dname).is_symlink():
                    edited.append(drel)
            except OSError:
                # A listable but unsearchable parent (L1 HIGH, reproduced): lstat raises.
                unexamined.append(drel)
        for fname in sorted(walk_files):
            path = root / fname
            rel = path.relative_to(tree)
            name = rel.as_posix()
            try:
                if not path.is_symlink() and _is_bytecode_residue(rel, tree):
                    continue
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                    edited.append(name)
                    continue
                entry = receipt.get(name)
                if entry is None or _sha256_stream(path) != entry["installed"]:
                    edited.append(name)
            except OSError:
                unexamined.append(name)
    return edited, unexamined


def _tree_proves_pristine(tree: Path) -> bool:
    """True only if the tree matches, file for file, the receipt copied beside it.

    ⛔ An absent, corrupt or empty sibling receipt is UNPROVEN, never pristine (spore-861
    ruling B; spore-492's "an empty receipt is never OK")."""
    files, status = read_activation_receipt_file(_tree_receipt_path(tree))
    if status != "ok" or files is None:
        return False
    edited, unexamined = _edits_against_receipt(tree, files)
    return not edited and not unexamined


def _prune_activation_backups(backups_root: Path, keep: int) -> list[str]:
    """Remove whole-tree backups that PROVE pristine, beyond the newest ``keep`` of them.

    ⚖ spore-861, ruling B (Phill 2026-09-13). A tree may be deleted only if it matches the
    receipt of the install that wrote it, with no extra files. Every other tree, including
    every one without a readable receipt, is kept and named. Only ``tree-<digits>``
    directories are candidates; the bare-timestamp directories older releases wrote can
    hold the only copy of an edit and are never touched. Deleting a tree also deletes its
    receipt. Returns operator lines; never raises."""
    notes: list[str] = []
    try:
        entries = list(backups_root.iterdir())
    except OSError as e:
        return [f"  note: could not list {backups_root} to remove old backups ({e})."]
    trees: list[tuple[int, Path]] = []
    for p in entries:
        suffix = p.name[len(_TREE_BACKUP_PREFIX):]
        if (p.name.startswith(_TREE_BACKUP_PREFIX) and suffix.isdigit()
                and p.is_dir() and not p.is_symlink()):
            trees.append((int(suffix), p))
    trees.sort()
    pristine = [t for _, t in trees if _tree_proves_pristine(t)]
    unproven = [t for _, t in trees if t not in pristine]
    for old in pristine[:-keep] if keep > 0 else pristine:
        if not _tree_proves_pristine(old):  # re-proved at the moment of deletion (L2 LOW)
            unproven.append(old)
            continue
        try:
            shutil.rmtree(old)
        except OSError as e:
            notes.append(f"  note: could not remove the old backup {old} ({e}).")
            continue
        try:
            _tree_receipt_path(old).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            notes.append(f"  note: removed {old} but not its receipt ({e}).")
    if unproven:
        notes.append(
            "  Kept, because they may hold edits (levain deletes a backup only when it "
            "proves unedited): " + ", ".join(str(t) for t in unproven)
        )
    return notes


def _copy_activation_tree(
    layer_roots: Sequence[Path],
    dst: Path,
    *,
    base_activation: Path,
    anneal_path: str | None = None,
    emit: Callable[[str], None] = print,
) -> None:
    """Compose the ordered activation-tree layer STACK `layer_roots` into `dst`, keeping
    the whole previous tree as a backup and recording an install receipt.

    `layer_roots` (base first, then composing packs by `pack.toml` order — see
    `packs.order_activation_roots`) is merged per relative path, LAST layer wins, so a
    pack's `activation/posture.md` overrides base's. A single base root is the base-only
    case, byte-identical to the pre-layering copytree.

    Honesty floor — `base_activation` (the adapter's OWN base tree) must itself contribute
    files, so a present-but-empty base (a corrupt wheel) cannot be masked by a pack. The new
    tree is assembled in a staging dir, so ANY build failure leaves `dst` untouched.

    `anneal_path`, when provided, is substituted into `{{ANNEAL_MEMORY}}` in every hook .py
    under `dst/hooks/` (recursively, so a pack's nested hook is reached).

    ⛔ THE PREVIOUS TREE IS THE BACKUP (spore-861). On a re-install the whole previous `dst`
    is RENAMED into `<install>/.levain/backups/activation/tree-<time_ns>/`, so nothing is
    classified before anything is destroyed: every file, symlink and directory the operator
    had is kept, including an edit saved a moment before the swap. If that location cannot
    take the rename, the tree goes to a sibling `.levain-activation-prev-<time_ns>` and the
    operator is told; if that fails too, nothing has moved and this raises.

    THE RECEIPT (spore-900). `.levain/activation-manifest.json` records, per file, the
    sha256 of the bytes installed and of the winning source. It is copied BESIDE the
    retained tree as `tree-<ns>.receipt.json` before the swap, so that tree can later be
    proved unedited (`_tree_proves_pristine`), and the live receipt is removed before the
    swap so it can never outlive the tree it describes. The new receipt is written after.

    The "Operator-edited ... preserved at" lines are computed AFTER the swap, from the
    retained tree against its own receipt; with no usable receipt they say levain cannot
    tell. Rotation (`_prune_activation_backups`) deletes only trees that prove unedited.
    """
    composed = _compose_activation_layers(layer_roots)

    if not _compose_activation_layers([base_activation]):
        raise InitError(
            f"the base activation tree at {base_activation} contributes no files "
            f"(missing or empty — a corrupt wheel). Refusing to install a base-less "
            f"activation/; reinstall with `pip install --force-reinstall levain`."
        )

    new_tree = dst.parent / f".levain-activation-new-{time.time_ns()}"
    try:
        # Directory structure first (including EMPTY dirs, as copytree did).
        for root in layer_roots:
            if not root.is_dir():
                continue
            for d in sorted(root.rglob("*")):
                if not d.is_dir():
                    continue
                rel = d.relative_to(root)
                if not _activation_excluded(rel):
                    (new_tree / rel).mkdir(parents=True, exist_ok=True)
        for rel_str, source in composed.items():
            target = new_tree / rel_str
            target.parent.mkdir(parents=True, exist_ok=True)
            # Cross-layer file/dir collision: fail loud instead of copying INTO a dir.
            if target.is_dir():
                raise InitError(
                    f"activation layer conflict: {rel_str!r} is a file in one layer "
                    f"and a directory in another. Composing packs must not collide on "
                    f"a path; fix the pack layout and re-run."
                )
            # No is_file() guard: a vanished source (TOCTOU) must surface, not yield an
            # install missing an activation file.
            shutil.copy2(source, target)
        if anneal_path is not None:
            # The slot sits in a Python string literal, so the value is escaped for one, the same
            # way `_expected_activation` renders it (spore-866).
            _substitute_hook_placeholders(new_tree / "hooks", {"{{ANNEAL_MEMORY}}": _py_literal_body(anneal_path)})

        # This run's receipt, from the staged bytes and the winning source of each.
        receipt: dict[str, dict[str, str]] = {
            rel_str: {
                "installed": _sha256_stream(new_tree / rel_str),
                "source": _sha256_stream(source),
            }
            for rel_str, source in composed.items()
        }
        prior_installed, prior_status = read_activation_receipt(dst.parent)
    except BaseException:
        shutil.rmtree(new_tree, ignore_errors=True)
        raise

    live_receipt = activation_receipt_path(dst.parent)
    # Held in memory so a swap that does not go through can restore the live receipt
    # exactly as it was; see the rollback below.
    prior_receipt_text: str | None = None
    if prior_status == "ok":
        try:
            prior_receipt_text = _read_receipt_text(live_receipt)
        except (OSError, ValueError):
            pass
    backups_root = dst.parent / ".levain" / "backups" / "activation"
    had_previous = dst.exists() or dst.is_symlink()
    stamp = time.time_ns()
    tree_backup = backups_root / f"{_TREE_BACKUP_PREFIX}{stamp}"
    carried_receipt: Path | None = None

    def _drop_carried() -> None:
        if carried_receipt is not None:
            try:
                carried_receipt.unlink()
            except OSError:
                pass

    # The previous tree's receipt travels with it, copied BEFORE the invalidation below
    # removes the live one. A failed copy leaves that tree unproven, so it is kept. The
    # path is recorded before copying, so a partial copy is removed rather than orphaned
    # (L1 LOW, reproduced). A symlinked activation/ carries none; see the swap.
    if had_previous and prior_status == "ok" and not dst.is_symlink():
        carried_receipt = _tree_receipt_path(tree_backup)
        try:
            backups_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live_receipt, carried_receipt)
        except OSError:
            _drop_carried()
            carried_receipt = None

    # ⛔ INVALIDATE THE LIVE RECEIPT BEFORE THE SWAP (L1 + L2 HIGH, reproduced
    # 2026-09-13): removed only afterwards, it outlived any interrupt, kill or unwritable
    # `.levain/` in between, describing a tree no longer installed. Absent reads as
    # UNKNOWN. Skipped when it already records exactly the bytes about to be installed, so
    # a no-change reinstall is never blocked by an unwritable `.levain/`.
    if prior_installed is None or {
        rel: entry["installed"] for rel, entry in prior_installed.items()
    } != {rel: entry["installed"] for rel, entry in receipt.items()}:
        try:
            live_receipt.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            shutil.rmtree(new_tree, ignore_errors=True)
            _drop_carried()
            raise InitError(
                f"could not remove the previous activation install receipt at "
                f"{live_receipt} ({e}). It describes the tree this re-install replaces, and "
                f"left in place it would report install's own files as your edits next "
                f"time. Nothing was changed; fix the permissions on "
                f"{dst.parent / '.levain'} and re-run."
            ) from e

    moved_to: Path | None = None
    # Where the rename in flight would put the previous tree. Set BEFORE each `os.replace`, so
    # the rollback decides from DISK whether that rename landed, never from which statement ran
    # last. Assigning `moved_to` before the rename lost the receipt when the rename failed
    # (complement + glm HIGH, round 2); assigning it after lost `activation/` when an interrupt
    # landed between the two (codex HIGH, round 3). Both reproduced 2026-09-14.
    candidate: Path | None = None
    link_target: str | None = None
    notes: list[str] = []
    try:
        if had_previous and dst.is_symlink():
            # A symlinked activation/ moves as the LINK. Renamed into backups, a relative
            # link dangles and the notice points at nothing (L1 MED, L2 LOW, reproduced).
            # Kept beside activation/ it still resolves; the content stays at its target.
            link_target = os.readlink(dst)
            candidate = dst.parent / f".levain-activation-prev-{stamp}"
            os.replace(dst, candidate)
            moved_to = candidate
        elif had_previous:
            try:
                backups_root.mkdir(parents=True, exist_ok=True)
                candidate = tree_backup
                os.replace(dst, tree_backup)
                moved_to = tree_backup
            except OSError as e:
                if not (dst.exists() or dst.is_symlink()):
                    raise  # the rollback's reconcile finds the tree where the rename left it
                candidate = dst.parent / f".levain-activation-prev-{stamp}"
                try:
                    os.replace(dst, candidate)
                    moved_to = candidate
                except OSError as e2:
                    raise InitError(
                        f"could not keep the previous activation/ under {backups_root} "
                        f"({e}) or beside it ({e2}). Nothing was moved or deleted; fix the "
                        f"permissions and re-run."
                    ) from e2
                _drop_carried()
                notes.append(
                    f"  note: could not keep the previous activation/ under {backups_root} "
                    f"({e}). It is kept at {moved_to} instead, which levain never removes."
                )
        os.replace(new_tree, dst)
    except BaseException as exc:
        shutil.rmtree(new_tree, ignore_errors=True)
        # Decided from DISK: a rename that landed before the interrupt did leaves the tree at
        # `candidate` and nothing at `dst`, whatever `moved_to` says (codex HIGH, round 3).
        if (moved_to is None and candidate is not None
                and (candidate.exists() or candidate.is_symlink())
                and not (dst.exists() or dst.is_symlink())):
            moved_to = candidate
        original_at_dst = moved_to is None and had_previous
        if (moved_to is not None and not (dst.exists() or dst.is_symlink())
                and (moved_to.exists() or moved_to.is_symlink())):
            try:
                os.replace(moved_to, dst)  # put the original tree back
                original_at_dst = True
            except OSError as e3:
                # Raised in place of `exc` so the operator learns where the tree is (codex
                # MED). `_drop_carried` is skipped: a carried receipt that still exists sits
                # beside the tree under `backups_root`, which can still prove itself.
                raise InitError(
                    f"the new activation/ could not be installed ({exc}), and putting the "
                    f"previous one back failed too ({e3}). The previous activation/ is intact "
                    f"at {moved_to}; move it back to {dst} by hand. Nothing was deleted."
                ) from exc
        # ⛔ THE ORIGINAL TREE IS BACK, SO ITS RECEIPT GOES BACK (complement HIGH + glm MED ×2,
        # reproduced 2026-09-14). The carried copy is used when it still exists (codex MED,
        # round 2). The text read before the invalidation is the fallback, because the copy is
        # absent whenever `backups_root` refused it, is already unlinked once the sibling fallback
        # ran, and never exists for a symlinked activation/. Relying on the copy alone left an
        # intact tree with no receipt on each of those paths, and on the refusal path beside an
        # error saying nothing was deleted.
        if (original_at_dst and not live_receipt.exists()
                and (dst.exists() or dst.is_symlink())):
            try:
                if carried_receipt is not None and carried_receipt.exists():
                    os.replace(carried_receipt, live_receipt)
                    carried_receipt = None
                elif prior_receipt_text is not None:
                    _atomic_write_text(live_receipt, prior_receipt_text)
            except OSError:
                pass
        _drop_carried()
        raise

    # Everything below runs only after the new tree is in place: best-effort, never
    # raising into a completed install.
    try:
        _write_activation_receipt(dst.parent, receipt)
    except OSError as e:
        notes.append(
            f"  note: could not record the activation install receipt ({e}). The install "
            f"is complete; the next `levain init --force` cannot tell your edits from "
            f"install's own files, and that backup will be kept rather than rotated."
        )

    try:
        _report_and_rotate(moved_to, link_target, prior_status, prior_installed,
                           backups_root, emit, notes)
    except OSError as e:
        # The install is complete; nothing after the swap may turn it into a traceback that
        # repeats on every later run (L1 HIGH, reproduced).
        notes.append(f"  note: the install is complete, but reporting on or rotating old "
                     f"backups failed ({e}); no backup was rotated by that step.")
    for note in notes:
        emit(note)


def _report_and_rotate(
    moved_to: Path | None,
    link_target: str | None,
    prior_status: str,
    prior_installed: Mapping[str, Mapping[str, str]] | None,
    backups_root: Path,
    emit: Callable[[str], None],
    notes: list[str],
) -> None:
    """Post-swap notices for the retained tree, then receipt-gated rotation."""
    if moved_to is not None and link_target is not None:
        emit(f"  activation/ was a symlink to {link_target}; that content is untouched at "
             f"its target, and the link itself is kept at {moved_to}")
        return
    if moved_to is not None:
        if prior_status == "ok" and prior_installed is not None:
            edited, unexamined = _edits_against_receipt(moved_to, prior_installed)
            for name in edited:
                emit(f"  ! Operator-edited {name} preserved at {moved_to / name}")
            for name in unexamined:
                emit(f"  note: could not compare {name} with its install receipt; it is "
                     f"preserved in {moved_to}")
            emit(f"  Previous activation/ kept whole at {moved_to}")
        elif prior_status == "absent":
            emit(
                f"  Previous activation/ kept whole at {moved_to} (no install receipt "
                f"covers it, so levain cannot tell whether you edited anything in it)"
            )
        else:
            # ⚖ C6 AMENDED (Phill, 2026-09-15): a corrupt/empty receipt read the same as
            # "no receipt yet" here, contradicting doctor's own C6 text ("a damaged receipt
            # is not a pre-receipt install") a few lines away in doctor.py. Reproduced via
            # the real CLI 2026-09-16: absent, corrupt and empty all printed the byte-
            # identical "no install receipt covers it" line.
            emit(
                f"  Previous activation/ kept whole at {moved_to} (its install receipt "
                f"was unreadable ({prior_status}), so levain cannot tell whether you "
                f"edited anything in it — a damaged receipt is not a pre-receipt install)"
            )
        if moved_to.parent == backups_root:
            notes.extend(_prune_activation_backups(backups_root, _ACTIVATION_TREES_KEPT))


def _substitute_hook_placeholders(hooks_dir: Path, mapping: dict[str, str]) -> None:
    """Replace install-time placeholders in every .py file under `hooks_dir`,
    RECURSIVELY (so a pack's nested hook, e.g. `hooks/sub/x.py`, is reached — the
    composition supports nested subtrees, so the substitution must too; base hooks
    are flat and unaffected).

    Hooks ship with `{{ANNEAL_MEMORY}}` (and potentially more keys later) so
    they can use the install-time-resolved absolute path of anneal-memory
    without depending on PATH at fire time. The substitution is a simple
    string replace — placeholder is unique enough that false positives are
    not a real risk.
    """
    if not hooks_dir.is_dir():
        return
    for py_file in hooks_dir.rglob("*.py"):
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


def _codex_block_dict(block: str) -> dict | None:
    """The parsed TOML of a codex `[mcp_servers.anneal_memory]` block, or None if
    it does not parse.

    Used to detect ANY change to the block, not just a `--db` repoint (Diogenes
    HIGH, 2026-09-09, reproduced on disk). `_codex_block_store` reads one field
    out of the block; everything else an operator put there — `env`,
    `startup_timeout_ms`, extra keys — was outside the store-only guard's field
    of view and got silently dropped on re-init whenever `--db` happened to
    match. Comparing the full parsed dict answers the actual question: is
    anything being taken away.
    """
    try:
        return tomllib.loads(block)
    except (tomllib.TOMLDecodeError, ValueError):
        return None


_LEVAIN_OWNED_CODEX_KEYS = ("command", "args")


def _without_levain_keys(parsed: dict | None) -> dict | None:
    """`parsed` with the keys `init` itself writes removed from the anneal_memory server
    table, so only what an operator added is compared. None stays None."""
    if parsed is None:
        return None
    servers = dict(parsed.get("mcp_servers") or {})
    server = servers.get("anneal_memory")
    if isinstance(server, dict):
        servers["anneal_memory"] = {
            k: v for k, v in server.items() if k not in _LEVAIN_OWNED_CODEX_KEYS
        }
    return {**parsed, "mcp_servers": servers}


def _is_levain_launcher(parsed: dict | None, store: str | None) -> bool:
    """Whether the anneal_memory server table launches `store` exactly as a levain `init`
    wrote it: the pre-spore-751 `anneal-memory --db <store> serve`, or the current
    `<python> -P -m anneal_memory --db <store> serve`. Any other launcher was chosen by
    someone, and replacing it is replacing their customisation."""
    if parsed is None or store is None:
        return False
    server = (parsed.get("mcp_servers") or {}).get("anneal_memory") or {}
    command, args = server.get("command"), server.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    if args == ["--db", store, "serve"]:
        return Path(command).name in ("anneal-memory", "anneal-memory.exe")
    # The current shape's command is the interpreter `init` ran under, so it differs between
    # venvs and cannot be compared for equality without re-raising Diogenes' false alarm on a
    # re-init from another venv. It must at least BE a Python interpreter (complement + glm +
    # codex HIGH, round 3, reproduced 2026-09-14: a wrapper keeping levain's exact args read
    # as levain's own). ⚠ This picks the NOTICE only; `_merge_codex_config` backs the block
    # up whatever this returns, so a misjudged launcher costs a wrong sentence, not the block.
    return (
        args == ["-P", "-m", "anneal_memory", "--db", store, "serve"]
        and _PYTHON_NAME.fullmatch(Path(command).name) is not None
    )


_PYTHON_NAME = re.compile(r"python(?:3(?:\.\d+)?)?(?:\.exe)?")


def _codex_block_store(block: str) -> str | None:
    """The store path a codex `[mcp_servers.anneal_memory]` block points at, or None.

    ⛔ PARSES TOML; does not pattern-match quotes. `args = ['--db', '/x', 'serve']` is
    perfectly valid TOML — literal (single-quoted) strings — and a regex keyed to `"`
    returns None for it, which would skip the warning AND the backup and perform the
    exact silent repoint this guard exists to stop. A guard that does not fire on a
    valid input is worse than no guard, because the absence reads as approval.
    Parsing also means a commented-out `args` line cannot be mistaken for the live
    value. codex L3 MED.
    """
    try:
        data = tomllib.loads(block)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    server = (data.get("mcp_servers") or {}).get("anneal_memory") or {}
    args = server.get("args")
    if not isinstance(args, list):
        return None
    # ARGV SEMANTICS, NOT FIRST-MATCH. anneal-memory parses these with argparse, so a
    # repeated flag means the LAST one wins — returning the first would name a store
    # the server is not using and skip the warning for a real repoint. Both spellings
    # are accepted on a command line, so both are read here. codex L3 MED.
    store: str | None = None
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            continue
        if arg == "--db" and i + 1 < len(args) and isinstance(args[i + 1], str):
            store = args[i + 1]
        elif arg.startswith("--db="):
            store = arg[len("--db="):]
    return store


def _merge_codex_config(
    path: Path, fragment: str, *, emit: Callable[[str], None] = print
) -> None:
    """Insert/replace the `[mcp_servers.anneal_memory]` block in config.toml.

    Idempotent — re-running init replaces the block in place rather than
    appending a duplicate section header (which TOML-parse-fails on next
    Codex startup).

    ⛔ THIS REGISTRATION IS GLOBAL BY DESIGN (`templates/adapters/codex/README.md`),
    so replacing the block REPOINTS EVERY CODEX SESSION ON THE MACHINE at a
    different store. That used to happen SILENTLY. On 2026-09-04 an install run
    against a temp entity dir — a manual e2e that isolated the entity but not
    `CODEX_HOME` — left every codex invocation reading a 4KB throwaway fixture for
    hours; a reviewer that consulted memory got nothing and would read nothing as
    "not known". `absence_of_signal_rendered_as_health`, inside the shared apparatus.

    ⚡ SAME SHAPE AS THE ACTIVATION BACKUP EIGHT LINES UP, AND THAT IS THE POINT:
    `hooks.json` in this very directory has always been copied aside and announced
    before being replaced, while `config.toml` — larger, shared, and global — was
    overwritten without either. Our own documented procedure destroyed operator
    state without saying so, which is the defect `init --force` had for a patched
    hook. Fixed the same way: preserve it, and SAY SO.

    ⚖ IT WARNS, IT DOES NOT REFUSE, and that is deliberate rather than timid.
    Repointing codex at a different install is a legitimate operator action — it is
    the DOCUMENTED REPAIR for this very defect (`levain init --adapter codex` from
    whichever install you want codex reading). A refusal would block the fix for the
    problem it is guarding.
    """
    if not path.is_file():
        text = fragment.rstrip() + "\n"
        _require_toml(text, path, None)
        # Atomic like the replace path below: a torn write must not leave a partial
        # config.toml that the undo cannot confirm as ours (complement L3 r6).
        _atomic_write_text(path, text)
        return

    existing = path.read_text(encoding="utf-8")
    new_block_match = _CODEX_MCP_BLOCK_RE.search(fragment)
    if not new_block_match:
        return

    new_block = new_block_match.group(0).rstrip() + "\n"
    # Judged before any backup or write, on exactly the text that will be written (L3 r1).
    merged = _splice_codex_block(existing, new_block)
    prior_unparsed = _require_toml(merged, path, existing)

    repoint: tuple[str, str, Path] | None = None
    unknown_prior: Path | None = None
    customized: Path | None = None
    relaunched = False
    relaunched_bak: Path | None = None
    reformatted_bak: Path | None = None
    new_dict: dict | None = None
    old_block_match = _CODEX_MCP_BLOCK_RE.search(existing)
    if old_block_match:
        old_dict = _codex_block_dict(old_block_match.group(0))
        new_dict = _codex_block_dict(new_block)
        old_store = _codex_block_store(old_block_match.group(0))
        new_store = _codex_block_store(new_block)
        # ⛔ THE BACKUP IS KEYED ON REPLACING A BLOCK, NOT ON HAVING PARSED THE OLD STORE
        # (glm-5.3 L3 MED, 2026-09-07, reproduced on disk). This used to require BOTH stores
        # to parse. `_codex_block_store` returns None whenever the block is not the shape we
        # write — `args` absent, not a list, or carrying no `--db`, e.g. an operator who moved
        # their store into a wrapper `command`. The `sub` below replaced that block ANYWAY,
        # in the machine-global config, with NO backup and NO message: measured, an operator
        # block naming its own store via a wrapper was destroyed with EMPTY output.
        # ⚡ So the guard did not fire on exactly the HAND-EDITED input it exists for, and
        # this function's own docstring already states the rule it was breaking: "a guard that
        # does not fire on a valid input is worse than no guard, because the absence reads as
        # approval." Not-parsing is LESS reason to proceed silently, not more — an unreadable
        # prior state is the case where a copy matters most.
        # ⛔ AND STORE-EQUALITY WAS ITSELF TOO NARROW A QUESTION (Diogenes HIGH, 2026-09-09,
        # reproduced on disk). The old guard compared `--db` alone, so an operator's `env` or
        # `startup_timeout_ms` on the SAME store vanished with no backup and no message —
        # `_codex_block_store` reads one field; everything else in the block was outside the
        # guard's field of view. Comparing the full parsed dict (`_codex_block_dict`) asks the
        # actual question — is anything being taken away — and still stays silent on a true
        # re-run of levain's own identical block, since that block compares dict-equal to
        # itself.
        # ⚖ Still warn-not-refuse, unchanged: repointing is a legitimate operator action and
        # the documented repair for this very defect. Re-running against the store already
        # registered stays silent, because nothing is being taken away.
        # ⛔ KEYED ON `old_store`, NOT `old_dict` (L3 codex+complement+glm, all three, HIGH,
        # 2026-09-13, reproduced against `test_an_UNPARSEABLE_codex_block_is_backed_up_...`).
        # A first version of this keyed `replacing_unknown` on `old_dict is None` — but a
        # wrapper-`command` block (no `args`/`--db`) is perfectly valid TOML, so `old_dict`
        # parses while `old_store` stays None. That routed the genuinely-unknown-store case
        # into `content_changed` instead, and the operator was told "it still points at the
        # same store" — a FALSE claim about a machine-global config, worse than the lost-note
        # this whole guard exists to prevent. `replacing_unknown` must stay store-keyed;
        # `content_changed` only fires once a store IS known and unchanged.
        replacing_unknown = old_store is None
        store_changed = (
            not replacing_unknown
            and new_store is not None
            and old_store != new_store
        )
        # ⛔ LEVAIN'S OWN KEYS ARE NOT A CUSTOMISATION (Diogenes MEDIUM, 2026-09-14, his
        # signature reproduced: 7 lines and a backup). `command` and `args` are what `init`
        # writes, so they change on every pre-spore-751 upgrade and on a re-init from another
        # interpreter; compared whole, the dicts always differed and the operator was told a
        # customisation was gone when nothing of theirs was touched.
        # ⛔ AND ONLY WHILE THE OLD LAUNCHER IS ONE LEVAIN ITSELF WROTE (codex HIGH, round 2,
        # reproduced 2026-09-14): a customisation can live INSIDE `command`/`args` — a wrapper
        # command plus `--trace` on the same store was replaced with no backup and no warning.
        relaunched = (
            not replacing_unknown and not store_changed and old_dict != new_dict
            and _without_levain_keys(old_dict) == _without_levain_keys(new_dict)
            and _is_levain_launcher(old_dict, old_store)
        )
        content_changed = (
            not replacing_unknown and not store_changed and old_dict != new_dict
            and not relaunched
        )
        # Settings identical, TEXT not: comments or formatting an operator put inside the block,
        # which the replacement below deletes (codex MED, round 4, reproduced 2026-09-14: a
        # `# corporate runbook` line vanished with no backup and no notice).
        raw_changed = old_block_match.group(0).rstrip() + "\n" != new_block
        # ⛔ PRESERVE ALWAYS; THE HEURISTIC CHOOSES ONLY THE WORDING (spore-865, applied 2026-09-14
        # after three review rounds each found a launcher the classifier misjudged, and a fourth
        # found a comment-only change no named case covered). The gate is the two FACTS, not the
        # named cases: back up whenever the parsed block or its raw text differs, so a gap in the
        # classification below can only produce an extra backup or a wrong notice, never a lost
        # block. A byte-identical re-run stays silent.
        if raw_changed or old_dict != new_dict:
            bak = _timestamped_backup_path(path)
            try:
                shutil.copy2(path, bak)
            except OSError as e:
                whither = (
                    "whose current store could not be read"
                    if replacing_unknown
                    else f"from {old_store} to {new_store}"
                    if store_changed
                    else "whose settings would be overwritten"
                    if content_changed
                    else "whose launcher would be updated"
                    if relaunched
                    else "whose comments or formatting would be replaced"
                )
                raise InitError(
                    f"could not back up {path} ({e}) before replacing Codex's global "
                    f"anneal memory registration {whither}. Refusing to change "
                    f"the machine-wide registration without a copy; fix the permissions "
                    f"and re-run."
                ) from e
            if replacing_unknown:
                unknown_prior = bak
            elif store_changed:
                assert old_store is not None and new_store is not None  # store_changed implies it
                repoint = (old_store, new_store, bak)
            elif content_changed:
                customized = bak
            elif relaunched:
                relaunched_bak = bak
            else:
                reformatted_bak = bak
    existing = merged

    # ATOMIC, and announced only AFTER it lands. A partial `write_text` can truncate
    # the operator's whole global codex config, and announcing "now points at X"
    # before the write means a failure leaves a notice describing a repoint that
    # never happened — a true-sounding statement about a world that does not exist.
    # codex L3 MED + glm L3 MED, convergent.
    # ⛔ `copymode` IS LOAD-BEARING, NOT TIDINESS (Diogenes MEDIUM, 2026-09-07, reproduced
    # here in both directions). `os.replace` swaps the INODE, so without it the file's mode
    # is whatever the fresh `tmp` inherited from the umask and the operator's is discarded.
    # The `write_text` this block replaced truncated the EXISTING inode and so preserved the
    # mode for free — the atomicity fix silently traded it away. MEASURED, real repoint of a
    # 0o600 config: 0o600 -> 0o644 without this line, 0o600 -> 0o600 with it.
    # ⚖ `copymode` AND NOT `copystat`, MEASURED RATHER THAN ASSUMED. The first fix here used
    # `copystat`, which restores the mode AND the timestamps: the repointed file then claims
    # it was never modified. Probed side by side on this box — `copystat` leaves `st_mtime`
    # UNCHANGED across a real content change, `copymode` lets it advance. The target is the
    # pre-0.4.5 `write_text` semantics, which preserved the mode and moved the mtime, so
    # restoring the mode ALONE is the whole correction; carrying timestamps and macOS
    # `st_flags` across is a second deviation dressed as thoroughness. **Restore exactly the
    # property that was lost, not every property the API offers.**
    # ⚡ AND THE REPOINT BACKUP EARLIER IN THIS FUNCTION ALREADY GOT IT RIGHT: it uses
    # `shutil.copy2`, which carries the mode, so on the same run `config.toml.bak.<ns>`
    # stayed 0o600 while the live `config.toml` beside it went 0o644. The copy made to
    # protect operator state was better protected than the file itself.
    # ⛔ CITED BY SYMBOL, NOT BY DISTANCE, AND THIS IS THE THIRD TIME THE SAME FIGURE WENT
    # WRONG IN THE SAME PARAGRAPH. The finding said "the backup ELEVEN LINES UP"; at the
    # commit it was filed against, `shutil.copy2` was at :2167 and `os.replace` at :2194 —
    # 27, so it was NEVER true, not even of the file it described. The first draft of this
    # comment copied "eleven" forward AND excused it as once-correct; both were wrong.
    # ⚡ THEN THE REPLACEMENT FIGURE ROTTED THREE TIMES WHILE THIS PARAGRAPH WAS BEING
    # WRITTEN — each measurement correct when taken and stale by the next keystroke,
    # because the thing being measured is the distance to the sentence doing the measuring.
    # No current figure is stated here for that reason. The only stable distance is one
    # anchored to a COMMIT (27, at 0d09703), because that file cannot change.
    # ⚖ A DISTANCE IS A COORDINATE WEARING A DIFFERENT WORD — and it is WORSE than a line
    # number, because it rots on the writer's OWN keystrokes rather than on someone else's
    # later edit. `tools.py` already ruled "cite the symbol, not the line"; that rule was
    # obeyed to the letter here and broken anyway, because "eleven lines up" did not read
    # as a coordinate. `shutil.copy2` is greppable and survives every edit above it.
    # ⛔ THE MODE IS RESTORED; ACLs AND XATTRS ARE NOT, AND THIS COMMENT MUST NOT READ AS
    # "PERMISSIONS ARE PRESERVED" (L2 finding, 2026-09-07, measured on this box). `os.replace`
    # drops both, and nothing in the `shutil.copy*` family restores an ACL on macOS —
    # `shutil._copyxattr` is a NO-OP on darwin because `os.listxattr` does not exist there.
    # Verified: a `chmod +a "group:staff allow read"` ACE on the config is GONE after the
    # swap, and this machine's real `~/.codex/config.toml` carries a `com.apple.provenance`
    # xattr that does not survive either. So an operator who restricted this file with
    # `chmod +a` rather than a mode bit still gets it silently unrestricted here.
    # ⚠ Named rather than fixed, deliberately: the repair is a different shape (write through
    # the existing inode, or re-apply the ACL explicitly) and it trades against the atomicity
    # this block exists to provide. Routed, not taken. A false "permissions preserved" claim
    # would be worse than the documented gap — that is this repo's own rule about the
    # activation tree's symlink exception, applied here.
    # ⚠ This file is machine-global by this function's own docstring and sits beside
    # `~/.codex/auth.json`. An operator who chmod'ed it 0o600 meant it, and this is the
    # DOCUMENTED REPAIR path — the one place we are guaranteed to touch it.
    # ⚖ INSIDE the `try` on purpose: a copymode failure aborts before `os.replace`, so the
    # existing error's "Codex's registration is unchanged" stays TRUE. Fail-closed matches
    # this module's stance; best-effort here would restore the silent widening it fixes.
    # ⛔ REPLACE THE SYMLINK'S TARGET, NOT THE SYMLINK (L1 finding, 2026-09-07, reproduced).
    # `os.replace` onto a symlink REPLACES THE LINK WITH A REGULAR FILE. Measured: a
    # `~/.codex/config.toml` symlinked into a dotfiles repo came back `is_symlink=False`
    # with the repointed content, while THE REAL FILE STILL HELD THE OLD REGISTRATION — so
    # the operator's next `stow`/`chezmoi apply` silently reverts the repoint we just
    # announced. `a_true_statement_standing_where_a_thing_should_be`: the notice was
    # accurate at the moment it printed and describes a world that ends at the next re-stow.
    # ⚠ `resolve()` is safe here: the `not path.is_file()` early-return above already
    # rejected a broken link, and on a regular file it is a no-op. The operator-facing
    # messages keep saying `path` — the name they typed — while the write lands on the
    # inode that name actually refers to.
    # ⚠ KNOWN-OPEN, NOT FIXED HERE: a HARDLINKED config still loses its link (nlink 2 -> 1)
    # and the other name keeps the old content. There is no atomic-rename form that
    # preserves a hardlink; fixing it means writing through the existing inode, which is
    # exactly the truncation risk this block exists to remove. Documented in the CHANGELOG
    # rather than silently traded.
    target = path.resolve()
    tmp = target.with_name(f"{target.name}.levain-new-{time.time_ns()}")
    try:
        tmp.write_text(existing, encoding="utf-8")
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise InitError(
            f"could not write {path} ({e}). Codex's registration is unchanged"
            + (f"; your previous config is also copied at {repoint[2]}." if repoint
               else f"; your previous config is also copied at {unknown_prior}."
               if unknown_prior is not None
               else f"; your previous config is also copied at "
               f"{customized or relaunched_bak or reformatted_bak}."
               if (customized or relaunched_bak or reformatted_bak) is not None else ".")
        ) from e

    if prior_unparsed is not None:
        emit(f"  ! {path} did not parse as TOML before levain changed it, and still does not "
             f"({prior_unparsed}). levain's block is in it; check the file with codex.")
    if unknown_prior is not None:
        emit(f"  ! Codex's GLOBAL anneal_memory block was replaced in {path}.")
        emit("    Its previous store could not be read, so it was not a shape levain")
        emit("    wrote — if you had customised that block, that customisation is gone.")
        emit(f"    Your previous config is copied at {unknown_prior}")
        emit("    (Every codex session on this machine reads that block, not just this")
        emit("     install.)")

    if customized is not None:
        emit(f"  ! Codex's GLOBAL anneal_memory block was replaced in {path}.")
        emit("    It still points at the same store, but other settings in that block")
        emit("    (env vars, timeouts, or other keys) were not preserved — if you had")
        emit("    customised it, that customisation is gone.")
        emit(f"    Your previous config is copied at {customized}")
        emit("    (Every codex session on this machine reads that block, not just this")
        emit("     install.)")

    if repoint:
        old_store, new_store, bak = repoint
        emit(f"  ! Codex's GLOBAL anneal memory was pointed at {old_store}")
        emit(f"    and now points at {new_store}.")
        emit(f"    {path} backed up to {bak}")
        emit("    (Every codex session on this machine reads that store, not")
        emit("     just this install. Re-run init from the install you want it")
        emit("     reading if this was not what you meant.)")

    if relaunched:
        server = ((new_dict or {}).get("mcp_servers") or {}).get("anneal_memory") or {}
        emit(f"  Codex's GLOBAL anneal_memory registration in {path} keeps its store and now")
        emit(f"    starts the memory server as: {server.get('command')} "
             f"{' '.join(str(a) for a in server.get('args') or [])}")
        emit(f"    Your previous config is copied at {relaunched_bak}")

    if reformatted_bak is not None:
        emit(f"  Codex's GLOBAL anneal_memory block in {path} keeps its settings, but comments or")
        emit("    formatting inside that block were not kept.")
        emit(f"    Your previous config is copied at {reformatted_bak}")


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
        [sys.executable, "-P", "-m", "anneal_memory", "--db", str(store), *sub_args],
    ]
    errors: list[str] = []
    for cmd in candidates:
        try:
            from levain.launch import child_env

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                    env=child_env())
        except OSError as e:
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
    from levain.manifest import anneal_invocation, pip_invocation

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
        emit("    default — a partnership entity needs the partnership schema.")
        emit(f"    Fix: {pip_invocation()} install -U anneal-memory")
        emit(f"    Then: {anneal_invocation('--db', str(store), 'set-schema', 'partnership')}")
        return False

    emit(f"Initializing anneal-memory store at {store}...")

    # Persist the partnership schema at creation (anneal AM-INITSCHEMA).
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
    emit("    `set-schema` (the partnership schema).")
    emit(f"    Fix: {pip_invocation()} install -U anneal-memory")
    emit(f"    Then: {anneal_invocation('--db', str(store), 'init', '--schema', 'partnership')}")
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
    elif adapter == "openhands":
        # Hookless: no context file / hooks / MCP — just the adapter marker in config.
        rows.append(("adapter", install.joinpath(".levain", "config.json")))

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
        # Points FORWARD at the behavior note rather than restating it. Framed as
        # "yours to tune" ALONE this read as optional polish, immediately above a
        # block saying posture.md is the file that actually directs the entity.
        print(
            "  (activation/posture.md is what DIRECTS your entity — see below. It "
            "and\n   recency_directives.md are yours to tune as you learn the shapes "
            "your\n   substrate's training leaks.)"
        )


def _behavior_note_lines(install: Path, has_posture: bool) -> list[str]:
    """The interview's honest closing statement: what it just captured, and what
    it did NOT.

    The interview fills IDENTITY (`seed/world.md` + `seed/origin.md` — the two
    render targets). It never touches the file that shapes BEHAVIOR: the
    activation posture injected at primacy on every session, which ships as a
    generic starter. Nothing in the onboarding flow said those were different
    files, so an operator finishes having described at length how they want to
    be worked with and reasonably believes they configured it. (First reported
    from outside this machine by Alex De Groodt, 2026-07-30: *"I'm not sure if
    it's applying all that I told it."* He was right.)

    `has_posture` is passed rather than probed so the text stays pure and
    testable: a hookless adapter (openhands) installs NO activation tree, and
    naming a file that is not on disk would trade one wrong belief for another.
    Its posture ships in the package (`levain.firing.contract.DIRECTIVES`); the
    operator-editable surface there is the seed's own partnership discipline.
    """
    lines = [""]
    lines.append("What the interview did NOT set:")
    lines.append("  Your answers describe WHO you are — they were written into your")
    lines.append("  seed files under seed/, which your entity reads as CONTEXT.")
    lines.append("")
    if has_posture:
        lines.append("  They did NOT write HOW it behaves. That is a separate file:")
        lines.append(f"        {install / 'activation' / 'posture.md'}")
        lines.append("  — the block injected at the TOP of every session. It ships as a")
        lines.append("  generic starter and the interview leaves it untouched.")
        lines.append("")
        lines.append("  So if you told the interview how you want to be worked with and")
        lines.append("  want that ENFORCED rather than merely known, edit posture.md now.")
        lines.append("  (activation/recency_directives.md is the same idea applied within")
        lines.append("   a session.)")
    else:
        lines.append("  They did NOT write HOW it behaves. This adapter is hookless, so")
        lines.append("  it has no activation/ tree — its posture and drift-defense ship")
        lines.append("  inside the package and are injected by the runtime. The")
        lines.append("  operator-editable discipline is:")
        lines.append(f"        {install / 'seed' / 'partnership.md'}")
    return lines


def _next_steps_lines(install: Path, adapter: str, store_ok: bool = True) -> list[str]:
    """The post-install next-steps banner as a list of lines (leading + trailing
    blank included, so a plain print-each reproduces the CLI byte-for-byte).
    Pure — extracted from `_print_next_steps` so a web init renders the same
    guidance as structured lines.

    Carries `_behavior_note_lines` so BOTH surfaces (terminal `levain init` and
    the web init form, which renders these same lines) inherit the identity-vs-
    behavior statement from ONE site. It is placed BEFORE "Next steps:" because
    it corrects a belief the operator has just formed, and after the action list
    is where a correction goes unread.
    """
    lines: list[str] = [""]
    lines.append("=" * 60)
    if store_ok:
        lines.append("Install complete.")
    else:
        lines.append("Install PARTIAL — files laid down, store init FAILED. See above.")
    lines.append("=" * 60)
    lines.append(f"  Install:   {install}")
    lines.append(f"  Adapter:   {adapter}")
    lines.extend(
        _behavior_note_lines(
            install, has_posture=(install / "activation" / "posture.md").is_file()
        )
    )
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
    if adapter == "openhands":
        # Hookless: no verify-hooks (there are none) — the activation is the runtime
        # condenser, so the "smoke-test the hooks" guidance would be misleading.
        lines.append("  - Run it (needs the OpenHands runtime — pip install 'levain[openhands]'):")
        lines.append(f"        levain run {install}")
        lines.append("  - Sovereign by construction: it runs on an open model, keeps its OWN")
        lines.append("    memory under .levain/, and never touches your flow store.")
        lines.append(f"  - Verify the install (loud):  levain doctor --path {install}")
        lines.append("")
        return lines
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
