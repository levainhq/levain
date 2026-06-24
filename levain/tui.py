"""levain.tui — Levain v2: the terminal-native control-plane surface.

The third front-end over the v2 control plane, peer to the web-app (``levain
serve``) and the parked in-host MCP-App (``levain serve-app``). Same canonical
substrate, another replaceable surface: ``canonical_object_model_plus_replaceable_surfaces``
made literal. Where ``levain serve`` is the cross-platform GUI surface, this is
the Unix-terminal-native one — no browser, no server, no port, no account.

**It calls the same in-process API the web server calls.** The read path is
``dashboard.build_substrate_view`` → a ``SubstrateView``, rendered straight off
``view.layout()`` (the schema-driven panel manifest — the IA + edit-class model
live in Python on the substrate, so this surface CANNOT drift from what it
edits). The write path is ``writes.apply_edit(scope, req)`` — the SAME
governed dispatcher the ``POST /edit`` handler calls. All the load-bearing
governance (the Class-A allowlist, the destructive-verb ``confirm`` gate, the
``require=True`` continuity lock, the optimistic stale-check) lives INSIDE
``writes`` (substrate-side), so an in-process caller inherits the identical moat
and merely skips the HTTP-only guards (Host allowlist / CSRF) that only exist
because HTTP has a network. No IPC, no shell, no MCP round-trip.

**The locked v2 design doctrine, translated to the terminal:**
- *bridge-not-Jarvis* — an inspect-and-steer console of keystroke verbs over
  panels, never a chat input. (A TUI is natively this; there is no temptation.)
- *NO THEATER* — every key the footer advertises is a real, enabled verb for the
  selected panel; every number on screen is true store state. A verb key is shown
  ONLY when it is wired and applicable (so the read-only scaffold advertises only
  navigation; write verbs appear when a panel's edit-class permits one).
- *displays-are-glass / controls-are-metal* — the edit-class becomes the visual
  grammar: Class-C panels (the consolidate's own cognition) render dim and offer
  no verb (glass, read-only); Class-A/B panels render active and carry their verb
  affordances (metal). The materiality is the authorization model worn as paint.

Structure: a PURE view-model + navigation/render layer (no curses — fully
unit-testable: ``TuiModel`` + the ``dispatch_*`` reducers + the ``render_*``
helpers + the ``build_*_req`` request shapers + ``edit_error_status``), and a thin
curses DRIVER (``_run_curses`` + the paint helpers + the ``$EDITOR`` shell-out)
that reads keys, calls the reducer, and paints the model. The driver needs a real
terminal so it is exercised by the live L4 canary, not the unit suite; the pure
layer carries the logic the apparatus reviews.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from levain.dashboard import (
    CLASS_A,
    CLASS_B,
    ZONE_IDENTITY,
    ZONE_MIND,
    ZONE_OPERATE,
    SubstrateSource,
    SubstrateView,
)

__all__ = [
    "ZONES",
    "TuiModel",
    "Verb",
    "panels_for_zone",
    "panel_item_count",
    "is_item_list",
    "is_record_undoable",
    "render_panel_lines",
    "panel_verbs",
    "select_zone",
    "select_panel",
    "move_in_panel",
    "with_view",
    "current_spore",
    "current_row",
    "current_episode",
    "current_edit_record",
    "build_touch_req",
    "build_descend_req",
    "build_ascend_req",
    "build_tombstone_req",
    "build_undo_req",
    "build_seed_req",
    "build_set_disposition_req",
    "build_surface_at_req",
    "build_spore_update_req",
    "build_config_edit_req",
    "build_state_edit_req",
    "edit_error_status",
    "run_tui",
]


def _oneline(s: str) -> str:
    """Collapse newlines so a value stays on ONE rendered line. The verb-list
    renderers emit exactly one line per item, and the driver's cursor highlight +
    auto-scroll map item-index 1:1 to line-index — a stray ``\\n`` in spore text or
    episode content would wrap and desync the highlight (complement L3 MED-1)."""
    return s.replace("\n", " ").replace("\r", " ")

# Zone order = the IA's tab order (Identity · Operate · Mind), keyed 1/2/3.
ZONES: tuple[tuple[str, str], ...] = (
    (ZONE_IDENTITY, "Identity"),
    (ZONE_OPERATE, "Operate"),
    (ZONE_MIND, "Mind"),
)

# Panel kinds whose detail is an item LIST the cursor selects a row in (a verb
# acts on the selected row). These render ONE line per item so the item index
# maps 1:1 to a line (the driver's highlight + auto-scroll depend on it —
# `structural_invariants_beat_discipline`, locked by a test). Every other kind —
# including the Class-C crystals/wraps lists — is a text panel that scrolls as one
# body (no verb, so no cursor needed). Tray + Keep joined the list when their
# Class-B operator-I/O verbs landed (the curses peer of the web's Slice-3b verbs).
_LIST_KINDS = frozenset({"spores", "tray", "keep", "episodes", "edits"})

# Audit-record kinds that are NOT file-undoable — verb-mediated lifecycle ops
# whose reversibility is anneal's, not this layer's. Mirrors writes._VERB_KINDS
# EXACTLY (kept here so the pure layer stays decoupled from the write module — a
# drift would make the edits panel offer [u]ndo on a row the server would 400).
_VERB_MEDIATED_KINDS = frozenset(
    {
        "spore_touch", "spore_descend", "spore_ascend", "episode_tombstone",
        # the Slice-3b operator-I/O kinds (capture / re-route / schedule / metadata
        # edit) — verb-mediated, anneal-owned reversibility, never file-undoable.
        "spore_seed", "spore_set_disposition", "spore_surface_at", "spore_update",
    }
)

# Sentinel for "argument not supplied" where None is a meaningful value. Used by
# build_set_disposition_req: surface_at=None means "clear the alarm" (a real wire
# value), distinct from "don't touch surface_at at all" (omit the key).
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# The pure view-model. Navigation + selection state over an immutable view; the
# reducers return a NEW model (no in-place mutation → trivially testable). The
# curses driver owns the only mutable thing (the screen) and the side effects
# ($EDITOR, apply_edit); everything here is a pure function of (model, input).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TuiModel:
    """Immutable navigation state over a ``SubstrateView``.

    ``zone_idx`` selects the active zone (index into ``ZONES``); ``panel_idx`` the
    panel within that zone; ``item_idx`` the row within a list panel (Class-B verbs
    target it); ``scroll`` the detail viewport offset for a text panel. ``status``
    is the transient message line (a write result / refusal / hint)."""

    view: SubstrateView
    zone_idx: int = 0
    panel_idx: int = 0
    item_idx: int = 0
    scroll: int = 0
    status: str = ""
    read_only: bool = False  # inspect-only: suppress ALL write affordances (NO THEATER)

    @property
    def zone(self) -> str:
        return ZONES[self.zone_idx][0]

    def panels(self) -> list[dict[str, Any]]:
        return panels_for_zone(self.view, self.zone)

    def current_panel(self) -> dict[str, Any] | None:
        panels = self.panels()
        if not panels:
            return None
        return panels[min(self.panel_idx, len(panels) - 1)]


def panels_for_zone(view: SubstrateView, zone: str) -> list[dict[str, Any]]:
    """The layout() panels for one zone, in declared order. The manifest is the
    single source of which panels exist + their edit-class — the TUI never invents
    a panel, so it cannot drift from the web surface or from what is editable."""
    return [p for p in view.layout() if p.get("zone") == zone]


def panel_item_count(view: SubstrateView, panel: dict[str, Any]) -> int:
    """How many SELECTABLE rows a panel has — only the verb-navigable list panels
    (spores/episodes/edits); 0 for a text/scroll panel (incl. crystals/wraps, which
    render rich and scroll but carry no per-item verb)."""
    kind = panel.get("kind")
    if kind == "spores":
        return len(view.open_spores)
    if kind == "tray":
        return len(view.tray)
    if kind == "keep":
        return len(view.keep)
    if kind == "episodes":
        return len(view.episodes)
    if kind == "edits":
        return len(view.recent_edits)
    return 0


def is_item_list(panel: dict[str, Any] | None) -> bool:
    """True if the panel is item-navigable (the cursor selects a row a verb acts
    on). The driver highlights + auto-scrolls to the cursor for these; everything
    else scrolls as one body."""
    return panel is not None and panel.get("kind") in _LIST_KINDS


def is_record_undoable(record: dict[str, Any]) -> bool:
    """True if an audit record can be file-undone — NOT a verb-mediated lifecycle
    op (anneal owns those), NOT an undo-of-an-undo, NOT explicitly ``undoable:
    false``. Mirrors the server's ``_apply_undo`` refusal so the footer advertises
    ``[u]ndo`` only where it will actually land (NO THEATER), instead of offering it
    on every edit-log row and letting the server 400 the dead ones."""
    if record.get("action") == "undo":
        return False
    if record.get("kind") in _VERB_MEDIATED_KINDS:
        return False
    return record.get("undoable") is not False


# ---------------------------------------------------------------------------
# Verb affordances — the metal. Computed from a panel's edit-class + kind so the
# footer advertises ONLY real, applicable, wired verbs (NO THEATER). The pure
# layer names the verbs; the driver collects any extra input ($EDITOR text, a
# resolve-kind choice, a confirm) and calls apply_edit.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verb:
    """A keystroke affordance on the selected panel/item.

    ``key`` is the bound character; ``label`` the footer hint; ``kind`` the
    ``apply_edit`` request kind (or a pseudo-kind the driver interprets — ``edit``
    for the $EDITOR Class-A flow, ``undo`` for the audit-log restore). ``destructive``
    drives the confirm gate; ``needs_kind`` flags a spore resolve verb that must
    choose an anneal-validated kind first. ``row_scoped`` is True for a verb that
    acts on the selected row (touch/edit/descend/…) and False for a PANEL-level verb
    (the Tray/Keep freeform capture) — the panel-level verb is still offered on an
    EMPTY list (you dump INTO an empty Tray), where row-scoped verbs are suppressed."""

    key: str
    label: str
    kind: str
    destructive: bool = False
    needs_kind: bool = False
    row_scoped: bool = True


def panel_verbs(panel: dict[str, Any] | None, *, read_only: bool = False) -> list[Verb]:
    """The verbs available on a panel, by edit-class + kind — the curses peer of the
    web's Slice-3b verb affordances. A Class-C panel offers none (glass). Class-A
    config/section panels offer ``[e]dit``. The Class-B operator-I/O panels:

    - **Open Loops** (spores): touch / edit-text / schedule / park / descend / ascend.
    - **Tray** (operator inbox): new-dump (panel-level) / edit / reclassify / metabolize
      (→ loop) / schedule / dismiss.
    - **Keep** (durable): note-dump (panel-level) / edit / activate (the keep-note
      promote — note→loop / parked-loop→un-park) / →tray / remind / drop. The note-only
      verbs (→tray, remind) are pruned for a parked LOOP row by ``_active_verbs`` — this
      returns the panel SUPERSET (it has no row), so the driver does the row shaping.

    Episodes offer tombstone; the edits panel offers ``[u]ndo`` of a file-undoable
    record. Returns ``[]`` for a panel that affords nothing — the footer then shows
    navigation only.

    ``read_only`` is the inspect-only view mode (``levain tui --read-only``, and the
    flow self-ops cockpit that points this kernel at a substrate with no ``.levain``
    install): it suppresses EVERY affordance so the footer advertises navigation
    only (NO THEATER — a write verb is never shown when there is no governed write
    target behind it). The data layer is always read-only; this is the UI mode that
    matches it for an inspection surface."""
    if panel is None or read_only:
        return []
    kind = panel.get("kind")
    edit_class = panel.get("edit_class")

    # The manifest's edit_class OWNS the affordance — a verb is offered only when
    # BOTH the kind and the declared edit-class agree (so a read-layer regression
    # that mistagged a panel can't leak a mutation key — codex/kimi L3).
    if edit_class == CLASS_A and kind in {"config", "section"}:
        return [Verb("e", "[e]dit", "edit")]
    if kind == "spores" and edit_class == CLASS_B:  # the entity's live cognition loops
        return [
            Verb("t", "[t]ouch", "spore_touch"),
            Verb("e", "[e]dit", "spore_edit"),
            Verb("s", "[s]chedule", "spore_schedule"),
            Verb("p", "[p]ark", "spore_park"),
            Verb("d", "[d]escend", "spore_descend", destructive=True, needs_kind=True),
            Verb("a", "[a]scend", "spore_ascend", destructive=True, needs_kind=True),
        ]
    if kind == "tray" and edit_class == CLASS_B:  # the operator session-I/O inbox
        return [
            Verb("n", "[n]ew", "spore_capture", row_scoped=False),
            Verb("e", "[e]dit", "spore_edit"),
            Verb("c", "re[c]lassify", "spore_reclassify"),
            Verb("m", "[m]etabolize", "spore_metabolize"),
            Verb("s", "[s]chedule", "spore_schedule"),
            Verb("d", "[d]ismiss", "spore_descend", destructive=True, needs_kind=True),
        ]
    if kind == "keep" and edit_class == CLASS_B:  # durable reference (notes + parked loops)
        # The full superset; _active_verbs prunes the note-only promote verbs for a
        # parked-loop row (NO THEATER per the SELECTED row). `activate` is row-dispatched
        # in the driver: a note → set_disposition{loop}, a parked loop → un-park (tier=warm).
        return [
            Verb("n", "[n]ote", "spore_capture", row_scoped=False),
            Verb("e", "[e]dit", "spore_edit"),
            Verb("m", "[m] activate", "keep_activate"),
            Verb("y", "→ tra[y]", "spore_to_tray"),
            Verb("s", "remind [s]", "keep_remind"),
            Verb("d", "[d]rop", "spore_descend", destructive=True, needs_kind=True),
        ]
    if kind == "episodes" and edit_class == CLASS_B:
        return [Verb("x", "[x] tombstone", "episode_tombstone", destructive=True)]
    if kind == "edits":  # the audit log carries no edit-class chip; undo is its verb
        return [Verb("u", "[u]ndo", "undo")]
    return []


# ---------------------------------------------------------------------------
# Request shapers — pure builders for the apply_edit dicts. Trivial, but keeping
# them pure means the request CONTRACT is unit-tested independently of curses.
# (The Class-A `edit` request is built in the driver because it needs the live
# section body for `expected_body` + the $EDITOR result for `new_body`.)
# ---------------------------------------------------------------------------

def build_touch_req(spore_id: str) -> dict[str, Any]:
    return {"kind": "spore_touch", "spore_id": spore_id}


def build_descend_req(spore_id: str, spore_kind: str) -> dict[str, Any]:
    return {
        "kind": "spore_descend",
        "spore_id": spore_id,
        "spore_kind": spore_kind,
        "confirm": True,
    }


def build_ascend_req(spore_id: str, spore_kind: str, ref: str) -> dict[str, Any]:
    return {
        "kind": "spore_ascend",
        "spore_id": spore_id,
        "spore_kind": spore_kind,
        "ref": ref,
        "confirm": True,
    }


def build_tombstone_req(episode_id: str) -> dict[str, Any]:
    return {"kind": "episode_tombstone", "episode_id": episode_id, "confirm": True}


def build_undo_req(edit_id: str) -> dict[str, Any]:
    return {"kind": "undo", "edit_id": edit_id}


# --- Slice-3b operator-I/O builders (the curses peer of the web's dump/sort verbs) ---

def build_seed_req(text: str, disposition: str) -> dict[str, Any]:
    """A freeform DUMP → a Tray ``seed`` (or a Keep ``note``). The "human dumps, AI
    sorts" capture: the server (``_apply_spore_seed``) requires a NON_COGNITION
    disposition so a raw dump is born cognition-excluded — never pollutes salience."""
    return {"kind": "spore_seed", "text": text, "disposition": disposition}


def build_set_disposition_req(
    spore_id: str, disposition: str, *, surface_at: Any = _UNSET
) -> dict[str, Any]:
    """Re-route a spore's disposition (metabolize Tray→loop, promote a Keep note
    →loop/→tray). ``surface_at`` is OMITTED unless supplied — present (a ``YYYY-MM-DD``)
    rides the re-route atomically (the keep-note "remind me": note→seed + schedule in
    one CAS); ``None`` would mean an explicit clear. Absent ⇒ leave ``next`` untouched."""
    req: dict[str, Any] = {
        "kind": "spore_set_disposition", "spore_id": spore_id, "disposition": disposition,
    }
    if surface_at is not _UNSET:
        req["surface_at"] = surface_at
    return req


def build_surface_at_req(spore_id: str, surface_at: str | None) -> dict[str, Any]:
    """Schedule a loop/Tray item to resurface at a future session-open. ``None``/``''``
    clears the alarm; a ``YYYY-MM-DD`` sets it (the server re-validates the date)."""
    return {"kind": "spore_surface_at", "spore_id": spore_id, "surface_at": surface_at}


def build_spore_update_req(
    spore_id: str, *, text: str | None = None,
    spore_type: str | None = None, tier: str | None = None,
) -> dict[str, Any]:
    """The forming-workbench metadata edit: refine ``text``, reclassify ``type`` (only
    while a Tray item is forming — locked once it metabolizes), or re-``tier`` (park a
    loop → ``parked``, un-park a Keep loop → ``warm``). At least one field; all
    non-destructive (reversible by re-edit) → no confirm. ``type`` is sent under the
    wire key ``type`` (named ``spore_type`` here to avoid shadowing the builtin)."""
    req: dict[str, Any] = {"kind": "spore_update", "spore_id": spore_id}
    if text is not None:
        req["text"] = text
    if spore_type is not None:
        req["type"] = spore_type
    if tier is not None:
        req["tier"] = tier
    return req


def build_config_edit_req(
    *, source: str, heading: str | None, expected_body: str, new_body: str
) -> dict[str, Any]:
    """A Class-A config/section file edit. ``state`` (the neocortex State section)
    and ``config`` (world.md sections / posture / recency) share the load→$EDITOR→
    save round-trip; the only difference is the ``kind`` the dispatcher routes on,
    derived from the source by the caller (the continuity file → ``state``)."""
    return {
        "kind": "config",
        "source": source,
        "heading": heading,
        "expected_body": expected_body,
        "new_body": new_body,
    }


def build_state_edit_req(
    *, expected_body: str, new_body: str, heading: str = "State"
) -> dict[str, Any]:
    """A neocortex-section write. ``heading`` is DATA, not a constant — the caller
    threads the actual section heading so that if a second Class-A section is ever
    added, the request targets the right section (the server re-validates the
    edit-class regardless). Defaults to ``State``, today's lone Class-A section."""
    return {
        "kind": "state",
        "heading": heading,
        "expected_body": expected_body,
        "new_body": new_body,
    }


def edit_error_status(code: str, message: str) -> str:
    """Map an ``EditError`` to a one-line status message. ``stale`` (409) is the
    one the operator most needs framed as an action, not a fault — the substrate
    moved under them (a wrap, or a concurrent edit), so the move is reload."""
    if code == "stale":
        return "⚠ stale — the substrate moved under you (a wrap or another edit); reloaded"
    if code == "confirm_required":
        return f"⚠ {message}"
    if code == "not_editable":
        return "⚠ read-only — that surface is the entity's cognition, not an operator input"
    if code == "lock_unavailable":
        return "⚠ continuity lock unavailable (lock-less FS) — refused to edit State unserialized"
    return f"⚠ {code}: {message}"


# ---------------------------------------------------------------------------
# Detail rendering — pure: (view, panel) → list[str]. One renderer per kind,
# adapted from dashboard.render_text. The curses driver truncates to the pane
# width and scrolls; these just produce the logical lines.
# ---------------------------------------------------------------------------

def render_panel_lines(view: SubstrateView, panel: dict[str, Any]) -> list[str]:
    """The detail body for one panel, as logical (un-truncated) lines."""
    kind = panel.get("kind")
    if kind == "config":
        return _render_config(view, panel)
    if kind == "section":
        return _render_section(view, panel)
    if kind == "spores":
        return _render_spores(view)
    if kind == "tray":
        return _render_tray(view)
    if kind == "keep":
        return _render_keep(view)
    if kind == "episodes":
        return _render_episodes(view)
    if kind == "edits":
        return _render_edits(view)
    if kind == "health":
        return _render_health(view)
    if kind == "graph":
        return _render_cognition_trace(view)
    if kind == "crystals":
        return _render_crystals(view)
    if kind == "wraps":
        return _render_wraps(view)
    return ["(no renderer for this panel)"]


def _render_config(view: SubstrateView, panel: dict[str, Any]) -> list[str]:
    ref = panel.get("ref")
    if not isinstance(ref, int) or ref >= len(view.config_docs):
        return ["(config doc unavailable)"]
    d = view.config_docs[ref]
    head = f"{d.source}" + (f"  § {d.heading}" if d.heading else "")
    lines = [head, f"edit-class {d.edit_class}", ""]
    lines.extend(d.body.splitlines() or ["(empty)"])
    return lines


def _render_section(view: SubstrateView, panel: dict[str, Any]) -> list[str]:
    ref = panel.get("ref")
    if not isinstance(ref, int) or ref >= len(view.sections):
        return ["(section unavailable)"]
    s = view.sections[ref]
    cls = "A — operator-editable live-state" if s.edit_class == CLASS_A else "C — consolidated cognition (read-only)"
    lines = [f"{s.heading}", f"edit-class {cls}", ""]
    lines.extend(s.body.splitlines() or ["(empty)"])
    return lines


def _render_spores(view: SubstrateView) -> list[str]:
    """ONE line per loop (the item cursor maps 1:1 to a line). The resolve-kinds
    surface in the descend/ascend picker at verb time, not the scan view."""
    if not view.open_spores:
        return ["(no open loops)"]
    return [
        f"[{s.tier}·{s.salience}] {_oneline(s.text)}"
        + (f" → {_oneline(s.next)}" if s.next else "")
        + f"  · {s.id}"
        for s in view.open_spores
    ]


def _render_tray(view: SubstrateView) -> list[str]:
    """ONE line per Tray item — the operator session-I/O inbox, badged by disposition
    (seed/handoff/agenda). Read-only display in 3a (the governed dump/sort verbs are 3b)."""
    if not view.tray:
        return ["(tray clear)"]
    return [
        f"[{s.disposition}] {_oneline(s.text)}"
        + (f" → surface {_oneline(s.next)}" if s.next else "")
        + f"  · {s.id}"
        for s in view.tray
    ]


def _render_keep(view: SubstrateView) -> list[str]:
    """ONE line per Keep item — durable pinned-dormant loops (the parked tier), exempt
    from the dormancy→compost prompt. Read-only display in 3a."""
    if not view.keep:
        return ["(keep empty)"]
    return [f"[{s.tier}] {_oneline(s.text)}  · {s.id}" for s in view.keep]


def _render_episodes(view: SubstrateView) -> list[str]:
    """ONE line per episode (the item cursor maps 1:1 to a line)."""
    if not view.episodes:
        return ["(no recent episodes)"]
    out: list[str] = []
    for e in view.episodes:
        stamp = e.timestamp.split("T")[0] if e.timestamp else ""
        out.append(f"[{e.type}] {_oneline(e.content)}  · {stamp} · {e.id}")
    return out


def _render_edits(view: SubstrateView) -> list[str]:
    """ONE line per audit record (the item cursor maps 1:1 to a line)."""
    if not view.recent_edits:
        return ["(no edits yet)"]
    out: list[str] = []
    for r in view.recent_edits:
        ts = str(r.get("ts", "")).split("T")[0]
        kind = r.get("kind", "?")
        action = r.get("action", "?")
        src = _oneline(str(r.get("source", "?")))
        flag = "" if is_record_undoable(r) else "  (not file-undoable)"
        out.append(f"{ts}  {kind}/{action}  {src}  · {r.get('id', '?')}{flag}")
    return out


def _render_health(view: SubstrateView) -> list[str]:
    h = view.health
    if h is None:
        msg = view.errors.get("store", "unavailable")
        return [f"(health unavailable — {msg})"]
    lines: list[str] = []
    if h.write_path_live:
        lines.append(
            f"Write-path:  LIVE — {h.total_links} links "
            f"(avg {h.avg_strength:.2f}, max {h.max_strength:.2f})"
        )
    else:
        lines.append("Write-path:  DARK — 0 associations (a graduated wrap should form links)")
    lines.append(
        f"Graduations: {h.graduations_validated_total} validated / "
        f"{h.graduations_demoted_total} demoted"
    )
    by_type = ", ".join(f"{k} {v}" for k, v in sorted(h.episodes_by_type.items()))
    lines.append(
        f"Episodes:    {h.total_episodes:,} ({h.episodes_since_wrap} since wrap)"
        + (f" · {by_type}" if by_type else "")
    )
    chars = f"{h.continuity_chars:,} chars" if h.continuity_chars is not None else "not yet created"
    last = h.last_wrap_at.split("T")[0] if h.last_wrap_at else "never"
    lines.append(f"Continuity:  {chars} · {h.total_wraps} wraps · last {last}")
    if h.tombstones:
        lines.append(f"Tombstones:  {h.tombstones}")
    if h.wrap_in_progress:
        lines.append("⚠ wrap in progress — snapshot may be momentarily inconsistent")
    return lines


def _render_cognition_trace(view: SubstrateView) -> list[str]:
    """The Mind-zone 'Cognition trace' panel: the graph stats headline + the real
    per-wrap vitals (the no-theater oscilloscope — true graduation/link counts per
    wrap, never a decorative hairball)."""
    lines: list[str] = []
    if view.graph is not None:
        g = view.graph
        suffix = " (capped for display)" if g.truncated else ""
        lines.append(f"Association graph: {len(g.nodes)} nodes, {len(g.edges)} edges{suffix}")
    else:
        lines.append("Association graph: unavailable")
    lines.append("")
    if view.wraps:
        lines.append("Per-wrap vitals (newest first):")
        for w in view.wraps[:20]:
            stamp = w.wrapped_at.split("T")[0] if w.wrapped_at else ""
            lines.append(
                f"  {stamp}  {w.graduations_validated}↑/{w.graduations_demoted}↓ grad  "
                f"+{w.associations_formed}/~{w.associations_strengthened}/-{w.associations_decayed} links  "
                f"{w.continuity_chars:,} chars"
            )
    else:
        lines.append("(no wraps yet — the trace fills as the entity consolidates)")
    return lines


def _render_crystals(view: SubstrateView) -> list[str]:
    if not view.crystal_index:
        return ["(no crystallized patterns)"]
    return [f"{c.name} ({c.level}x) — {c.one_clause}" for c in view.crystal_index]


def _render_wraps(view: SubstrateView) -> list[str]:
    if not view.wraps:
        return ["(no wraps yet)"]
    lines = [
        "Each wrap is a RE-PROJECTION of the continuity from the moving substrate —",
        "not a saved version. There is no restore: you view-as-of and re-project.",
        "",
    ]
    for w in view.wraps:
        stamp = w.wrapped_at.split("T")[0] if w.wrapped_at else ""
        lines.append(
            f"{stamp}  {w.episodes_compressed} eps · "
            f"{w.graduations_validated}↑/{w.graduations_demoted}↓ grad · "
            f"+{w.associations_formed} links · {w.continuity_chars:,} chars"
        )
    return lines


# ---------------------------------------------------------------------------
# Navigation reducers — pure (model, *) → model. The driver maps keys to these.
# ---------------------------------------------------------------------------

def select_zone(model: TuiModel, zone_idx: int) -> TuiModel:
    zone_idx = max(0, min(zone_idx, len(ZONES) - 1))
    return replace(model, zone_idx=zone_idx, panel_idx=0, item_idx=0, scroll=0, status="")


def select_panel(model: TuiModel, delta: int) -> TuiModel:
    panels = model.panels()
    if not panels:
        return model
    new_idx = max(0, min(model.panel_idx + delta, len(panels) - 1))
    return replace(model, panel_idx=new_idx, item_idx=0, scroll=0)


def move_in_panel(model: TuiModel, delta: int) -> TuiModel:
    """``j``/``k`` move the item cursor in a list panel, else scroll the detail."""
    panel = model.current_panel()
    if panel is None:
        return model
    if panel.get("kind") in _LIST_KINDS:
        count = panel_item_count(model.view, panel)
        if count == 0:
            return model
        new_item = max(0, min(model.item_idx + delta, count - 1))
        return replace(model, item_idx=new_item)
    new_scroll = max(0, model.scroll + delta)
    return replace(model, scroll=new_scroll)


def with_view(model: TuiModel, view: SubstrateView, status: str = "") -> TuiModel:
    """Re-seat the model on a freshly rebuilt view (after a write / refresh),
    clamping the cursors so they stay in range if the data shrank."""
    new = replace(model, view=view, status=status)
    panels = new.panels()
    panel_idx = min(new.panel_idx, max(0, len(panels) - 1))
    new = replace(new, panel_idx=panel_idx)
    panel = new.current_panel()
    count = panel_item_count(view, panel) if panel else 0
    item_idx = min(new.item_idx, max(0, count - 1)) if count else 0
    return replace(new, item_idx=item_idx)


def current_spore(model: TuiModel) -> Any | None:
    """The selected OPEN-LOOP row specifically (the spores panel only). The driver uses
    the broader ``current_row`` (which spans Open Loops / Tray / Keep); ``current_spore``
    remains the narrower public accessor for a spores-panel-only consumer."""
    panel = model.current_panel()
    if panel is None or panel.get("kind") != "spores":
        return None
    if model.item_idx >= len(model.view.open_spores):
        return None
    return model.view.open_spores[model.item_idx]


def current_row(model: TuiModel) -> Any | None:
    """The selected spore-like item for whichever of the THREE spore projections is
    active — Open Loops (``view.open_spores``), Tray (``view.tray``), or Keep
    (``view.keep``). Each maps the item cursor 1:1 onto its own list, so the shared
    lifecycle verbs (touch/edit/descend/disposition/…) act on the right row across all
    three. Returns ``None`` off a spore panel or past the list end."""
    panel = model.current_panel()
    if panel is None:
        return None
    kind = panel.get("kind")
    if not isinstance(kind, str):
        return None
    src = {
        "spores": model.view.open_spores,
        "tray": model.view.tray,
        "keep": model.view.keep,
    }.get(kind)
    if src is None or model.item_idx >= len(src):
        return None
    return src[model.item_idx]


def current_episode(model: TuiModel) -> Any | None:
    panel = model.current_panel()
    if panel is None or panel.get("kind") != "episodes":
        return None
    if model.item_idx >= len(model.view.episodes):
        return None
    return model.view.episodes[model.item_idx]


def current_edit_record(model: TuiModel) -> dict[str, Any] | None:
    panel = model.current_panel()
    if panel is None or panel.get("kind") != "edits":
        return None
    if model.item_idx >= len(model.view.recent_edits):
        return None
    return model.view.recent_edits[model.item_idx]


# ---------------------------------------------------------------------------
# CLI entry — `levain tui`. Resolves the install, guards the tty + the store,
# then hands off to the curses driver (imported lazily so importing this module
# never requires curses, and the pure layer stays importable for the test suite).
# ---------------------------------------------------------------------------

def run_tui(path: Path, *, read_only: bool = False) -> int:
    """``levain tui`` entry point. Read+write control plane in the terminal.

    ``read_only`` runs the inspect-only variant (``--read-only``): every write verb
    is suppressed, so it is a pure inspection surface over the substrate — the mode
    the flow self-ops cockpit (a substrate with no ``.levain`` write target) needs.

    Nonzero only when it cannot start (no store, not a tty); a degraded sub-tier
    renders visibly inside, exactly like ``levain dashboard``."""
    source = SubstrateSource.local(path)
    if not source.anneal.episodic_db.exists():
        print(
            f"No anneal store at {source.anneal.episodic_db}.\n"
            "Run `levain init` in this directory, or pass --path to an install.",
            file=sys.stderr,
        )
        return 1
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print(
            "levain tui needs an interactive terminal. For a non-interactive "
            "glance use `levain dashboard` (or `--json`); for a browser use "
            "`levain serve`.",
            file=sys.stderr,
        )
        return 1

    # Build the first view BEFORE entering curses. build() is fail-soft for
    # data/IO (degraded tiers land in view.errors), but a programming-error class
    # propagates — surfacing that here, on a clean terminal, beats crashing inside
    # the event loop and tearing down the screen mid-frame. (In-loop rebuilds are
    # guarded separately — see _tui_curses._rebuild.)
    try:
        view = source.build()
    except Exception as exc:  # noqa: BLE001 — startup guard: report any fault cleanly
        print(f"Could not assemble the substrate view: {exc}", file=sys.stderr)
        return 1

    from levain import _tui_curses  # lazy: curses + the screen driver

    # The write target is carried on the source (SubstrateSource.local sets write_scope
    # from the install); main_loop derives read-only from write_scope + the flag.
    return _tui_curses.main_loop(source, view, read_only=read_only)
