"""The team-view registry: how the cockpit (`levain serve`) finds the running `levain team view` servers.

One small JSON file per running view, in ``<levain home>/team-views/``. Per-entry files, not one shared file:
a view's file name is unique (``<pid>-<port>.json``), so two views starting at once cannot race and no lock is
needed. A write is a temp file plus ``os.replace`` (atomic), and a view removes its own file on a clean exit.

The levain home is ``$LEVAIN_HOME`` if set, else ``~/.levain`` (``Path.home()`` follows ``$HOME``, which is how
tests and the demo point it at a temp directory and never the real one).

PRIVACY: an entry holds only the repo path, the URL, the project name, the pid and the start time. No ledger
content, no member names, nothing per person.

STALE ENTRIES (a crashed view leaves its file): the reader lists an entry only when BOTH (1) its pid is alive and
(2) its URL answers a loopback probe with this entry's own NONCE. The nonce is random, made when the view starts,
written into its entry and served by that view at GET /team_view.id, so a reused pid, a port that now belongs to
another program, or ANOTHER team view on that port all fail it. The probe is one plain 200 (no redirect is
followed), small, and bounded by one overall deadline; the whole listing has its own deadline. The reader never
deletes anything (the cockpit stays read-only). A starting view prunes entries by the pid in the FILE NAME and
never deletes an entry it merely fails to parse (a newer version's entry must survive an older view). A URL that
is not ``http://`` on a loopback IPv4 host is never listed and never probed.

Stdlib only; imports nothing from the rest of levain.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 1
PROBE_PATH = "/team_view.id"
PROBE_PREFIX = "levain-team-view:"      # the body is exactly PROBE_PREFIX + nonce
PROBE_DEADLINE = 1.0                    # seconds, the WHOLE probe (connect + headers + body)
LIST_DEADLINE = 2.0                     # seconds, the whole live_views() call
PROBE_MAX_BYTES = 256
MAX_VIEWS = 8
_FIELDS = ("repo", "url", "project", "pid", "started", "nonce")
_NAME_RE = re.compile(r"(\d+)-(\d+)\.json")
_TMP_RE = re.compile(r"\.(\d+)-(\d+)\.tmp")
_NONCE_RE = re.compile(r"[0-9a-f]{16,64}")


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


def register(repo: str, url: str, project: str, *, nonce: str, pid: int | None = None) -> Path:
    """Write this view's entry atomically and return its path (pass it to ``unregister``)."""
    norm = loopback_http_url(url)
    if norm is None:
        raise ValueError(f"not a loopback http URL: {url!r}")
    if not _NONCE_RE.fullmatch(nonce or ""):
        raise ValueError("nonce must be 16-64 lowercase hex characters")
    pid = os.getpid() if pid is None else pid
    port = urlsplit(norm).port
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{pid}-{port}.json"
    tmp = d / f".{pid}-{port}.tmp"
    entry = {"v": VERSION, "repo": str(repo), "url": norm, "project": str(project), "pid": pid, "nonce": nonce,
             "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        tmp.write_text(json.dumps(entry, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()        # present only if the write or the replace failed
        except OSError:
            pass
    return path


def unregister(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _read(path: Path) -> dict | None:
    m = _NAME_RE.fullmatch(path.name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not m or not isinstance(raw, dict) or raw.get("v") != VERSION or not all(k in raw for k in _FIELDS):
        return None
    if not isinstance(raw["pid"], int) or isinstance(raw["pid"], bool) or raw["pid"] != int(m.group(1)):
        return None                    # the file name is the pid's identity; a mismatch is not ours to trust
    if not all(isinstance(raw[k], str) for k in ("repo", "project", "started", "nonce")):
        return None
    if not _NONCE_RE.fullmatch(raw["nonce"]):
        return None
    url = loopback_http_url(raw["url"])
    if url is None or urlsplit(url).port != int(m.group(2)):
        return None
    return {"repo": raw["repo"][:500], "url": url, "project": raw["project"][:120], "pid": raw["pid"],
            "started": raw["started"][:40], "nonce": raw["nonce"]}


def prune_dead() -> None:
    """Remove entries (and stray temp files) whose pid, read from the FILE NAME, is gone. A reader never deletes, and
    nothing is ever deleted because it failed to parse: an entry of a newer version must survive an older view."""
    d = registry_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        m = _NAME_RE.fullmatch(n) or _TMP_RE.fullmatch(n)
        if m and not _pid_alive(int(m.group(1))):
            try:
                (d / n).unlink()
            except OSError:
                pass


def probe(url: str, nonce: str, deadline: float = PROBE_DEADLINE) -> bool:
    """Does this loopback URL answer GET /team_view.id with exactly this nonce? One plain 200, no redirect followed,
    at most PROBE_MAX_BYTES read, everything inside one overall deadline (a slow-drip server cannot hold it)."""
    norm = loopback_http_url(url)
    if norm is None:
        return False
    u = urlsplit(norm)
    end = time.monotonic() + deadline
    want = (PROBE_PREFIX + nonce).encode()
    conn = None
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=min(deadline, 0.5))
        conn.request("GET", PROBE_PATH, headers={"Accept": "text/plain", "Connection": "close"})
        resp = conn.getresponse()
        if resp.status != 200:        # 3xx included: http.client never follows, and a redirect is not an answer
            return False
        got = b""
        while len(got) < PROBE_MAX_BYTES and time.monotonic() < end:
            if conn.sock is not None:
                conn.sock.settimeout(max(0.05, min(0.25, end - time.monotonic())))
            chunk = resp.read1(PROBE_MAX_BYTES - len(got))
            if not chunk:
                break
            got += chunk
            if len(got) > len(want) + 1:
                return False
        return got.strip() == want
    except Exception:  # noqa: BLE001 - any failure means "not a live view"
        return False
    finally:
        if conn is not None:
            conn.close()


def live_views(deadline: float = LIST_DEADLINE) -> list[dict]:
    """The registered views alive now: pid alive AND the nonce probe answers. Read-only. Every file is validated in
    turn (the list is never sliced first, so junk cannot hide a real entry) until MAX_VIEWS are confirmed, the
    files run out, or the overall deadline passes."""
    end = time.monotonic() + deadline
    d = registry_dir()
    try:
        names = sorted(n for n in os.listdir(d) if _NAME_RE.fullmatch(n))
    except OSError:
        return []
    out = []
    for n in names:
        if time.monotonic() >= end or len(out) >= MAX_VIEWS:
            break
        e = _read(d / n)
        if e and _pid_alive(e["pid"]) and probe(e["url"], e["nonce"], min(PROBE_DEADLINE, end - time.monotonic())):
            out.append(e)
    return sorted(({k: v for k, v in e.items() if k != "nonce"} for e in out),
                  key=lambda e: (e["project"].casefold(), e["url"]))
