"""Web Push for the phone page: a nudge to the operator's phone that opens the page, where the passkey signs.

A push carries text only. Nothing in this package authorises anything; the real queue is read on the page.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path


def live(queue_dir: Path | str | None) -> bool:
    """True when a paired phone has pings on for this queue directory. Read-only: it never creates the push store, so
    a gate on an install with no phone costs only an ``lstat``. Any fault reads as False (a ping is optional)."""
    if queue_dir is None:
        return False
    from levain.webpush.store import DB_NAME, DIR_NAME

    path = Path(queue_dir) / DIR_NAME / DB_NAME
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return False
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            return conn.execute("SELECT 1 FROM subscriptions WHERE retired_at IS NULL AND device_id IS NOT NULL "
                                "LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return False
