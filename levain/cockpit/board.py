"""The cockpit's read-only board (C12): the work as DERIVED columns, the audit window over the desk's work. Nothing here
writes, stores a column, or keeps a second needs-you list.

Two sources, one value. When this install has a board store (T3, READY_SCHEMA v3: items stored, state derived on every read,
every tracked repo of the registry read from one snapshot) the cards are that board's items, read through
:func:`levain.desk.board_cli.state_doc`; the columns are the v2 ``STATE_COLUMN`` set: ``unknown`` is its own column, first, and
never folded into another; ``off`` (cut, parked, out of 1.0) is a COUNT (``off_count``), never a column of cards. An install
with no board store yet reads the desk's dispatch queue as before (C13's ``list(all=True)`` items, each with its last journal
record), and its ``unknown`` column is empty. The needs-you column IS the Q17-ALL band's open items, read through
``levain.band_view``'s own read-only snapshot and its allocated numbers (a number not yet allocated reads ``None``: a look
never numbers an item). Known edge: the board's own ``audit`` (landed-not-closed, expired gates, unattributed work) is not carried
here, and neither the List nor this board draws it: ``levain board state`` is where it shows. A source that cannot be read is named in ``unreadable``, never shown as an empty column; a store
never made is ``queue: "none"`` ("no queue yet"). ``build`` never raises."""
from __future__ import annotations

import argparse
import copy
import threading
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from levain.desk.ready import UNREADABLE_DESK
from levain.cockpit.display_safe import visible
from levain.desk import ready

BOARD_PATH = "/cockpit/board.json"
UNKNOWN = "unknown"
TEXT_MAX = 1000        # the kernel's board validator refuses a longer card field (engine.BOARD_TEXT_MAX; one test pins them equal)
CATEGORIES = ready.CATEGORIES
COLUMNS = (UNKNOWN, *CATEGORIES)        # the columns a board value can carry, in drawing order: unknown first (v2 draws it above the rest),
                                         # and only while it holds a card: an empty unknown column is left out, so a board with nothing unplaced draws as it did
CARDS_MAX = 2000       # the kernel's board validator refuses a board of more cards (engine.BOARD_CARDS_MAX; one test pins them equal)
BAND_HINT = "levain band"
NO_QUEUE = "no queue yet"
_WHAT_MAX = 300


def is_desk_owed(card: Mapping[str, Any]) -> bool:
    """A band card the desk alone owes (the band's one rule, :func:`levain.band_view.desk_owed`): not the operator's. Only a band
    card: any other card may carry a ``section`` of its own (the board validator keeps extra keys) and is never hidden by it."""
    from levain import band_view

    return card.get("band") is True and band_view.desk_owed(card.get("section"), card.get("carried"))


def visible_cards(cards: Any) -> list[Any]:
    """The cards a board draws and counts: every card but one the desk alone owes. A non-mapping card stays (never hidden)."""
    return [k for k in cards or [] if not (isinstance(k, Mapping) and is_desk_owed(k))]


def _last_records(records: Any) -> dict[str, Mapping[str, Any]]:
    """The newest journal record per item id (store order is the journal's order)."""
    out: dict[str, Mapping[str, Any]] = {}
    for rec in records or ():
        if isinstance(rec, Mapping) and isinstance(rec.get("id"), str):
            out[rec["id"]] = rec
    return out


def _line(rec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if rec is None:
        return None
    return {k: rec.get(k) for k in ("iso", "who", "why", "source", "from_category", "to_category", "applied_by")}


def _text(value: Any) -> Any:
    """A card's drawn text made display-safe and bounded here, so one bad field is shown marked and cut, never a reason for the
    kernel's board validator to refuse the whole board."""
    return visible(value)[:TEXT_MAX] if isinstance(value, str) else value


def _card(item: Mapping[str, Any], rec: Mapping[str, Any] | None) -> dict[str, Any]:
    card = {k: item.get(k) for k in ("id", "repo", "title", "done_line", "tier", "depends_on", "status", "status_category",
                                    "declared_category", "held_reason", "well_formed", "rank", "unknown_category")}
    for k in ("id", "repo", "title", "done_line"):
        card[k] = _text(card[k])
    card["journal"] = _line(rec)
    return card


# A repo's git evidence is nearly all of a board build, so it is kept while the repo's refs have not moved. NOT a time-only TTL:
# the key is every ref and sha (`for-each-ref`), the stamps (size + mtime_ns) of FETCH_HEAD / packed-refs / shallow in the git
# dir and the common dir, every config file git reads for the repo (`board_evidence.config_stamp`), and the read's own arguments
# (`since` among them). Everything else the board derives (items, the registry, seats, the band, waiters, documents) is read
# live on every build, so a card never waits for a cache. One entry per repo path. A read that could not answer in full
# (any `unreadable`) is never kept: it is retried by the next build. One build per repo at a time (single flight): a build
# that arrives during another waits for it and takes its result from the cache. How often the panel is read is the panel
# refresher's own interval (grep `board=` in levain/web_server.py), not stated here.
_EVIDENCE_CACHE: dict[str, tuple[Any, Any, Any]] = {}
_EVIDENCE_LOCK = threading.Lock()
_BUILD_LOCKS: dict[str, threading.Lock] = {}


def _refs_state(repo: Path) -> Any:
    """What the evidence read depends on in the repo, or None when that cannot be read (then nothing is cached)."""
    from levain.desk import board_evidence as be

    try:
        refs = be._git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        dirs, config = be.config_stamp(repo)
    except be.GitFailed:
        return None
    stamps = tuple((d, name, be.stat_stamp(Path(d) / name)) for d in dirs for name in ("FETCH_HEAD", "packed-refs", "shallow"))
    return (refs, stamps, config)


def _hit(key: str, state: Any, arg_key: Any) -> Any:
    with _EVIDENCE_LOCK:
        hit = _EVIDENCE_CACHE.get(key)
    if hit is not None and hit[0] == state and hit[1] == arg_key:
        return copy.deepcopy(hit[2])
    return None


def _cached_read_evidence(repo: Path, *, repo_name: str | None = None, since: str | None = None, **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``board_evidence.read_git_evidence``, kept while the repo's refs state is the same (see above)."""
    from levain.desk import board_evidence as be

    args = {"repo_name": repo_name, **({"since": since} if since is not None else {}), **kw}
    state = _refs_state(Path(repo))
    key = str(Path(repo))
    arg_key = tuple(sorted(args.items()))
    if state is None:
        return be.read_git_evidence(repo, **args)
    got = _hit(key, state, arg_key)
    if got is not None:
        return got
    with _EVIDENCE_LOCK:
        lock = _BUILD_LOCKS.setdefault(key, threading.Lock())
    with lock:                                   # single flight: a second build waits here, then finds the first one's result
        state = _refs_state(Path(repo))
        if state is None:
            return be.read_git_evidence(repo, **args)
        got = _hit(key, state, arg_key)
        if got is not None:
            return got
        out = be.read_git_evidence(repo, **args)
        # kept only when it read in full, and only if nothing moved while it was read
        if not out[0].get("unreadable") and _refs_state(Path(repo)) == state:
            with _EVIDENCE_LOCK:
                _EVIDENCE_CACHE[key] = (state, arg_key, copy.deepcopy(out))
        return out


def _v2_target(items: Path | str | None) -> Any:
    """The board store to read, or None when this install has none yet (a store with a registry; else the dispatch queue is read). A store that is
    named but cannot be resolved raises ``bs.BoardError``: that is an unreadable board, never a quiet fall back."""
    from levain.desk import board_store as bs

    try:
        target = bs.resolve_store(items)
    except bs.BoardError as exc:
        if str(exc).startswith("no items store"):
            return None
        raise
    # No registry file: ``levain board state`` would read the repository in ITS working directory, which for a cockpit is
    # whatever directory the server started in. That is not a board; this install has none yet.
    return target if (target.store_dir / "repos.jsonl").is_file() else None


def _v2_card(st: Mapping[str, Any]) -> dict[str, Any]:
    state = str(st.get("state"))
    card: dict[str, Any] = {"id": _text(str(st.get("id"))), "repo": _text(st.get("repo")), "title": _text(st.get("title") or ""),
                            "tier": st.get("tier"), "status": state, "status_category": str(st.get("column")).replace("_", "-"),
                            "kind": st.get("kind"), "reason": _text(st.get("reason")), "journal": None}
    if state in ("unknown", "blocked"):
        card["held_reason"] = card["reason"]
    return card


def _v2(items: Path | str | None, *, now: str | None) -> tuple[list[dict[str, Any]], int, list[str], str | None] | None:
    """``(cards, off_count, unreadable, as_of)`` from the board store, or None when there is none. ``cards`` holds every item
    the board shows but a needs-you one (the band owns that column) and an off one (counted, not drawn)."""
    from levain.desk import board_cli as bc
    from levain.desk import board_evidence as be
    from levain.desk import board_store as bs

    try:
        target = _v2_target(items)
    except bs.BoardError:
        return [], 0, [UNREADABLE_DESK], None
    if target is None:
        return None
    args = argparse.Namespace(json=True, items=str(items) if items else None, evidence_extra=None, producers=[],
                              land_waiter=None, repo=None, repo_name=None, also_repo=[], since=be.BOARD_EPOCH, now=now)
    try:
        rc, doc = bc.state_doc(args, lambda _text: None, _cached_read_evidence)
    except Exception:  # noqa: BLE001 - never a 500: the board is named unreadable
        rc, doc = bc.EXIT_USAGE, None
    if doc is None:
        return [], 0, [UNREADABLE_DESK], None
    cards: list[dict[str, Any]] = []
    off = 0
    for st in doc.get("items") or ():
        col = st.get("column")
        if col == "off":
            off += 1
        elif col != "needs_you":
            cards.append(_v2_card(st))
    return cards, off, [], doc.get("as_of")


def _band_cards(question_store: Any) -> tuple[list[dict[str, Any]], str | None]:
    """The band's open items as needs-you cards, and the reason they could not be read (else None)."""
    if question_store is None:
        return [], None        # this cockpit hosts no question store: nothing to read, nothing unreadable
    from levain import band_view

    try:
        items, numbers, _closed = band_view._read(question_store)
    except Exception:  # noqa: BLE001 - a band that cannot be read is reported, never an empty column
        return [], "band"
    cards = []
    for ref, q in items:
        card: dict[str, Any] = {"id": f"band:{ref}", "number": numbers.get(ref), "journal": None, "band": True,
                                "repo": None, "status": "needs-you", "status_category": "needs-you"}
        if q is None:
            card.update(title="UNREADABLE: this band item could not be read", unreadable=True)
        else:
            card.update(title=_text(str(q.what)[:_WHAT_MAX]), source=band_view._tag(q), blocks=q.blocks, filed_at=q.filed_at)
            lat = band_view.later_of(question_store, q, numbers.get(ref))
            if lat is not None:       # a snoozed question: one collapsed LATER card, no blocks (row 131)
                card.update(later=lat[0], later_label=_text(lat[1]), blocks="")
                words = band_view.later_facets(question_store, q)
                if words is not None:
                    card["later_words"] = words[1]
            # the host's own band section and carried flag, passed through as data (levain never classifies a row); a
            # store with none (the kernel's own, an older host) sets nothing, and the row is a needs-you row as before
            section = getattr(q, "section", "")
            if section in ("you", "desk"):
                card["section"] = section
                carried = getattr(q, "carried", None)
                if isinstance(carried, bool):      # a real bool only, and only beside a section it qualifies
                    card["carried"] = carried
            from levain.consent.questions import intention_of, intention_state_of, intention_text_of

            ring = intention_of(question_store, q)      # the intention id this row rings, only when the host can act on it
            if ring is not None:
                card["intention"] = ring
                card["intention_state"] = intention_state_of(question_store, q)   # the List band row's version binds it
                words = intention_text_of(question_store, q)
                if words is not None:
                    card["intention_text"] = words
            since = getattr(q, "section_since", "")
            if isinstance(since, str) and since:      # the host's own age clock for this row, when it sends one
                card["since"] = since
        cards.append(card)
    return cards, None


def build(store_dir: Path | str | None, question_store: Any = None, *, generated: str | None = None,
          drop_dir: Path | str | None = None, board_items: Path | str | None = None) -> dict[str, Any]:
    """The board JSON: ``{schema, generated, queue, columns: [{category, cards, count}], off_count, unreadable, band_hint,
    note, journal_as_of_read}``. With a board store (``board_items``, else ``$LEVAIN_BOARD_ITEMS``, the pointer, the levain
    home's own) the cards are the v2 board's, ``off_count`` counts what is cut, parked or out of 1.0 and ``store_dir`` (the
    dispatch queue) is not read; the producers and the land waiter are the store's board config, read by the same code as
    ``levain board state``. Without a board store this is the dispatch queue as it was and ``off_count`` is 0: its items, the
    journal and the live-seat projections (``running`` cards, no journal line) come from ONE read transaction (:func:`levain.desk.ready.snapshot`), so a card's journal line is never newer than its column;
    ``journal_as_of_read`` stays and names that the line is the record as of this read. No CI projection: Levain keeps no CI-wait
    source (flow's ``state/land_waiter`` is flow's).

    ``drop_dir`` is accepted and ignored for one more release (the value no longer carries ``tell``); it warns."""
    if drop_dir is not None:
        warnings.warn("board.build(drop_dir=) is ignored and will be removed: the board value no longer carries `tell`",
                      DeprecationWarning, stacklevel=2)
    unreadable: list[str] = []
    items: list[Mapping[str, Any]] = []
    journal: dict[str, Mapping[str, Any]] = {}
    queue = "none"
    v2cards: list[dict[str, Any]] | None = None
    off_count = 0
    v2 = _v2(board_items, now=generated)
    if v2 is not None:
        v2cards, off_count, bad, as_of = v2
        unreadable.extend(bad)
        queue = "unreadable" if bad else "ok"
        generated = generated or as_of
    elif store_dir is not None:
        try:
            out = ready.snapshot(store_dir, generated=generated)
            items = list(out.get("items") or [])
            unreadable.extend(str(u) for u in out.get("unreadable") or ())
            generated = out.get("generated") or generated
            if not (Path(store_dir) / "dispatch.db").exists():
                queue = "none"
            elif UNREADABLE_DESK in unreadable:
                queue = "unreadable"
            else:
                queue = "ok"
                journal = _last_records(out.get("journal"))
        except Exception:  # noqa: BLE001 - never a 500: the store is named unreadable
            unreadable.append(UNREADABLE_DESK)
            queue = "unreadable"
    by_cat: dict[str, list[dict[str, Any]]] = {c: [] for c in COLUMNS}
    for card in v2cards or ():
        by_cat[card["status_category"] if card["status_category"] in by_cat else UNKNOWN].append(card)
    for item in items:
        cat = item.get("status_category")
        if cat == "needs-you":
            continue          # the band owns this column; never a second needs-you list
        by_cat[cat if cat in by_cat else "backlog"].append(_card(item, journal.get(str(item.get("id")))))
    band, band_bad = _band_cards(question_store)
    if band_bad:
        unreadable.append(band_bad)
    by_cat["needs-you"] = band
    note = NO_QUEUE if queue == "none" else None
    room = max(0, CARDS_MAX - sum(len(v) for c, v in by_cat.items() if c != "done"))
    more = max(0, len(by_cat["done"]) - room)
    if more:             # the whole board is refused past CARDS_MAX: the done column, which only grows, is what is cut
        by_cat["done"] = by_cat["done"][:room]
        note = (note + "; " if note else "") + f"done: [{more} more] not drawn"
    if band_bad:
        note = (note + "; " if note else "") + f"the needs-you band could not be read here; run `{BAND_HINT}`"
    return {"schema": 1, "generated": generated, "queue": queue, "unreadable": unreadable, "note": note,
            "band_hint": BAND_HINT, "journal_as_of_read": True, "off_count": off_count,
            "columns": [{"category": c, "count": len(by_cat[c]) + (more if c == "done" else 0), "cards": by_cat[c]}
                        for c in COLUMNS if c != UNKNOWN or by_cat[c]]}


class Flight:
    """One board build at a time for a host, and a bounded wait for it. ``get(fn, wait_s)`` starts ``fn`` on its own thread unless
    one is already running, and waits at most ``wait_s`` for that build. A caller that does not get it in time is answered from
    the last build that finished (marked ``stale``), or, with none yet, a board that says it is still being built: a request
    never holds a slot of the host's gate for an unbounded cold read, and the build itself runs on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run: tuple[threading.Event, list[Any]] | None = None
        self._last: dict[str, Any] | None = None

    def get(self, fn: Callable[[], dict[str, Any]], wait_s: float) -> dict[str, Any]:
        with self._lock:
            if self._run is None:
                done, slot = threading.Event(), []
                self._run = (done, slot)
                threading.Thread(target=self._work, args=(fn, done, slot), name="board-build", daemon=True).start()
            done, slot = self._run
        if done.wait(wait_s):
            if isinstance(slot[0], BaseException):
                raise slot[0]
            return copy.deepcopy(slot[0])
        with self._lock:
            last = copy.deepcopy(self._last)
        if last is not None:
            last["stale"] = True
            last["note"] = (last["note"] + "; " if last.get("note") else "") + "the board is being rebuilt: this is the last read"
            return last
        return building_board()

    def _work(self, fn: Callable[[], dict[str, Any]], done: threading.Event, slot: list[Any]) -> None:
        try:
            value = fn()
            with self._lock:
                self._last = copy.deepcopy(value)
            slot.append(value)
        except BaseException as exc:  # noqa: BLE001 - handed to the waiters, never lost on a thread
            slot.append(exc)
        finally:
            with self._lock:
                self._run = None
            done.set()


def building_board() -> dict[str, Any]:
    """The board a caller is given while the first build of this process has not finished: no cards, and it says so."""
    return {"schema": 1, "generated": None, "queue": "unreadable", "unreadable": ["building"], "building": True,
            "note": "the board is being read for the first time (a cold read of the repos): this view refreshes when it is done",
            "band_hint": BAND_HINT, "journal_as_of_read": True, "off_count": 0,
            "columns": [{"category": c, "count": 0, "cards": []} for c in CATEGORIES]}
