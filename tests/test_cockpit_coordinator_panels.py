"""C4-b / C6-b: the review queue and the coordinator morning card on the cockpit, read-only over the real coordinator store."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.cockpit.coordinator_panels import MORNING_ID, REVIEW_ID, coordinator_discoverer
from levain.cockpit.engine import Cockpit
from levain.cockpit.providers import build_default_cockpit
from levain.coordinator import morning, review
from levain.coordinator.store import CoordStore
from tests.test_cockpit_k2a import _install

CRED = {"class": "none", "device_class": None}


def _ids(ck: Cockpit) -> list[str]:
    return [p for z in ck.manifest(CRED)["regions"]["zones"] for p in z["panels"]]


def _cockpit(directory) -> Cockpit:
    ck = Cockpit(entity=lambda ctx: {"name": "t"})
    ck.discover(coordinator_discoverer(lambda: directory), retire=True)
    return ck


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


def test_no_store_file_draws_no_panel_and_creates_none(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    ck = _cockpit(d)
    assert REVIEW_ID not in _ids(ck) and MORNING_ID not in _ids(ck)
    assert list(d.iterdir()) == []                      # a read never makes the store


def test_the_queue_lists_open_items_newest_first_with_feed_label_age_and_who_clears(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    mine, _ = review.file_item(store, "doubt", {"text": "the receipt does not match"}, dedupe_key="b",
                               authority_relevant=True)
    done, _ = review.file_item(store, "end", {"text": "a finished thing"}, dedupe_key="c")
    review.clear_item(store, done.item_id)
    ck = _cockpit(d)
    assert REVIEW_ID in _ids(ck)
    panel = ck.panel(REVIEW_ID)
    # newest open first, then the cleared one (listed so it can be reopened)
    assert [r["id"] for r in panel["rows"]][0] == "review:" + mine.item_id
    assert [r["id"] for r in panel["rows"]][2] == "review:" + done.item_id and len(panel["rows"]) == 3
    bodies = [r["body"] for r in panel["rows"]]
    assert bodies[0].startswith("Doubt · just now · yours to clear: ") and "the receipt does not match" in bodies[0]
    assert bodies[1].startswith("Seat died · just now: ")
    assert bodies[2].startswith("Session ended at a limit · cleared just now: ")
    assert panel["count"] == 2                    # the cleared row is listed for its Reopen, never counted
    assert [r["facets"]["review_state"] for r in panel["rows"]] == ["open", "open", "cleared"]
    # the delivery pass has not run in this store: an empty "waiting" is not "nothing waits"
    assert panel["note"].startswith("Not known yet: the delivery pass has not run yet")
    assert "Nothing is waiting" not in panel["note"]


def test_the_queue_says_when_the_delivery_pass_last_ran_once_it_has(tmp_path: Path) -> None:
    from levain.coordinator import deliver

    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()                     # the store exists before the pass: a pass on none leaves no mark
    deliver.run(store)
    with review.read_transaction(store.open_readonly) as conn:
        ran_at = review.last_pass(conn)
    assert ran_at
    note = _cockpit(d).panel(REVIEW_ID)["note"]
    assert note == f"Nothing is waiting on you (the delivery pass last ran {ran_at})."
    rows = {r["id"]: r["body"] for r in _cockpit(d).panel(MORNING_ID)["rows"]}
    assert rows["delivery"].endswith(f"the delivery pass last ran {ran_at}")


def test_the_waiting_line_is_review_waiting_on_you_and_nothing_recomputed(tmp_path: Path, monkeypatch) -> None:
    d = _dir(tmp_path)
    CoordStore(d)._connect().close()
    seen = []

    def fake(store, *, sign_open, answer_open, conn=None):
        seen.append(store)
        return {"threads": {"t-1": [{"kind": "sign", "ref_id": "r", "reply_id": "p", "text": "x"}]},
                "loose": [{"kind": "answer", "ref_id": "q", "reply_id": "p2", "text": "y"}], "unresolved": []}

    monkeypatch.setattr(review, "waiting_on_you", fake)
    assert _cockpit(d).panel(REVIEW_ID)["note"] == "Waiting on you: 2 (1 SIGN, 1 ANSWER)" and len(seen) == 1


def test_the_morning_card_draws_the_real_card_of_an_empty_store(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()                    # a store in use: its tables exist (a card never makes them)
    c = morning.card(store)
    rows = _cockpit(d).panel(MORNING_ID)["rows"]
    assert [r["id"] for r in rows] == [f"away:{a}" for a in c["away"]] + ["delivery"]
    assert all(r["body"].endswith(": off") for r in rows if r["id"].startswith("away:"))
    assert "has not run yet" in rows[-1]["body"]


def test_the_morning_card_rows_for_away_undecided_budget_and_waiting(tmp_path: Path) -> None:
    from levain.cockpit import coordinator_panels as cp

    c = {"away": {"claude_code": {"away": True, "since": "2026-10-25T20:10:00Z"}, "codex": {"away": None}},
         "undecided": [{"request_id": "r1", "agent": "claude_code", "tool": "Bash", "what": "git push", "gap": 7,
                        "thread_id": "t-1", "rule": "levain consent rule 7 allow|deny"}],
         "budgets": [{"thread_id": "t-2", "envelope_id": "e1", "unreadable": "OSError: gone", "refusal": True},
                     {"thread_id": "t-3", "envelope_id": "e2", "unreadable": "", "refusal": True,
                      "used": {"turns": 5.0, "tokens": 9.0}, "budget": {"turns": 5, "tokens": 100},
                      "sign": {"reply_id": "rp", "open": True}}],
         "waiting": {"threads": {"t-1": [{"kind": "sign", "reply_id": "w1", "text": "Allow?"}]}, "loose": [],
                     "unresolved": [{"kind": "answer", "reply_id": "w2", "text": "?"}]},
         "errors": ["undecided: boom"]}
    from levain.coordinator import budget

    c["budgets"][1]["used"] = {k: 1.0 for k in budget.METERS}
    c["budgets"][1]["budget"] = {k: 1 for k in budget.METERS}
    rows = {r.id: r.body for r in cp._morning_rows(c)}
    assert rows["away:claude_code"].startswith("Away · claude_code: ON since 2026-10-25T20:10:00Z")
    assert "UNKNOWN" in rows["away:codex"]
    assert "UNDECIDED · gap #7" in rows["undecided:r1"] and "rule it: levain consent rule 7" in rows["undecided:r1"]
    assert "UNREADABLE, new steps stop" in rows["budget:t-2"]
    assert "CROSSED: new steps stop" in rows["budget:t-3"] and "SIGN to extend waits" in rows["budget:t-3"]
    assert "Waiting · SIGN · t-1" in rows["waiting:w1"] and "unreadable" in rows["waiting:w2"]
    assert rows["error:0"].endswith("undecided: boom")


def test_the_served_cockpit_draws_the_panels_by_the_stores_presence(tmp_path: Path, monkeypatch) -> None:
    """Through make_server (which attaches the consent reader whose directory the panels read), over the store a
    LEVAIN_HOME names: absent -> no panel; present -> both. The morning card offers no verb; the queue's row offers
    review_clear (tests/test_review_clear_paths.py holds who may fire it)."""
    import json
    import urllib.error
    import urllib.request

    from tests.test_cockpit_k2a import _serve

    home = tmp_path / "home"
    (home / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    _root, src, _ids_ = _install(tmp_path)

    def get(url: str) -> tuple[int, dict]:
        req = urllib.request.Request(url, headers={"X-Levain-Write-Token": "T"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        assert get(f"{url}/cockpit/panel/{REVIEW_ID}.json?profile=full")[0] == 404
        store = CoordStore(home / "consent")
        review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
        for pid in (REVIEW_ID, MORNING_ID):
            status, body = get(f"{url}/cockpit/panel/{pid}.json?profile=full")
            assert status == 200 and body["rows"], (pid, status, body)
            offered = {a["verb"] for r in body["rows"] for a in r.get("actions") or ()}
            assert offered == ({"review_clear"} if pid == REVIEW_ID else set()), (pid, offered)


def _tree_bytes(d: Path) -> dict[str, bytes]:
    """The database file itself. A read-only SQLite open of a WAL database may leave its -shm/-wal side files; they
    hold no schema, so the claim tested is that the database file is not written."""
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file() and p.suffix == ".db"}


def test_a_dispatch_db_with_only_job_tables_draws_no_panel_and_is_byte_unchanged(tmp_path: Path) -> None:
    """dispatch.db is the job store's file too: its existence proves nothing about the coordinator (C4-b L3 r1)."""
    from levain.dispatch.jobs import JobStore

    d = _dir(tmp_path)
    JobStore(d)._connect().close()
    before = _tree_bytes(d)
    ck = _cockpit(d)
    assert REVIEW_ID not in _ids(ck) and MORNING_ID not in _ids(ck)
    assert _tree_bytes(d) == before
    import sqlite3

    conn = sqlite3.connect(f"file:{d / 'dispatch.db'}?mode=ro", uri=True)
    try:
        assert "threads" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def test_a_cockpit_read_of_a_coordinator_store_leaves_every_file_byte_unchanged(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    review.file_item(store, "seat", {"text": "seat lane-a died"}, dedupe_key="a")
    before = _tree_bytes(d)
    ck = _cockpit(d)
    assert REVIEW_ID in _ids(ck) and ck.panel(REVIEW_ID)["rows"] and ck.panel(MORNING_ID)["rows"]
    assert _tree_bytes(d) == before


def test_a_store_deleted_and_recreated_is_read_afresh(tmp_path: Path) -> None:
    """No store outlives the read that opened it (C4-b L3 r1): a recreated file is the file read, and a recreated
    job-only file draws no panel and no error."""
    from levain.dispatch.jobs import JobStore

    d = _dir(tmp_path)
    review.file_item(CoordStore(d), "seat", {"text": "the first store"}, dedupe_key="a")
    ck = _cockpit(d)
    assert "the first store" in ck.panel(REVIEW_ID)["rows"][0]["body"]
    for f in d.iterdir():
        f.unlink()
    review.file_item(CoordStore(d), "seat", {"text": "the second store"}, dedupe_key="b")
    assert "the second store" in ck.panel(REVIEW_ID)["rows"][0]["body"]
    for f in d.iterdir():
        f.unlink()
    JobStore(d)._connect().close()
    assert REVIEW_ID not in _ids(ck) and MORNING_ID not in _ids(ck)


def test_a_corrupt_store_is_one_panels_error_and_never_the_manifests(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    (d / "dispatch.db").write_bytes(b"this is not a sqlite database" * 50)
    ck = _cockpit(d)
    m = ck.manifest(CRED)
    assert not m.get("errors"), m.get("errors")
    assert "now" in _ids(ck) and REVIEW_ID in _ids(ck) and MORNING_ID in _ids(ck)
    for pid in (REVIEW_ID, MORNING_ID):
        rows = ck.panel(pid)["rows"]
        assert len(rows) == 1 and rows[0]["body"].startswith("Could not read: "), rows


def test_uncounted_usage_is_shown_never_a_measured_looking_zero() -> None:
    from levain.cockpit import coordinator_panels as cp
    from levain.coordinator import budget

    b = {"thread_id": "t-9", "envelope_id": "e9", "unreadable": "", "refusal": False,
         "used": {k: 0.0 for k in budget.METERS}, "budget": {k: 10 for k in budget.METERS}, "sign": None,
         "uncounted": [["claude_code", "sess-w1"], ["codex", "sess-w2"]]}
    c = {"away": {}, "undecided": [], "budgets": [b], "waiting": {"threads": {}, "loose": [], "unresolved": []},
         "errors": []}
    body = cp._morning_rows(c)[0].body
    assert "not counted by Q13 yet: sess-w1, sess-w2" in body
    b["uncounted"] = []
    assert "not counted" not in cp._morning_rows(c)[0].body


def test_each_discovery_hands_the_engine_fresh_read_callables(tmp_path: Path) -> None:
    """The engine matches a retired panel's state to its spec by the read callable's identity, so a panel that is
    re-registered after retiring must bring a new one (``Cockpit.discover`` docstring)."""
    from datetime import datetime, timezone

    from levain.cockpit.engine import ReadContext

    d = _dir(tmp_path)
    CoordStore(d)._connect().close()
    discover = coordinator_discoverer(lambda: d)
    ctx = ReadContext(datetime.now(timezone.utc))
    first = {s.id: s.read for s in discover(ctx)}
    second = {s.id: s.read for s in discover(ctx)}
    assert set(first) == {REVIEW_ID, MORNING_ID, "coord-status"} == set(second)
    assert all(first[i] is not second[i] for i in first)


def test_a_corrupt_store_file_is_a_could_not_read_row_and_a_logic_error_is_labelled_a_panel_fault(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()
    ck = _cockpit(d)
    assert MORNING_ID in _ids(ck)

    def boom(_store):
        raise KeyError("a bug in the card")

    ck2 = Cockpit(entity=lambda ctx: {"name": "t"})
    ck2.discover(coordinator_discoverer(lambda: d, card=boom), retire=True)
    row = ck2.panel(MORNING_ID)["rows"][0]
    assert row["title"] == "Panel fault" and "not the store: KeyError" in row["body"]
    (d / "dispatch.db").write_bytes(b"not a database" * 100)
    ck3 = _cockpit(d)
    assert REVIEW_ID in _ids(ck3)
    row = ck3.panel(REVIEW_ID)["rows"][0]
    assert row["title"] == "Could not read" and "the coordinator store:" in row["body"]


def test_the_feed_label_table_is_the_one_review_owns_and_covers_every_feed() -> None:
    assert set(review.FEED_LABELS) == set(review.FEEDS)


def test_a_stamp_ahead_of_the_clock_reads_just_now_never_a_negative_day() -> None:
    from datetime import datetime, timezone

    from levain.cockpit import coordinator_panels as cp

    from datetime import timedelta

    from levain.cockpit.consent import utc_stamp

    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    assert cp._age(utc_stamp(now + timedelta(days=1)), now) == "just now"
    assert cp._age(utc_stamp(now - timedelta(days=1, hours=1)), now) == "1d ago"


def test_a_long_row_is_clipped_before_its_command_never_inside_it() -> None:
    from levain.cockpit import coordinator_panels as cp

    cmd = " · rule it: levain consent rule 7 allow|deny --agent claude_code --session s1 --signing-key KEY"
    row = cp._row("x", "UNDECIDED · gap #7", "a " * 400, tail=cmd)
    assert row.body.endswith(cmd) and len(row.body) <= cp._ROW_MAX


def test_a_malformed_away_entry_is_unknown_in_the_card_and_drawn_unknown(tmp_path: Path, monkeypatch) -> None:
    """C4 r1 item 5: morning.card normalises it once; the cockpit draws what the card says."""
    d = _dir(tmp_path)
    store = CoordStore(d)
    store.threads()
    monkeypatch.setattr(morning, "_away", lambda _q: {"claude_code": "garbage", "codex": None})
    assert morning.card(store)["away"] == {"claude_code": {"away": None}, "codex": {"away": None}}
    rows = {r["id"]: r["body"] for r in _cockpit(d).panel(MORNING_ID)["rows"]}
    assert rows["away:claude_code"].endswith("UNKNOWN: the store could not be read")
    assert rows["away:codex"].endswith("UNKNOWN: the store could not be read")


def test_a_row_longer_than_the_row_limit_is_clipped_at_the_end() -> None:
    from levain.cockpit import coordinator_panels as cp

    short = cp._row("a", "Title", "text " * 300, tail=" · rule it: levain consent rule 7 allow")
    assert len(short["body"] if isinstance(short, dict) else short.body) <= cp._ROW_MAX
    assert (short.body).endswith(" · rule it: levain consent rule 7 allow")          # a tail that fits stays whole
    huge = cp._row("a", "Title", "text", tail=" · " + "x" * (cp._ROW_MAX + 50))
    assert len(huge.body) <= cp._ROW_MAX
    assert huge.body.endswith(cp._SHORTENED) and huge.body.startswith("Title: text")


def test_a_command_too_long_for_the_row_is_marked_shortened_never_a_clean_prefix() -> None:
    from levain.cockpit import coordinator_panels as cp

    cmd = " · rule it: levain consent rule 7 allow --agent claude_code --session " + "s" * cp._ROW_MAX + " --signing-key KEY"
    row = cp._row("a", "UNDECIDED", "a " * 300, tail=cmd)
    assert len(row.body) <= cp._ROW_MAX and row.body.endswith(cp._SHORTENED)
    assert not row.body.endswith("KEY") and "…" in row.body
