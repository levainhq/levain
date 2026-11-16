"""levain.desk.night_audit: the dead-man check and the morning's blind-time and event-audit lines (ND-6b; night desk
design 2.8 and 7).

Pure builders over an events store, read-only. Nothing here writes, pings or is wired to the morning card: the
morning's caller (levain#16) takes the lines.

A BEAT is an event of kind ``heartbeat``, first_party, of the watcher's own subject (:data:`WATCHER_SUBJECT`), whose
``state`` is ``ok``; a ``late`` or ``missing`` heartbeat is a report of an absence, not a beat, and another sensor's
heartbeat is not the watcher's. BLIND TIME is a stretch of the night between two beats (or a night edge) longer than
the window. The lines state what was seen and never say the night was quiet or that the desk was always watching
(design section 7). WATCHER_SUBJECT is a placeholder until the watcher's writer row pins the value.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.desk import events

__all__ = ["DEAD_MAN_WINDOW", "WATCHER_SUBJECT", "blind_stretches", "blind_time_lines", "dead_man",
           "event_audit_lines"]

# [seat] a watcher that has not beaten for longer than this is dead; the same gap, between two beats, is blind time
DEAD_MAN_WINDOW = timedelta(minutes=15)
# [seat] the subject of the dead-man watcher's own heartbeat: the one beat that counts. Another sensor's ok heartbeat
# (another subject, or any trust below first_party) says that sensor is alive, not that the watcher is
WATCHER_SUBJECT = "watcher"
# [seat] a beat stamped this far ahead of ``now`` or less is clock jitter and counts; further ahead is not a beat yet
CLOCK_JITTER = timedelta(seconds=5)


def _at(value: str | datetime, name: str) -> datetime:
    """``value`` (a stamp or an aware datetime) as an aware UTC datetime. A naive one is a TypeError naming ``name``."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(f"{name} is a naive datetime, which has no zone; pass an aware one or a UTC stamp")
        return value.astimezone(timezone.utc)
    return parse_stamp(value)


def dead_man(last_beat: str | datetime | None, now: str | datetime, window: timedelta = DEAD_MAN_WINDOW) -> bool:
    """True when the watcher is dead: no beat ever, the last one more than ``window`` before ``now``, or a last
    one stamped ahead of ``now`` by more than the clock jitter (a stamp from the future is not a beat that has
    happened; it must not keep the watcher alive until real time catches up)."""
    if last_beat is None:
        return True
    gap = _at(now, "now") - _at(last_beat, "last_beat")
    return gap > window or gap < -CLOCK_JITTER


def _beats(store: Any, start: datetime, end: datetime) -> tuple[datetime | None, list[datetime]]:
    """(the latest watcher beat at or before ``start``, the watcher beats in (start, end)), as aware datetimes, in
    one read snapshot of a read-only connection (a missing store raises ``StoreAbsent``; nothing is made)."""
    with read_transaction(store.open_readonly) as conn:
        rows = conn.execute("SELECT received_at, fields FROM events WHERE kind = 'heartbeat' AND "
                            "trust = 'first_party' AND received_at < ?", (utc_stamp(end),)).fetchall()
    ok = sorted(parse_stamp(at) for at, fields in rows
                if (f := json.loads(fields)).get("state") == "ok" and f.get("subject") == WATCHER_SUBJECT)
    before = [at for at in ok if at <= start]
    return (before[-1] if before else None), [at for at in ok if start < at < end]


def _gaps(prior: datetime | None, inside: list[datetime], start: datetime, end: datetime,
          window: timedelta) -> list[tuple[datetime, datetime]]:
    """The gaps over ``window`` among the beats and the night's edges, clipped to the night. With an earlier beat the
    first silence is measured from it (so one straddling the start is not under-reported) and only its part inside
    the night is returned; with none, the night's clock starts at ``start``."""
    points = [start, *inside, end]
    if prior is None:
        return [(a, b) for a, b in zip(points, points[1:]) if b - a > window]
    blind_from = max(prior + window, start)
    opening = [(blind_from, points[1])] if points[1] - prior > window and blind_from < points[1] else []
    return opening + [(a, b) for a, b in zip(points[1:], points[2:]) if b - a > window]


def blind_stretches(store: Any, night_start: str | datetime, night_end: str | datetime, *,
                    window: timedelta = DEAD_MAN_WINDOW) -> list[tuple[str, str]]:
    """The stretches of ``[night_start, night_end]`` (stamps) between consecutive beats longer than ``window``, each
    clipped to the night. The edges count as points: a night with no earlier beat starts its clock at the start."""
    start, end = _at(night_start, "night_start"), _at(night_end, "night_end")
    prior, inside = _beats(store, start, end)
    return [(utc_stamp(a), utc_stamp(b)) for a, b in _gaps(prior, inside, start, end, window)]


def _span(a: datetime, b: datetime) -> str:
    minutes = int((b - a).total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60}m"


def _hm(a: datetime, b: datetime) -> tuple[str, str]:
    """Both ends as clock times, each with its date when the two fall on different days."""
    if a.date() == b.date():
        return a.strftime("%H:%MZ"), b.strftime("%H:%MZ")
    return a.strftime("%m-%d %H:%MZ"), b.strftime("%m-%d %H:%MZ")


def blind_time_lines(store: Any, night_start: str | datetime, night_end: str | datetime, *,
                     window: timedelta = DEAD_MAN_WINDOW) -> list[str]:
    """One line per blind stretch, or one line stating the beats seen when there was none. The beats are read once:
    the stretches and the count come from the one snapshot."""
    mins = int(window.total_seconds() // 60)
    start, end = _at(night_start, "night_start"), _at(night_end, "night_end")
    prior, inside = _beats(store, start, end)
    stretches = _gaps(prior, inside, start, end, window)
    if stretches:
        lines = []
        for a, b in stretches:
            first, last = _hm(a, b)
            lines.append(f"blind {first} to {last} ({_span(a, b)}): no heartbeat for more than {mins}m")
        return lines
    first, last = _hm(start, end)
    return [f"no gap over {mins}m between heartbeats ({len(inside)} heartbeats seen {first} to {last})"]


def event_audit_lines(store: Any, since: str | datetime | None = None,
                      until: str | datetime | None = None) -> list[str]:
    """One line per incident opened in ``[since, until)``: its kind and subject, trust, how many events it took, its
    final state (closed, and by what; or still open) and its latest event's state. Typed fields only: an event's
    untrusted text is never shown. A missing store raises ``StoreAbsent``: it is not an empty night."""
    lo = None if since is None else utc_stamp(_at(since, "since"))
    end = None if until is None else _at(until, "until")
    hi = None if end is None else utc_stamp(end)
    with read_transaction(store.open_readonly) as conn:
        # one row per incident: a close is a scalar lookup (never a join that repeats the incident), and closed is
        # whether a close row exists, not whether its cause is non-empty
        incidents = conn.execute(
            "SELECT i.incident_id, i.kind, i.trust, i.opened_at, "
            "EXISTS (SELECT 1 FROM incident_closes c WHERE c.incident_id = i.incident_id), "
            "(SELECT c.cause FROM incident_closes c WHERE c.incident_id = i.incident_id "
            "ORDER BY c.gseq DESC LIMIT 1), "
            "(SELECT c.at FROM incident_closes c WHERE c.incident_id = i.incident_id "
            "ORDER BY c.gseq DESC LIMIT 1), "
            "(SELECT COUNT(*) FROM events e WHERE e.incident_id = i.incident_id), "
            "(SELECT e.event_id FROM events e WHERE e.incident_id = i.incident_id ORDER BY e.gseq DESC LIMIT 1) "
            "FROM incidents i WHERE (? IS NULL OR i.opened_at >= ?) AND (? IS NULL OR i.opened_at < ?) "
            "ORDER BY i.opened_gseq", (lo, lo, hi, hi)).fetchall()
        out = []
        for _iid, kind, trust, opened, is_closed, cause, closed, n, last_id in incidents:
            last = events._event(conn, last_id) if last_id else None
            subject = last.fields.get("subject", "?") if last else "?"
            o = parse_stamp(opened)
            if is_closed:
                c = parse_stamp(closed)
                opened_hm, closed_hm = _hm(o, c)
                final = f"closed {closed_hm} ({cause})"
            else:
                opened_hm, final = _hm(o, end or o)[0], "still open"
            out.append(f"{subject} [{kind}, {trust}]: opened {opened_hm}, {n} event{'s' if n != 1 else ''}, "
                       f"{final}, last event {last.state if last else 'none'}")
    return out or ["0 incidents opened"]
