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
import os
import stat
import threading
import warnings
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
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
# The order the cap cuts columns in once the board is over it: done first (it only grows), then the columns an agent can re-derive, and
# what needs the operator (unknown, then the band) last: a board is never trimmed of the thing it exists to say.
TRIM_ORDER = ("done", "backlog", "waiting", "running", "ready", "unknown", "needs-you")
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
# The evidence table is bounded, least recently used out (a long-lived server cycling through checkouts never grows it without end): at
# most ``EVIDENCE_MAX`` repos. The build locks are STRIPED, not kept per repo: a fixed tuple of ``LOCK_STRIPES`` locks, a repo's lock
# the one at ``crc32(repo key) % LOCK_STRIPES``. There is no table of locks to grow or evict, so no lock can be dropped between
# being looked up and being taken (two repos that share a stripe just wait for each other).
_EVIDENCE_CACHE: OrderedDict[str, tuple[Any, Any, Any]] = OrderedDict()
_EVIDENCE_LOCK = threading.Lock()
EVIDENCE_MAX = 64
LOCK_STRIPES = 32
_BUILD_LOCKS: tuple[threading.Lock, ...] = tuple(threading.Lock() for _ in range(LOCK_STRIPES))


def _build_lock(key: str) -> threading.Lock:
    return _BUILD_LOCKS[zlib.crc32(key.encode("utf-8", "surrogateescape")) % LOCK_STRIPES]


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
        if hit is not None:
            _EVIDENCE_CACHE.move_to_end(key)
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
    with _build_lock(key):                                   # single flight: a second build waits here, then finds the first one's result
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
                _EVIDENCE_CACHE.move_to_end(key)
                while len(_EVIDENCE_CACHE) > EVIDENCE_MAX:
                    _EVIDENCE_CACHE.popitem(last=False)
        return out


@dataclass(frozen=True)
class Snapshot:
    """What ONE board request is of, resolved once: the target (store directory, items file, pointered), the refusal when the store
    could not be resolved, whether the install names a store at all (``configured``), and ``key``: the store directory, its
    ``store.id``, the stamps of its board config and registry. The flight key is ``key`` and the build is handed THIS value, so the
    key and the board it names cannot diverge (nothing in the build resolves the pointer again)."""

    target: Any
    refusal: str | None
    configured: bool
    key: Any


def take_snapshot(items: Path | str | None = None) -> Snapshot:
    """Resolve the board store once. Cheap (a few stats and a small read) and never raises."""
    from levain.desk import board_evidence as be
    from levain.desk import board_store as bs

    try:
        target = bs.resolve_store(items)
    except bs.BoardError as exc:
        if str(exc).startswith("no items store"):
            return Snapshot(None, None, False, ("none",))
        return Snapshot(None, str(exc), True, ("unresolved", str(exc)[:200]))
    except Exception as exc:  # noqa: BLE001 - a snapshot is never a reason for a 500
        return Snapshot(None, f"the board store cannot be resolved ({type(exc).__name__})", True, ("unresolved", type(exc).__name__))
    d = target.store_dir
    try:
        sid = (d / bs.STORE_ID_NAME).read_text("utf-8").strip()
    except (OSError, ValueError):
        sid = None
    configured = bool(target.pointered or (items is not None and str(items) != "") or os.environ.get(bs.ITEMS_ENV) or bs.is_store(target))
    return Snapshot(target, None, configured, (str(d), sid, be.stat_stamp(d / bs.CONFIG_NAME), be.stat_stamp(d / "repos.jsonl")))


def store_key(items: Path | str | None = None) -> Any:
    """The key of a fresh snapshot (see :class:`Snapshot`); a caller that also builds takes the snapshot itself and uses both."""
    return take_snapshot(items).key


def _v2_target(snap: Snapshot) -> Any:
    """The snapshot's board store to read, or None when this install has none yet (else the dispatch queue is read). A store that
    is CONFIGURED (a pointer names it, or ``store.id`` is in it, or ``items`` / ``$LEVAIN_BOARD_ITEMS`` names it) and cannot be
    read (its directory missing or not a directory, its ``repos.jsonl`` not a file, dangling or refused) raises ``bs.BoardError``:
    an unreadable board, never a quiet fall back to "no queue yet" or the v1 queue. A configured store with NO ``repos.jsonl`` is
    returned like any other: ``state_doc`` reads it (an empty board when it holds no item, its items read unknown when it does).
    There is no shortcut here that decides a store is empty without reading it. Only an install with no store at all reads None."""
    from levain.desk import board_store as bs

    if snap.refusal is not None:
        raise bs.BoardError(snap.refusal)
    target = snap.target
    if target is None:
        return None
    if not bs.is_store(target):        # ONE rule for a target that is no store (as in ``state_doc``): a legacy items file, which must
        if not snap.configured:        # exist as a regular file; a repos.jsonl beside it is not read
            return None
        try:
            ist = os.stat(target.items_path)
        except OSError as exc:
            raise bs.BoardError(f"the items file {target.items_path} cannot be read: {exc.strerror or type(exc).__name__}") from None
        if not stat.S_ISREG(ist.st_mode):
            raise bs.BoardError(f"the items file {target.items_path} cannot be read: not a file")
        return target
    registry = target.store_dir / "repos.jsonl"
    try:
        st = os.stat(registry)         # os.stat, not Path.is_file(): is_file() answers False for EACCES too, and a store we cannot read is not an absent one
    except (FileNotFoundError, NotADirectoryError) as exc:
        if os.path.lexists(registry):  # the name is there but leads nowhere (a dangling symlink): that is a registry that cannot be read
            raise bs.BoardError(f"the board store's {registry.name} cannot be read: it leads nowhere") from None
        if not snap.configured:
            return None                # no registry file: `levain board state` would read the repository in ITS working directory, which for a
                                       # cockpit is wherever the server started. That is not a board; this install has none yet.
        try:
            dst = os.stat(target.store_dir)
        except OSError as dexc:
            raise bs.BoardError(f"the board store {target.store_dir} cannot be read: {dexc}") from None
        if not stat.S_ISDIR(dst.st_mode):
            raise bs.BoardError(f"the board store {target.store_dir} cannot be read: not a directory") from None
        return target
    except OSError as exc:
        raise bs.BoardError(f"the board store {registry} cannot be read: {exc}") from None
    if not stat.S_ISREG(st.st_mode):
        if snap.configured:
            raise bs.BoardError(f"the board store's {registry.name} is not a file")
        return None
    return target


def _v2_card(st: Mapping[str, Any]) -> dict[str, Any]:
    state = str(st.get("state"))
    card: dict[str, Any] = {"id": _text(str(st.get("id"))), "repo": _text(st.get("repo")), "title": _text(st.get("title") or ""),
                            "tier": st.get("tier"), "status": state, "status_category": str(st.get("column")).replace("_", "-"),
                            "kind": st.get("kind"), "reason": _text(st.get("reason")), "journal": None}
    if state in ("unknown", "blocked"):
        card["held_reason"] = card["reason"]
    return card


UNREAD_LINES = 5          # how many unreadable reasons a board value names (the rest are counted): the list is drawn, so it is bounded
UNREAD_MAX = 200


def _unreadable_names(why: list[str]) -> list[str]:
    """``desk`` first (the store is what failed), then each distinct reason, display-safe and cut, at most ``UNREAD_LINES`` of them."""
    seen = list(dict.fromkeys(_text(w)[:UNREAD_MAX] for w in why))
    out = [UNREADABLE_DESK, *seen[:UNREAD_LINES]]
    if len(seen) > UNREAD_LINES:
        out.append(f"(+{len(seen) - UNREAD_LINES} more unreadable)")
    return out


def _v2(items: Path | str | None, *, now: str | None, snap: Snapshot | None = None) -> tuple[list[dict[str, Any]], int, list[str], str | None] | None:
    """``(cards, off_count, unreadable, as_of)`` from the board store, or None when there is none. ``cards`` holds every item
    the board shows but a needs-you one (the band owns that column) and an off one (counted, not drawn). ``unreadable`` names every
    SOURCE the read could not read (see :func:`levain.desk.board_cli.state_doc`'s ``unreadable``): the one rule is that exit 3 and
    a non-empty ``audit`` alone do NOT make the board unreadable (a gate, landed-not-closed or an unknown item is an ordinary entry
    about an item), but a source with no item id behind it (a repo that cannot be read, a torn items file, an unread producer, the
    registry) does: then the board is never drawn as a clean, empty one."""
    from levain.desk import board_cli as bc
    from levain.desk import board_evidence as be
    from levain.desk import board_store as bs

    snap = snap or take_snapshot(items)
    try:
        target = _v2_target(snap)
    except bs.BoardError as exc:
        return [], 0, _unreadable_names([str(exc)]), None
    if target is None:
        return None
    args = argparse.Namespace(json=True, items=str(items) if items else None, evidence_extra=None, producers=[],
                              land_waiter=None, repo=None, repo_name=None, also_repo=[], since=be.BOARD_EPOCH, now=now)
    bad: list[str] = []
    try:
        rc, doc = bc.state_doc(args, lambda _text: None, _cached_read_evidence, bad, strict_store=True, target=target)
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
    return cards, off, _unreadable_names(bad) if bad else [], doc.get("as_of")


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
          drop_dir: Path | str | None = None, board_items: Path | str | None = None, snapshot: Snapshot | None = None) -> dict[str, Any]:
    """The board JSON: ``{schema, generated, queue, columns: [{category, cards, count}], off_count, unreadable, band_hint,
    note, journal_as_of_read, as_of}``: ``generated`` is the time of this build, ``as_of`` the OLDEST fetch time of the repos read
    (null when a repo has none, or there is no board store). With a board store (``board_items``, else ``$LEVAIN_BOARD_ITEMS``, the pointer, the levain
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
    as_of: str | None = None             # the OLDEST fetch of the repos read (the board's freshness), its own key: ``generated`` stays this build's clock
    v2 = _v2(board_items, now=generated, snap=snapshot)
    if v2 is not None:
        v2cards, off_count, bad, as_of = v2
        unreadable.extend(bad)
        queue = "unreadable" if bad else "ok"
        generated = generated or datetime.now(timezone.utc).isoformat(timespec="seconds")      # this build's own clock, never a repo's fetch time
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
    more = {c: 0 for c in COLUMNS}
    over = sum(len(v) for v in by_cat.values()) - CARDS_MAX
    for c in TRIM_ORDER:        # the whole board is refused past CARDS_MAX: the cards past it are cut from the end of a column, in this order
        if over <= 0:
            break
        cut = min(over, len(by_cat[c]))
        if cut:
            by_cat[c] = by_cat[c][:len(by_cat[c]) - cut]
            more[c] = cut
            over -= cut
    for c in TRIM_ORDER:
        if more[c]:
            note = (note + "; " if note else "") + f"{c}: [{more[c]} more] not drawn"
    if band_bad:
        note = (note + "; " if note else "") + f"the needs-you band could not be read here; run `{BAND_HINT}`"
    return {"schema": 1, "generated": generated, "as_of": as_of, "queue": queue, "unreadable": unreadable, "note": note,
            "band_hint": BAND_HINT, "journal_as_of_read": True, "off_count": off_count,
            "columns": [{"category": c, "count": len(by_cat[c]) + more[c], "cards": by_cat[c]}
                        for c in COLUMNS if c != UNKNOWN or by_cat[c] or more[c]]}


class Flight:
    """One board build at a time PER KEY (the store the build is of, :func:`store_key`) for a host, and a bounded wait for it.
    ``get(fn, wait_s, key=)`` starts ``fn`` on its own thread unless one is already running for that key, and waits at most
    ``wait_s`` for that build. A caller that does not get it in time is answered from the last build of ITS key that finished
    (marked ``stale``), or, with none yet, a board that says it is still being built: a request never holds a slot of the host's
    gate for an unbounded cold read, and the build itself runs on. A changed key is its own flight and is never answered with
    another key's board, fresh or stale."""

    KEPT = 4              # the last boards kept (one per key, the most recent keys): a host with one store keeps one
    MAX_FLIGHTS = 4       # builds in progress across ALL keys: past it a request starts no thread, it is answered from the last read of its key

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[Any, tuple[threading.Event, list[Any]]] = {}
        self._last: OrderedDict[Any, dict[str, Any]] = OrderedDict()

    def get(self, fn: Callable[[], dict[str, Any]], wait_s: float, key: Any = None) -> dict[str, Any]:
        with self._lock:
            run = self._runs.get(key)
            if run is None and len(self._runs) >= self.MAX_FLIGHTS:
                return self._too_many(key)
            if run is None:
                done, slot = threading.Event(), []
                self._runs[key] = (done, slot)
                try:
                    threading.Thread(target=self._work, args=(fn, done, slot, key), name="board-build", daemon=True).start()
                except BaseException:
                    del self._runs[key]       # no thread, so nothing would ever clear it: the next call starts its own
                    raise
                run = (done, slot)
        done, slot = run
        if not done.wait(wait_s):
            with self._lock:
                if not done.is_set():          # still running: the last read of this key, or "building"
                    return self._stale_or_building(key)
            # it finished while this call was giving up: the fresh result beats the stale one
        if isinstance(slot[0], BaseException):
            raise slot[0]
        return copy.deepcopy(slot[0])

    def _too_many(self, key: Any) -> dict[str, Any]:
        """No build runs for ``key`` and the flights are all busy: its last read marked stale, or a board that says to retry
        (it is not being built, so it is not "building"; the caller holds ``_lock``)."""
        if key in self._last:
            return self._stale_or_building(key)
        board = building_board()
        board["building"] = False
        board["unreadable"] = ["too many builds in flight"]
        board["note"] = "too many board builds in flight; retry"
        return board

    def _stale_or_building(self, key: Any) -> dict[str, Any]:
        """The last read of ``key`` marked stale, or the "building" board when it has none (the caller holds ``_lock``)."""
        last = copy.deepcopy(self._last.get(key))
        if last is None:
            return building_board()
        last["stale"] = True
        last["note"] = (last["note"] + "; " if last.get("note") else "") + "the board is being rebuilt: this is the last read"
        return last

    def _work(self, fn: Callable[[], dict[str, Any]], done: threading.Event, slot: list[Any], key: Any) -> None:
        try:
            value = fn()
            with self._lock:
                self._last[key] = copy.deepcopy(value)
                self._last.move_to_end(key)
                while len(self._last) > self.KEPT:
                    self._last.popitem(last=False)
            slot.append(value)
        except BaseException as exc:  # noqa: BLE001 - handed to the waiters, never lost on a thread
            slot.append(exc)
        finally:
            with self._lock:
                self._runs.pop(key, None)
            done.set()


def building_board() -> dict[str, Any]:
    """The board a caller is given while the first build of this process has not finished: no cards, and it says so."""
    return {"schema": 1, "generated": None, "queue": "unreadable", "unreadable": ["building"], "building": True,
            "note": "the board is being read for the first time (a cold read of the repos): this view refreshes when it is done",
            "band_hint": BAND_HINT, "journal_as_of_read": True, "off_count": 0,
            "columns": [{"category": c, "count": 0, "cards": []} for c in CATEGORIES]}
