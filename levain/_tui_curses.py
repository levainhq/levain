"""levain._tui_curses — the curses DRIVER for ``levain tui``.

Thin by design: it reads keys, calls the pure reducers in ``levain.tui``, and
paints the resulting model. It holds the only mutable things — the screen, and
the side effects: the ``$EDITOR`` shell-out, the confirm/prompt/choose modals, the
``writes.apply_edit`` calls, and the view rebuild after a write. All
navigation/render/verb-shaping LOGIC lives in ``levain.tui`` and is unit-tested
there; this module is exercised by the live L4 canary because curses needs a real
terminal.

The locked materiality doctrine is painted with terminal attributes (no color
pairs → portable, dependency-free): a Class-C panel renders ``A_DIM`` (glass,
read-only), a Class-A/B panel renders ``A_BOLD`` (metal, carries verbs), and the
selected panel/row renders ``A_REVERSE``. NO THEATER: the footer advertises only
the verbs the selected panel+row actually affords (``_active_verbs``), and only
those keys are dispatchable — an unbound key does nothing. (The ``[A]/[B]/[C]``
chip is the un-fakeable signal; on a terminal whose ``A_DIM`` is a no-op the chip,
not the dimming, carries the read-only signal.)

Governance is NOT re-implemented here: every write goes through the same
``writes.apply_edit`` the web ``POST /edit`` handler calls, so the Class-A
allowlist, the server-side ``confirm`` gate, the ``require=True`` continuity lock,
and the stale-check enforce identically. The modals are UX over enforcement the
substrate owns (``structural_invariants_beat_discipline``).
"""

from __future__ import annotations

import curses
import os
import shlex
import subprocess
import tempfile
from dataclasses import replace

from levain.dashboard import CLASS_A, CLASS_B, CLASS_C, SubstrateSource, SubstrateView
from levain.tui import (
    ZONES,
    TuiModel,
    Verb,
    build_ascend_req,
    build_config_edit_req,
    build_descend_req,
    build_seed_req,
    build_set_disposition_req,
    build_spore_update_req,
    build_state_edit_req,
    build_surface_at_req,
    build_tombstone_req,
    build_touch_req,
    build_undo_req,
    current_edit_record,
    current_episode,
    current_row,
    edit_error_status,
    is_item_list,
    is_record_undoable,
    move_in_panel,
    panel_item_count,
    panel_verbs,
    render_panel_lines,
    select_panel,
    select_zone,
    with_view,
)
from levain.writes import EditError, apply_edit

_MIN_H = 10
_MIN_W = 50
_LEFT_W = 30  # panel-list column width (clamped to a third of the screen)


def main_loop(
    source: SubstrateSource,
    view: SubstrateView,
    *,
    read_only: bool = False,
) -> int:
    """Run the curses event loop under ``curses.wrapper`` (which guarantees the
    terminal is restored on any exit). The first view is built by the caller,
    pre-curses, so a startup fault never tears down the screen mid-frame.

    The write target is ``source.write_scope`` (the explicit governed write surface).
    ``read_only`` forces the inspect-only variant; a source with ``write_scope is None``
    is read-only too. Either way ``_active_verbs`` advertises + dispatches nothing and
    ``_apply`` refuses (the un-bypassable chokepoint) — a pure inspection surface."""
    return curses.wrapper(_loop, source, view, read_only)


def _loop(
    stdscr: "curses.window", source: SubstrateSource,
    view: SubstrateView, read_only: bool = False,
) -> int:
    curses.curs_set(0)
    stdscr.keypad(True)
    # A substrate with no write scope is read-only EVEN IF the caller didn't pass
    # read_only=True (NO THEATER: never advertise a verb nothing can honor). Fold the
    # two into the model's read_only so the footer, dispatch, and _apply all agree.
    effective_read_only = read_only or source.write_scope is None
    hint = "read-only cockpit · press ? for help" if effective_read_only else "press ? for help"
    model = TuiModel(view=view, status=hint, read_only=effective_read_only)
    show_help = False

    while True:
        if show_help:
            _paint_help(stdscr)
        else:
            model = _paint(stdscr, model)
        ch = stdscr.getch()

        # A terminal resize repaints against the new size (help or main, whichever
        # is showing) — never dismisses help, never paints a stale frame.
        if ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
            continue
        if show_help:
            show_help = False  # any other key dismisses help
            continue

        page = _body_height(stdscr)
        if ch in (ord("q"), 27):  # q / ESC  (also: getch()==-1 falls through → no-op)
            return 0
        elif ch == ord("?"):
            show_help = True
        elif ch in (ord("1"), ord("2"), ord("3")):
            model = select_zone(model, ch - ord("1"))
        elif ch == ord("\t"):
            model = select_zone(model, (model.zone_idx + 1) % len(ZONES))
        elif ch in (ord("h"), curses.KEY_LEFT, ord("[")):
            model = select_panel(model, -1)
        elif ch in (ord("l"), curses.KEY_RIGHT, ord("]")):
            model = select_panel(model, +1)
        elif ch in (ord("j"), curses.KEY_DOWN):
            model = move_in_panel(model, +1)
        elif ch in (ord("k"), curses.KEY_UP):
            model = move_in_panel(model, -1)
        elif ch == curses.KEY_NPAGE:
            model = move_in_panel(model, +page)
        elif ch == curses.KEY_PPAGE:
            model = move_in_panel(model, -page)
        elif ch == ord("g"):
            model = move_in_panel(model, -10_000)
        elif ch == ord("G"):
            model = move_in_panel(model, +10_000)
        elif ch == ord("r"):
            model = _rebuild(model, source, "refreshed")
        else:
            # Verb keys — fire only if the SELECTED panel+row affords that verb
            # (the same set the footer advertised). An unbound key does nothing.
            verbs = {v.key: v for v in _active_verbs(model)}
            if 0 <= ch < 256 and chr(ch) in verbs:
                model = _handle_verb(stdscr, model, source, verbs[chr(ch)])


def _body_height(stdscr: "curses.window") -> int:
    h, _ = stdscr.getmaxyx()
    return max(1, h - 5)  # rows 0-1 header/tabs, 2 rule, h-2 status, h-1 footer


def _active_verbs(model: TuiModel) -> list[Verb]:
    """The verbs the SELECTED panel+row actually affords. Row-aware where the row
    changes the affordance (NO THEATER — never advertise a key the selected row can't
    honor):
    - **edits** — ``[u]ndo`` only on a file-undoable record (mirrors the server refusal).
    - **keep** — the note-promote verbs (→ tray / remind) apply ONLY to a Keep NOTE; a
      pinned-dormant LOOP keeps ``activate`` (which row-dispatches to un-park) but not those.
    - **spores/tray/keep** — a resolve verb is advertised only when the SELECTED row's
      type actually yields that kind (``descend_kinds`` / ``ascend_kinds``), mirroring the
      web's ``Array.isArray(...) && .length`` gate; the Keep note-promote verbs (→ tray /
      remind) apply ONLY to a Keep NOTE; a pinned-dormant LOOP keeps ``activate`` (which
      row-dispatches to un-park) but not those.
    - **empty list** — the row-scoped verbs vanish, but a PANEL-level verb (the Tray/Keep
      freeform capture, ``row_scoped=False``) stays: you dump INTO an empty Tray.
    A read-only model suppresses everything (``panel_verbs(read_only=True)`` → ``[]``)."""
    sel = model.current_panel()
    verbs = panel_verbs(sel, read_only=model.read_only)
    if sel is None or not is_item_list(sel):
        return verbs  # config/section/health/… aren't row lists — their verbs stand as-is
    kind = sel.get("kind")
    if panel_item_count(model.view, sel) == 0:
        return [v for v in verbs if not v.row_scoped]  # only panel-level (capture) survives
    if kind == "edits":
        rec = current_edit_record(model)
        if rec is None or not is_record_undoable(rec):
            verbs = [v for v in verbs if v.kind != "undo"]
        return verbs
    if kind == "episodes":
        # episode_tombstone acts on the selected EPISODE — a DIFFERENT row source than the
        # spore projections below (`current_row` covers only spores/tray/keep). Keep its verb
        # when a row is selected; the empty case already returned above (codex L3).
        return verbs if current_episode(model) is not None else []
    # the three spore projections — prune by the SELECTED row so the footer never advertises
    # a key the row can't honor (the NO-THEATER contract this function exists to keep).
    row = current_row(model)
    if row is None:
        return [v for v in verbs if not v.row_scoped]
    if not getattr(row, "descend_kinds", None):
        verbs = [v for v in verbs if v.kind != "spore_descend"]
    if not getattr(row, "ascend_kinds", None):
        verbs = [v for v in verbs if v.kind != "spore_ascend"]
    if kind == "keep" and getattr(row, "disposition", None) != "note":
        verbs = [v for v in verbs if v.kind not in ("spore_to_tray", "keep_remind")]
    return verbs


def _rebuild(model: TuiModel, source: SubstrateSource, status: str) -> TuiModel:
    """Rebuild the substrate view, keeping the last-good view on failure. build()
    is fail-soft for data/IO (degraded tiers land in view.errors), but a
    programming-error class propagates — and neither a refresh keystroke nor a
    post-write rebuild may crash the session, so catch broadly and surface it."""
    try:
        return with_view(model, source.build(), status=status)
    except Exception as exc:  # noqa: BLE001 — UI liveness: never crash on a rebuild
        return replace(model, status=f"⚠ view refresh failed ({exc}) — showing last view")


# ---------------------------------------------------------------------------
# Verb handlers — collect any required input (a confirm, a resolve-kind, a ref,
# an $EDITOR session), build the request via the pure shapers, and apply it. On
# success or a stale 409 the view is rebuilt; any other refusal shows a status.
# ---------------------------------------------------------------------------

def _handle_verb(
    stdscr: "curses.window", model: TuiModel,
    source: SubstrateSource, verb: Verb,
) -> TuiModel:
    kind = verb.kind
    if kind == "edit":
        return _handle_edit(stdscr, model, source)

    # The spore lifecycle verbs act on the SELECTED row of whichever spore projection is
    # active (Open Loops / Tray / Keep) — current_row dispatches to the right list.
    if kind == "spore_touch":
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        return _apply(model, source, build_touch_req(s.id), f"touched {s.id}")

    if kind == "spore_descend":
        # One verb, three faces by panel (parity with the web): Open Loops "compost" +
        # Tray "dismiss" both pick a resolve kind; Keep "remove" auto-picks 'composted'
        # (else the first kind) with a single confirm and NO picker (web openRemoveConfirm).
        # KNOWN CAS GAP (codex L3, spore-173): the kind/face is chosen off the SNAPSHOT row;
        # the server descend has no expect_disposition CAS (unlike ascend's AM-SPORE-CAS), so
        # a cross-process re-route in the confirm window can resolve a now-live loop. PRE-
        # EXISTING + parity-equal with the web; the fix is the anneal-side descend CAS (spore-173).
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        if not s.descend_kinds:
            return replace(model, status="⚠ no resolve kinds for this item's type")
        pk = (model.current_panel() or {}).get("kind")
        if pk == "keep":
            chosen = "composted" if "composted" in s.descend_kinds else s.descend_kinds[0]
            if not _confirm(stdscr, f"remove {s.id} from Keep (as '{chosen}')? "
                                    "(recoverable in anneal's resolved set)"):
                return replace(model, status="cancelled")
            return _apply(model, source, build_descend_req(s.id, chosen), f"removed {s.id}")
        word, past = ("dismiss", "dismissed") if pk == "tray" else ("compost", "composted")
        chosen = _choose(stdscr, f"{word} {s.id} as:", list(s.descend_kinds))
        if chosen is None:
            return replace(model, status="cancelled")
        if not _confirm(stdscr, f"{word} {s.id} as '{chosen}'? (recoverable in anneal's resolved set)"):
            return replace(model, status="cancelled")
        return _apply(model, source, build_descend_req(s.id, chosen), f"{past} {s.id}")

    if kind == "spore_ascend":
        s = current_row(model)
        if s is None:
            return replace(model, status="no loop selected")
        if not s.ascend_kinds:
            return replace(model, status="⚠ no ascend kinds for this loop's type")
        chosen = _choose(stdscr, f"ascend {s.id} as:", list(s.ascend_kinds))
        if chosen is None:
            return replace(model, status="cancelled")
        ref = _prompt_line(stdscr, f"ascend {s.id} → ref (what the loop became):")
        if not ref:
            return replace(model, status="cancelled")
        if not _confirm(stdscr, f"promote {s.id} → '{ref}' as '{chosen}'?"):
            return replace(model, status="cancelled")
        return _apply(model, source, build_ascend_req(s.id, chosen, ref), f"promoted {s.id}")

    # ---- Slice-3b operator-I/O verbs (the curses peer of the web dump/sort/promote verbs) ----

    if kind == "spore_edit":  # refine text in $EDITOR → spore_update{text} (server strips)
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        edited = _edit_via_editor(stdscr, s.text or "")
        if edited is None:
            return replace(model, status="⚠ edit not applied — $EDITOR is missing or exited non-zero")
        if not edited.strip():
            return replace(model, status="⚠ text cannot be cleared to empty")
        if edited.strip() == (s.text or "").strip():
            return replace(model, status="no change")
        return _apply(model, source, build_spore_update_req(s.id, text=edited), f"edited {s.id}")

    if kind == "spore_capture":  # PANEL-level freeform dump → Tray seed / Keep note
        # Deliberately sends NO `type` → the server defaults to `thought` and the AI sorts it
        # ("human dumps, AI sorts" — the same as the web dump's default "let flow sort" option).
        # The operator classifies post-dump via the Tray `c` (reclassify) verb when they care.
        panel = model.current_panel()
        disposition = "note" if (panel and panel.get("kind") == "keep") else "seed"
        text = _edit_via_editor(stdscr, "")
        if text is None:
            return replace(model, status="⚠ capture not saved — $EDITOR is missing or exited non-zero")
        if not text.strip():
            return replace(model, status="nothing captured")
        label = "kept a note" if disposition == "note" else "dropped a Tray seed"
        return _apply(model, source, build_seed_req(text, disposition), label)

    if kind == "spore_reclassify":  # change a forming Tray item's type (locks at metabolize)
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        from anneal_memory.spores import VALID_TYPES  # lazy (matches writes.py)
        chosen = _choose(stdscr, f"reclassify {s.id} as:", sorted(VALID_TYPES))
        if chosen is None:
            return replace(model, status="cancelled")
        if chosen == s.type:
            return replace(model, status="no change")
        return _apply(model, source, build_spore_update_req(s.id, spore_type=chosen),
                      f"reclassified {s.id} → {chosen}")

    if kind == "spore_metabolize":  # Tray seed → live cognition loop (gain direction, no confirm)
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        return _apply(model, source, build_set_disposition_req(s.id, "loop"),
                      f"metabolized {s.id} → loop")

    if kind == "spore_schedule":  # surface_at on a loop/Tray item ('-' clears the alarm)
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        # Show the existing alarm so a RESCHEDULE sees the current value (web prefill parity).
        now = getattr(s, "next", None) or "none"
        date = _prompt_line(stdscr, f"surface {s.id} on (YYYY-MM-DD, '-' clears) [now: {now}]:")
        if not date:
            return replace(model, status="cancelled")
        value = "" if date == "-" else date
        ok = f"cleared schedule on {s.id}" if value == "" else f"scheduled {s.id} → {date}"
        return _apply(model, source, build_surface_at_req(s.id, value), ok)

    if kind == "spore_park":  # pin a loop to Keep (hide from cognition, stays open)
        s = current_row(model)
        if s is None:
            return replace(model, status="no loop selected")
        return _apply(model, source, build_spore_update_req(s.id, tier="parked"),
                      f"parked {s.id} → Keep")

    if kind == "spore_to_tray":  # promote a Keep note → the Tray inbox (note→seed)
        s = current_row(model)
        if s is None:
            return replace(model, status="no note selected")
        return _apply(model, source, build_set_disposition_req(s.id, "seed"),
                      f"promoted {s.id} → Tray")

    if kind == "keep_activate":  # row-dispatched: a note → loop; a parked loop → un-park
        s = current_row(model)
        if s is None:
            return replace(model, status="no item selected")
        if getattr(s, "disposition", None) == "note":
            # note → loop: backstopped server-side (set_disposition CASes the disposition).
            return _apply(model, source, build_set_disposition_req(s.id, "loop"),
                          f"activated {s.id} → loop")
        # parked loop → un-park: KNOWN CAS GAP (codex/complement L3, spore-173) — the note-vs-
        # parked route is read off the SNAPSHOT, and spore_update{tier} is CAS-free server-side,
        # so a stale note here accepts tier=warm + reports "reactivated" while staying a note.
        # Low-probability single-operator; fix = expect_disposition on the tier update (spore-173).
        return _apply(model, source, build_spore_update_req(s.id, tier="warm"),
                      f"reactivated {s.id} → warm")

    if kind == "keep_remind":  # note → Tray seed + future surface, atomically (one CAS)
        s = current_row(model)
        if s is None:
            return replace(model, status="no note selected")
        date = _prompt_line(stdscr, f"remind ({s.id}) in the Tray on (YYYY-MM-DD):")
        if not date:
            return replace(model, status="cancelled")
        return _apply(model, source, build_set_disposition_req(s.id, "seed", surface_at=date),
                      f"reminder: {s.id} → Tray on {date}")

    if kind == "episode_tombstone":
        e = current_episode(model)
        if e is None:
            return replace(model, status="no episode selected")
        if not _confirm(stdscr, f"tombstone {e.id}? content PERMANENTLY erased (only an audit row remains)"):
            return replace(model, status="cancelled")
        return _apply(model, source, build_tombstone_req(e.id), f"tombstoned {e.id}")

    if kind == "undo":
        r = current_edit_record(model)
        if r is None:
            return replace(model, status="no edit selected")
        if not is_record_undoable(r):  # belt-and-suspenders: _active_verbs already hid it
            return replace(model, status="⚠ that record isn't file-undoable")
        eid = r.get("id")
        if not isinstance(eid, str):
            return replace(model, status="⚠ that record has no id to undo")
        return _apply(model, source, build_undo_req(eid), f"undid {eid}")

    return model


def _handle_edit(
    stdscr: "curses.window", model: TuiModel, source: SubstrateSource,
) -> TuiModel:
    """The Class-A ``[e]dit`` flow: load the section's current body, hand it to
    ``$EDITOR``, and apply the result with the original body as ``expected_body``
    (the stale-check — a wrap landing mid-edit cleanly 409s rather than clobbers).
    State (the continuity section) routes ``kind=state``; world.md/posture/recency
    route ``kind=config``."""
    panel = model.current_panel()
    if panel is None:
        return model
    # Defense-in-depth: _active_verbs only offers `e` on a Class-A panel, but never
    # launch $EDITOR / build a write off a non-Class-A panel even if reached another
    # way — the manifest's edit_class is the authorization signal (codex/kimi L3).
    if panel.get("edit_class") != CLASS_A:
        return replace(model, status="that surface isn't operator-editable (read-only)")
    view = model.view
    ref = panel.get("ref")
    pkind = panel.get("kind")
    no_editor = "⚠ edit not applied — $EDITOR is missing or exited non-zero"

    if pkind == "section" and isinstance(ref, int) and ref < len(view.sections):
        heading = view.sections[ref].heading
        body = view.sections[ref].body
        edited = _edit_via_editor(stdscr, body)
        if edited is None:
            return replace(model, status=no_editor)
        if edited == body:
            return replace(model, status="no change")
        # Thread the ACTUAL heading (not a hardcoded "State") so a future second
        # Class-A section targets correctly; the server re-validates the edit-class.
        req = build_state_edit_req(heading=heading, expected_body=body, new_body=edited)
        label = heading
    elif pkind == "config" and isinstance(ref, int) and ref < len(view.config_docs):
        body = view.config_docs[ref].body
        edited = _edit_via_editor(stdscr, body)
        if edited is None:
            return replace(model, status=no_editor)
        if edited == body:
            return replace(model, status="no change")
        src = panel.get("source")
        cfg_heading = panel.get("heading")
        if not isinstance(src, str):
            return replace(model, status="⚠ panel has no write address")
        req = build_config_edit_req(
            source=src, heading=cfg_heading if isinstance(cfg_heading, str) else None,
            expected_body=body, new_body=edited,
        )
        label = cfg_heading if isinstance(cfg_heading, str) else src
    else:
        return replace(model, status="edit not wired for this panel kind")

    return _apply(model, source, req, f"edited {label}")


def _apply(
    model: TuiModel, source: SubstrateSource,
    req: dict, ok_msg: str,
) -> TuiModel:
    """Run one ``apply_edit`` and re-seat the model. Success → rebuild + ok status;
    a ``stale`` 409 → rebuild (the substrate moved) + the reload status; any other
    refusal → keep the view, show the mapped status."""
    if model.read_only:
        # Structural backstop (structural_invariants_beat_discipline): in a read-only
        # view no write can reach apply_edit, regardless of how dispatch was reached.
        # _active_verbs already advertises + dispatches nothing; this is the single
        # un-bypassable chokepoint every write funnels through.
        return replace(model, status="read-only view — write verbs are suppressed")
    scope = source.write_scope
    if scope is None:
        # Belt-and-suspenders: model.read_only already folds in `write_scope is None`
        # (see _loop), so this is unreachable via the loop — but _apply is the
        # un-bypassable chokepoint, so it refuses on its OWN state, not a caller flag.
        return replace(model, status="this substrate has no write scope — read-only")
    try:
        apply_edit(scope, req)
    except EditError as exc:
        status = edit_error_status(exc.code, str(exc))
        if exc.code == "stale":
            return _rebuild(model, source, status)
        return replace(model, status=status)
    except OSError as exc:  # last-resort: never crash the UI on an unexpected IO fault
        return replace(model, status=f"⚠ write failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — UI liveness: a write-layer bug can't kill the loop
        return replace(model, status=f"⚠ write error: {exc}")
    return _rebuild(model, source, ok_msg)


# ---------------------------------------------------------------------------
# Input modals — the side-effecting bits the pure layer can't own. Each draws on
# the status row, reads, and restores. Kept dumb: no logic, just collection.
# ---------------------------------------------------------------------------

def _confirm(stdscr: "curses.window", message: str) -> bool:
    """y/Y confirms; anything else declines. A resize redraws + re-reads — it must
    NOT count as a decline (that would silently cancel a destructive verb on an
    accidental window resize — complement/kimi L3)."""
    while True:
        h, w = stdscr.getmaxyx()
        _safe_addstr(stdscr, h - 2, 0, (message + "   [y/N]").ljust(w)[:w], curses.A_REVERSE)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
            continue
        return ch in (ord("y"), ord("Y"))


def _prompt_line(stdscr: "curses.window", label: str) -> str | None:
    """Read a single line of text (for the ascend ref). Empty / aborted → ``None``.
    The label is capped so at least a third of the width stays free for input on a
    narrow terminal (else the field collapses to one column)."""
    h, w = stdscr.getmaxyx()
    shown = label[: max(8, (w * 2) // 3)]
    _safe_addstr(stdscr, h - 2, 0, (shown + " ").ljust(w)[:w], curses.A_REVERSE)
    stdscr.refresh()
    raw = b""
    try:
        curses.echo()
        try:
            curses.curs_set(1)  # may raise on a terminal without cursor-visibility
        except curses.error:
            pass
        raw = stdscr.getstr(h - 2, min(len(shown) + 1, w - 2), 256)
    except curses.error:
        raw = b""
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    return text or None


def _choose(stdscr: "curses.window", label: str, options: list[str]) -> str | None:
    """Pick one of ``options``. A single option is auto-selected. Otherwise show a
    numbered menu (1-9; a clipped overflow is announced) and read a digit; Esc
    cancels; any other key redraws and re-reads — a fat-fingered key never silently
    aborts the verb."""
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    shown = options[:9]
    more = f"  …(+{len(options) - 9} more)" if len(options) > 9 else ""
    menu = (
        label + "  " + "   ".join(f"{i + 1}:{o}" for i, o in enumerate(shown))
        + more + "   (Esc cancel)"
    )
    while True:
        h, w = stdscr.getmaxyx()  # re-read each pass so a resize mid-prompt is handled
        _safe_addstr(stdscr, h - 2, 0, menu.ljust(w)[:w], curses.A_REVERSE)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == 27:  # Esc → cancel
            return None
        if ord("1") <= ch <= ord(str(len(shown))):
            return shown[ch - ord("1")]
        # any other key: redraw + re-read (no silent cancel)


def _edit_via_editor(stdscr: "curses.window", initial_text: str) -> str | None:
    """Suspend curses, open ``$EDITOR`` on a tmp file seeded with ``initial_text``,
    resume, and return the edited text. ``None`` if the editor couldn't be launched
    OR exited non-zero (a crash / ``:cq``) — in that case the (possibly partial) tmp
    buffer is NOT committed; the caller surfaces it as a status, never a crash."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    fd, tmp = tempfile.mkstemp(suffix=".md", prefix="levain-edit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(initial_text)
        curses.endwin()  # hand the terminal to the editor
        rc = 1
        try:
            rc = subprocess.call(shlex.split(editor) + [tmp])
        except (OSError, ValueError):  # missing editor / unbalanced quotes in $EDITOR
            return None
        finally:
            curses.flushinp()       # drop keys typed during the edit
            stdscr.clearok(True)    # force a full repaint — drop $EDITOR screen residue
            stdscr.refresh()        # re-enter curses
        if rc != 0:
            return None  # editor aborted / crashed — do NOT commit a partial buffer
        try:
            with open(tmp, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Painting.
# ---------------------------------------------------------------------------

def _safe_addstr(stdscr: "curses.window", y: int, x: int, text: str, attr: int = 0) -> None:
    """Write clipped to the window width, swallowing the curses error from the
    bottom-right cell. curses raises if you write the last cell or past the edge —
    a TUI that crashes on a narrow terminal is the opposite of robust."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    avail = w - x
    if avail <= 0:
        return
    s = text[:avail]
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass  # last-cell write; harmless


def _paint(stdscr: "curses.window", model: TuiModel) -> TuiModel:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < _MIN_H or w < _MIN_W:
        _safe_addstr(stdscr, 0, 0, "terminal too small — resize (or use `levain dashboard`)")
        stdscr.refresh()
        return model

    view = model.view
    title = view.entity_name or view.paths.episodic_db.stem

    # Header (row 0): entity + store path, right-aligned store.
    _safe_addstr(stdscr, 0, 0, f" Levain — {title}", curses.A_BOLD)
    store = str(view.paths.episodic_db)
    sx = max(len(f" Levain — {title}") + 2, w - len(store) - 1)
    _safe_addstr(stdscr, 0, sx, store, curses.A_DIM)

    # Zone tabs (row 1): the active zone reversed.
    x = 1
    for i, (_zone, label) in enumerate(ZONES):
        tab = f" {i + 1} {label} "
        attr = curses.A_REVERSE if i == model.zone_idx else curses.A_NORMAL
        _safe_addstr(stdscr, 1, x, tab, attr)
        x += len(tab) + 1
    _safe_addstr(stdscr, 2, 0, "─" * w, curses.A_DIM)

    left_w = min(_LEFT_W, w // 3)
    body_top, body_bottom = 3, h - 3  # inclusive rows for the body
    body_h = body_bottom - body_top + 1

    # Left column: the zone's panels, with edit-class chip + materiality. Scroll
    # the column to keep the selected panel visible on a short terminal (Mind has
    # ~9 panels — without this the selected one can be off-screen while its detail
    # shows on the right; codex L3 LOW).
    panels = model.panels()
    left_scroll = max(0, model.panel_idx - body_h + 1)
    for row_i, panel in enumerate(panels[left_scroll:left_scroll + body_h]):
        pidx = left_scroll + row_i
        cls = panel.get("edit_class") or ""
        # NO-THEATER: the metal [A]/[B] chip + bold is THIS surface's operability signal. A
        # panel can be Class B in the shared manifest because the WEB operates it (Tray/Keep,
        # Slice 3b) while the curses surface has no verb for it yet → render it as glass (no
        # chip, dim) rather than a false metal [B] until the TUI grows the verb [codex L3
        # NO-THEATER]. `read_only=False` probes "can this surface EVER operate this kind?",
        # independent of the cockpit's current read-only MODE (which suppresses every verb).
        if cls == CLASS_B and not panel_verbs(panel, read_only=False):
            cls = ""
        chip = f"[{cls}]" if cls else "   "
        label = str(panel.get("title", panel.get("kind", "?")))
        row = f"{chip} {label}"
        if pidx == model.panel_idx:
            attr = curses.A_REVERSE
        elif model.read_only or cls == CLASS_C or cls == "":
            # glass — Class-C cognition/record, OR (read_only) the WHOLE surface:
            # nothing is steerable here, so nothing reads as metal. Keeps the
            # materiality doctrine honest with the footer (which advertises no verbs).
            attr = curses.A_DIM
        else:
            attr = curses.A_BOLD  # metal — Class A/B, operator-steerable
        _safe_addstr(stdscr, body_top + row_i, 0, row.ljust(left_w)[:left_w], attr)

    # Vertical rule between the columns.
    for yy in range(body_top, body_bottom + 1):
        _safe_addstr(stdscr, yy, left_w, "│", curses.A_DIM)

    # Right column: the selected panel's detail.
    sel = model.current_panel()
    detail_x = left_w + 2
    scroll = 0
    if sel is not None:
        lines = render_panel_lines(view, sel)
        max_scroll = max(0, len(lines) - body_h)
        if is_item_list(sel) and panel_item_count(view, sel) > 0:
            # The item cursor maps 1:1 to a line; auto-scroll to keep it visible.
            # (An EMPTY list panel falls to the else branch — it renders its
            # "(no …)" placeholder with no false cursor highlight.)
            item = model.item_idx
            scroll = model.scroll
            if item < scroll:
                scroll = item
            elif item >= scroll + body_h:
                scroll = item - body_h + 1
            scroll = max(0, min(scroll, max_scroll))
            if scroll != model.scroll:
                model = replace(model, scroll=scroll)
            for i, line in enumerate(lines[scroll:scroll + body_h]):
                attr = curses.A_REVERSE if (scroll + i) == item else 0
                _safe_addstr(stdscr, body_top + i, detail_x, line, attr)
        else:
            scroll = min(model.scroll, max_scroll)
            if scroll != model.scroll:
                model = replace(model, scroll=scroll)  # clamp past-content offsets
            for i, line in enumerate(lines[scroll:scroll + body_h]):
                _safe_addstr(stdscr, body_top + i, detail_x, line)
        if max_scroll > 0:
            pos = f"{min(scroll + body_h, len(lines))}/{len(lines)}"
            _safe_addstr(stdscr, body_bottom, w - len(pos) - 1, pos, curses.A_DIM)

    # Status line (row h-2) + footer (row h-1, with the selected panel's verbs).
    if model.status:
        _safe_addstr(stdscr, h - 2, 0, model.status[:w - 1], curses.A_BOLD)
    verb_hint = "   ".join(v.label for v in _active_verbs(model))
    nav = " 1/2/3 zone · [ ] panel · j/k move · r refresh · ? help · q quit"
    footer = nav + (f"   ║   {verb_hint} " if verb_hint else " ")
    _safe_addstr(stdscr, h - 1, 0, footer.ljust(w)[:w], curses.A_REVERSE)

    stdscr.refresh()
    return model


_HELP_LINES = [
    "levain tui — keys",
    "",
    "  navigation",
    "    1 / 2 / 3      switch zone (Identity / Operate / Mind)",
    "    Tab            cycle zone forward",
    "    [ ] or h l     previous / next panel in the zone",
    "    j / k          move (select item in a list panel, else scroll)",
    "    PgDn / PgUp    page · g / G  first / last (or top / bottom)",
    "    r              refresh (rebuild the substrate view)",
    "    q / Esc        quit · ?  this help (any key dismisses)",
    "",
    "  verbs (only where the SELECTED panel + row affords them)",
    "    config / sections   e            edit a Class-A input ($EDITOR): State / world.md",
    "    Open Loops          t e s p d a  touch · edit · schedule · park · descend · ascend",
    "    Tray (inbox)        n e c m s d  new-dump · edit · re(c)lassify · metabolize→loop ·",
    "                                     schedule · dismiss",
    "    Keep (durable)      n e m y s d  note-dump · edit · activate · →tra(y) · remind · drop",
    "                                     (→tray / remind are note-only; on a parked loop,",
    "                                      m un-parks it back into cognition)",
    "    Episodes / Edits    x · u        tombstone an episode · undo a Class-A file edit",
    "    n (new-dump / note) works on an EMPTY Tray/Keep too — it's a panel-level capture.",
    "",
    "  Class-C is dim (consolidated cognition — glass, read-only);",
    "  Class-A/B is bold (operator inputs — metal, steerable).",
    "  Destructive verbs confirm; every write goes through the governed seam.",
]


def _paint_help(stdscr: "curses.window") -> None:
    stdscr.erase()
    for i, line in enumerate(_HELP_LINES):
        attr = curses.A_BOLD if i == 0 else curses.A_NORMAL
        _safe_addstr(stdscr, 1 + i, 2, line, attr)
    stdscr.refresh()
