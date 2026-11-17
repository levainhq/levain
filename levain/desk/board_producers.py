"""levain.desk.board_producers: the producers of the board's ``seats``, ``band`` and ``waiters`` evidence.

Each source reaches :mod:`levain.desk.board_state` as producer blocks ``{producer, store_id, captured_at, complete, rows}``.
This module writes the NATIVE producer's blocks (Levain's own reads, producer name :data:`NATIVE_PRODUCER`) and reads the
EXTERNAL producers' files, ``<store dir>/evidence.<producer>.json``. The judgement (readable, complete, composed per
predicate) is the fold's; here a block is only ever made or loaded, and a read that cannot be whole says ``complete: false``
with the reason, never a short list passed off as whole. A producer that cannot see something must not claim it.

* ``seats``: the active claude launches of the desk store, read through its read-only open. The block is whole ONLY when every
  active claude launch is listed by ``claude agents --json`` AND carries a ``board_items`` attribute that is a list of strings
  (the ids it was launched for). A launch without the attribute, with another kind of value, with no session yet, or not
  listed makes the block incomplete and says which; the rows of the seats that were seen still go in (a positive claim stands).
  No active claude launch is a whole, empty block. The read does not wait: a launch the list does not show is reported, not
  re-read after a pause.
* ``band``: the question store through :func:`levain.band_view.render_full` and the open authority rows (consent, sign,
  extend) the band shows; its ``failed`` or ``pending``, an authority row whose shape cannot be read, or a store with
  :data:`BAND_ROWS_MAX` rows (older ones unread) makes the block incomplete. An open question or authority row is an ``open``
  row, an answered question a ``resolved`` row carrying its channel and its grade.
* ``waiters``: ``none`` and complete ONLY when the caller says this install runs no land waiter; otherwise no block at all,
  so the source stays unreadable until a producer that runs waiters supplies it (the CLI does not expect the native one for it).

An external file is ``{"evidence_v": 2, "blocks": {"seats": <block>, "band": <block>, "waiters": <block>}}`` and is read in
ONE read of its whole bytes. The producer must write it by temp file, fsync and rename: the file is not under the store's
lock, so a reader that opened a half-written file would see a torn one (which reads as that producer unreadable). A block in
it must be an object naming that producer and carrying no ``form`` key; any other makes the whole file unreadable.

Known edges. A signing row whose question does not contain the literal item id never closes that item (by design, P5-b: a
backing row names ``repo#N`` and is filed ``--authorises``). ``_grade`` runs the countersign read once per resolved unsigned row
(one store read each; boards are small).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.desk.board_state import EVIDENCE_V, PRODUCER_SOURCES

__all__ = ["BAND_ROWS_MAX", "NATIVE_PRODUCER", "native_blocks", "read_external"]

NATIVE_PRODUCER = "levain"
#: How many band rows one read takes; a store that holds this many has older rows unread, so the block says incomplete.
BAND_ROWS_MAX = 10_000
_PRODUCER = re.compile(r"[a-z0-9][a-z0-9_-]*")
#: An item id as a whole token: not the tail of a longer word, path or id, and not followed by more of one (``app#2.txt`` is a
#: file name, not item app#2; a full stop that ends a sentence still ends the id).
_ITEM_ID = re.compile(r"(?<![\w.#/-])[A-Za-z0-9][A-Za-z0-9._-]*#[0-9]+(?![\w#/\\-]|\.\w)")
_MISSING = object()


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
        if any(not isinstance(b, dict) or b.get("producer") != name for b in got.values()):
            failures[name] = f"{path.name} holds a block that is not an object naming {name} (it names another producer, or none)"
            continue
        if any("form" in b for b in got.values()):
            failures[name] = f"{path.name} holds a block with a form key, which only the flat reader may set"
            continue
        for source in PRODUCER_SOURCES:
            if source in got:
                blocks[source].append(got[source])
    return blocks, failures


def native_blocks(*, now: datetime, store_id: str | None, no_land_waiter: bool = False,
                  consent_dir: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """The native producer's block for each of seats, band and waiters, captured at ``now`` (aware). ``environ`` (default the
    process's) names the sign queue a countersign is read from."""
    out: dict[str, dict[str, Any]] = {}
    env = os.environ if environ is None else environ
    for source, read in (("seats", lambda: _seats(consent_dir)), ("band", lambda: _band(consent_dir, now, env))):
        try:
            rows, why = read()
        except Exception as exc:                    # noqa: BLE001 - a read that blew up is an incomplete block, never a crash
            rows, why = [], f"{type(exc).__name__}: {str(exc)[:100]}"
        out[source] = _block(now, store_id, rows, why)
    if no_land_waiter:      # declared absent: the one case the native producer speaks for waiters; otherwise whoever runs them does
        out["waiters"] = _block(now, store_id, [], None)
    return out


def _active_launches(directory: Path) -> tuple[Path, list[Any]]:
    """``(the desk store's directory, its active claude launches)``, through the store's read-only open: this read never
    makes a table or takes the write lock."""
    from levain.desk import lease
    from levain.desk.store import CoordStore

    store = CoordStore(directory)
    conn = store.open_readonly()
    try:
        return store.directory, [ln for ln in lease._in_state(conn, "active") if ln.harness == "claude"]
    finally:
        conn.close()


def _launch_items(launch: Any) -> list[str] | None:
    """The item ids a launch was started for, or None when it carries no ``board_items`` list of strings."""
    got = getattr(launch, "board_items", _MISSING)
    if isinstance(got, (list, tuple)) and all(isinstance(i, str) for i in got):
        return list(got)
    return None


def _seats(consent_dir: Path | None) -> tuple[list[dict[str, Any]], str | None]:
    from levain.desk import launch, work_attach

    directory = Path(consent_dir) if consent_dir is not None else work_attach.live_consent_dir()
    if not (directory / "dispatch.db").exists():
        return [], None                             # no desk store has ever been made here: no launch, so no seat
    where, active = _active_launches(directory)
    if not active:
        return [], None
    whys: list[str] = []
    listed: dict[str, dict[str, Any]] = {}
    read_ok = False
    with_session = [ln for ln in active if ln.harness_session_id]
    if with_session:
        exe, env = launch._harness()
        got = launch._agents_checked(exe, cwd=str(where), env=env) if exe is not None else None
        if exe is None:
            whys.append("claude is not on PATH, so no session could be listed")
        elif got is None:
            whys.append("claude agents --json could not be read")
        else:
            rows, bad = got
            read_ok = True
            listed = launch._by_session(rows)
            if bad:
                whys.append(f"{bad} row(s) of claude agents --json are not objects")
    seats: list[dict[str, Any]] = []
    for ln in active:
        if ln.harness_session_id in listed and listed[ln.harness_session_id].get("state") == "done":
            continue                                # a finished launch holds nothing: it is neither a seat nor a gap
        items = _launch_items(ln)
        if items is None:
            whys.append(f"launch {ln.launch_id} carries no board_items (T7 writes it)")
        if not ln.harness_session_id:
            whys.append(f"launch {ln.launch_id} has no session yet")
        elif read_ok and ln.harness_session_id not in listed:
            whys.append(f"launch {ln.launch_id} not listed by claude agents")
        elif ln.harness_session_id in listed:
            seats.append({"name": ln.launch_id, "items": items or [], "since": ln.at, "branch": None})
    return seats, "; ".join(whys) or None


def _grade(store: Any, question: Any, queue: Path | None = None) -> str:
    """How a resolved row was answered: signed or not, by phone or at a terminal; ``unknown`` when the store cannot say.
    A native answer carries no signing flag, so the answer's signing sidecar (:func:`levain.band_view.answer_sign_of`) says;
    it is read once, for the grade and for the countersign check. ``queue`` is the sign queue the countersign reads."""
    from levain import band_view

    answer = question.answer
    signed = answer.signed
    sign: Any = None
    if signed is not True:
        try:
            sign = band_view.answer_sign_of(store, question.id) or {}
        except Exception:                           # noqa: BLE001 - a sidecar that cannot be read is a grade the store cannot say
            sign = None
    if signed is None:
        state = (sign or {}).get("sign_state")
        signed = True if state == "signed" else False if state == "unsigned" else None
    where = "phone" if answer.channel == "phone" else "terminal"
    if signed is True:
        return f"signed-{where}"
    if _countersigned(store, question.id, sign, queue):
        return "countersigned"
    return f"unsigned-{where}" if signed is False else "unknown"


def _countersigned(store: Any, question_id: str, sign: Any, queue: Path | None) -> bool:
    """A remote answer recorded unsigned (``sign``: its sidecar) whose sign card the phone's passkey signed over exactly that
    answer: :func:`levain.consent.answer_countersign.verdict` says exactly ``countersigned`` (fail closed: only a verified
    signature reads so). Only the read failures the verdict could let through are caught, and read as not countersigned."""
    if not isinstance(sign, Mapping) or sign.get("sign_state") != "unsigned" or sign.get("sign_reason") != "remote":
        return False
    from levain.consent import answer_countersign as ac

    try:
        return ac.verdict(store, question_id, sign_queue=queue).state == "countersigned"
    except (OSError, sqlite3.Error, ValueError):
        return False


def _sign_queue(consent_dir: Path | None, environ: Mapping[str, str]) -> Path | None:
    """The sign queue the countersign reads: the live install's (a named ``$LEVAIN_SIGN_QUEUE_DIR``, or the live store's hook
    queue) for the live store; for an explicit ``consent_dir`` (the hook queue is the consent dir) that directory, never the
    live one, unless the environment names a queue."""
    from levain.consent import answer_countersign as ac

    try:
        if consent_dir is None or environ.get(ac.ENV_QUEUE):
            return ac.sign_queue_dir(environ)
        return Path(consent_dir)
    except (OSError, ValueError, KeyError):
        return None


def _items_in(*texts: Any) -> list[str]:
    return list(dict.fromkeys(i for t in texts if isinstance(t, str) for i in _ITEM_ID.findall(t)))


def _band(consent_dir: Path | None, now: datetime, environ: Mapping[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    from levain import band_sources, band_view
    from levain.consent.questions import QuestionStore

    store = QuestionStore(consent_dir) if consent_dir is not None else QuestionStore()
    seen = band_view.render_full(store, numbering=False, now=now)
    whys: list[str] = []
    if seen.failed:
        whys.append("the band could not read an authority source")
    if seen.pending:
        whys.append("an open band item has no number yet")
    rows: list[dict[str, Any]] = []
    queue = _sign_queue(consent_dir, environ)
    questions = store.recent(BAND_ROWS_MAX)
    if len(questions) >= BAND_ROWS_MAX:
        whys.append(f"more than {BAND_ROWS_MAX} band rows: older rows not read")
    for q in questions:
        items = _items_in(q.what, q.blocks)
        if q.answer is None:
            rows.append({"id": q.id, "state": "open", "items": items, "by": "", "grade": "unknown"})
        elif not isinstance(q.answer.channel, str) or not q.answer.channel:
            whys.append(f"the answer to {q.id} names no channel")
        else:
            rows.append({"id": q.id, "state": "resolved", "items": items, "by": q.answer.channel, "grade": _grade(store, q, queue)})
    if not band_view._foreign(store):          # the open authority rows (consent, sign, extend) the band shows; read even when a source failed
        try:
            extras = band_sources.read_extras(store.directory, store._clock, now=now)
        except Exception as exc:                # noqa: BLE001 - the authority rows are lost; the question rows already read still stand
            whys.append(f"the band could not read an authority source ({type(exc).__name__})")
            return rows, "; ".join(dict.fromkeys(whys))
        for e in extras.items:
            if (e.unreadable is not None or not all(isinstance(x, str) and x for x in (e.source, e.ref))
                    or not isinstance(e.what, str) or not isinstance(e.blocks, str)):
                whys.append(f"authority row {e.source}:{e.ref} cannot be read")
                continue
            rows.append({"id": f"{e.source}:{e.ref}", "state": "open", "items": _items_in(e.what, e.blocks), "by": "", "grade": "unknown"})
        if extras.failed:
            whys.append("the band could not read an authority source")
    return rows, "; ".join(dict.fromkeys(whys)) or None
