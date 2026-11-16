"""The dream's status for a desk (D1-L3): what the brief says of the last dream, and what a running desk is told when a
dream saved memory after it started.

READ ONLY. This module writes nothing and rotates nothing. The one record it reads is the ``agent`` feed of the desk's
own review store (``review_items``, inside the read the caller already holds): the cards ``levain.dream.card`` files
for a night, keyed ``dream:<occurrence>`` (kind ``consolidated`` or ``failed``), ``dream-night:<agent>:<day>`` (kind
``did-not-consolidate``) and ``dream-rollback:<occurrence>`` (kind ``rolled-back``). A card is filed after the run's
effect, so its ``at`` is the moment the dream's save was KNOWN, a few seconds after the save itself.

Invariants this relies on, so a change to one of them is a change here:

* a ``consolidated`` card is filed only for a save (``agent._file_run_card``: outcome ``saved``), once per dedupe key;
* a card's ``at`` is a fixed-width UTC stamp, so stamps compare as text and parse with ``consent.parse_stamp``;
* a SHADOW night files a ``consolidated`` card too (``shadow.shadow_card``), with the remedy :data:`SHADOW_REMEDY`:
  nothing was saved to a real store, so it never counts as a dream that wrote memory ("a shadow install emits no dream
  signal"); a test holds the constant equal to the shadow card's remedy;
* a desk's launch row ``at`` is the stamp the launch was REQUESTED (``launches.at``, written with the pending event),
  before the process is spawned or activated, and a desk loads the memory text when it starts, so a save after that
  stamp may be memory it does not hold. Earlier than the load, never later: a dream that saves between the request
  and the load draws a note the desk did not need (harmless), and none is missed;
* a rollback card is keyed by the occurrence of the save it undoes (``dream-rollback:<occurrence>``), so a rollback
  cancels exactly that occurrence's save, whatever stamp it carries.

The rotation itself is the desk's own act through ``coord close --kind dream`` (:mod:`levain.desk.seam_handoffs`), at a
seam it is not in. Nothing here starts it, fences it or authorises it. Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from levain.cockpit.consent import parse_stamp
from levain.desk.review import FEEDS

__all__ = ["DREAM_NOTE", "DREAM_UNREAD", "SHADOW_REMEDY", "STALE_HOURS", "DreamRecord", "records", "seam_note", "status_lines"]

assert "agent" in FEEDS

#: the remedy ``levain.dream.shadow.shadow_card`` files: how a shadow night's card is told from a real save
SHADOW_REMEDY = "nothing to undo: the shadow store is a scratch copy"
#: hours since the last saved dream past which the brief says "no dream since". The design names a threshold and no
#: number; a daily dream's gap is 24 hours plus its retry window, so this is a daily dream plus six hours of slack
STALE_HOURS = 30
_KINDS = ("consolidated", "failed", "did-not-consolidate", "rolled-back")
_PREFIXES = ("dream:", "dream-night:", "dream-rollback:")

#: what the degraded brief says in place of the dream status: the store was not read
DREAM_UNREAD = "Dream: cannot-derive (no store was read, so the last dream and the memory's freshness are unknown)"
#: the constant line ``coord open --verbose`` prints (the brief's own Dream line is read from the store)
DREAM_NOTE = ("Dream: the brief's Dream line reads this store's dream cards: the last dream that saved, a later night "
              "that failed or did not consolidate, and \"no dream since X\" when nothing has saved for more than "
              f"{STALE_HOURS} hours; a running desk keeps the memory it loaded and is told, on its next open, when a "
              "dream saved after it started")


@dataclass(frozen=True)
class DreamRecord:
    kind: str          # one of the card kinds
    at: str            # the card's filing stamp
    occurrence: str


def records(conn: sqlite3.Connection) -> list[DreamRecord]:
    """The dream cards of the ``agent`` feed, newest first, a shadow night's save left out. A store with no
    ``review_items`` table has none. A card whose body cannot be read is skipped (the rest stand)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_items'").fetchone() is None:
        return []
    marks = " OR ".join("dedupe_key LIKE ?" for _ in _PREFIXES)
    out: list[DreamRecord] = []
    for at, body in conn.execute(f"SELECT at, body FROM review_items WHERE feed = 'agent' AND ({marks}) "
                                 "ORDER BY at DESC, rowid DESC LIMIT 200", [p + "%" for p in _PREFIXES]):
        try:
            doc = json.loads(body)
            kind = doc["kind"]
            occ = doc.get("occurrence")
            occurrence = str(occ.get("id")) if isinstance(occ, dict) else ""
            shadow = str(doc.get("remedy", "")).startswith(SHADOW_REMEDY)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if kind in _KINDS and not (kind == "consolidated" and shadow):
            out.append(DreamRecord(kind, at, occurrence))
    return out


def _hm(stamp: str) -> str:
    return parse_stamp(stamp).strftime("%H:%M")


def _age(now: datetime, stamp: str) -> str:
    secs = int((now - parse_stamp(stamp)).total_seconds())
    if secs < 0:
        return "in the future"
    hours, rem = divmod(secs, 3600)
    return f"{hours}h{rem // 60:02d}m ago" if hours else f"{rem // 60}m ago"


def _current(recs: list[DreamRecord]) -> tuple[DreamRecord | None, DreamRecord | None]:
    """``(live, back)``: the newest save no rollback of the same occurrence undid, and the rollback of the NEWEST save
    when it was undone (None when the newest save stands). A rollback of an older occurrence says nothing of the
    memory now held."""
    undone = {r.occurrence: r for r in recs if r.kind == "rolled-back"}
    saves = [r for r in recs if r.kind == "consolidated"]
    live = next((r for r in saves if r.occurrence not in undone), None)
    return live, (undone.get(saves[0].occurrence) if saves else None)


def status_lines(recs: list[DreamRecord], now: datetime) -> list[str]:
    """The brief's Dream lines at ``now`` (aware), or none when no dream card was ever filed here (an install that
    never dreamed says nothing it cannot know). ``no dream since X`` when nothing has saved for more than
    :data:`STALE_HOURS`, or when a later night failed or did not consolidate."""
    if not recs:
        return []
    saved, reverted = _current(recs)
    later = [r for r in recs if r.kind in ("failed", "did-not-consolidate") and (saved is None or r.at > saved.at)]
    lines: list[str] = []
    if saved is None:
        last = recs[0]
        lines.append(f"Dream: no dream has saved memory in this store; the last outcome is {last.kind}, {last.at} "
                     f"({_age(now, last.at)})")
    else:
        stale = (now - parse_stamp(saved.at)).total_seconds() > STALE_HOURS * 3600
        if stale or later:
            # recs is newest first: the night after the save is the OLDEST later outcome
            why = f"; the night after it {later[-1].kind} ({later[-1].at})" if later else ""
            lines.append(f"Dream: no dream since {saved.at} ({_age(now, saved.at)}){why}")
        else:
            lines.append(f"Dream: memory refreshed by the {_hm(saved.at)} dream, {saved.at} ({_age(now, saved.at)})")
    if reverted is not None:
        lines.append(f"Dream: memory reverted at {_hm(reverted.at)} ({reverted.at}); a revert asks for no rotation")
    return lines


def seam_note(recs: list[DreamRecord], started_at: str, lv: str, tail: str) -> str | None:
    """What a running desk that started at ``started_at`` is told: the newest dream that saved memory after it
    started and was not rolled back, or None when none did. ``lv`` and ``tail`` spell the close command as the launch brief spells it."""
    saved, _ = _current(recs)
    if saved is None or saved.at <= started_at:
        return None
    return (f"Dream seam: the {_hm(saved.at)} dream saved new memory at {saved.at}, after this desk started "
            f"({started_at}); this desk holds the memory from before it. Hand over onto it only at a seam you are "
            "not in: never while he is typing, never in the middle of work. At that seam run "
            f"`{lv} coord close --kind dream --body-file PATH --request-id ID --prev-handoff-id H --token N{tail}`; "
            "the next desk loads the new memory. Nothing rotates you on its own.")
