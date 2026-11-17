"""levain.desk.board_producers: the producers of the board's ``seats``, ``band`` and ``waiters`` evidence.

Each source reaches :mod:`levain.desk.board_state` as producer blocks ``{producer, store_id, captured_at, complete,
rows}``. This module writes the NATIVE producer's blocks (Levain's own reads, producer name :data:`NATIVE_PRODUCER`) and
reads the EXTERNAL producers' files, ``<store dir>/evidence.<producer>.json``. The judgement (readable, complete,
composed per predicate) is the fold's; here a block is only ever made or loaded, and a read that cannot be whole says
``complete: false`` with the reason, never a short list passed off as whole.

* ``seats``: the active claude launches of the desk store whose ``harness_session_id`` ``claude agents --json`` lists,
  with the desk's own recheck (one read that misses a launch is not a death; a second read agrees first). A row that is
  not an object in that list makes the block incomplete (:func:`levain.desk.launch._agents_checked`). A launch that is
  not listed is not emitted: a dead seat holds nothing.
* ``band``: the question store through :func:`levain.band_view.render_full`; its ``failed`` or ``pending`` makes the block
  incomplete. An open question is an ``open`` row, an answered one a ``resolved`` row carrying its channel and its grade.
* ``waiters``: ``none`` and complete ONLY when the caller says this install runs no land waiter; otherwise no block at all,
  so the source stays unreadable until a producer that runs waiters supplies it (the CLI does not expect the native one for it).

An external file is ``{"evidence_v": 2, "blocks": {"seats": <block>, "band": <block>, "waiters": <block>}}`` and is read in
ONE read of its whole bytes. The producer must write it by temp file, fsync and rename: the file is not under the store's
lock, so a reader that opened a half-written file would see a torn one (which reads as that producer unreadable).
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk.board_state import EVIDENCE_V, PRODUCER_SOURCES

__all__ = ["NATIVE_PRODUCER", "native_blocks", "read_external"]

NATIVE_PRODUCER = "levain"
_PRODUCER = re.compile(r"[a-z0-9][a-z0-9_-]*")
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*#[0-9]+")
_ROWS_MAX = 1_000_000


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _block(now: datetime, store_id: str | None, rows: Any, why: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"producer": NATIVE_PRODUCER, "store_id": store_id, "captured_at": _stamp(now),
                           "complete": why is None, "rows": rows}
    if why is not None:
        out["why"] = why
    return out


def read_external(store_dir: Path, producers: Sequence[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """``({source: [block, ...]}, {producer: why it could not be read})`` for the named producers' files."""
    blocks: dict[str, list[dict[str, Any]]] = {s: [] for s in PRODUCER_SOURCES}
    failures: dict[str, str] = {}
    for name in producers:
        if not _PRODUCER.fullmatch(name):
            failures[name] = "not a producer name (lowercase letters, digits, - and _)"
            continue
        path = Path(store_dir) / f"evidence.{name}.json"
        try:
            doc = json.loads(path.read_bytes().decode("utf-8"))                      # ONE read, then parse
        except FileNotFoundError:
            failures[name] = f"no {path.name} in the store"
            continue
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            failures[name] = f"{path.name} cannot be read ({type(exc).__name__})"
            continue
        if not isinstance(doc, dict) or doc.get("evidence_v") != EVIDENCE_V or not isinstance(doc.get("blocks"), dict):
            failures[name] = f"{path.name} is not an evidence_v {EVIDENCE_V} file with a blocks object"
            continue
        got = doc["blocks"]
        if any(isinstance(b, dict) and b.get("producer") != name for b in got.values()):
            failures[name] = f"{path.name} holds a block naming another producer"
            continue
        for source in PRODUCER_SOURCES:
            if source in got:
                blocks[source].append(got[source])
    return blocks, failures


def native_blocks(*, now: datetime, store_id: str | None, no_land_waiter: bool = False, consent_dir: Path | None = None,
                  recheck_s: float | None = None, sleep: Callable[[float], None] = time.sleep) -> dict[str, dict[str, Any]]:
    """The native producer's block for each of seats, band and waiters, captured at ``now`` (aware)."""
    out: dict[str, dict[str, Any]] = {}
    for source, read in (("seats", lambda: _seats(consent_dir, now, recheck_s, sleep)), ("band", lambda: _band(consent_dir, now))):
        try:
            rows, why = read()
        except Exception as exc:                    # noqa: BLE001 - a read that blew up is an incomplete block, never a crash
            rows, why = [], f"{type(exc).__name__}: {str(exc)[:100]}"
        out[source] = _block(now, store_id, rows, why)
    if no_land_waiter:      # declared absent: the one case the native producer speaks for waiters; otherwise whoever runs them does
        out["waiters"] = _block(now, store_id, [], None)
    return out


def _seats(consent_dir: Path | None, now: datetime, recheck_s: float | None, sleep: Callable[[float], None]) -> tuple[list[dict[str, Any]], str | None]:
    from levain.desk import launch, work_attach
    from levain.desk.store import CoordStore

    directory = Path(consent_dir) if consent_dir is not None else work_attach.live_consent_dir()
    if not (directory / "dispatch.db").exists():
        return [], None                             # no desk store has ever been made here: no launch, so no seat
    store = CoordStore(directory)
    active = [ln for ln in store.launches_active() if ln.harness == "claude" and ln.harness_session_id]
    if not active:
        return [], None
    exe, env = launch._harness()
    if exe is None:
        return [], "claude is not on PATH, so no session could be listed"
    cwd = str(store.directory)
    got = launch._agents_checked(exe, cwd=cwd, env=env)
    if got is None:
        return [], "claude agents --json could not be read"
    rows, bad = got
    if any(ln.harness_session_id not in launch._by_session(rows) for ln in active):
        sleep(launch.UNLISTED_RECHECK if recheck_s is None else recheck_s)
        again = launch._agents_checked(exe, cwd=cwd, env=env)
        if again is None:
            return [], "claude agents --json could not be read again"
        rows, bad = again
    if bad:
        return [], f"{bad} row(s) of claude agents --json are not objects"
    listed = launch._by_session(rows)
    seats = [{"name": ln.launch_id, "items": [i for i in (getattr(ln, "board_items", None) or ()) if isinstance(i, str)],
              "since": ln.at, "branch": None}
             for ln in active if ln.harness_session_id in listed and listed[ln.harness_session_id].get("state") != "done"]
    return seats, None


def _grade(answer: Any) -> str:
    """How a resolved row was answered: signed or not, by phone or at a terminal; ``unknown`` when the store cannot say."""
    where = "phone" if answer.channel == "phone" else "terminal"
    if answer.signed is True:
        return f"signed-{where}"
    return f"unsigned-{where}" if answer.signed is False else "unknown"


def _band(consent_dir: Path | None, now: datetime) -> tuple[list[dict[str, Any]], str | None]:
    from levain import band_view
    from levain.consent.questions import QuestionStore

    store = QuestionStore(consent_dir) if consent_dir is not None else QuestionStore()
    seen = band_view.render_full(store, numbering=False, now=now)
    if seen.failed:
        return [], "the band could not read an authority source"
    if seen.pending:
        return [], "an open band item has no number yet"
    rows: list[dict[str, Any]] = []
    for q in store.recent(_ROWS_MAX):
        items = list(dict.fromkeys(_ITEM_ID.findall(f"{q.what} {q.blocks}")))
        if q.answer is None:
            rows.append({"id": q.id, "state": "open", "items": items, "by": "", "grade": "unknown"})
        else:
            rows.append({"id": q.id, "state": "resolved", "items": items, "by": q.answer.channel, "grade": _grade(q.answer)})
    return rows, None
