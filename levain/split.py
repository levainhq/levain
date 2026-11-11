"""levain.split: a split is two linked intentions, the first part's completion waking the second (row 178 build 2).

The link lives on the SECOND part alone: an open agenda carrying ``on_event: done:<first part's id>`` and a ``check_by``
day (anneal's opaque keys). The first part keeps no pointer to it, so there is no second record to drift: "what waits on
this" is a scan for ``on_event == done:<id>``. The wake is :func:`levain.germinate.germinate` reading the first part's own
resolution (finished by a completion kind), so it holds however the first part was resolved (the List row, the band card, a
CLI) and whether or not an act that wanted to hurry it ran.

Two acts, each safe to run again:

* :func:`create_then` files the second part. Its ``origin_key`` is derived from the first part and the words, so a retry
  after a lost reply lands the one row, never two.
* :func:`wake_after` runs the wake for ONE first part right now (an event-only germinate), for the writers that just resolved
  it. It is an accelerator: the next ordinary germinate run wakes the same row if this one never ran.

Build 2 covers a first part that is the operator's (band or nudge): its completion is the operator marking it done. A desk or
board first part is composted when it germinates (its work has not happened yet), so its ``done:`` could never fire:
:func:`create_then` refuses a link on one, in words. That arrives with the ``landed:`` event in a later build.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from levain.spores import (
    DEFAULT_CHECK_BY_DAYS,
    TRAY_WRITE_DISPOSITIONS,
    done_event,
    executor_of,
    guarded_spore_store,
    list_open_readonly,
    norm_event,
    valid_check_by,
)

__all__ = ["ThenRefused", "create_then", "followers_of", "then_origin_key", "wake_after"]

THEN_KEY_PREFIX = "levain-then:"


class ThenRefused(ValueError):
    """The link cannot be made. ``code`` is the write seam's error code, ``status`` its HTTP status."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def then_origin_key(anchor: dict[str, Any], text: str) -> str:
    """The second part's ``origin_key``: one per (first part, words). A retry of the same ask names the same record."""
    base = anchor.get("origin_key") or anchor.get("id") or ""
    return THEN_KEY_PREFIX + hashlib.sha256(f"{base}\0{text}".encode("utf-8")).hexdigest()[:40]


def followers_of(items: list[dict[str, Any]], anchor_id: str) -> list[dict[str, Any]]:
    """The open agendas among ``items`` that wait on ``anchor_id`` finishing (either spelling of its id)."""
    key = norm_event(done_event(anchor_id))
    return [s for s in items if norm_event(s.get("on_event")) == key and s.get("status", "open") == "open"]


def create_then(spores_json: Path | str, anchor_id: str, text: str, *, check_by: str | None = None,
                today: date | None = None) -> dict[str, Any]:
    """File the second part of a split: a new agenda for the operator (target ``band``) that wakes when ``anchor_id`` is done,
    with a check-by day (default :data:`DEFAULT_CHECK_BY_DAYS` days on; a day before today is refused, since it would ring at
    once). The first part must be open, a Tray row (a seed or an agenda) and the operator's own (not a desk or board part). Returns
    ``{"spore_id", "replay", "check_by", "event", "note"}``. Raises :class:`ThenRefused`."""
    from anneal_memory.spores import SporeError

    today = today or datetime.now().date()
    words = text.strip() if isinstance(text, str) else ""
    if not words:
        raise ThenRefused("bad_verb_arg", 422, "a then-part needs words: what is it you do after this?")
    if check_by is None:
        check_by = (today + timedelta(days=DEFAULT_CHECK_BY_DAYS)).isoformat()
    elif not valid_check_by(check_by):
        raise ThenRefused("bad_verb_arg", 422, f"check_by must be a day, YYYY-MM-DD (got {check_by!r})")
    elif check_by < today.isoformat():
        raise ThenRefused("bad_verb_arg", 422, f"check_by {check_by} is already past: it would ring at once")
    store = guarded_spore_store(spores_json)
    anchor = store.get(anchor_id)
    if not isinstance(anchor, dict) or anchor.get("status") == "resolved":
        raise ThenRefused("stale", 409, "the item was resolved or removed since it was read")
    disposition = anchor.get("disposition")
    if not isinstance(disposition, str) or disposition.strip().casefold() not in TRAY_WRITE_DISPOSITIONS:
        raise ThenRefused("bad_verb_arg", 422, "a then-part follows a seed or an agenda on the List, not a loop")
    if executor_of(anchor.get("target")) == "machine":
        raise ThenRefused("bad_verb_arg", 422, "this part is handed to the desk or a board, and it is closed when it is handed "
                          "off, so nothing could mark it done: a then-part follows a part you do yourself")
    key = then_origin_key(anchor, words)
    prior = store.get_by_origin_key(key)
    if prior is not None and prior.get("status") == "resolved":
        raise ThenRefused("stale", 409, f"that part was already filed as {prior.get('id')} and has ended; "
                          "word it differently to file it again")
    replay = prior is not None
    event = done_event(anchor["id"])
    try:
        made = store.add(type="task", text=words, disposition="agenda", target="band", on_event=event,
                         check_by=check_by, origin_key=key, today=today)
    except ValueError as exc:
        raise ThenRefused("bad_verb_arg", 422, str(exc)) from exc
    except SporeError as exc:
        raise ThenRefused("verb_failed", 422, str(exc)) from exc
    day = made.get("check_by") or check_by
    return {"spore_id": made["id"], "replay": replay, "check_by": day, "event": event,
            "note": f"filed {made['id']}: it comes to your band when this is marked done; if it is not, you are asked on {day}"}


def wake_after(spores_json: Path | str, anchor_id: str, *, queue_dir: Path | str | None = None,
               agent: str = "claude_code") -> list[str]:
    """Wake the second parts that wait on ``anchor_id`` now: the ids woken, none when nothing waits on it (then nothing is
    opened or written, no band store is created). The event is checked against the store by the germinate it runs, so a
    caller that names a first part that is not finished wakes nothing."""
    from levain import germinate as g
    from levain.consent.queue import default_queue_dir

    path = Path(spores_json)
    if not path.is_file() or not followers_of(list_open_readonly(path), anchor_id):
        return []
    qdir = Path(queue_dir) if queue_dir is not None else default_queue_dir()
    res = g.germinate(path, qdir, install=path.parent.parent, agent=agent, event=done_event(anchor_id))
    if res["errors"]:
        raise RuntimeError("; ".join(str(e.get("error")) for e in res["errors"]))
    return [str(o["spore_id"]) for o in res["due"] if o.get("done")]

