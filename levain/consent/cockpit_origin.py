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
from pathlib import Path

__all__ = ["FILE_NAME", "MAX", "read_origin", "valid", "write_origin"]

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


def write_origin(queue_dir: Path | str, origin: object) -> Path | None:
    """``origin`` into ``<queue dir>/cockpit-origin.json`` whole (a temporary file, then a rename), mode 0600; None
    (nothing written) when it is not a valid https origin."""
    good = valid(origin)
    if good is None:
        return None
    path = Path(queue_dir) / FILE_NAME
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"cockpit_origin": good}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def read_origin(queue_dir: Path | str) -> str | None:
    """The stored origin, or None when there is none or the file is not exactly a valid one."""
    path = Path(queue_dir) / FILE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(_FILE_MAX + 1)
        data = json.loads(raw.decode("utf-8")) if len(raw) <= _FILE_MAX else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"cockpit_origin"}:
        return None
    return valid(data["cockpit_origin"])
