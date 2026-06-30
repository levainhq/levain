"""Tests for the pack-composition layer (levain/packs.py).

The load-bearing test is `test_discover_roster_reproduces_base_partition`: the
data-driven roster on the REAL shipped base templates must reproduce the exact
render-vs-verbatim partition the install pipeline previously hard-coded. That is
the behavior-preservation guard for the roster refactor — if it holds, the
installed seed files are byte-identical to the pre-refactor install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from levain.install import _templates_root
from levain.packs import (
    PackError,
    SeedEntry,
    compose_roster,
    discover_roster,
    import_entries,
    import_names,
    load_pack_manifest,
    order_activation_roots,
    render_entries,
    verbatim_names,
)

# The partition the install pipeline depended on before the roster refactor.
BASE_RENDER = ["world.md", "origin.md"]  # ordered = the interview sequence
BASE_VERBATIM = {
    "partnership.md",
    "memory.md",
    "spore_instructions.md",
    "continuity.md",
    "README.md",
}


def _write_pack(root: Path, toml: str, seed_files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.toml").write_text(toml, encoding="utf-8")
    seed = root / "seed"
    seed.mkdir()
    for name, body in seed_files.items():
        (seed / name).write_text(body, encoding="utf-8")
    return root


# ---------- base manifest ----------

def test_base_pack_manifest_parses():
    with _templates_root() as tr:
        m = load_pack_manifest(tr)
    assert m.name == "levain-base"
    assert m.order == 0
    assert m.render == ("world.md", "origin.md")


# ---------- THE GUARD: roster reproduces the historical partition ----------

def test_discover_roster_reproduces_base_partition():
    with _templates_root() as tr:
        roster = discover_roster(tr)
    render = [e.name for e in render_entries(roster)]
    verbatim = set(verbatim_names(roster))
    # render set AND ORDER are behavioral (the interview sequence).
    assert render == BASE_RENDER
    # verbatim membership is behavioral; order is not.
    assert verbatim == BASE_VERBATIM
    # total partition is the full seed/ set, disjoint.
    assert set(render).isdisjoint(verbatim)
    assert set(render) | verbatim == {e.name for e in roster}


def test_render_entries_carry_real_source_paths():
    with _templates_root() as tr:
        roster = discover_roster(tr)
        for e in render_entries(roster):
            assert e.path.is_file()
            assert e.path.name == e.name
            assert e.is_render


# ---------- manifest validation (honesty floor) ----------

def test_load_pack_manifest_missing_raises(tmp_path: Path):
    with pytest.raises(PackError, match="pack manifest not found"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_malformed_toml_raises(tmp_path: Path):
    # The TOMLDecodeError honesty-floor branch — fail loud on a broken manifest.
    (tmp_path / "pack.toml").write_text('name = "x" = broken\n', encoding="utf-8")
    with pytest.raises(PackError, match="could not parse"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_requires_name(tmp_path: Path):
    (tmp_path / "pack.toml").write_text("order = 1\n", encoding="utf-8")
    with pytest.raises(PackError, match="'name' is required"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_rejects_non_int_order(tmp_path: Path):
    (tmp_path / "pack.toml").write_text('name = "x"\norder = "high"\n', encoding="utf-8")
    with pytest.raises(PackError, match="'order' must be an integer"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_rejects_non_bool_order_true(tmp_path: Path):
    # bool is an int subclass — guard against `order = true` silently becoming 1.
    (tmp_path / "pack.toml").write_text('name = "x"\norder = true\n', encoding="utf-8")
    with pytest.raises(PackError, match="'order' must be an integer"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_rejects_bad_render(tmp_path: Path):
    (tmp_path / "pack.toml").write_text('name = "x"\nrender = "world.md"\n', encoding="utf-8")
    with pytest.raises(PackError, match="'render' must be a list"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_rejects_duplicate_render(tmp_path: Path):
    # A malformed ordered list with dupes must fail loud, not silently double-render.
    (tmp_path / "pack.toml").write_text(
        'name = "x"\nrender = ["world.md", "world.md"]\n', encoding="utf-8"
    )
    with pytest.raises(PackError, match="duplicate entries"):
        load_pack_manifest(tmp_path)


def test_load_pack_manifest_render_defaults_empty(tmp_path: Path):
    (tmp_path / "pack.toml").write_text('name = "verbatim-only"\n', encoding="utf-8")
    m = load_pack_manifest(tmp_path)
    assert m.render == ()
    assert m.order == 0


# ---------- discovery semantics ----------

def test_discover_roster_render_order_follows_manifest_not_glob(tmp_path: Path):
    # Manifest declares b before a; glob order is alphabetical (a, b). The roster
    # must honor the MANIFEST order — render order is the interview sequence.
    _write_pack(
        tmp_path,
        'name = "p"\nrender = ["b.md", "a.md"]\n',
        {"a.md": "A {{X}}", "b.md": "B {{Y}}"},
    )
    roster = discover_roster(tmp_path)
    assert [e.name for e in render_entries(roster)] == ["b.md", "a.md"]


def test_discover_roster_unlisted_files_are_verbatim(tmp_path: Path):
    _write_pack(
        tmp_path,
        'name = "p"\nrender = ["world.md"]\n',
        {"world.md": "{{X}}", "doctrine.md": "static", "notes.md": "static"},
    )
    roster = discover_roster(tmp_path)
    assert [e.name for e in render_entries(roster)] == ["world.md"]
    assert set(verbatim_names(roster)) == {"doctrine.md", "notes.md"}


def test_discover_roster_verbatim_only_pack(tmp_path: Path):
    # A pack with no render list (pure knowledge) — everything copies.
    _write_pack(
        tmp_path,
        'name = "domain"\norder = 10\n',
        {"audit_method.md": "doctrine", "sizing.md": "doctrine"},
    )
    roster = discover_roster(tmp_path)
    assert render_entries(roster) == []
    assert set(verbatim_names(roster)) == {"audit_method.md", "sizing.md"}


def test_discover_roster_render_lists_missing_file_raises(tmp_path: Path):
    _write_pack(tmp_path, 'name = "p"\nrender = ["nope.md"]\n', {"present.md": "x"})
    with pytest.raises(PackError, match="render lists 'nope.md'"):
        discover_roster(tmp_path)


def test_discover_roster_missing_seed_dir_raises(tmp_path: Path):
    (tmp_path / "pack.toml").write_text('name = "p"\n', encoding="utf-8")
    with pytest.raises(PackError, match="seed directory not found"):
        discover_roster(tmp_path)


def test_discover_roster_rejects_non_md_seed_file(tmp_path: Path):
    # A non-.md asset must fail loud, never be silently dropped from the install.
    _write_pack(tmp_path, 'name = "p"\nrender = ["world.md"]\n', {"world.md": "{{X}}"})
    (tmp_path / "seed" / "logo.png").write_bytes(b"\x89PNG\r\n")
    with pytest.raises(PackError, match=r"only \.md"):
        discover_roster(tmp_path)


# ---------- multi-layer composition (Slice 2) ----------

def test_compose_single_layer_reproduces_base_partition():
    # compose_roster([base]) — the single-layer path — must reproduce the base
    # partition against INDEPENDENT literals. (Asserting it equals discover_roster
    # would be tautological: discover_roster IS compose_roster([dir]).)
    with _templates_root() as tr:
        roster = compose_roster([tr])
    assert [e.name for e in render_entries(roster)] == BASE_RENDER
    assert set(verbatim_names(roster)) == BASE_VERBATIM


def test_compose_empty_raises():
    with pytest.raises(PackError, match="at least one pack layer"):
        compose_roster([])


def test_compose_unions_new_files(tmp_path: Path):
    base = _write_pack(tmp_path / "base", 'name = "base"\norder = 0\nrender = ["world.md"]\n',
                       {"world.md": "{{X}}", "partnership.md": "P"})
    dom = _write_pack(tmp_path / "dom", 'name = "dom"\norder = 10\n',
                      {"audit.md": "doctrine", "sizing.md": "doctrine"})
    roster = compose_roster([base, dom])
    assert {e.name for e in roster} == {"world.md", "partnership.md", "audit.md", "sizing.md"}
    assert [e.name for e in render_entries(roster)] == ["world.md"]
    assert set(verbatim_names(roster)) == {"partnership.md", "audit.md", "sizing.md"}


def test_compose_last_layer_wins_by_filename(tmp_path: Path):
    base = _write_pack(tmp_path / "base", 'name = "base"\norder = 0\n', {"world.md": "BASE"})
    over = _write_pack(tmp_path / "over", 'name = "over"\norder = 10\n', {"world.md": "OVERRIDE"})
    roster = compose_roster([base, over])
    entry = next(e for e in roster if e.name == "world.md")
    assert entry.path.read_text() == "OVERRIDE"


def test_compose_precedence_by_order_not_arg_order(tmp_path: Path):
    lo = _write_pack(tmp_path / "lo", 'name = "lo"\norder = 10\n', {"x.md": "LO"})
    hi = _write_pack(tmp_path / "hi", 'name = "hi"\norder = 20\n', {"x.md": "HI"})
    for args in ([lo, hi], [hi, lo]):
        entry = next(e for e in compose_roster(args) if e.name == "x.md")
        assert entry.path.read_text() == "HI"


def test_compose_render_order_base_first_then_pack(tmp_path: Path):
    base = _write_pack(
        tmp_path / "base",
        'name = "base"\norder = 0\nrender = ["world.md", "origin.md"]\n',
        {"world.md": "{{A}}", "origin.md": "{{B}}"},
    )
    role = _write_pack(tmp_path / "role", 'name = "role"\norder = 20\nrender = ["queue.md"]\n',
                       {"queue.md": "{{C}}"})
    roster = compose_roster([base, role])
    assert [e.name for e in render_entries(roster)] == ["world.md", "origin.md", "queue.md"]


def test_compose_verbatim_override_of_render_file_becomes_verbatim(tmp_path: Path):
    # The documented footgun: overriding a render file WITHOUT re-listing it in
    # the override's `render` ships it verbatim (with unfilled placeholders).
    base = _write_pack(tmp_path / "base", 'name = "base"\norder = 0\nrender = ["world.md"]\n',
                       {"world.md": "{{X}}"})
    over = _write_pack(tmp_path / "over", 'name = "over"\norder = 10\n',
                       {"world.md": "static override"})
    entry = next(e for e in compose_roster([base, over]) if e.name == "world.md")
    assert entry.mode == "verbatim"
    assert entry.path.read_text() == "static override"


def test_scan_seed_layer_unreadable_dir_raises_packerror(tmp_path: Path, monkeypatch):
    # An OSError reading seed/ must surface as PackError, so run_init's clean
    # FAIL: path catches it instead of an unhandled traceback (codex L3 LOW).
    _write_pack(tmp_path, 'name = "p"\n', {"a.md": "x"})

    def boom(self, pattern):  # noqa: ANN001
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "glob", boom)
    with pytest.raises(PackError, match="could not read"):
        discover_roster(tmp_path)


def test_compose_render_override_relisted_stays_render(tmp_path: Path):
    base = _write_pack(tmp_path / "base", 'name = "base"\norder = 0\nrender = ["world.md"]\n',
                       {"world.md": "{{X}}"})
    over = _write_pack(tmp_path / "over", 'name = "over"\norder = 10\nrender = ["world.md"]\n',
                       {"world.md": "{{Y}} override"})
    entry = next(e for e in compose_roster([base, over]) if e.name == "world.md")
    assert entry.mode == "render"
    assert entry.path.read_text() == "{{Y}} override"


def test_compose_same_order_tie_is_deterministic_arg_order_last_wins(tmp_path: Path):
    # Equal `order`: the stable sort preserves arg order, so the LATER pack wins a
    # filename collision. Pin it — a regression to an unstable sort would slip.
    a = _write_pack(tmp_path / "a", 'name = "a"\norder = 5\n', {"x.md": "A"})
    b = _write_pack(tmp_path / "b", 'name = "b"\norder = 5\n', {"x.md": "B"})
    entry = next(e for e in compose_roster([a, b]) if e.name == "x.md")
    assert entry.path.read_text() == "B"


def test_compose_render_override_keeps_base_interview_position(tmp_path: Path):
    # A pack overrides + re-lists a base render file: CONTENT comes from the pack
    # but the interview POSITION stays where base put it (first-appearance order).
    # Overriding content must not reorder the interview.
    base = _write_pack(
        tmp_path / "base",
        'name = "base"\norder = 0\nrender = ["world.md", "origin.md"]\n',
        {"world.md": "{{A}}", "origin.md": "{{B}}"},
    )
    over = _write_pack(tmp_path / "over", 'name = "over"\norder = 10\nrender = ["world.md"]\n',
                       {"world.md": "{{A}} override"})
    roster = compose_roster([base, over])
    assert [e.name for e in render_entries(roster)] == ["world.md", "origin.md"]
    world = next(e for e in roster if e.name == "world.md")
    assert world.path.read_text() == "{{A}} override"


# --- import classification (Slice 3) ------------------------------------------

def _entry(name: str, path: Path | None = None, mode: str = "verbatim") -> SeedEntry:
    return SeedEntry(name=name, path=path or Path(f"/src/{name}"), mode=mode)


# The base compose order: render entries (world, origin) then verbatim (alpha).
_BASE_ROSTER = [
    _entry("world.md", mode="render"),
    _entry("origin.md", mode="render"),
    _entry("continuity.md"),
    _entry("memory.md"),
    _entry("partnership.md"),
    _entry("README.md"),
    _entry("spore_instructions.md"),
]

_CURRICULUM = ["origin.md", "partnership.md", "world.md", "memory.md", "spore_instructions.md"]


def test_import_entries_curriculum_order_excludes_continuity_and_readme():
    """The base import list is the authored curriculum order — NOT the roster's
    render-then-verbatim-alpha order — and continuity.md + README.md never load
    (continuity loads via the anneal server; README is documentation)."""
    names = import_names(_BASE_ROSTER)
    assert names == _CURRICULUM
    assert "continuity.md" not in names
    assert "README.md" not in names


def test_import_entries_pack_file_loads_appended_after_curriculum():
    """A pack's NEW seed file imports BY DEFAULT (fail toward loading), appended
    after the base curriculum — the load-bearing Slice 3 behavior."""
    roster = [*_BASE_ROSTER, _entry("audit_method.md")]
    names = import_names(roster)
    assert names[:5] == _CURRICULUM
    assert names[-1] == "audit_method.md"


def test_import_entries_override_keeps_position_and_uses_winning_entry():
    """Overriding a base file's CONTENT keeps its curriculum position (lookup by
    filename) and resolves to the WINNING layer's entry (its source path), no
    matter where the override sits in the roster."""
    pack_partnership = _entry("partnership.md", path=Path("/pack/partnership.md"))
    roster = [e for e in _BASE_ROSTER if e.name != "partnership.md"]
    roster.append(pack_partnership)  # winning override at the END of the roster
    result = import_entries(roster)
    assert [e.name for e in result] == _CURRICULUM       # position 2 preserved
    assert result[1].path == Path("/pack/partnership.md")  # the winning entry


def test_import_entries_multiple_pack_files_append_in_roster_order():
    """Two pack additions both load, appended in roster order after the base."""
    roster = [*_BASE_ROSTER, _entry("audit_method.md"), _entry("sizing.md")]
    assert import_names(roster) == [*_CURRICULUM, "audit_method.md", "sizing.md"]


# ---------- order_activation_roots: the activation peer of seed layering ----------
#
# These pin that the activation tree composes with the SAME ordering semantics as
# the seed roster (base = pack #0 with its OWN pack.toml order, higher order = later
# = WINS, ties keep input order, below-base order loses to base) and that a pack's
# activation/ tree is OPTIONAL. Signature is
# order_activation_roots(base_pack_dir, base_activation, pack_dirs) — base_pack_dir
# carries base's pack.toml order; base_activation is the (per-adapter) tree path.

def _base_pack(tmp_path: Path, order: int = 0) -> tuple[Path, Path]:
    """A base pack-layer dir (pack.toml + seed/) plus its activation/ tree; returns
    (base_pack_dir, base_activation)."""
    base_pack = _write_pack(
        tmp_path / "base", f'name = "levain-base"\norder = {order}\n', {"world.md": "BASE\n"}
    )
    base_act = base_pack / "activation"
    base_act.mkdir()
    return base_pack, base_act


def _activation_pack(root: Path, order: int, *, with_activation: bool) -> Path:
    """A minimal valid pack layer (pack.toml + seed/), optionally with an
    activation/ dir. order_activation_roots reads only the manifest + the
    activation/ dir's existence, so the seed body is immaterial here."""
    _write_pack(root, f'name = "p{order}"\norder = {order}\n', {"x.md": "x\n"})
    if with_activation:
        (root / "activation").mkdir()
    return root


def test_order_activation_roots_base_only(tmp_path: Path):
    base_pack, base_act = _base_pack(tmp_path)
    assert order_activation_roots(base_pack, base_act, []) == [base_act]


def test_order_activation_roots_includes_pack_with_activation(tmp_path: Path):
    base_pack, base_act = _base_pack(tmp_path)
    pack = _activation_pack(tmp_path / "pack", 10, with_activation=True)
    assert order_activation_roots(base_pack, base_act, [pack]) == [
        base_act, pack / "activation"
    ]


def test_order_activation_roots_drops_pack_without_activation(tmp_path: Path):
    """A pack's activation/ tree is OPTIONAL — a seed-only pack contributes no
    activation root (base is the whole stack)."""
    base_pack, base_act = _base_pack(tmp_path)
    pack = _activation_pack(tmp_path / "pack", 10, with_activation=False)
    assert order_activation_roots(base_pack, base_act, [pack]) == [base_act]


def test_order_activation_roots_orders_by_pack_order(tmp_path: Path):
    """Higher pack order = later = wins; base (order 0) first. Passed in REVERSE
    input order to prove it sorts by manifest order, not input order."""
    base_pack, base_act = _base_pack(tmp_path)
    lo = _activation_pack(tmp_path / "lo", 10, with_activation=True)
    hi = _activation_pack(tmp_path / "hi", 20, with_activation=True)
    assert order_activation_roots(base_pack, base_act, [hi, lo]) == [
        base_act, lo / "activation", hi / "activation"
    ]


def test_order_activation_roots_equal_order_keeps_input_order(tmp_path: Path):
    """Equal order → stable sort keeps input order (matches compose_roster)."""
    base_pack, base_act = _base_pack(tmp_path)
    a = _activation_pack(tmp_path / "a", 10, with_activation=True)
    b = _activation_pack(tmp_path / "b", 10, with_activation=True)
    assert order_activation_roots(base_pack, base_act, [a, b]) == [
        base_act, a / "activation", b / "activation"
    ]


def test_order_activation_roots_below_base_order_loses_to_base(tmp_path: Path):
    """A pack ordered BELOW base sorts BEFORE base → base is LATER → base WINS, the
    same edge compose_roster's [base, *packs] stable sort produces."""
    base_pack, base_act = _base_pack(tmp_path)  # base order 0
    neg = _activation_pack(tmp_path / "neg", -5, with_activation=True)
    # base is last in the returned winning order → it overrides the pack.
    assert order_activation_roots(base_pack, base_act, [neg]) == [neg / "activation", base_act]


def test_order_activation_roots_missing_base_raises_even_with_pack(tmp_path: Path):
    """A MISSING base activation tree fails loud even when a pack contributes
    activation files — a pack must not mask an absent base (codex L3 re-verify
    HIGH: otherwise the composed map is non-empty and an entity installs with no
    base posture/hooks)."""
    base_pack = _write_pack(
        tmp_path / "base", 'name = "levain-base"\norder = 0\n', {"world.md": "x\n"}
    )
    missing_base_act = base_pack / "activation"  # deliberately NOT created
    pack = _activation_pack(tmp_path / "pack", 10, with_activation=True)
    with pytest.raises(PackError, match="base activation tree not found"):
        order_activation_roots(base_pack, missing_base_act, [pack])


def test_order_activation_roots_tracks_base_order_like_compose_roster(tmp_path: Path):
    """order_activation_roots READS base's pack.toml order (not a hard-coded 0), so
    it tracks compose_roster's same-source read: a NON-ZERO base order flips the
    winner identically in both. The structural invariant against seed/activation
    desync (L1 MED). base order 10, pack order 5 → pack sorts before base → base
    WINS in BOTH layerings."""
    base_pack, base_act = _base_pack(tmp_path, order=10)
    pack = _write_pack(tmp_path / "pack", 'name = "p"\norder = 5\n', {"world.md": "PACK\n"})
    (pack / "activation").mkdir()
    # seed layering (compose_roster): base wins world.md (base order 10 > pack 5)
    roster = compose_roster([base_pack, pack])
    world_winner = next(e.path for e in roster if e.name == "world.md")
    assert world_winner == base_pack / "seed" / "world.md"
    # activation layering must AGREE: base wins → base_act is LAST in winning order.
    roots = order_activation_roots(base_pack, base_act, [pack])
    assert roots[-1] == base_act
