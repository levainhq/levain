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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from levain.intentions import (
    DEFAULT_CHECK_BY_DAYS,
    TRAY_WRITE_DISPOSITIONS,
    done_event,
    event_anchor,
    executor_of,
    finished_by_completion,
    guarded_intention_store,
    is_event_agenda,
    list_open_readonly,
    norm_event,
    valid_check_by,
)

__all__ = ["ThenRefused", "WakeFailed", "anchor_problem", "becomes_waiting", "create_then", "followers_of", "reagenda_refusal",
           "then_origin_key", "wake_after"]

THEN_KEY_PREFIX = "levain-then:"


class ThenRefused(ValueError):
    """The link cannot be made. ``code`` is the write seam's error code, ``status`` its HTTP status."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status = code, status


class WakeFailed(RuntimeError):
    """A wake that hit an error part-way. ``woke`` holds the ids that DID wake before or beside it (their stamps are committed),
    so a caller reports them with the error instead of dropping them; the next germinate run wakes the rest."""

    def __init__(self, message: str, woke: list[str]) -> None:
        super().__init__(message)
        self.woke = woke


def then_origin_key(anchor: dict[str, Any], text: str) -> str:
    """The second part's ``origin_key``: one per (first part, words). A retry of the same ask names the same record."""
    base = anchor.get("origin_key") or anchor.get("id") or ""
    return THEN_KEY_PREFIX + hashlib.sha256(f"{base}\0{text}".encode("utf-8")).hexdigest()[:40]


def followers_of(items: list[dict[str, Any]], anchor_id: str) -> list[dict[str, Any]]:
    """The open agendas among ``items`` that wait on ``anchor_id`` finishing (either spelling of its id) AND that the wake can
    still reach: the same :func:`is_event_agenda` germinate reads, so "waits on it" here and "wakes on it" there are one
    answer. A part moved off the agenda (to a someday) keeps its ``on_event`` but is never woken or rung at its check-by, so it
    is not waiting on anything and does not hold the first part."""
    key = norm_event(done_event(anchor_id))
    return [s for s in items if is_event_agenda(s) and norm_event(s.get("on_event")) == key]


def anchor_problem(anchor: Mapping[str, Any] | None) -> tuple[str, int, str, str] | None:
    """Why ``anchor`` (a row as stored, or None for no such row) cannot be the first part of a split, or None when it can:
    ``(code, status, message, short)``, the message in the words of a refusal to file a then-part and ``short`` the same
    reason as a clause. The one check :func:`create_then` makes at link time and a second part makes again when it becomes an
    agenda (:func:`reagenda_refusal`), so a link cannot be made in a state it would be refused in."""
    if not isinstance(anchor, Mapping) or anchor.get("status") == "resolved":
        return ("stale", 409, "the item was resolved or removed since it was read", "it has ended or is gone")
    disposition = anchor.get("disposition")
    if not isinstance(disposition, str) or disposition.strip().casefold() not in TRAY_WRITE_DISPOSITIONS:
        return ("bad_verb_arg", 422, "a then-part follows a someday or an agenda on the List, not a loop",
                "it is a loop, not a someday or an agenda on the List")
    if executor_of(anchor.get("target")) == "machine":
        return ("bad_verb_arg", 422, "this part is handed to the desk or a board, and it is closed when it is handed "
                "off, so nothing could mark it done: a then-part follows a part you do yourself",
                "it is handed to the desk or a board, and is closed when handed off, so nothing could mark it done")
    return None


def becomes_waiting(pre: Mapping[str, Any], disposition: Any) -> bool:
    """Whether giving ``pre`` (a row as stored) ``disposition`` makes an event agenda of a row that is not one now: it names an
    event it has not woken on, is open, and the new disposition is an agenda (a someday that kept its ``on_event`` going back)."""
    return (isinstance(disposition, str) and disposition.strip().casefold() == "agenda" and bool(pre.get("on_event"))
            and not pre.get("germinated") and pre.get("status", "open") == "open" and not is_event_agenda(pre))


def reagenda_refusal(follower: Mapping[str, Any], anchor: Mapping[str, Any] | None) -> str | None:
    """Why ``follower`` (a row as stored, moved off the agenda with its ``on_event`` kept) may not become an agenda again, or
    None when it may: the part it names (``anchor``, read the way germinate reads it) is no longer a first part that can be
    marked done, by :func:`anchor_problem`. A first part already finished is no problem: the wake reaches the follower at its
    next run. Asked of a write that makes the row an agenda, since the retarget guard cannot hold a first part for a part that
    was not waiting when the first part was handed off."""
    named = event_anchor(follower)
    if named is None or finished_by_completion(anchor):
        return None
    problem = anchor_problem(anchor)
    if problem is None:
        return None
    fid = follower.get("id")
    return (f"{fid} waits on {named} being marked done, and that can no longer happen: {problem[3]}. Link a fresh then-part to a "
            f"part you do yourself, or leave {fid} a someday.")


def create_then(intentions_json: Path | str, anchor_id: str, text: str, *, check_by: str | None = None,
                today: date | None = None) -> dict[str, Any]:
    """File the second part of a split: a new agenda for the operator (target ``band``) that wakes when ``anchor_id`` is done,
    with a check-by day (default :data:`DEFAULT_CHECK_BY_DAYS` days on; a day before today is refused, since it would ring at
    once). The first part must be open, a Tray row (a someday or an agenda) and the operator's own (not a desk or board part).
    Returns ``{"intention_id", "replay", "check_by", "event", "note"}``. Raises :class:`ThenRefused`."""
    from anneal_memory.spores import IntentionError

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
    store = guarded_intention_store(intentions_json)
    anchor = store.get(anchor_id)
    problem = anchor_problem(anchor)
    if problem is not None:
        raise ThenRefused(*problem[:3])
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
    except IntentionError as exc:
        raise ThenRefused("verb_failed", 422, str(exc)) from exc
    day = made.get("check_by") or check_by
    return {"intention_id": made["id"], "replay": replay, "check_by": day, "event": event,
            "note": f"filed {made['id']}: it comes to your band when this is marked done; if it is not, you are asked on {day}"}


def wake_after(intentions_json: Path | str, anchor_id: str, *, queue_dir: Path | str | None = None,
               agent: str = "claude_code") -> list[str]:
    """Wake the second parts that wait on ``anchor_id`` now: the ids woken, none when nothing waits on it (then nothing is
    opened or written, no band store is created). The event is checked against the store by the germinate it runs, so a
    caller that names a first part that is not finished wakes nothing. An error in the run raises :class:`WakeFailed`, which
    carries the ids that did wake."""
    from levain import germinate as g
    from levain.consent.queue import default_queue_dir

    path = Path(intentions_json)
    if not path.is_file() or not followers_of(list_open_readonly(path), anchor_id):
        return []
    qdir = Path(queue_dir) if queue_dir is not None else default_queue_dir()
    res = g.germinate(path, qdir, install=path.parent.parent, agent=agent, event=done_event(anchor_id))
    woke = [str(o["intention_id"]) for o in res["due"] if o.get("done")]
    if res["errors"]:
        raise WakeFailed("; ".join(str(e.get("error")) for e in res["errors"]), woke)
    return woke

