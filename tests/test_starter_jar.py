"""The starter jar: level computed from real per-day counts in the entity's own store,
and an EMPTY, labelled jar (never a faked level) when there is no store or no history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from levain.dashboard import _read_jar

NOW = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)


def _store(path, per_day: dict[int, int]) -> None:
    """per_day: {days_ago: n}. Stamped at local noon of that day so host TZ cannot move it."""
    local_noon = NOW.astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE episodes (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, type TEXT, content TEXT)")
    i = 0
    for ago, n in per_day.items():
        for _ in range(n):
            ts = (local_noon - timedelta(days=ago)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            con.execute("INSERT INTO episodes VALUES (?,?,?,?)", (f"e{i}", ts, "observation", "x"))
            i += 1
    con.commit()
    con.close()


def test_level_is_todays_count_against_the_median_day_and_empty_without_data(tmp_path):
    # 6 prior days of 4,4,4,6,8,10 (median 5) and 3 today: 3 / (2*5) = 0.3, typical at the 0.5 mark
    db = tmp_path / "memory.db"
    _store(db, {0: 3, 1: 4, 2: 4, 3: 4, 4: 6, 5: 8, 6: 10})
    jar = _read_jar(db, NOW)
    assert (jar.status, jar.today, jar.typical, jar.history_days) == ("ok", 3, 5.0, 6)
    assert abs(jar.level - 0.3) < 1e-9
    assert jar.label == "3 today · typical 5 (median of 6 d)"
    # saturates at twice typical, never above full
    _store(tmp_path / "busy.db", {0: 40, 1: 4, 2: 4, 3: 4})
    assert _read_jar(tmp_path / "busy.db", NOW).level == 1.0
    # no store, a corrupt store, and too little history are EMPTY and say so, with no invented level
    for bad in (None, tmp_path / "missing.db"):
        j = _read_jar(bad, NOW)
        assert (j.status, j.level, j.today) == ("no_store", 0.0, None)
    junk = tmp_path / "junk.db"; junk.write_bytes(b"not sqlite")
    assert _read_jar(junk, NOW).status == "no_store"
    young = tmp_path / "young.db"; _store(young, {0: 7, 1: 5})
    j = _read_jar(young, NOW)
    assert (j.status, j.level, j.typical, j.today) == ("no_history", 0.0, None, 7)
    assert "only 1 day(s) of history" in j.label
    # reduced motion: the shipped CSS removes the bubbles and the level transition
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "levain/templates/web/dashboard.css").read_text()
    import re
    block = css[re.search(r"prefers-reduced-motion:\s*reduce\)\s*\{\s*\.jar", css).start():]
    assert re.search(r"\.jar-bubble\s*\{\s*display:\s*none", block)
    # the host zone's DST rules apply per instant: on the US fall-back day an episode at
    # 00:30 EDT is still that day's, and a path with URI metacharacters still opens
    import os
    import time
    import pytest
    if not hasattr(time, "tzset"):
        pytest.skip("no time.tzset on this platform; the DST section needs it")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"; time.tzset()
    try:
        fall = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)  # 10:00 EST
        tricky = tmp_path / "a?b#c%20d"; tricky.mkdir()
        db = tricky / "memory.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE episodes (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, type TEXT, content TEXT)")
        stamps = ["2026-11-01T04:30:00.000000Z"] + [f"2026-10-{d:02d}T16:00:00.000000Z" for d in (25, 26, 27, 28, 29, 30) for _ in range(2)]
        for i, ts in enumerate(stamps):
            con.execute("INSERT INTO episodes VALUES (?,?,?,?)", (f"d{i}", ts, "observation", "x"))
        con.commit(); con.close()
        j = _read_jar(db, fall)
        assert (j.status, j.today, j.day) == ("ok", 1, "2026-11-01")  # 04:30Z is 00:30 EDT, TODAY
        # only old rows plus a recent one: the window has a zero-median -> empty and labelled
        far = tmp_path / "far.db"; _store(far, {2: 1, 100: 1})
        assert _read_jar(far, NOW).status == "no_history"
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    # a malformed timestamp row cannot hide real history, and an old-but-quiet store is judged
    # on its zero days, not as a young one
    odd = tmp_path / "odd.db"; _store(odd, {0: 2, 1: 4, 2: 4, 3: 4, 4: 4})
    con = sqlite3.connect(odd); con.execute("INSERT INTO episodes VALUES ('bad','','x','x')"); con.commit(); con.close()
    assert _read_jar(odd, NOW).status == "ok"
