"""`levain doctor` — loud, in-environment health check of a Levain install.

What it checks:
  - Install layout: `seed/`, and (for a HOOKED adapter) `activation/` + hook
    scripts present and parse. A HOOKLESS adapter (openhands — detected by the
    `.levain/config.json` `adapter` marker + the absence of hosted-harness files)
    has no activation tree; that check is skipped for it, since its activation is
    the runtime condenser, not installed files.
  - Runtime: Python interpreter; `anneal-memory` available as CLI or module.
  - Store: `.levain/memory.db` opens cleanly via sqlite3.
  - Per detected adapter (Claude Code if `CLAUDE.md` + `.claude/`; Codex if
    `AGENTS.md`; openhands via the marker with no residue):
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
import re
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

    # Detect the adapter via `effective_adapter` — the SHARED classifier (doctor/verify/run
    # all use it, so they can't diverge) where hosted files DOMINATE a possibly-stale config
    # marker. So an install is HOOKLESS only when it is a clean openhands entity (marker +
    # no hosted residue): a `--force` adapter switch is read by its files, not the stale
    # marker — a healthy claude reinstall is never false-FAILed as "hookless", and an
    # openhands marker on top of a hook tree is never green-lit (its activation IS checked).
    # (Lazy import to match this module's no-top-level-levain-imports discipline.)
    from levain.install import effective_adapter

    hookless = effective_adapter(install) == "openhands"

    core: list[CheckResult] = []
    core.extend(_check_install_layout(install, expect_hooks=not hookless))
    if not hookless:
        core.extend(_check_hook_freshness(install))
    if not hookless:
        # Additive scope report — a hookless (openhands) entity has no hooks and
        # therefore no activation gate to report on.
        core.extend(_check_activation_scope(install))
    core.extend(_check_seed_content(install))
    core.extend(_check_recorded_answers(install))
    core.extend(_check_runtime(install))
    core.extend(_check_store(install))
    core.extend(_check_compat_set(install))
    for r in core:
        _emit(r)

    # Adapter detection. A hookless install (openhands) is identified by its config
    # marker (it lays down no CLAUDE.md/AGENTS.md tag-file); a hosted-harness install by
    # its tag-file. Wiring is checked downstream — a tagged install with missing wiring
    # surfaces as a wiring FAIL, not "no adapter."
    adapter_results: dict[str, list[CheckResult]] = {}
    if hookless:
        adapter_results["openhands"] = _check_openhands(install)
    else:
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
    if not invoke and not hookless:
        # A hookless install has no activation hooks — the verify-hooks nudge would mislead.
        print(
            "  Note: doctor is static — it does NOT invoke the activation hooks.\n"
            "        For the actual firing test, run `levain verify-hooks`\n"
            "        or `levain doctor --invoke`.\n"
            "        Also: hooks no-op when LEVAIN_HOOK_SUPPRESS=1 is in the env."
        )
    return 1 if failed or verify_rc != 0 else 0


# The base-install REQUIRED-minimum seed set (a static check on an installed
# dir). NOT the full seed taxonomy: the canonical seed classification — which
# files load as harness context (the roster-driven adapter @import list), which
# are reached on demand through a carrier pointer, and which are not entity
# context at all (continuity.md / README.md) — lives in `levain.packs`
# (NON_IMPORT_SEED / ON_DEMAND_SEED / BASE_IMPORT_ORDER / import_entries). A PACK
# extends the seed set, so this fixed list checks only the base minimum; if doctor
# ever needs to validate a pack-layered install's full import list it must read the
# installed adapter file or a recorded roster, not grow this constant (Slice 3
# deferral).
#
# ⚠ `spore_instructions.md` JOINED THIS LIST 2026-08-01, WITH THE MOVE THAT MADE IT
# ON-DEMAND. It had never been here: eagerly imported, its absence surfaced anyway
# (a broken `@seed/` line in the carrier). Behind a POINTER it would not — the
# carrier would instruct the entity to read a file that is not on disk, with doctor
# reporting green. That is `absence_of_signal_rendered_as_health`, and the move to
# on-demand is what would have created it. An on-demand seed needs its existence
# checked MORE than an eager one, not less.
_SEED_REQUIRED = (
    "origin.md",
    "partnership.md",
    "world.md",
    "memory.md",
    "spore_instructions.md",
)
_SEED_EXPECTED = _SEED_REQUIRED + ("continuity.md", "README.md")
_HOOK_REQUIRED = ("session_start.py", "user_prompt_submit.py", "_levain_hook.py")
_ACTIVATION_FILES = ("posture.md", "recency_directives.md")

# Base-pack seed files the interview RENDERS (fills). A surviving BARE `{{SLOT}}`
# in one of these means the file was copied-not-rendered, or a slot was never
# asked — the interview-desync class the wiring/layout checks stay green through
# (Chip's Jul-1 rehearsal: the entity ends up not knowing its operator while
# `doctor` reports healthy). VERBATIM seeds (README.md/continuity.md) legitimately
# carry `{{...}}` documentation, so they are NOT content-checked. A pack that
# adds render targets isn't covered here (no recorded roster at doctor time) —
# these two base files carry the identity slots (OPERATOR_NAME/ENTITY_NAME) that
# matter most, so it is the right minimum.
_RENDER_TARGET_SEEDS = ("world.md", "origin.md")


def _check_seed_content(install: Path) -> list[CheckResult]:
    """Content sanity: a rendered base seed carries NO unfilled `{{SLOT}}`.
    Reuses the interview's code-aware slot scan, so a code-formatted `` `{{X}}` ``
    documentation reference is not mistaken for an unfilled slot."""
    from levain.interview import _unique_slots

    results: list[CheckResult] = []
    seed = install / "seed"
    for name in _RENDER_TARGET_SEEDS:
        f = seed / name
        if not f.is_file():
            continue  # a missing required seed is the layout check's job
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            # A binary / invalid-UTF-8 seed is a corrupt install, not a crash —
            # `read_text` raises UnicodeDecodeError (a UnicodeError, NOT OSError).
            results.append(
                CheckResult(f"seed content ({name})", False, f"unreadable: {e}")
            )
            continue
        unfilled = _unique_slots(content)
        if unfilled:
            markers = ", ".join("{{" + s + "}}" for s in unfilled)
            results.append(
                CheckResult(
                    f"seed content ({name})",
                    False,
                    f"unfilled placeholder(s): {markers}",
                    "The interview did not fill this file — the operator's "
                    "identity was not captured. Re-run `levain init` and answer "
                    "every prompt.",
                )
            )
        else:
            results.append(
                CheckResult(f"seed content ({name})", True, "no unfilled placeholders")
            )
    return results


def _check_recorded_answers(install: Path) -> list[CheckResult]:
    """CONTENT, as distinct from WIRING — the check `doctor` did not have.

    The gap this closes, stated exactly: `_check_seed_content` above proves no
    `{{SLOT}}` SURVIVED, which proves the render RAN. It cannot see whether the
    render ran over the right values. A seed whose answers were shifted by one slot
    has no surviving placeholder, no missing file, no broken hook — every wiring
    check passes and `doctor` prints green over an entity that does not know who its
    operator is. That is the one failure class Levain's own pitch says cannot happen
    here, so it is the one `doctor` must be able to see.

    Three checks over the recorded interview (`.levain/answers.json`, the same map
    `levain update` re-renders from):

      1. THE RECORD EXISTS. Absent, it fails — it does not pass quietly. An install
         whose content cannot be verified is not an install verified as good, and
         rendering "unable to check" as a green tick is the precise habit this
         function exists to break.
      2. REQUIRED ANSWERS ARE NON-EMPTY. An empty required slot renders to nothing
         and leaves no placeholder behind, so it is invisible to every other check.
      3. THE SEED ON DISK STILL MATCHES THE RECORD — REPORTED, NOT FAILED.
         `render_template` substitutes values VERBATIM, so a recorded answer that is
         missing from its seed file means the seed was edited after init. That is an
         invited action, not a defect (see the note at the check itself), so it is
         surfaced as information about a future re-render, never as a failure.

    Plus `shape_violations`, which is where a slot-shift actually gets CAUGHT rather
    than merely being possible to catch.

    KNOWN, DELIBERATE LIMITS — none of these are bugs, and pretending otherwise
    would make this check a worse liar than the one it replaces:
      - A semantically WRONG answer that is well-formed is undetectable, here or
        anywhere. No mechanism can know whether "Chris" is your name.
      - Check 3 is a SUBSTRING match over the whole file, not a slot-anchored one,
        so a short or common value ("a", "CEO") can match incidentally elsewhere in
        the seed. It is reliable for the long, distinctive values (names, bios) and
        weak for terse ones — acceptable precisely because it only ever INFORMS.
      - Only slots in BOTH the base field plan and the record are checked. A `--pack`
        layer may add or replace render templates and no roster is recorded at
        doctor time (the same Slice-3 deferral `_SEED_REQUIRED` notes), so pack
        slots are passed over rather than guessed at — a false FAIL on a healthy
        packed install would teach operators to ignore this check, which costs more
        than the coverage gained.
    """
    from levain.answers import (
        SHAPE_IMPOSSIBLE,
        is_required,
        shape_violations,
    )
    from levain.install import _templates_root, read_answers

    answers = read_answers(install)
    if not answers:
        return [
            CheckResult(
                "seed content (recorded answers)",
                False,
                "no recorded interview at .levain/answers.json — seed CONTENT "
                "cannot be verified",
                "Wiring checks alone cannot tell a filled seed from a scrambled "
                "one. This means the install predates answer recording (levain "
                "0.3.13, 2026-07-07) — anything installed since records it "
                "automatically, so a NEW install never sees this. To fix an older "
                "one, re-run `levain init --force --path <install>` and answer the "
                "interview again: your anneal store, and everything the entity has "
                "learned, is preserved — only the seed is re-rendered.",
            )
        ]

    try:
        from levain.interview import build_field_plan, parse_template

        with _templates_root() as troot:
            specs = [
                parse_template(troot / "seed" / name)
                for name in _RENDER_TARGET_SEEDS
                if (troot / "seed" / name).is_file()
            ]
            # TWO lists, and keeping them apart IS the fix. `plan` is what this install's
            # interview would ASK; `fields` is what the record actually HOLDS. Narrowing to
            # `fields` before looking is what made a MISSING slot invisible — filtering by
            # `f.slot in answers` deletes the absent slot from the evidence, and the check
            # then asks whether any absent slot is blank. Nothing absent is ever blank.
            plan = build_field_plan(specs)
            fields = [f for f in plan if f.slot in answers]
    except Exception as e:  # noqa: BLE001 — an unreadable template must not crash doctor
        return [
            CheckResult(
                "seed content (recorded answers)",
                False,
                f"could not read the base interview templates to check content: {e}",
                "Reinstall the package: `pip install --force-reinstall levain`.",
            )
        ]

    if not fields:
        # A record that shares NOT ONE slot with the base seed is not a record of
        # this install's interview. The previous shape of this branch returned a
        # cheerful OK here and called it "pack-layered" — which meant a record
        # holding only `{"OPERATOR_NMAE": "Chris"}` (one typo, nothing else) passed
        # doctor green, reopening the exact "the record proves nothing" hole this
        # function exists to close. Caught at L3 by codex.
        #
        # A pack CAN legitimately reach here by replacing BOTH base render templates
        # with its own slots, so the message names that possibility instead of
        # accusing — but it still reports UNVERIFIED, because "we could not check"
        # and "we checked and it is fine" must never print the same badge.
        return [
            CheckResult(
                "seed content (recorded answers)",
                False,
                f"the recorded interview ({len(answers)} answer(s)) shares no fields "
                f"with the base seed, so its content cannot be verified",
                "Either a pack replaced both base seed templates (expected — there "
                "is nothing here to check, and doctor records no roster to tell), or "
                "this .levain/answers.json does not belong to this install. Compare "
                "it against `levain init --answers-template`.",
            )
        ]

    results: list[CheckResult] = []

    # TWO TIERS, because a blank is not one kind of problem.
    #
    # The terminal interview and the web form BOTH accept a blank answer for a
    # non-optional field (blank = skip). Failing every such install would condemn
    # people for using the product exactly as its own surfaces let them — so a blank
    # elsewhere is REPORTED, and only IDENTITY is fatal. That line is not arbitrary:
    # a thin seed still supports a working partnership, while an entity with no name
    # for itself or its operator is the failure the pitch says cannot happen.
    # `levain.answers.IDENTITY_SLOTS` is the same constant the `--answers` gate uses,
    # so the two surfaces cannot drift into two different rules.
    from levain.answers import IDENTITY_SLOTS

    empty_identity = sorted(
        f.slot for f in fields if f.slot in IDENTITY_SLOTS and not answers[f.slot].strip()
    )
    # ABSENT is a DISTINCT failure from BLANK, and collapsing the two was the hole: a
    # record is scanned for identity slots that are EMPTY, so an identity slot that was
    # never written at all — one typo'd key among otherwise-correct ones — was filtered
    # out of the evidence before the scan and printed GREEN over a seed with no operator.
    # The PARTIAL case of the same hole codex closed at L3 for the TOTAL case (the
    # `fields`-empty branch that returned a cheerful "pack-layered" OK), and the likelier
    # one by far: one mistyped key beats every key being wrong. Second
    # `absence_of_signal_rendered_as_health` in this one function, which is the argument
    # for reading the WHOLE function whenever one branch of it turns out to lie.
    missing_identity = sorted(
        f.slot for f in plan if f.slot in IDENTITY_SLOTS and f.slot not in answers
    )
    empty_other = sorted(
        f.slot
        for f in fields
        if f.slot not in IDENTITY_SLOTS and is_required(f) and not answers[f.slot].strip()
    )
    if missing_identity or empty_identity:
        # Name WHICH failure it is. "never recorded" and "recorded blank" send the
        # operator to different places — the first says the answer file is wrong (a
        # typo'd or dropped key), the second says the interview was skipped.
        parts = []
        if missing_identity:
            parts.append(f"NEVER RECORDED: {', '.join(missing_identity)}")
        if empty_identity:
            parts.append(f"recorded EMPTY: {', '.join(empty_identity)}")
        results.append(
            CheckResult(
                "seed content (answers present)",
                False,
                f"identity field(s) {' · '.join(parts)}",
                "The seed renders with nothing here and leaves no placeholder "
                "behind, so the entity does not know who it is partnering with "
                "while every other check passes. A slot that is missing entirely is "
                "usually a mistyped key in .levain/answers.json — compare it against "
                "`levain init --answers-template`. (If a pack replaced the base "
                "origin seed, these slots may not apply to this install; doctor "
                "records no roster and cannot tell, so it reports rather than "
                "assumes.) Otherwise re-run `levain init --force`, or supply them "
                "via --answers.",
            )
        )
    elif empty_other:
        results.append(
            CheckResult(
                "seed content (answers present)",
                True,
                f"{len(fields)} recorded field(s); identity present; left blank: "
                f"{', '.join(empty_other)} (a thinner seed, not a broken one)",
            )
        )
    else:
        results.append(
            CheckResult(
                "seed content (answers present)",
                True,
                f"{len(fields)} recorded field(s), none empty",
            )
        )

    # Every recorded answer must appear VERBATIM in the seed file its slot fills.
    # Two value shapes are skipped rather than mis-reported: one containing `<!--`
    # (render strips template comments AFTER substitution, so such a value can be
    # legitimately altered on its way to disk) and one containing `{{` (it would be
    # indistinguishable from an unfilled slot, which is check 1's job anyway).
    seed_cache: dict[str, str | None] = {}

    def _seed_text(name: str) -> str | None:
        if name not in seed_cache:
            f = install / "seed" / name
            try:
                seed_cache[name] = f.read_text(encoding="utf-8") if f.is_file() else None
            except (OSError, UnicodeError):
                seed_cache[name] = None
        return seed_cache[name]

    diverged: list[str] = []
    skipped: list[str] = []
    for f in fields:
        value = answers[f.slot].strip()
        if not value:
            continue
        if "<!--" in value or "{{" in value:
            # Not checkable: render strips template comments AFTER substitution, and
            # a `{{` in a value is indistinguishable from an unfilled slot. COUNTED,
            # not silently dropped — reporting "carries every recorded answer" while
            # having quietly declined to look at some of them is the same
            # unverified-as-verified move this whole function objects to.
            skipped.append(f.slot)
            continue
        text = _seed_text(f.spec_name)
        if text is None:
            continue  # a missing/unreadable seed is the layout check's job
        if value not in text:
            diverged.append(f"{f.slot} (in {f.spec_name})")
    if diverged:
        # REPORTED, NEVER FAILED — and the reason is a correction, not a softening.
        # The install banner explicitly invites this: "Files created (you can
        # hand-edit any of these)" (`install.py` `_print_manifest`) lists the seed.
        # Failing here would condemn an install for doing exactly what the product
        # told the operator to do, with no way back — `levain update` "fixes" the
        # FAIL by re-rendering from the record, i.e. by destroying the edit.
        #
        # It is also not a corruption in the first place: THE ENTITY READS THE SEED.
        # A hand-edited seed is the operator's intent, correctly delivered; the
        # record is only the re-render INPUT. So the real and only consequence is
        # forward-looking — a later re-render would overwrite these edits — and that
        # is a thing to be TOLD, not condemned for.
        results.append(
            CheckResult(
                "seed content (seed matches record)",
                True,
                f"seed edited since init for: {', '.join(sorted(diverged))} — the "
                f"seed is what the entity reads, so this is fine; note only that "
                f"a re-render (`levain update`) would restore the recorded answers "
                f"over these edits",
            )
        )
    else:
        detail = "rendered seed carries every recorded answer"
        if skipped:
            detail += (
                f" (not checkable, contains template syntax: {', '.join(sorted(skipped))})"
            )
        results.append(CheckResult("seed content (seed matches record)", True, detail))

    shapes = shape_violations(fields, answers)
    impossible = [m for sev, m in shapes if sev == SHAPE_IMPOSSIBLE]
    unusual = [m for sev, m in shapes if sev != SHAPE_IMPOSSIBLE]
    if impossible:
        results.append(
            CheckResult(
                "seed content (answer shapes)",
                False,
                "; ".join(impossible),
                "A single-line field cannot hold a multi-line value if it was "
                "answered at its own prompt — this is the signature of answers "
                "landing in the wrong slots. Check the seed reads correctly, then "
                "re-run init with a slot-keyed --answers file.",
            )
        )
    elif unusual:
        results.append(
            CheckResult(
                "seed content (answer shapes)",
                True,
                f"plausible; {len(unusual)} note(s): " + "; ".join(unusual),
            )
        )
    else:
        results.append(
            CheckResult("seed content (answer shapes)", True, "consistent with each field")
        )

    return results


def _check_install_layout(install: Path, expect_hooks: bool = True) -> list[CheckResult]:
    """Static layout check. ``expect_hooks=False`` (a hookless adapter, e.g. openhands)
    checks the seed but SKIPS the activation-tree + hook-script requirement entirely —
    those files do not exist for an adapter whose activation is the runtime condenser, so
    demanding them would FAIL a perfectly healthy sovereign entity."""
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
                # DERIVED from _SEED_REQUIRED, never hand-listed: the hand-written
                # form read "{origin,partnership,world,memory}.md" and silently went
                # stale the moment the required set changed.
                "Required: " + ", ".join(f"seed/{f}" for f in _SEED_REQUIRED),
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

    if not expect_hooks:
        # Hookless adapter (openhands): no activation tree — the condenser is the runtime
        # activation. Report it as an informational PASS, not a missing-tree FAIL.
        results.append(
            CheckResult(
                "activation/",
                True,
                "n/a — hookless adapter (activation is the runtime condenser)",
            )
        )
        return results

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


# ---------------------------------------------------------------------------
# Activation scope — the gate doctor never reported (Alex De Groodt, 2026-08-04)
#
# ⚠ THE FINDING IN ONE LINE: a healthy-looking doctor was compatible with the
# WHOLE activation layer being off. Every static check passed — hooks present,
# wired to this install, python resolvable, store open — because they all check
# FILES. What decides whether a hook emits anything is a RUNTIME gate,
# `_levain_hook.in_install_session()`, and doctor never mentioned it existed.
# Alex moved Levain into a global tool, worked in his own project directories,
# and lost posture / recency directives / spore surfacing / crystal recall / the
# wrap nudge in every session that mattered, for weeks, with doctor green.
#
# ⛔ DELIBERATELY NARROW, AND IT MUST STAY THAT WAY. This is additive: it reads
# `.levain/config.json` and the user-level harness settings and reports. It does
# NOT touch `_check_hook_freshness`, `_hook_body`, `PackProvenance`, or anything
# else the doctor redesign owns — that redesign is BLOCKED on a design defect
# (an init-time receipt cannot notice the package moved) and this finding is the
# same CLASS, so folding them together would ship the narrow fix behind a
# blocker. Recorded, not merged.
#
# Scope is a GAUGE in the GATE/GAUGE/FEED tiering, with one exception: the
# combination "install-scoped + wired ONLY at user level" is not a soft reading,
# it is a dark install, and it fails.

_SCOPE_CONFIG_REL = (".levain", "config.json")


def _configured_scope(install: Path) -> str:
    """The activation scope this install declares: ``"global"`` or ``"install"``.

    MUST agree with ``_levain_hook.configured_scope()`` — same key, same
    fail-closed rule (anything that is not exactly "global" means install
    scope). The hook cannot be imported here (it is a template, and it resolves
    its own install root from ``__file__``), so the contract is pinned by test
    instead: ``TestScopeAgreesWithHook``.

    ``LEVAIN_SCOPE`` is NOT read HERE — this function answers "what does the
    install DECLARE", which is a property of the install. The env override is
    handled by :func:`_env_scope_caveat` and reported alongside, rather than
    folded in.

    ⚠ AN EARLIER VERSION IGNORED THE ENV VAR ENTIRELY, on the reasoning that
    doctor's environment is not the session's. Two L3 seats independently
    called that wrong, and they were right: it holds for a per-invocation
    ``LEVAIN_SCOPE=global claude``, and breaks the moment an operator exports it
    from a shell profile — which is the natural way to make "always on" durable.
    Then doctor's env IS every session's env, and staying silent misreports in
    BOTH directions: green while the layer is dark (config says global, profile
    says install), and a hard FAIL on an install that is working fine (config
    says install, profile says global). The second is worse than saying nothing,
    because it sends an operator to fix something that is not broken.

    The fix is not to guess which kind of export it is. It is to stop asserting
    a scope that cannot be verified from here, and say so.
    """
    try:
        data = json.loads(install.joinpath(*_SCOPE_CONFIG_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "install"
    if not isinstance(data, dict):
        return "install"
    value = data.get("scope")
    if isinstance(value, str) and value.strip().lower() == "global":
        return "global"
    return "install"


def _user_level_wiring(install: Path) -> list[str]:
    """Settings files wiring THIS install's hooks from a place `levain init`
    did NOT put them — i.e. wiring that is an operator CHOICE.

    Returns display paths, or [] (absent/unreadable files included — this is a
    report, never a hard failure on someone else's config).

    ⛔ `~/.codex/hooks.json` IS DELIBERATELY NOT CHECKED, AND THIS IS THE WHOLE
    SUBTLETY OF THE CHECK. Codex has no per-project hooks file, so
    `levain init --adapter codex` writes the GLOBAL `~/.codex/hooks.json`
    itself (`install.py`, `hooks_target`). For a Codex install, user-level
    wiring is not a signal of anything — it is the only deployment there is,
    and every Codex install on earth would match it.

    ⚠ THIS EXACT MISTAKE ALREADY SHIPPED ONCE. levain 0.4.0 turned `doctor`
    PERMANENTLY RED for every Codex operator, because `_check_hook_freshness`
    compared every install against the Claude Code tree — and it went out in
    the release built to carry this same reporter's previous fixes. The first
    draft of THIS function repeated it, and a green suite did not catch that
    either; running `levain doctor` against a real Codex install did. The
    defect is never "does the code run", it is always "which tree is it
    pointed at".

    Claude Code is different in the way that matters: `levain init` writes
    `<install>/.claude/settings.json`, INSIDE the install. So hooks wired from
    `~/.claude/settings.json` are something the operator did on purpose, and
    combined with install scope that is precisely the dark configuration —
    invisible to every check that only reads the install's own settings.
    """
    found: list[str] = []
    # CLAUDE_CONFIG_DIR relocates the whole ~/.claude tree; checking only the
    # home path would read a directory Claude Code is not using and false-green
    # the dark configuration (codex L3).
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(cfg_dir).expanduser() if cfg_dir else Path.home() / ".claude"
    # ⚠ THE INSTALL'S OWN SETTINGS FILE IS NOT USER-LEVEL WIRING, EVEN WHEN IT
    # SITS AT THIS PATH. An install rooted at $HOME makes
    # <install>/.claude/settings.json and ~/.claude/settings.json the SAME FILE
    # — the one `levain init` wrote — so reading it as a deliberate operator
    # choice false-FAILs a perfectly correct install and makes `levain doctor`
    # exit nonzero. Same shape as the codex case below: confusing the file the
    # installer wrote with a signal of operator intent. This function's own
    # docstring already names the discriminator ("levain init writes
    # <install>/.claude/settings.json, INSIDE the install"); the code just never
    # checked that identity. Third instance of doctor going red for a whole
    # operator class, so the guard is an explicit identity test, not a path
    # heuristic. Found at review.
    try:
        own_settings = (install / ".claude" / "settings.json").resolve()
    except (OSError, ValueError):
        own_settings = None
    for path in [root / "settings.json"]:
        try:
            if not path.is_file():
                continue
            if own_settings is not None and path.resolve() == own_settings:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, script in (("SessionStart", "session_start.py"),
                              ("UserPromptSubmit", "user_prompt_submit.py")):
            entries = hooks.get(event)
            if isinstance(entries, list) and _user_entry_targets(entries, install, script):
                found.append(str(path))
                break
    return found


def _user_entry_targets(entries: list, install: Path, expected_script: str) -> bool:
    """Does ANY command in these user-level entries run THIS install's hook?

    Deliberately NOT ``_hook_command_targets``, for two reasons codex found at
    L3, both of which make that function wrong for a user-level file:

    1. It inspects only the FIRST command it finds across all entries. A foreign
       hook registered ahead of Levain's would hide Levain's entirely, and the
       dark configuration would go unreported. Here every entry and every hook
       is scanned.
    2. It substitutes ``${CLAUDE_PROJECT_DIR}`` with this install's path. That
       is right for the install's OWN settings file, where the placeholder does
       resolve to the install — and wrong here, because in a user-level file the
       harness expands it to whatever project is open, so such a command never
       runs this install's hook at all. Treating it as wiring to this install
       would then send the operator to fix a scope that was never the problem.
       User-level wiring must name an absolute path.

    Fail-soft on shape: a foreign settings file may contain anything (``None``
    entries included — an unguarded ``entry.get`` there aborted the whole doctor
    run). Never raise; this is a report about someone else's config.
    """
    expected = (install / "activation" / "hooks" / expected_script).resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command")
            if not isinstance(cmd, str) or not cmd:
                continue
            try:
                tokens = shlex.split(cmd)
            except ValueError:
                continue
            for tok in tokens[1:]:
                if "${CLAUDE_PROJECT_DIR}" in tok or "$CLAUDE_PROJECT_DIR" in tok:
                    continue  # resolves elsewhere at user level — not this install
                try:
                    # EXPAND BEFORE RESOLVING. `Path(tok)` does no variable
                    # expansion and no `expanduser`, so `~/lev/.../hook.py` and
                    # `$HOME/lev/.../hook.py` — both of which a shell DOES
                    # expand, and the second of which levain's own
                    # settings.template.json spells inside double quotes —
                    # resolved to `<cwd>/~/lev/...` and `<cwd>/$HOME/lev/...`,
                    # never equalled `expected`, and the scan returned [].
                    # A miss in `_hook_command_targets` produces a wiring FAIL,
                    # which is fail-loud; a miss HERE produces the reassuring
                    # green on the one configuration this function exists to
                    # fail — `absence_of_signal_rendered_as_health` inside the
                    # instrument built to end it. The `${CLAUDE_PROJECT_DIR}`
                    # skip stays AHEAD of this, so a placeholder is still
                    # correctly not-this-install rather than being expanded to
                    # the empty string and matching something. Measured on all
                    # four spellings (Diogenes 2026-09-03).
                    if Path(os.path.expandvars(tok)).expanduser().resolve() == expected:
                        return True
                # ⛔ RuntimeError IS IN THIS CLAUSE BECAUSE OF `expanduser`, AND
                # LEAVING IT OUT REPRODUCED THE CLASS THIS FUNCTION WAS BUILT TO
                # CLOSE. `Path("~someuser/...").expanduser()` raises RuntimeError
                # ("Could not determine home directory") for a user with no passwd
                # entry — measured, not reasoned — and RuntimeError is neither
                # OSError nor ValueError. A foreign entry naming another operator's
                # home is an ordinary thing to find in a shared
                # ~/.claude/settings.json, so without this the expansion added a
                # fresh way to ABORT THE WHOLE DOCTOR RUN on somebody else's
                # config: exactly codex#7, which
                # `test_never_raises_on_a_malformed_foreign_config` exists to
                # prevent, reintroduced by the fix for the blindness right above.
                # The question that caught it, and it is checkable AT WRITE TIME:
                # would the condition this guard covers disable the guard?
                except (OSError, ValueError, RuntimeError):
                    continue
    return False


def _env_scope_caveat() -> str:
    """A note when ``LEVAIN_SCOPE`` is set in doctor's own environment.

    Returns "" when unset. Deliberately does NOT resolve the ambiguity: doctor
    cannot tell a one-off ``LEVAIN_SCOPE=global levain doctor`` from a line in
    the operator's shell profile, and those two mean opposite things about every
    other session. Naming the value and the ambiguity is the honest report.
    """
    raw = os.environ.get("LEVAIN_SCOPE", "").strip()
    if not raw:
        return ""
    effective = "global" if raw.lower() == "global" else "install"
    return (
        f" — NOTE: LEVAIN_SCOPE={raw} is set in this shell, which overrides the "
        f"config and resolves to {effective} scope. If that is exported "
        f"persistently (a shell profile), it applies to your real sessions too "
        f"and the config line above is not the whole story"
    )


def _check_activation_scope(install: Path) -> list[CheckResult]:
    """Report the runtime gate, and fail the one combination that is dark."""
    scope = _configured_scope(install)
    env_note = _env_scope_caveat()
    env_raw = os.environ.get("LEVAIN_SCOPE", "").strip().lower()

    if scope == "global":
        # Careful with this wording. Scope opens the RUNTIME GATE; it does not
        # make a harness INVOKE the hook. A Claude Code install wired only in
        # <install>/.claude/settings.json is still never invoked outside that
        # project, so "fires in every session" would be an overclaim doctor
        # cannot back (codex L3). Report the gate, and name the other half.
        detail = (
            "global — the gate is open, so the hooks no longer suppress "
            "themselves outside the install "
            f"(set in {Path(*_SCOPE_CONFIG_REL)}). They still only run where "
            "your harness actually invokes them, so wire them at the user level "
            "if you want them everywhere"
        ) + env_note
        return [CheckResult("activation scope", True, detail)]

    # Computed HERE, not above: the global branch returns without using it, and
    # scanning a stranger's settings file for a result nobody reads is how the
    # null-entry crash fired on installs that were never going to be flagged.
    user_wiring = _user_level_wiring(install)
    if user_wiring and env_raw != "global":
        # env_raw == "global" means the gate IS open for every session in this
        # environment, so the install is NOT dark and failing it would be a
        # false alarm sending the operator to fix working software.
        where = ", ".join(user_wiring)
        return [
            CheckResult(
                "activation scope",
                False,
                f"install-scoped, but this install's hooks are wired at the user "
                f"level ({where}) — they run for sessions outside the install and "
                f"then SUPPRESS THEMSELVES, so posture, recency directives, spore "
                f"surfacing, crystal recall and the wrap nudge are all silently off "
                f"in those sessions" + env_note,
                # A fresh `levain init` writes NO config.json, so this has to
                # say CREATE, and it has to show the literal JSON — an operator
                # told to "opt in to global scope" with no example has been
                # given a diagnosis, not a fix.
                'Opt in to global scope: create or edit '
                f'{Path(*_SCOPE_CONFIG_REL)} so it contains {{"scope": "global"}} '
                "(it survives `levain update`), or set LEVAIN_SCOPE=global for a "
                "single session. To keep the hooks scoped instead, remove the "
                "user-level wiring and use the install's own .claude/settings.json.",
            )
        ]

    detail = (
        f"install-scoped (the default) — hooks fire only for sessions whose "
        f"working directory is inside {install}"
    )
    from levain.install import effective_adapter
    # NOT `(install / "AGENTS.md").is_file()`: a `levain init --adapter
    # claude-code --force` over a former codex install leaves AGENTS.md behind,
    # and the operator would get a Codex diagnostic about hooks their install no
    # longer uses — a wrong-artifact reading inside the check written to stop
    # wrong-artifact readings (glm L3). `effective_adapter` is the shared
    # classifier doctor/verify/run already agree on.
    if effective_adapter(install) == "codex":
        # Codex wires ~/.codex/hooks.json globally by design, so this operator's
        # hooks DO run in every Codex session on the machine and then no-op
        # outside the install. Saying so is the point of the check: that is the
        # condition that looked like health while the activation layer was off.
        detail += (
            " — your Codex hooks are wired globally, so they run in every Codex "
            'session and stay silent outside it. Set {"scope": "global"} in '
            f"{Path(*_SCOPE_CONFIG_REL)} if you want the entity present everywhere"
        )
    return [CheckResult("activation scope", True, detail + env_note)]


def _check_openhands(install: Path) -> list[CheckResult]:
    """The hookless (openhands) adapter check — reached only when the marker says openhands
    AND no hosted-harness residue is present (the `hookless` gate in `run_doctor` already
    guarantees the latter via `_hosted_artifacts`). So this is a clean sovereign-entity PASS;
    the seed + store + runtime health are covered by the core checks, and a hookless entity
    has no hooks/MCP to verify. (A stray hook tree makes the install non-hookless at the gate,
    routing it to the normal tag-file detection / no-adapter FAIL instead.)"""
    return [
        CheckResult(
            "adapter",
            True,
            "openhands — sovereign entity (no hooks; run it with `levain run`)",
        )
    ]


def _check_carrier_freshness(install: Path, carrier: Path) -> list[CheckResult]:
    """Does the rendered adapter carrier still match the CURRENT seed classification?

    ⚠ THE UPGRADE PATH DOES NOT RE-RENDER THIS FILE. Adapter carriers are written at
    `init` only: `update.py` never calls `apply_init`/`_install_adapter`, and
    `reconcile.py` says so outright ("the adapter @import list is NOT regenerated").
    So when a seed file changes class — as `spore_instructions.md` did on 2026-08-01,
    eager -> on-demand — every EXISTING install keeps eagerly importing it forever,
    gaining none of the benefit, while every other check stays green.

    That is the same shape as the defect being fixed: the operator cannot see it, and
    nothing tells them. Presence-only carrier checks cannot catch it (the file IS
    present; its CONTENT is stale). So this check reads the carrier and reports the
    drift with the exact remedy.

    Reported as a FAILING check rather than a note, deliberately: it is actionable and
    silently costs the operator the thing they upgraded for. Found by codex at L3 —
    the finding neither the Claude layers nor the open-weight seat produced, because
    it lives in the files this change never touched.
    """
    from levain.packs import ON_DEMAND_SEED

    try:
        text = carrier.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        return [CheckResult(f"{carrier.name} freshness", False, f"unreadable: {e}")]

    # An on-demand seed appearing as an EAGER entry — `@seed/<name>` (claude-code) or
    # a numbered "N. `seed/<name>`" read-list row (codex). The on-demand POINTER also
    # names the file, as ``- `seed/<name>` — summary``, so match the eager forms only.
    stale = [
        name
        for name in ON_DEMAND_SEED
        if f"@seed/{name}" in text or f". `seed/{name}`" in text
    ]
    if stale:
        return [
            CheckResult(
                f"{carrier.name} freshness",
                False,
                "eagerly loads seed file(s) now classified on-demand: "
                + ", ".join(stale),
                f"This install predates the change. Re-render the carrier with "
                f"`levain init --force --path {install}` (your store, seed answers "
                f"and operator edits are kept). `levain update` does NOT rewrite it.",
            )
        ]
    return [CheckResult(f"{carrier.name} freshness", True, "matches current seed classification")]


def _hook_body(text: str) -> str:
    """A hook script's comparable body: install-time placeholder substitutions
    normalised away, so a healthy install is not reported as drifted."""
    return re.sub(
        r"^_INSTALL_ANNEAL_BIN = .*$", "_INSTALL_ANNEAL_BIN = <>", text, flags=re.M
    ).strip()


def _check_hook_freshness(install: Path) -> list[CheckResult]:
    """Do the installed HOOK SCRIPTS still match the ones this package ships?

    ⚠ A THIRD SURFACE IN THE SAME CLASS, and the class is the finding. Levain
    copies artifacts into the operator's install at `init` and has NO upgrade path
    that refreshes them: `update.py` never re-installs, and `reconcile.py` only
    handles pack drift — it SURFACES activation changes rather than applying them
    (reconcile.py:296). So `pip install -U levain` upgrades the package and leaves
    the operator running the old copies. It already bit the adapter carrier; it
    bites hooks harder, because a hook fix is exactly what a bug report produces.

    Concretely, 2026-08-01: Alex De Groodt reported that `install_root()` accepted
    any ANCESTOR as the install, which silently killed the whole activation layer
    for an install nested under the launch dir. The fix ships in the package — and
    would reach nobody who merely upgraded, because their `activation/hooks/` copy
    is whatever `init` wrote.

    ⚠ SCOPE — and the correction matters, because the first version of this
    docstring claimed a mechanism the code does not use. The operator-owned files
    (`posture.md`, `recency_directives.md` — the ones every operator is explicitly
    TOLD to tune) are excluded by DIRECTORY: they live in `activation/`, while this
    walks `activation/hooks/` only. The `*.py` filter is not what protects them; it
    only skips `__pycache__`. Mutation proved it — widening the glob to `*` changed
    nothing, because the markdown was never in scope to begin with.

    The distinction still governs the check: hooks are MACHINERY and must match the
    package, activation markdown is OPERATOR-OWNED and must never be compared.
    Flagging a tuned posture would fail an operator for doing what the product asks.
    Enforced by the directory boundary — so if this ever grows to walk
    `activation/` itself, the exclusion has to become explicit.

    ⚠ THE SHIPPED TREE IS SELECTED BY ADAPTER, THROUGH `install`'s OWN HELPER — NEVER BY
    SEARCH (0.4.1, after 0.4.0 shipped the opposite). 0.4.0 iterated
    `(templates/activation/hooks, adapters/codex/activation/hooks)` and broke on the
    first present dir, under a comment asserting "the first present tree is this
    install's lineage." That was FALSE: `templates/activation/hooks` ALWAYS ships, so
    the loop always broke on the claude-code tree and the codex branch was unreachable
    dead code. Every codex install had its hooks compared against the claude-code
    hooks — which legitimately differ — so `doctor` reported them stale and exited 1
    PERMANENTLY, and the remedy it printed (`levain init --force`) rewrote the same
    codex hooks and failed again. The check written to carry the `install_root` fix to
    operators destroyed its own signal for an entire adapter lineage.

    The repair is SUBTRACTION, not a corrected branch. The rule for which tree an
    adapter installs from already exists exactly once — `install._base_activation_root`,
    the same helper `init` renders from. (The defect was `the_comment_names_one_
    authority_the_code_leaves_a_second_alive`; the fix is `a_fix_that_deletes_mechanism_
    beats_one_that_adds_it`.)

    ⚠ **AND THIS PARAGRAPH USED TO END WITH A CLAIM THAT WAS FALSE WHEN WRITTEN**
    (corrected 2026-08-03): *"doctor CANNOT disagree with install about lineage, because
    there is no second copy of the rule left to drift."* It could, and it did. `init`
    renders from `order_activation_roots(templates_root, _base_activation_root(...),
    pack_dirs)` — base is only the FIRST layer — while this check called
    `_base_activation_root` alone. **Doctor took half the helper chain**, so an install
    built with a pack that overrides a hook had that hook installed correctly and then
    reported stale against a tree it never came from: EXIT 1 permanently, with
    `init --force` unable to clear it. Byte-for-byte the 0.4.0 shape above, one release
    later, for a different population, shipped INSIDE the fix for it —
    `guard_scoped_by_symptom_misses_the_class`.

    It was latent when found (no shipped pack overrides hooks), and the confidence is
    what made it dangerous: by this very release's own argument about `_base_seed_root`,
    *"the previous docstring's confidence is what kept anyone from looking."* Pack-owned
    hooks are now resolved against `.levain/manifest.json` — see the comment in the body.
    """
    from levain.install import _base_activation_root, _templates_root, effective_adapter

    hooks_dir = install / "activation" / "hooks"
    if not hooks_dir.is_dir():
        return []  # hookless adapter, or a layout failure the layout check owns

    # A hooks/ dir with no coherent adapter identity is an INCOHERENT install, and
    # `run_doctor`'s adapter-detection block owns it — it emits
    # "no adapter detected (no CLAUDE.md or AGENTS.md at install root)" and fails the
    # run. Comparing such an install against an arbitrarily chosen tree is exactly how
    # 0.4.0 manufactured a hook-freshness failure for a defect of a different name.
    #
    # ⚠ This comment said "the LAYOUT check owns that" until 2026-08-03, and the test
    # asserting this path repeated the misattribution in its docstring. It is wrong:
    # `_check_install_layout` never inspects CLAUDE.md or AGENTS.md at all — it checks
    # the install root, seed/, _ACTIVATION_FILES, the hooks dir, _HOOK_REQUIRED presence
    # and hook syntax. Verified by reproduction: delete CLAUDE.md from a healthy install
    # and LAYOUT prints [OK] on both its checks while the FAIL comes from the adapter
    # block. No behavioural bug — the operator IS protected — but it sent the next
    # reader to the wrong function, which is the same "a comment naming a world it is
    # not in" class this release shipped to fix.
    adapter = effective_adapter(install)
    if adapter is None:
        return []

    # ⚠ PACK-OWNED HOOKS ARE NOT BASE HOOKS, AND COMPARING THEM AGAINST BASE WAS THE
    # SAME DEFECT 0.4.1 SHIPPED TO FIX — one release later, for a different population.
    #
    # `init` renders the activation tree from the LAYERED stack (install.py:387-389,
    # `order_activation_roots(templates_root, _base_activation_root(...), pack_dirs)`)
    # and a pack layer WINS per relative path (documented install.py:1620: "a pack's
    # ``hooks/x.py`` replaces base's ``hooks/x.py``"). This check read only
    # `_base_activation_root`, so a hook a pack contributed was installed CORRECTLY and
    # then reported stale against a tree it never came from — doctor EXIT 1 forever,
    # and `init --force`, the remedy this check itself prints, could not clear it.
    #
    # That makes the docstring's central claim above false as written: doctor took HALF
    # the helper chain, so there WAS a second copy of the rule left to drift. install's
    # rule is order_activation_roots(base, pack_dirs); this one was base alone.
    #
    # THE DATA WAS ALREADY ON DISK. `.levain/manifest.json` records every pack layer's
    # contributed files with their source hashes, so a pack-owned hook is checked
    # against the hash the pack ACTUALLY shipped — still real drift detection, just
    # against the right authority. Hooks are copied verbatim (not rendered), so the
    # source hash and the installed bytes are directly comparable.
    #
    # `_sha256_file` is imported rather than reimplemented ON PURPOSE (`derive_dont_
    # invent`): we are comparing against a digest the manifest computed, so the two must
    # use one hashing definition or this check drifts back into disagreeing with the
    # thing it consults.
    # ⚠ KEYED BY PATH RELATIVE TO `activation/hooks/`, **NEVER BY BASENAME** — and the loop
    # below iterates the UNION of these keys with base's. Those are two separate defects
    # with two separate fixes, and repairing either alone leaves the other alive.
    #
    # FOURTH OCCURRENCE of the shape 0.4.0 and 0.4.1 each shipped: a check operating in one
    # namespace over a tree composed in another. `install` builds the activation tree in
    # RELATIVE-PATH space as a UNION over layers; this check worked in BASENAME space over
    # BASE's file list alone. That produced two independent failures:
    #
    #   (a) a pack shipping `activation/hooks/sub/session_start.py` collapsed onto the key
    #       `session_start.py` and made doctor report a completely UNTOUCHED base hook
    #       stale — EXIT 1 permanently, which a perfect `init --force` could not clear
    #       (simulated three times: red, red, red). `install.py:1677` documents nested pack
    #       hooks as SUPPORTED, so this is not an invented shape.
    #   (b) a pack hook that base does NOT ship was installed, recorded in the manifest,
    #       and never compared to ANYTHING — its digest was read here and thrown away.
    #       Replacing one wholesale with `os.system(...)` still printed "hook scripts match
    #       the package".
    #
    # THE SUITE COULD NOT SEE EITHER — 299 tests passed patched and unpatched — because
    # every pack test used `hook: str = "session_start.py"`, the one shape in which
    # basename and relative path coincide. The cement laid after the THIRD occurrence
    # parametrised the ADAPTER axis and left the pack-path axis hardcoded, so the fourth
    # occurrence simply arrived on the unparametrised axis.
    pack_hooks: dict[str, str] = {}
    _PREFIX = "activation/hooks/"
    try:
        from levain.manifest import read_pack_locks_status

        # ⛔ `read_pack_locks_status`, NOT `read_pack_locks` — AND THE DIFFERENCE IS THE WHOLE
        # POINT. The thin wrapper returns `[]` for ABSENT and for CORRUPT alike, so a truncated
        # or partially-synced `.levain/manifest.json` silently turned pack-hook checking OFF
        # while this check went on reporting "hook scripts match the package". Tampering with an
        # installed pack hook was then invisible. `absence_of_signal_rendered_as_health`, inside
        # doctor, again — and the realistic path is not an attacker, it is a half-written file.
        #
        # ⛔⛔ THE JUSTIFICATION THAT STOOD HERE WAS FALSE: "the LOCK check owns lock integrity."
        # NO DOCTOR CHECK READS THE PACK AXIS. Measured: `read_pack_locks_status` had ZERO
        # callers in this file; its consumers are `update.py` and `reconcile.py`. Doctor's lock
        # check reads `manifest.read_lock_status` — a DIFFERENT function, covering the ENGINE
        # compat set, whose corrupt branch reports on `.levain/manifest.json` as a version
        # record and says nothing about pack provenance. The axis was unowned, and this comment
        # is what stopped anyone noticing.
        provs, pack_status = read_pack_locks_status(install)
        if pack_status == "corrupt":
            return [
                CheckResult(
                    "hook freshness",
                    False,
                    "pack provenance in .levain/manifest.json is unreadable, so pack-owned "
                    "hooks CANNOT be checked — a tampered or stale pack hook would not be "
                    "reported. This is a refusal to guess, not a hook failure.",
                    "Restore .levain/manifest.json (a truncated or partially-synced file is "
                    f"the usual cause), or re-onboard with `levain init --force --path {install}`. "
                    "Base-adapter hooks are still compared; only the pack layer is dark.",
                )
            ]
        for prov in provs:
            for rel, digest in prov.files.items():
                if rel.startswith(_PREFIX) and rel.endswith(".py"):
                    pack_hooks[rel[len(_PREFIX):]] = digest
    except Exception:
        # A genuinely ABSENT lock is the ordinary pack-less install: nothing to compare, and
        # base-only comparison is correct rather than degraded. Only `corrupt` is the silent
        # case, and it is now handled above rather than swallowed here.
        pack_hooks = {}

    stale: list[str] = []
    try:
        from levain.manifest import _sha256_file

        with _templates_root() as templates_root:
            shipped_root = _base_activation_root(adapter, templates_root) / "hooks"
            if not shipped_root.is_dir() and not pack_hooks:
                return []  # no base hooks tree and no pack hooks; nothing to compare
            # `rglob`, not `glob`: base is now read in the SAME relative-path space the
            # pack keys use, so a nested hook on either side lands on one key and the
            # union below is well defined.
            base_hooks: dict[str, Path] = {}
            if shipped_root.is_dir():
                base_hooks = {
                    str(p.relative_to(shipped_root)): p
                    for p in shipped_root.rglob("*.py")
                }
            for rel in sorted(set(base_hooks) | set(pack_hooks)):
                installed = hooks_dir / rel
                if not installed.is_file():
                    continue
                recorded = pack_hooks.get(rel)
                if recorded is not None:
                    # Pack-owned: the authority is the pack's recorded hash, not base.
                    # Reached for pack hooks base does not ship too — that is fix (b).
                    if _sha256_file(installed) != recorded:
                        stale.append(rel)
                    continue
                shipped = base_hooks[rel]
                if _hook_body(installed.read_text(encoding="utf-8")) != _hook_body(
                    shipped.read_text(encoding="utf-8")
                ):
                    stale.append(rel)
    except (OSError, UnicodeError, RuntimeError) as e:
        return [CheckResult("hook freshness", False, f"could not compare: {e}")]

    if stale:
        return [
            CheckResult(
                "hook freshness",
                False,
                "installed hook script(s) differ from the package: "
                + ", ".join(sorted(set(stale))),
                f"Your hooks predate the installed levain version, and hook fixes "
                f"do NOT arrive via `pip install -U` or `levain update`. Re-render "
                f"with `levain init --force --path {install}` (your store, seed "
                f"answers and activation markdown edits are kept).",
            )
        ]
    return [CheckResult("hook freshness", True, "hook scripts match the package")]


def _check_context_surface(install: Path, carrier: Path) -> list[CheckResult]:
    """Report the EAGER context surface — the bytes this install loads into every
    single session — broken down by what those bytes ARE.

    ⚠ THIS CHECK EXISTS BECAUSE AN OPERATOR HAD TO GUESS. Alex De Groodt, the first
    installer outside this machine: *"I'm wondering if levain isn't filling too much
    context actually."* He was right — it was 48,451 bytes, 65% of it documentation
    about the machinery — and he could only feel it, because nothing in Levain ever
    reported the number. A harness that will not tell you what it loads makes its own
    operator the instrument. That is the root under F2, and it is the part a
    one-time trim does not fix.

    The health signal is the mechanism SHARE, never the byte total: an operator with
    a long, rich `world.md` has a large surface and a HEALTHY one — that is Levain
    working. An operator whose surface is mostly docs-about-the-substrate has the
    defect. Failing on raw size would punish precisely the right behaviour.

    Reported for the file set the carrier actually imports, read from the CARRIER on
    disk rather than recomputed from the roster, so it measures what this install
    really loads — including an install that predates a classification change, or one
    the operator hand-edited.
    """
    from levain.packs import MECHANISM_SHARE_WARN, seed_role

    try:
        text = carrier.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        return [CheckResult("context surface", False, f"carrier unreadable: {e}")]

    # Both eager forms: claude-code `@seed/<name>`, codex `N. \`seed/<name>\``.
    names: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("@seed/"):
            names.append(s[len("@seed/"):].strip())
        else:
            m = re.match(r"^\d+\.\s+`seed/([^`]+)`", s)
            if m:
                names.append(m.group(1))

    by_role: dict[str, int] = {}
    total = len(text.encode("utf-8"))  # the carrier itself loads too
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        f = install / "seed" / name
        try:
            size = f.stat().st_size
        except OSError:
            continue  # a dangling import is the freshness check's job, not this one
        total += size
        by_role[seed_role(name)] = by_role.get(seed_role(name), 0) + size

    if not seen:
        return [CheckResult("context surface", True, "no eager seed imports")]

    mechanism = by_role.get("mechanism", 0)
    share = mechanism / total if total else 0.0
    breakdown = " · ".join(
        f"{role} {by_role[role]:,}B" for role in sorted(by_role) if by_role[role]
    )
    detail = (
        f"{total:,} B loaded every session across {len(seen)} seed file(s) "
        f"+ carrier — {breakdown} ({share:.0%} mechanism)"
    )
    # ⚠ REPORTS, NEVER FAILS — and that split is deliberate, not softness.
    #
    # The first version FAILED above the threshold, which meant a fresh base install
    # failed `doctor` on day one for a ratio the operator cannot act on: `memory.md`
    # is OUR composition choice, and the remedy ("move it on-demand") is a Levain
    # internals change, not something an operator does. Failing them for our decision
    # points the signal at the wrong party — the same defect class this whole change
    # is about. Enforcement belongs where the actor is, so the BUDGET is enforced
    # repo-side in `scripts/check_seed_budget.py`, which fails OUR build.
    #
    # What the operator needs here is the NUMBER, every time, which is the exact
    # thing Alex did not have. So it always prints, and says plainly whose problem a
    # skewed ratio is.
    if share > MECHANISM_SHARE_WARN:
        detail += " — mostly machinery, which is a Levain composition issue, not yours"
    return [CheckResult("context surface", True, detail)]


def _check_claude_code(install: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(CheckResult("CLAUDE.md", True, "present"))
    results.extend(_check_carrier_freshness(install, install / "CLAUDE.md"))
    results.extend(_check_context_surface(install, install / "CLAUDE.md"))

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
    results.extend(_check_carrier_freshness(install, install / "AGENTS.md"))
    results.extend(_check_context_surface(install, install / "AGENTS.md"))

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
