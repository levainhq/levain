"""levain.dashboard — Levain v2: the read-only substrate view (Slices 1 + 1.5).

The v2 spine is *the substrate escapes the session*. Today a Levain partnership
entity only exists INSIDE a live Claude Code / Codex session — to see your memory,
your open loops, the health of your Hebbian graph, who the entity is and how it
thinks, you have to be mid-conversation. This module is the first move out:
assemble the entity's WHOLE substrate — the anneal stores AND the install's
seed/config surface — into a single ``SubstrateView`` that renders from outside
(the local sovereign web-app, a CLI, the parked in-host MCP-App).

Slice 1 served State + health + graph + crystals + spores. **Slice 1.5 shows
EVERYTHING, read-only:** all six neocortex sections, the seed/config surface
(origin / operator / posture / constitution), recent episodes, the wrap-history
timeline — organized by the ``Identity · Operate · Mind`` IA. Every emitted
surface carries its **edit-class** (A = operator config / B = lifecycle verb /
C = consolidated cognition, read-only) and **zone**; the ``layout()`` manifest
declares the render program in Python, so the frontend renders affordances FROM
the substrate schema and Slice 2 turns on per-class editing without a data-layer
rewrite (``structural_invariants_beat_discipline`` at the UI layer).

This module is the host-agnostic DATA half. It imports ``anneal_memory`` in-process
(no IPC wall, no CLI shell, no MCP round-trip — Levain and anneal are both Python and
the store is local) and calls the library read APIs directly. The CLI's ``--json``
shapes are the reference for WHAT to serve; this calls ``store.*`` itself.

Three properties make this the correct first slice:
- **read-only** — acts on nothing, so "human is the fan-in" holds trivially.
- **migration-free** — reads the operator's EXISTING store; zero re-seed.
- **billing-immune** — invoked human-present, never headless.

Failure discipline mirrors the recall hook: each sub-read is independently
fail-soft. A corrupt crystal file, an absent spore store, an unwrapped entity with
no continuity yet, a missing ``seed/`` dir — none of these blank the whole board.
Every degradation lands in ``SubstrateView.errors`` so the surface can SHOW that a
tier is unavailable rather than silently rendering it empty (a silent-empty health
panel is exactly the ``invisible_infrastructure_failure`` the dashboard exists to
make visible).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from levain.writes import WriteScope

# anneal-memory is Levain's layer-1 dependency (pinned >=0.8 in pyproject); these
# imports are in-process library calls, not a subprocess. Import lazily inside the
# builder so importing this module never hard-fails if anneal is mis-installed —
# the failure surfaces as a SubstrateView error, consistent with every other tier.

__all__ = [
    "AnnealPaths",
    "SubstrateSource",
    "Health",
    "GraphNode",
    "GraphEdge",
    "AssociationGraph",
    "CrystalEntry",
    "OpenSpore",
    "EpisodeRow",
    "WrapRow",
    "ConfigDoc",
    "Section",
    "SubstrateView",
    "build_substrate_view",
    "render_text",
    "render_summary",
    "run_dashboard",
]


# --- the IA: zones (Identity · Operate · Mind) + edit-classes (A/B/C) -------
# The three zones encode the governance model — the IA IS the edit-class
# structure. Edit-classes: A = operator config (direct edit, human-is-fan-in,
# safe); B = lifecycle data (anneal's validated verbs, never raw writes); C =
# consolidated cognition (the consolidate single-writer owns it — READ-ONLY, the
# human influences it only via inputs). 1.5 renders every class read-only; the
# tags are present so Slice 2 turns on affordances per class with no rewrite.
ZONE_IDENTITY = "identity"
ZONE_OPERATE = "operate"
ZONE_MIND = "mind"
CLASS_A = "A"
CLASS_B = "B"
CLASS_C = "C"

# The ONE neocortex section the operator directly edits (Class A). It is live-state
# (last-writer-wins; flow's own "quick update" discipline treats it as a direct
# targeted edit, no consolidate needed) — an INPUT to cognition, not a conclusion the
# consolidate produced. Every other section (Patterns / Decisions / Context /
# Understanding / Active Threads) is the felt layer the consolidate single-writer owns
# → Class C, read-only. This name is the single source of truth for that split; both
# the read layer (`_parse_sections`) and the write layer (`writes._apply_state_edit`)
# derive from `_section_edit_class` below, so they can never drift apart.
STATE_HEADING = "State"


def _section_edit_class(name: str) -> str:
    """The edit-class of a neocortex section, by name. ``State`` is the lone Class-A
    (operator-editable live-state) section; every other section is Class-C (the
    consolidate owns it — read-only). The one rule, used by BOTH the read render and
    the Slice-2b write boundary, so the writable set provably matches what the
    dashboard tags editable (``structural_invariants_beat_discipline`` at the
    edit-class layer)."""
    return CLASS_A if name == STATE_HEADING else CLASS_C


# ---------------------------------------------------------------------------
# Path resolution — derive the four sibling stores from the episodic db stem,
# mirroring anneal's own convention (``memory.db`` → ``memory.continuity.md`` /
# ``memory.spores.json`` / ``memory.crystal.json``). Each path is overridable so
# Levain's installer (which already knows the real {{ANNEAL_MEMORY}} location)
# can pass them explicitly rather than rely on derivation.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnnealPaths:
    """The four files a partnership entity's substrate lives in."""

    episodic_db: Path
    continuity_md: Path
    crystal_json: Path
    spores_json: Path

    @classmethod
    def from_db(cls, episodic_db: str | Path) -> "AnnealPaths":
        """Derive the sibling stores from the episodic db path, by stem.

        ``/x/memory.db`` → continuity ``/x/memory.continuity.md``,
        spores ``/x/memory.spores.json``, crystal ``/x/memory.crystal.json``.
        """
        db = Path(episodic_db).expanduser()
        parent, stem = db.parent, db.stem
        return cls(
            episodic_db=db,
            continuity_md=parent / f"{stem}.continuity.md",
            crystal_json=parent / f"{stem}.crystal.json",
            spores_json=parent / f"{stem}.spores.json",
        )


@dataclass(frozen=True)
class SubstrateSource:
    """WHERE a substrate view is assembled from — the data-source seam.

    Slice 1.5 has exactly one source: a local Levain install (its anneal store +
    its install-root seed/config files). The seam exists so a *team commons*
    source (``scope="team"``, a different paths bundle) is a SWAP, not a rewrite —
    ``build_substrate_view`` + ``SubstrateView`` + the render core all stay
    source-agnostic (they don't care where the bytes came from). This is
    ``canonical_object_model_plus_replaceable_surfaces`` at the data layer.

    ``anneal`` locates the four anneal stores; ``install_root`` (optional) locates
    the seed/config surface (``<root>/seed/*``, ``<root>/activation/*``) which does
    NOT live next to the db. ``scope`` is carried through to the view (the IA's
    personal|team axis; 1.5 only ever emits ``personal``)."""

    anneal: AnnealPaths
    install_root: Path | None = None
    scope: str = "personal"
    # The governed WRITE surface (the write-path peer, `writes.WriteScope`). Decoupled
    # from `install_root` so a NON-install substrate (flow's own store — no `.levain/`)
    # can still be writable. ``None`` → a read-only source (no write affordances, the
    # `POST /edit` route 422s). ``.local()`` auto-derives it from the install; a bare /
    # read-only inspection source leaves it ``None``.
    write_scope: WriteScope | None = None
    # Optional view-limit override carried ON the source so EVERY build() path respects
    # it — the web /substrate.json, the TUI refresh, and post-write rebuilds all call a
    # bare source.build(), not just the first render. None → build_substrate_view's own
    # defaults apply. [codex L3 MED]
    max_spores: int | None = None
    # Surface-level branding override (entity name + masthead wordmark/model). The bridge
    # flow-brands its cockpit; None → the view's derived entity + the renderers' Levain
    # default chrome (so a bare Levain install is untouched).
    entity_name: str | None = None
    brand_wordmark: str | None = None
    brand_model: str | None = None

    @classmethod
    def local(cls, install_root: str | Path) -> "SubstrateSource":
        """A Levain install keeps its anneal store at ``<install>/.levain/memory.db``
        (the convention ``doctor._match_store`` enforces) and its seed/config files
        at the install root. Resolve both from the install directory — and derive the
        governed write surface from the same install (so a local install is writable,
        exactly as before the WriteScope decoupling)."""
        from levain.writes import WriteScope  # lazy: avoid the dashboard↔writes cycle

        root = Path(str(install_root)).expanduser().resolve()
        return cls(
            anneal=AnnealPaths.from_db(root / ".levain" / "memory.db"),
            install_root=root,
            scope="personal",
            write_scope=WriteScope.from_install_root(root),
        )

    def build(self, **kwargs: Any) -> "SubstrateView":
        """Assemble the view from this source. The single call entry points use,
        so a team-commons source flows through unchanged. A source-level ``max_spores``
        is applied to EVERY build path (an explicit kwarg still wins)."""
        if self.max_spores is not None:
            kwargs.setdefault("max_spores", self.max_spores)
        view = build_substrate_view(
            self.anneal,
            install_root=self.install_root,
            ledger_root=self.write_scope.ledger_root if self.write_scope else None,
            scope=self.scope,
            **kwargs,
        )
        # Surface branding override (the bridge flow-brands; a bare Levain install leaves
        # these None → the view's derived entity + the renderers' own default chrome).
        if self.entity_name is not None:
            view.entity_name = self.entity_name
        if self.brand_wordmark is not None:
            view.brand_wordmark = self.brand_wordmark
        if self.brand_model is not None:
            view.brand_model = self.brand_model
        return view


# ---------------------------------------------------------------------------
# Result shapes — plain dataclasses, JSON-serializable via to_dict(). These are
# the dashboard's vocabulary; the web-app + MCP-App serialize them, the browser
# renders them. Kept faithful to the underlying reads (no speculative fields) —
# sourdough_scoping, not a designed-ahead schema.
# ---------------------------------------------------------------------------

@dataclass
class Health:
    """The "is this substrate alive and well?" panel — the dashboard's reason to
    exist. ``write_path_live`` is the headline: a Hebbian graph with zero links
    after real wraps is the silent-0-links death the operator can't see from
    inside a session."""

    write_path_live: bool
    total_links: int
    avg_strength: float
    max_strength: float
    density: float
    local_density: float  # density among CONNECTED episodes — the network-health metric
    links_formed_total: int
    links_strengthened_total: int
    links_decayed_total: int
    graduations_validated_total: int
    graduations_demoted_total: int
    total_episodes: int
    episodes_since_wrap: int
    episodes_by_type: dict[str, int]
    tombstones: int
    continuity_chars: int | None
    total_wraps: int
    last_wrap_at: str | None
    wrap_in_progress: bool  # a wrap is mid-flight → this snapshot may be inconsistent

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class GraphNode:
    id: str
    type: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class GraphEdge:
    source: str
    target: str
    strength: float
    co_citations: int
    affective_tag: str | None
    affective_intensity: float  # AssociationPair defaults this to 0.0 — never None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class AssociationGraph:
    """The Hebbian co-citation graph — the visual heart of the dashboard. Nodes
    are episodes that have at least one association above threshold; edges carry
    strength + co-citation count + limbic (affective) tagging."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False  # nodes capped for render

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "truncated": self.truncated,
        }


@dataclass
class CrystalEntry:
    """One row of the crystallized-pattern index — the always-loadable name+clause
    menu of graduated wisdom that lives off the always-on context budget."""

    name: str
    level: int
    one_clause: str
    permanence: str
    activation_mode: str
    last_activated_on: str
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class OpenSpore:
    """An open prospective loop — the entity's unfinished intentions. ``tier`` /
    ``salience`` / ``next`` convey live state; germination is computed, not stored."""

    id: str
    type: str
    tier: str
    salience: int
    domain: str
    text: str
    seen: str
    next: str | None
    pointer: str | None
    # The valid Class-B resolve kinds for THIS spore's type, derived from anneal's
    # own DESCEND_BY_TYPE / ASCEND_BY_TYPE (Slice 2b-ii). Schema-driven from the
    # substrate so the plane's verb affordances can't drift from anneal's taxonomy
    # (principle #4). Empty for an unknown type → the plane offers no resolve verb.
    descend_kinds: list[str] = field(default_factory=list)
    ascend_kinds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class EpisodeRow:
    """One recent episode — the raw input layer the consolidate compresses from.
    The PANEL is Class B (read-only here): per scope §4's edit-class taxonomy the
    chip encodes the EDIT MODEL, and the only episode mutation is a verb-mediated
    tombstone (never a raw write), exactly like spores — so the Operate zone is
    coherently verb-mediated. (§6's build-note loosely said "Class A"; §4, the
    canonical table, governs — and seam #2 keys Slice-2 affordances off this chip,
    so it must signal verb-mediated, not direct-edit.) The dashboard surfaces the
    entity's own facts, truncated for the list."""

    id: str
    timestamp: str
    type: str
    source: str
    content: str
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class WrapRow:
    """One historical wrap — the consolidate's audit trail surfaced as a timeline.
    The read foundation for the **projection-history viewer** (Slice 2b-ii / spore-093
    v0): each wrap is a RE-PROJECTION of the continuity from the moving substrate, not
    a saved version — so the timeline teaches *view-as-of + re-project, never restore*
    (the Slice-2 RESTORE half was killed 2026-06-14 as incoherent over a projection of
    a live multi-layer substrate). v0 shows the per-wrap size delta; the full
    content/lineage `as-of` viewer is v1 (an anneal-side content-store dep). Class C —
    read-only consolidated history."""

    wrapped_at: str
    episodes_compressed: int
    continuity_chars: int
    graduations_validated: int
    graduations_demoted: int
    associations_formed: int
    associations_strengthened: int
    associations_decayed: int

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ConfigDoc:
    """A seed/config document from the install root (NOT the anneal store).

    Edit-classes (Slice-2 sharpening of scope §4):
    - ``world.md`` sections (who the **operator** is) + ``posture.md`` /
      ``recency_directives.md`` (how it thinks) → **Class A** (operator inputs,
      directly editable: you fix facts about yourself / tune the thinking style).
    - ``origin.md`` (who the **entity** is — its own birth self-statement) →
      **Class C-view** (read-only). Per scope §1 the operator governs *inputs* and
      never overwrites the entity's cognition; the origin self-statement is the
      entity's, not the operator's, so it is NOT directly editable (the prior §4
      draft lumped it with the operator's ``world.md`` as Class A — corrected). The
      operator's legitimate influence on the entity's self is the future
      correction-as-input channel (§9), not a file rewrite; until then it reads.
    - the constitution (``partnership.md`` / ``memory.md`` / ``spore_instructions.md``)
      → **Class C-view** (the advanced "fork my methodology" edit is Slice 2+).

    All live in the Identity zone. ``heading`` is the exact ``## `` section heading
    for a per-section doc (``world.md`` splits one doc per section), else ``None``
    for a whole-file doc — it is the write address the Slice-2 edit path round-trips
    to locate the section unambiguously (the slug in ``key`` is lossy)."""

    key: str
    title: str
    body: str
    edit_class: str
    zone: str
    source: str  # the file it came from, relative to the install root
    heading: str | None = None  # exact ## heading for a per-section doc, else None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Section:
    """A neocortex narrative section (State / Active Threads / Patterns / Decisions
    / Context / Understanding), rendered as-is. The dashboard does not re-derive
    these — it surfaces the entity's own words. ``State`` is Class A (free-text,
    last-writer-wins — directly editable in Slice 2); the rest are Class C (the
    consolidate owns them, read-only). All live in the Mind zone."""

    heading: str
    body: str
    edit_class: str = CLASS_C
    zone: str = ZONE_MIND

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SubstrateView:
    """The whole substrate, assembled. Any tier may be ``None`` / empty with a
    matching entry in ``errors`` — degrade visibly, never silently. ``layout()``
    declares the ordered, zoned, edit-classed render program the frontend renders
    from."""

    paths: AnnealPaths
    scope: str = "personal"
    entity_name: str | None = None
    # Optional masthead branding (the SURFACE's identity, not the substrate's). None →
    # the renderers' Levain defaults. The bridge sets these to flow-brand its cockpit
    # without forking the shared web assets / TUI chrome.
    brand_wordmark: str | None = None
    brand_model: str | None = None
    health: Health | None = None
    graph: AssociationGraph | None = None
    crystal_index: list[CrystalEntry] = field(default_factory=list)
    open_spores: list[OpenSpore] = field(default_factory=list)
    episodes: list[EpisodeRow] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    config_docs: list[ConfigDoc] = field(default_factory=list)
    wraps: list[WrapRow] = field(default_factory=list)
    recent_edits: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def layout(self) -> list[dict[str, Any]]:
        """The declared render program: an ordered list of panels, each with its
        ``zone`` (identity/operate/mind), ``edit_class`` (A/B/C), ``title``, and a
        ``ref`` index into the relevant collection for multi-item kinds. The
        frontend renders FROM this — the IA + governance model live HERE in Python,
        not hardcoded in JS, so the app cannot drift from what it edits. Slice 2
        reads ``edit_class`` to enable affordances; 1.5 renders read-only.

        Panels are emitted Identity → Operate → Mind (tab/scroll order). Singleton
        kinds (health/graph/crystals/spores/episodes/wraps) read their data from the
        matching view field; indexed kinds (config/section) carry ``ref``."""
        panels: list[dict[str, Any]] = []

        # --- Identity: the seed/config surface (who + how it thinks) ---------
        for i, d in enumerate(self.config_docs):
            panels.append(
                {
                    "kind": "config",
                    "zone": d.zone,
                    "edit_class": d.edit_class,
                    "title": d.title,
                    "ref": i,
                    "source": d.source,  # render which seed/config file this is [codex L3]
                    "heading": d.heading,  # exact ## section (world.md) write-address; None for whole-file
                }
            )

        # --- Operate: the inputs/loops you steer -----------------------------
        panels.append(
            {"kind": "spores", "zone": ZONE_OPERATE, "edit_class": CLASS_B,
             "title": f"Open loops ({len(self.open_spores)})"}
        )
        panels.append(
            {"kind": "episodes", "zone": ZONE_OPERATE, "edit_class": CLASS_B,
             "title": f"Recent episodes ({len(self.episodes)})"}
        )
        # the operator's own Class-A edit log (Slice 2a) — the audit/undo surface;
        # no edit-class chip (it's the record OF edits, not an editable tier).
        panels.append(
            {"kind": "edits", "zone": ZONE_OPERATE, "edit_class": "",
             "title": f"Recent edits ({len(self.recent_edits)})"}
        )

        # --- Mind: the cognition you observe (don't puppet) ------------------
        panels.append(
            {"kind": "health", "zone": ZONE_MIND, "edit_class": CLASS_C, "title": "Health"}
        )
        panels.append(
            # kind stays "graph" (wire contract; the association-graph payload
            # still rides view.graph for its stats + Slice-2 topology). The
            # renderer draws the cognition-trace oscilloscope from view.wraps —
            # real per-wrap vitals, per the no-theater design rule.
            {"kind": "graph", "zone": ZONE_MIND, "edit_class": CLASS_C,
             "title": "Cognition trace"}
        )
        panels.append(
            {"kind": "crystals", "zone": ZONE_MIND, "edit_class": CLASS_C,
             "title": f"Crystallized patterns ({len(self.crystal_index)})"}
        )
        # Section panels carry the same write-address pair config panels do
        # (``source`` + ``heading``) so a Class-A section (State) can be edited
        # through the governed seam. ``source`` is the continuity file's install-
        # relative path (the convention constant — matches writes._apply_state_edit's
        # own target derivation); the read-only Class-C sections carry it too
        # (harmless — the frontend only opens an affordance on edit_class "A").
        _cont_src = str(Path(*LEVAIN_CONTINUITY_REL))
        for i, s in enumerate(self.sections):
            panels.append(
                {"kind": "section", "zone": s.zone, "edit_class": s.edit_class,
                 "title": s.heading, "ref": i,
                 "source": _cont_src, "heading": s.heading}
            )
        panels.append(
            {"kind": "wraps", "zone": ZONE_MIND, "edit_class": CLASS_C,
             "title": f"Projection history ({len(self.wraps)} wraps)"}
        )
        return panels

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": {
                "episodic_db": str(self.paths.episodic_db),
                "continuity_md": str(self.paths.continuity_md),
                "crystal_json": str(self.paths.crystal_json),
                "spores_json": str(self.paths.spores_json),
            },
            "scope": self.scope,
            "entity_name": self.entity_name,
            "brand_wordmark": self.brand_wordmark,
            "brand_model": self.brand_model,
            "health": self.health.to_dict() if self.health else None,
            "graph": self.graph.to_dict() if self.graph else None,
            "crystal_index": [c.to_dict() for c in self.crystal_index],
            "open_spores": [s.to_dict() for s in self.open_spores],
            "episodes": [e.to_dict() for e in self.episodes],
            "sections": [s.to_dict() for s in self.sections],
            "config_docs": [d.to_dict() for d in self.config_docs],
            "wraps": [w.to_dict() for w in self.wraps],
            "recent_edits": self.recent_edits,
            "layout": self.layout(),
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _one_clause(explanation: str, limit: int = 160) -> str:
    """The short menu form of a crystallized pattern's explanation: the lead
    clause up to an em-dash break, else the first sentence, truncated."""
    text = " ".join(explanation.split())
    for sep in (" — ", " -- "):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    else:
        # no em-dash: first sentence
        for stop in (". ", "; "):
            if stop in text:
                text = text.split(stop, 1)[0]
                break
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit < 1:
        return ""
    return text[: limit - 1].rstrip() + "…"


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """All ``## `` sections of a markdown doc, in document order, as
    ``(heading, body)``. Used for both the neocortex (filtered) and the operator
    seed file (every section becomes its own config panel)."""
    out: list[tuple[str, str]] = []
    current: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current is not None:
                out.append((current, "\n".join(buf).strip()))
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out.append((current, "\n".join(buf).strip()))
    return out


def _parse_sections(markdown: str, wanted: tuple[str, ...]) -> list[Section]:
    """Split a continuity file on ``## `` headings and return the wanted ones in
    requested order. Tolerant of the FLOW_SCHEMA shape (## State / ## Active
    Threads / ## Patterns / …) without depending on a specific schema. ``State``
    is tagged Class A (free-text, directly editable in Slice 2); every other
    section is Class C (the consolidate owns it)."""
    blocks = dict(_split_sections(markdown))
    out: list[Section] = []
    for name in wanted:
        if name in blocks:
            out.append(Section(heading=name, body=blocks[name],
                               edit_class=_section_edit_class(name)))
    return out


def _h1_name_suffix(markdown: str) -> str | None:
    """The operator-set entity name baked into a seed file's H1 at install
    (``# Who You Are — <name>`` / ``# Continuity — <name>``). Returns the suffix
    after the dash, or None if the H1 carries no ``— <name>`` form. The seed
    "names nothing" by design, so this is the operator's post-formation name —
    a real first-class name field is a Slice-2 concern (§9 of the scope)."""
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("# "):
            head = s[2:].strip()
            for sep in (" — ", " -- ", " - "):
                if sep in head:
                    name = head.split(sep, 1)[1].strip()
                    return name or None
            return None  # an H1 without a "— name" suffix carries no name
    return None


# The operator-set config lives in one small JSON file under the install's
# ``.levain/`` dir (alongside the anneal stores) — the first-class home for Class-A
# settings (entity name today; future operator toggles). Slice-2 §9: the name is the
# operator's, post-formation and sovereign, so it is a real config field, NOT the
# seed (which "names nothing"). The legacy H1-suffix read stays as a FALLBACK so an
# install that named its entity via the old H1 form still surfaces it.
LEVAIN_CONFIG_REL = (".levain", "config.json")

# The neocortex continuity file's path RELATIVE to a Levain install root — the
# convention ``SubstrateSource.local`` lays down (anneal store at
# ``<root>/.levain/memory.db`` → continuity at ``<root>/.levain/memory.continuity.md``).
# The Slice-2b State write derives its target from this same constant (NOT from a
# request-supplied path), so the write target and the layout-emitted section ``source``
# can never disagree, and a ``state`` edit is structurally confined to this one file.
LEVAIN_CONTINUITY_REL = (".levain", "memory.continuity.md")


def _read_levain_config(install_root: Path) -> dict[str, Any]:
    """Fail-soft read of ``<install>/.levain/config.json`` → a dict (``{}`` if the
    file is absent, unreadable, non-UTF8, or not a JSON object). Never raises: a
    bad config must not blank the config tier (same fail-soft promise as the seed
    files)."""
    p = install_root.joinpath(*LEVAIN_CONFIG_REL)
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# The constitution files an entity carries (read-only view), with display names.
# The seed ``continuity.md`` is intentionally NOT here: it is the near-empty SCHEMA
# template (just the six section headings), and the operator's LIVE neocortex
# already renders in the Mind zone — showing the empty schema as "constitution"
# would only confuse. (Scope §4 lists it; this is a deliberate deviation.)
_CONSTITUTION = (
    ("partnership.md", "Constitution · how we work"),
    ("memory.md", "Constitution · your memory"),
    ("spore_instructions.md", "Constitution · open loops"),
)

# Bound the one operator-unbounded config tier: every other tier is capped
# (max_spores/max_episodes/max_wraps/max_graph_nodes), but world.md splits into
# one panel per ``##`` section, so a pathological world.md could emit arbitrarily
# many. Cap the world-section split (the shipped template has 11; 40 is generous
# headroom) so the Identity zone can't become an unbounded document dump.
_MAX_WORLD_SECTIONS = 40


def _read_config_docs(install_root: Path) -> list[ConfigDoc]:
    """Read the seed/config surface from a Levain install root. Each file is
    independently fail-soft — a missing file is simply skipped (an entity may not
    carry every optional doc); only a fault that makes the whole tier unreadable
    propagates to the caller's error handler. Returns Identity-zone docs in
    render order: entity self → operator (per section) → thinking style →
    constitution."""
    docs: list[ConfigDoc] = []
    seed = install_root / "seed"
    activation = install_root / "activation"

    def _read(p: Path) -> str | None:
        try:
            return p.read_text(encoding="utf-8") if p.is_file() else None
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError: a single non-UTF8 seed file is
            # SKIPPED exactly like a missing one — one corrupt file must never blank
            # the whole config tier (the per-file fail-soft promise above).
            return None

    # entity self — Class C-view (read-only): the entity's own birth self-statement,
    # not an operator input. The operator governs inputs, never overwrites the
    # entity's cognition (scope §1); influence on the felt self is the future
    # correction-as-input channel (§9), not a rewrite. (Sharpens §4, which had
    # drafted origin.md as Class A alongside the operator's world.md.)
    origin = _read(seed / "origin.md")
    if origin:
        docs.append(ConfigDoc(
            key="origin", title="Origin · who the entity is", body=origin,
            edit_class=CLASS_C, zone=ZONE_IDENTITY, source="seed/origin.md",
        ))

    # operator — one panel per world.md section (Identity / How They Think / …),
    # bounded so an over-long world.md can't flood the Identity zone.
    world = _read(seed / "world.md")
    if world:
        for heading, section_body in _split_sections(world)[:_MAX_WORLD_SECTIONS]:
            if not section_body:
                continue
            slug = heading.lower().replace(" ", "-").replace("&", "and")
            docs.append(ConfigDoc(
                key=f"world:{slug}", title=f"Operator · {heading}", body=section_body,
                edit_class=CLASS_A, zone=ZONE_IDENTITY, source="seed/world.md",
                heading=heading,
            ))

    # thinking style
    for fname, title in (("posture.md", "Posture · how it thinks"),
                         ("recency_directives.md", "Recency directives")):
        body = _read(activation / fname)
        if body:
            docs.append(ConfigDoc(
                key=fname.replace(".md", ""), title=title, body=body,
                edit_class=CLASS_A, zone=ZONE_IDENTITY,
                source=f"activation/{fname}",
            ))

    # constitution (read-only view)
    for fname, title in _CONSTITUTION:
        body = _read(seed / fname)
        if body:
            docs.append(ConfigDoc(
                key=fname.replace(".md", ""), title=title, body=body,
                edit_class=CLASS_C, zone=ZONE_IDENTITY, source=f"seed/{fname}",
            ))

    return docs


# ``get_associations`` binds the id list TWICE (``episode_a IN (...) OR
# episode_b IN (...)``) → 2N+2 bound variables; chunk well under the 999-var
# floor of libsqlite < 3.32 so the graph doesn't go dark on a mature store.
_ASSOC_ID_CHUNK = 400


def _build_graph(
    store: Any, *, min_strength: float, max_nodes: int, max_edges: int = 2000
) -> AssociationGraph:
    """Assemble the Hebbian association graph from a (read-only) store.

    Chunks the corpus id fan-in (a whole-corpus single query overflows SQLite's
    bound-variable cap), BOUNDS materialization at ``max_edges`` (a dense store
    could otherwise pull millions of pair objects into memory), and guarantees
    every emitted edge has BOTH endpoints present as nodes — no dangling edges,
    even when the recall window, the node cap, or the edge cap drops an endpoint.
    ``truncated`` is set honestly whenever any cap (the per-chunk strength-ordered
    limit or the total edge budget) may have dropped a real edge.
    """
    result = store.recall(limit=100000)
    episodes = {ep.id: ep for ep in result.episodes}
    all_ids = list(episodes)

    seen: set[frozenset[str]] = set()
    pairs = []
    # honest from the start: if the recall window itself dropped episodes (a store
    # with >100k episodes), edges to those nodes can't be seen → already truncated.
    # [codex L3 LOW]
    truncated = result.total_matching > len(result.episodes)
    for i in range(0, len(all_ids), _ASSOC_ID_CHUNK):
        chunk = all_ids[i : i + _ASSOC_ID_CHUNK]
        got = store.get_associations(chunk, min_strength=min_strength, limit=max_edges)
        if len(got) >= max_edges:
            # chunk hit anneal's per-query (ORDER BY strength) cap → weaker valid
            # edges in this chunk may have been dropped
            truncated = True
        for p in got:
            # Drop any pair whose endpoint fell outside the recall window (else
            # the edge would dangle). Dedup order-independently — OR-semantics +
            # chunking can return a cross-chunk pair from either side.
            if p.episode_a not in episodes or p.episode_b not in episodes:
                continue
            key = frozenset((p.episode_a, p.episode_b))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(p)
            if len(pairs) >= max_edges:
                truncated = True
                break
        if len(pairs) >= max_edges:
            break

    connected: set[str] = set()
    for p in pairs:
        connected.add(p.episode_a)
        connected.add(p.episode_b)

    if len(connected) > max_nodes:
        # keep the highest-degree nodes; cap the render
        degree: dict[str, int] = {}
        for p in pairs:
            degree[p.episode_a] = degree.get(p.episode_a, 0) + 1
            degree[p.episode_b] = degree.get(p.episode_b, 0) + 1
        connected = set(
            sorted(connected, key=lambda i: degree.get(i, 0), reverse=True)[:max_nodes]
        )
        truncated = True

    nodes = [
        GraphNode(id=ep.id, type=ep.type.value, label=_truncate(ep.content, 60))
        for eid, ep in episodes.items()
        if eid in connected
    ]
    edges = [
        GraphEdge(
            source=p.episode_a,
            target=p.episode_b,
            strength=p.strength,
            co_citations=p.co_citations,
            affective_tag=p.affective_tag,
            # coerce defensively across the API boundary — a legacy/neutral row
            # could carry None despite the type contract saying 0.0
            affective_intensity=float(p.affective_intensity or 0.0),
        )
        for p in pairs
        if p.episode_a in connected and p.episode_b in connected
    ]
    return AssociationGraph(nodes=nodes, edges=edges, truncated=truncated)


def _episode_tags(ep: Any) -> list[str]:
    """Tags ride in an episode's ``metadata`` dict (the episodic writer stores
    ``--tags`` there). Defensive: any non-list / absent value → empty."""
    meta = getattr(ep, "metadata", None)
    if isinstance(meta, dict):
        tags = meta.get("tags")
        if isinstance(tags, list):
            return [str(t) for t in tags]
    return []


# The six neocortex sections of the FLOW_SCHEMA, in canonical order. Slice 1
# showed only the first two; 1.5 shows them all.
_ALL_SECTIONS = ("State", "Active Threads", "Patterns", "Decisions", "Context", "Understanding")

# The sections that ride the MODEL-visible render_summary (the MCP-App content
# half) — kept to the two headlines so the all-six expansion never bloats model
# context. The full six always ride structuredContent for the UI.
_SUMMARY_SECTIONS = frozenset({"State", "Active Threads"})


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def build_substrate_view(
    paths: AnnealPaths,
    *,
    today: date | None = None,
    install_root: Path | None = None,
    ledger_root: Path | None = None,
    scope: str = "personal",
    graph_min_strength: float = 0.0,
    max_graph_nodes: int = 300,
    max_graph_edges: int = 2000,
    max_spores: int = 50,
    max_episodes: int = 50,
    max_wraps: int = 50,
    sections: tuple[str, ...] = _ALL_SECTIONS,
) -> SubstrateView:
    """Assemble the full read-only substrate view from the operator's stores.

    PURE READ — the episodic Store is opened ``read_only=True`` and a missing
    store is REPORTED, never fabricated (no db/dir creation, no wrap contention).
    Each tier is independently fail-soft on DATA/IO faults (a corrupt crystal
    file, an unreadable continuity, a missing ``seed/`` dir) → recorded to
    ``view.errors`` and rendered visibly empty. Programming-error classes are NOT
    caught — they surface loud.

    ``install_root`` (when given) locates the seed/config surface — the
    operator/origin/posture/constitution docs that live at the install root, not
    next to the db. Without it (a bare store with no install context) the config
    tier is simply absent (no error — there is nothing to read there)."""
    view = SubstrateView(paths=paths, scope=scope)

    # Data/IO fault classes we degrade on; programming bugs propagate (loud).
    # anneal wraps store faults in AnnealMemoryError, imported lazily so this
    # module stays importable without anneal.
    try:
        from anneal_memory import AnnealMemoryError

        data_faults: tuple[type[BaseException], ...] = (
            AnnealMemoryError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        )
    except Exception:  # noqa: BLE001 — anneal absent; the store tier reports it
        data_faults = (OSError, ValueError, json.JSONDecodeError)
    store_faults: tuple[type[BaseException], ...] = (*data_faults, ImportError)

    # --- health + graph + episodes + wraps (episodic Store, READ-ONLY) ------
    try:
        if not paths.episodic_db.exists():
            raise FileNotFoundError(f"no anneal store at {paths.episodic_db}")
        from anneal_memory import Store

        with Store(paths.episodic_db, read_only=True) as store:
            # wrap history — fetched ONCE, used by both the timeline AND health's
            # totals, in its OWN try. It does not read the continuity file, so a
            # non-UTF8 continuity (which anneal's status() reads + faults on) must
            # not blank the timeline as collateral of a health failure.
            wraps: list[WrapRow] = []
            try:
                # Coerce raw wrap rows to typed WrapRows AT THE BOUNDARY. SQLite's
                # loose INTEGER affinity can return TEXT/NULL in counter columns; an
                # un-coerced 'bad'/None would escape this tier as a TypeError (NOT a
                # data_fault) — through either the sort key OR health's sum() below —
                # and blank the WHOLE view. `int(x or 0)` maps NULL→0 and turns
                # malformed text into a ValueError (∈ data_faults) so the wraps tier
                # degrades VISIBLY in isolation; wrapped_at is str-coerced so the sort
                # never compares mixed types. [codex L3 HIGH] (supersedes the kimi-L3
                # `wrapped_at or ""` guard, which covered only the null-sort sub-case.)
                def _int(x: Any) -> int:
                    return int(x or 0)

                wraps = [
                    WrapRow(
                        wrapped_at=str(w.wrapped_at) if w.wrapped_at is not None else "",
                        episodes_compressed=_int(w.episodes_compressed),
                        continuity_chars=_int(w.continuity_chars),
                        graduations_validated=_int(w.graduations_validated),
                        graduations_demoted=_int(w.graduations_demoted),
                        associations_formed=_int(w.associations_formed),
                        associations_strengthened=_int(w.associations_strengthened),
                        associations_decayed=_int(w.associations_decayed),
                    )
                    for w in store.get_wrap_history()
                ]
                view.wraps = sorted(
                    wraps, key=lambda w: w.wrapped_at or "", reverse=True
                )[:max_wraps]
            except data_faults as exc:
                view.errors["wraps"] = f"{type(exc).__name__}: {exc}"

            # health — anneal's status() USED to read the continuity file and fault on
            # a non-UTF8 one; AM-STATUS-HARDEN (anneal 0.9.0) hardened that read, so
            # status() now returns valid health with continuity_chars=None instead of
            # raising. We still read the continuity size OURSELVES below (its own
            # guard) — so the health card survives a corrupt continuity and only that
            # one field degrades. wraps above + graph/episodes below stay independent
            # regardless. (errors["health"] now fires only on a genuine status() fault.)
            try:
                status = store.status()
                a = status.association_stats
                # NB the links_*/graduations_* totals below sum the `wraps` list
                # fetched in the (independent) wraps tier above; if that tier faulted
                # (errors["wraps"]), `wraps` is [] and these read 0 — do not treat a 0
                # here as authoritative when "wraps" is in errors. [complement L3 LOW-2]
                # read continuity size from the (overridable) AnnealPaths location —
                # consistent with how sections are read — not status()'s stem-derived
                # path, so the two never disagree.
                try:
                    cont_chars = (
                        len(paths.continuity_md.read_text(encoding="utf-8"))
                        if paths.continuity_md.exists()
                        else None
                    )
                except (OSError, ValueError):
                    cont_chars = None  # ValueError covers UnicodeDecodeError
                view.health = Health(
                    write_path_live=bool(a and a.total_links > 0),
                    total_links=a.total_links if a else 0,
                    avg_strength=a.avg_strength if a else 0.0,
                    max_strength=a.max_strength if a else 0.0,
                    density=a.density if a else 0.0,
                    local_density=a.local_density if a else 0.0,
                    links_formed_total=sum(w.associations_formed for w in wraps),
                    links_strengthened_total=sum(w.associations_strengthened for w in wraps),
                    links_decayed_total=sum(w.associations_decayed for w in wraps),
                    graduations_validated_total=sum(w.graduations_validated for w in wraps),
                    graduations_demoted_total=sum(w.graduations_demoted for w in wraps),
                    total_episodes=status.total_episodes,
                    episodes_since_wrap=status.episodes_since_wrap,
                    episodes_by_type=dict(status.episodes_by_type),
                    tombstones=status.tombstone_count,
                    continuity_chars=cont_chars,
                    total_wraps=status.total_wraps,
                    last_wrap_at=status.last_wrap_at,
                    wrap_in_progress=status.wrap_in_progress,
                )
            except data_faults as exc:
                view.errors["health"] = f"{type(exc).__name__}: {exc}"

            # graph
            try:
                view.graph = _build_graph(
                    store,
                    min_strength=graph_min_strength,
                    max_nodes=max_graph_nodes,
                    max_edges=max_graph_edges,
                )
            except data_faults as exc:
                view.errors["graph"] = f"{type(exc).__name__}: {exc}"

            # recent episodes (the raw input layer; newest first via recall)
            try:
                for ep in store.recall(limit=max_episodes).episodes:
                    view.episodes.append(
                        EpisodeRow(
                            id=ep.id,
                            timestamp=ep.timestamp,
                            type=ep.type.value,
                            source=ep.source,
                            content=_truncate(ep.content, 280),
                            tags=_episode_tags(ep),
                        )
                    )
            except data_faults as exc:
                view.errors["episodes"] = f"{type(exc).__name__}: {exc}"
    except store_faults as exc:  # Store unopenable / missing / anneal import fault
        view.errors["store"] = f"{type(exc).__name__}: {exc}"

    # --- crystallized-pattern index (defensive per-row: one bad row is
    #     skipped, never blanks the whole tier — as drift-tolerant as the
    #     library's own active() read) -------------------------------------
    try:
        from anneal_memory.crystal import CrystalStore

        if paths.crystal_json.exists():
            for c in CrystalStore(paths.crystal_json).active():
                try:
                    view.crystal_index.append(
                        CrystalEntry(
                            name=str(c.get("name") or "(unnamed)"),
                            level=int(c.get("level", 0) or 0),
                            one_clause=_one_clause(str(c.get("explanation", ""))),
                            permanence=str(c.get("permanence", "")),
                            activation_mode=str(c.get("activation_mode", "")),
                            last_activated_on=str(c.get("last_activated_on", "")),
                            tags=list(c.get("tags") or []),
                        )
                    )
                except (ValueError, TypeError):
                    continue  # one malformed row skipped; the tier survives
    except store_faults as exc:
        view.errors["crystal_index"] = f"{type(exc).__name__}: {exc}"

    # --- open spores (prospective loops; defensive per-row) ----------------
    try:
        from anneal_memory.spores import (
            ASCEND_BY_TYPE,
            DESCEND_BY_TYPE,
            SporeStore,
        )

        if paths.spores_json.exists():
            items = SporeStore(paths.spores_json).list_open(today=today)[:max_spores]
            for s in items:
                try:
                    stype = str(s.get("type", ""))
                    view.open_spores.append(
                        OpenSpore(
                            id=str(s.get("id", "")),
                            type=stype,
                            tier=str(s.get("tier", "")),
                            salience=int(s.get("salience", 0) or 0),
                            domain=str(s.get("domain", "")),
                            text=_truncate(str(s.get("text", "")), 200),
                            seen=str(s.get("seen", "")),
                            next=s.get("next"),
                            pointer=s.get("pointer"),
                            descend_kinds=sorted(DESCEND_BY_TYPE.get(stype, frozenset())),
                            ascend_kinds=sorted(ASCEND_BY_TYPE.get(stype, frozenset())),
                        )
                    )
                except (ValueError, TypeError):
                    continue  # one malformed row skipped; the tier survives
    except store_faults as exc:
        view.errors["open_spores"] = f"{type(exc).__name__}: {exc}"

    # --- continuity narrative sections (all six neocortex sections) ---------
    try:
        if paths.continuity_md.exists():
            md = paths.continuity_md.read_text(encoding="utf-8")
            view.sections = _parse_sections(md, sections)
    except (OSError, ValueError) as exc:
        view.errors["sections"] = f"{type(exc).__name__}: {exc}"

    # --- seed/config surface (install root — operator/origin/posture/constitution)
    #     Only when an install root is known; a bare store has no config context.
    if install_root is not None:
        try:
            if not install_root.exists():
                raise FileNotFoundError(f"no install root at {install_root}")
            view.config_docs = _read_config_docs(install_root)
            # entity name — prefer the first-class .levain/config.json (Slice-2 §9:
            # the name is operator-set + sovereign), fall back to the legacy
            # origin.md H1 suffix. The config read is fail-soft (never raises), so it
            # can't turn a populated config_docs into a lying errors["config"]. The
            # origin body comes from the ALREADY-loaded config_docs, not a 2nd
            # filesystem read that could race a delete/rename and falsely error.
            # [complement L3 MEDIUM-1]
            config = _read_levain_config(install_root)
            name = config.get("entity_name")
            if not isinstance(name, str) or not name.strip():
                origin_doc = next(
                    (d for d in view.config_docs if d.key == "origin"), None
                )
                name = _h1_name_suffix(origin_doc.body) if origin_doc else None
            if name:
                view.entity_name = name.strip()
            # the operator's Class-A edit log (Slice 2a). recent_edits is fail-soft
            # (returns [] on any read fault) so it never propagates an error here.
            from levain.writes import recent_edits  # lazy: writes↔dashboard cycle

            # The edit-log ledger: prefer the scope's explicit ledger_root when given
            # (so the READ surface matches where WRITES land — no read/write split), else
            # the install convention <root>/.levain (byte-identical to the old
            # recent_edits(install_root) on the same .levain/edits.jsonl). [L1 MED]
            view.recent_edits = recent_edits(
                ledger_root if ledger_root is not None else install_root / ".levain"
            )
        except (OSError, ValueError) as exc:
            view.errors["config"] = f"{type(exc).__name__}: {exc}"
    elif ledger_root is not None:
        # A non-install substrate (flow's own store) has no seed/config surface but DOES
        # carry an explicit ledger (its WriteScope.ledger_root) — surface its edit log so
        # the cockpit shows the audit trail + undo affordances. recent_edits is fail-soft.
        from levain.writes import recent_edits  # lazy: writes↔dashboard cycle

        view.recent_edits = recent_edits(ledger_root)

    return view


# ---------------------------------------------------------------------------
# Presentation + CLI surface — ``levain dashboard [--json]``.
#
# The smallest real "escapes the session" affordance: an operator runs this
# OUTSIDE any Claude Code / Codex session and sees their substrate. Read-only,
# so it acts on nothing — the human-is-fan-in invariant holds trivially. The
# JSON form is the shape the web-app + control-pane serve; the text form is the
# terminal glance.
# ---------------------------------------------------------------------------

def _resolve_source(path: Path) -> SubstrateSource:
    """Resolve a Levain install directory into a ``SubstrateSource`` — the anneal
    store (``<install>/.levain/memory.db``) plus the install root for the
    seed/config surface."""
    return SubstrateSource.local(path)


def _resolve_store(path: Path) -> AnnealPaths:
    """Back-compat: the anneal paths alone (no install-root config context). Kept
    for callers that only need the store tiers; new code uses ``_resolve_source``."""
    return _resolve_source(path).anneal


def render_text(view: SubstrateView) -> str:
    """A terminal glance: the operator's memory health, association graph,
    crystallized wisdom, open loops, recent episodes, the neocortex sections, and
    the seed/config surface — from outside a session. Degraded tiers are named at
    the bottom, never silently dropped."""
    title = view.entity_name or view.paths.episodic_db.stem
    masthead = view.brand_model or "Levain substrate"
    out: list[str] = [f"{masthead} — {title}", f"  store: {view.paths.episodic_db}", ""]

    h = view.health
    if h is not None:
        out.append("Health")
        if h.write_path_live:
            out.append(
                f"  Write-path:   LIVE — {h.total_links} links "
                f"(avg {h.avg_strength:.2f}, max {h.max_strength:.2f})"
            )
        else:
            out.append(
                "  Write-path:   DARK — no associations (a graduated wrap should "
                "form links; 0 = the silent-0-links death)"
            )
        out.append(
            f"  Graduations:  {h.graduations_validated_total} validated / "
            f"{h.graduations_demoted_total} demoted"
        )
        by_type = ", ".join(f"{k} {v}" for k, v in sorted(h.episodes_by_type.items()))
        out.append(
            f"  Episodes:     {h.total_episodes:,} ({h.episodes_since_wrap} since last wrap)"
            + (f" · {by_type}" if by_type else "")
        )
        chars = (
            f"{h.continuity_chars:,} chars"
            if h.continuity_chars is not None
            else "not yet created"
        )
        last = h.last_wrap_at.split("T")[0] if h.last_wrap_at else "never"
        out.append(f"  Continuity:   {chars} · {h.total_wraps} wraps · last {last}")
        if h.tombstones:
            out.append(f"  Tombstones:   {h.tombstones}")
        if h.wrap_in_progress:
            out.append("  ⚠ wrap in progress — snapshot may be momentarily inconsistent")
        out.append("")

    if view.graph is not None:
        g = view.graph
        suffix = " (capped for display)" if g.truncated else ""
        out.append(f"Association graph: {len(g.nodes)} nodes, {len(g.edges)} edges{suffix}")
        out.append("")

    if view.crystal_index:
        out.append(f"Crystallized patterns ({len(view.crystal_index)})")
        for c in view.crystal_index:
            out.append(f"  - {c.name} ({c.level}x) — {c.one_clause}")
        out.append("")

    if view.open_spores:
        out.append(f"Open loops ({len(view.open_spores)})")
        for s in view.open_spores:
            nxt = f" → next {s.next}" if s.next else ""
            out.append(f"  - [{s.tier}] {s.text}{nxt}")
        out.append("")

    if view.episodes:
        out.append(f"Recent episodes ({len(view.episodes)})")
        for e in view.episodes[:12]:
            stamp = e.timestamp.split("T")[0] if e.timestamp else ""
            out.append(f"  - [{e.type}] {e.content}  ({stamp})")
        out.append("")

    for sec in view.sections:
        out.append(f"{sec.heading}  [{sec.edit_class}]")
        for line in sec.body.splitlines():
            out.append(f"  {line}")
        out.append("")

    if view.config_docs:
        out.append(f"Seed / config ({len(view.config_docs)})")
        for d in view.config_docs:
            out.append(f"  - {d.title}  [{d.edit_class}] ({d.source})")
        out.append("")

    if view.wraps:
        out.append(f"Projection history ({len(view.wraps)} wraps)")
        for w in view.wraps[:10]:
            stamp = w.wrapped_at.split("T")[0] if w.wrapped_at else ""
            out.append(
                f"  - {stamp}: {w.graduations_validated}↑/{w.graduations_demoted}↓ grad · "
                f"+{w.associations_formed} links · {w.continuity_chars:,} chars"
            )
        out.append("")

    if view.errors:
        out.append("⚠ Degraded tiers (rendered empty above):")
        for tier, msg in view.errors.items():
            out.append(f"  - {tier}: {msg}")

    return "\n".join(out).rstrip() + "\n"


def render_summary(view: SubstrateView) -> str:
    """The MODEL-visible summary — the ``content`` half of the MCP-App split.

    The full ``SubstrateView`` rides in ``structuredContent`` (the UI's render
    prop, hidden from the model). This is the compact, reason-over-able digest
    the model actually sees: write-path health, the load-bearing counts, and the
    State / Active-Threads headlines — NOT the whole graph + spore + crystal dump
    (that would bloat model context, which is the exact inversion the
    content/structuredContent split exists to prevent). Also the text-only
    fallback for hosts without MCP-Apps support."""
    title = view.entity_name or view.paths.episodic_db.stem
    masthead = view.brand_model or "Levain substrate"
    lines: list[str] = [f"{masthead} — {title}"]

    h = view.health
    if h is not None:
        if h.write_path_live:
            lines.append(
                f"Health: write-path LIVE — {h.total_links} Hebbian links "
                f"(avg {h.avg_strength:.2f})"
            )
        else:
            lines.append(
                "Health: write-path DARK — 0 associations "
                "(a graduated wrap should form links; 0 = the silent-0-links death)"
            )
        last = h.last_wrap_at.split("T")[0] if h.last_wrap_at else "never"
        lines.append(
            f"  {h.total_episodes:,} episodes ({h.episodes_since_wrap} since wrap) · "
            f"{h.graduations_validated_total} graduations validated / "
            f"{h.graduations_demoted_total} demoted · "
            f"{h.total_wraps} wraps (last {last})"
        )
        if h.wrap_in_progress:
            lines.append("  ⚠ wrap in progress — snapshot may be momentarily inconsistent")
    elif "store" in view.errors:
        lines.append(f"Health: unavailable — {view.errors['store']}")

    lines.append(f"Crystallized patterns: {len(view.crystal_index)}")
    lines.append(f"Open loops: {len(view.open_spores)}")
    lines.append(f"Recent episodes: {len(view.episodes)}")
    if view.config_docs:
        lines.append(f"Seed/config docs: {len(view.config_docs)}")

    # Only the two headline sections ride the MODEL-visible summary — the full six
    # ride in structuredContent for the UI. Folding all six here would bloat model
    # context, the exact inversion the content/structuredContent split exists to
    # prevent (the 1.5 expansion to all-six sections must NOT leak into this digest).
    for sec in view.sections:
        if sec.heading not in _SUMMARY_SECTIONS:
            continue
        head = next((ln.strip() for ln in sec.body.splitlines() if ln.strip()), "")
        if head:
            lines.append(f"{sec.heading}: {_truncate(head, 200)}")

    if view.errors:
        lines.append("Degraded tiers (rendered empty in the app): " + ", ".join(view.errors))

    return "\n".join(lines)


def run_dashboard(path: Path, as_json: bool = False) -> int:
    """``levain dashboard`` entry point. Nonzero only if the store itself is
    unreachable — a degraded sub-tier (e.g. a corrupt crystal file) renders
    visibly and is not a dashboard failure."""
    source = _resolve_source(path)
    if not source.anneal.episodic_db.exists():
        print(
            f"No anneal store at {source.anneal.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1
    view = source.build()
    if as_json:
        print(json.dumps(view.to_dict(), indent=2))
    else:
        print(render_text(view), end="")
    return 1 if "store" in view.errors else 0
