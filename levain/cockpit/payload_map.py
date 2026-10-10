"""levain.cockpit.payload_map: design §6's "every live field, mapped" table, as data.

§6 says no field is invented and none orphaned: a field is mapped, deliberately dropped with the
reason, or listed unmapped in §6.4. This module is that table in machine form so that
``scripts/cockpit_payload_diff.py`` can diff saved/live payload keys against it.

``literal`` marks the honesty line: True = §6 states this mapping in so many words; False = the
entry is the row-level consequence of a stated mapping that §6 does not spell out (for example
``episodes[].id`` becoming ``Row.id``). A False entry is a proposed amendment to §6, and the diff
report lists them separately, so the table cannot quietly grow past what the design says.

Disposition: ``map`` (the field has a target), ``drop`` (deliberately dropped, reason in
``target``), ``unmapped`` (named in §6.4 as not mapped)."""

from __future__ import annotations

import re

# (payload, pattern, disposition, target or reason, literal)
# Patterns use `[]` for list elements and `*` for one path segment; a trailing `.**` covers a subtree.
TABLE: list[tuple[str, str, str, str, bool]] = []


def _add(payload: str, disposition: str, target: str, literal: bool, *patterns: str) -> None:
    for p in patterns:
        TABLE.append((payload, p, disposition, target, literal))


# --- 6.1 flowConnect /board ---------------------------------------------------------------------
B = "board"
_add(B, "map", "manifest generated_at", True, "generated")
_add(B, "map", "manifest errors entry with the count", True, "skipped")
_add(B, "map", "panel status/error", True, "decisions.ok", "loops.ok", "tray.ok", "keep.ok")
_add(B, "map", "decisions panel rows: title / Row.id / facets repo,seat,stopped / due", True,
     "decisions.items[].what", "decisions.items[].id", "decisions.items[].repo",
     "decisions.items[].seat", "decisions.items[].stopped", "decisions.items[].dated")
_add(B, "map", "at (decisions row)", True, "decisions.items[].ts")
_add(B, "drop", "same instant as ts / fold state", True, "decisions.items[].iso", "decisions.items[].event")
for panel in ("loops", "tray", "keep"):
    _add(B, "map", f"{panel} panel Row.id / title / body / truncated", True,
         f"{panel}.items[].id", f"{panel}.items[].title", f"{panel}.items[].body", f"{panel}.items[].truncated")
    _add(B, "map", f"{panel} row facets of the same name", True,
         *(f"{panel}.items[].{f}" for f in ("disposition", "domain", "tier", "age_days", "due", "overdue_days", "held_until")))
_add(B, "map", "tray filtered: [{count, reason}]", True, "tray.held")

# --- 6.2 Bridge /substrate.json ---------------------------------------------------------------------
S = "substrate"
_add(S, "map", "manifest entity", True, "entity_name", "brand_wordmark", "brand_model")
_add(S, "map", "manifest entity.store_label: a home-relative display label for the masthead's store line, never the "
     "absolute path (Phill 10-09 20:54, option c; honours §6.2)", False, "paths.episodic_db")
_add(S, "drop", "install-private paths", True, "paths.continuity_md", "paths.crystal_json", "paths.spores_json", "scope")
_add(S, "map", "regions.zones[].panels + PanelHead", True,
     "layout[].kind", "layout[].id", "layout[].zone", "layout[].title")
_add(S, "map", "Row.id / title / body / facets (open_spores, tray, keep rows)", True,
     "open_spores[].type", "open_spores[].salience", "open_spores[].text",
     "tray[].type", "tray[].salience", "tray[].text", "keep[].type", "keep[].salience", "keep[].text")
_add(S, "map", "spore row facets / Row.id of the tray, loops and keep panels", False,
     *(f"{b}[].{f}" for b in ("open_spores", "tray", "keep")
       for f in ("id", "tier", "domain", "disposition", "next", "seen", "pointer")))
_add(S, "drop", "derived from the spore type by anneal; verbs and their kinds arrive with K2a's registry", False,
     *(f"{b}[].{f}" for b in ("open_spores", "tray", "keep") for f in ("descend_kinds", "ascend_kinds")))
_add(S, "map", "episodes panel: facets episode_type, source, at", True,
     "episodes[].type", "episodes[].source", "episodes[].timestamp")
_add(S, "map", "episodes row Row.id / title+body / tags", False, "episodes[].id", "episodes[].content", "episodes[].tags")
_add(S, "map", "edits panel: facets edit_kind, at, undoable", True,
     "recent_edits[].kind", "recent_edits[].ts", "recent_edits[].undoable")
_add(S, "map", "edits row Row.id / the audit record, carried whole as the row's stored fields", False,
     "recent_edits[].id", "recent_edits[].action", "recent_edits[].source", "recent_edits[].heading",
     "recent_edits[].text", "recent_edits[].type", "recent_edits[].disposition",
     "recent_edits[].prior_disposition", "recent_edits[].surface_at", "recent_edits[].fields",
     "recent_edits[].verb_kind", "recent_edits[].backup", "recent_edits[].prev_sha256",
     "recent_edits[].new_sha256", "recent_edits[].prev_len", "recent_edits[].new_len", "recent_edits[].undid",
     "recent_edits[].restored_to")
_add(S, "map", "crystals panel facets crystal_level, permanence, last_activated_on, tags", True,
     "crystal_index[].level", "crystal_index[].permanence", "crystal_index[].last_activated_on",
     "crystal_index[].tags")
_add(S, "map", "crystals row Row.id / title / body", False,
     "crystal_index[].name", "crystal_index[].one_clause", "crystal_index[].activation_mode")
_add(S, "map", "metric health (each counter a metric; write_path_live a status)", True, "health.**")
_add(S, "map", "visual cognition-graph data + text projection", True, "graph.**")
_add(S, "map", "visual wrap-history data + text projection", True, "wraps[].*")
_add(S, "map", "prose panels, one per heading, action section_edit", True,
     "sections[].heading", "sections[].body")
_add(S, "map", "prose panel placement / edit tier (zone and edit_class become region and tier)", False,
     "sections[].zone", "sections[].edit_class")
_add(S, "map", "prose panels, action config", True, "config_docs[].*")
_add(S, "drop", "edit_class becomes the verb's tier on VerbHead (K2a); ref is a positional index replaced "
     "by the panel/row ids; source and heading are the section_edit / config write address, carried by "
     "those verbs' fields. §6.2 maps only layout[].kind/id/zone/title: this is a gap the diff found", False,
     "layout[].edit_class", "layout[].ref", "layout[].source", "layout[].heading")
_add(S, "map", "line focus in header", True, "focus.text", "focus.set_at", "focus.source", "focus.stale",
     "focus.freshness")
_add(S, "drop", "the renderer formats age", True, "focus.age_label")
_add(S, "map", "manifest errors", True, "errors", "errors.*")
_add(S, "map", "manifest credential + each VerbHead.gesture", True, "writable", "write_token_required")
_add(S, "map", "manifest verbs", True, "action_verbs.**")
_add(S, "map", "PanelHead note / empty / error", True,
     "extra_panels.*.note", "extra_panels.*.empty", "extra_panels.*.error")
_add(S, "map", "facets (meta decomposed) / title+body / emphasis", True,
     "extra_panels.*.lines[].meta", "extra_panels.*.lines[].text", "extra_panels.*.lines[].accent",
     "extra_panels.*.lines[].dim")
_add(S, "map", "prose panel", True, "extra_panels.*.markdown")
_add(S, "map", "PanelAction + VerbHead.fields", True, "extra_panels.*.action.**")
_add(S, "map", "placement of each extra panel in the layout", False, "extra_panels")
# The Bridge's current installed levain predates these; they are on levain main (SubstrateView).
_add(S, "map", "manifest entity.jar (the masthead starter jar), a K1 session-2 amendment to §6.2", False, "jar.**")
_add(S, "map", "line state in header (the panel `state` exists, like focus); §6.2 does not spell this out, "
     "a K1 session-2 amendment", False, "state.text", "state.set_at", "state.source")
_add(S, "drop", "the renderer formats age (as for focus.age_label)", False, "state.age_label")

# --- 6.3 /header.json, /sky.json, /fleet.json ------------------------------------------------------
H = "header"
_add(H, "map", "lines focus, state in header (from the source keys, not the display text)", True,
     "focus", "state")
_add(H, "map", "metric weather (stale or error with that message)", True, "weather", "error")

K = "sky"
_add(K, "map", "visual sky: ok -> status, stars/heads -> data", True,
     "ok", "stars[].*", "stars", "heads.*")
_add(K, "drop", "the render time, not a read time", True, "now")

F = "fleet"
_add(F, "drop", "the date of generated_at", True, "today")
_add(F, "map", "constellation panel note", True, "note")
_add(F, "map", "visual constellation + one manifest per entity", True, "entities[].name", "entities[].governance")
_add(F, "map", "that entity's metric health", True, "entities[].card.health.*")
_add(F, "map", "line focus", True, "entities[].card.state_focus")
_add(F, "map", "metric panel with six counts (string deleted)", True, "entities[].card.spore_summary")
_add(F, "map", "triage-lists", True, "entities[].card.attention_spores", "entities[].card.inbox_items")
_add(F, "map", "metric inbox", True, "entities[].card.inbox_line")
_add(F, "map", "line overnight", True, "entities[].card.overnight")
_add(F, "map", "Alerts on the health metric panel", True, "entities[].card.flags")
_add(F, "drop", "a string derived from the flags", True, "entities[].card.action")
_add(F, "map", "per-panel status partial", True, "entities[].card.partial")
_add(F, "map", "metric", True, "entities[].card.store_bytes")
_add(F, "map", "metrics", True, "entities[].card.signals[].*")
_add(F, "map", "line", True, "entities[].plate.substrate")
_add(F, "map", "metrics (read = Metric.read)", True, "entities[].plate.dims[].*")
_add(F, "map", "metrics, LIVE/DARK -> ok/bad", True, "entities[].plate.receipt_faces.*")
_add(F, "map", "triage-list, facet exposure_count, order exposure.desc", True, "entities[].plate.top_exposed[].*")

# Keyed maps: children are DATA (a repo name, a panel id, a verb name, an episode type), not schema.
KEYED: dict[str, tuple[str, ...]] = {
    "substrate": ("extra_panels", "action_verbs", "health.episodes_by_type", "errors"),
    "sky": ("heads",),
    "fleet": (),
    "board": (),
    "header": (),
}


def normalize(payload: str, path: str) -> str:
    """Replace the child segment of every keyed-map prefix by ``*``."""
    out = path
    for prefix in KEYED.get(payload, ()):
        out = re.sub(rf"^({re.escape(prefix)})\.[^.\[\]]+", r"\1.*", out)
    return out


def _regex(pattern: str) -> re.Pattern[str]:
    esc = re.escape(pattern).replace(r"\*\*", ".+").replace(r"\*", r"[^.\[\]]+")
    return re.compile(rf"^{esc}$")


def classify(payload: str, path: str) -> tuple[str, str, bool] | None:
    """The table entry covering ``path`` (disposition, target, literal), or None. A container node
    (a dict or list whose children are mapped) is covered when any pattern lies beneath it."""
    norm = normalize(payload, path)
    rows = [(r, _regex(r[1])) for r in TABLE if r[0] == payload]
    for r, rx in rows:
        if rx.match(norm):
            return r[2], r[3], r[4]
    for r, _rx in rows:
        if r[1].startswith(norm + ".") or r[1].startswith(norm + "[]"):
            return "map", "container of mapped fields", True
    return None
