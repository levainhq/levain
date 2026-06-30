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
    discover_roster,
    load_pack_manifest,
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
