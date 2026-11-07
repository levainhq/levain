"""levain.webpush.declarative: the per-device switch for Declarative Web Push (Phill 11-07, np-029b717e (a)).

A tap on a push to an iPhone Home Screen app was measured never to run the service worker's ``notificationclick`` (the
phone reported no click row for any ping since the bundle5 worker; every landing was a bare ``/`` resume). A Declarative
Web Push payload (``web_push: 8030`` and a ``notification`` with an absolute ``navigate`` url; WebKit blog 16535) lets the OS
show the notification itself and navigate the app on the tap with no worker code. It is OFF for every device: this file
names the devices it is on for, and ``levain ping declarative on --device <id>`` is the only way a device gets in.

The switch is one file, ``<queue dir>/webpush/declarative_devices.json``: ``{"devices": ["paired-d-...", ...]}``. A missing,
unreadable or malformed file means no device (off). Nothing here decides who may see a ping; it picks the payload shape.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

__all__ = ["FILE_NAME", "enabled_devices", "set_device"]

FILE_NAME = "declarative_devices.json"
_DEVICE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_MAX_DEVICES = 32
_FILE_MAX = 8192


def _path(queue_dir: Path | str) -> Path:
    return Path(queue_dir) / "webpush" / FILE_NAME


def enabled_devices(queue_dir: Path | str) -> frozenset[str]:
    """The device ids the switch is on for; empty (off) when the file is missing, unreadable or not exactly the shape
    above. Never raises."""
    try:
        raw = _path(queue_dir).read_bytes()
        if len(raw) > _FILE_MAX:
            return frozenset()
        data = json.loads(raw.decode("utf-8"))
        devices = data["devices"] if isinstance(data, dict) else None
        if not isinstance(devices, list):
            return frozenset()
        return frozenset(d for d in devices if isinstance(d, str) and _DEVICE.fullmatch(d))
    except Exception:  # noqa: BLE001 - a switch that cannot be read is off
        return frozenset()


def set_device(queue_dir: Path | str, device: str, on: bool) -> frozenset[str]:
    """Turn the switch on or off for ``device`` and return the devices it is now on for. Raises ValueError for a device id
    of an unexpected shape or a switch already on for :data:`_MAX_DEVICES` devices. Written atomically (a temp file
    in the same directory, then a rename)."""
    if not isinstance(device, str) or not _DEVICE.fullmatch(device):
        raise ValueError(f"not a device id: {device!r}")
    now = set(enabled_devices(queue_dir))
    now.add(device) if on else now.discard(device)
    if len(now) > _MAX_DEVICES:
        raise ValueError(f"the switch is on for more than {_MAX_DEVICES} devices")
    path = _path(queue_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=FILE_NAME + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"devices": sorted(now)}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return frozenset(now)
