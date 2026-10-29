"""levain.consent.cockpit_origin — the page origin the web cockpit serves its sign sheet on, kept where a reader can find it.

The cockpit learns its origin at boot (the Signer's pinned ``page_origin``) and held it in memory only, so a band reader
(a separate process) had no origin to build a sign request's deep link from (Q17-ALL S3, :func:`levain.consent.sign.deep_link`).
The cockpit now writes it beside the other per-install consent settings, as ``<queue dir>/cockpit-origin.json``::

    {"cockpit_origin": "https://<host name>[:<port>]"}

Only a bare ``https`` origin is stored or read (:func:`valid`): no path, query, fragment, credentials or newline, a host
name never an address, at most :data:`MAX` characters. The operator taps the link from his phone, so the check runs on
write AND on read; a file that fails it reads as no origin, and a reader then shows no link (nothing is guessed). The
file is not a security bound (any process of the operator's uid can rewrite it); it only names where a link points, and
the sheet it opens signs nothing without his passkey.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

__all__ = ["FILE_NAME", "MAX", "read_origin", "valid", "withdraw_origin", "write_origin"]

FILE_NAME = "cockpit-origin.json"
MAX = 300                                  # a host name (253) plus scheme and port
_FILE_MAX = 1024


def valid(text: object) -> str | None:
    """The normalised https origin ``text`` is, else None."""
    from levain.consent.sign import cockpit_origin

    if not isinstance(text, str) or not text.startswith("https://") or len(text) > MAX or text != text.strip() \
            or text.endswith("/"):
        return None
    try:
        return cockpit_origin(text)
    except ValueError:
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass                                 # nothing more can be done; a reader still validates what it finds


def write_origin(queue_dir: Path | str, origin: object) -> Path | None:
    """Starting a server REPLACES the published origin. A valid https ``origin`` goes into
    ``<queue dir>/cockpit-origin.json`` whole (the directory made at 0700 if missing; a temporary file, then a rename),
    mode 0600. Anything else (http, None, a bad value) REMOVES the file, and so does a failed write: no earlier server's
    origin survives a start that cannot publish its own. Returns the path written, else None; never raises OSError."""
    path = Path(queue_dir) / FILE_NAME
    good = valid(origin)
    if good is None:
        _unlink(path)
        return None
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"cockpit_origin": good}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        _unlink(path)
        return None
    finally:
        _unlink(tmp)
    return path


def withdraw_origin(queue_dir: Path | str, origin: object) -> None:
    """Remove the stored origin only if it still holds ``origin`` (this server's own); another instance's value stays."""
    good = valid(origin)
    if good is not None and read_origin(queue_dir) == good:
        _unlink(Path(queue_dir) / FILE_NAME)


def read_origin(queue_dir: Path | str) -> str | None:
    """The stored origin, or None when there is none or the file is not exactly a valid one."""
    path = Path(queue_dir) / FILE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None                  # a FIFO or device at the path is no origin
            raw = fh.read(_FILE_MAX + 1)
        data = json.loads(raw.decode("utf-8")) if raw is not None and len(raw) <= _FILE_MAX else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"cockpit_origin"}:
        return None
    return valid(data["cockpit_origin"])
