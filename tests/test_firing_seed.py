"""levain.firing.seed — the entity-seed reader + PresenceSource (spore-294, step 4).

The seed leaf renders an entity's OWN ``seed/*.md`` into the two identity seams (constitution +
re-anchor). Like the rest of ``levain.firing`` it is a dependency-isolated leaf (stdlib +
``levain.firing.isolation`` only) — these tests run in ANY env, including one without the
``openhands`` extra.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from levain.firing.isolation import LEVAIN_ENTITY_DIR_ENV
from levain.firing.presence import ReanchorRequest, build_presence
from levain.firing.seed import SEED_SUBDIR, EntitySeed, SeedPresence, _clean


@pytest.fixture(autouse=True)
def _unbound_env(monkeypatch):
    """Every test starts UNBOUND — no ambient $LEVAIN_ENTITY_DIR from another test. ("" reads as
    unbound: resolve_entity_dir .strip()s it to falsy.)"""
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, "")


def _seed_entity(
    tmp_path: Path,
    *,
    name: str = "Coyote",
    operator: str = "Phill Clapham",
    files: tuple[str, ...] = ("origin.md", "world.md", "partnership.md"),
) -> Path:
    """A minimal filled entity dir: ``.levain/`` + the requested ``seed/`` files."""
    ent = tmp_path / "entity"
    (ent / ".levain").mkdir(parents=True, exist_ok=True)
    seed = ent / SEED_SUBDIR
    seed.mkdir(parents=True, exist_ok=True)
    bodies = {
        "origin.md": (
            f"# Who You Are — {name}\n\n"
            "> Part of the seed. This is the template guidance note that must be stripped.\n\n"
            f"You are **{name}**. You run on minimax-m3.\n\n"
            "<!-- interview: a comment that must be stripped -->\n\n"
            f"Your job: partner with {operator}.\n"
        ),
        "world.md": f"# Who Your Operator Is\n\n## Identity\n\n{operator}. 46. Columbus, OH.\n",
        "partnership.md": "# How We Work\n\nYou are a partner, not an assistant.\n",
        "memory.md": "# Your Memory\n\nanneal mechanics — must NOT be in the constitution.\n",
    }
    for f in files:
        (seed / f).write_text(bodies[f], encoding="utf-8")
    return ent


# --- dependency isolation (the leaf invariant) --------------------------------------


def test_seed_leaf_imports_without_openhands_or_anneal():
    """A fresh interpreter importing ``levain.firing.seed`` must NOT pull OpenHands OR anneal_memory —
    the dependency-isolated-leaf invariant (a runtime check, not a source grep)."""
    code = (
        "import sys; import levain.firing.seed; "
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in {'openhands', 'anneal_memory'}); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- the constitution frame (always-loaded identity) --------------------------------


def test_constitution_composes_identity_operator_partnership(tmp_path):
    ent = _seed_entity(tmp_path, name="Coyote", operator="Phill Clapham")
    c = EntitySeed(ent).constitution()
    assert c is not None
    assert "Coyote" in c                        # origin (who it is)
    assert "Phill Clapham" in c                 # world (its operator)
    assert "partner, not an assistant" in c     # partnership (the floor)
    # order: identity → operator → how-we-work
    assert c.index("Coyote") < c.index("Phill Clapham") < c.index("partner, not an assistant")


def test_constitution_strips_guidance_and_comments(tmp_path):
    c = EntitySeed(_seed_entity(tmp_path)).constitution() or ""
    assert "template guidance note that must be stripped" not in c  # `> Part of the seed…` dropped
    assert "a comment that must be stripped" not in c  # <!-- --> dropped
    assert "<!--" not in c and "-->" not in c


def test_constitution_excludes_memory_mechanics(tmp_path):
    """memory.md is machinery, not identity/behavior — it must NOT sit in every system prompt."""
    ent = _seed_entity(tmp_path, files=("origin.md", "world.md", "partnership.md", "memory.md"))
    c = EntitySeed(ent).constitution() or ""
    assert "anneal mechanics" not in c


def test_constitution_none_when_no_seed(tmp_path):
    """A bare .levain-only entity has no seed → None (caller falls back to the generic default)."""
    ent = tmp_path / "bare"
    (ent / ".levain").mkdir(parents=True)
    assert EntitySeed(ent).constitution() is None


def test_constitution_partial_when_some_files_missing(tmp_path):
    """Only origin present → the constitution is just origin (no crash on the absent world/partnership)."""
    ent = _seed_entity(tmp_path, files=("origin.md",))
    c = EntitySeed(ent).constitution()
    assert c is not None and "Coyote" in c
    assert "---" not in c  # single part → no separator


# --- the re-anchor (compressed identity at recency) ---------------------------------


def test_reanchor_is_the_origin_charter(tmp_path):
    r = EntitySeed(_seed_entity(tmp_path)).reanchor()
    assert r is not None and "Coyote" in r
    # the re-anchor is the compressed identity (origin.md ONLY), not the full constitution frame:
    # world.md-unique content (the operator's location, its ## Identity heading) is absent.
    assert "Columbus, OH" not in r


def test_reanchor_none_when_origin_absent(tmp_path):
    ent = _seed_entity(tmp_path, files=("world.md",))  # no origin.md
    assert EntitySeed(ent).reanchor() is None


# --- resolution + isolation ---------------------------------------------------------


def test_resolves_from_env_when_dir_unset(tmp_path, monkeypatch):
    """A zero-arg EntitySeed (as build_presence rebuilds on fork) resolves the entity from
    $LEVAIN_ENTITY_DIR — the fork-safe channel, never a frozen path."""
    ent = _seed_entity(tmp_path)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))
    assert (EntitySeed().constitution() or "").find("Coyote") >= 0


def test_unbound_is_fail_soft_none(tmp_path):
    """No explicit dir + no env → None, never a raise (and never a fallback to some default store)."""
    assert EntitySeed().constitution() is None
    assert EntitySeed().reanchor() is None


def test_explicit_dir_wins_over_env(tmp_path, monkeypatch):
    a = _seed_entity(tmp_path / "a", name="Anansi")
    b = _seed_entity(tmp_path / "b", name="Coyote")
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(b))
    assert "Anansi" in (EntitySeed(a).constitution() or "")


def test_symlinked_seed_escaping_the_tree_is_refused(tmp_path):
    """watch-it (b): isolation applies to the seed too. A `seed/` DIR symlinked OUTSIDE the entity
    tree (an escape to another entity / the operator's home) fails closed — no identity read."""
    outside = tmp_path / "outside_seed"
    outside.mkdir()
    (outside / "origin.md").write_text("# Who You Are — Impostor\n\nYou are **Impostor**.\n")
    ent = tmp_path / "entity"
    (ent / ".levain").mkdir(parents=True)
    (ent / SEED_SUBDIR).symlink_to(outside, target_is_directory=True)

    assert EntitySeed(ent).constitution() is None
    assert EntitySeed(ent).reanchor() is None


def test_per_file_symlink_escape_is_refused(tmp_path):
    """apparatus HIGH-1: a legit in-tree `seed/` whose origin.md is a symlink to a FILE outside the
    tree must NOT leak the foreign identity — the guard is per-FILE, not just per-dir."""
    outside = tmp_path / "foreign"
    outside.mkdir()
    (outside / "impostor.md").write_text("# Who You Are — Impostor\n\nYou are **Impostor** SECRET.\n")
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))  # legit seed/, no origin yet
    (ent / SEED_SUBDIR / "origin.md").symlink_to(outside / "impostor.md")

    assert EntitySeed(ent).reanchor() is None  # reanchor reads origin.md → refused
    assert "Impostor" not in (EntitySeed(ent).constitution() or "")  # not leaked into the frame


def test_seed_file_resolving_into_flow_store_is_refused(tmp_path, monkeypatch):
    """apparatus HIGH-1 (forbidden zone): even when the entity root is an ANCESTOR of the flow store
    (so plain containment-under-root passes vacuously), a seed file resolving into ~/.anneal-memory/
    is refused — the same forbidden-zone check the store guard runs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    flow = tmp_path / ".anneal-memory"
    flow.mkdir()
    (flow / "memory.continuity.md").write_text("# flow\n\nYou are FLOW-OPERATOR SECRET.\n")
    ent = tmp_path  # entity dir == HOME → an ancestor of the flow store
    (ent / ".levain").mkdir(exist_ok=True)
    seed = ent / SEED_SUBDIR
    seed.mkdir(exist_ok=True)
    (seed / "origin.md").symlink_to(flow / "memory.continuity.md")

    assert EntitySeed(ent).reanchor() is None
    assert "SECRET" not in (EntitySeed(ent).constitution() or "")


def test_read_non_utf8_is_fail_soft(tmp_path):
    """apparatus HIGH-1: a non-UTF-8 byte raises UnicodeDecodeError (⊂ ValueError, NOT OSError). The
    catch must swallow it → None, never crash. A bad ORIGIN → whole constitution None (origin is
    required); a bad ENRICHER → skipped, origin still renders. Both must be crash-free."""
    # bad origin → None constitution + None reanchor (fall back to generic default)
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_bytes(b"# X\n\nYou are \xff\xfe not-utf8\n")
    assert EntitySeed(ent).reanchor() is None
    assert EntitySeed(ent).constitution() is None

    # good origin + bad enricher (world.md) → constitution still renders origin+partnership, no crash
    ent2 = _seed_entity(tmp_path / "e2", files=("origin.md", "partnership.md"))
    (ent2 / SEED_SUBDIR / "world.md").write_bytes(b"# W\n\n\xff\xfe bad\n")
    c = EntitySeed(ent2).constitution()
    assert c is not None and "Coyote" in c and "partner, not an assistant" in c


def test_seed_dir_present_distinguishes_bare_from_unreadable(tmp_path):
    """The signal build_entity_agent uses to warn: a bare .levain entity has NO seed/ (present=False,
    generic default expected); a present-but-refused seed reads as present (warn-worthy)."""
    bare = tmp_path / "bare"
    (bare / ".levain").mkdir(parents=True)
    assert EntitySeed(bare).seed_dir_present() is False
    assert EntitySeed(_seed_entity(tmp_path)).seed_dir_present() is True


# --- the _clean scaffolding strip (code-aware, leading-only) -------------------------


def test_clean_preserves_fenced_code_and_body_blockquote(tmp_path):
    """apparatus MED-1: the strip must be as careful as the interview WRITER — fenced code (a `>>>`
    REPL line) and a body blockquote (an operator's own epigraph) survive; only the LEADING guidance
    blockquote is stripped."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\n"
        "> Part of the seed. STRIP-THIS leading guidance note.\n\n"
        "You are **Z**.\n\n"
        "> My mantra: keep it loose.\n\n"  # a BODY blockquote — operator's own, must survive
        "```\n>>> agent.run()\n```\n",  # fenced REPL — the `>>>` must survive
        encoding="utf-8",
    )
    c = EntitySeed(ent).constitution() or ""
    assert "STRIP-THIS" not in c            # leading guidance dropped
    assert "My mantra: keep it loose" in c  # body blockquote preserved
    assert ">>> agent.run()" in c           # fenced code preserved


def test_constitution_leads_with_precedence_directive(tmp_path):
    """apparatus MED-2: the seed suffix lands AFTER the stock OpenHands identity, so the constitution
    leads with a precedence directive asserting the seed is who the entity is (not the model)."""
    c = EntitySeed(_seed_entity(tmp_path)).constitution() or ""
    assert c.startswith("This is who you are")
    assert "not the model" in c
    assert c.index("This is who you are") < c.index("Coyote")  # precedence leads the identity


# --- origin.md is REQUIRED / hollow-seed / codex L3 fixes ----------------------------


def test_origin_required_even_when_enrichers_readable(tmp_path):
    """apparatus codex MED-HIGH: origin.md refused (symlink escape) but world+partnership readable
    must NOT yield a hollow constitution (operator+discipline, no IDENTITY) — that is the silent
    'boots as itself' failure. constitution() returns None so the caller warns + falls back."""
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "impostor.md").write_text("# Who You Are — Impostor\n\nYou are **Impostor**.\n")
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").symlink_to(foreign / "impostor.md")

    assert EntitySeed(ent).constitution() is None       # no identity → no seed-backed constitution
    assert EntitySeed(ent).seed_dir_present() is True   # but present → the caller WARNS


def test_hollow_heading_only_seed_reads_as_empty(tmp_path):
    """apparatus codex LOW-MED: a file that cleans to headings-only (all body was guidance) is a
    hollow seed, not identity — _read returns None so it can't inject a bare '# Who You Are'."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\n> Part of the seed. All guidance, no identity body.\n", encoding="utf-8"
    )
    assert EntitySeed(ent)._read("origin.md") is None  # guidance stripped → headings-only → hollow
    assert EntitySeed(ent).constitution() is None  # origin hollow → no identity → None


def test_clean_preserves_html_comment_inside_a_fence(tmp_path):
    """apparatus codex MED: a literal <!-- --> INSIDE a code fence survives (comment stripping runs
    inside the line scanner, not as a global pre-pass), while a real guidance comment is stripped."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\nYou are **Z**.\n\n"
        "```\n<!-- literal example that must survive -->\n```\n\n"
        "<!-- real guidance that must be stripped -->\n",
        encoding="utf-8",
    )
    c = EntitySeed(ent).constitution() or ""
    assert "<!-- literal example that must survive -->" in c
    assert "real guidance that must be stripped" not in c


def test_clean_strips_multiline_comment_outside_a_fence(tmp_path):
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\nYou are **Z**.\n\n<!-- multi\nline\nguidance -->\n\nTail body.\n",
        encoding="utf-8",
    )
    c = EntitySeed(ent).constitution() or ""
    assert "You are **Z**" in c and "Tail body." in c
    assert "guidance" not in c


def test_flow_store_resolve_failure_is_fail_soft(tmp_path, monkeypatch):
    """apparatus codex LOW: a raise from resolving the forbidden flow-store path must degrade to None
    (fail-soft), never crash build. flow_store_dir() is inside the guarded try."""

    def _boom() -> Path:
        raise OSError("broken HOME")

    monkeypatch.setattr("levain.firing.seed.flow_store_dir", _boom)
    ent = _seed_entity(tmp_path)
    assert EntitySeed(ent).constitution() is None
    assert EntitySeed(ent).reanchor() is None


# --- L3-verify consensus: content-based guidance strip (epigraphs survive) ------------


def test_operator_epigraph_under_h1_is_preserved(tmp_path):
    """apparatus L3-verify CONSENSUS (complement/nemotron/codex): a leading blockquote is stripped
    ONLY when it is recognized TEMPLATE guidance — an operator's own epigraph right under the H1
    (the most natural placement) survives, not silently eaten by a position-only heuristic."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Sage\n\n"
        "> I am the space between signal and noise.\n\n"  # operator epigraph — must SURVIVE
        "You run on minimax-m3.\n",
        encoding="utf-8",
    )
    c = EntitySeed(ent).constitution() or ""
    assert "space between signal and noise" in c


def test_all_blockquote_identity_is_not_hollow(tmp_path):
    """The degenerate case: an operator whose ENTIRE identity statement is a blockquote must not be
    misclassified as hollow (→ silent generic fallback). A non-guidance leading blockquote is body."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Sage\n\n> I am Sage: a research partner. This is the whole of who I am.\n",
        encoding="utf-8",
    )
    assert EntitySeed(ent)._read("origin.md") is not None
    assert "the whole of who I am" in (EntitySeed(ent).constitution() or "")


def test_real_rendered_templates_clean_correctly(tmp_path):
    """The strongest check: the ACTUAL interview-rendered seed templates → guidance blockquotes
    (`> Part of the seed…`, `> **Seed material…`) stripped, identity/operator content kept."""
    interview = pytest.importorskip("levain.interview")
    ans = {
        "ENTITY_NAME": "Sage", "SUBSTRATE": "minimax-m3", "OPERATOR_NAME": "Phill Clapham",
        "JOB": "research partner", "AGE": "46", "LOCATION": "Columbus", "ROLES": "- architect",
        "PERSONAL_HISTORY": "bassist", "COGNITION": "plays", "HEALTH": "x", "FAMILY": "y",
        "INTERESTS": "poker", "WORK": "levain", "COMMUNICATION": "direct", "BOUNDARIES": "none",
        "STRATEGIC_DIRECTION": "z", "CONTEXT": "llc",
    }
    tmpl = Path(interview.__file__).parent / "templates" / "seed"
    for name, marker in (("origin.md", "Sage"), ("world.md", "Phill Clapham")):
        rendered = interview.render_template(interview.parse_template(tmpl / name), ans)
        cleaned = _clean(rendered)
        assert "Part of the seed" not in cleaned and "Seed material" not in cleaned
        assert marker in cleaned


def test_utf8_bom_is_handled(tmp_path):
    """apparatus L3-verify: a UTF-8 BOM must not defeat the leading-guidance strip (it kept the H1
    from matching `#`, leaking the guidance). utf-8-sig reads it away."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_bytes(
        "﻿# Who You Are — Q\n\n> Part of the seed. STRIP.\n\nYou are **Q**.\n".encode("utf-8")
    )
    c = EntitySeed(ent).constitution() or ""
    assert "STRIP" not in c
    assert "You are **Q**" in c


def test_seed_dir_present_is_fail_soft_on_permission_error(tmp_path):
    """apparatus L3-verify CRITICAL: seed_dir_present()'s is_dir() must not crash on a permission
    failure on the entity root — it fail-softs like the rest of the file."""
    import os

    ent = _seed_entity(tmp_path)
    os.chmod(ent, 0o000)
    try:
        assert EntitySeed(ent).seed_dir_present() in (True, False)  # no raise
    finally:
        os.chmod(ent, 0o755)


def test_mismatched_fence_marker_is_fenced_content(tmp_path):
    """apparatus codex (final verify): a ~~~ line inside a ``` fence — or a 3-backtick inside a
    4-backtick fence — is fenced CONTENT, not a spurious close. So a literal <!-- --> after it, still
    inside the fence, survives (the 'fenced code untouched' guarantee holds with (char,len) tracking)."""
    bt, bt4 = "`" * 3, "`" * 4
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        f"# Who You Are — Z\n\nYou are **Z**.\n\n{bt}\n"
        f"~~~ not a closing fence\n<!-- literal comment stays -->\n{bt}\n\n"
        f"{bt4}\n{bt}\ninner three-backticks\n{bt}\n{bt4}\n",
        encoding="utf-8",
    )
    c = EntitySeed(ent).constitution() or ""
    assert "~~~ not a closing fence" in c
    assert "<!-- literal comment stays -->" in c
    assert "inner three-backticks" in c


def test_multiline_contiguous_guidance_is_fully_stripped(tmp_path):
    """The world.md guidance is a MULTI-line blockquote; stripping only the first line would leak the
    continuation. The whole contiguous `>` run (opened by a guidance marker) is dropped."""
    ent = _seed_entity(tmp_path, files=("origin.md", "partnership.md"))
    (ent / SEED_SUBDIR / "world.md").write_text(
        "# Who Your Operator Is\n\n"
        "> **Seed material — operator template.** blah blah.\n"
        ">\n"
        "> **No fast-moving state.** more guidance text.\n\n"
        "## Identity\n\nPhill Clapham. Columbus.\n",
        encoding="utf-8",
    )
    c = EntitySeed(ent).constitution() or ""
    assert "Seed material" not in c and "No fast-moving" not in c  # whole block gone
    assert "Phill Clapham" in c  # real content survives


def test_glued_epigraph_consumed_but_blank_separated_survives(tmp_path):
    """Documented behavior (apparatus codex): a contiguous `>` run IS one blockquote — an epigraph
    glued onto a guidance line (no blank) is part of that block and consumed; a blank-separated
    epigraph is a SEPARATE block and survives."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    # glued (no blank between) → consumed with the guidance
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\n"
        "> Part of the seed. strip this.\n"
        "> glued epigraph with no blank line\n\n"
        "You are **Z**.\n",
        encoding="utf-8",
    )
    glued = EntitySeed(ent).constitution() or ""
    assert "glued epigraph" not in glued and "You are **Z**" in glued

    # blank-separated → a separate blockquote, survives
    ent2 = _seed_entity(tmp_path / "e2", files=("world.md", "partnership.md"))
    (ent2 / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\n"
        "> Part of the seed. strip this.\n\n"
        "> a real epigraph, blank-separated\n\n"
        "You are **Z**.\n",
        encoding="utf-8",
    )
    sep = EntitySeed(ent2).constitution() or ""
    assert "a real epigraph, blank-separated" in sep and "strip this" not in sep


def test_has_body_excludes_rules_and_fence_markers(tmp_path):
    """apparatus codex low-med: a file that cleans to headings + a horizontal rule / bare fence is
    still hollow (no substantive identity body)."""
    ent = _seed_entity(tmp_path, files=("world.md", "partnership.md"))
    (ent / SEED_SUBDIR / "origin.md").write_text(
        "# Who You Are — Z\n\n> Part of the seed. Guidance.\n\n---\n", encoding="utf-8"
    )
    assert EntitySeed(ent)._read("origin.md") is None  # H1 + rule only → hollow


# --- SeedPresence + the registry (serialization-safe reconstruction) ----------------


def test_seed_presence_reanchors_from_env(tmp_path, monkeypatch):
    ent = _seed_entity(tmp_path)
    monkeypatch.setenv(LEVAIN_ENTITY_DIR_ENV, str(ent))
    out = SeedPresence().reanchor(ReanchorRequest())
    assert out is not None
    assert "re-anchor" in out.lower() and "Coyote" in out


def test_seed_presence_unbound_is_none(tmp_path):
    assert SeedPresence().reanchor(ReanchorRequest()) is None


def test_build_presence_lazy_imports_the_seed_leaf():
    """build_presence('entity_seed') resolves via the blessed lazy allowlist — the fork-safe cold
    rebuild path — and yields a SeedPresence."""
    p = build_presence("entity_seed")
    assert isinstance(p, SeedPresence)


def test_build_presence_lazy_in_a_cold_interpreter():
    """The real fork path: a fresh interpreter that imported ONLY levain.firing.presence (never the
    seed leaf) can still build 'entity_seed' via the lazy allowlist."""
    code = (
        "from levain.firing.presence import build_presence; "
        "p = build_presence('entity_seed'); "
        "assert type(p).__name__ == 'SeedPresence', type(p)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout
