"""Pack manifest + seed-roster discovery — the declared composition layer.

A *pack-layer* is a directory holding a ``pack.toml`` plus a ``seed/`` tree (and,
later, an ``activation/`` tree). The base Levain templates ARE pack #0.

The load-bearing reason this layer exists: **render-vs-verbatim cannot be
inferred from a seed file's content.** ``README.md`` and ``continuity.md`` both
carry ``{{...}}`` placeholders yet must be copied byte-exact (the README's braces
are documentation; continuity's ``{{ENTITY_NAME}}`` is first-wrap scaffold) —
running them through ``render_template`` corrupts them (it strips comments and
collapses blank runs). No content marker — slot presence, the onboarding blurb —
reproduces the ``{world, origin}`` render set. So which files the interview
RENDERS is an *authored decision*, declared in ``pack.toml``; every other
``seed/*.md`` is verbatim-copied.

``pack.toml`` schema::

    name   = "levain-base"        # required, str
    order  = 0                    # optional int (default 0); higher = later = wins
    render = ["world.md", ...]    # optional ordered list (default []); the interview
                                  # sequence — order is significant, NOT glob order

Slice 1 is single-root discovery (the base pack) replacing the hard-coded roster
in ``install.py``; multi-root layering composes ``discover_roster`` over an
ordered stack of pack dirs in a later slice.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

PACK_MANIFEST_NAME = "pack.toml"


class PackError(Exception):
    """A pack manifest is missing, malformed, or references a missing seed file.

    Carries a ready-to-surface ``.args[0]`` message so install surfaces can print
    it directly (mirrors ``install.InitError``)."""


@dataclass(frozen=True)
class PackManifest:
    """The parsed ``pack.toml`` of one pack-layer."""

    name: str
    order: int = 0
    render: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeedEntry:
    """One resolved seed file in the roster: its filename, source path, and
    whether the interview RENDERS it or it is copied VERBATIM."""

    name: str
    path: Path
    mode: str  # "render" | "verbatim"

    @property
    def is_render(self) -> bool:
        return self.mode == "render"


def load_pack_manifest(pack_dir: Path) -> PackManifest:
    """Parse ``<pack_dir>/pack.toml``.

    Raises :class:`PackError` if the manifest is missing or malformed. Missing is
    an ERROR, not a defaulted empty — a missing base manifest means a corrupt
    wheel, and an absent declaration must never silently render the wrong set
    (the honesty floor: a failed read is an error, never a false default)."""
    manifest_path = pack_dir / PACK_MANIFEST_NAME
    if not manifest_path.is_file():
        raise PackError(
            f"pack manifest not found at {manifest_path}. A pack-layer must declare "
            f"a {PACK_MANIFEST_NAME}; the base templates ship one (a missing base "
            f"manifest means a corrupt wheel — reinstall with "
            f"`pip install --force-reinstall levain`)."
        )
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise PackError(f"could not read {manifest_path}: {e}") from e
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise PackError(f"could not parse {manifest_path}: {e}") from e

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PackError(f"{manifest_path}: 'name' is required and must be a non-empty string")

    order = data.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise PackError(f"{manifest_path}: 'order' must be an integer (got {order!r})")

    render_raw = data.get("render", [])
    if not isinstance(render_raw, list) or not all(isinstance(x, str) for x in render_raw):
        raise PackError(f"{manifest_path}: 'render' must be a list of filenames (got {render_raw!r})")
    if len(set(render_raw)) != len(render_raw):
        dupes = sorted({x for x in render_raw if render_raw.count(x) > 1})
        raise PackError(f"{manifest_path}: 'render' has duplicate entries: {dupes}")

    return PackManifest(name=name, order=order, render=tuple(render_raw))


def discover_roster(pack_dir: Path) -> list[SeedEntry]:
    """Resolve the seed roster of a single pack-layer.

    Returns the RENDER entries first, in the manifest's declared ``render`` order
    (that order IS the interview sequence — never glob order, which would flip
    e.g. world.md/origin.md), then the VERBATIM entries sorted by filename (a
    stable, deterministic order; verbatim files are copied independently so order
    is non-behavioral). A ``render`` entry with no matching ``seed/<name>`` is a
    :class:`PackError` (the declaration outran the files). Only ``.md`` seed
    files are supported — a non-.md file in ``seed/`` raises rather than being
    silently dropped (the honesty floor)."""
    manifest = load_pack_manifest(pack_dir)
    seed_dir = pack_dir / "seed"
    if not seed_dir.is_dir():
        raise PackError(f"pack {manifest.name!r}: seed directory not found at {seed_dir}")

    files = [f for f in seed_dir.glob("*") if f.is_file()]
    non_md = sorted(f.name for f in files if f.suffix != ".md")
    if non_md:
        raise PackError(
            f"pack {manifest.name!r}: seed/ holds non-.md file(s) {non_md}; only .md "
            f"seed files are supported (a non-.md asset would otherwise be silently "
            f"dropped from the install)."
        )
    by_name = {f.name: f for f in files}

    entries: list[SeedEntry] = []
    for name in manifest.render:
        src = by_name.get(name)
        if src is None:
            raise PackError(
                f"pack {manifest.name!r}: render lists {name!r} but seed/{name} is missing"
            )
        entries.append(SeedEntry(name=name, path=src, mode="render"))

    rendered = {e.name for e in entries}
    for name in sorted(by_name):
        if name in rendered:
            continue
        entries.append(SeedEntry(name=name, path=by_name[name], mode="verbatim"))

    return entries


def render_entries(roster: Sequence[SeedEntry]) -> list[SeedEntry]:
    """The render-mode entries, in interview order."""
    return [e for e in roster if e.is_render]


def verbatim_names(roster: Sequence[SeedEntry]) -> list[str]:
    """The verbatim-mode filenames, in roster order."""
    return [e.name for e in roster if not e.is_render]
