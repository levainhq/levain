"""M17 L3 r2 (codex MED): a grant's ``refused_count`` is one aggregate query per grant, not every historical request
id of the instance brought into Python. The old logic is kept here as the oracle; the new count must equal it."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from levain.consent.ledger import request_outcomes
from levain.consent.listing import read_grant_evidence
from tests.test_grant_phone import world  # noqa: F401  (world is a fixture)
from tests.test_grant_phone import _now
from tests.test_phone_grants_tab import _answered, _grant

MATCH = ("via = 'grant' AND state = 'allowed' AND json_extract(answer, '$.grant_id') = ? "
         "AND json_extract(answer, '$.grant_seq') = ? AND json_extract(answer, '$.grant_hash') = ?")


def _oracle(qdir: Path, gid: str, seq: int, link_hash: str) -> int:
    """The pre-fix logic: every request id of the instance, outcomes read for all of them, refused counted."""
    with sqlite3.connect(qdir / "queue.db") as conn:
        ids = [r[0] for r in conn.execute(f"SELECT id FROM requests WHERE {MATCH}", (gid, seq, link_hash))]
    outcomes = request_outcomes(qdir, ids)
    return sum(1 for rid in ids if outcomes.get(rid) == "refused")


def _link(world, gid):  # noqa: F811
    from levain.consent.grants import grant_view
    row = {g["id"]: g for g in grant_view(world.qdir, now=_now())["grants"]}[gid]
    return row


def _links(qdir: Path, gids):
    with sqlite3.connect(qdir / "queue.db") as conn:
        got = {}
        for gid in gids:
            r = conn.execute("SELECT json_extract(answer,'$.grant_seq'), json_extract(answer,'$.grant_hash') "
                             "FROM requests WHERE json_extract(answer,'$.grant_id') = ? LIMIT 1", (gid,)).fetchone()
            got[gid] = (gid, r[0], r[1])
        return got


def test_the_aggregate_count_equals_the_old_per_id_count(world):
    _grant(world, "cg-0000000000aa", "Write")
    _grant(world, "cg-0000000000bb", "Edit")
    for i in range(12):
        _answered(world, "Edit", f"e{i}.txt", "refused" if i % 3 == 0 else "accepted")
    for i in range(7):
        _answered(world, "Write", f"w{i}.txt", "refused" if i % 2 else "accepted")
    links = list(_links(world.qdir, ["cg-0000000000aa", "cg-0000000000bb"]).values())
    got = read_grant_evidence(world.qdir, links)
    for gid, seq, h in links:
        assert got[gid]["refused_count"] == _oracle(world.qdir, gid, seq, h)
    assert (got["cg-0000000000bb"]["refused_count"], got["cg-0000000000aa"]["refused_count"]) == (4, 3)
    assert got["cg-0000000000bb"]["count"] == 12 and len(got["cg-0000000000bb"]["recent"]) == 5


def test_no_ledger_counts_none_and_a_grant_with_no_answers_is_absent(world):
    _grant(world, "cg-0000000000aa", "Write")
    _answered(world, "Write", "w.txt", "refused")
    links = list(_links(world.qdir, ["cg-0000000000aa"]).values())
    (world.qdir / "ledger.db").rename(world.qdir / "ledger.moved")
    got = read_grant_evidence(world.qdir, links)
    assert got["cg-0000000000aa"]["refused_count"] == 0 and got["cg-0000000000aa"]["count"] == 1
    assert read_grant_evidence(world.qdir, [("cg-0000000000zz", 1, "x")]) == {}


def test_the_count_stays_one_query_at_scale(world):
    """~20,000 requests answered by one grant: the listing makes ONE count query per grant and holds no id list.
    (A cost, not a wrong answer: this asserts the shape and records the timing.)"""
    _grant(world, "cg-0000000000bb", "Edit")
    for i in range(6):
        _answered(world, "Edit", f"e{i}.txt", "refused" if i < 2 else "accepted")
    q, led = world.qdir / "queue.db", world.qdir / "ledger.db"
    n = 20000
    with sqlite3.connect(q) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        seed = conn.execute("SELECT * FROM requests WHERE via = 'grant' ORDER BY rowid LIMIT 1").fetchone()
        rows = []
        for i in range(n):
            row = dict(zip(cols, seed))
            row["id"] = f"bulk-{i}"
            if "event_id" in row:
                row["event_id"] = f"ev-{i}"
            rows.append(tuple(row[c] for c in cols))
        conn.executemany(f"INSERT INTO requests ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", rows)
    with sqlite3.connect(led) as conn:
        lcols = [r[1] for r in conn.execute("PRAGMA table_info(calls)") if r[1] != "rowid"]
        pre = conn.execute("SELECT * FROM calls WHERE phase = 'pre' AND request_id IS NOT NULL LIMIT 1").fetchone()
        names = [r[1] for r in conn.execute("PRAGMA table_info(calls)")]
        fail = conn.execute("SELECT * FROM calls WHERE phase = 'post_failure' LIMIT 1").fetchone()
        rows = []
        for i in range(n):
            row = dict(zip(names, pre))
            row["request_id"], row["tool_use_id"] = f"bulk-{i}", f"tu-bulk-{i}"
            row["outcome"] = "unknown"
            row["id"] = f"bulk-call-{i}"
            rows.append(tuple(row[c] for c in lcols))
            if i % 4 == 0:                     # a refusal is the post row of the call (the table's CHECK: no stamp)
                post = dict(zip(names, fail))
                post.update(request_id=None, tool_use_id=f"tu-bulk-{i}", id=f"bulk-post-{i}")
                rows.append(tuple(post[c] for c in lcols))
        conn.executemany(f"INSERT INTO calls ({','.join(lcols)}) VALUES ({','.join('?' for _ in lcols)})", rows)
    link = list(_links(world.qdir, ["cg-0000000000bb"]).values())
    seen: list[str] = []
    import sqlite3 as _s
    real = _s.connect

    class _Spy:
        def __init__(self, c): self._c = c
        def execute(self, sql, *a):
            seen.append(sql)
            return self._c.execute(sql, *a)
        def __getattr__(self, k): return getattr(self._c, k)

    import levain.consent.listing as listing
    listing.sqlite3 = type("S", (), {"connect": staticmethod(lambda *a, **k: _Spy(real(*a, **k))),
                                     "OperationalError": _s.OperationalError, "Connection": _s.Connection,
                                     "Error": _s.Error})
    try:
        t0 = time.perf_counter()
        got = read_grant_evidence(world.qdir, link)
        dt = time.perf_counter() - t0
    finally:
        listing.sqlite3 = _s
    refused_queries = [s for s in seen if "GROUP BY 1, 2, 3" in s]
    assert len(refused_queries) == 1
    assert not any(s.strip().startswith("SELECT id FROM requests") for s in seen)     # the id list is never read
    gid = link[0][0]
    assert got[gid]["count"] == 6 + n and len(got[gid]["recent"]) == 5
    assert got[gid]["refused_count"] == 2 + n // 4
    assert got[gid]["refused_count"] == _oracle(world.qdir, *link[0])
    assert dt < 10


def test_two_links_of_one_grant_id_count_the_instance_they_list(world):
    """Complement LOW 3: the same gid listed under two (seq, hash) links. ``count`` and ``refused_count`` must name the
    SAME instance: the one that answered, never the last link listed."""
    _grant(world, "cg-0000000000aa", "Write")
    for i in range(4):
        _answered(world, "Write", f"w{i}.txt", "refused" if i < 3 else "accepted")
    (_, seq, h), = _links(world.qdir, ["cg-0000000000aa"]).values()
    # sorted() puts (gid, seq+1, ...) after the real link: the dead link is the last one listed
    got = read_grant_evidence(world.qdir, [("cg-0000000000aa", seq, h), ("cg-0000000000aa", seq + 1, "dead")])
    assert got["cg-0000000000aa"]["count"] == 4 and got["cg-0000000000aa"]["refused_count"] == 3


def test_refused_never_exceeds_count_when_an_answer_lands_between_reads(world):
    """Codex MED: a grant answer landing after the count was read must not reach the refusal count. The seam answers a
    new refused call right after the instance's count query ran; one snapshot keeps count and refusals together."""
    _grant(world, "cg-0000000000aa", "Write")
    for i in range(3):
        _answered(world, "Write", f"w{i}.txt", "refused")
    links = list(_links(world.qdir, ["cg-0000000000aa"]).values())
    import levain.consent.listing as listing
    real = sqlite3.connect
    fired: list[int] = []

    class _Seam:
        def __init__(self, c): self._c = c
        def execute(self, sql, *a):
            r = self._c.execute(sql, *a)
            if "COUNT(*), MAX(decided_at)" in sql and not fired:
                fired.append(1)
                r = r.fetchall()
                _answered(world, "Write", "late.txt", "refused")
                return iter(r) if False else _Rows(r)
            return r
        def __getattr__(self, k): return getattr(self._c, k)

    class _Rows(list):
        def fetchone(self): return self[0] if self else None
        def fetchall(self): return list(self)

    listing.sqlite3 = type("S", (), {"connect": staticmethod(lambda *a, **k: _Seam(real(*a, **k))),
                                     "OperationalError": sqlite3.OperationalError, "Connection": sqlite3.Connection,
                                     "Error": sqlite3.Error})
    try:
        got = read_grant_evidence(world.qdir, links)
    finally:
        listing.sqlite3 = sqlite3
    assert fired
    g = got["cg-0000000000aa"]
    assert g["count"] == 3 and g["refused_count"] <= g["count"]
    assert g["refused_count"] == 3
