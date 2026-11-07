"""levain.webpush.receipts: the device-receipt report, what each phone said about each push it was sent.

``levain ping receipts [--since 7d] [--json] [--ref REF]``. The server knows what it sent and what each push service
answered (``deliveries``); a device that woke for a push reports ``received`` (the worker's push event fired) and
``shown`` to ``/webpush/receipt``, and a tap is already an ``opens`` row. ``shown`` means the browser still lists the
notification for THIS ping (showNotification resolved and getNotifications holds one carrying the ping's number), never that
the OS drew it: a Focus, a scheduled summary or a banner the OS chose to hold back look the same from inside. The proof
that someone SAW a ping is a tap (an ``opens`` row) or an answer, not ``shown``. This module joins the four per ping and
device.

Reading it: a ping with no receipt is ``no receipt yet``, never lost. A device that has not woken since cannot have
reported, so ``silent`` is said only when that device reported a receipt (on any ping, of any kind) whose time is LATER
than this ping's delivery to it: ping ids are not send order (a held ping is released later under its old, lower id),
so the times decide, never the ids. A tap is the strongest sign the ping was seen: a tapped ping's outcome is ``shown``
even when its receipts were lost. ``received_count`` above one is a duplicate delivery (the repeat-notification signal).
Times are the worker's own clock for ``received_at`` and ``shown_at`` and the server's for ``sent_at`` and ``tapped_at``,
so a latency carries the device's clock skew and can be slightly negative; the ``silent`` comparison uses the time the
SERVER received the device's newest report (``reported_at``), so a wrong phone clock cannot make a ping silent.

What the push service said (``push``), from every attempt to that device for that ping, the first that applies:

- ``accepted``: some attempt got a 2xx (the push service took it; ``accepted`` is true only here)
- ``no-answer``: no 2xx, and some attempt got no answer (status 0) or none within the send's wall budget (-1, UNKNOWN):
  the push may still have gone, so ``accepted`` is null, never false
- ``gone``: every answer was a refusal and one was 404 or 410 (the subscription is dead; the send retired it)
- ``refused``: every answer was another non-2xx refusal (4xx or 5xx)
- ``none``: no delivery on record for this device (it reported or tapped without one, or the send is still running)

``accepted`` is false only for ``gone`` and ``refused``; ``outcome`` is ``not accepted`` only for those two and only when
the device reported nothing and did not tap.

The read is four aggregate queries joined to the filtered ``pings`` in SQL (no parameter per ping) and one grouping pass,
so a report without ``--since`` over a long record stays linear in its rows.

Known edge, not built against: ``/webpush/receipt`` takes no token, so two slow unauthenticated clients can hold both of
its semaphore slots; that channel is measurement, not delivery, and has its own semaphore, so no push is affected.

The TAP keys (``clicked_at``, ``click_target``, ``opened_at``, ``opened_how``, the per-device ``clicked`` and the top-level
``landings``) are ADDITIVE to ``levain-ping-receipts/1`` and the version stays ``/1`` on purpose: every key the format had
is unchanged, and no consumer in this repo or in the flow scripts reads the report by an exact schema (a consumer that
rejects unknown keys would be a new constraint, not one that exists). A click or an open also counts as ``shown`` (an
``opened`` row proves the click ran even when its own POST was lost), and a device whose only reports are worker tap rows
still counts as having reported (``silent`` rather than ``no receipt yet`` for an older ping).

JSON form (``format`` ``levain-ping-receipts/1``, the keys the desk joins to the native APNs side on ``ref``)::

    {"format": "levain-ping-receipts/1", "since": <stamp or null>, "ref": <ref or null>,
     "pings": [{"ping": int, "at": stamp, "kind": str, "ref": str|null,
                "devices": [{"device": str, "push": "accepted"|"no-answer"|"gone"|"refused"|"none",
                             "accepted": bool|null, "statuses": [int], "sent_at": stamp|null,
                             "received_at": stamp|null, "shown_at": stamp|null, "tapped_at": stamp|null,
                             "clicked_at": stamp|null, "click_target": str|null, "opened_at": stamp|null,
                             "opened_how": str|null,
                             "received_count": int, "duplicate": bool,
                             "sent_to_received_s": float|null, "received_to_shown_s": float|null,
                             "outcome": "shown"|"received"|"silent"|"no receipt yet"|"not accepted"}]}],
     "devices": [{"device": str, "sent": int, "accepted": int, "received": int, "shown": int, "tapped": int,
                  "clicked": int, "duplicates": int, "silent": int, "median_sent_to_received_s": float|null}],
     "landings": [{"device": str, "at": stamp, "target": str, "how": str}]}   # newest 20 page landings (the TAP record)
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from levain.webpush.store import STAMP, PushStore

FORMAT = "levain-ping-receipts/1"
_SINCE = re.compile(r"^(\d+)([dhm])$")
PUSH_STATES = ("accepted", "no-answer", "gone", "refused", "none")
_PUSH_WORDS = {"accepted": "accepted", "no-answer": "no answer", "gone": "gone (404/410)", "refused": "refused",
               "none": "no delivery on record"}
# the filtered pings, joined by every read below: no SQL parameter per ping
LANDINGS_SHOWN = 20   # the newest cockpit page landings the report lists
_PINGS = "(? IS NULL OR p.at >= ?) AND (? IS NULL OR p.ref = ?)"

__all__ = ["FORMAT", "PUSH_STATES", "build", "parse_since", "push_state", "text"]


def parse_since(word: str) -> timedelta:
    """``7d`` / ``12h`` / ``30m`` as a duration; ValueError otherwise."""
    m = _SINCE.match(word.strip().lower())
    if m is None or int(m.group(1)) == 0:
        raise ValueError(f"--since {word!r}: use a number and d, h or m (7d, 12h, 30m)")
    return timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[m.group(2)]: int(m.group(1))})


def push_state(statuses: list[int]) -> str:
    """The push service's answer over every attempt (the module docstring's five states)."""
    if not statuses:
        return "none"
    if any(200 <= s < 300 for s in statuses):
        return "accepted"
    if any(s <= 0 for s in statuses):
        return "no-answer"
    if any(s in (404, 410) for s in statuses):
        return "gone"
    return "refused"


def _t(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gap(a: str | None, b: str | None) -> float | None:
    x, y = _t(a), _t(b)
    return None if x is None or y is None else round((y - x).total_seconds(), 3)


def build(store: PushStore, *, since: timedelta | None = None, ref: str | None = None,
          now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = None if since is None else (now - since).strftime(STAMP)
    args = (cutoff, cutoff, ref, ref)
    by: dict[tuple[int, str], dict[str, Any]] = {}
    per_ping: dict[int, list[str]] = {}

    def cell(ping: int, device: str) -> dict[str, Any]:
        x = by.get((ping, device))
        if x is None:
            x = by[(ping, device)] = {"statuses": [], "sent": [], "received_n": 0, "received": None, "shown": None,
                                      "tapped": None, "clicked": None, "click_target": None, "opened": None,
                                      "opened_how": None}
            per_ping.setdefault(ping, []).append(device)
        return x

    with store.read() as c:
        pings = [dict(r) for r in c.execute(f"SELECT p.id, p.at, p.kind, p.ref FROM pings p WHERE {_PINGS} ORDER BY p.id",
                                            args)]
        for d in c.execute("SELECT d.ping_id, s.device_id, d.at, d.status FROM deliveries d "
                           "JOIN pings p ON p.id = d.ping_id JOIN subscriptions s ON s.id = d.subscription_id "
                           f"WHERE {_PINGS} AND s.device_id IS NOT NULL ORDER BY d.id", args):
            x = cell(d["ping_id"], d["device_id"])
            x["statuses"].append(d["status"])
            x["sent"].append(d["at"])
        # every receipt stamp has the one form (whole stamp, milliseconds, Z), so MIN and MAX on the text are by time
        for r in c.execute("SELECT r.ping_id, r.device_id, SUM(r.kind = 'received') AS n, "
                           "MIN(CASE WHEN r.kind = 'received' THEN r.at END) AS received, "
                           "MIN(CASE WHEN r.kind = 'shown' THEN r.at END) AS shown "
                           f"FROM receipts r JOIN pings p ON p.id = r.ping_id WHERE {_PINGS} "
                           "GROUP BY r.ping_id, r.device_id", args):
            x = cell(r["ping_id"], r["device_id"])
            x["received_n"], x["received"], x["shown"] = int(r["n"] or 0), r["received"], r["shown"]
        for o in c.execute("SELECT o.ping_id, o.device_id, MIN(o.at) AS at FROM opens o JOIN pings p ON p.id = o.ping_id "
                           f"WHERE {_PINGS} AND o.device_id IS NOT NULL GROUP BY o.ping_id, o.device_id", args):
            cell(o["ping_id"], o["device_id"])["tapped"] = o["at"]
        # the TAP record (store.TAP_KINDS): the worker's own click and how it then opened a window, per ping and device
        for t in c.execute("SELECT t.ping_id, t.device_id, t.kind, t.at, t.target, t.how FROM taps t JOIN pings p ON p.id = t.ping_id "
                           f"WHERE {_PINGS} AND t.kind IN ('clicked','opened') ORDER BY t.at, t.id", args):
            x = cell(t["ping_id"], t["device_id"])
            if t["kind"] == "clicked" and x["clicked"] is None:
                x["clicked"], x["click_target"] = t["at"], t["target"]
            elif t["kind"] == "opened":
                x["opened"], x["opened_how"] = t["at"], t["how"]   # the newest wins
        # where a cockpit page landed, newest first (a page's own word, no ping): the other half of a tap
        landings = [{"device": r["device_id"], "at": r["at"], "target": r["target"], "how": r["how"]}
                    for r in c.execute("SELECT device_id, at, target, how FROM taps WHERE kind = 'landed' "
                                       "AND (? IS NULL OR reported_at >= ?) ORDER BY id DESC LIMIT ?",
                                       (cutoff, cutoff, LANDINGS_SHOWN))]
        # the newest report each device has made, over the whole record (any ping): a receipt, or a worker tap report
        # (clicked, opened: a phone that reported only those still demonstrably woke and reported)
        last = {r["device_id"]: _t(r["m"]) for r in c.execute(
            "SELECT device_id, MAX(m) AS m FROM (SELECT device_id, MAX(reported_at) AS m FROM receipts GROUP BY device_id "
            "UNION ALL SELECT device_id, MAX(reported_at) AS m FROM taps WHERE kind IN ('clicked','opened') "
            "GROUP BY device_id) GROUP BY device_id")}

    out_pings, roll = [], {}
    for p in pings:
        devices = []
        for device in sorted(per_ping.get(p["id"], ())):
            x = by[(p["id"], device)]
            push = push_state(x["statuses"])
            ok = True if push == "accepted" else False if push in ("gone", "refused") else None
            recv, shown, tapped = x["received"], x["shown"], x["tapped"]
            sent_at = min(x["sent"]) if x["sent"] else None
            last_sent = _t(max(x["sent"])) if x["sent"] else None
            reported = last.get(device)
            if shown or tapped or x["clicked"] or x["opened"]:   # an open proves the click ran, whatever happened to its POST
                outcome = "shown"
            elif recv:
                outcome = "received"
            elif ok is False:
                outcome = "not accepted"
            elif last_sent is not None and reported is not None and reported > last_sent:
                outcome = "silent"
            else:
                outcome = "no receipt yet"
            row = {"device": device, "push": push, "accepted": ok, "statuses": x["statuses"], "sent_at": sent_at,
                   "received_at": recv, "shown_at": shown, "tapped_at": tapped, "clicked_at": x["clicked"],
                   "click_target": x["click_target"], "opened_at": x["opened"], "opened_how": x["opened_how"],
                   "received_count": x["received_n"],
                   "duplicate": x["received_n"] > 1,
                   "sent_to_received_s": _gap(sent_at, recv),
                   "received_to_shown_s": _gap(recv, shown),
                   "outcome": outcome}
            devices.append(row)
            r = roll.get(device)
            if r is None:
                r = roll[device] = {"device": device, "sent": 0, "accepted": 0, "received": 0, "shown": 0,
                                    "tapped": 0, "clicked": 0, "duplicates": 0, "silent": 0, "_lat": []}
            r["sent"] += 1 if x["statuses"] else 0
            r["accepted"] += 1 if ok else 0
            r["received"] += 1 if recv else 0
            r["shown"] += 1 if shown else 0
            r["tapped"] += 1 if tapped else 0
            r["clicked"] += 1 if x["clicked"] else 0
            r["duplicates"] += 1 if row["duplicate"] else 0
            r["silent"] += 1 if outcome == "silent" else 0
            if row["sent_to_received_s"] is not None:
                r["_lat"].append(row["sent_to_received_s"])
        out_pings.append({"ping": p["id"], "at": p["at"], "kind": p["kind"], "ref": p["ref"], "devices": devices})
    devices_out = []
    for r in sorted(roll.values(), key=lambda v: v["device"]):
        lat = r.pop("_lat")
        r["median_sent_to_received_s"] = round(statistics.median(lat), 3) if lat else None
        devices_out.append(r)
    return {"format": FORMAT, "since": cutoff, "ref": ref, "pings": out_pings, "devices": devices_out,
            "landings": landings}


def _s(v: float | None) -> str:
    return "-" if v is None else f"{v:g}s"


def text(report: dict[str, Any]) -> str:
    lines = []
    for p in report["pings"]:
        lines.append(f"#{p['ping']} {p['at']} {p['kind']} {p['ref'] or '-'}")
        if not p["devices"]:
            lines.append("    no device was sent this ping")
        for d in p["devices"]:
            push = _PUSH_WORDS.get(d["push"], d["push"])
            dup = f"; DUPLICATE x{d['received_count']}" if d["duplicate"] else ""
            lines.append(f"    {d['device']}: {d['outcome']} (push service {push}; received {d['received_at'] or '-'}, "
                         f"shown {d['shown_at'] or '-'}, tapped {d['tapped_at'] or '-'}; click {d['clicked_at'] or '-'} "
                         f"-> {d['click_target'] or '-'}, opened {d['opened_how'] or '-'}; sent->received "
                         f"{_s(d['sent_to_received_s'])}, received->shown {_s(d['received_to_shown_s'])}{dup})")
    if report["devices"]:
        lines.append("")
        lines.append("per device: sent / accepted / received / shown / tapped / duplicates / silent / median sent->received")
    for r in report["devices"]:
        lines.append(f"  {r['device']}: {r['sent']} / {r['accepted']} / {r['received']} / {r['shown']} / {r['tapped']} / "
                     f"{r['duplicates']} / {r['silent']} / {_s(r['median_sent_to_received_s'])}")
    if report.get("landings"):
        lines.append("")
        lines.append("cockpit landings, newest first: device, time, where the page was (how it handled a tap)")
        for g in report["landings"]:
            lines.append(f"  {g['device']}: {g['at']} {g['target']} ({g['how']})")
    lines.append("")
    lines.append("A ping with no receipt is `no receipt yet`, not lost: a phone that has not woken since cannot have "
                 "reported. `silent` means that phone reported something LATER than this ping reached it.")
    return "\n".join(lines)
