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
    from levain.webpush.store import DB_NAME, DIR_NAME, live_rows

    path = Path(queue_dir) / DIR_NAME / DB_NAME
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return False
        # a short wait: a gate hook reads this, and a sender holding the write lock must not stall the gate
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        try:
            return bool(live_rows(conn, first=True))     # the send's own rule (PushStore.live_subscriptions)
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return False
