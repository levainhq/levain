"""The team-view registry: how the cockpit (`levain serve`) finds the running `levain team view` servers.

One small JSON file per running view, in ``<levain home>/team-views/``. Per-entry files, not one shared file:
a view's file name is unique (``<pid>-<port>.json``), so two views starting at once cannot race and no lock is
needed. A write is a temp file plus ``os.replace`` (atomic), and a view removes its own file on a clean exit.

The levain home is ``$LEVAIN_HOME`` if set, else ``~/.levain`` (``Path.home()`` follows ``$HOME``, which is how
tests and the demo point it at a temp directory and never the real one).

PRIVACY: an entry holds only the repo path, the URL, the project name, the pid and the start time. No ledger
content, no member names, nothing per person.

STALE ENTRIES (a crashed view leaves its file): the reader lists an entry only when BOTH (1) its pid is alive and
(2) its URL answers a short loopback probe whose body is this server's own stylesheet marker. Neither alone is
enough: a live pid can be a reused pid, and an answering port can belong to some other program. The reader never
deletes anything (the cockpit stays read-only); a starting view prunes dead-pid files. A URL that is not
``http://`` on a loopback host is never listed and never probed, so a hand-edited entry cannot make the cockpit
fetch or link elsewhere.

Stdlib only; imports nothing from the rest of levain.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 1
PROBE_PATH = "/team_view.css"
PROBE_MARKER = b"team view additions"   # the first line of the view's own stylesheet
PROBE_TIMEOUT = 0.5
MAX_VIEWS = 8
_FIELDS = ("repo", "url", "project", "pid", "started")
_NAME_RE = re.compile(r"\d+-\d+\.json")


def home() -> Path:
    raw = os.environ.get("LEVAIN_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".levain"


def registry_dir() -> Path:
    return home() / "team-views"


def loopback_http_url(url: object) -> str | None:
    """The normalised URL if it is ``http://`` on 127.x or localhost with an explicit port, else None."""
    if not isinstance(url, str) or len(url) > 200:
        return None
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if u.scheme != "http" or not host or port is None or u.username or u.password:
        return None
    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return None
        if ip.version != 4 or not ip.is_loopback:
            return None
    return f"http://{host}:{port}/"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def register(repo: str, url: str, project: str, *, pid: int | None = None) -> Path:
    """Write this view's entry atomically and return its path (pass it to ``unregister``)."""
    norm = loopback_http_url(url)
    if norm is None:
        raise ValueError(f"not a loopback http URL: {url!r}")
    pid = os.getpid() if pid is None else pid
    port = urlsplit(norm).port
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{pid}-{port}.json"
    tmp = d / f".{pid}-{port}.tmp"
    entry = {"v": VERSION, "repo": str(repo), "url": norm, "project": str(project), "pid": pid,
             "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp.write_text(json.dumps(entry, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def unregister(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _read(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("v") != VERSION or not all(k in raw for k in _FIELDS):
        return None
    if not isinstance(raw["pid"], int) or isinstance(raw["pid"], bool) or raw["pid"] <= 0:
        return None
    if not all(isinstance(raw[k], str) for k in ("repo", "project", "started")):
        return None
    url = loopback_http_url(raw["url"])
    if url is None:
        return None
    return {"repo": raw["repo"][:500], "url": url, "project": raw["project"][:120], "pid": raw["pid"],
            "started": raw["started"][:40]}


def prune_dead() -> None:
    """Remove entries whose pid is gone (called when a view starts; a reader never deletes)."""
    d = registry_dir()
    try:
        names = [n for n in os.listdir(d) if _NAME_RE.fullmatch(n)]
    except OSError:
        return
    for n in names:
        e = _read(d / n)
        if e is None or not _pid_alive(e["pid"]):
            try:
                (d / n).unlink()
            except OSError:
                pass


def probe(url: str) -> bool:
    """Does the loopback URL answer as a team view (its own stylesheet, with the marker)?"""
    if loopback_http_url(url) is None:
        return False
    req = urllib.request.Request(url.rstrip("/") + PROBE_PATH, headers={"Accept": "text/css"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # a loopback probe never goes via a proxy
    try:
        with opener.open(req, timeout=PROBE_TIMEOUT) as r:
            return r.status == 200 and PROBE_MARKER in r.read(4096)
    except Exception:  # noqa: BLE001 - any failure means "not a live view"
        return False


def live_views() -> list[dict]:
    """The registered views that are alive now: pid alive AND the loopback probe answers. Read-only, bounded."""
    d = registry_dir()
    try:
        names = sorted(n for n in os.listdir(d) if _NAME_RE.fullmatch(n))
    except OSError:
        return []
    out = []
    for n in names[:MAX_VIEWS * 2]:
        e = _read(d / n)
        if e and _pid_alive(e["pid"]) and probe(e["url"]):
            out.append(e)
        if len(out) >= MAX_VIEWS:
            break
    return sorted(out, key=lambda e: (e["project"].casefold(), e["url"]))
