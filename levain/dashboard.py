"""levain.dashboard — Levain v2: the read-only substrate view (Slices 1 + 1.5).

The v2 spine is *the substrate escapes the session*. Today a Levain partnership
entity only exists INSIDE a live Claude Code / Codex session — to see your memory,
your open loops, the health of your memory store, who the entity is and how it
thinks, you have to be mid-conversation. This module is the first move out:
assemble the entity's WHOLE substrate — the anneal stores AND the install's
seed/config surface — into a single ``SubstrateView`` that renders from outside
(the local sovereign web-app, a CLI, the parked in-host MCP-App).

Slice 1 served State + health + graph + crystals + spores. **Slice 1.5 shows
EVERYTHING, read-only:** every neocortex section, the seed/config surface
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

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping

from levain.spores import (
    BUCKET_KEEP,
    BUCKET_TRAY,
    LOOP_DISPOSITION,
    bucket_of,
    disposition_of,
)

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
    "State",
    "Jar",
    "SubstrateView",
    "build_substrate_view",
    "write_state",
    "StateWrite",
    "render_text",
    "render_summary",
    "run_dashboard",
    "run_state",
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
# (last-writer-wins; the embedder's own "quick update" discipline treats it as a direct
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
    # from `install_root` so a NON-install substrate (the embedder's own store — no `.levain/`)
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
    # WHERE the live operator-context (the state line) is read from. ``.local()`` defaults
    # it to the install's ``.levain/context.json``; a downstream control plane with an
    # N-of-1 source overrides it to wherever its own sensor app writes. None →
    # ``view.state`` is None (no live-context source).
    context_json: Path | None = None
    # A host-supplied transcript reader for ``GET /cockpit/session/<stem>.json``, for a host whose sessions are not
    # levain-hook records (its ids are not the install's 24-hex stems, or it has no install root). ``None`` → the
    # route reads the install's own session log, as a bare Levain install always has. Set, it wins over
    # ``install_root`` for that route. The contract a host implements:
    #   ``session_reader(stem: str, now: datetime) -> dict | None``
    # ``stem`` arrives already shape-checked by the route (``[A-Za-z0-9][A-Za-z0-9_.-]{0,127}``, never ``..``), still
    # behind the route's token gate. Return ``None`` for a session the host does not know (the route answers 404).
    # Otherwise return the install reader's body: ``agent``, ``workspace``, ``first_seen``, ``last_seen``, ``resume``,
    # ``events`` (a list of ``{"at", "kind", "text", "cut"}``, ``kind`` one of prompt|reply|tool), ``consent``,
    # ``skipped`` (and ``context`` when the host has one). ``events`` must be a list or the route treats the read as
    # failed (500), as it does an exception. Every field is display text, never markup.
    session_reader: Callable[[str, datetime], dict[str, Any] | None] | None = None

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
            context_json=root / ".levain" / "context.json",
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
            context_json=self.context_json,
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
    exist. ``status`` is the headline: ``live`` (the store reads and at least one wrap
    committed) or ``no-wraps`` (it reads, nothing committed yet; ``unreadable`` is a
    view with no Health at all, named by its consumers). The age of the last wrap is a
    fact the renderers display, never a gate. ``write_path_live`` is the DEPRECATED
    key, ``status == "live"``, kept one release so an older reader does not KeyError.
    ``total_links`` and the figures beside it describe the historical graph (anneal
    stopped forming links in 1.0.2); they are never health."""

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
    status: str | None = None  # "live" | "no-wraps"; None derives it from write_path_live (an older caller)
    # The substrate oracles (anneal-cut train step 2). Facts, never an activity counter: a healthy wrap can
    # graduate nothing and form nothing. None = could not be read / nothing to compare. Schema compatibility
    # has no field: anneal refuses to open a newer-schema store, which lands in ``errors["store"]`` (a view with
    # no Health at all), so a Health that exists is a store that opened.
    save_age_hours: float | None = None  # age of the last COMMITTED save (last_wrap_at), not of any episode
    projection_converged: bool | None = None  # sha256 of the continuity file == the last wrap's content_hash; None = unknown
    # audit_backlog: the durable audit_write_failures count (what the trail LOST; bounded, so 0 means none RECORDED,
    # never complete). levain opens the store read-only, where anneal never builds the audit trail, so only this
    # durable counter is readable. None = could not be read.
    audit_backlog: int | None = None
    save_at: str | None = None  # the stamp save_age_hours is counted from: a page keeps the age current from it, on its own clock
    projection_note: str | None = None  # why the projection reads diverged (a missing file vs an edit); None otherwise

    def __post_init__(self) -> None:
        # one truth, two spellings: a caller that built Health the old way (write_path_live only) gets the status it
        # implies, and the deprecated key always follows the status, so to_dict() never contradicts itself
        if self.status is None:
            self.status = "live" if self.write_path_live else "no-wraps"
        self.write_path_live = self.status == "live"

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
    """The historical co-citation graph. Nodes are episodes that have at least one
    association above threshold; edges carry strength + co-citation count + the
    affective tag old rows carry. anneal >= 1.0.2 forms no new links:
    ``formation_off_at_wrap`` is the wrap after which none were (None on a store that
    still forms them, or on an anneal that cannot say)."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False  # nodes capped for render
    formation_off_at_wrap: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "truncated": self.truncated,
            "formation_off_at_wrap": self.formation_off_at_wrap,
        }


def _last_wrap_row(db: Path) -> tuple[str | None, str | None, str | None] | None:
    """``(wrapped_at, content_hash, pair_id)`` of the last COMMITTED wraps row (anneal >= 1.0.0), read-only, in ONE
    read, so the save age and the expected hash can never come from two different instants. ``content_hash`` is the
    sha256 hex of the continuity text that wrap saved; ``pair_id`` names its tmp sidecar. Either may be NULL on a
    migrated old row. None when there is no wrap, a column is absent, or the db cannot be read: unknown."""
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT wrapped_at, content_hash, pair_id FROM wraps ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except (OSError, ValueError, sqlite3.Error):
        return None
    if not row:
        return None
    return tuple(v if isinstance(v, str) and v else None for v in row)  # type: ignore[return-value]


def _sha256_of(path: Path) -> tuple[str | None, bool]:
    """``(hex, missing)``: sha256 of the file's bytes (what anneal hashed is the text it wrote). ``(None, True)``
    when the file is absent; ``(None, False)`` when it exists but cannot be read (unknown, not missing)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest(), False
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False


def _sidecar_path(continuity: Path, pair_id: str) -> Path:
    """anneal v1.0.0 ``Store._continuity_tmp_path``: ``final.with_name(f"{final.stem}.{suffix}.md.tmp")`` with the
    wrap's ``<token12>-<uuid8>`` pair id as the suffix. Phase 1 writes it, the wraps row commits, Phase 3 renames
    it over the file, so the row can lead the file by exactly this sidecar. Built, never globbed: another wrap's
    leftover sidecar is not this wrap's."""
    return continuity.with_name(f"{continuity.stem}.{pair_id}.md.tmp")


# anneal v1.0.0 ``store.py`` ``_NEW_FORMAT_PAIR_ID_RE``: the only pair-id shape a wrap writes into a sidecar name.
_PAIR_ID_RE = re.compile(r"[0-9a-f]{12}-[0-9a-f]{8}")
_SETTLING_NOTE = "settling (wrap externalizing)"
_INCOMPLETE_NOTE = ("externalization incomplete: the last wrap's file was never moved into place "
                    "(anneal recovers it when the store is next opened for writing)")
_SETTLE_SECONDS = 600.0  # a sidecar older than this is not an externalization in flight


def _proven_sidecar_age(continuity: Path, pair_id: str, want: str, ref: datetime) -> float | None:
    """Seconds since the exact sidecar for ``pair_id`` was last written, when it exists AND hashes to ``want``;
    None when there is no such proof. An invalid pair_id (not anneal's shape) or an unreadable sidecar is "no
    settling proof", never a fault."""
    try:
        if not _PAIR_ID_RE.fullmatch(pair_id):
            return None
        side = _sidecar_path(continuity, pair_id)
        got, _ = _sha256_of(side)
        if got is None or got != want:
            return None
        return max(0.0, ref.timestamp() - side.stat().st_mtime)
    except (ValueError, OSError):
        return None


def health_oracles(
    status: Any, row: tuple[str | None, str | None, str | None] | None, continuity: Path | None, *,
    now: datetime | None = None,
    reread: Callable[[], tuple[str | None, str | None, str | None] | None] | None = None,
) -> dict[str, Any]:
    """The substrate oracles from facts the store already states (works on anneal 1.0.0; nothing here needs
    1.0.2). ``row`` is the latest wraps row, ``(wrapped_at, content_hash, pair_id)`` from ONE read: recency (the
    last COMMITTED save) and the expected hash both come from it (status() supplies the audit counter, and the age
    only when the row could not be read). Convergence is the sha256 of the continuity file against that
    content_hash. A mismatch is "settling" (None) only when PROVEN: the exact sidecar for that row's pair_id exists
    and hashes to the row's content_hash. Otherwise a missing file is False ("continuity file missing"), a
    mismatch is False ("differs ... edited since?"), and anything unreadable or NULL where it is needed (hash,
    pair_id) is None. The audit backlog is the durable audit_write_failures count, the one audit fact a read-only
    open can see; 0 means none recorded, not that the trail is complete. Each is None when it cannot be told."""
    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:  # a naive now compares in UTC, as build_substrate_view does
        ref = ref.replace(tzinfo=timezone.utc)
    converged: bool | None = None
    note: str | None = None
    # Race: the row and the file are two reads. Hash the file, then re-read the latest row; if a wrap committed in
    # between, redo once with the new row; a second change reads unknown, never a false DIVERGED.
    for attempt in (0, 1):
        wrapped_at, want, pair_id = row if row else (None, None, None)
        converged, note = None, None
        if row and want and continuity is not None:
            have, missing = _sha256_of(continuity)
            if reread is not None:
                row2 = reread()
                if row2 != row:
                    row = row2
                    if attempt == 1:
                        converged, note = None, "unknown (a wrap is committing)"
                        break
                    continue
            if have is not None and have == want:
                converged = True
            elif have is None and not missing:
                converged = None  # unreadable: unknown
            elif not pair_id:
                converged = None  # cannot prove settling without the pair id: unknown, never a guess
            else:
                side_age = _proven_sidecar_age(continuity, pair_id, want, ref)
                if side_age is not None:
                    if side_age > _SETTLE_SECONDS:
                        converged, note = False, _INCOMPLETE_NOTE
                    else:
                        converged, note = None, _SETTLING_NOTE
                elif missing:
                    converged, note = False, "continuity file missing"
                else:
                    converged, note = False, "continuity file differs from the last wrap (edited since?)"
        break
    wrapped_at = row[0] if row else None
    age: float | None = None
    raw = wrapped_at or getattr(status, "last_wrap_at", None)
    if raw:
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age = max(0.0, (ref - when).total_seconds() / 3600.0)
        except ValueError:
            age = None
    failures = getattr(status, "audit_write_failures", None)
    return {
        "save_age_hours": age,
        "save_at": str(raw) if age is not None else None,
        "projection_converged": converged,
        "projection_note": note,
        "audit_backlog": int(failures) if isinstance(failures, int) and not isinstance(failures, bool) else None,
    }


def health_oracle_line(h: "Health") -> str | None:
    """The one text line the renderers print for the oracles; None when none could be read."""
    bits: list[str] = []
    if h.save_age_hours is not None:
        bits.append("last save " + (f"{math.floor(h.save_age_hours + 0.5)}h ago" if h.save_age_hours >= 1 else "under 1h ago"))
    if h.projection_converged is not None:
        bits.append("projection converged" if h.projection_converged
                    else "PROJECTION DIVERGED (" + (h.projection_note or "continuity file differs from the last wrap (edited since?)") + ")")
    elif h.projection_note:
        bits.append("projection " + h.projection_note)
    if h.audit_backlog is not None:
        bits.append("no audit write failures recorded" if not h.audit_backlog
                    else f"AUDIT: {h.audit_backlog} audit writes failed")
    return " · ".join(bits) if bits else None


def graph_caption(g: "AssociationGraph") -> str:
    """The one line the text renderers print for the graph: its size, or ``not formed``
    for an empty one (a fresh store never grows a graph), then the formation note."""
    if not g.nodes and not g.edges:
        line = "not formed"
    else:
        line = f"{len(g.nodes)} nodes, {len(g.edges)} edges" + (" (capped for display)" if g.truncated else "")
    if g.formation_off_at_wrap is not None:
        line += f" · no new links since wrap {g.formation_off_at_wrap}"
    return line


def _formation_off_at_wrap(store: Any) -> int | None:
    """``Store.association_formation()['off_at_wrap']`` when the installed anneal has the
    accessor and says formation is off; None otherwise (anneal 1.0.1 has no accessor, so
    nothing is claimed). Never raises: a missing accessor, an odd shape or a failing read all mean no caption."""
    fn = getattr(store, "association_formation", None)
    if not callable(fn):
        return None
    try:
        got = fn()
    except Exception:  # noqa: BLE001 — the caption is cosmetic; a failing accessor must not cost the graph it captions
        return None
    if not isinstance(got, dict) or got.get("mode") != "disabled":
        return None
    off = got.get("off_at_wrap")
    return off if isinstance(off, int) and not isinstance(off, bool) else None


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
    # The operator-I/O disposition (control-plane Slice 3): ``loop`` (default — the
    # entity's own prospective loop) vs ``seed``/``handoff``/``agenda`` (the operator Tray).
    # Drives the Open-Loops / Tray / Keep render split; absent/falsy → ``loop`` (every
    # pre-Slice-3 spore). See ``levain.spores`` for the vocab + the bucketing partition.
    disposition: str = LOOP_DISPOSITION
    # The valid Class-B resolve kinds for THIS spore's type, derived from anneal's
    # own DESCEND_BY_TYPE / ASCEND_BY_TYPE (Slice 2b-ii). Schema-driven from the
    # substrate so the plane's verb affordances can't drift from anneal's taxonomy
    # (principle #4). Empty for an unknown type → the plane offers no resolve verb.
    descend_kinds: list[str] = field(default_factory=list)
    ascend_kinds: list[str] = field(default_factory=list)
    # when the spore was created, as stored ("" when the store has none): the Tray's age, never versioned
    created: str = ""
    # anneal's store-assigned origin key ("" when the store has none): the key a cockpit label is
    # written under (K2b-3b), so a rendered row can name its own label resource. Never versioned
    origin_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def open_spore_from(s: Mapping[str, Any]) -> "OpenSpore":
    """One stored spore (anneal's dict) as the view's ``OpenSpore``. The cockpit's spore rows and
    the version a spore write is compared against (``cockpit.providers.spore_row_version``) are
    both built through here. Raises ``ValueError``/``TypeError`` on a malformed row."""
    from anneal_memory.spores import ASCEND_BY_TYPE, DESCEND_BY_TYPE   # lazy, as the view's own read

    stype = str(s.get("type", ""))
    return OpenSpore(
        id=str(s.get("id", "")),
        type=stype,
        tier=str(s.get("tier", "")),
        salience=int(s.get("salience", 0) or 0),
        domain=str(s.get("domain", "")),
        # FULL (raw) text over the wire — NOT truncated. A 200-char
        # cap here lost the tail with no way to see it, and the 3b
        # text-edit seeded the editor from this value → save wrote the
        # truncation back (silent data loss). The surface clamps the row
        # for density + offers a per-item expand; canonical stays whole.
        text=str(s.get("text", "")),
        seen=str(s.get("seen", "")),
        # str-coerce next like its siblings: the TUI runs _oneline(next)
        # which would crash on a non-str next from a corrupted store
        # [codex L3 LOW]. None stays None (it's optional).
        next=(str(nx) if (nx := s.get("next")) is not None else None),
        pointer=s.get("pointer"),
        # str-coerce for parity (a corrupted store could carry a non-str
        # disposition); disposition_of itself stays the pure twin of the embedder's.
        disposition=str(disposition_of(s)),
        descend_kinds=sorted(DESCEND_BY_TYPE.get(stype, frozenset())),
        ascend_kinds=sorted(ASCEND_BY_TYPE.get(stype, frozenset())),
        origin_key=ok if isinstance(ok := s.get("origin_key"), str) else "",
        created=str(s.get("created") or ""),
    )


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
    - the constitution (``partnership.md`` / ``memory.md`` / ``memory_workflow.md`` / ``spore_instructions.md``)
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


# The starter jar: how full today is against this entity's own typical day. Every number
# is a count read from the entity's own episodic store; with no store or too little
# history the jar is EMPTY and says why (never a faked level).
JAR_WINDOW_DAYS = 14  # the "recent days" the typical day is the median of
JAR_MIN_HISTORY_DAYS = 3  # fewer prior days than this -> no typical day exists


@dataclass
class Jar:
    """Episodes captured today (host-local date) against the median daily count over the
    last ``JAR_WINDOW_DAYS`` full days (idle days count as zero; the window starts at the
    store's first episode, so a young entity is judged on the days it has had). ``level``
    is 0..1 with a typical day at 0.5 (full at twice typical). ``status``: ``ok`` |
    ``no_store`` | ``no_history``; anything but ``ok`` is level 0 and the ``label`` says
    why. Pure counts: nothing here reads an episode's content. Read in its own short
    transaction, so it is not atomic with the other tiers of one view (a capture landing
    between them can differ by one episode)."""

    status: str
    today: int | None
    typical: float | None
    history_days: int
    level: float
    label: str
    day: str | None  # the host-local date ``today`` belongs to (a client resets its baseline on a new day)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _local_day(ts: Any) -> "date | None":
    """The host-local calendar date of a stored UTC timestamp (None if unparseable)."""
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone().date()
    except (ValueError, TypeError, OverflowError):
        return None


def _read_jar(episodic_db: Path | None, now: datetime) -> "Jar":
    """Count episodes per host-local day straight from the store (read-only, stdlib
    sqlite3; independent of the anneal tiers). Fail-soft: any fault -> the ``no_store``
    jar, never an exception."""
    import sqlite3
    from statistics import median

    empty = Jar("no_store", None, None, 0, 0.0, "no store to read", None)
    if episodic_db is None:
        return empty
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Host-local days, resolved PER INSTANT (``astimezone()`` with no argument applies the
    # host zone's DST rules to each timestamp; a fixed offset would mis-bucket across a shift).
    # ONE statement = one snapshot: the window's rows plus a flag for whether the store holds
    # anything OLDER than the window (a sentinel row, present even when the window is empty:
    # a quiet old store is not a young one). Timestamps are the
    # ISO-8601 UTC strings anneal writes; a row in any other shape is skipped, never misread.
    try:
        today = now.astimezone().date()
        window_start = today - timedelta(days=JAR_WINDOW_DAYS)
        lower = (datetime.combine(window_start, datetime.min.time()).astimezone(timezone.utc)
                 - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        if not episodic_db.exists():
            return empty
        con = sqlite3.connect(f"{episodic_db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT NULL, EXISTS(SELECT 1 FROM episodes WHERE timestamp < ? "
                "AND timestamp GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*') "
                "UNION ALL SELECT timestamp, 0 FROM episodes WHERE timestamp >= ?",
                (lower, lower),
            ).fetchall()
        finally:
            con.close()
    except (OSError, RuntimeError, OverflowError, sqlite3.Error, ValueError):
        try:
            present = episodic_db.exists()
        except (OSError, RuntimeError):
            present = False
        return Jar("no_store", None, None, 0, 0.0,
                   "store unreadable" if present else "no store to read", None)
    counts: dict[Any, int] = {}
    older = False
    for ts, has_older in rows:
        older = older or bool(has_older)
        if ts is None:  # the sentinel row carrying the older-than-window flag
            continue
        day = _local_day(ts)
        if day is not None:
            counts[day] = counts.get(day, 0) + 1
    store_first = window_start if older else min(counts, default=None)
    n_today = counts.get(today, 0)
    day_iso = today.isoformat()
    if store_first is None or store_first >= today:
        return Jar("no_history", n_today, None, 0, 0.0, f"{n_today} today \u00b7 no history yet", day_iso)
    first = max(store_first, window_start)
    span = (today - first).days  # full days strictly before today
    if span < JAR_MIN_HISTORY_DAYS:
        return Jar("no_history", n_today, None, span, 0.0,
                   f"{n_today} today \u00b7 only {span} day(s) of history", day_iso)
    daily = [counts.get(first + timedelta(days=i), 0) for i in range(span)]
    typical = float(median(daily))
    if typical <= 0:
        return Jar("no_history", n_today, None, span, 0.0,
                   f"{n_today} today \u00b7 no typical day in the last {span} d", day_iso)
    level = min(n_today / (2 * typical), 1.0)
    return Jar("ok", n_today, typical, span, level,
               f"{n_today} today \u00b7 typical {typical:g} (median of {span} d)", day_iso)


def _humanize_age(delta_seconds: float) -> str:
    """``set just now`` / ``set 12m ago`` / ``set 3h ago`` / ``set 2d ago``. Coarse
    by design: the state line is a daily-ish signal, not a stopwatch. A negative delta
    (a future stamp) reads as "" (never a fabricated freshness)."""
    if delta_seconds < 0:
        return ""  # guard the FLOAT: int(-0.5)==0 would slip a sub-second future stamp
    s = int(delta_seconds)
    if s < 90:
        return "set just now"
    if s < 3600:
        return f"set {s // 60}m ago"
    if s < 86400:
        return f"set {s // 3600}h ago"
    return f"set {s // 86400}d ago"


# A state older than this is not shown or injected: a stale line presented as
# current is worse than none, so an expired state is DROPPED, never flagged.
STATE_EXPIRES_AFTER_HOURS = 12
# One bound, enforced at write (``write_state`` refuses) AND at read (an over-cap state
# reads as unset), so no surface shows a line another surface cannot. Mirrored by the hooks.
STATE_MAX_TEXT_LEN = 500
# A stamp up to this far in the future is clock skew between writer and reader (a phone
# app, another machine), not bad data: it reads as age 0, never as unset.
STATE_CLOCK_SKEW_SECONDS = 300
STATE_KEYS = ("state", "state_set_at", "state_source")   # the stored record a state write binds to
STATE_LOCK_WAIT_S = 5.0   # how long a state write waits for another writer's lock before refusing


@dataclass
class State:
    """The operator's optional, freeform "what's going on" line, in their own words
    (keys ``state`` / ``state_set_at`` / ``state_source`` / ``state_run`` in the context file). Rendered VERBATIM with its age, and NOTHING more: it is never
    parsed, scored, classified, or used to gate, soften or reduce scope — the human is
    the authority on their own state. Expires after ``STATE_EXPIRES_AFTER_HOURS``: an
    expired, age-unknown or future-stamped state reads as unset (``text=None``)."""

    text: str | None  # the authored state; None when unset OR expired → render nothing
    set_at: str | None
    source: str | None
    age_label: str = ""  # "set 2h ago"; "" when text is None
    version: str | None = None  # the stored record's write version (``state_version``); None if unreadable
    run: str | None = None  # ISO stamp at which this text's run began (``state_run``); ``set_at`` for a line written before it existed

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def state_record(data: dict[str, Any]) -> dict[str, Any]:
    """The stored record a state write binds to: the ``STATE_KEYS`` as stored, other keys ignored.
    The cockpit's state panel versions this same record (``Read.version_of``)."""
    return {k: data.get(k) for k in STATE_KEYS}


def state_version(data: Any) -> str | None:
    """The version a state write binds to: the cockpit's value version of ``state_record``, so
    the legacy page and the manifest name the same version. None for a file that is not a JSON
    object."""
    if not isinstance(data, dict):
        return None
    from levain.cockpit.engine import value_version_of   # lazy: the cockpit imports this module
    return value_version_of(state_record(data))


def _read_state(context_json: Path | None, now: datetime) -> "State | None":
    """Read ``{state, state_set_at, state_source, state_run}`` from the live-context file. None = no
    source configured; fail-soft to ``text=None`` on a missing/unreadable/malformed file;
    whitespace collapsed; superset-tolerant (other keys are ignored). EXPIRY: a state whose age cannot be established, is further
    in the future than ``STATE_CLOCK_SKEW_SECONDS``, is older than
    ``STATE_EXPIRES_AFTER_HOURS``, or is over ``STATE_MAX_TEXT_LEN`` reads as unset."""
    if context_json is None:
        return None
    empty = State(text=None, set_at=None, source=None)
    try:
        if not context_json.exists():
            # a source with no file yet: unset, and its first write names the cockpit's VALUE_ABSENT
            from levain.cockpit.engine import VALUE_ABSENT   # lazy: the cockpit imports this module
            return State(text=None, set_at=None, source=None, version=VALUE_ABSENT)
        data = json.loads(context_json.read_text(encoding="utf-8"))
    except FileNotFoundError:     # removed after the exists() check: as a missing file
        from levain.cockpit.engine import VALUE_ABSENT
        return State(text=None, set_at=None, source=None, version=VALUE_ABSENT)
    except (OSError, ValueError):
        return empty
    return state_from_data(data, now)


def state_from_data(data: Any, now: datetime) -> State:
    """The state line from a parsed context file (``_read_state``'s rules); a caller that holds the
    record it versions builds the line from that same read."""
    empty = State(text=None, set_at=None, source=None, version=state_version(data))
    if not isinstance(data, dict):
        return empty
    raw_text = data.get("state")
    text = " ".join(raw_text.split()) if isinstance(raw_text, str) and raw_text.split() else None
    raw_at = data.get("state_set_at")
    set_at = raw_at if isinstance(raw_at, str) and raw_at.strip() else None
    raw_src = data.get("state_source")
    source = raw_src if isinstance(raw_src, str) and raw_src.strip() else None
    if not text or not set_at or len(text) > STATE_MAX_TEXT_LEN:
        return empty  # no text, no stamp (age unknowable) or over the cap → not shown
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(set_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = (now - ts).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return empty
    if delta < -STATE_CLOCK_SKEW_SECONDS or delta >= STATE_EXPIRES_AFTER_HOURS * 3600:
        return empty
    delta = max(delta, 0.0)
    raw_run = data.get("state_run")
    run = set_at        # a line written before the run was stored began its run when it was set
    if isinstance(raw_run, str) and raw_run.strip():
        try:
            datetime.fromisoformat(raw_run)
            run = raw_run
        except ValueError:
            pass
    return State(text=text, set_at=set_at, source=source, age_label=_humanize_age(delta), version=empty.version, run=run)


def _write_context_line(context_json: Path, key: str, text: str, *, source: str,
                        run: str | None = None) -> "str | None":
    """Merge-preserving atomic write of one ``{key, key_set_at, key_source, key_run}`` record
    (the body of ``write_state``). ``key_run`` is ``run`` (the stamp a continuing run began at),
    or this write's own stamp when ``run`` is None. Returns the run stamp it stored, None for a clear."""
    text = " ".join(text.split())  # collapse whitespace; "" → clear
    data: dict[str, Any] = {}
    try:
        if context_json.exists():
            existing = json.loads(context_json.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = existing
    except (OSError, ValueError):
        data = {}  # corrupt → start fresh; don't fail the operator's set
    stamp = datetime.now(timezone.utc).isoformat() if text else None
    if stamp is not None:
        data[key] = text
        data[f"{key}_set_at"] = stamp
        data[f"{key}_source"] = source
        data[f"{key}_run"] = run or stamp
    else:
        # CLEAR: POP all four keys — the exact shape the CLI/sensor clear writes
        # (drain_context_signals / perception_sweep pop, they don't leave a state="" +
        # a fresh live stamp). One on-disk contract for "unset", not two: an empty-string
        # tombstone carrying a current source/stamp is misleading to any raw-JSON reader
        # that doesn't replicate the truthiness-gate. `_read_state` already reads BOTH a
        # missing key and "" as unset, so this is read-invisible.
        data.pop(key, None)
        data.pop(f"{key}_set_at", None)
        data.pop(f"{key}_source", None)
        data.pop(f"{key}_run", None)
    # Write THROUGH a symlink to its target (don't clobber the link with a regular
    # file), and keep os.replace atomic by putting the temp in the TARGET's dir.
    target = Path(os.path.realpath(context_json)) if context_json.is_symlink() else context_json
    target.parent.mkdir(parents=True, exist_ok=True)
    # A TRULY-unique temp in the target's dir (mkstemp, not a name that could collide
    # under concurrent writers); unlinked on a mid-write fault so a failure leaves the
    # real file untouched AND no litter.
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, target)  # atomic same-filesystem swap — no torn read
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return (run or stamp) if stamp is not None else None


class StateLockUnavailable(RuntimeError):
    """A version-bound state write on a platform with no ``flock`` (Windows): refused, since the
    compare and the write cannot be made one step there."""


@contextmanager
def _state_lock(context_json: Path, *, required: bool) -> Iterator[None]:
    """An exclusive lock held across one state write, on a sidecar of the file's REAL path (so a
    symlinked context file and its target share one lock, and the atomic ``os.replace`` of the file
    never swaps the locked inode). ``write_state`` takes it, so every writer that sets the line
    through it is serialised (the test names them). Where ``flock`` does not exist, an unchecked write runs unlocked
    (last writer wins, as before) and a ``required`` one is refused. Where it does exist, a lock
    that cannot be taken (the lock file, ``flock`` itself) refuses every write with ``OSError``."""
    try:
        import fcntl
    except ImportError:
        if required:
            raise StateLockUnavailable("this platform has no flock: a version-bound state write is refused")
        yield
        return
    lock = os.path.realpath(context_json) + ".lock"
    # where flock exists every writer takes the lock or does not write: one unlocked write could land
    # between a checked writer's compare and its write and be overwritten unseen
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # a bounded wait, then refuse (SQLite's busy_timeout shape): a stopped holder never hangs a writer
        deadline = time.monotonic() + STATE_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(f"the state file is locked by another writer ({lock}); try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)    # closing the descriptor releases the flock


@dataclass(frozen=True)
class StateWrite:
    """What one ``write_state`` did beyond the line itself: the felt episode it owed.
    ``episode`` is ``pending`` (only with ``defer_episode``: ``deliver`` finishes it), ``written`` (the store holds this run's episode: this call wrote it, or an
    earlier call of the same run had; anneal does not say which), ``cleared`` (a clear sets
    nothing), ``no_store`` (no episodic store to write to) or ``failed`` (the line is set, the
    episode is not; ``episode_error`` says why)."""

    text: str
    episode: str
    episode_error: str | None = None
    deliver: "Callable[[], StateWrite] | None" = None   # set only with ``defer_episode``: episode ``pending``, this finishes it


def _exc_text(exc: BaseException) -> str:
    """``Type: message`` for a result, without trusting the message: an exception whose ``__str__``
    raises must still become a ``failed`` result, never a second exception after the line is set."""
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:   # noqa: BLE001
        return type(exc).__name__


STATE_EPISODE_SOURCE = "levain-state"
STATE_EPISODE_TAGS = ("felt", "state")


def _state_origin_key(run: str, text: str) -> str:
    """The episode's identity: the run (the stamp its text began at) and the text, hashed into
    anneal's origin-key grammar. A different run or text never collides; the same run always does.
    Two context files sharing one store that begin the same words at the same instant are one run
    to the store: it keeps the first."""
    return "levain.state:" + hashlib.sha256(f"{run}\n{text}".encode("utf-8")).hexdigest()


def _record_state_episode(episodic_db: Path, text: str, source: str, *, run: str) -> StateWrite:
    """Append the one ``observation`` episode, tagged ``felt,state``, that carries the line
    verbatim: an input the consolidate reads, never a write into the felt layer. Fail-soft: the
    line is already set, so any fault here is reported in the result, never raised. A store
    that does not exist is not created for a state line.

    The episode is stamped with the instant its run began and keyed on the run, so a retry, N
    identical requests, or a writer whose episode lands late each find the one episode their run
    owes and write no second; a line whose episode failed (locked store, a death between the two
    writes) gets it on the next set of the same text. It is recorded with the store's default
    agent trust, not ``operator``: this path is reachable by an HTTP write token an agent can hold,
    so marking its text operator testimony would claim an authority the path cannot prove. Known
    edge: an operator who tombstones a run's episode and then re-sets the same unchanged text gets
    ``failed`` with anneal's refusal (a key is never reused); a changed text or a clear starts a new run."""
    if not episodic_db.is_file():
        return StateWrite(text, "no_store")
    try:
        from anneal_memory import Store   # lazy, as the view's own read

        began = datetime.fromisoformat(run)
        if began.tzinfo is None:
            began = began.replace(tzinfo=timezone.utc)      # as the read treats a naive stamp
        with Store(str(episodic_db)) as store:
            store.record(
                content=text, episode_type="observation", source=STATE_EPISODE_SOURCE,
                metadata={"tags": list(STATE_EPISODE_TAGS), "state_source": source},
                timestamp=began.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                origin_key=_state_origin_key(run, text),
            )
    except Exception as exc:   # noqa: BLE001: a secondary effect never fails the state write
        return StateWrite(text, "failed", _exc_text(exc))
    return StateWrite(text, "written")


def write_state(context_json: Path, text: str, *, source: str = "cli",
                check: "Callable[[], None] | None" = None,
                episodic_db: Path | None = None,
                record_episode: "Callable[[str, str, str], None] | None" = None,
                defer_episode: bool = False) -> StateWrite:
    """Set (or, with blank ``text``, clear) the operator's freeform state line: the
    write-peer of ``_read_state``, kept adjacent so the contract (the state keys, a
    tz-aware ISO stamp) cannot drift between read and write.

    MERGE-preserving: reads the existing object and updates ONLY the four state keys,
    so a file that carries other keys (a sensor app's superset, or keys an older
    version wrote) keeps them. Atomic (temp + ``os.replace``, so a reader never sees a torn
    file) and serialised with every other levain state writer by ``_state_lock``;
    ``check`` (the cockpit's version compare) runs under that lock, so the compare and the
    write are one step. A writer outside levain (a sensor app, the embedder's own state CLI) does
    not take the lock: a concurrent whole-object writer holding an older snapshot can
    revert a just-set line (re-set is one keystroke). A blank text
    CLEARS by popping all four keys. The parent dir is created if absent; a
    corrupt/unreadable existing file is replaced rather than failing the set. The text is
    stored verbatim apart from whitespace collapse; nothing reads meaning into it.

    A RUN is the stretch of one text while it stays live. ``state_run`` stores the stamp it began
    at, set in the same locked write as the line: a re-set of the live text keeps the run (and
    refreshes ``state_set_at``); a changed, cleared or expired text begins a new one.

    This is the ONE place "a state line was set" is computed. Given ``episodic_db`` it also
    appends ONE ``observation`` episode tagged ``felt,state`` carrying the line verbatim, keyed on
    its run, AFTER the line is written and outside the lock (never an episode without the line);
    none for a clear, and a run never gets a second one. A writer that passes no ``episodic_db``
    records no episode. ``record_episode(text, source, run)``, when given, is called INSTEAD of
    recording into ``episodic_db`` (an embedder whose episode store is not an anneal store levain can
    open): once per set, with the run, so its sink dedupes per run; a raise is ``failed``, never a
    failed set. The sink is synchronous and must be bounded: it runs on the caller's thread. It
    must key on ``run`` and not infer recency from call order (two concurrent sets can reach it
    in the opposite order from the one in which they landed). ``levain state`` (the CLI) has no
    scope, so it always records into the install's own store. With ``defer_episode`` the line is
    set and the result is ``pending`` with ``deliver``: the caller finishes it AFTER dropping any
    lock of its own, so a slow store or sink never holds that lock."""
    collapsed = " ".join(text.split())
    if len(collapsed) > STATE_MAX_TEXT_LEN:
        raise ValueError(f"state exceeds {STATE_MAX_TEXT_LEN} chars")
    with _state_lock(context_json, required=check is not None):
        if check is not None:
            check()
        prior = _read_state(context_json, datetime.now(timezone.utc))   # hides an expired line
        continues = prior is not None and prior.text == collapsed and collapsed != ""
        run = _write_context_line(context_json, "state", collapsed, source=source,
                                  run=prior.run if continues else None)
    if run is None:
        return StateWrite("", "cleared")
    def deliver() -> StateWrite:
        if record_episode is not None:
            try:
                record_episode(collapsed, source, run)
            except Exception as exc:   # noqa: BLE001: a secondary effect never fails the state write
                return StateWrite(collapsed, "failed", _exc_text(exc))
            return StateWrite(collapsed, "written")
        if episodic_db is None:
            return StateWrite(collapsed, "no_store")
        return _record_state_episode(episodic_db, collapsed, source, run=run)

    if defer_episode:
        return StateWrite(collapsed, "pending", deliver=deliver)
    return deliver()


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
    # The spore layer, split into its three operator-facing projections (Slice 3). The
    # FULL list_open result is bucketed via ``levain.spores.bucket_of`` — a total
    # partition, so a spore lands in exactly one. ``open_spores`` keeps its name (the
    # active prospective loops — the entity's live cognition) for backward-compat with
    # every existing consumer; ``tray`` (the operator session-I/O inbox) and ``keep`` (the
    # durable pinned-dormant loops) are new. Each is independently capped at ``max_spores``.
    open_spores: list[OpenSpore] = field(default_factory=list)
    tray: list[OpenSpore] = field(default_factory=list)
    keep: list[OpenSpore] = field(default_factory=list)
    episodes: list[EpisodeRow] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    config_docs: list[ConfigDoc] = field(default_factory=list)
    wraps: list[WrapRow] = field(default_factory=list)
    recent_edits: list[dict[str, Any]] = field(default_factory=list)
    # The operator's live self-authored state line (set from any surface, read by
    # every session). None when there is no live-context source. NOT one of the anneal
    # memory stores: live-state, not consolidated cognition.
    state: State | None = None
    jar: Jar | None = None  # today's episode count against the entity's own typical day
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
        # The three spore projections, adjacent: the entity's active loops, the operator
        # session-I/O inbox, the durable pinned-dormant shelf (Slice 3 capture-UX (A) —
        # ONE Tray, not three disposition panels). All three are Class B: Open Loops gets
        # the lifecycle verbs; Tray (Slice 3b) gets the freeform dump box + per-row triage
        # verbs (metabolize / schedule / dismiss); Keep gets the lifecycle verbs + schedule.
        # Honesty floor: the three projections share ONE spore read; when it FAULTS the
        # count is meaningless, so the header must show "(unavailable)" — never "(0)" over
        # an unreadable store (that would contradict the body's "unavailable" and read as a
        # false zero on a header-only glance). [no_data != no_event; header/body parity]
        _spore_err = "open_spores" in self.errors
        _oc = "unavailable" if _spore_err else len(self.open_spores)
        _tc = "unavailable" if _spore_err else len(self.tray)
        _kc = "unavailable" if _spore_err else len(self.keep)
        panels.append(
            {"kind": "spores", "zone": ZONE_OPERATE, "edit_class": CLASS_B,
             "title": f"Open loops ({_oc})"}
        )
        panels.append(
            {"kind": "tray", "zone": ZONE_OPERATE, "edit_class": CLASS_B,
             "title": f"Tray ({_tc})"}
        )
        panels.append(
            {"kind": "keep", "zone": ZONE_OPERATE, "edit_class": CLASS_B,
             "title": f"Keep ({_kc})"}
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
            "tray": [s.to_dict() for s in self.tray],
            "keep": [s.to_dict() for s in self.keep],
            "episodes": [e.to_dict() for e in self.episodes],
            "sections": [s.to_dict() for s in self.sections],
            "config_docs": [d.to_dict() for d in self.config_docs],
            "wraps": [w.to_dict() for w in self.wraps],
            "recent_edits": self.recent_edits,
            "state": self.state.to_dict() if self.state else None,
            "jar": self.jar.to_dict() if self.jar else None,
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


def installed_adapter(install_root: Path) -> str | None:
    """The adapter recorded in ``.levain/config.json`` (``"openhands"``), or ``None``.

    The detection channel for a HOOKLESS install (openhands lays down no CLAUDE.md /
    AGENTS.md tag-file, so ``doctor`` / ``verify-hooks`` can't detect it by file
    presence). Only the hookless installer stamps this today; a hosted-harness install
    returns ``None`` here and is detected by its tag-file, unchanged. Fail-soft (rides
    ``_read_levain_config``)."""
    adapter = _read_levain_config(install_root).get("adapter")
    return adapter if isinstance(adapter, str) else None


# The constitution files an entity carries (read-only view), with display names.
# The seed ``continuity.md`` is intentionally NOT here: it is the near-empty SCHEMA
# template (just the six section headings), and the operator's LIVE neocortex
# already renders in the Mind zone — showing the empty schema as "constitution"
# would only confuse. (Scope §4 lists it; this is a deliberate deviation.)
_CONSTITUTION = (
    ("partnership.md", "Constitution · how we work"),
    ("memory.md", "Constitution · your memory"),
    ("memory_workflow.md", "Constitution · your memory's wrap"),
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
    """Assemble the historical association graph from a (read-only) store.

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
    return AssociationGraph(
        nodes=nodes, edges=edges, truncated=truncated,
        formation_off_at_wrap=_formation_off_at_wrap(store),
    )


def _episode_tags(ep: Any) -> list[str]:
    """Tags ride in an episode's ``metadata`` dict (the episodic writer stores
    ``--tags`` there). Defensive: any non-list / absent value → empty."""
    meta = getattr(ep, "metadata", None)
    if isinstance(meta, dict):
        tags = meta.get("tags")
        if isinstance(tags, list):
            return [str(t) for t in tags]
    return []


def _episode_row(ep: Any) -> EpisodeRow:
    """Build the dashboard's ``EpisodeRow`` from a raw anneal episode — the SINGLE
    place the row shape lives, so the recent-episodes panel and the keyword-recall
    surface (spore-107) can never drift apart. Content rides FULL (raw) over the wire:
    truncating it here lost the tail with no way to see it (and an edit would have
    written the truncation back). The surface clamps the row for density + offers a
    per-item expand; the canonical text stays whole [display-renders / view-clamps]."""
    return EpisodeRow(
        id=ep.id,
        timestamp=ep.timestamp,
        type=ep.type.value,
        source=ep.source,
        content=ep.content,
        tags=_episode_tags(ep),
    )


# A keyword longer than this is pathological (no real search term is) — reject it for
# parity with the POST-body cap, so an unbounded ?keyword= can't force a giant LIKE scan.
# Loopback + the bounded read-gate + the result cap already bound the risk; this closes
# the one remaining unbounded input at the data layer (every caller inherits it).
_MAX_KEYWORD_LEN = 256


def recall_episode_rows(
    episodic_db: Path, *, keyword: str, limit: int
) -> tuple[list[EpisodeRow], str | None]:
    """Read-only keyword search over the episodic store — the data layer behind the
    cockpit's episode-search box (spore-107). Opens the Store ``read_only=True`` and
    delegates the match to anneal's own ``Store.recall(keyword=...)`` (content ``LIKE``,
    wildcards escaped, newest first), returning rows in the SAME shape as the recent-
    episodes panel (via ``_episode_row``) so the surface renders them identically.

    Returns ``(rows, error)``: a store/data fault degrades into the ``error`` string
    (the surface shows 'search unavailable', never blanks), mirroring
    ``build_substrate_view``'s degrade-don't-fail read discipline. An empty ``keyword``
    is the caller's no-op (returns no rows) — the surface reverts to the recent list on
    clear rather than asking the store to match everything."""
    if not keyword:
        return [], None
    if len(keyword) > _MAX_KEYWORD_LEN:
        return [], f"keyword too long (max {_MAX_KEYWORD_LEN} chars)"
    # Mirror build_substrate_view's fault classification (anneal wraps store faults in
    # AnnealMemoryError, imported lazily so this module stays importable without anneal).
    try:
        from anneal_memory import AnnealMemoryError

        data_faults: tuple[type[BaseException], ...] = (
            AnnealMemoryError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        )
    except Exception:  # noqa: BLE001 — anneal absent; the surface reports it
        data_faults = (OSError, ValueError, json.JSONDecodeError)
    store_faults: tuple[type[BaseException], ...] = (*data_faults, ImportError)

    rows: list[EpisodeRow] = []
    try:
        if not episodic_db.exists():
            raise FileNotFoundError(f"no anneal store at {episodic_db}")
        from anneal_memory import Store

        with Store(episodic_db, read_only=True) as store:
            try:
                for ep in store.recall(keyword=keyword, limit=limit).episodes:
                    rows.append(_episode_row(ep))
            except data_faults as exc:
                return rows, f"{type(exc).__name__}: {exc}"
    except store_faults as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return rows, None


# The neocortex sections of anneal's partnership schema (FLOW_SCHEMA), in its order, including
# the optional Durable Facts (anneal 0.9.28). A section the continuity lacks is skipped.
_ALL_SECTIONS = ("State", "Active Threads", "Durable Facts", "Patterns", "Decisions", "Context",
                 "Understanding")

# The sections that ride the MODEL-visible render_summary (the MCP-App content
# half) — kept to the two headlines so the full expansion never bloats model
# context. Every section always rides structuredContent for the UI.
_SUMMARY_SECTIONS = frozenset({"State", "Active Threads"})


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def build_substrate_view(
    paths: AnnealPaths,
    *,
    today: date | None = None,
    now: datetime | None = None,
    install_root: Path | None = None,
    ledger_root: Path | None = None,
    context_json: Path | None = None,
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
    tier is simply absent (no error — there is nothing to read there).

    ``context_json`` (when given) locates the live operator-context file holding the
    minimal ``{state, state_set_at, state_source}`` contract, read fail-soft into
    ``view.state`` (None when absent). ``now`` is the reference instant for the state
    line's expiry (defaults to UTC-now; passed explicitly in tests)."""
    view = SubstrateView(paths=paths, scope=scope)

    # --- live operator context (the state line), independent of the anneal stores; its
    # own fail-soft read (never raises). A daily-driver cockpit must not blank on an
    # operator-context read fault, so _read_state degrades to text=None internally.
    _now = now if now is not None else datetime.now(timezone.utc)
    if _now.tzinfo is None:  # a naive caller-supplied now → compare in UTC
        _now = _now.replace(tzinfo=timezone.utc)
    view.state = _read_state(context_json, _now)
    view.jar = _read_jar(paths.episodic_db, _now)

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
                    write_path_live=status.total_wraps > 0,
                    status="live" if status.total_wraps > 0 else "no-wraps",
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
                    **health_oracles(
                        status,
                        _last_wrap_row(paths.episodic_db),
                        paths.continuity_md,
                        now=_now,
                        reread=lambda: _last_wrap_row(paths.episodic_db),
                    ),
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
                    view.episodes.append(_episode_row(ep))
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

    # --- open spores → the three operator-facing projections (Slice 3) ------
    #     Open Loops (active cognition) / Tray (operator session-I/O inbox) / Keep
    #     (durable pinned-dormant), bucketed via levain.spores.bucket_of. Bucket the
    #     FULL ranked list_open result (NOT a pre-sliced head): parked-tier Keep items
    #     rank LAST (parked sorts after hot/warm/cold) and a flat [:max_spores] would
    #     starve them out of their own panel. Each bucket is independently capped at
    #     max_spores — keeping each bucket's TOP max_spores BY RANK, which is meaningful
    #     ONLY because list_open is pre-ranked (a future refactor that feeds an UNRANKED
    #     list here would make each bucket's cap arbitrary). Defensive per-row: one
    #     malformed row skipped, the tier survives.
    try:
        from anneal_memory.spores import SporeStore

        if paths.spores_json.exists():
            # bucket name → its destination list; the BUCKET_LOOP default is open_spores
            bucket_target = {BUCKET_TRAY: view.tray, BUCKET_KEEP: view.keep}
            for s in SporeStore(paths.spores_json).list_open(today=today):
                # The WHOLE per-row pipeline (status-skip → route → cap → construct) lives
                # in ONE try, so a fault on ANY phase drops just that row — the "one
                # malformed row skipped, the tier survives" guarantee holds for routing,
                # not only construction [complement L3 LOW]. (anneal's _load already
                # guarantees dict rows, so this is defense-in-depth, not a live exposure.)
                try:
                    # Defensive (codex L3 MED): anneal MOVES a resolved spore to the
                    # `resolved` array AND sets status=resolved, so a status=resolved row
                    # still in the `spores` array is store drift. list_open does NOT filter
                    # status — skip it so it can't render as an open loop carrying verbs.
                    if s.get("status") == "resolved":
                        continue
                    target = bucket_target.get(bucket_of(s), view.open_spores)
                    if len(target) >= max_spores:
                        continue  # this bucket is full — keep scanning for the others
                    target.append(open_spore_from(s))
                except (ValueError, TypeError):
                    continue  # one malformed row skipped; the tier survives
    except store_faults as exc:
        view.errors["open_spores"] = f"{type(exc).__name__}: {exc}"

    # --- continuity narrative sections (every neocortex section) ------------
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
            # Pack white-label — the SAME config channel as entity_name (install baked
            # it from pack.toml [brand]). surface_name → the masthead/wordmark/title
            # name; subtitle → the tagline line. A programmatic SubstrateSource
            # override (the bridge flow-branding its cockpit) still WINS: build()
            # copies its non-None brand fields ON TOP of these after build returns.
            surface_name = config.get("surface_name")
            if isinstance(surface_name, str) and surface_name.strip():
                view.brand_wordmark = surface_name.strip()
            subtitle = config.get("subtitle")
            if isinstance(subtitle, str) and subtitle.strip():
                view.brand_model = subtitle.strip()
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
        # A non-install substrate (the embedder's own store) has no seed/config surface but DOES
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
    # The masthead is the tool's NAME → the wordmark (brand_wordmark), never the
    # subtitle (brand_model): a subtitle-only brand shows the Levain default here,
    # consistent with the TUI/web wordmark (which also key off brand_wordmark). [L1]
    masthead = view.brand_wordmark or "Levain substrate"
    out: list[str] = [f"{masthead} — {title}", f"  store: {view.paths.episodic_db}"]
    if view.state is not None and view.state.text:
        out.append(f"  state: {view.state.text} ({view.state.age_label})")
    if view.jar is not None:
        out.append(f"  jar: {view.jar.label}" + ("" if view.jar.status == "ok" else " (empty)"))
    out.append("")

    h = view.health
    if h is not None:
        out.append("Health")
        last_wrap = h.last_wrap_at.split("T")[0] if h.last_wrap_at else None
        if h.write_path_live:
            out.append(f"  Write-path:   LIVE — last wrap {last_wrap or 'recorded'}")
        else:
            out.append("  Write-path:   no wraps yet")
        oracle = health_oracle_line(h)
        if oracle:
            out.append(f"  Substrate:    {oracle}")
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
        out.append(f"Association graph: {graph_caption(g)}")
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

    # Tray — the operator session-I/O inbox (badged by disposition); Keep — durable
    # pinned-dormant loops (the parked tier, lifted out of Open loops above).
    if view.tray:
        out.append(f"Tray ({len(view.tray)})")
        for s in view.tray:
            nxt = f" → surface {s.next}" if s.next else ""
            out.append(f"  - [{s.disposition}] {s.text}{nxt}")
        out.append("")

    if view.keep:
        out.append(f"Keep ({len(view.keep)})")
        for s in view.keep:
            out.append(f"  - [{s.tier}] {s.text}")
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
                + (f"+{w.associations_formed} links · " if w.associations_formed else "")
                + f"{w.continuity_chars:,} chars"
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
    # The masthead is the tool's NAME → the wordmark (brand_wordmark), never the
    # subtitle (brand_model): a subtitle-only brand shows the Levain default here,
    # consistent with the TUI/web wordmark (which also key off brand_wordmark). [L1]
    masthead = view.brand_wordmark or "Levain substrate"
    lines: list[str] = [f"{masthead} — {title}"]
    if view.state is not None and view.state.text:  # fresh only: _read_state drops an expired one
        lines.append(  # quoted, like the hook: the operator's words, delimited
            f"State: {json.dumps(view.state.text, ensure_ascii=False)} ({view.state.age_label})"
        )

    h = view.health
    if h is not None:
        if h.write_path_live:
            lines.append("Health: write-path LIVE")
        else:
            lines.append("Health: no wraps yet")
        oracle = health_oracle_line(h)
        if oracle:
            lines.append(f"  {oracle}")
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
    if view.tray:
        lines.append(f"Tray (operator inbox): {len(view.tray)}")
    if view.keep:
        lines.append(f"Keep (pinned-dormant): {len(view.keep)}")
    lines.append(f"Recent episodes: {len(view.episodes)}")
    if view.config_docs:
        lines.append(f"Seed/config docs: {len(view.config_docs)}")

    # Only the two headline sections ride the MODEL-visible summary — every section
    # rides in structuredContent for the UI. Folding them all here would bloat model
    # context, the exact inversion the content/structuredContent split exists to
    # prevent (the 1.5 expansion to every section must NOT leak into this digest).
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


def run_state(
    path: Path, text: str | None = None, *, source: str = "cli", clear: bool = False
) -> int:
    """``levain state`` entry point — set / show / clear the operator's state line.
    Reads and writes the install's ``.levain/context.json``. With ``text`` (or
    ``--clear``) it SETS; with neither it SHOWS the state while it is still within its
    12 h life.
    The text is echoed verbatim and never interpreted."""
    install_root = Path(path).expanduser().resolve()
    context_json = install_root / ".levain" / "context.json"
    if (text is not None or clear) and not (install_root / ".levain" / "memory.db").exists():
        print(
            f"note: no Levain store at {install_root} — setting state here anyway; "
            "run `levain init` (or pass --path to an install) so a cockpit reads it.",
            file=sys.stderr,
        )
    if clear:
        text = ""
    if text is not None:
        episodic_db = None      # the episode is secondary: a store that cannot be derived is no store
        if text.strip():
            try:
                episodic_db = SubstrateSource.local(install_root).anneal.episodic_db
            except Exception:   # noqa: BLE001
                pass
        try:
            done = write_state(context_json, text, source=source, episodic_db=episodic_db)
        except (ValueError, OSError) as exc:
            print(f"state not set: {exc}", file=sys.stderr)
            return 2
        if done.episode == "failed":
            print(f"note: state set, but its felt episode was not written: {done.episode_error}", file=sys.stderr)
        print(f"state set: {' '.join(text.split())}" if text.strip() else "state cleared")
        return 0
    state = _read_state(context_json, datetime.now(timezone.utc))
    if state is None or state.text is None:
        print("no state set")
        return 0
    via = f" · via {state.source}" if state.source else ""
    print(f"{state.text}  ({state.age_label}{via})")
    return 0
