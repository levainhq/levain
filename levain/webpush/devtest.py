"""The notification-action device test (band 299): ``/devtest`` is a page Phill opens in the Home Screen app; it shows one
test notification WITH action buttons and reports what iOS did (``shown``) and what a tap did (``click``) to
``POST /webpush/devtest``. This module keeps those reports (the newest :data:`KEEP`, one file beside the push store) and
prints them for the desk: ``levain ping devtest [--json]``.

A report is a tiny allowlisted record, nothing else is stored: ``{event: shown|click, action: str<=20, state: str<=20,
maxActions: int|null, ua: str<=80}``. A device posts at most :data:`RATE_MAX` reports a minute.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path

FILE = "devtest_events.jsonl"
KEEP = 100
RATE_MAX = 12        # reports one device may post inside RATE_WINDOW seconds
RATE_WINDOW = 60.0
LOCK_WAIT_SECS = 2.0
_KEYS = frozenset({"event", "action", "state", "maxActions", "ua"})
_EVENTS = frozenset({"shown", "click"})
_LIMITS = {"action": 20, "state": 20, "ua": 80}


class Busy(Exception):
    """The report file's lock was not free in time; the page posts again."""


class RateLimited(Exception):
    """This device posted more than :data:`RATE_MAX` reports inside :data:`RATE_WINDOW` seconds."""


def _text_ok(v: object, limit: int) -> bool:
    return isinstance(v, str) and len(v) <= limit and v.isprintable()


def clean(obj: object) -> dict[str, object] | None:
    """The allowlisted report, or None when ``obj`` is not exactly such a record (an unknown key, a wrong type, an
    over-long or non-printable string, an event outside ``shown``/``click``)."""
    if not isinstance(obj, dict) or not set(obj) <= _KEYS:
        return None
    ev = obj.get("event")
    if not isinstance(ev, str) or ev not in _EVENTS:   # a list or dict here is unhashable: refuse it, never raise
        return None
    out: dict[str, object] = {"event": obj["event"]}
    for k, limit in _LIMITS.items():
        v = obj.get(k, "")
        if not _text_ok(v, limit):
            return None
        out[k] = v
    m = obj.get("maxActions")
    if m is not None and (isinstance(m, bool) or not isinstance(m, int) or not 0 <= m <= 1000):
        return None
    out["maxActions"] = m
    return out


def _lines(d: Path) -> list[str]:
    try:
        return [ln for ln in (d / FILE).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def record(queue_dir: str | os.PathLike[str], device_id: str | None, report: dict[str, object], *, rate: bool = True) -> int:
    """Add one cleaned report and return how many are kept. The file is replaced whole under a sidecar lock, so a reader
    sees the old file or the new one. Raises :class:`RateLimited` (unless ``rate`` is False: the server's own rows),
    :class:`Busy`, or ``OSError``."""
    d = Path(queue_dir)
    now = time.time()
    lock = os.open(d / (FILE + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy from None
                time.sleep(0.02)
        old = _lines(d)
        recent = 0
        for ln in old:   # every kept row (at most KEEP): other devices' rows must not push this device's out of the count
            try:
                row = json.loads(ln)
                mine = isinstance(row, dict) and row.get("device") == device_id and now - float(row.get("ts") or 0) < RATE_WINDOW
            except (ValueError, TypeError):
                continue
            if mine:
                recent += 1
        if rate and recent >= RATE_MAX:
            raise RateLimited
        row = json.dumps({"ts": round(now, 3), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": device_id, **report},
                         separators=(",", ":"))
        lines = old[-(KEEP - 1):] + [row]
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".devtest_events.tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, d / FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(lines)
    finally:
        os.close(lock)


def events(queue_dir: str | os.PathLike[str]) -> list[dict[str, object]]:
    """Every kept report, oldest first; an unreadable line is skipped."""
    out = []
    for ln in _lines(Path(queue_dir)):
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _plain(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def summary(rows: list[dict[str, object]]) -> dict[str, object]:
    """What the desk wants in one look: whether actions were ever reported as supported, and what each tap did."""
    shown = [r for r in rows if r.get("event") == "shown"]
    clicks = [r for r in rows if r.get("event") == "click"]
    reported = [r for r in rows if r.get("event") in ("shown", "click")]
    pushed = [r for r in rows if r.get("event") == "pushed"]
    return {
        "shown": len(shown), "clicks": len(clicks), "requested": sum(1 for r in rows if r.get("event") == "requested"),
        "pushed": [r.get("state") for r in pushed],   # what the push service answered each server-sent push
        # what the phone itself said, newest first: a click carries it as well as a shown report
        "maxActions": next((r.get("maxActions") for r in reversed(reported) if r.get("maxActions") is not None), None),
        "held": [r.get("action") for r in shown],     # what the phone held for each shown notification (actions=N, absent, ...)
        "action_taps": [r for r in clicks if r.get("action") in ("approve", "deny")],
        "body_taps": [r for r in clicks if r.get("action") == "(body tap)"],
        "verdict": ("no tap reported yet" if not clicks else
                    "ACTION BUTTONS WORKED: an approve/deny tap was reported" if any(r.get("action") in ("approve", "deny") for r in clicks)
                    else "only body taps reported: no action button was usable"),
    }


def render(rows: list[dict[str, object]]) -> str:
    out = [_plain(f"{r.get('at')} device={r.get('device')} {r.get('event')} action={r.get('action')!r} "
                  f"state={r.get('state')!r} maxActions={r.get('maxActions')} ua={r.get('ua')!r}") for r in rows]
    s = summary(rows)
    out.append(f"-- shown={s['shown']} clicks={s['clicks']} maxActions={s['maxActions']}: {s['verdict']}")
    return "\n".join(out)


# --- the server-sent test push ------------------------------------------------------------------------------------------
# A page timer cannot fire once iOS suspends or closes the app, so the test notification is a real web push, the product's own
# path: ``POST /webpush/devtest/send {delay_s}`` records the request and, after the delay, a server thread pushes ONE devtest
# payload to the REQUESTING device's subscriptions only. The worker's push handler shows it with the action buttons.
SEND_DELAYS = (10, 60)
SEND_MAX = 6              # requests one device may make inside SEND_WINDOW seconds
SEND_WINDOW = 3600.0
TITLE = "levain device test"
BODY = "Tap Approve, Deny, or the body. This asks nothing of you."
ACTIONS = [{"action": "approve", "title": "Approve"}, {"action": "deny", "title": "Deny"}]
EXTRA = {"devtest": True, "actions": ACTIONS}   # merged into the push payload (sender.deliver ``extra``)
_PAIRED = "paired-"


class NoDevice(Exception):
    """The caller is not a paired device, or it holds no live push subscription: there is nowhere to send to."""


class Pending(Exception):
    """This device already has a test push waiting for its delay."""


class SendLimited(Exception):
    """This device asked for more than :data:`SEND_MAX` test pushes inside :data:`SEND_WINDOW` seconds."""


_lock = threading.Lock()
_pending: dict[str, object] = {}
_asked: dict[str, list[float]] = {}


def clean_send(obj: object) -> int | None:
    """The delay in seconds when ``obj`` is exactly ``{"delay_s": 10|60}``, else None."""
    if not isinstance(obj, dict) or set(obj) != {"delay_s"}:
        return None
    d = obj["delay_s"]
    return d if type(d) is int and d in SEND_DELAYS else None


def _live(queue_dir: str | os.PathLike[str], device_id: str) -> bool:
    from levain.webpush.store import PushStore

    return bool(PushStore(queue_dir).live_subscriptions(device=device_id))


def _timer(delay: float, fn):   # a seam: tests run the callback by hand
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


def _note(queue_dir, device_id: str, event: str, state: str) -> None:
    try:
        record(queue_dir, device_id, {"event": event, "action": "", "state": state[:20], "maxActions": None, "ua": ""}, rate=False)
    except (OSError, Busy):
        pass   # the desk read loses a row; the test goes on


def _push(queue_dir, device_id: str) -> None:
    """The delayed send, in the timer thread. Pushes to ``device_id`` only. Under a test run it sends nothing unless the
    test replaced ``sender.send`` itself."""
    from levain.home_guard import under_test_run
    from levain.webpush import sender

    try:
        if under_test_run() and getattr(sender.send, "__module__", "") == sender.__name__:
            _note(queue_dir, device_id, "pushed", "blocked: test run")
            return
        results = sender.send(queue_dir, kind="devtest", ref=None, title=TITLE, body=BODY, url="/devtest", device=device_id, extra=EXTRA)
        _note(queue_dir, device_id, "pushed", ",".join(str(s) for _, s in results) or "no subscription")
    except Exception as err:   # noqa: BLE001 - a timer thread never raises
        _note(queue_dir, device_id, "pushed", "error " + type(err).__name__)
    finally:
        with _lock:
            _pending.pop(device_id, None)


def request_send(queue_dir: str | os.PathLike[str], device_id: str | None, delay_s: int) -> dict[str, object]:
    """Record the request and start the delayed push to ``device_id`` alone. Raises :class:`NoDevice`, :class:`Pending`,
    :class:`SendLimited`. At most one pending push per device and :data:`SEND_MAX` requests an hour."""
    if not isinstance(device_id, str) or not device_id.startswith(_PAIRED) or not _live(queue_dir, device_id):
        raise NoDevice
    now = time.time()
    with _lock:
        if device_id in _pending:
            raise Pending
        recent = [t for t in _asked.get(device_id, []) if now - t < SEND_WINDOW]
        if len(recent) >= SEND_MAX:
            _asked[device_id] = recent
            raise SendLimited
        _asked[device_id] = recent + [now]
        _pending[device_id] = True   # held before the timer exists, so a second request cannot slip in
    try:
        _note(queue_dir, device_id, "requested", f"delay={delay_s}s")
        timer = _timer(delay_s, lambda: _push(queue_dir, device_id))
    except BaseException:
        with _lock:
            _pending.pop(device_id, None)
        raise
    with _lock:
        if device_id in _pending:
            _pending[device_id] = timer
    return {"delay_s": delay_s, "pending": True}
