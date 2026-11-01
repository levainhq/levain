"""levain.webpush.receipts: the device-receipt report, what each phone said about each push it was sent.

``levain ping receipts [--since 7d] [--json] [--ref REF]``. The server knows what it sent and what each push service
answered (``deliveries``); a device that woke for a push reports ``received`` (the worker's push event fired) and
``shown`` (showNotification resolved) to ``/webpush/receipt``, and a tap is already an ``opens`` row. This module joins the
four per ping and device.

Reading it: a ping with no receipt is ``no receipt yet``, never lost. A device that has not woken since cannot have
reported, so ``silent`` is said only when that device has reported on a LATER ping. ``received_count`` above one is a
duplicate delivery (the repeat-notification signal). Times are the worker's own clock for ``received_at`` and
``shown_at`` and the server's for ``sent_at`` and ``tapped_at``, so a latency carries the device's clock skew and can be
slightly negative.

JSON form (``format`` ``levain-ping-receipts/1``, the keys the desk joins to the native APNs side on ``ref``)::

    {"format": "levain-ping-receipts/1", "since": <stamp or null>, "ref": <ref or null>,
     "pings": [{"ping": int, "at": stamp, "kind": str, "ref": str|null,
                "devices": [{"device": str, "accepted": bool|null, "statuses": [int], "sent_at": stamp|null,
                             "received_at": stamp|null, "shown_at": stamp|null, "tapped_at": stamp|null,
                             "received_count": int, "duplicate": bool,
                             "sent_to_received_s": float|null, "received_to_shown_s": float|null,
                             "outcome": "shown"|"received"|"silent"|"no receipt yet"|"not accepted"}]}],
     "devices": [{"device": str, "sent": int, "accepted": int, "received": int, "shown": int, "tapped": int,
                  "duplicates": int, "silent": int, "median_sent_to_received_s": float|null}]}

``accepted`` is null for a ping with no delivery to that device (it reported or tapped without one on record);
``outcome`` is ``not accepted`` only when every push-service answer was a refusal and the device reported nothing.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from levain.webpush.store import STAMP, PushStore

FORMAT = "levain-ping-receipts/1"
_SINCE = re.compile(r"^(\d+)([dhm])$")

__all__ = ["FORMAT", "build", "parse_since", "text"]


def parse_since(word: str) -> timedelta:
    """``7d`` / ``12h`` / ``30m`` as a duration; ValueError otherwise."""
    m = _SINCE.match(word.strip().lower())
    if m is None or int(m.group(1)) == 0:
        raise ValueError(f"--since {word!r}: use a number and d, h or m (7d, 12h, 30m)")
    return timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[m.group(2)]: int(m.group(1))})


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
    with store.read() as c:
        pings = [dict(r) for r in c.execute(
            "SELECT id, at, kind, ref FROM pings WHERE (? IS NULL OR at >= ?) AND (? IS NULL OR ref = ?) ORDER BY id",
            (cutoff, cutoff, ref, ref))]
        ids = [p["id"] for p in pings]
        marks = ",".join("?" * len(ids))
        deliveries = [dict(r) for r in c.execute(
            "SELECT d.ping_id, d.at, d.status, s.device_id FROM deliveries d JOIN subscriptions s ON s.id = d.subscription_id "
            f"WHERE d.ping_id IN ({marks}) ORDER BY d.id", ids)] if ids else []
        receipts = [dict(r) for r in c.execute(
            f"SELECT ping_id, device_id, kind, at FROM receipts WHERE ping_id IN ({marks}) ORDER BY id", ids)] if ids else []
        opens = [dict(r) for r in c.execute(
            f"SELECT ping_id, device_id, at FROM opens WHERE ping_id IN ({marks}) ORDER BY id", ids)] if ids else []
        # the newest ping each device has reported on, over the whole record: a ping before it was passed over
        last = {r["device_id"]: r["m"] for r in c.execute("SELECT device_id, MAX(ping_id) AS m FROM receipts GROUP BY device_id")}

    by: dict[tuple[int, str], dict[str, Any]] = {}

    def cell(ping: int, device: str) -> dict[str, Any]:
        return by.setdefault((ping, device), {"statuses": [], "sent": [], "received": [], "shown": [], "tapped": []})

    for d in deliveries:
        if d["device_id"]:
            x = cell(d["ping_id"], d["device_id"])
            x["statuses"].append(d["status"])
            x["sent"].append(d["at"])
    for r in receipts:
        cell(r["ping_id"], r["device_id"])[r["kind"]].append(r["at"])
    for o in opens:
        if o["device_id"]:
            cell(o["ping_id"], o["device_id"])["tapped"].append(o["at"])

    out_pings, roll = [], {}
    for p in pings:
        devices = []
        for (pid, device), x in sorted(by.items()):
            if pid != p["id"]:
                continue
            ok = None if not x["statuses"] else any(200 <= s < 300 for s in x["statuses"])
            recv, shown = sorted(x["received"])[:1], sorted(x["shown"])[:1]
            if shown:
                outcome = "shown"
            elif recv:
                outcome = "received"
            elif ok is False:
                outcome = "not accepted"
            elif last.get(device, 0) > p["id"]:
                outcome = "silent"
            else:
                outcome = "no receipt yet"
            sent_at = min(x["sent"]) if x["sent"] else None
            row = {"device": device, "accepted": ok, "statuses": x["statuses"], "sent_at": sent_at,
                   "received_at": recv[0] if recv else None, "shown_at": shown[0] if shown else None,
                   "tapped_at": min(x["tapped"]) if x["tapped"] else None, "received_count": len(x["received"]),
                   "duplicate": len(x["received"]) > 1,
                   "sent_to_received_s": _gap(sent_at, recv[0] if recv else None),
                   "received_to_shown_s": _gap(recv[0] if recv else None, shown[0] if shown else None),
                   "outcome": outcome}
            devices.append(row)
            r = roll.setdefault(device, {"device": device, "sent": 0, "accepted": 0, "received": 0, "shown": 0,
                                         "tapped": 0, "duplicates": 0, "silent": 0, "_lat": []})
            r["sent"] += 1 if x["statuses"] else 0
            r["accepted"] += 1 if ok else 0
            r["received"] += 1 if recv else 0
            r["shown"] += 1 if shown else 0
            r["tapped"] += 1 if x["tapped"] else 0
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
    return {"format": FORMAT, "since": cutoff, "ref": ref, "pings": out_pings, "devices": devices_out}


def _s(v: float | None) -> str:
    return "-" if v is None else f"{v:g}s"


def text(report: dict[str, Any]) -> str:
    lines = []
    for p in report["pings"]:
        lines.append(f"#{p['ping']} {p['at']} {p['kind']} {p['ref'] or '-'}")
        if not p["devices"]:
            lines.append("    no device was sent this ping")
        for d in p["devices"]:
            push = {None: "no delivery on record", True: "accepted", False: "not accepted"}[d["accepted"]]
            dup = f"; DUPLICATE x{d['received_count']}" if d["duplicate"] else ""
            lines.append(f"    {d['device']}: {d['outcome']} (push service {push}; received {d['received_at'] or '-'}, "
                         f"shown {d['shown_at'] or '-'}, tapped {d['tapped_at'] or '-'}; sent->received "
                         f"{_s(d['sent_to_received_s'])}, received->shown {_s(d['received_to_shown_s'])}{dup})")
    if report["devices"]:
        lines.append("")
        lines.append("per device: sent / accepted / received / shown / tapped / duplicates / silent / median sent->received")
    for r in report["devices"]:
        lines.append(f"  {r['device']}: {r['sent']} / {r['accepted']} / {r['received']} / {r['shown']} / {r['tapped']} / "
                     f"{r['duplicates']} / {r['silent']} / {_s(r['median_sent_to_received_s'])}")
    lines.append("")
    lines.append("A ping with no receipt is `no receipt yet`, not lost: a phone that has not woken since cannot have "
                 "reported. `silent` means that phone reported a LATER ping.")
    return "\n".join(lines)
