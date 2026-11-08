"""The Keep and legacy migration (task surfaces TS-4): ``levain spores keep-migrate``.

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
  anneal's ``delete``; a spore destination is composted). Re-adding a retired disposition cannot go
  through the guarded store (that is the point of the guard), so revert alone uses anneal's plain
  ``SporeStore``; the plan file is the operator's act of restoring his own rows.

Nothing is silently lost: a note the classifier cannot place becomes an ``observation`` episode
(T3 (b) as the default for anything unclassified). The classifier is deterministic and conservative
(``deterministic before a model call``); the plan is where a model pass (T3 (a)) or the operator
overrides a destination, before he approves it once.

Frame names (FRAME.md TS-4) against this tree: ``descend(kind="composted")``, ``origin_key`` and the
disposition vocab match anneal 1.0.3 / ``levain.spores`` exactly. There is no existing "write an
observation episode" helper to reuse beyond the raw ``anneal_memory.Store.record`` used by
``levain.dashboard._record_state_episode``; that is what this module calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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

DEST_EPISODE = "episode"
DEST_SPORE = "spore"
EPISODE_TYPES = ("observation", "decision")
SPORE_DISPOSITIONS = ("seed", "agenda")

Fault = Callable[[str, Mapping[str, Any]], None]


class KeepMigrateError(RuntimeError):
    """A migration step that cannot proceed safely (nothing is half-done: the message says what)."""


# ---------------------------------------------------------------- reading the store, read-only

def _read_doc(spores_json: Path) -> dict[str, Any]:
    """The spores document, parsed and NEVER written (anneal's own keyed read saves; this must not)."""
    try:
        with open(spores_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"spores": [], "resolved": []}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeepMigrateError(f"{spores_json} is unreadable ({exc}); inspect it by hand") from exc
    if not isinstance(data, dict) or not isinstance(data.get("spores", []), list):
        raise KeepMigrateError(f"{spores_json} is not a spores document; inspect it by hand")
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


def pending_counts(spores_json: Path | str) -> dict[str, int]:
    """What awaits migration, for ``levain doctor`` and the one-line ``levain update`` print:
    ``{"notes": N, "handoffs": M, "agendas": K}`` (K = undated agendas, which migrate to seeds).
    Read-only; a missing store counts zero."""
    out = {"notes": 0, "handoffs": 0, "agendas": 0}
    for row in _read_doc(Path(spores_json))["spores"]:
        if not isinstance(row, dict) or not _is_candidate(row):
            continue
        d = _disp(row)
        out["notes" if d == "note" else "handoffs" if d == "handoff" else "agendas"] += 1
    return out


def doctor_line(spores_json: Path | str) -> str | None:
    """The doctor string (``keep: N notes / M handoffs await migration``), or None when nothing waits.
    Undated agendas ride in the handoffs count's sentence only when present, so the frame's string is
    exact when there are none."""
    c = pending_counts(spores_json)
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
            return {"kind": DEST_SPORE, "disposition": "seed", "next": None}, why
        return {"kind": DEST_EPISODE, "episode_type": kind}, why
    if d == "handoff":
        if handoff_to == "agenda":
            return ({"kind": DEST_SPORE, "disposition": "agenda", "next": agenda_date},
                    f"legacy handoff to a dated agenda ({agenda_date}), so it reaches him once as a band row (T10b)")
        return ({"kind": DEST_SPORE, "disposition": "seed", "next": None},
                "legacy handoff to a seed: the agent sees it in the Tray at session start (T10a)")
    return ({"kind": DEST_SPORE, "disposition": "seed", "next": None},
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


def plan(spores_json: Path | str, *, migrations_dir: Path | str, episodic_db: Path | str | None = None,
         handoff_to: str = "seed", agenda_date: str | None = None,
         overrides: Mapping[str, Mapping[str, Any]] | None = None,
         now: datetime | None = None) -> Path:
    """The DRY RUN. Writes the plan file and returns its path; ``spores_json`` is read, never written.

    ``overrides`` maps an item's origin key (or spore id) to a destination dict, for a model pass or a
    test (T3 (a)); the operator can equally edit the file. ``handoff_to="agenda"`` needs ``agenda_date``
    (``YYYY-MM-DD``; the upgrade day), else handoffs become seeds."""
    spores_json = Path(spores_json)
    if handoff_to not in ("seed", "agenda"):
        raise KeepMigrateError("handoff_to must be 'seed' or 'agenda'")
    if handoff_to == "agenda":
        try:
            date.fromisoformat(agenda_date or "")
        except ValueError as exc:
            raise KeepMigrateError("handoff_to='agenda' needs agenda_date as YYYY-MM-DD") from exc
    doc = _read_doc(spores_json)
    stamp = _utc_stamp(now)
    items: list[dict[str, Any]] = []
    for row in doc["spores"]:
        if not isinstance(row, dict) or not _is_candidate(row):
            continue
        dest, why = _propose(row, handoff_to=handoff_to, agenda_date=agenda_date)
        okey = row.get("origin_key") if isinstance(row.get("origin_key"), str) and row.get("origin_key") else None
        ov = (overrides or {}).get(okey or "") or (overrides or {}).get(str(row.get("id")))
        if ov:
            dest, why = dict(ov), str(ov.get("why") or "destination set by override")
        item: dict[str, Any] = {
            "index": len(items),
            "spore_id": row.get("id"),
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
            item["destination"]["body"] = dest.get("body") or _episode_body(item, dest["episode_type"])
            item["destination"]["tags"] = list(dest.get("tags") or (
                EPISODE_TAGS if dest["episode_type"] == "observation" else DECISION_TAGS))
        items.append(item)
    out = Path(migrations_dir) / f"keep-{stamp}.json"
    n = 1
    while out.exists():                        # never overwrite an earlier plan
        n += 1
        out = Path(migrations_dir) / f"keep-{stamp}-{n}.json"
    _atomic_write(out, {
        "kind": PLAN_KIND, "schema": PLAN_SCHEMA, "plan_id": out.stem,
        "created": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "spores_json": str(spores_json),
        "episodic_db": str(episodic_db) if episodic_db is not None else None,
        "options": {"handoff_to": handoff_to, "agenda_date": agenda_date},
        "counts": {"items": len(items), **pending_counts(spores_json)},
        "items": items,
    })
    return out


# ---------------------------------------------------------------- apply / revert

def _item_key(item: Mapping[str, Any]) -> str:
    return item.get("origin_key") or f"id:{item.get('spore_id')}"


def _dest_key(plan_id: str, item: Mapping[str, Any]) -> str:
    h = hashlib.sha256(f"{plan_id}\n{_item_key(item)}".encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{h}"


def _revert_key(plan_id: str, item: Mapping[str, Any]) -> str:
    h = hashlib.sha256(f"{plan_id}\n{_item_key(item)}".encode("utf-8")).hexdigest()
    return f"{_REVERT_PREFIX}:{h}"


def _find_row(store: Any, item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The spore (open or resolved) this item was planned from, by origin key, else id."""
    key = item.get("origin_key")
    if key:
        try:
            row = store.get_by_origin_key(key)
        except Exception:                       # noqa: BLE001 - a key outside the grammar falls back to the id
            row = None
        if row is not None:
            return row
    sid = item.get("spore_id")
    return store.get(sid) if sid else None


def _restore_row_fields(store: Any, spore_id: str, row: Mapping[str, Any]) -> None:
    """Put the original's notes / created / seen back VERBATIM (anneal's ``add`` mints fresh ones and a
    note added through ``update`` is re-stamped). One locked transaction, so a reader never sees half."""
    with store._transaction() as data:           # noqa: SLF001 - no public setter for verbatim notes
        for it in data["spores"]:
            if it.get("id") == spore_id:
                it["notes"] = list(_notes(row))
                if isinstance(row.get("created"), str):
                    it["created"] = row["created"]
                if isinstance(row.get("seen"), str):
                    it["seen"] = row["seen"]
                return
    raise KeepMigrateError(f"spore {spore_id} vanished while restoring its fields")


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
    elif k == DEST_SPORE:
        if d.get("disposition") not in SPORE_DISPOSITIONS:
            raise KeepMigrateError(f"item {item.get('index')}: a spore destination must be one of {SPORE_DISPOSITIONS}")
        if d["disposition"] == "agenda":
            try:
                date.fromisoformat(str(d.get("next") or ""))
            except ValueError as exc:
                raise KeepMigrateError(f"item {item.get('index')}: an agenda destination needs a next date") from exc
    else:
        raise KeepMigrateError(f"item {item.get('index')}: unknown destination kind {k!r}")


def _write_destination(item: dict[str, Any], plan_id: str, spores: Any, db: Path | None) -> dict[str, Any]:
    d = item["destination"]
    key = _dest_key(plan_id, item)
    if d["kind"] == DEST_EPISODE:
        from anneal_memory import Store

        assert db is not None
        with Store(str(db)) as st:
            ep = st.record(
                content=d["body"], episode_type=d["episode_type"], source=EPISODE_SOURCE,
                metadata={"tags": list(d.get("tags") or EPISODE_TAGS), "plan": plan_id,
                          "spore": item.get("spore_id")},
                origin_key=key)
        return {"kind": DEST_EPISODE, "id": ep.id, "origin_key": key}
    row = item["row"]
    made = spores.add(
        type=row.get("type", "thought"), text=item["text"], domain=row.get("domain") or "",
        tier=row.get("tier") or "warm", salience=int(row.get("salience") or 0),
        next=d.get("next") or None, pointer=row.get("pointer"), disposition=d["disposition"],
        origin_key=key)
    if not made.get("notes") and _notes(row):    # a fresh seed; a retry that found it leaves it alone
        _restore_row_fields(spores, made["id"], row)
    return {"kind": DEST_SPORE, "id": made["id"], "origin_key": key}


def resolve_destination(result: Mapping[str, Any] | None, *, spores_json: Path | str,
                        episodic_db: Path | str | None = None) -> bool:
    """True iff a recorded destination id resolves in the live stores (an episode by its id, a spore by
    its id, open or resolved)."""
    if not result or not result.get("id"):
        return False
    if result["kind"] == DEST_EPISODE:
        if episodic_db is None or not Path(episodic_db).is_file():
            return False
        from anneal_memory import Store

        with Store(str(episodic_db)) as st:
            return st.get(result["id"]) is not None
    from levain.spores import guarded_spore_store

    return guarded_spore_store(spores_json).get(result["id"]) is not None


@dataclass
class Report:
    plan_path: Path
    applied: list[int] = field(default_factory=list)
    reverted: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)   # (index, reason): left as they were


def apply(plan_path: Path | str, *, episodic_db: Path | str | None = None,
          fault: Fault | None = None, today: date | None = None) -> Report:
    """``--apply <plan>``. Idempotent and resumable: an item already applied is skipped, one whose
    destination is written but whose original is still open resumes at the compost. ``fault`` is a
    test seam, called as ``fault(stage, item)`` at ``after_destination`` and ``after_record``."""
    from levain.spores import guarded_spore_store

    pp = Path(plan_path)
    doc = load_plan(pp)
    spores_json = Path(doc["spores_json"])
    plan_id = doc["plan_id"]
    rep = Report(pp)
    for it in doc["items"]:
        if it["state"] in (STATE_PENDING, STATE_DEST, STATE_STALE):
            _validate_dest(it)                 # every item judged before the first write
    db = _episode_store(doc, episodic_db) if _needs_episodes(doc, (STATE_PENDING, STATE_DEST, STATE_STALE)) else None
    spores = guarded_spore_store(spores_json)
    for it in doc["items"]:
        idx = it["index"]
        if it["state"] in (STATE_APPLIED, STATE_REVERTED):
            continue
        row = _find_row(spores, it)
        if row is None:
            it["state"] = STATE_STALE
            rep.skipped.append((idx, "the item is no longer in the store"))
            continue
        if row.get("status") != "open":
            if it["state"] == STATE_DEST:      # crashed after the compost, before the state flip
                it["state"] = STATE_APPLIED
                rep.applied.append(idx)
            else:
                it["state"] = STATE_STALE
                rep.skipped.append((idx, "already resolved since the plan was made"))
            continue
        own = [n for n in _notes(row) if NOTE_MARK not in n]       # a resumed apply has added its own marker
        if row.get("text") != it["text"] or _disp(row) != it["disposition"] or own != it["notes"]:
            it["state"] = STATE_STALE
            rep.skipped.append((idx, "edited since the plan was made; re-run the dry run"))
            continue
        result = _write_destination(it, plan_id, spores, db)
        if fault:
            fault("after_destination", it)
        it["result"] = result
        it["state"] = STATE_DEST
        _atomic_write(pp, doc)                 # the destination id is on disk BEFORE the original goes
        if fault:
            fault("after_record", it)
        marker = f"{NOTE_MARK}{pp}"
        if not any(marker in n for n in _notes(row)):
            spores.update(row["id"], add_note=f"migrated out of the Keep by {marker}", today=today)
        spores.descend(row["id"], kind="composted", expect_disposition=it["disposition"], today=today)
        it["state"] = STATE_APPLIED
        _atomic_write(pp, doc)
        rep.applied.append(idx)
    _atomic_write(pp, doc)
    return rep


def revert(plan_path: Path | str, *, episodic_db: Path | str | None = None,
           fault: Fault | None = None, today: date | None = None) -> Report:
    """``--revert <plan>``. Per applied (or half-applied) item: re-add the original (same text, notes,
    disposition), THEN remove what the apply made (tombstone the episode / compost the seed)."""
    from anneal_memory.spores import SporeStore
    from levain.spores import guarded_spore_store

    pp = Path(plan_path)
    doc = load_plan(pp)
    plan_id = doc["plan_id"]
    spores_json = Path(doc["spores_json"])
    todo = (STATE_APPLIED, STATE_DEST)
    db = _episode_store(doc, episodic_db) if _needs_episodes(doc, todo) else None
    plain = SporeStore(spores_json)             # ONLY for restoring a retired disposition (see module doc)
    guarded = guarded_spore_store(spores_json)
    rep = Report(pp)
    for it in doc["items"]:
        idx = it["index"]
        if it["state"] not in todo:
            if it["state"] != STATE_REVERTED:
                rep.skipped.append((idx, f"state {it['state']}: nothing was applied"))
            continue
        orig = _find_row(plain, it)
        if orig is None or orig.get("status") != "open":
            row = it["row"]
            made = plain.add(
                type=row.get("type", "thought"), text=it["text"], domain=row.get("domain") or "",
                tier=row.get("tier") or "warm", salience=int(row.get("salience") or 0),
                next=row.get("next") or None, pointer=row.get("pointer"),
                disposition=it["disposition"], target=row.get("target"), attention=row.get("attention"),
                germinated=row.get("germinated"), origin_key=_revert_key(plan_id, it))
            _restore_row_fields(plain, made["id"], row)
            it["restored_id"] = made["id"]
        if fault:
            fault("after_restore", it)
        res = it.get("result") or {}
        if res.get("kind") == DEST_EPISODE and res.get("id"):
            from anneal_memory import Store

            assert db is not None
            with Store(str(db)) as st:
                if st.get(res["id"]) is not None:
                    st.delete(res["id"])
        elif res.get("kind") == DEST_SPORE and res.get("id"):
            dest = guarded.get(res["id"])
            if dest is not None and dest.get("status") == "open":
                guarded.update(dest["id"], add_note=f"reverted by {NOTE_MARK}{pp}", today=today)
                guarded.descend(dest["id"], kind="composted", today=today)
        it["state"] = STATE_REVERTED
        _atomic_write(pp, doc)
        rep.reverted.append(idx)
    _atomic_write(pp, doc)
    return rep
