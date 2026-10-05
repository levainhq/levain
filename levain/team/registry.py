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

import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 1
PROBE_PATH = "/team_view.id"
PROBE_PREFIX = "levain-team-view:"      # the body is exactly PROBE_PREFIX + nonce
PROBE_DEADLINE = 1.0                    # seconds, the WHOLE probe (connect + headers + body)
LIST_DEADLINE = 2.0                     # seconds, the whole live_views() call
PROBE_MAX_BYTES = 1024                 # status line + headers (the guard CSP alone is ~200) + body, all inside this
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


def _pid_alive(pid: object) -> bool:
    """Is this a live process? pid <= 0 and non-ints are never alive (kill(0, ...) would signal OUR process group)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):   # no such process, or a pid too large for the C long
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


def _pid_alive_shape(pid: object) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2 ** 31


def _read(path: Path) -> dict | None:
    m = _NAME_RE.fullmatch(path.name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not m or not isinstance(raw, dict) or raw.get("v") != VERSION or not all(k in raw for k in _FIELDS):
        return None
    if not _pid_alive_shape(raw["pid"]) or raw["pid"] != int(m.group(1)):
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
        try:   # one poisoned name (a 200-digit pid, pid 0) must never abort the loop
            m = _NAME_RE.fullmatch(n) or _TMP_RE.fullmatch(n)
            if m and not _pid_alive(int(m.group(1))):
                (d / n).unlink()
        except Exception:  # noqa: BLE001
            continue


def probe(url: str, nonce: str, deadline: float = PROBE_DEADLINE) -> bool:
    """Does this loopback URL answer GET /team_view.id with exactly this nonce?

    A raw socket, not http.client: its getresponse() has no overall deadline, so a status line or headers dripped
    a byte at a time would hold the probe. Here connect, request, status line, headers and body all draw on ONE
    budget (``deadline`` seconds, at most PROBE_MAX_BYTES received); the loop stops at the first complete answer.
    Only the status line is parsed (it must be ``HTTP/1.x 200``), so no redirect is ever followed, and the body
    must be exactly the nonce text. ``localhost`` is taken as 127.0.0.1 with no name resolution."""
    norm = loopback_http_url(url)
    if norm is None:
        return False
    u = urlsplit(norm)
    host = "127.0.0.1" if u.hostname == "localhost" else u.hostname
    end = time.monotonic() + deadline
    want = (PROBE_PREFIX + nonce).encode()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)   # connect() to a numeric IPv4 tuple resolves nothing
        sock.settimeout(max(0.05, min(deadline, end - time.monotonic())))
        sock.connect((host, u.port))
        sock.sendall(f"GET {PROBE_PATH} HTTP/1.1\r\nHost: {host}:{u.port}\r\nAccept: text/plain\r\n"
                     "Connection: close\r\n\r\n".encode("ascii"))
        buf = b""
        while len(buf) < PROBE_MAX_BYTES:
            left = end - time.monotonic()
            if left <= 0:
                return False
            sock.settimeout(left)
            chunk = sock.recv(PROBE_MAX_BYTES - len(buf))
            if not chunk:
                break
            buf += chunk
            head, sep, body = buf.partition(b"\r\n\r\n")
            if sep and body.strip() == want:
                return _status_ok(head)
            if sep and len(body) > len(want) + 2:
                return False
        head, sep, body = buf.partition(b"\r\n\r\n")
        return bool(sep) and _status_ok(head) and body.strip() == want
    except Exception:  # noqa: BLE001 - any failure means "not a live view"
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _status_ok(head: bytes) -> bool:
    first = head.split(b"\r\n", 1)[0]
    return re.fullmatch(rb"HTTP/1\.[01] 200( .*)?", first) is not None


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
        try:   # one poisoned file must never abort the loop or hide the real views after it
            e = _read(d / n)
            if e and _pid_alive(e["pid"]) and probe(e["url"], e["nonce"], min(PROBE_DEADLINE, end - time.monotonic())):
                out.append(e)
        except Exception:  # noqa: BLE001
            continue
    return sorted(({k: v for k, v in e.items() if k != "nonce"} for e in out),
                  key=lambda e: (e["project"].casefold(), e["url"]))
