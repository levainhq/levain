"""The Keep and legacy migration (task surfaces TS-4): ``levain intentions keep-migrate``.

The Keep is retired (TS-1): a new ``note`` or ``handoff`` is refused at every write seam, but legacy
rows still sit in a store, and so do undated agendas (which can never germinate). This module moves
every one of them to its home, in three separate acts that never share a write:

* :func:`plan` (the DRY RUN, and the default): read ``spores.json`` WITHOUT touching it (no anneal
  load: anneal's keyed read backfills origin keys and saves, which would change bytes), classify
  every open note / handoff / undated agenda, and write ``<migrations_dir>/keep-<ts>.json`` holding
  the full text, notes, origin key, the whole stored row, the proposed destination and WHY. The
  operator reads and may edit the plan (``destination.kind`` / ``episode_type`` / ``body``).
* :func:`apply` (``--apply <plan>``): per item, write the destination, record its id in the plan
  (atomic replace), and only THEN compost the original with the plan path in a note. A crash between
  any two steps leaves the original open; a re-run resumes (every destination write is keyed on
  ``plan id + origin key``, so it lands once).
* :func:`revert` (``--revert <plan>``): per item, re-add the original FIRST (same text, notes,
  disposition, created, seen) and only then remove the destination (an episode is tombstoned through
  anneal's ``delete``; an intention destination is composted). Re-adding a retired disposition cannot go
  through the guarded store (that is the point of the guard), so revert alone uses anneal's plain
  ``IntentionStore``; the plan file is the operator's act of restoring his own rows.

Nothing is silently lost: a note the classifier cannot place becomes an ``observation`` episode
(T3 (b) as the default for anything unclassified). The classifier is deterministic and conservative
(``deterministic before a model call``); the plan is where a model pass (T3 (a)) or the operator
overrides a destination, before he approves it once.

Frame names (FRAME.md TS-4) against this tree: ``descend(kind="composted")``, ``origin_key`` and the
disposition vocab match anneal 1.0.3 / ``levain.intentions`` exactly. There is no existing "write an
observation episode" helper to reuse beyond the raw ``anneal_memory.Store.record`` used by
``levain.dashboard._record_state_episode``; that is what this module calls.

Safety rules (L3 1107-63):

* ``apply`` and ``revert`` hold an exclusive ``flock`` on ``<plan>.lock`` for the whole run and load
  the plan under it, so two runs on one plan cannot interleave (a second is refused, nothing written).
  A plan name is reserved with ``O_CREAT|O_EXCL``, so a planner never replaces an earlier plan.
* Staleness compares the WHOLE row (anneal's ``intention_version``: every field but ``seen`` and
  ``origin_key``, so ``next``, ``target``, tier, ... all count), with this plan's own marker note set
  aside so a resumed apply still matches. The marker update and the compost are compare-and-set on
  that version (``expected_version``) inside anneal's lock: an edit landing after the compare makes
  the compost refuse instead of composting newer content.
* An item whose destination exists (recorded, or found by its key after a crash before the record)
  but whose original can no longer be composted (edited, vanished, resolved by someone else) goes to
  ``conflict``, never ``stale``: its destination stays on record and ``revert`` removes it, and the
  original is left exactly as it is (never resurrected).
* ``revert`` re-adds an original under a revert key and records its id before touching the
  destination; a re-run finds the key and never re-adds or re-writes it.
* Every keyed intention this module plants (a destination seed / agenda, a restored original) is created
  WITH its verbatim notes / created / seen in one store transaction (:func:`_add_verbatim`), so no
  keyed row ever exists half-made, and a keyed row found on a retry is left exactly as found (an
  edit made to it after a crash is never re-written; apply names it in ``Report.kept``).
* ``revert`` marks an item ``reverted`` only when it finished. A destination edited mid-revert (the
  compare-and-set refused) or an original no longer in the store (never re-added, and its destination
  kept so its content stays) leaves the item ``revert_blocked``, named in the report, and a later
  ``revert`` retries it.

Known edges (the reachability line: no real caller reaches these, or they cost no data):

* ``_INTENT`` reads the first line only: an intention on a later line classifies as an observation,
  which loses nothing.
* :func:`resolve_destination` needs ``episodic_db`` passed to resolve an episode (a test/report helper).
* ``plan`` does not infer ``episodic_db`` from the intentions path; ``apply`` refuses cleanly without it.
* A planner killed between reserving the name and writing leaves an empty ``keep-<ts>.json`` that
  ``load_plan`` refuses by name; delete it.
* A compare-and-set that refuses after the marker note landed leaves that note on the (still open)
  original; it is text only.
* An episode destination whose plan body was edited between a crash and the retry is refused by
  anneal (``OriginKeyConflict``) rather than written twice; restore the body or re-plan.
* The doctor line says "1 handoffs": it is the frame's exact string.
* :func:`_add_verbatim` calls anneal's private ``_prepare_add`` / ``_find_by_origin_key`` /
  ``_add_locked`` (anneal 1.0.3's own ``add``, split so the verbatim fields commit with the create);
  an anneal that renames them fails loudly (AttributeError), never silently.
* A ``revert_blocked`` destination retried by a later ``revert`` is composted as it then stands
  (the operator's second revert is the act); a compost is not a delete, so the edit stays readable.
* An original removed outside the plan keeps its item ``revert_blocked`` on every revert until the
  operator settles it by hand (the destination holds the content).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from levain.intention_legacy import (
    KEEP_EPISODE_META_KEY,
    KEEP_PLAN_DEST_KIND,
    KEEP_PLAN_ID_KEY,
    KEEP_PLAN_STORE_KEY,
)

try:
    import fcntl
except ImportError:                              # pragma: no cover - Windows: guarded writes are refused anyway
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "KeepMigrateError",
    "PLAN_KIND",
    "EPISODE_SOURCE",
    "EPISODE_TAGS",
    "MIGRATIONS_SUBDIR",
    "pending_counts",
    "doctor_line",
    "plan",
    "apply",
    "revert",
    "load_plan",
    "Report",
    "resolve_destination",
    "classify_note",
    "durable_fact_line",
]

PLAN_KIND = "keep-migrate"
PLAN_SCHEMA = 1
MIGRATIONS_SUBDIR = "migrations"          # <install>/.levain/migrations/keep-<ts>.json
EPISODE_SOURCE = "levain-keep-migrate"
EPISODE_TAGS = ("keep-migrated", "durable-fact")
DECISION_TAGS = ("keep-migrated", "decision")
NOTE_MARK = "keep-migrate plan "
_KEY_PREFIX = "levain.keepmig"
_REVERT_PREFIX = "levain.keepmig.rv"

STATE_PENDING = "pending"
STATE_DEST = "destination_written"
STATE_APPLIED = "applied"
STATE_REVERTED = "reverted"
STATE_STALE = "stale"                       # the row changed (or vanished) since the plan: left alone, reported
STATE_CONFLICT = "conflict"                 # as stale, but a destination exists: kept on record, revert removes it
STATE_REVERT_BLOCKED = "revert_blocked"     # revert could not finish it (destination edited mid-revert, original
#                                             gone): never `reverted`, `revert_from` keeps its prior state, retried

DEST_EPISODE = "episode"
DEST_INTENTION = KEEP_PLAN_DEST_KIND     # a persisted plan key: a plan an older release wrote still names it
EPISODE_TYPES = ("observation", "decision")
INTENTION_DISPOSITIONS = ("seed", "agenda")

Fault = Callable[[str, Mapping[str, Any]], None]


class KeepMigrateError(RuntimeError):
    """A migration step that cannot proceed safely (nothing is half-done: the message says what)."""


# ---------------------------------------------------------------- reading the store, read-only

def _read_doc(intentions_json: Path) -> dict[str, Any]:
    """The intentions document, parsed and NEVER written (anneal's own keyed read saves; this must not)."""
    try:
        with open(intentions_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"spores": [], "resolved": []}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeepMigrateError(f"{intentions_json} is unreadable ({exc}); inspect it by hand") from exc
    if not isinstance(data, dict) or not isinstance(data.get("spores", []), list):
        raise KeepMigrateError(f"{intentions_json} is not an intentions document; inspect it by hand")
    data.setdefault("spores", [])
    data.setdefault("resolved", [])
    return data


def _disp(row: Mapping[str, Any]) -> str:
    d = row.get("disposition")
    return d if isinstance(d, str) else ""


def _is_candidate(row: Mapping[str, Any]) -> bool:
    d = _disp(row)
    if d in ("note", "handoff"):
        return True
    return d == "agenda" and not row.get("next")


def _notes(row: Mapping[str, Any]) -> list[str]:
    n = row.get("notes")
    return [x for x in n if isinstance(x, str)] if isinstance(n, list) else []


def pending_counts(intentions_json: Path | str) -> dict[str, int]:
    """What awaits migration, for ``levain doctor`` and the one-line ``levain update`` print:
    ``{"notes": N, "handoffs": M, "agendas": K}`` (K = undated agendas, which migrate to seeds).
    Read-only; a missing store counts zero."""
    out = {"notes": 0, "handoffs": 0, "agendas": 0}
    for row in _read_doc(Path(intentions_json))["spores"]:
        if not isinstance(row, dict) or not _is_candidate(row):
            continue
        d = _disp(row)
        out["notes" if d == "note" else "handoffs" if d == "handoff" else "agendas"] += 1
    return out


def doctor_line(intentions_json: Path | str) -> str | None:
    """The doctor string (``keep: N notes / M handoffs await migration``), or None when nothing waits.
    Undated agendas ride in the handoffs count's sentence only when present, so the frame's string is
    exact when there are none."""
    c = pending_counts(intentions_json)
    if not (c["notes"] or c["handoffs"] or c["agendas"]):
        return None
    line = f"keep: {c['notes']} notes / {c['handoffs']} handoffs await migration"
    if c["agendas"]:
        line += f" (+ {c['agendas']} undated agendas to seeds)"
    return line


# ---------------------------------------------------------------- the classifier (deterministic)

_RULING = re.compile(
    r"\b(ruled|ruling|decided|decision|approved|rejected|chose|agreed|settled|we will|we won't|"
    r"never again|from now on|standing order)\b", re.I)
_INTENT = re.compile(
    r"^\s*(?:[-*]\s*)?(?:todo\b|to do\b|to-do\b|remember to\b|remind me\b|need to\b|needs to\b|should\b|"
    r"follow up\b|follow-up\b|check\b|look into\b|revisit\b|try\b|ask\b|call\b|email\b|send\b|"
    r"fix\b|finish\b|build\b|write\b|decide\b|figure out\b)", re.I)
_FACT_HINT = re.compile(r"(`|\$ |\bhttps?://|--[a-z]|\bgotcha\b|\bnever\b|\balways\b|=|/)", re.I)


def classify_note(text: str) -> tuple[str, str]:
    """``(destination, why)`` for a Keep note: ``decision`` | ``seed`` | ``observation``. Ruling beats
    intention; anything the cues do not place is an ``observation`` (nothing is lost, T3 (b))."""
    if _RULING.search(text):
        return "decision", "wording of a ruling (ruled/decided/approved/...): kept as a decision episode, his words as written"
    if _INTENT.search(text):
        return "seed", "reads as an intention (starts with a to-do verb): re-planted as a seed"
    if _FACT_HINT.search(text):
        return "observation", "a fact, command or gotcha: an observation episode in Durable Facts form for the next wrap to place"
    return "observation", "unplaced by the classifier: kept whole as an observation episode, nothing lost"


_STOP = frozenset(
    "the a an and or of to in on for with is are was were be been it this that these those at by as from "
    "not no do does did can will would should could has have had but if then than so we you i they he she "
    "its our your their".split())


def _cues(text: str, n: int = 5) -> list[str]:
    seen: list[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{3,}", text):
        lw = w.lower().strip(".-_")
        if lw and lw not in _STOP and lw not in seen:
            seen.append(lw)
    seen.sort(key=lambda w: -len(w))     # stable: longest first, ties keep reading order
    return seen[:n]


def durable_fact_line(text: str) -> str:
    """``- <fact> — cues: a, b, c`` (the ``## Durable Facts`` form, T2/T4)."""
    one = " ".join(text.split())
    cues = _cues(text)
    return f"- {one} — cues: {', '.join(cues)}" if cues else f"- {one}"


def _episode_body(item: Mapping[str, Any], etype: str) -> str:
    text = item["text"]
    when = str(item.get("created") or "")[:10]
    notes = item.get("notes") or []
    tail = ("\n\nnotes on the item:\n" + "\n".join(f"- {n}" for n in notes)) if notes else ""
    if etype == "decision":
        return (f"Decision carried out of the Keep (migrated {when or 'undated'}; his words as written):\n"
                f"{text}{tail}\n\nPremise: as written in the note above.")
    return f"{durable_fact_line(text)}{tail}"


def _propose(row: Mapping[str, Any], *, handoff_to: str, agenda_date: str | None
             ) -> tuple[dict[str, Any], str]:
    """The destination dict and the reason for one candidate row."""
    d = _disp(row)
    text = str(row.get("text", ""))
    if d == "note":
        kind, why = classify_note(text)
        if kind == "seed":
            return {"kind": DEST_INTENTION, "disposition": "seed", "next": None}, why
        return {"kind": DEST_EPISODE, "episode_type": kind}, why
    if d == "handoff":
        if handoff_to == "agenda":
            return ({"kind": DEST_INTENTION, "disposition": "agenda", "next": agenda_date},
                    f"legacy handoff to a dated agenda ({agenda_date}), so it reaches him once as a band row (T10b)")
        return ({"kind": DEST_INTENTION, "disposition": "seed", "next": None},
                "legacy handoff to a seed: the agent sees it in the Tray at session start (T10a)")
    return ({"kind": DEST_INTENTION, "disposition": "seed", "next": None},
            "undated agenda never germinates: becomes a seed (SD-15, decided)")


# ---------------------------------------------------------------- the plan file

def _utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _atomic_write(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:                                     # the rename itself durable before the next store write
            dfd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dfd = None
        if dfd is not None:
            try:
                os.fsync(dfd)
            except OSError:
                pass
            finally:
                os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_plan(plan_path: Path | str) -> dict[str, Any]:
    p = Path(plan_path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KeepMigrateError(f"cannot read plan {p}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("kind") != PLAN_KIND or not isinstance(doc.get("items"), list):
        raise KeepMigrateError(f"{p} is not a {PLAN_KIND} plan")
    if doc.get("schema") != PLAN_SCHEMA:
        raise KeepMigrateError(f"{p}: plan schema {doc.get('schema')!r} is not {PLAN_SCHEMA}")
    return doc


def plan(intentions_json: Path | str, *, migrations_dir: Path | str, episodic_db: Path | str | None = None,
         handoff_to: str = "seed", agenda_date: str | None = None,
         overrides: Mapping[str, Mapping[str, Any]] | None = None,
         now: datetime | None = None) -> Path:
    """The DRY RUN. Writes the plan file and returns its path; ``intentions_json`` is read, never written.

    ``overrides`` maps an item's origin key (or intention id) to a destination dict, for a model pass or a
    test (T3 (a)); the operator can equally edit the file. ``handoff_to="agenda"`` needs ``agenda_date``
    (``YYYY-MM-DD``; the upgrade day), else handoffs become seeds."""
    intentions_json = Path(intentions_json)
    if handoff_to not in ("seed", "agenda"):
        raise KeepMigrateError("handoff_to must be 'seed' or 'agenda'")
    if handoff_to == "agenda":
        try:
            date.fromisoformat(agenda_date or "")
        except ValueError as exc:
            raise KeepMigrateError("handoff_to='agenda' needs agenda_date as YYYY-MM-DD") from exc
    doc = _read_doc(intentions_json)
    stamp = _utc_stamp(now)
    items: list[dict[str, Any]] = []
    for row in doc["spores"]:
        if not isinstance(row, dict) or not _is_candidate(row):
            continue
        dest, why = _propose(row, handoff_to=handoff_to, agenda_date=agenda_date)
        okey = row.get("origin_key") if isinstance(row.get("origin_key"), str) and row.get("origin_key") else None
        ov = (overrides or {}).get(okey or "") or (overrides or {}).get(str(row.get("id")))
        if ov:
            dest = {k: v for k, v in dict(ov).items() if k != "why"}
            why = str(ov.get("why") or "destination set by override")
        item: dict[str, Any] = {
            "index": len(items),
            KEEP_PLAN_ID_KEY: row.get("id"),
            "origin_key": okey,
            "disposition": _disp(row),
            "text": row.get("text", ""),
            "notes": _notes(row),
            "created": row.get("created"),
            "row": row,                       # the whole stored row: revert restores from this
            "destination": dest,
            "why": why,
            "state": STATE_PENDING,
            "result": None,
        }
        if dest.get("kind") == DEST_EPISODE:
            etype = dest.get("episode_type")
            if etype not in EPISODE_TYPES:     # an override's shape, judged before it is used
                raise KeepMigrateError(f"item {item['index']}: an episode destination needs episode_type "
                                       f"{EPISODE_TYPES} (got {etype!r})")
            item["destination"]["body"] = dest.get("body") or _episode_body(item, etype)
            item["destination"]["tags"] = list(dest.get("tags") or (
                EPISODE_TAGS if etype == "observation" else DECISION_TAGS))
        _validate_dest(item)                   # a plan that cannot be applied is never written
        items.append(item)
    counts = {"items": len(items), "notes": 0, "handoffs": 0, "agendas": 0}
    for i in items:                            # from the items themselves: no second read to drift
        d = i["disposition"]
        counts["notes" if d == "note" else "handoffs" if d == "handoff" else "agendas"] += 1
    out = _reserve_plan_path(Path(migrations_dir), stamp)
    _atomic_write(out, {
        "kind": PLAN_KIND, "schema": PLAN_SCHEMA, "plan_id": out.stem,
        "created": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        KEEP_PLAN_STORE_KEY: str(intentions_json),
        "episodic_db": str(episodic_db) if episodic_db is not None else None,
        "options": {"handoff_to": handoff_to, "agenda_date": agenda_date},
        "counts": counts,
        "items": items,
    })
    return out


def _reserve_plan_path(migrations_dir: Path, stamp: str) -> Path:
    """``keep-<stamp>[-n].json``, claimed with ``O_CREAT|O_EXCL`` so two planners in one second never
    share (and so replace) a name; the caller's atomic write then fills the reserved file."""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        out = migrations_dir / (f"keep-{stamp}.json" if n == 1 else f"keep-{stamp}-{n}.json")
        try:
            fd = os.open(str(out), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return out


@contextlib.contextmanager
def _plan_lock(plan_path: Path) -> Iterator[None]:
    """An exclusive, non-blocking ``flock`` on ``<plan>.lock`` for one apply / revert run. The lock
    file is never unlinked (unlinking a lock file races a waiter onto a different inode)."""
    if fcntl is None:
        raise KeepMigrateError("keep-migrate apply/revert needs a POSIX file lock (fcntl); none here")
    lock = plan_path.with_name(plan_path.name + ".lock")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise KeepMigrateError(f"cannot open the plan lock {lock}: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise KeepMigrateError(
                f"another keep-migrate apply/revert holds {plan_path}; nothing was changed") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- apply / revert

def _item_key(item: Mapping[str, Any]) -> str:
    return item.get("origin_key") or "id:" + str(item.get(KEEP_PLAN_ID_KEY))


def _dest_key(plan_id: str, item: Mapping[str, Any]) -> str:
    h = hashlib.sha256(f"{plan_id}\n{_item_key(item)}".encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{h}"


def _revert_key(plan_id: str, item: Mapping[str, Any]) -> str:
    h = hashlib.sha256(f"{plan_id}\n{_item_key(item)}".encode("utf-8")).hexdigest()
    return f"{_REVERT_PREFIX}:{h}"


def _find_row(store: Any, item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The intention (open or resolved) this item was planned from, by origin key, else id."""
    key = item.get("origin_key")
    if key:
        try:
            row = store.get_by_origin_key(key)
        except Exception:                       # noqa: BLE001 - a key outside the grammar falls back to the id
            row = None
        if row is not None:
            return row
    sid = item.get(KEEP_PLAN_ID_KEY)
    return store.get(sid) if sid else None


def _add_verbatim(store: Any, *, origin_key: str, row: Mapping[str, Any], text: str,
                  disposition: str, next_: str | None, guard: bool) -> tuple[dict[str, Any], bool]:
    """``(intention, created)``: plant ``row``'s content under ``origin_key`` WITH its notes / created /
    seen verbatim, in ONE locked transaction (anneal's ``add`` mints fresh notes and dates, and a note
    added through ``update`` is re-stamped). There is no moment a keyed row exists without its
    verbatim fields, so a keyed row found on a retry is always complete, and it is returned untouched
    (``created`` False): whatever anyone did to it since is theirs and is never re-written.

    This is anneal's own ``add`` (``_prepare_add`` before the lock, the keyed lookup and
    ``_add_locked`` inside it) with the three fields set before the commit. ``guard`` applies the
    retired-disposition guard the guarded store's ``add`` would (revert alone restores a retired
    one, through anneal's plain store)."""
    if guard:
        from levain.intentions import _refuse_unwritable

        _refuse_unwritable(disposition)
    fields = store._prepare_add(                  # noqa: SLF001 - see the docstring
        type=row.get("type", "thought"), text=text, domain=row.get("domain") or "",
        tier=row.get("tier") or "warm", salience=int(row.get("salience") or 0), next=next_,
        pointer=row.get("pointer"), disposition=disposition, target=row.get("target") or None,
        attention=row.get("attention") or None, germinated=row.get("germinated") or None)
    with store._transaction() as data:            # noqa: SLF001
        existing = store._find_by_origin_key(data, origin_key)   # noqa: SLF001
        if existing is not None:
            return dict(existing), False
        item = store._add_locked(data, fields, origin_key)      # noqa: SLF001
        item["notes"] = list(_notes(row))
        if isinstance(row.get("created"), str):
            item["created"] = row["created"]
        if isinstance(row.get("seen"), str):
            item["seen"] = row["seen"]
        return dict(item), True


def _episode_store(doc: Mapping[str, Any], episodic_db: Path | str | None) -> Path:
    raw = episodic_db if episodic_db is not None else doc.get("episodic_db")
    p = Path(raw) if raw else None
    if p is None or not p.is_file():
        raise KeepMigrateError(
            "this plan writes episodes but the episode store was not found "
            f"({p}); nothing was changed. Pass the install's .levain/memory.db")
    return p


def _needs_episodes(doc: Mapping[str, Any], states: Sequence[str]) -> bool:
    return any(i.get("destination", {}).get("kind") == DEST_EPISODE and i.get("state") in states
               for i in doc["items"])


def _validate_dest(item: Mapping[str, Any]) -> None:
    d = item.get("destination") or {}
    k = d.get("kind")
    if k == DEST_EPISODE:
        if d.get("episode_type") not in EPISODE_TYPES or not str(d.get("body") or "").strip():
            raise KeepMigrateError(f"item {item.get('index')}: an episode destination needs episode_type "
                                   f"{EPISODE_TYPES} and a body")
    elif k == DEST_INTENTION:
        if d.get("disposition") not in INTENTION_DISPOSITIONS:
            raise KeepMigrateError(f"item {item.get('index')}: an intention destination must be one of {INTENTION_DISPOSITIONS}")
        if d["disposition"] == "agenda":
            try:
                date.fromisoformat(str(d.get("next") or ""))
            except ValueError as exc:
                raise KeepMigrateError(f"item {item.get('index')}: an agenda destination needs a next date") from exc
    else:
        raise KeepMigrateError(f"item {item.get('index')}: unknown destination kind {k!r}")


def _write_destination(item: dict[str, Any], plan_id: str, intentions: Any, db: Path | None
                       ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``(result, found)``: the destination's record, and the keyed intention a crashed run had already
    written (None when this call made it, and for an episode)."""
    d = item["destination"]
    key = _dest_key(plan_id, item)
    if d["kind"] == DEST_EPISODE:
        from anneal_memory import Store

        assert db is not None
        with Store(str(db)) as st:
            ep = st.record(
                content=d["body"], episode_type=d["episode_type"], source=EPISODE_SOURCE,
                metadata={"tags": list(d.get("tags") or EPISODE_TAGS), "plan": plan_id,
                          KEEP_EPISODE_META_KEY: item.get(KEEP_PLAN_ID_KEY)},
                origin_key=key)
        return {"kind": DEST_EPISODE, "id": ep.id, "origin_key": key}, None
    made, created = _add_verbatim(intentions, origin_key=key, row=item["row"], text=item["text"],
                                  disposition=d["disposition"], next_=d.get("next") or None, guard=True)
    return {"kind": DEST_INTENTION, "id": made["id"], "origin_key": key}, (None if created else made)


def _marker_notes_out(row: Mapping[str, Any], plan_id: str) -> dict[str, Any]:
    """``row`` without this plan's own marker note (a resumed apply added it), a missing and an
    emptied ``notes`` alike."""
    out = dict(row)
    notes = [n for n in _notes(row) if not (NOTE_MARK in n and plan_id in n)]
    if notes:
        out["notes"] = notes
    else:
        out.pop("notes", None)
    return out


def _content_version(row: Mapping[str, Any], plan_id: str) -> str:
    """anneal's whole-row version (every field the destination depends on, ``next`` included; ``seen``
    and ``origin_key`` excluded by anneal), this plan's marker note set aside."""
    from anneal_memory.spores import intention_version

    return intention_version(_marker_notes_out(row, plan_id))     # type: ignore[arg-type]


def _marked(row: Mapping[str, Any], plan_id: str) -> bool:
    return any(NOTE_MARK in n and plan_id in n for n in _notes(row))


def _composted_by_us(row: Mapping[str, Any], plan_id: str) -> bool:
    res = row.get("resolution")
    return (row.get("status") != "open" and isinstance(res, Mapping) and res.get("kind") == "composted"
            and _marked(row, plan_id))


def _locate_destination(item: Mapping[str, Any], plan_id: str, intentions: Any, db: Path | None
                        ) -> dict[str, Any] | None:
    """The destination this item wrote: the recorded result, else a lookup by its deterministic key
    (a crash after the write, before the record). None when none was written (or it is unreadable
    because the episode store is absent)."""
    if item.get("result") and item["result"].get("id"):
        return dict(item["result"])
    kind = (item.get("destination") or {}).get("kind")
    key = _dest_key(plan_id, item)
    if kind == DEST_EPISODE:
        if db is None:
            return None
        from anneal_memory import Store

        with Store(str(db)) as st:
            ep = st.get_by_origin_key(key)
        return {"kind": DEST_EPISODE, "id": ep.id, "origin_key": key} if ep is not None else None
    if kind == DEST_INTENTION:
        sp = intentions.get_by_origin_key(key)
        return {"kind": DEST_INTENTION, "id": sp["id"], "origin_key": key} if sp is not None else None
    return None


def resolve_destination(result: Mapping[str, Any] | None, *, intentions_json: Path | str,
                        episodic_db: Path | str | None = None) -> bool:
    """True iff a recorded destination id resolves in the live stores (an episode by its id, an intention by
    its id, open or resolved)."""
    if not result or not result.get("id"):
        return False
    if result["kind"] == DEST_EPISODE:
        if episodic_db is None or not Path(episodic_db).is_file():
            return False
        from anneal_memory import Store

        with Store(str(episodic_db)) as st:
            return st.get(result["id"]) is not None
    from levain.intentions import guarded_intention_store

    return guarded_intention_store(intentions_json).get(result["id"]) is not None


@dataclass
class Report:
    plan_path: Path
    applied: list[int] = field(default_factory=list)
    reverted: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)   # (index, reason): left as they were
    kept: list[tuple[int, str]] = field(default_factory=list)      # (index, reason): applied, but a row
    # a crashed run wrote was found edited since and left exactly as found


def apply(plan_path: Path | str, *, episodic_db: Path | str | None = None,
          fault: Fault | None = None, today: date | None = None) -> Report:
    """``--apply <plan>``. Idempotent and resumable: an item already applied is skipped, one whose
    destination is written but whose original is still open resumes at the compost. ``fault`` is a
    test seam, called as ``fault(stage, item)`` at ``after_destination`` and ``after_record``.

    Held under the plan lock throughout (a second run on the same plan is refused). An item whose
    original changed since the plan is left alone: ``stale`` if nothing was written for it,
    ``conflict`` (destination kept on record for ``revert``) if its destination already exists."""
    from anneal_memory.spores import IntentionError, intention_version
    from levain.intentions import guarded_intention_store

    pp = Path(plan_path).resolve()
    with _plan_lock(pp):
        doc = load_plan(pp)                    # read under the lock: never a copy another run is rewriting
        intentions_json = Path(doc[KEEP_PLAN_STORE_KEY])
        plan_id = doc["plan_id"]
        rep = Report(pp)
        live = (STATE_PENDING, STATE_DEST, STATE_STALE)
        for it in doc["items"]:
            if it["state"] in live:
                _validate_dest(it)             # every item judged before the first write
        db = _episode_store(doc, episodic_db) if _needs_episodes(doc, live) else None
        intentions = guarded_intention_store(intentions_json)

        def leave(it: dict[str, Any], why: str) -> None:
            found = _locate_destination(it, plan_id, intentions, db)
            if found is not None:
                it["result"] = found
                it["state"] = STATE_CONFLICT
                why += (f"; its destination ({found['kind']} {found['id']}) is kept on record and "
                        "--revert removes it")
            else:
                it["state"] = STATE_STALE
            _atomic_write(pp, doc)
            rep.skipped.append((it["index"], why))

        for it in doc["items"]:
            idx = it["index"]
            if it["state"] not in live:
                continue
            row = _find_row(intentions, it)
            if row is None:
                leave(it, "the item is no longer in the store")
                continue
            if row.get("status") != "open":
                if it["state"] == STATE_DEST and _composted_by_us(row, plan_id):
                    it["state"] = STATE_APPLIED    # crashed after the compost, before the state flip
                    _atomic_write(pp, doc)
                    rep.applied.append(idx)
                else:
                    leave(it, "already resolved since the plan was made")
                continue
            if _content_version(row, plan_id) != _content_version(it["row"], plan_id):
                leave(it, "edited since the plan was made; re-run the dry run")
                continue
            result, found = _write_destination(it, plan_id, intentions, db)
            if found is not None and (_notes(found) != _notes(it["row"]) or found.get("status") != "open"):
                rep.kept.append((idx, f"destination intention {found['id']} was changed after a crashed run "
                                      "wrote it; left exactly as found"))
            if fault:
                fault("after_destination", it)
            it["result"] = result
            it["state"] = STATE_DEST
            _atomic_write(pp, doc)             # the destination id is on disk BEFORE the original goes
            if fault:
                fault("after_record", it)
            marker = f"{NOTE_MARK}{pp}"
            try:                               # compare-and-set on the version just compared
                cur = row
                if not _marked(row, plan_id):
                    cur = intentions.update(row["id"], add_note=f"migrated out of the Keep by {marker}",
                                        expected_version=intention_version(row), today=today)
                intentions.descend(row["id"], kind="composted", expect_disposition=it["disposition"],
                               expected_version=intention_version(cur), today=today)
            except IntentionError:
                leave(it, "changed while being applied; the original is left as it now is")
                continue
            it["state"] = STATE_APPLIED
            _atomic_write(pp, doc)
            rep.applied.append(idx)
        _atomic_write(pp, doc)
        return rep


def revert(plan_path: Path | str, *, episodic_db: Path | str | None = None,
           fault: Fault | None = None, today: date | None = None) -> Report:
    """``--revert <plan>``. Per applied (or half-applied) item: re-add the original (same text, notes,
    disposition), THEN remove what the apply made (tombstone the episode / compost the seed).

    A ``conflict`` item (destination written, original changed) and a ``pending`` / ``stale`` one whose
    destination is found by its key (a crash before the record) get their destination removed and
    their original left exactly as it is. An original resolved by someone other than this plan, or no
    longer in the store, is never resurrected. An item revert cannot finish goes ``revert_blocked``
    (never ``reverted``) and is retried next time. Held under the plan lock throughout."""
    from anneal_memory.spores import IntentionError, IntentionStore, intention_version
    from levain.intentions import NEW_ID_PREFIX, guarded_intention_store

    pp = Path(plan_path).resolve()
    with _plan_lock(pp):
        doc = load_plan(pp)
        plan_id = doc["plan_id"]
        intentions_json = Path(doc[KEEP_PLAN_STORE_KEY])
        restore_states = (STATE_APPLIED, STATE_DEST)
        if _needs_episodes(doc, restore_states + (STATE_CONFLICT, STATE_REVERT_BLOCKED)):
            db: Path | None = _episode_store(doc, episodic_db)
        elif _needs_episodes(doc, (STATE_PENDING, STATE_STALE)):
            try:                               # only a probe for an unrecorded crash-time write
                db = _episode_store(doc, episodic_db)
            except KeepMigrateError:
                db = None
        else:
            db = None
        guarded = guarded_intention_store(intentions_json)       # first: an anneal below the floor gets its clear error here
        plain = IntentionStore(intentions_json, id_prefix=NEW_ID_PREFIX)         # ONLY for restoring a retired disposition (see module doc)
        rep = Report(pp)

        def block(it: dict[str, Any], base: str, why: str) -> None:
            it["state"] = STATE_REVERT_BLOCKED  # never `reverted`: a later --revert retries it
            it["revert_from"] = base
            _atomic_write(pp, doc)
            rep.skipped.append((it["index"], why + "; a later --revert retries it"))

        for it in doc["items"]:
            idx = it["index"]
            if it["state"] == STATE_REVERTED:
                continue
            # a blocked item is retried as the state it was blocked from
            base = ((it.get("revert_from") or STATE_DEST) if it["state"] == STATE_REVERT_BLOCKED
                    else it["state"])
            res = _locate_destination(it, plan_id, guarded, db)
            restore = base in restore_states
            if not restore and res is None:
                rep.skipped.append((idx, f"state {it['state']}: nothing was applied"))
                continue
            if restore:
                rev_key = _revert_key(plan_id, it)
                again = plain.get_by_origin_key(rev_key)
                if again is not None:          # restored already (created whole, see _add_verbatim): left as it is
                    it["restored_id"] = again["id"]
                else:
                    orig = _find_row(plain, it)
                    if orig is None:           # removed outside this plan: never assumed ours, never resurrected
                        block(it, base, "the original is no longer in the store (removed outside this plan); "
                                        "not re-added, and its destination is kept so its content stays")
                        continue
                    if orig.get("status") == "open":
                        pass                   # never composted (a crash before the compost)
                    elif not (base == STATE_APPLIED or _composted_by_us(orig, plan_id)):
                        rep.skipped.append((idx, "the original was resolved outside this plan; not re-added"))
                    else:
                        made, _ = _add_verbatim(plain, origin_key=rev_key, row=it["row"], text=it["text"],
                                                disposition=it["disposition"],
                                                next_=it["row"].get("next") or None, guard=False)
                        it["restored_id"] = made["id"]
                _atomic_write(pp, doc)         # the restore is on record before the destination goes
            if fault:
                fault("after_restore", it)
            if res is not None and res.get("kind") == DEST_EPISODE:
                from anneal_memory import Store

                assert db is not None
                with Store(str(db)) as st:
                    if st.get(res["id"]) is not None:
                        st.delete(res["id"])
            elif res is not None and res.get("kind") == DEST_INTENTION:
                dest = guarded.get(res["id"])
                if dest is not None and dest.get("status") == "open":
                    try:                       # compare-and-set: an edit landing mid-revert is not composted
                        cur = guarded.update(dest["id"], add_note=f"reverted by {NOTE_MARK}{pp}",
                                             expected_version=intention_version(dest), today=today)
                        guarded.descend(dest["id"], kind="composted",
                                        expected_version=intention_version(cur), today=today)
                    except IntentionError:
                        block(it, base, f"destination {dest['id']} changed during the revert; left open")
                        continue
            it["state"] = STATE_REVERTED
            it.pop("revert_from", None)
            _atomic_write(pp, doc)
            rep.reverted.append(idx)
        _atomic_write(pp, doc)
        return rep
