"""levain.webpush.route — the retired route (``desk`` | ``devices``): what is left of it.

Presence 1b (Phill 11-02) folded the route into the one intent record (:mod:`levain.webpush.intent`): ``devices`` is
``devices: on`` and ``desk`` is ``devices: follow``, the measurement deciding. The route file, the automatic suggestion
(``auto_suggest``, the one automatic writer of intent) and the ``who`` scan are gone: nothing flips the record on its own.

What stays:

- :data:`ROUTE_WORDS`, the two tap-token aliases (``desk`` -> follow, ``devices`` -> devices on) that
  :mod:`levain.webpush.presence_tokens` still lists;
- :func:`_load`, the reader of an old ``route.json`` that :func:`levain.webpush.intent.migrate` maps onto the record;
- :func:`remote_login_present` and :func:`_local_host`: the pure parse of ``who`` output, kept for a remote-activity caller
  (levain ships none) that wants to tell an ssh login from a local tab.

Stdlib only.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from pathlib import Path

from levain.webpush.presence import MAX_SECS

__all__ = ["ROUTES", "ROUTE_WORDS", "remote_login_present"]

ROUTES = ("desk", "devices")
ROUTE_WORDS = ROUTES   # the tap-token aliases of the retired route
FILE_NAME = "route.json"
_MAX_BYTES = 1024
_NUM_LIMIT = 1e15
_HOSTED = re.compile(r"\(([^()\s]+)\)\s*$")


def _num(v: object) -> bool:
    # compared, never passed to isfinite: an int of hundreds of digits overflows that call; nan and inf fail the compare
    return not isinstance(v, bool) and isinstance(v, (int, float)) and abs(v) < _NUM_LIMIT


def _load(queue_dir: Path | str | None) -> dict | None:
    """The OLD record on file (``route.json``: ``{route, ts, until, source}``), ``None`` for no file. Raises
    :class:`ValueError` for a file that cannot be read safely. For the migration only."""
    if queue_dir is None:
        return None
    try:
        fd = os.open(Path(queue_dir) / "webpush" / FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"unreadable ({type(e).__name__})") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError as e:
        raise ValueError(f"unreadable ({type(e).__name__})") from e
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise ValueError("file too large")
    try:
        got = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ValueError("not valid JSON") from e
    if not isinstance(got, dict) or got.get("route") not in ROUTES or not _num(got.get("ts")):
        raise ValueError("not a route record")
    until = got.get("until")
    if until is not None and not _num(until):
        raise ValueError("bad until")
    if until is None and got["route"] == "desk":
        raise ValueError("a desk record always ends")
    if got.get("source") not in ("operator", "auto"):
        raise ValueError("unknown source")
    return {"route": got["route"], "ts": float(got["ts"]), "source": got["source"],
            "until": None if until is None else float(min(until, got["ts"] + MAX_SECS))}


def remote_login_present(who_output: str) -> bool:
    """Whether ``who`` output lists a login from another host: a line whose tty is not ``console`` and that ends with a
    parenthesised host (an ssh or mosh session lists as ``user ttysNNN date (host)``). A local Terminal tab has no host and
    does not count; neither does a display name such as ``(:0)`` or a loopback host."""
    for line in (who_output or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] == "console":
            continue
        m = _HOSTED.search(line)
        if m and not _local_host(m.group(1)):
            return True
    return False


def _local_host(host: str) -> bool:
    # parsed, never prefix-matched: "127.evil.example" and "fe80:" lookalikes are names, while 127.0.0.2 is loopback too
    if host.startswith(":") and not host.startswith("::"):
        return True   # a display such as :0
    h = host.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h.lower() == "localhost"
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local
