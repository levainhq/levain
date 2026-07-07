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
    # Optional human-readable version (semver, no enforced format). NOT used for
    # composition; recorded into the install's pack provenance purely as sugar for
    # the drift NOTICE ("v1.2 -> v1.3" reads nicer than "content changed"). Drift
    # DETECTION is hash-based, so a pack needs no version for `levain update` to
    # reconcile it.
    version: str | None = None


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
    # A pack name is an IDENTIFIER, and it is used as a filesystem path COMPONENT
    # (the docs layer dir `<order>-<name>` in install._copy_pack_docs). A path
    # separator or NUL in the name could traverse/escape the destination and
    # overwrite files outside `.levain/docs` — so refuse it at the parse gate
    # (fail-closed, structural; a malformed/hostile pack can't reach the copy).
    # codex L3 HIGH.
    if "/" in name or "\\" in name or "\x00" in name:
        raise PackError(
            f"{manifest_path}: 'name' must be a plain identifier with no path "
            f"separators (got {name!r})"
        )

    order = data.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise PackError(f"{manifest_path}: 'order' must be an integer (got {order!r})")

    render_raw = data.get("render", [])
    if not isinstance(render_raw, list) or not all(isinstance(x, str) for x in render_raw):
        raise PackError(f"{manifest_path}: 'render' must be a list of filenames (got {render_raw!r})")
    if len(set(render_raw)) != len(render_raw):
        dupes = sorted({x for x in render_raw if render_raw.count(x) > 1})
        raise PackError(f"{manifest_path}: 'render' has duplicate entries: {dupes}")

    version = data.get("version")
    if version is not None and not (isinstance(version, str) and version.strip()):
        raise PackError(
            f"{manifest_path}: 'version', if present, must be a non-empty string "
            f"(got {version!r})"
        )

    return PackManifest(name=name, order=order, render=tuple(render_raw), version=version)


def _scan_seed_layer(pack_dir: Path, manifest: PackManifest) -> dict[str, Path]:
    """Validate one layer's ``seed/`` and index it ``{filename: path}``.

    Honesty floor: a missing ``seed/`` directory, a non-.md asset (which would
    otherwise be silently dropped), or a ``render`` entry naming a file the layer
    does not provide all raise :class:`PackError`."""
    seed_dir = pack_dir / "seed"
    if not seed_dir.is_dir():
        raise PackError(f"pack {manifest.name!r}: seed directory not found at {seed_dir}")
    try:
        files = [f for f in seed_dir.glob("*") if f.is_file()]
    except OSError as e:
        raise PackError(f"pack {manifest.name!r}: could not read {seed_dir}: {e}") from e
    non_md = sorted(f.name for f in files if f.suffix != ".md")
    if non_md:
        raise PackError(
            f"pack {manifest.name!r}: seed/ holds non-.md file(s) {non_md}; only .md "
            f"seed files are supported (a non-.md asset would otherwise be silently "
            f"dropped from the install)."
        )
    by_name = {f.name: f for f in files}
    for name in manifest.render:
        if name not in by_name:
            raise PackError(
                f"pack {manifest.name!r}: render lists {name!r} but seed/{name} is missing"
            )
    return by_name


def compose_roster(pack_dirs: Sequence[Path]) -> list[SeedEntry]:
    """Compose an ordered STACK of pack-layers into one seed roster.

    Each entry of ``pack_dirs`` is a pack-layer directory (``pack.toml`` +
    ``seed/``). Layers are sorted by manifest ``order`` ascending (the base pack,
    ``order = 0``, comes first); the LAST layer providing a filename WINS
    (override). A file's render-vs-verbatim mode and its interview position
    follow its winning layer's manifest — so a layer overriding a render file
    (e.g. ``world.md``) must RE-LIST it in its own ``render`` to keep it rendered,
    else the override ships verbatim with unfilled ``{{...}}`` placeholders.

    Render entries come first, ordered by each render file's FIRST appearance
    (layer rank, then position in that layer's ``render`` list) — so base render
    files keep their interview order, a pack's new render files append after, and
    overriding a render file's CONTENT does not reorder the interview. Verbatim
    entries follow, sorted by filename (non-behavioral — each is copied
    independently)."""
    if not pack_dirs:
        raise PackError("compose_roster requires at least one pack layer")
    loaded = sorted(
        [(d, load_pack_manifest(d)) for d in pack_dirs],
        key=lambda dm: dm[1].order,
    )

    winning_path: dict[str, Path] = {}
    is_render: dict[str, bool] = {}
    sort_key: dict[str, tuple[int, int]] = {}  # (layer rank, render position) — render files only
    for rank, (pack_dir, manifest) in enumerate(loaded):
        by_name = _scan_seed_layer(pack_dir, manifest)
        render_index = {name: i for i, name in enumerate(manifest.render)}
        for name, path in by_name.items():
            winning_path[name] = path
            if name in render_index:
                is_render[name] = True
                # setdefault = FIRST render appearance keeps the interview position
                # stable: overriding a render file's CONTENT does not reorder the
                # interview (a base render file stays where base put it).
                sort_key.setdefault(name, (rank, render_index[name]))
            else:
                is_render[name] = False
                sort_key.pop(name, None)  # a verbatim override of a prior layer's render file

    render_names = sorted((n for n in winning_path if is_render[n]), key=lambda n: sort_key[n])
    verbatim = sorted(n for n in winning_path if not is_render[n])
    return (
        [SeedEntry(name=n, path=winning_path[n], mode="render") for n in render_names]
        + [SeedEntry(name=n, path=winning_path[n], mode="verbatim") for n in verbatim]
    )


def discover_roster(pack_dir: Path) -> list[SeedEntry]:
    """Resolve the seed roster of a SINGLE pack-layer — the base case of
    :func:`compose_roster`. Render entries in the manifest's declared ``render``
    order (the interview sequence — never glob order, which would flip
    world.md/origin.md), then verbatim entries sorted by filename. A ``render``
    entry with no matching ``seed/<name>`` raises; only ``.md`` seed files are
    supported (a non-.md file raises rather than being silently dropped)."""
    return compose_roster([pack_dir])


def order_activation_roots(
    base_pack_dir: Path, base_activation: Path, pack_dirs: Sequence[Path]
) -> list[Path]:
    """The activation-tree layer roots in winning order (last wins) for an install.

    The activation peer of :func:`compose_roster`'s seed layering: each layer
    carries its ``pack.toml`` ``order``; the result is stable-sorted by ``(order,
    input rank)`` ascending — IDENTICAL ordering to the seed roster (base = pack
    #0, higher ``order`` = later = WINS, equal orders keep input order). So a pack's
    ``activation/posture.md`` overrides base exactly as its ``seed/world.md`` does,
    and a pack ordered BELOW base LOSES to base (base is later) — the same edge
    ``compose_roster`` produces.

    Base's ``order`` is READ from ``base_pack_dir``'s ``pack.toml`` — the SAME
    source :func:`compose_roster` reads (``compose_roster([base_pack_dir, ...])``)
    — NOT hard-coded, so the seed and activation layerings can never desync on base
    order (today both see ``order = 0``). ``base_activation`` is the base activation
    TREE, passed separately because it sits at a per-adapter path
    (``templates/activation`` for Claude Code, ``adapters/codex/activation`` for
    Codex), distinct from ``base_pack_dir`` (``templates_root``).

    Returns ``base_activation`` plus each pack's ``<pack>/activation`` dir THAT
    EXISTS — a pack's activation tree is OPTIONAL (a pack may layer only seed
    files); base is always included (it is the adapter's required tree). Raises
    :class:`PackError` if ``base_activation`` is MISSING (a corrupt wheel — and a
    pack contributing files must not mask it) or a pack manifest is malformed
    (though in the install flow ``compose_roster`` has already validated those)."""
    # The base activation tree is REQUIRED (it carries the entity's posture/hooks).
    # A missing one = corrupt wheel — fail loud HERE so a pack contributing files
    # can't mask an absent base (the composed map would be non-empty, slipping past
    # the downstream empty-composition guard and installing a base-less tree). codex
    # L3 re-verify HIGH.
    if not base_activation.is_dir():
        raise PackError(
            f"base activation tree not found at {base_activation}. The wheel may be "
            f"corrupt; reinstall with `pip install --force-reinstall levain`."
        )
    # (order, input rank, root, is_base) — the explicit (order, rank) key
    # reproduces compose_roster's stable sort over [base, *packs] exactly,
    # including the below-base base-wins edge. base order is READ (not 0-literal)
    # so it tracks compose_roster's same-source read.
    base_order = load_pack_manifest(base_pack_dir).order
    layers: list[tuple[int, int, Path, bool]] = [(base_order, 0, base_activation, True)]
    for rank, pack_dir in enumerate(pack_dirs, start=1):
        manifest = load_pack_manifest(pack_dir)
        layers.append((manifest.order, rank, pack_dir / "activation", False))
    layers.sort(key=lambda layer: (layer[0], layer[1]))
    return [root for _order, _rank, root, is_base in layers if is_base or root.is_dir()]


def render_entries(roster: Sequence[SeedEntry]) -> list[SeedEntry]:
    """The render-mode entries, in interview order."""
    return [e for e in roster if e.is_render]


def verbatim_entries(roster: Sequence[SeedEntry]) -> list[SeedEntry]:
    """The verbatim-mode entries (name + winning-layer source path), in roster
    order. Carries the source path so a copy reads from the file's WINNING layer
    (which may be a pack), not a reconstructed base path."""
    return [e for e in roster if not e.is_render]


def verbatim_names(roster: Sequence[SeedEntry]) -> list[str]:
    """The verbatim-mode filenames, in roster order."""
    return [e.name for e in verbatim_entries(roster)]


# --- import classification: which seed files load as always-on harness context ---
#
# Orthogonal to render-vs-verbatim (the composition mode): a seed file is render
# OR verbatim AND, separately, imported-as-context OR not. This is the canonical
# seed import taxonomy — install.py renders the per-adapter import list from it,
# and doctor.py's required-seed list points here (one source of truth).

# Seed files that are NOT loaded as always-on harness context. EVERYTHING ELSE in
# the roster IS imported — a pack's new methodology file loads by DEFAULT (fail
# toward loading: a seed file that installs to disk but never reaches context is
# the invisible-infrastructure failure the adapter import list exists to prevent).
#   - continuity.md is the entity's living memory; it loads through the
#     anneal-memory server (the ``anneal://continuity`` resource), not as a static
#     context file. Importing it as static text would fork the memory surface.
#   - README.md documents the seed directory for a human browsing it; it is not
#     entity context.
NON_IMPORT_SEED: tuple[str, ...] = ("continuity.md", "README.md")

# The authored load ORDER of the base methodology-core seed files — the curriculum
# sequence (who you are -> how we work -> who your operator is -> your memory ->
# your open loops). This is an ORDER HINT, not a membership gate: an importable
# seed file NOT named here (a pack's addition, or a future base file) still loads,
# appended after these in roster order. Overriding one of these (same filename)
# keeps its curriculum position.
BASE_IMPORT_ORDER: tuple[str, ...] = (
    "origin.md",
    "partnership.md",
    "world.md",
    "memory.md",
    "spore_instructions.md",
)


def import_entries(roster: Sequence[SeedEntry]) -> list[SeedEntry]:
    """The seed entries that load as always-on harness context, in load order.

    Membership: every roster seed file EXCEPT those in :data:`NON_IMPORT_SEED`
    (continuity.md / README.md) — so a pack's new seed file imports by default.

    Order: the :data:`BASE_IMPORT_ORDER` curriculum first (for the base files the
    roster actually provides, each taken from its WINNING layer), then any other
    importable file appended in roster order — so a pack's additions land after
    the base curriculum. Override-stable: overriding a base file's CONTENT keeps
    its curriculum position (the lookup is by filename, the entry is the winner)."""
    by_name = {e.name: e for e in roster}
    ordered: list[SeedEntry] = []
    seen: set[str] = set()
    for name in BASE_IMPORT_ORDER:
        entry = by_name.get(name)
        if entry is not None and name not in NON_IMPORT_SEED:
            ordered.append(entry)
            seen.add(name)
    for entry in roster:
        if entry.name in seen or entry.name in NON_IMPORT_SEED:
            continue
        ordered.append(entry)
        seen.add(entry.name)
    return ordered


def import_names(roster: Sequence[SeedEntry]) -> list[str]:
    """The importable seed filenames, in load order (see :func:`import_entries`)."""
    return [e.name for e in import_entries(roster)]
