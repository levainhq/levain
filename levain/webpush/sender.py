"""Send one ping to every live subscription, and record what each push service answered.

``spawn`` runs the send in a detached child, the way the consent adapter starts the operator's notify command, so
a gate never waits on a push service.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from levain.webpush import ece, vapid
from levain.webpush.store import PushStore

MAX_PAYLOAD = 3000       # push services accept ~4 KiB; this leaves room for the record's overhead
TIMEOUT = 10.0
TTL = "3600"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # a push service answers in place; a redirect is not followed
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _clip(text: str, room: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= room:
        return text
    return raw[: max(room - 3, 0)].decode("utf-8", "ignore") + "..."


def build_payload(title: str, body: str, tag: str, url: str, ping_id: int) -> bytes:
    payload = {"title": _clip(title, 200), "body": "", "tag": _clip(tag, 200), "url": _clip(url, 500), "ping": ping_id}
    used = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    payload["body"] = _clip(body, MAX_PAYLOAD - used)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _post(queue_dir: Path, sub: dict, data: bytes) -> int:
    endpoint = sub["endpoint"]
    if not endpoint.startswith("https://"):
        return 0
    try:
        body = ece.encrypt(data, ece.b64d(sub["p256dh"]), ece.b64d(sub["auth"]))
        headers = {**vapid.authorization(queue_dir, endpoint), "TTL": TTL, "Urgency": "high",
                   "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream"}
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return int(err.code)
    except Exception:   # noqa: BLE001 - unreachable service, bad keys: recorded as status 0, never raised at the gate
        return 0


def send(queue_dir: Path | str, *, kind: str, ref: str | None, title: str, body: str, url: str = "/") -> list[tuple[int, int]]:
    """Record the ping, push it to each live subscription; returns (subscription id, HTTP status) pairs (status 0:
    no answer). 404 and 410 retire the subscription."""
    qdir = Path(queue_dir)
    store = PushStore(qdir)
    subs = store.live_subscriptions()
    ping_id = store.add_ping(kind, ref, title, body)
    data = build_payload(title, body, ref or kind, url, ping_id)
    results: list[tuple[int, int]] = []
    for sub in subs:
        status = _post(qdir, sub, data)
        store.add_delivery(ping_id, sub["id"], status, retire=status in (404, 410))
        results.append((sub["id"], status))
    return results


def spawn(queue_dir: Path | str, *, kind: str, ref: str | None, title: str, body: str, url: str = "/") -> bool:
    """Start :func:`send` detached, never waited on, in the queue directory. True when the child started."""
    from levain.launch import child_env

    argv = [sys.executable, "-m", "levain.webpush", "send", "--queue-dir", str(queue_dir), "--kind", kind,
            "--title", title, "--body", body, "--url", url]
    if ref is not None:
        argv += ["--ref", ref]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, env=child_env(), cwd=str(queue_dir))
    except Exception:   # noqa: BLE001 - a failed ping never fails the gate
        return False
    return True
