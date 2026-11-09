"""The count of open Needs-you items, for the app-icon badge (1101-6 lane B).

The cockpit's Needs-you strip counts a consent request in state ``pending`` (filed, not yet answered, not expired) plus a
question that is open (no answer): ``needsItems`` in ``dashboard_core.js`` over the decisions panel's rows, which the
engine builds from exactly these two sources (``levain.consent.listing.read_listing`` and
``QuestionStore.open_questions``) with exactly these two predicates. This is the same count read at the same two sources,
for a caller with no page (the push sender, the unread-count route). A notice never counts: it is not a needs item
(Phill's N3 ruling), and nothing here reads the notices table.

``None`` means unknown (a store that could not be read), never zero: a wrong 0 would clear a real badge.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["BADGE_MAX", "COUNT_FILE", "COUNT_FRESH_S", "open_needs_count", "badge_value", "recorded_count", "resolved_badge"]

log = logging.getLogger(__name__)
# a store that could not be read: the answer is unknown. Anything else is a defect, logged loudly and still unknown (a push never fails on it)
_READ_FAULTS = (sqlite3.Error, OSError, ValueError)

BADGE_MAX = 99      # payload room is tight and a badge past two digits says nothing more; the page shows the exact count
COUNT_FILE = "needs-count.json"    # in the queue dir: the count the serving process last read from its own question store
COUNT_FRESH_S = 120                # a recorded count older than this is not believed


def open_needs_count(queue_dir: "Path | str", question_source: Any = None) -> "int | None":
    """Pending consent requests plus open questions in ``queue_dir`` as of now. Each store is read on its own so a failure in one
    does not stop the other, but either failing makes the answer None (unknown), never a lower-bound number.
    Creates nothing (a queue with no ``questions.db`` has no open questions). ``question_source``: the store the server serves
    its questions from (anything with ``open_questions()``, as a host's store has); none reads the native store in ``queue_dir``."""
    total, failed = 0, False
    try:
        from levain.consent.listing import read_listing

        got = read_listing(queue_dir, now=datetime.now(timezone.utc))
        total = sum(1 for e in got.pending if e.state == "pending")
    except _READ_FAULTS as exc:
        log.warning("needs count: the consent listing could not be read (%s); the count is unknown", exc)
        failed = True
    except Exception:  # noqa: BLE001 - a defect, not a read fault: logged with its trace, the count stays unknown
        log.exception("needs count: the consent listing failed unexpectedly; the count is unknown")
        failed = True
    try:
        from levain.band_view import desk_owed, later_facets
        from levain.consent.questions import QuestionStore

        store = question_source if question_source is not None else QuestionStore(queue_dir)
        # an open question snoozed past today is not counted, as on the strip (band_view.later_facets, the one LATER
        # predicate; a fault there is due, so the question still counts)
        # nor is a row the desk alone owes (section desk, not carried over to him): the strip's rule (dashboard_core needsItems)
        # a host row with neither field is his (getattr default), never a fault
        total += sum(1 for q in store.open_questions()
                     if later_facets(store, q) is None and not desk_owed(getattr(q, "section", ""), getattr(q, "carried", False)))
    except _READ_FAULTS as exc:
        log.warning("needs count: the question store could not be read (%s); the count is unknown", exc)
        failed = True
    except Exception:  # noqa: BLE001 - a defect, not a read fault: logged with its trace, the count stays unknown
        log.exception("needs count: the question store failed unexpectedly; the count is unknown")
        failed = True
    # a half that could not be read makes the count unknown, never a smaller number: a partial count could lower a real badge (6 -> 2)
    result = None if failed else total
    if question_source is not None:       # only the process holding the host's store knows its count: it says so for the others
        _record(queue_dir, result)
    return result


def _record(queue_dir: "Path | str", n: "int | None") -> None:
    """Keep the host's count where a process without its store can read it (:func:`recorded_count`). An unknown count is kept too
    (``n`` null): the file's presence is what marks a host install, so an old or unknown record means unknown, never the native
    store's different number. Never raises: a count that cannot be kept leaves the other side to its own read."""
    path = Path(queue_dir) / COUNT_FILE
    tmp = None
    try:
        if not path.parent.is_dir():
            return
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"n": n, "at": time.time(), "host": True}) + "\n")
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_record(queue_dir: "Path | str", now: "float | None") -> "tuple[bool, int | None]":
    """(is a host install, its fresh count or None). A record file that exists marks a host install even when it is old, null or
    off its shape; one that cannot be read at all but exists is host-unknown, and no file is a plain install."""
    path = Path(queue_dir) / COUNT_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except OSError:
        return (True, None) if path.exists() else (False, None)
    try:
        doc = json.loads(raw)
        n, at = doc["n"], doc["at"]
        if type(n) is not int or n < 0 or isinstance(at, bool) or not isinstance(at, (int, float)):
            return True, None
        age = (time.time() if now is None else now) - at
        return True, (min(n, BADGE_MAX) if 0 <= age <= COUNT_FRESH_S else None)
    except (ValueError, KeyError, TypeError):
        return True, None


def recorded_count(queue_dir: "Path | str", *, now: "float | None" = None) -> "int | None":
    """The host's count its serving process recorded within :data:`COUNT_FRESH_S`, clipped to :data:`BADGE_MAX`; None when
    there is no record, it is old or from the future, it is null, or it is off its shape."""
    return _read_record(queue_dir, now)[1]


def resolved_badge(queue_dir: "Path | str") -> "int | None":
    """The badge for a push whose caller did not say. A host install (it has ever recorded a count) answers with its fresh count,
    else None (unknown: the push carries no badge and the phone keeps its own); only an install with no host record counts natively."""
    host, n = _read_record(queue_dir, None)
    if host:
        return n
    return badge_value(queue_dir)


def badge_value(queue_dir: "Path | str", question_source: Any = None) -> "int | None":
    """The int a push carries as ``badge``: the open count clipped to :data:`BADGE_MAX`, None when unknown."""
    n = open_needs_count(queue_dir, question_source)
    return None if n is None else min(max(n, 0), BADGE_MAX)
