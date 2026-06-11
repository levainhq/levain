"""levain.dashboard — Levain v2, Slice 1: the read-only substrate dashboard.

The v2 spine is *the substrate escapes the session*. Today a Levain partnership
entity only exists INSIDE a live Claude Code / Codex session — to see your memory,
your open loops, the health of your Hebbian graph, you have to be mid-conversation.
Slice 1 is the first move out: assemble the entity's whole anneal substrate into a
single ``SubstrateView`` that can be rendered from outside (an MCP-App inside the
host, a CLI, a future control-pane).

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
no continuity yet — none of these blank the whole board. Every degradation lands in
``SubstrateView.errors`` so the surface can SHOW that a tier is unavailable rather
than silently rendering it empty (a silent-empty health panel is exactly the
``invisible_infrastructure_failure`` the dashboard exists to make visible).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# anneal-memory is Levain's layer-1 dependency (pinned >=0.8 in pyproject); these
# imports are in-process library calls, not a subprocess. Import lazily inside the
# builder so importing this module never hard-fails if anneal is mis-installed —
# the failure surfaces as a SubstrateView error, consistent with every other tier.

__all__ = [
    "AnnealPaths",
    "Health",
    "GraphNode",
    "GraphEdge",
    "AssociationGraph",
    "CrystalEntry",
    "OpenSpore",
    "Section",
    "SubstrateView",
    "build_substrate_view",
    "render_text",
    "render_summary",
    "run_dashboard",
]


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


# ---------------------------------------------------------------------------
# Result shapes — plain dataclasses, JSON-serializable via to_dict(). These are
# the dashboard's vocabulary; the MCP-App server serializes them, the host
# renders them. Kept faithful to the underlying anneal reads (no speculative
# fields) — sourdough_scoping, not a designed-ahead schema.
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

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Section:
    """A continuity narrative section (State / Active Threads), rendered as-is.
    The dashboard does not re-derive these — it surfaces the entity's own words."""

    heading: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SubstrateView:
    """The whole substrate, assembled. Any tier may be ``None`` with a matching
    entry in ``errors`` — degrade visibly, never silently."""

    paths: AnnealPaths
    health: Health | None = None
    graph: AssociationGraph | None = None
    crystal_index: list[CrystalEntry] = field(default_factory=list)
    open_spores: list[OpenSpore] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": {
                "episodic_db": str(self.paths.episodic_db),
                "continuity_md": str(self.paths.continuity_md),
                "crystal_json": str(self.paths.crystal_json),
                "spores_json": str(self.paths.spores_json),
            },
            "health": self.health.to_dict() if self.health else None,
            "graph": self.graph.to_dict() if self.graph else None,
            "crystal_index": [c.to_dict() for c in self.crystal_index],
            "open_spores": [s.to_dict() for s in self.open_spores],
            "sections": [s.to_dict() for s in self.sections],
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


def _parse_sections(markdown: str, wanted: tuple[str, ...]) -> list[Section]:
    """Split a continuity file on ``## `` headings and return the wanted ones in
    requested order. Tolerant of the FLOW_SCHEMA shape (## State / ## Active
    Threads / ## Patterns / …) without depending on a specific schema."""
    blocks: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current is not None:
                blocks[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        blocks[current] = "\n".join(buf).strip()
    out: list[Section] = []
    for name in wanted:
        if name in blocks:
            out.append(Section(heading=name, body=blocks[name]))
    return out


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
    truncated = False
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


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def build_substrate_view(
    paths: AnnealPaths,
    *,
    today: date | None = None,
    graph_min_strength: float = 0.0,
    max_graph_nodes: int = 300,
    max_graph_edges: int = 2000,
    max_spores: int = 50,
    sections: tuple[str, ...] = ("State", "Active Threads"),
) -> SubstrateView:
    """Assemble the full read-only substrate view from the operator's stores.

    PURE READ — the episodic Store is opened ``read_only=True`` and a missing
    store is REPORTED, never fabricated (no db/dir creation, no wrap contention).
    Each tier is independently fail-soft on DATA/IO faults (a corrupt crystal
    file, an unreadable continuity) → recorded to ``view.errors`` and rendered
    visibly empty. Programming-error classes are NOT caught — they surface loud.
    """
    view = SubstrateView(paths=paths)

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

    # --- health + graph (episodic Store, READ-ONLY — never creates it) -----
    try:
        if not paths.episodic_db.exists():
            raise FileNotFoundError(f"no anneal store at {paths.episodic_db}")
        from anneal_memory import Store

        with Store(paths.episodic_db, read_only=True) as store:
            # health
            try:
                status = store.status()
                wraps = store.get_wrap_history()
                a = status.association_stats
                # read continuity size from the (overridable) AnnealPaths
                # location — consistent with how sections are read — rather than
                # status()'s stem-derived path, so the two never disagree.
                try:
                    cont_chars = (
                        len(paths.continuity_md.read_text(encoding="utf-8"))
                        if paths.continuity_md.exists()
                        else None
                    )
                except OSError:
                    cont_chars = None
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
        from anneal_memory.spores import SporeStore

        if paths.spores_json.exists():
            items = SporeStore(paths.spores_json).list_open(today=today)[:max_spores]
            for s in items:
                try:
                    view.open_spores.append(
                        OpenSpore(
                            id=str(s.get("id", "")),
                            type=str(s.get("type", "")),
                            tier=str(s.get("tier", "")),
                            salience=int(s.get("salience", 0) or 0),
                            domain=str(s.get("domain", "")),
                            text=_truncate(str(s.get("text", "")), 200),
                            seen=str(s.get("seen", "")),
                            next=s.get("next"),
                            pointer=s.get("pointer"),
                        )
                    )
                except (ValueError, TypeError):
                    continue  # one malformed row skipped; the tier survives
    except store_faults as exc:
        view.errors["open_spores"] = f"{type(exc).__name__}: {exc}"

    # --- continuity narrative sections (State / Active Threads) -------------
    try:
        if paths.continuity_md.exists():
            md = paths.continuity_md.read_text(encoding="utf-8")
            view.sections = _parse_sections(md, sections)
    except (OSError, ValueError) as exc:
        view.errors["sections"] = f"{type(exc).__name__}: {exc}"

    return view


# ---------------------------------------------------------------------------
# Presentation + CLI surface — ``levain dashboard [--json]``.
#
# The smallest real "escapes the session" affordance: an operator runs this
# OUTSIDE any Claude Code / Codex session and sees their substrate. Read-only,
# so it acts on nothing — the human-is-fan-in invariant holds trivially. The
# JSON form is the shape the future MCP-App control-pane serves; the text form
# is the terminal glance.
# ---------------------------------------------------------------------------

def _resolve_store(path: Path) -> AnnealPaths:
    """A Levain install keeps its anneal store at ``<install>/.levain/memory.db``
    (the convention ``doctor._match_store`` enforces). Derive the four sibling
    stores from there."""
    install = Path(str(path)).expanduser().resolve()
    return AnnealPaths.from_db(install / ".levain" / "memory.db")


def render_text(view: SubstrateView) -> str:
    """A terminal glance: the operator's memory health, association graph,
    crystallized wisdom, open loops, and State / Active Threads — from outside a
    session. Degraded tiers are named at the bottom, never silently dropped."""
    out: list[str] = ["Levain substrate", f"  store: {view.paths.episodic_db}", ""]

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

    for sec in view.sections:
        out.append(sec.heading)
        for line in sec.body.splitlines():
            out.append(f"  {line}")
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
    lines: list[str] = [f"Levain substrate — {view.paths.episodic_db.stem}"]

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

    for sec in view.sections:
        # the entity's own words — first non-empty line of each, as a headline
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
    paths = _resolve_store(path)
    if not paths.episodic_db.exists():
        print(
            f"No anneal store at {paths.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1
    view = build_substrate_view(paths)
    if as_json:
        print(json.dumps(view.to_dict(), indent=2))
    else:
        print(render_text(view), end="")
    return 1 if "store" in view.errors else 0
