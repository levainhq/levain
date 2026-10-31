"""Send one ping to every live subscription, and record what each push service answered.

``spawn`` runs the send in a detached child, the way the consent adapter starts the operator's notify command, so
a gate never waits on a push service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import site
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from levain.webpush import ece, vapid
from levain.webpush.store import PushStore

TIMEOUT = 6.0            # per socket operation: connect and read each, so one request ends well inside the budget
WALL_BUDGET = 20.0       # one dead push service never holds the rest of a send longer than this
UNKNOWN = -1             # a delivery not finished by the budget: unknown, never "corrected" by a late answer
TTL = "3600"
# serialized-byte budgets for the fields around the body, so even a title of control characters (six bytes each
# once JSON escapes them) leaves the body room inside ece.MAX_PLAINTEXT
_TITLE_ROOM, _TAG_ROOM, _URL_ROOM = 600, 300, 800
_SPAWN_STDIN_MAX = 12000   # well under a pipe buffer, so the gate's write never blocks on the child


def _dumps(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")


def _jlen(text: str) -> int:
    """Bytes ``text`` takes inside the serialized payload (its JSON string form, without the quotes)."""
    return len(_dumps({"x": text})) - 8


def _fit(text: str, room: int) -> str:
    """The longest prefix of ``text`` (with ``...`` when cut) whose serialized form is at most ``room`` bytes.
    A binary search over the prefix length: it ends in about log2(len) steps whatever the characters are."""
    if _jlen(text) <= room:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _jlen(text[:mid] + "...") <= room:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "..."


def build_payload(title: str, body: str, tag: str, url: str, ping_id: int) -> bytes:
    """The JSON the service worker reads, at most :data:`ece.MAX_PLAINTEXT` bytes as SERIALIZED (quotes and control
    characters escape to two and six bytes, so the clip is by serialized size, never by raw length)."""
    payload = {"title": _fit(title, _TITLE_ROOM), "body": "", "tag": _fit(tag, _TAG_ROOM),
              "url": _fit(url, _URL_ROOM), "ping": ping_id}
    payload["body"] = _fit(body, ece.MAX_PLAINTEXT - len(_dumps(payload)))
    data = _dumps(payload)
    assert len(data) <= ece.MAX_PLAINTEXT
    return data


def topic_for(ref: str | None) -> str | None:
    """The RFC 8030 section 5.4 ``Topic`` for a ping about ``ref``: a re-ping of the same request replaces the one
    still queued for an offline phone. 32 url-safe characters; None when the ping has no ref (a test ping)."""
    if not ref:
        return None
    return base64.urlsafe_b64encode(hashlib.sha256(ref.encode("utf-8", "replace")).digest()[:24]).decode("ascii")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # a push service answers in place; a redirect is not followed
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


_TRANSIENT = object()   # _prepare: no request this time, and nothing wrong with the subscription


def _prepare(queue_dir: Path, sub: dict, data: bytes, topic: str | None) -> "urllib.request.Request | object | None":
    """The request for one subscription, or None when its endpoint is not a known push service or its keys fail.
    Runs in the sending thread (it touches the store for the VAPID token); the pool only does the network."""
    from levain.webpush.routes import valid_endpoint

    endpoint = valid_endpoint(sub["endpoint"])
    if endpoint is None:
        return None
    try:
        body = ece.encrypt(data, ece.b64d(sub["p256dh"]), ece.b64d(sub["auth"]))
    except Exception:   # noqa: BLE001 - bad keys: recorded as status 0, never raised at the gate
        return None
    try:
        auth = vapid.authorization(queue_dir, endpoint)
    except Exception:   # noqa: BLE001 - the store, not the subscription: status 0, the subscription stays live
        return _TRANSIENT
    headers = {**auth, "TTL": TTL, "Urgency": "high",
               "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream"}
    if topic:
        headers["Topic"] = topic
    return urllib.request.Request(endpoint, data=body, headers=headers, method="POST")


class _Dead(int):
    """A status that is not 404 or 410 but retires the subscription all the same: the push service named the key as the
    fault (VapidPkHashMismatch: the subscription was made under another Levain install's key), so no later ping fares better."""


def retires(status: int) -> bool:
    """True when the push service's answer retires the subscription: 404 or 410, or a refusal that names the key
    (:class:`_Dead`). The one rule :func:`deliver` retires by and :mod:`levain.webpush.held` reads a retired device by."""
    return status in (404, 410) or isinstance(status, _Dead)


def _post(req: urllib.request.Request) -> int:
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as err:
        try:
            if err.code in (400, 403) and b"VapidPkHashMismatch" in err.read(512):
                return _Dead(err.code)
        except Exception:   # noqa: BLE001 - an unreadable body is an ordinary refusal
            pass
        return int(err.code)
    except Exception:   # noqa: BLE001 - unreachable service: recorded as status 0, never raised at the gate
        return 0


def send(queue_dir: Path | str, *, kind: str, ref: str | None, title: str, body: str, url: str = "/",
         command: bool | None = None, push: bool = True, hold: str | None = None,
         device: str | None = None) -> list[tuple[int, int]]:
    """Record the ping (``command``: whether the notify command started for it, None when none is set), push it to each live subscription concurrently; returns (subscription id, HTTP status)
    pairs (status 0: no answer; :data:`UNKNOWN`: none within :data:`WALL_BUDGET`). 404 and 410 retire the subscription, and so does a refusal that names the key (:class:`_Dead`). Every
    store write happens in this thread; the workers only talk to the push services. ``push=False`` pushes nothing; with
    ``hold`` (the reason, see :meth:`PushStore.add_ping`) the recorded ping also waits in the held queue for
    :func:`levain.webpush.held.flush`. ``device`` (a ``paired-`` name) sends to that device's subscriptions only; the
    default is every live one."""
    qdir = Path(queue_dir)
    store = PushStore(qdir)
    # the ping row and its held row are one transaction: a ping is never recorded as held-for-later and then lost
    ping_id = store.add_ping(kind, ref, title, body, command, hold=(url, hold) if hold and not push else None)
    if not push:   # the caller saw no live phone, or the presence rule held the push
        return []
    return deliver(qdir, store, ping_id, kind=kind, ref=ref, title=title, body=body, url=url, device=device)


def deliver(qdir: Path, store: PushStore, ping_id: int, *, kind: str, ref: str | None, title: str, body: str,
            url: str, device: str | None = None) -> list[tuple[int, int]]:
    """Push the already recorded ping ``ping_id`` to each live subscription (``device``: that device's only) and
    record each answer."""
    subs = store.live_subscriptions(device=device)
    data = build_payload(title, body, ref or kind, url, ping_id)
    topic = topic_for(ref)
    reqs = [(sub, _prepare(qdir, sub, data, topic)) for sub in subs]
    statuses: dict[int, int] = {}

    def work(sid: int, req: urllib.request.Request) -> None:
        statuses[sid] = _post(req)

    # one daemon thread per live subscription (the store caps them at MAX_SUBSCRIPTIONS), so every send starts
    # at once and an unfinished one never holds the process past the budget
    workers = [threading.Thread(target=work, args=(sub["id"], r), daemon=True) for sub, r in reqs
               if isinstance(r, urllib.request.Request)]
    deadline = time.monotonic() + WALL_BUDGET
    for t in workers:
        t.start()
    for t in workers:
        t.join(max(0.0, deadline - time.monotonic()))
    answered = dict(statuses)   # one snapshot: an answer after the budget is never written
    results: list[tuple[int, int]] = []
    for sub, r in reqs:
        if r is None:   # bad stored keys or an endpoint off the allowlist: no later ping fares better
            status = _Dead(0)
            retire = True
        elif r is _TRANSIENT:
            status, retire = 0, False
        else:
            status = answered.get(sub["id"], UNKNOWN)
            retire = retires(status)
        store.add_delivery(ping_id, sub["id"], status, retire=retire)
        results.append((sub["id"], status))
    return results


def spawn(queue_dir: Path | str, *, kind: str, ref: str | None, title: str, body: str, url: str = "/",
          command: bool | None = None, push: bool = True, hold: str | None = None) -> bool:
    """Start :func:`send` detached, never waited on. ``push=False`` records the ping and pushes it to no phone: how a
    ping that only the notify command carried gets its row, decided by the caller's own read of the phones. The text goes to the child on its
    STDIN as JSON, so a body that begins with ``-`` is not an option and the consent text never sits in the process
    table. True when the child started."""
    from levain.launch import child_env

    fields = {"title": title[:300], "body": body[:4000], "ref": None if ref is None else ref[:200], "url": url[:500],
              "command": command, "push": push, "hold": hold}
    data = json.dumps(fields).encode("utf-8", "replace")
    while len(data) > _SPAWN_STDIN_MAX and fields["body"]:
        fields["body"] = fields["body"][: len(fields["body"]) // 2]
        data = json.dumps(fields).encode("utf-8", "replace")
    env = child_env()
    root = str(Path(__file__).resolve().parents[2])   # a source checkout: -P leaves the child no other way to import levain
    if not any(root == str(Path(d).resolve()) for d in site.getsitepackages() + [site.getusersitepackages()]):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    try:
        # the child runs in "/", so a relative queue dir is resolved here, against this process's cwd
        argv = [sys.executable, "-P", "-m", "levain.webpush", "send", "--queue-dir",
                str(Path(queue_dir).resolve()), "--kind", kind]
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True, env=env, cwd=os.sep)
        try:
            child.stdin.write(data)
        finally:
            child.stdin.close()
    except Exception:   # noqa: BLE001 - a failed ping never fails the gate
        return False
    return True
