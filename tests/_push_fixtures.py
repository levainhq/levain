"""Test helpers for the push store (W1b slice C): the store accepts a subscription only under a ``paired-<id>`` device whose
cockpit pairing journal holds it unrevoked, so a test that needs "a phone with pings on" pairs one in a journal beside
the queue directory. ``owned_devices`` is the autouse fixture body a test module installs; ``phone`` subscribes one."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from levain.cockpit.firejournal import CockpitDB
from levain.webpush.store import PAIRED_PREFIX, PushStore

_STAMP = "2026-10-27T00:00:00Z"


def journal_of(store: PushStore) -> Path:
    """The pairing journal root beside ``store``'s queue directory, created on first use."""
    root = Path(store.directory).parent / "pairing-journal"
    if not (root / "cockpit.db").exists():
        root.mkdir(parents=True, exist_ok=True)
        CockpitDB(root).set_install_class("reported")
    return root


def pair(store: PushStore, name: str) -> str:
    """The paired device ``name`` in the journal beside ``store`` (made once); returns its credential name."""
    root = journal_of(store)
    conn = sqlite3.connect(root / "cockpit.db", isolation_level=None)
    try:
        conn.execute("INSERT OR IGNORE INTO paired_devices (id, label, token_hash, created, paired_by) "
                     "VALUES (?,?,?,?,?)", (name, name, "test-" + name, _STAMP, "box"))
    finally:
        conn.close()
    return PAIRED_PREFIX + name


def revoke(store: PushStore, name: str | None = None) -> None:
    """Revoke ``name`` (every paired device when None) in the journal beside ``store``."""
    conn = sqlite3.connect(journal_of(store) / "cockpit.db", isolation_level=None)
    try:
        if name is None:
            conn.execute("UPDATE paired_devices SET revoked_at = '2026-10-19T00:00:00Z' WHERE revoked_at IS NULL")
        else:
            conn.execute("UPDATE paired_devices SET revoked_at = '2026-10-19T00:00:00Z' WHERE id = ?", (name,))
    finally:
        conn.close()


def phone(qdir: Path | str, endpoint: str = "https://fcm.googleapis.com/fcm/send/phone", name: str = "phone") -> None:
    """A phone with pings on: a paired device and its live subscription."""
    store = PushStore(qdir)
    store.add_subscription(endpoint, "p", "a", pair(store, name), journal=journal_of(store))


def owned_devices(monkeypatch: Any) -> None:
    """Make ``PushStore.add_subscription`` pair any plain device name a test passes (a ``None`` or empty one still
    reaches the store as given, so its refusal stays testable)."""
    real = PushStore.add_subscription

    def add(self: PushStore, endpoint: str, p256dh: str, auth: str, device_id: Any, **kw: Any) -> int:
        if isinstance(device_id, str) and device_id and not device_id.startswith(PAIRED_PREFIX):
            device_id = pair(self, device_id)
            kw["journal"] = journal_of(self)
        return real(self, endpoint, p256dh, auth, device_id, **kw)

    monkeypatch.setattr(PushStore, "add_subscription", add)
